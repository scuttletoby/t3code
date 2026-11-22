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
import sys, tempfile, time, types, unittest
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

    def agent(self, name, demand_mb):
        folder = f'{AGENTS}/t3code-{name}-0a1b2c3d.scope'
        self.write(folder + '/memory.stat', f'anon {demand_mb * MB}\nfile 999999999\nshmem 0\n')
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
        return dict(os.environ, HEAVY_FACTS_ROOT=self.root, HEAVY_FACTS_CACHE=self.cache, HEAVY_QUEUE_ROOT=self.queue,
                    HEAVY_CONFIG=os.path.join(self.tmp, 'no-config.json'), HOME=self.home,
                    T3_CAPACITY_RESERVE_DIR=self.reserve, **more)

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

    def test_heavy_jobs_live_waiting_stale_and_garbled(self):
        build = 'job-build' if 'job-build' in RULE['kinds'] else RULE['unknownKinds']['job']
        self.m.job('build', 3072, demand_mb=1000)                       # live, admitted
        self.m.job('build', 3072, state='waiting')                      # counted as waiting only
        self.m.job('build', 3072, hold=False)                           # its heavy is gone: not counted, not removed
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
        self.m.write('/sys/fs/cgroup/cgroup.controllers', 'memory\n')      # cgroup v2 but no user manager
        shutil.rmtree(self.m.root + MANAGER)
        code, d = self.facts('--kind', WRITER, '--fresh')
        self.assertEqual((code, d['tier'], set(d['fits'].values())), (75, 'basic', {0}))

    def test_an_unreadable_signal_is_elevated_never_ok(self):
        self.m.write('/proc/pressure/memory', 'some garbage\nfull avg10=x\n')
        code, d = self.facts('--mb', '64')
        self.assertEqual((code, d['free'], d['level']), (75, False, 'elevated'))
        self.m.write('/proc/pressure/memory', CALM)
        self.m.write(MANAGER + '/heavy.slice/memory.pressure', 'nonsense\n')
        os.unlink(self.m.cache)                     # (else the elevated reading carries: calm must last first)
        self.assertEqual(self.facts('--mb', '64')[1]['level'], 'ok', 'no job of its own: the box passes nothing')
        self.m.job('build', 3072, demand_mb=100)
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

    def test_a_reservation_whose_processes_are_gone_still_holds_it_all(self):
        other = os.path.join(self.tmp, 'other')
        self.m.process(4001, other, 300)
        with open(os.path.join(self.m.reserve, 'game.conf'), 'w') as f:
            f.write(f'2000 {other}/\n')
        self.assertEqual(self.facts('--mb', '64')[1]['memory']['holdMb'], 1700)
        shutil.rmtree(f'{self.m.root}/proc/4001')    # its process ended: the room is still kept for it
        self.assertEqual(self.facts('--mb', '64', '--fresh')[1]['memory']['holdMb'], 2000)


class Holds(Case):
    """heavy.capacity.holds of the machine file: a cgroup outside the user managers (here a CI runners' slice) that
    is in use keeps its budget less its use for every other user."""
    CG = '/sys/fs/cgroup/ci.slice/ci-runners.slice'

    def machine_file(self, data, mode=0o644):
        path = self.m.root + '/etc/fork-kit/machine.json'
        for folder in ('/etc', '/etc/fork-kit'):
            os.makedirs(self.m.root + folder, exist_ok=True)
            os.chmod(self.m.root + folder, 0o755)
        with open(path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(path, mode)

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

    def test_cost_file_missing_learned_broken(self):
        build = 'job-build'
        if build not in RULE['kinds']:
            self.skipTest('no job-build kind in the rule')
        self.m.job('build', 3072, demand_mb=0)
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
        self.assertEqual(hf.read_cache(self.m.cache, data['atMs'], data['key'])[0]['queue']['agents'], 1)
        self.assertEqual(hf.read_cache(self.m.cache, data['atMs'], 'another key'), (None, None))

    def test_without_a_cache_folder_the_level_never_drops_straight_to_ok(self):
        with mock.patch.object(hf, 'RUN_USER', os.path.join(self.tmp, 'no-run')):
            for i in range(3):                      # a calm machine, called again and again: every call starts over
                code, d = self.facts('--mb', '64', now=NOW + datetime.timedelta(minutes=i * 10),
                                     env={'HEAVY_FACTS_CACHE': ''})
                self.assertEqual((code, d['level']), (75, 'elevated'), i)
            code, d = self.facts('--kind', WRITER, env={'HEAVY_FACTS_CACHE': ''})
            self.assertEqual((code, d['level']), (0, 'elevated'), 'the floor still admits')
        self.assertFalse(os.path.exists(self.m.cache))


def fake_heavy(tmp, body):
    path = os.path.join(tmp, 'fake-heavy')
    with open(path, 'w') as f:
        f.write('#!/bin/sh\n' + body + '\n')
    os.chmod(path, 0o755)
    return path


class T3Capacity(Case):
    FIRST = r'^available=\d+MB swapfree=\d+MB load=[0-9.]+/\d+cores agents=\d+ jobs=\d+ waiting=\d+ ' \
            r'reserved=\d+MB need=\d+MB free=-?\d+MB level=\w+ tier=\w+$'

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
        self.assertIn('need=3072MB', self.cli([T3CAP])[1].splitlines()[0])
        code, out, _ = self.cli([T3CAP, '--kind', WRITER])
        self.assertEqual((code, out.splitlines()[-1]), (0, 'OK'))
        for bad in (['x'], ['1', '2'], ['--kind']):
            self.assertEqual(self.cli([T3CAP] + bad)[0], 2, bad)

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


class Kinds(Case):
    """Every heavy ticket is a job: a kind the rule does not know (tsc, gradle, a project's own) is the rule's
    unknownKinds.job, never an agent."""
    JOB = RULE['unknownKinds']['job']

    def job_fits(self, d):
        return {k: v for k, v in d['fits'].items() if RULE['kinds'][k]['group'] == 'job'}

    def test_unknown_kinds_at_the_job_ceiling_leave_no_job_free(self):
        ceiling = RULE['ceilings']['job']
        for _ in range(ceiling):
            self.m.job('tsc', 3072, demand_mb=10)
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
                self.m.job(kind, 3072, demand_mb=10)
                code, d = self.facts('--kind', kind, '--fresh')
                self.assertEqual((code, d['kind'], d['queue']['running']), (0, self.JOB, 1))
                self.assertEqual(d['memory']['owedMb'], self.cost(self.JOB) - 10)

    def test_a_known_job_kind_keeps_its_own_cost(self):
        self.m.job('test', 3072, demand_mb=10)
        self.m.job('job-typecheck', 3072, demand_mb=10)
        d = self.facts('--kind', 'build')[1]
        self.assertEqual(d['memory']['owedMb'], self.cost('job-test') + self.cost('job-typecheck') - 20)


class OtherUsers(Case):
    """A caller running as another user (a CI runner) sees this user's work, and the other way round."""

    def test_other_users_jobs_and_agents_are_counted(self):
        idle = self.facts('--kind', 'build')[1]
        self.m.write(OTHER + '/heavy.slice/run-x1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        self.m.write(OTHER + '/heavy.slice/memory.pressure', CALM)
        self.m.write(OTHER + '/app.slice/app-t3code.slice/t3code-z-1.scope/memory.stat', f'anon {10 * MB}\nshmem 0\n')
        self.m.write(OTHER + '/app.slice/app-t3code.slice/other.scope/memory.stat', 'anon 0\nshmem 0\n')
        os.makedirs(self.m.root + OTHER + '/heavy.slice/run-gone.scope')          # ended: no memory.stat
        code, d = self.facts('--kind', 'build', '--fresh')
        self.assertEqual(d['queue'], {'running': 1, 'waiting': 0, 'agents': 1})
        self.assertEqual(d['memory']['owedMb'], self.cost(RULE['unknownKinds']['job']) - 10 + self.cost(WRITER) - 10)
        self.assertTrue(any(n.startswith('other users: 1 heavy job(s), 1 agent(s)') for n in d['notes']))
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
        self.m.job('build', 3072, demand_mb=1000)
        self.assertEqual(self.facts('--kind', 'build')[1]['memory']['owedMb'], self.cost('job-build') - 1000)
        self.m.write(f'{MANAGER}/heavy.slice/run-j1.scope/memory.stat', f'anon {1000 * MB}\nfi')
        self.assertEqual(self.facts('--kind', 'build', '--fresh')[1]['memory']['owedMb'], self.cost('job-build'))


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
