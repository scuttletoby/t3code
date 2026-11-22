#!/usr/bin/env python3
"""Tests of fork-kit/agent-guards/bin/capacity-pipe-guard (the Claude Code PreToolUse hook that refuses a piped
capacity check: `t3-capacity 2048 | tail -1 && <heavy step>` ran the heavy step on NOT OK, four agents, 2026-11-18).
The guard is loaded with no machine file (the default check, t3-capacity) and, in Configured, with one that names a
second check. Its settings entry (missing hook, failed hook) is tested in wiring.test.py.

  python3 -I -B fork-kit/agent-guards/tests/capacity-pipe.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402

HOOK = os.path.join(fx.BIN, 'capacity-pipe-guard')
G = fx.load_hook('capacity-pipe-guard')

REFUSED = [
    't3-capacity 2048 | tail -1 && heavy ./gradlew build',   # as reported
    't3-capacity 2048 | tail -1',
    't3-capacity | cat',                                     # every pipe, also one that decides nothing
    't3-capacity 2048|tail -1',
    't3-capacity 2048 2>&1 | tail -1 && x',
    't3-capacity 2048 |& tail -1',
    't3-capacity 2048 > /tmp/cap.txt | tail -1',
    't3-capacity 2048 | tee /tmp/cap.txt',
    't3-capacity $N | tail -1',
    't3-capacity "$NEED" | tail -1',
    't3-capacity 2048 \\\n  | tail -1',
    'set -o pipefail; t3-capacity 2048 | tail -1 && x',      # right, but refused: the rule is "never pipe it"
    # the command word, however it is written
    '/usr/local/bin/t3-capacity 2048 | tail -1',
    '~/.local/bin/t3-capacity 2048 | head -1',
    '"$BIN"/t3-capacity 1 | tail -1',
    '"t3-capacity" 2048 | tail -1',
    "'t3-capacity' 2048 | tail -1",
    't3-capaci\\ty 2048 | tail -1 # t3-capacity',
    'X=1 t3-capacity 2048 | tail -1',
    'T3_CAPACITY_USER=x Y="a b" t3-capacity 2048 | tail -1',
    'sudo t3-capacity 2048 | tail -1',
    'sudo -u someone t3-capacity 2048 | tail -1',
    'env A=b t3-capacity 2048 | tail -1',
    'env -i PATH=/usr/bin t3-capacity 2048 | tail -1',
    'timeout 50 t3-capacity 768 | tail -1',                  # timing: guard input, not a wait
    'timeout -k 5 50s /usr/local/bin/t3-capacity 768 | tail -1',  # timing: guard input, not a wait
    'command t3-capacity 1 | tail -1',
    'nice -n 5 t3-capacity 1 | tail',
    'time t3-capacity 1 | tail',
    'sudo env A=b nice t3-capacity 1 | tail',
    'sudo -n t3-capacity 2048 | tail -1 && x',               # -n takes no value for sudo (it does for nice)
    'sudo -iu someone t3-capacity 1 | tail',
    'sudo -E -u someone -g staff t3-capacity 1 | tail',
    'nice -n 5 ionice -c 3 t3-capacity 1 | tail',
    'stdbuf -oL t3-capacity 1 | tail',
    'stdbuf -o L t3-capacity 1 | tail',
    'time -p t3-capacity 2048 | tail -1 && x',
    # #931: the keyword `time` before a group; a flag cluster whose value is glued on; env -S runs its value's words
    'time { t3-capacity 2048; } | tail -1 && x',
    'time -p { t3-capacity 2048; } | tail -1 && x',
    '! time { t3-capacity 1; } | tail',
    'sudo -uroot t3-capacity 1 | tail -1 && x',
    'sudo -iuroot t3-capacity 1 | tail',
    "env -S 't3-capacity 1' | tail",
    "env -S't3-capacity 1' | tail",
    "env -iS 't3-capacity 1' | tail",
    "env --split-string='t3-capacity 1' | tail",
    "env --split-string 'A=1 t3-capacity 1' | tail",
    "env -S 'nice t3-capacity 1' | tail",
    'env -S \'bash -c "t3-capacity 1 | tail"\'',
    'exec -a cap t3-capacity 1 | tail',
    # PR 993 round 1: the other wrappers a command runs behind (chrt and taskset take a priority / mask first,
    # flock a lock file; numactl, doas, runuser, systemd-run flags with their values)
    'chrt -i 0 t3-capacity 1 | tail',
    'chrt 10 t3-capacity 1 | tail',
    'chrt --idle 0 t3-capacity 1 | tail',
    'chrt -b 0 nice t3-capacity 1 | tail',
    'chrt -d -T 1000 -P 2000 0 t3-capacity 1 | tail',
    'taskset 0x3 t3-capacity 1 | tail',
    'taskset -c 0-3 t3-capacity 1 | tail',
    'taskset -c 0 t3-capacity 1 | tail',
    'flock /tmp/cap.lock t3-capacity 1 | tail',
    'flock -w 5 /tmp/cap.lock t3-capacity 1 | tail',             # timing: guard input, not a wait
    'numactl -N 0 t3-capacity 1 | tail',
    'numactl --cpunodebind=0 t3-capacity 1 | tail',
    'doas -u root t3-capacity 1 | tail',
    'runuser -u someone t3-capacity 1 | tail',
    'systemd-run --user --scope -p MemoryMax=1G t3-capacity 1 | tail',
    'unbuffer t3-capacity 1 | tail',
    'chronic t3-capacity 1 | tail',
    'ionice -c 3 chrt -i 0 t3-capacity 1 | tail',
    'sudo chrt -i 0 t3-capacity 1 | tail',
    # PR 993 round 2: systemd-run -d (--same-dir) takes no value; doas -a STYLE, runuser -s SHELL / -c TEXT; the
    # kit's own heavy (--wait S, --kind K, --mb N, --measure K before the command)
    'systemd-run -P -d t3-capacity 1 | tail',
    'systemd-run --user -d -p MemoryMax=1G t3-capacity 1 | tail',
    'doas -a style t3-capacity 1 | tail',
    'doas -a style -u root t3-capacity 1 | tail',
    'runuser -s /bin/sh -u someone t3-capacity 1 | tail',
    "runuser -u someone -s /bin/bash t3-capacity 1 | tail",
    "runuser someone -c 't3-capacity 1 | tail'",
    "runuser -l someone -c 't3-capacity 1 | tail -1'",
    "runuser -c 't3-capacity 1 | tail' someone",
    # issue 999: the text of su / runuser glued to -c (also in a letter cluster) or given as a long flag
    "runuser -c't3-capacity 1 | tail' someone",
    "runuser -lc't3-capacity 1 | tail' someone",
    "runuser someone -c't3-capacity 1 | tail'",
    "su -c't3-capacity 1 | tail' someone",
    "su -lc't3-capacity 1 | tail'",
    "runuser --command='t3-capacity 1 | tail' someone",
    "runuser --command 't3-capacity 1 | tail' someone",
    "runuser --co='t3-capacity 1 | tail' someone",
    "su --command='t3-capacity 1 | tail' someone",
    "su --session-command='t3-capacity 1 | tail' someone",
    "su --session-command 't3-capacity 1 | tail' someone",
    "sudo runuser -u someone --command='t3-capacity 1 | tail'",
    # PR 1041 round 1: the value of -s / --shell (also glued at the end of a cluster) is no shell of its own
    "su -s /bin/sh -c't3-capacity 1 | tail' someone",
    "su -s /bin/sh -c 't3-capacity 1 | tail' someone",
    "su -ls/bin/sh -c't3-capacity 1 | tail' someone",
    "su -ls /bin/sh -c't3-capacity 1 | tail' someone",
    "su --shell /bin/bash -c't3-capacity 1 | tail' someone",
    "runuser -u someone -s /bin/sh -c't3-capacity 1 | tail'",
    'heavy t3-capacity 1 | tail',
    'heavy --wait 60 t3-capacity 1 | tail',
    'heavy --kind test --mb 512 -- t3-capacity 1 | tail',
    'heavy --measure build t3-capacity 1 | tail',
    'heavy nice t3-capacity 1 | tail',
    # after && ; newline, in if / while / until / for / case, behind !
    'cd /x && t3-capacity 2048 | tail -1 && heavy x',
    'true; t3-capacity 2048 | tail -1',
    'echo a\nt3-capacity 2048 | tail -1 && x',
    'echo "a" && t3-capacity 1 | tail -1 || echo no',
    'echo a > f; t3-capacity 1 | tail',
    'cat x # a comment\nt3-capacity 1 | tail',
    'if t3-capacity 2048 | grep -q OK; then x; fi',          # "NOT OK" holds "OK" too
    'if true; then t3-capacity 2048 | tail -1; fi',
    'while ! t3-capacity 2048 | tail -1; do sleep 60; done',  # timing: guard input, not a wait
    'until t3-capacity 2048 | grep -qx OK; do sleep 60; done',  # timing: guard input, not a wait
    '! t3-capacity 2048 | grep -q NOT',
    'for n in 1 2; do t3-capacity 2048 | tail -1; done',
    'case x in x) t3-capacity 1 | tail -1 ;; esac',
    'f() { t3-capacity 2048 | tail -1; }; f && x',
    'function f { t3-capacity 2048 | tail -1; }; f && x',
    'function f() { t3-capacity 2048 | tail -1; }; f && x',
    'function f {\n  t3-capacity 2048 | tail -1\n}',
    # $(..), backticks, <(..)
    'out=$(t3-capacity 2048 | tail -1) && x',
    'echo "$(t3-capacity 2048 | tail -1)"',
    'echo `t3-capacity 2048 | tail -1`',
    'x=$(echo $(t3-capacity 1 | tail -1))',
    '[ "$(t3-capacity 2048 | tail -1)" = OK ] && x',
    'diff <(t3-capacity 1 | tail -1) x',
    # a group whose last command it is
    '(t3-capacity 2048) | tail -1',
    '(cd /x && t3-capacity 2048) | tail -1',
    '{ t3-capacity 2048; } | tail -1 && x',
    '{ t3-capacity 2048\n} | tail -1',
    '{ echo a; t3-capacity 2048; } 2>&1 | tail -1',
    # shell text handed on, both forms of quoting
    "bash -c 't3-capacity 2048 | tail -1 && x'",
    'bash -c "t3-capacity 2048 | tail -1 && x"',
    "bash -lc 't3-capacity 2048 | tail -1'",
    "sh -c 't3-capacity 2048 | tail -1'",
    "/bin/bash -c 't3-capacity 2048 | tail -1'",
    'bash -o pipefail -c "t3-capacity 2048 | tail -1"',
    'bash -c "t3-capacity $N | tail -1"',
    'heavy bash -c "t3-capacity 2048 | tail -1 && ./gradlew"',
    "timeout 600 bash -c 't3-capacity 2048 | tail -1 && x'",  # timing: guard input, not a wait
    'bash -c "bash -c \\"t3-capacity 1 | tail -1\\""',
    "bash -c 'bash -c \"t3-capacity 1 | tail -1\"'",
    'bash -c "echo $(t3-capacity 1 | tail -1)"',
    "su - someone -c 't3-capacity 1 | tail -1'",
    "eval 't3-capacity 2048 | tail -1'",
    'eval t3-capacity 2048 \\| tail -1',
    "ssh host1 't3-capacity 2048 | tail -1'",
    "watch -n 60 't3-capacity 2048 | tail -1'",
    # a heredoc into a shell is commands
    "bash <<'EOF'\nt3-capacity 2048 | tail -1 && x\nEOF",
    'bash <<EOF\nt3-capacity 2048 | tail -1\nEOF',
    'bash -s <<-EOF\n\tt3-capacity 2048 | tail -1\n\tEOF',
    'sh <<"END" && echo done\nls\nt3-capacity 1 | tail\nEND\necho after',
    "cat > note <<'EOF'\ntext\nEOF\nt3-capacity 1 | tail -1",   # behind a heredoc that is only text
]

ALLOWED = [
    't3-capacity',
    't3-capacity 2048',
    't3-capacity 2048 && heavy ./gradlew build',
    't3-capacity 2048 && heavy x | tail -20',                # the pipe is behind the gate
    't3-capacity 2048 && python3 x.py 2>&1 | grep -E "^(FAIL|OK)" | head',
    't3-capacity; echo $?',
    't3-capacity 2048; echo "rc=$?"',
    't3-capacity 2048 || echo "not ok"',
    't3-capacity 2048 || { echo no; exit 1; }',
    't3-capacity 2048 > /tmp/cap.txt && x',
    't3-capacity 2048 >/dev/null 2>&1 && x',
    't3-capacity 2048 & wait',
    'if t3-capacity 2048; then x; fi',
    'if t3-capacity 2048; then x | tail; fi',
    'if ! t3-capacity 2048; then echo wait | cat; fi',
    'until t3-capacity 2048; do sleep 60; done',             # timing: guard input, not a wait
    'X=$(t3-capacity 2048) && x',
    'out=$(t3-capacity 2048); rc=$?; echo "$out" | tail -1; [ $rc = 0 ] && x',
    'echo x | t3-capacity 2048 && y',                        # into it: the status is its own
    "bash -c 't3-capacity 2048 && x'",
    'bash -c "t3-capacity 2048 && x | tail"',
    "bash <<'EOF'\nt3-capacity 2048 && x\nEOF",
    # a group that gates inside, piped as a whole
    '{ t3-capacity 2048 && heavy x; } 2>&1 | tail -30',
    '(t3-capacity 2048 && heavy x) | tail -30',
    'if t3-capacity 2048; then heavy x; fi | tail -30',
    '{ t3-capacity 2048; echo "rc=$?"; } | tail -2',
    # text that only mentions it
    'grep -n t3-capacity AGENTS.md | head',
    'rg "t3-capacity 2048 \\| tail" -l | head',
    'cat /usr/local/bin/t3-capacity | head -20',
    'command -v t3-capacity | head -1',
    'which t3-capacity | cat',
    'type -a t3-capacity | head',
    'man t3-capacity | head',
    'ls | grep t3-capacity',
    'echo t3-capacity | cat',
    'printf "%s\\n" t3-capacity 1 | tail -1',
    'echo "never t3-capacity 2048 | tail -1" | tee note',
    "echo 't3-capacity 2048 | tail -1'",
    'git commit -m "t3-capacity 1 | tail is refused"',
    "projtool commit -m 'no t3-capacity N | tail' -- x | tail -3",
    'gh pr comment 1 --body "t3-capacity 2048 | tail -1 && cmd hides NOT OK" | cat',
    'bash -c "echo \'t3-capacity 1 | tail\'"',
    'sudo install -m 755 scripts/dev/t3-capacity /usr/local/bin/t3-capacity | cat',
    'sudo -n cat /usr/local/bin/t3-capacity | head',
    'sudo -n diff /usr/local/bin/t3-capacity scripts/dev/t3-capacity | head',
    'sudo -n install -m 755 scripts/dev/t3-capacity /usr/local/bin/ 2>&1 | tail -3',
    'sudo -k ls t3-capacity | head',
    'timeout -s KILL 5 cat t3-capacity | head',              # timing: guard input, not a wait
    'nice -n t3-capacity x | tail',                          # the value of -n, not the command
    'function f { t3-capacity 2048 && x; }; f | tail',
    'time -p grep t3-capacity x | head',
    # #931: these were refused: the value glued to -u was read as a flag cluster ending in -t (a value letter)
    'sudo -uroot cat /usr/local/bin/t3-capacity | head',
    'sudo -uroot diff /usr/local/bin/t3-capacity scripts/dev/t3-capacity | head',
    'sudo -ugit install -m 755 scripts/dev/t3-capacity /usr/local/bin/ 2>&1 | tail -3',
    'time { t3-capacity 2048 && x; } | tail -3',
    "env -S 'cat t3-capacity' | tail",
    'env -C t3-capacity ls | tail',                         # the value of -C (a folder), not the command
    'chrt -i 0 cat t3-capacity | tail',
    'taskset -c 0 grep t3-capacity x | head',
    'flock t3-capacity ls | tail',                          # the lock file, not the command
    'chrt -p 1234 | tail',
    'doas -a t3-capacity ls | tail',                        # the values of -a, -s and --kind, not the command
    'runuser -s t3-capacity -u someone ls | tail',
    'heavy --kind t3-capacity ls | tail',
    "runuser someone -c 'grep t3-capacity x | tail'",
    "runuser someone -c'grep t3-capacity x | tail'",            # issue 999: glued and long forms read the same way
    "su --command='t3-capacity 1 > f && x' someone | tail",
    "runuser -s'/bin/sh' --shell=t3-capacity someone | tail",   # values of -s / --shell, no text to read
    "su --session-command='echo t3-capacity | tail'",
    "su -s /bin/sh -c'grep t3-capacity x | tail' someone",       # PR 1041: still read as text, nothing piped
    'heavy sweep --dry-run | tail',
    'taskset -p 1234 | tail',
    'python3 -I scripts/dev/run_tests.py scripts/dev/tests/test_t3_capacity.py 2>&1 | tail -5',
    '# t3-capacity 2048 | tail -1\nls',
    'ls # t3-capacity 2048 | tail -1',
    # another program
    't3-capacity-old 1 | tail',
    'my-t3-capacity 1 | tail',
    './t3-capacity.bak 1 | tail',
    # a heredoc that is only text (briefs are written this way)
    "cat > brief.md <<'EOF'\nnever `t3-capacity 2048 | tail -1 && cmd`\nEOF",
    'cat <<EOF > x\nt3-capacity 1 | tail\nEOF',
    "cat <<'EOF' | tee x\nt3-capacity 1 | tail\nEOF",
    'python3 x.py <<EOF\nt3-capacity 1 | tail\nEOF',
    "t3 thread send x <<'EOF'\ngate each run with `t3-capacity 2048 && <cmd>`, never `t3-capacity 2048 | tail -1`\nEOF\nls | head",
    "cat > a <<'EOF'\nt3-capacity 1 | tail\nEOF\nt3-capacity 2048 && x",
]

# Read by nobody on purpose (the docstring's "Not covered"); pinned so a change that starts refusing them is seen.
NOT_COVERED = [
    'C=t3-capacity; $C 2048 | tail -1',
    'sh scripts/dev/t3-capacity 1 | tail -1',
    "cat <<'EOF' | bash\nt3-capacity 1 | tail -1\nEOF",
    'echo ${X:-$(t3-capacity 1 | tail -1)}',
    'echo 2048 | xargs t3-capacity | tail -1',
    'sudo --user someone t3-capacity 1 | tail -1',
    "cat <<EOF\n$(t3-capacity 1 | tail -1)\nEOF",
]


class Reads(unittest.TestCase):
    def test_a_pipe_out_of_t3_capacity_is_refused(self):
        for cmd in REFUSED:
            with self.subTest(cmd):
                self.assertTrue(G.piped(cmd))

    def test_everything_else_passes(self):
        for cmd in ALLOWED:
            with self.subTest(cmd):
                self.assertFalse(G.piped(cmd))

    def test_what_it_does_not_cover_passes(self):
        for cmd in NOT_COVERED:
            with self.subTest(cmd):
                self.assertFalse(G.piped(cmd))

    def test_a_refused_command_stays_refused_inside_every_carrier(self):
        """Each refused one-line command again, as shell text: in $(..), in a ( ) group, in bash -c with both quotes."""
        for cmd in REFUSED:
            if '\n' in cmd or "'" in cmd or '"' in cmd or '\\' in cmd or '`' in cmd or '$' in cmd:
                continue
            for form in ('echo "$(%s)"', 'x=$(%s) && y', '( %s )', '{ %s; }', "bash -c '%s'", 'bash -c "%s"',
                         "heavy bash -lc '%s'", "eval '%s'", 'true && %s', "bash <<'EOF'\n%s\nEOF"):
                with self.subTest(form % cmd):
                    self.assertTrue(G.piped(form % cmd))

    def test_an_allowed_command_stays_allowed_inside_every_carrier(self):
        for cmd in ALLOWED:
            if '\n' in cmd or "'" in cmd or '"' in cmd or '\\' in cmd or '`' in cmd or '$' in cmd:
                continue
            for form in ('echo "$(%s)"', 'x=$(%s) && y', "bash -c '%s'", 'bash -c "%s"', 'true && %s',
                         "bash <<'EOF'\n%s\nEOF", "cat <<'EOF'\n%s\nEOF"):
                with self.subTest(form % cmd):
                    self.assertFalse(G.piped(form % cmd))

    def test_the_real_shell_agrees_on_what_hides_the_status(self):
        """The rule's reason, on a real sh with a fake t3-capacity that answers NOT OK: every refused sample form runs
        the step behind it, every allowed one does not."""
        tmp = tempfile.mkdtemp(prefix='capacity-pipe-')
        self.addCleanup(shutil.rmtree, tmp, True)
        tool = os.path.join(tmp, 't3-capacity')
        with open(tool, 'w') as f:
            f.write('#!/bin/sh\necho "NOT OK: full"\nexit 1\n')
        os.chmod(tool, 0o755)
        env = dict(os.environ, PATH=tmp + os.pathsep + os.environ['PATH'])
        cases = {'t3-capacity 2048 | tail -1 && echo RAN': True, '(t3-capacity 2048) | tail -1 && echo RAN': True,
                 '{ t3-capacity 2048; } | tail -1 && echo RAN': True, 'x=$(t3-capacity 2048 | tail -1) && echo RAN': True,
                 "sh -c 't3-capacity 2048 | tail -1 && echo RAN'": True,
                 "env -S 't3-capacity 2048' | tail -1 && echo RAN": True,
                 "env -S 't3-capacity 2048' && echo RAN": False,
                 't3-capacity 2048 && echo RAN': False, 'x=$(t3-capacity 2048) && echo RAN': False,
                 '{ t3-capacity 2048 && echo RAN; } | tail -5': False, 't3-capacity 2048 > /dev/null && echo RAN': False}
        for cmd, hidden in cases.items():
            with self.subTest(cmd):
                out = subprocess.run(['/bin/sh', '-c', cmd], env=env, capture_output=True, text=True, timeout=60).stdout
                self.assertEqual('RAN' in out, hidden, out)
                self.assertEqual(bool(G.piped(cmd)), hidden)
        bash = shutil.which('bash')
        if bash:   # the keyword `time` before a group is bash's (sh has no such keyword)
            for cmd, hidden in {'time { t3-capacity 2048; } | tail -1 && echo RAN': True,
                                'time -p { t3-capacity 2048; } 2>/dev/null | tail -1 && echo RAN': True,
                                'time { t3-capacity 2048 && echo RAN; } 2>/dev/null | tail -5': False}.items():
                with self.subTest(cmd):
                    out = subprocess.run([bash, '-c', cmd], env=env, capture_output=True, text=True, timeout=60).stdout
                    self.assertEqual('RAN' in out, hidden, out)
                    self.assertEqual(bool(G.piped(cmd)), hidden)

    def test_deep_or_long_text_is_read_in_time_and_never_crashes(self):
        deep = '$(' * 3000 + 't3-capacity 1 | tail' + ')' * 3000
        self.assertFalse(G.piped(deep), 'past 40 levels it stops reading: allowed, said in the docstring')
        self.assertTrue(G.piped('$(' * 30 + 't3-capacity 1 | tail' + ')' * 30))
        start = time.monotonic()
        self.assertTrue(G.piped('echo ' + 'word "q $x" \'s\' ; ' * 20000 + 't3-capacity 1 | tail'))
        self.assertFalse(G.piped('(' * 50000 + 't3-capacity 1' + ')' * 50000))
        self.assertFalse(G.piped('"' + 't3-capacity | ' * 50000))
        self.assertTrue(G.piped('echo ' + '$(x) `y` "$(z)" <(w) ' * 20000 + '; t3-capacity 1 | tail'))
        self.assertLess(time.monotonic() - start, 60)  # a quadratic read of these inputs takes far longer

    def test_broken_shell_text_is_read_without_a_crash(self):
        for cmd in ('t3-capacity "', "t3-capacity '", 't3-capacity $(', 't3-capacity `', 't3-capacity ${', 't3-capacity \\',
                    't3-capacity <<', 't3-capacity <<EOF', ') t3-capacity', '} fi done esac t3-capacity', 't3-capacity >',
                    "t3-capacity $'", '| t3-capacity', 't3-capacity |', 't3-capacity &>', ';;;((()))&&&|||t3-capacity',
                    'case t3-capacity in', 't3-capacity\x00|x', 'bash -c', 'eval', 'ssh t3-capacity', 'sudo -u'):
            with self.subTest(cmd):
                G.piped(cmd)   # any answer; no exception
                G.piped(cmd + ' | tail')


class Hook(unittest.TestCase):
    """The hook as Claude Code runs it: PreToolUse JSON on stdin, exit 2 + stderr refuses."""
    def hook(self, event, raw=None):
        return subprocess.run([sys.executable, '-I', HOOK], input=json.dumps(event) if raw is None else raw,
                              capture_output=True, text=True, timeout=60)

    def bash(self, command):
        return self.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Bash', 'tool_input': {'command': command}})

    def test_refused_is_exit_2_with_the_reason_on_stderr(self):
        r = self.bash('t3-capacity 2048 | tail -1 && heavy ./gradlew build')
        self.assertEqual((r.returncode, r.stdout), (2, ''))
        self.assertTrue(r.stderr.startswith('Refused by capacity-pipe-guard: '), r.stderr)
        for said in ('`t3-capacity N && <cmd>`', 'LAST command', 'Every pipe out of t3-capacity is refused',
                     'nothing needs it'):
            self.assertIn(said, r.stderr)

    def test_allowed_is_exit_0_and_silent(self):
        for cmd in ('t3-capacity 2048 && heavy ./gradlew build', 'ls -la', 'grep t3-capacity x | head', ''):
            with self.subTest(cmd):
                r = self.bash(cmd)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_a_monitor_command_is_read_like_a_bash_command(self):
        """Monitor runs shell text too, and a wait for capacity is what it is used for."""
        loop = 'until t3-capacity 2048 | tail -1; do sleep 60; done'   # timing: guard input, not a wait
        r = self.hook({'hook_event_name': 'PreToolUse', 'tool_name': 'Monitor', 'tool_input': {'command': loop, 'description': 'x'}})
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn('Refused by capacity-pipe-guard', r.stderr)
        for tool_input in ({'command': 'until t3-capacity 2048; do sleep 60; done'},   # timing: guard input, not a wait
                           {'ws': {'url': 'wss://x/t3-capacity | tail'}, 'description': 't3-capacity 1 | tail'}):
            with self.subTest(tool_input):
                r = self.hook({'tool_name': 'Monitor', 'tool_input': tool_input})
                self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_only_bash_and_monitor_commands_are_read(self):
        for tool, key in (('Write', 'content'), ('Edit', 'new_string'), ('Read', 'command')):
            with self.subTest(tool):
                r = self.hook({'tool_name': tool, 'tool_input': {key: 't3-capacity 1 | tail -1', 'command': 't3-capacity 1 | tail'}})
                self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_its_own_failure_lets_the_command_run_and_says_so(self):
        """Fail open: exit 1 is "the hook failed, run the command" for Claude Code; exit 2 would refuse."""
        for name, raw in (('no JSON', 'not json t3-capacity 1 | tail'), ('empty', ''), ('a list', '[1]'),
                          ('tool_input is text', '{"tool_name": "Bash", "tool_input": "t3-capacity 1 | tail"}')):
            with self.subTest(name):
                r = self.hook(None, raw=raw)
                self.assertEqual((r.returncode, r.stdout), (1, ''))
                self.assertTrue(r.stderr.startswith('capacity-pipe-guard: failed ('), r.stderr)
                self.assertIn('NOT checked and runs', r.stderr)

    def test_odd_but_valid_events_pass(self):
        for event in ({'tool_name': 'Bash'}, {'tool_name': 'Bash', 'tool_input': None}, {'tool_name': 'Bash', 'tool_input': {}},
                      {'tool_name': 'Bash', 'tool_input': {'command': None}}, {'tool_name': 'Bash', 'tool_input': {'command': 7}}, {}):
            with self.subTest(event):
                r = self.hook(event)
                self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_a_bug_in_the_reader_fails_open_too(self):
        event = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 't3-capacity 1 | tail'}})
        for boom in (RuntimeError('bug'), RecursionError('deep'), KeyboardInterrupt(), SystemExit(2), MemoryError()):
            with self.subTest(type(boom).__name__):
                err = io.StringIO()
                with mock.patch.object(G, 'read', side_effect=boom), mock.patch.object(sys, 'stdin', io.StringIO(event)), \
                        mock.patch.object(sys, 'stderr', err):
                    self.assertEqual(G.main([]), 1)
                self.assertIn('capacity-pipe-guard: failed (' + type(boom).__name__, err.getvalue())

    def test_a_half_written_copy_never_refuses(self):
        """State: the copy stopped half-way. Cut anywhere, it lets the command run (1 when Python cannot read it, 0
        when the cut leaves whole lines: main() is the last line), never 2."""
        tmp = tempfile.mkdtemp(prefix='capacity-pipe-')
        self.addCleanup(shutil.rmtree, tmp, True)
        with open(HOOK) as f:
            whole = f.read()
        path = os.path.join(tmp, 'capacity-pipe-guard')
        event = json.dumps({'tool_name': 'Bash', 'tool_input': {'command': 't3-capacity 1 | tail -1'}})
        cuts = [len(whole) * k // 12 for k in range(12)] + [whole.index('Rule: EVERY pipe'), len(whole) - 2]
        for cut in cuts:
            with self.subTest(cut):
                with open(path, 'w') as f:
                    f.write(whole[:cut])
                r = subprocess.run([sys.executable, '-I', path], input=event, capture_output=True, text=True, timeout=60)
                self.assertIn(r.returncode, (0, 1), r.stderr)
        self.assertEqual(r.returncode, 1, 'the last cut is inside `sys.exit(main())`')


class Usage(unittest.TestCase):
    def test_an_argument_is_a_usage_error_not_a_refusal(self):
        """--install is gone (the kit's settings entries are `agent-guards hooks`); any argument is exit 1, which
        lets the command run, never 2."""
        for args in (['--install'], ['--help'], ['x']):
            with self.subTest(args):
                r = subprocess.run([sys.executable, '-I', HOOK, *args], input='', capture_output=True, text=True,
                                   timeout=60)
                self.assertEqual(r.returncode, 1)
                self.assertIn('usage: capacity-pipe-guard', r.stderr)


class Configured(unittest.TestCase):
    """capacityPipe.commands from the machine file: every check named there is guarded, the default one only when
    it is named; a broken section keeps the default and says so."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='capacity-pipe-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def run_hook(self, cmd, section=None, text=None):
        machine = fx.machine_file(self.tmp, section, text)
        return fx.run_hook('capacity-pipe-guard', {'tool_name': 'Bash', 'tool_input': {'command': cmd}}, machine)

    def test_a_second_check_is_guarded_too(self):
        section = {'capacityPipe': {'commands': ['t3-capacity', 'mem-free']}}
        for cmd in ('mem-free 2048 | tail -1 && x', 't3-capacity 1 | tail', 'bash -c "mem-free 1 | tail"'):
            with self.subTest(cmd):
                r = self.run_hook(cmd, section)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn('Refused by capacity-pipe-guard', r.stderr)
        r = self.run_hook('mem-free 2048 | tail -1', section)
        self.assertIn('`mem-free N && <cmd>`', r.stderr)            # the message names the check that was piped
        for cmd in ('mem-free 2048 && x', 'echo "mem-free 1 | tail"', 'git commit -m "no mem-free | tail"'):
            with self.subTest(cmd):
                self.assertEqual(self.run_hook(cmd, section).returncode, 0)

    def test_a_list_without_the_default_drops_it(self):
        section = {'capacityPipe': {'commands': ['mem-free']}}
        self.assertEqual(self.run_hook('t3-capacity 1 | tail', section).returncode, 0)
        self.assertEqual(self.run_hook('mem-free 1 | tail', section).returncode, 2)

    def test_a_broken_section_keeps_the_default_and_says_so(self):
        for name, kw in (('empty list', {'section': {'capacityPipe': {'commands': []}}}),
                         ('a path, not a name', {'section': {'capacityPipe': {'commands': ['/bin/x | y']}}}),
                         ('unknown key', {'section': {'capacityPipe': {'command': ['x']}}}),
                         ('not JSON', {'text': '{"version": 1, "agent-guards": '}),
                         ('empty file', {'text': ''})):
            with self.subTest(name):
                r = self.run_hook('t3-capacity 2048 | tail -1', **kw)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn('built-in defaults used', r.stderr)
                r = self.run_hook('t3-capacity 2048 && x', **kw)
                self.assertEqual(r.returncode, 0)
                self.assertIn('built-in defaults used', r.stderr)

    def test_no_machine_file_or_no_section_is_the_default_quietly(self):
        for kw in ({}, {'section': None}):
            r = fx.run_hook('capacity-pipe-guard', {'tool_name': 'Bash', 'tool_input': {'command': 't3-capacity | x'}})
            self.assertEqual((r.returncode, r.stdout), (2, ''))
            self.assertNotIn('defaults', r.stderr)
            r = self.run_hook('ls', **kw)
            self.assertEqual((r.returncode, r.stderr), (0, ''))

    def test_a_machine_file_others_can_write_is_not_used(self):
        r = self.run_hook('mem-free 1 | tail', {'capacityPipe': {'commands': ['mem-free']}})
        self.assertEqual(r.returncode, 2)
        os.chmod(os.path.join(self.tmp, 'machine.json'), 0o666)
        r = fx.run_hook('capacity-pipe-guard', {'tool_name': 'Bash', 'tool_input': {'command': 'mem-free 1 | tail'}},
                        os.path.join(self.tmp, 'machine.json'))
        self.assertEqual(r.returncode, 0)                            # the default list: mem-free is not a check
        self.assertIn('built-in defaults used', r.stderr)


if __name__ == '__main__':
    unittest.main()
