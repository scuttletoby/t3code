#!/usr/bin/env python3
"""Tests of fork-ci-rerun.py (the one automatic re-run of an infrastructure failure), with a fake GitHub, and of its
wiring in fork-ci-rerun.yml. Run: python3 <this>."""
import copy, io, json, os, re, subprocess, sys, tempfile, time, unittest, urllib.error
import importlib.util

sys.dont_write_bytecode = True   # importing the script must not leave __pycache__ in the tree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, 'fork-ci-rerun.py')
_spec = importlib.util.spec_from_file_location('fork_ci_rerun', SCRIPT)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)
_spec = importlib.util.spec_from_file_location('fork_ci_pressure', os.path.join(HERE, 'fork-ci-pressure.py'))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

CFG = cr.load()
REPO = CFG['repository']
OWNER = CFG['actors'][0]
BRANCH = CFG['push_branches'][0]
FOLLOWER = CFG['followers'][0]
SKIP_VAR, SKIP_VALUE = CFG['skip_when']['variable'], CFG['skip_when']['is'][0]
BOT = 'github-actions[bot]'
T0 = 1_800_000_000
SHA = '0123456789abcdef0123456789abcdef01234567'
NEWER = 'fedcba9876543210fedcba9876543210fedcba98'

# GitHub's own messages, as its annotations carry them.
LOST = ('The self-hosted runner: fleet-vm-1 lost communication with the server. Verify the machine is running and '
        'has a healthy network connection. Anything in your workflow that terminates the runner process, starves it '
        'for CPU/Memory, or blocks its network access can cause this error.')
SHUTDOWN = 'The runner has received a shutdown signal. This can happen when the runner service is stopped.'
JOB_LIMIT = 'The job has exceeded the maximum execution time of 15m0s'
STEP_LIMIT = "The action 'Test' has timed out after 10 minutes."
CANCELED = 'The operation was canceled.'
EXIT_1 = 'Process completed with exit code 1.'
EXIT_137 = 'Process completed with exit code 137.'


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def ann(message, level='failure', title=''):
    return {'message': message, 'annotation_level': level, 'title': title}


def note(host=None, cgroups=None, status='failure', seconds=600):
    """A pressure note as fork-ci-pressure.py writes it, decoded as GitHub hands it back."""
    h = {'full_share': 0.0, 'some_share': 0.5, 'full_avg10': 0.0, 'full_avg60': 0.0, 'full_avg300': 0.0,
         'some_avg60': 0.1, 'some_avg300': 0.1, 'available_mb': 9000, 'swap_used_mb': 0, 'oom_kill': 0}
    h.update(host or {})
    boxes = cgroups if cgroups is not None else [box()]
    body = json.dumps({'status': status, 'seconds': seconds, 'host': h, 'cgroups': boxes}, separators=(',', ':'))
    return ann(cr.NOTE_HEAD + body, level='notice', title=cr.NOTE_TITLE)


def box(**kw):
    b = {'path': '/fleet.slice/job-1.service', 'high_mb': 6144, 'max_mb': 8192, 'current_mb': 3000,
         'high': 0, 'max': 0, 'oom': 0, 'oom_kill': 0, 'full_share': 0.0}
    b.update(kw)
    return b


CALM = note()


def run(rid=1, attempt=1, conclusion='failure', status='completed', event='push', branch=BRANCH, actor=OWNER,
        by=OWNER, head_repo=REPO, updated=T0 - 120, workflow='fork-tests.yml'):
    return {'id': rid, 'run_attempt': attempt, 'status': status, 'conclusion': conclusion, 'event': event,
            'head_branch': branch, 'head_sha': SHA, 'path': f'.github/workflows/{workflow}',
            'repository': {'full_name': REPO}, 'head_repository': {'full_name': head_repo},
            'actor': {'login': actor}, 'triggering_actor': {'login': by}, 'updated_at': iso(updated)}


def job(jid, name='Test (1)', conclusion='failure'):
    return {'id': jid, 'name': name, 'conclusion': conclusion, 'status': 'completed',
            'check_run_url': f'https://api.example/repos/{REPO}/check-runs/{jid}'}


class Fake:
    """GitHub's REST API as fork-ci-rerun.py uses it; a re-run makes a new attempt triggered by github-actions."""

    def __init__(self, runs, jobs=None, annotations=None, heads=None):
        self.runs = {r['id']: r for r in runs}
        self.jobs = jobs or {}
        self.ann = annotations or {}
        self.heads = heads if heads is not None else {BRANCH: SHA, 'feature': SHA}
        self.attempts = {}
        self.calls = []
        self.post_fails = None    # an exception to raise for the next POST
        self.lost_reply = False   # the POST reaches GitHub, its reply never comes back

    def call(self, path, method='GET', data=None):
        self.calls.append((method, path))
        if method == 'POST':
            m = re.fullmatch(r'actions/runs/(\d+)/rerun-failed-jobs', path)
            if not m:
                raise AssertionError(f'unexpected POST {path}')
            r = self.runs[int(m[1])]
            if self.post_fails:
                e, self.post_fails = self.post_fails, None
                raise e() if callable(e) and not isinstance(e, BaseException) else e
            if r['status'] != 'completed':
                raise urllib.error.HTTPError(path, 403, 'This workflow is already running', {}, None)
            self.attempts[(r['id'], r['run_attempt'])] = copy.deepcopy(r)
            r.update(run_attempt=r['run_attempt'] + 1, status='queued', conclusion=None,
                     triggering_actor={'login': BOT})
            if self.lost_reply:
                raise urllib.error.URLError('connection reset')
            return {}
        m = re.fullmatch(r'actions/runs/(\d+)', path)
        if m:
            return copy.deepcopy(self.runs[int(m[1])])
        m = re.fullmatch(r'actions/runs/(\d+)/attempts/(\d+)', path)
        if m:
            got = self.attempts.get((int(m[1]), int(m[2])))
            if got is None:
                got = dict(self.runs[int(m[1])], run_attempt=int(m[2]), triggering_actor={'login': OWNER})
            return copy.deepcopy(got)
        m = re.fullmatch(r'check-runs/(\d+)/annotations\?per_page=100', path)
        if m:
            return copy.deepcopy(self.ann.get(int(m[1]), []))
        m = re.fullmatch(r'branches/(.+)', path)
        if m:
            if m[1] not in self.heads:
                raise urllib.error.HTTPError(path, 404, 'Branch not found', {}, None)
            return {'commit': {'sha': self.heads[m[1]]}}
        raise AssertionError(f'unexpected GET {path}')

    def pages(self, path, key, most=10):
        self.calls.append(('GET', path))
        if path.startswith(f'actions/workflows/{CFG["workflow"]}/runs?created=%3E%3D'):
            return [copy.deepcopy(r) for r in self.runs.values()]
        m = re.fullmatch(r'actions/runs/(\d+)/jobs\?filter=latest', path)
        if m:
            return copy.deepcopy(self.jobs.get(int(m[1]), []))
        raise AssertionError(f'unexpected pages {path}')

    def posts(self):
        return [p for m, p in self.calls if m == 'POST']


def sweep(gh, env=None, run_id=None, cfg=CFG):
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'summary.md')
        code = cr.sweep(cfg, dict(env or {}, GITHUB_STEP_SUMMARY=path), gh, now=lambda: T0, run_id=run_id, out=out)
        text = ''
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                text = f.read()
    return code, out.getvalue(), text


def one(annotations, job_conclusion='failure', **kw):
    """A failed run whose one failed job (id 11) carries these annotations, plus a passed job and the follower."""
    jobs = [job(11, conclusion=job_conclusion), job(12, 'Lint', 'success'), job(13, FOLLOWER, 'failure')]
    return Fake([run(**kw)], {1: jobs}, {11: annotations, 13: [ann(EXIT_1)]})


class Signs(unittest.TestCase):
    def assertReran(self, gh, out):
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)
        self.assertIn('::warning title=fork-ci re-run::run 1', out)

    def test_lost_runner_is_rerun_once_with_the_message_in_the_log(self):
        for message in (LOST, SHUTDOWN, 'The hosted runner: GitHub Actions 7 lost communication with the server.'):
            gh = one([ann(message), ann(CANCELED)])
            code, out, text = sweep(gh)
            self.assertEqual(code, 0, out)
            self.assertReran(gh, out)
            self.assertIn('lost', out)
            self.assertIn('**re-run**', text)

    def test_lost_runner_needs_no_pressure_note_an_old_run_without_the_new_lines(self):
        gh = one([ann(LOST)])   # the job never reached its end step: no note
        self.assertReran(gh, sweep(gh)[1])

    def test_a_full_box_is_rerun_with_its_numbers_in_the_log(self):
        cases = {
            'box OOM kill': note(cgroups=[box(oom_kill=1)]),
            'box memory.max hits': note(cgroups=[box(max=3)]),
            'machine OOM kill': note(host={'oom_kill': 2}),
            'memory.high throttling while stalled': note(host={'full_share': 25.0}, cgroups=[box(high=40)]),
            'memory.high throttling, the box stalled': note(cgroups=[box(high=40, full_share=30.0)]),
        }
        for label, n in cases.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), n])
                code, out, text = sweep(gh)
                self.assertReran(gh, out)
                self.assertIn('memory full', out)
                self.assertRegex(out, r'machine: full [0-9.]+% some [0-9.]+% of 600 s')
                self.assertIn('box /fleet.slice/job-1.service: max 8192 MB high 6144 MB', out)
                self.assertIn('memory full', text)

    def test_memory_high_events_without_a_stall_are_no_sign(self):
        gh = one([ann(EXIT_1), note(host={'full_share': 2.0}, cgroups=[box(high=40, full_share=3.0)])])
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('no infrastructure sign', out)

    def test_time_limit_while_stalled_is_rerun(self):
        for message in (JOB_LIMIT, STEP_LIMIT):
            for stall in ({'full_share': 12.0}, {'full_avg300': 15.0}):
                with self.subTest(message=message, stall=stall):
                    gh = one([ann(message), ann(CANCELED), note(host=stall, status='cancelled')], 'cancelled',
                             conclusion='cancelled')
                    code, out, _ = sweep(gh)
                    self.assertReran(gh, out)
                    self.assertIn('limit while stalled', out)
        gh = one([ann(JOB_LIMIT), note(cgroups=[box(full_share=50.0)], status='cancelled')], 'cancelled')
        self.assertReran(gh, sweep(gh)[1])

    def test_time_limit_on_a_calm_machine_is_left_red(self):
        gh = one([ann(JOB_LIMIT), ann(CANCELED), CALM], 'cancelled')
        code, out, text = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('not stalled', out)
        self.assertIn('left red', text)

    def test_time_limit_without_a_note_is_left_red_fail_closed(self):
        gh = one([ann(JOB_LIMIT), ann(CANCELED)], 'cancelled')   # an old run, or the note was dropped
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('cannot tell it was stalled', out)

    def test_a_test_failure_is_never_rerun(self):
        gh = one([ann(EXIT_1), CALM])
        code, out, text = sweep(gh)
        self.assertEqual((code, gh.posts()), (0, []), out)
        self.assertIn('Test (1) ended failure: no infrastructure sign', out)
        self.assertIn('| 1 | left red |', text)

    def test_a_job_with_no_readable_signs_is_left_red(self):
        garbled = dict(CALM, message=cr.NOTE_HEAD + '{"host": ')
        cases = {'no annotations at all': [], 'a garbled note': [ann(EXIT_1), garbled],
                 'a note without its head': [ann(EXIT_1), dict(CALM, message='{"host":{},"cgroups":[]}')],
                 'a note of the wrong shape': [ann(EXIT_1), dict(CALM, message=cr.NOTE_HEAD + '[1, 2]')],
                 'two notes (one forged)': [ann(EXIT_137), CALM, note(cgroups=[box(oom_kill=5)])],
                 'numbers that are not numbers': [ann(EXIT_137), note(host={'oom_kill': '9'},
                                                                      cgroups=[box(oom_kill=True, max='x')])],
                 'a cancel by a person': [ann(CANCELED)]}
        for label, anns in cases.items():
            with self.subTest(label):
                gh = one(anns)
                code, out, _ = sweep(gh)
                self.assertEqual((code, gh.posts()), (0, []), out)
                self.assertIn('left red', out)

    def test_one_job_without_a_sign_leaves_the_whole_run_red(self):
        jobs = [job(11), job(12, 'Test (2)'), job(13, FOLLOWER)]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)], 12: [ann(EXIT_1), CALM]})
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('Test (2) ended failure', out)

    def test_every_failed_job_with_a_sign_is_rerun_together(self):
        jobs = [job(11), job(12, 'Build and Typecheck'), job(13, FOLLOWER)]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)], 12: [ann(EXIT_137), note(cgroups=[box(oom_kill=1)])]})
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)
        self.assertIn('Test (1) (failure): lost', out)
        self.assertIn('Build and Typecheck (failure): memory full', out)

    def test_only_a_follower_failed_is_left_red(self):
        gh = Fake([run()], {1: [job(12, 'Lint', 'success'), job(13, FOLLOWER)]}, {13: [ann(LOST)]})
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('no failed job of its own', out)

    def test_a_job_skipped_after_a_failed_one_needs_no_sign(self):
        jobs = [job(11), job(12, 'Lint', 'skipped'), job(13, FOLLOWER)]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)]})
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)


class NeverTwice(unittest.TestCase):
    def test_a_second_failure_after_the_automatic_rerun_stays_red(self):
        gh = one([ann(LOST)], attempt=2, by=BOT)
        code, out, text = sweep(gh)
        self.assertEqual((code, gh.posts()), (0, []), out)
        self.assertIn('a repeat red stays red', out)
        self.assertIn('1 run(s) whose latest attempt was an automatic re-run', out)
        self.assertIn('1 run(s) whose latest attempt was an automatic re-run', text)

    def test_a_person_rerunning_after_the_automatic_rerun_gets_no_second_one(self):
        gh = one([ann(LOST)], attempt=3, by=OWNER)
        gh.attempts[(1, 2)] = run(attempt=2, by=BOT)
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), [], out)
        self.assertIn('attempt 2 was triggered by', out)
        self.assertIn('never twice', out)

    def test_a_person_rerun_without_an_automatic_one_may_get_it(self):
        gh = one([ann(LOST)], attempt=2, by=OWNER)   # attempt 1 by the owner too (the fake's default)
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)

    def test_the_watcher_runs_again_on_the_run_it_reran(self):
        gh = one([ann(LOST)])
        sweep(gh)
        self.assertEqual(len(gh.posts()), 1)
        code, out, _ = sweep(gh)   # the re-run is queued: not completed, nothing to do
        self.assertEqual((code, len(gh.posts())), (0, 1), out)
        gh.runs[1].update(status='completed', conclusion='failure')   # and it failed again, the same way
        code, out, _ = sweep(gh)
        self.assertEqual((code, len(gh.posts())), (0, 1), out)
        self.assertIn('a repeat red stays red', out)

    def test_the_request_sent_twice_github_refuses_the_second(self):
        gh = one([ann(LOST)])
        first = copy.deepcopy(gh.runs[1])
        sweep(gh)
        # A second watcher (or the fleet sweep) read the run before the first re-ran it and asks again.
        done, how = cr.rerun(gh, first)
        self.assertFalse(done)
        self.assertIn('already re-run', how)
        self.assertEqual(len(gh.posts()), 1, 'the second request is not even sent: the run is read again first')

    def test_github_refusing_a_rerun_that_already_started_is_no_trouble(self):
        gh = one([ann(LOST)])
        # Another watcher re-ran it between this pass's last read and its request.
        def race():
            gh.runs[1].update(run_attempt=2, status='queued', triggering_actor={'login': BOT})
            return urllib.error.HTTPError('x', 403, 'This workflow is already running', {}, None)
        gh.post_fails = race
        code, out, text = sweep(gh)
        self.assertEqual(code, 0, out)
        self.assertIn('already re-run (GitHub refused a second request: HTTP 403)', out)

    def test_a_refusal_with_the_run_unchanged_is_trouble_and_tried_again(self):
        gh = one([ann(LOST)])
        gh.post_fails = urllib.error.HTTPError('x', 500, 'Server Error', {}, None)
        code, out, text = sweep(gh)
        self.assertEqual(code, 1, out)
        self.assertIn('the next pass tries again', out)
        self.assertIn('not read', text)
        code, out, _ = sweep(gh)   # the next pass
        self.assertEqual(code, 0, out)
        self.assertEqual(len(gh.posts()), 2)
        self.assertEqual(gh.runs[1]['run_attempt'], 2)

    def test_the_watcher_stopped_half_way_the_request_never_reached_github(self):
        gh = one([ann(LOST)])
        gh.post_fails = urllib.error.URLError('timed out')
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.runs[1]['run_attempt']), (1, 1), out)
        code, out, _ = sweep(gh)   # the next pass finishes it, once
        self.assertEqual((code, gh.runs[1]['run_attempt']), (0, 2), out)
        self.assertEqual(len(gh.posts()), 2)
        sweep(gh)
        self.assertEqual(len(gh.posts()), 2)

    def test_the_watcher_stopped_half_way_the_rerun_was_asked_but_not_started(self):
        gh = one([ann(LOST)])
        gh.lost_reply = True   # GitHub made attempt 2; the watcher never heard back
        code, out, _ = sweep(gh)
        self.assertEqual(code, 1, out)
        gh.lost_reply = False
        for _ in range(2):   # queued, never started: the next passes leave it alone
            code, out, _ = sweep(gh)
            self.assertEqual((code, len(gh.posts())), (0, 1), out)
        gh.runs[1].update(status='completed', conclusion='cancelled')   # it never ran (cancelled while queued)
        code, out, _ = sweep(gh)
        self.assertEqual(len(gh.posts()), 1, out)
        self.assertIn('a repeat red stays red', out)


class Scope(unittest.TestCase):
    def test_untrusted_runs_are_left_alone(self):
        cases = {'a stranger': dict(actor='someone-else', by='someone-else'),
                 'triggered by a stranger': dict(by='someone-else'),
                 'a pull request from a fork': dict(event='pull_request', branch='feature',
                                                    head_repo='someone-else/t3code'),
                 'a push to another branch': dict(branch='feature'),
                 'another event': dict(event='workflow_dispatch'),
                 'another workflow': dict(workflow='other.yml')}
        for label, kw in cases.items():
            with self.subTest(label):
                gh = one([ann(LOST)], **kw)
                code, out, _ = sweep(gh)
                self.assertEqual((code, gh.posts()), (0, []), out)
                self.assertIn('not a trusted run', out)
                self.assertFalse([p for _, p in gh.calls if '/jobs' in p], 'an untrusted run costs no job read')

    def test_an_own_pull_request_is_rerun(self):
        gh = one([ann(LOST)], event='pull_request', branch='feature')
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)

    def test_a_branch_that_moved_on_or_is_gone_is_not_rerun(self):
        for heads in ({BRANCH: NEWER}, {}):
            with self.subTest(heads=heads):
                gh = one([ann(LOST)])
                gh.heads = heads
                code, out, _ = sweep(gh)
                self.assertEqual((code, gh.posts()), (0, []), out)
                self.assertIn('has moved on or is gone', out)

    def test_runs_that_passed_are_still_running_or_ended_long_ago_cost_nothing(self):
        gh = Fake([run(1, conclusion='success'), run(2, status='in_progress', conclusion=None),
                   run(3, updated=T0 - CFG['catch_up_minutes'] * 60 - 1)], {}, {})
        code, out, text = sweep(gh)
        self.assertEqual(code, 0, out)
        self.assertEqual([p for _, p in gh.calls if not p.startswith('actions/workflows/')], [])
        self.assertIn('0 run(s) re-run by this pass', text)

    def test_an_empty_list_of_runs(self):
        code, out, text = sweep(Fake([]))
        self.assertEqual(code, 0, out)
        self.assertIn('0 run(s) re-run by this pass; 0 run(s)', out)
        self.assertIn('### fork-ci re-run', text)

    def test_one_run_by_id_ignores_the_catch_up_window(self):
        gh = one([ann(LOST)], updated=T0 - 5 * 3600)
        code, out, _ = sweep(gh, run_id=1)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)
        self.assertFalse([p for _, p in gh.calls if p.startswith('actions/workflows/')])

    def test_fleet_mode_reports_and_does_not_rerun(self):
        gh = one([ann(LOST)])
        code, out, text = sweep(gh, env={SKIP_VAR: SKIP_VALUE})
        self.assertEqual((code, gh.posts()), (0, []), out)
        self.assertIn('::error title=fork-ci re-run::', out)
        self.assertIn('re-run it by hand', out)
        self.assertIn(f'not re-run ({SKIP_VAR}={SKIP_VALUE})', text)
        gh = one([ann(LOST)])
        sweep(gh, env={SKIP_VAR: 'auto'})
        self.assertEqual(len(gh.posts()), 1)

    def test_a_github_error_on_one_run_does_not_stop_the_others(self):
        gh = Fake([run(1), run(2)], {2: [job(21)]}, {21: [ann(LOST)]})
        real = gh.pages
        def pages(path, key, most=10):
            if path.startswith('actions/runs/1/'):
                raise urllib.error.URLError('reset')
            return real(path, key, most)
        gh.pages = pages
        code, out, _ = sweep(gh)
        self.assertEqual(code, 1, out)
        self.assertEqual(gh.posts(), ['actions/runs/2/rerun-failed-jobs'])

    def test_a_pipe_in_a_message_keeps_the_summary_table_whole(self):
        gh = one([ann('The hosted runner: a | b lost communication with the server.'), CALM])
        _, _, text = sweep(gh)
        self.assertIn('a \\| b', text)
        row = [l for l in text.splitlines() if l.startswith('| 1 |')][0]
        self.assertEqual(row.count(' | '), 2, row)


class Config(unittest.TestCase):
    def test_the_real_config_is_valid_and_names_the_rules_parts(self):
        self.assertEqual(cr.check_config(CFG), [])
        r = subprocess.run([sys.executable, '-I', SCRIPT, 'check'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_github_messages_match_their_sign_and_nothing_else_does(self):
        lost, limit = CFG['signs']['lost'], CFG['signs']['limit']
        for m in (LOST, SHUTDOWN):
            self.assertTrue(cr.matches(lost, [m]) and not cr.matches(limit, [m]), m)
        for m in (JOB_LIMIT, STEP_LIMIT):
            self.assertTrue(cr.matches(limit, [m]) and not cr.matches(lost, [m]), m)
        for m in (EXIT_1, EXIT_137, CANCELED, 'Error: the test timed out after 5000 ms'):
            self.assertFalse(cr.matches(lost + limit, [m]), m)

    def test_bad_configs_are_refused(self):
        def bad(change):
            cfg = copy.deepcopy(CFG)
            change(cfg)
            return cr.check_config(cfg)
        cases = {
            'a bad pattern': lambda c: c['signs']['lost'].append('(['),
            'an unknown sign': lambda c: c['signs'].__setitem__('exit', ['137']),
            'no actors': lambda c: c.__setitem__('actors', []),
            'an event a fork can cause': lambda c: c['events'].append('workflow_run'),
            'a zero threshold': lambda c: c['stalled'].__setitem__('full_share_percent', 0),
            'a missing threshold': lambda c: c['memory_full'].pop('max_events'),
            'a string threshold': lambda c: c['memory_full'].__setitem__('oom_kills', '1'),
            'an unknown key': lambda c: c.__setitem__('retries', 2),
            'no skip variable': lambda c: c['skip_when'].pop('variable'),
            'no window': lambda c: c.__setitem__('catch_up_minutes', -1),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assertTrue(bad(change), label)
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(dict(CFG, retries=2), f)
        try:
            r = subprocess.run([sys.executable, '-I', SCRIPT, 'check', '--config', f.name], capture_output=True,
                               text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unknown key 'retries'", r.stderr)
        finally:
            os.unlink(f.name)

    def test_the_script_holds_no_fact_of_this_repository(self):
        with open(SCRIPT, encoding='utf-8') as f:
            text = f.read()
        for fact in [REPO, OWNER, CFG['workflow'], FOLLOWER] + CFG['signs']['lost'] + CFG['signs']['limit']:
            self.assertFalse(fact in text.replace('fork-ci-rerun', ''), f'the script names {fact!r}')


class ProbeToDecider(unittest.TestCase):
    """The probe's notice, as GitHub decodes it, is the note the decider reads (one contract, both ends)."""

    def test_a_probe_notice_round_trips(self):
        n = {'seconds': 300, 'host': {'full_share': 40.0, 'full_avg300': 1.0, 'oom_kill': 0},
             'cgroups': [{'path': '/a%b\nc', 'oom_kill': 2, 'max': 0, 'high': 0, 'full_share': None}]}
        out = io.StringIO()
        body = json.dumps({'status': 'failure', **n}, separators=(',', ':'), sort_keys=True)
        print(f'::notice title={probe.NOTE_TITLE}::{probe.command(probe.NOTE_HEAD + body)}', file=out)
        line = out.getvalue().strip()
        title, _, data = line[len('::notice '):].partition('::')
        self.assertEqual(title, f'title={cr.NOTE_TITLE}')
        self.assertNotIn('\n', data)
        message = data.replace('%0A', '\n').replace('%0D', '\r').replace('%25', '%')   # what the runner does
        got, why = cr.note_of([ann(message, 'notice', cr.NOTE_TITLE)])
        self.assertIsNone(why)
        self.assertEqual(got['cgroups'][0]['path'], '/a%b\nc')
        self.assertEqual(cr.job_sign(CFG, [ann(EXIT_137), ann(message, 'notice', cr.NOTE_TITLE)])[0], 'memory full')
        self.assertEqual((probe.NOTE_TITLE, probe.NOTE_HEAD), (cr.NOTE_TITLE, cr.NOTE_HEAD))


class Wiring(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, '.github/workflows/fork-ci-rerun.yml'), encoding='utf-8') as f:
            self.text = f.read()

    def test_the_workflow_runs_the_sweep_on_a_schedule_with_least_permissions(self):
        import yaml
        wf = yaml.safe_load(self.text)
        self.assertEqual(set(wf[True]), {'schedule', 'workflow_dispatch'})
        self.assertEqual(wf['permissions'], {})
        sweep_job = wf['jobs']['sweep']
        self.assertEqual(sweep_job['permissions'], {'actions': 'write', 'checks': 'read', 'contents': 'read'})
        self.assertFalse(wf['concurrency']['cancel-in-progress'], 'a pass is never cut off by the next')
        step = sweep_job['steps'][-1]
        self.assertIn('python3 -I .github/scripts/fork-ci-rerun.py sweep', step['run'])
        self.assertEqual(step['env']['CI_RUNNERS'], '${{ vars.CI_RUNNERS }}')
        self.assertEqual(SKIP_VAR, 'CI_RUNNERS')
        checkout = sweep_job['steps'][0]['with']['sparse-checkout']
        self.assertIn('/.github/fork-ci-rerun.json', checkout)
        self.assertIn('/.github/scripts/fork-ci-rerun.py', checkout)

    def test_the_sweep_is_offset_from_the_fleet_sweep(self):
        import yaml
        ours = yaml.safe_load(self.text)[True]['schedule'][0]['cron']
        with open(os.path.join(ROOT, '.github/workflows/fork-fleet.yml'), encoding='utf-8') as f:
            fleet = yaml.safe_load(f)[True]['schedule'][0]['cron']
        self.assertEqual((ours.split()[0], fleet.split()[0]), ('5-55/10', '*/10'))


if __name__ == '__main__':
    unittest.main()
