"""homelab's test-machine leases (`homelab lease ...`, lib/homelabkit/leases.py): a machine file and a lease folder in a
temporary folder, a fixed clock. No root, no network.

  python3 -I -B fork-kit/homelab/tests/leases.test.py

The state question, answered here: a lease taken twice (by another holder, by the same one), a lease that expires
(refused one second before, free after; an expired lease cannot be renewed), run again (release twice), stopped
half-way (a temporary file left behind is ignored), broken state (a damaged lease file fails closed, a lease folder
that is missing, a link or writable by everyone is refused), and two takers at once (exactly one wins).
RedCheck breaks each rule in a copy of lib/homelabkit/ and shows its test then fails.
"""
import json, os, sys, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from homelab_fixture import Fixture, leases, red_check  # noqa: E402

THIS = os.path.abspath(__file__)


class Leases(Fixture):
    def setUp(self):
        super().setUp()
        self.write_machine_file()

    def take(self, machine='test-a', holder='project-a', *more):
        return self.run_cli('lease', 'take', machine, '--holder', holder, *more)

    def test_take_writes_a_lease_and_list_shows_it(self):
        code, out, err = self.take('test-a', 'project-a', '--minutes', '30', '--purpose', 'release test')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('test-a leased to project-a until', out)
        lease = self.lease_file('test-a')
        self.assertEqual((lease['machine'], lease['holder'], lease['purpose']), ('test-a', 'project-a', 'release test'))
        self.assertEqual(lease['until'] - lease['taken'], 30 * 60)
        code, out, _ = self.run_cli('lease', 'list', '--json')
        self.assertEqual(code, 0)
        rows = {r['machine']: r['lease'] for r in json.loads(out)}
        self.assertEqual(rows['test-a']['holder'], 'project-a')
        self.assertIsNone(rows['test-b'])

    def test_default_minutes_come_from_the_machine_file(self):
        self.assertEqual(self.take()[0], 0)
        lease = self.lease_file('test-a')
        self.assertEqual(lease['until'] - lease['taken'], 60 * 60)

    def test_taken_twice_by_another_holder_is_refused(self):
        self.assertEqual(self.take('test-a', 'project-a')[0], 0)
        before = self.lease_file('test-a')
        code, out, err = self.take('test-a', 'project-b')
        self.assertEqual(code, 1)
        self.assertIn('test-a is leased to project-a', err)
        self.assertEqual(self.lease_file('test-a'), before)            # the first lease is untouched
        self.assertEqual(self.take('test-b', 'project-b')[0], 0)       # another machine is still free

    def test_taken_twice_by_the_same_holder_is_refused(self):
        self.assertEqual(self.take('test-a', 'project-a')[0], 0)
        before = self.lease_file('test-a')
        code, _, err = self.take('test-a', 'project-a')
        self.assertEqual(code, 1)
        self.assertIn('already leased to project-a', err)
        self.assertIn('renew', err)
        self.assertEqual(self.lease_file('test-a'), before)

    def test_a_lease_expires(self):
        self.assertEqual(self.take('test-a', 'project-a', '--minutes', '1')[0], 0)
        self.clock[0] += 59
        self.assertEqual(self.take('test-a', 'project-b')[0], 1)         # still held one second before the end
        self.clock[0] += 1
        code, out, _ = self.run_cli('lease', 'list', '--json')
        self.assertIsNone({r['machine']: r['lease'] for r in json.loads(out)}['test-a'])
        code, out, err = self.take('test-a', 'project-b')
        self.assertEqual((code, err), (0, ''))
        self.assertIn('replaces the expired lease of project-a', out)
        self.assertEqual(self.lease_file('test-a')['holder'], 'project-b')

    def test_renew_extends_only_the_holders_live_lease(self):
        self.assertEqual(self.take('test-a', 'project-a', '--minutes', '10')[0], 0)
        taken = self.lease_file('test-a')['taken']
        self.clock[0] += 300
        code, _, err = self.run_cli('lease', 'renew', 'test-a', '--holder', 'project-b')
        self.assertEqual(code, 1)
        self.assertIn('leased to project-a', err)
        code, out, _ = self.run_cli('lease', 'renew', 'test-a', '--holder', 'project-a', '--minutes', '20')
        self.assertEqual(code, 0)
        lease = self.lease_file('test-a')
        self.assertEqual((lease['taken'], lease['until']), (taken, self.clock[0] + 20 * 60))

    def test_an_expired_lease_cannot_be_renewed(self):
        self.assertEqual(self.take('test-a', 'project-a', '--minutes', '1')[0], 0)
        self.clock[0] += 61
        code, _, err = self.run_cli('lease', 'renew', 'test-a', '--holder', 'project-a')
        self.assertEqual(code, 1)
        self.assertIn('not leased', err)

    def test_release_by_the_holder_only_and_again_is_fine(self):
        self.assertEqual(self.take()[0], 0)
        code, _, err = self.run_cli('lease', 'release', 'test-a', '--holder', 'project-b')
        self.assertEqual(code, 1)
        self.assertIn('leased to project-a', err)
        self.assertIsNotNone(self.lease_file('test-a'))
        self.assertEqual(self.run_cli('lease', 'release', 'test-a', '--holder', 'project-a')[0], 0)
        self.assertIsNone(self.lease_file('test-a'))
        code, out, _ = self.run_cli('lease', 'release', 'test-a', '--holder', 'project-a')   # run again
        self.assertEqual(code, 0)
        self.assertIn('not leased', out)
        self.assertEqual(self.take('test-a', 'project-b')[0], 0)

    def test_bad_requests_are_refused_before_any_write(self):
        for argv, words in [(('lease', 'take', 'nope', '--holder', 'p'), 'unknown machine'),
                            (('lease', 'take', 'test-a', '--holder', 'p', '--minutes', '241'), 'at most 240'),
                            (('lease', 'take', 'test-a', '--holder', 'p', '--minutes', '0'), 'at least 1'),
                            (('lease', 'take', 'test-a', '--holder', 'two words'), 'holder'),
                            (('lease', 'take', 'test-a', '--holder', 'p', '--purpose', 'x\ny'), 'purpose')]:
            with self.subTest(argv=argv):
                code, _, err = self.run_cli(*argv)
                self.assertEqual(code, 2)
                self.assertIn(words, err)
        self.assertEqual(sorted(os.listdir(self.lease_dir)), [])

    def test_a_damaged_lease_file_fails_closed(self):
        with open(os.path.join(self.lease_dir, 'test-a.json'), 'w') as f:
            f.write('{"holder": ')
        code, _, err = self.take('test-a', 'project-b')
        self.assertEqual(code, 2)
        self.assertIn(os.path.join(self.lease_dir, 'test-a.json'), err)
        self.assertEqual(self.run_cli('lease', 'list')[0], 2)

    def test_a_lease_file_that_is_a_link_is_refused(self):
        target = os.path.join(self.dir, 'elsewhere.json')
        with open(target, 'w') as f:
            json.dump({'version': 1, 'machine': 'test-a', 'holder': 'x', 'user': 'x', 'purpose': '',
                       'taken': 0, 'until': 4_000_000_000}, f)
        os.symlink(target, os.path.join(self.lease_dir, 'test-a.json'))
        code, _, err = self.take('test-a', 'project-b')
        self.assertEqual(code, 2)
        self.assertIn('link', err)

    def test_a_temporary_file_left_by_a_stopped_take_is_ignored(self):
        with open(os.path.join(self.lease_dir, '.test-a.json.123.tmp'), 'w') as f:
            f.write('{"half')
        self.assertEqual(self.take()[0], 0)
        self.assertEqual(self.lease_file('test-a')['holder'], 'project-a')

    def test_the_lease_folder_must_be_there_and_safe(self):
        os.rmdir(self.lease_dir)
        code, _, err = self.take()
        self.assertEqual(code, 2)
        self.assertIn(f'{self.lease_dir}: not there', err)
        os.symlink(self.dir, self.lease_dir)
        code, _, err = self.take()
        self.assertEqual(code, 2)
        self.assertIn('symbolic link', err)
        os.unlink(self.lease_dir)
        os.mkdir(self.lease_dir)
        os.chmod(self.lease_dir, 0o777)
        code, _, err = self.take()
        self.assertEqual(code, 2)
        self.assertIn('writable by everyone', err)
        self.assertEqual(os.listdir(self.lease_dir), [])

    def test_two_takers_at_once_one_wins(self):
        real_read = leases.Store._read

        def slow_read(store, dir_fd, machine):     # widen the gap between reading and writing the lease
            found = real_read(store, dir_fd, machine)
            time.sleep(0.05)
            return found

        store = leases.Store(self.lease_dir, ['test-a'], 60, 240)
        results = []

        def taker(holder):
            try:
                store.take('test-a', holder, None, '')
                results.append(holder)
            except leases.Refused:
                pass

        with mock.patch.object(leases.Store, '_read', slow_read):
            threads = [threading.Thread(target=taker, args=(f'project-{i}',)) for i in range(6)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(results), 1, results)
        self.assertEqual(self.lease_file('test-a')['holder'], results[0])


MUTATIONS = {
    'a lease ends at its until time': ('leases', "lease['until'] > now()", "lease['until'] >= now()",
                                       'Leases.test_a_lease_expires'),
    'a live lease is refused to another holder': ('leases', "            if self.live(old):\n                if old",
                                                  "            if False:\n                if old",
                                                  'Leases.test_taken_twice_by_another_holder_is_refused'),
    'a live lease is refused to its own holder': ('leases', "                if old['holder'] == holder:\n",
                                                  "                if old['holder'] == holder:\n"
                                                  "                    return old, None\n",
                                                  'Leases.test_taken_twice_by_the_same_holder_is_refused'),
    'one change at a time': ('leases', 'fcntl.flock(lock, fcntl.LOCK_EX)', 'pass',
                             'Leases.test_two_takers_at_once_one_wins'),
    'renew: the holder only': ('leases', "            if old['holder'] != holder:", '            if False:',
                               'Leases.test_renew_extends_only_the_holders_live_lease'),
    'renew: a live lease only': ('leases', 'if not self.live(old):', 'if old is None:',
                                 'Leases.test_an_expired_lease_cannot_be_renewed'),
    'release: the holder only': ('leases', "if self.live(old) and old['holder'] != holder:", 'if False:',
                                 'Leases.test_release_by_the_holder_only_and_again_is_fine'),
    'a damaged lease fails closed': ('leases', 'except ValueError as e:\n', 'except ValueError as e:\n'
                                     '            return None\n',
                                     'Leases.test_a_damaged_lease_file_fails_closed'),
    'no link is followed': ('leases', " | os.O_NOFOLLOW\n", "\n", 'Leases.test_a_lease_file_that_is_a_link_is_refused'),
    'no folder everyone can write': ('leases', 'if os.fstat(fd).st_mode & stat.S_IWOTH:', 'if False:',
                                     'Leases.test_the_lease_folder_must_be_there_and_safe'),
    'at most maxMinutes': ('leases', 'if minutes > self.max_minutes:', 'if False:',
                           'Leases.test_bad_requests_are_refused_before_any_write'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_failing_test(self):
        red_check(self, THIS, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
