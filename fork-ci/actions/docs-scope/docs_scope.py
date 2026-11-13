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
matches its `paths` or `paths_from`; every job runs when such a path matches no job, or when the config changed.
"Matches no job" is judged by the base's entries only: a head-only `*_from` entry can add a job, never stop a full run.

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
"""
import argparse, json, os, re, subprocess, sys

PLAIN_MODES = ('100644', '100755', '000000')   # a regular file, or (000000) the side where it does not exist
KEYS = {'docs', 'never_docs', 'never_docs_from', 'docs_only_events', 'jobs'}
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
    try:
        c = json.loads(text.encode('utf-8', 'surrogateescape'))
    except (ValueError, RecursionError) as e:
        raise Unclear(f'config {path} at {ref[:10]} is not JSON: {e}')
    if not isinstance(c, dict) or set(c) - KEYS or 'docs' not in c:
        raise Unclear(f'config {path}: needs "docs", and no other keys than {sorted(KEYS)}')
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
    return c


def read_entries(src, refs, run=git):
    """[{path prefix: label} per ref, {} where the file is not]; Unclear if no ref has the file, or one is
    malformed: an object without any named field, a field no kept object has (a typo; none kept), or an entry that is
    not a plain path (./, /, .., a wildcard) and so could never match. Entries match a path exactly or as a folder."""
    per_ref, read = [], False
    fields = src['field'] if isinstance(src['field'], list) else [src['field']]
    for ref in refs:
        found = {}
        per_ref.append(found)
        text = run('show', f'{ref}:{src["file"]}')
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
            raise Unclear(f'{src["file"]} at {ref[:10]} cannot be read as configured: {e}')
        read = True
    if not read:
        raise Unclear(f'{src["file"]} is at neither the base nor the head: its entries are unknown')
    return per_ref


def union(per_ref):
    return {k: v for found in per_ref for k, v in found.items()}


def entry_match(path, entries):
    for x, label in entries.items():
        if path == x or path.startswith(x + '/'):
            return x, label
    return None


def decide(event, config_path, head='HEAD', base='', run=git, sha='', event_path=''):
    """(docs_only, {job: 'true'|'false'}, [reason lines]); the first line is the overall reason."""
    try:
        if not plain_path(config_path) or config_path.endswith('/'):
            raise Unclear(f'the config path {config_path} is not a plain repository path (no ./, .., leading /)')
        rows, cbase, what = the_change(event, head, base, run, sha, event_path)
        c = load_config(config_path, cbase, run)
        refs = (cbase, head)
        never_from = [(s, union(read_entries(s, refs, run))) for s in c['never_docs_from']]
        jobs = {}   # name -> (globs, entries at the base or the head (run the job), entries at the base (judge "every"))
        for n, j in c['jobs'].items():
            per = [read_entries(s, refs, run) for s in j['paths_from']]
            jobs[n] = ([glob_re(g) for g in j['paths']], [union(p) for p in per], [p[0] for p in per])
    except Unclear as e:
        return False, {}, [f'full run: {e}']
    docs_globs = [glob_re(g) for g in c['docs']]
    never = [(glob_re(n['glob']), f'{n["why"]} ({n["glob"]})') for n in c['never_docs']]
    odd = {p for p, old, new in rows if old not in PLAIN_MODES or new not in PLAIN_MODES}
    not_docs = {}   # path -> why it is not docs
    for p in sorted({p for p, _, _ in rows}):
        if p in odd:
            not_docs[p] = 'a symlink or submodule, not a plain file'
        elif ODD_NAME.search(p):
            not_docs[p] = 'a name with a control character or bytes that are not UTF-8'
        elif not any(g.match(p) for g in docs_globs):
            not_docs[p] = 'not a docs file'
        elif p == config_path:
            not_docs[p] = 'the docs-scope config'
        else:
            why = next((w for g, w in never if g.match(p)), None)
            for s, entries in never_from:
                hit = why is None and entry_match(p, entries)
                if hit:
                    why = f'{s["why"]} ({hit[1]}: {hit[0]})'
            if why:
                not_docs[p] = f'a build or test input: {why}'
    lines = []
    if not not_docs and event in c['docs_only_events']:
        lines.append(f'docs-only: {len({p for p, _, _ in rows})} docs file(s) changed against {what}')
        return True, {n: 'false' for n in jobs}, lines + [f'job {n}: skip (docs-only)' for n in jobs]

    def job_hit(p, globs, froms):
        if any(g.match(p) for g in globs):
            return 'its paths'
        return next((f'{m[1]}: {m[0]}' for e in froms for m in [entry_match(p, e)] if m), None)

    # A file that is not docs but that no job names (a root build file, a workflow, the config): every job runs.
    # "Names" by the base's rules only: the PR's own registry entries may add a job, never cancel the full run.
    every = (config_path if config_path in not_docs else
             next((p for p in not_docs if not any(job_hit(p, globs, base_froms)
                                                  for globs, _, base_froms in jobs.values())), None))
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
    for n, (globs, froms, _) in jobs.items():
        hit = None if every else next(((p, h) for p in not_docs for h in [job_hit(p, globs, froms)] if h), None)
        out[n] = 'true' if every or hit else 'false'
        lines.append(f'job {n}: run (every job runs for {every})' if every else
                     f'job {n}: run ({hit[0]} matches {hit[1]})' if hit else
                     f'job {n}: skip (no changed file that is not docs matches its paths)')
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
