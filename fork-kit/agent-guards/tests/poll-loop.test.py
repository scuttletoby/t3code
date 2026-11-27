#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/poll-loop-guard (the Claude Code PreToolUse hook that refuses a Bash command
that waits on CI, a pull request or a job by sleeping and checking again; #1150). The guard is loaded with no
machine file (its defaults) and, in Configured, with one. Its settings entry is tested in wiring.test.py.

  python3 -I -B fork-kit/agent-guards/tests/poll-loop.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = os.path.join(fx.BIN, 'poll-loop-guard')
G = fx.load_hook('poll-loop-guard')
DEFAULTS = dict(G.DEFAULTS)

REFUSED = [
    # the loops the study found
    'for i in $(seq 1 40); do grep -q DONE "$F" && break; sleep 30; done',
    'while ! gh pr checks 12; do sleep 60; done',
    'sleep 300; gh run view 99',
    'sleep 300 && ls out/',
    # CI waits with no sleep
    'gh run watch 99',
    'gh pr checks 12 --watch',
    'gh run watch 99 --exit-status && echo ok',
    # endless loops on a file, a process or a status URL
    'until test -f /work/done; do sleep 10; done',
    'while [ ! -e out.txt ]; do sleep 5; done; cat out.txt',
    'while kill -0 $PID; do sleep 15; done',
    'while pgrep -f build >/dev/null; do sleep 20; done',
    'until curl -sf https://api.github.com/repos/o/r/actions/runs/1 | grep -q completed; do sleep 2; done',
    'while true; do gh run view 1 --json status | grep -q completed && break; sleep 1; done',
    # counted rounds that add up
    'for i in {1..20}; do tail -1 log | grep -q ok && break; sleep 10; done',
    'for i in `seq 30`; do kill -0 $P || break; sleep 5; done',
    'for x in a b c d e; do sleep 30; ls /work/$x; done',
    # handed on to another shell
    "bash -c 'while ! gh pr checks 3; do sleep 30; done'",
    'eval "sleep 200; gh pr view 3"',
    'timeout 600 bash -c "until test -f x; do sleep 10; done"',
    'cd /work && (sleep 400; cat result.txt)',
]
PASSED = [
    # ordinary short sleeps inside a real script run
    'sleep 1',
    'sleep 30',
    'sleep 2 && curl -s http://127.0.0.1:8080/health',
    'for i in $(seq 1 60); do curl -sf http://127.0.0.1:3000/ && break; sleep 1; done',
    'until curl -sf localhost:5173; do sleep 1; done',
    'for i in 1 2 3; do ls; sleep 2; done',
    'npm test; sleep 5; cat report.txt',
    # one check, no wait
    'sleep 60 && gh pr checks 3',
    'gh pr view 3 --json state',
    'gh run view 99 --log-failed | tail -40',
    'ls -la; cat a.txt; grep -n x b.txt',
    # a long sleep with no check after it is not a poll
    'sleep 200',
    'while true; do ./serve; sleep 10; done',
    # text that only names these
    'echo "while ! gh pr checks 1; do sleep 60; done"',
    "git commit -m 'drop the sleep 300; gh run view loop'",
    "cat > notes.md <<'X'\nwhile ! gh pr checks 1; do sleep 60; done\nX",
    '# while ! gh pr checks 1; do sleep 60; done\nls',
    # a capacity wait is not a CI or file wait
    'until t3-capacity 3072 >/dev/null; do sleep 60; done',
    '',
]


def bash(command, **more):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command, **more},
            'session_id': 's'}


class Reads(unittest.TestCase):
    def test_waits_on_ci_files_and_jobs_are_refused(self):
        for cmd in REFUSED:
            with self.subTest(cmd):
                self.assertIsNotNone(G.verdict(cmd, DEFAULTS))

    def test_short_sleeps_single_checks_and_mentions_pass(self):
        for cmd in PASSED:
            with self.subTest(cmd):
                self.assertIsNone(G.verdict(cmd, DEFAULTS))

    def test_the_limits_come_from_the_settings(self):
        loose = dict(DEFAULTS, maxSleepSeconds=1000, loopSleepSeconds=60)
        self.assertIsNone(G.verdict('sleep 300; gh run view 1', loose))
        self.assertIsNone(G.verdict('until test -f x; do sleep 10; done', loose))
        self.assertIsNotNone(G.verdict('until test -f x; do sleep 60; done', loose))
        tight = dict(DEFAULTS, maxSleepSeconds=10)
        self.assertIsNotNone(G.verdict('sleep 30 && ls out/', tight))

    def test_broken_or_deep_text_never_crashes(self):
        for cmd in ('while', 'do done; ;; ((', 'for i in; do', 'sleep', 'sleep x; gh run view',
                    '$(' * 200 + 'sleep 1', 'bash -c ' * 30 + 'ls', 'x' * 200000):
            with self.subTest(cmd[:30]):
                G.verdict(cmd, DEFAULTS)


class Hook(unittest.TestCase):
    def hook(self, event, raw=None):
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event) if raw is None else raw,
                              capture_output=True, text=True, timeout=60)

    def test_refused_is_exit_2_with_the_way_to_wait_on_stderr(self):
        r = self.hook(bash('for i in $(seq 1 40); do grep -q DONE "$F" && break; sleep 30; done'))
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertTrue(r.stderr.startswith('Refused by poll-loop-guard: '), r.stderr)
        for said in ('run_in_background', 't3_background_wait', 'Monitor', 'push and stop', 'watch_pull_request'):
            self.assertIn(said, r.stderr)

    def test_passed_is_exit_0_and_silent(self):
        for event in (bash('sleep 1'), bash('ls'), bash('while ! gh pr checks 1; do sleep 60; done',
                                                         run_in_background=True),
                      {'tool_name': 'Monitor', 'tool_input': {'command': 'until test -f x; do sleep 10; done'}},
                      {'tool_name': 'Read', 'tool_input': {}}, {}, {'tool_name': 'Bash'},
                      {'tool_name': 'Bash', 'tool_input': {'command': None}}, bash("echo 'open quote")):
            with self.subTest(event):
                r = self.hook(event)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_its_own_failure_lets_the_command_run_and_says_so(self):
        for raw in ('not json', '', '{"tool_name": "Bash", "tool_input": "sleep 300; gh run view"}'):
            with self.subTest(raw):
                r = self.hook(None, raw=raw)
                self.assertEqual((r.returncode, r.stdout), (1, ''))
                self.assertIn('poll-loop-guard: failed (', r.stderr)
                self.assertIn('NOT checked and runs', r.stderr)

    def test_a_bug_in_the_reader_fails_open(self):
        event = json.dumps(bash('sleep 300; gh run view 1'))
        for boom in (RuntimeError('bug'), RecursionError('deep'), MemoryError()):
            with self.subTest(type(boom).__name__):
                err = io.StringIO()
                with mock.patch.object(G, 'verdict', side_effect=boom), \
                        mock.patch.object(sys, 'stdin', io.StringIO(event)), mock.patch.object(sys, 'stderr', err):
                    self.assertEqual(G.main([]), 1)

    def test_a_half_written_copy_never_refuses(self):
        tmp = tempfile.mkdtemp(prefix='poll-loop-')
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(HOOK) as f:
            whole = f.read()
        path = os.path.join(tmp, 'poll-loop-guard')
        event = json.dumps(bash('sleep 300; gh run view 1'))
        for cut in [len(whole) * k // 10 for k in range(10)] + [len(whole) - 2]:
            with self.subTest(cut):
                with open(path, 'w') as f:
                    f.write(whole[:cut])
                r = subprocess.run([sys.executable, '-I', path], input=event, capture_output=True, text=True,
                                   timeout=60)
                self.assertIn(r.returncode, (0, 1), r.stderr)

    def test_it_is_fast(self):
        """Under 50 ms a call on average once loaded (the start of Python itself is not counted)."""
        cmds = REFUSED + PASSED
        start = time.perf_counter()
        for _ in range(5):
            for cmd in cmds:
                G.verdict(cmd, DEFAULTS)
        self.assertLess((time.perf_counter() - start) / (5 * len(cmds)), 0.05)


class Configured(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='poll-loop-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_hook(self, cmd, section=None, text=None):
        return fx.run_hook('poll-loop-guard', bash(cmd), fx.machine_file(self.tmp, section, text))

    def test_the_off_switch_lets_everything_through(self):
        r = self.run_hook('sleep 300; gh run view 1', {'pollLoop': {'enabled': False}})
        self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_its_limits_are_read(self):
        section = {'pollLoop': {'maxSleepSeconds': 1000, 'loopSleepSeconds': 60}}
        self.assertEqual(self.run_hook('sleep 300; gh run view 1', section).returncode, 0)
        self.assertEqual(self.run_hook('sleep 1200; gh run view 1', section).returncode, 2)

    def test_a_broken_part_lets_every_command_through_and_says_so(self):
        for name, kw in (('wrong type', {'section': {'pollLoop': {'maxSleepSeconds': 'x'}}}),
                         ('a bool for a number', {'section': {'pollLoop': {'loopSleepSeconds': True}}}),
                         ('unknown key', {'section': {'pollLoop': {'maxSleep': 1}}}),
                         ('half-written file', {'text': '{"version": 1, "agent-guards": {"pollLoop": {"ena'}),
                         ('empty file', {'text': ''})):
            with self.subTest(name):
                r = self.run_hook('sleep 300; gh run view 1', **kw)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn('built-in defaults used', r.stderr)
                self.assertIn('lets every command through', json.loads(r.stdout)['systemMessage'])

    def test_an_old_machine_file_without_the_part_uses_the_defaults_quietly(self):
        for section in (None, {'capacityPipe': {'commands': ['t3-capacity']}}):
            with self.subTest(section):
                r = self.run_hook('sleep 300; gh run view 1', section)
                self.assertEqual(r.returncode, 2)
                self.assertNotIn('defaults', r.stderr)
                self.assertEqual((self.run_hook('sleep 1', section).returncode), 0)


if __name__ == '__main__':
    unittest.main()
