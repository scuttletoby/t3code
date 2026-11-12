#!/usr/bin/env python3
"""docs-scope: is this change docs-only, and which of the configured jobs does it reach?

The change:
  - pull_request: the union of two diffs, both without rename detection (a rename lists its old and its new path):
    merge base of the base and the PR head .. the PR head (every push together, never the last one alone), and
    base .. GitHub's merge commit (what merging changes on the base it was tested with). HEAD must be GitHub's merge
    commit (refs/pull/N/merge): HEAD^1 is the base, HEAD^2 the PR head.
  - any other event: --base .. HEAD, only when the caller gives --base (for example the last main commit that passed).

A path is docs when it is a plain file (no symlink or submodule), matches a `docs` glob, is not the config file, and
matches no `never_docs` glob or `never_docs_from` entry. The change is docs-only when every path is docs and the event
is in `docs_only_events`. A job runs when a path that is not docs matches its `paths` or `paths_from`.

The config is read from the BASE commit, never from the checkout, and this script runs from the action's own folder
at the caller's pinned SHA: a pull request cannot loosen the rules or the code that judge it. `*_from` files are read
at the base and the head; an entry at either counts.

Fails closed: anything unclear means docs_only=false and every job runs, with the reason printed. Jobs missing from
the `jobs` output (config unreadable) must be read as "run": callers test `!= 'false'`.

  python3 -I docs_scope.py --config .github/docs-scope.json --event "$GITHUB_EVENT_NAME" [--base SHA]
Writes docs_only=true|false, jobs=<JSON {"job": "true"|"false"}> and reason=<one line> to $GITHUB_OUTPUT.
"""
import argparse, json, os, re, subprocess, sys

PLAIN_MODES = ('100644', '100755', '000000')   # a regular file, or (000000) the side where it does not exist
KEYS = {'docs', 'never_docs', 'never_docs_from', 'docs_only_events', 'jobs'}
SOURCE_KEYS = {'file', 'list', 'where', 'field', 'label', 'why'}


class Unclear(Exception):
    """Anything that makes the answer unknown: the run is full and this is the reason."""


def git(*args):
    """stdout, or None when git fails."""
    r = subprocess.run(['git', *args], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else None


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
    return re.compile(''.join(out) + r'\Z')


def changes(a, b, run=git):
    """[(path, old mode, new mode)] between commits a and b, renames as delete + add; None on a git error."""
    out = run('diff', '--raw', '--no-renames', '--no-abbrev', '-z', a, b)
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


def the_change(event, head, base, run=git):
    """(rows, the commit the config is read at, what was compared); Unclear when the change is unknown."""
    if event == 'pull_request':
        line = (run('rev-list', '--parents', '-n', '1', head) or '').split()
        if len(line) != 3:
            raise Unclear(f"{head} is not a two-parent merge commit (GitHub's refs/pull/N/merge): the PR diff is unknown")
        _, base, pr = line
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
        extra = set(s) - SOURCE_KEYS
        if extra or not isinstance(s.get('file'), str) or 'field' not in s or (need_why and 'why' not in s):
            raise Unclear(f'config: {where} entry {json.dumps(s)[:80]} needs file and field'
                          f'{" and why" if need_why else ""} (and no other keys than {sorted(SOURCE_KEYS)})')
    return value


def load_config(path, ref, run=git):
    text = run('show', f'{ref}:{path}')
    if text is None:
        raise Unclear(f'no config {path} at the base {ref[:10]} (a PR that adds it runs in full)')
    try:
        c = json.loads(text)
    except ValueError as e:
        raise Unclear(f'config {path} at {ref[:10]} is not JSON: {e}')
    if not isinstance(c, dict) or set(c) - KEYS or 'docs' not in c:
        raise Unclear(f'config {path}: needs "docs", and no other keys than {sorted(KEYS)}')
    strings(c['docs'], 'docs')
    for i, n in enumerate(c.setdefault('never_docs', [])):
        if not isinstance(n, dict) or set(n) != {'glob', 'why'}:
            raise Unclear(f'config: never_docs[{i}] must be {{"glob", "why"}}')
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
    """{path prefix: label} of every entry the source names at any ref; Unclear if no ref has the file or one is
    malformed. Entries match a path exactly or as a folder."""
    found, read = {}, False
    fields = src['field'] if isinstance(src['field'], list) else [src['field']]
    for ref in refs:
        text = run('show', f'{ref}:{src["file"]}')
        if text is None:
            continue
        try:
            items = json.loads(text)
            for key in (src.get('list') or '').split('.') if src.get('list') else []:
                items = items[key]
            items = items if isinstance(items, list) else [items]
            for item in items:
                if any(item.get(k) != v for k, v in (src.get('where') or {}).items()):
                    continue
                label = item.get(src['label'], '?') if src.get('label') else src['file']
                for f in fields:
                    value = item.get(f) or []
                    for x in value if isinstance(value, list) else [value]:
                        if not isinstance(x, str) or not x.strip('/'):
                            raise TypeError(f'{f}: {x!r} is not a path')
                        found[x.rstrip('/')] = f'{label} {f}'
        except (ValueError, KeyError, IndexError, AttributeError, TypeError) as e:
            raise Unclear(f'{src["file"]} at {ref[:10]} cannot be read as configured: {e}')
        read = True
    if not read:
        raise Unclear(f'{src["file"]} is at neither the base nor the head: its entries are unknown')
    return found


def entry_match(path, entries):
    for x, label in entries.items():
        if path == x or path.startswith(x + '/'):
            return x, label
    return None


def decide(event, config_path, head='HEAD', base='', run=git):
    """(docs_only, {job: 'true'|'false'}, [reason lines]); the first line is the overall reason."""
    try:
        rows, cbase, what = the_change(event, head, base, run)
        c = load_config(config_path, cbase, run)
        refs = (cbase, head)
        never_from = [(s, read_entries(s, refs, run)) for s in c['never_docs_from']]
        jobs = {n: ([glob_re(g) for g in j['paths']], [read_entries(s, refs, run) for s in j['paths_from']])
                for n, j in c['jobs'].items()}
    except Unclear as e:
        return False, {}, [f'full run: {e}']
    docs_globs = [glob_re(g) for g in c['docs']]
    never = [(glob_re(n['glob']), f'{n["why"]} ({n["glob"]})') for n in c['never_docs']]
    odd = {p for p, old, new in rows if old not in PLAIN_MODES or new not in PLAIN_MODES}
    not_docs = {}   # path -> why it is not docs
    for p in sorted({p for p, _, _ in rows}):
        if p in odd:
            not_docs[p] = 'a symlink or submodule, not a plain file'
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
    if not_docs:
        first = next(iter(not_docs))
        more = f' and {len(not_docs) - 1} more' if len(not_docs) > 1 else ''
        lines.append(f'full run: {first} is {not_docs[first]}{more}')
    else:
        lines.append(f'full run: only docs changed, but a {event or "unknown"} run is not in docs_only_events')
    out = {}
    for n, (globs, froms) in jobs.items():
        hit = next(((p, 'its paths') for p in not_docs if any(g.match(p) for g in globs)), None) or next(
            ((p, f'{m[1]}: {m[0]}') for p in not_docs for e in froms for m in [entry_match(p, e)] if m), None)
        out[n] = 'true' if hit else 'false'
        lines.append(f'job {n}: run ({hit[0]} matches {hit[1]})' if hit
                     else f'job {n}: skip (no changed file that is not docs matches its paths)')
    return False, out, lines


def main(argv=None, run=git):
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    p.add_argument('--config', required=True)
    p.add_argument('--event', required=True)
    p.add_argument('--head', default='HEAD')
    p.add_argument('--base', default='')
    a = p.parse_args(argv)
    docs_only, jobs, lines = decide(a.event, a.config, a.head, a.base, run)
    print('\n'.join(lines))
    out = os.environ.get('GITHUB_OUTPUT')
    if out:
        reason = ' '.join(lines[0].split())   # one line, whatever a path holds
        with open(out, 'a') as f:
            f.write(f'docs_only={"true" if docs_only else "false"}\n'
                    f'jobs={json.dumps(jobs, separators=(",", ":"))}\nreason={reason}\n')
    return 0


if __name__ == '__main__':
    sys.exit(main())
