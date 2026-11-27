"""update-policy baseline: the drift lines, on a fake host (apt-config, systemctl, sshd answered from a table; /proc/net
and the disk from a temporary folder) with a fixed clock. No root, no systemd, no network. RedCheck breaks each rule
in a copy of the code and shows its test then fails.

  python3 -I -B fork-kit/update-policy/tests/baseline.test.py
"""
import os, sys, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fixture as fx  # noqa: E402
from fixture import at, baseline  # noqa: E402

APT_GOOD = '''APT::Periodic "";
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
Unattended-Upgrade "";
Unattended-Upgrade::Allowed-Origins "";
Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}";
Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";
Unattended-Upgrade::Automatic-Reboot "true";
Unattended-Upgrade::Automatic-Reboot "false";
'''
SSHD_GOOD = 'port 22\nusepam yes\npasswordauthentication no\nkbdinteractiveauthentication no\n'
HEADER = '  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n'


def net_line(local, remote, st, inode):
    return f'   0: {local} {remote} {st} 00000000:00000000 00:00000000 00000000     0        0 {inode} 1 0\n'


class Baseline(fx.HostCase):
    def setUp(self):
        super().setUp()
        self.now = at('2026-11-24 08:00')
        self.m = fx.FakeMachine(self.host, self.now)
        self.host.answers.update({
            'apt-config': (0, APT_GOOD, ''),
            'systemctl is-enabled': (0, 'enabled\n', ''),
            'sshd': (0, SSHD_GOOD, ''),
        })
        self.write('/usr/bin/unattended-upgrade')
        for name in ('tcp', 'tcp6', 'udp', 'udp6'):
            self.write(f'/proc/net/{name}', HEADER)

    def drift(self, state=None, channel=True, **over):
        if channel:
            over.setdefault('alertCommand', ['/usr/local/bin/notify'])      # a machine with an alert channel
        return baseline.check(self.host, self.m, fx.make_settings(**over), state or {}, self.now)

    def test_a_machine_with_no_alert_channel_is_drift(self):
        self.assertEqual(self.drift(channel=False), [
            'no alert channel: alerts go only to the journal (add the machine file\'s "alert" section, then run '
            '`update-policy test-alert` as root)'])
        channel = {'command': ['/usr/local/bin/notify'], 'timeoutSeconds': 30}
        s = fx.settings.check({'role': 'dev', 'quietHour': '05:00', 'timeZone': 'UTC'}, 'test', channel)
        self.assertEqual(baseline.check(self.host, self.m, s, {}, self.now), [])

    def test_a_machine_on_the_policy_has_no_drift(self):
        self.assertEqual(self.drift(), [])
        self.assertIn(['systemctl', 'is-enabled', 'update-policy.timer'], [c[0] for c in self.host.calls])

    def test_updates_off_no_security_origin_timers_off_not_installed(self):
        os.unlink(self.root + '/usr/bin/unattended-upgrade')
        apt = ('APT::Periodic::Unattended-Upgrade "0";\n'
               'Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}";\n')
        self.host.answers['apt-config'] = (0, apt, '')
        self.host.answers['systemctl is-enabled'] = lambda argv: (
            (1, 'disabled\n', '') if argv[2] == 'apt-daily-upgrade.timer' else (0, 'enabled\n', ''))
        self.assertEqual(self.drift(), [
            'unattended-upgrades is not installed (apt install unattended-upgrades)',
            'APT::Periodic::Update-Package-Lists is off: security updates are not installed by themselves '
            '(dpkg-reconfigure -plow unattended-upgrades)',
            'APT::Periodic::Unattended-Upgrade is off: security updates are not installed by themselves '
            '(dpkg-reconfigure -plow unattended-upgrades)',
            'unattended-upgrades does not take the distribution\'s security pocket (no <codename>-security or '
            'Debian-Security line in Allowed-Origins / Origins-Pattern; ESM lines alone do not count)',
            'apt-daily-upgrade.timer is disabled: apt does not run on its schedule'])

    def test_esm_lines_alone_are_no_security_pocket(self):
        """Ubuntu's default keeps the ESM -apps-security and -infra-security lines on: without the main line they
        pass a plain "security" match."""
        esm = APT_GOOD.replace('Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";\n',
                               'Unattended-Upgrade::Allowed-Origins:: "${distro_id}ESMApps:${distro_codename}-apps-security";\n'
                               'Unattended-Upgrade::Allowed-Origins:: "${distro_id}ESM:${distro_codename}-infra-security";\n')
        self.host.answers['apt-config'] = (0, esm, '')
        self.assertIn('does not take the distribution\'s security pocket', self.drift()[0])
        for line in ('"Debian:bookworm-security"', '"origin=Debian,codename=${distro_codename}-security,'
                     'label=Debian-Security"'):
            key = 'Origins-Pattern' if '=' in line else 'Allowed-Origins'
            self.host.answers['apt-config'] = (0, esm + f'Unattended-Upgrade::{key}:: {line};\n', '')
            self.assertEqual(self.drift(), [], line)

    def test_apt_booleans_off_and_on(self):
        for value in ('false', 'no', 'off', 'disable', 'without'):
            self.host.answers['apt-config'] = (0, APT_GOOD.replace('Unattended-Upgrade "1"', f'Unattended-Upgrade "{value}"'), '')
            self.assertIn('APT::Periodic::Unattended-Upgrade is off', self.drift()[0], value)
        for value in ('enable', 'with', 'yes', 'on', 'weird'):
            self.host.answers['apt-config'] = (0, APT_GOOD + f'Unattended-Upgrade::Automatic-Reboot "{value}";\n', '')
            self.assertIn(f'Unattended-Upgrade::Automatic-Reboot is "{value}"', self.drift()[0], value)

    def test_an_origins_pattern_with_security_counts(self):
        self.host.answers['apt-config'] = (0, APT_GOOD.replace(
            'Unattended-Upgrade::Allowed-Origins:: "${distro_id}:${distro_codename}-security";\n',
            'Unattended-Upgrade::Origins-Pattern:: "origin=Debian,codename=${distro_codename},label=Debian-Security";\n'
        ), '')
        self.assertEqual(self.drift(), [])

    def test_restart_policy_drift(self):
        self.host.answers['apt-config'] = (0, APT_GOOD + 'Unattended-Upgrade::Automatic-Reboot "true";\n', '')
        self.host.answers['systemctl is-enabled'] = lambda argv: (
            (4, 'not-found\n', '') if argv[2].startswith('update-policy') else (0, 'enabled\n', ''))
        lines = self.drift({'lastAlertError': 'exit 7'})
        self.assertEqual(len(lines), 5, lines)
        self.assertIn('unattended-upgrades restarts by itself (Unattended-Upgrade::Automatic-Reboot is "true")',
                      lines[0])
        self.assertIn('update-policy.timer is installed but not-found, not enabled: pending restarts are neither '
                      'made nor reported', lines[1])
        self.assertIn('update-policy-baseline.timer is installed but not-found', lines[2])
        self.assertIn('update-policy-boot.service is installed but not-found', lines[3])
        self.assertEqual(lines[4], 'the last alert failed: exit 7')

    def test_a_failed_tick_unit_is_drift(self):
        self.host.answers['systemctl is-failed'] = lambda argv: (
            (0, 'failed\n', '') if argv[2] == 'update-policy.service' else (1, 'active\n', ''))
        self.assertEqual(self.drift(), ['update-policy.service failed its last run (journalctl -u update-policy)'])

    def test_a_check_that_breaks_is_drift_and_the_others_still_run(self):
        self.host.answers['sshd'] = lambda argv: 1 / 0
        with mock.patch.object(baseline.os, 'statvfs', side_effect=RuntimeError('boom')):
            lines = self.drift()
        self.assertEqual(lines, ["cannot read sshd's settings (ZeroDivisionError: division by zero)",
                                 'cannot read the disks (RuntimeError: boom)'])
        os.unlink(self.root + '/proc/net/tcp')
        os.makedirs(self.root + '/proc/net/tcp')                        # EISDIR, not "no such file"
        self.assertEqual(self.drift(baseline={'ports': []})[0][:39], 'cannot read the listening ports (IsADir')

    def test_facts_it_cannot_read_are_drift(self):
        self.host.answers['apt-config'] = (None, '', 'apt-config: not installed')
        self.host.answers['sshd'] = (255, '', 'sshd: no hostkeys available -- exiting.')
        lines = self.drift()
        self.assertIn('cannot read apt\'s settings (apt-config: not installed)', lines)
        self.assertIn('cannot read sshd\'s settings for every connection (sshd -T: sshd: no hostkeys available -- '
                      'exiting.)', lines)

    def test_pending_age_is_from_the_first_sight_in_this_boot(self):
        self.pending(at('2026-11-24 07:00'), ['linux-firmware'])           # rewritten an hour ago
        state = {'pendingSeen': {'boot': 'boot-1', 'at': '2026-11-19T08:00:00Z'}}
        self.assertIn('restart pending since 2026-11-19 08:00 UTC (5.0 days) for linux-firmware', self.drift(state)[0])
        self.m.boot_ids.append('boot-2')                                  # another boot: that record is old
        self.assertEqual(self.drift(state), [])

    def test_a_restart_pending_too_long(self):
        self.pending(at('2026-11-21 08:00'), ['linux-image-6.8.0-50-generic'])
        self.assertEqual(self.drift(), [])                                  # 3 days: not more than 3
        self.pending(at('2026-11-20 20:00'), ['linux-image-6.8.0-50-generic'])
        self.assertEqual(self.drift(), ['restart pending since 2026-11-20 20:00 UTC (3.5 days) for '
                                        'linux-image-6.8.0-50-generic: more than 3 days; never idle in the quiet '
                                        'window? (journalctl -u update-policy)'])
        self.assertIn('a live machine: restart it in a planned window', self.drift(role='live')[0])
        self.assertEqual(self.drift(baseline={'pendingDays': 7}), [])

    def test_ssh_password_login(self):
        self.host.answers['sshd'] = (0, SSHD_GOOD.replace('passwordauthentication no', 'passwordauthentication yes'),
                                     '')
        self.assertEqual(self.drift(), ['ssh allows password login for every connection (PasswordAuthentication yes)',
                                        'ssh allows password login for root from outside (PasswordAuthentication yes)'])
        self.host.answers['sshd'] = (0, SSHD_GOOD.replace('kbdinteractiveauthentication no',
                                                          'kbdinteractiveauthentication yes'), '')
        self.assertIn('ssh allows password login through PAM for every connection (KbdInteractiveAuthentication yes, '
                      'UsePAM yes)', self.drift())
        self.host.answers['sshd'] = (0, 'usepam no\npasswordauthentication no\nkbdinteractiveauthentication yes\n', '')
        self.assertEqual(self.drift(), [])
        del self.host.answers['sshd']                                       # no sshd: nothing to check
        self.assertEqual(self.drift(), [])

    def test_a_match_block_that_allows_passwords_is_seen(self):
        def sshd(argv):
            if argv[2:3] == ['-C'] and 'user=deploy,' in argv[3]:
                return 0, SSHD_GOOD.replace('passwordauthentication no', 'passwordauthentication yes'), ''
            return 0, SSHD_GOOD, ''
        self.host.answers['sshd'] = sshd
        self.assertEqual(self.drift(), [])
        self.assertEqual(self.drift(baseline={'sshUsers': ['root', 'deploy']}),
                         ['ssh allows password login for deploy from outside (PasswordAuthentication yes)'])
        self.assertIn(['sshd', '-T', '-C', 'user=deploy,host=outside.invalid,addr=203.0.113.1'],
                      [c[0] for c in self.host.calls])

    def test_a_full_disk(self):
        full = os.statvfs_result((4096, 4096, 1000, 50, 40, 0, 0, 0, 0, 255))    # 95% used, rounded up
        with mock.patch.object(baseline.os, 'statvfs', return_value=full):
            self.assertEqual(self.drift(), ['disk / is 96% full (more than 90%)'])
            self.assertEqual(self.drift(baseline={'diskPercent': 96}), [])
        self.assertEqual(self.drift(baseline={'diskPaths': ['/nope']}),
                         ['cannot read the disk of /nope (No such file or directory)'])

    def ports(self, tcp=(), tcp6=(), udp=(), udp6=()):
        for name, rows in (('tcp', tcp), ('tcp6', tcp6), ('udp', udp), ('udp6', udp6)):
            self.write(f'/proc/net/{name}', HEADER + ''.join(net_line(*r) for r in rows))

    def test_listening_ports_outside_the_list(self):
        self.ports(tcp=[('0100007F:1F90', '00000000:0000', '0A', 11),       # 127.0.0.1:8080, loopback
                        ('00000000:0016', '00000000:0000', '0A', 12),       # 0.0.0.0:22
                        ('0100A8C0:0CB5', '00000000:0000', '0A', 13),       # 192.168.0.1:3253
                        ('65126464:0EB5', '00000000:0000', '0A', 14),       # 100.100.18.101:3765
                        ('0100A8C0:01BB', '0200A8C0:D431', '01', 15)],      # an open connection
                   tcp6=[('00000000000000000000000000000000:0050', '00000000000000000000000000000000:0000', '0A',
                          16)],                                             # [::]:80
                   udp=[('00000000:A2F9', '00000000:0000', '07', 17),       # 0.0.0.0:41721
                        ('0100A8C0:D431', '08080808:0035', '01', 18)])      # a connected client
        self.assertEqual(self.drift(), [])                                  # no list: no port check
        lines = self.drift(baseline={'ports': ['tcp/22', 'udp/41000-42000', 'tcp/3765@100.64.0.0/10']})
        self.assertEqual(lines, ['listening on tcp [::]:80: not in baseline.ports',
                                 'listening on tcp 192.168.0.1:3253: not in baseline.ports'])
        lines = self.drift(baseline={'ports': ['tcp/22@100.64.0.0/10', 'udp/41721', 'tcp/3253', 'tcp/80'],
                                     'ignoreLoopback': False})
        self.assertEqual(lines, ['listening on tcp 0.0.0.0:22: not in baseline.ports',
                                 'listening on tcp 100.100.18.101:3765: not in baseline.ports',
                                 'listening on tcp 127.0.0.1:8080: not in baseline.ports'])
        lines = self.drift(baseline={'ports': ['tcp/22', 'udp/41721', 'tcp/3253', 'tcp/80',
                                               'tcp/3765@100.64.0.0/10']})
        self.assertEqual(lines, [])
        self.assertEqual(self.drift(baseline={'ports': ['tcp/22@0.0.0.0/0', 'udp/41721', 'tcp/3253', 'tcp/80',
                                                        'tcp/3765']}), [])

    def test_the_program_that_holds_a_port_is_named(self):
        self.ports(tcp=[('00000000:1F90', '00000000:0000', '0A', 4242)])
        self.write('/proc/77/comm', 'python3\n')
        os.makedirs(self.root + '/proc/77/fd')
        os.symlink('socket:[4242]', self.root + '/proc/77/fd/5')
        self.assertEqual(self.drift(baseline={'ports': []}),
                         ['listening on tcp 0.0.0.0:8080 (python3): not in baseline.ports'])

    def test_run_prints_only_drift_alerts_with_it_and_exits_one(self):
        s = fx.make_settings(alertCommand=['/usr/local/bin/notify'], baseline={'ports': []})
        self.ports(tcp=[('00000000:1F90', '00000000:0000', '0A', 1)])
        with mock.patch('builtins.print') as out:
            self.assertEqual(baseline.run(self.host, self.m, lambda: s), 1)
        self.assertEqual([c.args[0] for c in out.call_args_list],
                         ['listening on tcp 0.0.0.0:8080: not in baseline.ports'])
        self.assertEqual(self.m.alerts, [('drift', 'listening on tcp 0.0.0.0:8080: not in baseline.ports')])
        self.ports()
        with mock.patch('builtins.print') as out:
            self.assertEqual(baseline.run(self.host, self.m, lambda: s), 0)
        self.assertEqual(out.call_args_list, [])
        self.assertEqual(len(self.m.alerts), 1)

    def test_a_failed_alert_is_kept_and_cleared_by_the_next_one_that_goes_out(self):
        s = fx.make_settings(alertCommand=['/usr/local/bin/notify'], baseline={'ports': []})
        self.ports(tcp=[('00000000:1F90', '00000000:0000', '0A', 1)])
        self.m.alert_error = 'exit 7'
        with mock.patch('builtins.print'):
            baseline.run(self.host, self.m, lambda: s)
            self.assertEqual(self.m.read_state()['lastAlertError'], 'exit 7')
            self.m.alert_error = None
            baseline.run(self.host, self.m, lambda: s)
        self.assertIsNone(self.m.read_state()['lastAlertError'])

    def test_a_machine_file_it_cannot_use_is_drift(self):
        def broken():
            raise fx.settings.MachineFileError('/etc/fork-kit/machine.json: no "update-policy" section')
        with mock.patch('builtins.print') as out:
            self.assertEqual(baseline.run(self.host, self.m, broken), 1)
        self.assertIn('the machine file cannot be used', out.call_args_list[0].args[0])


MUTATIONS = {
    'no alert channel is drift': (
        'baseline', "    if s['alertCommand']:\n        return []\n    return ['no alert channel",
        "    if True:\n        return []\n    return ['no alert channel",
        'Baseline.test_a_machine_with_no_alert_channel_is_drift'),
    'Automatic-Reboot not false is drift': (
        'baseline', "    if problem:\n        out.append(problem)", "    if False:\n        out.append(problem)",
        'Baseline.test_restart_policy_drift'),
    'apt booleans': (
        'aptconf', "FALSE = ('0', 'no', 'false', 'without', 'off', 'disable')", "FALSE = ('0', 'false')",
        'Baseline.test_apt_booleans_off_and_on'),
    'the last value wins': (
        'aptconf', "    return values[-1].strip().lower()", "    return values[0].strip().lower()",
        'Baseline.test_a_machine_on_the_policy_has_no_drift'),
    'the main security pocket is needed': (
        'baseline', "    if 'esm' in v or v.endswith(('-apps-security', '-infra-security')):\n        return False\n", "",
        'Baseline.test_esm_lines_alone_are_no_security_pocket'),
    'a failed tick unit is drift': (
        'baseline', "        if (text.strip().splitlines() or [''])[0] == 'failed':", "        if False:",
        'Baseline.test_a_failed_tick_unit_is_drift'),
    'a check that breaks does not stop the others': (
        'baseline', "    except Exception as e:      # noqa: BLE001", "    except ValueError as e:      # noqa: BLE001",
        'Baseline.test_a_check_that_breaks_is_drift_and_the_others_still_run'),
    'pending days': (
        'baseline', "    if days <= s['baseline']['pendingDays']:", "    if True:",
        'Baseline.test_a_restart_pending_too_long'),
    'pending age from the first sight': (
        'baseline', "    pending, _seen = tick.first_seen(state or {}, pending, boot)", "",
        'Baseline.test_pending_age_is_from_the_first_sight_in_this_boot'),
    'a wildcard listener needs a wildcard entry': (
        'baseline', "            if net.prefixlen == 0 and net.version == addr.version:", "            if True:",
        'Baseline.test_listening_ports_outside_the_list'),
    'loopback is ignored only when asked': (
        'baseline', "        if b['ignoreLoopback'] and addr.is_loopback:", "        if addr.is_loopback:",
        'Baseline.test_listening_ports_outside_the_list'),
    'connected UDP is no listener': (
        'baseline', "            if proto == 'udp' and (st != UDP_UNCONNECTED or remote.split(':')[1] != '0000'):",
        "            if False:", 'Baseline.test_listening_ports_outside_the_list'),
    'password login through PAM': (
        'baseline', "    if kbd == 'yes' and cfg.get('usepam') == 'yes':", "    if False:",
        'Baseline.test_ssh_password_login'),
    'Match blocks: -C for each user': (
        'baseline', "    for user in s['baseline']['sshUsers']:", "    for user in []:",
        'Baseline.test_a_match_block_that_allows_passwords_is_seen'),
    'facts it cannot read are drift': (
        'baseline', "        return out + [cfg]", "        return out",
        'Baseline.test_facts_it_cannot_read_are_drift'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        if os.environ.get(fx.LIB_ENV):
            self.skipTest('a red check run')
        fx.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
