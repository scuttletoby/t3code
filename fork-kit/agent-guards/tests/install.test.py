"""agent-guards-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): the Claude Code managed settings drop-in (--settings / --no-settings). What every kit's installer does
(copy, switch, links, record, self-check, stops half-way, uninstall) and the shared bootstrap are tested once for
every kit in fork-kit/common/tests/kit_install.test.py. The rule tested here: while the drop-in is there, every hook
it names is there too (a missing actions-disable-guard refuses every Bash command), whatever step a run stops at.
No root, no network.

  python3 -I -B fork-kit/agent-guards/tests/install.test.py
"""
import json, os, shutil, sys, time, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B: a __pycache__ in an installed version folder is a change
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
sys.path.insert(0, os.path.join(KIT, 'lib'))
import install_fixture as fx  # noqa: E402
from agent_guards import wiring  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
DROP_IN = wiring.DROP_IN
CONFIG = '/etc/agent-guards/config.json'
FOREIGN = {'permissions': {'deny': ['Bash(rm -rf /)']}}


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 'agent-guards-install')

    def setUp(self):
        super().setUp()
        self.boot = int(time.time()) - 3600           # the fake host booted an hour ago
        with open(os.path.join(self.m.PROC, 'stat'), 'w') as f:
            f.write(f'cpu  1 2 3\nbtime {self.boot}\n')
        self.homes = os.path.join(self.host, 'home')
        os.makedirs(self.homes)
        self.m.HOMES = [os.path.join(self.homes, 'agent')]

    def session(self, pid, argv=('claude', '-p'), started=None, exe=None, cwd=None, ppid=1):
        """A fake process in PROC: argv, started at `started` (epoch seconds; default: at boot). started='bad': a
        stat that cannot be read. exe, cwd: the targets of its /proc links; ppid: its parent."""
        d = os.path.join(self.m.PROC, str(pid))
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, 'cmdline'), 'wb') as f:
            f.write(b'\0'.join(a.encode() for a in argv) + b'\0')
        with open(os.path.join(d, 'stat'), 'w') as f:
            if started == 'bad':
                f.write('garbled')
            else:
                ticks = int(((started or self.boot) - self.boot) * os.sysconf('SC_CLK_TCK'))
                f.write(f'{pid} (node) S {ppid} ' + ' '.join(['0'] * 17) + f' {ticks} 0 0\n')
        for name, target in (('exe', exe), ('cwd', cwd)):
            if target:
                os.symlink(target, os.path.join(d, name))
        return d

    def config(self):
        with open(self.path(CONFIG)) as f:
            return json.load(f)

    def drop_in(self):
        """The drop-in's settings object, or None when it is not there."""
        try:
            with open(self.path(DROP_IN)) as f:
                return json.load(f)
        except FileNotFoundError:
            return None

    def write_drop_in(self, data):
        os.makedirs(os.path.dirname(self.path(DROP_IN)), exist_ok=True)
        with open(self.path(DROP_IN), 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def assert_named_hooks_there(self):
        """The rule: a drop-in names only hooks that are installed (links that end in a file)."""
        if not os.path.lexists(self.path(DROP_IN)):
            return
        for _event, _matcher, cmd in wiring.commands_in(self.drop_in()):
            path = cmd.split(';', 1)[0].split('=', 1)[1]
            self.assertTrue(self.is_file(path), f'{DROP_IN} names {path}, which is not there')

    def test_fresh_install_writes_no_drop_in(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config(), {'version': 1, 'settings': False})
        self.assertIsNone(self.drop_in())
        self.assertIn('hooks not in managed settings', out)
        for hook in wiring.HOOKS + ('agent-guards',):
            self.assertTrue(self.is_file(f'/usr/local/bin/{hook}'), hook)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_copy_carries_the_bytecode_of_the_fast_path(self):
        """#1094: the fast path runs the hooks from bytecode, so the copy holds a checked-hash .pyc of each file of
        BYTECODE, made for that file's text, in the manifest (the self-check finds one changed), and the fast path is
        linked beside the hooks."""
        import importlib.util
        self.assertEqual(self.install()[0], 0)
        self.assertTrue(self.is_file(f'/usr/local/bin/{wiring.FAST}'))
        folder = self.resolve('/usr/local/libexec/agent-guards/current')
        with open(os.path.join(folder, 'MANIFEST.sha256')) as f:
            listed = {line.split('  ', 1)[1] for line in f.read().splitlines() if line}
        rels = [f'bin/{h}' for h in wiring.FAST_HOOKS] + ['lib/agent_guards/fast.py', 'lib/agent_guards/quick.py',
                                                          'lib/agent_guards/config.py']
        for rel in rels:
            with self.subTest(rel):
                pyc = os.path.normpath(importlib.util.cache_from_source(rel))
                self.assertIn(pyc, listed)
                with open(os.path.join(folder, rel), 'rb') as f:
                    source = f.read()
                with open(os.path.join(folder, pyc), 'rb') as f:
                    head = f.read(16)
                self.assertEqual(head, importlib.util.MAGIC_NUMBER + (3).to_bytes(4, 'little')
                                 + importlib.util.source_hash(source))
        self.assertEqual(self.install()[0], 0)                       # again: the same copy, nothing made again
        with open(os.path.join(folder, os.path.normpath(importlib.util.cache_from_source(rels[0]))), 'ab') as f:
            f.write(b'x')
        code, out, _err = self.run_main('self-check')
        self.assertNotEqual(code, 0)
        self.assertIn('changed since install', out)

    def test_settings_writes_the_drop_in_with_every_hook(self):
        code, out, err = self.install('--settings')
        self.assertEqual(code, 0, err)
        with open(self.path(DROP_IN)) as f:
            self.assertEqual(f.read(), wiring.drop_in_text())
        self.assertEqual(self.config(), {'version': 1, 'settings': True})
        self.assertIn('hooks in managed settings', out)
        self.assert_named_hooks_there()
        for hook in wiring.HOOKS:
            for event in {e for e, _m, items in wiring.ENTRIES if hook in dict(items)}:
                self.assertIsNotNone(wiring.find(self.drop_in(), hook, event), (hook, event))
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_choice_is_kept_and_no_settings_removes_it(self):
        self.assertEqual(self.install('--settings')[0], 0)
        code, _, err = self.install(sha=SHA2)                       # an update with neither option keeps it
        self.assertEqual(code, 0, err)
        self.assertIsNotNone(self.drop_in())
        code, out, err = self.install('--no-settings', sha=SHA2)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.drop_in())
        self.assertIn(f'removed {DROP_IN}', out)
        self.assertEqual(self.install(sha=SHA1)[0], 0)
        self.assertIsNone(self.drop_in())
        self.assertEqual(self.config()['settings'], False)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_an_unrelated_facts_command_is_left_alone_and_our_own_link_is_kept(self):
        # review round 1: `facts` is a common name; another program there is never replaced or removed
        os.makedirs(self.path('/usr/local/bin'), exist_ok=True)
        with open(self.path('/usr/local/bin/facts'), 'w') as f:
            f.write('#!/bin/sh\necho another program\n')
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('warning: /usr/local/bin/facts is there and is not this kit\'s link: left alone', out)
        self.assertTrue(self.is_file('/usr/local/bin/facts-hook'))
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.process(4242, '/bin/sh', '/usr/local/bin/facts', 'x')    # the other program running: not our code
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        with open(self.path('/usr/local/bin/facts')) as f:
            self.assertEqual(f.read(), '#!/bin/sh\necho another program\n')
        os.unlink(self.path('/usr/local/bin/facts'))
        self.fresh()
        code, out, _ = self.install()                         # nothing there: our link, kept by the next run
        self.assertNotIn('warning', out)
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/facts')))
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/facts')))
        self.assertTrue(self.is_file('/usr/local/bin/facts'))
        self.process(4243, '/usr/bin/python3', '/usr/local/bin/facts', 'x')   # our link running: uninstall waits
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('code of this kit is in use', err)

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install('--settings')[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_a_drop_in_it_did_not_write_is_left_alone(self):
        for name, text in (('other settings', FOREIGN), ('not JSON', '{"hooks": '),
                           ('hooks of another tool', {'hooks': {'Stop': [{'hooks': [
                               {'type': 'command', 'command': '/usr/local/bin/other-hook'}]}]}})):
            with self.subTest(name):
                self.fresh()
                self.write_drop_in(text)
                with open(self.path(DROP_IN)) as f:
                    before = f.read()
                code, _, err = self.install('--settings')
                self.assertEqual(code, 1)
                self.assertIn('was not written by this kit', err)
                code, _, err = self.install('--no-settings')       # leaves it alone, installs the hooks
                self.assertEqual(code, 0, err)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with open(self.path(DROP_IN)) as f:
                    self.assertEqual(f.read(), before)

    def test_a_managed_folder_that_is_a_link_is_refused(self):
        os.makedirs(self.path('/elsewhere'))
        os.makedirs(self.path('/etc'), exist_ok=True)
        os.symlink('/elsewhere', self.path(wiring.MANAGED_DIR))
        code, _, err = self.install('--settings')
        self.assertEqual(code, 1)
        self.assertIn('is not a root-owned folder', err)
        self.assertEqual(os.listdir(self.path('/elsewhere')), [])
        self.assertEqual(self.install('--no-settings')[0], 0)      # the hooks alone do not touch it

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                self.fresh()
                code, _, _ = self.install('--settings', stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                self.assert_named_hooks_there()
                code, _, err = self.install('--settings')
                self.assertEqual(code, 0, err)
                self.assertIsNotNone(self.drop_in())
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_an_update_stopped_half_way_keeps_the_hooks_of_the_drop_in(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                self.fresh()
                self.assertEqual(self.install('--settings')[0], 0)
                code, _, _ = self.install(sha=SHA2, stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                self.assertIsNotNone(self.drop_in())
                self.assert_named_hooks_there()

    def test_uninstall_removes_the_drop_in_before_the_hooks(self):
        n = 0
        while True:
            n += 1
            self.fresh()
            self.assertEqual(self.install('--settings')[0], 0)
            with self.stop_context(n):
                code, _, err, stopped = self.uninstall_stopped_after(n, check=self.assert_named_hooks_there)
                self.assert_named_hooks_there()
                if not stopped:
                    self.assertEqual(code, 0, err)
                    break
                code, _, err = self.run_main('uninstall')          # run again: finishes
                self.assertEqual(code, 0, err)
            self.assertIsNone(self.drop_in())
            self.assertFalse(os.path.lexists(self.path('/usr/local/bin/actions-disable-guard')))
        self.assertGreater(n, 3)

    # lens A/B minor: uninstall removed hook files that running sessions or other settings files still named
    def test_rollback_is_no_settings_and_keeps_the_hooks(self):
        self.assertEqual(self.install('--settings')[0], 0)
        self.session(4242)                                      # a session that started with the hooks
        code, out, err = self.install('--no-settings', sha=SHA2)
        self.assertEqual(code, 0, err)
        self.assertIsNone(self.drop_in())
        for hook in wiring.HOOKS:                               # the running session still finds every hook
            self.assertTrue(self.is_file(f'/usr/local/bin/{hook}'), hook)
        cfg = self.config()
        self.assertEqual(cfg['settings'], False)
        self.assertIsInstance(cfg['settingsOff'], int)
        self.assertGreater(cfg['settingsOff'], time.time() - 60)
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()                                # run again: the time is kept, nothing changes
        self.assertEqual(self.install('--no-settings', sha=SHA2)[0], 0)
        self.assertEqual(self.snapshot(), before)

    def test_uninstall_refused_while_a_session_that_had_the_hooks_runs(self):
        self.assertEqual(self.install('--settings')[0], 0)
        for argv, started in ((('claude', '-p', 'x'), None), (('/usr/bin/node', '/x/@anthropic-ai/claude-code/cli.js'),
                                                              None), (('claude',), 'bad')):
            with self.subTest(argv=argv, started=started):
                d = self.session(4242, argv, started)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1)
                self.assertIn('started while the hooks were in managed settings', err)
                self.assertIn('process 4242', err)
                self.assertIsNone(self.drop_in())               # the drop-in went first: no new session gets them
                self.assertEqual(self.config()['settings'], False)
                for hook in wiring.HOOKS:
                    self.assertTrue(self.is_file(f'/usr/local/bin/{hook}'), hook)
                shutil.rmtree(d)
        self.session(4243, ('bash', '-c', 'claude')) # not Claude Code itself
        self.session(4244, started=time.time() + 5)            # started after the drop-in went: it never had them
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.path('/usr/local/bin/actions-disable-guard')))

    def test_uninstall_refused_while_another_settings_file_names_a_hook(self):
        entry = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
            {'type': 'command', 'command': '/usr/local/bin/actions-disable-guard'}]}]}}
        user = os.path.join(self.m.HOMES[0], '.claude', 'settings.json')
        for where in (self.path(f'{wiring.MANAGED_DIR}/managed-settings.json'),
                      self.path(f'{os.path.dirname(DROP_IN)}/zz-hand.json'), user,
                      os.path.join(self.m.HOMES[0], '.claude-other', 'settings.local.json')):
            with self.subTest(where):
                self.fresh()
                self.assertEqual(self.install('--settings')[0], 0)
                os.makedirs(os.path.dirname(where), exist_ok=True)
                with open(where, 'w') as f:
                    json.dump(entry, f)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1)
                self.assertIn('names /usr/local/bin/actions-disable-guard', err)
                self.assertIn('Remove those entries by hand', err)
                os.unlink(where)
                self.assertEqual(self.config(), {'version': 1, 'settings': True})   # nothing changed
                self.assertIsNotNone(self.drop_in())
                self.assertEqual(self.run_main('self-check')[0], 0)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
        if os.geteuid() != 0:                                    # a file it cannot read may name one
            self.fresh()
            self.assertEqual(self.install('--settings')[0], 0)
            os.makedirs(os.path.dirname(user), exist_ok=True)
            with open(user, 'w') as f:
                f.write('{}')
            os.chmod(user, 0)
            self.addCleanup(os.chmod, user, 0o600)
            code, _, err = self.run_main('uninstall')
            self.assertEqual(code, 1)
            self.assertIn('cannot be read', err)

    def test_uninstall_stopped_after_pending_then_a_session_is_still_refused(self):
        """Stopped between "pending" and the drop-in's removal: the next run removes it, puts the time and still
        counts a session that started while it was there."""
        self.assertEqual(self.install('--settings')[0], 0)
        cfg = dict(self.config(), settings=False, settingsOff='pending')    # the state such a stop leaves
        with open(self.path(CONFIG), 'w') as f:
            json.dump(cfg, f)
        self.assertIsNotNone(self.drop_in())
        self.assert_named_hooks_there()
        self.session(4242)
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIsInstance(self.config()['settingsOff'], int)
        self.assertIsNone(self.drop_in())
        self.assertEqual(self.install('--settings')[0], 0)     # the owner keeps the kit after all: back as it was
        self.assertEqual(self.config(), {'version': 1, 'settings': True})
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_managed_folder_others_can_write_is_refused(self):
        os.makedirs(self.path(wiring.MANAGED_DIR))
        os.chmod(self.path(wiring.MANAGED_DIR), 0o777)
        code, _, err = self.install('--settings')
        self.assertEqual(code, 1)
        self.assertIn('only root can write', err)
        self.assertEqual(os.listdir(self.path(wiring.MANAGED_DIR)), [])

    # PR 847 round 2: uninstall's running-session check (lens B minor 2, lens A minors 1 and 2)
    HAND = '/etc/agent-guards/hand-wired-seen.json'
    ENTRY = {'hooks': {'PreToolUse': [{'matcher': 'Bash', 'hooks': [
        {'type': 'command', 'command': '/usr/local/bin/actions-disable-guard'}]}]}}

    def make(self, rel, text='', mode=0o644):
        path = os.path.join(self.host, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        os.chmod(path, mode)
        return path

    def assert_uninstall_refused(self, *parts):
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        for part in parts:
            self.assertIn(part, err)
        self.assertTrue(self.is_file('/usr/local/bin/actions-disable-guard'))
        return err

    def test_r2_minorB2_a_session_is_found_by_its_program_path_not_its_name(self):
        native = self.make('home/agent/.local/share/claude/versions/2.1.9', '#!/bin/sh\n', 0o755)
        os.makedirs(os.path.join(self.m.HOMES[0], '.local', 'bin'))
        os.symlink(native, os.path.join(self.m.HOMES[0], '.local', 'bin', 'claude'))
        cli = self.make('opt/node_modules/@anthropic-ai/claude-code/cli.js')
        work = os.path.join(self.host, 'work')
        os.makedirs(work)
        os.symlink(cli, os.path.join(work, 'cc.js'))
        other = self.make('opt/elsewhere/agent-bin', '#!/bin/sh\n', 0o755)
        os.unlink(os.path.join(self.m.HOMES[0], '.local', 'bin', 'claude'))
        os.symlink(other, os.path.join(self.m.HOMES[0], '.local', 'bin', 'claude'))   # an install in its own folder
        for argv, exe, cwd in ((('cc', '-p', 'x'), native, None),                    # a renamed link to the build
                               (('foo', '-p'), native + ' (deleted)', None),         # exec -a, updated since
                               (('/usr/bin/node', './cc.js', '-p'), '/usr/bin/node', work),   # node, a link to cli.js
                               (('agent-bin', '-p'), other, None)):                  # the installed `claude`
            with self.subTest(argv=argv):
                self.fresh()
                self.assertEqual(self.install('--settings')[0], 0)
                d = self.session(4242, argv, exe=exe, cwd=cwd)
                self.assert_uninstall_refused('process 4242', 'started while the hooks were in managed settings')
                shutil.rmtree(d)
        self.session(4243, ('bash', '-c', 'claude'), exe='/usr/bin/bash')            # not Claude Code itself
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def test_r2_minorA1_uninstall_run_inside_a_session_that_has_the_hooks_is_refused(self):
        self.assertEqual(self.install('--settings')[0], 0)
        self.session(os.getpid(), ('python3', 'agent-guards-install'), started=time.time(), ppid=5001)
        self.session(5001, ('sudo', 'agent-guards-install'), started=time.time(), ppid=5000)
        self.session(5000, ('claude',), ppid=1)                       # the session whose Bash tool ran sudo
        self.assert_uninstall_refused('inside Claude Code session process 5000',
                                      'Run uninstall from a terminal')
        shutil.rmtree(os.path.join(self.m.PROC, '5000'))              # a session that started after the drop-in went
        self.session(5000, ('claude',), started=time.time() + 5, ppid=1)
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def test_r2_minorA2_after_hand_wired_entries_go_the_next_uninstall_still_waits_for_older_sessions(self):
        user = os.path.join(self.m.HOMES[0], '.claude', 'settings.json')
        for name in ('no drop-in ever', 'drop-in removed earlier'):
            with self.subTest(name):
                self.fresh()
                self.assertEqual(self.install('--settings' if name != 'no drop-in ever' else '--no-settings')[0], 0)
                if name == 'drop-in removed earlier':
                    self.assertEqual(self.install('--no-settings')[0], 0)
                    cfg = dict(self.config(), settingsOff=self.boot + 10)     # removed long ago
                    with open(self.path(CONFIG), 'w') as f:
                        json.dump(cfg, f)
                os.makedirs(os.path.dirname(user), exist_ok=True)
                with open(user, 'w') as f:
                    json.dump(self.ENTRY, f)
                d = self.session(4242, started=self.boot + 100)              # started with the hand-wired entry
                self.assert_uninstall_refused('names /usr/local/bin/actions-disable-guard')
                os.unlink(user)                                               # the owner removes it, runs again
                self.assert_uninstall_refused('process 4242', 'named the hooks')
                self.assert_uninstall_refused('process 4242')                 # and again: still
                shutil.rmtree(d)
                self.assertEqual(self.run_main('uninstall')[0], 0)
                self.assertFalse(os.path.lexists(self.path('/etc/agent-guards')))

    def test_r2_minorA2_without_the_drop_in_ever_on_every_older_session_counts(self):
        self.assertEqual(self.install()[0], 0)
        d = self.session(4242)
        self.assert_uninstall_refused('process 4242', 'drop-in was never on')
        shutil.rmtree(d)
        self.session(4243, started=time.time() + 5)
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def test_r2_minorA2_project_settings_of_a_running_session_are_searched(self):
        proj = os.path.join(self.host, 'proj')
        self.make('proj/.claude/settings.local.json', json.dumps(self.ENTRY))
        os.makedirs(os.path.join(proj, 'sub'))
        self.assertEqual(self.install('--settings')[0], 0)
        d = self.session(4242, started=time.time() + 5, cwd=os.path.join(proj, 'sub'))   # newer: has no drop-in
        err = self.assert_uninstall_refused('names /usr/local/bin/actions-disable-guard')
        self.assertIn(os.path.join(proj, '.claude', 'settings.local.json'), err)
        shutil.rmtree(d)
        self.assertEqual(self.run_main('uninstall')[0], 0)            # a project no session runs in is not read

    def test_r2_state_an_uninstall_stopped_after_the_hand_wired_record_or_a_broken_record_still_counts(self):
        for text in (json.dumps({'seen': self.boot + 200}), 'garbled', '[]', json.dumps({'seen': 'x'})):
            with self.subTest(text):
                self.fresh()
                self.assertEqual(self.install('--settings')[0], 0)
                self.assertEqual(self.install('--no-settings')[0], 0)
                with open(self.path(CONFIG)) as f:
                    cfg = json.load(f)
                with open(self.path(CONFIG), 'w') as f:
                    json.dump(dict(cfg, settingsOff=self.boot + 10), f)
                with open(self.path(self.HAND), 'w') as f:                # what a run stopped after it leaves
                    f.write(text)
                d = self.session(4242, started=self.boot + 100)
                self.assert_uninstall_refused('process 4242')
                shutil.rmtree(d)
                self.assertEqual(self.run_main('uninstall')[0], 0)

    # #931 item 4: the installer points found on an older guard installer, checked here
    LEFTOVER = os.path.join(os.path.dirname(DROP_IN), '.agent-guards.json.k3x_9q2a.tmp')

    def put(self, path, text):
        """A file at host path `path` of the installed tree (self.make writes beside it, in the fake host)."""
        os.makedirs(os.path.dirname(self.path(path)), exist_ok=True)
        with open(self.path(path), 'w') as f:
            f.write(text)
        return self.path(path)

    def leave_temp_copy(self):
        """What a run killed between write_file's write and its rename leaves: a full copy of the drop-in."""
        return self.put(self.LEFTOVER, wiring.drop_in_text())

    def test_931_a_temp_copy_left_by_a_killed_run_is_removed_by_the_next_run(self):
        self.assertEqual(self.install('--settings')[0], 0)
        keep = self.put(os.path.join(os.path.dirname(DROP_IN), '.agent-guards.json.bak'), 'x')
        conf_left = self.put(os.path.join(os.path.dirname(CONFIG), '.config.json.0abc_xyz.tmp'), '{')
        self.leave_temp_copy()
        code, _, err = self.install()   # nothing to write: the leftovers still go
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.path(self.LEFTOVER)))
        self.assertFalse(os.path.lexists(conf_left))
        self.assertTrue(os.path.exists(keep), 'a file with another name is not the kit\'s')
        self.leave_temp_copy()
        self.assertEqual(self.install('--no-settings')[0], 0)
        self.assertFalse(os.path.lexists(self.path(self.LEFTOVER)))

    def test_931_uninstall_is_not_refused_by_the_kits_own_temp_copy(self):
        """A killed install's `.agent-guards.json.XXXXXXXX.tmp` beside the drop-in: no settings file (foreign_settings
        reads the folder's `*`, which leaves dot files out), and settings_off() removes it with the drop-in."""
        self.assertEqual(self.install('--settings')[0], 0)
        self.leave_temp_copy()
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, out + err)
        self.assertFalse(os.path.lexists(self.path(self.LEFTOVER)))
        self.assertFalse(self.is_file('/usr/local/bin/actions-disable-guard'))

    def test_931_keys_added_to_config_by_hand_are_kept(self):
        self.assertEqual(self.install()[0], 0)
        with open(self.path(CONFIG), 'w') as f:
            json.dump({'version': 1, 'settings': False, 'note': 'set by hand', 'n': 100000}, f)
        for extra in (('--settings',), (), ('--no-settings',)):
            with self.subTest(extra):
                code, _, err = self.install(*extra)
                self.assertEqual(code, 0, err)
                self.assertEqual((self.config()['note'], self.config()['n']), ('set by hand', 100000))
        self.assertEqual(self.config()['settings'], False)
        self.assertIsInstance(self.config()['settingsOff'], int)
        self.assertEqual(self.install('--settings')[0], 0)
        self.assertNotIn('settingsOff', self.config(), 'a key of the kit\'s own still goes when the kit drops it')

    def test_931_a_config_with_nan_or_a_key_given_twice_is_refused(self):
        self.assertEqual(self.install()[0], 0)
        for text in ('{"version": 1, "settings": NaN}', '{"version": 1, "settings": Infinity}',
                     '{"version": 1, "settings": true, "settings": false}'):
            with self.subTest(text):
                with open(self.path(CONFIG), 'w') as f:
                    f.write(text)
                code, _, err = self.install()
                self.assertEqual(code, 1, err)
                self.assertIn('cannot be read', err)
                with open(self.path(CONFIG)) as f:
                    self.assertEqual(f.read(), text, 'left as it was')

    def fresh(self):
        shutil.rmtree(self.root)
        os.makedirs(self.root)


if __name__ == '__main__':
    unittest.main()
