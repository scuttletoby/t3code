"""t3-golive copy (the practice stop), its guard and the restore primitive, on a fake machine
(tests/golive_fixture.py): the service manager is faked (stop kills a fake server, start spawns a new one), the T3
home is a temp folder, hooks and the quiet command are stub scripts. Nothing here stops, starts or reads a real T3.
python3 -I -B fork-kit/golive/tests/copy.test.py"""
import errno, hashlib, io, itertools, json, os, shutil, signal, sqlite3, subprocess, sys, tempfile, threading, time
import unittest
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

# A copy run in its own process, SIGKILLed where `setup` says (it sets on[<adapter call>] = kill, or wraps a function).
KILLED = '''import os, sys
sys.path[:0] = {paths!r}
import golive_fixture as fx
from golive import coldcopy, config, osadapter
config.MACHINE_FILE, config.MACHINE_OWNER = {machine!r}, os.getuid()
osadapter.SYSTEMCTL, osadapter.CGROUP_FILE = {systemctl!r}, {cgroup!r}
coldcopy.QUIET_GAP, coldcopy.POLL = 0, 0.01


def kill(*args):
    os.kill(os.getpid(), 9)


on = {{}}
{setup}
adapter = fx.FakeAdapter({root!r}, None, on=on)
coldcopy.run_copy(config.load(), adapter, say=lambda line: None)
'''

# Commits rows into a WAL-mode database and exits without closing it: the commits stay in the -wal, not
# checkpointed into the database file, as a server stopped without a checkpoint leaves them.
WAL_WRITER = '''import os, sqlite3, sys
c = sqlite3.connect(sys.argv[1], isolation_level=None)
c.execute('PRAGMA journal_mode=WAL')
c.execute('PRAGMA wal_autocheckpoint=0')
c.execute('CREATE TABLE IF NOT EXISTS t (id INTEGER PRIMARY KEY, body TEXT)')
for i in range(int(sys.argv[2])):
    c.execute('INSERT INTO t (body) VALUES (?)', ('row %d ' % i * 40,))
if sys.argv[3] == 'checkpoint':
    c.close()
else:
    os._exit(0)
'''
NEWER = '0.1.10'


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
        patcher = mock.patch.multiple(coldcopy, QUIET_GAP=0, POLL=0.01, STOP_WAIT=3, START_WAIT=5, GUARD_WAIT=0.3)
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

    def distinct_stamps(self):
        """For the whole test: each _stamp() is a new second, so copies made in the same real second never share a
        folder name (a copy correctly refuses a name that exists)."""
        stamps = (f'20990101T{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}Z' for i in itertools.count())
        patcher = mock.patch.object(coldcopy, '_stamp', side_effect=lambda: next(stamps))
        patcher.start()
        self.addCleanup(patcher.stop)

    def last_run(self):
        return journal.read(self.m.state)[0][-1]

    def steps(self, run=None):
        return [(s['step'], s['phase']) for s in (run or self.last_run())['steps']]

    def copies(self):
        return sorted(os.listdir(self.m.backups))

    def updater(self, version=NEWER):
        """What the auto-updater does: installs a newer build, names it in the service state, restarts T3 on it."""
        self.install(version)
        self.m.spawn_server(version)

    def install(self, version=NEWER):
        with open(shutil.which('sleep', path='/usr/bin:/bin'), 'rb') as f:
            self.m.write(f'home/runtime/versions/{version}/t3', f.read(), mode=0o755)
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': version}))

    def killed(self, setup):
        """Run a copy in its own process, SIGKILLed where setup says; returns its run from the journal."""
        code = KILLED.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON, os.path.dirname(os.path.abspath(__file__))],
                             machine=config.MACHINE_FILE, systemctl=osadapter.SYSTEMCTL,
                             cgroup=osadapter.CGROUP_FILE, root=self.m.root, setup=setup)
        p = subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL, capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(p.returncode, -signal.SIGKILL, p.stderr)
        run = self.last_run()
        self.assertIsNone(run['end'])
        return run

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

    def test_the_report_runs_only_before_t3_stops_never_during_the_stop(self):
        # Fork #180: the copy's report (inside its check) has ended before the stop step starts, and none runs
        # while T3 is stopped or starting again.
        reports = lambda: len(self.m.calls('stub-t3-calls.jsonl'))
        at = {}
        self.adapter.on = {'stop': lambda: at.setdefault('stop', reports()),
                           'start': lambda: at.setdefault('start', reports())}
        self.copy(owner_says_go=True)
        self.assertEqual((at['stop'], at['start'], reports()), (1, 1, 1))

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
        with mock.patch.object(config, 'MIN_QUIET_WAIT', 0), self.assertRaises(journal.Refused) as e:
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

    def test_quiet_wait_seconds_is_at_least_the_gap_of_two_samples(self):
        # nit: 0 was accepted, yet two quiet samples 60 s apart can never fit in it, so every copy refused.
        self.configure(quietWaitSeconds=59)
        with self.assertRaises(config.machine_file.MachineFileError) as e:
            config.load()
        self.assertIn('quietWaitSeconds', str(e.exception))
        self.configure(quietWaitSeconds=60)
        self.assertEqual(config.load().quiet_wait_seconds, 60)

    def test_an_update_during_the_quiet_wait_is_refused_before_anything_changes(self):
        # Must fix 1: the auto-updater keeps the same quiet contract and may restart T3 on a newer build (and
        # migrate the database) while the copy waits; a copy labelled with the old version must never be made.
        real = coldcopy.wait_quiet

        def wait_then_update(*args):
            real(*args)
            self.updater()
        with mock.patch.object(coldcopy, 'wait_quiet', wait_then_update):
            with self.assertRaises(journal.Refused) as e:
                self.copy()
        self.assertIn(f'the live server changed (t3 {fx.LIVE}', str(e.exception))
        self.assertIn(f'-> t3 {NEWER}', str(e.exception))
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.calls(), ['quiet', 'quiet'], 'T3 was stopped, or the guard armed, for a stale check')
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_an_update_during_the_pre_hooks_is_refused_before_the_stop(self):
        real = coldcopy._hooks

        def hooks(run, name, *args):
            out = real(run, name, *args)
            if name == 'pre-hooks':
                self.updater()
            return out
        with mock.patch.object(coldcopy, '_hooks', hooks):
            with self.assertRaises(journal.Refused) as e:
                self.copy()
        self.assertIn('the live server changed', str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'post'])
        self.assertEqual(self.copies(), [])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_an_update_at_the_stop_takes_no_copy_and_says_which_build_runs_again(self):
        # Between the last look and the stop: the service state names another build once T3 is down.
        # r1 correctness nit 2: the restart then waited START_WAIT for the old build and reported "T3 did not come
        # back" while the new build ran.
        self.install()
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': fx.LIVE}))
        self.adapter.on['stop'] = lambda: self.m.write('home/runtime/service-state.json',
                                                       json.dumps({'protocol': 3, 'activeVersion': NEWER}))
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as e:
            self.copy()
        self.assertLess(time.monotonic() - started, coldcopy.START_WAIT, 'it waited for the old build')
        self.assertIn('service state changed at the stop', str(e.exception))
        notes = ' '.join(getattr(e.exception, '__notes__', ()))
        self.assertNotIn('did not come back', notes)
        self.assertIn(f'T3 runs again, on t3 {NEWER}', notes)
        self.assertIn(f't3 {NEWER} runs again', ' '.join(self.said))
        [verify] = [s for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertEqual(verify['data']['version'], NEWER)
        self.assertEqual(self.copies(), [])
        self.assertEqual(self.last_run()['end']['result'], 'failed')

    def test_a_runtime_record_of_another_server_of_the_same_build_does_not_stop_the_copy(self):
        # r1 correctness nit 1: a pid check after the stop could only run after a crash (upstream removes the
        # record on a clean stop), and there it refused a copy whose label was right: the state check is the guard.
        self.adapter.on['stop'] = lambda: self.m.runtime_file(pid=os.getpid(), serverVersion=fx.LIVE)
        result = self.copy(owner_says_go=True)
        self.assertEqual(coldcopy.read_manifest(result['copy'])['liveVersion'], fx.LIVE)

    def test_free_space_is_checked_again_after_the_quiet_wait(self):
        with mock.patch.object(coldcopy, '_free_bytes', side_effect=[10 ** 15, 0]):
            with self.assertRaises(journal.Refused) as e:
                self.copy()
        self.assertIn('MB free', str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet'])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_the_state_dir_disk_must_keep_the_floor_too(self):
        # The journal and the guard log live in stateDir; a full disk there would stop a restart (see below).
        with mock.patch.object(coldcopy, '_device', lambda path: path), \
                mock.patch.object(coldcopy, '_free_bytes', lambda path: 0 if path == self.m.state else 10 ** 15):
            with self.assertRaises(journal.Refused) as e:
                self.copy(owner_says_go=True)
        self.assertIn(f'{self.m.state}: 0 MB free', str(e.exception))
        self.assertEqual(self.calls(), [])

    def test_t3_starts_first_and_is_journalled_after(self):
        # A full disk under the journal must not keep T3 stopped: start, then write the line.
        real = journal.Run._append

        def append(run, event, **fields):
            if event == 'step' and fields.get('step') == 'start-service':
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(run, event, **fields)
        old_pid = self.m.server.pid
        with mock.patch.object(journal.Run, '_append', append):
            with self.assertRaises(OSError) as e:
                self.copy(owner_says_go=True)
        self.assertEqual(e.exception.errno, errno.ENOSPC)
        self.assertEqual(self.calls()[-3:], ['stop', 'start', 'post'])
        self.assert_t3_runs_again(old_pid)
        self.assertEqual(self.last_run()['end']['result'], 'failed')

    def test_the_guard_must_say_it_waits_before_t3_stops(self):
        self.adapter.guard_up = False
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('did not come up', str(e.exception))
        self.assertIn('look at: journalctl --user -u t3-golive-guard-', str(e.exception))
        self.assertIn(os.path.join(self.m.state, 'guard.log'), str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached'])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_a_guard_that_died_stops_the_run_before_the_stop(self):
        self.adapter.guard_alive = False
        with self.assertRaises(journal.Refused) as e:
            self.copy()
        self.assertIn('is not running any more', str(e.exception))
        self.assertIn('look at: journalctl --user -u t3-golive-guard-', str(e.exception))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'post'])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_a_failing_post_hook_on_a_refusal_needs_settling(self):
        # Jobs may stay paused: that is no clean refusal.
        self.codes('pre', 3)
        self.codes('post', 4)
        with self.assertRaises(RuntimeError) as e:
            self.copy()
        self.assertIn('post exited 4', str(e.exception))
        self.assertEqual(self.last_run()['end']['result'], 'failed')
        self.assertIsNotNone(journal.unfinished(self.m.state))

    def test_the_first_error_is_kept_when_the_restart_fails_too(self):
        real = coldcopy._copy_file

        def disk_full(src, dst, deadline, mode):
            if src.endswith('settings.json'):
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(src, dst, deadline, mode)

        def start_fails():
            raise osadapter.AdapterError('systemctl --user start exited 1')
        self.adapter.on['start'] = start_fails
        with mock.patch.object(coldcopy, '_copy_file', disk_full):
            with self.assertRaises(OSError) as e:
                self.copy(owner_says_go=True)
        self.assertEqual(e.exception.errno, errno.ENOSPC)
        self.assertIn('then: AdapterError: systemctl --user start exited 1', ' '.join(e.exception.__notes__))
        detail = self.last_run()['end']['detail']
        self.assertIn('No space left', detail)
        self.assertIn('AdapterError', detail)

    def test_a_folder_that_cannot_be_read_is_refused_not_skipped(self):
        if os.geteuid() == 0:
            self.skipTest('root reads every folder')
        folder = self.m.path('home/userdata/attachments/a1')
        os.chmod(folder, 0)
        self.addCleanup(os.chmod, folder, 0o755)
        with self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn('cannot be read in full', str(e.exception))
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.copies(), [])

    def test_a_restore_s_leftover_temp_files_are_never_copied(self):
        self.m.write('home/userdata/.statev2.sqlite.golive-restore-20260101T000000Z', b'half a restore')
        dest = self.copy(owner_says_go=True)['copy']
        self.assertNotIn('.statev2.sqlite.golive-restore-20260101T000000Z',
                         {e['path'] for e in coldcopy.read_manifest(dest)['files']})

    def test_the_files_a_restore_kept_are_never_copied(self):
        # r1 correctness nit 3: each restore's kept old files went into every later copy and its free-space need.
        kept = ('statev2.sqlite.golive-aside-20260101T000000Z', 'statev2.sqlite-wal.golive-aside-20260101T000000Z',
                'statev2.sqlite-shm.golive-aside-20260101T000000Z', 'settings.json.golive-aside-20260101T000000Z-2')
        before = coldcopy.plan(self.m.path('home/userdata'))[1]
        for n in kept:
            self.m.write(f'home/userdata/{n}', b'old data ' * 1000)
        self.assertEqual(coldcopy.plan(self.m.path('home/userdata'))[1], before, 'counted in the free-space need')
        dest = self.copy(owner_says_go=True)['copy']
        paths = {e['path'] for e in coldcopy.read_manifest(dest)['files']}
        self.assertFalse(paths & set(kept), paths & set(kept))

    def test_kept_files_are_named_and_counted(self):
        # r2 nit: kept files (each as big as what it kept) piled up unnamed; a refusal for free disk now names them.
        kept = ('statev2.sqlite.golive-aside-20260101T000000Z', 'settings.json.golive-aside-20260101T000000Z')
        for n in kept:
            self.m.write(f'home/userdata/{n}', b'old data ' * 1000)
        self.m.write('home/userdata/.statev2.sqlite.golive-restore-20260101T000000Z-aside', b'a temp, not kept')
        paths = [self.m.path(f'home/userdata/{n}') for n in sorted(kept)]
        self.assertEqual(coldcopy.kept_files(self.m.home), [(p, 9000) for p in paths])
        with mock.patch.object(coldcopy, '_free_bytes', return_value=0), self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn('2 file(s) earlier restores kept', str(e.exception))
        for path in paths:
            self.assertIn(path, str(e.exception))
        self.assertEqual(self.calls(), [], 'T3 was stopped')
        result = self.copy(owner_says_go=True)
        self.assertEqual(result['keptFiles'], paths)
        self.assertTrue(any(paths[0] in line for line in self.said))

    def test_a_linked_database_is_refused_before_t3_stops(self):
        # r2 correctness nit 2: the copy took the link, stopped T3, then failed with KeyError 'sha256' every time.
        db = self.m.path('home/userdata/statev2.sqlite')
        os.rename(db, self.m.path('home/real.sqlite'))
        os.symlink(self.m.path('home/real.sqlite'), db)
        with self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn('statev2.sqlite in', str(e.exception))
        self.assertIn('is a link', str(e.exception))
        self.assertEqual(self.calls(), [], 'T3 was stopped')
        self.assertEqual(self.copies(), [])
        self.assertIsNone(journal.unfinished(self.m.state))

    def test_a_home_with_a_restore_stopped_half_way_is_never_copied(self):
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': []}))
        with self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn('a restore stopped half-way', str(e.exception))
        # r5 correctness 3: lead with removing the record when the kept files are back (a rerun would restore the
        # copy over a home T3 has served since), and only then running the restore again.
        said = str(e.exception)
        self.assertIn('if the kept files it names are back in the home, remove it', said)
        self.assertLess(said.index('remove it'), said.index('otherwise run the restore again'))
        self.assertEqual((self.calls(), self.copies()), ([], []))

    def test_the_home_is_looked_at_again_just_before_the_stop(self):
        # A restore (or a link) that appears during the wait or the pre hooks: refused, T3 never stopped.
        real = coldcopy._hooks

        def hooks(run, name, paths, home):
            if name == 'pre-hooks':
                self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': []}))
            return real(run, name, paths, home)
        with mock.patch.object(coldcopy, '_hooks', hooks), self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn('a restore stopped half-way', str(e.exception))
        self.assertEqual(self.calls(), ['run_detached', 'pre', 'post'])

    def test_incomplete_copy_folders_are_named_and_counted(self):
        # r1 data-safety C: a copy killed or failed half-way left a folder that was never used, named or counted.
        # r2: three copies, so each needs its own second (CI ran two in one second: "already exists").
        self.distinct_stamps()
        real = coldcopy._copy_file

        def disk_full(src, dst, deadline, mode):
            if src.endswith('settings.json'):
                raise OSError(errno.ENOSPC, 'No space left on device')
            return real(src, dst, deadline, mode)
        with mock.patch.object(coldcopy, '_copy_file', disk_full), self.assertRaises(OSError):
            self.copy(owner_says_go=True)
        journal.settle(self.m.state, 'test: looked at the home')
        [half] = [os.path.join(self.m.backups, n) for n in self.copies()]
        self.assertEqual([f for f, _ in coldcopy.incomplete_copies(self.m.backups)], [half])
        with mock.patch.object(coldcopy, '_free_bytes', return_value=0), self.assertRaises(journal.Refused) as e:
            self.copy(owner_says_go=True)
        self.assertIn(f'1 incomplete copy folder(s), never used', str(e.exception))
        self.assertIn(half, str(e.exception))
        result = self.copy(owner_says_go=True)
        self.assertEqual(result['incompleteCopies'], [half])
        self.assertTrue(any(half in line for line in self.said))
        self.assertEqual(coldcopy.incomplete_copies(self.m.backups)[0][0], half, 'the finished copy is not listed')
        self.assertEqual(len(coldcopy.incomplete_copies(self.m.backups)), 1)

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
        run = self.killed("on['stop'] = kill")
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

    def test_the_guard_says_it_waits_then_waits_for_the_run_and_leaves_a_finished_one_alone(self):
        done = []
        with journal.run(self.m.state, 'copy') as r:
            with r.step('stop-service'):
                pass
            t = threading.Thread(target=lambda: done.append(coldcopy.guard(config.load(), self.adapter, r.id)))
            t.start()
            self.assertTrue(coldcopy._guard_waiting(self.m.state, r.id), 'the run cannot see the guard waiting')
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

    def test_a_journal_the_guard_cannot_read_fails_open(self):
        # Should fix 4: unreadable, the guard cannot tell whether the run stopped T3: it puts T3 back anyway.
        os.makedirs(self.m.state, exist_ok=True)
        with open(os.path.join(self.m.state, 'journal.jsonl'), 'w') as f:
            f.write('not json\nnot json either\n')
        self.adapter.stop()
        did = coldcopy.guard(config.load(), self.adapter, 'some-run')
        self.assertIn('journal unreadable', did[0])
        self.assertEqual(did[1], 'started t3code.service')
        self.assertIn('post says 0', did[2])
        self.assertTrue(self.adapter.service()['active'])

    def test_a_copy_killed_while_waiting_for_a_quiet_point_needs_no_settling(self):
        # Nothing changed before the first step; the next copy runs and check only notes it.
        self.killed('coldcopy.wait_quiet = kill')
        self.assertIsNone(journal.unfinished(self.m.state))
        code, result = check.run_check(config.load(), self.adapter)
        self.assertEqual(code, 0, result)
        self.assertIn('stopped before its first step; it changed nothing', ' '.join(result['info']))
        self.copy(owner_says_go=True)
        self.assertEqual(self.last_run()['end']['result'], 'ok')

    def test_a_copy_killed_mid_copy_has_no_manifest_and_the_guard_starts_t3(self):
        run = self.killed('''
real = coldcopy._copy_file
def copy_file(src, dst, deadline, mode):
    if src.endswith('settings.json'):
        kill()
    return real(src, dst, deadline, mode)
coldcopy._copy_file = copy_file''')
        self.assertEqual(self.steps(run)[-1], ('cold-copy', 'started'))
        [name] = self.copies()
        with self.assertRaises(coldcopy.Incomplete):
            coldcopy.read_manifest(os.path.join(self.m.backups, name))
        did = coldcopy.guard(config.load(), self.adapter, run['id'])
        self.assertEqual(did[0], 'started t3code.service')
        self.assertTrue(self.adapter.service()['active'])
        with self.assertRaises(journal.Unfinished):
            self.copy()

    def test_a_copy_killed_just_before_its_manifest_is_never_used(self):
        run = self.killed('''
real_rename = os.rename
os.rename = lambda src, dst: kill() if dst.endswith('manifest.json') else real_rename(src, dst)''')
        self.assertEqual(self.steps(run)[-1], ('manifest', 'started'))
        dest = os.path.join(self.m.backups, self.copies()[0])
        self.assertTrue(os.path.exists(os.path.join(dest, '.manifest.json.tmp')))
        with self.assertRaises(coldcopy.Incomplete):
            coldcopy.verify_copy(dest)
        self.assertEqual(coldcopy.guard(config.load(), self.adapter, run['id'])[0], 'started t3code.service')

    def test_a_copy_killed_during_the_restart_is_whole_and_the_guard_runs_the_post_hooks(self):
        run = self.killed("on['start'] = kill")
        self.assertEqual(self.steps(run)[-1], ('manifest', 'done'))
        dest = os.path.join(self.m.backups, self.copies()[0])
        self.assertEqual(coldcopy.verify_copy(dest)['liveVersion'], fx.LIVE)
        did = coldcopy.guard(config.load(), self.adapter, run['id'])
        self.assertEqual(len(did), 1)  # the service already runs; only the post hooks
        self.assertIn('post says 0', did[0])
        with self.assertRaises(journal.Unfinished):
            self.copy()

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
                    facts.update(coldcopy.restore(r, kw.pop('dest', self.dest), self.m.home,
                                                  **{'from_home': self.m.home, 'version': fx.LIVE, **kw}))
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

    def test_a_stopped_restore_s_temp_files_are_removed_and_count_as_free(self):
        # r1 correctness nit 5: they stayed forever and were not counted in the free-space check.
        leftover = ('.statev2.sqlite.golive-restore-20260101T000000Z',
                    '.statev2.sqlite-wal.golive-restore-20260101T000000Z-aside')
        for n in leftover:
            self.m.write(f'home/userdata/{n}', b't' * (3 * coldcopy.MB))
        files, want = coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        need = (sum(files[n]['bytes'] for n in want)
                + sum(os.path.getsize(os.path.join(self.userdata, n)) for n in coldcopy._replaced(self.userdata,
                                                                                               coldcopy.DEFAULT_RESTORE)))
        with mock.patch.object(coldcopy, '_free_bytes', return_value=need - 5 * coldcopy.MB):
            facts = self.restore()
        self.assertEqual(facts['removedTemps'], sorted(leftover))
        self.assertFalse([n for n in os.listdir(self.userdata) if coldcopy.RESTORE_TEMP in n])
        # Life goes on after the restore (a home that is exactly the copy's would keep nothing: alreadyRestored).
        self.m.write('home/userdata/settings.json', '{"after the restore": true}')
        need = (sum(files[n]['bytes'] for n in want)
                + sum(os.path.getsize(os.path.join(self.userdata, n)) for n in coldcopy._replaced(self.userdata,
                                                                                               coldcopy.DEFAULT_RESTORE)))
        with mock.patch.object(coldcopy, '_free_bytes', return_value=need):
            coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        with mock.patch.object(coldcopy, '_free_bytes', return_value=need - 1), self.assertRaises(OSError) as e:
            coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        self.assertIn('kept copy of each file it replaces', str(e.exception))

    def test_a_restore_reads_its_temp_files_back_from_disk(self):
        # r2 nit: the temps were hashed only as written, not read back from disk like the kept files.
        real = coldcopy._hash
        with mock.patch.object(coldcopy, '_hash', lambda path, *a, **kw: 'torn' if coldcopy.RESTORE_TEMP in path
                               and not path.endswith('-aside') else real(path, *a, **kw)):
            with self.assertRaises(coldcopy.Incomplete) as e:
                self.restore()
        self.assertIn('does not read back', str(e.exception))
        self.assertEqual(self.all_hashes() - {sha(os.path.join(self.dest, 'userdata', n)) for n in
                                              ('statev2.sqlite', 'statev2.sqlite-wal', 'settings.json')},
                         set(self.newer.values()), 'the home was changed')
        self.assertFalse([n for n in os.listdir(self.userdata) if coldcopy.ASIDE in n], 'kept before checked')

    def test_a_finished_restore_leaves_no_record(self):
        facts = self.restore()
        self.assertFalse(os.path.lexists(os.path.join(self.userdata, coldcopy.RESTORE_MARK)))
        self.assertEqual(facts['alsoKept'], [])
        self.assertIsNone(coldcopy.stopped_restore(self.m.home))

    def test_an_unreadable_record_of_a_stopped_restore_is_refused_before_any_change(self):
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': [], 'names': ['../x']}))
        with self.assertRaises(RuntimeError) as e:
            coldcopy.stopped_restore(self.m.home)
        self.assertIn('which kept files are the old home is unknown', str(e.exception))
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', '{"oldHome": ')
        for call in (lambda: coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     self.restore):
            with self.assertRaises(RuntimeError) as e:
                call()
            self.assertIn('which kept files are the old home is unknown', str(e.exception))
        self.assertEqual(self.all_hashes() - {sha(os.path.join(self.userdata, coldcopy.RESTORE_MARK))},
                         set(self.newer.values()))

    def test_a_record_whose_kept_files_were_put_back_by_hand_keeps_the_home_as_the_old_home(self):
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'copy': self.dest, 'oldHome': [
            f'{n}.golive-aside-20260101T000000Z' for n in ('statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-shm')]}))
        facts = self.restore()
        self.assertIn('staleRecord', facts)
        self.assertEqual(facts['staleKept'], [])
        self.assertEqual(sorted(n.split(coldcopy.ASIDE)[0] for n in facts['movedAside']),
                         ['settings.json', 'statev2.sqlite', 'statev2.sqlite-shm', 'statev2.sqlite-wal'])
        self.assertLessEqual(set(self.newer.values()), self.all_hashes())
        self.assertFalse(os.path.lexists(os.path.join(self.userdata, coldcopy.RESTORE_MARK)))

    def test_a_stale_record_names_the_kept_files_it_named_that_are_still_there(self):
        # r4 data nit 2: when a record went stale, what earlier reruns kept (alsoKept) was named nowhere.
        stamp = '20260101T000000Z'
        old = [f'{n}{coldcopy.ASIDE}{stamp}' for n in ('statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-shm')]
        copied_back = f'settings.json{coldcopy.ASIDE}{stamp}'
        shutil.copy2(os.path.join(self.userdata, 'settings.json'), os.path.join(self.userdata, copied_back))
        also = f'statev2.sqlite{coldcopy.ASIDE}20260101T000001Z'
        self.m.write(f'home/userdata/{also}', b'what a T3 wrote on the half-way home')
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}',
                     json.dumps({'copy': self.dest, 'oldHome': old + [copied_back], 'alsoKept': [also]}))
        facts = self.restore()
        self.assertEqual(facts['staleKept'], [copied_back, also])
        self.assertIn(also, facts['staleRecord'])
        self.assertIn(copied_back, facts['staleRecord'])
        self.assertTrue(os.path.exists(os.path.join(self.userdata, also)))

    def test_a_kept_file_gone_from_both_its_kept_name_and_the_home_is_not_moved_back(self):
        # r4 data nit 3: a kept file deleted by mistake counted as moved back, and the record went stale.
        stamp = '20260101T000000Z'
        old = [f'{n}{coldcopy.ASIDE}{stamp}' for n in ('statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-shm',
                                                       'settings.json')]
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': old}))
        os.unlink(os.path.join(self.userdata, 'settings.json'))
        os.unlink(os.path.join(self.userdata, 'statev2.sqlite-shm'))  # a -shm need not be anywhere
        before = self.all_hashes()
        for call in (lambda: coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     self.restore):
            with self.assertRaises(RuntimeError) as e:
                call()
            self.assertIn(f'settings.json{coldcopy.ASIDE}{stamp} gone from the home too', str(e.exception))
            self.assertNotIn('statev2.sqlite-shm' + coldcopy.ASIDE, str(e.exception).split('gone from the home')[1])
        self.assertEqual(self.all_hashes(), before)

    def test_restore_checks_the_disk_again_under_the_ownership_lock(self):
        # r4 correctness nit 2: the disk was checked only before the lock was taken.
        locked, real = [], coldcopy.hold_owner_lock

        def hold(*a, **kw):
            locked.append(True)
            return real(*a, **kw)
        with mock.patch.object(coldcopy, 'hold_owner_lock', hold), \
                mock.patch.object(coldcopy, '_free_bytes', lambda path: 0 if locked else 1 << 50), \
                self.assertRaises(OSError) as e:
            self.restore()
        self.assertIn('not enough free disk', str(e.exception))
        self.assertEqual(self.all_hashes(), set(self.newer.values()))

    def test_a_refusal_for_restore_disk_names_kept_files_and_apart_the_ones_a_stopped_restore_needs(self):
        # r3 data 1: the refusal named nothing of the old home, so the owner could not see where the disk went.
        old = 'statev2.sqlite.golive-aside-20260101T000000Z'
        also = 'statev2.sqlite.golive-aside-20260101T000001Z'
        other = 'settings.json.golive-aside-20250101T000000Z'
        for n in (old, also, other):
            self.m.write(f'home/userdata/{n}', b'old')
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': [old], 'alsoKept': [also]}))
        with mock.patch.object(coldcopy, '_free_bytes', return_value=0), self.assertRaises(OSError) as e:
            coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        may_go, must_stay = str(e.exception).split('they must stay until it is done')
        self.assertIn(os.path.join(self.userdata, other), may_go)
        self.assertNotIn(old, may_go)
        self.assertNotIn(also, may_go)
        self.assertIn(os.path.join(self.userdata, old), must_stay)
        self.assertIn(os.path.join(self.userdata, also), must_stay)

    def test_a_rerun_after_a_finished_restore_never_names_the_restored_data_as_the_old_home(self):
        # r3 correctness nit 2: killed right after the record was removed, before the step ended. The rerun found
        # no record and kept the restored (copy's) data as the "old home".
        first = self.restore()
        before = sorted(os.listdir(self.userdata))
        again = self.restore()
        self.assertEqual(again['movedAside'], [])
        self.assertIn('alreadyRestored', again)
        self.assertEqual(sorted(os.listdir(self.userdata)), before, 'the restored data was kept again')
        self.assertLessEqual(set(self.newer.values()), self.all_hashes())
        self.assertTrue(all(os.path.exists(os.path.join(self.userdata, n)) for n in first['movedAside']))
        # A home that differs in one file is kept whole as before.
        self.m.write('home/userdata/settings.json', '{"changed": true}')
        third = self.restore()
        self.assertNotIn('alreadyRestored', third)
        self.assertEqual(sorted(n.split(coldcopy.ASIDE)[0] for n in third['movedAside']),
                         ['settings.json', 'statev2.sqlite', 'statev2.sqlite-wal'])

    def test_a_record_put_back_in_full_by_moving_or_copying_is_stale(self):
        stamp = '20260101T000000Z'
        names = {n: f'{n}{coldcopy.ASIDE}{stamp}' for n in ('statev2.sqlite', 'statev2.sqlite-wal',
                                                           'statev2.sqlite-shm', 'settings.json')}
        for n, k in names.items():  # the home is the old home; its kept files are copies of it
            shutil.copy2(os.path.join(self.userdata, n), os.path.join(self.userdata, k))
        os.unlink(os.path.join(self.userdata, names['statev2.sqlite']))  # moved back
        os.unlink(os.path.join(self.userdata, 'statev2.sqlite-shm'))      # the kept -shm need not go back
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'copy': self.dest,
                                                                         'oldHome': sorted(names.values())}))
        self.assertIsNone(coldcopy.stopped_restore(self.m.home))
        self.assertIn('staleRecord', self.restore())

    def test_a_moved_back_record_with_no_copy_and_no_sums_is_refused_not_stale(self):
        # Fork #244 item 3: a record that names no readable copy and holds no SHA256 of its kept files cannot tell
        # a kept file moved back from one deleted with the half-way home's file left in its place: it refuses.
        stamp = '20260101T000000Z'
        names = {n: f'{n}{coldcopy.ASIDE}{stamp}' for n in ('statev2.sqlite', 'statev2.sqlite-wal',
                                                           'statev2.sqlite-shm', 'settings.json')}
        self.m.write(f'home/userdata/{coldcopy.RESTORE_MARK}', json.dumps({'oldHome': sorted(names.values())}))
        before = self.all_hashes()
        for call in (lambda: coldcopy.stopped_restore(self.m.home),
                     lambda: coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     self.restore):
            with self.assertRaises(RuntimeError) as e:
                call()
            self.assertIn('the record names no copy', str(e.exception))
            self.assertIn('remove the record once the home is right', str(e.exception))
        self.assertEqual(self.all_hashes(), before)

    def test_a_restore_stopped_at_any_point_loses_nothing_and_never_pairs_a_wal_wrongly(self):
        # Should fix 2: was "moved aside one by one", which could leave a -wal without its database.
        stops = kill_at_each_change(self, self.restore, self.assert_whole_home)
        self.assertGreaterEqual(stops, 8)

    def assert_whole_home(self):
        """After a stop anywhere: no -wal without its database, the database paired only with its own WAL (or
        none), and every file the home had still there under some name."""
        def h(n):
            p = os.path.join(self.userdata, n)
            return sha(p) if os.path.exists(p) else None
        db, wal = h('statev2.sqlite'), h('statev2.sqlite-wal')
        new_db, new_wal = (sha(os.path.join(self.dest, 'userdata', n)) for n in ('statev2.sqlite', 'statev2.sqlite-wal'))
        self.assertIsNotNone(db, 'the home has no database')
        self.assertIn((db, wal), {(self.newer['statev2.sqlite'], self.newer['statev2.sqlite-wal']),
                                  (self.newer['statev2.sqlite'], None), (new_db, None), (new_db, new_wal)})
        if h('statev2.sqlite-shm') is not None:
            self.assertEqual((db, wal), (self.newer['statev2.sqlite'], self.newer['statev2.sqlite-wal']))
        self.assertLessEqual(set(self.newer.values()), self.all_hashes(), 'a file of the home was lost')

    def test_restore_takes_only_a_copy_of_that_home_under_that_build(self):
        for kw in ({'version': '0.1.8'}, {'from_home': self.m.path('another-home')}):
            with self.subTest(**kw):
                with self.assertRaises(coldcopy.WrongCopy):
                    self.restore(**kw)
                with self.assertRaises(coldcopy.WrongCopy):
                    coldcopy.read_manifest(self.dest, kw.get('from_home', self.m.home), kw.get('version', fx.LIVE))
        self.assertEqual(self.all_hashes(), set(self.newer.values()))

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
                coldcopy.restore(r, self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        self.assertIn('outside a journalled step', str(e.exception))
        self.assertEqual(self.all_hashes(), set(self.newer.values()))

    def test_plan_restore_is_the_read_only_pre_check(self):
        files, want = coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        self.assertEqual(sorted(want), ['settings.json', 'statev2.sqlite', 'statev2.sqlite-wal'])
        self.assertEqual(self.all_hashes(), set(self.newer.values()))
        with open(os.path.join(self.dest, 'userdata/settings.json'), 'a') as f:
            f.write(' ')
        with self.assertRaises(journal.Refused):
            with journal.run(self.m.state, 'back') as r:
                try:
                    coldcopy.plan_restore(self.dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
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


def kill_at_each_change(test, restore, check, after_stop=None):
    """Stop a restore at each change it makes to the home (a file copy, a link, an unlink or a rename: the restore
    has no clean-up, so stopping on an error there is a kill), call after_stop() (if given: what may happen before
    anyone looks, such as T3 starting at boot) and check() on what is left, then run the restore again (it must
    finish) and check() again; the home is put back between tries. Returns how many stops were tried."""
    userdata = test.userdata
    saved = tempfile.mkdtemp(prefix='golive-home-', dir=test.m.root)
    names = [n for n in os.listdir(userdata) if os.path.isfile(os.path.join(userdata, n))]
    for n in names:
        shutil.copy2(os.path.join(userdata, n), saved)
    real = {f: getattr(os, f) for f in ('link', 'unlink', 'rename')}
    real_copy = coldcopy._copy_file
    stop = 0
    while True:
        count = [0]

        def counted(f):
            def call(*args, **kw):
                count[0] += 1
                if count[0] > stop:
                    raise OSError(errno.EIO, f'stopped before change {count[0]}')
                return (real_copy if f == 'copy' else real[f])(*args, **kw)
            return call
        with mock.patch.multiple(coldcopy.os, **{f: counted(f) for f in real}), \
                mock.patch.object(coldcopy, '_copy_file', counted('copy')):
            try:
                restore()
                finished = True
            except OSError:
                finished = False
        with test.subTest(stop=stop):
            if not finished and after_stop is not None:
                after_stop()
            check()
            if not finished:
                with mock.patch.object(coldcopy, '_stamp', return_value=f'20990101T0000{stop:02d}Z'):
                    restore()
                check()
        if finished:
            return stop
        for n in os.listdir(userdata):  # put the home back for the next try (test files only)
            if os.path.isfile(os.path.join(userdata, n)) and not n.startswith('server-owner.sqlite'):
                os.unlink(os.path.join(userdata, n))
        for n in names:
            shutil.copy2(os.path.join(saved, n), userdata)
        stop += 1


class RealSqlite(CopyBase):
    """A real WAL-mode SQLite database with commits only in its -wal (and a -shm that is not copied)."""

    def setUp(self):
        super().setUp()
        self.userdata = self.m.path('home/userdata')
        self.db = os.path.join(self.userdata, 'statev2.sqlite')
        for n in ('statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-shm'):
            os.remove(os.path.join(self.userdata, n))
        self.write_rows(50, 'checkpoint')
        self.write_rows(30, 'leave-in-wal')

    def write_rows(self, n, how):
        subprocess.run([sys.executable, '-I', '-c', WAL_WRITER, self.db, str(n), how], check=True, timeout=60,
                       stdin=subprocess.DEVNULL)

    def open_rows(self, folder, with_wal=True):
        """(integrity_check, rows) of the database in folder, opened from a scratch copy of the database and its
        -wal only (no -shm: SQLite rebuilds it), so opening it changes nothing here."""
        scratch = tempfile.mkdtemp(prefix='golive-open-', dir=self.m.root)
        for n in ('statev2.sqlite',) + (('statev2.sqlite-wal',) if with_wal else ()):
            if os.path.exists(os.path.join(folder, n)):
                shutil.copy2(os.path.join(folder, n), scratch)
        c = sqlite3.connect(os.path.join(scratch, 'statev2.sqlite'))
        try:
            return c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT count(*) FROM t').fetchone()[0]
        finally:
            c.close()

    def test_a_cold_copy_of_a_wal_mode_database_opens_whole_with_every_commit(self):
        self.assertTrue(os.path.exists(self.db + '-shm'))
        self.assertGreater(os.path.getsize(self.db + '-wal'), 0)
        self.assertEqual(self.open_rows(self.userdata, with_wal=False), ('ok', 50), '30 commits are only in the WAL')
        dest = self.copy(owner_says_go=True)['copy']
        copied = os.path.join(dest, 'userdata')
        self.assertFalse(os.path.exists(os.path.join(copied, 'statev2.sqlite-shm')))
        self.assertEqual(self.open_rows(copied), ('ok', 80))

    def test_a_restore_of_a_wal_mode_database_stopped_anywhere_opens_whole(self):
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')    # life goes on: the database file changes,
        self.write_rows(20, 'leave-in-wal')  # and a new WAL holds more
        self.assertEqual(self.open_rows(self.userdata), ('ok', 120))
        self.adapter.stop()
        old = {n: sha(os.path.join(self.userdata, n)) for n in ('statev2.sqlite', 'statev2.sqlite-wal',
                                                                 'statev2.sqlite-shm', 'settings.json')}

        def restore():
            with journal.run(self.m.state, 'back') as r:
                with r.step('restore'):
                    coldcopy.restore(r, dest, self.m.home, from_home=self.m.home, version=fx.LIVE)

        def check_home():
            integrity, rows = self.open_rows(self.userdata)
            self.assertEqual(integrity, 'ok')
            self.assertIn(rows, (120, 100, 50, 80), 'neither the old nor the copied database')
            have = {sha(os.path.join(self.userdata, n)) for n in os.listdir(self.userdata)
                    if os.path.isfile(os.path.join(self.userdata, n)) and not n.startswith('server-owner')}
            self.assertLessEqual(set(old.values()), have, 'a file of the home was lost')
            if os.path.exists(self.db + '-wal'):
                self.assertTrue(os.path.exists(self.db))

        def settled_restore():
            try:
                restore()
            except Exception:
                journal.settle(self.m.state, 'test: looked at the home')
                raise
        kill_at_each_change(self, settled_restore, check_home)
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_t3_started_after_a_restore_stopped_anywhere_never_changes_the_kept_old_files(self):
        # r1 data-safety A: the kept database was a hard link to the live one. Stopped between the companion
        # unlinks and the database rename, then T3 started at boot: its writes went into the kept database too,
        # and the kept pair mixed old and new pages (and passed integrity_check).
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()

        mark = os.path.join(self.userdata, coldcopy.RESTORE_MARK)
        seen = {}

        def restore():
            seen.clear()
            seen['record'] = os.path.exists(mark)  # a restore before this one stopped after it named the old home
            try:
                with journal.run(self.m.state, 'back') as r, r.step('restore'):
                    seen['result'] = coldcopy.restore(r, dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
            except Exception:
                journal.settle(self.m.state, 'test: looked at the home')
                raise

        def t3_starts():  # the enabled service starts T3 at boot; it opens what the home holds and writes
            if os.path.exists(self.db):
                c = sqlite3.connect(self.db)
                c.execute("UPDATE t SET body='NEW SERVER' WHERE id <= 3")
                c.commit()
                c.close()

        def kept_files_are_the_old_home():
            # The old home is what the record names after a stop, and the result's movedAside after a rerun (r2
            # data N1: a rerun's newest kept set was the half-way home, and nothing said which set was the old one).
            if 'result' not in seen:
                if not os.path.exists(mark):  # stopped before anything left the home: the home is the old home
                    self.assertEqual(self.open_rows(self.userdata)[1], 120)
                    return
                with open(mark) as f:
                    old_home = json.load(f)['oldHome']
                new_server_writes = 0
            else:
                old_home = seen['result']['movedAside']
                # A rerun with no record found the home whole (the old home), so T3's writes there belong to it.
                new_server_writes = None if not seen['record'] else 0
            kept = {n.split(coldcopy.ASIDE)[0]: n for n in old_home}
            # The home before the first restore had all four; a T3 that started on a whole home and closed cleanly
            # checkpointed its WAL away, so then the home (the old home) has no WAL and no -shm.
            whole = {'statev2.sqlite', 'settings.json'} | ({'statev2.sqlite-wal', 'statev2.sqlite-shm'}
                                                           if new_server_writes is not None else set())
            self.assertLessEqual(whole, set(kept), 'the old home is not named whole')
            scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
            for n in ('statev2.sqlite', 'statev2.sqlite-wal'):
                if n in kept:
                    shutil.copy2(os.path.join(self.userdata, kept[n]), os.path.join(scratch, n))
            c = sqlite3.connect(os.path.join(scratch, 'statev2.sqlite'))
            try:
                self.assertEqual(c.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
                self.assertEqual(c.execute('SELECT count(*) FROM t').fetchone()[0], 120)
                if new_server_writes is not None:
                    self.assertEqual(c.execute("SELECT count(*) FROM t WHERE body='NEW SERVER'").fetchone()[0], 0,
                                     'a T3 started after the stop changed the kept old files')
            finally:
                c.close()
            if 'result' in seen and seen['record']:
                # What a T3 started on the half-way home wrote is kept too (alsoKept), never lost.
                also = {n.split(coldcopy.ASIDE)[0] for n in seen['result']['alsoKept']}
                self.assertNotIn('settings.json', also, 'an unchanged file was kept again')
                self.assertIn('statev2.sqlite', also, 'what T3 wrote on the half-way home was not kept')
        stops = kill_at_each_change(self, restore, kept_files_are_the_old_home, after_stop=t3_starts)
        self.assertGreaterEqual(stops, 12)

    def test_newest_kept_set_after_a_rerun_is_the_old_home(self):
        # r2 data N1 (the lens probe RerunAsideProbe, as written): stopped between the WAL unlink and the database
        # rename, the rerun kept the half-way home (database and settings, no WAL) as the newest "old" set.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()

        def restore(stamp):
            with mock.patch.object(coldcopy, '_stamp', return_value=stamp), \
                    journal.run(self.m.state, 'back') as r, r.step('restore'):
                return coldcopy.restore(r, dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        real = os.rename

        def rn(a, b, *k, **kw):
            if b.endswith('statev2.sqlite'):
                raise OSError(5, 'killed before the db rename')
            return real(a, b, *k, **kw)
        with mock.patch.object(coldcopy.os, 'rename', rn), self.assertRaises(OSError):
            restore('20990101T000001Z')
        journal.settle(self.m.state, 'probe')
        self.assertFalse(os.path.exists(self.db + '-wal'), 'not stopped where the probe means')
        result = restore('20990101T000002Z')
        newest = {n.split(coldcopy.ASIDE)[0] for n in result['movedAside']}
        self.assertIn('statev2.sqlite-wal', newest)
        # Nothing new was in the half-way home, so nothing was kept again; the named set opens with every commit.
        self.assertEqual(result['alsoKept'], [])
        self.assertTrue(all('20990101T000001Z' in n for n in result['movedAside']), result['movedAside'])
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        for n in result['movedAside']:
            if n.startswith('statev2.sqlite.') or n.startswith('statev2.sqlite-wal.'):
                shutil.copy2(os.path.join(self.userdata, n), os.path.join(scratch, n.split(coldcopy.ASIDE)[0]))
        self.assertEqual(self.open_rows(scratch), ('ok', 120))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))


    def stop_before_the_db_rename(self, dest, stamp):
        """A restore stopped between the WAL removal and the database rename (the r2 probe's stop)."""
        real = os.rename

        def rn(a, b, *k, **kw):
            if b.endswith('statev2.sqlite'):
                raise OSError(5, 'killed before the db rename')
            return real(a, b, *k, **kw)
        with mock.patch.object(coldcopy.os, 'rename', rn), self.assertRaises(OSError):
            self.restore_at(dest, stamp)
        journal.settle(self.m.state, 'probe')
        self.assertFalse(os.path.exists(self.db + '-wal'), 'not stopped where the probe means')

    def restore_at(self, dest, stamp):
        with mock.patch.object(coldcopy, '_stamp', return_value=stamp), \
                journal.run(self.m.state, 'back') as r, r.step('restore'):
            return coldcopy.restore(r, dest, self.m.home, from_home=self.m.home, version=fx.LIVE)

    def home_hashes(self):
        return {n: sha(os.path.join(self.userdata, n)) for n in os.listdir(self.userdata)
                if os.path.isfile(os.path.join(self.userdata, n)) and not n.startswith('server-owner')}

    def test_a_partly_put_back_record_still_names_the_old_wal(self):
        # r3 correctness 1 / data 2 (PartPutBackProbe): the owner moved back only the kept database, not its WAL;
        # the rerun called the record stale and kept the half-way home (no WAL) as the old home.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        self.stop_before_the_db_rename(dest, '20990101T000001Z')
        os.rename(os.path.join(self.userdata, 'statev2.sqlite' + coldcopy.ASIDE + '20990101T000001Z'), self.db)
        before = self.home_hashes()
        for call in (lambda: coldcopy.plan_restore(dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     lambda: self.restore_at(dest, '20990101T000002Z')):
            with self.assertRaises(RuntimeError) as e:
                call()
            self.assertIn('only partly put back', str(e.exception))
            self.assertIn('statev2.sqlite-wal' + coldcopy.ASIDE, str(e.exception))
        self.assertEqual(self.home_hashes(), before, 'a refused restore changed the home')
        journal.settle(self.m.state, 'probe: looked')
        # Finishing the put-back makes the home the old home again: the rerun keeps it whole, every commit in it.
        os.rename(os.path.join(self.userdata, 'statev2.sqlite-wal' + coldcopy.ASIDE + '20990101T000001Z'),
                  self.db + '-wal')
        result = self.restore_at(dest, '20990101T000003Z')
        self.assertIn('staleRecord', result)
        kept = {n.split(coldcopy.ASIDE)[0]: n for n in result['movedAside']}
        self.assertLessEqual({'statev2.sqlite', 'statev2.sqlite-wal', 'settings.json'}, set(kept))
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        for n in ('statev2.sqlite', 'statev2.sqlite-wal'):
            shutil.copy2(os.path.join(self.userdata, kept[n]), os.path.join(scratch, n))
        self.assertEqual(self.open_rows(scratch), ('ok', 120))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def put_back_by_hand(self):
        """The owner puts back every kept file the record names (moved back over the half-way home's files)."""
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            record = json.load(f)
        for n in record['oldHome']:
            os.replace(os.path.join(self.userdata, n), os.path.join(self.userdata, n.split(coldcopy.ASIDE)[0]))
        return record

    def assert_foreign_wal_refused_then_stale_whole(self, dest):
        before = self.home_hashes()
        for call in (lambda: coldcopy.plan_restore(dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     lambda: self.restore_at(dest, '20990101T000002Z')):
            with self.assertRaises(RuntimeError) as e:
                call()
            said = str(e.exception)
            self.assertIn("statev2.sqlite-wal is the copy's", said)
            self.assertIn('do NOT remove the record', said)
            self.assertNotIn('remove the record instead', said)
        journal.settle(self.m.state, 'probe: looked')
        self.assertEqual(self.home_hashes(), before, 'a refused restore changed the home')
        os.unlink(self.db + '-wal')  # what the README says: remove the half-way home's -wal
        for n in ('-shm', '-journal'):
            if os.path.exists(self.db + n):
                os.unlink(self.db + n)
        result = self.restore_at(dest, '20990101T000003Z')
        self.assertIn('staleRecord', result)
        kept = {n.split(coldcopy.ASIDE)[0]: n for n in result['movedAside']}
        self.assertNotIn('statev2.sqlite-wal', kept)
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        shutil.copy2(os.path.join(self.userdata, kept['statev2.sqlite']), os.path.join(scratch, 'statev2.sqlite'))
        self.assertEqual(self.open_rows(scratch), ('ok', 100))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_a_put_back_with_the_copys_wal_left_is_refused_stopped_before_the_record_removal(self):
        # r4 data 1 (StaleForeignWalProbe): the old home had no -wal; a stop before the record's removal, then a
        # full put-back left the copy's WAL beside the old database, and the rerun kept that mix as the old home.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.assertFalse(os.path.exists(self.db + '-wal'), 'the old home must have no -wal for this probe')
        real = os.unlink

        def ul(p, *k, **kw):
            if p.endswith(coldcopy.RESTORE_MARK):
                raise OSError(5, 'killed before the record removal')
            return real(p, *k, **kw)
        with mock.patch.object(coldcopy.os, 'unlink', ul), self.assertRaises(OSError):
            self.restore_at(dest, '20990101T000001Z')
        journal.settle(self.m.state, 'probe')
        self.put_back_by_hand()
        self.assert_foreign_wal_refused_then_stale_whole(dest)

    def test_a_put_back_with_the_copys_wal_left_is_refused_stopped_after_the_wal_rename(self):
        # r4 correctness 1 (ForeignWalProbe): stopped after the copy's WAL went in, before settings.json.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        real = os.rename

        def rn(a, b, *k, **kw):
            if b.endswith('settings.json'):
                raise OSError(5, 'killed after the WAL rename')
            return real(a, b, *k, **kw)
        with mock.patch.object(coldcopy.os, 'rename', rn), self.assertRaises(OSError):
            self.restore_at(dest, '20990101T000001Z')
        journal.settle(self.m.state, 'probe')
        self.put_back_by_hand()
        self.assert_foreign_wal_refused_then_stale_whole(dest)

    def stop_after_the_wal_rename(self, dest, stamp):
        """A restore stopped after the copy's WAL went in, before settings.json (the r4 ForeignWalProbe's stop)."""
        real = os.rename

        def rn(a, b, *k, **kw):
            if b.endswith('settings.json'):
                raise OSError(5, 'killed after the WAL rename')
            return real(a, b, *k, **kw)
        with mock.patch.object(coldcopy.os, 'rename', rn), self.assertRaises(OSError):
            self.restore_at(dest, stamp)
        journal.settle(self.m.state, 'probe')

    def refusals(self, dest):
        """The messages of plan_restore and restore, both refused, the home unchanged by either."""
        before, said = self.home_hashes(), []
        for call in (lambda: coldcopy.plan_restore(dest, self.m.home, from_home=self.m.home, version=fx.LIVE),
                     lambda: self.restore_at(dest, '20990101T000009Z')):
            with self.assertRaises(RuntimeError) as e:
                call()
            said.append(str(e.exception))
        journal.settle(self.m.state, 'probe: looked')
        self.assertEqual(self.home_hashes(), before, 'a refused restore changed the home')
        return said

    def test_a_wal_t3_wrote_after_the_put_back_cannot_be_told_and_the_refusal_says_how_to_tell(self):
        # r5 finding 1 (both lenses): the refusal led with "Remove them" and called T3's own WAL "a WAL of another
        # database"; following it dropped the home from 107 to 100 rows. Fork #244: a WAL that is not the copy's
        # may still be a T3's on the copy's database (its WAL started again after a checkpoint), so the kit no
        # longer claims it is the put-back home's: it names both and how to tell them apart on scratch copies.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.stop_after_the_wal_rename(dest, '20990101T000001Z')
        self.put_back_by_hand()
        os.unlink(self.db + '-wal')  # the copy's WAL removed as the README says, but the record forgotten
        self.write_rows(7, 'leave-in-wal')  # T3 runs on the put-back home, killed with 7 commits only in its WAL
        self.assertEqual(self.open_rows(self.userdata), ('ok', 107))
        for said in self.refusals(dest):
            self.assertIn("whose statev2.sqlite-wal, statev2.sqlite-shm are cannot be told", said)
            self.assertIn('not its SHA256, not its WAL header', said)
            self.assertNotIn('remove the record instead', said)
            self.assertNotIn("is the copy's", said)
            self.assertNotIn('another database', said)
            self.assert_both_ways_and_how_to_tell(said)
        # The owner compares on scratch copies, as the refusal says: with the WAL the database holds what it holds
        # alone plus newer rows, so it is the put-back home's WAL: keep it, remove the record.
        self.assertEqual(self.open_rows(self.userdata, with_wal=False), ('ok', 100))
        os.unlink(os.path.join(self.userdata, coldcopy.RESTORE_MARK))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 107))
        result = self.restore_at(dest, '20990101T000002Z')  # the next restore keeps the whole home
        kept = {n.split(coldcopy.ASIDE)[0]: n for n in result['movedAside']}
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        for n in ('statev2.sqlite', 'statev2.sqlite-wal'):
            shutil.copy2(os.path.join(self.userdata, kept[n]), os.path.join(scratch, n))
        self.assertEqual(self.open_rows(scratch), ('ok', 107))

    def test_a_stray_shm_with_no_wal_is_named_always_safe_to_remove(self):
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.stop_after_the_wal_rename(dest, '20990101T000001Z')
        self.put_back_by_hand()
        os.unlink(self.db + '-wal')
        with open(self.db + '-shm', 'wb') as f:
            f.write(b's' * 64)
        for said in self.refusals(dest):
            self.assertIn('statev2.sqlite-shm is a -shm with no -wal', said)
            self.assertIn('always safe to remove (then run the restore again)', said)
            self.assertNotIn('record', said.split('did not have:')[1])
        os.unlink(self.db + '-shm')
        self.assertIn('staleRecord', self.restore_at(dest, '20990101T000002Z'))

    def test_the_copys_wal_from_a_rerun_with_another_copy_is_named_the_copys(self):
        # The record names every copy its reruns restored from, so a WAL the second copy put in is the copy's too.
        self.distinct_stamps()
        first = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'leave-in-wal')
        second = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.stop_after_the_wal_rename(first, '20990101T000001Z')
        real = os.rename

        def rn(a, b, *k, **kw):
            if b.endswith('settings.json'):
                raise OSError(5, 'killed after the WAL rename')
            return real(a, b, *k, **kw)
        with mock.patch.object(coldcopy.os, 'rename', rn), self.assertRaises(OSError), \
                mock.patch.object(coldcopy, '_stamp', return_value='20990101T000002Z'), \
                journal.run(self.m.state, 'back') as r, r.step('restore'):
            coldcopy.restore(r, second, self.m.home, from_home=self.m.home, version=fx.LIVE)
        journal.settle(self.m.state, 'probe')
        self.assertEqual(sha(self.db + '-wal'), sha(os.path.join(second, 'userdata', 'statev2.sqlite-wal')))
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            self.assertEqual(json.load(f)['alsoCopies'], [second])
        self.put_back_by_hand()
        for said in self.refusals(first):
            self.assertIn("statev2.sqlite-wal is the copy's", said)
            self.assertIn('do NOT remove the record', said)

    def assert_both_ways_and_how_to_tell(self, said):
        """Fork #244 items 1 and 4: a WAL whose owner cannot be told is named with both readings, how to tell them
        apart without the copy folder, and a way out that loses nothing."""
        self.assertIn('keep it and remove the record', said)  # the put-back home's
        self.assertIn('keep the record and run the restore again', said)  # the half-way home's
        self.assertIn('scratch copies', said)
        self.assertIn('row counts', said)
        self.assertIn('move it out of userdata (do not delete it)', said)

    def test_a_wal_beside_a_record_whose_copy_cannot_be_read_is_named_unknown(self):
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.stop_after_the_wal_rename(dest, '20990101T000001Z')
        self.put_back_by_hand()
        os.rename(os.path.join(dest, coldcopy.MANIFEST), os.path.join(dest, 'moved-away.json'))
        with self.assertRaises(RuntimeError) as e:
            coldcopy.stopped_restore(self.m.home)
        self.assertIn('whose statev2.sqlite-wal is cannot be told', str(e.exception))
        self.assertIn(f'no readable manifest in {dest}', str(e.exception))
        self.assertNotIn('remove the record instead', str(e.exception))
        self.assert_both_ways_and_how_to_tell(str(e.exception))

    def test_a_wal_with_the_copys_frames_and_t3_s_after_them_is_the_copy_s(self):
        # Fork #244 item 1 (lens228cr6 finding 1, its probe): the old home had no -wal; a restore stopped after
        # the copy's WAL went in; a T3 ran on that half-way home and was killed with 7 commits in the copy's WAL;
        # the owner moved the kept files back. The WAL is no longer the copy's byte for byte, and the refusal said
        # "keep it, remove the record": the home then opened with 87 rows (the copy's 80 + 7), not the old 100.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.assertFalse(os.path.exists(self.db + '-wal'))
        self.stop_after_the_wal_rename(dest, '20990101T000001Z')
        copys_wal = sha(self.db + '-wal')
        self.write_rows(7, 'leave-in-wal')  # T3 on the half-way home, killed
        self.assertNotEqual(sha(self.db + '-wal'), copys_wal)
        self.put_back_by_hand()
        self.assertEqual(self.open_rows(self.userdata, with_wal=False), ('ok', 100))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 87), 'the mixed WAL serves the copy\'s rows')
        for said in self.refusals(dest):
            self.assertNotIn('remove the record instead', said)
            self.assertIn("statev2.sqlite-wal, statev2.sqlite-shm are the copy's WAL with more commits after", said)
            self.assertIn('do NOT remove the record', said)
            self.assertIn('move them out of userdata', said)
        # Following the advice: the WAL goes (moved out, kept by the owner), the record stays, the restore runs
        # again and keeps the whole old home.
        for n in ('-wal', '-shm'):
            os.rename(self.db + n, self.m.path('moved-out' + n))
        result = self.restore_at(dest, '20990101T000002Z')
        self.assertIn('staleRecord', result)
        kept = {n.split(coldcopy.ASIDE)[0]: n for n in result['movedAside']}
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        shutil.copy2(os.path.join(self.userdata, kept['statev2.sqlite']), os.path.join(scratch, 'statev2.sqlite'))
        self.assertEqual(self.open_rows(scratch), ('ok', 100))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_a_copied_back_put_back_names_the_copys_wal(self):
        # Fork #244 item 2 (lens228dr6 finding 1, its probe): the owner copied the kept files back (not moved), so
        # no kept file was gone and the record was taken as live: nothing named the copy's WAL beside the old
        # database, and removing the record (as the copy's refusal says) left the old database with the copy's WAL.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.adapter.stop()
        self.stop_after_the_wal_rename(dest, '20990101T000001Z')
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            rec = json.load(f)
        for n in rec['oldHome']:
            shutil.copy2(os.path.join(self.userdata, n), os.path.join(self.userdata, n.split(coldcopy.ASIDE)[0]))
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80), 'old database + the copy\'s WAL: no error')
        with self.assertRaises(RuntimeError) as e:
            coldcopy.stopped_restore(self.m.home, dest)
        self.assertIn("statev2.sqlite-wal is the copy's", str(e.exception))
        self.assertIn('do NOT remove the record', str(e.exception))
        for said in self.refusals(dest):
            self.assertIn("statev2.sqlite-wal is the copy's", said)
        # The copy's own refusal (a copy while the record is there) names it too, not only "a restore run would".
        problem = coldcopy._home_problem(config.load())
        self.assertIn("statev2.sqlite-wal is the copy's", problem)
        # Following the advice: remove the copy's WAL, keep the record, run the restore again.
        os.unlink(self.db + '-wal')
        result = self.restore_at(dest, '20990101T000002Z')
        self.assertEqual(result['movedAside'], rec['oldHome'], 'the copied-back record still names the old home')
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))
        scratch = tempfile.mkdtemp(prefix='golive-kept-', dir=self.m.root)
        shutil.copy2(os.path.join(self.userdata, rec['oldHome'][0]), os.path.join(scratch, 'statev2.sqlite'))
        self.assertEqual(self.open_rows(scratch), ('ok', 100))

    def test_a_restore_stopped_once_the_old_files_are_kept_is_rerun_with_no_second_kept_set(self):
        # The state question for item 2: right after the kept files are written, every kept file equals its home
        # file, as after a copy-back. Nothing of the half-way home is there, so it is not refused, and the rerun
        # keeps nothing again (the disk the first run fit is enough).
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        real = os.unlink

        def ul(p, *k, **kw):
            if os.path.basename(p).startswith('statev2.sqlite') and coldcopy.ASIDE not in p:
                raise OSError(5, 'killed before the first removal')
            return real(p, *k, **kw)
        with mock.patch.object(coldcopy.os, 'unlink', ul), self.assertRaises(OSError):
            self.restore_at(dest, '20990101T000001Z')
        journal.settle(self.m.state, 'probe')
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            rec = json.load(f)
        self.assertIn('statev2.sqlite-wal' + coldcopy.ASIDE + '20990101T000001Z', rec['oldHome'])
        self.assertEqual(coldcopy.stopped_restore(self.m.home, dest), rec)
        result = self.restore_at(dest, '20990101T000002Z')
        self.assertEqual((result['movedAside'], result['alsoKept']), (rec['oldHome'], []))
        self.assertFalse([n for n in os.listdir(self.userdata) if '20990101T000002Z' in n], 'kept again')
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_a_deleted_kept_wal_beside_a_record_whose_copy_cannot_be_read_is_refused(self):
        # Fork #244 item 3 (lens228cr6 nit 2): the record's copy cannot be read (pruned), the owner moved the
        # database back but deleted the kept -wal, so the home's -wal is the copy's. Without the copy's SHA256 the
        # deleted kept file looked moved back, and the record was called stale (the old database + the copy's WAL).
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        self.kill_before_the_record_removal(dest, '20990101T000001Z')
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            rec = json.load(f)
        for n in rec['oldHome']:
            b = n.split(coldcopy.ASIDE)[0]
            (os.unlink(os.path.join(self.userdata, n)) if b.endswith(('-wal', '-shm'))
             else os.replace(os.path.join(self.userdata, n), os.path.join(self.userdata, b)))
        os.rename(os.path.join(dest, coldcopy.MANIFEST), os.path.join(dest, 'moved-away.json'))
        before = self.home_hashes()
        with self.assertRaises(RuntimeError) as e:
            coldcopy.stopped_restore(self.m.home)
        said = str(e.exception)
        self.assertIn('only partly put back', said)
        self.assertIn('statev2.sqlite-wal' + coldcopy.ASIDE + '20990101T000001Z', said)
        self.assertIn(f'no readable manifest in {dest}', said)
        self.assertIn('scratch copies', said)
        self.assertEqual(self.home_hashes(), before)

    def test_a_full_move_back_beside_a_record_whose_copy_cannot_be_read_is_still_stale(self):
        # The other side of item 3: every kept file moved back holds the kept file's bytes (the record's 'sums'),
        # so it is moved back for sure, readable copy or not.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        self.kill_before_the_record_removal(dest, '20990101T000001Z')
        self.put_back_by_hand()
        os.rename(os.path.join(dest, coldcopy.MANIFEST), os.path.join(dest, 'moved-away.json'))
        self.assertIsNone(coldcopy.stopped_restore(self.m.home))

    def kill_before_the_record_removal(self, dest, stamp):
        real = os.unlink

        def ul(p, *k, **kw):
            if p.endswith(coldcopy.RESTORE_MARK):
                raise OSError(5, 'killed before the record removal')
            return real(p, *k, **kw)
        with mock.patch.object(coldcopy.os, 'unlink', ul), self.assertRaises(OSError):
            self.restore_at(dest, stamp)
        journal.settle(self.m.state, 'probe')

    def test_deleted_kept_wal_with_the_copys_wal_in_the_home_is_lost(self):
        # r5 finding 2 (the data lens's failing test): the old home had a -wal; the owner moved the database back
        # but deleted the kept -wal and -shm, so the home's -wal is the copy's. It was taken as the old home.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        self.kill_before_the_record_removal(dest, '20990101T000001Z')
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            rec = json.load(f)
        for n in rec['oldHome']:
            b = n.split(coldcopy.ASIDE)[0]
            (os.unlink(os.path.join(self.userdata, n)) if b.endswith(('-wal', '-shm'))
             else os.replace(os.path.join(self.userdata, n), os.path.join(self.userdata, b)))
        for said in self.refusals(dest):
            self.assertIn('only partly put back', said)
            self.assertIn("the home's statev2.sqlite-wal holds the bytes of the copy's file", said)

    def test_a_put_back_of_files_the_copy_holds_byte_for_byte_is_stale_not_lost(self):
        # The old home's files equal the copy's (no change since the copy): moved back, they are the kept bytes,
        # so the home is the old home. A record without 'sums' (none was ever written) cannot tell: it refuses.
        dest = self.copy(owner_says_go=True)['copy']
        self.adapter.stop()
        self.kill_before_the_record_removal(dest, '20990101T000001Z')
        record = self.put_back_by_hand()
        self.assertEqual(sha(self.db + '-wal'), sha(os.path.join(dest, 'userdata', 'statev2.sqlite-wal')))
        mark = os.path.join(self.userdata, coldcopy.RESTORE_MARK)
        with open(mark, 'w') as f:
            json.dump({k: v for k, v in record.items() if k != 'sums'}, f)
        with self.assertRaises(RuntimeError) as e:
            coldcopy.stopped_restore(self.m.home, dest)
        self.assertIn('not moved back', str(e.exception))
        with open(mark, 'w') as f:
            json.dump(record, f)
        result = self.restore_at(dest, '20990101T000002Z')
        self.assertIn('staleRecord', result)
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_a_rerun_after_a_stop_fits_on_the_disk_the_first_run_fit(self):
        # r3 data 1: the rerun counted a kept copy of every home file again, though the record holds the old home.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(3000, 'checkpoint')
        self.write_rows(5, 'leave-in-wal')
        self.adapter.stop()

        def used():  # the ownership lock's files hold no data (restore holds the lock while it checks the disk)
            return sum(os.lstat(os.path.join(self.userdata, n)).st_size for n in os.listdir(self.userdata)
                       if os.path.isfile(os.path.join(self.userdata, n)) and not n.startswith(coldcopy.OWNER_LOCK))
        files, want = coldcopy.plan_restore(dest, self.m.home, from_home=self.m.home, version=fx.LIVE)
        budget = (used() + sum(files[n]['bytes'] for n in want)
                  + sum(os.lstat(os.path.join(self.userdata, n)).st_size
                        for n in coldcopy._replaced(self.userdata, coldcopy.DEFAULT_RESTORE)))
        with mock.patch.object(coldcopy, '_free_bytes', lambda path: budget - used()):
            self.stop_before_the_db_rename(dest, '20990101T000001Z')
            result = self.restore_at(dest, '20990101T000002Z')
        self.assertIn('statev2.sqlite-wal', {n.split(coldcopy.ASIDE)[0] for n in result['movedAside']})
        self.assertEqual(result['alsoKept'], [])
        self.assertEqual(self.open_rows(self.userdata), ('ok', 80))

    def test_a_third_run_still_names_what_a_stopped_rerun_kept(self):
        # r3 nit (data 3, correctness 3): a rerun's alsoKept was not added to the record.
        dest = self.copy(owner_says_go=True)['copy']
        self.write_rows(20, 'checkpoint')
        self.write_rows(20, 'leave-in-wal')
        self.adapter.stop()
        self.stop_before_the_db_rename(dest, '20990101T000001Z')
        c = sqlite3.connect(self.db)  # a T3 started at boot on the half-way home writes
        c.execute("UPDATE t SET body='NEW SERVER' WHERE id <= 3")
        c.commit()
        c.close()
        wrote = sha(self.db)
        self.stop_before_the_db_rename(dest, '20990101T000002Z')
        with open(os.path.join(self.userdata, coldcopy.RESTORE_MARK)) as f:
            record = json.load(f)
        self.assertTrue(record['alsoKept'] and all('20990101T000002Z' in n for n in record['alsoKept']), record)
        self.assertTrue(all('20990101T000001Z' in n for n in record['oldHome']), record)
        result = self.restore_at(dest, '20990101T000003Z')
        self.assertEqual(result['alsoKept'], record['alsoKept'])
        self.assertEqual(result['movedAside'], record['oldHome'])
        self.assertIn(wrote, {sha(os.path.join(self.userdata, n)) for n in result['alsoKept']})
        self.assertFalse([n for n in os.listdir(self.userdata) if '20990101T000003Z' in n], 'kept again')


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
