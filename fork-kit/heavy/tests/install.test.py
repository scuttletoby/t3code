"""heavy-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as root).
No root, no systemd, no network.

  python3 -I -B fork-kit/heavy/tests/install.test.py
"""
import contextlib, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA1, SHA2 = 'a' * 40, 'b' * 40
OLD_WRAPPER = b'#!/bin/sh\nexec systemd-run --user --scope --quiet --slice=heavy.slice --collect -- "$@"\n'


def load():
    path = os.path.join(KIT, 'sbin', 'heavy-install')
    loader = importlib.machinery.SourceFileLoader('heavy_install', path)
    spec = importlib.util.spec_from_loader('heavy_install', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Install(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='heavy-install-test-'))
        self.addCleanup(self.close)
        self.m = load()
        self.m.PREFIX, self.m.ROOT_UID = self.root, os.geteuid()
        os.makedirs(self.path('/usr/local/bin'))

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

    def config(self):
        with open(self.path('/etc/heavy/config.json')) as f:
            return json.load(f)

    def snapshot(self):
        """Every path under the fake root with its kind, mode, mtime and link target."""
        out = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = os.path.join(dirpath, name)
                st = os.lstat(full)
                out[full[len(self.root):]] = (st.st_mode, st.st_mtime_ns,
                                              os.readlink(full) if os.path.islink(full) else None)
        return out

    def test_fresh_install_links_config_record_and_self_check(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.path('/usr/local/bin/heavy')), '/usr/local/libexec/heavy/current/bin/heavy')
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA1}')
        self.assertEqual(self.config(), {'version': 1, 'slice': 'heavy.slice', 'grace_seconds': 30})
        self.assertFalse(os.path.exists(self.path('/etc/systemd/user')))
        self.assertEqual(self.run_main('self-check')[0], 0)
        # the installed wrapper runs from its version folder
        r = subprocess.run([sys.executable, '-I', '-B', self.path(f'/usr/local/libexec/heavy/versions/{SHA1}/bin/heavy'),
                            '--help'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0)
        self.assertIn('heavy <command>', r.stdout)

    def test_runs_again_and_changes_nothing(self):
        self.assertEqual(self.install('--memory-max', '6G')[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_old_plain_wrapper_is_kept_and_put_back_by_uninstall(self):
        old = self.path('/usr/local/bin/heavy')
        with open(old, 'wb') as f:
            f.write(OLD_WRAPPER)
        os.chmod(old, 0o755)
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('is kept as /usr/local/libexec/heavy/replaced/heavy.', out)
        self.assertTrue(os.path.islink(old))
        self.assertEqual(self.install()[0], 0)                       # again: one backup only
        self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced'))), 1)
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertFalse(os.path.islink(old))
        with open(old, 'rb') as f:
            self.assertEqual(f.read(), OLD_WRAPPER)
        self.assertEqual(os.stat(old).st_mode & 0o777, 0o755)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))
        self.assertEqual(self.run_main('uninstall')[0], 0)            # again: nothing left, still fine

    def test_foreign_link_or_folder_is_refused_before_anything_is_written(self):
        os.symlink('/somewhere/else', self.path('/usr/local/bin/heavy'))
        code, _, err = self.install()
        self.assertEqual(code, 1)
        self.assertIn('is not the kit\'s link', err)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy')))
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                with open(self.path('/usr/local/bin/heavy'), 'wb') as f:
                    f.write(OLD_WRAPPER)
                os.chmod(self.path('/usr/local/bin/heavy'), 0o755)
                code, _, _ = self.install('--memory-high', '5G', '--sweep-timer', stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                # until the links step, the old wrapper is what runs
                if self.m.STEPS.index(step) < self.m.STEPS.index('links'):
                    self.assertFalse(os.path.islink(self.path('/usr/local/bin/heavy')))
                # a timer only once the release it runs is the one in use (#183 nit d)
                if os.path.exists(self.path('/etc/systemd/user/heavy-sweep.service')):
                    current = os.readlink(self.path('/usr/local/libexec/heavy/current'))     # absolute, as installed
                    self.assertTrue(os.path.isfile(self.path(current + '/lib/heavy_sweep.py')))
                    self.assertTrue(os.path.islink(self.path('/usr/local/bin/heavy')))
                code, _, err = self.install('--memory-high', '5G', '--sweep-timer')
                self.assertEqual(code, 0, err)
                self.assertTrue(os.path.isfile(self.path('/etc/systemd/user/heavy-sweep.timer')))
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertEqual(len(os.listdir(self.path('/usr/local/libexec/heavy/replaced'))), 1)

    def test_half_copied_version_is_made_again(self):
        self.assertEqual(self.install()[0], 0)
        bad = self.path(f'/usr/local/libexec/heavy/versions/{SHA1}/bin/heavy')
        with open(bad, 'a') as f:
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn('changed since install', out)
        os.makedirs(self.path('/usr/local/libexec/heavy/versions/.tmp-leftover'))
        code, out, _ = self.install()
        self.assertEqual(code, 0)
        self.assertIn('made again', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/heavy/versions/.tmp-leftover')))

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = os.path.join(self.root, 'src')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'common'),
                        ignore=shutil.ignore_patterns('__pycache__'))
        with open(os.path.join(src, 'bin', 'heavy'), 'w') as f:
            f.write('this is not python\n')
        code, _, err = self.install(sha=SHA2, source=src)
        self.assertEqual(code, 1)
        self.assertIn('failed its self-test', err)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA1}')

    def test_upgrade_keeps_config_and_the_old_version(self):
        self.assertEqual(self.install('--slice', 'builds.slice', '--grace', '2.5')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(self.config()['slice'], 'builds.slice')
        self.assertEqual(self.config()['grace_seconds'], 2.5)
        self.assertEqual(os.readlink(self.path('/usr/local/libexec/heavy/current')),
                         f'/usr/local/libexec/heavy/versions/{SHA2}')
        self.assertTrue(os.path.isdir(self.path(f'/usr/local/libexec/heavy/versions/{SHA1}')))

    def test_limits_write_the_slice_unit_and_uninstall_removes_it(self):
        code, out, err = self.install('--slice', 'builds.slice', '--memory-high', '5G', '--memory-max', '6G',
                                      '--swap-max', '1G', '--cpu-weight', '40')
        self.assertEqual(code, 0, err)
        unit = self.path('/etc/systemd/user/builds.slice')
        with open(unit) as f:
            text = f.read()
        for line in ('[Slice]', 'MemoryHigh=5G', 'MemoryMax=6G', 'MemorySwapMax=1G', 'CPUWeight=40'):
            self.assertIn(line + '\n', text)
        self.assertIn('daemon-reload', out)
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.exists(unit))

    def test_a_slice_unit_someone_else_wrote_is_not_overwritten(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path('/etc/systemd/user/heavy.slice'), 'w') as f:
            f.write('[Slice]\nMemoryMax=1G\n')
        code, _, err = self.install('--memory-max', '6G')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        with open(self.path('/etc/systemd/user/heavy.slice')) as f:
            self.assertEqual(f.read(), '[Slice]\nMemoryMax=1G\n')
        self.assertEqual(self.install()[0], 0)                      # without limits: used as it is

    def test_bad_options_are_refused(self):
        for extra in (['--slice', 'heavy'], ['--slice', '../x.slice'], ['--grace', '0'], ['--grace', 'soon'],
                      ['--memory-max', '6 GB'], ['--cpu-weight', '0'], ['--swap-max', '1G']):
            with self.subTest(extra=extra):
                code, _, err = self.install(*extra)
                self.assertEqual(code, 1, err)
        self.assertEqual(self.install(sha='abc')[0], 1)
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))

    def test_installed_version_runs_the_sweep_from_its_own_copy_of_common(self):
        self.assertEqual(self.install()[0], 0)
        version = self.path(f'/usr/local/libexec/heavy/versions/{SHA1}')
        self.assertTrue(os.path.isfile(os.path.join(version, 'common', 'machine_file.py')))
        self.assertTrue(os.path.isfile(os.path.join(version, 'lib', 'heavy_sweep.py')))
        moved = os.path.join(self.root, 'elsewhere')   # nothing of the repository next to it
        shutil.copytree(version, moved)
        for args in (['sweep', '--help'], ['docker-label', '--value']):
            r = subprocess.run([sys.executable, '-I', '-B', os.path.join(moved, 'bin', 'heavy'), *args],
                               capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
        self.assertRegex(r.stdout, r'^uid=\d+,(pid=\d+,start=\d+|scope=heavy-.+\.scope)\n$')

    def test_sweep_timer_units_written_kept_and_removed(self):
        units = [self.path('/etc/systemd/user/heavy-sweep.service'), self.path('/etc/systemd/user/heavy-sweep.timer')]
        code, out, err = self.install('--sweep-timer')
        self.assertEqual(code, 0, err)
        self.assertIn('systemctl --user enable --now heavy-sweep.timer', out)
        with open(units[0]) as f:
            text = f.read()
            self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/heavy/current/lib/heavy_sweep.py '
                          'sweep\n', text)
            self.assertIn('ConditionPathExists=/usr/local/libexec/heavy/current/lib/heavy_sweep.py\n', text)
        with open(units[1]) as f:
            self.assertIn('WantedBy=timers.target\n', f.read())
        self.assertTrue(self.config()['sweep_timer'])
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()
        self.assertEqual(self.install(sha=SHA1)[0], 0)              # again, without the option: kept
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.install(sha=SHA2)[0], 0)              # upgrade keeps it
        self.assertTrue(all(os.path.isfile(u) for u in units))
        self.assertEqual(self.install('--no-sweep-timer', sha=SHA2)[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))
        self.assertNotIn('sweep_timer', self.config())
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install('--sweep-timer', sha=SHA2)[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(any(os.path.exists(u) for u in units))

    def test_sweep_units_someone_else_wrote_are_refused_and_kept(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path('/etc/systemd/user/heavy-sweep.timer'), 'w') as f:
            f.write('[Timer]\nOnCalendar=daily\n')
        code, _, err = self.install('--sweep-timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        self.assertFalse(os.path.exists(self.path('/etc/heavy')))
        self.assertEqual(self.install()[0], 0)                      # without the option it is left alone
        self.assertEqual(self.run_main('uninstall')[0], 0)
        with open(self.path('/etc/systemd/user/heavy-sweep.timer')) as f:
            self.assertEqual(f.read(), '[Timer]\nOnCalendar=daily\n')

    def test_not_root_is_refused(self):
        self.m.ROOT_UID = os.geteuid() + 1
        self.assertEqual(self.install()[0], 1)
        self.assertEqual(self.run_main('uninstall')[0], 1)


if __name__ == '__main__':
    unittest.main(verbosity=2)
