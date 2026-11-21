"""heavy's measured history and the capacity front (lib/heavy_measure.py, `heavy --measure`, `heavy capacity`).

  python3 -I -B fork-kit/heavy/tests/measure.test.py

No systemd, no root, nothing real: every test has its own temporary folder for the history (HEAVY_STATE_DIR), a
fake cgroup folder for the scope, and a fake capacity check (a small script that writes down how it was called).
The real history (~/.local/state/heavy), the machine's capacity check and heavy.slice are never touched; the
recording by a real heavy with real systemd is in heavy.test.py.

The state question, one test each: the first run with no history, a history that is missing, unreadable or
corrupt, two writers at once (separate processes), a writer killed half-way through a line, a job killed before
it records, the rotation bound, and a kinds file that is missing or wrong. Against an older kit
(HEAVY_TEST_KIT=<folder of an older fork-kit/heavy>) every test fails: that is the red check.
"""
import importlib.machinery, importlib.util, json, os, shutil, signal, stat, subprocess, sys, tempfile, time
import unittest

KIT = os.environ.get('HEAVY_TEST_KIT') or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MEASURE = os.path.join(KIT, 'lib', 'heavy_measure.py')
HEAVY = os.path.join(KIT, 'bin', 'heavy')
MB = 1 << 20


def load(name, path):
    loader = importlib.machinery.SourceFileLoader(name, path)
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def hm():
    return load('heavy_measure', MEASURE)


KINDS = {
    'version': 1, 'percentile': 90, 'marginPercent': 25, 'minRuns': 3, 'recentRuns': 10, 'roundMb': 128,
    'minMb': 256,
    'kinds': {'node-tests': {'fallbackMb': 1536}, 'gradle-build': {'fallbackMb': 3072, 'maxMb': 8192},
              'review': {'fallbackMb': 1024}},
}


def rec(kind, used=None, peak=None, end='exited'):
    return {'v': 1, 'at': '2026-11-21T00:00:00Z', 'kind': kind, 'end': end, 'seconds': 1.0, 'usedMb': used,
            'peakMb': peak}


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='heavy-measure-test-'))
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.state = os.path.join(self.dir, 'state')
        self.history = os.path.join(self.state, 'measured.jsonl')
        self.m = hm()
        self.kinds = self.write_kinds(KINDS)
        self.calls = os.path.join(self.dir, 'calls')
        self.check = os.path.join(self.dir, 'check')
        with open(self.check, 'w') as f:      # the fake capacity check: writes its arguments and env, exits $CHECK_EXIT
            f.write(f'#!/bin/sh\nprintf "%s|" "$@" >> "{self.calls}"; '
                    f'printf "front=%s kind=%s\\n" "$HEAVY_CAPACITY_FRONT" "$HEAVY_CAPACITY_KIND" >> "{self.calls}"\n'
                    f'echo "available=9999MB need=$1MB"\nexit "${{CHECK_EXIT:-0}}"\n')
        os.chmod(self.check, 0o755)

    def write_kinds(self, data, name='kinds.json'):
        path = os.path.join(self.dir, name)
        with open(path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def add(self, *records):
        for r in records:
            self.m.append(self.state, r)

    def capacity(self, *args, env=None, kinds=None):
        e = dict(os.environ, HEAVY_STATE_DIR=self.state)
        e.pop('HEAVY_CAPACITY_FRONT', None)
        e.update(env or {})
        return subprocess.run([sys.executable, '-I', '-B', HEAVY, 'capacity', '--kinds', kinds or self.kinds,
                               '--check', self.check, *args], capture_output=True, text=True, env=e, timeout=60)

    def calls_made(self):
        try:
            with open(self.calls) as f:
                return f.read().splitlines()
        except FileNotFoundError:
            return []


class History(Base):
    def test_first_run_has_no_history_and_every_kind_gets_its_fallback(self):
        self.assertFalse(os.path.exists(self.state))
        records, note = self.m.read_history(self.state)
        self.assertEqual((records, note), ([], None))
        found, _ = self.m.amounts(self.m.load_kinds(self.kinds), self.state)
        self.assertEqual({k: v['mb'] for k, v in found.items()},
                         {'node-tests': 1536, 'gradle-build': 3072, 'review': 1024})

    def test_append_makes_a_private_folder_and_file_and_reads_back(self):
        self.add(rec('node-tests', 700, 900))
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(self.history).st_mode), 0o600)
        records, note = self.m.read_history(self.state)
        self.assertIsNone(note)
        self.assertEqual([(r['kind'], r['usedMb'], r['peakMb']) for r in records], [('node-tests', 700, 900)])

    def test_an_unreadable_history_fails_closed_to_the_fallback_never_to_0(self):
        os.makedirs(self.history)             # a folder where the file should be: cannot be read, even as root
        records, note = self.m.read_history(self.state)
        self.assertIsNone(records)
        self.assertIn('cannot be read', note)
        mb, how = self.m.amount('node-tests', self.m.load_kinds(self.kinds), records, note)
        self.assertEqual(mb, 1536)
        self.assertIn('fallback', how)

    def test_a_history_that_is_a_link_is_not_followed(self):
        target = os.path.join(self.dir, 'elsewhere.jsonl')
        with open(target, 'w') as f:
            f.write('\n'.join(json.dumps(rec('node-tests', 100)) for _ in range(5)) + '\n')
        os.makedirs(self.state)
        os.symlink(target, self.history)
        records, note = self.m.read_history(self.state)
        self.assertIsNone(records)
        with self.assertRaises(OSError):
            self.add(rec('node-tests', 100))

    def test_a_corrupt_history_is_not_trusted(self):
        os.makedirs(self.state)
        with open(self.history, 'wb') as f:
            f.write(b'\x00\xffgarbage\n' * 5 + (json.dumps(rec('node-tests', 100)) + '\n').encode() * 5)
        records, note = self.m.read_history(self.state)
        self.assertIsNone(records)
        self.assertIn('not trusted', note)
        r = self.capacity('node-tests')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls_made()[0], '1536|front=1 kind=node-tests')

    def test_records_of_another_version_or_shape_are_not_counted(self):
        self.add(*[rec('node-tests', 500)] * 30)
        with open(self.history, 'a') as f:
            f.write(json.dumps(dict(rec('node-tests', 50000), v=2)) + '\n')       # a newer heavy: not counted
        records, note = self.m.read_history(self.state)
        self.assertEqual(len(records), 30)
        self.assertIn('1 line(s)', note)

    def test_a_writer_killed_half_way_costs_only_its_own_line(self):
        self.add(*[rec('node-tests', 500)] * 3)
        # a writer that dies in the middle of its line (kill -9 between two writes of a broken writer)
        code = (f'import os, signal\nfd = os.open({self.history!r}, os.O_WRONLY | os.O_APPEND)\n'
                f'os.write(fd, b\'{{"v":1,"kind":"node-tests","usedMb":9\')\nos.kill(os.getpid(), signal.SIGKILL)\n')
        p = subprocess.run([sys.executable, '-I', '-c', code], timeout=30)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        self.add(rec('node-tests', 600))
        records, note = self.m.read_history(self.state)
        self.assertEqual([r['usedMb'] for r in records], [500, 500, 500, 600])
        self.assertIn('1 line(s)', note)

    def test_two_writers_at_once_lose_no_record(self):
        n, each = 8, 150
        code = (f'import importlib.machinery, importlib.util, sys\n'
                f'l = importlib.machinery.SourceFileLoader("hm", {MEASURE!r})\n'
                f's = importlib.util.spec_from_loader("hm", l); m = importlib.util.module_from_spec(s); l.exec_module(m)\n'
                f'sys.stdin.read()\n'
                f'for i in range({each}):\n'
                f'    m.append({self.state!r}, {{"v": 1, "at": "x", "kind": "w" + sys.argv[1], "end": "exited", '
                f'"seconds": 1, "usedMb": i + 1, "peakMb": None}})\n')
        procs = [subprocess.Popen([sys.executable, '-I', '-c', code, str(k)], stdin=subprocess.PIPE)
                 for k in range(n)]
        for p in procs:                         # all start writing at the same moment
            p.stdin.close()
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        self.assertLess(os.path.getsize(self.history), self.m.MAX_BYTES)   # no rotation in this test
        records, note = self.m.read_history(self.state)
        self.assertIsNone(note)
        for k in range(n):
            self.assertEqual([r['usedMb'] for r in records if r['kind'] == f'w{k}'], list(range(1, each + 1)))

    def test_rotation_keeps_the_history_bounded_and_the_newest_records(self):
        self.m.MAX_BYTES = 4096
        for i in range(400):
            self.add(rec('node-tests', i + 1))
        self.assertLessEqual(os.path.getsize(self.history), 4096 + 200)
        self.assertLessEqual(os.path.getsize(self.history + '.1'), 4096 + 200)
        self.assertEqual(sorted(os.listdir(self.state)), ['measured.jsonl', 'measured.jsonl.1', 'measured.lock'])
        records, note = self.m.read_history(self.state)
        self.assertIsNone(note)
        used = [r['usedMb'] for r in records]
        self.assertEqual(used[-1], 400)
        self.assertEqual(used, sorted(used))       # oldest first, across the two files

    def test_a_rotation_held_by_another_writer_is_skipped_not_waited_for(self):
        import fcntl
        self.m.MAX_BYTES = 1024
        os.makedirs(self.state)
        fd = os.open(os.path.join(self.state, 'measured.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        start = time.monotonic()
        for i in range(30):
            self.add(rec('node-tests', i + 1))
        self.assertLess(time.monotonic() - start, 5)
        self.assertFalse(os.path.exists(self.history + '.1'))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.add(rec('node-tests', 31))
        self.assertTrue(os.path.exists(self.history + '.1'))


class Amount(Base):
    def cfg(self, **over):
        return dict(self.m.load_kinds(self.kinds), **over)

    def test_percentile_plus_margin_rounded_up(self):
        runs = [rec('node-tests', u) for u in (400, 420, 450, 500, 520, 530, 560, 600, 610, 900)]
        mb, how = self.m.amount('node-tests', self.cfg(), runs)
        self.assertEqual(mb, 768)                  # p90 of 10 = 610; x 1.25 = 762.5; up to 128 -> 768
        self.assertIn('p90 used 610 MB of 10 runs + 25%', how)

    def test_a_measured_kind_can_ask_for_less_than_its_fallback(self):
        mb, _ = self.m.amount('gradle-build', self.cfg(), [rec('gradle-build', 1100)] * 5)
        self.assertEqual(mb, 1408)
        self.assertLess(mb, 3072)

    def test_too_few_runs_use_the_fallback(self):
        mb, how = self.m.amount('node-tests', self.cfg(), [rec('node-tests', 300)] * 2)
        self.assertEqual(mb, 1536)
        self.assertIn('2 measured runs, 3 needed', how)

    def test_stopped_runs_and_other_kinds_do_not_count(self):
        runs = [rec('node-tests', 300)] * 2 + [rec('node-tests', 50, end='stopped')] * 5 + [rec('review', 300)] * 5
        mb, _ = self.m.amount('node-tests', self.cfg(), runs)
        self.assertEqual(mb, 1536)

    def test_only_the_recent_runs_count(self):
        runs = [rec('node-tests', 5000)] * 20 + [rec('node-tests', 400)] * 10
        mb, _ = self.m.amount('node-tests', self.cfg(), runs)
        self.assertEqual(mb, 512)

    def test_never_below_min_mb_and_capped_at_max_mb(self):
        self.assertEqual(self.m.amount('node-tests', self.cfg(), [rec('node-tests', 1)] * 5)[0], 256)
        mb, how = self.m.amount('node-tests', self.cfg(), [rec('node-tests', 99999)] * 5)
        self.assertEqual(mb, 3072)                 # default cap: 2 x fallbackMb
        self.assertIn('capped', how)
        self.assertEqual(self.m.amount('gradle-build', self.cfg(), [rec('gradle-build', 99999)] * 5)[0], 8192)

    def test_measure_peak_uses_the_kernel_peak(self):
        runs = [rec('node-tests', 300, 2000)] * 5
        self.assertEqual(self.m.amount('node-tests', self.cfg(measure='peak'), runs)[0], 2560)
        self.assertEqual(self.m.amount('node-tests', self.cfg(), runs)[0], 384)

    def test_kinds_file_rules(self):
        bad = [
            ('missing', None),
            ('not json', '{'),
            ('unknown top key', dict(KINDS, persentile=90)),
            ('unknown kind key', dict(KINDS, kinds={'x': {'fallbackMb': 512, 'fallback': 1}})),
            ('no fallback', dict(KINDS, kinds={'x': {}})),
            ('fallback 0', dict(KINDS, kinds={'x': {'fallbackMb': 0}})),
            ('kind of digits', dict(KINDS, kinds={'1536': {'fallbackMb': 512}})),
            ('max below fallback', dict(KINDS, kinds={'x': {'fallbackMb': 512, 'maxMb': 256}})),
            ('newer version', dict(KINDS, version=2)),
            ('bool for int', dict(KINDS, minRuns=True)),
            ('measure', dict(KINDS, measure='rss')),
            ('no kinds', dict(KINDS, kinds={})),
        ]
        for name, data in bad:
            with self.subTest(name):
                path = os.path.join(self.dir, 'nope.json') if data is None else self.write_kinds(data, 'bad.json')
                with self.assertRaises(self.m.Refused):
                    self.m.load_kinds(path)

    def test_kinds_inside_a_project_config(self):
        path = self.write_kinds({'repository': 'x/y', 'capacity': KINDS}, 'project.json')
        self.assertEqual(sorted(self.m.load_kinds(path)['kinds']), ['gradle-build', 'node-tests', 'review'])


class Capacity(Base):
    def test_a_plain_number_and_no_argument_are_passed_on_unchanged(self):
        self.write_kinds('{ broken')            # a whole number never needs the kinds file
        for args in (['1536'], [], ['3072', 'extra'], ['0']):
            r = self.capacity(*args)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls_made(), ['1536|front=1 kind=', '|front=1 kind=', '3072|extra|front=1 kind=',
                                             '0|front=1 kind='])

    def test_a_kind_becomes_its_measured_amount(self):
        self.add(*[rec('node-tests', 700)] * 5)
        r = self.capacity('node-tests')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout, 'available=9999MB need=896MB\n')      # stdout is the check's alone
        self.assertEqual(self.calls_made(), ['896|front=1 kind=node-tests'])

    def test_a_kind_without_history_gets_its_fallback(self):
        r = self.capacity('--', 'gradle-build')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.calls_made(), ['3072|front=1 kind=gradle-build'])

    def test_the_checks_exit_code_is_heavys(self):
        r = self.capacity('review', env={'CHECK_EXIT': '1'})
        self.assertEqual(r.returncode, 1)
        self.assertEqual(self.calls_made(), ['1024|front=1 kind=review'])

    def test_refusals_never_run_the_check(self):
        cases = [
            (['node-test'], {}, None),                       # a typo of a kind
            (['Node-Tests'], {}, None),                      # neither a number nor a kind
            (['node-tests'], {}, self.write_kinds('{', 'broken.json')),
            (['node-tests'], {'HEAVY_CAPACITY_FRONT': '1'}, None),     # the check calls the front again
            (['node-tests'], {'HEAVY_STATE_DIR': 'relative'}, None),
        ]
        for args, env, kinds in cases:
            with self.subTest(args=args, env=env, kinds=kinds):
                r = self.capacity(*args, env=env, kinds=kinds)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertIn('heavy: capacity:', r.stderr)
                self.assertEqual(r.stdout, '')
        self.assertEqual(self.calls_made(), [])

    def test_a_check_that_is_not_an_absolute_program_is_refused(self):
        for check in ('check', os.path.join(self.dir, 'missing'), self.kinds):
            with self.subTest(check=check):
                r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'capacity', '--kinds', self.kinds, '--check',
                                    check, '1536'], capture_output=True, text=True, timeout=60,
                                   env=dict(os.environ, HEAVY_STATE_DIR=self.state))
                self.assertEqual(r.returncode, 2)

    def test_show_lists_each_kind_and_how(self):
        self.add(*[rec('node-tests', 700)] * 5)
        e = dict(os.environ, HEAVY_STATE_DIR=self.state)
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'capacity', '--kinds', self.kinds, '--show', '--json'],
                           capture_output=True, text=True, env=e, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        kinds = json.loads(r.stdout)['kinds']
        self.assertEqual({k: v['mb'] for k, v in kinds.items()},
                         {'node-tests': 896, 'gradle-build': 3072, 'review': 1024})
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'capacity', '--kinds', self.kinds, '--show',
                            'node-tests'], capture_output=True, text=True, env=e, timeout=60)
        self.assertRegex(r.stdout, r'^node-tests +896 MB  p90 used 700 MB of 5 runs')
        self.assertEqual(self.calls_made(), [])

    def test_help(self):
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'capacity', '--help'], capture_output=True, text=True,
                           timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn('usage: heavy capacity', r.stdout)


class FakeBox:
    def __init__(self, root):
        self.root, self.refreshed = root, 0

    def folder(self, scope=None):
        return os.path.join(self.root, scope) if scope else self.root

    def refresh(self):
        self.refreshed += 1


class FakeInner:
    interval = 3

    def __init__(self):
        self.calls = []

    def sample(self):
        self.calls.append('sample')

    def record(self, wait=None):
        self.calls.append('record')

    def record_soon(self):
        self.calls.append('record_soon')


class Recording(Base):
    """The Measurer heavy runs while a job runs, on a fake scope folder."""

    def scope(self, used_mb, peak_mb):
        folder = os.path.join(self.dir, 'cg', 'heavy-job-1.scope')
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, 'memory.peak'), 'w') as f:
            f.write(f'{peak_mb * MB}\n')
        with open(os.path.join(folder, 'used'), 'w') as f:
            f.write(str(used_mb * MB))
        return folder

    def measurer(self, inner=None, kind='node-tests'):
        def used_of(folder):
            try:
                with open(os.path.join(folder, 'used')) as f:
                    return int(f.read())
            except OSError:
                return None
        return self.m.Measurer(FakeBox(os.path.join(self.dir, 'cg')), 'heavy-job-1.scope', kind, self.state,
                               used_of, inner=inner, out=lambda msg: self.notes.append(msg))

    def setUp(self):
        super().setUp()
        self.notes = []

    def test_a_run_records_its_highest_used_and_the_kernels_peak(self):
        m = self.measurer()
        self.assertEqual(m.interval, self.m.FIRST_LOOK)       # looks often until the scope is seen
        m.sample()                                             # no scope yet
        self.scope(100, 150)
        m.sample()
        self.assertEqual(m.interval, self.m.SAMPLE_SECONDS)
        self.scope(400, 450)
        m.sample()
        self.scope(200, 450)
        m.sample()
        m.record()
        m.record()                                             # once only
        records, _ = self.m.read_history(self.state)
        self.assertEqual([(r['kind'], r['usedMb'], r['peakMb'], r['end']) for r in records],
                         [('node-tests', 400, 450, 'exited')])

    def test_the_peak_file_opened_while_the_scope_lived_is_read_after_it_is_gone(self):
        m = self.measurer()
        folder = self.scope(100, 300)
        m.sample()
        with open(os.path.join(folder, 'memory.peak'), 'w') as f:     # grew after the last sample
            f.write(f'{900 * MB}\n')
        shutil.rmtree(folder)                                  # the scope is gone when heavy records
        m.sample()
        m.record()
        records, _ = self.m.read_history(self.state)
        self.assertEqual([(r['usedMb'], r['peakMb']) for r in records], [(100, 900)])

    def test_a_stopped_run_is_recorded_as_stopped_and_not_counted(self):
        m = self.measurer()
        self.scope(5000, 5000)
        m.sample()
        m.record_soon()
        records, _ = self.m.read_history(self.state)
        self.assertEqual([r['end'] for r in records], ['stopped'])
        self.assertEqual(self.m.amount('node-tests', self.m.load_kinds(self.kinds), records * 9)[0], 1536)

    def test_a_job_never_seen_records_nothing(self):
        m = self.measurer()
        m.sample()
        m.record()
        self.assertFalse(os.path.exists(self.history))

    def test_a_job_killed_before_it_records_leaves_the_history_as_it_was(self):
        self.add(*[rec('node-tests', 500)] * 5)
        before = open(self.history, 'rb').read()
        folder = self.scope(800, 900)
        code = (f'import importlib.machinery, importlib.util, os, signal\n'
                f'l = importlib.machinery.SourceFileLoader("hm", {MEASURE!r})\n'
                f's = importlib.util.spec_from_loader("hm", l); m = importlib.util.module_from_spec(s); l.exec_module(m)\n'
                f'class B:\n    def folder(self, s=None): return {folder!r}\n    def refresh(self): pass\n'
                f'x = m.Measurer(B(), "s", "node-tests", {self.state!r}, lambda f: 5 << 30)\n'
                f'x.sample()\nos.kill(os.getpid(), signal.SIGKILL)\n')
        p = subprocess.run([sys.executable, '-I', '-c', code], timeout=30)
        self.assertEqual(p.returncode, -signal.SIGKILL)
        self.assertEqual(open(self.history, 'rb').read(), before)
        self.assertEqual(self.m.amount('node-tests', self.m.load_kinds(self.kinds),
                                       self.m.read_history(self.state)[0])[0], 640)

    def test_a_failed_write_is_a_note_not_a_failed_job(self):
        os.makedirs(self.history)               # cannot append
        m = self.measurer()
        self.scope(100, 100)
        m.sample()
        m.record()
        self.assertEqual(len(self.notes), 1)
        self.assertIn('could not record the memory of node-tests', self.notes[0])

    def test_a_queued_job_still_feeds_the_queue(self):
        inner = FakeInner()
        m = self.measurer(inner)
        self.scope(100, 100)
        m.sample()
        self.assertEqual(m.interval, 3)
        m.record()
        self.assertEqual(inner.calls, ['sample', 'record'])
        inner2 = FakeInner()
        m2 = self.measurer(inner2)
        m2.record_soon()
        self.assertEqual(inner2.calls, ['record_soon'])

    def test_heavy_takes_measure_alone_or_with_kind(self):
        heavy = load('heavy_bin', HEAVY)
        self.assertEqual(heavy.job_options(['--measure', 'node-tests', 'npm', 'test']),
                         ({'kind': None, 'mb': 3072, 'wait': 7200.0, 'measure': 'node-tests'}, ['npm', 'test']))
        self.assertEqual(heavy.job_options(['--kind', 'gradle-build', '--measure=gb', '--', 'x'])[0]['measure'], 'gb')
        self.assertEqual(heavy.job_options(['--measure', 'k', '--mb', '512', 'x'])[0], 'usage')    # --mb needs --kind
        self.assertEqual(heavy.job_options(['--measure', 'k'])[0], 'usage')
        self.assertEqual(heavy.job_options(['npm', 'test']), (None, ['npm', 'test']))


if __name__ == '__main__':
    unittest.main(verbosity=2)
