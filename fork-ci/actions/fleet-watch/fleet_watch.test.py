#!/usr/bin/env python3
"""Tests of fleet_watch.py (watch, sweep, pending) against a fake of GitHub's API, and of action.yml's step run as a
runner runs it, with no token. Run: python3 <this>."""
import calendar, contextlib, importlib.util, io, json, os, re, subprocess, sys, tempfile, time, unittest
import urllib.error, urllib.request
from unittest import mock

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('fleet_watch', os.path.join(HERE, 'fleet_watch.py'))
fw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fw)

REPO, OWNER, BOT = 'owner/proj', 'owner', 'github-actions[bot]'
CI = '.github/workflows/ci.yml'
SHA, NEWER_SHA = 'a' * 40, 'b' * 40
T0 = 1_800_000_000
DESK, VM, HOSTED = ['proj-desk', 'proj-desk-plugins-0123456789abcdef-a1'], ['proj-vm'], ['ubuntu-24.04']
TOKENS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN')
CONFIG = {
    'repository': REPO, 'hosted': 'ubuntu-24.04', 'variable': 'PROJ_RUNNERS',
    'trusted': {'workflows': [CI], 'actors': [OWNER], 'push_branches': ['main']},
    'pools': [
        {'name': 'desk', 'label': 'proj-desk', 'slots': 2, 'per_run': 2, 'beacon': 'PROJ_BEACON_DESK',
         'launch': ['launch-desk'], 'waits': False},
        {'name': 'vm', 'label': 'proj-vm', 'slots': 2, 'beacon': 'PROJ_BEACON_VM', 'launch': ['launch-vm'],
         'waits': True}],
    'beacon_fresh_seconds': 180, 'stuck_seconds': 600, 'fleet_stuck_seconds': 7200,
    'dispatch': {'every_seconds': 20, 'beacon_seconds': 60, 'idle_passes': 3},
    'jobs': [{'job': 'plugins', 'memory_mb': 4000}, {'job': 'engine', 'memory_mb': 3000}],
    'watch': {'followers': ['fleet_watch', 'required'], 'errors': ['No space left on device']},
}
GOOD_STEPS = [{'name': 'Set up job', 'conclusion': 'success'}, {'name': 'Build', 'conclusion': 'success'}]


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def http_error(code):
    return urllib.error.HTTPError('https://api.github.test/x', code, 'no', None, None)


def a_run(rid=5, *, actor=OWNER, triggering=None, event='push', branch='main', attempt=1, status='completed',
          conclusion='failure', head_repo=REPO, repo=REPO, path=CI, sha=SHA, ended=T0 + 900):
    return {'id': rid, 'run_attempt': attempt, 'status': status, 'conclusion': conclusion, 'event': event,
            'head_branch': branch, 'head_sha': sha, 'path': path, 'repository': {'full_name': repo},
            'head_repository': {'full_name': head_repo}, 'actor': {'login': actor},
            'triggering_actor': {'login': triggering or actor}, 'created_at': iso(T0), 'updated_at': iso(ended)}


def a_job(jid, labels, *, name=None, status='completed', runner='runner-1', conclusion='success', created=T0,
          completed=T0 + 900, steps=GOOD_STEPS):
    return {'id': jid, 'name': name or f'job {jid}', 'labels': list(labels), 'status': status, 'runner_name': runner,
            'conclusion': conclusion, 'created_at': iso(created), 'completed_at': iso(completed) if completed else None,
            'steps': list(steps)}


def failed_in(step):
    return [{'name': 'Set up job', 'conclusion': 'success' if step != 'Set up job' else 'failure'},
            {'name': step, 'conclusion': 'failure'}]


class FakeGitHub:
    """This repository as GitHub's API shows it: runs, each attempt's jobs, job logs. Records every call; `trouble` is
    [(regex of 'METHOD path', exception)]. A re-run or a cancel changes the run as GitHub would."""

    def __init__(self, now=T0 + 1000):
        self.runs, self.jobs, self.logs, self.calls, self.trouble, self.now = {}, {}, {}, [], [], now

    def add(self, run, *jobs):
        self.runs[run['id']] = run
        self.jobs[(run['id'], run['run_attempt'])] = list(jobs)
        return run

    def writes(self):
        return [(m, p) for m, p in self.calls if m != 'GET']

    def pages(self, path, key, most=10):
        return self.call(f'{path}{"&" if "?" in path else "?"}per_page=100&page=1')[key]

    def log(self, job_id):
        self.calls.append(('GET', f'actions/jobs/{job_id}/logs'))
        got = self.logs[job_id]
        if isinstance(got, Exception):
            raise got
        return got

    def call(self, path, method='GET', data=None):
        self.calls.append((method, path))
        for match, error in self.trouble:
            if re.fullmatch(match, f'{method} {path}'):
                raise error
        if m := re.fullmatch(r'actions/runs/(\d+)', path):
            return dict(self.runs[int(m[1])])
        if m := re.fullmatch(r'actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100&page=1', path):
            return {'jobs': self.jobs.get((int(m[1]), int(m[2])), [])}
        if m := re.fullmatch(r'actions/workflows/([\w.-]+)/runs\?(.*)', path):
            q = dict(x.split('=', 1) for x in m[2].split('&'))
            got = [r for r in sorted(self.runs.values(), key=lambda r: -r['id']) if r['path'].endswith('/' + m[1])
                   and (r['head_branch'] == q.get('branch', r['head_branch']))
                   and r['event'] == q.get('event', r['event']) and r['status'] == q.get('status', r['status'])]
            return {'workflow_runs': got[:int(q['per_page'])]}
        run = self.runs[int(re.fullmatch(r'actions/runs/(\d+)/[a-z-]+', path)[1])]
        if path.endswith('/rerun-failed-jobs') and method == 'POST':
            if run['status'] != 'completed':
                raise http_error(403)
            run.update(run_attempt=run['run_attempt'] + 1, triggering_actor={'login': BOT}, status='queued',
                       conclusion=None)
            return {}
        if path.endswith('/cancel') and method == 'POST':
            if run['status'] == 'completed':
                raise http_error(409)
            run.update(status='completed', conclusion='cancelled', updated_at=iso(self.now))
            for j in self.jobs[(run['id'], run['run_attempt'])]:
                if j['status'] != 'completed':
                    j.update(status='completed', conclusion='cancelled', completed_at=iso(self.now))
            return {}
        raise AssertionError(f'unexpected call {method} {path}')


def quiet(fn, *a, **k):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        got = fn(*a, **k)
    return got, out.getvalue()


def watch(gh, rid=5, mode='auto', now=None):
    return quiet(fw.watch, CONFIG, gh, gh.call(f'actions/runs/{rid}'), mode, now or gh.now)


def cut_run(gh, rid=5, **kw):
    """A red run whose desk job's runner stopped (no failed step) while a hosted job passed."""
    return gh.add(a_run(rid, **kw), a_job(rid * 10 + 1, DESK, name='plugins', conclusion='failure',
                                         steps=[{'name': 'Set up job', 'conclusion': 'success'},
                                                {'name': 'Build', 'conclusion': None}]),
                  a_job(rid * 10 + 2, HOSTED, name='lint'))


class Watch(unittest.TestCase):
    def test_a_desk_job_whose_runner_stopped_is_re_run_once(self):
        gh = FakeGitHub()
        cut_run(gh)
        got, out = watch(gh)
        self.assertTrue(got)
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/rerun-failed-jobs')])
        self.assertIn('no failed step', out)
        # The re-run is the bot's: it ends red again, and a second watch (or the same event again) leaves it.
        gh.runs[5].update(status='completed', conclusion='failure')
        gh.jobs[(5, 2)] = gh.jobs[(5, 1)]
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertEqual(len(gh.writes()), 1)
        self.assertIn("triggering_actor is 'github-actions[bot]'", out)

    def test_each_way_a_desk_runner_cuts_a_job_off(self):
        ways = {
            'waited': dict(runner=None, conclusion='cancelled', completed=T0 + 600, steps=()),
            'set up': dict(steps=failed_in('Set up job')[:1]),
            'gate': dict(steps=failed_in('Set up runner')),
            'no step': dict(steps=[{'name': 'Build', 'conclusion': 'cancelled'}]),
        }
        for way, kw in ways.items():
            with self.subTest(way):
                gh = FakeGitHub()
                kw = {'conclusion': 'failure', **kw}
                gh.add(a_run(conclusion='cancelled' if way == 'waited' else 'failure'),
                       a_job(51, DESK, name='plugins', **kw))
                self.assertTrue(watch(gh)[0])
                self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/rerun-failed-jobs')])
                self.assertNotIn(('GET', 'actions/jobs/51/logs'), gh.calls)

    def test_a_job_that_did_not_wait_long_enough_or_was_cancelled_while_it_ran_stands(self):
        cases = {
            'short wait': (dict(runner=None, conclusion='cancelled', completed=T0 + 599, steps=()), 'cancelled'),
            'vm, fleet mode': (dict(runner=None, conclusion='cancelled', completed=T0 + 3600, steps=()), 'cancelled'),
            'cancelled while it ran': (dict(conclusion='cancelled'), 'cancelled'),
            'run cancelled': (dict(conclusion='failure', steps=()), 'cancelled'),
        }
        for case, (kw, run_conclusion) in cases.items():
            with self.subTest(case):
                gh = FakeGitHub()
                gh.add(a_run(conclusion=run_conclusion), a_job(51, VM if 'vm' in case else DESK, **kw))
                self.assertFalse(watch(gh, mode='fleet' if 'fleet' in case else 'auto')[0])
                self.assertEqual(gh.writes(), [])

    def test_the_pool_that_waits_is_cut_off_after_fleet_stuck_seconds_in_fleet_mode(self):
        gh = FakeGitHub(now=T0 + 8000)
        gh.add(a_run(ended=T0 + 7300), a_job(51, VM, runner=None, conclusion='cancelled', completed=T0 + 7200,
                                              steps=()))
        self.assertTrue(watch(gh, mode='fleet')[0])

    def test_a_job_that_failed_by_itself_keeps_the_red(self):
        cases = {
            'hosted job': a_job(52, HOSTED, name='lint', conclusion='failure', steps=failed_in('Lint')),
            'vm job': a_job(52, VM, name='engine', conclusion='timed_out', steps=failed_in('Test')),
        }
        for case, own in cases.items():
            with self.subTest(case):
                gh = FakeGitHub()
                gh.add(a_run(), a_job(51, DESK, name='plugins', conclusion='failure', steps=()), own)
                gh.logs[52] = '##[error]Process completed with exit code 1.'
                got, out = watch(gh)
                self.assertFalse(got)
                self.assertEqual(gh.writes(), [])
                self.assertIn('failed by itself', out)

    def test_followers_do_not_keep_the_red(self):
        gh = FakeGitHub()
        gh.add(a_run(), a_job(51, DESK, conclusion='failure', steps=()),
               a_job(52, HOSTED, name='required', conclusion='failure', steps=failed_in('All passed')),
               a_job(53, HOSTED, name='fleet_watch (wait)', conclusion='failure', steps=failed_in('Watch')))
        self.assertTrue(watch(gh)[0])

    def test_a_hosted_or_always_on_runner_that_stopped_is_not_this_scripts_to_re_run(self):
        gh = FakeGitHub()
        gh.add(a_run(), a_job(51, HOSTED, conclusion='failure', steps=()))
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('no job was cut off', out)

    def test_the_log_decides_a_desk_job_that_failed_in_a_step(self):
        cases = {
            'killed': ('##[error]Process completed with exit code 137.', True),
            'config error': ('x\n2026-01-01T00:00:00Z ##[error]No space left on device\n', True),
            'its own': ('##[error]Process completed with exit code 1.', False),
            'no error line': ('Process completed with exit code 137.', False),
            'unreadable': (http_error(404), False),
            'garbled': (ValueError('x'), False),
        }
        for case, (log, rerun) in cases.items():
            with self.subTest(case):
                gh = FakeGitHub()
                gh.add(a_run(), a_job(51, DESK, conclusion='failure', steps=failed_in('Build')))
                gh.logs[51] = log
                got, out = watch(gh)
                self.assertEqual(got, rerun, out)
                self.assertEqual(len(gh.writes()), int(rerun))

    def test_logs_are_read_only_when_nothing_else_decides(self):
        gh = FakeGitHub()
        gh.add(a_run(), a_job(51, DESK, conclusion='failure', steps=failed_in('Build')),
               a_job(52, HOSTED, conclusion='failure', steps=failed_in('Lint')))
        watch(gh)
        self.assertFalse([c for c in gh.calls if c[1].endswith('/logs')])
        gh = FakeGitHub()
        gh.add(a_run(), *[a_job(50 + i, DESK, conclusion='failure', steps=failed_in('Build')) for i in range(3)])
        gh.logs.update({50: '##[error]own', 51: '##[error]own', 52: '##[error]own'})
        watch(gh)
        self.assertEqual([c[1] for c in gh.calls if c[1].endswith('/logs')], ['actions/jobs/50/logs'])

    def test_runs_it_must_not_touch_are_left_after_one_read(self):
        cases = {
            'other workflow': dict(path='.github/workflows/deploy.yml'),
            'other repository': dict(repo='someone/else'),
            'fork pull request': dict(event='pull_request', head_repo='someone/proj', branch='x'),
            'schedule': dict(event='schedule'),
            'push to another branch': dict(branch='topic'),
            'untrusted actor': dict(actor='someone'),
            'bot re-run': dict(triggering=BOT),
            'attempt 3': dict(attempt=3),
            'still running': dict(status='in_progress', conclusion=None),
            'old': dict(ended=T0 + 900 - 25 * 3600),
        }
        for case, kw in cases.items():
            with self.subTest(case):
                gh = FakeGitHub()
                cut_run(gh, **kw)
                got, out = watch(gh)
                self.assertFalse(got)
                self.assertEqual(gh.calls, [('GET', 'actions/runs/5')], out)

    def test_a_trusted_pull_request_and_a_persons_second_attempt_are_re_run(self):
        for case, kw in {'pull request': dict(event='pull_request', branch='topic'), 'attempt 2': dict(attempt=2)}.items():
            with self.subTest(case):
                gh = FakeGitHub()
                cut_run(gh, **kw)
                self.assertTrue(watch(gh)[0])

    def test_a_branch_with_a_newer_commit_is_not_re_run(self):
        gh = FakeGitHub()
        cut_run(gh)
        gh.add(a_run(6, status='in_progress', conclusion=None, sha=NEWER_SHA))
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('a newer run', out)
        gh = FakeGitHub()
        cut_run(gh)
        gh.add(a_run(6, conclusion='success'))   # the same commit again (a manual run): this one still counts
        self.assertTrue(watch(gh)[0])

    def test_a_re_run_someone_else_started_first_is_not_an_error(self):
        gh = FakeGitHub()
        cut_run(gh)
        run = gh.call('actions/runs/5')
        quiet(fw.rerun, gh, run, [])
        got, out = quiet(fw.rerun, gh, run, [])
        self.assertFalse(got)
        self.assertIn('already going', out)
        gh = FakeGitHub()
        cut_run(gh)
        gh.trouble = [(r'POST .*/rerun-failed-jobs', http_error(403))]
        with self.assertRaises(urllib.error.HTTPError):
            quiet(fw.rerun, gh, gh.call('actions/runs/5'), [])

    def test_a_stop_half_way_is_finished_by_the_next_watch(self):
        gh = FakeGitHub()
        cut_run(gh)
        gh.trouble = [(r'POST .*', http_error(502))]
        with self.assertRaises(urllib.error.HTTPError):
            watch(gh)
        gh.trouble = []
        self.assertTrue(watch(gh)[0])


class Sweep(unittest.TestCase):
    def sweep(self, gh, mode='auto'):
        notes = []
        got, out = quiet(fw.sweep, CONFIG, gh, mode, gh.now, notes)
        return got, out, notes

    def waiting(self, gh, rid=5, labels=DESK, created=T0, **kw):
        return gh.add(a_run(rid, status='in_progress', conclusion=None, **kw),
                      a_job(rid * 10 + 1, labels, status='queued', runner=None, conclusion=None, created=created,
                            completed=None, steps=()),
                      a_job(rid * 10 + 2, HOSTED, status='in_progress', conclusion=None, completed=None))

    def test_a_run_waiting_too_long_is_cancelled_then_re_run_once(self):
        gh = FakeGitHub(now=T0 + 600)
        self.waiting(gh)
        got, out, notes = self.sweep(gh)
        self.assertEqual(got, 0)
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/cancel')])
        self.assertIn('waited 10 min for pool desk', notes[0])
        # Its watch did not run: the next sweep re-runs it, and the one after does nothing.
        self.sweep(gh)
        self.sweep(gh)
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/cancel'), ('POST', 'actions/runs/5/rerun-failed-jobs')])

    def test_a_run_not_yet_waiting_too_long_or_not_trusted_is_left(self):
        cases = {
            'short wait': dict(created=T0 + 1),
            'bot re-run': dict(triggering=BOT),
            'other repository': dict(repo='someone/else'),
        }
        for case, kw in cases.items():
            with self.subTest(case):
                gh = FakeGitHub(now=T0 + 600)
                self.waiting(gh, **kw)
                self.sweep(gh)
                self.assertEqual(gh.writes(), [])

    def test_fleet_mode_waits_longer_on_the_pool_that_waits(self):
        gh = FakeGitHub(now=T0 + 7199)
        self.waiting(gh, labels=VM)
        self.sweep(gh, mode='fleet')
        self.assertEqual(gh.writes(), [])
        gh.now = T0 + 7200
        self.sweep(gh, mode='fleet')
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/cancel')])

    def test_a_run_that_ended_while_listed_is_left_to_its_watch(self):
        gh = FakeGitHub(now=T0 + 600)
        self.waiting(gh)
        gh.trouble = [(r'POST .*/cancel', http_error(409))]
        got, _, notes = self.sweep(gh)
        self.assertEqual((got, notes), (0, []))

    def test_catch_up_is_bounded_to_the_last_hour(self):
        gh = FakeGitHub(now=T0 + 900 + 3601)
        cut_run(gh)
        self.sweep(gh)
        self.assertEqual(gh.writes(), [])
        gh.now = T0 + 900 + 3600
        self.sweep(gh)
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/rerun-failed-jobs')])

    def test_only_the_configured_workflows_are_listed(self):
        gh = FakeGitHub()
        cut_run(gh, path='.github/workflows/deploy.yml')
        self.sweep(gh)
        self.assertEqual(gh.writes(), [])
        self.assertTrue(all('/ci.yml/' in p for _, p in gh.calls), gh.calls)

    def test_trouble_with_one_run_does_not_stop_the_others(self):
        gh = FakeGitHub()
        cut_run(gh, 5)
        cut_run(gh, 6, branch='main', sha=SHA)
        gh.trouble = [(r'GET actions/runs/6/attempts/.*', http_error(500))]
        got, out, notes = self.sweep(gh)
        self.assertEqual(got, 1)
        self.assertIn('the next sweep tries again', out)
        self.assertEqual(gh.writes(), [('POST', 'actions/runs/5/rerun-failed-jobs')])

    def test_an_empty_repository_is_quiet(self):
        gh = FakeGitHub()
        self.assertEqual(self.sweep(gh)[0], 0)
        self.assertEqual(gh.writes(), [])


class Budget(unittest.TestCase):
    def test_a_sweep_stops_at_the_call_budget(self):
        runs = {'workflow_runs': [a_run(i, status='in_progress', conclusion=None) for i in range(1, 100)]}
        calls = []

        def call(self, path, method='GET', data=None):
            calls.append(path)
            return runs if '/workflows/' in path else {'jobs': []}
        with mock.patch.object(fw.ff.GitHub, 'call', call):
            gh = fw.GitHub(None, REPO, most=20)
            with self.assertRaises(fw.Spent):
                quiet(fw.sweep, CONFIG, gh, 'auto', T0, [])
        self.assertEqual(len(calls), 20)

    def test_a_logs_redirect_never_gets_the_token(self):
        sent = []

        class Opener:
            def open(self, req, timeout):
                sent.append(dict(req.header_items()))
                raise urllib.error.HTTPError(req.full_url, 302, 'Found', {'Location': 'https://blob.test/log'}, None)

        def urlopen(req, timeout):
            sent.append(dict(req.header_items()))
            return contextlib.nullcontext(io.BytesIO(b'##[error]x'))
        with mock.patch.object(urllib.request, 'build_opener', lambda *h: Opener()), \
                mock.patch.object(urllib.request, 'urlopen', urlopen):
            self.assertEqual(fw.GitHub('tok', REPO).log(51), '##[error]x')
        self.assertEqual(sent[0].get('Authorization'), 'Bearer tok')
        self.assertNotIn('Authorization', sent[1])


class Pending(unittest.TestCase):
    def pending(self, gh, attempt=1):
        return quiet(fw.pending, CONFIG, gh, 5, attempt, 'auto', gh.now)[0]

    def test_a_cut_off_attempt_is_pending_until_its_re_run_decides(self):
        gh = FakeGitHub()
        cut_run(gh)
        self.assertEqual(self.pending(gh)[0], True)
        watch(gh)
        self.assertEqual(self.pending(gh)[0], True)   # attempt 2 runs: it decides
        gh.runs[5].update(status='completed', conclusion='failure')
        self.assertEqual(self.pending(gh, 2)[0], False)   # the re-run's red is reported

    def test_anything_else_is_not_pending(self):
        cases = {
            'own failure': lambda gh: gh.add(a_run(), a_job(51, HOSTED, conclusion='failure', steps=failed_in('x'))),
            'unreadable log': lambda gh: (gh.add(a_run(), a_job(51, DESK, conclusion='failure',
                                                                 steps=failed_in('x'))),
                                          gh.logs.update({51: http_error(500)})),
            'untrusted': lambda gh: cut_run(gh, actor='someone'),
            'newer commit': lambda gh: (cut_run(gh), gh.add(a_run(6, sha=NEWER_SHA, conclusion='success'))),
        }
        for case, make in cases.items():
            with self.subTest(case):
                gh = FakeGitHub()
                make(gh)
                self.assertEqual(self.pending(gh)[0], False)
                self.assertEqual(gh.writes(), [])


class Config(unittest.TestCase):
    def test_a_broken_watch_section_is_refused(self):
        for bad in ([], {'followers': 'x'}, {'followers': ['']}, {'errors': [1]}, {'errors': ['(']}):
            with self.subTest(bad), self.assertRaises(ValueError):
                fw.settings(dict(CONFIG, watch=bad))
        self.assertEqual(fw.settings({k: v for k, v in CONFIG.items() if k != 'watch'}), ([], fw.MACHINE))


class Action(unittest.TestCase):
    """action.yml's step and the script's command line, as a runner runs them: bash -e, and no token at all."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.config = os.path.join(self.dir, 'fleet.json')
        with open(self.config, 'w') as f:
            json.dump(CONFIG, f)
        self.output = os.path.join(self.dir, 'output')
        open(self.output, 'w').close()

    def run_step(self, **inputs):
        with open(os.path.join(HERE, 'action.yml')) as f:
            step = yaml.safe_load(f)['runs']['steps'][0]
        env = {k: v for k, v in os.environ.items() if k not in TOKENS}
        values = {'config': self.config, 'verb': '', 'run-id': '', 'attempt': '', 'vars': '{}', 'token': '', **inputs}
        for name, expr in step['env'].items():
            env[name] = values[re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', expr)[1]]
        env.update(GITHUB_ACTION_PATH=HERE, GITHUB_OUTPUT=self.output, GITHUB_API_URL='http://127.0.0.1:9')
        return subprocess.run(['bash', '-e', '-c', step['run']], env=env, capture_output=True, text=True, timeout=60)

    def test_no_token_is_red_and_never_pending(self):
        for verb in ('pending', 'watch', 'sweep'):
            with self.subTest(verb):
                got = self.run_step(verb=verb, **{'run-id': '5', 'attempt': '1'})
                self.assertNotEqual(got.returncode, 0, got.stdout)
                with open(self.output) as f:
                    self.assertNotIn('pending=true', f.read())

    def test_bad_inputs_are_refused(self):
        for inputs in ({'verb': 'rerun'}, {'verb': 'watch', 'run-id': '5; x'}, {'verb': 'pending', 'run-id': '5'}):
            with self.subTest(inputs):
                got = self.run_step(**inputs)
                self.assertNotEqual(got.returncode, 0)
                self.assertRegex(got.stdout, r'::error::fleet')

    def test_main_writes_its_output(self):
        gh = FakeGitHub()
        cut_run(gh)
        code, _ = quiet(fw.main, ['--config', self.config, 'pending', '--run-id', '5', '--attempt', '1'],
                        {'GITHUB_OUTPUT': self.output}, gh, gh.now)
        self.assertEqual(code, 0)
        code, _ = quiet(fw.main, ['--config', self.config, 'watch', '--run-id', '5'], {'GITHUB_OUTPUT': self.output},
                        gh, gh.now)
        with open(self.output) as f:
            self.assertEqual(f.read(), 'pending=true\nrerun=true\n')

    def test_the_example_caller_asks_for_actions_write_only(self):
        with open(os.path.join(HERE, 'fleet-watch-caller.example.yml')) as f:
            workflow = yaml.safe_load(f)
        self.assertEqual(workflow['permissions'], {})
        self.assertEqual(workflow['jobs']['watch']['permissions'], {'actions': 'write', 'contents': 'read'})


if __name__ == '__main__':
    for name in TOKENS:
        os.environ.pop(name, None)
    unittest.main()
