#!/usr/bin/env python3
"""Tests of fork-fleet-desk-agent.py, the desk link's desktop side: through the real `serve` and `launch` of
fork-fleet-desk.py (row D2) behind a fake ssh, and against a scripted fake serve for a broken or hostile always-on
host. Fake launcher and walls in a temp folder; no root, no ssh connection, no network. Run: python3 <this>. Linux."""
import configparser, glob, importlib.util, io, json, os, resource, signal, subprocess, sys, tempfile, threading, time, unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, 'fork-fleet-desk.py')


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


A, desk = load('fork_fleet_desk_agent', 'fork-fleet-desk-agent.py'), load('fork_fleet_desk', 'fork-fleet-desk.py')
wire = A.wire
POOL = 'desk'
START_IN = 'ACTIONS_RUNNER_INPUT_JITCONFIG=abc+/=\nFORK_FLEET_RUN=7\nFORK_FLEET_ATTEMPT=1\n'
# The fake launcher: does what plan.json says for its verb, and logs each call (argv, stdin, env, its pids).
LAUNCHER = r'''#!/usr/bin/python3 -I
import json, os, signal, subprocess, sys, time
d = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(d, 'plan.json'))).get(sys.argv[1], {})
stdin = sys.stdin.read() if not sys.stdin.isatty() else ''
pids = [os.getpid()]
if plan.get('deaf'):   # deaf to SIGTERM, and so is its child: only SIGKILL ends them
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if plan.get('sleep'):
    pids.append(subprocess.Popen(['sleep', '300']).pid)
with open(os.path.join(d, 'calls.jsonl'), 'a') as f:
    f.write(json.dumps({'argv': sys.argv[1:], 'stdin': stdin, 'env': dict(os.environ), 'pids': pids,
                        'who': os.path.basename(__file__)}) + '\n')
time.sleep(plan.get('sleep', 0))
sys.stdout.write(plan.get('stdout', '')); sys.stderr.write(plan.get('stderr', ''))
sys.exit(plan.get('exit', 0))
'''
# A scripted always-on host: answers the hello, writes the lines of serve-plan.json, then logs what it gets.
FAKE_SERVE = r'''#!/usr/bin/python3 -I
import json, os, sys, time
d = os.path.dirname(os.path.abspath(__file__))
plan = json.load(open(os.path.join(d, 'serve-plan.json')))
sys.stderr.write(plan.get('stderr', '')); sys.stderr.flush()
if 'exit' in plan:
    sys.exit(plan['exit'])
hello = sys.stdin.buffer.readline()
os.write(1, hello)
for line in plan['lines']:
    os.write(1, line.encode() + b'\n')
time.sleep(plan.get('deaf', 0))   # reads nothing meanwhile
with open(os.path.join(d, 'got.jsonl'), 'ab') as f:
    while data := os.read(0, 65536):
        f.write(data); f.flush()
'''


def gone(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] in ('Z', 'X')
    except FileNotFoundError:
        return True


def until(cond, seconds=10):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return cond()


class Desk(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.run_dir = os.path.join(self.tmp, 'serve-run')
        self.plan({})
        self.file('fork-fleet-launch.sh', LAUNCHER, 0o755)   # a kit: the walls and their launcher in one folder
        self.file('fork-fleet-walls.py', LAUNCHER, 0o755)
        self.file('key', 'not a real key\n', 0o600)
        self.file('known_hosts', 'host ssh-ed25519 AAAA\n')
        self.file('fake-serve', FAKE_SERVE, 0o755)
        self.file('ssh', f'#!/bin/sh\nprintf "%s\\n" "$@" > {self.tmp}/ssh-argv\n'
                  f'FORK_FLEET_DESK_DIR={self.run_dir} exec /usr/bin/python3 -I {DESK} serve {POOL}\n', 0o755)
        self.settings()

    def tearDown(self):
        for pid in self.pids():
            if not gone(pid):
                os.kill(pid, signal.SIGKILL)
        subprocess.run(['rm', '-rf', '--', self.tmp], check=True)

    def file(self, name, text, mode=0o644):
        path = os.path.join(self.tmp, name)
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def fresh(self):
        for name, mode in (('key', 0o600), ('known_hosts', 0o644), ('fork-fleet-launch.sh', 0o755),
                           ('fork-fleet-walls.py', 0o755)):
            os.chmod(os.path.join(self.tmp, name), mode)
        return self.settings()

    def plan(self, plan):
        self.file('plan.json', json.dumps(plan))

    def settings(self, extra='', ssh='ssh'):
        t = self.tmp
        return self.file('desk.conf', f'POOL={POOL}\nHOST=fleet-host.example\nKEY={t}/key\nKNOWN_HOSTS={t}/known_hosts\n'
                         f'WALLS={t}/fork-fleet-walls.py\nRUN_DIR={t}/agent-run\nSSH={t}/{ssh}\n{extra}', 0o600)

    def calls(self):
        try:
            with open(os.path.join(self.tmp, 'calls.jsonl')) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def pids(self):
        return [p for c in self.calls() for p in c['pids']]

    def agent(self, ssh='ssh'):
        self.settings(ssh=ssh)
        stopping, log, result = threading.Event(), io.StringIO(), {}
        conf = A.load(os.path.join(self.tmp, 'desk.conf'))
        t = threading.Thread(target=lambda: result.update(code=A.run(conf, log, stopping)), daemon=True)
        t.start()
        self.addCleanup(lambda: (stopping.set(), t.join(15)))
        return t, stopping, log, result

    def linked(self):
        t, stopping, log, result = self.agent()
        self.assertTrue(until(lambda: os.path.exists(os.path.join(self.run_dir, f'{POOL}.sock'))), log.getvalue())
        return t, stopping, log, result

    def launch(self, verb, *args, stdin=None, wait=20):
        out, err = io.StringIO(), io.StringIO()
        try:
            code = desk.launch(POOL, verb, list(args), stdin, self.run_dir, out, err, wait)
        except desk.Refused as e:
            code, err = e.code, io.StringIO(str(e))
        return code, out.getvalue(), err.getvalue()

    # ------------------------------------------------------------ through the real serve

    def test_verbs_round_trip_and_start_runs_the_walls_first_with_a_fixed_environment(self):
        self.plan({'active': {'stdout': 'desk-7\n'}, 'check': {'stdout': 'PASS  os  linux\n'}, 'start': {'exit': 0}})
        with mock.patch.dict(os.environ, {'FORK_FLEET_LAUNCH_CONF': '/x', 'FORK_FLEET_WALLS_ROOT': '/y'}):
            t, stopping, log, result = self.linked()
            self.assertEqual(self.launch('active'), (0, 'desk-7\n', ''))
            self.assertEqual(self.launch('start', 'desk-7', stdin=START_IN)[0], 0)
        calls = self.calls()
        self.assertEqual([c['argv'] for c in calls], [['active'], ['check'], ['start', 'desk-7']])
        self.assertEqual([c['who'] for c in calls],
                         ['fork-fleet-launch.sh', 'fork-fleet-walls.py', 'fork-fleet-launch.sh'])
        self.assertEqual(calls[2]['stdin'], START_IN)
        for c in calls:   # the launcher and the walls read their own settings files: no variable reaches them
            self.assertEqual({k: v for k, v in c['env'].items() if k not in ('PWD', 'SHLVL', '_')}, A.ENV)
        stopping.set()
        t.join(15)
        self.assertEqual(result['code'], 0)
        self.assertFalse(os.path.exists(os.path.join(self.run_dir, f'{POOL}.sock')))   # the session ended with it

    def test_a_failed_walls_check_starts_no_runner(self):
        self.plan({'check': {'exit': 1, 'stdout': 'FAIL  interop  WSL interop: on\n'}})
        self.linked()
        code, _, err = self.launch('start', 'desk-7', stdin=START_IN)
        self.assertEqual(code, 1)
        self.assertIn('FAIL  interop', err)
        self.assertEqual([c['argv'] for c in self.calls()], [['check']])

    def test_a_verb_at_its_limit_is_killed_with_its_children_and_answered_unknown(self):
        self.plan({'stop': {'sleep': 300, 'deaf': True}})
        verbs = {**wire.VERBS, 'stop': (*wire.VERBS['stop'][:2], 5)}
        with mock.patch.dict(wire.VERBS, verbs):
            self.linked()
            began = time.monotonic()
            code, _, err = self.launch('stop', 'desk-7')
        self.assertEqual(code, wire.UNKNOWN, err)
        self.assertIn('killed at its limit', err)
        self.assertLess(time.monotonic() - began, 9)   # the agent's answer at 5 s, not serve's own at 10 s
        self.assertEqual(len(self.pids()), 2)
        self.assertTrue(until(lambda: all(map(gone, self.pids()))), 'the verb or its child outlived its limit')

    def test_the_session_ending_kills_every_running_verb(self):
        self.plan({'stop': {'sleep': 300}})
        t, stopping, log, result = self.linked()
        got = {}
        threading.Thread(target=lambda: got.update(r=self.launch('stop', 'desk-7')), daemon=True).start()
        self.assertTrue(until(lambda: len(self.pids()) == 2))
        serve = int(subprocess.run(['pgrep', '-f', f'{DESK} serve {POOL}'], capture_output=True, text=True,
                                   check=True).stdout.split()[0])
        os.kill(serve, signal.SIGTERM)      # the always-on host goes away mid-verb
        self.assertTrue(until(lambda: all(map(gone, self.pids()))), log.getvalue())
        self.assertTrue(until(lambda: 'r' in got))
        self.assertEqual(got['r'][0], wire.UNKNOWN)
        self.assertTrue(until(lambda: 'session ended: killed stop' in log.getvalue()), log.getvalue())

    def test_answers_are_cut_to_the_wire_in_bytes(self):
        self.plan({'active': {'stdout': 'é' * 100000, 'stderr': '\ud800 x'}})
        self.linked()
        code, out, err = self.launch('active')
        self.assertEqual(code, 0, err)
        self.assertTrue(out.endswith(wire.CUT))
        self.assertLessEqual(wire.text_size(out), wire.MAX_TEXT)

    def test_the_launchers_own_75_and_3_never_look_like_the_links(self):
        self.plan({'prune': {'exit': wire.UNKNOWN}, 'active': {'exit': wire.NO_SESSION}})
        self.linked()
        code, _, err = self.launch('prune')
        self.assertEqual(code, 1)
        self.assertIn('launcher itself exited 75', err)
        self.assertEqual(self.launch('active')[0], 1)   # D2's launch turns a launcher's 3 into 1

    def test_the_launcher_is_the_one_beside_the_walls(self):
        """One kit: the agent runs the launcher in the walls' folder (its real path), the one those walls read. A LAUNCH
        setting elsewhere (an old launcher the walls never read) is refused, `check` and `run` alike (exit 2)."""
        t = self.tmp
        self.assertEqual(A.load(self.settings())['LAUNCH'], os.path.join(os.path.realpath(t), 'fork-fleet-launch.sh'))
        for launch in (f'{t}/fork-fleet-launch.sh', f'{t}/./fork-fleet-launch.sh'):   # the kit's own: still fine
            with self.subTest(launch=launch):
                self.assertEqual(A.main(['--config', self.settings(f'LAUNCH={launch}\n'), 'check']), 0)
        os.mkdir(os.path.join(t, 'old'), 0o755)
        self.file('old/fork-fleet-launch.sh', LAUNCHER, 0o755)
        os.symlink(f'{t}/old/fork-fleet-launch.sh', f'{t}/old-link.sh')
        for launch in (f'{t}/old/fork-fleet-launch.sh', f'{t}/old-link.sh', '/opt/fork-fleet/fork-fleet-launch.sh'):
            with self.subTest(launch=launch):
                conf = self.settings(f'LAUNCH={launch}\n')
                err = io.StringIO()
                with mock.patch('sys.stderr', err):
                    self.assertEqual(A.main(['--config', conf, 'check']), 2)
                    self.assertEqual(A.main(['--config', conf, 'run']), 2)
                self.assertIn('one kit', err.getvalue())
        kit = os.path.join(t, 'kit2')   # WALLS elsewhere: its own launcher runs, not the default one
        os.mkdir(kit, 0o755)
        for name in ('fork-fleet-launch.sh', 'fork-fleet-walls.py'):
            self.file(f'kit2/{name}', LAUNCHER, 0o755)
        with open(self.settings()) as f:
            text = f.read().replace(f'{t}/fork-fleet-walls.py', f'{kit}/fork-fleet-walls.py')
        conf = self.file('desk.conf', text, 0o600)
        self.assertEqual(A.load(conf)['LAUNCH'], os.path.join(os.path.realpath(kit), 'fork-fleet-launch.sh'))

    def test_an_unsafe_launcher_is_not_run(self):
        os.chmod(os.path.join(self.tmp, 'fork-fleet-launch.sh'), 0o777)
        self.linked()
        code, _, err = self.launch('active')
        self.assertEqual(code, 1)
        self.assertIn('only it may write', err)
        self.assertEqual(self.calls(), [])

    def test_at_most_max_verbs_run_at_once(self):
        self.plan({'stop': {'sleep': 300}})
        with mock.patch.object(A, 'MAX_VERBS', 1):
            self.linked()
            threading.Thread(target=lambda: self.launch('stop', 'desk-7'), daemon=True).start()
            self.assertTrue(until(lambda: len(self.pids()) == 2))
            code, _, err = self.launch('prune')
        self.assertEqual(code, 1)
        self.assertIn('verbs run already', err)

    # ------------------------------------------------------------ a broken or hostile always-on host

    def scripted(self, plan):
        self.file('serve-plan.json', json.dumps(plan))
        log, stopping = io.StringIO(), threading.Event()
        session = A.Session(A.load(self.settings(ssh='fake-serve')), log, stopping)
        result = {}
        t = threading.Thread(target=lambda: result.update(r=session.run()), daemon=True)
        t.start()
        t.join(20)
        stopping.set()
        return result.get('r'), log.getvalue()

    def test_a_request_out_of_the_rules_ends_the_session_and_runs_nothing(self):
        for line in ('{"v":1,"id":1,"verb":"rm","args":["-rf","/"]}',
                     '{"v":1,"id":1,"verb":"stop","args":["../x-1"]}',
                     '{"v":1,"id":1,"verb":"start","args":["desk-7"],"stdin":"EVIL=1\\n"}',
                     '{"v":1,"id":1,"verb":"active","args":[],"extra":1}', '[' * 100000 + ']' * 100000):
            (linked, _), log = self.scripted({'lines': [line]})
            self.assertTrue(linked)
            self.assertIn('session ends', log)
        self.assertEqual(self.calls(), [])

    def test_the_link_never_carries_a_verb_the_machine_runs_only_itself(self):
        # check-net connects as root, from the desktop's own network, to each HOST:PORT it is given and names the
        # first it cannot reach: through the link, a map of the desktop's home network for the always-on host.
        for verb, args in (('check-net', ['http://example.com', '192.168.1.1:445', '192.168.1.20:8006']),
                           ('refresh', [])):
            (linked, _), log = self.scripted({'lines': [json.dumps({'v': 1, 'id': 1, 'verb': verb, 'args': args})]})
            self.assertTrue(linked)
            self.assertIn('not a launcher verb', log)
        self.assertEqual(self.calls(), [])
        self.assertLessEqual({'check-net', 'refresh'}, wire.LOCAL_ONLY)

    def test_a_request_of_the_wrong_type_ends_the_session_without_a_crash(self):
        for line in ('{"v":1,"id":1,"verb":["start"],"args":[]}', '{"v":1,"id":1,"verb":{"a":1},"args":[]}',
                     '{"v":1,"id":1,"verb":null,"args":[]}', '{"v":1,"id":1,"verb":"active","args":[["x"]]}'):
            result, log = self.scripted({'lines': [line]})
            self.assertIsNotNone(result, f'Session.run raised for {line}:\n{log}')
            self.assertIn('session ends', log)
        self.assertEqual(self.calls(), [])

    def test_an_always_on_host_that_reads_nothing_ends_the_session(self):
        # pings it never reads the pongs of: ssh's pipe fills, then at most OUT_MAX frames wait in the agent
        pings = [json.dumps({'v': 1, 'ping': n}) for n in range(1, 8001)]
        with mock.patch.object(A, 'OUT_MAX', 4):
            result, log = self.scripted({'lines': pings, 'deaf': 60})
        self.assertIsNotNone(result, log)
        self.assertIn('reads nothing: 4 frames wait', log)

    def test_serves_exit_76_survives_the_race_with_sshs_own_exit(self):
        # real ssh closes its stdout on the channel's end a moment before it exits with the remote exit code
        self.file('slow-ssh', '#!/bin/sh\necho "upgrade the desktop agent" >&2\nexec 1>&-\nsleep 0.3\nexit 76\n',
                  0o755)
        session = A.Session(A.load(self.settings(ssh='slow-ssh')), io.StringIO(), threading.Event())
        self.assertEqual(session.run(), (False, wire.WRONG_VERSION))

    def test_a_wrong_wire_version_says_which_side_to_upgrade(self):
        for got, side in ((2, "this desktop's desk agent"), (0, "the always-on host's fork-fleet files")):
            (_, code), log = self.scripted({'lines': [json.dumps({'v': got, 'ping': 1})]})
            self.assertEqual(code, wire.WRONG_VERSION)
            self.assertIn(f'upgrade {side}', log)
        (linked, code), log = self.scripted({'exit': 76, 'stderr': 'upgrade the desktop agent\n'})   # serve's 76
        self.assertEqual((linked, code), (False, wire.WRONG_VERSION))
        self.assertIn('ssh: upgrade the desktop agent', log)
        self.assertEqual(A.delay(0, False, wire.WRONG_VERSION), A.MAX)

    def test_pings_are_answered_and_a_silent_host_ends_the_session(self):
        with mock.patch.object(A, 'DEAD', 1):
            (linked, _), log = self.scripted({'lines': ['{"v":1,"ping":4}']})
        self.assertTrue(linked)
        self.assertIn('no word from the always-on host', log)
        with open(os.path.join(self.tmp, 'got.jsonl')) as f:
            self.assertEqual([json.loads(x) for x in f.read().split()], [{'v': 1, 'pong': 4}])

    def test_a_desktop_that_slept_drops_the_link_at_once(self):
        real, jumped = A.boottime, []
        def clock():   # BOOTTIME counts the sleep: 100 s pass between two reads once the link is up
            return real() + (100 if jumped else 0)
        with mock.patch.object(A, 'boottime', clock):
            threading.Timer(1.0, lambda: jumped.append(1)).start()
            began = time.monotonic()
            (linked, _), log = self.scripted({'lines': []})
        self.assertTrue(linked)
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn('asleep, or the link is down', log)

    def test_a_wall_clock_jump_alone_is_a_sleep(self):
        # WSL2: Windows' sleep pauses the distro; BOOTTIME may not move, only the wall clock is stepped on resume
        real, jumped = time.time, []
        threading.Timer(1.0, lambda: jumped.append(1)).start()
        with mock.patch.object(A.time, 'time', lambda: real() + (3600 if jumped else 0)):
            began = time.monotonic()
            (linked, _), log = self.scripted({'lines': []})
        self.assertTrue(linked)
        self.assertLess(time.monotonic() - began, 5)
        self.assertIn('asleep, or the link is down', log)

    def test_the_clock_counts_a_sleep_once_and_never_goes_back(self):
        boot, wall = [100.0], [5000.0]
        with mock.patch.object(A, 'boottime', lambda: boot[0]), mock.patch.object(A.time, 'time', lambda: wall[0]):
            clock = A.Clock()
            self.assertEqual(clock(), 100)
            boot[0] += 600; wall[0] += 600       # a Linux sleep: BOOTTIME and the wall clock move together
            self.assertEqual(clock(), 700)
            wall[0] += 3600                       # WSL2: only the wall clock moves
            self.assertEqual(clock(), 4300)
            wall[0] -= 50                         # the wall clock set back: the clock does not go back
            self.assertEqual(clock(), 4300)
            boot[0] += 1; wall[0] += 1 + 5        # a small step (time sync): not a sleep
            self.assertEqual(clock(), 4301)

    def test_a_wall_clock_set_back_and_then_corrected_is_no_sleep(self):
        boot, wall = [100.0], [5000.0]
        with mock.patch.object(A, 'boottime', lambda: boot[0]), mock.patch.object(A.time, 'time', lambda: wall[0]):
            clock = A.Clock()
            self.assertEqual(clock(), 100)
            wall[0] -= 3600; boot[0] += 1        # set back an hour (by hand, or a bad RTC read)
            self.assertEqual(clock(), 101)
            wall[0] += 3600 + 1; boot[0] += 1    # time sync puts it right: no time was lost
            self.assertEqual(clock(), 102)
            wall[0] += 600                       # then a real WSL2 sleep still counts (past the furthest-ahead mark)
            self.assertEqual(clock(), 701)

    def test_a_hostile_host_cannot_grow_the_agents_memory_through_sshs_stderr(self):
        # the host's side (serve, or its sshd) sends one endless stderr line; the agent reads it in bounded pieces
        mb = 256
        self.file('noisy-ssh', '#!/bin/sh\nhead -c %d /dev/zero | tr "\\\\0" x >&2\nsleep 5\n' % (mb << 20), 0o755)
        before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        log = io.StringIO()
        session = A.Session(A.load(self.settings(ssh='noisy-ssh')), log, threading.Event())
        t = threading.Thread(target=session.run, daemon=True)
        t.start()
        t.join(30)
        grew = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024 - before
        self.assertLess(grew, 32, f'the agent grew by {grew} MB for {mb} MB of ssh stderr with no newline')
        self.assertEqual(log.getvalue().count('ssh: x'), 1)   # one log line for the one long line, not one per piece

    def test_time_is_clock_boottime(self):
        # the sleep tests patch boottime(); this one fails if it reads a clock that stops in a sleep
        clocks = {time.CLOCK_BOOTTIME: 5000.0, time.CLOCK_MONOTONIC: 10.0}
        with mock.patch.object(A.time, 'clock_gettime', lambda c: clocks.get(c, -1.0)), \
                mock.patch.object(A.time, 'monotonic', lambda: 10.0):
            self.assertEqual(A.boottime(), 5000.0)
            self.assertEqual(A.slept_for(), 4990.0)

    # ------------------------------------------------------------ the loop, the settings, the unit

    def test_backoff_doubles_with_jitter_to_max_and_a_short_link_is_no_reset(self):
        for tries in range(8):
            d = A.delay(tries, False, 255)
            self.assertTrue(min(A.MAX, A.BASE * 2 ** tries) / 2 <= d <= min(A.MAX, A.BASE * 2 ** tries))
        self.assertTrue(A.FIRST[0] <= A.delay(5, True, 0) <= A.FIRST[1])
        waits, stopping = [], threading.Event()

        class Fake:   # linked each time, but dropped at once: still backs off
            def __init__(self, *a):
                pass
            def run(self):
                return True, 1

        def wait(seconds, stop):
            waits.append(seconds)
            if len(waits) == 4:
                stopping.set()
            return False
        with mock.patch.object(A, 'wait', wait):
            self.assertEqual(A.run(A.load(self.settings()), io.StringIO(), stopping, Fake), 0)
        self.assertGreaterEqual(waits[3], A.BASE * 4)

    def test_wait_returns_early_when_the_desktop_slept(self):
        t = [0.0]
        gap = iter([0, 0, 50])
        stopping = threading.Event()
        with mock.patch.object(stopping, 'wait', lambda s: t.__setitem__(0, t[0] + s)):
            self.assertTrue(A.wait(600, stopping, now=lambda: t[0], gap=lambda: next(gap)))
        self.assertLess(t[0], 600)

    def test_wait_returns_early_when_only_the_wall_clock_jumped(self):
        mono, wall, stopping = [1000.0], [5000.0], threading.Event()

        def tick(seconds):
            mono[0] += seconds
            wall[0] += seconds + (3600 if mono[0] > 1010 else 0)   # resumed: time sync steps the wall clock only
        with mock.patch.object(stopping, 'wait', tick), mock.patch.object(A.time, 'time', lambda: wall[0]):
            self.assertTrue(A.wait(600, stopping, now=lambda: mono[0], gap=lambda: 0.0))
        self.assertLess(mono[0], 1100)

    def test_one_agent_per_desktop(self):
        conf = A.load(self.settings())
        os.makedirs(conf['RUN_DIR'], 0o700)
        fd = os.open(os.path.join(conf['RUN_DIR'], 'agent.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_EX)
        log, stopped = io.StringIO(), threading.Event()
        stopped.set()   # without the lock, run() would stop at once and return 0
        self.assertEqual(A.run(conf, log, stopped), 1)
        self.assertIn('another desk agent', log.getvalue())
        os.close(fd)

    def test_unusable_settings_are_refused(self):
        bad = ['HOST=-oProxyCommand=x', 'PORT=0', 'NOPE=1', 'POOL=Desk', f'KEY={self.tmp}/../x', 'POOL=a\nPOOL=b',
               'SSH=ssh']
        for extra in bad:
            with self.assertRaises(A.Bad, msg=extra):
                A.load(self.settings(extra))
        for name, mode in (('key', 0o640), ('desk.conf', 0o620), ('known_hosts', 0o666)):
            self.fresh()
            os.chmod(os.path.join(self.tmp, name), mode)
            with self.assertRaises(A.Bad, msg=name):
                A.load(os.path.join(self.tmp, 'desk.conf'))
        self.fresh()
        os.symlink(os.path.join(self.tmp, 'desk.conf'), os.path.join(self.tmp, 'link.conf'))
        self.assertEqual(A.main(['--config', os.path.join(self.tmp, 'link.conf'), 'check']), 2)
        self.file('desk.conf', f'HOST=h\nKEY={self.tmp}/key\nKNOWN_HOSTS={self.tmp}/known_hosts\n', 0o600)
        with self.assertRaisesRegex(A.Bad, 'POOL is required'):
            A.load(os.path.join(self.tmp, 'desk.conf'))
        self.fresh()
        with mock.patch('sys.stdout', io.StringIO()) as out:
            self.assertEqual(A.main(['--config', os.path.join(self.tmp, 'desk.conf'), 'check']), 0)
        self.assertIn('settings ok', out.getvalue())

    def test_the_ssh_line_trusts_only_the_pinned_host_key_and_this_ssh_reads_it(self):
        conf = A.load(self.settings())
        argv = A.ssh_argv(conf)
        for opt in ('BatchMode=yes', 'StrictHostKeyChecking=yes', 'GlobalKnownHostsFile=none', 'IdentityAgent=none',
                    f'UserKnownHostsFile={conf["KNOWN_HOSTS"]}', 'ClearAllForwardings=yes', 'HostKeyAlgorithms=ssh-ed25519'):
            self.assertIn(opt, argv)
        self.assertEqual(argv[1:3], ['-F', 'none'])
        self.assertEqual(argv[-4:], ['--', 'fleet-host.example', 'serve', POOL])
        if not os.path.exists('/usr/bin/ssh'):
            self.skipTest('no ssh here')
        p = subprocess.run(['/usr/bin/ssh', '-G', *argv[1:-2]], capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 0, p.stderr)   # every option is one this OpenSSH knows; nothing is dialled
        self.assertIn('stricthostkeychecking true', p.stdout)
        self.assertIn('user fleet-desk', p.stdout)

    def test_the_desk_link_user_is_the_same_on_both_sides(self):
        self.assertEqual(A.DEFAULTS['USER'], desk.USER)
        key_tool = os.path.join(HERE, 'fork-fleet-desk-key.py')   # row D4
        if not os.path.exists(key_tool):
            self.skipTest('the key tool (row D4) is not here yet')
        key = load('fork_fleet_desk_key', 'fork-fleet-desk-key.py')
        self.assertEqual(A.DEFAULTS['USER'], key.USER)
        # the key's forced command is what runs; the agent asks for the same, and the agent's python is serve's
        self.assertEqual(A.ssh_argv(A.load(self.settings()))[-2:], f'{key.SERVE} {POOL}'.split()[-2:])
        self.assertTrue(key.SERVE.startswith(f'{A.PYTHON} -I {os.path.dirname(A.DEFAULTS["WALLS"])}/'))

    def test_the_unit_runs_the_installed_agent_and_ends_its_verbs_with_it(self):
        unit = configparser.ConfigParser(strict=False, interpolation=None)
        unit.optionxform = str
        unit.read(os.path.join(HERE, 'fork-fleet-desk-agent.service'))
        s = unit['Service']
        self.assertEqual(s['ExecStart'], f'{A.PYTHON} -I /opt/fork-fleet/fork-fleet-desk-agent.py run')
        self.assertEqual((s['Restart'], s['KillMode'], s['RestartPreventExitStatus']),
                         ('always', 'control-group', '2'))
        self.assertNotIn('Environment', s)
        self.assertNotIn('User', s)
        self.assertNotIn('EnvironmentFile', s)
        self.assertEqual((s['MemoryMax'], s['MemorySwapMax']), ('512M', '0'))
        self.assertEqual(A.DEFAULTS['WALLS'], '/opt/fork-fleet/fork-fleet-walls.py')


if __name__ == '__main__':
    unittest.main()
