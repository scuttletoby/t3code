"""model-guard-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as
root), and the self-check that reports a machine where the hook is not installed. No root, no network. RedCheck
breaks each installer and self-check rule in a copy and shows its test then fails.

  python3 -I -B fork-kit/model-guard/tests/install.test.py
"""
import contextlib, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, types
import unittest

sys.dont_write_bytecode = True   # the CI runner has no -B: a __pycache__ in an installed version folder is a change

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA1, SHA2 = 'a' * 40, 'b' * 40
sys.path.insert(0, os.path.join(KIT, 'lib'))
import model_guard  # noqa: E402


INSTALLER = os.path.join(KIT, 'sbin', 'model-guard-install')
DROP_IN = '/etc/claude-code/managed-settings.d/model-guard.json'
MANAGED = '/etc/claude-code/managed-settings.json'


def load(source=None):
    if source is not None:
        mod = types.ModuleType('model_guard_install')
        mod.__file__ = INSTALLER
        exec(compile(source, INSTALLER, 'exec'), mod.__dict__)
        return mod
    loader = importlib.machinery.SourceFileLoader('model_guard_install', INSTALLER)
    spec = importlib.util.spec_from_loader('model_guard_install', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Install(unittest.TestCase):
    installer_source = None     # RedCheck: a broken copy of the installer
    guard_lib = None            # RedCheck: a folder with a broken copy of lib/model_guard.py

    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='model-guard-install-test-'))
        os.chmod(self.root, 0o755)
        self.addCleanup(self.close)
        self.m = load(self.installer_source)
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        if self.guard_lib:
            self.m.OWN_LIB = self.guard_lib

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

    def write(self, path, data):
        os.makedirs(os.path.dirname(self.path(path)), mode=0o755, exist_ok=True)
        with open(self.path(path), 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        os.chmod(self.path(path), 0o644)

    def turn_on(self, only_managed=True, mode=None, section=None):
        """What the person who runs the machine does after install: policy, machine file, and only managed hooks."""
        policy = self.path('/etc/fork-kit/model-policy.json')
        with open(os.path.join(KIT, 'model-policy.example.json')) as f:
            data = json.load(f)
        self.write('/etc/fork-kit/model-policy.json', dict(data, mode=mode or data['mode']))
        self.write('/etc/fork-kit/machine.json', {'version': 1, 'model-guard': dict({'policyFile': policy},
                                                                                     **(section or {}))})
        if only_managed:
            self.write(MANAGED, {'allowManagedHooksOnly': True})

    def self_check(self):
        code, out, _ = self.run_main('self-check')
        return code, out

    def hook_in_use(self, call):
        """The decision of the guard in the version `current` points at (in this process, with the test's files)."""
        version = self.path(os.readlink(self.path('/usr/local/libexec/model-guard/current')))
        guard = self.m.load_guard(os.path.join(version, 'lib'))
        out = io.StringIO()
        guard.run_hook(io.StringIO(json.dumps(call)), out, self.path('/etc/fork-kit/machine.json'), os.geteuid())
        return json.loads(out.getvalue())['hookSpecificOutput'].get('permissionDecision') if out.getvalue() else None

    def test_fresh_install_links_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('"Turn it on"', out)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/model-guard')),
                         '/usr/local/libexec/model-guard/current/bin/model-guard')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                         f'/usr/local/libexec/model-guard/versions/{SHA1}')
        self.assertFalse(os.path.exists(self.path('/etc/fork-kit')))       # never writes policy or machine file
        self.assertFalse(os.path.exists(self.path(MANAGED)))               # nor the managed settings themselves
        with open(self.path(DROP_IN)) as f:
            self.assertEqual(json.load(f), {'hooks': {'PreToolUse': [model_guard.hook_entry()]}})
        self.assertEqual(oct(os.stat(self.path(DROP_IN)).st_mode & 0o777), '0o644')
        self.assertEqual(os.listdir(self.path('/etc/claude-code/managed-settings.d')), ['model-guard.json'])
        self.turn_on()
        code, out = self.self_check()
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
                self.assertEqual(self.self_check()[0], 0)

    def test_an_upgrade_stopped_after_each_step_still_refuses_then_finishes(self):
        bad, good = {'tool_name': 'Agent', 'tool_input': {}}, {'tool_name': 'Agent',
                                                               'tool_input': {'model': 'large', 'effort': 'high'}}
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root, mode=0o755)
                self.assertEqual(self.install()[0], 0)
                self.turn_on(mode='enforce')
                self.assertEqual(self.self_check()[0], 0)
                code, _, _ = self.install(sha=SHA2, stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                # at every stop the hook in use is a whole version and still refuses and passes as before
                self.assertIn(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                              (f'/usr/local/libexec/model-guard/versions/{SHA1}',
                               f'/usr/local/libexec/model-guard/versions/{SHA2}'))
                self.assertEqual(self.hook_in_use(bad), 'deny')
                self.assertIsNone(self.hook_in_use(good))
                with open(self.path(DROP_IN)) as f:
                    self.assertEqual(json.load(f), {'hooks': {'PreToolUse': [model_guard.hook_entry()]}})
                code, _, err = self.install(sha=SHA2)
                self.assertEqual(code, 0, err)
                self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                                 f'/usr/local/libexec/model-guard/versions/{SHA2}')
                self.assertEqual(self.self_check()[0], 0)

    def test_a_second_installer_is_refused_while_one_runs(self):
        self.assertEqual(self.install()[0], 0)
        fd = os.open(self.path('/etc/model-guard/install.lock'), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)        # another installer holds the lock
        before = self.snapshot()
        for argv in (('install', '--source', KIT, '--sha', SHA2), ('uninstall',)):
            code, _, err = self.run_main(*argv)
            self.assertEqual(code, 1, argv)
            self.assertIn('another model-guard-install is running', err)
        self.assertEqual(self.snapshot(), before)
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.install(sha=SHA2)[0], 0)

    def test_leftovers_of_a_stopped_run_are_cleared(self):
        self.assertEqual(self.install()[0], 0)
        versions = self.path('/usr/local/libexec/model-guard/versions')
        os.makedirs(os.path.join(versions, '.tmp-aaaaaaaaaaaa-x', 'lib'))   # stopped while copying
        os.symlink('/elsewhere', self.path('/usr/local/libexec/model-guard/current.new-link-999999'))
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(sorted(os.listdir(versions)), [SHA1, SHA2])
        self.assertEqual(sorted(os.listdir(self.path('/usr/local/libexec/model-guard'))), ['current', 'versions'])

    def test_foreign_link_is_refused_before_anything_is_written(self):
        os.makedirs(self.path('/usr/local/bin'))
        os.symlink('/somewhere/else', self.path('/usr/local/bin/model-guard'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not the kit\'s link', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
        self.assertFalse(os.path.exists(self.path('/etc/model-guard')))

    def test_current_that_is_not_a_link_is_refused(self):
        self.assertEqual(self.install()[0], 0)
        current = self.path('/usr/local/libexec/model-guard/current')
        os.unlink(current)
        os.mkdir(current)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('current exists and is not a link', err)

    def test_managed_settings_folder_that_is_not_ours_is_refused(self):
        os.makedirs(self.path('/etc'))
        os.symlink(self.path('/elsewhere'), self.path('/etc/claude-code'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('/etc/claude-code exists and is not a root-owned folder', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
        os.unlink(self.path('/etc/claude-code'))
        os.makedirs(self.path(DROP_IN))                         # a folder where the drop-in goes
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not a plain file', err)

    def test_a_version_file_others_can_write_or_a_link_is_made_again(self):
        version = self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}')
        elsewhere = self.path('/elsewhere.py')   # the same text, outside the version folder
        for name, spoil in (('writable', lambda f: os.chmod(f, 0o666)),
                            ('link', lambda f: (shutil.copy(f, elsewhere), os.unlink(f), os.symlink(elsewhere, f)))):
            with self.subTest(name):
                self.assertEqual(self.install()[0], 0)
                guard = os.path.join(version, 'lib', 'model_guard.py')
                spoil(guard)
                code, out, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertIn('made again', out)
                self.assertEqual(os.lstat(guard).st_mode & 0o7777, 0o644)

    def test_self_check_reports_a_repointed_link(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.turn_on()
        self.assertEqual(self.self_check()[0], 0)
        current = self.path('/usr/local/libexec/model-guard/current')
        os.unlink(current)
        os.symlink(f'/usr/local/libexec/model-guard/versions/{SHA1}', current)  # an old, whole version
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('/usr/local/libexec/model-guard/current: differs from what was installed (link)', out)

    def test_self_check_reports_a_bad_record_row(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        with open(self.path('/etc/model-guard/installed.tsv'), 'a') as f:
            f.write('just one field\n')
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn("bad row ['just one field']", out)

    def test_changed_copy_is_reported_and_made_again(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        with open(self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}/lib/model_guard.py'), 'a') as f:
            f.write('# changed\n')
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('differs from what was installed', out)
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.self_check()[0], 0)

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
        self.assertEqual(self.self_check()[0], 0)

    def test_uninstall_removes_the_kit_and_runs_again(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertIn('remove that PreToolUse entry', out)
        for path in ('/usr/local/bin/model-guard', '/usr/local/sbin/model-guard-install',
                     '/usr/local/libexec/model-guard', '/etc/model-guard', DROP_IN):
            self.assertFalse(os.path.lexists(self.path(path)), path)
        self.assertTrue(os.path.exists(self.path(MANAGED)))     # the owner's own settings stay
        self.assertTrue(os.path.exists(self.path('/etc/fork-kit/machine.json')))
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

    def test_self_check_reports_managed_settings_that_do_not_run_the_hook(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        self.write(DROP_IN, {'hooks': {'PreToolUse': [dict(model_guard.hook_entry(), matcher='Agent')]}})
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn(f'{DROP_IN}: differs from what was installed (file)', out)
        self.assertIn('entry 0 runs the guard but is not its entry: its matcher misses Task', out)
        self.assertIn('no managed PreToolUse entry runs the guard', out)
        os.unlink(self.path(DROP_IN))
        code, out = self.self_check()
        self.assertIn('no managed PreToolUse entry runs the guard', out)
        self.assertEqual(self.install()[0], 0)                  # running it again puts the drop-in back
        self.assertEqual(self.self_check()[0], 0)
        self.write(MANAGED, {'allowManagedHooksOnly': True, 'disableAllHooks': True})
        self.assertIn('disableAllHooks is set', self.self_check()[1])
        self.write('/etc/claude-code/managed-settings.d/zz.json', '{')
        self.assertIn('zz.json: not valid JSON', self.self_check()[1])

    def test_self_check_wants_only_managed_hooks_unless_the_machine_file_allows_others(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on(only_managed=False)
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('allowManagedHooksOnly is not true', out)
        self.turn_on(only_managed=False, section={'allowOtherHooks': True})
        self.assertEqual(self.self_check()[0], 0)
        self.turn_on(only_managed=False, section={'allowOtherHooks': 'yes'})
        self.assertIn('allowOtherHooks', self.self_check()[1])

    def test_self_check_reports_a_managed_settings_file_others_can_write(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        os.chmod(self.path(MANAGED), 0o666)
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('writable by group or others', out)

    def test_self_check_reports_no_policy(self):
        self.assertEqual(self.install()[0], 0)
        self.turn_on()
        os.unlink(self.path('/etc/fork-kit/model-policy.json'))
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('every checked call is refused', out)
        os.unlink(self.path('/etc/fork-kit/machine.json'))
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('every checked call is refused', out)


# rule: (text in sbin/model-guard-install, what it becomes, the test that must then fail)
INSTALLER_MUTATIONS = {
    'current is recorded': ("    rows.append((CURRENT, 'link', f'{VERSIONS}/{a.sha}'))\n", '',
                            'test_self_check_reports_a_repointed_link'),
    'a version file must be safe': ('            if st.st_uid != ROOT_UID or st.st_mode & 0o022 or stat.S_ISLNK(',
                                    '            if False and (',
                                    'test_a_version_file_others_can_write_or_a_link_is_made_again'),
    'current must be a link': ('    if os.path.lexists(p(CURRENT)) and not os.path.islink(p(CURRENT)):',
                               '    if False:', 'test_current_that_is_not_a_link_is_refused'),
    'stopped copies are cleared': ("        if name.startswith('.tmp-'):", '        if False:',
                                   'test_leftovers_of_a_stopped_run_are_cleared'),
    'stopped links are cleared': ("        if old.startswith(name + '.new-link-') and", '        if False and',
                                  'test_leftovers_of_a_stopped_run_are_cleared'),
    'one installer at a time': ('            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)', '            pass',
                                'test_a_second_installer_is_refused_while_one_runs'),
    'the managed folder is checked': ('    for path in (folder, drop_ins):\n        real = p(path)\n        if',
                                      '    for path in (folder, drop_ins):\n        real = p(path)\n'
                                      '        if False and',
                                      'test_managed_settings_folder_that_is_not_ours_is_refused'),
    'the drop-in is written': ('    write_file(drop_in, guard.drop_in_text())\n', '',
                               'test_fresh_install_links_record_and_self_check'),
    'uninstall removes the drop-in': ('            os.unlink(p(drop_in))   # first', '            pass   # first',
                                      'test_uninstall_removes_the_kit_and_runs_again'),
}
# rule: (text in lib/model_guard.py, what it becomes, the test that must then fail): the self-check
GUARD_MUTATIONS = {
    'links are compared': ('                ok = os.readlink(prefix + path) == want', '                ok = True',
                           'test_self_check_reports_a_repointed_link'),
    'a bad row is reported': ("            problems.append(f'{INSTALLED}: bad row {row!r}')", '            pass',
                              'test_self_check_reports_a_bad_record_row'),
    'managed settings are checked': ('        problems += settings_problems(',
                                     '        problems += [] and settings_problems(',
                                     'test_self_check_reports_managed_settings_that_do_not_run_the_hook'),
    'unusable managed files are reported': ("        problems += [f'{path}: {error}' for path, _, error in files "
                                            "if error]",
                                            '        pass',
                                            'test_self_check_reports_a_managed_settings_file_others_can_write'),
    'other hooks only when allowed': ("        allow_other_hooks = cfg['allowOtherHooks']",
                                      '        allow_other_hooks = True',
                                      'test_self_check_wants_only_managed_hooks_unless_the_machine_file_allows_others'),
}


class RedCheck(unittest.TestCase):
    def run_broken(self, test, **attrs):
        result = unittest.TestResult()
        type('Broken', (Install,), attrs)(test).run(result)
        return result

    def test_each_installer_rule_has_a_test_that_fails_without_it(self):
        with open(INSTALLER, encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, test) in INSTALLER_MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                result = self.run_broken(test, installer_source=source.replace(text, broken))
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')

    def test_each_self_check_rule_has_a_test_that_fails_without_it(self):
        with open(os.path.join(KIT, 'lib', 'model_guard.py'), encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, test) in GUARD_MUTATIONS.items():
            with self.subTest(rule=rule), tempfile.TemporaryDirectory() as d:
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                os.makedirs(os.path.join(d, 'model-guard', 'lib'))
                shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(d, 'common'),
                                ignore=shutil.ignore_patterns('__pycache__', 'tests'))
                with open(os.path.join(d, 'model-guard', 'lib', 'model_guard.py'), 'w', encoding='utf-8') as f:
                    f.write(source.replace(text, broken))
                result = self.run_broken(test, guard_lib=os.path.join(d, 'model-guard', 'lib'))
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
