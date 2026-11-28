"""model_guard: a Claude Code PreToolUse hook that keeps agents' model picks inside a policy (fork-kit/model-guard).

It checks every call that can start an agent or change an agent's model: Claude Code's Agent tool (Task in older
versions) and Workflow tool, and T3's MCP tools in T3_TOOLS. CHECKED_TOOLS below is the one list: the hook's
matcher, tool_kind() and the self-check are all made from it. Every other tool passes untouched.

The policy is a JSON file {"mode": "off" | "advise" | "enforce", "allow": [rows], "version": 1}; the machine file
(fork-kit/common) names it in its "model-guard" section. Rules (README.md next to this folder):

- every checked call names its model and its effort, and both are in one row; else enforce refuses and lists
  the rows (the hook cannot see the parent's model or the provider's catalog, so nothing may be inherited);
- a T3 pick names only option ids the guard can read: on a Claude row the ids T3's Claude driver reads and the
  shared effort ids; on a row of another driver the shared effort ids, ultracode, and the row's effortOptionIds
  (read as efforts) and otherOptionIds (#601);
- a T3 pick names its effort under each id the row's driver reads it from (DRIVER_EFFORT_IDS for T3's built-in
  drivers, Claude's among them, and each of the row's effortOptionIds), so the driver never runs a default or
  inherited level under an effort it ignores; a row with efforts whose driver reads no id the guard knows refuses
  every T3 pick (#629);
- a call that would run a model the hook cannot see (a thread fork, a Workflow script it cannot read, a model
  field on a tool that has none) is refused in enforce;
- the policy file or the machine file missing, unreadable or invalid: refused, naming the file (fail closed);
- mode off: every call passes; advise: every call passes and the agent is told what enforce would do;
- every advise note and every refusal is also written to the system log (tag model-guard), so the machine owner
  has a record (`journalctl -t model-guard`); a log that cannot be written never changes the answer;
- a passing call gets the row's "useFor" note, so the agent sees what the model is for.

Plain Python 3 standard library. No state: the files are read again on every call.
"""
import hashlib, json, os, re, stat, subprocess, sys, time

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
for _common in (os.path.join(HERE, 'common'), os.path.join(os.path.dirname(HERE), 'common')):
    if os.path.isfile(os.path.join(_common, 'machine_file.py')):
        sys.path.insert(0, _common)
        break
import machine_file  # noqa: E402

KIT = 'model-guard'
MODES = ('off', 'advise', 'enforce')
# T3's driver kind of Claude: the native Agent and Workflow tools only ever start Claude agents
CLAUDE_DRIVER = 'claudeAgent'
# the option ids a model's effort control goes by (the same list as T3's usage pacing gate,
# apps/server/src/usage/UsagePacingGate.ts EFFORT_OPTION_IDS; tests/guard.test.py keeps the two in step)
EFFORT_OPTION_IDS = ('effort', 'reasoningEffort', 'reasoning_effort', 'reasoning', 'thinking_level')
# Ultracode, as T3 runs it (packages/shared/src/model.ts withLegacyUltracodeEffort; tests/guard.test.py keeps the
# two in step): the option "ultracode" on, run at effort xhigh; the old effort value "ultracode" means xhigh with
# the option on (unless the option is given). The guard names what runs: the effort "ultracode".
ULTRACODE_OPTION_ID = 'ultracode'
ULTRACODE_EFFORT = 'ultracode'
ULTRACODE_RUNS_AT = 'xhigh'
# The option ids T3's Claude driver reads (apps/server/src/claudeModelOptions.ts; the option list of every Claude
# model in T3's bundled model list, plus ultracode, which ClaudeModelCatalog.ts withUltracodeToggle adds; a test keeps
# them in step) and the one it reads the effort from. A Claude row refuses any other id in a T3 pick: T3 has no
# driver on a model selection, so the same model on another driver's instance could read it as its reasoning.
CLAUDE_OPTION_IDS = ('effort', ULTRACODE_OPTION_ID, 'contextWindow', 'fastMode', 'thinking')
# the Claude ids T3 reads only as true/false (claudeModelOptions.ts getModelSelectionBooleanOptionValue); another
# driver may read the same id as its reasoning level (Mistral Vibe's ACP "thinking", Pi's "thinking"), so a text
# value is refused (#629)
CLAUDE_BOOLEAN_IDS = ('fastMode', 'thinking')
# The option id each of T3's built-in drivers reads its reasoning level from, as the guard already knew it for Claude
# (#629; a test reads each one from T3's code and keeps the list of built-in drivers in step). A pick of such a row
# must name its effort there: under another id the driver ignores, it runs its default or the parent's level.
DRIVER_EFFORT_IDS = {
    CLAUDE_DRIVER: 'effort',          # apps/server/src/claudeModelOptions.ts
    'codex': 'reasoningEffort',       # apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts
    'fx': 'effort',                   # packages/provider-fx/src/server/acpSupport.ts FX_EFFORT_CONFIG_ID
    'grok': 'reasoningEffort',        # packages/provider-grok/src/server/adapter.ts GROK_REASONING_EFFORT_OPTION_ID
    'kiro': 'reasoningEffort',        # packages/provider-kiro/src/server/adapter.ts applyKiroEffort
    'muse': 'reasoningEffort',        # packages/provider-muse/src/server/adapter.ts selectionEffort
    'opencode': 'variant',            # packages/provider-opencode/src/server/OpenCodeRuntime.ts openCodeVariant
    'pi': 'thinking',                 # packages/provider-pi/src/server/adapter.ts applySelection
}
# T3's built-in drivers with no fixed reasoning id: the agent or the model names it (an ACP agent's config option,
# a Cursor model's parameter). Their rows, like those of any driver not known here, list it in effortOptionIds; a
# row with efforts and no such id refuses every T3 pick (fail closed).
AGENT_NAMED_EFFORT_DRIVERS = ('acpRegistry', 'antigravity', 'cursor')
# Every built-in driver kind of T3 (a contract test reads the list from T3's code). A row naming one of them in
# another case or with spaces around it is refused: a pick of it could run on that driver's instance, which the
# guard would then check with the wrong row's ids (#638 review).
BUILT_IN_DRIVER_KINDS = tuple(DRIVER_EFFORT_IDS) + AGENT_NAMED_EFFORT_DRIVERS
# Option ids of a built-in driver that pick an agent, which brings settings the guard cannot see: an OpenCode "agent"
# (packages/provider-opencode/src/server/adapter.ts turnPromptOptions; its own prompt, tools and settings) and a
# Kiro "mode" (packages/provider-kiro/src/server/acpSupport.ts KIRO_AGENT_CONFIG_ID, which switches Kiro's agent and
# with it the agent's own model). Never an option that changes neither, and never read as an effort (#629, #638).
DRIVER_MODEL_OPTION_IDS = {'kiro': ('mode',), 'opencode': ('agent',)}
CLAUDE_EFFORT_ID = DRIVER_EFFORT_IDS[CLAUDE_DRIVER]
# T3's option id for an ACP agent's session mode (packages/provider-acp/src/server/sessionConfig.ts
# ACP_SESSION_MODE_OPTION_ID). Some agents repeat their reasoning levels as modes; T3 then hides the control but still
# applies a pick of it (adapter.ts setMode), so it is never an option that "changes no reasoning".
ACP_SESSION_MODE_ID = '_t3/session-mode'
# an otherOptionIds id may not name a model or reasoning control (an owner slip would pass such a pick unchecked)
NOT_OTHER = re.compile(r'model|effort|reason|think|thought|ultracode|provider|driver|instance', re.I)

# Every tool the hook checks, and how. The one list: HOOK_MATCHER, tool_kind() and the self-check come from it.
# Claude Code's own tools by name; T3's under any MCP server name (mcp__<server>__<tool>).
# RemoteTrigger creates, changes and fires cloud routines that run on a model the guard cannot see.
CLAUDE_TOOLS = {'Agent': 'agent', 'Task': 'agent', 'Workflow': 'workflow', 'RemoteTrigger': 'remote'}
T3_TOOLS = ('delegate_task', 'create_threads', 't3_thread_launch', 't3_thread_configure', 't3_thread_transfer',
            't3_thread_send', 't3_thread_send_attachments', 'schedule_task', 't3_project_create',
            't3_project_update')
# T3 tools the T3 server checks against its own modelPolicy (each route's own check, and the backstop for a fork).
# The hook leaves them to the server only while T3's managed policy locks that policy (server_lock below).
SERVER_CHECKED_TOOLS = ('delegate_task', 'create_threads')
# T3's managed policy file: root-owned, read by T3 at start (packages/shared/src/managedPolicyFile.ts); a locked
# setting there wins over the settings file an agent can write
T3_POLICY_PATHS = {'linux': '/etc/t3code/policy.json', 'darwin': '/Library/Application Support/T3Code/policy.json'}
# RemoteTrigger actions that only read
REMOTE_READ_ACTIONS = ('list', 'get', 'list_runs', 'get_run_log')
# the policy versions this kit reads
POLICY_VERSION = 1
MCP_TOOL = re.compile(r'^mcp__.+__(' + '|'.join(T3_TOOLS) + r')$')
# what the self-check tests the matcher with (T3's own server name; the matcher takes any)
CHECKED_TOOLS = tuple(CLAUDE_TOOLS) + tuple(f'mcp__t3-code__{t}' for t in T3_TOOLS)
# T3 tools that run on a thread's existing model and take none: a model field on them is refused, as the guard
# cannot see what it would do (T3 ignores unknown fields today; a later T3 might not)
NO_MODEL_TOOLS = ('t3_thread_send', 't3_thread_send_attachments', 'schedule_task', 't3_thread_transfer')
MODEL_FIELD = re.compile(r'.*(model|effort|provider|driver|instance|ultracode|reasoning|thinking).*'
                         r'|options?|target|selection', re.I)
# the hook as Claude Code runs it: `|| exit 2` blocks the call when the hook itself is missing or fails
HOOK_LINK = '/usr/local/bin/model-guard'
HOOK_COMMAND = HOOK_LINK + ' || exit 2'
HOOK_MATCHER = '^(' + '|'.join(CLAUDE_TOOLS) + r'|mcp__.+__(' + '|'.join(T3_TOOLS) + '))$'
INSTALLED = '/etc/model-guard/installed.tsv'
# Claude Code's managed settings: root-owned, and read by every Claude Code and Agent SDK session whatever its
# settingSources (https://code.claude.com/docs/en/managed-settings). The installer writes its own drop-in file.
MANAGED_DIRS = {'linux': '/etc/claude-code', 'darwin': '/Library/Application Support/ClaudeCode',
                'win32': r'C:\Program Files\ClaudeCode'}
DROP_IN_NAME = 'model-guard.json'
PolicyError = machine_file.MachineFileError


def managed_dir(platform=None):
    platform = platform or sys.platform
    for prefix, path in MANAGED_DIRS.items():
        if platform.startswith(prefix):
            return path
    raise PolicyError(f'no Claude Code managed settings folder known for platform {platform!r}')


def hook_entry():
    """The PreToolUse entry that runs the guard; the installer writes it, the self-check wants exactly it."""
    # onFailure "block" (Claude Code 2.1.295+) blocks the call when the hook cannot start or times out; without it a
    # timed-out PreToolUse hook lets the call through. "|| exit 2" covers the guard failing on older versions.
    return {'matcher': HOOK_MATCHER,
            'hooks': [{'type': 'command', 'command': HOOK_COMMAND, 'onFailure': 'block'}]}


def drop_in_text():
    return json.dumps({'hooks': {'PreToolUse': [hook_entry()]}}, indent=2) + '\n'


def tool_kind(name):
    """'agent', 'workflow', the T3 tool's name, or None for a tool this hook does not check."""
    if not isinstance(name, str):
        return None
    if name in CLAUDE_TOOLS:
        return CLAUDE_TOOLS[name]
    m = MCP_TOOL.match(name)
    return m.group(1) if m else None


# ---------- the policy


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f'key {k!r} is given twice')
        out[k] = v
    return out


def _words(value):
    if not value or any(not isinstance(v, str) or not v.strip() for v in value):
        return 'must be a list of one or more non-empty texts'
    return 'names a value twice' if len(set(value)) != len(value) else None


def _text(value):
    return None if value.strip() else 'must not be empty'


def _option_ids(value):
    problem = _words(value)
    if problem is None and any(v != js_trim(v) for v in value):
        problem = 'must be option ids without spaces around them (T3 trims them)'
    return problem


# "ultracodeOption": false says T3 offers this Claude model no ultracode option (its effort list has no ultracode,
# ClaudeModelCatalog.ts withUltracodeToggle), so a T3 pick of it need not name ultracode, and T3 refuses one that does.
# "effortOptionIds" and "otherOptionIds" (rows of other drivers only): the option ids of the driver's own the guard
# may read, as an effort (an ACP agent names its reasoning control by its own id, packages/provider-acp
# sessionConfig.ts role "reasoning") or as an option that changes neither the model nor its reasoning. A pick of
# such a row that names any other id is refused (#601).
ROW = {'driver': (str, machine_file.REQUIRED, _text), 'model': (str, machine_file.REQUIRED, _text),
       'efforts': (list, None, _words), 'defaultEffort': (str, None, _text), 'aliases': (list, None, _words),
       'useFor': (str, None, None), 'ultracodeOption': (bool, True, None),
       'effortOptionIds': (list, None, _option_ids), 'otherOptionIds': (list, None, _option_ids)}
# the guard's own /etc folder: an uninstall removes everything in it, so the policy may not live there
ETC_DIR = os.path.dirname(INSTALLED)


def parse_policy(text, path):
    """The policy as {'mode', 'allow'}; raises PolicyError naming the file and the reason."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as e:
        raise PolicyError(f'{path}: not valid JSON ({e})') from None
    if isinstance(data, dict) and isinstance(data.get('version'), int) and not isinstance(data['version'], bool) \
            and data['version'] > POLICY_VERSION:
        # checked first: a newer policy may use keys or rules this kit does not know
        raise PolicyError(f'{path}.version: {data["version"]} is newer than this kit knows ({POLICY_VERSION}); '
                          f'update the kit')
    positive = lambda v: None if v >= 1 else 'must be 1 or more'  # noqa: E731
    policy = machine_file.fields(data, {'mode': (str, machine_file.REQUIRED, None),
                                        'allow': (list, machine_file.REQUIRED, None),
                                        'version': (int, 1, positive)}, path)
    if policy['mode'] not in MODES:
        raise PolicyError(f'{path}.mode: must be one of {", ".join(MODES)} (it is {policy["mode"]!r})')
    rows, names = [], {}
    for i, raw in enumerate(policy['allow']):
        where = f'{path}.allow[{i}]'
        row = machine_file.fields(raw, ROW, where)
        twin = [k for k in BUILT_IN_DRIVER_KINDS
                if k != row['driver'] and k.casefold() == js_trim(row['driver']).casefold()]
        if twin:
            raise PolicyError(f'{where}.driver: {row["driver"]!r} is not T3\'s driver kind {twin[0]!r}: write it '
                              f'exactly so (a pick of it may run on a {twin[0]} instance, which reads other option '
                              f'ids)')
        if row['defaultEffort'] is not None and row['defaultEffort'] not in (row['efforts'] or ()):
            raise PolicyError(f'{where}.defaultEffort: {row["defaultEffort"]!r} is not one of its efforts')
        if not row['ultracodeOption'] and row['driver'] != CLAUDE_DRIVER:
            raise PolicyError(f'{where}.ultracodeOption: only {CLAUDE_DRIVER} rows have an ultracode option')
        if not row['ultracodeOption'] and ULTRACODE_EFFORT in (row['efforts'] or ()):
            raise PolicyError(f'{where}.ultracodeOption: false, so the model cannot run the effort ultracode')
        effort_ids, other_ids = row['effortOptionIds'] or [], row['otherOptionIds'] or []
        if effort_ids and row['efforts'] is None:
            raise PolicyError(f'{where}.effortOptionIds: the row lists no efforts, so no pick could name them')
        for key, ids in (('effortOptionIds', effort_ids), ('otherOptionIds', other_ids)):
            if ids and row['driver'] == CLAUDE_DRIVER:
                raise PolicyError(f'{where}.{key}: only rows of other drivers take it ({CLAUDE_DRIVER} options are '
                                  f'T3\'s own)')
            if ULTRACODE_OPTION_ID in ids:
                raise PolicyError(f'{where}.{key}: ultracode is always read as the effort ultracode')
        agents = [i for i in effort_ids if i in DRIVER_MODEL_OPTION_IDS.get(row['driver'], ())]
        if agents:
            raise PolicyError(f'{where}.effortOptionIds: {agents[0]!r} picks an agent of {row["driver"]}, which '
                              f'brings settings the guard cannot see, so it is never read as an effort')
        both = [i for i in other_ids if i in effort_ids or i in EFFORT_OPTION_IDS
                or i == DRIVER_EFFORT_IDS.get(row['driver'])]
        if both:
            raise PolicyError(f'{where}.otherOptionIds: {both[0]!r} is an effort option id, so it cannot be an option '
                              f'that changes no reasoning')
        if ACP_SESSION_MODE_ID in other_ids:
            raise PolicyError(f'{where}.otherOptionIds: {ACP_SESSION_MODE_ID!r} is T3\'s session mode, which some '
                              f'agents use as a second reasoning control (list it in effortOptionIds if it is one)')
        named = [i for i in other_ids if NOT_OTHER.search(i) or i in DRIVER_MODEL_OPTION_IDS.get(row['driver'], ())]
        if named:
            raise PolicyError(f'{where}.otherOptionIds: {named[0]!r} names a model or reasoning control, so it cannot '
                              f'be an option that changes neither')
        for name in [row['model']] + (row['aliases'] or []):
            # a name (model or alias) means one row of a driver, never two
            if (row['driver'], name) in names:
                raise PolicyError(f'{where}: {row["driver"]} {name!r} is also in allow[{names[row["driver"], name]}]')
            names[row['driver'], name] = i
        rows.append(row)
    return {'mode': policy['mode'], 'allow': rows, 'version': policy['version']}


def in_folder(path, folder):
    """True when path is folder or inside it: by name, as written and resolved (links followed) on both sides, so
    /private/etc/x is in /etc/x where /etc links to /private/etc; and, while the folder exists, by being the same
    folder as one of path's resolved parents, which also catches another spelling on a case-insensitive file system
    (/etc/Model-Guard on macOS)."""
    names = {os.path.normpath(folder), os.path.realpath(folder)}
    real = os.path.realpath(path)
    for name in (os.path.normpath(path), real):
        if any(name == n or name.startswith(n.rstrip(os.sep) + os.sep) for n in names):
            return True
    try:
        want = os.stat(folder)
    except OSError:
        return False
    while True:
        try:
            if os.path.samestat(os.stat(real), want):
                return True
        except OSError:
            pass
        parent = os.path.dirname(real)
        if parent == real:
            return False
        real = parent


def load_policy(machine_path=None, owner_uid=0):
    """(the machine section, the policy). Both files root-owned (owner_uid in tests); raises PolicyError."""
    machine_path = machine_path or machine_file.default_path()
    section = machine_file.read_section(KIT, machine_path, owner_uid)
    def absolute(v):
        if not os.path.isabs(v) or '\0' in v:
            return 'must be an absolute path'
        if in_folder(v, ETC_DIR):
            return f'must not be in {ETC_DIR}, which an uninstall removes'
        return None
    def paths(v):
        if not v or any(not isinstance(p, str) or not os.path.isabs(p) or '\0' in p for p in v):
            return 'must be a list of one or more absolute paths'
        return 'names a path twice' if len(set(v)) != len(v) else None
    cfg = machine_file.fields(section, {
        'policyFile': (str, machine_file.REQUIRED, absolute),
        'allowOtherHooks': (bool, False, None),
        't3ModelManifests': (list, None, paths),   # read by the self-check only (#600)
        't3ServerStates': (list, None, paths),     # each T3 home's userdata/server-runtime.json (server_lock)
    }, f'{machine_path}: {KIT}')
    text = machine_file.read_protected_text(cfg['policyFile'], owner_uid)
    return cfg, parse_policy(text, cfg['policyFile'])


def row_effort_ids(row):
    """The option ids a row's driver reads its effort from (each must be named in a T3 pick): the one the guard
    knows for a built-in driver, and the row's effortOptionIds. Empty for a driver the guard knows no id for."""
    known = DRIVER_EFFORT_IDS.get(row['driver'])
    return tuple(dict.fromkeys(((known,) if known else ()) + tuple(row['effortOptionIds'] or ())))


def describe(row):
    efforts = '|'.join(row['efforts']) if row['efforts'] else 'no effort may be named'
    extra = [f'default {row["defaultEffort"]}'] if row['defaultEffort'] else []
    if row['aliases']:
        extra.append('Agent tool: ' + ' or '.join(repr(a) for a in row['aliases']))
    if not row['ultracodeOption']:
        extra.append('no ultracode option')
    if row['effortOptionIds']:
        extra.append('effort option ids ' + '|'.join(row['effortOptionIds']))
    if row['otherOptionIds']:
        extra.append('other option ids ' + '|'.join(row['otherOptionIds']))
    text = f'{row["driver"]} {row["model"]} ({efforts}' + ''.join(f'; {e}' for e in extra) + ')'
    return text + (f': use for {row["useFor"]}' if row['useFor'] else '')


# ---------- one call


# JavaScript's String.prototype.trim: its WhiteSpace and LineTerminator characters (not Python's str.strip, which
# trims \x1c-\x1f and keeps U+FEFF)
JS_SPACE = '\t\n\v\f\r \u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a\u2028\u2029' \
           '\u202f\u205f\u3000\ufeff'


def js_trim(text):
    return text.strip(JS_SPACE)


def _option_pairs(options, where, problems):
    """The (id, value) pairs of an options value ({id: value} or [{id, value}]) as T3 reads them
    (packages/contracts/src/model.ts ProviderOptionSelections): ids and text values trimmed. T3 drops a value that is
    not a text or a boolean, or an empty id or text, and then uses the default; its lookups take the first entry of
    an id. So such a value is refused, and so is an id given twice after trimming. None when refused."""
    if options is None:
        return []
    if isinstance(options, dict):
        raw = list(options.items())
    elif isinstance(options, list) and all(isinstance(o, dict) for o in options):
        raw = [(o.get('id'), o.get('value')) for o in options]
    else:
        problems.append(f'{where}.options is neither an object nor a list of {{id, value}}')
        return None
    pairs, seen = [], set()
    for key, value in raw:
        name = js_trim(key) if isinstance(key, str) else ''
        if not name:
            problems.append(f'{where}.options has an option id {key!r} that is not a non-empty text')
            return None
        if name in seen:
            problems.append(f'{where}.options names {name!r} twice (T3 trims option ids, so {key!r} is {name!r})')
            return None
        seen.add(name)
        if name == ULTRACODE_OPTION_ID and not isinstance(value, bool):
            problems.append(f'{where}.options.ultracode must be true or false (T3 uses the default for {value!r})')
            return None
        if isinstance(value, str) and js_trim(value):
            value = js_trim(value)
        elif not isinstance(value, bool):
            problems.append(f'{where}.options.{name}: {value!r} is not a non-empty text or true/false (T3 drops it '
                            f'and uses the default)')
            return None
        pairs.append((name, value))
    return pairs


def _efforts_in(pairs, effort_ids=EFFORT_OPTION_IDS):
    """The efforts option pairs run, as T3 runs them: ultracode on means the effort "ultracode" (it runs at xhigh,
    so that xhigh is not named again)."""
    efforts = [value for key, value in pairs if key in effort_ids]
    ultracode = [value for key, value in pairs if key == ULTRACODE_OPTION_ID]
    if ULTRACODE_EFFORT in efforts:       # the old effort value: xhigh, with the option on unless it is given
        efforts = [ULTRACODE_RUNS_AT if e == ULTRACODE_EFFORT else e for e in efforts]
        ultracode = ultracode or [True]
    if True in ultracode:
        efforts = [e for e in efforts if e != ULTRACODE_RUNS_AT] + [ULTRACODE_EFFORT]
    return efforts


def check_pick(where, model, efforts, driver, rows, agent, problems, matched, pairs=None):
    """One model pick against the rows; adds problems, or the rows it matched. Without a driver every row naming
    the model must accept the pick. A row without "efforts" accepts only a pick that names no effort. A T3 pick
    gives its option pairs instead of efforts: each row reads them with its own effort option ids, refuses an option
    id it cannot read (#601), and wants the effort under each id its driver reads it from (row_effort_ids; with none
    known, a row with efforts refuses the pick, #629)."""
    if not isinstance(model, str) or not js_trim(model) or js_trim(model) == 'inherit':
        problems.append(f'{where} names no model (every call must name one; the guard cannot see the parent\'s)')
        return
    model = js_trim(model)  # T3 trims it too
    if driver is not None and not isinstance(driver, str):
        problems.append(f'{where} has a driver that is not a text')
        return
    found = [r for r in rows if (driver is None or r['driver'] == driver)
             and (r['model'] == model or (agent and model in (r['aliases'] or ())))]
    if not found:
        problems.append(f'{where}: model {model!r}' + (f' of driver {driver}' if driver else '') + ' is not allowed')
        return
    for row in found:
        if pairs is not None:
            if row['driver'] == CLAUDE_DRIVER:
                own, effort_ids = (CLAUDE_EFFORT_ID,), EFFORT_OPTION_IDS
                read = set(EFFORT_OPTION_IDS) | set(CLAUDE_OPTION_IDS)
            else:
                own = row_effort_ids(row)
                if not own and row['efforts'] is not None:
                    problems.append(f'{where}: the guard does not know which option id {row["driver"]} reads the '
                                    f'effort of {model!r} from, so it cannot tell what level would run: the policy row '
                                    f'must list it in effortOptionIds')
                    return
                effort_ids = EFFORT_OPTION_IDS + own
                read = set(effort_ids) | {ULTRACODE_OPTION_ID} | set(row['otherOptionIds'] or ())
            keys = [key for key, _ in pairs]
            unread = [key for key in keys if key not in read]
            if unread and row['driver'] == CLAUDE_DRIVER:
                problems.append(f'{where}: option {unread[0]!r} is not one T3\'s {CLAUDE_DRIVER} reads '
                                f'({", ".join(CLAUDE_OPTION_IDS)}): T3 runs a model selection on the named '
                                f'instance, and on another driver\'s instance {model!r} could read it as its '
                                f'reasoning, unchecked')
                return
            text = [key for key, value in pairs if key in CLAUDE_BOOLEAN_IDS and not isinstance(value, bool)]
            if text and row['driver'] == CLAUDE_DRIVER:
                problems.append(f'{where}.options.{text[0]} must be true or false: T3\'s {CLAUDE_DRIVER} reads it '
                                f'only so, and on another driver\'s instance {model!r} could read a text as its '
                                f'reasoning, unchecked')
                return
            if unread:
                problems.append(f'{where}: option {unread[0]!r} is not one the guard can read for {row["driver"]} '
                                f'{model!r} (a driver may name its reasoning control by its own id): the '
                                f'policy row must list it in effortOptionIds (a reasoning control, checked as '
                                f'an effort) or otherOptionIds (it changes neither the model nor its reasoning)')
                return
            missing = [i for i in own if i not in keys]
            if missing and row['efforts'] is not None:
                # the driver reads its reasoning there: left out, it runs its default or the parent's level, whatever
                # a shared id it ignores says (#618 review)
                problems.append(f'{where} names no effort for {model!r} under {missing[0]!r}, the option '
                                f'{row["driver"]} reads it from (allowed: {"|".join(row["efforts"])}); left out, '
                                f'the driver runs its default or inherited level')
                return
            efforts = _efforts_in(pairs, effort_ids)
        if row['efforts'] is None:
            if efforts:
                problems.append(f'{where}: effort {efforts[0]!r} is not allowed for {model!r} (its row lists no '
                                f'efforts, so the call may name none)')
                return
            continue
        if not efforts:
            problems.append(f'{where} names no effort for {model!r} (allowed: {"|".join(row["efforts"])})')
            return
        bad = [e for e in efforts if e not in row['efforts']]
        if bad:
            problems.append(f'{where}: effort {bad[0]!r} is not allowed for {model!r} '
                            f'(allowed: {"|".join(row["efforts"])})')
            return
    matched.extend(r for r in found if r not in matched)


# ---------- a Workflow script: a strict text check, not a sandbox. Every agent() call must be a plain call whose
# options are an object literal naming model and effort as plain texts; anything the check cannot follow (a name
# that reaches the global object or builds code, a computed key, another workflow, agent used other than as a
# direct call) refuses the whole script. Telling a regular expression from a division needs a full JavaScript
# parser, so the check does not guess: every / that does not open a comment is refused, and so is every line end
# other than \n and \r\n (a bare \r, U+2028, U+2029), which JavaScript also ends a // comment at.

class ScriptError(Exception):
    pass


WF_FORBIDDEN = frozenset((
    'eval', 'Function', 'constructor', 'globalThis', 'global', 'self', 'window', 'this', 'import', 'require',
    'Reflect', 'Proxy', 'with', '__proto__', 'prototype', 'getPrototypeOf', 'setPrototypeOf', 'defineProperty',
    'defineProperties', 'getOwnPropertyDescriptor', 'getOwnPropertyDescriptors', 'arguments', 'setTimeout',
    'setInterval'))
# after these words a [ starts an array (not a member access), unless the word is a property name (after . or ?.);
# "of" is the word only as the first of directly inside a for (...) header, after the binding; elsewhere it is a name
WF_EXPRESSION_WORDS = frozenset((
    'return', 'typeof', 'case', 'do', 'else', 'in', 'of', 'new', 'delete', 'void', 'throw', 'instanceof', 'yield',
    'await', 'const', 'let', 'var', 'export'))
WF_MAX_BYTES = 1024 * 1024
WF_SLASH = ('a regular expression literal or a / operator (no regular expression literals or / operators in a '
            'checked Workflow script: use new RegExp("...") or multiply instead)')
_WF_ODD_LINE_END = re.compile('\r(?!\n)|[\u2028\u2029]')
_WF_PUNCT = re.compile(r'>>>=|\.\.\.|===|!==|\*\*=|<<=|>>=|>>>|&&=|\|\|=|\?\?=|=>|==|!=|<=|>=|&&|\|\||\?\?|'
                       r'\?\.(?!\d)|\+\+|--|\+=|-=|\*=|%=|&=|\|=|\^=|\*\*|<<|>>|[{}()\[\];,<>+\-*%&|^!~?:=.]')
_WF_NAME = re.compile(r'[A-Za-z_$][\w$]*', re.A)
_WF_NUMBER = re.compile(r'(?:0[xXoObB][\da-fA-F_]+|(?:\d[\d_]*\.?[\d_]*|\.\d[\d_]*)(?:[eE][+-]?\d+)?)n?', re.A)
_OPEN = {'(': ')', '[': ']', '{': '}', '`${': '}'}


def _js_tokens(src):
    """The script as tokens [kind, text, line, extra]: kind name, num, str (extra: it has an escape) or punct. A
    template literal without ${} is a str; one with ${} is the puncts '`${', '}${' and '}`' around its code. Raises
    ScriptError for what the check refuses, among it every / that does not open a comment and every line end
    other than \\n and \\r\\n."""
    toks, stack, parens = [], [], 0
    i, n, line = 0, len(src), 1

    def fail(msg):
        raise ScriptError(f'line {line}: {msg}')

    odd = _WF_ODD_LINE_END.search(src)
    if odd:
        line += src.count('\n', 0, odd.start())
        fail(f'a line end other than \\n or \\r\\n ({odd.group()!r}), which JavaScript also ends a // comment at')

    def template():
        nonlocal i, line
        buf, esc = [], False
        while i < n:
            c = src[i]
            if c == '\\':
                esc = True
                line += src[i + 1:i + 2] == '\n'
                buf.append(src[i:i + 2])
                i += 2
            elif c == '`':
                i += 1
                return ''.join(buf), esc, True
            elif src.startswith('${', i):
                i += 2
                return ''.join(buf), esc, False
            else:
                line += c == '\n'
                buf.append(c)
                i += 1
        fail('a template literal is not closed')

    while i < n:
        c = src[i]
        if c in ' \t\r\f\v':
            i += 1
        elif c == '\n':
            line += 1
            i += 1
        elif not c.isascii():
            fail(f'a character outside ASCII ({c!r}) outside a string or comment')
        elif src.startswith('//', i):
            j = src.find('\n', i)
            i = n if j < 0 else j
        elif src.startswith('/*', i):
            j = src.find('*/', i + 2)
            if j < 0:
                fail('a comment is not closed')
            line += src.count('\n', i, j)
            i = j + 2
        elif src.startswith('<!--', i) or src.startswith('-->', i):
            fail('an HTML-like comment (<!-- or -->)')
        elif c in '\'"':
            j, buf, esc = i + 1, [], False
            while True:
                if j >= n or src[j] == '\n':
                    fail('a string is not closed on its line')
                if src[j] == '\\':
                    esc = True
                    line += src[j + 1:j + 2] == '\n'
                    j += 2
                    continue
                if src[j] == c:
                    break
                buf.append(src[j])
                j += 1
            toks.append(['str', ''.join(buf), line, esc])
            i = j + 1
        elif c == '`':
            start = line
            i += 1
            text, esc, closed = template()
            if closed:
                toks.append(['str', text, start, esc])
            else:
                toks.append(['punct', '`${', start, None])
                stack.append(0)
        elif c == '/':
            fail(WF_SLASH)
        elif c.isdigit() or (c == '.' and src[i + 1:i + 2].isdigit()):
            m = _WF_NUMBER.match(src, i)
            toks.append(['num', m.group(), line, None])
            i = m.end()
        elif _WF_NAME.match(src, i):
            m = _WF_NAME.match(src, i)
            toks.append(['name', m.group(), line, None])
            i = m.end()
        elif _WF_PUNCT.match(src, i):
            text = _WF_PUNCT.match(src, i).group()
            i += len(text)
            if text == '{' and stack:
                stack[-1] += 1
            elif text == '}' and stack:
                if stack[-1] == 0:
                    stack.pop()
                    body, esc, closed = template()
                    toks.append(['punct', '}`' if closed else '}${', line, None])
                    if not closed:
                        stack.append(0)
                    continue
                stack[-1] -= 1
            if text == '(':
                parens += 1
            elif text == ')':
                if not parens:
                    fail('a ) without its (')
                parens -= 1
            toks.append(['punct', text, line, None])
        else:
            fail(f'the character {c!r} outside a string or comment')
    if stack or parens:
        fail('the script ends inside a template literal or brackets')
    return toks


def _split(toks, start, close):
    """The comma-separated parts of a bracket whose opener is at toks[start]: (parts as token lists, the index of
    its closer). Raises ScriptError when the brackets do not match."""
    parts, cur, depth, k = [], [], [], start + 1
    while k < len(toks):
        kind, text = toks[k][0], toks[k][1]
        if kind == 'punct' and not depth and text == close:
            return parts + [cur], k
        if kind == 'punct':
            if text in _OPEN:
                depth.append(_OPEN[text])
            elif text == '}${':
                if not depth or depth[-1] != '}':
                    raise ScriptError(f'line {toks[k][2]}: brackets do not match')
                depth[-1] = '}'
            elif text in (')', ']', '}', '}`'):
                if not depth or depth[-1] != text[0]:
                    raise ScriptError(f'line {toks[k][2]}: brackets do not match')
                depth.pop()
            elif text == ',' and not depth:
                parts.append(cur)
                cur = []
                k += 1
                continue
        cur.append(toks[k])
        k += 1
    raise ScriptError(f'line {toks[start][2]}: a {toks[start][1]} is not closed')


def _agent_options(toks, k, where):
    """(model, effort) of the agent( call whose ( is at toks[k]; raises ScriptError when the call is not of the
    one form the guard reads: agent(prompt, {key: value, ...}) with model and effort plain texts, given once."""
    args, _ = _split(toks, k, ')')
    if args and not args[-1]:
        args.pop()  # a trailing comma
    if len(args) != 2 or not args[0] or any(t[1] == '...' and t[0] == 'punct' for a in args for t in a[:1]):
        raise ScriptError(f'{where} must have two arguments: the prompt and an options object literal')
    opts = args[1]
    if opts[0][:2] != ['punct', '{']:
        raise ScriptError(f'{where}: its options must be an object literal written in the call')
    props, end = _split(opts, 0, '}')
    if end != len(opts) - 1:
        raise ScriptError(f'{where}: its options must be one object literal and nothing more')
    if props and not props[-1]:
        props.pop()
    found = {}
    for prop in props:
        key = prop[0] if prop else None
        if key is None or key[0] not in ('name', 'str', 'num') or key[0] == 'str' and key[3] \
                or len(prop) < 2 or prop[1][:2] != ['punct', ':']:
            raise ScriptError(f'{where}: every option must be written key: value (no spread, computed key, method '
                              f'or shorthand)')
        if key[1] == '__proto__':
            raise ScriptError(f'{where}: an option named __proto__')
        if key[1] in ('model', 'effort'):
            value = prop[2:]
            if key[1] in found:
                raise ScriptError(f'{where}: {key[1]} is given twice')
            if len(value) != 1 or value[0][0] != 'str' or value[0][3]:
                raise ScriptError(f'{where}: its {key[1]} must be one plain text literal')
            found[key[1]] = value[0][1]
    return found.get('model'), found.get('effort')


def workflow_problems(args, rows, problems, matched):
    """Check a Workflow call: only an inline script the guard can read, whose every agent() call names an allowed
    model and effort."""
    for key in ('scriptPath', 'name'):
        if args.get(key) is not None:
            problems.append(f'Workflow {key} runs a script the guard cannot read at run time: pass the script '
                            f'inline as "script"')
            return
    script = args.get('script')
    if not isinstance(script, str) or not script.strip():
        problems.append('Workflow has no inline "script" to check')
        return
    if len(script.encode('utf-8', 'surrogatepass')) > WF_MAX_BYTES:
        problems.append(f'the Workflow script is larger than {WF_MAX_BYTES} bytes')
        return
    try:
        toks = _js_tokens(script)
        calls, opens, heads, of_words = 0, [], [], set()

        def dot_before(j):
            return j >= 1 and toks[j - 1][0] == 'punct' and toks[j - 1][1] in ('.', '?.')

        for k, (kind, text, line, _) in enumerate(toks):
            prev = toks[k - 1] if k else None
            after_dot = dot_before(k)
            if kind == 'name' and text == 'of' and not after_dot and opens and opens[-1] == '(' and heads[-1] \
                    and not heads[-1]['of'] and prev is not None \
                    and (prev[0] == 'name' and (prev[1] not in WF_EXPRESSION_WORDS or prev[1] == 'of'
                                                or dot_before(k - 1))
                         or prev[0] == 'punct' and prev[1] in (']', '}', ')')):
                heads[-1]['of'] = True   # the for-of keyword; any other "of" is a name
                of_words.add(k)
            if kind == 'name' and text in WF_FORBIDDEN:
                raise ScriptError(f'line {line}: uses {text!r}, which the guard cannot follow')
            if kind == 'name' and text == 'workflow' and not after_dot:
                raise ScriptError(f'line {line}: runs another workflow, whose script the guard cannot read')
            if kind == 'name' and text == 'agent' and not after_dot:
                if k + 1 >= len(toks) or toks[k + 1][:2] != ['punct', '(']:
                    raise ScriptError(f'line {line}: uses agent other than as a direct call agent(prompt, '
                                      f'{{model, effort, ...}})')
                calls += 1
                where = f'Workflow agent() call {calls} (line {line})'
                model, effort = _agent_options(toks, k + 1, where)
                check_pick(where, model, [] if effort is None else [effort], CLAUDE_DRIVER, rows, True, problems,
                           matched)
            if kind != 'punct':
                continue
            if text == '[':
                word = prev is not None and prev[0] == 'name' and prev[1] in WF_EXPRESSION_WORDS \
                    and not dot_before(k - 1) and (prev[1] != 'of' or k - 1 in of_words)
                if prev is not None and (prev[0] in ('num', 'str')
                                         or prev[0] == 'name' and not word
                                         or prev[0] == 'punct' and prev[1] in (')', ']', '}', '}`', '?.')):
                    if toks[k + 1][0] != 'num' or toks[k + 2][:2] != ['punct', ']']:
                        raise ScriptError(f'line {line}: a computed member access other than [number]')
                elif prev is not None and prev[0] == 'punct' and (prev[1] == '{' or prev[1] == ','
                                                                  and opens and opens[-1] == '{'):
                    raise ScriptError(f'line {line}: a computed key')
            if text in _OPEN:
                opens.append(text)
                for_head = text == '(' and prev is not None and prev[0] == 'name' and not after_dot and (
                    prev[1] == 'for' or prev[1] == 'await' and k >= 2 and toks[k - 2][:2] == ['name', 'for'])
                heads.append({'of': False} if for_head else None)
            elif text in ('}', ')', ']', '}`', '}${'):
                if not opens or _OPEN[opens[-1]] != text[0]:
                    raise ScriptError(f'line {line}: brackets do not match')
                opens.pop()
                heads.pop()
                if text == '}${':
                    opens.append('`${')
                    heads.append(None)
    except ScriptError as e:
        problems.append(f'the Workflow script cannot be checked ({e}); write each agent() call as agent(prompt, '
                        f'{{model: "...", effort: "...", ...}}) with no tricks')
    except IndexError:
        problems.append('the Workflow script cannot be checked (it ends in the middle of an expression)')


def check_call(kind, args, rows):
    """(problems, matched rows) for one checked tool call."""
    problems, matched = [], []
    args = args if isinstance(args, dict) else {}
    if kind == 'agent':
        effort = args.get('effort')
        check_pick('the Agent call', args.get('model'), [] if effort is None else [effort], CLAUDE_DRIVER, rows,
                   True, problems, matched)
        return problems, matched
    if kind == 'workflow':
        workflow_problems(args, rows, problems, matched)
        return problems, matched
    if kind == 'remote':
        action = args.get('action')
        if action not in REMOTE_READ_ACTIONS:
            problems.append(f'RemoteTrigger {action!r} creates, changes or fires a cloud routine, which runs on a '
                            f'model the guard cannot see (only {", ".join(REMOTE_READ_ACTIONS)} pass)')
        return problems, matched

    def target(where, value, selection=False):
        if not isinstance(value, dict):
            problems.append(f'{where} is missing: name its model and options.effort (nothing is inherited)')
            return
        if selection and 'driverKind' in value:
            # A ModelSelection (t3_thread_launch, t3_thread_configure, a project's default) has only instanceId or
            # provider, model and options; T3 drops any other key (packages/contracts/src/modelSelection.ts) and
            # runs the pick on the instance, whatever driver this names. So the guard never reads it there: such a
            # pick is checked against every row of its model, and a driverKind is refused so it cannot mislead.
            problems.append(f'{where}.driverKind: T3 ignores it on a model selection (it runs the pick on the named '
                            f'instance), so the guard cannot use it: leave it out, and every row of the model must '
                            f'allow the pick')
            return
        pairs = _option_pairs(value.get('options'), where, problems)
        if pairs is None:
            return
        # only a delegate_task or create_threads target has a real driverKind: T3 refuses an instance of another
        # driver there (OrchestratorMcpService.ts resolveTarget). On a model selection it is refused above.
        model, driver = value.get('model'), value.get('driverKind')
        check_pick(where, model, None, driver, rows, False, problems, matched, pairs)
        # A Claude pick that leaves ultracode out can still run it: delegate_task with the parent's model and instance
        # starts from the parent's options and lays the given ones over them (OrchestratorMcpService.ts resolveTarget,
        # "Given values override inherited"), so under a parent running ultracode the child runs it too; and T3 takes
        # the catalog's default where the default effort is ultracode (ClaudeModelCatalog.ts ultracodeByDefault).
        # So a pick whose row does not allow ultracode must say ultracode: false itself, unless the row says the
        # model has no ultracode option (T3 refuses the option there, and nothing can turn it on).
        if driver in (None, CLAUDE_DRIVER) and isinstance(model, str) \
                and ULTRACODE_OPTION_ID not in (key for key, _ in pairs) \
                and any(r['driver'] == CLAUDE_DRIVER and r['model'] == js_trim(model) and r['ultracodeOption']
                        and ULTRACODE_EFFORT not in (r['efforts'] or ()) for r in rows):
            problems.append(f'{where}: options must name ultracode (false): without it {js_trim(model)!r} can run '
                            f'ultracode (inherited from the parent thread, or the model\'s default). If T3 offers '
                            f'this model no ultracode option, the policy row says "ultracodeOption": false')

    if kind in NO_MODEL_TOOLS:
        for key in args:
            if isinstance(key, str) and MODEL_FIELD.fullmatch(key):
                problems.append(f'{key}: {kind} takes no model, so the guard cannot see what this field would do. '
                                f'Set a model with t3_thread_configure, t3_thread_launch or delegate_task')
    if kind == 't3_thread_transfer' and args.get('action') == 'fork':
        problems.append('t3_thread_transfer fork starts a thread on the source thread\'s model, which the guard '
                        'cannot see: start it with t3_thread_launch and a modelSelection instead')
    elif kind == 'delegate_task':
        target('target', args.get('target'))
    elif kind == 'create_threads':
        threads = args.get('threads')
        if not isinstance(threads, list) or not threads:
            problems.append('threads is missing or empty')
        for i, thread in enumerate(threads if isinstance(threads, list) else []):
            target(f'threads[{i}].target', thread.get('target') if isinstance(thread, dict) else None)
    elif kind == 't3_thread_launch':
        target('modelSelection', args.get('modelSelection'), selection=True)
    elif kind == 't3_thread_configure' and 'modelSelection' in args:   # without it the call only reads the thread
        target('modelSelection', args.get('modelSelection'), selection=True)
    elif kind in ('t3_project_create', 't3_project_update') and args.get('defaultModelSelection') is not None:
        # the project's default model: schedule_task and other callers that name none run on it. Left out or null,
        # the call sets no model (the project falls back to a default this call does not set), so only a given
        # selection is checked.
        target('defaultModelSelection', args.get('defaultModelSelection'), selection=True)
    return problems, matched


def _out(decision=None, reason=None, context=None):
    out = {'hookEventName': 'PreToolUse'}
    if decision:
        out['permissionDecision'], out['permissionDecisionReason'] = decision, reason
    if context:
        out['additionalContext'] = context
    return {'hookSpecificOutput': out}


SYSLOG_IDENT = KIT
RECORD_MAX = 4000


def syslog_record(text):
    """Write one line to the system log under SYSLOG_IDENT (journalctl -t model-guard; /var/log/syslog or
    auth.log elsewhere)."""
    import syslog
    syslog.openlog(SYSLOG_IDENT, 0, syslog.LOG_USER)
    syslog.syslog(syslog.LOG_WARNING, text)


def _record(record, payload, text):
    """Hand the owner's record one line; a recorder that fails never changes the hook's answer."""
    if record is None:
        return
    session = payload.get('session_id') if isinstance(payload, dict) else None
    line = (f'session {session}: ' if isinstance(session, str) else '') + text
    try:
        record(line[:RECORD_MAX])
    except Exception:  # noqa: BLE001 - the record is best effort; the answer stands
        pass


def t3_policy_path(platform=None):
    """Where T3 reads its managed policy file on this platform, or None where the hook cannot read one."""
    platform = platform or sys.platform
    return next((p for name, p in T3_POLICY_PATHS.items() if platform.startswith(name)), None)


def _covers(h, r):
    """Whether the guard's row h allows at least every pick T3's locked row r allows. A row without efforts allows
    only a pick that names no effort, on both sides; a row with efforts refuses such a pick, so r without efforts is
    covered only by h without efforts, and r with efforts only by h listing each of them."""
    if h['driver'] != r.get('driver') or h['model'] != r.get('model'):
        return False
    efforts = r.get('efforts')
    if efforts is None:
        return h['efforts'] is None
    return isinstance(efforts, list) and h['efforts'] is not None and all(e in h['efforts'] for e in efforts)


def _wider_row(t3_rows, rows):
    """The first row T3's locked policy allows that no row of the guard's policy covers, or None."""
    for r in t3_rows:
        if not isinstance(r, dict) or not any(_covers(h, r) for h in rows):
            return r
    return None


# a row of T3's locked modelPolicy, as packages/contracts/src/modelPolicy.ts reads it: any other key, or a value it
# does not decode, makes T3 read the whole policy as `unreadable` (enforce, nothing allowed)
T3_ROW_KEYS = ('driver', 'model', 'efforts', 'defaultEffort', 'useFor')
T3_DRIVER = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]*')     # ProviderDriverKind (providerInstance.ts), at most 64
T3_DRIVER_MAX = 64
# the marker a T3 server that checks SERVER_CHECKED_TOOLS itself writes beside its server-runtime.json at start
# (apps/server/src/fork/modelPolicyRoutesMarker.ts); an older T3 loads the lock too but checks neither route
T3_ROUTES_MARKER = 'fork-capabilities.json'
T3_STATE_MAX = 1 << 20
UNREADABLE_LOCK = 'locks a modelPolicy T3 reads as unreadable'


def _trimmed(value, empty=False):
    """Whether T3 reads `value` as the same text it encodes back (TrimmedString; non-blank unless `empty`)."""
    return isinstance(value, str) and js_trim(value) == value and (empty or value != '')


def _t3_value_problem(value):
    """Why T3 would read this locked modelPolicy value as `unreadable` (or refuse to start on it), or None. T3
    starts on a value only when it reads back as written: mode and allow both given, no other key, each text
    already trimmed (managedSettings.ts round trip)."""
    if not isinstance(value, dict):
        return 'its value is not a JSON object'
    extra = sorted(set(value) - {'mode', 'allow'})
    if extra:
        return f'its value has a key T3 does not read ({extra[0]!r})'
    if value.get('mode') not in MODES:
        return 'its value needs a mode of ' + ', '.join(MODES)
    if not isinstance(value.get('allow'), list):
        return 'its value needs a list of allow rows'
    for i, row in enumerate(value['allow']):
        where = f'allow[{i}]'
        if not isinstance(row, dict):
            return f'{where} is not a JSON object'
        extra = sorted(set(row) - set(T3_ROW_KEYS))
        if extra:
            return f'{where} has a key T3 does not read ({extra[0]!r})'
        driver = row.get('driver')
        if not _trimmed(driver) or len(driver) > T3_DRIVER_MAX or not T3_DRIVER.fullmatch(driver):
            return f'{where}: driver must be a driver name'
        if not _trimmed(row.get('model')):
            return f'{where}: model must be a non-empty text'
        efforts = row.get('efforts')
        if 'efforts' in row and (not isinstance(efforts, list) or not efforts
                                 or not all(_trimmed(e) for e in efforts)):
            return f'{where}: efforts must be a list of one or more non-empty texts'
        if 'defaultEffort' in row and (not _trimmed(row['defaultEffort'])
                                       or (efforts is not None and row['defaultEffort'] not in efforts)):
            return f'{where}: defaultEffort must be one of its efforts'
        if 'useFor' in row and not _trimmed(row['useFor'], empty=True):
            return f'{where}: useFor must be a text'
    return None


def process_started(pid):
    """When the process started, in seconds since the epoch (as st_mtime), or None when that cannot be read. The
    earliest it can have been: each source counts whole seconds somewhere, and an earlier start is the safe side
    (a lock changed at that time counts as changed after the start)."""
    try:
        if sys.platform.startswith('linux'):
            with open(f'/proc/{int(pid)}/stat', encoding='ascii') as f:
                ticks = int(f.read().rsplit(')', 1)[1].split()[19])
            with open('/proc/stat', encoding='ascii') as f:
                boot = next(int(line.split()[1]) for line in f if line.startswith('btime '))
            return boot + ticks / os.sysconf('SC_CLK_TCK')
        if sys.platform == 'darwin':
            # elapsed time, [[dd-]hh:]mm:ss: no time zone in it, unlike lstart
            out = subprocess.run(['/bin/ps', '-o', 'etime=', '-p', str(int(pid))], capture_output=True, text=True,
                                 timeout=5, env={'LC_ALL': 'C'}).stdout.strip()
            m = re.fullmatch(r'(?:(?:(\d+)-)?(\d+):)?(\d+):(\d+)', out)
            if not m:
                return None
            days, hours, minutes, seconds = (int(g or 0) for g in m.groups())
            return time.time() - (((days * 24 + hours) * 60 + minutes) * 60 + seconds) - 1
    except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError):
        return None
    return None


def _alive(pid):
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except (OSError, OverflowError):
        return False
    return True


def _read_plain_json(path):
    """(the JSON in path, its os.stat_result): a plain file, not a link, read without waiting, at most
    T3_STATE_MAX bytes, no key given twice. Raises OSError or ValueError (RecursionError too) when it is not."""
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError('not a plain file')
        with os.fdopen(os.dup(fd), 'rb') as f:
            raw = f.read(T3_STATE_MAX + 1)
    finally:
        os.close(fd)
    if len(raw) > T3_STATE_MAX:
        raise ValueError(f'larger than {T3_STATE_MAX} bytes')
    return json.loads(raw.decode('utf-8'), object_pairs_hook=_no_duplicates), st


def _server_loaded(state_path, lock_path, changed):
    """None when the T3 server whose runtime state is at state_path runs, loaded its policy from lock_path,
    started after the lock last changed (T3 reads its managed policy only at start), and wrote the marker that says
    it checks SERVER_CHECKED_TOOLS itself; else why not. Anything that cannot be read is a why, never a crash."""
    try:
        return _server_loaded_or_raise(state_path, lock_path, changed)
    except (OSError, ValueError, RecursionError, TypeError, OverflowError) as e:
        return f'{state_path}: no running T3 server can be read there ({e})'


def _server_loaded_or_raise(state_path, lock_path, changed):
    state, _ = _read_plain_json(state_path)
    pid = state.get('pid') if isinstance(state, dict) else None
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0 or not _alive(pid):
        return f'{state_path}: no T3 server is running there'
    loaded = state.get('managedPolicy')
    source = loaded.get('source') if isinstance(loaded, dict) else None
    if not isinstance(source, str) or '\0' in source or os.path.realpath(source) != os.path.realpath(lock_path):
        return f'{state_path}: the running T3 server did not load {lock_path}; restart T3 first'
    started = process_started(pid)
    if started is None or started <= changed:
        return f'{state_path}: the running T3 server started before {lock_path} last changed; restart T3 first'
    marker_path = os.path.join(os.path.dirname(state_path), T3_ROUTES_MARKER)
    old = (f'{marker_path}: the running T3 server did not say it checks {", ".join(SERVER_CHECKED_TOOLS)} itself '
           f'(a T3 older than this kit); update T3')
    try:
        marker, marker_st = _read_plain_json(marker_path)
    except FileNotFoundError:
        return old
    except (OSError, ValueError, RecursionError) as e:   # a marker there that cannot be read says nothing either
        return (f'{marker_path}: the running T3 server did not say it checks {", ".join(SERVER_CHECKED_TOOLS)} '
                f'itself: its marker cannot be read ({e})')
    routes = marker.get('modelPolicyRoutes') if isinstance(marker, dict) else None
    # its own pid, written after the process started: a marker a stopped server left (its pid since reused) is not
    if not isinstance(marker, dict) or marker.get('version') != 1 or isinstance(marker.get('version'), bool) \
            or marker.get('pid') != pid or isinstance(marker.get('pid'), bool) or marker_st.st_mtime < started \
            or not isinstance(routes, list) or not all(t in routes for t in SERVER_CHECKED_TOOLS):
        return old
    return None


def server_lock(policy, path=None, owner_uid=0, servers=None):
    """None when T3's managed policy locks modelPolicy at the guard policy's mode or stricter, with a value T3 reads
    (not as `unreadable`) and no row the guard's rows do not cover, and every T3 server named in `servers` (the
    runtime state files from the machine file's t3ServerStates) loaded it and checks the tools in
    SERVER_CHECKED_TOOLS itself, so the T3 server checks them at least as the guard would and an agent cannot turn
    that off; else why not. Fails closed: a file that is missing, not root-owned, writable by group or others,
    behind a link, unreadable or not this shape counts as no lock, and so does a lock no named server has loaded
    yet (T3 reads it at start), a server older than this kit, or no server named at all."""
    mode = policy['mode']
    path = path or t3_policy_path()
    if path is None:
        return 'this platform has no T3 managed policy file the guard reads'
    try:
        document = json.loads(machine_file.read_protected_text(path, owner_uid=owner_uid),
                              object_pairs_hook=_no_duplicates)
        st = os.stat(path)
    except PolicyError as e:
        return f'T3\'s managed policy is not usable here ({e})'
    except (ValueError, RecursionError) as e:
        return f'{path}: not JSON ({e})'
    except OSError as e:
        return f'{path}: cannot be read ({e})'
    entry = (document.get('settings') if isinstance(document, dict) else None) or {}
    entry = entry.get('modelPolicy') if isinstance(entry, dict) else None
    if not isinstance(entry, dict) or entry.get('locked') is not True:
        return f'{path} does not lock modelPolicy'
    value = entry.get('value')
    unreadable = _t3_value_problem(value)
    if unreadable is not None:
        return (f'{path} {UNREADABLE_LOCK} ({unreadable}): T3 then refuses every pick '
                f'on its routes, so the guard checks them')
    if MODES.index(value['mode']) < MODES.index(mode):
        return f'{path} locks modelPolicy at {value["mode"]}, below this policy\'s {mode}'
    wider = _wider_row(value['allow'], policy['allow'])
    if wider is not None:
        return f'{path} allows {json.dumps(wider)}, which no row of this policy covers'
    if not servers:
        return ('the machine file names no T3 server (t3ServerStates), so the guard cannot tell that a running T3 '
                'loaded the lock')
    for state_path in servers:
        not_loaded = _server_loaded(state_path, path, max(st.st_mtime, st.st_ctime))
        if not_loaded is not None:
            return not_loaded
    return None


def _left_to_t3(tool, kind, cfg, policy, lock):
    """Whether the T3 server checks this call itself: T3's own server name only (another MCP server's tool of the
    same name never reaches T3), and only while its lock holds. A lock check that breaks keeps the guard's check."""
    if kind not in SERVER_CHECKED_TOOLS or tool != f'mcp__t3-code__{kind}' or lock is None:
        return False
    try:
        return lock(cfg, policy) is None
    except Exception:  # noqa: BLE001 - fail closed for the guard's own check; never block a call by crashing
        return False


def decide(payload, load, record=None, lock=None):
    """The hook's answer (a dict to print) or None (no output: the call goes on as without the hook).
    `load()` returns (machine section, policy) or raises PolicyError. Never returns 'allow': a passing call still
    goes through the session's own permission rules. `record(line)` gets every refusal and every advise note.
    `lock(cfg, policy)` is server_lock: None leaves a tool the T3 server checks to the server (_left_to_t3)."""
    tool = payload.get('tool_name') if isinstance(payload, dict) else None
    kind = tool_kind(tool)
    if kind is None:
        return None
    try:
        cfg, policy = load()
    except PolicyError as e:
        reason = (f'model-guard refused {tool}: the model policy cannot be used ({e}). The guard fails closed. Do '
                  f'not work around it: tell the person who runs this machine.')
        _record(record, payload, reason)
        return _out('deny', reason)
    if policy['mode'] == 'off':
        return None
    if _left_to_t3(tool, kind, cfg, policy, lock):
        return None
    problems, matched = check_call(kind, payload.get('tool_input'), policy['allow'])
    notes = '; '.join(f'{r["model"]} is for {r["useFor"]}' for r in matched if r['useFor'])
    if not problems:
        return _out(context=f'model-guard: {notes}.') if notes else None
    rows = '; '.join(describe(r) for r in policy['allow']) or 'none (every pick is refused)'
    why = (f'{"; ".join(problems)}. Name an allowed model and its effort on every such call. '
           f'Allowed rows: {rows}. The policy ({cfg["policyFile"]}) is the machine owner\'s; do not edit it.')
    if policy['mode'] == 'enforce':
        _record(record, payload, f'enforce: refused {tool}: {"; ".join(problems)}')
        return _out('deny', f'model-guard refused {tool}: {why}')
    _record(record, payload, f'advise: enforce would refuse {tool}: {"; ".join(problems)}')
    return _out(context=f'model-guard (advise mode, nothing refused): enforce would refuse this {tool} call: {why}')


def run_hook(stdin, stdout, machine_path=None, owner_uid=0, record=syslog_record, t3_policy=None):
    try:
        payload = json.loads(stdin.read())
    except ValueError as e:
        payload = None
        reason = f'model-guard refused: the hook input is not JSON ({e})'
    else:
        reason = 'model-guard refused: the hook input is not a JSON object'
    if not isinstance(payload, dict):
        _record(record, None, reason)
        out = _out('deny', reason)
    else:
        out = decide(payload, lambda: load_policy(machine_path, owner_uid), record,
                     lambda cfg, policy: server_lock(policy, t3_policy, owner_uid, cfg.get('t3ServerStates')))
    if out:
        stdout.write(json.dumps(out) + '\n')
    return 0


# ---------- drift: does Claude Code's managed settings run the hook?


def entry_problems(entry):
    """Why one PreToolUse entry is not the guard's (None when it is): it is exactly hook_entry(). The matcher must be
    exactly HOOK_MATCHER: the self-check does not try to follow Claude Code's matcher rules, which may change, and
    an entry from an older kit (whose matcher misses a tool added since) is reported until the installer runs."""
    if not isinstance(entry, dict) or set(entry) != {'matcher', 'hooks'}:
        return 'it is not exactly {"matcher", "hooks"}'
    if entry['matcher'] != HOOK_MATCHER:
        return f'its matcher is not exactly {HOOK_MATCHER!r} (run model-guard-install install to rewrite it)'
    if entry['hooks'] != hook_entry()['hooks']:
        return (f'its hooks are not exactly one command {HOOK_COMMAND!r} with onFailure "block" '
                '(no async, no timeout, nothing else)')
    return None


def settings_problems(files, allow_other_hooks):
    """Problems with the merged managed settings, given [(path, data)] in Claude Code's order (managed-settings.json,
    then managed-settings.d/*.json by name). The guard must run as one entry of its own, nothing may switch hooks
    off, and only managed hooks may run unless the machine file allows others."""
    problems, good, only_managed = [], [], None
    for path, data in files:
        if not isinstance(data, dict):
            problems.append(f'{path}: not a JSON object')
            continue
        if 'disableAllHooks' in data and data['disableAllHooks'] not in (False, 'false'):
            problems.append(f'{path}: disableAllHooks is set, so no hook runs')
        if 'allowManagedHooksOnly' in data:
            only_managed = data['allowManagedHooksOnly']
        hooks = data.get('hooks')
        entries = hooks.get('PreToolUse') if isinstance(hooks, dict) else None
        for i, entry in enumerate(entries if isinstance(entries, list) else []):
            why = entry_problems(entry)
            if why is None:
                good.append(path)
            elif HOOK_LINK in json.dumps(entry):
                problems.append(f'{path}: PreToolUse entry {i} runs the guard but is not its entry: {why}')
    if not good:
        problems.append(f'no managed PreToolUse entry runs the guard (matcher {HOOK_MATCHER!r}, one command '
                        f'{HOOK_COMMAND!r}): run model-guard-install install')
    if only_managed is not True and not allow_other_hooks:
        problems.append('allowManagedHooksOnly is not true, so a user or project hook can change a checked call '
                        'after the guard has seen it (set it in managed-settings.json, or "allowOtherHooks": true '
                        f'in the machine file\'s "{KIT}" section to accept that)')
    return problems


# Claude Code's own model limits (live-tested with Claude Code 2.1.325, see README "Claude Code's model keys"): they
# bind the main session, Agent sub-agents and Workflow agents, whatever the call names, so in enforce mode the
# managed settings must carry them, made from the policy's claudeAgent rows.
EFFORT_ORDER = ('low', 'medium', 'high', 'xhigh', 'max')
# Claude Code's model aliases: the env variable that maps each to a model (an update of Claude Code moves the map)
FAMILY_ALIASES = ('opus', 'sonnet', 'haiku', 'fable')
# variables that make a sub-agent or Workflow agent run on another model or effort than the call the guard saw;
# empty is the neutral value (Claude Code ignores an empty one), and managed env wins over every other source
NEUTRAL_ENV = ('CLAUDE_CODE_SUBAGENT_MODEL', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE', 'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL',
               'CLAUDE_CODE_SUBAGENT_EFFORT')


def model_settings(policy):
    """The managed settings keys enforce mode needs, made from the policy's claudeAgent rows: {key: value}."""
    rows = [r for r in policy['allow'] if r['driver'] == CLAUDE_DRIVER]
    env = {name: '' for name in NEUTRAL_ENV}
    for r in rows:
        for alias in r['aliases'] or ():
            if alias in FAMILY_ALIASES:
                env[f'ANTHROPIC_DEFAULT_{alias.upper()}_MODEL'] = r['model']
    out = {'availableModels': sorted({r['model'] for r in rows}), 'enforceAvailableModels': True}
    efforts = [ULTRACODE_RUNS_AT if e == ULTRACODE_EFFORT else e for r in rows for e in (r['efforts'] or ())]
    known = [e for e in efforts if e in EFFORT_ORDER]
    if known:
        out['maxEffortLevel'] = max(known, key=EFFORT_ORDER.index)
    out['env'] = env
    return out


def model_settings_problems(files, policy):
    """Where the merged managed settings ([(path, data)], later files win, env merged by name) miss or differ from
    model_settings(policy). Only enforce mode needs them: they bind in every mode, so advise and off do not."""
    if policy['mode'] != 'enforce':
        return []
    want = model_settings(policy)
    if not want['availableModels']:
        return []  # no claudeAgent row: the guard refuses every Claude pick, and availableModels cannot say "none"
    have, env, lists = {}, {}, []
    for _, data in files:
        if not isinstance(data, dict):
            continue
        for key in ('availableModels', 'enforceAvailableModels', 'maxEffortLevel'):
            if key in data:
                have[key] = data[key]
        if 'availableModels' in data:
            lists.append(data['availableModels'])
        if isinstance(data.get('env'), dict):
            env.update(data['env'])
    problems = []
    if any(not isinstance(v, list) or not all(isinstance(m, str) for m in v) for v in lists):
        problems.append(f'availableModels is not a list of texts in every file ({lists!r})')
    # the last file's list must be the rows' models, and no earlier one may name more: the check holds whether
    # Claude Code takes the last list or joins the lists of all managed files
    elif not lists or sorted(set(lists[-1])) != want['availableModels'] \
            or not {m for v in lists for m in v} <= set(want['availableModels']):
        problems.append(f'availableModels is {have.get("availableModels")!r} (in all files {lists!r}), not the '
                        f'claudeAgent rows\' models {want["availableModels"]!r}')
    if have.get('enforceAvailableModels') is not True:
        problems.append('enforceAvailableModels is not true')
    if 'maxEffortLevel' in want and have.get('maxEffortLevel') != want['maxEffortLevel']:
        problems.append(f'maxEffortLevel is {have.get("maxEffortLevel")!r}, not the rows\' highest effort '
                        f'{want["maxEffortLevel"]!r}')
    for name, value in want['env'].items():
        if env.get(name) != value:
            problems.append(f'env {name} is {env.get(name)!r}, not {value!r}')
    if problems:
        problems = ['managed settings miss Claude Code\'s own model limits (' + '; '.join(problems) + '). Put these '
                    'keys in managed-settings.json: ' + json.dumps(want, sort_keys=True)]
    return problems


# ---------- drift: does T3 still offer no ultracode option to an "ultracodeOption": false row's model? (#600)
# T3 reloads its model list (the model manifest) from upstream about every hour and keeps it in its state folder
# (apps/server/src/provider/ModelManifest.ts: <stateDir>/model-manifest.json, {fetchedAtMs, manifest}). The machine
# file names those files ("t3ModelManifests"); the self-check reads each. They are the T3 user's files, so this is
# a report, not a control: a refusal never depends on them.
T3_MANIFEST_MAX = 16 * 1024 * 1024
ROW_FIX = 'take "ultracodeOption": false out of its policy row, and name "ultracode": false in its picks'


def read_t3_manifest(path):
    """(T3's claudeAgent model list in one manifest file, None) or (None, why it cannot be used). The file is T3's
    cache ({fetchedAtMs, manifest}) or a manifest itself; a plain file, not a link, read without waiting."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    except OSError as e:
        return None, f'cannot be read ({e.strerror})'
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None, 'is not a plain file'
        with os.fdopen(os.dup(fd), 'rb') as f:
            raw = f.read(T3_MANIFEST_MAX + 1)
    except OSError as e:
        return None, f'cannot be read ({e.strerror})'
    finally:
        os.close(fd)
    if len(raw) > T3_MANIFEST_MAX:
        return None, f'is larger than {T3_MANIFEST_MAX} bytes'
    try:
        data = json.loads(raw.decode('utf-8'))
    except (ValueError, RecursionError) as e:
        return None, f'is not valid JSON ({e})'
    if isinstance(data, dict) and 'manifest' in data and 'version' not in data:
        data = data['manifest']     # T3's cache
    if not isinstance(data, dict) or data.get('version') != 1 or isinstance(data.get('version'), bool):
        return None, 'is not a T3 model manifest of version 1'
    providers = data.get('providers')
    catalog = providers.get(CLAUDE_DRIVER) if isinstance(providers, dict) else None
    if not isinstance(catalog, dict) or not isinstance(catalog.get('models'), list) \
            or not isinstance(catalog.get('profiles'), dict):
        return None, f'has no {CLAUDE_DRIVER} model list'
    return catalog, None


def _offers_ultracode(descriptor):
    """True when an option descriptor gives the model ultracode: the option itself, or a select with the choice
    ultracode (T3 turns the effort choice into the option, ClaudeModelCatalog.ts withUltracodeToggle)."""
    if not isinstance(descriptor, dict):
        return False
    choices = descriptor.get('options')
    return descriptor.get('id') == ULTRACODE_OPTION_ID or descriptor.get('type') == 'select' \
        and isinstance(choices, list) and any(isinstance(c, dict) and c.get('id') == ULTRACODE_EFFORT for c in choices)


def _model_entries(catalog, model):
    """The option list of each entry of one T3 model list that T3 resolves model to (None: no option list). T3 takes
    the entry whose slug is model, and only when none is, the first whose alias is it (in any case). Several entries
    with that slug (a list T3 refuses) all count, so the worst one wins."""
    entries = [e for e in catalog['models'] if isinstance(e, dict)]
    def alias_of(entry):
        aliases = entry.get('aliases') if isinstance(entry.get('aliases'), list) else []
        return any(isinstance(a, str) and js_trim(a).lower() == model.lower() for a in aliases)
    named = [e for e in entries if isinstance(e.get('slug'), str) and js_trim(e['slug']) == model]
    named = named or [e for e in entries if alias_of(e)][:1]
    found = []
    for entry in named:
        name = entry.get('profile')
        profile = catalog['profiles'].get(js_trim(name)) if isinstance(name, str) else None
        capabilities = profile.get('capabilities') if isinstance(profile, dict) else None
        descriptors = capabilities.get('optionDescriptors') if isinstance(capabilities, dict) else None
        found.append(descriptors if isinstance(descriptors, list) else None)
    return found


def ultracode_option_in(catalog, model):
    """What one T3 model list says of model: 'option' (T3 offers it ultracode), 'no list' (it has no option list, so
    T3 checks no options), 'missing' (not in the list) or 'none' (an option list without ultracode). Every entry
    T3 resolves the model to counts (_model_entries); the worst one wins."""
    entries = _model_entries(catalog, model)
    if not entries:
        return 'missing'
    found = 'none'
    for descriptors in entries:
        if descriptors is None:
            found = 'no list'
        elif any(_offers_ultracode(d) for d in descriptors):
            return 'option'
    return found


def ultracode_option_problems(policy, manifest_paths):
    """Problems with the policy's "ultracodeOption": false rows against T3's model lists (manifest_paths, from the
    machine file): a model T3 now offers ultracode, one with no option list or not in the list, and a list that
    cannot be read or is not named. Mode off checks nothing, so it needs none."""
    rows = [r for r in policy['allow'] if r['driver'] == CLAUDE_DRIVER and not r['ultracodeOption']]
    if not rows or policy['mode'] == 'off':
        return []
    models = ', '.join(repr(r['model']) for r in rows)
    if not manifest_paths:
        return [f'policy rows with "ultracodeOption": false ({models}), but the machine file\'s "{KIT}" section '
                f'names no T3 model list ("t3ModelManifests"), so the self-check cannot tell whether T3 now offers '
                f'them the ultracode option']
    problems = []
    for path in manifest_paths:
        catalog, why = read_t3_manifest(path)
        if why:
            problems.append(f'T3 model list {path} {why}: the self-check cannot tell whether T3 now offers {models} '
                            f'the ultracode option')
            continue
        for row in rows:
            model, state = row['model'], ultracode_option_in(catalog, row['model'])
            if state == 'option':
                problems.append(f'{model!r} has "ultracodeOption": false, but T3\'s model list {path} offers it the '
                                f'ultracode option, so a pick that leaves the option out can run ultracode '
                                f'(inherited from the parent, or the model\'s default): {ROW_FIX}')
            elif state == 'no list':
                problems.append(f'{model!r} has "ultracodeOption": false, but T3\'s model list {path} gives it no '
                                f'option list, so T3 checks no options for it and keeps an inherited ultracode: '
                                f'{ROW_FIX}')
            elif state == 'missing':
                problems.append(f'{model!r} has "ultracodeOption": false, but it is not in T3\'s model list {path} '
                                f'(a custom model, or one T3 no longer lists), so the self-check cannot see its '
                                f'options, and T3 checks none for a model without an option list: {ROW_FIX}')
    return problems


def efforts_offered_in(catalog, model):
    """What one T3 model list says of the levels a Claude model offers: (the levels, None) or (None, 'missing' when
    no entry names it | 'no list' when an entry has no option list). A level is one choice of the "effort" select;
    xhigh also when that select offers ultracode (T3 puts xhigh in its place when the list has none); ultracode also
    when T3 offers the model the ultracode option (T3 turns the old effort into it). Every entry T3 resolves the
    model to counts (_model_entries): a level is offered only when each offers it (the worst one wins)."""
    entries = _model_entries(catalog, model)
    if not entries:
        return None, 'missing'
    if None in entries:
        return None, 'no list'
    offered = None
    for descriptors in entries:
        levels = {c['id'] for d in descriptors if isinstance(d, dict) and d.get('id') == CLAUDE_EFFORT_ID
                  and d.get('type') == 'select' and isinstance(d.get('options'), list)
                  for c in d['options'] if isinstance(c, dict) and isinstance(c.get('id'), str)}
        if ULTRACODE_EFFORT in levels:
            levels.add(ULTRACODE_RUNS_AT)
        if any(_offers_ultracode(d) for d in descriptors):
            levels.add(ULTRACODE_EFFORT)
        offered = levels if offered is None else offered & levels
    return frozenset(offered), None


def effort_offer_problems(policy, manifest_paths):
    """(problems, notes) for the allowed levels of the policy's Claude rows against T3's model lists (#643): a level
    the model does not offer (T3's delegate_task refuses a pick of it, but its other paths, such as a launch, a
    configure or a project default, start the model with no level, so it runs its default), a model with no option
    list, a list that cannot be read or is not named, and a list that has none of the rows' models (likely the wrong
    file). One model not in a list that has others (a custom model) is a note: there is nothing to compare. T3's model list holds option lists for Claude models only; other drivers' rows are not
    compared (README: list only the levels the model offers). Mode off checks nothing, so it needs none."""
    rows = [r for r in policy['allow'] if r['driver'] == CLAUDE_DRIVER and r['efforts']]
    if policy['mode'] == 'off' or not rows:
        return [], []
    models = ', '.join(repr(r['model']) for r in rows)
    if not manifest_paths:
        return ([f'{CLAUDE_DRIVER} policy rows with efforts ({models}), but the machine file\'s "{KIT}" section names '
                f'no T3 model list ("t3ModelManifests"), so the self-check cannot tell whether each model offers the '
                f'levels its row allows (a level it does not offer runs the model\'s default)'], [])
    problems, notes = [], []
    for path in manifest_paths:
        catalog, why = read_t3_manifest(path)
        if why is not None:
            problems.append(f'T3 model list {path} {why}: the self-check cannot tell whether {models} offer the '
                            f'levels their rows allow')
            continue
        found = {row['model']: efforts_offered_in(catalog, row['model']) for row in rows}
        if all(state == 'missing' for _, state in found.values()):
            problems.append(f'T3 model list {path} has none of {models}: it is likely not T3\'s model list, so the '
                            f'self-check cannot tell whether they offer the levels their rows allow')
            continue
        for row in rows:
            model = row['model']
            offered, state = found[model]
            if state == 'missing':
                notes.append(f'{model!r} is not in T3\'s model list {path} (a custom model, or one T3 no longer '
                             f'lists), so its allowed levels are not compared')
            elif offered is None:
                problems.append(f'{model!r} lists efforts, but T3\'s model list {path} gives it no option list, so '
                                f'T3 checks no level for it and the self-check cannot tell which it offers')
            else:
                missing = [e for e in row['efforts'] if e not in offered]
                if missing:
                    also = ' and defaultEffort' if row['defaultEffort'] in missing else ''
                    problems.append(f'{model!r} allows {", ".join(missing)}, but T3\'s model list {path} does not '
                                    f'offer {"it" if len(missing) == 1 else "them"} for that model, so a pick of '
                                    f'{"it" if len(missing) == 1 else "one"} runs the model\'s default level outside '
                                    f'delegate_task: take {"it" if len(missing) == 1 else "them"} out of the row\'s '
                                    f'efforts{also}')
    return problems, notes


def effort_id_problems(policy):
    """Rows whose every T3 pick is refused because the guard knows no option id their driver reads the effort from
    (#629): said by the self-check, so the owner adds effortOptionIds before enforce refuses the picks."""
    if policy['mode'] == 'off':
        return []
    return [f'{row["driver"]} {row["model"]!r} lists efforts, but the guard knows no option id {row["driver"]} reads '
            f'the effort from, so every T3 pick of it is refused: list that id in the row\'s effortOptionIds'
            for row in policy['allow'] if row['efforts'] is not None and not row_effort_ids(row)]


def managed_files(folder, owner_uid=0):
    """[(path, data, error)] of Claude Code's managed settings files in its order (managed-settings.json, then
    managed-settings.d/*.json by name); each must be root-owned and not writable by others, like the machine file.
    A missing managed-settings.json is fine; error is None or why the file cannot be used."""
    main = os.path.join(folder, 'managed-settings.json')
    drop_ins = os.path.join(folder, 'managed-settings.d')
    try:
        paths = [os.path.join(drop_ins, f) for f in sorted(os.listdir(drop_ins))
                 if f.endswith('.json') and not f.startswith('.')]
    except FileNotFoundError:
        paths = []
    except OSError as e:
        return [(drop_ins, None, f'cannot be listed ({e.strerror})')]
    files = []
    for path in [main] + paths:
        try:
            files.append((path, json.loads(machine_file.read_protected_text(path, owner_uid)), None))
        except PolicyError as e:
            if path != main or not str(e).endswith(': not there'):
                files.append((path, None, f'cannot be used: {e}'))
        except (ValueError, RecursionError) as e:
            files.append((path, None, f'not valid JSON ({e})'))
    return files


def self_check(machine_path=None, owner_uid=0, prefix=''):
    """PROBLEM lines on stdout and exit 1 when the kit's files changed, the policy cannot be used, or Claude Code's
    managed settings do not run the hook."""
    problems = []
    try:
        with open(prefix + INSTALLED, encoding='utf-8') as f:
            rows = [line.rstrip('\n').split('\t') for line in f if line.strip() and not line.startswith('#')]
    except OSError as e:
        rows = []
        problems.append(f'cannot read {INSTALLED} ({e.strerror}): the kit is not installed')
    for row in rows:
        if len(row) != 3:
            problems.append(f'{INSTALLED}: bad row {row!r}')
            continue
        path, kind, want = row
        try:
            if kind == 'link':
                ok = os.readlink(prefix + path) == want
            else:
                with open(prefix + path, 'rb') as f:
                    ok = hashlib.sha256(f.read()).hexdigest() == want
        except OSError:
            ok = False
        if not ok:
            problems.append(f'{path}: differs from what was installed ({kind})')
    allow_other_hooks, policy = False, None
    try:
        cfg, policy = load_policy(machine_path, owner_uid)
    except PolicyError as e:
        problems.append(f'the policy cannot be used, so every checked call is refused: {e}')
    else:
        allow_other_hooks = cfg['allowOtherHooks']
        print(f'policy {cfg["policyFile"]}: mode {policy["mode"]}, {len(policy["allow"])} rows')
        problems += ultracode_option_problems(policy, cfg['t3ModelManifests'])
        problems += effort_id_problems(policy)
        offer_problems, offer_notes = effort_offer_problems(policy, cfg['t3ModelManifests'])
        problems += offer_problems
        for note in offer_notes:
            print(f'note: {note}')
        t3 = t3_policy_path()
        why = server_lock(policy, prefix + t3 if t3 else None, owner_uid, cfg['t3ServerStates'])
        print(f'T3 server routes ({", ".join(SERVER_CHECKED_TOOLS)}): '
              + ('left to T3, whose managed policy locks modelPolicy' if why is None else f'checked here too: {why}'))
        if why is not None and UNREADABLE_LOCK in why:
            problems.append(why)    # T3 refuses every agent pick on its routes until the lock is fixed
    try:
        files = managed_files(prefix + managed_dir(), owner_uid)
    except PolicyError as e:
        problems.append(str(e))
    else:
        problems += [f'{path}: {error}' for path, _, error in files if error]
        usable = [(path, data) for path, data, error in files if not error]
        problems += settings_problems(usable, allow_other_hooks)
        if policy is not None:
            problems += model_settings_problems(usable, policy)
    for p in problems:
        print(f'PROBLEM: {p}')
    print('self-check: ' + ('OK' if not problems else f'{len(problems)} problems'))
    return 0 if not problems else 1
