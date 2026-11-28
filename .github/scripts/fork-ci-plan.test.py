#!/usr/bin/env python3
"""The plan file (.github/fork-ci-plan.json) and Fork tests (.github/workflows/fork-tests.yml) name the same parts.

The plan file is what the own CI lane (fork-kit/ci-lane, issue #1157 phase 2) runs: each part is one
`node .github/scripts/fork-ci-tests.cjs ARGS` call of fork-tests.yml, with the job, the job's check name and the
step it is in, and its shards. One plan, two runners: a call added, changed, moved or resharded in the workflow
without the plan (or the other way round) fails here, so the lane can never run a different set of tests than
Actions and still be compared with it. The lane reads the plan at the commit it tests, through the kit's own
reader, which must accept this file too. Run: python3 <this>.
"""
import copy, json, os, re, shlex, sys, unittest

import yaml

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
PLAN = os.path.join(ROOT, '.github', 'fork-ci-plan.json')
sys.path.insert(0, os.path.join(ROOT, 'fork-kit', 'ci-lane', 'lib'))
from cilane import plan as kit_plan  # noqa: E402

CALL = 'node .github/scripts/fork-ci-tests.cjs'
SHARD_ENV = '${{ matrix.shard }}/${{ strategy.job-total }}'


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def workflow_calls(text):
    """Every fork-ci-tests.cjs call of the workflow: (job, check, step, args, shards). A call this reading cannot
    take apart (two in one step, one not at the start of its line but after VAR=value words) is an error, never
    skipped: a call the test does not see would be a call the plan can miss."""
    jobs = yaml.safe_load(text)['jobs']
    calls = []
    for key, job in jobs.items():
        name = job.get('name', key)
        matrix = ((job.get('strategy') or {}).get('matrix') or {})
        shards = matrix.get('shard')
        for step in job.get('steps') or []:
            run = step.get('run')
            if not isinstance(run, str) or CALL not in run:
                continue
            if run.count(CALL) != 1 or '\n' in run.strip():
                raise ValueError(f'{key}: step {step.get("name")!r}: a fork-ci-tests.cjs call this test cannot read')
            before, after = run.strip().split(CALL)
            if not re.fullmatch(r'(?:[A-Z_][A-Z0-9_]*=(?:"[^"]*"|[^\s"]*) +)*', before):
                raise ValueError(f'{key}: step {step.get("name")!r}: words before the call that are not VAR=value')
            args = shlex.split(after)
            sharded = (step.get('env') or {}).get('FORK_CI_TEST_SHARD')
            if sharded is not None and sharded != SHARD_ENV:
                raise ValueError(f'{key}: FORK_CI_TEST_SHARD is not {SHARD_ENV}')
            if sharded is not None and not isinstance(shards, list):
                raise ValueError(f'{key}: FORK_CI_TEST_SHARD without a matrix of shards')
            count = len(shards) if sharded is not None else 1
            if sharded is not None and shards != list(range(1, count + 1)):
                raise ValueError(f'{key}: the shard matrix is not 1..N')
            check = name.replace('${{ matrix.shard }}', '{shard}') if sharded is not None else name
            calls.append((key, check, step.get('name'), tuple(args), count))
    return calls


def plan_calls(plan):
    return [(p['job'], p['check'], p['step'], tuple(p['args']), p.get('shards', 1)) for p in plan['parts']]


def differences(plan, text):
    """What one side has and the other has not, as two sorted lists (empty, empty: the same)."""
    ours, theirs = plan_calls(plan), workflow_calls(text)
    return sorted(set(ours) - set(theirs)), sorted(set(theirs) - set(ours))


class Same(unittest.TestCase):
    def setUp(self):
        self.plan = json.loads(read(PLAN))
        self.text = read(os.path.join(ROOT, self.plan['workflow']))

    def test_the_plan_and_the_workflow_name_the_same_calls(self):
        self.assertEqual(differences(self.plan, self.text), ([], []))

    def test_each_call_is_in_the_plan_once(self):
        calls = plan_calls(self.plan)
        self.assertEqual(len(calls), len(set(calls)))
        self.assertEqual(len(calls), len(workflow_calls(self.text)))

    def test_the_kit_reads_the_plan(self):
        got = kit_plan.parse(read(PLAN))
        self.assertTrue(got.jobs)
        names = [p['name'] for p in self.plan['parts'] if p['lane']]
        self.assertEqual(sorted({j.part for j in got.jobs}), sorted(names))

    def test_every_part_the_lane_leaves_says_why(self):
        for p in self.plan['parts']:
            self.assertEqual(p['lane'] is False, bool(p.get('why')), p['name'])


class RedCheck(unittest.TestCase):
    """Each change on one side only is seen."""

    def setUp(self):
        self.plan = json.loads(read(PLAN))
        self.text = read(os.path.join(ROOT, self.plan['workflow']))

    def assertDiffers(self, plan, text):
        self.assertNotEqual(differences(plan, text), ([], []))

    def test_a_new_call_in_the_workflow(self):
        at = "        run: node .github/scripts/fork-ci-tests.cjs fork-kit/\n"
        self.assertEqual(self.text.count(at), 1)
        extra = at + "      - name: More\n        run: node .github/scripts/fork-ci-tests.cjs --run x.test.py\n"
        self.assertDiffers(self.plan, self.text.replace(at, extra))

    def test_changed_args_in_the_workflow(self):
        at = 'fork-ci-tests.cjs fork-ci/ .github/scripts/fork-fleet-walls'
        self.assertEqual(self.text.count(at), 1)
        self.assertDiffers(self.plan, self.text.replace(at, 'fork-ci-tests.cjs fork-ci/'))

    def test_another_shard_count(self):
        at = '        shard: [1, 2, 3]\n    steps:\n      # The same files as in `workflows`'
        self.assertEqual(self.text.count(at), 1)
        self.assertDiffers(self.plan, self.text.replace(at, at.replace('3]', '3, 4]')))

    def test_a_renamed_step_or_job(self):
        self.assertDiffers(self.plan, self.text.replace('- name: Fork kit tests\n', '- name: Kit tests\n'))
        self.assertDiffers(self.plan, self.text.replace('name: Fork kit tests ${{ matrix.shard }}',
                                                        'name: Kit ${{ matrix.shard }}'))

    def test_a_part_only_in_the_plan(self):
        plan = copy.deepcopy(self.plan)
        plan['parts'].append({**plan['parts'][0], 'name': 'extra', 'args': ['extra/']})
        self.assertDiffers(plan, self.text)

    def test_a_call_this_test_cannot_read_is_an_error(self):
        at = "        run: node .github/scripts/fork-ci-tests.cjs fork-kit/\n"
        for run in ('        run: node .github/scripts/fork-ci-tests.cjs a/ && '
                    'node .github/scripts/fork-ci-tests.cjs b/\n',
                    '        run: cd x && node .github/scripts/fork-ci-tests.cjs fork-kit/\n',
                    '        run: |\n          true\n          node .github/scripts/fork-ci-tests.cjs fork-kit/\n'):
            with self.subTest(run=run), self.assertRaises(ValueError):
                workflow_calls(self.text.replace(at, run))


if __name__ == '__main__':
    unittest.main()
