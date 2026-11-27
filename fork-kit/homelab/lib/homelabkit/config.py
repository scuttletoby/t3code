"""The "homelab" section of the private machine file (fork-kit/homelab/README.md, fork-kit/common/README.md).

  "homelab": {
    "machines": [{"name": "test-a", "host": "test-a.lan.example", "port": 22, "note": "..."}],
    "leases": {"dir": "/var/lib/fork-kit/homelab/leases", "defaultMinutes": 60, "maxMinutes": 480},
    "checkTimeoutSeconds": 3
  }

Machine names, hosts and the lease folder are facts of one machine: they live only there, never in the kit.
"""
import importlib.machinery, importlib.util, os, re

KIT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _load_machine_file():
    """fork-kit/common/machine_file.py: in the version folder once installed, next to the kit in the repository."""
    for folder in (os.path.join(KIT, 'common'), os.path.join(os.path.dirname(KIT), 'common')):
        path = os.path.join(folder, 'machine_file.py')
        if os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader('fork_kit_machine_file', path)
            spec = importlib.util.spec_from_loader('fork_kit_machine_file', loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise ImportError('fork-kit/common/machine_file.py not found next to the homelab kit')


machine_file = _load_machine_file()
MachineFileError = machine_file.MachineFileError
REQUIRED = machine_file.REQUIRED

NAME = re.compile(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?')        # also the lease file's name
HOST = re.compile(r'[A-Za-z0-9_](?:[A-Za-z0-9_.:-]{0,252})')       # a DNS name or an address; never an option


def _name(v):
    return None if NAME.fullmatch(v) else 'must be lowercase letters, digits and inner dashes (at most 63)'


def _host(v):
    return None if HOST.fullmatch(v) else 'must be a host name or address (letters, digits, . : - _)'


def _port(v):
    return None if 1 <= v <= 65535 else 'must be 1 to 65535'


def _absolute(v):
    return None if os.path.isabs(v) and '/../' not in v + '/' else 'must be an absolute path'


def _between(low, high):
    return lambda v: None if low <= v <= high else f'must be {low} to {high}'


SECTION = {'machines': (list, REQUIRED, lambda v: None if v else 'must name at least one machine'),
           'leases': (dict, REQUIRED, None),
           'checkTimeoutSeconds': (int, 3, _between(1, 60))}
MACHINE = {'name': (str, REQUIRED, _name),
           'host': (str, REQUIRED, _host),
           'port': (int, 22, _port),
           'note': (str, '', None)}
LEASES = {'dir': (str, REQUIRED, _absolute),
          'defaultMinutes': (int, 60, _between(1, 7 * 24 * 60)),
          'maxMinutes': (int, 480, _between(1, 7 * 24 * 60))}


def load(path=None, owner_uid=0):
    """The checked homelab settings: {'path', 'machines': [...], 'leases': {...}, 'checkTimeoutSeconds'}. Raises
    MachineFileError naming the file and the key. `path` and `owner_uid` are for tests."""
    path = path or machine_file.default_path()
    where = f'{path}: homelab'
    s = machine_file.fields(machine_file.read_section('homelab', path, owner_uid), SECTION, where)
    machines, seen = [], set()
    for i, m in enumerate(s['machines']):
        m = machine_file.fields(m, MACHINE, f'{where}.machines[{i}]')
        if m['name'] in seen:
            raise MachineFileError(f'{where}.machines[{i}].name: {m["name"]!r} is given twice')
        seen.add(m['name'])
        machines.append(m)
    leases = machine_file.fields(s['leases'], LEASES, f'{where}.leases')
    if leases['defaultMinutes'] > leases['maxMinutes']:
        raise MachineFileError(f'{where}.leases.defaultMinutes: {leases["defaultMinutes"]} is more than maxMinutes '
                               f'({leases["maxMinutes"]})')
    return {'path': path, 'machines': machines, 'leases': leases, 'checkTimeoutSeconds': s['checkTimeoutSeconds']}
