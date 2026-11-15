#!/usr/bin/env python3
"""Tests of fork-fleet-desk.py (the desk link's always-on side) and of fork-fleet-desk-wire.py, its wire format, with
the contract between the wire and fork-fleet-launch.sh and fork-fleet.py. A fake desk agent on pipes, real Unix sockets
in a temp directory; no root, no ssh, no network. Run: python3 <this>. Linux only."""
import importlib.util, inspect, io, json, os, pwd, re, signal, socket, subprocess, sys, tempfile, threading, time, types
import unittest
from unittest import mock

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
DESK = os.path.join(HERE, 'fork-fleet-desk.py')


def load(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


desk, ff = load('fork_fleet_desk', 'fork-fleet-desk.py'), load('fork_fleet', 'fork-fleet.py')
wire = desk.wire
POOL = 'desk'
START_IN = 'ACTIONS_RUNNER_INPUT_JITCONFIG=abc+/=\nFORK_FLEET_RUN=7\nFORK_FLEET_ATTEMPT=1\n'


class Agent:
    """A fake desk agent: says hello, answers pings, and answers each request with `answer(frame)` (None: silence)."""

    def __init__(self, answer, hello=POOL, pong=True):
        self.answer, self.pong, self.got = answer, pong, []
        self.to_serve_r, self.to_serve_w = os.pipe()
        self.from_serve_r, self.from_serve_w = os.pipe()
        os.write(self.to_serve_w, wire.encode({'v': 1, 'hello': hello}))
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        reader = wire.Reader()
        while data := os.read(self.from_serve_r, 65536):
            for frame in reader.feed(data):
                self.got.append(frame)
                if 'ping' in frame:
                    reply = {'v': 1, 'pong': frame['ping']} if self.pong else None
                elif 'id' in frame:
                    reply = self.answer(frame)
                    reply = reply and {**reply, 'id': frame['id']}
                else:
                    continue
                if reply is not None:
                    os.write(self.to_serve_w, wire.encode(reply))

    def hang_up(self):
        os.close(self.to_serve_w)


class Link(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = os.path.join(self.tmp.name, 'run')
        self.sock = os.path.join(self.dir, f'{POOL}.sock')

    def tearDown(self):
        self.tmp.cleanup()

    def serve(self, agent, **kw):
        log = io.StringIO()
        result = {}
        t = threading.Thread(target=lambda: result.update(code=desk.serve(
            POOL, self.dir, agent.to_serve_r, agent.from_serve_w, log=log, **kw)), daemon=True)
        t.start()
        for _ in range(200):
            if os.path.exists(self.sock) or not t.is_alive():
                break
            time.sleep(0.01)
        return t, result, log

    def launch(self, verb, *args, stdin=None, wait=None):
        out, err = io.StringIO(), io.StringIO()
        try:
            code = desk.launch(POOL, verb, list(args), stdin, self.dir, out, err, wait)
        except desk.Refused as e:
            code, err = e.code, io.StringIO(str(e))
        return code, out.getvalue(), err.getvalue()

    def test_verbs_round_trip_with_exit_codes_and_output(self):
        def answer(f):
            if f['verb'] == 'active':
                return wire.answer(0, 'fleet-desk-1\n', '')
            if f['verb'] == 'start':
                return wire.answer(0) if f['stdin'] == START_IN else wire.answer(9)
            return wire.answer(1, '', 'not enough memory\x1b[2J\n')
        agent = Agent(answer)
        t, _, _ = self.serve(agent)
        self.assertEqual(self.launch('active'), (0, 'fleet-desk-1\n', ''))
        self.assertEqual(self.launch('start', 'fleet-desk-5', stdin=START_IN)[0], 0)
        self.assertEqual(self.launch('fits', '4096'), (1, '', 'not enough memory?[2J\n'))   # escapes made harmless
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), '0o660')
        self.assertEqual(oct(os.stat(self.dir).st_mode & 0o777), '0o700')
        agent.hang_up()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertFalse(os.path.exists(self.sock))

    def test_no_session_fails_at_once_and_sends_nothing(self):
        began = time.monotonic()
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)        # no directory at all (after a boot)
        os.mkdir(self.dir, 0o700)
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)        # no socket
        with socket.socket(socket.AF_UNIX) as s:                          # a socket left by a killed session
            s.bind(self.sock)
        code, _, err = self.launch('active')
        self.assertEqual(code, wire.NO_SESSION)
        self.assertIn('no live desk session', err)
        self.assertLess(time.monotonic() - began, 1)

    def test_bad_requests_are_refused_before_sending(self):
        agent = Agent(lambda f: wire.answer(0))
        t, _, _ = self.serve(agent)
        for verb, args, stdin in (('start', ['fleet-desk-5'], START_IN + 'EVIL=1\n'), ('start', ['../x-1'], START_IN),
                                  ('stop', ['a b-1'], None), ('rm', ['-rf'], None), ('active', ['x'], None),
                                  ('fits', ['1e9'], None), ('check-net', ['file:///etc', '1.2.3.4:5'], None)):
            self.assertEqual(self.launch(verb, *args, stdin=stdin)[0], 2, verb)
        self.assertEqual([f for f in agent.got if 'id' in f], [])
        agent.hang_up()
        t.join(5)

    def test_second_session_for_the_pool_is_refused(self):
        a = Agent(lambda f: wire.answer(0))
        t, _, _ = self.serve(a)
        b, got = Agent(lambda f: wire.answer(0)), []

        def second():
            try:
                desk.serve(POOL, self.dir, b.to_serve_r, b.from_serve_w, log=io.StringIO())
            except desk.Refused as e:
                got.append(e.code)
        t2 = threading.Thread(target=second, daemon=True)
        t2.start()
        t2.join(5)
        b.hang_up()
        self.assertEqual(got, [wire.NO_SESSION])
        self.assertEqual([f for f in b.got if 'hello' in f], [])            # refused before the hello
        self.assertEqual(self.launch('active')[0], 0)                      # the first one still serves
        a.hang_up()
        t.join(5)

    def test_wrong_hello_or_bad_frame_ends_the_session(self):
        t, result, log = self.serve(Agent(lambda f: wire.answer(0), hello='other'))
        t.join(5)
        self.assertEqual(result['code'], 1)
        self.assertIn('hello for pool', log.getvalue())
        self.assertFalse(os.path.exists(self.sock))
        agent = Agent(lambda f: {'v': 1, 'exit': 0, 'stdout': '', 'stderr': '', 'extra': 1})
        t, result, log = self.serve(agent)
        self.assertEqual(self.launch('active')[0], wire.UNKNOWN)
        t.join(5)
        self.assertEqual(result['code'], 1)
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)

    def test_desktop_gone_mid_verb_answers_unknown_at_once(self):
        agent = Agent(lambda f: None)
        t, _, _ = self.serve(agent)
        threading.Timer(0.3, agent.hang_up).start()
        began = time.monotonic()
        code, _, err = self.launch('start', 'fleet-desk-5', stdin=START_IN, wait=20)
        self.assertEqual(code, wire.UNKNOWN)
        self.assertIn('the desk session ended', err)
        self.assertLess(time.monotonic() - began, 3)
        t.join(5)
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)

    def test_silent_desktop_ends_the_session(self):
        agent = Agent(lambda f: None, pong=False)
        t, result, log = self.serve(agent, ping=0.2, dead=0.6)
        t.join(5)
        self.assertEqual(result['code'], 1)
        self.assertIn('no word from the desktop', log.getvalue())
        self.assertTrue(any('ping' in f for f in agent.got))
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)

    def test_serve_pings_a_desktop_that_talks_but_hears_nothing(self):
        """The agent drops a link with no frame FROM serve in its DEAD s. Answers spaced under PING s (several verbs in
        flight) used to keep serve quiet, so the agent killed a healthy verb: serve pings whenever it sent nothing."""
        agent = Agent(lambda f: None)
        t, _, _ = self.serve(agent, ping=0.3, dead=5)
        stop = threading.Event()

        def chatter():   # the desktop talks all the time (here: pongs), so serve always hears from it
            while not stop.wait(0.05):
                os.write(agent.to_serve_w, wire.encode({'v': 1, 'pong': 0}))
        threading.Thread(target=chatter, daemon=True).start()
        time.sleep(1.5)
        stop.set()
        pings = [f for f in agent.got if 'ping' in f]
        self.assertGreaterEqual(len(pings), 3, agent.got)   # about every 0.3 s, not never
        agent.hang_up()
        t.join(5)

    def test_a_request_of_the_wrong_type_is_refused_and_the_session_lives(self):
        agent = Agent(lambda f: wire.answer(0, 'ok\n'))
        t, _, _ = self.serve(agent)
        for bad in ({'verb': ['active'], 'args': []}, {'verb': {'a': 1}, 'args': []}, {'verb': 'active', 'args': 'x'}):
            with socket.socket(socket.AF_UNIX) as c:
                c.connect(self.sock)
                c.sendall(json.dumps({'v': 1, **bad}).encode() + b'\n')
                got = wire.check_answer(json.loads(c.makefile('rb').readline()), with_id=False)
            self.assertEqual(got['exit'], 2, bad)
            self.assertIn('not a launcher verb', got['stderr'])
        self.assertEqual(self.launch('active'), (0, 'ok\n', ''))   # serve is still there
        for bad in (['start'], {'a': 1}, None, 7):
            with self.assertRaises(wire.WireError):
                wire.check_request({'v': 1, 'id': 1, 'verb': bad, 'args': []}, with_id=True)
        self.assertEqual([f['verb'] for f in agent.got if 'id' in f], ['active'])   # only the good one went on
        agent.hang_up()
        t.join(5)

    def test_answering_pings_keeps_the_session_and_slow_answer_is_unknown(self):
        agent = Agent(lambda f: None)
        t, _, _ = self.serve(agent, ping=0.2, dead=0.6)
        code, _, _ = self.launch('active', wait=1.5)                        # no answer: unknown, not a hang
        self.assertEqual(code, wire.UNKNOWN)
        self.assertTrue(t.is_alive())                                       # pongs kept it open past `dead`
        agent.hang_up()
        t.join(5)

    def test_socket_directory_must_be_private(self):
        os.mkdir(self.dir, 0o755)
        with self.assertRaises(desk.Refused):
            desk.serve(POOL, self.dir, *os.pipe(), log=io.StringIO())
        self.assertNotEqual(self.launch('active')[0], 0)
        os.rmdir(self.dir)
        os.symlink(self.tmp.name, self.dir)
        with self.assertRaises(desk.Refused):
            desk.safe_dir(self.dir, (os.geteuid(),))

    def test_only_root_uses_the_fixed_directory(self):
        env = {'FORK_FLEET_DESK_DIR': '/tmp/elsewhere'}
        self.assertEqual(desk.sock_dir(env, euid=0), desk.DIR)
        self.assertEqual(desk.sock_dir(env, euid=1000), '/tmp/elsewhere')
        self.assertEqual(desk.sock_dir({}, euid=1000), desk.DIR)

    def test_cli_and_the_dispatcher_through_the_link(self):
        agent = Agent(lambda f: wire.answer(0, 'fleet-desk-1\nfleet-desk-2\nother-3\n') if f['verb'] == 'active'
                      else wire.answer(1, '', 'no\n'))
        t, _, _ = self.serve(agent)
        env = {'PATH': os.environ['PATH'], 'FORK_FLEET_DESK_DIR': self.dir}
        got = subprocess.run([sys.executable, '-I', DESK, 'launch', POOL, 'active'], env=env, capture_output=True,
                             text=True, timeout=30)
        self.assertEqual((got.returncode, got.stdout), (0, 'fleet-desk-1\nfleet-desk-2\nother-3\n'))
        pool = {'name': POOL, 'label': 'fleet-desk', 'launch': [sys.executable, '-I', DESK, 'launch', POOL]}
        host = ff.Host(pool, run=lambda *a, **kw: subprocess.run(*a, **kw, env=env))
        self.assertEqual(host.units(), {'fleet-desk-1', 'fleet-desk-2'})
        self.assertFalse(host.fits(4096))
        with self.assertRaises(ff.HostError) as e:
            host.stop('fleet-desk-1')
        self.assertIn('no', str(e.exception))
        agent.hang_up()
        t.join(5)
        with self.assertRaises(ff.HostError) as e:
            host.units()
        self.assertIn('no live desk session', str(e.exception))

    def test_serve_cli_cleans_up_on_sigterm(self):
        env = {'PATH': os.environ['PATH'], 'FORK_FLEET_DESK_DIR': self.dir}
        p = subprocess.Popen([sys.executable, '-I', DESK, 'serve', POOL], env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        p.stdin.write(wire.encode({'v': 1, 'hello': POOL}))
        p.stdin.flush()
        self.assertEqual(wire.decode(p.stdout.readline().rstrip(b'\n')), {'v': 1, 'hello': POOL})
        for _ in range(200):
            if os.path.exists(self.sock):
                break
            time.sleep(0.01)
        p.send_signal(signal.SIGTERM)
        p.wait(10)
        p.stdin.close(), p.stdout.close(), p.stderr.close()
        self.assertFalse(os.path.exists(self.sock))
        self.assertEqual(self.launch('active')[0], wire.NO_SESSION)

    # -- reviewer tests (PR #192, correctness lens): each fails when its guard is removed
    def test_peer_of_another_uid_is_dropped_without_a_second_user(self):
        # SO_PEERCRED faked: the peer looks like uid 4242, in no group of the socket. No second user, no root needed.
        agent = Agent(lambda f: wire.answer(0))
        t, _, _ = self.serve(agent)
        real = desk.struct
        fake = types.SimpleNamespace(unpack=lambda fmt, b: (real.unpack(fmt, b)[0], 4242, 4242))
        with mock.patch.object(desk, 'struct', fake):
            code = self.launch('active', wait=2)[0]
        self.assertNotEqual(code, 0)
        self.assertEqual([f for f in agent.got if 'id' in f], [])          # nothing reached the agent
        self.assertEqual(self.launch('active')[0], 0)                      # own uid still served
        agent.hang_up(); t.join(5)

    def test_stale_socket_of_a_killed_serve_is_replaced(self):
        os.mkdir(self.dir, 0o700)
        with socket.socket(socket.AF_UNIX) as s:                          # left by a SIGKILLed serve
            s.bind(self.sock)
        agent = Agent(lambda f: wire.answer(0, 'x\n'))
        t, result, log = self.serve(agent)
        time.sleep(0.2)
        self.assertTrue(t.is_alive(), log.getvalue())
        self.assertEqual(self.launch('active')[:2], (0, 'x\n'))
        agent.hang_up(); t.join(5)

    def test_serve_rechecks_a_raw_request(self):
        agent = Agent(lambda f: wire.answer(0))
        t, _, _ = self.serve(agent)
        for raw in (b'{"v":1,"verb":"rm","args":["-rf"]}\n',
                    b'{"v":1,"verb":"start","args":["x-1"],"stdin":"EVIL=1\\n"}\n',
                    b'{"v":1,"verb":"active","args":[]}\n{"v":1,"verb":"prune","args":[]}\n'):
            with socket.socket(socket.AF_UNIX) as s:
                s.connect(self.sock); s.sendall(raw); s.settimeout(3)
                got = wire.decode(s.recv(65536).split(b'\n')[0])
            self.assertEqual(got['exit'], 2, raw)
        self.assertEqual([f for f in agent.got if 'id' in f], [])
        agent.hang_up(); t.join(5)

    def test_serve_own_deadline_answers_unknown(self):
        agent = Agent(lambda f: None)
        t, _, _ = self.serve(agent)
        with mock.patch.object(desk.wire, 'serve_seconds', lambda verb: 0.3):
            code, _, err = self.launch('active', wait=5)
        self.assertEqual(code, wire.UNKNOWN)
        self.assertIn('no answer in time', err)
        agent.hang_up(); t.join(5)

    def test_answer_to_an_id_never_sent_ends_the_session(self):
        agent = Agent(lambda f: wire.answer(0))
        t, result, log = self.serve(agent)
        os.write(agent.to_serve_w, wire.encode({**wire.answer(0), 'id': 99}))
        t.join(5)
        self.assertEqual(result.get('code'), 1)
        self.assertIn('never sent', log.getvalue())

    def test_launch_refuses_a_socket_of_another_owner(self):
        os.mkdir(self.dir, 0o700)
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(self.sock)
        real = os.lstat
        def fake(p, *a, **k):
            st = real(p, *a, **k)
            return types.SimpleNamespace(st_mode=st.st_mode, st_uid=4242) if p == self.sock else st
        with mock.patch.object(desk.os, 'lstat', fake):
            code, _, err = self.launch('active')
        self.assertEqual(code, 1, err)

    def test_silent_client_is_dropped(self):
        agent = Agent(lambda f: wire.answer(0))
        t, _, _ = self.serve(agent)
        with mock.patch.object(desk, 'CLIENT', 0.3):
            s = socket.socket(socket.AF_UNIX); s.connect(self.sock); s.settimeout(5)
            self.assertEqual(s.recv(10), b'')                              # closed after CLIENT s, not held
            s.close()
        agent.hang_up(); t.join(5)

    # -- fix round 1 (PR #192)
    def test_serve_never_runs_as_root(self):
        with mock.patch.object(desk.os, 'geteuid', lambda: 0):
            with self.assertRaises(desk.Refused) as e:
                desk.serve(POOL, self.dir, *os.pipe(), log=io.StringIO())
        self.assertIn('never runs as root', str(e.exception))
        self.assertFalse(os.path.exists(self.dir))                         # refused before it touched anything

    def test_root_launch_takes_the_desk_users_directory(self):
        # The dispatcher (root) reaches a directory owned by the desk-link user ($FORK_FLEET_DESK_USER, default
        # fleet-desk), and no other user's.
        os.mkdir(self.dir, 0o710)
        me = pwd.getpwuid(os.geteuid()).pw_name
        with mock.patch.object(desk.os, 'geteuid', lambda: 0):
            for env, want in (({'FORK_FLEET_DESK_USER': me}, wire.NO_SESSION),
                              ({'FORK_FLEET_DESK_USER': 'no-such-x'}, 1)):
                try:
                    code = desk.launch(POOL, 'active', [], None, self.dir, io.StringIO(), io.StringIO(), env=env)
                except desk.Refused as e:
                    code = e.code
                self.assertEqual(code, want, env)

    def test_socket_is_0660_in_the_directory_group_and_the_group_is_let_in(self):
        mine = [g for g in os.getgroups() if g != os.getegid()]
        gid = mine[0] if mine else os.getegid()                          # another group of this user, when it has one
        os.mkdir(self.dir, 0o710)
        os.chown(self.dir, -1, gid)
        agent = Agent(lambda f: wire.answer(0, 'ok\n'))
        born, chown = [], os.chown                                       # its mode as bound, before chown/chmod
        def seen(p, u, g):
            born.append(os.stat(p).st_mode & 0o777)
            chown(p, u, g)
        with mock.patch.object(desk.os, 'chown', seen):
            t, _, log = self.serve(agent)
            for _ in range(200):
                if born or not t.is_alive():
                    break
                time.sleep(0.01)
        self.assertEqual(born, [0o600], log.getvalue())                  # owner only until the group is set
        st = os.stat(self.sock)
        self.assertEqual((oct(st.st_mode & 0o777), st.st_gid), ('0o660', gid), log.getvalue())
        real = desk.struct                                               # the dispatcher's user: another uid, the group
        fake = types.SimpleNamespace(unpack=lambda fmt, b: (real.unpack(fmt, b)[0], 4242, gid))
        with mock.patch.object(desk, 'struct', fake):
            self.assertEqual(self.launch('active')[:2], (0, 'ok\n'))
        agent.hang_up(); t.join(5)

    @unittest.skipIf(os.geteuid() == 0, 'root opens any file')
    def test_a_lock_or_directory_left_by_an_older_root_serve_is_refused_clearly(self):
        os.mkdir(self.dir, 0o700)
        os.close(os.open(os.path.join(self.dir, f'{POOL}.lock'), os.O_CREAT | os.O_WRONLY, 0o000))
        with self.assertRaises(desk.Refused) as e:
            desk.serve(POOL, self.dir, *os.pipe(), log=io.StringIO())
        self.assertIn('older serve that ran as root', str(e.exception))
        shut = os.path.join(self.tmp.name, 'shut')                       # a /run it cannot write: tmpfiles' job
        os.mkdir(shut, 0o500)
        with self.assertRaises(desk.Refused) as e:
            desk.serve(POOL, os.path.join(shut, 'run'), *os.pipe(), log=io.StringIO())
        self.assertIn('systemd-tmpfiles', str(e.exception))
        os.chmod(shut, 0o700)

    def test_wrong_wire_version_says_which_side_to_upgrade(self):
        for v, side in ((0, 'upgrade the desktop agent'), (2, "upgrade this host's fork-fleet files")):
            r, w = os.pipe()
            out_r, out_w = os.pipe()
            os.write(w, f'{{"v":{v},"hello":"{POOL}"}}\n'.encode())
            log = io.StringIO()
            self.assertEqual(desk.serve(POOL, self.dir, r, out_w, log=log), wire.WRONG_VERSION)
            self.assertIn(side, log.getvalue())
            for fd in (r, w, out_r, out_w):
                os.close(fd)

    def test_a_launchers_own_exit_3_is_not_no_session(self):
        agent = Agent(lambda f: wire.answer(3, '', 'three\n'))
        t, _, _ = self.serve(agent)
        code, _, err = self.launch('stop', 'fleet-desk-5')
        self.assertEqual(code, 1)
        self.assertIn("the desktop's launcher exited 3", err)
        agent.hang_up(); t.join(5)

    def test_launch_takes_its_budget_all_told(self):
        # A stuck serve: connect and send each take nearly CLIENT s, the answer never comes. launch still ends within
        # its budget, not CLIENT + CLIENT + budget (start's worst case was 120 s, the dispatcher's own limit).
        os.mkdir(self.dir, 0o700)
        with socket.socket(socket.AF_UNIX) as s:
            s.bind(self.sock)

        class Stuck:
            def __init__(self, *a):
                self.t = None
            def settimeout(self, t):
                self.t = t
            def connect(self, _):
                time.sleep(max(0, self.t - 0.05))
            def sendall(self, _):
                time.sleep(max(0, self.t - 0.05))
            def recv(self, _):
                time.sleep(self.t)
                raise socket.timeout()
            def __enter__(self):
                return self
            def __exit__(self, *a):
                pass
        began = time.monotonic()
        with mock.patch.object(desk, 'CLIENT', 1), mock.patch.object(desk.socket, 'socket', Stuck):
            code = self.launch('active', wait=1.5)[0]
        self.assertEqual(code, wire.UNKNOWN)
        self.assertLess(time.monotonic() - began, 1.5 + 0.4)

    def test_an_accept_error_costs_one_client_not_the_session(self):
        agent = Agent(lambda f: wire.answer(0, 'ok\n'))
        t, result, log = self.serve(agent)
        real, failed = socket.socket.accept, []

        def accept(sock):
            if not failed:
                failed.append(1)
                raise OSError(24, 'Too many open files')
            return real(sock)
        with mock.patch.object(socket.socket, 'accept', accept):
            first = self.launch('active', wait=3)[0]
        self.assertEqual(failed, [1])
        self.assertEqual(first, 0)                                          # it waited in the backlog, then was taken
        self.assertTrue(t.is_alive(), log.getvalue())                      # the session goes on
        self.assertEqual(self.launch('active')[:2], (0, 'ok\n'))
        self.assertIn('Too many open files', log.getvalue())
        agent.hang_up(); t.join(5)

    def test_odd_names_and_bytes_never_crash_launch(self):
        env = {'PATH': os.environ['PATH'], 'FORK_FLEET_DESK_DIR': self.dir}
        for argv, stdin in (([b'launch', b'\xff\n', b'active'], b''), ([b'launch', b'desk', b'stop', b'\xff-1'], b''),
                            ([b'launch', b'desk', b'\xc3\xa9'], b''),
                            ([b'launch', b'desk', b'start', b'x-1'], b'\xff\n'),
                            ([b'launch', b'de"sk', b'fits', b'1\x00'.replace(b'\x00', b'\x01')], b'')):
            got = subprocess.run([sys.executable, '-I', DESK.encode(), *argv], env=env, input=stdin,
                                 capture_output=True, timeout=30)
            self.assertEqual(got.returncode, 2, (argv, got.stderr))
            self.assertNotIn(b'Traceback', got.stderr)
        strict = [io.TextIOWrapper(io.BytesIO(b), encoding='utf-8', errors='strict') for b in (b'\xff\n', b'', b'')]
        with mock.patch.multiple(desk.sys, stdin=strict[0], stdout=strict[1], stderr=strict[2]):   # a UTF-8 locale
            self.assertEqual(desk.main(['launch', POOL, 'start', 'x-1'], env), 2)
        agent = Agent(lambda f: wire.answer(0, 'café \ud800\n'))          # odd text back: shown, not a crash
        t, _, _ = self.serve(agent)
        got = subprocess.run([sys.executable, '-I', DESK, 'launch', POOL, 'active'], env=env, capture_output=True,
                             timeout=30)
        self.assertEqual((got.returncode, got.stdout.decode()), (0, 'café ?\n'), got.stderr)
        agent.hang_up(); t.join(5)


class Wire(unittest.TestCase):
    """The wire's rules, and its contract with fork-fleet-launch.sh and fork-fleet.py (one source each)."""
    LAUNCH = open(os.path.join(HERE, 'fork-fleet-launch.sh'), encoding='utf-8').read()

    def shell_var(self, name):
        return re.search(rf"^{name}='\^(.*)\$'$", self.LAUNCH, re.M).group(1)

    def test_verbs_are_the_launchers(self):
        case = self.LAUNCH[self.LAUNCH.index('case "${1-}:$#" in'):]
        verbs = dict(re.findall(r'^  ([a-z-]+):([0-9]+|\*)\)', case, re.M))
        # every launcher verb crosses the link or is local only (a local-only verb may name one the launcher has yet
        # to get, as `refresh` before row D7)
        self.assertEqual(set(verbs) - wire.LOCAL_ONLY, set(wire.VERBS))
        self.assertFalse(set(wire.VERBS) & wire.LOCAL_ONLY)
        self.assertIn('check-net', verbs)   # the launcher has it, and the link never carries it (LOCAL_ONLY)
        for verb, n in verbs.items():
            if verb in wire.LOCAL_ONLY:
                continue
            fixed, rest, _ = wire.VERBS[verb]
            self.assertEqual(n == '*', rest is not None, verb)
            if n != '*':
                self.assertEqual(int(n) - 1, len(fixed), verb)

    def test_local_only_verbs_never_cross_the_link(self):
        for verb in wire.LOCAL_ONLY:
            for with_id in (False, True):
                frame = {'verb': verb, 'args': []} | ({'id': 1} if with_id else {})
                with self.assertRaises(wire.WireError, msg=verb):
                    wire.check_request(frame, with_id=with_id)

    def test_patterns_are_the_launchers_and_the_dispatchers(self):
        self.assertEqual(wire.NAME.pattern, self.shell_var('NAME'))
        self.assertEqual(wire.RUNNER_VERSION.pattern, self.shell_var('VERSION'))
        self.assertEqual(wire.POOL.pattern, ff.POOL.pattern)

    def test_every_dispatcher_verb_answers_before_the_dispatcher_gives_up(self):
        # The agent kills a verb at its limit, serve answers 75 after that, launch ends after that, and all of it
        # before Host._do's timeout. The verbs are read from Host itself, so a new one is checked too.
        limit = int(re.search(r'timeout=([0-9]+)', inspect.getsource(ff.Host._do)).group(1))
        src = ''.join(inspect.getsource(f) for f in vars(ff.Host).values() if inspect.isfunction(f))
        verbs = set(re.findall(r"self\._(?:must|do)\('([a-z-]+)'", src))
        self.assertEqual(verbs, {'start', 'ready', 'active', 'fits', 'stop', 'prune'})
        self.assertEqual(verbs, set(wire.VERBS))   # the link carries what the dispatcher calls, nothing more
        for verb in wire.VERBS:
            self.assertLess(wire.limit(verb), wire.serve_seconds(verb), verb)
            self.assertLess(wire.serve_seconds(verb), wire.seconds(verb), verb)
        for verb in verbs:
            self.assertLess(wire.seconds(verb), limit, verb)

    def test_the_dispatchers_start_input_passes(self):
        text = ff.runner_env('abc+/=', {'id': 123, 'run_attempt': 2})
        wire.check_start_input(text)
        for bad in (text + 'X=1\n', text.replace('FORK_FLEET_RUN=123\n', ''), text + text, text.replace('123', '0')):
            with self.assertRaises(wire.WireError):
                wire.check_start_input(bad)

    def test_deep_json_is_a_wire_error(self):
        line = b'[' * 100000 + b']' * 100000                             # under MAX_FRAME, too deep for json
        self.assertLess(len(line), wire.MAX_FRAME)
        with self.assertRaises(wire.WireError):
            wire.decode(line)
        with self.assertRaises(wire.WireError):
            wire.Reader().feed(line + b'\n')

    def test_answer_text_is_clean_of_lone_surrogates(self):
        got = wire.check_answer(json.loads('{"v":1,"exit":0,"stdout":"a\\ud800b","stderr":""}'), with_id=False)
        self.assertEqual(got['stdout'], 'a?b')
        got['stdout'].encode('utf-8')

    def test_text_is_sized_in_bytes(self):
        frame = wire.answer(0, '\u00e9' * wire.MAX_TEXT, 'x' * (wire.MAX_TEXT + 5))   # 6 bytes each on the wire
        self.assertLessEqual(len(wire.encode(frame)), wire.MAX_FRAME)
        for key in ('stdout', 'stderr'):
            self.assertLessEqual(wire.text_size(frame[key]), wire.MAX_TEXT)
            self.assertTrue(frame[key].endswith(wire.CUT))
        self.assertEqual(wire.fit('short \u00e9'), 'short \u00e9')
        over = {'v': 1, 'exit': 0, 'stdout': '\u00e9' * (wire.MAX_TEXT // 6 + 1), 'stderr': ''}   # few characters
        with self.assertRaises(wire.WireError):
            wire.check_answer(over, with_id=False)

    def test_start_input_ends_in_a_newline(self):
        with self.assertRaisesRegex(wire.WireError, 'newline'):
            wire.check_start_input(START_IN[:-1])
        with self.assertRaisesRegex(wire.WireError, 'newline'):
            wire.check_start_input(START_IN[:-1] + 'x')
        wire.check_start_input(START_IN)

    def test_frames(self):
        r = wire.Reader()
        frame = wire.request('start', ['x-1'], START_IN, ident=4)
        self.assertEqual(r.feed(wire.encode(frame)[:5]), [])
        self.assertEqual(r.feed(wire.encode(frame)[5:]), [frame])
        for bad in (b'{"v":2}\n', b'[1]\n', b'nope\n', b'{"v":true}\n', b'\xff\n', b'x' * (wire.MAX_FRAME + 1)):
            with self.assertRaises(wire.WireError, msg=bad[:20]):
                wire.Reader().feed(bad)
        for bad in ({'v': 1, 'id': 1, 'exit': 256, 'stdout': '', 'stderr': ''},
                    {'v': 1, 'id': -1, 'exit': 0, 'stdout': '', 'stderr': ''},
                    {'v': 1, 'id': 1, 'exit': True, 'stdout': '', 'stderr': ''},
                    {'v': 1, 'id': 1, 'exit': 0, 'stdout': 'x' * (wire.MAX_TEXT + 1), 'stderr': ''}):
            with self.assertRaises(wire.WireError):
                wire.check_answer(bad, with_id=True)
        with self.assertRaises(wire.WireError):
            wire.check_request({'v': 1, 'verb': 'active', 'args': [], 'stdin': 'x'}, with_id=False)


class Install(unittest.TestCase):
    """The runbook and the key tool (fork-fleet-desk-key.py, row D4) name this program the same way."""
    DOC = open(os.path.join(HERE, '..', 'fork-fleet.md'), encoding='utf-8').read()

    def test_the_runbook_installs_and_names_the_desk_link(self):
        self.assertIn(f'command="{desk.FORCED} POOL"', self.DOC)
        self.assertEqual(desk.FORCED.split()[2], desk.INSTALLED)
        self.assertIn(f"echo '{desk.TMPFILES}'", self.DOC)
        for f in ('fork-fleet-desk.py', 'fork-fleet-desk-wire.py'):
            self.assertIn(f'scripts/{f}:{f}:644', self.DOC)
        self.assertIn(f'--shell /bin/sh {desk.USER}', self.DOC)            # nologin would close the link

    def test_the_key_tool_writes_this_forced_command(self):
        tool = os.path.join(HERE, 'fork-fleet-desk-key.py')
        if not os.path.exists(tool):
            self.skipTest('fork-fleet-desk-key.py (row D4) is not here yet')
        spec = importlib.util.spec_from_file_location('fork_fleet_desk_key', tool)
        dk = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(dk)                   # its own constants, not its text
        self.assertEqual((dk.SERVE, dk.DESK, dk.USER), (desk.FORCED, desk.INSTALLED, desk.USER))


if __name__ == '__main__':
    unittest.main()
