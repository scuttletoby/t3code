"""agent_guards.config: the facts of the agent guard hooks, from the machine file's "agent-guards" section.

Every key is optional; a missing machine file or section gives the built-in defaults below (the values the hooks
had before they moved into this kit). Each hook reads only its own part, key by key: a top-level key no hook knows
(a later kit's section) or another hook's broken part changes nothing for it; a bad key of its own keeps that key's
default, an unknown key of its own is ignored, a bad refuseScripts, facts.projects or facts.records row is left out
(each of these is named). A broken file never weakens a guard below the defaults: the hook uses them and names the
problem in its answer (systemMessage, and additionalContext where the event takes it) and on stderr.
`agent-guards self-check` names every problem, unknown keys too.

  "agent-guards": {
    "actionsDisable": {"stateDir": "~/.local/state/actions-disable-guard",
                       "refuseScripts": [{"name": "garden.py", "flag": "--fix", "why": "it disables workflows"}]},
    "capacityPipe":   {"commands": ["t3-capacity"]},
    "memoryIndex":    {"maxChars": 6000, "maxLine": 160, "stateDir": "~/.cache/memory-index-guard"},
    "bgGuard":        {"skipSessionsFiles": [], "maxBlocks": 3, "stateDir": "~/.local/state/bg-guard"},
    "agentSlots":     {"sessionsFile": "", "queue": "", "readyHeading": "## Ready", "maxAlive": 10,
                       "processName": "claude", "note": ""},
    "facts":          {"dirs": [], "staleDays": 30, "maxRows": 60, "indexLines": 40, "indexChars": 4000,
                       "projects": [{"match": "github.com/owner/repo", "dirs": ["/srv/repo-facts"], "text": "",
                                     "name": "repo"}],
                       "records": [{"path": "/srv/records", "projectsDir": "projects",
                                    "shared": ["machines", "orch", "shared"]}]}
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


def _projects(value):
    for k, v in enumerate(value):
        p = _project_problem(k, v)
        if p:
            return p
    return None


def _records(value):
    for k, v in enumerate(value):
        p = _record_problem(k, v)
        if p:
            return p
    return None


def _scripts(value):
    for k, v in enumerate(value):
        p = _row_problem(k, v)
        if p:
            return p
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
    'facts': {
        'dirs': (list, [], _paths),
        'staleDays': (int, 30, _positive),
        'maxRows': (int, 60, _positive),
        'indexLines': (int, 40, _positive),
        'indexChars': (int, 4000, _positive),
        'projects': (list, [], _projects),
        'records': (list, [], _records),
    },
}
HOOKS = tuple(SPEC)


def defaults(hook):
    return {k: copy.deepcopy(d) for k, (_t, d, _c) in SPEC[hook].items()}


def _row_problem(k, v):
    """The problem of one refuseScripts row, or None."""
    if not isinstance(v, dict) or set(v) - {'name', 'flag', 'why'} or not isinstance(v.get('name'), str) \
            or not isinstance(v.get('flag'), str) or not isinstance(v.get('why', ''), str):
        return f'entry {k} must be {{"name": "<file name>", "flag": "<flag>", "why": "<text>"}}'
    if not re.match(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$', v['name']) \
            or not re.sub(r'[^a-z0-9]', '', os.path.splitext(v['name'])[0].lower()):
        return f'entry {k}: "name" must be a plain file name, not {v["name"]!r}'
    if not re.match(r'^-{1,2}[A-Za-z0-9][A-Za-z0-9-]*$', v['flag']):
        return f'entry {k}: "flag" must be one option like --fix, not {v["flag"]!r}'
    return None


def _project_problem(k, v):
    """The problem of one facts.projects row, or None."""
    if not isinstance(v, dict) or set(v) - {'match', 'dirs', 'text', 'name'} or not isinstance(v.get('match'), str) \
            or not isinstance(v.get('dirs', []), list) or not isinstance(v.get('text', ''), str) \
            or not isinstance(v.get('name', ''), str):
        return (f'entry {k} must be {{"match": "<host/owner/repo or a folder>", "dirs": [<paths>], "text": "<text>", '
                '"name": "<its folder under the record repo\'s projects folder>"}')
    if v.get('name') and not _NAME_RE.match(v['name']):
        return f'entry {k}: "name" must be one folder name (letters, digits, . _ -), not {v["name"]!r}'
    m = v['match']
    if m.startswith('/') or m.startswith('~/'):
        if _path(m):
            return f'entry {k}: "match" {m!r} {_path(m)}'
    elif not re.match(r'^[A-Za-z0-9.-]+(:[0-9]+)?/[A-Za-z0-9._/-]+$', m):
        return f'entry {k}: "match" must be a remote as host/owner/repo or a folder, not {m!r}'
    p = _paths(v.get('dirs', []))
    return f'entry {k}: "dirs": {p}' if p else None


_NAME_RE = re.compile(r'^(?!\.\.?$)[A-Za-z0-9._-]+$')


def _record_problem(k, v):
    """The problem of one facts.records row, or None."""
    if not isinstance(v, dict) or set(v) - {'path', 'projectsDir', 'shared'} or not isinstance(v.get('path'), str) \
            or not isinstance(v.get('projectsDir', 'projects'), str) or not isinstance(v.get('shared', []), list):
        return (f'entry {k} must be {{"path": "<the record repo\'s checkout>", "projectsDir": "projects", '
                '"shared": ["machines", "orch", "shared"]}')
    if not v['path'] or _path(v['path']):
        return f'entry {k}: "path" {v["path"]!r} {_path(v["path"]) or "must not be empty"}'
    for name in [v.get('projectsDir', 'projects')] + v.get('shared', []):
        if not isinstance(name, str) or not _NAME_RE.match(name):
            return (f'entry {k}: a folder of the record repo must be one folder name (letters, digits, . _ -), '
                    f'not {name!r}')
    return None


ROW_CHECKS = {'refuseScripts': _row_problem, 'projects': _project_problem, 'records': _record_problem}


def _own(section, hook):
    """(settings, [problems]) of one hook from its own part of the section. Each key is checked on its own: a bad
    key keeps its built-in default (and is named), the others keep their values; a bad refuseScripts row is dropped
    (and named), the good rows stay. An unknown key of its own part (a typo, or a newer kit's key) is named and
    ignored."""
    out, problems = defaults(hook), []
    where = f'{KIT}.{hook}'
    part = section.get(hook, {})
    if not isinstance(part, dict):
        return out, [f'{where}: must be a JSON object (built-in defaults used)']
    for key, value in part.items():
        if key not in SPEC[hook]:
            problems.append(f'{where}: unknown key {key!r} (ignored; known: {", ".join(sorted(SPEC[hook]))})')
            continue
        if key in ROW_CHECKS and isinstance(value, list):
            rows = []
            for k, row in enumerate(value):
                p = ROW_CHECKS[key](k, row)
                if p:
                    problems.append(f'{where}.{key}: {p} (that entry is left out)')
                else:
                    rows.append(row)
            out[key] = rows
            continue
        try:
            out[key] = machine_file.fields({key: value}, {key: SPEC[hook][key]}, where)[key]
        except machine_file.MachineFileError as e:
            problems.append(f'{e} (built-in default used)')
    return out, problems


def _section():
    """(section or None, problem or None). Never raises."""
    try:
        return machine_file.read_section(KIT, MACHINE_PATH, OWNER_UID, optional=True), None
    except machine_file.MachineFileError as e:
        return None, str(e)
    except Exception as e:  # noqa: BLE001 - a guard keeps its defaults whatever the file holds
        return None, f'{type(e).__name__}: {e}'


def load(hook):
    """(settings of one hook with defaults filled in, problem text or None). Never raises. A hook reads only its
    own part: another hook's part, or a key no hook knows (a later kit's), never changes what it gets."""
    section, problem = _section()
    if section is None:
        return defaults(hook), problem
    try:
        out, problems = _own(section, hook)
    except Exception as e:  # noqa: BLE001
        return defaults(hook), f'{type(e).__name__}: {e}'
    return out, '; '.join(problems) or None


def load_all():
    """{hook: settings}, [problems]: for the self-check, which is strict: it also names keys no hook knows."""
    section, problem = _section()
    if section is None:
        return {h: defaults(h) for h in HOOKS}, [problem] if problem else []
    out, problems = {}, []
    for key in section:
        if key not in SPEC:
            problems.append(f'{KIT}: unknown key {key!r} (ignored by every hook; known: {", ".join(HOOKS)})')
    for hook in HOOKS:
        out[hook], p = _own(section, hook)
        problems += p
    return out, problems


def expand(path, home=None):
    """A configured path for this user: "~/x" under home (default: $HOME, as the hooks always did)."""
    if path.startswith('~/'):
        return os.path.join(home if home is not None else os.path.expanduser('~'), path[2:])
    return path


def problem_text(name, problem):
    """The one line that says a hook runs on (some of) its built-in defaults."""
    return (f'{name}: the machine file\'s "{KIT}" section cannot be used as written ({problem}); built-in defaults '
            'used for that part. Tell the owner; `agent-guards self-check` names every problem.')


def notice(name, problem, event, out=None):
    """The hook's answer `out` (a dict or None) with the config problem added where the agent and the owner see
    it: systemMessage (shown in the session), and for the events that take it, additionalContext (read by the
    agent); a block reason gets it appended. None when there is nothing to say."""
    if not problem:
        return out
    text = problem_text(name, problem)
    out = dict(out or {})
    out['systemMessage'] = (out['systemMessage'] + ' ' + text) if out.get('systemMessage') else text
    if out.get('decision') == 'block' and isinstance(out.get('reason'), str):
        out['reason'] += ' (' + text + ')'
    ev = (event or {}).get('hook_event_name') if isinstance(event, dict) else None
    if ev in ('PreToolUse', 'PostToolUse', 'UserPromptSubmit'):
        hso = dict(out.get('hookSpecificOutput') or {}, hookEventName=ev)
        hso['additionalContext'] = (hso['additionalContext'] + ' ' + text) if hso.get('additionalContext') else text
        out['hookSpecificOutput'] = hso
    return out


def warn(name, problem):
    """One line on stderr about a config problem (also in the hook's answer: notice); the hook goes on."""
    if problem:
        try:
            sys.stderr.write(problem_text(name, problem) + '\n')
        except Exception:  # noqa: BLE001
            pass
