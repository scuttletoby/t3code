"""The update-policy section of the machine file (fork-kit/update-policy/README.md, "Machine file").

read() returns the checked settings, or raises machine_file.MachineFileError naming the file and the key. Nothing
here has a default for the facts of one machine (its role, its quiet hour and its time zone): a file without them
is refused, and a refused file never restarts anything.
"""
import fnmatch, ipaddress, os, re, unicodedata
import machine_file
from machine_file import REQUIRED, MachineFileError

SECTION = 'update-policy'
ROLES = ('dev', 'personal', 'live')
RESTARTS = ('dev', 'personal')          # roles that may restart themselves; live never does
HHMM_RE = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])\Z')
USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}\Z')
SLICE_RE = re.compile(r'^[A-Za-z0-9_.:-]{1,200}\.slice\Z')
PORT_RE = re.compile(r'^(tcp|udp)/([0-9]{1,5})(?:-([0-9]{1,5}))?(?:@(.+))?\Z')
# values that look like credentials: GitHub, OpenAI-style and Slack tokens, and long hex or base64 runs. A secret in
# the alert command's argv is readable by every user in /proc while it runs: keep it in a root-only file the
# command reads (README, Machine file).
TOKEN_RES = (re.compile(r'(gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}'), re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'),
             re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{8,}'), re.compile(r'\b[0-9a-fA-F]{32,}\b'),
             re.compile(r'(?<![/A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]*[0-9])(?=[A-Za-z0-9+_=-]*[a-z])'
                        r'(?=[A-Za-z0-9+_=-]*[A-Z])[A-Za-z0-9+_=-]{32,}(?![/A-Za-z0-9+_=-])'))


def one_line(text, limit=300):
    """Text from elsewhere (a user's inhibitor reason, a container name, a script's answer) made safe for one journal
    line and the alert text: control and format characters escaped (a newline cannot start a forged record), and
    cut to `limit` characters."""
    out = []
    for c in str(text):
        if c == '\\':
            out.append('\\\\')
        elif unicodedata.category(c)[0] == 'C':
            out.append(f'\\x{ord(c):02x}' if ord(c) < 0x100 else f'\\u{ord(c):04x}')
        else:
            out.append(c)
    text = ''.join(out)
    return text if len(text) <= limit else text[:limit - 3] + '...'


def _role(value):
    return None if value in ROLES else f'must be one of {", ".join(ROLES)}'


def _quiet_hour(value):
    return None if HHMM_RE.match(value) else 'must be HH:MM (24 hours, for example "05:00")'


def _zone(value):
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(value)
    except Exception:       # ValueError, ZoneInfoNotFoundError, ImportError (Python before 3.9)
        return 'must be a time zone name this machine knows (for example "Europe/London" or "UTC")'
    return None


def _between(low, high):
    return lambda v: None if low <= v <= high else f'must be from {low} to {high}'


def _absolute_command(value):
    if not value:
        return None
    if not all(isinstance(x, str) and x and '\0' not in x for x in value):
        return 'must be a list of strings'
    for x in value[1:]:
        if any(r.search(x) for r in TOKEN_RES):
            return ('an argument looks like a token or key: every user can read the arguments while the command runs; '
                    'keep the secret in a root-only file the command reads')
    return None if os.path.isabs(value[0]) else 'its first string must be an absolute path to a program'


def _users(value):
    bad = [x for x in value if not isinstance(x, str) or not USER_RE.match(x)]
    return f'{bad[0]!r} is not a user name' if bad else None


def _patterns(value):
    bad = [x for x in value if not isinstance(x, str) or not x]
    return 'must be a list of names or patterns (*, ?)' if bad else None


def _paths(value):
    bad = [x for x in value if not isinstance(x, str) or not os.path.isabs(x) or '\0' in x]
    return f'{bad[0]!r} is not an absolute path' if bad else None


def _slice(value):
    return None if SLICE_RE.match(value) else 'must be a slice name (it ends in .slice)'


def parse_port(text):
    """'tcp/22', 'udp/60000-61000', 'tcp/3773@100.64.0.0/10', 'tcp/8080@127.0.0.1' -> (proto, low, high, network or
    None). Raises ValueError."""
    m = PORT_RE.match(text) if isinstance(text, str) else None
    if not m:
        raise ValueError(f'{text!r} is not PROTO/PORT[-PORT][@ADDRESS] (for example "tcp/22")')
    low = int(m.group(2))
    high = int(m.group(3)) if m.group(3) else low
    if not 0 < low <= high <= 65535:
        raise ValueError(f'{text!r}: ports are 1 to 65535, the first not above the second')
    net = ipaddress.ip_network(m.group(4), strict=False) if m.group(4) else None
    return m.group(1), low, high, net


def _ports(value):
    if value is None:
        return None
    if not isinstance(value, list):
        return 'must be a list (or null: no port check)'
    for x in value:
        try:
            parse_port(x)
        except ValueError as e:
            return str(e)
    return None


BASELINE = {
    'ports': ((list, type(None)), None, _ports),
    'ignoreLoopback': (bool, True, None),
    'diskPercent': (int, 90, _between(1, 100)),
    'diskPaths': (list, ['/'], _paths),
    'pendingDays': (int, 3, _between(1, 60)),
    'sshUsers': (list, ['root'], _users),
}

def _ignore(value):
    for e in value:
        if not isinstance(e, dict) or set(e) != {'cve', 'reason', 'until'}:
            return 'each entry is {"cve": "CVE-...", "reason": "...", "until": "YYYY-MM-DD"}'
        if not isinstance(e['cve'], str) or not re.match(r'^CVE-[0-9]{4}-[0-9]{4,}\Z', e['cve']):
            return f'{e["cve"]!r} is not a CVE id'
        if not isinstance(e['reason'], str) or not e['reason'].strip():
            return f'{e["cve"]}: a reason is needed'
        if not isinstance(e['until'], str) or not re.match(r'^[0-9]{4}-[0-9]{2}-[0-9]{2}\Z', e['until']):
            return f'{e["cve"]}: "until" must be YYYY-MM-DD'
    return None


def _https(value):
    return None if value == '' or re.match(r'^https://[^\s]+\Z', value) else 'must be an https:// URL (or "")'


def _share(value):
    return None if 0 <= value <= 1 else 'must be from 0 to 1'


EXPOSURE = {
    'enabled': (bool, False, None),
    'epssMin': ((int, float), 0.1, _share),
    'fixWaitingDays': (int, 3, _between(0, 60)),
    'staleDays': (int, 3, _between(1, 60)),
    'maxFeedMb': (int, 64, _between(1, 1024)),
    'maxXmlMb': (int, 1024, _between(16, 4096)),
    'timeoutSeconds': (int, 60, _between(5, 600)),
    'ovalUrl': (str, '', _https),
    'kevUrl': (str, '', _https),
    'epssUrl': (str, '', _https),
    'ignore': (list, [], _ignore),
}

SPEC = {
    'role': (str, REQUIRED, _role),
    'quietHour': (str, REQUIRED, _quiet_hour),
    'timeZone': (str, REQUIRED, _zone),
    'windowMinutes': (int, 120, _between(5, 720)),
    'retryMinutes': (int, 15, _between(5, 240)),
    'alertCommand': (list, [], _absolute_command),
    'alertOnBoot': (bool, False, None),
    'containerUsers': (list, [], _users),
    'systemDocker': (bool, True, None),
    'ignoreContainers': (list, [], _patterns),
    'heavySlice': (str, 'heavy.slice', _slice),
    'busyScripts': (list, [], _paths),
    'baseline': (dict, {}, None),
    'exposure': (dict, {}, None),
}


def check(section, where):
    """The section as a dict with every default filled in (baseline too)."""
    out = machine_file.fields(section, SPEC, where)
    out['baseline'] = machine_file.fields(out['baseline'], BASELINE, f'{where}.baseline')
    out['exposure'] = machine_file.fields(out['exposure'], EXPOSURE, f'{where}.exposure')
    return out


def read(path=None, owner_uid=0):
    where = f'{path or machine_file.default_path()}: {SECTION}'
    return check(machine_file.read_section(SECTION, path=path, owner_uid=owner_uid), where)


def ignored_container(settings, name):
    return any(fnmatch.fnmatchcase(name, p) for p in settings['ignoreContainers'])


__all__ = ['MachineFileError', 'ROLES', 'RESTARTS', 'SECTION', 'check', 'read', 'parse_port', 'ignored_container',
           'one_line']
