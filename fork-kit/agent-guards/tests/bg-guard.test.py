"""Tests of fork-kit/agent-guards/bin/bg-guard (Stop hook: block a stop while a Bash command the agent started in
the background still runs, at most maxBlocks times per session). The hook runs under a stand-in parent named
`claude` (a copy of /bin/sh) that starts a command looking like a Claude Code Bash tool command in the background,
as agent_guards.check does. Each test has its own HOME and state folder.

  python3 -I -B fork-kit/agent-guards/tests/bg-guard.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = 'bg-guard'
BUSY = "sh -c 'sleep 30; true' shell-snapshots/snapshot-bg-test >/dev/null 2>&1 & B=$!; "


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='bg-guard-test-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.claude = os.path.join(self.tmp, 'claude')
        shutil.copy('/bin/sh', self.claude)
        self.machine = fx.NO_MACHINE
        self.state = os.path.join(self.home, '.local', 'state', 'bg-guard')

    def stop(self, sid='s1', busy=True, parent=True, event=None):
        """One Stop hook run; (code, stdout, stderr)."""
        ev = json.dumps(event if event is not None else {'hook_event_name': 'Stop', 'session_id': sid})
        argv = [sys.executable, '-I', '-B', '-c', fx.RUNNER, fx.LIB, self.machine, str(os.geteuid()),
                os.path.join(fx.BIN, HOOK)]
        env = dict(os.environ, HOME=self.home, BG_EVENT=ev, BG_PY=argv[0], BG_RUNNER=argv[4], BG_LIB=argv[5],
                   BG_MACHINE=argv[6], BG_UID=argv[7], BG_HOOK=argv[8])
        if not parent:      # no `claude` above it, also when an agent runs this test
            return fx.run_hook(HOOK, ev, self.machine, env=env, parent=None)
        # a simple command in a pipeline: the shell forks and execs it, so the hook's parent is `claude`
        script = (BUSY if busy else '') + 'printf "%s" "$BG_EVENT" | "$BG_PY" -I -B -c "$BG_RUNNER" "$BG_LIB" ' \
            '"$BG_MACHINE" "$BG_UID" "$BG_HOOK"; r=$?; ' \
            + ('kill $B 2>/dev/null; ' if busy else '') + 'exit $r'
        return self.under_claude(script, env)

    def under_claude(self, script, env):
        """`script` run by the stand-in `claude`, in a process group of its own, ended whole afterwards."""
        p = subprocess.Popen([self.claude, '-c', script], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env, start_new_session=True)
        try:
            out, err = p.communicate(timeout=60)
        finally:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
        return subprocess.CompletedProcess(p.args, p.returncode, out, err)

    def assert_blocked(self, r):
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual(out['decision'], 'block')
        self.assertIn('still running', out['reason'])
        self.assertIn("sleep 30; true", out['reason'])      # it names the command

    def assert_passed(self, r):
        self.assertEqual((r.returncode, r.stdout), (0, ''), r.stderr)


class Stop(Base):
    def test_a_background_command_blocks_none_passes(self):
        self.assert_blocked(self.stop())
        self.assert_passed(self.stop(busy=False))

    def test_at_most_max_blocks_per_session(self):
        for _ in range(3):
            self.assert_blocked(self.stop())
        self.assert_passed(self.stop())
        self.assert_blocked(self.stop(sid='s2'))          # each session counts its own
        self.machine = fx.machine_file(self.tmp, {'bgGuard': {'maxBlocks': 5}})
        self.assert_blocked(self.stop())                   # a higher limit from the machine file
        self.machine = fx.machine_file(self.tmp, {'bgGuard': {'maxBlocks': 1}})
        self.assert_passed(self.stop(sid='s2'))

    def test_listed_sessions_are_never_blocked(self):
        listed = os.path.join(self.tmp, 'sessions.txt')
        with open(listed, 'w') as f:
            f.write('# orchestrators\ns-orch\n\n')
        self.machine = fx.machine_file(self.tmp, {'bgGuard': {'skipSessionsFiles': [
            listed, os.path.join(self.tmp, 'not-there.txt'), '~/also-not-there.txt']}})
        self.assert_passed(self.stop(sid='s-orch'))
        self.assert_blocked(self.stop(sid='s1'))          # a missing file skips no one
        self.assert_blocked(self.stop(sid='# orchestrators'))

    def test_no_claude_parent_or_no_session_passes(self):
        self.assert_passed(self.stop(parent=False))
        self.assert_passed(self.stop(sid=''))
        self.assert_passed(self.stop(event={'hook_event_name': 'Stop'}))

    def test_a_state_folder_it_cannot_write_blocks_no_stop(self):
        with open(os.path.join(self.tmp, 'a-file'), 'w') as f:
            f.write('x')
        self.machine = fx.machine_file(self.tmp, {'bgGuard': {'stateDir': os.path.join(self.tmp, 'a-file', 'x')}})
        self.assert_passed(self.stop())

    def test_an_odd_session_id_stays_in_the_state_folder(self):
        self.assert_blocked(self.stop(sid='../../escape'))
        self.assertEqual(os.listdir(self.state), ['escape.count'])

    def test_odd_input_passes(self):
        for text in ('', 'not json', '[]'):
            with self.subTest(text):
                r = fx.run_hook(HOOK, text, env=dict(os.environ, HOME=self.home))
                self.assert_passed(r)


class UnderAnAgent(Base):
    """config.test.py failed 4 of 15 when an agent ran the suites with another command of its own in the background:
    the shared fixture ran each hook as a child of the test, so bg-guard walked up to that agent's Claude Code, found
    the other command and blocked. Here the tests themselves run under such an agent (the stand-in `claude` with a
    background command): a hook run as the fixture once ran it blocks; the fixture's runs now never see that agent."""

    CHILD = '''import json, os, subprocess, sys
sys.path.insert(0, sys.argv[1])
import guards_fixture as fx
ev = {"hook_event_name": "Stop", "session_id": "under-an-agent"}
old = subprocess.run([sys.executable, "-I", "-B", "-c", fx.RUNNER, fx.LIB, fx.NO_MACHINE, str(os.geteuid()),
                      os.path.join(fx.BIN, "bg-guard")], input=json.dumps(ev), capture_output=True, text=True,
                     timeout=60)
runs = [old] + [fx.run_hook("bg-guard", ev, parent=p) for p in ("claude", None)]
print(json.dumps([[r.returncode, r.stdout, r.stderr] for r in runs]))
'''

    def test_the_fixtures_hook_runs_never_see_the_agent_that_runs_the_tests(self):
        script = BUSY + '"$BG_PY" -I -B -c "$BG_CHILD" "$BG_TESTS"; r=$?; kill $B 2>/dev/null; exit $r'
        env = dict(os.environ, HOME=self.home, BG_PY=sys.executable, BG_CHILD=self.CHILD,
                   BG_TESTS=os.path.dirname(os.path.abspath(__file__)))
        r = self.under_claude(script, env)
        self.assertEqual(r.returncode, 0, r.stderr)
        old, standin, orphan = (subprocess.CompletedProcess([], *x) for x in json.loads(r.stdout))
        self.assert_blocked(old)              # the agent and its background command are there
        self.assert_passed(standin)
        self.assert_passed(orphan)


class Count(Base):
    """count_block itself: the lock, and a count file that is garbled."""

    def setUp(self):
        super().setUp()
        self.g = fx.load_hook(HOOK)
        self.c = {'skipSessionsFiles': [], 'maxBlocks': 3, 'stateDir': self.state}

    def test_two_at_once_count_two(self):
        results, start = [], threading.Barrier(12)

        def one():
            start.wait()
            results.append(self.g.count_block(self.c, 'same'))
        threads = [threading.Thread(target=one) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(30)
        self.assertEqual(sorted(results), [False] * 9 + [True] * 3)
        with open(os.path.join(self.state, 'same.count')) as f:
            self.assertEqual(f.read(), '3')

    def test_a_garbled_count_starts_again(self):
        os.makedirs(self.state)
        for text in ('garbled', '', '\0\0'):
            with self.subTest(text):
                with open(os.path.join(self.state, 'g.count'), 'w') as f:
                    f.write(text)
                self.assertTrue(self.g.count_block(self.c, 'g'))
                with open(os.path.join(self.state, 'g.count')) as f:
                    self.assertEqual(f.read(), '1')


if __name__ == '__main__':
    unittest.main()
