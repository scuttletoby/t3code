"""The two ways schedule-kick talks to GitHub, both narrow on purpose (fork-kit/schedule-kick/README.md, Token):

- TokenApi: the REST API with a token read from a file the config names (actions:write on the listed repositories
  only). No redirect is followed, so the token never goes to another host.
- GhAsApi: `gh-as --repo OWNER/NAME gh ...` of the gh-apps kit (a GitHub App token for that one repository).

Neither reads GH_TOKEN, GITHUB_TOKEN or gh's own login: a person's login is never used.
Each has runs_since(repo, workflow, since) -> [created_at, ...] (runs of that workflow created at or after `since`,
any event) and dispatch(repo, workflow, ref, inputs).
"""
import json, subprocess, urllib.error, urllib.parse, urllib.request

API = 'https://api.github.com'
LIST_LIMIT = 5


class ApiError(Exception):
    def __init__(self, message, rate_limited=False, reset=None):
        super().__init__(message)
        self.rate_limited = rate_limited
        self.reset = reset          # epoch seconds GitHub gave for the end of the limit, or None


def iso(when):
    return when.strftime('%Y-%m-%dT%H:%M:%SZ')


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise urllib.error.HTTPError(req.full_url, code, f'redirect to {newurl} refused', headers, fp)


class TokenApi:
    def __init__(self, token, base=API, timeout=30):
        self.token, self.base, self.timeout = token, base, timeout
        self.remaining = None       # x-ratelimit-remaining of the last answer
        self.opener = urllib.request.build_opener(_NoRedirect)

    def _call(self, method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method, headers={
            'Accept': 'application/vnd.github+json', 'Authorization': f'Bearer {self.token}',
            'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'fork-kit-schedule-kick',
            **({'Content-Type': 'application/json'} if data is not None else {})})
        try:
            with self.opener.open(req, timeout=self.timeout) as r:
                self._limits(r.headers)
                text = r.read().decode('utf-8', 'replace')
                return json.loads(text) if text.strip() else None
        except urllib.error.HTTPError as e:
            self._limits(e.headers)
            remaining = e.headers.get('x-ratelimit-remaining') if e.headers else None
            limited = e.code == 429 or (e.code == 403 and remaining == '0')
            reset = e.headers.get('x-ratelimit-reset') if e.headers else None
            detail = ''
            try:
                detail = json.loads(e.read().decode('utf-8', 'replace')).get('message', '')
            except Exception:       # noqa: BLE001 - any body: the status says enough
                pass
            raise ApiError(f'{method} {path}: HTTP {e.code} {detail}'.strip(), limited,
                           int(reset) if reset and reset.isdigit() else None) from None
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ApiError(f'{method} {path}: {type(e).__name__}: {getattr(e, "reason", e)}') from None

    def _limits(self, headers):
        value = headers.get('x-ratelimit-remaining') if headers else None
        if value is not None and value.isdigit():
            self.remaining = int(value)

    def runs_since(self, repo, workflow, since):
        query = urllib.parse.urlencode({'created': f'>={iso(since)}', 'per_page': LIST_LIMIT})
        got = self._call('GET', f'/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/runs?{query}')
        if not isinstance(got, dict) or not isinstance(got.get('workflow_runs'), list):
            raise ApiError(f'runs of {repo} {workflow}: an answer without workflow_runs')
        return [r.get('created_at') for r in got['workflow_runs'] if isinstance(r, dict)]

    def dispatch(self, repo, workflow, ref, inputs):
        self._call('POST', f'/repos/{repo}/actions/workflows/{urllib.parse.quote(workflow)}/dispatches',
                   {'ref': ref, 'inputs': dict(inputs)})


class GhAsApi:
    def __init__(self, path, home, run=subprocess.run, timeout=120):
        self.path, self.run, self.timeout = path, run, timeout
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8'}     # nothing of the caller's gh
        self.remaining = None

    def _gh(self, repo, args):
        try:
            r = self.run([self.path, '--repo', repo, 'gh', *args], capture_output=True, text=True,
                         timeout=self.timeout, env=self.env, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError) as e:
            raise ApiError(f'{self.path}: {type(e).__name__}: {e}') from None
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip()
            raise ApiError(f'gh-as {" ".join(args[:2])}: exit {r.returncode}: {err[-300:]}',
                           'rate limit' in err.lower())
        return r.stdout

    def runs_since(self, repo, workflow, since):
        out = self._gh(repo, ['run', 'list', '--workflow', workflow, '--created', f'>={iso(since)}',
                              '--limit', str(LIST_LIMIT), '--json', 'createdAt'])
        try:
            got = json.loads(out)
        except ValueError:
            raise ApiError(f'runs of {repo} {workflow}: gh run list did not print JSON') from None
        if not isinstance(got, list):
            raise ApiError(f'runs of {repo} {workflow}: gh run list did not print a list')
        return [r.get('createdAt') for r in got if isinstance(r, dict)]

    def dispatch(self, repo, workflow, ref, inputs):
        args = ['workflow', 'run', workflow, '--ref', ref]
        for key, value in inputs.items():
            args += ['-f', f'{key}={value}']
        self._gh(repo, args)
