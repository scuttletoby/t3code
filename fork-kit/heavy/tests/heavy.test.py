"""heavy with a real systemd user manager: leftovers, exit codes, signals, HEAVY_KEEP, nesting, refusals.

  python3 -I -B fork-kit/heavy/tests/heavy.test.py

Needs a systemd user manager and a slice with a memory limit (HEAVY_TEST_SLICE, default heavy.slice). Without one
every test is skipped, unless HEAVY_REAL_TESTS=1 (CI: .github/workflows/fork-tests.yml), where that fails.
HEAVY_TEST_BIN runs the suite against another wrapper: with tests/old-heavy.sh (the wrapper before this kit) every
leftover test fails, which is the red check. Every test stops what it started: processes are found by their
temporary folder in the command line, scopes by those processes.
"""
# How CI runs it (.github/scripts/fork-ci-tests.cjs, which records what it ran; fork-ci-result checks that):
# fork-ci: run with HEAVY_REAL_TESTS=1
import json, os, re, shutil, signal, subprocess, sys, tempfile, time, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_TOOLS = os.path.join(KIT, 'tests', 'fake_tools.py')
LABEL = 'fork-kit.heavy.owner'
HEAVY = os.environ.get('HEAVY_TEST_BIN') or os.path.join(KIT, 'bin', 'heavy')
OLD = os.path.join(KIT, 'tests', 'old-heavy.sh')
WRAPPERS = {os.path.realpath(HEAVY), os.path.realpath(OLD)}
SLICE = os.environ.get('HEAVY_TEST_SLICE') or 'heavy.slice'
REQUIRED = os.environ.get('HEAVY_REAL_TESTS') == '1'


def manager_problem():
    try:
        r = subprocess.run(['systemctl', '--user', 'show', SLICE, '-p', 'LoadState,FragmentPath,MemoryMax,MemoryHigh'],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        return str(e)
    if r.returncode != 0:
        return r.stderr.strip() or 'systemctl --user failed'
    p = dict(line.partition('=')[::2] for line in r.stdout.splitlines())
    if not p.get('FragmentPath') or (p.get('MemoryMax') == 'infinity' and p.get('MemoryHigh') == 'infinity'):
        return f'{SLICE} has no unit file with a memory limit'
    return None


def nested_problem():
    """Run inside a heavy scope (`heavy python3 heavy.test.py`), the heavy under test would run nested (no scope of
    its own) and the cleanup would stop the outer scope, this test run with it. Each command the tests run gets a
    heavy scope of its own: run the file itself without heavy."""
    try:
        with open('/proc/self/cgroup') as f:
            leaf = f.read().strip().rpartition('/')[2]
    except OSError:
        return None
    if leaf.startswith('heavy-') and leaf.endswith('.scope'):
        return f'runs inside the heavy scope {leaf}: run this file without heavy (every command it runs gets its own)'
    return None


PROBLEM = nested_problem() or manager_problem()


def comm(pid):
    try:
        with open(f'/proc/{pid}/comm') as f:
            return f.read().strip()
    except OSError:
        return None


def wait_for(cond, timeout=15.0, what='condition'):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return
        time.sleep(0.05)
    raise AssertionError(f'timed out waiting for {what}')


@unittest.skipIf(PROBLEM and not REQUIRED, f'no usable systemd user manager: {PROBLEM}')
class Heavy(unittest.TestCase):
    def setUp(self):
        if PROBLEM:
            self.fail(f'HEAVY_REAL_TESTS=1 but: {PROBLEM}')
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='heavy-test-'))
        self.addCleanup(self.cleanup)
        # Every test process has the test folder in its command line (that is how ours() finds it)
        self.sleeper = os.path.join(self.dir, 'sleeper')                 # a copy of sleep: a quiet leftover
        shutil.copy2(shutil.which('sleep'), self.sleeper)
        # leftovers let go of heavy's stdin/stdout/stderr, so a test that reads heavy's output sees it end when heavy
        # ends (with the old wrapper too, which is what the red check runs)
        self.sleeper += ' 300 </dev/null >/dev/null 2>&1'
        quiet = 'exec </dev/null >/dev/null 2>&1\n'
        self.spin = self.script('spin', quiet + 'while :; do :; done')          # a busy one
        self.stubborn = self.script('stubborn', quiet + f"trap '' TERM INT HUP\nwhile :; do {self.dir}/sleeper 0.1; done")
        # a private memory queue: never the user's live one (a test job must not wait behind real builds)
        self.queue_root = os.path.join(self.dir, 'queue')
        # a fake docker (tests/fake_tools.py) on a state file of this test: never the machine's own containers
        self.docker_state = os.path.join(self.dir, 'docker.json')
        with open(self.docker_state, 'w') as f:
            json.dump({'containers': []}, f)
        self.docker = self.script('docker', f'exec "{sys.executable}" -I -B "{FAKE_TOOLS}" "{self.docker_state}" '
                                            f'docker "$@"')
        self.env = dict(os.environ, HEAVY_SLICE=SLICE, HEAVY_CONFIG=os.path.join(self.dir, 'no-config.json'),
                        HEAVY_GRACE='1', HEAVY_QUEUE_ROOT=self.queue_root, HEAVY_DOCKER=self.docker)
        self.env.pop('HEAVY_KEEP', None)

    def containers(self):
        with open(self.docker_state) as f:
            return {c['Name'].lstrip('/'): (c['Config']['Labels'] or {}).get(LABEL) for c in json.load(f)['containers']}

    def add_container(self, name, owner):
        with open(self.docker_state) as f:
            state = json.load(f)
        state['containers'].append({'Id': os.urandom(32).hex(), 'Name': '/' + name, 'Created': '2026-01-01T00:00:00Z',
                                    'State': {'Running': True},
                                    'Config': {'Labels': {LABEL: owner} if owner else {}, 'Image': 'img'}})
        with open(self.docker_state, 'w') as f:
            json.dump(state, f)

    def db_up(self, name='test-db'):
        """Shell text for a command that starts a container the way a test should: labelled with heavy's scope."""
        return f'"$HEAVY_DOCKER" run -d --label "$("{HEAVY}" docker-label)" --name {name} mariadb >/dev/null || exit 9'

    def double_fork(self):
        """Shell text that starts a sleeper which leaves its parent and its session (a daemon's double fork)."""
        return f'(setsid {self.sleeper} &)'

    def script(self, name, body):
        path = os.path.join(self.dir, name)
        with open(path, 'w') as f:
            f.write(f'#!/bin/sh\n{body}\n')
        os.chmod(path, 0o755)
        return path

    def ours(self):
        """Pids of processes whose command line names this test's folder: the command and what it started, not the
        wrapper itself (its arguments name the folder too)."""
        found = []
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or int(pid) == os.getpid():
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    args = f.read().split(b'\0')
            except OSError:
                continue
            if self.dir.encode() not in b'\0'.join(args):
                continue
            if any(os.path.realpath(a.decode(errors='replace')) in WRAPPERS for a in args[:3] if a):
                continue
            found.append(int(pid))
        return found

    def scopes(self):
        """heavy scopes in the slice that hold one of this test's processes."""
        pids = set(self.ours())
        units = []
        for pid in pids:
            try:
                with open(f'/proc/{pid}/cgroup') as f:
                    leaf = f.read().strip().rpartition('/')[2]
            except OSError:
                continue
            if leaf.endswith('.scope') and leaf not in units:
                units.append(leaf)
        return units

    def cleanup(self):
        stray = self.ours()
        units = [u for u in self.scopes() if u.startswith(('heavy-', 'run-'))]
        # and scopes left with no process in them (systemd-run killed while it made the scope): their description
        # is the command line
        listed = subprocess.run(['systemctl', '--user', 'list-units', '--all', '--plain', '--no-legend', 'heavy-*'],
                                capture_output=True, text=True, timeout=60).stdout
        units += [line.split()[0] for line in listed.splitlines() if self.dir in line and line.split()[0] not in units]
        # KILL first (a stop would wait for what ignores TERM), then stop the scopes that are left empty
        if units:
            subprocess.run(['systemctl', '--user', 'kill', '-s', 'KILL', *units], capture_output=True, timeout=60)
        end = time.monotonic() + 15
        while self.ours() and time.monotonic() < end:
            for pid in self.ours():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
            time.sleep(0.05)
        if units:
            subprocess.run(['systemctl', '--user', 'stop', *units], capture_output=True, timeout=60)
        wait_for(lambda: not self.ours(), what='test processes to end')
        shutil.rmtree(self.dir, ignore_errors=True)
        if not getattr(self, 'leaves_on_purpose', False):
            self.assertEqual(stray, [], 'a test left processes running (stopped by the cleanup)')

    def heavy(self, *args, env=None, wrapper=None, **kw):
        return subprocess.run([wrapper or HEAVY, *args], capture_output=True, text=True, timeout=60,
                              env=env or self.env, cwd=self.dir, **kw)

    def heavy_lines(self, stderr):
        return [line for line in stderr.splitlines() if line.startswith('heavy:')]

    # --- leftovers -----------------------------------------------------------------------------------------------

    def test_leftover_child_is_stopped_and_named(self):
        r = self.heavy('sh', '-c', f'{self.spin} & {self.sleeper} & exit 0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ours(), [], 'leftovers still running after heavy returned')
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertRegex(lines[0], r'^heavy: stopped 2 leftover processes \((spin, sleeper|sleeper, spin)\)$')

    def test_leftover_after_a_failing_command_keeps_the_exit_code(self):
        r = self.heavy('sh', '-c', f'{self.sleeper} & exit 5')
        self.assertEqual(r.returncode, 5)
        self.assertEqual(self.ours(), [])
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 1 leftover process (sleeper)'])

    def test_leftover_that_ignores_term_is_killed_after_the_grace_time(self):
        start = time.monotonic()
        r = self.heavy('sh', '-c', f'{self.stubborn} & exit 0')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.ours(), [])
        # stubborn, and maybe the short sleeper it runs in its loop
        self.assertRegex(r.stderr, r'heavy: stopped (1 leftover process|2 leftover processes) \(stubborn')
        self.assertLess(time.monotonic() - start, 20)

    def test_clean_command_prints_nothing_of_its_own(self):
        r = self.heavy('sh', '-c', 'echo out; echo err >&2; exit 0')
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, 'out\n', 'err\n'))

    def test_nested_heavy_reports_once_and_leaves_nothing(self):
        r = self.heavy(HEAVY, 'sh', '-c', f'{self.sleeper} & exit 0')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.ours(), [])
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 1 leftover process (sleeper)'])

    def test_keep_leaves_the_daemon_and_says_so(self):
        env = dict(self.env, HEAVY_KEEP='1')
        r = self.heavy('sh', '-c', f'{self.sleeper} & exit 0', env=env)
        self.assertEqual(r.returncode, 0)
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        m = re.fullmatch(r'heavy: kept 1 leftover process \(sleeper\) in (heavy-keep-sh-[0-9a-f]{8}\.scope) \(HEAVY_KEEP=1\);'
                         r' stop them with: systemctl --user stop \1', lines[0])
        self.assertTrue(m, lines[0])
        self.assertEqual(len(self.ours()), 1, 'the kept process is not running')
        subprocess.run(['systemctl', '--user', 'stop', m.group(1)], check=True, timeout=60)
        wait_for(lambda: not self.ours(), what='the kept process to stop')

    def test_a_command_named_keep_gets_a_plain_scope(self):
        # only HEAVY_KEEP=1 makes a heavy-keep-* scope: the leftover sweep (#159) keeps those by their name
        keep = self.script('keep', 'cat /proc/self/cgroup')
        r = self.heavy(keep)
        self.assertEqual(r.returncode, 0, r.stderr)
        leaf = r.stdout.strip().rpartition('/')[2]
        self.assertRegex(leaf, r'^heavy-keep_-[0-9a-f]{8}\.scope$')
        self.assertFalse(leaf.startswith('heavy-keep-'), leaf)

    def test_a_double_forked_child_is_stopped_too(self):
        r = self.heavy('sh', '-c', f'{self.double_fork()}; {self.sleeper} & exit 0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ours(), [])
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 2 leftover processes (sleeper)'])

    def test_a_container_the_command_started_is_removed_and_no_other(self):
        uid = os.getuid()
        self.add_container('unlabelled', None)
        self.add_container('other-scope', f'uid={uid},scope=heavy-other-0a1b2c3d.scope')
        self.add_container('other-user', f'uid={uid + 1},scope=heavy-sh-0a1b2c3d.scope')
        r = self.heavy('sh', '-c', f'{self.db_up()}; exit 4')
        self.assertEqual(r.returncode, 4, r.stderr)                 # the command's own exit code, as always
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: removed 1 leftover container (test-db)'])
        self.assertEqual(self.containers(), {'unlabelled': None,
                                             'other-scope': f'uid={uid},scope=heavy-other-0a1b2c3d.scope',
                                             'other-user': f'uid={uid + 1},scope=heavy-sh-0a1b2c3d.scope'})

    def test_a_container_and_a_double_forked_child_go_when_heavy_is_killed_with_9(self):
        # kill -9 leaves heavy no chance to clean up: its guard does it (the command itself dies with heavy)
        p = self.start('sh', '-c', f'{self.db_up()}; {self.double_fork()}; {self.sleeper} & exec {self.sleeper}')
        wait_for(lambda: [comm(pid) for pid in self.ours()].count('sleeper') == 3 and 'test-db' in self.containers(),
                 what='the command, its children and its container')
        p.kill()
        _, err = p.communicate(timeout=60)       # stderr ends when the guard is done with it
        wait_for(lambda: not self.ours(), what='the leftovers of a killed heavy to stop')
        self.assertEqual(self.containers(), {})
        lines = self.heavy_lines(err)
        self.assertEqual(len(lines), 2, err)
        # the command itself dies with heavy, maybe just before the guard looks, maybe just after
        self.assertRegex(lines[0], r'^heavy: heavy was killed: stopped (2|3) leftover processes \(sleeper\)$')
        self.assertEqual(lines[1], 'heavy: heavy was killed: removed 1 leftover container (test-db)')
        self.assertEqual(self.scopes(), [])

    def test_a_rerun_after_a_killed_heavy_starts_clean(self):
        # the same test again (the same container name, as a test suite has): the first run left nothing behind
        cmd = f'{self.db_up()}; echo up; exec {self.sleeper}'
        p = self.start('sh', '-c', cmd)
        self.assertEqual(p.stdout.readline(), 'up\n')
        p.kill()
        p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the first run to be cleaned up')
        r = self.heavy('sh', '-c', f'{self.db_up()}; echo ran')
        self.assertEqual((r.returncode, r.stdout), (0, 'ran\n'), r.stderr)
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: removed 1 leftover container (test-db)'])
        r = self.heavy('sh', '-c', 'echo again')               # and a run with nothing to clean says nothing
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, 'again\n', ''))
        self.assertEqual(self.containers(), {})

    def test_a_kept_scope_keeps_its_container(self):
        env = dict(self.env, HEAVY_KEEP='1')
        r = self.heavy('sh', '-c', f'{self.db_up()}; exit 0', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.heavy_lines(r.stderr), [])
        [(name, owner)] = self.containers().items()
        self.assertRegex(owner, r'scope=heavy-keep-sh-[0-9a-f]{8}\.scope$')

    def test_docker_missing_or_down_never_changes_the_run(self):
        r = self.heavy('sh', '-c', f'{self.sleeper} & exit 3',
                       env=dict(self.env, HEAVY_DOCKER=os.path.join(self.dir, 'no-docker')))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 1 leftover process (sleeper)'])
        with open(self.docker_state, 'w') as f:
            json.dump({'containers': [], 'dockerDown': True}, f)
        r = self.heavy('sh', '-c', 'exit 3')
        self.assertEqual(r.returncode, 3)
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertRegex(lines[0], r'^heavy: could not remove the containers of heavy-sh-[0-9a-f]{8}\.scope '
                                   r'\(Cannot connect to the Docker daemon')

    def test_leftovers_that_cannot_be_checked_are_reported(self):
        # the last systemctl query fails (an overloaded host: bus error or timeout): heavy says so, never silent
        self.leaves_on_purpose = True
        real = shutil.which('systemctl')
        fake = self.script('systemctl', f'case "$*" in *" show heavy-"*) echo "bus error" >&2; exit 1;; esac\n'
                                        f'exec {real} "$@"')
        r = self.heavy('sh', '-c', f'{self.sleeper} & exit 0', env=dict(self.env, HEAVY_SYSTEMCTL=fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertRegex(lines[0], r'^heavy: could not check heavy-sh-[0-9a-f]{8}\.scope for leftovers ')

    # --- passthrough ---------------------------------------------------------------------------------------------

    def test_exit_codes_as_the_shell_sees_them(self):
        for script, want in (('exit 0', 0), ('exit 3', 3), ('exit 255', 255), ('kill -TERM $$', 143),
                             ('kill -INT $$', 130), ('kill -KILL $$', 137), ('kill -SEGV $$', 139)):
            with self.subTest(script=script):
                r = subprocess.run(['sh', '-c', '"$0" sh -c "$1"; echo "status=$?"', HEAVY, script],
                                   capture_output=True, text=True, timeout=60, env=self.env, cwd=self.dir)
                self.assertIn(f'status={want}', r.stdout, r.stderr)
                self.assertEqual(self.heavy_lines(r.stderr), [])

    def test_stdin_folder_environment_and_open_files(self):
        env = dict(self.env, HEAVY_TEST_VALUE='kept as is')
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b'from fd 7\n')
        os.close(write_fd)
        os.dup2(read_fd, 7)
        os.close(read_fd)
        try:
            r = self.heavy('sh', '-c', 'cat; pwd; echo "$HEAVY_TEST_VALUE"; cat <&7', input='from stdin\n',
                           env=env, pass_fds=(7,))
        finally:
            os.close(7)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, f'from stdin\n{self.dir}\nkept as is\nfrom fd 7\n')

    def test_environment_is_the_callers_in_a_c_locale_too(self):
        # Python's C-locale coercion (PEP 538) adds LC_CTYPE=C.UTF-8 to heavy's own environment: not to the command's
        env = {k: v for k, v in self.env.items() if k in ('PATH', 'XDG_RUNTIME_DIR', 'DBUS_SESSION_BUS_ADDRESS',
                                                          'HEAVY_SLICE', 'HEAVY_CONFIG', 'HEAVY_GRACE', 'HEAVY_DOCKER')}
        env['LANG'] = 'C'
        r = self.heavy('env', '-0', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dict(item.split('=', 1) for item in r.stdout.split('\0') if item)
        self.assertRegex(got.pop('HEAVY_SCOPE', ''), r'^heavy-env-[0-9a-f]{8}\.scope$')
        got.pop('INVOCATION_ID', None)      # systemd-run --scope sets it (the old wrapper too)
        self.assertEqual(got, env)

    def test_scope_name_is_in_the_environment_nested_too(self):
        r = self.heavy('sh', '-c', 'echo "$HEAVY_SCOPE"; "$0" sh -c \'echo "$HEAVY_SCOPE"; cat /proc/self/cgroup\'',
                       HEAVY)
        self.assertEqual(r.returncode, 0, r.stderr)
        outer, inner, cgroup = r.stdout.split()
        self.assertRegex(outer, r'^heavy-sh-[0-9a-f]{8}\.scope$')
        self.assertEqual((inner, cgroup.rpartition('/')[2]), (outer, outer))

    def test_nested_command_gets_default_sigpipe_and_sigxfsz(self):
        # the nested heavy execs from Python, which ignores SIGPIPE and SIGXFSZ; SIG_IGN survives exec
        r = self.heavy('sh', '-c', '"$0" sh -c "grep SigIgn /proc/self/status"', HEAVY)
        ign = int(r.stdout.split()[-1], 16)
        for sig in (signal.SIGPIPE, signal.SIGXFSZ):
            self.assertEqual(ign >> (sig - 1) & 1, 0, f'{sig.name} ignored in the nested command: {r.stdout}')

    def test_nested_writer_into_a_closed_pipe_ends_quietly(self):
        r = self.heavy('sh', '-c', '"$0" yes | head -1', HEAVY)
        self.assertEqual((r.returncode, r.stdout), (0, 'y\n'), r.stderr)
        self.assertNotIn('Broken pipe', r.stderr)
        # a writer that ignores write errors dies of SIGPIPE too, instead of spinning
        r = self.heavy('sh', '-c', '"$0" sh -c "while :; do echo x; done" | head -1', HEAVY)
        self.assertEqual((r.returncode, r.stdout), (0, 'x\n'), r.stderr)

    def test_runs_in_its_own_scope_in_the_slice(self):
        r = self.heavy('cat', '/proc/self/cgroup')
        parent, _, leaf = r.stdout.strip().rpartition('/')
        self.assertRegex(leaf, r'^heavy-cat-[0-9a-f]{8}\.scope$')
        self.assertEqual(parent.rpartition('/')[2], SLICE, r.stdout)

    # --- signals to heavy ----------------------------------------------------------------------------------------

    def start(self, *args, env=None):
        p = subprocess.Popen([HEAVY, *args], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                             env=env or self.env, cwd=self.dir, start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        return p

    def test_term_to_heavy_stops_the_command_and_its_leftovers(self):
        p = self.start('sh', '-c', f'{self.sleeper} & exec {self.sleeper}')
        wait_for(lambda: len(self.ours()) == 2, what='both processes')
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertEqual(self.ours(), [])
        self.assertIn('heavy: SIGTERM: stopped', err)

    def test_term_to_heavy_removes_the_commands_container(self):
        p = self.start('sh', '-c', f'{self.db_up()}; echo up; exec {self.sleeper}')
        self.assertEqual(p.stdout.readline(), 'up\n')
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertEqual(self.containers(), {})
        self.assertIn('heavy: SIGTERM: removed 1 leftover container (test-db)', err)

    def test_int_to_heavy_and_a_command_that_ignores_it(self):
        p = self.start('sh', '-c', f'{self.sleeper} & exec {self.stubborn}')
        wait_for(lambda: len(self.ours()) >= 2, what='both processes')
        p.send_signal(signal.SIGINT)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGINT)
        self.assertEqual(self.ours(), [])
        self.assertRegex(err, r'heavy: SIGINT: stopped [23] leftover processes \(.*stubborn')

    def test_hup_to_heavy_after_the_command_handles_it(self):
        p = self.start('sh', '-c', f"trap 'exit 0' HUP; {self.sleeper} & while :; do sleep 0.1; done")
        wait_for(lambda: len(self.ours()) == 2, what='sh and the background process')
        time.sleep(0.2)   # the trap is set before the background process starts; this only lets sh reach its loop
        p.send_signal(signal.SIGHUP)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, 0, err)   # the command's own exit code, as before
        self.assertEqual(self.ours(), [])
        self.assertIn('heavy: SIGHUP: stopped 1 leftover process (sleeper)', err)

    def counter(self, seconds):
        """A command that handles INT and USR1 and keeps running: it counts both, prints int=N usr1=M at the end."""
        path = os.path.join(self.dir, 'counter')
        with open(path, 'w') as f:
            f.write('#!/usr/bin/python3 -I\n'
                    'import signal, time\n'
                    'sigs = [signal.SIGINT, signal.SIGUSR1]\n'
                    'signal.pthread_sigmask(signal.SIG_BLOCK, sigs)\n'
                    'print("ready", flush=True)\n'
                    f'n, end = {{s: 0 for s in sigs}}, time.monotonic() + {seconds}\n'
                    'while (left := end - time.monotonic()) > 0:\n'
                    '    info = signal.sigtimedwait(sigs, left)\n'
                    '    if info:\n'
                    '        n[info.si_signo] += 1\n'
                    'print(f"int={n[signal.SIGINT]} usr1={n[signal.SIGUSR1]}", flush=True)\n')
        os.chmod(path, 0o755)
        return path

    def wait_ready(self, p):
        self.assertEqual(p.stdout.readline(), 'ready\n')

    def test_default_grace_lets_a_command_that_handles_int_finish(self):
        # Ctrl-C to a command that handles it (a REPL, a server saving its world): 30 s by default before heavy stops it
        env = dict(self.env)
        env.pop('HEAVY_GRACE')
        p = self.start(self.counter(8), env=env)
        self.wait_ready(p)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual((p.returncode, out), (0, 'int=1 usr1=0\n'), err)

    def test_group_signal_reaches_the_command_once(self):
        # heavy and the command both get a signal sent to the process group: heavy must not pass it on as well.
        # Five USR1 rounds (passed on, never a stop), because the kernel sometimes merges a doubled signal
        p = self.start(self.counter(3), env=dict(self.env, HEAVY_GRACE='10'))
        self.wait_ready(p)
        for _ in range(5):
            os.killpg(p.pid, signal.SIGUSR1)
            time.sleep(0.2)
        os.killpg(p.pid, signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertEqual((p.returncode, out), (0, 'int=1 usr1=5\n'), err)

    def test_signal_to_heavy_alone_is_passed_on(self):
        p = self.start(self.counter(2), env=dict(self.env, HEAVY_GRACE='10'))
        self.wait_ready(p)
        for sig in (signal.SIGUSR1, signal.SIGUSR1, signal.SIGINT):
            p.send_signal(sig)
            time.sleep(0.2)
        out, err = p.communicate(timeout=60)
        self.assertEqual((p.returncode, out), (0, 'int=1 usr1=2\n'), err)

    def test_name_based_kills_do_not_reach_the_signal_probe(self):
        # `pkill -INT heavy` / `killall heavy` signal every process named heavy. Had the probe that name too, the signal
        # would look like a group signal and heavy would not pass it on (the command got nothing until the grace ran out)
        p = self.start(self.counter(2), env=dict(self.env, HEAVY_GRACE='10'))
        self.wait_ready(p)
        kids = [int(n) for n in os.listdir('/proc') if n.isdigit() and self.ppid(int(n)) == p.pid]
        names = [comm(k) for k in kids]
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertIn('grp-sigprobe', names, err)
        self.assertIn('scope-guard', names, err)       # `pkill -9 heavy` must leave the guard to clean up
        self.assertEqual([n for n in names if n and 'heavy' in n], [], err)

    @staticmethod
    def ppid(pid):
        try:
            with open(f'/proc/{pid}/stat') as f:
                return int(f.read().rsplit(')', 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            return None

    def test_timeout_signal_to_heavy_and_its_group_counts_once(self):
        # timeout(1) sends the signal to heavy, then to its whole process group: one event, not a second signal
        r = subprocess.run(['timeout', '-s', 'INT', '1', HEAVY, self.counter(3)], capture_output=True, text=True,
                           timeout=60, env=dict(self.env, HEAVY_GRACE='10'), cwd=self.dir)
        self.assertEqual((r.returncode, r.stdout), (124, 'ready\nint=1 usr1=0\n'), r.stderr)
        self.assertEqual(self.heavy_lines(r.stderr), [])

    def test_kill_9_on_heavy_takes_the_command_with_it(self):
        p = self.start('sh', '-c', f'exec {self.sleeper}')
        # the command itself, not systemd-run still making the scope (its arguments name the folder too)
        wait_for(lambda: [comm(pid) for pid in self.ours()] == ['sleeper'], what='the command')
        p.kill()
        _, err = p.communicate(timeout=60)      # and heavy's guard is done (it holds heavy's stderr till then)
        wait_for(lambda: not self.ours(), what='the command to die with heavy')
        # the command dies with heavy; the guard may look just before that and stop it itself
        self.assertIn(self.heavy_lines(err), ([], ['heavy: heavy was killed: stopped 1 leftover process (sleeper)']))

    # --- refusals: never run uncapped ----------------------------------------------------------------------------

    def test_missing_slice_is_refused(self):
        marker = os.path.join(self.dir, 'ran')
        env = dict(self.env, HEAVY_SLICE='heavy-test-missing-x.slice')
        # the old wrapper makes the slice up (with its parents); stop them again
        self.addCleanup(subprocess.run, ['systemctl', '--user', 'stop', 'heavy-test-missing-x.slice',
                                         'heavy-test-missing.slice', 'heavy-test.slice'], capture_output=True, timeout=60)
        r = self.heavy('touch', marker, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(marker))
        self.assertIn('uncapped', r.stderr)

    def test_unreachable_user_manager_is_refused(self):
        marker = os.path.join(self.dir, 'ran')
        env = dict(self.env, XDG_RUNTIME_DIR=os.path.join(self.dir, 'none'),
                   DBUS_SESSION_BUS_ADDRESS=f'unix:path={self.dir}/none/bus')
        r = self.heavy('touch', marker, env=env)
        self.assertEqual(r.returncode, 1)
        self.assertFalse(os.path.exists(marker))
        self.assertIn('not running touch', r.stderr)

    def test_bad_config_is_refused(self):
        cfg = os.path.join(self.dir, 'config.json')
        with open(cfg, 'w') as f:
            f.write('{"slice": "x", ')
        env = dict(self.env, HEAVY_CONFIG=cfg)
        env.pop('HEAVY_SLICE')
        r = self.heavy('true', env=env)
        self.assertEqual(r.returncode, 1)
        self.assertIn('cannot read', r.stderr)

    def test_no_command_is_a_usage_error(self):
        r = self.heavy()
        self.assertEqual(r.returncode, 2)
        self.assertIn('usage: heavy', r.stderr)

    # --- the memory queue (lib/heavy_queue.py; the rules themselves: tests/queue.test.py) ---------------------------

    def queue_module(self):
        import importlib.machinery, importlib.util
        loader = importlib.machinery.SourceFileLoader('heavy_queue', os.path.join(KIT, 'lib', 'heavy_queue.py'))
        spec = importlib.util.spec_from_loader('heavy_queue', loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod

    def queue_jobs(self, env):
        r = self.heavy('queue', '--json', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        return [j for j in json.loads(r.stdout)['jobs'] if j['kind'].startswith('heavy-test')]

    def peaks(self):
        with open(os.path.join(self.dir, '.local', 'state', 'heavy', 'peaks.json')) as f:
            return json.load(f)['kinds']

    def test_a_queued_job_runs_is_logged_and_leaves_its_peak(self):
        env = dict(self.env, HOME=self.dir)
        r = self.heavy('--kind', 'heavy-test', '--mb', '64', '--wait', '60', '--', 'sleep', '4', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        state = os.path.join(self.dir, '.local', 'state', 'heavy')
        with open(os.path.join(state, 'waits.log')) as f:
            self.assertIn('"outcome": "admitted"', f.read())
        self.assertGreater(len(self.peaks()['heavy-test']), 0)
        self.assertEqual(self.queue_jobs(env), [])
        self.assertTrue(os.path.isdir(os.path.join(self.queue_root, SLICE)))       # the private queue was used

    def test_a_failed_run_leaves_its_peak_too(self):
        env = dict(self.env, HOME=self.dir)
        r = self.heavy('--kind', 'heavy-test', '--mb', '64', '--wait', '60', '--', 'sh', '-c', 'sleep 4; exit 3',
                       env=env)
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertGreater(len(self.peaks()['heavy-test']), 0)      # a failed or OOM-killed run often peaks highest

    def test_a_queued_heavy_killed_with_9_frees_its_reservation_once_its_guard_stops_the_scope(self):
        env = dict(self.env, HOME=self.dir)
        p = self.start('--kind', 'heavy-test', '--mb', '64', '--wait', '60', '--', 'sh', '-c',
                       f'{self.sleeper} & exec {self.sleeper}', env=env)
        wait_for(lambda: len(self.ours()) == 2, what='both processes')
        jobs = self.queue_jobs(env)
        self.assertEqual([(j['kind'], j['state']) for j in jobs], [('heavy-test', 'admitted')])
        # the guard holds no file of the queue: the reservation ends with the job's processes, never later
        guards = [int(n) for n in os.listdir('/proc') if n.isdigit() and self.ppid(int(n)) == p.pid
                  and comm(int(n)) == 'scope-guard']
        self.assertEqual(len(guards), 1)
        held = []
        for fd in os.listdir(f'/proc/{guards[0]}/fd'):
            try:
                held.append(os.readlink(f'/proc/{guards[0]}/fd/{fd}'))
            except OSError:
                pass
        self.assertEqual([h for h in held if h.startswith(self.queue_root)], [], held)
        p.kill()
        _, err = p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the guard to stop the scope')
        self.assertRegex(err, r'heavy: heavy was killed: stopped (1 leftover process|2 leftover processes) \(sleeper\)')
        self.assertEqual(self.queue_jobs(env), [])

    def test_a_queued_job_that_never_fits_gives_up_with_76(self):
        hq = self.queue_module()
        q = hq.Queue(hq.Box(SLICE, '', None, queue_root=self.queue_root),
                     hq.machine_file.fields({}, hq.SETTINGS, 'test'), out=lambda m: None)
        with q:                                     # a job that holds far more than any machine has
            t = q.create('heavy-test-holder', 1 << 30, 'heavy-test-holder.scope', 'x')
            t.data['state'] = 'admitted'
            q.write(t)
        try:
            r = self.heavy('--kind', 'heavy-test', '--mb', '64', '--wait', '0', '--', 'true',
                           env=dict(self.env, HOME=self.dir))
        finally:
            hq.release(q, t)
        self.assertEqual(r.returncode, 76, r.stderr)
        self.assertIn('gave up after waiting', r.stderr)

    # --- measured need (lib/heavy_measure.py; the history and the capacity front are in measure.test.py) ----------

    def measured(self, state):
        try:
            with open(os.path.join(state, 'measured.jsonl')) as f:
                return [json.loads(line) for line in f if line.strip()]
        except FileNotFoundError:
            return []

    def grow(self, mb, seconds):
        """A command that holds mb MB of written memory for a while (the test folder is in its command line)."""
        return [sys.executable, '-I', '-c', f'import time; b = b"x" * ({mb} << 20); time.sleep({seconds})  # {self.dir}']

    def test_a_measured_job_records_its_memory_under_its_kind(self):
        state = os.path.join(self.dir, 'state')
        r = self.heavy('--measure', 'heavy-test', '--', *self.grow(120, 2), env=dict(self.env, HEAVY_STATE_DIR=state))
        self.assertEqual(r.returncode, 0, r.stderr)
        records = self.measured(state)
        self.assertEqual([(x['kind'], x['end']) for x in records], [('heavy-test', 'exited')], r.stderr)
        self.assertGreaterEqual(records[0]['peakMb'], 120)         # the kernel's high-water mark, seen every second
        self.assertGreaterEqual(records[0]['usedMb'], 100)
        self.assertLess(records[0]['usedMb'], 1024)

    def test_a_queued_job_records_under_its_queue_kind(self):
        state = os.path.join(self.dir, 'state')
        r = self.heavy('--kind', 'heavy-test', '--mb', '256', '--wait', '60', '--', *self.grow(80, 1),
                       env=dict(self.env, HOME=self.dir, HEAVY_STATE_DIR=state))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x['kind'] for x in self.measured(state)], ['heavy-test'])

    def test_a_nested_heavy_records_nothing_of_its_own(self):
        state = os.path.join(self.dir, 'state')
        inner = ' '.join(f"'{a}'" for a in self.grow(60, 1))
        r = self.heavy('--measure', 'heavy-outer', '--', 'sh', '-c', f'"{HEAVY}" --measure heavy-inner -- {inner}',
                       env=dict(self.env, HEAVY_STATE_DIR=state))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual([x['kind'] for x in self.measured(state)], ['heavy-outer'])

    def test_a_measured_heavy_killed_with_9_records_nothing_and_leaves_the_history_whole(self):
        state = os.path.join(self.dir, 'state')
        os.makedirs(state)
        before = json.dumps({'v': 1, 'at': 'x', 'kind': 'heavy-test', 'end': 'exited', 'seconds': 1, 'usedMb': 5,
                             'peakMb': 6}) + '\n'
        with open(os.path.join(state, 'measured.jsonl'), 'w') as f:
            f.write(before)
        p = self.start('--measure', 'heavy-test', '--', 'sh', '-c', f'exec {self.sleeper}',
                       env=dict(self.env, HEAVY_STATE_DIR=state))
        wait_for(lambda: len(self.ours()) == 1, what='the command')
        time.sleep(0.5)                             # the measurer has the scope open
        p.kill()
        p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the guard to stop the scope')
        with open(os.path.join(state, 'measured.jsonl')) as f:
            self.assertEqual(f.read(), before)

    # --- red check -----------------------------------------------------------------------------------------------

    def test_red_check_the_old_wrapper_leaves_the_child_running(self):
        """The leftover test catches the bug: with the old one-line wrapper the child survives, silently."""
        self.leaves_on_purpose = True
        r = self.heavy('sh', '-c', f'{self.sleeper} & exit 0', wrapper=OLD)
        self.assertEqual(r.returncode, 0)
        self.assertEqual(len(self.ours()), 1, 'the old wrapper was expected to leave the child running')
        self.assertEqual(self.heavy_lines(r.stderr), [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
