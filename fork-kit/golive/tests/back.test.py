"""t3-golive back (W1, W2, W3) and status on a fake machine (golive_fixture.py, back_fixture.py): the service
manager is faked, the T3 home is a temp folder, a stub t3 acts as the fork build, the old build and an upstream
nightly. Nothing here stops, starts, reads or calls a real T3 or its port.
python3 -I -B fork-kit/golive/tests/back.test.py
"""
import hashlib, io, json, os, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
import back_fixture as bx  # noqa: E402
from golive import back, check, coldcopy, config, journal  # noqa: E402

# A back run in its own process, SIGKILLed where `setup` says.
KILLED = '''import os, sys
sys.path[:0] = {paths!r}
import back_fixture as bx
from golive import back, coldcopy, config, osadapter
config.MACHINE_FILE, config.MACHINE_OWNER = {machine!r}, os.getuid()
osadapter.SYSTEMCTL, osadapter.CGROUP_FILE = {systemctl!r}, {cgroup!r}
coldcopy.POLL, coldcopy.STOP_WAIT, coldcopy.START_WAIT = 0.01, 3, 3
back.T3_STUB = {stub!r}


def kill(*args):
    os.kill(os.getpid(), 9)


on = {{}}
{setup}
back.run_back(config.load(), bx.Adapter({root!r}, None, on=on), 'w1', lose_work_since={since!r}, say=lambda l: None)
'''


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class BackBase(unittest.TestCase):
    def setUp(self):
        self.m = bx.Machine()
        self.addCleanup(self.m.close)
        self.m.standard()
        self.adapter = bx.Adapter(self.m.root, self.m)
        patcher = mock.patch.multiple(coldcopy, QUIET_GAP=0, POLL=0.01, STOP_WAIT=3, START_WAIT=3, GUARD_WAIT=0.3)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(back, 'T3_STUB', self.m.path('bin/t3-stub'))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.said = []

    def take_copy(self):
        result = coldcopy.run_copy(config.load(), self.adapter, owner_says_go=True, say=self.said.append)
        self.copy, self.at = result['copy'], coldcopy.read_manifest(result['copy'])['createdAt']
        self.copied = sha(os.path.join(self.copy, 'userdata/statev2.sqlite'))
        return self.copy

    def switch(self):
        """A switch to the fork build, journalled as PR 6 will: its verify, and the counts before it."""
        self.m.switched()
        with journal.run(self.m.state, 'switch') as r:
            with r.step('check') as facts:
                facts['reportCounts'] = {'projects': 3, 'projectsArchived': 0, 'threads': 40, 'threadsArchived': 2}
            with r.step('verify') as facts:
                facts.update(version=fx.TARGET, pid=self.m.server.pid)
        self.migrated = sha(self.m.path('home/userdata/statev2.sqlite'))

    def back(self, way, **kw):
        return back.run_back(config.load(), self.adapter, way, say=self.said.append, **kw)

    def calls(self):
        return [c[0] for c in self.m.calls('fake-adapter-calls.jsonl')]

    def updates(self):
        return [c for c in self.m.calls('back-t3-calls.jsonl') if c[0] == 'update']

    def live_version(self):
        with open(self.m.path('home/runtime/service-state.json')) as f:
            return json.load(f)['activeVersion']

    def db(self):
        return sha(self.m.path('home/userdata/statev2.sqlite'))

    def kept(self):
        return {sha(p) for p, _ in coldcopy.kept_files(self.m.home)}

    def last_run(self):
        return journal.read(self.m.state)[0][-1]

    def main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(back.osadapter, 'for_this_os', return_value=self.adapter):
            code = check.main(list(args), out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class Ways(BackBase):
    def test_no_copy_names_w1_impossible_and_why(self):
        self.switch()
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn('no finished copy of this home', ways['w1']['why'])

    def test_an_incomplete_copy_is_never_used_and_is_named(self):
        self.take_copy()
        os.unlink(os.path.join(self.copy, coldcopy.MANIFEST))
        self.switch()
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn('incomplete', ways['w1']['why'])
        self.assertIn(self.copy, ways['w1']['why'])

    def test_a_copy_of_another_build_whose_t3_is_not_kept_is_refused(self):
        self.take_copy()
        self.switch()
        os.unlink(self.m.path(f'home/runtime/versions/{fx.LIVE}/.install-complete'))
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn(f'the t3 {fx.LIVE} it was taken under is not kept', ways['w1']['why'])

    def test_a_copy_taken_under_the_build_running_now_is_no_way_back(self):
        self.take_copy()
        self.switch()
        path = os.path.join(self.copy, coldcopy.MANIFEST)  # as if taken under the fork build
        with open(path) as f:
            manifest = json.load(f)
        with open(path, 'w') as f:
            json.dump({**manifest, 'liveVersion': fx.TARGET}, f)
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn('not older than', ways['w1']['why'])

    def test_w2_needs_a_fork_build_live_and_its_base_nightly_staged_or_installed(self):
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w2']['ok'])
        self.assertIn('not a fork build', ways['w2']['why'])
        self.switch()
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w2']['ok'])
        self.assertIn(f'{bx.BASE} is not available', ways['w2']['why'])
        self.m.stage_release(bx.BASE)
        ways = back.ways(config.load(), self.adapter)
        self.assertTrue(ways['w2']['ok'], ways['w2'])
        self.assertEqual(ways['w2']['version'], bx.BASE)

    def test_w3_needs_an_upstream_stable_newer_than_the_base(self):
        self.switch()
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w3']['ok'])
        self.assertIn(f'no upstream stable newer than {bx.BASE}', ways['w3']['why'])
        self.m.stage_release('0.1.9')  # stable, but older than the base: not W3
        self.assertFalse(back.ways(config.load(), self.adapter)['w3']['ok'])
        self.m.stage_release(bx.STABLE)
        ways = back.ways(config.load(), self.adapter)
        self.assertTrue(ways['w3']['ok'], ways['w3'])
        self.assertEqual(ways['w3']['version'], bx.STABLE)

    def test_back_with_no_way_lists_the_three_and_changes_nothing(self):
        self.take_copy()
        self.switch()
        before = self.m.snapshot()
        code, out, err = self.main('back')
        self.assertEqual(code, 0, out + err)
        for way in ('W1', 'W2', 'W3'):
            self.assertIn(way, out)
        self.assertIn(f'--lose-work-since {self.at}', out)
        self.assertEqual(self.m.snapshot(), before)
        self.assertNotIn('stop', self.calls()[-1:])


class W1(BackBase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        self.switch()

    def test_by_hand_it_needs_the_copy_s_time_typed(self):
        before, calls = self.m.snapshot(), self.calls()
        with self.assertRaisesRegex(journal.Refused, f'--lose-work-since {self.at}'):
            self.back('w1')
        with self.assertRaisesRegex(journal.Refused, 'does not match the copy'):
            self.back('w1', lose_work_since='20200101T000000Z')
        self.assertEqual(self.m.snapshot(), before)
        self.assertEqual(self.calls(), calls)
        self.assertEqual(self.last_run()['end']['result'], 'refused')

    def test_restores_the_copy_keeps_the_migrated_data_and_goes_back_to_the_old_build(self):
        self.m.set_report(fx.report(version=fx.TARGET, database={'counts': {
            'projects': 3, 'projectsArchived': 0, 'threads': 43, 'threadsArchived': 2}}))
        result = self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.db(), self.copied)
        self.assertIn(self.migrated, self.kept())  # moved aside, never deleted
        self.assertEqual(self.live_version(), fx.LIVE)
        self.assertEqual(result['version'], fx.LIVE)
        update = self.updates()[-1]
        self.assertEqual(update[:4], ['update', fx.LIVE, '--allow-downgrade', '--yes'])
        steps = [(s['step'], s['data']) for s in self.last_run()['steps'] if s['phase'] == 'started']
        self.assertEqual([s for s, _ in steps], ['plan', 'stop-service', 'restore', 'update', 'verify'])
        self.assertEqual(steps[3][1]['t3'], self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))
        self.assertEqual(self.calls()[-3:], ['disable', 'stop', 'start'])  # off at boot before the data moves
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertTrue(any('threads: 40 at the copy, 43 now' in line for line in self.said), self.said)
        self.assertEqual(self.last_run()['end']['result'], 'ok')

    def test_run_twice_the_second_sees_the_old_build_verified_and_does_nothing(self):
        self.back('w1', lose_work_since=self.at)
        snap, calls, updates = self.m.snapshot(), self.calls(), self.updates()
        result = self.back('w1', lose_work_since=self.at)
        self.assertTrue(result['nothing'])
        self.assertEqual(self.updates(), updates)
        self.assertNotIn('stop', self.calls()[len(calls):])
        self.assertEqual(self.m.snapshot(), snap)

    def test_automatic_inside_a_run_needs_no_time_typed(self):
        with journal.run(self.m.state, 'switch') as r:
            back.restore_and_downgrade(r, config.load(), self.adapter, self.copy, automatic=True,
                                       say=self.said.append)
        self.assertEqual(self.db(), self.copied)
        self.assertEqual(self.live_version(), fx.LIVE)

    def test_a_failed_downgrade_needs_settling_but_a_rerun_of_back_finishes_it(self):
        self.m.write('update.code', '3')
        with self.assertRaisesRegex(RuntimeError, 'exited 3'):
            self.back('w1', lose_work_since=self.at)
        self.assertTrue(journal.needs_settling(self.last_run()))
        with self.assertRaises(journal.Unfinished):  # anything else waits for settle
            coldcopy.run_copy(config.load(), self.adapter, owner_says_go=True, say=self.said.append)
        os.unlink(self.m.path('update.code'))
        with self.assertRaisesRegex(journal.Refused, 'stopped half-way: run `t3-golive back --way w1'):
            self.back('w2')
        self.assertTrue(journal.needs_settling(self.last_run()))  # a refused follow-up does not hide it
        self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.db(), self.copied)
        self.assertEqual(self.live_version(), fx.LIVE)
        self.assertEqual(self.last_run()['end']['result'], 'ok')

    def test_the_guard_timer_firing_during_a_manual_back_is_refused_by_the_lock(self):
        seen = []

        def guard_fires(*args):
            try:
                back.run_back(config.load(), self.adapter, 'w1', automatic=True, say=self.said.append)
            except journal.Busy as e:
                seen.append(str(e))
        self.adapter.on['stop'] = guard_fires
        self.back('w1', lose_work_since=self.at)
        self.assertEqual(len(seen), 1)
        self.assertIn('back', seen[0])
        self.assertEqual(len(self.updates()), 1)

    def killed(self, setup):
        script = KILLED.format(paths=[os.path.dirname(os.path.abspath(__file__)), os.path.join(fx.KIT, 'lib'),
                                      fx.COMMON], machine=config.MACHINE_FILE, systemctl=back.osadapter.SYSTEMCTL,
                               cgroup=back.osadapter.CGROUP_FILE, root=self.m.root, setup=setup, since=self.at,
                               stub=self.m.path('bin/t3-stub'))
        p = subprocess.run([sys.executable, '-I', '-B', '-c', script], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, -9, p.stderr)
        self.assertIsNone(self.last_run()['end'])

    def assert_rerun_finishes(self):
        self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.db(), self.copied)
        self.assertIn(self.migrated, self.kept())
        self.assertEqual(self.live_version(), fx.LIVE)
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertEqual(self.last_run()['end']['result'], 'ok')

    def test_stopped_after_the_stop_then_run_again(self):
        self.killed("on['stop'] = kill")
        self.assertFalse(self.adapter.service()['enabled'])  # T3 does not start at boot on a half-way home
        self.assert_rerun_finishes()
        self.assertTrue(any('counts since the copy: cannot tell (T3 is not running steadily' in s for s in self.said))

    def test_stopped_after_the_move_aside_then_run_again(self):
        self.killed('real = coldcopy._write_mark\n'
                    'def mark(*a):\n    real(*a)\n    kill()\ncoldcopy._write_mark = mark')
        self.assert_rerun_finishes()

    def test_stopped_mid_copy_then_run_again(self):
        self.killed('real = coldcopy._copy_file\n'
                    'def copy(src, dst, *a):\n    open(dst, "wb").write(b"half")\n    kill()\n'
                    'coldcopy._copy_file = copy')
        self.assert_rerun_finishes()

    def test_stopped_mid_downgrade_then_run_again(self):
        self.m.write('kill-after-update', '')
        self.killed('')
        os.unlink(self.m.path('kill-after-update'))
        self.assert_rerun_finishes()


class W2W3(BackBase):
    def setUp(self):
        super().setUp()
        self.switch()

    def test_w2_runs_the_base_nightly_on_the_data_as_it_is(self):
        self.m.stage_release(bx.BASE)
        self.back('w2')
        self.assertEqual(self.db(), self.migrated)  # loses nothing
        self.assertEqual(self.kept(), set())
        self.assertEqual(self.live_version(), bx.BASE)
        self.assertEqual(self.updates()[-1][:4], ['update', bx.BASE, '--allow-downgrade', '--yes'])
        t3 = [s['data']['t3'] for s in self.last_run()['steps'] if s['step'] == 'update' and s['phase'] == 'started'][0]
        self.assertEqual(t3, self.m.path(f'stage/{bx.BASE}/t3'))
        self.assertTrue(self.back('w2')['nothing'])  # twice

    def test_verify_needs_the_service_enabled_again(self):
        self.m.stage_release(bx.BASE)
        self.adapter.disable()
        self.m.write('update-leaves-disabled', '')
        with self.assertRaisesRegex(RuntimeError, 'not enabled'):
            self.back('w2')

    def test_w2_with_no_base_nightly_is_refused_and_changes_nothing(self):
        before = self.m.snapshot()
        with self.assertRaisesRegex(journal.Refused, f'w2 is not possible now: t3 {bx.BASE} is not available'):
            self.back('w2')
        self.assertEqual(self.m.snapshot(), before)
        self.assertEqual(self.updates(), [])

    def test_w3_goes_to_the_newer_stable(self):
        self.m.stage_release(bx.STABLE)
        self.back('w3')
        self.assertEqual(self.live_version(), bx.STABLE)
        self.assertEqual(self.db(), self.migrated)


class Status(BackBase):
    def setUp(self):
        super().setUp()
        self.take_copy()
        self.switch()

    def live_report(self, counts=None, **over):
        """The live fork build's report: every migration applied."""
        database = {'pendingMigrations': [], **({'counts': counts} if counts else {})}
        self.m.set_report(fx.report(version=fx.TARGET, database=database, **over))

    def status(self):
        code, out, err = self.main('status')
        return code, out + err

    def test_up_names_version_features_providers_and_copies_and_writes_nothing(self):
        self.live_report(fork={'featuresOn': ['agentSlots']}, settings={
            'providerInstances': [{'id': 'codex', 'driver': 'codex', 'enabled': True}]})
        before = self.m.snapshot()
        code, out = self.status()
        self.assertEqual(code, 0, out)
        self.assertIn(f'T3 is up: t3 {fx.TARGET}', out)
        self.assertNotIn('FAIL', out)
        self.assertIn('fork features on: agentSlots', out)
        self.assertIn('codex (codex, on)', out)
        self.assertIn(self.copy, out)
        self.assertEqual(self.m.snapshot(), before)

    def test_down_is_a_clear_answer_and_no_report_runs(self):
        self.adapter.stop()
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('T3 is DOWN', out)
        self.assertEqual([c for c in self.m.calls('back-t3-calls.jsonl') if c[0] == '__fork-report'], [])

    def test_mid_restart_is_a_clear_answer_never_ok(self):
        self.m.service(**{**json.load(open(self.m.path('service.json'))), 'ActiveState': 'activating'})
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('T3 is restarting', out)
        self.m.service(**{**json.load(open(self.m.path('service.json'))), 'ActiveState': 'active'})
        self.m.write('home/runtime/.restart-pending', '')
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('T3 is restarting', out)

    def test_a_golive_run_holding_the_lock_reads_as_restarting(self):
        with journal.lock(self.m.state, 'back', exclusive=True):
            code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('T3 is restarting', out)

    def test_no_http_answer_fails(self):
        self.m.runtime_file(pid=self.m.server.pid, serverVersion=fx.TARGET, port=bx.closed_port())
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('FAIL  http', out)

    def test_counts_below_the_pre_switch_lists_fail(self):
        self.live_report({'projects': 3, 'projectsArchived': 0, 'threads': 39, 'threadsArchived': 2})
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('FAIL  counts: threads: 39 now, 40 before', out)

    def test_pending_migrations_fail(self):
        self.m.set_report(fx.report(version=fx.TARGET))  # the fixture's report has 82 and 83 pending
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('FAIL  migrations: 0 applied; pending: [82, 83]', out)

    def test_another_version_than_the_last_verified_fails(self):
        self.m.install('0.1.11')
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': '0.1.11'}))
        self.m.spawn_server('0.1.11')
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn(f'FAIL  live server: t3 0.1.11, pid {self.m.server.pid}, port {self.m.port}; '
                      f'expected t3 {fx.TARGET}', out)
        self.assertIn(f'FAIL  service state: names t3 0.1.11, expected t3 {fx.TARGET}', out)

    def test_a_launcher_pointing_at_another_build_fails(self):
        link = self.m.path('bin-user/t3')
        os.makedirs(os.path.dirname(link))
        os.symlink(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'), link)
        self.m.write('home/runtime/launchers', f'\n{link}\n')
        code, out = self.status()
        self.assertIn(f'FAIL  t3 link: {link} -> t3 {fx.LIVE}', out)

    def test_a_crash_inside_status_is_a_failure_not_a_crash(self):
        with mock.patch.object(back.live, 'live_server', side_effect=ValueError('boom')):
            code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('boom', out)


if __name__ == '__main__':
    unittest.main()
