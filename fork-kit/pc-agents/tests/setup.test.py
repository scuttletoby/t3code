"""pc-agents setup on a fake WSL2 distro (fake_distro.py): every file and mode, T3 enabled only after every wall
holds, a second run changes nothing, a run stopped at any write finishes on the next, and a distro that is not
fresh is refused with nothing changed.

  python3 -I -B fork-kit/pc-agents/tests/setup.test.py
"""
import json, os, re, shutil, stat, sys, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, KIT, REPO, SHA1, SHA2, Case, blob, cli, k  # noqa: E402


class Setup(Case):
    def test_fresh_distro_serves_only_after_the_walls_hold(self):
        code, out = self.setup_run()
        self.assertEqual(code, 3, out)
        self.assertIn('FAIL  automount', out)
        self.assertIn('wsl --terminate', out)
        self.assertFalse([line for line in self.log() if line.startswith('enable')])
        self.assertIn('disable --now t3code-agents.service pc-agents-walls.timer', self.log())
        self.terminate()
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertNotIn('FAIL', out)
        self.assertIn('enable --now t3code-agents.service pc-agents-walls.timer', self.log())
        self.assertIn('tailscale up --advertise-tags=tag:pc-agents', out)

    def test_every_file_and_permission(self):
        self.ready()
        c = k.config()
        record = k.record()
        for p, (text, mode) in k.render(c).items():
            st = os.lstat(self.path(p))
            self.assertTrue(stat.S_ISREG(st.st_mode), p)
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid), (mode, os.geteuid()), p)
            self.assertEqual(self.read(p), text, p)
            self.assertEqual(record[p], k.sha(text), p)
        for p in k.MASKS:
            self.assertEqual(os.readlink(self.path(p)), '/dev/null', p)
        self.assertEqual(os.readlink(self.path('/opt/pc-agents/current')), SHA1)
        for src, (dst, mode) in k.CODE.items():
            p = self.path(f'/opt/pc-agents/{SHA1}/{dst}')
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), mode, dst)
            self.assertEqual(blob(p), blob(os.path.join(REPO, src)), dst)
        self.assertIn('agent:x:1100:1100::/home/agent:/bin/bash', self.read('/etc/passwd'))
        self.assertTrue(os.path.isfile(self.path('/home/agent/.t3/runtime/versions/1.2.3/t3')))
        self.assertIn('T3CODE_VERSION=1.2.3', self.log('runuser')[0])
        self.assertIn('T3CODE_RELEASE_BASE_URL=https://example.com/releases', self.log('runuser')[0])

    def test_what_the_files_say(self):
        self.ready()
        policy = json.loads(self.read('/etc/t3code/policy.json'))
        self.assertEqual(policy['settings'], {'agentSlots.maxPerMachine': {'value': 2, 'locked': True},
                                              'automaticUpdates': {'value': False, 'locked': True},
                                              'linkedEnvironmentsEnabled': {'value': True, 'locked': True}})
        self.assertEqual(policy['limits']['maxRuntimeMode'], 'full-access')
        self.assertEqual((policy['limits']['connect'], policy['limits']['webhookTasks']), (False, False))
        wsl = self.read('/etc/wsl.conf')
        for line in ('[automount]\nenabled=false\nmountFsTab=false', '[interop]\nenabled=false\nappendWindowsPath=false',
                     '[boot]\nsystemd=true', 'generateResolvConf=false', 'default=agent'):
            self.assertIn(line, wsl)
        unit = self.read('/etc/systemd/system/t3code-agents.service')
        for line in ('User=agent', 'ExecStartPre=+/opt/pc-agents/current/sbin/pc-agents check', 'ProtectSystem=strict',
                     'InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix', 'MemoryMax=8G', 'CPUQuota=400%',
                     'ExecStart=/home/agent/.t3/runtime/versions/1.2.3/t3 serve', 'T3CODE_PORT=3800',
                     'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK', 'SystemCallFilter=~io_uring_setup'):
            self.assertIn(line, unit)
        self.assertNotIn('AF_VSOCK', unit)
        nft = self.read('/etc/pc-agents/firewall.nft')
        for line in ('meta skuid != 1100 accept', '172.16.0.0/12', '10.0.0.0/8', '192.168.0.0/16', '100.64.0.0/10',
                     'fc00::/7'):
            self.assertIn(line, nft)
        self.assertEqual(self.read('/etc/resolv.conf').splitlines()[1:], ['nameserver 1.1.1.1', 'nameserver 9.9.9.9'])

    def test_second_run_changes_nothing(self):
        self.ready()
        before = self.snapshot()
        os.unlink(self.path('/log/systemctl'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse([line for line in self.log() if line.startswith('restart')])
        self.assertEqual(len(self.log('runuser')), 1)   # T3 downloaded once

    def test_new_release_or_config_restarts_t3(self):
        self.ready()
        os.unlink(self.path('/log/systemctl'))
        self.assertEqual(self.setup_run(SHA2)[0], 0)
        self.assertEqual(os.readlink(self.path('/opt/pc-agents/current')), SHA2)
        self.assertIn('restart t3code-agents.service', self.log())
        os.unlink(self.path('/log/systemctl'))
        self.machine({**CONFIG, 'memoryMax': '4G'})
        self.assertEqual(self.setup_run(SHA2)[0], 0)
        self.assertIn('MemoryMax=4G', self.read('/etc/systemd/system/t3code-agents.service'))
        self.assertIn('restart t3code-agents.service', self.log())

    def test_stopped_half_way_then_run_again(self):
        self.terminate()
        self.assertEqual(self.setup_run()[0], 0)
        whole = {p: v[4] for p, v in self.snapshot().items()}
        real_put, writes = cli.put, []

        def counting(*a, **kw):
            writes.append(a[0])
            return real_put(*a, **kw)
        cli.put = counting
        self.addCleanup(setattr, cli, 'put', real_put)
        shutil.rmtree(self.root)
        self.setUp()
        self.terminate()
        self.setup_run()
        total = len(writes)
        self.assertGreater(total, 20)
        for stop in range(1, total + 1):
            shutil.rmtree(self.root)
            self.setUp()
            self.terminate()

            def stopping(*a, n=[0], **kw):
                n[0] += 1
                if n[0] == stop:
                    raise KeyboardInterrupt
                return real_put(*a, **kw)
            cli.put = stopping
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run()
            cli.put = real_put
            self.assertFalse([line for line in self.log() if line.startswith('enable')], f'stopped at write {stop}')
            code, out = self.setup_run()
            self.assertEqual(code, 0, f'stopped at write {stop} ({writes[stop - 1]}): {out}')
            self.assertEqual({p: v[4] for p, v in self.snapshot().items()}, whole, f'stopped at write {stop}')
            self.assertIn('enable --now t3code-agents.service pc-agents-walls.timer', self.log())

    def test_t3_install_fails_then_works(self):
        self.terminate()
        self.write('/log/runuser-fail', '')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 install failed', out)
        self.assertFalse([line for line in self.log() if line.startswith('enable')])
        os.unlink(self.path('/log/runuser-fail'))
        self.assertEqual(self.setup_run()[0], 0)


class Refused(Case):
    def refused(self, why):
        before = self.snapshot()
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('refused, nothing changed', out)
        self.assertIn(why, out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.log(), [])

    def test_a_distro_with_its_own_user(self):
        with open(self.path('/etc/passwd'), 'a') as f:
            f.write('someone:x:1000:1000::/home/someone:/bin/bash\n')
        self.refused('already has login users (someone)')

    def test_a_wsl_conf_of_its_own(self):
        self.write('/etc/wsl.conf', '[boot]\nsystemd=true\n[user]\ndefault=someone\n')
        self.refused('/etc/wsl.conf was not written by this kit')

    def test_a_policy_of_its_own(self):
        self.write('/etc/t3code/policy.json', '{"schemaVersion": 1}\n')
        self.refused('/etc/t3code/policy.json was not written by this kit')

    def test_an_agent_that_is_not_the_kits(self):
        with open(self.path('/etc/passwd'), 'a') as f:
            f.write('agent:x:1200:1200::/home/agent:/bin/bash\n')
        self.refused('user agent has uid 1200, not 1100')

    def test_an_agent_with_sudo(self):
        self.ready()
        self.write('/etc/group', self.read('/etc/group').replace('sudo:x:27:', 'sudo:x:27:agent'))
        os.unlink(self.path('/log/systemctl'))
        self.refused('user agent is in other groups (sudo)')

    def test_an_unmasked_ssh(self):
        self.write('/etc/systemd/system/ssh.service', '[Service]\nExecStart=/usr/sbin/sshd\n')
        self.refused('ssh.service exists and is not a mask')

    def test_a_file_edited_after_setup(self):
        self.ready()
        with open(self.path('/etc/systemd/system/t3code-agents.service'), 'a') as f:
            f.write('[Service]\nUser=root\n')
        os.unlink(self.path('/log/systemctl'))
        self.refused('t3code-agents.service was not written by this kit, or was edited since')

    def test_the_examples_are_valid_and_the_same(self):
        with open(os.path.join(REPO, 'fork-kit', 'common', 'machine.example.json'), encoding='utf-8') as f:
            example = json.load(f)['pc-agents']
        with open(os.path.join(KIT, 'README.md'), encoding='utf-8') as f:
            readme = re.search(r'```json\n(\{\n  "version".*?)\n```', f.read(), re.S).group(1)
        self.assertEqual(json.loads(readme)['pc-agents'], example)
        self.machine(example)
        self.assertEqual(k.config()['agentUid'], 1100)
        self.assertEqual(k.config()['lockedSettings'], {'agentSlots.enabled': True, 'agentSlots.maxPerMachine': 2})

    def test_bad_config(self):
        for change, why in (({'lockedSettings': {'linkedEnvironmentsEnabled': False}}, 'set by the kit itself'),
                            ({'lockedSettings': {'agentSlots': {'maxPerMachine': 2}}}, 'one field per dotted path'),
                            ({'nameservers': ['192.168.1.1']}, 'only public addresses'),
                            ({'t3Version': 'latest'}, 'an exact version'),
                            ({'maxRuntimeMode': 'root'}, 'one of')):
            self.machine({**CONFIG, **change})
            code, out = self.setup_run()
            self.assertEqual(code, 2, out)
            self.assertIn(why, out)
        self.assertFalse(os.path.lexists(self.path('/etc/t3code/policy.json')))


if __name__ == '__main__':
    unittest.main()
