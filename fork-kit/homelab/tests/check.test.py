"""homelab check and the machine file's homelab section, in a temporary folder; machines are ports on 127.0.0.1.
No root. RedCheck breaks each guard in a copy of the kit and shows its test then fails.

  python3 -I -B fork-kit/homelab/tests/check.test.py
"""
import ipaddress, json, os, socket, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import homelab_fixture as hf  # noqa: E402

config = hf.config


def listening():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    s.listen(4)
    return s


def closed_port():
    s = socket.socket()
    s.bind(('127.0.0.1', 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Check(hf.Temp):

    def setUp(self):
        super().setUp()
        self.server = listening()
        self.addCleanup(self.server.close)
        self.up = dict(hf.TEST_BOX, host='127.0.0.1', port=self.server.getsockname()[1])

    def test_a_good_file_a_lease_folder_and_machines_that_answer_pass(self):
        self.write_machine(self.up, dict(hf.BUILD_BOX, host='127.0.0.1', port=self.server.getsockname()[1]))
        code, out, _ = self.run_cli('check')
        self.assertEqual(code, 0, out)
        lines = out.splitlines()
        self.assertEqual(lines[:3], ['ok   machine file: 2 machines, 1 can be leased',
                                     f'ok   lease folder {self.leases}: you can take leases',
                                     'ok   test-box: free'])
        self.assertRegex(lines[3], rf'^ok   test-box: 127\.0\.0\.1:{self.up["port"]} answers \(\d+ ms\)$')
        self.assertRegex(lines[4], r'^ok   build-box: .* answers')

    def test_a_machine_that_does_not_answer_fails_the_check(self):
        down = dict(hf.BUILD_BOX, host='127.0.0.1', port=closed_port())
        self.write_machine(self.up, down)
        code, out, _ = self.run_cli('check', '--timeout', '2')
        self.assertEqual(code, 1)
        self.assertIn(f'FAIL build-box: 127.0.0.1:{down["port"]} does not answer', out)
        self.assertIn('ok   test-box', out)

    def test_no_reach_tries_no_machine(self):
        self.write_machine()

        def connect(*a):
            raise AssertionError('connected')
        code, out, _ = self.run_cli('check', '--no-reach', connect=connect)
        self.assertEqual(code, 0, out)
        self.assertNotIn('answer', out)

    def test_a_held_lease_is_shown(self):
        self.write_machine(self.up)
        self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 0)
        self.assertIn('ok   test-box: leased by proj-a/agent-1 until 2026-11-27T09:00:00Z', out)

    def test_a_missing_or_broken_lease_folder_or_lease_fails_the_check(self):
        self.write_machine(self.up)
        with open(os.path.join(self.leases, 'test-box.json'), 'w') as f:
            f.write('{}')
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 1)
        self.assertIn('test-box.json: not a lease this kit wrote', out)
        os.unlink(os.path.join(self.leases, 'test-box.json'))
        os.rmdir(self.leases)
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 1)
        self.assertIn(f'FAIL lease folder {self.leases} is not there', out)

    @unittest.skipIf(os.getuid() == 0, 'root can write every folder')
    def test_a_lease_folder_this_user_cannot_write_fails_the_check(self):
        self.write_machine(self.up)
        os.chmod(self.leases, 0o555)
        self.addCleanup(os.chmod, self.leases, 0o755)
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 1)
        self.assertIn('you cannot write it; the owner adds you to its group', out)

    def test_no_lease_folder_is_needed_when_no_machine_can_be_leased(self):
        self.write_machine(hf.BUILD_BOX, leaseDir=os.path.join(self.tmp, 'none'))
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual((code, out), (0, 'ok   machine file: 1 machine, 0 can be leased\n'))

    def test_a_missing_machine_file_fails_and_says_where_to_read_how(self):
        code, out, _ = self.run_cli('check')
        self.assertEqual(code, 1)
        self.assertEqual(out.splitlines()[0][:5], 'FAIL ')
        self.assertIn(f'{self.machine}: not there. The owner writes it as root', out)
        self.assertIn('fork-kit/homelab/README.md', out)

    def test_a_machine_file_anyone_can_write_fails(self):
        self.write_machine(self.up)
        os.chmod(self.machine, 0o666)
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 1)
        self.assertIn('is writable by group or others', out)


    def test_a_lease_folder_anyone_can_write_fails_the_check(self):
        self.write_machine(self.up)
        os.chmod(self.leases, 0o777)
        code, out, _ = self.run_cli('check', '--no-reach')
        self.assertEqual(code, 1)
        self.assertIn(f'FAIL lease folder {self.leases} is writable by everyone', out)

    def test_check_json_gives_each_finding_for_scripts(self):
        self.write_machine(self.up, hf.BUILD_BOX)

        def connect(where, timeout):
            if where[0] != '127.0.0.1':
                raise OSError(111, 'Connection refused')
            return socket.socket()
        code, out, _ = self.run_cli('check', '--json', '--timeout', '2', connect=connect)
        self.assertEqual(code, 1)
        report = json.loads(out)
        self.assertIs(report['ok'], False)
        rows = [(c['ok'], c['what'], c['machine']) for c in report['checks']]
        self.assertEqual(rows, [(True, 'machineFile', None), (True, 'leaseDir', None), (True, 'lease', 'test-box'),
                                (True, 'reach', 'test-box'), (False, 'reach', 'build-box')])
        self.assertIn('does not answer', report['checks'][-1]['text'])
        code, out, _ = self.run_cli('check', '--json', '--no-reach')
        self.assertEqual((code, json.loads(out)['ok']), (0, True))

    def test_a_timeout_out_of_range_is_a_usage_error(self):
        self.write_machine(self.up)
        for t in ('-1', '0', '0.5', '61', 'nan', 'inf', 'x'):
            with self.subTest(timeout=t):
                with self.assertRaises(SystemExit) as cm:
                    self.run_cli('check', '--timeout', t)
                self.assertEqual(cm.exception.code, 2)
        for t in ('1', '60'):
            self.assertEqual(self.run_cli('check', '--no-reach', '--timeout', t)[0], 0)

class Section(hf.Temp):

    def refused(self, why, *machines, **section):
        self.write_machine(*machines, **section)
        with self.assertRaises(config.MachineFileError) as cm:
            config.load()
        self.assertIn(why, str(cm.exception))

    def test_a_good_section_reads_with_its_defaults(self):
        self.write_machine()
        cfg = config.load()
        self.assertEqual(cfg['machines'][0], dict(hf.TEST_BOX, port=22, note=''))
        self.assertEqual(cfg['machines'][1]['lease'], None)

    def test_mistakes_are_refused_with_the_key_named(self):
        cases = [
            ("unknown key 'hots'", dict(hf.BUILD_BOX, hots='x')),
            ('"host" is missing', {'name': 'x'}),
            ('machines[0].host: must be a host name or an IP address', dict(hf.BUILD_BOX, host='a b')),
            ('machines[0].port: must be 1 to 65535', dict(hf.BUILD_BOX, port=0)),
            ('machines[0].port: must be int', dict(hf.BUILD_BOX, port='22')),
            ('machines[0].name: must be lower-case', dict(hf.BUILD_BOX, name='Box')),
            ('machines[0].roles: a role is given twice', dict(hf.BUILD_BOX, roles=['a', 'a'])),
            ('lease.defaultMinutes: more than maxMinutes', dict(hf.TEST_BOX, lease={'defaultMinutes': 90,
                                                                                    'maxMinutes': 60})),
            ("unknown key 'max'", dict(hf.TEST_BOX, lease={'max': 60})),
        ]
        for why, m in cases:
            with self.subTest(why=why):
                self.refused(why, m)

    def test_a_name_given_twice_is_refused(self):
        self.refused("the name 'build-box' is given twice", hf.BUILD_BOX, dict(hf.BUILD_BOX, host='192.0.2.21'))

    def test_a_leasable_machine_needs_a_lease_folder(self):
        self.write_machine(hf.TEST_BOX)
        with open(self.machine) as f:
            data = json.load(f)
        del data['homelab']['leaseDir']
        with open(self.machine, 'w') as f:
            json.dump(data, f)
        with self.assertRaises(config.MachineFileError) as cm:
            config.load()
        self.assertIn('"leaseDir" is missing (a machine has "lease")', str(cm.exception))
        self.refused('leaseDir: must be an absolute path', hf.TEST_BOX, leaseDir='leases')

    def test_the_example_section_parses_and_names_no_real_machine(self):
        with open(os.path.join(os.path.dirname(hf.KIT), 'common', 'machine.example.json')) as f:
            data = json.load(f)
        cfg = config.parse(data['homelab'], 'example')
        self.assertTrue(any(m['lease'] for m in cfg['machines']))
        for m in cfg['machines']:
            try:
                ip = ipaddress.ip_address(m['host'])
            except ValueError:
                self.assertTrue(m['host'].endswith('.example'), m['host'])       # RFC 2606 names only
            else:
                self.assertTrue(any(ip in ipaddress.ip_network(n) for n in
                                    ('192.0.2.0/24', '198.51.100.0/24', '203.0.113.0/24')), m['host'])  # RFC 5737


MUTATIONS = {
    'a machine that does not answer fails': (
        'check', "        return _row(False, 'reach',", "        return _row(True, 'reach',",
        'Check.test_a_machine_that_does_not_answer_fails_the_check'),
    'a missing machine file fails': (
        'check', "        return [_row(False, 'machineFile',", "        return [_row(True, 'machineFile',",
        'Check.test_a_missing_machine_file_fails_and_says_where_to_read_how'),
    'the lease folder must be writable': (
        'check', 'if lease.writable(d, dir_fd):', 'if True:',
        'Check.test_a_lease_folder_this_user_cannot_write_fails_the_check'),
    'check reports a folder anyone can write': (
        'lease', '    if os.fstat(fd).st_mode & stat.S_IWOTH:', '    if False:',
        'Check.test_a_lease_folder_anyone_can_write_fails_the_check'),
    'the timeout is bounded': (
        'cli', '    if not lo <= t <= hi:', '    if False:',
        'Check.test_a_timeout_out_of_range_is_a_usage_error'),
    'names are unique': (
        'config', '    if twice:\n', '    if False:\n', 'Section.test_a_name_given_twice_is_refused'),
    'a leasable machine needs a folder': (
        'config', "if any(m['lease'] for m in s['machines']) and not s['leaseDir']:", 'if False:',
        'Section.test_a_leasable_machine_needs_a_lease_folder'),
    'default within max': (
        'config', "    if s['defaultMinutes'] > s['maxMinutes']:", '    if False:',
        'Section.test_mistakes_are_refused_with_the_key_named'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        hf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
