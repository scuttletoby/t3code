"""ci-lane's pieces at their edges: the plan reader's refusals, the gh-as client and the launcher contract (the argv
and stdin it sends, what it accepts back), each on a fake subprocess. No network, no root.

  python3 -I -B fork-kit/ci-lane/tests/parts.test.py
"""
import json, os, subprocess, sys, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(KIT, 'lib'))
sys.dont_write_bytecode = True
from cilane import lane, plan as plans  # noqa: E402
from cilane.github import ApiError, GhAs  # noqa: E402

PART = {'name': 'kit', 'job': 'kit_tests', 'check': 'Kit {shard}', 'step': 'K', 'args': ['fork-kit/'],
        'shards': 2, 'lane': True}


def plan_text(*parts, **top):
    return json.dumps({'version': 1, 'workflow': '.github/workflows/fork-tests.yml', 'parts': list(parts), **top})


class Plan(unittest.TestCase):
    def test_a_good_plan(self):
        p = plans.parse(plan_text(PART, {**PART, 'name': 'w', 'check': 'W', 'shards': 1},
                                  {**PART, 'name': 'r', 'check': 'R', 'shards': 1, 'lane': False, 'why': 'root'}))
        self.assertEqual([j.key for j in p.jobs], ['kit:1', 'kit:2', 'w:1'])
        self.assertEqual([j.actions_check for j in p.jobs], ['Kit 1', 'Kit 2', 'W'])
        self.assertEqual(p.left, [('r', 'root')])

    def test_refused(self):
        bad = {
            'not JSON': '{',
            'version 2': plan_text(PART).replace('"version": 1', '"version": 2'),
            'an unknown top key': plan_text(PART, extra=1),
            'no parts': plan_text(),
            'an unknown part key': plan_text({**PART, 'env': 'x'}),
            'a name twice': plan_text(PART, PART),
            'a bad name': plan_text({**PART, 'name': 'Kit'}),
            'args not words': plan_text({**PART, 'args': ['a b']}),
            'no args': plan_text({**PART, 'args': []}),
            'shards 0': plan_text({**PART, 'shards': 0}),
            'shards and no {shard}': plan_text({**PART, 'check': 'Kit'}),
            '{shard} and no shards': plan_text({**PART, 'shards': 1}),
            'lane not a bool': plan_text({**PART, 'lane': 1}),
            'left without why': plan_text({**PART, 'lane': False}),
            'too large': plan_text({**PART, 'why': 'x' * 70000}),
        }
        for what, text in bad.items():
            with self.subTest(what), self.assertRaises(plans.PlanError):
                plans.parse(text)


class Recorder:
    def __init__(self, code=0, out='', err=''):
        self.code, self.out, self.err, self.calls = code, out, err, []

    def __call__(self, args, **kw):
        self.calls.append((args, kw))
        return subprocess.CompletedProcess(args, self.code, self.out, self.err)


class Client(unittest.TestCase):
    def test_calls_gh_as_for_the_one_repository_with_the_body_on_stdin(self):
        run = Recorder(out='{"id": 7}')
        gh = GhAs('/usr/local/bin/gh-as', '/home/lane', 'o/r', run=run)
        self.assertEqual(gh.create_check({'name': 'x'}), 7)
        args, kw = run.calls[0]
        self.assertEqual(args, ['/usr/local/bin/gh-as', '--repo', 'o/r', 'gh', 'api', '--method', 'POST',
                                'repos/o/r/check-runs', '--input', '-'])
        self.assertEqual(json.loads(kw['input']), {'name': 'x'})
        self.assertEqual(kw['env'], {'PATH': '/usr/bin:/bin', 'HOME': '/home/lane', 'LANG': 'C.UTF-8'})

    def test_errors_say_their_status_and_rate_limits(self):
        gh = GhAs('/g', '/h', 'o/r', run=Recorder(1, err='gh: API rate limit exceeded (HTTP 403)'))
        with self.assertRaises(ApiError) as e:
            gh.api('GET', 'x')
        self.assertTrue(e.exception.rate_limited)
        self.assertEqual(e.exception.status, 403)

    def test_a_missing_plan_file_is_none_and_other_errors_raise(self):
        self.assertIsNone(GhAs('/g', '/h', 'o/r', run=Recorder(1, err='Not Found (HTTP 404)')).file_at('p', 'a' * 40))
        with self.assertRaises(ApiError):
            GhAs('/g', '/h', 'o/r', run=Recorder(1, err='(HTTP 500)')).file_at('p', 'a' * 40)
        got = GhAs('/g', '/h', 'o/r', run=Recorder(out='{"encoding": "base64", "content": "aGk="}'))
        self.assertEqual(got.file_at('p', 'a' * 40), 'hi')

    def test_only_our_apps_checks_of_that_name_and_commit(self):
        sha = 'a' * 40
        runs = [{'id': 1, 'name': 'n', 'head_sha': sha, 'app': {'id': 5}},
                {'id': 2, 'name': 'n', 'head_sha': sha, 'app': {'id': 6}},      # another App: never ours
                {'id': 3, 'name': 'm', 'head_sha': sha, 'app': {'id': 5}},
                {'id': 4, 'name': 'n', 'head_sha': 'b' * 40, 'app': {'id': 5}}]
        gh = GhAs('/g', '/h', 'o/r', run=Recorder(out=json.dumps({'check_runs': runs})))
        self.assertEqual([r['id'] for r in gh.our_checks(sha, 'n', 5)], [1])

    def test_graphql_errors_raise(self):
        with self.assertRaises(ApiError):
            GhAs('/g', '/h', 'o/r', run=Recorder(out='{"errors": [{"message": "x"}]}')).graphql('q', {})


class Launcher(unittest.TestCase):
    JOB = plans.Job('kit', 2, 3, 'Kit {shard}', 'K')

    def test_start_sends_the_head_part_shard_and_attempt_on_stdin(self):
        run = Recorder()
        lane.Launcher(['/usr/bin/sudo', '-n', '/l'], run=run).start('lane-x-kit-2-1', 'a' * 40, self.JOB, 1)
        args, kw = run.calls[0]
        self.assertEqual(args, ['/usr/bin/sudo', '-n', '/l', 'lane-start', 'lane-x-kit-2-1'])
        self.assertEqual(kw['input'], f'FORK_LANE_SHA={"a" * 40}\nFORK_LANE_PART=kit\nFORK_LANE_SHARD=2/3\n'
                                      'FORK_LANE_ATTEMPT=1\n')

    def test_the_unit_name_fits_the_launchers_rule(self):
        import re
        name = lane.unit_name('a' * 40, plans.Job('a' * 24, 10, 10, 'x {shard}', 's'), 2)
        self.assertRegex(name, re.compile(r'^[a-z0-9][a-z0-9-]{0,62}-[0-9]{1,20}$'))

    def test_result_answers(self):
        ok = lane.Launcher(['/l'], run=Recorder(out='{"state": "done", "exit": 0, "records": {}}'))
        self.assertEqual(ok.result('n')['exit'], 0)
        for out in ('', 'x', '{"state": "maybe"}', '{"state": "done"}', '{"state": "done", "exit": "0"}'):
            with self.subTest(out=out), self.assertRaises(lane.LaunchError):
                lane.Launcher(['/l'], run=Recorder(out=out)).result('n')
        with self.assertRaises(lane.LaunchError):
            lane.Launcher(['/l'], run=Recorder(1, err='die')).result('n')


if __name__ == '__main__':
    unittest.main()
