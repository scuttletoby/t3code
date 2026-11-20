"""t3-golive-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): an old hand copy of t3-golive kept and put back (the last one kept), the first host's hand install in the
kit's own layout taken over, and no uninstall from under a golive run or one that needs settling. What every kit's
installer does (copy, switch, links, record, self-check, stops half-way, uninstall) and the shared bootstrap are
tested once for every kit in fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck
breaks each guard in a copy of the installer (or of kit_install.py) and shows its test then fails.

  python3 -I -B fork-kit/golive/tests/install.test.py
"""
import fcntl, hashlib, json, os, shutil, stat, sys, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(KIT), 'common', 'tests'))
import install_fixture as fx  # noqa: E402

SHA1, SHA2 = fx.SHA1, fx.SHA2
COMMAND = '/usr/local/bin/t3-golive'
HAND_COPY = '#!/usr/bin/python3 -IB\n# t3-golive copied by hand from ci-release\n'
MACHINE = '/etc/fork-kit/machine.json'
LIBEXEC = '/usr/local/libexec/t3-golive'
LINK = f'{LIBEXEC}/current/bin/t3-golive'
STATE = '/var/lib/golive-state'


class Install(fx.FakeHost):
    KIT_DIR, INSTALLER = KIT, os.path.join(KIT, 'sbin', 't3-golive-install')

    def hand_copy(self, text=HAND_COPY):
        os.makedirs(self.path('/usr/local/bin'), exist_ok=True)
        with open(self.path(COMMAND), 'w') as f:
            f.write(text)
        os.chmod(self.path(COMMAND), 0o755)

    def kept(self):
        folder = self.path('/usr/local/libexec/t3-golive/replaced')
        return sorted(n for n in os.listdir(folder) if n != 'order') if os.path.isdir(folder) else []

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
                self.assertTrue(self.is_file(COMMAND))              # links followed in the fake host only
                if os.path.islink(self.path(COMMAND)):
                    self.assertEqual(os.readlink(self.path(COMMAND)), LINK)
                else:                                               # still the hand copy, untouched
                    with open(self.path(COMMAND)) as f:
                        self.assertEqual(f.read(), HAND_COPY)
                code, _, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(len(self.kept()), 1)
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertEqual(self.run_main('uninstall')[0], 0)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), HAND_COPY)

    def test_an_uninstall_puts_back_the_hand_copy_kept_last(self):
        """By the order they were kept, not by a file time: a copy put in place with `cp -p` (or from a tarball)
        carries an older time than the one it replaced. A copy kept again later is the last one again."""
        older, newer = '#!/bin/sh\n# an older hand copy\n', HAND_COPY
        for order in ((older, newer), (older, newer, older)):
            with self.subTest(order=len(order)):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                for n, text in enumerate(order):
                    if n:
                        os.unlink(self.path(COMMAND))               # someone copied one by hand again later
                    self.hand_copy(text)
                    when = 2_000_000_000 - n * 1000                 # each later copy carries an older time
                    os.utime(self.path(COMMAND), (when, when))
                    self.assertEqual(self.install()[0], 0)
                folder = self.path(f'{LIBEXEC}/replaced')
                self.assertEqual(len(self.kept()), 2)
                for name in self.kept():                            # the one kept last has the oldest time
                    with open(os.path.join(folder, name)) as f:
                        when = 1 if f.read() == order[-1] else 2
                    os.utime(os.path.join(folder, name), (when, when))
                code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), order[-1])

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

    def journal(self, *runs):
        """Writes runs into <stateDir>/journal.jsonl with golive's own journal writer: (command, steps started,
        end result or None for a kill, settled)."""
        J = self.mod.journal
        with open(self.path(f'{STATE}/lock'), 'a'):                 # as a run leaves it
            pass
        for command, steps, end, settled in runs:
            r = J.Run(self.path(STATE), command)
            r._append('begin', command=command, pid=4321)
            for step in steps:
                r._append('step', step=step, phase='started')
            if end:
                r._append('end', result=end, detail='stopped (test)')
            if settled:
                r._append('settle', detail='put right (test)')

    def golive_state(self):
        shutil.rmtree(self.root)
        os.makedirs(self.path(STATE))
        self.machine_file({'stateDir': STATE, 'home': '/srv/t3'})
        self.assertEqual(self.install()[0], 0)

    def test_uninstall_is_refused_while_a_run_needs_settling(self):
        """The run has ended (no lock held), but what it left needs this kit's code: T3 stopped, the holder, the
        switch guard's timer. Settled, refused, scratch and changed-nothing runs do not hold it."""
        refused = {
            'a killed switch': [('switch', ['stop-t3'], None, False)],
            'a failed W1': [('back', ['w1-stop'], 'failed', False)],
            'held T3 after a failed switch': [('switch', ['stop-t3', 'w1'], 'failed', False)],
            'settled, then a new run killed': [('switch', ['stop-t3'], 'failed', True),
                                               ('switch', ['stop-t3'], None, False)],
        }
        for what, runs in refused.items():
            with self.subTest(what=what):
                self.golive_state()
                self.journal(*runs)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn('a golive run needs settling', err)
                self.assertIn('`t3-golive settle`', err)
                self.assertTrue(os.path.isdir(self.version()))
                self.assertTrue(os.path.islink(self.path(COMMAND)))
        fine = {
            'no journal': [],
            'an ok run': [('switch', ['stop-t3'], 'ok', False)],
            'a settled failure': [('switch', ['stop-t3'], 'failed', True)],
            'a refusal': [('switch', ['stop-t3'], 'refused', False)],
            'killed before its first step': [('switch', [], None, False)],
        }
        for what, runs in fine.items():
            with self.subTest(what=what):
                self.golive_state()
                self.journal(*runs)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertFalse(os.path.lexists(self.path(LIBEXEC)))
        self.golive_state()                                         # a journal it cannot trust refuses
        with open(self.path(f'{STATE}/journal.jsonl'), 'w') as f:
            f.write('{"broken": 1}\n{"v": 1}\n')
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1)
        self.assertIn('cannot see whether a run needs settling', err)
        self.assertTrue(os.path.isdir(self.version()))

    def test_a_special_file_in_the_state_folder_refuses_and_never_blocks(self):
        """The state folder is the T3 user's: a FIFO (or a link) as the lock or the journal would make root's
        uninstall wait forever with the install lock held."""
        for name in ('lock', 'journal.jsonl'):
            for kind in ('fifo', 'link'):
                with self.subTest(name=name, kind=kind):
                    self.golive_state()
                    if kind == 'fifo':
                        os.mkfifo(self.path(f'{STATE}/{name}'))
                    else:                                           # to a plain file: it would pass if followed
                        with open(self.path('/elsewhere'), 'w'):
                            pass
                        os.symlink(self.path('/elsewhere'), self.path(f'{STATE}/{name}'))
                    with self.no_hang():
                        code, _, err = self.run_main('uninstall')
                    self.assertEqual(code, 1, err)
                    self.assertIn(f'{STATE}/{name}', err)
                    self.assertTrue(os.path.isdir(self.version()))
                    with self.no_hang():                            # the install lock was let go
                        self.assertEqual(self.install()[0], 0)

    def hand_layout(self, sha, manifest):
        """The first host's hand install, in the kit's own layout: /usr/local/bin/t3-golive is exactly the kit's
        link, `current` a relative link to versions/<sha>, that folder a copy of the kit with (or without) a
        manifest of its own and a note, and no /etc/t3-golive and no installer link."""
        folder = self.path(f'{LIBEXEC}/versions/{sha}')
        shutil.copytree(KIT, folder, ignore=shutil.ignore_patterns('tests', '__pycache__'))
        os.makedirs(os.path.join(folder, 'common'))
        shutil.copy(os.path.join(fx.COMMON, 'machine_file.py'), os.path.join(folder, 'common'))
        with open(os.path.join(folder, 'HAND-INSTALLED.txt'), 'w') as f:
            f.write('copied by hand from ci-release\n')
        if manifest:
            lines = []
            for dirpath, _, filenames in os.walk(folder):
                for name in filenames:
                    with open(os.path.join(dirpath, name), 'rb') as f:
                        lines.append(f'{hashlib.sha256(f.read()).hexdigest()}  '
                                     f'{os.path.relpath(os.path.join(dirpath, name), folder)}')
            with open(os.path.join(folder, 'MANIFEST.sha256'), 'w') as f:
                f.write('\n'.join(sorted(lines)) + '\n')
        os.symlink(f'versions/{sha}', self.path(f'{LIBEXEC}/current'))
        os.makedirs(self.path('/usr/local/bin'), exist_ok=True)
        os.symlink(LINK, self.path(COMMAND))

    def test_a_hand_install_in_the_kit_layout_is_taken_over(self):
        """The first host's real state (#508). The install switches `current`, keeps the hand version folder
        (not in the record), and the command is never missing; run again it changes nothing; the same release
        as the hand folder makes that folder again; uninstall removes it all like the kit's own install: there is
        no plain hand copy to put back."""
        hand = 'c' * 40
        for manifest in (True, False):
            with self.subTest(manifest=manifest):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_layout(hand, manifest)
                self.assertTrue(self.is_file(COMMAND))
                code, out, err = self.install()
                self.assertEqual(code, 0, err)
                self.assertEqual(self.current(), f'{LIBEXEC}/versions/{SHA1}')
                self.assertEqual(os.readlink(self.path(COMMAND)), LINK)
                self.assertEqual(self.resolve(COMMAND), self.version(rel='bin/t3-golive'))
                self.assertEqual(self.kept(), [])
                self.assertTrue(os.path.isfile(self.version(hand, 'HAND-INSTALLED.txt')))
                with open(self.path('/etc/t3-golive/installed.tsv')) as f:
                    self.assertNotIn(hand, f.read())
                self.assertEqual(self.run_main('self-check')[0], 0)
                before = self.snapshot()
                self.assertEqual(self.install()[0], 0)
                self.assertEqual(self.snapshot(), before)
                code, out, err = self.install(sha=hand)
                self.assertEqual(code, 0, err)
                self.assertIn(f'versions/{hand} is incomplete or changed: made again', out)
                self.assertFalse(os.path.exists(self.version(hand, 'HAND-INSTALLED.txt')))
                self.assertEqual(self.run_main('self-check')[0], 0)
                code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertNotIn('put the old', out)
                for gone in (LIBEXEC, '/etc/t3-golive', COMMAND, '/usr/local/sbin/t3-golive-install'):
                    self.assertFalse(os.path.lexists(self.path(gone)), gone)

    def test_a_hand_install_in_the_kit_layout_with_every_stop_half_way(self):
        hand = 'c' * 40
        for step in self.m.STEPS:
            with self.subTest(step=step):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_layout(hand, manifest=True)
                self.assertEqual(self.install(stop_after=step)[0], f'stopped after {step} (test)')
                self.assertTrue(self.is_file(COMMAND))
                self.assertEqual(self.install()[0], 0)
                self.assertEqual(self.run_main('self-check')[0], 0)
                self.assertTrue(os.path.isfile(self.version(hand, 'HAND-INSTALLED.txt')))


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
    'hand copy: the last one kept is put back': (
        'engine', '        for path, src in reversed(kept):', '        for path, src in kept:',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: by the order kept, not by a file time': (
        'engine', '        return [found[n] for n in order] + [found[n] for n in sorted(set(found) - set(order))]',
        '        return sorted(found.values(), key=lambda pk: os.path.getmtime(pk[1]))',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: one kept again is the last one again': (
        'engine', '        order = [n for n in self.kept_order() if n != name] + [name]',
        '        order = self.kept_order() + ([] if name in self.kept_order() else [name])',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: in the install record': (
        'engine', '        for _, kept in self.replaced():\n', '        for _, kept in []:\n',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: only a plain file is kept, never a link': (
        'engine', '        return os.path.isfile(real) and not os.path.islink(real)', '        return os.path.lexists(real)',
        'test_a_hand_copy_that_is_a_link_or_folder_is_refused'),
    'uninstall: refused under a golive run': (
        'installer', '                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n',
        '                pass\n', 'test_uninstall_is_refused_while_a_golive_run_holds_its_lock'),
    'uninstall: refused while a run needs settling': (
        'installer', '            self.refuse_unsettled(state)\n', '',
        'test_uninstall_is_refused_while_a_run_needs_settling'),
    'uninstall: a journal it cannot trust refuses': (
        'installer', '            except journal.Damaged as e:        # Newer too\n                raise',
        '            except journal.Damaged as e:        # Newer too\n                return\n                raise',
        'test_uninstall_is_refused_while_a_run_needs_settling'),
    'uninstall: a FIFO in the state folder is never waited on': (
        'installer', 'fd = os.open(self.p(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)',
        'fd = os.open(self.p(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)',
        'test_a_special_file_in_the_state_folder_refuses_and_never_blocks'),
    'uninstall: only a plain file in the state folder': (
        'installer', '        if not stat.S_ISREG(os.fstat(fd).st_mode):\n            os.close(fd)',
        '        if False:\n            os.close(fd)', 'test_a_special_file_in_the_state_folder_refuses_and_never_blocks'),
    'uninstall: no link followed in the state folder': (
        'installer', 'fd = os.open(self.p(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK',
        'fd = os.open(self.p(path), os.O_RDONLY | os.O_NONBLOCK',
        'test_a_special_file_in_the_state_folder_refuses_and_never_blocks'),
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
