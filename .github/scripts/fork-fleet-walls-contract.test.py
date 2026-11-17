#!/usr/bin/env python3
"""The contract every walls file in fork-fleet-walls/ must pass (fork-fleet-walls.py's docstring), on fake machines
from its <os>.fixture.json: usage, an empty machine, the passing fixture and every failing change, through the walls
file and through fork-fleet-walls.py. Split from fork-fleet-walls.test.py, whose helpers it uses, so the test runner's
workers run the two side by side. A new walls file (and its fixture) is tested here without a change to this file.
Run: python3 <this>. Linux only."""
import importlib.util, json, os, sys, unittest

sys.dont_write_bytecode = True
_spec = importlib.util.spec_from_file_location(
    'fork_fleet_walls_test', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fork-fleet-walls.test.py'))
walls_test = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(walls_test)
FOLDER, OSES, fw = walls_test.FOLDER, walls_test.OSES, walls_test.fw
fixture_problems, overlay, run, slurp = (walls_test.fixture_problems, walls_test.overlay, walls_test.run,
                                         walls_test.slurp)


class EveryFixture(walls_test.WallsRunner):
    def test_contract(self):
        for name in OSES:
            with self.subTest(name):
                fixture = json.loads(slurp(os.path.join(FOLDER, f'{name}.fixture.json')))
                for args in ([], ['start'], ['check', 'describe']):   # usage: exit 2, nothing on stdout
                    p = run([os.path.join(self.folder, name), *args], self.root({}))
                    self.assertEqual((p.returncode, p.stdout), (2, ''), f'{name} {args}')
                ids, fails = self.both(name, self.root({}))   # an empty machine fails closed, `os` among them
                self.assertIn('os', fails)
                good = self.root(fixture['pass'])
                ids, fails = self.both(name, good)
                self.assertEqual(fails, [], f'{name}: walls fail on its passing fixture')
                self.assertEqual(fw.detect(good), name, f'{name}: its passing fixture is detected as another OS')
                p = run([self.install(), 'check'], good)   # and through fork-fleet-walls.py
                self.assertEqual((p.returncode, p.stdout), (0, ''), p.stderr)
                self.assertEqual(fixture_problems(fixture, ids), [], name)
                for wall, changes in fixture['fail'].items():
                    for change in changes if isinstance(changes, list) else [changes]:
                        with self.subTest(name=name, wall=wall, change=change):
                            root = self.root(overlay(fixture['pass'], change))
                            self.assertIn(wall, self.both(name, root)[1])
                            p = run([self.install(), 'check'], root)
                            self.assertIn(p.returncode, (1, 3), p.stderr)   # 3: the change made it another OS


if __name__ == '__main__':
    unittest.main()
