#!/usr/bin/env python3
"""Did fork-checks.yml run on this pull request's head? (issue #401, run by fork-tests.yml's fork_checks_ran job)

fork-checks.yml runs on pull_request_target, which GitHub blocks from 2026-12-02 unless a repository Actions policy
allows it for that file (.github/fork-actions-policies.json). A blocked run shows only as a failed run that no ruleset
requires, so the schema and upstream-file checks would stop without anyone noticing. This script makes the required
fork-ci-result notice: on the head commit, a check run of the configured name (`check`, or `outside_check` for a pull
request from another fork) must exist as a job of a run of the configured workflow file (a job is a check run; the
Actions API needs only actions: read, which every caller of fork-tests.yml grants). Any state counts (queued, running, failed: exists = ran; failing is fork-checks' own job). Only skipped check runs fail at once.

It polls every poll_seconds for at most wait_seconds, then fails with "fork-checks did not run: check the Actions
policy (#401)". An API error fails closed: a rate limit or a server error is retried within the same cap (and named in
the failure if it lasts), any other error (403, 404 ...) fails at once. Not a pull_request event: nothing to check.

Environment: EVENT_NAME, HEAD_SHA, OUTSIDE_PR (true/false), GITHUB_TOKEN (actions: read),
GITHUB_REPOSITORY, GITHUB_API_URL. Test: fork-checks-ran.test.py.
"""
import argparse, http.client, json, os, re, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, '..', 'fork-actions-policies.json')
SHA = re.compile(r'[0-9a-f]{40}')
NOT_RAN = 'fork-checks did not run: check the Actions policy (#401)'


class Transient(Exception):
    """Worth retrying: a server error, a dropped connection, a garbled reply."""


class RateLimited(Transient):
    def __init__(self, msg, reset):
        super().__init__(msg)
        self.reset = reset   # epoch seconds when a retry may work


class Fatal(Exception):
    """Retrying cannot help (no permission, not found, bad request): fail now."""


class GitHub:
    def __init__(self, token, repo, api):
        self.token, self.repo, self.api = token, repo, api.rstrip('/')

    def get(self, path):
        req = urllib.request.Request(f'{self.api}/repos/{self.repo}/{path}', headers={
            'Accept': 'application/vnd.github+json', 'User-Agent': 'fork-checks-ran',
            'Authorization': f'Bearer {self.token}'})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get('message', '')
            except (ValueError, AttributeError, OSError, http.client.HTTPException):
                msg = ''
            what = f'HTTP {e.code} on {path.split("?")[0]}: {msg}'.rstrip(': ')
            h = e.headers
            if e.code == 429 or (e.code == 403 and (h.get('X-RateLimit-Remaining') == '0' or h.get('Retry-After')
                                                    or 'rate limit' in msg.lower())):
                if h.get('Retry-After', '').isdigit():
                    reset = time.time() + int(h['Retry-After'])
                elif h.get('X-RateLimit-Reset', '').isdigit():
                    reset = float(h['X-RateLimit-Reset'])
                else:
                    reset = time.time() + 60
                raise RateLimited(f'GitHub API rate limit ({what})', reset) from None
            if e.code >= 500:
                raise Transient(what) from None
            raise Fatal(what) from None
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            raise Transient(f'{type(e).__name__} on {path.split("?")[0]}: {e}') from None


def config_problems(cfg):
    problems = [f'fork_checks.{k} is not a non-empty string' for k in ('workflow', 'check', 'outside_check')
                if not isinstance(cfg.get(k), str) or not cfg.get(k)]
    for k in ('wait_seconds', 'poll_seconds'):
        if not isinstance(cfg.get(k), int) or isinstance(cfg.get(k), bool) or cfg.get(k) <= 0:
            problems.append(f'fork_checks.{k} is not a positive whole number')
    return problems


def look(api, sha, name, workflow):
    """'ran', 'skipped' (only skipped jobs of that name) or None: the jobs (= check runs) of the workflow's runs on sha."""
    runs = api.get(f'actions/workflows/{urllib.parse.quote(os.path.basename(workflow))}/runs?'
                   + urllib.parse.urlencode({'head_sha': sha, 'per_page': 100}))['workflow_runs']
    mine = []
    for run in runs:
        if run['path'].split('@')[0] != workflow:
            continue
        jobs = api.get(f'actions/runs/{int(run["id"])}/jobs?filter=all&per_page=100')['jobs']
        mine += [j for j in jobs if j['name'] == name]
        if any(not (j['status'] == 'completed' and j['conclusion'] == 'skipped') for j in mine):
            return 'ran'
    return 'skipped' if mine else None


def check(env, cfg, api, now=time.time, sleep=time.sleep, say=print):
    event = env.get('EVENT_NAME', '')
    if event != 'pull_request':
        say(f'not a pull request ({event or "no event"}): fork-checks runs only on pull requests; nothing to check')
        return 0
    problems = config_problems(cfg)
    sha, outside = env.get('HEAD_SHA', ''), env.get('OUTSIDE_PR', '')
    if not SHA.fullmatch(sha):
        problems.append(f'HEAD_SHA {sha!r} is not a commit id')
    if outside not in ('true', 'false'):
        problems.append(f'OUTSIDE_PR {outside!r} is not true or false')
    if problems:
        say('::error::' + '; '.join(problems))
        return 1
    name = cfg['outside_check'] if outside == 'true' else cfg['check']
    workflow, poll = cfg['workflow'], cfg['poll_seconds']
    deadline = now() + cfg['wait_seconds']
    say(f'Looking for check run "{name}" of {workflow} on {sha} (at most {cfg["wait_seconds"]} s).')
    last_error = None
    while True:
        wait = poll
        try:
            found = look(api, sha, name, workflow)
            last_error = None
        except Fatal as e:
            say(f'::error::fork-checks could not be confirmed (failing closed): GitHub API error: {e}. A 404: '
                f'{workflow} renamed or gone? Then update .github/fork-actions-policies.json and the Actions policy (#401)')
            return 1
        except Transient as e:
            found, last_error = None, str(e)
            say(f'::warning::{e}; retrying')
            if isinstance(e, RateLimited):
                wait = max(poll, e.reset - now())
        except (KeyError, TypeError, IndexError, AttributeError) as e:
            say(f'::error::fork-checks could not be confirmed (failing closed): unexpected GitHub reply ({e!r})')
            return 1
        if found == 'ran':
            say(f'ok: fork-checks ran on {sha} (check run "{name}" of {workflow})')
            return 0
        if found == 'skipped':
            say(f'::error::{NOT_RAN}: its check run "{name}" on {sha} was skipped, so nothing was checked')
            return 1
        left = deadline - now()
        if wait > left:
            if last_error:
                say(f'::error::fork-checks could not be confirmed (failing closed) within {cfg["wait_seconds"]} s: '
                    f'the last GitHub API call failed: {last_error}')
            else:
                say(f'::error::{NOT_RAN}: no check run "{name}" of {workflow} on {sha} after {cfg["wait_seconds"]} s')
            return 1
        sleep(wait)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n', 1)[0])
    ap.add_argument('--config', default=CONFIG)
    args = ap.parse_args(argv)
    env = os.environ
    try:
        with open(args.config) as f:
            cfg = json.load(f).get('fork_checks') or {}
    except (OSError, ValueError, AttributeError) as e:
        print(f'::error::cannot read {args.config}: {e}')
        return 1
    api = GitHub(env.get('GITHUB_TOKEN', ''), env.get('GITHUB_REPOSITORY', ''),
                 env.get('GITHUB_API_URL') or 'https://api.github.com')
    return check(env, cfg, api, say=lambda m: print(m, flush=True))


if __name__ == '__main__':
    sys.exit(main())
