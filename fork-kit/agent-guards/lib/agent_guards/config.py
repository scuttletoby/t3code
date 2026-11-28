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
    "leakGuard":      {"patternsFile": "/etc/fork-kit/leak-patterns", "maxPushBytes": 20000000, "repos": []},
    "facts":          {"dirs": [], "staleDays": 30, "maxRows": 60, "indexLines": 40, "indexChars": 4000,
                       "projects": [{"match": "github.com/owner/repo", "dirs": ["/srv/repo-facts"], "text": "",
                                     "name": "repo"}],
                       "records": [{"path": "/srv/records", "projectsDir": "projects",
                                    "shared": ["machines", "orch", "shared"]}]},
    "harnessEnv":     [{"name": "API_TIMEOUT_MS", "value": "400000"}, {"name": "DISABLE_AUTOUPDATER", "value": "1"}],
    "pollLoop":       {"enabled": true, "maxSleepSeconds": 120, "loopSleepSeconds": 5},
    "readSize":       {"enabled": true, "maxChars": 20000},
    "batchNudge":     {"enabled": true, "afterTurns": 3, "tailBytes": 1048576,
                       "readOnlyTools": ["Read", "Grep", "Glob", "LS", "NotebookRead"],
                       "readOnlyCommands": ["cat", "head", "tail", "sed", "grep", "ls", ...]}
  }

The three speed hooks (pollLoop, readSize, batchNudge) differ in one way: a problem in their own part, or in the
file, turns that hook off (it lets every call through and says so) instead of using the defaults, because they guard
speed, not safety, and must never block work on a broken config. "enabled": false turns one off on purpose.

"harnessEnv" is not a hook's part: the installer reads it (harness_env, HARNESS_ENV) for the managed settings' env.

Paths are absolute or start with "~/" (each user's own home). Plain Python 3 standard library.
"""
import copy, hashlib, os, re, sys, time

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


def _tools(value):
    for v in value:
        if not isinstance(v, str) or not re.match(r'^[A-Za-z][A-Za-z0-9_]{0,63}$', v):
            return f'every entry must be a tool name (letters, digits, _), not {v!r}'
    return None


def _repos(value):
    for v in value:
        if not isinstance(v, str) or not re.match(r'^[\w.*?-]+/[\w.*?-]+$', v):
            return f'every entry must be a GitHub repo as owner/name (globs * ? allowed), not {v!r}'
    return None


def _words(value):
    for v in value:
        if not isinstance(v, str) or not COMMAND_RE.match(v):
            return f'every entry must be a command name (letters, digits, . _ + -), not {v!r}'
    return None


READ_ONLY_TOOLS = ['Read', 'Grep', 'Glob', 'LS', 'NotebookRead']
READ_ONLY_COMMANDS = ['cat', 'head', 'tail', 'sed', 'grep', 'egrep', 'fgrep', 'rg', 'ls', 'find', 'wc', 'stat', 'file',
                      'tree', 'du', 'df', 'pwd', 'echo', 'printf', 'jq', 'awk', 'sort', 'uniq', 'cut', 'tr', 'nl',
                      'diff', 'cmp', 'readlink', 'realpath', 'basename', 'dirname', 'which', 'type', 'column', 'less',
                      'git', 'facts', 'true']


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
    'leakGuard': {
        'patternsFile': (str, '/etc/fork-kit/leak-patterns', _path),
        'maxPushBytes': (int, 20_000_000, _positive),
        'repos': (list, [], _repos),
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
    'pollLoop': {
        'enabled': (bool, True, None),
        'maxSleepSeconds': (int, 120, _positive),
        'loopSleepSeconds': (int, 5, _positive),
    },
    'readSize': {
        'enabled': (bool, True, None),
        'maxChars': (int, 20000, _positive),
    },
    'batchNudge': {
        'enabled': (bool, True, None),
        'afterTurns': (int, 3, _positive),
        'tailBytes': (int, 1048576, _positive),
        'readOnlyTools': (list, READ_ONLY_TOOLS, _tools),
        'readOnlyCommands': (list, READ_ONLY_COMMANDS, _words),
    },
}
HOOKS = tuple(SPEC)

# "harnessEnv": the Claude Code environment variables `agent-guards-install --settings` writes in the "env" of the
# managed settings drop-in, for every session on the machine (speed: smaller tool outputs, fewer compactions, a
# shorter wait on a stuck request). Each is in the Claude Code docs (code.claude.com/docs/en/env-vars). None: off
# (not written) unless the machine file gives a value.
HARNESS_ENV = {
    'BASH_MAX_OUTPUT_LENGTH': '12000',     # Claude Code's default 30000 characters
    # default 25000; a t3_thread_read page (at most 30000 bytes, about 7500 tokens) fits twice
    'MAX_MCP_OUTPUT_TOKENS': '15000',
    # default 600000. Not only the wait for the response headers: it also caps the first-byte watchdog of a large
    # request and bounds a whole non-streaming fallback answer, so not below 5 minutes
    'API_TIMEOUT_MS': '300000',
    # off: no kit, timer or CI step updates Claude Code, so turning the background updater off would keep a machine
    # on its version (security fixes too) until someone updates it by hand. A machine with its own update path
    # sets it to "1"
    'DISABLE_AUTOUPDATER': None,
}
# Variables T3 sets for each session it starts (the prompt-cache TTLs, the auto-compact window). Managed env wins
# over T3's per-launch env and T3 does not read managed settings, so one here would silently override T3's value
# (and T3 would show the wrong one): refused. FORCE_PROMPT_CACHING_5M overrides both TTLs.
T3_SETS = ('CLAUDE_CODE_PROMPT_CACHE_TTL', 'CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL', 'CLAUDE_CODE_AUTO_COMPACT_WINDOW',
           'FORCE_PROMPT_CACHING_5M')
ENV_NAME_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,127}$')


def harness_env(section):
    """({name: value} for the drop-in's "env", [problems]) from a section's "harnessEnv": a list of rows
    {"name": "<VARIABLE>", "value": "<text>" | <whole number> | null}. A row's value replaces the default; null takes
    the variable out. Rows, not an object keyed by the name: the machine file refuses keys that look like a secret,
    and MAX_MCP_OUTPUT_TOKENS is one. A name that looks like a secret is still refused here (the drop-in is world
    readable), unless it is one of HARNESS_ENV, and so is one T3 sets per session (T3_SETS). A bad row, or a name
    given again, is left out and named; a
    "harnessEnv" that is not a list keeps the defaults. Never raises."""
    env = {k: v for k, v in HARNESS_ENV.items()}
    where, problems = f'{KIT}.harnessEnv', []
    rows = section.get('harnessEnv', []) if isinstance(section, dict) else []
    if not isinstance(rows, list):
        return {k: v for k, v in env.items() if v is not None}, [f'{where}: must be a list of {{"name": "<VARIABLE>", '
                                                                 '"value": "<text>"}} rows (built-in defaults used)']
    seen = set()
    for k, row in enumerate(rows):
        name = row.get('name') if isinstance(row, dict) else None
        value = row.get('value') if isinstance(row, dict) else None
        if not isinstance(row, dict) or set(row) - {'name', 'value'} or 'value' not in row \
                or not isinstance(name, str) or isinstance(value, bool) \
                or not (value is None or isinstance(value, (str, int))):
            problems.append(f'{where}: entry {k} must be {{"name": "<VARIABLE>", "value": "<text>" or null}} (that '
                            'entry is left out)')
        elif not ENV_NAME_RE.match(name):
            problems.append(f'{where}: entry {k}: "name" must be a variable name (letters, digits, _), not {name!r} '
                            '(that entry is left out)')
        elif name in seen:
            problems.append(f'{where}: entry {k}: {name!r} is given twice (that entry is left out)')
        elif name in T3_SETS:
            problems.append(f'{where}: entry {k}: {name!r} is not set here: T3 sets it per session, and managed env '
                            'would override T3\'s value without T3 knowing (that entry is left out)')
        elif machine_file.SECRET_KEY.search(name) and name not in HARNESS_ENV:
            problems.append(f'{where}: entry {k}: {name!r} looks like a secret, and the managed settings are readable '
                            'by every user (that entry is left out)')
        elif isinstance(value, str) and ('\0' in value or len(value) > 4096):
            problems.append(f'{where}: entry {k}: the value of {name!r} must be at most 4096 characters, without NUL '
                            '(that entry is left out)')
        else:
            seen.add(name)
            env[name] = None if value is None else str(value)
    return {k: v for k, v in env.items() if v is not None}, problems


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
        if key not in SPEC and key != 'harnessEnv':
            problems.append(f'{KIT}: unknown key {key!r} (ignored by every hook; known: {", ".join(HOOKS)})')
    for hook in HOOKS:
        out[hook], p = _own(section, hook)
        problems += p
    problems += harness_env(section)[1]
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


NOTICE_DIR = '~/.cache/agent-guards/notices'
NOTICE_DAYS = 7


def first_notice(text, event, home=None):
    """True the first time a session sees `text` (a speed hook's config notice), False after: a broken machine file
    costs the agent one notice per hook per session, not one on every call (#1151). A marker file per (session,
    text) in ~/.cache/agent-guards/notices, made with O_EXCL, so two calls at once still say it once; markers older
    than NOTICE_DAYS go when a new one is made. No session id, or a folder it cannot write: True (say it every
    time rather than never)."""
    sid = event.get('session_id') if isinstance(event, dict) else None
    if not isinstance(sid, str) or not sid:
        return True
    folder = expand(NOTICE_DIR, home)
    name = hashlib.sha256(f'{sid}\0{text}'.encode()).hexdigest()[:32]
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)   # a file there raises FileExistsError too: not "seen"
    except OSError:
        return True
    try:
        fd = os.open(os.path.join(folder, name), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return False
    except OSError:
        return True
    os.close(fd)
    try:
        old = time.time() - NOTICE_DAYS * 86400
        for e in os.scandir(folder):
            if e.is_file(follow_symlinks=False) and e.stat(follow_symlinks=False).st_mtime < old:
                os.unlink(e.path)
    except OSError:
        pass
    return True


def warn(name, problem):
    """One line on stderr about a config problem (also in the hook's answer: notice); the hook goes on."""
    if problem:
        try:
            sys.stderr.write(problem_text(name, problem) + '\n')
        except Exception:  # noqa: BLE001
            pass
