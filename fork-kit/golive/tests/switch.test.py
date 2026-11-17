"""t3-golive switch, verify, the automatic way back (W1) and the switch's guard, on a fake machine
(tests/golive_fixture.py): the service manager is faked, the T3 home is a temp folder, `t3` is a stub that acts as
the old build (preflight, downgrade), the fork build (update), a fork build whose server never gets ready, or one
whose server dies mid-verify. A kill is a BaseException raised right after a journal line, with the run's end line
then cut off, as a SIGKILL leaves it. Nothing here stops, starts or reads a real T3.
python3 -I -B fork-kit/golive/tests/switch.test.py"""
import datetime, functools, hashlib, http.server, io, json, os, shutil, sys, threading, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
from golive import check, coldcopy, config, journal, live, switch  # noqa: E402

HOOK = '''#!{python} -I
import json, sys
with open({log!r}, 'a') as f:
    f.write(json.dumps([{name!r}]) + '\\n')
sys.exit(int(open({codes!r}).read()) if __import__('os').path.exists({codes!r}) else 0)
'''
PROVIDERS = [{'id': 'claude-a', 'driver': 'claudeAgent'}, {'id': 'codex', 'driver': 'codex'}]


class Killed(BaseException):
    """A kill: no code of the run runs after it (it is not an Exception)."""


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


class Quiet(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


class SwitchBase(unittest.TestCase):
    mode = 'ok'  # what the fork build does after `update`: ok, never (its server never gets ready), dies

    def setUp(self):
        self.m = fx.Machine()
        self.addCleanup(self.m.close)
        self.http = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.m.runtime_file = functools.partial(fx.Machine.runtime_file, self.m, port=self.http.server_address[1])
        self.m.standard()
        self.before = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS})
        self.after = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS},
                               database={'pendingMigrations': [], 'counts': {'projects': 3, 'projectsArchived': 0,
                                                                              'threads': 41, 'threadsArchived': 2}})
        self.m.set_report(self.before)
        for name in ('quiet', 'pre', 'post'):
            self.m.write(f'bin/{name}', HOOK.format(python=sys.executable, log=self.m.path('fake-adapter-calls.jsonl'),
                                                     name=name, codes=self.m.path(f'{name}.code')), mode=0o755)
        self.configure()
        env = mock.patch.dict(os.environ, {'HOME': self.m.root})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('T3CODE_INSTALL_BIN_DIR', None)
        os.makedirs(self.m.path('.local/bin'))
        self.link = self.m.path('.local/bin/t3')
        os.symlink(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'), self.link)
        self.adapter = fx.FakeAdapter(self.m.root, self.m)
        for target, values in ((coldcopy, dict(QUIET_GAP=0, POLL=0.01, STOP_WAIT=3, START_WAIT=5, GUARD_WAIT=0.3)),
                               (switch, dict(MIN_LIMIT=1, _t3=self.fake_t3))):
            patcher = mock.patch.multiple(target, **values)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.preflight, self.t3_calls, self.said = 'ready', [], []
        self.db_before = sha(self.m.path('home/userdata/statev2.sqlite'))

    def configure(self, **over):
        self.m.config(**{'quietCommand': self.m.path('bin/quiet'), 'preHooks': [self.m.path('bin/pre')],
                         'postHooks': [self.m.path('bin/post')], **over})

    def rehearsed(self, result='ok', **over):
        """A rehearsal's journal record, as switch.passed_rehearsal reads it."""
        data = {'fromVersion': fx.LIVE, 'targetVersion': fx.TARGET, 'migrations': [82, 83], 'seconds': 0, **over}
        try:
            with journal.run(self.m.state, 'rehearse') as r:
                with r.step('result') as facts:
                    facts.update(data)
                if result != 'ok':
                    raise journal.Refused('the rehearsal failed')
        except journal.Refused:
            pass

    # --- the stub t3 (old build, fork build) ---
    def install(self, version):
        with open(shutil.which('sleep', path='/usr/bin:/bin'), 'rb') as f:
            self.m.write(f'home/runtime/versions/{version}/t3', f.read(), mode=0o755)
        self.m.write(f'home/runtime/versions/{version}/.install-complete', version)
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 4, 'activeVersion': version}))

    def fake_t3(self, binary, args, timeout):
        self.t3_calls.append([binary, *args])
        if args[0] == '__service-preflight':
            return 0, json.dumps({'status': self.preflight, 'version': fx.LIVE})
        assert args[0] == 'update' and '--yes' in args and args[args.index('--base-dir') + 1] == self.m.home, args
        version = args[1]
        self.install(version)
        if version == fx.TARGET:  # the fork build migrates at its start
            self.m.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'migrated' * 600)
            self.m.set_report(self.after)
            if self.mode == 'never':
                self.adapter._svc(ActiveState='active', MainPID=os.getpid())
                return 0, ''
        else:
            self.m.set_report(self.before)
        self.m.spawn_server(version)
        self.adapter._svc(ActiveState='active', MainPID=os.getpid())
        return 0, ''

    # --- running ---
    def switch(self, owner_says_go=False):
        return switch.run_switch(config.load(), self.adapter, owner_says_go, say=self.said.append)

    def refused(self, *words):
        with self.assertRaises(journal.Refused) as cm:
            self.switch()
        for w in words:
            self.assertIn(w, str(cm.exception))
        self.assertNotIn('stop', self.calls())
        self.assertEqual(self.last_run()['end']['result'], 'refused')
        return str(cm.exception)

    def calls(self):
        return [c[0] for c in self.m.calls('fake-adapter-calls.jsonl')]

    def started_data(self, name, run=None):
        return [s['data'] for s in (run or self.last_run())['steps'] if (s['step'], s['phase']) == (name, 'started')][0]

    def relink(self, version):
        os.remove(self.link)
        os.symlink(self.m.path(f'home/runtime/versions/{version}/t3'), self.link)

    def last_run(self):
        return journal.read(self.m.state)[0][-1]

    def steps(self, run=None):
        return [(s['step'], s['phase']) for s in (run or self.last_run())['steps']]

    def done(self, name, run=None):
        return [s['data'] for s in (run or self.last_run())['steps'] if (s['step'], s['phase']) == (name, 'done')]

    def server(self):
        return live.live_server(self.m.home, self.adapter.service()['mainPid'])

    def kill_after(self, step, phase):
        """The run is killed right after it journals `step phase`; its end line is cut off afterwards."""
        real = journal.Run._append

        def append(run, event, **fields):
            real(run, event, **fields)
            if event == 'step' and (fields.get('step'), fields.get('phase')) == (step, phase):
                raise Killed(step)
        with mock.patch.object(journal.Run, '_append', append):
            with self.assertRaises(Killed):
                self.switch()
        path = os.path.join(self.m.state, 'journal.jsonl')
        with open(path, 'rb') as f:
            lines = f.read().splitlines(keepends=True)
        self.assertEqual(json.loads(lines[-1])['event'], 'end')
        with open(path, 'wb') as f:
            f.write(b''.join(lines[:-1]))
        run = self.last_run()
        self.assertIsNone(run['end'])
        return run

    def guard(self, run_id, wait=False):
        return switch.back_auto(config.load(), self.adapter, run_id, wait, say=self.said.append)


class Switch(SwitchBase):
    def test_switch_copies_updates_verifies_and_runs_the_post_hooks(self):
        self.rehearsed()
        old = self.server()
        result = self.switch()
        self.assertEqual(result['verified'], fx.TARGET)
        self.assertEqual([s for s, p in self.steps() if p == 'done'],
                         ['guard', 'pre-hooks', 'stopping', 'cold-copy', 'read-back', 'manifest', 'old-preflight',
                          'update', 'verify', 'post-hooks'])
        self.assertEqual(self.done('verify')[0]['result'], 'verified')
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertEqual(self.server()['version'], fx.TARGET)
        self.assertEqual(os.path.realpath(self.link), self.m.path(f'home/runtime/versions/{fx.TARGET}/t3'))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'stop', 'post'])
        # The copy: a new folder with the database, the old runtime, the unit and the service state, read back.
        copy = result['copy']
        m = coldcopy.verify_copy(copy)
        self.assertEqual(m['liveVersion'], fx.LIVE)
        self.assertEqual(sha(os.path.join(copy, 'userdata/statev2.sqlite')), self.db_before)
        extras = {e['path'] for e in m['extras']}
        self.assertLessEqual({f'runtime/versions/{fx.LIVE}/t3', 'runtime/service-state.json',
                              'unit/t3code.service'}, extras)
        self.assertEqual(sha(os.path.join(copy, 'extra/runtime/versions', fx.LIVE, 't3')), sha(old['binary']))
        # The old build's preflight ran on the copy; then the staged fork build updated the live home.
        staged = live.staged_runtime(self.m.stage, fx.TARGET)
        self.assertEqual(self.t3_calls, [
            [old['binary'], '__service-preflight', '--database-path', os.path.join(copy, 'userdata/statev2.sqlite'),
             '--launcher-protocol', '3'],
            [staged, 'update', fx.TARGET, '--base-dir', self.m.home, '--yes']])
        # The guard: a waiter now and a timer for the deadline, both `back --auto` for this run.
        [call] = [c for c in self.m.calls('fake-adapter-calls.jsonl') if c[0] == 'run_detached']
        rid = self.last_run()['id']
        self.assertEqual(call[2], [coldcopy.KIT_BIN, 'back', '--auto', '--run', rid, '--wait'])
        self.assertEqual(call[4], [coldcopy.KIT_BIN, 'back', '--auto', '--run', rid])
        self.assertGreaterEqual(call[3], switch.MIN_LIMIT)
        # The guard finds the run final and does nothing; a second switch says it is already on the target.
        self.assertIn('nothing to do', ' '.join(self.guard(rid)))
        n = len(self.calls())
        again = self.switch()
        self.assertEqual(again['nothing'], 'already-on-target')
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(self.steps(), [])

    def test_a_copy_never_goes_over_an_old_folder(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_stamp', return_value='20990101T000000Z'):
            os.makedirs(os.path.join(self.m.backups, f't3-switch-20990101T000000Z-{fx.LIVE}'))
            self.refused('already exists')

    def test_verify_counts_may_grow_but_never_shrink(self):
        self.rehearsed()
        self.after['database']['counts']['threads'] = 39
        self.mode = 'ok'
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('threads', str(cm.exception))
        self.assertIn('rolled back', str(cm.exception))


    def test_the_time_limit_is_three_rehearsals_and_at_least_the_floor(self):
        self.rehearsed(seconds=0)
        self.switch()
        self.assertEqual(self.started_data('stopping')['limit'], switch.MIN_LIMIT)

    def test_verify_names_every_problem(self):
        self.rehearsed()
        self.switch()
        cfg, rec = config.load(), self.started_data('stopping')
        binary = live.staged_runtime(cfg.stage_dir, fx.TARGET)

        def look(version=fx.TARGET):
            return ' | '.join(switch._look(cfg, self.adapter, version, rec, binary, False))
        self.assertEqual(look(), '')
        svc = self.adapter.service()
        with mock.patch.object(self.adapter, 'service', return_value={**svc, 'active': False}):
            self.assertIn('is not active', look())
        self.assertIn('the server runs t3', look(fx.LIVE))
        with mock.patch.object(switch, 'answers', return_value=False):
            self.assertIn('nothing answers HTTP', look())
        self.relink(fx.LIVE)
        self.assertIn('the t3 link', look())
        self.relink(fx.TARGET)
        for change, words in (({'database': {**self.after['database'], 'pendingMigrations': [83]}}, 'pending'),
                              ({'fork': {**self.after['fork'], 'featuresOn': ['waitFor']}}, 'fork features on'),
                              ({'settings': {**self.after['settings'], 'providerInstances': PROVIDERS[:1]}},
                               'provider instances'),
                              ({'ok': False}, 'not ok')):
            with self.subTest(words):
                self.m.set_report({**self.after, **change})
                self.assertIn(words, look())
        self.m.set_report(self.after)
        self.assertEqual(look(), '')


class Refusals(SwitchBase):
    def test_no_rehearsal(self):
        self.refused('no rehearsal')

    def test_the_last_rehearsal_did_not_pass(self):
        self.rehearsed()
        self.rehearsed(result='failed')
        self.refused('did not pass')

    def test_a_rehearsal_for_another_target(self):
        self.rehearsed(targetVersion='0.1.10-nightly.20261101.1.fork.1')
        self.refused('another target')

    def test_a_rehearsal_from_another_version(self):
        self.rehearsed(fromVersion='0.1.8')
        self.refused('t3 0.1.8')

    def test_a_rehearsal_too_old(self):
        self.configure(rehearsalMaxAgeHours=1)
        self.rehearsed()
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        with mock.patch.object(switch, '_utcnow', return_value=later):
            self.refused('too old')

    def test_migrations_differ_from_the_rehearsal(self):
        self.rehearsed(migrations=[82])
        self.refused('migrations')

    def test_a_fork_feature_on_in_the_live_settings(self):
        self.rehearsed()
        self.before['fork']['featuresOn'] = ['waitFor']
        self.m.set_report(self.before)
        self.refused('waitFor')

    def test_no_quiet_command_and_no_owner_go(self):
        self.rehearsed()
        self.configure(quietCommand=None)
        self.refused('quietCommand')

    def test_not_quiet_any_more_after_the_pre_hooks(self):
        self.rehearsed()
        real = coldcopy.quiet_now
        answers = iter([(True, 'q'), (True, 'q'), (False, 'a turn runs')])
        with mock.patch.object(coldcopy, 'quiet_now', side_effect=lambda *a: next(answers)):
            self.refused('not quiet')
        self.assertEqual(self.calls()[-2:], ['pre', 'post'])
        del real

    def test_a_failing_pre_hook_runs_the_post_hooks_and_refuses(self):
        self.rehearsed()
        self.m.write('pre.code', '3')
        self.refused('exited 3')
        self.assertEqual(self.calls()[-2:], ['pre', 'post'])

    def test_a_guard_that_dies_before_the_stop(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_guard_alive', return_value=False):
            self.refused('not running any more')
        self.assertEqual(self.calls()[-1], 'post')

    def test_a_home_with_a_restore_stopped_half_way(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_home_problem', return_value='a restore stopped half-way'):
            self.refused('a restore stopped half-way')
        self.assertNotIn('pre', self.calls())

    def test_free_disk_counts_the_old_runtime_and_is_checked_before_the_quiet_wait(self):
        self.rehearsed()
        runtime = sum(os.path.getsize(os.path.join(d, n)) for d, _, ns in
                      os.walk(self.m.path(f'home/runtime/versions/{fx.LIVE}')) for n in ns)
        with mock.patch.object(coldcopy, '_free_check', side_effect=journal.Refused('no room')) as free:
            self.refused('no room')
        self.assertGreaterEqual(free.call_args_list[0].args[1], coldcopy._plan_total(config.load()) + runtime)
        self.assertNotIn('quiet', self.calls())

    def test_a_server_that_changed_before_the_stop(self):
        self.rehearsed()
        real = check.server_changed

        def changed(*args):  # check's own look passes; switch's own look sees the change
            return 'the server changed' if sys._getframe(1).f_code.co_name == 'run_switch' else real(*args)
        with mock.patch.object(check, 'server_changed', changed):
            self.refused('the server changed')
        self.assertNotIn('pre', self.calls())

    def test_a_guard_that_does_not_come_up(self):
        self.rehearsed()
        self.adapter.guard_up = False
        self.refused('did not come up')

    def test_a_second_run_while_one_is_active(self):
        self.rehearsed()
        with journal.lock(self.m.state, 'switch', exclusive=True):
            with self.assertRaises(journal.Busy):
                self.switch()
        self.assertEqual(self.calls(), [])

    def test_a_stale_lock_file_from_a_dead_run_is_no_lock(self):
        self.rehearsed()
        self.m.write('kit-state/lock', json.dumps({'pid': 999999999, 'command': 'switch', 'at': 'then'}))
        self.assertEqual(self.switch()['verified'], fx.TARGET)

    def test_an_older_run_that_needs_settling(self):
        self.rehearsed()
        self.kill_after('stopping', 'started')
        with self.assertRaises(journal.Unfinished):
            self.switch()


class WayBack(SwitchBase):
    def assert_rolled_back(self):
        run = self.last_run()
        self.assertEqual(self.done('back-verify', run)[-1]['result'], 'rolled back')
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(sha(self.m.path('home/userdata/statev2.sqlite')), self.db_before)
        aside = [n for n in os.listdir(self.m.path('home/userdata')) if '.golive-aside-' in n]
        self.assertTrue(any(n.startswith('statev2.sqlite.') for n in aside), aside)
        self.assertEqual(os.path.realpath(self.link), self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))
        self.assertEqual(self.t3_calls[-1][1:], ['update', fx.LIVE, '--base-dir', self.m.home, '--allow-downgrade',
                                                 '--yes'])
        self.assertEqual(self.calls()[-1], 'post')
        self.assertTrue(switch.final(run))
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'])))
        return run

    def test_a_fork_build_that_never_gets_ready_is_rolled_back(self):
        self.rehearsed()
        self.mode = 'never'
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('rolled back', str(cm.exception))
        run = self.assert_rolled_back()
        self.assertEqual(self.done('verify', run)[0]['result'], 'failed')
        self.assertEqual(run['end']['result'], 'failed')  # the owner looks, then settles

    def test_a_server_that_dies_mid_verify_is_rolled_back(self):
        self.rehearsed()
        real = live.fork_report

        def report_then_die(binary, home):
            got = real(binary, home)
            if got['database']['pendingMigrations'] == []:
                fx.kill(self.m.server)
            return got
        with mock.patch.object(live, 'fork_report', side_effect=report_then_die):
            with self.assertRaises(RuntimeError):
                self.switch()
        self.assert_rolled_back()

    def test_the_runtime_folder_comes_back_from_the_copy_when_pruned(self):
        self.rehearsed()
        self.mode = 'never'
        real = self.fake_t3

        old = sha(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))
        seen = []

        def prune_then(binary, args, timeout):
            if args[:2] == ['update', fx.LIVE]:
                seen.append(sha(binary) if os.path.isfile(binary) else None)
            out = real(binary, args, timeout)
            if args[:2] == ['update', fx.TARGET]:
                shutil.rmtree(self.m.path(f'home/runtime/versions/{fx.LIVE}'))
            return out
        switch._t3 = prune_then
        with self.assertRaises(RuntimeError):
            self.switch()
        self.assertEqual(self.done('back-downgrade')[0]['runtimeFromCopy'], True)
        self.assertEqual(seen, [old], 'the old runtime came back from the copy before its update ran')
        self.assert_rolled_back()

    def test_a_server_that_changed_during_the_stop_is_started_again_without_a_copy(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_same_after_stop', side_effect=RuntimeError('changed while stopping')):
            with self.assertRaises(RuntimeError):
                self.switch()
        self.assertNotIn('cold-copy', [s for s, _ in self.steps()])
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)

    def test_the_way_back_verify_wants_the_counts_and_migrations_of_before(self):
        self.rehearsed()
        self.mode = 'never'
        with self.assertRaises(RuntimeError):
            self.switch()
        cfg, rec = config.load(), self.started_data('stopping')
        binary = live.staged_runtime(cfg.stage_dir, fx.TARGET)

        def look():
            return ' | '.join(switch._look(cfg, self.adapter, fx.LIVE, rec, binary, True))
        self.assertEqual(look(), '')
        self.m.set_report({**self.before, 'database': {**self.before['database'], 'counts': {
            **self.before['database']['counts'], 'threads': 41}}})
        self.assertIn('threads', look())
        self.m.set_report({**self.before, 'database': {**self.before['database'], 'pendingMigrations': []}})
        self.assertIn('pending', look())

    def test_extras_are_read_back(self):
        self.rehearsed()
        real = switch._copy_extras

        def then_spoil(cfg, unit_path, dest, version, deadline):
            entries = real(cfg, unit_path, dest, version, deadline)
            with open(os.path.join(dest, 'extra', 'runtime', 'service-state.json'), 'a') as f:
                f.write(' ')
            return entries
        with mock.patch.object(switch, '_copy_extras', then_spoil), self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('read-back', str(cm.exception))
        self.assertNotIn('update', [s for s, _ in self.steps()])

    def test_a_way_back_that_does_not_verify_says_so_and_is_not_final(self):
        self.rehearsed()
        with mock.patch.object(switch, '_look', return_value=['still wrong']), \
                mock.patch.object(coldcopy, 'START_WAIT', 0.2), self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('the way back did not verify', str(cm.exception))
        run = self.last_run()
        self.assertEqual(self.done('back-verify', run)[0]['result'], 'failed')
        self.assertEqual(run['end']['result'], 'failed')
        self.assertFalse(switch.final(run))

    def test_an_old_build_preflight_that_is_not_ready_starts_t3_again_without_an_update(self):
        self.rehearsed()
        self.preflight = 'blocked'
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('blocked', str(cm.exception))
        self.assertNotIn('update', [s for s, _ in self.steps()])
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.calls()[-2:], ['start', 'post'])


class Guard(SwitchBase):
    def test_the_guard_does_nothing_while_a_run_holds_the_lock(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        n = len(self.calls())
        with journal.lock(self.m.state, 'switch', exclusive=True):
            did = self.guard(run['id'])
        self.assertIn('nothing done', ' '.join(did))
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(len(self.last_run()['steps']), len(run['steps']))

    def test_killed_after_the_stop_before_the_copy_the_guard_starts_the_old_build(self):
        self.rehearsed()
        run = self.kill_after('stopping', 'done')
        self.guard(run['id'], wait=True)
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.calls()[-2:], ['start', 'post'])
        run = self.last_run()
        self.assertTrue(switch.final(run))
        self.assertEqual(run['end']['result'], 'failed')
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'])))

    def test_killed_before_the_stop_the_guard_finds_the_old_build_still_running(self):
        self.rehearsed()
        old = self.server()
        run = self.kill_after('stopping', 'started')
        self.guard(run['id'])
        self.assertEqual(self.server()['pid'], old['pid'])
        self.assertNotIn('start', self.calls())
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)
        self.assertTrue(switch.final(self.last_run()))

    def test_killed_mid_copy_the_copy_stays_incomplete_and_the_guard_starts_the_old_build(self):
        self.rehearsed()
        run = self.kill_after('cold-copy', 'started')
        copy = [s['data']['copy'] for s in run['steps'] if (s['step'], s['phase']) == ('stopping', 'started')][0]
        self.guard(run['id'])
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertNotIn('update', [s for s, _ in self.steps()])
        self.assertFalse(os.path.exists(os.path.join(copy, coldcopy.MANIFEST)))

    def test_killed_after_the_update_before_verify_the_guard_goes_back(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        self.assertEqual(self.server()['version'], fx.TARGET)
        self.guard(run['id'])
        self.assert_back()

    def test_after_a_reboot_the_guard_by_hand_goes_back_and_check_asks_to_settle(self):
        # A reboot loses the transient timer; the enabled service starts the build the unit names. The journal
        # still shows the switch unfinished: check refuses, and `back --auto --run <id>` by hand puts it right.
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('update', 'done')
        code, result = check.run_check(config.load(), self.adapter)
        self.assertEqual(code, 1)
        self.guard(run['id'])
        self.assert_back()

    def test_killed_mid_way_back_the_guard_runs_the_way_back_again(self):
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('back-restore', 'started')
        self.guard(run['id'])
        self.assert_back()

    def test_a_journal_the_guard_cannot_read_fails_open(self):
        self.rehearsed()
        run = self.kill_after('stopping', 'done')
        with open(os.path.join(self.m.state, 'journal.jsonl'), 'ab') as f:
            f.write(b'not json\n{"also": "bad"}\n')
        did = self.guard(run['id'])
        self.assertIn('unreadable', ' '.join(did))
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.calls()[-1], 'post')

    def test_a_guard_for_another_run_does_nothing_even_when_the_last_run_needs_it(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        self.assertIn('nothing to do', ' '.join(self.guard('some-other-run')))
        self.assertNotIn('back-stop', [s for s, _ in self.steps()])
        self.guard(run['id'])
        self.assert_back()

    def test_a_guard_for_an_older_run_does_nothing(self):
        self.rehearsed()
        self.switch()
        self.assertIn('nothing to do', ' '.join(self.guard('some-older-run')))

    def assert_back(self):
        run = self.last_run()
        self.assertEqual(self.done('back-verify', run)[-1]['result'], 'rolled back')
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(sha(self.m.path('home/userdata/statev2.sqlite')), self.db_before)
        self.assertEqual(run['end']['result'], 'failed')
        self.assertEqual(self.calls()[-1], 'post')
        self.assertTrue(switch.final(run))


class Pieces(unittest.TestCase):
    def test_answers_sees_any_http_reply_and_no_listener(self):
        srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Quiet)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            self.assertTrue(switch.answers(srv.server_address[1]))
        finally:
            srv.shutdown()
            srv.server_close()
        self.assertFalse(switch.answers(srv.server_address[1]))
        self.assertFalse(switch.answers(None))

    def test_the_cli_runs_switch_detached_and_refuses_here_inside_the_service(self):
        m = fx.Machine()
        self.addCleanup(m.close)
        m.standard()
        adapter = fx.FakeAdapter(m.root, m)
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(switch.main_switch(config.load(), adapter, False, False, out, err), 0)
        [call] = m.calls('fake-adapter-calls.jsonl')
        self.assertEqual(call[2][1:3], ['switch', '--here'])
        with mock.patch.object(adapter, 'inside_service', return_value=True):
            self.assertEqual(switch.main_switch(config.load(), adapter, True, False, out, err), 1)
        self.assertIn('without --here', err.getvalue())

    def test_the_command_line_wires_switch_and_back(self):
        m = fx.Machine()
        self.addCleanup(m.close)
        m.standard()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(check.main(['switch', '--owner-says-go'], out=out, err=err), 0, err.getvalue())
        [call] = m.calls('systemd-run-calls.jsonl')
        self.assertEqual(call[call.index('--') + 1:], [coldcopy.KIT_BIN, 'switch', '--here', '--owner-says-go'])
        self.assertEqual(check.main(['back', '--auto', '--run', 'no-such-run'], out=out, err=err), 0)
        self.assertIn('nothing to do', out.getvalue())
        with self.assertRaises(SystemExit):
            check.main(['back', '--run', 'no-such-run'], out=out, err=io.StringIO())


if __name__ == '__main__':
    unittest.main()
