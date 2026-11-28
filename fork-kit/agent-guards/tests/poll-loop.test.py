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


SID = 'aaaaaaaa-1111-2222-3333-444444444444'      # this session
OTHER = 'bbbbbbbb-1111-2222-3333-444444444444'    # another one
TASKS = f'/tmp/claude-{os.getuid()}/-work-p/{SID}/tasks'
MINE = 4242                                       # a PID in the agent's own process tree (a stand-in test)
ENDED = 4343                                      # a PID that names no process now (a stand-in test)
OWN = G.Own(SID, pid=lambda n: n == MINE, gone=lambda n: n == ENDED)
# the clash: bg-guard refuses to end the turn while these jobs run, so a wait on them must pass (the first two are
# the shapes agents were refused on, 11-28)
OWN_WAITS = [
    f'f={TASKS}/b1bj9nwnj.output; for i in $(seq 1 19); do [ -s "$f" ] && break; sleep 30; done; cat "$f"',
    f'D={TASKS}; timeout 290 bash -c "until [ -s $D/bkyuc0ftk.output ]; do sleep 5; done"; cat $D/bkyuc0ftk.output',
    f'until [ -s {TASKS}/x.output ]; do sleep 20; done',
    f'export F={TASKS}/x.output; until grep -q DONE "$F"; do sleep 20; done',
    f'until tail -1 {TASKS}/x.output | grep -q ok; do sleep 20; done',
    f'until test -s ${{T}}/x.output; do sleep 30; done'.replace('${T}', TASKS),
    f'sleep 300; tail -5 {TASKS}/x.output',
    f'while kill -0 {MINE} 2>/dev/null; do sleep 30; done',
    f'while kill -0 {MINE} 2>&1 >/dev/null; do sleep 30; done',
    f'P={MINE}; while kill -0 $P; do sleep 30; done',
    f'while ps -p {MINE} >/dev/null; do sleep 30; done',
    f'while [ -d /proc/{MINE} ]; do sleep 30; done',
    'make & p=$!; while kill -0 $p; do sleep 10; done',
    f'while kill -0 {MINE} 2>/dev/null || [ ! -s {TASKS}/x.output ]; do sleep 30; done',
    f'until [[ -s {TASKS}/x.output ]]; do sleep 30; done',
    f'while ps -p {MINE} -o comm= >/dev/null; do sleep 30; done',
    f'until grep -qF "all done" {TASKS}/x.output; do sleep 30; done',
    f'until tail -n 5 {TASKS}/x.output | grep -q x; do sleep 30; done',
    # bg-guard's wait after one of the jobs it named has ended (it then names no process at all)
    f'while kill -0 {ENDED} 2>/dev/null || kill -0 {MINE} 2>/dev/null; do sleep 30; done',
    f'while kill -0 {ENDED} 2>/dev/null; do sleep 30; done',
]
# still refused with the agent's own jobs known: not its own, or CI beside its own
NOT_OWN = [
    f'until [ -s /tmp/claude-1000/-work-p/{OTHER}/tasks/x.output ]; do sleep 20; done',   # another session's
    f'until [ -s {TASKS}/../../../../etc/x ]; do sleep 20; done',                         # out of the folder
    f'until [ -s {TASKS}/sub/dir/x ]; do sleep 20; done',                                 # not a task file
    f'until [ -s /work/{SID}/x.output ]; do sleep 20; done',                              # no tasks folder
    f'while kill -0 {MINE + 1}; do sleep 30; done',                                       # not in its tree
    'p=$!; while kill -0 $p; do sleep 10; done',                                          # no & in this command
    'make 2>&1; p=$!; while kill -0 $p; do sleep 10; done',                               # 2>&1 is no &
    f'until grep -q DONE {TASKS}/x.output /var/log/syslog; do sleep 20; done',            # and another file
    f'until grep -q DONE {TASKS}/x.output || gh pr checks 1; do sleep 20; done',          # and CI
    f'until [ -s {TASKS}/x.output ] && [ -f /work/done ]; do sleep 20; done',
    'while pgrep -f vitest >/dev/null; do sleep 20; done',                                # pgrep: anyone's
    f'until [ -s x.output ]; do sleep 20; done',                                          # relative
    f'f=$(ls {TASKS}/*.output); until [ -s "$f" ]; do sleep 20; done',                    # a value not known
    f'until [ -s "{TASKS}/$ID.output" ]; do sleep 20; done',
    f'while kill -0 $(cat {TASKS}/x.pid); do sleep 30; done',
    f'while ! jq -e .done {TASKS}/x.output >/dev/null; do sleep 30; done',                # jq: not on the list
    # round 1 of the review (PR #1196): each passed on 2b73dd51
    f'OUT={TASKS}/x.output; until [[ -s $OUT || -f /tmp/ci-done ]]; do sleep 60; done',   # [[ ]] split at ||
    f'until [[ -s {TASKS}/x.output && ! -e /tmp/lock ]]; do sleep 60; done',
    f'until [ -s /proc/{MINE}/cwd/ci-status.txt ]; do sleep 60; done',                    # /proc/<pid>/ reaches
    f'until grep -q success /proc/{MINE}/root/home/u/ci.json; do sleep 60; done',         # every file
    f'until find {TASKS} -name b1.output -exec gh pr checks 5 \\; ; do sleep 60; done',     # find runs anything
    f'until find {TASKS} -newer x -exec gh run view 1 \\; | grep -q completed; do sleep 60; done',
    f'while ps -e -p {MINE} -o comm | grep -q gh; do sleep 60; done',                     # ps: other selectors
    f'while ps -p {MINE} -u root >/dev/null; do sleep 60; done',
    f'while ps -p {MINE} -C gh >/dev/null; do sleep 30; done',
    f'while ps -p {MINE} --ppid 1 >/dev/null; do sleep 30; done',
    f'while ! kill -0 {MINE} -5555; do sleep 30; done',                                   # a process group
    f'until grep -q pass {TASKS}/x.output - < /tmp/ci-status; do sleep 60; done',         # another input
    f'until grep -q pass {TASKS}/x.output /dev/stdin < /tmp/ci-status; do sleep 60; done',
    f'until rg --pre=ghcheck -q pass {TASKS}/x.output; do sleep 60; done',                # rg runs a command
    f'f={TASKS}/x.output; read -r f < /tmp/ptr; until [ -s "$f" ]; do sleep 60; done',    # set again
    f'f={TASKS}/x.output; for f in /tmp/x; do until [ -s "$f" ]; do sleep 60; done; done',
    f'f={TASKS}/x; f+=.output; until [ -s "$f" ]; do sleep 60; done',
    f'P={MINE}; while kill -0 $P; do sleep 30; read P < /run/other.pid; done',
    f'x && f={TASKS}/x.output; until [ -s "$f" ]; do sleep 60; done',                     # set or not
    f'f={TASKS}/x.output true; until [ -s "$f" ]; do sleep 60; done',                     # for `true` only
    f'(f={TASKS}/x.output); until [ -s "$f" ]; do sleep 60; done',                        # in a subshell
    f'bash -c "f={TASKS}/x.output"; until [ -s "$f" ]; do sleep 60; done',
    f'declare -n f={TASKS}/x.output; until [ -s "$f" ]; do sleep 60; done',
    f'until grep -f /tmp/patterns {TASKS}/x.output; do sleep 60; done',                   # an option off the list
    f'until [ -s /work/{SID}/tasks/x.output ]; do sleep 60; done',                        # not Claude's folder
    f'until [ -s /tmp/claude-{os.getuid() + 1}/p/{SID}/tasks/x.output ]; do sleep 60; done',   # another user's
    f'until [ -s {TASKS}/x.log ]; do sleep 60; done',                                     # not a .output
    # a PID that names no process, anywhere but a `while` loop's own condition: it waits for one to take it
    f'until kill -0 {ENDED}; do sleep 30; done',
    f'while ! kill -0 {ENDED}; do sleep 30; done',
    f'while true; do kill -0 {ENDED} || break; sleep 30; done',
    f'while bash -c "kill -0 {ENDED}"; do sleep 30; done',
]


class OwnJob(unittest.TestCase):
    """The clash (11-28): bg-guard refuses to end a turn while a background job of the agent runs, and this guard
    refused every wait on that job but `timeout 590 tail --pid=..`. A wait whose every check reads only the agent's
    own background job passes; anything else is judged as before."""

    def test_a_wait_on_its_own_background_job_passes(self):
        for cmd in OWN_WAITS:
            with self.subTest(cmd):
                self.assertIsNotNone(G.verdict(cmd, DEFAULTS), 'a wait at all')   # without knowing what is its own
                self.assertIsNone(G.verdict(cmd, DEFAULTS, own=OWN))

    def test_a_wait_on_anything_else_stays_refused(self):
        for cmd in NOT_OWN + REFUSED:
            with self.subTest(cmd):
                self.assertIsNotNone(G.verdict(cmd, DEFAULTS, own=OWN))

    def test_no_session_id_makes_no_file_its_own(self):
        for sid in ('', 'x', None, 'a/b/c/d/e/f', '../../../tmp'):
            with self.subTest(sid):
                own = G.Own(sid, pid=lambda n: False)
                self.assertFalse(own.path('/tmp/claude-1000/p//tasks/x.output'))
                self.assertIsNotNone(G.verdict('until [ -s /tmp/c//tasks/x.output ]; do sleep 20; done', DEFAULTS,
                                               own=own))

    def test_a_pid_test_that_fails_is_not_its_own(self):
        def boom(n):
            raise OSError('gone')
        for gone in (lambda n: False, boom):    # a gone test that fails: not gone either
            self.assertIsNotNone(G.verdict(f'while kill -0 {MINE}; do sleep 30; done', DEFAULTS,
                                           own=G.Own(SID, pid=boom, gone=gone)))

    def test_the_refusal_names_the_own_job_way(self):
        text = G.reason(*G.verdict('until test -f x; do sleep 10; done', DEFAULTS), DEFAULTS)
        self.assertIn('own background job', text)
        self.assertIn('kill -0 <pid>', text)

    def test_its_process_tree_is_the_claude_one(self):
        """The hook's own PID test, for real: a process under the stand-in Claude Code is its own; the test's own
        process (above that Claude Code) and PID 1 are not."""
        driver = ('import json, os, sys\nsys.path.insert(0, sys.argv[1])\nimport guards_fixture as fx\n'
                  'g = fx.load_hook("poll-loop-guard")\no = g.Own("x" * 8)\n'
                  'print(json.dumps([o.pid(int(a)) for a in sys.argv[2:]]))\n')
        script = 'sleep 30 & "$@" $!; r=$?; kill $! 2>/dev/null; exit $r'
        r = subprocess.run(['claude', '-c', script, 'claude', sys.executable, '-I', '-B', '-c', driver,
                            os.path.dirname(os.path.abspath(__file__)), str(os.getpid()), '1'],
                           executable='/bin/sh', capture_output=True, text=True, timeout=60, start_new_session=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout), [False, False, True])


    def test_a_symlink_on_the_way_makes_no_file_its_own(self):
        """The path as written must be the real one: a symlink placed as a task file, or as its tasks folder,
        would make any file its own."""
        root = tempfile.mkdtemp(prefix='poll-loop-own-')
        self.addCleanup(shutil.rmtree, root, True)
        own = G.Own(SID, pid=lambda n: False, roots=[root])
        tasks = os.path.join(root, f'claude-{os.getuid()}', 'p', SID, 'tasks')
        os.makedirs(tasks)
        real = os.path.join(tasks, 'real.output')
        open(real, 'w').close()
        os.symlink('/etc/hostname', os.path.join(tasks, 'link.output'))
        self.assertTrue(own.path(real))
        self.assertTrue(own.path(os.path.join(tasks, 'not-yet.output')))      # written when the job prints
        self.assertFalse(own.path(os.path.join(tasks, 'link.output')))
        other = os.path.join(root, f'claude-{os.getuid()}', 'q', SID)
        os.makedirs(os.path.join(root, 'elsewhere'))
        os.makedirs(other)
        os.symlink(os.path.join(root, 'elsewhere'), os.path.join(other, 'tasks'))
        self.assertFalse(own.path(os.path.join(other, 'tasks', 'x.output')))
        for cmd in (f'until [ -s {tasks}/link.output ]; do sleep 60; done',
                    f'until [ -s {other}/tasks/x.output ]; do sleep 60; done'):
            self.assertIsNotNone(G.verdict(cmd, DEFAULTS, own=own), cmd)
        self.assertIsNone(G.verdict(f'until [ -s {real} ]; do sleep 60; done', DEFAULTS, own=own))

    def test_a_pid_that_names_no_process_is_gone_for_real(self):
        """The hook's own test: a PID above the kernel's highest (2^22) never names a process."""
        free = 2 ** 22 + 1
        own = G.Own(SID, pid=lambda n: n == MINE)
        self.assertTrue(own.gone(free))
        self.assertFalse(own.gone(os.getpid()))
        self.assertFalse(own.gone(1))
        self.assertIsNone(G.verdict(f'while kill -0 {free} 2>/dev/null || kill -0 {MINE} 2>/dev/null; do sleep 30; '
                                    'done', DEFAULTS, own=own))
        self.assertIsNotNone(G.verdict(f'until kill -0 {free}; do sleep 30; done', DEFAULTS, own=own))
        self.assertIsNotNone(G.verdict(f'while kill -0 {os.getpid()}; do sleep 30; done', DEFAULTS, own=own))

    def test_a_monitor_curl_to_anything_but_this_machine_counts_as_ci(self):
        for cmd in ('until curl -sf localhost:8080/health; do sleep 5; done',
                    'until curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/ | grep -q 200; do sleep 5; done'):
            with self.subTest(cmd):
                self.assertIsNone(G.verdict(cmd, DEFAULTS, own=OWN, monitor=True))
        for cmd in ('until curl -s https://status.example.com/api | grep -q ok; do sleep 60; done',
                    'until curl -s example.com/status; do sleep 60; done',
                    'until curl -s localhost example.com; do sleep 5; done',
                    'until curl --connect-to localhost:80:example.com:80 localhost; do sleep 5; done',
                    'until curl -x http://proxy:3128 localhost; do sleep 5; done',
                    'until curl -s "$URL"; do sleep 5; done',
                    'until wget -q -O- https://api.github.com/repos/o/r/actions/runs/1; do sleep 60; done'):
            with self.subTest(cmd):
                self.assertEqual(G.verdict(cmd, DEFAULTS, own=OWN, monitor=True)[2], 'ci')

def transcript(folder, tool_use_id, name, compact=True, junk=0):
    """A transcript file whose last line is the assistant's tool_use of `name` with that id (after `junk` bytes)."""
    path = os.path.join(folder, 'session.jsonl')
    line = {'type': 'assistant', 'message': {'role': 'assistant', 'content': [
        {'type': 'tool_use', 'id': tool_use_id, 'name': name, 'input': {'command': 'x', 'description': 'd'}}]}}
    with open(path, 'w') as f:
        if junk:
            f.write(json.dumps({'type': 'user', 'message': {'content': 'j' * junk}}) + '\n')
        f.write(json.dumps(line, separators=(',', ':') if compact else None) + '\n')
    return path


class MonitorCalls(unittest.TestCase):
    """Claude Code runs a Monitor's command through the Bash hooks as tool_name "Bash" (seen 11-28, 2.1.325: a
    PreToolUse "Bash" hook got {"tool_name": "Bash", "tool_input": {"command", "description"}} for a Monitor call), so
    a Monitor until-loop was refused as a Bash loop although the kit wires this guard for Bash only to let it pass.
    The guard looks the call up in the transcript."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='poll-loop-monitor-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def event(self, cmd, path, tid='toolu_01Mon'):
        return dict(bash(cmd), tool_use_id=tid, transcript_path=path)

    def hook(self, event):
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event), capture_output=True, text=True,
                              timeout=60)

    def test_a_monitor_until_loop_on_a_file_passes_and_one_on_ci_is_refused(self):
        for compact in (True, False):
            with self.subTest(compact=compact):
                path = transcript(self.tmp, 'toolu_01Mon', 'Monitor', compact)
                for cmd in ('until [ -s /work/out ]; do sleep 20; done; cat /work/out',
                            'until grep -q "idle alarm" /var/log/filler.log; do sleep 5; done'):
                    r = self.hook(self.event(cmd, path))
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''), cmd)
                for cmd in ('until gh pr checks 1; do sleep 60; done', 'gh run watch 9',
                            'until [ -s /work/out ] && gh pr checks 1; do sleep 60; done'):
                    r = self.hook(self.event(cmd, path))
                    self.assertEqual(r.returncode, 2, cmd)
                    self.assertIn('push and stop', r.stderr)

    def test_a_bash_call_or_one_it_cannot_find_is_judged_as_bash(self):
        cmd = 'until [ -s /work/out ]; do sleep 20; done'
        bash_line = transcript(self.tmp, 'toolu_01Mon', 'Bash')
        for name, ev in (('a Bash call', self.event(cmd, bash_line)),
                         ('another id', self.event(cmd, bash_line, tid='toolu_other')),
                         ('no transcript', self.event(cmd, os.path.join(self.tmp, 'none.jsonl'))),
                         ('no id', dict(bash(cmd), transcript_path=bash_line)),
                         ('an odd id', self.event(cmd, bash_line, tid='"x" .*')),
                         ('a folder', self.event(cmd, self.tmp))):
            with self.subTest(name):
                start = time.monotonic()
                r = self.hook(ev)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertLess(time.monotonic() - start, G.MONITOR_LOOK + 10)   # bounded (a slow runner: + 10 s)

    def test_a_line_written_just_after_the_hook_starts_is_found(self):
        path = os.path.join(self.tmp, 'late.jsonl')
        open(path, 'w').close()
        import threading
        t = threading.Timer(0.3, lambda: transcript(self.tmp, 'toolu_late', 'Monitor') and
                            os.replace(os.path.join(self.tmp, 'session.jsonl'), path))
        t.start()
        self.addCleanup(t.cancel)
        self.assertEqual(G.tool_of({'tool_use_id': 'toolu_late', 'transcript_path': path}), 'Monitor')

    def test_it_reads_only_the_end_of_a_big_transcript(self):
        near = transcript(self.tmp, 'toolu_near', 'Monitor', junk=3 * G.TRANSCRIPT_TAIL)
        self.assertEqual(G.tool_of({'tool_use_id': 'toolu_near', 'transcript_path': near}, wait=0), 'Monitor')
        with open(near, 'a') as f:      # the line is now further from the end than the tail it reads
            f.write(json.dumps({'type': 'user', 'message': {'content': 'k' * G.TRANSCRIPT_TAIL}}) + '\n')
        with fx.bytes_read(G) as reads:
            self.assertIsNone(G.tool_of({'tool_use_id': 'toolu_near', 'transcript_path': near}, wait=0))
        self.assertLessEqual(sum(reads), G.TRANSCRIPT_TAIL)

    def test_a_wait_tool_of_the_host_is_never_read(self):
        for name in ('mcp__t3-code__t3_background_wait', 'Monitor', 'mcp__t3-code__watch_pull_request'):
            with self.subTest(name):
                r = self.hook({'hook_event_name': 'PreToolUse', 'tool_name': name,
                               'tool_input': {'command': 'until test -f x; do sleep 10; done', 'taskId': 'b1'}})
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))


class BgGuardAgrees(unittest.TestCase):
    """The contract of the two guards, run for real under a stand-in Claude Code with a background Bash tool
    command: bg-guard blocks the stop and names a wait; this guard lets exactly that wait through (and still refuses
    the same loop on a process outside the agent's tree)."""

    DRIVER = r"""import json, os, re, subprocess, sys
sys.path.insert(0, sys.argv[1])
import guards_fixture as fx
def hook(name, ev):
    return subprocess.run([sys.executable, '-I', '-B', '-c', fx.RUNNER, fx.LIB, fx.NO_MACHINE, str(os.geteuid()),
                           os.path.join(fx.BIN, name)], input=json.dumps(ev), capture_output=True, text=True,
                          timeout=60)
stop = hook('bg-guard', {'hook_event_name': 'Stop', 'session_id': 'agrees-1'})
reason = json.loads(stop.stdout)['reason']
wait = re.search(r'`(while [^`]+)`', reason).group(1)
bash = lambda c: {'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'session_id': 'agrees-1',
                  'tool_input': {'command': c, 'timeout': 600000}}
mine = hook('poll-loop-guard', bash(wait))
other = hook('poll-loop-guard', bash(re.sub(r'kill -0 \d+', 'kill -0 %d' % int(sys.argv[2]), wait)))
print(json.dumps({'reason': reason, 'wait': wait, 'mine': [mine.returncode, mine.stderr],
                  'other': [other.returncode, other.stderr]}))
"""

    def test_the_wait_bg_guard_names_passes_this_guard(self):
        tmp = tempfile.mkdtemp(prefix='poll-loop-agrees-')
        self.addCleanup(shutil.rmtree, tmp, True)
        script = ("sh -c 'sleep 30; true' shell-snapshots/snapshot-agrees >/dev/null 2>&1 & B=$!; "
                  '"$@"; r=$?; kill $B 2>/dev/null; exit $r')
        r = subprocess.run(['claude', '-c', script, 'claude', sys.executable, '-I', '-B', '-c', self.DRIVER,
                            os.path.dirname(os.path.abspath(__file__)), str(os.getpid())],
                           executable='/bin/sh', capture_output=True, text=True, timeout=120, start_new_session=True,
                           env=dict(os.environ, HOME=tmp))
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertIn('t3_background_wait', out['reason'])
        self.assertRegex(out['wait'], r'^while kill -0 \d+ 2>/dev/null; do sleep 30; done$')
        self.assertEqual(out['mine'], [0, ''], out['wait'])
        self.assertEqual(out['other'][0], 2, out['other'])          # the test process is above that Claude Code


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
