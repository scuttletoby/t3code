"""heavy's memory queue (lib/heavy_queue.py) on a fake cgroup tree and a fake /proc/meminfo, with a fake clock. No
systemd and no root; the queue folder and the state folder are temporary. Real processes hold and drop tickets
where the kernel's part matters (a waiter killed with -9, two callers at the same instant).

  python3 -I -B fork-kit/heavy/tests/queue.test.py

The state question, one test each: a waiter that dies, a job that crashes holding its reservation, a rerun, a
reboot (empty folders), old-format or garbled tickets, two callers at the same instant. RedCheck breaks each
admission and clean-up rule in a copy of lib/heavy_queue.py and shows the test for that rule fails.
"""
import contextlib, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, signal, subprocess, sys
import tempfile, time, types, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE = os.path.join(KIT, 'lib', 'heavy_queue.py')
SWEEP = os.path.join(KIT, 'lib', 'heavy_sweep.py')
HEAVY = os.path.join(KIT, 'bin', 'heavy')
MB = 1 << 20
SLICE_CG = '/user.slice/user-1.slice/user@1.service/heavy.slice'
DAY = 1_763_424_000          # 2025-11-18T00:00:00Z


def load_module(name, path, source=None):
    if source is None:
        loader = importlib.machinery.SourceFileLoader(name, path)
        spec = importlib.util.spec_from_loader(name, loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    mod = types.ModuleType(name)
    mod.__file__ = path
    exec(compile(source, path, 'exec'), mod.__dict__)
    return mod


class FakeHost:
    """A temporary cgroup tree for the slice and its scopes, a meminfo file, a queue root and a state folder."""

    def __init__(self, hq, budget_mb=5120):
        self.dir = tempfile.mkdtemp(prefix='heavy-queue-test-')
        self.cg_root = os.path.join(self.dir, 'cg')
        self.meminfo = os.path.join(self.dir, 'meminfo')
        os.makedirs(os.path.join(self.dir, 'run'))
        self.queue_root = os.path.join(self.dir, 'run', 'heavy-queue')
        self.state = os.path.join(self.dir, 'state')
        self.set_slice(used_mb=0)
        self.set_avail(16000)
        self.box = hq.Box('heavy.slice', SLICE_CG, budget_mb * MB, cg_root=self.cg_root, meminfo=self.meminfo,
                          queue_root=self.queue_root, state_dir=self.state)

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def _cg(self, folder, used_mb, cache_mb=0, swap_mb=0, full10=0.0, some60=0.0):
        os.makedirs(folder, exist_ok=True)
        files = {
            'memory.current': str((used_mb + cache_mb) * MB),
            'memory.stat': f'anon {used_mb * MB}\nfile {cache_mb * MB}\nactive_file {cache_mb * MB}\n'
                           f'inactive_file 0\nfile_mapped 0\nfile_dirty 0\nfile_writeback 0\n',
            'memory.swap.current': str(swap_mb * MB),
            'memory.pressure': f'some avg10=0.00 avg60={some60:.2f} avg300=0.00 total=1\n'
                               f'full avg10={full10:.2f} avg60=0.00 avg300=0.00 total=1\n',
        }
        for name, text in files.items():
            with open(os.path.join(folder, name), 'w') as f:
                f.write(text)

    def set_slice(self, used_mb, **kw):
        self._cg(self.cg_root + SLICE_CG, used_mb, **kw)

    def set_scope(self, scope, used_mb):
        self._cg(self.cg_root + SLICE_CG + '/' + scope, used_mb)

    def drop_scope(self, scope):
        shutil.rmtree(self.cg_root + SLICE_CG + '/' + scope, ignore_errors=True)

    def set_avail(self, mb, swap_total_mb=None, swap_free_mb=None):
        swap = '' if swap_total_mb is None else f'SwapTotal: {swap_total_mb * 1024} kB\nSwapFree: {swap_free_mb * 1024} kB\n'
        with open(self.meminfo, 'w') as f:
            f.write(f'MemTotal: 32000000 kB\nMemFree: 1000 kB\nMemAvailable: {mb * 1024} kB\n{swap}')

    def log(self):
        try:
            with open(os.path.join(self.state, 'waits.log')) as f:
                return [json.loads(line) for line in f]
        except FileNotFoundError:
            return []

    def files(self):
        d = self.box.queue_dir
        return sorted(n for n in os.listdir(d) if n != 'lock') if os.path.isdir(d) else []


class Clock:
    def __init__(self, start=DAY + 3600):
        self.t = self.start = start
        self.hooks = []

    def now(self):
        return self.t

    def monotonic(self):
        return self.t

    def sleep(self, s):
        if self.t - self.start > 1200:      # a broken wait limit (RedCheck) must fail, not hang
            raise RuntimeError('waited 20 minutes on the fake clock')
        self.t += max(s, 0.01)
        for h in list(self.hooks):
            h(self)


# A real process that joins the queue (the module path, the fake host's paths and the job as JSON in argv). It
# prints 'admitted <seq>' when admitted, then holds its ticket until its stdin closes; 'gave-up' after its wait.
CALLER = r'''
import importlib.machinery, importlib.util, json, sys, time
a = json.loads(sys.argv[1])
loader = importlib.machinery.SourceFileLoader('heavy_queue', a['module'])
spec = importlib.util.spec_from_loader('heavy_queue', loader)
hq = importlib.util.module_from_spec(spec); loader.exec_module(hq)
box = hq.Box('heavy.slice', a['slice_cg'], a['budget'] * hq.MB, cg_root=a['cg_root'], meminfo=a['meminfo'],
             queue_root=a['queue_root'], state_dir=a['state'])
cfg = hq.machine_file.fields({'pollSeconds': 0.2}, hq.SETTINGS, 'test')
q = hq.Queue(box, cfg, out=lambda m: None)
while time.time() < a['start']:
    time.sleep(0.001)
try:
    t = q.enter(a['kind'], a['mb'], a['scope'], 'x', a['wait'])
except hq.GaveUp:
    print('gave-up', flush=True); sys.exit(3)
print('admitted', t.seq, flush=True)
sys.stdin.read()
hq.release(q, t)
'''


class QueueTests(unittest.TestCase):
    mod = None

    @classmethod
    def hq(cls):
        if cls.mod is None:
            cls.mod = load_module('heavy_queue', QUEUE)
        return cls.mod

    def setUp(self):
        hq = self.hq()
        self.host = FakeHost(hq)
        self.addCleanup(self.host.close)
        self.clock = Clock()
        self.lines = []
        self.cfg = hq.machine_file.fields({}, hq.SETTINGS, 'test')
        self.q = self.queue()

    def queue(self, **cfg):
        hq = self.hq()
        settings = dict(self.cfg, **cfg)
        return hq.Queue(self.host.box, settings, now=self.clock.now, sleep=self.clock.sleep,
                        monotonic=self.clock.monotonic, out=self.lines.append)

    def other(self, kind='other', mb=1000, state='waiting', since=None, scope=None):
        """A ticket held by this test (a second open file: to the queue it is another live job)."""
        with self.q:
            t = self.q.create(kind, mb, scope or f'heavy-{kind}-0.scope', kind)
            t.data['state'] = state
            if since is not None:
                t.data['since'] = t.data['sinceMono'] = since
            self.q.write(t)
        self.addCleanup(self.hq().close, t)
        return t

    def live(self):
        with self.q:
            return self.q.tickets()

    def decide(self, mb, keep=True, **cfg):
        q = self.queue(**cfg) if cfg else self.q
        me = self.other('me', mb)
        with q:
            answer = q.decide(me, q.tickets(), self.host.box.facts(q.cfg))
        if not keep:
            self.hq().close(me)                     # gone before the next look
        return answer

    # ---------- admission ----------

    def test_a_job_that_fits_is_admitted_at_once_and_logged(self):
        t = self.q.enter('gradle', 2048, 'heavy-gradle-1.scope', 'gradlew', 60)
        self.assertEqual(t.state, 'admitted')
        self.assertEqual([e['outcome'] for e in self.host.log()], ['admitted'])
        self.assertEqual(self.host.log()[0]['waitedS'], 0)
        self.assertEqual(self.lines, [])            # nothing printed when nothing was waited for
        self.hq().release(self.q, t)
        self.assertEqual(self.host.files(), [])

    def test_used_counts_anon_and_swap_but_not_clean_cache(self):
        self.host.set_slice(used_mb=1000, cache_mb=3000, swap_mb=500)
        facts = self.host.box.facts(self.cfg)
        self.assertEqual(facts['used'], 1500 * MB)
        self.assertEqual(facts['budget'], (5120 - 512) * MB)

    def test_waits_with_reason_and_position_until_memory_frees(self):
        self.host.set_slice(used_mb=3500)
        polls = []

        def free_later(clock):
            polls.append(clock.t)
            if len(polls) == 5:
                self.host.set_slice(used_mb=500)
        self.clock.hooks.append(free_later)
        t = self.q.enter('typecheck', 3072, 's.scope', 'tsc', 600)
        self.assertEqual(t.state, 'admitted')
        self.assertEqual(len(polls), 5)
        self.assertEqual(len([m for m in self.lines if m.startswith('waiting')]), 1)   # printed on change only
        self.assertIn('needs 3072 MB, room 1108 MB', self.lines[0])
        self.assertIn('position 1 of 1', self.lines[0])
        self.assertIn('admitted typecheck (3072 MB) after waiting 10s', self.lines[-1])
        self.assertEqual(self.host.log()[-1]['waitedS'], 10)

    def test_a_waiting_line_is_printed_again_once_a_minute(self):
        self.host.set_slice(used_mb=4000)
        with self.assertRaises(self.hq().GaveUp):
            self.q.enter('big', 3072, 's.scope', 'x', 150)
        self.assertEqual(len([m for m in self.lines if m.startswith('waiting')]), 3)    # at 0, 60 and 120 s

    def test_admitted_jobs_hold_what_they_have_not_used_yet(self):
        a = self.other('gradle', 2048, 'admitted', scope='heavy-a.scope')
        ok, why, fig = self.decide(3000)
        self.assertFalse(ok)                        # 4608 - 0 used - 2048 owed = 2560 < 3000
        self.assertEqual(fig['owedMb'], 2048)
        self.host.set_scope('heavy-a.scope', 1500)
        self.host.set_slice(used_mb=1500)
        ok, why, fig = self.decide(3000)            # 4608 - 1500 used - 548 owed = 2560: the same room
        self.assertFalse(ok)
        self.assertEqual(fig['owedMb'], 548)
        self.host.set_scope('heavy-a.scope', 2500)  # grown past its reservation: owes nothing, counted once in used
        self.host.set_slice(used_mb=2500)
        self.assertEqual(self.decide(2000)[2]['owedMb'], 0)
        self.assertTrue(a)

    def test_the_machines_free_memory_limits_too(self):
        self.host.set_avail(3000)                   # 3000 - 1024 reserve = 1976
        ok, why, fig = self.decide(2048)
        self.assertFalse(ok)
        self.assertEqual(fig['roomMb'], 1976)
        self.assertTrue(self.decide(1900)[0])

    def test_memory_pressure_admits_nobody(self):
        self.host.set_slice(used_mb=0, full10=6.0)
        ok, why, _ = self.decide(100)
        self.assertFalse(ok)
        self.assertIn('pressure', why)
        self.host.set_slice(used_mb=0, some60=11.0)
        self.assertFalse(self.decide(100)[0])
        self.host.set_slice(used_mb=0, full10=4.0, some60=9.0)
        self.assertTrue(self.decide(100)[0])

    def test_first_come_first_served(self):
        self.other('a', 3000)                       # waiting, ahead, fits: its share is kept for it
        ok, why, fig = self.decide(3000)
        self.assertFalse(ok)
        self.assertEqual(fig['position'], 2)
        self.assertEqual(fig['roomMb'], 4608 - 3000)

    def test_a_small_job_passes_a_big_one_until_the_pass_limit(self):
        self.other('running', 2000, 'admitted', scope='heavy-run.scope')    # its end makes room for big
        self.host.set_scope('heavy-run.scope', 2000)
        self.host.set_slice(used_mb=2000)
        self.other('big', 4000, since=self.clock.now() - 599)
        self.assertTrue(self.decide(1000)[0])       # big does not fit now; small passes
        self.other('big2', 4000, since=self.clock.now() - 600)
        ok, why, _ = self.decide(1000)
        self.assertFalse(ok)                        # big2 has waited 10 min: nothing passes it any more
        self.assertIn('pass limit', why)

    def test_a_job_larger_than_the_budget_runs_alone(self):
        ok, why, _ = self.decide(6000)
        self.assertTrue(ok)
        self.assertIn('alone', why)

    def test_a_job_larger_than_the_budget_waits_for_the_others(self):
        self.other('small', 500, 'admitted')
        ok, why, _ = self.decide(6000)
        self.assertFalse(ok)
        self.assertIn('larger than the budget', why)

    def test_a_first_job_larger_than_the_budget_goes_before_later_ones(self):
        self.other('huge', 9000)
        ok, why, _ = self.decide(100)
        self.assertFalse(ok)
        self.assertIn('runs alone', why)

    def test_a_job_that_cannot_fit_even_when_the_queue_empties_does_not_hold_it(self):
        self.other('small', 512, 'admitted', scope='heavy-small.scope')
        self.host.set_scope('heavy-small.scope', 512)
        self.host.set_slice(used_mb=512)
        self.host.set_avail(4000)                   # once small ends: 4000 + 512 - 1024 = 3488 < 3500
        self.other('big', 3500, since=self.clock.now() - 601)
        ok, why, _ = self.decide(512)
        self.assertTrue(ok, why)                    # holding the queue for big would help nobody

    def test_past_the_pass_limit_nothing_passes_while_a_running_job_would_make_room(self):
        self.other('running', 1000, 'admitted', scope='heavy-run.scope')
        self.host.set_scope('heavy-run.scope', 1000)
        self.host.set_slice(used_mb=1000)
        self.host.set_avail(4000)                   # once running ends: 4000 + 1000 - 1024 = 3976 >= 3500
        self.other('big', 3500, since=self.clock.now() - 601)
        ok, why, _ = self.decide(512)
        self.assertFalse(ok)
        self.assertIn('pass limit', why)

    def test_a_job_larger_than_the_budget_still_needs_the_machines_free_memory(self):
        self.host.set_avail(1200)
        ok, why, _ = self.decide(6000, keep=False)
        self.assertFalse(ok)                        # alone, but the machine does not have the budget free
        self.assertIn('larger than the budget', why)
        self.host.set_avail(16000)
        self.host.set_slice(used_mb=1000)           # plain heavy jobs use more than the headroom
        self.assertFalse(self.decide(6000, keep=False)[0])
        self.host.set_slice(used_mb=0)
        self.assertTrue(self.decide(6000, keep=False)[0])

    def test_swap_under_the_floor_admits_nobody(self):
        self.host.set_avail(16000, swap_total_mb=4096, swap_free_mb=900)
        ok, why, _ = self.decide(100)
        self.assertFalse(ok)
        self.assertIn('swap free 900 MB is under swapFreeMinMb 1024 MB', why)
        self.assertTrue(self.decide(100, swapFreeMinMb=0)[0])
        self.host.set_avail(16000, swap_total_mb=4096, swap_free_mb=1100)
        self.assertTrue(self.decide(100)[0])
        self.host.set_avail(16000, swap_total_mb=0, swap_free_mb=0)      # no swap at all: no swap check
        self.assertTrue(self.decide(100)[0])

    def hooked(self, answers, **cfg):
        """A queue whose admit command is a fake: it answers from the list (the last answer repeats)."""
        calls = []

        def hook(argv, timeout):
            calls.append((argv, timeout))
            return answers[min(len(calls), len(answers)) - 1]
        settings = dict(self.cfg, admitCommand=['/bin/true', '--mb', '{mb}'], **cfg)
        q = self.hq().Queue(self.host.box, settings, now=self.clock.now, sleep=self.clock.sleep,
                            monotonic=self.clock.monotonic, out=self.lines.append, hook=hook)
        return q, calls

    def test_the_admit_command_is_asked_with_the_reservation(self):
        q, calls = self.hooked([(0, '')])
        t = q.enter('gradle', 2048, 's.scope', 'x', 60)
        self.assertEqual(t.state, 'admitted')
        self.assertEqual(calls, [(['/bin/true', '--mb', '2048'], 20)])
        self.hq().release(q, t)

    def test_the_admit_command_saying_no_keeps_the_job_waiting_in_its_place(self):
        q, calls = self.hooked([(1, 'available 900MB: NOT OK'), (None, 'no answer in 20 s'), (0, '')])
        self.other('later', 100)                    # joins after: must not get ahead
        t = q.enter('gradle', 2048, 's.scope', 'x', 600)
        self.assertEqual(t.state, 'admitted')
        self.assertEqual(len(calls), 3)
        self.assertIn('the admit command says not now (exit 1: available 900MB: NOT OK)', self.lines[0])
        self.hq().release(q, t)

    def test_the_admit_command_is_not_asked_when_the_queue_says_no(self):
        self.host.set_slice(used_mb=4500)
        q, calls = self.hooked([(0, '')])
        with self.assertRaises(self.hq().GaveUp):
            q.enter('gradle', 2048, 's.scope', 'x', 30)
        self.assertEqual(calls, [])

    def test_a_real_admit_command_that_hangs_is_cut_off(self):
        hq = self.hq()
        start = time.monotonic()
        code, line = hq.run_hook(['/bin/sh', '-c', 'sleep 60 & sleep 60'], 1)
        self.assertIsNone(code)
        self.assertIn('no answer in 1 s', line)
        self.assertLess(time.monotonic() - start, 10)
        self.assertEqual(hq.run_hook(['/bin/sh', '-c', 'echo checking; echo NOT OK; exit 3'], 5), (3, 'NOT OK'))
        self.assertEqual(hq.run_hook(['/bin/sh', '-c', 'exit 0'], 5)[0], 0)
        self.assertIsNone(hq.run_hook([os.path.join(self.host.dir, 'missing')], 5)[0])

    def refusing(self, refused_mb):
        """A queue whose admit command refuses only jobs of refused_mb (another tool's reservation leaves too little
        for them, as a t3-capacity-style check says)."""
        def hook(argv, timeout):
            return (1, 'NOT OK') if argv[-1] == str(refused_mb) else (0, '')
        settings = dict(self.cfg, admitCommand=['/bin/true', '{mb}'])
        return self.hq().Queue(self.host.box, settings, now=self.clock.now, sleep=self.clock.sleep,
                               monotonic=self.clock.monotonic, out=self.lines.append, hook=hook)

    def test_a_waiter_the_admit_command_refuses_keeps_no_room_for_itself(self):
        q = self.refusing(3072)                     # budget 4608 MB; nothing runs
        x = self.other('x', 3072, since=self.clock.now() - 7000)    # first, and long past the pass limit
        with q:
            ok, why, _ = q.look(x)
        self.assertFalse(ok)
        self.assertIn('admit command says not now', why)
        self.assertIs(self.live()[0].data['hookNo'], True)          # written into its ticket
        y = q.enter('y', 2048, 'y.scope', 'x', 0)   # fits the queue and the admit command: starts on this look
        self.assertEqual(y.state, 'admitted')
        self.hq().release(q, y)

    def test_a_refused_waiter_past_the_pass_limit_still_holds_while_a_running_job_would_make_room(self):
        self.other('running', 1000, 'admitted', scope='heavy-run.scope')
        self.host.set_scope('heavy-run.scope', 1000)
        self.host.set_slice(used_mb=1000)
        q = self.refusing(3072)
        x = self.other('x', 3072, since=self.clock.now() - 601)
        with q:
            self.assertFalse(q.look(x)[0])
        ok, why, _ = self.decide(2048)              # y fits, but x gets its turn once the running job ends
        self.assertFalse(ok)
        self.assertIn('pass limit', why)

    def test_a_real_admit_command_whose_leftover_holds_its_output_is_not_now(self):
        hq = self.hq()
        pid_file = os.path.join(self.host.dir, 'leftover.pid')
        start = time.monotonic()
        code, line = hq.run_hook(['/bin/sh', '-c', f'sleep 60 & echo $! > {pid_file}; echo OK; exit 0'], 20)
        self.assertLess(time.monotonic() - start, 5)    # not the 20 s timeout
        self.assertIsNone(code)
        self.assertIn('exit 0, but a process it started still holds its output open', line)
        with open(pid_file) as f:
            pid = int(f.read())
        end = time.monotonic() + 5
        while os.path.exists(f'/proc/{pid}') and time.monotonic() < end:
            time.sleep(0.05)
        self.assertFalse(os.path.exists(f'/proc/{pid}'), 'the leftover of the admit command was not killed')
        # a leftover that lets go of the output does not change the answer
        self.assertEqual(hq.run_hook(['/bin/sh', '-c', 'sleep 1 >/dev/null 2>&1 & echo fine; exit 0'], 5), (0, 'fine'))

    def test_a_missing_admit_command_stops_the_job_that_needs_it_not_the_queue(self):
        hq = self.hq()
        missing = os.path.join(self.host.dir, 'gone')
        q = self.queue(admitCommand=[missing, '{mb}'])
        with self.assertRaises(hq.Refused) as cm:
            q.enter('gradle', 2048, 's.scope', 'x', 60)
        self.assertIn(f'{missing} is not a program this user can run', str(cm.exception))
        self.assertEqual(self.host.files(), [])     # its ticket is gone
        self.assertEqual(hq.status(q)['settings']['admitCommand'], [missing, '{mb}'])    # heavy queue still works
        self.host.set_slice(used_mb=4500)
        with self.assertRaises(hq.GaveUp):          # a job the queue's own rule holds still just waits
            q.enter('gradle', 2048, 's.scope', 'x', 5)

    def hold_lock(self):
        """The queue lock, taken through another open file (to the queue: another process holds it)."""
        with self.q:
            pass
        fd = os.open(os.path.join(self.host.box.queue_dir, 'lock'), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.addCleanup(os.close, fd)
        return fd

    def test_a_finished_job_deletes_its_ticket_while_the_lock_is_taken(self):
        t = self.q.enter('gradle', 2048, 's.scope', 'x', 60)
        self.hold_lock()                            # a slow admit command, or a stuck process
        start = self.clock.now()
        self.hq().release(self.q, t)
        self.assertEqual(self.host.files(), [])
        self.assertIsNone(t.fd)
        self.assertEqual(self.clock.now(), start)   # and it did not wait for the lock

    def test_peaks_and_verbs_wait_longer_than_one_admit_command(self):
        hq = self.hq()
        fd = self.hold_lock()
        give_back = self.clock.now() + hq.LOCK_TIMEOUT + 10     # an admit command that took 40 s of its 50 s
        self.clock.hooks.append(lambda c: c.t >= give_back and fcntl.flock(fd, fcntl.LOCK_UN))
        q = self.queue(admitCommand=['/bin/true'], admitTimeoutSeconds=50)
        self.assertEqual(q.lock_wait(), hq.LOCK_TIMEOUT + 50)
        self.assertEqual(self.q.lock_wait(), hq.LOCK_TIMEOUT)   # no admit command: the plain wait
        hq.record_peak(q, 'gradle', 1000)
        self.assertEqual(hq.read_peaks(self.host.state)[0], {'gradle': [1000]})
        self.assertEqual(hq.status(q)['queue'], {'running': 0, 'waiting': 0})

    def test_a_stop_signal_saves_the_peak_with_a_short_try_only(self):
        hq = self.hq()
        t = self.other('gradle', 2048, 'admitted', scope='heavy-g.scope')
        self.hold_lock()
        self.host.set_scope('heavy-g.scope', 1400)
        s = hq.Sampler(self.queue(admitCommand=['/bin/true']), t)
        s.sample()
        start = self.clock.now()
        s.record_soon()
        self.assertLessEqual(self.clock.now() - start, hq.STOP_LOCK_WAIT + 0.5)
        self.assertIn('could not record the peak of gradle', self.lines[-1])

    def test_a_stop_just_after_the_admission_stays_pending_for_heavy(self):
        hq = self.hq()
        before = signal.pthread_sigmask(signal.SIG_BLOCK, [])
        self.addCleanup(signal.pthread_sigmask, signal.SIG_SETMASK, before)
        t = self.q.enter('gradle', 2048, 's.scope', 'x', 60, keep_blocked=True)
        self.addCleanup(hq.release, self.q, t)
        self.assertIn(signal.SIGINT, signal.pthread_sigmask(signal.SIG_BLOCK, []))
        os.kill(os.getpid(), signal.SIGINT)         # Ctrl-C right after the admission: no KeyboardInterrupt here
        with self.assertRaises(hq.Stopped) as cm:
            hq.stopped_already()                    # heavy takes it before it starts the command
        self.assertEqual(cm.exception.sig, signal.SIGINT)
        signal.pthread_sigmask(signal.SIG_SETMASK, before)
        t2 = self.q.enter('gradle', 100, 's2.scope', 'x', 60)   # without keep_blocked the mask comes back
        hq.release(self.q, t2)
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), before)

    def test_gives_up_after_the_wait_and_leaves_no_ticket(self):
        self.host.set_slice(used_mb=4500)
        with self.assertRaises(self.hq().GaveUp) as cm:
            self.q.enter('gradle', 2048, 's.scope', 'x', 30)
        self.assertIn('needs 2048 MB', str(cm.exception))
        self.assertEqual(self.host.log()[-1]['outcome'], 'gave-up')
        self.assertGreaterEqual(self.host.log()[-1]['waitedS'], 30)
        self.assertEqual(self.host.files(), [])

    def test_a_signal_while_waiting_stops_cleanly(self):
        self.host.set_slice(used_mb=4500)
        self.clock.hooks.append(lambda c: os.kill(os.getpid(), signal.SIGTERM))
        old = signal.signal(signal.SIGTERM, signal.SIG_DFL)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        with self.assertRaises(self.hq().Stopped) as cm:
            self.q.enter('gradle', 2048, 's.scope', 'x', 600)
        self.assertEqual(cm.exception.sig, signal.SIGTERM)
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_DFL)      # put back
        self.assertEqual(self.host.log()[-1]['outcome'], 'stopped')
        self.assertEqual(self.host.files(), [])

    def test_a_signal_while_the_lock_is_held_is_taken_after_it_is_given_back(self):
        hq = self.hq()
        seen = []

        def handler(sig, frame):                    # a stop must never arrive as an exception inside the lock
            seen.append(sig)
            raise RuntimeError('a signal handler ran while heavy held the queue lock')
        old = signal.signal(signal.SIGTERM, handler)
        self.addCleanup(signal.signal, signal.SIGTERM, old)
        decide = self.q.decide

        def decide_then_signal(*a):
            os.kill(os.getpid(), signal.SIGTERM)
            return decide(*a)
        self.q.decide = decide_then_signal
        with self.assertRaises(hq.Stopped) as cm:
            self.q.enter('gradle', 2048, 's.scope', 'x', 600)
        self.assertEqual(cm.exception.sig, signal.SIGTERM)
        self.assertEqual(seen, [])
        self.assertEqual(self.host.log()[-1]['outcome'], 'stopped')     # a stop that came while deciding wins
        self.assertEqual(self.host.files(), [])
        self.assertEqual(signal.pthread_sigmask(signal.SIG_BLOCK, []), set())
        fd = os.open(os.path.join(self.host.box.queue_dir, 'lock'), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)                  # the lock is free

    def test_a_lock_held_by_a_stuck_process_ends_the_wait_on_time(self):
        hq = self.hq()
        with self.q:
            pass                                    # makes the folder and the lock file
        fd = os.open(os.path.join(self.host.box.queue_dir, 'lock'), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)              # another open file: to the queue, another process
        start = self.clock.t
        with self.assertRaises(hq.GaveUp) as cm:
            self.q.enter('gradle', 2048, 's.scope', 'x', 30)
        self.assertIn('queue lock', str(cm.exception))
        self.assertLessEqual(self.clock.t - start, 31)
        self.assertEqual(self.host.files(), [])

    # ---------- the state question ----------

    def test_a_waiter_killed_with_9_frees_its_place(self):
        self.host.set_slice(used_mb=4500)
        p = self.caller('dies', 1000, wait=60)
        self.wait_for(lambda: len(self.live()) == 1)
        p.kill()
        p.wait()
        self.assertEqual(self.live(), [])
        self.assertEqual(self.host.files(), [])

    def test_a_crashed_job_frees_its_reservation(self):
        p = self.caller('crash', 4000, wait=60)
        self.assertTrue(p.stdout.readline().startswith('admitted'))
        self.assertFalse(self.decide(1000)[0])      # the crashed job's 4000 MB is still held
        p.kill()
        p.wait()
        self.assertTrue(self.decide(1000)[0])       # gone with its process; nothing to clean by hand
        self.assertNotIn('crash', [t.kind for t in self.live()])

    def test_a_ticket_whose_holder_closed_it_is_deleted_on_the_next_look(self):
        t = self.other('gone', 1000, 'admitted')
        self.hq().close(t)
        self.assertEqual(self.live(), [])

    def test_a_rerun_starts_clean_and_keeps_order(self):
        for _ in range(3):
            t = self.q.enter('gradle', 1000, 's.scope', 'x', 60)
            self.assertEqual(t.seq, 1)              # the last run's ticket is gone
            self.hq().release(self.q, t)
        a = self.other('a', 100)
        b = self.other('b', 100)
        self.assertLess(a.seq, b.seq)

    def test_after_a_reboot_the_folders_are_made_again(self):
        shutil.rmtree(os.path.join(self.host.dir, 'run', 'heavy-queue'), ignore_errors=True)
        shutil.rmtree(self.host.state, ignore_errors=True)
        t = self.q.enter('gradle', 1000, 's.scope', 'x', 60)
        self.assertEqual(oct(os.stat(self.host.box.queue_dir).st_mode & 0o777), '0o700')
        self.assertEqual(len(self.host.log()), 1)
        self.hq().release(self.q, t)

    def test_no_runtime_folder_is_refused(self):
        shutil.rmtree(os.path.join(self.host.dir, 'run'))
        with self.assertRaises(self.hq().Refused):
            self.q.enter('gradle', 1000, 's.scope', 'x', 60)

    def test_a_queue_folder_others_can_change_is_refused(self):
        self.live()
        os.chmod(self.host.box.queue_dir, 0o777)
        with self.assertRaises(self.hq().Refused):
            self.live()

    def test_a_garbled_or_old_live_ticket_counts_as_admitted_at_3072(self):
        for raw in (b'{"v": 0, "kind": "old", "mb": 10}', b'\x00garbage', b''):
            with self.subTest(raw=raw):
                t = self.other('x', 10)
                os.ftruncate(t.fd, 0)
                os.pwrite(t.fd, raw, 0)
                live = self.live()
                self.assertEqual([(x.state, x.mb) for x in live], [('admitted', 3072)])
                self.assertIn('cannot be read', live[0].note)
                self.hq().close(t)
                self.assertEqual(self.live(), [])  # and deleted once nobody holds it

    def test_other_files_and_links_in_the_folder_are_ignored_or_removed(self):
        self.live()
        d = self.host.box.queue_dir
        with open(os.path.join(d, 'notes.txt'), 'w') as f:
            f.write('x')
        os.symlink('/etc/passwd', os.path.join(d, 't-7-1.json'))
        self.assertEqual(self.live(), [])
        self.assertEqual(self.host.files(), ['notes.txt'])

    def test_two_callers_at_the_same_instant(self):
        self.host.set_slice(used_mb=1000)           # room 3608: one 3000 MB job at a time
        start = time.time() + 0.5
        p1 = self.caller('one', 3000, start=start)
        p2 = self.caller('two', 3000, start=start)
        first = self.first_line(p1, p2)
        self.assertIsNotNone(first)
        winner, loser = first
        self.assertTrue(winner.stdout.readline().startswith('admitted'))
        seqs = sorted(t.seq for t in self.live())
        self.assertEqual(seqs, [1, 2])              # one lock: two different places, never the same one
        self.assertEqual(sorted(t.state for t in self.live()), ['admitted', 'waiting'])
        winner.stdin.close()
        winner.wait()
        self.assertTrue(loser.stdout.readline().startswith('admitted'))
        loser.stdin.close()
        loser.wait()
        self.assertEqual(self.live(), [])

    # helpers for real processes

    def caller(self, kind, mb, wait=60, start=0):
        args = {'module': QUEUE, 'slice_cg': SLICE_CG, 'budget': 5120, 'cg_root': self.host.cg_root,
                'meminfo': self.host.meminfo, 'queue_root': self.host.queue_root, 'state': self.host.state,
                'kind': kind, 'mb': mb, 'scope': f'heavy-{kind}-0.scope', 'wait': wait, 'start': start}
        p = subprocess.Popen([sys.executable, '-I', '-B', '-c', CALLER, json.dumps(args)], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, text=True)
        self.addCleanup(self.stop, p)
        return p

    @staticmethod
    def stop(p):
        if p.poll() is None:
            p.kill()
        p.wait()
        for f in (p.stdin, p.stdout):
            if f:
                f.close()

    @staticmethod
    def wait_for(cond, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            if cond():
                return
            time.sleep(0.02)
        raise AssertionError('timed out')

    def first_line(self, p1, p2):
        """(the caller that was admitted, the other one) once one ticket is admitted."""
        self.wait_for(lambda: any(t.state == 'admitted' for t in self.live()) and len(self.live()) == 2)
        admitted = [t for t in self.live() if t.state == 'admitted'][0]
        return (p1, p2) if admitted.kind == 'one' else (p2, p1)

    # ---------- wait log and summary ----------

    def test_minutes_waited_per_day(self):
        os.makedirs(self.host.state)
        rows = [('2025-11-17T10:00:00Z', 'gradle', 120, 'admitted'), ('2025-11-18T01:00:00Z', 'gradle', 300, 'admitted'),
                ('2025-11-18T02:00:00Z', 'typecheck', 600, 'gave-up'), ('2025-11-01T00:00:00Z', 'gradle', 9999, 'admitted')]
        with open(os.path.join(self.host.state, 'waits.log'), 'w') as f:
            for at, kind, w, out in rows:
                f.write(json.dumps({'at': at, 'kind': kind, 'mb': 1, 'waitedS': w, 'outcome': out}) + '\n')
            f.write('{"half a line\n')
        days = self.hq().waits(self.host.state, 7, DAY + 3600)
        self.assertEqual([d['day'] for d in days], ['2025-11-18', '2025-11-17'])
        self.assertEqual(days[0]['waitedMinutes'], 15.0)
        self.assertEqual(days[0]['longestMinutes'], 10.0)
        self.assertEqual(days[0]['gaveUp'], 1)
        self.assertEqual(days[0]['kinds']['gradle'], {'jobs': 1, 'waitedMinutes': 5.0})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(self.hq().waits_main(self.host.state, ['--days', '1'], now=DAY + 3600), 0)
        self.assertIn('2025-11-18', out.getvalue())
        self.assertNotIn('2025-11-17', out.getvalue())

    def test_no_wait_log_yet(self):
        self.assertEqual(self.hq().waits(self.host.state, 7, DAY), [])

    def test_the_wait_log_stays_small(self):
        hq = self.hq()
        os.makedirs(self.host.state)
        path = os.path.join(self.host.state, 'waits.log')
        with open(path, 'w') as f:
            f.write('x' * (hq.WAITS_MAX_BYTES + 10) + '\n' + ('{}\n' * (hq.WAITS_KEEP_LINES + 5)))
        with self.q:
            self.q.log('gradle', 1, 0, 'admitted')
        with open(path) as f:
            lines = f.readlines()
        self.assertEqual(len(lines), hq.WAITS_KEEP_LINES)
        self.assertIn('gradle', lines[-1])

    def test_queue_status_has_the_free_v1_memory_and_queue_fields(self):
        self.other('a', 1000, 'admitted')
        self.other('b', 4000)
        st = self.hq().status(self.q)
        self.assertEqual(st['queue'], {'running': 1, 'waiting': 1})
        self.assertEqual(st['memory']['reservedMb'], 1000)
        self.assertEqual(st['memory']['freeMb'], 4608 - 1000)
        for key in ('budgetMb', 'reservedMb', 'freeMb', 'pressureFull10'):
            self.assertIn(key, st['memory'])

    # ---------- peaks ----------

    def test_the_highest_recent_peak_is_learned_from_the_first_run(self):
        hq = self.hq()
        self.assertEqual(hq.reservation('gradle', 2048, self.host.state)[0], 2048)
        hq.record_peak(self.q, 'gradle', 2300)
        mb, how = hq.reservation('gradle', 2048, self.host.state)
        self.assertEqual(mb, 2688)                  # 2300 x 1.15 = 2645, rounded up to 128 MB
        self.assertIn('learned', how)

    def test_one_high_run_among_many_low_ones_sets_the_reservation(self):
        hq = self.hq()
        for peak in [1400] + [450] * 19:            # a median would learn 518 MB and let the 1400 MB run in short
            hq.record_peak(self.q, 'gradle', peak)
        self.assertEqual(hq.reservation('gradle', 1024, self.host.state)[0], 1664)

    def test_learning_never_goes_below_the_start_value(self):
        hq = self.hq()
        for peak in (450, 500, 480):
            hq.record_peak(self.q, 'gradle', peak)
        mb, how = hq.reservation('gradle', 2048, self.host.state)
        self.assertEqual(mb, 2048)
        self.assertIn('start value', how)

    def test_learned_values_stay_at_most_four_times_the_start(self):
        hq = self.hq()
        hq.record_peak(self.q, 'huge', 100000)
        self.assertEqual(hq.reservation('huge', 768, self.host.state)[0], 3072)

    def test_one_leaky_run_does_not_lock_its_kind_out(self):
        hq = self.hq()
        for peak in [1400] * 19 + [4200]:          # one build leaked up to the slice's memory.high
            hq.record_peak(self.q, 'gradle', peak)
        self.assertEqual(hq.reservation('gradle', 2048, self.host.state)[0], 4864)     # uncapped: > budget 4608
        self.host.set_slice(used_mb=900)            # a plain heavy job (a test server) in the slice
        mb, how = self.q.reservation('gradle', 2048)
        self.assertEqual(mb, 4608 - 1024)           # the budget minus vmReserveMb
        self.assertIn('capped', how)
        t = self.q.enter('gradle', mb, 'g.scope', 'gradlew', 0)
        self.assertEqual(t.state, 'admitted')       # the kind still runs; the slice's limits catch a real overrun
        self.hq().release(self.q, t)

    def test_the_cap_never_goes_below_the_start_value_or_lowers_a_normal_peak(self):
        hq = self.hq()
        hq.record_peak(self.q, 'big', 9000)
        self.assertEqual(self.q.reservation('big', 5000)[0], 5000)    # a start value above the budget stays
        hq.record_peak(self.q, 'gradle', 2300)
        self.assertEqual(self.q.reservation('gradle', 2048)[0], 2688)  # under the cap: the learned value

    def test_a_give_up_of_a_job_as_large_as_the_budget_says_so(self):
        self.host.set_slice(used_mb=900)
        with self.assertRaises(self.hq().GaveUp) as cm:
            self.q.enter('gradle', 4608, 'g.scope', 'gradlew', 0)
        self.assertIn('is not below the whole budget of 4608 MB', str(cm.exception))

    def test_only_the_last_20_runs_count(self):
        hq = self.hq()
        for peak in [9000] * 5 + [2500] * 20:
            hq.record_peak(self.q, 'gradle', peak)
        self.assertEqual(hq.reservation('gradle', 2048, self.host.state)[0], 2944)

    def test_a_broken_peak_file_starts_a_new_history(self):
        hq = self.hq()
        os.makedirs(self.host.state)
        with open(os.path.join(self.host.state, 'peaks.json'), 'w') as f:
            f.write('{broken')
        self.assertEqual(hq.reservation('gradle', 2048, self.host.state, out=self.lines.append)[0], 2048)
        hq.record_peak(self.q, 'gradle', 1000)
        self.assertEqual(hq.read_peaks(self.host.state)[0], {'gradle': [1000]})

    def test_a_peak_file_of_a_newer_version_is_left_alone(self):
        hq = self.hq()
        os.makedirs(self.host.state)
        path = os.path.join(self.host.state, 'peaks.json')
        with open(path, 'w') as f:
            json.dump({'v': 99, 'kinds': {'gradle': [1, 2, 3]}}, f)
        hq.record_peak(self.q, 'gradle', 1000)
        with open(path) as f:
            self.assertEqual(json.load(f)['v'], 99)

    def test_the_sampler_records_the_highest_use_of_the_scope(self):
        hq = self.hq()
        t = self.other('gradle', 2048, 'admitted', scope='heavy-g.scope')
        s = hq.Sampler(self.q, t)
        s.sample()                                  # the scope is not there yet
        for used in (300, 1400, 900):
            self.host.set_scope('heavy-g.scope', used)
            s.sample()
        self.host.drop_scope('heavy-g.scope')
        s.sample()
        s.record()
        self.assertEqual(hq.read_peaks(self.host.state)[0], {'gradle': [1400]})

    def test_temporary_files_of_killed_writers_are_removed(self):
        hq = self.hq()
        os.makedirs(self.host.state)
        p = subprocess.Popen(['true'])
        p.wait()                                    # a pid that is gone
        dead = os.path.join(self.host.state, f'peaks.json.{p.pid}.tmp')
        alive = os.path.join(self.host.state, f'waits.log.{os.getppid()}.tmp')
        for path in (dead, alive):
            with open(path, 'w') as f:
                f.write('half')
        hq.record_peak(self.q, 'gradle', 1000)
        self.assertFalse(os.path.exists(dead))
        self.assertTrue(os.path.exists(alive))

    def test_a_run_that_was_never_seen_records_nothing(self):
        hq = self.hq()
        t = self.other('quick', 100, 'admitted')
        hq.Sampler(self.q, t).record()
        self.assertFalse(os.path.exists(os.path.join(self.host.state, 'peaks.json')))

    # ---------- settings and the command line ----------

    def machine(self, data):
        d = tempfile.mkdtemp(prefix='heavy-queue-machine-')
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o755)
        path = os.path.join(d, 'machine.json')
        with open(path, 'w') as f:
            json.dump(data, f)
        os.chmod(path, 0o644)
        return path

    def test_settings_default_without_the_file_the_section_or_the_key(self):
        hq = self.hq()
        missing = os.path.join(tempfile.gettempdir(), 'heavy-queue-no-such-folder', 'machine.json')
        for path in (missing, self.machine({'version': 1, 'cleaner': {}}),
                     self.machine({'version': 1, 'heavy': {'sweep': {}}})):
            with self.subTest(path=path):
                self.assertEqual(hq.settings(path, os.getuid())['headroomMb'], 512)

    def test_settings_from_the_machine_file_and_bad_ones_are_refused(self):
        hq = self.hq()
        path = self.machine({'version': 1, 'heavy': {'queue': {'headroomMb': 256, 'passLimitSeconds': 300}}})
        cfg = hq.settings(path, os.getuid())
        self.assertEqual((cfg['headroomMb'], cfg['passLimitSeconds'], cfg['vmReserveMb']), (256, 300, 1024))
        for bad in ({'headroom': 1}, {'headroomMb': -1}, {'pollSeconds': 0}, {'headroomMb': True}):
            with self.subTest(bad=bad), self.assertRaises(hq.Refused):
                hq.settings(self.machine({'version': 1, 'heavy': {'queue': bad}}), os.getuid())

    def test_the_admit_command_must_be_a_program_with_an_absolute_path(self):
        hq = self.hq()
        ok = {'admitCommand': ['/bin/sh', '-c', 'exit 0', '{mb}'], 'admitTimeoutSeconds': 5, 'swapFreeMinMb': 0}
        cfg = hq.settings(self.machine({'version': 1, 'heavy': {'queue': ok}}), os.getuid())
        self.assertEqual(cfg['admitCommand'][0], '/bin/sh')
        plain = os.path.join(self.host.dir, 'not-a-program')
        with open(plain, 'w') as f:
            f.write('')
        for bad in (['sh', '-c', 'true'], [''], '/bin/true', [1], {'admitTimeoutSeconds': 0}):
            data = bad if isinstance(bad, dict) else {'admitCommand': bad}
            with self.subTest(bad=bad), self.assertRaises(hq.Refused):
                hq.settings(self.machine({'version': 1, 'heavy': {'queue': data}}), os.getuid())
        # a path that is not a program (yet, or any more) is not refused here: it stops the job that needs it
        cfg = hq.settings(self.machine({'version': 1, 'heavy': {'queue': {'admitCommand': [plain]}}}), os.getuid())
        self.assertEqual(cfg['admitCommand'], [plain])

    def test_the_sweep_accepts_the_queue_key(self):
        sweep = load_module('heavy_sweep', SWEEP)
        sweep.machine_file.fields({'sweep': {}, 'queue': {'headroomMb': 1}}, sweep.SECTION_SPEC, 'test')

    def test_kind_names(self):
        hq = self.hq()
        self.assertEqual(hq.check_kind('gradle-build.2'), 'gradle-build.2')
        for bad in ('', 'Gradle', 'a/b', 'x' * 41, '-x'):
            with self.subTest(bad=bad), self.assertRaises(hq.Refused):
                hq.check_kind(bad)


class CommandLine(unittest.TestCase):
    def setUp(self):
        self.heavy = load_module('heavy_bin', HEAVY)

    def test_job_options(self):
        jo = self.heavy.job_options
        self.assertEqual(jo(['ls']), (None, ['ls']))
        self.assertEqual(jo(['--kind', 'gradle', '--mb', '2048', '--', 'gw']),
                         ({'kind': 'gradle', 'mb': 2048, 'wait': 7200.0}, ['--', 'gw']))
        self.assertEqual(jo(['--kind=tsc', '--wait=60', 'tsc'])[0], {'kind': 'tsc', 'mb': 3072, 'wait': 60.0})
        for bad in (['--kind', 'x'], ['--mb', '10', 'ls'], ['--kind', 'x', '--kind', 'y', 'ls'],
                    ['--kind', 'x', '--mb', 'lots', 'ls'], ['--kind', 'x', '--mb', '1', 'ls'], ['--kind']):
            with self.subTest(bad=bad):
                self.assertEqual(jo(bad)[0], 'usage')

    def test_usage_errors_exit_2_before_anything_runs(self):
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, '--kind', 'x'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)
        self.assertIn('usage: heavy --kind', r.stderr)

    def test_queue_help(self):
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'queue', '--help'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn('heavy waits', r.stdout)


MUTATIONS = {
    'a ticket nobody holds is deleted': ('                else:                # nobody holds it: its process is gone\n'
                                         '                    _unlink(path)\n                    continue\n',
                                         '                else:\n                    pass\n',
                                         'test_a_crashed_job_frees_its_reservation'),
    'owed counts unused reservations': ('owed += max(0, t.mb * MB - used)', 'owed += 0',
                                        'test_admitted_jobs_hold_what_they_have_not_used_yet'),
    'the VM check': ('box_room = None if avail is None else avail - reserve - owed', 'box_room = None',
                     'test_the_machines_free_memory_limits_too'),
    'the pressure brake': ("if facts['full10'] > cfg['pressureFull10'] or facts['some60'] > cfg['pressureSome60']:",
                           'if False:', 'test_memory_pressure_admits_nobody'),
    'the swap floor': ("if cfg['swapFreeMinMb'] and swap_total", 'if False and swap_total',
                       'test_swap_under_the_floor_admits_nobody'),
    'earlier jobs keep their share': ('shares += need(t)       # its share', 'shares += 0  #',
                                      'test_first_come_first_served'),
    'the pass limit': ("if self.waited(t) >= cfg['passLimitSeconds'] and holding_helps(t, shares):", 'if False:',
                       'test_a_small_job_passes_a_big_one_until_the_pass_limit'),
    'holding only while it helps': ('            return need(t) <= room_after\n', '            return True\n',
                                    'test_a_job_that_cannot_fit_even_when_the_queue_empties_does_not_hold_it'),
    'oversized runs alone': ('return (not admitted and not shares and used <= headroom',
                             'return (used <= headroom',
                             'test_a_job_larger_than_the_budget_waits_for_the_others'),
    'oversized still needs free memory': ('and (box_room is None or need(t) <= box_room))', 'and True)',
                                          'test_a_job_larger_than_the_budget_still_needs_the_machines_free_memory'),
    'the admit command': ('ok, why = self.ask_hook(t.mb)', 'pass',
                          'test_the_admit_command_saying_no_keeps_the_job_waiting_in_its_place'),
    'a refused waiter holds no share': ("refused = t.data.get('hookNo') is True", 'refused = False',
                                        'test_a_waiter_the_admit_command_refuses_keeps_no_room_for_itself'),
    'a learned value can be admitted': ('if cap_mb is not None and mb > cap_mb:', 'if False:',
                                        'test_one_leaky_run_does_not_lock_its_kind_out'),
    'a missing admit command stops its job': ('        if not runnable(argv[0]):\n', '        if False:\n',
                                              'test_a_missing_admit_command_stops_the_job_that_needs_it_not_the_queue'),
    'the release needs no lock': ("    _unlink(os.path.join(queue.box.queue_dir, t.name))\n    close(t)\n",
                                  "    try:\n        with queue:\n            _unlink(os.path.join(queue.box.queue_dir, "
                                  "t.name))\n    except Exception:\n        pass\n    close(t)\n",
                                  'test_a_finished_job_deletes_its_ticket_while_the_lock_is_taken'),
    'the lock waits outlast the admit command': ("return LOCK_TIMEOUT + (self.cfg['admitTimeoutSeconds'] if",
                                                 "return LOCK_TIMEOUT + (0 if",
                                                 'test_peaks_and_verbs_wait_longer_than_one_admit_command'),
    'stop signals stay blocked once admitted': ('if not (admitted and keep_blocked):', 'if True:',
                                                'test_a_stop_just_after_the_admission_stays_pending_for_heavy'),
    'stop signals blocked in the lock': ('old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)',
                                         'old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, [])',
                                         'test_a_signal_while_the_lock_is_held_is_taken_after_it_is_given_back'),
    'the lock wait has an end': ('if self.monotonic() >= deadline:', 'if False:',
                                 'test_a_lock_held_by_a_stuck_process_ends_the_wait_on_time'),
    'learning never lowers the start value': ('if mb <= start_mb:', 'if mb == start_mb:',
                                              'test_learning_never_goes_below_the_start_value'),
    'the highest peak, not a middle one': ('top = max(runs)', 'top = sorted(runs)[len(runs) // 2]',
                                           'test_one_high_run_among_many_low_ones_sets_the_reservation'),
    'stale temporary files': ('        _unlink(os.path.join(folder, name))\n', '        pass\n',
                              'test_temporary_files_of_killed_writers_are_removed'),
    'cache is not used memory': ("max(current - max(droppable, 0), st.get('anon', 0)) + swap", 'current + swap',
                                 'test_used_counts_anon_and_swap_but_not_clean_cache'),
    'the wait limit': ('if waited >= wait_s:', 'if False:', 'test_gives_up_after_the_wait_and_leaves_no_ticket'),
    'garbled tickets count': ("return Ticket(name, {'v': None, 'kind': '?', 'mb': DEFAULT_MB, 'state': 'admitted',",
                              "return Ticket(name, {'v': None, 'kind': '?', 'mb': 1, 'state': 'waiting',",
                              'test_a_garbled_or_old_live_ticket_counts_as_admitted_at_3072'),
    'the queue folder is checked': ('or st.st_mode & 0o022:', 'or False:',
                                    'test_a_queue_folder_others_can_change_is_refused'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(QUEUE) as f:
            source = f.read()
        for rule, (text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                cls = type('Broken', (QueueTests,), {'mod': load_module('heavy_queue', QUEUE,
                                                                        source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')
                ok = unittest.TestResult()
                QueueTests(test).run(ok)
                self.assertTrue(ok.wasSuccessful(), f'{test} fails on the real code: {ok.failures or ok.errors}')


if __name__ == '__main__':
    unittest.main(verbosity=2)
