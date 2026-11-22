"""pc-agents on a plain Linux box (fake_distro.py, platform "linux"; issue #1027): one setup run serves, a second
changes nothing, a run stopped at any write finishes on the next; no WSL step is taken (no wsl.conf, no WSL mask, the
host's own resolv.conf and cron, at and ssh left alone) and every other wall holds, with the linux walls of the
fleet; the owner's own login users are fine, a hand-made agent user is kept and checked, never widened; sudo rules:
only an allow-listed helper (both kinds of machine); a root Docker socket the agent could reach fails; an old machine
file (no "platform") is read as wsl2, as before.

  python3 -I -B fork-kit/pc-agents/tests/linux.test.py
"""
import os, shutil, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, LINUX_CONFIG, SHA1, Case, cli, k  # noqa: E402

UNITS = 'enable --now t3code-agents.service pc-agents-walls.timer pc-agents-update.timer'
# The sandbox lines that differ between the two kinds; every other line is the same on both
PER_PLATFORM = ('ProtectHome=', 'ReadWritePaths=', 'BindPaths=', 'BindReadOnlyPaths=', 'InaccessiblePaths=')
HELPER = '/usr/local/sbin/agent-helper'


class Linux(Case):
    platform = 'linux'


class Setup(Linux):
    def test_one_run_serves(self):
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertNotIn('FAIL', out)
        for wsl in ('wsl --terminate', 'wsl.conf', 'Windows'):   # no WSL step (the fleet's own os wall says "not wsl")
            self.assertNotIn(wsl, out)
        self.assertIn(UNITS, self.log())
        self.assertIn('versions/1.2.3/t3 serve', self.read('/log/units/t3code-agents.service.active'))
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))
        self.assertEqual(self.check()[0], 0)

    def test_second_run_changes_nothing(self):
        self.ready()
        before = self.same_files()
        self.write('/log/systemctl', '')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.same_files(), before)
        self.assertNotIn('restart t3code-agents.service', self.log())   # nothing T3 runs with changed

    def test_no_wsl_step(self):
        self.ready()
        for p in ('/etc/wsl.conf', '/etc/binfmt.d/WSLInterop.conf') + tuple(
                '/etc/systemd/system/' + u for u in ('cron.service', 'atd.service', 'ssh.service', 'ssh.socket')):
            self.assertFalse(os.path.lexists(self.path(p)), p)
        self.assertEqual(os.readlink(self.path('/etc/systemd/system/user@1100.service')), '/dev/null')
        # the host's resolver is the owner's: T3's unit alone gets the machine file's nameservers
        self.assertEqual(os.readlink(self.path('/etc/resolv.conf')), '../run/systemd/resolve/stub-resolv.conf')
        self.assertEqual(self.read(k.STATE + '/resolv.conf'), k.HEAD + 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n')

    def test_what_the_unit_says(self):
        self.ready()
        unit = self.read('/etc/systemd/system/t3code-agents.service')
        for line in ('ProtectHome=tmpfs', 'BindPaths=/home/agent',
                     'BindReadOnlyPaths=/etc/pc-agents/resolv.conf:/etc/resolv.conf',
                     'InaccessiblePaths=-/mnt -/media -/run/media -/tmp/.X11-unix -/run/tailscale -/run/docker.sock '
                     '-/run/containerd'):
            self.assertIn('\n' + line + '\n', unit)
        self.assertNotIn('ProtectHome=read-only', unit)
        self.assertNotIn('WSL', unit)
        linux, wsl2 = (k.sandbox({**k.config(), 'platform': p}) for p in ('linux', 'wsl2'))
        self.assertEqual([x for x in linux if not x.startswith(PER_PLATFORM)],
                         [x for x in wsl2 if not x.startswith(PER_PLATFORM)])   # caps, firewall, syscalls: the same

    def test_ssh_refuses_the_agent(self):
        """No ssh mask on the owner's box: sshd refuses the agent instead (a key it wrote, a port forward)."""
        self.ready()
        self.assertEqual(self.read('/etc/ssh/sshd_config.d/pc-agents.conf'), k.HEAD + 'DenyUsers agent\n')
        self.assertIn('try-reload-or-restart ssh.service', self.log())
        self.write('/log/systemctl', '')
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('try-reload-or-restart ssh.service', self.log())   # every run: one stopped before it too

    def test_owner_users_are_fine(self):
        self.write('/etc/passwd', self.read('/etc/passwd') + 'second:x:1001:1001::/home/second:/bin/zsh\n')
        self.ready()
        self.assertEqual(self.check()[0], 0)

    def test_as_agent(self):
        self.ready()
        code, out = self.call(cli.main, ['as-agent', '--', 'claude', 'login'])
        self.assertEqual(code, 0, out)
        argv = self.log('systemd-run')
        for line in ('ProtectHome=tmpfs', 'BindPaths=/home/agent',
                     'BindReadOnlyPaths=/etc/pc-agents/resolv.conf:/etc/resolv.conf'):
            self.assertIn('--property=' + line, argv)

    def test_stopped_half_way_then_run_again(self):
        self.no_watch()
        self.same_fleet_walls()
        self.assertEqual(self.setup_run()[0], 0)
        whole = self.same_files()
        real_put, writes = cli.put, []

        def counting(*a, **kw):
            writes.append(a[0])
            return real_put(*a, **kw)
        cli.put = counting
        self.addCleanup(setattr, cli, 'put', real_put)
        shutil.rmtree(self.root)
        self.setUp()
        self.setup_run()
        total = len(writes)
        self.assertGreater(total, 20)
        for stop in range(1, total + 1):
            shutil.rmtree(self.root)
            self.setUp()

            def stopping(*a, n=[0], **kw):
                n[0] += 1
                if n[0] == stop:
                    raise KeyboardInterrupt
                return real_put(*a, **kw)
            cli.put = stopping
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run()
            cli.put = real_put
            self.write('/log/systemctl', '')
            code, out = self.setup_run()
            self.assertEqual(code, 0, f'stopped at write {stop} ({writes[stop - 1]}): {out}')
            self.assertEqual(self.same_files(), whole, f'stopped at write {stop}')
            self.assertIn(UNITS, self.log())
            self.assertIn('try-reload-or-restart ssh.service', self.log())


class HandMadeAgent(Linux):
    """An agent user the owner made before the kit: kept and checked, never widened."""
    def agent(self, line, groups=''):
        self.write('/etc/passwd', self.read('/etc/passwd') + line + '\n')
        self.write('/etc/group', self.read('/etc/group').replace('sudo:x:27:owner', f'sudo:x:27:owner{groups}') +
                   'agent:x:1100:\n')
        os.makedirs(self.path('/home/agent'), exist_ok=True)

    def test_kept_and_its_shell_narrowed(self):
        self.agent('agent:x:1100:1100:Agents:/home/agent:/bin/bash')
        self.ready()
        self.assertEqual([x for x in self.read('/etc/passwd').splitlines() if x.startswith('agent:')],
                         ['agent:x:1100:1100:Agents:/home/agent:/usr/sbin/nologin'])
        self.assertEqual(self.log('usermod'), ['--shell /usr/sbin/nologin agent'])
        self.assertEqual(self.check()[0], 0)

    def test_refused_when_not_the_kits(self):
        for line, groups, why in (('agent:x:1001:1001::/home/agent:/bin/bash', '', 'uid 1001, not 1100'),
                                  ('agent:x:1100:1100::/home/agent:/bin/bash', ',agent', 'other groups (sudo)'),
                                  ('agent:x:1100:1100::/srv/agent:/bin/bash', '', 'home is /srv/agent'),
                                  ('agent:x:1100:1000::/home/agent:/bin/bash', '', 'primary group')):
            with self.subTest(line=line):
                shutil.rmtree(self.root)
                self.setUp()
                self.agent(line, groups)
                before = self.same_files()
                code, out = self.setup_run()
                self.assertEqual(code, 1, out)
                self.assertIn(why, out)
                lock = ('/run/pc-agents', k.LOCK)   # setup's lock, taken before it looks
                self.assertEqual(self.same_files(lock), before)   # refused, nothing changed: not widened, not narrowed


class LinuxWalls(Linux):
    def setUp(self):
        super().setUp()
        self.walled()

    def fails(self, wall, *also):
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertEqual({line.split()[1] for line in out.splitlines() if line.startswith('FAIL')}, {wall, *also}, out)
        return out

    def test_all_hold(self):
        code, out = self.check()
        self.assertEqual(code, 0, out)
        walls = [line.split()[1] for line in out.splitlines()]
        for wall in ('linux:os', 'linux:kernel', 'linux:systemd', 'linux:cgroup-bpf', 'linux:container', 'platform',
                     'masks', 'record', 'linger', 'user-units', 'drop-ins', 'units', 'agent', 'sudoers', 'docker',
                     'ssh', 'restart', 'kit-code', 'runtime', 'firewall', 'gateway', 'file:resolv.conf',
                     'file:pc-agents.conf', 'file:policy.json', 'file:t3code-agents.service', 'file:firewall.nft'):
            self.assertIn(wall, walls)
        for wall in ('automount', 'file:wsl.conf', 'linux:mounts'):   # WSL's; the launcher's job view
            self.assertNotIn(wall, walls)
        self.assertFalse([w for w in walls if w.startswith('wsl2:')])

    def test_each_wall(self):
        def edit(p, old, new):
            return lambda: self.write(p, self.read(p).replace(old, new))
        cases = {
            'masks': lambda: os.unlink(self.path('/etc/systemd/system/user@1100.service')),
            'agent': edit('/etc/group', 'owner:x:1000:', 'owner:x:1000:agent'),
            'gateway': lambda: self.write('/proc/net/route', 'Iface\tDestination\tGateway\nx\t00000000\t08080808\n'),
            'file:resolv.conf': edit(k.STATE + '/resolv.conf', '9.9.9.9', '192.168.1.1'),
            'file:pc-agents.conf': edit('/etc/ssh/sshd_config.d/pc-agents.conf', 'DenyUsers', '#DenyUsers'),
            'docker': lambda: self.write('/run/docker.sock', {'text': '', 'mode': '666'}),
            'ssh': lambda: self.write('/etc/ssh/sshd_config', 'PermitRootLogin no\n'),
            'sudoers': lambda: self.write('/etc/sudoers.d/90-agent', 'agent ALL=(ALL) NOPASSWD: ALL\n'),
            'firewall': lambda: os.unlink(self.path('/log/nft-loaded')),
            'linux:kernel': lambda: self.write('/proc/sys/kernel/osrelease', '5.4.0-1-generic\n'),
        }
        clean = self.same_files()
        for wall, change in cases.items():
            with self.subTest(wall=wall):
                shutil.rmtree(self.root)
                self.setUp()
                self.assertEqual(self.same_files(), clean)
                change()
                self.fails(wall)

    def test_docker_socket(self):
        self.write('/run/docker.sock', {'text': '', 'mode': '660'})   # root:docker 0660, the agent in no group
        self.write('/run/containerd/containerd.sock', {'text': '', 'mode': '600'})
        self.assertEqual(self.check()[0], 0)
        self.write('/run/containerd/containerd.sock', {'text': '', 'mode': '602'})
        self.assertIn('/run/containerd/containerd.sock', self.fails('docker'))

    def test_sshd_reads_the_kits_drop_in(self):
        self.write('/etc/ssh/sshd_config', 'Include /etc/ssh/sshd_config.d/*.conf\nKbdInteractiveAuthentication no\n'
                   'Match User backup\n  ForceCommand /usr/local/bin/backup\n')
        self.assertEqual(self.check()[0], 0)
        self.write('/etc/ssh/sshd_config', 'Match User backup\n  ForceCommand /usr/local/bin/backup\n'
                   'Include /etc/ssh/sshd_config.d/*.conf\n')   # read only for that one user
        self.fails('ssh')

    def test_wsl2_machine_file_on_plain_linux(self):
        """The machine file says wsl2 (or says nothing: an old one) on a plain Linux box: platform fails."""
        for section in ({**CONFIG, 'platform': 'wsl2'}, CONFIG):
            with self.subTest(section=section):
                self.machine(section)
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn('FAIL  platform  ', out)
                self.assertIn('this machine is linux', out)


class Helpers:
    """A sudo rule for the agent passes only as `agent ALL=(root) NOPASSWD: <helpers>`, each an allow-listed helper
    (the machine file's sudoHelpers, word for word) that is root's alone. Anything wider fails, on both kinds."""
    def setUp(self):
        super().setUp()
        self.walled()
        self.write(HELPER, {'text': '#!/bin/sh\n', 'mode': '755'})
        self.machine({**(LINUX_CONFIG if self.platform == 'linux' else CONFIG),
                      'sudoHelpers': [HELPER, '/usr/local/sbin/agent-gpu on']})
        self.write('/usr/local/sbin/agent-gpu', {'text': '#!/bin/sh\n', 'mode': '755'})

    def rule(self, text, holds):
        self.write('/etc/sudoers.d/50-agent', text + '\n')
        code, out = self.check()
        self.assertEqual(code, 0 if holds else 1, out)
        self.assertEqual({line.split()[1] for line in out.splitlines() if line.startswith('FAIL')},
                         set() if holds else {'sudoers'}, out)

    def test_allow_listed(self):
        for text in (f'agent ALL=(root) NOPASSWD: {HELPER}', f'agent ALL = (root) NOPASSWD: {HELPER}, '
                     '/usr/local/sbin/agent-gpu on', 'agent ALL=(root) NOPASSWD: /usr/local/sbin/agent-gpu  on'):
            with self.subTest(text=text):
                self.rule(text, True)

    def test_wider_fails(self):
        for text in (f'agent ALL=(ALL) NOPASSWD: {HELPER}', f'agent ALL=(root:root) NOPASSWD: {HELPER}',
                     f'agent ALL=(root) {HELPER}', f'agent ALL=(root) NOPASSWD:SETENV: {HELPER}',
                     f'agent ALL=(root) NOPASSWD: {HELPER}, /bin/sh', f'agent ALL=(root) NOPASSWD: {HELPER} *',
                     f'agent ALL=(root) NOPASSWD: {HELPER}*', 'agent ALL=(root) NOPASSWD: /usr/local/sbin/agent-gpu',
                     'agent ALL=(root) NOPASSWD: /usr/local/sbin/agent-gpu off', f'agent box=(root) NOPASSWD: {HELPER}',
                     f'%agent ALL=(root) NOPASSWD: {HELPER}', f'#1100 ALL=(root) NOPASSWD: {HELPER}',
                     f'agent,bob ALL=(root) NOPASSWD: {HELPER}', f'ALL ALL=(root) NOPASSWD: {HELPER}',
                     f'agent ALL=(root) NOPASSWD: {HELPER}\nDefaults:agent env_keep += "LD_PRELOAD"',
                     f'agent ALL=(root) NOPASSWD: {HELPER}\nDefaults!{HELPER} !env_reset',
                     f'agent ALL=(root) NOPASSWD: {HELPER} : box = (root) /bin/sh'):
            with self.subTest(text=text):
                self.rule(text, False)

    def test_helper_not_roots_alone(self):
        for change in (lambda: os.chmod(self.path(HELPER), 0o775), lambda: os.unlink(self.path(HELPER)),
                       lambda: os.chmod(self.path('/usr/local/sbin'), 0o777)):
            with self.subTest(change=change):
                os.chmod(self.path('/usr/local/sbin'), 0o755)
                self.write(HELPER, {'text': '#!/bin/sh\n', 'mode': '755'})
                change()
                self.rule(f'agent ALL=(root) NOPASSWD: {HELPER}', False)

    def test_not_listed(self):
        self.machine(LINUX_CONFIG if self.platform == 'linux' else CONFIG)
        self.rule(f'agent ALL=(root) NOPASSWD: {HELPER}', False)


class LinuxHelpers(Helpers, Linux):
    pass


class Wsl2Helpers(Helpers, Case):
    pass


class OldMachineFile(Case):
    """A machine file from before the platform key (WSL2 only) reads as wsl2: the same files, nothing restarts."""
    def test_read_as_wsl2(self):
        self.assertEqual(k.config()['platform'], 'wsl2')
        old = k.render(k.config())
        self.machine({**CONFIG, 'platform': 'wsl2'})
        self.assertEqual(k.render(k.config()), old)

    def test_set_up_then_key_added(self):
        self.ready()
        before = self.same_files(skip=('/etc/fork-kit/machine.json',))
        self.write('/log/systemctl', '')
        self.machine({**CONFIG, 'platform': 'wsl2'})
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.same_files(skip=('/etc/fork-kit/machine.json',)), before)
        self.assertNotIn('restart t3code-agents.service', self.log())

    def test_wsl2_walls_unchanged(self):
        self.walled()
        code, out = self.check()
        self.assertEqual(code, 0, out)
        walls = [line.split()[1] for line in out.splitlines()]
        for wall in ('wsl2:os', 'wsl2:interop', 'automount', 'file:wsl.conf', 'file:resolv.conf', 'platform'):
            self.assertIn(wall, walls)
        self.assertNotIn('ssh', walls)
        self.assertNotIn('file:pc-agents.conf', walls)


class BadConfig(Case):
    def test_refused(self):
        for change, why in (({'platform': 'windows'}, 'platform: wsl2 or linux'),
                            ({'platform': 'Linux'}, 'platform: wsl2 or linux'),
                            ({'sudoHelpers': 'x'}, 'sudoHelpers'),
                            ({'sudoHelpers': ['agent-helper']}, 'an absolute path'),
                            ({'sudoHelpers': [HELPER + ' *']}, 'an absolute path'),
                            ({'sudoHelpers': ['/usr/bin/../bin/sh']}, 'an absolute path'),
                            ({'sudoHelpers': [HELPER + ' ALL']}, 'an absolute path'),
                            ({'sudoHelpers': [HELPER + ', /bin/sh']}, 'an absolute path'),
                            ({'sudoHelpers': [f'/usr/local/sbin/h{i}' for i in range(9)]}, 'at most 8')):
            with self.subTest(change=change):
                self.machine({**CONFIG, **change})
                code, out = self.setup_run()
                self.assertEqual(code, 2, out)
                self.assertIn(why, out)
        self.assertFalse(os.path.lexists(self.path('/etc/t3code/policy.json')))


if __name__ == '__main__':
    unittest.main()
