"""model_guard: a Claude Code PreToolUse hook that keeps agents' model picks inside a policy (fork-kit/model-guard).

It checks the calls where an agent picks the model of another agent: T3's MCP tools delegate_task, create_threads,
t3_thread_launch and t3_thread_configure, and Claude Code's own Agent tool (Task in older versions). Every other
tool passes untouched.

The policy is a JSON file {"mode": "off" | "advise" | "enforce", "allow": [rows]}; the machine file
(fork-kit/common) names it in its "model-guard" section. Rules (README.md next to this folder):

- every checked call names its model and its effort, and both are in one row; else enforce refuses and lists
  the rows (the hook cannot see the parent's model or the provider's catalog, so nothing may be inherited);
- the policy file or the machine file missing, unreadable or invalid: refused, naming the file (fail closed);
- mode off: every call passes; advise: every call passes and the agent is told what enforce would do;
- a passing call gets the row's "useFor" note, so the agent sees what the model is for.

Plain Python 3 standard library. No state: the files are read again on every call.
"""
import hashlib, json, os, re, sys

HERE = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
for _common in (os.path.join(HERE, 'common'), os.path.join(os.path.dirname(HERE), 'common')):
    if os.path.isfile(os.path.join(_common, 'machine_file.py')):
        sys.path.insert(0, _common)
        break
import machine_file  # noqa: E402

KIT = 'model-guard'
MODES = ('off', 'advise', 'enforce')
# T3's driver kind of Claude: the native Agent tool only ever starts Claude agents
CLAUDE_DRIVER = 'claudeAgent'
# the option ids a model's effort control goes by (the same list as T3's usage pacing gate,
# apps/server/src/usage/UsagePacingGate.ts EFFORT_OPTION_IDS; tests/guard.test.py keeps the two in step)
EFFORT_OPTION_IDS = ('effort', 'reasoningEffort', 'reasoning_effort', 'reasoning', 'thinking_level')
T3_TOOLS = ('delegate_task', 'create_threads', 't3_thread_launch', 't3_thread_configure')
AGENT_TOOLS = ('Agent', 'Task')
MCP_TOOL = re.compile(r'^mcp__.+__(' + '|'.join(T3_TOOLS) + r')$')
# the hook as the settings files run it: `|| exit 2` blocks the call when the hook itself is missing
HOOK_LINK = '/usr/local/bin/model-guard'
HOOK_COMMAND = HOOK_LINK + ' || exit 2'
HOOK_MATCHER = '^(' + '|'.join(AGENT_TOOLS) + r'|mcp__.+__(' + '|'.join(T3_TOOLS) + '))$'
INSTALLED = '/etc/model-guard/installed.tsv'
PolicyError = machine_file.MachineFileError


def tool_kind(name):
    """'agent', the T3 tool's name, or None for a tool this hook does not check."""
    if name in AGENT_TOOLS:
        return 'agent'
    m = MCP_TOOL.match(name) if isinstance(name, str) else None
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


ROW = {'driver': (str, machine_file.REQUIRED, _text), 'model': (str, machine_file.REQUIRED, _text),
       'efforts': (list, None, _words), 'defaultEffort': (str, None, _text), 'aliases': (list, None, _words),
       'useFor': (str, None, None)}


def parse_policy(text, path):
    """The policy as {'mode', 'allow'}; raises PolicyError naming the file and the reason."""
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except (ValueError, RecursionError) as e:
        raise PolicyError(f'{path}: not valid JSON ({e})') from None
    policy = machine_file.fields(data, {'mode': (str, machine_file.REQUIRED, None),
                                        'allow': (list, machine_file.REQUIRED, None)}, path)
    if policy['mode'] not in MODES:
        raise PolicyError(f'{path}.mode: must be one of {", ".join(MODES)} (it is {policy["mode"]!r})')
    rows, names = [], {}
    for i, raw in enumerate(policy['allow']):
        where = f'{path}.allow[{i}]'
        row = machine_file.fields(raw, ROW, where)
        if row['defaultEffort'] is not None and row['defaultEffort'] not in (row['efforts'] or ()):
            raise PolicyError(f'{where}.defaultEffort: {row["defaultEffort"]!r} is not one of its efforts')
        for name in [row['model']] + (row['aliases'] or []):
            # a name (model or alias) means one row of a driver, never two
            if (row['driver'], name) in names:
                raise PolicyError(f'{where}: {row["driver"]} {name!r} is also in allow[{names[row["driver"], name]}]')
            names[row['driver'], name] = i
        rows.append(row)
    return {'mode': policy['mode'], 'allow': rows}


def load_policy(machine_path=None, owner_uid=0):
    """(the machine section, the policy). Both files root-owned (owner_uid in tests); raises PolicyError."""
    machine_path = machine_path or machine_file.default_path()
    section = machine_file.read_section(KIT, machine_path, owner_uid)
    absolute = lambda v: None if os.path.isabs(v) else 'must be an absolute path'  # noqa: E731
    cfg = machine_file.fields(section, {
        'policyFile': (str, machine_file.REQUIRED, absolute),
        'claudeSettings': (list, [], lambda v: next((f'{x!r} must be an absolute path' for x in v
                                                    if not isinstance(x, str) or not os.path.isabs(x)), None)),
    }, f'{machine_path}: {KIT}')
    text = machine_file.read_protected_text(cfg['policyFile'], owner_uid)
    return cfg, parse_policy(text, cfg['policyFile'])


def describe(row):
    efforts = '|'.join(row['efforts']) if row['efforts'] else 'no effort control'
    extra = [f'default {row["defaultEffort"]}'] if row['defaultEffort'] else []
    if row['aliases']:
        extra.append('Agent tool: ' + ' or '.join(repr(a) for a in row['aliases']))
    text = f'{row["driver"]} {row["model"]} ({efforts}' + ''.join(f'; {e}' for e in extra) + ')'
    return text + (f': use for {row["useFor"]}' if row['useFor'] else '')


# ---------- one call


def _efforts_in(options, where, problems):
    """The effort values an options value names ({id: value} or [{id, value}])."""
    if options is None:
        return []
    if isinstance(options, dict):
        pairs = list(options.items())
    elif isinstance(options, list) and all(isinstance(o, dict) for o in options):
        pairs = [(o.get('id'), o.get('value')) for o in options]
    else:
        problems.append(f'{where}.options is neither an object nor a list of {{id, value}}')
        return []
    return [value for key, value in pairs if key in EFFORT_OPTION_IDS]


def check_pick(where, model, efforts, driver, rows, agent, problems, matched):
    """One model pick against the rows; adds problems, or the rows it matched."""
    if not isinstance(model, str) or not model.strip() or model == 'inherit':
        problems.append(f'{where} names no model (every call must name one; the guard cannot see the parent\'s)')
        return
    model = model.strip()  # T3 trims it too
    if driver is not None and not isinstance(driver, str):
        problems.append(f'{where} has a driver that is not a text')
        return
    found = [r for r in rows if (driver is None or r['driver'] == driver)
             and (r['model'] == model or (agent and model in (r['aliases'] or ())))]
    if not found:
        problems.append(f'{where}: model {model!r}' + (f' of driver {driver}' if driver else '') + ' is not allowed')
        return
    for row in found:
        if row['efforts'] is None:
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


def check_call(kind, args, rows):
    """(problems, matched rows) for one checked tool call."""
    problems, matched = [], []
    args = args if isinstance(args, dict) else {}
    if kind == 'agent':
        effort = args.get('effort')
        check_pick('the Agent call', args.get('model'), [] if effort is None else [effort], CLAUDE_DRIVER, rows,
                   True, problems, matched)
        return problems, matched

    def target(where, value):
        if not isinstance(value, dict):
            problems.append(f'{where} is missing: name its model and options.effort (nothing is inherited)')
            return
        efforts = _efforts_in(value.get('options'), where, problems)
        check_pick(where, value.get('model'), efforts, value.get('driverKind'), rows, False, problems, matched)

    if kind == 'delegate_task':
        target('target', args.get('target'))
    elif kind == 'create_threads':
        threads = args.get('threads')
        if not isinstance(threads, list) or not threads:
            problems.append('threads is missing or empty')
        for i, thread in enumerate(threads if isinstance(threads, list) else []):
            target(f'threads[{i}].target', thread.get('target') if isinstance(thread, dict) else None)
    elif kind == 't3_thread_launch':
        target('modelSelection', args.get('modelSelection'))
    elif kind == 't3_thread_configure' and 'modelSelection' in args:
        target('modelSelection', args.get('modelSelection'))  # without it the call only reads the thread
    return problems, matched


def _out(decision=None, reason=None, context=None):
    out = {'hookEventName': 'PreToolUse'}
    if decision:
        out['permissionDecision'], out['permissionDecisionReason'] = decision, reason
    if context:
        out['additionalContext'] = context
    return {'hookSpecificOutput': out}


def decide(payload, load):
    """The hook's answer (a dict to print) or None (no output: the call goes on as without the hook).
    `load()` returns (machine section, policy) or raises PolicyError. Never returns 'allow': a passing call still
    goes through the session's own permission rules."""
    tool = payload.get('tool_name') if isinstance(payload, dict) else None
    kind = tool_kind(tool)
    if kind is None:
        return None
    try:
        cfg, policy = load()
    except PolicyError as e:
        return _out('deny', f'model-guard refused {tool}: the model policy cannot be used ({e}). The guard fails '
                            f'closed. Do not work around it: tell the person who runs this machine.')
    if policy['mode'] == 'off':
        return None
    problems, matched = check_call(kind, payload.get('tool_input'), policy['allow'])
    notes = '; '.join(f'{r["model"]} is for {r["useFor"]}' for r in matched if r['useFor'])
    if not problems:
        return _out(context=f'model-guard: {notes}.') if notes else None
    rows = '; '.join(describe(r) for r in policy['allow']) or 'none (every pick is refused)'
    why = (f'{"; ".join(problems)}. Name an allowed model and its effort on every such call. '
           f'Allowed rows: {rows}. The policy ({cfg["policyFile"]}) is the machine owner\'s; do not edit it.')
    if policy['mode'] == 'enforce':
        return _out('deny', f'model-guard refused {tool}: {why}')
    return _out(context=f'model-guard (advise mode, nothing refused): enforce would refuse this {tool} call: {why}')


def run_hook(stdin, stdout, machine_path=None, owner_uid=0):
    try:
        payload = json.loads(stdin.read())
    except ValueError as e:
        payload = None
        reason = f'model-guard refused: the hook input is not JSON ({e})'
    else:
        reason = 'model-guard refused: the hook input is not a JSON object'
    if not isinstance(payload, dict):
        out = _out('deny', reason)
    else:
        out = decide(payload, lambda: load_policy(machine_path, owner_uid))
    if out:
        stdout.write(json.dumps(out) + '\n')
    return 0


# ---------- drift: is the hook installed where the machine file says?


def _covers(matcher, name):
    """Claude Code's matcher rule: empty or '*' is every tool, plain words joined by | are exact names, anything
    else is a regular expression searched in the name."""
    if matcher in (None, '', '*'):
        return True
    if re.fullmatch(r'[\w|]+', matcher):
        return name in matcher.split('|')
    try:
        return re.search(matcher, name) is not None
    except re.error:
        return False


def registration_problems(path):
    """What is missing for the hook to run in one Claude Code settings file."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return [f'{path}: cannot be read ({e})']
    if not isinstance(data, dict):
        return [f'{path}: not a JSON object']
    if data.get('disableAllHooks') is True:
        return [f'{path}: disableAllHooks is on, so the hook never runs']
    hooks = data.get('hooks') if isinstance(data.get('hooks'), dict) else {}
    entries = hooks.get('PreToolUse') if isinstance(hooks.get('PreToolUse'), list) else []
    ours = [(e.get('matcher'), h) for e in entries if isinstance(e, dict) for h in (e.get('hooks') or [])
            if isinstance(h, dict) and h.get('type') == 'command' and HOOK_LINK in str(h.get('command', ''))]
    if not ours:
        return [f'{path}: no PreToolUse hook runs {HOOK_LINK} (the hook is not installed for this account)']
    problems = []
    names = list(AGENT_TOOLS) + [f'mcp__t3-code__{t}' for t in T3_TOOLS]
    missed = [n for n in names if not any(_covers(m, n) for m, _ in ours)]
    if missed:
        problems.append(f'{path}: the hook\'s matcher misses {", ".join(missed)} (use {HOOK_MATCHER!r})')
    if not any(str(h.get('command', '')).strip() == HOOK_COMMAND for _, h in ours):
        problems.append(f'{path}: the hook command is not {HOOK_COMMAND!r} (without "|| exit 2" a missing hook '
                        f'lets every call through)')
    return problems


def self_check(machine_path=None, owner_uid=0, prefix=''):
    """PROBLEM lines on stdout and exit 1 when the kit's files changed, the policy cannot be used, or a settings
    file the machine file lists does not run the hook."""
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
    try:
        cfg, policy = load_policy(machine_path, owner_uid)
    except PolicyError as e:
        problems.append(f'the policy cannot be used, so every checked call is refused: {e}')
    else:
        print(f'policy {cfg["policyFile"]}: mode {policy["mode"]}, {len(policy["allow"])} rows')
        if not cfg['claudeSettings']:
            problems.append(f'the machine file lists no claudeSettings in "{KIT}", so nothing shows where the hook '
                            f'runs')
        for path in cfg['claudeSettings']:
            problems += registration_problems(path)
    for p in problems:
        print(f'PROBLEM: {p}')
    print('self-check: ' + ('OK' if not problems else f'{len(problems)} problems'))
    return 0 if not problems else 1
