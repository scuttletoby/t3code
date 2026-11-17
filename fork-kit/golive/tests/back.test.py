"""t3-golive back (W1, W2, W3) and status on a fake machine (golive_fixture.py, back_fixture.py): the service
manager is faked, the T3 home is a temp folder, a stub t3 acts as the fork build, the old build and an upstream
nightly. Nothing here stops, starts, reads or calls a real T3 or its port.
python3 -I -B fork-kit/golive/tests/back.test.py
"""
import hashlib, io, json, os, shutil, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import golive_fixture as fx  # noqa: E402
import back_fixture as bx  # noqa: E402
from golive import back, check, coldcopy, config, journal, keptruntime  # noqa: E402

# A back run in its own process, SIGKILLed where `setup` says.
KILLED = '''import os, sys
sys.path[:0] = {paths!r}
import back_fixture as bx
from golive import back, coldcopy, config, keptruntime, osadapter
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

    def switch(self, keep=True):
        """A switch to the fork build, journalled as PR 6 will: the counts before it, the old build's runtime kept
        (keptruntime.keep, before the update), the fork build's update (it prunes the old runtime, as upstream's
        does), its verify."""
        with journal.run(self.m.state, 'switch') as r:
            with r.step('check') as facts:
                facts['reportCounts'] = {'projects': 3, 'projectsArchived': 0, 'threads': 40, 'threadsArchived': 2}
            if keep:
                with r.step('keep-runtime', version=fx.LIVE) as facts:
                    facts.update(keptruntime.keep(r, config.load(), fx.LIVE))
            with r.step('update', version=fx.TARGET):
                self.m.switched()
                self.m.upstream_update(fx.TARGET)
            with r.step('verify') as facts:
                facts.update(version=fx.TARGET, pid=self.m.server.pid)
        self.migrated = sha(self.m.path('home/userdata/statev2.sqlite'))
        os.unlink(self.m.path('back-t3-calls.jsonl'))  # the switch's own update is not one of back's

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
        self.switch(keep=False)
        self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{fx.LIVE}')))  # pruned, as upstream does
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn(f'the t3 {fx.LIVE} it was taken under is not kept', ways['w1']['why'])
        self.assertIn('prunes', ways['w1']['why'])
        with self.assertRaisesRegex(journal.Refused, 'w1 is not possible now: .* not kept'):
            self.back('w1', lose_work_since=self.at)

    def test_a_copy_of_another_home_is_never_listed(self):
        self.take_copy()
        path = os.path.join(self.copy, coldcopy.MANIFEST)
        with open(path) as f:
            manifest = json.load(f)
        with open(path, 'w') as f:
            json.dump({**manifest, 'home': self.m.path('another-home')}, f)
        self.assertEqual(back.copies(config.load()), [])
        self.assertIn('no finished copy of this home', back.ways(config.load(), self.adapter)['w1']['why'])

    def test_an_install_complete_naming_another_version_is_not_a_whole_runtime(self):
        self.assertIsNotNone(back.installed_t3(self.m.home, fx.LIVE))
        self.m.write(f'home/runtime/versions/{fx.LIVE}/.install-complete', fx.TARGET)
        self.assertIsNone(back.installed_t3(self.m.home, fx.LIVE))
        os.rename(self.m.path(f'home/runtime/versions/{fx.LIVE}'), self.m.path('elsewhere'))
        self.m.write('elsewhere/.install-complete', fx.LIVE)
        os.symlink(self.m.path('elsewhere'), self.m.path(f'home/runtime/versions/{fx.LIVE}'))
        self.assertIsNone(back.installed_t3(self.m.home, fx.LIVE))  # upstream passes a linked version folder by

    def test_no_report_runs_beside_a_pending_update(self):
        self.switch()
        pending = {'status': 'pending', 'fromVersion': fx.TARGET, 'targetVersion': bx.BASE}
        self.m.write('home/runtime/service-state.json',
                     json.dumps({'protocol': 3, 'activeVersion': fx.TARGET, 'update': pending}))
        report, why = back.live_report(config.load(), self.adapter)
        self.assertIsNone(report)
        self.assertIn('not running steadily', why)
        self.assertEqual([c for c in self.m.calls('back-t3-calls.jsonl') if c[0] == '__fork-report'], [])

    def test_here_is_refused_inside_the_boot_service(self):
        self.take_copy()
        self.switch()
        runs = len(journal.read(self.m.state)[0])
        with mock.patch.object(self.adapter, 'inside_service', return_value=True):
            code, out, err = self.main('back', '--way', 'w1', '--here', '--lose-work-since', self.at)
        self.assertEqual(code, 1)
        self.assertIn('runs inside', err)
        self.assertEqual(len(journal.read(self.m.state)[0]), runs)
        self.assertEqual(self.updates(), [])

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


class KeptRuntime(BackBase):
    """keptruntime: the old build's runtime kept outside the home (the switch calls keep() before its update)."""

    def keep(self):
        with journal.run(self.m.state, 'switch') as r:
            with r.step('keep-runtime') as facts:
                facts.update(keptruntime.keep(r, config.load(), fx.LIVE))
        return facts

    def test_keep_copies_the_runtime_out_of_the_home_and_reads_it_back(self):
        before = self.m.snapshot()
        facts = self.keep()
        self.assertEqual(facts['kept'], os.path.join(self.m.state, 'kept-runtimes', fx.LIVE))
        self.assertEqual(keptruntime.check(config.load(), fx.LIVE), (facts['kept'], None))
        self.assertEqual(sha(os.path.join(facts['kept'], 'runtime', 't3')),
                         sha(self.m.path(f'home/runtime/versions/{fx.LIVE}/t3')))
        self.assertEqual(self.m.snapshot(), before)  # nothing in the home changed

    def test_keep_twice_keeps_once(self):
        self.keep()
        record = os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, keptruntime.RECORD)
        mtime = os.stat(record).st_mtime_ns
        self.assertTrue(self.keep()['already'])
        self.assertEqual(os.stat(record).st_mtime_ns, mtime)

    def test_keep_over_a_damaged_one_sets_it_aside_and_keeps_anew(self):
        kept = self.keep()['kept']
        with open(os.path.join(kept, 'runtime', 't3'), 'ab') as f:
            f.write(b'x')
        facts = self.keep()
        self.assertFalse(facts['already'])
        self.assertTrue(os.path.isdir(facts['setAside']))  # never deleted
        self.assertEqual(keptruntime.check(config.load(), fx.LIVE)[0], kept)

    def test_a_keep_stopped_half_way_is_never_used_and_keep_again_finishes(self):
        def stop(*args):
            raise KeyboardInterrupt
        with mock.patch.object(keptruntime, '_copy_file', side_effect=stop):
            with self.assertRaises(KeyboardInterrupt):
                self.keep()
        self.assertIsNone(keptruntime.check(config.load(), fx.LIVE)[0])
        self.assertIn('kept-runtimes', ' '.join(os.listdir(self.m.state)))
        journal.settle(self.m.state, 'a test stopped the keep')
        self.keep()
        self.assertIsNotNone(keptruntime.check(config.load(), fx.LIVE)[0])
        self.assertEqual(os.listdir(os.path.join(self.m.state, 'kept-runtimes')), [fx.LIVE])

    def test_plan_keep_refuses_before_any_step(self):
        cfg = config.load()
        self.assertGreater(keptruntime.plan_keep(cfg, fx.LIVE), 0)
        with self.assertRaisesRegex(ValueError, 'must stay free'):
            keptruntime.plan_keep(cfg, fx.LIVE, floor_bytes=1 << 62)
        with self.assertRaisesRegex(ValueError, 'nothing to keep'):
            keptruntime.plan_keep(cfg, '0.1.8')
        self.keep()
        self.assertEqual(keptruntime.plan_keep(cfg, fx.LIVE, floor_bytes=1 << 62), 0)  # kept already


    def kept_t3(self):
        return os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, 'runtime', 't3')

    def test_check_finds_a_changed_byte_of_the_same_size(self):
        self.keep()
        with open(self.kept_t3(), 'r+b') as f:
            first = f.read(1)
            f.seek(0)
            f.write(bytes([first[0] ^ 1]))
        self.assertIn('does not match', keptruntime.check(config.load(), fx.LIVE)[1])

    def test_check_refuses_entries_the_record_does_not_name(self):
        self.keep()
        with open(os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, 'runtime', 'extra.so'), 'w') as f:
            f.write('planted')
        self.assertIn('does not name (extra.so)', keptruntime.check(config.load(), fx.LIVE)[1])

    def test_check_refuses_a_record_of_another_version(self):
        self.keep()
        path = os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, keptruntime.RECORD)
        with open(path) as f:
            record = json.load(f)
        with open(path, 'w') as f:
            json.dump({**record, 'version': '0.1.8'}, f)
        self.assertIn(f'is not a record of t3 {fx.LIVE}', keptruntime.check(config.load(), fx.LIVE)[1])

    def test_a_linked_kept_folder_is_never_used(self):
        kept = self.keep()['kept']
        os.rename(kept, self.m.path('elsewhere'))
        os.symlink(self.m.path('elsewhere'), kept)
        self.assertIn('is a link', keptruntime.check(config.load(), fx.LIVE)[1])

    def test_plan_keep_with_no_state_folder_nor_its_parent_is_a_refusal(self):
        cfg = config.load()
        cfg.state_dir = self.m.path('no/such/state')
        with self.assertRaisesRegex(ValueError, 'free disk'):
            keptruntime.plan_keep(cfg, fx.LIVE)
        self.assertFalse(os.path.exists(self.m.path('no')))

    def put_back(self):
        with journal.run(self.m.state, 'back') as r:
            with r.step('runtime') as facts:
                facts.update(keptruntime.put_back(r, config.load(), fx.LIVE))
        return facts

    def test_put_back_sets_an_incomplete_version_folder_aside_and_puts_the_kept_one_back(self):
        self.keep()
        os.unlink(self.m.path(f'home/runtime/versions/{fx.LIVE}/.install-complete'))
        facts = self.put_back()
        self.assertTrue(facts['putBack'])
        self.assertTrue(os.path.isdir(facts['setAside']))  # never deleted
        self.assertIsNotNone(back.installed_t3(self.m.home, fx.LIVE))

    def test_put_back_fails_when_the_runtime_is_not_whole_after_it(self):
        self.keep()
        shutil.rmtree(self.m.path(f'home/runtime/versions/{fx.LIVE}'))
        real, versions = keptruntime._fsync_dir, self.m.path('home/runtime/versions')

        def fsync(path):
            real(path)
            if path == versions:  # something takes the sentinel away right after the rename
                os.unlink(os.path.join(versions, fx.LIVE, keptruntime.SENTINEL))
        with mock.patch.object(keptruntime, '_fsync_dir', fsync):
            with self.assertRaisesRegex(RuntimeError, 'not a whole runtime after the put-back'):
                self.put_back()

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
        self.assertEqual([s for s, _ in steps], ['plan', 'runtime', 'pre-hooks', 'stop-service', 'restore', 'update',
                                                 'verify', 'post-hooks'])
        self.assertEqual(steps[5][1]['t3'], self.m.path(f'home/runtime/versions/{fx.LIVE}/t3'))
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

    def test_after_the_switch_pruned_the_old_runtime_w1_puts_the_kept_one_back(self):
        old = self.m.path(f'home/runtime/versions/{fx.LIVE}')
        self.assertFalse(os.path.exists(old))  # the switch's update pruned it, as upstream's does
        kept = os.path.join(self.m.state, 'kept-runtimes', fx.LIVE)
        self.assertTrue(back.ways(config.load(), self.adapter)['w1']['ok'])
        self.back('w1', lose_work_since=self.at)
        self.assertEqual(sha(os.path.join(old, 't3')), sha(os.path.join(kept, 'runtime', 't3')))
        self.assertEqual(self.updates()[-1][:2], ['update', fx.LIVE])
        self.assertEqual(self.live_version(), fx.LIVE)
        done = [s['data'] for s in self.last_run()['steps'] if s['step'] == 'runtime' and s['phase'] == 'done']
        self.assertTrue(done[0]['putBack'])
        self.assertEqual(keptruntime.check(config.load(), fx.LIVE)[0], kept)  # still kept, never deleted

    def test_w1_after_w2_is_taken_not_called_already_back(self):
        self.m.stage_release(bx.BASE)
        self.back('w2')
        self.assertEqual(self.live_version(), bx.BASE)
        self.assertIsNotNone(keptruntime.check(config.load(), fx.LIVE)[0])  # W2's update left the kept one alone
        result = self.back('w1', lose_work_since=self.at)
        self.assertFalse(result.get('nothing'))
        self.assertEqual(self.live_version(), fx.LIVE)
        self.assertEqual(self.db(), self.copied)
        self.assertTrue(self.back('w1', lose_work_since=self.at)['nothing'])  # and now W1 twice is nothing to do

    def test_w1_after_w2_with_no_kept_runtime_is_refused_naming_why(self):
        self.m.stage_release(bx.BASE)
        self.back('w2')
        shutil.rmtree(os.path.join(self.m.state, 'kept-runtimes'))
        snap, calls = self.m.snapshot(), self.calls()
        with self.assertRaisesRegex(journal.Refused, f'w1 is not possible now: .*t3 {fx.LIVE} .* not kept'):
            self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.m.snapshot(), snap)
        self.assertEqual(self.calls(), calls)

    def test_a_corrupt_or_unfinished_kept_runtime_is_never_used(self):
        kept = os.path.join(self.m.state, 'kept-runtimes', fx.LIVE)
        with open(os.path.join(kept, 'runtime', 't3'), 'ab') as f:
            f.write(b'x')
        ways = back.ways(config.load(), self.adapter)
        self.assertFalse(ways['w1']['ok'])
        self.assertIn('does not match', ways['w1']['why'])
        snap, calls = self.m.snapshot(), self.calls()
        with self.assertRaisesRegex(journal.Refused, 'does not match'):
            self.back('w1', lose_work_since=self.at)
        os.unlink(os.path.join(kept, keptruntime.RECORD))
        self.assertIn('unfinished', back.ways(config.load(), self.adapter)['w1']['why'])
        self.assertEqual(self.m.snapshot(), snap)
        self.assertEqual(self.calls(), calls)

    def test_a_back_after_a_half_done_switch_is_never_already_back(self):
        self.back('w1', lose_work_since=self.at)
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'switch') as r:
                with r.step('check'):
                    pass
                raise RuntimeError('the switch failed half-way')
        with self.assertRaises(journal.Refused):
            self.back('w1', lose_work_since=self.at)
        self.assertTrue(journal.needs_settling(self.last_run()))  # nothing looked at what the switch left: not ok

    def test_back_never_follows_a_stopped_copy(self):
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'copy') as r:
                with r.step('stop-service'):
                    raise RuntimeError('killed')
        with self.assertRaises(journal.Unfinished):
            self.back('w1', lose_work_since=self.at)

    def test_an_older_stopped_back_s_plan_is_not_taken_up_again(self):
        with self.assertRaises(RuntimeError):  # a W2 stopped half-way, finished by a later back (ok)
            with journal.run(self.m.state, 'back') as r:
                with r.step('plan', way='w2', version=bx.BASE, t3=None, copy=None):
                    pass
                raise RuntimeError('stopped')
        with journal.run(self.m.state, 'back', may_follow=back.FOLLOWS) as r:
            pass
        with self.assertRaises(RuntimeError):  # then a switch stopped half-way, with no way back journalled
            with journal.run(self.m.state, 'switch') as r:
                with r.step('check'):
                    pass
                raise RuntimeError('stopped')
        self.back('w1', lose_work_since=self.at)  # not "the last back (w2) stopped half-way"
        self.assertEqual(self.live_version(), fx.LIVE)

    def test_the_hooks_run_before_the_stop_and_after_the_verify(self):
        self.m.hooks('pre-a', 'post-a')
        self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])
        steps = [s['step'] for s in self.last_run()['steps'] if s['phase'] == 'done']
        self.assertLess(steps.index('pre-hooks'), steps.index('stop-service'))
        self.assertGreater(steps.index('post-hooks'), steps.index('verify'))

    def test_a_failing_pre_hook_runs_the_post_hooks_and_never_stops_t3(self):
        self.m.hooks('pre-a', 'post-a')
        self.m.write('hooks.log.pre-a.code', '1')
        calls = self.calls()
        with self.assertRaisesRegex(journal.Refused, 'pre hook failed.*T3 was not stopped'):
            self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])
        self.assertEqual(self.calls(), calls)
        self.assertEqual(self.db(), self.migrated)

    def status(self):
        with mock.patch.object(back, '_http', return_value=(True, 'answers')):
            code, out, err = self.main('status')
        return code, out + err

    def test_a_failed_w1_after_the_stop_leaves_the_jobs_paused_until_a_back_ends_ok(self):
        self.m.hooks('pre-a', 'post-a')
        self.m.write('update.code', '3')
        code, out, err = self.main('back', '--way', 'w1', '--here', '--lose-work-since', self.at)
        self.assertEqual(code, 1)
        self.assertEqual(self.m.hook_log(), ['pre-a'])  # T3 was stopped: no post hook while it stays down
        self.assertIn('T3 stays stopped and is not started at boot', err)
        self.assertIn('background jobs stay paused', err)
        self.assertFalse(self.adapter.service()['enabled'])
        code, out = self.status()
        self.assertIn('background jobs stay paused', out)
        os.unlink(self.m.path('update.code'))
        self.assert_rerun_finishes()
        self.assertEqual(self.m.hook_log(), ['pre-a', 'pre-a', 'post-a'])

    def test_a_post_hook_failing_after_the_verify_is_finished_by_a_rerun_that_runs_only_the_post_hooks(self):
        self.m.hooks('pre-a', 'post-a')
        self.m.write('hooks.log.post-a.code', '1')
        code, out, err = self.main('back', '--way', 'w1', '--here', '--lose-work-since', self.at)
        self.assertEqual(code, 1)
        self.assertIn('a post hook failed', err)
        self.assertIn('runs only the post hooks', err)
        self.assertNotIn('stays stopped', err)
        self.assertTrue(self.adapter.service()['enabled'])
        code, out = self.status()
        self.assertEqual(code, 1, out)
        self.assertIn('only its post hooks are left', out)
        self.assertNotIn('stays stopped', out)
        self.assert_rerun_runs_only_what_is_left(['follows', 'resume', 'post-hooks'])
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a', 'post-a'])

    def assert_rerun_runs_only_what_is_left(self, steps):
        """T3 serves new work after the stopped back; the rerun must neither stop T3 nor restore the copy again."""
        self.m.write('home/userdata/statev2.sqlite', b'SQLite format 3\0' + b'new work' * 700)
        work, calls, updates = self.db(), self.calls(), self.updates()
        if os.path.exists(self.m.path('hooks.log.post-a.code')):
            os.unlink(self.m.path('hooks.log.post-a.code'))
        self.assertEqual(self.back('w1', lose_work_since=self.at)['version'], fx.LIVE)
        self.assertEqual(self.db(), work)
        self.assertNotIn('stop', self.calls()[len(calls):])
        self.assertNotIn('disable', self.calls()[len(calls):])
        self.assertEqual(self.updates(), updates)
        self.assertEqual([s['step'] for s in self.last_run()['steps'] if s['phase'] == 'started'], steps)
        self.assertEqual(self.last_run()['end']['result'], 'ok')
        self.assertTrue(self.adapter.service()['enabled'])
        self.assertTrue(self.back('w1', lose_work_since=self.at)['nothing'])  # and then nothing is left

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

    def test_stopped_mid_put_back_of_the_kept_runtime_then_run_again(self):
        self.killed('real = keptruntime._copy_file\n'
                    'def copy(src, dst, mode):\n    real(src, dst, mode)\n    kill()\n'
                    'keptruntime._copy_file = copy')
        self.assertFalse(os.path.exists(self.m.path(f'home/runtime/versions/{fx.LIVE}')))
        self.assert_rerun_finishes()

    def test_stopped_mid_downgrade_then_run_again(self):
        self.m.write('kill-after-update', '')
        self.killed('')
        os.unlink(self.m.path('kill-after-update'))
        self.assert_rerun_finishes()


    def test_killed_between_the_verify_and_the_post_hooks_then_run_again_runs_only_the_post_hooks(self):
        # In this process (the subprocess's fake service cannot start a server for the verify): stopped by a
        # BaseException at the post hooks, then the run's end line is cut, as a kill leaves it.
        self.m.hooks('pre-a', 'post-a')
        real = coldcopy._hooks

        def hooks(run, name, *a):
            if name == 'post-hooks':
                raise SystemExit('killed')
            return real(run, name, *a)
        with mock.patch.object(coldcopy, '_hooks', hooks), self.assertRaises(SystemExit):
            self.back('w1', lose_work_since=self.at)
        path = os.path.join(self.m.state, 'journal.jsonl')
        with open(path, 'rb') as f:
            lines = f.read().splitlines(keepends=True)
        self.assertEqual(json.loads(lines[-1])['event'], 'end')
        with open(path, 'wb') as f:
            f.writelines(lines[:-1])
        self.assertIsNone(self.last_run()['end'])
        self.assertEqual(self.m.hook_log(), ['pre-a'])
        self.assert_rerun_runs_only_what_is_left(['follows', 'resume', 'post-hooks'])
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])

    def test_killed_between_the_update_and_the_verify_then_run_again_verifies_without_a_second_restore(self):
        self.m.hooks('pre-a', 'post-a')
        self.killed('coldcopy._wait_started = kill')
        self.assertEqual(self.m.hook_log(), ['pre-a'])
        self.assert_rerun_runs_only_what_is_left(['follows', 'resume', 'verify', 'post-hooks'])
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])

    def test_killed_after_the_update_with_another_build_named_since_redoes_the_whole_w1(self):
        self.killed('coldcopy._wait_started = kill')
        os.unlink(self.m.path('start-requested'))
        self.m.write('home/runtime/service-state.json', json.dumps({'protocol': 3, 'activeVersion': fx.TARGET}))
        calls = self.calls()
        self.assert_rerun_finishes()
        self.assertIn('stop', self.calls()[len(calls):])  # not resumed: the state no longer names t3 LIVE

    def test_a_resumed_w1_whose_verify_fails_runs_no_post_hook(self):
        self.m.hooks('pre-a', 'post-a')
        self.killed('coldcopy._wait_started = kill')
        os.unlink(self.m.path('start-requested'))  # T3 does not come back
        code, out, err = self.main('back', '--way', 'w1', '--here', '--lose-work-since', self.at)
        self.assertEqual(code, 1)
        self.assertIn('did not come back', err)
        self.assertIn('background jobs stay paused', err)
        self.assertEqual(self.m.hook_log(), ['pre-a'])
        self.assertEqual([s['step'] for s in self.last_run()['steps'] if s['phase'] == 'started'],
                         ['follows', 'resume', 'verify'])

    def test_a_kept_file_changed_after_the_check_is_refused_by_the_put_back(self):
        real, kept_t3 = keptruntime._copy_tree, os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, 'runtime', 't3')

        def changed_first(src, dst, entries, *a, **kw):
            with open(kept_t3, 'r+b') as f:  # same size: only the hash tells
                first = f.read(1)
                f.seek(0)
                f.write(bytes([first[0] ^ 1]))
            return real(src, dst, entries, *a, **kw)
        calls = self.calls()
        with mock.patch.object(keptruntime, '_copy_tree', changed_first):
            with self.assertRaisesRegex(RuntimeError, 'changed since it was checked'):
                self.back('w1', lose_work_since=self.at)
        self.assertEqual(self.calls(), calls)  # T3 never stopped
        self.assertIsNone(back.installed_t3(self.m.home, fx.LIVE))
        self.assertEqual(self.db(), self.migrated)

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

    def test_w2_runs_the_hooks_around_the_update(self):
        self.m.hooks('pre-a', 'post-a')
        self.m.stage_release(bx.BASE)
        self.back('w2')
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])
        steps = [s['step'] for s in self.last_run()['steps'] if s['phase'] == 'done']
        self.assertEqual(steps, ['plan', 'pre-hooks', 'update', 'verify', 'post-hooks'])

    def test_a_failed_w2_still_runs_the_post_hooks(self):
        self.m.hooks('pre-a', 'post-a')
        self.m.stage_release(bx.BASE)
        self.m.write('update.code', '3')
        with self.assertRaisesRegex(RuntimeError, 'exited 3'):
            self.back('w2')
        self.assertEqual(self.m.hook_log(), ['pre-a', 'post-a'])

    def test_a_rerun_of_a_stopped_w2_whose_t3_is_gone_is_refused_naming_it(self):
        self.m.stage_release(bx.BASE)
        self.m.write('update.code', '3')
        with self.assertRaises(RuntimeError):
            self.back('w2')
        os.unlink(self.m.path('update.code'))
        shutil.rmtree(self.m.path(f'stage/{bx.BASE}'))
        with self.assertRaisesRegex(journal.Refused, f'stopped back.s t3 {bx.BASE} is not available any more'):
            self.back('w2')
        self.assertEqual(len(self.updates()), 1)
        self.assertTrue(journal.needs_settling(self.last_run()))

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

    def test_a_failed_back_is_named_by_status_never_up(self):
        self.live_report()
        self.m.stage_release(bx.BASE)
        self.m.write('update.code', '3')  # upstream fails before it touches the service: the fork build still runs
        with self.assertRaises(RuntimeError):
            self.back('w2')
        with mock.patch.object(back, '_http', return_value=(True, 'answers')):
            code, out = self.status()
        self.assertEqual(code, 1, out)
        self.assertIn('FAIL  journal: the last run (back', out)
        self.assertIn('run `t3-golive back --way w2` again: it runs the update', out)
        self.assertTrue(out.startswith(f'T3 is up but not right (journal): t3 {fx.TARGET}'), out)

    def test_status_writes_no_lock_file(self):
        os.unlink(os.path.join(self.m.state, 'lock'))
        self.status()
        self.assertFalse(os.path.exists(os.path.join(self.m.state, 'lock')))

    def test_status_with_no_state_folder_makes_none(self):
        shutil.rmtree(self.m.state)
        self.status()
        self.assertFalse(os.path.exists(self.m.state))

    def test_a_damaged_kept_runtime_is_up_but_w1_not_available_never_ok(self):
        self.live_report()
        with open(os.path.join(self.m.state, 'kept-runtimes', fx.LIVE, 'runtime', 't3'), 'ab') as f:
            f.write(b'x')
        before = self.m.snapshot()
        with mock.patch.object(back, '_http', return_value=(True, 'answers')):
            code, out = self.status()
        self.assertEqual(code, 1, out)
        self.assertTrue(out.startswith(f'T3 is up but not right (way back W1): t3 {fx.TARGET}'), out)
        self.assertIn('FAIL  way back W1: not available', out)
        self.assertEqual(self.m.snapshot(), before)

    def test_after_a_half_done_switch_the_hint_is_to_take_a_way_back(self):
        with self.assertRaises(RuntimeError):
            with journal.run(self.m.state, 'switch') as r:
                with r.step('check'):
                    pass
                raise RuntimeError('the switch failed half-way')
        code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('FAIL  journal: the last run (switch', out)
        self.assertNotIn('run the same', out)
        self.assertIn('take a way back', out)

    def test_http_asks_localhost_only_whatever_origin_the_runtime_file_names(self):
        self.m.runtime_file(pid=self.m.server.pid, serverVersion=fx.TARGET, port=self.m.port,
                            origin='http://192.0.2.1:9')
        asked = []

        def opened(opener, url, timeout=None):
            asked.append(url)
            raise OSError('not asked for real here')
        with mock.patch.object(back.urllib.request.OpenerDirector, 'open', opened):
            self.status()
        self.assertEqual(asked, [f'http://127.0.0.1:{self.m.port}/'])

    def test_kept_runtimes_are_listed(self):
        code, out = self.status()
        self.assertIn(f'kept runtime of t3 {fx.LIVE}', out)

    def test_a_crash_inside_status_is_a_failure_not_a_crash(self):
        with mock.patch.object(back.live, 'live_server', side_effect=ValueError('boom')):
            code, out = self.status()
        self.assertEqual(code, 1)
        self.assertIn('boom', out)


if __name__ == '__main__':
    unittest.main()
