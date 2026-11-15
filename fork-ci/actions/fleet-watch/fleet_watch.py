#!/usr/bin/env python3
"""fleet-watch: re-runs, once, what a dispatcher pool's runner did not finish (README.md beside this file).

  watch --run-id N                  a run completed: re-run its failed jobs once when a dispatcher job was cut off
  sweep                             cancel a run waiting too long for a dispatcher pool; watch a red run no watch did
  pending --run-id N --attempt A    will this red attempt be re-run? pending=true only when sure

GitHub's record of each run (its latest attempt, who started it) is the only state; at most MAX_CALLS GitHub calls.
"""
import argparse, importlib.util, os, re, sys, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location(
    'fork_fleet', os.path.join(HERE, '..', '..', '..', '.github', 'scripts', 'fork-fleet.py'))
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)
say = ff.say

MAX_CALLS = 200        # GitHub calls one run of this script makes at most; the next sweep goes on from GitHub's record
WINDOW = 24 * 3600     # a run that ended longer ago is never re-run
CATCH_UP = 3600        # the sweep re-runs (as `watch` would) red runs that ended this long ago at most
MAX_ATTEMPT = 2        # attempts this script re-runs at most: 1 and 2 (a person's re-run); 3 and later by hand only
LOG_MAX = 8 << 20      # bytes of a job's log read for its error lines
RED = ('failure', 'timed_out', 'cancelled', 'startup_failure')
SET_UP = ('Set up job', 'Set up runner')   # the runner's own steps: its set-up, and the job-started hook (the gate)
MACHINE = (re.compile(r'Process completed with exit code 137\b'),)   # a step killed (SIGKILL), most often for memory


class Spent(Exception):
    pass


class GitHub(ff.GitHub):
    """The router's client with a call budget, and a job's log (a redirect to storage that must not get the token)."""

    def __init__(self, token, repo, most=MAX_CALLS):
        super().__init__(token, repo)
        self.left = most

    def spend(self):
        if self.left <= 0:
            raise Spent(f'{MAX_CALLS} GitHub calls made: the rest waits for the next sweep')
        self.left -= 1

    def call(self, path, method='GET', data=None):
        self.spend()
        return super().call(path, method, data)

    def log(self, job_id):
        self.spend()
        if not self.token:
            raise ValueError('no token for a job log')

        class Stay(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None   # urllib would send the token on to the storage host
        req = urllib.request.Request(f'{ff.API}/repos/{self.repo}/actions/jobs/{int(job_id)}/logs', headers={
            'Accept': 'application/vnd.github+json', 'User-Agent': 'fleet-watch',
            'Authorization': f'Bearer {self.token}'})
        try:
            with urllib.request.build_opener(Stay).open(req, timeout=30) as r:
                return r.read(LOG_MAX).decode('utf-8', 'replace')
        except urllib.error.HTTPError as e:
            where = e.headers.get('Location') if e.code in (301, 302, 303, 307, 308) else None
            if not (where and where.startswith('https://')):
                raise
        with urllib.request.urlopen(urllib.request.Request(where, headers={'User-Agent': 'fleet-watch'}),
                                    timeout=60) as r:
            return r.read(LOG_MAX).decode('utf-8', 'replace')


def settings(cfg):
    """The config's optional "watch" section: followers (names of jobs whose red only reports other jobs: a summary
    job, the job that bounds the wait) and errors (patterns of a job log's error lines that mean the machine, not the
    change, broke the job)."""
    got = cfg.get('watch', {})
    if not isinstance(got, dict):
        raise ValueError('watch must be an object')
    names, errors = got.get('followers', []), got.get('errors', [])
    if not (isinstance(names, list) and all(isinstance(n, str) and n for n in names)):
        raise ValueError('watch.followers must be a list of job names')
    if not (isinstance(errors, list) and all(isinstance(e, str) and e for e in errors)):
        raise ValueError('watch.errors must be a list of patterns')
    try:
        return names, MACHINE + tuple(re.compile(e) for e in errors)
    except re.error as e:
        raise ValueError(f'watch.errors: {e}') from None


def pool_of(cfg, job):
    """The dispatcher pool a job asked for (a retired label counts as one that does not wait), or None."""
    labels = set(job.get('labels') or ())
    for p in ff.dispatched(cfg):
        if p['label'] in labels:
            return p
    if labels & set(cfg.get('retired_labels', [])):
        return {'name': 'retired', 'waits': False}
    return None


def bound(cfg, pool, mode):
    """How long a job may wait for this pool: on the pool fleet mode queues on, fleet_stuck_seconds."""
    return cfg['fleet_stuck_seconds'] if mode == 'fleet' and pool.get('waits') else cfg['stuck_seconds']


def waited(job, now=None):
    start = ff.stamp(job.get('created_at'))
    end = now if now is not None else ff.stamp(job.get('completed_at'))
    return int(end - start) if start and end else 0


def untrusted(cfg, run):
    """Why this script leaves the run alone whatever its jobs did, or None. From GitHub's record of its latest
    attempt: its repository, workflows, events and branches, and an attempt a trusted actor started. The re-run this
    script starts is not one, so each attempt a person started is re-run at most once."""
    trusted, repo = cfg['trusted'], cfg['repository']
    if (run.get('repository') or {}).get('full_name') != repo:
        return 'another repository'
    if (run.get('path') or '').split('@')[0] not in trusted['workflows']:
        return f'workflow {run.get("path")!r} is not one of {trusted["workflows"]}'
    head, event = (run.get('head_repository') or {}).get('full_name'), run.get('event')
    if event not in ('push', 'pull_request') or head != repo:
        return f'a {event!r} run whose head is {head!r}'
    if event == 'push' and run.get('head_branch') not in trusted['push_branches']:
        return f'a push to {run.get("head_branch")!r}'
    for who in ('actor', 'triggering_actor'):
        login = (run.get(who) or {}).get('login')
        if login not in trusted['actors']:
            return f'its {who} is {login!r}, not a trusted actor (a re-run this script started is never re-run)'
    if int(run.get('run_attempt') or 1) > MAX_ATTEMPT:
        return f'attempt {run.get("run_attempt")}: from attempt {MAX_ATTEMPT + 1} on, re-runs are by hand'
    return None


def follows(name, followers):
    return any(name == f or name.startswith(f + ' (') for f in followers)


def sort(cfg, run, job, mode, followers):
    """('cut', why): the dispatcher runner did not finish the job; ('log', None): the job's log decides; ('own', None):
    it failed by itself; (None, None): it passed, or its red says nothing either way."""
    if job.get('conclusion') not in RED or follows(job.get('name') or '', followers):
        return None, None
    pool, ran = pool_of(cfg, job), job.get('runner_name')
    if pool and not ran:
        if waited(job) >= bound(cfg, pool, mode):
            return 'cut', f'it waited {waited(job) // 60} min for pool {pool["name"]} and never started'
        return None, None   # cancelled before its wait was too long: a person or a newer push did that
    if job.get('conclusion') == 'cancelled' or run.get('conclusion') == 'cancelled':
        return None, None   # cancelled while it ran: that stands
    failed = [s.get('name') for s in job.get('steps') or [] if s.get('conclusion') == 'failure']
    if not failed or all(name in SET_UP for name in failed):
        if not pool:
            return None, None   # an always-on or GitHub-hosted runner's trouble: not this script's to re-run
        if not failed:
            return 'cut', f'it ended {job["conclusion"]} on {ran} with no failed step (the runner stopped or lost ' \
                          'its network)'
        return 'cut', f'it failed in {failed[0]!r}, before any step of its own (the runner, or the gate refused it)'
    return ('log' if pool else 'own'), None


def plan(cfg, gh, run, mode, now):
    """(cut [(job name, why)], why nothing is re-run or None). Cut jobs are re-run only when no other job failed by
    itself: then that red stands whatever a re-run says. Logs are read last, and only until one shows a job's own
    failure."""
    why = untrusted(cfg, run)
    if why:
        return [], why
    if run.get('status') != 'completed':
        return [], f'it is {run.get("status")}'
    if now - (ff.stamp(run.get('updated_at')) or 0) > WINDOW:
        return [], 'it ended more than 24 hours ago'
    followers, machine = settings(cfg)
    jobs = gh.pages(f'actions/runs/{int(run["id"])}/attempts/{int(run.get("run_attempt") or 1)}/jobs', 'jobs', most=3)
    cut, logs = [], []
    for j in jobs:
        what, why = sort(cfg, run, j, mode, followers)
        if what == 'own':
            return [], f'{j["name"]} failed by itself: that red stands, nothing is re-run'
        if what == 'cut':
            cut.append((j['name'], why))
        elif what == 'log':
            logs.append(j)
    if not cut and not logs:
        return [], 'no job was cut off on a dispatcher pool'
    for j in logs:
        try:
            lines = [x.split('##[error]', 1)[1] for x in gh.log(j['id']).splitlines() if '##[error]' in x]
        except ff.TROUBLE as e:
            return [], f'the log of {j["name"]} cannot be read ({e.__class__.__name__}): unsure, the red stands'
        hit = next((x for x in lines if any(p.search(x) for p in machine)), None)
        if hit is None:
            return [], f'{j["name"]} failed by itself: that red stands, nothing is re-run'
        cut.append((j['name'], f'its error {hit.strip()[:120]!r} comes from the machine, not the change'))
    newest = gh.call(f'actions/workflows/{urllib.parse.quote(os.path.basename(run["path"].split("@")[0]))}/runs?'
                     f'branch={urllib.parse.quote(run.get("head_branch") or "", safe="")}&event={run["event"]}'
                     '&per_page=1').get('workflow_runs') or []
    if newest and newest[0].get('id') != run['id'] and newest[0].get('head_sha') != run.get('head_sha'):
        return [], f'a newer run of {run.get("head_branch")!r} exists (run {newest[0].get("id")})'
    return cut, None


def rerun(gh, run, cut):
    """Re-runs the run's failed and cancelled jobs (and the jobs that need them). False when a re-run is already going
    (another watch or sweep started it: GitHub refuses a second)."""
    rid, attempt = int(run['id']), int(run.get('run_attempt') or 1)
    try:
        gh.call(f'actions/runs/{rid}/rerun-failed-jobs', 'POST')
    except urllib.error.HTTPError as e:
        if not 400 <= e.code < 500:
            raise
        got = gh.call(f'actions/runs/{rid}')
        if int(got.get('run_attempt') or 1) > attempt or got.get('status') != 'completed':
            say(f'run {rid}: a re-run is already going (attempt {got.get("run_attempt")})')
            return False
        raise
    for name, why in cut:
        say(f'run {rid}: {name}: {why}: its failed jobs re-run once (attempt {attempt + 1}, on the fallback runners)')
    return True


def watch(cfg, gh, run, mode, now):
    cut, why = plan(cfg, gh, run, mode, now)
    if why:
        say(f'run {run.get("id")} attempt {run.get("run_attempt")}: not re-run: {why}')
        return False
    return rerun(gh, run, cut)


def sweep(cfg, gh, mode, now, notes):
    """Cancels a run whose dispatcher job waits too long, and re-runs a red run no watch re-ran. Returns the number of
    troubles; what was not done is done by the next sweep, from GitHub's record."""
    trouble, since = 0, time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(now - WINDOW))
    for path in cfg['trusted']['workflows']:
        base = f'actions/workflows/{urllib.parse.quote(os.path.basename(path))}/runs'
        try:
            busy = [r for s in ('queued', 'in_progress') for r in gh.pages(f'{base}?status={s}', 'workflow_runs', 1)]
            done = gh.pages(f'{base}?status=completed&created=%3E%3D{since}', 'workflow_runs', 1)
        except Spent:
            raise
        except ff.TROUBLE as e:
            say(f'::warning::fleet-watch: the runs of {path} cannot be listed ({e.__class__.__name__}: {e})')
            trouble += 1
            continue
        for run in busy + done:
            try:
                if untrusted(cfg, run):
                    continue
                if run.get('status') == 'completed':
                    if run.get('conclusion') in RED and now - (ff.stamp(run.get('updated_at')) or 0) <= CATCH_UP \
                            and watch(cfg, gh, run, mode, now):
                        notes.append(f'run {run["id"]} re-run (its watch did not)')
                    continue
                jobs = gh.pages(f'actions/runs/{int(run["id"])}/attempts/{int(run.get("run_attempt") or 1)}/jobs',
                                'jobs', most=3)
                late = [j for j in jobs if j.get('status') == 'queued' and not j.get('runner_name') and pool_of(cfg, j)
                        and waited(j, now) >= bound(cfg, pool_of(cfg, j), mode)]
                if not late:
                    continue
                try:
                    gh.call(f'actions/runs/{int(run["id"])}/cancel', 'POST')
                except urllib.error.HTTPError as e:
                    if e.code != 409:
                        raise
                    continue   # it ended since it was listed: its watch takes it
                j = late[0]
                why = f'{j["name"]} waited {waited(j, now) // 60} min for pool {pool_of(cfg, j)["name"]}'
                say(f'::warning::fleet-watch: run {run["id"]}: {why}: cancelled; its watch re-runs it on the fallback')
                notes.append(f'run {run["id"]} cancelled: {why}')
            except Spent:
                raise
            except ff.TROUBLE as e:
                trouble += 1
                say(f'::warning::fleet-watch: run {run.get("id")}: {e.__class__.__name__}: {e}; the next sweep tries '
                    'again')
    return trouble


def pending(cfg, gh, run_id, attempt, mode, now):
    run = gh.call(f'actions/runs/{int(run_id)}')
    if int(run.get('run_attempt') or 1) > attempt:
        return True, f'attempt {run["run_attempt"]} runs or ran after attempt {attempt}: it decides'
    cut, why = plan(cfg, gh, run, mode, now)
    if why:
        return False, why
    return True, f'{", ".join(name for name, _ in cut)} cut off: its watch re-runs it, that attempt decides'


def main(argv=None, env=None, gh=None, now=None):
    env = os.environ if env is None else env
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--config', required=True)
    ap.add_argument('verb', choices=('watch', 'sweep', 'pending'))
    ap.add_argument('--run-id', default='')
    ap.add_argument('--attempt', default='')
    args = ap.parse_args(argv)
    if args.verb != 'sweep' and not re.fullmatch(r'[1-9][0-9]{0,19}', args.run_id):
        raise ValueError(f'{args.verb} needs --run-id, a run id (got {args.run_id!r})')
    if args.verb == 'pending' and not re.fullmatch(r'[1-9][0-9]{0,4}', args.attempt):
        raise ValueError(f'pending needs --attempt, the red attempt (got {args.attempt!r})')
    cfg = ff.load(args.config)
    settings(cfg)
    env = ff.with_vars(cfg, env)
    mode, now = ff.mode_in(cfg, env.get('FLEET_MODE')), now or time.time()
    gh = gh or GitHub(env.get('GH_TOKEN') or env.get('GITHUB_TOKEN'), cfg['repository'])
    out, notes, code = [], [], 0
    try:
        if args.verb == 'sweep':
            code = 1 if sweep(cfg, gh, mode, now, notes) else 0
        elif args.verb == 'watch':
            run = gh.call(f'actions/runs/{int(args.run_id)}')
            out.append(f'rerun={"true" if watch(cfg, gh, run, mode, now) else "false"}')
        else:
            yes, why = pending(cfg, gh, args.run_id, int(args.attempt), mode, now)
            say(f'run {args.run_id} attempt {args.attempt}: {"pending" if yes else "not pending"}: {why}')
            out.append(f'pending={"true" if yes else "false"}')
    except Spent as e:
        say(f'::warning::fleet-watch: {e}')
        code = 1
    if out and env.get('GITHUB_OUTPUT'):
        with open(env['GITHUB_OUTPUT'], 'a', encoding='utf-8') as f:
            f.write(''.join(f'{line}\n' for line in out))
    ff.summary(env, notes)
    return code


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:   # pending's output stays empty: the caller counts that as red
        say(f'::error::fleet-watch: {e.__class__.__name__}: {e}')
        sys.exit(1)
