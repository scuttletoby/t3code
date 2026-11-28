"""ci-lane's parity report: the lane's part results against the Actions steps that run the same calls, read from a
fake GitHub (no network), and the phase 3 gate.

  python3 -I -B fork-kit/ci-lane/tests/parity.test.py
"""
import datetime, os, sys, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))
sys.dont_write_bytecode = True
from cilane import parity  # noqa: E402
from cilane.github import ApiError  # noqa: E402

WF = '.github/workflows/fork-tests.yml'
SHA = 'a' * 40
T0 = datetime.datetime(2026, 11, 28, 12, 0, tzinfo=datetime.timezone.utc)
JOBS = {'kit:1': {'state': 'passed', 'seconds': 100, 'check': 'Kit 1', 'step': 'K'},
        'kit:2': {'state': 'failed', 'seconds': 120, 'check': 'Kit 2', 'step': 'K'},
        'workflows:1': {'state': 'fallback', 'seconds': None, 'check': 'W', 'step': 'S'}}


def step(name, conclusion, secs):
    return {'name': name, 'conclusion': conclusion, 'started_at': '2026-11-28T12:00:00Z',
            'completed_at': f'2026-11-28T12:{secs // 60:02d}:{secs % 60:02d}Z'}


class FakeGitHub:
    def __init__(self):
        self.runs = [{'id': 5, 'run_attempt': 1, 'path': WF, 'head_sha': SHA, 'status': 'completed'},
                     {'id': 9, 'run_attempt': 1, 'path': '.github/workflows/other.yml', 'head_sha': SHA,
                      'status': 'completed'}]
        self.jobs = {5: [{'name': 'Kit 1', 'steps': [step('Checkout', 'success', 3), step('K', 'success', 200)]},
                         {'name': 'Kit 2', 'steps': [step('K', 'success', 150)]},
                         {'name': 'W', 'steps': [step('S', 'success', 60)]}]}
        self.asked = 0
        self.fail = False

    def actions_runs(self, sha):
        self.asked += 1
        if self.fail:
            raise ApiError('HTTP 502')
        return self.runs

    def run_jobs(self, run_id):
        return self.jobs[run_id]


def entry(sha=SHA, jobs=None):
    return {'sha': sha, 'what': 'PR #1', 'finished': '2026-11-28T11:00:00Z', 'conclusion': 'failure',
            'workflow': WF, 'jobs': jobs or dict(JOBS), 'actions': None, 'actionsTries': 0}


class Side(unittest.TestCase):
    def test_each_job_is_matched_by_its_check_and_step(self):
        got = parity.actions_side(FakeGitHub(), SHA, WF, JOBS)
        self.assertEqual(got['run'], 5)
        self.assertEqual(got['jobs']['kit:1'], {'conclusion': 'success', 'seconds': 200})
        self.assertEqual(got['jobs']['kit:2']['conclusion'], 'success')

    def test_the_newest_attempt_counts(self):
        gh = FakeGitHub()
        gh.runs.append({'id': 6, 'run_attempt': 2, 'path': WF, 'head_sha': SHA, 'status': 'completed'})
        gh.jobs[6] = [{'name': 'Kit 1', 'steps': [step('K', 'failure', 10)]}]
        got = parity.actions_side(gh, SHA, WF, JOBS)
        self.assertEqual((got['run'], got['jobs']['kit:1']['conclusion']), (6, 'failure'))
        self.assertEqual(got['jobs']['kit:2']['conclusion'], 'missing')

    def test_no_complete_run_yet(self):
        gh = FakeGitHub()
        gh.runs[0]['status'] = 'in_progress'
        self.assertIsNone(parity.actions_side(gh, SHA, WF, JOBS))


class Fetch(unittest.TestCase):
    def test_at_most_per_tick_heads_and_spaced_retries(self):
        gh = FakeGitHub()
        gh.runs = []
        state = {'history': [entry('a' * 40), entry('b' * 40), entry('c' * 40)]}
        self.assertEqual(parity.fetch(state, gh, T0, print), 0)
        self.assertEqual(gh.asked, parity.PER_TICK)
        parity.fetch(state, gh, T0 + datetime.timedelta(minutes=1), print)
        self.assertEqual(gh.asked, 3)          # the third; the first two wait RETRY_MINUTES
        parity.fetch(state, gh, T0 + datetime.timedelta(minutes=parity.RETRY_MINUTES), print)
        self.assertEqual(gh.asked, 5)

    def test_gives_up_after_tries_and_never_asks_again(self):
        gh = FakeGitHub()
        gh.runs = []
        state = {'history': [entry()]}
        lines = []
        for i in range(parity.TRIES + 3):
            parity.fetch(state, gh, T0 + datetime.timedelta(minutes=i * parity.RETRY_MINUTES), lines.append)
        self.assertEqual((gh.asked, state['history'][0]['actions']), (parity.TRIES, 'none'))
        self.assertEqual(len(lines), 1)

    def test_an_error_is_counted_and_tried_again(self):
        gh = FakeGitHub()
        gh.fail = True
        state = {'history': [entry()]}
        self.assertEqual(parity.fetch(state, gh, T0, [].append), 1)
        gh.fail = False
        parity.fetch(state, gh, T0 + datetime.timedelta(minutes=parity.RETRY_MINUTES), print)
        self.assertEqual(state['history'][0]['actions']['run'], 5)

    def test_heads_without_jobs_or_a_plan_are_not_asked(self):
        gh = FakeGitHub()
        state = {'history': [{**entry(), 'jobs': {}}, {**entry(), 'workflow': None}]}
        parity.fetch(state, gh, T0, print)
        self.assertEqual(gh.asked, 0)


class Report(unittest.TestCase):
    def compared(self):
        e = entry()
        e['actions'] = parity.actions_side(FakeGitHub(), SHA, WF, e['jobs'])
        return {'history': [e]}

    def test_counts_same_and_different_and_skips_what_cannot_be_compared(self):
        r = parity.report(self.compared())
        self.assertEqual((r['heads'], r['parts'], r['same']), (1, 2, 1))     # the fallback part is not compared
        self.assertEqual([(d['part'], d['own'], d['actions']) for d in r['differences']],
                         [('kit:2', 'failed', 'success')])
        self.assertEqual(r['unexplained'], 1)
        self.assertFalse(r['gate']['ready'])
        self.assertIn('1 unexplained', parity.text(r))

    def test_an_explained_difference(self):
        r = parity.report(self.compared(), {f'{SHA}:kit:2': 'flaky on hosted runners'})
        self.assertEqual(r['unexplained'], 0)
        self.assertIn('explained: flaky', parity.text(r))

    def test_the_gate(self):
        e = entry(jobs={'kit:1': JOBS['kit:1']})
        e['actions'] = {'run': 5, 'jobs': {'kit:1': {'conclusion': 'success', 'seconds': 200}}}
        state = {'history': [dict(e, sha=f'{i:040x}') for i in range(parity.GATE_HEADS)]}
        self.assertTrue(parity.report(state)['gate']['ready'])
        state['history'].pop()
        self.assertFalse(parity.report(state)['gate']['ready'])               # too few heads
        slow = {'history': [dict(e, sha=f'{i:040x}', jobs={'kit:1': {**JOBS['kit:1'], 'seconds': 300}})
                            for i in range(parity.GATE_HEADS)]}
        self.assertFalse(parity.report(slow)['gate']['ready'])                # slower than Actions

    def test_heads_not_yet_compared(self):
        r = parity.report({'history': [entry(), {**entry(), 'actions': 'none'}]})
        self.assertEqual((r['heads'], r['parts']), (0, 0))
        self.assertFalse(r['gate']['ready'])


if __name__ == '__main__':
    unittest.main()
