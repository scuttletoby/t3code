#!/usr/bin/env python3
"""Fork only: where fork-tests.yml's Linux jobs run, the repository's own fleet of self-hosted runners or GitHub-hosted.

Every fact about the repository and its fleet is in one file, .github/fork-fleet.json (CONFIG); none is here. Runbook,
owner steps, the design and the failure table: .github/fork-fleet.md.

  python3 -I fork-fleet.py route     the last step of fork-tests.yml's `changes` job (GitHub-hosted). Writes to
                                     $GITHUB_OUTPUT: runners=<JSON {job key: runs-on}>, mode, attempt, watch.
                                     A job key is the job id, or <id>-<shard> for a sharded job.
  python3 -I fork-fleet.py watch     fork-tests.yml's `fleet_watch` job (GitHub-hosted, actions: write): a fleet job of
                                     its run that waits too long for a runner cancels the run with an error.
  python3 -I fork-fleet.py sweep     fork-fleet.yml every 10 minutes (GitHub-hosted, actions: write): in auto and github
                                     mode, a trusted run that the fleet did not finish is re-run once, GitHub-hosted;
                                     in fleet mode, a run whose fleet job waited 2 hours is cancelled with an error.
  python3 -I fork-fleet.py dispatch  a fleet host's dispatcher, as root, from an installed copy of main's script and
                                     CONFIG: starts a one-job runner for each queued fleet job of a trusted run.
  python3 -I fork-fleet.py gate      every fleet runner's job-started hook, from the same installed copy: a job that
                                     breaks the rule has its Runner.Worker killed before the job's first step.

The setting, repository variable CONFIG["variable"] (route and sweep read it from $FLEET_MODE):
  auto (also unset)  a job goes to a pool whose dispatcher reports a free slot, GitHub-hosted otherwise. Anything
                     unclear (no heartbeat, an old or unreadable one) is GitHub-hosted with a note: auto never makes a
                     run red.
  fleet              every job that runs goes to the fleet, pools with a free slot first, the rest queue on the pool
                     marked `waits`. A job that only prints a skip stays GitHub-hosted. Untrusted runs go hosted.
  github             every job GitHub-hosted, as before the fleet.

No fleet runner waits for work. The dispatcher makes a one-job (JIT) runner for one queued job at a time, and only when:
the job asks for exactly a pool's label plus the per-run random label `route` made for it; GitHub's own record of the
run passes the trust rule (`refused`: a workflow in trusted.workflows of this repository, started and triggered by a
trusted actor, as a push to a trusted branch or a pull request whose head and base are this repository and which a
trusted actor opened); and no other queued job could take that runner. A job that names a fleet label by itself (a
changed workflow, a pull request from a fork) finds no runner. The job-started hook (`gate`) is the second line: it
refuses any job but the run and attempt its runner was made for, or one that breaks the rule, by killing the job's
Runner.Worker, so no later step runs, `if: always()` ones included.
Standard library only: a fleet host copies this file and CONFIG, nothing else.
"""
import argparse, calendar, http.client, json, os, re, secrets, signal, subprocess, sys, time
import urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), 'fork-fleet.json')
MODES = ('auto', 'fleet', 'github')
API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
# Labels a self-hosted runner may get without asking, or a job may ask for without naming a pool. A pool's label is none
# of them, and the dispatcher counts all of them as its runner's own when it checks that no other job could take it.
GENERIC = frozenset(('self-hosted', 'linux', 'x64', 'arm64', 'arm', 'windows', 'macos'))
LABEL = re.compile(r'[a-z0-9][a-z0-9-]{0,62}')
VARIABLE = re.compile(r'(?!GITHUB_)[A-Z][A-Z0-9_]{0,99}')
JOB_ID = re.compile(r'[a-z0-9_]{1,40}')
NONCE = re.compile(r'(?P<key>[a-z0-9_]{1,40}(?:-[0-9]{1,3})?)-[0-9a-f]{16}')
JIT = re.compile(r'[A-Za-z0-9+/=]{1,65536}')
WINDOW = 24 * 3600     # the sweep looks at runs created this long ago at most
CANCEL_WAIT = 120      # seconds the sweep watches a run it cancelled, to re-run it in the same pass
SHOW_UP = 120          # seconds the watch waits for every routed fleet job to be listed (a skipped one never is)
FAILED = ('failure', 'timed_out')
MAX_TRIES = 3          # runners the dispatcher starts for one job before it leaves the job to the watch and the sweep
RUNNER_GROUP = 1       # the repository's default runner group
WORKER = 'Runner.Worker'


def say(msg):
    print(msg, flush=True)


def load(path=CONFIG):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    problems = check_config(cfg)
    if problems:
        raise ValueError(f'{path}: ' + '; '.join(problems))
    return cfg


def positive(x):
    return isinstance(x, int) and not isinstance(x, bool) and x > 0


def check_config(cfg):
    out = []
    for key in ('repository', 'variable', 'hosted', 'trusted', 'pools', 'jobs', 'stuck_seconds', 'fleet_stuck_seconds',
                'beacon_fresh_seconds', 'dispatch'):
        if key not in cfg:
            out.append(f'no {key!r}')
    if out:
        return out
    for key in ('stuck_seconds', 'fleet_stuck_seconds', 'beacon_fresh_seconds'):
        if not positive(cfg[key]):
            out.append(f'{key} must be a positive whole number')
    for key in ('every_seconds', 'beacon_seconds', 'idle_passes'):
        if not positive((cfg['dispatch'] or {}).get(key)):
            out.append(f'dispatch.{key} must be a positive whole number')
    pools = cfg['pools']
    for field in ('name', 'label', 'beacon'):
        got = [p.get(field) for p in pools]
        if len(set(map(str, got))) != len(got):
            out.append(f'two pools with one {field}')
    for p in pools:
        who = f'pool {p.get("name")!r}'
        if not (isinstance(p.get('label'), str) and LABEL.fullmatch(p['label']) and p['label'] not in GENERIC):
            out.append(f'{who}: label must be one label of its own (a-z, 0-9, -), none of {sorted(GENERIC)}')
        if not positive(p.get('slots')):
            out.append(f'{who}: slots must be a positive whole number')
        if not (isinstance(p.get('beacon'), str) and VARIABLE.fullmatch(p['beacon'])):
            out.append(f'{who}: beacon must be a repository variable name (A-Z, 0-9, _; not GITHUB_)')
        launch = p.get('launch')
        if not (isinstance(launch, list) and launch and all(isinstance(x, str) and x for x in launch)):
            out.append(f'{who}: launch must be a command list')
        if not isinstance(p.get('waits'), bool):
            out.append(f'{who}: waits must be true or false')
    if sum(1 for p in pools if p.get('waits') is True) != 1:
        out.append('exactly one pool must have "waits": true (where fleet mode queues)')
    for j in cfg['jobs']:
        if not (isinstance(j.get('job'), str) and JOB_ID.fullmatch(j['job'])):
            out.append(f'job {j.get("job")!r}: the id must be a-z, 0-9 and _')
        if not positive(j.get('memory_mb')):
            out.append(f'job {j.get("job")!r}: memory_mb must be a positive number')
        if j.get('shards') is not None and not (positive(j['shards']) and j['shards'] < 1000):
            out.append(f'job {j.get("job")!r}: shards must be 1 to 999')
    if out:
        return out
    keys = [k for k, _ in job_keys(cfg)]
    if len(set(keys)) != len(keys):
        out.append('a job key twice')
    return out


def job_keys(cfg):
    """[(key, job entry)] in CONFIG's order (its preference: the longest jobs first)."""
    out = []
    for j in cfg['jobs']:
        n = j.get('shards')
        out += [(f'{j["job"]}-{i}', j) for i in range(1, n + 1)] if n else [(j['job'], j)]
    return out


def mode_of(value):
    """The setting as one of MODES; ValueError for anything else (a typo must not quietly mean auto)."""
    got = (value or '').strip().lower() or 'auto'
    if got not in MODES:
        raise ValueError(f'the repository variable is {value!r}, not one of {", ".join(MODES)} (or unset: auto)')
    return got


def stamp(text):
    return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')) if text else None


def fleet_labels(cfg):
    return {p['label'] for p in cfg['pools']}


def is_fleet(cfg, job):
    return bool(set(job.get('labels') or ()) & fleet_labels(cfg))


# ---------------------------------------------------------------- the rule: route, dispatcher, sweep and the hook

def refused(cfg, env, event):
    """Why this run may NOT use the fleet, or None when it may. env: the GITHUB_* variables; event: the payload."""
    repo, trusted = cfg['repository'], cfg['trusted']
    if env.get('GITHUB_REPOSITORY') != repo:
        return f'repository {env.get("GITHUB_REPOSITORY")!r}, not {repo}'
    ref = env.get('GITHUB_WORKFLOW_REF') or ''
    if not any(ref.startswith(f'{repo}/{w}@') for w in trusted['workflows']):
        return f'workflow {ref!r} is not one of {trusted["workflows"]}'
    for name in ('GITHUB_ACTOR', 'GITHUB_TRIGGERING_ACTOR'):
        if env.get(name) not in trusted['actors']:
            return f'{name} is {env.get(name)!r}, not a trusted actor'
    if not isinstance(event, dict):
        return 'no event payload'
    kind = env.get('GITHUB_EVENT_NAME')
    if kind == 'push':
        branches = [f'refs/heads/{b}' for b in trusted['push_branches']]
        if env.get('GITHUB_REF') not in branches:
            return f'a push to {env.get("GITHUB_REF")!r}'
        if (event.get('repository') or {}).get('full_name') != repo:
            return 'a push to another repository'
        return None
    if kind != 'pull_request':
        return f'a {kind!r} event'
    pr = event.get('pull_request') or {}
    for side in ('head', 'base'):
        got = ((pr.get(side) or {}).get('repo') or {}).get('full_name')
        if got != repo:
            return f'a pull request whose {side} is {got!r}'
    if (pr.get('user') or {}).get('login') not in trusted['actors']:
        return f'a pull request opened by {(pr.get("user") or {}).get("login")!r}'
    return None


def facts_of_run(run):
    """The GITHUB_* variables `refused` reads, from GitHub's own record of a workflow run (never from a job)."""
    repo = (run.get('repository') or {}).get('full_name')
    path = (run.get('path') or '').split('@')[0]
    return {'GITHUB_REPOSITORY': repo, 'GITHUB_WORKFLOW_REF': f'{repo}/{path}@refs/heads/{run.get("head_branch")}',
            'GITHUB_ACTOR': (run.get('actor') or {}).get('login'),
            'GITHUB_TRIGGERING_ACTOR': (run.get('triggering_actor') or {}).get('login'),
            'GITHUB_EVENT_NAME': run.get('event'), 'GITHUB_REF': f'refs/heads/{run.get("head_branch")}'}


def run_refused(cfg, gh, run):
    """`refused` for a run as GitHub records it. A pull request run must list its pull requests (GitHub lists none for
    one from a fork); each is read from pulls/<n>, and its head must be the run's head repository."""
    env = facts_of_run(run)
    if env['GITHUB_EVENT_NAME'] != 'pull_request':
        return refused(cfg, env, {'repository': run.get('repository') or {}})
    prs = run.get('pull_requests') or []
    if not prs:
        return 'a pull request run with no pull request of this repository (one from a fork)'
    head = (run.get('head_repository') or {}).get('full_name')
    for p in prs:
        pull = gh.call(f'pulls/{int(p["number"])}')
        got = ((pull.get('head') or {}).get('repo') or {}).get('full_name')
        if got != head:
            return f'pull request #{p["number"]} has head {got!r}, the run {head!r}'
        why = refused(cfg, env, {'pull_request': {'head': {'repo': {'full_name': head}}, 'base': pull.get('base'),
                                                  'user': pull.get('user')}})
        if why:
            return why
    return None


def read_event(env):
    path = env.get('GITHUB_EVENT_PATH')
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f), None
    except (TypeError, OSError, ValueError) as e:
        return None, f'the event file {path!r} cannot be read ({e.__class__.__name__})'


def made_for(env):
    """Why this job is not the one its runner was made for, or None. The dispatcher's launcher puts FORK_FLEET_RUN and
    FORK_FLEET_ATTEMPT in the runner's environment from a root-only file; a workflow cannot set the hook's environment."""
    want = (env.get('FORK_FLEET_RUN'), env.get('FORK_FLEET_ATTEMPT'))
    got = (env.get('GITHUB_RUN_ID'), env.get('GITHUB_RUN_ATTEMPT'))
    if not all(want):
        return 'this runner was not started by the dispatcher (no FORK_FLEET_RUN in its environment)'
    if want != got:
        return f'this runner was made for run {want[0]} attempt {want[1]}, not run {got[0]} attempt {got[1]}'
    return None


def worker_above(pid=None, proc='/proc'):
    """The pid of the nearest Runner.Worker among this process's ancestors, or None."""
    pid = os.getppid() if pid is None else pid
    for _ in range(64):
        if pid <= 1:
            return None
        try:
            with open(f'{proc}/{pid}/stat', encoding='utf-8', errors='replace') as f:
                stat = f.read()
        except OSError:
            return None
        comm, rest = stat[stat.index('(') + 1:stat.rindex(')')], stat[stat.rindex(')') + 2:].split()
        if comm == WORKER:
            return pid
        pid = int(rest[1])
    return None


def stop_job(why, kill=os.kill, find=worker_above, pause=time.sleep):
    """Refuse the job for good: an exit code alone would let the job's `if: always()` steps run."""
    say(f'::error::fork-fleet: this job may not run on a fleet runner: {why} (.github/fork-fleet.md)')
    pid = find()
    if pid is None:
        say(f'::error::fork-fleet: no {WORKER} above this hook, so the job cannot be stopped: failing the hook')
        return 1
    pause(2)   # the worker waits for this hook, so nothing of the job runs meanwhile; the log line above gets out
    kill(pid, signal.SIGKILL)
    return 1


def gate(cfg, env, stop=stop_job):
    event, why = read_event(env)
    why = why or made_for(env) or refused(cfg, env, event)
    if why:
        return stop(why)
    say(f'fork-fleet: allowed (run {env.get("GITHUB_RUN_ID")} attempt {env.get("GITHUB_RUN_ATTEMPT")}, '
        f'{env.get("GITHUB_EVENT_NAME")} {env.get("GITHUB_REF")})')
    return 0


# ---------------------------------------------------------------- GitHub

class GitHub:
    def __init__(self, token, repo):
        self.token, self.repo = token, repo

    def call(self, path, method='GET', data=None):
        """path: below /repos/<repo>/."""
        if not self.token:
            raise ValueError(f'no token for {path}')
        body = None if data is None else json.dumps(data).encode()
        req = urllib.request.Request(f'{API}/repos/{self.repo}/{path}', data=body, method=method,
                                     headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'fork-fleet',
                                              'Authorization': f'Bearer {self.token}'})
        with urllib.request.urlopen(req, timeout=30) as r:
            got = r.read()
        return json.loads(got) if got else {}

    def pages(self, path, key, most=10):
        out = []
        for page in range(1, most + 1):
            got = self.call(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')[key]
            out += got
            if len(got) < 100:
                return out
        return out


# A truncated reply is http.client.IncompleteRead (an HTTPException), a garbled one a ValueError.
TROUBLE = (urllib.error.URLError, http.client.HTTPException, OSError, ValueError, KeyError, TypeError)


# ---------------------------------------------------------------- route

def runs(job, gates):
    """Does this job run for this change? gates: the `changes` job's outputs (None when they are missing)."""
    if not job.get('gates') or gates is None:
        return True
    return any(str(gates.get(g)) != 'false' for g in job['gates'])


def beacon_free(cfg, pool, env, now):
    """(free slots its dispatcher last reported, why none are counted) from the pool's heartbeat variable."""
    raw = env.get(pool['beacon']) or ''
    if not raw:
        return 0, f'no heartbeat in {pool["beacon"]} (no dispatcher has run)'
    try:
        got = json.loads(raw)
        at, free = got['at'], got['free']
        if isinstance(at, bool) or not isinstance(at, (int, float)) or isinstance(free, bool) or not isinstance(free, int):
            raise ValueError(raw)
    except (ValueError, TypeError, KeyError):
        return 0, f'its heartbeat {pool["beacon"]} cannot be read'
    age = now - at
    if age > cfg['beacon_fresh_seconds'] or age < -60:
        return 0, f'its dispatcher\'s last heartbeat is {int(age)} s old'
    return max(free, 0), None


def route(cfg, env, event, mode_value, gates, now=time.time, nonce=None):
    """(mode, {key: runs-on}). Raises only for a broken setting or CONFIG; it calls no API."""
    mode = mode_of(mode_value)
    hosted = cfg['hosted']
    out = {key: hosted for key, _ in job_keys(cfg)}
    if mode == 'github':
        say('GitHub-hosted for every job: the setting is github')
        return mode, out
    why = refused(cfg, env, event)
    if why:
        say(f'::notice::fork-fleet: GitHub-hosted for every job: {why} (only trusted runs may use the fleet)')
        return mode, out
    wanted = [key for key, job in job_keys(cfg) if runs(job, gates)]
    if not wanted:
        say('GitHub-hosted for every job: no routed job runs for this change')
        return mode, out
    nonce = nonce or secrets.token_hex(8)   # this run's own label: no runner exists for it until the dispatcher makes one

    def labels(pool, key):
        return [pool['label'], f'{pool["label"]}-{key}-{nonce}']
    todo, t = list(wanted), now()
    for pool in cfg['pools']:
        free, why = beacon_free(cfg, pool, env, t)
        if why:
            say(f'pool {pool["name"]}: no free slot counted: {why}')
        for _ in range(free):
            if not todo:
                break
            key = todo.pop(0)
            out[key] = labels(pool, key)
            say(f'{key}: pool {pool["name"]} (its dispatcher reports a free slot)')
    if mode == 'fleet':
        waiting = next(p for p in cfg['pools'] if p['waits'])
        for key in todo:
            out[key] = labels(waiting, key)
            say(f'{key}: queues on pool {waiting["name"]} (fleet mode waits for a fleet runner)')
    elif todo:
        say(f'GitHub-hosted, no free fleet slot left: {", ".join(todo)}')
    return mode, out


def cmd_route(cfg, env):
    event, _ = read_event(env)
    gates = None
    try:
        gates = json.loads(env.get('GATES') or 'null')
    except ValueError:
        say('::warning::fork-fleet: the changes outputs cannot be read: every routed job counts as running')
    mode, out = route(cfg, env, event, env.get('FLEET_MODE'), gates)
    attempt = env.get('GITHUB_RUN_ATTEMPT') or '1'
    fleet = sum(1 for v in out.values() if not isinstance(v, str))
    lines = [f'runners={json.dumps(out, separators=(",", ":"))}', f'mode={mode}', f'attempt={attempt}',
             f'watch={"true" if fleet else "false"}']
    with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
        f.write(''.join(f'{line}\n' for line in lines))
    where = {}
    for key, labels in out.items():
        where.setdefault(labels if isinstance(labels, str) else f'pool label {labels[0]}', []).append(key)
    summary(env, [f'### Runners ({mode}, attempt {attempt})', '', '| Runs on | Jobs |', '| --- | --- |']
            + [f'| `{w}` | {", ".join(keys)} |' for w, keys in where.items()])
    return 0


def summary(env, lines):
    path = env.get('GITHUB_STEP_SUMMARY')
    if path and lines:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(''.join(f'{line}\n' for line in lines))


# ---------------------------------------------------------------- watch

def watch(cfg, env, gh, now=time.time, sleep=time.sleep):
    """Bounds this run's wait for fleet runners: a fleet job queued stuck_seconds (fleet mode: fleet_stuck_seconds)
    cancels the run with an error; in auto the sweep then re-runs it GitHub-hosted."""
    mode = mode_of(env.get('FLEET_MODE'))
    bound = cfg['fleet_stuck_seconds'] if mode == 'fleet' else cfg['stuck_seconds']
    rid, attempt = int(env['GITHUB_RUN_ID']), int(env.get('GITHUB_RUN_ATTEMPT') or 1)
    try:
        want = sum(1 for v in json.loads(env.get('FLEET_RUNNERS') or '{}').values() if not isinstance(v, str))
    except (ValueError, AttributeError):
        want = 0
    start, seen = now(), set()
    while True:
        try:
            jobs = [j for j in gh.pages(f'actions/runs/{rid}/attempts/{attempt}/jobs', 'jobs') if is_fleet(cfg, j)]
            seen |= {j['id'] for j in jobs}
            waiting = [j for j in jobs if j.get('status') == 'queued' and not j.get('runner_name')]
            late = [j for j in waiting if now() - (stamp(j.get('created_at')) or now()) >= bound]
            if late:
                j = late[0]
                waited = int((now() - stamp(j['created_at'])) // 60)
                say(f'::error::fork-fleet: {j["name"]} waited {waited} min for a fleet runner (labels '
                    f'{", ".join(j.get("labels") or [])}): cancelling this run. '
                    + ('The sweep re-runs it GitHub-hosted within 10 minutes.' if mode != 'fleet' else
                       'Fleet mode: check the pool\'s dispatcher (.github/fork-fleet.md).'))
                gh.call(f'actions/runs/{rid}/cancel', method='POST')
                return 1
            if not waiting and (len(seen) >= want or now() - start >= SHOW_UP):
                say(f'every fleet job of this run has a runner ({len(seen)} of {want})')
                return 0
        except TROUBLE as e:
            say(f'::warning::fork-fleet: the job list cannot be read ({e.__class__.__name__}: {e}); trying again')
        if now() - start >= bound + SHOW_UP:
            say('::warning::fork-fleet: stopped watching: GitHub did not answer; in auto the sweep is the fallback')
            return 0
        sleep(30)


# ---------------------------------------------------------------- sweep

def rescue_reason(cfg, run, jobs):
    """Why a completed run gets one re-run, or None. jobs: its fleet jobs (latest attempt)."""
    for j in jobs:
        if not j.get('runner_name'):
            created, done = stamp(j.get('created_at')), stamp(j.get('completed_at'))
            if created and done and done - created >= cfg['stuck_seconds']:
                return f'{j["name"]} waited {int((done - created) // 60)} min for a fleet runner and never started'
    if run.get('conclusion') == 'cancelled':
        return None   # cancelled by a person (or a newer push) while a fleet runner had it: that stands
    for j in jobs:
        if not j.get('runner_name') or j.get('conclusion') not in FAILED:
            continue
        if not [s for s in j.get('steps') or [] if s.get('conclusion') == 'failure']:
            return f'{j["name"]} ended {j["conclusion"]} on {j["runner_name"]} with no failed step of its own'
    return None


def current(gh, run):
    """Is the run's commit still the head of its branch? A newer push makes an old run moot."""
    try:
        branch = gh.call(f'branches/{urllib.parse.quote(run.get("head_branch") or "", safe="")}')
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return False   # the branch is gone (merged and deleted)
        raise
    return (branch.get('commit') or {}).get('sha') == run.get('head_sha')


def fleet_jobs_of(cfg, gh, run_id):
    return [j for j in gh.pages(f'actions/runs/{run_id}/jobs?filter=latest', 'jobs') if is_fleet(cfg, j)]


def sweep(cfg, env, gh, now=time.time, sleep=time.sleep):
    # Fleet mode re-runs nothing; it only cancels, with an error, a run whose fleet job waited past
    # fleet_stuck_seconds (the run's own watch does that too, but a re-run of failed jobs has no watch).
    fleet = mode_of(env.get('FLEET_MODE')) == 'fleet'
    bound = cfg['fleet_stuck_seconds'] if fleet else cfg['stuck_seconds']
    workflow = os.path.basename(cfg['trusted']['workflows'][0])
    since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now() - WINDOW))
    notes, trouble = [], 0
    for run in gh.pages(f'actions/workflows/{workflow}/runs?created=%3E%3D{since}', 'workflow_runs', most=3):
        try:
            jobs = fleet_jobs_of(cfg, gh, run['id'])
            if not jobs:
                continue
            # The sweep's own re-run is triggered by github-actions: untrusted, so each run is re-run once at most.
            why = run_refused(cfg, gh, run)
            if why:
                say(f'run {run["id"]}: left alone, not a trusted run: {why}')
                continue
            if run.get('status') != 'completed':
                stuck = [j for j in jobs if j.get('status') == 'queued' and not j.get('runner_name')
                         and now() - stamp(j['created_at']) >= bound]
                if not stuck:
                    continue
                waited = f'{stuck[0]["name"]} has waited {int((now() - stamp(stuck[0]["created_at"])) // 60)} min'
                if fleet:
                    say(f'::error::fork-fleet: run {run["id"]}: {waited} for a fleet runner: cancelling the run '
                        f'(fleet mode: not re-run; is the fleet\'s dispatcher up? .github/fork-fleet.md)')
                    gh.call(f'actions/runs/{run["id"]}/cancel', method='POST')
                    notes.append(f'run {run["id"]} cancelled: {waited} for a fleet runner (fleet mode)')
                    continue
                say(f'run {run["id"]}: {waited} for a fleet runner: cancelling the run, to re-run it GitHub-hosted')
                gh.call(f'actions/runs/{run["id"]}/cancel', method='POST')
                notes.append(f'run {run["id"]} cancelled: {stuck[0]["name"]} waited for a fleet runner')
                end = now() + CANCEL_WAIT
                while now() < end:
                    sleep(10)
                    run = gh.call(f'actions/runs/{run["id"]}')
                    if run.get('status') == 'completed':
                        break
                else:
                    say(f'run {run["id"]}: not finished cancelling yet; the next sweep re-runs it')
                    continue
                jobs = fleet_jobs_of(cfg, gh, run['id'])
            why = None if fleet else rescue_reason(cfg, run, jobs)
            if not why:
                continue
            if not current(gh, run):
                say(f'run {run["id"]}: {why}, but {run.get("head_branch")!r} has moved on: not re-run')
                continue
            gh.call(f'actions/runs/{run["id"]}/rerun', method='POST')
            say(f'run {run["id"]} re-run (GitHub-hosted: the sweep\'s re-run is not a trusted one): {why}')
            notes.append(f'run {run["id"]} re-run: {why}')
        except TROUBLE as e:
            trouble += 1
            say(f'::warning::fork-fleet: run {run.get("id")}: {e.__class__.__name__}: {e}; the next sweep tries again')
    summary(env, notes)
    return 1 if trouble else 0


# ---------------------------------------------------------------- dispatch (a fleet host, as root)

class HostError(Exception):
    pass


class Host:
    """A pool's `launch` command: .github/scripts/fork-fleet-launch.sh, or a wrapper that runs it on another machine."""

    def __init__(self, pool, run=subprocess.run):
        self.pool, self.run = pool, run
        self.mine = re.compile(rf'{re.escape(pool["label"])}-[0-9]{{1,20}}')

    def _do(self, *args, stdin=None):
        try:
            got = self.run(list(self.pool['launch']) + list(args), input=stdin, capture_output=True, text=True,
                           timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            raise HostError(f'pool {self.pool["name"]}: {args[0]}: {e.__class__.__name__}') from None
        return got

    def _must(self, *args, stdin=None):
        got = self._do(*args, stdin=stdin)
        if got.returncode != 0:
            last = (got.stderr or '').strip().splitlines()[-1:] or [f'exit {got.returncode}']
            raise HostError(f'pool {self.pool["name"]}: {args[0]}: {last[0]}')
        return got.stdout

    def units(self):
        """Names of this pool's runners whose unit is still there (starting, running or stopping)."""
        return {n for n in self._must('active').split() if self.mine.fullmatch(n)}

    def fits(self, mb):
        return self._do('fits', str(int(mb))).returncode == 0

    def start(self, name, env_text):
        self._must('start', name, stdin=env_text)

    def stop(self, name):
        self._must('stop', name)

    def prune(self):
        self._must('prune')


def fleet_job(cfg, labels):
    """(pool, job key) when a job asks for exactly a pool's label and the run's own label `route` made for that job."""
    labels = list(labels or ())
    if len(labels) != 2 or labels[0] == labels[1]:
        return None
    keys = {k for k, _ in job_keys(cfg)}
    for pool in cfg['pools']:
        if pool['label'] not in labels:
            continue
        other = labels[1] if labels[0] == pool['label'] else labels[0]
        prefix = pool['label'] + '-'
        m = NONCE.fullmatch(other[len(prefix):]) if other.startswith(prefix) else None
        return (pool, m['key']) if m and m['key'] in keys else None
    return None


def could_take(runner_labels, job_labels):
    """Could a runner with these labels (plus every default label GitHub may add) be given this job?"""
    have = {x.lower() for x in runner_labels} | GENERIC
    return {x.lower() for x in job_labels or ()} <= have


def runner_env(jit, run):
    """The launcher's stdin: KEY=value lines, read into a root-only EnvironmentFile (never argv)."""
    if not JIT.fullmatch(jit or ''):
        raise ValueError('generate-jitconfig gave no usable config')
    return (f'ACTIONS_RUNNER_INPUT_JITCONFIG={jit}\nFORK_FLEET_RUN={int(run["id"])}\n'
            f'FORK_FLEET_ATTEMPT={int(run["run_attempt"])}\n')


def set_variable(gh, name, value):
    try:
        gh.call(f'actions/variables/{name}', 'PATCH', {'name': name, 'value': value})
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        gh.call('actions/variables', 'POST', {'name': name, 'value': value})


def new_state():
    """Kept between passes only to pace heartbeats, idle checks, retries and messages. GitHub (runs, jobs, runner
    registrations) and the host (units) hold everything else, so a restarted dispatcher picks up where one stopped."""
    return {'beacon': {}, 'idle': {}, 'tries': {}, 'said': set()}


def once(state, key, msg):
    if key not in state['said']:
        state['said'].add(key)
        say(msg)


def dispatch_pass(cfg, gh, hosts, state, now=time.time):
    pools = [p for p in cfg['pools'] if p['name'] in hosts]
    runs = {}
    for status in ('queued', 'in_progress'):
        for run in gh.pages(f'actions/runs?status={status}', 'workflow_runs', most=3):
            runs[run['id']] = run
    queued = [(run, j) for run in runs.values() for j in gh.pages(f'actions/runs/{run["id"]}/jobs?filter=latest', 'jobs')
              if j.get('status') == 'queued' and not j.get('runner_name')]
    queued_ids = {j['id'] for _, j in queued}
    state['said'] = {k for k in state['said'] if k[0] in queued_ids}
    state['tries'] = {k: v for k, v in state['tries'].items() if k in queued_ids}
    units = {p['name']: hosts[p['name']].units() for p in pools}
    ours = {}
    for r in gh.pages('actions/runners', 'runners'):
        for p in pools:
            if hosts[p['name']].mine.fullmatch(r.get('name') or ''):
                ours[r['name']] = (p, r)
    state['idle'] = {k: v for k, v in state['idle'].items() if k in ours}

    # Leftovers: a registration with no runner behind it, and a runner whose job went away before it started. Such a
    # runner is stopped at once when a queued job of another attempt or run could be given it (the hook would refuse
    # that job), else after idle_passes passes (a runner that is just taking its own job is not busy yet).
    for name, (p, r) in ours.items():
        if r.get('status') == 'offline' and name not in units[p['name']]:
            try:
                gh.call(f'actions/runners/{int(r["id"])}', 'DELETE')
            except urllib.error.HTTPError as e:
                if e.code != 404:
                    raise
            say(f'{name}: deleted a registration with no runner behind it')
        elif r.get('status') == 'online' and not r.get('busy') and int(name.rsplit('-', 1)[1]) not in queued_ids:
            state['idle'][name] = state['idle'].get(name, 0) + 1
            labels = [x.get('name') for x in r.get('labels') or [] if isinstance(x, dict)]
            if state['idle'][name] >= cfg['dispatch']['idle_passes'] or \
                    any(could_take(labels, j.get('labels')) for _, j in queued):
                hosts[p['name']].stop(name)
                state['idle'].pop(name)
                say(f'{name}: stopped, its job is no longer queued')
        else:
            state['idle'].pop(name, None)
    for p in pools:
        hosts[p['name']].prune()

    # Launches: one runner per queued fleet job of a trusted run.
    active = {p['name']: len(units[p['name']]) for p in pools}
    pending = {p['name']: 0 for p in pools}
    started_mb = {p['name']: 0 for p in pools}   # memory of runners this pass started, not in use yet
    memory = {k: j['memory_mb'] for k, j in job_keys(cfg)}
    trust = {}
    for run, j in queued:
        hit = fleet_job(cfg, j.get('labels'))
        if not hit or hit[0]['name'] not in hosts:
            continue
        pool, key = hit
        if j.get('run_attempt', run.get('run_attempt')) != run.get('run_attempt'):
            continue   # re-run after the run list was read: its runner would be made for the old attempt; next pass
        name = f'{pool["label"]}-{int(j["id"])}'
        if name in units[pool['name']] or name in ours:
            continue   # started already (its runner is coming up), or a leftover deleted above: next pass
        tag = f'job {j["id"]} ({j.get("name")}) of run {run["id"]}'
        if (run['id'], run.get('run_attempt')) not in trust:
            trust[(run['id'], run.get('run_attempt'))] = run_refused(cfg, gh, run)
        why = trust[(run['id'], run.get('run_attempt'))]
        if why:
            once(state, (j['id'], 'trust'), f'{tag}: no runner, not a trusted run: {why}')
            continue
        pending[pool['name']] += 1
        if state['tries'].get(j['id'], 0) >= MAX_TRIES:
            once(state, (j['id'], 'tries'), f'::warning::{tag}: {MAX_TRIES} runners started and none took it; left to '
                                            'the watch and the sweep')
            continue
        if active[pool['name']] >= pool['slots']:
            continue
        rivals = [o for _, o in queued if o['id'] != j['id'] and could_take(j['labels'], o.get('labels'))]
        if rivals:
            once(state, (j['id'], 'rival', rivals[0]['id']),
                 f'::warning::{tag}: not started while job {rivals[0]["id"]} ({rivals[0].get("name")}) of another run '
                 f'could take its runner (labels {rivals[0].get("labels")})')
            continue
        if not hosts[pool['name']].fits(memory[key] + started_mb[pool['name']]):
            once(state, (j['id'], 'memory'), f'{tag}: waits for {memory[key]} MB on pool {pool["name"]}')
            continue
        try:
            got = gh.call('actions/runners/generate-jitconfig', 'POST',
                          {'name': name, 'runner_group_id': RUNNER_GROUP, 'labels': list(j['labels']),
                           'work_folder': '_work'})
        except urllib.error.HTTPError as e:
            if e.code != 409:
                raise
            continue   # a registration with this name appeared since the list: the next pass sorts it out
        state['tries'][j['id']] = state['tries'].get(j['id'], 0) + 1
        hosts[pool['name']].start(name, runner_env(got.get('encoded_jit_config'), run))
        active[pool['name']] += 1
        pending[pool['name']] -= 1
        started_mb[pool['name']] += memory[key]
        say(f'{name}: started for {tag}, attempt {run.get("run_attempt")}')

    # Heartbeats: what `route` may count on, per pool.
    t = now()
    biggest = max(memory.values())
    for p in pools:
        free = max(p['slots'] - active[p['name']] - pending[p['name']], 0)
        if free and not hosts[p['name']].fits(biggest + started_mb[p['name']]):
            free = 0
        last = state['beacon'].get(p['name'])
        if last and last[0] == free and t - last[1] < cfg['dispatch']['beacon_seconds']:
            continue
        set_variable(gh, p['beacon'], json.dumps({'at': int(t), 'free': free}, separators=(',', ':')))
        state['beacon'][p['name']] = (free, t)
    return 0


def read_token(path):
    st = os.stat(path)
    if st.st_uid != os.geteuid() or st.st_mode & 0o077:
        raise ValueError(f'{path} must belong to this user and be readable by it alone (chmod 600)')
    with open(path, encoding='utf-8') as f:
        token = f.read().strip()
    if not token:
        raise ValueError(f'{path} is empty')
    return token


def take_lock(path):
    import fcntl
    os.makedirs(os.path.dirname(path) or '.', mode=0o700, exist_ok=True)   # /run is empty after a boot
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        raise ValueError(f'another dispatcher holds {path}') from None
    return fd


def cmd_dispatch(cfg, args, sleep=time.sleep):
    names = args.pool or [p['name'] for p in cfg['pools']]
    unknown = sorted(set(names) - {p['name'] for p in cfg['pools']})
    if unknown:
        raise ValueError(f'no pool {unknown} in the config')
    token = read_token(args.token_file)
    lock = take_lock(args.lock)
    gh = GitHub(token, cfg['repository'])
    hosts = {p['name']: Host(p) for p in cfg['pools'] if p['name'] in names}
    state = new_state()
    while True:
        try:
            dispatch_pass(cfg, gh, hosts, state)
        except (*TROUBLE, HostError) as e:
            say(f'::warning::fork-fleet: this pass stopped ({e.__class__.__name__}: {e}); the next one starts over')
            if args.once:
                return 1
        if args.once:
            os.close(lock)
            return 0
        sleep(cfg['dispatch']['every_seconds'])


# ---------------------------------------------------------------- main

def main(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default=CONFIG)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('route')
    sub.add_parser('gate')
    sub.add_parser('watch')
    sub.add_parser('sweep')
    dis = sub.add_parser('dispatch')
    dis.add_argument('--pool', action='append', help='a pool this host serves (default: every pool)')
    dis.add_argument('--token-file', default='/etc/fork-fleet/token')
    dis.add_argument('--lock', default='/run/fork-fleet/dispatch.lock')
    dis.add_argument('--once', action='store_true', help='one pass, then exit (1 when it stopped half-way)')
    args = ap.parse_args(argv)
    cfg = load(args.config)
    if args.cmd == 'gate':
        return gate(cfg, env)
    if args.cmd == 'route':
        return cmd_route(cfg, env)
    if args.cmd == 'dispatch':
        return cmd_dispatch(cfg, args)
    gh = GitHub(env.get('GITHUB_TOKEN'), cfg['repository'])
    return watch(cfg, env, gh) if args.cmd == 'watch' else sweep(cfg, env, gh)


if __name__ == '__main__':
    gating = 'gate' in sys.argv[1:]
    try:
        sys.exit(main())
    except Exception as e:   # the gate refuses on anything unexpected: a gate that crashes open is no gate
        if gating:
            sys.exit(stop_job(f'the gate itself failed: {e.__class__.__name__}: {e}'))
        say(f'::error::fork-fleet: {e.__class__.__name__}: {e}')
        sys.exit(1)
