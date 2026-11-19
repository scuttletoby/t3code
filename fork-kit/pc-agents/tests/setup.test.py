"""pc-agents setup on a fake WSL2 distro (fake_distro.py): every file and mode, T3 enabled only after every wall
holds, a second run changes nothing, a run stopped at any write or a failed T3 install finishes on the next and
restarts T3, the firewall is loaded before T3 is downloaded, the owner steps run as the agent only under the unit's
walls, and a distro that is not fresh is refused with nothing changed.

  python3 -I -B fork-kit/pc-agents/tests/setup.test.py
"""
import json, os, re, shutil, signal, stat, subprocess, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from fake_distro import CONFIG, KIT, REPO, SHA1, SHA2, Case, blob, cli, k  # noqa: E402

DPKG = '--force-confdef --force-confold --configure -a'   # how setup runs dpkg: no config-file question
# apt-get's options: no config-file question, and a wait for dpkg's lock (apt-get's own locks are retried by setup)
APT = 'Dpkg::Options::=--force-confdef Dpkg::Options::=--force-confold DPkg::Lock::Timeout=300'


class Setup(Case):
    def test_fresh_distro_serves_only_after_the_walls_hold(self):
        code, out = self.setup_run()
        self.assertEqual(code, 3, out)
        self.assertIn('FAIL  automount', out)
        self.assertIn('wsl --terminate', out)
        self.assertFalse([line for line in self.log() if line.startswith('enable')])
        self.assertIn('disable --now t3code-agents.service pc-agents-walls.timer pc-agents-update.timer', self.log())
        self.terminate()
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertNotIn('FAIL', out)
        self.assertIn('enable --now t3code-agents.service pc-agents-walls.timer pc-agents-update.timer', self.log())
        self.assertIn('tailscale up --advertise-tags=tag:pc-agents --accept-dns=false', out)
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))
        self.assertIn('versions/1.2.3/t3 serve', self.read('/log/units/t3code-agents.service.active'))

    def test_every_file_and_permission(self):
        self.ready()
        c = k.config()
        record = k.record()
        for p, (text, mode) in k.render(c).items():
            st = os.lstat(self.path(p))
            self.assertTrue(stat.S_ISREG(st.st_mode), p)
            self.assertEqual((stat.S_IMODE(st.st_mode), st.st_uid), (mode, os.geteuid()), p)
            self.assertEqual(self.read(p), text, p)
            self.assertEqual(record[p], {k.sha(text)}, p)
        for p in k.masks(c):
            self.assertEqual(os.readlink(self.path(p)), '/dev/null', p)
        self.assertEqual(os.readlink(self.path('/opt/pc-agents/current')), SHA1)
        for src, (dst, mode) in k.CODE.items():
            p = self.path(f'/opt/pc-agents/{SHA1}/{dst}')
            self.assertEqual(stat.S_IMODE(os.stat(p).st_mode), mode, dst)
            self.assertEqual(blob(p), blob(os.path.join(REPO, src)), dst)
        self.assertIn('agent:x:1100:1100::/home/agent:/usr/sbin/nologin', self.read('/etc/passwd'))
        self.assertIn('/etc/systemd/system/user@1100.service', k.masks(c))
        t3 = self.path('/opt/pc-agents/t3/runtime/versions/1.2.3/t3')
        self.assertEqual(os.stat(t3).st_uid, os.geteuid())
        self.assertEqual(record['/opt/pc-agents/t3/runtime/versions/1.2.3/t3'], {k.file_sha(t3[len(self.root):])})
        self.assertFalse(os.path.lexists(self.path('/home/agent/.t3')))
        # root downloads the release archive and its SHA256SUMS over HTTPS only, checks and unpacks it, and runs
        # nothing it downloaded (the fake t3 notes a run in /log/t3-ran)
        self.assertEqual(self.log('install'), [f'1.2.3 {k.arch()}'])
        fetched = [line.split()[-1] for line in self.log('curl') if 'https://example.com/' in line]
        self.assertEqual(fetched, ['https://example.com/releases/v1.2.3/SHA256SUMS',
                                   f'https://example.com/releases/v1.2.3/t3-1.2.3-linux-{k.arch()}.tar.gz'])
        self.assertTrue(all('--proto =https --proto-redir =https' in line for line in self.log('curl')
                            if 'https://' in line))
        self.assertFalse(os.path.exists(self.path('/log/t3-ran')))
        self.assertEqual(self.read('/opt/pc-agents/t3/runtime/versions/1.2.3/.install-complete'), '1.2.3\n')

    def test_what_the_files_say(self):
        self.ready()
        policy = json.loads(self.read('/etc/t3code/policy.json'))
        self.assertEqual(policy['settings'], {'agentSlots.maxPerMachine': {'value': 2, 'locked': True},
                                              'linkedEnvironmentsEnabled': {'value': True, 'locked': True}})
        self.assertEqual(policy['limits']['maxRuntimeMode'], 'full-access')
        self.assertEqual((policy['limits']['connect'], policy['limits']['webhookTasks']), (False, False))
        self.assertEqual(policy['limits']['updates'], {'mode': 'off'})   # the only thing keeping updates off
        wsl = self.read('/etc/wsl.conf')
        for line in ('[automount]\nenabled=false\nmountFsTab=false', '[interop]\nenabled=false\nappendWindowsPath=false',
                     '[boot]\nsystemd=true', 'generateResolvConf=false', '[user]\ndefault=root'):
            self.assertIn(line, wsl)
        self.assertNotIn('default=agent', wsl)
        unit = self.read('/etc/systemd/system/t3code-agents.service')
        for line in ('User=agent', 'ExecStartPre=+/opt/pc-agents/current/sbin/pc-agents check', 'ProtectSystem=strict',
                     'InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix', 'MemoryMax=8G', 'CPUQuota=400%',
                     '-/run/tailscale', 'IPAddressAllow=100.101.102.103/32',
                     'ExecStart=/opt/pc-agents/t3/runtime/versions/1.2.3/t3 serve', 'T3CODE_PORT=3800',
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
        self.assertEqual(len(self.log('install')), 1)   # T3 downloaded once

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
        self.no_watch()
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
            if stop < total:   # the last write is the marker right before the restart, after the enable
                self.assertFalse([line for line in self.log() if line.startswith('enable')], f'stopped at write {stop}')
            code, out = self.setup_run()
            self.assertEqual(code, 0, f'stopped at write {stop} ({writes[stop - 1]}): {out}')
            self.assertEqual({p: v[4] for p, v in self.snapshot().items()}, whole, f'stopped at write {stop}')
            self.assertIn('enable --now t3code-agents.service pc-agents-walls.timer pc-agents-update.timer', self.log())

    def test_t3_install_fails_then_works(self):
        self.terminate()
        self.write('/log/install-fail', '')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 install failed', out)
        self.assertFalse([line for line in self.log() if line.startswith('enable')])
        os.unlink(self.path('/log/install-fail'))
        self.assertEqual(self.setup_run()[0], 0)

    def test_t3_installed_loose_is_made_roots_alone(self):   # tar keeps the archive's modes; mktemp -d is 0700
        self.terminate()
        self.write('/log/install-loose', '')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        d = self.path('/opt/pc-agents/t3/runtime/versions/' + CONFIG['t3Version'])
        self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o755)
        for p in (d + '/lib', d + '/lib/x'):
            self.assertEqual(stat.S_IMODE(os.lstat(p).st_mode), 0o755, p)
        self.assertEqual(stat.S_IMODE(os.lstat(d + '/lib/x/m.js').st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.lstat(d + '/lib/s').st_mode), 0o755)   # no setuid left
        self.assertEqual(os.readlink(d + '/lib/y'), 'x')
        self.assertEqual(len(self.log('install')), 1)

    def test_t3_with_a_link_out_is_refused_and_the_target_untouched(self):
        self.terminate()
        self.write('/log/install-link-out', '')
        self.write('/log/outside', 'o\n')
        os.chmod(self.path('/log/outside'), 0o664)
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn(f'T3 1.2.3 refused: t3-1.2.3-linux-{k.arch()}/out links to {self.root}/log/outside, out of '
                      'the folder', out)
        self.assertEqual(stat.S_IMODE(os.stat(self.path('/log/outside')).st_mode), 0o664)
        self.assertEqual(os.listdir(self.path('/opt/pc-agents/t3/runtime/versions')), [])   # nothing left
        self.assertFalse([line for line in self.log() if line.startswith('enable')])

    def test_a_release_archive_that_is_not_the_one_named_or_unpacks_out_is_refused(self):
        for marker, said in (
                ('sums-bad', f't3-1.2.3-linux-{k.arch()}.tar.gz has SHA-256 '),
                ('tar-outside', f't3-1.2.3-linux-{k.arch()}/../evil is not under t3-1.2.3-linux-{k.arch()}/'),
                ('tar-through-link', f't3-1.2.3-linux-{k.arch()}/l/f is unpacked through or over a link'),
                ('tar-hardlink-out', f't3-1.2.3-linux-{k.arch()}/h links to /etc/passwd, not a file before it')):
            with self.subTest(marker):
                kept = self.keep()
                self.terminate()
                self.write('/log/' + marker, '')
                code, out = self.setup_run()
                self.assertEqual(code, 1, out)
                self.assertIn('T3 1.2.3 refused: ' + said, out)
                self.assertEqual(os.listdir(self.path('/opt/pc-agents/t3/runtime/versions')), [])
                self.assertFalse([p for p in self.snapshot() if p.endswith(('/evil', '/f', '/h'))])
                self.assertFalse([line for line in self.log() if line.startswith('enable')])
                self.restore(kept)

    def test_setuid_in_the_runtime_fails_the_runtime_wall(self):
        self.ready()
        d = self.path('/opt/pc-agents/t3/runtime/versions/' + CONFIG['t3Version'])
        self.write(d[len(self.root):] + '/x', '')
        os.chmod(d + '/x', 0o4755)
        self.assertIn('is setuid or setgid', cli.tree(d[len(self.root):], d[len(self.root):]) or '')

    def test_runtime_closed_or_half_removed_is_installed_again(self):
        self.ready()
        v = '/opt/pc-agents/t3/runtime/versions/'
        d = self.path(v + CONFIG['t3Version'])
        os.chmod(d, 0o700)   # stopped before root_alone opened it: the agent could not run t3
        self.write(v + '.removing-1/lib/a', '')   # a removal stopped half-way
        os.unlink(self.path('/log/systemctl'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertIn('is not mode 0755: removing it', out)
        self.assertIn('stop t3code-agents.service', self.log())
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o755)
        self.assertEqual(sorted(os.listdir(self.path(v))), [CONFIG['t3Version']])
        self.assertEqual(len(self.log('install')), 2)

    def test_loose_t3_from_an_older_setup_is_installed_again(self):
        self.terminate()
        self.write('/log/install-loose', '')
        self.assertEqual(self.setup_run()[0], 0)
        d = self.path('/opt/pc-agents/t3/runtime/versions/' + CONFIG['t3Version'])
        os.chmod(d + '/lib', 0o775)   # as an older kit's install left it (upstream's tar kept the archive's modes)
        self.assertEqual(self.check()[0], 1)   # the runtime wall fails, as on the PC
        os.unlink(self.path('/log/install-loose'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertIn(': removing it to install T3', out)
        self.assertEqual(len(self.log('install')), 2)
        d = self.path('/opt/pc-agents/t3/runtime/versions/' + CONFIG['t3Version'])
        self.assertFalse(os.path.lexists(d + '/lib'))
        before = self.snapshot()
        self.assertEqual(self.setup_run()[0], 0)
        self.assertEqual(len(self.log('install')), 2)
        self.assertEqual(self.snapshot(), before)

    def test_stopped_while_removing_a_loose_runtime_then_run_again(self):
        self.ready()
        v = '/opt/pc-agents/t3/runtime/versions/'
        d = self.path(v + CONFIG['t3Version'])
        os.chmod(d, 0o700)
        real = cli.shutil.rmtree

        def stopped(path, *a, **kw):
            os.unlink(os.path.join(path, 't3'))   # part removed, then stopped
            raise KeyboardInterrupt
        cli.shutil.rmtree = stopped
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run()
        finally:
            cli.shutil.rmtree = real
        self.assertNotIn(v + CONFIG['t3Version'] + '/t3', k.record())   # no old hash for the new t3 to match
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(os.listdir(self.path(v))), [CONFIG['t3Version']])
        self.assertEqual(stat.S_IMODE(os.lstat(d).st_mode), 0o755)

    def test_failed_t3_install_on_upgrade_still_restarts_t3_on_the_next_run(self):
        self.ready()
        os.unlink(self.path('/log/systemctl'))
        self.machine({**CONFIG, 't3Version': '1.2.4'})
        self.write('/log/install-fail', '')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 restarts once it succeeds', out)
        self.assertTrue(os.path.isfile(self.path(k.PENDING)))
        os.unlink(self.path('/log/install-fail'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertIn('versions/1.2.4/t3 serve', self.read('/log/units/t3code-agents.service.active'))
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))

    def stop_after(self, path, before=False):
        """Run setup, stopped (as by a kill) right after path is written, or right before."""
        real_put = cli.put

        def stopping(p, *a, **kw):
            if p == path and before:
                raise KeyboardInterrupt
            changed = real_put(p, *a, **kw)
            if p == path:
                raise KeyboardInterrupt
            return changed
        cli.put = stopping
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run()
        finally:
            cli.put = real_put

    def test_killed_after_the_policy_is_written_still_restarts_t3(self):
        self.ready()
        os.unlink(self.path('/log/systemctl'))
        self.machine({**CONFIG, 'maxRuntimeMode': 'approval-required'})
        self.stop_after('/etc/t3code/policy.json')
        running = '/log/units/t3code-agents.service.active'
        self.assertNotIn('approval-required', self.read(running))   # the old T3 still runs with the old policy
        self.assertIn('approval-required', self.read('/etc/t3code/policy.json'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertIn('approval-required', self.read(running))
        os.unlink(self.path('/log/systemctl'))
        self.assertEqual(self.setup_run()[0], 0)   # and only once
        self.assertFalse([line for line in self.log() if line.startswith('restart')])

    def test_killed_at_a_write_then_the_machine_file_edited(self):
        unit = '/etc/systemd/system/t3code-agents.service'
        for before in (True, False):   # the record already names the new file, which is or is not written yet
            with self.subTest(before=before):
                self.ready()
                self.machine({**CONFIG, 'memoryMax': '4G'})
                self.stop_after(unit, before)
                self.machine({**CONFIG, 'memoryMax': '2G'})   # the owner changes their mind before running again
                code, out = self.setup_run()
                self.assertEqual(code, 0, out)
                self.assertIn('MemoryMax=2G', self.read(unit))
                self.assertEqual(k.record()[unit], {k.file_sha(unit)})
                shutil.rmtree(self.root)
                self.setUp()

    def test_nftables_installed_after_a_package_list_update(self):
        os.replace(self.path('/usr/sbin/nft'), self.path('/log/nft-package'))
        os.unlink(self.path('/log/packages/nftables'))
        self.terminate()
        self.write('/log/apt-fail', '')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('apt-get update && apt-get install -y nftables libatomic1 unzip', out)
        os.unlink(self.path('/log/apt-fail'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.log('apt-get'), ['update', 'update', 'install -y nftables libatomic1 unzip'])

    def test_libatomic1_and_unzip_are_installed_once(self):
        """Node (the agents' tool) does not start without libatomic.so.1 and the agent has no sudo; unzip is for
        postinstall steps. A fresh Ubuntu has neither (#269)."""
        self.ready()
        self.assertEqual(self.log('apt-get'), ['update', 'install -y libatomic1 unzip'])
        for p in ('libatomic1', 'unzip'):
            self.assertEqual(self.read('/log/packages/' + p), 'install ok installed\n')
        for log in ('apt-get', 'dpkg', 'systemctl'):
            os.unlink(self.path('/log/' + log))
        self.assertEqual(self.setup_run()[0], 0)   # runs again: nothing to install
        self.assertEqual(self.log('apt-get'), [])
        self.assertEqual(self.log('dpkg'), [])
        self.write('/log/packages/unzip', 'install ok unpacked')   # an apt-get stopped half-way
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.log('dpkg'), ['--audit', DPKG])   # first: apt-get refuses while dpkg was interrupted
        self.assertEqual(self.log('apt-get'), ['update', 'install -y unzip'])
        self.assertFalse([line for line in self.log() if line.startswith('restart')])   # no T3 restart for a package

    def test_a_held_package_counts_as_installed(self):
        """`apt-mark hold` makes the status `hold ok installed`: installed, so no apt-get on every run (#296)."""
        self.ready()
        for log in ('apt-get', 'dpkg'):
            os.unlink(self.path('/log/' + log))
        self.write('/log/packages/unzip', 'hold ok installed')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.log('apt-get'), [])
        self.assertEqual(self.log('dpkg'), [])

    def test_packages_are_installed_after_the_kits_own_resolv_conf(self):
        """A distro of an older kit (no libatomic1) whose machine file's nameservers broke DNS: the corrected
        nameservers reach resolv.conf before apt-get needs them, and a run stopped at apt-get goes on (#296)."""
        self.ready()
        os.unlink(self.path('/log/packages/libatomic1'))
        self.write('/log/dns', 'nameserver 8.8.8.8')   # the mirror resolves only through the corrected nameserver
        self.machine({**CONFIG, 'nameservers': ['8.8.8.8']})
        self.write('/log/apt-fail', '')   # and the network is down for this run
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('nameserver 8.8.8.8\n', self.read('/etc/resolv.conf'))   # written before apt-get
        self.assertEqual(self.check()[0], 1)   # between the two runs: the files changed, T3 did not restart (#321)
        os.unlink(self.path('/log/apt-fail'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('/log/packages/libatomic1'), 'install ok installed\n')
        self.assertEqual(self.check()[0], 0)

    def test_dpkg_and_apt_get_ask_nothing(self):
        """stdin is /dev/null: a config-file question (an upgrade stopped half-way over an edited file) or a debconf
        one takes its default instead of failing at end of file (#296)."""
        self.write('/log/questions', '')
        self.ready()
        self.assertEqual(self.read('/log/packages/unzip'), 'install ok installed\n')
        self.write('/log/packages/unzip', 'install ok unpacked')
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read('/log/packages/unzip'), 'install ok installed\n')
        self.assertEqual(set(self.log('apt-get-options')), {APT})   # every apt-get run, every option (#321)

    def test_dpkg_configure_runs_only_when_something_is_half_installed(self):
        """`dpkg --configure -a` takes dpkg's lock without waiting: on a fresh distro with nothing half-installed
        it is not run, so unattended-upgrades holding that lock is left to apt-get's own wait (#321). A journal file
        whose name is not all digits (the whole name: not 0003.new) is one apt-get does not look at either, and a
        distro without that folder, or with it empty, has no journal (#349)."""
        fresh = self.keep()
        self.assertFalse(os.path.lexists(self.path('/var/lib/dpkg')))
        for p in (None, '/var/lib/dpkg/updates/', '/var/lib/dpkg/updates/tmp.i', '/var/lib/dpkg/updates/0003.new'):
            with self.subTest(p):
                self.restore(fresh)
                if p:
                    self.write(p, '')
                self.ready()
                self.assertEqual(self.log('dpkg'), ['--audit'])
                self.assertEqual(self.log('apt-get'), ['update', 'install -y libatomic1 unzip'])

    def test_an_updates_folder_it_cannot_read_counts_as_interrupted(self):
        """apt skips a /var/lib/dpkg/updates it cannot read; setup runs `dpkg --configure -a` then, which costs one
        dpkg run that waits for its lock and finds nothing to do (#349)."""
        fresh = self.keep()
        full = self.path('/var/lib/dpkg/updates')
        self.addCleanup(lambda: os.path.isdir(full) and os.chmod(full, 0o755))
        for how in ('a file', 'mode 000'):   # ENOTDIR, EACCES (the tests never run as root)
            with self.subTest(how):
                self.restore(fresh)
                if how == 'a file':
                    self.write('/var/lib/dpkg/updates', '')
                else:
                    self.write('/var/lib/dpkg/updates/', '')
                    os.chmod(full, 0)
                self.ready()
                self.assertEqual(self.log('dpkg'), ['--audit', DPKG])
                self.assertEqual(self.log('apt-get'), ['update', 'install -y libatomic1 unzip'])

    def test_dpkg_configure_runs_when_its_audit_fails(self):
        """A `dpkg --audit` that fails and names nothing (a status file it cannot parse) is not taken as all clear:
        `dpkg --configure -a` runs (#349)."""
        self.write('/log/dpkg-audit-fail', '')
        self.ready()
        self.assertEqual(self.log('dpkg'), ['--audit', DPKG])

    def test_an_interrupted_dpkg_that_names_nothing_is_finished(self):
        """dpkg stopped after its last status change but before saving it (`wsl --shutdown` during
        unattended-upgrades): its journal under /var/lib/dpkg/updates says installed, so `dpkg --audit` names
        nothing, yet apt-get install refuses ("dpkg was interrupted") while that folder holds a file named all
        digits, on every run. Setup finishes it first, as apt asks; the next run has nothing to finish (#349)."""
        self.write('/var/lib/dpkg/updates/0003', 'Package: unzip\nStatus: install ok installed\n')
        self.write('/var/lib/dpkg/updates/tmp.i', '')
        self.ready()
        self.assertEqual(self.log('dpkg'), ['--audit', DPKG])
        self.assertEqual(os.listdir(self.path('/var/lib/dpkg/updates')), ['tmp.i'])
        self.assertEqual(self.log('apt-get'), ['update', 'install -y libatomic1 unzip'])
        os.unlink(self.path('/log/packages/unzip'))   # and a later run with a package to install
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.log('dpkg'), ['--audit', DPKG, '--audit'])

    def test_a_lock_apt_daily_holds_is_waited_for(self):
        """A new distro where apt-daily runs: apt-get update's lists lock, install's archives lock and dpkg's own
        lock are taken without a wait (only DPkg::Lock::Timeout's frontend lock waits), so setup tries again (#321)."""
        self.write('/log/packages/unzip', 'install ok unpacked')   # an upgrade stopped half-way
        for name in ('dpkg-locked', 'apt-locked-update', 'apt-locked-install'):
            self.write('/log/' + name, '2')
        self.ready()
        self.assertEqual(self.log('dpkg'), ['--audit'] + [DPKG] * 3)
        self.assertEqual(self.log('apt-get'), ['update'] * 3 + ['install -y libatomic1 unzip'] * 3)
        self.assertEqual(self.read('/log/packages/libatomic1'), 'install ok installed\n')
        self.assertEqual(self.check()[0], 0)

    def test_a_lock_is_told_apart_under_any_locale(self):
        """sudo keeps LANG and LC_*, and apt and dpkg ship translations: setup runs them under LC_ALL=C, so a lock
        held while the owner's locale is German is still waited for (#349)."""
        env = {n: v for n, v in os.environ.items() if n not in ('LC_ALL', 'LC_MESSAGES')}
        with mock.patch.dict(os.environ, {**env, 'LANG': 'de_DE.UTF-8'}, clear=True):
            self.write('/log/packages/unzip', 'install ok unpacked')
            for name in ('dpkg-locked', 'apt-locked-update', 'apt-locked-install'):
                self.write('/log/' + name, '2')
            self.ready()
        self.assertEqual(self.log('dpkg'), ['--audit'] + [DPKG] * 3)
        self.assertEqual(self.log('apt-get'), ['update'] * 3 + ['install -y libatomic1 unzip'] * 3)

    def test_a_lock_file_it_may_not_open_is_not_waited_for(self):
        """apt-get adds `Unable to lock directory` to any lock failure, also one no other apt-get causes (here the
        lock file cannot be opened): that one is not tried again for 5 minutes, nor named as a busy apt (#349)."""
        self.write('/log/apt-denied', '')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('Permission denied', out)
        self.assertNotIn('Another apt-get or dpkg still runs', out)
        self.assertEqual(self.log('apt-get'), ['update'])

    def test_a_lock_held_past_the_wait_is_named_with_its_own_fix(self):
        """A lock still held after the wait (5 minutes on a machine): the advice is to wait for the other apt-get or
        dpkg, not to fix the network; nothing is installed, and the next run goes on."""
        for name, failed in (('dpkg-locked', 'dpkg --configure -a failed'),
                             ('apt-locked-update', 'apt-get update failed'),
                             ('apt-locked-install', 'apt-get install failed')):
            with self.subTest(lock=name):
                self.ready()
                os.unlink(self.path('/log/packages/libatomic1'))
                self.write('/log/packages/unzip', 'install ok unpacked')
                self.write('/log/' + name, '1000000')
                code, out = self.setup_run()
                self.assertEqual(code, 1, out)
                self.assertIn(failed, out)
                self.assertIn('Could not get lock' if 'apt' in name else 'locked by another process', out)
                self.assertIn('Another apt-get or dpkg still runs', out)
                self.assertNotIn('network', out)
                self.assertFalse(os.path.exists(self.path('/log/packages/libatomic1')))
                os.unlink(self.path('/log/' + name))
                code, out = self.setup_run()
                self.assertEqual(code, 0, out)
                self.assertEqual(self.read('/log/packages/libatomic1'), 'install ok installed\n')
                shutil.rmtree(self.root)
                self.setUp()

    def test_t3_that_does_not_start_is_reported(self):
        self.ready()
        self.write('/log/t3-crash', '')
        self.machine({**CONFIG, 'memoryMax': '4G'})
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('journalctl -u t3code-agents.service', out)
        self.assertNotIn('its log:', out)   # an empty journal: the message still comes
        self.assertIn('exit status 1', out)   # with the main process's exit status
        self.write('/log/t3-crash', '77')
        code, out = self.setup_run()
        self.assertIn('exit status 77: T3 cannot apply the managed policy', out)
        self.assertTrue(os.path.isfile(self.path(k.PENDING)))
        # T3's own error comes with it, from the unit's journal
        self.write('/log/journal', 'starting\n[12:00:01.234] ERROR (#1): ManagedPolicyFileError: T3 Code cannot use '
                                   'the managed policy \x1b]52;c;eA==\x07at x\n    at stack (y.js:1)\n'
                                   'Main process exited, code=exited, status=77/NOPERM\n')
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('its log: [12:00:01.234] ERROR (#1): ManagedPolicyFileError: T3 Code cannot use the managed '
                      'policy ?]52;c;eA==?at x:', out)   # terminal controls from the journal never reach the terminal
        args = self.log('journalctl')[-1].split()   # only this run's journal, not an earlier crash's
        self.assertEqual(args[:2] + args[4:], ['-u', 't3code-agents.service', '-n', '80', '-o', 'cat', '--no-pager'])
        self.assertEqual(args[2], '--since')
        self.assertLessEqual(abs(int(args[3].lstrip('@')) - time.time()), 300)
        os.unlink(self.path('/log/t3-crash'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))


    def test_how_t3_ended_is_said_only_when_not_cleanly(self):
        self.ready()
        self.machine({**CONFIG, 'memoryMax': '4G'})
        for crash, said in (('2 9', 'killed by signal 9'), ('3 6', 'killed by signal 6'), ('1 0', None)):
            self.write('/log/t3-crash', crash)   # ExecMainCode ExecMainStatus: CLD_KILLED, CLD_DUMPED, a clean exit
            code, out = self.setup_run()
            self.assertEqual(code, 1, out)
            self.assertIn('T3 did not start or stay up', out)
            if said:
                self.assertIn(said, out)
            else:
                self.assertNotIn('exit status', out)
                self.assertNotIn('killed', out)

    def test_t3_that_dies_a_moment_after_the_restart_is_reported(self):
        self.ready()
        self.write('/log/t3-dies', '')   # T3 starts, then exits on something it cannot apply
        self.machine({**CONFIG, 'memoryMax': '4G'})
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('T3 did not start or stay up (T3 is inactive)', out)
        self.assertNotIn('done:', out)
        self.assertTrue(os.path.isfile(self.path(k.PENDING)))
        os.unlink(self.path('/log/t3-dies'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))

    def test_rerun_within_the_start_limit_still_restarts_t3(self):
        self.ready()
        self.write('/log/units/t3code-agents.service.limit', '')   # systemd: start request repeated too quickly
        self.machine({**CONFIG, 'memoryMax': '4G'})
        os.unlink(self.path('/log/systemctl'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        log = self.log()
        self.assertLess(log.index('reset-failed t3code-agents.service'), log.index('restart t3code-agents.service'))

    def test_agent_of_an_older_kit_gets_no_login_shell(self):
        self.ready()
        self.write('/etc/passwd', self.read('/etc/passwd').replace('/home/agent:/usr/sbin/nologin', '/home/agent:/bin/bash'))
        code, out = self.setup_run()
        self.assertEqual(code, 0, out)
        self.assertIn('agent:x:1100:1100::/home/agent:/usr/sbin/nologin', self.read('/etc/passwd'))
        self.assertEqual(self.log('usermod'), ['--shell /usr/sbin/nologin agent'])
        os.unlink(self.path('/log/usermod'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertEqual(self.log('usermod'), [])   # once


class RestartPending(Case):
    """The restart wall with setup: a T3 that started before setup's last change fails `check`."""
    stop_after = Setup.stop_after

    def test_interrupted_upgrade_keeps_old_t3_and_check_fails(self):   # lenspcas1 #1
        self.ready()
        self.machine({**CONFIG, 't3Version': '1.2.4', 'maxRuntimeMode': 'approval-required'})
        self.write('/log/install-fail', '')
        self.assertEqual(self.setup_run()[0], 1)
        running = self.read('/log/units/t3code-agents.service.active')
        self.assertIn('versions/1.2.3/t3 serve', running)
        self.assertIn('full-access', running)
        code, out = self.call(cli.main, ['as-agent', '--', 'sh'])
        self.assertEqual(code, 1, out)
        self.assertEqual(self.log('systemd-run'), [])
        os.unlink(self.path('/log/systemctl'))
        code, out = self.check('--enforce')
        self.assertNotEqual(code, 0, out)
        self.assertIn('FAIL  restart', out)
        self.assertIn('stop t3code-agents.service', self.log())
        os.unlink(self.path('/log/install-fail'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('versions/1.2.4/t3 serve', self.read('/log/units/t3code-agents.service.active'))
        self.assertEqual(self.check('--enforce')[0], 0)

    def test_killed_after_the_policy_is_written_check_fails(self):   # lenspcac1 #3
        self.ready()
        self.machine({**CONFIG, 'maxRuntimeMode': 'approval-required'})
        self.stop_after('/etc/t3code/policy.json')
        self.assertNotIn('approval-required', self.read('/log/units/t3code-agents.service.active'))
        self.assertNotEqual(self.check('--enforce')[0], 0)

    def test_upgrade_killed_at_any_write_fails_check_until_run_again(self):
        def files():
            return {p: v[4] for p, v in self.snapshot().items() if p not in (k.PENDING, k.RECORD)}
        upgrade = {**CONFIG, 'memoryMax': '4G', 'maxRuntimeMode': 'approval-required'}
        real_put, writes = cli.put, []
        self.addCleanup(setattr, cli, 'put', real_put)
        self.no_watch()
        self.ready()
        self.machine(upgrade)
        upgradable = self.keep()   # a machine set up all the way, its config just changed
        cli.put = lambda *a, **kw: writes.append(a[0]) or real_put(*a, **kw)
        self.assertEqual(self.setup_run(SHA2)[0], 0)
        cli.put = real_put
        self.assertGreater(len(writes), 10)
        for stop in range(1, len(writes) + 1):
            self.restore(upgradable)
            before = files()

            def stopping(*a, n=[0], **kw):
                n[0] += 1
                if n[0] == stop:
                    raise KeyboardInterrupt
                return real_put(*a, **kw)
            cli.put = stopping
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run(SHA2)
            cli.put = real_put
            code, out = self.check()
            if files() != before:   # something T3 runs with changed
                self.assertIn('FAIL  restart', out, f'stopped at write {stop} ({writes[stop - 1]})')
            code, out = self.setup_run(SHA2)
            self.assertEqual(code, 0, f'stopped at write {stop}: {out}')
            code, out = self.check()
            self.assertEqual(code, 0, f'stopped at write {stop}: {out}')

    def test_check_passes_on_its_own_restart_and_a_kill_while_watching_restarts_again(self):
        self.ready()
        self.machine({**CONFIG, 'memoryMax': '4G'})
        real, seen = cli.stays_up, []

        def watching():   # killed while setup watches the restarted T3
            seen.append(self.check())
            raise KeyboardInterrupt
        cli.stays_up = watching
        try:
            with self.assertRaises(KeyboardInterrupt):
                self.setup_run()
        finally:
            cli.stays_up = real
        self.assertEqual(seen[0][0], 0, seen[0][1])   # the T3 setup just restarted is not stale
        self.assertTrue(os.path.isfile(self.path(k.PENDING)))
        self.assertEqual(self.check()[0], 0)
        os.unlink(self.path('/log/systemctl'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('restart t3code-agents.service', self.log())   # once more, then the marker goes
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))

    def test_marker_of_an_older_kit_fails_until_setup_runs_again(self):
        self.ready()
        self.write(k.PENDING, 'setup changed what T3 runs with; the next good run restarts T3 and removes this\n')
        self.assertIn('FAIL  restart', self.check()[1])
        os.unlink(self.path('/log/systemctl'))
        self.assertEqual(self.setup_run()[0], 0)
        self.assertIn('restart t3code-agents.service', self.log())
        self.assertFalse(os.path.lexists(self.path(k.PENDING)))
        self.assertEqual(self.check()[0], 0)


class Signals(Case):
    def test_sigterm_or_sighup_kills_the_running_download_too(self):
        for sig in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=sig.name):
                self.write('/log/install-hang', '')
                p = subprocess.Popen([sys.executable, '-I', os.path.join(KIT, 'sbin', 'pc-agents'), 'setup',
                                      '--source', REPO, '--sha', SHA1], env={'PC_AGENTS_ROOT': self.root,
                                      'PATH': '/usr/bin:/bin'}, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                hang = self.path('/log/hang.pid')
                try:
                    end = time.monotonic() + 60
                    while not os.path.exists(hang) and time.monotonic() < end and p.poll() is None:
                        time.sleep(0.05)
                    self.assertTrue(os.path.exists(hang), 'the fake download did not start')
                    time.sleep(0.2)
                    pid = int(self.read('/log/hang.pid'))
                    p.send_signal(sig)
                    self.assertEqual(p.wait(timeout=30), 128 + sig)
                    end = time.monotonic() + 10
                    while time.monotonic() < end and alive(pid):
                        time.sleep(0.05)
                    self.assertFalse(alive(pid), 'the download outlived pc-agents')
                finally:
                    if p.poll() is None:
                        p.kill()
                        p.wait()
                    if os.path.exists(hang):
                        try:
                            os.kill(int(self.read('/log/hang.pid')), signal.SIGKILL)
                        except (ProcessLookupError, ValueError):
                            pass
                        os.unlink(hang)


def alive(pid):
    """pid runs (a zombie, ended but not yet reaped, does not count)."""
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class StaleFirewall(Case):
    def test_tightened_allow_with_failed_install(self):
        self.machine({**CONFIG, 'allow': ['192.168.1.0/24']})
        self.ready()
        os.unlink(self.path('/log/systemctl'))
        self.machine({**CONFIG, 't3Version': '1.2.4'})
        self.write('/log/install-fail', '')
        self.assertEqual(self.setup_run()[0], 1)
        self.assertNotIn('192.168.1.0/24', self.read('/log/nft-loaded'))   # new table already loaded
        status, out = self.check()
        fails = [line for line in out.splitlines() if line.startswith('FAIL')]
        self.assertEqual(status, 1)                      # the old T3 still runs: only the restart wall fails,
        self.assertEqual([line.split()[1] for line in fails], ['restart'], out)   # the firewall agrees with the file

    def test_a_loaded_table_that_is_not_the_files_fails(self):
        self.ready()
        self.write('/log/nft-loaded', self.read('/log/nft-loaded').replace('10.0.0.0/8', '10.0.0.0/9'))
        os.unlink(self.path('/log/systemctl'))
        code, out = self.check('--enforce')
        self.assertEqual(code, 1, out)
        self.assertIn('FAIL  firewall', out)
        self.assertIn('stop t3code-agents.service', self.log())


class AsAgent(Case):
    def test_runs_as_the_agent_under_the_units_walls(self):
        self.ready()
        tool = 'pc-agents-test-tool-only-in-the-agents-path'   # like claude in ~/.local/bin: not on root's PATH
        code, out = self.call(cli.main, ['as-agent', '--', tool, 'login'])
        self.assertEqual(code, 0, out)
        argv = self.log('systemd-run')
        self.assertEqual(argv[-5:], ['--', '/usr/bin/env', '--', tool, 'login'])   # env looks it up in PATH below
        self.assertTrue([a for a in argv if re.fullmatch(r'--unit=pc-agents-as-agent-\d+', a)], argv)
        for arg in ('--pty', '--uid=agent', '--gid=agent', '--setenv=HOME=/home/agent', '--setenv=SHELL=/bin/bash',
                    '--setenv=PATH=/home/agent/.local/bin:/usr/local/bin:/usr/bin:/bin',
                    '--property=InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix -/usr/lib/wsl -/run/tailscale'):
            self.assertIn(arg, argv)
        for line in k.sandbox(k.config()):
            self.assertIn('--property=' + line, argv)

    def test_refused_when_a_wall_does_not_hold(self):
        self.ready()
        self.write('/var/lib/systemd/linger/agent', '')
        code, out = self.call(cli.main, ['as-agent', '--', 'claude'])
        self.assertEqual(code, 1, out)
        self.assertIn('not running anything as the agent', out)
        self.assertEqual(self.log('systemd-run'), [])

    def test_pair_before_its_t3_is_installed_says_so(self):
        """An upgrade stopped half-way (the new t3Version written, its download failed): no env error, the cause."""
        self.ready()
        self.machine({**CONFIG, 't3Version': '1.2.4'})
        self.write('/log/install-fail', '')
        self.assertEqual(self.setup_run()[0], 1)
        for stopped in (False, True):   # the old T3 still runs, or the 2-minute check stopped it (a restart is due)
            if stopped:
                k.run(['/usr/bin/systemctl', 'stop', k.UNIT])
            code, out = self.call(cli.main, ['pair'])
            self.assertEqual(code, 1, out)
            self.assertIn('T3 1.2.4 is not installed yet: run pc-agents setup again (README step 3)', out)
            self.assertEqual(self.log('systemd-run'), [])

    def test_pair_and_as_agent_are_for_root_only(self):
        for argv in (['pair'], ['pair', '--ttl', '1m'], ['as-agent', '--', 'claude']):
            with mock.patch.object(k, 'ROOT', ''), mock.patch.object(os, 'geteuid', lambda: 1100), \
                    mock.patch.object(k, 'config', mock.Mock(side_effect=AssertionError('read the machine file'))):
                code, out = self.call(cli.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn('run this as root', out)
        self.assertEqual(self.log('systemd-run'), [])

    def test_pair_runs_the_installed_t3_pair_as_the_agent(self):
        self.ready()
        self.machine({**CONFIG, 't3Version': '1.2.4'})
        self.assertEqual(self.setup_run()[0], 0)   # 1.2.3's folder stays: a glob over versions would find two
        for args in ([], ['--ttl', '15m']):
            code, out = self.call(cli.main, ['pair'] + args)
            self.assertEqual(code, 0, out)
            argv = self.log('systemd-run')
            self.assertEqual(argv[argv.index('--'):], ['--', '/usr/bin/env', '--',
                                                       '/opt/pc-agents/t3/runtime/versions/1.2.4/t3', 'pair'] + args)
            self.assertIn('--uid=agent', argv)
            self.assertIn('--setenv=T3CODE_HOME=/home/agent/.t3', argv)   # the unit's T3 home: its running server
        self.write('/var/lib/systemd/linger/agent', '')
        os.unlink(self.path('/log/systemd-run'))
        code, out = self.call(cli.main, ['pair'])
        self.assertEqual(code, 1, out)
        self.assertEqual(self.log('systemd-run'), [])


class Refused(Case):
    def refused(self, why):
        lock = ('/run', os.path.dirname(k.LOCK), k.LOCK)   # the lock setup holds: a tmpfs file, gone at boot
        before = {p: v for p, v in self.snapshot().items() if p not in lock}
        code, out = self.setup_run()
        self.assertEqual(code, 1, out)
        self.assertIn('refused, nothing changed', out)
        self.assertIn(why, out)
        self.assertEqual({p: v for p, v in self.snapshot().items() if p not in lock}, before)
        self.assertEqual(self.log(), [])

    def test_a_distro_with_its_own_user(self):
        with open(self.path('/etc/passwd'), 'a') as f:
            f.write('someone:x:1000:1000::/home/someone:/bin/bash\n')
        self.refused('already has login users (someone)')
        self.refused('userdel -r')

    def test_folders_that_are_not_roots_alone(self):
        for d in ('/opt/pc-agents', '/etc/systemd/system'):
            os.makedirs(self.path(d), exist_ok=True)
            os.chmod(self.path(d), 0o775)
            self.refused(f'{d} is not owned by uid')
            os.chmod(self.path(d), 0o755)

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
        self.assertEqual(k.config()['lockedSettings'], {})   # fork-only settings would stop an upstream t3Version

    def test_bad_config(self):
        for change, why in (({'lockedSettings': {'linkedEnvironmentsEnabled': False}}, 'set by the kit itself'),
                            ({'lockedSettings': {'agentSlots.maxPerMachine.x': {'y': 2}}}, 'one field per dotted path'),
                            ({'nameservers': ['192.168.1.1']}, 'only public addresses'),
                            ({'t3Version': 'latest'}, 'an exact version'),
                            ({'t3Version': '0.1.6'}, '0.1.7 or newer'),   # no managed policy: no locks at all
                            ({'t3Version': '0.0.30-fork.1'}, '0.1.7 or newer'),
                            # built before 0.1.7 had the managed policy: a pre-release is older than its release
                            ({'t3Version': '0.1.7-nightly.20261031.3991'}, '0.1.7 or newer'),
                            ({'t3Version': '0.1.7-nightly.20261031.3991.fork.1'}, '0.1.7 or newer'),   # the fork's
                            ({'t3Version': '0.1.7-fork.1.rc.1'}, '0.1.7 or newer'),
                            ({'maxRuntimeMode': 'root'}, 'one of')):
            self.machine({**CONFIG, **change})
            code, out = self.setup_run()
            self.assertEqual(code, 2, out)
            self.assertIn(why, out)
        self.assertFalse(os.path.lexists(self.path('/etc/t3code/policy.json')))


    def test_versions_from_0_1_7_on_are_accepted(self):
        """Compared as numbers, the release itself included: a string compare would refuse 0.1.10 (#296). The fork's
        stable build of a release (`v<upstream>-fork.<n>`, fork-release.yml) is that release (#321)."""
        for v in ('0.1.7', '0.1.7-fork.1', '0.1.7-fork.12', '0.1.8-rc.1', '0.1.10',
                  '0.1.10-nightly.20261114.4682.fork.1', '0.2.0', '1.0.0'):
            self.machine({**CONFIG, 't3Version': v})
            self.assertEqual(k.config()['t3Version'], v)


class Readme(unittest.TestCase):
    def test_owner_steps_run_as_written_in_powershell(self):
        """Every kit command of the owner steps is a whole PowerShell line: `pc-agents ...` alone is not found there."""
        with open(os.path.join(KIT, 'README.md'), encoding='utf-8') as f:
            readme = f.read()
        steps = readme[readme.index('## Owner steps'):readme.index('## Tailscale tag')]
        spans = re.findall(r'`([^`]*pc-agents (?:pair|as-agent)\b[^`]*)`', steps)
        self.assertGreaterEqual(len(spans), 5, spans)
        for span in spans:
            self.assertRegex(span, r'^wsl -d <distro> -u root (--|-e) /opt/pc-agents/current/sbin/pc-agents ')
        self.assertIn('pc-agents pair --ttl 15m`', steps)
        for key in ('user.name', 'user.email'):   # the agent's git identity: placeholders the owner fills in
            line = next(s for s in spans if f'git config --global {key} ' in s)
            self.assertRegex(line, rf"git config --global {key} '<[^>]+>'$")
            # -e: wsl runs it without root's shell, so a name with an apostrophe (O'Brien) reaches git as typed
            self.assertTrue(line.startswith('wsl -d <distro> -u root -e '), line)


class ReleaseHolds(unittest.TestCase):
    """Setup copies k.CODE out of the release commit the bootstrap downloads, and a release commit holds only the
    paths of .github/fork-ci-release.json: every source must be in one of them (setup crashed on a real distro
    with FileNotFoundError for fork-fleet-walls.py, since the tests run from a full checkout)."""

    def test_every_code_source_is_in_a_release(self):
        root = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', '..'))
        with open(os.path.join(root, '.github', 'fork-ci-release.json')) as f:
            paths = json.load(f)['paths']
        missing = [src for src in k.CODE if not any(src == p or src.startswith(p.rstrip('/') + '/') for p in paths)]
        self.assertEqual(missing, [], 'add these to .github/fork-ci-release.json paths')
        for src in k.CODE:
            self.assertTrue(os.path.isfile(os.path.join(root, src)), src)


if __name__ == '__main__':
    unittest.main()
