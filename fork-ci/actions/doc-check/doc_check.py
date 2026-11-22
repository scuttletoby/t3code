#!/usr/bin/env python3
"""doc-check: rules for the Markdown files agents read, on the files a change adds or modifies (or on every file).

  * size budgets (lines and bytes) for instruction files, matched by glob, first match wins; above `warn_at` of a
    budget a WARN line that never fails;
  * no history in instruction files (changelog blocks, "is live on" claims, dated "Updated" banners);
  * every relative link resolves to a file or folder in the repository (code fences are skipped; `/x` is from the
    repository root, as GitHub reads it; a link that leaves the repository is broken);
  * a newly added file whose name looks like a dated report, handoff or review belongs in the pull request or an
    issue, not in the repository.

The rules come from the project's JSON config (README "Config"); every key is optional and a missing, empty or `{}`
config means the defaults. With --base the config is read at the base commit, never from the checkout, so a change
cannot loosen the rules that judge it (a change to the config applies from the next change); with --all (or --files) it is read from the checkout. A config that is not
valid JSON, has an unknown key or a value of the wrong type is an error (exit 2), never a pass.

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
FENCE = re.compile(r'^ {0,3}(`{3,}|~{3,})')
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


def usage(b, text):
    n, size = text.count('\n') + (0 if text.endswith('\n') or not text else 1), len(text.encode('utf-8'))
    return n, size, max(n / b[1], size / b[2])


def link_lines(text):
    """(line number, target) of each link outside code fences."""
    fence = None
    for no, line in enumerate(text.split('\n'), 1):
        m = FENCE.match(line)
        if m:
            mark = m.group(1)
            if fence is None:
                fence = mark
            elif mark[0] == fence[0] and len(mark) >= len(fence) and not line.strip()[len(mark):].strip():
                fence = None
            continue
        if fence is None:
            for lm in LINK.finditer(line):
                target = (lm.group(1) or lm.group(2)).split('#', 1)[0]
                if target:
                    yield no, target


def broken_link(path, target, root):
    """Why a relative link target is broken, or None when it resolves (or is no file link)."""
    if SCHEME.match(target) or target.startswith('//'):
        return None
    target = unquote(target)
    full = os.path.normpath(os.path.join(root, target.lstrip('/')) if target.startswith('/')
                            else os.path.join(root, os.path.dirname(path), target))
    real_root = os.path.realpath(root)
    if os.path.commonpath([real_root, os.path.realpath(full)]) != real_root:
        return 'points outside the repository'
    return None if os.path.exists(full) else 'no such file'


def check(rules, path, text, added, root='.', base_text=None):
    """(problems, warnings) for one file's text. base_text: the file at the merge base ('' new, None unknown)."""
    probs, warns = [], []
    b = budget(rules, path)
    if b is not None:
        n, size, share = usage(b, text)
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
            why = broken_link(path, target, root)
            if why:
                probs.append((path, no, f'broken link {target}: {why}'))
    return probs, warns


def selected(rules, path):
    return any(rx.match(path) for rx in rules['files']) and not any(rx.match(path) for rx in rules['exclude'])


def changed_files(base):
    """(merge base, [(path, added)]) of the files the change adds or modifies; renames count as added."""
    mb = git('merge-base', base, 'HEAD')
    if mb is None or not mb.strip():
        raise ConfigError(f'no merge base of {base} and HEAD (check out with fetch-depth: 0)')
    mb = mb.strip()
    out = git('diff', '--name-status', '--no-renames', '--diff-filter=AM', '-z', mb, 'HEAD')
    if out is None:
        raise ConfigError(f'git diff {mb}..HEAD failed')
    parts = out.split('\0')
    return mb, [(parts[i + 1], parts[i] == 'A') for i in range(0, len(parts) - 1, 2)]


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
            mb, files = changed_files(a.base)
        else:
            rules, note = load_config(a.config)
            mb = None
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
    probs, warns, count = [], [], 0
    for path, added in files:
        if not selected(rules, path) or not os.path.isfile(path):
            continue
        count += 1
        if os.path.islink(path):
            warns.append((path, None, 'a symlink: not checked'))
            continue
        try:
            with open(path, encoding='utf-8') as f:
                text = f.read()
        except (OSError, UnicodeDecodeError) as e:
            warns.append((path, None, f'cannot read it ({e.__class__.__name__}): not checked'))
            continue
        base_text = None
        if mb is not None and budget(rules, path) is not None:
            old = git('show', f'{mb}:{path}')
            base_text = old if old is not None else ''
        p, w = check(rules, path, text, added, '.', base_text)
        probs += p
        warns += w
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
