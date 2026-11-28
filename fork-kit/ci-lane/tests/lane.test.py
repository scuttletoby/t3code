"""ci-lane's tick on a fake GitHub and a fake launcher (no network, no root, no systemd): what a check may claim,
retries and leases, superseded heads, and the state question (runs again, stopped half-way, old or empty state).
RedCheck breaks each guard of the verdict in turn and shows a test then fails.

  python3 -I -B fork-kit/ci-lane/tests/lane.test.py
"""
import copy, datetime, json, os, sys, tempfile, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))
sys.dont_write_bytecode = True
from cilane import cli, lane, plan as plans  # noqa: E402
from cilane.github import ApiError  # noqa: E402

REPO, APP = 'o/r', 77
MAIN, PR1, PR2, OUT = 'a' * 40, 'b' * 40, 'c' * 40, 'd' * 40
T0 = datetime.datetime(2026, 11, 28, 12, 0, tzinfo=datetime.timezone.utc)
PLAN = json.dumps({'version': 1, 'workflow': '.github/workflows/fork-tests.yml', 'parts': [
    {'name': 'workflows', 'job': 'workflows', 'check': 'W', 'step': 'S', 'args': ['!fork-kit/'], 'lane': True},
    {'name': 'kit', 'job': 'kit_tests', 'check': 'Kit {shard}', 'step': 'K', 'args': ['fork-kit/'], 'shards': 2,
     'lane': True},
    {'name': 'root', 'job': 'r', 'check': 'R', 'step': 'R', 'args': ['--run', 'x'], 'lane': False,
     'why': 'runs as root'}]})
CFG = {'version': 1, 'repo': REPO, 'mode': 'shadow', 'checkName': 'fork-ci-own', 'appId': APP,
       'auth': {'ghAs': '/usr/local/bin/gh-as'}, 'launch': ['/usr/bin/sudo', '-n', '/x/launch'],
       'maxJobs': 8, 'leaseMinutes': 40, 'explained': {}}


def pr(number, sha, branch, cross=False, repo=REPO):
    return {'number': number, 'isCrossRepository': cross, 'headRefName': branch, 'headRefOid': sha,
            'headRepository': {'nameWithOwner': repo}}


class FakeGitHub:
    def __init__(self):
        self.tip = MAIN
        self.prs = [pr(1, PR1, 'one')]
        self.total = None
        self.plans = {}             # sha -> text (default PLAN)
        self.checks = {}            # id -> check run
        self.calls = []
        self.fail = set()           # method names that raise

    def _maybe(self, what):
        self.calls.append(what)
        if what in self.fail:
            raise ApiError(f'{what}: HTTP 502', status=502)

    def graphql(self, query, variables):
        self._maybe('graphql')
        return {'repository': {'nameWithOwner': REPO,
                               'defaultBranchRef': {'name': 'main', 'target': {'oid': self.tip}},
                               'pullRequests': {'totalCount': self.total or len(self.prs), 'nodes': self.prs}}}

    def file_at(self, path, sha):
        self._maybe('file_at')
        self.assert_path = path
        return self.plans.get(sha, PLAN)

    def our_checks(self, sha, name, app_id):
        self._maybe('our_checks')
        return [c for c in self.checks.values() if c['head_sha'] == sha and c['name'] == name]

    def create_check(self, body):
        self._maybe('create_check')
        cid = len(self.checks) + 1
        self.checks[cid] = {'id': cid, **body}
        return cid

    def update_check(self, cid, body):
        self._maybe('update_check')
        self.checks[cid].update(body)

    def check_of(self, sha):
        got = [c for c in self.checks.values() if c['head_sha'] == sha]
        assert len(got) <= 1, got
        return got[0] if got else None


def records(commit, part, shard, of, attempt, **over):
    rec = {'v': 1, 'sha': commit, 'job': f'lane-{part}', 'run_attempt': attempt, 'call': {'shard': f'{shard}/{of}'},
           'owed': ['t1', 't2'], 'results': [{'key': 't1', 'status': 0}, {'key': 't2', 'status': 0}]}
    rec.update(over)
    return rec


class FakeLauncher:
    def __init__(self):
        self.units = {}             # name -> {'sha', 'part', 'shard', 'of', 'attempt', 'result'}
        self.started, self.stopped = [], []
        self.refuse = False
        self.order = None           # the GitHub fake's call list, to see what came first

    def start(self, name, sha, job, attempt):
        if self.refuse:
            raise lane.LaunchError(f'lane-start {name}: exit 1: no free runner user')
        if self.order is not None:
            self.order.append(f'start {name}')
        self.started.append(name)
        self.units[name] = {'sha': sha, 'part': job.part, 'shard': job.shard, 'of': job.of, 'attempt': attempt,
                            'result': {'state': 'running'}}

    def result(self, name):
        return self.units[name]['result'] if name in self.units else {'state': 'gone'}

    def stop(self, name):
        self.stopped.append(name)

    def pass_all(self, **over):
        for u in self.units.values():
            if u['result']['state'] == 'running':
                u['result'] = {'state': 'done', 'exit': 0,
                               'records': records(u['sha'], u['part'], u['shard'], u['of'], u['attempt'], **over)}

    def finish(self, name, result):
        self.units[name]['result'] = result


class Case(unittest.TestCase):
    def setUp(self):
        self.gh, self.launcher = FakeGitHub(), FakeLauncher()
        self.launcher.order = self.gh.calls
        self.state = {'version': 1, 'heads': {}, 'history': []}
        self.now, self.lines = T0, []
        self.cfg = copy.deepcopy(CFG)

    def tick(self, minutes=1):
        self.now += datetime.timedelta(minutes=minutes)
        return lane.Lane(self.cfg, self.gh, self.launcher, self.state, self.now, self.lines.append).tick()

    def conclusion(self, sha):
        c = self.gh.check_of(sha)
        return c and (c.get('conclusion') or c.get('status'))

    def units_of(self, sha):
        return sorted(n for n in self.launcher.started if n.startswith(f'lane-{sha[:12]}-'))


class Runs(Case):
    def test_a_new_head_gets_its_check_in_progress_before_any_job(self):
        self.assertEqual(self.tick(), 0)
        for sha in (MAIN, PR1):
            self.assertEqual(self.conclusion(sha), 'in_progress')
            self.assertEqual(self.gh.check_of(sha)['external_id'], f'ci-lane:{sha}')
        first_start = next(i for i, c in enumerate(self.gh.calls) if c.startswith('start '))
        self.assertEqual(self.gh.calls[:first_start].count('create_check'), 2)
        self.assertEqual(self.units_of(MAIN), [f'lane-{MAIN[:12]}-kit-1-1', f'lane-{MAIN[:12]}-kit-2-1',
                                               f'lane-{MAIN[:12]}-workflows-1-1'])

    def test_all_parts_passed_is_success_and_the_head_moves_to_history(self):
        self.tick()
        self.launcher.pass_all()
        self.assertEqual(self.tick(), 0)
        self.assertEqual(self.conclusion(MAIN), 'success')
        self.assertIn('`root` | | on Actions', self.gh.check_of(MAIN)['output']['summary'])
        self.assertEqual(self.state['heads'], {})
        self.assertEqual([h['sha'] for h in self.state['history']], [MAIN, PR1])

    def test_a_real_failure_is_failure_and_is_not_retried(self):
        self.tick()
        self.launcher.finish(f'lane-{PR1[:12]}-kit-2-1', {'state': 'done', 'exit': 1, 'records': None})
        self.launcher.pass_all()
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'failure')
        self.assertEqual(len(self.units_of(PR1)), 3)

    def test_max_jobs_and_the_default_branch_first(self):
        self.cfg['maxJobs'] = 2
        self.tick()
        self.assertEqual(len(self.launcher.started), 2)
        self.assertTrue(all(n.startswith(f'lane-{MAIN[:12]}') for n in self.launcher.started))
        self.launcher.pass_all()
        self.tick()
        self.assertEqual(len(self.launcher.started), 4)

    def test_cross_repo_pull_requests_are_never_run(self):
        self.gh.prs = [pr(2, OUT, 'x', cross=True, repo='else/r'), pr(3, PR2, 'y', cross=False, repo='else/r')]
        self.tick()
        self.assertIsNone(self.gh.check_of(OUT))
        self.assertIsNone(self.gh.check_of(PR2))
        self.assertEqual(self.units_of(OUT) + self.units_of(PR2), [])

    def test_no_plan_file_at_the_commit_is_neutral_and_runs_nothing(self):
        self.gh.plans[PR1] = None
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'neutral')
        self.assertIn('no .github/fork-ci-plan.json', self.gh.check_of(PR1)['output']['title'])
        self.assertEqual(self.units_of(PR1), [])

    def test_a_plan_it_cannot_read_is_neutral(self):
        self.gh.plans[PR1] = PLAN.replace('"version": 1', '"version": 2')
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'neutral')
        self.assertEqual(self.units_of(PR1), [])


class Verdict(Case):
    """No green without a complete record of this very run."""

    def run_with(self, **over):
        self.tick()
        self.launcher.pass_all(**over)
        self.tick()
        return self.conclusion(PR1)

    def test_a_complete_record_passes(self):
        self.assertEqual(self.run_with(), 'success')

    def test_records_of_another_commit(self):
        self.assertEqual(self.run_with(sha=MAIN), 'failure')

    def test_records_of_another_attempt(self):
        self.assertEqual(self.run_with(run_attempt=2), 'failure')

    def test_records_of_another_part(self):
        self.assertEqual(self.run_with(job='lane-other'), 'failure')

    def test_records_of_another_shard(self):
        self.assertEqual(self.run_with(call={'shard': '1/1'}), 'failure')

    def test_an_owed_test_without_a_result(self):
        self.assertEqual(self.run_with(results=[{'key': 't1', 'status': 0}]), 'failure')

    def test_a_failed_result_even_with_exit_0(self):
        self.assertEqual(self.run_with(results=[{'key': 't1', 'status': 0}, {'key': 't2', 'status': 1}]),
                         'failure')

    def test_nothing_owed(self):
        self.assertEqual(self.run_with(owed=[], results=[]), 'failure')

    def test_exit_0_without_records(self):
        self.tick()
        for u in self.launcher.units.values():
            u['result'] = {'state': 'done', 'exit': 0}
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'failure')

    def test_judge_alone(self):
        job = plans.Job('kit', 1, 2, 'Kit {shard}', 'K')
        ok = {'state': 'done', 'exit': 0, 'records': records(PR1, 'kit', 1, 2, 1)}
        self.assertEqual(lane.judge(ok, PR1, job, 1)[0], 'passed')
        self.assertEqual(lane.judge(ok, PR1, job, 2)[0], 'failed')
        self.assertEqual(lane.judge({**ok, 'exit': 3}, PR1, job, 1), ('failed', 'exit 3'))


class Lost(Case):
    def test_a_unit_gone_without_a_result_gets_one_retry_then_actions_counts(self):
        self.tick()
        name = f'lane-{PR1[:12]}-workflows-1-1'
        del self.launcher.units[name]                   # the machine died: no unit, no result
        self.tick()
        retry = f'lane-{PR1[:12]}-workflows-1-2'
        self.assertIn(retry, self.launcher.started)     # attempt 2, under a name of its own
        del self.launcher.units[retry]
        self.launcher.pass_all()
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'neutral')     # never success
        self.assertIn('Actions counts', self.gh.check_of(PR1)['output']['title'])
        self.assertTrue(any(line.startswith('ALERT') for line in self.lines))
        self.assertEqual(len(self.units_of(PR1)), 4)          # one retry, no more

    def test_a_lost_retry_does_not_hide_a_real_failure(self):
        self.tick()
        del self.launcher.units[f'lane-{PR1[:12]}-workflows-1-1']
        self.tick()
        del self.launcher.units[f'lane-{PR1[:12]}-workflows-1-2']
        self.launcher.finish(f'lane-{PR1[:12]}-kit-1-1', {'state': 'done', 'exit': 1})
        self.launcher.pass_all()
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'failure')

    def test_the_lease_runs_out_the_unit_is_stopped_and_retried(self):
        self.tick()
        self.tick(minutes=39)
        self.assertEqual(self.launcher.stopped, [])
        self.tick(minutes=1)
        self.assertIn(f'lane-{PR1[:12]}-kit-1-1', self.launcher.stopped)
        self.assertIn(f'lane-{PR1[:12]}-kit-1-2', self.launcher.started)

    def test_a_launcher_that_cannot_answer_waits_for_the_lease(self):
        self.tick()
        broken = mock.patch.object(self.launcher, 'result', side_effect=lane.LaunchError('lane-result: exit 1'))
        with broken:
            self.assertGreater(self.tick(), 0)
            self.assertEqual(self.launcher.stopped, [])
            self.tick(minutes=40)
        self.assertEqual(len(self.launcher.stopped), 6)

    def test_a_start_refused_three_times_is_lost(self):
        self.launcher.refuse = True
        for _ in range(3):
            self.assertGreater(self.tick(), 0)
        job = self.state['heads'][PR1]['jobs']['kit:1']
        self.assertEqual((job['attempt'], job['state']), (2, 'queued'))
        for _ in range(3):
            self.tick()
        self.assertEqual(self.conclusion(PR1), 'neutral')


class Superseded(Case):
    def test_a_new_push_stops_the_old_heads_jobs_and_cancels_its_check(self):
        self.tick()
        self.gh.prs = [pr(1, PR2, 'one')]
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'cancelled')
        self.assertEqual(sorted(self.launcher.stopped), self.units_of(PR1))
        self.assertNotIn(PR1, self.state['heads'])
        self.assertEqual(self.conclusion(PR2), 'in_progress')

    def test_a_head_outside_an_incomplete_list_is_left_alone(self):
        self.tick()
        self.gh.prs, self.gh.total = [pr(2, PR2, 'two')], 150      # PR 1 fell off the first 100
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'in_progress')
        self.assertEqual(self.launcher.stopped, [])

    def test_no_heads_answer_supersedes_nothing(self):
        self.tick()
        self.gh.fail.add('graphql')
        self.assertGreater(self.tick(), 0)
        self.assertEqual(self.launcher.stopped, [])
        self.assertEqual(sorted(self.state['heads']), [MAIN, PR1])


class State(Case):
    """Runs again, stops half-way, old or empty state."""

    def test_runs_again_changes_nothing(self):
        self.tick()
        before = (copy.deepcopy(self.gh.checks), list(self.launcher.started))
        self.tick()
        self.assertEqual((self.gh.checks, self.launcher.started), before)

    def test_a_finished_head_is_not_run_again(self):
        self.tick()
        self.launcher.pass_all()
        self.tick()
        started = list(self.launcher.started)
        self.tick()
        self.assertEqual(self.launcher.started, started)
        self.assertEqual(len(self.gh.checks), 2)

    def test_empty_journal_with_a_completed_check_runs_nothing(self):
        self.tick()
        self.launcher.pass_all()
        self.tick()
        self.state = {'version': 1, 'heads': {}, 'history': []}
        started = list(self.launcher.started)
        self.tick()
        self.assertEqual(self.launcher.started, started)
        self.assertEqual(self.conclusion(MAIN), 'success')
        self.assertEqual(len(self.gh.checks), 2)

    def test_stopped_after_the_check_was_posted_the_head_runs_once_more_as_the_retry(self):
        self.launcher.refuse = True
        self.tick()                                       # the checks are posted; then the journal is lost
        self.launcher.refuse = False
        self.state = {'version': 1, 'heads': {}, 'history': []}
        self.tick()
        self.assertEqual(len(self.gh.checks), 2)           # the same check, not a second one
        self.assertTrue(all(n.endswith('-2') for n in self.launcher.started))
        for name in list(self.launcher.units):
            if name.startswith(f'lane-{PR1[:12]}-kit-1'):
                del self.launcher.units[name]
        self.launcher.pass_all()
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'neutral')  # a lost retry: no third run, Actions counts

    def test_a_check_that_cannot_be_posted_starts_nothing(self):
        self.gh.fail.add('create_check')
        self.assertGreater(self.tick(), 0)
        self.assertEqual(self.launcher.started, [])
        self.assertEqual(self.state['heads'], {})
        self.gh.fail.clear()
        self.tick()
        self.assertEqual(len(self.launcher.started), 6)

    def test_a_verdict_that_cannot_be_posted_is_posted_on_the_next_tick(self):
        self.tick()
        self.launcher.pass_all()
        self.gh.fail.add('update_check')
        self.tick()
        self.assertIn(PR1, self.state['heads'])
        self.gh.fail.clear()
        self.tick()
        self.assertEqual(self.conclusion(PR1), 'success')

    def test_broken_or_old_journal_files_are_read_as_empty(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, 'state.json')
            for text in ('{', '[]', '{"version": 0, "heads": {}, "history": []}'):
                with open(path, 'w') as f:
                    f.write(text)
                lines = []
                self.assertEqual(cli.read_state(d, lines.append), {'version': 1, 'heads': {}, 'history': []})
                self.assertIn('rebuild', lines[0])
            cli.write_state(d, self.state)
            self.assertEqual(cli.read_state(d, print), self.state)
            self.assertFalse(os.path.exists(path + '.tmp'))


class Command(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__('shutil').rmtree(self.dir))
        self.config = os.path.join(self.dir, 'config.json')
        self.write(CFG)

    def write(self, cfg):
        with open(self.config, 'w') as f:
            json.dump(cfg, f)

    def run_main(self, *args, gh=None, launcher=None):
        lines = []
        code = cli.main([*args, '--config', self.config, '--state-dir', os.path.join(self.dir, 's')],
                        gh=gh, launcher=launcher, now=T0, log=lines.append)
        return code, lines

    def test_bad_configs_are_refused(self):
        for bad in ({'repo': 'nope'}, {'mode': 'own'}, {'appId': 0}, {'auth': {'ghAs': 'gh-as'}},
                    {'launch': ['sudo']}, {'maxJobs': 0}, {'leaseMinutes': 999}, {'extra': 1},
                    {'checkName': 'a\nb'}):
            with self.subTest(bad=bad):
                self.write({**CFG, **bad})
                self.assertEqual(self.run_main('tick')[0], 2)

    def test_mode_off_runs_nothing(self):
        self.write({**CFG, 'mode': 'off'})
        gh = FakeGitHub()
        code, lines = self.run_main('tick', gh=gh, launcher=FakeLauncher())
        self.assertEqual((code, gh.calls), (0, []))

    def test_a_tick_writes_the_journal_and_status_shows_its_rows(self):
        gh, launcher = FakeGitHub(), FakeLauncher()
        self.assertEqual(self.run_main('tick', gh=gh, launcher=launcher)[0], 0)
        with mock.patch('builtins.print') as out:
            self.assertEqual(cli.main(['status', '--json', '--state-dir', os.path.join(self.dir, 's')]), 0)
        rows = json.loads(out.call_args[0][0])
        self.assertEqual(len(rows), 6)
        self.assertEqual({r['state'] for r in rows}, {'running'})

    def test_a_second_tick_while_one_holds_the_lock_does_nothing(self):
        import fcntl
        os.makedirs(os.path.join(self.dir, 's'))
        with open(os.path.join(self.dir, 's', 'lock'), 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            gh = FakeGitHub()
            code, lines = self.run_main('tick', gh=gh, launcher=FakeLauncher())
        self.assertEqual((code, gh.calls), (0, []))
        self.assertIn('another tick', lines[0])


class RedCheck(unittest.TestCase):
    """Each guard of the verdict, broken in turn: some Verdict or Lost test then fails."""

    def fails(self, target, replacement, tests):
        with mock.patch.object(lane, target, replacement):
            suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromName(t, sys.modules[__name__])
                                       for t in tests)
            result = unittest.TestResult()
            suite.run(result)
        self.assertTrue(result.failures or result.errors, f'{target} broken, yet {tests} pass')

    def test_a_judge_that_trusts_exit_0(self):
        self.fails('judge', lambda result, *a: ('passed', '') if result['exit'] == 0 else ('failed', ''),
                   ['Verdict.test_records_of_another_commit', 'Verdict.test_exit_0_without_records'])

    def test_a_verdict_that_counts_fallback_as_pass(self):
        real = lane.verdict
        self.fails('verdict', lambda head: ('success', 'x') if real(head)[0] == 'neutral' else real(head),
                   ['Lost.test_a_unit_gone_without_a_result_gets_one_retry_then_actions_counts'])

    def test_a_lose_that_retries_for_ever(self):
        def lose(job, why, log, sha):
            job.update(attempt=job['attempt'] + 1, state='queued', name=None, tries=0)
        self.fails('lose', lose, ['Lost.test_a_unit_gone_without_a_result_gets_one_retry_then_actions_counts'])


if __name__ == '__main__':
    unittest.main()
