"""pc-agents check on a fake WSL2 distro (fake_distro.py): every wall passes on a walled distro, and each one fails
on its own when its part of the wall is taken away; --enforce stops T3.

  python3 -I -B fork-kit/pc-agents/tests/check.test.py
"""
import os, shutil, subprocess, sys, time, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, Case, DRVFS, SHA1, k  # noqa: E402


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
                     'record', 'linger', 'user-units', 'drop-ins', 'units', 'agent', 'kit-code', 'runtime',
                     'automount', 'firewall', 'gateway', 'file:policy.json', 'file:t3code-agents.service',
                     'file:firewall.nft', 'file:wsl.conf', 'file:pc-agents-walls.timer'):
            self.assertIn(wall, walls)
        for wall in k.UNIT_WALLS:   # the launcher's job units: not T3's unit
            self.assertNotIn('wsl2:' + wall, walls)

    def test_enforce_stops_t3(self):
        os.unlink(self.path('/log/nft-loaded'))
        code, out = self.check('--enforce')
        self.assertEqual(code, 1, out)
        self.assertIn('stop t3code-agents.service', self.log())
        self.assertFalse(os.path.exists(self.path('/log/units/t3code-agents.service.active')))
        self.assertIn('stopped t3code-agents.service', out)

    def test_enforce_says_when_the_stop_fails(self):
        os.unlink(self.path('/log/nft-loaded'))
        self.write('/log/systemctl-fail', '')
        code, out = self.check('--enforce')
        self.assertEqual(code, 1, out)
        self.assertIn('stopping t3code-agents.service FAILED', out)

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
            'record': lambda: os.chmod(self.path(k.RECORD), 0o666),
            'linger': lambda: self.write('/var/lib/systemd/linger/agent', ''),
            'user-units': lambda: self.write('/home/agent/.config/systemd/user/x.service', '[Service]\n'),
            'units': lambda: self.write('/log/units/t3code-agents.service.dropins', '/run/systemd/system/x.conf'),
            'runtime': lambda: os.chmod(self.path(k.runtime(CONFIG) + '/t3'), 0o777),
            'file:pc-agents-walls.timer': edit('/etc/systemd/system/pc-agents-walls.timer', '2min', '1d'),
        }
        clean = {p: v[4] for p, v in self.snapshot().items()}
        for wall, change in cases.items():
            with self.subTest(wall=wall):
                shutil.rmtree(self.root)
                self.setUp()
                self.assertEqual({p: v[4] for p, v in self.snapshot().items()}, clean)
                change()
                self.fails(wall, *(['wsl2:resolv'] if wall == 'file:resolv.conf' else []))

    def again(self, **section):
        """machine.json changed and set up again: the new files, recorded, their firewall loaded."""
        self.machine({**CONFIG, **section})
        self.files()
        self.assertIsNone(k.load_firewall())

    def test_agent_user_manager_masked(self):
        os.unlink(self.path('/etc/systemd/system/user@1100.service'))
        self.fails('masks')

    def test_unit_copies_and_prefix_drop_ins(self):
        for p in ('/usr/lib/systemd/system/t3code-agents.service', '/run/systemd/generator.late/pc-agents-walls.timer',
                  '/etc/systemd/system/t3code-.service.d/', '/etc/systemd/system/pc-agents-.timer.d/',
                  '/run/systemd/system.attached/t3code-agents.service.d/', '/etc/systemd/system/service.d/'):
            with self.subTest(path=p):
                self.write(p, '[Service]\n')
                self.fails('drop-ins')
                (os.rmdir if p.endswith('/') else os.unlink)(self.path(p.rstrip('/')))

    def test_units_as_systemd_loaded_them(self):
        for name, change in (('timer disabled', lambda: os.unlink(self.path('/log/units/pc-agents-walls.timer.enabled'))),
                             ('T3 disabled', lambda: os.unlink(self.path('/log/units/t3code-agents.service.enabled'))),
                             ('not reloaded', lambda: self.write('/log/units/reload', '')),
                             ('drop-in', lambda: self.write('/log/units/pc-agents-walls.service.dropins', '/x.conf'))):
            with self.subTest(name):
                shutil.rmtree(self.root)
                self.setUp()
                change()
                self.fails('units')

    def test_firewall_changed_after_it_was_loaded(self):
        self.write('/log/nft-loaded', self.read('/log/nft-loaded').replace('accept', 'accept\n', 1))
        self.fails('firewall')

    def test_firewall_file_newer_than_the_loaded_table(self):
        self.machine({**CONFIG, 'allow': ['192.168.1.0/24']})
        self.files()
        self.fails('firewall')
        self.assertIsNone(k.load_firewall())
        self.assertEqual(self.check()[0], 0)

    def test_allow_or_peers_covering_the_gateway(self):
        for key in ('allow', 'peers'):
            with self.subTest(key):
                self.again(**{key: ['172.16.0.0/12']})
                self.fails('gateway')
        self.again()
        self.assertEqual(self.check()[0], 0)

    def test_runtime_changed_or_missing(self):
        t3 = k.runtime(CONFIG) + '/t3'
        self.write(t3, {'text': '#!/bin/sh\nexec /home/agent/x\n', 'mode': '755'})
        self.fails('runtime')
        os.chmod(self.path('/opt/pc-agents/t3/runtime'), 0o777)
        self.fails('runtime')
        shutil.rmtree(self.path(k.runtime(CONFIG)))   # not installed: T3 cannot start, nothing to wall
        os.chmod(self.path('/opt/pc-agents/t3/runtime'), 0o755)
        code, out = self.check()
        self.assertEqual(code, 0, out)

    def test_kit_code_folders(self):
        os.chmod(self.path('/opt/pc-agents'), 0o775)
        self.fails('kit-code')
        os.chmod(self.path('/opt/pc-agents'), 0o755)
        os.symlink('/etc', self.path(f'/opt/pc-agents/{SHA1}/lib/etc'))
        self.fails('kit-code')

    def test_automount_only_under_usr_lib_wsl(self):
        self.write('/proc/self/mountinfo', self.read('/proc/self/mountinfo') +
                   '45 29 0:45 / /usr/lib/wslfoo rw - 9p drvfs rw\n')
        self.fails('automount')

    def test_no_config_fails(self):
        os.unlink(self.path('/etc/fork-kit/machine.json'))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('FAIL  check  ', out)


class Holes(Case):
    """Ways the agent could run code outside T3's walled unit, or the walls be changed under systemd."""
    def setUp(self):
        super().setUp()
        self.walled()

    def test_agent_user_unit(self):
        self.write('/home/agent/.config/systemd/user/x.service', '[Service]\nExecStart=/home/agent/x\n')
        os.makedirs(self.path('/home/agent/.config/systemd/user/default.target.wants'))
        os.symlink('../x.service', self.path('/home/agent/.config/systemd/user/default.target.wants/x.service'))
        self.write('/var/lib/systemd/linger/agent', '')
        self.assertEqual(self.check()[0], 1)

    def test_system_control_drop_in(self):
        self.write('/etc/systemd/system.control/t3code-agents.service.d/50-x.conf', '[Service]\nRestrictAddressFamilies=\n')
        self.assertEqual(self.check()[0], 1)

    def test_timer_drop_in(self):
        self.write('/etc/systemd/system/pc-agents-walls.timer.d/off.conf', '[Timer]\nOnCalendar=\n')
        self.assertEqual(self.check()[0], 1)

    def test_prefix_drop_in(self):
        self.write('/etc/systemd/system/t3code-.service.d/x.conf', '[Service]\nUser=root\n')
        self.assertEqual(self.check()[0], 1)


class Crash(Case):
    """A check that cannot run, or crashes, still stops T3 (the walls service's ExecStopPost)."""
    def setUp(self):
        super().setUp()
        self.walled()

    def stop_post(self, result):
        line = next(x for x in k.render(k.config())['/etc/systemd/system/pc-agents-walls.service'][0].splitlines()
                    if x.startswith('ExecStopPost='))
        cmd = line.split('=', 1)[1].replace('$$', '$').replace('/usr/bin/systemctl', self.path('/usr/bin/systemctl'))
        subprocess.run(cmd, shell=True, check=True, env={'SERVICE_RESULT': result, 'PATH': '/usr/bin:/bin'})

    def test_missing_lib_stops_t3(self):
        os.unlink(self.path(f'/opt/pc-agents/{SHA1}/lib/pc_agents.py'))
        p = subprocess.run([sys.executable, '-I', self.path(f'/opt/pc-agents/{SHA1}/sbin/pc-agents'), 'check',
                            '--enforce'], env={'PC_AGENTS_ROOT': self.root, 'PATH': '/usr/bin:/bin'},
                           capture_output=True, text=True, timeout=60)
        self.assertNotEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn('stop t3code-agents.service', self.log())   # the check itself could not stop T3
        self.stop_post('exit-code')
        self.assertIn('stop t3code-agents.service', self.log())
        self.assertFalse(os.path.exists(self.path('/log/units/t3code-agents.service.active')))

    def test_success_does_not_stop_t3(self):
        self.stop_post('success')
        self.assertEqual(self.log(), [])


class Unit(Case):
    """What the kit's files say: things no wall can see from outside the unit."""
    def test_unit(self):
        files = k.render(k.config())
        unit = files['/etc/systemd/system/t3code-agents.service'][0]
        self.assertIn('\nExecStart=/opt/pc-agents/t3/runtime/versions/1.2.3/t3 serve\n', unit)
        self.assertIn(' -/run/tailscale', unit)
        deny = next(x for x in unit.splitlines() if x.startswith('IPAddressDeny=')).split('=', 1)[1].split()
        self.assertEqual(set(deny), set(k.BLOCKED4 + k.BLOCKED6))
        self.assertIn('\nIPAddressAllow=100.101.102.103/32\n', unit)
        self.assertIn('\nStartLimitBurst=5\n', unit)
        self.assertIn('\n[user]\ndefault=root\n', files['/etc/wsl.conf'][0])
        self.assertIn('\nOnUnitActiveSec=2min\n', files['/etc/systemd/system/pc-agents-walls.timer'][0])

    def test_config(self):
        # The kit passes these; T3 itself tells a complete object from a partial one and does not start on a partial.
        ok = {'agentSlots': {'maxPerMachine': 2}, 'providerInstances.codex-work': {'driver': 'codex'},
              'agentSlots.maxPerMachine': 2, 'providerInstances': {}}
        self.assertEqual([p for p, v in ok.items() if k.locked_problem(p, v)], [])
        bad = {'providerInstances.codex-work.enabled': True, 'agentSlots.a.b': {'x': 1}, 'automaticUpdates': True,
               'linkedEnvironmentsEnabled.x': 1, 'Agent': 1, 'a..b': 1}
        self.assertEqual([p for p, v in bad.items() if not k.locked_problem(p, v)], [])
        for section in ({**CONFIG, 'organization': '   '}, {**CONFIG, 'peers': []},
                        {key: v for key, v in CONFIG.items() if key != 'peers'}):
            with self.subTest(section=section):
                self.machine(section)
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn('FAIL  check  ', out)


class Run(Case):
    def test_timeout_kills_the_whole_group(self):
        self.write('/x', {'text': f'#!/bin/sh\nsleep 60 &\necho $! >{self.root}/pid\nwait\n', 'mode': '755'})
        r = k.run(['/x'], timeout=1)
        self.assertEqual(r.returncode, 124)
        self.assertIn('took more than 1 s', r.stderr)
        pid = int(self.read('/pid'))
        for _ in range(50):
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail(f'sleep {pid} still runs')


if __name__ == '__main__':
    unittest.main()
