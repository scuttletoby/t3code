#!/usr/bin/env python3
"""Tests of fleet_watch.py (watch, sweep, pending) against a fake of GitHub's API, and of action.yml's step run as a
runner runs it, with no token. Run: python3 <this>."""
import calendar, contextlib, importlib.util, io, json, os, re, subprocess, sys, tempfile, time, unittest
import urllib.error, urllib.parse, urllib.request
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
        out = []
        for page in range(1, most + 1):
            got = self.call(f'{path}{"&" if "?" in path else "?"}per_page=100&page={page}')[key]
            out += got
            if len(got) < 100:
                break
        return out

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
        if m := re.fullmatch(r'actions/jobs/(\d+)', path):
            return dict(next(j for js in self.jobs.values() for j in js if j['id'] == int(m[1])))
        if m := re.fullmatch(r'actions/runs/(\d+)/attempts/(\d+)/jobs\?per_page=100&page=(\d+)', path):
            page = int(m[3])
            return {'jobs': self.jobs.get((int(m[1]), int(m[2])), [])[(page - 1) * 100:page * 100]}
        if m := re.fullmatch(r'actions/workflows/([\w.-]+)/runs\?(.*)', path):
            q = dict(x.split('=', 1) for x in m[2].split('&'))
            since = urllib.parse.unquote(q.get('created', '')).removeprefix('>=')
            got = [r for r in sorted(self.runs.values(), key=lambda r: -r['id']) if r['path'].endswith('/' + m[1])
                   and (r['head_branch'] == q.get('branch', r['head_branch']))
                   and r['event'] == q.get('event', r['event']) and r['status'] == q.get('status', r['status'])
                   and r['created_at'] >= since]
            per, page = int(q['per_page']), int(q.get('page', 1))
            return {'workflow_runs': got[(page - 1) * per:page * per]}
        run = self.runs[int(re.fullmatch(r'actions/runs/(\d+)/[a-z-]+', path)[1])]
        if path.endswith('/rerun-failed-jobs') and method == 'POST':
            if run['status'] != 'completed':
                raise http_error(403)
            old = self.jobs.get((run['id'], run['run_attempt']), [])
            run.update(run_attempt=run['run_attempt'] + 1, triggering_actor={'login': BOT}, status='queued',
                       conclusion=None)
            # GitHub's new attempt: the red jobs again, queued on the fallback; the jobs that passed, as they were.
            self.jobs[(run['id'], run['run_attempt'])] = [
                dict(j, id=j['id'] + 1000, labels=list(HOSTED), status='queued', runner_name=None, conclusion=None,
                     completed_at=None, steps=[]) if j['conclusion'] != 'success' else dict(j) for j in old]
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
        self.assertEqual([j['status'] for j in gh.jobs[(5, 2)]], ['queued', 'completed'])
        gh.runs[5].update(status='completed', conclusion='failure')
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
            'run cancelled': (dict(conclusion='cancelled', steps=[{'name': 'Build', 'conclusion': 'cancelled'}]),
                              'cancelled'),
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
        quiet(fw.rerun, CONFIG, gh, run, [])
        got, out = quiet(fw.rerun, CONFIG, gh, run, [])
        self.assertFalse(got)
        self.assertIn('attempt 2 started since it was read', out)
        self.assertEqual(len(gh.writes()), 1)

        class Late(FakeGitHub):   # the other re-run lands between the read and the POST: GitHub refuses this one
            def call(self, path, method='GET', data=None):
                if method == 'POST' and self.runs[5]['run_attempt'] == 1:
                    super().call(path, method, data)
                    raise http_error(403)
                return super().call(path, method, data)
        gh = Late()
        cut_run(gh)
        got, out = quiet(fw.rerun, CONFIG, gh, gh.call('actions/runs/5'), [])
        self.assertFalse(got)
        self.assertIn('already going', out)
        gh = FakeGitHub()
        cut_run(gh)
        gh.trouble = [(r'POST .*/rerun-failed-jobs', http_error(403))]
        with self.assertRaises(urllib.error.HTTPError):
            quiet(fw.rerun, CONFIG, gh, gh.call('actions/runs/5'), [])

    def test_the_run_is_read_again_just_before_the_re_run(self):
        # A bot attempt 2 that ended red while this one planned (here: at its newest-run check) gets no attempt 3.
        class Race(FakeGitHub):
            def call(self, path, method='GET', data=None):
                if method == 'GET' and '?branch=' in path and self.runs[5]['run_attempt'] == 1:
                    self.runs[5] = dict(self.runs[5], run_attempt=2, triggering_actor={'login': BOT},
                                        status='completed', conclusion='failure')
                return super().call(path, method, data)
        ways = {'watch': lambda gh: watch(gh), 'sweep': lambda gh: quiet(fw.sweep, CONFIG, gh, 'auto', gh.now, [])}
        for case, run in ways.items():
            with self.subTest(case):
                gh = Race()
                cut_run(gh)
                run(gh)
                self.assertEqual(gh.runs[5]['run_attempt'], 2)
                self.assertEqual(gh.writes(), [])

    def test_a_job_that_hit_its_own_timeout_is_its_own_failure(self):
        gh = FakeGitHub()
        gh.add(a_run(), a_job(51, DESK, conclusion='timed_out', steps=[{'name': 'Test', 'conclusion': 'cancelled'}]))
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('failed by itself', out)
        self.assertEqual(gh.writes(), [])

    def test_a_job_that_failed_by_itself_in_a_run_the_sweep_cancelled_keeps_the_red(self):
        gh = FakeGitHub()
        gh.add(a_run(conclusion='cancelled'),
               a_job(51, DESK, runner=None, conclusion='cancelled', completed=T0 + 700, steps=()),
               a_job(52, HOSTED, name='lint', conclusion='failure', steps=failed_in('Lint')))
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('lint failed by itself', out)
        self.assertEqual(gh.writes(), [])

    def test_a_run_with_more_jobs_than_are_read_is_unsure(self):
        gh = FakeGitHub()
        gh.add(a_run(), *[a_job(1000 + i, HOSTED) for i in range(300)],
               a_job(51, DESK, conclusion='failure', steps=()))
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('not all read', out)

    def test_a_run_created_more_than_a_day_ago_is_not_re_run(self):
        # lens233cr1 N1: a person re-runs a 2-day-old run today and the desk runner stops. A re-run (attempt 3) would
        # end red where the sweep's reds cannot see it; so it is not re-run, and attempt 2's red is reported.
        gh = FakeGitHub()
        run = cut_run(gh, attempt=2)
        run['created_at'] = iso(T0 - 2 * 86400)
        got, out = watch(gh)
        self.assertFalse(got)
        self.assertIn('created or ended more than 24 hours ago', out)
        self.assertEqual(gh.writes(), [])
        self.assertFalse(quiet(fw.pending, gh, 5, 2)[0][0])

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

    def test_catch_up_re_runs_only_what_the_jobs_alone_decide(self):
        gh = FakeGitHub()
        gh.add(a_run(), a_job(51, DESK, conclusion='failure', steps=failed_in('Build')))
        gh.logs[51] = '##[error]Process completed with exit code 137.'
        _, out, _ = self.sweep(gh)
        self.assertEqual(gh.writes(), [])
        self.assertNotIn(('GET', 'actions/jobs/51/logs'), gh.calls)
        self.assertIn('left to its watch', out)

    def test_a_job_a_runner_took_since_it_was_listed_is_not_cancelled(self):
        class Race(FakeGitHub):   # the pool's runner picks the late job up while the sweep reads the run again
            def call(self, path, method='GET', data=None):
                if method == 'GET' and path == 'actions/runs/5':
                    self.jobs[(5, 1)][0].update(status='in_progress', runner_name='runner-1')
                return super().call(path, method, data)
        gh = Race(now=T0 + 600)
        self.waiting(gh)
        got, out, notes = self.sweep(gh)
        self.assertEqual((got, notes, gh.writes()), (0, [], []))
        self.assertIn('a runner took its late jobs', out)
        self.assertIn(('GET', 'actions/jobs/51'), gh.calls)

    def test_a_run_that_changed_since_it_was_listed_is_not_cancelled(self):
        class Race(FakeGitHub):   # a person re-runs it while the sweep reads its jobs
            def call(self, path, method='GET', data=None):
                if '/attempts/1/jobs' in path:
                    self.runs[5] = dict(self.runs[5], run_attempt=2)
                    self.jobs[(5, 2)] = []
                return super().call(path, method, data)
        gh = Race(now=T0 + 600)
        self.waiting(gh)
        self.sweep(gh)
        self.assertEqual(gh.writes(), [])

    def test_red_attempts_of_its_own_re_runs_are_listed_for_the_report(self):
        gh = FakeGitHub(now=T0 + 1000)
        cut_run(gh)
        self.sweep(gh)   # catch-up: attempt 2, the bot's
        gh.runs[5].update(status='completed', conclusion='failure', updated_at=iso(T0 + 990))
        reds = []
        quiet(fw.sweep, CONFIG, gh, 'auto', gh.now, [], reds)
        self.assertEqual(reds, [{'run_id': 5, 'attempt': 2}])
        self.assertEqual(len(gh.writes()), 1)   # and it is never re-run again
        cases = {
            'an hour later': (dict(), gh.now + 3600),
            'passed': (dict(conclusion='success'), gh.now),
            'a person re-ran it': (dict(triggering_actor={'login': OWNER}), gh.now),
            'cancelled (a person or a newer push)': (dict(conclusion='cancelled'), gh.now),
            'other branch': (dict(head_branch='topic'), gh.now),
        }
        for case, (change, now) in cases.items():
            with self.subTest(case):
                other = FakeGitHub(now=now)
                other.add(dict(gh.runs[5], **change))
                reds = []
                quiet(fw.sweep, CONFIG, other, 'auto', now, [], reds)
                self.assertEqual(reds, [])

    def bot_red_run(self, gh, rid=5, created=T0, ended=None):
        run = gh.add(a_run(rid, attempt=2, triggering=BOT, ended=ended or gh.now - 10))
        run['created_at'] = iso(created)
        return run

    def test_reds_reaches_every_re_run_the_watch_could_have_made(self):
        # lens233cr1 N1: page 1 of the runs created in the last 24 hours missed these.
        cases = {
            'behind 150 newer runs': (T0 + 1000, 150),
            'created 25 hours ago, re-run 23 hours later': (T0 + 25 * 3600, 0),
        }
        for case, (now, newer) in cases.items():
            with self.subTest(case):
                gh = FakeGitHub(now=now)
                self.bot_red_run(gh)
                for i in range(newer):
                    gh.add(a_run(100 + i, conclusion='success'))
                reds = []
                got, out = quiet(fw.sweep, CONFIG, gh, 'auto', gh.now, [], reds)
                self.assertEqual((got, reds), (0, [{'run_id': 5, 'attempt': 2}]), out)
                self.assertEqual(gh.writes(), [])

    def test_runs_it_cannot_all_list_are_an_error(self):
        gh = FakeGitHub()
        for i in range(100):
            gh.add(a_run(100 + i, conclusion='success'))
        with mock.patch.object(fw, 'RUN_PAGES', 1):
            got, out, _ = self.sweep(gh)
        self.assertEqual(got, 1)
        self.assertRegex(out, r'::error::fleet-watch: .*not all read: reds may miss')

    def test_a_re_run_still_running_past_the_reach_of_reds_is_an_error(self):
        gh = FakeGitHub(now=T0 + fw.WINDOW + fw.LONGEST + 1)
        run = self.bot_red_run(gh)
        run.update(status='in_progress', conclusion=None)
        got, out, _ = self.sweep(gh)
        self.assertEqual(got, 1)
        self.assertIn('report its red by hand', out)
        gh.now -= 2
        self.assertEqual(self.sweep(gh)[0], 0)

    def test_each_sweep_starts_at_another_workflow(self):
        cfg = dict(CONFIG, trusted=dict(CONFIG['trusted'], workflows=[CI, '.github/workflows/other.yml']))
        firsts = set()
        for ms in range(2):
            gh = FakeGitHub()
            quiet(fw.sweep, cfg, gh, 'auto', T0 + ms / 1000, [])
            firsts.add(gh.calls[0][1].split('/')[2])
        self.assertEqual(firsts, {'ci.yml', 'other.yml'})

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

    def test_a_sweep_out_of_calls_says_reds_may_miss_some(self):
        class Spending(FakeGitHub):
            def call(self, path, method='GET', data=None):
                if len(self.calls) >= 2:
                    raise fw.Spent('200 GitHub calls made')
                return super().call(path, method, data)
        with tempfile.TemporaryDirectory() as tmp:
            config, output = os.path.join(tmp, 'fleet.json'), os.path.join(tmp, 'output')
            with open(config, 'w') as f:
                json.dump(CONFIG, f)
            gh = Spending()
            code, out = quiet(fw.main, ['--config', config, 'sweep'], {'GITHUB_OUTPUT': output, 'FLEET_VARS': '{}'},
                              gh, gh.now)
            self.assertEqual(code, 1)
            self.assertIn('::error::fleet-watch: 200 GitHub calls made; reds may miss', out)
            with open(output) as f:
                self.assertEqual(f.read(), 'reds=[]\n')

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
        return quiet(fw.pending, gh, 5, attempt)[0]

    def test_a_cut_off_attempt_is_pending_once_its_re_run_is_there(self):
        gh = FakeGitHub()
        cut_run(gh)
        self.assertEqual(self.pending(gh)[0], False)   # asked before the watch: the red is reported
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
                watch(gh)
                self.assertEqual(self.pending(gh)[0], False)
                self.assertEqual(len(gh.writes()), 0)

    def test_pending_and_watch_agree_whatever_the_mode(self):
        # A vm job cancelled after 700 s: auto would re-run it, fleet mode (the watch's setting) does not.
        for mode in ('auto', 'fleet'):
            with self.subTest(mode):
                gh = FakeGitHub()
                gh.add(a_run(conclusion='cancelled'),
                       a_job(51, VM, runner=None, conclusion='cancelled', completed=T0 + 700, steps=()))
                rerun = watch(gh, mode=mode)[0]
                self.assertEqual(self.pending(gh)[0], rerun)

    def test_a_refused_re_run_is_not_pending(self):
        for code in (403, 422, 502):
            with self.subTest(code):
                gh = FakeGitHub()
                cut_run(gh)
                gh.trouble.append((r'POST actions/runs/5/rerun-failed-jobs', http_error(code)))
                with self.assertRaises(urllib.error.HTTPError):
                    watch(gh)
                self.assertEqual(self.pending(gh)[0], False)


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
        env = {'GITHUB_OUTPUT': self.output, 'FLEET_VARS': '{}'}
        code, _ = quiet(fw.main, ['--config', self.config, 'watch', '--run-id', '5'], env, gh, gh.now)
        self.assertEqual(code, 0)
        code, _ = quiet(fw.main, ['--config', self.config, 'pending', '--run-id', '5', '--attempt', '1'], env, gh,
                        gh.now)
        self.assertEqual(code, 0)
        gh.runs[5].update(status='completed', conclusion='failure')
        code, _ = quiet(fw.main, ['--config', self.config, 'sweep'], env, gh, gh.now)
        with open(self.output) as f:
            self.assertEqual(f.read(), 'rerun=true\npending=true\nreds=[{"run_id":5,"attempt":2}]\n')

    def test_watch_and_sweep_refuse_missing_or_garbled_vars(self):
        cases = {'missing': '', 'not json': '{x', 'not an object': '[]', 'not a mode': '{"PROJ_RUNNERS": "flet"}',
                 'not a string': '{"PROJ_RUNNERS": 1}'}
        for verb in ('watch', 'sweep'):
            for case, raw in cases.items():
                with self.subTest(verb=verb, case=case):
                    gh = FakeGitHub()
                    cut_run(gh)
                    with self.assertRaises(ValueError):
                        quiet(fw.main, ['--config', self.config, verb, '--run-id', '5'],
                              {'GITHUB_OUTPUT': self.output, 'FLEET_VARS': raw}, gh, gh.now)
                    self.assertEqual(gh.calls, [])
                    got = self.run_step(verb=verb, vars=raw, **{'run-id': '5'})
                    self.assertNotEqual(got.returncode, 0)
                    self.assertRegex(got.stdout, r'::error::fleet-watch: ValueError: .*(vars input|flet)')

    def test_the_example_caller_asks_for_actions_write_only(self):
        with open(os.path.join(HERE, 'fleet-watch-caller.example.yml')) as f:
            workflow = yaml.safe_load(f)
        self.assertEqual(workflow['permissions'], {})
        self.assertEqual(workflow['jobs']['watch']['permissions'], {'actions': 'write', 'contents': 'read'})
        steps = workflow['jobs']['watch']['steps']
        self.assertIs(steps[0]['with']['persist-credentials'], False)
        uses = [s.get('with', {}).get('verb') for s in steps if 'fleet-watch' in (s.get('uses') or '')]
        self.assertEqual(uses[:2], [steps[1]['with']['verb'], 'pending'])   # pending is asked after the watch
        self.assertEqual(steps[1]['with']['vars'], '${{ toJSON(vars) }}')


if __name__ == '__main__':
    for name in TOKENS:
        os.environ.pop(name, None)
    unittest.main()
