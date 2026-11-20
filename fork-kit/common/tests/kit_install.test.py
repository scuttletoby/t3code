"""The installer contract every kit on fork-kit/common/kit_install.py keeps, run against each of them, and the shared
bootstrap.sh's options. A fake host in a temporary folder (every fixed path under PREFIX, the current user as
root); no root, no systemd, no network. RedCheck breaks each guard in a copy of kit_install.py (or of bootstrap.sh)
and shows its test then fails.

  python3 -I -B fork-kit/common/tests/kit_install.test.py
"""
import contextlib, fcntl, os, shutil, subprocess, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
KITS = {
    'cleaner': os.path.join(fx.FORK_KIT, 'cleaner', 'sbin', 'fork-cleaner-install'),
    'golive': os.path.join(fx.FORK_KIT, 'golive', 'sbin', 't3-golive-install'),
}


class Contract:
    """Mixed into one FakeHost per kit (below)."""

    def first_link(self):
        return sorted(self.m.LINKS)[0]

    def module_file(self, sha=SHA1):
        return self.version(sha, f'lib/{self.m.PACKAGE}/{self.m.MODULES[-1]}.py')

    def test_every_module_of_the_kit_is_installed(self):
        lib = os.path.join(self.KIT_DIR, 'lib', self.m.PACKAGE)
        self.assertEqual(sorted(n[:-3] for n in os.listdir(lib) if n.endswith('.py')), sorted(self.m.MODULES))
        found = []
        for dirpath, dirnames, filenames in os.walk(self.KIT_DIR):
            dirnames[:] = [d for d in dirnames if d not in ('tests', '__pycache__')]
            found += [os.path.relpath(os.path.join(dirpath, n), self.KIT_DIR) for n in filenames]
        missing = sorted(set(found) - set(self.m.source_files()) - {'bootstrap.sh'})
        self.assertEqual(missing, [], 'a file of the kit the installer does not copy')
        self.assertIn('common/kit_install.py', self.m.source_files())

    def test_fresh_install_links_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        for path, rel in self.m.LINKS.items():
            self.assertEqual(os.readlink(self.path(path)), f'{self.m.LIBEXEC}/current/{rel}')
        self.assertEqual(self.current(), f'{self.m.LIBEXEC}/versions/{SHA1}')
        self.assertEqual(self.run_main('self-check')[0], 0)
        for rel in self.m.LINKS.values():                           # each link leads to a file of the copy
            self.assertTrue(os.path.isfile(self.version(rel=rel)), rel)
            self.assertIn(rel, self.m.EXECUTABLE)
        moved = os.path.join(self.root, 'elsewhere')                # runs from its own copy of common/
        shutil.copytree(self.version(), moved)
        rel, text = self.m.HELP
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, rel), '--help'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(text, r.stdout)
        installer = [rel for rel in self.m.LINKS.values() if rel.startswith('sbin/')][0]
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, installer), '--help'],
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)                 # the installed installer finds kit_install
        self.assertIn('self-check', r.stdout)

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
                os.makedirs(self.root)
                code, _, _ = self.install(stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                if os.path.lexists(self.path(self.m.CURRENT)):      # current only ever names a whole copy
                    self.assertEqual(self.m.tree_problems(self.path(self.current())), [])
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertEqual(os.listdir(self.path(self.m.VERSIONS)), [SHA1])

    def test_a_link_to_a_half_copied_version_is_made_whole(self):
        """A copy made by hand (or by an installer killed in the old days) that `current` names: whatever is in
        versions/<sha> without a whole manifest is made again, and the link stays on the sha."""
        half = self.version()
        os.makedirs(os.path.join(half, 'bin'))
        rel = self.m.HELP[0]
        with open(os.path.join(half, rel), 'w') as f:
            f.write('half\n')
        os.makedirs(self.path(self.m.LIBEXEC), exist_ok=True)
        os.symlink(f'{self.m.LIBEXEC}/versions/{SHA1}', self.path(self.m.CURRENT))
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        os.unlink(self.version(rel='MANIFEST.sha256'))              # the same with its manifest gone
        code, out, err = self.install()
        self.assertEqual((code, 'made again' in out), (0, True), err)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_copy_from_an_older_installer_is_made_again(self):
        """A version folder whose manifest lacks a file this release copies (an install from before the shared
        installer had no common/kit_install.py) is made again."""
        self.assertEqual(self.install()[0], 0)
        dest = self.version()
        os.unlink(os.path.join(dest, 'common', 'kit_install.py'))
        with open(os.path.join(dest, 'MANIFEST.sha256')) as f:
            lines = [ln for ln in f if not ln.rstrip().endswith('  common/kit_install.py')]
        with open(os.path.join(dest, 'MANIFEST.sha256'), 'w') as f:
            f.writelines(lines)
        self.assertEqual(self.m.tree_problems(dest), [])            # whole as far as its own manifest says
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertIn('made again', out)
        self.assertTrue(os.path.isfile(os.path.join(dest, 'common', 'kit_install.py')))
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_an_upgrade_keeps_the_old_version_folder(self):
        """A timer or a detached run started from the old release names its folder: it stays."""
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(self.current(), f'{self.m.LIBEXEC}/versions/{SHA2}')
        self.assertEqual(sorted(os.listdir(self.path(self.m.VERSIONS))), [SHA1, SHA2])
        self.assertEqual(self.m.tree_problems(self.version(SHA1)), [])
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_changed_copy_is_found_and_made_again(self):
        self.assertEqual(self.install()[0], 0)
        with open(self.module_file(), 'a') as f:
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('changed since install', out)
        os.makedirs(self.path(f'{self.m.VERSIONS}/.tmp-leftover'))
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertFalse(os.path.exists(self.path(f'{self.m.VERSIONS}/.tmp-leftover')))

    def test_the_same_release_over_a_changed_copy_is_built_first_then_swapped(self):
        """#491: a reinstall of the release in use over a changed copy builds the new copy beside it and swaps it in
        whole: a copy that stops half way, or fails its self-test, leaves the copy in use where it was."""
        self.assertEqual(self.install()[0], 0)
        changed = self.module_file()
        with open(changed, 'a') as f:
            f.write('# changed\n')
        with open(changed) as f:
            text = f.read()
        real, calls = self.m.read_source, []

        def stops_half_way(source, rel):                    # the source is read whole once by the option check
            calls.append(rel)
            if len(calls) == len(self.m.source_files()) + 3:
                raise self.m.Refused('the source went away (test)')
            return real(source, rel)
        with mock.patch.object(self.m, 'read_source', stops_half_way):
            code, _, err = self.install()
        self.assertEqual(code, 1, err)
        with open(changed) as f:
            self.assertEqual(f.read(), text)                # the copy in use: still all there
        self.assertEqual(self.current(), f'{self.m.LIBEXEC}/versions/{SHA1}')
        self.assertTrue(os.path.isfile(self.version(rel=self.m.HELP[0])))
        real_test = self.m.self_test
        with mock.patch.object(self.m, 'self_test',
                               lambda dest: ['broken (test)'] if '.tmp-' in dest else real_test(dest)):
            code, out, err = self.install()
        self.assertEqual(code, 1, err)
        self.assertIn('the new copy failed its self-test (the version in use is unchanged)', err)
        self.assertNotIn('made again', out)
        with open(changed) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual(os.listdir(self.path(self.m.VERSIONS)), [SHA1])
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
        self.assertEqual(os.listdir(self.path(self.m.VERSIONS)), [SHA1])
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
        self.assertFalse(os.path.exists(self.path(self.m.LIBEXEC)))
        os.unlink(readme)
        shutil.copy(os.path.join(self.KIT_DIR, 'README.md'), readme)
        lib = os.path.join(src, 'lib')                      # a link on the way to a file
        os.rename(lib, os.path.join(self.root, 'lib-elsewhere'))
        os.symlink(os.path.join(self.root, 'lib-elsewhere'), lib)
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1)
        self.assertIn('the installer copies no links', err)
        self.assertFalse(os.path.exists(self.path(self.m.LIBEXEC)))
        os.unlink(lib)
        os.rename(os.path.join(self.root, 'lib-elsewhere'), lib)
        self.assertEqual(self.install(source=src)[0], 0)    # the same folder without links installs

    def test_installed_files_must_be_root_owned_and_not_writable_by_others(self):
        self.assertEqual(self.install()[0], 0)
        for change in ('group-writable', 'another owner'):
            with self.subTest(change=change):
                if change == 'group-writable':
                    os.chmod(self.module_file(), 0o664)
                else:
                    self.m.ROOT_UID = os.geteuid() + 1
                code, out, _ = self.run_main('self-check')
                self.assertEqual(code, 1, out)
                self.assertIn('not root-owned, writable by others, or a link', out)
                self.m.ROOT_UID = os.geteuid()
                self.assertEqual(self.install()[0], 0)      # the installer makes it again
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_module_the_help_never_loads_is_still_imported_by_the_self_test(self):
        src = self.copy_kit()
        with open(os.path.join(src, 'lib', self.m.PACKAGE, 'zz_extra.py'), 'w') as f:
            f.write('raise ImportError("broken (test)")\n')
        self.m.MODULES = tuple(self.m.MODULES) + ('zz_extra',)
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1)
        self.assertIn(f'lib/{self.m.PACKAGE} does not import', err)
        self.assertIn('broken (test)', err)
        self.assertFalse(os.path.lexists(self.path(self.m.CURRENT)))

    def test_the_self_test_runs_the_installer_help_too(self):
        """A copy whose own installer cannot load (an installer from before kit_install.py copied no
        common/kit_install.py) could not be updated, checked or uninstalled later: it is refused."""
        src = self.copy_kit()
        installer = os.path.join(src, 'sbin', self.m.PROG)
        text = fx.source_of(installer, ('    import kit_install', '    import kit_install_gone'))
        with open(installer, 'w') as f:
            f.write(text)
        code, _, err = self.install(source=src)
        self.assertEqual(code, 1)
        self.assertIn(f'sbin/{self.m.PROG} --help failed', err)
        self.assertIn('cannot load its installer code', err)            # its own refusal, not a traceback
        self.assertFalse(os.path.lexists(self.path(self.m.CURRENT)))

    def transient(self, folder, name, text):
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), 'w') as f:
            f.write(text)
        return os.path.join(folder, name)

    def test_uninstall_is_refused_while_code_of_the_kit_runs(self):
        """A process or a transient unit (systemd-run's: a detached run, a timer still pending) that runs code from
        <libexec> or through the kit's links: removing it would leave that with nothing to run."""
        code_file = f'{self.m.LIBEXEC}/versions/{SHA1}/{self.m.HELP[0]}'
        user_units = os.path.join(self.m.RUN, 'user', '1000', 'systemd', 'transient')
        cases = {
            'a process of a version': lambda: self.process(4242, '/usr/bin/python3', '-I', '-B', code_file, 'hold'),
            'a process through a link': lambda: self.process(4243, '/usr/bin/python3', '-IB', self.first_link()),
            'a user unit (a pending timer)': lambda: self.transient(
                user_units, 'kit-x-guard.service', '[Service]\nExecStart=\nExecStart="/usr/bin/python3" "-I" '
                f'"-B" "{code_file}" "switch-guard" "--run" "r1"\n'),
            'a system unit through a link': lambda: self.transient(
                os.path.join(self.m.RUN, 'systemd', 'transient'), 'run-u7.service',
                f'[Service]\nExecStart="{self.first_link()}" "daily"\n'),
        }
        for what, make in cases.items():
            with self.subTest(what=what):
                self.assertEqual(self.install()[0], 0)
                made = make()
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn('code of this kit is in use', err)
                self.assertIn('run uninstall again', err)
                self.assertTrue(os.path.isdir(self.version()))
                self.assertTrue(os.path.islink(self.path(self.first_link())))
                if made:
                    os.unlink(made)
                else:
                    shutil.rmtree(self.m.PROC)
                    os.makedirs(self.m.PROC)
                self.assertEqual(self.run_main('uninstall')[0], 0)          # once it ended
                self.assertFalse(os.path.lexists(self.path(self.m.LIBEXEC)))

    def test_what_is_not_the_kit_never_holds_uninstall(self):
        """Other code, a name that only starts like the kit's folder, this uninstall and what started it, and a
        FIFO among the transient units (never waited on) all let it go ahead; an unreadable process list refuses."""
        self.assertEqual(self.install()[0], 0)
        self.process(4250, '/usr/bin/python3', f'{self.m.LIBEXEC}-host/bin/x')        # another folder
        self.process(4251, '/bin/sh', '-c', 'true')
        self.process(os.getpid(), '/usr/bin/python3', '-IB', self.first_link(), 'uninstall')   # this one
        with open(os.path.join(self.m.PROC, str(os.getpid()), 'stat'), 'w') as f:
            f.write(f'{os.getpid()} (python3) S 4260 1 1 0\n')
        self.process(4260, 'sudo', self.first_link(), 'uninstall')                    # what started it
        units = os.path.join(self.m.RUN, 'user', '1000', 'systemd', 'transient')
        self.transient(units, 'other.service', f'[Service]\nExecStart="{self.m.LIBEXEC}-host/bin/x"\n')
        os.mkfifo(os.path.join(units, 'planted.service'))
        with self.no_hang():
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.install()[0], 0)
        shutil.rmtree(self.m.PROC)
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('cannot be read', err)
        self.assertTrue(os.path.isdir(self.version()))

    def test_a_replaced_folder_that_is_not_a_folder_is_refused_before_anything_is_written(self):
        os.makedirs(self.path(self.m.LIBEXEC))
        os.symlink('/somewhere/else', self.path(self.m.REPLACED))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn(f'{self.m.REPLACED} exists and is not a folder', err)
        self.assertFalse(os.path.exists(self.path(self.m.VERSIONS)))
        self.assertFalse(os.path.exists(self.path(self.m.ETC)))

    def test_old_state_is_checked_again_under_the_lock(self):
        """What another process wrote between the first check and the lock is refused before anything is
        written (a folder at a link path, here)."""
        take = self.m.install_lock

        @contextlib.contextmanager
        def lock_then_someone_writes():
            with take():
                os.makedirs(self.path(self.first_link()))
                yield

        self.m.install_lock = lock_then_someone_writes
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn("is not the kit's link", err)
        self.assertFalse(os.path.exists(self.path(self.m.VERSIONS)))

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = self.copy_kit()
        with open(os.path.join(src, 'lib', self.m.PACKAGE, f'{self.m.MODULES[-1]}.py'), 'w') as f:
            f.write('this is not python\n')
        code, _, err = self.install(sha=SHA2, source=src)
        self.assertEqual(code, 1)
        self.assertIn('failed its self-test', err)
        self.assertEqual(self.current(), f'{self.m.LIBEXEC}/versions/{SHA1}')

    def test_uninstall_leaves_nothing_and_runs_again(self):
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        for gone in (self.m.LIBEXEC, self.m.ETC, *self.m.LINKS):
            self.assertFalse(os.path.lexists(self.path(gone)), gone)
        self.assertEqual(self.run_main('uninstall')[0], 0)          # again: nothing left, still fine
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('not installed?', out)

    def test_uninstall_waits_for_no_installer(self):
        self.assertEqual(self.install()[0], 0)
        fd = os.open(self.path(self.m.LOCK), os.O_RDWR)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn(f'another {self.m.PROG} is running', err)
        self.assertTrue(os.path.isdir(self.version()))
        self.assertTrue(os.path.islink(self.path(self.first_link())))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.run_main('uninstall')[0], 0)

    def test_foreign_link_is_refused_before_anything_is_written(self):
        for path in self.m.LINKS:
            with self.subTest(path=path):
                shutil.rmtree(self.root)
                os.makedirs(self.path(os.path.dirname(path)))
                os.symlink('/somewhere/else', self.path(path))
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn("is not the kit's link", err)
                self.assertFalse(os.path.exists(self.path(self.m.LIBEXEC)))
                self.assertFalse(os.path.exists(self.path(self.m.ETC)))

    def test_one_installer_at_a_time(self):
        os.makedirs(self.path(self.m.ETC))
        fd = os.open(self.path(self.m.LOCK), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn(f'another {self.m.PROG} is running', err)
        self.assertFalse(os.path.exists(self.path(self.m.LIBEXEC)))

    def test_bad_options_and_not_root_are_refused(self):
        self.assertEqual(self.install(sha='abc')[0], 1)
        self.assertEqual(self.install(source=self.root)[0], 1)
        self.m.ROOT_UID = os.geteuid() + 1
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.run_main('uninstall')[0], 1)
        self.assertFalse(os.path.exists(self.path(self.m.ETC)))


class Cleaner(Contract, fx.FakeHost):
    KIT_DIR, INSTALLER = os.path.join(fx.FORK_KIT, 'cleaner'), KITS['cleaner']


class Golive(Contract, fx.FakeHost):
    KIT_DIR, INSTALLER = os.path.join(fx.FORK_KIT, 'golive'), KITS['golive']


class Bootstrap(fx.FakeHost):
    """bootstrap.sh's options. As root it would go on to download, so the root-only line is checked as a user."""
    KIT_DIR, INSTALLER = Cleaner.KIT_DIR, KITS['cleaner']

    def refused(self, *args):
        done = self.run_bootstrap(*args)
        self.assertEqual(done.returncode, 1, done.stdout)
        return done.stderr.strip()

    def test_the_kit_is_one_it_knows(self):
        self.assertEqual(self.refused('--repo', 'a/b', '--sha', SHA1), 'fork-kit-bootstrap: --kit must be one of: '
                         'cleaner golive')
        self.assertIn('--kit must be one of', self.refused('--kit', '../heavy', '--repo', 'a/b', '--sha', SHA1))
        self.assertEqual(self.refused('--repo', 'a/b', '--sha', SHA1, '--kit'),
                         'fork-kit-bootstrap: --kit needs a value')

    def test_only_the_kit_options_are_passed_on(self):
        """Nothing reaches the installer unchecked: a second --source would install a folder the release check
        never saw."""
        for kit in ('cleaner', 'golive'):
            for extra in (['--source', '/elsewhere'], ['--x']):
                with self.subTest(kit=kit, extra=extra):
                    err = self.refused('--kit', kit, '--repo', 'a/b', '--sha', SHA1, *extra)
                    self.assertIn(f'unknown option {extra[0]}', err)
        self.assertEqual(self.refused('--kit', 'cleaner', '--repo', 'a/b', '--sha', SHA1, '--timer', '--sha'),
                         'fork-kit-bootstrap: --sha needs a value')
        self.assertIn('--sha must be the full 40-character release commit',
                      self.refused('--kit', 'cleaner', '--repo', 'a/b', '--sha', SHA1, '--timer', '--sha', 'abc'))
        for timer in ('--timer', '--no-timer'):                  # an option of one kit only
            self.assertEqual(self.refused(timer, '--kit', 'golive', '--repo', 'a/b', '--sha', SHA1),
                             f'fork-kit-bootstrap: {timer} is not an option of --kit golive')
        if os.geteuid() != 0:                                    # as root it would go on to download
            for args in (['--kit', 'cleaner', '--timer'], ['--no-timer', '--kit', 'cleaner'], ['--kit', 'golive']):
                with self.subTest(args=args):
                    self.assertIn('run this as root', self.refused('--repo', 'a/b', '--sha', SHA1, *args))


MUTATIONS = {
    'copy: the copy in use stays until the new one is whole': (
        'engine', "            if name.startswith('.tmp-'):", "            if name.startswith('.tmp-') or name == sha:",
        'test_the_same_release_over_a_changed_copy_is_built_first_then_swapped'),
    'copy: the new copy is self-tested before the swap': (
        'engine', '            problems = self.self_test(tmp)\n            if problems:',
        '            problems = []\n            if problems:',
        'test_the_same_release_over_a_changed_copy_is_built_first_then_swapped'),
    'copy: the swap without renameat2 cleans up': (
        'engine', '        os.rename(aside, new)\n', '', 'test_the_swap_without_renameat2_leaves_nothing_behind'),
    'copy: a copy whose manifest lacks a file is made again': (
        'engine', ' and set(read_manifest(dest)) == set(files):', ':',
        'test_a_copy_from_an_older_installer_is_made_again'),
    'copy: a version folder without its manifest is made again': (
        'engine', "            return [f'{folder}: no readable MANIFEST.sha256 ({e.strerror})']",
        '            return []', 'test_a_link_to_a_half_copied_version_is_made_whole'),
    'self-test: every module is imported': (
        'engine', 'for m in sys.argv[2:]: __import__', 'for m in []: __import__',
        'test_a_module_the_help_never_loads_is_still_imported_by_the_self_test'),
    'source: a link to a file is refused': (
        'engine', "ffd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW |", "ffd = os.open(parts[-1], os.O_RDONLY |",
        'test_a_link_in_the_source_is_never_followed'),
    'source: a link on the way is refused': (
        'engine', 'DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC',
        'DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC', 'test_a_link_in_the_source_is_never_followed'),
    'files: root-owned': (
        'engine', 'if st.st_uid != self.ROOT_UID or st.st_mode & 0o022 or', 'if st.st_mode & 0o022 or',
        'test_installed_files_must_be_root_owned_and_not_writable_by_others'),
    'files: not writable by others': (
        'engine', 'if st.st_uid != self.ROOT_UID or st.st_mode & 0o022 or', 'if st.st_uid != self.ROOT_UID or',
        'test_installed_files_must_be_root_owned_and_not_writable_by_others'),
    'links: a foreign link is refused': (
        'engine', "            raise Refused(f'{path} exists and is not the kit\\'s link", "            return\n"
        "            raise Refused(f'{path} exists and is not the kit\\'s link",
        'test_foreign_link_is_refused_before_anything_is_written'),
    'uninstall: takes the lock': (
        'engine', '        with self.install_lock():           # never from under a running install\n'
        '            return self._uninstall()', '        return self._uninstall()',
        'test_uninstall_waits_for_no_installer'),
    'self-test: the installer of the copy runs too': (
        'engine', "        for rel, text in (self.HELP, (f'sbin/{self.PROG}', 'self-check')):",
        '        for rel, text in (self.HELP,):', 'test_the_self_test_runs_the_installer_help_too'),
    'uninstall: refused while code of the kit runs': (
        'engine', '            if in_use:\n', '            if False:\n',
        'test_uninstall_is_refused_while_code_of_the_kit_runs'),
    'uninstall: a transient unit (a pending timer) counts': (
        'engine', "                    found.append(f'transient unit", "                    str(f'transient unit",
        'test_uninstall_is_refused_while_code_of_the_kit_runs'),
    'uninstall: a process through the kit\'s links counts': (
        'engine', 'arg.startswith(roots) or arg in links', 'arg.startswith(roots)',
        'test_uninstall_is_refused_while_code_of_the_kit_runs'),
    'uninstall: this uninstall and what started it do not count': (
        'engine', '            if int(name) in mine:', '            if int(name) == os.getpid():',
        'test_what_is_not_the_kit_never_holds_uninstall'),
    'uninstall: a FIFO among the units is never waited on': (
        'engine', 'fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)',
        'fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)', 'test_what_is_not_the_kit_never_holds_uninstall'),
    'uninstall: no process list refuses': (
        'engine', "            raise Refused(f'{self.PROC} cannot be read", "            return []\n"
        "            raise Refused(f'{self.PROC} cannot be read", 'test_what_is_not_the_kit_never_holds_uninstall'),
    'check: the replaced folder is checked': (
        'engine', 'for path in (self.ETC, self.LIBEXEC, self.VERSIONS, self.REPLACED):',
        'for path in (self.ETC, self.LIBEXEC, self.VERSIONS):',
        'test_a_replaced_folder_that_is_not_a_folder_is_refused_before_anything_is_written'),
    'check: again under the lock': (
        'engine', '        self.check_old_state()      # again, now that no other installer runs\n', '',
        'test_old_state_is_checked_again_under_the_lock'),
    'bootstrap: --timer is kept, nothing after it passed on': (
        'bootstrap', '    --timer|--no-timer) opt="$1"; shift ;;', '    --timer|--no-timer) break ;;',
        'test_only_the_kit_options_are_passed_on'),
    'bootstrap: golive takes no --timer': (
        'bootstrap', ' [ -z "$opt" ] || die "$opt is not an option of --kit golive" ;;', ' ;;',
        'test_only_the_kit_options_are_passed_on'),
    'bootstrap: an unknown kit is refused': (
        'bootstrap', '  *) die "--kit must be one of: cleaner golive" ;;', '  *) installer=x ;;',
        'test_the_kit_is_one_it_knows'),
}


class RedCheck(unittest.TestCase):
    def test_each_guard_has_a_test_that_fails_without_it(self):
        for kit, base in (('cleaner', Cleaner), ('golive', Golive)):
            for rule, m in MUTATIONS.items():
                if m[0] == 'bootstrap' and kit != 'cleaner':
                    continue
                with self.subTest(kit=kit):
                    self.BASE = Bootstrap if m[0] == 'bootstrap' else base
                    fx.red_check(self, {rule: m})


if __name__ == '__main__':
    unittest.main(verbosity=2)
