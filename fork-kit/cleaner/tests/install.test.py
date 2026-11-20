"""fork-cleaner-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): the config, the daily timer units, and an update through an installed installer from before
fork-kit/common/kit_install.py (tests/fixtures/, a copy of that installer as released). What every kit's installer does (copy, switch, links, record,
self-check, stops half-way, uninstall) and the shared bootstrap are tested once for every kit in
fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck breaks each guard in a copy of
the installer and shows its test then fails.

  python3 -I -B fork-kit/cleaner/tests/install.test.py
"""
import contextlib, io, json, os, shutil, subprocess, sys, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
UNITS = ('/etc/systemd/user/fork-cleaner.service', '/etc/systemd/user/fork-cleaner.timer')
FOREIGN = '[Timer]\nOnCalendar=daily\n'
OLD_INSTALLER = os.path.join(KIT, 'tests', 'fixtures', 'fork-cleaner-install-before-kit-install')


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 'fork-cleaner-install')

    def config(self):
        with open(self.path('/etc/fork-cleaner/config.json')) as f:
            return json.load(f)

    def units(self):
        return [os.path.isfile(self.path(u)) for u in UNITS]

    def test_fresh_install_writes_the_config_and_no_timer(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config(), {'version': 1, 'timer': False})
        self.assertEqual(self.units(), [False, False])                 # no timer unless asked
        self.assertIn(', no timer', out)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_runs_again_with_the_timer_and_changes_nothing(self):
        self.assertEqual(self.install('--timer')[0], 0)
        before = self.snapshot()
        code, _, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)

    def test_every_stop_half_way_with_the_timer_then_run_again(self):
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

    def test_a_config_it_cannot_read_is_refused(self):
        os.makedirs(self.path('/etc/fork-cleaner'))
        for text in ('{not json', '[]'):
            with self.subTest(text=text):
                with open(self.path('/etc/fork-cleaner/config.json'), 'w') as f:
                    f.write(text)
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn('/etc/fork-cleaner/config.json', err)
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/fork-cleaner')))

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
        with open(self.path('/elsewhere'), 'w') as f:                       # a link is never ours either, even
            f.write('# cleaner kit: written by fork-cleaner-install.\n')     # to a file that says so
        os.symlink(self.path('/elsewhere'), self.path(UNITS[1]))
        self.assertEqual(self.install('--timer')[0], 1)

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

    def test_an_update_through_the_installer_from_before_kit_install(self):
        """A cleaner installed before the shared installer, updated with its installed fork-cleaner-install (not the
        bootstrap): that old code copies no common/kit_install.py, and its self-test runs only fork-cleaner, so it
        installs a copy whose installer cannot load. That installer then refuses cleanly and names the way out (the
        bootstrap), and the bootstrap's way (the release's own installer) makes the copy whole again."""
        src = self.copy_kit()                                       # the release (with RedCheck's break, if any)
        with open(os.path.join(src, 'sbin', 'fork-cleaner-install'), 'w') as f:
            f.write(fx.source_of(self.INSTALLER, self.broken[1:] if self.broken else None))
        old = fx._module('old_cleaner_installer', OLD_INSTALLER, fx.source_of(OLD_INSTALLER))
        old.PREFIX, old.ROOT_UID = self.root, os.geteuid()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = old.main(['install', '--source', src, '--sha', SHA1])
        self.assertEqual(code, 0, err.getvalue())                   # old code: it cannot be changed now
        self.assertFalse(os.path.exists(self.version(rel='common/kit_install.py')))
        installer = self.version(rel='sbin/fork-cleaner-install')
        for argv in (['--help'], ['self-check'], ['uninstall']):
            with self.subTest(argv=argv):
                r = subprocess.run([sys.executable, '-I', '-B', installer, *argv], capture_output=True, text=True,
                                   timeout=60, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn('fork-cleaner-install: cannot load its installer code', r.stderr)
                self.assertIn('install again with the bootstrap', r.stderr)
                self.assertNotIn('Traceback', r.stderr)
        code, out, err = self.install(source=src)                   # what the bootstrap runs
        self.assertEqual(code, 0, err)
        self.assertIn(f'versions/{SHA1} is incomplete or changed: made again', out)
        r = subprocess.run([sys.executable, '-I', '-B', installer, '--help'], capture_output=True, text=True,
                           timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(self.run_main('self-check')[0], 0)
        before = self.snapshot()                                    # and again: nothing changes
        self.assertEqual(self.install(source=src)[0], 0)
        self.assertEqual(self.snapshot(), before)


MUTATIONS = {
    'units: checked again before they are written': (
        'installer', '                if os.path.lexists(self.p(path)) and not self.ours_unit(path):\n'
        '                    raise', '                if False:\n                    raise',
        'test_a_unit_someone_writes_during_the_install_is_refused_and_kept'),
    'units: a foreign unit is refused before anything is written': (
        'installer', '        if timer and foreign:', '        if False:',
        'test_units_someone_else_wrote_are_refused_and_kept'),
    'units: a link is never ours': (
        'installer', '        if os.path.islink(real):\n            return False\n', '',
        'test_units_someone_else_wrote_are_refused_and_kept'),
    'installer: a copy without kit_install.py refuses cleanly': (
        'installer', 'except ImportError as e:', 'except ArithmeticError as e:',
        'test_an_update_through_the_installer_from_before_kit_install'),
    'timer: kept by a run without the option': (
        'installer', "bool(self.read_config().get('timer'))", 'False',
        'test_timer_kept_by_an_upgrade_and_removed_by_no_timer_and_uninstall'),
}


class RedCheck(unittest.TestCase):
    BASE = Install

    def test_each_guard_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
