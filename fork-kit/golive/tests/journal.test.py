"""The kit's lock and journal, the systemd adapter (with stub commands) and the version order.
No root, no systemd, no T3.  python3 -I -B fork-kit/golive/tests/journal.test.py"""
import fcntl, json, os, subprocess, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import journal, live, osadapter  # noqa: E402

HOLD = '''import os, sys, time
sys.path[:0] = {paths!r}
from golive import journal
with journal.run({state!r}, 'copy') as r:
    with r.step('stop-service'):
        pass
    with r.step('cold-copy') as facts:
        facts['sha256'] = 'abc'
    with r.step('switch'):
        print('ready', flush=True)
        if {kill}:
            os.kill(os.getpid(), 9)
        time.sleep(60)
'''


class Base(unittest.TestCase):
    def setUp(self):
        self.m = fx.Machine()
        self.addCleanup(self.m.close)
        self.state = self.m.state

    def hold(self, kill):
        code = HOLD.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON], state=self.state, kill=kill)
        p = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL,
                             text=True)
        self.addCleanup(fx.kill, p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def journal_lines(self):
        with open(os.path.join(self.state, 'journal.jsonl'), 'rb') as f:
            return f.read().split(b'\n')


class Lock(Base):
    def test_a_second_run_while_one_runs_is_refused_and_names_the_first(self):
        p = self.hold(kill=False)
        with self.assertRaises(journal.Busy) as e:
            with journal.run(self.state, 'switch'):
                self.fail('ran beside another run')
        self.assertIn(f'copy (pid {p.pid}', str(e.exception))

    def test_a_run_is_refused_while_a_check_runs(self):
        with journal.lock(self.state, 'check', exclusive=False):
            with self.assertRaises(journal.Busy) as e:
                with journal.run(self.state, 'copy'):
                    self.fail('ran beside a check')
        self.assertIn('a golive check is running', str(e.exception))
        self.assertFalse(os.path.exists(os.path.join(self.state, 'journal.jsonl')))

    def test_an_empty_lock_held_alone_is_a_run_starting(self):
        # The instant between a changing run taking the lock and writing its name: a check must not call it a check.
        os.makedirs(self.state)
        fd = os.open(os.path.join(self.state, 'lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        with self.assertRaises(journal.Busy) as e:
            with journal.lock(self.state, 'check', exclusive=False):
                pass
        self.assertIn('a golive run is just starting', str(e.exception))

    def test_the_lock_dies_with_its_holder(self):
        p = self.hold(kill=True)
        p.wait()
        with journal.lock(self.state, 'copy', exclusive=True):
            pass  # free: no stale lock to clear by hand

    def test_the_state_folder_is_private(self):
        with journal.run(self.state, 'copy'):
            pass
        self.assertEqual(os.stat(self.state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(os.path.join(self.state, 'journal.jsonl')).st_mode & 0o777, 0o600)


class Journal(Base):
    def test_a_finished_run_reads_back_with_its_steps_and_facts(self):
        with journal.run(self.state, 'copy') as r:
            with r.step('cold-copy', database='statev2.sqlite') as facts:
                facts['sha256'] = 'abc'
        runs, torn = journal.read(self.state)
        self.assertIsNone(torn)
        self.assertEqual(len(runs), 1)
        self.assertEqual(runs[0]['end']['result'], 'ok')
        self.assertEqual([(s['step'], s['phase'], s['data']) for s in runs[0]['steps']],
                         [('cold-copy', 'started', {'database': 'statev2.sqlite'}),
                          ('cold-copy', 'done', {'sha256': 'abc'})])
        self.assertIsNone(journal.unfinished(self.state))

    def test_an_exception_ends_the_run_failed(self):
        with self.assertRaises(OSError):
            with journal.run(self.state, 'copy') as r:
                with r.step('cold-copy'):
                    raise OSError('disk full')
        run = journal.read(self.state)[0][-1]
        self.assertEqual(run['end']['result'], 'failed')
        self.assertIn('disk full', run['end']['detail'])
        self.assertEqual([s['phase'] for s in run['steps']], ['started'])  # never "done"
        # An error after a step started can leave the home half-applied, like a kill: the next run is refused
        # until the owner settles it, and the refusal says how.
        with self.assertRaises(journal.Unfinished) as e:
            with journal.run(self.state, 'copy'):
                self.fail('ran over a failed run')
        self.assertIn('ended failed after step "cold-copy" started', str(e.exception))
        self.assertIn('t3-golive settle --reason', str(e.exception))
        what, run = journal.settle(self.state, 'database copy checked by hand')
        self.assertEqual(what, 'settled')
        self.assertEqual(journal.read(self.state)[0][-1]['settled']['detail'], 'database copy checked by hand')
        with journal.run(self.state, 'copy'):
            pass

    def test_a_run_that_failed_before_its_first_step_does_not_block(self):
        with self.assertRaises(ValueError):
            with journal.run(self.state, 'copy'):
                raise ValueError('refused before changing anything')
        self.assertIsNone(journal.unfinished(self.state))
        with journal.run(self.state, 'copy'):
            pass

    def test_run_ids_are_unique_within_a_second(self):
        for _ in range(3):
            with journal.run(self.state, 'copy'):
                pass
        ids = [r['id'] for r in journal.read(self.state)[0]]
        self.assertEqual(len(set(ids)), 3)

    def test_a_run_killed_half_way_blocks_the_next_until_it_is_settled(self):
        p = self.hold(kill=True)
        p.wait()
        last = journal.unfinished(self.state)
        self.assertEqual((last['command'], last['pid'], last['steps'][-1]['step'], last['steps'][-1]['phase']),
                         ('copy', p.pid, 'switch', 'started'))
        with self.assertRaises(journal.Unfinished) as e:
            with journal.run(self.state, 'copy'):
                self.fail('ran over an unfinished run')
        self.assertIn('step "switch" started', str(e.exception))
        self.assertIn('t3-golive settle --reason', str(e.exception))
        with self.assertRaises(ValueError):
            journal.settle(self.state, '  ')  # the owner says what was done
        what, settled = journal.settle(self.state, 'service restarted on the old build by hand')
        self.assertEqual((what, settled['id']), ('settled', last['id']))
        self.assertIsNone(journal.unfinished(self.state))
        run = journal.read(self.state)[0][-1]
        self.assertEqual((run['end'], run['settled']['detail']), (None, 'service restarted on the old build by hand'))
        before = self.journal_lines()
        self.assertEqual(journal.settle(self.state, 'again'), (None, None))  # running it again changes nothing
        self.assertEqual(self.journal_lines(), before)
        with journal.run(self.state, 'copy'):
            pass

    def test_settle_waits_for_a_running_command(self):
        self.hold(kill=False)
        with self.assertRaises(journal.Busy):
            journal.settle(self.state, 'x')

    def test_a_torn_last_line_is_reported_ignored_and_cut_by_the_next_run(self):
        with journal.run(self.state, 'copy'):
            pass
        path = os.path.join(self.state, 'journal.jsonl')
        with open(path, 'ab') as f:
            f.write(b'{"v": 1, "prev": "')
        runs, torn = journal.read(self.state)
        self.assertEqual(len(runs), 1)
        self.assertIn('torn line', torn)
        with journal.run(self.state, 'copy'):
            pass
        runs, torn = journal.read(self.state)
        self.assertEqual((len(runs), torn), (2, None))

    def test_damage_before_the_last_line_is_refused(self):
        with journal.run(self.state, 'copy'):
            pass
        with journal.run(self.state, 'copy'):
            pass
        lines = self.journal_lines()
        cases = {
            'edited line': [lines[0].replace(b'copy', b'kopy')] + lines[1:],
            'lost line': lines[:1] + lines[2:],
            'garbage line': lines[:1] + [b'not json'] + lines[1:],
        }
        # an "end" whose run never began, with a valid chain
        stray = json.loads(lines[1])
        stray['run'] = 'nobody'
        cases['line of an unknown run'] = [lines[0], json.dumps(stray, sort_keys=True).encode()]
        for label, new in cases.items():
            with self.subTest(label):
                with open(os.path.join(self.state, 'journal.jsonl'), 'wb') as f:
                    f.write(b'\n'.join(new) + (b'' if new[-1] == b'' else b'\n'))
                with self.assertRaises(journal.Damaged):
                    journal.read(self.state)
                with self.assertRaises(journal.Damaged) as e:
                    with journal.run(self.state, 'copy'):
                        self.fail('ran on a damaged journal')
                self.assertNotIsInstance(e.exception, journal.Newer)
                self.assertIn(os.path.join(self.state, 'journal.jsonl'), str(e.exception))
                self.assertIn('t3-golive settle', str(e.exception))

    def test_settle_moves_a_damaged_journal_aside(self):
        with journal.run(self.state, 'copy'):
            pass
        path = os.path.join(self.state, 'journal.jsonl')
        with open(path, 'ab') as f:
            f.write(b'garbage\n{"v": 1}\n')
        what, aside = journal.settle(self.state, 'read it, home is fine')
        self.assertEqual(what, 'moved')
        self.assertTrue(aside.startswith(path + '.damaged-'))
        self.assertIn(b'garbage', open(aside, 'rb').read())
        self.assertEqual(journal.read(self.state), ([], None))
        with journal.run(self.state, 'copy'):
            pass

    def test_a_journal_from_a_newer_kit_says_update_the_kit_and_is_left_alone(self):
        with journal.run(self.state, 'copy'):
            pass
        path = os.path.join(self.state, 'journal.jsonl')
        with open(path, 'ab') as f:
            f.write(json.dumps({'v': 2, 'prev': 'anything', 'event': 'begin'}).encode() + b'\n')
        before = self.journal_lines()
        for attempt in (lambda: journal.read(self.state), lambda: journal.settle(self.state, 'x')):
            with self.assertRaises(journal.Newer) as e:
                attempt()
            self.assertIn('update the kit', str(e.exception))
            self.assertNotIn('cannot be trusted', str(e.exception))
        with self.assertRaises(journal.Newer):
            with journal.run(self.state, 'copy'):
                self.fail('ran on a newer journal')
        self.assertEqual(self.journal_lines(), before)

    def test_an_empty_or_missing_journal_is_a_fresh_start(self):
        self.assertEqual(journal.read(self.state), ([], None))
        os.makedirs(self.state, exist_ok=True)
        open(os.path.join(self.state, 'journal.jsonl'), 'wb').close()
        self.assertEqual(journal.read(self.state), ([], None))
        with journal.run(self.state, 'copy'):
            pass
        self.assertEqual(len(journal.read(self.state)[0]), 1)


class Adapter(Base):
    def test_service_reads_the_unit_and_its_home(self):
        self.m.unit(home='/srv/t3 home/%%x')
        svc = osadapter.SystemdUser().service()
        self.assertEqual(svc, {'loaded': True, 'active': True, 'enabled': True, 'mainPid': os.getpid(),
                               'unitPath': self.m.path('units/t3code.service'), 'home': '/srv/t3 home/%x'})
        self.assertEqual(self.m.calls('systemctl-calls.jsonl'),
                         [['--user', 'show', 't3code.service',
                           '--property=LoadState,ActiveState,UnitFileState,FragmentPath,MainPID']])

    def test_service_not_installed(self):
        self.m.service(LoadState='not-found', ActiveState='inactive', UnitFileState='', FragmentPath='', MainPID=0)
        svc = osadapter.SystemdUser().service()
        self.assertEqual((svc['loaded'], svc['mainPid'], svc['home']), (False, None, None))

    def test_unquote_matches_upstream(self):
        for raw, want in (('/a/b', '/a/b'), ('"/a b"', '/a b'), ('"/a \\"q\\""', '/a "q"'),
                          ('"/a\\\\b"', '/a\\b'), ('/100%%', '/100%'), (' "/x" ', '/x')):
            with self.subTest(raw):
                self.assertEqual(osadapter.unquote_systemd(raw), want)
        self.assertIsNone(osadapter.unit_home('[Service]\nExecStart=/x\n'))

    def test_run_detached_starts_the_guard_first(self):
        osadapter.SystemdUser().run_detached('t3-golive-switch', ['/k/bin/t3-golive', 'switch'],
                                             guard_after_seconds=900, guard_argv=['/k/bin/t3-golive', 'back'])
        self.assertEqual(self.m.calls('systemd-run-calls.jsonl'), [
            ['--user', '--quiet', '--collect', '--unit=t3-golive-switch-guard', '--on-active=900s',
             '--timer-property=AccuracySec=1s', '--', '/k/bin/t3-golive', 'back'],
            ['--user', '--quiet', '--collect', '--unit=t3-golive-switch', '--', '/k/bin/t3-golive', 'switch'],
        ])

    def test_run_detached_refuses_bad_input(self):
        a = osadapter.SystemdUser()
        for unit, after in (('a b', 5), ('../x', 5), ('ok', 0), ('ok', '5'), ('ok', None)):
            with self.subTest(unit=unit, after=after):
                with self.assertRaises(osadapter.AdapterError):
                    a.run_detached(unit, ['/x'], guard_after_seconds=after, guard_argv=['/y'])
        self.assertEqual(self.m.calls('systemd-run-calls.jsonl'), [])

    def test_a_failing_service_manager_is_an_error(self):
        self.m.write('bin/systemctl', '#!/bin/sh\necho no bus >&2\nexit 1\n', mode=0o755)
        with self.assertRaises(osadapter.AdapterError) as e:
            osadapter.SystemdUser().service()
        self.assertIn('no bus', str(e.exception))

    def test_stop_and_start_name_only_the_boot_unit(self):
        self.m.unit()
        a = osadapter.SystemdUser()
        a.stop()
        a.start()
        self.assertEqual(self.m.calls('systemctl-calls.jsonl'),
                         [['--user', 'stop', 't3code.service'], ['--user', 'start', 't3code.service']])


class Versions(unittest.TestCase):
    def test_order_matches_upstream(self):
        ordered = ['0.1.9', '0.1.10-nightly.20261114.4682', '0.1.10-nightly.20261114.4682.fork.1',
                   '0.1.10-nightly.20261115.1', '0.1.10', '0.2.0', '1.0.0']
        for i, a in enumerate(ordered):
            for j, b in enumerate(ordered):
                with self.subTest(a=a, b=b):
                    self.assertEqual(live.compare_versions(a, b), (i > j) - (i < j))
        self.assertEqual(live.compare_versions('1.0.0+build.1', '1.0.0'), 0)
        self.assertEqual(live.compare_versions('1.0.0-alpha', '1.0.0-1'), 1)


if __name__ == '__main__':
    unittest.main()
