"""t3-golive check on a fake machine (tests/golive_fixture.py): ready, nothing to do, every refusal, and that it
writes nothing to the T3 home, never stops or starts the service and runs the staged build with an empty HOME and a
bare PATH. No root, no systemd, no real T3.  python3 -I -B fork-kit/golive/tests/check.test.py"""
import io, json, os, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import check, journal  # noqa: E402


class CheckBase(unittest.TestCase):
    def setUp(self):
        self.m = fx.Machine()
        self.addCleanup(self.m.close)

    def check(self, *args):
        out, err = io.StringIO(), io.StringIO()
        code = check.main(['check', *args], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def check_json(self):
        code, out, err = self.check('--json')
        result = json.loads(out) if out else None
        return code, result, err

    def failed(self, result):
        return {c['name']: c['detail'] for c in result['checks'] if not c['ok']}


class Ready(CheckBase):
    def test_ready_prints_the_plan_and_touches_nothing(self):
        self.m.standard()
        before = self.m.snapshot()
        code, out, err = self.check()
        self.assertEqual(code, 0, out + err)
        self.assertIn('Ready. Plan:', out)
        self.assertIn(f'{fx.LIVE} -> {fx.TARGET}', out)
        self.assertIn('threads now: 40', out)
        self.assertNotIn('FAIL', out)
        self.assertEqual(self.m.snapshot(), before, 'check changed the T3 home')
        # The service manager was only asked, never told; nothing was started detached.
        self.assertEqual([c[:2] for c in self.m.calls('systemctl-calls.jsonl')], [['--user', 'show']])
        self.assertEqual(self.m.calls('systemd-run-calls.jsonl'), [])
        # The staged build ran the report with the home named, an empty HOME and a bare PATH.
        [call] = self.m.calls('stub-t3-calls.jsonl')
        self.assertEqual(call['argv'], ['__fork-report', '--base-dir', self.m.home])
        self.assertEqual(call['env']['PATH'], '/usr/bin:/bin')
        self.assertNotEqual(call['env']['HOME'], os.environ.get('HOME'))
        self.assertEqual(call['cwd'], call['env']['HOME'])
        self.assertFalse(os.path.exists(call['env']['HOME']), 'the empty HOME was not removed')

    def test_json_names_the_live_server(self):
        self.m.standard()
        code, result, _ = self.check_json()
        self.assertEqual(code, 0)
        self.assertTrue(result['ok'])
        self.assertEqual(result['live']['version'], fx.LIVE)
        self.assertEqual(result['live']['pid'], self.m.server.pid)
        self.assertEqual(result['live']['binary'], os.path.join(self.m.home, 'runtime/versions', fx.LIVE, 't3'))
        self.assertEqual(result['pendingMigrations'], [82, 83])

    def test_log_lines_before_the_report_are_tolerated(self):
        self.m.standard()
        self.m.set_report(fx.report(), prefix='INFO something {not json}\n')
        self.assertEqual(self.check()[0], 0)

    def test_already_on_the_target_is_nothing_to_do(self):
        self.m.standard(version=fx.TARGET)
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn('already runs', out)
        self.assertEqual(self.m.calls('stub-t3-calls.jsonl'), [])

    def test_no_database_is_nothing_to_protect(self):
        self.m.standard()
        os.remove(os.path.join(self.m.home, 'userdata/statev2.sqlite'))
        code, out, _ = self.check()
        self.assertEqual(code, 0)
        self.assertIn('use the normal installer', out)


class Refusals(CheckBase):
    def refused(self, name, contains=''):
        before = self.m.snapshot()
        code, result, err = self.check_json()
        self.assertEqual(code, 1, err)
        self.assertFalse(result['ok'])
        failed = self.failed(result)
        self.assertIn(name, failed, failed)
        self.assertIn(contains, failed[name])
        self.assertNotIn('plan', result)
        self.assertEqual(self.m.snapshot(), before)
        self.assertTrue(all(c[1] == 'show' for c in self.m.calls('systemctl-calls.jsonl')))
        return failed

    def test_not_a_boot_service(self):
        self.m.standard()
        self.m.service(LoadState='not-found', ActiveState='inactive', UnitFileState='', FragmentPath='', MainPID=0)
        self.refused('boot service', 'not run as a boot service')
        self.refused('live server', 'not started by the boot service')

    def test_service_serves_another_home(self):
        self.m.standard()
        os.makedirs(self.m.path('other'))
        self.m.unit(home=self.m.path('other'))
        self.refused('boot service', 'other')

    def test_service_stopped_or_disabled(self):
        self.m.standard()
        self.m.unit(UnitFileState='disabled')
        self.refused('boot service', 'enabled: False')

    def test_server_not_started_by_the_service(self):
        self.m.standard()
        self.m.unit(MainPID=1)
        self.refused('live server', 'not started by the boot service')

    def test_no_running_server(self):
        self.m.standard()
        os.remove(os.path.join(self.m.home, 'userdata/server-runtime.json'))
        self.refused('live server', 'no server is running')

    def test_runtime_file_names_a_dead_pid(self):
        self.m.standard()
        fx.kill(self.m.server)
        self.refused('live server', 'is not running')

    def test_runtime_file_names_another_program(self):
        self.m.standard()
        self.m.runtime_file(pid=os.getpid())
        self.refused('live server', 'not a runtime of this home')

    def test_pending_update(self):
        self.m.standard()
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': fx.LIVE, 'update': {
            'id': 'u', 'fromVersion': fx.LIVE, 'targetVersion': '0.1.10', 'dbPath': '/x', 'status': 'pending'}}))
        self.refused('no update in flight', 'pending')

    def test_bad_or_missing_service_state(self):
        self.m.standard()
        self.m.write('home/runtime/service-state.json', '[]')
        self.refused('no update in flight', 'not a service state')

    def test_markers(self):
        self.m.standard()
        for marker in ('home/runtime/.restart-pending', 'home/runtime/.service-stopping',
                       'home/runtime/db-backup/abc/.restore-pending'):
            with self.subTest(marker=marker):
                self.m.write(marker, '')
                try:
                    self.refused('no markers', os.path.basename(marker))
                finally:
                    os.remove(self.m.path(marker))
        self.assertEqual(self.check()[0], 0)

    def test_target_older_than_live(self):
        self.m.standard(target='0.1.8')
        self.refused('target is newer', '0.1.9 -> 0.1.8')

    def test_staged_archive_does_not_match_its_checksum(self):
        self.m.standard()
        line = open(self.m.path('stage/SHA256SUMS')).read()
        self.m.write('stage/SHA256SUMS', '0' * 64 + line[64:])
        self.refused('target staged', 'does not match its SHA256SUMS line')

    def test_staged_runtime_differs_from_the_archive(self):
        self.m.standard()
        self.m.write(f'stage/{fx.TARGET}/lib/runtime.txt', 'changed')
        self.refused('target staged', 'differs')

    def test_staged_runtime_has_an_extra_file(self):
        self.m.standard()
        self.m.write(f'stage/{fx.TARGET}/extra.js', 'x')
        self.refused('target staged', 'extra.js')

    def test_staged_runtime_not_extracted(self):
        self.m.standard()
        os.remove(self.m.path(f'stage/{fx.TARGET}/t3'))
        self.refused('target staged', 'is missing')

    def test_no_checksums(self):
        self.m.standard()
        os.remove(self.m.path('stage/SHA256SUMS'))
        self.refused('target staged', 'SHA256SUMS')

    def test_staged_build_is_another_version(self):
        self.m.standard()
        self.m.set_report(fx.report(version='0.1.10-nightly.20261114.4682'))
        self.refused('report: target build')

    def test_fork_feature_switched_on(self):
        self.m.standard()
        self.m.set_report(fx.report(fork={'featuresOn': ['waitFor']}))
        self.refused('fork features off', 'waitFor')

    def test_report_not_ok_names_its_problems(self):
        self.m.standard()
        self.m.set_report(fx.report(ok=False, problems=[{'code': 'settings-unreadable', 'message': 'bad json'}]))
        self.refused('report: home readable', 'settings-unreadable: bad json')

    def test_report_of_another_version_or_none(self):
        self.m.standard()
        for value in (fx.report(reportVersion=2), 'no json here', '{"half": '):
            with self.subTest(value=str(value)[:20]):
                self.m.set_report(value)
                self.refused('report')

    def test_not_enough_free_disk(self):
        self.m.standard()
        self.m.config(freeFloorMb=1 << 40)
        self.refused('free disk', 'need')

    def free_disk(self, free, backups_elsewhere=False):
        """Pretend each folder has `free(path)` bytes free; optionally put the backups on another disk."""
        real_stat, real_statvfs = os.stat, os.statvfs

        def statvfs(path):
            st = real_statvfs(path)
            return os.statvfs_result((1, 1, st.f_blocks, free(path), free(path)) + tuple(st)[5:])

        def stat(path, *a, **k):
            st = real_stat(path, *a, **k)
            if backups_elsewhere and path == self.m.backups:
                fields = list(st)
                fields[2] = st.st_dev + 1
                return os.stat_result(fields)
            return st
        return mock.patch.multiple(check.os, statvfs=statvfs, stat=stat)

    def test_free_disk_is_twice_the_database_plus_floor(self):
        self.m.standard()
        self.m.config(freeFloorMb=0)
        db = 4096 + 16 + 1024  # the fixture's database with its WAL
        with self.free_disk(lambda path: 2 * db - 1):
            self.refused('free disk', 'need')
        with self.free_disk(lambda path: 2 * db):
            self.assertEqual(self.check()[0], 0)

    def test_backups_on_another_disk_need_the_database_on_each(self):
        self.m.standard()
        self.m.config(freeFloorMb=0)
        db = 4096 + 16 + 1024
        with self.free_disk(lambda path: db, backups_elsewhere=True):
            code, result, _ = self.check_json()
        self.assertEqual(code, 0, self.failed(result))
        self.assertEqual({c['name'] for c in result['checks'] if 'disk' in c['name']},
                         {'free disk (home)', 'free disk (backups)'})
        with self.free_disk(lambda path: db - 1 if path == self.m.backups else 10 * db, backups_elsewhere=True):
            self.refused('free disk (backups)', 'need')

    def test_every_reason_is_listed_at_once(self):
        self.m.standard(target='0.1.8')
        self.m.write('home/runtime/.restart-pending', '')
        os.remove(self.m.path('stage/SHA256SUMS'))
        failed = self.refused('no markers')
        self.assertTrue({'target is newer', 'target staged'} <= set(failed), failed)

    def test_missing_home(self):
        self.m.config(home=self.m.path('nowhere'))
        self.assertEqual(self.check()[0], 1)
        self.assertFalse(os.path.exists(self.m.path('nowhere')))


class CannotRun(CheckBase):
    def test_no_machine_file_prints_a_template(self):
        code, _, err = self.check()
        self.assertEqual(code, 2)
        self.assertIn('does not exist', err)
        self.assertIn('"golive": {', err)

    def test_machine_file_problems_are_named(self):
        cases = {
            'unknown key': ({'version': 1, 'golive': {'home': '/h', 'targetVersion': '1.0.0', 'stageDir': '/s',
                                                      'backupDir': '/b', 'quietCommand': 'x'}}, 'quietCommand'),
            'missing key': ({'version': 1, 'golive': {'home': '/h'}}, 'missing'),
            'newer version': ({'version': 2, 'golive': {}}, 'version 2'),
            'no section': ({'version': 1, 'heavy': {}}, 'no "golive" section'),
            'relative path': ({'version': 1, 'golive': {'home': 'h', 'targetVersion': '1.0.0', 'stageDir': '/s',
                                                        'backupDir': '/b'}}, 'absolute'),
            'bad version': ({'version': 1, 'golive': {'home': '/h', 'targetVersion': 'latest', 'stageDir': '/s',
                                                      'backupDir': '/b'}}, 'exact version'),
        }
        for label, (data, words) in cases.items():
            with self.subTest(label):
                self.m.write('etc/machine.json', json.dumps(data))
                code, _, err = self.check()
                self.assertEqual(code, 2)
                self.assertIn(words, err)

    def test_machine_file_must_be_root_owned_and_not_writable_by_others(self):
        self.m.standard()
        os.chmod(self.m.path('etc/machine.json'), 0o664)
        code, _, err = self.check()
        self.assertEqual((code, 'writable by group' in err), (2, True))
        os.chmod(self.m.path('etc/machine.json'), 0o644)
        fx.machine_file.ROOT_UID = os.getuid() + 1
        code, _, err = self.check()
        self.assertEqual((code, 'must be owned by root' in err), (2, True))

    def test_the_shipped_command_loads(self):
        p = subprocess.run([os.path.join(fx.KIT, 'bin', 't3-golive'), 'check', '--help'], capture_output=True,
                           text=True, timeout=60, stdin=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin'})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('Read only: may the live T3 move to the target build now?', p.stdout)

    def test_no_service_manager(self):
        self.m.standard()
        fx.osadapter.SYSTEMCTL = self.m.path('bin/none')
        code, _, err = self.check()
        self.assertEqual(code, 2)
        self.assertIn('needs systemd', err)
        self.assertEqual(self.m.calls('stub-t3-calls.jsonl'), [])


HOLD = '''import os, sys, time
sys.path[:0] = {paths!r}
from golive import journal
with journal.run({state!r}, 'copy') as r:
    with r.step('stop-service'):
        pass
    with r.step('cold-copy', database='statev2.sqlite'):
        print('ready', flush=True)
        if {kill}:
            os.kill(os.getpid(), 9)
        time.sleep(60)
'''


class WithJournal(CheckBase):
    def hold(self, kill):
        code = HOLD.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON], state=self.m.state, kill=kill)
        p = subprocess.Popen([sys.executable, '-I', '-B', '-c', code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(fx.kill, p)
        self.assertEqual(p.stdout.readline().strip(), 'ready')
        return p

    def test_check_is_refused_while_a_run_is_active(self):
        self.m.standard()
        p = self.hold(kill=False)
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        self.assertIn(f'copy (pid {p.pid}', self.failed(result)['one run at a time'])

    def test_a_run_killed_half_way_is_reported(self):
        self.m.standard()
        p = self.hold(kill=True)
        p.wait()
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        detail = self.failed(result)['journal']
        self.assertIn('(copy, pid', detail)
        self.assertIn('step "cold-copy" started', detail)

    def test_a_failed_last_run_is_a_note_not_a_refusal(self):
        self.m.standard()
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'copy'):
                raise RuntimeError('disk full')
        code, result, _ = self.check_json()
        self.assertEqual(code, 0)
        self.assertIn('ended failed: RuntimeError: disk full', ' '.join(result['info']))

    def test_two_checks_may_run_together(self):
        self.m.standard()
        with journal.lock(self.m.state, 'check', exclusive=False):
            self.assertEqual(self.check()[0], 0)


if __name__ == '__main__':
    unittest.main()
