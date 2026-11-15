"""t3-golive copy (the practice stop), its guard and the restore primitive, on a fake machine
(tests/golive_fixture.py): the service manager is faked (stop kills a fake server, start spawns a new one), the T3
home is a temp folder, hooks and the quiet command are stub scripts. Nothing here stops, starts or reads a real T3.
python3 -I -B fork-kit/golive/tests/copy.test.py"""
import errno, hashlib, io, json, os, signal, subprocess, sys, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import check, coldcopy, config, journal, live, osadapter  # noqa: E402

# A stub command: appends ["<name>", <env facts>] to the shared call log, then exits with the next code from
# <name>.codes (one per line; the last one repeats; none: 0).
STUB = '''#!{python} -I
import json, os, sys
root, name = {root!r}, {name!r}
with open(os.path.join(root, 'fake-adapter-calls.jsonl'), 'a') as f:
    f.write(json.dumps([name, os.environ.get('T3CODE_AUTO_UPDATE_CURRENT_VERSION'),
                        os.environ.get('T3CODE_AUTO_UPDATE_TARGET_VERSION'), os.getcwd()]) + '\\n')
codes = os.path.join(root, name + '.codes')
try:
    with open(codes) as f:
        lines = f.read().split()
except FileNotFoundError:
    lines = []
code = int(lines[0]) if lines else 0
if len(lines) > 1:
    with open(codes, 'w') as f:
        f.write('\\n'.join(lines[1:]))
print(name + ' says ' + str(code))
sys.exit(code)
'''

HOLD_OWNER = '''import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('BEGIN EXCLUSIVE')
print('ready', flush=True)
time.sleep(60)
'''

KILLED = '''import os, sys
sys.path[:0] = {paths!r}
import golive_fixture as fx
from golive import coldcopy, config, osadapter
config.MACHINE_FILE, config.MACHINE_OWNER = {machine!r}, os.getuid()
osadapter.SYSTEMCTL, osadapter.CGROUP_FILE = {systemctl!r}, {cgroup!r}
coldcopy.QUIET_GAP, coldcopy.POLL = 0, 0.01
adapter = fx.FakeAdapter({root!r}, None, on={{'stop': lambda: os.kill(os.getpid(), 9)}})
coldcopy.run_copy(config.load(), adapter, say=lambda line: None)
'''


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class CopyBase(unittest.TestCase):
    def setUp(self):
        self.m = fx.Machine()
        self.addCleanup(self.m.close)
        self.m.standard()
        for name in ('quiet', 'pre', 'post'):
            self.m.write(f'bin/{name}', STUB.format(python=sys.executable, root=self.m.root, name=name), mode=0o755)
        self.configure()
        # More of a real home: logs, the ownership lock, a stale -shm, folders, a link.
        self.m.write('home/userdata/logs/server.log', 'log line\n')
        self.m.write('home/userdata/statev2.sqlite-shm', b's' * 64)
        self.m.write('home/userdata/attachments/a1/photo.png', b'\x89PNG' + b'p' * 300, mode=0o600)
        os.symlink('a1/photo.png', self.m.path('home/userdata/attachments/latest'))
        self.adapter = fx.FakeAdapter(self.m.root, self.m)
        patcher = mock.patch.multiple(coldcopy, QUIET_GAP=0, POLL=0.01, STOP_WAIT=3, START_WAIT=5)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []

    def configure(self, **over):
        values = {'targetVersion': fx.TARGET, 'quietCommand': self.m.path('bin/quiet'),
                  'preHooks': [self.m.path('bin/pre')], 'postHooks': [self.m.path('bin/post')], **over}
        self.m.config(**values)

    def codes(self, name, *codes):
        self.m.write(f'{name}.codes', '\n'.join(str(c) for c in codes))

    def copy(self, owner_says_go=False):
        return coldcopy.run_copy(config.load(), self.adapter, owner_says_go, say=self.said.append)

    def calls(self):
        return [c[0] for c in self.m.calls('fake-adapter-calls.jsonl')]

    def last_run(self):
        return journal.read(self.m.state)[0][-1]

    def steps(self, run=None):
        return [(s['step'], s['phase']) for s in (run or self.last_run())['steps']]

    def copies(self):
        return sorted(os.listdir(self.m.backups))

    def assert_t3_runs_again(self, old_pid):
        svc = self.adapter.service()
        self.assertTrue(svc['active'])
        server = live.live_server(self.m.home, svc['mainPid'])
        self.assertNotEqual(server['pid'], old_pid)
        self.assertEqual(server['version'], fx.LIVE)


class Copy(CopyBase):
    def test_the_practice_stop_copies_reads_back_writes_the_manifest_last_and_starts_t3_again(self):
        userdata = self.m.path('home/userdata')
        before = {n: sha(os.path.join(userdata, n)) for n in ('statev2.sqlite', 'statev2.sqlite-wal',
                                                               'settings.json')}
        old_pid = self.m.server.pid
        result = self.copy()

        [name] = self.copies()
        dest = os.path.join(self.m.backups, name)
        self.assertEqual(result['copy'], dest)
        self.assertTrue(name.startswith('t3-copy-') and name.endswith(f'-{fx.LIVE}'), name)
        self.assertEqual(os.stat(dest).st_mode & 0o777, 0o700)
        m = coldcopy.read_manifest(dest)
        self.assertEqual((m['liveVersion'], m['home'], m['complete']), (fx.LIVE, self.m.home, True))
        files = {e['path']: e for e in m['files']}
        for n, digest in before.items():
            self.assertEqual(files[n]['sha256'], digest, n)
            self.assertEqual(sha(os.path.join(dest, 'userdata', n)), digest, n)
            self.assertEqual(sha(os.path.join(userdata, n)), digest, f'the copy changed the home: {n}')
        # Left out: logs, the server's runtime record and ownership lock, SQLite's -shm.
        for n in ('logs', 'logs/server.log', 'server-runtime.json', 'server-owner.sqlite', 'statev2.sqlite-shm'):
            self.assertNotIn(n, files)
            self.assertFalse(os.path.lexists(os.path.join(dest, 'userdata', n)), n)
        self.assertEqual(files['attachments/a1/photo.png']['mode'], 0o600)
        self.assertEqual(os.stat(os.path.join(dest, 'userdata/attachments/a1/photo.png')).st_mode & 0o777, 0o600)
        self.assertEqual(files['attachments/latest'], {'path': 'attachments/latest', 'kind': 'link',
                                                       'target': 'a1/photo.png'})
        self.assertEqual(os.readlink(os.path.join(dest, 'userdata/attachments/latest')), 'a1/photo.png')
        self.assertEqual(coldcopy.verify_copy(dest)['liveVersion'], fx.LIVE)
        self.assertFalse(os.path.exists(os.path.join(dest, '.manifest.json.tmp')))

        # Order: quiet twice, guard armed, pre hooks, quiet again, stop, start, post hooks.
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'stop', 'start', 'post'])
        [guard] = [c for c in self.m.calls('fake-adapter-calls.jsonl') if c[0] == 'run_detached']
        self.assertEqual(guard[2], [coldcopy.KIT_BIN, 'guard', '--run', self.last_run()['id']])
        self.assertTrue(os.access(coldcopy.KIT_BIN, os.X_OK))
        # The quiet command got the fork setting's contract: both versions, the home as its folder.
        quiet = [c for c in self.m.calls('fake-adapter-calls.jsonl') if c[0] == 'quiet']
        self.assertEqual(quiet[0][1:], [fx.LIVE, fx.TARGET, self.m.home])
        # The journal: every step done, the manifest after the read-back, T3 verified, the run ended ok.
        run = self.last_run()
        self.assertEqual([s for s, p in self.steps(run) if p == 'done'],
                         ['guard', 'pre-hooks', 'stop-service', 'cold-copy', 'read-back', 'manifest',
                          'start-service', 'verify', 'post-hooks'])
        self.assertEqual(run['end']['result'], 'ok')
        self.assert_t3_runs_again(old_pid)
        self.assertEqual(check.run_check(config.load(), self.adapter)[0], 0)

    def test_a_copy_never_goes_over_another(self):
        with mock.patch.object(coldcopy, '_stamp', return_value='20261115T120000Z'):
            self.copy()
            before = self.calls()
            with self.assertRaises(journal.Refused) as e:
                self.copy()
        self.assertIn('already exists', str(e.exception))
        self.assertEqual(self.calls()[len(before):], ['quiet', 'quiet'], 'T3 was stopped for a copy that cannot be')
        self.assertEqual(self.last_run()['end']['result'], 'refused')

    def test_no_quiet_point_is_refused_and_nothing_is_changed(self):
        self.configure(quietWaitSeconds=0)
        self.codes('quiet', 1)
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('no quiet point within 0 s', str(e.exception))
        self.assertIn('quiet says 1', str(e.exception))
        self.assertEqual(self.calls(), ['quiet'])
        self.assertEqual(self.copies(), [])
        run = self.last_run()
        self.assertEqual((run['end']['result'], run['steps']), ('refused', []))
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_quiet_must_hold_twice_a_gap_apart(self):
        coldcopy.QUIET_GAP = 0.3
        self.codes('quiet', 0, 1, 0, 0)
        started = time.monotonic()
        self.copy()
        self.assertGreaterEqual(time.monotonic() - started, 0.3)
        self.assertEqual([c for c in self.calls() if c == 'quiet'], ['quiet'] * 5)
        self.assertTrue(any('waiting for a quiet point' in line for line in self.said))

    def test_no_quiet_command_needs_the_owner_to_say_go(self):
        self.configure(quietCommand=None)
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('--owner-says-go', str(e.exception))
        self.assertEqual(self.calls(), [])
        self.copy(owner_says_go=True)
        self.assertEqual(self.calls(), ['run_detached', 'pre', 'stop', 'start', 'post'])
        [stop] = [s for s in self.last_run()['steps'] if s['step'] == 'stop-service' and s['phase'] == 'started']
        self.assertTrue(stop['data']['ownerSaysGo'])

    def test_a_failing_pre_hook_runs_the_post_hooks_and_never_stops_t3(self):
        self.codes('pre', 3)
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('pre exited 3', str(e.exception))
        self.assertIn('T3 was not stopped', str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'post'])
        self.assertEqual(self.last_run()['end']['result'], 'refused')
        self.assertIsNone(journal.unfinished(self.m.state), 'a refusal with every step undone needs no settling')

    def test_busy_again_after_the_pre_hooks_is_refused_before_the_stop(self):
        self.codes('quiet', 0, 0, 1)
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('not quiet any more', str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'post'])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_a_copy_that_fails_half_way_has_no_manifest_and_t3_runs_again(self):
        real = coldcopy._copy_file

        def disk_full(src, dst, deadline, mode):
            if src.endswith('settings.json'):
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(src, dst, deadline, mode)
        old_pid = self.m.server.pid
        with mock.patch.object(coldcopy, '_copy_file', disk_full):
            with self.assertRaises(OSError):
                self.copy()
        [name] = self.copies()
        dest = os.path.join(self.m.backups, name)
        self.assertFalse(os.path.exists(os.path.join(dest, 'manifest.json')))
        with self.assertRaises(coldcopy.Incomplete) as e:
            coldcopy.read_manifest(dest)
        self.assertIn('incomplete and is never used', str(e.exception))
        self.assertEqual(self.calls()[-3:], ['stop', 'start', 'post'])
        self.assert_t3_runs_again(old_pid)
        run = self.last_run()
        self.assertEqual(run['end']['result'], 'failed')
        self.assertIn(('verify', 'done'), self.steps(run))
        # A failure after T3 stopped needs the owner to look: the next run is refused until it is settled.
        with self.assertRaises(journal.Unfinished):
            self.copy()

    def test_a_read_back_that_differs_writes_no_manifest(self):
        real, seen = coldcopy._hash, []

        def flipped(path, deadline=None, drop_cache=False):
            seen.append(path)
            return '0' * 64 if path.endswith('statev2.sqlite-wal') else real(path, deadline, drop_cache)
        with mock.patch.object(coldcopy, '_hash', flipped):
            with self.assertRaises(RuntimeError) as e:
                self.copy()
        self.assertIn('read-back', str(e.exception))
        dest = os.path.join(self.m.backups, self.copies()[0])
        self.assertFalse(os.path.exists(os.path.join(dest, 'manifest.json')))
        self.assertTrue(any(p.startswith(dest) for p in seen) and any(p.startswith(self.m.home) for p in seen),
                        'both sides are read back')
        self.assertTrue(self.adapter.service()['active'])

    def test_too_long_a_stop_gives_up_and_starts_t3_again(self):
        self.configure(stopLimitSeconds=1)
        clock = iter(range(0, 10**6, 100))
        with mock.patch.object(coldcopy, '_clock', lambda: next(clock)):
            with self.assertRaises(TimeoutError) as e:
                self.copy(owner_says_go=True)
        self.assertIn('stopLimitSeconds', str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.m.backups, self.copies()[0], 'manifest.json')))

    def test_another_server_owning_the_home_stops_the_copy_before_anything_is_copied(self):
        p = subprocess.Popen([sys.executable, '-I', '-c', HOLD_OWNER, self.m.path('home/userdata/server-owner.sqlite')],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        self.addCleanup(fx.kill, p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        coldcopy.STOP_WAIT = 0.3
        with self.assertRaises(TimeoutError) as e:
            self.copy()
        self.assertIn('still holds', str(e.exception))
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.calls()[-3:], ['stop', 'start', 'post'])
        self.assertEqual(self.last_run()['end']['result'], 'failed')

    def test_the_ownership_lock_keeps_a_server_out_while_the_copy_runs(self):
        seen = []

        def probe(*args):
            c = __import__('sqlite3').connect(self.m.path('home/userdata/server-owner.sqlite'), timeout=0,
                                              isolation_level=None)
            try:
                c.execute('BEGIN EXCLUSIVE')
                seen.append('free')
            except Exception:
                seen.append('held')
            finally:
                c.close()
            return real(*args)
        real = coldcopy.read_back
        with mock.patch.object(coldcopy, 'read_back', probe):
            self.copy()
        self.assertEqual(seen, ['held'])

    def test_a_second_run_while_one_runs_is_refused(self):
        with journal.run(self.m.state, 'switch'):
            with self.assertRaises(journal.Busy):
                self.copy()
        self.assertEqual(self.calls(), [])

    def test_the_check_refusing_stops_the_copy_before_any_step(self):
        self.m.write('home/runtime/.restart-pending', '')
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('no markers', str(e.exception))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.last_run()['end']['result'], 'refused')


class Guard(CopyBase):
    def test_a_copy_killed_while_t3_is_stopped_is_put_right_by_the_guard_and_reported(self):
        code = KILLED.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON, os.path.dirname(os.path.abspath(__file__))],
                             machine=config.MACHINE_FILE, systemctl=osadapter.SYSTEMCTL,
                             cgroup=osadapter.CGROUP_FILE, root=self.m.root)
        p = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL, p.stderr)
        run = self.last_run()
        self.assertIsNone(run['end'])
        self.assertEqual(self.steps(run)[-1], ('stop-service', 'started'))
        self.assertFalse(self.adapter.service()['active'], 'the fake stop ran')

        did = coldcopy.guard(config.load(), self.adapter, run['id'])
        self.assertEqual(did[0], 'started t3code.service')
        self.assertIn('post says 0', did[1])
        self.assertTrue(self.adapter.service()['active'])
        with open(os.path.join(self.m.state, 'guard.log')) as f:
            self.assertEqual(json.loads(f.readline())['run'], run['id'])
        # The killed run stays for the owner: check names it, and the next copy is refused until it is settled.
        code, result = check.run_check(config.load(), self.adapter)
        self.assertEqual(code, 1)
        self.assertIn('stopped half-way', json.dumps(result))
        with self.assertRaises(journal.Unfinished):
            self.copy()

    def test_the_guard_waits_for_the_run_and_leaves_a_finished_one_alone(self):
        done = []
        with journal.run(self.m.state, 'copy') as r:
            with r.step('stop-service'):
                pass
            t = threading.Thread(target=lambda: done.append(coldcopy.guard(config.load(), self.adapter, r.id)))
            t.start()
            time.sleep(0.3)
            self.assertTrue(t.is_alive(), 'the guard did not wait for the run to end')
        t.join(10)
        self.assertEqual(done, [[]])
        self.assertEqual(self.calls(), [])

    def test_the_guard_starts_t3_left_stopped_by_a_failed_run(self):
        with self.assertRaises(OSError):
            with journal.run(self.m.state, 'copy') as r:
                with r.step('stop-service'):
                    self.adapter.stop()
                    raise OSError('the start failed too')
        self.assertEqual(coldcopy.guard(config.load(), self.adapter, r.id), ['started t3code.service'])

    def test_an_unknown_run_or_one_that_never_stopped_t3_is_left_alone(self):
        self.adapter.stop()
        self.assertEqual(coldcopy.guard(config.load(), self.adapter, 'no-such-run'), [])
        self.assertEqual(self.calls(), ['stop'])


class Restore(CopyBase):
    def setUp(self):
        super().setUp()
        self.dest = self.copy(owner_says_go=True)['copy']
        self.userdata = self.m.path('home/userdata')
        self.copied = sha(os.path.join(self.dest, 'userdata/statev2.sqlite'))
        # Life goes on after the copy; then the service is stopped (rehearse / back do that).
        self.m.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'newer' * 900)
        self.m.write('home/userdata/statev2.sqlite-wal', b'newer wal')
        self.m.write('home/userdata/settings.json', '{"newer": true}')
        self.adapter.stop()
        self.newer = {n: sha(os.path.join(self.userdata, n)) for n in os.listdir(self.userdata)
                      if os.path.isfile(os.path.join(self.userdata, n)) and not n.startswith('server-owner.sqlite')}

    def restore(self, **kw):
        try:
            with journal.run(self.m.state, 'back') as r:
                with r.step('restore') as facts:
                    facts.update(coldcopy.restore(r, kw.pop('dest', self.dest), self.m.home, **kw))
        except Exception:
            journal.settle(self.m.state, 'test: looked at the home')
            raise
        return facts

    def all_hashes(self):
        """Every file of the home's userdata but the ownership lock (a server holding it may write to it)."""
        return {sha(os.path.join(self.userdata, n)) for n in os.listdir(self.userdata)
                if os.path.isfile(os.path.join(self.userdata, n)) and not n.startswith('server-owner.sqlite')}

    def test_restore_puts_the_copy_back_and_moves_the_old_files_aside(self):
        facts = self.restore()
        self.assertEqual(sha(os.path.join(self.userdata, 'statev2.sqlite')), self.copied)
        self.assertEqual(sha(os.path.join(self.userdata, 'settings.json')),
                         sha(os.path.join(self.dest, 'userdata/settings.json')))
        self.assertEqual(sorted(facts['restored']), ['settings.json', 'statev2.sqlite', 'statev2.sqlite-wal'])
        # Nothing was deleted: every file the home had is still there, under a name with a time in it.
        self.assertLessEqual(set(self.newer.values()), self.all_hashes())
        self.assertEqual(sorted(n.split('.golive-aside-')[0] for n in facts['movedAside']),
                         ['settings.json', 'statev2.sqlite', 'statev2.sqlite-shm', 'statev2.sqlite-wal'])
        self.assertFalse(os.path.exists(os.path.join(self.userdata, 'statev2.sqlite-shm')), 'a stale -shm stayed')
        self.assertFalse([n for n in os.listdir(self.userdata) if '.golive-restore-' in n], 'temps left behind')
        # The copy itself is untouched and can be used again.
        coldcopy.verify_copy(self.dest)
        self.assertEqual(journal.read(self.m.state)[0][-1]['end']['result'], 'ok')

    def test_a_restore_that_stopped_half_way_runs_again_from_the_untouched_copy(self):
        real, renames = os.rename, []

        def fail_on_the_first_put_back(src, dst):
            if '.golive-restore-' in src:
                raise OSError(errno.EIO, 'killed here')
            renames.append(dst)
            return real(src, dst)
        with mock.patch.object(coldcopy.os, 'rename', fail_on_the_first_put_back):
            with self.assertRaises(OSError):
                self.restore()
        self.assertFalse(os.path.exists(os.path.join(self.userdata, 'statev2.sqlite')), 'the half state')
        with mock.patch.object(coldcopy, '_stamp', return_value='20990101T000000Z'):
            self.restore()
        self.assertEqual(sha(os.path.join(self.userdata, 'statev2.sqlite')), self.copied)
        self.assertLessEqual(set(self.newer.values()), self.all_hashes(), 'the first try lost a file')

    def test_a_copy_without_a_finished_manifest_is_never_used(self):
        os.rename(os.path.join(self.dest, 'manifest.json'), os.path.join(self.dest, 'manifest.json.x'))
        with self.assertRaises(coldcopy.Incomplete):
            self.restore()
        self.m.write(os.path.relpath(os.path.join(self.dest, 'manifest.json'), self.m.root),
                     json.dumps({'manifestVersion': 1, 'complete': False, 'files': []}))
        with self.assertRaises(coldcopy.Incomplete):
            self.restore()
        self.assertEqual(self.all_hashes(), set(self.newer.values()), 'the home was changed')

    def test_a_copy_that_does_not_match_its_manifest_is_never_used(self):
        with open(os.path.join(self.dest, 'userdata/statev2.sqlite'), 'r+b') as f:
            f.write(b'X')
        with self.assertRaises(coldcopy.Incomplete) as e:
            self.restore()
        self.assertIn('does not match its manifest', str(e.exception))
        self.assertEqual(self.all_hashes(), set(self.newer.values()))
        with self.assertRaises(coldcopy.Incomplete):
            coldcopy.verify_copy(self.dest)

    def test_restore_is_refused_while_a_server_owns_the_home(self):
        p = subprocess.Popen([sys.executable, '-I', '-c', HOLD_OWNER, os.path.join(self.userdata, 'server-owner.sqlite')],
                             stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True)
        self.addCleanup(fx.kill, p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        with self.assertRaises(TimeoutError):
            self.restore()
        self.assertEqual(self.all_hashes(), set(self.newer.values()))

    def test_restore_is_a_change_and_runs_only_inside_a_step(self):
        with journal.run(self.m.state, 'back') as r:
            with self.assertRaises(RuntimeError) as e:
                coldcopy.restore(r, self.dest, self.m.home)
        self.assertIn('outside a journalled step', str(e.exception))
        self.assertEqual(self.all_hashes(), set(self.newer.values()))

    def test_plan_restore_is_the_read_only_pre_check(self):
        files, want = coldcopy.plan_restore(self.dest, self.m.home)
        self.assertEqual(sorted(want), ['settings.json', 'statev2.sqlite', 'statev2.sqlite-wal'])
        self.assertEqual(self.all_hashes(), set(self.newer.values()))
        with open(os.path.join(self.dest, 'userdata/settings.json'), 'a') as f:
            f.write(' ')
        with self.assertRaises(journal.Refused):
            with journal.run(self.m.state, 'back') as r:
                try:
                    coldcopy.plan_restore(self.dest, self.m.home)
                except coldcopy.Incomplete as e:
                    raise journal.Refused(str(e)) from None
        self.assertIsNone(journal.unfinished(self.m.state), 'a refused pre-check needs no settling')

    def test_restore_takes_top_level_names_only(self):
        for bad in ('../settings.json', 'attachments/a1/photo.png', '..', ''):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    self.restore(names=(bad,))

    def test_a_copy_with_no_database_is_refused(self):
        os.rename(os.path.join(self.dest, 'manifest.json'), os.path.join(self.dest, 'm'))
        with open(os.path.join(self.dest, 'm')) as f:
            m = json.load(f)
        m['files'] = [e for e in m['files'] if not e['path'].startswith('statev2')]
        self.m.write(os.path.relpath(os.path.join(self.dest, 'manifest.json'), self.m.root), json.dumps(m))
        with self.assertRaises(coldcopy.Incomplete):
            self.restore()


class CommandLine(CopyBase):
    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        return check.main(list(args), out=out, err=err), out.getvalue(), err.getvalue()

    def test_copy_runs_detached_by_default(self):
        code, out, err = self.main('copy', '--owner-says-go')
        self.assertEqual(code, 0, err)
        [call] = self.m.calls('systemd-run-calls.jsonl')
        unit = [a for a in call if a.startswith('--unit=')][0][len('--unit='):]
        self.assertTrue(unit.startswith('t3-golive-copy-'), unit)
        self.assertEqual(call[call.index('--') + 1:], [coldcopy.KIT_BIN, 'copy', '--here', '--owner-says-go'])
        self.assertIn(unit, out)
        self.assertEqual(self.calls(), [], 'nothing ran in this process')
        self.assertFalse(os.path.exists(os.path.join(self.m.state, 'journal.jsonl')))

    def test_copy_here_is_refused_inside_the_boot_service(self):
        self.m.write('cgroup', '0::/user.slice/user-1000.slice/user@1000.service/app.slice/t3code.service\n')
        code, _, err = self.main('copy', '--here')
        self.assertEqual(code, 1)
        self.assertIn('without --here', err)
        self.assertFalse(os.path.exists(os.path.join(self.m.state, 'journal.jsonl')))
        self.assertEqual(self.m.calls('systemctl-calls.jsonl'), [])

    def test_inside_the_service_is_read_from_the_cgroup_and_fails_closed(self):
        a = osadapter.SystemdUser()
        for text, inside in (('0::/user.slice/user@1000.service/app.slice/t3code.service\n', True),
                             ('1:name=systemd:/user.slice/user@1000.service/app.slice/t3code.service/x\n', True),
                             ('0::/user.slice/user@1000.service/app.slice/t3code.service.d\n', False),
                             ('0::/user.slice/user@1000.service/app.slice/other.service\n', False)):
            with self.subTest(text=text):
                self.m.write('cgroup', text)
                self.assertIs(a.inside_service(), inside)
        os.remove(self.m.path('cgroup'))
        self.assertTrue(a.inside_service())

    def test_a_refused_copy_says_so_and_exits_1(self):
        self.configure(quietCommand=None)
        out, err = io.StringIO(), io.StringIO()
        code = coldcopy.main_copy(config.load(), self.adapter, True, False, out, err)
        self.assertEqual(code, 1)
        self.assertIn('Refused: ', err.getvalue())
        code = coldcopy.main_copy(config.load(), self.adapter, True, True, out, err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn('copy done', out.getvalue())


class Manifest(unittest.TestCase):
    def test_a_manifest_this_kit_cannot_read_is_incomplete(self):
        m = fx.Machine()
        self.addCleanup(m.close)
        d = m.path('c')
        os.makedirs(d)
        for text in (None, '{', '[]', json.dumps({'manifestVersion': 2, 'complete': True, 'files': []}),
                     json.dumps({'manifestVersion': 1, 'complete': 'yes', 'files': []})):
            with self.subTest(text=text):
                if text is not None:
                    m.write('c/manifest.json', text)
                with self.assertRaises(coldcopy.Incomplete):
                    coldcopy.read_manifest(d)


if __name__ == '__main__':
    unittest.main()
