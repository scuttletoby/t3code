"""The two ways schedule-kick talks to GitHub, both narrow on purpose (fork-kit/schedule-kick/README.md, Token):

- TokenApi: the REST API with a token read from a file the config names (actions:write on the listed repositories
  only). No redirect is followed, so the token never goes to another host.
- GhAsApi: `gh-as --repo OWNER/NAME gh ...` of the gh-apps kit (a GitHub App token for that one repository).

Neither reads GH_TOKEN, GITHUB_TOKEN or gh's own login: a person's login is never used.
Each has runs_since(repo, workflow, ref, since) -> [{'created', 'event', 'branch'}, ...] (runs of that workflow
created at or after `since`; the caller picks the ones that count) and dispatch(repo, workflow, ref, inputs).
"""
import json, subprocess, urllib.error, urllib.parse, urllib.request

API = 'https://api.github.com'
LIST_LIMIT = 100            # the most one page holds: pull_request or push runs can come first
MAX_REPLY = 1024 * 1024     # bytes of one answer read at most
MAX_RESET = 10 ** 11        # an x-ratelimit-reset past this (year 5138) is not a time
MAX_RETRY_AFTER = 24 * 3600
SECONDARY = 'secondary rate limit'


class ApiError(Exception):
    def __init__(self, message, rate_limited=False, reset=None, retry_after=None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.reset = reset              # epoch seconds GitHub gave for the end of the limit, or None
        self.retry_after = retry_after  # seconds GitHub asked to wait (secondary limit), or None


def iso(when):
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


def _number(text, most):
    """A header's whole number, or None when it is missing, not digits or larger than `most`."""
    if not isinstance(text, str) or not text.isascii() or not text.isdigit() or len(text) > 12:
        return None
    value = int(text)
    return value if value <= most else None


def _row(created, event, branch):
    return {'created': created, 'event': event, 'branch': branch}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f'redirect to {newurl} refused', headers, fp)


class TokenApi:
    def __init__(self, token, base=API, timeout=30):
        self.token, self.base, self.timeout = token, base, timeout
        self.remaining = None       # x-ratelimit-remaining of the last answer
        self.reset = None           # x-ratelimit-reset of the last answer (epoch seconds)
        self.opener = urllib.request.build_opener(_NoRedirect)

    @staticmethod
    def _read(r):
        data = r.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise ValueError(f'an answer larger than {MAX_REPLY} bytes')
        return data.decode('utf-8', 'replace')

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {self.token}',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'fork-kit-schedule-kick',
            **({'Content-Type': 'application/json'} if data is not None else {})})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                self._limits(r.headers)
                text = self._read(r)
                return json.loads(text) if text.strip() else None
        except urllib.error.HTTPError as e:
            self._limits(e.headers)
            headers = e.headers or {}
            detail = ''
            try:
                detail = str(json.loads(self._read(e)).get('message', ''))[:300]
            except Exception:       # noqa: BLE001 - any body: the status says enough
                pass
            retry_after = _number(headers.get('retry-after'), MAX_RETRY_AFTER)
            limited = e.code == 429 or (e.code == 403 and (
                headers.get('x-ratelimit-remaining') == '0' or retry_after is not None
                or SECONDARY in detail.lower()))
            raise ApiError(f'{method} {path}: HTTP {e.code} {detail}'.strip(), limited,
                           _number(headers.get('x-ratelimit-reset'), MAX_RESET), retry_after) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f'{method} {path}: {type(e).__name__}: {getattr(e, "reason", e)}') from None

    def _limits(self, headers):
        if not headers:
            return
        remaining = _number(headers.get('x-ratelimit-remaining'), MAX_RESET)
        if remaining is not None:
            self.remaining = remaining
        reset = _number(headers.get('x-ratelimit-reset'), MAX_RESET)
        if reset is not None:
            self.reset = reset

    def runs_since(self, repo, workflow, ref, since, event=None):
        query = urllib.parse.urlencode({'created': f'>={iso(since)}', 'branch': ref, 'per_page': LIST_LIMIT,
                                        **({'event': event} if event else {})})
        got = self._call('GET', f'/repos/{repo}/actions/workflows/{urllib.parse.quote(str(workflow))}/runs?{query}')
        if not isinstance(got, dict) or not isinstance(got.get('workflow_runs'), list):
            raise ApiError(f'runs of {repo} {workflow}: an answer without workflow_runs')
        return [_row(r.get('created_at'), r.get('event'), r.get('head_branch'))
                for r in got['workflow_runs'] if isinstance(r, dict)]

    def run(self, repo, run_id):
        got = self._call('GET', f'/repos/{repo}/actions/runs/{int(run_id)}')
        if not isinstance(got, dict):
            raise ApiError(f'run {run_id} of {repo}: not an object')
        return got

    def dispatch(self, repo, workflow, ref, inputs):
        self._call('POST', f'/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/dispatches',
                   {'ref': ref, 'inputs': dict(inputs)})


class GhAsApi:
    def __init__(self, path, home, run=subprocess.run, timeout=120):
        self.path, self.run, self.timeout = path, run, timeout
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8'}     # nothing of the caller's gh
        self.remaining = None
        self.reset = None

    def _gh(self, repo, args):
        try:
            r = self.run([self.path, '--repo', repo, 'gh', *args], capture_output=True, text=True,
                         timeout=self.timeout, env=self.env, stdin=subprocess.DEVNULL)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise ApiError(f'{self.path}: {type(e).__name__}: {e}') from None
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip()
            raise ApiError(f'gh-as {" ".join(args[:2])}: exit {r.returncode}: {err[-300:]}',
                           'rate limit' in err.lower())     # the primary and the secondary limit
        if len(r.stdout or '') > MAX_REPLY:
            raise ApiError(f'gh-as {" ".join(args[:2])}: an answer larger than {MAX_REPLY} bytes')
        return r.stdout

    def runs_since(self, repo, workflow, ref, since):
        out = self._gh(repo, ['run', 'list', '--workflow', workflow, '--branch', ref, '--created', f'>={iso(since)}',
                              '--limit', str(LIST_LIMIT), '--json', 'createdAt,event,headBranch'])
        try:
            got = json.loads(out)
        except ValueError:
            raise ApiError(f'runs of {repo} {workflow}: gh run list did not print JSON') from None
        if not isinstance(got, list):
            raise ApiError(f'runs of {repo} {workflow}: gh run list did not print a list')
        return [_row(r.get('createdAt'), r.get('event'), r.get('headBranch')) for r in got if isinstance(r, dict)]

    def dispatch(self, repo, workflow, ref, inputs):
        args = ['workflow', 'run', workflow, '--ref', ref]
        for key, value in inputs.items():
            args += ['-f', f'{key}={value}']
        self._gh(repo, args)
