"""The daily baseline check: compares the machine with the policy and prints only what drifted.

check(host, settings, state, now) returns the drift lines ([]: none):

- unattended-upgrades is installed and on (APT::Periodic, apt's own booleans), its timers are enabled, and it
  takes the distribution's own security pocket (`<codename>-security` or Debian-Security; ESM lines alone do not
  count);
- the restart policy matches the role: unattended-upgrades never restarts by itself (Automatic-Reboot false: this
  kit owns restarts), the kit's units are enabled and did not fail, and the last alert went out;
- no restart pending for more than baseline.pendingDays days (since this boot first saw it);
- no listening port outside baseline.ports (when the list is given);
- no password login over ssh (PasswordAuthentication, or keyboard-interactive through PAM), for a connection
  from outside and for each of baseline.sshUsers (`sshd -T -C`, so Match blocks count);
- no disk of baseline.diskPaths fuller than baseline.diskPercent.

A fact it cannot read is drift too ("cannot read ..."), never a pass, and one check that breaks never stops the
others.
"""
import ipaddress, os, re

from updatepolicy import aptconf, settings as settings_mod, tick

TIMERS = ('apt-daily.timer', 'apt-daily-upgrade.timer')
OWN_UNITS = ('update-policy.timer', 'update-policy-baseline.timer', 'update-policy-boot.service')
WATCHED = ('update-policy.service', 'update-policy-boot.service')   # the baseline's own unit fails on drift
OUTSIDE = 'host=outside.invalid,addr=203.0.113.1'                   # a connection from a TEST-NET address
SECURITY_RE = re.compile(r'(^|,)(codename|archive|suite|a|n)=[^,]*-security(,|$)|(^|,)label=debian-security(,|$)')
TCP_LISTEN, UDP_UNCONNECTED = '0A', '07'
NET_FILES = (('tcp', '/proc/net/tcp', 4), ('tcp', '/proc/net/tcp6', 6), ('udp', '/proc/net/udp', 4),
             ('udp', '/proc/net/udp6', 6))


def main_security(value):
    """An origin line that takes the distribution's own security pocket: `${distro_id}:${distro_codename}-security`,
    or a pattern with `codename=...-security` or `label=Debian-Security`. ESM's -apps-security and -infra-security
    are extra pockets, not it."""
    v = value.strip().lower()
    if 'esm' in v or v.endswith(('-apps-security', '-infra-security')):
        return False
    return bool(re.search(r'^[^:,=]*:[^:,=]*-security$', v) or SECURITY_RE.search(v))


def updates_drift(host, cfg):
    out = []
    if not os.path.exists(host.p('/usr/bin/unattended-upgrade')):
        out.append('unattended-upgrades is not installed (apt install unattended-upgrades)')
    if isinstance(cfg, str):
        return out + [cfg]
    if aptconf.last(cfg, 'APT::Periodic::Enable') in aptconf.FALSE:
        out.append('APT::Periodic::Enable is off: no automatic updates at all')
    for key in ('APT::Periodic::Update-Package-Lists', 'APT::Periodic::Unattended-Upgrade'):
        if aptconf.last(cfg, key) in ('',) + aptconf.FALSE:
            out.append(f'{key} is off: security updates are not installed by themselves '
                       '(dpkg-reconfigure -plow unattended-upgrades)')
    origins = [v for k, vs in cfg.items() for v in vs
               if k in ('Unattended-Upgrade::Allowed-Origins::', 'Unattended-Upgrade::Origins-Pattern::')]
    if not any(main_security(v) for v in origins):
        out.append('unattended-upgrades does not take the distribution\'s security pocket (no <codename>-security '
                   'or Debian-Security line in Allowed-Origins / Origins-Pattern; ESM lines alone do not count)')
    for unit in TIMERS:
        state = enabled(host, unit)
        if state != 'enabled':
            out.append(f'{unit} is {state}: apt does not run on its schedule')
    return out


def enabled(host, unit):
    code, out, err = host.run(['systemctl', 'is-enabled', unit], timeout=10)
    word = (out.strip().splitlines() or [''])[0]
    if word:
        return word
    return f'unknown ({(err.strip() or f"exit {code}")[:80]})'


def policy_drift(host, cfg, s, state):
    out = []
    problem = aptconf.restart_problem(cfg) if isinstance(cfg, dict) else None
    if problem:
        out.append(problem)
    for unit in OWN_UNITS + (('update-policy-exposure.timer',) if s['exposure']['enabled'] else ()):
        word = enabled(host, unit)
        if word != 'enabled':
            out.append(f'{unit} is installed but {word}, not enabled: pending restarts are neither made nor '
                       f'reported (update-policy-install install ... --timer enables them)')
    for unit in WATCHED:
        code, text, _err = host.run(['systemctl', 'is-failed', unit], timeout=10)
        if (text.strip().splitlines() or [''])[0] == 'failed':
            out.append(f'{unit} failed its last run (journalctl -u {unit[:-len(".service")]})')
    if state.get('lastAlertError'):
        out.append(f'the last alert failed: {state["lastAlertError"]}')
    return out


def pending_drift(pending, s, now, state=None, boot=''):
    pending, _seen = tick.first_seen(state or {}, pending, boot)
    if pending is None:
        return []
    days = (now - pending.since).total_seconds() / 86400
    if days <= s['baseline']['pendingDays']:
        return []
    who = 'a live machine: restart it in a planned window' if s['role'] not in settings_mod.RESTARTS else \
        'never idle in the quiet window? (journalctl -u update-policy)'
    return [f'{pending.text(now)}: more than {s["baseline"]["pendingDays"]} days; {who}']


# -- listening ports --------------------------------------------------------------------------------------------------

def _address(hexaddr, family):
    raw = bytes.fromhex(hexaddr)
    if family == 4:
        return ipaddress.IPv4Address(raw[::-1])
    words = b''.join(raw[i:i + 4][::-1] for i in range(0, 16, 4))
    a = ipaddress.IPv6Address(words)
    return a.ipv4_mapped or a


def listeners(host):
    """[(proto, address, port, inode)] of the sockets that listen (TCP) or wait unconnected (UDP)."""
    out = set()
    for proto, path, family in NET_FILES:
        try:
            with open(host.p(path), encoding='ascii') as f:
                lines = f.read().splitlines()[1:]
        except FileNotFoundError:
            continue            # no IPv6 in this kernel: nothing listens there
        for line in lines:
            parts = line.split()
            if len(parts) < 10:
                continue
            local, remote, st, inode = parts[1], parts[2], parts[3], parts[9]
            if proto == 'tcp' and st != TCP_LISTEN:
                continue
            if proto == 'udp' and (st != UDP_UNCONNECTED or remote.split(':')[1] != '0000'):
                continue
            addr, port = local.split(':')
            out.add((proto, _address(addr, family), int(port, 16), inode))
    return sorted(out, key=lambda x: (x[0], x[2], str(x[1])))


def socket_owners(host, inodes):
    """{inode: process name} for the sockets asked about (read from /proc/*/fd, as root)."""
    names, want = {}, {f'socket:[{i}]' for i in inodes}
    try:
        pids = [n for n in os.listdir(host.p('/proc')) if n.isdigit()]
    except OSError:
        return names
    for pid in pids:
        fd_dir = host.p(f'/proc/{pid}/fd')
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target in want:
                try:
                    with open(host.p(f'/proc/{pid}/comm'), encoding='utf-8', errors='replace') as f:
                        names.setdefault(target[8:-1], f.read().strip())
                except OSError:
                    pass
    return names


def allowed(entries, proto, addr, port):
    for e_proto, low, high, net in entries:
        if e_proto != proto or not low <= port <= high:
            continue
        if net is None:
            return True
        if addr.is_unspecified:     # every address: only an entry for every address allows it
            if net.prefixlen == 0 and net.version == addr.version:
                return True
            continue
        if addr.version == net.version and addr in net:
            return True
    return False


def ports_drift(host, s):
    b = s['baseline']
    if b['ports'] is None:
        return []
    entries = [settings_mod.parse_port(x) for x in b['ports']]
    bad = []
    for proto, addr, port, inode in listeners(host):
        if b['ignoreLoopback'] and addr.is_loopback:
            continue
        if not allowed(entries, proto, addr, port):
            bad.append((proto, addr, port, inode))
    owners = socket_owners(host, [x[3] for x in bad]) if bad else {}
    out, seen = [], set()
    for proto, addr, port, inode in bad:
        shown = f'[{addr}]' if addr.version == 6 else str(addr)
        key = (proto, shown, port)
        if key in seen:
            continue
        seen.add(key)
        who = f' ({owners[inode]})' if inode in owners else ''
        out.append(f'listening on {proto} {shown}:{port}{who}: not in baseline.ports')
    return out


# -- ssh and disks ----------------------------------------------------------------------------------------------------

def _sshd_lines(host, extra, who):
    code, out, err = host.run(['sshd', '-T'] + extra, timeout=20)
    if code is None and 'not installed' in err:
        return None
    if code != 0:
        return [f'cannot read sshd\'s settings for {who} (sshd -T: {(err.strip() or f"exit {code}")[:160]})']
    cfg = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition(' ')
        cfg.setdefault(key.lower(), value.strip().lower())
    found = []
    if cfg.get('passwordauthentication') == 'yes':
        found.append(f'ssh allows password login for {who} (PasswordAuthentication yes)')
    kbd = cfg.get('kbdinteractiveauthentication', cfg.get('challengeresponseauthentication'))
    if kbd == 'yes' and cfg.get('usepam') == 'yes':
        found.append(f'ssh allows password login through PAM for {who} (KbdInteractiveAuthentication yes, '
                     'UsePAM yes)')
    return found


def ssh_drift(host, s):
    """sshd's effective settings (`sshd -T`), and with `-C` for each of baseline.sshUsers connecting from outside,
    so a Match block that turns passwords back on is seen. No sshd: nothing to check."""
    found = _sshd_lines(host, [], 'every connection')
    if found is None:
        return []
    for user in s['baseline']['sshUsers']:
        found += _sshd_lines(host, ['-C', f'user={user},{OUTSIDE}'], f'{user} from outside') or []
    return list(dict.fromkeys(found))


def disk_drift(host, s):
    out = []
    limit = s['baseline']['diskPercent']
    for path in s['baseline']['diskPaths']:
        try:
            v = os.statvfs(host.p(path))
        except OSError as e:
            out.append(f'cannot read the disk of {path} ({e.strerror})')
            continue
        used = (v.f_blocks - v.f_bfree) * v.f_frsize
        total = used + v.f_bavail * v.f_frsize
        pct = -(-used * 100 // total) if total else 0       # rounded up, as df does
        if pct > limit:
            out.append(f'disk {path} is {pct}% full (more than {limit}%)')
    return out


def alert_drift(s):
    """A machine with no alert channel: every alert goes only to the journal, which nobody reads in time."""
    if s['alertCommand']:
        return []
    return ['no alert channel: alerts go only to the journal (add the machine file\'s "alert" section, then run '
            '`update-policy test-alert` as root)']


def _guarded(name, fn):
    """One check; one that breaks is a drift line, and the others still run."""
    try:
        return fn()
    except Exception as e:      # noqa: BLE001 - a baseline that crashes would report nothing at all
        return [f'cannot read {name} ({type(e).__name__}: {e})']


def check(host, m, s, state, now):
    cfg = _guarded('apt\'s settings', lambda: [aptconf.dump(host)])[0]     # a string: why it cannot be read
    lines = []
    for name, fn in (('the update settings', lambda: updates_drift(host, cfg)),
                     ('the restart policy', lambda: policy_drift(host, cfg, s, state)),
                     ('the pending restart', lambda: pending_drift(m.pending(), s, now, state, m.boot_id())),
                     ('the listening ports', lambda: ports_drift(host, s)),
                     ('sshd\'s settings', lambda: ssh_drift(host, s)),
                     ('the disks', lambda: disk_drift(host, s)),
                     ('the alert channel', lambda: alert_drift(s))):
        lines += _guarded(name, fn)
    return [settings_mod.one_line(x, 400) for x in lines]


def run(host, m, read_settings):
    """Prints the drift lines; alerts with them; 0 none, 1 drift (or the machine file cannot be used)."""
    try:
        s = read_settings()
    except settings_mod.MachineFileError as e:
        print(f'update-policy baseline: the machine file cannot be used, nothing checked: {e}')
        return 1
    now = m.now()
    state = m.read_state()
    lines = check(host, m, s, state, now)
    for line in lines:
        print(line)
    if lines and s['alertCommand']:     # without one, the lines above are in the journal already
        err = tick.send_alert(m, s, 'drift', '\n'.join(lines))
        if err is not None or state.get('lastAlertError'):
            with m.lock() as mine:
                if mine:
                    st = m.read_state()
                    st['lastAlertError'] = err
                    m.write_state(st)
    return 1 if lines else 0
