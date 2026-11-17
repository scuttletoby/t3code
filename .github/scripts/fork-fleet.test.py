#!/usr/bin/env python3
"""Tests of fork-fleet.py, fork-fleet-launch.sh and fork-fleet-hook.sh, and of their wiring in fork-tests.yml,
fork-fleet.yml and fork-release.yml. Run: python3 <this>. Linux only (the gate and launcher tests run real processes)."""
import copy, datetime, http.client, importlib.util, io, json, os, pwd, re, shutil, signal, socket, stat, subprocess, sys
import tempfile, time
import unittest, urllib.error, urllib.parse
from contextlib import redirect_stdout
from functools import partial

sys.dont_write_bytecode = True   # importing fork-fleet.py must not leave __pycache__ in the tree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LAUNCH = os.path.join(HERE, 'fork-fleet-launch.sh')


def job_paths():
    """THE allow list of system paths a fleet job sees (fork-fleet-job-paths.txt, as the launcher reads it)."""
    with open(os.path.join(HERE, 'fork-fleet-job-paths.txt')) as f:
        return [x.strip() for x in f if x.strip() and not x.lstrip().startswith('#')]
_spec = importlib.util.spec_from_file_location('fork_fleet', os.path.join(HERE, 'fork-fleet.py'))
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)

CFG = ff.load()
REPO = CFG['repository']
OWNER = CFG['trusted']['actors'][0]
WORKFLOW = CFG['trusted']['workflows'][0]
POOLS = {p['name']: p for p in CFG['pools']}
FIRST, SECOND = CFG['pools'][0]['name'], CFG['pools'][1]['name']   # route fills pools in this order
WAITING = next(p for p in CFG['pools'] if p['waits'])['name']
KEYS = [k for k, _ in ff.job_keys(CFG)]
MEMORY = {k: j['memory_mb'] for k, j in ff.job_keys(CFG)}
HOSTED = CFG['hosted']
NONCE = '0123456789abcdef'
NEWEST_RUNNER = '2.330.0'
T0 = 1_800_000_000
SHA = '0123456789abcdef0123456789abcdef01234567'      # a pushed commit
SYNCED = 'feedface' * 5                                # a commit the upstream sync tagged
UNKNOWN = 'decafbad' * 5                               # a commit whose tag cannot be read (git fails)
SYNC_TAG = CFG['upstream_sync']['tag_prefix']
GIT_ENV = {'PATH': '/usr/bin:/bin', 'GIT_CONFIG_GLOBAL': '/dev/null', 'GIT_CONFIG_NOSYSTEM': '1', 'LC_ALL': 'C',
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}


def data_roots():
    """The folders that hold data, as the launcher's no_data has them (one source): it refuses a bound path there."""
    with open(LAUNCH) as f:
        body = f.read().split('\nno_data() {\n', 1)[1].split('\n}\n', 1)[0]
    roots = re.findall(r'(/[a-z]+)/\*', body.split(')', 1)[0])
    assert '/home' in roots, body
    return roots


def launcher_tmp():
    """A temp folder for the launcher's tests, which bind it (WORK_ROOT, HOOK, RESOLV_CONF): never under a folder that
    holds data, which the launcher refuses, even when TMPDIR is one (a local run with TMPDIR under /home)."""
    base = os.path.realpath(tempfile.gettempdir())
    if any(base == r or base.startswith(r + '/') for r in data_roots()):
        base = next(d for d in ('/tmp', '/var/tmp') if os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK))
    return tempfile.mkdtemp(dir=base)


def fake_ls_remote(argv, **kw):
    """`git ls-remote --exit-code <repo> <ref>` of this repository: SYNCED's tag exists, UNKNOWN's read fails."""
    code = 0 if argv[-1].endswith(SYNCED) else 128 if argv[-1].endswith(UNKNOWN) else 2
    return subprocess.CompletedProcess(argv, code, '', '')


REAL_MADE_BY_THE_SYNC = ff.made_by_the_sync


def made_by_the_sync_offline(cfg, sha, run=fake_ls_remote):
    return REAL_MADE_BY_THE_SYNC(cfg, sha, run=run)


ff.made_by_the_sync = made_by_the_sync_offline   # every test but UpstreamSync and GateProcess: no network


def sync_remote(top):
    """(SYNC_REMOTE of a local bare copy of this repository, a commit the sync tagged there) for the real git."""
    def git(*a):
        return subprocess.run(['git', *a], env=GIT_ENV, check=True, capture_output=True, text=True).stdout.strip()
    remote = os.path.join(top, 'remote', '{}.git')
    work = os.path.join(top, 'work')
    git('init', '-q', '--bare', remote.format(REPO))
    git('init', '-q', work)
    git('-C', work, 'commit', '-q', '--allow-empty', '-m', 'upstream')
    sha = git('-C', work, 'rev-parse', 'HEAD')
    git('-C', work, 'push', '-q', remote.format(REPO), f'HEAD:refs/tags/{SYNC_TAG}{sha}')
    return 'file://' + remote, sha


def env(**over):
    e = {'GITHUB_REPOSITORY': REPO, 'GITHUB_WORKFLOW_REF': f'{REPO}/{WORKFLOW}@refs/heads/main',
         'GITHUB_ACTOR': OWNER, 'GITHUB_TRIGGERING_ACTOR': OWNER, 'GITHUB_EVENT_NAME': 'push',
         'GITHUB_REF': 'refs/heads/main', 'GITHUB_SHA': SHA, 'GITHUB_RUN_ID': '5', 'GITHUB_RUN_ATTEMPT': '1'}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


PUSH = {'repository': {'full_name': REPO}}
PR_ENV = dict(GITHUB_EVENT_NAME='pull_request', GITHUB_REF='refs/pull/7/merge')


def pr(head=REPO, base=REPO, user=OWNER):
    return {'pull_request': {'head': {'repo': {'full_name': head}}, 'base': {'repo': {'full_name': base}},
                             'user': {'login': user}}}


def labels(pool, key, nonce=NONCE):
    """What `route` puts out for a fleet job."""
    return [POOLS[pool]['label'], f'{POOLS[pool]["label"]}-{key}-{nonce}']


def job_labels(pool, key, nonce=NONCE, attempt=1):
    """The labels a fleet job of that run attempt asks for: runs-on adds the attempt to route's run label."""
    pool_label, run_label = labels(pool, key, nonce)
    return [pool_label, f'{run_label}-a{attempt}']


def quiet(fn, *a, **k):
    out = io.StringIO()
    with redirect_stdout(out):
        got = fn(*a, **k)
    return got, out.getvalue()


def iso(t):
    return ff.time.strftime('%Y-%m-%dT%H:%M:%SZ', ff.time.gmtime(t))


def http_error(code):
    return urllib.error.HTTPError('https://api.github.test/x', code, 'no', None, None)


def a_run(rid=5, *, actor=OWNER, triggering=None, event='push', branch='main', attempt=1, status='queued',
          conclusion=None, prs=(), head_repo=REPO, repo=REPO, path=WORKFLOW, sha=SHA):
    """A workflow run as GitHub's API records it."""
    return {'id': rid, 'run_attempt': attempt, 'status': status, 'conclusion': conclusion, 'event': event,
            'head_branch': branch, 'head_sha': sha, 'path': path, 'repository': {'full_name': repo},
            'head_repository': {'full_name': head_repo}, 'actor': {'login': actor},
            'triggering_actor': {'login': triggering or actor}, 'pull_requests': [{'number': n} for n in prs]}


def a_job(jid, job_labels, *, status='queued', runner=None, conclusion=None, created=T0, completed=None, steps=(),
          name=None):
    return {'id': jid, 'name': name or f'job {jid}', 'labels': list(job_labels), 'status': status,
            'runner_name': runner, 'conclusion': conclusion, 'created_at': iso(created),
            'completed_at': iso(completed) if completed else None, 'steps': list(steps)}


class FakeGitHub:
    """This repository as GitHub's API shows it: runs and their jobs, runners, pull requests, branches, variables.
    Records every call; `trouble` is [(regex of 'METHOD path', exception)]."""

    def __init__(self):
        self.runs, self.jobs, self.pulls, self.branches, self.variables = {}, {}, {}, {}, {}
        self.runners, self.calls, self.trouble = [], [], []
        self.on_cancel = None
        self.newest = NEWEST_RUNNER
        self.ids = iter(range(900, 10_000))

    def add(self, run, *jobs):
        self.runs[run['id']] = run
        self.jobs[run['id']] = list(jobs)
        return run

    def own_pr(self, number=7, head=REPO, base=REPO, user=OWNER):
        self.pulls[number] = {'number': number, 'head': {'repo': {'full_name': head}},
                              'base': {'repo': {'full_name': base}}, 'user': {'login': user}}

    def pages(self, path, key, most=10):
        return self.call(path)[key]

    def writes(self):
        return [(m, p) for m, p, _ in self.calls if m != 'GET']

    def call(self, path, method='GET', data=None):
        self.calls.append((method, path, data))
        route = f'{method} {path}'
        for match, error in self.trouble:
            if re.fullmatch(match, route):
                raise error
        if m := re.fullmatch(r'GET actions/runs\?status=(\w+)', route):
            return {'workflow_runs': [r for r in self.runs.values() if r['status'] == m[1]]}
        if re.fullmatch(r'GET actions/workflows/[\w.-]+/runs\?created=%3E%3D[\dT:Z-]+', route):
            return {'workflow_runs': sorted(self.runs.values(), key=lambda r: -r['id'])}
        if m := re.fullmatch(r'GET actions/runs/(\d+)/(?:jobs\?filter=latest|attempts/\d+/jobs)', route):
            return {'jobs': copy.deepcopy(self.jobs[int(m[1])])}
        if m := re.fullmatch(r'GET actions/runs/(\d+)', route):
            return copy.deepcopy(self.runs[int(m[1])])
        if m := re.fullmatch(r'POST actions/runs/(\d+)/(cancel|rerun)', route):
            if m[2] == 'cancel' and self.on_cancel:
                self.on_cancel(int(m[1]))
            return {}
        if m := re.fullmatch(r'GET branches/(.+)', route):
            name = urllib.parse.unquote(m[1])
            if name not in self.branches:
                raise http_error(404)
            return {'name': name, 'commit': {'sha': self.branches[name]}}
        if m := re.fullmatch(r'GET pulls/(\d+)', route):
            return copy.deepcopy(self.pulls[int(m[1])])
        if route == 'GET /repos/actions/runner/releases/latest':
            return {'tag_name': f'v{self.newest}'}
        if route == 'GET actions/runners':
            return {'runners': copy.deepcopy(self.runners)}
        if route == 'POST actions/runners/generate-jitconfig':
            if any(r['name'] == data['name'] for r in self.runners):
                raise http_error(409)
            rid = next(self.ids)
            self.runners.append({'id': rid, 'name': data['name'], 'status': 'offline', 'busy': False,
                                 'labels': [{'name': x} for x in ['self-hosted', 'Linux', 'X64'] + data['labels']]})
            return {'runner': {'id': rid, 'name': data['name']}, 'encoded_jit_config': f'SklUc2VjcmV0{rid}=='}
        if m := re.fullmatch(r'DELETE actions/runners/(\d+)', route):
            if not any(r['id'] == int(m[1]) for r in self.runners):
                raise http_error(404)
            self.runners = [r for r in self.runners if r['id'] != int(m[1])]
            return {}
        if m := re.fullmatch(r'PATCH actions/variables/(\w+)', route):
            if m[1] not in self.variables:
                raise http_error(404)
            self.variables[m[1]] = data['value']
            return {}
        if route == 'POST actions/variables':
            if data['name'] in self.variables:
                raise http_error(409)
            self.variables[data['name']] = data['value']
            return {}
        raise AssertionError(f'unexpected call {route}')


class Config(unittest.TestCase):
    def test_the_repository_config_is_valid(self):
        self.assertEqual(ff.check_config(CFG), [])

    def test_broken_configs_are_refused(self):
        cases = {
            'a generic label': (lambda c: c['pools'][0].__setitem__('label', 'self-hosted'), 'label'),
            'two pools, one label': (lambda c: c['pools'][1].__setitem__('label', c['pools'][0]['label']), 'label'),
            'a beacon GitHub owns': (lambda c: c['pools'][0].__setitem__('beacon', 'GITHUB_X'), 'beacon'),
            'no launch command': (lambda c: c['pools'][0].__setitem__('launch', []), 'launch'),
            'no waiting pool': (lambda c: [p.__setitem__('waits', False) for p in c['pools']], 'waits'),
            'two waiting pools': (lambda c: [p.__setitem__('waits', True) for p in c['pools']], 'waits'),
            'no slots': (lambda c: c['pools'][0].__setitem__('slots', 0), 'slots'),
            'a job id with a dash': (lambda c: c['jobs'][0].__setitem__('job', 'a-b'), 'id'),
            'a dispatcher pace of 0': (lambda c: c['dispatch'].__setitem__('every_seconds', 0), 'every_seconds'),
            'no stuck bound': (lambda c: c.pop('stuck_seconds'), 'stuck_seconds'),
            'an upstream sync with no tag prefix': (lambda c: c['upstream_sync'].pop('tag_prefix'), 'upstream_sync'),
            'a sync tag prefix without its /': (lambda c: c['upstream_sync'].__setitem__('tag_prefix', 'synced'),
                                                'tag_prefix'),
        }
        for name, (change, word) in cases.items():
            with self.subTest(name):
                bad = copy.deepcopy(CFG)
                change(bad)
                problems = ff.check_config(bad)
                self.assertTrue(any(word in p for p in problems), problems)

    def test_shards_expand_to_keys(self):
        self.assertIn('test_server-8', KEYS)
        self.assertNotIn('test_server', KEYS)
        self.assertIn('lint', KEYS)

    def test_the_setting(self):
        self.assertEqual(ff.mode_of(None), 'auto')
        self.assertEqual(ff.mode_of(''), 'auto')
        self.assertEqual(ff.mode_of(' Fleet '), 'fleet')
        with self.assertRaises(ValueError):
            ff.mode_of('fleeet')   # a typo must not quietly mean auto


class Rule(unittest.TestCase):
    def test_push_to_main_by_the_owner(self):
        self.assertIsNone(ff.refused(CFG, env(), PUSH))

    def test_own_pull_request(self):
        self.assertIsNone(ff.refused(CFG, env(**PR_ENV), pr()))

    def test_refused(self):
        cases = {
            'pull request from a fork': (env(**PR_ENV), pr(head='someone/t3code')),
            'pull request into another repo': (env(**PR_ENV), pr(base='pingdotgg/t3code')),
            'pull request opened by someone else': (env(**PR_ENV), pr(user='dependabot[bot]')),
            'actor': (env(GITHUB_ACTOR='someone'), PUSH),
            'triggering actor (a re-run by someone else)': (env(GITHUB_TRIGGERING_ACTOR='someone'), PUSH),
            'push to another branch': (env(GITHUB_REF='refs/heads/fork/x'), PUSH),
            'push to a tag': (env(GITHUB_REF='refs/tags/v1'), PUSH),
            'another repository': (env(GITHUB_REPOSITORY='someone/t3code'), PUSH),
            'a push payload of another repository': (env(), {'repository': {'full_name': 'someone/t3code'}}),
            'another workflow': (env(GITHUB_WORKFLOW_REF=f'{REPO}/.github/workflows/ci.yml@refs/heads/main'), PUSH),
            'a workflow whose path only starts the same': (
                env(GITHUB_WORKFLOW_REF=f'{REPO}/{WORKFLOW}x@refs/heads/main'), PUSH),
            'workflow_dispatch (release runs)': (env(GITHUB_EVENT_NAME='workflow_dispatch'), {}),
            'schedule': (env(GITHUB_EVENT_NAME='schedule'), {}),
            'pull_request_target': (env(GITHUB_EVENT_NAME='pull_request_target'), pr()),
            'no payload': (env(), None),
            'no variables': ({}, PUSH),
            'a push the upstream sync made (its tag exists)': (env(GITHUB_SHA=SYNCED), PUSH),
            'a push whose sync tag cannot be read': (env(GITHUB_SHA=UNKNOWN), PUSH),
            'a push with no commit sha': (env(GITHUB_SHA=None), PUSH),
            'a push with a short sha': (env(GITHUB_SHA=SHA[:12]), PUSH),
        }
        for name, (e, event) in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(ff.refused(CFG, e, event))


class RunRefused(unittest.TestCase):
    """The rule on GitHub's own record of a run: what the dispatcher and the sweep decide by."""

    def check(self, run, gh=None):
        gh = gh or FakeGitHub()
        return ff.run_refused(CFG, gh, run), gh

    def test_a_push_to_main_by_the_owner(self):
        self.assertIsNone(self.check(a_run())[0])

    def test_the_owners_own_pull_request_is_read_from_the_api(self):
        gh = FakeGitHub()
        gh.own_pr()
        why, gh = self.check(a_run(event='pull_request', branch='fork/x', prs=[7]), gh)
        self.assertIsNone(why)
        self.assertIn(('GET', 'pulls/7', None), gh.calls)

    def test_refused(self):
        def stranger_pr(gh):
            gh.own_pr(user='someone')

        def other_head(gh):
            gh.own_pr(head='someone/t3code')
        cases = {
            'a pull request run listing no pull request (one from a fork)':
                (a_run(event='pull_request', branch='x', head_repo='someone/t3code'), None),
            'a pull request whose head is not the run\'s': (a_run(event='pull_request', branch='x', prs=[7]), other_head),
            'a pull request someone else opened': (a_run(event='pull_request', branch='x', prs=[7]), stranger_pr),
            'a re-run by github-actions (the sweep\'s own)': (a_run(attempt=2, triggering='github-actions[bot]'), None),
            'a re-run by someone else': (a_run(attempt=2, triggering='someone'), None),
            'a stranger\'s push': (a_run(actor='someone'), None),
            'a push to another branch': (a_run(branch='fork/x'), None),
            'another workflow': (a_run(path='.github/workflows/ci.yml'), None),
            'another repository': (a_run(repo='someone/t3code'), None),
            'a workflow_dispatch run': (a_run(event='workflow_dispatch'), None),
            'a workflow_run run': (a_run(event='workflow_run'), None),
            'the push run of a commit the upstream sync made': (a_run(sha=SYNCED), None),
            'a push run whose sync tag cannot be read': (a_run(sha=UNKNOWN), None),
        }
        for name, (run, setup) in cases.items():
            with self.subTest(name):
                gh = FakeGitHub()
                if setup:
                    setup(gh)
                self.assertIsNotNone(self.check(run, gh)[0])


class Gate(unittest.TestCase):
    """The hook's decision (the kill itself: GateProcess)."""

    def gate(self, e, event=PUSH):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'event.json')
            if event is not None:
                with open(path, 'w') as f:
                    f.write(event if isinstance(event, str) else json.dumps(event))
            stops = []
            got, log = quiet(ff.gate, CFG, dict(e, GITHUB_EVENT_PATH=path), stop=lambda why: stops.append(why) or 1)
        return got, stops

    MADE = dict(FORK_FLEET_RUN='5', FORK_FLEET_ATTEMPT='1')

    def test_the_job_its_runner_was_made_for(self):
        self.assertEqual(self.gate(env(**self.MADE)), (0, []))
        self.assertEqual(self.gate(env(**PR_ENV, **self.MADE), pr()), (0, []))

    def test_refused_and_stopped(self):
        cases = {
            'a runner the dispatcher did not start': env(),
            'another run': env(GITHUB_RUN_ID='6', **self.MADE),
            'a re-run on a runner made for attempt 1': env(GITHUB_RUN_ATTEMPT='2', **self.MADE),
            'a stranger\'s run': env(GITHUB_ACTOR='someone', **self.MADE),
            'a push the upstream sync made': env(GITHUB_SHA=SYNCED, **self.MADE),
        }
        for name, e in cases.items():
            with self.subTest(name):
                got, stops = self.gate(e)
                self.assertEqual((got, len(stops)), (1, 1))
        for name, event in {'a fork\'s pull request': pr(head='someone/t3code'), 'no event file': None,
                            'a broken event file': '{'}.items():
            with self.subTest(name):
                got, stops = self.gate(env(**PR_ENV, **self.MADE), event)
                self.assertEqual((got, len(stops)), (1, 1))

    def test_made_for(self):
        self.assertIsNone(ff.made_for(env(**self.MADE)))
        self.assertIn('not started by the dispatcher', ff.made_for(env()))
        self.assertIn('attempt 1, not run 5 attempt 2', ff.made_for(env(GITHUB_RUN_ATTEMPT='2', **self.MADE)))

    def test_stop_job_kills_the_worker(self):
        kills = []
        got, log = quiet(ff.stop_job, 'why', kill=lambda pid, sig: kills.append((pid, sig)), find=lambda: 4321,
                         pause=lambda s: None)
        self.assertEqual((got, kills), (1, [(4321, signal.SIGKILL)]))
        self.assertIn('::error::', log)
        kills.clear()
        got, log = quiet(ff.stop_job, 'why', kill=lambda pid, sig: kills.append(pid), find=lambda: None,
                         pause=lambda s: None)
        self.assertEqual((got, kills), (1, []))
        self.assertIn('cannot be stopped', log)


class WorkerAbove(unittest.TestCase):
    def proc(self, d, table):
        for pid, (comm, ppid) in table.items():
            os.makedirs(os.path.join(d, str(pid)))
            with open(os.path.join(d, str(pid), 'stat'), 'w') as f:
                f.write(f'{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0 0\n')

    def test_the_nearest_worker_among_the_ancestors(self):
        with tempfile.TemporaryDirectory() as d:
            self.proc(d, {40: ('python3', 30), 30: ('a) S 7 (b', 20), 20: ('Runner.Worker', 15),
                          15: ('Runner.Worker', 10), 10: ('Runner.Listener', 1)})
            self.assertEqual(ff.worker_above(40, proc=d), 20)

    def test_none_without_a_worker(self):
        with tempfile.TemporaryDirectory() as d:
            self.proc(d, {40: ('python3', 30), 30: ('bash', 1), 50: ('loop', 50), 60: ('x', 61)})
            self.assertIsNone(ff.worker_above(40, proc=d))
            self.assertIsNone(ff.worker_above(50, proc=d))   # a loop ends
            self.assertIsNone(ff.worker_above(60, proc=d))   # a gone parent ends


@unittest.skipUnless(os.path.isfile('/usr/bin/python3') and os.path.isdir('/proc/self'), 'Linux with /usr/bin/python3')
class GateProcess(unittest.TestCase):
    """The installed hook as a runner runs it: a process named Runner.Worker runs the job-started hook, then, whatever
    the hook returned, the job's `if: always()` step. A refused job must end with the hook: no marker."""

    WORKER = ('import subprocess, sys\n'
              'hook, marker = sys.argv[1], sys.argv[2]\n'
              'subprocess.run(["/bin/bash", hook])   # the job-started hook\n'
              'with open(marker, "w") as f:   # an `if: always()` step\n'
              '    f.write("ran")\n')

    def install(self, d, config=None, mutate=None, remote=None):
        """remote: SYNC_REMOTE of the copy (a local bare repository, sync_remote), so its sync check reads no network."""
        opt = os.path.join(d, 'opt')
        os.mkdir(opt)
        shutil.copy(os.path.join(HERE, 'fork-fleet.py'), opt)
        shutil.copy(os.path.join(HERE, 'fork-fleet-hook.sh'), os.path.join(opt, 'hook.sh'))
        with open(os.path.join(opt, 'fork-fleet.json'), 'w') as f:
            f.write(json.dumps(CFG) if config is None else config)
        changes = ([mutate] if mutate else []) + ([(f'SYNC_REMOTE = {ff.SYNC_REMOTE!r}', f'SYNC_REMOTE = {remote!r}')]
                                                  if remote else [])
        path = os.path.join(opt, 'fork-fleet.py')
        for old, new in changes:
            with open(path) as f:
                text = f.read()
            self.assertEqual(text.count(old), 1)
            with open(path, 'w') as f:
                f.write(text.replace(old, new))
        return os.path.join(opt, 'hook.sh')

    def start(self, d, hook, e, event):
        worker = os.path.join(d, 'Runner.Worker')
        os.symlink(os.path.realpath(sys.executable), worker)
        script, marker, event_path = (os.path.join(d, n) for n in ('worker.py', 'marker', 'event.json'))
        with open(script, 'w') as f:
            f.write(self.WORKER)
        with open(event_path, 'w') as f:
            json.dump(event, f)
        e = dict(e, PATH='/usr/bin:/bin', GITHUB_EVENT_PATH=event_path)
        return subprocess.Popen([worker, '-I', script, hook, marker], env=e, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True), marker

    def test_the_hook_ends_a_refused_job_before_its_always_steps(self):
        made = dict(FORK_FLEET_RUN='5', FORK_FLEET_ATTEMPT='1')
        with tempfile.TemporaryDirectory() as top:
            remote, synced = sync_remote(top)
            self.check_hook(made, remote, synced)

    def check_hook(self, made, remote, synced):
        cases = {   # name: (environment, event, config, mutation, the always() step runs)
            'allowed': (env(**made), PUSH, None, None, True),
            'a push the upstream sync made (real git, its tag in the repository)':
                (env(GITHUB_SHA=synced, **made), PUSH, None, None, False),
            'a push whose sync tag cannot be read (git fails)':
                (env(**made), PUSH, None, ("'ls-remote', '--exit-code'", "'no-such-command', '--exit-code'"), False),
            'a fork\'s pull request': (env(**PR_ENV, **made), pr(head='someone/t3code'), None, None, False),
            'a re-run on an old runner': (env(GITHUB_RUN_ATTEMPT='2', **made), PUSH, None, None, False),
            'a runner the dispatcher did not start': (env(), PUSH, None, None, False),
            'a gate that crashes (broken config)': (env(**made), PUSH, '[]', None, False),
            # The test's own check: without the kill, the always() step does run.
            'a mutant gate that only exits 1': (env(**PR_ENV, **made), pr(head='someone/t3code'), None,
                                               ('kill(pid, signal.SIGKILL)', 'pid'), True),
        }
        with tempfile.TemporaryDirectory() as top:
            started = {}
            for i, (name, (e, event, config, mutate, runs)) in enumerate(cases.items()):
                d = os.path.join(top, str(i))
                os.mkdir(d)
                started[name] = self.start(d, self.install(d, config, mutate, remote), e, event), runs
            for name, ((proc, marker), runs) in started.items():
                with self.subTest(name):
                    out = proc.communicate(timeout=60)[0]
                    self.assertEqual(os.path.exists(marker), runs, out)
                    if runs:
                        self.assertEqual(proc.returncode, 0, out)
                    else:
                        self.assertEqual(proc.returncode, -signal.SIGKILL, out)
                        self.assertIn('::error::fork-fleet: this job may not run on a fleet runner', out)

    def test_the_crash_message(self):
        with tempfile.TemporaryDirectory() as d:
            (proc, marker) = self.start(d, self.install(d, '[]'), env(FORK_FLEET_RUN='5', FORK_FLEET_ATTEMPT='1'),
                                        PUSH)
            out = proc.communicate(timeout=60)[0]
            self.assertIn('the gate itself failed', out)
            self.assertFalse(os.path.exists(marker))


class UpstreamSync(unittest.TestCase):
    """made_by_the_sync itself, with the real git: the push run of a commit the upstream sync tagged never uses the
    fleet (owner decision: upstream code runs on GitHub-hosted runners only); any doubt refuses."""

    def call(self, sha, code=2, error=None):
        calls = []

        def run(argv, **kw):
            calls.append((argv, kw))
            if error:
                raise error
            return subprocess.CompletedProcess(argv, code, '', '')
        return REAL_MADE_BY_THE_SYNC(CFG, sha, run=run), calls

    def test_it_asks_this_repository_anonymously_for_the_one_tag(self):
        why, calls = self.call(SHA)
        self.assertIsNone(why)
        (argv, kw), = calls
        self.assertEqual(argv, ['git', '-c', 'protocol.version=2', 'ls-remote', '--exit-code',
                                f'https://github.com/{REPO}.git', f'refs/tags/{SYNC_TAG}{SHA}'])
        self.assertEqual(kw['env']['GIT_CONFIG_GLOBAL'], '/dev/null')   # no config, helper or token of the caller
        self.assertEqual(kw['env']['GIT_CONFIG_NOSYSTEM'], '1')
        self.assertEqual(kw['env']['GIT_TERMINAL_PROMPT'], '0')
        self.assertFalse({k for k in kw['env'] if 'TOKEN' in k or k == 'HOME'})
        self.assertTrue(0 < kw['timeout'] <= 60)

    def test_a_tagged_commit_and_any_doubt_are_refused(self):
        cases = {'tagged': (SHA, 0, None, 'pushed by the upstream sync'),
                 'git fails': (SHA, 128, None, 'cannot be told (git ls-remote exit 128)'),
                 'git hangs': (SHA, 0, subprocess.TimeoutExpired('git', 30), 'cannot be told (TimeoutExpired)'),
                 'no git': (SHA, 0, FileNotFoundError('git'), 'cannot be told (FileNotFoundError)')}
        for name, (sha, code, error, words) in cases.items():
            with self.subTest(name):
                self.assertIn(words, self.call(sha, code, error)[0])
        for sha in (None, '', SHA[:12], SHA.upper(), SHA + '0', '-' + SHA[1:], f'{SHA[:39]}\n'):
            with self.subTest(sha=sha):
                why, calls = self.call(sha)
                self.assertIn('not a full sha', why)
                self.assertEqual(calls, [])   # nothing a run controls reaches git unchecked

    @unittest.skipUnless(shutil.which('git', path='/usr/bin:/bin'), 'git in /usr/bin or /bin')
    def test_with_the_real_git(self):
        with tempfile.TemporaryDirectory() as top:
            remote, synced = sync_remote(top)
            saved = ff.SYNC_REMOTE
            try:
                ff.SYNC_REMOTE = remote
                self.assertIn('pushed by the upstream sync', REAL_MADE_BY_THE_SYNC(CFG, synced))
                self.assertIsNone(REAL_MADE_BY_THE_SYNC(CFG, SHA))
                ff.SYNC_REMOTE = remote.replace(top, os.path.join(top, 'gone'))
                self.assertIn('cannot be told', REAL_MADE_BY_THE_SYNC(CFG, SHA))
            finally:
                ff.SYNC_REMOTE = saved

    def test_the_sync_pushes_the_tag_this_rule_reads(self):
        with open(os.path.join(HERE, 'fork-follow-push.sh')) as f:
            text = f.read()
        self.assertIn(f'\nSYNCED_TAG_PREFIX={SYNC_TAG}\n', text)


def beacon(free, at=T0):
    return json.dumps({'at': at, 'free': free})


class Route(unittest.TestCase):
    def route(self, mode='auto', beacons=None, gates=None, e=None, event=PUSH, now=T0, nonce=NONCE):
        e = dict(e or env())
        for name, value in (beacons or {}).items():
            e[POOLS[name]['beacon']] = value
        (got_mode, out), log = quiet(ff.route, CFG, e, event, mode, gates, now=lambda: now, nonce=nonce)
        return got_mode, out, log

    def fleet(self, out):
        return {k: v for k, v in out.items() if v != HOSTED}

    def test_github_mode_is_hosted(self):
        _, out, _ = self.route('github', beacons={FIRST: beacon(5), SECOND: beacon(5)})
        self.assertEqual(set(out.values()), {HOSTED})

    def test_auto_without_a_heartbeat_is_hosted(self):
        _, out, log = self.route()
        self.assertEqual(self.fleet(out), {})
        self.assertIn('no heartbeat', log)

    def test_auto_fills_the_free_slots_pools_in_order(self):
        _, out, _ = self.route(beacons={FIRST: beacon(1), SECOND: beacon(2)})
        self.assertEqual(self.fleet(out), {KEYS[0]: labels(FIRST, KEYS[0]), KEYS[1]: labels(SECOND, KEYS[1]),
                                           KEYS[2]: labels(SECOND, KEYS[2])})

    def test_stale_and_garbled_heartbeats_count_no_slot(self):
        cases = {'stale': beacon(2, at=T0 - CFG['beacon_fresh_seconds'] - 1), 'from the future': beacon(2, at=T0 + 61),
                 'not JSON': '{"at": 1', 'a list': '[]', 'no free': json.dumps({'at': T0}),
                 'free is not a number': json.dumps({'at': T0, 'free': '2'}), 'at is a bool': '{"at": true, "free": 2}',
                 'free is a fraction': json.dumps({'at': T0, 'free': 1.5}), 'negative': beacon(-3)}
        for name, value in cases.items():
            with self.subTest(name):
                _, out, log = self.route(beacons={FIRST: value})
                self.assertEqual(self.fleet(out), {})
                if name != 'negative':
                    self.assertRegex(log, 'cannot be read|s old')

    def test_a_closed_pool_is_a_notice_on_the_run_page(self):
        """A new actions/runner release closes the pool (fail closed): auto goes hosted, and the run page says why."""
        closed = json.dumps({'at': T0, 'free': 0, 'why': 'the runner template is actions/runner 2.329.9, older than '
                             'the newest release 2.330.0: no runner starts until the owner refreshes the template '
                             '(.github/fork-fleet.md, host step 2)'})
        _, out, log = self.route(beacons={FIRST: closed, SECOND: beacon(2, at=T0 - CFG['beacon_fresh_seconds'] - 1)})
        self.assertEqual(self.fleet(out), {})
        self.assertIn(f'::notice::fork-fleet: pool {FIRST}: no free slot counted: its dispatcher says: the runner '
                      'template is actions/runner 2.329.9, older than the newest release 2.330.0', log)
        self.assertRegex(log, rf'::error::fork-fleet: pool {SECOND}: no free slot counted: .* s old')
        _, _, log = self.route()   # never set up: a log line, no notice
        self.assertIn(f'pool {FIRST}: no free slot counted: no heartbeat', log)
        self.assertNotIn('::notice::', log)

    def test_fleet_mode_queues_the_rest_on_the_waiting_pool(self):
        mode, out, _ = self.route('fleet', beacons={FIRST: beacon(1)})
        self.assertEqual(mode, 'fleet')
        self.assertEqual(out[KEYS[0]], labels(FIRST, KEYS[0]))
        for key in KEYS[1:]:
            self.assertEqual(out[key], labels(WAITING, key))

    def test_jobs_that_do_not_run_stay_hosted(self):
        gates = {g: 'false' for j in CFG['jobs'] for g in j['gates']}
        gates['rust'] = 'true'
        running = [k for k, j in ff.job_keys(CFG) if ff.runs(j, gates)]
        self.assertEqual(running, ['lint', 'rust'])   # lint has no gates: it always runs
        _, out, _ = self.route('fleet', gates=gates)
        self.assertEqual(self.fleet(out), {'lint': labels(WAITING, 'lint'), 'rust': labels(WAITING, 'rust')})
        _, out, _ = self.route(beacons={SECOND: beacon(2)}, gates=gates)
        self.assertEqual(self.fleet(out), {'lint': labels(SECOND, 'lint'), 'rust': labels(SECOND, 'rust')})

    def test_untrusted_runs_are_hosted_in_every_mode(self):
        for mode in ('auto', 'fleet'):
            with self.subTest(mode):
                _, out, log = self.route(mode, beacons={FIRST: beacon(5)}, e=env(**PR_ENV),
                                         event=pr(head='someone/t3code'))
                self.assertEqual(set(out.values()), {HOSTED})
                self.assertIn('only trusted runs', log)

    def test_the_upstream_syncs_push_run_is_hosted_in_every_mode(self):
        for mode in ('auto', 'fleet'):
            with self.subTest(mode):
                _, out, log = self.route(mode, beacons={FIRST: beacon(5), SECOND: beacon(5)}, e=env(GITHUB_SHA=SYNCED))
                self.assertEqual(set(out.values()), {HOSTED})
                self.assertIn(f'::notice::fork-fleet: GitHub-hosted for every job: commit {SYNCED[:12]} was pushed '
                              'by the upstream sync', log)

    def test_each_run_gets_labels_of_its_own_that_the_dispatcher_reads_back(self):
        _, a, _ = self.route('fleet', nonce=None)
        _, b, _ = self.route('fleet', nonce=None)
        for key in KEYS:
            self.assertNotEqual(a[key], b[key])
            self.assertEqual(ff.fleet_job(CFG, a[key]), None)   # runs-on adds the attempt; without it, no runner
            self.assertEqual(ff.fleet_job(CFG, [a[key][0], a[key][1] + '-a3']), (POOLS[WAITING], key, 3))

    def test_a_broken_setting_fails(self):
        with self.assertRaises(ValueError):
            self.route('flet')

    def outputs(self, **over):
        with tempfile.TemporaryDirectory() as d:
            out, summ, event = (os.path.join(d, n) for n in ('out', 'summary', 'event'))
            with open(event, 'w') as f:
                json.dump(PUSH, f)
            e = env(GITHUB_OUTPUT=out, GITHUB_STEP_SUMMARY=summ, GITHUB_EVENT_PATH=event, FLEET_MODE='',
                    GATES='{"test": "false"}')
            e.update(over)
            got, log = quiet(ff.cmd_route, CFG, e)
            self.assertEqual(got, 0)
            with open(out) as f:
                lines = dict(line.split('=', 1) for line in f.read().splitlines())
            with open(summ) as f:
                self.assertIn('Runners', f.read())
        return lines, log

    def test_cmd_route_outputs(self):
        lines, _ = self.outputs()
        self.assertEqual((lines['mode'], lines['attempt'], lines['watch']), ('auto', '1', 'false'))
        self.assertEqual(json.loads(lines['runners']), {k: HOSTED for k in KEYS})
        lines, _ = self.outputs(FLEET_MODE='fleet', GITHUB_RUN_ATTEMPT='3')
        self.assertEqual((lines['mode'], lines['attempt'], lines['watch']), ('fleet', '3', 'true'))
        runners = json.loads(lines['runners'])
        self.assertEqual(runners['test-1'], HOSTED)   # gate test is false: it only prints a skip
        self.assertEqual(runners['lint'][0], POOLS[WAITING]['label'])
        lines, _ = self.outputs(**{POOLS[FIRST]['beacon']: beacon(1, at=int(ff.time.time()))})
        self.assertEqual(lines['watch'], 'true')
        self.assertEqual(lines['rerun'], '{}')   # the fork's config has no reruns: always the line, {} for none

    def test_cmd_route_with_unreadable_outputs_counts_every_job(self):
        lines, log = self.outputs(FLEET_MODE='fleet', GATES='{')
        self.assertIn('::warning::', log)
        self.assertNotIn(HOSTED, json.loads(lines['runners']).values())


class Watch(unittest.TestCase):
    def watch(self, jobs, mode='', want=None, attempt='1', trouble=None):
        gh = FakeGitHub()
        gh.add(a_run(status='in_progress', attempt=int(attempt)), *jobs)
        if trouble:
            gh.trouble.append((r'GET actions/runs/5/attempts/\d+/jobs', trouble))
        clock = [T0]

        def sleep(s):
            clock[0] += s
        want = sum(1 for j in jobs if ff.is_fleet(CFG, j)) if want is None else want
        e = {'GITHUB_RUN_ID': '5', 'GITHUB_RUN_ATTEMPT': attempt, 'FLEET_MODE': mode,
             'FLEET_RUNNERS': json.dumps({f'k{i}': labels(WAITING, 'lint') for i in range(want)})}
        got, log = quiet(ff.watch, CFG, e, gh, now=lambda: clock[0], sleep=sleep)
        return got, gh.writes(), log, clock[0] - T0, gh

    def test_a_job_waiting_past_the_bound_cancels_the_run(self):
        for mode, bound in (('', CFG['stuck_seconds']), ('fleet', CFG['fleet_stuck_seconds'])):
            with self.subTest(mode or 'auto'):
                got, writes, log, waited, _ = self.watch([a_job(11, labels(WAITING, 'lint'))], mode=mode)
                self.assertEqual((got, writes, waited), (1, [('POST', 'actions/runs/5/cancel')], bound))
                self.assertIn('::error::', log)

    def test_the_wait_counts_from_the_jobs_creation(self):
        got, writes, _, waited, _ = self.watch([a_job(11, labels(WAITING, 'lint'), created=T0 - 590)])
        self.assertEqual((got, waited), (1, 30))

    def test_every_job_started(self):
        got, writes, _, waited, _ = self.watch([a_job(11, labels(WAITING, 'lint'), status='in_progress', runner='r')])
        self.assertEqual((got, writes, waited), (0, [], 0))

    def test_a_routed_job_that_never_shows_up_ends_the_watch(self):
        got, writes, _, waited, _ = self.watch([a_job(11, labels(WAITING, 'lint'), status='in_progress', runner='r')],
                                               want=2)
        self.assertEqual((got, writes, waited), (0, [], ff.SHOW_UP))

    def test_hosted_jobs_are_not_watched(self):
        got, writes, _, waited, _ = self.watch([a_job(11, [HOSTED])], want=0)
        self.assertEqual((got, writes, waited), (0, [], 0))

    def test_a_truncated_or_garbled_reply_is_never_red(self):
        for name, error in {'truncated': http.client.IncompleteRead(b'{"jobs": [', 400),
                            'not JSON': json.JSONDecodeError('Expecting value', '{"jo', 4),
                            'no answer': urllib.error.URLError('timed out')}.items():
            with self.subTest(name):
                got, writes, log, waited, _ = self.watch([a_job(11, labels(WAITING, 'lint'))], trouble=error)
                self.assertEqual((got, writes), (0, []))
                self.assertEqual(waited, CFG['stuck_seconds'] + ff.SHOW_UP)
                self.assertIn('::warning::', log)

    def test_a_re_run_watches_its_own_attempt(self):
        _, _, _, _, gh = self.watch([a_job(11, labels(WAITING, 'lint'), status='in_progress', runner='r')],
                                    attempt='2')
        self.assertIn('actions/runs/5/attempts/2/jobs', [p for _, p, _ in gh.calls])


class Sweep(unittest.TestCase):
    def sweep(self, run, *jobs, mode='', head=SHA, at=T0 + 800, setup=None):
        gh = FakeGitHub()
        gh.add(run, *jobs)
        gh.branches[run['head_branch']] = head
        if setup:
            setup(gh)
        clock = [at]

        def sleep(s):
            clock[0] += s
        got, log = quiet(ff.sweep, CFG, {'FLEET_MODE': mode}, gh, now=lambda: clock[0], sleep=sleep)
        return got, [p for m, p in gh.writes()], log

    def never_started(self):
        return a_job(11, labels(WAITING, 'lint'), status='completed', conclusion='cancelled', completed=T0 + 700)

    def started(self, conclusion='failure', step='cancelled'):
        return a_job(11, labels(WAITING, 'lint'), status='completed', conclusion=conclusion, runner='fleet-vm-11',
                     completed=T0 + 300, steps=[{'name': 'Test', 'conclusion': step}])

    def test_a_job_that_waited_too_long_is_re_run(self):
        got, posts, _ = self.sweep(a_run(status='completed', conclusion='cancelled'), self.never_started())
        self.assertEqual((got, posts), (0, ['actions/runs/5/rerun']))

    def test_a_stuck_queued_job_cancels_then_re_runs(self):
        def finish(gh):
            def on_cancel(rid):
                gh.runs[rid].update(status='completed', conclusion='cancelled')
                gh.jobs[rid] = [self.never_started()]
            gh.on_cancel = on_cancel
        _, posts, _ = self.sweep(a_run(status='in_progress'), a_job(11, labels(WAITING, 'lint')), setup=finish)
        self.assertEqual(posts, ['actions/runs/5/cancel', 'actions/runs/5/rerun'])

    def test_a_queued_job_not_yet_stuck_is_left(self):
        _, posts, _ = self.sweep(a_run(status='in_progress'), a_job(11, labels(WAITING, 'lint'), created=T0 + 500))
        self.assertEqual(posts, [])

    def test_a_cancel_that_does_not_finish_is_left_for_the_next_sweep(self):
        _, posts, log = self.sweep(a_run(status='in_progress'), a_job(11, labels(WAITING, 'lint')))
        self.assertEqual(posts, ['actions/runs/5/cancel'])
        self.assertIn('next sweep', log)

    def test_a_moved_or_deleted_branch_is_not_re_run(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='cancelled'), self.never_started(), head='def')
        self.assertEqual(posts, [])
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='cancelled'), self.never_started(),
                                 setup=lambda gh: gh.branches.clear())
        self.assertEqual(posts, [])

    def test_every_attempt_of_a_trusted_run(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='failure', attempt=3), self.started())
        self.assertEqual(posts, ['actions/runs/5/rerun'])

    def test_a_lost_runner_is_re_run(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='failure'), self.started())
        self.assertEqual(posts, ['actions/runs/5/rerun'])

    def test_a_persons_cancel_stands(self):
        """A run a person cancelled while a fleet job ran: the job may end failed with no failed step of its own,
        like a lost runner. Removing the cancelled check in rescue_reason turns this red."""
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='cancelled'), self.started())
        self.assertEqual(posts, [])

    def test_a_failed_test_stays_red(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='failure'), self.started(step='failure'))
        self.assertEqual(posts, [])

    def test_untrusted_runs_are_never_touched(self):
        stuck = a_job(11, labels(WAITING, 'lint'))
        cases = {
            'a stranger\'s run': a_run(actor='someone', status='in_progress'),
            'a fork\'s pull request': a_run(event='pull_request', branch='x', head_repo='someone/t3code',
                                            status='in_progress'),
            'the sweep\'s own re-run': a_run(attempt=2, triggering='github-actions[bot]', status='in_progress'),
            'the upstream sync\'s push run': a_run(sha=SYNCED, status='in_progress'),
        }
        for name, run in cases.items():
            with self.subTest(name):
                _, posts, log = self.sweep(run, stuck)
                self.assertEqual(posts, [])
                self.assertIn('not a trusted run', log)
            with self.subTest(name + ', finished'):
                done = dict(run, status='completed', conclusion='failure')
                _, posts, _ = self.sweep(done, self.started())
                self.assertEqual(posts, [])

    def test_fleet_mode_re_runs_nothing(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='cancelled'), self.never_started(), mode='fleet')
        self.assertEqual(posts, [])
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='failure'), self.started(), mode='fleet')
        self.assertEqual(posts, [])

    def test_fleet_mode_cancels_a_run_that_waited_past_its_bound(self):
        """A re-run of failed jobs in fleet mode has no watch of its own: the sweep reports the wait instead."""
        bound = CFG['fleet_stuck_seconds']
        _, posts, _ = self.sweep(a_run(status='in_progress'), a_job(11, labels(WAITING, 'lint')), mode='fleet',
                                 at=T0 + bound - 1)
        self.assertEqual(posts, [])
        _, posts, log = self.sweep(a_run(status='in_progress'), a_job(11, labels(WAITING, 'lint')), mode='fleet',
                                   at=T0 + bound)
        self.assertEqual(posts, ['actions/runs/5/cancel'])
        self.assertIn('::error::', log)
        _, posts, _ = self.sweep(a_run(actor='someone', status='in_progress'), a_job(11, labels(WAITING, 'lint')),
                                 mode='fleet', at=T0 + bound)
        self.assertEqual(posts, [])

    def test_hosted_jobs_are_not_the_sweeps_business(self):
        _, posts, _ = self.sweep(a_run(status='completed', conclusion='failure'),
                                 a_job(11, [HOSTED], status='completed', conclusion='failure', runner='GitHub Actions 1'))
        self.assertEqual(posts, [])

    def test_github_trouble_is_a_warning_and_the_next_sweep_tries_again(self):
        got, posts, log = self.sweep(a_run(status='completed', conclusion='cancelled'), self.never_started(),
                                     setup=lambda gh: gh.trouble.append((r'GET actions/runs/5/jobs.*',
                                                                         http.client.IncompleteRead(b'{', 9))))
        self.assertEqual((got, posts), (1, []))
        self.assertIn('next sweep', log)


class FakeLauncher:
    """A pool's launch command as fork-fleet-launch.sh answers: the host's units and its memory."""

    def __init__(self, pool):
        self.pool, self.units, self.memory_ok, self.template = pool, set(), True, NEWEST_RUNNER
        self.started, self.stopped, self.fits_asked, self.pruned, self.fail = [], [], [], 0, None
        self.refreshed = 0

    def run(self, cmd, input=None, capture_output=True, text=True, timeout=None):
        launch = POOLS[self.pool]['launch']
        assert cmd[:len(launch)] == launch and capture_output and text and timeout, cmd
        verb, args = cmd[len(launch)], cmd[len(launch) + 1:]
        if self.fail == verb:
            return subprocess.CompletedProcess(cmd, 1, '', 'fork-fleet-launch: systemctl failed\n')
        out, code = '', 0
        if verb == 'active':
            out = ''.join(f'{n}\n' for n in sorted(self.units)) + 'someone-else-1\n'
        elif verb == 'fits':
            self.fits_asked.append(int(args[0]))
            ok = self.memory_ok(int(args[0])) if callable(self.memory_ok) else self.memory_ok
            code = 0 if ok else 1
        elif verb == 'start':
            self.started.append((args[0], input))
            self.units.add(args[0])
        elif verb == 'stop':
            self.stopped.append(args[0])
            self.units.discard(args[0])
        elif verb == 'prune':
            self.pruned += 1
        elif verb == 'refresh-net':
            self.refreshed += 1
        elif verb == 'ready':
            if tuple(map(int, self.template.split('.'))) < tuple(map(int, args[0].split('.'))):
                return subprocess.CompletedProcess(cmd, 1, '', f'fork-fleet-launch: the runner template is actions/runner '
                                                   f'{self.template}, older than the newest release {args[0]}: no '
                                                   'runner starts until the owner refreshes the template\n')
        else:
            raise AssertionError(cmd)
        return subprocess.CompletedProcess(cmd, code, out, '')


class Dispatch(unittest.TestCase):
    def setUp(self):
        self.gh = FakeGitHub()
        self.fake = {name: FakeLauncher(name) for name in POOLS}
        self.hosts = {name: ff.Host(POOLS[name], run=f.run) for name, f in self.fake.items()}
        self.state = ff.new_state()
        self.clock = [T0]
        self.log = ''

    def dispatch(self, state=None):
        _, log = quiet(ff.dispatch_pass, CFG, self.gh, self.hosts, self.state if state is None else state,
                       now=lambda: self.clock[0])
        self.log += log
        return log

    def jits(self):
        return [d for m, p, d in self.gh.calls if p == 'actions/runners/generate-jitconfig']

    def started(self, pool=WAITING):
        return [n for n, _ in self.fake[pool].started]

    def name(self, jid, pool=WAITING):
        return f'{POOLS[pool]["label"]}-{jid}'

    def test_a_trusted_job_gets_one_runner_made_for_it(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.dispatch()
        self.assertEqual(self.jits(), [{'name': self.name(11), 'runner_group_id': 1, 'labels': job_labels(WAITING, 'lint'),
                                        'work_folder': '_work'}])
        jit = f'SklUc2VjcmV0{self.gh.runners[0]["id"]}=='
        self.assertEqual(self.fake[WAITING].started,
                         [(self.name(11), f'ACTIONS_RUNNER_INPUT_JITCONFIG={jit}\nFORK_FLEET_RUN=5\nFORK_FLEET_ATTEMPT=1\n')])
        self.assertNotIn(jit, self.log)   # the JIT config is a credential: never printed

    def test_the_owners_pull_request_and_a_re_run(self):
        self.gh.own_pr()
        self.gh.add(a_run(event='pull_request', branch='fork/x', prs=[7]), a_job(11, job_labels(FIRST, 'lint')))
        self.gh.add(a_run(6, attempt=2), a_job(12, job_labels(WAITING, 'rust', attempt=2)))
        self.dispatch()
        self.assertEqual(self.started(FIRST), [self.name(11, FIRST)])
        self.assertEqual(self.fake[WAITING].started[0][1].splitlines()[1:], ['FORK_FLEET_RUN=6', 'FORK_FLEET_ATTEMPT=2'])

    def test_a_job_of_a_newer_attempt_than_the_run_read_waits_a_pass(self):
        """A re-run between the run list and the job list: a runner made for the old attempt would have its job
        refused by the gate. The next pass reads the new attempt."""
        self.gh.add(a_run(attempt=1), dict(a_job(11, job_labels(WAITING, 'lint', attempt=2)), run_attempt=2))
        self.dispatch()
        self.assertEqual((self.jits(), self.started()), ([], []))
        self.gh.runs[5]['run_attempt'] = 2
        self.dispatch()
        self.assertEqual(self.fake[WAITING].started[0][1].splitlines()[1:], ['FORK_FLEET_RUN=5', 'FORK_FLEET_ATTEMPT=2'])

    def test_untrusted_runs_get_no_runner_and_say_so_once(self):
        def fork_pr(gh):
            gh.add(a_run(event='pull_request', branch='x', head_repo='someone/t3code'), a_job(11, job_labels(WAITING, 'lint')))

        def stranger(gh):
            gh.add(a_run(actor='someone'), a_job(11, job_labels(WAITING, 'lint')))

        def sweep_rerun(gh):
            gh.add(a_run(attempt=2, triggering='github-actions[bot]'), a_job(11, job_labels(WAITING, 'lint', attempt=2)))

        def someone_elses_pr(gh):
            gh.own_pr(user='someone')
            gh.add(a_run(event='pull_request', branch='x', prs=[7]), a_job(11, job_labels(WAITING, 'lint')))

        def upstream_sync(gh):
            gh.add(a_run(sha=SYNCED), a_job(11, job_labels(WAITING, 'lint')))
        for setup in (fork_pr, stranger, sweep_rerun, someone_elses_pr, upstream_sync):
            with self.subTest(setup.__name__):
                self.setUp()
                setup(self.gh)
                self.dispatch()
                self.dispatch()
                self.assertEqual((self.jits(), self.started()), ([], []))
                self.assertEqual(self.log.count('not a trusted run'), 1)

    def test_only_the_exact_label_pair_gets_a_runner(self):
        label = POOLS[WAITING]['label']
        other = POOLS[FIRST]['label']
        for name, asked in {
            'the pool label alone': [label],
            'the pool label and self-hosted': [label, 'self-hosted'],
            'three labels': job_labels(WAITING, 'lint') + ['linux'],
            'another pool\'s run label': [label, f'{other}-lint-{NONCE}-a1'],
            'an unknown job key': [label, f'{label}-nope-{NONCE}-a1'],
            'a short nonce': [label, f'{label}-lint-0123-a1'],
            'no attempt (route\'s labels as they are)': labels(WAITING, 'lint'),
            'another attempt of the run': job_labels(WAITING, 'lint', attempt=2),
            'attempt 0': [label, f'{label}-lint-{NONCE}-a0'],
            'one label twice': [label, label],
            'self-hosted alone': ['self-hosted'],
        }.items():
            with self.subTest(name):
                self.setUp()
                self.gh.add(a_run(), a_job(11, asked))
                self.dispatch()
                self.assertEqual((self.jits(), self.started()), ([], []))

    def test_no_runner_while_another_runs_job_could_take_it(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.gh.add(a_run(6, actor='someone'), a_job(21, ['Self-Hosted', 'LINUX']))
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.started(), [])
        self.assertEqual(self.log.count('could take its runner'), 1)
        self.gh.jobs[6] = [a_job(21, ['Self-Hosted', 'LINUX'], status='completed', conclusion='cancelled')]
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11)])

    def test_jobs_of_one_run_and_hosted_jobs_are_no_rivals(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')), a_job(12, job_labels(WAITING, 'rust')), a_job(13, [HOSTED]))
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11), self.name(12)])

    def test_slots(self):
        self.gh.add(a_run(), *[a_job(10 + i, job_labels(WAITING, k)) for i, k in enumerate(('lint', 'rust', 'build'))])
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.started(), [self.name(10), self.name(11)])
        self.assertEqual(POOLS[WAITING]['slots'], 2)

    def test_memory(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')), a_job(12, job_labels(WAITING, 'rust')))
        self.fake[WAITING].memory_ok = False
        self.dispatch()
        self.dispatch()
        self.assertEqual(self.started(), [])
        self.assertEqual(self.log.count(f'waits for {MEMORY["lint"]} MB'), 1)
        self.setUp()
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')), a_job(12, job_labels(WAITING, 'rust')))
        self.fake[WAITING].memory_ok = lambda mb: mb <= MEMORY['lint'] + MEMORY['rust'] - 1
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11)])   # the second asks with the first one's memory added
        self.assertEqual(self.fake[WAITING].fits_asked[:2], [MEMORY['lint'], MEMORY['lint'] + MEMORY['rust']])

    def test_a_name_taken_meanwhile_is_left_to_the_next_pass(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.gh.trouble.append(('POST actions/runners/generate-jitconfig', http_error(409)))
        self.dispatch()
        self.assertEqual(self.started(), [])
        self.gh.trouble.clear()
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11)])
        self.gh.trouble.append(('POST actions/runners/generate-jitconfig', http_error(500)))
        self.gh.add(a_run(6), a_job(12, job_labels(WAITING, 'rust')))
        with self.assertRaises(urllib.error.HTTPError):
            self.dispatch()

    def registration(self, jid, status='online', busy=False, pool=WAITING, key='lint', rid=None):
        self.gh.runners.append({'id': rid or 500 + jid, 'name': self.name(jid, pool), 'status': status, 'busy': busy,
                                'labels': [{'name': x} for x in ['self-hosted', 'Linux'] + job_labels(pool, key)]})

    def test_leftovers(self):
        self.registration(40, status='offline')                        # no runner behind it: deleted
        self.registration(41, status='offline')                        # its unit is starting: kept
        self.fake[WAITING].units.add(self.name(41))
        self.registration(42, busy=True)                               # running a job: kept
        self.gh.runners.append({'id': 77, 'name': 'someone-else-3', 'status': 'offline', 'busy': False, 'labels': []})
        self.dispatch()
        self.assertEqual(sorted(r['name'] for r in self.gh.runners),
                         sorted([self.name(41), self.name(42), 'someone-else-3']))
        self.assertEqual(self.fake[WAITING].stopped, [])
        self.assertEqual(self.fake[WAITING].pruned, 1)

    def test_a_registration_gone_already_is_fine(self):
        self.registration(40, status='offline')
        self.gh.trouble.append((r'DELETE actions/runners/\d+', http_error(404)))
        self.dispatch()
        self.assertIn('deleted', self.log)

    def test_an_idle_runner_whose_job_went_away_is_stopped(self):
        self.registration(40)
        self.fake[WAITING].units.add(self.name(40))
        for _ in range(CFG['dispatch']['idle_passes'] - 1):
            self.dispatch()
        self.assertEqual(self.fake[WAITING].stopped, [])
        self.dispatch()
        self.assertEqual(self.fake[WAITING].stopped, [self.name(40)])

    def test_an_idle_runner_another_job_could_take_is_stopped_at_once(self):
        self.registration(40)
        self.fake[WAITING].units.add(self.name(40))
        self.gh.add(a_run(6, actor='someone'), a_job(41, [POOLS[WAITING]['label']]))
        self.dispatch()
        self.assertEqual(self.fake[WAITING].stopped, [self.name(40)])

    def test_a_runner_still_coming_up_for_its_queued_job_is_not_idle(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.dispatch()
        self.gh.runners[0]['status'] = 'online'
        for _ in range(CFG['dispatch']['idle_passes'] + 1):
            self.dispatch()
        self.assertEqual((self.fake[WAITING].stopped, self.started()), ([], [self.name(11)]))

    def test_a_restarted_dispatcher_starts_nothing_twice(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.dispatch()
        self.dispatch(ff.new_state())
        self.assertEqual((len(self.jits()), self.started()), (1, [self.name(11)]))
        self.assertEqual(len(self.gh.runners), 1)   # the starting runner's registration is kept

    def test_at_most_max_tries_runners_for_one_job(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        for _ in range(ff.MAX_TRIES + 2):
            self.dispatch()
            self.fake[WAITING].units.clear()   # its runner died before it took the job
            self.gh.runners.clear()
        self.assertEqual(len(self.started()), ff.MAX_TRIES)
        self.assertEqual(self.log.count('none took it'), 1)

    def test_a_host_that_cannot_answer_skips_only_its_pool(self):
        """s1/s2 item 3: one dispatcher for both pools; one host down leaves the other pool working."""
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.gh.add(a_run(6), a_job(12, job_labels(FIRST, 'rust')))
        self.registration(40, status='offline', pool=FIRST)   # FIRST's: not touched while its host cannot answer
        self.fake[FIRST].fail = 'active'
        self.assertEqual(self.dispatch().count(f'pool {FIRST} is left alone'), 1)
        self.assertEqual((self.started(WAITING), self.started(FIRST)), ([self.name(11)], []))
        self.assertIn(self.name(40, FIRST), [r['name'] for r in self.gh.runners])
        self.assertEqual((self.fake[FIRST].pruned, self.fake[WAITING].pruned), (0, 1))
        beat = json.loads(self.gh.variables[POOLS[FIRST]['beacon']])
        self.assertEqual(beat['free'], 0)
        self.assertIn('systemctl failed', beat['why'])
        self.assertEqual(json.loads(self.gh.variables[POOLS[WAITING]['beacon']])['free'], POOLS[WAITING]['slots'] - 1)
        self.fake[FIRST].fail = None
        self.dispatch()
        self.assertEqual(self.started(FIRST), [self.name(12, FIRST)])
        self.assertNotIn(self.name(40, FIRST), [r['name'] for r in self.gh.runners])
        self.assertNotIn('why', json.loads(self.gh.variables[POOLS[FIRST]['beacon']]))

    def test_a_pool_down_for_the_same_reason_is_said_once_then_every_ten_minutes(self):
        """A desktop asleep all night: its pool's host fails every pass (20 s); the log gets a line, then one every
        DOWN_EVERY s with the count, then one when it answers again; not one per pass."""
        self.fake[FIRST].fail = 'active'
        for _ in range(5):
            self.dispatch()
            self.clock[0] += 20
        self.assertEqual(self.log.count(f'pool {FIRST} is left alone'), 1)
        self.assertNotIn('still left alone', self.log)
        self.clock[0] += ff.DOWN_EVERY
        self.assertIn(f'pool {FIRST} is still left alone (6 passes in a row)', self.dispatch())
        self.assertNotIn('still left alone', self.dispatch())
        self.fake[FIRST].fail = None
        self.assertIn(f'pool {FIRST} answers again, after 7 passes left alone', self.dispatch())
        self.fake[FIRST].fail = 'active'
        self.assertIn(f'pool {FIRST} is left alone for this pass', self.dispatch())   # a new outage: said at once

    def test_a_failed_start_skips_only_its_pool_and_is_tried_again(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')), a_job(12, job_labels(WAITING, 'rust')))
        self.gh.add(a_run(6), a_job(13, job_labels(FIRST, 'build')))
        self.fake[WAITING].fail = 'start'
        log = self.dispatch()
        self.assertEqual(log.count(f'pool {WAITING} is left alone'), 1)
        self.assertEqual(len([j for j in self.jits() if j['name'].startswith(POOLS[WAITING]['label'])]), 1)
        self.assertEqual(self.started(FIRST), [self.name(13, FIRST)])
        self.assertEqual(json.loads(self.gh.variables[POOLS[WAITING]['beacon']])['free'], 0)
        self.fake[WAITING].fail = None
        self.dispatch()   # its registration (no runner behind it) is deleted, the next pass starts the job
        self.dispatch()
        self.assertEqual(sorted(self.started()), [self.name(11), self.name(12)])

    def test_github_trouble_still_stops_the_pass(self):
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.gh.trouble.append(('GET actions/runners', http_error(502)))
        with self.assertRaises(urllib.error.HTTPError):
            self.dispatch()
        self.assertEqual(self.started(), [])

    def test_an_outdated_template_starts_nothing_and_says_why(self):
        """Item 6, fail closed: a template older than the newest actions/runner release starts no runner (it would
        update itself in its copy); its leftovers are still cleaned up and the heartbeat says why."""
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.registration(40, status='offline')
        self.fake[WAITING].template = '2.329.9'
        self.dispatch()
        log = self.dispatch()
        self.assertEqual((self.jits(), self.started()), ([], []))
        self.assertEqual(self.log.count('refreshes the template'), 1)
        self.assertEqual(log, '')
        self.assertNotIn(self.name(40), [r['name'] for r in self.gh.runners])   # cleanup goes on
        self.assertEqual(self.fake[WAITING].pruned, 2)
        beat = json.loads(self.gh.variables[POOLS[WAITING]['beacon']])
        self.assertEqual(beat['free'], 0)
        self.assertIn('refreshes the template', beat['why'])
        free, why = ff.beacon_free(CFG, POOLS[WAITING], {POOLS[WAITING]['beacon']: self.gh.variables[POOLS[WAITING]['beacon']]},
                                   T0)
        self.assertEqual(free, 0)
        self.assertIn('older than the newest release 2.330.0', why)
        self.assertEqual(json.loads(self.gh.variables[POOLS[FIRST]['beacon']])['free'], POOLS[FIRST]['slots'])
        self.fake[WAITING].template = '2.330.0'
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11)])

    def test_the_own_address_is_looked_up_every_pass_and_a_failed_lookup_starts_nothing(self):
        """Fix round 1 (#191, both lenses' minor 1): every pass the launcher looks this host's own public address up
        again (refresh-net blocks a new one for running jobs too). When no lookup answers, the last block stays and
        no runner starts on that pool, its leftovers are still cleaned up, it is said once, and the next pass that
        gets an answer starts the job."""
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.registration(40, status='offline')
        self.fake[WAITING].fail = 'refresh-net'
        self.dispatch()
        log = self.dispatch()
        self.assertEqual((self.jits(), self.started()), ([], []))
        self.assertEqual(self.log.count(f'until it answers (the last block stays)'), 1)
        self.assertEqual(log, '')
        self.assertNotIn(self.name(40), [r['name'] for r in self.gh.runners])   # cleanup goes on
        self.assertEqual(self.fake[WAITING].pruned, 2)
        self.assertEqual(json.loads(self.gh.variables[POOLS[WAITING]['beacon']])['free'], 0)
        self.fake[WAITING].fail = None
        self.dispatch()
        self.assertEqual(self.started(), [self.name(11)])
        self.assertEqual({n: f.refreshed for n, f in self.fake.items()}, {n: 3 for n in self.fake if n != WAITING} |
                         {WAITING: 1})   # every pass, each pool (the failed ones are not counted)
        self.fake[WAITING].fail = 'refresh-net'
        self.dispatch()
        self.assertEqual(self.log.count(f'until it answers (the last block stays)'), 2)   # a new outage: said again

    def test_the_newest_runner_release_is_read_hourly(self):
        reads = lambda: len([c for c in self.gh.calls if c[1] == '/repos/actions/runner/releases/latest'])
        self.gh.trouble.append(('GET /repos/actions/runner/releases/latest', http_error(502)))
        with self.assertRaises(urllib.error.HTTPError):
            self.dispatch()   # never read: no runner starts before the template is checked
        self.gh.trouble.clear()
        self.dispatch()
        self.clock[0] += ff.RELEASE_EVERY - 1
        self.dispatch()
        self.assertEqual(reads(), 2)
        self.clock[0] += 1
        self.gh.newest = '2.331.0'
        self.gh.trouble.append(('GET /repos/actions/runner/releases/latest', http_error(502)))
        self.dispatch()   # read failed: the last one is kept
        self.assertEqual(self.state['newest'][0], NEWEST_RUNNER)
        self.gh.trouble.clear()
        self.clock[0] += ff.RELEASE_EVERY
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.dispatch()
        self.assertEqual(self.state['newest'][0], '2.331.0')
        self.assertEqual(self.started(), [])   # the template (2.330.0) is now one release behind
        self.gh.newest = 'garbage'
        self.clock[0] += ff.RELEASE_EVERY
        self.dispatch()
        self.assertEqual(self.state['newest'][0], '2.331.0')

    def beacons(self):
        return [(m, p) for m, p, _ in self.gh.calls if 'actions/variables' in p]

    def test_heartbeats(self):
        vm, first = POOLS[WAITING]['beacon'], POOLS[FIRST]['beacon']
        self.dispatch()
        self.assertEqual(self.gh.variables, {vm: f'{{"at":{T0},"free":{POOLS[WAITING]["slots"]}}}',
                                             first: f'{{"at":{T0},"free":{POOLS[FIRST]["slots"]}}}'})
        self.assertEqual(self.beacons(), [('PATCH', f'actions/variables/{first}'), ('POST', 'actions/variables'),
                                          ('PATCH', f'actions/variables/{vm}'), ('POST', 'actions/variables')])
        self.gh.calls.clear()
        self.clock[0] += CFG['dispatch']['every_seconds']
        self.dispatch()
        self.assertEqual(self.beacons(), [])   # unchanged and recent: not written again
        self.clock[0] = T0 + CFG['dispatch']['beacon_seconds']
        self.dispatch()
        self.assertEqual(len(self.beacons()), 2)
        self.gh.calls.clear()
        self.clock[0] += 5
        self.gh.add(a_run(), *[a_job(10 + i, job_labels(WAITING, k)) for i, k in enumerate(('lint', 'rust', 'build'))])
        self.dispatch()
        self.assertEqual(self.beacons(), [('PATCH', f'actions/variables/{vm}')])   # changed: written at once
        self.assertEqual(json.loads(self.gh.variables[vm])['free'], 0)   # 2 slots, 2 started, 1 more waiting

    def test_a_queued_job_not_started_yet_counts_against_the_free_slots(self):
        """Item 4: a fleet job the dispatcher holds back (here: a job of another run could take its runner) still
        needs a slot. Left out of the count, route would send one job more to this pool than it can run."""
        self.gh.add(a_run(), a_job(11, job_labels(WAITING, 'lint')))
        self.gh.add(a_run(6, actor='someone'), a_job(21, ['self-hosted']))
        self.dispatch()
        self.assertEqual(self.started(), [])
        self.assertEqual(json.loads(self.gh.variables[POOLS[WAITING]['beacon']])['free'], POOLS[WAITING]['slots'] - 1)

    def test_no_free_slot_without_memory_for_the_biggest_job(self):
        self.fake[WAITING].memory_ok = lambda mb: mb < max(MEMORY.values())
        self.dispatch()
        self.assertEqual(json.loads(self.gh.variables[POOLS[WAITING]['beacon']])['free'], 0)

    def test_a_beacon_write_other_than_missing_fails(self):
        self.gh.trouble.append((r'PATCH actions/variables/\w+', http_error(403)))
        with self.assertRaises(urllib.error.HTTPError):
            self.dispatch()

    def test_runner_env_refuses_a_bad_config(self):
        for jit in (None, '', 'a b', 'a\nFORK_FLEET_RUN=1', 'x' * 65537):
            with self.subTest(repr(jit)[:20]), self.assertRaises(ValueError):
                ff.runner_env(jit, a_run())

    def test_the_token_file(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'token')
            with open(path, 'w') as f:
                f.write('tok\n')
            os.chmod(path, 0o600)
            self.assertEqual(ff.read_token(path), 'tok')
            os.chmod(path, 0o640)
            with self.assertRaises(ValueError):
                ff.read_token(path)
            with open(path, 'w') as f:
                f.write('\n')
            os.chmod(path, 0o600)
            with self.assertRaises(ValueError):
                ff.read_token(path)

    def test_one_dispatcher_at_a_time(self):
        with tempfile.TemporaryDirectory() as d:
            fd = ff.take_lock(os.path.join(d, 'lock'))
            try:
                with self.assertRaises(ValueError):
                    ff.take_lock(os.path.join(d, 'lock'))
            finally:
                os.close(fd)
            os.close(ff.take_lock(os.path.join(d, 'lock')))
            os.close(ff.take_lock(os.path.join(d, 'run', 'fork-fleet', 'lock')))   # after a boot: no directory yet
            self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, 'run', 'fork-fleet')).st_mode), 0o700)

    def test_one_lock_per_pool(self):
        """Item 3: a dispatcher per pool may run beside another; two for one pool may not."""
        with tempfile.TemporaryDirectory() as d:
            first = ff.take_locks(d, [FIRST])
            second = ff.take_locks(d, [SECOND])
            self.assertEqual(sorted(os.listdir(d)), sorted([f'dispatch-{FIRST}.lock', f'dispatch-{SECOND}.lock']))
            with self.assertRaises(ValueError):
                ff.take_locks(d, [FIRST, SECOND])
            for fd in second:
                os.close(fd)
            both = None
            with self.assertRaises(ValueError):
                both = ff.take_locks(d, [SECOND, FIRST])   # SECOND's lock is let go again: FIRST is held
            self.assertIsNone(both)
            os.close(ff.take_locks(d, [SECOND])[0])
            for fd in first:
                os.close(fd)
            for fd in ff.take_locks(d, [FIRST, SECOND, FIRST]):
                os.close(fd)

    def test_the_dispatcher_takes_its_pools_locks(self):
        with tempfile.TemporaryDirectory() as d:
            token = os.path.join(d, 'token')
            with open(token, 'w') as f:
                f.write('tok\n')
            os.chmod(token, 0o600)
            held = ff.take_locks(os.path.join(d, 'locks'), [FIRST])
            argv = ['dispatch', '--pool', FIRST, '--token-file', token, '--lock-dir', os.path.join(d, 'locks'), '--once']
            with self.assertRaisesRegex(ValueError, f'dispatch-{FIRST}.lock'):
                ff.main(argv, env={})
            for fd in held:
                os.close(fd)
            with self.assertRaises(SystemExit), redirect_stdout(io.StringIO()), \
                    __import__('contextlib').redirect_stderr(io.StringIO()):
                ff.main(['dispatch', '--lock', os.path.join(d, 'x'), '--once'], env={})   # one lock for all: gone

    def test_an_unknown_pool_is_refused_before_anything_else(self):
        with self.assertRaises(ValueError):
            ff.main(['dispatch', '--pool', 'nope', '--token-file', '/nonexistent', '--once'], env={})


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() != 0, 'Linux, not as root')
def walls_probe():
    """check-net's walls' probe, the Python code the launcher runs in the unit (its WALLS)."""
    with open(LAUNCH) as f:
        text = f.read()
    return text.split("read -r -d '' WALLS <<'EOF' || true\n", 1)[1].split('\nEOF\n', 1)[0]


class Launcher(unittest.TestCase):
    """fork-fleet-launch.sh with a fake systemd-run, systemctl, chown, pgrep, setpriv, ip, nft, iptables and id (it
    runs here as this user, not root; LauncherAsRoot runs the real ones). The fake systemd-run records each unit it
    starts, with its user, for `systemctl list-units` and `systemctl show` (with --wait, check-net's probe, it runs the
    command itself, or exits $REC/probe-exit when that file exists). The fake ip keeps the namespaces and links
    it made in $REC/netns and $REC/links; the fake iptables has a DOCKER-USER chain only when $REC/docker-user exists;
    the fake id answers from $REC/id-<flag>-<user> when that file exists."""

    FAKES = {
        'systemd-run': '#!/usr/bin/env bash\nprintf "%s\\n" "$@" > "$REC/systemd-run"\n'
                       'printf "%s\\0" "$@" > "$REC/systemd-run.0"\n'
                       'for a in "$@"; do case $a in EnvironmentFile=*) f=${a#EnvironmentFile=}; '
                       'cp -- "$f" "$REC/env"; stat -c %a -- "$f" > "$REC/env-mode" ;; esac; done\n'
                       # check-net's probe (--wait): prints $REC/probe-out and exits $REC/probe-exit when that file
                       # exists, else runs here.
                       'if [[ " $* " == *" --wait "* ]]; then a=("$@"); for i in "${!a[@]}"; do '
                       '[[ ${a[i]} != --unit ]] || cp -- "$REC/systemd-run.0" "$REC/${a[i+1]}.0"; done; '
                       '[[ ! -e $REC/probe-exit ]] || { cat -- "$REC/probe-out" 2>/dev/null; '
                       'exit "$(cat "$REC/probe-exit")"; }; '
                       'while [[ $1 != -- ]]; do shift; done; shift; exec "$@"; fi\n'
                       'code=$(cat "$REC/systemd-run-exit" 2>/dev/null || echo 0)\n'
                       'if [[ $code == 0 ]]; then while (($#)); do case $1 in --unit) unit=$2 ;; --uid) user=$2 ;; '
                       'esac; shift; done; printf "%s.service loaded active running x\\n" "$unit" >> "$REC/units"; '
                       'printf "%s\\n" "$user" > "$REC/user-$unit.service"; fi\nexit "$code"\n',
        'systemctl': '#!/usr/bin/env bash\n[[ ! -e $REC/systemctl-fails ]] || exit 1\n'
                     'case $1 in list-units) cat -- "$REC/units" 2>/dev/null; exit 0 ;; '
                     'show) cat -- "$REC/user-${!#}" 2>/dev/null; exit 0 ;; '
                     'stop) printf "%s\\n" "$*" >> "$REC/stopped" ;; '
                     'set-property) printf "%s\\n" "$*" >> "$REC/set-property"; [[ ! -e $REC/set-property-fails ]] ;; '
                     '*) exit 2 ;; esac\n',
        'chown': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$REC/chown"\n',
        # Processes of a runner user: a file $REC/procs-UID; setpriv's kill(-1) ends them unless procs-stay exists.
        'pgrep': '#!/usr/bin/env bash\n[[ $1 == -u ]] || exit 3\nprintf "%s\\n" "$2" >> "$REC/pgrep"\n'
                 '[[ -e $REC/procs-$2 ]]\n',
        'setpriv': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$REC/setpriv"\n'
                   '[[ -e $REC/procs-stay ]] || rm -f -- "$REC"/procs-*\n',
        # nft list: the table is there when $REC/nft is (the last one applied); nft -f -: applies (records) it.
        'nft': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$REC/nft-args"\n'
               'if [[ $1 == list ]]; then [[ -e $REC/nft ]]; exit; fi\ncat > "$REC/nft"\n[[ ! -e $REC/nft-fails ]]\n',
        'ip': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$REC/ip"\n[[ ! -e $REC/ip-fails ]] || exit 1\n'
              'case "$*" in "-n "*" link show lo") grep -qxF -- "$2" "$REC/netns" 2>/dev/null ;; '
              '"netns add "*) printf "%s\\n" "$3" >> "$REC/netns" ;; '
              '"link show "*) grep -qxF -- "$3" "$REC/links" 2>/dev/null ;; '
              '"link add "*) printf "%s\\n" "$3" >> "$REC/links" ;; esac\n',
        'iptables': '#!/usr/bin/env bash\nprintf "%s\\n" "$*" >> "$REC/iptables"\n[[ -e $REC/docker-user ]] || exit 1\n'
                    'case $3 in -S) exit 0 ;; '
                    '-C) [[ ! -e $REC/iptables-C-exit ]] || exit "$(cat "$REC/iptables-C-exit")"; '
                    'grep -qxF -- "${*:5}" "$REC/docker-user" ;; '
                    '-I) printf "%s\\n" "${*:6}" >> "$REC/docker-user" ;; *) exit 2 ;; esac\n',
        # The own-address lookups (OWN_ADDRESS_URLS https://own-X.test/): answer $REC/own-X, or fail when it is
        # missing; any other URL (check-net's probe) goes to the real curl.
        'curl': '#!/usr/bin/env bash\nfor a; do case $a in https://own-*.test/) a=${a#https://}; '
                '[[ -e $REC/${a%.test/} ]] || { echo "curl: (6) no answer" >&2; exit 6; }; '
                'exec cat -- "$REC/${a%.test/}" ;; esac; done\nexec /usr/bin/curl "$@"\n',
        'id': '#!/usr/bin/env bash\nf=$REC/id${1}-${!#}\nif [[ -e $f ]]; then cat -- "$f"; exit 0; fi\n'
              'for p in /usr/bin/id /bin/id; do [[ -x $p ]] && exec "$p" "$@"; done\nexit 127\n',
    }
    PRIVATE4 = ('0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 172.16.0.0/12 192.0.0.0/24 '
                '192.168.0.0/16 198.18.0.0/15 224.0.0.0/4 240.0.0.0/4')
    OWN = '198.51.100.20'          # this host's own public address, as the fake lookups answer
    OWN_URLS = 'https://own-a.test/,https://own-b.test/'
    WALL_PROPS = ('RestrictAddressFamilies=~AF_VSOCK',
                  'SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS',
                  'SystemCallArchitectures=native')
    USERS = ('nobody', 'daemon')   # two slots: two users of their own (in CI and here, each in its own group only)
    NAME = 'fleet-vm-t3code-11'
    JIT = 'SklUc2VjcmV0'
    INPUT = f'ACTIONS_RUNNER_INPUT_JITCONFIG={JIT}\nFORK_FLEET_RUN=5\nFORK_FLEET_ATTEMPT=1\n'

    def setUp(self):
        for user in self.USERS:
            try:
                pwd.getpwnam(user)
            except KeyError:
                self.skipTest(f'no user {user}')
            if subprocess.run(['id', '-G', user], capture_output=True, text=True).stdout.split() != \
                    [str(pwd.getpwnam(user).pw_gid)]:
                self.skipTest(f'{user} is in other groups here')
        # Folders as an install makes them, whatever this machine's umask: the launcher refuses a bound path under a
        # folder its group may write (closed_folder), and a test's own folders must not trip it.
        self.addCleanup(os.umask, os.umask(0o022))
        self.d = launcher_tmp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.rec = self.path('rec')
        os.mkdir(self.rec)
        os.mkdir(self.path('bin'))
        for name, text in self.FAKES.items():
            self.write(f'bin/{name}', text.replace('$REC', self.rec), 0o755)
        self.template = self.path('template')
        os.makedirs(self.path('template/bin'))
        self.write('template/run.sh', '#!/bin/sh\n', 0o755)
        self.write('template/bin/Runner.Listener', 'x', 0o755)
        self.write('template/.fork-fleet-version', f'{NEWEST_RUNNER}\n', 0o644)
        os.mkdir(self.path('linger'))
        self.write('resolv.conf', 'nameserver 1.1.1.1\n', 0o644)
        self.write('ip_forward', '1\n', 0o644)
        for x in 'ab':
            self.write(f'rec/own-{x}', f'{self.OWN}\n', 0o644)
        for d in ('template', 'template/bin'):
            os.chmod(self.path(d), 0o755)   # whatever this machine's umask is
        self.hook = self.write('hook.sh', '#!/bin/sh\n', 0o755)
        with open(os.path.join(os.path.dirname(LAUNCH), 'fork-fleet-job-paths.txt')) as f:
            self.job_paths = self.write('job-paths.txt', f.read(), 0o644)   # as installed: writable by root only
        self.write('meminfo', 'MemTotal:       16384000 kB\nMemAvailable:    8388608 kB\n', 0o644)
        self.conf()

    def path(self, rel):
        return os.path.join(self.d, rel)

    def write(self, rel, text, mode):
        p = self.path(rel)
        with open(p, 'w') as f:
            f.write(text)
        os.chmod(p, mode)
        return p

    def conf(self, extra='', **over):
        settings = dict(RUNNER_USERS=','.join(self.USERS), TEMPLATE=self.template, WORK_ROOT=self.path('var/runners'),
                        RUN_DIR=self.path('run'), HOOK=self.hook, LINGER_DIR=self.path('linger'),
                        RESERVE_MB='2048', MAX_SECONDS='3600', NET_BASE='10.204', RESOLV_CONF=self.path('resolv.conf'),
                        IP_FORWARD=self.path('ip_forward'), OWN_ADDRESS_URLS=self.OWN_URLS, OWN_ADDRESS_SECONDS='0',
                        MEMINFO=self.path('meminfo'), SYSTEMD_RUN=self.path('bin/systemd-run'),
                        SYSTEMCTL=self.path('bin/systemctl'), JOB_PATHS=self.job_paths)
        settings.update(over)
        self.write('launch.conf', '# fleet\n\n' + ''.join(f'{k}={v}\n' for k, v in settings.items()) + extra, 0o600)

    def env(self):
        return {'PATH': f'{self.path("bin")}:/usr/bin:/bin', 'FORK_FLEET_LAUNCH_CONF': self.path('launch.conf')}

    def launch(self, *args, stdin=''):
        return subprocess.run(['bash', LAUNCH, *args], input=stdin, env=self.env(), capture_output=True, text=True,
                              timeout=60)

    def rec_text(self, name):
        try:
            with open(os.path.join(self.rec, name)) as f:
                return f.read()
        except FileNotFoundError:
            return None

    def sandbox(self, user, root, rw=''):
        """The unit's sandbox, start's and check-net's alike: its own network namespace, the private ranges refused in
        the kernel too, of this host's files only the allow list (job_fs: its own root, the system paths read-only
        and one mount each, its copy, no mount of the host's later), its own /run, /dev/shm, /tmp and /var/tmp (so
        root never cleans a shared one), no /home, no other user's processes, its own IPC, only the pseudo devices,
        and the walls (no AF_VSOCK, io_uring or 32-bit system calls)."""
        props = (f'NetworkNamespacePath=/run/netns/fork-fleet-{user}', f'IPAddressDeny={self.PRIVATE4} {self.OWN} ::/0',
                 'IPAddressAllow=localhost', f'RootDirectory={root}', 'MountAPIVFS=yes', 'MountFlags=private',
                 'BindReadOnlyPaths=' + ' '.join(f'{os.path.realpath(p)}:{p}:norbind' for p in self.allowed()),
                 *([f'BindPaths={rw}:{rw}:norbind'] if rw else []),
                 'TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 /var/tmp:size=1G,mode=1777',
                 f'BindReadOnlyPaths={os.path.realpath(self.path("resolv.conf"))}:{os.path.realpath("/etc/resolv.conf")}',
                 'NoNewPrivileges=yes', 'ProtectSystem=strict', 'ProtectHome=yes', 'ProtectProc=invisible',
                 'PrivateIPC=yes', 'RemoveIPC=yes', 'PrivateDevices=yes', *self.WALL_PROPS)
        return [a for prop in props for a in ('-p', prop)]

    def allowed(self, extra=()):
        """The paths job_fs binds read-only here: JOB_PATHS's (one source: the file itself) and ALLOW_PATHS's that are
        here and no symlink, then the hook."""
        return [p for p in (*job_paths(), *extra) if os.path.exists(p) and not os.path.islink(p)] + [self.hook]

    def seen(self, rw=''):
        """The mount points check-net's probe is told to allow."""
        return ' '.join(['/', *self.allowed(), *([rw] if rw else []), os.path.realpath('/etc/resolv.conf')])

    def sources(self):
        """Where check-net's probe is told each bound point comes from: POINT=DEVICE:INODE of the real path job_fs
        checked and bound."""
        def of(p):
            st = os.stat(p)
            return f'{st.st_dev}:{st.st_ino}'
        return ' '.join([*(f'{p}={of(p)}' for p in self.allowed()),
                         f'{os.path.realpath("/etc/resolv.conf")}={of(self.path("resolv.conf"))}'])

    def test_start(self):
        got = self.launch('start', self.NAME, stdin=self.INPUT)
        self.assertEqual(got.returncode, 0, got.stderr)
        run_dir, dest = self.path('run'), self.path(f'var/runners/{self.NAME}')
        args = self.rec_text('systemd-run').splitlines()
        # Exactly these: no PrivateTmp (it leaves /dev/shm shared), nothing dropped.
        self.assertEqual(args, ['--quiet', '--unit', f'fork-fleet-{self.NAME}', '--collect', '--uid', 'nobody',
                                '--working-directory', dest, '-p', 'Type=exec', '-p', 'KillMode=control-group',
                                '-p', f'EnvironmentFile={run_dir}/{self.NAME}.env',
                                *self.sandbox('nobody', f'{run_dir}/{self.NAME}.root', dest),
                                '-p', f'ReadWritePaths={dest}', '-p', 'RuntimeMaxSec=3600', '-p', 'TimeoutStopSec=90',
                                '-p', 'TimeoutAbortSec=90', '--', f'{dest}/run.sh'])
        # HOME and TMPDIR inside the copy, which goes once its unit is gone: nothing of a job reaches the next one.
        self.assertEqual(self.rec_text('env'), self.INPUT + f'HOME={dest}/home\nTMPDIR={dest}/tmp\n'
                                                            f'ACTIONS_RUNNER_HOOK_JOB_STARTED={self.hook}\n')
        for d in ('home', 'tmp'):
            self.assertEqual(stat.S_IMODE(os.stat(f'{dest}/{d}').st_mode), 0o700)
        self.assertEqual(self.rec_text('env-mode').strip(), '600')
        self.assertFalse(os.path.exists(f'{run_dir}/{self.NAME}.env'))   # read by systemd, then removed
        self.assertEqual(os.stat(f'{dest}/run.sh').st_ino, os.stat(f'{self.template}/run.sh').st_ino)
        self.assertEqual(self.rec_text('chown'), f'-- nobody: {dest} {dest}/home {dest}/tmp\n')   # nothing else of it
        self.assertEqual(self.rec_text('pgrep'), f'{pwd.getpwnam("nobody").pw_uid}\n')   # its processes were checked
        self.assertEqual(stat.S_IMODE(os.stat(dest).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('var/runners')).st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(os.stat(run_dir).st_mode), 0o700)
        self.assertNotIn(self.JIT, got.stdout + got.stderr)

    def test_the_dispatchers_host_drives_it(self):
        pool = dict(POOLS[WAITING], launch=['bash', LAUNCH])
        host = ff.Host(pool, run=partial(subprocess.run, env=self.env()))
        host.start(f'{pool["label"]}-11', ff.runner_env('A' * 4000, a_run()))
        self.assertIn('FORK_FLEET_RUN=5', self.rec_text('env'))
        self.write('rec/units', f'fork-fleet-{pool["label"]}-11.service loaded active running x\n', 0o644)
        self.assertEqual(host.units(), {f'{pool["label"]}-11'})
        self.assertTrue(host.fits(1024))
        self.assertFalse(host.fits(8192))
        host.stop(f'{pool["label"]}-11')
        host.prune()
        with self.assertRaises(ff.HostError):
            host.start(f'{pool["label"]}-11', ff.runner_env('A', a_run()))   # that copy is still there

    def test_start_refuses_bad_input(self):
        good = dict(line.split('=', 1) for line in self.INPUT.splitlines())
        lines = lambda **over: ''.join(f'{k}={v}\n' for k, v in {**good, **over}.items() if v is not None)
        cases = {
            'the hook set from the input': (self.INPUT + 'ACTIONS_RUNNER_HOOK_JOB_STARTED=/tmp/x\n', 'unexpected'),
            'another variable': (self.INPUT + 'PATH=/tmp\n', 'unexpected'),
            'a line twice': (self.INPUT + 'FORK_FLEET_RUN=6\n', 'twice'),
            'no attempt': (lines(FORK_FLEET_ATTEMPT=None), 'needs'),
            'nothing': ('', 'needs'),
            'a config with a space': (lines(ACTIONS_RUNNER_INPUT_JITCONFIG='a b'), 'not well formed'),
            'a config with a substitution': (lines(ACTIONS_RUNNER_INPUT_JITCONFIG='$(id)'), 'not well formed'),
            'an empty config': (lines(ACTIONS_RUNNER_INPUT_JITCONFIG=''), 'not well formed'),
            'a config too long': (lines(ACTIONS_RUNNER_INPUT_JITCONFIG='A' * 65537), 'not well formed'),
            'run 0': (lines(FORK_FLEET_RUN='0'), 'not well formed'),
            'a run that is no number': (lines(FORK_FLEET_RUN='5x'), 'not well formed'),
        }
        for name, (stdin, word) in cases.items():
            with self.subTest(name):
                got = self.launch('start', self.NAME, stdin=stdin)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(word, got.stderr)
                self.assertNotIn(self.JIT, got.stderr)
        for bad in ('../x-1', 'Fleet-1', 'fleet-vm', 'a' * 70 + '-1', '-1', 'x-1/y'):
            with self.subTest(bad):
                got = self.launch('start', bad, stdin=self.INPUT)
                self.assertIn('not a runner name', got.stderr)
        self.assertIsNone(self.rec_text('systemd-run'))

    def test_the_longest_config_is_taken(self):
        got = self.launch('start', self.NAME, stdin=self.INPUT.replace(self.JIT, 'A' * 65536))
        self.assertEqual(got.returncode, 0, got.stderr)

    def refused(self, word, *args, stdin=None):
        got = self.launch(*(args or ('start', self.NAME)), stdin=self.INPUT if stdin is None else stdin)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn(word, got.stderr)
        self.assertIsNone(self.rec_text('systemd-run'))
        return got

    def test_a_job_sees_only_the_allow_list(self):
        """Fix round 1 (#191, security minor 3), the one mount rule (fork-fleet-job-paths.txt, read by job_fs): the
        unit's root is an empty folder of its own holding only the list's symlinks, and of this host's files it gets
        the list's paths (one mount each, read-only), ALLOW_PATHS's, the hook and its copy, nothing else. So a drive
        mounted anywhere else (a Windows drive at /c, a share at /data, /mnt, /media) is not in it, before or after the
        job starts, and the host's /run (WSL's interop sockets) is a new, empty one. LauncherAsRoot proves it for real
        (hidden_from_job)."""
        extra = '/var/cache'
        self.conf(ALLOW_PATHS=f'{extra},/no-such-fork-fleet-path')
        self.start(self.NAME)
        dest, root = self.path(f'var/runners/{self.NAME}'), self.path(f'run/{self.NAME}.root')
        args = self.rec_text('systemd-run').splitlines()
        props = [args[i + 1] for i, a in enumerate(args) if a == '-p']
        self.assertIn(f'RootDirectory={root}', props)
        self.assertIn('MountFlags=private', props)
        binds = [x.split(':')[0] for p in props if p.startswith(('BindReadOnlyPaths=', 'BindPaths='))
                 for x in p.split('=', 1)[1].split()]
        self.assertEqual(sorted(binds), sorted([*map(os.path.realpath, self.allowed([extra])), dest,
                                                os.path.realpath(self.path('resolv.conf'))]))
        self.assertIn('TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 '
                      '/var/tmp:size=1G,mode=1777', props)
        links = {os.path.join('/', os.path.relpath(os.path.join(d, x), root)): os.readlink(os.path.join(d, x))
                 for d, dirs, files in os.walk(root) for x in dirs + files if os.path.islink(os.path.join(d, x))}
        self.assertEqual(links, {p: os.readlink(p) for p in job_paths() if os.path.islink(p)})
        for d, dirs, files in os.walk(root):
            for x in files:
                self.assertTrue(os.path.islink(os.path.join(d, x)), os.path.join(d, x))
        self.write('rec/units', '', 0o644)
        self.assertEqual(self.launch('prune').returncode, 0)
        self.assertFalse(os.path.lexists(root))   # it goes with its unit

    def test_a_path_that_may_hold_data_is_never_allowed(self):
        """ALLOW_PATHS and the list itself: a plain absolute path, never one a job has its own of or that holds
        data of this host's; the list is root's only."""
        for bad in ('/home/x', '/root', '/tmp/x', '/run/x', '/proc', '/mnt/x', '/media/x', '/srv/x', '/a/../b',
                    '/a/./b', 'usr', '/a:b', '/a b'):
            with self.subTest(bad):
                self.conf(ALLOW_PATHS=bad)
                got = self.launch('start', self.NAME, stdin=self.INPUT)
                self.assertNotEqual(got.returncode, 0)
                self.assertIsNone(self.rec_text('systemd-run'))
        self.conf()
        os.chmod(self.job_paths, 0o664)
        self.refused('must be writable by its owner only')
        os.chmod(self.job_paths, 0o644)
        with open(self.job_paths, 'a') as f:
            f.write('/home\n')
        self.refused('a job never sees it from this host')

    def test_a_link_above_a_path_is_followed_before_any_check(self):
        """Fix round 2 (#191 security re-check R1, #194 both lenses' major): a symlink in a folder above a path
        (systemd follows it in a bind's source) never leads a job past the checks. Each path, of the list, ALLOW_PATHS,
        the hook, resolv.conf and WORK_ROOT, is resolved to its real path first: that is checked and that is bound."""
        if not os.path.islink('/var/run') or not os.path.isdir('/var/run/lock'):
            self.skipTest('no /var/run -> /run link here')
        self.conf(ALLOW_PATHS='/var/run/lock')   # it leads to /run/lock: the host's own /run
        self.refused('(that is /run/lock): a job never sees it')
        self.conf()
        with open(self.job_paths, 'a') as f:
            f.write('/var/run/lock\n')
        self.refused('(that is /run/lock): a job never sees it')

    def test_each_bind_comes_from_the_real_path(self):
        """The hook, resolv.conf and WORK_ROOT reached through a linked folder are bound from where they really are
        (the source the probe and the per-OS walls check), never through the link."""
        for d in ('real/hook', 'real/resolv', 'real/work'):
            os.makedirs(self.path(d))
        for link, to in (('hl', 'real/hook'), ('rl', 'real/resolv'), ('wl', 'real/work')):
            os.symlink(self.path(to), self.path(link))
        os.chmod(self.path('real/hook'), 0o755)   # the hook's folder: its owner's alone (#208 N1)
        hook = self.write('real/hook/hook.sh', '#!/bin/sh\n', 0o755)
        resolv = self.write('real/resolv/resolv.conf', 'nameserver 1.1.1.1\n', 0o644)
        self.conf(HOOK=self.path('hl/hook.sh'), RESOLV_CONF=self.path('rl/resolv.conf'),
                  WORK_ROOT=self.path('wl/runners'))
        self.start(self.NAME)
        args = self.rec_text('systemd-run').splitlines()
        binds = [x for i, a in enumerate(args) if a == '-p' and args[i + 1].startswith(('BindReadOnlyPaths=',
                 'BindPaths=')) for x in args[i + 1].split('=', 1)[1].split()]
        self.assertIn(f'{hook}:{self.path("hl/hook.sh")}:norbind', binds)
        self.assertIn(f'{resolv}:{os.path.realpath("/etc/resolv.conf")}', binds)
        work = self.path(f'real/work/runners/{self.NAME}')
        self.assertIn(f'{work}:{work}:norbind', binds)
        self.assertTrue(os.path.isdir(work))
        self.assertFalse([b for b in binds if '/hl/' in b.split(':')[0] or '/rl/' in b or '/wl/' in b])

    def test_a_folder_others_may_swap_a_link_into_is_refused(self):
        """The race: once the path has no link, swapping one in between the check and the bind takes write access to
        a folder above it. A folder others may write that is not sticky (like /tmp is) lets anyone: refused."""
        os.mkdir(self.path('open'))
        os.chmod(self.path('open'), 0o777)
        hook = self.write('open/hook.sh', '#!/bin/sh\n', 0o755)
        self.conf(HOOK=hook)
        self.refused('which others may write')
        os.chmod(self.path('open'), 0o1777)   # sticky: only the owner may rename or remove what is in it
        # Fix round 4 (#208 N1): but the hook's own folder must be its owner's alone (anyone may add a file beside it
        # in a sticky one), so the hook goes one folder down; the sticky folder above it passes.
        self.refused(f'{self.path("open")} must be writable by its owner only')
        os.mkdir(self.path('open/own'), 0o755)
        self.conf(HOOK=self.write('open/own/hook.sh', '#!/bin/sh\n', 0o755))
        # PR #213 re-check 2 (security N5): in a sticky folder others may write, the folder below it must be root's
        # (here: this user's): another user who made it may rename it away and put a link in its place (a stat that
        # says so: the tests cannot chown).
        self.write('bin/stat', '#!/bin/sh\n[ "$1 $2 $3" = "-c %u --" ] && case $4 in */open/own) echo 4242; exit 0;; '
                   'esac\nexec /usr/bin/stat "$@"\n', 0o755)
        # #226 item 6: every folder on the way is checked for its owner now, sticky one above it or not.
        self.refused(f'{self.path("open/own")}, a folder of uid 4242')
        os.chmod(self.path('open'), 0o1775)   # its group, the same
        self.refused(f'{self.path("open/own")}, a folder of uid 4242')
        os.chmod(self.path('open'), 0o1777)
        os.remove(self.path('bin/stat'))
        self.start(self.NAME)

    def test_the_folder_rule_says_the_fix(self):
        """PR #213 re-check 2 (correctness observation): the refusal names the folder and the fix."""
        os.makedirs(self.path('grp/own'))
        os.chmod(self.path('grp/own'), 0o755)
        os.chmod(self.path('grp'), 0o775)
        self.conf(HOOK=self.write('grp/own/hook.sh', '#!/bin/sh\n', 0o755))
        g = self.path('grp')
        self.refused(f'which others may write (its group or all): they could swap a link into it (chmod go-w {g}, '
                     f'or make it sticky: chmod +t {g})')

    def test_a_path_through_a_link_another_user_may_repoint_is_refused(self):
        """#211: a link on the way to a bound path (the hook's among them) is root's (or, in these tests, the user's
        who runs the launcher), in a folder no other user may write: a link another user owns, or may swap, could
        be repointed between start's check and the job. Each link on the way is checked, not only the real path."""
        os.makedirs(self.path('real/hook'), 0o755)
        os.chmod(self.path('real/hook'), 0o755)
        self.write('real/hook/hook.sh', '#!/bin/sh\n', 0o755)
        os.mkdir(self.path('pub'))
        os.chmod(self.path('pub'), 0o777)   # not sticky: anyone may swap a link in it
        os.symlink(self.path('real/hook'), self.path('pub/hl'))
        self.conf(HOOK=self.path('pub/hl/hook.sh'))
        self.refused(f'goes through {self.path("pub/hl")}, in {self.path("pub")}, which others may write')
        os.symlink(self.path('pub/hl'), self.path('l2'))   # a link to a link: each one on the way is checked
        self.conf(HOOK=self.path('l2/hook.sh'))
        self.refused(f'goes through {self.path("pub/hl")}, in {self.path("pub")}')
        os.chmod(self.path('pub'), 0o755)
        # A link another user owns (a stat that says so: the tests cannot chown): that user could repoint it.
        self.write('bin/stat', '#!/bin/sh\n[ "$1 $2 $3" = "-c %u --" ] && case $4 in */pub/hl) echo 4242; exit 0;; esac\n'
                   'exec /usr/bin/stat "$@"\n', 0o755)
        self.refused(f'goes through {self.path("pub/hl")}, a link of uid 4242')
        os.remove(self.path('bin/stat'))
        os.chmod(self.path('pub'), 0o1777)   # sticky: only a link's owner may swap it, and that is this user (root)
        self.start(self.NAME)

    def test_a_link_in_a_folder_another_user_owns_or_its_group_may_write_is_refused(self):
        """Fix round 1 (#213 S7, C5): a root link is safe only in root's folder that only root may write (or a sticky
        one): the folder's owner may remove any link in it, and a group-writable folder lets every member swap it.
        Through start and through `paths`, as the doc says (step 1b: "in a folder only its owner may write")."""
        os.makedirs(self.path('real/hook'), 0o755)
        os.chmod(self.path('real/hook'), 0o755)
        self.write('real/hook/hook.sh', '#!/bin/sh\n', 0o755)
        os.mkdir(self.path('grp'))
        os.symlink(self.path('real/hook'), self.path('grp/hl'))
        self.conf(HOOK=self.path('grp/hl/hook.sh'))
        for mode in (0o775, 0o2775, 0o770, 0o730):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path('grp'), mode)
                self.refused(f'goes through {self.path("grp/hl")}, in {self.path("grp")}, which others may write')
        os.chmod(self.path('grp'), 0o755)
        # The folder another user owns (a stat that says so: the tests cannot chown), sticky or not: that user may
        # remove the link and make another.
        self.write('bin/stat', '#!/bin/sh\n[ "$1 $2 $3" = "-c %u --" ] && case $4 in */grp) echo 4242; exit 0;; esac\n'
                   'exec /usr/bin/stat "$@"\n', 0o755)
        for mode in (0o755, 0o1777):
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path('grp'), mode)
                self.refused(f'goes through {self.path("grp/hl")}, in {self.path("grp")}, a folder of uid 4242')
        os.remove(self.path('bin/stat'))
        for mode, name in ((0o755, 'fleet-vm-t3code-11'), (0o1775, 'fleet-vm-t3code-12')):   # its owner's, or sticky
            with self.subTest(mode=oct(mode)):
                os.chmod(self.path('grp'), mode)
                self.start(name)

    def test_every_folder_above_a_bound_path_is_roots_or_this_users_and_slash_too(self):
        """#226 item 6: a folder above the real path that another user owns, closed (0755) or not, is that user's to
        rename entries in: they could move the hook's folder away and put a link in its place between the check and
        the bind. Only root's folders (or, in these tests, this user's) pass, and `/` is one of them."""
        os.makedirs(self.path('own/sub'))
        for d in ('own', 'own/sub'):
            os.chmod(self.path(d), 0o755)
        self.conf(HOOK=self.write('own/sub/hook.sh', '#!/bin/sh\n', 0o755))
        for folder, pattern in ((self.path('own'), '*/own'), ('/', '/')):
            with self.subTest(folder):
                self.write('bin/stat', '#!/bin/sh\n[ "$1 $2 $3" = "-c %u --" ] && case $4 in '
                           f'{pattern}) echo 4242; exit 0;; esac\nexec /usr/bin/stat "$@"\n', 0o755)
                self.refused(f', a folder of uid 4242: its owner could swap a link into it (only root\'s folders, or '
                             f'this user\'s: chown root: {folder}, or move the path out of it)')   # PR #240 fix 1
        os.remove(self.path('bin/stat'))
        self.start(self.NAME)

    def walls_root(self, conf, files=(), links=(), dirs=()):
        """A fake machine for `paths` (FORK_FLEET_WALLS_ROOT): its launch.conf, files, folders and links (relative,
        so they resolve inside it, as on the machine)."""
        root = self.path('machine')
        os.makedirs(root + '/etc/fork-fleet')
        for d in dirs:
            os.makedirs(root + d, exist_ok=True)
        for f, text in (*files, ('/etc/fork-fleet/launch.conf', conf)):
            os.makedirs(os.path.dirname(root + f), exist_ok=True)
            with open(root + f, 'w') as fh:
                fh.write(text)
            os.chmod(root + f, 0o644)
        for link, to in links:
            os.makedirs(os.path.dirname(root + link), exist_ok=True)
            os.symlink(to, root + link)
        for folder, _, _ in os.walk(root):   # as installed, any umask: a link's folder is its owner's alone
            os.chmod(folder, 0o755)
        return root

    def paths(self, root, conf='/etc/fork-fleet/launch.conf'):
        kit = self.path('kit')   # the launcher as installed: its list beside it, writable by its owner only
        if not os.path.isdir(kit):
            os.mkdir(kit)
            for f in (LAUNCH, os.path.join(os.path.dirname(LAUNCH), 'fork-fleet-job-paths.txt')):
                shutil.copy(f, kit)
                os.chmod(os.path.join(kit, os.path.basename(f)), 0o644)
        return subprocess.run(['bash', os.path.join(kit, os.path.basename(LAUNCH)), 'paths'], capture_output=True, text=True, timeout=60,
                              env={'PATH': '/usr/bin:/bin', 'FORK_FLEET_WALLS_ROOT': root,
                                   **({'FORK_FLEET_LAUNCH_CONF': conf} if conf else {})})

    def test_paths_is_what_job_fs_binds_with_every_link_resolved(self):
        """`paths` (read-only; what the per-OS walls check): each bound path and the real path it comes from, of
        the list, the hook (and its two files), WORK_ROOT and resolv.conf, on a fake machine. A line of the list
        through a link is bound from the real path; the lenses' cases are refused: a link to a Windows drive (/c),
        to /home, to a share; a link out of the machine."""
        conf = ('JOB_PATHS=/etc/fork-fleet/job-paths\nHOOK=/hl/hook.sh\nRESOLV_CONF=/rl/resolv.conf\n'
                'WORK_ROOT=/var/lib/ff/runners\n')
        root = self.walls_root(conf, files=(('/etc/fork-fleet/job-paths', '# list\n/usr\n/lib/x\n/bin\n'),
                                            ('/opt/ff/hook.sh', ''), ('/opt/ff/fork-fleet.py', ''),
                                            ('/run/r/resolv.conf', '')),
                               dirs=('/usr/lib/x', '/data/w', '/c/Users', '/home/u', '/data/s/x'),
                               links=(('/lib', 'usr/lib'), ('/bin', 'usr/bin'), ('/hl', 'opt/ff'), ('/rl', 'run/r'),
                                      ('/var/lib/ff', '../../data/w'), ('/opt/win', '../c'),
                                      ('/opt/h', '../home'), ('/usr/share/s', '../../data/s'), ('/opt/abs', '/')))
        got = self.paths(root)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(got.stdout.splitlines(), [
            '/usr /usr', '/lib/x /usr/lib/x', '/hl/hook.sh /opt/ff/hook.sh', '/hl/fork-fleet.py /opt/ff/fork-fleet.py',
            '/data/w/runners /data/w/runners', '/etc/resolv.conf /run/r/resolv.conf'])
        for allow, word in (('/opt/win/Users', '/opt/win/Users leads to /c/Users, which no line'),
                            ('/opt/h/u', '/opt/h/u (that is /home/u): a job never sees it'),
                            ('/usr/share/s/x', '/usr/share/s/x leads to /data/s/x, which no line'),
                            ('/opt/abs/usr', 'leads out of FORK_FLEET_WALLS_ROOT'),
                            ('/mnt/x', 'a job never sees it')):
            with self.subTest(allow):
                with open(root + '/etc/fork-fleet/launch.conf', 'w') as f:
                    f.write(conf + f'ALLOW_PATHS={allow}\n')
                got = self.paths(root)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn(word, got.stderr)
                self.assertEqual(got.stdout, '')

    def test_paths_refuses_a_link_in_a_folder_its_group_may_write(self):
        """Fix round 1 (#213 S7): `paths` (what the walls read) refuses what start refuses: the hook through a root
        link in a folder its group may write, which any member could swap between the check and the job."""
        root = self.walls_root('HOOK=/opt/shared/hl/hook.sh\n', files=(('/opt/ff/hook.sh', ''),),
                               links=(('/opt/shared/hl', '../ff'),))
        self.assertEqual(self.paths(root).returncode, 0)
        os.chmod(root + '/opt/shared', 0o775)
        got = self.paths(root)
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('/opt/shared/hl, in /opt/shared, which others may write', got.stderr)

    def test_paths_refuses_a_plain_folder_in_a_folder_its_group_may_write(self):
        """PR #213 re-check RS N5: a plain folder on the way to a bound path, in a folder its group may write, is
        refused as a link there is (closed_folder, one rule): a member could rename the folder away and put a link in
        its place between the check and the bind. Sticky, or only its owner may write: passes."""
        root = self.walls_root('HOOK=/opt/shared/ff/hook.sh\n', files=(('/opt/shared/ff/hook.sh', ''),))
        self.assertEqual(self.paths(root).returncode, 0)
        for mode in (0o775, 0o2775, 0o770, 0o757):
            with self.subTest(mode=oct(mode)):
                os.chmod(root + '/opt/shared', mode)
                got = self.paths(root)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn('/opt/shared/ff/hook.sh leads to /opt/shared/ff/hook.sh, under /opt/shared, which others '
                              'may write (its group or all)', got.stderr)
        for mode in (0o1775, 0o755):
            with self.subTest(mode=oct(mode)):
                os.chmod(root + '/opt/shared', mode)
                self.assertEqual(self.paths(root).returncode, 0, self.paths(root).stderr)

    def test_paths_refuses_a_bound_path_that_holds_data(self):
        """Fix round 3 (#191 re-check, correctness): the hook, its two files, WORK_ROOT and resolv.conf are bound into
        every job like the list's lines: one under /home, /root, /mnt, /media or /srv is refused, as written and once
        a link in a folder above is resolved. (Not /run, /tmp and the other own-or-none roots: systemd-resolved keeps
        resolv.conf in /run.)"""
        files = (('/home/o/kit/hook.sh', ''), ('/home/o/kit/fork-fleet.json', '{}'), ('/mnt/share/r/resolv.conf', ''),
                 ('/opt/ok/hook.sh', ''), ('/home/o/x.py', ''))
        dirs = ('/usr', '/srv/s')
        for name, conf, links, word in (
                ('hook to /home', 'HOOK=/opt/ff/hook.sh\n', (('/opt/ff', '../home/o/kit'),), '(that is /home/o/kit/hook.sh)'),
                ('resolv.conf to /mnt', 'RESOLV_CONF=/etc/ff/resolv.conf\n', (('/etc/ff', '../mnt/share/r'),),
                 '(that is /mnt/share/r/resolv.conf)'),
                ('WORK_ROOT to /srv', 'WORK_ROOT=/var/lib/ff/runners\n', (('/var/lib/ff', '../../srv/s'),),
                 '(that is /srv/s/runners)'),
                ('the file beside the hook to /home', 'HOOK=/opt/ok/hook.sh\n',
                 (('/opt/ok/fork-fleet.py', '../../home/o/x.py'),), '(that is /home/o/x.py)'),
                ('WORK_ROOT in /srv as written', 'WORK_ROOT=/srv/s/runners\n', (), '/srv/s/runners: a job never sees')):
            with self.subTest(name):
                if os.path.isdir(self.path('machine')):
                    shutil.rmtree(self.path('machine'))
                got = self.paths(self.walls_root(conf, files=files, links=links, dirs=dirs))
                self.assertNotEqual(got.returncode, 0, 'bound from a folder that holds data: ' + got.stdout)
                self.assertIn(word, got.stderr)
                self.assertIn('it holds data', got.stderr)
                self.assertEqual(got.stdout, '')

    def test_start_refuses_a_work_root_that_holds_data_and_an_open_file_beside_the_hook(self):
        """Fix round 3 (#191 re-check nits): start, not only `paths`: WORK_ROOT under /srv is refused before anything
        is made; the two files beside the hook (every job sees them) must be writable by their owner only, as the hook
        is."""
        self.conf(WORK_ROOT='/srv/fork-fleet/runners')
        self.refused('it holds data')
        self.conf()
        for f in ('fork-fleet.py', 'fork-fleet.json'):
            with self.subTest(f):
                self.write(f, '', 0o666)
                self.refused(f'{f} must be writable by its owner only')
                os.chmod(self.path(f), 0o644)
        self.start(self.NAME)

    def test_start_refuses_a_hook_folder_others_may_write(self):
        """Fix round 4 (#208 security N1): the two files beside the hook are checked by start but bound later (if they
        exist then), so the hook's folder must be writable by its owner only: in a shared folder (sticky, as /var/tmp
        is, or not) another user could make fork-fleet.py there in between, and every job would run it."""
        for mode, why in ((0o1777, f'{self.d} must be writable by its owner only'),
                          (0o775, f'under {self.d}, which others may write'),   # its group: the same rule
                          (0o777, f'under {self.d}, which others may write')):   # real_path's rule, first
            with self.subTest(oct(mode)):
                os.chmod(self.d, mode)
                self.refused(why)
        os.chmod(self.d, 0o755)
        self.start(self.NAME)

    def test_paths_reads_the_conf_the_launcher_would(self):
        """Fix round 2 (#194 minor 2): one lookup of launch.conf, FORK_FLEET_LAUNCH_CONF's or the default; none at
        all is the defaults (a host not set up yet). The test knob is for `paths` as a user only."""
        root = self.walls_root('ALLOW_PATHS=/opt/tools\n', dirs=('/opt/tools', '/opt/other'),
                               files=(('/etc/other.conf', 'ALLOW_PATHS=/opt/other\n'),))
        self.assertIn('/opt/tools /opt/tools\n', self.paths(root).stdout)
        got = self.paths(root, '/etc/other.conf')
        self.assertIn('/opt/other /opt/other\n', got.stdout)
        self.assertNotIn('/opt/tools', got.stdout)
        os.remove(root + '/etc/fork-fleet/launch.conf')
        got = self.paths(root)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('/var/lib/fork-fleet/runners /var/lib/fork-fleet/runners\n', got.stdout)
        self.assertIn('/etc/resolv.conf /etc/fork-fleet/resolv.conf\n', got.stdout)
        os.chmod(root + '/etc/other.conf', 0o666)
        self.assertIn('must be writable by its owner only', self.paths(root, '/etc/other.conf').stderr)
        self.conf()
        env = {**self.env(), 'FORK_FLEET_WALLS_ROOT': root}
        for args in (('start', self.NAME), ('check-net', 'https://example.com/', '127.0.0.1:1'), ('prune',)):
            with self.subTest(args):
                got = subprocess.run(['bash', LAUNCH, *args], env=env, capture_output=True, text=True, timeout=60)
                self.assertNotEqual(got.returncode, 0)
                self.assertIn("FORK_FLEET_WALLS_ROOT is set: it is for the walls' tests only", got.stderr)
        self.assertIsNone(self.rec_text('systemd-run'))
        with open(root + '/etc/fork-fleet/launch.conf', 'w') as f:
            f.write('JOB_PATHS=job-paths\n')
        os.chmod(root + '/etc/fork-fleet/launch.conf', 0o644)
        self.assertIn('JOB_PATHS is a plain absolute path', self.paths(root).stderr)

    def test_a_configured_template_is_refused(self):
        for f in ('.env', '.path', '.runner', '.credentials', '.credentials_rsaparams'):
            with self.subTest(f):
                self.write(f'template/{f}', 'ACTIONS_RUNNER_HOOK_JOB_STARTED=\n', 0o644)
                self.refused(f'{f} exists')
                os.remove(self.path(f'template/{f}'))

    def test_a_template_others_may_change_is_refused(self):
        os.chmod(self.path('template/bin/Runner.Listener'), 0o775)
        self.refused('others may write')
        os.chmod(self.path('template/bin/Runner.Listener'), 0o755)
        os.chmod(self.path('template/bin'), 0o757)
        self.refused('others may write')

    def test_a_template_that_is_a_symlink_is_refused(self):
        os.rename(self.template, self.path('real'))
        os.symlink(self.path('real'), self.template)
        self.refused('symlink')

    def test_a_hook_others_may_change_is_refused(self):
        os.chmod(self.hook, 0o775)
        self.refused('writable by its owner only')

    def test_a_conf_others_may_change_is_refused(self):
        os.chmod(self.path('launch.conf'), 0o620)
        self.refused('writable by its owner only', 'active')

    def test_the_conf_is_read_never_run(self):
        mark = self.path('mark')
        self.conf(RUNNER_USERS=f'$(touch${{IFS}}{mark})')
        self.refused('not a list of user names')
        self.assertFalse(os.path.exists(mark))
        self.conf(extra='PATH=/tmp\n')
        self.refused('not a setting', 'active')
        self.conf(extra=f'HOOK=$(touch {mark})\n')
        self.refused('not a setting', 'active')
        self.assertFalse(os.path.exists(mark))

    def test_the_runner_user_must_be_a_user_of_its_own(self):
        for user in ('root', pwd.getpwuid(os.geteuid()).pw_name):
            with self.subTest(user):
                self.conf(RUNNER_USERS=f'nobody,{user}')
                self.refused('a user of its own')
        self.conf(RUNNER_USERS='nobody,nobody')
        self.refused('are one user')
        self.conf(RUNNER_USERS='nobody,fork-fleet-no-such-user')
        self.refused('no user fork-fleet-no-such-user')
        self.conf()
        self.write('linger/daemon', '', 0o644)
        self.refused('daemon lingers')

    def start(self, name):
        got = self.launch('start', name, stdin=self.INPUT)
        self.assertEqual(got.returncode, 0, got.stderr)
        args = self.rec_text('systemd-run').splitlines()
        return args[args.index('--uid') + 1]

    def test_each_slot_runs_as_a_user_no_other_unit_runs_as(self):
        """Item 1: two runners at once never share a user (so never a HOME, TMPDIR or config)."""
        self.assertEqual([self.start('fleet-vm-t3code-11'), self.start('fleet-vm-t3code-12')], list(self.USERS))
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.refused('no slot is free', 'start', 'fleet-vm-t3code-13')
        self.write('rec/units', 'fork-fleet-fleet-vm-t3code-12.service loaded active running x\n', 0o644)   # 11 ended
        self.assertEqual(self.start('fleet-vm-t3code-14'), 'nobody')
        self.assertFalse(os.path.exists(self.path('var/runners/fleet-vm-t3code-11')))   # its copy went first
        os.remove(os.path.join(self.rec, 'user-fork-fleet-fleet-vm-t3code-12.service'))
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.refused('runs as no runner user', 'start', 'fleet-vm-t3code-15')

    def test_a_runner_users_leftover_processes_are_killed_first(self):
        uid = pwd.getpwnam('nobody').pw_uid
        self.write(f'rec/procs-{uid}', '', 0o644)
        self.assertEqual(self.start(self.NAME), 'nobody')
        self.assertEqual(self.rec_text('setpriv'), f'--reuid={uid} --regid={pwd.getpwnam("nobody").pw_gid} '
                                                   '--clear-groups -- /bin/bash --norc --noprofile -c kill -s KILL -- -1\n')
        self.write('rec/units', '', 0o644)
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.write(f'rec/procs-{uid}', '', 0o644)
        self.write('rec/procs-stay', '', 0o644)
        self.refused('processes of nobody are still running', 'start', 'fleet-vm-t3code-12')

    def test_the_copy_must_be_on_the_templates_filesystem(self):
        """cp -al hard-links: across filesystems it would fail half-way (or copy, writable by the job)."""
        shm = '/dev/shm'
        if not os.path.isdir(shm) or os.stat(shm).st_dev == os.stat(self.d).st_dev:
            self.skipTest('no second filesystem to test with')
        other = tempfile.mkdtemp(dir=shm)
        self.addCleanup(shutil.rmtree, other, True)
        self.conf(WORK_ROOT=os.path.join(other, 'runners'))
        self.refused('must be on one filesystem')

    def test_ready(self):
        """Item 6: the template's runner against the newest release; fail closed."""
        major, minor, patch = map(int, NEWEST_RUNNER.split('.'))
        for newest, ok in ((NEWEST_RUNNER, True), (f'{major}.{minor - 1}.9', True), (f'{major}.{minor}.{patch + 1}', False),
                           (f'{major}.{minor + 1}.0', False), (f'{major + 1}.0.0', False)):
            with self.subTest(newest):
                got = self.launch('ready', newest)
                self.assertEqual(got.returncode == 0, ok, got.stderr)
                if not ok:
                    self.assertIn(f'actions/runner {NEWEST_RUNNER}, older than the newest release {newest}', got.stderr)
                    self.assertIn('`fork-fleet-launch.sh refresh` refreshes the template', got.stderr)
                    self.assertIn('host step 7', got.stderr)
        self.assertIn('not a version', self.launch('ready', 'v2.330.0;id').stderr)
        self.write('template/.fork-fleet-version', 'latest\n', 0o644)
        self.assertIn('not a version', self.launch('ready', NEWEST_RUNNER).stderr)
        self.refused('not a version')
        os.remove(self.path('template/.fork-fleet-version'))
        self.assertIn('has no .fork-fleet-version', self.launch('ready', NEWEST_RUNNER).stderr)
        self.refused('has no .fork-fleet-version')

    def test_a_name_that_ran_already_is_refused(self):
        self.assertEqual(self.launch('start', self.NAME, stdin=self.INPUT).returncode, 0)
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.refused('ran already')

    def test_a_failed_start_leaves_no_environment_file(self):
        self.write('rec/systemd-run-exit', '3', 0o644)
        got = self.launch('start', self.NAME, stdin=self.INPUT)
        self.assertNotEqual(got.returncode, 0)
        self.assertEqual([n for n in os.listdir(self.path('run')) if n.endswith('.env')], [])

    def test_root_never_writes_through_a_link_planted_in_run_dir(self):
        """Root opens RUN_DIR's files without following a link: launch.lock only read-only (flock needs no more), a
        job's .env only as a new file. A link planted there (by whatever could once write RUN_DIR) is refused, and the
        file it points to stays as it was (a plain `>` would truncate the job hook or launch.conf, as root)."""
        run = self.path('run')
        os.makedirs(run, 0o700)
        os.chmod(run, 0o700)
        with open(self.hook) as f:
            hook = f.read()
        for planted, verbs in (('launch.lock', (('prune',), ('start', self.NAME))),
                               (f'{self.NAME}.env', (('start', self.NAME),))):
            for target in (self.hook, self.path('made-by-root')):   # a file, and a dangling link
                for verb in verbs:
                    with self.subTest(planted=planted, target=target, verb=verb[0]):
                        os.symlink(target, f'{run}/{planted}')
                        try:
                            got = self.launch(*verb, stdin=self.INPUT if verb[0] == 'start' else '')
                        finally:
                            os.remove(f'{run}/{planted}')
                        self.assertNotEqual(got.returncode, 0)
                        self.assertIn(f'{run}/{planted}', got.stderr)
                        self.assertIsNone(self.rec_text('systemd-run'))
                        with open(self.hook) as f:
                            self.assertEqual(f.read(), hook)
                        self.assertFalse(os.path.lexists(self.path('made-by-root')))
        os.remove(f'{run}/launch.lock')   # made by the .env cases' start (a plain file)
        os.mkfifo(f'{run}/launch.lock')   # not a plain file: refused before it is opened (a FIFO would hang)
        got = self.launch('prune')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('not a plain file', got.stderr)

    def test_active(self):
        self.write('rec/units', 'fork-fleet-fleet-vm-t3code-11.service loaded active running x\n'
                                'fork-fleet-bogus.service loaded active running x\n'
                                'fork-fleet-fleet-pc-t3code-12.service loaded deactivating stop-sigterm y\n', 0o644)
        got = self.launch('active')
        self.assertEqual((got.returncode, got.stdout), (0, 'fleet-vm-t3code-11\nfleet-pc-t3code-12\n'))
        self.write('rec/systemctl-fails', '', 0o644)
        self.assertNotEqual(self.launch('active').returncode, 0)

    def test_fits(self):
        free = 8388608 // 1024 - 2048
        self.assertEqual(self.launch('fits', str(free)).returncode, 0)
        self.assertEqual(self.launch('fits', str(free + 1)).returncode, 1)
        self.assertIn('not a number', self.launch('fits', '1e3').stderr)
        self.write('meminfo', 'MemTotal: 1 kB\n', 0o644)
        self.assertIn('no MemAvailable', self.launch('fits', '1').stderr)

    def test_fits_asks_the_free_script_too(self):
        """FREE_COMMAND (free/v1, fork-kit/common/FREE.md): after the memory rule, its answer through FREE_CHECK."""
        with open(os.path.join(HERE, '..', '..', 'fork-kit', 'common', 'free_contract.py')) as f:
            reader = self.write('free_contract.py', f.read(), 0o644)
        script = self.path('free')
        self.conf(FREE_COMMAND=script, FREE_CHECK=reader)
        free = 8388608 // 1024 - 2048

        def answer(code, **facts):
            at = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
            body = json.dumps({'v': 1, 'free': code == 0, 'at': at, **facts})
            self.write('free', f"#!/bin/sh\necho '{body}'\n: > {self.rec}/free-ran\nexit {code}\n", 0o755)
        answer(0)
        self.assertEqual(self.launch('fits', str(free)).returncode, 0)
        answer(75, reason='a game is running')
        got = self.launch('fits', '1')
        self.assertEqual(got.returncode, 75)
        self.assertIn('busy: a game is running', got.stderr)
        answer(0, memory={'freeMb': 1000})
        self.assertNotEqual(self.launch('fits', '1001').returncode, 0)
        self.assertEqual(self.launch('fits', '1000').returncode, 0)
        answer(3)
        self.assertIn('not available: exit 3', self.launch('fits', '1').stderr)
        answer(0)
        os.remove(self.path('rec/free-ran'))
        self.assertEqual(self.launch('fits', str(free + 1)).returncode, 1)
        self.assertFalse(os.path.exists(self.path('rec/free-ran')), 'the memory rule says no first')
        os.chmod(reader, 0o666)
        self.assertIn('writable by its owner only', self.launch('fits', '1').stderr)
        os.chmod(reader, 0o644)
        self.conf(FREE_COMMAND='free')
        self.assertIn('plain absolute paths', self.launch('fits', '1').stderr)

    def test_stop(self):
        self.assertEqual(self.launch('stop', self.NAME).returncode, 0)
        self.assertEqual(self.rec_text('stopped'), f'stop -- fork-fleet-{self.NAME}.service\n')
        self.assertIn('not a runner name', self.launch('stop', '*').stderr)

    def test_prune(self):
        root, run = self.path('var/runners'), self.path('run')
        for d in (root, run, self.path('elsewhere')):
            os.makedirs(d)
            os.chmod(d, 0o755)   # whatever this machine's umask is
        for name in ('fleet-vm-t3code-11', 'fleet-vm-t3code-12', 'notes'):
            os.makedirs(f'{root}/{name}/_work')
        self.write('run/fleet-vm-t3code-12.env', 'x', 0o600)
        os.symlink(self.path('elsewhere'), f'{root}/fleet-vm-t3code-13')
        self.write('rec/units', 'fork-fleet-fleet-vm-t3code-11.service loaded active running x\n', 0o644)
        self.write('rec/systemctl-fails', '', 0o644)
        self.assertNotEqual(self.launch('prune').returncode, 0)   # no unit list: nothing is removed
        self.assertEqual(len(os.listdir(root)), 4)
        os.remove(os.path.join(self.rec, 'systemctl-fails'))
        self.assertEqual(self.launch('prune').returncode, 0)
        self.assertEqual(sorted(os.listdir(root)), ['fleet-vm-t3code-11', 'fleet-vm-t3code-13', 'notes'])
        self.assertEqual(os.listdir(run), ['launch.lock'])
        self.assertTrue(os.path.isdir(self.path('elsewhere')))

    # Item 1 of round 3: each runner user's own network namespace, internet only, made (or found) before every start.
    NOBODY_NET, DAEMON_NET = '10.204.255.248', '10.204.0.4'   # uid % 16384 * 4 from NET_BASE: 65534 and 1

    def ip_calls(self, user, net, made=True):
        uid = pwd.getpwnam(user).pw_uid
        ns, host, peer, base = f'fork-fleet-{user}', f'ffh-{uid}', f'ffp-{uid}', net.rsplit('.', 1)[0]
        last = int(net.rsplit('.', 1)[1])
        return [f'-n {ns} link show lo', *([f'netns add {ns}'] if made else []), f'link show {host}',
                *([f'link add {host} type veth peer name {peer} netns {ns}'] if made else []),
                f'addr replace {base}.{last + 1}/30 dev {host}', f'link set {host} up', f'-n {ns} link set lo up',
                f'-n {ns} addr replace {base}.{last + 2}/30 dev {peer}', f'-n {ns} link set {peer} up',
                f'-n {ns} route replace default via {base}.{last + 1}']

    def test_each_runner_gets_a_network_of_its_own_with_internet_only(self):
        self.assertEqual(self.start('fleet-vm-t3code-11'), 'nobody')
        self.assertEqual(self.rec_text('ip').splitlines(), self.ip_calls('nobody', self.NOBODY_NET))
        self.assertEqual(self.rec_text('nft-args'), '-f -\n')
        table = self.rec_text('nft')
        # One transaction: the old table (if any) goes and the new one comes, or neither (nft -f is atomic).
        self.assertTrue(table.startswith('table inet fork_fleet\ndelete table inet fork_fleet\ntable inet fork_fleet {'))
        self.assertIn('elements = { ' + ', '.join(self.PRIVATE4.split()) + ' }', table)
        for rule in ('type filter hook input priority -10; policy accept;',
                     'iifname "ffh-*" counter reject with icmpx admin-prohibited',   # the host itself: nothing of it
                     'iifname "ffh-*" ip daddr @private4 counter reject with icmpx admin-prohibited',
                     f'iifname "ffh-*" ip daddr {self.OWN} counter reject with icmpx admin-prohibited',
                     'iifname "ffh-*" meta nfproto ipv6 counter drop',
                     'oifname "ffh-*" ct state != { established, related } counter drop',   # nothing opens to it
                     'ip saddr 10.204.0.0/16 oifname != "ffh-*" masquerade'):
            with self.subTest(rule):
                self.assertIn(f'    {rule}\n', table)
        self.assertEqual(len(re.findall(r'iifname "ffh-\*"', table)), 4)
        self.assertEqual(self.start('fleet-vm-t3code-12'), 'daemon')
        self.assertEqual(self.rec_text('ip').splitlines(),
                         self.ip_calls('nobody', self.NOBODY_NET) + self.ip_calls('daemon', self.DAEMON_NET))
        # The unit is put in it, and refused the same ranges again in the kernel (IPAddressDeny), should the table go.
        args = self.rec_text('systemd-run').splitlines()
        self.assertEqual(args[args.index('NetworkNamespacePath=/run/netns/fork-fleet-daemon') - 1], '-p')
        self.assertIn(f'IPAddressDeny={self.PRIVATE4} {self.OWN} ::/0', args)
        # The next job of that user: its namespace and link are there, nothing is made twice, the table is made again.
        os.remove(os.path.join(self.rec, 'ip'))
        self.write('rec/units', 'fork-fleet-fleet-vm-t3code-12.service loaded active running x\n', 0o644)
        self.assertEqual(self.start('fleet-vm-t3code-13'), 'nobody')
        self.assertEqual(self.rec_text('ip').splitlines(), self.ip_calls('nobody', self.NOBODY_NET, made=False))
        self.assertEqual(self.rec_text('nft-args'), '-f -\n' * 3)
        self.assertEqual(self.rec_text('netns'), 'fork-fleet-nobody\nfork-fleet-daemon\n')
        self.assertNotIn(' -I ', self.rec_text('iptables'))   # no DOCKER-USER chain here: nothing added

    def test_no_runner_starts_without_its_network(self):
        """Fail closed: when any part of the network cannot be made, or the host does not forward, no unit starts."""
        cases = {'nft fails': ('rec/nft-fails', 'network: nft -f - failed'),
                 'ip fails': ('rec/ip-fails', 'network: ip netns add fork-fleet-nobody failed')}
        for name, (mark, word) in cases.items():
            with self.subTest(name):
                self.write(mark, '', 0o644)
                self.refused(word)
                os.remove(self.path(mark))
        self.write('ip_forward', '0\n', 0o644)
        self.refused('ip_forward is not 1')
        self.write('ip_forward', '1\n', 0o644)
        self.write('resolv.conf', '# none\nsearch lan\n', 0o644)
        self.refused('names no nameserver')
        os.remove(self.path('resolv.conf'))
        self.refused('resolv.conf is missing')
        self.write('resolv.conf', 'nameserver 1.1.1.1\n', 0o666)
        self.refused('writable by its owner only')
        for bad in ('10.300', '192.168', '10.204.0', '10.04', '10.x'):
            with self.subTest(bad):
                self.conf(NET_BASE=bad)
                self.refused('NET_BASE is 10.N', 'active')

    def test_dockers_forward_policy_gets_its_two_rules_once(self):
        """On a host with Docker (FORWARD policy DROP) the table's traffic needs two accepts in DOCKER-USER: added
        once, never twice; an iptables that gives no clear answer stops the start."""
        self.write('rec/docker-user', '', 0o644)
        self.start('fleet-vm-t3code-11')
        self.start('fleet-vm-t3code-12')
        # (the fake appends; iptables -I 1 puts each at the top)
        self.assertEqual(self.rec_text('docker-user'), '-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT\n'
                                                       '-i ffh-+ -j ACCEPT\n')
        self.assertEqual(sum(1 for line in self.rec_text('iptables').splitlines() if ' -I ' in f' {line} '), 2)
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.write('rec/units', '', 0o644)
        self.write('rec/iptables-C-exit', '4', 0o644)
        self.refused('iptables -C DOCKER-USER gave no answer (exit 4)', 'start', 'fleet-vm-t3code-13')

    def test_own_address_is_looked_up_before_every_start_and_refused(self):
        """D1: this host's own public address (the way back into its forwarded ports): looked up at each URL before
        every start, blocked by the table and the unit. An odd, private or missing answer is left out; no answer at
        all: no runner."""
        self.write('rec/own-a', ' 203.0.113.9\r\n', 0o644)
        self.write('rec/own-b', '203.0.113.9', 0o644)
        self.start('fleet-vm-t3code-11')
        self.assertIn('    iifname "ffh-*" ip daddr 203.0.113.9 counter reject with icmpx admin-prohibited\n',
                      self.rec_text('nft'))
        self.assertIn(f'IPAddressDeny={self.PRIVATE4} 203.0.113.9 ::/0', self.rec_text('systemd-run').splitlines())
        os.remove(os.path.join(self.rec, 'systemd-run'))
        self.write('rec/units', '', 0o644)
        cases = {'a web page': ('<html>', 'answered no IPv4 address'), 'leading zero': ('203.0.113.09', 'no IPv4'),
                 'too big': ('203.0.113.256', 'no IPv4'), 'a LAN address': ('192.168.1.1', 'not a public address'),
                 'CGNAT': ('100.64.0.1', 'not a public address'), 'loopback': ('127.0.0.1', 'not a public address'),
                 'no answer': (None, 'gave no answer')}
        for name, (answer, word) in cases.items():
            with self.subTest(name):
                for x in 'ab':
                    if answer is None:
                        os.remove(self.path(f'rec/own-{x}'))
                    else:
                        self.write(f'rec/own-{x}', answer + '\n', 0o644)
                got = self.refused('no lookup (OWN_ADDRESS_URLS) gave this host\'s public IPv4 address')
                self.assertIn(word, got.stderr)
                self.refused('no lookup', 'check-net', 'https://example.com/', f'127.0.0.1:{self.server()}')
        for bad in ('http://own-a.test/', 'https://own-a.test/,', 'https://a.test/;id', ''):
            with self.subTest(bad):
                self.conf(OWN_ADDRESS_URLS=bad)
                self.refused('OWN_ADDRESS_URLS is a comma-separated list of https URLs', 'active')

    def test_every_address_the_lookups_give_is_blocked(self):
        """Fix round 1 (#191, correctness minor 2): two WAN links (each lookup sees another address) and one lookup
        that is down keep the pool open: every public address any lookup gives is blocked (the union), and an address
        blocked before stays blocked (the last 4, newest first), so a lookup that is down this time opens nothing."""
        self.write('rec/own-a', '203.0.113.8\n', 0o644)
        self.write('rec/own-b', '203.0.113.9\n', 0o644)
        self.start('fleet-vm-t3code-11')
        for a in ('203.0.113.8', '203.0.113.9'):
            self.assertIn(f'    iifname "ffh-*" ip daddr {a} counter reject with icmpx admin-prohibited\n',
                          self.rec_text('nft'))
        self.assertIn(f'IPAddressDeny={self.PRIVATE4} 203.0.113.8 203.0.113.9 ::/0',
                      self.rec_text('systemd-run').splitlines())
        os.remove(self.path('rec/own-b'))   # one lookup down, the other one answers: the pool stays open
        self.write('rec/units', '', 0o644)
        got = self.launch('start', 'fleet-vm-t3code-12', stdin=self.INPUT)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('own-b.test/ gave no answer', got.stderr)
        self.assertIn(f'IPAddressDeny={self.PRIVATE4} 203.0.113.8 203.0.113.9 ::/0',
                      self.rec_text('systemd-run').splitlines())   # .9, seen before, stays blocked
        for i, new in enumerate(('198.51.100.1', '198.51.100.2', '198.51.100.3', '198.51.100.4')):
            self.write('rec/own-a', f'{new}\n', 0o644)
            self.write('rec/units', '', 0o644)
            self.start(f'fleet-vm-t3code-{13 + i}')
        self.assertIn(f'IPAddressDeny={self.PRIVATE4} 198.51.100.4 198.51.100.3 198.51.100.2 198.51.100.1 ::/0',
                      self.rec_text('systemd-run').splitlines())
        self.assertEqual(self.rec_text('nft').count('ip daddr 198.51.100.'), 4)
        self.assertNotIn('203.0.113.', self.rec_text('nft'))

    def test_refresh_net_blocks_a_new_address_for_running_jobs(self):
        """Fix round 1 (#191, both lenses' minor 1): the address changes while a job runs. refresh-net (every
        dispatcher pass) looks it up again, re-applies the table (it covers every runner's link) and adds it to each
        running unit's IPAddressDeny; nothing changes when nothing would; no answer at all fails and keeps the last
        block."""
        self.start(self.NAME)
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertEqual(self.rec_text('nft-args'), '-f -\nlist table inet fork_fleet\n')   # the same: not applied
        self.assertIsNone(self.rec_text('set-property'))
        for x in 'ab':
            self.write(f'rec/own-{x}', '203.0.113.77\n', 0o644)
        got = self.launch('refresh-net')
        self.assertEqual(got.returncode, 0, got.stderr)
        for a in ('203.0.113.77', self.OWN):   # the new one, and the old one stays
            self.assertIn(f'    iifname "ffh-*" ip daddr {a} counter reject with icmpx admin-prohibited\n',
                          self.rec_text('nft'))
        self.assertEqual(self.rec_text('set-property'),
                         f'set-property --runtime -- fork-fleet-{self.NAME}.service IPAddressDeny=203.0.113.77 {self.OWN}\n')
        os.remove(os.path.join(self.rec, 'nft'))   # the table went (someone flushed the ruleset): made again
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertIn('ip daddr 203.0.113.77 counter reject', self.rec_text('nft'))
        table = self.rec_text('nft')
        for x in 'ab':
            os.remove(self.path(f'rec/own-{x}'))
        got = self.launch('refresh-net')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('no lookup', got.stderr)
        self.assertEqual(self.rec_text('nft'), table)   # the last block stays
        self.assertIn('refresh-net', self.launch('nope').stderr)

    def test_refresh_net_changes_nothing_for_a_reorder_and_retries_a_failed_unit(self):
        """Fix round 2 (#191 correctness re-check nits 5 and 6): the same addresses in another order (flapping
        lookups) re-apply nothing; a unit whose IPAddressDeny could not be changed is tried again next pass."""
        self.start(self.NAME)
        for x in 'ab':
            self.write(f'rec/own-{x}', '203.0.113.77\n', 0o644)
        self.write('rec/set-property-fails', '', 0o644)
        got = self.launch('refresh-net')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn(f'IPAddressDeny not changed for {self.NAME}', got.stderr)
        want = f'set-property --runtime -- fork-fleet-{self.NAME}.service IPAddressDeny=203.0.113.77 {self.OWN}\n'
        self.assertEqual(self.rec_text('set-property'), want)
        os.remove(self.path('rec/set-property-fails'))
        self.assertEqual(self.launch('refresh-net').returncode, 0)   # the same addresses: tried again
        self.assertEqual(self.rec_text('set-property'), want * 2)
        self.assertEqual(self.launch('refresh-net').returncode, 0)   # done: nothing more
        self.assertEqual(self.rec_text('set-property'), want * 2)
        for x in 'ab':   # the old address first now: the same two, so nothing is applied again
            self.write(f'rec/own-{x}', f'{self.OWN}\n', 0o644)
        os.remove(os.path.join(self.rec, 'nft-args'))
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertEqual(self.rec_text('nft-args'), 'list table inet fork_fleet\n')
        self.assertEqual(self.rec_text('set-property'), want * 2)

    def test_refresh_net_reuses_a_fresh_lookup_for_own_address_seconds(self):
        """Issue #204 item 2: refresh-net runs every dispatcher pass (20 s); it looks the address up again only once
        its last answer (RUN_DIR/own-lookup) is OWN_ADDRESS_SECONDS old (120 by default; 0: every pass), so a pool
        asks each lookup service 720 times a day, not 4320. A fresh answer is not enough when the table went or a
        unit still waits for its IPAddressDeny: looked up and done now. State: no answer yet (a reboot empties /run),
        a garbled one or one from the future (the clock set back): looked up; a failed lookup is not remembered."""
        self.conf(OWN_ADDRESS_SECONDS='120')
        self.start(self.NAME)
        stamp = self.path('run/own-lookup')

        def looked_up():   # with every lookup down, a pass that looks up fails; the answers come back after
            for x in 'ab':
                os.rename(self.path(f'rec/own-{x}'), self.path(f'rec/down-{x}'))
            try:
                got = self.launch('refresh-net')
            finally:
                for x in 'ab':
                    os.rename(self.path(f'rec/down-{x}'), self.path(f'rec/own-{x}'))
            if got.returncode == 0:
                return False
            self.assertIn('no lookup', got.stderr)
            return True
        self.assertFalse(os.path.exists(stamp))
        self.assertTrue(looked_up())   # none yet; and a failed one leaves none
        self.assertFalse(os.path.exists(stamp))
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertFalse(looked_up())
        now = int(time.time())
        for name, text in (('a stale answer', f'{now - 121}\n'), ('one from the future', f'{now + 600}\n'),
                           ('a garbled one', 'x\n'), ('an empty one', '')):
            with self.subTest(name):
                self.write('run/own-lookup', text, 0o600)
                self.assertTrue(looked_up())
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        os.remove(os.path.join(self.rec, 'nft'))   # the table went: looked up and made again
        self.assertTrue(looked_up())
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertIn(f'ip daddr {self.OWN} counter reject', self.rec_text('nft'))
        self.assertFalse(looked_up())
        for x in 'ab':   # a new address whose set-property fails: tried again next pass, so looked up again
            self.write(f'rec/own-{x}', '203.0.113.77\n', 0o644)
        self.write('run/own-lookup', f'{now - 121}\n', 0o600)
        self.write('rec/set-property-fails', '', 0o644)
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertTrue(looked_up())
        os.remove(self.path('rec/set-property-fails'))
        self.assertEqual(self.launch('refresh-net').returncode, 0)
        self.assertFalse(looked_up())
        self.conf(OWN_ADDRESS_SECONDS='0')
        self.assertTrue(looked_up())
        for bad in ('-1', '1e3', '999999'):
            with self.subTest(bad):
                self.conf(OWN_ADDRESS_SECONDS=bad)
                self.assertIn('OWN_ADDRESS_SECONDS', self.launch('refresh-net').stderr)

    def test_a_lookups_answer_never_reaches_the_terminal_raw(self):
        """Fix round 3 (#191 re-check S1): a lookup service's answer, curl's words and a caller's argument reach the
        terminal of whoever runs check-net, refresh-net or the walls proof by hand: printable ASCII only (an OSC 52
        clipboard write, a hidden OSC 8 link, a bidi mark become ?), at most 2000 bytes."""
        evil = '\x1b]52;c;aWQ+L3RtcC94Cg==\x07\u202e1.2.3.4'   # clipboard write, then a bidi override
        self.write('rec/own-a', evil + '\n', 0o644)
        self.write('rec/own-b', '203.0.113.9\n', 0o644)
        got = self.launch('refresh-net')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('answered no IPv4 address', got.stderr)
        for c in ('\x1b', '\x07', '\u202e'):
            self.assertNotIn(c, got.stderr, f'{c!r} from the lookup reached stderr raw: {got.stderr!r}')
        self.write('rec/own-a', 'x' * 9000 + '\n', 0o644)
        got = self.launch('refresh-net')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertTrue(all(len(line) <= 2000 + len('fork-fleet-launch: ') for line in got.stderr.splitlines()))
        got = self.launch('check-net', 'https://example.com/\x1b]8;;x\x07', '127.0.0.1:1')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('not a plain http(s) URL', got.stderr)
        self.assertNotIn('\x1b', got.stderr)
        got = self.launch('nope\x1b[2J')
        self.assertNotEqual(got.returncode, 0)
        self.assertNotIn('\x1b', got.stderr)

    def test_check_nets_probe_lines_reach_the_terminal_as_printable_text(self):
        """Fix round 4 (#208 security S1, correctness nit 2): check-net's walls probe names host files any local user
        may make (a socket in /tmp or /dev/shm, a FUSE mount point) and the job's view of them: every probe line goes
        through the launcher's own shown() (shown_lines), so ESC, BEL or a bidi mark never reach the terminal raw."""
        evil = '\x1b]52;c;aWQK\x07\u202ex'
        port = self.server()
        # The job's side (both units' output, the fake systemd-run prints it).
        self.write('rec/probe-exit', '0', 0o644)
        self.write('rec/probe-out', f'ok    sockets: {evil}.sock inside\n', 0o644)
        # The host's side: a socket with such a name where the host probe looks (it may not reach it on a host with
        # a big /tmp: its budget; the probe itself is run below on a folder of its own).
        if os.path.isdir('/dev/shm') and os.access('/dev/shm', os.W_OK):
            shm = tempfile.mkdtemp(dir='/dev/shm', prefix='!fork-fleet-test-')
            self.addCleanup(shutil.rmtree, shm, True)
            sock = socket.socket(socket.AF_UNIX)
            self.addCleanup(sock.close)
            sock.bind(os.path.join(shm, evil + '.sock'))
        got = self.check_net(f'127.0.0.1:{port}')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('ok    sockets: ?]52;c;aWQK????x.sock inside\n', got.stdout)
        self.assertIn('host: sockets: ', got.stdout)
        for c in ('\x1b', '\x07', '\u202e'):
            self.assertNotIn(c, got.stdout + got.stderr)
        # The lens's case: the host probe on a folder holding that socket, through the launcher's shown_lines.
        with open(LAUNCH) as f:
            text = f.read()
        funcs = ''.join(text[text.index(f'\n{name}() {{\n') + 1:].split('\n}\n', 1)[0] + '\n}\n'
                        for name in ('shown', 'shown_lines'))
        roots = "('/tmp', '/var/tmp', '/run', '/dev/shm')"
        self.assertIn(roots, walls_probe())
        d = launcher_tmp()
        self.addCleanup(shutil.rmtree, d, True)
        sock = socket.socket(socket.AF_UNIX)
        self.addCleanup(sock.close)
        sock.bind(os.path.join(d, evil + '.sock'))
        out = subprocess.run(['bash', '-c', funcs + '/usr/bin/python3 -I -c "$W" host "" "" 2>&1 | shown_lines'],
                             env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C.UTF-8',
                                  'W': walls_probe().replace(roots, repr((d,)))},
                             capture_output=True, timeout=120).stdout
        self.assertIn(b'host: sockets: ', out)
        self.assertIn(b'?]52;c;aWQK????x.sock', out)
        for c in (b'\x1b', b'\x07', '\u202e'.encode()):
            self.assertNotIn(c, out)

    def test_every_lookups_address_stays_blocked_with_more_than_four_lookups(self):
        """Fix round 3 (#191 re-check nit): the last-4 limit never drops an address a lookup gives now: with five
        lookups that each see another address, all five are blocked."""
        urls = [f'https://own-{x}.test/' for x in 'abcde']
        self.conf(OWN_ADDRESS_URLS=','.join(urls))
        for i, x in enumerate('abcde'):
            self.write(f'rec/own-{x}', f'203.0.113.{10 + i}\n', 0o644)
        self.start(self.NAME)
        deny = next(a for a in self.rec_text('systemd-run').splitlines() if a.startswith('IPAddressDeny='))
        for i in range(5):
            self.assertIn(f' 203.0.113.{10 + i} ', deny)
            self.assertIn(f'ip daddr 203.0.113.{10 + i} counter reject', self.rec_text('nft'))

    def test_a_runner_user_in_another_group_is_refused(self):
        """A runner user also in, say, docker (root on the host) or adm (the logs) is never handed a job."""
        gid = pwd.getpwnam('daemon').pw_gid
        self.write('rec/id-G-daemon', f'{gid} 999\n', 0o644)
        self.refused('daemon is in other groups')
        self.write('rec/id-G-daemon', f'{gid}\n', 0o644)
        self.assertEqual(self.start(self.NAME), 'nobody')

    def test_two_users_that_would_share_a_network_are_refused(self):
        self.write('rec/id-u-daemon', '16382\n', 0o644)   # 65534 % 16384 too: nobody's /30
        self.refused('daemon and nobody would share one network')

    def test_one_launcher_at_a_time(self):
        """launch.lock: a second launcher (the other pool's dispatcher) waits until the first is done."""
        import fcntl, time
        os.mkdir(self.path('run'), 0o700)
        with open(self.path('run/launch.lock'), 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            stdin = self.write('input', self.INPUT, 0o600)
            with open(stdin) as i, open(os.path.join(self.rec, 'err'), 'w') as e:
                proc = subprocess.Popen(['bash', LAUNCH, 'start', self.NAME], stdin=i, stdout=subprocess.DEVNULL,
                                        stderr=e, env=self.env())
            self.addCleanup(proc.kill)
            time.sleep(3)
            self.assertIsNone(proc.poll())   # waiting for the lock: nothing made, nothing started
            self.assertIsNone(self.rec_text('systemd-run'))
            self.assertIsNone(self.rec_text('ip'))
        self.assertEqual(proc.wait(30), 0, self.rec_text('err'))
        self.assertIsNotNone(self.rec_text('systemd-run'))

    # check-net, the proof of the network (.github/fork-fleet.md, host step 6).
    def server(self):
        import http.server, threading
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)
        srv.RequestHandlerClass.log_message = lambda *a: None
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return srv.server_address[1]

    def closed_port(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            return s.getsockname()[1]

    def test_check_net_refuses_what_proves_nothing(self):
        port = self.server()
        cases = {('check-net',): 'usage: check-net', ('check-net', 'https://example.com'): 'usage: check-net',
                 ('check-net', 'ftp://example.com', f'127.0.0.1:{port}'): 'not a plain http(s) URL',
                 ('check-net', 'https://example.com/$(id)', f'127.0.0.1:{port}'): 'not a plain http(s) URL',
                 ('check-net', 'https://example.com', f'localhost:{port}'): 'not an IPv4 HOST:PORT',
                 ('check-net', 'https://example.com', f'127.0.0.1:{port};id'): 'not an IPv4 HOST:PORT',
                 # A service this host does not reach either: its refusal inside would say nothing.
                 ('check-net', 'https://example.com', f'127.0.0.1:{port}', f'127.0.0.1:{self.closed_port()}'):
                     'would prove nothing'}
        for args, word in cases.items():
            with self.subTest(args):
                self.refused(word, *args)

    def check_net(self, *targets, url='https://example.com/'):
        return self.launch('check-net', url, *targets)

    def test_check_net_runs_its_probe_in_a_runners_own_sandbox(self):
        port = self.server()
        self.write('rec/probe-exit', '0', 0o644)
        got = self.check_net(f'127.0.0.1:{port}')
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn('the runners reach https://example.com/ and none of the 1 services named nor this host\'s own '
                      'public address; AF_VSOCK, io_uring and 32-bit system calls are closed', got.stdout)
        self.assertIn('host: io_uring ', got.stdout)   # the control, outside: what this host has
        args = self.waited('fork-fleet-check-net')
        dash = args.index('--')
        # The very sandbox a job gets (the same props as start), as a free runner user, in its namespace.
        self.assertEqual(args[:dash], ['--quiet', '--wait', '--pipe', '--collect', '--unit', 'fork-fleet-check-net',
                                       '--uid', 'nobody', *self.sandbox('nobody', self.path('run/check-net.root')),
                                       '-p', 'RuntimeMaxSec=120'])
        self.assertEqual(args[dash + 1:dash + 4], ['/bin/bash', '--norc', '--noprofile'])
        self.assertEqual(args[dash + 4], '-c')
        self.assertEqual(args[dash + 6:], ['check-net', 'https://example.com/', self.OWN, '127.0.0.1', str(port)])
        # The walls' probe: a unit of its own, the same sandbox.
        args = self.waited('fork-fleet-check-walls')
        dash = args.index('--')
        self.assertEqual(args[:dash], ['--quiet', '--wait', '--pipe', '--collect', '--unit', 'fork-fleet-check-walls',
                                       '--uid', 'nobody', *self.sandbox('nobody', self.path('run/check-net.root')),
                                       '-p', 'RuntimeMaxSec=60'])
        self.assertEqual(args[dash + 1:dash + 4], ['/usr/bin/python3', '-I', '-c'])
        self.assertEqual(args[dash + 5:], ['job', self.seen(), self.sources()])
        self.assertEqual(self.rec_text('ip').splitlines(), self.ip_calls('nobody', self.NOBODY_NET))
        self.write('rec/probe-exit', '1', 0o644)
        got = self.check_net(f'127.0.0.1:{port}')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn('NOT as they must be', got.stderr)

    def waited(self, unit):
        with open(os.path.join(self.rec, f'{unit}.0')) as f:
            return f.read().split('\0')[:-1]

    def test_check_nets_probe_fails_on_anything_reachable(self):
        """The probes themselves, run here outside any namespace or filter: the service this host reaches is
        reachable, its own address is not refused by a wall, io_uring gives a ring, so they fail."""
        port = self.server()
        if os.path.isdir('/dev/shm'):   # a socket where a desktop keeps them (WSLg's): the probe must name it
            shm = tempfile.mkdtemp(dir='/dev/shm', prefix='fork-fleet-test-')
            self.addCleanup(shutil.rmtree, shm, True)
            sock = socket.socket(socket.AF_UNIX)
            sock.bind(os.path.join(shm, 's'))
            sock.close()
        self.write('rec/own-a', '127.0.0.2\n', 0o644)   # the lookup refuses it; only its probe's own check is wanted
        got = self.check_net(f'127.0.0.1:{port}', url=f'http://127.0.0.1:{port}/')
        self.assertIn('not a public address', got.stderr)
        self.write('rec/own-a', f'{self.OWN}\n', 0o644)
        got = self.check_net(f'127.0.0.1:{port}', url=f'http://127.0.0.1:{port}/')
        self.assertNotEqual(got.returncode, 0)
        self.assertIn(f'FAIL  127.0.0.1:{port} is reachable from inside', got.stdout)
        self.assertIn(f'FAIL  this host\'s own public address {self.OWN} is not refused by the walls', got.stdout)
        if os.path.exists('/proc/sys/kernel/io_uring_disabled'):   # a kernel with io_uring
            self.assertRegex(got.stdout, r'FAIL  io_uring (gives a ring inside|is not refused by the unit)')
        self.assertIn(f'ok    http://127.0.0.1:{port}/ answers (HTTP 200)', got.stdout)
        self.assertIn('NOT as they must be', got.stderr)
        # This host's own file system, as no unit has it: its devices, its mounts outside the list, and mounts that
        # receive the host's later ones (systemd makes / shared) all fail.
        with open('/proc/self/mountinfo') as f:
            info = [line.split() for line in f]
        if os.path.exists('/dev/console'):
            self.assertIn('FAIL  devices: host ones inside:', got.stdout)
        if any(f[4].startswith(('/boot', '/home', '/mnt', '/media', '/srv', '/run/')) for f in info):
            self.assertIn('FAIL  mounts: not allowed inside:', got.stdout)
        if any(t.startswith(('shared:', 'master:')) for f in info for t in f[6:f.index('-')]):
            self.assertIn("FAIL  later mounts: the host's reach", got.stdout)
        # Fix round 2 (#191 correctness re-check minor 1): the PC proof's evidence for WSL's drives and WSLg's sockets.
        if any(os.listdir(d) for d in ('/mnt', '/media') if os.path.isdir(d)):
            self.assertIn('FAIL  /mnt and /media: hold ', got.stdout)
        if os.path.isdir('/dev/shm'):
            self.assertIn('FAIL  sockets: ', got.stdout)   # LauncherAsRoot names them, in a mount namespace
        # Each bound point is the very file or folder job_fs checked (seen from outside, by device and inode): here
        # they are, and a point whose source is another one (a link swapped in before the bind) fails.
        # Here, outside the unit, only resolv.conf is another file (the host's, not the jobs').
        resolv = os.path.realpath('/etc/resolv.conf')
        self.assertIn(f'FAIL  mount sources: not what job_fs checked: {resolv}\n', got.stdout)
        args = self.waited('fork-fleet-check-walls')
        code, job, seen, sources = args[args.index('--') + 4:]
        sources = ' '.join(x for x in sources.split() if not x.startswith(resolv + '='))

        def probe(sources):
            return subprocess.run(['python3', '-I', '-c', code, job, seen, sources], capture_output=True, text=True,
                                  timeout=60).stdout
        self.assertIn('ok    mount sources: each the host path job_fs checked\n', probe(sources))
        usr = os.stat('/usr')
        swapped = sources.replace(f'/usr={usr.st_dev}:{usr.st_ino}', f'/usr={usr.st_dev}:{usr.st_ino + 1}')
        self.assertNotEqual(swapped, sources)
        self.assertIn('FAIL  mount sources: not what job_fs checked: /usr\n', probe(swapped))
        self.assertIn('FAIL  mount sources: none given\n', probe(''))

    def test_the_walls_hold_in_a_real_systemd_unit(self):
        """The walls' probe in a real unit of this user's systemd, with the walls check-net gives it: AF_VSOCK,
        io_uring and 32-bit system calls are closed. Without them (the red check) the probe fails on this host."""
        port = self.server()
        self.write('rec/probe-exit', '0', 0o644)
        self.assertEqual(self.check_net(f'127.0.0.1:{port}').returncode, 0)
        real_walls(self, self.waited('fork-fleet-check-walls'), ['systemd-run', '--user'])

    def test_usage(self):
        for args in ((), ('start',), ('active', 'x'), ('nope',), ('ready',)):
            with self.subTest(args):
                self.assertIn('usage', self.launch(*args).stderr)
        self.assertIn('ready NEWEST', self.launch().stderr)
        self.assertIn('check-net URL HOST:PORT...', self.launch().stderr)


def real_walls(test, args, systemd_run, *more, full=False):
    """Runs check-net's walls' probe (args: its recorded systemd-run argv) in a real unit of `systemd_run`. Here (a
    user's systemd, no mount namespace) only with the walls and NoNewPrivileges of the recorded sandbox: every system
    call line says ok. full (root, CI): with all of the recorded sandbox but its network (a namespace that is not this
    test's): every line says ok (devices, mounts, later mounts, sockets too), the tools a job uses work inside, and a
    mount the host makes after the unit started does not reach it (the red check: without MountFlags=private it does).
    Then without the walls: where this host has io_uring, the probe fails."""
    if subprocess.run([*systemd_run, '--wait', '--quiet', '--collect', '--', '/bin/true'], capture_output=True,
                      timeout=60).returncode != 0:
        test.skipTest(f'no systemd to run a unit with: {" ".join(systemd_run)}')
    dash = args.index('--')
    props = [args[i + 1] for i in range(dash) if args[i] == '-p']
    walls = [p for p in props if p.split('=')[0] in
             ('RestrictAddressFamilies', 'SystemCallFilter', 'SystemCallArchitectures', 'NoNewPrivileges')]
    test.assertEqual(len(walls), 4, walls)
    if full:
        walls = [p for p in props if p.split('=')[0] not in ('NetworkNamespacePath', 'IPAddressDeny', 'IPAddressAllow')]
    unit = [*systemd_run, '--wait', '--pipe', '--quiet', '--collect', *more, *(a for w in walls for a in ('-p', w))]
    probe = args[dash + 1:]
    got = subprocess.run([*unit, '--', *probe], capture_output=True, text=True, timeout=120)
    test.assertRegex(got.stdout, r'ok    AF_VSOCK is refused')
    test.assertIn('ok    io_uring is refused (ENOSYS)', got.stdout)
    if os.uname().machine == 'x86_64':
        test.assertRegex(got.stdout, r'ok    i386 system calls (end the process \(SIGSYS\)|do not work on this kernel)')
        test.assertRegex(got.stdout, r'ok    x32 system calls (end the process \(SIGSYS\)|do not work on this kernel)')
    test.assertNotRegex(got.stdout, r'FAIL  (AF_VSOCK|io_uring|i386|x32)')
    if full:
        test.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        test.assertNotIn('FAIL', got.stdout)
        for want in ('ok    devices: only the pseudo ones', 'ok    mounts: only the allowed ones',
                     "ok    later mounts: no mount inside receives the host's", 'ok    /mnt and /media: nothing inside',
                     'ok    sockets: none in /tmp, /var/tmp, /run, /dev/shm',
                     'ok    mount sources: each the host path job_fs checked'):
            test.assertIn(want, got.stdout)
        tools = [t for t in (['git', '--version'], ['node', '--version'], ['java', '-version'], ['docker', '--version'])
                 if shutil.which(t[0]) and os.path.realpath(shutil.which(t[0])).startswith(('/usr/', '/etc/'))]
        for t in tools + [['head', '-c', '16', '/dev/urandom']]:
            ran = subprocess.run([*unit, '--', shutil.which(t[0]), *t[1:]], capture_output=True, timeout=120)
            test.assertEqual(ran.returncode, 0, (t, ran.stdout, ran.stderr))
        hidden_from_job(test, unit, walls)
    bare = subprocess.run([*systemd_run, '--wait', '--pipe', '--quiet', '--collect', *more, '-p', 'NoNewPrivileges=yes',
                           '--', *probe], capture_output=True, text=True, timeout=120)
    if 'io_uring gives a ring' in subprocess.run(probe, capture_output=True, text=True, timeout=60).stdout:
        test.assertEqual(bare.returncode, 1, bare.stdout + bare.stderr)
        test.assertIn('FAIL  io_uring gives a ring inside', bare.stdout)


LATE = """import json, os, sys
print('started', flush=True)
sys.stdin.readline()
print(json.dumps({p: os.path.lexists(p) for p in sys.argv[1:]}), flush=True)
"""


def without(cmd, prop):
    """cmd without its `-p prop` pair."""
    out = []
    for a in cmd:
        if a == prop and out and out[-1] == '-p':
            out.pop()
        else:
            out.append(a)
    return out


def hidden_from_job(test, unit, props):
    """What a job must never see, whenever the host makes it (as root, in a real unit with the recorded sandbox):
    a drive mounted outside /mnt and /media (a Windows drive at /c, a share at /data) before the job starts and one
    mounted after it started; a socket in the host's /run (where WSL's interop sockets live, /run/WSL); and a mount the
    host makes under an ALLOWED path after the job started (a share mounted while a job runs). The red check: without
    MountFlags=private the last one does reach the job (the rest stay hidden: they are not in its root at all)."""
    import json
    allow = [p for p in props if p.startswith('BindReadOnlyPaths=') and ':norbind' in p][0]
    late = [x.split(':')[0] for x in allow.split('=', 1)[1].split() if x.split(':')[0].startswith('/var/lib/')][0]
    sub = os.path.join(late, 'share')
    os.makedirs(sub, exist_ok=True)
    drives = [tempfile.mkdtemp(prefix=f'fork-fleet-drive-{x}-', dir='/') for x in ('before', 'after')]
    run = tempfile.mkdtemp(prefix='fork-fleet-interop-', dir='/run')
    for d in (*drives, run):
        test.addCleanup(os.rmdir, d)
    mounted = []

    def mount(d):
        subprocess.run(['mount', '-t', 'tmpfs', '-o', 'size=1m', 'fork-fleet-test', d], check=True)
        mounted.append(d)
        with open(os.path.join(d, 'marker'), 'w') as f:
            f.write('x')

    def umount_all():
        while mounted:
            subprocess.run(['umount', mounted.pop()], check=True)
    test.addCleanup(umount_all)
    mount(drives[0])
    sock = socket.socket(socket.AF_UNIX)
    sock.bind(os.path.join(run, 'interop'))
    test.addCleanup(os.remove, os.path.join(run, 'interop'))
    test.addCleanup(sock.close)
    paths = [os.path.join(drives[0], 'marker'), drives[0], os.path.join(drives[1], 'marker'), drives[1],
             os.path.join(run, 'interop'), run, os.path.join(sub, 'marker')]

    def seen(cmd):
        with subprocess.Popen([*cmd, '--', '/usr/bin/python3', '-I', '-c', LATE, *paths], stdin=subprocess.PIPE,
                              stdout=subprocess.PIPE, text=True) as job:
            test.assertEqual(job.stdout.readline().strip(), 'started')
            mount(drives[1])
            mount(sub)
            try:
                answer, _ = job.communicate('go\n', timeout=60)
            finally:
                for d in (sub, drives[1]):
                    mounted.remove(d)
                    subprocess.run(['umount', d], check=True)
        return {p for p, there in json.loads(answer).items() if there}

    test.assertEqual(seen(unit), set())
    test.assertEqual(seen(without(unit, 'MountFlags=private')), {os.path.join(sub, 'marker')})


# LauncherAsRoot's systemd-run for check-net: the unit's namespace and user, the rest of its sandbox left out.
NETNS_SYSTEMD_RUN = """#!/usr/bin/env bash
ns='' user=''
while (($#)); do
  case $1 in
    --uid) user=$2 && shift ;;
    -p) [[ $2 != NetworkNamespacePath=* ]] || ns=${2#NetworkNamespacePath=}; shift ;;
    --) shift && break ;;
  esac
  shift
done
[[ -n $ns && -n $user ]] || { echo "fake systemd-run: no namespace or no user" >&2; exit 99; }
# The walls' probe needs the unit's filters, which this fake cannot give: test_the_walls_hold_in_a_real_systemd_unit.
[[ $1 != /usr/bin/python3 ]] || exit 0
exec nsenter --net="$ns" -- setpriv --reuid="$(id -u -- "$user")" --regid="$(id -g -- "$user")" --clear-groups -- "$@"
"""


class WallsProof(unittest.TestCase):
    """fork-fleet-walls-proof.sh proves the walls on a new kind of host, as root, with throwaway users. Here: it
    parses, refuses without root and changes nothing, and has the parts fix round 1 asked for (LauncherAsRoot runs it
    as root, where it must refuse: that class's users are a pool's)."""
    PROOF = os.path.join(HERE, 'fork-fleet-walls-proof.sh')

    def src(self):
        with open(self.PROOF) as f:
            return f.read()

    def test_it_parses_and_needs_root(self):
        self.assertEqual(subprocess.run(['bash', '-n', self.PROOF]).returncode, 0)
        if os.geteuid() != 0:
            got = subprocess.run(['bash', self.PROOF], capture_output=True, text=True, timeout=30)
            self.assertEqual((got.returncode, got.stdout), (1, ''))
            self.assertIn('run it as root', got.stderr)
        self.assertIn('"$launch" check-net https://github.com', self.src())

    def test_it_looks_for_every_part_of_a_pool_running_or_not(self):
        src = self.src()
        for part in ('/etc/fork-fleet*', '/opt/fork-fleet*', '/var/lib/fork-fleet*', '/run/fork-fleet*',
                     "list-unit-files --no-legend --plain \\\n  'fork-fleet*' 't3code-fleet*'",
                     "list-units --all --no-legend --plain 'fork-fleet*' 't3code-fleet*'",
                     "grep -E '^fork-(fleet|runner)-'", 'nft list table inet fork_fleet',
                     "ip netns list | grep -E '^fork-fleet-'", '^ffh-', "pgrep -af 'fork-fleet(\\.py|-launch\\.sh)'",
                     '/run/netns/fork-fleet-$u'):
            self.assertIn(part, src)
        self.assertLess(src.index('die "this host has a pool'), src.index('useradd --system'))

    def test_every_cleanup_step_runs_and_a_failed_one_is_named(self):
        src = self.src()
        cleanup = src[src.index('cleanup() {'):src.index('trap cleanup EXIT')]
        self.assertIn('set +e', cleanup)
        for undo in ('kill "$listener"', 'systemctl stop', 'ip netns del', 'ip link del', 'kill_all', 'userdel',
                     'nft delete table inet fork_fleet', 'iptables -w 60 -D DOCKER-USER', 'rm -rf', 'sysctl -qw'):
            self.assertRegex(cleanup, r'step [^\n]*' + re.escape(undo), undo)
        self.assertIn('cleanup steps that failed', cleanup)
        # step() runs the next step whatever this one gave, and names the failed one
        got = subprocess.run(['bash', '-c', src[src.index('step() {'):src.index('kill_all()')] +
                              'set -e; failed=(); step false one; step true; step false two; echo "${failed[*]}"'],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((got.returncode, got.stdout), (0, 'false one false two\n'))

    def test_cleanup_deletes_the_launchers_namespace_and_only_the_docker_rules_that_are_there(self):
        """Fix round 2 (#198 nits): cleanup deletes the namespace the launcher made for that user (its name and path),
        and a DOCKER-USER rule the launcher never added (a run that stopped early) is not a failed step (no exit 1)."""
        src = self.src()
        with open(os.path.join(HERE, 'fork-fleet-launch.sh')) as f:
            launch = f.read()
        self.assertIn('ns=fork-fleet-$user ', launch)
        self.assertIn('NetworkNamespacePath=/run/netns/fork-fleet-$user', launch)
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        bin_, rec = os.path.join(tmp, 'bin'), os.path.join(tmp, 'rec')
        os.makedirs(bin_)
        os.makedirs(os.path.join(tmp, 'netns'))
        open(os.path.join(tmp, 'netns', 'fork-fleet-u1'), 'w').close()
        # Fakes: each records its arguments; iptables -C finds only the rules listed in $T/present.
        for name in ('ip', 'iptables', 'systemctl', 'nft', 'pkill', 'userdel', 'sysctl', 'kill'):
            body = f'echo "{name} $*" >>"$T/rec"\n'
            if name == 'iptables':
                body += ('if [[ " $* " == *" -C "* ]]; then [[ ! -e $T/c-exit ]] || exit "$(<"$T/c-exit")"; '
                         'grep -qxF -- "${*: -1}" "$T/present"; exit; fi\n')
            if name in ('ip', 'nft'):
                body += '[[ " $* " != *" show "* && " $* " != *" list "* ]] || exit 1\n'
            with open(os.path.join(bin_, name), 'w') as f:
                f.write('#!/bin/bash\n' + body + 'exit 0\n')
            os.chmod(os.path.join(bin_, name), 0o755)
        with open(os.path.join(bin_, 'id'), 'w') as f:
            f.write('#!/bin/bash\necho 4242\n')
        os.chmod(os.path.join(bin_, 'id'), 0o755)
        code = src[src.index('step() {'):src.index('trap cleanup EXIT')].replace('/run/netns/', tmp + '/netns/')
        rules = ['-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT', '-i ffh-+ -j ACCEPT']
        for present in ([], ['ACCEPT']):
            with open(os.path.join(tmp, 'present'), 'w') as f:
                f.write('\n'.join(present) + '\n')
            open(rec, 'w').close()
            got = subprocess.run(['bash', '-c', code + '\nmade=(u1) docker=("$R1" "$R2") failed=() tmp="" listener="" '
                                  'forward=0\ncleanup\necho done'],
                                 env={'PATH': bin_ + ':/usr/bin:/bin', 'T': tmp, 'R1': rules[0], 'R2': rules[1]},
                                 capture_output=True, text=True, timeout=30)
            self.assertEqual((got.returncode, got.stdout, got.stderr), (0, 'done\n', ''))
            with open(rec) as f:
                calls = f.read().splitlines()
            self.assertIn('ip netns del fork-fleet-u1', calls)
            deleted = [c for c in calls if ' -D DOCKER-USER ' in c]
            self.assertEqual(deleted, [f'iptables -w 60 -D DOCKER-USER {r}' for r in rules] if present else [])
        # Fix round 3 (#198 re-check nit): a check that gives no answer (exit 4, the xtables lock) is neither "there"
        # nor "absent": a failed step, named, exit 1, and the rule is not deleted blind.
        with open(os.path.join(tmp, 'c-exit'), 'w') as f:
            f.write('4\n')
        open(rec, 'w').close()
        got = subprocess.run(['bash', '-c', code + '\nmade=() docker=("$R1") failed=() tmp="" listener="" forward=0\n'
                              'cleanup\necho done'],
                             env={'PATH': bin_ + ':/usr/bin:/bin', 'T': tmp, 'R1': rules[0]},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((got.returncode, got.stdout), (1, ''))
        self.assertIn(f'iptables -C DOCKER-USER {rules[0]} (exit 4: no answer; the rule may be left)', got.stderr)
        with open(rec) as f:
            self.assertNotIn(' -D DOCKER-USER ', f.read())

    def fakes(self, scripts):
        """A folder of fake commands (name: bash body), first on PATH; $T is the test's temp folder."""
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        os.makedirs(os.path.join(tmp, 'bin'))
        for name, body in scripts.items():
            with open(os.path.join(tmp, 'bin', name), 'w') as f:
                f.write('#!/bin/bash\n' + body)
            os.chmod(os.path.join(tmp, 'bin', name), 0o755)
        return tmp, {'PATH': os.path.join(tmp, 'bin') + ':/usr/bin:/bin', 'T': tmp}

    def test_it_owns_only_the_docker_rules_that_are_missing_and_stops_when_a_check_gives_no_answer(self):
        """Fix round 3 (#198 re-check nits): of Docker's two DOCKER-USER rules, the proof owns (cleanup deletes) only
        the ones missing when it starts; a rule there already is someone else's. A check that gives no answer (exit 4)
        stops the proof before it changes anything, not "missing"."""
        src = self.src()
        code = src[src.index('docker=() made=()'):src.index('step() {')]
        tmp, env = self.fakes({'iptables': 'case " $* " in *" -S "*) exit 0 ;; esac\n'
                                           '[[ ! -e $T/c-exit ]] || exit "$(<"$T/c-exit")"\n'
                                           'grep -qxF -- "${*:5}" "$T/present"\n'})
        rules = ['-o ffh-+ -m conntrack --ctstate RELATED,ESTABLISHED -j ACCEPT', '-i ffh-+ -j ACCEPT']
        run = lambda: subprocess.run(['bash', '-c', 'set -euo pipefail\ndie() { echo "DIE $*" >&2; exit 1; }\n' + code +
                                      '\nprintf "%s\\n" "${docker[@]}"'], env=env, capture_output=True, text=True,
                                     timeout=30)
        for present, owned in (([], rules), ([rules[0]], [rules[1]]), (rules, [])):
            with self.subTest(present=present):
                with open(os.path.join(tmp, 'present'), 'w') as f:
                    f.write(''.join(r + '\n' for r in present))
                got = run()
                self.assertEqual(got.returncode, 0, got.stderr)
                self.assertEqual([x for x in got.stdout.splitlines() if x], owned)
        with open(os.path.join(tmp, 'c-exit'), 'w') as f:
            f.write('4\n')
        got = run()
        self.assertEqual((got.returncode, got.stdout), (1, ''))
        self.assertIn('DIE iptables -C DOCKER-USER', got.stderr)
        self.assertIn('gave no answer (exit 4)', got.stderr)
        os.remove(os.path.join(tmp, 'c-exit'))
        # Fix round 4 (#208 correctness nit 3): the first check, whether Docker's chain is there, is held to the same
        # rule: 0 there, 1 (or no iptables) no Docker, anything else no answer, which stops the proof (else the
        # launcher could add the rules and the proof would not own them).
        tmp2, env2 = self.fakes({'iptables': 'case " $* " in *" -S "*) exit "$(<"$T/s-exit")" ;; esac\n'
                                             'grep -qxF -- "${*:5}" "$T/present"\n'})
        open(os.path.join(tmp2, 'present'), 'w').close()
        for s_exit, want_rc, owned in (('0', 0, rules), ('1', 0, []), ('4', 1, []), ('2', 1, [])):
            with self.subTest(s_exit=s_exit):
                with open(os.path.join(tmp2, 's-exit'), 'w') as f:
                    f.write(s_exit + '\n')
                got = subprocess.run(['bash', '-c', 'set -euo pipefail\ndie() { echo "DIE $*" >&2; exit 1; }\n' + code +
                                      '\nprintf "%s\\n" "${docker[@]}"'], env=env2, capture_output=True, text=True,
                                     timeout=30)
                self.assertEqual(got.returncode, want_rc, got.stderr)
                self.assertEqual([x for x in got.stdout.splitlines() if x], owned)
                if want_rc:
                    self.assertIn(f'DIE iptables -S DOCKER-USER gave no answer (exit {s_exit})', got.stderr)
        os.remove(os.path.join(tmp2, 'bin', 'iptables'))   # no iptables at all: no Docker
        got = subprocess.run([shutil.which('bash'), '-c', 'set -euo pipefail\ndie() { echo "DIE $*" >&2; exit 1; }\n' + code +
                              '\nprintf "%s\\n" "${docker[@]}"'], env={**env2, 'PATH': os.path.join(tmp2, 'bin')},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual((got.returncode, got.stdout.strip()), (0, ''), got.stderr)

    def test_each_block_alone_counts_only_when_its_target_answers_from_the_host(self):
        """Fix round 3 (#198 re-check nit): a listener that died makes every connection fail, so a "refused" from the
        namespace or the unit would prove nothing: each block-alone check first connects from this host, and when
        that fails it is a FAIL (not tried), never an ok."""
        src = self.src()
        tmp, env = self.fakes({
            # python3 on this host answers $T/host; in the runner's namespace (ip netns exec) and in the unit
            # (systemd-run), $T/inside.
            'python3': 'cat -- "$T/host"\n',
            'ip': '[[ $1 == netns && $2 == exec ]] || exit 2\ncat -- "$T/inside"\n',
            'systemd-run': 'cat -- "$T/inside"\n'})
        os.makedirs(os.path.join(tmp, 'netns'))
        open(os.path.join(tmp, 'netns', 'fork-fleet-u1'), 'w').close()
        code = src[src.index('# Each of the two network blocks'):].replace('/run/netns/', tmp + '/netns/')
        for name, host, inside, want_rc, want in (
                ('both blocks hold', 'open', 'ConnectionRefusedError', 0,
                 ['ok    the nft table alone refuses', 'ok    IPAddressDeny alone refuses', 'PASSED']),
                ('the listener died', 'ConnectionRefusedError', 'ConnectionRefusedError', 1,
                 ['FAIL  the nft table alone: not tried (192.0.2.7:4321 does not answer from this host either',
                  'FAIL  IPAddressDeny alone: not tried (127.0.0.1:4321 does not answer', 'FAILED (exit 1)']),
                ('neither block holds', 'open', 'open', 1,
                 ['FAIL  the nft table alone lets', 'FAIL  IPAddressDeny does not apply here', 'FAILED (exit 1)'])):
            with self.subTest(name):
                for f, text in (('host', host), ('inside', inside)):
                    with open(os.path.join(tmp, f), 'w') as fh:
                        fh.write(text + '\n')
                got = subprocess.run(['bash', '-c', 'set -euo pipefail\nusers=(u1) own=192.0.2.7 port=4321 '
                                      'cg=cgroup2fs rc=0\n' + code], env=env, capture_output=True, text=True,
                                     timeout=30)
                self.assertEqual(got.returncode, want_rc, got.stdout + got.stderr)
                for line in want:
                    self.assertIn(line, got.stdout)
                if want_rc:
                    self.assertNotIn('ok    ', got.stdout)

    def test_its_messages_reach_the_terminal_as_printable_text(self):
        """Fix round 3 (#191 re-check S1, the proof too): die names other processes' command lines (a pool's part):
        printable ASCII only, at most 2000 bytes."""
        src = self.src()
        got = subprocess.run(['bash', '-c', src[src.index('die() {'):src.index('((EUID == 0))')] + 'die "$X"'],
                             env={'PATH': '/usr/bin:/bin', 'X': 'process 7 fork-fleet.py \x1b]52;c;eA==\x07\u202e' + 'y' * 3000},
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(got.returncode, 1)
        self.assertIn('fork-fleet-walls-proof: process 7 fork-fleet.py ?]52;c;eA==??', got.stderr)
        self.assertLessEqual(len(got.stderr), 2000 + len('fork-fleet-walls-proof: \n'))

    def test_it_prints_the_cgroup_version_and_tries_each_block_alone(self):
        src = self.src()
        self.assertIn('stat -fc %T /sys/fs/cgroup', src)
        self.assertIn('ip netns exec "fork-fleet-${users[0]}"', src)
        self.assertIn('-p IPAddressDeny=any', src)
        for key in ('JOB_PATHS=$tmp/job-paths.txt', 'HOOK=$tmp/hook.sh'):
            self.assertIn(key, src)


@unittest.skipUnless(sys.platform.startswith('linux') and os.geteuid() == 0 and
                     os.environ.get('FORK_FLEET_ROOT_TESTS') == '1',
                     'as root with FORK_FLEET_ROOT_TESTS=1 only: it creates and deletes users (CI: the workflow lint '
                     'job; elsewhere: a throwaway container)')
class LauncherAsRoot(unittest.TestCase):
    """fork-fleet-launch.sh as root with two real runner users and the real chown, pgrep, setpriv and find (systemd-run
    and systemctl stay fakes: no unit really starts; ip, nft and iptables are the fakes too, but for the network test,
    which makes real namespaces). Item 1 of round 2: a slot's user cannot read or change another slot's copy, HOME or
    start config, and nothing a job leaves (files in HOME, a running process) reaches the next job of that user.
    Round 3: root deletes nothing in /dev/shm (the swap race), and check-net proves a runner's network for real."""

    USERS = ('fork-fleet-t1', 'fork-fleet-t2')
    NAME = Launcher.NAME
    INPUT = Launcher.INPUT

    @classmethod
    def setUpClass(cls):
        for user in cls.USERS:
            try:
                pwd.getpwnam(user)
                raise AssertionError(f'user {user} exists already: this test only runs where it may make its own')
            except KeyError:
                pass
        for user in cls.USERS:
            subprocess.run(['useradd', '--no-create-home', '--user-group', '--shell', '/usr/sbin/nologin', user],
                           check=True)
            cls.addClassCleanup(subprocess.run, ['userdel', user], check=False)
            cls.addClassCleanup(subprocess.run, ['pkill', '-KILL', '-u', user], check=False)   # runs before userdel

    def setUp(self):
        self.d = launcher_tmp()
        self.addCleanup(shutil.rmtree, self.d, True)
        os.chmod(self.d, 0o711)   # the runner users pass through to their copies, as through /var/lib
        self.rec = self.path('rec')
        for d in ('rec', 'bin', 'template/bin', 'linger'):
            os.makedirs(self.path(d))
        os.chmod(self.rec, 0o700)
        for name in ('systemd-run', 'systemctl', 'ip', 'nft', 'iptables', 'curl'):
            Launcher.write(self, f'bin/{name}', Launcher.FAKES[name].replace('$REC', self.rec), 0o755)
        for x in 'ab':
            Launcher.write(self, f'rec/own-{x}', f'{Launcher.OWN}\n', 0o644)
        self.template = self.path('template')
        Launcher.write(self, 'template/run.sh', '#!/bin/sh\n', 0o755)
        Launcher.write(self, 'template/.fork-fleet-version', f'{NEWEST_RUNNER}\n', 0o644)
        for d in ('template', 'template/bin'):
            os.chmod(self.path(d), 0o755)
        self.hook = Launcher.write(self, 'hook.sh', '#!/bin/sh\n', 0o755)
        Launcher.write(self, 'meminfo', 'MemAvailable:    8388608 kB\n', 0o644)
        Launcher.write(self, 'resolv.conf', 'nameserver 1.1.1.1\n', 0o644)
        Launcher.write(self, 'ip_forward', '1\n', 0o644)
        with open(os.path.join(os.path.dirname(LAUNCH), 'fork-fleet-job-paths.txt')) as f:
            self.job_paths = Launcher.write(self, 'job-paths.txt', f.read(), 0o644)   # root's, as installed
        self.conf()

    path = Launcher.path

    def conf(self, **over):
        settings = dict(RUNNER_USERS=','.join(self.USERS), TEMPLATE=self.template, WORK_ROOT=self.path('var/runners'),
                        RUN_DIR=self.path('run'), HOOK=self.hook, LINGER_DIR=self.path('linger'),
                        MEMINFO=self.path('meminfo'), RESOLV_CONF=self.path('resolv.conf'),
                        IP_FORWARD=self.path('ip_forward'), SYSTEMD_RUN=self.path('bin/systemd-run'),
                        SYSTEMCTL=self.path('bin/systemctl'), OWN_ADDRESS_URLS=Launcher.OWN_URLS,
                        JOB_PATHS=self.job_paths)
        settings.update(over)
        Launcher.write(self, 'launch.conf', ''.join(f'{k}={v}\n' for k, v in settings.items() if v), 0o600)

    def launch(self, *args, stdin='', path=None):
        return subprocess.run(['bash', LAUNCH, *args], input=stdin, capture_output=True, text=True, timeout=300,
                              env={'PATH': f'{path or self.path("bin")}:/usr/sbin:/usr/bin:/sbin:/bin',
                                   'FORK_FLEET_LAUNCH_CONF': self.path('launch.conf')})

    def start(self, name):
        got = self.launch('start', name, stdin=self.INPUT)
        self.assertEqual(got.returncode, 0, got.stderr)
        with open(os.path.join(self.rec, 'systemd-run')) as f:
            args = f.read().splitlines()
        return args[args.index('--uid') + 1], self.path(f'var/runners/{name}')

    def as_user(self, user, *cmd, **kw):
        pw = pwd.getpwnam(user)
        return subprocess.run(['setpriv', f'--reuid={pw.pw_uid}', f'--regid={pw.pw_gid}', '--clear-groups', '--',
                               *cmd], capture_output=True, text=True, timeout=30, **kw)

    def test_one_slots_user_cannot_read_or_change_the_others(self):
        (u1, a), (u2, b) = self.start('fleet-vm-t3code-11'), self.start('fleet-vm-t3code-12')
        self.assertEqual((u1, u2), self.USERS)
        for user, copy_ in ((u1, a), (u2, b)):   # each writes its own copy, HOME and TMPDIR
            for rel in ('.runner', 'home/.vite-plus-cache', 'tmp/x'):
                self.assertEqual(self.as_user(user, 'sh', '-c', f'echo secret > {copy_}/{rel}').returncode, 0)
        for rel in ('.runner', 'home/.vite-plus-cache', 'tmp/x'):
            with self.subTest(rel):
                self.assertNotEqual(self.as_user(u2, 'cat', f'{a}/{rel}').returncode, 0)
                self.assertNotEqual(self.as_user(u2, 'sh', '-c', f'echo evil >> {a}/{rel}').returncode, 0)
        for target in (a, f'{a}/home', self.path('run'), self.path('var/runners')):
            with self.subTest(target):
                self.assertNotEqual(self.as_user(u2, 'ls', target).returncode, 0)
        for target in (f'{a}/run.sh', f'{b}/run.sh', f'{self.template}/run.sh', self.hook):
            with self.subTest(target):   # the runner itself and the hook: root's, read-only to both
                self.assertNotEqual(self.as_user(u2, 'sh', '-c', f'echo evil >> {target}').returncode, 0)
        self.assertNotEqual(self.as_user(u2, 'mkdir', f'{a}/../x').returncode, 0)
        with open(f'{a}/home/.vite-plus-cache') as f:
            self.assertEqual(f.read(), 'secret\n')

    def test_nothing_a_job_leaves_reaches_the_next_job_of_its_user(self):
        user, a = self.start(self.NAME)
        self.assertEqual(user, self.USERS[0])
        other_user, b = self.start('fleet-vm-t3code-12')   # the other slot: running, must keep its files
        plant = (f'mkdir -p {a}/home/.vite-plus && echo x > {a}/home/.vite-plus/planted && '
                 f'ln -s /etc/passwd {a}/home/link && echo x > {a}/tmp/planted')
        self.assertEqual(self.as_user(user, 'sh', '-c', plant).returncode, 0)
        pw = pwd.getpwnam(user)
        left = subprocess.Popen(['setpriv', f'--reuid={pw.pw_uid}', f'--regid={pw.pw_gid}', '--clear-groups', '--',
                                 'sh', '-c', 'sleep 300 & exec sleep 300'], start_new_session=True,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(left.kill)
        reaper = __import__('threading').Thread(target=left.wait, daemon=True)   # as init would: no zombie left
        reaper.start()
        # The first job's unit ends (the other slot's goes on); the next job gets the free user.
        with open(os.path.join(self.rec, 'units'), 'w') as f:
            f.write('fork-fleet-fleet-vm-t3code-12.service loaded active running x\n')
        again, c = self.start('fleet-vm-t3code-13')
        self.assertEqual(again, user)
        reaper.join(10)
        self.assertEqual(left.returncode, -signal.SIGKILL)
        self.assertEqual(subprocess.run(['pgrep', '-u', str(pw.pw_uid)]).returncode, 1)   # the background one too
        self.assertFalse(os.path.exists(a))   # with its HOME and TMPDIR
        self.assertEqual(os.listdir(f'{c}/home') + os.listdir(f'{c}/tmp'), [])
        self.assertTrue(os.path.isdir(b))
        self.assertEqual(os.stat('/etc/passwd').st_size > 0, True)   # the link went, not what it pointed at
        # /tmp, /var/tmp, /dev/shm and /run: the unit's own memory file systems (systemd's, not tested here), gone
        # with it; the launcher never deletes in a shared one (test_root_deletes_nothing_in_dev_shm).

    def as_user_bg(self, user, *cmd):
        pw = pwd.getpwnam(user)
        proc = subprocess.Popen(['setpriv', f'--reuid={pw.pw_uid}', f'--regid={pw.pw_gid}', '--clear-groups', '--',
                                 *cmd], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def stop():
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
        self.addCleanup(stop)
        return stop

    def test_root_deletes_nothing_in_dev_shm(self):
        """The swap race (round 3, S-2), in the real /dev/shm: runner user B owns a directory there and lets user A's
        job leave a file in it; while root readies A for its next job, B swaps that directory for a symlink to one
        under /etc, again and again. Root must never delete through it: the launcher deletes nothing in /dev/shm (each
        unit has a /dev/shm of its own, gone with it), so the file under /etc stays, every round."""
        a, b = self.USERS
        pb = pwd.getpwnam(b)
        victim_dir = tempfile.mkdtemp(prefix='fork-fleet-victim-', dir='/etc')
        self.addCleanup(shutil.rmtree, victim_dir, True)
        os.chmod(victim_dir, 0o755)
        victim = os.path.join(victim_dir, 'planted')
        bdir = tempfile.mkdtemp(prefix='fork-fleet-race-', dir='/dev/shm')
        self.addCleanup(shutil.rmtree, bdir, True)
        os.chown(bdir, pb.pw_uid, pb.pw_gid)
        os.chmod(bdir, 0o755)
        sub = os.path.join(bdir, 'sub')
        # B's swap: sub a directory (with A's file), then a symlink to the directory under /etc, and back, forever
        # (one process per step: each state lasts about a millisecond, long enough for a find and an rm to see it).
        racer = f'cd {bdir} && while :; do mv -T sub real; mv -T link sub; mv -T sub link; mv -T real sub; done'
        failed = []
        for round_ in range(40):
            with open(victim, 'w') as f:
                f.write('root\'s\n')
            self.assertEqual(self.as_user(b, 'sh', '-c', f'cd {bdir} && rm -rf sub real link && mkdir -m 777 sub && '
                                                          f'ln -s {victim_dir} link').returncode, 0)
            self.assertEqual(self.as_user(a, 'sh', '-c', f'echo x > {sub}/planted').returncode, 0)   # A's job
            stop = self.as_user_bg(b, 'sh', '-c', racer)
            with open(os.path.join(self.rec, 'units'), 'w') as f:
                f.write('')   # A's last unit ended: its user is free
            got = self.launch('start', f'fleet-vm-t3code-{100 + round_}', stdin=self.INPUT)
            stop()
            self.assertTrue(os.path.exists(victim), f'round {round_}: root deleted {victim} through /dev/shm')
            if got.returncode:
                failed.append(f'round {round_}: {got.stderr.strip()}')
        self.assertEqual(failed, [])

    def test_check_nets_probe_names_drives_and_sockets(self):
        """Fix round 2 (#191 correctness re-check minor 1): the probe's evidence for WSL's drives (/mnt/c) and WSLg's
        sockets, in a mount namespace of its own (empty memory file systems on the places it looks): an entry in /mnt
        and a socket in /run fail by name; without them both lines say ok."""
        script = ('for d in /tmp /var/tmp /run /dev/shm /mnt /media; do mount -t tmpfs fork-fleet-test "$d" || exit 9; '
                  'done; [ "$1" = clean ] || { : > /mnt/drive; /usr/bin/python3 -c "import socket; '
                  'socket.socket(socket.AF_UNIX).bind(\'/run/s\')"; }; exec /usr/bin/python3 -I -c "$0" job / ""')

        def probe(mode):
            got = subprocess.run(['unshare', '-m', '--propagation', 'private', 'sh', '-c', script, walls_probe(), mode],
                                 capture_output=True, text=True, timeout=120)
            self.assertNotEqual(got.returncode, 9, got.stderr)
            return got.stdout
        out = probe('dirty')
        self.assertIn('FAIL  /mnt and /media: hold /mnt/drive inside\n', out)
        self.assertIn('FAIL  sockets: /run/s inside\n', out)
        out = probe('clean')
        self.assertIn('ok    /mnt and /media: nothing inside\n', out)
        self.assertIn('ok    sockets: none in /tmp, /var/tmp, /run, /dev/shm\n', out)

    def test_check_net_proves_a_runners_network(self):
        """check-net with real namespaces and the real nft table (CI installs nftables): from inside a runner's
        namespace, as its user, the host's loopback service, the host's own LAN, Tailscale-like and public-looking
        addresses, a LAN host and a Tailscale-like resolver are closed, and a public web server answers. systemd-run is
        a fake that enters the namespace and becomes the runner user: the unit's IPAddressDeny (the same ranges, the
        kernel's second refusal) is not part of this test."""
        for tool in ('nft', 'ip', 'nsenter', 'curl', 'timeout'):
            self.assertIsNotNone(shutil.which(tool), f'no {tool} here')
        forward = '/proc/sys/net/ipv4/ip_forward'
        with open(forward) as f:
            if f.read().strip() != '1':
                with open(forward, 'w') as w:   # a throwaway machine (CI) or container only
                    w.write('1\n')

        def ip(*args):
            subprocess.run(['ip', *args], check=True, capture_output=True, text=True)

        def later(*cmd):
            self.addCleanup(subprocess.run, list(cmd), capture_output=True, check=False)
        later('nft', 'delete', 'table', 'inet', 'fork_fleet')
        for user in self.USERS:
            later('ip', 'netns', 'del', f'fork-fleet-{user}')
        # A LAN (192.168.77.0/24) with a host .2 that also has a Tailscale-like resolver 100.100.100.100, a "public"
        # network 203.0.113.0/24 with a web server .2, and this host's own public-looking and Tailscale-like addresses.
        for ns, link, mine, theirs in (('fft-lan', 'fft-lan0', '192.168.77.1', '192.168.77.2'),
                                       ('fft-wan', 'fft-wan0', '203.0.113.1', '203.0.113.2')):
            later('ip', 'netns', 'del', ns)
            ip('netns', 'add', ns)
            ip('link', 'add', link, 'type', 'veth', 'peer', 'name', 'eth0', 'netns', ns)
            ip('addr', 'add', f'{mine}/24', 'dev', link)
            ip('link', 'set', link, 'up')
            ip('-n', ns, 'addr', 'add', f'{theirs}/24', 'dev', 'eth0')
            ip('-n', ns, 'link', 'set', 'eth0', 'up')
            ip('-n', ns, 'link', 'set', 'lo', 'up')
        ip('-n', 'fft-lan', 'addr', 'add', '100.100.100.100/32', 'dev', 'lo')
        ip('-n', 'fft-wan', 'addr', 'add', '203.0.113.3/24', 'dev', 'eth0')   # this host's own public address (D1)
        ip('route', 'add', '100.100.100.100/32', 'via', '192.168.77.2')
        later('ip', 'link', 'del', 'fft-dummy')
        ip('link', 'add', 'fft-dummy', 'type', 'dummy')
        for addr in ('198.51.100.7/32', '100.64.7.1/32'):
            ip('addr', 'add', addr, 'dev', 'fft-dummy')
        ip('link', 'set', 'fft-dummy', 'up')
        import socket
        for addr in (('127.0.0.1', 8384), ('0.0.0.0', 3773)):   # a GUI-like one on loopback, a T3-like one on every address
            srv = socket.create_server(addr)
            self.addCleanup(srv.close)
        web = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, web, True)
        for cmd in (['nsenter', '--net=/run/netns/fft-lan', 'python3', '-I', '-c',
                     'import socket, time\nss = [socket.create_server(("0.0.0.0", p)) for p in (22, 53)]\n'
                     'time.sleep(600)'],
                    ['nsenter', '--net=/run/netns/fft-wan', 'python3', '-I', '-m', 'http.server', '80', '--bind',
                     '203.0.113.2', '--directory', web],
                    # a port forwarded back into the home, on its own public address: the walls must refuse it
                    ['nsenter', '--net=/run/netns/fft-wan', 'python3', '-I', '-c',
                     'import socket, time\ns = socket.create_server(("203.0.113.3", 443))\ntime.sleep(600)']):
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.addCleanup(proc.wait)
            self.addCleanup(proc.kill)
        import time
        for host, port in (('192.168.77.2', 22), ('100.100.100.100', 53), ('203.0.113.2', 80), ('203.0.113.3', 443)):
            for _ in range(100):
                try:
                    socket.create_connection((host, port), 1).close()
                    break
                except OSError:
                    time.sleep(0.1)
            else:
                self.fail(f'{host}:{port} never listened')
        os.makedirs(self.path('netbin'))
        Launcher.write(self, 'netbin/systemd-run', NETNS_SYSTEMD_RUN, 0o755)
        Launcher.write(self, 'netbin/curl', Launcher.FAKES['curl'].replace('$REC', self.rec), 0o755)
        for x in 'ab':
            Launcher.write(self, f'rec/own-{x}', '203.0.113.3\n', 0o644)
        self.conf(SYSTEMD_RUN=self.path('netbin/systemd-run'), IP_FORWARD=forward)
        targets = ('127.0.0.1:8384', '192.168.77.1:3773', '198.51.100.7:3773', '100.64.7.1:3773', '192.168.77.2:22',
                   '100.100.100.100:53')
        got = self.launch('check-net', 'http://203.0.113.2/', *targets, path=self.path('netbin'))
        self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
        for target in targets:
            self.assertIn(f'ok    {target} is closed', got.stdout)
        self.assertIn('ok    http://203.0.113.2/ answers (HTTP 200)', got.stdout)
        self.assertIn("ok    this host's own public address 203.0.113.3 is refused (No route to host)", got.stdout)
        self.assertIn(f'check-net: as {self.USERS[0]}, in its namespace fork-fleet-{self.USERS[0]}', got.stdout)

    def test_the_walls_proof_refuses_a_host_with_a_pools_users(self):
        """As root, with this class's runner users (a pool's part, as an idle pool's after a reboot): it refuses
        before it makes anything, and names them."""
        users = {p.pw_name for p in pwd.getpwall()}
        got = subprocess.run(['bash', WallsProof.PROOF], capture_output=True, text=True, timeout=60)
        self.assertEqual((got.returncode, got.stdout), (1, ''), got.stderr)
        for user in self.USERS:
            self.assertIn(f'user {user}', got.stderr)
        self.assertEqual({p.pw_name for p in pwd.getpwall()}, users)

    def test_the_walls_hold_in_a_real_systemd_unit(self):
        """As Launcher's, in a unit of the system's systemd as a real runner user (CI: the hosted runner's), and with
        all of the sandbox but its network: only the allowed mounts, the pseudo devices, no socket of the host's, no
        later mount, git, node, java and docker work, and a drive outside /mnt and /media (before or after the job
        started), the host's /run and a share mounted later under an allowed path stay hidden (hidden_from_job)."""
        late = tempfile.mkdtemp(prefix='fork-fleet-allowed-', dir='/var/lib')   # ALLOW_PATHS: no /tmp path is
        self.addCleanup(shutil.rmtree, late, True)
        os.chmod(late, 0o755)
        self.conf(ALLOW_PATHS=late)
        import http.server, threading
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), http.server.SimpleHTTPRequestHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        Launcher.write(self, 'rec/probe-exit', '0', 0o644)
        got = self.launch('check-net', 'https://example.com/', f'127.0.0.1:{srv.server_address[1]}')
        self.assertEqual(got.returncode, 0, got.stderr)
        real_walls(self, Launcher.waited(self, 'fork-fleet-check-walls'), ['systemd-run'], '--uid', self.USERS[0],
                   full=True)


    def test_the_real_job_unit_writes_its_copy_and_nothing_else(self):
        """Issue #204 item 1: start's own unit, in the system's systemd (CI: the hosted runner's) as a real runner
        user, with every property start gives it but its network (a namespace: test_check_net_proves_a_runners_network):
        a trivial job writes its copy, HOME, TMPDIR and its own memory file systems; not the template's files its copy
        links, nor anything else (every other mount it sees is read-only). Issue #217: the unit has the launcher's own
        stop timeouts, so systemd's defaults decide nothing for it, and `stop` ends it. The red check: without
        ProtectSystem=strict its root is writable."""
        if subprocess.run(['systemd-run', '--wait', '--quiet', '--collect', '--', '/bin/true'], capture_output=True,
                          timeout=60).returncode != 0:
            self.skipTest('no system systemd to run a unit with')
        Launcher.write(self, 'bin/systemd-run', REAL_SYSTEMD_RUN.replace('$REC', self.rec), 0o755)
        self.conf(SYSTEMCTL=shutil.which('systemctl', path='/usr/bin:/bin'))
        Launcher.write(self, 'template/run.sh', JOB_WRITES, 0o755)

        def run(name, drop=''):
            Launcher.write(self, 'rec/drop', drop, 0o644)
            self.addCleanup(subprocess.run, ['systemctl', 'stop', f'fork-fleet-{name}.service'], capture_output=True)
            got = self.launch('start', name, stdin=self.INPUT)
            self.assertEqual(got.returncode, 0, got.stderr)
            dest = os.path.realpath(self.path(f'var/runners/{name}'))
            said = subprocess.run(['cat', '--', f'{dest}/out'], capture_output=True, text=True, timeout=120).stdout
            return dest, said.splitlines()
        name = 'fleet-vm-t3code-31'
        dest, said = run(name)
        own = [dest + '/written', dest + '/home/written', dest + '/tmp/written', '/tmp/written', '/var/tmp/written',
               '/dev/shm/written']
        self.assertEqual([x for x in said if x.startswith('wrote ')], [f'wrote {p}' for p in own], said)
        self.assertEqual([x for x in said if x.startswith('refused ')],
                         [f'refused {p}' for p in (dest + '/run.sh', os.path.dirname(dest) + '/written', '/written',
                                                   '/etc/written', '/usr/written', '/var/lib/written')], said)
        rw = {x[3:] for x in said if x.startswith('rw ')}
        self.assertIn(dest, rw)
        own_fs = {dest, '/tmp', '/var/tmp', '/run'}   # its copy and its own memory file systems; and the kernel's
        self.assertEqual({m for m in rw - own_fs if not re.match(r'/(proc|sys|dev)(/|$)', m)}, set(), said)
        with open(f'{dest}/written') as f:
            self.assertEqual(f.read(), 'x\n')
        with open(f'{self.template}/run.sh') as f:
            self.assertEqual(f.read(), JOB_WRITES)
        unit = f'fork-fleet-{name}.service'
        shown = subprocess.run(['systemctl', 'show', '-p', 'TimeoutStopUSec', '-p', 'TimeoutAbortUSec', unit],
                               capture_output=True, text=True, timeout=60).stdout.splitlines()
        self.assertEqual(sorted(shown), ['TimeoutAbortUSec=1min 30s', 'TimeoutStopUSec=1min 30s'])
        import importlib.machinery
        loader = importlib.machinery.SourceFileLoader('walls_linux', os.path.join(HERE, 'fork-fleet-walls', 'linux'))
        walls = importlib.util.module_from_spec(importlib.util.spec_from_loader(loader.name, loader))
        loader.exec_module(walls)
        self.assertEqual(walls.stop_defaults_used(), set())   # the walls read systemd's own file for the unit
        got = self.launch('stop', name)
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertNotEqual(subprocess.run(['systemctl', 'is-active', '--quiet', unit], timeout=60).returncode, 0)
        _, said = run('fleet-vm-t3code-32', drop='ProtectSystem=strict')
        self.assertIn('rw /', said)
        self.launch('stop', 'fleet-vm-t3code-32')


# LauncherAsRoot's systemd-run for the real job unit: the real one, with start's arguments but its network and the
# property in $REC/drop, and a FIFO in the copy that the job tells the test through.
REAL_SYSTEMD_RUN = """#!/usr/bin/env bash
drop=$(cat -- "$REC/drop") dir='' args=()
while (($#)); do
  if [[ $1 == -p ]]; then
    case $2 in NetworkNamespacePath=* | IPAddressDeny=* | IPAddressAllow=* | "$drop") ;; *) args+=(-p "$2") ;; esac
    shift 2
    continue
  fi
  [[ $1 != --working-directory ]] || dir=$2
  args+=("$1")
  shift
done
mkfifo -m 0600 -- "$dir/out" && chown --reference="$dir" -- "$dir/out" || exit 99
exec /usr/bin/systemd-run "${args[@]}"
"""
# The trivial job: tries to write each path, says which mounts it sees read-write, then waits for its stop.
JOB_WRITES = """#!/bin/bash
exec >"$PWD/out" 2>&1
for p in "$PWD/written" "$HOME/written" "$TMPDIR/written" /tmp/written /var/tmp/written /dev/shm/written \
  "$PWD/run.sh" "${PWD%/*}/written" /written /etc/written /usr/written /var/lib/written; do
  if (echo x >"$p") 2>/dev/null; then echo "wrote $p"; else echo "refused $p"; fi
done
while read -r _ _ _ _ point opts _; do [[ ,$opts, != *,rw,* ]] || echo "rw $point"; done </proc/self/mountinfo
exec >/dev/null 2>&1
exec sleep 600
"""

class Wiring(unittest.TestCase):
    """fork-tests.yml, fork-fleet.yml, fork-release.yml and the hook against the config."""

    @classmethod
    def setUpClass(cls):
        import yaml

        def read(rel):
            with open(os.path.join(ROOT, rel)) as f:
                return f.read()
        cls.text = read(WORKFLOW)
        cls.tests = yaml.safe_load(cls.text)
        cls.sweep = yaml.safe_load(read('.github/workflows/fork-fleet.yml'))
        cls.follow = yaml.safe_load(read('.github/workflows/fork-follow.yml'))
        cls.checks = yaml.safe_load(read('.github/workflows/fork-checks.yml'))
        cls.release = yaml.safe_load(read('.github/workflows/fork-release.yml'))
        cls.hook = read('.github/scripts/fork-fleet-hook.sh')

    def expected_runs_on(self, j):
        """route's labels for the job, its run label with this attempt added (-a<attempt>): a runner made for one
        attempt can never be given a job of another (a re-run after a cancel would be refused by the gate)."""
        v, out = CFG['variable'], 'needs.changes.outputs'
        key = f"format('{j['job']}-{{0}}', matrix.shard)" if j.get('shards') else f"'{j['job']}'"
        r = f"fromJSON({out}.runners || '{{}}')[{key}]"
        return (f"${{{{ vars.{v} != 'github' && ({out}.fleet_attempt == github.run_attempt || "
                f"({out}.fleet_mode == 'fleet' && vars.{v} == 'fleet')) && "
                f"{r}[1] && fromJSON(format('[\"{{0}}\",\"{{1}}-a{{2}}\"]', {r}[0], {r}[1], github.run_attempt)) "
                f"|| '{HOSTED}' }}}}")

    def test_every_routed_job_uses_the_route(self):
        jobs = self.tests['jobs']
        for j in CFG['jobs']:
            with self.subTest(j['job']):
                spec = jobs[j['job']]
                self.assertEqual(spec['runs-on'], self.expected_runs_on(j))
                self.assertEqual(spec['needs'], 'changes')
                shards = (spec.get('strategy') or {}).get('matrix', {}).get('shard')
                self.assertEqual(len(shards) if shards else None, j.get('shards'))

    def test_test_web_collects_chromium_garbage_after_every_file_and_checks_vitest_reads_it(self):
        """Fork issue #245: without the setting, Test Web's Chromium pages grow until the browser connection closes.
        Its name must be the one fork-vitest-gc-check.cjs looks for in the installed vitest, checked before Test."""
        with open(os.path.join(ROOT, '.github/scripts/fork-vitest-gc-check.cjs')) as f:
            name = re.search(r'^const VARIABLE = "(\w+)";$', f.read(), re.M).group(1)
        steps = self.tests['jobs']['test_web']['steps']
        names = [s.get('name') for s in steps]
        test = steps[names.index('Test')]
        self.assertGreaterEqual(float(test.get('env', {}).get(name, 0)), 1e6,
                                f'{name}: a threshold no disk reaches, so vitest collects after every file')
        check = [i for i, s in enumerate(steps) if 'fork-vitest-gc-check.cjs' in (s.get('run') or '')]
        self.assertEqual(len(check), 1, 'one step runs fork-vitest-gc-check.cjs')
        self.assertLess(check[0], names.index('Test'))
        self.assertEqual(steps[check[0]].get('working-directory'), test.get('working-directory'))

    @staticmethod
    def evaluate(expr, variables, outputs, attempt, shard=None):
        """A runs-on expression in Python: GitHub's && and || return an operand, as Python's and/or do."""
        body = expr.strip()[3:-2]
        for a, b in ((r'\bvars\.(\w+)', r'V.get("\1", "")'), (r'\bneeds\.changes\.outputs\.(\w+)', r'O.get("\1", "")'),
                     (r'\bgithub\.run_attempt\b', 'A'), (r'\bmatrix\.shard\b', 'S'), (r'\bfromJSON\(', 'J('),
                     (r'\bformat\(', 'F('), ('&&', ' and '), (r'\|\|', ' or ')):
            body = re.sub(a, b, body)

        class Null:
            """GitHub's null: falsy, and indexing it (or anything but an object or array) gives null again."""
            def __getitem__(self, key):
                return self

            def __bool__(self):
                return False
        null = Null()

        class Obj(dict):
            def __missing__(self, key):
                return null

        class Arr(list):
            def __getitem__(self, i):
                return list.__getitem__(self, i) if isinstance(i, int) and 0 <= i < len(self) else null

        class Str(str):
            def __getitem__(self, i):
                return null

        def wrap(v):
            if isinstance(v, dict):
                return Obj({k: wrap(x) for k, x in v.items()})
            if isinstance(v, list):
                return Arr(wrap(x) for x in v)
            return Str(v) if isinstance(v, str) else null if v is None else v
        text = lambda a: '' if a is null else str(a)   # format(): null is ''
        got = eval(body, {'__builtins__': {}}, {'V': variables, 'O': outputs, 'A': attempt, 'S': shard,   # noqa: S307
                                                'J': lambda s: wrap(json.loads(s)),
                                                'F': lambda f, *a: f.format(*map(text, a))})
        return json.loads(json.dumps(got))

    def test_runs_on_follows_the_setting_on_a_re_run(self):
        """s2 minor 1: a re-run of failed jobs keeps `changes`' outputs from the first attempt."""
        lint = self.tests['jobs']['lint']['runs-on']
        routed = lambda mode, attempt='1': {'runners': json.dumps({'lint': labels(WAITING, 'lint')}), 'fleet_mode': mode,
                                            'fleet_attempt': attempt}
        cases = {   # name: (vars, changes outputs, run attempt, runs on the fleet)
            'the attempt route ran in': ({}, routed('auto'), '1', True),
            'the setting is github now': ({'CI_RUNNERS': 'github'}, routed('auto'), '1', False),
            'auto, a re-run of failed jobs': ({}, routed('auto'), '2', False),
            'auto, a re-run of all jobs (route ran again)': ({}, routed('auto', '2'), '2', True),
            'fleet, a re-run while still fleet': ({'CI_RUNNERS': 'fleet'}, routed('fleet'), '2', True),
            'fleet, a re-run after a switch to auto': ({'CI_RUNNERS': 'auto'}, routed('fleet'), '2', False),
            'fleet, a re-run after a switch to github': ({'CI_RUNNERS': 'github'}, routed('fleet'), '2', False),
            'fleet, a re-run after the setting was removed': ({}, routed('fleet'), '2', False),
            'a release run (no route)': ({}, {}, '1', False),
            'route sent the job nowhere': ({}, dict(routed('auto'), runners='{}'), '1', False),
            'route made the job GitHub-hosted': ({}, dict(routed('auto'), runners=json.dumps({'lint': HOSTED})), '1',
                                                  False),
        }
        for name, (variables, outputs, attempt, on_fleet) in cases.items():
            with self.subTest(name):
                got = self.evaluate(lint, variables, outputs, attempt)
                self.assertEqual(got, job_labels(WAITING, 'lint', attempt=int(attempt)) if on_fleet else HOSTED)
                if on_fleet:   # what the dispatcher reads back: this attempt's job, and no other attempt's
                    self.assertEqual(ff.fleet_job(CFG, got), (POOLS[WAITING], 'lint', int(attempt)))
        test = self.tests['jobs']['test']['runs-on']
        out = {'runners': json.dumps({'test-2': labels(WAITING, 'test-2'), 'test-1': HOSTED}), 'fleet_mode': 'auto',
               'fleet_attempt': '1'}
        self.assertEqual(self.evaluate(test, {}, out, '1', shard=2), job_labels(WAITING, 'test-2'))
        self.assertEqual(self.evaluate(test, {}, out, '1', shard=1), HOSTED)
        self.assertEqual(self.evaluate(test, {}, out, '1', shard=3), HOSTED)

    def test_no_other_job_can_reach_the_fleet(self):
        routed = {j['job'] for j in CFG['jobs']}
        for name, spec in self.tests['jobs'].items():
            if name in routed:
                continue
            with self.subTest(name):
                if 'uses' in spec:   # a call of a workflow in this repository (the secret scan): its jobs must stay off the fleet too
                    self.assertTrue(spec['uses'].startswith('./.github/workflows/'), spec['uses'])
                    import yaml
                    with open(os.path.join(ROOT, spec['uses'][2:])) as f:
                        called = yaml.safe_load(f)['jobs']
                    self.assertTrue(called, spec['uses'])
                    specs = [(name + '/' + n, s) for n, s in called.items()]
                else:
                    specs = [(name, spec)]
                for where, s in specs:
                    self.assertIsInstance(s.get('runs-on'), str, where)
                    self.assertTrue(s['runs-on'] == HOSTED or s['runs-on'].startswith('macos-'), (where, s['runs-on']))
        self.assertEqual(self.sweep['jobs']['sweep']['runs-on'], HOSTED)

    def test_the_upstream_sync_runs_on_github_hosted_runners_only(self):
        """Owner decision 2026-11-12: the upstream sync (fork-follow.yml, fork-checks.yml) and the sweep run on
        GitHub-hosted runners only, whatever CI_RUNNERS says: a literal hosted label, no expression, no fleet or
        self-hosted label. The sync's release call passes a ref, so fork-tests' route never runs there (every routed
        job is then GitHub-hosted: test_runs_on_follows_the_setting_on_a_re_run, 'a release run')."""
        hosted = re.compile(r'(ubuntu|windows|macos)-(latest|[0-9]+(\.[0-9]+)?)(-arm)?')
        never = {'self-hosted'} | {p['label'] for p in CFG['pools']}
        for name, workflow in (('fork-follow.yml', self.follow), ('fork-checks.yml', self.checks),
                               ('fork-fleet.yml', self.sweep)):
            for job, spec in workflow['jobs'].items():
                with self.subTest(workflow=name, job=job):
                    if 'uses' in spec:
                        self.assertEqual((name, spec['uses']), ('fork-follow.yml', './.github/workflows/fork-release.yml'))
                        self.assertTrue(str(spec['with'].get('ref') or '').strip())
                        continue
                    runs_on = spec.get('runs-on')
                    self.assertIsInstance(runs_on, str)
                    self.assertNotIn('${{', runs_on)
                    self.assertTrue(re.fullmatch(hosted, runs_on), runs_on)
                    self.assertNotIn(runs_on.lower(), never)
        self.assertEqual(self.release['jobs']['tests']['uses'], './.github/workflows/fork-tests.yml')
        self.assertEqual(self.release['jobs']['tests']['with']['ref'], '${{ needs.preflight.outputs.ref }}')
        self.assertEqual(self.tests['jobs']['changes']['steps'][-1]['if'], "inputs.ref == ''")

    def test_the_launcher_runs_as_root_in_ci(self):
        """LauncherAsRoot needs root and real users: the workflow lint job (GitHub-hosted, throwaway) runs it."""
        job = self.tests['jobs']['workflows']
        self.assertEqual(job['runs-on'], HOSTED)
        runs = [st.get('run') for st in job['steps']]
        root = runs.index('sudo env FORK_FLEET_ROOT_TESTS=1 python3 -B .github/scripts/fork-fleet.test.py LauncherAsRoot')
        # check-net's test needs the real nft, which the hosted image may lack: installed in a step before it.
        self.assertTrue(any('apt-get install -y -qq nftables' in (r or '') for r in runs[:root]), runs)

    def test_the_route_step(self):
        changes = self.tests['jobs']['changes']
        step = changes['steps'][-1]
        self.assertEqual((step['id'], step['if'], step['run']),
                         ('route', "inputs.ref == ''", 'python3 -I .github/scripts/fork-fleet.py route'))
        want = {'FLEET_MODE': f'${{{{ vars.{CFG["variable"]} }}}}',
                # A fork-only pull request routes on its own gates, all false (fork-owned-paths.test.py).
                'GATES': "${{ steps.fork_only.outputs.fork_only == 'true' && toJSON(steps.fork_only.outputs)"
                         " || toJSON(steps.detect.outputs) }}"}
        want.update({p['beacon']: f'${{{{ vars.{p["beacon"]} }}}}' for p in CFG['pools']})
        self.assertEqual(step['env'], want)
        self.assertEqual({k: v for k, v in changes['outputs'].items() if 'route' in v}, {
            'runners': '${{ steps.route.outputs.runners }}', 'fleet_mode': '${{ steps.route.outputs.mode }}',
            'fleet_attempt': '${{ steps.route.outputs.attempt }}',
            'fleet_watch': "${{ steps.route.outputs.watch || 'false' }}"})
        sparse = changes['steps'][0]['with']['sparse-checkout']
        self.assertIn('/.github/fork-fleet.json', sparse)
        self.assertIn('/.github/scripts/fork-fleet.py', sparse)
        self.assertNotIn('route', self.tests['jobs'])   # folded into changes (s2 minor 2)

    def test_no_variable_or_secret_leaks_into_the_route(self):
        import yaml
        self.assertNotRegex(self.text, r'toJSON\(\s*vars\s*\)')   # would print every variable to the log
        self.assertNotIn('secrets.', yaml.safe_dump(self.tests['jobs']['changes']))
        self.assertNotIn('secrets.', yaml.safe_dump(self.tests['jobs']['fleet_watch']))

    def test_the_watch_job(self):
        watch = self.tests['jobs']['fleet_watch']
        self.assertEqual(watch['needs'], 'changes')
        self.assertIn("github.event.pull_request.head.repo.full_name == github.repository", watch['if'])
        self.assertIn("needs.changes.outputs.fleet_watch == 'true'", watch['if'])
        self.assertEqual(watch['runs-on'], HOSTED)
        self.assertEqual(watch['permissions'], {'actions': 'write', 'contents': 'read'})
        self.assertGreaterEqual(watch['timeout-minutes'] * 60, CFG['fleet_stuck_seconds'] + ff.SHOW_UP + 60)
        step = watch['steps'][-1]
        self.assertEqual(step['run'], 'python3 -I .github/scripts/fork-fleet.py watch')
        self.assertEqual(step['env'], {'FLEET_MODE': '${{ needs.changes.outputs.fleet_mode }}',
                                       'FLEET_RUNNERS': '${{ needs.changes.outputs.runners }}',
                                       'GITHUB_TOKEN': '${{ github.token }}'})
        self.assertIn('fleet_watch', self.tests['jobs']['fork_ci_result']['needs'])

    def test_the_sweep(self):
        job = self.sweep['jobs']['sweep']
        self.assertEqual(job['permissions'], {'actions': 'write', 'contents': 'read', 'pull-requests': 'read'})
        self.assertEqual(job['steps'][-1]['env'], {'FLEET_MODE': f'${{{{ vars.{CFG["variable"]} }}}}',
                                                   'GITHUB_TOKEN': '${{ github.token }}'})
        self.assertEqual(job['steps'][-1]['run'], 'python3 -I .github/scripts/fork-fleet.py sweep')

    def test_the_release_caller_grants_what_fork_tests_asks(self):
        """A called workflow fails to start when a job, even a skipped one, asks for more than the caller grants."""
        level = {'none': 0, 'read': 1, 'write': 2}
        top = self.tests.get('permissions') or {}
        granted = self.release['jobs']['tests'].get('permissions')
        self.assertIsInstance(granted, dict)
        for name, job in self.tests['jobs'].items():
            for scope, asked in (job.get('permissions', top) or {}).items():
                with self.subTest(job=name, scope=scope):
                    self.assertGreaterEqual(level[granted.get(scope, 'none')], level[asked])

    def test_no_routed_job_reads_a_secret(self):
        """A fleet job runs on our machines: it gets no secret."""
        import yaml
        for j in CFG['jobs']:
            with self.subTest(j['job']):
                self.assertNotIn('secrets.', yaml.safe_dump(self.tests['jobs'][j['job']]))

    def test_no_routed_job_uses_sudo_on_the_fleet(self):
        """A fleet runner has no sudo: every sudo in a routed job is behind a GitHub-hosted check."""
        for j in CFG['jobs']:
            for step in self.tests['jobs'][j['job']]['steps']:
                script = step.get('run') or ''
                if 'sudo ' not in script:
                    continue
                with self.subTest(job=j['job'], step=step.get('name')):
                    self.assertIn('if [[ "$RUNNER_ENVIRONMENT" == self-hosted ]]', script)
                    self.assertLess(script.index('RUNNER_ENVIRONMENT'), script.index('sudo '))

    def test_the_hook_runs_the_installed_gate_isolated(self):
        self.assertIn('exec /usr/bin/python3 -I "$(dirname -- "$0")/fork-fleet.py" '
                      '--config "$(dirname -- "$0")/fork-fleet.json" gate', self.hook)


# ---------------------------------------------------------------- any repository's config (fork-ci/actions/fleet-route)

# A project with no GitHub-hosted runners: a PC whose dispatcher makes one-job runners (one job of a run at most, pushes
# and draft pull requests only), an always-on build runner, and an always-on runner for pull requests ready for review.
GEN = {'repository': REPO, 'private': True, 'hosted': False, 'trusted': CFG['trusted'], 'stuck_seconds': 900,
       'fleet_stuck_seconds': 7200, 'beacon_fresh_seconds': 180, 'dispatch': CFG['dispatch'],
       'retired_labels': ['proj-old'],
       'pools': [{'name': 'pc', 'label': 'proj-pc', 'slots': 2, 'per_run': 1, 'beacon': 'PROJ_BEACON_PC',
                  'launch': ['launch'], 'waits': False, 'takes': ['push', 'draft']},
                 {'name': 'build', 'static': True, 'runs_on': ['self-hosted', 'proj-build'], 'slots': 1,
                  'waits': True, 'takes': ['push', 'draft']},
                 {'name': 'ready', 'static': True, 'runs_on': ['self-hosted', 'proj-ready'], 'slots': 1,
                  'waits': True, 'takes': ['ready']}],
       'jobs': [{'job': 'plugins', 'memory_mb': 4000}, {'job': 'engine', 'memory_mb': 3000},
                {'job': 'docs', 'memory_mb': 500, 'gates': ['docs']}]}
GEN_PC = ['proj-pc', 'proj-pc-{}-' + NONCE]
BUILD, READY = ['self-hosted', 'proj-build'], ['self-hosted', 'proj-ready']   # GEN's always-on pools
GEN_HOSTED = {**GEN, 'hosted': 'ubuntu-24.04'}   # the same fleet with GitHub-hosted runners
PRIVATE = {'full_name': REPO, 'private': True}   # the event's repository, as a private repository's event has it


def draft(is_draft, **kw):
    event = pr(**kw)
    event['pull_request']['draft'] = is_draft
    return event


def private(event):
    """The event as a private repository's run has it (GitHub puts the repository in every event payload)."""
    return dict(event, repository=dict(event.get('repository') or {}, **PRIVATE)) if isinstance(event, dict) else event


class AnyRepository(unittest.TestCase):
    def route(self, cfg=GEN, mode='', pc=None, e=None, event=PUSH, attempt=None, now=T0, gates=None, own=True,
              fallback=False):
        """own: the event as a private repository's (GEN's static pools need it)."""
        e = dict(e or env())
        if pc is not None:
            e['PROJ_BEACON_PC'] = pc
        got, log = quiet(ff.route_all, cfg, e, private(event) if own else event, mode, gates, now=lambda: now,
                         nonce=NONCE, attempt=attempt)
        return (*got, log) if fallback else (*got[:2], log)

    def test_a_route_only_config_is_valid(self):
        self.assertEqual(ff.check_config(GEN), [])
        only_static = copy.deepcopy(GEN)
        only_static['pools'].pop(0)
        for key in ('beacon_fresh_seconds', 'dispatch'):
            only_static.pop(key)   # no dispatcher: no heartbeat and no dispatcher settings
        self.assertEqual(ff.check_config(only_static), [])

    def test_broken_configs_are_refused(self):
        cases = {
            'a hosted label with a space': (lambda c: c.__setitem__('hosted', 'ubuntu latest'), 'hosted'),
            'hosted: true': (lambda c: c.__setitem__('hosted', True), 'hosted'),
            'a static pool of generic labels only': (lambda c: c['pools'][1].__setitem__('runs_on', ['self-hosted']),
                                                     'runs_on'),
            'per_run on the pool that waits': (lambda c: c['pools'][1].__setitem__('per_run', 1), 'per_run'),
            'per_run of 0': (lambda c: c['pools'][0].__setitem__('per_run', 0), 'per_run'),
            'takes an unknown kind': (lambda c: c['pools'][0].__setitem__('takes', ['merged']), 'takes'),
            'takes nothing': (lambda c: c['pools'][0].__setitem__('takes', []), 'takes'),
            'no pool waits for ready pull requests': (lambda c: c['pools'][2].__setitem__('waits', False), 'ready'),
            'two pools wait for pushes': (lambda c: c['pools'][2].__setitem__('takes', ['push', 'ready']), 'push'),
            'a dispatcher pool, no dispatch settings': (lambda c: c.pop('dispatch'), 'dispatch'),
            'a generic retired label': (lambda c: c.__setitem__('retired_labels', ['self-hosted']), 'retired'),
        }
        for name, (change, word) in cases.items():
            with self.subTest(name):
                bad = copy.deepcopy(GEN)
                change(bad)
                problems = ff.check_config(bad)
                self.assertTrue(any(word in p for p in problems), problems)

    def test_one_heavy_job_per_run_on_the_pc_the_rest_on_the_always_on_pool(self):
        mode, out, log = self.route(pc=beacon(2))
        self.assertEqual(mode, 'fleet')   # no GitHub-hosted runners: auto is fleet
        self.assertEqual(out, {'plugins': [GEN_PC[0], GEN_PC[1].format('plugins')],   # config order: heaviest first
                               'engine': ['self-hosted', 'proj-build'], 'docs': ['self-hosted', 'proj-build']})
        self.assertNotIn('::error::', log)

    def test_a_job_that_avoids_a_pool_for_pushes_goes_to_the_next_one(self):
        """Issue 224 item 3: a project builds what its deploy ships on its always-on pool only (push runs), while its
        other heavy job may use the desktop. per_run then goes to the heaviest job that does not avoid the pool."""
        cfg = copy.deepcopy(GEN)
        cfg['jobs'][0]['avoid'] = {'pc': ['push']}
        self.assertEqual(ff.check_config(cfg), [])
        _, out, log = self.route(cfg, pc=beacon(2))
        self.assertEqual(out, {'plugins': BUILD, 'engine': [GEN_PC[0], GEN_PC[1].format('engine')], 'docs': BUILD})
        self.assertIn('pool pc: not for plugins in push runs', log)
        _, out, _ = self.route(cfg, pc=beacon(2), e=env(**PR_ENV), event=draft(True))   # a draft: avoid says push only
        self.assertEqual(out['plugins'], [GEN_PC[0], GEN_PC[1].format('plugins')])
        for mode in ff.MODES:   # fleet mode queues it on the pool that waits, never on the avoided one
            with self.subTest(mode=mode):
                _, out, _ = self.route(cfg, mode=mode, pc=beacon(0))
                self.assertEqual(out['plugins'], BUILD)
        cfg['jobs'][1]['avoid'] = {'pc': ['push', 'draft']}
        cfg['jobs'][2]['avoid'] = {'pc': ['push']}
        _, out, log = self.route(cfg, pc=beacon(2))   # every job avoids it: the pool takes none, the slot stays free
        self.assertEqual(set(map(tuple, out.values())), {tuple(BUILD)})

    def test_a_broken_avoid_is_refused(self):
        cases = {
            'not an object': ([], 'avoid must be an object'),
            'empty': ({}, 'avoid must be an object'),
            'an unknown pool': ({'desk': ['push']}, "pool 'desk', which the config does not have"),
            'a static pool': ({'build': ['push']}, 'only a dispatcher pool that does not wait'),
            'the pool that waits': ({'ready': ['ready']}, 'only a dispatcher pool that does not wait'),
            'an unknown kind': ({'pc': ['merged']}, "avoid['pc'] must list some of"),
            'no kinds': ({'pc': []}, "avoid['pc'] must list some of"),
            'kinds as a string': ({'pc': 'push'}, "avoid['pc'] must list some of"),
        }
        for name, (avoid, words) in cases.items():
            with self.subTest(name):
                bad = copy.deepcopy(GEN)
                bad['jobs'][0]['avoid'] = avoid
                problems = ff.check_config(bad)
                self.assertTrue(any(words in p for p in problems), problems)
        waiting = copy.deepcopy(CFG)   # the fork's own config: its pool that waits is a dispatcher pool
        waiting['jobs'][0]['avoid'] = {WAITING: ['push']}
        self.assertTrue(any('does not wait' in p for p in ff.check_config(waiting)), ff.check_config(waiting))

    def test_an_unknown_job_key_is_refused(self):
        # A misspelled `avoid` would otherwise be ignored and the job sent to the pool it meant to avoid.
        bad = copy.deepcopy(GEN)
        bad['jobs'][0]['avoids'] = {'pc': ['push']}
        problems = ff.check_config(bad)
        self.assertTrue(any("unknown key 'avoids'" in p for p in problems), problems)
        self.assertEqual(ff.check_config(copy.deepcopy(GEN)), [])

    def rerun(self, cfg, pc=None, e=None, event=PUSH, attempt=None, mode='', gates=None, own=True):
        """(rerun output, log) of route_all for this run."""
        e = dict(e or env())
        if pc is not None:
            e['PROJ_BEACON_PC'] = pc
        got = {}
        _, log = quiet(ff.route_all, cfg, e, private(event) if own else event, mode, gates, now=lambda: T0,
                       nonce=NONCE, attempt=attempt, rerun=got)
        return got, log

    def test_a_trusted_persons_rerun_may_use_the_reruns_pool_for_its_own_attempt(self):
        """Issue 224 item 4: a re-run of failed jobs does not re-run route; with `reruns`, route says ahead which run
        label each job may ask for on that pool, and the job adds -a<its attempt>. The dispatcher (unchanged) accepts
        exactly that pair for that attempt only."""
        cfg = {**copy.deepcopy(GEN), 'reruns': 'pc'}
        self.assertEqual(ff.check_config(cfg), [])
        for attempt in (None, 1, 2, 3):   # a full re-run re-runs route too: the rerun labels never carry an attempt
            with self.subTest(attempt=attempt):
                got, _ = self.rerun(cfg, pc=beacon(2), attempt=attempt)
                self.assertEqual(got, {'actors': list(GEN['trusted']['actors']), 'pool': 'proj-pc',
                                       'labels': {k: GEN_PC[1].format(k) for k in ('plugins', 'engine', 'docs')}})
        for attempt in (2, 3):   # what the README's runs-on asks for on attempt N
            with self.subTest(attempt=attempt):
                ask = [got['pool'], got['labels']['engine'] + f'-a{attempt}']
                self.assertEqual(ff.fleet_job(cfg, ask)[1:], ('engine', attempt))
        self.assertIsNone(ff.fleet_job(cfg, [got['pool'], got['labels']['engine']]))   # never without an attempt
        got, _ = self.rerun(GEN, pc=beacon(2))   # no reruns: re-runs take fallback
        self.assertEqual(got, {})

    def test_a_full_pool_still_takes_reruns_a_missing_or_stale_heartbeat_does_not(self):
        cfg = {**copy.deepcopy(GEN), 'reruns': 'pc'}
        got, _ = self.rerun(cfg, pc=beacon(0))   # full now: the re-run waits for a slot (the sweep bounds the wait)
        self.assertEqual(got['pool'], 'proj-pc')
        cases = {'no heartbeat': None, 'a stale heartbeat': beacon(2, at=T0 - 3600), 'a garbled heartbeat': '{'}
        for name, pc in cases.items():
            with self.subTest(name):
                got, log = self.rerun(cfg, pc=pc)
                self.assertEqual(got, {})
                self.assertIn('re-runs of failed jobs take the fallback, not pool pc', log)

    def test_reruns_only_for_trusted_runs_kinds_the_pool_takes_and_jobs_that_do_not_avoid_it(self):
        cfg = {**copy.deepcopy(GEN_HOSTED), 'reruns': 'pc'}
        cases = {
            'a pull request from another repository': dict(e=env(**PR_ENV), event=pr(head='someone/fork')),
            'an untrusted actor': dict(e=env(GITHUB_ACTOR='someone', GITHUB_TRIGGERING_ACTOR='someone')),
            'github mode': dict(mode='github'),
            'a ready pull request (pc does not take ready)': dict(e=env(**PR_ENV), event=draft(False)),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                got, _ = self.rerun(cfg, pc=beacon(2), **kw)
                self.assertEqual(got, {})
        got, _ = self.rerun(cfg, pc=beacon(2), gates={'docs': 'false'})   # a job this change skips gets none
        self.assertEqual(sorted(got['labels']), ['engine', 'plugins'])
        cfg['jobs'][0]['avoid'] = {'pc': ['push']}
        got, _ = self.rerun(cfg, pc=beacon(2))
        self.assertEqual(sorted(got['labels']), ['docs', 'engine'])
        got, _ = self.rerun(cfg, pc=beacon(2), e=env(**PR_ENV), event=draft(True))   # avoid says push only
        self.assertIn('plugins', got['labels'])
        for job in cfg['jobs']:
            job['avoid'] = {'pc': ['push']}
        got, _ = self.rerun(cfg, pc=beacon(2))
        self.assertEqual(got, {})

    def test_reruns_must_name_a_dispatcher_pool(self):
        for name in ('build', 'desk', '', None):
            with self.subTest(name=name):
                problems = ff.check_config({**GEN, 'reruns': name})
                self.assertTrue(any('reruns' in p for p in problems), problems)

    def test_pool_by_draft_state(self):
        _, out, _ = self.route(pc=beacon(2), e=env(**PR_ENV), event=draft(True))
        self.assertEqual(out['plugins'][0], 'proj-pc')
        _, out, log = self.route(pc=beacon(2), e=env(**PR_ENV), event=draft(False))
        self.assertEqual(set(map(tuple, out.values())), {('self-hosted', 'proj-ready')})
        self.assertIn('pool pc: does not take ready runs', log)
        _, out, _ = self.route(pc=beacon(2), e=env(**PR_ENV), event=pr())   # no draft field: ready
        self.assertEqual(set(map(tuple, out.values())), {('self-hosted', 'proj-ready')})

    def test_an_unreachable_pc_is_red_and_falls_back_to_the_always_on_pool(self):
        """Old bug 1280 (OldBugs): an expired or revoked dispatcher token stops its heartbeat; that must never be silent."""
        for name, value in {'stale': beacon(2, at=T0 - 181), 'garbled': '{"at": '}.items():
            with self.subTest(name):
                _, out, log = self.route(pc=value)
                self.assertEqual(set(map(tuple, out.values())), {('self-hosted', 'proj-build')})
                self.assertRegex(log, r'::error::fork-fleet: pool pc: no free slot counted: .*valid token')
        _, _, log = self.route(CFG, pc=None, e=env(**{POOLS[FIRST]['beacon']: beacon(1, at=T0 - 10_000)}))
        self.assertIn(f'::error::fork-fleet: pool {FIRST}', log)   # the fork's own auto mode: red too, then hosted

    def test_a_dispatcher_whose_token_is_refused_writes_no_heartbeat_and_route_turns_red(self):
        """Old bug 1280 end to end: the dispatcher's pass stops on the 401, so its heartbeat ages and route says so."""
        gh = FakeGitHub()
        gh.trouble.append((r'GET actions/runs\?status=\w+', http_error(401)))
        fake = FakeLauncher(WAITING)
        with self.assertRaises(urllib.error.HTTPError):
            quiet(ff.dispatch_pass, CFG, gh, {WAITING: ff.Host(POOLS[WAITING], run=fake.run)}, ff.new_state(),
                  now=lambda: T0)
        self.assertEqual(gh.variables, {})
        _, _, log = self.route(CFG, mode='fleet', e=env(**{POOLS[WAITING]['beacon']: beacon(2, at=T0 - 600)}))
        self.assertIn(f'::error::fork-fleet: pool {WAITING}', log)

    def test_github_mode_without_hosted_runners_is_red_and_fleet(self):
        mode, out, log = self.route(mode='github', pc=beacon(1))
        self.assertEqual(mode, 'fleet')
        self.assertIn('::error::', log)
        self.assertEqual(out['engine'], ['self-hosted', 'proj-build'])

    def test_a_pull_request_from_another_repository_never_gets_a_self_hosted_runner(self):
        """head.repo != base.repo: GitHub-hosted in every mode, whatever the heartbeats say, never a skip (a skipped
        required check counts as passed). With "hosted": false there is no runner for it: route fails red."""
        fresh = {p['beacon']: beacon(5) for p in CFG['pools']}
        fresh['PROJ_BEACON_PC'] = beacon(5)
        events = {'fork head': pr(head='someone/t3code'), 'other base': pr(base='someone/t3code'),
                  'fork draft': draft(True, head='someone/t3code'), 'no payload': None}
        for cfg in (CFG, GEN_HOSTED, GEN):
            for mode in ff.MODES:
                for name, event in events.items():
                    with self.subTest(cfg=cfg['hosted'], mode=mode, event=name):
                        if cfg['hosted'] is False:
                            with self.assertRaisesRegex(ValueError, 'another repository.*no job of it can run'):
                                self.route(cfg, mode, e=env(**PR_ENV, **fresh), event=event)
                            continue
                        _, out, fallback, _ = self.route(cfg, mode, e=env(**PR_ENV, **fresh), event=event,
                                                         fallback=True)
                        self.assertEqual(set(out.values()) | {fallback}, {cfg['hosted']})

    def test_every_label_put_out_is_one_something_serves(self):
        """A static pool's runs_on, or a dispatcher pool's label with a run label it parses back: never ubuntu-latest
        or a label nothing serves, for jobs that run and jobs whose change gate is off (they only print a skip)."""
        static = [tuple(p['runs_on']) for p in GEN['pools'] if p.get('static')]
        for mode in ff.MODES:
            for kind, (e, event) in {'push': (env(), PUSH), 'draft': (env(**PR_ENV), draft(True)),
                                     'ready': (env(**PR_ENV), draft(False))}.items():
                for pc in (None, beacon(0), beacon(1), beacon(9), beacon(9, at=T0 - 999)):
                    for gates in (None, {'docs': 'false'}, {'docs': 'true'}):
                        with self.subTest(mode=mode, kind=kind, pc=pc, gates=gates):
                            _, out, fallback, _ = self.route(mode=mode, pc=pc, e=e, event=event, attempt=2,
                                                             gates=gates, fallback=True)
                            self.assertIn(tuple(fallback), static)
                            for key, got in out.items():
                                self.assertIsInstance(got, list)
                                if tuple(got) not in static:
                                    self.assertEqual(ff.fleet_job(GEN, got)[1:], (key, 2))

    def test_hosted_never_names_a_self_hosted_pool_or_retired_label(self):
        """Lens #53 security, major 1: untrusted runs go to `hosted`, so it must be a GitHub-hosted label. The config check
        refuses it, and route checks it again (a config handed to it need not have passed load)."""
        for cfg in (GEN_HOSTED, CFG):
            bad = ['self-hosted', 'linux', 'x64'] + [p['label'] for p in ff.dispatched(cfg)] + \
                [p['label'] + '-plugins-' + NONCE for p in ff.dispatched(cfg)] + \
                [x for p in cfg['pools'] if p.get('static') for x in p['runs_on']] + cfg.get('retired_labels', [])
            for label in bad:
                with self.subTest(repository=cfg['hosted'], hosted=label):
                    wrong = dict(cfg, hosted=label)
                    self.assertTrue(any('hosted' in p for p in ff.check_config(wrong)), ff.check_config(wrong))
                    with self.assertRaisesRegex(ValueError, 'not a GitHub-hosted label'):
                        quiet(ff.route, wrong, env(**PR_ENV), pr(head='someone/t3code'), 'auto', None)
        self.assertEqual(ff.check_config(GEN_HOSTED), [])

    def test_static_pools_serve_a_private_repository_only(self):
        """Lens #53 security, major 2: a static runner has no job-started hook, so a workflow a pull request changed
        could name its labels. The config must say the repository is private, and route uses no static pool unless the
        event says so too (unknown refuses)."""
        public = dict(GEN)
        public.pop('private')
        for cfg in (public, dict(GEN, private=False), dict(GEN_HOSTED, private=False)):
            self.assertTrue(any('private' in p for p in ff.check_config(cfg)), ff.check_config(cfg))
        static = {x for p in GEN['pools'] if p.get('static') for x in p['runs_on']} - ff.GENERIC
        for name, repository in {'public': {'full_name': REPO, 'private': False}, 'not said': {'full_name': REPO},
                                 'a string': {'full_name': REPO, 'private': 'true'}}.items():
            for kind, (e, event) in {'push': (env(), {'repository': repository}),
                                     'ready': (env(**PR_ENV), dict(draft(False), repository=repository))}.items():
                for mode in ff.MODES:
                    with self.subTest(name, kind=kind, mode=mode):
                        with self.assertRaisesRegex(ValueError, 'no runner may take'):
                            self.route(GEN, mode, pc=beacon(2), e=e, event=event, own=False)
                        _, out, fallback, log = self.route(GEN_HOSTED, mode, pc=beacon(2), e=e, event=event,
                                                           own=False, fallback=True)
                        put = {x for v in [*out.values(), fallback] for x in ([v] if isinstance(v, str) else v)}
                        self.assertFalse(put & static, out)
                        self.assertIn('::error::fork-fleet: no static pool is used', log)

    def test_private_must_be_a_boolean(self):
        """Issue #63: "private" is true or false, never text, null or a number. The fork's own config has no static
        pool, so only the type check can refuse it there; GEN's static pools need "private": true besides."""
        wrong = 'private must be true or false'
        for value in ('true', 'false', '', None, 1, 0, [], {}):
            with self.subTest(private=value):
                self.assertEqual(ff.check_config(dict(CFG, private=value)), [wrong])
                self.assertIn(wrong, ff.check_config(dict(GEN, private=value)))
        for value in (True, False):
            with self.subTest(private=value):
                self.assertEqual(ff.check_config(dict(CFG, private=value)), [])
                self.assertNotIn(wrong, ff.check_config(dict(GEN, private=value)))
        self.assertEqual(ff.check_config(GEN), [])

    def test_the_waiting_static_pool_is_the_always_on_one_in_any_order(self):
        """Issue #64: of two static pools of one kind, the one that waits is where a job whose gate is off, every job of
        an untrusted run, and the fallback go; never the first one listed."""
        spare = {'name': 'spare', 'static': True, 'runs_on': ['self-hosted', 'proj-spare'], 'slots': 1,
                 'waits': False, 'takes': ['push', 'draft']}
        for where in (1, 2):   # before the build pool, which waits for pushes, and after it
            cfg = copy.deepcopy(GEN)
            cfg['pools'].insert(where, spare)
            with self.subTest(spare_at=where):
                self.assertEqual(ff.check_config(cfg), [])
                self.assertEqual(ff.always_on(cfg, 'push', None)['name'], 'build')
                for mode in ff.MODES:
                    _, out, fallback, _ = self.route(cfg, mode, pc=beacon(0), gates={'docs': 'false'}, fallback=True)
                    self.assertEqual((out['docs'], fallback), (BUILD, BUILD), mode)
                    _, out, fallback, _ = self.route(cfg, mode, pc=beacon(2), e=env(GITHUB_TRIGGERING_ACTOR='someone'),
                                                     fallback=True)
                    self.assertEqual(set(map(tuple, out.values())) | {tuple(fallback)}, {tuple(BUILD)}, mode)

    def test_a_static_label_is_never_a_dispatcher_or_retired_label(self):
        """Lens #53 security minor and correctness minor: a static runner with a dispatcher pool's label is a rival of
        every one-job runner (the dispatcher never starts one) and may be given a job its gate kills."""
        for label in ('proj-pc', 'proj-pc-plugins-' + NONCE + '-a1', 'proj-old'):
            with self.subTest(label):
                bad = copy.deepcopy(GEN)
                bad['pools'][1]['runs_on'] = ['self-hosted', label]
                self.assertTrue(any('runs_on names a dispatcher' in p for p in ff.check_config(bad)),
                                ff.check_config(bad))

    def test_without_hosted_runners_nothing_goes_to_ubuntu_latest(self):
        """Lens #53 correctness, major 1: with "hosted": false, a job whose change gate is off, a run where no routed
        job runs, a manual or scheduled run, a dependency bot's pull request and an untrusted re-run all go to the
        always-on pool of their kind; a run that may hold another repository's code is red (never skipped, never
        self-hosted)."""
        off = {'docs': 'false'}
        nothing = copy.deepcopy(GEN)
        nothing['jobs'] = [dict(j, gates=['docs']) for j in GEN['jobs']]
        bot = 'dependabot[bot]'
        same = {
            'gate off': (GEN, env(), PUSH, BUILD),
            'gate off, ready pull request': (GEN, env(**PR_ENV), draft(False), READY),
            'no routed job runs': (nothing, env(), PUSH, BUILD),
            'manual': (GEN, env(GITHUB_EVENT_NAME='workflow_dispatch'), PUSH, BUILD),
            'scheduled': (GEN, env(GITHUB_EVENT_NAME='schedule'), PUSH, BUILD),
            'dependency bot': (GEN, env(**PR_ENV, GITHUB_ACTOR=bot, GITHUB_TRIGGERING_ACTOR=bot), pr(user=bot), READY),
            'untrusted re-run': (GEN, env(GITHUB_TRIGGERING_ACTOR='someone'), PUSH, BUILD),
            'another workflow': (GEN, env(GITHUB_WORKFLOW_REF=f'{REPO}/.github/workflows/x.yml@refs/heads/main'),
                                 PUSH, BUILD),
            'own pull_request_target': (GEN, env(GITHUB_EVENT_NAME='pull_request_target'), draft(True), BUILD),
        }
        for name, (cfg, e, event, home) in same.items():
            for mode in ff.MODES:
                with self.subTest(name, mode=mode):
                    _, out, fallback, log = self.route(cfg, mode, pc=beacon(0), e=e, event=event, gates=off,
                                                       fallback=True)
                    self.assertEqual(out['docs'], home)
                    self.assertEqual(fallback, home)
                    self.assertNotIn('ubuntu', json.dumps(out))
        _, _, log = self.route(GEN, e=env(GITHUB_EVENT_NAME='schedule'))
        self.assertIn('pool build (always there) for every job: a \'schedule\' event (only trusted runs may use the '
                      'fleet; a run of this repository', log)
        foreign = {
            'fork pull request': (env(**PR_ENV), pr(head='someone/t3code')),
            'fork pull_request_target': (env(GITHUB_EVENT_NAME='pull_request_target'), pr(head='someone/t3code')),
            'workflow_run': (env(GITHUB_EVENT_NAME='workflow_run'), PUSH),
            'issue_comment': (env(GITHUB_EVENT_NAME='issue_comment'), PUSH),
            'another repository': (env(GITHUB_REPOSITORY='someone/t3code'), PUSH),
            'a push event of another repository': (env(), {'repository': {'full_name': 'someone/t3code',
                                                                           'private': True}}),
        }
        for name, (e, event) in foreign.items():
            with self.subTest(name):
                event = event if 'repository' in event else private(event)
                with self.assertRaisesRegex(ValueError, 'another repository'):
                    self.route(GEN, pc=beacon(2), e=e, event=event, gates=off, own=False)
                _, out, fallback, _ = self.route(GEN_HOSTED, pc=beacon(2), e=e, event=event, gates=off, own=False,
                                                 fallback=True)
                self.assertEqual(set(out.values()) | {fallback}, {'ubuntu-24.04'})

    def test_hosted_false_needs_a_static_pool_for_each_kind_and_no_upstream_sync(self):
        no_ready = copy.deepcopy(GEN)
        no_ready['pools'][2] = {'name': 'ready', 'label': 'proj-ready', 'slots': 1, 'beacon': 'PROJ_BEACON_READY',
                                'launch': ['launch'], 'waits': True, 'takes': ['ready']}
        self.assertTrue(any('static pool must take ready' in p for p in ff.check_config(no_ready)))
        self.assertEqual(ff.check_config(dict(no_ready, hosted='ubuntu-24.04')), [])
        self.assertTrue(any('upstream_sync needs a hosted' in p for p in
                            ff.check_config(dict(GEN, upstream_sync={'tag_prefix': 'synced/'}))))

    def test_a_re_run_of_failed_jobs_falls_back_to_a_runner_that_exists(self):
        """Lens #53 correctness major 2 / security minor: a re-run of failed jobs does not re-run route, and a run label
        serves one attempt. The fallback output, for a job of another attempt, is never a dispatcher pool's label: the
        always-on pool for a run of this repository, GitHub-hosted for the fork's own config (it has none)."""
        with tempfile.TemporaryDirectory() as d:
            cases = {'GEN': (GEN, private(PUSH), BUILD), 'GEN ready': (GEN, private(draft(False)), READY),
                     'the fork': (CFG, PUSH, HOSTED), 'GEN hosted': (GEN_HOSTED, private(PUSH), BUILD)}
            for name, (cfg, event, want) in cases.items():
                with self.subTest(name):
                    out, path = os.path.join(d, name), os.path.join(d, 'event')
                    with open(path, 'w') as f:
                        json.dump(event, f)
                    e = env(**({} if 'pull_request' not in event else PR_ENV), GITHUB_OUTPUT=out, GITHUB_EVENT_PATH=path,
                            FLEET_MODE='fleet', PROJ_BEACON_PC=beacon(2, at=int(ff.time.time())))
                    quiet(ff.cmd_route, cfg, e, True)
                    with open(out) as f:
                        got = dict(line.split('=', 1) for line in f.read().splitlines())
                    self.assertEqual(json.loads(got['fallback']), want)
                    self.assertIsNone(ff.fleet_job(cfg, want if isinstance(want, list) else [want]))

    def test_with_attempt_the_run_label_is_complete_and_without_it_the_fork_wiring_adds_it(self):
        _, out, _ = self.route(pc=beacon(1), attempt=3)
        self.assertEqual(out['plugins'][1], GEN_PC[1].format('plugins') + '-a3')
        self.assertEqual(ff.fleet_job(GEN, out['plugins'])[1:], ('plugins', 3))
        _, out, _ = self.route(pc=beacon(1))
        self.assertIsNone(ff.fleet_job(GEN, out['plugins']))

    def test_the_dispatcher_and_its_default_pools_leave_static_pools_alone(self):
        self.assertIsNone(ff.fleet_job(GEN, ['self-hosted', 'proj-build']))
        self.assertEqual([p['name'] for p in ff.dispatched(GEN)], ['pc'])
        with self.assertRaisesRegex(ValueError, 'no pool build with a dispatcher'):
            ff.cmd_dispatch(GEN, ff.argparse.Namespace(pool=['build'], token_file='/nonexistent', lock_dir='/nonexistent',
                                                       once=True))

    def test_no_upstream_sync_means_no_sync_check(self):
        cfg = copy.deepcopy(GEN)
        self.assertIsNone(ff.made_by_the_sync(cfg, 'not a sha'))
        cfg['upstream_sync'] = {'tag_prefix': 'synced/'}
        self.assertIn('not a full sha', ff.made_by_the_sync(cfg, 'not a sha'))

    def test_a_job_queued_on_a_retired_or_static_label_is_still_the_sweeps(self):
        """A run routed before a pool's label changed queues on the old label: the watch and sweep keep handling it."""
        for got in (['proj-old', 'proj-old-x'], ['self-hosted', 'proj-build']):
            self.assertTrue(ff.is_fleet(GEN, {'labels': got}))
        self.assertFalse(ff.is_fleet(GEN, {'labels': ['self-hosted']}))
        gh = FakeGitHub()
        gh.add(a_run(status='in_progress'), a_job(11, ['proj-old', 'proj-old-lint']))
        gh.branches['main'] = SHA
        waits = []

        def sleep(s):   # the fake run never ends: a sweep that waits for it (auto mode) fails here, never hangs
            waits.append(s)
            if len(waits) > 50:
                raise AssertionError('the sweep waits for the cancelled run: it took the setting as auto')
        _, log = quiet(ff.sweep, GEN, {}, gh, now=lambda: T0 + GEN['fleet_stuck_seconds'], sleep=sleep)
        self.assertEqual([p for _, p in gh.writes()], ['actions/runs/5/cancel'])
        self.assertIn('::error::', log)

    def run_script(self, config, extra=()):
        """The script as the action runs it: no token, no gh config, a broken or missing config."""
        with tempfile.TemporaryDirectory() as d:
            out, event = os.path.join(d, 'out'), os.path.join(d, 'event')
            with open(event, 'w') as f:
                json.dump(private(PUSH), f)
            e = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'GH_CONFIG_DIR': d, 'GITHUB_OUTPUT': out,
                 'GITHUB_EVENT_PATH': event, **env()}
            got = subprocess.run([sys.executable, '-I', os.path.join(HERE, 'fork-fleet.py'), '--config', config,
                                  'route', *extra], env=e, capture_output=True, text=True, timeout=60)
            written = ''
            if os.path.exists(out):
                with open(out, encoding='utf-8') as f:
                    written = f.read()
        return got, written

    def test_a_missing_or_broken_config_fails_red_and_puts_out_nothing(self):
        """The caller's runs-on then falls back: the always-on pool only for a push or a pull request of this
        repository (fleet-route's README), otherwise GitHub-hosted."""
        with tempfile.TemporaryDirectory() as d:
            broken = os.path.join(d, 'fleet.json')
            with open(broken, 'w') as f:
                json.dump(dict(GEN, hosted=True), f)
            for config in (os.path.join(d, 'missing.json'), broken):
                with self.subTest(config):
                    got, written = self.run_script(config)
                    self.assertEqual(got.returncode, 1)
                    self.assertIn('::error::fork-fleet:', got.stdout)
                    self.assertEqual(written, '')
            good = os.path.join(d, 'good.json')
            with open(good, 'w') as f:
                json.dump(GEN, f)
            got, written = self.run_script(good, ['--with-attempt'])
            self.assertEqual(got.returncode, 0, got.stdout + got.stderr)
            self.assertIn('runners={"plugins":["self-hosted","proj-build"]', written)


class OldBugs(unittest.TestCase):
    """Bugs of a project's own router, kept as tests of this one."""

    def setUp(self):
        self.gh = FakeGitHub()
        self.gh.branches['main'] = SHA

    def sweep(self):
        got, log = quiet(ff.sweep, CFG, {}, self.gh, now=lambda: T0 + 800, sleep=lambda s: None)
        return got, [p for _, p in self.gh.writes()], log

    def cut_off(self, rid=5, **kw):
        """A completed run whose fleet job lost its runner, and a job that needs it (cancelled with it)."""
        return self.gh.add(a_run(rid, status='completed', conclusion='failure', **kw),
                           a_job(11, labels(WAITING, 'lint'), status='completed', conclusion='failure',
                                 runner='fleet-vm-11', completed=T0 + 300, steps=[{'conclusion': 'cancelled'}]),
                           a_job(12, [HOSTED], status='completed', conclusion='skipped', name='needs lint'))

    def test_1388_a_re_run_covers_dependents_without_reading_the_workflow(self):
        """The router never parses the workflow's needs: it re-runs the whole run, so every dependent runs again."""
        self.cut_off()
        _, posts, _ = self.sweep()
        self.assertEqual(posts, ['actions/runs/5/rerun'])
        self.assertFalse([p for _, p, _ in self.gh.calls if 'contents' in p or 'workflows/' in p and 'runs' not in p])

    def test_1348_a_cut_off_pull_request_run_is_re_run_not_only_main(self):
        self.gh.own_pr()
        self.gh.branches['fork/x'] = SHA
        self.cut_off(event='pull_request', branch='fork/x', prs=[7])
        _, posts, _ = self.sweep()
        self.assertEqual(posts, ['actions/runs/5/rerun'])
        self.gh.branches['fork/x'] = 'f' * 40   # a newer push replaced it: that run is moot
        self.gh.calls.clear()
        _, posts, log = self.sweep()
        self.assertEqual(posts, [])
        self.assertIn('moved on', log)

    def test_1279_a_re_run_that_does_not_start_is_red_and_the_next_sweep_starts_it(self):
        """No waiting for a re-run that never comes: the failed POST is red now, and the sweep keeps no state, so the
        next one re-runs it. Once re-run (attempt 2, by github-actions), later sweeps leave it alone."""
        self.cut_off()
        self.gh.trouble.append((r'POST actions/runs/5/rerun', http_error(500)))
        got, posts, log = self.sweep()
        self.assertEqual((got, posts), (1, ['actions/runs/5/rerun']))
        self.assertIn('::warning::', log)
        self.gh.trouble.clear()
        got, posts, _ = self.sweep()
        self.assertEqual((got, posts), (0, ['actions/runs/5/rerun'] * 2))
        self.gh.runs[5].update(run_attempt=2, triggering_actor={'login': 'github-actions[bot]'})
        self.gh.calls.clear()
        self.assertEqual(self.sweep()[:2], (0, []))

    def test_running_the_dispatcher_twice_deletes_only_offline_leftovers_once(self):
        fake = FakeLauncher(WAITING)
        hosts = {WAITING: ff.Host(POOLS[WAITING], run=fake.run)}
        label = POOLS[WAITING]['label']

        def reg(jid, status='offline', busy=False):
            self.gh.runners.append({'id': jid, 'name': f'{label}-{jid}', 'status': status, 'busy': busy,
                                    'labels': [{'name': label}]})
        reg(40)                          # its job ended, its unit is gone: a leftover
        reg(41)                          # its unit is starting: kept
        fake.units.add(f'{label}-41')
        reg(42, status='online', busy=True)
        self.gh.runners.append({'id': 77, 'name': 'someone-else-3', 'status': 'offline', 'busy': False, 'labels': []})
        other = POOLS[FIRST if WAITING != FIRST else SECOND]['label']
        self.gh.runners.append({'id': 78, 'name': f'{other}-78', 'status': 'offline', 'busy': False, 'labels': []})
        state = ff.new_state()
        for _ in range(2):
            quiet(ff.dispatch_pass, CFG, self.gh, hosts, state, now=lambda: T0)
        deletes = [p for m, p in self.gh.writes() if m == 'DELETE']
        self.assertEqual(deletes, ['actions/runners/40'])
        self.assertEqual(sorted(r['id'] for r in self.gh.runners), [41, 42, 77, 78])


class Vars(unittest.TestCase):
    """The fleet-route action passes the repository variables as one JSON object ($FLEET_VARS)."""
    CFG = {**GEN, 'variable': 'PROJ_RUNNERS'}

    def outputs(self, **over):
        with tempfile.TemporaryDirectory() as d:
            out, event = os.path.join(d, 'out'), os.path.join(d, 'event')
            with open(event, 'w') as f:
                json.dump(private(PUSH), f)
            e = env(GITHUB_OUTPUT=out, GITHUB_EVENT_PATH=event, **over)
            got, log = quiet(ff.cmd_route, self.CFG, e)
            self.assertEqual(got, 0)
            with open(out) as f:
                return dict(line.split('=', 1) for line in f.read().splitlines()), log

    def test_the_heartbeat_and_setting_come_from_vars(self):
        beat = beacon(2, at=int(ff.time.time()))
        lines, _ = self.outputs(FLEET_VARS=json.dumps({'PROJ_BEACON_PC': beat, 'OTHER': 'x'}))
        self.assertEqual(json.loads(lines['runners'])['plugins'][0], 'proj-pc')
        lines, _ = self.outputs(FLEET_VARS='{}')   # no heartbeat: the always-on pool
        self.assertEqual(json.loads(lines['runners'])['plugins'], ['self-hosted', 'proj-build'])

    def test_a_setting_in_vars_is_checked_and_env_wins(self):
        with self.assertRaises(ValueError):   # a typo is red, never auto
            self.outputs(FLEET_VARS=json.dumps({'PROJ_RUNNERS': 'fleeet'}))
        lines, _ = self.outputs(FLEET_VARS=json.dumps({'PROJ_RUNNERS': 'fleeet'}), FLEET_MODE='fleet')
        self.assertEqual(lines['mode'], 'fleet')
        stale = beacon(2, at=1)
        lines, _ = self.outputs(FLEET_VARS=json.dumps({'PROJ_BEACON_PC': beacon(2, at=int(ff.time.time()))}),
                                PROJ_BEACON_PC=stale)
        self.assertEqual(json.loads(lines['runners'])['plugins'], ['self-hosted', 'proj-build'])

    def test_unreadable_vars_are_red(self):
        for raw in ('{', '[]', '"x"'):
            with self.subTest(raw):
                lines, log = self.outputs(FLEET_VARS=raw)
                self.assertIn('::error::fork-fleet: the vars input', log)
                self.assertEqual(json.loads(lines['runners'])['plugins'], ['self-hosted', 'proj-build'])


if __name__ == '__main__':
    unittest.main()
