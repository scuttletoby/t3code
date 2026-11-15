#!/usr/bin/env python3
"""Tests of fork-fleet-desk.py (the desk link's always-on side) and of fork-fleet-desk-wire.py, its wire format, with
the contract between the wire and fork-fleet-launch.sh and fork-fleet.py. A fake desk agent on pipes, real Unix sockets
in a temp directory; no root, no ssh, no network. Run: python3 <this>. Linux only."""
import importlib.util, inspect, io, os, re, signal, subprocess, sys, tempfile, threading, time, unittest

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
        self.assertEqual(oct(os.stat(self.sock).st_mode & 0o777), '0o600')
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
        import socket
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


class Wire(unittest.TestCase):
    """The wire's rules, and its contract with fork-fleet-launch.sh and fork-fleet.py (one source each)."""
    LAUNCH = open(os.path.join(HERE, 'fork-fleet-launch.sh'), encoding='utf-8').read()

    def shell_var(self, name):
        return re.search(rf"^{name}='\^(.*)\$'$", self.LAUNCH, re.M).group(1)

    def test_verbs_are_the_launchers(self):
        case = self.LAUNCH[self.LAUNCH.index('case "${1-}:$#" in'):]
        verbs = dict(re.findall(r'^  ([a-z-]+):([0-9]+|\*)\)', case, re.M))
        self.assertEqual(set(verbs), set(wire.VERBS))
        for verb, n in verbs.items():
            fixed, rest, _ = wire.VERBS[verb]
            self.assertEqual(n == '*', rest is not None, verb)
            if n != '*':
                self.assertEqual(int(n) - 1, len(fixed), verb)

    def test_patterns_are_the_launchers_and_the_dispatchers(self):
        self.assertEqual(wire.NAME.pattern, self.shell_var('NAME'))
        self.assertEqual(wire.RUNNER_VERSION.pattern, self.shell_var('VERSION'))
        self.assertEqual(wire.POOL.pattern, ff.POOL.pattern)
        self.assertIn(f'$url =~ ^{wire.URL.pattern}$ ]]', self.LAUNCH)

    def test_every_dispatcher_verb_answers_before_the_dispatcher_gives_up(self):
        limit = int(re.search(r'timeout=([0-9]+)', inspect.getsource(ff.Host._do)).group(1))
        for verb in ('start', 'ready', 'active', 'fits', 'stop', 'prune'):
            self.assertLess(wire.seconds(verb), limit, verb)

    def test_the_dispatchers_start_input_passes(self):
        text = ff.runner_env('abc+/=', {'id': 123, 'run_attempt': 2})
        wire.check_start_input(text)
        for bad in (text + 'X=1\n', text.replace('FORK_FLEET_RUN=123\n', ''), text + text, text.replace('123', '0')):
            with self.assertRaises(wire.WireError):
                wire.check_start_input(bad)

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


if __name__ == '__main__':
    unittest.main()
