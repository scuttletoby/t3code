#!/usr/bin/env python3
"""doc-check: rules for the Markdown files agents read, on the files a change adds or modifies (or on every file).

  * size budgets (lines and bytes) for instruction files, matched by glob, first match wins; above `warn_at` of a
    budget a WARN line that never fails;
  * no history in instruction files (changelog blocks, "is live on" claims, dated "Updated" banners);
  * every relative link resolves to a file or folder in the repository (code blocks, fenced or indented, read as
    CommonMark does, and inline code are skipped; `/x` is from the repository root, as GitHub reads it; a link that
    leaves the repository is broken); with --base, no file the config selects may be left with a link that resolved on
    the base and does not now because the change removed a path or changed a symlink on its way;
  * a newly added file whose name looks like a dated report, handoff or review belongs in the pull request or an
    issue, not in the repository;
  * a checked file is UTF-8 text that can be read (anything else fails, and its size is still checked); a symlink is
    checked as the file it points to, which must be in the repository and not in .git (with --base, an untouched
    symlink is checked too when the change touches the file it reads or a symlink on the way, and fails when the
    change removes or retargets a path it went through; one that already pointed to no file on the base, a loop
    too, is not this change's). Sizes are the bytes on disk (CRLF included).

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
import argparse, json, os, re, stat, subprocess, sys
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
THEMATIC = re.compile(r'([-*_])(?:[ \t]*\1){2,}[ \t]*$')   # "- - -" is a thematic break, not a list item
HEADING = re.compile(r'#{1,6}(?=[ \t]|$)')
INLINE_CODE = re.compile(r'(?<!`)(`+)(?!`).*?(?<!`)\1(?!`)')   # whole runs only: an unmatched run is text
SCHEME = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:')
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1',
           'GIT_LITERAL_PATHSPECS': '1'}
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


def opens_fence(body):
    """The fence a line's content opens (a backtick fence has no backtick after it), or None."""
    m = FENCE.match(body)
    return m.group(1) if m and not (m.group(1)[0] == '`' and '`' in body[len(m.group(1)):]) else None


def starts_block(body):
    """Whether a line's content (0-3 spaces in) starts a block that ends a paragraph, so it is no lazy line."""
    return bool(LIST_ITEM.match(body) or THEMATIC.match(body) or HEADING.match(body) or opens_fence(body)
                or body.startswith('>'))


def link_lines(text):
    """(line number, target) of each link outside code blocks. As in CommonMark, a fence opens with 0-3 spaces of
    indent (a tab is 4) counted from the content column of the list item it is in (on the item's own line too), and
    a fence in a list item ends with the item; a line 4 or more past that column is indented code unless it goes on
    with a paragraph. A line less indented than an item's content ends the item, unless it continues the
    item's paragraph (a lazy line); each marker of a line like "- - item" opens an item."""
    fence, fence_col, items = None, 0, []   # items: the content columns of the open list items
    code, para = False, False               # in indented code; the line before is paragraph text (lazy lines go on)
    quote = False                           # that paragraph is in a block quote, which this checker does not model
    for no, line in enumerate(text.split('\n'), 1):
        wide = line.expandtabs(4)
        body = wide.lstrip(' ')
        indent = len(wide) - len(body)
        if body and fence is not None and indent < fence_col:
            fence = None                        # the list item the fence was in has ended
        if body:
            kept = len(items)
            while kept and indent < items[kept - 1]:
                kept -= 1
            # the line is in the paragraph's own block; a line without ">" after a quote's paragraph goes on with it
            # only lazily, so it opens what it would open outside the quote ("2." too)
            in_para = para and kept == len(items) and not quote
            # a lazy line goes on with the paragraph of an item it is not indented into: text, nothing opens
            lazy = para and kept < len(items) and not (indent - (items[kept - 1] if kept else 0) <= 3
                                                       and starts_block(body))
            if not lazy:
                del items[kept:]
        col = fence_col if fence is not None else (items[-1] if items else 0)
        m = FENCE.match(body) if body and indent - col <= 3 else None
        if fence is not None:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not body[len(m.group(1)):].strip():
                fence = None
            continue
        if body:
            code = indent - col >= 4 and (code or not para)   # indented code cannot interrupt a paragraph
        if code or not body:
            para = quote = False
            continue
        text_only = lazy or indent - col > 3     # 4 or more in and not code: the paragraph goes on too
        if not text_only and opens_fence(body):
            fence, fence_col, para = opens_fence(body), col, False
            continue
        at, rest = indent, '' if text_only else body
        while rest and not THEMATIC.match(rest) and LIST_ITEM.match(rest):
            item = LIST_ITEM.match(rest)
            after = rest[item.end():]
            if in_para and (not after.strip() or item.group(1)[0].isdigit() and int(item.group(1)[:-1]) != 1):
                break   # an empty item, or a numbered one not at 1, cannot interrupt a paragraph: text
            in_para = False
            gap = len(after) - len(after.lstrip(' '))
            items.append(at + item.end() + (gap if after.strip() and gap <= 4 else 1))
            at, rest = items[-1], after.lstrip(' ') if gap <= 4 else ''
        if rest and opens_fence(rest):
            fence, fence_col = opens_fence(rest), items[-1]
        para = text_only or bool(rest) and fence is None and not (THEMATIC.match(rest) or HEADING.match(rest))
        quote = rest.startswith('>') if rest else quote and text_only
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
    """(merge base, [(path, added)], [removed], {relinked}): the files the change adds or modifies (a rename's new name
    counts as added, and a change of type, such as a file turned into a symlink, as modified), those it deletes (a
    rename's old name), and the symlinks it modifies (retargeted, or a file turned into a symlink or back)."""
    mb = git('merge-base', base, 'HEAD')
    if mb is None or not mb.strip():
        raise ConfigError(f'no merge base of {base} and HEAD (check out with fetch-depth: 0)')
    mb = mb.strip()
    out = git('diff', '--raw', '--no-renames', '--diff-filter=AMTD', '-z', mb, 'HEAD')
    if out is None:
        raise ConfigError(f'git diff {mb}..HEAD failed')
    parts = out.split('\0')
    rows = [(parts[i].split(), parts[i + 1]) for i in range(0, len(parts) - 1, 2)]   # :old new sha sha kind, path
    return (mb, [(path, f[4] == 'A') for f, path in rows if f[4] != 'D'],
            [path for f, path in rows if f[4] == 'D'],
            {path for f, path in rows if f[4] in ('M', 'T') and '120000' in (f[0][1:], f[1])})


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


def resolve(path, entry):
    """Walk a repository path name by name as the file system does, following symlinks in the repository (a folder
    symlink on the way too): (kind, [each path walked]). entry(path) is (kind, symlink text) with kind 'file', 'dir',
    'link', None (no such path) or '?' (cannot tell). kind None: no such path, out of the repository or a loop."""
    if '\0' in path:
        return None, []
    parts, done, walked, hops, kind = path.split('/'), [], [], 0, 'dir'
    while parts:
        name = parts.pop(0)
        if name in ('', '.'):
            continue
        if kind == 'file':                      # a file is no folder
            return None, walked
        if name == '..':
            if not done:
                return None, walked
            done.pop()
            continue
        cur = '/'.join(done + [name])
        walked.append(cur)
        kind, target = entry(cur)
        if kind == 'link':
            hops += 1
            if hops > 40 or target.startswith('/'):
                return None, walked
            parts = target.split('/') + parts
            kind = 'dir'
            continue
        if kind in (None, '?'):
            return kind, walked
        done.append(name)
    return kind, walked


def head_entry(root='.'):
    """entry() for resolve() on the checkout."""
    def entry(path):
        try:
            st = os.lstat(os.path.join(root, path))
        except (OSError, ValueError):
            return None, None
        if stat.S_ISLNK(st.st_mode):
            return 'link', os.readlink(os.path.join(root, path))
        return ('dir' if stat.S_ISDIR(st.st_mode) else 'file'), None
    return entry


def base_entry(mb):
    """entry() for resolve() on the merge base's tree; git failing is an error, never a pass."""
    seen = {}
    def entry(path):
        if path not in seen:
            out = git('ls-tree', '-z', mb, '--', path)
            if out is None:
                raise ConfigError(f'git ls-tree {mb} failed')
            if not out:
                seen[path] = (None, None)
            else:
                mode, kind, rest = out.split(' ', 2)
                if mode == '120000':
                    target = git('cat-file', 'blob', rest.split('\t', 1)[0])
                    if target is None:
                        raise ConfigError(f'git cat-file failed on {path} at {mb}')
                    seen[path] = ('link', target)
                else:   # a submodule ('commit') cannot be told
                    seen[path] = {'blob': ('file', None), 'tree': ('dir', None)}.get(kind, ('?', None))
        return seen[path]
    return entry


def broken_by(walked, gone, relinked):
    """What this change did to the paths a name walked through on the base, or None: it removed the last one (or
    emptied that folder), or removed or changed a symlink on the way."""
    if walked and (walked[-1] in gone or any(g.startswith(walked[-1] + '/') for g in gone)):
        return f'this change removed {walked[-1]}'
    for p in reversed(walked):
        if p in gone:
            return f'this change removed {p}'
        if p in relinked:
            return f'this change changed the symlink {p}'
    return None


def links_to_removed(rules, tracked, removed, relinked, checked, mb, root='.'):
    """Problems, in selected tracked files this run did not check, for a symlink and (links on) a link that points to
    no file now but resolved on the base (through the same path, folder symlinks included), when the change removed a
    path or changed a symlink on the way. What was already broken on the base is not this change's."""
    gone = {os.path.normpath(p) for p in removed}
    if not gone and not relinked:
        return []
    base = base_entry(mb)
    probs = []
    for path in tracked:
        if not path or path in checked or not selected(rules, path):
            continue
        if not (os.path.isfile(path) or os.path.islink(path)):
            continue
        text, links_from, problem = read_doc(path, root)
        if text is None:
            if os.path.islink(path):
                kind, walked = resolve(path, base)
                why = kind == 'file' and broken_by(walked, gone, relinked)
                if why:
                    probs.append((path, None, f'{problem}: {why}'))
            continue
        if not rules['links']:
            continue
        for no, target in link_lines(lf(text)):
            full = link_path(links_from or path, target, root)
            if full is None or broken_link(links_from or path, target, root) is None:
                continue
            kind, walked = resolve(os.path.relpath(full, root), base)
            why = kind in ('file', 'dir') and broken_by(walked, gone, relinked)
            if why:
                probs.append((path, no, f'broken link {target}: {why}'))
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
            mb, files, removed, relinked = changed_files(a.base)
            out = git('ls-files', '-z')
            if out is None:
                raise ConfigError('git ls-files failed (run from the repository root)')
            tracked = [f for f in out.split('\0') if f]
            # An untouched symlink that reads a file this change touched (its target, or a symlink on the way) is
            # checked too, as --all checks it; one that points to no file now is the removal scan's.
            changed = {p for p, _ in files} | set(removed)
            head = head_entry()
            files += [(p, False) for p in tracked if p not in changed and os.path.islink(p) and selected(rules, p)
                      and changed & set(resolve(p, head)[1]) and read_doc(p)[0] is not None]
        else:
            rules, note = load_config(a.config)
            mb, removed, relinked, tracked = None, [], set(), []
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
        if mb is not None:
            probs += links_to_removed(rules, tracked, removed, relinked, checked, mb)
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
