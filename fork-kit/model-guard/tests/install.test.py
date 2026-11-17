"""model-guard-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as
root), and the self-check that reports a machine where the hook is not installed. No root, no network.

  python3 -I -B fork-kit/model-guard/tests/install.test.py
"""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA1, SHA2 = 'a' * 40, 'b' * 40
sys.path.insert(0, os.path.join(KIT, 'lib'))
import model_guard  # noqa: E402


def load():
    path = os.path.join(KIT, 'sbin', 'model-guard-install')
    loader = importlib.machinery.SourceFileLoader('model_guard_install', path)
    spec = importlib.util.spec_from_loader('model_guard_install', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Install(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='model-guard-install-test-'))
        os.chmod(self.root, 0o755)
        self.addCleanup(self.close)
        self.m = load()
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.root, ignore_errors=True)

    def path(self, p):
        return self.root + p

    def run_main(self, *argv, stop_after=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = self.m.main(list(argv), stop_after=stop_after)
            except SystemExit as e:
                code = str(e)
        return code, out.getvalue(), err.getvalue()

    def install(self, sha=SHA1, stop_after=None, source=KIT):
        return self.run_main('install', '--source', source, '--sha', sha, stop_after=stop_after)

    def snapshot(self):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full[len(self.root):]] = (st.st_mode, st.st_mtime_ns,
                                              os.readlink(full) if os.path.islink(full) else None)
        return out

    def turn_on(self, settings=True):
        """What the person who runs the machine does after install: machine file, policy, settings entry."""
        os.makedirs(self.path('/etc/fork-kit'), exist_ok=True)
        policy = self.path('/etc/fork-kit/model-policy.json')
        shutil.copy(os.path.join(KIT, 'model-policy.example.json'), policy)
        os.chmod(policy, 0o644)
        listed = []
        if settings:
            listed = [self.path('/home/a/.claude/settings.json')]
            os.makedirs(os.path.dirname(listed[0]))
            entry = {'matcher': model_guard.HOOK_MATCHER,
                     'hooks': [{'type': 'command', 'command': model_guard.HOOK_COMMAND, 'timeout': 10}]}
            with open(listed[0], 'w') as f:
                json.dump({'hooks': {'PreToolUse': [entry]}}, f)
        with open(self.path('/etc/fork-kit/machine.json'), 'w') as f:
            json.dump({'version': 1, 'model-guard': {'policyFile': policy, 'claudeSettings': listed}}, f)
        os.chmod(self.path('/etc/fork-kit/machine.json'), 0o644)
        return listed

    def test_fresh_install_links_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('"Turn it on"', out)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/model-guard')),
                         '/usr/local/libexec/model-guard/current/bin/model-guard')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                         f'/usr/local/libexec/model-guard/versions/{SHA1}')
        self.assertFalse(os.path.exists(self.path('/etc/fork-kit')))       # never writes policy or machine file
        self.turn_on()
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 0, out)
        self.assertIn('mode advise', out)
        # the installed hook runs from its own version folder and refuses when it finds no policy
        r = subprocess.run([sys.executable, '-I', '-B',
                            self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}/bin/model-guard')],
                           input='{"tool_name": "Agent", "tool_input": {}}', capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root, mode=0o755)
                code, _, _ = self.install(stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.turn_on()
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_foreign_link_is_refused_before_anything_is_written(self):
        os.makedirs(self.path('/usr/local/bin'))
        os.symlink('/somewhere/else', self.path('/usr/local/bin/model-guard'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not the kit\'s link', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
        self.assertFalse(os.path.exists(self.path('/etc/model-guard')))

    def test_changed_copy_is_reported_and_made_again(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        with open(self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}/lib/model_guard.py'), 'a') as f:
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('differs from what was installed', out)
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = os.path.join(self.root, 'src', 'model-guard')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__', 'tests'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'src', 'common'),
                        ignore=shutil.ignore_patterns('__pycache__', 'tests'))
        with open(os.path.join(src, 'lib', 'model_guard.py'), 'a') as f:   # a guard that lets everything through
            f.write('\ndef run_hook(stdin, stdout, machine_path=None, owner_uid=0):\n    return 0\n')
        code, _, err = self.install(sha=SHA2, source=src)
        self.assertEqual(code, 1)
        self.assertIn('did not refuse a call with no policy', err)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                         f'/usr/local/libexec/model-guard/versions/{SHA1}')

    def test_upgrade_switches_and_keeps_the_old_version(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                         f'/usr/local/libexec/model-guard/versions/{SHA2}')
        self.assertTrue(os.path.isdir(self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}')))
        self.turn_on()
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_uninstall_removes_the_kit_and_runs_again(self):
        self.assertEqual(self.install()[0], 0)
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertIn('remove that PreToolUse entry', out)
        for path in ('/usr/local/bin/model-guard', '/usr/local/sbin/model-guard-install',
                     '/usr/local/libexec/model-guard', '/etc/model-guard'):
            self.assertFalse(os.path.lexists(self.path(path)), path)
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def test_bad_input_and_not_root_are_refused(self):
        self.assertEqual(self.install(sha='abc')[0], 1)
        self.assertEqual(self.install(source=os.path.join(self.root, 'nothing'))[0], 1)
        self.m.ROOT_UID = os.geteuid() + 1
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.run_main('uninstall')[0], 1)
        self.assertFalse(os.path.exists(self.path('/etc/model-guard')))

    # ---- the drift check: a machine where the hook is not installed or not turned on

    def test_self_check_reports_a_machine_without_the_kit(self):
        self.turn_on()
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('the kit is not installed', out)

    def test_self_check_reports_a_settings_file_without_the_hook(self):
        self.assertEqual(self.install()[0], 0)
        listed = self.turn_on()
        with open(listed[0], 'w') as f:
            json.dump({'hooks': {}}, f)
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('no PreToolUse hook runs /usr/local/bin/model-guard', out)
        os.unlink(listed[0])
        code, out, _ = self.run_main('self-check')
        self.assertIn('cannot be read', out)

    def test_self_check_reports_no_settings_listed_and_no_policy(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on(settings=False)
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('lists no claudeSettings', out)
        os.unlink(self.path('/etc/fork-kit/model-policy.json'))
        code, out, _ = self.run_main('self-check')
        self.assertIn('every checked call is refused', out)
        os.unlink(self.path('/etc/fork-kit/machine.json'))
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('every checked call is refused', out)


if __name__ == '__main__':
    unittest.main(verbosity=2)
