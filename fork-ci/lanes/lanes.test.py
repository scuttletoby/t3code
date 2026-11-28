"""fork-ci lanes: the claims contract against claims.Claims, the pool's admission, and lanes run through pool.py.
Run: python3 -B fork-ci/lanes/lanes.test.py
"""
import json, os, signal, subprocess, sys, tempfile, shutil, time, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claims, contract, pool   # noqa: E402

POOL = os.path.join(HERE, 'pool.py')


class DefaultClaims(contract.LaneContract, unittest.TestCase):
    """The contract, against the fork's own claims."""


class Liveness(unittest.TestCase):
    def test_this_process_is_alive_and_a_reused_pid_is_not(self):
        # Is a holder whose pid now belongs to another process (another start time) gone? Yes.
        self.assertTrue(claims.alive(claims.me()))
        self.assertFalse(claims.alive({'pid': os.getpid(), 'start': 'not-this-start'}))

    def test_a_finished_process_is_gone(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        h = {'pid': p.pid, 'start': claims.start_time(p.pid)}
        p.wait()
        self.assertFalse(claims.alive(h))

    def test_what_it_cannot_check_counts_as_alive(self):   # never swept on a guess
        self.assertTrue(claims.alive({'pid': 'x'}))
        self.assertTrue(claims.alive({'pid': 1}))   # another user's process (no start time given)

    def test_an_orphaned_command_keeps_its_holder_alive(self):
        # pool.py killed (SIGKILL: nothing released) while its command runs on: is the claim swept? No: the holder
        # stays alive while any process of its group runs, and is gone once the last one ends.
        gone = subprocess.Popen([sys.executable, '-c', 'pass'])
        h = {'pid': gone.pid, 'start': claims.start_time(gone.pid)}
        gone.wait()
        orphan = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], process_group=0)
        self.addCleanup(lambda: orphan.poll() is None and orphan.kill())
        self.assertTrue(claims.alive({**h, 'pgid': orphan.pid}))
        self.assertFalse(claims.alive(h))
        orphan.kill()
        orphan.wait()
        self.assertFalse(claims.alive({**h, 'pgid': orphan.pid}))

    def test_this_process_as_a_holder_names_its_group(self):
        self.assertEqual(claims.me()['pgid'], os.getpgrp())


class Stopping(unittest.TestCase):
    class Proc:
        def __init__(self):
            self.terminated = 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated += 1

    def test_a_signal_before_the_command_started_stops_it(self):
        # SIGTERM between the claim and the command's start: does the command run on unclaimed, or unstopped? No: it
        # is stopped the moment it starts, and the exit is 128 + the signal.
        s = pool.Stopper()
        s.on_signal(signal.SIGTERM, None)
        proc = self.Proc()
        s.started(proc)
        self.assertEqual(proc.terminated, 1)
        self.assertEqual(s.code(0), 128 + signal.SIGTERM)

    def test_a_signal_while_it_runs_stops_it(self):
        s = pool.Stopper()
        proc = self.Proc()
        s.started(proc)
        self.assertEqual(s.code(3), 3)
        s.on_signal(signal.SIGINT, None)
        self.assertEqual(proc.terminated, 1)
        self.assertEqual(s.code(-2), 128 + signal.SIGINT)


class Admission(unittest.TestCase):
    def test_admit(self):
        # When does one more lane start? Below max_lanes and with its cost free above the reserve and what admitted
        # lanes have not used yet; with free memory or the cost unknown, only as the only lane.
        for lanes, free, cost, pending, ok in ((0, 3000, 800, 0, True), (1, 3000, 800, 0, True),
                                               (2, 1800, 800, 0, False), (4, 99999, 800, 0, False),
                                               (0, None, 800, 0, True), (1, None, 800, 0, False),
                                               (0, 1500, 800, 0, False), (1, 3000, 800, 1200, False),
                                               (1, 3000, 800, 1100, True), (0, 3000, None, 0, True),
                                               (1, 99999, None, 0, False)):
            with self.subTest(lanes=lanes, free=free, cost=cost, pending=pending):
                self.assertEqual(pool.admit(lanes, free, cost, 1024, 4, pending)[0], ok)

    def folder(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        return d

    def cfg(self, d, lane_mb=800):
        return {'claims_dir': d, 'lane_mb': lane_mb, 'reserve_mb': 1024, 'max_lanes': 4}

    def test_a_burst_admits_only_what_fits(self):
        # Four lanes started together on room for two: free memory has not dropped yet (no lane has used its memory),
        # so does each see the same room? No: each admitted lane's cost counts until its group uses it.
        d = self.folder()
        c = claims.Claims(d, alive=lambda h: True)
        got = [pool.take(c, self.cfg(d), ['a'], 'shared', {'pid': n, 'start': 's', 'pgid': n}, f'l{n}', False,
                         free=lambda: 3000, rss=lambda pgid: 0, group=n + 100)[0] for n in (1, 2, 3, 4)]
        self.assertEqual([g is not None for g in got], [True, True, False, False])

    def test_memory_a_lane_already_uses_is_not_counted_twice(self):
        # A lane whose group already uses its cost is in the measured free memory: it reserves nothing more.
        d = self.folder()
        c = claims.Claims(d, alive=lambda h: True)
        got = [pool.take(c, self.cfg(d), ['a'], 'shared', {'pid': n, 'start': 's', 'pgid': n}, f'l{n}', False,
                         free=lambda: 3000, rss=lambda pgid: 800, group=n + 100)[0] for n in (1, 2, 3)]
        self.assertEqual([g is not None for g in got], [True, True, True])

    def test_a_corrupt_measurement_admits_one_lane_at_a_time(self):
        # measured.json it cannot read: never the (lower) default cost on a guess; one lane at a time, and the next
        # measurement starts a fresh list.
        d = self.folder()
        with open(os.path.join(d, pool.MEASURED), 'w') as f:
            f.write('[9000, ')
        self.assertIsNone(pool.read_measured(d))
        c = claims.Claims(d, alive=lambda h: True)
        take = lambda n: pool.take(c, self.cfg(d, lane_mb=1), ['a'], 'shared', {'pid': n, 'start': 's'},  # noqa: E731
                                   f'l{n}', False, free=lambda: 99999, rss=lambda pgid: 0)[0]
        self.assertIsNotNone(take(1))
        self.assertIsNone(take(2))
        pool.record(d, 500)
        self.assertEqual(pool.read_measured(d), [500])
        self.assertEqual(pool.read_measured(self.folder()), [])   # missing: nothing measured yet

    def test_a_restart_on_two_servers_keeps_its_place_on_both(self):
        # A restart that needs a and b, refused on b: does a younger lane get a in the meantime? No.
        d = self.folder()
        c = claims.Claims(d, alive=lambda h: True)
        cfg = self.cfg(d, lane_mb=1)
        kw = dict(free=lambda: 99999, rss=lambda pgid: 0)
        h = lambda n: {'pid': n, 'start': 's'}   # noqa: E731
        held, _ = pool.take(c, cfg, ['b'], 'shared', h(1), 'l1', False, **kw)
        got, why = pool.take(c, cfg, ['a', 'b'], 'alone', h(9), 'l9', True, since=5.0, **kw)
        self.assertIsNone(got, why)
        self.assertEqual(sorted(w['server'] for w in c.holders() if w['kind'] == 'wait'), ['a', 'b'])
        got, why = pool.take(c, cfg, ['a'], 'shared', h(2), 'l2', False, **kw)
        self.assertIsNone(got, why)
        self.assertIn('waiting restart goes first', why)
        for t in held:
            c.release(t)
        got, why = pool.take(c, cfg, ['a', 'b'], 'alone', h(9), 'l9', True, since=5.0, **kw)
        self.assertIsNotNone(got, why)
        self.assertEqual([w for w in c.holders() if w['kind'] == 'wait'], [])   # its waits end with the grant

    def test_the_cost_is_the_largest_recent_peak(self):
        self.assertEqual(pool.cost_mb([], 800), 800)
        self.assertEqual(pool.cost_mb([300, 500, 'x', -1], 800), 500)
        self.assertEqual(pool.cost_mb([9000] + [100] * pool.KEEP, 800), 100)   # an old peak ages out

    def test_free_memory_is_measured(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        path = os.path.join(d, 'meminfo')
        with open(path, 'w') as f:
            f.write('MemTotal:       16000000 kB\nMemAvailable:    2048000 kB\n')
        self.assertEqual(pool.available_mb(path), 2000)
        self.assertIsNone(pool.available_mb(os.path.join(d, 'gone')))

    def test_all_servers_or_none(self):
        # A lane that gets one server but not the next holds neither (no deadlock between two lanes).
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d)
        cfg = {'claims_dir': d, 'lane_mb': 1, 'reserve_mb': 0, 'max_lanes': 4}
        c = claims.Claims(d, alive=lambda h: True)
        c.claim('b', 'alone', {'pid': 1, 'start': 's'}, 'other')
        got, why = pool.take(c, cfg, ['a', 'b'], 'shared', {'pid': 2, 'start': 's'}, 'mine', False,
                             free=lambda: 9999, rss=lambda pgid: 0)
        self.assertIsNone(got, why)
        self.assertEqual([h['server'] for h in c.holders()], ['b'])


class Config(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='lanes-config-')
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, c, sub=''):
        os.makedirs(os.path.join(self.d, sub), exist_ok=True)
        path = os.path.join(self.d, sub, 'lanes.json')
        with open(path, 'w') as f:
            f.write(c if isinstance(c, str) else json.dumps(c))
        return path

    HOST = {'XDG_STATE_HOME': '/state'}

    def test_a_config_it_cannot_use(self):
        for c in ('{', {}, {'servers': {}}, {'servers': {'a': {'port': 0}}}, {'servers': {'a b': {'port': 1}}},
                  {'servers': {'a': {'port': 1}, 'b': {'port': 1}}}, {'servers': {'a': {'port': 1}}, 'max_lanes': 0},
                  {'servers': {'a': {'port': 1}}, 'project': 'y'},
                  {'claims_dir': '/elsewhere', 'servers': {'a': {'port': 1}}}):   # the folder is the host's
            with self.subTest(c):
                with self.assertRaises(pool.ConfigError):
                    pool.load_config(self.write(c), self.HOST)

    def test_the_claims_folder_is_one_per_host(self):
        # Two checkouts of a project (two runners' work folders): do their lanes see each other? Yes: the folder is
        # the host's (FORK_CI_LANES_DIR, absolute, or the state folder), never one beside the config.
        one = pool.load_config(self.write({'servers': {'api': {'port': 4100}}}, 'one'), self.HOST)
        two = pool.load_config(self.write({'servers': {'api': {'port': 4100}}}, 'two'), self.HOST)
        self.assertEqual(one['claims_dir'], '/state/fork-ci-lanes')
        self.assertEqual(two['claims_dir'], one['claims_dir'])
        got = pool.load_config(self.write({'servers': {'a': {'port': 1}}}), {'FORK_CI_LANES_DIR': '/srv/lanes'})
        self.assertEqual(got['claims_dir'], '/srv/lanes')
        with self.assertRaises(pool.ConfigError):   # relative: it would be one per working folder
            pool.load_config(self.write({'servers': {'a': {'port': 1}}}), {'FORK_CI_LANES_DIR': 'lanes'})

    def test_a_project_can_only_tighten_the_host_limits(self):
        # A pull request edits the project's config: can it let in more lanes or keep less free than the host
        # allows? No: max_lanes is the lower, reserve_mb and lane_mb the higher of the project's and the host's.
        host = {**self.HOST, 'FORK_CI_LANES_MAX_LANES': '2', 'FORK_CI_LANES_RESERVE_MB': '2048',
                'FORK_CI_LANES_LANE_MB': '600'}
        loose = {'servers': {'a': {'port': 1}}, 'max_lanes': 50, 'reserve_mb': 0, 'lane_mb': 1}
        c = pool.load_config(self.write(loose), host)
        self.assertEqual((c['max_lanes'], c['reserve_mb'], c['lane_mb']), (2, 2048, 600))
        tight = {'servers': {'a': {'port': 1}}, 'max_lanes': 1, 'reserve_mb': 4096, 'lane_mb': 900}
        c = pool.load_config(self.write(tight), host)
        self.assertEqual((c['max_lanes'], c['reserve_mb'], c['lane_mb']), (1, 4096, 900))
        c = pool.load_config(self.write({'servers': {'a': {'port': 1}}}), self.HOST)
        self.assertEqual((c['lane_mb'], c['reserve_mb'], c['max_lanes']), (800, 1024, 4))
        with self.assertRaises(pool.ConfigError):
            pool.load_config(self.write({'servers': {'a': {'port': 1}}}), {**self.HOST, 'FORK_CI_LANES_MAX_LANES': 'x'})


class Run(unittest.TestCase):
    """Real lanes through pool.py run (real processes and the real lock)."""

    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='lanes-run-'))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.config = os.path.join(self.d, 'lanes.json')
        with open(self.config, 'w') as f:
            json.dump({'servers': {'api': {'port': 4100}, 'db-main': {'port': 4101}}, 'lane_mb': 1, 'reserve_mb': 0,
                       'max_lanes': 4}, f)
        self.env = {**os.environ, 'FORK_CI_LANES_DIR': os.path.join(self.d, 'claims'), 'FORK_CI_LANES_LANE_MB': '1',
                    'FORK_CI_LANES_RESERVE_MB': '0'}
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs if p.poll() is None])

    def lane(self, lane, *args, code='pass', wait='60'):
        p = subprocess.Popen([sys.executable, '-I', POOL, 'run', '--config', self.config, '--lane', lane, *args,
                              '--wait-s', wait, '--', sys.executable, '-c', code], cwd=self.d, env=self.env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def done(self, p):
        out, err = p.communicate(timeout=60)
        return p.returncode, out + err

    def path(self, name):
        return os.path.join(self.d, name)

    def until(self, name):
        code = (f'import os, time\nopen({self.path(name + ".in")!r}, "w").write(str(os.getpid()))\n'
                f'while not os.path.exists({self.path(name + ".go")!r}): time.sleep(0.02)\n')
        return code

    def test_two_shared_lanes_run_at_once_and_get_their_ports(self):
        # Do two lanes that share a server run side by side, each told its ports? Each waits for the other's start.
        meet = ('import os, time\n'
                'open({me!r}, "w").write(os.environ["LANE_PORT_DB_MAIN"] + " " + os.environ["LANE_ID"])\n'
                't = time.time()\n'
                'while not os.path.exists({other!r}):\n    time.sleep(0.02)\n    assert time.time() - t < 30\n')
        a = self.lane('one', '--server', 'api', '--server', 'db-main',
                      code=meet.format(me=self.path('one'), other=self.path('two')))
        b = self.lane('two', '--server', 'api', '--server', 'db-main',
                      code=meet.format(me=self.path('two'), other=self.path('one')))
        self.assertEqual(self.done(a)[0], 0)
        self.assertEqual(self.done(b)[0], 0)
        with open(self.path('one')) as f:
            self.assertEqual(f.read(), '4101 one')
        self.assertEqual(claims.Claims(os.path.join(self.d, 'claims')).holders(), [])   # both released

    def test_an_alone_lane_waits_for_the_shared_one(self):
        a = self.lane('shared', '--server', 'api', code=self.until('shared'))
        self.wait_for(self.path('shared.in'))
        b = self.lane('restart', '--server', 'api', '--alone', '--restart', code='pass')
        self.wait_for_wait('restart')
        self.assertIsNone(b.poll())
        c = self.lane('late', '--server', 'api', code='pass', wait='0')   # behind the waiting restart: no start
        rc, out = self.done(c)
        self.assertEqual(rc, 75, out)
        self.assertIn('waiting restart goes first', out)
        open(self.path('shared.go'), 'w').close()
        self.assertEqual(self.done(a)[0], 0)
        self.assertEqual(self.done(b)[0], 0)

    def test_sigterm_stops_the_command_and_releases(self):
        a = self.lane('one', '--server', 'api', '--alone', code=self.until('one'))
        self.wait_for(self.path('one.in'))
        a.send_signal(signal.SIGTERM)
        rc, out = self.done(a)
        self.assertEqual(rc, 128 + signal.SIGTERM, out)
        self.assertEqual(claims.Claims(os.path.join(self.d, 'claims')).holders(), [])
        self.assertEqual(self.done(self.lane('two', '--server', 'api', '--alone'))[0], 0)

    def test_a_killed_pool_keeps_its_claim_while_the_command_runs(self):
        # pool.py killed with SIGKILL (no cleanup) while its shared command runs on: can an alone lane get the server
        # under the orphaned command? No: the claim holds until the command's last process ends.
        a = self.lane('one', '--server', 'api', code=self.until('one'))
        self.wait_for(self.path('one.in'))
        with open(self.path('one.in')) as f:
            orphan = int(f.read())
        self.addCleanup(lambda: subprocess.run(['kill', '-9', str(orphan)], capture_output=True))
        a.kill()
        a.wait()
        rc, out = self.done(self.lane('two', '--server', 'api', '--alone', '--restart', wait='1'))
        self.assertEqual(rc, 75, out)
        self.assertIn('held shared', out)
        os.kill(orphan, signal.SIGKILL)
        for _ in range(500):
            try:
                os.kill(orphan, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        rc, out = self.done(self.lane('three', '--server', 'api', '--alone'))
        self.assertEqual(rc, 0, out)

    def test_a_lane_records_its_peak_memory(self):
        self.assertEqual(self.done(self.lane('one', '--server', 'api', code='x = bytearray(30 << 20)'))[0], 0)
        got = pool.read_measured(os.path.join(self.d, 'claims'))
        self.assertEqual(len(got), 1)
        self.assertGreaterEqual(got[0], 30)

    def test_an_unknown_server_or_no_command(self):
        rc, out = self.done(self.lane('one', '--server', 'cache'))
        self.assertEqual(rc, 2, out)
        self.assertIn("unknown server 'cache'", out)

    def wait_for(self, path):
        for _ in range(1500):
            if os.path.exists(path):
                return
            time.sleep(0.02)
        self.fail(f'{path} never appeared')

    def wait_for_wait(self, lane):
        c = claims.Claims(os.path.join(self.d, 'claims'))
        for _ in range(1500):
            if any(h['kind'] == 'wait' and h['lane'] == lane for h in c.holders()):
                return
            time.sleep(0.02)
        self.fail(f'lane {lane} never waited')


if __name__ == '__main__':
    unittest.main(verbosity=1)
