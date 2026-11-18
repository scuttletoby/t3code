"""fork-cleaner-install on a fake host in a temporary folder (every fixed path under PREFIX, the current user as
root). No root, no systemd, no network.

  python3 -I -B fork-kit/cleaner/tests/install.test.py
"""
import contextlib, fcntl, importlib.machinery, importlib.util, io, json, os, shutil, subprocess, sys, tempfile, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SHA1, SHA2 = 'a' * 40, 'b' * 40
UNITS = ('/etc/systemd/user/fork-cleaner.service', '/etc/systemd/user/fork-cleaner.timer')
FOREIGN = '[Timer]\nOnCalendar=daily\n'


def load():
    path = os.path.join(KIT, 'sbin', 'fork-cleaner-install')
    loader = importlib.machinery.SourceFileLoader('fork_cleaner_install', path)
    spec = importlib.util.spec_from_loader('fork_cleaner_install', loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class Install(unittest.TestCase):
    def setUp(self):
        self.umask = os.umask(0o022)
        self.root = os.path.realpath(tempfile.mkdtemp(prefix='cleaner-install-test-'))
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

    def install(self, *extra, sha=SHA1, stop_after=None, source=KIT):
        return self.run_main('install', '--source', source, '--sha', sha, *extra, stop_after=stop_after)

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

    def test_broken_new_copy_leaves_the_version_in_use(self):
        self.assertEqual(self.install()[0], 0)
        src = os.path.join(self.root, 'src')
        shutil.copytree(KIT, src, ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(os.path.join(os.path.dirname(KIT), 'common'), os.path.join(self.root, 'common'),
                        ignore=shutil.ignore_patterns('__pycache__'))
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


if __name__ == '__main__':
    unittest.main(verbosity=2)
