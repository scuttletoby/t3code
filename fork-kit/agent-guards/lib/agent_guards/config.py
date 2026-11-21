"""agent_guards.config: the facts of the agent guard hooks, from the machine file's "agent-guards" section.

Every key is optional; a missing machine file or section gives the built-in defaults below (the values the hooks
had before they moved into this kit). A broken file or section never weakens a guard below those defaults: the hook
uses them and names the problem on stderr (`agent-guards self-check` names it too).

  "agent-guards": {
    "actionsDisable": {"stateDir": "~/.local/state/actions-disable-guard",
                       "refuseScripts": [{"name": "garden.py", "flag": "--fix", "why": "it disables workflows"}]},
    "capacityPipe":   {"commands": ["t3-capacity"]},
    "memoryIndex":    {"maxChars": 6000, "maxLine": 160, "stateDir": "~/.cache/memory-index-guard"},
    "bgGuard":        {"skipSessionsFiles": [], "maxBlocks": 3, "stateDir": "~/.local/state/bg-guard"},
    "agentSlots":     {"sessionsFile": "", "queue": "", "readyHeading": "## Ready", "maxAlive": 10,
                       "processName": "claude", "note": ""}
  }

Paths are absolute or start with "~/" (each user's own home). Plain Python 3 standard library.
"""
import copy, os, re, sys

_kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_common = os.path.join(_kit, 'common')
sys.path.insert(0, _common if os.path.isfile(os.path.join(_common, 'machine_file.py'))
                else os.path.join(os.path.dirname(_kit), 'common'))
import machine_file  # noqa: E402

KIT = 'agent-guards'
MACHINE_PATH = None   # tests only (a runner script sets it); shipped hooks read the fixed path
OWNER_UID = 0         # tests only
REQ = machine_file.REQUIRED
COMMAND_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._+-]{0,63}$')


def _path(value):
    if value and not (os.path.isabs(value) or value.startswith('~/')):
        return 'must be an absolute path or start with "~/"'
    if '\0' in value:
        return 'must not hold a NUL character'
    return None


def _paths(value):
    for v in value:
        if not isinstance(v, str) or not v or _path(v):
            return f'every entry must be an absolute path or start with "~/" ({v!r} is not)'
    return None


def _positive(value):
    return None if value > 0 else 'must be above 0'


def _commands(value):
    if not value:
        return 'must name at least one command'
    for v in value:
        if not isinstance(v, str) or not COMMAND_RE.match(v):
            return f'every entry must be a command name (letters, digits, . _ + -), not {v!r}'
    return None


def _scripts(value):
    for k, v in enumerate(value):
        if not isinstance(v, dict) or set(v) - {'name', 'flag', 'why'} or not isinstance(v.get('name'), str) \
                or not isinstance(v.get('flag'), str) or not isinstance(v.get('why', ''), str):
            return f'entry {k} must be {{"name": "<file name>", "flag": "<flag>", "why": "<text>"}}'
        if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$', v['name']) or not re.sub(r'[^a-z0-9]', '', os.path.splitext(v['name'])[0].lower()):
            return f'entry {k}: "name" must be a plain file name, not {v["name"]!r}'
        if not re.match(r'^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$', v['flag']):
            return f'entry {k}: "flag" must be one option like --fix, not {v["flag"]!r}'
    return None


SPEC = {
    'actionsDisable': {
        'stateDir': (str, '~/.local/state/actions-disable-guard', _path),
        'refuseScripts': (list, [], _scripts),
    },
    'capacityPipe': {
        'commands': (list, ['t3-capacity'], _commands),
    },
    'memoryIndex': {
        'maxChars': (int, 6000, _positive),
        'maxLine': (int, 160, _positive),
        'stateDir': (str, '~/.cache/memory-index-guard', _path),
    },
    'bgGuard': {
        'skipSessionsFiles': (list, [], _paths),
        'maxBlocks': (int, 3, _positive),
        'stateDir': (str, '~/.local/state/bg-guard', _path),
    },
    'agentSlots': {
        'sessionsFile': (str, '', _path),
        'queue': (str, '', _path),
        'readyHeading': (str, '## Ready', None),
        'maxAlive': (int, 10, _positive),
        'processName': (str, 'claude', lambda v: None if COMMAND_RE.match(v) else 'must be a process name'),
        'note': (str, '', None),
    },
}
HOOKS = tuple(SPEC)


def defaults(hook):
    return {k: copy.deepcopy(d) for k, (_t, d, _c) in SPEC[hook].items()}


def load(hook):
    """(settings of one hook with defaults filled in, problem text or None). Never raises."""
    try:
        section = machine_file.read_section(KIT, MACHINE_PATH, OWNER_UID, optional=True)
        if section is None:
            return defaults(hook), None
        machine_file.fields(section, {h: (dict, {}, None) for h in HOOKS}, KIT)
        return machine_file.fields(section.get(hook, {}), SPEC[hook], f'{KIT}.{hook}'), None
    except machine_file.MachineFileError as e:
        return defaults(hook), str(e)
    except Exception as e:  # noqa: BLE001 - a guard keeps its defaults whatever the file holds
        return defaults(hook), f'{type(e).__name__}: {e}'


def load_all():
    """{hook: settings}, [problems]: for the self-check."""
    out, problems = {}, []
    for hook in HOOKS:
        out[hook], p = load(hook)
        if p and p not in problems:
            problems.append(p)
    return out, problems


def expand(path, home=None):
    """A configured path for this user: "~/x" under home (default: $HOME, as the hooks always did)."""
    if path.startswith('~/'):
        return os.path.join(home if home is not None else os.path.expanduser('~'), path[2:])
    return path


def warn(name, problem):
    """One line on stderr about a config problem; the hook goes on with its defaults."""
    if problem:
        try:
            sys.stderr.write(f'{name}: the machine file\'s "{KIT}" section cannot be used ({problem}); built-in '
                             'defaults used. Run `agent-guards self-check`.\n')
        except Exception:  # noqa: BLE001
            pass
