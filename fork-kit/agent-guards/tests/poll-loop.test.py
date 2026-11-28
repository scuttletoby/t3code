#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/poll-loop-guard (the Claude Code PreToolUse hook that refuses a Bash command
that waits on CI, a pull request or a job by sleeping and checking again; #1150). The guard is loaded with no
machine file (its defaults) and, in Configured, with one. Its settings entry is tested in wiring.test.py.

  python3 -I -B fork-kit/agent-guards/tests/poll-loop.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest, uuid
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
    # fix round 1 (#1151 code lens): bypasses that passed before
    'echo "# x"; while ! gh pr checks 1; do sleep 60; done',
    'gh pr comment 5 -b "#1150 done"; while ! gh pr checks 5; do sleep 60; done',
    "sudo -u bob bash -c 'while ! gh pr checks 1; do sleep 60; done'",
    'cat <<EOF | bash\nwhile ! gh pr checks 1; do sleep 60; done\nEOF',
    'watch -n 60 gh pr checks 1',
    "watch -n60 'gh run view 9 --json status'",
    # counted waits still count
    'n=0; while ! gh pr checks 5 && [ $n -lt 100 ]; do n=$((n+1)); sleep 60; done',
    'while (( n < 50 )); do test -f /work/done && break; n=$((n+1)); sleep 10; done',
    'for i in 1 2 3; do gh pr checks 5 && break; sleep 60; done',
    # fix round 2 (#1151 code lens): a status test in the body is no bound, and $(...) inside a word is read
    'while true; do p=$(gh pr checks 5 | grep -c pending); [ "$p" -eq 0 ] && break; sleep 60; done',
    'while [ "$(gh pr view 5 --json state -q .state)" = OPEN ]; do sleep 60; done',
    'while true; do gh run view 9 --exit-status; rc=$?; [ $rc -eq 0 ] && break; sleep 60; done',
    'until [ "$(gh run view 9 --json status -q .status)" = completed ]; do sleep 30; done',
    'while [ `gh pr checks 5 | grep -c pending` -gt 0 ]; do sleep 60; done',
    # a quoted # mid-word or after a space is no comment (removing the quote tracking turns these red)
    'echo "a # b"; while ! gh pr checks 1; do sleep 60; done',
    'gh pr comment 5 -b "done #1150"; while ! gh pr checks 5; do sleep 60; done',
]
# fix round 1 (#1151 code lens): retry loops it refused before
RETRIES = [
    'n=0; while [ $n -lt 3 ]; do git fetch origin && break; n=$((n+1)); sleep 10; done',
    'for i in 1 2 3; do npm ci && break; sleep 60; done; cat npm-debug.log',
    'ls dist; for i in 1 2 3 4 5; do rsync -a dist/ host:/srv && break; sleep 30; done',
    'for i in 1 2 3; do npm ci && break; sleep 60; done',
    'n=0; while true; do git push && break; n=$((n+1)); [ $n -ge 3 ] && exit 1; sleep 30; done',
    'until git pull; [ $n -lt 3 ]; do n=$((n+1)); sleep 30; done',
    'for i in $(seq 1 5); do make 2>&1 | grep -q ok && break; sleep 60; done',
    'while (( n < 3 )); do ./deploy && break; n=$((n+1)); sleep 30; done; tail -5 deploy.log',
]
# known gaps, pinned so a change is seen: a count it cannot read is one round
GAPS = [
    'for i in $(seq 1 $N); do gh pr checks 1 && break; sleep 60; done',
    'while [ $n -lt $MAX ]; do test -f x && break; n=$((n+1)); sleep 60; done',
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

# F7: a pkill -f whose pattern is in the command's own text kills the shell that runs it (exit 143/144)
SELF_KILLS = [
    'pkill -f "vitest run"',
    'pkill -f vitest; sleep 1; pnpm test',
    'sudo pkill -9 -f vitest',
    'pkill -KILL -f vitest',
    'pkill -SIGTERM --full vitest',
    'pkill -fi VITEST',
    'pkill -u builder -f vitest',
    'pkill -f -- vitest',
    'timeout 5 pkill -f vitest',
    "bash -c 'pkill -f vitest'",
    'ls; pkill -f "node .*vitest" || true',
    'kill $(pgrep -f vitest)',
    'kill -9 "$(pgrep -f vitest)"',
    'pgrep -f vitest | xargs kill',
    'pgrep -f vitest | xargs -r kill -9',
    "pkill -f '[v]itest'; cd /work/vitest",     # the bracket helps only if the name is nowhere else
    'pkill -f bash',                             # the shell's own name
    'pkill -ABRT -f vitest',                     # a signal name is not an option cluster (A is -A)
    "pkill -f \"vitest'[[:punct:]]''\"",            # matches only the text as the host quotes it ('\\'')
    "bash -c 'pkill -f \"c .[p]kill\"'",           # matches only the text as written (a host that passes it as is)
    "pkill -fi '[V]ITEST'; cd /work/vitest",     # -i: any case
    "pkill -f '[[:alpha:]]itest'; cd /work/vitest",   # a POSIX class
]
KILL_PASSES = [
    "pkill -f '[v]itest run'",
    "pkill -f '[v]itest'; echo stopped",
    "pkill -f '[V]ITEST'; cd /work/vitest",     # no -i: case counts
    'pkill vitest',
    'pkill -x -f vitest',
    'pkill -A -f vitest',
    'pkill --ignore-ancestors -f vitest',
    'pkill -F run.pid',
    'pkill -f -F run.pid vitest',
    'pkill -f "$PAT"',
    "pkill -f '^node .*vitest'",
    "pkill -f 'vitest$'",
    'pkill -f "(unclosed"',
    "pkill -f '[[:alpha:]]itest'",
    'kill "$(cat run.pid)"',
    'pgrep -f vitest',
    'pgrep -f vitest | wc -l',
    'kill -0 $(pgrep -f vitest)',
    'echo "pkill -f vitest"',
    'git commit -m "stop pkill -f vitest"',
    'cat > notes.md <<EOF\npkill -f vitest\nEOF',
    '# pkill -f vitest\nls',
]
KILL_GAPS = [
    'PAT=vitest; pkill -f "$PAT"',             # a pattern from a variable is not read
    'for p in $(pgrep -f vitest); do kill $p; done',
]
# the same shapes run for real: {m} is a marker no other process has, {h}{t} the marker split after its first letter
REAL = [
    'pkill -f {m}', 'pkill -9 -f {m}', 'pkill --full {m}', 'timeout 5 pkill -f {m}', "bash -c 'pkill -f {m}'",
    'kill $(pgrep -f {m})', 'kill "$(pgrep -f {m})"', 'pgrep -f {m} | xargs kill', 'pkill -fi {M}',
    "pkill -f '[{h}]{t}'; echo {m}", "pkill -f '[{h}]{t}'", 'pkill {m}', 'pkill -x -f {m}', 'pkill -A -f {m}',
    "pkill -f '^{m}'", "pkill -f '{m}$'", 'pkill -f "({m}"', "pkill -f '[[:alpha:]]{t}'", 'echo pkill -f {m}',
    'pgrep -f {m}', 'kill -0 $(pgrep -f {m})', 'pkill -USR1 -f {m}', 'pkill -ABRT -f {m}',
    'pkill -f "{m}\'[[:punct:]]\'\'"',
    "pkill -fi '[{H}]{T}'; echo {m}", "pkill -f '[{H}]{T}'; echo {m}", "pkill -f '[[:alpha:]]{t}'; echo {m}",
]


def bash(command, **more):
    return {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command, **more},
            'session_id': 's'}


class Reads(unittest.TestCase):
    def test_waits_on_ci_files_and_jobs_are_refused(self):
        for cmd in REFUSED:
            with self.subTest(cmd):
                self.assertIsNotNone(G.verdict(cmd, DEFAULTS))

    def test_retry_loops_and_known_gaps_pass(self):
        for cmd in RETRIES + GAPS:
            with self.subTest(cmd):
                self.assertIsNone(G.verdict(cmd, DEFAULTS))

    def test_the_way_out_fits_the_wait(self):
        ci = G.reason(*G.verdict('while ! gh pr checks 1; do sleep 60; done', DEFAULTS), DEFAULTS)
        self.assertIn('push and stop', ci)
        self.assertNotIn('t3_background_wait', ci)
        local = G.reason(*G.verdict('until test -f x; do sleep 10; done', DEFAULTS), DEFAULTS)
        self.assertIn('t3_background_wait', local)
        self.assertNotIn('push and stop', local)
        self.assertIn('retry loop', local)

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


class SelfKill(unittest.TestCase):
    """F7: `pkill -f X` killed its own shell 107 times in two weeks (exit 144, the rest of the command never ran)."""

    def test_a_pattern_in_its_own_text_is_refused_in_the_foreground_and_the_background(self):
        for cmd in SELF_KILLS:
            with self.subTest(cmd):
                self.assertEqual(G.verdict(cmd, DEFAULTS)[2], 'kill')
                self.assertEqual(G.verdict(cmd, DEFAULTS, background=True)[2], 'kill')

    def test_a_pattern_that_misses_its_own_text_passes(self):
        for cmd in KILL_PASSES + KILL_GAPS:
            with self.subTest(cmd):
                self.assertIsNone(G.verdict(cmd, DEFAULTS, background=True))
                self.assertIsNone(G.verdict(cmd, DEFAULTS))

    def test_a_background_wait_still_passes(self):
        self.assertIsNone(G.verdict('while ! gh pr checks 1; do sleep 60; done', DEFAULTS, background=True))

    def test_the_refusal_names_the_way_out(self):
        text = G.reason(*G.verdict('pkill -f "vitest run"', DEFAULTS), DEFAULTS)
        self.assertTrue(text.startswith("Refused by poll-loop-guard: `pkill -f 'vitest run'` also matches"), text)
        for said in ("'[v]itest run'", 'kill "$(cat run.pid)"', '-x, -A', 'exit 143/144'):
            self.assertIn(said, text)
        self.assertNotIn('sleeping', text)

    def test_the_hook_refuses_a_background_self_kill(self):
        r = subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(bash('pkill -f x1', run_in_background=True)),
                           capture_output=True, text=True, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertIn('kills that shell', r.stderr)

    @unittest.skipUnless(shutil.which('pkill') and shutil.which('pgrep'), 'no procps here')
    def test_it_refuses_exactly_what_really_kills_its_shell(self):
        """The contract: run each shape the way the host does (`bash -c` around an `eval '<text>'`) with a marker
        only that shell has; the guard refuses it exactly when the shell really dies."""
        died_any = lived_any = False
        for shape in REAL:
            m = 'zq' + uuid.uuid4().hex[:8]
            cmd = shape.format(m=m, M=m.upper(), h=m[0], t=m[1:], H=m[0].upper(), T=m[1:].upper())
            with self.subTest(cmd):
                r = subprocess.run(['bash', '-c', "eval '" + cmd.replace("'", "'\\''") + "'\necho __alive__"],
                                   capture_output=True, text=True, timeout=30)
                died = '__alive__' not in r.stdout
                died_any, lived_any = died_any or died, lived_any or not died
                self.assertEqual(G.verdict(cmd, DEFAULTS) is not None, died, (r.returncode, r.stderr[-300:]))
        self.assertTrue(died_any and lived_any)


class Hook(unittest.TestCase):
    def hook(self, event, raw=None):
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event) if raw is None else raw,
                              capture_output=True, text=True, timeout=60)

    def test_refused_is_exit_2_with_the_way_to_wait_on_stderr(self):
        r = self.hook(bash('for i in $(seq 1 40); do grep -q DONE "$F" && break; sleep 30; done'))
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertTrue(r.stderr.startswith('Refused by poll-loop-guard: '), r.stderr)
        for said in ('run_in_background', 't3_background_wait', 'Monitor', 'retry loop'):
            self.assertIn(said, r.stderr)
        r = self.hook(bash('while ! gh pr checks 1; do sleep 60; done'))
        self.assertEqual(r.returncode, 2)
        for said in ('push and stop', 'watch_pull_request'):
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
        """Fast once loaded (the start of Python itself is not counted): a few ms a call on a quiet machine. The
        limit is 500 ms on average, so a busy CI runner never fails it; a regex that backtracks or a parse that
        grows with the square of the command is far slower than that on these commands."""
        cmds = REFUSED + PASSED + RETRIES + GAPS
        start = time.perf_counter()
        for _ in range(5):
            for cmd in cmds:
                G.verdict(cmd, DEFAULTS)
        self.assertLess((time.perf_counter() - start) / (5 * len(cmds)), 0.5)


class Configured(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='poll-loop-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_hook(self, cmd='sleep 300; gh run view 1', section=None, text=None, sid=None):
        event = dict(bash(cmd), session_id=uuid.uuid4().hex if sid is None else sid)
        return fx.run_hook('poll-loop-guard', event, fx.machine_file(self.tmp, section, text),
                           env=dict(os.environ, HOME=self.tmp))

    def test_a_broken_file_is_said_once_a_session(self):
        """#1151: one notice per hook per session, not one on every call; stderr (not read by the agent) keeps it."""
        broken = {'text': '{"version": 1, "agent-guards": {"pollLoop": {"ena'}
        first = self.run_hook(sid='one', **broken)
        again = self.run_hook(sid='one', **broken)
        other = self.run_hook(sid='two', **broken)
        self.assertIn('systemMessage', json.loads(first.stdout))
        self.assertEqual((again.returncode, again.stdout), (0, ''))
        self.assertIn('built-in defaults used', again.stderr)
        self.assertIn('systemMessage', json.loads(other.stdout))
        nosid = self.run_hook(sid='', **broken)
        self.assertIn('systemMessage', json.loads(nosid.stdout))

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
