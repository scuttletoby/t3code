"""The daily baseline check: compares the machine with the policy and prints only what drifted.

check(host, settings, state, now) returns the drift lines ([]: none):

- unattended-upgrades is installed and on (APT::Periodic), its timers are enabled, and it takes the security
  pocket (an Allowed-Origins or Origins-Pattern entry that names security);
- the restart policy matches the role: unattended-upgrades never restarts by itself (Automatic-Reboot false: this
  kit owns restarts), the kit's timer and boot look are enabled, and the last alert went out;
- no restart pending for more than baseline.pendingDays days;
- no listening port outside baseline.ports (when the list is given);
- no password login over ssh (PasswordAuthentication, or keyboard-interactive through PAM);
- no disk of baseline.diskPaths fuller than baseline.diskPercent.

A fact it cannot read is drift too ("cannot read ..."), never a pass.
"""
import ipaddress, os, re

from updatepolicy import settings as settings_mod, tick

APT_LINE = re.compile(r'^(\S+) "(.*)";$')
TRUE = ('1', 'true', 'yes', 'on', 'always')
TIMERS = ('apt-daily.timer', 'apt-daily-upgrade.timer')
OWN_UNITS = ('update-policy.timer', 'update-policy-boot.service')
TCP_LISTEN, UDP_UNCONNECTED = '0A', '07'
NET_FILES = (('tcp', '/proc/net/tcp', 4), ('tcp', '/proc/net/tcp6', 6), ('udp', '/proc/net/udp', 4),
             ('udp', '/proc/net/udp6', 6))


def apt_config(host):
    """{key: [values]} from `apt-config dump` (a list key gives one value per item), or a string saying why not."""
    code, out, err = host.run(['apt-config', 'dump'], timeout=20)
    if code != 0:
        return f'cannot read apt\'s settings ({(err.strip() or f"exit {code}")[:160]})'
    found = {}
    for line in out.splitlines():
        m = APT_LINE.match(line.strip())
        if m:
            found.setdefault(m.group(1), []).append(m.group(2))
    return found


def _last(cfg, key):
    values = cfg.get(key) or ['']
    return values[-1].strip().lower()


def updates_drift(host, cfg):
    out = []
    if not os.path.exists(host.p('/usr/bin/unattended-upgrade')):
        out.append('unattended-upgrades is not installed (apt install unattended-upgrades)')
    if isinstance(cfg, str):
        return out + [cfg]
    if _last(cfg, 'APT::Periodic::Enable') in ('0', 'false', 'no'):
        out.append('APT::Periodic::Enable is off: no automatic updates at all')
    for key in ('APT::Periodic::Update-Package-Lists', 'APT::Periodic::Unattended-Upgrade'):
        if _last(cfg, key) in ('', '0'):
            out.append(f'{key} is off: security updates are not installed by themselves '
                       '(dpkg-reconfigure -plow unattended-upgrades)')
    origins = [v for k, vs in cfg.items() for v in vs
               if k in ('Unattended-Upgrade::Allowed-Origins::', 'Unattended-Upgrade::Origins-Pattern::')]
    if not any('security' in v.lower() for v in origins):
        out.append('unattended-upgrades takes no security origin (Allowed-Origins / Origins-Pattern)')
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
    if isinstance(cfg, dict) and _last(cfg, 'Unattended-Upgrade::Automatic-Reboot') in TRUE:
        out.append('unattended-upgrades restarts by itself (Unattended-Upgrade::Automatic-Reboot is true): the '
                   'restart policy is bypassed; a later apt.conf file overrides the kit\'s (apt-config dump)')
    for unit in OWN_UNITS:
        word = enabled(host, unit)
        if word != 'enabled':
            out.append(f'{unit} is {word}: pending restarts are neither made nor reported '
                       f'(systemctl enable --now {unit})')
    if state.get('lastAlertError'):
        out.append(f'the last alert failed: {state["lastAlertError"]}')
    return out


def pending_drift(pending, s, now):
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
            continue
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

def ssh_drift(host):
    code, out, err = host.run(['sshd', '-T'], timeout=20)
    if code is None and 'not installed' in err:
        return []
    if code != 0:
        return [f'cannot read sshd\'s settings (sshd -T: {(err.strip() or f"exit {code}")[:160]})']
    cfg = {}
    for line in out.splitlines():
        key, _, value = line.strip().partition(' ')
        cfg.setdefault(key.lower(), value.strip().lower())
    found = []
    if cfg.get('passwordauthentication') == 'yes':
        found.append('ssh allows password login (PasswordAuthentication yes)')
    kbd = cfg.get('kbdinteractiveauthentication', cfg.get('challengeresponseauthentication'))
    if kbd == 'yes' and cfg.get('usepam') == 'yes':
        found.append('ssh allows password login through PAM (KbdInteractiveAuthentication yes, UsePAM yes)')
    return found


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


def check(host, m, s, state, now):
    cfg = apt_config(host)
    return (updates_drift(host, cfg) + policy_drift(host, cfg, s, state) + pending_drift(m.pending(), s, now)
            + ports_drift(host, s) + ssh_drift(host) + disk_drift(host, s))


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
