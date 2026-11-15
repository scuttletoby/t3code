#!/usr/bin/env python3
"""Tests of fork-fleet-walls/wsl2 beyond the contract every walls file passes (fork-fleet-walls.test.py, from
wsl2.fixture.json): that its reading of the launcher's sandbox() fits the real launcher, that it takes every linux
wall from the linux file, and that it runs the linux file only from a source no more open than itself. Run:
python3 <this>. Linux only."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile, unittest
from importlib._bootstrap_external import _code_to_timestamp_pyc

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
JOB_PATHS = 'fork-fleet-job-paths.txt'


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

    def copy(self, linux_mode=0o755, wsl2_mode=0o755, linux_text=None):
        """A walls folder of its own (wsl2, linux, the allow list one folder up): its wsl2's path."""
        kit = self.tmp()
        folder = os.path.join(kit, 'fork-fleet-walls')
        os.mkdir(folder, 0o755)
        shutil.copy(os.path.join(HERE, JOB_PATHS), kit)
        os.chmod(os.path.join(kit, JOB_PATHS), 0o644)
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
            if isinstance(value, dict):
                os.chmod(full, int(value['mode'], 8))
        return root


class Launcher(Case):
    def test_the_real_launcher_holds_the_vsock_and_sockets_walls(self):   # a reworded launcher must not blind them
        root = self.fake({wsl2.LAUNCHER.lstrip('/'): slurp(os.path.join(HERE, 'fork-fleet-launch.sh'))})
        m = load('wsl2', root)
        m.LINUX = load('linux', root)
        m.LINUX.KIT = os.path.dirname(os.path.dirname(self.copy()))   # the allow list as an install has it (0644)
        self.assertIsNone(m.vsock_wall())
        self.assertIsNone(m.sockets_wall())
        props = m.launcher_props()
        self.assertEqual(props.get('NetworkNamespacePath'), ['/run/netns/fork-fleet-$user'])
        self.assertTrue(all(name.isalnum() for name in props), sorted(props))

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
        m = load('wsl2', root)
        m.LINUX = load('linux', root)
        return m

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
        launcher = FIXTURE['pass'][wsl2.LAUNCHER.lstrip('/')]
        arch = '    -p SystemCallArchitectures=native\n'
        cases = [
            ({}, True), (on, False),
            ({**on, 'proc/sys/kernel/unprivileged_userns_clone': '0\n'}, True),   # Debian's knob
            ({**on, 'proc/sys/kernel/apparmor_restrict_unprivileged_userns': '1\n'}, False),   # ways around it
            ({**on, wsl2.LAUNCHER.lstrip('/'): launcher.replace(arch, arch + '    -p RestrictNamespaces=no\n'
                '    -p RestrictNamespaces= -p RestrictNamespaces=yes\n')}, True),   # an empty one drops the no
        ]
        for value, holds in (('yes', True), ('~user', True), ('"~net user"', True), ('"net mnt"', True),
                             ("'~user net'", True), ('~"net user"', True),
                             ('"net user"', False), ('~net', False), ('no', False)):
            cases.append(({**on, wsl2.LAUNCHER.lstrip('/'): launcher.replace(
                arch, arch + f'    -p RestrictNamespaces={value}\n')}, holds))
        for changes, holds in cases:
            with self.subTest(changes=changes):
                why = self.walls(self.fake(changes)).userns_wall()
                (self.assertIsNone if holds else self.assertIsNotNone)(why)

if __name__ == '__main__':
    unittest.main()
