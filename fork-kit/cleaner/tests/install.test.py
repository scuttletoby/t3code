"""fork-cleaner-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as
root), and bootstrap.sh's options. No root, no systemd, no network. RedCheck breaks each guard in a copy of the
installer (or of bootstrap.sh) and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/install.test.py
"""
import contextlib, fcntl, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA1, SHA2 = 'a' * 40, 'b' * 40
UNITS = ('/etc/systemd/user/fork-cleaner.service', '/etc/systemd/user/fork-cleaner.timer')
FOREIGN = '[Timer]\nOnCalendar=daily\n'


INSTALLER = os.path.join(KIT, 'sbin', 'fork-cleaner-install')
BOOTSTRAP = os.path.join(KIT, 'bootstrap.sh')


def source_of(path, replace=None):
    """The text of `path`; with `replace` = (text, broken text), that text (which must be there exactly once)
    replaced."""
    with open(path, encoding='utf-8') as f:
        text = f.read()
    if replace is not None:
        if text.count(replace[0]) != 1:
            raise AssertionError(f'{replace[0]!r} is not in {path} exactly once')
        text = text.replace(replace[0], replace[1])
    return text


def load(replace=None):
    spec = importlib.util.spec_from_loader('fork_cleaner_install', loader=None)
    mod = importlib.util.module_from_spec(spec)
    mod.__file__ = INSTALLER
    exec(compile(source_of(INSTALLER, replace), INSTALLER, 'exec'), mod.__dict__)
    return mod


class Install(unittest.TestCase):
    broken = None       # RedCheck: ('install' or 'bootstrap', text, broken text)

    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='cleaner-install-test-'))
        self.addCleanup(self.close)
        self.m = load(self.broken[1:] if self.broken and self.broken[0] == 'install' else None)
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        self.bootstrap = os.path.join(self.root, 'bootstrap.sh')
        with open(self.bootstrap, 'w', encoding='utf-8') as f:
            f.write(source_of(BOOTSTRAP, self.broken[1:] if self.broken and self.broken[0] == 'bootstrap' else None))

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

    def install(self, *extra, sha=SHA1, stop_after=None, source=KIT):
        return self.run_main('install', '--source', source, '--sha', sha, *extra, stop_after=stop_after)

    def copy_kit(self):
        """A source folder of the kit (and its common/ beside it) the test may change."""
        src = os.path.join(self.root, 'src')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'common'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        return src

    def version(self, sha=SHA1, rel=''):
        return self.path(f'/usr/local/libexec/fork-cleaner/versions/{sha}' + (f'/{rel}' if rel else ''))

    def config(self):
        with open(self.path('/etc/fork-cleaner/config.json')) as f:
            return json.load(f)

    def units(self):
        return [os.path.isfile(self.path(u)) for u in UNITS]

    def snapshot(self):
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full[len(self.root):]] = (st.st_mode, st.st_mtime_ns,
                                              os.readlink(full) if os.path.islink(full) else None)
        return out

    def test_every_module_of_the_kit_is_installed(self):
        found = sorted(n[:-3] for n in os.listdir(os.path.join(KIT, 'lib', 'cleaner')) if n.endswith('.py'))
        self.assertEqual(found, sorted(self.m.MODULES))

    def test_fresh_install_links_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/fork-cleaner')),
                         '/usr/local/libexec/fork-cleaner/current/bin/fork-cleaner')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/fork-cleaner/current')),
                         f'/usr/local/libexec/fork-cleaner/versions/{SHA1}')
        self.assertEqual(self.config(), {'version': 1, 'timer': False})
        self.assertEqual(self.units(), [False, False])                 # no timer unless asked
        self.assertEqual(self.run_main('self-check')[0], 0)
        moved = os.path.join(self.root, 'elsewhere')                   # runs from its own copy of common/
        shutil.copytree(self.path(f'/usr/local/libexec/fork-cleaner/versions/{SHA1}'), moved)
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, 'bin', 'fork-cleaner'), '--help'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('fork-cleaner daily', r.stdout)
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, 'bin', 'fork-cleaner'), 'daily', 'x'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout)                     # loads every module, refuses the option

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install('--timer')[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_the_timer_unit_runs_daily_of_the_release_in_use(self):
        code, out, err = self.install('--timer')
        self.assertEqual(code, 0, err)
        self.assertIn('systemctl --user enable --now fork-cleaner.timer', out)
        with open(self.path(UNITS[0])) as f:
            text = f.read()
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/fork-cleaner/current/bin/fork-cleaner '
                      'daily\n', text)
        self.assertNotIn(' run\n', text)
        with open(self.path(UNITS[1])) as f:
            text = f.read()
        self.assertIn('OnCalendar=', text)
        self.assertIn('WantedBy=timers.target\n', text)
        names = os.listdir(self.path('/etc/systemd/user'))
        self.assertEqual(sorted(n for n in names if n.endswith('.timer')), ['fork-cleaner.timer'])   # one timer only
        self.assertTrue(self.config()['timer'])
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_timer_kept_by_an_upgrade_and_removed_by_no_timer_and_uninstall(self):
        self.assertEqual(self.install('--timer')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)                 # upgrade without the option: kept
        self.assertEqual(self.units(), [True, True])
        self.assertTrue(os.path.isdir(self.path(f'/usr/local/libexec/fork-cleaner/versions/{SHA1}')))
        code, out, _ = self.install('--no-timer', sha=SHA2)
        self.assertEqual(code, 0)
        self.assertIn('systemctl --user disable --now fork-cleaner.timer', out)
        self.assertEqual(self.units(), [False, False])
        self.assertEqual(self.install(sha=SHA2)[0], 0)                 # again without the option: still off
        self.assertEqual(self.units(), [False, False])
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install('--timer', sha=SHA2)[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(self.units(), [False, False])
        for gone in ('/usr/local/libexec/fork-cleaner', '/etc/fork-cleaner', '/usr/local/bin/fork-cleaner',
                     '/usr/local/sbin/fork-cleaner-install'):
            self.assertFalse(os.path.lexists(self.path(gone)), gone)
        self.assertEqual(self.run_main('uninstall')[0], 0)             # again: nothing left, still fine

    def test_units_someone_else_wrote_are_refused_and_kept(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path(UNITS[1]), 'w') as f:
            f.write(FOREIGN)
        code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        self.assertIn('--no-timer', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))
        self.assertEqual(self.install()[0], 0)                          # without the option it is left alone
        self.assertEqual(self.run_main('uninstall')[0], 0)
        with open(self.path(UNITS[1])) as f:
            self.assertEqual(f.read(), FOREIGN)
        os.unlink(self.path(UNITS[1]))
        os.symlink(self.path('/elsewhere'), self.path(UNITS[1]))           # a link is never ours either
        self.assertEqual(self.install('--timer')[0], 1)

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                code, _, _ = self.install('--timer', stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                # a timer only once the release it runs is the one in use
                if os.path.exists(self.path(UNITS[0])):
                    current = os.readlink(self.path('/usr/local/libexec/fork-cleaner/current'))
                    self.assertTrue(os.path.isfile(self.path(current + '/bin/fork-cleaner')))
                    self.assertTrue(os.path.islink(self.path('/usr/local/bin/fork-cleaner')))
                code, _, err = self.install('--timer')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.units(), [True, True])
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_changed_copy_is_found_and_made_again(self):
        self.assertEqual(self.install()[0], 0)
        bad = self.path(f'/usr/local/libexec/fork-cleaner/versions/{SHA1}/lib/cleaner/rules.py')
        with open(bad, 'a') as f:
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('changed since install', out)
        os.makedirs(self.path('/usr/local/libexec/fork-cleaner/versions/.tmp-leftover'))
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner/versions/.tmp-leftover')))

    def test_the_same_release_over_a_changed_copy_is_built_first_then_swapped(self):
        """#491: a reinstall of the release in use over a changed copy builds the new copy beside it and swaps it in
        whole: a copy that stops half way, or fails its self-test, leaves the copy in use where it was."""
        self.assertEqual(self.install()[0], 0)
        changed = self.version(rel='lib/cleaner/rules.py')
        with open(changed, 'a') as f:
            f.write('# changed\n')
        with open(changed) as f:
            text = f.read()
        real, calls = self.m.read_source, []

        def stops_half_way(source, rel):                    # the source is read whole once by the option check
            calls.append(rel)
            if len(calls) == len(self.m.SOURCE_FILES) + 3:
                raise self.m.Refused('the source went away (test)')
            return real(source, rel)
        with mock.patch.object(self.m, 'read_source', stops_half_way):
            code, _, err = self.install()
        self.assertEqual(code, 1, err)
        with open(changed) as f:
            self.assertEqual(f.read(), text)                # the copy in use: still all there
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/fork-cleaner/current')),
                         f'/usr/local/libexec/fork-cleaner/versions/{SHA1}')
        self.assertTrue(os.path.isfile(self.version(rel='bin/fork-cleaner')))
        real_test = self.m.self_test
        with mock.patch.object(self.m, 'self_test',
                               lambda dest: ['broken (test)'] if '.tmp-' in dest else real_test(dest)):
            code, out, err = self.install()
        self.assertEqual(code, 1, err)
        self.assertIn('the new copy failed its self-test (the version in use is unchanged)', err)
        self.assertNotIn('made again', out)
        with open(changed) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(os.listdir(self.path('/usr/local/libexec/fork-cleaner/versions')), [SHA1])
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_swap_without_renameat2_leaves_nothing_behind(self):
        self.assertEqual(self.install()[0], 0)
        with open(self.version(rel='README.md'), 'a') as f:
            f.write('changed\n')
        with mock.patch.object(self.m, '_renameat2', lambda: None):
            code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('made again', out)
        self.assertEqual(os.listdir(self.path('/usr/local/libexec/fork-cleaner/versions')), [SHA1])
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_link_in_the_source_is_never_followed(self):
        """Run as root, the installer must not copy a file only root may read into a world-readable release
        because the source holds a link to it."""
        src = self.copy_kit()
        secret = os.path.join(self.root, 'secret')
        with open(secret, 'w') as f:
            f.write('owner only\n')
        os.chmod(secret, 0o600)
        readme = os.path.join(src, 'README.md')
        os.unlink(readme)
        os.symlink(secret, readme)
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1)
        self.assertIn('README.md: a link is on its way or it is not a file (the installer copies no links)', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))
        os.unlink(readme)
        shutil.copy(os.path.join(KIT, 'README.md'), readme)
        lib = os.path.join(src, 'lib')                      # a link on the way to a file
        os.rename(lib, os.path.join(self.root, 'lib-elsewhere'))
        os.symlink(os.path.join(self.root, 'lib-elsewhere'), lib)
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1)
        self.assertIn('the installer copies no links', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))
        os.unlink(lib)
        os.rename(os.path.join(self.root, 'lib-elsewhere'), lib)
        self.assertEqual(self.install(source=src)[0], 0)    # the same folder without links installs

    def test_installed_files_must_be_root_owned_and_not_writable_by_others(self):
        self.assertEqual(self.install()[0], 0)
        for change in ('group-writable', 'another owner'):
            with self.subTest(change=change):
                if change == 'group-writable':
                    os.chmod(self.version(rel='lib/cleaner/engine.py'), 0o664)
                else:
                    self.m.ROOT_UID = os.geteuid() + 1
                code, out, _ = self.run_main('self-check')
                self.assertEqual(code, 1, out)
                self.assertIn('not root-owned, writable by others, or a link', out)
                self.m.ROOT_UID = os.geteuid()
                self.assertEqual(self.install()[0], 0)      # the installer makes it again
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_unit_someone_writes_during_the_install_is_refused_and_kept(self):
        """The units are checked again right before they are written: one written while the installer ran (after
        its first check) is not overwritten."""
        real = self.m.switch_link

        def and_a_foreign_unit(path, target):
            real(path, target)
            if not os.path.lexists(self.path(UNITS[1])):
                os.makedirs(self.path('/etc/systemd/user'), exist_ok=True)
                with open(self.path(UNITS[1]), 'w') as f:
                    f.write(FOREIGN)
        with mock.patch.object(self.m, 'switch_link', and_a_foreign_unit):
            code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        with open(self.path(UNITS[1])) as f:
            self.assertEqual(f.read(), FOREIGN)

    def test_uninstall_waits_for_no_installer(self):
        self.assertEqual(self.install()[0], 0)
        fd = os.open(self.path('/etc/fork-cleaner/install.lock'), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('another fork-cleaner-install is running', err)
        self.assertTrue(os.path.isdir(self.version()))
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/fork-cleaner')))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def run_bootstrap(self, *args):
        return subprocess.run(['sh', self.bootstrap, *args], capture_output=True, text=True, timeout=60,
                              stdin=subprocess.DEVNULL)

    def test_bootstrap_passes_on_only_the_timer_options(self):
        """Nothing after --timer reaches the installer unchecked: a second --source would install a folder the
        release check never saw."""
        for extra in (['--source', '/elsewhere'], ['--x']):
            with self.subTest(extra=extra):
                done = self.run_bootstrap('--repo', 'a/b', '--sha', SHA1, '--timer', *extra)
                self.assertEqual(done.returncode, 1)
                self.assertIn(f'unknown option {extra[0]}', done.stderr)
        done = self.run_bootstrap('--repo', 'a/b', '--sha', SHA1, '--timer', '--sha')
        self.assertEqual((done.returncode, done.stderr.strip()), (1, 'fork-cleaner-bootstrap: --sha needs a value'))
        done = self.run_bootstrap('--repo', 'a/b', '--sha', SHA1, '--timer', '--sha', 'abc')
        self.assertIn('--sha must be the full 40-character release commit', done.stderr)   # the last one is checked
        if os.geteuid() != 0:                               # as root it would go on to download
            for timer in ('--timer', '--no-timer'):
                done = self.run_bootstrap('--repo', 'a/b', timer, '--sha', SHA1)
                self.assertEqual(done.returncode, 1)
                self.assertIn('run this as root', done.stderr)

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = self.copy_kit()
        with open(os.path.join(src, 'lib', 'cleaner', 'engine.py'), 'w') as f:
            f.write('this is not python\n')
        code, _, err = self.install(sha=SHA2, source=src)
        self.assertEqual(code, 1)
        self.assertIn('failed its self-test', err)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/fork-cleaner/current')),
                         f'/usr/local/libexec/fork-cleaner/versions/{SHA1}')

    def test_foreign_link_is_refused_before_anything_is_written(self):
        os.makedirs(self.path('/usr/local/bin'))
        os.symlink('/somewhere/else', self.path('/usr/local/bin/fork-cleaner'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("is not the kit's link", err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))
        self.assertFalse(os.path.exists(self.path('/etc/fork-cleaner')))

    def test_one_installer_at_a_time(self):
        os.makedirs(self.path('/etc/fork-cleaner'))
        fd = os.open(self.path('/etc/fork-cleaner/install.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('another fork-cleaner-install is running', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))

    def test_bad_options_and_not_root_are_refused(self):
        self.assertEqual(self.install(sha='abc')[0], 1)
        self.assertEqual(self.install(source=self.root)[0], 1)
        self.m.ROOT_UID = os.geteuid() + 1
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.run_main('uninstall')[0], 1)
        self.assertFalse(os.path.exists(self.path('/etc/fork-cleaner')))


MUTATIONS = {
    'copy: the copy in use stays until the new one is whole': (
        'install', "        if name.startswith('.tmp-'):", "        if name.startswith('.tmp-') or name == sha:",
        'test_the_same_release_over_a_changed_copy_is_built_first_then_swapped'),
    'copy: the new copy is self-tested before the swap': (
        'install', '        problems = self_test(tmp)\n        if problems:', '        problems = []\n        if problems:',
        'test_the_same_release_over_a_changed_copy_is_built_first_then_swapped'),
    'copy: the swap without renameat2 cleans up': (
        'install', '    os.rename(aside, new)\n', '',
        'test_the_swap_without_renameat2_leaves_nothing_behind'),
    'source: a link to a file is refused': (
        'install', "ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW |", "ffd = os.open(parts[-1], os.O_RDONLY |",
        'test_a_link_in_the_source_is_never_followed'),
    'source: a link on the way is refused': (
        'install', 'DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC',
        'DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC', 'test_a_link_in_the_source_is_never_followed'),
    'files: root-owned': (
        'install', 'if st.st_uid != ROOT_UID or st.st_mode & 0o022 or', 'if st.st_mode & 0o022 or',
        'test_installed_files_must_be_root_owned_and_not_writable_by_others'),
    'files: not writable by others': (
        'install', 'if st.st_uid != ROOT_UID or st.st_mode & 0o022 or', 'if st.st_uid != ROOT_UID or',
        'test_installed_files_must_be_root_owned_and_not_writable_by_others'),
    'units: checked again before they are written': (
        'install', '            if os.path.lexists(p(path)) and not ours_unit(path):\n                raise',
        '            if False:\n                raise', 'test_a_unit_someone_writes_during_the_install_is_refused_and_kept'),
    'uninstall: takes the lock': (
        'install', '    with install_lock():                # never from under a running install\n        return _uninstall()',
        '    return _uninstall()', 'test_uninstall_waits_for_no_installer'),
    'bootstrap: the timer options are kept, nothing after them passed on': (
        'bootstrap', '    --timer|--no-timer) timer="$1"; shift ;;', '    --timer|--no-timer) break ;;',
        'test_bootstrap_passes_on_only_the_timer_options'),
}


class RedCheck(unittest.TestCase):
    def test_each_guard_has_a_test_that_fails_without_it(self):
        for rule, (where, text, broken, test) in MUTATIONS.items():
            with self.subTest(rule=rule):
                mutant = type('Broken', (Install,), {'broken': (where, text, broken)})
                result = unittest.TestResult()
                mutant(test).run(result)
                self.assertEqual(result.testsRun, 1)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with {rule!r} broken')
                self.assertFalse([e for e in result.errors if 'exactly once' in e[1]], f'{rule}: text not found')


if __name__ == '__main__':
    unittest.main(verbosity=2)
