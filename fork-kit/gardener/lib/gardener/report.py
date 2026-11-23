"""gardener.report: the rolled-up report of one project's run, and its one issue on GitHub, updated in place.

The issue is found by a hidden marker line, `<!-- gardener-report:NAME -->`. Everything from the marker down is the
gardener's and is written whole on each run; text a person wrote above the marker is kept. An issue without the
marker (edited away by hand) is no longer the report: the next run looks again and, finding none, opens a new one.
A closed report issue is left closed, and a new one is opened. GitHub calls per run: at most one issue read (the
number the state file remembers), one search (only when that read does not give an open report issue) and one
write (create or edit).
"""
import json, os, re, subprocess

SEVERITY_ORDER = {'error': 0, 'warn': 1, 'info': 2}
MAX_BODY = 60000             # GitHub's limit is 65536 characters


class GitHubError(Exception):
    pass


def marker(name):
    return f'<!-- gardener-report:{name} -->'


def redact(text, root, home):
    """Local folder names out of text that may go to a public issue: the project root becomes ".", the home "~"."""
    if root:
        text = text.replace(root.rstrip('/') + '/', './').replace(root.rstrip('/'), '.')
    if home and home != '/':
        text = text.replace(home.rstrip('/') + '/', '~/').replace(home.rstrip('/'), '~')
    return text


def _cell(text):
    return text.replace('\\', '\\\\').replace('|', '\\|').replace('\r', ' ').replace('\n', ' ').strip()


def render(name, today, findings, notes, judgment=None, root='', home=''):
    """The report text from the marker down (markdown)."""
    findings = sorted(findings, key=lambda f: (SEVERITY_ORDER.get(f['severity'], 3), f['check'], f['id'], f['where']))
    counts = {s: sum(1 for f in findings if f['severity'] == s) for s in SEVERITY_ORDER}
    lines = [marker(name), f'## Gardener report: {name}, {today}', '']
    if findings:
        lines.append(f'{counts["error"]} error(s), {counts["warn"]} warning(s), {counts["info"]} note(s). '
                     'Report only, unless a check is set to fix.')
    else:
        lines.append('No findings.')
    lines.append('')
    tail = []
    for n in notes:
        tail.append(f'- {_cell(redact(n, root, home))}')
    if judgment:
        tail += ['', '### Judgment (local model)', '', redact(judgment, root, home).strip()]
    tail += ['', f'<sub>Written by the gardener (fork-kit/gardener) on {today}; it rewrites everything below the '
                 'marker on each run.</sub>']
    if notes:
        tail.insert(0, '### Notes')
        tail.insert(1, '')
    if findings:
        lines += ['| Severity | Check | Finding | Where | Fix |', '| --- | --- | --- | --- | --- |']
    shown = 0
    budget = MAX_BODY - len('\n'.join(lines)) - len('\n'.join(tail)) - 200
    for f in findings:
        row = '| ' + ' | '.join(_cell(redact(v, root, home)) for v in
                                (f['severity'], f['check'], f'{f["id"]}: {f["text"]}', f['where'], f['fix'])) + ' |'
        if len(row) + 1 > budget:
            break
        lines.append(row)
        budget -= len(row) + 1
        shown += 1
    if shown < len(findings):
        lines += ['', f'{len(findings) - shown} more finding(s) not shown (the issue is full): run '
                      f'`gardener run --project {name} --dry-run` to see them all.']
    lines.append('')
    return '\n'.join(lines + tail) + '\n'


def body_with(old_body, name, new_part):
    """The new issue body: what a person wrote above the marker, then the new report part."""
    head = ''
    if old_body and marker(name) in old_body:
        head = old_body[:old_body.index(marker(name))]
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

    def search_open(self, repo, text):
        q = f'repo:{repo} is:issue is:open in:body "{text}"'
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
    if issue is None:
        mine = [i for i in gh.search_open(repo, f'gardener-report:{name}') if _ours(i, name)]
        if mine:
            issue = min(mine, key=lambda i: i['number'])      # the oldest open one, always the same
    if issue is None:
        made = gh.create(repo, f'Gardener report: {name}', part)
        return made['number'], 'created'
    gh.edit(repo, issue['number'], body_with(issue.get('body') or '', name, part))
    return issue['number'], 'updated'


def home_dir():
    return os.path.expanduser('~')
