#!/usr/bin/env python3
"""Tests of fork-fleet-walls/wsl2 beyond the contract every walls file passes (fork-fleet-walls.test.py, from
wsl2.fixture.json): that its reading of the launcher's sandbox() fits the real launcher, and that it loads the linux
walls only from a file no more open than itself. Run: python3 <this>. Linux only."""
import importlib.machinery, importlib.util, os, shutil, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
FOLDER = os.path.join(HERE, 'fork-fleet-walls')
_loader = importlib.machinery.SourceFileLoader('fork_fleet_walls_wsl2', os.path.join(FOLDER, 'wsl2'))
wsl2 = importlib.util.module_from_spec(importlib.util.spec_from_loader(_loader.name, _loader))
_loader.exec_module(wsl2)


def slurp(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


class Launcher(unittest.TestCase):
    def test_reads_the_real_launchers_sandbox(self):   # a reworded launcher must not leave the vsock wall blind
        props = wsl2.sandbox_props(slurp(os.path.join(HERE, 'fork-fleet-launch.sh')))
        self.assertEqual(props.get('NetworkNamespacePath'), ['/run/netns/fork-fleet-$user'])
        self.assertIn('ProtectSystem', props)
        self.assertTrue(all(name.isalnum() for name in props), sorted(props))

    def test_words(self):
        props = wsl2.sandbox_props('sandbox() {\n  props=(\n    -p A=1 -p "B=x y"  # -p C=2\n'
                                   '    # -p D=3\n    -p A=\n  )\n}\n-p E=4\n')
        self.assertEqual(props, {'A': ['1', ''], 'B': ['x y']})
        with self.assertRaisesRegex(ValueError, 'no sandbox'):
            wsl2.sandbox_props('props=(-p A=1)\n')


class LinuxFile(unittest.TestCase):
    """wsl2 runs the linux walls as its own code: a linux file more open than wsl2 itself is not run."""
    def answer(self, linux_mode, wsl2_mode=0o755):
        tmp = tempfile.mkdtemp(prefix='fork-fleet-walls-wsl2-test-')
        self.addCleanup(shutil.rmtree, tmp)
        for name, mode in (('linux', linux_mode), ('wsl2', wsl2_mode)):
            shutil.copy(os.path.join(FOLDER, name), tmp)
            os.chmod(os.path.join(tmp, name), mode)
        p = subprocess.run([os.path.join(tmp, 'wsl2'), 'describe'], capture_output=True, text=True, timeout=120,
                           stdin=subprocess.DEVNULL, env={'PATH': '/usr/bin:/bin', 'LC_ALL': 'C',
                                                          'FORK_FLEET_WALLS_ROOT': tmp + '/none'})
        self.assertEqual(p.returncode, 1, p.stderr)   # an empty machine
        return p.stdout

    def test_refuses_a_more_open_linux_file(self):
        for mode in (0o775, 0o757):
            with self.subTest(mode=oct(mode)):
                out = self.answer(mode)
                for wall in ('kernel', 'systemd', 'cgroup2', 'seccomp', 'container', 'mounts'):
                    self.assertRegex(out, f'FAIL  {wall}  .*no more writable than')

    def test_runs_one_as_open_as_itself(self):
        out = self.answer(0o775, 0o775)
        self.assertIn('cannot read the kernel version', out)
        self.assertNotIn('no more writable than', out)


if __name__ == '__main__':
    unittest.main()
