"""homelab lease: take, renew, release and list test-machine leases, in a temporary lease folder with a fixed clock.
No root, no network. RedCheck breaks each guard in a copy of the kit and shows its test then fails.

  python3 -I -B fork-kit/homelab/tests/lease.test.py
"""
import json, os, stat, subprocess, sys, textwrap, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import homelab_fixture as hf  # noqa: E402

NOW = hf.NOW
HOUR = 3600


class Take(hf.Temp):

    def setUp(self):
        super().setUp()
        self.write_machine()

    def test_a_free_machine_is_leased_for_its_default_time(self):
        code, out, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1', '--note', 'e2e')
        self.assertEqual(code, 0, err)
        self.assertIn('test-box: leased to proj-a/agent-1 until 2026-11-27T09:00:00Z (60 min left)', out)
        got = self.lease_file()
        self.assertEqual((got['holder'], got['since'], got['until'], got['note']), ('proj-a/agent-1', NOW,
                                                                                    NOW + HOUR, 'e2e'))
        mode = stat.S_IMODE(os.stat(os.path.join(self.leases, 'test-box.json')).st_mode)
        self.assertEqual(mode, 0o664)                 # the other agents of the group replace it later
        self.assertEqual([n for n in os.listdir(self.leases) if n.endswith('.tmp')], [])

    def test_a_lease_taken_twice_by_another_holder_is_refused_and_kept(self):
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')[0], 0)
        before = self.lease_file()
        code, out, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-b/agent-7', now=NOW + 60)
        self.assertEqual(code, 3, out + err)          # HELD: a script can wait and try again
        self.assertIn('test-box is leased by proj-a/agent-1 until 2026-11-27T09:00:00Z', err)
        self.assertEqual(self.lease_file(), before)

    def test_a_lease_taken_twice_by_its_holder_is_renewed(self):
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')[0], 0)
        code, out, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1', '--minutes', '30',
                                      now=NOW + 50 * 60)
        self.assertEqual(code, 0, err)
        self.assertIn('test-box: renewed to proj-a/agent-1 until 2026-11-27T09:20:00Z (30 min left)', out)
        got = self.lease_file()
        self.assertEqual((got['since'], got['until']), (NOW, NOW + 80 * 60))   # held since the first take

    def test_a_lease_that_expires_is_free_for_another_holder(self):
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')[0], 0)
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-b/agent-7',
                                      now=NOW + HOUR - 1)[0], 3)      # one second before its end: still held
        code, out, _ = self.run_cli('lease', 'list', now=NOW + HOUR)
        self.assertIn('test-box: free', out)
        code, out, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-b/agent-7', now=NOW + HOUR)
        self.assertEqual(code, 0, err)
        self.assertIn('test-box: the lease of proj-a/agent-1 ended at 2026-11-27T09:00:00Z', out)
        self.assertIn('test-box: leased to proj-b/agent-7 until 2026-11-27T10:00:00Z', out)
        got = self.lease_file()
        self.assertEqual((got['holder'], got['since']), ('proj-b/agent-7', NOW + HOUR))

    def test_the_old_holder_after_expiry_takes_a_new_lease_not_a_renewal(self):
        self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')
        code, out, _ = self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1', now=NOW + 2 * HOUR)
        self.assertEqual(code, 0)
        self.assertIn('leased to', out)
        self.assertEqual(self.lease_file()['since'], NOW + 2 * HOUR)

    def test_two_takers_at_once_never_both_win(self):
        # Each taker sleeps between reading the lease and writing its own: without the lock all of them would read
        # "free" and all would win.
        child = textwrap.dedent(f'''
            import sys, time
            sys.path.insert(0, {hf.LIB!r})
            from homelab import cli, config, lease
            config.MACHINE_PATH, config.OWNER_UID = {self.machine!r}, {os.getuid()}
            read = lease._read
            def slow(d, name):
                got = read(d, name)
                time.sleep(0.3)
                return got
            lease._read = slow
            sys.exit(cli.main(['lease', 'take', 'test-box', '--holder', sys.argv[1]], now={NOW}))
        ''')
        procs = [subprocess.Popen([sys.executable, '-I', '-B', '-c', child, f'proj-{i}/agent'],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for i in range(4)]
        codes = sorted(p.wait(timeout=60) for p in procs)
        self.assertEqual(codes, [0, 3, 3, 3])
        self.assertIn(self.lease_file()['holder'], {f'proj-{i}/agent' for i in range(4)})

    def test_more_than_the_machines_most_minutes_is_refused(self):
        code, _, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'a', '--minutes', '241')
        self.assertEqual(code, 1)
        self.assertIn('a lease is 1 to 240 minutes (asked: 241)', err)
        self.assertEqual(os.listdir(self.leases), [])
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'a', '--minutes', '0')[0], 1)

    def test_a_machine_without_lease_rules_or_not_in_the_file_is_refused(self):
        code, _, err = self.run_cli('lease', 'take', 'build-box', '--holder', 'a')
        self.assertEqual(code, 1)
        self.assertIn('build-box cannot be leased', err)
        code, _, err = self.run_cli('lease', 'take', 'nope', '--holder', 'a')
        self.assertEqual(code, 1)
        self.assertIn("no machine 'nope' in the machine file (known: test-box, build-box)", err)

    def test_a_holder_name_that_is_not_one_word_is_refused(self):
        for holder in ('', 'a b', '-a', 'a\nb', 'x' * 129):
            with self.subTest(holder=holder):
                code, _, err = self.run_cli('lease', 'take', 'test-box', f'--holder={holder}')
                self.assertEqual(code, 1)
                self.assertIn('holder', err)

    def test_a_missing_lease_folder_says_how_to_make_it(self):
        os.rmdir(self.leases)
        code, _, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'a')
        self.assertEqual(code, 1)
        self.assertIn(f'lease folder {self.leases} is not there; the owner makes it, as root: install -d -m 2775', err)
        self.assertFalse(os.path.exists(self.leases))

    def test_a_lease_file_this_kit_did_not_write_is_refused_not_overwritten(self):
        path = os.path.join(self.leases, 'test-box.json')
        for text in ('{not json', '[]', json.dumps({'version': 1, 'machine': 'other', 'holder': 'a', 'since': 1,
                                                    'until': 2})):
            with self.subTest(text=text):
                with open(path, 'w') as f:
                    f.write(text)
                code, _, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'a')
                self.assertEqual(code, 1)
                self.assertIn(f'{path}: not a lease this kit wrote', err)
                with open(path) as f:
                    self.assertEqual(f.read(), text)

    def test_a_lease_file_that_is_a_link_is_refused(self):
        target = os.path.join(self.tmp, 'elsewhere.json')
        with open(target, 'w') as f:
            f.write('{}')
        os.symlink(target, os.path.join(self.leases, 'test-box.json'))
        code, _, err = self.run_cli('lease', 'take', 'test-box', '--holder', 'a')
        self.assertEqual(code, 1)
        self.assertIn('is a symbolic link', err)
        with open(target) as f:
            self.assertEqual(f.read(), '{}')


class Release(hf.Temp):

    def setUp(self):
        super().setUp()
        self.write_machine()
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')[0], 0)

    def test_the_holder_releases_and_the_machine_is_free(self):
        code, out, _ = self.run_cli('lease', 'release', 'test-box', '--holder', 'proj-a/agent-1', now=NOW + 60)
        self.assertEqual((code, out), (0, 'test-box: released\n'))
        self.assertFalse(os.path.exists(os.path.join(self.leases, 'test-box.json')))
        self.assertEqual(self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-b/x', now=NOW + 61)[0], 0)

    def test_another_holder_cannot_release_a_live_lease(self):
        code, _, err = self.run_cli('lease', 'release', 'test-box', '--holder', 'proj-b/agent-7', now=NOW + 60)
        self.assertEqual(code, 3)
        self.assertIn('leased by proj-a/agent-1', err)
        self.assertEqual(self.lease_file()['holder'], 'proj-a/agent-1')

    def test_release_twice_or_after_expiry_is_fine(self):
        self.assertEqual(self.run_cli('lease', 'release', 'test-box', '--holder', 'proj-a/agent-1')[0], 0)
        code, out, _ = self.run_cli('lease', 'release', 'test-box', '--holder', 'proj-a/agent-1')
        self.assertEqual((code, out), (0, 'test-box: was not leased\n'))
        self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1')
        code, out, _ = self.run_cli('lease', 'release', 'test-box', '--holder', 'proj-b/x', now=NOW + 2 * HOUR)
        self.assertEqual((code, out), (0, 'test-box: was not leased\n'))       # expired: anyone clears it
        self.assertFalse(os.path.exists(os.path.join(self.leases, 'test-box.json')))


class List(hf.Temp):

    def test_list_shows_holders_notes_and_free_machines(self):
        other = dict(hf.TEST_BOX, name='test-box-2')
        self.write_machine(hf.TEST_BOX, other, hf.BUILD_BOX)
        self.run_cli('lease', 'take', 'test-box', '--holder', 'proj-a/agent-1', '--note', 'device tests')
        code, out, _ = self.run_cli('lease', 'list', now=NOW + 15 * 60)
        self.assertEqual(code, 0)
        self.assertEqual(out, 'test-box: leased by proj-a/agent-1 until 2026-11-27T09:00:00Z (45 min left) - '
                              'device tests\ntest-box-2: free\n')       # build-box cannot be leased: not listed
        code, out, _ = self.run_cli('lease', 'list', '--json')
        rows = json.loads(out)
        self.assertEqual([(r['machine'], r['lease'] and r['lease']['holder']) for r in rows],
                         [('test-box', 'proj-a/agent-1'), ('test-box-2', None)])


class MissingMachineFile(hf.Temp):

    def test_every_lease_command_names_the_missing_file_and_where_to_read_how(self):
        for argv in (('lease', 'take', 'test-box', '--holder', 'a'), ('lease', 'release', 'test-box', '--holder', 'a'),
                     ('lease', 'list')):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv)
                self.assertEqual((code, out), (1, ''))
                self.assertIn(f'{self.machine}: not there. The owner writes it as root', err)
                self.assertIn('fork-kit/homelab/README.md', err)
        self.assertEqual(os.listdir(self.leases), [])

    def test_a_machine_file_without_a_homelab_section_is_refused(self):
        with open(self.machine, 'w') as f:
            json.dump({'version': 1, 'heavy': {}}, f)
        code, _, err = self.run_cli('lease', 'list')
        self.assertEqual(code, 1)
        self.assertIn('no "homelab" section', err)


MUTATIONS = {
    'another live holder is refused': (
        'lease', "        if live and cur['holder'] != holder:\n            raise Held(cur)\n", '',
        'Take.test_a_lease_taken_twice_by_another_holder_is_refused_and_kept'),
    'a lease ends at its time': (
        'lease', "        live = cur is not None and cur['until'] > now\n", "        live = cur is not None\n",
        'Take.test_a_lease_that_expires_is_free_for_another_holder'),
    'a renewal keeps since': (
        'lease', "'since': cur['since'] if live else now", "'since': now",
        'Take.test_a_lease_taken_twice_by_its_holder_is_renewed'),
    'the lock is taken': (
        'lease', '        fcntl.flock(fd, fcntl.LOCK_EX)\n', '',
        'Take.test_two_takers_at_once_never_both_win'),
    'the most minutes is kept': (
        'lease', "not 1 <= minutes <= rules['maxMinutes']", 'not 1 <= minutes',
        'Take.test_more_than_the_machines_most_minutes_is_refused'),
    'a foreign lease file is refused': (
        'lease', "lease.get('machine') == name", 'True',
        'Take.test_a_lease_file_this_kit_did_not_write_is_refused_not_overwritten'),
    'release checks the holder': (
        'lease', "        if cur['until'] > now and cur['holder'] != holder:\n            raise Held(cur)\n", '',
        'Release.test_another_holder_cannot_release_a_live_lease'),
    'a missing file says where to read how': (
        'config', "        if 'not there' in str(e):", '        if False:',
        'MissingMachineFile.test_every_lease_command_names_the_missing_file_and_where_to_read_how'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        hf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
