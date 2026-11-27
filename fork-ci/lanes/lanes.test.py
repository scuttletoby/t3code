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
        self.assertFalse(claims.alive({**claims.me(), 'start': 'not-this-start'}))

    def test_a_finished_process_is_gone(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        h = {'pid': p.pid, 'start': claims.start_time(p.pid)}
        p.wait()
        self.assertFalse(claims.alive(h))

    def test_what_it_cannot_check_counts_as_alive(self):   # never swept on a guess
        self.assertTrue(claims.alive({'pid': 'x'}))
        self.assertTrue(claims.alive({'pid': 1}))   # another user's process (no start time given)


class Admission(unittest.TestCase):
    def test_admit(self):
        # When does one more lane start? Below max_lanes and with its cost free above the reserve; with free memory
        # unknown, only as the only lane.
        for lanes, free, ok in ((0, 3000, True), (1, 3000, True), (2, 1800, False), (4, 99999, False),
                                (0, None, True), (1, None, False), (0, 1500, False)):
            with self.subTest(lanes=lanes, free=free):
                self.assertEqual(pool.admit(lanes, free, 800, 1024, 4)[0], ok)

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
        c.claim('b', 'alone', {'pid': 1}, 'other')
        got, why = pool.take(c, cfg, ['a', 'b'], 'shared', {'pid': 2}, 'mine', False, free=lambda: 9999)
        self.assertIsNone(got, why)
        self.assertEqual([h['server'] for h in c.holders()], ['b'])


class Config(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='lanes-config-')
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, c):
        path = os.path.join(self.d, 'lanes.json')
        with open(path, 'w') as f:
            f.write(c if isinstance(c, str) else json.dumps(c))
        return path

    def test_a_config_it_cannot_use(self):
        for c in ('{', {}, {'claims_dir': 'x', 'servers': {}}, {'claims_dir': 'x', 'servers': {'a': {'port': 0}}},
                  {'claims_dir': 'x', 'servers': {'a b': {'port': 1}}},
                  {'claims_dir': 'x', 'servers': {'a': {'port': 1}, 'b': {'port': 1}}},
                  {'claims_dir': 'x', 'servers': {'a': {'port': 1}}, 'max_lanes': 0},
                  {'claims_dir': 'x', 'servers': {'a': {'port': 1}}, 'project': 'y'}):
            with self.subTest(c):
                with self.assertRaises(pool.ConfigError):
                    pool.load_config(self.write(c))

    def test_claims_dir_is_from_the_configs_folder(self):
        c = pool.load_config(self.write({'claims_dir': '.lanes', 'servers': {'api': {'port': 4100}}}))
        self.assertEqual(c['claims_dir'], os.path.join(self.d, '.lanes'))
        self.assertEqual((c['lane_mb'], c['reserve_mb'], c['max_lanes']), (800, 1024, 4))


class Run(unittest.TestCase):
    """Real lanes through pool.py run (real processes and the real lock)."""

    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='lanes-run-'))
        self.addCleanup(shutil.rmtree, self.d, True)
        self.config = os.path.join(self.d, 'lanes.json')
        with open(self.config, 'w') as f:
            json.dump({'claims_dir': 'claims', 'servers': {'api': {'port': 4100}, 'db-main': {'port': 4101}},
                       'lane_mb': 1, 'reserve_mb': 0, 'max_lanes': 4}, f)
        self.procs = []
        self.addCleanup(lambda: [p.kill() for p in self.procs if p.poll() is None])

    def lane(self, lane, *args, code='pass', wait='60'):
        p = subprocess.Popen([sys.executable, '-I', POOL, 'run', '--config', self.config, '--lane', lane, *args,
                              '--wait-s', wait, '--', sys.executable, '-c', code], cwd=self.d,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.procs.append(p)
        return p

    def done(self, p):
        out, err = p.communicate(timeout=60)
        return p.returncode, out + err

    def path(self, name):
        return os.path.join(self.d, name)

    def until(self, name):
        code = (f'import os, time\nopen({self.path(name + ".in")!r}, "w").close()\n'
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
