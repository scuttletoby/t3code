"""`homelab check` and the homelab section of the machine file (lib/homelabkit/config.py, main.py): a machine file
in a temporary folder and machines on 127.0.0.1 (one port that listens, one that does not). No root, no network
beyond this machine.

  python3 -I -B fork-kit/homelab/tests/check.test.py

The state question, answered here: a missing machine file, a file without a homelab section, a section with a typo
or a bad value (refused, never ignored), the example file (it must load), and a check that meets a machine that
does not answer, a lease folder that is not there and a damaged lease file (each reported, exit 1).
RedCheck breaks each rule in a copy of lib/homelabkit/ and shows its test then fails.
"""
import json, os, shutil, socket, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from homelab_fixture import COMMON, Fixture, red_check  # noqa: E402
from homelabkit import config  # noqa: E402

THIS = os.path.abspath(__file__)


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class MachineFile(Fixture):
    def refused(self, *words):
        code, out, err = self.run_cli('check')
        self.assertEqual((code, out), (2, ''))
        for w in words:
            self.assertIn(w, err)
        return err

    def test_a_missing_machine_file_is_refused_and_named(self):
        err = self.refused(self.machine_path, 'not there')
        self.assertIn('fork-kit/common/README.md', err)
        code, _, err = self.run_cli('lease', 'take', 'test-a', '--holder', 'p')
        self.assertEqual(code, 2)
        self.assertIn(self.machine_path, err)

    def test_no_homelab_section_is_refused(self):
        self.write_machine_file(data={'version': 1, 'heavy': {}})
        self.refused('no "homelab" section')

    def test_the_machine_file_rules_apply(self):
        self.write_machine_file(mode=0o666)
        self.refused('writable by group or others')

    def test_typos_and_bad_values_are_refused(self):
        m = {'name': 'test-a', 'host': '127.0.0.1'}
        cases = [({'machines': [dict(m, prot=22)]}, "unknown key 'prot'"),
                 ({'machines': [dict(m, port=70000)]}, 'port'),
                 ({'machines': [dict(m, name='Test A')]}, 'name'),
                 ({'machines': [dict(m, host='-oProxy')]}, 'host'),
                 ({'machines': [m, dict(m)]}, 'given twice'),
                 ({'machines': []}, 'at least one machine'),
                 ({'leases': {'dir': 'leases'}}, 'absolute'),
                 ({'leases': {'dir': self.lease_dir, 'defaultMinutes': 300, 'maxMinutes': 240}},
                  'defaultMinutes'),
                 ({'checkTimeoutSeconds': 0}, 'checkTimeoutSeconds')]
        for over, words in cases:
            with self.subTest(over=over):
                self.write_machine_file(self.section(**over))
                self.refused(words)

    def test_the_example_file_loads(self):
        with open(os.path.join(COMMON, 'machine.example.json')) as f:
            example = json.load(f)
        self.write_machine_file(data={'version': 1, 'homelab': example['homelab']})
        s = config.load(self.machine_path, os.getuid())
        self.assertTrue(s['machines'])
        for m in s['machines']:
            self.assertTrue(m['host'].endswith('.example') or m['host'].startswith('192.0.2.'), m['host'])


class Check(Fixture):
    def setUp(self):
        super().setUp()
        self.server = socket.socket()
        self.server.bind(('127.0.0.1', 0))
        self.server.listen(8)
        self.addCleanup(self.server.close)
        self.up = {'name': 'test-a', 'host': '127.0.0.1', 'port': self.server.getsockname()[1]}
        self.down = {'name': 'test-b', 'host': '127.0.0.1', 'port': closed_port()}

    def test_all_reachable_is_ok_and_shows_leases(self):
        self.write_machine_file(self.section(machines=[self.up]))
        self.assertEqual(self.run_cli('lease', 'take', 'test-a', '--holder', 'project-a')[0], 0)
        code, out, err = self.run_cli('check')
        self.assertEqual((code, err), (0, ''))
        self.assertIn(self.machine_path, out)
        self.assertRegex(out, r'test-a +127\.0\.0\.1:\d+ +reachable +leased to project-a \(\S+\) until')
        self.assertIn('homelab check: ok', out)

    def test_a_machine_that_does_not_answer_is_a_problem(self):
        self.write_machine_file(self.section(machines=[self.up, self.down], checkTimeoutSeconds=2))
        code, out, _ = self.run_cli('check')
        self.assertEqual(code, 1)
        self.assertRegex(out, r'test-b +127\.0\.0\.1:\d+ +not reachable')
        self.assertIn('homelab check: 1 problem', out)
        code, out, _ = self.run_cli('check', '--json')
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertFalse(report['ok'])
        self.assertEqual({m['name']: m['reachable'] for m in report['machines']}, {'test-a': True, 'test-b': False})

    def test_lease_folder_and_lease_file_problems_are_reported(self):
        self.write_machine_file(self.section(machines=[self.up]))
        with open(os.path.join(self.lease_dir, 'test-a.json'), 'w') as f:
            f.write('not json')
        code, out, _ = self.run_cli('check', '--json')
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIn('test-a.json', report['machines'][0]['leaseProblem'])
        shutil.rmtree(self.lease_dir)
        code, out, _ = self.run_cli('check', '--json')
        self.assertEqual(code, 1)
        self.assertIn('not there', json.loads(out)['leaseDir']['problem'])


MUTATIONS = {
    'a missing machine file is refused, not a crash': (
        'main', 'except (config.MachineFileError, leases.LeaseError) as e:', 'except leases.LeaseError as e:',
        'MachineFile.test_a_missing_machine_file_is_refused_and_named'),
    'a machine name once': ('config', "if m['name'] in seen:", 'if False:', 'MachineFile.test_typos_and_bad_values_are_refused'),
    'a host is never an option': ('config', "HOST = re.compile(r'[A-Za-z0-9_]", "HOST = re.compile(r'[A-Za-z0-9_-]",
                                  'MachineFile.test_typos_and_bad_values_are_refused'),
    'defaultMinutes within maxMinutes': ('config', "if leases['defaultMinutes'] > leases['maxMinutes']:", 'if False:',
                                         'MachineFile.test_typos_and_bad_values_are_refused'),
    'a machine that does not answer is a problem': (
        'main', 'problems += (unreachable is not None) + (lease_problem is not None)',
        'problems += (lease_problem is not None)', 'Check.test_a_machine_that_does_not_answer_is_a_problem'),
    'a damaged lease file is a problem': (
        'main', 'problems += (unreachable is not None) + (lease_problem is not None)',
        'problems += (unreachable is not None)', 'Check.test_lease_folder_and_lease_file_problems_are_reported'),
    'a lease folder problem is a problem': (
        'main', 'folder_problem is None else 1', 'folder_problem is None else 0', 'Check.test_lease_folder_and_lease_file_problems_are_reported'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_failing_test(self):
        red_check(self, THIS, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
