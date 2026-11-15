"""heavy sweep on a fake host: a fake /proc, fake systemctl and docker (tests/fake_tools.py), a temporary machine
file, a fixed clock and recorded signals. No root, no systemd, no Docker; nothing on this machine is swept.

  python3 -I -B fork-kit/heavy/tests/sweep.test.py

RedCheck breaks each stop or report rule in a copy of lib/heavy_sweep.py and shows the test for that rule fails.
One test sends real signals, to a child process this test starts.
"""
import contextlib, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, signal, subprocess, sys
import tempfile, time, types, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SWEEP = os.path.join(KIT, 'lib', 'heavy_sweep.py')
FAKE_TOOLS = os.path.join(KIT, 'tests', 'fake_tools.py')
HEAVY = os.path.join(KIT, 'bin', 'heavy')
UID = os.getuid()
CLK = os.sysconf('SC_CLK_TCK')
BTIME = 1_700_000_000
NOW = BTIME + 500_000
MANAGER, SELF, SELF_PARENT, SERVICE = 500, 999, 998, 600
CG = f'/user.slice/user-{UID}.slice/user@{UID}.service'
LABEL = 'fork-kit.heavy.owner'


def load_sweep(source=None):
    if source is None:
        loader = importlib.machinery.SourceFileLoader('heavy_sweep', SWEEP)
        spec = importlib.util.spec_from_loader('heavy_sweep', loader)
        mod = importlib.util.module_from_spec(spec)
        loader.exec_module(mod)
        return mod
    mod = types.ModuleType('heavy_sweep')
    mod.__file__ = SWEEP
    exec(compile(source, SWEEP, 'exec'), mod.__dict__)
    return mod


REAL = load_sweep()


class Host:
    """A fake /proc and the state file the fake tools read."""

    def __init__(self, root):
        self.root = root
        self.proc = os.path.join(root, 'proc')
        os.makedirs(self.proc)
        with open(os.path.join(self.proc, 'stat'), 'w') as f:
            f.write(f'cpu 1 2 3\nbtime {BTIME}\n')
        self.state_path = os.path.join(root, 'state.json')
        self.state = {'proc': self.proc, 'units': {}, 'containers': []}
        self.save()
        self.ticks_later = {}

    def save(self):
        with open(self.state_path, 'w') as f:
            json.dump(self.state, f)

    def load(self):
        with open(self.state_path) as f:
            self.state = json.load(f)
        return self.state

    def calls(self, tool=None, verb=None):
        return [c for c in self.load().get('calls', []) if (tool is None or c[0] == tool)
                and (verb is None or verb in c)]

    def add(self, pid, ppid, comm, cmd, unit_path, age, uid=UID, cpu=0, state='S'):
        """cpu: percent used during the sweep's one-second sample."""
        d = os.path.join(self.proc, str(pid))
        os.makedirs(d, exist_ok=True)
        start = int((NOW - BTIME - age) * CLK)
        self.write_stat(pid, comm, state, ppid, 1000, start)
        with open(os.path.join(d, 'status'), 'w') as f:
            f.write(f'Name:\t{comm}\nUid:\t{uid}\t{uid}\t{uid}\t{uid}\n')
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in cmd.split()) + b'\0')
        with open(os.path.join(d, 'cgroup'), 'w') as f:
            f.write(f'0::{unit_path}\n')
        if cpu:
            self.ticks_later[pid] = 1000 + int(cpu / 100 * CLK)
        return start

    def write_stat(self, pid, comm, state, ppid, ticks, start):
        rest = [state, ppid, pid, pid, 0, -1, 0, 0, 0, 0, 0, ticks, 0, 0, 0, 20, 0, 1, 0, start] + [0] * 30
        with open(os.path.join(self.proc, str(pid), 'stat'), 'w') as f:
            f.write(f'{pid} ({comm}) ' + ' '.join(map(str, rest)) + '\n')

    def stat(self, pid):
        with open(os.path.join(self.proc, str(pid), 'stat')) as f:
            text = f.read()
        r = text.rfind(')')
        return text[text.find('(') + 1:r], text[r + 2:].split()

    def set_start(self, pid, start):
        comm, rest = self.stat(pid)
        self.write_stat(pid, comm, rest[0], rest[1], rest[11], start)

    def apply_cpu(self):
        for pid, ticks in self.ticks_later.items():
            if self.exists(pid):
                comm, rest = self.stat(pid)
                self.write_stat(pid, comm, rest[0], rest[1], ticks, rest[19])

    def exists(self, pid):
        return os.path.isdir(os.path.join(self.proc, str(pid)))

    def kill(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)

    def container(self, cid, name, age, running=True, owner=None, image='mariadb:11', **extra):
        created = time.strftime('%Y-%m-%dT%H:%M:%S', time.gmtime(NOW - age)) + '.123456789Z'
        c = {'Id': (cid * 64)[:64], 'Name': '/' + name, 'Created': created, 'State': {'Running': running},
             'Config': {'Labels': {LABEL: owner} if owner else {'other': 'x'}, 'Image': image}}
        c.update(extra)
        self.state['containers'].append(c)
        self.save()
        return c['Id'][:12]


class SweepTests(unittest.TestCase):
    mod = REAL

    def setUp(self):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='heavy-sweep-test-'))
        os.chmod(self.root, 0o755)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.host = Host(self.root)
        self.report = os.path.join(self.root, 'report')
        self.cfg = {'stop': True, 'minAgeSeconds': 600, 'longLivedSeconds': 7200, 'busyCpuPercent': 20,
                    'report': self.report, 'knownUnits': ['known.service'], 'knownContainers': ['dev-db'],
                    'testPatterns': ['pytest', r'\.test\.sh'], 'ignorePatterns': ['^tmux']}
        self.machine = os.path.join(self.root, 'machine.json')
        self.write_machine()
        h = self.host
        h.add(1, 0, 'systemd', '/sbin/init', '/init.scope', 400000, uid=0)
        h.add(MANAGER, 1, 'systemd', '/lib/systemd/systemd --user', f'{CG}/init.scope', 400000)
        h.add(SERVICE, MANAGER, 'node', 'node app.js', f'{CG}/app.slice/t3code.service', 300000, cpu=50)
        h.add(SELF_PARENT, SERVICE, 'sh', 'sh -c heavy sweep', f'{CG}/app.slice/t3code.service', 100)
        h.add(SELF, SELF_PARENT, 'python3', 'python3 heavy sweep', f'{CG}/app.slice/t3code.service', 1)
        h.state['units'] = {'t3code.service': {'MainPID': SERVICE, 'ActiveState': 'active'}}
        h.save()
        self.sent = []
        self.stubborn = set()
        self.before_open = None
        self.after_send = None

    def write_machine(self, data=None):
        data = data if data is not None else {'version': 1, 'heavy': {'sweep': self.cfg}}
        with open(self.machine, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.machine, 0o644)

    def shim(self, tool):
        path = os.path.join(self.root, tool)
        with open(path, 'w') as f:
            f.write(f'#!/bin/sh\nexec "{sys.executable}" -I -B "{FAKE_TOOLS}" "{self.host.state_path}" {tool} "$@"\n')
        os.chmod(path, 0o755)
        return path

    def sweep(self, docker=True):
        s = self.mod.Sweep()
        s.proc, s.machine_path, s.owner_uid = self.host.proc, self.machine, UID
        s.systemctl, s.docker = self.shim('systemctl'), (self.shim('docker') if docker else '')
        s.uid, s.pid, s.clk, s.stop_grace = UID, SELF, CLK, 0.3
        s._now = lambda: NOW
        sampled = []

        def sleep(_seconds):
            if not sampled:
                sampled.append(1)
                self.host.apply_cpu()
            else:
                time.sleep(0.01)
        s._sleep = sleep

        def pidfd_open(pid):
            if self.before_open:
                self.before_open(pid)
            if not self.host.exists(pid):
                raise ProcessLookupError(pid)
            return pid

        def pidfd_send(fd, sig):
            self.sent.append((fd, signal.Signals(sig).name))
            if not (sig == signal.SIGTERM and fd in self.stubborn):
                self.host.kill(fd)
            if self.after_send:
                self.after_send(fd, sig)
        s._pidfd_open, s._pidfd_send, s._close = pidfd_open, pidfd_send, lambda fd: None
        return s

    def run_sweep(self, *argv, docker=True, sweep=None):
        out, err = io.StringIO(), io.StringIO()
        code = self.mod.sweep_main(list(argv), sweep=sweep or self.sweep(docker), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def orphan(self, pid, cmd, age=3 * 3600, cpu=0, unit='app.slice/t3code.service', **kw):
        return self.host.add(pid, MANAGER, cmd.split()[0].rsplit('/', 1)[-1][:15], cmd, f'{CG}/{unit}', age, cpu=cpu, **kw)

    def signalled(self):
        return sorted({pid for pid, _ in self.sent})

    def nothing_stopped(self):
        self.assertEqual(self.sent, [])
        self.assertEqual(self.host.calls('systemctl', 'stop'), [])
        self.assertEqual(self.host.calls('docker', 'rm'), [])

    # ---------- processes ----------

    def test_test_shaped_busy_orphan_is_stopped_with_what_it_started(self):
        self.orphan(2001, 'python3 -m pytest tests/', age=1200, cpu=95)
        self.host.add(2002, 2001, 'sleep', 'sleep 5', f'{CG}/app.slice/t3code.service', 1)
        code, out, err = self.run_sweep()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.signalled(), [2001, 2002])
        self.assertFalse(self.host.exists(2001) or self.host.exists(2002))
        self.assertIn('2 processes stopped', out)
        self.assertIn("stopped: pid 2001 python3", out)
        self.assertIn("busy; test-shaped ('pytest')", out)
        with open(os.path.join(self.report, 'sweep.log')) as f:
            log = f.read()
        self.assertIn('stopping pid 2001', log)
        self.assertIn('result pid 2001: 2 stopped, 0 reused, 0 left', log)
        with open(os.path.join(self.report, 'last-report.txt')) as f:
            self.assertEqual(f.read(), out)

    def test_long_lived_idle_test_shaped_orphan_is_stopped(self):
        self.orphan(2001, 'bash run.test.sh', age=3 * 86400, cpu=0)
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertEqual(self.signalled(), [2001])
        self.assertIn('long-lived; test-shaped', out)

    def test_not_test_shaped_is_only_reported(self):
        self.orphan(2001, 'java -jar server.jar', age=3 * 3600, cpu=300)
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.nothing_stopped()
        self.assertIn('report only: pid 2001 java', out)
        self.assertIn('busy and long-lived; not test-shaped', out)
        self.assertIn('1 reported', out)

    def test_young_orphan_is_left_alone(self):
        self.orphan(2001, 'python3 -m pytest', age=300, cpu=100)
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertNotIn('2001', out)

    def test_idle_orphan_that_is_not_long_lived_is_left_alone(self):
        self.orphan(2001, 'python3 -m pytest', age=1200, cpu=5)
        self.run_sweep()
        self.nothing_stopped()

    def test_process_with_a_living_parent_is_left_alone(self):
        self.host.add(2001, SERVICE, 'python3', 'python3 -m pytest', f'{CG}/app.slice/t3code.service', 3600, cpu=99)
        self.run_sweep()
        self.nothing_stopped()

    def test_orphan_of_pid_1_counts_too(self):
        self.host.add(2001, 1, 'python3', 'python3 -m pytest', f'/user.slice/user-{UID}.slice/session-4.scope',
                      3600, cpu=99)
        self.run_sweep()
        self.assertEqual(self.signalled(), [2001])

    def test_a_units_own_main_process_is_left_alone(self):
        self.orphan(2001, 'python3 -m pytest --serve', unit='app.slice/runner.service', cpu=99)
        self.host.load()['units']['runner.service'] = {'MainPID': 2001, 'ActiveState': 'active'}
        self.host.save()
        self.run_sweep()
        self.nothing_stopped()

    def test_known_unit_is_left_alone(self):
        self.orphan(2001, 'python3 -m pytest', unit='app.slice/known.service', cpu=99)
        self.run_sweep()
        self.nothing_stopped()

    def test_ignore_pattern_is_left_alone_and_not_reported(self):
        self.orphan(2001, 'tmux new -d pytest', cpu=99)
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertNotIn('2001', out)

    def test_other_users_and_zombies_are_left_alone(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99, uid=UID + 1)
        self.orphan(2002, 'python3 -m pytest', cpu=0, state='Z')
        self.run_sweep()
        self.nothing_stopped()

    def test_never_stops_itself_or_what_started_it(self):
        self.host.add(SELF_PARENT, MANAGER, 'bash', 'bash sweep.test.sh', f'{CG}/app.slice/t3code.service',
                      3 * 3600, cpu=90)
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertNotIn(f'pid {SELF_PARENT}', out)

    def test_stubborn_process_gets_kill_after_the_grace_time(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        self.stubborn.add(2001)
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertEqual(self.sent, [(2001, 'SIGTERM'), (2001, 'SIGKILL')])

    def test_pid_reused_before_the_stop_is_not_signalled(self):
        start = self.orphan(2001, 'python3 -m pytest', cpu=99)

        def reuse(pid):
            if pid == 2001:
                self.host.set_start(2001, start + 500)     # another process got the pid
        self.before_open = reuse
        code, out, _ = self.run_sweep()
        self.assertEqual(self.sent, [])
        self.assertTrue(self.host.exists(2001))
        self.assertIn('1 pids now belong to another process, left alone', out)

    # ---------- heavy scopes ----------

    def scope(self, name='heavy-pytest-0a1b2c3d.scope'):
        return f'heavy.slice/{name}'

    def test_live_heavy_scope_is_left_alone(self):
        self.host.add(3000, SERVICE, 'python3', 'python3 heavy pytest', f'{CG}/app.slice/t3code.service', 3600)
        self.host.add(3001, 3000, 'pytest', 'pytest -x', f'{CG}/{self.scope()}', 3600, cpu=90)
        self.orphan(3002, 'python3 -m pytest worker', unit=self.scope(), cpu=99)      # its job ended; heavy still runs
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertNotIn('300', out.replace('3000', ''))

    def test_scope_whose_heavy_is_gone_is_stopped_as_a_whole(self):
        self.orphan(3002, 'node worker.js', unit=self.scope(), age=1200, cpu=0)      # not test-shaped, not busy
        self.host.add(3003, 3002, 'sleep', 'sleep 9', f'{CG}/{self.scope()}', 1200)
        self.host.load()['units']['heavy-pytest-0a1b2c3d.scope'] = {'ActiveState': 'active'}
        self.host.save()
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0, out)
        self.assertIn(['systemctl', '--user', 'stop', '--', 'heavy-pytest-0a1b2c3d.scope'], self.host.calls('systemctl'))
        self.assertFalse(self.host.exists(3002) or self.host.exists(3003))
        self.assertIn('stopped heavy-pytest-0a1b2c3d.scope: its heavy is gone; 2 processes', out)

    def test_young_scope_whose_heavy_is_gone_waits(self):
        self.orphan(3002, 'node worker.js', unit=self.scope(), age=30)
        self.host.load()['units']['heavy-pytest-0a1b2c3d.scope'] = {'ActiveState': 'active'}
        self.host.save()
        self.run_sweep()
        self.nothing_stopped()

    def test_scope_that_does_not_stop_is_a_problem_then_killed_by_hand(self):
        self.orphan(3002, 'node worker.js', unit=self.scope(), age=1200)
        st = self.host.load()
        st['units']['heavy-pytest-0a1b2c3d.scope'] = {'ActiveState': 'active'}
        st['stuckUnits'] = ['heavy-pytest-0a1b2c3d.scope']
        self.host.save()
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.signalled(), [3002])

    def test_kept_scope_is_never_stopped_only_reported(self):
        kept = self.scope('heavy-keep-server-0a1b2c3d.scope')
        self.orphan(3002, 'python3 -m pytest --serve', unit=kept, age=3 * 3600, cpu=90)
        st = self.host.load()
        st['units']['heavy-keep-server-0a1b2c3d.scope'] = {'ActiveState': 'active'}
        st['units']['heavy-keep-empty-0a1b2c3d.scope'] = {'ActiveState': 'active'}
        self.host.save()
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertIn('report only: pid 3002', out)
        self.assertIn('kept on purpose (HEAVY_KEEP=1 in heavy-keep-server-0a1b2c3d.scope)', out)

    def test_empty_heavy_scope_is_stopped(self):
        self.host.load()['units']['heavy-x-0a1b2c3d.scope'] = {'ActiveState': 'failed'}
        self.host.save()
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertIn(['systemctl', '--user', 'stop', '--', 'heavy-x-0a1b2c3d.scope'], self.host.calls('systemctl'))
        self.assertIn(['systemctl', '--user', 'reset-failed', '--', 'heavy-x-0a1b2c3d.scope'],
                      self.host.calls('systemctl'))
        self.assertIn('stopped heavy-x-0a1b2c3d.scope: no process left in it', out)

    # ---------- dry run, settings, lock, half-way ----------

    def test_dry_run_is_the_default_and_stops_nothing(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        self.host.add(SELF_PARENT + 1000, 1, 'x', 'x', f'{CG}/x.service', 1)
        self.host.load()['units']['heavy-x-0a1b2c3d.scope'] = {'ActiveState': 'failed'}
        self.host.save()
        cid = self.host.container('a', 'db1', 3600, owner=f'uid={UID},pid=4242,start=1')
        del self.cfg['stop']
        self.write_machine()
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.nothing_stopped()
        self.assertIn('dry run (heavy.sweep.stop is off in', out)
        self.assertIn('3 to stop or remove', out)
        self.assertIn('would stop: pid 2001', out)
        self.assertIn(f'would remove: container {cid}', out)
        self.cfg['stop'] = True
        self.write_machine()
        code, out, _ = self.run_sweep('--dry-run')
        self.nothing_stopped()
        self.assertIn('dry run (--dry-run)', out)

    def test_empty_sweep_section_runs_with_defaults_as_a_dry_run(self):
        self.write_machine({'version': 1, 'heavy': {'sweep': {}}})
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        s = self.sweep()
        os.environ['HOME'], home = self.root, os.environ.get('HOME')
        try:
            code, out, _ = self.run_sweep(sweep=s)
        finally:
            os.environ['HOME'] = home
        self.assertEqual(code, 0)
        self.nothing_stopped()
        self.assertIn('report only: pid 2001', out)          # no testPatterns: nothing is test-shaped
        self.assertTrue(os.path.isfile(os.path.join(self.root, '.local/state/heavy-sweep/last-report.txt')))

    def test_bad_machine_file_refuses_and_stops_nothing(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        self.host.container('a', 'db1', 3600, owner=f'uid={UID},pid=4242,start=1')
        cases = {'missing': None, 'broken': '{"version": 1,', 'newer': {'version': 2, 'heavy': {'sweep': {}}},
                 'no heavy': {'version': 1}, 'no sweep': {'version': 1, 'heavy': {}},
                 'unknown key': {'version': 1, 'heavy': {'sweep': {'stopp': True}}},
                 'unknown heavy key': {'version': 1, 'heavy': {'sweep': {}, 'slice': 'x.slice'}},
                 'bad type': {'version': 1, 'heavy': {'sweep': {'stop': 'yes'}}},
                 'bad pattern': {'version': 1, 'heavy': {'sweep': {'testPatterns': ['(']}}},
                 'too young': {'version': 1, 'heavy': {'sweep': {'minAgeSeconds': 5}}}}
        for name, data in cases.items():
            with self.subTest(case=name):
                if data is None:
                    os.unlink(self.machine)
                else:
                    self.write_machine(data)
                code, out, err = self.run_sweep()
                self.assertEqual(code, 1, err)
                self.assertIn('nothing checked or stopped', err)
                self.assertIn(self.machine, err)
                self.nothing_stopped()
                self.assertEqual(self.host.calls(), [])
        self.assertFalse(os.path.exists(self.report))

    def test_machine_file_others_can_write_is_refused(self):
        os.chmod(self.machine, 0o666)
        code, _, err = self.run_sweep()
        self.assertEqual(code, 1)
        self.assertIn('writable by group or others', err)

    def test_user_manager_unreachable_refuses_and_stops_nothing(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        self.host.state['systemdDown'] = True
        self.host.save()
        code, out, err = self.run_sweep()
        self.assertEqual(code, 1)
        self.assertIn('cannot reach the systemd user manager', err)
        self.nothing_stopped()

    def test_two_sweeps_at_once_the_second_does_nothing(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        os.makedirs(self.report)
        with open(os.path.join(self.report, 'sweep.lock'), 'a+') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertIn('another sweep is running', out)
        self.nothing_stopped()
        self.assertEqual(self.host.calls(), [])
        code, out, _ = self.run_sweep()                       # the lock is free again
        self.assertEqual(self.signalled(), [2001])

    def test_killed_half_way_then_the_next_sweep_finishes(self):
        self.orphan(2001, 'python3 -m pytest a', cpu=99)
        self.orphan(2002, 'python3 -m pytest b', cpu=99)

        def die(fd, sig):
            raise KeyboardInterrupt('killed half-way (test)')
        self.after_send = die
        with self.assertRaises(KeyboardInterrupt):
            self.run_sweep()
        self.assertEqual(self.signalled(), [2001])
        with open(os.path.join(self.report, 'sweep.log')) as f:
            self.assertIn('stopping pid 2001', f.read())
        self.assertFalse(os.path.exists(os.path.join(self.report, 'last-report.txt')))
        self.after_send = None
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertEqual(self.signalled(), [2001, 2002])
        self.assertIn('1 processes stopped', out)

    def test_log_is_appended_and_rotated(self):
        os.makedirs(self.report)
        log = os.path.join(self.report, 'sweep.log')
        with open(log, 'w') as f:
            f.write('x' * (self.mod.LOG_MAX + 1))
        self.run_sweep()
        self.run_sweep()
        self.assertTrue(os.path.isfile(log + '.1'))
        with open(log) as f:
            self.assertEqual(f.read().count('sweep start'), 2)

    def test_json_looks_only(self):
        self.orphan(2001, 'python3 -m pytest a', cpu=90)
        self.orphan(2002, 'python3 -m pytest b', cpu=60)
        self.orphan(2003, 'java -jar x.jar', cpu=30)
        code, out, _ = self.run_sweep('--json')
        self.assertEqual(code, 0)
        data = json.loads(out)
        self.assertEqual(data['v'], 1)
        self.assertEqual(data['leftovers'], [{'comm': 'python3', 'count': 2, 'cpuPercent': 150},
                                             {'comm': 'java', 'count': 1, 'cpuPercent': 30}])
        self.assertEqual([i['pids'] for i in data['stop']], [[2001], [2002]])
        self.assertEqual([i['pids'] for i in data['report']], [[2003]])
        self.nothing_stopped()
        self.assertFalse(os.path.exists(self.report))
        os.unlink(self.machine)
        code, out, _ = self.run_sweep('--json')
        self.assertEqual(code, 1)
        self.assertIn('not there', json.loads(out)['error'])

    # ---------- containers ----------

    def test_container_whose_owner_pid_is_gone_is_removed(self):
        cid = self.host.container('a', 'db1', 3 * 86400, owner=f'uid={UID},pid=4242,start=77')
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.host.calls('docker', 'rm'), [['docker', 'rm', '-f', '-v', '--', cid]])
        self.assertIn(f'removed: container {cid} db1 (mariadb:11, 3d0h old, running); owner pid 4242 is gone', out)
        self.assertIn('1 containers removed', out)

    def test_container_with_a_living_owner_is_kept(self):
        start = self.orphan(4242, 'python3 testrunner', age=3600)
        self.host.container('a', 'db1', 3600, owner=f'uid={UID},pid=4242,start={start}')
        self.host.container('b', 'db2', 3600, owner=f'uid={UID},scope=heavy-gradle-0a1b2c3d.scope')
        self.host.add(3000, SERVICE, 'python3', 'python3 heavy gradle', f'{CG}/app.slice/t3code.service', 3600)
        self.host.add(3001, 3000, 'java', 'java gradle', f'{CG}/heavy.slice/heavy-gradle-0a1b2c3d.scope', 3600)
        self.run_sweep()
        self.nothing_stopped()

    def test_container_whose_owner_pid_was_reused_is_removed(self):
        start = self.orphan(4242, 'python3 something-else', age=60)
        cid = self.host.container('a', 'db1', 3600, owner=f'uid={UID},pid=4242,start={start - 999}')
        self.run_sweep()
        self.assertEqual(self.host.calls('docker', 'rm'), [['docker', 'rm', '-f', '-v', '--', cid]])

    def test_container_whose_scope_is_gone_or_stopped_is_removed(self):
        gone = self.host.container('a', 'db1', 3600, owner=f'uid={UID},scope=heavy-old-0a1b2c3d.scope')
        self.orphan(3002, 'node worker.js', unit='heavy.slice/heavy-dead-0a1b2c3d.scope', age=1200)
        dead = self.host.container('b', 'db2', 3600, owner=f'uid={UID},scope=heavy-dead-0a1b2c3d.scope')
        self.run_sweep()
        self.assertEqual(sorted(c[-1] for c in self.host.calls('docker', 'rm')), sorted([gone, dead]))
        self.assertFalse(self.host.exists(3002))

    def test_young_container_whose_owner_is_gone_waits(self):
        self.host.container('a', 'db1', 60, owner=f'uid={UID},pid=4242,start=77')
        self.run_sweep()
        self.nothing_stopped()

    def test_unlabelled_container_is_only_reported(self):
        cid = self.host.container('a', 'mariadb-test-1', 3 * 86400)
        self.host.container('b', 'young', 60)
        self.host.container('c', 'stopped-one', 3 * 86400, running=False)
        self.host.container('d', 'dev-db', 3 * 86400)
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.nothing_stopped()
        self.assertIn(f'report only: container {cid} mariadb-test-1', out)
        self.assertIn('no owner label', out)
        self.assertEqual(out.count('container '), 1)

    def test_bad_owner_label_is_only_reported(self):
        self.host.container('a', 'db1', 3600, owner='pid=4242')
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertIn("bad owner label 'pid=4242'", out)

    def test_other_users_container_is_untouched(self):
        self.host.container('a', 'db1', 3600, owner=f'uid={UID + 1},pid=4242,start=77')
        code, out, _ = self.run_sweep()
        self.nothing_stopped()
        self.assertNotIn('db1', out)

    def test_exited_labelled_container_is_removed_too(self):
        cid = self.host.container('a', 'db1', 3600, running=False, owner=f'uid={UID},pid=4242,start=77')
        self.run_sweep()
        self.assertEqual(self.host.calls('docker', 'rm'), [['docker', 'rm', '-f', '-v', '--', cid]])

    def test_container_already_gone_is_fine(self):
        cid = self.host.container('a', 'db1', 3600, owner=f'uid={UID},pid=4242,start=77')
        self.host.container('b', 'db2', 3600, owner=f'uid={UID},pid=4242,start=77', goneBeforeInspect=True)
        self.host.state['vanishOnRm'] = [cid]
        self.host.save()
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0, out)
        self.assertIn(f'already gone: container {cid}', out)
        self.assertNotIn('db2', out)

    def test_no_docker_or_docker_down_still_sweeps_processes(self):
        self.orphan(2001, 'python3 -m pytest', cpu=99)
        code, out, _ = self.run_sweep(docker=False)
        self.assertEqual(code, 0)
        self.assertIn('note: docker: not installed; containers not checked', out)
        self.assertEqual(self.signalled(), [2001])
        self.host.load()['dockerDown'] = True
        self.host.save()
        self.orphan(2002, 'python3 -m pytest', cpu=99)
        code, out, _ = self.run_sweep()
        self.assertEqual(code, 0)
        self.assertIn('Cannot connect to the Docker daemon', out)
        self.assertEqual(self.signalled(), [2001, 2002])


class Label(unittest.TestCase):
    def test_owner_value_heavy_scope_first_then_the_parent_process(self):
        old = os.environ.pop('HEAVY_SCOPE', None)
        self.addCleanup(lambda: os.environ.__setitem__('HEAVY_SCOPE', old) if old else os.environ.pop('HEAVY_SCOPE', None))
        os.environ['HEAVY_SCOPE'] = 'heavy-pytest-0a1b2c3d.scope'
        self.assertEqual(REAL.owner_value(), f'uid={UID},scope=heavy-pytest-0a1b2c3d.scope')
        os.environ['HEAVY_SCOPE'] = 'not-a-heavy-scope; rm -rf'
        value = REAL.owner_value()
        os.environ.pop('HEAVY_SCOPE')
        if 'scope=' not in value:          # (this test may itself run under heavy)
            owner = REAL.parse_owner(value)
            self.assertEqual(owner['pid'], os.getppid())
        with open(f'/proc/{os.getpid()}/stat') as f:
            stat = f.read()
        start = int(stat[stat.rfind(')') + 2:].split()[19])
        self.assertEqual(REAL.owner_value(pid=os.getpid()), f'uid={UID},pid={os.getpid()},start={start}')

    def test_cli_and_round_trip_through_the_sweep_rules(self):
        env = dict(os.environ, HEAVY_SCOPE='')
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'docker-label', '--pid', str(os.getpid())],
                           capture_output=True, text=True, env=env)
        self.assertEqual(r.returncode, 0, r.stderr)
        key, _, value = r.stdout.strip().partition('=')
        self.assertEqual(key, LABEL)
        owner = REAL.parse_owner(value)
        self.assertEqual((owner['uid'], owner['pid']), (UID, os.getpid()))
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'docker-label', '--value'], capture_output=True,
                           text=True, env=dict(env, HEAVY_SCOPE='heavy-a-0a1b2c3d.scope'))
        self.assertEqual(r.stdout, f'uid={UID},scope=heavy-a-0a1b2c3d.scope\n')
        r = subprocess.run([sys.executable, '-I', '-B', HEAVY, 'docker-label', '--pid'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2)


class RealSignal(unittest.TestCase):
    """The real pidfd path, on a child process this test starts."""

    def test_signals_exactly_that_process_and_never_a_reused_pid(self):
        s = REAL.Sweep()
        s.stop_grace = 2
        child = subprocess.Popen(['sleep', '60'])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        p = s.read_proc(child.pid)
        self.assertEqual(p.uid, UID)
        wrong = REAL.Proc(**{k: getattr(p, k) for k in p.__slots__})
        wrong.start += 1
        self.assertEqual(s.signal(wrong, signal.SIGTERM), 'reused')
        self.assertIsNone(child.poll())
        self.assertEqual(s.signal(p, 0), 'sent')
        stopped, reused, left = s.stop_procs([p])
        child.wait(timeout=10)
        self.assertEqual((len(stopped), reused), (1, []))


# Each rule broken on purpose in a copy of lib/heavy_sweep.py: (text, broken text, the test that must then fail)
MUTATIONS = {
    'owner gone (reparented)': ('if p.pid in mine or p.pid in (1, manager) or p.ppid not in orphan_parents:',
                                'if p.pid in mine or p.pid in (1, manager):',
                                'test_process_with_a_living_parent_is_left_alone'),
    'older than minAgeSeconds': ("if p.age < cfg['minAgeSeconds'] or any(", 'if False or any(',
                                 'test_young_orphan_is_left_alone'),
    'busy or long-lived': ('if not (busy or long_lived):', 'if False:',
                           'test_idle_orphan_that_is_not_long_lived_is_left_alone'),
    'only test-shaped stopped': ('elif test:', 'elif True:', 'test_not_test_shaped_is_only_reported'),
    "a unit's own process": ("if str(p.pid) in (u.get('MainPID'), u.get('ControlPID')):", 'if False:',
                             'test_a_units_own_main_process_is_left_alone'),
    'known units': ("if any(fnmatch.fnmatchcase(p.unit, g) for g in cfg['knownUnits']):", 'if False:',
                    'test_known_unit_is_left_alone'),
    'live heavy scope': ('if any(p.ppid not in orphan_parents and p.ppid not in pids for p in ps) or pids & mine:',
                         'if pids & mine:', 'test_live_heavy_scope_is_left_alone'),
    'kept scope': ('elif is_kept_scope(unit):', 'elif False:', 'test_kept_scope_is_never_stopped_only_reported'),
    'empty scope': ('if unit not in members and not is_kept_scope(unit):', 'if False:',
                    'test_empty_heavy_scope_is_stopped'),
    'never itself': ('while pid in procs and pid not in mine:', 'while False:',
                     'test_never_stops_itself_or_what_started_it'),
    'dry run default': ("act = cfg['stop'] and not dry", 'act = True',
                        'test_dry_run_is_the_default_and_stops_nothing'),
    'pid reuse': ("if cur.start != p.start or cur.uid != p.uid:\n                return 'reused'",
                  "if False:\n                return 'reused'", 'test_pid_reused_before_the_stop_is_not_signalled'),
    'lock': ('fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'int(0)',
             'test_two_sweeps_at_once_the_second_does_nothing'),
    'container owner start time': ("gone = p is None or p.start != owner['start']", 'gone = p is None',
                                   'test_container_whose_owner_pid_was_reused_is_removed'),
    'container owner alive': ("if not gone or age < cfg['minAgeSeconds']:", "if age < cfg['minAgeSeconds']:",
                              'test_container_with_a_living_owner_is_kept'),
    'unlabelled only reported': ("return dict(base, action='report', why='no owner label')",
                                 "return dict(base, action='remove', why='no owner label')",
                                 'test_unlabelled_container_is_only_reported'),
    "other user's container": ("if owner['uid'] != self.uid:", 'if False:',
                               'test_other_users_container_is_untouched'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(SWEEP) as f:
            source = f.read()
        for rule, (text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                cls = type('Broken', (SweepTests,), {'mod': load_sweep(source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')
                ok = unittest.TestResult()
                SweepTests(test).run(ok)
                self.assertTrue(ok.wasSuccessful(), f'{test} fails on the real code')


if __name__ == '__main__':
    unittest.main(verbosity=2)
