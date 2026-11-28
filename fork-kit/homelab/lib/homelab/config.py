"""homelab.config: the machine file's "homelab" section (fork-kit/homelab/README.md, The machine file).

  "homelab": {
    "leaseDir": "/var/lib/fork-homelab/leases",
    "machines": [
      {"name": "test-1", "host": "test-1.example", "port": 22, "roles": ["test"],
       "lease": {"defaultMinutes": 60, "maxMinutes": 480}},
      {"name": "build-1", "host": "192.0.2.20", "roles": ["build"]}
    ]
  }

The names, hosts and addresses of real machines live only here, in the private machine file; the public kit and
its example name none. A machine with "lease" can be leased; one without it cannot. Anything the kit does not
fully understand is refused whole (machine_file's rules): a typo is never ignored. Plain Python 3.
"""
import ipaddress, os, re, sys

_kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_common = os.path.join(_kit, 'common')
sys.path.insert(0, _common if os.path.isfile(os.path.join(_common, 'machine_file.py'))
                else os.path.join(os.path.dirname(_kit), 'common'))
import machine_file  # noqa: E402

KIT = 'homelab'
MACHINE_PATH = None   # tests only; shipped commands read the fixed path
OWNER_UID = 0         # tests only
REQ = machine_file.REQUIRED
MachineFileError = machine_file.MachineFileError

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,62}$')
HOST_RE = re.compile(r'^(?=.{1,253}$)[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?'
                     r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*$')
MAX_LEASE_MINUTES = 7 * 24 * 60


def _name(v):
    return None if NAME_RE.match(v) else 'must be lower-case letters, digits and -, at most 63 (it is %r)' % v


def _host(v):
    try:
        ipaddress.ip_address(v)
        return None
    except ValueError:
        pass
    return None if HOST_RE.match(v) else 'must be a host name or an IP address (it is %r)' % v


def _port(v):
    return None if 1 <= v <= 65535 else 'must be 1 to 65535'


def _roles(v):
    for r in v:
        if not isinstance(r, str) or not NAME_RE.match(r):
            return 'each role must be lower-case letters, digits and - (it has %r)' % (r,)
    return 'a role is given twice' if len(set(v)) != len(v) else None


def _lease_dir(v):
    return None if os.path.isabs(v) and '..' not in v.split('/') else 'must be an absolute path without ..'


def _minutes(v):
    return None if 1 <= v <= MAX_LEASE_MINUTES else f'must be 1 to {MAX_LEASE_MINUTES} (7 days)'


def lease_rules(obj, where):
    s = machine_file.fields(obj, {
        'defaultMinutes': (int, 60, _minutes),
        'maxMinutes': (int, 480, _minutes),
    }, where)
    if s['defaultMinutes'] > s['maxMinutes']:
        raise MachineFileError(f'{where}.defaultMinutes: more than maxMinutes ({s["maxMinutes"]})')
    return s


def machine(obj, where):
    m = machine_file.fields(obj, {
        'name': (str, REQ, _name),
        'host': (str, REQ, _host),
        'port': (int, 22, _port),
        'roles': (list, [], _roles),
        'note': (str, '', None),
        'lease': (dict, None, None),
    }, where)
    if m['lease'] is not None:
        m['lease'] = lease_rules(m['lease'], f'{where}.lease')
    return m


def parse(section, where):
    """The checked section: leaseDir (None when no machine can be leased) and machines, in file order."""
    s = machine_file.fields(section, {
        'leaseDir': (str, None, _lease_dir),
        'machines': (list, REQ, None),
    }, where)
    s['machines'] = [machine(m, f'{where}.machines[{i}]') for i, m in enumerate(s['machines'])]
    names = [m['name'] for m in s['machines']]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise MachineFileError(f'{where}.machines: the name {twice[0]!r} is given twice')
    if any(m['lease'] for m in s['machines']) and not s['leaseDir']:
        raise MachineFileError(f'{where}: "leaseDir" is missing (a machine has "lease")')
    return s


def load():
    """The machine file's homelab section, checked. Raises MachineFileError (a missing file or section too)."""
    path = MACHINE_PATH or machine_file.default_path()
    try:
        section = machine_file.read_section(KIT, path=path, owner_uid=OWNER_UID)
    except MachineFileError as e:
        if 'not there' in str(e):
            raise MachineFileError(f'{path}: not there. The owner writes it as root; the "homelab" section is in '
                                   'fork-kit/homelab/README.md and fork-kit/common/machine.example.json') from None
        raise
    return parse(section, f'{path}: {KIT}')


def find(cfg, name):
    for m in cfg['machines']:
        if m['name'] == name:
            return m
    known = ', '.join(m['name'] for m in cfg['machines']) or 'none'
    raise MachineFileError(f'no machine {name!r} in the machine file (known: {known})')
