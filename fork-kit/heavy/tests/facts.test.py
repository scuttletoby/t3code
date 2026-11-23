"""`heavy facts` (lib/heavy_facts.py) and its two thin callers, bin/t3-capacity and bin/agent-slots, on a fake kernel
tree (HEAVY_FACTS_ROOT: /proc and /sys), a temporary queue, home and cache. No systemd, no T3, no root.

  python3 -I -B fork-kit/heavy/tests/facts.test.py

What the lenses attack, one test each: T3 down or not installed (the kernel still answers), agent scopes counted
(unlabelled = writer), every heavy ticket a job (unknown kinds: the job default, up to the job ceiling), other
users' jobs and agents, the Basic tier (never free), an unreadable signal (elevated, never ok), container limits,
reservations (own folder, other folder, sibling folder, with and without '/', through a link, ended, broken,
missing, cut short, their processes gone), the machine file's holds (in use or not, its own users, missing or
unreadable cgroup files, a missing, empty, broken or changed machine file), the state question (a second call
within 5 s, an old, future, broken, empty, foreign or other-settings cache, no cache folder at all, a writer
stopped half-way, a broken cost file), two callers at once, and a caller whose `heavy facts` is slow or fails (5 s,
never free). Expected numbers come from capacity-rule.json, never copied
here. RedCheck takes fix lines out of a copy of lib/heavy_facts.py and shows their tests fail.
"""
import contextlib, datetime, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, stat, subprocess
import pwd, sys, tempfile, time, types, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACTS = os.path.join(KIT, 'lib', 'heavy_facts.py')
HEAVY = os.path.join(KIT, 'bin', 'heavy')
T3CAP = os.path.join(KIT, 'bin', 't3-capacity')
SLOTS = os.path.join(KIT, 'bin', 'agent-slots')
MB = 1 << 20
UID = os.getuid()
MANAGER = f'/sys/fs/cgroup/user.slice/user-{UID}.slice/user@{UID}.service'
AGENTS = MANAGER + '/app.slice/app-t3code.slice/app-t3code-agents.slice/app-t3code-agents-a1.slice'
NOW = datetime.datetime(2026, 11, 20, 12, 0, 0, tzinfo=datetime.timezone.utc)
OTHER = '/sys/fs/cgroup/user.slice/user-4000000001.slice/user@4000000001.service'     # another user's manager


def psi(some=0.0, full=0.0):
    """A memory.pressure file as the kernel writes it."""
    return (f'some avg10={some:.2f} avg60=0.00 avg300=0.00 total=0\n'
            f'full avg10={full:.2f} avg60=0.00 avg300=0.00 total=0\n')


CALM = psi()


def load_module(name, path, source=None):
    if source is None:
        loader = importlib.machinery.SourceFileLoader(name, path)
        mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
        loader.exec_module(mod)
        return mod
    mod = types.ModuleType(name)
    mod.__file__ = path
    exec(compile(source, path, 'exec'), mod.__dict__)
    return mod


hf = load_module('heavy_facts', FACTS)
hq = load_module('heavy_queue', os.path.join(KIT, 'lib', 'heavy_queue.py'))
cr = hf.cr
RULE = cr.shipped_rule()
WRITER = RULE['unknownKinds']['other']          # what an unlabelled agent scope counts as
BUILD_MB = RULE['kinds']['job-build']['startMb']    # a build ticket that reserved just its kind's cost


class Machine:
    """A fake machine: 16 GB, 12 GB available, 4 GB swap free, calm PSI, 8 cores, cgroup v2 with a user manager."""

    def __init__(self, tmp):
        self.tmp, self.root = tmp, os.path.join(tmp, 'root')
        self.home, self.queue = os.path.join(tmp, 'home'), os.path.join(tmp, 'queue')
        self.reserve, self.cache = os.path.join(tmp, 'reserve.d'), os.path.join(tmp, 'cache', 'heavy-facts.json')
        self.held = []
        for d in (self.home, os.path.join(self.queue, 'heavy.slice'), self.reserve, os.path.dirname(self.cache)):
            os.makedirs(d)
        os.makedirs(self.root, mode=0o755)
        os.chmod(self.root, 0o755)      # the machine file's folders on the way: never group-writable (umask 002)
        os.chmod(self.home, 0o700)      # a home others cannot write (umask 002 would make it 775: #951 refuses it)
        self.write('/proc/meminfo', self.meminfo(16384, 12288))
        self.write('/proc/pressure/memory', CALM)
        self.write('/proc/loadavg', '1.50 1.00 0.50 2/300 4000\n')
        self.write('/sys/devices/system/cpu/online', '0-7\n')
        self.write('/sys/fs/cgroup/cgroup.controllers', 'cpu memory pids\n')
        self.write(MANAGER + '/heavy.slice/memory.pressure', CALM)
        os.makedirs(self.root + MANAGER + '/app.slice')

    @staticmethod
    def meminfo(total, avail, swap_total=4096, swap_free=4096):
        return (f'MemTotal: {total * 1024} kB\nMemFree: 1000 kB\nMemAvailable: {avail * 1024} kB\n'
                f'SwapTotal: {swap_total * 1024} kB\nSwapFree: {swap_free * 1024} kB\n')

    def write(self, path, text):
        full = self.root + path if path.startswith('/') else path
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w') as f:
            f.write(text)

    def agent(self, name, demand_mb, pids=()):
        folder = f'{AGENTS}/t3code-{name}-0a1b2c3d.scope'
        self.write(folder + '/memory.stat', f'anon {demand_mb * MB}\nfile 999999999\nshmem 0\n')
        self.write(folder + '/cgroup.procs', ''.join(f'{p}\n' for p in pids))
        for box in (MANAGER + '/app.slice/app-t3code.slice', AGENTS):   # every v2 cgroup has one (PSI on)
            self.write(box + '/memory.pressure', CALM)

    def job(self, kind, mb, demand_mb=None, state='admitted', hold=True, raw=None, seq=None):
        seq = seq or len(os.listdir(os.path.join(self.queue, 'heavy.slice'))) + 1
        scope = f'run-j{seq}.scope'
        path = os.path.join(self.queue, 'heavy.slice', f't-{seq}-{os.getpid()}.json')
        with open(path, 'w') as f:
            f.write(raw if raw is not None else json.dumps(
                {'v': 1, 'kind': kind, 'mb': mb, 'pid': os.getpid(), 'seq': seq, 'state': state, 'scope': scope}))
        if demand_mb is not None:
            self.write(f'{MANAGER}/heavy.slice/{scope}/memory.stat', f'anon {demand_mb * MB}\nshmem 0\n')
        if hold:                                    # a live heavy holds its ticket
            fd = os.open(path, os.O_RDONLY)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self.held.append(fd)

    def process(self, pid, cwd, rss_mb):
        os.makedirs(cwd, exist_ok=True)
        os.makedirs(f'{self.root}/proc/{pid}')
        os.symlink(cwd, f'{self.root}/proc/{pid}/cwd')
        self.write(f'/proc/{pid}/status', f'Name:\tx\nRssAnon:\t{rss_mb * 1024} kB\nRssShmem:\t0 kB\n')

    def env(self, **more):
        # HEAVY_FACTS_DISK_FREE_MB: the free disk t3-capacity reads (#982), 100 GB unless a test says otherwise
        return dict(dict(os.environ, HEAVY_TESTING='1', HEAVY_FACTS_ROOT=self.root, HEAVY_FACTS_CACHE=self.cache,
                         HEAVY_QUEUE_ROOT=self.queue,
                         HEAVY_CONFIG=os.path.join(self.tmp, 'no-config.json'), HOME=self.home,
                         T3_CAPACITY_RESERVE_DIR=self.reserve, HEAVY_FACTS_DISK_FREE_MB=str(100 * 1024)), **more)

    def close(self):
        for fd in self.held:
            os.close(fd)


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='facts-test-')
        self.m = Machine(self.tmp)
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.addCleanup(self.m.close)
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        real = pwd.getpwuid                         # #951: never this user's real home (the cache's last folder)

        def entry(uid):
            e = real(uid)
            return pwd.struct_passwd(e[:5] + (self.m.home,) + e[6:]) if uid == UID else e
        for patch in (mock.patch.object(pwd, 'getpwuid', entry), mock.patch.dict(os.environ, {'HOME': self.m.home})):
            patch.start()
            self.addCleanup(patch.stop)

    def facts(self, *args, now=NOW, cwd=None, mod=None, env=None):
        """In-process `heavy facts` at a fixed time: (exit code, object or None)."""
        mod = mod or hf
        cwd = cwd or self.work
        out = io.StringIO()
        old = os.getcwd()
        with mock.patch.dict(os.environ, dict(self.m.env(PWD=cwd), **(env or {})), clear=True), \
                mock.patch.object(mod, 'ROOT', self.m.root), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            os.chdir(cwd)
            try:
                code = mod.facts_main(list(args), 'heavy.slice', self.m.queue, hq, now=now)
            finally:
                os.chdir(old)
        text = out.getvalue()
        return code, json.loads(text) if text else None

    def cli(self, argv, timeout=30, cwd=None, **env):
        p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, cwd=cwd or self.work,
                           env=self.m.env(PWD=cwd or self.work, **env))
        return p.returncode, p.stdout, p.stderr

    def cost(self, kind):
        return RULE['kinds'][kind]['startMb']

    def machine_file(self, data, mode=0o644):
        path = self.m.root + '/etc/fork-kit/machine.json'
        for folder in ('/etc', '/etc/fork-kit'):
            os.makedirs(self.m.root + folder, exist_ok=True)
            os.chmod(self.m.root + folder, 0o755)
        with open(path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(path, mode)


class Ledger(Case):
    def test_t3_down_or_not_installed_still_answers_from_the_kernel(self):
        code, d = self.facts('--kind', WRITER)
        self.assertEqual(code, 0, d)
        self.assertEqual((d['free'], d['tier'], d['level'], d['queue']), (True, 'full', 'ok',
                                                                          {'running': 0, 'waiting': 0, 'agents': 0}))
        self.assertEqual(d['memory']['owedMb'], 0)
        self.assertEqual(set(d['fits']), set(k for k in RULE['kinds']))
        self.assertGreater(d['fits'][WRITER], 0)
        shutil.rmtree(self.m.root + MANAGER + '/app.slice')            # T3 never ran here
        self.assertEqual(self.facts('--kind', WRITER, '--fresh')[0], 0)

    def test_agent_scopes_count_as_writers(self):
        idle = self.facts('--kind', WRITER)[1]
        for i in range(3):
            self.m.agent(f'a{i}', 100)
        self.m.write(MANAGER + '/app.slice/app-t3code.slice/other.service/x.scope/memory.stat', 'anon 1\n')
        code, d = self.facts('--kind', WRITER, '--fresh')
        self.assertEqual(d['queue']['agents'], 3)
        self.assertEqual(d['memory']['owedMb'], 3 * (self.cost(WRITER) - 100), 'unlabelled agents owe a writer each')
        self.assertLess(d['fits'][WRITER], idle['fits'][WRITER])
        self.assertLess(d['memory']['freeMb'], idle['memory']['freeMb'])

    def test_agent_scopes_with_an_escaped_label_or_outside_the_agents_slice_count(self):
        # #800: an escaped label (systemd's \x2d) or T3 under a slice of another name never gives 0 agents
        self.m.write(AGENTS + '/t3code-my\\x2dthread-0a1b2c3d.scope/memory.stat', f'anon {100 * MB}\nshmem 0\n')
        self.m.write(AGENTS + '/run-u1.scope/memory.stat', f'anon {100 * MB}\nshmem 0\n')   # any scope in it
        self.m.write(MANAGER + '/app.slice/my-t3.slice/t3code-b-0a1b2c3d.scope/memory.stat',
                     f'anon {100 * MB}\nshmem 0\n')
        self.m.write(MANAGER + '/app.slice/app-t3code.slice/other.service/x.scope/memory.stat', 'anon 1\n')  # no
        for box in (MANAGER + '/app.slice/app-t3code.slice', AGENTS):
            self.m.write(box + '/memory.pressure', CALM)
        d = self.facts('--kind', WRITER, '--fresh')[1]
        self.assertEqual(d['queue']['agents'], 3)
        self.assertEqual(d['memory']['owedMb'], 3 * (self.cost(WRITER) - 100))
        self.assertTrue(any('t3code-b-0a1b2c3d.scope is not below an app-t3code slice' in n for n in d['notes']),
                        d['notes'])

    def test_heavy_jobs_live_waiting_stale_and_garbled(self):
        build = 'job-build' if 'job-build' in RULE['kinds'] else RULE['unknownKinds']['job']
        self.m.job('build', BUILD_MB, demand_mb=1000)                       # live, admitted
        self.m.job('build', BUILD_MB, state='waiting')                      # counted as waiting only
        self.m.job('build', BUILD_MB, hold=False)                           # its heavy is gone: not counted, not removed
        code, d = self.facts('--kind', build)
        self.assertEqual(d['queue'], {'running': 1, 'waiting': 1, 'agents': 0})
        self.assertEqual(d['memory']['owedMb'], self.cost(build) - 1000)
        self.assertEqual(len(os.listdir(os.path.join(self.m.queue, 'heavy.slice'))), 3)
        self.m.job('?', 0, raw='{not json')                             # live but unreadable: the expensive default
        d = self.facts('--fresh')[1]
        self.assertEqual(d['queue']['running'], 2)
        self.assertEqual(d['memory']['owedMb'], self.cost(build) - 1000 + self.cost(RULE['unknownKinds']['job']))

    def test_basic_tier_counts_no_agents_so_it_is_never_free(self):
        self.m.agent('a', 100)
        full = self.facts()[1]
        os.unlink(self.m.root + '/sys/fs/cgroup/cgroup.controllers')
        code, d = self.facts('--fresh')
        self.assertEqual(d['tier'], 'basic')
        self.assertEqual(d['queue']['agents'], 0)
        m = RULE['margin']
        base100 = max(m['minMb'] * 100, 16384 * m['totalPercent'])
        self.assertEqual(full['memory']['marginMb'], -(-base100 // 100))
        self.assertEqual(d['memory']['marginMb'], -(-base100 * m['noSwapOrNoCapsPercent'] // 10000))
        self.assertGreater(d['memory']['marginMb'], full['memory']['marginMb'])
        self.assertIn(hf.BASIC_WHY, d['notes'])
        for args in ((), ('--kind', WRITER), ('--kind', 'build'), ('--mb', '1')):
            code, d = self.facts(*args)
            self.assertEqual((code, d['free'], set(d['fits'].values())), (75, False, {0}), args)
            self.assertIn('basic tier', d['reason'])
        self.m.write('/sys/fs/cgroup/cgroup.controllers', 'memory\n')      # cgroup v2, no user manager, no user.slice
        shutil.rmtree(self.m.root + '/sys/fs/cgroup/user.slice')           # (a cgroup namespace, say)
        code, d = self.facts('--kind', WRITER, '--fresh')
        self.assertEqual((code, d['tier'], set(d['fits'].values())), (75, 'basic', {0}))

    def test_the_memory_cap_the_work_runs_under_is_its_room(self):
        # #951 point 1: the shared reader caps only the cgroup root; a CI runner's service with MemoryMax= (machine
        # tier: its work runs in its own cgroup) or a user-<uid>.slice with one (full tier: heavy jobs and agents
        # start under the user manager) was told OK for a size that fits the machine but not that cap
        mib = 1024 * 1024
        def cap(folder, high='max', mx='max', current=None, inactive=0):
            for name, v in (('memory.high', high), ('memory.max', mx)):
                self.m.write(f'{folder}/{name}', v if isinstance(v, str) else f'{v * mib}\n')
            if current is not None:
                self.m.write(folder + '/memory.current', f'{current * mib}\n')
                self.m.write(folder + '/memory.stat', f'anon 1\ninactive_file {inactive * mib}\n')
        self.assertEqual(self.facts('--mb', '64')[1]['memory']['availableMb'], 12288)
        user = f'/sys/fs/cgroup/user.slice/user-{UID}.slice'
        cap(user, mx=4096, current=1536, inactive=512)              # 1 GB used, 512 MB clean cache that can go
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['tier'], d['memory']['availableMb']), (0, 'full', 3072), d.get('reason'))
        self.assertTrue(any(n.startswith(f'memory cap of {user[len("/sys/fs/cgroup"):]}: 4096 MB')
                            for n in d['notes']), d['notes'])
        self.assertEqual(self.facts('--mb', '3500', '--fresh')[0], 75, 'fits the machine, not the cap')
        self.assertEqual(self.facts('--mb', '64')[1]['memory']['availableMb'], 3072, 'runs again: the cache')
        cap(user, high=2048, mx=4096, current=1536, inactive=512)  # memory.high below memory.max: the tighter one
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['availableMb'], 1024)
        cap(MANAGER, mx=512, current=0)                             # the manager's own cap, below the slice's
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['availableMb'], 512)
        for path in (MANAGER + '/memory.high', MANAGER + '/memory.max'):
            os.unlink(self.m.root + path)                           # no memory controller there: no cap
        cap(user, high='max', mx='max', current=1536)
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['availableMb'], 12288, '"max": none')
        cap(user, mx='garbage\n')                                   # a cap that cannot be read: busy for every kind
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['memory']['availableMb']), (75, None))
        self.assertTrue(any('cannot be read' in n for n in d['notes']), d['notes'])
        cap(user, mx=4096)
        os.unlink(self.m.root + user + '/memory.current')           # its use cannot be read: no room known
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['availableMb'], None)
        shutil.rmtree(self.m.root + user)                           # (user.slice stays: the machine tier)
        # the machine tier: the caller's own cgroup and every one above it
        runner = '/sys/fs/cgroup/system.slice/runner.service'
        self.m.write('/proc/self/cgroup', '0::/system.slice/runner.service\n')
        cap(runner, mx=8192, current=512)
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['tier'], d['memory']['availableMb']), (0, 'machine', 7680), d.get('reason'))
        self.assertEqual(self.facts('--mb', '7000', '--fresh')[0], 75, 'fits the machine, not the cap')
        cap('/sys/fs/cgroup/system.slice', mx=4096, current=256)
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['availableMb'], 3840)
        self.m.write('/proc/self/cgroup', '0::/system.slice/other.service\n')   # another caller, not capped itself
        d = self.facts('--mb', '64')[1]                             # the cached machine readings, its own caps
        self.assertEqual(d['memory']['availableMb'], 3840, 'its parent\'s cap holds it too')
        self.m.write('/proc/self/cgroup', '0::/user.slice/x.scope\n')            # no cap on its way
        self.assertEqual(self.facts('--mb', '64')[1]['memory']['availableMb'], 12288, 'never another\'s cached cap')
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual((d['tier'], d['memory']['availableMb']), ('machine', 12288))
        m = RULE['margin']
        wide = -(-max(m['minMb'] * 100, 16384 * m['totalPercent']) * m['noSwapOrNoCapsPercent'] // 10000)
        self.assertEqual(d['memory']['marginMb'], wide, 'the caller\'s own work runs in no heavy scope: no piece caps')

    def test_a_cgroup_path_outside_the_namespace_root_is_busy_not_uncapped(self):
        # #996 item 5: /proc/self/cgroup names a path above the cgroup namespace's root (`0::/..`) for a process
        # outside it. That path cannot be read, so its caps are unknown: busy for every kind, never "no cap" as for
        # the root itself
        shutil.rmtree(self.m.root + MANAGER)                        # the machine tier: the caller's own cgroup
        for line in ('0::/..', '0::/../other.slice', '0::/system.slice/../..', '0::/./x'):
            with self.subTest(line=line):
                self.m.write('/proc/self/cgroup', line + '\n')
                for _ in range(2):                                  # runs again: the same answer
                    code, d = self.facts('--mb', '64', '--fresh')
                    self.assertEqual((code, d['memory']['availableMb']), (75, None), d['notes'])
                    self.assertTrue(any('cannot be used' in n for n in d['notes']), d['notes'])
        for line in ('0::/', '0:://'):                              # the root (a namespace's): the shared reader's
            with self.subTest(line=line):
                self.m.write('/proc/self/cgroup', line + '\n')
                code, d = self.facts('--mb', '64', '--fresh')
                self.assertEqual((code, d['memory']['availableMb']), (0, 12288), d['notes'])

    def test_ready_refuses_a_cgroup_outside_the_namespace_at_once(self):
        # #998 item 2: a caller outside its cgroup namespace (`0::/..`, no user manager) is busy for every kind, and
        # that never clears; ready() let heavy wait its whole --wait (7200 s) for it. Now ready() refuses it at once
        def ready():
            with mock.patch.dict(os.environ, self.m.env(), clear=True), mock.patch.object(hf, 'ROOT', self.m.root):
                return hf.ready(hq, 'heavy.slice')
        shutil.rmtree(self.m.root + MANAGER)                        # the machine tier: the caller's own cgroup
        for line in ('0::/..', '0::/../other.slice', '0::/system.slice/../..', '0::/./x'):
            with self.subTest(line=line):
                self.m.write('/proc/self/cgroup', line + '\n')
                for _ in range(2):                                  # runs again: the same answer
                    why = ready() or ''
                    self.assertIn('outside this cgroup namespace', why)
                    self.assertIn('never free', why)
        for text in ('0::/\n', '0::/user.slice/x.scope\n', '', '1:memory:/x\n'):    # root, plain, empty, v1 alone
            with self.subTest(text=text):
                self.m.write('/proc/self/cgroup', text)
                self.assertIsNone(ready())
        os.unlink(self.m.root + '/proc/self/cgroup')                # no /proc: nothing above the root to refuse
        self.assertIsNone(ready())

    def test_ready_refuses_a_basic_tier_machine_at_once(self):
        # #981 leftover 2: a basic tier is never free, so a plain heavy there waited its whole --wait (7200 s) for an
        # answer that never comes; ready() (asked before any wait) names it, as it names a kit that cannot answer
        def ready(slice_name='heavy.slice'):
            with mock.patch.dict(os.environ, self.m.env(), clear=True), mock.patch.object(hf, 'ROOT', self.m.root):
                return hf.ready(hq, slice_name)
        self.assertIsNone(ready())
        self.assertIsNone(ready(), 'runs again: the same answer')
        box = self.m.root + MANAGER + '/heavy.slice'
        shutil.rmtree(box)                                          # no heavy job ran yet: no box, still fine
        self.assertIsNone(ready())
        controllers = self.m.root + '/sys/fs/cgroup/cgroup.controllers'
        os.unlink(controllers)                                      # no cgroup v2
        self.assertIn('basic tier', ready() or '')
        self.assertIn('no cgroup v2', ready())
        self.m.write('/sys/fs/cgroup/cgroup.controllers', '')       # v2, empty: still v2
        self.assertIsNone(ready())
        shutil.rmtree(self.m.root + MANAGER)                        # no manager, user.slice: the machine tier
        self.assertIsNone(ready())
        shutil.rmtree(self.m.root + '/sys/fs/cgroup/user.slice')    # neither
        self.assertIn('basic tier', ready() or '')
        os.makedirs(box)
        if os.geteuid() != 0:                                       # root lists every folder
            os.chmod(controllers, 0)                                # unreadable: not known to be v2
            self.addCleanup(os.chmod, controllers, 0o644)
            self.assertIn('basic tier', ready() or '')
            os.chmod(controllers, 0o644)
            os.chmod(box, 0)                                        # a heavy box that cannot be listed
            self.addCleanup(os.chmod, box, 0o755)
            self.assertIn('cannot be listed', ready() or '')
            os.chmod(box, 0o755)
            self.assertIsNone(ready())
        self.assertIsNone(ready(None), 'no slice named (another caller): no box to list')

    def test_no_user_manager_gets_the_machine_wide_answer(self):
        # #844: a CI runner's user (no lingering, no login session) has no user manager; every other user's agents
        # and heavy jobs are still read from user.slice, so it gets the answer a caller with a manager gets
        self.m.write(OTHER + '/heavy.slice/run-x1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        self.m.write(OTHER + '/heavy.slice/memory.pressure', CALM)
        self.m.write(OTHER + '/app.slice/app-t3code.slice/t3code-z-1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        answers = {}
        for tier in ('full', 'machine'):
            if tier == 'machine':
                shutil.rmtree(self.m.root + MANAGER)
            answers[tier] = [self.facts(*args, '--fresh') for args in (('--kind', WRITER), ('--mb', '1024'))]
        m = RULE['margin']
        wide = -(-max(m['minMb'] * 100, 16384 * m['totalPercent']) * m['noSwapOrNoCapsPercent'] // 10000)
        for (code, d), (full_code, full) in zip(answers['machine'], answers['full']):
            self.assertEqual((code, d['free'], d['tier'], d['level']), (0, True, 'machine', 'ok'), d.get('reason'))
            self.assertEqual((full_code, full['tier']), (0, 'full'))
            self.assertEqual(d['queue'], full['queue'])
            # #951: the caller's own work runs in no heavy scope, so no piece caps: the rule's wider margin
            self.assertEqual(d['memory']['marginMb'], wide)
            self.assertGreater(wide, full['memory']['marginMb'])
            same = ('availableMb', 'reservedMb', 'owedMb', 'holdMb', 'costMb', 'swapFreeMb', 'swapUsedPercent')
            self.assertEqual({k: d['memory'][k] for k in same}, {k: full['memory'][k] for k in same})
            self.assertEqual(d['memory']['freeMb'], full['memory']['freeMb'] - (wide - full['memory']['marginMb']))
            for k in d['fits']:
                self.assertLessEqual(d['fits'][k], full['fits'][k], k)
            self.assertEqual(d['queue'], {'running': 1, 'waiting': 0, 'agents': 1})
            self.assertIn(hf.MACHINE_WHY, d['notes'])
            self.assertNotIn(hf.BASIC_WHY, d['notes'])
        code, d = self.facts('--mb', '99999', '--fresh')
        self.assertEqual((code, d['tier']), (75, 'machine'))
        self.assertIn('machine-wide numbers', d['reason'])
        self.m.write(OTHER + '/app.slice/app-t3code.slice/t3code-z-2.scope/memory.stat', 'anon 0\nshmem 0\n')
        d = self.facts('--kind', WRITER, '--fresh')[1]
        self.assertEqual(d['queue']['agents'], 2, 'still counts every user\'s agents')

    def test_the_machine_and_full_tiers_see_every_person_s_reservations(self):
        # #945: a caller with no user manager is often another user (a CI runner, root) with another home: it reads
        # every person's reserve.d, so it never takes the room the owner keeps; one it cannot read: never free
        owner_home = '/home/owner'
        uid = 4000000001
        self.m.write('/etc/login.defs', 'UID_MIN 1000\nUID_MAX 4294967294\n')
        self.m.write('/etc/passwd', f'root:x:0:0::/root:/bin/sh\nowner:x:{uid}:{uid}::{owner_home}:/bin/sh\n'
                                    f'me:x:{UID}:{UID}::/home/me:/bin/sh\n')
        os.chmod(self.m.root + '/etc', 0o755)        # the machine file's folders: no one else can write them
        game = os.path.join(self.tmp, 'game')
        conf = owner_home + '/.config/t3-capacity/reserve.d'      # in the fake machine
        owner_reserve = self.m.root + conf
        self.m.write(conf + '/game.conf', f'1536 {game}/\n')
        self.m.write('/root/.config/t3-capacity/reserve.d/x.conf', '9999 /x/\n')       # root: not read
        self.m.write('/home/me/.config/t3-capacity/reserve.d/x.conf', '9999 /x/\n')    # the caller's: its own dir
        self.m.process(4001, game, 300)             # the caller's process in the owner's folder: not the owner's use
        os.makedirs(self.m.root + OTHER)
        full = self.facts('--mb', '64')[1]
        self.assertEqual((full['tier'], full['memory']['holdMb']), ('full', 1536), '#800: the full tier sees it too')
        if os.geteuid() != 0:                       # #800: in the full tier one that cannot be read is a note
            os.chmod(owner_reserve, 0)
            code, d = self.facts('--mb', '64', '--fresh')
            os.chmod(owner_reserve, 0o755)
            self.assertEqual((code, d['tier'], d['memory']['holdMb']), (0, 'full', 0), d.get('reason'))
            self.assertIn('reservations of user owner cannot be read (Permission denied): not counted', d['notes'])
        shutil.rmtree(self.m.root + MANAGER)
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['tier']), (0, 'machine'), d.get('reason'))
        self.assertEqual(d['memory']['holdMb'], 1536, (d['reservations'], d['notes']))
        self.assertEqual([(r['name'], r['mb'], r['useMb']) for r in d['reservations']], [('owner:game', 1536, 0)])
        room = d['memory']['freeMb']
        os.unlink(owner_reserve + '/game.conf')
        free = self.facts('--mb', '64', '--fresh')[1]['memory']['freeMb']
        self.assertEqual(free, room + 1536)
        self.m.write(conf + '/game.conf', f'1536 {game}/\n')
        code, d = self.facts('--mb', str(room + 1), '--fresh', cwd=self.work)
        self.assertEqual(code, 75, 'it fits only in the owner\'s reserved room')
        code, d = self.facts('--mb', str(room + 1), '--fresh', cwd=game)
        self.assertEqual(code, 0, 'a caller working in the reserved folder is the work it is kept for')
        os.mkfifo(owner_reserve + '/pipe.conf')     # never waits on a FIFO; not a plain file: cannot be read
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['tier'], set(d['fits'].values())), (75, 'basic', {0}))
        self.assertTrue(any(n.startswith('reservation owner:pipe.conf cannot be read') for n in d['notes']), d['notes'])
        os.unlink(owner_reserve + '/pipe.conf')
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['tier'], 'machine')
        if os.geteuid() != 0:                       # root reads every folder
            os.chmod(owner_reserve, 0)              # a home closed to others (mode 750 or 700)
            self.addCleanup(os.chmod, owner_reserve, 0o755)
            code, d = self.facts('--mb', '64', '--fresh')
            self.assertEqual((code, d['tier']), (75, 'basic'))
            self.assertTrue(any('reservations of user owner cannot be read' in n for n in d['notes']), d['notes'])
            os.chmod(owner_reserve, 0o755)
        os.makedirs(self.m.root + '/sys/fs/cgroup/user.slice/user-4000000002.slice')   # a session, no home known
        code, d = self.facts('--mb', '64', '--fresh')
        self.assertEqual((code, d['tier']), (75, 'basic'))
        self.assertTrue(any('user 4000000002 has a session' in n for n in d['notes']), d['notes'])

    def test_a_home_behind_an_automount_is_read_only_for_a_person_with_a_session(self):
        # #951: the machine tier read every passwd home on every call, so an autofs /home mounted them all; a home
        # behind a trigger that is not mounted is read only while its person has a session (then it is mounted)
        far, near = 4000000003, 4000000004
        self.m.write('/etc/login.defs', 'UID_MIN 1000\nUID_MAX 4294967294\n')
        self.m.write('/etc/passwd', f'far:x:{far}:{far}::/net/home/far:/bin/sh\n'
                                    f'near:x:{near}:{near}::/net/home/near/:/bin/sh\n')
        os.chmod(self.m.root + '/etc', 0o755)
        for name, mb in (('far', 1024), ('near', 512)):
            self.m.write(f'/net/home/{name}/.config/t3-capacity/reserve.d/r.conf', f'{mb} /{name}/\n')
        mounts = ('22 1 0:20 / / rw - ext4 /dev/vda1 rw\n'
                  '40 22 0:40 / /net/home rw,relatime - autofs systemd-1 rw,fd=5\n'
                  '41 40 0:41 / /net/home/near rw - nfs4 srv:/near rw\n')
        self.m.write('/proc/self/mountinfo', mounts)
        shutil.rmtree(self.m.root + MANAGER)        # the machine tier
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(d['tier'], 'machine', d.get('reason'))
        self.assertEqual([r['name'] for r in d['reservations']], ['near:r'], d['notes'])
        self.assertIn('reservations not read for 1 people without a session whose home is not mounted (autofs): far',
                      d['notes'])
        os.makedirs(self.m.root + f'/sys/fs/cgroup/user.slice/user-{far}.slice')     # far logs in: it is mounted
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(sorted(r['name'] for r in d['reservations']), ['far:r', 'near:r'], d['notes'])
        self.assertFalse(any('autofs' in n for n in d['notes']), d['notes'])
        os.rmdir(self.m.root + f'/sys/fs/cgroup/user.slice/user-{far}.slice')
        for broken in ('', 'not a mount table\n'):  # empty or unreadable mount table: every home read, as before
            self.m.write('/proc/self/mountinfo', broken)
            d = self.facts('--mb', '64', '--fresh')[1]
            self.assertEqual(sorted(r['name'] for r in d['reservations']), ['far:r', 'near:r'], (broken, d['notes']))

    def automount_people(self, far_home='/net/home/far', near_home='/net/home/near'):
        """far and near (no session), homes behind the autofs /net/home, each with a reservation; their uids."""
        far, near = 4000000003, 4000000004
        self.m.write('/etc/login.defs', 'UID_MIN 1000\nUID_MAX 4294967294\n')
        self.m.write('/etc/passwd', f'far:x:{far}:{far}::{far_home}:/bin/sh\n'
                                    f'near:x:{near}:{near}::{near_home}:/bin/sh\n')
        os.chmod(self.m.root + '/etc', 0o755)
        for name, home in (('far', far_home), ('near', near_home)):
            self.m.write(f'{home}/.config/t3-capacity/reserve.d/r.conf', f'512 /{name}/\n')
        return far, near

    def test_a_hung_home_is_given_up_in_time_and_never_counts_as_free(self):
        # #998 item 4: every person's reserve.d was read with no time limit, so one hung network or autofs home
        # stalled every `heavy facts` call (and so every heavy start, t3-capacity and agent-slots). Each home now has
        # HOME_SECONDS: one not read in time is unread, as a home that cannot be read is (never free in the machine
        # tier, a note in the full tier); the homes after it are still read
        self.automount_people()                     # far and near, each with a reservation
        self.m.write('/proc/self/mountinfo', '22 1 0:20 / / rw - ext4 /dev/vda1 rw\n')    # no automount: all read
        hung = self.m.root + '/net/home/far/.config/t3-capacity/reserve.d'
        real = hf.reservation_files

        def slow(cfg_dir, *args, **kw):
            if cfg_dir == hung:
                time.sleep(8)                       # (a hung NFS read never returns; this one ends the test's wait)
            return real(cfg_dir, *args, **kw)

        def facts():
            start = time.monotonic()
            with mock.patch.object(hf, 'HOME_SECONDS', 0.5, create=True), \
                    mock.patch.object(hf, 'reservation_files', slow):
                code, d = self.facts('--mb', '64', '--fresh')
            self.assertLess(time.monotonic() - start, 4, 'it waited for the hung home')
            return code, d
        for tier in ('full', 'machine'):
            with self.subTest(tier=tier):
                if tier == 'machine':
                    shutil.rmtree(self.m.root + MANAGER)
                for _ in range(2):                  # runs again: the same answer, never the whole wait
                    code, d = facts()
                    self.assertEqual([r['name'] for r in d['reservations']], ['near:r'], d['notes'])
                    self.assertTrue(any(n.startswith('reservations of user far cannot be read (not read within 0.5 s')
                                        for n in d['notes']), d['notes'])
                    self.assertEqual((code, d['tier']), (75, 'basic') if tier == 'machine' else (0, 'full'))
        d = self.facts('--mb', '64', '--fresh')[1]  # the home answers again: both read
        self.assertEqual((d['tier'], sorted(r['name'] for r in d['reservations'])), ('machine', ['far:r', 'near:r']))
        self.assertFalse(any('not read within' in n for n in d['notes']), d['notes'])

    def test_a_mounted_home_with_a_non_ascii_name_is_read(self):
        # #996 item 4: the mount table was decoded as ASCII and its octal escapes made one character per byte, so a
        # mounted home with a non-ASCII name (/net/home/jürgen: \303\274 in the table) did not match its passwd
        # line and read as "behind an unmounted autofs": the reservation of that person was not read
        self.automount_people(near_home='/net/home/jürgen')
        self.m.write('/proc/self/mountinfo', '22 1 0:20 / / rw - ext4 /dev/vda1 rw\n'
                                             '40 22 0:40 / /net/home rw - autofs systemd-1 rw,fd=5\n'
                                             '41 40 0:41 / /net/home/j\\303\\274rgen rw - nfs4 srv:/j rw\n')
        shutil.rmtree(self.m.root + MANAGER)        # the machine tier
        for _ in range(2):                          # runs again: the same answer
            d = self.facts('--mb', '64', '--fresh')[1]
            self.assertEqual([r['name'] for r in d['reservations']], ['near:r'], d['notes'])
            self.assertIn('reservations not read for 1 people without a session whose home is not mounted (autofs): '
                          'far', d['notes'])

    def test_a_mount_table_cut_at_its_limit_is_named_and_reads_every_home(self):
        # #996 item 4: the mount table was cut at 4 MiB without a note, so a home whose mount line fell past the cut
        # read as "behind an unmounted autofs" and was skipped. A table longer than the limit is read as unreadable
        # (every home read, as before #951), with a note
        self.automount_people()
        head = '22 1 0:20 / / rw - ext4 /dev/vda1 rw\n40 22 0:40 / /net/home rw - autofs systemd-1 rw,fd=5\n'
        filler = ''.join(f'{100 + i} 22 0:99 / /mnt/filler-{i:06d} rw - tmpfs tmpfs rw\n' for i in range(90000))
        self.assertGreater(len(head + filler), hf.MOUNTS_LIMIT)
        self.m.write('/proc/self/mountinfo', head + filler + '41 40 0:41 / /net/home/near rw - nfs4 srv:/near rw\n')
        shutil.rmtree(self.m.root + MANAGER)        # the machine tier
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(sorted(r['name'] for r in d['reservations']), ['far:r', 'near:r'], d['notes'])
        self.assertTrue(any('mount table' in n and 'every home is read' in n for n in d['notes']), d['notes'])

    def test_sessions_that_cannot_be_listed_never_skip_a_home(self):
        # #996 item 6: when user.slice could not be listed, no one had a session, so the automount rule skipped the
        # homes of people who are logged in too. Sessions that cannot be known: every home is read (as with an
        # unreadable mount table), with a note. (heavy facts answers busy then anyway, as other_users is blind; the
        # list of homes is asked here directly so the rule holds on its own.)
        far, near = self.automount_people()
        self.m.write('/proc/self/mountinfo', '22 1 0:20 / / rw - ext4 /dev/vda1 rw\n'
                                             '40 22 0:40 / /net/home rw - autofs systemd-1 rw,fd=5\n')
        os.makedirs(self.m.root + f'/sys/fs/cgroup/user.slice/user-{far}.slice')     # far is logged in
        users = self.m.root + '/sys/fs/cgroup/user.slice'

        def homes():
            with mock.patch.object(hf, 'ROOT', self.m.root):
                dirs, blind, notes = hf.other_reserve_dirs(UID)
            return [n for n, _, _ in dirs], blind, notes
        self.assertEqual(homes(), (['far'], [], ['reservations not read for 1 people without a session whose home is '
                                                 'not mounted (autofs): near']))
        os.chmod(users, 0o300)                      # cannot be listed (its folders can still be reached)
        self.addCleanup(os.chmod, users, 0o755)
        for _ in range(2):                          # runs again: the same answer
            names, blind, notes = homes()
            self.assertEqual((names, blind), (['far', 'near'], []))
            self.assertEqual(len(notes), 1, notes)
            self.assertIn('sessions cannot be known', notes[0])
        os.chmod(users, 0o755)                      # listed again: back to the rule
        self.assertEqual(homes()[0], ['far'])
        os.unlink(self.m.root + '/proc/self/mountinfo')   # no mount table either: every home, as before #951
        self.assertEqual(homes(), (['far', 'near'], [], []))

    def test_an_unreadable_signal_is_elevated_never_ok(self):
        self.m.write('/proc/pressure/memory', 'some garbage\nfull avg10=x\n')
        code, d = self.facts('--mb', '64')
        self.assertEqual((code, d['free'], d['level']), (75, False, 'elevated'))
        self.m.write('/proc/pressure/memory', CALM)
        self.m.write(MANAGER + '/heavy.slice/memory.pressure', 'nonsense\n')
        os.unlink(self.m.cache)                     # (else the elevated reading carries: calm must last first)
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'ok', 'no job of its own: the box passes nothing')
        self.m.job('build', BUILD_MB, demand_mb=100)
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['level'], 'elevated')
        os.unlink(self.m.root + MANAGER + '/heavy.slice/memory.pressure')     # a box without its PSI file
        os.unlink(self.m.cache)
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'elevated')
        shutil.rmtree(self.m.root + MANAGER + '/heavy.slice')                 # a live job, its box missing
        os.unlink(self.m.cache)
        code, d = self.facts('--mb', '64')
        self.assertEqual(d['level'], 'elevated')
        self.assertEqual(d['memory']['owedMb'], self.cost('job-build'), 'its scope is missing: it owes in full')
        for name in os.listdir(os.path.join(self.m.queue, 'heavy.slice')):    # the job ended: no box, no signal
            os.unlink(os.path.join(self.m.queue, 'heavy.slice', name))
        os.unlink(self.m.cache)
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'ok')
        self.m.agent('a', 100)
        os.unlink(self.m.root + MANAGER + '/app.slice/app-t3code.slice/memory.pressure')
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['level'], 'ok', 'the job box answers a --mb ask')
        self.assertEqual(self.facts('--kind', WRITER, '--fresh')[1]['level'], 'elevated')
        os.unlink(self.m.root + '/proc/pressure/memory')                     # a kernel without PSI: none anywhere
        self.assertEqual(self.facts('--kind', WRITER, '--fresh', now=NOW + datetime.timedelta(hours=1))[1]['level'],
                         'ok')
        self.m.write('/proc/meminfo', 'MemTotal: 16777216 kB\n')                # facts that cannot be read: busy
        code, d = self.facts('--kind', WRITER, '--fresh')
        self.assertEqual((code, d['free'], d['memory']['freeMb']), (75, False, None))
        self.assertIn('unreadable', d['reason'])
        self.assertEqual(set(d['fits'].values()), {0})

    def test_the_mb_question_and_the_answer_is_free_v1(self):
        fc = hf.load('free_contract', 'common/free_contract.py')
        code, d = self.facts('--mb', '1024')
        free_mb = d['memory']['freeMb']
        self.assertEqual(code, 0)
        self.assertEqual(self.facts('--mb', str(free_mb))[0], 0)
        code, d = self.facts('--mb', str(free_mb + 1))
        self.assertEqual(code, 75)
        self.assertEqual(fc.judge(code, json.dumps(d).encode(), NOW)[0], 'busy')
        self.assertLessEqual(len(json.dumps(d)), fc.MAX_BYTES)
        self.assertEqual(self.facts('--kind', 'build')[1]['kind'],
                         'job-build' if 'job-build' in RULE['kinds'] else RULE['unknownKinds']['job'])

    def test_wrong_calls(self):
        for args in (['--kind'], ['--kind', 'Bad Kind'], ['--mb', 'x'], ['--mb', '1', '--mb', '2'], ['extra']):
            self.assertEqual(self.facts(*args)[0], 2, args)


class Reservations(Case):
    def test_own_other_ended_broken_and_missing(self):
        mine, other = os.path.join(self.tmp, 'mine'), os.path.join(self.tmp, 'other')
        self.m.process(4001, other + '/sub', 300)
        self.m.process(4002, os.path.join(self.tmp, 'otherwise'), 900)      # prefix of a word: not under other/
        with open(os.path.join(self.m.reserve, 'a.conf'), 'w') as f:
            f.write(f'# comment\n\n2000 {other}/\n1000 {mine}/\n')
        with open(os.path.join(self.m.reserve, 'b.conf'), 'w') as f:
            f.write(f'500 {other}/ until=2026-11-19\nlots of words\n700 relative/path\n')
        base = Case.facts(self, '--mb', '64', cwd=self.work)[1]
        os.makedirs(mine + '/deep', exist_ok=True)
        code, d = self.facts('--mb', '64', '--fresh', cwd=mine + '/deep')
        held = {(r['name'], r['mb']): (r['useMb'], r['own']) for r in d['reservations']}
        self.assertEqual(held, {('a', 2000): (300, False), ('a', 1000): (0, True)})
        self.assertEqual(d['memory']['holdMb'], 2000 - 300, 'own reservation does not hold its caller back')
        self.assertEqual(base['memory']['holdMb'], 2000 - 300 + 1000)
        self.assertEqual(sorted(n.split(' ')[1] for n in d['notes']), ['b.conf:1', 'b.conf:2', 'b.conf:3'])
        shutil.rmtree(self.m.reserve)
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 0)

    def test_a_reservation_file_that_cannot_be_read_holds_nothing(self):
        with open(os.path.join(self.m.reserve, 'bin.conf'), 'wb') as f:
            f.write(b'\xff\xfe 1000 /x\n')
        d = self.facts()[1]
        self.assertEqual(d['memory']['holdMb'], 0)
        self.assertIn('cannot be read', d['notes'][0])


    def test_a_reservation_file_cut_short_holds_its_whole_lines(self):
        other = os.path.join(self.tmp, 'other')
        with open(os.path.join(self.m.reserve, 'half.conf'), 'w') as f:
            f.write(f'1000 {other}/\n20')               # its writer stopped half-way through the second line
        d = self.facts('--mb', '64')[1]
        self.assertEqual(d['memory']['holdMb'], 1000)
        self.assertEqual([n.split(' ')[1] for n in d['notes']], ['half.conf:2'])

    def test_an_agent_or_job_working_in_the_folder_is_counted_once(self):
        # #923: one idle 300 MB agent under a 1536 MB reservation holds 1536 MB in all (its use and its debt to the
        # rule together), not its debt on top of the reservation's 1536 - 300
        other = os.path.join(self.tmp, 'other')
        with open(os.path.join(self.m.reserve, 'game.conf'), 'w') as f:
            f.write(f'1536 {other}/\n')
        self.m.process(4001, other + '/sub', 300)
        self.m.agent('a', 300, pids=(4001,))
        code, d = self.facts('--mb', '64')
        owed = self.cost(WRITER) - 300
        self.assertLessEqual(self.cost(WRITER), 1536, 'the example needs a writer that fits the reservation')
        self.assertEqual(d['memory']['owedMb'], owed)
        self.assertEqual(d['memory']['holdMb'], 1536 - 300 - owed)
        self.assertEqual(d['memory']['owedMb'] + d['memory']['holdMb'] + 300, 1536)
        self.assertEqual([(r['useMb'], r['owedMb']) for r in d['reservations']], [(300, owed)])
        self.m.agent('b', 100, pids=(4002,))         # an agent working elsewhere: its debt is its own
        self.m.process(4002, os.path.join(self.tmp, 'elsewhere'), 100)
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual((d['memory']['holdMb'], d['memory']['owedMb']),
                         (1536 - 300 - owed, owed + self.cost(WRITER) - 100))
        build = 'job-build' if 'job-build' in RULE['kinds'] else RULE['unknownKinds']['job']
        self.m.job('build', BUILD_MB, demand_mb=200)    # a heavy job started in the folder: never held twice either
        self.m.write(f'{MANAGER}/heavy.slice/run-j1.scope/sub/cgroup.procs', '4003\n')
        self.m.process(4003, other, 200)
        d = self.facts('--mb', '64', '--fresh')[1]
        job_owed = self.cost(build) - 200
        self.assertEqual(d['memory']['holdMb'], max(0, 1536 - 500 - owed - job_owed))
        self.assertEqual(d['reservations'][0]['owedMb'], owed + job_owed)

    def reserve(self, name, *lines):
        with open(os.path.join(self.m.reserve, f'{name}.conf'), 'w') as f:
            f.write(''.join(f'{mb} {folder}/\n' for mb, folder in lines))

    def spanning(self):
        """Two reserved folders a (4096) and b (2048), one folder of b's reserved again below it (b/y, 1024), and
        work across them: an agent (demand 400) with a 300 MB process in a and a 100 MB one in b/y, and a heavy
        build (demand 200) with 150 MB in a and 50 MB in b. (reserved MB in all, MB its processes use, debts)."""
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        self.reserve('a', (4096, a))
        self.reserve('b', (2048, b), (1024, b + '/y'))
        self.m.agent('w', 400, pids=(4001, 4002))
        self.m.process(4001, a + '/src', 300)
        self.m.process(4002, b + '/y/z', 100)
        self.m.job('build', BUILD_MB, demand_mb=200)
        self.m.write(f'{MANAGER}/heavy.slice/run-j1.scope/cgroup.procs', '4003\n4004\n')
        self.m.process(4003, a, 150)
        self.m.process(4004, b, 50)
        build = 'job-build' if 'job-build' in RULE['kinds'] else RULE['unknownKinds']['job']
        return 4096 + 2048 + 1024, 600, (self.cost(WRITER) - 400, self.cost(build) - 200)

    def test_two_folders_nested_folders_and_work_across_them_count_each_debt_once(self):
        # #945: a piece with processes in two reserved folders, or in nested ones, had its whole debt taken off each,
        # so it was counted zero times. Now every process counts toward one reservation (the deepest), and a piece's
        # debt is split over its processes by their use: held + owed + used is exactly what is reserved
        total, used, (agent, job) = self.spanning()
        self.assertTrue(agent % 4 == 0 and job % 4 == 0, 'the shares below are whole MB')
        code, d = self.facts('--mb', '64')
        mem = d['memory']
        self.assertEqual(mem['owedMb'], agent + job)
        self.assertEqual(mem['holdMb'] + mem['owedMb'] + used, total, d['reservations'])
        got = {r['mb']: (r['useMb'], r['owedMb']) for r in d['reservations']}
        self.assertEqual(got, {4096: (450, agent * 3 // 4 + job * 3 // 4), 2048: (50, job // 4),
                               1024: (100, agent // 4)})
        # the same folder in two files: the first by name takes it, never both
        shutil.rmtree(self.m.reserve)
        os.makedirs(self.m.reserve)
        a = os.path.join(self.tmp, 'a')
        self.reserve('a', (4096, a))
        self.reserve('a2', (4096, a))
        d = self.facts('--mb', '64', '--fresh')[1]
        got = [(r['name'], r['useMb'], r['owedMb']) for r in d['reservations']]
        self.assertEqual(got, [('a', 450, agent * 3 // 4 + job * 3 // 4), ('a2', 0, 0)])
        self.assertEqual(d['memory']['holdMb'] + sum(r['owedMb'] for r in d['reservations']) + 450, 8192)

    def test_never_looser_than_main_beyond_counting_each_debt_once(self):
        # main held each reservation's MB less every matching process's use, and counted every debt again on top.
        # For the same facts the answer here may only be looser by the debts of the pieces working in reserved
        # folders (now counted once, #923), each piece once; with no piece working in one it is never looser at all
        a, b = os.path.join(self.tmp, 'a'), os.path.join(self.tmp, 'b')
        self.reserve('a', (2048, a))
        self.reserve('b', (2048, b), (1024, b + '/y'))
        self.m.process(4101, b + '/y', 300)         # not an agent's or a job's: in b/y and (as main saw it) in b
        self.m.process(4102, a, 200)

        def main_hold(procs):
            """main's hold: each reservation's MB less every matching process's use."""
            res = [(2048, a), (2048, b), (1024, b + '/y')]
            return sum(max(0, mb - sum(rss for cwd, rss in procs if (cwd + '/').startswith(f + '/')))
                       for mb, f in res)
        base = [(b + '/y', 300), (a, 200)]
        d = self.facts('--mb', '64')[1]
        main_free = d['memory']['freeMb'] + d['memory']['holdMb'] - main_hold(base)
        self.assertLessEqual(d['memory']['freeMb'], main_free)
        code, d = self.facts('--mb', str(main_free + 1), '--fresh')
        self.assertEqual(code, 75, 'main said NOT OK: so does this')
        # an agent working across a and b/y: looser than main by at most its debt, once
        self.m.agent('w', 400, pids=(4001, 4002))
        self.m.process(4001, a + '/src', 300)
        self.m.process(4002, b + '/y/z', 100)
        d = self.facts('--mb', '64', '--fresh')[1]
        procs = base + [(a + '/src', 300), (b + '/y/z', 100)]
        main_free = d['memory']['freeMb'] + d['memory']['holdMb'] - main_hold(procs)
        self.assertLessEqual(d['memory']['freeMb'], main_free + self.cost(WRITER) - 400)
        code, d = self.facts('--mb', str(main_free + self.cost(WRITER) - 400 + 1), '--fresh')
        self.assertEqual(code, 75)

    def test_a_reservation_whose_processes_are_gone_still_holds_it_all(self):
        other = os.path.join(self.tmp, 'other')
        self.m.process(4001, other, 300)
        with open(os.path.join(self.m.reserve, 'game.conf'), 'w') as f:
            f.write(f'2000 {other}/\n')
        self.assertEqual(self.facts('--mb', '64')[1]['memory']['holdMb'], 1700)
        shutil.rmtree(f'{self.m.root}/proc/4001')    # its process ended: the room is still kept for it
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 2000)


    def reserve_line(self, line, age_s=0):
        """One reservation file written age_s seconds before NOW (its mtime: what ttl= and idle= count from)."""
        path = os.path.join(self.m.reserve, 'r.conf')
        with open(path, 'w') as f:
            f.write(line + '\n')
        os.utime(path, (NOW.timestamp() - age_s,) * 2)
        return path

    def test_a_reservation_by_kind_is_sized_from_the_rule_and_heavy_s_peaks(self):
        # #861: sized from measured use per job kind, not a guessed MB that is held for good
        folder = os.path.join(self.tmp, 'game')
        self.reserve_line(f'build {folder}/')
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual([(r['name'], r['mb']) for r in d['reservations']], [('r', self.cost('job-build'))])
        state = os.path.join(self.m.home, '.local', 'state', 'heavy')
        os.makedirs(state)
        with open(os.path.join(state, 'peaks.json'), 'w') as f:
            json.dump({'v': 1, 'kinds': {'build': [self.cost('job-build') * 2]}}, f)
        learned = hq.reservation('build', self.cost('job-build'), state, out=lambda m: None)[0]
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['reservations'][0]['mb'], learned)
        self.reserve_line(f'tsc {folder}/')                  # a kind the rule does not know: the expensive job default
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['reservations'][0]['mb'],
                         self.cost(RULE['unknownKinds']['job']))

    def test_ttl_ends_a_reservation_its_writer_stopped_renewing(self):
        folder = os.path.join(self.tmp, 'game')
        path = self.reserve_line(f'1536 {folder}/ ttl=2h', age_s=2 * 3600 + 1)
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(d['memory']['holdMb'], 0)
        self.assertTrue(any('r.conf:1 ended: its ttl=' in n for n in d['notes']), d['notes'])
        os.utime(path, (NOW.timestamp() - 60,) * 2)     # renewed (touched) a minute ago
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 1536)
        os.utime(path, (NOW.timestamp() + 3600,) * 2)   # written "in the future" (a clock that went back): holds
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 1536)

    def test_idle_releases_a_reservation_nobody_works_in(self):
        # #861: 1536 MB held while using 0 for a long time kept other work out for memory nobody used
        folder = os.path.join(self.tmp, 'game')
        os.makedirs(folder)
        path = self.reserve_line(f'1536 {folder}/ idle=30m', age_s=31 * 60)
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(d['memory']['holdMb'], 0)
        self.assertTrue(any('reservation r is idle' in n for n in d['notes']), d['notes'])
        self.m.process(4001, folder + '/sub', 0)        # someone works there again (using nothing yet): it holds
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 1536)
        shutil.rmtree(f'{self.m.root}/proc/4001')
        os.utime(path, (NOW.timestamp() - 60,) * 2)     # nobody there, but written a minute ago: it holds
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 1536)

    def test_idle_keeps_holding_while_someone_works_in_a_deeper_reservation_s_folder(self):
        # a process counts its use toward the deepest reservation only (#945), but it works in the outer folder too:
        # the outer one's idle= must not release it while work goes on below it
        folder = os.path.join(self.tmp, 'game')
        self.reserve_line(f'1536 {folder}/ idle=30m', age_s=31 * 60)
        inner = os.path.join(self.m.reserve, 'inner.conf')
        with open(inner, 'w') as f:
            f.write(f'512 {folder}/sub/\n')
        os.utime(inner, (NOW.timestamp(),) * 2)
        self.m.process(4001, folder + '/sub/deep', 0)
        d = self.facts('--mb', '64', '--fresh')[1]
        self.assertEqual(d['memory']['holdMb'], 1536 + 512, d['notes'])
        self.assertFalse(any('reservation r is idle' in n for n in d['notes']), d['notes'])
        shutil.rmtree(f'{self.m.root}/proc/4001')      # nobody works anywhere below it now: the outer one is idle
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 512)

    def test_a_bad_or_unknown_option_never_makes_a_line_hold_less(self):
        folder = os.path.join(self.tmp, 'game')
        for line in (f'1536 {folder}/ ttl=soon', f'1536 {folder}/ idle=-1m', f'1536 {folder}/ # the game',
                     f'1536 {folder}/ ttl=', f'1536 {folder}/ owner=me'):
            with self.subTest(line=line):
                self.reserve_line(line, age_s=86400 * 30)
                self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 1536)

    def test_idle_is_not_applied_to_a_reservation_whose_processes_cannot_be_seen(self):
        # another person's reservation (the machine tier): a caller that is not root cannot see their working
        # folders, so it cannot tell idle from busy: it holds (stricter, never looser)
        if os.geteuid() == 0:
            self.skipTest('root sees every folder')
        folder = os.path.join(self.tmp, 'game')
        r = {'name': 'owner:game', 'prefixes': [folder + '/'], 'mb': 1536, 'useMb': 0, 'owedMb': 0,
             'uid': os.getuid() + 1, 'idleAt': 0, 'procs': 0}
        self.assertEqual(hf.release_idle([r], NOW.timestamp()), [])
        self.assertEqual(r['mb'], 1536)



class Holds(Case):
    """heavy.capacity.holds of the machine file: a cgroup outside the user managers (here a CI runners' slice) that
    is in use keeps its budget less its use for every other user."""
    CG = '/sys/fs/cgroup/ci.slice/ci-runners.slice'

    def holds(self, *holds):
        self.machine_file({'version': 1, 'heavy': {'capacity': {'holds': list(holds)}}})

    def runners(self, anon_mb, limit='4294967296'):
        self.m.write(self.CG + '/memory.stat', f'anon {anon_mb * MB}\nshmem 0\n')
        if limit is not None:
            self.m.write(self.CG + '/memory.max', f'{limit}\n')

    def held(self, *args):
        code, d = self.facts('--mb', '64', '--fresh', *args)
        return code, d, {r['name']: (r['mb'], r['useMb']) for r in d['reservations']} if d else None

    def test_a_cgroup_in_use_holds_its_budget_less_its_use(self):
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.runners(2000)
        code, d, held = self.held()
        self.assertEqual((code, held), (0, {'ci-slice': (4096, 2000)}))
        self.assertEqual(d['memory']['holdMb'], 4096 - 2000)
        self.runners(1000)                          # at most inUseFromMb (1024): idle, holds nothing
        self.assertEqual(self.held()[2], {})
        self.runners(5000)                          # more than its budget: holds nothing more
        code, d, held = self.held()
        self.assertEqual((held, d['memory']['holdMb']), ({'ci-slice': (4096, 5000)}, 0))
        self.runners(2000, 'max')                   # no limit: the hold's budgetMb (6144)
        self.assertEqual(self.held()[2], {'ci-slice': (6144, 2000)})
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice', 'budgetMb': 3000, 'inUseFromMb': 0})
        self.assertEqual(self.held()[2], {'ci-slice': (3000, 2000)})

    def test_its_own_users_the_missing_cgroup_and_unreadable_files(self):
        me = hf.user_name()
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice', 'users': ['nobody-*', me[:1] + '*']})
        self.runners(2000)
        self.assertEqual(self.held()[2], {}, 'a hold is never held back from its own users')
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        shutil.rmtree(self.m.root + self.CG)
        code, d, held = self.held()
        self.assertEqual((code, d['level'], held), (75, 'elevated', {}), 'other settings: the cached level is not '
                         'carried over, so it starts "elevated" (#945), never ok straight away')
        os.unlink(self.m.cache)                     # no cache at all: a first call
        code, d, held = self.held()
        self.assertEqual((code, held), (0, {}), 'no cgroup: nothing runs in it (also a machine without cgroup v2)')
        self.runners(2000, limit=None)              # memory.max cannot be read: the hold's budgetMb
        code, d, held = self.held()
        self.assertEqual(held, {'ci-slice': (6144, 2000)})
        self.assertTrue(any(n.startswith('hold ci-slice: its memory.max') for n in d['notes']), d['notes'])
        self.m.write(self.CG + '/memory.stat', 'anon 1')   # cut short: no shmem line, so its use is not known
        code, d, held = self.held()
        self.assertEqual(held, {'ci-slice': (6144, 0)}, 'use not known: the whole budget is held')
        self.assertTrue(any('memory.stat cannot be read' in n for n in d['notes']), d['notes'])

    def test_a_cgroup_without_a_memory_controller_holds_its_whole_budget(self):
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice', 'budgetMb': 5000})
        os.makedirs(self.m.root + self.CG)          # there, but no memory.stat: the controller is off for it
        code, d, held = self.held()
        self.assertEqual((code, held), (0, {'ci-slice': (5000, 0)}), 'it fails closed, not open')
        self.assertEqual(d['memory']['holdMb'], 5000)
        self.assertTrue(any(n.startswith('hold ci-slice: no memory controller') for n in d['notes']), d['notes'])

    def test_a_cgroup_that_ends_between_the_two_looks_holds_nothing(self):
        self.runners(2000)
        hold = dict(label='ci-slice', cgroup='ci.slice/ci-runners.slice', inUseFromMb=1024, budgetMb=6144, users=[])

        def ended(host, version, scope):
            shutil.rmtree(self.m.root + self.CG)
            return {'gone': True}
        with mock.patch.object(hf, 'ROOT', self.m.root), mock.patch.object(hf.rd, 'read_piece_demand', ended):
            self.assertEqual(hf.held_cgroups(hf.rd.RealHost(self.m.root), [hold], 'someone'), ([], []))

    def test_its_swap_is_not_counted_as_ram_it_uses(self):
        # memory.max limits RAM only: with max 4096, anon 3000 and 1500 swapped out it can still take 1096 MB of RAM
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.runners(3000)
        self.m.write(self.CG + '/memory.swap.current', f'{1500 * MB}\n')
        code, d, held = self.held()
        self.assertEqual((held, d['memory']['holdMb']), ({'ci-slice': (4096, 3000)}, 1096))
        self.runners(500)                           # mostly swapped out: in use (1500 > 1024), RAM use 500
        self.m.write(self.CG + '/memory.swap.current', f'{1000 * MB}\n')
        self.assertEqual(self.held()[2], {'ci-slice': (4096, 500)})

    def test_memory_max_of_max_is_no_limit_not_an_unreadable_file(self):
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.runners(2000, 'max')
        code, d, held = self.held()
        self.assertEqual(held, {'ci-slice': (6144, 2000)})
        self.assertFalse([n for n in d['notes'] if 'memory.max' in n], d['notes'])

    def test_at_most_8_holds(self):
        many = [{'label': f'r{i}', 'cgroup': f'r{i}.slice'} for i in range(9)]   # 9 distinct labels and cgroups
        self.holds(*many[:8])
        self.assertEqual(self.facts('--fresh')[0], 0)
        self.holds(*many)
        self.assertEqual(self.facts('--fresh'), (1, None), 'a 9th hold is refused')

    def test_nested_holds_are_refused(self):
        a = {'label': 'ci', 'cgroup': 'ci.slice'}
        b = {'label': 'ci-runners', 'cgroup': 'ci.slice/ci-runners.slice'}
        for holds in ((a, b), (b, a), (b, dict(a, cgroup=b['cgroup']))):
            self.holds(*holds)
            self.assertEqual(self.facts('--fresh'), (1, None), holds)
        self.holds({'label': 'x', 'cgroup': 'ci.slice/a'}, {'label': 'y', 'cgroup': 'ci.slice/ab'})
        self.assertEqual(self.facts('--fresh')[0], 0, 'siblings sharing first letters are not nested')

    def test_a_reservation_file_with_a_holds_name_gets_a_note(self):
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.runners(2000)
        with open(os.path.join(self.m.reserve, 'ci-slice.conf'), 'w') as f:
            f.write('1000 /x/app\n1000 /x/web\n')
        code, d, _ = self.held()
        self.assertEqual(sorted((r['name'], r['mb']) for r in d['reservations']),
                         [('ci-slice', 1000), ('ci-slice', 1000), ('ci-slice', 4096)], 'all of them still hold')
        self.assertEqual([n for n in d['notes'] if 'name of a machine-file hold' in n],
                         ['reservation ci-slice.conf has the name of a machine-file hold: both hold; rename the file'])

    def test_the_machine_file_missing_empty_or_broken(self):
        self.runners(2000)
        for data in (None, {'version': 1}, {'version': 1, 'heavy': {}}, {'version': 1, 'heavy': {'queue': {}}},
                     {'version': 1, 'heavy': {'capacity': {}}}):
            if data is not None:
                self.machine_file(data)
            code, d, held = self.held()
            self.assertEqual((code, held), (0, {}), data)
        hold = {'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'}
        for data, mode in (('', 0o644), ('{"version": 1, "heavy": {"capac', 0o644),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [hold]}}}, 0o666),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [hold, hold]}}}, 0o644),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [dict(hold, cgroup='user.slice/x')]}}},
                            0o644),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [dict(hold, cgroup='a/../b')]}}}, 0o644),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [dict(hold, budget=1)]}}}, 0o644),
                           ({'version': 1, 'heavy': {'capacity': {'holds': [{'label': 'x'}]}}}, 0o644),
                           ({'version': 1, 'heavy': {'capacity': {'slots': 2}}}, 0o644)):
            self.machine_file(data, mode)
            self.assertEqual(self.facts('--fresh'), (1, None), data)    # never free on a broken machine file
        code, out, err = self.cli([T3CAP, '64'])
        self.assertEqual(code, 1)
        self.assertRegex(out.splitlines()[-1], r'^NOT OK: heavy facts: .*heavy\.capacity')

    def test_a_changed_machine_file_is_read_again_within_5_s(self):
        self.runners(2000)
        self.assertEqual(self.facts('--mb', '64')[1]['reservations'], [])
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.assertEqual([r['name'] for r in self.facts('--mb', '64')[1]['reservations']], ['ci-slice'])

    def test_t3_capacity_shows_the_hold(self):
        self.holds({'label': 'ci-slice', 'cgroup': 'ci.slice/ci-runners.slice'})
        self.runners(2000)
        code, out, _ = self.cli([T3CAP, '64'])
        self.assertEqual(code, 0, out)
        self.assertIn('available=10192MB', out.splitlines()[0])
        self.assertEqual(out.splitlines()[1], 'reservations: ci-slice:4096MB(using 2000,holding 2096)')

    def test_the_example_machine_file_is_accepted(self):
        with open(os.path.join(os.path.dirname(KIT), 'common', 'machine.example.json')) as f:
            self.machine_file(f.read())
        self.assertEqual(self.facts('--fresh')[0], 0)
        self.assertEqual([h['label'] for h in hf.capacity_settings(
            hq.machine_file, hq.SECTION, self.m.root + '/etc/fork-kit/machine.json', os.getuid())['holds']],
            ['ci-slice'])

    def test_the_sweep_and_the_queue_accept_the_capacity_part(self):
        sweep = load_module('heavy_sweep', os.path.join(KIT, 'lib', 'heavy_sweep.py'))
        self.assertIn('capacity', sweep.SECTION_SPEC)
        self.assertIn('capacity', hq.SECTION)


class State(Case):
    def test_a_second_call_within_5_s_uses_the_cache_and_later_reads_again(self):
        first = self.facts('--mb', '64')[1]
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 6000))
        self.assertEqual(self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=4.9))[1]['memory'],
                         first['memory'])
        later = self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=5))[1]
        self.assertEqual(later['memory']['availableMb'], 6000)
        self.assertEqual(os.stat(self.m.cache).st_mode & 0o777, 0o600)

    def test_a_future_broken_empty_or_foreign_cache_is_read_again(self):
        self.facts()
        with open(self.m.cache) as f:
            good = f.read()
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 6000))
        for text, mode in ((good, 0o644), ('', 0o600), ('{"v": 1', 0o600), ('[1]', 0o600),
                           (json.dumps(dict(json.loads(good), v=hf.CACHE_VERSION + 1)), 0o600),
                           (json.dumps(dict({k: v for k, v in json.loads(good).items() if k != 'key'}, v=1)), 0o600),
                           (json.dumps(dict(json.loads(good), key='other.slice\n/q\n/r')), 0o600),
                           (json.dumps(dict(json.loads(good), extra=1)), 0o600), ('{"v": NaN}', 0o600)):
            with self.subTest(text=text[:20], mode=oct(mode)):
                with open(self.m.cache, 'w') as f:
                    f.write(text)
                os.chmod(self.m.cache, mode)
                self.assertEqual(self.facts()[1]['memory']['availableMb'], 6000)
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 7000))
        self.assertEqual(self.facts(now=NOW - datetime.timedelta(seconds=1))[1]['memory']['availableMb'], 7000,
                         'a cache from the future is not used')
        os.unlink(self.m.cache)
        os.symlink(os.path.join(self.tmp, 'elsewhere'), self.m.cache)
        self.assertEqual(self.facts()[1]['memory']['availableMb'], 7000)

    def test_a_writer_stopped_half_way_leaves_no_trace_that_matters(self):
        stale = self.m.cache + '.123.abcd.tmp'
        fresh = self.m.cache + '.124.abcd.tmp'
        for p in (stale, fresh):
            with open(p, 'w') as f:
                f.write('{"v": 1, "half')
        os.utime(stale, (time.time() - 120, time.time() - 120))
        code, d = self.facts()
        self.assertEqual(code, 0)
        self.assertEqual(sorted(os.listdir(os.path.dirname(self.m.cache))),
                         sorted([os.path.basename(self.m.cache), os.path.basename(fresh)]))
        os.chmod(os.path.dirname(self.m.cache), 0o500)             # cannot write: still answers, says so
        self.addCleanup(os.chmod, os.path.dirname(self.m.cache), 0o700)
        code, d = self.facts('--fresh')
        self.assertEqual(code, 0)
        self.assertTrue(any(n.startswith('cache not written') for n in d['notes']))

    def test_pressure_carries_from_the_last_reading(self):
        self.m.write('/proc/pressure/memory', psi(50))
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'elevated')
        self.m.write('/proc/pressure/memory', CALM)
        wait = RULE['pressure']['elevatedClearSeconds']
        t = NOW + datetime.timedelta(seconds=6)
        self.assertEqual(self.facts('--mb', '64', now=t)[1]['level'], 'elevated', 'calm starts, the wait begins')
        t2 = t + datetime.timedelta(seconds=wait)
        hops = [self.facts('--mb', '64', now=t + datetime.timedelta(seconds=s))[1]['level']
                for s in range(6, wait, 6)]
        self.assertEqual(set(hops), {'elevated'})
        self.assertEqual(self.facts('--mb', '64', now=t2)[1]['level'], 'ok')
        self.m.write('/proc/pressure/memory', psi(50))
        self.facts('--fresh', now=t2 + datetime.timedelta(seconds=6))
        self.m.write('/proc/pressure/memory', CALM)
        later = t2 + datetime.timedelta(seconds=6 + hf.CARRY_SECONDS + 1)
        self.assertEqual(self.facts('--mb', '64', now=later)[1]['level'], 'ok', 'a reading older than 5 min is not carried')

    def test_the_last_swap_reading_carries_to_the_next_call(self):
        # #839: swap that fills between two `heavy facts` calls is seen only through the swap free carried in the
        # cache (the trend); without it each call would see no trend and stay "ok"
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 12288, swap_free=600))
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'ok', 'no trend yet')
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 12288, swap_free=500))
        t = NOW + datetime.timedelta(seconds=6)
        fill = RULE['pressure']['swapFillingMb']
        self.assertLess(fill, 100)
        self.assertEqual(self.facts('--mb', '64', now=t)[1]['level'], 'elevated',
                         f'it fell 100 MB (more than {fill}) since the last call, under 15% free: filling')
        os.unlink(self.m.cache)                     # the same reading without the carried one: no trend
        self.assertEqual(self.facts('--mb', '64', now=t)[1]['level'], 'ok')
        with open(self.m.cache) as f:
            self.assertEqual(json.load(f)['pressure']['job']['swapFreeMb'], 500, 'kept for the next call')

    def test_cost_file_missing_learned_broken(self):
        build = 'job-build'
        if build not in RULE['kinds']:
            self.skipTest('no job-build kind in the rule')
        self.m.job('build', BUILD_MB, demand_mb=0)
        start = self.facts('--fresh')[1]['memory']['owedMb']
        self.assertEqual(start, self.cost(build))
        state = os.path.join(self.m.home, '.local', 'state', 'heavy')
        os.makedirs(state)
        with open(os.path.join(state, 'peaks.json'), 'w') as f:
            json.dump({'v': 1, 'kinds': {'build': [self.cost(build) * 2]}}, f)
        learned = self.facts('--fresh')[1]['memory']['owedMb']
        self.assertEqual(learned, hq.reservation('build', self.cost(build), state, out=lambda m: None)[0])
        self.assertGreater(learned, start)
        for text in ('{broken', '', '[]'):
            with open(os.path.join(state, 'peaks.json'), 'w') as f:
                f.write(text)
            self.assertEqual(self.facts('--fresh')[1]['memory']['owedMb'], start, text)

    def test_two_callers_at_once(self):
        self.m.agent('a', 100)
        procs = [subprocess.Popen([HEAVY, 'facts', '--mb', '64'], stdout=subprocess.PIPE, cwd=self.work,
                                  env=self.m.env(PWD=self.work)) for _ in range(6)]
        outs = [(p.communicate(timeout=60)[0], p.returncode) for p in procs]
        for out, code in outs:
            self.assertEqual(code, 0, out)
            self.assertEqual(json.loads(out)['queue']['agents'], 1)
        self.assertEqual(os.listdir(os.path.dirname(self.m.cache)), [os.path.basename(self.m.cache)])
        with open(self.m.cache) as f:
            data = json.load(f)
        folder = hf.given_folder(os.path.dirname(self.m.cache))[0]
        self.addCleanup(folder.close)
        name = os.path.basename(self.m.cache)
        self.assertEqual(hf.read_cache(folder, name, data['atMs'], data['key'])[0]['queue']['agents'], 1)
        self.assertEqual(hf.read_cache(folder, name, data['atMs'], 'another key'), (None, hf.no_carry()))

    def test_without_a_cache_folder_the_level_never_drops_straight_to_ok(self):
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(os.path.join(tmp_root, f'heavy-facts-{UID}'), mode=0o700)
        os.chmod(os.path.join(tmp_root, f'heavy-facts-{UID}'), 0o777)    # planted first, open to others
        home = self.loose_home()                                            # and no home that can hold one (#951)
        env = {'HEAVY_FACTS_CACHE': '', 'HOME': home}
        with self.no_run_user(tmp_root, self.passwd_home(home)):
            for i in range(3):                      # a calm machine, called again and again: every call starts over
                code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(minutes=i * 10), env=env)
                self.assertEqual((code, d['level']), (75, 'elevated'), i)
            code, d = self.facts('--kind', WRITER, env=env)
            self.assertEqual((code, d['level']), (0, 'elevated'), 'the floor still admits')
            self.assertTrue(any(n.startswith('no cache: ') for n in d['notes']), d['notes'])
        self.assertFalse(os.path.exists(self.m.cache))
        self.assertEqual(os.listdir(os.path.join(tmp_root, f'heavy-facts-{UID}')), [])

    def test_a_cache_of_another_version_never_skips_the_hold_down(self):
        # #945: a cache file that is there but cannot be used (once per user after a version change, or while an
        # old heavy writes it too) carried nothing, so the level went straight to the raw one, past the hold-down.
        # Now it starts "elevated", as with no cache folder; a missing or old (> 5 min) one carries nothing, as before
        self.facts('--mb', '64')
        with open(self.m.cache) as f:
            good = json.loads(f.read())
        for name, text in (('old version', json.dumps(dict(good, v=hf.CACHE_VERSION - 1))),
                           ('other settings', json.dumps(dict(good, key='other'))), ('broken', '{"v": 2')):
            with self.subTest(cache=name):
                with open(self.m.cache, 'w') as f:
                    f.write(text)
                code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=10))
                self.assertEqual((code, d['level']), (75, 'elevated'))
        later = NOW + datetime.timedelta(seconds=10 + RULE['pressure']['elevatedClearSeconds'])
        self.assertEqual(self.facts('--mb', '64', now=later)[0], 0, 'calm long enough: ok again')
        os.unlink(self.m.cache)
        self.assertEqual(self.facts('--mb', '64', now=later)[1]['level'], 'ok', 'no cache file yet: as before')
        with open(self.m.cache) as f:
            good = json.loads(f.read())
        with open(self.m.cache, 'w') as f:
            f.write(json.dumps(dict(good, atMs=good['atMs'] + 60000)))
        self.assertEqual(self.facts('--mb', '64', now=later)[1]['level'], 'elevated', 'from the future')

    def test_without_a_temporary_folder_it_can_write_there_is_no_cache(self):
        # #945: tempfile.gettempdir() ends in the working folder when no temporary folder can be written, so a
        # read-only sandbox got a heavy-facts-<uid>/ folder in the job's tree. Now: $TMPDIR, else /tmp, else none
        if os.geteuid() == 0:
            self.skipTest('root can write every folder')
        closed = os.path.join(self.tmp, 'closed')
        os.makedirs(closed)
        os.chmod(closed, 0o555)
        self.addCleanup(os.chmod, closed, 0o755)
        home = self.loose_home()
        with mock.patch.object(hf, 'RUN_USER', os.path.join(self.tmp, 'no-run')), \
                mock.patch.object(hf, 'TMP_DIRS', (closed,), create=True), mock.patch.object(tempfile, 'tempdir', None), \
                mock.patch.object(tempfile, '_candidate_tempdir_list', lambda: [closed, os.getcwd()]):
            with self.passwd_home(home):
                code, d = self.facts('--mb', '64', env={'HEAVY_FACTS_CACHE': '', 'TMPDIR': closed, 'HOME': home})
            self.assertEqual(os.listdir(self.work), [], 'a cache folder was made in the working folder')
            self.assertEqual((code, d['level']), (75, 'elevated'))
            self.assertTrue(any(n.startswith('no cache: no temporary folder') for n in d['notes']), d['notes'])
            # #951: a home that can hold it: the cache goes there instead, with a note
            code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(hours=1),
                                 env={'HEAVY_FACTS_CACHE': '', 'TMPDIR': closed})
        self.assertEqual(os.listdir(self.work), [], 'a cache folder was made in the working folder')
        self.assertEqual((code, d['level']), (0, 'ok'), d['notes'])
        self.assertTrue(any(n.startswith('no temporary folder') and os.path.join(self.m.home, '.cache') in n
                            for n in d['notes']), d['notes'])

    def test_without_run_user_the_cache_is_a_private_temporary_folder(self):
        # #844: a user with no user manager has no /run/user/<uid>; without a cache every call would start
        # "elevated" and a --mb ask would never be OK. A folder only it can use in the temporary folder keeps it
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(tmp_root)
        folder = os.path.join(tmp_root, f'heavy-facts-{UID}')
        with mock.patch.object(hf, 'RUN_USER', os.path.join(self.tmp, 'no-run')), \
                mock.patch.object(hf, 'TMP_ROOT', tmp_root):
            code, d = self.facts('--mb', '64', env={'HEAVY_FACTS_CACHE': ''})
            self.assertEqual((code, d['level']), (0, 'ok'), 'as with /run/user: a calm machine is free')
            self.m.write('/proc/meminfo', self.m.meminfo(16384, 6000))
            again = self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=4), env={'HEAVY_FACTS_CACHE': ''})
            self.assertEqual(again[1]['memory'], d['memory'], 'a second call within 5 s is answered from the cache')
            self.assertEqual(os.lstat(folder).st_mode & 0o777, 0o700)
            self.assertEqual(len(os.listdir(folder)), 1)
            self.assertRegex(os.listdir(folder)[0], r'^heavy-facts-v%d-[0-9a-f]{16}\.json$' % hf.CACHE_VERSION)
            for planted in ('link', 'file', 'open'):     # a folder that is not one only this user can use: no cache
                with self.subTest(planted=planted):
                    shutil.rmtree(folder)
                    if planted == 'link':
                        os.makedirs(os.path.join(self.tmp, 'elsewhere'), mode=0o700, exist_ok=True)
                        os.symlink(os.path.join(self.tmp, 'elsewhere'), folder)
                    elif planted == 'file':
                        open(folder, 'w').close()
                    else:
                        os.mkdir(folder)
                        os.chmod(folder, 0o755)
                    home = self.loose_home()        # and no home that can hold one (#951: else it goes there)
                    with self.passwd_home(home):
                        code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=30),
                                             env={'HEAVY_FACTS_CACHE': '', 'HOME': home})
                    self.assertEqual((code, d['level']), (75, 'elevated'))
                    self.assertTrue(any(n.startswith('no cache: ') for n in d['notes']), d['notes'])
                    self.assertEqual(os.listdir(os.path.join(self.tmp, 'elsewhere')) if planted == 'link' else [], [])
                    if planted == 'open':
                        os.rmdir(folder)
                    else:
                        os.unlink(folder)
                    os.mkdir(folder, 0o700)

    def no_run_user(self, tmp_root, *more):
        """No /run/user/<uid>, and <tmp_root> as the temporary folder (with more patches)."""
        stack = contextlib.ExitStack()
        for p in (mock.patch.object(hf, 'RUN_USER', os.path.join(self.tmp, 'no-run')),
                  mock.patch.object(hf, 'TMP_ROOT', tmp_root)) + more:
            stack.enter_context(p)
        return stack

    def loose_home(self):
        """A home in a folder others can write that is not sticky: no cache may go there (#951). Its path."""
        loose = os.path.join(self.tmp, 'loose')
        home = os.path.join(loose, 'home')
        os.makedirs(home, 0o700, exist_ok=True)
        os.chmod(loose, 0o777)
        return home

    def passwd_home(self, home):
        """pwd.getpwuid with this user's home as `home` (other users' as they are)."""
        import pwd
        real = pwd.getpwuid

        def entry(uid):
            e = real(uid)
            return types.SimpleNamespace(pw_name=e.pw_name, pw_dir=home, pw_uid=e.pw_uid) if uid == UID else e
        return mock.patch.object(pwd, 'getpwuid', entry)

    def test_a_taken_temporary_cache_name_falls_back_to_a_private_folder_in_the_home(self):
        # #951 point 2: another user can make <tmp>/heavy-facts-<uid> (or a file of that name) before this user
        # does; heavy refused it and ran without a cache, so every call started "elevated" and a --mb ask was never
        # OK. Now the cache goes in a folder only this user can use in its home (~/.cache) instead
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(tmp_root)
        taken = os.path.join(tmp_root, f'heavy-facts-{UID}')
        elsewhere = os.path.join(self.tmp, 'elsewhere')
        os.makedirs(elsewhere, 0o700)
        cache_root = os.path.join(self.m.home, '.cache')
        env = {'HEAVY_FACTS_CACHE': ''}

        def fallback():
            names = os.listdir(cache_root)
            self.assertEqual(len(names), 1, names)
            self.assertTrue(names[0].startswith('heavy-facts'), names)
            return os.path.join(cache_root, names[0])
        with self.no_run_user(tmp_root):
            for i, planted in enumerate(('file', 'open', 'link')):
                with self.subTest(planted=planted):
                    if planted == 'file':
                        open(taken, 'w').close()
                    elif planted == 'open':
                        os.unlink(taken)
                        os.mkdir(taken)
                        os.chmod(taken, 0o777)
                    else:
                        os.rmdir(taken)
                        os.symlink(elsewhere, taken)
                    at = NOW + datetime.timedelta(minutes=10 * i)      # past the carry window of the call before
                    self.m.write('/proc/meminfo', self.m.meminfo(16384, 12000))
                    code, d = self.facts('--mb', '64', now=at, env=env)
                    self.assertEqual((code, d['level']), (0, 'ok'), d['notes'])
                    self.assertTrue(any(taken in n for n in d['notes']), d['notes'])
                    self.m.write('/proc/meminfo', self.m.meminfo(16384, 6000))
                    again = self.facts('--mb', '64', now=at + datetime.timedelta(seconds=4), env=env)
                    self.assertEqual(again[1]['memory'], d['memory'], 'runs again: answered from the cache')
                    folder = fallback()
                    self.assertEqual(os.lstat(folder).st_mode & 0o777, 0o700)
                    self.assertEqual(len(os.listdir(folder)), 1, os.listdir(folder))
                    self.assertRegex(os.listdir(folder)[0], r'^heavy-facts-v%d-[0-9a-f]{16}\.json$' % hf.CACHE_VERSION)
                    self.assertEqual(os.listdir(elsewhere), [], 'nothing was written through the planted link')
            # stopped half-way: the fallback folder gone (or never made past ~/.cache): made again
            shutil.rmtree(fallback())
            self.assertEqual(self.facts('--mb', '64', now=NOW + datetime.timedelta(hours=1), env=env)[0], 0)
            fallback()
            # the hold of a NOT OK is kept there too (the same root as facts(): its machine id names the folder)
            with mock.patch.dict(os.environ, dict(env, HOME=self.m.home)), mock.patch.object(hf, 'ROOT', self.m.root):
                self.assertEqual(hf.hold('k:1', 'busy', 1_000_000)[:2], ('busy', None))
                self.assertEqual(hf.hold('k:1', 'free', 1_000_001)[0], 'busy', 'held in the fallback folder')
            self.assertIn(hf.HOLD_FILE, os.listdir(fallback()))
            # a ~/.cache that is a link or open to others, or a home in a folder others can write: no cache, as before
            shutil.rmtree(cache_root)
            for bad in ('link', 'open', 'home'):
                with self.subTest(bad=bad):
                    home = self.m.home
                    if bad == 'link':
                        os.symlink(elsewhere, cache_root)
                    elif bad == 'open':
                        os.mkdir(cache_root)
                        os.chmod(cache_root, 0o777)
                    else:
                        loose = os.path.join(self.tmp, 'loose')
                        os.makedirs(os.path.join(loose, 'home'), 0o700)
                        os.chmod(loose, 0o777)          # not sticky: another user could swap home for theirs
                        home = os.path.join(loose, 'home')
                    with self.passwd_home(home):
                        code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(hours=2),
                                             env=dict(env, HOME=home))
                    self.assertEqual((code, d['level']), (75, 'elevated'), d['notes'])
                    self.assertTrue(any(n.startswith('no cache: ') for n in d['notes']), d['notes'])
                    self.assertEqual(os.listdir(elsewhere), [])
                    if bad == 'home':
                        self.assertEqual(os.listdir(home), [], 'nothing made in a home others can swap')
                    elif bad == 'link':
                        os.unlink(cache_root)
                    else:
                        os.rmdir(cache_root)

    def test_two_machines_with_one_home_and_one_host_name_never_share_the_home_cache(self):
        # #996 item 3: the home cache folder was named by the host name only, so two machines that share a home and
        # a host name (clones, cloud images) read each other's 5 s readings. Now the machine id is in the name too
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(tmp_root)
        open(os.path.join(tmp_root, f'heavy-facts-{UID}'), 'w').close()     # the temporary name taken: the home
        cache_root = os.path.join(self.m.home, '.cache')
        env = {'HEAVY_FACTS_CACHE': ''}
        one, two = '0123456789abcdef' * 2, 'fedcba9876543210' * 2

        def on(machine, avail, at):
            self.m.write('/etc/machine-id', machine + '\n')
            os.chmod(self.m.root + '/etc', 0o755)           # (the machine file's folder must be closed to others)
            self.m.write('/proc/meminfo', self.m.meminfo(16384, avail))
            return self.facts('--mb', '64', now=at, env=env)[1]['memory']['availableMb']
        with self.no_run_user(tmp_root):
            self.assertEqual(on(one, 12000, NOW), 12000)
            self.assertEqual(on(one, 6000, NOW + datetime.timedelta(seconds=1)), 12000, 'runs again: the cache')
            self.assertEqual(on(two, 6000, NOW + datetime.timedelta(seconds=2)), 6000, 'never the other one\'s')
            folders = sorted(os.listdir(cache_root))
            self.assertEqual(len(folders), 2, folders)
            self.assertTrue(all(f.startswith(f'heavy-facts-{hf.host_name()}-') for f in folders), folders)
            # the previous kit's folder (host name only) is never read and never removed: another machine of this
            # home may still run that kit
            old = os.path.join(cache_root, f'heavy-facts-{hf.host_name()}')
            os.rename(os.path.join(cache_root, folders[0]), old)
            before = {n: os.stat(os.path.join(old, n)).st_mtime_ns for n in os.listdir(old)}
            self.assertEqual(on(one, 3000, NOW + datetime.timedelta(seconds=3)), 3000, 'the old cache was read')
            self.assertEqual({n: os.stat(os.path.join(old, n)).st_mtime_ns for n in os.listdir(old)}, before)
        # no machine id (or an unset one, "uninitialized" on a first boot): the boot id; neither: the host name
        with mock.patch.object(hf, 'ROOT', self.m.root):
            self.assertEqual(hf.machine_tag(), f'{hf.host_name()}-{one[:16]}')
            os.unlink(self.m.root + '/etc/machine-id')
            self.m.write('/proc/sys/kernel/random/boot_id', '01234567-89ab-cdef-0123-456789abcdef\n')
            # #998 item 3: a boot id is marked "boot" (its stale folders are removed: their names are this kit's)
            self.assertEqual(hf.machine_tag(), f'{hf.host_name()}-boot-0123456789abcdef')
            self.m.write('/etc/machine-id', 'uninitialized\n')
            self.assertEqual(hf.machine_tag(), f'{hf.host_name()}-boot-0123456789abcdef')
            self.m.write('/var/lib/dbus/machine-id', two + '\n')     # D-Bus's own machine id: steady, before the boot id
            self.assertEqual(hf.machine_tag(), f'{hf.host_name()}-{two[:16]}')
            os.unlink(self.m.root + '/var/lib/dbus/machine-id')
            os.unlink(self.m.root + '/proc/sys/kernel/random/boot_id')
            self.assertEqual(hf.machine_tag(), hf.host_name())

    def test_stale_boot_id_cache_folders_of_this_kit_are_removed(self):
        # #998 item 3: without a machine id the home cache folder is named by the boot id, so each boot made a new one
        # and nothing removed the old ones. A "boot" folder of this host name (not this boot's) whose files were not
        # written for STALE_BOOT_SECONDS is removed; the old kit's host-only folder, machine-id folders, other hosts'
        # and anything not a plain folder of plain files are never touched
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(tmp_root)
        open(os.path.join(tmp_root, f'heavy-facts-{UID}'), 'w').close()     # the temporary name taken: the home
        cache_root = os.path.join(self.m.home, '.cache')
        host = hf.host_name()
        one, two = '0123456789abcdef', 'fedcba9876543210'

        def boot(ident):
            return f'heavy-facts-{host}-boot-{ident}'

        def on(ident, at=NOW):
            self.m.write('/proc/sys/kernel/random/boot_id', ident * 2 + '\n')
            code, d = self.facts('--mb', '64', now=at, env={'HEAVY_FACTS_CACHE': ''})
            self.assertEqual(code, 0, d)
            return sorted(os.listdir(cache_root))

        def age(path):
            old = time.time() - 2 * 86400
            for name in os.listdir(path) if os.path.isdir(path) else ():
                os.utime(os.path.join(path, name), (old, old), follow_symlinks=False)
            os.utime(path, (old, old), follow_symlinks=False)

        def made(name, files=('heavy-facts-v4-0123456789abcdef.json',), folder=True):
            path = os.path.join(cache_root, name)
            if folder:
                os.makedirs(path, 0o700)
                for f in files:                     # a/: a folder; @a: a link; else a plain file
                    if f.endswith('/'):
                        os.makedirs(os.path.join(path, f))
                    elif f.startswith('@'):
                        os.symlink(os.path.join(self.tmp, 'elsewhere'), os.path.join(path, f))
                    else:
                        open(os.path.join(path, f), 'w').close()
            else:
                open(path, 'w').close()
            age(path)
            return name
        with self.no_run_user(tmp_root):
            self.assertEqual(on(one), [boot(one)])
            self.assertEqual(on(two, NOW + datetime.timedelta(seconds=1)), [boot(one), boot(two)],
                             'a boot folder written just now is kept (a clone of this host name may use it)')
            age(os.path.join(cache_root, boot(one)))
            kept = [made(f'heavy-facts-{host}'),                    # the old kit's host-only folder
                    made(f'heavy-facts-{host}-{one}'),              # a machine-id folder (another machine's)
                    made(f'heavy-facts-other-host-boot-{one}'),     # another host name's
                    made(boot('0' * 16), folder=False),             # not a folder
                    made(boot('1' * 16), files=('sub/',)),          # holds a folder
                    made(boot('6' * 16), files=('a.json', '@link')),  # holds a link
                    made(boot('2' * 15))]                           # not a name this kit makes
            for i in range(2):                                      # runs again: the same answer
                self.assertEqual(on(two, NOW + datetime.timedelta(seconds=2 + i)), sorted(kept + [boot(two)]))
            # stopped half-way: a stale folder that lost some of its files (its own time is new) and an empty one
            half = made(boot('3' * 16), files=('a.json', 'b.lock'))
            os.unlink(os.path.join(cache_root, half, 'a.json'))
            empty = made(boot('4' * 16), files=())
            self.assertEqual(on(two, NOW + datetime.timedelta(seconds=5)), sorted(kept + [boot(two)]))
            self.assertFalse(os.path.exists(os.path.join(cache_root, empty)))
            if os.geteuid() != 0:                                   # root reads every folder
                shut = made(boot('5' * 16))
                os.chmod(os.path.join(cache_root, shut), 0)         # cannot be read: kept, the answer unchanged
                self.addCleanup(os.chmod, os.path.join(cache_root, shut), 0o700)
                self.assertEqual(on(two, NOW + datetime.timedelta(seconds=6)), sorted(kept + [boot(two), shut]))

    def test_the_cache_folder_swapped_after_its_check_is_not_followed(self):
        # #951 point 3: the private temporary folder was checked, then used by its path, so a swap between the two
        # (a $TMPDIR that others can write and that is not sticky) pointed the cache and the hold at another folder.
        # Now it is opened once (O_DIRECTORY | O_NOFOLLOW) and every later step goes through that handle
        tmp_root = os.path.join(self.tmp, 'tmp')
        os.makedirs(tmp_root)
        folder = os.path.join(tmp_root, f'heavy-facts-{UID}')
        elsewhere = os.path.join(self.tmp, 'elsewhere')
        os.makedirs(elsewhere, 0o700)

        def swap():
            if not os.path.islink(folder):
                os.rename(folder, folder + '.moved')
                os.symlink(elsewhere, folder)

        def after_swap(real):
            def call(*a, **k):
                swap()
                return real(*a, **k)
            return call
        with self.no_run_user(tmp_root, mock.patch.object(hf, 'read_machine', after_swap(hf.read_machine))):
            code, d = self.facts('--mb', '64', env={'HEAVY_FACTS_CACHE': ''})
        self.assertEqual(code, 0, d)
        self.assertEqual(os.listdir(elsewhere), [], 'the cache was written in the swapped-in folder')
        self.assertEqual(len(os.listdir(folder + '.moved')), 1, 'written in the folder that was checked')
        for f in (folder, folder + '.moved'):           # again for the hold, stopped by nothing: a fresh folder
            os.unlink(f) if os.path.islink(f) else shutil.rmtree(f)
        with self.no_run_user(tmp_root, mock.patch.object(hf, '_hold_lock', after_swap(hf._hold_lock))), \
                mock.patch.dict(os.environ, {'HEAVY_FACTS_CACHE': ''}):
            self.assertEqual(hf.hold('k:1', 'busy', 1_000_000)[:2], ('busy', None))
        self.assertEqual(os.listdir(elsewhere), [], 'the hold was kept in the swapped-in folder')
        self.assertEqual(sorted(os.listdir(folder + '.moved')), sorted([hf.HOLD_FILE, hf.HOLD_LOCK]))

    def test_callers_with_other_settings_taking_turns_each_reach_ok(self):
        # #945: two callers of one user with other settings (here another reservations folder) shared one cache file
        # and each read the other's as unusable, so every call started "elevated": NOT OK on a calm machine for as
        # long as they took turns. Now each has its own file: the first call beside another caller's recent file
        # starts "elevated" (never straight to ok), then each one's own hold-down steps down
        run = os.path.join(self.tmp, 'run')
        os.makedirs(os.path.join(run, str(UID)), mode=0o700)
        other = os.path.join(self.tmp, 'reserve-b.d')
        os.makedirs(other)
        callers = ({'HEAVY_FACTS_CACHE': ''}, {'HEAVY_FACTS_CACHE': '', 'T3_CAPACITY_RESERVE_DIR': other})
        seen = []
        with mock.patch.object(hf, 'RUN_USER', run):
            for i in range(12):                     # A and B take turns every 20 s on a calm machine
                code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(seconds=20 * i), env=callers[i % 2])
                seen.append((code, d['level']))
        self.assertEqual(seen[:2], [(0, 'ok'), (75, 'elevated')], 'B starts "elevated" beside A\'s fresh file')
        self.assertEqual(seen[-6:], [(0, 'ok')] * 6, seen)
        files = sorted(os.listdir(os.path.join(run, str(UID))))
        self.assertEqual(len(files), 2, files)
        # a cache file of other settings past the carry window carries nothing and is removed on the next write
        stale = os.path.join(run, str(UID), 'heavy-facts.json')     # an older kit's
        open(stale, 'w').close()
        os.utime(stale, (time.time() - hf.CARRY_SECONDS - 60,) * 2)
        with mock.patch.object(hf, 'RUN_USER', run):
            self.facts('--mb', '64', '--fresh', now=NOW + datetime.timedelta(seconds=240), env=callers[0])
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(sorted(os.listdir(os.path.join(run, str(UID)))), files)


def fake_heavy(tmp, body):
    path = os.path.join(tmp, 'fake-heavy')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
    return path


class T3Capacity(Case):
    FIRST = r'^available=\d+MB swapfree=\d+MB load=[0-9.]+/\d+cores agents=\d+ jobs=\d+ waiting=\d+ ' \
            r'reserved=\d+MB need=\d+MB free=-?\d+MB level=\w+ tier=\w+ disk=\d+GB(<\d+GB)?$'

    def test_the_mb_argument_first_line_and_exit_codes(self):
        code, out, err = self.cli([T3CAP, '1536'])
        self.assertEqual(code, 0, out + err)
        lines = out.splitlines()
        self.assertRegex(lines[0], self.FIRST)
        self.assertIn('need=1536MB', lines[0])
        self.assertEqual(lines[-1], 'OK')
        self.assertTrue(any(line.startswith('fits: ') for line in lines))
        code, out, err = self.cli([T3CAP, '99999'])
        self.assertEqual(code, 1)
        self.assertTrue(out.splitlines()[-1].startswith('NOT OK: 99999 MB'))
        self.assertIn('never pipe it', err)
        out_file = os.path.join(self.tmp, 'cap.txt')
        with open(out_file, 'w') as f:   # #931: output to a file keeps the status; the line says so, not "a pipe hides"
            p = subprocess.run([T3CAP, '99999'], stdout=f, stderr=subprocess.PIPE, text=True, timeout=30,
                               cwd=self.work, env=self.m.env(PWD=self.work))
        self.assertEqual(p.returncode, 1)
        self.assertNotIn('pipe', p.stderr)
        self.assertIn('exit status is kept', p.stderr)
        with open(out_file) as f:
            self.assertTrue(f.read().splitlines()[-1].startswith('NOT OK'))
        with open(os.devnull, 'w') as f:   # the usual gate, `t3-capacity N >/dev/null && cmd`: no "file" there
            p = subprocess.run([T3CAP, '99999'], stdout=f, stderr=subprocess.PIPE, text=True, timeout=30,
                               cwd=self.work, env=self.m.env(PWD=self.work))
        self.assertEqual(p.returncode, 1)
        self.assertNotIn('pipe', p.stderr)
        self.assertNotIn('to a file', p.stderr)
        self.assertIn('/dev/null', p.stderr)
        self.assertIn('exit status is kept', p.stderr)
        self.assertIn('need=3072MB', self.cli([T3CAP])[1].splitlines()[0])
        code, out, _ = self.cli([T3CAP, '--kind', WRITER])
        self.assertEqual((code, out.splitlines()[-1]), (0, 'OK'))
        for bad in (['x'], ['1', '2'], ['--kind']):
            self.assertEqual(self.cli([T3CAP] + bad)[0], 2, bad)

    def test_free_disk_under_the_floor_is_not_ok_with_the_reason_in_the_first_line(self):
        # #982: a disk at 97% failed a CI job with "disk full" while t3-capacity said OK. The floor is 25 GB unless
        # the machine file's heavy.disk.floorGb says otherwise; free disk is read on the home folder's filesystem
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB=str(26 * 1024))
        self.assertEqual((code, out.splitlines()[-1]), (0, 'OK'), out)
        self.assertTrue(out.splitlines()[0].endswith(' disk=26GB'), out)
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB=str(24 * 1024))
        self.assertEqual(code, 1, out)
        first, last = out.splitlines()[0], out.splitlines()[-1]
        self.assertTrue(first.endswith(' disk=24GB<25GB'), first)
        self.assertEqual(last, f'NOT OK: disk: 24 GB free on {self.m.home}, under the floor of 25 GB '
                               '(heavy.disk.floorGb)')
        # the same with the machine file's floor and folder; a floor of 0 is off
        self.machine_file({'version': 1, 'heavy': {'disk': {'floorGb': 40, 'path': self.work}}})
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB=str(30 * 1024))
        self.assertEqual(code, 1, out)
        self.assertTrue(out.splitlines()[0].endswith(' disk=30GB<40GB'), out)
        self.assertIn(f'30 GB free on {self.work}, under the floor of 40 GB', out.splitlines()[-1])
        self.machine_file({'version': 1, 'heavy': {'disk': {'floorGb': 0}}})
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB='1')
        self.assertEqual((code, out.splitlines()[-1]), (0, 'OK'), out)

    def test_a_disk_it_cannot_read_or_a_broken_disk_setting_is_never_ok(self):
        self.machine_file({'version': 1, 'heavy': {'disk': {'path': os.path.join(self.tmp, 'not-there')}}})
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB='')
        self.assertEqual(code, 1, out)
        self.assertTrue(out.splitlines()[0].endswith(' disk=?'), out)
        self.assertRegex(out.splitlines()[-1], r'^NOT OK: disk: cannot read the free space of .*not-there')
        for bad in ({'floorGb': -1}, {'floorGb': '25'}, {'path': 'relative/folder'}, {'floor': 25}):
            with self.subTest(bad=bad):
                self.machine_file({'version': 1, 'heavy': {'disk': bad}})
                code, out, _ = self.cli([T3CAP, '64'])
                self.assertEqual(code, 1, out)
                self.assertIn('heavy.disk', out.splitlines()[-1])

    def test_a_floor_of_0_is_off_and_reads_no_disk(self):
        # floorGb 0 turns the floor off: the disk is not read, so a path it cannot read is no NOT OK either
        missing = os.path.join(self.tmp, 'not-there')
        self.machine_file({'version': 1, 'heavy': {'disk': {'floorGb': 0, 'path': missing}}})
        code, out, _ = self.cli([T3CAP, '64'], HEAVY_FACTS_DISK_FREE_MB='')
        self.assertEqual((code, out.splitlines()[-1]), (0, 'OK'), out)
        self.assertTrue(out.splitlines()[0].endswith(' disk=off'), out)

    def test_a_disk_setting_is_no_unknown_key_for_the_rest_of_the_kit(self):
        self.machine_file({'version': 1, 'heavy': {'disk': {'floorGb': 25}}})
        self.assertEqual(self.facts('--fresh')[0], 0, 'heavy facts accepts the section with a disk key')
        self.assertIsNone(hf.ready(hq), 'heavy\'s own check before a job too')

    def test_a_not_ok_holds_20_s_for_the_same_question(self):
        # #861: the check flipped OK / NOT OK within seconds, so "check, then start" could start work just before it
        # turned NOT OK; the orchestrator checked twice, 20 s apart. Now a NOT OK holds that long by itself.
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 2048))      # busy for 1536 MB
        code, out, _ = self.cli([T3CAP, '1536'])
        self.assertEqual(code, 1)
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 12288))     # free again a second later
        os.unlink(self.m.cache)
        code, out, _ = self.cli([T3CAP, '1536'])
        self.assertEqual(code, 1, 'held')
        self.assertRegex(out.splitlines()[0], self.FIRST, 'the first line callers parse is unchanged')
        self.assertTrue(out.splitlines()[-1].startswith('NOT OK: held: NOT OK '), out)
        self.assertEqual(self.cli([T3CAP, '1024'])[0], 0, 'another question is not held')
        hold_file = os.path.join(os.path.dirname(self.m.cache), hf.HOLD_FILE)
        with open(hold_file) as f:
            state = json.load(f)
        state['busy'] = {k: v - 21_000 for k, v in state['busy'].items()}     # 21 s later
        with open(hold_file, 'w') as f:
            json.dump(state, f)
        self.assertEqual(self.cli([T3CAP, '1536'])[0], 0, 'the hold has ended')

    def test_heavy_s_own_checks_share_t3_capacity_s_hold(self):
        """#866 with #861: heavy's capacity check (a queued job's and a plain job's) is hf.checked too, so a NOT OK
        that t3-capacity gave holds 20 s for heavy's same question, and heavy's own NOT OK holds its next ask."""
        ask = ('import importlib.machinery, importlib.util, sys; l = importlib.machinery.SourceFileLoader("h", '
               'sys.argv[1]); m = importlib.util.module_from_spec(importlib.util.spec_from_loader("h", l)); '
               'l.exec_module(m); c = m.capacity_check(); a = sys.argv[3] or None; '
               'print(repr(c(a, int(sys.argv[2]) if sys.argv[2] else None)))')

        def heavy_check(kind=None, mb=None):
            code, out, err = self.cli([sys.executable, '-I', '-B', '-c', ask, HEAVY, '' if mb is None else str(mb),
                                       kind or ''])
            self.assertEqual(code, 0, err)
            return eval(out)            # (free?, why) from our own print
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 2048))      # busy for a build of 2048 MB and a job
        self.assertEqual(self.cli([T3CAP, '--kind', 'job-build', '2048'])[0], 1)
        self.assertFalse(heavy_check()[0])                               # heavy's plain ask: busy, held from now
        self.m.write('/proc/meminfo', self.m.meminfo(16384, 12288))     # free again a second later
        os.unlink(self.m.cache)
        free, why = heavy_check('job-build', 2048)
        self.assertEqual((free, why.startswith('held: NOT OK ')), (False, True), f't3-capacity\'s NOT OK: {why}')
        free, why = heavy_check()
        self.assertEqual((free, why.startswith('held: NOT OK ')), (False, True), f'heavy\'s own NOT OK: {why}')
        self.assertEqual(heavy_check('job-test', 512), (True, ''), 'another question is not held')

    def test_the_hold_runs_again_and_meets_old_broken_or_foreign_state(self):
        folder = os.path.join(self.tmp, 'hold')
        os.makedirs(folder, mode=0o700)
        path = os.path.join(folder, hf.HOLD_FILE)
        t = 1_000_000_000_000
        self.assertEqual(hf.hold('k:1', 'free', t, folder)[:2], ('free', None))
        self.assertFalse(os.path.exists(path), 'nothing to keep: nothing written')
        self.assertEqual(hf.hold('k:1', 'busy', t, folder)[:2], ('busy', None))
        self.assertEqual(hf.hold('k:1', 'free', t + 19_999, folder)[0], 'busy')
        self.assertEqual(hf.hold('k:1', 'free', t + 19_999, folder)[0], 'busy', 'runs again: still held')
        self.assertEqual(hf.hold('k:1', 'free', t + 20_000, folder)[0], 'free', 'a held answer did not extend it')
        self.assertEqual(json.load(open(path))['busy'], {}, 'an ended hold is dropped')
        self.assertEqual(hf.hold('k:1', 'unavailable', t, folder)[0], 'unavailable')
        self.assertEqual(hf.hold('k:1', 'free', t - 5_000, folder)[0], 'busy', 'a clock that went back: held')
        self.assertEqual(hf.hold('k:1', 'free', t + 14_999, folder)[0], 'busy', 'and started again then')
        self.assertEqual(hf.hold('k:1', 'free', t + 15_000, folder)[0], 'free', '20 s from then, not from t (#981)')
        for bad in ('{broken', '', '{"v": 2, "busy": {}}', '{"v": 1, "busy": {"k:1": "x"}}', '[]'):
            with self.subTest(bad=bad):
                with open(path, 'w') as f:
                    f.write(bad)
                verdict, why, _ = hf.hold('k:1', 'free', t, folder)
                self.assertEqual(verdict, 'busy', 'a hold file that cannot be read holds (fails closed)')
                self.assertIn('cannot be read', why)
                self.assertEqual(hf.hold('k:1', 'free', t + 20_000, folder)[0], 'free', 'written again: it heals')
        os.unlink(path)
        os.symlink(os.path.join(self.tmp, 'elsewhere'), path)          # a link planted there
        self.assertEqual(hf.hold('k:1', 'free', t, folder)[0], 'busy')
        self.assertFalse(os.path.islink(path), 'replaced by a file of its own')
        os.chmod(path, 0o644)                                           # open to others
        self.assertEqual(hf.hold('k:1', 'free', t + 40_000, folder)[0], 'busy')
        for name in os.listdir(folder):                                 # a writer stopped half-way leaves a .tmp
            self.assertFalse(name.endswith('.tmp'), name)
        with mock.patch.object(hf, 'state_folder', lambda: (None, 'no temporary folder')):
            self.assertEqual(hf.hold('k:1', 'free', t)[0::2], ('free', 'no hold of a NOT OK (no temporary folder)'))

    def test_a_broken_hold_file_in_a_folder_that_cannot_be_written_holds_only_its_20_s(self):
        # a hold file that cannot be read fails closed, but when it cannot be written again either it must not hold
        # every later OK for good: it holds HOLD_SECONDS from when it last changed (its ctime), then it is ignored
        folder = os.path.join(self.tmp, 'hold')
        os.makedirs(folder, mode=0o700)
        path = os.path.join(folder, hf.HOLD_FILE)
        for bad in ('{broken', '{"busy": {"k:1": 5}}', '{"v": 0, "busy": []}'):    # broken, older formats
            with self.subTest(bad=bad):
                with open(path, 'w') as f:
                    f.write(bad)
                changed = int(os.lstat(path).st_ctime * 1000)
                with mock.patch.object(hf, 'write_cache', side_effect=PermissionError(13, 'Permission denied')):
                    verdict, why, note = hf.hold('k:1', 'free', changed + 5_000, folder)
                    self.assertEqual(verdict, 'busy', 'within its 20 s: held (fails closed)')
                    self.assertIn('cannot be read', why)
                    self.assertIn('was not written', note)
                    self.assertEqual(hf.hold('k:1', 'free', changed + hf.HOLD_SECONDS * 1000, folder)[0], 'free')
                    self.assertEqual(hf.hold('k:1', 'free', changed + 3_600_000, folder)[0], 'free', 'an hour on')
                    self.assertEqual(hf.hold('k:1', 'busy', changed + 3_600_000, folder)[0], 'busy')
        lstat = os.lstat

        real_open = os.open

        def blind(p, *a, **k):                      # it cannot even be looked at: ignored, with a note
            if p in (path, hf.HOLD_FILE):           # by its path, or by its name in the open folder (#951)
                raise PermissionError(13, 'Permission denied', p)
            return lstat(p, *a, **k)

        def closed(p, *a, **k):
            if p in (path, hf.HOLD_FILE):
                raise PermissionError(13, 'denied')
            return real_open(p, *a, **k)
        with mock.patch('os.lstat', blind), mock.patch('os.open', closed), \
                mock.patch.object(hf, '_hold_lock', return_value=(None, None)):
            verdict, why, note = hf.hold('k:1', 'free', int(time.time() * 1000), folder)
        self.assertEqual((verdict, why), ('free', None))
        self.assertIn('ignored', note)

    def test_a_future_hold_time_that_cannot_be_written_again_does_not_hold(self):
        # #981 leftover 4: a time in the future (the clock went back) holds and starts again from now; when the folder
        # cannot be written it never starts again, so it held every OK until the clock came back: an hour back, an
        # hour held. Without a write it cannot be bounded to 20 s, so it does not hold (with a note).
        folder = os.path.join(self.tmp, 'hold')
        os.makedirs(folder, mode=0o700)
        path = os.path.join(folder, hf.HOLD_FILE)
        t = 1_000_000_000_000
        for content, before in (('{broken', None),                                      # a broken file's ctime
                                (json.dumps({'v': 1, 'busy': {'k:1': t + 3_600_000}}), t)):  # a kept time
            with self.subTest(content=content):
                with open(path, 'w') as f:
                    f.write(content)
                if content == '{broken':
                    before = int(os.lstat(path).st_ctime * 1000) - 3_600_000
                with mock.patch.object(hf, 'write_cache', side_effect=PermissionError(13, 'Permission denied')):
                    for _ in range(2):                                              # runs again: the same
                        verdict, why, note = hf.hold('k:1', 'free', before, folder)
                        self.assertEqual((verdict, why), ('free', None), note)
                        self.assertIn('in the future', note)
                    self.assertEqual(hf.hold('k:1', 'busy', before, folder)[0], 'busy', 'a NOT OK is still one')
                self.assertEqual(hf.hold('k:1', 'free', before, folder)[0], 'busy', 'written: held, started again')
                with open(path) as f:
                    self.assertEqual(json.load(f)['busy'], {'k:1': before}, 'started again on disk too')
                self.assertEqual(hf.hold('k:1', 'free', before + hf.HOLD_SECONDS * 1000, folder)[0], 'free')

    def test_two_not_oks_at_once_are_both_kept(self):
        # the hold is a read, then a write: without a lock the second caller's write would drop the first's NOT OK
        folder = os.path.join(self.tmp, 'hold')
        os.makedirs(folder, mode=0o700)
        read = os.path.join(self.tmp, 'read')
        slow = f"""
import importlib.machinery, importlib.util, os, sys, time
loader = importlib.machinery.SourceFileLoader('hf', {FACTS!r})
hf = importlib.util.module_from_spec(importlib.util.spec_from_loader('hf', loader)); loader.exec_module(hf)
real = hf.write_cache
def slow_write(*a):
    open({read!r}, 'w').close()             # it has read the file and decided: the other caller comes now
    time.sleep(1.5)
    real(*a)
hf.write_cache = slow_write
print(hf.hold('a:1', 'busy', None, {folder!r}))
"""
        p = subprocess.Popen([sys.executable, '-I', '-B', '-c', slow], stdout=subprocess.PIPE, text=True)
        self.addCleanup(p.kill)
        deadline = time.monotonic() + 20
        while not os.path.exists(read):
            self.assertLess(time.monotonic(), deadline, 'the slow caller never got to its write')
            time.sleep(0.01)
        self.assertEqual(hf.hold('b:1', 'busy', None, folder)[0], 'busy')
        self.assertEqual(p.wait(timeout=20), 0)
        with open(os.path.join(folder, hf.HOLD_FILE)) as f:
            self.assertEqual(set(json.load(f)['busy']), {'a:1', 'b:1'}, 'both NOT OKs are kept')

    def test_a_hold_lock_that_stays_taken_never_stops_the_answer(self):
        folder = os.path.join(self.tmp, 'hold')
        os.makedirs(folder, mode=0o700)
        fd = os.open(os.path.join(folder, hf.HOLD_LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)              # another caller stopped while it held it (a live one, here)
        t = time.monotonic()
        verdict, why, note = hf.hold('k:1', 'busy', None, folder)
        self.assertLess(time.monotonic() - t, hf.HOLD_LOCK_SECONDS + 2)
        self.assertEqual(verdict, 'busy')
        self.assertIn('held without it', note)

    def test_reservations_line_and_available_after_them(self):
        other = os.path.join(self.tmp, 'other')
        with open(os.path.join(self.m.reserve, 'game.conf'), 'w') as f:
            f.write(f'2000 {other}/\n')
        code, out, _ = self.cli([T3CAP, '64'])
        self.assertIn('available=10288MB', out.splitlines()[0])
        self.assertEqual(out.splitlines()[1], 'reservations: game:2000MB(using 0,holding 2000)')
        os.makedirs(other)
        code, out, _ = self.cli([T3CAP, '64'], cwd=other)
        self.assertEqual(out.splitlines()[1], 'reservations: game:own')
        self.assertIn('available=12288MB', out.splitlines()[0])

    def test_reserved_and_available_count_each_once(self):
        # #923: available= already has the reservations' hold taken off; reserved= is only what agents and jobs may
        # still grow into, so available - reserved counts each once. The agent working in the reserved folder owes
        # its debt there, so the reservation holds the rest
        other = os.path.join(self.tmp, 'other')
        with open(os.path.join(self.m.reserve, 'game.conf'), 'w') as f:
            f.write(f'1536 {other}/\n')
        self.m.process(4001, other, 300)
        self.m.agent('a', 300, pids=(4001,))
        owed = self.cost(WRITER) - 300
        code, out, _ = self.cli([T3CAP, '64'])
        first, second = out.splitlines()[:2]
        self.assertRegex(first, self.FIRST)
        self.assertIn(f' reserved={owed}MB ', first)
        self.assertIn(f'available={12288 - (1536 - 300 - owed)}MB ', first)
        self.assertEqual(second, f'reservations: game:1536MB(using 300,holding {1536 - 300 - owed},owed {owed})')

    def test_available_counts_every_reservation_even_those_trimmed_from_the_answer(self):
        with open(os.path.join(self.m.reserve, 'many.conf'), 'w') as f:
            for i in range(80):
                f.write(f'10 {self.tmp}/{"long-folder-name-" * 4}{i}\n')
        code, out, _ = self.cli([T3CAP, '64'])
        self.assertIn('available=11488MB', out.splitlines()[0])

    def test_slow_failing_or_garbled_facts_are_never_ok(self):
        for body in ('exec sleep 60', 'exit 1', 'echo "not json"; exit 0', 'echo \'{"v":1,"free":true}\'; exit 0',
                     'exit 75'):
            with self.subTest(body=body):
                t = time.monotonic()
                code, out, err = self.cli([T3CAP, '64'], T3_CAPACITY_HEAVY=fake_heavy(self.tmp, body))
                self.assertLess(time.monotonic() - t, hf.ANSWER_SECONDS + 3)
                self.assertEqual(code, 1, out)
                self.assertTrue(out.splitlines()[0].startswith('available=0MB swapfree=0MB'), out)
                self.assertTrue(out.splitlines()[-1].startswith('NOT OK: heavy facts'), out)
                if 'sleep' in body:
                    self.assertIn(f'within {hf.ANSWER_SECONDS} s', out)

    def test_the_slow_facts_are_ended(self):
        mark = os.path.join(self.tmp, 'still-running')
        body = f'(sleep 8; touch {mark}) & exec sleep 60'
        self.assertEqual(self.cli([T3CAP, '64'], T3_CAPACITY_HEAVY=fake_heavy(self.tmp, body))[0], 1)
        time.sleep(4)
        self.assertFalse(os.path.exists(mark), 'a child of the slow heavy facts kept running')



class CpuBrake(Case):
    """#1047: memory alone said OK for a build while containers outside heavy kept every core busy. The rule's `cpu`
    brakes one more job (and MB alone, which asks for a job) on CPU pressure or load per core; agents are not braked,
    and a machine with no CPU PSI or load reading is braked by nothing more than before."""
    CPU = RULE['cpu']

    def cpu(self, some):
        self.m.write('/proc/pressure/cpu', f'some avg10={some:.2f} avg60=0.00 avg300=0.00 total=0\n')

    def test_calm_or_no_reading_brakes_nothing(self):
        for setup in (lambda: None, lambda: self.cpu(self.CPU['pressureSomeAvg10Pct'] - 1),
                      lambda: self.m.write('/proc/pressure/cpu', 'broken\n')):
            with self.subTest(setup=setup):
                setup()
                code, d = self.facts('--kind', 'job-build', '--fresh')
                self.assertEqual(code, 0, d)
                self.assertGreater(d['fits']['job-build'], 0)

    def test_pressure_brakes_jobs_not_agents(self):
        self.cpu(self.CPU['pressureSomeAvg10Pct'])
        code, d = self.facts('--kind', 'job-build')
        self.assertEqual(code, hf.EXIT_BUSY, d)
        self.assertIn('job-build: cpu pressure', d['reason'])
        self.assertEqual(d['cpu']['pressureSome10'], self.CPU['pressureSomeAvg10Pct'])
        self.assertEqual({k: n for k, n in d['fits'].items() if RULE['kinds'][k]['group'] == 'job'},
                         {k: 0 for k, v in RULE['kinds'].items() if v['group'] == 'job'})
        self.assertEqual(self.facts('--kind', WRITER, '--fresh')[0], 0)
        self.assertGreater(self.facts('--kind', WRITER)[1]['fits'][WRITER], 0)

    def test_load_per_core_brakes(self):
        cores = 8
        brake = cores * self.CPU['loadPerCorePercent'] / 100
        self.m.write('/proc/loadavg', f'{brake - 0.5:.2f} 1.00 0.50 2/300 4000\n')
        self.assertEqual(self.facts('--kind', 'job-build')[0], 0)
        self.m.write('/proc/loadavg', f'{brake:.2f} 1.00 0.50 2/300 4000\n')
        code, d = self.facts('--kind', 'job-build', '--fresh')
        self.assertEqual(code, hf.EXIT_BUSY, d)
        self.assertIn(f'cpu load {brake:g} on 8 cores', d['reason'])

    def test_t3_capacity_says_not_ok(self):
        self.cpu(100)
        code, out, _ = self.cli([T3CAP, '64'])
        self.assertEqual(code, 1, out)
        self.assertRegex(out.splitlines()[-1], r'^NOT OK: cpu pressure 100% ')
        self.assertEqual(self.cli([T3CAP, '--kind', WRITER])[0], 0, 'an agent is not braked')

    def test_a_rule_without_cpu_brakes_nothing(self):
        rule = {k: v for k, v in RULE.items() if k != 'cpu'}
        self.assertIsNone(hf.cpu_busy(rule, {'pressureSome10': 100.0, 'hostCores': 8}, 99.0))
        self.assertIsNotNone(hf.cpu_busy(RULE, {'pressureSome10': None, 'hostCores': 8}, 99.0))
        self.assertIsNone(hf.cpu_busy(RULE, {'pressureSome10': None, 'hostCores': None}, 99.0))   # nothing to compare
        self.assertIsNone(hf.cpu_busy(RULE, None, 99.0))

    def test_pressure_decides_over_load(self):
        """Load counts tasks waiting for I/O (a slow disk, a hung network mount): where CPU pressure is read, a calm
        one is not braked by a high load."""
        self.cpu(1)
        self.m.write('/proc/loadavg', '64.00 1.00 0.50 2/300 4000\n')
        code, d = self.facts('--kind', 'job-build')
        self.assertEqual(code, 0, d)

    def test_a_container_reads_its_own_cpu(self):
        """In a container with a CPU limit, its own cgroup's pressure decides, not the host's; without one, the
        host's load is compared with the host's CPUs, not the container's share of them."""
        self.m.write('/sys/fs/cgroup/cpu.max', '200000 100000\n')   # 2 of the host's 8 CPUs
        self.cpu(100)                                                 # the host is busy
        self.m.write('/sys/fs/cgroup/cpu.pressure', 'some avg10=3.00 avg60=0.00 avg300=0.00 total=0\n')
        code, d = self.facts('--kind', 'job-build')
        self.assertEqual(code, 0, d)
        self.assertEqual(d['cpu']['pressureSome10'], 3.0)
        self.m.write('/sys/fs/cgroup/cpu.pressure', 'some avg10=95.00 avg60=0.00 avg300=0.00 total=0\n')
        code, d = self.facts('--kind', 'job-build', '--fresh')
        self.assertEqual(code, hf.EXIT_BUSY, d)
        os.unlink(self.m.root + '/sys/fs/cgroup/cpu.pressure')        # no PSI of its own: the host's load
        self.m.write('/proc/loadavg', '6.00 1.00 0.50 2/300 4000\n')  # 6 on 8 host CPUs: under 150%
        self.assertEqual(self.facts('--kind', 'job-build', '--fresh')[0], 0)


class AgentSlots(Case):
    def hook(self, event, alive, heavy=None, active=False, max_alive=10):
        q, s, conf = (os.path.join(self.tmp, n) for n in ('q.md', 's.txt', 'c.json'))
        with open(q, 'w') as f:
            f.write('# Q\n## Ready\n- #1 a\n- #2 b\n## Waiting\n- #3 c\n')
        with open(s, 'w') as f:
            f.write('# orchestrators\nORCH\n')
        with open(conf, 'w') as f:
            json.dump({'sessions_file': s, 'queue': q, 'max_alive': max_alive}, f)
        env = self.m.env(AGENT_SLOTS_CONFIG=conf, AGENT_SLOTS_ALIVE=str(alive), PWD=self.work)
        if heavy:
            env['AGENT_SLOTS_HEAVY'] = heavy
        p = subprocess.run([SLOTS], input=json.dumps({'hook_event_name': event, 'session_id': 'ORCH',
                                                      'stop_hook_active': active}),
                           capture_output=True, text=True, timeout=30, env=env, cwd=self.work)
        self.assertEqual(p.returncode, 0)
        return json.loads(p.stdout) if p.stdout.strip() else None

    def test_free_is_what_both_the_process_limit_and_the_machine_allow(self):
        fits = self.facts('--kind', WRITER)[1]['fits'][WRITER]
        msg = self.hook('UserPromptSubmit', 2)['hookSpecificOutput']['additionalContext']
        self.assertIn(f'2/10 claude processes alive, {min(8, fits)} free (machine fits {fits} more {WRITER})', msg)
        self.assertEqual(self.hook('Stop', 2)['decision'], 'block')
        self.assertIsNone(self.hook('Stop', 2, active=True))
        self.assertIsNone(self.hook('Stop', 10))
        for i in range(40):                         # the machine is full of writers: no slot, even with 8 processes
            self.m.agent(f'w{i}', 0)
        os.unlink(self.m.cache)
        msg = self.hook('UserPromptSubmit', 2)['hookSpecificOutput']['additionalContext']
        self.assertIn('0 free (machine fits 0 more', msg)
        self.assertIsNone(self.hook('Stop', 2))

    def test_facts_that_do_not_answer_give_no_slot(self):
        for body in ('exit 1', 'exec sleep 60'):
            with self.subTest(body=body):
                heavy = fake_heavy(self.tmp, body)
                msg = self.hook('UserPromptSubmit', 2, heavy)['hookSpecificOutput']['additionalContext']
                self.assertIn('0 free (machine: no answer', msg)
                self.assertIsNone(self.hook('Stop', 2, heavy))

    def test_other_sessions_and_a_broken_config_are_left_alone(self):
        p = subprocess.run([SLOTS], input='not json', capture_output=True, text=True, timeout=30,
                           env=self.m.env(AGENT_SLOTS_CONFIG=os.path.join(self.tmp, 'none.json')))
        self.assertEqual((p.returncode, p.stdout), (0, ''))


class TestOnlySettings(Case):
    """#800: the test-only settings change nothing unless HEAVY_TESTING=1 is set too, so one that leaks into a real
    caller's environment never fakes its machine or its answer."""

    def test_the_fake_machine_needs_the_test_flag(self):
        env = dict(self.m.env())
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_module('heavy_facts_flag', FACTS).ROOT, self.m.root)
        del env['HEAVY_TESTING']
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(load_module('heavy_facts_noflag', FACTS).ROOT, '')
            self.assertIsNone(hf.test_env('HEAVY_FACTS_ROOT'))
        with mock.patch.dict(os.environ, dict(env, HEAVY_TESTING='yes'), clear=True):
            self.assertIsNone(hf.test_env('HEAVY_FACTS_ROOT'), 'only "1" turns it on')

    def test_heavy_s_own_start_asks_for_a_fresh_reading(self):
        # #866: heavy's start check must count a job admitted a second ago, so it never takes the 5 s cache
        marker = os.path.join(self.tmp, 'argv')
        fake = fake_heavy(self.tmp, f'echo "$@" > {marker}; exit 75')
        self.assertEqual(hf.ask(fake, 'build', 64, fresh=True)[0], 'unavailable')
        with open(marker) as f:
            self.assertEqual(f.read().split(), ['facts', '--kind', 'build', '--mb', '64', '--fresh'])
        hf.ask(fake)
        with open(marker) as f:
            self.assertEqual(f.read().split(), ['facts'])

    def test_the_callers_fake_heavy_and_process_count_need_the_test_flag(self):
        marker = os.path.join(self.tmp, 'fake-ran')
        fake = fake_heavy(self.tmp, f'touch {marker}; echo garbled; exit 0')
        env = {k: v for k, v in self.m.env(T3_CAPACITY_HEAVY=fake, AGENT_SLOTS_HEAVY=fake, AGENT_SLOTS_ALIVE='x',
                                           PWD=self.work).items() if k != 'HEAVY_TESTING'}
        conf = os.path.join(self.tmp, 'c.json')
        sessions = os.path.join(self.tmp, 's.txt')
        with open(sessions, 'w') as f:
            f.write('ORCH\n')
        with open(conf, 'w') as f:
            json.dump({'sessions_file': sessions, 'queue': os.path.join(self.tmp, 'none.md')}, f)
        p = subprocess.run([T3CAP, '64'], capture_output=True, text=True, timeout=30, env=env, cwd=self.work)
        self.assertNotIn('garbled', p.stdout + p.stderr)
        p = subprocess.run([SLOTS], input=json.dumps({'hook_event_name': 'UserPromptSubmit', 'session_id': 'ORCH'}),
                           capture_output=True, text=True, timeout=30, env=dict(env, AGENT_SLOTS_CONFIG=conf),
                           cwd=self.work)
        self.assertEqual(p.returncode, 0)
        self.assertIn('Agent slots:', p.stdout, 'AGENT_SLOTS_ALIVE=x is not read, so it cannot break the count')
        self.assertFalse(os.path.exists(marker), 'neither caller ran the fake heavy without HEAVY_TESTING=1')


class Kinds(Case):
    """Every heavy ticket is a job: a kind the rule does not know (tsc, gradle, a project's own) is the rule's
    unknownKinds.job, never an agent."""
    JOB = RULE['unknownKinds']['job']

    def job_fits(self, d):
        return {k: v for k, v in d['fits'].items() if RULE['kinds'][k]['group'] == 'job'}

    def test_unknown_kinds_at_the_job_ceiling_leave_no_job_free(self):
        ceiling = RULE['ceilings']['job']
        for _ in range(ceiling):
            self.m.job('tsc', 64, demand_mb=10)
        code, d = self.facts('--kind', 'build')
        self.assertEqual((code, d['free'], d['kind']), (75, False, 'job-build'))
        self.assertIn('ceiling', d['reason'])
        self.assertEqual(d['queue'], {'running': ceiling, 'waiting': 0, 'agents': 0})
        self.assertEqual(d['memory']['owedMb'], ceiling * (self.cost(self.JOB) - 10), 'the growth they still owe')
        self.assertEqual(set(self.job_fits(d).values()), {0})
        self.assertEqual(self.facts('--kind', 'gradle')[0], 75)

    def test_one_unknown_kind_owes_the_job_default_not_a_writer(self):
        self.assertNotEqual(self.cost(self.JOB), self.cost(WRITER))
        for kind in ('tsc', 'gradle', 'my-project.kind'):
            with self.subTest(kind=kind):
                for name in os.listdir(os.path.join(self.m.queue, 'heavy.slice')):
                    os.unlink(os.path.join(self.m.queue, 'heavy.slice', name))
                self.m.job(kind, 64, demand_mb=10)
                code, d = self.facts('--kind', kind, '--fresh')
                self.assertEqual((code, d['kind'], d['queue']['running']), (0, self.JOB, 1))
                self.assertEqual(d['memory']['owedMb'], self.cost(self.JOB) - 10)

    def test_plain_heavy_jobs_with_no_ticket_are_counted_from_the_slice(self):
        # a plain `heavy <cmd>` takes no ticket: its scope in this user's heavy slice is the only sign of it. Five of
        # them must not read as an idle machine; a scope a live ticket names is counted once, from its ticket
        box = MANAGER + '/heavy.slice'
        for i in range(5):
            self.m.write(f'{box}/heavy-plain{i}-0a1b2c3d.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        self.m.job('test', 64, demand_mb=10)                 # its scope run-j1.scope: named by its ticket
        os.makedirs(self.m.root + box + '/heavy-gone-0a1b2c3d.scope')      # ended: no memory.stat
        code, d = self.facts('--kind', 'build')
        self.assertEqual(code, 75, d)
        self.assertFalse(d['free'])
        self.assertIn('ceiling', d['reason'])
        self.assertEqual(d['queue'], {'running': 6, 'waiting': 0, 'agents': 0})
        self.assertEqual(d['memory']['owedMb'], 5 * (self.cost(self.JOB) - 10) + self.cost('job-test') - 10)

    def test_with_a_kind_the_rule_alone_decides_even_for_a_declared_size(self):
        # the queue asks `heavy facts --kind K --mb N`: free is exactly the rule's admit, with no stricter check of
        # its own. Free here is 12288 - 1639 = 10649; a build declaring 10700 does not fit it, but it is the first
        # job and leaves 12288 - 10700 = 1588 MB, above the severe line (50 % of the margin): the job floor admits it
        code, d = self.facts('--kind', 'build', '--mb', '10700')
        self.assertEqual((code, d['free'], d['memory']['freeMb'], d['memory']['costMb']), (0, True, 10649, 10700))
        self.m.job('test', 64, demand_mb=64)                # one job runs: no floor left, the budget refuses
        code, d = self.facts('--kind', 'build', '--mb', '10700', '--fresh')
        self.assertEqual((code, d['free']), (75, False))
        self.assertIn('budget', d['reason'])

    def test_a_known_job_kind_keeps_its_own_cost(self):
        self.m.job('test', 64, demand_mb=10)
        self.m.job('job-typecheck', self.cost('job-typecheck'), demand_mb=10)
        d = self.facts('--kind', 'build')[1]
        self.assertEqual(d['memory']['owedMb'], self.cost('job-test') + self.cost('job-typecheck') - 20)

    def test_a_job_owes_heavy_s_reservation_when_that_is_more_than_its_kind_s_cost(self):
        # #800: heavy reserved 5000 MB for this test job (its --mb, or the kind's learned peak): the job owes that,
        # not job-test's start cost; so does a job of a kind the rule does not know
        self.m.job('test', 5000, demand_mb=10)
        self.m.job('tsc', self.cost(self.JOB) + 1000, demand_mb=10)
        d = self.facts('--kind', 'build')[1]
        self.assertEqual(d['memory']['owedMb'], 5000 - 10 + self.cost(self.JOB) + 1000 - 10)
        code, d = self.facts('--kind', 'test', '--mb', '6000', '--fresh')
        self.assertEqual(d['memory']['costMb'], 6000, 'an ask that declares more costs that much')


class OtherUsers(Case):
    """A caller running as another user (a CI runner) sees this user's work, and the other way round."""

    def test_other_users_jobs_and_agents_are_counted(self):
        idle = self.facts('--kind', 'build')[1]
        self.m.write(OTHER + '/heavy.slice/run-x1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        self.m.write(OTHER + '/heavy.slice/memory.pressure', CALM)
        self.m.write(OTHER + '/app.slice/app-t3code.slice/t3code-z-1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        # #800: any scope right below an app-t3code slice is an agent, whatever its name
        self.m.write(OTHER + '/app.slice/app-t3code.slice/other.scope/memory.stat', 'anon 0\nshmem 0\n')
        os.makedirs(self.m.root + OTHER + '/heavy.slice/run-gone.scope')          # ended: no memory.stat
        code, d = self.facts('--kind', 'build', '--fresh')
        self.assertEqual(d['queue'], {'running': 1, 'waiting': 0, 'agents': 2})
        self.assertEqual(d['memory']['owedMb'],
                         self.cost(RULE['unknownKinds']['job']) - 10 + self.cost(WRITER) - 10 + self.cost(WRITER))
        self.assertTrue(any(n.startswith('other users: 1 heavy job(s), 2 agent(s)') for n in d['notes']))
        self.assertLess(d['fits']['job-build'], idle['fits']['job-build'])
        for i in range(2, RULE['ceilings']['job'] + 1):
            self.m.write(OTHER + f'/heavy.slice/run-x{i}.scope/memory.stat', 'anon 0\nshmem 0\n')
        code, d = self.facts('--kind', 'build', '--fresh')
        self.assertEqual((code, d['queue']['running']), (75, RULE['ceilings']['job']))
        self.assertIn('ceiling', d['reason'])

    def test_a_user_folder_that_cannot_be_listed_is_never_free(self):
        box = self.m.root + OTHER + '/heavy.slice'
        os.makedirs(box)
        listdir = os.listdir

        def refused(path='.'):                      # as a folder of mode 0 for a user who is not root
            if path == box:
                raise PermissionError(13, 'Permission denied', path)
            return listdir(path)
        with mock.patch('os.listdir', refused):
            code, d = self.facts('--kind', WRITER)
        self.assertEqual((code, d['tier'], set(d['fits'].values())), (75, 'basic', {0}))
        self.assertTrue(any('cannot be listed' in n for n in d['notes']))

    def test_strange_names_under_user_slice_are_skipped(self):
        for name in ('user-x.slice', 'user-1.slice.bak', 'session.slice'):
            self.m.write(f'/sys/fs/cgroup/user.slice/{name}/user@1.service/heavy.slice/run-a.scope/memory.stat',
                         'anon 0\nshmem 0\n')
        self.assertEqual(self.facts('--kind', 'build')[1]['queue']['running'], 0)


class Folders(Case):
    """A reservation's folder matches itself and what is below it, written with or without a trailing '/', never a
    sibling that shares its first letters; through a link it matches its real folder."""

    def setUp(self):
        super().setUp()
        self.base = os.path.join(self.tmp, 'app')
        self.sib = self.base + '-x'
        os.makedirs(self.base + '/sub')
        os.makedirs(self.sib)
        self.m.process(4001, self.sib, 300)               # a sibling's process uses nothing of app
        self.m.process(4002, self.base + '/sub', 100)

    def check(self, written):
        with open(os.path.join(self.m.reserve, 'k.conf'), 'w') as f:
            f.write(f'1000 {written}\n')
        d = self.facts('--mb', '64', '--fresh', cwd=self.sib)[1]
        self.assertEqual([(r['useMb'], r['own']) for r in d['reservations']], [(100, False)], written)
        self.assertEqual(d['memory']['holdMb'], 900, f'{written}: a sibling is held back')
        for cwd in (self.base, self.base + '/sub'):
            d = self.facts('--mb', '64', cwd=cwd)[1]
            self.assertEqual(([r['own'] for r in d['reservations']], d['memory']['holdMb']), ([True], 0),
                             f'{written} from {cwd}')

    def test_without_a_trailing_slash(self):
        self.check(self.base)

    def test_with_a_trailing_slash(self):
        self.check(self.base + '/')
        self.check(self.base + '//')

    def test_through_a_link(self):
        link = os.path.join(self.tmp, 'link')
        os.symlink(self.base, link)
        self.check(link)


class Container(Case):
    """Container limits (the cgroup root's memory.max and cpu.max) are read; a limit that cannot be read is busy."""

    def test_limits_are_read(self):
        self.m.write('/sys/fs/cgroup/memory.max', f'{8192 * MB}\n')
        self.m.write('/sys/fs/cgroup/memory.current', f'{3000 * MB}\n')
        self.m.write('/sys/fs/cgroup/memory.stat', f'anon 1\ninactive_file {1000 * MB}\n')
        self.m.write('/sys/fs/cgroup/cpu.max', '200000 100000\n')
        d = self.facts('--fresh')[1]
        self.assertEqual((d['memory']['availableMb'], d['cpu']['cores']), (8192 - 2000, 2))
        self.m.write('/sys/fs/cgroup/memory.max', 'max\n')
        self.m.write('/sys/fs/cgroup/cpu.max', 'max 100000\n')
        d = self.facts('--fresh')[1]
        self.assertEqual((d['memory']['availableMb'], d['cpu']['cores']), (12288, 8))

    def test_a_limit_that_cannot_be_read_is_busy(self):
        for path, text in (('/sys/fs/cgroup/memory.max', 'lots\n'), ('/sys/fs/cgroup/cpu.max', '0 0\n')):
            with self.subTest(path=path):
                self.m.write(path, text)
                code, d = self.facts('--kind', WRITER, '--fresh')
                self.assertEqual((code, d['free'], set(d['fits'].values())), (75, False, {0}))
                os.unlink(self.m.root + path)

    def test_a_cut_short_memory_stat_owes_in_full(self):
        self.m.job('build', BUILD_MB, demand_mb=1000)
        self.assertEqual(self.facts('--kind', 'build')[1]['memory']['owedMb'], self.cost('job-build') - 1000)
        self.m.write(f'{MANAGER}/heavy.slice/run-j1.scope/memory.stat', f'anon {1000 * MB}\nfi')
        self.assertEqual(self.facts('--kind', 'build', '--fresh')[1]['memory']['owedMb'], self.cost('job-build'))


class OneRule(unittest.TestCase):
    """#996 item 2: one rule, "someone else could swap a part of this path", in three copies: bin/heavy and
    lib/heavy_sweep.py others_can_swap (plain heavy loads no lib, the sweep runs alone) and lib/heavy_facts.py
    swappable. Each copy is asked about every owner and mode a folder above a home can have; all three agree."""

    def test_the_three_copies_of_the_swap_rule_agree(self):
        heavy = load_module('heavy_bin', HEAVY)
        sweep = load_module('heavy_sweep', os.path.join(KIT, 'lib', 'heavy_sweep.py'))
        me = os.geteuid()
        safe = os.stat_result((stat.S_IFDIR | 0o755, 2, 1, 1, 0, 0, 0, 0, 0, 0))     # / : root's, 755
        seen = set()
        for owner in (0, me, me + 1):
            for mode in (0o700, 0o750, 0o755, 0o775, 0o757, 0o777, 0o1777, 0o1775, 0o1757, 0o1700, 0o2775, 0o3777):
                st = os.stat_result((stat.S_IFDIR | mode, 3, 1, 1, owner, owner, 0, 0, 0, 0))
                with self.subTest(owner=owner, mode=oct(mode)), mock.patch.object(
                        os, 'lstat', lambda p, *a, **k: st if p == '/parent' else safe):
                    answers = (hf.swappable(st, me), heavy.others_can_swap('/parent/home'),
                               sweep.others_can_swap('/parent/home'))
                    self.assertEqual(len(set(answers)), 1, f'facts, heavy, sweep: {answers}')
                    seen.add((owner == me + 1, bool(mode & 0o022), bool(mode & 0o1000), answers[0]))
        # the rule itself, so the three cannot agree on a wrong one
        self.assertIn((True, False, False, True), seen, 'another user\'s folder')
        self.assertIn((False, True, False, True), seen, 'open to others, not sticky')
        self.assertIn((False, True, True, False), seen, 'open to others, sticky (/tmp)')
        self.assertIn((False, False, False, False), seen, 'root\'s or this user\'s, closed')
        # a folder on the way that cannot be looked at: both path copies say "can be swapped" (facts' walk opens
        # each part, so that one is refused there)
        with mock.patch.object(os, 'lstat', side_effect=PermissionError(13, 'denied')):
            self.assertEqual((heavy.others_can_swap('/parent/home'), sweep.others_can_swap('/parent/home')),
                             (True, True))


class RedCheck(Case):
    """Each fix line taken out of a copy of lib/heavy_facts.py makes its test fail."""

    def broken(self, line, instead):
        with open(FACTS) as f:
            src = f.read()
        self.assertEqual(src.count(line), 1, line)
        return load_module('heavy_facts_broken', FACTS, src.replace(line, instead))

    def failing(self, cls, name, mod):
        """How many of one test's checks (or subtests) fail with `heavy facts` from `mod`."""
        result = unittest.TestResult()
        with mock.patch.object(sys.modules[__name__], 'hf', mod):
            cls(name).run(result)
        return len(result.failures) + len(result.errors)

    def test_without_the_agent_scope_count_the_agent_test_fails(self):
        mod = self.broken("                pieces.append({'kind': rule['unknownKinds']['other'], 'demandMb': demand})"
                          "   # no kind label: a writer\n", '                pass\n')
        self.assertGreater(self.failing(Ledger, 'test_agent_scopes_count_as_writers', mod), 0)
        self.assertEqual(self.failing(Ledger, 'test_agent_scopes_count_as_writers', hf), 0)

    def test_without_unknown_kinds_as_jobs_the_kinds_tests_fail(self):
        mod = self.broken("    return rule['unknownKinds']['job']\n\n\ndef folder_prefixes",
                          "    return rule['unknownKinds']['other']\n\n\ndef folder_prefixes")
        for name in ('test_unknown_kinds_at_the_job_ceiling_leave_no_job_free',
                     'test_one_unknown_kind_owes_the_job_default_not_a_writer'):
            self.assertGreater(self.failing(Kinds, name, mod), 0, name)
            self.assertEqual(self.failing(Kinds, name, hf), 0, name)

    def test_without_the_trailing_slash_the_folder_tests_fail(self):
        for line, instead, name in (
                ("        p = f.rstrip('/') + '/'\n", "        p = f\n", 'test_without_a_trailing_slash'),
                ("    c = cwd.rstrip('/') + '/'\n", "    c = cwd\n", 'test_with_a_trailing_slash')):
            mod = self.broken(line, instead)
            self.assertGreater(self.failing(Folders, name, mod), 0, line)
            self.assertEqual(self.failing(Folders, name, hf), 0, name)


    def test_without_the_in_use_or_own_user_rule_the_hold_tests_fail(self):
        for line, name in (("            continue                                # not in use\n",
                             'test_a_cgroup_in_use_holds_its_budget_less_its_use'),
                           ("            continue                                # the caller's own budget is not "
                            "held back from it\n", 'test_its_own_users_the_missing_cgroup_and_unreadable_files')):
            mod = self.broken(line, '            pass\n')
            self.assertGreater(self.failing(Holds, name, mod), 0, line)
            self.assertEqual(self.failing(Holds, name, hf), 0, name)

    def test_without_the_923_and_844_fixes_their_tests_fail(self):
        for line, instead, cls, name in (
                ("                r['owedMb'] += debts[i] * (kb / total if total else 1 / len(pids))\n",
                 "                pass\n", Reservations, 'test_an_agent_or_job_working_in_the_folder_is_counted_once'),
                ("    res = [{'mb': r['mb'], 'useMb': r['useMb'] + r['owedMb']}",
                 "    res = [{'mb': r['mb'], 'useMb': r['useMb']}", Reservations,
                 'test_an_agent_or_job_working_in_the_folder_is_counted_once'),
                ("    return 'machine' if os.path.isdir(ROOT + USERS) else 'basic'\n",
                 "    return 'basic'\n", Ledger,
                 'test_no_user_manager_gets_the_machine_wide_answer'),
                ("    if tier == 'basic':\n        what =", "    if False:\n        what =", Ledger,
                 'test_ready_refuses_a_basic_tier_machine_at_once'),
                ("    basic = readings['tier'] not in ('full', 'machine')\n",
                 "    basic = readings['tier'] != 'full'\n", Ledger,
                 'test_no_user_manager_gets_the_machine_wide_answer'),
                ("    if private and (st.st_uid != os.getuid() or st.st_mode & 0o077):\n", "    if False:\n", State,
                 'test_without_run_user_the_cache_is_a_private_temporary_folder'),
                ("    else:\n        whys = []\n", "    else:\n        return None, 'no cache'\n", State,
                 'test_without_run_user_the_cache_is_a_private_temporary_folder')):
            with self.subTest(line=line[:60]):
                self.assertGreater(self.failing(cls, name, self.broken(line, instead)), 0, line)
                self.assertEqual(self.failing(cls, name, hf), 0, name)

    def test_without_each_945_fix_its_test_fails(self):
        for line, instead, cls, name in (
                # the whole debt to each reservation one of its processes works in (the PR's first version)
                ("                r['owedMb'] += debts[i] * (kb / total if total else 1 / len(pids))\n",
                 "                r['owedMb'] += debts[i] * len(pids) / len(pids)\n", Reservations,
                 'test_two_folders_nested_folders_and_work_across_them_count_each_debt_once'),
                # every reservation that holds a process's folder (main), not the deepest one
                ("        r = best(res, uid, cwd) if cwd else None\n        if r:\n            r['useMb'] += kb / 1024\n",
                 "        for r in [r for r in res if cwd and r['uid'] == uid and under(cwd, r['prefixes'])]:\n"
                 "            r['useMb'] += kb / 1024\n", Reservations,
                 'test_two_folders_nested_folders_and_work_across_them_count_each_debt_once'),
                ("        if d > depth:\n", "        if d >= 0 and found is None:\n", Reservations,
                 'test_two_folders_nested_folders_and_work_across_them_count_each_debt_once'),
                ("    if tier != 'basic':\n        # #945", "    if False:\n        # #945", Ledger,
                 'test_the_machine_and_full_tiers_see_every_person_s_reservations'),
                ("            blind += cannot\n", "            pass\n", Ledger,
                 'test_the_machine_and_full_tiers_see_every_person_s_reservations'),
                # #800: the full tier read no other person's reservations
                ("    if tier != 'basic':\n        # #945", "    if tier == 'machine':\n        # #945", Ledger,
                 'test_the_machine_and_full_tiers_see_every_person_s_reservations'),
                ("        return None, no_carry()\n    age = now_ms", "        return None, None\n    age = now_ms", State,
                 'test_a_cache_of_another_version_never_skips_the_hold_down'),
                # one cache file for every setting and version (the round-2 file): callers keep each other elevated
                ("    return f'heavy-facts-v{CACHE_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json'\n",
                 "    return 'heavy-facts.json'\n", State, 'test_callers_with_other_settings_taking_turns_each_reach_ok'),
                ("    for old in other_caches(folder, base, recent=False):", "    for old in []:", State,
                 'test_callers_with_other_settings_taking_turns_each_reach_ok'),
                ("        return None, (no_carry() if other_caches(folder, name, recent=True) else None)\n",
                 "        return None, None\n", State, 'test_callers_with_other_settings_taking_turns_each_reach_ok'),
                ("    root = TMP_ROOT or temp_root()\n", "    root = TMP_ROOT or __import__('tempfile').gettempdir()\n",
                 State, 'test_without_a_temporary_folder_it_can_write_there_is_no_cache')):
            with self.subTest(line=line[:60]):
                self.assertGreater(self.failing(cls, name, self.broken(line, instead)), 0, line)
                self.assertEqual(self.failing(cls, name, hf), 0, name)

    def test_without_each_998_fix_its_test_fails(self):
        for line, instead, cls, name in (
                ("    if outside:\n        return f'this cgroup", "    if False:\n        return f'this cgroup", Ledger,
                 'test_ready_refuses_a_cgroup_outside_the_namespace_at_once'),
                ("        for more_res, more, cannot in read_homes(dirs, now, size):\n",
                 "        for more_res, more, cannot in [reservation_files(f, now, (n, u), size) for n, u, f in dirs]:\n",
                 Ledger, 'test_a_hung_home_is_given_up_in_time_and_never_counts_as_free'),
                ("        drop_stale_boot_folders(cache, name)\n", "", State,
                 'test_stale_boot_id_cache_folders_of_this_kit_are_removed'),
                ("                continue                            # written lately: a machine with this host name "
                 "may use it\n", "                pass\n", State,
                 'test_stale_boot_id_cache_folders_of_this_kit_are_removed'),
                ("            if any(not stat.S_ISREG(f.st_mode) for _, f in files):\n                continue\n",
                 "", State, 'test_stale_boot_id_cache_folders_of_this_kit_are_removed')):
            with self.subTest(line=line[:60]):
                self.assertGreater(self.failing(cls, name, self.broken(line, instead)), 0, line)
                self.assertEqual(self.failing(cls, name, hf), 0, name)

    def test_without_each_913_fix_its_hold_test_fails(self):
        for line, instead, name in (
                ("        if use.get('gone'):\n", "        if use.get('gone'):\n            continue\n",
                 'test_a_cgroup_without_a_memory_controller_holds_its_whole_budget'),
                ("                continue                            # ended between the two looks: nothing runs "
                 "in it\n", '                pass\n', 'test_a_cgroup_that_ends_between_the_two_looks_holds_nothing'),
                ("        ram = None if use['demandMb'] is None else max(0, use['demandMb'] - swap)\n",
                 "        ram = use['demandMb']\n", 'test_its_swap_is_not_counted_as_ram_it_uses'),
                ("mb = h['budgetMb'] if raw == 'max' else int(raw) // (1 << 20)", "mb = int(raw) // (1 << 20)",
                 'test_memory_max_of_max_is_no_limit_not_an_unreadable_file'),
                ("    if len(cap['holds']) > MAX_HOLDS:\n", '    if False:\n', 'test_at_most_8_holds'),
                ("            if (b + '/').startswith(a + '/'):\n", '            if False:\n',
                 'test_nested_holds_are_refused'),
                ("              for n in sorted({r['name'] for r in res} & {h['label'] for h in where['holds']})]\n",
                 '              for n in []]\n', 'test_a_reservation_file_with_a_holds_name_gets_a_note')):
            with self.subTest(name=name):
                self.assertGreater(self.failing(Holds, name, self.broken(line, instead)), 0, line)
                self.assertEqual(self.failing(Holds, name, hf), 0, name)


if __name__ == '__main__':
    unittest.main()
