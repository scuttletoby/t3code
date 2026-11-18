"""t3-golive switch, verify, the automatic way back (#397's W1) and the switch's guard, on a fake machine
(tests/golive_fixture.py, tests/back_fixture.py): the service manager is faked, the T3 home is a temp folder, the
staged target is a real binary (a copy of `sleep`, so /proc shows the server it runs), reports and W1's downgrade
go through the stub t3 (back.T3_STUB), and the forward `t3 update` is faked here (it records a download if the
target is not installed, migrates the database and prunes the other runtimes, as upstream does). A kill is a
BaseException raised right after a journal line, with the run's end line then cut off, as a SIGKILL leaves it.
Nothing here stops, starts or reads a real T3.
python3 -I -B fork-kit/golive/tests/switch.test.py"""
import datetime, hashlib, io, json, os, shutil, sys, tempfile, threading, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import back_fixture as bx  # noqa: E402
import golive_fixture as fx  # noqa: E402
from golive import back, check, coldcopy, config, journal, keptruntime, live, switch  # noqa: E402

HOOK = '''#!{python} -I
import json, sys
with open({log!r}, 'a') as f:
    f.write(json.dumps([{name!r}]) + '\\n')
sys.exit(int(open({codes!r}).read()) if __import__('os').path.exists({codes!r}) else 0)
'''
PROVIDERS = [{'id': 'claude-a', 'driver': 'claudeAgent'}, {'id': 'codex', 'driver': 'codex'}]
SLEEP = shutil.which('sleep', path='/usr/bin:/bin')
REAL_T3 = switch._t3  # SwitchBase replaces it


class Killed(BaseException):
    """A kill: no code of the run runs after it (it is not an Exception)."""


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


def counts(threads):
    return {'projects': 3, 'projectsArchived': 0, 'threads': threads, 'threadsArchived': 2}


class SwitchBase(unittest.TestCase):
    mode = 'ok'  # what the fork build does after `update`: ok, or never (its server never gets ready)

    def setUp(self):
        self.m = bx.Machine()
        self.addCleanup(self.m.close)
        self.m.standard()
        with open(SLEEP, 'rb') as f:
            self.m.stage_release(fx.TARGET, files={'t3': f.read(), 'lib/runtime.txt': 'runtime data'})
        self.before = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS})
        self.after = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS},
                               database={'pendingMigrations': [], 'counts': counts(41)})
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
        self.adapter = bx.Adapter(self.m.root, self.m)
        stub, real_report = self.m.path('bin/t3-stub'), live.fork_report
        for target, values in ((coldcopy, dict(QUIET_GAP=0, POLL=0.01, STOP_WAIT=3, START_WAIT=5, GUARD_WAIT=0.3)),
                               (switch, dict(MIN_LIMIT=1, _t3=self.fake_t3)),
                               (back, dict(T3_STUB=stub)),
                               (live, dict(fork_report=lambda binary, home: real_report(stub, home)))):
            patcher = mock.patch.multiple(target, **values)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.preflight, self.t3_calls, self.said, self.downloads, self.tamper = 'ready', [], [], [], False
        self.db_before = sha(self.m.path('home/userdata/statev2.sqlite'))
        self.old_t3 = sha(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))

    def configure(self, **over):
        self.m.config(**{'quietCommand': self.m.path('bin/quiet'), 'preHooks': [self.m.path('bin/pre')],
                         'postHooks': [self.m.path('bin/post')], **over})

    def rehearsed(self, result='ok', **over):
        """A rehearsal's journal record, as rehearse writes it and switch.passed_rehearsal reads it."""
        target_sha = live.staged_build(self.m.stage, fx.TARGET)[1]
        data = {'fromVersion': fx.LIVE, 'targetVersion': fx.TARGET, 'targetSha256': target_sha, 'migrations': [82, 83],
                'migrateSeconds': 0.0, 'readySeconds': 0, **over}
        data = {k: v for k, v in data.items() if v is not None}
        try:
            with journal.run(self.m.state, 'rehearse', scratch=True) as r:
                with r.step('result') as facts:
                    facts.update(data)
                if result != 'ok':
                    raise journal.Refused('the rehearsal failed')
        except journal.Refused:
            pass

    # --- the forward `t3 update` of the installed target (upstream's, as far as the switch sees it) ---
    def fake_t3(self, binary, args, timeout):
        self.t3_calls.append([binary, *args])
        if args[0] == '__service-preflight':
            return 0, json.dumps({'status': self.preflight, 'version': fx.LIVE})
        assert args[:2] == ['update', fx.TARGET] and '--yes' in args, args
        assert args[args.index('--base-dir') + 1] == self.m.home, args
        versions = self.m.path('home/runtime/versions')
        if not os.path.exists(os.path.join(versions, fx.TARGET, '.install-complete')):
            self.downloads.append(fx.TARGET)  # upstream would fetch it: the switch must never let it
            self.m.install(fx.TARGET)
        if self.tamper:  # a target that is not the rehearsed build any more
            with open(os.path.join(versions, fx.TARGET, 'lib', 'runtime.txt'), 'a') as f:
                f.write('changed')
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 4, 'activeVersion': fx.TARGET}))
        self.m.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'migrated' * 600)
        self.m.set_report(self.after)
        for v in os.listdir(versions):  # upstream prunes every other whole runtime at the end of an update
            d = os.path.join(versions, v)
            if not v.startswith('.') and v != fx.TARGET and keptruntime.installed_t3(self.m.home, v):
                shutil.rmtree(d)
        if self.mode != 'never':
            self.m.spawn_server(fx.TARGET)
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

    def names(self, phase='done', run=None):
        return [s for s, p in self.steps(run) if p == phase]

    def done(self, name, run=None):
        return [s['data'] for s in (run or self.last_run())['steps'] if (s['step'], s['phase']) == (name, 'done')]

    def server(self):
        return live.live_server(self.m.home, self.adapter.service()['mainPid'])

    def kill_after(self, step, phase, nth=1):
        """The run is killed right after it journals `step phase` the nth time; its end line is cut off afterwards."""
        real, seen = journal.Run._append, []

        def append(run, event, **fields):
            real(run, event, **fields)
            if event == 'step' and (fields.get('step'), fields.get('phase')) == (step, phase):
                seen.append(1)
                if len(seen) == nth:
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
        return switch.guard(config.load(), self.adapter, run_id, wait, say=self.said.append)

    def copy_at(self, run=None):
        copy = self.started_data('stopping', run)['copy']
        return copy, coldcopy.read_manifest(copy, self.m.home)['createdAt']

    def downgrades(self):
        return [c for c in self.m.calls('back-t3-calls.jsonl') if c[0] == 'update']

    def assert_rolled_back(self, run=None, post=True):
        run = run or self.last_run()
        self.assertEqual(self.done('rolled-back', run)[-1]['result'], 'rolled back')
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertEqual(sha(self.m.path('home/userdata/statev2.sqlite')), self.db_before)
        self.assertEqual(sha(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3')), self.old_t3)
        self.assertEqual(self.downgrades()[-1], ['update', fx.LIVE, '--allow-downgrade', '--yes', '--base-dir',
                                                 self.m.home])
        self.assertEqual(self.started_data('plan', run)['automatic'], True)
        self.assertIn('disable', self.calls())
        if post:
            self.assertEqual(self.calls()[-1], 'post')
        self.assertEqual(self.downloads, [])
        self.assertTrue(switch.final(run))
        self.assertFalse(switch.w1_holds(run['steps']))
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'])))
        return run


class Switch(SwitchBase):
    def test_switch_keeps_copies_installs_the_rehearsed_build_updates_verifies_and_runs_the_post_hooks(self):
        self.rehearsed()
        old = self.server()
        result = self.switch()
        self.assertEqual(result['verified'], fx.TARGET)
        self.assertEqual(self.names(), ['keep-runtime', 'guard', 'pre-hooks', 'stopping', 'cold-copy', 'read-back',
                                        'manifest', 'before', 'old-preflight', 'install-target', 'update', 'verify',
                                        'post-hooks'])
        self.assertEqual(self.done('verify')[0]['result'], 'verified')
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertEqual(self.server()['version'], fx.TARGET)
        self.assertEqual(os.path.realpath(self.link), self.m.path(f'home/runtime/versions/{fx.TARGET}/t3'))
        self.assertEqual(self.calls(), ['quiet', 'quiet', 'run_detached', 'pre', 'quiet', 'stop', 'post'])
        # The copy: a new folder with the database, the unit and the service state, read back.
        copy = result['copy']
        self.assertTrue(os.path.basename(copy).startswith('t3-switch-'))
        m = coldcopy.verify_copy(copy)
        self.assertEqual(m['liveVersion'], fx.LIVE)
        self.assertEqual(sha(os.path.join(copy, 'userdata/statev2.sqlite')), self.db_before)
        self.assertEqual({e['path'] for e in m['extras']}, {'runtime/service-state.json', 'unit/t3code.service'})
        # The rehearsed build was installed by the kit (no download), and verify checked it byte for byte.
        target_sha = live.staged_build(self.m.stage, fx.TARGET)[1]
        self.assertEqual(self.downloads, [])
        self.assertEqual(self.done('install-target')[0]['installed'], True)
        self.assertEqual(self.started_data('install-target')['sha256'], target_sha)
        self.assertEqual(self.done('verify')[0]['sha256'], target_sha)
        archive = live.staged_archive(self.m.stage, fx.TARGET)[0]
        self.assertEqual(switch._identity(config.load(), archive, target_sha), [])
        # The old build's preflight ran on the copy; then the installed target updated the live home.
        self.assertEqual(self.t3_calls, [
            [old['binary'], '__service-preflight', '--database-path', os.path.join(copy, 'userdata/statev2.sqlite'),
             '--launcher-protocol', '3'],
            [self.m.path(f'home/runtime/versions/{fx.TARGET}/t3'), 'update', fx.TARGET, '--base-dir', self.m.home,
             '--yes']])
        # The guard: a waiter now and a timer for the deadline, both `switch-guard` for this run.
        [call] = [c for c in self.m.calls('fake-adapter-calls.jsonl') if c[0] == 'run_detached']
        rid = self.last_run()['id']
        self.assertEqual(call[2], [coldcopy.KIT_BIN, 'switch-guard', '--run', rid, '--wait'])
        self.assertEqual(call[4], [coldcopy.KIT_BIN, 'switch-guard', '--run', rid])
        self.assertEqual(call[3], switch.guard_after(config.load(), switch.MIN_LIMIT))
        # The guard finds the run final and does nothing; a second switch says it is already on the target.
        self.assertIn('nothing to do', ' '.join(self.guard(rid)))
        n = len(self.calls())
        again = self.switch()
        self.assertEqual(again['nothing'], 'already-on-target')
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(self.steps(), [])

    def test_the_old_runtime_is_kept_outside_the_home_before_the_stop(self):
        self.rehearsed()
        self.switch()
        order = self.names()
        self.assertLess(order.index('keep-runtime'), order.index('stopping'))
        kept = self.done('keep-runtime')[0]['kept']
        self.assertEqual(kept, keptruntime.kept_dir(config.load(), fx.LIVE))
        self.assertFalse(os.path.realpath(kept).startswith(self.m.home + os.sep))
        self.assertEqual(keptruntime.check(config.load(), fx.LIVE)[0], kept)
        self.assertEqual(sha(os.path.join(kept, 'runtime', 't3')), self.old_t3)
        self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{fx.LIVE}')))  # the update pruned it

    def test_a_copy_never_goes_over_an_old_folder(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_stamp', return_value='20990101T000000Z'):
            os.makedirs(os.path.join(self.m.backups, f't3-switch-20990101T000000Z-{fx.LIVE}'))
            self.refused('already exists')

    def test_the_time_limit_is_three_rehearsals_and_at_least_the_floor(self):
        self.rehearsed()
        self.switch()
        self.assertEqual(self.started_data('stopping')['limit'], switch.MIN_LIMIT)

    def test_the_time_limit_is_three_times_migrate_plus_ready_rounded_up(self):
        self.rehearsed(migrateSeconds=250.2, readySeconds=50)
        self.switch()
        self.assertEqual(self.started_data('stopping')['limit'], 3 * 301)

    def test_the_guard_timer_fires_after_the_latest_the_run_can_end(self):
        cfg = config.load()
        a, b = switch.guard_after(cfg, 100), switch.guard_after(cfg, 1100)
        self.assertEqual(b - a, 2 * 1000)  # the update's wait and verify's each take up to the limit
        hooks = (len(cfg.pre_hooks) + len(cfg.post_hooks)) * coldcopy.HOOK_TIMEOUT
        self.assertGreaterEqual(a, hooks + 2 * 100 + cfg.stop_limit_seconds + back.UPDATE_TIMEOUT
                                + live.REPORT_TIMEOUT + switch.GUARD_MARGIN)

    def test_a_verified_switch_with_a_thread_added_during_the_quiet_wait(self):
        # The counts verify keeps are read from the stopped home, not from check's look before the quiet wait.
        self.rehearsed()
        more = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS},
                         database={'counts': counts(41)})
        with mock.patch.object(coldcopy, 'wait_quiet', side_effect=lambda *a: self.m.set_report(more)):
            self.assertEqual(self.switch()['verified'], fx.TARGET)
        self.assertEqual(self.done('before')[0]['reportCounts']['threads'], 41)

    def test_verify_counts_from_after_the_stop_may_grow_but_never_shrink(self):
        # check saw 40 threads; one more was written before the stop; the target shows 41 of the 42.
        self.rehearsed()
        more = fx.report(version=fx.TARGET, settings={'providerInstances': PROVIDERS},
                         database={'counts': counts(42)})
        self.adapter.on['stop'] = lambda: self.m.set_report(more) if 'before' not in self.names('started') else None
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('threads: 42 before, 41 now', str(cm.exception))
        self.assertIn('rolled back', str(cm.exception))
        self.assert_rolled_back()

    def test_verify_names_every_problem(self):
        self.rehearsed()
        self.switch()
        cfg, before = config.load(), self.done('before')[0]
        before = {'counts': before['reportCounts'], 'providers': before['providers']}
        binary = keptruntime.home_t3(cfg.home, fx.TARGET)
        build = live.staged_archive(cfg.stage_dir, fx.TARGET)[0], live.staged_build(cfg.stage_dir, fx.TARGET)[1]

        def look(version=fx.TARGET):
            return ' | '.join(switch._look(cfg, self.adapter, version, before, binary, build))
        self.assertEqual(look(), '')
        svc = self.adapter.service()
        with mock.patch.object(self.adapter, 'service', return_value={**svc, 'active': False}):
            self.assertEqual(look(), f'{switch.osadapter.UNIT} is not active')  # at once, not via server_changed
        self.assertIn('the server runs t3', look(fx.LIVE))
        with mock.patch.object(switch, 'answers', return_value=False):
            self.assertIn('nothing answers HTTP', look())
        os.makedirs(self.m.path(f'home/runtime/versions/{fx.LIVE}'))
        self.relink(fx.LIVE)
        self.assertIn('the t3 link', look())
        self.relink(fx.TARGET)
        state = self.m.path('home/runtime/service-state.json')
        with open(state, encoding='utf-8') as f:
            saved = f.read()
        self.m.write('home/runtime/service-state.json', json.dumps(
            {'protocol': 4, 'activeVersion': fx.TARGET, 'update': {'status': 'pending'}}))
        self.assertIn('with an update pending', look())
        self.m.write('home/runtime/service-state.json', saved)
        self.m.write('home/runtime/.restart-pending', '')
        marker = self.m.path('home/runtime/.restart-pending')
        self.assertIn(f'marker {marker}', look())  # at once, not via server_changed
        os.unlink(self.m.path('home/runtime/.restart-pending'))
        for change, words in (({'database': {**self.after['database'], 'pendingMigrations': [83]}}, 'pending'),
                              ({'database': {**self.after['database'], 'pendingMigrations': None}}, 'pending'),
                              ({'database': {**self.after['database'], 'counts': {}}}, 'threads: 40 before'),
                              ({'fork': {**self.after['fork'], 'featuresOn': ['waitFor']}}, 'fork features on'),
                              ({'settings': {**self.after['settings'], 'providerInstances': PROVIDERS[:1]}},
                               'provider instances'),
                              ({'ok': False}, 'not ok')):
            with self.subTest(words):
                self.m.set_report({**self.after, **change})
                self.assertIn(words, look())
        self.m.set_report(self.after)
        self.assertEqual(look(), '')
        with open(self.m.path(f'home/runtime/versions/{fx.TARGET}/lib/runtime.txt'), 'a') as f:
            f.write('x')
        self.assertIn('not the rehearsed build', look())
        with open(self.m.path(f'home/runtime/versions/{fx.TARGET}/lib/runtime.txt'), 'w') as f:
            f.write('runtime data')
        self.assertEqual(look(), '')
        os.unlink(self.m.path(f'home/runtime/versions/{fx.TARGET}/.install-complete'))
        self.assertIn('not a whole runtime', look())
        self.assertEqual(switch._look(cfg, self.adapter, fx.TARGET, before, binary), [])  # without build=: no check


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

    def test_a_rehearsal_journalled_before_the_build_was_tied_to_it(self):
        self.rehearsed(targetSha256=None)  # as rehearse wrote it before this change
        self.refused('another build', 'rehearse again')

    def test_a_staged_build_changed_after_the_rehearsal(self):
        self.rehearsed()
        self.m.stage_release(fx.TARGET, files={'t3': b'#!/bin/sh\nexit 0\n', 'lib/runtime.txt': 'runtime data'})
        self.refused('another build')

    def test_a_staged_folder_changed_after_the_rehearsal(self):
        self.rehearsed()
        self.m.write(f'stage/{fx.TARGET}/lib/runtime.txt', 'changed')
        self.refused()

    def test_a_rehearsal_too_old(self):
        self.configure(rehearsalMaxAgeHours=1)
        self.rehearsed()
        later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=2)
        with mock.patch.object(switch, '_utcnow', return_value=later):
            self.refused('too old')

    def test_a_rehearsal_from_the_future(self):
        self.rehearsed()
        earlier = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
        with mock.patch.object(switch, '_utcnow', return_value=earlier):
            self.refused('after the clock')

    def test_a_rehearsal_record_without_a_migration_list_or_time(self):
        for broken in ({'migrations': None}, {'migrateSeconds': None}, {'readySeconds': '5'},
                       {'migrateSeconds': True}, {'readySeconds': -1}, {'migrateSeconds': float('nan')}):
            with self.subTest(broken=broken):
                self.rehearsed(**broken)
                self.refused('no migration list or time')

    def test_migrations_differ_from_the_rehearsal(self):
        self.rehearsed(migrations=[82])
        self.refused('migrations')

    def test_unknown_counts_refuse(self):
        self.rehearsed()
        for bad in ({}, {**counts(40), 'threads': None}, {**counts(40), 'projects': True}):
            with self.subTest(bad=bad):
                self.m.set_report({**self.before, 'database': {**self.before['database'], 'counts': bad}})
                self.refused()
        self.assertEqual([r for r in journal.read(self.m.state)[0] if r['command'] == 'switch' and r['steps']], [])

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
        answers = iter([(True, 'q'), (True, 'q'), (False, 'a turn runs')])
        with mock.patch.object(coldcopy, 'quiet_now', side_effect=lambda *a: next(answers)):
            self.refused('not quiet')
        self.assertEqual(self.calls()[-2:], ['pre', 'post'])

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

    def test_free_disk_counts_the_copy_the_kept_runtime_and_the_install_before_the_quiet_wait(self):
        self.rehearsed()
        cfg = config.load()
        with mock.patch.object(switch, '_disk', side_effect=journal.Refused('no room')) as disk:
            self.refused('no room')
        copy, keep, install = disk.call_args_list[0].args[1:]
        self.assertEqual(copy, coldcopy._plan_total(cfg))
        self.assertEqual(keep, keptruntime.size(cfg, fx.LIVE))
        self.assertEqual(install, switch._tree_bytes(self.m.path(f'stage/{fx.TARGET}')))
        self.assertGreater(keep * install, 0)
        self.assertNotIn('quiet', self.calls())

    def test_disk_adds_up_what_lands_on_one_device(self):
        cfg = config.load()
        free = coldcopy._free_bytes(self.m.root)
        switch._disk(cfg, 0, 0, 0)
        with self.assertRaises(journal.Refused) as cm:
            switch._disk(cfg, free // 2, free // 2, 2 * coldcopy.MB)  # each part fits alone, not all three
        self.assertIn('nothing was changed', str(cm.exception))

    def test_no_whole_old_runtime_to_keep_refuses_before_any_step(self):
        self.rehearsed()
        with mock.patch.object(keptruntime, 'plan_keep', side_effect=ValueError('nothing to keep')):
            self.refused('nothing to keep')
        self.assertEqual(self.steps(), [])

    def test_a_failing_keep_refuses_before_any_stop(self):
        self.rehearsed()
        with mock.patch.object(keptruntime, 'keep', side_effect=OSError('disk full')):
            self.refused('keeping the old build', 'disk full', 'not stopped')
        self.assertEqual(self.done('keep-runtime')[0]['error'], 'OSError: disk full')
        self.assertNotIn('run_detached', self.calls())

    def test_a_server_that_changed_before_the_stop(self):
        self.rehearsed()
        real = check.server_changed

        for where, hooks in (('_prechecks', []), ('run_switch', ['pre', 'post'])):
            with self.subTest(where):  # check's own look passes; the switch's own look (there) sees the change
                n = len(self.calls())

                def changed(*args):
                    return 'the server changed' if sys._getframe(1).f_code.co_name == where else real(*args)
                with mock.patch.object(check, 'server_changed', changed):
                    self.refused('the server changed')
                self.assertEqual([c for c in self.calls()[n:] if c in ('pre', 'post')], hooks)

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
    def test_a_fork_build_that_never_gets_ready_goes_back_with_w1(self):
        self.rehearsed()
        self.mode = 'never'
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('rolled back', str(cm.exception))
        run = self.assert_rolled_back()
        self.assertEqual(self.done('verify', run)[0], {'version': fx.LIVE, 'pid': self.server()['pid']})  # W1's
        self.assertEqual(self.done('runtime', run)[0]['putBack'], True)  # the update pruned it: put back from kept
        self.assertEqual(run['end']['result'], 'failed')  # the owner looks, then settles
        self.assertEqual(self.names(run=run)[-6:], ['runtime', 'stop-service', 'restore', 'update', 'verify',
                                                    'rolled-back'][-5:] + ['post-hooks'])

    def test_a_server_that_dies_mid_verify_goes_back(self):
        self.rehearsed()
        real = switch._report

        def report_then_die(binary, home):
            got = real(binary, home)
            if got['database']['pendingMigrations'] == [] and self.m.server is not None:
                fx.kill(self.m.server)
            return got
        with mock.patch.object(switch, '_report', side_effect=report_then_die), self.assertRaises(RuntimeError):
            self.switch()
        self.assert_rolled_back()

    def test_an_installed_target_that_is_not_the_rehearsed_build_goes_back(self):
        self.rehearsed()
        self.tamper = True
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('not the rehearsed build', str(cm.exception))
        self.assert_rolled_back()

    def test_a_staged_folder_changed_during_the_run_is_not_installed(self):
        self.rehearsed()
        real = coldcopy.copy_userdata

        def change_then(*args):
            self.m.write(f'stage/{fx.TARGET}/lib/runtime.txt', 'changed')
            return real(*args)
        with mock.patch.object(coldcopy, 'copy_userdata', change_then), self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('does not match its archive', str(cm.exception))
        self.assertNotIn('update', self.names('started'))
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)
        self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{fx.TARGET}')))

    def test_a_whole_other_build_staged_during_the_run_is_not_installed(self):
        self.rehearsed()  # the archive, SHA256SUMS and folder all replaced by another build: consistent, not rehearsed
        real = coldcopy.copy_userdata
        t3 = self.m.path(f'stage/{fx.TARGET}/t3')

        def restage_then(*args):
            with open(t3, 'rb') as f:
                self.m.stage_release(fx.TARGET, files={'t3': f.read(), 'lib/runtime.txt': 'another build'})
            return real(*args)
        with mock.patch.object(coldcopy, 'copy_userdata', restage_then), self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('changed since the rehearsal', str(cm.exception))
        self.assertNotIn('update', self.names('started'))
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)
        self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{fx.TARGET}')))

    def test_an_existing_target_folder_is_reused_when_it_is_the_build_else_set_aside(self):
        self.rehearsed()
        cfg = config.load()
        archive, target_sha = live.staged_archive(cfg.stage_dir, fx.TARGET)[0], live.staged_build(cfg.stage_dir,
                                                                                                 fx.TARGET)[1]
        self.m.write(f'home/runtime/versions/{fx.TARGET}/t3', b'other')  # no .install-complete: not the build
        self.m.write(f'home/runtime/versions/{switch.INSTALL}{fx.TARGET}/left', 'an install that was stopped')
        with journal.run(cfg.state_dir, 'switch') as run:
            with run.step('install-target') as facts:
                facts.update(switch._install_target(run, cfg, archive, target_sha))
            self.assertTrue(facts['installed'])
            self.assertEqual(sha(os.path.join(facts['setAside'], 't3')), hashlib.sha256(b'other').hexdigest())
            self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{switch.INSTALL}{fx.TARGET}')))
            self.assertEqual(switch._identity(cfg, archive, target_sha), [])
            with run.step('install-target') as again:
                again.update(switch._install_target(run, cfg, archive, target_sha))
            self.assertEqual(again, {'runtime': keptruntime.runtime_dir(cfg.home, fx.TARGET), 'installed': False})
            with self.assertRaises(RuntimeError):
                switch._install_target(run, cfg, archive, target_sha)  # outside a step

    def test_a_server_that_changed_during_the_stop_is_started_again_without_a_copy(self):
        self.rehearsed()
        with mock.patch.object(coldcopy, '_same_after_stop', side_effect=RuntimeError('changed while stopping')):
            with self.assertRaises(RuntimeError):
                self.switch()
        self.assertNotIn('cold-copy', self.names('started'))
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)

    def test_a_stopped_home_with_unknown_counts_starts_the_old_build_again(self):
        self.rehearsed()
        blind = {**self.before, 'database': {**self.before['database'], 'counts': {}}}
        self.adapter.on['stop'] = lambda: self.m.set_report(blind)
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('gave no counts', str(cm.exception))
        self.assertNotIn('install-target', self.names('started'))
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.calls()[-2:], ['start', 'post'])

    def test_a_stopped_home_with_a_feature_on_or_other_migrations_starts_the_old_build_again(self):
        for i, (change, words) in enumerate((
                ({'fork': {**self.before['fork'], 'featuresOn': ['waitFor']}}, 'fork features on'),
                ({'database': {**self.before['database'], 'pendingMigrations': [82]}}, 'migrations'))):
            with self.subTest(words), mock.patch.object(coldcopy, '_stamp', return_value=f'2099010{i + 1}T000000Z'):
                if journal.unfinished(self.m.state):
                    journal.settle(self.m.state, 'test: put right')
                self.m.set_report(self.before)
                self.rehearsed()
                self.adapter.on['stop'] = lambda: self.m.set_report({**self.before, **change})
                with self.assertRaises(RuntimeError) as cm:
                    self.switch()
                self.assertIn(words, str(cm.exception))
                self.assertNotIn('install-target', self.names('started'))
                self.assertEqual(self.server()['version'], fx.LIVE)

    def test_extras_are_read_back(self):
        self.rehearsed()
        real = switch._copy_extras

        def then_spoil(cfg, unit_path, dest, deadline):
            entries = real(cfg, unit_path, dest, deadline)
            with open(os.path.join(dest, 'extra', 'runtime', 'service-state.json'), 'a') as f:
                f.write(' ')
            return entries
        with mock.patch.object(switch, '_copy_extras', then_spoil), self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('read-back', str(cm.exception))
        self.assertNotIn('update', self.names('started'))

    def test_an_old_build_preflight_that_is_not_ready_starts_t3_again_without_an_update(self):
        self.rehearsed()
        self.preflight = 'blocked'
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        self.assertIn('blocked', str(cm.exception))
        self.assertNotIn('update', self.names('started'))
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.calls()[-2:], ['start', 'post'])

    def test_a_failed_w1_leaves_t3_stopped_and_disabled_once_and_status_names_it(self):
        self.rehearsed()
        self.mode = 'never'
        self.m.write('update.code', '1')  # the stub's downgrade fails
        with self.assertRaises(RuntimeError) as cm:
            self.switch()
        text = ' '.join([str(cm.exception), *getattr(cm.exception, '__notes__', ())])
        self.assertIn('putting T3 back failed', text)
        self.assertIn('no post hook ran', text)
        run = self.last_run()
        self.assertEqual(run['end']['result'], 'failed')
        self.assertTrue(switch.w1_holds(run['steps']))
        self.assertFalse(self.adapter.service()['active'])
        self.assertFalse(self.adapter.service()['enabled'])
        self.assertNotIn('post', self.calls())
        self.assertEqual(len(self.downgrades()), 1)
        # The guard does not run W1 again; status says the way back stopped T3 and what to run.
        n = len(self.calls())
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'], wait=True)))
        self.assertEqual(len(self.calls()), n)
        self.assertEqual(len(self.downgrades()), 1)
        code, lines = back.status(config.load(), self.adapter)
        self.assertEqual(code, 1)
        self.assertIn('T3 is DOWN', lines[0])
        self.assertIn('W1 stopped T3', '\n'.join(lines))
        self.assertIn('t3-golive back --way w1', '\n'.join(lines))
        # A rerun of the same back by hand finishes it (#397's W1 contract).
        os.unlink(self.m.path('update.code'))
        copy, at = self.copy_at(run)
        got = back.run_back(config.load(), self.adapter, 'w1', lose_work_since=at, say=self.said.append)
        self.assertEqual(got['version'], fx.LIVE)
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertEqual(self.calls()[-1], 'post')
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertEqual(self.started_data('plan')['copy'], copy)


class Guard(SwitchBase):
    def hold_lock_while(self, run_id, check_blocked):
        """The guard (as its timer runs it) starts while something else holds the kit's lock: it must wait."""
        out = {}
        with journal.lock(self.m.state, 'back', exclusive=True):
            t = threading.Thread(target=lambda: out.setdefault('did', self.guard(run_id)), daemon=True)
            t.start()
            time.sleep(0.3)
            self.assertTrue(t.is_alive(), 'the guard did not wait for the lock')
            check_blocked()
        t.join(30)
        self.assertFalse(t.is_alive())
        return out['did']

    def test_the_guard_waits_for_the_lock_then_acts_on_the_killed_run(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        n = len(self.last_run()['steps'])
        did = self.hold_lock_while(run['id'], lambda: self.assertEqual(len(self.last_run()['steps']), n))
        self.assertIn('rolled back', ' '.join(did))
        self.assertEqual(self.last_run()['end']['result'], 'failed')
        self.assert_rolled_back()

    def test_a_guard_timer_firing_into_a_running_back_waits_and_then_does_nothing(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        _, at = self.copy_at(run)
        real, out = coldcopy.restore, {}

        def restore_with_a_guard_firing(*args, **kw):
            t = threading.Thread(target=lambda: out.setdefault('did', self.guard(run['id'])), daemon=True)
            t.start()
            out['thread'] = t
            time.sleep(0.3)
            self.assertTrue(t.is_alive(), 'the guard did not wait for the running back')
            return real(*args, **kw)
        with mock.patch.object(coldcopy, 'restore', restore_with_a_guard_firing):
            got = back.run_back(config.load(), self.adapter, 'w1', lose_work_since=at, say=self.said.append)
        out['thread'].join(30)
        self.assertEqual(got['version'], fx.LIVE)
        self.assertIn('nothing to do', ' '.join(out['did']))
        self.assertEqual(len(self.downgrades()), 1)
        self.assertEqual(self.last_run()['command'], 'back')
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertEqual(self.server()['version'], fx.LIVE)

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

    def test_killed_before_or_mid_keep_nothing_live_changed_and_a_rerun_keeps_again(self):
        for phase in ('started', 'done'):
            with self.subTest(phase):
                self.rehearsed()
                if phase == 'started':  # mid-keep: the kit's own unfinished copy is left
                    real = keptruntime._copy_tree

                    def copy_then_die(src, dst, entries, verify=False):
                        real(src, dst, entries[:1], verify)
                        raise Killed('mid-keep')
                    with mock.patch.object(keptruntime, '_copy_tree', copy_then_die):
                        with self.assertRaises(Killed):
                            self.switch()
                    path = os.path.join(self.m.state, 'journal.jsonl')
                    with open(path, 'rb') as f:
                        lines = f.read().splitlines(keepends=True)
                    with open(path, 'wb') as f:
                        f.write(b''.join(lines[:-1]))
                else:
                    self.kill_after('keep-runtime', 'done')
                run = self.last_run()
                self.assertNotIn('stop', self.calls())
                self.assertEqual(self.server()['version'], fx.LIVE)
                self.assertIn('T3 was never stopped', ' '.join(self.guard(run['id'])))
                self.assertEqual(self.last_run()['end']['result'], 'failed')
                with self.assertRaises(journal.Unfinished):
                    self.switch()
                journal.settle(self.m.state, 'test: killed before any stop')
        self.assertEqual(self.switch()['verified'], fx.TARGET)
        self.assertEqual(keptruntime.check(config.load(), fx.LIVE)[0], keptruntime.kept_dir(config.load(), fx.LIVE))
        self.assertEqual([n for n in os.listdir(os.path.join(self.m.state, 'kept-runtimes'))
                          if n.startswith('.partial')], [])

    def test_killed_mid_copy_the_copy_stays_incomplete_and_the_guard_starts_the_old_build(self):
        self.rehearsed()
        run = self.kill_after('cold-copy', 'started')
        copy = self.started_data('stopping', run)['copy']
        self.guard(run['id'])
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertNotIn('update', self.names('started'))
        self.assertFalse(os.path.exists(os.path.join(copy, coldcopy.MANIFEST)))

    def test_killed_after_the_install_before_the_update_the_guard_starts_the_old_build(self):
        self.rehearsed()
        run = self.kill_after('install-target', 'done')
        self.guard(run['id'])
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(self.done('back-start')[0]['version'], fx.LIVE)

    def test_killed_after_the_update_before_verify_the_guard_goes_back(self):
        self.rehearsed()
        run = self.kill_after('update', 'done')
        self.assertEqual(self.server()['version'], fx.TARGET)
        self.guard(run['id'])
        self.assertEqual(self.last_run()['end']['result'], 'failed')
        self.assert_rolled_back()

    def test_killed_mid_verify_the_guard_goes_back(self):
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('verify', 'started')
        self.guard(run['id'])
        self.assert_rolled_back()

    def test_after_a_reboot_a_back_by_hand_finds_the_switch_copy_and_the_kept_runtime(self):
        # A reboot ends the guard and its timer. The journal still shows the switch unfinished: check refuses, and
        # `t3-golive back --way w1` by hand takes the switch's own copy and the kept old runtime.
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('update', 'done')
        self.assertEqual(check.run_check(config.load(), self.adapter)[0], 1)
        copy, at = self.copy_at(run)
        w1 = back.ways(config.load(), self.adapter)['w1']
        self.assertTrue(w1['ok'], w1['why'])
        self.assertEqual((w1['copy'], w1['version']), (copy, fx.LIVE))
        self.assertEqual(w1['kept'], keptruntime.kept_dir(config.load(), fx.LIVE))
        with self.assertRaises(journal.Refused):
            back.run_back(config.load(), self.adapter, 'w1', say=self.said.append)  # W1 by hand needs the time
        got = back.run_back(config.load(), self.adapter, 'w1', lose_work_since=at, say=self.said.append)
        self.assertEqual(got['version'], fx.LIVE)
        self.assertEqual(self.server()['version'], fx.LIVE)
        self.assertEqual(sha(self.m.path('home/userdata/statev2.sqlite')), self.db_before)
        self.assertEqual(self.done('runtime')[0]['putBack'], True)
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'])))

    def test_killed_mid_way_back_the_guard_does_not_redo_it_and_a_back_by_hand_finishes_it(self):
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('restore', 'started')
        did = ' '.join(self.guard(run['id']))
        self.assertIn('not run again', did)
        self.assertIn('no post hook ran', did)
        self.assertNotIn('post', self.calls())
        self.assertEqual(self.downgrades(), [])
        self.assertFalse(self.adapter.service()['active'])
        self.assertFalse(self.adapter.service()['enabled'])
        run = self.last_run()
        self.assertEqual(run['end']['result'], 'failed')
        self.assertTrue(switch.w1_holds(run['steps']))
        _, at = self.copy_at(run)
        got = back.run_back(config.load(), self.adapter, 'w1', lose_work_since=at, say=self.said.append)
        self.assertEqual(got['version'], fx.LIVE)
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertEqual(sha(self.m.path('home/userdata/statev2.sqlite')), self.db_before)
        self.assertEqual(self.calls()[-1], 'post')

    def test_killed_after_w1_planned_before_it_stopped_t3_the_guard_runs_w1(self):
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('plan', 'done')
        self.assertNotIn('stop-service', self.names('started', run))
        self.guard(run['id'])
        self.assertEqual(len(self.downgrades()), 1)
        self.assert_rolled_back()

    def test_killed_after_w1_stopped_t3_a_second_guard_does_nothing(self):
        self.rehearsed()
        self.mode = 'never'
        run = self.kill_after('update', 'started', nth=2)  # W1's downgrade started; the kill came before it ran
        self.guard(run['id'])
        n = len(self.calls())
        self.assertIn('nothing to do', ' '.join(self.guard(run['id'], wait=True)))
        self.assertEqual(len(self.calls()), n)
        self.assertFalse(self.adapter.service()['enabled'])

    def test_killed_after_verify_before_the_post_hooks_the_guard_runs_them(self):
        self.rehearsed()
        run = self.kill_after('verify', 'done')
        self.assertNotIn('post', self.calls())
        self.guard(run['id'])
        self.assertEqual(self.calls()[-1], 'post')
        self.assertEqual(self.server()['version'], fx.TARGET)
        self.assertNotIn('stop-service', self.names('started'))

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
        self.assertNotIn('plan', self.names('started'))
        self.guard(run['id'])
        self.assert_rolled_back()

    def test_a_guard_for_an_older_run_does_nothing(self):
        self.rehearsed()
        self.switch()
        self.assertIn('nothing to do', ' '.join(self.guard('some-older-run')))

    def test_a_missing_kept_runtime_or_copy_refuses_w1_and_leaves_the_target_running(self):
        for i, lose in enumerate(('kept', 'copy')):
            with self.subTest(lose):
                if journal.unfinished(self.m.state):
                    journal.settle(self.m.state, 'test: put right')
                self.m.t3_home(fx.LIVE)
                self.adapter._svc(ActiveState='active', MainPID=os.getpid(), UnitFileState='enabled')
                self.m.set_report(self.before)
                self.rehearsed()
                with mock.patch.object(coldcopy, '_stamp', return_value=f'2099010{i + 1}T000000Z'):
                    run = self.kill_after('update', 'done')
                if lose == 'kept':
                    shutil.rmtree(os.path.join(self.m.state, 'kept-runtimes'))
                else:
                    os.unlink(os.path.join(self.copy_at(run)[0], coldcopy.MANIFEST))
                did = ' '.join(self.guard(run['id']))
                self.assertIn('putting T3 back failed', did)
                self.assertIn('nothing was changed', did)
                self.assertEqual(self.server()['version'], fx.TARGET)
                self.assertNotIn('stop-service', self.names('started'))
                self.assertEqual(self.calls()[-1], 'post')
                self.assertEqual(self.last_run()['end']['result'], 'failed')


class Pieces(unittest.TestCase):
    def test_t3_runs_with_a_fixed_environment_only(self):
        root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, root)
        script = os.path.join(root, 'printenv')
        with open(script, 'w', encoding='utf-8') as f:
            f.write('#!/bin/sh\nenv\n')
        os.chmod(script, 0o755)
        with mock.patch.dict(os.environ, {'T3CODE_RELEASE_BASE_URL': 'http://elsewhere.invalid/',
                                          'HOME': root, 'SOME_TOKEN': 'x'}):
            code, out = REAL_T3(script, [], 10)
        self.assertEqual(code, 0)
        self.assertIn(f'HOME={root}', out)
        self.assertNotIn('T3CODE_RELEASE_BASE_URL', out)
        self.assertNotIn('SOME_TOKEN', out)

    def test_answers_sees_any_http_reply_and_no_listener(self):
        m = bx.Machine()
        self.addCleanup(m.close)
        self.assertTrue(switch.answers(m.port))
        self.assertFalse(switch.answers(bx.closed_port()))
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

    def test_the_command_line_wires_switch_and_switch_guard(self):
        m = fx.Machine()
        self.addCleanup(m.close)
        m.standard()
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(check.main(['switch', '--owner-says-go'], out=out, err=err), 0, err.getvalue())
        [call] = m.calls('systemd-run-calls.jsonl')
        self.assertEqual(call[call.index('--') + 1:], [coldcopy.KIT_BIN, 'switch', '--here', '--owner-says-go'])
        self.assertEqual(check.main(['switch-guard', '--run', 'no-such-run'], out=out, err=err), 0)
        self.assertIn('nothing to do', out.getvalue())
        for gone in (['back', '--auto', '--run', 'no-such-run'], ['switch-guard']):
            with self.subTest(gone), self.assertRaises(SystemExit):
                check.main(gone, out=out, err=io.StringIO())


if __name__ == '__main__':
    unittest.main()
