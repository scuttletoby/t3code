#!/usr/bin/env python3
"""Tests of fork-ci-pressure.py (a job's memory pressure numbers), with a fake /proc and cgroup tree, and of its
steps in fork-tests.yml. Run: python3 <this>."""
import io, json, os, re, shutil, subprocess, sys, tempfile, unittest
import importlib.util

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, 'fork-ci-pressure.py')
_spec = importlib.util.spec_from_file_location('fork_ci_pressure', SCRIPT)
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

# The jobs that run app builds and tests on a fleet or GitHub-hosted runner (each routable job of fork-fleet.json).
with open(os.path.join(ROOT, '.github/fork-fleet.json'), encoding='utf-8') as f:
    APP_JOBS = sorted({j['job'] for j in json.load(f)['jobs']})
GIB = 1024 ** 3


def psi_text(some_total, full_total, full_avg10=0.0, full_avg300=0.0):
    return (f'some avg10=1.00 avg60=0.50 avg300=0.20 total={some_total}\n'
            f'full avg10={full_avg10:.2f} avg60=0.00 avg300={full_avg300:.2f} total={full_total}\n')


class Machine:
    """A fake /proc and /sys/fs/cgroup: the job in /fleet.slice/job.service, the slice limited to 8 GiB."""

    def __init__(self, d):
        self.d, self.proc, self.root = d, os.path.join(d, 'proc'), os.path.join(d, 'cgroup')
        self.write('proc/self/cgroup', '0::/fleet.slice/job.service\n')
        self.write('proc/sys/kernel/random/boot_id', 'boot-1\n')
        self.write('proc/meminfo', 'MemTotal: 16000000 kB\nMemAvailable: 8192000 kB\n'
                                   'SwapTotal: 2097152 kB\nSwapFree: 1048576 kB\n')
        self.write('proc/vmstat', 'nr_free_pages 1\noom_kill 3\n')
        self.write('proc/pressure/memory', psi_text(0, 0))
        self.box('fleet.slice', high='max', top=str(8 * GIB), events=(5, 0, 0, 0), psi=psi_text(0, 0))
        self.box('fleet.slice/job.service', high='max', top='max', events=(0, 0, 0, 0), psi=psi_text(0, 0))
        self.write('cgroup/fleet.slice/job.service/memory.current', str(3 * GIB))

    def write(self, rel, text):
        path = os.path.join(self.d, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)

    def box(self, rel, high=None, top=None, events=None, psi=None):
        base = f'cgroup/{rel}'
        if high is not None:
            self.write(f'{base}/memory.high', high + '\n')
        if top is not None:
            self.write(f'{base}/memory.max', top + '\n')
        if events is not None:
            self.write(f'{base}/memory.events', ''.join(f'{k} {v}\n' for k, v in zip(probe.EVENTS, events)))
        if psi is not None:
            self.write(f'{base}/memory.pressure', psi)


def run(m, verb, temp, status=None, t=1000.0):
    out = io.StringIO()
    env = {'RUNNER_TEMP': temp}
    if status:
        env['JOB_STATUS'] = status
    code = probe.main(['p', verb], env=env, out=out, proc=m.proc, root=m.root, now=lambda: t)
    return code, out.getvalue()


def the_note(out):
    lines = [l for l in out.splitlines() if l.startswith('::notice ')]
    if not lines:
        return None
    assert len(lines) == 1, lines
    data = lines[0].split('::', 2)[2].replace('%0A', '\n').replace('%0D', '\r').replace('%25', '%')
    assert data.startswith(probe.NOTE_HEAD), data
    return json.loads(data[len(probe.NOTE_HEAD):])


class Probe(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d)
        self.m = Machine(self.d)
        self.temp = os.path.join(self.d, 'temp')
        os.makedirs(self.temp)

    def stall(self):
        """600 s later: the machine stalled 60 s (10%), the slice OOM-killed twice and hit memory.max."""
        self.m.write('proc/pressure/memory', psi_text(120_000_000, 60_000_000, full_avg10=30.0, full_avg300=12.5))
        self.m.write('proc/vmstat', 'nr_free_pages 1\noom_kill 5\n')
        self.m.box('fleet.slice', events=(9, 4, 2, 2), psi=psi_text(0, 90_000_000))

    def test_a_failed_job_prints_its_numbers_and_one_note(self):
        self.assertEqual(run(self.m, 'start', self.temp)[0], 0)
        self.stall()
        code, out = run(self.m, 'end', self.temp, 'failure', t=1600.0)
        self.assertEqual(code, 0)
        self.assertIn('fork-ci-pressure: job failure; 600 s since start; memory stall full 10.0% some 20.0%', out)
        self.assertIn('full avg10/60/300 30.0/0.0/12.5%', out)
        self.assertIn('available 8000 MB, swap used 1024 MB, OOM kills 2', out)
        self.assertIn('cgroup /fleet.slice: high None MB max 8192 MB', out)
        n = the_note(out)
        self.assertEqual((n['status'], n['seconds']), ('failure', 600))
        self.assertEqual((n['host']['full_share'], n['host']['full_avg300'], n['host']['oom_kill']), (10.0, 12.5, 2))
        own, slice_ = n['cgroups']
        self.assertEqual(own['path'], '/fleet.slice/job.service')
        self.assertEqual(own['current_mb'], 3072)
        self.assertEqual((slice_['path'], slice_['max_mb'], slice_['high'], slice_['max'], slice_['oom_kill'],
                          slice_['full_share']), ('/fleet.slice', 8192, 4, 4, 2, 15.0))
        self.assertNotIn('/', [c['path'] for c in n['cgroups']], 'the root has no limit: not listed')

    def test_a_passing_job_prints_its_numbers_but_no_note(self):
        run(self.m, 'start', self.temp)
        code, out = run(self.m, 'end', self.temp, 'success', t=1600.0)
        self.assertEqual(code, 0)
        self.assertIn('fork-ci-pressure: job success', out)
        self.assertIsNone(the_note(out))

    def test_a_cancelled_job_and_an_unknown_status_leave_a_note(self):
        run(self.m, 'start', self.temp)
        for status in ('cancelled', None):
            n = the_note(run(self.m, 'end', self.temp, status, t=1600.0)[1])
            self.assertEqual(n['status'], status or 'unknown')

    def test_no_start_sample_makes_every_since_start_number_null(self):
        code, out = run(self.m, 'end', self.temp, 'failure', t=1600.0)   # the start step never ran
        self.assertEqual(code, 0)
        self.assertIn('no start sample', out)
        n = the_note(out)
        self.assertIsNone(n['seconds'])
        self.assertEqual([n['host'][k] for k in ('full_share', 'some_share', 'oom_kill')], [None, None, None])
        self.assertTrue(all(c['oom_kill'] is None and c['full_share'] is None for c in n['cgroups']))
        self.assertEqual(n['cgroups'][1]['max_mb'], 8192, 'what is read at the end is still there')

    def test_a_garbled_or_foreign_start_sample_counts_as_none(self):
        for text in ('{"t": ', '[1]', '{"t": "soon"}', json.dumps({'t': 500.0, 'boot': 'boot-0'})):
            with self.subTest(text):
                with open(os.path.join(self.temp, probe.SAMPLE), 'w') as f:
                    f.write(text)
                code, out = run(self.m, 'end', self.temp, 'failure', t=1600.0)
                self.assertEqual(code, 0)
                self.assertIsNone(the_note(out)['host']['full_share'], out)

    def test_a_reboot_between_start_and_end_counts_nothing_since_start(self):
        run(self.m, 'start', self.temp)
        self.stall()
        self.m.write('proc/sys/kernel/random/boot_id', 'boot-2\n')
        n = the_note(run(self.m, 'end', self.temp, 'failure', t=1600.0)[1])
        self.assertIsNone(n['host']['oom_kill'])

    def test_counters_that_went_back_are_null_not_negative(self):
        run(self.m, 'start', self.temp)
        self.m.write('proc/vmstat', 'oom_kill 1\n')
        n = the_note(run(self.m, 'end', self.temp, 'failure', t=1600.0)[1])
        self.assertIsNone(n['host']['oom_kill'])

    def test_nothing_readable_still_ends_well(self):
        shutil.rmtree(self.m.root)
        for rel in ('pressure', 'meminfo', 'vmstat', 'self'):
            path = os.path.join(self.m.proc, rel)
            (shutil.rmtree if os.path.isdir(path) else os.unlink)(path)
        self.assertEqual(run(self.m, 'start', self.temp)[0], 0)
        code, out = run(self.m, 'end', self.temp, 'failure', t=1600.0)
        self.assertEqual(code, 0)
        n = the_note(out)
        self.assertEqual(n['cgroups'], [])
        self.assertIsNone(n['host']['full_avg10'])

    def test_a_cgroup_path_with_odd_characters_stays_one_annotation(self):
        self.m.write('proc/self/cgroup', '0::/odd%25\\x2dslice\n')
        self.m.box('odd%25\\x2dslice', high=str(GIB), events=(1, 0, 0, 0))
        run(self.m, 'start', self.temp)
        code, out = run(self.m, 'end', self.temp, 'failure', t=1600.0)
        self.assertEqual(the_note(out)['cgroups'][0]['path'], '/odd%25\\x2dslice')

    def test_it_never_fails_the_job(self):
        out = io.StringIO()
        self.assertEqual(probe.main(['p', 'start'], env={}, out=out), 0)
        self.assertIn('::warning::', out.getvalue())
        self.assertEqual(probe.main(['p', 'nonsense'], env={'RUNNER_TEMP': self.temp}, out=io.StringIO()), 0)
        os.chmod(self.temp, 0o500)   # the sample cannot be written
        self.addCleanup(os.chmod, self.temp, 0o700)
        if os.geteuid() != 0:
            code, text = run(self.m, 'start', self.temp)
            self.assertEqual(code, 0)
            self.assertIn('::warning::fork-ci-pressure: start', text)
        r = subprocess.run([sys.executable, '-I', SCRIPT, 'end'], capture_output=True, text=True,
                           env={'PATH': os.environ.get('PATH', ''), 'RUNNER_TEMP': self.temp, 'JOB_STATUS': 'failure'})
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('fork-ci-pressure: job failure', r.stdout)


def step_run(verb):
    """The probe step's command: a hang (a stalled machine) is stopped after 40 s with a warning, and the job goes on."""
    tail = 'no numbers; the job goes on' if verb == 'start' else 'no numbers'
    return (f'timeout -k 5 40 python3 -I .github/scripts/fork-ci-pressure.py {verb} || '
            f'echo "::warning::fork-ci-pressure: {verb} stopped (exit $?), {tail}"')


class Steps(unittest.TestCase):
    """Each app job of fork-tests.yml takes a sample right after its checkout and prints the numbers last."""

    def run_step(self, verb, python3, limit='40'):
        """The step's own text in GitHub's shell (bash -e), with a stand-in python3 and its time limit made short."""
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        with open(os.path.join(d, 'python3'), 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\n' + python3 + '\n')
        os.chmod(os.path.join(d, 'python3'), 0o755)
        text = step_run(verb)
        self.assertEqual(text.count(' 5 40 '), 1)
        text = text.replace(' 5 40 ', f' 1 {limit} ')
        return subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', text], capture_output=True,
                              text=True, timeout=30, env={'PATH': d + os.pathsep + os.environ.get('PATH', '')})

    def test_a_hung_probe_is_stopped_and_the_job_goes_on(self):
        # A stalled machine can hang the probe itself: without its own limit the step's 1-minute timeout fails the
        # step, and every later step (the tests) is skipped. Now the step ends well after the limit, with a warning.
        for verb in ('start', 'end'):
            with self.subTest(verb):
                r = self.run_step(verb, 'sleep 25', limit='1')
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f'::warning::fork-ci-pressure: {verb} stopped (exit 124)', r.stdout)

    def test_a_probe_that_ignores_the_stop_signal_is_killed(self):
        r = self.run_step('start', "trap '' TERM; sleep 25", limit='1')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('::warning::fork-ci-pressure: start stopped (exit 137)', r.stdout)

    def test_a_probe_that_ends_in_time_adds_no_warning(self):
        r = self.run_step('start', 'echo "fork-ci-pressure: start sample saved"')
        self.assertEqual((r.returncode, r.stdout), (0, 'fork-ci-pressure: start sample saved\n'), r.stderr)

    def test_every_app_job_has_the_start_and_end_steps(self):
        import yaml
        with open(os.path.join(ROOT, '.github/workflows/fork-tests.yml'), encoding='utf-8') as f:
            wf = yaml.safe_load(f)
        self.assertTrue(APP_JOBS)
        for name in APP_JOBS:
            with self.subTest(name):
                steps = wf['jobs'][name]['steps']
                self.assertEqual(steps[0]['name'], 'Checkout')
                self.assertEqual(steps[1]['run'], step_run('start'))
                self.assertNotIn('if', steps[1])
                end = steps[-1]
                self.assertEqual(end['run'], step_run('end'))
                self.assertTrue(re.fullmatch(r"\$\{\{ always\(\) && hashFiles\('\.github/scripts/fork-ci-pressure"
                                             r"\.py'\) != '' \}\}", end['if']), end['if'])
                self.assertEqual(end['env'], {'JOB_STATUS': '${{ job.status }}'})
                self.assertEqual(sum('fork-ci-pressure' in str(s.get('run')) for s in steps), 2)
                for probe in (steps[1], end):
                    # A hung probe never holds the job up to its own limit. No continue-on-error: nothing in
                    # fork-tests.yml may fail quietly (fork-contracts.test.py); the probe itself never fails.
                    self.assertIn(probe.get('timeout-minutes'), (1, 2), probe['name'])
                    self.assertNotIn('continue-on-error', probe)
                with open(os.path.join(ROOT, '.github/fork-ci-rerun.json'), encoding='utf-8') as f:
                    self.assertEqual(end['name'], json.load(f)['probe_end_step'])   # the re-run reads its record

    def test_the_probe_is_not_fork_owned_app_jobs_read_it(self):
        with open(os.path.join(ROOT, '.github/fork-owned-paths.txt'), encoding='utf-8') as f:
            self.assertIn('!.github/scripts/fork-ci-pressure.py', f.read().splitlines())


if __name__ == '__main__':
    unittest.main()
