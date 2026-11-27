"""gardener.config: the machine file's "gardener" section (fork-kit/gardener/README.md, Config).

  "gardener": {
    "stateDir": "~/.local/state/gardener",
    "gh": "gh",
    "projects": [{
      "name": "example", "repo": "OWNER/REPO", "reportRepo": "OWNER/REPO", "root": "/srv/example/repo",
      "schedule": "04:00", "quietHours": "02:00-07:00", "timeZone": "Europe/London",
      "model": {"endpoint": "http://127.0.0.1:8080/v1", "model": "example-model"},
      "checks": [{"name": "context-budget", "options": {...}},
                 {"path": "/srv/example/bin/doc-check", "id": "docs", "timeoutSeconds": 120}]
    }]
  }

A check is a general one shipped with the kit ("name") or a project's own script ("path"); the kit never names a
project's checks. Anything it does not fully understand is refused whole (machine_file's rules): a typo is never
ignored. Paths are absolute or start with "~/" (the home of the user the gardener runs as). Plain Python 3.
"""
import os, re, sys

_kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_common = os.path.join(_kit, 'common')
sys.path.insert(0, _common if os.path.isfile(os.path.join(_common, 'machine_file.py'))
                else os.path.join(os.path.dirname(_kit), 'common'))
import machine_file  # noqa: E402

KIT = 'gardener'
MACHINE_PATH = None   # tests only; shipped commands read the fixed path
OWNER_UID = 0         # tests only
REQ = machine_file.REQUIRED
MachineFileError = machine_file.MachineFileError

CHECKS_DIR = os.path.join(_kit, 'checks')
GENERAL_CHECKS = ('context-budget', 'facts', 'install-drift', 'machine-tools', 'review-tiers', 'workflows-off',
                  'worktrees')
NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,63}$')
REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}$')
TIME_RE = re.compile(r'^([01][0-9]|2[0-3]):([0-5][0-9])$')
COMMAND_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')


def _path(value):
    if not (os.path.isabs(value) or value.startswith('~/')) or '\0' in value:
        return 'must be an absolute path or start with "~/"'
    return None


def _path_or_empty(value):
    return _path(value) if value else None


def _name(value):
    return None if NAME_RE.match(value) and value not in ('.', '..') else \
        'must be lower case letters, digits, ".", "_" or "-" (at most 64)'


def _repo(value):
    return None if not value or (REPO_RE.match(value) and value.split('/')[1] not in ('.', '..')) else \
        'must be OWNER/REPO'


def _time(value):
    return None if TIME_RE.match(value) else 'must be HH:MM (24 hours)'


def _time_or_empty(value):
    return _time(value) if value else None


def _quiet(value):
    if not value:
        return None
    parts = value.split('-')
    if len(parts) != 2 or not all(TIME_RE.match(p) for p in parts) or parts[0] == parts[1]:
        return 'must be "HH:MM-HH:MM" (two different times; it may wrap past midnight)'
    return None


def _zone(value):
    if not value:
        return None
    try:
        import zoneinfo
        zoneinfo.ZoneInfo(value)
    except Exception:
        return 'is not a time zone this machine knows'
    return None


def _command(value):
    return None if COMMAND_RE.match(value) or not _path(value) else \
        'must be a command name (found on PATH) or an absolute path'


def _endpoint(value):
    return None if re.match(r'^https?://[^\s/]+(/[^\s]*)?$', value) else 'must be an http:// or https:// URL'


def _seconds(lo, hi):
    return lambda v: None if lo <= v <= hi else f'must be {lo} to {hi}'


def model(obj, where):
    return machine_file.fields(obj, {
        'endpoint': (str, REQ, _endpoint),
        'model': (str, REQ, lambda v: None if v.strip() else 'must not be empty'),
        'timeoutSeconds': (int, 120, _seconds(1, 3600)),
        'maxFiles': (int, 5, _seconds(0, 50)),
        'maxFileBytes': (int, 20000, _seconds(0, 200000)),
    }, where)


def check(obj, where):
    c = machine_file.fields(obj, {
        'name': (str, '', None),
        'path': (str, '', _path_or_empty),
        'id': (str, '', lambda v: _name(v) if v else None),
        'options': (dict, None, None),
        'fix': (bool, False, None),
        'timeoutSeconds': (int, 300, _seconds(1, 3600)),
        'publicText': (bool, False, None),
    }, where)
    if bool(c['name']) == bool(c['path']):
        raise MachineFileError(f'{where}: give exactly one of "name" (a general check: {", ".join(GENERAL_CHECKS)}) '
                               'or "path" (a project check)')
    if c['name'] and c['name'] not in GENERAL_CHECKS:
        raise MachineFileError(f'{where}.name: {c["name"]!r} is not a general check '
                               f'(known: {", ".join(GENERAL_CHECKS)}); a project check is given by "path"')
    if c['name'] and c['publicText']:
        raise MachineFileError(f'{where}.publicText: only for a project check ("path"); a general check\'s text is '
                               'always shown')
    c['options'] = c['options'] or {}
    c['id'] = c['id'] or c['name'] or _name_from_path(c['path'])
    return c


def _name_from_path(path):
    base = re.sub(r'[^a-z0-9._-]+', '-', os.path.basename(path).lower()).strip('-.') or 'check'
    return base[:64]


def project(obj, where):
    p = machine_file.fields(obj, {
        'name': (str, REQ, _name),
        'repo': (str, '', _repo),
        'reportRepo': (str, '', _repo),
        'root': (str, '', _path_or_empty),
        'schedule': (str, '', _time_or_empty),
        'quietHours': (str, '', _quiet),
        'timeZone': (str, '', _zone),
        'model': (dict, None, None),
        'checks': (list, REQ, None),
    }, where)
    p['model'] = model(p['model'], f'{where}.model') if p['model'] is not None else None
    p['checks'] = [check(c, f'{where}.checks[{i}]') for i, c in enumerate(p['checks'])]
    ids = [c['id'] for c in p['checks']]
    twice = sorted({i for i in ids if ids.count(i) > 1})
    if twice:
        raise MachineFileError(f'{where}.checks: the id {twice[0]!r} is given twice (set "id" on one of them)')
    return p


def parse(section, where):
    """The checked section with defaults filled in. Raises MachineFileError."""
    s = machine_file.fields(section, {
        'stateDir': (str, '~/.local/state/gardener', _path),
        'gh': (str, 'gh', _command),
        'projects': (list, REQ, None),
    }, where)
    s['projects'] = [project(p, f'{where}.projects[{i}]') for i, p in enumerate(s['projects'])]
    names = [p['name'] for p in s['projects']]
    twice = sorted({n for n in names if names.count(n) > 1})
    if twice:
        raise MachineFileError(f'{where}.projects: the name {twice[0]!r} is given twice')
    return s


def load():
    """The machine file's gardener section, checked. Raises MachineFileError (a missing file or section too)."""
    path = MACHINE_PATH or machine_file.default_path()
    section = machine_file.read_section(KIT, path=path, owner_uid=OWNER_UID)
    return parse(section, f'{path}: {KIT}')


def expand(path, home=None):
    """A configured path for the user who runs the gardener ("~/" is that user's home)."""
    if path.startswith('~/'):
        return os.path.join(home or os.path.expanduser('~'), path[2:])
    return path
