#!/usr/bin/env python3
"""docs-scope: is this change docs-only, and which of the configured jobs does it reach?

The change:
  - pull_request: the union of two diffs, both without rename detection (a rename lists its old and its new path):
    merge base of the base and the PR head .. the PR head (every push together, never the last one alone), and
    base .. GitHub's merge commit (what merging changes on the base it was tested with). HEAD must be GitHub's merge
    commit (refs/pull/N/merge): HEAD == $GITHUB_SHA, two parents, and HEAD^2 == the event's pull_request.head.sha.
    Only then is HEAD^1 the base (a checkout of a PR head that merged main in would otherwise pose as one).
  - any other event: --base .. HEAD, only when the caller gives --base (the last commit whose push run passed; never
    github.event.before, see README), HEAD == $GITHUB_SHA and the base is on HEAD's first-parent line (a commit that
    reached the branch through a merge's second parent, such as a fork PR's head, was never tested as the branch).

A path is docs when it is a plain file (no symlink or submodule), matches a `docs` glob, is not the config file, and
matches no `never_docs` glob or `never_docs_from` entry, and its name has no control character and is UTF-8. The change
is docs-only when every path is docs and the event is in `docs_only_events`. A job runs when a path that is not docs
matches its `paths` or `paths_from`; every job runs when such a path matches no job and no `no_build` glob, or when
the config changed. "Matches no job" is judged by the base's entries only: a head-only `*_from` entry can add a job,
never stop a full run. A path that matches `no_build` and no job builds nothing (logged, never a job output).

The config is read from the BASE commit, never from the checkout, and this script runs from the action's own folder
at the caller's pinned SHA: a pull request cannot loosen the rules or the code that judge it. `*_from` files are read
at the base and the head: an entry at either makes a file not docs or runs a job, but only the base's entries can keep
a file that no job names from running every job.

Fails closed: anything unclear, including an error this script did not foresee, means docs_only=false and every job
runs, with the reason printed. Jobs missing from the `jobs` output (config unreadable) must be read as "run": callers
test `!= 'false'`. Printed lines escape control characters, so a file name cannot inject a workflow command.

  python3 -I docs_scope.py --config .github/docs-scope.json --event "$GITHUB_EVENT_NAME" [--base SHA]
Reads $GITHUB_SHA and $GITHUB_EVENT_PATH (set by the runner).
Writes docs_only=true|false, jobs=<JSON {"job": "true"|"false"}> and reason=<one line> to $GITHUB_OUTPUT.

Tools outside a workflow ask the same rules about one path through docs_rule(), job_rule() and rules() (README,
"Python API"); decide() itself runs on those rules, so the two cannot differ.
"""
import argparse, copy, json, os, re, subprocess, sys

PLAIN_MODES = ('100644', '100755', '000000')   # a regular file, or (000000) the side where it does not exist
KEYS = {'docs', 'never_docs', 'never_docs_from', 'docs_only_events', 'jobs', 'no_build'}
DEFAULT_CONFIG = '.github/docs-scope.json'   # action.yml's default `config`
SOURCE_KEYS = {'file', 'list', 'where', 'field', 'label', 'why'}
ODD_NAME = re.compile(r'[\x00-\x1f\x7f-\x9f\ud800-\udfff]')   # a control character, or a byte that is not UTF-8


class Unclear(Exception):
    """Anything that makes the answer unknown: the run is full and this is the reason."""


def git(*args):
    """stdout, or None when git fails. Bytes that are not UTF-8 survive as lone surrogates (never a crash)."""
    r = subprocess.run(['git', *args], capture_output=True)
    return r.stdout.decode('utf-8', 'surrogateescape') if r.returncode == 0 else None


def shown(text):
    """One printable line: control characters, line separators and non-UTF-8 bytes escaped."""
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in text)


def plain_path(x):
    """A repository path as git prints it: no ./, .., //, leading / or glob character (a trailing / is a folder)."""
    if not isinstance(x, str) or ODD_NAME.search(x) or re.search(r'[*?\[\]{}\\]', x):
        return False
    return all(p not in ('', '.', '..') for p in (x[:-1] if x.endswith('/') else x).split('/'))


def glob_re(pattern):
    """A glob from the repository root: `**/` is any number of folders (none too), `**` anything, `*` and `?` stay
    inside one name."""
    i, out = 0, []
    while i < len(pattern):
        if pattern.startswith('**/', i):
            out.append('(?:[^/]+/)*'); i += 3
        elif pattern.startswith('**', i):
            out.append('.*'); i += 2
        elif pattern[i] in '*?':
            out.append('[^/]*' if pattern[i] == '*' else '[^/]'); i += 1
        else:
            out.append(re.escape(pattern[i])); i += 1
    return re.compile(''.join(out) + r'\Z', re.DOTALL)   # `**` crosses a newline in a name too


def changes(a, b, run=git):
    """[(path, old mode, new mode)] between commits a and b, renames as delete + add; None on a git error."""
    # --ignore-submodules=none: a .gitmodules `ignore = all` or a runner's diff.ignoreSubmodules hides no gitlink bump
    out = run('diff', '--raw', '--no-renames', '--no-abbrev', '--ignore-submodules=none', '-z', a, b)
    if out is None:
        return None
    parts, rows = out.split('\0'), []
    for i in range(0, len(parts) - 1, 2):
        meta, path = parts[i], parts[i + 1]
        if not meta.startswith(':'):
            return None   # not the format we expect: say "unclear"
        fields = meta[1:].split()
        rows.append((path, fields[0], fields[1]))
    return rows


def event_head_sha(event_path):
    """pull_request.head.sha from the runner's event file; Unclear when it is not there."""
    try:
        with open(event_path or '', 'rb') as f:
            sha = json.load(f)['pull_request']['head']['sha']
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Unclear(f'cannot read pull_request.head.sha from the event file ({type(e).__name__}): the PR is unknown')
    if not isinstance(sha, str) or not re.fullmatch(r'[0-9a-f]{40,64}', sha):
        raise Unclear('the event file has no pull_request.head.sha: the PR is unknown')
    return sha


def the_change(event, head, base, run=git, sha='', event_path=''):
    """(rows, the commit the config is read at, what was compared); Unclear when the change is unknown."""
    head_sha = (run('rev-parse', '-q', '--verify', f'{head}^{{commit}}') or '').strip()
    if not head_sha or head_sha != sha:
        raise Unclear(f'HEAD {head_sha[:10] or "?"} is not the commit this run is for ($GITHUB_SHA '
                      f'{sha[:10] or "unset"}): the change is unknown')
    if event == 'pull_request':
        line = (run('rev-list', '--parents', '-n', '1', head) or '').split()
        if len(line) != 3:
            raise Unclear(f"{head} is not a two-parent merge commit (GitHub's refs/pull/N/merge): the PR diff is unknown")
        _, base, pr = line
        if pr != event_head_sha(event_path):
            raise Unclear(f"HEAD^2 {pr[:10]} is not the event's pull_request.head.sha: HEAD is not GitHub's merge commit")
        mb = (run('merge-base', base, pr) or '').strip()
        if not mb:
            raise Unclear(f'no merge base between the base {base[:10]} and the PR head {pr[:10]}')
        pairs, what = ((mb, pr), (base, head)), f'the merge base {mb[:10]}'
    elif not base:
        raise Unclear(f'a {event or "unknown"} run without a base runs in full')
    else:
        resolved = (run('rev-parse', '-q', '--verify', f'{base}^{{commit}}') or '').strip()
        if not resolved:
            raise Unclear(f'the base {base} is not a commit here (shallow checkout or force push?)')
        if resolved not in (run('rev-list', '--first-parent', head) or '').split():
            raise Unclear(f'the base {resolved[:10]} is not on the first-parent line of {head} (a merged branch\'s '
                          f'commit, another branch, or a force push): it was never tested as this branch')
        base, pairs, what = resolved, ((resolved, head),), f'the base {resolved[:10]}'
    rows = []
    for a, b in pairs:
        got = changes(a, b, run)
        if got is None:
            raise Unclear(f'git diff {a[:10]} {b[:10]} failed')
        rows += got
    if not rows:
        raise Unclear('the change touches no file')
    return rows, base, what


def strings(value, where):
    if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
        raise Unclear(f'config: {where} must be a list of non-empty strings')
    return value


def sources(value, where, need_why=False):
    if not isinstance(value, list) or not all(isinstance(s, dict) for s in value):
        raise Unclear(f'config: {where} must be a list of objects')
    for s in value:
        field = s.get('field')
        fields = field if isinstance(field, list) else [field]
        ok = (not set(s) - SOURCE_KEYS and isinstance(s.get('file'), str) and plain_path(s['file'])
              and fields and all(isinstance(f, str) and f for f in fields)
              and all(isinstance(s.get(k, 'x'), str) and s.get(k, 'x') for k in ('list', 'label', 'why'))
              and isinstance(s.get('where', {}), dict) and (not need_why or 'why' in s))
        if not ok:
            raise Unclear(f'config: {where} entry {json.dumps(s)[:80]} needs a plain file path and field (strings)'
                          f'{" and why" if need_why else ""}, where an object, no other keys than {sorted(SOURCE_KEYS)}')
    return value


def load_config(path, ref, run=git):
    text = run('show', f'{ref}:{path}')
    if text is None:
        raise Unclear(f'no config {path} at the base {ref[:10]} (a PR that adds it runs in full)')
    return parse_config(text, f'{path} at {ref[:10]}')


def parse_config(text, where='the config'):
    """The config from its JSON text, checked (check_config); Unclear when it is not JSON."""
    try:
        c = json.loads(text.encode('utf-8', 'surrogateescape') if isinstance(text, str) else text)
    except (ValueError, RecursionError) as e:
        raise Unclear(f'config {where} is not JSON: {e}')
    return check_config(c, where)


def check_config(c, where='the config'):
    """c with its defaults filled in; Unclear for an unknown key or a wrong type (a typo must not drop a rule)."""
    if not isinstance(c, dict) or set(c) - KEYS or 'docs' not in c:
        raise Unclear(f'config {where}: needs "docs", and no other keys than {sorted(KEYS)}')
    strings(c['docs'], 'docs')
    for i, n in enumerate(c.setdefault('never_docs', [])):
        if not isinstance(n, dict) or set(n) != {'glob', 'why'} or not all(
                isinstance(n[k], str) and n[k] for k in ('glob', 'why')):
            raise Unclear(f'config: never_docs[{i}] must be {{"glob", "why"}}, both non-empty strings')
    sources(c.setdefault('never_docs_from', []), 'never_docs_from', need_why=True)
    strings(c.setdefault('docs_only_events', ['pull_request']), 'docs_only_events')
    jobs = c.setdefault('jobs', {})
    if not isinstance(jobs, dict):
        raise Unclear('config: jobs must be an object')
    for name, j in jobs.items():
        if not isinstance(j, dict) or set(j) - {'paths', 'paths_from'} or not j:
            raise Unclear(f'config: jobs.{name} takes "paths" and/or "paths_from"')
        strings(j.setdefault('paths', []), f'jobs.{name}.paths')
        sources(j.setdefault('paths_from', []), f'jobs.{name}.paths_from')
    strings(c.setdefault('no_build', []), 'no_build')
    return c


def read_entries(src, refs, run=git):
    """[{path prefix: label} per ref, {} where the file is not], read with git show (entries_at). Internal: tools
    outside a workflow use rules(), docs_rule() or job_rule()."""
    return entries_at(src, [(ref[:10], run('show', f'{ref}:{src["file"]}')) for ref in refs])


def entries_at(src, sides):
    """[{path prefix: label} per side, {} where the file is not]; sides is [(where, the file's text or None)].
    Unclear if no side has the file, or one is malformed: an object without any named field, a field no kept object
    has (a typo; none kept), or an entry that is not a plain path (./, /, .., a wildcard) and so could never match.
    Entries match a path exactly or as a folder."""
    per_ref, read = [], False
    fields = src['field'] if isinstance(src['field'], list) else [src['field']]
    for where, text in sides:
        found = {}
        per_ref.append(found)
        if text is None:
            continue
        try:
            items = json.loads(text.encode('utf-8', 'surrogateescape'))
            for key in src['list'].split('.') if 'list' in src else []:
                items = items[key]
            items = items if isinstance(items, list) else [items]
            seen = set()
            for item in items:
                if any(item.get(k) != v for k, v in src.get('where', {}).items()):
                    continue
                label = item.get(src['label'], '?') if 'label' in src else src['file']
                if not any(f in item for f in fields):
                    raise KeyError(f'an object ({shown(str(label))[:40]}) has none of the fields {fields}')
                for f in (f for f in fields if f in item):
                    seen.add(f)
                    value = item[f]
                    for x in value if isinstance(value, list) else [value]:
                        if not isinstance(x, str) or not plain_path(x):
                            raise TypeError(f'{f}: {shown(repr(x))[:80]} is not a plain repository path')
                        found[x.rstrip('/')] = f'{label} {f}'
            if set(fields) - seen:   # a typo, an empty list, or a "where" that keeps nothing
                raise KeyError(f'no kept object has the field(s) {sorted(set(fields) - seen)}')
        except (ValueError, KeyError, IndexError, AttributeError, TypeError, RecursionError) as e:
            raise Unclear(f'{src["file"]} at {where} cannot be read as configured: {e}')
        read = True
    if not read:
        at = ('at neither the base nor the head' if len(sides) == 2 else
              'not in ' + ' nor '.join(w for w, _ in sides) if sides else 'read nowhere')
        raise Unclear(f'{src["file"]} is {at}: its entries are unknown')
    return per_ref


def union(per_ref):
    return {k: v for found in per_ref for k, v in found.items()}


def entry_match(path, entries):
    for x, label in entries.items():
        if path == x or path.startswith(x + '/'):
            return x, label
    return None


class Rules:
    """A config's rules, with every `*_from` list read at each side (the action: the base, then the head). decide()
    judges a change with it; tools outside a workflow ask it about one path (rules(), docs_rule(), job_rule())."""

    def __init__(self, config, sides, config_path=DEFAULT_CONFIG):
        """config: checked (check_config); sides: [(where, read(file) -> its text or None)], the base first."""
        self.config_path = config_path
        self.docs_only_events = config['docs_only_events']
        self.docs = [glob_re(g) for g in config['docs']]
        self.never = [(glob_re(n['glob']), f'{n["why"]} ({n["glob"]})') for n in config['never_docs']]

        def per_side(s):
            return entries_at(s, [(where, read(s['file'])) for where, read in sides])
        self.never_from = [(s, union(per_side(s))) for s in config['never_docs_from']]
        self.jobs = {}   # name -> (globs, entries at any side (run the job), entries at the base (judge "every"))
        for n, j in config['jobs'].items():
            per = [per_side(s) for s in j['paths_from']]
            self.jobs[n] = ([glob_re(g) for g in j['paths']], [union(p) for p in per], [p[0] for p in per])
        self.no_build = [(glob_re(g), g) for g in config['no_build']]

    def not_docs(self, path, plain=True):
        """None when path is docs, else why it is not. plain=False: the path is a symlink or submodule."""
        if not plain:
            return 'a symlink or submodule, not a plain file'
        if ODD_NAME.search(path):
            return 'a name with a control character or bytes that are not UTF-8'
        if not any(g.match(path) for g in self.docs):
            return 'not a docs file'
        if path == self.config_path:
            return 'the docs-scope config'
        why = next((w for g, w in self.never if g.match(path)), None)
        for s, entries in self.never_from:
            hit = why is None and entry_match(path, entries)
            if hit:
                why = f'{s["why"]} ({hit[1]}: {hit[0]})'
        return f'a build or test input: {why}' if why else None

    def job_hit(self, name, path, base_only=False):
        """Why job name's paths or paths_from name path (any side, or the base's alone), else None."""
        globs, froms, base_froms = self.jobs[name]
        if any(g.match(path) for g in globs):
            return 'its paths'
        return next((f'{m[1]}: {m[0]}' for e in (base_froms if base_only else froms)
                     for m in [entry_match(path, e)] if m), None)

    def builds_nothing(self, path):
        """The no_build glob path matches when no job names it at any side, else None."""
        if any(self.job_hit(n, path) for n in self.jobs):
            return None
        return next((g for r, g in self.no_build if r.match(path)), None)

    def runs_every_job(self, path):
        """True when a change to path (not docs) runs every job: the config itself, or a path that no job names at
        the base and no no_build glob matches."""
        return path == self.config_path or not (any(self.job_hit(n, path, base_only=True) for n in self.jobs)
                                                or any(r.match(path) for r, _ in self.no_build))

    def job_runs(self, name, path, plain=True):
        """Why a change of path alone runs job name, as decide() says it, or None (the job is skipped). A job the
        config does not have runs (the action leaves it out of `jobs`, and callers read a missing job as "run")."""
        if self.not_docs(path, plain) is None:
            return None
        if name not in self.jobs:
            return f'{name} is not a job of the config (a missing job reads as "run")'
        if self.runs_every_job(path):
            return f'every job runs for {path}'
        hit = self.job_hit(name, path)
        return f'{path} matches {hit}' if hit else None


def rules(config, read, config_path=DEFAULT_CONFIG):
    """Rules from config (a dict or its JSON text, checked as the action checks it) with each `*_from` list read by
    read(file) -> its text or None. read may be a list of such readers, the base's first, as the action reads the
    base and the head: an entry at any side counts, and "no job names it" is judged by the first alone. Unclear when
    the config or a list cannot be read as configured (the action then runs every job)."""
    c = parse_config(config) if isinstance(config, (str, bytes)) else check_config(copy.deepcopy(config))
    readers = list(read) if isinstance(read, (list, tuple)) else [read]
    names = ['the tree'] if len(readers) == 1 else ['the base', 'the head'] if len(readers) == 2 else \
        [f'side {i + 1}' for i in range(len(readers))]
    sides = list(zip(names, readers))
    return Rules(c, sides, config_path)


def docs_rule(config, read, config_path=DEFAULT_CONFIG):
    """is_docs(path) -> bool by the action's rule (rules() says how config and read are taken). A config or list that
    cannot be read: nothing is docs (the action runs in full). A symlink or submodule is never docs: ask
    rules(...).not_docs(path, plain=False) for one."""
    try:
        r = rules(config, read, config_path)
    except Unclear:
        return lambda path: False
    return lambda path: r.not_docs(path) is None


def job_rule(config, job, read, config_path=DEFAULT_CONFIG):
    """runs(path) -> bool: does a change of this path run job, as decide() answers it (docs never, a path no job
    names at the base or the config every job, else the job's own paths and paths_from at any side). A config or list
    that cannot be read: every path runs it (the action runs in full)."""
    try:
        r = rules(config, read, config_path)
    except Unclear:
        return lambda path: True
    return lambda path: r.job_runs(job, path) is not None


def decide(event, config_path, head='HEAD', base='', run=git, sha='', event_path=''):
    """(docs_only, {job: 'true'|'false'}, [reason lines]); the first line is the overall reason."""
    try:
        if not plain_path(config_path) or config_path.endswith('/'):
            raise Unclear(f'the config path {config_path} is not a plain repository path (no ./, .., leading /)')
        rows, cbase, what = the_change(event, head, base, run, sha, event_path)
        c = load_config(config_path, cbase, run)
        r = Rules(c, [(ref[:10], lambda f, ref=ref: run('show', f'{ref}:{f}')) for ref in (cbase, head)],
                  config_path)
    except Unclear as e:
        return False, {}, [f'full run: {e}']
    jobs = r.jobs
    odd = {p for p, old, new in rows if old not in PLAIN_MODES or new not in PLAIN_MODES}
    not_docs = {}   # path -> why it is not docs
    for p in sorted({p for p, _, _ in rows}):
        why = r.not_docs(p, plain=p not in odd)
        if why:
            not_docs[p] = why
    lines = []
    if not not_docs and event in r.docs_only_events:
        lines.append(f'docs-only: {len({p for p, _, _ in rows})} docs file(s) changed against {what}')
        return True, {n: 'false' for n in jobs}, lines + [f'job {n}: skip (docs-only)' for n in jobs]

    # A file that is not docs but that no job names (a root build file, a workflow, the config): every job runs.
    # "Names" by the base's rules only: the PR's own registry entries may add a job, never cancel the full run.
    # A no_build glob names a file too (it builds nothing), so it does not run every job.
    every = (config_path if config_path in not_docs else next((p for p in not_docs if r.runs_every_job(p)), None))
    more = f' and {len(not_docs) - 1} more' if len(not_docs) > 1 else ''
    if every:
        tail = ': every job runs' if every == config_path else ', which no job names at the base: every job runs'
        lines.append(f'full run: {every} is {not_docs[every]}{more}{tail}')
    elif not_docs:
        first = next(iter(not_docs))
        lines.append(f'scoped run: {first} is {not_docs[first]}{more}; only the jobs these files reach run')
    else:
        lines.append(f'not docs-only: only docs changed, but a {event or "unknown"} run is not in docs_only_events')
    out = {}
    for n in jobs:
        hit = None if every else next(((p, h) for p in not_docs for h in [r.job_hit(n, p)] if h), None)
        out[n] = 'true' if every or hit else 'false'
        lines.append(f'job {n}: run (every job runs for {every})' if every else
                     f'job {n}: run ({hit[0]} matches {hit[1]})' if hit else
                     f'job {n}: skip (no changed file that is not docs matches its paths)')
    idle = [(p, g) for p in not_docs if p != config_path for g in [r.builds_nothing(p)] if g]
    if idle:
        more = f' and {len(idle) - 1} more' if len(idle) > 1 else ''
        lines.append(f'no_build: {idle[0][0]} builds nothing (matches {idle[0][1]}){more}')
    return False, out, lines


def main(argv=None, run=git):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', required=True)
    p.add_argument('--event', required=True)
    p.add_argument('--head', default='HEAD')
    p.add_argument('--base', default='')
    a = p.parse_args(argv)
    try:
        docs_only, jobs, lines = decide(a.event, a.config, a.head, a.base, run, os.environ.get('GITHUB_SHA', ''),
                                        os.environ.get('GITHUB_EVENT_PATH', ''))
    except Exception as e:   # an error nobody foresaw is "unclear" too: a full run, never a crash without outputs
        docs_only, jobs, lines = False, {}, [f'full run: docs-scope failed ({type(e).__name__}: {e})']
    lines = [shown(line) for line in lines]   # a file name cannot start a line, so no ::workflow-command::
    print('\n'.join(lines))
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        with open(out, 'a') as f:
            f.write(f'docs_only={"true" if docs_only else "false"}\n'
                    f'jobs={json.dumps(jobs, separators=(",", ":"))}\nreason={lines[0]}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
