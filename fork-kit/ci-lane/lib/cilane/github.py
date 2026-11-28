"""GitHub for the lane, as the CI App: `gh-as --repo OWNER/NAME gh api ...` of the gh-apps kit.

Check runs can only be written by a GitHub App, and the CI App's key stays with gh-as (root's), never with this
user. The App needs Checks (write), Actions, Contents and Pull requests (read) on the one repository, nothing else.
Every call has a timeout and a size cap; an error says whether it was a rate limit.
"""
import base64, json, re, subprocess, urllib.parse

MAX_REPLY = 4 * 1024 * 1024


class ApiError(Exception):
    def __init__(self, message, rate_limited=False, status=None):
        super().__init__(message)
        self.rate_limited, self.status = rate_limited, status


class GhAs:
    def __init__(self, path, home, repo, run=subprocess.run, timeout=60):
        self.path, self.repo, self.run, self.timeout = path, repo, run, timeout
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': home, 'LANG': 'C.UTF-8'}     # nothing of the caller's gh

    def api(self, method, path, body=None):
        args = [self.path, '--repo', self.repo, 'gh', 'api', '--method', method, path]
        if body is not None:
            args += ['--input', '-']
        try:
            r = self.run(args, input=json.dumps(body) if body is not None else '', capture_output=True, text=True,
                         timeout=self.timeout, env=self.env)
        except (OSError, ValueError, subprocess.SubprocessError) as e:
            raise ApiError(f'{self.path}: {type(e).__name__}: {e}') from None
        if r.returncode != 0:
            err = (r.stderr or r.stdout or '').strip()
            m = re.search(r'\bHTTP (\d{3})\b', err)
            raise ApiError(f'{method} {path.split("?")[0]}: exit {r.returncode}: {err[-300:]}',
                           'rate limit' in err.lower(), int(m.group(1)) if m else None)
        if len(r.stdout or '') > MAX_REPLY:
            raise ApiError(f'{method} {path}: an answer larger than {MAX_REPLY} bytes')
        try:
            return json.loads(r.stdout) if (r.stdout or '').strip() else None
        except ValueError:
            raise ApiError(f'{method} {path}: not JSON') from None

    def graphql(self, query, variables):
        got = self.api('POST', 'graphql', {'query': query, 'variables': variables})
        if not isinstance(got, dict) or got.get('errors') or not isinstance(got.get('data'), dict):
            raise ApiError(f'GraphQL: {str((got or {}).get("errors"))[:300] if isinstance(got, dict) else "no data"}')
        return got['data']

    def file_at(self, path, sha):
        """The file's text at the commit, or None when the commit has no such file (an older branch)."""
        try:
            got = self.api('GET', f'repos/{self.repo}/contents/{urllib.parse.quote(path)}?ref={sha}')
        except ApiError as e:
            if e.status == 404:
                return None
            raise
        if not isinstance(got, dict) or got.get('encoding') != 'base64' or not isinstance(got.get('content'), str):
            raise ApiError(f'{path} at {sha[:12]}: not a file')
        try:
            return base64.b64decode(got['content']).decode('utf-8')
        except ValueError:
            raise ApiError(f'{path} at {sha[:12]}: not UTF-8') from None

    def our_checks(self, sha, name, app_id):
        """The check runs of this name by our App on the commit (GitHub's record: the lane's lasting state)."""
        q = urllib.parse.urlencode({'check_name': name, 'app_id': app_id, 'filter': 'all', 'per_page': 20})
        got = self.api('GET', f'repos/{self.repo}/commits/{sha}/check-runs?{q}')
        runs = got.get('check_runs') if isinstance(got, dict) else None
        if not isinstance(runs, list):
            raise ApiError(f'check runs of {sha[:12]}: no list')
        return [r for r in runs if isinstance(r, dict) and (r.get('app') or {}).get('id') == app_id
                and r.get('name') == name and r.get('head_sha') == sha]

    def create_check(self, body):
        got = self.api('POST', f'repos/{self.repo}/check-runs', body)
        if not isinstance(got, dict) or type(got.get('id')) is not int:
            raise ApiError('check run: no id in the answer')
        return got['id']

    def update_check(self, check_id, body):
        self.api('PATCH', f'repos/{self.repo}/check-runs/{int(check_id)}', body)

    def actions_runs(self, sha):
        got = self.api('GET', f'repos/{self.repo}/actions/runs?head_sha={sha}&per_page=30')
        runs = got.get('workflow_runs') if isinstance(got, dict) else None
        if not isinstance(runs, list):
            raise ApiError(f'runs of {sha[:12]}: no list')
        return [r for r in runs if isinstance(r, dict)]

    def run_jobs(self, run_id):
        got = self.api('GET', f'repos/{self.repo}/actions/runs/{int(run_id)}/jobs?per_page=100')
        jobs = got.get('jobs') if isinstance(got, dict) else None
        if not isinstance(jobs, list):
            raise ApiError(f'jobs of run {run_id}: no list')
        return [j for j in jobs if isinstance(j, dict)]
