"""pc-agents check on a fake WSL2 distro (fake_distro.py): every wall passes on a walled distro, and each one fails
on its own when its part of the wall is taken away; --enforce stops T3.

  python3 -I -B fork-kit/pc-agents/tests/check.test.py
"""
import os, shutil, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import Case, DRVFS, SHA1, k  # noqa: E402


class Walls(Case):
    def setUp(self):
        super().setUp()
        self.walled()

    def fails(self, wall, *also):
        """check fails on wall, and on no other wall but those in also (which the same change takes away)."""
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertEqual({line.split()[1] for line in out.splitlines() if line.startswith('FAIL')}, {wall, *also}, out)

    def test_all_hold(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        walls = [line.split()[1] for line in out.splitlines()]
        for wall in ('wsl2:interop', 'wsl2:resolv', 'wsl2:windows-path', 'wsl2:networking', 'wsl2:os', 'masks',
                     'drop-ins', 'agent', 'kit-code', 'automount', 'firewall', 'gateway', 'file:policy.json',
                     'file:t3code-agents.service', 'file:firewall.nft', 'file:wsl.conf'):
            self.assertIn(wall, walls)
        for wall in k.UNIT_WALLS:   # the launcher's job units: not T3's unit
            self.assertNotIn('wsl2:' + wall, walls)

    def test_enforce_stops_t3(self):
        os.unlink(self.path('/log/nft-loaded'))
        code, out = self.check('--enforce')
        self.assertEqual(code, 1, out)
        self.assertIn('stop t3code-agents.service', self.log())

    def test_each_wall(self):
        def edit(p, old, new):
            return lambda: self.write(p, self.read(p).replace(old, new))
        cases = {
            'file:policy.json': edit('/etc/t3code/policy.json', '"full-access"', '"auto"'),
            'file:t3code-agents.service': lambda: os.chmod(self.path('/etc/systemd/system/t3code-agents.service'),
                                                           0o666),
            'file:resolv.conf': lambda: os.unlink(self.path('/etc/resolv.conf')),
            'masks': lambda: os.unlink(self.path('/etc/systemd/system/ssh.socket')),
            'drop-ins': lambda: os.makedirs(self.path('/etc/systemd/system/t3code-agents.service.d')),
            'agent': edit('/etc/group', 'sudo:x:27:', 'sudo:x:27:agent'),
            'kit-code': lambda: os.chmod(self.path(f'/opt/pc-agents/{SHA1}/lib/pc_agents.py'), 0o666),
            'automount': lambda: self.write('/proc/self/mountinfo', self.read('/proc/self/mountinfo') + DRVFS),
            'firewall': lambda: os.unlink(self.path('/log/nft-loaded')),
            'gateway': lambda: self.write('/proc/net/route', 'Iface\tDestination\tGateway\nx\t00000000\t08080808\n'),
            'wsl2:interop': lambda: self.write('/proc/sys/fs/binfmt_misc/WSLInterop',
                                               'enabled\ninterpreter /init\nflags: PF\noffset 0\nmagic 4d5a\n'),
            'wsl2:networking': edit('/usr/bin/wslinfo', 'echo nat', 'echo mirrored'),
        }
        clean = {p: v[4] for p, v in self.snapshot().items()}
        for wall, change in cases.items():
            with self.subTest(wall=wall):
                shutil.rmtree(self.root)
                self.setUp()
                self.assertEqual({p: v[4] for p, v in self.snapshot().items()}, clean)
                change()
                self.fails(wall, *(['wsl2:resolv'] if wall == 'file:resolv.conf' else []))

    def test_no_config_fails(self):
        os.unlink(self.path('/etc/fork-kit/machine.json'))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('FAIL  check  ', out)


if __name__ == '__main__':
    unittest.main()
