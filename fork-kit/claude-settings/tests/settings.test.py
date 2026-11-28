"""claude-settings (fork-kit/claude-settings): merge a config's keys into Claude Code settings files, run as a
person runs it, on stand-in settings files in a temporary folder (no real settings are read).

  python3 -I -B fork-kit/claude-settings/tests/settings.test.py

The state question: a second run changes nothing (no write, no backup); a file that is missing, empty, not JSON
or changed while the tool runs; other keys and other skillOverrides entries kept; a run stopped before the
rename leaves the old file whole (the tool writes a temp file and renames it).
"""
import io, json, os, shutil, stat, subprocess, sys, tempfile, unittest
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOL = os.path.join(KIT, 'bin', 'claude-settings')


def load_tool():
    loader = SourceFileLoader('claude_settings', TOOL)
    module = module_from_spec(spec_from_loader('claude_settings', loader))
    loader.exec_module(module)
    return module


cs = load_tool()

WANTED = {
    'disableClaudeAiConnectors': True,
    'skillOverrides': {'claude-approvals': 'user-invocable-only', 'loop': 'user-invocable-only'},
}


class Case(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='t3-claude-settings-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.config = self.path('config.json')
        self.write_json(self.config, {'version': 1, 'settings': WANTED})
        self.settings = self.path('settings.json')

    def path(self, name):
        return os.path.join(self.dir, name)

    def write_json(self, path, value):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(value, f)

    def raw(self):
        with open(self.settings, 'rb') as f:
            return f.read()

    def read_json(self, path):
        with open(path, encoding='utf-8') as f:
            return json.load(f)

    def run_tool(self, *args, files=None):
        out, err = io.StringIO(), io.StringIO()
        argv = list(args) + ['--config', self.config]
        for f in files if files is not None else [self.settings]:
            argv += ['--file', f]
        code = cs.main(argv, out, err)
        return code, out.getvalue(), err.getvalue()

    def backups(self):
        return sorted(n for n in os.listdir(self.dir) if '.bak-claude-settings-' in n)


class Apply(Case):
    def test_merges_and_keeps_other_keys(self):
        self.write_json(self.settings, {
            'hooks': {'Stop': []}, 'env': {'SECRET_VALUE': 'do-not-print'},
            'skillOverrides': {'mine': 'off', 'loop': 'on'}})
        code, out, err = self.run_tool('apply')
        self.assertEqual((code, err), (0, ''))
        self.assertEqual(self.read_json(self.settings), {
            'hooks': {'Stop': []}, 'env': {'SECRET_VALUE': 'do-not-print'},
            'skillOverrides': {'mine': 'off', 'loop': 'user-invocable-only', 'claude-approvals': 'user-invocable-only'},
            'disableClaudeAiConnectors': True})
        self.assertIn('set skillOverrides.loop', out)
        self.assertIn('set skillOverrides.claude-approvals', out)
        self.assertIn('set disableClaudeAiConnectors', out)
        self.assertNotIn('do-not-print', out)
        self.assertNotIn('user-invocable-only', out)  # keys only, never values
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(self.dir, self.backups()[0]), encoding='utf-8') as f:
            self.assertEqual(json.load(f)['skillOverrides'], {'mine': 'off', 'loop': 'on'})

    def test_second_run_changes_nothing(self):
        self.write_json(self.settings, {'model': 'x'})
        self.assertEqual(self.run_tool('apply')[0], 0)
        before = os.stat(self.settings).st_mtime_ns, self.raw()
        code, out, _ = self.run_tool('apply')
        self.assertEqual(code, 0)
        self.assertIn('in step', out)
        self.assertEqual((os.stat(self.settings).st_mtime_ns, self.raw()), before)
        self.assertEqual(len(self.backups()), 1)
        self.assertEqual(self.run_tool('check')[0], 0)

    def test_missing_file_is_created_private(self):
        code, out, _ = self.run_tool('apply')
        self.assertEqual(code, 0)
        self.assertEqual(self.read_json(self.settings), WANTED)
        self.assertEqual(self.backups(), [])
        if os.name == 'posix':
            self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o600)

    def test_empty_file(self):
        open(self.settings, 'w').close()
        self.assertEqual(self.run_tool('apply')[0], 0)
        self.assertEqual(self.read_json(self.settings), WANTED)

    def test_bom_is_read(self):
        with open(self.settings, 'w', encoding='utf-8-sig') as f:
            json.dump({'model': 'x'}, f)
        self.assertEqual(self.run_tool('apply')[0], 0)
        self.assertEqual(self.read_json(self.settings)['model'], 'x')

    def test_not_json_is_left_alone(self):
        with open(self.settings, 'w') as f:
            f.write('{"hooks": ')
        code, _, err = self.run_tool('apply')
        self.assertEqual(code, 2)
        self.assertIn('not JSON', err)
        self.assertEqual(self.raw().decode(), '{"hooks": ')
        self.assertEqual(self.backups(), [])

    def test_not_an_object_is_left_alone(self):
        self.write_json(self.settings, [1, 2])
        self.assertEqual(self.run_tool('apply')[0], 2)
        self.assertEqual(self.read_json(self.settings), [1, 2])

    def test_mode_is_kept(self):
        if os.name != 'posix':
            self.skipTest('posix modes')
        self.write_json(self.settings, {})
        os.chmod(self.settings, 0o640)
        self.run_tool('apply')
        self.assertEqual(stat.S_IMODE(os.stat(self.settings).st_mode), 0o640)

    def test_link_stays_a_link(self):
        if os.name != 'posix':
            self.skipTest('symlinks')
        real = self.path('real.json')
        self.write_json(real, {'a': 1})
        os.symlink(real, self.settings)
        self.assertEqual(self.run_tool('apply')[0], 0)
        self.assertTrue(os.path.islink(self.settings))
        self.assertEqual(self.read_json(real)['a'], 1)
        self.assertTrue(self.read_json(real)['disableClaudeAiConnectors'])

    def test_wrong_type_is_replaced(self):
        self.write_json(self.settings, {'disableClaudeAiConnectors': 1, 'skillOverrides': 'off'})
        self.assertEqual(self.run_tool('apply')[0], 0)
        self.assertEqual(self.read_json(self.settings), WANTED)

    def test_changed_while_running_writes_nothing(self):
        self.write_json(self.settings, {'a': 1})
        real_read = cs.read_settings
        calls = []

        def racing_read(path):
            calls.append(path)
            if len(calls) == 2:
                self.write_json(self.settings, {'a': 2})
            return real_read(path)

        cs.read_settings = racing_read
        self.addCleanup(setattr, cs, 'read_settings', real_read)
        code, _, err = self.run_tool('apply')
        self.assertEqual(code, 2)
        self.assertIn('changed while this ran', err)
        self.assertEqual(self.read_json(self.settings), {'a': 2})
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith('.tmp')], [])

    def test_stop_before_rename_leaves_old_file(self):
        self.write_json(self.settings, {'a': 1})
        real_replace = cs.os.replace

        def stop(*_):
            raise KeyboardInterrupt

        cs.os.replace = stop
        self.addCleanup(setattr, cs.os, 'replace', real_replace)
        with self.assertRaises(KeyboardInterrupt):
            self.run_tool('apply')
        cs.os.replace = real_replace
        self.assertEqual(self.read_json(self.settings), {'a': 1})
        self.assertEqual([n for n in os.listdir(self.dir) if n.endswith('.tmp')], [])
        self.assertEqual(self.run_tool('apply')[0], 0)
        self.assertEqual(self.read_json(self.settings)['a'], 1)

    def test_one_bad_file_does_not_stop_the_others(self):
        bad = self.path('bad.json')
        with open(bad, 'w') as f:
            f.write('nope')
        code, _, _ = self.run_tool('apply', files=[bad, self.settings])
        self.assertEqual(code, 2)
        self.assertEqual(self.read_json(self.settings), WANTED)

    def test_missing_folder_is_refused(self):
        code, _, err = self.run_tool('apply', files=[self.path('nope/settings.json')])
        self.assertEqual(code, 2)
        self.assertIn('is missing', err)


class Check(Case):
    def test_drift_is_reported_and_nothing_written(self):
        self.write_json(self.settings, {'skillOverrides': {'loop': 'user-invocable-only'}})
        before = self.raw()
        code, out, _ = self.run_tool('check')
        self.assertEqual(code, 1)
        self.assertIn('not in step: skillOverrides.claude-approvals', out)
        self.assertIn('not in step: disableClaudeAiConnectors', out)
        self.assertNotIn('skillOverrides.loop', out)
        self.assertEqual(self.raw(), before)

    def test_missing_file_is_drift(self):
        self.assertEqual(self.run_tool('check')[0], 1)
        self.assertFalse(os.path.exists(self.settings))


class Config(Case):
    def bad(self, value, word):
        self.write_json(self.config, value)
        code, _, err = self.run_tool('apply')
        self.assertEqual(code, 2)
        self.assertIn(word, err)
        self.assertFalse(os.path.exists(self.settings))

    def test_bad_configs(self):
        self.bad({'version': 2, 'settings': WANTED}, 'version')
        self.bad({'version': 1, 'settings': {}}, 'non-empty')
        self.bad({'version': 1, 'settngs': WANTED}, 'unknown keys')
        self.bad([1], 'not a JSON object')
        os.unlink(self.config)
        code, _, err = self.run_tool('apply')
        self.assertEqual(code, 2)
        self.assertIn('cannot read', err)

    def test_example_config_loads(self):
        self.assertTrue(cs.load_config(os.path.join(KIT, 'claude-settings.example.json')))


class Command(Case):
    def test_usage(self):
        for argv in ([], ['apply'], ['nope', '--config', 'x'], ['apply', '--config']):
            self.assertEqual(cs.main(argv, io.StringIO(), io.StringIO()), 2, argv)
        self.assertEqual(cs.main(['--help'], io.StringIO(), io.StringIO()), 0)

    def test_default_file(self):
        env = dict(os.environ, CLAUDE_CONFIG_DIR=self.dir)
        result = subprocess.run([sys.executable, '-I', '-B', TOOL, 'apply', '--config', self.config],
                                env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.read_json(self.settings), WANTED)


if __name__ == '__main__':
    unittest.main()
