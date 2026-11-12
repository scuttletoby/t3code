#!/usr/bin/env python3
"""Tests of fork-fleet.py and of its wiring in fork-tests.yml and fork-fleet.yml. Run: python3 <this>."""
import copy, importlib.util, io, json, os, subprocess, sys, tempfile, unittest
from contextlib import redirect_stdout
from unittest import mock

sys.dont_write_bytecode = True   # importing fork-fleet.py must not leave __pycache__ in the tree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
_spec = importlib.util.spec_from_file_location('fork_fleet', os.path.join(HERE, 'fork-fleet.py'))
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)

CFG = ff.load()
REPO = CFG['repository']
OWNER = CFG['trusted']['actors'][0]
WORKFLOW = CFG['trusted']['workflows'][0]
POOLS = {p['name']: p for p in CFG['pools']}
WAITING = next(p for p in CFG['pools'] if p['waits'])
KEYS = [k for k, _ in ff.job_keys(CFG)]
HOSTED = CFG['hosted']


def env(**over):
    e = {'GITHUB_REPOSITORY': REPO, 'GITHUB_WORKFLOW_REF': f'{REPO}/{WORKFLOW}@refs/heads/main',
         'GITHUB_ACTOR': OWNER, 'GITHUB_TRIGGERING_ACTOR': OWNER, 'GITHUB_EVENT_NAME': 'push',
         'GITHUB_REF': 'refs/heads/main', 'GITHUB_RUN_ATTEMPT': '1'}
    e.update(over)
    return {k: v for k, v in e.items() if v is not None}


PUSH = {'repository': {'full_name': REPO}}


def pr(head=REPO, base=REPO, user=OWNER):
    return {'pull_request': {'head': {'repo': {'full_name': head}}, 'base': {'repo': {'full_name': base}},
                             'user': {'login': user}}}


PR_ENV = dict(GITHUB_EVENT_NAME='pull_request', GITHUB_REF='refs/pull/7/merge')


def runner(pool, status='online', busy=False, extra=()):
    return {'status': status, 'busy': busy,
            'labels': [{'name': n} for n in list(POOLS[pool]['labels']) + list(extra)]}


class FakeGH:
    def __init__(self, answers=None, fail=None):
        self.answers, self.fail, self.calls = answers or {}, fail, []

    def call(self, path, method='GET', data=None):
        self.calls.append((method, path))
        if self.fail:
            raise self.fail
        for prefix, got in self.answers.items():
            if path.startswith(prefix):
                return got(method) if callable(got) else got
        raise AssertionError(f'unexpected call {method} {path}')

    def pages(self, path, key, most=10):
        return self.call(path)[key]


def quiet(fn, *a, **k):
    out = io.StringIO()
    with redirect_stdout(out):
        got = fn(*a, **k)
    return got, out.getvalue()


class Config(unittest.TestCase):
    def test_the_repository_config_is_valid(self):
        self.assertEqual(ff.check_config(CFG), [])

    def test_a_pool_with_only_default_labels_is_refused(self):
        bad = copy.deepcopy(CFG)
        bad['pools'][0]['labels'] = ['self-hosted', 'linux']
        self.assertTrue(any('no label of its own' in p for p in ff.check_config(bad)))

    def test_a_pool_without_a_capacity_decision_is_refused(self):
        bad = copy.deepcopy(CFG)
        del bad['pools'][0]['capacity']
        self.assertTrue(any('capacity' in p for p in ff.check_config(bad)))

    def test_a_fleet_without_a_waiting_pool_is_refused(self):
        bad = copy.deepcopy(CFG)
        for p in bad['pools']:
            p['waits'] = False
        self.assertTrue(any('waits' in p for p in ff.check_config(bad)))

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
        }
        for name, (e, event) in cases.items():
            with self.subTest(name):
                self.assertIsNotNone(ff.refused(CFG, e, event))

    def test_gate_reads_the_event_file_and_refuses_on_trouble(self):
        with tempfile.TemporaryDirectory() as d:
            good = os.path.join(d, 'event.json')
            with open(good, 'w') as f:
                json.dump(PUSH, f)
            self.assertEqual(quiet(ff.gate, CFG, env(GITHUB_EVENT_PATH=good))[0], 0)
            self.assertEqual(quiet(ff.gate, CFG, env(GITHUB_EVENT_PATH=os.path.join(d, 'none')))[0], 1)
            self.assertEqual(quiet(ff.gate, CFG, env(GITHUB_EVENT_PATH=None))[0], 1)
            broken = os.path.join(d, 'broken.json')
            with open(broken, 'w') as f:
                f.write('{')
            self.assertEqual(quiet(ff.gate, CFG, env(GITHUB_EVENT_PATH=broken))[0], 1)

    def test_the_installed_gate_runs_alone_and_crashes_closed(self):
        """A runner copies the script and the config only; a broken config refuses every job."""
        with tempfile.TemporaryDirectory() as d:
            script = os.path.join(d, 'fork-fleet.py')
            with open(os.path.join(HERE, 'fork-fleet.py')) as src, open(script, 'w') as dst:
                dst.write(src.read())
            event = os.path.join(d, 'event.json')
            with open(event, 'w') as f:
                json.dump(PUSH, f)
            conf = os.path.join(d, 'fleet.json')
            with open(conf, 'w') as f:
                json.dump(CFG, f)
            run = lambda c: subprocess.run([sys.executable, '-I', script, '--config', c, 'gate'],
                                           env=env(GITHUB_EVENT_PATH=event), capture_output=True, text=True)
            self.assertEqual(run(conf).returncode, 0)
            with open(conf, 'w') as f:
                f.write('[]')
            got = run(conf)
            self.assertEqual(got.returncode, 1)
            self.assertIn('refused', got.stdout)


class Route(unittest.TestCase):
    def route(self, mode='auto', token='t', gates=None, runners=(), e=None, event=PUSH, gh=None):
        gh = gh or FakeGH({'actions/runners': {'runners': list(runners)}})
        (got_mode, out), log = quiet(ff.route, CFG, e or env(), event, mode, token, gates, gh)
        return got_mode, out, log

    def test_github_mode_is_hosted_and_asks_nothing(self):
        gh = FakeGH(fail=AssertionError('no call'))
        _, out, _ = self.route('github', runners=[runner('vm106')], gh=gh)
        self.assertEqual(set(out.values()), {HOSTED})
        self.assertEqual(gh.calls, [])

    def test_auto_one_idle_runner_takes_the_first_job(self):
        _, out, _ = self.route(runners=[runner('vm106')])
        self.assertEqual(out[KEYS[0]], POOLS['vm106']['labels'])
        self.assertEqual([k for k in KEYS if out[k] != HOSTED], [KEYS[0]])

    def test_auto_pools_in_order(self):
        _, out, _ = self.route(runners=[runner('vm106'), runner('pc')])
        self.assertEqual(out[KEYS[0]], POOLS[CFG['pools'][0]['name']]['labels'])
        self.assertEqual(out[KEYS[1]], POOLS[CFG['pools'][1]['name']]['labels'])
        self.assertEqual(out[KEYS[2]], HOSTED)

    def test_busy_offline_and_unlabelled_runners_are_not_idle(self):
        other = {'status': 'online', 'busy': False, 'labels': [{'name': 'self-hosted'}]}
        _, out, _ = self.route(runners=[runner('vm106', busy=True), runner('pc', status='offline'), other])
        self.assertEqual(set(out.values()), {HOSTED})

    def test_extra_labels_on_a_runner_still_match(self):
        _, out, _ = self.route(runners=[runner('pc', extra=['self-hosted', 'Linux'])])
        self.assertEqual(out[KEYS[0]], POOLS['pc']['labels'])

    def test_a_job_that_does_not_run_gets_no_runner(self):
        gates = {g: 'false' for j in CFG['jobs'] for g in j['gates']}
        gates['rust'] = 'true'
        _, out, _ = self.route(gates=gates, runners=[runner('vm106'), runner('pc')])
        running = [k for k, j in ff.job_keys(CFG) if ff.runs(j, gates)]
        self.assertEqual(running, ['lint', 'rust'])   # lint has no gates: it always runs
        self.assertEqual(out['lint'], POOLS[CFG['pools'][0]['name']]['labels'])
        self.assertEqual(out['rust'], POOLS[CFG['pools'][1]['name']]['labels'])
        self.assertEqual({out[k] for k in KEYS if k not in running}, {HOSTED})

    def test_missing_gates_mean_every_job_runs(self):
        _, out, _ = self.route(gates=None, runners=[runner('vm106')])
        self.assertEqual(out[KEYS[0]], POOLS['vm106']['labels'])

    def test_untrusted_runs_are_hosted_in_every_mode(self):
        for mode in ('auto', 'fleet'):
            with self.subTest(mode):
                gh = FakeGH(fail=AssertionError('no call'))
                _, out, log = self.route(mode, e=env(**PR_ENV), event=pr(head='someone/t3code'), gh=gh)
                self.assertEqual(set(out.values()), {HOSTED})
                self.assertIn('only trusted runs', log)
                self.assertEqual(gh.calls, [])

    def test_auto_without_a_token_is_hosted(self):
        _, out, log = self.route(token='')
        self.assertEqual(set(out.values()), {HOSTED})
        self.assertIn(CFG['token_secret'], log)

    def test_auto_with_github_trouble_is_hosted_with_a_warning(self):
        gh = FakeGH(fail=OSError('timed out'))
        _, out, log = self.route(gh=gh)
        self.assertEqual(set(out.values()), {HOSTED})
        self.assertIn('::warning::', log)

    def test_auto_rerun_is_hosted(self):
        _, out, _ = self.route(e=env(GITHUB_RUN_ATTEMPT='2'), runners=[runner('vm106')])
        self.assertEqual(set(out.values()), {HOSTED})

    def test_fleet_never_goes_hosted_for_a_trusted_run(self):
        for name, kw in {'idle runners': dict(runners=[runner('pc')]), 'no token': dict(token=''),
                         'github trouble': dict(gh=FakeGH(fail=OSError('x'))),
                         'a re-run': dict(e=env(GITHUB_RUN_ATTEMPT='3'))}.items():
            with self.subTest(name):
                mode, out, _ = self.route('fleet', **kw)
                self.assertEqual(mode, 'fleet')
                self.assertNotIn(HOSTED, out.values())
        _, out, _ = self.route('fleet', runners=[runner('pc')])
        self.assertEqual(out[KEYS[0]], POOLS['pc']['labels'])
        self.assertEqual(out[KEYS[1]], WAITING['labels'])

    def test_fleet_leaves_jobs_that_do_not_run_hosted(self):
        gates = {g: 'false' for j in CFG['jobs'] for g in j['gates']}
        _, out, _ = self.route('fleet', token='', gates=gates)
        self.assertEqual(out['lint'], WAITING['labels'])
        self.assertEqual(out['rust'], HOSTED)

    def test_a_broken_setting_fails(self):
        with self.assertRaises(ValueError):
            self.route('flet')

    def test_cmd_route_writes_outputs(self):
        with tempfile.TemporaryDirectory() as d:
            out, summ, event = (os.path.join(d, n) for n in ('out', 'summary', 'event'))
            with open(event, 'w') as f:
                json.dump(PUSH, f)
            e = env(GITHUB_OUTPUT=out, GITHUB_STEP_SUMMARY=summ, GITHUB_EVENT_PATH=event, FLEET_MODE='',
                    FLEET_TOKEN='', GATES='{"test": "false"}')
            self.assertEqual(quiet(ff.cmd_route, CFG, e)[0], 0)
            with open(out) as f:
                lines = dict(line.split('=', 1) for line in f.read().splitlines())
            self.assertEqual(lines['mode'], 'auto')
            self.assertEqual(lines['attempt'], '1')
            self.assertEqual(set(json.loads(lines['runners'])), set(KEYS))
            with open(summ) as f:
                self.assertIn('Runners', f.read())


class Capacity(unittest.TestCase):
    def run_cap(self, e, job='build', codes=(0,), exists=True):
        calls = []
        codes = list(codes)

        def run(cmd, **k):
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, codes.pop(0) if len(codes) > 1 else codes[0])
        clock = [0.0]

        def sleep(s):
            clock[0] += s
        with mock.patch.object(ff.os.path, 'isfile', return_value=exists), \
                mock.patch.object(ff.shutil, 'which', return_value=None):
            got, log = quiet(ff.capacity, CFG, e, job, run=run, sleep=sleep, now=lambda: clock[0])
        return got, calls, log

    def test_hosted_does_nothing(self):
        got, calls, _ = self.run_cap({'RUNNER_ENVIRONMENT': 'github-hosted'})
        self.assertEqual((got, calls), (0, []))

    def test_unknown_fleet_runner_fails(self):
        got, _, log = self.run_cap({'RUNNER_ENVIRONMENT': 'self-hosted', 'RUNNER_NAME': 'stranger'})
        self.assertEqual(got, 1)
        self.assertIn('no pool', log)

    def vm106(self):
        name = WAITING['runner_name'].strip('^$')
        return {'RUNNER_ENVIRONMENT': 'self-hosted', 'RUNNER_NAME': name}

    def test_waits_until_ok_with_the_jobs_memory(self):
        got, calls, _ = self.run_cap(self.vm106(), codes=(1, 1, 0))
        self.assertEqual(got, 0)
        self.assertEqual(len(calls), 3)
        mb = next(j['memory_mb'] for j in CFG['jobs'] if j['job'] == 'build')
        self.assertIn(str(mb), calls[0])

    def test_never_ok_fails_after_the_limit(self):
        got, calls, log = self.run_cap(self.vm106(), codes=(1,))
        self.assertEqual(got, 1)
        wait = CFG['capacity_wait']
        self.assertEqual(len(calls), wait['max_seconds'] // wait['every_seconds'] + 1)
        self.assertIn('NOT OK', log)

    def test_missing_check_fails(self):
        got, _, log = self.run_cap(self.vm106(), exists=False)
        self.assertEqual(got, 1)
        self.assertIn('is not on runner', log)

    def test_a_pool_without_a_check(self):
        cfg = copy.deepcopy(CFG)
        for p in cfg['pools']:
            p['capacity'] = None
        got, _ = quiet(ff.capacity, cfg, self.vm106(), 'build', run=None)
        self.assertEqual(got, 0)

    def test_unknown_job_fails(self):
        got, _, _ = self.run_cap(self.vm106(), job='nope')
        self.assertEqual(got, 1)


T0 = 1_800_000_000


def iso(t):
    return ff.time.strftime('%Y-%m-%dT%H:%M:%SZ', ff.time.gmtime(t))


FLEET_LABELS = list(WAITING['labels'])


def job(name='Lint', labels=FLEET_LABELS, runner_name=None, status='completed', conclusion='cancelled', created=T0,
        completed=T0 + 700, steps=()):
    return {'name': name, 'labels': labels, 'runner_name': runner_name, 'status': status, 'conclusion': conclusion,
            'created_at': iso(created), 'completed_at': iso(completed) if completed else None, 'steps': list(steps)}


def wf_run(jobs, conclusion='cancelled', status='completed', attempt=1, sha='abc', branch='main', rid=5):
    return {'id': rid, 'run_attempt': attempt, 'status': status, 'conclusion': conclusion, 'head_sha': sha,
            'head_branch': branch}, jobs


class Sweep(unittest.TestCase):
    def sweep(self, runs, mode='', head='abc', now=T0 + 800, after_cancel=None):
        posts = []
        by_id = {r['id']: (r, j) for r, j in runs}

        def answer(path):
            def got(method):
                if method == 'POST':
                    posts.append(path)
                    return {}
                if '/attempts/1/jobs' in path:
                    rid = int(path.split('/')[2])
                    if after_cancel and posts:
                        return {'jobs': after_cancel[1]}
                    return {'jobs': by_id[rid][1]}
                if path.startswith('actions/runs/'):
                    return after_cancel[0] if after_cancel else by_id[int(path.split('/')[2])][0]
                if path.startswith('branches/'):
                    return {'commit': {'sha': head}}
                return {'workflow_runs': [r for r, _ in runs]}
            return got
        gh = FakeGH()
        gh.call = lambda path, method='GET', data=None: answer(path)(method)
        clock = [now]

        def sleep(s):
            clock[0] += s
        got, log = quiet(ff.sweep, CFG, {'FLEET_MODE': mode}, gh, now=lambda: clock[0], sleep=sleep)
        return got, posts, log

    def test_a_job_that_waited_too_long_is_re_run(self):
        got, posts, _ = self.sweep([wf_run([job()])])
        self.assertEqual(got, 0)
        self.assertEqual(posts, ['actions/runs/5/rerun'])

    def test_a_stuck_queued_job_cancels_then_re_runs(self):
        queued = job(status='queued', conclusion=None, completed=None)
        done = wf_run([job()])
        got, posts, _ = self.sweep([wf_run([queued], status='in_progress', conclusion=None)], after_cancel=done)
        self.assertEqual(posts, ['actions/runs/5/cancel', 'actions/runs/5/rerun'])

    def test_a_queued_job_not_yet_stuck_is_left(self):
        queued = job(status='queued', conclusion=None, completed=None, created=T0 + 500)
        _, posts, _ = self.sweep([wf_run([queued], status='in_progress', conclusion=None)])
        self.assertEqual(posts, [])

    def test_a_cancel_that_does_not_finish_is_left_for_the_next_sweep(self):
        queued = job(status='queued', conclusion=None, completed=None)
        still = wf_run([queued], status='in_progress', conclusion=None)
        _, posts, log = self.sweep([still], after_cancel=still)
        self.assertEqual(posts, ['actions/runs/5/cancel'])
        self.assertIn('next sweep', log)

    def test_a_moved_branch_is_not_re_run(self):
        _, posts, _ = self.sweep([wf_run([job()])], head='def')
        self.assertEqual(posts, [])

    def test_attempt_two_is_never_touched(self):
        _, posts, _ = self.sweep([wf_run([job()], attempt=2)])
        self.assertEqual(posts, [])

    def test_fleet_mode_moves_nothing(self):
        _, posts, _ = self.sweep([wf_run([job()])], mode='fleet')
        self.assertEqual(posts, [])

    def test_hosted_jobs_are_not_the_sweeps_business(self):
        _, posts, _ = self.sweep([wf_run([job(labels=[HOSTED])])])
        self.assertEqual(posts, [])

    def test_a_person_cancelling_a_running_fleet_job_stands(self):
        started = job(runner_name='vm106-t3code', conclusion='cancelled', completed=T0 + 30)
        _, posts, _ = self.sweep([wf_run([started])])
        self.assertEqual(posts, [])

    def test_a_lost_runner_is_re_run(self):
        lost = job(runner_name='vm106-t3code', conclusion='failure', steps=[{'name': 'Test', 'conclusion': None}])
        _, posts, _ = self.sweep([wf_run([lost], conclusion='failure')])
        self.assertEqual(posts, ['actions/runs/5/rerun'])

    def test_a_memory_shortage_is_re_run(self):
        short = job(runner_name='vm106-t3code', conclusion='failure',
                    steps=[{'name': ff.CAPACITY_STEP, 'conclusion': 'failure'}])
        _, posts, _ = self.sweep([wf_run([short], conclusion='failure')])
        self.assertEqual(posts, ['actions/runs/5/rerun'])

    def test_a_failed_test_stays_red(self):
        red = job(runner_name='vm106-t3code', conclusion='failure', steps=[{'name': 'Test', 'conclusion': 'failure'}])
        _, posts, _ = self.sweep([wf_run([red], conclusion='failure')])
        self.assertEqual(posts, [])

    def test_github_trouble_is_a_warning_and_the_next_sweep_tries_again(self):
        gh = FakeGH({'actions/workflows/': {'workflow_runs': [wf_run([])[0]]}})
        gh.answers['actions/runs/'] = lambda m: (_ for _ in ()).throw(OSError('down'))
        got, log = quiet(ff.sweep, CFG, {}, gh, now=lambda: T0)
        self.assertEqual(got, 1)
        self.assertIn('next sweep', log)


class Wiring(unittest.TestCase):
    """fork-tests.yml and fork-fleet.yml against the config."""

    @classmethod
    def setUpClass(cls):
        import yaml
        with open(os.path.join(ROOT, WORKFLOW)) as f:
            cls.text = f.read()
        cls.tests = yaml.safe_load(cls.text)
        with open(os.path.join(ROOT, '.github/workflows/fork-fleet.yml')) as f:
            cls.sweep = yaml.safe_load(f)

    def expected_runs_on(self, j):
        key = f"format('{j['job']}-{{0}}', matrix.shard)" if j.get('shards') else f"'{j['job']}'"
        return ("${{ (needs.route.outputs.attempt == github.run_attempt || needs.route.outputs.mode == 'fleet') && "
                f"fromJSON(needs.route.outputs.runners || '{{}}')[{key}] || '{HOSTED}' }}}}")

    def test_every_routed_job_uses_the_route(self):
        jobs = self.tests['jobs']
        for j in CFG['jobs']:
            with self.subTest(j['job']):
                spec = jobs[j['job']]
                self.assertEqual(spec['runs-on'], self.expected_runs_on(j))
                self.assertIn('route', spec['needs'])
                shards = (spec.get('strategy') or {}).get('matrix', {}).get('shard')
                self.assertEqual(len(shards) if shards else None, j.get('shards'))
                names = [s.get('name') for s in spec['steps']]
                self.assertEqual(names[0], 'Checkout')
                self.assertEqual(names[1], ff.CAPACITY_STEP)
                self.assertEqual(spec['steps'][1]['run'],
                                 f'python3 -I .github/scripts/fork-fleet.py capacity {j["job"]}')

    def test_no_other_job_can_reach_the_fleet(self):
        routed = {j['job'] for j in CFG['jobs']}
        for name, spec in self.tests['jobs'].items():
            if name in routed:
                continue
            with self.subTest(name):
                self.assertIsInstance(spec['runs-on'], str)
                self.assertTrue(spec['runs-on'] == HOSTED or spec['runs-on'].startswith('macos-'), spec['runs-on'])
        self.assertEqual(self.sweep['jobs']['sweep']['runs-on'], HOSTED)

    def test_the_route_job(self):
        route = self.tests['jobs']['route']
        self.assertEqual(route['runs-on'], HOSTED)
        step = route['steps'][-1]
        self.assertEqual(step['env']['FLEET_MODE'], f'${{{{ vars.{CFG["variable"]} }}}}')
        self.assertEqual(step['env']['FLEET_TOKEN'], f'${{{{ secrets.{CFG["token_secret"]} }}}}')
        self.assertEqual(self.sweep['jobs']['sweep']['steps'][-1]['env']['FLEET_MODE'],
                         f'${{{{ vars.{CFG["variable"]} }}}}')
        self.assertIn('route', self.tests['jobs']['fork_ci_result']['needs'])

    def test_no_routed_job_reads_a_secret(self):
        """A fleet job runs on our machines: it gets no secret (the route job, GitHub-hosted, reads the token)."""
        import yaml
        for j in CFG['jobs']:
            with self.subTest(j['job']):
                self.assertNotIn('secrets.', yaml.safe_dump(self.tests['jobs'][j['job']]))

    def test_no_routed_job_uses_sudo_on_the_fleet(self):
        """A fleet runner has no sudo: every sudo in a routed job is behind a GitHub-hosted check."""
        import yaml
        for j in CFG['jobs']:
            for step in self.tests['jobs'][j['job']]['steps']:
                script = step.get('run') or ''
                if 'sudo ' not in script:
                    continue
                with self.subTest(job=j['job'], step=step.get('name')):
                    self.assertIn('if [[ "$RUNNER_ENVIRONMENT" == self-hosted ]]', script)
                    self.assertLess(script.index('RUNNER_ENVIRONMENT'), script.index('sudo '))


if __name__ == '__main__':
    unittest.main()
