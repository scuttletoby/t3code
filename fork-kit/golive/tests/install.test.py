"""t3-golive-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): an old hand copy of t3-golive kept and put back, and no uninstall from under a golive run. What every kit's
installer does (copy, switch, links, record, self-check, stops half-way, uninstall) and the shared bootstrap are
tested once for every kit in fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck
breaks each guard in a copy of the installer (or of kit_install.py) and shows its test then fails.

  python3 -I -B fork-kit/golive/tests/install.test.py
"""
import fcntl, json, os, shutil, stat, sys, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
COMMAND = '/usr/local/bin/t3-golive'
HAND_COPY = '#!/usr/bin/python3 -IB\n# t3-golive copied by hand from ci-release\n'
MACHINE = '/etc/fork-kit/machine.json'


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 't3-golive-install')

    def hand_copy(self, text=HAND_COPY):
        os.makedirs(self.path('/usr/local/bin'), exist_ok=True)
        with open(self.path(COMMAND), 'w') as f:
            f.write(text)
        os.chmod(self.path(COMMAND), 0o755)

    def kept(self):
        folder = self.path('/usr/local/libexec/t3-golive/replaced')
        return sorted(os.listdir(folder)) if os.path.isdir(folder) else []

    def machine_file(self, section):
        os.makedirs(self.path('/etc/fork-kit'), exist_ok=True)
        with open(self.path(MACHINE), 'w') as f:
            json.dump({'version': 1, **({'golive': section} if section is not None else {})}, f)
        os.chmod(self.path(MACHINE), 0o644)

    def test_the_self_test_runs_t3_golive_help(self):
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertEqual(os.readlink(self.path(COMMAND)), '/usr/local/libexec/t3-golive/current/bin/t3-golive')
        self.assertEqual(os.readlink(self.path('/usr/local/sbin/t3-golive-install')),
                         '/usr/local/libexec/t3-golive/current/sbin/t3-golive-install')
        self.assertFalse(os.path.exists(self.path('/etc/t3-golive/config.json')))   # no options, no config
        self.assertEqual(self.kept(), [])
        with open(self.path('/etc/t3-golive/installed.tsv')) as f:
            record = f.read()
        self.assertIn(f'/usr/local/libexec/t3-golive/versions/{SHA1}/lib/golive/switch.py\tfile\t', record)

    def test_a_hand_copy_is_kept_replaced_and_put_back(self):
        self.hand_copy()
        code, out, err = self.install()
        self.assertEqual(code, 0, err)
        self.assertTrue(os.path.islink(self.path(COMMAND)))
        (name,) = self.kept()
        self.assertTrue(name.startswith('t3-golive.'))
        self.assertIn(f'the old {COMMAND}', out)
        self.assertIn('uninstall puts it back', out)
        kept = self.path(f'/usr/local/libexec/t3-golive/replaced/{name}')
        with open(kept) as f:
            self.assertEqual(f.read(), HAND_COPY)
        self.assertEqual(stat.S_IMODE(os.stat(kept).st_mode), 0o755)
        self.assertEqual(self.run_main('self-check')[0], 0)
        with open(kept, 'a') as f:                                  # the kept copy is in the record
            f.write('# changed\n')
        code, out, _ = self.run_main('self-check')
        self.assertEqual(code, 1)
        self.assertIn(f'/usr/local/libexec/t3-golive/replaced/{name}: differs', out)
        with open(kept, 'w') as f:
            f.write(HAND_COPY)
        before = self.snapshot()                                    # run again: nothing changes
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.install(sha=SHA2)[0], 0)              # an upgrade keeps it
        self.assertEqual(self.kept(), [name])
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertIn(f'put the old {COMMAND} back', out)
        self.assertFalse(os.path.islink(self.path(COMMAND)))
        with open(self.path(COMMAND)) as f:
            self.assertEqual(f.read(), HAND_COPY)
        self.assertEqual(stat.S_IMODE(os.stat(self.path(COMMAND)).st_mode), 0o755)
        for gone in ('/usr/local/libexec/t3-golive', '/etc/t3-golive', '/usr/local/sbin/t3-golive-install'):
            self.assertFalse(os.path.lexists(self.path(gone)), gone)

    def test_a_hand_copy_with_every_stop_half_way(self):
        """The command is never missing: the hand copy stays until the kit's link replaces it in one step, and a
        run after any stop keeps exactly one copy of it."""
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_copy()
                code, _, _ = self.install(stop_after=step)
                self.assertEqual(code, f'stopped after {step} (test)')
                self.assertTrue(os.path.isfile(self.path(COMMAND)))
                if not os.path.islink(self.path(COMMAND)):          # still the hand copy, untouched
                    with open(self.path(COMMAND)) as f:
                        self.assertEqual(f.read(), HAND_COPY)
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.kept()), 1)
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertEqual(self.run_main('uninstall')[0], 0)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), HAND_COPY)

    def test_an_uninstall_puts_back_the_newest_hand_copy(self):
        older = '#!/bin/sh\n# an older hand copy\n'
        self.hand_copy(older)
        self.assertEqual(self.install()[0], 0)
        os.unlink(self.path(COMMAND))                               # someone copied another one by hand later
        self.hand_copy()
        self.assertEqual(self.install()[0], 0)
        folder = self.path('/usr/local/libexec/t3-golive/replaced')
        self.assertEqual(len(self.kept()), 2)
        for name in self.kept():                                    # the older one is the older file
            with open(os.path.join(folder, name)) as f:
                when = 1 if f.read() == older else 2
            os.utime(os.path.join(folder, name), (when, when))
        self.assertEqual(self.run_main('uninstall')[0], 0)
        with open(self.path(COMMAND)) as f:
            self.assertEqual(f.read(), HAND_COPY)

    def test_a_hand_copy_that_is_a_link_or_folder_is_refused(self):
        for kind in ('link', 'folder'):
            with self.subTest(kind=kind):
                shutil.rmtree(self.root)
                os.makedirs(self.path('/usr/local/bin'))
                if kind == 'link':
                    os.symlink('/opt/t3-golive/bin/t3-golive', self.path(COMMAND))
                else:
                    os.makedirs(self.path(COMMAND))
                code, _, err = self.install()
                self.assertEqual(code, 1)
                self.assertIn("is not the kit's link (a link elsewhere, or a folder): remove it by hand", err)
                self.assertFalse(os.path.exists(self.path('/usr/local/libexec/t3-golive')))

    def test_uninstall_is_refused_while_a_golive_run_holds_its_lock(self):
        state = '/var/lib/golive-state'
        os.makedirs(self.path(state))
        self.machine_file({'stateDir': state, 'home': '/srv/t3'})
        self.assertEqual(self.install()[0], 0)
        self.assertEqual(self.run_main('uninstall')[0], 0)          # no lock file: no run ever here
        self.assertEqual(self.install()[0], 0)
        fd = os.open(self.path(f'{state}/lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn(f'a golive run holds {state}/lock', err)
        self.assertTrue(os.path.isdir(self.version()))
        self.assertTrue(os.path.islink(self.path(COMMAND)))
        self.assertEqual(self.install(sha=SHA2)[0], 0)              # an install under a run is fine: old folder kept
        self.assertTrue(os.path.isdir(self.version()))
        fcntl.flock(fd, fcntl.LOCK_UN)
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertFalse(os.path.exists(self.path('/usr/local/libexec/t3-golive')))

    def test_uninstall_reads_the_machine_file_or_refuses(self):
        for section in (None, {}):                                  # no golive section, no stateDir: no run here
            with self.subTest(section=section):
                self.machine_file(section)
                self.assertEqual(self.install()[0], 0)
                self.assertEqual(self.run_main('uninstall')[0], 0)
        self.assertEqual(self.install()[0], 0)
        with open(self.path(MACHINE), 'w') as f:
            f.write('{not json')
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('uninstall reads golive.stateDir', err)
        self.assertTrue(os.path.isdir(self.version()))


MUTATIONS = {
    'hand copy: kept before the link replaces it': (
        'engine', '            if path in self.KEEP_OLD:\n                self.keep_old(path)\n', '',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: a plain file is not refused': (
        'engine', '            if path in self.KEEP_OLD and self.plain_file(real):\n                continue', '',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: put back by uninstall': (
        'engine', "            if not os.path.lexists(real):\n                tmp = real + '.restore'",
        "            if False:\n                tmp = real + '.restore'", 'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: the newest one is put back': (
        'engine', '        for path, src in reversed(kept):', '        for path, src in kept:',
        'test_an_uninstall_puts_back_the_newest_hand_copy'),
    'hand copy: in the install record': (
        'engine', '        for _, kept in self.replaced():\n', '        for _, kept in []:\n',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: only a plain file is kept, never a link': (
        'engine', '        return os.path.isfile(real) and not os.path.islink(real)', '        return os.path.lexists(real)',
        'test_a_hand_copy_that_is_a_link_or_folder_is_refused'),
    'uninstall: refused under a golive run': (
        'installer', '                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n',
        '                pass\n', 'test_uninstall_is_refused_while_a_golive_run_holds_its_lock'),
    'uninstall: a machine file it cannot read refuses': (
        'installer', '        except machine_file.MachineFileError as e:\n            raise',
        '        except machine_file.MachineFileError as e:\n            return None\n            raise',
        'test_uninstall_reads_the_machine_file_or_refuses'),
}


class RedCheck(unittest.TestCase):
    BASE = Install

    def test_each_guard_has_a_test_that_fails_without_it(self):
        fx.red_check(self, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
