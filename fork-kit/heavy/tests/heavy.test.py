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
import inspect, json, os, pwd, re, secrets, shlex, shutil, signal, subprocess, sys, tempfile, threading, time, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FAKE_TOOLS = os.path.join(KIT, 'tests', 'fake_tools.py')
LABEL = 'fork-kit.heavy.owner'
HEAVY = os.environ.get('HEAVY_TEST_BIN') or os.path.join(KIT, 'bin', 'heavy')
OLD = os.path.join(KIT, 'tests', 'old-heavy.sh')
WRAPPERS = {os.path.realpath(HEAVY), os.path.realpath(OLD)}
SLICE = os.environ.get('HEAVY_TEST_SLICE') or 'heavy.slice'
REQUIRED = os.environ.get('HEAVY_REAL_TESTS') == '1'
# #996: every setting that names a folder heavy writes in. A test hands heavy each one inside its own folder: HOME
# alone is not enough, as the measured history is in the password database's home (lib/heavy_measure.py default_dir)
STATE_SETTINGS = ('HOME', 'HEAVY_STATE_DIR', 'HEAVY_QUEUE_ROOT', 'HEAVY_FACTS_CACHE')
LIVE = set()        # the folders of the Heavy tests running now: a run of heavy keeps its state in one of them


def escapes(env, folders):
    """#996: why heavy, run with env, could write outside folders (the tester's real home, history or queue), or
    None. Every STATE_SETTINGS folder must be in one of them, and HOME one only this user can write (else heavy's
    state notes fall back to the password database's home)."""
    for key in STATE_SETTINGS:
        value = env.get(key) or ''
        if not (os.path.isabs(value) and any(os.path.commonpath([d, os.path.realpath(value)]) == d for d in folders)):
            return f'{key}={value!r} is not in a test folder: heavy would write outside it'
    if os.stat(env['HOME']).st_mode & 0o022:
        return 'HOME can be written by others: heavy would use the real home instead'
    return None


# a command naming heavy: a wrapper's path anywhere in it (also inside `sh -c` text), or the bare word (the one on
# the PATH; not the end of a path such as the kit's own folder)
NAMES_HEAVY = re.compile('|'.join(re.escape(p) for p in {HEAVY, OLD, *WRAPPERS}) + r'|(?<![\w./-])heavy(?![\w./-])')


def guard(words, env, how):
    """#998 item 1: every process this test run starts passes here first (Popen, which run/call/check_output use,
    and the os calls below), so a run of heavy kept in a variable, given as args=, through a bare `run` or through
    os.system/os.spawn* is checked as well as one written inline. One that names heavy without its state in a running
    test's folder (Heavy.isolated) fails before it starts. It follows what runs, not how the source spells it."""
    words = [words] if isinstance(words, (str, bytes, os.PathLike)) else words
    if any(NAMES_HEAVY.search(os.fsdecode(w)) for w in words if isinstance(w, (str, bytes, os.PathLike))):
        problem = escapes(os.environ if env is None else env, LIVE)
        if problem:
            raise AssertionError(f'{how} runs heavy without Heavy.isolated: {problem}')


class GuardedPopen(subprocess.Popen):
    SIGNATURE = inspect.signature(subprocess.Popen)      # the real one (this class's own is *args, **kw)

    def __init__(self, *args, **kw):
        bound = self.SIGNATURE.bind(*args, **kw).arguments
        guard(bound['args'], bound.get('env'), 'Popen')
        super().__init__(*args, **kw)


def guarded(real, env_at):
    def call(*args, **kw):
        guard([w for a in args for w in (a if isinstance(a, (list, tuple)) else [a])],
              args[env_at] if env_at is not None and len(args) > env_at else kw.get('env'), f'os.{real.__name__}')
        return real(*args, **kw)
    return call


subprocess.Popen = GuardedPopen
# os.spawnl* call these spawnv* (os.exec* would replace this test run itself, so none is used)
for _name, _env_at in (('system', None), ('spawnv', None), ('spawnvp', None), ('spawnve', 3), ('spawnvpe', 3),
                       ('posix_spawn', 2), ('posix_spawnp', 2)):
    if hasattr(os, _name):
        setattr(os, _name, guarded(getattr(os, _name), _env_at))


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
# a failure that means heavy (or what it ran) did not end in time: the helpers' messages and subprocess's own
HUNG = r'still running after|no line from pid|TimeoutExpired'


def comm(pid):
    try:
        with open(f'/proc/{pid}/comm') as f:
            return f.read().strip()
    except OSError:
        return None


def read_line(p, timeout=60.0):
    """The next line of p's stdout, or a failure after the timeout (p is killed then): a heavy that hangs before
    its command prints fails the test instead of hanging it. Every read of heavy's output goes through here or
    through a call with a timeout (communicate, run)."""
    got = []
    reader = threading.Thread(target=lambda: got.append(p.stdout.readline()), daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        p.kill()
        raise AssertionError(f'no line from pid {p.pid} in {timeout:g} s')
    return got[0]


def wait_pid(pid, timeout=60.0):
    """os.waitpid for a forked child with a time limit: its exit code, or (after KILL) a failure after the timeout."""
    end = time.monotonic() + timeout
    while True:
        done, status = os.waitpid(pid, os.WNOHANG)
        if done:
            return os.waitstatus_to_exitcode(status)
        if time.monotonic() >= end:
            os.kill(pid, signal.SIGKILL)
            raise AssertionError(f'pid {pid} still running after {timeout:g} s')
        time.sleep(0.01)


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
        if Heavy.hung:
            self.fail(f'not run: heavy hung in {Heavy.hung} (its failure says where); the rest would only wait too')
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='heavy-test-'))
        self.addCleanup(self.cleanup)
        LIVE.add(self.dir)
        self.addCleanup(LIVE.discard, self.dir)      # after every later cleanup (they run last first)
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
        # HOME: heavy makes ~/.local/state/heavy/labelled; a test never makes it in the tester's real home (#761)
        # HEAVY_STATE_DIR: the measured history of every --kind/--measure job; its default is the password
        # database's home, not HOME, so without it each test job wrote the tester's real history (#996)
        # the capacity check of a job without --kind (#866): a fake that answers free at once, so no test waits on the
        # tester's busy machine; test_the_real_capacity_check_answers runs the kit's own. Its 20 s hold goes into the
        # test folder, never the user's live one
        os.makedirs(os.path.join(self.dir, 'facts'))
        self.env = dict(os.environ, HEAVY_SLICE=SLICE, HEAVY_CONFIG=os.path.join(self.dir, 'no-config.json'),
                        HEAVY_GRACE='1', HEAVY_QUEUE_ROOT=self.queue_root, HEAVY_DOCKER=self.docker, HOME=self.dir,
                        HEAVY_TESTING='1', HEAVY_CHECK_HEAVY=self.check_script('free-check', True),
                        HEAVY_FACTS_CACHE=os.path.join(self.dir, 'facts', 'heavy-facts.json'),
                        HEAVY_STATE_DIR=os.path.join(self.dir, 'measured'))
        self.env.pop('HEAVY_KEEP', None)

    hung = None     # the first test whose heavy did not end in time: every later test fails at once, not at its timeout

    def run(self, result=None):
        result = result or self.defaultTestResult()
        seen = len(result.errors), len(result.failures)
        try:
            return super().run(result)
        finally:
            for _, trace in result.errors[seen[0]:] + result.failures[seen[1]:]:
                if Heavy.hung is None and re.search(HUNG, trace):
                    Heavy.hung = self.id()

    def check_script(self, name, free, before=''):
        """A fake `heavy facts` (HEAVY_CHECK_HEAVY): runs the shell text BEFORE, then answers one valid free/v1 object,
        free or busy, dated now."""
        code = ('import datetime, json, sys; at = datetime.datetime.now(datetime.timezone.utc).isoformat(); '
                f'print(json.dumps({{"v": 1, "free": {free}, "at": at, "notes": [], "reason": "test busy"}})); '
                f'sys.exit({0 if free else 75})')
        return self.script(name, f'{before}\nexec "{sys.executable}" -I -c {shlex.quote(code)}')

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

    def settled(self, n, names='sleeper'):
        """Shell text that waits (at most 10 s) until n processes of the command's own scope run one of NAMES (words):
        a forked `sh` that has not exec'd yet would otherwise be named as a leftover `sh` (#981: once at load 14)."""
        match = ' || '.join(f'[ "$x" = {w} ]' for w in names.split())
        return ('cg=/sys/fs/cgroup$(sed -n "s/^0:://p" /proc/self/cgroup); i=0; while [ $i -lt 200 ]; do c=0; '
                'for p in $(cat "$cg/cgroup.procs"); do x=$(cat /proc/$p/comm 2>/dev/null); '
                f'{{ {match}; }} && c=$((c + 1)); done; [ $c -ge {n} ] && break; i=$((i + 1)); sleep 0.05; done')

    def guards(self):
        """Pids of this test's heavy guards: named scope-guard, with heavy's command line (it names the test folder)."""
        found = []
        for pid in os.listdir('/proc'):
            if not pid.isdigit() or comm(int(pid)) != 'scope-guard':
                continue
            try:
                with open(f'/proc/{pid}/cmdline', 'rb') as f:
                    if self.dir.encode() in f.read():
                        found.append(int(pid))
            except OSError:
                continue
        return found

    def the_guard(self):
        """The pid of this test's one guard, once it has renamed itself: heavy starts the command without waiting for
        that (the guard renames itself after its double fork), so on a busy machine the command can print first."""
        wait_for(lambda: len(self.guards()) == 1, what='the guard to rename itself')
        [guard] = self.guards()
        return guard

    def docker_calls(self, verb):
        with open(self.docker_state) as f:
            return [c for c in json.load(f).get('calls', []) if c[1:2] == [verb]]

    def mark(self, unit):
        """The note `heavy docker-label` leaves for heavy (lib/heavy_sweep.py note_label)."""
        return os.path.join('/run/user', str(os.getuid()), 'heavy', 'labelled', unit)

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

    def isolated(self, env):
        """#996: env keeps every folder heavy writes in (STATE_SETTINGS) inside this test's folder (escapes()): a
        test never writes the tester's real home, history or queue. Every run of heavy goes through here (heavy(),
        start(); #998: guard() stops one that does not, at run time)."""
        problem = escapes(env, (self.dir,))
        self.assertIsNone(problem, problem)
        return env

    def heavy(self, *args, env=None, wrapper=None, timeout=60, **kw):
        try:
            return subprocess.run([wrapper or HEAVY, *args], capture_output=True, text=True, timeout=timeout,
                                  env=self.isolated(env or self.env), cwd=self.dir, **kw)
        except subprocess.TimeoutExpired as e:        # what it said till then shows where it hangs
            said = e.stderr.decode(errors='replace') if isinstance(e.stderr, bytes) else e.stderr or ''
            raise AssertionError(f'heavy {" ".join(args)} still running after {timeout:g} s; it said: {said!r}')

    def heavy_lines(self, stderr):
        return [line for line in stderr.splitlines() if line.startswith('heavy:')]

    # --- leftovers -----------------------------------------------------------------------------------------------

    def test_leftover_child_is_stopped_and_named(self):
        # #981 leftover 7: without settled() the shell can exit before its children exec, and heavy names them "sh"
        r = self.heavy('sh', '-c', f'{self.spin} & {self.sleeper} & {self.settled(2, "spin sleeper")}; exit 0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ours(), [], 'leftovers still running after heavy returned')
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertRegex(lines[0], r'^heavy: stopped 2 leftover processes \((spin, sleeper|sleeper, spin)\)$')

    def test_leftover_after_a_failing_command_keeps_the_exit_code(self):
        r = self.heavy('sh', '-c', f'{self.sleeper} & {self.settled(1)}; exit 5')
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
        r = self.heavy(HEAVY, 'sh', '-c', f'{self.sleeper} & {self.settled(1)}; exit 0')
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.ours(), [])
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 1 leftover process (sleeper)'])

    def test_keep_leaves_the_daemon_and_says_so(self):
        env = dict(self.env, HEAVY_KEEP='1')
        r = self.heavy('sh', '-c', f'{self.sleeper} & {self.settled(1)}; exit 0', env=env)
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
        r = self.heavy('sh', '-c', f'{self.double_fork()}; {self.sleeper} & {self.settled(2)}; exit 0')
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

    def test_a_looser_docker_filter_still_removes_only_the_own_container(self):
        # heavy keeps only the containers whose inspected label is exactly its own: a Docker whose `ps --filter
        # label=K=V` matched more (the key only) still never makes heavy remove another scope's or user's container
        with open(self.docker_state, 'w') as f:
            json.dump({'containers': [], 'psMatchesKeyOnly': True}, f)
        uid = os.getuid()
        self.add_container('other-scope', f'uid={uid},scope=heavy-other-0a1b2c3d.scope')
        self.add_container('other-user', f'uid={uid + 1},scope=heavy-sh-0a1b2c3d.scope')
        r = self.heavy('sh', '-c', f'{self.db_up()}; exit 0')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: removed 1 leftover container (test-db)'])
        self.assertEqual(sorted(self.containers()), ['other-scope', 'other-user'])

    def test_docker_is_asked_only_after_a_run_that_labelled_a_container(self):
        # a run that never labelled a container never starts (socket activation) or waits for the Docker daemon
        r = self.heavy('sh', '-c', f'{self.sleeper} & echo "$HEAVY_SCOPE"')
        self.assertEqual(r.returncode, 0, r.stderr)
        # heavy made its state notes folder in the test's HOME, not in the tester's real one (#761 item 9)
        self.assertTrue(os.path.isdir(os.path.join(self.dir, '.local', 'state', 'heavy', 'labelled')))
        self.assertEqual(self.docker_calls('ps'), [])
        self.assertFalse(os.path.exists(self.mark(r.stdout.strip())))
        r = self.heavy('sh', '-c', f'{self.db_up()}; echo "$HEAVY_SCOPE"; test -e "{self.mark("$HEAVY_SCOPE")}"')
        self.assertEqual(r.returncode, 0, r.stderr)        # the note is there while the command runs
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: removed 1 leftover container (test-db)'])
        self.assertEqual(len(self.docker_calls('ps')), 1)
        self.assertFalse(os.path.exists(self.mark(r.stdout.strip())), 'the note outlived the run')

    def test_a_note_that_cannot_reach_heavy_in_run_user_goes_to_the_state_folder(self):
        # docker-label in a sandbox with its own /run/user (here: HEAVY_MARKS without heavy's /run/user folder, as
        # docker-label sees it there): its note goes to ~/.local/state/heavy/labelled, which heavy reads too, so heavy
        # still asks Docker at the end and removes the container (#744 item 8)
        env = dict(self.env, HOME=self.dir)
        state = os.path.join(self.dir, '.local', 'state', 'heavy', 'labelled')
        label = f'"$("{HEAVY}" docker-label)"'
        r = self.heavy('sh', '-c', f'case "$HEAVY_MARKS" in *,*) ;; *) exit 8;; esac; '
                                   f'export HEAVY_MARKS="${{HEAVY_MARKS#*,}}"; '
                                   f'"$HEAVY_DOCKER" run -d --label {label} --name test-db mariadb >/dev/null '
                                   f'|| exit 9; '
                                   f'test -e "{state}/$HEAVY_SCOPE" || exit 10; '
                                   f'test ! -e "{self.mark("$HEAVY_SCOPE")}" || exit 11', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: removed 1 leftover container (test-db)'])
        self.assertEqual(self.containers(), {})
        self.assertEqual(os.listdir(state), [], 'the note outlived the run')

    def test_heavy_ends_only_after_its_guard(self):
        # done() waits for the guard to end: a script that waits for heavy (without reading its stderr) never finds
        # heavy's guard still running. A stopped guard holds heavy until it goes on
        err = open(os.path.join(self.dir, 'stderr'), 'w')
        self.addCleanup(err.close)
        go = os.path.join(self.dir, 'go')
        p = subprocess.Popen([HEAVY, 'sh', '-c', f'echo up; while [ ! -e {go} ]; do sleep 0.05; done'],
                             stdout=subprocess.PIPE, stderr=err, text=True, env=self.isolated(self.env), cwd=self.dir,
                             start_new_session=True)
        self.addCleanup(lambda: p.poll() is None and p.kill())
        self.assertEqual(read_line(p), 'up\n')
        guard = self.the_guard()
        os.kill(guard, signal.SIGSTOP)
        self.addCleanup(lambda: guard in self.guards() and os.kill(guard, signal.SIGCONT))
        open(go, 'w').close()
        with self.assertRaises(subprocess.TimeoutExpired, msg='heavy ended while its guard still ran'):
            p.wait(timeout=2)
        os.kill(guard, signal.SIGCONT)
        self.assertEqual(p.wait(timeout=60), 0)
        self.assertEqual(self.guards(), [])

    def test_a_container_and_a_double_forked_child_go_when_heavy_is_killed_with_9(self):
        # kill -9 leaves heavy no chance to clean up: its guard does it (the command itself dies with heavy)
        p = self.start('sh', '-c', f'echo "$HEAVY_SCOPE"; {self.db_up()}; {self.double_fork()}; {self.sleeper} & '
                                   f'exec {self.sleeper}')
        wait_for(lambda: [comm(pid) for pid in self.ours()].count('sleeper') == 3 and 'test-db' in self.containers(),
                 what='the command, its children and its container')
        p.kill()
        out, err = p.communicate(timeout=60)     # stderr ends when the guard is done with it
        wait_for(lambda: not self.ours(), what='the leftovers of a killed heavy to stop')
        self.assertEqual(self.containers(), {})
        lines = self.heavy_lines(err)
        self.assertEqual(len(lines), 2, err)
        # the command itself dies with heavy, maybe just before the guard looks, maybe just after
        self.assertRegex(lines[0], r'^heavy: heavy was killed: stopped (2|3) leftover processes \(sleeper\)$')
        self.assertEqual(lines[1], 'heavy: heavy was killed: removed 1 leftover container (test-db)')
        self.assertEqual(self.scopes(), [])
        self.assertFalse(os.path.exists(self.mark(out.strip())), 'the guard left the note of docker-label')

    def test_a_kill_9_of_heavys_whole_process_group_leaves_nothing(self):
        # how timeout(1) -s KILL, a harness or a shell job kill ends a run: -9 to heavy's process group (heavy, its
        # signal probe and the command). The guard is in a session of its own, so it lives on and cleans up
        p = self.start('sh', '-c', f'{self.db_up()}; {self.double_fork()}; echo up; exec {self.sleeper}')
        self.assertEqual(read_line(p), 'up\n')
        wait_for(lambda: [comm(pid) for pid in self.ours()].count('sleeper') == 2, what='the command and its daemon')
        os.killpg(p.pid, signal.SIGKILL)
        _, err = p.communicate(timeout=60)       # stderr ends when the guard is done with it
        wait_for(lambda: not self.ours(), what='the guard to stop what the group kill left')
        self.assertEqual(self.containers(), {})
        self.assertIn('heavy: heavy was killed: removed 1 leftover container (test-db)', err)
        self.assertEqual(self.scopes(), [])

    def test_a_tree_kill_of_heavy_and_its_children_leaves_nothing(self):
        # pkill -9 -P / npm tree-kill: KILL to heavy and every process below it by parent pid. The guard is nobody's
        # child of heavy's (a double fork), so the walk never finds it, and it cleans up
        p = self.start('sh', '-c', f'{self.db_up()}; {self.double_fork()}; echo up; exec {self.sleeper}')
        self.assertEqual(read_line(p), 'up\n')
        wait_for(lambda: [comm(pid) for pid in self.ours()].count('sleeper') == 2, what='the command and its daemon')
        tree, todo = [], [p.pid]
        while todo:
            pid = todo.pop()
            tree.append(pid)
            todo += [int(n) for n in os.listdir('/proc') if n.isdigit() and self.ppid(int(n)) == pid]
        self.assertEqual([pid for pid in tree if comm(pid) == 'scope-guard'], [])
        for pid in reversed(tree):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
        _, err = p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the guard to stop what the tree kill left')
        self.assertEqual(self.containers(), {})
        self.assertIn('heavy: heavy was killed: removed 1 leftover container (test-db)', err)

    def test_the_guard_ignores_the_signals_heavy_handles(self):
        # a service stop's TERM, a terminal's INT and HUP, USR1/2 that heavy passes on: none ends the guard, so a
        # kill -9 of heavy afterwards is still cleaned up (QUIT is left out here: without the guard ignoring it, it
        # would dump a core)
        p = self.start('sh', '-c', f'{self.double_fork()}; echo up; exec {self.sleeper}')
        self.assertEqual(read_line(p), 'up\n')
        guard = self.the_guard()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP, signal.SIGUSR1, signal.SIGUSR2):
            os.kill(guard, sig)
        p.kill()
        _, err = p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the guard to stop the scope')
        self.assertRegex(err, r'heavy: heavy was killed: stopped (1 leftover process|2 leftover processes) \(sleeper\)')

    def test_a_kept_heavy_killed_with_9_keeps_its_daemon(self):
        # HEAVY_KEEP=1: the scope outlives heavy on purpose, so it has no guard, and kill -9 of heavy leaves the daemon
        env = dict(self.env, HEAVY_KEEP='1')
        p = self.start('sh', '-c', f'{self.double_fork()}; echo up; exec {self.sleeper}', env=env)
        self.assertEqual(read_line(p), 'up\n')
        self.assertEqual(self.guards(), [])
        p.kill()
        _, err = p.communicate(timeout=60)       # a guard would hold stderr till its clean-up was done
        self.assertEqual(self.heavy_lines(err), [])
        # the command dies with heavy; the daemon stays in its kept scope
        wait_for(lambda: [comm(pid) for pid in self.ours()] == ['sleeper'], what='only the daemon')
        [unit] = self.scopes()
        self.assertRegex(unit, r'^heavy-keep-sh-[0-9a-f]{8}\.scope$')
        subprocess.run(['systemctl', '--user', 'stop', unit], check=True, timeout=60)
        wait_for(lambda: not self.ours(), what='the kept daemon to stop')

    def test_a_rerun_after_a_killed_heavy_starts_clean(self):
        # the same test again (the same container name, as a test suite has): the first run left nothing behind
        cmd = f'{self.db_up()}; echo up; exec {self.sleeper}'
        p = self.start('sh', '-c', cmd)
        self.assertEqual(read_line(p), 'up\n')
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
        r = self.heavy('sh', '-c', f'{self.sleeper} & {self.settled(1)}; exit 3',
                       env=dict(self.env, HEAVY_DOCKER=os.path.join(self.dir, 'no-docker')))
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.heavy_lines(r.stderr), ['heavy: stopped 1 leftover process (sleeper)'])
        with open(self.docker_state, 'w') as f:
            json.dump({'containers': [], 'dockerDown': True}, f)
        r = self.heavy('sh', '-c', f'"{HEAVY}" docker-label >/dev/null; exit 3')   # labelled, the daemon then went
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

    def test_leftovers_are_stopped_by_hand_when_systemd_cannot_stop_the_scope(self):
        # `systemctl stop` fails: heavy sends TERM to what is still in the scope, then KILL to the whole cgroup
        real = shutil.which('systemctl')
        fake = self.script('systemctl', f'case "$*" in *" stop heavy-"*) echo "bus error" >&2; exit 1;; esac\n'
                                        f'exec {real} "$@"')
        r = self.heavy('sh', '-c', f'{self.stubborn} & exit 0', env=dict(self.env, HEAVY_SYSTEMCTL=fake))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.ours(), [])
        lines = self.heavy_lines(r.stderr)
        self.assertEqual(len(lines), 1, r.stderr)
        self.assertRegex(lines[0], r'^heavy: stopped (1 leftover process|2 leftover processes) \(stubborn')

    # --- passthrough ---------------------------------------------------------------------------------------------

    def test_exit_codes_as_the_shell_sees_them(self):
        for script, want in (('exit 0', 0), ('exit 3', 3), ('exit 255', 255), ('kill -TERM $$', 143),
                             ('kill -INT $$', 130), ('kill -KILL $$', 137), ('kill -SEGV $$', 139)):
            with self.subTest(script=script):
                r = subprocess.run(['sh', '-c', '"$0" sh -c "$1"; echo "status=$?"', HEAVY, script],
                                   capture_output=True, text=True, timeout=60, env=self.isolated(self.env),
                                   cwd=self.dir)
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
                                                          'HEAVY_SLICE', 'HEAVY_CONFIG', 'HEAVY_GRACE', 'HEAVY_DOCKER',
                                                          'HEAVY_TESTING', 'HEAVY_CHECK_HEAVY', *STATE_SETTINGS)}
        env['LANG'] = 'C'
        r = self.heavy('env', '-0', env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        got = dict(item.split('=', 1) for item in r.stdout.split('\0') if item)
        self.assertRegex(got.pop('HEAVY_SCOPE', ''), r'^heavy-env-[0-9a-f]{8}\.scope$')
        got.pop('INVOCATION_ID', None)      # systemd-run --scope sets it (the old wrapper too)
        # the folders heavy reads the notes of `heavy docker-label` from (device:inode; the old wrapper has none)
        self.assertRegex(got.pop('HEAVY_MARKS', '1:1'), r'^\d+:\d+(,\d+:\d+)?$')
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
                             env=self.isolated(env or self.env), cwd=self.dir, start_new_session=True)
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
        self.assertEqual(read_line(p), 'up\n')
        p.send_signal(signal.SIGTERM)
        _, err = p.communicate(timeout=30)
        self.assertEqual(p.returncode, -signal.SIGTERM)
        self.assertEqual(self.containers(), {})
        self.assertIn('heavy: SIGTERM: removed 1 leftover container (test-db)', err)

    def test_a_signal_to_heavy_removes_the_container_of_a_command_that_outlives_it(self):
        # the command ignores the signal: heavy stops the scope after the grace time, or at once at a second signal,
        # and removes the container then (the line names the signal that stopped it)
        for sigs, grace in (([signal.SIGTERM], '1'), ([signal.SIGINT, signal.SIGTERM], '5')):
            with self.subTest(sigs=[s.name for s in sigs]):
                with open(self.docker_state, 'w') as f:
                    json.dump({'containers': []}, f)
                p = self.start('sh', '-c', f'{self.db_up()}; echo up; exec {self.stubborn}',
                               env=dict(self.env, HEAVY_GRACE=grace))
                self.assertEqual(read_line(p), 'up\n')
                for sig in sigs:
                    p.send_signal(sig)
                    time.sleep(0.5)
                _, err = p.communicate(timeout=60)
                self.assertEqual(p.returncode, -sigs[-1], err)
                self.assertEqual(self.containers(), {})
                self.assertIn(f'heavy: {sigs[-1].name}: removed 1 leftover container (test-db)', err)
                wait_for(lambda: not self.ours(), what='the stubborn command to stop')

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
        self.assertEqual(read_line(p), 'ready\n')

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
        guards = [self.the_guard()]     # `pkill -9 heavy` must leave the guard to clean up
        p.send_signal(signal.SIGINT)
        out, err = p.communicate(timeout=60)
        self.assertIn('grp-sigprobe', names, err)
        self.assertEqual(len(guards), 1, err)
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
                           timeout=60, env=self.isolated(dict(self.env, HEAVY_GRACE='10')), cwd=self.dir)
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

    def test_a_measured_job_never_writes_the_testers_real_history(self):
        # #996: the tests set a fake HOME, but heavy takes the measured history's folder from the password database,
        # so each `heavy --kind heavy-test` run added a line to the tester's real ~/.local/state/heavy/measured.jsonl.
        # A kind no other run uses, run both ways (--kind, --measure): its lines are in the test folder, not the
        # real one
        real = os.path.join(pwd.getpwuid(os.getuid()).pw_dir, '.local', 'state', 'heavy')
        kind = f'heavy-test-{secrets.token_hex(4)}'

        def lines(folder):
            found = []
            for name in ('measured.jsonl', 'measured.jsonl.1'):
                try:
                    with open(os.path.join(folder, name), encoding='utf-8', errors='replace') as f:
                        found += [line for line in f if f'"kind":"{kind}"' in line]
                except FileNotFoundError:
                    pass
            return found
        for args in (('--kind', kind, '--mb', '64', '--wait', '60'), ('--measure', kind)):
            r = self.heavy(*args, '--', 'sleep', '3')
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(lines(real), [], f'a test wrote the real history in {real}')
        self.assertEqual(len(lines(self.env['HEAVY_STATE_DIR'])), 2, 'recorded in the test folder instead')

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
        guards = [self.the_guard()]
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

    # --- the capacity check of a job without --kind (#866) -------------------------------------------------------

    def real_check(self, env=None):
        """The environment with the kit's own capacity check (`heavy facts`) instead of the fake one."""
        return {k: v for k, v in (env or self.env).items() if k != 'HEAVY_CHECK_HEAVY'}

    def test_a_kit_without_the_capacity_rule_refuses_at_once_and_runs_nothing(self):
        """The CI hang (#866): a checkout without packages/shared/src/capacity has a heavy whose facts can never
        answer, and every job without --kind waited its whole --wait (2 hours) for an answer that never came."""
        kit = os.path.join(self.dir, 'kit', 'fork-kit')
        for part in ('heavy', 'common'):
            shutil.copytree(os.path.join(os.path.dirname(KIT), part), os.path.join(kit, part),
                            ignore=shutil.ignore_patterns('tests', '__pycache__'))
        ran = os.path.join(self.dir, 'ran')
        start = time.monotonic()
        r = self.heavy('sh', '-c', f'touch {ran}', env=self.real_check(),
                       wrapper=os.path.join(kit, 'heavy', 'bin', 'heavy'), timeout=30)
        self.assertLess(time.monotonic() - start, 15, r.stderr)
        self.assertEqual(r.returncode, 1, r.stderr)
        self.assertIn('the capacity check cannot run', r.stderr)
        self.assertIn('capacity-rule.json', r.stderr)
        self.assertFalse(os.path.exists(ran), 'the command ran without a capacity check')

    def test_the_real_capacity_check_answers_within_its_wait(self):
        start = time.monotonic()
        r = self.heavy('--wait', '0', 'true', env=self.real_check(), timeout=30)
        self.assertLess(time.monotonic() - start, 20, r.stderr)
        if REQUIRED or r.returncode == 0:           # CI's runner has room for one job; a tester's machine may not
            self.assertEqual((r.returncode, self.heavy_lines(r.stderr)), (0, []), r.stderr)
        else:
            self.assertEqual(r.returncode, 76, r.stderr)
            self.assertIn('gave up', r.stderr)

    def test_a_busy_capacity_check_gives_up_at_its_wait_and_runs_nothing(self):
        ran = os.path.join(self.dir, 'ran')
        r = self.heavy('--wait', '1', 'sh', '-c', f'touch {ran}',
                       env=dict(self.env, HEAVY_CHECK_HEAVY=self.check_script('busy-check', False)))
        self.assertEqual(r.returncode, 76, r.stderr)
        self.assertIn('gave up', r.stderr)
        self.assertIn('test busy', r.stderr)
        self.assertFalse(os.path.exists(ran))

    def stop(self, *procs):
        for p in procs:
            if p.poll() is None:
                p.send_signal(signal.SIGTERM)
            p.communicate(timeout=60)
        wait_for(lambda: not self.ours(), what='the commands to stop')

    def test_heavy_facts_counts_a_running_job_without_kind(self):
        p = self.start('sh', '-c', f'exec {self.sleeper}')
        wait_for(lambda: len(self.ours()) == 1, what='the command')
        r = self.heavy('facts', '--fresh')          # facts is the kit's own (the fake only answers heavy's check)
        self.assertIn(r.returncode, (0, 75), r.stderr)
        running = json.loads(r.stdout)['queue']['running']
        if REQUIRED:                                # CI: nothing else runs in the slice
            self.assertEqual(running, 1, r.stdout)
        else:
            self.assertGreaterEqual(running, 1, r.stdout)
        self.stop(p)

    def test_two_waiters_without_kind_are_admitted_one_at_a_time(self):
        """Both see room for one more job: without the admission lock both checks answer before either scope can be
        counted, and both start (#866). The fake check writes how many of this test's scopes it can see."""
        tag = 'w' + os.urandom(4).hex()
        log = os.path.join(self.dir, 'checks.log')
        count = (f'cg=$(systemctl --user show {SLICE} -p ControlGroup --value); '
                 f'n=$(ls "/sys/fs/cgroup$cg" 2>/dev/null | grep -c "^heavy-{tag}-"); echo "$n" >> {log}; sleep 0.5')
        env = dict(self.env, HEAVY_CHECK_HEAVY=self.check_script('counting-check', True, count))
        cmd = self.script(tag, f'exec {self.sleeper}')
        cg = '/sys/fs/cgroup' + subprocess.run(['systemctl', '--user', 'show', SLICE, '-p', 'ControlGroup', '--value'],
                                               capture_output=True, text=True, timeout=30).stdout.strip()

        def scopes():                               # not ours(): the fake check's own sh names this folder too
            try:
                return [n for n in os.listdir(cg) if n.startswith(f'heavy-{tag}-')]
            except FileNotFoundError:
                return []
        a, b = self.start('--wait', '60', cmd, env=env), self.start('--wait', '60', cmd, env=env)
        try:
            wait_for(lambda: len(scopes()) == 2, timeout=30, what='both commands\' scopes')
            with open(log) as f:
                seen = sorted(int(n) for n in f.read().split())
        finally:
            self.stop(a, b)
        self.assertEqual(seen, [0, 1], 'the second check ran before the first job could be counted')

    def test_a_waiter_killed_while_it_holds_the_admission_lock_lets_the_next_one_in(self):
        """Stops half-way: the lock is an flock that no child of heavy keeps, so a waiter killed in its check frees it
        at once, though the check it started lives on."""
        log = os.path.join(self.dir, 'checks.log')
        hang = f'echo "$WAITER" >> {log}; if [ "$WAITER" = a ]; then {self.sleeper}; fi'
        env = dict(self.env, HEAVY_CHECK_HEAVY=self.check_script('hanging-check', True, hang))
        a = self.start('--wait', '60', 'true', env=dict(env, WAITER='a'))
        wait_for(lambda: os.path.exists(log), what='the first check')
        a.kill()
        a.communicate(timeout=60)
        start = time.monotonic()
        r = self.heavy('--wait', '30', 'true', env=dict(env, WAITER='b'))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(time.monotonic() - start, 10, 'the killed waiter\'s lock was still held')
        with open(log) as f:
            self.assertEqual(f.read().split(), ['a', 'b'])
        for pid in self.ours():                     # the first check's sleeper
            os.kill(pid, signal.SIGKILL)
        wait_for(lambda: not self.ours(), what='the hanging check to end')

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
        self.assertGreaterEqual(records[0]['needMb'], records[0]['usedMb'])
        self.assertEqual(records[0]['code'], 0)

    def test_a_measured_job_records_its_exit_code_and_a_stopped_one_counts_as_stopped(self):
        state = os.path.join(self.dir, 'state')
        env = dict(self.env, HEAVY_STATE_DIR=state)
        fails = f'"{sys.executable}" -I -c "import time; b = bytes(60 << 20).replace(bytes(1), bytes([1])); ' \
                f'time.sleep(1)"; exit 3  # {self.dir}'
        r = self.heavy('--measure', 'heavy-test', '--', 'sh', '-c', fails, env=env)
        self.assertEqual(r.returncode, 3, r.stderr)
        p = self.start('--measure', 'heavy-test', '--', *self.grow(60, 300), env=env)
        wait_for(lambda: len(self.ours()) == 1, what='the command')
        time.sleep(1.5)                             # the measurer has seen the scope
        p.send_signal(signal.SIGTERM)
        p.communicate(timeout=60)
        self.assertEqual([(x['end'], x['code']) for x in self.measured(state)],
                         [('exited', 3), ('stopped', -signal.SIGTERM)])

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


def load_wrapper():
    """bin/heavy under test as a module (None for a wrapper that is not Python: tests/old-heavy.sh)."""
    import importlib.machinery, importlib.util
    with open(HEAVY, 'rb') as f:
        if b'python' not in f.readline():
            return None
    loader = importlib.machinery.SourceFileLoader('heavy_under_test', HEAVY)
    spec = importlib.util.spec_from_loader('heavy_under_test', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def load_sweep_of_wrapper():
    import importlib.machinery, importlib.util
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.realpath(HEAVY))), 'lib', 'heavy_sweep.py')
    loader = importlib.machinery.SourceFileLoader('heavy_sweep_under_test', path)
    spec = importlib.util.spec_from_loader('heavy_sweep_under_test', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Parts(unittest.TestCase):
    """Parts of bin/heavy the real-systemd tests cannot reach on purpose (a pid reused, heavy gone before the command
    starts, a Docker context): on temporary folders and child processes of this test only, no systemd needed."""

    def setUp(self):
        self.mod = load_wrapper()
        if self.mod is None:
            self.skipTest(f'{HEAVY} is not the Python wrapper')
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='heavy-parts-'))
        self.addCleanup(shutil.rmtree, self.dir, True)

    def child(self):
        c = subprocess.Popen(['sleep', '60'])
        self.addCleanup(lambda: (c.kill(), c.wait(timeout=60)))
        return c

    def test_a_stop_by_hand_never_signals_a_pid_that_is_no_longer_in_the_scope(self):
        # cgroup.procs named the pid, then the process ended and the pid went to another process (here: a child of
        # this test in another cgroup): neither TERM nor KILL reaches it; the KILL goes to cgroup.kill
        c = self.child()
        cg = '/heavy-test-0a1b2c3d.scope'
        os.makedirs(self.dir + cg)
        with open(self.dir + cg + '/cgroup.procs', 'w') as f:
            f.write(f'{c.pid}\n')
        open(self.dir + cg + '/cgroup.kill', 'w').close()
        self.mod.CGROUP_ROOTS = (self.dir,)
        left = self.mod.stop_scope(shutil.which('false'), 'heavy-test-0a1b2c3d.scope', cg, 0.2)
        self.assertEqual(left, [c.pid])
        self.assertIsNone(c.poll(), 'a process outside the scope was signalled')
        with open(self.dir + cg + '/cgroup.kill') as f:
            self.assertEqual(f.read(), '1')

    def test_a_process_still_in_the_scope_is_signalled(self):
        c = self.child()
        with open(f'/proc/{c.pid}/cgroup') as f:
            cg = next(line[3:].strip() for line in f if line.startswith('0::'))
        self.assertEqual(self.mod.signal_in([c.pid], cg, signal.SIGTERM), 1)
        self.assertEqual(c.wait(timeout=10), -signal.SIGTERM)
        self.assertEqual(self.mod.signal_in([c.pid], cg, signal.SIGTERM), 0)     # gone: nothing sent, no error

    def test_without_pidfds_nothing_is_signalled_by_pid(self):
        # Linux before 5.3 or Python before 3.9: between the cgroup check and kill(pid) the pid could go to another
        # process, so no signal goes by pid at all (the KILL goes to cgroup.kill; the rest is reported as left)
        import errno
        from unittest import mock
        c = self.child()
        with open(f'/proc/{c.pid}/cgroup') as f:
            cg = next(line[3:].strip() for line in f if line.startswith('0::'))
        for how in ('missing', 'ENOSYS'):
            with self.subTest(pidfd_open=how):
                with mock.patch.object(os, 'pidfd_open', **({'new': None} if how == 'missing' else
                                                            {'side_effect': OSError(errno.ENOSYS, 'no pidfds')})):
                    with mock.patch.object(os, 'kill', side_effect=AssertionError('signalled by pid')):
                        self.mod.signal_in([c.pid], cg, signal.SIGTERM)
                self.assertIsNone(c.poll())

    def test_no_grace_wait_when_nothing_could_be_signalled(self):
        # #761 item 2: without pidfds (Linux before 5.3, Python before 3.9) and without cgroup.kill heavy sends
        # nothing by hand, so waiting the grace time (30 s by default) would only hold the caller up
        from unittest import mock
        c = self.child()
        cg = '/heavy-test-0a1b2c3d.scope'
        os.makedirs(self.dir + cg)
        with open(self.dir + cg + '/cgroup.procs', 'w') as f:
            f.write(f'{c.pid}\n')
        self.mod.CGROUP_ROOTS = (self.dir,)
        start = time.monotonic()
        with mock.patch.object(os, 'pidfd_open', None):
            left = self.mod.stop_scope(shutil.which('false'), 'heavy-test-0a1b2c3d.scope', cg, 30)
        self.assertLess(time.monotonic() - start, 10, 'heavy waited although it sent nothing')
        self.assertEqual(left, [c.pid])
        self.assertIsNone(c.poll())

    def test_processes_that_end_a_moment_later_are_not_named_still_running(self):
        # #767 nit: without pidfds and cgroup.kill heavy sends nothing itself, but systemd's failed stop may already
        # have signalled the processes: heavy still looks for a moment (SETTLE), not the whole grace time
        from unittest import mock
        cg = '/heavy-test-0a1b2c3d.scope'
        os.makedirs(self.dir + cg)                      # no cgroup.kill
        self.mod.CGROUP_ROOTS = (self.dir,)
        first = []

        def pids(_cg):                                  # the process ends 0.2 s after heavy first looked
            first.append(first[0] if first else time.monotonic())
            return [] if time.monotonic() - first[0] >= 0.2 else [4194304]

        start = time.monotonic()
        with mock.patch.object(os, 'pidfd_open', None), mock.patch.object(self.mod, 'cgroup_pids', pids):
            left = self.mod.stop_scope(shutil.which('false'), 'heavy-test-0a1b2c3d.scope', cg, 30)
        self.assertEqual(left, [])
        self.assertLess(time.monotonic() - start, 10)

    def foreign_folder(self):
        """A folder another user owns: '/' for a user that is not root; as root (CI) a temporary one given away."""
        if os.geteuid() != 0:
            return '/'
        d = os.path.join(self.dir, 'theirs')
        os.mkdir(d, 0o755)
        os.chown(d, 65534, 65534)
        return d

    def test_the_state_folder_is_made_only_in_a_home_this_user_owns(self):
        # #761 items 1 and 8. heavy makes ~/.local/state when it is missing: never in another user's home (root with a
        # user's HOME, `sudo -E heavy ...`, would leave a root-owned folder there) and never in the job's working
        # folder (no HOME and no passwd entry: expanduser('~') is a relative '~'). docker-label finds the same folder.
        import contextlib, io, pwd, types
        from unittest import mock
        sweep = load_sweep_of_wrapper()
        mine, pw_home, work = (os.path.join(self.dir, n) for n in ('mine', 'pw-home', 'work'))
        for d in (mine, pw_home, work):
            os.mkdir(d, 0o700)
        os.mkdir(os.path.join(work, 'rel'), 0o700)     # HOME=rel would name this folder if relative ones counted
        theirs = self.foreign_folder()
        here = os.getcwd()
        self.addCleanup(os.chdir, here)
        os.chdir(work)

        me = os.getuid()

        def as_root(real):
            """os.stat or os.lstat as root sees this test's folders: this user's files read as root's."""
            def look(path, *args, **kwargs):
                st = real(path, *args, **kwargs)
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0 if st.st_uid == me else
                                       st.st_uid, st.st_gid, st.st_size, st.st_atime, st.st_mtime, st.st_ctime))
            return look

        def state(home, pw, euid=None, root=False):
            """(heavy's state notes folder, docker-label's base, heavy's base) for HOME=home (None: unset), a passwd
            home pw (None: no entry) and maybe another effective uid, or root (mocked: nothing runs as root)."""
            env = {k: v for k, v in os.environ.items() if k != 'HOME'}
            if home is not None:
                env['HOME'] = home
            entry = ({'side_effect': KeyError(os.getuid())} if pw is None else
                     {'return_value': types.SimpleNamespace(pw_dir=pw)})
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
                stack.enter_context(mock.patch.object(pwd, 'getpwuid', **entry))
                if euid is not None or root:
                    stack.enter_context(mock.patch.multiple(os, getuid=lambda: 0 if root else euid,
                                                            geteuid=lambda: 0 if root else euid))
                if root:
                    stack.enter_context(mock.patch.multiple(os, stat=as_root(os.stat), lstat=as_root(os.lstat)))
                return self.mod.state_marks_dir(), sweep.state_base(), self.mod.state_base()

        def labelled(home):
            return os.path.join(home, '.local', 'state', 'heavy', 'labelled')

        # HOME of this user: used and made
        got = state(mine, pw_home)
        self.assertEqual(got, (labelled(mine), os.path.join(mine, '.local', 'state'),
                               os.path.join(mine, '.local', 'state')))
        self.assertTrue(os.path.isdir(labelled(mine)))
        self.assertEqual(state(mine, pw_home)[0], labelled(mine))              # again: the same, nothing new
        # another user's HOME (as root with sudo -E): this user's own home from the passwd database instead
        got = state(theirs, pw_home)
        self.assertEqual(got[0], labelled(pw_home))
        self.assertEqual(got[1], got[2])
        if theirs != '/':
            self.assertEqual(os.listdir(theirs), [], 'a folder was made in another user\'s home')
        # HOME of a uid that is not this one (a uid that owns nothing here): no folder at all, none made in either
        shutil.rmtree(os.path.join(mine, '.local'))
        other = os.getuid() + 1
        self.assertEqual(state(mine, pw_home, euid=other), ('', '', ''))
        self.assertEqual(state(None, mine, euid=other), ('', '', ''))
        self.assertFalse(os.path.exists(os.path.join(mine, '.local')), 'a folder was made in another user\'s home')
        # HOME empty, unset or relative: the passwd database's home
        for home in ('', None, 'rel', '~'):
            with self.subTest(HOME=home):
                got = state(home, pw_home)
                self.assertEqual((got[0], got[1]), (labelled(pw_home), os.path.join(pw_home, '.local', 'state')))
        # no HOME and no passwd entry: no second folder, and nothing made in the working folder
        for home in (None, '', 'rel'):
            with self.subTest(HOME=home, passwd=None):
                self.assertEqual(state(home, None), ('', '', ''))
        self.assertEqual((os.listdir(work), os.listdir(os.path.join(work, 'rel'))), (['rel'], []),
                         'a folder was made in the working folder')
        # #767 item 1: a home others can write is not used (this user's own, made 1777)
        os.chmod(mine, 0o1777)
        self.assertEqual(state(mine, pw_home)[:2], (labelled(pw_home), os.path.join(pw_home, '.local', 'state')))
        self.assertFalse(os.path.exists(os.path.join(mine, '.local')))
        os.chmod(mine, 0o700)
        # #951 item 4: this user's own home (700) in a folder others can write and that is not sticky: they could
        # rename the home away and put their own in its place, so it is not used; the passwd home, or none at all
        loose = os.path.join(self.dir, 'loose')
        os.mkdir(loose)
        os.chmod(loose, 0o777)
        inside = os.path.join(loose, 'me')
        os.mkdir(inside, 0o700)
        self.assertEqual(state(inside, None), ('', '', ''))
        self.assertEqual(state(inside, pw_home)[:2], (labelled(pw_home), os.path.join(pw_home, '.local', 'state')))
        self.assertEqual(os.listdir(inside), [], 'a folder was made in a home someone else could swap')
        os.chmod(loose, 0o1777)                     # sticky, as /tmp is: only its owner can rename the home
        self.assertEqual(state(inside, None)[0], labelled(inside))
        shutil.rmtree(os.path.join(inside, '.local'))
        # #771 item 1: a ~/.local others can write is not used (they could swap state for a link): no second folder,
        # nothing made in it; only this user's again: used
        os.mkdir(os.path.join(mine, '.local'), 0o700)
        os.chmod(os.path.join(mine, '.local'), 0o777)
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(state(mine, pw_home)[0], '')
        # #945: said, not dropped silently (a ~/.local of mode 775, as umask 002 makes it), with the repair
        self.assertIn(f'chmod go-w {os.path.join(mine, ".local")}', err.getvalue())
        self.assertEqual(os.listdir(os.path.join(mine, '.local')), [])
        # a ~/.local another user owns (root's, from an older heavy run as root): the repair is chown, not chmod
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            self.assertEqual(self.mod.own_folder(os.path.join(theirs, 'state'), make_base=True), '')
        self.assertIn(f'sudo chown "$USER:" {theirs}', err.getvalue())
        self.assertNotIn('chmod go-w', err.getvalue())
        os.chmod(os.path.join(mine, '.local'), 0o700)
        self.assertEqual(state(mine, pw_home)[0], labelled(mine))
        # #771 nit: a HOME given as a link is resolved once, so a link swapped after the check changes nothing
        linked = os.path.join(self.dir, 'home-link')
        os.symlink(mine, linked)
        self.assertEqual(state(linked, pw_home), (labelled(mine),) + (os.path.join(mine, '.local', 'state'),) * 2)
        # as root: HOME=/tmp (root's, anyone can write it), where another user planted .local -> their own folder,
        # and HOME=/: root's own home from the passwd database instead, and nothing made in the planted folder
        tmp, victim, roots = (os.path.join(self.dir, n) for n in ('tmp', 'victim', 'root-home'))
        for d in (tmp, victim, roots):
            os.mkdir(d, 0o700)
        os.chmod(tmp, 0o1777)
        os.symlink(victim, os.path.join(tmp, '.local'))
        for home in (tmp, '/'):
            with self.subTest(root_HOME=home):
                got = state(home, roots, root=True)
                self.assertEqual(got, (labelled(roots),) + (os.path.join(roots, '.local', 'state'),) * 2)
        # a home only root can write, with .local planted as a link: never followed, so no second folder
        shutil.rmtree(os.path.join(roots, '.local'))
        os.symlink(victim, os.path.join(roots, '.local'))
        self.assertEqual(state(roots, None, root=True)[0], '')
        self.assertEqual(os.listdir(victim), [], 'root made folders through a planted .local link')
        # docker-label with no such home: one reason, no note anywhere
        sweep.RUNTIME_ROOT = os.path.join(self.dir, 'no-run')
        with mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != 'HOME'}, clear=True), \
                mock.patch.object(pwd, 'getpwuid', side_effect=KeyError(0)):
            why = sweep.note_label(f'uid={os.getuid()},scope=heavy-sh-0a1b2c3d.scope', '1:1')
        self.assertIn('no home folder this user owns', why)
        self.assertEqual(os.listdir(work), ['rel'])

    def test_the_command_never_starts_when_heavy_died_before_it_could(self):
        # child_setup runs in the forked command before exec: its parent must still be the heavy that forked it.
        # Here the command is a grandchild (its parent is another process, as after heavy died and it was reparented)
        setup = self.mod.child_setup([], signal.pthread_sigmask(signal.SIG_BLOCK, []))

        def run(grandchild):
            pid = os.fork()
            if pid == 0:
                code = 2
                try:
                    if grandchild:
                        g = os.fork()
                        if g == 0:
                            try:
                                setup()
                            finally:
                                os._exit(0)
                        code = wait_pid(g) & 0xff
                    else:
                        setup()
                        code = 0
                finally:
                    os._exit(code)
            return wait_pid(pid)

        self.assertEqual(run(grandchild=False), 0)
        self.assertEqual(run(grandchild=True), 127)

    def test_docker_through_a_context_counts_as_reachable(self):
        # rootless Docker picked with `docker context use`: no DOCKER_HOST, no DOCKER_CONTEXT, no usable default socket
        from unittest import mock
        folder = os.path.join(self.dir, 'bin')
        os.mkdir(folder)
        docker = os.path.join(folder, 'docker')
        with open(docker, 'w') as f:
            f.write('#!/bin/sh\nexit 1\n')
        os.chmod(docker, 0o755)
        self.mod.DOCKER_DIRS = (folder,)
        self.mod.DOCKER_SOCKET = os.path.join(self.dir, 'no.sock')
        cfg = os.path.join(self.dir, 'docker-config')
        os.mkdir(cfg)
        env = {k: v for k, v in os.environ.items() if k not in ('HEAVY_DOCKER', 'DOCKER_HOST', 'DOCKER_CONTEXT')}
        with mock.patch.dict(os.environ, dict(env, DOCKER_CONFIG=cfg), clear=True):
            self.assertIsNone(self.mod.docker_tool())                     # no config.json
            for text, want in (('{"currentContext": "default"}', None), ('{"currentContext": ""}', None),
                               ('not json', None), ('[]', None), ('{"currentContext": "rootless"}', docker)):
                with self.subTest(config=text):
                    with open(os.path.join(cfg, 'config.json'), 'w') as f:
                        f.write(text)
                    self.assertEqual(self.mod.docker_tool(), want)
            os.remove(os.path.join(cfg, 'config.json'))
            os.environ['DOCKER_HOST'] = 'unix:///nowhere.sock'
            self.assertEqual(self.mod.docker_tool(), docker)

    def test_marks_folder_the_same_for_heavy_and_docker_label_and_only_this_users(self):
        sweep = load_sweep_of_wrapper()
        root = os.path.join(self.dir, 'run')
        base = os.path.join(root, str(os.getuid()))
        os.makedirs(base, 0o700)
        state_root = os.path.join(self.dir, 'home', '.local', 'state')           # missing: heavy makes it
        os.mkdir(os.path.join(self.dir, 'home'), 0o700)                          # (in a home that is there)
        self.mod.RUNTIME_ROOT = sweep.RUNTIME_ROOT = root
        self.mod.STATE_ROOT = sweep.STATE_ROOT = state_root
        unit = 'heavy-sh-0a1b2c3d.scope'
        marks = self.mod.marks_dirs()                          # made by heavy before the command runs
        run, state = os.path.join(base, 'heavy', 'labelled'), os.path.join(state_root, 'heavy', 'labelled')
        self.assertEqual(marks, (run, state))
        ids = self.mod.marks_ids(marks)
        self.assertEqual(ids, ','.join(f'{os.stat(m).st_dev}:{os.stat(m).st_ino}' for m in marks))
        self.assertFalse(self.mod.labelled(marks, unit))
        self.assertEqual(sweep.note_label(f'uid={os.getuid()},scope={unit}', ids), '')
        self.assertTrue(self.mod.labelled(marks, unit))
        self.assertEqual(sweep.note_label(f'uid={os.getuid()},pid=1,start=2', ids), '')    # a process owner: no note
        self.assertEqual((os.listdir(run), os.listdir(state)), ([unit], []))
        self.mod.forget(marks, unit)
        self.assertEqual(os.listdir(run), [])
        self.mod.forget(marks, unit)                           # again: nothing, no error
        # a note in the state folder counts the same, and goes the same (heavy stopped half-way: its guard forgets)
        open(os.path.join(state, unit), 'w').close()
        self.assertTrue(self.mod.labelled(marks, unit))
        self.mod.forget(marks, unit)
        self.assertEqual((os.listdir(run), os.listdir(state)), ([], []))
        # a folder others can write: not one of heavy's
        os.chmod(run, 0o777)
        self.assertEqual(self.mod.marks_dir(), '')
        self.assertEqual(self.mod.marks_dirs(), (state,))
        self.assertIn('not a folder only this user can write',
                      sweep.note_label(f'uid={os.getuid()},scope={unit}', ''))
        os.chmod(run, 0o700)
        shutil.rmtree(base)                                    # no runtime folder at all: the same
        self.assertEqual(self.mod.marks_dir(), '')
        os.chmod(os.path.dirname(state), 0o777)                # and no usable state folder: heavy cannot tell, so it
        self.assertEqual(self.mod.marks_dirs(), ())            # asks Docker after every run
        self.assertTrue(self.mod.labelled((), unit))
        self.assertEqual(self.mod.marks_ids(()), '')
        os.chmod(os.path.dirname(state), 0o700)

    def test_the_command_gets_heavy_marks_only_from_its_own_heavy(self):
        # HEAVY_MARKS names the folders this heavy reads; an outer heavy's value, or one a user left in a shell, never
        # reaches a command whose heavy has none (docker-label would trust it and skip the second folder)
        env = {b'HEAVY_MARKS': b'1:1', b'PATH': b'/bin'}
        self.mod.give_marks(env, ())
        self.assertEqual(env, {b'PATH': b'/bin'})
        self.mod.give_marks(env, (self.dir,))
        st = os.stat(self.dir)
        self.assertEqual(env[b'HEAVY_MARKS'], f'{st.st_dev}:{st.st_ino}'.encode())


UNTIMED_CALLS = ('run', 'check_output', 'check_call', 'call')
NEVER_TIMED = ('getoutput', 'getstatusoutput')          # subprocess calls that take no timeout at all
PIPES = ('stdout', 'stderr')
READS = ('read', 'readline', 'readlines')
# calls that read a pipe they are given to its end (or until a line comes)
DRAINS = ('list', 'tuple', 'set', 'frozenset', 'sorted', 'iter', 'next', 'enumerate', 'zip', 'map', 'filter',
          'dict', 'any', 'all', 'sum', 'min', 'max')


def untimed(source, name):
    """The waits for a test process in source that have no time limit (#744 item 7, #761 item 3, #767 item 2), in
    the forms Hygiene.test_the_check_finds_each_listed_form lists (a form it does not list may get through):
    subprocess.run and friends without timeout= (also passed on uncalled, as to addCleanup, imported bare, or through
    `import subprocess as sp`), getoutput and getstatusoutput (no timeout at all), wait() without a time,
    communicate() without timeout= (its first positional argument is the input, not the time), a bound wait or
    communicate passed on uncalled without timeout=, os.waitpid or os.wait4 without WNOHANG, both imported bare
    (`from os import waitpid`: its flags are not checked), `with Popen(...)` (its exit waits with no time limit;
    #771), and outside read_line
    any read of a process's stdout or stderr (called, or passed on uncalled as to iter), a loop over one, or one
    given to list/next/enumerate/map/join and the like."""
    import ast
    tree = ast.parse(source)
    helpers = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == 'read_line']
    inside = {id(x) for h in helpers for x in ast.walk(h)}
    modules = {'subprocess'} | {a.asname for n in ast.walk(tree) if isinstance(n, ast.Import)
                                for a in n.names if a.name == 'subprocess' and a.asname}

    def is_pipe(n):
        return isinstance(n, ast.Attribute) and n.attr in PIPES

    def of_subprocess(n, names):
        return (isinstance(n, ast.Attribute) and n.attr in names and isinstance(n.value, ast.Name)
                and n.value.id in modules)

    def names_wnohang(n):
        return any(isinstance(x, (ast.Name, ast.Attribute)) and getattr(x, 'id', getattr(x, 'attr', '')) == 'WNOHANG'
                   for x in ast.walk(n))

    found = []
    for n in ast.walk(tree):
        if id(n) in inside:
            continue
        if isinstance(n, ast.ImportFrom) and n.module == 'subprocess':
            found += [f'{name}:{n.lineno} from subprocess import {a.name}' for a in n.names
                      if a.name in UNTIMED_CALLS + NEVER_TIMED]
        elif isinstance(n, ast.ImportFrom) and n.module == 'os':
            found += [f'{name}:{n.lineno} from os import {a.name} (use os.{a.name} with WNOHANG, or wait_pid)'
                      for a in n.names if a.name in ('waitpid', 'wait4')]
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            found += [f'{name}:{n.lineno} with Popen(...) (its exit waits with no time limit)' for item in n.items
                      if isinstance(item.context_expr, ast.Call)
                      and (of_subprocess(item.context_expr.func, ('Popen',))
                           or (isinstance(item.context_expr.func, ast.Name) and item.context_expr.func.id == 'Popen'))]
        elif isinstance(n, (ast.For, ast.AsyncFor, ast.comprehension)) and is_pipe(n.iter):
            found.append(f'{name}:{getattr(n, "lineno", n.iter.lineno)} loop over .{n.iter.attr} (use read_line)')
        elif isinstance(n, ast.Attribute) and n.attr in READS and is_pipe(n.value):
            found.append(f'{name}:{n.lineno} {n.value.attr}.{n.attr} (use read_line)')     # called or passed on
        if not isinstance(n, ast.Call):
            continue
        timed = any(k.arg == 'timeout' for k in n.keywords)
        for i, arg in enumerate(n.args):    # addCleanup(subprocess.run, ...): called later with the rest
            rest = len(n.args) - i - 1
            if of_subprocess(arg, NEVER_TIMED) or (of_subprocess(arg, UNTIMED_CALLS) and not timed):
                found.append(f'{name}:{n.lineno} subprocess.{arg.attr} passed on without a timeout')
            elif (isinstance(arg, ast.Attribute) and not timed
                  and ((arg.attr == 'wait' and rest < 1) or (arg.attr == 'communicate' and rest < 2))):
                found.append(f'{name}:{n.lineno} .{arg.attr} passed on without a timeout')
        drains = ((isinstance(n.func, ast.Name) and n.func.id in DRAINS)
                  or (isinstance(n.func, ast.Attribute) and n.func.attr == 'join'))
        if drains and any(is_pipe(a) for a in n.args):
            what = n.func.id if isinstance(n.func, ast.Name) else 'join'
            found.append(f'{name}:{n.lineno} {what}() over a pipe (use read_line)')
        if not isinstance(n.func, ast.Attribute):
            continue
        a = n.func.attr
        if of_subprocess(n.func, NEVER_TIMED) or (of_subprocess(n.func, UNTIMED_CALLS) and not timed):
            found.append(f'{name}:{n.lineno} subprocess.{a}')
        elif a == 'wait' and not n.args and not timed:
            found.append(f'{name}:{n.lineno} wait()')
        elif a == 'communicate' and len(n.args) < 2 and not timed:
            found.append(f'{name}:{n.lineno} communicate() without timeout=')
        elif (a in ('waitpid', 'wait4') and isinstance(n.func.value, ast.Name) and n.func.value.id == 'os'
              and not (len(n.args) > 1 and names_wnohang(n.args[1]))):
            found.append(f'{name}:{n.lineno} os.{a} without WNOHANG (use wait_pid)')
    return found


class Hygiene(unittest.TestCase):
    """#744 item 7: a test of heavy that waits for a process without a time limit hangs (once 19 minutes) where it
    should fail."""
    FILES = ('heavy.test.py', 'queue.test.py', 'sweep.test.py', 'install.test.py')

    def test_every_wait_for_a_test_process_has_a_timeout(self):
        here = os.path.dirname(os.path.abspath(__file__))
        found = []
        for name in self.FILES:
            with open(os.path.join(here, name), encoding='utf-8') as f:
                found += untimed(f.read(), name)
        self.assertEqual(found, [])

    def test_a_run_of_heavy_without_isolated_fails_before_it_starts(self):
        # #998 item 1: guard() follows what runs, so each of these spellings is stopped. The scratch env is the
        # tester's own shape (not a running test's folder); --help writes nothing even if one got through
        scratch = os.path.realpath(tempfile.mkdtemp(prefix='heavy-guard-'))
        self.addCleanup(shutil.rmtree, scratch, True)
        env = dict(os.environ, **{key: scratch for key in STATE_SETTINGS})
        cmd = [HEAVY, '--help']
        bare = subprocess.run           # as after `from subprocess import run` (which untimed() does not let in)
        runs = {
            'inline': lambda: subprocess.run([HEAVY, '--help'], env=env, capture_output=True, timeout=60),
            'kept in a variable': lambda: subprocess.run(cmd, env=env, capture_output=True, timeout=60),
            'no env': lambda: subprocess.run(cmd, capture_output=True, timeout=60),
            'args=': lambda: subprocess.run(args=cmd, env=env, capture_output=True, timeout=60),
            'a bare name': lambda: bare(cmd, env=env, capture_output=True, timeout=60),
            'sh -c text': lambda: subprocess.run(['sh', '-c', f'"{HEAVY}" --help'], env=env, capture_output=True,
                                                 timeout=60),
            'from the PATH': lambda: subprocess.run(['heavy', '--help'], env=env, capture_output=True, timeout=60),
            'Popen': lambda: subprocess.Popen(cmd, env=env, stdout=subprocess.DEVNULL),
            'os.system': lambda: os.system(f'"{HEAVY}" --help >/dev/null'),
            'os.spawnv': lambda: os.spawnv(os.P_WAIT, HEAVY, cmd),
            'os.spawnlp': lambda: os.spawnlp(os.P_WAIT, 'heavy', 'heavy', '--help'),
            'os.spawnve': lambda: os.spawnve(os.P_WAIT, HEAVY, cmd, env),
            'os.posix_spawn': lambda: os.posix_spawn(HEAVY, cmd, env),
        }
        for what, run in runs.items():
            with self.subTest(what):
                with self.assertRaisesRegex(AssertionError, 'runs heavy without Heavy.isolated'):
                    run()
        self.assertEqual(os.listdir(scratch), [])
        # the same command passes once its state is in a running test's folder, and other commands are not held
        LIVE.add(scratch)
        self.addCleanup(LIVE.discard, scratch)
        self.assertEqual(subprocess.run(cmd, env=env, capture_output=True, timeout=60).returncode, 0)
        self.assertEqual(subprocess.run(['true', 'heavy.slice', 'heavy-x.scope', KIT], timeout=60).returncode, 0)

    def test_the_check_finds_each_listed_form(self):
        # #761 item 3, #767 item 2: each of these hangs on a silent process; each timed form passes. The check
        # promises these forms only (untimed's docstring), not every way Python can wait
        cases = {
            'p.wait()': 1, 'p.wait(5)': 0, 'p.wait(timeout=5)': 0,
            'p.communicate()': 1, "p.communicate('x')": 1, "p.communicate('x', 5)": 0, 'p.communicate(timeout=5)': 0,
            "subprocess.run(['x'])": 1, "subprocess.check_output(['x'])": 1, "subprocess.run(['x'], timeout=5)": 0,
            'from subprocess import run': 1, 'from subprocess import check_output as co': 1,
            'from subprocess import Popen, PIPE': 0,
            "self.addCleanup(subprocess.run, ['x'])": 1, "self.addCleanup(subprocess.run, ['x'], timeout=5)": 0,
            'p.stdout.read()': 1, 'p.stdout.readline()': 1, 'p.stdout.readlines()': 1,
            'p.stderr.read()': 1, 'p.stderr.readline()': 1, 'p.stderr.readlines()': 1,
            'for line in p.stdout: pass': 1, 'for line in p.stderr: pass': 1, '[x for x in p.stdout]': 1,
            'list(p.stderr)': 1, 'next(p.stdout)': 1,
            'p.stdout.close()': 0, 'f.read()': 0,
            "for line in iter(p.stdout.readline, ''): pass": 1, 'enumerate(p.stdout)': 1,
            'list(map(str.strip, p.stdout))': 1, 'zip(p.stdout, x)': 1, 'filter(None, p.stderr)': 1,
            "''.join(p.stdout)": 1, "'-'.join(x)": 0,
            "import subprocess as sp\nsp.run(['x'])": 1, "import subprocess as sp\nsp.run(['x'], timeout=5)": 0,
            "subprocess.getoutput('x')": 1, "subprocess.getstatusoutput('x')": 1, 'from subprocess import getoutput': 1,
            "self.addCleanup(subprocess.getoutput, 'x')": 1,
            'os.waitpid(pid, 0)': 1, 'os.wait4(pid, 0)': 1, 'os.waitpid(pid, os.WNOHANG)': 0,
            'from os import waitpid': 1, 'from os import wait4 as w': 1, 'from os import WNOHANG, getpid': 0,
            "with subprocess.Popen(['x']) as p: pass": 1, "with Popen(['x']) as p: pass": 1,
            "import subprocess as sp\nwith sp.Popen(['x']) as p: pass": 1, "with open('x') as f: pass": 0,
            "p = subprocess.Popen(['x'])": 0,
            'self.addCleanup(p.wait)': 1, 'self.addCleanup(p.communicate)': 1, 'self.addCleanup(p.wait, 5)': 0,
            'self.addCleanup(p.wait, timeout=5)': 0, 'self.addCleanup(p.communicate, None, 5)': 0,
            'self.addCleanup(p.stdout.close)': 0,
            'def read_line(p):\n    return p.stdout.readline()': 0,
        }
        for text, want in cases.items():
            with self.subTest(code=text):
                self.assertEqual(len(untimed(text, 'x.py')), want, untimed(text, 'x.py'))

    def test_read_line_fails_instead_of_hanging(self):
        p = subprocess.Popen(['sleep', '60'], stdout=subprocess.PIPE, text=True)
        self.addCleanup(lambda: (p.kill(), p.wait(timeout=60), p.stdout.close()))
        start = time.monotonic()
        with self.assertRaisesRegex(AssertionError, 'no line'):
            read_line(p, timeout=0.5)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(p.wait(timeout=10), -signal.SIGKILL)       # and the silent process is stopped


if __name__ == '__main__':
    unittest.main(verbosity=2)
