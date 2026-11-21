"""agent-guards-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): the Claude Code managed settings drop-in (--settings / --no-settings). What every kit's installer does
(copy, switch, links, record, self-check, stops half-way, uninstall) and the shared bootstrap are tested once for
every kit in fork-kit/common/tests/kit_install.test.py. The rule tested here: while the drop-in is there, every hook
it names is there too (a missing actions-disable-guard refuses every Bash command), whatever step a run stops at.
No root, no network.

  python3 -I -B fork-kit/agent-guards/tests/install.test.py
"""
import json, os, shutil, sys, unittest

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

    def fresh(self):
        shutil.rmtree(self.root)
        os.makedirs(self.root)


if __name__ == '__main__':
    unittest.main()
