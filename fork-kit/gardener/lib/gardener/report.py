"""gardener.report: the rolled-up report of one project's run, and its one issue on GitHub, updated in place.

Two reports are written from the same findings:
- The issue part (render_issue) is built from an allow-list, because the report repository may be public: per
  finding only its severity, check, id, a `where` that is a path inside the project's repository (relative) or a
  pull request or issue of it (`#N`, written `OWNER/REPO#N` when the report goes to another repository), and
  its text and fix with what still looks like a local name (a path, URL, address, user@host, e-mail, a Windows or
  share path) or a well-known token shape masked. Free text only of the general checks (their words are known), the gardener's own findings
  and project checks set to "publicText": true; another project check's findings show id, where and a count. A
  finding whose `where` is outside the repository, or that has none, is only counted per check, without its id
  (the gardener's own findings excepted: their words are fixed). With counts_only (a private project repository
  reporting to a public one) every finding is only counted and there is no judgment. Notes are the gardener's own
  words; the judgment is masked like the text. Nothing a check printed on stderr goes there.
- The local report (render_local, report.md in the state folder) holds everything, as it is.

The issue is found by a hidden marker line, `<!-- gardener-report:NAME -->`. Everything from the marker down is the
gardener's and is written whole on each run; text a person wrote above the marker is kept (cut, with a note, only
when the whole body would pass MAX_BODY). An issue without the marker (edited away by hand) is no longer the
report: the next run looks again and, finding none, opens a new one. A closed report issue is left closed, and a
new one is opened. GitHub calls per run: at most one issue read (the number the state file remembers); only when
that does not give the open report issue, a search by title and, when that finds none, one by marker; and one write
(create or edit).
"""
import json, os, re, subprocess

SEVERITY_ORDER = {'error': 0, 'warn': 1, 'info': 2}
MAX_PART = 50000             # the gardener's part of the issue
MAX_BODY = 60000             # the whole body, a person's text above the marker too; GitHub's limit is 65536
CUT_NOTE = '\n\n_(the text above was cut to fit the issue size limit)_\n\n'
OUTSIDE = '<outside repo>'
LOCAL_HINT = ('Full text, notes and paths outside the repository: `report.md` in the gardener\'s state folder on the '
              'machine that runs it.')

_SAFE_REL = re.compile(r'[A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)*')
_REPO_WHERE = re.compile(r'([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+): (.+)')
_NUMBER = re.compile(r'#[1-9][0-9]{0,8}')
_MASKS = (
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S), '<key>'),
    (re.compile(r'\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|glpat-[\w-]{20,}|xox[abposr]-[\w-]{10,}'
                r'|sk-[\w-]{20,}|AKIA[0-9A-Z]{16})\b'), '<token>'),      # well-known token shapes
    (re.compile(r'[A-Za-z][A-Za-z0-9+.-]*://\S+'), '<url>'),
    (re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)*'), '<user@host>'),     # e-mail, ssh user@host
    (re.compile(r'\b\d{1,3}(?:\.\d{1,3}){3}\b'), '<ip>'),
    (re.compile(r'(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){4,7}[0-9A-Fa-f]{1,4}\b'
                r'|(?<![\w:])(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?::'
                r'(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4})*)?(?![\w:])'), '<ip>'),     # IPv6, also with ::
    (re.compile(r'(?<![\w\\])\\\\[^\s\'"`|),;]+'), '<path>'),        # \\host\share
    (re.compile(r'(?<![\w])[A-Za-z]:[\\/][^\s\'"`|),;]*'), '<path>'),   # C:\Users\..., C:/...
    (re.compile(r'\S*-home-\S*'), '<path>'),                      # a home folder encoded in a folder name
    (re.compile(r'(?<!~)~[\w.-]*(?:/\S*)?'), '<path>'),
    (re.compile(r'(?<![\w.~-])/[^\s\'"`|),;]+'), '<path>'),
    (re.compile(r'\b[\w-]+(?:\.[\w-]+)*\.(?:local|lan|internal|localdomain|home\.arpa|ts\.net)\b', re.I),
     '<host>'),
)


class GitHubError(Exception):
    pass


def marker(name):
    return f'<!-- gardener-report:{name} -->'


_END = r'(?=/|$|[\s\'"`|),;:]|\.(?:\s|$))'   # a path ends here: not inside a longer folder name


def redact(text, root, home):
    """The project root becomes ".", the home "~"; each only as a whole folder (/srv/proj2 is not /srv/proj)."""
    for path, short in ((root, '.'), (home, '~')):
        path = (path or '').rstrip('/')
        if path:
            text = re.sub(re.escape(path) + _END, short, text)
    return text


def public_text(text, root, home):
    """Text for the issue: the root as ".", then every path outside it, URL, address, e-mail and local host name
    masked."""
    text = redact(text or '', root, home)
    for rx, sub in _MASKS:
        text = rx.sub(sub, text)
    return text


def public_where(where, root, report_repo='', repo=''):
    """Where to show in the issue: a path inside the repository, relative (":LINE" kept), "OWNER/REPO: path" of the
    report repository, a pull request or issue of the project's repository ("#N"; "OWNER/REPO#N" when the report
    repository is another one, so the link is not to its own #N), or '' for none. None: outside the repository, or
    not a plain path."""
    w = (where or '').strip()
    if not w:
        return ''
    if _NUMBER.fullmatch(w):
        if not repo or (report_repo and repo.lower() != report_repo.lower()):
            return f'{repo}{w}' if repo else None
        return w
    m = _REPO_WHERE.fullmatch(w)
    if m:
        if report_repo and m.group(1).lower() == report_repo.lower():
            rest = public_where(m.group(2), '', '')
            return f'{m.group(1)}: {rest}' if rest else None
        return None
    line = ''
    m = re.fullmatch(r'(.+?)(:\d+)', w)
    if m:
        w, line = m.groups()
    if w.startswith('~'):
        return None
    if os.path.isabs(w):
        if not root:
            return None
        w = os.path.relpath(os.path.normpath(w), os.path.normpath(root))
    w = os.path.normpath(w)
    if w == '.':
        return '.' + line
    if w == '..' or w.startswith('../') or not _SAFE_REL.fullmatch(w):
        return None
    return w + line


def _cell(text):
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ').strip()


def _counts(findings):
    c = {s: sum(1 for f in findings if f['severity'] == s) for s in SEVERITY_ORDER}
    return (f'{c["error"]} error(s), {c["warn"]} warning(s), {c["info"]} note(s). Report only, unless a check is '
            'set to fix.') if findings else 'No findings.'


def _document(name, title, today, counts, rows, notes, judgment, cap, more_hint, extra=()):
    rows = sorted(rows, key=lambda r: (SEVERITY_ORDER.get(r[0], 3), r[1], r[2], r[3]))
    lines = [marker(name), f'## {title}: {name}, {today}', '', counts, *extra, '']
    tail = []
    if notes:
        tail += ['### Notes', ''] + [f'- {_cell(n)}' for n in notes]
    if judgment:
        tail += ['', '### Judgment (model draft)', '',
                 '<sub>A model wrote this; nobody checked it. Use it as a hint, not as a finding.</sub>', '',
                 judgment.strip()]
    tail += ['', f'<sub>Written by the gardener (fork-kit/gardener) on {today}; it rewrites everything below the '
                 'marker on each run.</sub>']
    if rows:
        lines += ['| Severity | Check | Finding | Where | Fix |', '| --- | --- | --- | --- | --- |']
    shown = 0
    budget = (cap - len('\n'.join(lines)) - len('\n'.join(tail)) - 300) if cap else None
    for r in rows:
        row = '| ' + ' | '.join(_cell(v) for v in r) + ' |'
        if budget is not None:
            if len(row) + 1 > budget:
                break
            budget -= len(row) + 1
        lines.append(row)
        shown += 1
    if shown < len(rows):
        lines += ['', f'{len(rows) - shown} more row(s) not shown (the issue is full): {more_hint}']
    lines.append('')
    return '\n'.join(lines + tail) + '\n'


def public_findings(findings, root, home, report_repo='', text_checks=(), counts_only=False, repo=''):
    """The allow-listed view of the findings (module doc): [{severity, check, id, text, where, fix}]. text_checks:
    the ids of the checks whose text may be shown (general checks and "publicText" project checks); the gardener's
    own findings always. Folded into one counted entry: a finding outside the repository or with no where (per
    severity, check and where; no id: a project check's id may name a local thing), and one whose text may not be
    shown (per severity, check, id and where). counts_only: every finding, per severity and check. repo: the
    project's repository (a "#N" where is one of its pull requests or issues)."""
    out, hidden = [], {}
    for f in findings:
        w = public_where(f.get('where'), root, report_repo, repo)
        if counts_only:
            key = (f['severity'], f['check'], '', '', 'in the local report only')
        elif w is None or (w == '' and not f.get('own')):
            key = (f['severity'], f['check'], '', OUTSIDE if w is None else '', 'in the local report only')
        elif not f.get('own') and f['check'] not in text_checks:
            key = (f['severity'], f['check'], f['id'], w, 'text in the local report only (the check is not set to '
                   'publicText)')
        else:
            out.append({'severity': f['severity'], 'check': f['check'], 'id': f['id'], 'where': w,
                        'text': public_text(f['text'], root, home),
                        'fix': public_text(f.get('fix') or '', root, home)})
            continue
        hidden[key] = hidden.get(key, 0) + 1
    for (severity, check, id_, where, why), n in hidden.items():
        out.append({'severity': severity, 'check': check, 'id': id_, 'where': where,
                    'text': f'{n} finding(s), {why}', 'fix': ''})
    return out


COUNTS_ONLY = ('Counts only: the project\'s repository is private (or not set, or its visibility could not be read) '
               'and this report repository is public.')


def render_issue(name, today, findings, notes, judgment=None, root='', home='', report_repo='', text_checks=(),
                 counts_only=False, repo=''):
    """The issue part, from the marker down (markdown), from the allow-list (module doc)."""
    rows = [(f['severity'], f['check'], f'{f["id"]}: {f["text"]}' if f['id'] else f['text'], f['where'], f['fix'])
            for f in public_findings(findings, root, home, report_repo, text_checks, counts_only, repo)]
    return _document(name, 'Gardener report', today, _counts(findings), rows,
                     [public_text(n, root, home) for n in notes],
                     public_text(judgment, root, home) if judgment and not counts_only else None, MAX_PART,
                     'the local report has them all', ('', LOCAL_HINT) + (('', COUNTS_ONLY) if counts_only else ()))


def render_local(name, today, findings, notes, judgment=None):
    """The local report (report.md): every finding, its detail and its full where, as they are."""
    rows = [(f['severity'], f['check'], f'{f["id"]}: {f["text"]}' + (f' ({f["detail"]})' if f.get('detail') else ''),
             f.get('where') or '', f.get('fix') or '') for f in findings]
    return _document(name, 'Gardener report (local, in full)', today, _counts(findings), rows, notes, judgment,
                     None, '')


def body_with(old_body, name, new_part):
    """The new issue body: what a person wrote above the marker, then the new report part; at most MAX_BODY (the
    person's text is cut, with a note, when it does not fit)."""
    head = ''
    if old_body and marker(name) in old_body:
        head = old_body[:old_body.index(marker(name))]
    room = MAX_BODY - len(new_part)
    if len(head) > room:
        head = head[:max(0, room - len(CUT_NOTE))] + CUT_NOTE
    return head + new_part


class GitHub:
    """GitHub through `gh api` (the configured gh command, with the login of the user who runs the gardener)."""

    def __init__(self, gh, timeout=60):
        self.gh, self.timeout = gh, timeout

    def call(self, method, path, fields=None):
        args = [self.gh, 'api', '--method', method, path]
        data = None
        if fields is not None:
            args += ['--input', '-']
            data = json.dumps(fields)
        try:
            p = subprocess.run(args, input=data, capture_output=True, text=True, timeout=self.timeout,
                               stdin=None if data is not None else subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise GitHubError(f'gh api {method} {path}: {e}') from None
        if p.returncode != 0:
            msg = (p.stderr or p.stdout).strip().splitlines()
            msg = msg[-1] if msg else f'exit {p.returncode}'
            if re.search(r'rate limit|HTTP 429|retry-after', msg, re.I):
                msg += ' (rate limit: the next run tries again)'
            raise GitHubError(f'gh api {method} {path}: {msg}')
        try:
            return json.loads(p.stdout) if p.stdout.strip() else None
        except ValueError:
            raise GitHubError(f'gh api {method} {path}: the answer is not JSON') from None

    def issue(self, repo, number):
        return self.call('GET', f'repos/{repo}/issues/{number}')

    def private(self, repo):
        """True, False, or None when GitHub does not say."""
        got = self.call('GET', f'repos/{repo}')
        v = got.get('private') if isinstance(got, dict) else None
        return v if isinstance(v, bool) else None

    def search_open(self, repo, text, field='body'):
        q = f'repo:{repo} is:issue is:open in:{field} "{text}"'
        from urllib.parse import quote
        found = self.call('GET', f'search/issues?q={quote(q)}&per_page=10') or {}
        return found.get('items') or []

    def create(self, repo, title, body):
        return self.call('POST', f'repos/{repo}/issues', {'title': title, 'body': body})

    def edit(self, repo, number, body):
        return self.call('PATCH', f'repos/{repo}/issues/{number}', {'body': body})


def _ours(issue, name):
    return (isinstance(issue, dict) and issue.get('state') == 'open' and 'pull_request' not in issue
            and marker(name) in (issue.get('body') or ''))


def publish(gh, repo, name, part, known_number=None):
    """Write the report part to the project's report issue; returns (issue number, 'created' or 'updated').
    Raises GitHubError."""
    issue = None
    if known_number:
        try:
            got = gh.issue(repo, known_number)
        except GitHubError:
            got = None                         # deleted, moved or unreadable: look again
        if _ours(got, name):
            issue = got
    if issue is None:            # by title, then (renamed by hand) by the marker; GitHub search may skip comments
        for text, field in ((f'Gardener report: {name}', 'title'), (f'gardener-report:{name}', 'body')):
            mine = [i for i in gh.search_open(repo, text, field) if _ours(i, name)]
            if mine:
                issue = min(mine, key=lambda i: i['number'])  # the oldest open one, always the same
                break
    if issue is None:
        made = gh.create(repo, f'Gardener report: {name}', part)
        return made['number'], 'created'
    gh.edit(repo, issue['number'], body_with(issue.get('body') or '', name, part))
    return issue['number'], 'updated'


def home_dir():
    return os.path.expanduser('~')
