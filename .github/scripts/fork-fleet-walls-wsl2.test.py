#!/usr/bin/env python3
"""Tests of fork-fleet-walls/wsl2 beyond the contract every walls file passes (fork-fleet-walls.test.py, from
wsl2.fixture.json): that its reading of the launcher's sandbox() fits the real launcher, every change to that
sandbox() that opens a wall (a fixture changes the fake machine only, and the launcher is the kit's, beside the walls
folder), that it takes every linux wall from the linux file, and that it runs the linux file only from a source no
more open than itself. Run: python3 <this>. Linux only."""
import importlib.util, json, os, shutil, signal, subprocess, sys, tempfile, unittest
from importlib._bootstrap_external import _code_to_timestamp_pyc

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
JOB_PATHS = 'fork-fleet-job-paths.txt'
LAUNCHER = os.path.join(HERE, 'fork-fleet-launch.sh')


def slurp(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def load(name, root=''):
    """A walls file as a module, reading the fake machine root."""
    old = os.environ.get('FORK_FLEET_WALLS_ROOT')
    os.environ['FORK_FLEET_WALLS_ROOT'] = root
    try:
        module = type(sys)(f'walls_{name}')
        module.__file__ = os.path.join(FOLDER, name)
        exec(compile(slurp(module.__file__), module.__file__, 'exec'), module.__dict__)
    finally:
        if old is None:
            del os.environ['FORK_FLEET_WALLS_ROOT']
        else:
            os.environ['FORK_FLEET_WALLS_ROOT'] = old
    return module


wsl2 = load('wsl2')
FIXTURE = json.loads(slurp(os.path.join(FOLDER, 'wsl2.fixture.json')))


class Case(unittest.TestCase):
    def tmp(self):
        tmp = tempfile.mkdtemp(prefix='fork-fleet-walls-wsl2-test-')
        os.chmod(tmp, 0o755)
        self.addCleanup(shutil.rmtree, tmp)
        return tmp

    def copy(self, linux_mode=0o755, wsl2_mode=0o755, linux_text=None, launcher_text=None):
        """A walls folder of its own (wsl2, linux; the launcher and its allow list one folder up, as an install has
        them): its wsl2's path. launcher_text: the launcher's text instead (None: the real one)."""
        kit = self.tmp()
        folder = os.path.join(kit, 'fork-fleet-walls')
        os.mkdir(folder, 0o755)
        shutil.copy(os.path.join(HERE, JOB_PATHS), kit)
        os.chmod(os.path.join(kit, JOB_PATHS), 0o644)
        with open(os.path.join(kit, 'fork-fleet-launch.sh'), 'w', encoding='utf-8') as f:
            f.write(slurp(LAUNCHER) if launcher_text is None else launcher_text)
        os.chmod(os.path.join(kit, 'fork-fleet-launch.sh'), 0o755)
        for name, mode in (('linux', linux_mode), ('wsl2', wsl2_mode)):
            shutil.copy(os.path.join(FOLDER, name), folder)
            if name == 'linux' and linux_text is not None:
                with open(os.path.join(folder, name), 'w', encoding='utf-8') as f:
                    f.write(linux_text)
            os.chmod(os.path.join(folder, name), mode)
        return os.path.join(folder, 'wsl2')

    def run_wsl2(self, path, root, verb='describe'):
        return subprocess.run([path, verb], capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL,
                              env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C', 'FORK_FLEET_WALLS_ROOT': root})

    def fake(self, changes=None):
        root = self.tmp()
        files = dict(FIXTURE['pass'])
        files.update(changes or {})
        for rel, value in files.items():
            full = os.path.join(root, rel)
            if value is None:
                continue
            if rel.endswith('/'):
                os.makedirs(full, exist_ok=True)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w', encoding='utf-8') as f:
                f.write(value['text'] if isinstance(value, dict) else value)
            os.chmod(full, int(value['mode'], 8) if isinstance(value, dict) else 0o644)   # as installed, any umask
        return root


def walls_with(test, root, launcher_text=None):
    """wsl2 and its linux walls as modules on the fake machine root, with a kit of their own (launcher_text: its
    launcher's text; None: the real one)."""
    kit = os.path.dirname(os.path.dirname(test.copy(launcher_text=launcher_text)))
    m = load('wsl2', root)
    m.LINUX = load('linux', root)
    m.LINUX.KIT, m.LINUX.LAUNCHER = kit, os.path.join(kit, 'fork-fleet-launch.sh')
    # The walls hand FORK_FLEET_WALLS_ROOT to the launcher when they run it (job_view), not only at load: keep it set
    # for the whole test, or `paths` checks this test machine's own /opt (open to all on some CI runners).
    old = os.environ.get('FORK_FLEET_WALLS_ROOT')
    os.environ['FORK_FLEET_WALLS_ROOT'] = root
    test.addCleanup(lambda: os.environ.pop('FORK_FLEET_WALLS_ROOT') if old is None
                    else os.environ.__setitem__('FORK_FLEET_WALLS_ROOT', old))
    return m


ARCH = '    -p SystemCallArchitectures=native\n'
TMPFS = '    -p "TemporaryFileSystem=/run /dev/shm:size=1G,mode=1777 /tmp:size=1G,mode=1777 /var/tmp:size=1G,mode=1777"\n'
FAMILIES = '    -p RestrictAddressFamilies=~AF_VSOCK\n'
FILTER = '    -p "SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS"\n'
# Changes to the real launcher's sandbox() (old line -> new text) that open a wall, with what its FAIL line says. They
# were fixture cases while the walls read a launcher on the fake machine; the kit's launcher is not on it.
OPENS = [
    ('sockets', TMPFS, TMPFS.replace('/run ', ''), 'see this host\'s /run'),
    ('sockets', TMPFS, TMPFS.replace(' /tmp:size=1G,mode=1777', ''), 'see this host\'s /tmp'),
    ('sockets', TMPFS, TMPFS + '    -p TemporaryFileSystem=\n', 'see this host\'s /run'),   # an empty one resets
    ('sockets', TMPFS, TMPFS + '    -p TemporaryFileSystem=\n    -p TemporaryFileSystem=/tmp\n', 'see this host\'s /run'),
    ('sockets', TMPFS, '    # ' + TMPFS.lstrip(), 'see this host\'s /run'),   # commented out
    ('sockets', FAMILIES, '    \n', 'AF_VSOCK is not on'),
    ('sockets', FAMILIES, '    -p "RestrictAddressFamilies=AF_UNIX AF_INET AF_VSOCK"\n', 'AF_VSOCK is not on'),
    ('sockets', FAMILIES, FAMILIES + '    -p RestrictAddressFamilies=\n', 'AF_VSOCK is not on'),
    ('sockets', FAMILIES, '    # ' + FAMILIES.lstrip(), 'AF_VSOCK is not on'),
    ('sockets', FILTER, '    \n', 'io_uring_setup is not on'),
    ('sockets', FILTER, FILTER + '    -p "SystemCallFilter=@system-service"\n', 'io_uring_setup is not on'),
    ('sockets', FILTER, '    -p "SystemCallFilter=~@mount"\n', 'io_uring_setup is not on'),   # a ~ filter without it
    ('sockets', ARCH, '    \n', 'SystemCallArchitectures is not set'),
    ('sockets', ARCH, ARCH + '    -p SystemCallArchitectures=\n', 'SystemCallArchitectures is not set'),
    ('sockets', ARCH, ARCH + '    -p SystemCallArchitectures=x86\n', 'SystemCallArchitectures is native x86'),
    ('sockets', ARCH, '    -p "SystemCallArchitectures=native x86"\n', 'SystemCallArchitectures is native x86'),
    ('sockets', 'sandbox() {\n', 'sandbox_x() {\n', 'cannot tell (ValueError: the launcher has no sandbox()'),
    ('userns', ARCH, ARCH + '    -p RestrictNamespaces=~net\n', 'jobs can make user namespaces'),
    ('userns', ARCH, ARCH + '    -p "RestrictNamespaces=net user"\n', 'jobs can make user namespaces'),
    ('userns', ARCH, ARCH + '    -p RestrictNamespaces=yes\n    -p RestrictNamespaces=\n', 'jobs can make user namespaces'),
    ('userns', ARCH, ARCH + '    -p RestrictNamespaces=no\n', 'jobs can make user namespaces'),
]


class Launcher(Case):
    def test_the_real_launcher_holds_the_vsock_and_sockets_walls(self):   # a reworded launcher must not blind them
        m = walls_with(self, self.fake())
        self.assertIsNone(m.vsock_wall())
        self.assertIsNone(m.sockets_wall())
        props = m.launcher_props()
        self.assertEqual(props.get('NetworkNamespacePath'), ['/run/netns/fork-fleet-$user'])
        self.assertTrue(all(name.isalnum() for name in props), sorted(props))

    def test_every_change_that_opens_a_wall_fails_it(self):
        """Each change, on the real launcher, FAILs its wall with its reason (not "cannot tell": `paths` still works),
        directly and as the only FAIL; the real launcher passes them all."""
        real = slurp(LAUNCHER)
        on = {'proc/sys/user/max_user_namespaces': '15000\n'}   # userns: off on the host, so the units must refuse it
        for wall, old, new, why in OPENS:
            with self.subTest(wall=wall, new=new):
                self.assertEqual(real.count(old), 1, f'the launcher no longer has {old!r}: update this test')
                p = self.run_wsl2(self.copy(launcher_text=real.replace(old, new)),
                                  self.fake(on if wall == 'userns' else {}), 'check')
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertEqual([line.split()[1] for line in p.stdout.splitlines()], [wall], p.stdout)
                self.assertIn(why, p.stdout)
                if 'cannot tell' not in why:
                    self.assertNotIn('cannot tell', p.stdout)
        p = self.run_wsl2(self.copy(), self.fake(on), 'check')
        self.assertEqual(p.returncode, 1, p.stderr)   # the real launcher: only the host's own userns setting fails
        self.assertEqual([line.split()[1] for line in p.stdout.splitlines()], ['userns'], p.stdout)

    def test_the_launcher_it_reads_is_the_kits_and_safe(self):
        """sandbox() comes from the launcher beside the walls folder, the one `paths` runs: no other copy on the
        machine counts, and an open or linked launcher is not read."""
        bad = slurp(LAUNCHER).replace(FAMILIES, '    \n')
        p = self.run_wsl2(self.copy(), self.fake({'opt/fork-fleet/fork-fleet-launch.sh': bad}), 'check')
        self.assertEqual((p.returncode, p.stdout), (0, ''), p.stderr)   # a launcher on the machine is not the kit's
        p = self.run_wsl2(self.copy(launcher_text=bad), self.fake(), 'check')
        self.assertIn('AF_VSOCK is not on', p.stdout)
        root = self.fake({'proc/sys/user/max_user_namespaces': '15000\n'})
        for name, change in (('group-writable', lambda l: os.chmod(l, 0o775)),
                             ('a link', lambda l: (os.rename(l, l + '.real'), os.symlink(l + '.real', l))),
                             ('a FIFO', lambda l: (os.remove(l), os.mkfifo(l, 0o755))),   # read at once, no wait
                             ('a folder', lambda l: (os.remove(l), os.mkdir(l, 0o755)))):
            with self.subTest(name):
                m = walls_with(self, root)
                change(m.LINUX.LAUNCHER)
                def late(*_): raise TimeoutError('launcher_props waited on the launcher')
                old = signal.signal(signal.SIGALRM, late)
                signal.alarm(10)
                try:
                    with self.assertRaises((ValueError, OSError)) as raised:
                        m.launcher_props()
                finally:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old)
                self.assertNotIsInstance(raised.exception, TimeoutError)
                if name != 'a link':
                    self.assertIn('is not a plain file', str(raised.exception))
                for wall in (m.sockets_wall, m.userns_wall):
                    with self.assertRaises((ValueError, OSError)):
                        wall()

    def test_words(self):
        props = wsl2.sandbox_props('sandbox() {\n  props=(\n    -p A=1 -p "B=x y"  # -p C=2\n'
                                   '    # -p D=3\n    -p A=\n  )\n}\n-p E=4\n')
        self.assertEqual(props, {'A': ['1', ''], 'B': ['x y']})
        with self.assertRaisesRegex(ValueError, 'no sandbox'):
            wsl2.sandbox_props('props=(-p A=1)\n')

    def test_lists_merge_as_systemd_merges_them(self):   # an empty assignment drops all before it
        for values, want in ((['x86', 'native'], ['x86', 'native']), (['x86', '', 'native'], ['native']),
                             (['native', ''], []), (None, []), (['/run /tmp:size=1', '/var/tmp'],
                                                                ['/run', '/tmp:size=1', '/var/tmp'])):
            with self.subTest(values=values):
                self.assertEqual(wsl2.merged(values), want)

    def test_windows_handlers_by_what_they_run(self):
        cases = [
            ('WSLInterop', None, True), ('windows', 'enabled\ninterpreter /init\nmagic 4d5a\n', True),
            ('x', 'disabled\ninterpreter /usr/bin/x\nmagic 4d5a90\n', True),
            ('x', 'enabled\ninterpreter /init\nmagic 2321\n', True),
            ('x', 'enabled\ninterpreter /mnt/c/tools/run\nmagic 7f454c46\n', True),
            ('x', 'enabled\ninterpreter /usr/bin/x\nextension .EXE\n', True),
            ('x', None, True),   # cannot be read: cannot be told
            ('python3.12', 'enabled\ninterpreter /usr/bin/python3.12\nmagic cb0d0d0a\n', False),
            ('qemu-arm', 'enabled\ninterpreter /usr/bin/qemu-arm-static\nmagic 7f454c46010101\n', False),
        ]
        for name, text, bad in cases:
            with self.subTest(name=name, text=text):
                (self.assertIsNotNone if bad else self.assertIsNone)(wsl2.windows_handler(name, text))


class LinuxFile(Case):
    """wsl2 runs the linux walls as its own code: from their source, only when no more open than wsl2 itself."""
    def test_a_more_open_or_linked_linux_file_is_not_run(self):
        for mode in (0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                p = self.run_wsl2(self.copy(mode), self.fake())
                self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)
                self.assertIn('no more writable than', p.stderr)
        path = self.copy()
        linux = os.path.join(os.path.dirname(path), 'linux')
        os.rename(linux, linux + '.real')
        os.symlink(linux + '.real', linux)
        p = self.run_wsl2(path, self.fake())
        self.assertEqual((p.returncode, p.stdout), (2, ''), p.stderr)

    def test_runs_one_as_open_as_itself(self):
        p = self.run_wsl2(self.copy(0o775, 0o775), self.fake())
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)

    def test_a_cached_pyc_beside_it_never_runs(self):   # what was checked is what runs: the source
        path = self.copy()
        linux = os.path.join(os.path.dirname(path), 'linux')
        evil = slurp(linux).replace('def mounts_wall():\n', 'def mounts_wall():\n    return None\n', 1)
        st = os.stat(linux)
        cache = importlib.util.cache_from_source(linux)
        os.makedirs(os.path.dirname(cache))
        with open(cache, 'wb') as f:
            f.write(_code_to_timestamp_pyc(compile(evil, linux, 'exec'), int(st.st_mtime), st.st_size))
        p = self.run_wsl2(path, self.fake({'etc/fork-fleet/launch.conf': 'ALLOW_PATHS=/c\n'}), 'check')
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertRegex(p.stdout, r'FAIL  mounts  .*/c \(on 9p')

    def test_every_linux_wall_is_a_wall_here(self):   # a wall the linux file gains is reported here too
        text = slurp(os.path.join(FOLDER, 'linux')).replace(
            "WALLS = [\n", "WALLS = [\n    ('extra', 'a new linux wall', lambda: 'it does not hold'),\n", 1)
        p = self.run_wsl2(self.copy(linux_text=text), self.fake())
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertIn('FAIL  extra  a new linux wall: it does not hold', p.stdout)
        ids = [line.split()[1] for line in self.run_wsl2(self.copy(), self.fake()).stdout.splitlines()]
        linux = load('linux')
        self.assertEqual(ids[:len(linux.WALLS)], [w for w, _, _ in linux.WALLS])
        self.assertEqual(sorted(ids), sorted(FIXTURE['fail']))



class HostSettings(Case):
    """The walls fixtures cannot show: links (where /etc/resolv.conf leads) and the other ways user namespaces are off."""
    def walls(self, root, *links):
        for link, target in links:
            full = os.path.join(root, link)
            if os.path.lexists(full):
                os.remove(full)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            os.symlink(target, full)
        return walls_with(self, root)

    def test_resolv_conf_must_not_lead_into_mnt(self):
        stub = {'run/systemd/resolve/stub-resolv.conf': 'nameserver 127.0.0.53\n', 'etc/resolv.conf': None,
                'mnt/wsl/resolv.conf': 'nameserver 172.20.0.1\n'}
        cases = [   # where /etc/resolv.conf leads (WSL's default first) -> holds
            ([('etc/resolv.conf', '/mnt/wsl/resolv.conf')], False),
            ([('etc/resolv.conf', '../mnt/wsl/resolv.conf')], False),
            ([('etc/resolv.conf', 'r2'), ('etc/r2', '/mnt/wsl/resolv.conf')], False),   # through a second link
            ([('etc/resolv.conf', '/run/r/resolv.conf'), ('run/r', '../mnt/wsl')], False),   # a link in a parent
            ([('etc/resolv.conf', '/run/r/stub-resolv.conf'), ('run/r', '../run/systemd/resolve')], True),
            ([('etc/resolv.conf', 'resolv.conf')], False),   # a loop
            ([('etc/resolv.conf', '/run/systemd/resolve/missing')], False),
            ([('etc/resolv.conf', '/run/systemd/resolve')], False),   # a folder, not a file
            ([('etc/resolv.conf', '/run/systemd/resolve/stub-resolv.conf')], True),
            ([('etc/resolv.conf', '../run/systemd/resolve/stub-resolv.conf')], True),
        ]
        for links, holds in cases:
            with self.subTest(links=links):
                root = self.fake(stub)
                m = self.walls(root, *links)
                try:
                    why = m.resolv_wall()
                except ValueError as e:
                    why = str(e)
                (self.assertIsNone if holds else self.assertIsNotNone)(why)

    def test_user_namespaces_off_another_way(self):
        on = {'proc/sys/user/max_user_namespaces': '15000\n'}
        launcher = slurp(LAUNCHER)
        cases = [   # (fake machine, the launcher's text: None the real one) -> holds
            ({}, None, True), (on, None, False),
            ({**on, 'proc/sys/kernel/unprivileged_userns_clone': '0\n'}, None, True),   # Debian's knob
            ({**on, 'proc/sys/kernel/apparmor_restrict_unprivileged_userns': '1\n'}, None, False),   # ways around it
            (on, launcher.replace(ARCH, ARCH + '    -p RestrictNamespaces=no\n'
                '    -p RestrictNamespaces= -p RestrictNamespaces=yes\n'), True),   # an empty one drops the no
        ]
        for value, holds in (('yes', True), ('~user', True), ('"~net user"', True), ('"net mnt"', True),
                             ("'~user net'", True), ('~"net user"', True),
                             ('"net user"', False), ('~net', False), ('no', False)):
            cases.append((on, launcher.replace(ARCH, ARCH + f'    -p RestrictNamespaces={value}\n'), holds))
        for changes, text, holds in cases:
            with self.subTest(changes=changes, text=text and text[text.index(ARCH):][:120]):
                self.assertIn(ARCH, launcher)
                why = walls_with(self, self.fake(changes), text).userns_wall()
                (self.assertIsNone if holds else self.assertIsNotNone)(why)

if __name__ == '__main__':
    unittest.main()
