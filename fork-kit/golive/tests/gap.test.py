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
import importlib.util, json, os, random, shutil, signal, sqlite3, subprocess, sys, threading, time, unittest
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


def home_owned(home, hold=0.0):
    """`__ssh-helper home-owned`: a launcher serves, or a server (or the kit) holds the owner lock. hold: how long
    the probe keeps the serving lock (upstream's: microseconds; longer here, so the kit's poll surely meets it)."""
    return probe(os.path.join(home, 'runtime', 'service-serving.lock'), hold) or \
        held(os.path.join(home, 'userdata', 'server-owner.sqlite'))


def probe(path, hold):
    """held(path), keeping the lock `hold` seconds when it got it (BEGIN EXCLUSIVE, then close: tunnel.ts held())."""
    if not os.path.exists(path):
        return False
    c = sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=0, isolation_level=None)
    try:
        c.execute('BEGIN EXCLUSIVE')
        time.sleep(hold)
        return False
    except sqlite3.OperationalError:
        return True
    finally:
        c.close()


PROBE_HOLD = 0.004  # an SSH probe's hold, made long enough that the kit's 20 ms poll meets one every few polls


class Prober:
    """SSH launches waiting in the launch script's `home_owned` loop (tunnel.ts), probing the serving lock, many
    times faster than upstream's once a second, at random intervals. A launch that finds the home free starts a
    hand server (race.launch)."""

    def __init__(self, race):
        self.race, self.stop, self.probes = race, threading.Event(), 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        home = self.race.m.home
        serving = os.path.join(home, 'runtime', 'service-serving.lock')
        if not os.path.exists(serving):  # a launcher of this home ran before (upstream creates it then)
            sqlite3.connect(serving).close()
        while not self.stop.wait(random.uniform(0.006, 0.03)):
            self.probes += 1
            if not home_owned(home, PROBE_HOLD):
                self.race.launch('old')  # it looked already: it starts its own server now
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()


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
        self.probing = 0  # seconds the start takes before its launcher claims the serving lock, while launches probe

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
        if self.probing:
            with Prober(self.race) as p:
                time.sleep(self.probing)
            self.probes = p.probes
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
        [verify] = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertEqual(verify['handover'], 'launcher')
        self.assertNotIn('renewFailed', verify)

    def test_ssh_launches_probing_the_serving_lock_during_the_start_never_free_the_home(self):
        """Review finding (handover phantom): the kit's poll meeting an SSH launch's own probe of the serving lock
        is not the launcher; before the fix the kit let go at the first one, and the next probe started a server."""
        self.adapter.after_stop, self.adapter.before_start, self.adapter.probing = [], [], 1.5
        old = self.m.server.pid
        self.copy()
        self.assertGreater(self.adapter.probes, 40)
        self.assert_no_hand_server()
        self.assert_t3_runs_again(old)
        [verify] = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertEqual(verify['handover'], 'launcher')

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
        self.assertIn(f'T3 did not come back: a T3 server the boot service did not start holds the home: pid {pid}',
                      said)
        self.assertRegex(said, f'first quit or disconnect the T3 desktop app .*, then `kill {pid}`, then '
                               '`systemctl --user restart t3code.service`, then `t3-golive status`')
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

    probing = 0  # seconds `t3 update` installs before it restarts T3, while waiting launches probe

    def fake_t3(self, binary, args, timeout):
        if args[0] != 'update':
            return super().fake_t3(binary, args, timeout)
        self.markers.append(('before update', upstream_wait_ms(self.m.home)))
        self.race.launch('waited')  # `t3 update` restarts the service: the incidents' second
        self.race.launch('fresh')
        if self.probing:
            with Prober(self.race):
                time.sleep(self.probing)
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

    def test_ssh_launches_probing_through_the_update_never_free_the_home(self):
        marker_since(self, fx.TARGET)
        self.probing = 1.5
        self.rehearsed()
        self.switch()
        self.assertEqual(self.race.hands, [], self.race.log)
        [verify] = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'verify' and s['phase'] == 'done']
        self.assertEqual(verify['handover'], 'launcher')


class SwitchBackstop(switch_t.SwitchBase):
    """An SSH client older than 0.1.10 starts a hand server while `t3 update` restarts T3 (a target that takes no
    serving lock: the kit let go before the start). Verify stops on it, and W1 refuses to restore under it: the
    switch says so, and what to do."""

    def setUp(self):
        super().setUp()
        from golive import handoff
        patcher = mock.patch.object(handoff, 'FOREIGN_GRACE', 0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.race = Race(self, self.m)

    def fake_t3(self, binary, args, timeout):
        if args[0] != 'update':
            return super().fake_t3(binary, args, timeout)
        mode, self.mode = self.mode, 'never'
        try:
            code, out = super().fake_t3(binary, args, timeout)
        finally:
            self.mode = mode
        self.race.launch('old')
        return code, out

    def test_verify_stops_on_it_w1_refuses_and_the_switch_says_where_t3_stands(self):
        self.rehearsed()
        began = time.monotonic()
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertLess(time.monotonic() - began, 60)
        [pid] = self.race.hands
        said = str(cm.exception)
        self.assertIn(f'verify failed: a T3 server the boot service did not start holds the home: pid {pid}', said)
        self.assertIn('the way back W1 was refused too', said)
        self.assertRegex(said, f'first quit or disconnect the T3 desktop app .*, then `kill {pid}`, then '
                               '`t3-golive back --way w1 --lose-work-since ')
        self.assertNotIn('systemctl --user restart', said)
        os.kill(pid, 0)  # still running: not the kit's to stop
        run = self.last_run()
        self.assertEqual(run['end']['result'], 'failed')
        self.assertNotIn('stop-service', [s['step'] for s in run['steps']])  # W1 changed nothing
        self.assertEqual(self.live_version(), fx.TARGET)
        # the remedy works: the server gone, W1 by hand goes back
        os.kill(pid, signal.SIGKILL)
        for _ in range(500):
            if live._proc(pid) is None:
                break
            time.sleep(0.01)
        at = coldcopy.read_manifest(self.started_data('stopping', run)['copy'])['createdAt']
        got = back.run_back(config.load(), self.adapter, 'w1', lose_work_since=at, say=self.said.append)
        self.assertEqual(got['version'], fx.LIVE)

    def live_version(self):
        with open(self.m.path('home/runtime/service-state.json')) as f:
            return json.load(f)['activeVersion']


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

    def test_a_w1_rerun_behind_its_own_stop_never_says_to_start_t3(self):
        """Review finding (refusal remedy): a W1 that stopped and disabled T3 and failed half-way holds T3 stopped
        on purpose; a hand server that took the home since must not lead the owner to start the service."""
        with mock.patch.object(coldcopy, 'restore', side_effect=RuntimeError('disk gone')):
            with self.assertRaisesRegex(RuntimeError, 'disk gone'):
                self.back('w1', lose_work_since=self.at)
        self.assertFalse(self.adapter.service()['enabled'])
        race = Race(self, self.m)  # the kit exited: its marker runs out, its lock is gone (#530)
        race.launch('waited')
        calls = len(self.calls())
        with self.assertRaises(journal.Refused) as cm:
            self.back('w1', lose_work_since=self.at)
        said = str(cm.exception)
        self.assertRegex(said, f'first quit or disconnect the T3 desktop app .*, then `kill {race.hands[0]}`, then '
                               f'run `t3-golive back --way w1 --lose-work-since {self.at}` again; do not start '
                               't3code.service')
        self.assertNotIn('systemctl --user restart', said)
        self.assertEqual(self.calls()[calls:], [])  # nothing started or stopped
        self.assertEqual(self.last_run()['end']['result'], 'failed')  # it follows a stopped back: still unsettled

    def test_a_w1_whose_marker_cannot_be_written_refuses_before_saying_it_stops_t3(self):
        os.chmod(self.m.path('home/runtime'), 0o500)
        self.addCleanup(os.chmod, self.m.path('home/runtime'), 0o755)
        calls = len(self.calls())
        with self.assertRaisesRegex(journal.Refused, 'update marker .* cannot be written.*T3 was not stopped'):
            self.back('w1', lose_work_since=self.at)
        self.assertNotIn('stop', self.calls()[calls:])
        self.assertFalse(any('stopping T3' in s for s in self.said), self.said)
        self.assertTrue(self.adapter.service()['active'])


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
        self.assertEqual(self.h.served(self.m.home), {'marker': None})  # closed: nothing open any more

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
        self.assertEqual(g.handover, 'launcher')
        g.adopt(coldcopy.hold_owner_lock(userdata, 0))
        with mock.patch.object(self.h, 'HANDOVER_WAIT', 0.1):
            began = time.monotonic()
            g.starting(lambda: None, NEW)  # no launcher comes up
        self.assertEqual(g.handover, 'timeout')
        self.assertFalse(held(owner))
        self.assertLess(time.monotonic() - began, 5)

    def test_a_probe_loop_on_the_serving_lock_is_not_the_launcher(self):
        """Review finding (handover phantom), at the unit: SSH probes flicker the serving lock all through a long
        start; the owner lock stays held until a real launcher takes the serving lock and keeps it."""
        userdata, runtime = self.m.path('home/userdata'), self.m.path('home/runtime')
        os.makedirs(userdata)
        serving = os.path.join(runtime, 'service-serving.lock')
        sqlite3.connect(serving).close()
        owner = os.path.join(userdata, 'server-owner.sqlite')
        g = self.h.Gap(self.m.home)
        g.adopt(coldcopy.hold_owner_lock(userdata, 0))
        stop, probes, free = threading.Event(), [], []

        def ssh_probes():
            while not stop.wait(random.uniform(0.006, 0.03)):
                probes.append(probe(serving, PROBE_HOLD))

        def start():  # `t3 update`: it installs for a while, then its launcher claims the serving lock
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                if not held(owner):
                    free.append(time.monotonic())
                    break
                time.sleep(0.005)
            stop.set()
            t.join()
            launcher = sqlite3.connect(serving, isolation_level=None, timeout=1)
            launcher.execute('BEGIN EXCLUSIVE')
            claimed = time.monotonic()
            for _ in range(300):
                if not held(owner):
                    break
                time.sleep(0.005)
            free.append(time.monotonic() - claimed)
            launcher.close()
        t = threading.Thread(target=ssh_probes, daemon=True)
        t.start()
        g.starting(start, NEW)
        self.assertEqual(len(free), 1, 'the kit let go of the owner lock while only SSH probes held the serving lock')
        self.assertGreater(len(probes), 40)
        # handed over only after the launcher kept it over several polls (a probe the kit met in the poll just
        # before the claim may count towards HANDOVER_HOLD: the launcher holds it at the end all the same)
        self.assertGreaterEqual(free[0], self.h.HANDOVER_POLL * (self.h.HANDOVER_HOLD_POLLS - 1))
        self.assertLess(free[0], 0.25)  # within its server's 250 ms wait for the owner lock
        self.assertEqual(g.handover, 'launcher')

    def test_a_failed_renewal_is_journalled(self):
        with mock.patch.object(self.h, 'RENEW', 0.05):
            self.h.open_gap(self.m.home)
            os.chmod(self.m.path('home/runtime'), 0o500)
            self.addCleanup(os.chmod, self.m.path('home/runtime'), 0o755)
            time.sleep(0.3)
            os.chmod(self.m.path('home/runtime'), 0o755)
            facts = self.h.served(self.m.home)
        self.assertRegex(facts['renewFailed'], r'^\d+ time\(s\), last: .*Permission denied')

    def test_an_old_build_s_start_gets_the_home_free(self):
        userdata = self.m.path('home/userdata')
        os.makedirs(userdata)
        g = self.h.Gap(self.m.home)
        g.adopt(coldcopy.hold_owner_lock(userdata, 0))
        seen = []
        g.starting(lambda: seen.append(held(os.path.join(userdata, 'server-owner.sqlite'))), fx.LIVE)
        self.assertEqual(seen, [False])
        self.assertTrue(g.handover.startswith('before the start'))
        self.assertTrue(self.h.takes_serving_lock(NEW))
        self.assertTrue(self.h.takes_serving_lock(NEW + '.fork.1'))
        self.assertFalse(self.h.takes_serving_lock('0.1.10-nightly.20261114.4682'))
        self.assertFalse(self.h.takes_serving_lock(fx.LIVE))

    def test_served_removes_only_the_kit_s_own_marker(self):
        self.h.open_gap(self.m.home)
        self.assertEqual(self.h.served(self.m.home), {'marker': 'removed'})
        self.h.open_gap(self.m.home)
        with open(self.h.marker_path(self.m.home), 'w') as f:
            f.write('{"startedAt":1,"expiresAt":99999999999999}')
        self.assertTrue(self.h.served(self.m.home)['marker'].startswith('left'))
        self.assertTrue(os.path.exists(self.h.marker_path(self.m.home)))


class Foreign(unittest.TestCase):
    """The backstop names a server only while that very process holds the home's owner lock."""

    def setUp(self):
        from golive import handoff
        self.h = handoff
        self.m = fx.Machine()
        self.addCleanup(self.m.close)
        os.makedirs(self.m.path('home/userdata'))
        os.makedirs(self.m.path('home/runtime'))
        self.race = Race(self, self.m)
        self.svc = {'active': True, 'enabled': True, 'mainPid': 0}  # no process is the service's

    def record(self, pid):
        self.m.write('home/userdata/server-runtime.json', json.dumps({'version': 1, 'pid': pid}))

    def test_a_stale_record_naming_a_reused_pid_is_not_foreign(self):
        """A server killed with SIGKILL leaves its record; its pid now runs something else, while the new server
        (not yet recorded) holds the owner lock. Before the fix: "kill <that unrelated pid>"."""
        unrelated = subprocess.Popen([sys.executable, '-I', '-c', 'import time; time.sleep(60)'])
        self.addCleanup(unrelated.wait)
        self.addCleanup(unrelated.kill)
        self.record(unrelated.pid)
        self.assertIsNone(self.h.foreign(self.m.home, self.svc))  # nobody holds the home
        owner = self.m.path('home/userdata/server-owner.sqlite')
        holder = subprocess.Popen([sys.executable, '-I', '-c', HOLDER, owner], stdout=subprocess.PIPE)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), b'held')
        self.assertIsNone(self.h.foreign(self.m.home, self.svc))
        watch = self.h.ForeignWatch(self.m.home)
        with mock.patch.object(self.h, 'FOREIGN_GRACE', 0):
            self.assertIsNone(watch.check(self.svc))

    def test_a_hand_server_holding_the_home_is_foreign_and_the_stopped_server_is_not(self):
        self.race.launch('old')
        [pid] = self.race.hands
        found = self.h.foreign(self.m.home, self.svc)
        self.assertEqual(found['pid'], pid)
        self.assertIsNone(self.h.foreign(self.m.home, self.svc, old_pid=pid))
        self.assertIsNone(self.h.foreign(self.m.home, {**self.svc, 'mainPid': found['ppid']}))  # the service's own
        self.assertTrue(self.h.holds_lock(pid, self.m.path('home/userdata/server-owner.sqlite')))
        self.assertFalse(self.h.holds_lock(os.getpid(), self.m.path('home/userdata/server-owner.sqlite')))

    def test_the_remedy_follows_where_t3_stands(self):
        found = {'pid': 4242, 'ppid': 1, 'what': 't3 serve'}
        up = self.h.said(found, self.svc)
        self.assertIn('then `kill 4242`, then `systemctl --user restart t3code.service`, then `t3-golive status`', up)
        held_back = self.h.said(found, self.svc, '`t3-golive back --way w2`', stopped=True)
        disabled = self.h.said(found, {**self.svc, 'enabled': False}, '`t3-golive back --way w2`')
        for text in (held_back, disabled):
            self.assertIn('then `kill 4242`, then run `t3-golive back --way w2` again; do not start t3code.service',
                          text)
            self.assertNotIn('systemctl --user restart', text)
        for text in (up, held_back, disabled):
            self.assertLess(text.index('quit or disconnect the T3 desktop app'), text.index('kill 4242'))


HOLDER = '''import sqlite3, sys, time
c = sqlite3.connect(sys.argv[1], isolation_level=None, timeout=5)
c.execute('BEGIN EXCLUSIVE')
print('held', flush=True)
time.sleep(60)
'''


if __name__ == '__main__':
    unittest.main()
