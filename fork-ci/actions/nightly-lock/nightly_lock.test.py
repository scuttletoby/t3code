#!/usr/bin/env python3
"""The nightly lock (nightly_lock.py and its action): long and short heavy steps on one shared CI machine never run at
the same time, the --first caller goes first (it is never overtaken by a caller that waits), a cancelled or killed
holder frees the lock at once, a wait has an end and says who held the lock, and old, empty or broken state is met.
Real processes and real flocks in a temporary lock folder (--dir), never the machine's /tmp. Ported from a project's
tests of the same tool.
Run: python3 fork-ci/actions/nightly-lock/nightly_lock.test.py
"""
import fcntl, os, re, shutil, signal, subprocess, sys, tempfile, threading, time, unittest

import yaml

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, 'nightly_lock.py')
ACTION = os.path.join(HERE, 'action.yml')
DEV = HERE
LOCK, WANT, HOLDER = 'nightly.lock', 'nightly-first.lock', 'nightly.holder'
PROC_READS = 5   # /proc/locks is handed out one page at a time: a read can skip a lock held the whole time


def lock_lines(p):
    """The /proc/locks lines about the file at p, each once, as its words without the leading count ([] when p is not
    there); None when /proc/locks cannot be read. Several reads: one can skip a line (see PROC_READS)."""
    try:
        st = os.stat(p)
    except FileNotFoundError:
        return []
    key = f'{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}'
    seen = {}
    for _ in range(PROC_READS):
        try:
            with open('/proc/locks') as f:
                lines = f.read().splitlines()
        except OSError:
            return None
        for words in (line.split() for line in lines):
            if key in words:
                seen[tuple(words[1:])] = None
    return list(seen)


# The command a holder runs: writes "start NAME" to the log, waits until the file NAME.go exists, writes "end NAME".
JOB = '''
import os, sys, time
log, name, folder = sys.argv[1:4]
open(log, 'a').write(f'start {name}\\n')
while not os.path.exists(os.path.join(folder, name + '.go')):
    if not os.path.isdir(folder):   # the test is gone (its temp folder removed): never spin on as an orphan
        sys.exit(3)
    time.sleep(0.02)
open(log, 'a').write(f'end {name}\\n')
'''

# Runs the tool's main() in a Python that has already started and loaded the tool, with each argument SOON made "now +
# 1.5 s" only then: the 1.5 s counts from the tool's own start, so a slow Python start-up cannot use it up.
SOON_RUNNER = '''
import sys, time
sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder
sys.path.insert(0, sys.argv[1])
import nightly_lock
soon = str(time.time() + 1.5)   # timing: the tool's own --until limit under test
sys.exit(nightly_lock.main([soon if a == 'SOON' else a for a in sys.argv[2:]]))
'''


def wait_for(cond, seconds=60):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def workflow(name):
    with open(os.path.join(WORKFLOWS, name)) as f:
        return yaml.safe_load(f)


def read(path):
    with open(path) as f:
        return f.read()


def steps(wf, job):
    return [s.get('run', '') for s in workflow(wf)['jobs'][job]['steps']]


def alive(pid):
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


def parent(pid):
    """The parent pid of a live process, or None."""
    try:
        with open(f'/proc/{pid}/stat') as f:
            return int(f.read().rsplit(')', 1)[1].split()[1])
    except (OSError, IndexError, ValueError):
        return None


def kill_later(case, pid):
    case.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if alive(pid) else None)


def stop_holder(p):
    """Stops a lock holder the way a cancelled job does: SIGTERM, which it passes to its command and waits for. A SIGKILL
    would end only the holder and leave its command (own process group) running after the test."""
    p.terminate()
    try:
        p.wait(60)
    except subprocess.TimeoutExpired:
        p.kill()
        p.wait()


def free(locks, name=LOCK):
    """Could another caller take this lock now (LOCK_EX, not blocking)?"""
    try:
        fd = os.open(os.path.join(locks, name), os.O_RDONLY)
    except FileNotFoundError:
        return True
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class Base(unittest.TestCase):
    """A temporary lock folder and helpers; no tests of its own."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='nightly-lock-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.locks = os.path.join(self.d, 'locks')   # not made: the tool makes it (empty state)
        self.log = os.path.join(self.d, 'log')
        self.procs = []
        self.addCleanup(self.stop_all)

    def env(self, **extra):
        return dict(os.environ, NIGHTLY_LOCK_POLL_S='0.05', **extra)

    def stop_all(self):
        for p in self.procs:
            if p.poll() is None:
                p.kill()
            p.wait()
            for f in (p.stdout, p.stderr):
                if f:
                    f.close()

    def start(self, name, *args, cmd=None, env=None, tool=(TOOL,)):
        cmd = cmd or [sys.executable, '-c', JOB, self.log, name, self.d]
        p = subprocess.Popen([sys.executable, '-I', *tool, '--dir', self.locks, *args, '--', *cmd], env=env or self.env(),
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def go(self, name):
        open(os.path.join(self.d, name + '.go'), 'w').close()

    def lines(self):
        try:
            with open(self.log) as f:
                return f.read().split('\n')[:-1]
        except FileNotFoundError:
            return []

    def started(self, name):
        return wait_for(lambda: f'start {name}' in self.lines())

    def holds_want(self, p):
        """Does the --first caller p hold WANT shared, the mark that keeps every caller without --first out?"""
        lines = lock_lines(os.path.join(self.locks, WANT)) or []
        return any(w[:3] == ('FLOCK', 'ADVISORY', 'READ') and w[3] == str(p.pid) for w in lines)   # not '->': waiting



class Lock(Base):
    def test_two_holders_never_overlap(self):
        a = self.start('a', '--wait', '30')
        self.assertTrue(self.started('a'))
        b = self.start('b', '--wait', '30')
        time.sleep(0.5)
        self.assertNotIn('start b', self.lines())   # b waits while a runs
        self.go('a')
        self.assertTrue(self.started('b'))
        self.go('b')
        self.assertEqual((a.wait(60), b.wait(60)), (0, 0))
        self.assertEqual(self.lines(), ['start a', 'end a', 'start b', 'end b'])

    def test_the_soak_goes_before_a_mutation_step_that_waited_longer(self):
        hold = self.start('hold', '--wait', '30')            # the mutation's Python step runs
        self.assertTrue(self.started('hold'))
        java = self.start('java', '--wait', '30')            # the Java step waits first ...
        self.assertIn('waiting for the nightly lock', java.stderr.readline())   # its first line: it waits now
        soak = self.start('soak', '--first', '--wait', '30')   # ... the soak comes later
        self.assertTrue(wait_for(lambda: self.holds_want(soak)))   # it waits now, ahead of java
        self.go('hold')
        self.assertTrue(self.started('soak'))
        time.sleep(0.5)
        self.assertNotIn('start java', self.lines())         # never beside the soak, never before it
        self.go('soak')
        self.assertTrue(self.started('java'))
        self.go('java')
        self.assertEqual([p.wait(60) for p in (hold, soak, java)], [0, 0, 0])
        self.assertEqual(self.lines(), ['start hold', 'end hold', 'start soak', 'end soak', 'start java', 'end java'])
        self.assertIn('took the nightly lock', java.stderr.read())

    def test_a_soak_waits_only_for_the_step_that_holds_the_lock(self):
        java = self.start('java', '--wait', '30')
        self.assertTrue(self.started('java'))
        soak = self.start('soak', '--first', '--wait', '30')
        time.sleep(0.3)
        self.assertNotIn('start soak', self.lines())
        self.go('java')
        self.assertTrue(self.started('soak'))
        self.go('soak')
        self.assertEqual((java.wait(60), soak.wait(60)), (0, 0))
        self.assertIn('took the nightly lock', soak.stderr.read())

    def test_a_killed_holder_frees_the_lock_even_when_its_child_lives_on(self):   # runner died / job cancelled hard
        child_pid = os.path.join(self.d, 'child.pid')
        orphan = [sys.executable, '-c', 'import os, subprocess, sys, time\n'
                  f'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
                  f'open({child_pid!r}, "w").write(str(p.pid))\np.wait()']   # ends when the cleanup kills p
        a = self.start('a', '--wait', '30', cmd=orphan)
        self.assertTrue(wait_for(lambda: os.path.exists(child_pid) and read(child_pid)))
        pid = int(read(child_pid))
        self.addCleanup(lambda: os.kill(pid, signal.SIGKILL) if os.path.exists(f'/proc/{pid}') else None)
        a.kill()   # SIGKILL: no cleanup code runs
        a.wait()
        self.assertTrue(os.path.exists(f'/proc/{pid}'))   # the grandchild still runs ...
        b = self.start('b', '--wait', '5')
        self.assertTrue(self.started('b'))                 # ... and does not hold the lock
        self.go('b')
        self.assertEqual(b.wait(60), 0)

    def test_sigterm_reaches_the_command_and_frees_the_lock(self):   # a cancelled job: the runner sends SIGTERM/SIGINT
        mark = os.path.join(self.d, 'got-term')
        trap = [sys.executable, '-c', 'import signal, sys, time\n'
                f'signal.signal(signal.SIGTERM, lambda *a: (open({mark!r}, "w").close(), sys.exit(9)))\n'
                f'open({self.log!r}, "a").write("start t\\n")\ntime.sleep(60)']
        t = self.start('t', '--wait', '30', cmd=trap)
        self.assertTrue(self.started('t'))
        t.send_signal(signal.SIGTERM)
        self.assertEqual(t.wait(60), 9)   # the command's own exit code
        self.assertTrue(os.path.exists(mark))
        b = self.start('b', '--wait', '5')
        self.assertTrue(self.started('b'))
        self.go('b')

    def test_a_wait_ends_names_the_holder_and_runs_nothing(self):
        a = self.start('a', '--wait', '30', env=self.env(GITHUB_RUN_ID='4242', GITHUB_WORKFLOW='mutation', GITHUB_JOB='mutation'))
        self.assertTrue(self.started('a'))
        for args in (('--wait', '0.3'), ('--first', '--wait', '0.3'), ('--until', 'SOON'), ('--first', '--until', 'SOON')):
            with self.subTest(args):
                p = self.start('never', *args, tool=('-c', SOON_RUNNER, DEV))   # SOON: 1.5 s after the tool started
                self.assertEqual(p.wait(60), 75)
                err = p.stderr.read()
                self.assertIn('lock held by mutation / mutation run 4242 since', err)
                self.assertIn('did not run', err)
        self.assertNotIn('start never', self.lines())
        self.go('a')
        self.assertEqual(a.wait(60), 0)

    def test_runs_again_on_old_or_empty_state(self):
        os.makedirs(self.locks)
        with open(os.path.join(self.locks, HOLDER), 'w') as f:
            f.write('soak / soak run 1 since 09:00 UTC\n')   # left by a job that was killed: only a note
        for name in ('one', 'two'):   # a second run after the first: the lock is free again at once
            self.go(name)
            p = self.start(name, '--wait', '0')
            self.assertEqual(p.wait(60), 0, p.stderr.read())
        for f in (LOCK, WANT):
            os.unlink(os.path.join(self.locks, f))   # removed between runs: made again
        self.go('three')
        self.assertEqual(self.start('three', '--first', '--wait', '0').wait(60), 0)
        self.assertEqual(self.start('rc', '--wait', '0', cmd=['sh', '-c', 'exit 3']).wait(60), 3)   # its exit code
        note = read(os.path.join(self.locks, HOLDER))   # rewritten by the last holder
        self.assertNotIn('soak run 1', note)
        self.assertIn(' since ', note)

    def test_until_already_past_runs_nothing_even_when_the_lock_is_free(self):
        self.go('late')
        for args in (('--until', str(time.time() - 5)), ('--first', '--until', str(time.time() - 5))):
            with self.subTest(args):
                p = self.start('late', *args)
                self.assertEqual(p.wait(60), 75)
                err = p.stderr.read()
                self.assertIn('has passed', err)
                self.assertIn('did not run', err)
        self.assertNotIn('start late', self.lines())

    def test_the_gave_up_note_says_why_and_an_old_one_never_stays(self):   # the soak-red issue reads it
        note = os.path.join(self.d, 'note')
        a = self.start('a', '--wait', '30', env=self.env(GITHUB_RUN_ID='77', GITHUB_WORKFLOW='mutation', GITHUB_JOB='mutation'))
        self.assertTrue(self.started('a'))
        p = self.start('never', '--first', '--wait', '0.2', '--gave-up-note', note)
        self.assertEqual(p.wait(60), 75)
        self.assertRegex(read(note), r'^lock held by mutation / mutation run 77 since .*\(gave up after 0 min\)\n$')
        self.go('a')
        self.assertEqual(a.wait(60), 0)
        self.go('b')
        self.assertEqual(self.start('b', '--first', '--wait', '5', '--gave-up-note', note).wait(60), 0)
        self.assertFalse(os.path.exists(note))   # a run that ran leaves no note of an earlier one

    def stderr_lines(self, p):
        """p's stderr lines as they come (a reader thread): a test waits on what the tool says, not on a clock."""
        out = []
        def pump():
            for line in p.stderr:
                out.append(line)
        t = threading.Thread(target=pump, daemon=True)
        t.start()
        self.addCleanup(t.join, 60)
        return out

    def hold_want(self):
        """Holds WANT shared, exactly what a waiting --first caller holds; LOCK itself stays free. The fd."""
        os.makedirs(self.locks, exist_ok=True)
        fd = os.open(os.path.join(self.locks, WANT), os.O_RDONLY | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_SH)
        return fd

    def test_a_waiting_soak_keeps_a_mutation_step_out_of_a_free_lock(self):
        """Is a caller without --first kept out of a FREE lock while a --first caller waits, and let in once it no
        longer waits? The test starts counting only once the tool says it has looked and seen the waiting --first
        caller (a real event, not a deadline set before the tool ran), so a slow machine cannot skip the case."""
        fd = self.hold_want()
        try:
            p = self.start('step', '--wait', '120')
            err = self.stderr_lines(p)
            self.assertTrue(wait_for(lambda: any('waiting for the nightly lock: a --first caller is waiting' in line
                                                 for line in err), 120), ''.join(err))
            self.assertTrue(free(self.locks), 'the case under test: LOCK itself is free')
            end = time.monotonic() + 0.5   # timing: probe window, several of the tool's looks (POLL_S 0.05)
            while time.monotonic() < end:
                self.assertNotIn('start step', self.lines(), 'took a free lock while a --first caller waits')
                self.assertIsNone(p.poll())
                time.sleep(0.02)
        finally:
            os.close(fd)   # the --first caller no longer waits
        self.assertTrue(self.started('step'), 'not let in once no --first caller waits: was it looking at all?')
        self.go('step')
        self.assertEqual(p.wait(60), 0)

    def test_a_step_kept_out_by_a_waiting_soak_gives_up_at_its_own_limit_and_says_why(self):
        """At its time limit, does the caller give up (exit 75, nothing run) naming the --first caller it saw at its
        last look? The reason comes from a look the tool made, so a run that never looked (the case skipped on a slow
        machine) cannot pass: it would say "has passed" or name no --first caller."""
        fd = self.hold_want()
        try:
            for args in (('--wait', '0.5'), ('--until', 'SOON')):   # timing: the tool's own limit under test
                with self.subTest(args):
                    p = self.start('never', *args, tool=('-c', SOON_RUNNER, DEV))   # SOON: 1.5 s after the tool started
                    try:
                        self.assertEqual(p.wait(60), 75)
                    except subprocess.TimeoutExpired:
                        self.fail('a mutation step waited past its time limit while a soak waited')
                    err = p.stderr.read()
                    self.assertRegex(err, r'a --first caller is waiting for the lock; it goes first \(gave up after '
                                          r'0 min\); did not run', 'the give-up line must come from a look that saw '
                                          'the waiting --first caller')
                    self.assertNotIn('has passed', err)
        finally:
            os.close(fd)
        self.assertNotIn('start never', self.lines())

    def test_the_soak_keeps_its_waiting_mark_until_it_holds_the_lock(self):
        hold = self.start('hold', '--wait', '30')
        self.assertTrue(self.started('hold'))
        soak = self.start('soak', '--first', '--wait', '30')
        self.assertTrue(wait_for(lambda: not free(self.locks, WANT)))
        end = time.monotonic() + 0.6   # timing: probe window: the mark must hold for all of it
        while time.monotonic() < end:   # the whole wait, not only its start
            self.assertFalse(free(self.locks, WANT), 'the soak let go of its mark while it waits')
            time.sleep(0.02)
        self.go('hold')
        self.assertTrue(self.started('soak'))
        self.assertTrue(wait_for(lambda: free(self.locks, WANT)))   # holding: no mark any more
        self.go('soak')
        self.assertEqual((hold.wait(60), soak.wait(60)), (0, 0))

    def test_another_plain_caller_s_look_is_not_taken_for_a_waiting_first_caller(self):
        """Two callers without --first look at the same moment: the one that finds the waiting mark taken (held
        exclusive, for the moment of the other's look) must not say a --first caller is waiting, and takes the free
        lock once that look is over. The test holds the mark exclusive as such a look does, and lets go only once the
        tool has met it (it waits in the kernel, or it has already said why it waits)."""
        os.makedirs(self.locks, exist_ok=True)
        fd = os.open(os.path.join(self.locks, WANT), os.O_RDONLY | os.O_CREAT, 0o666)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            p = self.start('step', '--wait', '30')
            err = self.stderr_lines(p)

            def met():
                lines = lock_lines(os.path.join(self.locks, WANT)) or []
                return any(w[:1] == ('->',) and str(p.pid) in w for w in lines) or err
            self.assertTrue(wait_for(met, 60), 'the tool never looked at the waiting mark')
        finally:
            os.close(fd)   # the other caller's look is over
        self.assertTrue(self.started('step'), ''.join(err))
        self.go('step')
        self.assertEqual(p.wait(60), 0)
        self.assertFalse([line for line in err if 'a --first caller is waiting' in line], ''.join(err))

    def test_a_command_killed_by_a_signal_exits_128_plus_n(self):
        self.assertEqual(self.start('k', '--wait', '1', cmd=['sh', '-c', 'kill -TERM $$']).wait(60), 143)

    # The workflows run `nightly_lock.py -- bash -ec '<several commands>'`: bash does not exec its last command, and a
    # signal that ends bash leaves its child. The lock must stay held until that child (and its own sessions) ended.
    def bash_job(self, body, *args, env=None):
        p = self.start('bash', '--wait', '5', *args, cmd=['bash', '-ec', body + '\necho next'], env=env)
        return p

    def pid_from(self, name):
        f = os.path.join(self.d, name)
        self.assertTrue(wait_for(lambda: os.path.exists(f) and read(f).strip()))
        pid = int(read(f))
        kill_later(self, pid)
        return pid

    def test_sigterm_to_the_workflows_bash_shape_ends_the_heavy_child_before_the_lock_is_free(self):
        pidf = os.path.join(self.d, 'heavy.pid')
        a = self.bash_job(f'{sys.executable} -c "import os, time; open({pidf!r}, \'w\').write(str(os.getpid())); time.sleep(30)"')
        heavy = self.pid_from('heavy.pid')
        a.send_signal(signal.SIGTERM)   # the job is cancelled
        self.assertEqual(a.wait(60), 143)
        self.assertFalse(alive(heavy), 'the lock was let go while the heavy child of bash still runs')
        self.assertTrue(free(self.locks))

    def test_a_child_that_cleans_up_on_sigterm_keeps_the_lock_until_it_ends(self):   # mutate_java stops PIT and its DB
        pidf, done, go = (os.path.join(self.d, n) for n in ('slow.pid', 'cleaned', 'cleanup.go'))
        # The child's clean-up lasts until the test writes cleanup.go, not a set time: the second caller below always
        # comes while it runs, however slow the box is (#2018).
        slow = (f'import os, signal, sys, time\nstop = []\nsignal.signal(signal.SIGTERM, lambda *a: stop.append(1))\n'
                f'open({pidf!r}, "w").write(str(os.getpid()))\nwhile not stop: time.sleep(0.02)\n'
                f'while not os.path.exists({go!r}):\n'
                f'    if not os.path.isdir({self.d!r}):   # the test is gone: never spin on as an orphan\n'
                f'        sys.exit(3)\n'
                f'    time.sleep(0.02)\n'
                f'open({done!r}, "w").close()')
        script = os.path.join(self.d, 'slow.py')
        with open(script, 'w') as f:
            f.write(slow)
        # Grace far beyond the waits: the wrapper's SIGKILL never ends the clean-up the test is still holding open.
        a = self.bash_job(f'{sys.executable} {script}', env=self.env(NIGHTLY_LOCK_GRACE_S='120'))
        child = self.pid_from('slow.pid')
        bash = parent(child)
        self.assertNotIn(bash, (None, a.pid))   # bash runs the child (it does not exec it)
        a.send_signal(signal.SIGTERM)
        # The real event: bash has ended (its child got a new parent); the child cleans up until cleanup.go.
        self.assertTrue(wait_for(lambda: parent(child) != bash), 'bash did not end on SIGTERM')
        b = self.start('b', '--wait', '0.2', cmd=['true'])   # took the lock wrongly: exit 0 at once, never a hang
        self.assertEqual(b.wait(60), 75, 'a second holder took the lock while the first holder\'s child cleans up')
        self.assertTrue(alive(child) and not os.path.exists(done))   # b really came during the clean-up
        open(go, 'w').close()
        self.assertEqual(a.wait(60), 143)
        self.assertTrue(os.path.exists(done))   # it was let finish, not killed
        self.go('c')
        self.assertEqual(self.start('c', '--wait', '1').wait(60), 0)

    def test_a_grandchild_in_its_own_session_ends_before_the_lock_is_free(self):   # mutate_java starts PIT that way
        pidf = os.path.join(self.d, 'pit.pid')
        script = os.path.join(self.d, 'spawn.py')
        with open(script, 'w') as f:
            f.write(f'import subprocess, sys, time\n'
                    f'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], start_new_session=True)\n'
                    f'open({pidf!r}, "w").write(str(p.pid))\ntime.sleep(60)')
        a = self.bash_job(f'{sys.executable} {script}')
        pit = self.pid_from('pit.pid')
        a.send_signal(signal.SIGTERM)
        self.assertEqual(a.wait(60), 143)
        self.assertFalse(alive(pit), 'a process the command started in its own session outlived the lock')

    def test_what_the_command_leaves_running_is_stopped_before_the_lock_is_free(self):
        pidf = os.path.join(self.d, 'left.pid')
        # The first grace is far beyond the waits, so a cleanup that skips SIGTERM (SIGKILL only after the grace)
        # overruns a.wait(60); the second ceiling tells the 0.5 s grace from the 30 s default (env ignored).
        for name, body, grace, ceiling in (
                ('stops on SIGTERM', f'sleep 60 & echo $! > {pidf}', '120', 45),
                ('ignores SIGTERM: SIGKILL after the grace time',
                 f'{sys.executable} -c "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)" & '
                 f'echo $! > {pidf}; sleep 0.3', '0.5', 20)):
            with self.subTest(name):
                if os.path.exists(pidf):
                    os.unlink(pidf)
                t = time.monotonic()
                a = self.bash_job(body, env=self.env(NIGHTLY_LOCK_GRACE_S=grace))
                self.assertEqual(a.wait(60), 0)   # the command's own exit code
                left = self.pid_from('left.pid')
                self.assertFalse(alive(left))
                self.assertLess(time.monotonic() - t, ceiling)   # timing: see the loop's note
                self.assertIn('still run', a.stderr.read())
                self.assertTrue(free(self.locks))

    def test_orphans_that_end_are_collected_while_the_command_still_runs(self):
        # A mutation night runs each covering test file per planted bug; a test killed by its timeout leaves orphans,
        # which come to the wrapper (subreaper). Collected only at the end, thousands wait as zombies and count against
        # the runner's process limit (quick check on #1595).
        script = os.path.join(self.d, 'orphans.py')
        with open(script, 'w') as f:
            f.write('import os, subprocess, time\n'
                    'for _ in range(200):\n'
                    '    subprocess.run(["sh", "-c", "sleep 0.01 &"])\n'
                    'time.sleep(2)\n'
                    'z = 0\n'
                    'for n in os.listdir("/proc"):\n'
                    '    if n.isdigit():\n'
                    '        try:\n'
                    '            rest = open(f"/proc/{n}/stat").read().rsplit(")", 1)[1].split()\n'
                    '        except OSError:\n'
                    '            continue\n'
                    '        if rest[0] == "Z" and int(rest[1]) == os.getppid():\n'
                    '            z += 1\n'
                    'print(z)\n'
                    'raise SystemExit(7)\n')
        p = subprocess.run([sys.executable, '-I', TOOL, '--dir', self.locks, '--wait', '10', '--', sys.executable, '-I',
                            script],
                           env=self.env(NIGHTLY_LOCK_GRACE_S='2'), capture_output=True, text=True, timeout=120)
        self.assertEqual(p.returncode, 7, p.stderr)   # the command's own exit code still comes through
        zombies = int(p.stdout.split()[-1])
        self.assertLess(zombies, 10, f'{zombies} ended orphans wait as zombies under the wrapper while the command runs')
        self.assertTrue(free(self.locks))

    def test_bad_arguments(self):
        for args in ((), ('--wait', '1'), ('--wait', '1', '--until', '2', '--', 'true')):
            p = subprocess.run([sys.executable, '-I', TOOL, '--dir', self.locks, *args], env=self.env(),
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, args)
        for args in (('--name', '../x'), ('--name', ''), ('--name', '.hidden'), ('--dir', '')):
            p = subprocess.run([sys.executable, '-I', TOOL, *args, '--wait', '0', '--', 'true'], env=self.env(),
                               capture_output=True, text=True)
            self.assertEqual(p.returncode, 2, args)
        for flag in ('--wait', '--until'):   # a limit that never runs out (nan, inf) or makes no sense: exit 2, nothing run
            for value in ('nan', 'inf', '-inf', '-1', '1e12'):
                with self.subTest(flag=flag, value=value):
                    p = subprocess.run([sys.executable, '-I', TOOL, '--dir', self.locks, flag, value, '--', 'sh', '-c',
                                        f'echo ran >> {self.log}'], env=self.env(), capture_output=True, text=True,
                                       timeout=60)
                    self.assertEqual(p.returncode, 2, p.stderr)
                    self.assertNotIn('Traceback', p.stderr)
        self.assertEqual(self.lines(), [])


class Action(Base):
    """The action's real step, run with bash as the runner runs it (inputs by env, GITHUB_ACTION_PATH, $GITHUB_OUTPUT)."""
    def step(self):
        with open(ACTION, encoding='utf-8') as f:
            (step,) = yaml.safe_load(f)['runs']['steps']
        return step

    def run_step(self, run, first='false', wait='5', until='', name='nightly', background=False):
        out = os.path.join(self.d, f'output-{len(self.procs)}')
        open(out, 'w').close()
        env = {k: v for k, v in self.env().items() if not k.startswith('GITHUB_')}
        env.update(GITHUB_ACTION_PATH=HERE, GITHUB_OUTPUT=out, RUN=run, FIRST=first, WAIT=wait, UNTIL=until,
                   NAME=name, DIR=self.locks)
        p = subprocess.Popen(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', self.step()['run']], env=env,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=self.d)
        self.procs.append(p)
        return p, out

    def test_the_step_execs_the_tool_with_inputs_by_env_only(self):
        """Is the tool the step's own process (so the runner's cancel signal reaches it) and no input pasted in?"""
        run = self.step()['run']
        self.assertIn('exec python3 -I "$GITHUB_ACTION_PATH/nightly_lock.py"', run)
        self.assertNotIn('${{', run)

    def test_the_command_runs_with_its_exit_code_and_a_give_up_is_an_output(self):
        """Does the step run the script inside the lock with its exit code, and on a give-up exit 75 with gave_up?"""
        p, out = self.run_step(f'echo ran >> {self.log}; exit 4')
        self.assertEqual(p.wait(60), 4, p.stderr.read())
        self.assertEqual(self.lines(), ['ran'])
        self.assertEqual(read(out), '', 'gave_up is set only when nothing ran')
        hold = self.start('hold', '--wait', '30', env=self.env(GITHUB_RUN_ID='5', GITHUB_WORKFLOW='w', GITHUB_JOB='j'))
        self.assertTrue(self.started('hold'))
        p, out = self.run_step(f'echo never >> {self.log}', first='true', wait='0.3')
        self.assertEqual(p.wait(60), 75)
        self.assertRegex(read(out), r'^gave_up=lock held by w / j run 5 since .*\(gave up after 0 min\)\n$')
        self.go('hold')
        self.assertEqual(hold.wait(60), 0)
        self.assertNotIn('never', self.lines())

    def test_a_cancel_signal_to_the_step_ends_the_script_before_the_lock_is_free(self):
        """The runner signals the step's process: does the script's child end before the lock is let go?"""
        pidf = os.path.join(self.d, 'child.pid')
        p, _ = self.run_step(f'sleep 60 & echo $! > {pidf}; wait', wait='5')
        self.assertTrue(wait_for(lambda: os.path.exists(pidf) and read(pidf).strip()))
        child = int(read(pidf))
        kill_later(self, child)
        p.send_signal(signal.SIGTERM)
        self.assertEqual(p.wait(60), 143)
        self.assertFalse(alive(child))
        self.assertTrue(free(self.locks))

    def test_bad_inputs_run_nothing(self):
        for kw in ({'first': 'yes'}, {'wait': '', 'until': ''}, {'wait': '1', 'until': '2'}, {'name': '../x'},
                   {'wait': 'nan'}, {'wait': '', 'until': 'inf'}):
            with self.subTest(kw):
                p, _ = self.run_step(f'echo ran >> {self.log}', **kw)
                self.assertEqual(p.wait(60), 2, kw)
        self.assertEqual(self.lines(), [])


class State(Base):
    """Old, empty or broken state, two locks at once, and a run killed while it holds the lock."""
    def test_lock_files_an_older_version_left_are_used_as_they_are(self):
        """Lock files with content, read-only, and a holder note that is not text: is the lock still taken?"""
        os.makedirs(self.locks)
        for name, data in ((LOCK, b'old pid 123\n'), (WANT, b'x' * 5000), (HOLDER, b'\xff\xfe\x00 not text')):
            with open(os.path.join(self.locks, name), 'wb') as f:
                f.write(data)
            os.chmod(os.path.join(self.locks, name), 0o444)
        self.go('one')
        p = self.start('one', '--wait', '1')
        self.assertEqual(p.wait(60), 0, p.stderr.read())
        hold = self.start('hold', '--wait', '30')
        self.assertTrue(self.started('hold'))
        q = self.start('two', '--wait', '0.2')
        self.assertEqual(q.wait(60), 75)
        self.assertIn('lock held by', q.stderr.read())
        self.go('hold')
        self.assertEqual(hold.wait(60), 0)

    def test_a_note_that_is_not_text_while_the_lock_is_held_is_shown_never_a_crash(self):
        """The lock is held and its holder note is not text (an older version's, a broken write): does a caller that
        waits, with or without --first, give up with 75 naming the holder as best it can, never a traceback?"""
        hold = self.start('hold', '--wait', '30')
        self.assertTrue(self.started('hold'))
        with open(os.path.join(self.locks, HOLDER), 'wb') as f:
            f.write(b'\xff\xfe\x00 not text\nsecond=line')
        note = os.path.join(self.d, 'note')
        for args in (('--wait', '0.2'), ('--first', '--wait', '0.2')):
            with self.subTest(args):
                p = self.start('never', *args, '--gave-up-note', note)
                self.assertEqual(p.wait(60), 75)
                err = p.stderr.read()
                self.assertNotIn('Traceback', err)
                self.assertIn('lock held by', err)
                self.assertIn('not text', err)
                self.assertRegex(read(note), r'^lock held by [^\n\x00]*not text[^\n\x00]*\n$')   # one line
        self.assertNotIn('start never', self.lines())
        self.go('hold')
        self.assertEqual(hold.wait(60), 0)

    def test_a_project_s_old_lock_name_still_excludes_its_old_copy(self):
        """A project moving from its own copy passes its old --name: the copy's lock and this one are one lock."""
        hold = self.start('hold', '--name', 'project-nightly', '--wait', '30')
        self.assertTrue(self.started('hold'))
        self.assertFalse(free(self.locks, 'project-nightly.lock'))
        q = self.start('same', '--name', 'project-nightly', '--wait', '0.2')
        self.assertEqual(q.wait(60), 75)
        self.go('other')
        self.assertEqual(self.start('other', '--name', 'another', '--wait', '1').wait(60), 0, 'another name: no wait')
        self.go('hold')
        self.assertEqual(hold.wait(60), 0)

    def test_a_lock_that_cannot_be_used_runs_nothing(self):
        """A lock folder that is a file or a broken link: exit 71 with one line, never a traceback, nothing run."""
        os.makedirs(self.d, exist_ok=True)
        for make in (lambda p: open(p, 'w').close(), lambda p: os.symlink(os.path.join(self.d, 'gone', 'x'), p)):
            with self.subTest(make):
                if os.path.lexists(self.locks):
                    os.remove(self.locks)
                make(self.locks)
                self.go('never')
                p = self.start('never', '--wait', '1')
                self.assertEqual(p.wait(60), 71)
                err = p.stderr.read()
                self.assertIn('cannot use the lock', err)
                self.assertNotIn('Traceback', err)
        self.assertNotIn('start never', self.lines())

    def test_a_holder_killed_mid_run_leaves_a_note_that_decides_nothing(self):
        """Killed while it holds the lock (SIGKILL: no clean-up): the next caller takes it at once, and the dead
        holder's note is replaced, never trusted."""
        a = self.start('a', '--wait', '30', env=self.env(GITHUB_RUN_ID='1', GITHUB_WORKFLOW='w', GITHUB_JOB='dead'))
        self.assertTrue(self.started('a'))
        a.kill()
        a.wait()
        self.assertIn('dead run 1', read(os.path.join(self.locks, HOLDER)))
        self.go('b')
        self.assertEqual(self.start('b', '--wait', '0').wait(60), 0)
        self.assertNotIn('dead run 1', read(os.path.join(self.locks, HOLDER)))
        self.go('a')   # the killed holder's command (its own process group) ends too


if __name__ == '__main__':
    unittest.main()
