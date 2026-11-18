#!/usr/bin/env python3
"""weakened-tests: which existing tests does this pull request delete, skip or weaken? A report, never a failure.

The change is the pull request's own: the merge base of --base and --head .. --head (every push together, computed
again on each run, so a force push is compared with the base afresh), with git's rename detection, so a renamed test
file is compared with its old self instead of being reported as deleted. A test file is a file that matches a
language's `files` globs; its tests are the matches of the language's `test` patterns (each names the test with a
`name` group). A test's lines run from its first line (with the decorator or annotation lines just above it, the
`before` patterns) to the next test's.

Listed, for each existing test (a test of the base side; new tests are never listed):
  - deleted: gone from the file, and no test of that name was added in another file the change touches (moved);
  - skipped: a `skip` pattern matches more often in the test's lines than before (@Disabled, pytest.mark.skip, it.skip);
  - focused: a `focus` pattern matches more often (it.only): the file's other tests stop running;
  - assertion: a line matching an `assert` pattern is gone or changed (whitespace aside);
  - and for the file itself (name "(file)"): a skip or focus added outside any test, or a side that is not UTF-8.

The patterns are config: Java, Python and Node defaults below, and a project's config (read from the BASE commit, so
a pull request cannot change what judges it) adds patterns to a language or adds a language. Unknown keys and wrong
types make the report "could not read", never a quiet empty list; so does a commit that is not in the checkout, no
merge base, a git error, or test files over `max_bytes`.

The report is the job summary and one pull request comment (found by a marker and its author, edited in place: a
re-run with the same result edits nothing). A run with nothing to list posts no comment until there was one. The PR
body is read for a "why" per listed test: a body line that names the test (or, for "(file)", the file) counts.
Every error, including one this script did not foresee, is a ::warning:: and exit 0.

  python3 -I weakened_tests.py --config .github/weakened-tests.json --repo OWNER/NAME --pr N --base SHA --head SHA
      [--path DIR] [--comment-author LOGIN] [--no-comment] [--refused]
Writes status=ok|could-not-read|refused, count=<items> and missing_why=<items without a why> to $GITHUB_OUTPUT (count
and missing_why are empty when the change could not be read; missing_why is empty when the PR body could not be read).
--refused: git-clean refused the checkout, so no git runs; status is refused, and a report comment of an earlier head
is replaced by a note (none is posted when there is none).
"""
import argparse, collections, copy, json, os, re, subprocess, sys

MARKER = '<!-- fork-ci weakened-tests -->'
FILE = '(file)'
DEFAULT_CONFIG = '.github/weakened-tests.json'
DEFAULT_MAX_BYTES = 10_000_000
MAX_ROWS = 150          # rows in the comment and the summary; a comment holds at most 65536 characters
MAX_TEXT = 60_000
CELL = 120
KEYS = {'languages', 'max_bytes'}
LANG_KEYS = ('files', 'test', 'before', 'skip', 'focus', 'assert')
KIND_TEXT = {'deleted': 'deleted', 'skipped': 'skipped or disabled', 'focused': 'focused (the other tests in the file '
             'stop running)', 'assertion': 'assertion removed or changed', 'unreadable': 'could not read this file'}
PLAIN_MODES = ('100644', '100755')
NODE_EXT = ('js', 'jsx', 'ts', 'tsx', 'mjs', 'cjs', 'mts', 'cts')
# The default test patterns must not backtrack on a PR's own file (a stalled step writes no report): each repeated
# part can match a character one way only, and the annotations between @Test and the method are at most 20.
# A Java annotation's arguments: strings (a parenthesis inside one is text) and one level of nested parentheses.
JAVA_STR = r'"(?:[^"\\\n]|\\.)*"'
JAVA_ARGS = rf'\((?:{JAVA_STR}|[^()"]|\((?:{JAVA_STR}|[^()"])*\))*\)'
DEFAULT_LANGUAGES = {
    'java': {
        'files': ['**/src/test/**/*.java', '**/*Test.java', '**/*Tests.java', '**/*IT.java'],
        'test': [rf'@(?:Test|ParameterizedTest|RepeatedTest)\b(?:{JAVA_ARGS})?(?:\s+@\w+(?:{JAVA_ARGS})?){{0,20}}\s+'
                 r'(?:(?:public|protected|private|static|final)\s+)*void\s+(?P<name>\w+)\s*\('],
        'before': [r'^\s*@'],
        'skip': [r'@Disabled\w*\b', r'@Enabled\w+\b', r'@Ignore\b', r'\bassume[A-Z]\w*\s*\('],
        'focus': [],
        'assert': [r'\b(?:assert\w*|verify\w*|fail)\s*\(', r'^\s*assert\s'],
    },
    'python': {
        'files': ['**/test_*.py', '**/*_test.py', '**/*.test.py', '**/tests/**/*.py', '**/test/**/*.py'],
        'test': [r'^[ \t]*(?:async[ \t]+)?def[ \t]+(?P<name>test\w*)\s*\('],
        'before': [r'^\s*@'],
        'skip': [r'[@.]skip\w*\b', r'\bskip(?:If|Unless)\b', r'\bxfail\b', r'\bexpectedFailure\b'],
        'focus': [],
        'assert': [r'\bassert\w*\b', r'\bpytest\.raises\b', r'\bself\.fail\s*\('],
    },
    'node': {
        'files': [f'**/{p}.{e}' for e in NODE_EXT for p in ('*.test', '*.spec', '__tests__/**/*')],
        'test': [r'(?<![\w.$])[xf]?(?:it|test)(?:\.\w+)*(?:\([^()]*\))?\s*\(\s*(?P<q>[\'"`])'
                 r'(?P<name>(?:\\.|(?!(?P=q))[^\\\n])*)(?P=q)'],
        'before': [],
        'skip': [r'\.(?:skip\w*|todo)\b', r'(?<![\w.$])x(?:it|test|describe)\b'],
        'focus': [r'\.only\b', r'(?<![\w.$])f(?:it|test|describe)\b'],
        'assert': [r'\b(?:expect|assert)\w*\b'],
    },
}

Item = collections.namedtuple('Item', 'path name kind detail')
Found = collections.namedtuple('Found', 'items merge_base')
Test = collections.namedtuple('Test', 'name lines')


class Unclear(Exception):
    """The change or the config could not be read: the report says so instead of listing nothing."""


# No global or system config or attributes, no hooks, fsmonitor command, commit-graph or replace refs from the
# repository: the same two as ../git-clean/git_clean.py's (git_clean.test.py pins them to its; this file stays self-contained).
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'}
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.commitGraph=false',
            '-c', 'core.attributesFile=/dev/null', '--no-replace-objects')


class Git:
    """git in one folder: stdout as bytes, or None when git fails."""

    def __init__(self, cwd='.'):
        self.cwd = cwd

    def __call__(self, *args, input=None):
        r = subprocess.run(['git', *GIT_SAFE, *args], cwd=self.cwd, input=input, capture_output=True,
                           env={**os.environ, **GIT_ENV})
        return r.stdout if r.returncode == 0 else None


def glob_re(pattern):
    """A glob from the repository root: `**/` is any number of folders (none too), `**` anything, `*` and `?` stay
    inside one name (docs-scope's rule)."""
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


def load_config(text):
    """{language: {key: [compiled]}} and max_bytes: the defaults, plus what the project's config (JSON text, or None
    for no config) adds. Unclear for an unknown key, a wrong type, a bad pattern or a test pattern without `name`."""
    langs, max_bytes = copy.deepcopy(DEFAULT_LANGUAGES), DEFAULT_MAX_BYTES
    if text is not None:
        try:
            c = json.loads(text)
        except (ValueError, RecursionError) as e:
            raise Unclear(f'the config is not JSON: {e}')
        if not isinstance(c, dict) or set(c) - KEYS:
            raise Unclear(f'the config must be an object with no other keys than {sorted(KEYS)}')
        max_bytes = c.get('max_bytes', max_bytes)
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise Unclear('the config: max_bytes must be a positive whole number')
        given = c.get('languages', {})
        if not isinstance(given, dict):
            raise Unclear('the config: languages must be an object')
        for name, lang in given.items():
            if not isinstance(lang, dict) or set(lang) - set(LANG_KEYS) or not all(
                    isinstance(v, list) and all(isinstance(x, str) and x for x in v) for v in lang.values()):
                raise Unclear(f'the config: languages.{name} must be an object of lists of non-empty strings, keys '
                              f'among {list(LANG_KEYS)}')
            if name not in langs:
                if not lang.get('files') or not lang.get('test'):
                    raise Unclear(f'the config: the new language {name} needs "files" and "test"')
                langs[name] = {k: [] for k in LANG_KEYS}
            for k, v in lang.items():
                langs[name][k] += v
    compiled = {}
    for name, lang in langs.items():
        try:
            compiled[name] = {'files': [glob_re(g) for g in lang['files']],
                              **{k: [re.compile(p, re.M) for p in lang[k]] for k in LANG_KEYS if k != 'files'}}
        except re.error as e:
            raise Unclear(f'the config: a pattern of {name} is not a regular expression: {e}')
        if not all('name' in p.groupindex for p in compiled[name]['test']):
            raise Unclear(f'the config: every test pattern of {name} needs a (?P<name>...) group')
    return compiled, max_bytes


def language(path, langs):
    return next((lang for lang in langs.values() if any(g.match(path) for g in lang['files'])), None) if path else None


def parse(text, lang):
    """([Test], preamble lines): each test's lines, from its `before` lines to the next test's; the lines above the
    first test are the file's own."""
    lines, found = text.split('\n'), {}
    for p in lang['test']:
        line, at = 0, 0   # matches come in order: count the newlines since the last one, not from the top each time
        for m in p.finditer(text):
            line, at = line + text.count('\n', at, m.start()), m.start()
            found.setdefault(line, m.group('name'))
    heads, last = [], -1
    for line in sorted(found):
        top = line
        while top - 1 > last and any(b.search(lines[top - 1]) for b in lang['before']):
            top -= 1
        heads.append((top, found[line]))
        last = line
    tests = [Test(name, lines[top:heads[i + 1][0] if i + 1 < len(heads) else len(lines)])
             for i, (top, name) in enumerate(heads)]
    return tests, lines[:heads[0][0] if heads else len(lines)]


def norm(line):
    return ' '.join(line.split())


def compare(path, name, old, new, lang, assertions=True):
    """The items for one existing test (or the file's own lines): skips and focus added, assertions gone."""
    items, old_set = [], {norm(x) for x in old}
    for kind in ('skip', 'focus'):
        for p in lang[kind]:
            if sum(len(p.findall(x)) for x in new) > sum(len(p.findall(x)) for x in old):
                hits = [norm(x) for x in new if p.search(x)]
                items.append(Item(path, name, 'skipped' if kind == 'skip' else 'focused',
                                  next((h for h in hits if h not in old_set), hits[0])))
                break
    if assertions:
        def asserts(lines):
            return collections.Counter(norm(x) for x in lines if any(p.search(x) for p in lang['assert']))
        gone = list((asserts(old) - asserts(new)).elements())
        if gone:
            items.append(Item(path, name, 'assertion', gone[0] + (f' (+{len(gone) - 1} more)' if len(gone) > 1 else '')))
    return items


def rows_of(raw):
    """[(status, old mode, new mode, old blob, new blob, old path, new path)] from `git diff --raw -z -M`."""
    parts, rows, i = raw.decode('utf-8', 'surrogateescape').split('\0'), [], 0
    while i < len(parts) - 1:
        meta = parts[i]
        if not meta.startswith(':') or len(meta[1:].split()) != 5:
            raise Unclear('git diff printed something this script does not read')
        old_mode, new_mode, old_sha, new_sha, status = meta[1:].split()
        two = status[0] in 'RC'
        old_path, new_path = parts[i + 1], parts[i + 2] if two else parts[i + 1]
        rows.append((status[0], old_mode, new_mode, old_sha, new_sha, old_path, new_path))
        i += 3 if two else 2
    return rows


def find(base, head, config_path, git):
    """Found(items, merge base) for the change merge-base(base, head)..head; Unclear when it cannot be read."""
    shas = {}
    for what, ref in (('base', base), ('head', head)):
        out = git('rev-parse', '-q', '--verify', f'{ref}^{{commit}}')
        if not out:
            raise Unclear(f'the {what} {ref[:12] or "(empty)"} is not a commit in the checkout (check out with '
                          f'fetch-depth: 0)')
        shas[what] = out.decode().strip()
    mb = (git('merge-base', shas['base'], shas['head']) or b'').decode().strip()
    if not mb:
        raise Unclear(f'no merge base between the base {shas["base"][:12]} and the head {shas["head"][:12]} '
                      f'(a shallow checkout?)')
    listed = git('ls-tree', '-z', '--name-only', shas['base'], '--', config_path)
    if listed is None:
        raise Unclear(f'git ls-tree failed for the config {config_path}')
    text = None
    if listed:
        blob = git('show', f'{shas["base"]}:{config_path}')
        if blob is None:
            raise Unclear(f'the config {config_path} at the base could not be read')
        try:
            text = blob.decode('utf-8')
        except UnicodeDecodeError:
            raise Unclear(f'the config {config_path} at the base is not UTF-8')
    langs, max_bytes = load_config(text)
    raw = git('-c', 'core.quotepath=off', 'diff', '--raw', '-z', '-M', '--no-abbrev', '--no-ext-diff', '--no-color', mb, shas['head'])
    if raw is None:
        raise Unclear(f'git diff {mb[:12]} {shas["head"][:12]} failed')
    files = []
    for status, old_mode, new_mode, old_sha, new_sha, old_path, new_path in rows_of(raw):
        old_lang = language(old_path, langs) if status not in 'AC' and old_mode in PLAIN_MODES else None
        new_lang = language(new_path, langs) if status != 'D' and new_mode in PLAIN_MODES else None
        if old_lang or new_lang:
            files.append((old_path if old_lang else None, old_sha if old_lang else None, old_lang,
                          new_path if new_lang else None, new_sha if new_lang else None, new_lang))
    blobs = sorted({s for f in files for s in (f[1], f[4]) if s})
    sizes = git('cat-file', '--batch-check', input=''.join(s + '\n' for s in blobs).encode()) if blobs else b''
    if sizes is None:
        raise Unclear('git cat-file --batch-check failed')
    try:
        total = sum(int(line.split()[2]) for line in sizes.decode().splitlines())
    except (IndexError, ValueError):
        raise Unclear('a test file of the change is missing from the checkout')
    if total > max_bytes:
        raise Unclear(f'the test files this change touches hold {total} bytes, over max_bytes {max_bytes}')
    items, gone, added = [], [], []
    for old_path, old_sha, old_lang, new_path, new_sha, new_lang in files:
        try:
            old = git('cat-file', 'blob', old_sha).decode('utf-8') if old_sha else None
            new = git('cat-file', 'blob', new_sha).decode('utf-8') if new_sha else None
        except (UnicodeDecodeError, AttributeError):
            items.append(Item(new_path or old_path, FILE, 'unreadable', 'not UTF-8, or git could not read it'))
            continue
        old_tests, old_pre = parse(old, old_lang) if old is not None else ([], [])
        new_tests, new_pre = parse(new, new_lang) if new is not None else ([], [])
        if old is not None and new is not None:
            items += compare(new_path, FILE, old_pre, new_pre, new_lang, assertions=False)
        mine = collections.defaultdict(list)
        for t in new_tests:
            mine[t.name].append(t)
        for t in old_tests:
            if mine.get(t.name):
                items += compare(new_path, t.name, t.lines, mine[t.name].pop(0).lines, new_lang)
            else:
                gone.append((old_path, t, old_lang))
        added += [(new_path, t, new_lang) for ts in mine.values() for t in ts]
    for old_path, t, old_lang in gone:   # moved to another file the change touches: compared there
        hit = next((a for a in added if a[1].name == t.name), None)
        if hit:
            added.remove(hit)
            items += compare(hit[0], t.name, t.lines, hit[1].lines, hit[2])
        else:
            items.append(Item(old_path, t.name, 'deleted', ''))
    return Found(sorted(set(items)), mb)


def explained(item, body_lines):
    """True when a line of the PR body names the test (or, for the file's own items, the file)."""
    if item.name == FILE:
        names = (item.path, item.path.rsplit('/', 1)[-1])
    else:
        names = (item.name,)
    return any(re.search(r'(?<!\w)' + re.escape(n) + r'(?!\w)', line) for n in names for line in body_lines)


def code(text):
    """A table cell's code span: nothing in a name can end the span, split the row, open HTML or mention anyone."""
    text = ''.join(c if c.isprintable() else ' ' for c in str(text))
    text = text.replace('`', "'").replace('|', '¦').replace('<', '‹').replace('>', '›')
    text = text if len(text) <= CELL else text[:CELL - 1] + '…'
    return f'`{text.strip() or " "}`'


REFUSED_TEXT = ('**No report for head {head}:** git-clean refused the checkout\'s git config (the job log says why), '
                'so nothing was checked. An earlier report here was for another head.')


def render(items, reason, why, merge_base, head, refused=False):
    lines = [MARKER, '### Existing tests this pull request deletes, skips or weakens', '']
    if refused:
        lines += [REFUSED_TEXT.format(head=code(head[:12])), '', 'Report only: this never fails the pull request.']
        return '\n'.join(lines) + '\n'
    if items is None:
        lines += [f'**Could not read the change:** {code(reason)}. This is not an empty list: nothing was checked.',
                  '', 'Report only: this never fails the pull request.']
        return '\n'.join(lines) + '\n'
    lines += [f'Head {code(head[:12])} against its merge base {code(merge_base[:12])} with the base. Report only: '
              f'this never fails the pull request.', '']
    if not items:
        lines.append('None of the existing tests in the files this change touches was deleted, skipped or weakened.')
        return '\n'.join(lines) + '\n'
    lines += ['| test | file | change | why in the PR body |', '| --- | --- | --- | --- |']
    for item, ok in list(zip(items, why))[:MAX_ROWS]:
        change = KIND_TEXT[item.kind] + (f': {code(item.detail)}' if item.detail else '')
        lines.append(f'| {code(item.name)} | {code(item.path)} | {change} | '
                     f'{"?" if ok is None else "yes" if ok else "no"} |')
    if len(items) > MAX_ROWS:
        lines.append(f'\n… and {len(items) - MAX_ROWS} more not shown.')
    lines.append('')
    missing = sum(1 for ok in why if ok is False)
    if why and why[0] is None:
        lines.append('The pull request body could not be read: the why column is unknown.')
    elif missing:
        lines.append(f'**Without a why: {missing}.** Add one line per listed test to the pull request body that names '
                     f'the test and says why it changed (rows marked no).')
    text = '\n'.join(lines) + '\n'
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT].rsplit('\n', 1)[0] + '\n\n… cut to fit a comment.\n'


def escape(text):
    """One log line: nothing in a file or test name can start a workflow command."""
    return str(text).replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def gh_run(*args, input=None):
    return subprocess.run(['gh', *args], input=input, text=True, capture_output=True)


def read_body(gh, repo, pr):
    r = gh('api', f'repos/{repo}/pulls/{pr}')
    if r.returncode:
        raise RuntimeError(f'could not read the pull request: {(r.stderr or "").strip()[:200]}')
    return json.loads(r.stdout).get('body') or ''


def publish(gh, repo, pr, text, author, quiet):
    """created, updated, unchanged or none: the one comment with the marker by `author`, edited in place."""
    r = gh('api', '--paginate', f'repos/{repo}/issues/{pr}/comments?per_page=100',
           '--jq', '.[] | {id: .id, login: .user.login, body: .body} | @json')
    if r.returncode:
        raise RuntimeError(f'could not list the comments: {(r.stderr or "").strip()[:200]}')
    mine = [c for c in map(json.loads, r.stdout.splitlines()) if c.get('login') == author
            and (c.get('body') or '').startswith(MARKER)]
    if not mine and quiet:
        return 'none'
    if mine and mine[0]['body'].rstrip() == text.rstrip():
        return 'unchanged'
    where, method = ((f'repos/{repo}/issues/comments/{mine[0]["id"]}', 'PATCH') if mine
                     else (f'repos/{repo}/issues/{pr}/comments', 'POST'))
    r = gh('api', '-X', method, where, '--input', '-', input=json.dumps({'body': text}))
    if r.returncode:
        raise RuntimeError(f'could not {"edit" if mine else "post"} the comment: {(r.stderr or "").strip()[:200]}')
    return 'updated' if mine else 'created'


def append(path, text):
    if path:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(text)


def cli(argv=None, env=None, gh=None, out=print):
    env, gh = os.environ if env is None else env, gh or gh_run
    p = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    for name in ('--repo', '--pr', '--base', '--head'):
        p.add_argument(name, required=True)
    p.add_argument('--config', default=DEFAULT_CONFIG)
    p.add_argument('--path', default='.')
    p.add_argument('--comment-author', default='github-actions[bot]')
    p.add_argument('--no-comment', action='store_true')
    p.add_argument('--refused', action='store_true')
    try:
        a = p.parse_args(argv)
    except SystemExit:
        out('::warning::weakened-tests: bad arguments; nothing was checked (this never fails the pull request)')
        return 0
    if a.refused:
        return refused(a, env, gh, out)
    items, reason, mb = None, '', ''
    try:
        items, mb = find(a.base, a.head, a.config, Git(a.path))
    except Unclear as e:
        reason = str(e)
    except Exception as e:   # noqa: BLE001 - report-only: an unforeseen error is a "could not read", not a red step
        reason = f'unexpected error: {type(e).__name__}: {e}'
    body = None
    if items:
        try:
            body = read_body(gh, a.repo, a.pr).replace('\r', '').split('\n')
        except Exception as e:   # noqa: BLE001
            out(f'::warning::weakened-tests: {escape(e)}')
    why = [None if body is None else explained(i, body) for i in items or []]
    text = render(items, reason, why, mb, a.head)
    if items is None:
        out(f'::warning::weakened-tests: could not read the change: {escape(reason)}')
    for item, ok in zip(items or [], why):
        out(f'weakened-tests: {escape(item.path)}: {escape(item.name)}: {item.kind} {escape(item.detail)}'
            f'{"" if ok else " (no why in the PR body)" if ok is False else ""}')
    out(f'weakened-tests: {"could not read" if items is None else f"{len(items)} listed"}')
    try:
        append(env.get('GITHUB_STEP_SUMMARY'), text)
        append(env.get('GITHUB_OUTPUT'), f'status={"could-not-read" if items is None else "ok"}\n'
               f'count={"" if items is None else len(items)}\n'
               f'missing_why={"" if items is None or (items and body is None) else sum(1 for ok in why if not ok)}\n')
    except OSError as e:
        out(f'::warning::weakened-tests: could not write the outputs: {escape(e)}')
    if not a.no_comment:
        try:
            out(f'weakened-tests: comment {publish(gh, a.repo, a.pr, text, a.comment_author, items == [])}')
        except Exception as e:   # noqa: BLE001
            out(f'::warning::weakened-tests: {escape(e)}')
    return 0


def refused(a, env, gh, out):
    """git-clean refused the checkout: no git, status refused, and an earlier head's report comment replaced."""
    text = render(None, '', [], '', a.head, refused=True)
    try:
        append(env.get('GITHUB_STEP_SUMMARY'), text)
        append(env.get('GITHUB_OUTPUT'), 'status=refused\ncount=\nmissing_why=\n')
    except OSError as e:
        out(f'::warning::weakened-tests: could not write the outputs: {escape(e)}')
    if not a.no_comment:
        try:
            out(f'weakened-tests: comment {publish(gh, a.repo, a.pr, text, a.comment_author, True)}')
        except Exception as e:   # noqa: BLE001
            out(f'::warning::weakened-tests: {escape(e)}')
    return 0


if __name__ == '__main__':
    sys.exit(cli())
