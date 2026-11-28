"""claude-login-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current
user as root): the config and the timer units. What every kit's installer does (copy, switch, links, record,
self-check, stops half-way, uninstall) and the shared bootstrap are tested once for every kit in
fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network.

  python3 -I -B fork-kit/claude-login/tests/install.test.py
"""
import json, os, shutil, sys, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
UNITS = ('/etc/systemd/user/claude-login-check.service', '/etc/systemd/user/claude-login-check.timer')


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 'claude-login-install')

    def units(self):
        return [os.path.isfile(self.path(u)) for u in UNITS]

    def test_fresh_install_has_no_timer_and_checks_itself(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        with open(self.path('/etc/claude-login/config.json')) as f:
            self.assertEqual(json.load(f), {'version': 1, 'timer': False})
        self.assertEqual(self.units(), [False, False])
        self.assertTrue(os.path.islink(self.path('/usr/local/bin/claude-login')))
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_the_timer_runs_the_check_hourly_from_the_release_in_use(self):
        code, out, err = self.install('--timer')
        self.assertEqual(code, 0, err)
        self.assertIn('systemctl --user enable --now claude-login-check.timer', out)
        with open(self.path(UNITS[0])) as f:
            text = f.read()
        self.assertIn('ExecStart=/usr/bin/python3 -I -B /usr/local/libexec/claude-login/current/bin/claude-login '
                      'check\n', text)
        self.assertIn('TimeoutStartSec=10min\n', text)
        self.assertIn('UMask=0077\n', text)
        with open(self.path(UNITS[1])) as f:
            self.assertIn('OnCalendar=hourly\n', f.read())
        self.assertEqual(self.run_main('self-check')[0], 0)

    def test_every_stop_half_way_then_run_again(self):
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                code, _, _ = self.install('--timer', stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                code, _, err = self.install('--timer')
                self.assertEqual(code, 0, err)
                self.assertEqual(self.units(), [True, True])

    def test_timer_kept_by_an_upgrade_removed_by_no_timer_and_uninstall(self):
        self.assertEqual(self.install('--timer')[0], 0)
        self.assertEqual(self.install(sha=SHA2)[0], 0)
        self.assertEqual(self.units(), [True, True])
        self.assertEqual(self.install('--no-timer', sha=SHA2)[0], 0)
        self.assertEqual(self.units(), [False, False])
        self.assertEqual(self.install('--timer', sha=SHA2)[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        for gone in UNITS + ('/usr/local/libexec/claude-login', '/etc/claude-login', '/usr/local/bin/claude-login'):
            self.assertFalse(os.path.lexists(self.path(gone)), gone)

    def test_units_someone_else_wrote_are_refused_and_kept(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path(UNITS[1]), 'w') as f:
            f.write('[Timer]\nOnCalendar=daily\n')
        code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        with open(self.path(UNITS[1])) as f:
            self.assertEqual(f.read(), '[Timer]\nOnCalendar=daily\n')

    def test_a_unit_someone_else_wrote_that_is_not_utf8_is_refused_not_a_traceback(self):
        os.makedirs(self.path('/etc/systemd/user'))
        with open(self.path(UNITS[0]), 'wb') as f:
            f.write(b'# \xff\xfe not text\n[Service]\n')
        code, _, err = self.install('--timer')
        self.assertEqual(code, 1)
        self.assertIn('was not written by this kit', err)
        self.assertNotIn('Traceback', err)


if __name__ == '__main__':
    unittest.main()
