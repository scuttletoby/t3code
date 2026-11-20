"""t3-golive-install's own part on a fake host in a temporary folder (every fixed path under PREFIX, the current user
as root): an old hand copy of t3-golive kept and put back (the last one kept), the first host's hand install in the
kit's own layout taken over, and no uninstall from under a golive run or one that needs settling. What every kit's
installer does (copy, switch, links, record, self-check, stops half-way, uninstall) and the shared bootstrap are
tested once for every kit in fork-kit/common/tests/kit_install.test.py. No root, no systemd, no network. RedCheck
breaks each guard in a copy of the installer (or of kit_install.py) and shows its test then fails.

  python3 -I -B fork-kit/golive/tests/install.test.py
"""
import contextlib, fcntl, hashlib, json, os, pwd, select, shutil, signal, stat, sys, time, unittest
from unittest import mock

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

    def test_the_hand_copy_comes_back_with_its_own_mode_and_owner(self):
        """Not a guessed 0755 or 0644, root-owned: the mode (a set-id bit too) and owner it had when it was kept. An
        order file from before they were kept (names only) still puts it back, with the kept copy's mode."""
        for mode in (0o750, 0o700, 0o644, 0o4755):
            with self.subTest(mode=oct(mode)):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_copy()
                os.chmod(self.path(COMMAND), mode)
                self.assertEqual(self.install()[0], 0)
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertEqual(stat.S_IMODE(os.lstat(self.path(COMMAND)).st_mode), mode)
                self.assertEqual(os.lstat(self.path(COMMAND)).st_uid, os.geteuid())
        shutil.rmtree(self.root)                                    # another owner: chown, then chmod
        os.makedirs(self.root)
        self.hand_copy()
        os.chmod(self.path(COMMAND), 0o750)
        self.assertEqual(self.install()[0], 0)
        order = self.path(f'{LIBEXEC}/replaced/order')
        with open(order) as f:
            (row,) = f.read().splitlines()
        name, mode, _, _ = row.split('\t')
        self.assertEqual(mode, '750')
        with open(order, 'w') as f:
            f.write(f'{name}\t4750\t4242\t4343\n')
        calls, chmod, stage = [], os.chmod, self.path(f'{COMMAND}.restore')

        def record_chmod(path, mode, *a, **k):
            calls.append(('chmod', path, mode))
            chmod(path, mode, *a, **k)

        def record_chown(path, uid, gid):
            """Between chown and chmod the old owner owns the copy (with no set-gid bit yet): only root may reach
            it then, in a folder of root's that no one else can enter, not beside the command."""
            st = os.lstat(os.path.dirname(path))
            calls.append(('chown', path, uid, gid, os.path.dirname(path), stat.S_IMODE(st.st_mode), st.st_uid))
        with mock.patch.object(os, 'chown', record_chown), mock.patch.object(os, 'chmod', record_chmod):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        calls = [c for c in calls if c[1].startswith(stage)]
        self.assertEqual(calls, [('chown', f'{stage}/t3-golive', 4242, 4343, stage, 0o700, os.geteuid()),
                                 ('chmod', f'{stage}/t3-golive', 0o4750)])
        self.assertFalse(os.path.lexists(stage))
        self.assertEqual(stat.S_IMODE(os.lstat(self.path(COMMAND)).st_mode), 0o4750)
        for old_mode, back in ((0o750, 0o755), (0o640, 0o644)):     # an order from before: names only
            with self.subTest(old_order=oct(old_mode)):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_copy()
                os.chmod(self.path(COMMAND), old_mode)
                self.assertEqual(self.install()[0], 0)
                with open(order, 'w') as f:
                    f.write(f'{self.kept()[0]}\n')
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), HAND_COPY)
                self.assertEqual(stat.S_IMODE(os.lstat(self.path(COMMAND)).st_mode), back)

    def test_uninstall_stopped_anywhere_never_leaves_the_command_missing(self):
        """The hand copy goes back over the link in one step, so `t3-golive` is there after every change uninstall
        makes; stopped after any one of them, a run again puts back exactly the hand copy (its mode too) and
        leaves no <path>.restore."""
        def there():
            self.assertTrue(self.is_file(COMMAND), 't3-golive is missing')
        for n in range(1, 500):             # no subTest: a failure must end the loop, not go on to n + 1
            with self.stop_context(n):
                shutil.rmtree(self.root)
                os.makedirs(self.root)
                self.hand_copy()
                os.chmod(self.path(COMMAND), 0o750)
                self.assertEqual(self.install()[0], 0)
                code, _, err, stopped = self.uninstall_stopped_after(n, check=there)
                there()
                if stopped:
                    self.assertEqual(code, 'stopped (test)')
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), HAND_COPY)
                self.assertEqual(stat.S_IMODE(os.lstat(self.path(COMMAND)).st_mode), 0o750)
                self.assertEqual(os.listdir(self.path('/usr/local/bin')), ['t3-golive'])
                if not stopped:
                    break
        else:
            self.fail('uninstall made more than 500 changes')
        self.assertGreater(n, 5)

    def test_a_leftover_restore_file_is_removed_by_install_and_uninstall(self):
        """<path>.restore, left by an uninstall stopped while putting the hand copy back (root's folder, with a half
        copy in it or none), goes on the next run; so does a plain file of that name."""
        for cmd in ('install', 'uninstall'):
            for kind in ('folder', 'empty folder', 'file'):
                with self.subTest(cmd=cmd, kind=kind):
                    shutil.rmtree(self.root)
                    os.makedirs(self.root)
                    if cmd == 'uninstall':
                        self.assertEqual(self.install()[0], 0)
                    os.makedirs(self.path('/usr/local/bin'), exist_ok=True)
                    leftover = self.path(f'{COMMAND}.restore')
                    if kind == 'file':
                        with open(leftover, 'w') as f:
                            f.write('half\n')
                    else:
                        os.mkdir(leftover, 0o700)
                        if kind == 'folder':
                            with open(os.path.join(leftover, 't3-golive'), 'w') as f:
                                f.write('half\n')
                    code, _, err = self.install() if cmd == 'install' else self.run_main('uninstall')
                    self.assertEqual(code, 0, err)
                    self.assertFalse(os.path.lexists(leftover))

    def test_the_hand_copy_comes_back_where_the_command_is_missing(self):
        """No link at /usr/local/bin/t3-golive (an uninstall by an installer from before this one, stopped between
        removing the link and putting the copy back, or an admin who removed it): uninstall still puts the kept
        hand copy back, with its mode, and a run again leaves it there."""
        self.hand_copy()
        os.chmod(self.path(COMMAND), 0o750)
        self.assertEqual(self.install()[0], 0)
        os.unlink(self.path(COMMAND))
        for run in (1, 2):
            with self.subTest(run=run):
                code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with open(self.path(COMMAND)) as f:
                    self.assertEqual(f.read(), HAND_COPY)
                self.assertEqual(stat.S_IMODE(os.lstat(self.path(COMMAND)).st_mode), 0o750)
                self.assertEqual(os.listdir(self.path('/usr/local/bin')), ['t3-golive'])
                self.assertFalse(os.path.lexists(self.path(LIBEXEC)))

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

    def test_uninstall_makes_the_lock_a_first_run_would_take(self):
        """No golive run here yet, so no <stateDir>/lock: uninstall makes it as golive's first run would (the file
        0600, a missing folder 0700), as the state folder's owner, and holds it, so a first run that starts during
        the uninstall finds it held. In a folder of root's (no golive run as a user makes it there) nothing is
        made."""
        J = self.mod.journal
        for folder in ('there', 'missing'):
            with self.subTest(folder=folder):
                shutil.rmtree(self.root)
                os.makedirs(self.path(STATE) if folder == 'there' else self.path(os.path.dirname(STATE)))
                self.machine_file({'stateDir': STATE, 'home': '/srv/t3'})
                self.assertEqual(self.install()[0], 0)
                self.m.SYSTEM_UID = -1                              # the folder is a user's, whoever runs this
                tried, in_use = [], self.m.in_use

                def first_run_meanwhile():
                    with self.assertRaises(J.Busy):
                        with J.lock(self.path(STATE), 'switch', exclusive=True):
                            pass
                    tried.append(1)
                    return in_use()
                with mock.patch.object(self.m, 'in_use', first_run_meanwhile):
                    code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertEqual(tried, [1])
                self.assertIn(f'made {STATE}/lock', out)
                st = os.lstat(self.path(f'{STATE}/lock'))
                self.assertTrue(stat.S_ISREG(st.st_mode))
                self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid), (0o600, os.geteuid()))
                if folder == 'missing':
                    self.assertEqual(stat.S_IMODE(os.lstat(self.path(STATE)).st_mode), 0o700)
        shutil.rmtree(self.root)
        os.makedirs(self.path(os.path.dirname(STATE)))
        self.machine_file({'stateDir': STATE, 'home': '/srv/t3'})
        self.assertEqual(self.install()[0], 0)
        self.m.SYSTEM_UID = os.geteuid()                            # root's folder
        code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.path(STATE)))

    def lock_host(self, *folders, state=STATE):
        """A fresh fake host with those folders, the machine file naming state, and the kit installed; every file
        here is a user's (SYSTEM_UID matches no one)."""
        shutil.rmtree(self.root)
        for folder in folders:
            os.makedirs(self.path(folder))
        self.machine_file({'stateDir': state, 'home': '/srv/t3'})
        self.assertEqual(self.install()[0], 0)
        self.m.SYSTEM_UID = -1

    def test_the_lock_is_never_made_through_a_link_the_t3_user_could_plant(self):
        """The folders on the way to stateDir are the T3 user's: a link there to another user's folder would make
        root's uninstall run as that folder's owner and make the state folder and the lock in it. Each step is
        opened with no link followed and a link that is not root's refuses, before anything is made or run as
        anyone; a folder swapped for such a link after that walk changes nothing either, as the lock is made in
        the folder the walk opened."""
        other = '/home/other'
        cases = {
            'a folder on the way': ('/var/lib', other, ['/var']),
            'a folder on the way, a relative link': ('/var/lib', '../home/other', ['/var']),
            'the state folder': (STATE, f'{other}/state', ['/var/lib', f'{other}/state']),
            'the state folder, a relative link': (STATE, '../../home/other/state', ['/var/lib', f'{other}/state']),
        }
        for what, (at, target, folders) in cases.items():
            with self.subTest(what=what):
                self.lock_host(other, *folders)
                os.symlink(target, self.path(at))
                before = self.files_under(self.path(other))
                ran, run_as = [], self.m.run_as
                with mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a[:2]), run_as(*a, **k))):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'{at} is a link of uid {os.geteuid()}, who could point it at another user', err)
                self.assertEqual((ran, self.files_under(self.path(other))), ([], before))
                self.assertTrue(os.path.isdir(self.version()))
                os.unlink(self.path(at))                            # the link gone: made as the user, and done
                code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertIn(f'made {STATE}/lock', out)
                self.assertEqual(self.files_under(self.path(other)), before)
        self.lock_host(other, '/var/lib')                           # swapped after the walk, before the child
        ran, run_as = [], self.m.run_as

        def swapped_first(*a, **k):
            os.rename(self.path('/var/lib'), self.path('/var/lib.old'))
            os.symlink(other, self.path('/var/lib'))
            ran.append(a[:2])
            return run_as(*a, **k)
        with mock.patch.object(self.m, 'run_as', swapped_first):
            code, out, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertEqual(self.files_under(self.path(other)), {})
        self.assertTrue(os.path.isfile(self.path('/var/lib.old/golive-state/lock')))
        self.assertEqual(len(ran), 1)

    @contextlib.contextmanager
    def owners(self, folders=(), links=None, uid=None, others=None):
        """While the installer walks to the state folder and makes the lock: os.fstat with those host folders (and
        every link, where links is set) shown as owned by uid (SYSTEM_UID by default: root, a uid no one here
        has), and each folder of others ({folder: uid}) as owned by its uid. Only then: the machine file is read
        and the kit removed as on any other host."""
        fake, real, walking = self.m.SYSTEM_UID if uid is None else uid, os.fstat, []
        ids = {(st.st_dev, st.st_ino): fake for st in (os.lstat(self.path(f)) for f in folders)}
        for f, owner in (others or {}).items():
            st = os.lstat(self.path(f))
            ids[st.st_dev, st.st_ino] = owner

        def fstat(fd):
            st = real(fd)
            if walking and stat.S_ISLNK(st.st_mode) and links:
                return os.stat_result(st[:4] + (fake,) + st[5:])
            if walking and (st.st_dev, st.st_ino) in ids:
                return os.stat_result(st[:4] + (ids[st.st_dev, st.st_ino],) + st[5:])
            return st

        def during(fn):
            def call(*a, **k):
                walking.append(1)
                try:
                    return fn(*a, **k)
                finally:
                    walking.pop()
            return call
        with mock.patch.object(os, 'fstat', fstat), \
                mock.patch.object(self.m, 'real_folder', during(self.m.real_folder)), \
                mock.patch.object(self.m, 'make_lock', during(self.m.make_lock)):
            yield

    def root_host(self, *folders, state=STATE):
        """lock_host, with root a uid no one here has: as on a host, /, and links, are root's (owners)."""
        self.lock_host(*folders, state=state)
        self.m.SYSTEM_UID = os.geteuid() + 4242

    def test_a_link_of_roots_on_the_way_is_gone_along(self):
        """/var -> /data/var, made by root in a folder of root's (an admin's layout, as /home -> /var/home): the
        lock is made in the real folder, as that folder's owner, whatever the link's spelling. A relative link is
        read from its own folder, not from /. Run again, with the lock there now, it is opened the same way."""
        cases = {                           # link: (target, folders, roots, where the lock goes)
            'absolute': ('/var', '/data/var', ['/data/var/lib'], ['/'], '/data/var/lib'),
            'relative': ('/var', 'data/var', ['/data/var/lib'], ['/'], '/data/var/lib'),
            'with .. and .': ('/var', '/data/../data/./var', ['/data/var/lib'], ['/'], '/data/var/lib'),
            'relative, deeper': ('/var/lib', 'real-lib', ['/var/real-lib'], ['/', '/var'], '/var/real-lib'),
        }
        for what, (at, target, folders, roots, real) in cases.items():
            with self.subTest(what=what):
                self.root_host(*folders)
                os.symlink(target, self.path(at))
                with self.owners(roots, links=True):
                    code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertIn(f'made {STATE}/lock (as uid {os.geteuid()})', out)
                st = os.lstat(self.path(f'{real}/golive-state/lock'))
                self.assertEqual((stat.S_ISREG(st.st_mode), stat.S_IMODE(st.st_mode)), (True, 0o600))
                self.assertEqual(stat.S_IMODE(os.lstat(self.path(f'{real}/golive-state')).st_mode), 0o700)
                self.assertEqual(self.install()[0], 0)              # runs again: the lock is there now
                with self.owners(roots, links=True):
                    code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                self.assertNotIn('made', out)
                self.assertFalse(os.path.lexists(self.path(LIBEXEC)))

    def test_a_link_of_roots_is_gone_along_only_from_a_folder_of_roots(self):
        """A link root owns is trusted only where no one else could have put it: in a folder of root's that no one
        else may write in. In a user's folder (moved there from elsewhere, or hard-linked where
        fs.protected_hardlinks is off), or in a folder others may write in, it refuses, before anything is made or
        run as anyone."""
        cases = {                           # where /var is: (folder owned by root?, its mode)
            "a user's folder": (False, 0o755),
            "a folder of root's anyone may write in": (True, 0o777),
            "a sticky folder of root's anyone may write in": (True, 0o1777),
            "a folder of root's its group may write in": (True, 0o775),
        }
        for what, (roots, mode) in cases.items():
            with self.subTest(what=what):
                self.root_host('/var', '/data/lib')
                os.chmod(self.path('/var'), mode)
                os.symlink('/data/lib', self.path('/var/lib'))
                before, ran, run_as = self.files_under(self.path('/data')), [], self.m.run_as
                with self.owners(['/', '/var'] if roots else ['/'], links=True), \
                        mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a), run_as(*a, **k))):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                owner = self.m.SYSTEM_UID if roots else os.geteuid()
                self.assertIn(f"/var/lib is a link of root's in a folder that someone other than root may write in "
                              f'(uid {owner}, mode {mode:o})', err)
                self.assertEqual((ran, self.files_under(self.path('/data'))), ([], before))
                self.assertTrue(os.path.isdir(self.version()))

    def test_the_walk_refuses_what_is_not_a_plain_way_there(self):
        """A file on the way refuses as not a folder; a link it cannot read, a loop of links and "x/.." where x is
        missing refuse too, with a message, never a traceback or a wait."""
        self.lock_host('/var')
        with open(self.path('/var/lib'), 'w'):
            pass
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        self.assertIn('/var/lib cannot be opened as a folder (Not a directory)', err)
        cases = {
            'a link it cannot read': ('data/var', ['/data/var/lib'], 'cannot be read (Permission denied)'),
            'a loop of links': ('/var', [], 'too many links on the way'),
            'a missing folder before ..': ('nothere/../data/var', ['/data/var/lib'], 'a missing folder before ".."'),
        }
        for what, (target, folders, says) in cases.items():
            with self.subTest(what=what):
                self.root_host(*folders)
                os.symlink(target, self.path('/var'))
                readlink = os.readlink
                cannot = mock.Mock(side_effect=PermissionError(13, 'Permission denied'))
                with self.owners(['/'], links=True), self.no_hang(10), \
                        mock.patch.object(os, 'readlink', cannot if what == 'a link it cannot read' else readlink):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(says, err)
                self.assertTrue(os.path.isdir(self.version()))
                self.assertFalse(os.path.lexists(self.path('/data/var/lib/golive-state')))

    def test_below_a_users_folder_every_folder_is_that_users(self):
        """A folder of another user's (or of root's) below the T3 user's folder may have been moved there by the
        T3 user: the lock would be made as its owner, or opened where they chose. As on a host, /, /var and
        /var/lib are root's and /var/lib/t3 the T3 user's (the first folder that is not root's sets the owner the
        rest must have). Refused, with the lock there or not, before anything is made or run as anyone."""
        state = '/var/lib/t3/golive-state'
        for owner in ('another user', 'root'):
            for lock in (False, True):
                with self.subTest(owner=owner, lock=lock):
                    self.root_host(state, state=state)
                    if lock:
                        with open(self.path(f'{state}/lock'), 'w'):
                            pass
                    uid = os.geteuid() + 1 if owner == 'another user' else self.m.SYSTEM_UID
                    ran, run_as = [], self.m.run_as
                    with self.owners(['/', '/var', '/var/lib'], others={state: uid}), \
                            mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a), run_as(*a, **k))):
                        code, _, err = self.run_main('uninstall')
                    self.assertEqual(code, 1, err)
                    self.assertIn(f'{state} belongs to uid {uid}, inside a folder of uid {os.geteuid()}', err)
                    self.assertEqual(ran, [])
                    self.assertEqual(os.listdir(self.path(state)), ['lock'] if lock else [])
                    self.assertTrue(os.path.isdir(self.version()))
        with self.subTest(owner='the same user, below root\'s folders'):    # the normal host: allowed
            self.root_host(state, state=state)
            with self.owners(['/', '/var', '/var/lib']):
                code, out, err = self.run_main('uninstall')
            self.assertEqual(code, 0, err)
            self.assertIn(f'made {state}/lock (as uid {os.geteuid()})', out)

    def test_a_folder_on_fuse_refuses(self):
        """A FUSE filesystem (a user's own, where /etc/fuse.conf has user_allow_other) can show any owner and any
        link, root's too: no folder on the way may be on one, nor on a filesystem that cannot be told. Refused
        with the lock there or not, before anything is made or run as anyone."""
        kind, where, real = None, '/var/lib', self.m.mount_type

        def mount_type(fd):
            st, at = os.fstat(fd), os.lstat(self.path(where))
            return kind if (st.st_dev, st.st_ino) == (at.st_dev, at.st_ino) else real(fd)
        cases = [(k, '/var/lib', lock) for k in ('fuse', 'fuseblk', 'fuse.sshfs', None) for lock in (False, True)]
        for kind, where, lock in cases + [('fuse', '/', False), (None, '/', True)]:     # / is checked too
            with self.subTest(kind=kind, where=where, lock=lock):
                self.lock_host(STATE)
                if lock:
                    with open(self.path(f'{STATE}/lock'), 'w'):
                        pass
                ran, run_as = [], self.m.run_as
                with mock.patch.object(self.m, 'mount_type', mount_type), \
                        mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a), run_as(*a, **k))):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'{where} is on a filesystem of type {kind or "that cannot be told"}', err)
                self.assertEqual((ran, os.listdir(self.path(STATE))), ([], ['lock'] if lock else []))
                self.assertTrue(os.path.isdir(self.version()))

    def test_the_filesystem_type_comes_from_the_mount_id(self):
        """From /proc/self/fdinfo/<fd> and /proc/self/mountinfo, never from the filesystem itself (a FUSE program
        could make that wait): optional fields or none, a subtype, and an unknown mount (None)."""
        fdinfo = os.path.join(self.host, 'fdinfo')
        os.makedirs(fdinfo)
        self.m.FDINFO, self.m.MOUNTINFO = fdinfo, os.path.join(self.host, 'mountinfo')
        with open(self.m.MOUNTINFO, 'w') as f:
            f.write('22 1 8:1 / / rw,relatime shared:1 - ext4 /dev/sda1 rw\n'
                    '42 22 0:50 / /home/t3/mnt rw,nosuid,nodev,relatime shared:5 master:2 - fuse.sshfs t3@x:/ rw\n'
                    '43 22 0:51 / /- rw - fuseblk /dev/sdb1 rw\n')
        for mount, kind in (('22', 'ext4'), ('42', 'fuse.sshfs'), ('43', 'fuseblk'), ('99', None)):
            with self.subTest(mount=mount):
                with open(os.path.join(fdinfo, '7'), 'w') as f:
                    f.write(f'pos:\t0\nflags:\t012200000\nmnt_id:\t{mount}\nino:\t2\n')
                self.assertEqual(self.m.mount_type(7), kind)
        self.assertIsNone(self.m.mount_type(8))                     # no fdinfo
        self.m.FDINFO, self.m.MOUNTINFO = '/proc/self/fdinfo', '/proc/self/mountinfo'
        fd = os.open(self.root, os.O_PATH)
        self.addCleanup(os.close, fd)
        self.assertTrue(self.m.mount_type(fd))                      # this host's own

    def test_an_existing_lock_and_journal_are_opened_through_the_walk(self):
        """Where the lock is there already (every host where golive has run), it and the journal are opened in the
        folder the walk reached, not by their path: a link of the T3 user's to another user's state folder
        refuses, and that user's lock and journal are not opened."""
        other = '/home/other/state'
        for target in (other, '../../home/other/state'):
            with self.subTest(target=target):
                self.lock_host(other, '/var/lib')
                for name in ('lock', 'journal.jsonl'):
                    with open(self.path(f'{other}/{name}'), 'w'):
                        pass
                os.symlink(target, self.path(STATE))
                opened, real = [], os.open

                def watched(path, *a, **k):
                    opened.append(path)
                    return real(path, *a, **k)
                with mock.patch.object(os, 'open', watched):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'{STATE} is a link of uid {os.geteuid()}', err)
                self.assertFalse([p for p in opened if os.path.basename(str(p)) in ('lock', 'journal.jsonl')])
                self.assertTrue(os.path.isdir(self.version()))

    def test_the_lock_is_made_with_the_user_entrys_group(self):
        """As the folder's owner, with that user's own group from the user entry (not the folder's group, nor
        root's); a uid with no user entry refuses, with nothing made."""
        self.lock_host('/var/lib')
        ran, run_as = [], self.m.run_as
        entry = mock.Mock(pw_gid=4343)
        with mock.patch.object(pwd, 'getpwuid', lambda uid: entry), \
                mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a[:2]), run_as(*a, **k))):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertNotEqual(os.lstat(self.path('/var/lib')).st_gid, 4343)
        self.assertEqual(ran, [(os.geteuid(), 4343)])
        self.lock_host('/var/lib')
        ran = []
        with mock.patch.object(pwd, 'getpwuid', mock.Mock(side_effect=KeyError(os.geteuid()))), \
                mock.patch.object(self.m, 'run_as', lambda *a, **k: (ran.append(a[:2]), run_as(*a, **k))):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        self.assertIn(f'belongs to uid {os.geteuid()}, which has no user entry', err)
        self.assertEqual(ran, [])
        self.assertFalse(os.path.lexists(self.path(STATE)))

    def test_the_folders_made_are_as_golive_makes_them(self):
        """The same modes golive's own first run gives them (journal.lock: os.makedirs, the state folder 0700, the
        folders above it as umask leaves them), and stateDir spelled as golive spells it ("x/.." dropped)."""
        J = self.mod.journal
        for state in ('/var/lib/a/b/golive-state', '/var/lib/nothere/../a/b/golive-state'):
            with self.subTest(state=state):
                self.lock_host('/var/lib', state=state)
                code, out, err = self.run_main('uninstall')
                self.assertEqual(code, 0, err)
                with J.lock(self.path('/var/lib/twin/b/golive-state'), 'switch', exclusive=True):
                    pass
                modes = [stat.S_IMODE(os.lstat(self.path(f'/var/lib/{top}{rest}')).st_mode)
                         for top in ('a', 'twin') for rest in ('', '/b', '/b/golive-state', '/b/golive-state/lock')]
                self.assertEqual(modes[:4], modes[4:])
                self.assertEqual(modes[:4], [0o755, 0o755, 0o700, 0o600])
                self.assertFalse(os.path.lexists(self.path('/var/lib/nothere')))

    def test_what_is_made_is_never_reached_through_a_link(self):
        """The folders and the lock the child makes, and root's opening them again after it, go through no link
        the T3 user puts there meanwhile: nothing is made or opened in the other folder, and uninstall refuses."""
        other = '/home/other'
        made_at = {'the state folder, by the child': 'golive-state', 'the lock, by the child': 'lock'}
        for what, name in made_at.items():
            with self.subTest(what=what):
                self.lock_host(other, '/var/lib')
                before, mkdir = self.files_under(self.path(other)), os.mkdir

                def then_swapped(path, mode=0o777, *, dir_fd=None, name=name):
                    mkdir(path, mode, dir_fd=dir_fd)
                    if path != 'golive-state':
                        return
                    if name == 'golive-state':
                        os.rename(path, 'golive-state.old', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
                        os.symlink(self.path(other), path, dir_fd=dir_fd)
                    else:
                        os.symlink(self.path(f'{other}/lock'), self.path(f'{STATE}/lock'))
                with mock.patch.object(os, 'mkdir', then_swapped):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'cannot make {STATE}/lock as uid {os.geteuid()}', err)
                self.assertEqual(self.files_under(self.path(other)), before)
                self.assertTrue(os.path.isdir(self.version()))
        with self.subTest(what='the state folder, by root after the child'):
            self.lock_host(other, '/var/lib')
            with open(self.path(f'{other}/lock'), 'w'):
                pass
            run_as = self.m.run_as

            def then_swapped(*a, **k):
                run_as(*a, **k)
                os.rename(self.path(STATE), self.path(f'{STATE}.old'))
                os.symlink(self.path(other), self.path(STATE))
            with mock.patch.object(self.m, 'run_as', then_swapped):
                code, _, err = self.run_main('uninstall')
            self.assertEqual(code, 1, err)
            self.assertIn(f'{STATE} cannot be opened without following a link', err)
            self.assertTrue(os.path.isdir(self.version()))

    def test_root_checks_again_what_the_child_made(self):
        """Between the child and root, the T3 user may put another folder at the state folder's name: one of
        another user's (moved in) or one on FUSE. Root's walk to what the child made checks each folder as the
        walk to it does: refused, and the lock in that folder is not taken."""
        other = os.geteuid() + 1
        for what in ("another user's folder", 'a folder on FUSE'):
            with self.subTest(what=what):
                self.lock_host('/var/lib/planted')
                with open(self.path('/var/lib/planted/lock'), 'w'):
                    pass
                planted, run_as, real = os.lstat(self.path('/var/lib/planted')), self.m.run_as, self.m.mount_type
                fuse = what == 'a folder on FUSE'

                def then_swapped(*a, **k):
                    run_as(*a, **k)
                    os.rename(self.path(STATE), self.path(f'{STATE}.made'))
                    os.rename(self.path('/var/lib/planted'), self.path(STATE))

                def mount_type(fd):
                    st = os.fstat(fd)
                    return 'fuse.sshfs' if fuse and (st.st_dev, st.st_ino) == (planted.st_dev, planted.st_ino) \
                        else real(fd)
                with self.owners(others={} if fuse else {'/var/lib/planted': other}), \
                        mock.patch.object(self.m, 'run_as', then_swapped), \
                        mock.patch.object(self.m, 'mount_type', mount_type):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'{STATE} is on a filesystem of type fuse.sshfs' if fuse else
                              f'{STATE} belongs to uid {other}, inside a folder of uid {os.geteuid()}', err)
                self.assertTrue(os.path.isdir(self.version()))

    def test_a_mount_on_the_lock_or_journal_refuses(self):
        """A filesystem mounted on the lock or the journal file itself (fusermount takes a user's own file as a
        mount point; with user_allow_other, root's open would go to the T3 user's program, which could keep it
        waiting and show anything): refused before the file is opened for reading."""
        for name in ('lock', 'journal.jsonl'):
            with self.subTest(name=name):
                self.golive_state()
                self.journal(('switch', ['stop-t3'], 'ok', False))
                at, real, opened, real_open = os.lstat(self.path(f'{STATE}/{name}')), self.m.mount_id, [], os.open

                def mount_id(fd):
                    st = os.fstat(fd)
                    return 'elsewhere' if (st.st_dev, st.st_ino) == (at.st_dev, at.st_ino) else real(fd)

                def watched(path, flags, *a, **k):
                    fd = real_open(path, flags, *a, **k)
                    st = os.fstat(fd)
                    if not flags & os.O_PATH and (st.st_dev, st.st_ino) == (at.st_dev, at.st_ino):
                        opened.append(path)
                    return fd
                with mock.patch.object(self.m, 'mount_id', mount_id), mock.patch.object(os, 'open', watched):
                    code, _, err = self.run_main('uninstall')
                self.assertEqual(code, 1, err)
                self.assertIn(f'{STATE}/{name} is on another mount than its folder', err)
                self.assertEqual(opened, [])
                self.assertTrue(os.path.isdir(self.version()))

    def test_the_lock_opened_is_the_file_looked_at(self):
        """The lock is looked at as a path only, then that same file is opened (through /proc/self/fd), not
        whatever has its name by then: a link to another user's lock swapped in between is not followed, and
        that lock is not taken."""
        other = '/home/other/state'
        self.golive_state()
        os.makedirs(self.path(other))
        with open(self.path(f'{STATE}/lock'), 'w'), open(self.path(f'{other}/lock'), 'w'):
            pass
        real, swapped = self.m.mount_id, []

        def mount_id(fd):
            st, lock = os.fstat(fd), os.lstat(self.path(f'{STATE}/lock'))
            if not swapped and (st.st_dev, st.st_ino) == (lock.st_dev, lock.st_ino):
                os.rename(self.path(f'{STATE}/lock'), self.path(f'{STATE}/lock.looked-at'))
                os.symlink(self.path(f'{other}/lock'), self.path(f'{STATE}/lock'))
                swapped.append(1)
            return real(fd)
        free, in_use = [], self.m.in_use

        def under_the_lock():               # the other user's lock is not held by this uninstall
            fd = os.open(self.path(f'{other}/lock'), os.O_RDONLY)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                free.append(1)
            except BlockingIOError:
                pass
            finally:
                os.close(fd)
            return in_use()
        with mock.patch.object(self.m, 'mount_id', mount_id), mock.patch.object(self.m, 'in_use', under_the_lock):
            code, _, err = self.run_main('uninstall')
        self.assertEqual((code, swapped, free), (0, [1], [1]), err)

    def test_a_lease_on_the_lock_never_makes_root_wait(self):
        """The lock is the T3 user's own file, so they may hold a lease on it: an open without O_NONBLOCK would
        wait for it (lease-break-time, 45 s by default) with the install lock held. Refused at once instead."""
        self.golive_state()
        with open(self.path(f'{STATE}/lock'), 'w'):
            pass
        old = signal.signal(signal.SIGIO, signal.SIG_IGN)       # the lease's holder is told by SIGIO
        self.addCleanup(signal.signal, signal.SIGIO, old)
        fd = os.open(self.path(f'{STATE}/lock'), os.O_RDONLY)
        self.addCleanup(os.close, fd)
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        self.addCleanup(fcntl.fcntl, fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        with self.no_hang(10):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        self.assertIn(f'{STATE}/lock cannot be read (Resource temporarily unavailable)', err)
        self.assertTrue(os.path.isdir(self.version()))

    def test_the_child_holds_none_of_roots_files_and_is_ended(self):
        """The child runs as the user, who may stop it: it holds none of root's open files (install.lock among
        them) but the folder it needs, and has /dev/null as stdout. Stopped (or kept waiting), it is ended after
        CHILD_SECONDS; root's run ended first (Ctrl-C), it is ended too. Either way it is gone, not left holding
        anything."""
        log = os.path.join(self.host, 'log')
        extra = os.open(os.path.join(self.host, 'install.lock'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, extra)
        keep = os.open(self.host, os.O_PATH | os.O_DIRECTORY)
        self.addCleanup(os.close, keep)
        high = next(n for n in range(200, 1000) if not os.path.lexists(f'/proc/self/fd/{n}'))
        os.dup2(extra, high)                # above the pipe run_as opens, so above every fd the child keeps
        self.addCleanup(os.close, high)
        stdio = os.open(os.path.join(self.host, 'stdio'), os.O_RDWR | os.O_CREAT, 0o600)
        self.addCleanup(os.close, stdio)

        def look():
            seen = []
            for fd in (extra, high, keep):
                try:
                    os.fstat(fd)
                    seen.append('open')
                except OSError:
                    seen.append('closed')
            seen += [os.readlink(f'/proc/self/fd/{n}') for n in (0, 1, 2)]
            seen.append('own session' if os.getsid(0) == os.getpid() else "root's session")
            with open(log, 'w') as f:
                f.write(' '.join(seen))
        saved = [os.dup(n) for n in (0, 1, 2)]     # root's 0-2 a file of its own, not /dev/null already
        try:
            for n in (0, 1, 2):
                os.dup2(stdio, n)
            self.m.run_as(os.geteuid(), os.getegid(), look, 'look', keep=(keep,))
        finally:
            for n, fd in enumerate(saved):
                os.dup2(fd, n)
                os.close(fd)
        with open(log) as f:
            self.assertEqual(f.read(), 'closed closed open /dev/null /dev/null /dev/null own session')
        pidfile = os.path.join(self.host, 'pid')

        def child_pid():
            for _ in range(200):
                if os.path.exists(pidfile):
                    with open(pidfile) as f:
                        return int(f.read() or 0) or None
                time.sleep(0.05)
            return None

        def gone(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)

        def start(then):
            def fn():
                with open(pidfile + '.tmp', 'w') as f:
                    f.write(str(os.getpid()))
                os.rename(pidfile + '.tmp', pidfile)
                then()
            return fn
        def gone_later():                   # registered before each run: a broken guard (RedCheck) leaves none
            pid = child_pid() if os.path.exists(pidfile) else None
            if pid is not None:
                os.unlink(pidfile)
                gone(pid)
        stop = start(lambda: os.kill(os.getpid(), signal.SIGSTOP))
        wait = start(lambda: time.sleep(30))
        self.m.CHILD_SECONDS = 1
        self.addCleanup(gone_later)
        with self.no_hang(15), self.assertRaisesRegex(self.mod.kit_install.Refused, r'it took over 1 s'):
            self.m.run_as(os.geteuid(), os.getegid(), stop, 'make it')
        pid = child_pid()
        with self.assertRaises(ProcessLookupError):                 # killed and reaped
            os.kill(pid, 0)
        os.unlink(pidfile)
        self.m.CHILD_SECONDS = 30
        self.addCleanup(gone_later)
        real = select.select

        def interrupted(*a):
            if child_pid() is not None:
                raise KeyboardInterrupt
            return real(*a)
        with self.no_hang(10), mock.patch.object(select, 'select', interrupted), \
                self.assertRaises(KeyboardInterrupt):
            self.m.run_as(os.geteuid(), os.getegid(), wait, 'make it')
        pid = child_pid()
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)

    def test_put_back_leaves_no_restore_folder_of_its_own(self):
        """put_back alone (not only _remove's loop after it) leaves no <path>.restore."""
        self.hand_copy()
        kept = os.path.join(self.host, 'kept')
        with open(kept, 'w') as f:
            f.write(HAND_COPY)
        self.m.put_back(COMMAND, kept, (0o750, os.geteuid(), os.getegid()))
        self.assertEqual(os.listdir(self.path('/usr/local/bin')), ['t3-golive'])
        with open(self.path(COMMAND)) as f:
            self.assertEqual(f.read(), HAND_COPY)

    def test_a_lock_gone_before_root_opens_it_refuses(self):
        """Made by the user's child, then gone before root opens it (the T3 user removed it): no lock is held, so
        uninstall refuses and a run again makes it."""
        self.lock_host(STATE)
        run_as = self.m.run_as

        def then_removed(*a, **k):
            run_as(*a, **k)
            os.unlink(self.path(f'{STATE}/lock'))
        with mock.patch.object(self.m, 'run_as', then_removed):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        self.assertIn(f'{STATE}/lock is gone again: run uninstall again', err)
        self.assertTrue(os.path.isdir(self.version()))
        code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 0, err)
        self.assertFalse(os.path.lexists(self.path(LIBEXEC)))

    def test_the_lock_is_made_as_the_user_with_no_root_rights(self):
        """In a child process: no groups, then the group, then the user (in that order: after setuid it could no
        longer drop the rest); what fails there refuses."""
        log = os.path.join(self.host, 'log')
        user = os.geteuid() + 1

        def fake(name):
            def call(*args):
                with open(log, 'a') as f:
                    f.write(f'{name}{args}\n')
            return call
        with mock.patch.object(os, 'setgroups', fake('setgroups')), mock.patch.object(os, 'setgid', fake('setgid')), \
                mock.patch.object(os, 'setuid', fake('setuid')):
            self.m.run_as(user, 4343, fake('fn'), 'make it')
            with self.assertRaisesRegex(self.mod.kit_install.Refused, r'cannot make it \(no such\)'):
                self.m.run_as(user, 4343, lambda: (_ for _ in ()).throw(OSError('no such')), 'make it')
        with open(log) as f:
            self.assertEqual(f.read().splitlines(), ['setgroups([],)', 'setgid(4343,)', f'setuid({user},)', 'fn()',
                                                     'setgroups([],)', 'setgid(4343,)', f'setuid({user},)'])
        with mock.patch.object(os, 'setgroups', mock.Mock(side_effect=PermissionError(1, 'not root'))):
            with self.assertRaisesRegex(self.mod.kit_install.Refused, 'not root'):
                self.m.run_as(user, 4343, fake('fn never'), 'make it')
        with open(log) as f:
            self.assertNotIn('fn never', f.read())

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

    def test_the_journal_is_read_where_no_lock_is_made(self):
        """A state folder of root's with a journal but no lock (a run as root, or a lock removed by hand): no lock is
        made there, but the journal is still read, and a run that needs settling refuses; settled, it is fine."""
        for runs, code in (([('switch', ['stop-t3'], None, False)], 1), ([('switch', ['stop-t3'], 'ok', False)], 0)):
            with self.subTest(code=code):
                self.golive_state()
                self.journal(*runs)
                os.unlink(self.path(f'{STATE}/lock'))
                self.m.SYSTEM_UID = os.geteuid()                    # the state folder is root's
                got, out, err = self.run_main('uninstall')
                self.assertEqual(got, code, err)
                self.assertFalse(os.path.lexists(self.path(f'{STATE}/lock')))
                if code:
                    self.assertIn('a golive run needs settling', err)
                    self.assertTrue(os.path.isdir(self.version()))

    def test_a_journal_over_the_cap_is_not_read(self):
        """journal.jsonl is the T3 user's: a huge one (a sparse 1 TB file, say) is never read whole into root's
        memory or copied to /tmp; over JOURNAL_BYTES it refuses with a message, having read no more than
        JOURNAL_BYTES + 1. At the cap it is read as usual. (Here: a cap of 4096 and a sparse 64 MiB file.)"""
        self.golive_state()
        self.journal(('switch', ['stop-t3'], 'ok', False))
        size = os.path.getsize(self.path(f'{STATE}/journal.jsonl'))
        read, fdopen = [], os.fdopen

        class Reader:                       # how much of the journal is read
            def __init__(self, f):
                self.f = f

            def __enter__(self):
                return self

            def __exit__(self, *_):
                self.f.close()

            def read(self, n=-1):
                read.append(n)
                return self.f.read(n)

        def watched(fd, *a, **k):
            st, journal = os.fstat(fd), os.lstat(self.path(f'{STATE}/journal.jsonl'))
            f = fdopen(fd, *a, **k)
            return Reader(f) if (st.st_dev, st.st_ino) == (journal.st_dev, journal.st_ino) else f
        self.m.JOURNAL_BYTES = size
        self.assertEqual(self.run_main('uninstall')[0], 0)
        self.golive_state()
        self.journal(('switch', ['stop-t3'], 'ok', False))
        with open(self.path(f'{STATE}/journal.jsonl'), 'r+b') as f:
            f.truncate(64 << 20)
        self.m.JOURNAL_BYTES = 4096
        with self.no_hang(10), mock.patch.object(os, 'fdopen', watched):
            code, _, err = self.run_main('uninstall')
        self.assertEqual(code, 1, err)
        self.assertIn(f'{STATE}/journal.jsonl is over 4096 bytes', err)
        self.assertEqual(read, [4097])
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
        'engine', '                self.keep_old(path)\n', '',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: a plain file is not refused': (
        'engine', '            if path in self.KEEP_OLD and self.plain_file(real):\n                continue', '',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: put back by uninstall': (
        'engine', '            if path in last and (self.ours(path, target) or not os.path.lexists(self.p(path))):',
        '            if False:', 'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: the last one kept is put back': (
        'engine', '        for path, src in reversed(kept):', '        for path, src in kept:',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: by the order kept, not by a file time': (
        'engine', '        return [found[n] for n in order] + [found[n] for n in sorted(set(found) - set(order))]',
        '        return sorted(found.values(), key=lambda pk: os.path.getmtime(pk[1]))',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: one kept again is the last one again': (
        'engine', '        rows = [r for r in self.order_rows() if r[0] != name] + [(name, meta)]',
        '        rows = self.order_rows() + ([] if name in self.kept_order() else [(name, meta)])',
        'test_an_uninstall_puts_back_the_hand_copy_kept_last'),
    'hand copy: in the install record': (
        'engine', '        for _, kept in self.replaced():\n', '        for _, kept in []:\n',
        'test_a_hand_copy_is_kept_replaced_and_put_back'),
    'hand copy: only a plain file is kept, never a link': (
        'engine', '        return os.path.isfile(real) and not os.path.islink(real)', '        return os.path.lexists(real)',
        'test_a_hand_copy_that_is_a_link_or_folder_is_refused'),
    'hand copy: put back over the link in one step': (
        'engine', '        os.replace(tmp, real)\n        os.rmdir(stage)', '        if os.path.lexists(real):\n'
        '            os.unlink(real)\n        os.rename(tmp, real)\n        os.rmdir(stage)',
        'test_uninstall_stopped_anywhere_never_leaves_the_command_missing'),
    'hand copy: its own mode': (
        'engine', '            os.chmod(tmp, mode)             # after chown', '            os.chmod(tmp, 0o755)  #',
        'test_the_hand_copy_comes_back_with_its_own_mode_and_owner'),
    'hand copy: its own owner': (
        'engine', '            os.chown(tmp, uid, gid)\n', '            pass\n',
        'test_the_hand_copy_comes_back_with_its_own_mode_and_owner'),
    'hand copy: the mode after the owner (chown clears a set-id bit)': (
        'engine', '            os.chown(tmp, uid, gid)\n            os.chmod(tmp, mode)',
        '            os.chmod(tmp, mode)\n            os.chown(tmp, uid, gid)',
        'test_the_hand_copy_comes_back_with_its_own_mode_and_owner'),
    'hand copy: an old order file still puts it back': (
        'engine', '            os.chmod(tmp, stat.S_IMODE(os.stat(kept).st_mode))', '            os.chmod(tmp, 0o600)',
        'test_the_hand_copy_comes_back_with_its_own_mode_and_owner'),
    'hand copy: a leftover .restore goes on install': (
        'engine', '                self.drop_restore_leftover(path)\n                self.keep_old(path)',
        '                self.keep_old(path)', 'test_a_leftover_restore_file_is_removed_by_install_and_uninstall'),
    'hand copy: made where only root can reach it before chown': (
        'engine', "        tmp = os.path.join(stage, os.path.basename(real))", "        tmp = real + '.tmp'",
        'test_the_hand_copy_comes_back_with_its_own_mode_and_owner'),
    'hand copy: a leftover .restore goes before the copy is made again': (
        'engine', "        self.drop_restore_leftover(path)\n        os.mkdir(stage, 0o700)",
        "        os.mkdir(stage, 0o700)", 'test_uninstall_stopped_anywhere_never_leaves_the_command_missing'),
    'hand copy: put back where the command is missing': (
        'engine', ' or not os.path.lexists(self.p(path))):', '):',
        'test_the_hand_copy_comes_back_where_the_command_is_missing'),
    'hand copy: a leftover .restore goes on uninstall': (
        'engine', '                os.unlink(self.p(path))\n            if path in self.KEEP_OLD:\n'
        '                self.drop_restore_leftover(path)\n', '                os.unlink(self.p(path))\n',
        'test_a_leftover_restore_file_is_removed_by_install_and_uninstall'),
    'uninstall: makes the lock a first run would take': (
        'installer', '                made = self.make_lock(state, fd, where, rest)\n', '                made = None\n',
        'test_uninstall_makes_the_lock_a_first_run_would_take'),
    "uninstall: makes no lock in root's folder": (
        'installer', '        if uid == self.SYSTEM_UID:\n            return None\n', '',
        'test_uninstall_makes_the_lock_a_first_run_would_take'),
    'uninstall: the way to the lock is walked with no link followed': (
        'installer', 'new = os.open(part, WALK, dir_fd=fd)', 'new = os.open(part, WALK & ~os.O_NOFOLLOW, dir_fd=fd)',
        'test_the_lock_is_never_made_through_a_link_the_t3_user_could_plant'),
    "uninstall: a link on the way to the lock that is not root's refuses": (
        'installer', '            if st.st_uid != self.SYSTEM_UID:\n', '            if False:\n',
        'test_the_lock_is_never_made_through_a_link_the_t3_user_could_plant'),
    'uninstall: the lock is made in the folder walked to, not by its path': (
        'installer', '            at = fd\n            for i, part in enumerate(rest):',
        '            at = os.open(self.p(where), os.O_RDONLY)\n            for i, part in enumerate(rest):',
        'test_the_lock_is_never_made_through_a_link_the_t3_user_could_plant'),
    "uninstall: a link of root's on the way is gone along": (
        'installer', "                        todo = [x for x in (target if target.startswith('/') else",
        "                        raise self.Refused('link')\n"
        "                        todo = [x for x in (target if target.startswith('/') else",
        'test_a_link_of_roots_on_the_way_is_gone_along'),
    'uninstall: a lock gone before root opens it refuses': (
        'installer', '            if lfd is None:\n                raise self.Refused(', '            if False:\n'
        '                raise self.Refused(', 'test_a_lock_gone_before_root_opens_it_refuses'),
    'uninstall: the lock is made as the user, not root': (
        'installer', '                    os.setuid(uid)\n', '', 'test_the_lock_is_made_as_the_user_with_no_root_rights'),
    "uninstall: the lock is made with none of root's groups": (
        'installer', '                    os.setgroups([])\n', '', 'test_the_lock_is_made_as_the_user_with_no_root_rights'),
    'uninstall: refused under a golive run': (
        'installer', '                fcntl.flock(lfd, fcntl.LOCK_EX | fcntl.LOCK_NB)\n',
        '                pass\n', 'test_uninstall_is_refused_while_a_golive_run_holds_its_lock'),
    'uninstall: refused while a run needs settling': (
        'installer', '            self.refuse_unsettled(state, folder)\n            yield\n        finally:',
        '            yield\n        finally:',
        'test_uninstall_is_refused_while_a_run_needs_settling'),
    'uninstall: a journal it cannot trust refuses': (
        'installer', '            except journal.Damaged as e:        # Newer too\n                raise',
        '            except journal.Damaged as e:        # Newer too\n                return\n                raise',
        'test_uninstall_is_refused_while_a_run_needs_settling'),
    'uninstall: only a plain file in the state folder (a FIFO is never waited on)': (
        'installer', '            if not stat.S_ISREG(os.fstat(pfd).st_mode):', '            if False:',
        'test_a_special_file_in_the_state_folder_refuses_and_never_blocks'),
    'uninstall: no link followed in the state folder': (
        'installer', 'pfd = os.open(name, os.O_PATH | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=dir_fd)',
        'pfd = os.open(name, os.O_PATH | os.O_CLOEXEC, dir_fd=dir_fd)',
        'test_a_special_file_in_the_state_folder_refuses_and_never_blocks'),
    'uninstall: a relative link of root\'s is read from its own folder': (
        'installer', "else f'{done}/{target}'", "else f'/{target}'", 'test_a_link_of_roots_on_the_way_is_gone_along'),
    "uninstall: a link of root's only from a folder of root's": (
        'installer', '                if folder.st_uid != self.SYSTEM_UID or folder.st_mode & 0o022:',
        '                if folder.st_mode & 0o022:', 'test_a_link_of_roots_is_gone_along_only_from_a_folder_of_roots'),
    "uninstall: a link of root's only from a folder no one else may write in": (
        'installer', '                if folder.st_uid != self.SYSTEM_UID or folder.st_mode & 0o022:',
        '                if folder.st_uid != self.SYSTEM_UID:',
        'test_a_link_of_roots_is_gone_along_only_from_a_folder_of_roots'),
    'uninstall: something on the way that is not a link refuses as such': (
        'installer', '                if not stat.S_ISLNK(st.st_mode):', '                if False:',
        'test_the_walk_refuses_what_is_not_a_plain_way_there'),
    'uninstall: a link that cannot be read refuses': (
        'installer', "            except OSError as e:\n                raise self.Refused(f'{shown} cannot be read",
        "            except KeyError as e:\n                raise self.Refused(f'{shown} cannot be read",
        'test_the_walk_refuses_what_is_not_a_plain_way_there'),
    'uninstall: a loop of links ends': (
        'installer', '                        if hops > 40:', '                        if False:',
        'test_the_walk_refuses_what_is_not_a_plain_way_there'),
    'uninstall: a missing folder before ".." refuses': (
        'installer', "                        if '..' in todo:", '                        if False:',
        'test_the_walk_refuses_what_is_not_a_plain_way_there'),
    "uninstall: below a user's folder, every folder is that user's": (
        'installer', '        if uid != owner:\n', '        if False:\n',
        'test_below_a_users_folder_every_folder_is_that_users'),
    'uninstall: no folder on FUSE': (
        'installer', "        if kind is None or kind.split('.')[0] in ('fuse', 'fuseblk'):", '        if kind is None:',
        'test_a_folder_on_fuse_refuses'),
    'uninstall: no folder whose filesystem cannot be told': (
        'installer', "        if kind is None or kind.split('.')[0] in ('fuse', 'fuseblk'):",
        "        if kind is not None and kind.split('.')[0] in ('fuse', 'fuseblk'):", 'test_a_folder_on_fuse_refuses'),
    'uninstall: the filesystem type after the "-" of mountinfo': (
        'installer', "return fields[fields.index('-', 6) + 1]", 'return fields[7]',
        'test_the_filesystem_type_comes_from_the_mount_id'),
    'uninstall: the lock is made with the group of the user entry': (
        'installer', '            gid = pwd.getpwuid(uid).pw_gid', '            gid = os.fstat(fd).st_gid',
        'test_the_lock_is_made_with_the_user_entrys_group'),
    "uninstall: the lock is made with none of root's group": (
        'installer', '            gid = pwd.getpwuid(uid).pw_gid', '            gid = 0',
        'test_the_lock_is_made_with_the_user_entrys_group'),
    'uninstall: a uid with no user entry refuses': (
        'installer', '        except KeyError:\n            raise self.Refused(',
        '        except KeyError:\n            gid = 0\n        if False:\n            raise self.Refused(',
        'test_the_lock_is_made_with_the_user_entrys_group'),
    'uninstall: the folders above the state folder as golive makes them': (
        'installer', '0o700 if i == len(rest) - 1 else 0o777', '0o700',
        'test_the_folders_made_are_as_golive_makes_them'),
    'uninstall: stateDir spelled as golive spells it': (
        'installer', 'return os.path.normpath(state) if', 'return state if',
        'test_the_folders_made_are_as_golive_makes_them'),
    'uninstall: the child makes the folders through no link': (
        'installer', 'at = os.open(part, kit_install.DIR_FLAGS | os.O_NONBLOCK, dir_fd=at)',
        'at = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NONBLOCK, dir_fd=at)',
        'test_what_is_made_is_never_reached_through_a_link'),
    'uninstall: the child makes the lock through no link': (
        'installer', "os.close(os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK",
        "os.close(os.open('lock', os.O_RDWR | os.O_CREAT | os.O_NONBLOCK",
        'test_what_is_made_is_never_reached_through_a_link'),
    'uninstall: root opens what the child made through no link': (
        'installer', 'new = os.open(part, WALK, dir_fd=at)', 'new = os.open(part, WALK & ~os.O_NOFOLLOW, dir_fd=at)',
        'test_what_is_made_is_never_reached_through_a_link'),
    "uninstall: the child holds none of root's files": (
        'installer', "                for k in mine:      # before the rights are dropped: from then on that user may "
        "stop it\n                    os.closerange(low, k)\n                    low = max(low, k + 1)\n"
        "                os.closerange(low, os.sysconf('SC_OPEN_MAX'))\n", '',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    'uninstall: a stopped child is ended': (
        'installer', '                if left <= 0 or not select.select([r], [], [], left)[0]:', '                if False:',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    'uninstall: the child is ended when root stops': (
        'installer', '                    os.kill(pid, signal.SIGKILL)\n', '                    pass\n',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    'hand copy: put_back removes its own .restore': (
        'engine', '        os.rmdir(stage)\n        self.say(', '        self.say(',
        'test_put_back_leaves_no_restore_folder_of_its_own'),
    "uninstall: the owner below the first folder that is not root's is carried down the walk": (
        'installer', '                    owner = self.checked(fd, done, owner, path)',
        '                    self.checked(fd, done, owner, path)', 'test_below_a_users_folder_every_folder_is_that_users'),
    'uninstall: / is checked too': (
        'installer', "                owner = self.checked(fd, '/', None, path)", '                owner = None',
        'test_a_folder_on_fuse_refuses'),
    'uninstall: root checks again the folders the child made': (
        'installer', '                    self.checked(at, shown, uid, state)\n', '',
        'test_root_checks_again_what_the_child_made'),
    'uninstall: the folders the child made are that user\'s': (
        'installer', '                    self.checked(at, shown, uid, state)\n',
        '                    self.checked(at, shown, None, state)\n', 'test_root_checks_again_what_the_child_made'),
    'uninstall: the child has /dev/null on 0-2': (
        'installer', '                for std in (0, 1, 2):\n                    if std not in mine',
        '                for std in (1,):\n                    if std not in mine',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    'uninstall: the child holds none of root\'s files above the ones it keeps': (
        'installer', "                os.closerange(low, os.sysconf('SC_OPEN_MAX'))\n", '',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    "uninstall: the child is in a session of its own": (
        'installer', '                os.setsid()         #', '                pass  #',
        'test_the_child_holds_none_of_roots_files_and_is_ended'),
    'uninstall: the journal is read where no lock is made': (
        'installer', '                if folder is not None:\n                    self.refuse_unsettled(state, folder)',
        '                if False:\n                    self.refuse_unsettled(state, folder)',
        'test_the_journal_is_read_where_no_lock_is_made'),
    'uninstall: a journal over the cap is not read': (
        'installer', '            data = f.read(self.JOURNAL_BYTES + 1)', '            data = f.read()',
        'test_a_journal_over_the_cap_is_not_read'),
    'uninstall: a journal over the cap refuses': (
        'installer', '        if len(data) > self.JOURNAL_BYTES:', '        if False:',
        'test_a_journal_over_the_cap_is_not_read'),
    'uninstall: a mount on the lock or journal refuses': (
        'installer', '            if mount is None or mount != self.mount_id(dir_fd):', '            if False:',
        'test_a_mount_on_the_lock_or_journal_refuses'),
    'uninstall: the lock opened is the file looked at': (
        'installer', "                return os.open(f'/proc/self/fd/{pfd}', os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)",
        "                return os.open(name, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=dir_fd)",
        'test_the_lock_opened_is_the_file_looked_at'),
    'uninstall: a lease on the lock never makes root wait': (
        'installer', "                return os.open(f'/proc/self/fd/{pfd}', os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)",
        "                return os.open(f'/proc/self/fd/{pfd}', os.O_RDONLY | os.O_CLOEXEC)",
        'test_a_lease_on_the_lock_never_makes_root_wait'),
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
