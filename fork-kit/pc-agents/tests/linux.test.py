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


# T3's unit on a plain Linux box as fake_distro's LINUX_CONFIG gives it, before the `gpu` setting reached linux
# (#1027 item 3): with no setting (or "gpu": false) it stays byte for byte this one.
LINUX_UNIT_SHA = '6cb2a94d01fbb9b59c0b4c614c297a18591e6b236e4e8ee74a9f27e821519141'
UNIT = '/etc/systemd/system/t3code-agents.service'
CARDS = tuple(f'/dev/nvidia{i}' for i in range(16))
NVIDIA = CARDS + ('/dev/nvidiactl', '/dev/nvidia-modeset', '/dev/nvidia-uvm', '/dev/nvidia-uvm-tools')
# What `gpu` on adds to the linux unit, and nothing else: NVIDIA's device nodes, each skipped when missing ("-")
LINUX_GPU = ['BindPaths=' + ' '.join('-' + p for p in NVIDIA + ('/dev/nvidia-caps',)),
             *(f'DeviceAllow={p} rw' for p in NVIDIA), 'DeviceAllow=char-nvidia-caps rw']
DRIVER = ('/dev/nvidia0', '/dev/nvidiactl', '/dev/nvidia-uvm', '/dev/nvidia-uvm-tools', '/dev/nvidia-modeset',
          '/dev/nvidia-caps/nvidia-cap1')   # what a box with one card and NVIDIA's driver loaded has


class LinuxGpu(Linux):
    """`gpu` on a plain Linux box (#1027 item 3): on, NVIDIA's device nodes that exist and nothing else (never
    /dev/dxg or /usr/lib/wsl); off, the unit as before. No NVIDIA driver with gpu on: a note naming the two ways out,
    never T3 down (nothing is given with no nodes; a node the setting does not give still fails)."""
    def driver(self, nodes=DRIVER):
        for p in nodes:
            self.write(p, '')

    def gpu_line(self, out):
        return next(line for line in out.splitlines() if line.split()[1:2] == ['gpu'])

    def test_off_unit_unchanged(self):
        c = k.config()
        self.assertIs(c['gpu'], False)
        self.assertEqual(k.sha(k.render(c)[UNIT][0]), LINUX_UNIT_SHA)
        self.machine({**LINUX_CONFIG, 'gpu': False})
        self.assertEqual(k.render(k.config()), k.render(c))
        self.assertFalse([x for x in k.sandbox(c) if 'nvidia' in x or 'dxg' in x])

    def test_on_adds_nvidia_nodes_only(self):
        off = k.sandbox(k.config())
        self.machine({**LINUX_CONFIG, 'gpu': True})
        on = k.sandbox(k.config())
        self.assertEqual([x for x in on if x not in LINUX_GPU], off)   # every other wall as it was
        self.assertEqual([x for x in on if x not in off], LINUX_GPU)
        unit = k.render(k.config())[UNIT][0]
        for wsl in ('dxg', '/usr/lib/wsl', 'TemporaryFileSystem'):
            self.assertNotIn(wsl, unit)
        # the same with no driver at all: what the unit says does not depend on what /dev has now
        self.driver()
        self.assertEqual(k.render(k.config())[UNIT][0], unit)

    def test_on_with_the_driver(self):
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.ready()
        for line in LINUX_GPU:
            self.assertIn('\n' + line + '\n', self.read(UNIT))
        code, out = self.check()
        self.assertEqual(code, 0, out)
        line = self.gpu_line(out)
        self.assertTrue(line.startswith('PASS  gpu  GPU access is on'), out)
        for p in DRIVER:
            self.assertIn(p, line)
        self.assertNotIn('dxg', line)
        self.assertEqual(self.call(cli.main, ['as-agent', '--', 'nvidia-smi'])[0], 0)
        argv = self.log('systemd-run')
        for line in LINUX_GPU:
            self.assertIn('--property=' + line, argv)
        self.assertFalse([a for a in argv if 'dxg' in a or '/usr/lib/wsl' in a])

    def no_driver_note(self, line):
        self.assertTrue(line.startswith('PASS  gpu  GPU access is on'), line)
        self.assertIn('no NVIDIA driver', line)
        self.assertIn('"gpu": false', line)   # the two ways out: the driver, or the setting off
        self.assertIn('nvidia-persistenced', line)
        self.assertNotIn('nvidia-uvm (', line)   # no driver: no note about one of its nodes

    def test_no_driver_is_a_note(self):
        """gpu on, but no NVIDIA driver loaded (no /dev/nvidiactl, or no card node): setup still enables T3 and
        check passes with a note naming the two ways out; no crash. (#1038 round 1: a failure here took the whole
        machine down, its CPU work too, though T3 gets nothing with no nodes.)"""
        for nodes in ((), ('/dev/nvidiactl',), ('/dev/nvidia0', '/dev/nvidia-uvm')):
            with self.subTest(nodes=nodes):
                shutil.rmtree(self.root)
                self.setUp()
                self.driver(nodes)
                self.machine({**LINUX_CONFIG, 'gpu': True})
                code, out = self.setup_run()
                self.assertEqual(code, 0, out)
                self.assertNotIn('Traceback', out)
                self.assertIn(UNITS, self.log())
                self.assertNotIn('disable', '\n'.join(self.log()))
                code, out = self.check()
                self.assertEqual(code, 0, out)
                self.no_driver_note(self.gpu_line(out))

    def test_driver_gone_after_setup(self):
        """Set up with the driver; after a reboot it does not load (a kernel update it was not rebuilt for): T3's own
        start check (ExecStartPre: check), the 2-minute check --enforce and setup again all pass with the note, and
        nothing stops or disables T3. update.test.py: the automatic update still runs."""
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.ready()
        unit = self.read(UNIT)
        for p in DRIVER:
            os.unlink(self.path(p))
        self.seen('/dev/null')   # T3 started again after the reboot: none of the nodes
        self.write('/log/systemctl', '')
        code, out = self.check('--enforce')
        self.assertEqual(code, 0, out)
        self.no_driver_note(self.gpu_line(out))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertFalse([x for x in self.log() if x.startswith(('stop', 'disable', 'kill'))], self.log())
        self.assertEqual(self.read(UNIT), unit)
        self.driver()   # back after the next boot: the unit never changed, so no setup run is needed
        self.seen(*DRIVER)
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertNotIn('no NVIDIA driver', self.gpu_line(out))

    def test_driver_added_later(self):
        """gpu on before the driver: setup finishes (the note); the driver loaded, the unit is the one it wrote (it
        lists the nodes, it does not read /dev), a setup run again changes nothing, and the running T3 gets the
        nodes at its next start (a note till then)."""
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.assertEqual(self.setup_run()[0], 0)
        unit = self.read(UNIT)
        before = self.same_files()
        self.seen('/dev/null')
        self.driver()
        self.write('/log/systemctl', '')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read(UNIT), unit)
        self.assertEqual({p: f for p, f in self.same_files().items() if not p.startswith('/dev')},
                         {p: f for p, f in before.items() if not p.startswith('/dev')})
        self.assertNotIn('restart t3code-agents.service', self.log())
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 started before /dev/nvidia0', self.gpu_line(out))

    def test_uvm_missing_is_a_note(self):
        """The driver makes /dev/nvidia-uvm on first use: missing, T3 still runs (CUDA jobs fail): a note."""
        self.driver([p for p in DRIVER if not p.startswith('/dev/nvidia-uvm')])
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.ready()
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('no /dev/nvidia-uvm', self.gpu_line(out))

    def test_on_then_again_then_off(self):
        self.driver()
        self.ready()
        default = self.read(UNIT)
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.write('/log/systemctl', '')
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('restart t3code-agents.service', self.log())
        before = self.same_files()
        self.write('/log/systemctl', '')
        self.assertEqual(self.setup_run()[0], 0)
        self.assertEqual(self.same_files(), before)
        self.assertNotIn('restart t3code-agents.service', self.log())
        self.machine({**LINUX_CONFIG, 'gpu': False})
        self.write('/log/systemctl', '')
        self.assertEqual(self.setup_run()[0], 0)
        self.assertEqual(self.read(UNIT), default)
        self.assertEqual(k.sha(default), LINUX_UNIT_SHA)
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertIn('PASS  gpu  GPU access is off', self.check()[1])

    def test_unit_differs_from_the_setting(self):
        for written, now in ((False, True), (True, False)):
            with self.subTest(unit_written_with=written, machine_file_says=now):
                shutil.rmtree(self.root)
                self.setUp()
                self.driver()
                self.machine({**LINUX_CONFIG, 'gpu': written})
                self.walled()
                self.machine({**LINUX_CONFIG, 'gpu': now})
                code, out = self.check()
                self.assertEqual(code, 1, out)
                self.assertIn(f'the unit has GPU access {"on" if written else "off"}', self.gpu_line(out))

    def test_wsl_lines_on_linux_fail(self):
        """WSL's GPU lines (an edit, or a unit from the wrong kind) are not linux's: the gpu wall fails."""
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.walled()
        self.write(UNIT, self.read(UNIT).replace(LINUX_GPU[0], LINUX_GPU[0] + '\nBindPaths=-/dev/dxg'))
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('only some of the GPU lines', self.gpu_line(out))

    def seen(self, *nodes):
        """The running T3's own /dev (what /proc/PID/root shows root) holds these nodes."""
        pid = self.read('/log/units/t3code-agents.service.pid').strip()
        self.write(f'/proc/{pid}/root/dev/', '')
        for p in nodes:
            self.write(f'/proc/{pid}/root{p}', '')

    def test_what_t3_sees(self):
        """check looks into the running T3's /dev: with gpu on, NVIDIA's nodes and nothing more; with it off, none."""
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.walled()
        self.seen('/dev/null', *DRIVER)
        self.assertEqual(self.check()[0], 0)
        self.seen('/dev/dxg')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 sees /dev/dxg', self.gpu_line(out))

    def test_off_t3_sees_none(self):
        self.driver()
        self.walled()
        self.seen('/dev/null')
        self.assertEqual(self.check()[0], 0)
        self.seen('/dev/nvidia0')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 sees /dev/nvidia0', self.gpu_line(out))

    def test_turned_off_while_t3_sees_them(self):
        """gpu on, T3 running with NVIDIA's nodes; turned off: setup's own check does not fail on that T3 (it is about
        to restart: the restart wall's job), so setup finishes and T3 restarts without them."""
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.ready()
        self.seen(*DRIVER)
        self.machine({**LINUX_CONFIG, 'gpu': False})
        self.write('/log/systemctl', '')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))

    def test_an_earlier_boots_marker_hides_nothing(self):
        """A restart marker left by a setup stopped half-way, then a reboot: older than any T3 now, so the look into
        T3's /dev still runs, and a node the setting does not give still fails (#1038 round 1)."""
        self.driver()
        self.walled()
        self.seen('/dev/null', '/dev/nvidia0')
        self.write(k.PENDING, 'boot_id=an-earlier-boot monotonic_us=1\n')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('PASS  restart', out)
        self.assertIn('T3 sees /dev/nvidia0', self.gpu_line(out))

    def test_started_before_a_node_is_a_note(self):
        """A node the host has that the running T3 does not (made after T3 started: nvidia-uvm on first use)."""
        self.driver()
        self.machine({**LINUX_CONFIG, 'gpu': True})
        self.walled()
        self.seen(*[p for p in DRIVER if p != '/dev/nvidia-uvm'])
        code, out = self.check()
        self.assertEqual(code, 0, out)
        self.assertIn('T3 started before /dev/nvidia-uvm', self.gpu_line(out))


class Wsl2GpuSeen(Case):
    """The same look into T3's /dev on WSL2: off, no /dev/dxg; on, /dev/dxg and no NVIDIA node."""
    def seen(self, *nodes):
        pid = self.read('/log/units/t3code-agents.service.pid').strip()
        for p in nodes:
            self.write(f'/proc/{pid}/root{p}', '')

    def test_off(self):
        self.walled()
        self.seen('/dev/null')
        self.assertEqual(self.check()[0], 0)
        self.seen('/dev/dxg')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 sees /dev/dxg', out)

    def test_on(self):
        self.machine({**CONFIG, 'gpu': True})
        self.walled()
        self.seen('/dev/dxg')
        self.assertEqual(self.check()[0], 0)
        self.seen('/dev/nvidia0')
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 sees /dev/nvidia0', out)

    def test_linux_lines_on_wsl2_fail(self):
        """The twin of LinuxGpu.test_wsl_lines_on_linux_fail: an NVIDIA line in a WSL2 unit with gpu on (an edit, or
        a unit from the wrong kind) is not WSL2's: the gpu wall fails (#1038 round 1)."""
        self.machine({**CONFIG, 'gpu': True})
        self.walled()
        unit = self.read(UNIT)
        self.write(UNIT, unit.replace('DeviceAllow=/dev/dxg rw\n',
                                      'DeviceAllow=/dev/dxg rw\nDeviceAllow=/dev/nvidia0 rw\n'))
        self.assertNotEqual(self.read(UNIT), unit)
        code, out = self.check()
        self.assertEqual(code, 1, out)
        self.assertIn('only some of the GPU lines', next(x for x in out.splitlines() if x.split()[1:2] == ['gpu']))


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
