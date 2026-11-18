"""Tests of laws.py, the law-test runner (run: python3 -I -B fork-ci/laws/laws.test.py)."""
import os
import sys
import unittest
import unittest.mock

sys.dont_write_bytecode = True
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import laws  # noqa: E402
from laws import Broken  # noqa: E402


def numbers(rng):
    return [rng.randint(0, 9) for _ in range(rng.randint(10, 30))]


def no_3_before_7(steps):
    """A law: a 7 never comes after a 3."""
    if 3 in steps and 7 in steps[steps.index(3):]:
        raise Broken('3 before 7', f'{len(steps)} steps')


class Runner(unittest.TestCase):
    def check(self, play=no_3_before_7, env=None, **kw):
        kw.setdefault('seed', 1)
        kw.setdefault('tries', 50)
        return laws.check('t', numbers, play, env=env or {}, rerun='python3 -I -B t.test.py', **kw)

    def test_a_failure_prints_the_seed_the_rerun_and_the_shortest_list(self):
        with self.assertRaises(AssertionError) as e:
            self.check()
        said = str(e.exception)
        self.assertIn('law broken: 3 before 7', said)
        self.assertIn("law test 't', seed 1, try ", said)
        self.assertRegex(said, r'run this try again: LAWS_SEED=1 LAWS_TRY=\d+ LAWS_TRIES=50 python3 -I -B t.test.py')
        self.assertIn('(2 of ', said)   # shrunk to the two steps that break it
        self.assertIn('        3,\n        7,\n', said)
        self.assertIn('shrinking finished', said)
        self.assertIn('README.md, "A failure"', said)

    def test_the_rerun_line_plays_only_that_try_and_fails_the_same_way(self):
        with self.assertRaises(AssertionError) as e:
            self.check()
        i = int(str(e.exception).split('LAWS_TRY=')[1].split()[0])
        played = []
        with self.assertRaises(AssertionError) as again:
            self.check(lambda s: (played.append(s), no_3_before_7(s)), env={'LAWS_SEED': '1', 'LAWS_TRY': str(i),
                                                                           'LAWS_TRIES': '50'})
        self.assertEqual(played[0], numbers(laws.rng_for('t', 1, i)))
        self.assertEqual(str(again.exception), str(e.exception))

    def test_the_same_seed_gives_the_same_steps_and_another_try_others(self):
        self.assertEqual(numbers(laws.rng_for('t', 5, 3)), numbers(laws.rng_for('t', 5, 3)))
        self.assertNotEqual(numbers(laws.rng_for('t', 5, 3)), numbers(laws.rng_for('t', 5, 4)))
        self.assertNotEqual(numbers(laws.rng_for('t', 5, 3)), numbers(laws.rng_for('u', 5, 3)))

    def test_tries_are_capped_and_counted(self):
        self.assertEqual(self.check(lambda s: None, tries=7), 7)
        self.assertEqual(self.check(lambda s: None, tries=7, env={'LAWS_TRIES': '3'}), 3)
        self.assertEqual(self.check(lambda s: None, tries=7, env={'LAWS_TRY': '2'}), 1)
        with self.assertRaises(ValueError):
            laws.settings(1, 1, {'LAWS_TRIES': str(laws.MAX_TRIES + 1)})

    def test_a_bad_setting_fails_never_fewer_tries(self):
        for env in ({'LAWS_SEED': 'x'}, {'LAWS_SEED': '-1'}, {'LAWS_TRIES': '0'}, {'LAWS_TRIES': '2.5'},
                    {'LAWS_TRIES': ' '}, {'LAWS_TRY': '7'}, {'LAWS_TRY': '3', 'LAWS_TRIES': '3'}):
            with self.subTest(env), self.assertRaises(ValueError):
                self.check(lambda s: None, tries=5, env=env)
        self.assertEqual(laws.settings(9, 4, {'LAWS_SEED': '', 'LAWS_TRIES': ''}), (9, 4, None))

    def test_shrinking_keeps_the_same_law(self):
        def play(steps):   # a shorter list breaks another law: the report must still show the first one
            if len(steps) < 6:
                raise Broken('short list')
            no_3_before_7(steps)
        with self.assertRaises(AssertionError) as e:
            self.check(play)
        self.assertIn('law broken: 3 before 7', str(e.exception))
        self.assertIn('(6 of ', str(e.exception))

    def test_shrinking_is_capped(self):
        with self.assertRaises(AssertionError) as e:
            self.check(runs=2)
        self.assertIn('shrinking stopped at its cap: a shorter list may exist', str(e.exception))
        clock = iter(range(0, 1000, 10)).__next__   # each look at the clock: 10 s later
        best, error, done = laws.shrink(no_3_before_7, [1, 3, 2, 7, 5, 5, 5, 5], '3 before 7', seconds=15, clock=clock)
        self.assertFalse(done)

    def test_a_crash_counts_as_a_failure_keyed_by_its_type(self):
        def play(steps):
            if 3 in steps:
                raise KeyError('x')
        with self.assertRaises(AssertionError) as e:
            self.check(play)
        self.assertIn("law broken: 'x'", str(e.exception))
        self.assertIn('(1 of ', str(e.exception))

    def test_it_never_sleeps(self):
        with unittest.mock.patch('time.sleep', side_effect=AssertionError('slept')):
            with self.assertRaises(AssertionError) as e:
                self.check()
        self.assertIn('law broken', str(e.exception))

    def test_coverage_names_what_the_seed_no_longer_reaches(self):
        c = laws.Coverage(('a', 'b', 'c'))
        c.saw('a')
        c.saw('c')
        with self.assertRaises(AssertionError) as e:
            c.check()
        self.assertIn('no longer reach: b', str(e.exception))
        c.saw('b')
        c.check()


if __name__ == '__main__':
    unittest.main()
