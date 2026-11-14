#!/usr/bin/env python3
"""Fork only: where fork-tests.yml's Linux jobs run, the repository's own fleet of self-hosted runners or GitHub-hosted.

Every fact about the repository and its fleet is in one file, .github/fork-fleet.json (CONFIG); none is here. Runbook,
owner steps, the design and the failure table: .github/fork-fleet.md.

  python3 -I fork-fleet.py route     the last step of fork-tests.yml's `changes` job (GitHub-hosted). Writes to
                                     $GITHUB_OUTPUT: runners=<JSON {job key: runs-on}>, mode, attempt, watch,
                                     fallback=<JSON runs-on for a job of another attempt (a re-run of failed jobs)>.
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
  auto (also unset)  a job goes to a pool whose dispatcher reports a free slot, GitHub-hosted otherwise. No heartbeat
                     is GitHub-hosted with a log line; an old or unreadable one (the dispatcher is down, its token
                     expired) with an error annotation, so it is never silent. auto never makes a run red.
  fleet              every job that runs goes to the fleet, pools with a free slot first, the rest queue on the pool
                     marked `waits`. A job that only prints a skip stays GitHub-hosted. Untrusted runs go hosted.
With "hosted": false (no GitHub-hosted runners) every job that would go GitHub-hosted goes to the always-on (static)
pool for its kind of run instead, and a run that may hold code from another repository fails red. A static pool
(runners registered by hand) has no dispatcher and no job-started hook, so it is only for a private repository: the
config must say "private": true, and route refuses static pools unless the event says the repository is private.
  github             every job GitHub-hosted, as before the fleet.

No fleet runner waits for work. The dispatcher makes a one-job (JIT) runner for one queued job at a time, and only when:
the job asks for exactly a pool's label plus the per-run random label `route` made for it, which the job's runs-on ends
with its own attempt (so a runner made for one attempt can never be given another's job); GitHub's own record of the
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
POOL = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')
KINDS = ('push', 'draft', 'ready')   # a pool's `takes`: pushes, draft pull requests, pull requests ready for review
# Events whose code and workflow are this repository's own (a pull request event: only when head and base are too).
OWN_EVENTS = ('push', 'workflow_dispatch', 'schedule')
# The run's label for one job: <pool label>-<job key>-<16 hex, made by route>-a<attempt, added by the job's runs-on>.
NONCE = re.compile(r'(?P<key>[a-z0-9_]{1,40}(?:-[0-9]{1,3})?)-[0-9a-f]{16}-a(?P<attempt>[1-9][0-9]{0,4})')
VERSION = re.compile(r'v?([0-9]{1,5})\.([0-9]{1,5})\.([0-9]{1,5})')
JIT = re.compile(r'[A-Za-z0-9+/=]{1,65536}')
SHA = re.compile(r'[0-9a-f]{40}')
TAG_PREFIX = re.compile(r'[a-z0-9][a-z0-9/_-]{0,58}/')
SYNC_REMOTE = 'https://github.com/{}.git'   # .format(repository): read anonymously, the repository is public
SYNC_CHECK_SECONDS = 30
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
    for key in ('repository', 'hosted', 'trusted', 'pools', 'jobs', 'stuck_seconds', 'fleet_stuck_seconds'):
        if key not in cfg:
            out.append(f'no {key!r}')
    pools = cfg.get('pools') or []
    launched = [p for p in pools if not p.get('static')]
    for key in ('beacon_fresh_seconds', 'dispatch') if launched else ():
        if key not in cfg:
            out.append(f'no {key!r} (a pool without "static": true has a dispatcher)')
    if out:
        return out
    hosted = cfg['hosted']
    if not (hosted is False or isinstance(hosted, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]{0,62}', hosted)):
        out.append('hosted must be a GitHub-hosted runner label, or false for none')
    elif hosted is not False and self_hosted(cfg, hosted):
        out.append(f'hosted is {hosted!r}, a self-hosted, pool or retired label: untrusted runs go there')
    if hosted is False and 'upstream_sync' in cfg:
        out.append('upstream_sync needs a hosted label: upstream code runs on GitHub-hosted runners only')
    if any(p.get('static') for p in pools) and cfg.get('private') is not True:
        out.append('a static pool needs "private": true: its runners have no job-started hook, so on a public '
                   'repository a pull request from a fork could name their labels (static pools: private repositories '
                   'only)')
    if 'private' in cfg and not isinstance(cfg['private'], bool):
        out.append('private must be true or false')
    for key in ('stuck_seconds', 'fleet_stuck_seconds') + (('beacon_fresh_seconds',) if launched else ()):
        if not positive(cfg[key]):
            out.append(f'{key} must be a positive whole number')
    for key in ('every_seconds', 'beacon_seconds', 'idle_passes') if launched else ():
        if not positive((cfg['dispatch'] or {}).get(key)):
            out.append(f'dispatch.{key} must be a positive whole number')
    if 'upstream_sync' in cfg:
        prefix = (cfg['upstream_sync'] or {}).get('tag_prefix')
        if not (isinstance(prefix, str) and TAG_PREFIX.fullmatch(prefix)):
            out.append('upstream_sync.tag_prefix must be a tag name prefix ending in / (a-z, 0-9, /, _ and -)')
    retired = cfg.get('retired_labels', [])
    if not (isinstance(retired, list) and all(isinstance(x, str) and LABEL.fullmatch(x) and x not in GENERIC
                                              for x in retired)):
        out.append(f'retired_labels must be a list of labels, none of {sorted(GENERIC)}')
    for field, among in (('name', pools), ('label', launched), ('beacon', launched)):
        got = [p.get(field) for p in among]
        if len(set(map(str, got))) != len(got):
            out.append(f'two pools with one {field}')
    for p in pools:
        who = f'pool {p.get("name")!r}'
        if not (isinstance(p.get('name'), str) and POOL.fullmatch(p['name'])):
            out.append(f'{who}: name must be a-z, 0-9, _ and - (it names the dispatcher\'s lock file)')
        if not positive(p.get('slots')):
            out.append(f'{who}: slots must be a positive whole number')
        if not isinstance(p.get('waits'), bool):
            out.append(f'{who}: waits must be true or false')
        takes = p.get('takes', list(KINDS))
        if not (isinstance(takes, list) and takes and all(x in KINDS for x in takes)):
            out.append(f'{who}: takes must list some of {list(KINDS)}')
        if 'per_run' in p and (p.get('waits') is not False or not positive(p['per_run'])):
            out.append(f'{who}: per_run must be a positive whole number, on a pool that does not wait')
        if p.get('static'):
            got = p.get('runs_on')
            if not (isinstance(got, list) and got and all(isinstance(x, str) and LABEL.fullmatch(x) for x in got)
                    and set(got) - GENERIC):
                out.append(f'{who}: runs_on must list its runners\' labels, one of them not one of {sorted(GENERIC)}')
            elif any(dispatcher_or_retired(cfg, x) for x in got if x not in GENERIC):
                out.append(f'{who}: runs_on names a dispatcher pool\'s or a retired label')
            continue
        if not (isinstance(p.get('label'), str) and LABEL.fullmatch(p['label']) and p['label'] not in GENERIC):
            out.append(f'{who}: label must be one label of its own (a-z, 0-9, -), none of {sorted(GENERIC)}')
        if not (isinstance(p.get('beacon'), str) and VARIABLE.fullmatch(p['beacon'])):
            out.append(f'{who}: beacon must be a repository variable name (A-Z, 0-9, _; not GITHUB_)')
        launch = p.get('launch')
        if not (isinstance(launch, list) and launch and all(isinstance(x, str) and x for x in launch)):
            out.append(f'{who}: launch must be a command list')
    for kind in KINDS:
        if sum(1 for p in pools if p.get('waits') is True and kind in takes_of(p)) != 1:
            out.append(f'exactly one pool with "waits": true must take {kind} runs (where fleet mode queues them)')
        if hosted is False and not any(p.get('static') and kind in takes_of(p) for p in pools):
            out.append(f'with "hosted": false a static pool must take {kind} runs (where jobs that are not routed, '
                       're-runs of failed jobs and runs the dispatchers refuse go)')
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


def dispatcher_or_retired(cfg, label):
    """Is this a dispatcher pool's label, one of its run labels, or a retired label (or a run label of one)?"""
    retired = cfg.get('retired_labels')
    names = [p.get('label') for p in cfg.get('pools') or [] if not p.get('static')] + (
        retired if isinstance(retired, list) else [])
    return any(isinstance(n, str) and (label == n or label.startswith(n + '-')) for n in names)


def self_hosted(cfg, label):
    """Could a self-hosted runner of this fleet have this label: a generic one, a static pool's, a dispatcher pool's (or
    a run label of one) or a retired one? `hosted` must be none of them."""
    static = {x for p in cfg.get('pools') or [] if p.get('static') and isinstance(p.get('runs_on'), list)
              for x in p['runs_on']}
    return label in GENERIC or label in static or dispatcher_or_retired(cfg, label)


def takes_of(pool):
    got = pool.get('takes', KINDS)
    return got if isinstance(got, list) else KINDS


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


def dispatched(cfg):
    """The pools a dispatcher serves; a "static" pool's runners are registered by hand and always there."""
    return [p for p in cfg['pools'] if not p.get('static')]


def fleet_labels(cfg):
    """Labels that mark a job as the fleet's: the watch and the sweep handle a job asking for one. retired_labels keep
    a job queued on a pool's old label (a run routed before the change) handled until it is gone."""
    return ({p['label'] for p in dispatched(cfg)} | set(cfg.get('retired_labels', []))
            | {x for p in cfg['pools'] if p.get('static') for x in p['runs_on'] if x not in GENERIC})


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
        return made_by_the_sync(cfg, env.get('GITHUB_SHA'))
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


def made_by_the_sync(cfg, sha, run=subprocess.run):
    """Why a push of commit `sha` may not use the fleet because the upstream sync made it, or None.

    Owner decision: upstream's code runs on GitHub-hosted runners only. The sync (fork-follow-push.sh advance) moves
    main with the owner's token, so its push run passes every other check here. It first tags the commit
    <upstream_sync.tag_prefix><sha> in this repository and moves main only when that push worked. A tag here can only
    be made by a holder of push rights (the owner, the sync's token): nothing an upstream commit holds (its message,
    author, files, a branch or pull request upstream) makes or removes one. And a tag only ever sends a run to
    GitHub-hosted runners, so the worst a stray tag does is that. Read anonymously (the job hook has no token); any
    doubt (no full sha, git failing, a timeout) refuses."""
    if 'upstream_sync' not in cfg:
        return None   # this repository has no upstream sync
    if not (isinstance(sha, str) and SHA.fullmatch(sha)):
        return f'the pushed commit {sha!r} is not a full sha, so whether the upstream sync made it cannot be told'
    ref = f'refs/tags/{cfg["upstream_sync"]["tag_prefix"]}{sha}'
    plain = {'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'GIT_TERMINAL_PROMPT': '0', 'GIT_CONFIG_NOSYSTEM': '1',
             'GIT_CONFIG_GLOBAL': '/dev/null'}   # no git config of whoever runs this (the job hook: a runner user)
    try:
        got = run(['git', '-c', 'protocol.version=2', 'ls-remote', '--exit-code', SYNC_REMOTE.format(cfg['repository']),
                   ref], env=plain, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                  timeout=SYNC_CHECK_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return f'whether the upstream sync made {sha[:12]} cannot be told ({e.__class__.__name__})'
    if got.returncode == 2:   # --exit-code: the remote has no such ref
        return None
    if got.returncode == 0:
        return (f'commit {sha[:12]} was pushed by the upstream sync (tag {ref}): upstream code runs on '
                'GitHub-hosted runners only')
    return f'whether the upstream sync made {sha[:12]} cannot be told (git ls-remote exit {got.returncode})'


def facts_of_run(run):
    """The GITHUB_* variables `refused` reads, from GitHub's own record of a workflow run (never from a job)."""
    repo = (run.get('repository') or {}).get('full_name')
    path = (run.get('path') or '').split('@')[0]
    return {'GITHUB_REPOSITORY': repo, 'GITHUB_WORKFLOW_REF': f'{repo}/{path}@refs/heads/{run.get("head_branch")}',
            'GITHUB_ACTOR': (run.get('actor') or {}).get('login'),
            'GITHUB_TRIGGERING_ACTOR': (run.get('triggering_actor') or {}).get('login'),
            'GITHUB_EVENT_NAME': run.get('event'), 'GITHUB_REF': f'refs/heads/{run.get("head_branch")}',
            'GITHUB_SHA': run.get('head_sha')}


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
        """path: below /repos/<repo>/, or from the API's root when it starts with /."""
        if not self.token:
            raise ValueError(f'no token for {path}')
        body = None if data is None else json.dumps(data).encode()
        url = f'{API}{path}' if path.startswith('/') else f'{API}/repos/{self.repo}/{path}'
        req = urllib.request.Request(url, data=body, method=method,
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
    note = got.get('why')
    if free <= 0 and isinstance(note, str) and note:
        return 0, 'its dispatcher says: ' + re.sub(r'[^\w .,:;()/+=-]', '?', note)[:300]
    return max(free, 0), None


def mode_in(cfg, value):
    """The setting as route, watch and sweep apply it: with no GitHub-hosted runners ("hosted": false), auto and github
    act as fleet, so a job always has a runner of the fleet's to wait for."""
    mode = mode_of(value)
    if cfg['hosted'] is False and mode != 'fleet':
        if mode == 'github':
            say('::error::fork-fleet: the setting is github, but this repository has no GitHub-hosted runners '
                '("hosted": false in the fleet config): fleet mode instead')
        return 'fleet'
    return mode


def kind_of(env, event):
    """One of KINDS for a run of this repository (`outside` let it through): any event but a pull request is a push."""
    if env.get('GITHUB_EVENT_NAME') not in ('pull_request', 'pull_request_target'):
        return 'push'
    return 'draft' if (event.get('pull_request') or {}).get('draft') is True else 'ready'


def outside(cfg, env, event):
    """Why this run may hold code or a workflow from another repository, or None: a push, a manual or a scheduled run
    of this repository, or a pull request (or pull_request_target) whose head and base are this repository. Wider than
    the trust rule (`refused`): any actor, branch and workflow of this repository passes."""
    repo = cfg['repository']
    if env.get('GITHUB_REPOSITORY') != repo:
        return f'repository {env.get("GITHUB_REPOSITORY")!r}, not {repo}'
    if not isinstance(event, dict):
        return 'no event payload'
    kind = env.get('GITHUB_EVENT_NAME')
    if kind in ('pull_request', 'pull_request_target'):
        pr = event.get('pull_request') or {}
        for side in ('head', 'base'):
            got = ((pr.get(side) or {}).get('repo') or {}).get('full_name')
            if got != repo:
                return f'a pull request whose {side} is {got!r}'
        return None
    if kind not in OWN_EVENTS:
        return f'a {kind!r} event (it may carry code from another repository)'
    got = (event.get('repository') or {}).get('full_name')
    return None if got == repo else f'a {kind} event of repository {got!r}'


def static_refused(cfg, event):
    """Why no static pool may take this run, or None. Their runners are always there and have no job-started hook, so
    a workflow a pull request changed could name their labels: they serve a private repository only. Unknown refuses."""
    if not any(p.get('static') for p in cfg['pools']):
        return None
    got = ((event or {}).get('repository') or {}).get('private') if isinstance(event, dict) else None
    if got is not True:
        return f'the event does not say the repository is private (repository.private: {got!r})'
    return None


def hosted_of(cfg):
    """The hosted label (or False), checked again here: a config route is handed need not have passed `load`."""
    hosted = cfg['hosted']
    if hosted is not False and (not isinstance(hosted, str) or self_hosted(cfg, hosted)):
        raise ValueError(f'hosted is {hosted!r}: not a GitHub-hosted label (a self-hosted, pool or retired one)')
    return hosted


def always_on(cfg, kind, static_why):
    """The static pool that takes `kind` runs (the one that waits first), or None (none, or static pools refused)."""
    if static_why:
        return None
    pools = [p for p in cfg['pools'] if p.get('static') and kind in takes_of(p)]
    return next(iter(sorted(pools, key=lambda p: not p['waits'])), None)


def route(cfg, env, event, mode_value, gates, now=time.time, nonce=None, attempt=None):
    """(mode, {key: runs-on}); route_all without the fallback."""
    return route_all(cfg, env, event, mode_value, gates, now, nonce, attempt)[:2]


def route_all(cfg, env, event, mode_value, gates, now=time.time, nonce=None, attempt=None):
    """(mode, {key: runs-on}, fallback). Raises for a broken setting or CONFIG, and with "hosted": false for a run that
    may hold another repository's code or that no static pool may take (red: nothing can run it). It calls no API (a
    push: one anonymous git ls-remote, made_by_the_sync). With `attempt`, a run label ends with -a<attempt> already.
    Every runs-on is the hosted label, a static pool's runs_on, or a dispatcher pool's label plus this run's own label:
    never a label that nothing serves. `fallback` is the runs-on for a job of another attempt (a re-run of failed jobs
    does not re-run route, and a run label serves one attempt only): the always-on pool for a trusted run, else where
    this run's jobs went; never a dispatcher pool's label."""
    mode = mode_in(cfg, mode_value)
    hosted = hosted_of(cfg)
    keys = [key for key, _ in job_keys(cfg)]
    foreign = outside(cfg, env, event)
    if foreign:
        if hosted is False:
            raise ValueError(f'this run may hold code from another repository ({foreign}): it may never use a '
                             'self-hosted runner, and the config has no GitHub-hosted one ("hosted": false), so no job '
                             'of it can run')
        say(f'::notice::fork-fleet: GitHub-hosted for every job: {foreign} (only trusted runs may use the fleet; '
            f'never a self-hosted runner for a run that may hold another repository\'s code, in any mode: {hosted})')
        return mode, {key: hosted for key in keys}, hosted
    kind = kind_of(env, event)
    static_why = static_refused(cfg, event)
    if static_why:
        say(f'::error::fork-fleet: no static pool is used: {static_why} (static pools serve private repositories only)')
    home = always_on(cfg, kind, static_why)
    spare = hosted or (list(home['runs_on']) if home else None)
    if spare is None:
        raise ValueError(f'no runner may take this {kind} run: the config has no GitHub-hosted one ("hosted": false), '
                         f'and {static_why or "no static pool takes it"}')
    there = 'GitHub-hosted' if hosted else f'pool {home["name"]} (always there)'
    out = {key: spare for key in keys}
    if mode == 'github':
        say('GitHub-hosted for every job: the setting is github')
        return mode, out, spare
    why = refused(cfg, env, event)
    if why:
        say(f'::notice::fork-fleet: {there} for every job: {why} (only trusted runs may use the fleet; '
            + (f'never a self-hosted runner for them, in any mode: {hosted})' if hosted else
               'a run of this repository, and the config has no GitHub-hosted runner)'))
        return mode, out, spare
    fallback = list(home['runs_on']) if home else spare
    wanted = [key for key, job in job_keys(cfg) if runs(job, gates)]
    if not wanted:
        say(f'{there} for every job: no routed job runs for this change')
        return mode, out, fallback
    nonce = nonce or secrets.token_hex(8)   # this run's own label: no runner exists for it until the dispatcher makes one

    def labels(pool, key):
        if pool.get('static'):
            return list(pool['runs_on'])
        return [pool['label'], f'{pool["label"]}-{key}-{nonce}' + (f'-a{int(attempt)}' if attempt else '')]
    todo, t = list(wanted), now()
    for pool in cfg['pools']:
        if kind not in takes_of(pool):
            say(f'pool {pool["name"]}: does not take {kind} runs')
            continue
        if pool.get('static') and static_why:
            continue
        free, why = (pool['slots'], None) if pool.get('static') else beacon_free(cfg, pool, env, t)
        if why:
            # A pool its dispatcher closed (a new actions/runner release: the owner refreshes the template) or reports
            # down: a notice. A heartbeat gone stale or garbled (the dispatcher is down, its token expired or was
            # revoked): an error on the run page, or the jobs would go elsewhere unseen for days. A pool with no
            # heartbeat at all was never set up: a log line.
            seen = ('' if not env.get(pool['beacon']) else '::notice::fork-fleet: '
                    if why.startswith('its dispatcher says') else '::error::fork-fleet: ')
            say(f'{seen}pool {pool["name"]}: no free slot counted: {why}'
                + (' (is its dispatcher running, with a valid token? .github/fork-fleet.md)' if 'error' in seen else ''))
        for _ in range(min(free, pool.get('per_run', free))):
            if not todo:
                break
            key = todo.pop(0)
            out[key] = labels(pool, key)
            say(f'{key}: pool {pool["name"]} ' + ('(always there)' if pool.get('static') else
                                                  '(its dispatcher reports a free slot)'))
    waiting = next(p for p in cfg['pools'] if p['waits'] and kind in takes_of(p))
    if mode == 'fleet' and todo and not (waiting.get('static') and static_why):
        for key in todo:
            out[key] = labels(waiting, key)
            say(f'{key}: queues on pool {waiting["name"]} (fleet mode waits for a fleet runner)')
    elif todo:
        say(f'{there}, no free fleet slot left: {", ".join(todo)}')
    return mode, out, fallback


def with_vars(cfg, env):
    """env, with the setting and each dispatcher pool's heartbeat also read from $FLEET_VARS, the repository variables
    as one JSON object (the fleet-route action's `vars` input, toJSON(vars)). A variable set in env itself wins."""
    raw = env.get('FLEET_VARS')
    if not raw:
        return env
    try:
        got = json.loads(raw)
        if not isinstance(got, dict):
            raise ValueError(raw)
    except ValueError:
        say('::error::fork-fleet: the vars input is not a JSON object of repository variables: no heartbeat and no '
            'setting are read from it')
        return env
    env = dict(env)
    names = [p['beacon'] for p in dispatched(cfg)] + ([cfg['variable']] if cfg.get('variable') else [])
    for name in names:
        if not env.get(name) and isinstance(got.get(name), str):
            env[name] = got[name]
    if not env.get('FLEET_MODE') and cfg.get('variable'):
        env['FLEET_MODE'] = env.get(cfg['variable']) or ''
    return env


def cmd_route(cfg, env, with_attempt=False):
    env = with_vars(cfg, env)
    event, _ = read_event(env)
    gates = None
    try:
        gates = json.loads(env.get('GATES') or 'null')
    except ValueError:
        say('::warning::fork-fleet: the changes outputs cannot be read: every routed job counts as running')
    attempt = env.get('GITHUB_RUN_ATTEMPT') or '1'
    mode, out, fallback = route_all(cfg, env, event, env.get('FLEET_MODE'), gates, attempt=with_attempt and int(attempt))
    fleet = sum(1 for v in out.values() if not isinstance(v, str))
    lines = [f'runners={json.dumps(out, separators=(",", ":"))}', f'mode={mode}', f'attempt={attempt}',
             f'watch={"true" if fleet else "false"}', f'fallback={json.dumps(fallback, separators=(",", ":"))}']
    with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
        f.write(''.join(f'{line}\n' for line in lines))
    where = {}
    for key, labels in out.items():
        if isinstance(labels, str):
            shown = labels
        elif len(labels) == 2 and labels[1].startswith(labels[0] + '-'):
            shown = f'pool label {labels[0]}'
        else:
            shown = ', '.join(labels)
        where.setdefault(shown, []).append(key)
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
    mode = mode_in(cfg, env.get('FLEET_MODE'))
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
    fleet = mode_in(cfg, env.get('FLEET_MODE')) == 'fleet'
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

    def ready(self, newest):
        """HostError (with the launcher's reason) unless the pool's runner template is not older than `newest`."""
        self._must('ready', newest)

    def start(self, name, env_text):
        self._must('start', name, stdin=env_text)

    def stop(self, name):
        self._must('stop', name)

    def prune(self):
        self._must('prune')


def fleet_job(cfg, labels):
    """(pool, job key, attempt) when a job asks for exactly a pool's label and the run's own label for that job."""
    labels = list(labels or ())
    if len(labels) != 2 or labels[0] == labels[1]:
        return None
    keys = {k for k, _ in job_keys(cfg)}
    for pool in dispatched(cfg):
        if pool['label'] not in labels:
            continue
        other = labels[1] if labels[0] == pool['label'] else labels[0]
        prefix = pool['label'] + '-'
        m = NONCE.fullmatch(other[len(prefix):]) if other.startswith(prefix) else None
        return (pool, m['key'], int(m['attempt'])) if m and m['key'] in keys else None
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
    """Kept between passes only to pace heartbeats, idle checks, retries, messages and the newest runner release.
    GitHub (runs, jobs, runner registrations) and the host (units) hold everything else, so a restarted dispatcher
    picks up where one stopped."""
    return {'beacon': {}, 'idle': {}, 'tries': {}, 'said': set(), 'newest': None}


RELEASE_EVERY = 3600   # seconds between reads of the newest actions/runner release


def newest_runner(gh, state, now):
    """The newest actions/runner release (x.y.z), read at most once an hour. A failed read keeps the last one; with
    none yet it raises (TROUBLE), so no runner starts before the template has been checked against it."""
    last = state['newest']
    if last and now - last[1] < RELEASE_EVERY:
        return last[0]
    try:
        got = gh.call('/repos/actions/runner/releases/latest')
        m = VERSION.fullmatch(str(got.get('tag_name')))
        if not m:
            raise ValueError(f'actions/runner\'s latest release is {got.get("tag_name")!r}')
    except TROUBLE:
        if last:
            return last[0]
        raise
    state['newest'] = ('.'.join(m.groups()), now)
    return state['newest'][0]


def once(state, key, msg):
    if key not in state['said']:
        state['said'].add(key)
        say(msg)


def dispatch_pass(cfg, gh, hosts, state, now=time.time):
    """One pass for the pools in `hosts`. GitHub trouble stops the pass. A pool whose host fails (HostError) is left
    alone for the rest of the pass, with a warning and a heartbeat of 0 free slots; the other pools go on."""
    pools = [p for p in cfg['pools'] if p['name'] in hosts]
    down = {}     # pool name: why its host is left alone for the rest of this pass
    closed = {}   # pool name: why no runner starts on it this pass (its leftovers are still cleaned up)

    def host(p, verb, *args):
        """hosts[p].<verb>(*args); on a HostError, None and the pool is down for the rest of this pass."""
        try:
            return getattr(hosts[p['name']], verb)(*args)
        except HostError as e:
            down[p['name']] = str(e)
            say(f'::warning::fork-fleet: {e}; pool {p["name"]} is left alone for this pass, the other pools go on')
            return None

    runs = {}
    for status in ('queued', 'in_progress'):
        for run in gh.pages(f'actions/runs?status={status}', 'workflow_runs', most=3):
            runs[run['id']] = run
    queued = [(run, j) for run in runs.values() for j in gh.pages(f'actions/runs/{run["id"]}/jobs?filter=latest', 'jobs')
              if j.get('status') == 'queued' and not j.get('runner_name')]
    queued_ids = {j['id'] for _, j in queued}
    state['said'] = {k for k in state['said'] if k[0] in queued_ids or k[0] == 'ready'}
    state['tries'] = {k: v for k, v in state['tries'].items() if k in queued_ids}
    newest = newest_runner(gh, state, now())
    units = {}
    for p in pools:
        got = host(p, 'units')
        if got is None:
            continue
        units[p['name']] = got
        try:
            hosts[p['name']].ready(newest)
        except HostError as e:
            closed[p['name']] = str(e)
            once(state, ('ready', p['name'], str(e)), f'::warning::fork-fleet: {e}')
    ours = {}
    for r in gh.pages('actions/runners', 'runners'):
        for p in pools:
            if hosts[p['name']].mine.fullmatch(r.get('name') or ''):
                ours[r['name']] = (p, r)
    state['idle'] = {k: v for k, v in state['idle'].items() if k in ours}

    # Leftovers: a registration with no runner behind it, and a runner whose job went away before it started. Such a
    # runner is stopped at once when a queued job of another run could be given it (the hook would refuse that job),
    # else after idle_passes passes (a runner that is just taking its own job is not busy yet). A runner's labels name
    # the run attempt it was made for, so no job of another attempt can be given it.
    for name, (p, r) in ours.items():
        if p['name'] in down:
            continue
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
                host(p, 'stop', name)
                if p['name'] in down:
                    continue
                state['idle'].pop(name)
                say(f'{name}: stopped, its job is no longer queued')
        else:
            state['idle'].pop(name, None)
    for p in pools:
        if p['name'] not in down:
            host(p, 'prune')

    # Launches: one runner per queued fleet job of a trusted run.
    active = {name: len(got) for name, got in units.items()}
    pending = {p['name']: 0 for p in pools}
    started_mb = {p['name']: 0 for p in pools}   # memory of runners this pass started, not in use yet
    memory = {k: j['memory_mb'] for k, j in job_keys(cfg)}
    trust = {}
    for run, j in queued:
        hit = fleet_job(cfg, j.get('labels'))
        if not hit or hit[0]['name'] not in hosts:
            continue
        pool, key, attempt = hit
        if j.get('run_attempt', run.get('run_attempt')) != run.get('run_attempt'):
            continue   # re-run after the run list was read: its runner would be made for the old attempt; next pass
        if attempt != run.get('run_attempt'):
            continue   # its label names another attempt: the gate would refuse it on any runner
        name = f'{pool["label"]}-{int(j["id"])}'
        if pool['name'] in down or name in units[pool['name']] or name in ours:
            continue   # its host failed; or started already (its runner is coming up), or a leftover deleted above
        tag = f'job {j["id"]} ({j.get("name")}) of run {run["id"]}'
        if (run['id'], run.get('run_attempt')) not in trust:
            trust[(run['id'], run.get('run_attempt'))] = run_refused(cfg, gh, run)
        why = trust[(run['id'], run.get('run_attempt'))]
        if why:
            once(state, (j['id'], 'trust'), f'{tag}: no runner, not a trusted run: {why}')
            continue
        pending[pool['name']] += 1
        if pool['name'] in closed:
            continue
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
        fits = host(pool, 'fits', memory[key] + started_mb[pool['name']])
        if pool['name'] in down:
            continue
        if not fits:
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
        host(pool, 'start', name, runner_env(got.get('encoded_jit_config'), run))
        if pool['name'] in down:
            continue   # the next pass deletes its registration (no runner behind it) and tries again
        active[pool['name']] += 1
        pending[pool['name']] -= 1
        started_mb[pool['name']] += memory[key]
        say(f'{name}: started for {tag}, attempt {run.get("run_attempt")}')

    # Heartbeats: what `route` may count on, per pool, with the reason when it is nothing.
    t = now()
    biggest = max(memory.values())
    for p in pools:
        why = down.get(p['name']) or closed.get(p['name'])
        free = 0 if why else max(p['slots'] - active[p['name']] - pending[p['name']], 0)
        if free and not host(p, 'fits', biggest + started_mb[p['name']]):
            free, why = 0, down.get(p['name'])
        beat = {'at': int(t), 'free': free}
        if why:
            beat['why'] = why[:300]
        last = state['beacon'].get(p['name'])
        if last and last[0] == (free, why) and t - last[1] < cfg['dispatch']['beacon_seconds']:
            continue
        set_variable(gh, p['beacon'], json.dumps(beat, separators=(',', ':')))
        state['beacon'][p['name']] = ((free, why), t)
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
        os.close(fd)
        raise ValueError(f'another dispatcher holds {path}') from None
    return fd


def take_locks(lock_dir, names):
    """One lock per pool served: a second dispatcher may serve the other pools, never one this one serves."""
    fds = []
    try:
        for name in sorted(set(names)):
            fds.append(take_lock(os.path.join(lock_dir, f'dispatch-{name}.lock')))
    except ValueError:
        for fd in fds:
            os.close(fd)
        raise
    return fds


def cmd_dispatch(cfg, args, sleep=time.sleep):
    names = args.pool or [p['name'] for p in dispatched(cfg)]
    unknown = sorted(set(names) - {p['name'] for p in dispatched(cfg)})
    if unknown:
        raise ValueError(f'no pool {", ".join(unknown)} with a dispatcher in the config')
    token = read_token(args.token_file)
    locks = take_locks(args.lock_dir, names)
    gh = GitHub(token, cfg['repository'])
    hosts = {p['name']: Host(p) for p in dispatched(cfg) if p['name'] in names}
    state = new_state()
    while True:
        try:
            dispatch_pass(cfg, gh, hosts, state)
        except (*TROUBLE, HostError) as e:
            say(f'::warning::fork-fleet: this pass stopped ({e.__class__.__name__}: {e}); the next one starts over')
            if args.once:
                return 1
        if args.once:
            for fd in locks:
                os.close(fd)
            return 0
        sleep(cfg['dispatch']['every_seconds'])


# ---------------------------------------------------------------- main

def main(argv=None, env=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', default=CONFIG)
    sub = ap.add_subparsers(dest='cmd', required=True)
    sub.add_parser('route').add_argument('--with-attempt', action='store_true',
                                         help="end each run label with -a<this attempt> (the job's runs-on does "
                                              "not). A job whose github.run_attempt is not the attempt output (a re-run "
                                              "of failed jobs) must use the fallback output instead")
    sub.add_parser('gate')
    sub.add_parser('watch')
    sub.add_parser('sweep')
    dis = sub.add_parser('dispatch', allow_abbrev=False)   # --lock (one lock for all pools) is gone: no prefix match
    dis.add_argument('--pool', action='append', help='a pool this host serves (default: every pool)')
    dis.add_argument('--token-file', default='/etc/fork-fleet/token')
    dis.add_argument('--lock-dir', default='/run/fork-fleet', help='where dispatch-<pool>.lock is taken, one per pool')
    dis.add_argument('--once', action='store_true', help='one pass, then exit (1 when it stopped half-way)')
    args = ap.parse_args(argv)
    cfg = load(args.config)
    if args.cmd == 'gate':
        return gate(cfg, env)
    if args.cmd == 'route':
        return cmd_route(cfg, env, args.with_attempt)
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
