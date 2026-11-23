"""update-policy-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current
user as root): the timer choice, the system units, the apt file that turns unattended-upgrades' own restarts off,
and the guards on enabled units. What every kit's installer does (copy, switch, links, record, self-check, stops
half-way, uninstall) and the shared bootstrap are tested once for every kit in
fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck breaks each guard in a copy of
the installer and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/install.test.py
"""
import json, os, shutil, sys, unittest
from unittest import mock

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
UNIT_DIR = '/etc/systemd/system'
UNITS = tuple(f'{UNIT_DIR}/{n}' for n in ('update-policy.service', 'update-policy.timer',
                                          'update-policy-baseline.service', 'update-policy-baseline.timer',
                                          'update-policy-boot.service', 'update-policy-exposure.service',
                                          'update-policy-exposure.timer'))
APT = '/etc/apt/apt.conf.d/zz-update-policy'
FILES = UNITS + (APT,)
FOREIGN = 'Unattended-Upgrade::Automatic-Reboot "true";\n'


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 'update-policy-install')

    def config(self):
        with open(self.path('/etc/update-policy/config.json')) as f:
            return json.load(f)

    def there(self):
        return [os.path.isfile(self.path(p)) for p in FILES]

    def read(self, path):
        with open(self.path(path)) as f:
            return f.read()

    def setUp(self):
        super().setUp()
        self.calls = []                  # (systemctl args, whether the apt file was there then)
        self.systemctl_error = {}        # first arg -> error
        self.apt_value = 'false'

        def systemctl(*args):
            self.calls.append((args, os.path.exists(self.path(APT))))
            return self.systemctl_error.get(args[0])
        self.m.systemctl = systemctl
        self.m.apt_reboot = lambda: self.apt_value

    def test_fresh_install_without_the_timer_writes_nothing_outside_the_kit(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(self.config(), {'version': 1, 'timer': False})
        self.assertEqual(self.there(), [False] * len(FILES))
        self.assertIn(', no timer', out)
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_timer_writes_the_units_and_turns_apts_own_restarts_off(self):
        code, out, err = self.install('--timer')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.there(), [True] * len(FILES))
        self.assertIn('enabled and started update-policy.timer, update-policy-baseline.timer', out)
        self.assertIn('\nUnattended-Upgrade::Automatic-Reboot "false";\n', self.read(APT))
        self.assertTrue(self.read(APT).startswith('// update-policy kit: written by update-policy-install.'))
        service = self.read(UNITS[0])
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/update-policy/current/bin/update-policy '
                      'tick\n', service)
        self.assertIn('ConditionPathExists=/usr/local/libexec/update-policy/current/bin/update-policy\n', service)
        self.assertIn('OnCalendar=*:0/5\n', self.read(UNITS[1]))
        self.assertIn('update-policy baseline\n', self.read(UNITS[2]))
        self.assertIn('Persistent=true\n', self.read(UNITS[3]))
        self.assertIn('update-policy booted\n', self.read(UNITS[4]))
        exposure = self.read(UNITS[5])
        self.assertIn('\nDynamicUser=yes\n', exposure)                      # the check runs unprivileged,
        self.assertIn('\nExecStopPost=+/usr/bin/python3 -I -B /usr/local/libexec/update-policy/current/bin/'
                      'update-policy exposure-alert', exposure)               # the alert as root
        self.assertIn('Persistent=true\n', self.read(UNITS[6]))
        self.assertIn('WantedBy=multi-user.target\n', self.read(UNITS[4]))
        self.assertTrue(self.config()['timer'])
        self.assertEqual(self.run_main('self-check')[0], 0)
        with open(self.path(APT), 'a') as f:                             # changed by hand: self-check says so
            f.write('Unattended-Upgrade::Automatic-Reboot "true";\n')
        self.assertEqual(self.run_main('self-check')[0], 1)
        self.assertEqual(self.install()[0], 0)                           # run again: put right
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
                if os.path.exists(self.path(APT)) or os.path.exists(self.path(UNITS[0])):
                    current = os.readlink(self.path('/usr/local/libexec/update-policy/current'))
                    self.assertTrue(os.path.isfile(self.path(current + '/bin/update-policy')))
                    self.assertTrue(os.path.islink(self.path('/usr/local/bin/update-policy')))
                code, _, err = self.install('--timer')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.there(), [True] * len(FILES))
                self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_units_are_on_before_apt_stops_restarting_and_off_after_it_starts_again(self):
        """No gap: with --timer the units are enabled and started while unattended-upgrades still restarts by
        itself, and the apt file comes last; --no-timer and uninstall take the apt file away first."""
        self.assertEqual(self.install('--timer')[0], 0)
        self.assertEqual(self.calls, [(('daemon-reload',), False),
                                      (('enable', '--now', 'update-policy.timer', 'update-policy-baseline.timer',
                                        'update-policy-exposure.timer'), False),
                                      (('enable', 'update-policy-boot.service'), False)])
        self.calls.clear()
        code, out, _ = self.install('--no-timer')
        self.assertEqual(code, 0)
        self.assertEqual(self.calls, [(('disable', '--now', 'update-policy.timer', 'update-policy-baseline.timer',
                                        'update-policy-exposure.timer', 'update-policy-boot.service'), False),
                                      (('daemon-reload',), False)])
        self.assertEqual(self.there(), [False] * len(FILES))
        self.assertIn('unattended-upgrades\' own Automatic-Reboot setting applies', out)
        self.assertEqual(self.install()[0], 0)                           # again without the option: still off
        self.assertEqual(self.there(), [False] * len(FILES))
        self.assertEqual(self.install('--timer')[0], 0)
        self.calls.clear()
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual([c[0][0] for c in self.calls], ['disable', 'daemon-reload'])
        self.assertEqual(self.there(), [False] * len(FILES))

    def test_units_that_cannot_be_enabled_leave_apts_own_restarts_on(self):
        self.systemctl_error = {'enable': 'Failed to enable unit: Access denied'}
        code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('systemctl enable --now update-policy.timer update-policy-baseline.timer '
                      'update-policy-exposure.timer failed (Failed to '
                      'enable unit: Access denied)', err)
        self.assertFalse(os.path.exists(self.path(APT)))
        self.systemctl_error = {}
        self.assertEqual(self.install('--timer')[0], 0)                  # fixed: run again finishes it
        self.assertEqual(self.there(), [True] * len(FILES))

    def test_apt_still_restarting_by_itself_fails_the_install_unless_forced(self):
        self.apt_value = 'true'                                           # /etc/apt/apt.conf or a zzz- file wins
        code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('apt still says Unattended-Upgrade::Automatic-Reboot "true"', err)
        self.assertIn('--force', err)
        self.assertNotEqual(self.run_main('self-check')[0], 0)            # not recorded as installed
        self.apt_value = None
        self.assertIn('apt\'s settings cannot be read', self.install('--timer')[2])
        self.apt_value = 'true'
        code, out, err = self.install('--timer', '--force')
        self.assertEqual(code, 0, err)
        self.assertIn('WARNING (--force): apt still says', out)
        self.apt_value = 'false'
        self.assertEqual(self.install('--timer')[0], 0)

    def test_uninstall_leaves_nothing_the_state_too(self):
        self.assertEqual(self.install('--timer', sha=SHA1)[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)                   # upgrade without the option: kept
        self.assertEqual(self.there(), [True] * len(FILES))
        os.makedirs(self.path('/var/cache/private/update-policy-exposure'))
        os.symlink('private/update-policy-exposure', self.path('/var/cache/update-policy-exposure'))
        os.makedirs(self.path('/var/lib/update-policy'))
        with open(self.path('/var/lib/update-policy/state.json'), 'w') as f:
            f.write('{}')
        code, out, _ = self.run_main('uninstall')
        self.assertEqual(code, 0)
        self.assertEqual(self.there(), [False] * len(FILES))
        for gone in ('/usr/local/libexec/update-policy', '/etc/update-policy', '/usr/local/bin/update-policy',
                     '/usr/local/sbin/update-policy-install', '/var/lib/update-policy',
                     '/var/cache/update-policy-exposure', '/var/cache/private/update-policy-exposure'):
            self.assertFalse(os.path.lexists(self.path(gone)), gone)
        self.assertEqual(self.run_main('uninstall')[0], 0)              # again: nothing left, still fine

    def test_files_someone_else_wrote_are_refused_and_kept(self):
        for path in (APT, UNITS[1]):
            with self.subTest(path=path):
                shutil.rmtree(self.root)
                os.makedirs(os.path.dirname(self.path(path)))
                with open(self.path(path), 'w') as f:
                    f.write(FOREIGN)
                code, _, err = self.install('--timer')
                self.assertEqual(code, 1)
                self.assertIn(f'{path} exists and was not written by this kit', err)
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/update-policy')))
                self.assertEqual(self.install()[0], 0)                   # without the option it is left alone
                self.assertEqual(self.run_main('uninstall')[0], 0)
                self.assertEqual(self.read(path), FOREIGN)
        os.unlink(self.path(UNITS[1]))
        with open(self.path('/elsewhere'), 'w') as f:                    # a link is never ours, even to a file
            f.write('# update-policy kit: written by update-policy-install.\n')     # that says so
        os.symlink(self.path('/elsewhere'), self.path(UNITS[1]))
        self.assertEqual(self.install('--timer')[0], 1)

    def test_a_file_someone_writes_during_the_install_is_refused_and_kept(self):
        real = self.m.switch_link

        def and_a_foreign_file(path, target):
            real(path, target)
            if not os.path.lexists(self.path(APT)):
                os.makedirs(os.path.dirname(self.path(APT)), exist_ok=True)
                with open(self.path(APT), 'w') as f:
                    f.write(FOREIGN)
        with mock.patch.object(self.m, 'switch_link', and_a_foreign_file):
            code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        self.assertEqual(self.read(APT), FOREIGN)


MUTATIONS = {
    'files: checked again before they are written': (
        'installer', '            if os.path.lexists(self.p(path)) and not self.ours_file(path):\n'
        '                raise', '            if False:\n                raise',
        'test_a_file_someone_writes_during_the_install_is_refused_and_kept'),
    'files: a foreign one is refused before anything is written': (
        'installer', '            if foreign:', '            if False:',
        'test_files_someone_else_wrote_are_refused_and_kept'),
    'files: a link is never ours': (
        'installer', '        if os.path.islink(real):\n            return False\n', '',
        'test_files_someone_else_wrote_are_refused_and_kept'),
    'no gap: units on before the apt file': (
        'installer', "        for path, rel in self.files()[:-1]:\n            self.write_from(path, os.path.join(dest, rel))\n",
        "        for path, rel in self.files():\n            self.write_from(path, os.path.join(dest, rel))\n",
        'test_the_units_are_on_before_apt_stops_restarting_and_off_after_it_starts_again'),
    'no gap: units that cannot be enabled stop the install': (
        'installer', "            if err:\n                raise self.Refused(f'systemctl {",
        "            if False:\n                raise self.Refused(f'systemctl {",
        'test_units_that_cannot_be_enabled_leave_apts_own_restarts_on'),
    'apt still restarting fails the install': (
        'installer', "            if not plan['force']:", "            if False:",
        'test_apt_still_restarting_by_itself_fails_the_install_unless_forced'),
    'timer: kept by a run without the option': (
        'installer', "bool(self.read_config().get('timer'))", 'False',
        'test_uninstall_leaves_nothing_the_state_too'),
}


class RedCheck(unittest.TestCase):
    BASE = Install

    def test_each_guard_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
