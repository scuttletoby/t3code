"""The gap: while the kit has T3 stopped, an SSH launch of the T3 desktop app must not start its own server on the
home (lib/golive/handoff.py). On the fake machine (tests/golive_fixture.py), with a model of upstream's launch script
(packages/ssh/src/tunnel.ts) and of the boot service's launcher (apps/server/src/serviceLauncher.ts):

- a FRESH launch (the desktop notices its server is gone) waits while upstream's update marker is valid, then while
  the home is owned; a launch that WAITED through a long stop already spent its marker wait (60 s at most) and only
  waits while the home is owned. Either one that finds the home free starts its own `t3 serve`: here a process that
  takes the home's owner lock and writes the runtime record, and is no child of the service (as in the incidents);
- the boot service's launcher (from SERVING_LOCK_SINCE on) takes runtime/service-serving.lock, then its server
  waits for the owner lock for a moment (upstream's busy timeout); a server that finds it held idles (the launcher
  waits for the home: in the fake, for good).

The Race classes reproduce the incidents (#513, and the 11-19 switch): they fail on a kit without the marker and the
handover. python3 -I -B fork-kit/golive/tests/gap.test.py"""
import importlib.util, json, os, shutil, signal, sqlite3, subprocess, sys, threading, time, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import golive_fixture as fx  # noqa: E402
import back_fixture as bx  # noqa: E402
from golive import back, coldcopy, config, journal, live, switch  # noqa: E402

NODE = shutil.which('node')  # before a test's fake PATH


def _load(name):
    spec = importlib.util.spec_from_file_location(name.replace('.', '_'), os.path.join(HERE, f'{name}.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


copy_t, switch_t, back_t = _load('copy.test'), _load('switch.test'), _load('back.test')
NEW = '0.1.11-nightly.20261118.4890'  # a build whose launcher takes the serving lock
TTL = 60_000

# The hand server an SSH launch starts: not a child of the service (its parent exits at once), it owns the home.
HAND_SERVER = '''import json, os, sqlite3, subprocess, sys
home = sys.argv[1]
if os.fork():
    os._exit(0)
os.setsid()
c = sqlite3.connect(os.path.join(home, 'userdata', 'server-owner.sqlite'), isolation_level=None)
c.execute('BEGIN EXCLUSIVE')
with open(os.path.join(home, 'userdata', 'server-runtime.json'), 'w') as f:
    json.dump({'version': 1, 'pid': os.getpid(), 'port': 3773, 'origin': 'http://127.0.0.1:3773',
               'startedAt': '2026-01-01T00:00:00Z', 'ownerId': 'hand', 'serverVersion': sys.argv[2]}, f)
with open(sys.argv[3], 'w') as f:
    f.write(str(os.getpid()))
import time
time.sleep(120)
'''


def upstream_wait_ms(home):
    """Upstream's reader (DesktopUpdateMarker.remainingWait; the launch script's node fallback): written here again,
    from upstream's code, not from the kit's."""
    try:
        with open(os.path.join(home, 'runtime', 'desktop-update-in-progress'), encoding='utf-8') as f:
            m = json.load(f)
        started, expires = m['startedAt'], m['expiresAt']
    except (OSError, ValueError, KeyError, TypeError):
        return None
    now = time.time() * 1000
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (started, expires)):
        return None
    if started <= now and expires > now and expires > started:
        return min(expires - now, TTL)
    return None


def held(path):
    """Another connection holds this SQLite lock (upstream's probe: a missing file is not held)."""
    if not os.path.exists(path):
        return False
    c = sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=0, isolation_level=None)
    try:
        c.execute('BEGIN EXCLUSIVE')
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        c.close()


def home_owned(home):
    """`__ssh-helper home-owned`: a launcher serves, or a server (or the kit) holds the owner lock."""
    return held(os.path.join(home, 'runtime', 'service-serving.lock')) or \
        held(os.path.join(home, 'userdata', 'server-owner.sqlite'))


class Race:
    """The SSH launches and the launcher on one fake machine; `log` says what each launch did."""

    def __init__(self, test, machine):
        self.t, self.m, self.log, self.hands, self.serving = test, machine, [], [], None
        test.addCleanup(self.close)

    def launch(self, kind, version=fx.LIVE):
        """kind: 'fresh', 'waited', or 'old' (an SSH client older than 0.1.10: it honours neither)."""
        home = self.m.home
        if kind == 'fresh' and upstream_wait_ms(home) is not None:
            return self.log.append((kind, 'waits: update marker'))
        if kind != 'old' and home_owned(home):
            return self.log.append((kind, 'waits: home owned'))
        pid_file = self.m.path(f'hand-{len(self.hands)}.pid')
        subprocess.run([sys.executable, '-I', '-c', HAND_SERVER, home, version, pid_file], check=True, timeout=10)
        for _ in range(500):
            if os.path.exists(pid_file) and os.path.getsize(pid_file):
                break
            time.sleep(0.01)
        with open(pid_file) as f:
            self.hands.append(int(f.read()))
        self.log.append((kind, f'started a hand server, pid {self.hands[-1]}'))

    def launcher(self, start_server, claims_serving=True):
        """The boot service's launcher: the serving lock first (new builds), then its server meets the owner lock
        (upstream's 250 ms busy timeout, given more room here); returns False when it idles."""
        if claims_serving:
            self.serving = sqlite3.connect(self.m.path('home/runtime/service-serving.lock'), isolation_level=None,
                                           check_same_thread=False)
            self.serving.execute('BEGIN EXCLUSIVE')
            self.launch('waited')  # a launch in the moment the launcher starts its server
        owner = os.path.join(self.m.home, 'userdata', 'server-owner.sqlite')
        end = time.monotonic() + 1.0
        while held(owner):
            if time.monotonic() >= end:
                return False
            time.sleep(0.01)
        start_server()
        return True

    def stopped(self):
        if self.serving is not None:
            self.serving.close()
            self.serving = None

    def close(self):
        self.stopped()
        for pid in self.hands:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


class RaceAdapter(fx.FakeAdapter):
    """FakeAdapter whose stop lets fresh launches look, and whose start lets launches look first (the incidents'
    second), then runs the launcher model."""

    def __init__(self, root, machine, race, claims_serving=True, on=None):
        super().__init__(root, machine, on)
        self.race, self.claims_serving, self.markers = race, claims_serving, []
        self.after_stop, self.before_start = ['fresh'], ['waited', 'fresh']

    def stop(self):
        super().stop()
        self.race.stopped()
        self.markers.append(('after stop', upstream_wait_ms(self.machine.home)))
        for kind in self.after_stop:
            self.race.launch(kind)

    def start(self):
        self.markers.append(('before start', upstream_wait_ms(self.machine.home)))
        for kind in self.before_start:
            self.race.launch(kind)
        if not self.race.launcher(super().start, self.claims_serving):
            self._svc(ActiveState='active', MainPID=os.getpid())  # the launcher runs, its server idles
            self._record('start-idle')


def marker_since(test, version):
    """Count `version` as a build whose launcher takes the serving lock (a kit without handoff.py: nothing)."""
    try:
        from golive import handoff
    except ImportError:
        return
    patcher = mock.patch.object(handoff, 'SERVING_LOCK_SINCE', version)
    patcher.start()
    test.addCleanup(patcher.stop)


class CopyRace(copy_t.CopyBase):
    def setUp(self):
        super().setUp()
        self.race = Race(self, self.m)
        self.adapter = RaceAdapter(self.m.root, self.m, self.race)
        marker_since(self, fx.LIVE)  # the fake live build's launcher takes the serving lock

    def assert_no_hand_server(self):
        self.assertEqual(self.race.hands, [], self.race.log)

    def test_a_launch_that_waited_through_the_copy_finds_the_home_owned_until_the_new_launcher_serves(self):
        old = self.m.server.pid
        self.copy()
        self.assert_no_hand_server()
        self.assertEqual(self.race.log, [('fresh', 'waits: update marker'), ('waited', 'waits: home owned'),
                                         ('fresh', 'waits: update marker'), ('waited', 'waits: home owned')])
        self.assert_t3_runs_again(old)
        self.assertFalse(held(self.m.path('home/userdata/server-owner.sqlite')), 'the kit let go of the owner lock')

    def test_the_marker_is_upstream_s_valid_through_the_stop_and_removed_once_t3_is_verified(self):
        self.copy()
        for when, ms in self.adapter.markers:
            self.assertIsNotNone(ms, when)
            self.assertLessEqual(ms, TTL, when)
        self.assertFalse(os.path.exists(self.m.path('home/runtime/desktop-update-in-progress')))
        self.assertFalse(os.path.exists(self.m.path('home/runtime/desktop-update-in-progress.tmp')))
        [verify] = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertEqual(verify['marker'], 'removed')

    def test_an_old_launcher_without_the_serving_lock_still_starts(self):
        marker_since(self, NEW)  # 0.1.9 is older: the kit lets go of the owner lock just before the start
        self.adapter.claims_serving = False
        self.adapter.before_start = ['fresh']  # (a launch in that moment is the backstop's: see CopyBackstop)
        old = self.m.server.pid
        self.copy()
        self.assert_t3_runs_again(old)
        self.assertNotIn('start-idle', self.calls())
        self.assert_no_hand_server()

    def test_a_marker_a_newer_update_wrote_is_left_alone(self):
        newer = json.dumps({'startedAt': int(time.time() * 1000), 'expiresAt': int(time.time() * 1000) + 5000},
                           separators=(',', ':'))

        def upstream_update(*args):
            self.m.write('home/runtime/desktop-update-in-progress', newer)
        self.adapter.on['start'] = upstream_update
        self.copy()
        with open(self.m.path('home/runtime/desktop-update-in-progress')) as f:
            self.assertEqual(f.read(), newer)
        [verify] = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertTrue(verify['marker'].startswith('left'), verify)

    def test_a_copy_killed_in_the_gap_leaves_a_marker_that_runs_out_by_itself(self):
        self.killed("on['stop'] = kill")
        with open(self.m.path('home/runtime/desktop-update-in-progress')) as f:
            m = json.load(f)
        self.assertEqual(sorted(m), ['expiresAt', 'startedAt'])
        self.assertEqual(m['expiresAt'] - m['startedAt'], TTL)  # no renewal after the kill: gone within 60 s
        self.assertLessEqual(m['expiresAt'], time.time() * 1000 + TTL)
        self.assertFalse(held(self.m.path('home/userdata/server-owner.sqlite')))  # a dead kit holds no lock

    def test_a_marker_that_cannot_be_written_refuses_before_the_stop(self):
        os.chmod(self.m.path('home/runtime'), 0o500)
        self.addCleanup(os.chmod, self.m.path('home/runtime'), 0o755)
        with self.assertRaisesRegex(journal.Refused, 'update marker .* cannot be written.*T3 was not stopped'):
            self.copy()
        self.assertNotIn('stop', self.calls())


class CopyBackstop(copy_t.CopyBase):
    """An SSH client older than 0.1.10 honours neither the marker nor the locks: the hand server it starts in the
    second after the stop is named at once, and the kit stops no server it did not start."""

    def setUp(self):
        super().setUp()
        from golive import handoff
        patcher = mock.patch.object(handoff, 'FOREIGN_GRACE', 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.race = Race(self, self.m)
        self.adapter = RaceAdapter(self.m.root, self.m, self.race)
        self.adapter.after_stop, self.adapter.before_start = ['old'], []

    def test_a_hand_server_in_the_gap_fails_the_copy_soon_and_says_what_to_do(self):
        began = time.monotonic()
        with self.assertRaises(Exception) as cm:  # its owner lock first: the stop step cannot take it
            self.copy()
        self.assertLess(time.monotonic() - began, coldcopy.STOP_WAIT + coldcopy.START_WAIT)
        [pid] = self.race.hands
        said = '\n'.join([str(cm.exception), *getattr(cm.exception, '__notes__', ())])
        self.assertIn(f'T3 did not come back: a T3 server the boot service did not start serves the home: pid {pid}',
                      said)
        self.assertIn(f'kill {pid}', said)
        os.kill(pid, 0)  # still running: not the kit's to stop
        self.assertIsNotNone(upstream_wait_ms(self.m.home))  # left to run out: T3 was not verified


class SwitchRace(switch_t.SwitchBase):
    """The 11-19 switch: a launch that waited through the switch's copy finds the home free in the second before the
    fork build's launcher takes it."""

    def setUp(self):
        super().setUp()
        self.race = Race(self, self.m)
        self.markers = []
        stop = self.adapter.stop

        def stop_and_look():
            stop()
            self.markers.append(('after stop', upstream_wait_ms(self.m.home)))
            self.race.launch('fresh')
        self.adapter.stop = stop_and_look

    def fake_t3(self, binary, args, timeout):
        if args[0] != 'update':
            return super().fake_t3(binary, args, timeout)
        self.markers.append(('before update', upstream_wait_ms(self.m.home)))
        self.race.launch('waited')  # `t3 update` restarts the service: the incidents' second
        self.race.launch('fresh')
        mode, self.mode = self.mode, 'never'
        try:
            code, out = super().fake_t3(binary, args, timeout)
        finally:
            self.mode = mode
        if not self.race.launcher(lambda: self.m.spawn_server(fx.TARGET)):
            self.adapter._record('start-idle')
        return code, out

    def test_a_launch_that_waited_through_the_switch_finds_the_home_owned_and_the_switch_verifies(self):
        marker_since(self, fx.TARGET)
        self.rehearsed()
        self.switch()
        self.assertEqual(self.race.hands, [], self.race.log)
        self.assertEqual([k for k, _ in self.race.log], ['fresh', 'waited', 'fresh', 'waited'])
        for when, ms in self.markers:
            self.assertIsNotNone(ms, when)
        self.assertFalse(os.path.exists(self.m.path('home/runtime/desktop-update-in-progress')))
        self.assertEqual(self.last_run()['end']['result'], 'ok')


class W1Gap(back_t.BackBase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        self.switch()

    def test_w1_keeps_the_marker_and_the_owner_lock_from_its_stop_to_its_update(self):
        seen = []

        def look(*args):
            seen.append((upstream_wait_ms(self.m.home), held(self.m.path('home/userdata/server-owner.sqlite'))))
        self.adapter.on['stop'] = look
        restore = coldcopy.restore

        def look_then_restore(*args, **kw):
            look()
            return restore(*args, **kw)
        with mock.patch.object(coldcopy, 'restore', side_effect=look_then_restore):
            self.back('w1', lose_work_since=self.at)
        self.assertIsNotNone(seen[0][0])
        self.assertEqual(seen[1][1], True)  # the kit holds the home from its stop on: no free second
        self.assertFalse(os.path.exists(self.m.path('home/runtime/desktop-update-in-progress')))

    def test_w1_refuses_while_a_server_the_boot_service_did_not_start_serves_the_home(self):
        race = Race(self, self.m)
        self.adapter.stop()
        race.launch('waited')
        before = self.m.snapshot()
        with self.assertRaisesRegex(journal.Refused, f'kill {race.hands[0]}.*nothing was changed'):
            self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.m.snapshot(), before)


class Marker(unittest.TestCase):
    """The marker as upstream writes and reads it (DesktopUpdateMarker.ts; tests/contract.test.py pins the code)."""

    def setUp(self):
        from golive import handoff
        self.h = handoff
        self.m = fx.Machine()
        self.addCleanup(self.m.close)
        os.makedirs(self.m.path('home/runtime'))

    def test_publish_writes_upstream_s_format_through_a_temp_file(self):
        data = self.h.publish(self.m.home)
        m = json.loads(data)
        self.assertEqual(list(m), ['startedAt', 'expiresAt'])
        self.assertEqual(m['expiresAt'] - m['startedAt'], TTL)
        self.assertNotIn(b' ', data)  # JSON.stringify's compact form
        self.assertFalse(os.path.exists(self.h.marker_path(self.m.home) + '.tmp'))
        self.assertAlmostEqual(upstream_wait_ms(self.m.home), TTL, delta=2000)
        self.assertAlmostEqual(self.h.remaining_ms(self.m.home), TTL, delta=2000)

    def test_upstream_s_launch_script_reads_it(self):
        """The launch script's own node reader (tunnel.ts, between its NODE markers), run on the kit's marker."""
        node = NODE
        if node is None:
            self.skipTest('no node')
        # In the kit_tests sparse checkout (.github/workflows/fork-tests.yml): a missing file fails, never skips.
        tunnel = os.path.join(fx.KIT, '..', '..', 'packages', 'ssh', 'src', 'tunnel.ts')
        with open(tunnel, encoding='utf-8') as f:
            text = f.read()
        start = text.index("<<'NODE'\n", text.index('update_remaining() {')) + len("<<'NODE'\n")
        script = text[start:text.index('\nNODE\n', start)].replace('@@T3_UPDATE_TTL_MS@@', str(TTL))
        self.h.publish(self.m.home)
        p = subprocess.run([node, '-', self.h.marker_path(self.m.home)], input=script, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertGreater(int(p.stdout.strip()), 0)  # it waits
        with open(self.h.marker_path(self.m.home), 'w') as f:
            f.write('{"startedAt":1,"expiresAt":2}')
        p = subprocess.run([node, '-', self.h.marker_path(self.m.home)], input=script, capture_output=True, text=True,
                           timeout=30)
        self.assertEqual((p.returncode, p.stdout.strip()), (0, ''), p.stderr)  # an expired one: it does not

    def test_a_gap_renews_the_marker_and_closing_it_leaves_it_to_run_out(self):
        with mock.patch.object(self.h, 'RENEW', 0.05):
            g = self.h.open_gap(self.m.home)
            first = g.written
            time.sleep(0.3)
            self.assertNotEqual(g.written, first)
            self.h.close(self.m.home)
        self.assertTrue(os.path.exists(self.h.marker_path(self.m.home)))
        self.assertIsNotNone(upstream_wait_ms(self.m.home))
        self.assertIsNone(self.h.served(self.m.home))  # closed: nothing open any more

    def test_starting_lets_go_once_the_launcher_serves_or_after_the_wait(self):
        userdata = self.m.path('home/userdata')
        os.makedirs(userdata)
        lock = coldcopy.hold_owner_lock(userdata, 0)
        g = self.h.Gap(self.m.home)
        g.adopt(lock)
        owner = os.path.join(userdata, 'server-owner.sqlite')
        seen = []

        def launcher():
            seen.append(held(owner))
            serving = sqlite3.connect(self.m.path('home/runtime/service-serving.lock'), isolation_level=None)
            serving.execute('BEGIN EXCLUSIVE')
            for _ in range(200):
                if not held(owner):
                    break
                time.sleep(0.01)
            seen.append(held(owner))
            serving.close()
        g.starting(launcher, NEW)
        self.assertEqual(seen, [True, False])  # held up to the serving lock, then handed over
        self.assertTrue(g.handed_to_launcher)
        g.adopt(coldcopy.hold_owner_lock(userdata, 0))
        with mock.patch.object(self.h, 'HANDOVER_WAIT', 0.1):
            began = time.monotonic()
            g.starting(lambda: None, NEW)  # no launcher comes up
        self.assertFalse(g.handed_to_launcher)
        self.assertFalse(held(owner))
        self.assertLess(time.monotonic() - began, 5)

    def test_an_old_build_s_start_gets_the_home_free(self):
        userdata = self.m.path('home/userdata')
        os.makedirs(userdata)
        g = self.h.Gap(self.m.home)
        g.adopt(coldcopy.hold_owner_lock(userdata, 0))
        seen = []
        g.starting(lambda: seen.append(held(os.path.join(userdata, 'server-owner.sqlite'))), fx.LIVE)
        self.assertEqual(seen, [False])
        self.assertTrue(self.h.takes_serving_lock(NEW))
        self.assertTrue(self.h.takes_serving_lock(NEW + '.fork.1'))
        self.assertFalse(self.h.takes_serving_lock('0.1.10-nightly.20261114.4682'))
        self.assertFalse(self.h.takes_serving_lock(fx.LIVE))

    def test_served_removes_only_the_kit_s_own_marker(self):
        self.h.open_gap(self.m.home)
        self.assertEqual(self.h.served(self.m.home), 'removed')
        self.h.open_gap(self.m.home)
        with open(self.h.marker_path(self.m.home), 'w') as f:
            f.write('{"startedAt":1,"expiresAt":99999999999999}')
        self.assertTrue(self.h.served(self.m.home).startswith('left'))
        self.assertTrue(os.path.exists(self.h.marker_path(self.m.home)))


if __name__ == '__main__':
    unittest.main()
