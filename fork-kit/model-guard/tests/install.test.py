"""model-guard-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as
root), and the self-check that reports a machine where the hook is not installed. No root, no network. RedCheck
breaks each installer and self-check rule in a copy and shows its test then fails.

  python3 -I -B fork-kit/model-guard/tests/install.test.py
"""
import contextlib, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, types
import unittest
from unittest import mock

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

    def turn_on(self, only_managed=True, mode=None, section=None, model_keys=True):
        """What the person who runs the machine does (README "Turn it on"): policy, machine file, and the managed
        settings of their own: only managed hooks, and Claude Code's model keys made from the policy."""
        policy = self.path('/etc/fork-kit/model-policy.json')
        with open(os.path.join(KIT, 'model-policy.example.json')) as f:
            data = json.load(f)
        data = dict(data, mode=mode or data['mode'])
        self.write('/etc/fork-kit/model-policy.json', data)
        self.write('/etc/fork-kit/machine.json', {'version': 1, 'model-guard': dict({'policyFile': policy},
                                                                                     **(section or {}))})
        if only_managed:
            keys = model_guard.model_settings(model_guard.parse_policy(json.dumps(data), 'p')) if model_keys else {}
            self.write(MANAGED, dict({'allowManagedHooksOnly': True}, **keys))

    def fresh(self):
        shutil.rmtree(self.root)
        os.makedirs(self.root, mode=0o755)

    def current(self):
        return os.readlink(self.path('/usr/local/libexec/model-guard/current'))

    def versions(self):
        return sorted(os.listdir(self.path('/usr/local/libexec/model-guard/versions')))

    def self_check(self):
        code, out, _ = self.run_main('self-check')
        return code, out

    def hook_in_use(self, call):
        """The decision of the guard in the version `current` points at (in this process, with the test's files)."""
        version = self.path(os.readlink(self.path('/usr/local/libexec/model-guard/current')))
        guard = self.m.load_guard(os.path.join(version, 'lib'))
        out = io.StringIO()
        guard.run_hook(io.StringIO(json.dumps(call)), out, self.path('/etc/fork-kit/machine.json'), os.geteuid(),
                       record=None)
        return json.loads(out.getvalue())['hookSpecificOutput'].get('permissionDecision') if out.getvalue() else None

    def test_fresh_install_links_record_and_self_check(self):
        self.turn_on(only_managed=False)
        with open(self.path('/etc/fork-kit/machine.json')) as f:
            machine = f.read()
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('run model-guard --self-check', out)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/model-guard')),
                         '/usr/local/libexec/model-guard/current/bin/model-guard')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/model-guard/current')),
                         f'/usr/local/libexec/model-guard/versions/{SHA1}')
        self.assertEqual(sorted(os.listdir(self.path('/etc/fork-kit'))), ['machine.json', 'model-policy.json'])
        with open(self.path('/etc/fork-kit/machine.json')) as f:
            self.assertEqual(f.read(), machine)                             # never writes policy or machine file
        self.assertFalse(os.path.exists(self.path(MANAGED)))               # nor the managed settings themselves
        with open(self.path(DROP_IN)) as f:
            self.assertEqual(json.load(f), {'hooks': {'PreToolUse': [model_guard.hook_entry()]}})
        self.assertEqual(oct(os.stat(self.path(DROP_IN)).st_mode & 0o777), '0o644')
        self.assertEqual(os.listdir(self.path('/etc/claude-code/managed-settings.d')), ['model-guard.json'])
        self.turn_on()
        code, out = self.self_check()
        self.assertEqual(code, 0, out)
        self.assertIn('mode advise', out)
        # the installed hook runs from its own version folder and refuses when it finds no policy (/etc/fork-kit
        # outside the test folder; a stand-in system log, so the test writes nothing to the real one)
        run = ('import runpy, sys, types; log = types.ModuleType("syslog"); log.LOG_USER = log.LOG_WARNING = 0; '
               'log.openlog = lambda *a: None; log.syslog = lambda p, t: print("LOG " + t, file=sys.stderr); '
               'sys.modules["syslog"] = log; sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name="__main__")')
        r = subprocess.run([sys.executable, '-I', '-B', '-c', run,
                            self.path(f'/usr/local/libexec/model-guard/versions/{SHA1}/bin/model-guard')],
                           input='{"tool_name": "Agent", "tool_input": {}}', capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(json.loads(r.stdout)['hookSpecificOutput']['permissionDecision'], 'deny')
        self.assertIn('LOG model-guard refused Agent', r.stderr)

    def test_runs_again_and_changes_nothing(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                self.fresh()
                self.turn_on()
                code, _, _ = self.install(stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(self.self_check()[0], 0)

    def test_an_upgrade_stopped_after_each_step_still_refuses_then_finishes(self):
        bad, good = {'tool_name': 'Agent', 'tool_input': {}}, {'tool_name': 'Agent',
                                                               'tool_input': {'model': 'large', 'effort': 'high'}}
        for step in self.m.STEPS:
            with self.subTest(step=step):
                self.fresh()
                self.turn_on(mode='enforce')
                self.assertEqual(self.install()[0], 0)
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
                self.assertEqual(self.versions(), [SHA2])
                self.assertEqual(self.self_check()[0], 0)

    def test_a_second_installer_is_refused_while_one_runs(self):
        self.turn_on()
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

    def test_a_lock_file_an_uninstall_removed_is_not_a_lock(self):
        """An uninstall removes the lock file while it holds it. An installer that opened that file just before
        would get a lock no one else sees: it must refuse, not run next to the next installer."""
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        real_flock, lock = fcntl.flock, self.path('/etc/model-guard/install.lock')

        def flock_after_an_uninstall(fd, op):
            os.unlink(lock)                 # the uninstall finished between this installer's open and its lock
            return real_flock(fd, op)
        before = self.snapshot()
        with mock.patch.object(self.m.fcntl, 'flock', flock_after_an_uninstall):
            code, _, err = self.install(sha=SHA2)
        self.assertEqual(code, 1)
        self.assertIn('just uninstalled the kit: run again', err)
        self.assertEqual(self.current(), f'/usr/local/libexec/model-guard/versions/{SHA1}')
        self.assertEqual(set(self.snapshot()) | {'/etc/model-guard/install.lock'}, set(before))
        self.assertEqual(self.install(sha=SHA2)[0], 0)      # run again: a new lock file, and it goes on

    def test_a_lock_folder_an_uninstall_removed_before_the_open_is_refused(self):
        """#589 review: an uninstall can remove the guard's /etc folder between the installer's ensure_dir and its
        open of the lock file. The open then finds no folder: refused with "run again", nothing more written."""
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        real_ensure_dir, calls = self.m.ensure_dir, []

        def ensure_dir_then_uninstall(path):
            real_ensure_dir(path)
            if path == self.m.ETC and not calls:
                calls.append(path)
                shutil.rmtree(self.path(self.m.ETC))      # the uninstall's last step, between the two calls
        before = self.snapshot()
        with mock.patch.object(self.m, 'ensure_dir', ensure_dir_then_uninstall):
            code, _, err = self.install(sha=SHA2)
        self.assertEqual(calls, [self.m.ETC])
        self.assertEqual(code, 1)
        self.assertIn('just uninstalled the kit: run again', err)
        # nothing written: only the guard's /etc folder (the uninstall's) is gone
        self.assertEqual(set(self.snapshot()), {k for k in before if not k.startswith(self.m.ETC)})
        code, _, err = self.install(sha=SHA2)                   # run again: it goes on
        self.assertEqual(code, 0, err)
        self.assertEqual(self.self_check()[0], 0)

    def test_leftovers_of_a_stopped_run_are_cleared(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        versions = self.path('/usr/local/libexec/model-guard/versions')
        os.makedirs(os.path.join(versions, '.tmp-aaaaaaaaaaaa-x', 'lib'))   # stopped while copying
        os.symlink('/elsewhere', self.path('/usr/local/libexec/model-guard/current.new-link-999999'))
        self.assertEqual(self.install(sha=SHA2, stop_after='record')[0], 'stopped after record (test)')
        self.assertEqual(sorted(os.listdir(versions)), [SHA1, SHA2])
        self.assertEqual(sorted(os.listdir(self.path('/usr/local/libexec/model-guard'))), ['current', 'versions'])

    def test_foreign_link_is_refused_before_anything_is_written(self):
        self.turn_on()
        os.makedirs(self.path('/usr/local/bin'))
        os.symlink('/somewhere/else', self.path('/usr/local/bin/model-guard'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not the kit\'s link', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
        self.assertFalse(os.path.exists(self.path('/etc/model-guard')))

    def test_current_that_is_not_a_link_is_refused(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        current = self.path('/usr/local/libexec/model-guard/current')
        os.unlink(current)
        os.mkdir(current)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('current exists and is not a link', err)

    def test_managed_settings_folder_that_is_not_ours_is_refused(self):
        self.turn_on(only_managed=False)
        os.symlink(self.path('/elsewhere'), self.path('/etc/claude-code'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('/etc/claude-code exists and is not a root-owned folder', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
        os.unlink(self.path('/etc/claude-code'))
        os.makedirs(self.path('/etc/claude-code/managed-settings.d'))
        real_owner = self.m.owner_of
        for folder in ('/etc/claude-code', '/etc/claude-code/managed-settings.d'):
            with self.subTest(folder), mock.patch.object(self.m, 'owner_of',
                                                         lambda r: 4242 if r == self.path(folder) else real_owner(r)):
                code, _, err = self.install()                   # a folder root does not own
                self.assertEqual(code, 1)
                self.assertIn(f'{folder} exists and is not a root-owned folder', err)
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/model-guard')))
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
                self.turn_on()
                self.assertEqual(self.install()[0], 0)
                guard = os.path.join(version, 'lib', 'model_guard.py')
                spoil(guard)
                code, out, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertIn('made again', out)
                self.assertEqual(os.lstat(guard).st_mode & 0o7777, 0o644)

    def test_self_check_reports_a_repointed_link(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install(sha=SHA2, stop_after='record')[0], 'stopped after record (test)')
        self.assertEqual(self.self_check()[0], 0)
        current = self.path('/usr/local/libexec/model-guard/current')
        os.unlink(current)
        os.symlink(f'/usr/local/libexec/model-guard/versions/{SHA1}', current)  # an old, whole version
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('/usr/local/libexec/model-guard/current: differs from what was installed (link)', out)

    def test_self_check_reports_a_bad_record_row(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        with open(self.path('/etc/model-guard/installed.tsv'), 'a') as f:
            f.write('just one field\n')
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn("bad row ['just one field']", out)

    def test_changed_copy_is_reported_and_made_again(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
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
        self.turn_on()
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

    def test_upgrade_switches_and_removes_old_versions(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        versions = self.path('/usr/local/libexec/model-guard/versions')
        # stopped before pruning: both versions stay, and running it again prunes
        self.assertEqual(self.install(sha=SHA2, stop_after='record')[0], 'stopped after record (test)')
        self.assertEqual(self.versions(), [SHA1, SHA2])
        os.makedirs(self.path('/elsewhere/keep'))
        os.symlink(self.path('/elsewhere'), os.path.join(versions, 'a-link'))   # removed, never followed
        with open(os.path.join(versions, 'a-file'), 'w') as f:
            f.write('stray\n')
        code, out, err = self.install(sha=SHA2)
        self.assertEqual(code, 0, err)
        self.assertEqual(self.current(), f'/usr/local/libexec/model-guard/versions/{SHA2}')
        self.assertEqual(self.versions(), [SHA2])
        self.assertIn(f'removed the old version folder /usr/local/libexec/model-guard/versions/{SHA1}', out)
        self.assertTrue(os.path.isdir(self.path('/elsewhere/keep')))
        self.assertEqual(self.self_check()[0], 0)
        self.assertEqual(self.install(sha=SHA1)[0], 0)          # back to the first: the same, the other way
        self.assertEqual(self.versions(), [SHA1])
        self.assertEqual(self.self_check()[0], 0)

    def test_a_first_install_without_a_valid_policy_writes_nothing(self):
        """From the settings step on, the hook refuses every checked call while no policy loads: the first install
        refuses before it writes anything, and the settings step checks again with the new version."""
        cases = {
            'no machine file': lambda: os.unlink(self.path('/etc/fork-kit/machine.json')),
            'no policy file': lambda: os.unlink(self.path('/etc/fork-kit/model-policy.json')),
            'a broken policy': lambda: self.write('/etc/fork-kit/model-policy.json', '{'),
            'a newer policy': lambda: self.write('/etc/fork-kit/model-policy.json', {'version': 99}),
        }
        for name, spoil in cases.items():
            with self.subTest(name):
                self.fresh()
                self.turn_on(only_managed=False)
                spoil()
                before = self.snapshot()
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn('no valid model policy', err)
                self.assertIn('nothing was written', err)
                self.assertEqual(self.snapshot(), before)
                self.turn_on(only_managed=False)
                self.assertEqual(self.install()[0], 0, name)

    def test_the_settings_step_checks_the_policy_again(self):
        self.turn_on()
        real_self_test = self.m.self_test

        def self_test_then_policy_goes(dest):             # the policy goes while the installer copies
            os.unlink(self.path('/etc/fork-kit/model-policy.json'))
            return real_self_test(dest)
        with mock.patch.object(self.m, 'self_test', self_test_then_policy_goes):
            code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('the hook is not turned on yet', err)
        self.assertFalse(os.path.lexists(self.path(DROP_IN)))
        self.turn_on()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.self_check()[0], 0)
        os.unlink(self.path('/etc/fork-kit/model-policy.json'))   # the hook runs already: a rerun still repairs
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('WARNING: this version is already in use and cannot use the model policy', out)

    def test_an_upgrade_that_cannot_read_the_policy_warns(self):
        """#589 review: when the hook already runs, the settings step still repairs, but says when the new version
        cannot read the policy (say a kit that reads it otherwise), since the hook then refuses every checked call."""
        self.turn_on()
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertNotIn('WARNING', out)
        self.write('/etc/fork-kit/model-policy.json', {'version': 99, 'mode': 'off', 'allow': []})
        code, out, err = self.install(sha=SHA2)
        self.assertEqual(code, 0, err)
        self.assertIn('WARNING: this version is already in use and cannot use the model policy', out)
        self.assertIn('update the kit', out)
        self.assertIn('so the hook already refuses every checked call', out)   # printed after the switch
        self.assertEqual(self.current(), f'/usr/local/libexec/model-guard/versions/{SHA2}')
        self.turn_on()
        code, out, err = self.install(sha=SHA2)                 # runs again with a policy it reads: no warning
        self.assertEqual(code, 0, err)
        self.assertNotIn('WARNING', out)

    def test_a_drop_in_someone_else_owns_is_written_again(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        drop_in = self.path(DROP_IN)
        inode = os.lstat(drop_in).st_ino
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(os.lstat(drop_in).st_ino, inode)       # same text, mode and owner: left alone
        real_owner = self.m.owner_of
        with mock.patch.object(self.m, 'owner_of', lambda r: 4242 if r == drop_in else real_owner(r)):
            self.assertEqual(self.install()[0], 0)
        self.assertNotEqual(os.lstat(drop_in).st_ino, inode)    # the same text, but not root's: a new file

    def test_uninstall_removes_the_kit_and_runs_again(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install(sha=SHA2, stop_after='record')[0], 'stopped after record (test)')
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertIn('remove that PreToolUse entry', out)
        for path in ('/usr/local/bin/model-guard', '/usr/local/sbin/model-guard-install',
                     '/usr/local/libexec/model-guard', '/etc/model-guard', DROP_IN):
            self.assertFalse(os.path.lexists(self.path(path)), path)
        self.assertTrue(os.path.exists(self.path(MANAGED)))     # the owner's own settings stay
        self.assertTrue(os.path.exists(self.path('/etc/fork-kit/machine.json')))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.lexists(self.path('/etc/model-guard')))

    def test_an_uninstall_stopped_after_each_step_is_finished_by_running_it_again(self):
        steps = self.m.UNINSTALL_STEPS
        for step in steps:
            with self.subTest(step=step):
                self.fresh()
                self.turn_on()
                self.assertEqual(self.install()[0], 0)
                self.assertEqual(self.install(sha=SHA2, stop_after='record')[0], 'stopped after record (test)')
                code, _, _ = self.run_main('uninstall', stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                done = steps[:steps.index(step) + 1]
                # the hook goes first: no session runs a half-removed kit
                self.assertFalse(os.path.lexists(self.path(DROP_IN)))
                # the short command stays while its version folder is there, so the owner can run it again
                installer = self.path('/usr/local/sbin/model-guard-install')
                self.assertEqual(os.path.lexists(installer), 'installer-link' not in done)
                if 'installer-link' not in done:                  # and it still leads to a whole installer
                    self.assertEqual(os.readlink(installer),
                                     '/usr/local/libexec/model-guard/current/sbin/model-guard-install')
                    self.assertTrue(os.path.isfile(self.path(self.current() + '/sbin/model-guard-install')))
                self.assertEqual(os.path.isfile(self.path(f'/usr/local/libexec/model-guard/versions/{SHA2}/sbin/'
                                                          'model-guard-install')), 'libexec' not in done)
                self.assertEqual(os.path.exists(self.path('/etc/model-guard/install.lock')), 'lock' not in done)
                if 'old-versions' in done and 'libexec' not in done:
                    self.assertEqual(self.versions(), [SHA2])
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                for path in ('/usr/local/bin/model-guard', '/usr/local/sbin/model-guard-install',
                             '/usr/local/libexec/model-guard', '/etc/model-guard', DROP_IN):
                    self.assertFalse(os.path.lexists(self.path(path)), path)
                self.assertTrue(os.path.exists(self.path(MANAGED)))

    def test_uninstall_leaves_a_drop_in_folder_alone(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        os.unlink(self.path(DROP_IN))
        os.makedirs(self.path(DROP_IN + '/inside'))
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertIn('is not a plain file, so it was left alone: remove it by hand', out)
        self.assertTrue(os.path.isdir(self.path(DROP_IN + '/inside')))
        for path in ('/usr/local/bin/model-guard', '/usr/local/sbin/model-guard-install',
                     '/usr/local/libexec/model-guard', '/etc/model-guard'):
            self.assertFalse(os.path.lexists(self.path(path)), path)

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
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        self.write(DROP_IN, {'hooks': {'PreToolUse': [dict(model_guard.hook_entry(), matcher='Agent')]}})
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn(f'{DROP_IN}: differs from what was installed (file)', out)
        self.assertIn('entry 0 runs the guard but is not its entry: its matcher is not exactly', out)
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
        self.turn_on(only_managed=False)
        self.assertEqual(self.install()[0], 0)
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('allowManagedHooksOnly is not true', out)
        self.turn_on(only_managed=False, section={'allowOtherHooks': True})
        self.assertEqual(self.self_check()[0], 0)
        self.turn_on(only_managed=False, section={'allowOtherHooks': 'yes'})
        self.assertIn('allowOtherHooks', self.self_check()[1])

    def test_self_check_reports_a_managed_settings_file_others_can_write(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
        os.chmod(self.path(MANAGED), 0o666)
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn('writable by group or others', out)

    def test_self_check_wants_claude_codes_model_keys_in_enforce(self):
        self.turn_on(mode='enforce')
        self.assertEqual(self.install()[0], 0)
        code, out = self.self_check()
        self.assertEqual(code, 0, out)
        self.turn_on(mode='enforce', model_keys=False)
        code, out = self.self_check()
        self.assertEqual(code, 1)
        self.assertIn("managed settings miss Claude Code's own model limits", out)
        self.assertIn('Put these keys', out)
        self.assertIn('"availableModels"', out)
        self.turn_on(mode='advise', model_keys=False)           # advise binds nothing, so it needs no keys
        self.assertEqual(self.self_check()[0], 0)

    def test_self_check_reports_no_policy(self):
        self.turn_on()
        self.assertEqual(self.install()[0], 0)
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
    'a first install needs a policy': ('        if not os.path.lexists(p(drop_in)):\n            raise Refused',
                                       '        if False:\n            raise Refused',
                                       'test_a_first_install_without_a_valid_policy_writes_nothing'),
    'the settings step checks the policy again': ("    check_policy(guard, so='the hook is not turned on yet', warn=True)",
                                                  '    pass', 'test_the_settings_step_checks_the_policy_again'),
    'an upgrade that cannot read the policy warns': ('        if warn:', '        if False:',
                                                     'test_an_upgrade_that_cannot_read_the_policy_warns'),
    'a lock folder gone before the open is refused': (
        "    except FileNotFoundError:\n        raise Refused('another model-guard-install just uninstalled",
        "    except KeyError:\n        raise Refused('another model-guard-install just uninstalled",
        'test_a_lock_folder_an_uninstall_removed_before_the_open_is_refused'),
    'a file root does not own is written again': ('                    and owner_of(real) == ROOT_UID:',
                                                  '                    :',
                                                  'test_a_drop_in_someone_else_owns_is_written_again'),
    'a managed folder root does not own is refused': ('                                      or owner_of(real) != ROOT_UID):',
                                                      '                                      or False):',
                                                      'test_managed_settings_folder_that_is_not_ours_is_refused'),
    'old versions are removed': ("    prune_versions(a.sha)\n    done('prune')", "    done('prune')",
                                 'test_upgrade_switches_and_removes_old_versions'),
    'a pruned link is not followed': ('        if os.path.islink(path) or not os.path.isdir(path):',
                                      '        if not os.path.isdir(path):',
                                      'test_upgrade_switches_and_removes_old_versions'),
    'a lock file an uninstall removed is refused': ('        if not same:', '        if False:',
                                                    'test_a_lock_file_an_uninstall_removed_is_not_a_lock'),
    'uninstall keeps the lock till last': ('                if path != p(LOCK):', '                if True:',
                                           'test_an_uninstall_stopped_after_each_step_is_finished_by_running_it_again'),
    'uninstall removes the old versions': ('            prune_versions(current)', '            pass',
                                           'test_an_uninstall_stopped_after_each_step_is_finished_by_running_it_again'),
    'uninstall keeps the short command till its folder goes': (
        "        done('drop-in')\n", "        done('drop-in')\n        os.unlink(p(INSTALLER))\n",
        'test_an_uninstall_stopped_after_each_step_is_finished_by_running_it_again'),
    'uninstall leaves a drop-in folder alone': ('            if stat.S_ISREG(os.lstat(p(drop_in)).st_mode):',
                                                '            if True:', 'test_uninstall_leaves_a_drop_in_folder_alone'),
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
    "Claude Code's model keys are checked": ('            problems += model_settings_problems(usable, policy)',
                                            '            pass', 'test_self_check_wants_claude_codes_model_keys_in_enforce'),
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
