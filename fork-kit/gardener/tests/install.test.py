"""gardener-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current
user as root): the config and the timer units. What every kit's installer does (copy, switch, links, record,
self-check, stops half-way, uninstall) and the shared bootstrap are tested once for every kit in
fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck breaks each guard in a copy of
the installer and shows its test then fails.

  python3 -I -B fork-kit/gardener/tests/install.test.py
"""
import json, os, shutil, sys, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
UNITS = ('/etc/systemd/user/gardener.service', '/etc/systemd/user/gardener.timer')
FOREIGN = '[Timer]\nOnCalendar=daily\n'


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 'gardener-install')

    def config(self):
        with open(self.path('/etc/gardener/config.json')) as f:
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
                    current = os.readlink(self.path('/usr/local/libexec/gardener/current'))
                    self.assertTrue(os.path.isfile(self.path(current + '/bin/gardener')))
                    self.assertTrue(os.path.islink(self.path('/usr/local/bin/gardener')))
                code, _, err = self.install('--timer')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.units(), [True, True])
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_a_config_it_cannot_read_is_refused(self):
        os.makedirs(self.path('/etc/gardener'))
        for text in ('{not json', '[]'):
            with self.subTest(text=text):
                with open(self.path('/etc/gardener/config.json'), 'w') as f:
                    f.write(text)
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn('/etc/gardener/config.json', err)
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/gardener')))

    def test_the_timer_unit_ticks_every_hour_from_the_release_in_use(self):
        code, out, err = self.install('--timer')
        self.assertEqual(code, 0, err)
        self.assertIn('systemctl --user enable --now gardener.timer', out)
        with open(self.path(UNITS[0])) as f:
            text = f.read()
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/gardener/current/bin/gardener '
                      'run --timer\n', text)
        self.assertIn('Type=oneshot\n', text)
        self.assertIn('UMask=0077\n', text)                            # files it makes: this user's only
        self.assertIn('IOSchedulingClass=idle\n', text)                # report work never competes with builds
        with open(self.path(UNITS[1])) as f:
            text = f.read()
        self.assertIn('OnCalendar=hourly\n', text)
        self.assertIn('WantedBy=timers.target\n', text)
        names = os.listdir(self.path('/etc/systemd/user'))
        self.assertEqual(sorted(n for n in names if n.endswith('.timer')), ['gardener.timer'])   # one timer only
        self.assertTrue(self.config()['timer'])
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_installed_checks_run_from_the_release(self):
        """Each general check in the installed copy finds the kit's lib beside it (no source tree needed)."""
        import subprocess
        self.assertEqual(self.install()[0], 0)
        release = self.path(os.readlink(self.path('/usr/local/libexec/gardener/current')))
        ctx = json.dumps({'version': 1, 'project': {'name': 'x', 'repo': '', 'root': self.root}, 'check': 'c',
                          'options': {'files': {}}, 'fix': False, 'today': '2026-11-23', 'gh': 'gh'})
        for name in ('context-budget', 'worktrees'):
            path = os.path.join(release, 'checks', name)
            self.assertTrue(os.access(path, os.X_OK), name)
            r = subprocess.run([sys.executable, '-I', '-B', path], input=ctx, capture_output=True, text=True,
                               cwd=self.root, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr)

    def assert_disable_said_first(self, out):
        """Users are told to disable the timer before the units go, not after (no dangling wants link)."""
        said = out.find('systemctl --user disable --now gardener.timer')
        removed = out.find('removed /etc/systemd/user/')
        self.assertGreaterEqual(said, 0, out)
        self.assertGreaterEqual(removed, 0, out)
        self.assertLess(said, removed, out)

    def test_timer_kept_by_an_upgrade_and_removed_by_no_timer_and_uninstall(self):
        self.assertEqual(self.install('--timer')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)                 # upgrade without the option: kept
        self.assertEqual(self.units(), [True, True])
        self.assertTrue(os.path.isdir(self.path(f'/usr/local/libexec/gardener/versions/{SHA1}')))
        code, out, _ = self.install('--no-timer', sha=SHA2)
        self.assertEqual(code, 0)
        self.assert_disable_said_first(out)
        self.assertEqual(self.units(), [False, False])
        self.assertEqual(self.install(sha=SHA2)[0], 0)                 # again without the option: still off
        self.assertEqual(self.units(), [False, False])
        self.assertEqual(self.run_main('self-check')[0], 0)
        self.assertEqual(self.install('--timer', sha=SHA2)[0], 0)
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assert_disable_said_first(out)
        self.assertEqual(self.units(), [False, False])
        for gone in ('/usr/local/libexec/gardener', '/etc/gardener', '/usr/local/bin/gardener',
                     '/usr/local/sbin/gardener-install'):
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
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/gardener')))
        self.assertEqual(self.install()[0], 0)                          # without the option it is left alone
        self.assertEqual(self.run_main('uninstall')[0], 0)
        with open(self.path(UNITS[1])) as f:
            self.assertEqual(f.read(), FOREIGN)
        os.unlink(self.path(UNITS[1]))
        with open(self.path('/elsewhere'), 'w') as f:                       # a link is never ours either, even
            f.write('# gardener kit: written by gardener-install.\n')     # to a file that says so
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
    'timer: kept by a run without the option': (
        'installer', "bool(self.read_config().get('timer'))", 'False',
        'test_timer_kept_by_an_upgrade_and_removed_by_no_timer_and_uninstall'),
    'units: disable is said before they are removed': (
        'installer', "        if ours:\n            self.say(f'the timer goes: each user who enabled it runs `{DISABLE}` first')\n",
        '', 'test_timer_kept_by_an_upgrade_and_removed_by_no_timer_and_uninstall'),
}


class RedCheck(unittest.TestCase):
    BASE = Install

    def test_each_guard_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
