#!/usr/bin/env python3
"""Did fork-checks.yml pass on this pull request's head? (issues #401 and #430, run by fork-tests.yml's fork_checks_ran job)

fork-checks.yml runs on pull_request_target, which GitHub blocks from 2026-12-02 unless a repository Actions policy
allows it for that file (.github/fork-actions-policies.json). Its checks are not required, so a blocked, failed or
cancelled run would merge unseen. This script makes the required fork-ci-result notice: on the head commit, the newest
pull_request_target run of the configured workflow file that belongs to THIS pull request must have a job (= check run)
of the configured name (`check`) whose latest attempt concluded success. Runs of other events do not count: they run
the pull request's own copy of the file. A run belongs to this pull request when its head repository is this
repository and its pull_requests list holds PR_NUMBER (#433) with that entry's head branch and head repository the
run's own (#443): anyone can open a pull request from their own fork at the same public commit (its run skips the
job), and a second pull request at the same commit may target a base branch whose fork-checks.yml is a no-op. GitHub
lists on a run the open pull requests whose head SHA or head branch matches it, so a second pull request from
another branch at the same commit may be listed on this one's runs; its entry names the other branch, so it is not
counted. GitHub leaves pull_requests empty on runs of a pull request from another repository, so such a run is never
counted (fail closed). Two pull requests from the same head branch may both be listed on each other's runs, and so
may a pull request closed and a new one opened from the same branch at the same commit (the old pull request's runs
list the new one, and decide until its own run exists); either needs someone who can push to this repository. It
waits while that job is queued or running (or skipped in a run not yet finished); failed, cancelled or skipped
fails at once, and so does a finished run without the job (a blocked event leaves one). The Actions API needs only
actions: read, which every caller of fork-tests.yml grants.

The config is read from the pull request's base commit (BASE_SHA, fetched when the checkout lacks it), not from the
checkout: pull_request_target runs the base branch's copy of fork-checks.yml, so a pull request that renames its job
or file is judged by the names the base branch's copy produces, and cannot edit the config its own check reads.
Missing or undecodable there: fail.

It polls every poll_seconds for at most wait_seconds, counted from before the config is read (its git calls count),
then fails. The job's timeout leaves room for the checkout, the cap, one last poll and a minute for the job's
set-up: fork-contracts.test.py checks it with GIT_TIMEOUT, GIT_CALLS, API_TIMEOUT and API_CALLS_PER_POLL below, so
the message shows. An API error fails closed: a rate limit or a server error is retried within the same cap (and
named in the failure if it lasts), any other error (403, 404 ...) fails at once. A pull request from another fork
fails at once (the job's if: skips those; fork-checks' only job for them always fails).

Not a pull_request event: a run that another workflow called (GITHUB_WORKFLOW_REF names the caller) has nothing to
check, since its check runs are named "<caller job> / ... / fork-ci-result", never the required "fork-ci-result". A
direct run (push, workflow_dispatch) passes only on the default branch: on any other branch its passing fork-ci-result
would land on that branch's tip, which may be an open pull request's head that fork-checks never passed (#430).

--copy is fork-checks.yml's own check job on a title or body edit: it ends as the newest real check of the head ended
(see copy()), so a skipped job never stands as "passed" next to a red check from a base change (#1134).

Environment: EVENT_NAME, HEAD_SHA, BASE_SHA, PR_NUMBER, OUTSIDE_PR (true/false), DEFAULT_BRANCH, GITHUB_REF,
GITHUB_WORKFLOW_REF, GITHUB_TOKEN (actions: read), GITHUB_REPOSITORY, GITHUB_API_URL. Test: fork-checks-ran.test.py.
"""
import http.client, json, os, re, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

CONFIG_PATH = '.github/fork-actions-policies.json'
TESTS_WORKFLOW = '.github/workflows/fork-tests.yml'   # the workflow whose fork-ci-result a ruleset requires
SHA = re.compile(r'[0-9a-f]{40}')
NUMBER = re.compile(r'[1-9][0-9]*')
GIT_TIMEOUT, GIT_CALLS = 60, 3            # seconds per git call; at most cat-file, fetch, show
API_TIMEOUT, API_CALLS_PER_POLL = 30, 2   # seconds per API call; one poll: the run list, then one run's jobs
# --copy (fork-checks.yml on a title or body edit): its first step's name, and how long it waits for the real check.
COPY_STEP = 'Copy the newest real check (a title or body edit)'
COPY_WAIT_SECONDS = 480
NOT_RAN = 'fork-checks did not run: check the Actions policy (#401)'
WAY_OUT = ('Once that is fixed, push again or close and reopen the pull request (a blocked run cannot be re-run), '
           'then this job runs again.')
RERUN = ('Fix what it reports and push again; if it was cancelled or timed out, re-run it (or push again, or close and '
         'reopen the pull request), then re-run the failed jobs of this run.')
SKIPPED = ('fork-checks skips that job only when it sees the pull request as coming from another repository (or the '
           'base branch\'s fork-checks.yml changed its if:), and a re-run skips it again (it replays the same event). '
           'Check that the head branch is still in this repository and look at the base branch\'s fork-checks.yml, then '
           'push again or close and reopen the pull request. A pull request from another repository needs a maintainer.')


class Transient(Exception):
    """Worth retrying: a server error, a dropped connection, a garbled reply."""


class RateLimited(Transient):
    def __init__(self, msg, reset):
        super().__init__(msg)
        self.reset = reset   # epoch seconds when a retry may work


class Fatal(Exception):
    """Retrying cannot help (no permission, not found, bad request): fail now."""


class ConfigError(Exception):
    """The base commit's config cannot be read."""


class GitHub:
    def __init__(self, token, repo, api):
        self.token, self.repo, self.api = token, repo, api.rstrip('/')

    def get(self, path):
        req = urllib.request.Request(f'{self.api}/repos/{self.repo}/{path}', headers={
            'Accept': 'application/vnd.github+json', 'User-Agent': 'fork-checks-ran',
            'Authorization': f'Bearer {self.token}'})
        try:
            with urllib.request.urlopen(req, timeout=API_TIMEOUT) as r:
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


def base_config(sha, cwd=None, env=None):
    """fork_checks of CONFIG_PATH on commit sha, fetched from origin when the checkout lacks it."""
    def git(*args):
        try:
            return subprocess.run(['git', *args], cwd=cwd, env=env, capture_output=True, timeout=GIT_TIMEOUT)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise ConfigError(f'git {args[0]} failed: {e}') from None

    if git('cat-file', '-e', f'{sha}^{{commit}}').returncode != 0:
        r = git('fetch', '-q', '--no-tags', '--no-recurse-submodules', '--depth=1', '--filter=blob:none', 'origin', sha)
        if r.returncode != 0:
            raise ConfigError(f'cannot fetch the base commit {sha}: {r.stderr.decode(errors="replace").strip()}')
    r = git('show', f'{sha}:{CONFIG_PATH}')
    if r.returncode != 0:
        raise ConfigError(f'no {CONFIG_PATH} on the base commit {sha} ({r.stderr.decode(errors="replace").strip()})')
    try:
        cfg = json.loads(r.stdout.decode())['fork_checks']
        if not isinstance(cfg, dict):
            raise TypeError('fork_checks is not an object')
    except (ValueError, KeyError, TypeError) as e:
        raise ConfigError(f'cannot read {CONFIG_PATH} on the base commit {sha}: {e!r}') from None
    return cfg


def config_problems(cfg):
    problems = [f'fork_checks.{k} is not a non-empty string' for k in ('workflow', 'check')
                if not isinstance(cfg.get(k), str) or not cfg.get(k)]
    for k in ('wait_seconds', 'poll_seconds'):
        if not isinstance(cfg.get(k), int) or isinstance(cfg.get(k), bool) or cfg.get(k) <= 0:
            problems.append(f'fork_checks.{k} is not a positive whole number')
    return problems


def of_this_pull_request(run, repo, number):
    """A run from this repository whose pull_requests list holds this pull request from the run's own head branch
    and head repository. GitHub lists the open pull requests whose head SHA or head branch matches the run, so a
    second pull request at the same commit can be listed on this one's runs (#443): its entry is counted only if its
    head.ref is the run's head_branch and its head.repo.id is the run's head repository's id (each entry's
    head.repo has an id, a short name and an API url, no full_name). An empty or missing list (what GitHub gives a
    pull request from another repository), or a missing field on either side, is never counted."""
    head, prs, branch = run.get('head_repository'), run.get('pull_requests'), run.get('head_branch')
    if not (isinstance(head, dict) and head.get('full_name') == repo and isinstance(prs, list)):
        return False
    repo_id = head.get('id')
    if not (isinstance(branch, str) and branch and type(repo_id) is int):   # not bool, not None
        return False

    def from_this_run(p):
        h = p.get('head') if isinstance(p, dict) else None
        r = h.get('repo') if isinstance(h, dict) else None
        return isinstance(r, dict) and h.get('ref') == branch and r.get('id') == repo_id
    return any(isinstance(p, dict) and p.get('number') == number and from_this_run(p) for p in prs)


def copy_kind(job):
    """Is this job of fork-checks.yml a copy (its first step, COPY_STEP, ran: a title or body edit) or a real check
    (that step skipped, or a job with steps but none of that name: a run of an older fork-checks.yml)? None while
    that cannot be told yet: no steps listed, or the step still queued (a real job lists it queued too)."""
    steps = job.get('steps')
    if not isinstance(steps, list) or not steps:
        return None
    named = [s for s in steps if isinstance(s, dict) and s.get('name') == COPY_STEP]
    if not named:
        return 'real'
    s = named[0]
    if s.get('status') == 'completed':
        return 'real' if s.get('conclusion') == 'skipped' else 'copy'
    return 'copy' if s.get('status') == 'in_progress' else None


def look(api, sha, name, workflow, repo, number, before=None, real_only=False):
    """What the newest pull_request_target run of the workflow on sha for pull request `number` from `repo` says
    about its job `name`, latest attempt: None (no such run yet), ('going', url), ('blocked', run url: finished
    without the job) or (conclusion, url). before: only runs with a smaller id (made earlier). real_only: pass over
    runs whose job is a copy (copy_kind), so a copy copies a real check, never another copy; one API call per run
    read. Without it, at most API_CALLS_PER_POLL calls."""
    runs = api.get(f'actions/workflows/{urllib.parse.quote(os.path.basename(workflow))}/runs?'
                   + urllib.parse.urlencode({'head_sha': sha, 'per_page': 100}))['workflow_runs']
    mine = [r for r in runs if r['path'].split('@')[0] == workflow and r.get('event') == 'pull_request_target'
            and of_this_pull_request(r, repo, number) and (before is None or int(r['id']) < before)]
    # A run that skipped every job checked nothing (an older fork-checks.yml of this PR's skipped its check on a title
    # or body edit), so the newest run that did not end so decides. Only when every run ended so does the newest one
    # decide (and its skipped check fails). The run list says it, so this costs no API call.
    checked = [r for r in mine if not (r.get('status') == 'completed' and r.get('conclusion') == 'skipped')]
    for run in sorted(checked or mine, key=lambda r: int(r['id']), reverse=True):
        jobs = api.get(f'actions/runs/{int(run["id"])}/jobs?filter=latest&per_page=100')['jobs']
        named = [j for j in jobs if j['name'] == name]
        if not named:
            return ('blocked', run.get('html_url', f'run {run["id"]}')) if run['status'] == 'completed' \
                else ('going', run.get('html_url', ''))
        j = named[0]
        if real_only:
            kind = copy_kind(j)
            if kind == 'copy':
                continue
            if kind is None:   # unknown: wait while it runs, fail once it has ended (fail closed)
                if j['status'] != 'completed':
                    return 'going', j.get('html_url', '')
                return f'{j["conclusion"] or "without a conclusion"}, and not clearly a real check', \
                    j.get('html_url', '')
        # A skipped check in a run not yet finished: the run may still end "skipped" and be passed over, so wait.
        if j['status'] != 'completed' or (j['conclusion'] == 'skipped' and run.get('status') != 'completed'):
            return 'going', j.get('html_url', '')
        return j['conclusion'] or 'without a conclusion', j.get('html_url', '')
    return None


def not_a_pull_request(env, say):
    event, ref = env.get('EVENT_NAME', '') or 'no event', env.get('GITHUB_REF', '')
    repo, wf_ref = env.get('GITHUB_REPOSITORY', ''), env.get('GITHUB_WORKFLOW_REF', '')
    if not repo or not wf_ref.startswith(repo + '/') or '@' not in wf_ref:
        say(f'::error::cannot tell which workflow runs this ({event}; GITHUB_WORKFLOW_REF {wf_ref!r}): failing closed')
        return 1
    workflow = wf_ref[len(repo) + 1:].split('@')[0]
    if workflow != TESTS_WORKFLOW:
        say(f'not a pull request ({event}), called by {workflow}: its check runs are named "<caller job> / ... / '
            'fork-ci-result", not the required fork-ci-result; nothing to check')
        return 0
    default = env.get('DEFAULT_BRANCH', '')
    if default and ref == f'refs/heads/{default}':
        say(f'not a pull request ({event}) on {default}: fork-checks runs only on pull requests; nothing to check')
        return 0
    say(f'::error::a {event} run of {TESTS_WORKFLOW} on {ref or "no ref"} puts its fork-ci-result on that commit, '
        f'which may be an open pull request\'s head that fork-checks never passed (#430), so it fails. Run it from '
        f'the default branch ({default or "unknown"}) and name the commit or branch to test: '
        f'dispatch fork-tests.yml on {default or "the default branch"} with ref=<branch>')
    return 1


def check(env, api, config, now=time.time, sleep=time.sleep, say=print):
    """config(base_sha) -> the fork_checks dict (base_config in the workflow); raises ConfigError."""
    event = env.get('EVENT_NAME', '')
    if event != 'pull_request':
        return not_a_pull_request(env, say)
    sha, base, outside = env.get('HEAD_SHA', ''), env.get('BASE_SHA', ''), env.get('OUTSIDE_PR', '')
    number, repo = env.get('PR_NUMBER', ''), env.get('GITHUB_REPOSITORY', '')
    problems = [f'{k} {v!r} is not a commit id' for k, v in (('HEAD_SHA', sha), ('BASE_SHA', base))
                if not SHA.fullmatch(v)]
    if not NUMBER.fullmatch(number):
        problems.append(f'PR_NUMBER {number!r} is not a pull request number')
    if not repo:
        problems.append('GITHUB_REPOSITORY is empty')
    if outside not in ('true', 'false'):
        problems.append(f'OUTSIDE_PR {outside!r} is not true or false')
    if problems:
        say('::error::' + '; '.join(problems))
        return 1
    if outside == 'true':
        say('::error::outside pull request: fork-checks does not check it (its Outside pull request job fails); '
            'a maintainer must review')
        return 1
    start = now()   # the cap counts the git calls that read the config
    try:
        cfg = config(base)
    except ConfigError as e:
        say(f'::error::fork-checks could not be confirmed (failing closed): {e}')
        return 1
    problems = config_problems(cfg)
    if problems:
        say(f'::error::{CONFIG_PATH} on the base commit {base}: ' + '; '.join(problems))
        return 1
    name, workflow, poll = cfg['check'], cfg['workflow'], cfg['poll_seconds']
    deadline = start + cfg['wait_seconds']
    say(f'Waiting for check run "{name}" of {workflow} on {sha} for pull request #{number} to pass (at most '
        f'{cfg["wait_seconds"]} s; config from the base commit {base}).')
    last_error = None
    while True:
        wait = poll
        try:
            found = look(api, sha, name, workflow, repo, int(number))
            last_error = None
        except Fatal as e:
            say(f'::error::fork-checks could not be confirmed (failing closed): GitHub API error: {e}. A 404: '
                f'{workflow} renamed or gone? Then update {CONFIG_PATH} and the Actions policy (#401)')
            return 1
        except Transient as e:
            found, last_error = None, str(e)
            say(f'::warning::{e}; retrying')
            if isinstance(e, RateLimited):
                wait = max(poll, e.reset - now())
        except (KeyError, TypeError, IndexError, AttributeError, ValueError) as e:
            say(f'::error::fork-checks could not be confirmed (failing closed): unexpected GitHub reply ({e!r})')
            return 1
        state, url = found or (None, '')
        if state == 'success':
            say(f'ok: fork-checks passed on {sha} (check run "{name}" of {workflow}: {url})')
            return 0
        if state == 'blocked':
            say(f'::error::{NOT_RAN}: its newest run on {sha} finished without a "{name}" job ({url}). {WAY_OUT}')
            return 1
        if state == 'skipped':
            say(f'::error::fork-checks did not check {sha}: its check run "{name}" was skipped ({url}). {SKIPPED}')
            return 1
        if state not in (None, 'going'):
            say(f'::error::fork-checks did not pass on {sha}: check run "{name}" of {workflow} ended {state} '
                f'({url}). {RERUN}')
            return 1
        left = deadline - now()
        if wait > left:
            if last_error:
                say(f'::error::fork-checks could not be confirmed (failing closed) within {cfg["wait_seconds"]} s: '
                    f'the last GitHub API call failed: {last_error}. Re-run the failed jobs of this run.')
            elif state == 'going':
                say(f'::error::fork-checks did not finish on {sha} within {cfg["wait_seconds"]} s (check run "{name}" '
                    f'of {workflow}: {url}). Once it has passed, re-run the failed jobs of this run.')
            else:
                say(f'::error::{NOT_RAN}: no pull_request_target run of {workflow} for pull request #{number} from '
                    f'this repository with a check run "{name}" on {sha} after {cfg["wait_seconds"]} s (runs of other '
                    f'pull requests on the same commit do not count). {WAY_OUT}')
            return 1
        sleep(wait)


def copy(env, api, config, now=time.time, sleep=time.sleep, say=print):
    """--copy: fork-checks.yml's check job on a title or body edit (base unchanged). The head is unchanged, so instead
    of checking the same thing again this job ends as the newest earlier run of this pull request on the head that
    really checked (not a copy, look's real_only) ended: success passes, anything else fails, and so does no such
    run (missing) or a job that cannot be told apart (unknown). It waits while that check runs, at most
    COPY_WAIT_SECONDS. A skipped check job would count as passed next to a red one from a base change (#1134), so the
    job always runs. config(sha) reads fork_checks from the default branch's commit (DEFAULT_SHA), whose
    fork-checks.yml runs this.
    Environment: HEAD_SHA, DEFAULT_SHA, PR_NUMBER, GITHUB_RUN_ID, GITHUB_REPOSITORY, GITHUB_TOKEN, GITHUB_API_URL."""
    sha, default, number = env.get('HEAD_SHA', ''), env.get('DEFAULT_SHA', ''), env.get('PR_NUMBER', '')
    run_id, repo = env.get('GITHUB_RUN_ID', ''), env.get('GITHUB_REPOSITORY', '')
    problems = [f'{k} {v!r} is not a commit id' for k, v in (('HEAD_SHA', sha), ('DEFAULT_SHA', default))
                if not SHA.fullmatch(v)]
    problems += [f'{k} {v!r} is not a number' for k, v in (('PR_NUMBER', number), ('GITHUB_RUN_ID', run_id))
                 if not NUMBER.fullmatch(v)]
    if not repo:
        problems.append('GITHUB_REPOSITORY is empty')
    if problems:
        say('::error::' + '; '.join(problems))
        return 1
    start = now()
    try:
        cfg = config(default)
    except ConfigError as e:
        say(f'::error::nothing to copy (failing closed): {e}')
        return 1
    problems = config_problems(cfg)
    if problems:
        say(f'::error::{CONFIG_PATH} on {default}: ' + '; '.join(problems))
        return 1
    name, workflow, poll = cfg['check'], cfg['workflow'], cfg['poll_seconds']
    deadline = start + COPY_WAIT_SECONDS
    way_out = ('Push again or close and reopen the pull request (that checks it again), or re-run the real check '
               'and then this job.')
    say(f'A title or body edit: the head {sha} is unchanged, so this copies the result of the newest earlier run of '
        f'{workflow} for pull request #{number} that checked it (at most {COPY_WAIT_SECONDS} s).')
    last_error = None
    while True:
        wait = poll
        try:
            found = look(api, sha, name, workflow, repo, int(number), before=int(run_id), real_only=True)
            last_error = None
        except Fatal as e:
            say(f'::error::nothing to copy (failing closed): GitHub API error: {e}')
            return 1
        except Transient as e:
            found, last_error = ('going', ''), str(e)
            say(f'::warning::{e}; retrying')
            if isinstance(e, RateLimited):
                wait = max(poll, e.reset - now())
        except (KeyError, TypeError, IndexError, AttributeError, ValueError) as e:
            say(f'::error::nothing to copy (failing closed): unexpected GitHub reply ({e!r})')
            return 1
        if found is None:
            say(f'::error::no earlier run of {workflow} for pull request #{number} checked {sha}: nothing to copy, '
                f'so this fails. {way_out}')
            return 1
        state, url = found
        if state == 'success':
            say(f'ok: copied: the real check of {sha} passed ({url})')
            return 0
        if state != 'going':
            say(f'::error::copied: the real check of {sha} ended {state} ({url}). {way_out}')
            return 1
        if wait > deadline - now():
            what = f'the last GitHub API call failed: {last_error}' if last_error else f'it is still running ({url})'
            say(f'::error::the real check of {sha} did not finish within {COPY_WAIT_SECONDS} s ({what}). '
                'Once it has ended, re-run this job.')
            return 1
        sleep(wait)


def main():
    env = os.environ
    api = GitHub(env.get('GITHUB_TOKEN', ''), env.get('GITHUB_REPOSITORY', ''),
                 env.get('GITHUB_API_URL') or 'https://api.github.com')
    say = lambda m: print(m, flush=True)
    if sys.argv[1:] == ['--copy']:
        return copy(env, api, base_config, say=say)
    if sys.argv[1:]:
        say(f'::error::unknown arguments {sys.argv[1:]!r} (only --copy)')
        return 1
    return check(env, api, base_config, say=say)


if __name__ == '__main__':
    sys.exit(main())
