"""t3-golive check on a fake machine (tests/golive_fixture.py): ready, nothing to do, every refusal, and that it
writes nothing to the T3 home, never stops or starts the service and runs the staged build with an empty HOME and a
bare PATH. No root, no systemd, no real T3.  python3 -I -B fork-kit/golive/tests/check.test.py"""
import io, json, os, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import check, config, journal  # noqa: E402


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

    def test_no_database_and_nothing_ever_ran_is_nothing_to_protect(self):
        self.m.config()
        os.makedirs(self.m.path('home/userdata'))
        self.m.service(LoadState='not-found', ActiveState='inactive', UnitFileState='', FragmentPath='', MainPID=0)
        code, result, _ = self.check_json()
        self.assertEqual((code, result['ok'], result['nothing']), (0, True, 'no-database'), result)
        self.assertTrue(all(c['ok'] for c in result['checks']), result)
        self.assertIn('use the normal installer', ' '.join(result['info']))


class NothingToDoNeverHidesARefusal(CheckBase):
    """"Nothing to do" (exit 0) only when no check failed: a half switch, a half restore, a stopped service, a
    dead server or a run to settle always refuse, and the refusal names what was found."""

    def refused(self, *names):
        before = self.m.snapshot()
        code, result, err = self.check_json()
        self.assertEqual(code, 1, (err, result))
        self.assertFalse(result['ok'])
        self.assertNotIn('nothing', result)
        self.assertNotIn('plan', result)
        failed = self.failed(result)
        for name in names:
            self.assertIn(name, failed, failed)
        self.assertEqual(self.m.snapshot(), before)
        return failed

    def kill_a_run_half_way(self, command='switch', step='update'):
        code = KILLED.format(paths=[os.path.join(fx.KIT, 'lib'), fx.COMMON], state=self.m.state, command=command,
                             step=step)
        subprocess.run([sys.executable, '-I', '-B', '-c', code], stdin=subprocess.DEVNULL, timeout=60)
        self.assertIsNotNone(journal.unfinished(self.m.state))

    def test_switch_killed_after_the_update_with_the_service_failed(self):
        # upstream's update wrote activeVersion = target; the new server never came up and the unit failed.
        self.m.standard()
        self.kill_a_run_half_way()
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': fx.TARGET}))
        fx.kill(self.m.server)
        self.m.server = None
        self.m.unit(ActiveState='failed', MainPID=0)
        failed = self.refused('journal', 'boot service', 'live server', 'no half switch')
        self.assertIn('step "update" started', failed['journal'])
        self.assertIn('settle', failed['journal'])

    def test_half_switch_without_a_journal_is_named(self):
        # The same half switch, the run already settled (or done by hand): still refused, and named.
        self.m.standard()
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': fx.TARGET}))
        os.remove(self.m.path('home/userdata/server-runtime.json'))
        failed = self.refused('live server', 'no half switch')
        self.assertIn(f'says {fx.TARGET} is active but no server of it runs', failed['no half switch'])

    def test_on_the_target_but_a_check_fails_is_refused(self):
        self.m.standard(version=fx.TARGET)
        self.m.write('home/runtime/.restart-pending', '')
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        self.assertIn('no markers', self.failed(result))
        self.assertIn(f'already runs {fx.TARGET}', ' '.join(result['info']))
        os.remove(self.m.path('home/runtime/.restart-pending'))
        self.kill_a_run_half_way()
        self.refused('journal')

    def test_restore_killed_with_the_database_moved_aside(self):
        self.m.standard()
        self.kill_a_run_half_way(command='back', step='restore')
        os.rename(self.m.path('home/userdata/statev2.sqlite'), self.m.path('home/userdata/statev2.sqlite.aside'))
        failed = self.refused('journal', 'database')
        self.assertIn('statev2.sqlite.aside', failed['database'])
        self.assertIn('restore may have stopped half-way', failed['database'])
        self.assertIn('t3code.service is installed', failed['database'])

    def test_live_boot_service_with_no_database(self):
        self.m.standard()
        os.remove(self.m.path('home/userdata/statev2.sqlite'))
        failed = self.refused('database')
        self.assertIn('server-runtime.json', failed['database'])
        self.assertNotIn('installer', json.dumps(self.check_json()[1]))

    def test_home_setting_names_the_wrong_existing_folder(self):
        self.m.standard()
        for wrong in ('wrong-empty', 'wrong-with-userdata'):
            with self.subTest(wrong):
                os.makedirs(self.m.path(wrong + ('/userdata' if 'userdata' in wrong else '')))
                self.m.config(home=self.m.path(wrong))
                failed = self.refused('boot service', 'database')
                self.assertIn(f'serves {self.m.home!r}', failed['boot service'])

    def test_a_folder_that_is_not_a_t3_home(self):
        self.m.config()
        os.makedirs(self.m.home)
        self.m.service(LoadState='not-found', ActiveState='inactive', UnitFileState='', FragmentPath='', MainPID=0)
        self.assertIn('not a T3 home', self.refused('T3 home')['T3 home'])

    def test_no_database_with_a_run_to_settle(self):
        self.m.config()
        os.makedirs(self.m.path('home/userdata'))
        self.m.service(LoadState='not-found', ActiveState='inactive', UnitFileState='', FragmentPath='', MainPID=0)
        self.kill_a_run_half_way()
        self.refused('journal')


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

    def test_unit_home_must_be_absolute_and_the_same_folder(self):
        # A relative home is read from the service's folder, not the kit's: refused even when it would resolve to
        # this home from where check runs. A link to the home is the same folder and passes.
        self.m.standard()
        cwd = os.getcwd()
        self.addCleanup(os.chdir, cwd)
        os.chdir(self.m.root)
        self.m.unit(home='home')
        self.refused('boot service', "'home'")
        os.symlink(self.m.home, self.m.path('home-link'))
        self.m.unit(home=self.m.path('home-link'))
        code, result, _ = self.check_json()
        self.assertEqual(code, 0, self.failed(result))

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

    def test_runtime_file_disagrees_with_the_process(self):
        self.m.standard()
        self.m.runtime_file(pid=self.m.server.pid, serverVersion='0.1.8')
        self.refused('live server', 'says version 0.1.8 but the process runs 0.1.9')

    def test_server_binary_deleted_on_disk(self):
        self.m.standard()
        os.remove(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))
        self.refused('live server', 'deleted or replaced')

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

    def test_staged_t3_is_not_executable(self):
        self.m.standard()
        os.chmod(self.m.path(f'stage/{fx.TARGET}/t3'), 0o644)
        self.refused('target staged', 'is not an executable')

    def test_staged_runtime_holds_a_link(self):
        self.m.standard()
        os.makedirs(self.m.path('elsewhere'))
        os.symlink(self.m.path('elsewhere'), self.m.path(f'stage/{fx.TARGET}/linked'))
        self.refused('target staged', 'holds links')

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


    def test_a_service_manager_error_still_prints_json(self):
        self.m.standard()
        self.m.write('bin/systemctl', '#!/bin/sh\necho no bus >&2\nexit 1\n', mode=0o755)
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        self.assertIn('no bus', self.failed(result)['boot service'])


class CannotRun(CheckBase):
    def test_no_machine_file_prints_a_template(self):
        code, _, err = self.check()
        self.assertEqual(code, 2)
        self.assertIn('not there', err)
        self.assertIn('"golive": {', err)
        self.assertIn('"stateDir"', err)

    def test_kit_folders_inside_the_t3_home_are_refused(self):
        # check promises to write nothing in the home: its lock (stateDir) must not be there, nor the backups or
        # the staged build. Nothing is created.
        self.m.standard()
        for key in ('stateDir', 'backupDir', 'stageDir'):
            for inside in ('home', 'home/kit'):
                with self.subTest(key=key, inside=inside):
                    self.m.config(**{key: self.m.path(inside)})
                    before = self.m.snapshot()
                    code, _, err = self.check()
                    self.assertEqual(code, 2, err)
                    self.assertIn(f'golive.{key}', err)
                    self.assertIn('inside it', err)
                    self.assertEqual(self.m.snapshot(), before, 'check wrote into the T3 home')
        os.symlink(self.m.home, self.m.path('home-link'))
        self.m.config(stateDir=self.m.path('home-link/kit'))
        self.assertEqual(self.check()[0], 2)

    def test_the_example_machine_file_section_is_accepted(self):
        with open(os.path.join(fx.COMMON, 'machine.example.json')) as f:
            section = json.load(f)['golive']
        cfg = config.Config(section)
        self.assertEqual((cfg.home, cfg.free_floor_mb), (section['home'], section['freeFloorMb']))
        self.assertEqual(set(section), set(config.SPEC))

    def test_the_state_folder_comes_from_the_machine_file_only(self):
        # A detached run (a transient user unit) and a shell see different environments; both must use the same
        # lock and journal, so no environment variable moves them.
        self.m.standard()
        self.m.config(stateDir=None)
        code, _, err = self.check()
        self.assertEqual(code, 2)
        self.assertIn('"stateDir" is missing', err)
        self.m.config()
        for xdg in ('a', 'b'):
            with mock.patch.dict(os.environ, {'XDG_STATE_HOME': self.m.path(xdg), 'HOME': self.m.path(xdg)}):
                self.assertEqual(config.load().state_dir, self.m.state)
                self.assertEqual(self.check()[0], 0)
            self.assertFalse(os.path.exists(self.m.path(xdg)))

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
                self.assertIn(self.m.path('etc/machine.json'), err)

    def test_machine_file_must_be_root_owned_and_not_writable_by_others(self):
        self.m.standard()
        os.chmod(self.m.path('etc/machine.json'), 0o664)
        code, _, err = self.check()
        self.assertEqual((code, 'writable by group' in err), (2, True))
        os.chmod(self.m.path('etc/machine.json'), 0o644)
        fx.config.MACHINE_OWNER = os.getuid() + 1
        code, _, err = self.check()
        self.assertEqual((code, 'owned by uid' in err), (2, True))

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


KILLED = '''import os, sys
sys.path[:0] = {paths!r}
from golive import journal
with journal.run({state!r}, {command!r}) as r:
    with r.step({step!r}):
        os.kill(os.getpid(), 9)
'''

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

    def test_a_run_that_failed_before_its_first_step_is_a_note(self):
        self.m.standard()
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'copy'):
                raise RuntimeError('disk full')
        code, result, _ = self.check_json()
        self.assertEqual(code, 0)
        self.assertIn('ended failed before its first step: RuntimeError: disk full', ' '.join(result['info']))

    def test_a_run_that_failed_after_a_step_is_refused_until_settled(self):
        self.m.standard()
        with self.assertRaises(OSError):
            with journal.run(self.m.state, 'copy') as r:
                with r.step('stop-service'):
                    raise OSError('no bus')
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        detail = self.failed(result)['journal']
        self.assertIn('ended failed after step "stop-service" started', detail)
        self.assertIn('t3-golive settle --reason', detail)
        code, out, err = self.check_settle('started the service again by hand')
        self.assertEqual(code, 0, err)
        self.assertIn('settled: the last run (copy', out)
        self.assertEqual(self.check()[0], 0)
        self.assertIn('nothing to settle', self.check_settle('again')[1])

    def check_settle(self, reason):
        out, err = io.StringIO(), io.StringIO()
        code = check.main(['settle', '--reason', reason], out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_a_damaged_journal_names_the_file_and_how_to_settle(self):
        self.m.standard()
        for _ in range(2):
            with journal.run(self.m.state, 'copy'):
                pass
        path = os.path.join(self.m.state, 'journal.jsonl')
        lines = open(path, 'rb').read().split(b'\n')
        with open(path, 'wb') as f:
            f.write(b'\n'.join([lines[0].replace(b'copy', b'kopy')] + lines[1:]))
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        detail = self.failed(result)['journal']
        self.assertIn(path, detail)
        self.assertIn('t3-golive settle', detail)
        code, out, err = self.check_settle('read it; the home is on the old build')
        self.assertEqual(code, 0, err)
        self.assertIn('moved the damaged journal aside to ' + path + '.damaged-', out)
        self.assertFalse(os.path.exists(path))
        self.assertEqual(self.check()[0], 0)

    def test_a_journal_from_a_newer_kit_says_update_the_kit(self):
        self.m.standard()
        path = os.path.join(self.m.state, 'journal.jsonl')
        os.makedirs(self.m.state)
        self.m.write(path, json.dumps({'v': 2, 'whatever': 'new'}) + '\n')
        code, result, _ = self.check_json()
        self.assertEqual(code, 1)
        detail = self.failed(result)['journal']
        self.assertIn('newer go-live kit', detail)
        self.assertIn('update the kit', detail)
        self.assertNotIn('cannot be trusted', detail)
        code, _, err = self.check_settle('x')
        self.assertEqual(code, 1)
        self.assertIn('update the kit', err)
        self.assertTrue(os.path.exists(path), 'settle moved a newer kit\'s journal')

    def test_switch_can_run_check_under_its_own_run(self):
        # The later switch holds its own exclusive run and must pass check inside it: no gap between the two.
        self.m.standard()
        cfg = config.load()
        adapter = fx.osadapter.SystemdUser()
        with journal.run(self.m.state, 'switch') as r:
            with r.step('check'):
                code, result = check.run_check(cfg, adapter, own_run=r)
                self.assertEqual(code, 0, self.failed(result))
                # Without its run it is Busy like anyone else, and a run that does not hold the lock is refused.
                code, result = check.run_check(cfg, adapter)
                self.assertIn('one run at a time', self.failed(result))
                with self.assertRaises(ValueError):
                    check.run_check(cfg, adapter, own_run=journal.Run(self.m.state, 'switch'))
        with self.assertRaises(ValueError):
            check.run_check(cfg, adapter, own_run=r)  # the run has ended: it holds no lock now
        os.makedirs(self.m.path('other-state'))
        with journal.run(self.m.path('other-state'), 'switch') as other:
            with self.assertRaises(ValueError):
                check.run_check(cfg, adapter, own_run=other)

    def test_own_run_still_sees_an_earlier_run_to_settle(self):
        self.m.standard()
        with self.assertRaises(OSError):
            with journal.run(self.m.state, 'copy') as r:
                with r.step('stop-service'):
                    raise OSError('x')
        # journal.run refuses first; check under a run is never a way past an unsettled run.
        with self.assertRaises(journal.Unfinished):
            with journal.run(self.m.state, 'switch'):
                pass

    def test_two_checks_may_run_together(self):
        self.m.standard()
        with journal.lock(self.m.state, 'check', exclusive=False):
            self.assertEqual(self.check()[0], 0)


if __name__ == '__main__':
    unittest.main()
