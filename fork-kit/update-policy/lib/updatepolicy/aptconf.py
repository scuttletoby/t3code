"""apt's effective settings, as `apt-config dump` prints them after reading every file (the tick reads them every time,
so an override that loses to a later file is seen within 5 minutes, not only by the daily baseline)."""
import re

LINE = re.compile(r'^(\S+) "(.*)";$')
# apt's own booleans (StringToBool): anything else is neither, and counts as "not false" for Automatic-Reboot
TRUE = ('1', 'yes', 'true', 'with', 'on', 'enable')
FALSE = ('0', 'no', 'false', 'without', 'off', 'disable')
REBOOT = 'Unattended-Upgrade::Automatic-Reboot'


def dump(host):
    """{key: [values]} (a list key gives one value per item), or a string saying why it cannot be read."""
    code, out, err = host.run(['apt-config', 'dump'], timeout=20)
    if code != 0:
        return f'cannot read apt\'s settings ({(err.strip() or f"exit {code}")[:160]})'
    found = {}
    for line in out.splitlines():
        m = LINE.match(line.strip())
        if m:
            found.setdefault(m.group(1), []).append(m.group(2))
    return found


def last(cfg, key):
    values = cfg.get(key) or ['']
    return values[-1].strip().lower()


def restart_problem(cfg):
    """None when unattended-upgrades does not restart by itself; else why (a string)."""
    if isinstance(cfg, str):
        return cfg + ': cannot tell whether unattended-upgrades restarts by itself'
    value = last(cfg, REBOOT)
    if value in ('',) + FALSE:
        return None
    return (f'unattended-upgrades restarts by itself ({REBOOT} is "{value}"): a file read after the kit\'s '
            'zz-update-policy (a later apt.conf.d file, or /etc/apt/apt.conf) overrides it; remove that line')
