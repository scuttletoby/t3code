#!/usr/bin/env python3
"""doc-check: rules for the Markdown files agents read, on the files a change adds or modifies (or on every file).

  * size budgets (lines and bytes) for instruction files, matched by glob, first match wins; above `warn_at` of a
    budget a WARN line that never fails;
  * no history in instruction files (changelog blocks, "is live on" claims, dated "Updated" banners);
  * every relative link resolves to a file or folder in the repository (code fences, opened as CommonMark does, and
    inline code are skipped; `/x` is from the repository root, as GitHub reads it; a link that leaves the repository is broken); with --base, a
    file the change deletes or renames away must have no link left to it in any file the config selects;
  * a newly added file whose name looks like a dated report, handoff or review belongs in the pull request or an
    issue, not in the repository;
  * a checked file is UTF-8 text that can be read (anything else fails, and its size is still checked); a symlink is
    checked as the file it points to, which must be in the repository and not in .git (with --base, a change that
    removes the file an untouched symlink points to fails). Sizes are the bytes on disk (CRLF included).

The rules come from the project's JSON config (README "Config"); every key is optional and a missing, empty or `{}`
config means the defaults. With --base the config is read at the base commit, never from the checkout, so a change
cannot loosen the rules that judge it through the config (a change to the config applies from the next change; the
calling workflow is the change's own, so only review guards it); with --all (or --files) it is read from the checkout.
A config that is not valid JSON, has an unknown key or a value of the wrong type is an error (exit 2), never a pass.

    python3 -I doc_check.py --base origin/main [--config .github/doc-check.json]   # changed files (CI)
    python3 -I doc_check.py --all                                                  # every tracked .md file
    python3 -I doc_check.py --files a.md b.md                                      # these files
Run from the repository root. Exit 0: no problem (warnings allowed); 1: a rule is broken; 2: the config, git or the
base could not be read. Read-only and stateless: a re-run gives the same output.
"""
import argparse, json, os, re, subprocess, sys
from urllib.parse import unquote

DEFAULT_CONFIG = '.github/doc-check.json'
DEFAULTS = {
    'files': ['**/*.md'],
    'exclude': [],
    'budgets': [
        {'glob': 'AGENTS.md', 'lines': 150, 'bytes': 12000},
        {'glob': '**/AGENTS.md', 'lines': 100, 'bytes': 8000},
        {'glob': '**/CLAUDE.md', 'lines': 40, 'bytes': 3000},
        {'glob': '**/SKILL.md', 'lines': 250, 'bytes': 8000},
    ],
    'warn_at': 0.9,
    'history': [
        {'pattern': r'<!--\s*(RELEASE|HOTFIX|[A-Z0-9 ./+-]{6,}\d{4}-\d{2}-\d{2})',
         'why': 'changelog block (history belongs in git and releases)'},
        {'pattern': r'\b(is|are) (now )?(live|deployed) on\b', 'flags': 'i',
         'why': '"live/deployed on" claim (ask the live source, not a doc)'},
        {'pattern': r'^[ \t>*_#-]*(Updated|Last updated)[ \t:*_]*\d{4}-\d{2}-\d{2}', 'flags': 'im',
         'why': 'dated "Updated" banner'},
    ],
    'report_name': r'(REVIEW|AUDIT|HANDOFF|TAKEOVER|REPORT)[^/]*\d{4}-\d{2}-\d{2}|\d{4}-?\d{2}-?\d{2}.*(REVIEW|AUDIT|HANDOFF)',
    'report_name_flags': 'i',
    'links': True,
}
FLAGS = {'i': re.I, 'm': re.M, 's': re.S}
LINK = re.compile(r'\]\(\s*(?:<([^<>\n]+)>|([^()\s<>]+))(?:\s+(?:"[^"\n]*"|\'[^\'\n]*\'))?\s*\)')
# CommonMark: a fence opens or closes with 0-3 spaces of indent, counted from the content of the list item it is in.
FENCE = re.compile(r'(`{3,}|~{3,})')
LIST_ITEM = re.compile(r'([-*+]|\d{1,9}[.)])(?=[ \t]|$)')
INLINE_CODE = re.compile(r'(`+)(?!`).*?(?<!`)\1(?!`)')
SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'}
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '--no-replace-objects')


class ConfigError(Exception):
    """The config or the change cannot be read: exit 2."""


def git(*args):
    """stdout as text (undecodable bytes kept as surrogates), or None when git fails."""
    r = subprocess.run(['git', *GIT_SAFE, *args], capture_output=True, env={**os.environ, **GIT_ENV})
    return r.stdout.decode('utf-8', 'surrogateescape') if r.returncode == 0 else None


def shown(text):
    """One printable line: a file name cannot inject a workflow command or a second line."""
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in str(text))


def glob_re(pattern):
    """A glob from the repository root: `**/` any number of folders (none too), `**` anything, `*` `?` one name."""
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
    return re.compile(''.join(out) + r'\Z', re.DOTALL)


def _regex(pattern, flags, where):
    if not isinstance(pattern, str) or not isinstance(flags, str) or set(flags) - set(FLAGS):
        raise ConfigError(f'{where}: pattern must be a string and flags only i, m, s')
    try:
        return re.compile(pattern, sum(FLAGS[f] for f in set(flags)))
    except re.error as e:
        raise ConfigError(f'{where}: not a regular expression ({e})')


def parse_config(text, where):
    """The rules from the config's text (None or blank: the defaults). Each key present replaces its default."""
    raw = {}
    if text is not None and text.strip():
        try:
            raw = json.loads(text)
        except ValueError as e:
            raise ConfigError(f'{where}: not valid JSON ({e})')
        if not isinstance(raw, dict):
            raise ConfigError(f'{where}: must be a JSON object')
    unknown = sorted(k for k in raw if k not in DEFAULTS and not k.startswith('_'))
    if unknown:
        raise ConfigError(f'{where}: unknown key(s) {", ".join(unknown)}')
    c = {**DEFAULTS, **{k: v for k, v in raw.items() if not k.startswith('_')}}
    for key in ('files', 'exclude'):
        if not isinstance(c[key], list) or not all(isinstance(g, str) and g for g in c[key]):
            raise ConfigError(f'{where}: {key} must be a list of globs')
    if not isinstance(c['warn_at'], (int, float)) or isinstance(c['warn_at'], bool) or not 0 < c['warn_at'] <= 1:
        raise ConfigError(f'{where}: warn_at must be a number above 0, at most 1')
    if not isinstance(c['links'], bool):
        raise ConfigError(f'{where}: links must be true or false')
    if not isinstance(c['budgets'], list):
        raise ConfigError(f'{where}: budgets must be a list')
    budgets = []
    for n, b in enumerate(c['budgets']):
        ok = (isinstance(b, dict) and set(b) <= {'glob', 'lines', 'bytes', 'why'} and isinstance(b.get('glob'), str)
              and all(isinstance(b.get(k), int) and not isinstance(b.get(k), bool) and b[k] > 0
                      for k in ('lines', 'bytes')))
        if not ok:
            raise ConfigError(f'{where}: budgets[{n}] needs glob, lines > 0 and bytes > 0 (and an optional why)')
        budgets.append((glob_re(b['glob']), b['glob'], b['lines'], b['bytes']))
    if not isinstance(c['history'], list):
        raise ConfigError(f'{where}: history must be a list')
    history = []
    for n, h in enumerate(c['history']):
        if not isinstance(h, dict) or set(h) - {'pattern', 'flags', 'why'} or not isinstance(h.get('why'), str):
            raise ConfigError(f'{where}: history[{n}] needs pattern and why (and optional flags)')
        history.append((_regex(h.get('pattern'), h.get('flags', ''), f'{where}: history[{n}]'), h['why']))
    report = None
    if c['report_name'] is not None:
        report = _regex(c['report_name'], c['report_name_flags'], f'{where}: report_name')
    return {'files': [glob_re(g) for g in c['files']], 'exclude': [glob_re(g) for g in c['exclude']],
            'budgets': budgets, 'warn_at': float(c['warn_at']), 'history': history, 'report': report,
            'links': c['links']}


def load_config(path, ref=None):
    """(rules, note). ref: read the file at that commit (absent there: defaults); None: from the checkout."""
    if ref is None:
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except FileNotFoundError:
            text = None
        except (OSError, UnicodeDecodeError) as e:
            raise ConfigError(f'{path}: cannot read it ({e.__class__.__name__})')
        where = path
    else:
        if git('cat-file', '-e', f'{ref}^{{commit}}') is None:
            raise ConfigError(f'base {ref}: not a commit here (fetch it, or check out with fetch-depth: 0)')
        text = git('show', f'{ref}:{path}') if git('cat-file', '-e', f'{ref}:{path}') is not None else None
        where = f'{path} at the base'
    if text is None:
        return parse_config(None, where), f'no {where}: defaults'
    if not text.strip():
        return parse_config(None, where), f'{where} is empty: defaults'
    return parse_config(text, where), f'rules from {where}'


def budget(rules, path):
    """(glob, lines, bytes) of the first budget whose glob matches the path, or None (no size or history rule)."""
    for rx, g, lines, size in rules['budgets']:
        if rx.match(path):
            return g, lines, size
    return None


def lf(text):
    """The text with every line ending one '\\n', as Python's text mode reads it."""
    return text.replace('\r\n', '\n').replace('\r', '\n')


def usage(b, text):
    """(lines, bytes, share of the budget) of a file's exact text: bytes as on disk, CRLF and undecodable bytes too."""
    size, text = len(text.encode('utf-8', 'surrogateescape')), lf(text)
    n = text.count('\n') + (0 if text.endswith('\n') or not text else 1)
    return n, size, max(n / b[1], size / b[2])


def link_lines(text):
    """(line number, target) of each link outside code fences. As in CommonMark, a fence opens with 0-3 spaces of
    indent (a tab is 4) counted from the content column of the list item it is in; a line indented 4 or more is no
    fence, and a fence in a list item ends with the item."""
    fence, fence_col, items = None, 0, []   # items: the content columns of the open list items
    for no, line in enumerate(text.split('\n'), 1):
        wide = line.expandtabs(4)
        body = wide.lstrip(' ')
        indent = len(wide) - len(body)
        if body and fence is not None and indent < fence_col:
            fence = None                        # the list item the fence was in has ended
        if body:
            while items and indent < items[-1]:
                items.pop()
        col = fence_col if fence is not None else (items[-1] if items else 0)
        m = FENCE.match(body) if body and indent - col <= 3 else None
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not body[len(m.group(1)):].strip():
                fence = None
            continue
        if m and not (m.group(1)[0] == '`' and '`' in body[len(m.group(1)):]):
            fence, fence_col = m.group(1), col
            continue
        item = LIST_ITEM.match(body) if body and indent - col <= 3 else None
        if item:
            rest = body[item.end():]
            gap = len(rest) - len(rest.lstrip(' '))
            items.append(indent + item.end() + (gap if rest.strip() and 1 <= gap <= 4 else 1))
        if fence is None:
            for lm in LINK.finditer(INLINE_CODE.sub('', line)):
                target = re.split(r'[#?]', lm.group(1) or lm.group(2), maxsplit=1)[0]
                if target:
                    yield no, target


def link_path(path, target, root):
    """The path a relative link target names (under root), or None when it is no file link."""
    if SCHEME.match(target) or target.startswith('//'):
        return None
    target = unquote(target)
    return os.path.normpath(os.path.join(root, target.lstrip('/')) if target.startswith('/')
                            else os.path.join(root, os.path.dirname(path), target))


def broken_link(path, target, root):
    """Why a relative link target is broken, or None when it resolves (or is no file link)."""
    full = link_path(path, target, root)
    if full is None:
        return None
    real_root = os.path.realpath(root)
    try:
        real = os.path.realpath(full)
    except (ValueError, OSError) as e:   # a NUL byte (%00), for example
        return f'not a path ({e.__class__.__name__})'
    if os.path.commonpath([real_root, real]) != real_root:
        return 'points outside the repository'
    return None if os.path.exists(full) else 'no such file'


def check(rules, path, text, added, root='.', base_text=None, links_from=None):
    """(problems, warnings) for one file's exact text. base_text: the file at the merge base ('' new, None unknown).
    links_from: the path its links are relative to (a symlink's target), else the path."""
    probs, warns = [], []
    b = budget(rules, path)
    exact, text = text, lf(text)
    if b is not None:
        n, size, share = usage(b, exact)
        if share > 1:
            probs.append((path, None, f'{n} lines / {size} B, budget {b[1]} lines / {b[2]} B ({b[0]}): move '
                                      'sometimes-needed text into its own file (never raise the budget)'))
        elif share > rules['warn_at']:
            msg = (f'{n} lines / {size} B = {share:.0%} of its budget ({b[1]} lines / {b[2]} B): move sometimes-needed '
                   'text into its own file before it fails')
            if base_text is not None and (not base_text or usage(b, base_text)[2] <= rules['warn_at']):
                msg += f'; this change moved it past {rules["warn_at"]:.0%}'
            warns.append((path, None, msg))
        for rx, why in rules['history']:
            m = rx.search(text)
            if m:
                probs.append((path, text.count('\n', 0, m.start()) + 1, why))
    if added and rules['report'] is not None and rules['report'].search(path):
        probs.append((path, None, 'dated report, handoff or review file: put it in the pull request or an issue'))
    if rules['links']:
        for no, target in link_lines(text):
            why = broken_link(links_from or path, target, root)
            if why:
                probs.append((path, no, f'broken link {target}: {why}'))
    return probs, warns


def selected(rules, path):
    return any(rx.match(path) for rx in rules['files']) and not any(rx.match(path) for rx in rules['exclude'])


def changed_files(base):
    """(merge base, [(path, added)], [removed]): the files the change adds or modifies (a rename's new name counts as
    added, and a change of type, such as a file turned into a symlink, as modified), and those it deletes (a rename's
    old name)."""
    mb = git('merge-base', base, 'HEAD')
    if mb is None or not mb.strip():
        raise ConfigError(f'no merge base of {base} and HEAD (check out with fetch-depth: 0)')
    mb = mb.strip()
    out = git('diff', '--name-status', '--no-renames', '--diff-filter=AMTD', '-z', mb, 'HEAD')
    if out is None:
        raise ConfigError(f'git diff {mb}..HEAD failed')
    parts = out.split('\0')
    pairs = [(parts[i], parts[i + 1]) for i in range(0, len(parts) - 1, 2)]
    return (mb, [(path, kind == 'A') for kind, path in pairs if kind != 'D'],
            [path for kind, path in pairs if kind == 'D'])


def read_doc(path, root='.'):
    """(exact text, links_from, problem) of a file to check. Undecodable bytes are kept (surrogateescape), so its
    size is still checked; a symlink is read as the file it points to, which must be in the repository."""
    links_from = None
    if os.path.islink(path):
        real, real_root = os.path.realpath(path), os.path.realpath(root)
        if os.path.commonpath([real_root, real]) != real_root:
            return None, None, 'a symlink that points outside the repository: make it a file'
        if '.git' in os.path.relpath(real, real_root).split(os.sep):
            return None, None, 'a symlink that points into .git: make it a file'
        if not os.path.isfile(real):
            return None, None, 'a symlink that points to no file: make it a file'
        links_from = os.path.relpath(real, real_root)
    try:
        with open(path, 'rb') as f:
            raw = f.read()
    except OSError as e:
        return None, None, f'cannot read it ({e.__class__.__name__})'
    try:
        return raw.decode('utf-8'), links_from, None
    except UnicodeDecodeError as e:
        return raw.decode('utf-8', 'surrogateescape'), links_from, f'not UTF-8 text (byte {e.start}): save it as UTF-8'


def base_doc(mb, path):
    """The text a path read at the merge base, through symlinks in the repository: '' when it was no file there (or
    pointed to none), None when git cannot tell."""
    for _ in range(40):
        out = git('ls-tree', '-z', mb, '--', path)
        if out is None:
            return None
        if not out:
            return ''
        mode, kind, rest = out.split(' ', 2)
        if kind != 'blob':
            return ''
        blob = git('cat-file', 'blob', rest.split('\t', 1)[0])
        if blob is None or mode != '120000':
            return blob
        path = os.path.normpath(os.path.join(os.path.dirname(path), blob))
        if os.path.isabs(path) or path.split(os.sep)[0] == '..':
            return ''
    return None


def removed_target(path, gone, root='.'):
    """The path a symlink names (hop by hop, inside the repository) that the change removed, or None."""
    for _ in range(40):
        target = os.path.normpath(os.path.join(os.path.dirname(path), os.readlink(path)))
        rel = os.path.relpath(os.path.join(root, target), root)
        if rel in gone or any(g.startswith(rel + os.sep) for g in gone):
            return rel
        if rel.split(os.sep)[0] == '..' or not os.path.islink(os.path.join(root, rel)):
            return None
        path = rel
    return None


def links_to_removed(rules, removed, checked, root='.'):
    """Problems, in selected tracked files this run did not check, for a symlink to a file the change removed, and
    (links on) for links to a file the change removed (or into a folder it emptied) that no longer resolve."""
    gone = {os.path.normpath(p) for p in removed}
    if not gone:
        return []
    out = git('ls-files', '-z')
    if out is None:
        raise ConfigError('git ls-files failed (run from the repository root)')
    probs = []
    for path in out.split('\0'):
        if not path or path in checked or not selected(rules, path):
            continue
        if not (os.path.isfile(path) or os.path.islink(path)):
            continue
        text, links_from, problem = read_doc(path, root)
        if text is None:
            hit = os.path.islink(path) and removed_target(path, gone, root)
            if hit:
                probs.append((path, None, f'{problem}: this change removed {hit}'))
            continue
        if not rules['links']:
            continue
        for no, target in link_lines(lf(text)):
            full = link_path(links_from or path, target, root)
            if full is None or broken_link(links_from or path, target, root) is None:
                continue
            rel = os.path.relpath(full, root)
            if rel in gone or any(g.startswith(rel + os.sep) for g in gone):
                probs.append((path, no, f'broken link {target}: this change removed {rel}'))
    return probs


def annotate(kind, path, line, msg):
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        where = f'file={shown(path)}' + (f',line={line}' if line else '')
        print(f'::{kind} {where}::{shown(msg)}'.replace('%', '%25'))


def main(argv=None):
    ap = argparse.ArgumentParser(description='Doc rules for agent instruction files.')
    ap.add_argument('--config', default=DEFAULT_CONFIG)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument('--base', help='check the files changed since the merge base with this commit')
    mode.add_argument('--all', action='store_true', help='check every tracked file the config selects')
    mode.add_argument('--files', nargs='+', help='check these files')
    a = ap.parse_args(argv)
    try:
        if a.base is not None:
            if not a.base.strip():
                raise ConfigError('--base is empty: give the base commit, or use --all')
            rules, note = load_config(a.config, a.base)
            mb, files, removed = changed_files(a.base)
        else:
            rules, note = load_config(a.config)
            mb, removed = None, []
            if a.all:
                out = git('ls-files', '-z')
                if out is None:
                    raise ConfigError('git ls-files failed (run from the repository root)')
                files = [(f, False) for f in out.split('\0') if f]
            else:
                files = [(os.path.normpath(f), False) for f in a.files]
    except ConfigError as e:
        print(f'doc_check: ERROR {shown(e)}')
        annotate('error', a.config, None, f'doc-check: {e}')
        return 2
    print(f'doc_check: {shown(note)}')
    probs, warns, count, checked, found = [], [], 0, set(), {}
    for path, added in files:
        # A symlink to no file is checked (and fails); another path that is not a file is skipped.
        if not selected(rules, path) or not (os.path.isfile(path) or os.path.islink(path)):
            continue
        count += 1
        checked.add(path)
        text, links_from, problem = read_doc(path)
        first = [(path, None, problem)] if problem else []
        if text is None:
            probs += first
            continue
        base_text = None
        if mb is not None and budget(rules, path) is not None:
            base_text = base_doc(mb, path)
        p, w = check(rules, path, text, added, '.', base_text, links_from)
        found[path] = (links_from, first + p, w)
    for path, (links_from, p, w) in found.items():
        # A symlink whose target this run also checked: what the target already reports (same budget, history,
        # links) is said once, at the target.
        if links_from is not None and links_from in found and links_from != path:
            said = {(no, msg) for _, no, msg in found[links_from][1] + found[links_from][2]}
            p = [x for x in p if x[1:] not in said]
            w = [x for x in w if x[1:] not in said]
        probs += p
        warns += w
    try:
        probs += links_to_removed(rules, removed, checked)
    except ConfigError as e:
        print(f'doc_check: ERROR {shown(e)}')
        return 2
    for path, line, msg in probs:
        print(f'DOC  {shown(path)}{":" + str(line) if line else ""}: {shown(msg)}')
        annotate('error', path, line, msg)
    for path, line, msg in warns:
        print(f'WARN {shown(path)}{":" + str(line) if line else ""}: {shown(msg)}')
        annotate('warning', path, line, msg)
    print(f'doc_check: {count} file(s), {len(probs)} problem(s), {len(warns)} warning(s)')
    return 1 if probs else 0


if __name__ == '__main__':
    sys.exit(main())
