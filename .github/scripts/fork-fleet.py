#!/usr/bin/env python3
"""Fork only: where fork-tests.yml's Linux jobs run, the repository's own fleet of self-hosted runners or GitHub-hosted.

Every fact about the repository and its fleet is in one file, .github/fork-fleet.json (CONFIG); none is here. Runbook,
owner steps and the failure table: .github/fork-fleet.md.

  python3 -I fork-fleet.py route           fork-tests.yml's `route` job (GitHub-hosted). Writes to $GITHUB_OUTPUT:
                                           runners=<JSON {job key: runs-on}>, mode=<auto|fleet|github>, attempt=<n>.
                                           A job key is the job id, or <id>-<shard> for a sharded job.
  python3 -I fork-fleet.py gate            a fleet runner's job-started hook, from an installed copy beside an installed
                                           CONFIG (--config): exit 1 refuses the job before its first step.
  python3 -I fork-fleet.py capacity <job>  a routed job's step after checkout: on a fleet runner, waits until the pool's
                                           memory check passes for the job's memory_mb; on GitHub-hosted, nothing.
  python3 -I fork-fleet.py sweep           fork-fleet.yml every 10 minutes (GitHub-hosted, actions: write): in auto and
                                           github mode, a run whose fleet job waited stuck_seconds for a runner is
                                           cancelled, and a run that a fleet runner did not finish is re-run once (all
                                           jobs; attempt 2 of an auto run goes hosted).

The setting, repository variable CONFIG["variable"] (route and sweep read it from $FLEET_MODE):
  auto (also unset)  a job goes to an idle fleet runner when one is free, GitHub-hosted otherwise. Anything unclear (no
                     token, a GitHub error, a re-run) is GitHub-hosted with a note: auto never makes a run red.
  fleet              every job that runs goes to the fleet, idle runners first, the rest queue on the pool marked
                     `waits`; never GitHub-hosted for a trusted run. Untrusted runs still go hosted (below).
  github             every job GitHub-hosted, as before the fleet.
Only trusted runs may use the fleet, in every mode (`refused`): a workflow in trusted.workflows of this repository,
started and triggered by a trusted actor, as a push to a trusted branch or a pull request whose head and base are this
repository and which a trusted actor opened. Never a pull request from another repository: this repository is public,
and its code would run on the fleet. `route` routes by that rule, and each fleet runner's job-started hook runs the same
rule (`gate`) from main's copy, so a job that names a fleet label by itself (a changed workflow, a re-run) is refused.
Standard library only: a fleet runner copies this file and CONFIG, nothing else.
"""
import argparse, calendar, json, os, re, shutil, subprocess, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(os.path.dirname(HERE), 'fork-fleet.json')
MODES = ('auto', 'fleet', 'github')
API = os.environ.get('GITHUB_API_URL') or 'https://api.github.com'
# Labels a runner may get by default; a pool must name at least one other label, so a job that asks only for these never
# lands on the fleet, and the sweep tells a fleet job by its pool's own labels.
GENERIC = frozenset(('self-hosted', 'linux', 'x64', 'arm64', 'arm', 'windows', 'macos'))
WINDOW = 24 * 3600     # the sweep looks at runs created this long ago at most
CANCEL_WAIT = 120      # seconds the sweep watches a run it cancelled, to re-run it in the same pass
FAILED = ('failure', 'timed_out')
# The routed jobs' memory step (fork-tests.yml; fork-fleet.test.py pins the name): when it is the only failed step, the
# fleet runner was short of memory, not the change wrong, and the sweep re-runs the run like a lost runner.
CAPACITY_STEP = 'Wait for memory on a fleet runner'


def say(msg):
    print(msg, flush=True)


def load(path=CONFIG):
    with open(path, encoding='utf-8') as f:
        cfg = json.load(f)
    problems = check_config(cfg)
    if problems:
        raise ValueError(f'{path}: ' + '; '.join(problems))
    return cfg


def check_config(cfg):
    out = []
    for key in ('repository', 'variable', 'token_secret', 'hosted', 'trusted', 'pools', 'jobs', 'stuck_seconds',
                'capacity_wait'):
        if key not in cfg:
            out.append(f'no {key!r}')
    if out:
        return out
    names = [p.get('name') for p in cfg['pools']]
    if len(set(names)) != len(names):
        out.append('two pools with one name')
    for p in cfg['pools']:
        if not set(p.get('labels') or ()) - GENERIC:
            out.append(f'pool {p.get("name")!r} names no label of its own (only {sorted(GENERIC)})')
        try:
            re.compile(p.get('runner_name') or '')
        except re.error as e:
            out.append(f'pool {p.get("name")!r}: runner_name is no regex ({e})')
        if not p.get('runner_name'):
            out.append(f'pool {p.get("name")!r} has no runner_name')
        cap = p.get('capacity', 'missing')
        if cap == 'missing' or not (cap is None or (isinstance(cap, list) and cap and all(isinstance(x, str) for x in cap))):
            out.append(f'pool {p.get("name")!r}: capacity must be a command list or null (no memory check)')
    if cfg['pools'] and not any(p.get('waits') for p in cfg['pools']):
        out.append('no pool has "waits": true (where fleet mode queues)')
    keys = [k for k, _ in job_keys(cfg)]
    if len(set(keys)) != len(keys):
        out.append('a job key twice')
    for j in cfg['jobs']:
        if not isinstance(j.get('memory_mb'), int) or j['memory_mb'] <= 0:
            out.append(f'job {j.get("job")!r}: memory_mb must be a positive number')
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


# ---------------------------------------------------------------- the rule: route and every fleet runner's hook

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


def read_event(env):
    path = env.get('GITHUB_EVENT_PATH')
    try:
        with open(path, encoding='utf-8') as f:
            return json.load(f), None
    except (TypeError, OSError, ValueError) as e:
        return None, f'the event file {path!r} cannot be read ({e.__class__.__name__})'


def gate(cfg, env):
    event, why = read_event(env)
    why = why or refused(cfg, env, event)
    if why:
        say(f'::error::fork-fleet: this job may not run on a fleet runner: {why} (.github/fork-fleet.md)')
        return 1
    say(f'fork-fleet: allowed ({env.get("GITHUB_EVENT_NAME")} {env.get("GITHUB_REF")})')
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


TROUBLE = (urllib.error.URLError, OSError, ValueError, KeyError, TypeError)


# ---------------------------------------------------------------- route

def idle_slots(cfg, runners):
    """[(pool, count of its online, idle runners)] in CONFIG's pool order."""
    out = []
    for p in cfg['pools']:
        want = set(p['labels'])
        n = sum(1 for r in runners if r.get('status') == 'online' and not r.get('busy')
                and want <= {lab.get('name') for lab in r.get('labels') or []})
        out.append((p, n))
    return out


def runs(job, gates):
    """Does this job run for this change? gates: the `changes` job's outputs (None when they are missing)."""
    if not job.get('gates') or gates is None:
        return True
    return any(str(gates.get(g)) != 'false' for g in job['gates'])


def route(cfg, env, event, mode_value, token, gates, gh=None):
    """(mode, {key: runs-on}). Raises only for a broken setting or CONFIG; GitHub trouble never makes it raise."""
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
    if mode == 'auto' and int(env.get('GITHUB_RUN_ATTEMPT') or 1) > 1:
        say('GitHub-hosted for every job: a re-run (attempt 2+) in auto mode')
        return mode, out
    waiting = next(p for p in cfg['pools'] if p.get('waits'))
    slots = []
    if not token:
        say(f'::notice::fork-fleet: no {cfg["token_secret"]} secret, so the fleet\'s runners cannot be listed'
            + (': GitHub-hosted for every job' if mode == 'auto' else f': every job queues on pool {waiting["name"]}'))
    else:
        gh = gh or GitHub(token, cfg['repository'])
        try:
            slots = idle_slots(cfg, gh.pages('actions/runners', 'runners'))
        except TROUBLE as e:
            say(f'::warning::fork-fleet: the runner list cannot be read ({e})'
                + (': GitHub-hosted for every job' if mode == 'auto' else f': queueing on pool {waiting["name"]}'))
    todo = list(wanted)
    for pool, n in slots:
        for _ in range(n):
            if not todo:
                break
            key = todo.pop(0)
            out[key] = pool['labels']
            say(f'{key}: an idle runner of pool {pool["name"]}')
    if mode == 'fleet':
        for key in todo:
            out[key] = waiting['labels']
            say(f'{key}: queues on pool {waiting["name"]} (fleet mode waits for a fleet runner)')
    elif todo:
        say(f'GitHub-hosted, no idle fleet runner left: {", ".join(todo)}')
    return mode, out


def cmd_route(cfg, env):
    event, _ = read_event(env)
    gates = None
    try:
        gates = json.loads(env.get('GATES') or 'null')
    except ValueError:
        say('::warning::fork-fleet: the changes outputs cannot be read: every routed job counts as running')
    mode, out = route(cfg, env, event, env.get('FLEET_MODE'), env.get('FLEET_TOKEN'), gates)
    attempt = env.get('GITHUB_RUN_ATTEMPT') or '1'
    lines = [f'runners={json.dumps(out, separators=(",", ":"))}', f'mode={mode}', f'attempt={attempt}']
    with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
        f.write(''.join(f'{line}\n' for line in lines))
    where = {}
    for key, labels in out.items():
        where.setdefault(labels if isinstance(labels, str) else ', '.join(labels), []).append(key)
    summary(env, [f'### Runners ({mode}, attempt {attempt})', '', '| Runs on | Jobs |', '| --- | --- |']
            + [f'| `{w}` | {", ".join(keys)} |' for w, keys in where.items()])
    return 0


def summary(env, lines):
    path = env.get('GITHUB_STEP_SUMMARY')
    if path and lines:
        with open(path, 'a', encoding='utf-8') as f:
            f.write(''.join(f'{line}\n' for line in lines))


# ---------------------------------------------------------------- capacity

def capacity(cfg, env, job_name, run=subprocess.run, sleep=time.sleep, now=time.monotonic):
    if env.get('RUNNER_ENVIRONMENT') != 'self-hosted':
        say('GitHub-hosted: no memory check')
        return 0
    name = env.get('RUNNER_NAME') or ''
    pool = next((p for p in cfg['pools'] if re.search(p['runner_name'], name)), None)
    if pool is None:
        say(f'::error::fork-fleet: runner {name!r} is in no pool of .github/fork-fleet.json (runner_name)')
        return 1
    if pool['capacity'] is None:
        say(f'pool {pool["name"]}: no memory check (capacity null in .github/fork-fleet.json)')
        return 0
    job = next((j for j in cfg['jobs'] if j['job'] == job_name), None)
    if job is None:
        say(f'::error::fork-fleet: job {job_name!r} is not in .github/fork-fleet.json')
        return 1
    cmd = [part.replace('{mb}', str(job['memory_mb'])) for part in pool['capacity']]
    if not (os.path.isfile(cmd[0]) or shutil.which(cmd[0])):
        say(f'::error::fork-fleet: pool {pool["name"]}\'s memory check {cmd[0]} is not on runner {name!r}')
        return 1
    wait = cfg['capacity_wait']
    start = now()
    while True:
        try:
            ok = run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=60).returncode == 0
        except (OSError, subprocess.SubprocessError):
            ok = False
        if ok:
            say(f'pool {pool["name"]}: {job["memory_mb"]} MB free for {job_name}')
            return 0
        waited = now() - start
        if waited >= wait['max_seconds']:
            say(f'::error::fork-fleet: {" ".join(cmd)} stayed NOT OK for {int(waited)} s on {name!r}')
            return 1
        say(f'pool {pool["name"]}: not {job["memory_mb"]} MB free yet, waiting ({int(waited)} s)')
        sleep(wait['every_seconds'])


# ---------------------------------------------------------------- sweep

def fleet_labels(cfg):
    return set().union(*(set(p['labels']) - GENERIC for p in cfg['pools'])) if cfg['pools'] else set()


def stamp(text):
    return calendar.timegm(time.strptime(text, '%Y-%m-%dT%H:%M:%SZ')) if text else None


def rescue_reason(cfg, run, jobs):
    """Why a completed attempt-1 run gets one re-run, or None. jobs: its fleet jobs."""
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
        failed = [s.get('name') for s in j.get('steps') or [] if s.get('conclusion') == 'failure']
        if not failed:
            return f'{j["name"]} ended {j["conclusion"]} on {j["runner_name"]} with no failed step of its own'
        if failed == [CAPACITY_STEP]:
            return f'{j["name"]}: {j["runner_name"]} never had the memory the job needs'
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


def sweep(cfg, env, gh, now=time.time, sleep=time.sleep):
    mode = mode_of(env.get('FLEET_MODE'))
    if mode == 'fleet':
        say('fleet mode: a fleet job waits for its runner, nothing is moved')
        return 0
    mine = fleet_labels(cfg)
    workflow = os.path.basename(cfg['trusted']['workflows'][0])
    since = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now() - WINDOW))
    notes, trouble = [], 0
    for run in gh.pages(f'actions/workflows/{workflow}/runs?created=%3E%3D{since}', 'workflow_runs', most=3):
        if run.get('run_attempt') != 1:
            continue
        try:
            jobs = [j for j in gh.pages(f'actions/runs/{run["id"]}/attempts/1/jobs', 'jobs')
                    if set(j.get('labels') or ()) & mine]
            if not jobs:
                continue
            if run.get('status') != 'completed':
                stuck = [j for j in jobs if j.get('status') == 'queued' and not j.get('runner_name')
                         and now() - stamp(j['created_at']) >= cfg['stuck_seconds']]
                if not stuck:
                    continue
                say(f'run {run["id"]}: {stuck[0]["name"]} has waited {int((now() - stamp(stuck[0]["created_at"])) // 60)}'
                    f' min for a fleet runner: cancelling the run, to re-run it GitHub-hosted')
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
                jobs = [j for j in gh.pages(f'actions/runs/{run["id"]}/attempts/1/jobs', 'jobs')
                        if set(j.get('labels') or ()) & mine]
            why = rescue_reason(cfg, run, jobs)
            if not why:
                continue
            if not current(gh, run):
                say(f'run {run["id"]}: {why}, but {run.get("head_branch")!r} has moved on: not re-run')
                continue
            gh.call(f'actions/runs/{run["id"]}/rerun', method='POST')
            say(f'run {run["id"]} re-run (attempt 2, GitHub-hosted in auto mode): {why}')
            notes.append(f'run {run["id"]} re-run: {why}')
        except TROUBLE as e:
            trouble += 1
            say(f'::warning::fork-fleet: run {run.get("id")}: {e}; the next sweep tries again')
    summary(env, notes)
    return 1 if trouble else 0


# ---------------------------------------------------------------- main

def main(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default=CONFIG)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('route')
    sub.add_parser('gate')
    cap = sub.add_parser('capacity')
    cap.add_argument('job')
    sub.add_parser('sweep')
    args = ap.parse_args(argv)
    cfg = load(args.config)
    if args.cmd == 'gate':
        return gate(cfg, env)
    if args.cmd == 'route':
        return cmd_route(cfg, env)
    if args.cmd == 'capacity':
        return capacity(cfg, env, args.job)
    return sweep(cfg, env, GitHub(env.get('GITHUB_TOKEN'), cfg['repository']))


if __name__ == '__main__':
    gating = 'gate' in sys.argv[1:]
    try:
        sys.exit(main())
    except Exception as e:   # the gate refuses on anything unexpected: a gate that crashes open is no gate
        say(f'::error::fork-fleet: {e.__class__.__name__}: {e}' + (': refused' if gating else ''))
        sys.exit(1)
