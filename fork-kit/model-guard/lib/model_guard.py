"""model_guard: a Claude Code PreToolUse hook that keeps agents' model picks inside a policy (fork-kit/model-guard).

It checks every call that can start an agent or change an agent's model: Claude Code's Agent tool (Task in older
versions) and Workflow tool, and T3's MCP tools in T3_TOOLS. CHECKED_TOOLS below is the one list: the hook's
matcher, tool_kind() and the self-check are all made from it. Every other tool passes untouched.

The policy is a JSON file {"mode": "off" | "advise" | "enforce", "allow": [rows]}; the machine file
(fork-kit/common) names it in its "model-guard" section. Rules (README.md next to this folder):

- every checked call names its model and its effort, and both are in one row; else enforce refuses and lists
  the rows (the hook cannot see the parent's model or the provider's catalog, so nothing may be inherited);
- a call that would run a model the hook cannot see (a thread fork, a Workflow script it cannot read, a model
  field on a tool that has none) is refused in enforce;
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

# Every tool the hook checks, and how. The one list: HOOK_MATCHER, tool_kind() and the self-check come from it.
# Claude Code's own tools by name; T3's under any MCP server name (mcp__<server>__<tool>).
CLAUDE_TOOLS = {'Agent': 'agent', 'Task': 'agent', 'Workflow': 'workflow'}
T3_TOOLS = ('delegate_task', 'create_threads', 't3_thread_launch', 't3_thread_configure', 't3_thread_transfer',
            't3_thread_send', 't3_thread_send_attachments', 'schedule_task')
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
        'allowOtherHooks': (bool, False, None),
    }, f'{machine_path}: {KIT}')
    text = machine_file.read_protected_text(cfg['policyFile'], owner_uid)
    return cfg, parse_policy(text, cfg['policyFile'])


def describe(row):
    efforts = '|'.join(row['efforts']) if row['efforts'] else 'no effort may be named'
    extra = [f'default {row["defaultEffort"]}'] if row['defaultEffort'] else []
    if row['aliases']:
        extra.append('Agent tool: ' + ' or '.join(repr(a) for a in row['aliases']))
    text = f'{row["driver"]} {row["model"]} ({efforts}' + ''.join(f'; {e}' for e in extra) + ')'
    return text + (f': use for {row["useFor"]}' if row['useFor'] else '')


# ---------- one call


def _efforts_in(options, where, problems):
    """The efforts an options value ({id: value} or [{id, value}]) runs, as T3 runs them: ultracode on means the
    effort "ultracode" (it runs at xhigh, so that xhigh is not named again)."""
    if options is None:
        return []
    if isinstance(options, dict):
        pairs = list(options.items())
    elif isinstance(options, list) and all(isinstance(o, dict) for o in options):
        pairs = [(o.get('id'), o.get('value')) for o in options]
    else:
        problems.append(f'{where}.options is neither an object nor a list of {{id, value}}')
        return []
    efforts = [value for key, value in pairs if key in EFFORT_OPTION_IDS]
    ultracode = [value for key, value in pairs if key == ULTRACODE_OPTION_ID]
    if ULTRACODE_EFFORT in efforts:       # the old effort value: xhigh, with the option on unless it is given
        efforts = [ULTRACODE_RUNS_AT if e == ULTRACODE_EFFORT else e for e in efforts]
        ultracode = ultracode or [True]
    if any(v not in (False, None) for v in ultracode):
        efforts = [e for e in efforts if e != ULTRACODE_RUNS_AT] + [ULTRACODE_EFFORT]
    return efforts


def check_pick(where, model, efforts, driver, rows, agent, problems, matched):
    """One model pick against the rows; adds problems, or the rows it matched. Without a driver every row naming
    the model must accept the pick. A row without "efforts" accepts only a pick that names no effort."""
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
# after these words a [ starts an array (not a member access)
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
        calls, opens = 0, []
        for k, (kind, text, line, _) in enumerate(toks):
            prev = toks[k - 1] if k else None
            after_dot = prev is not None and prev[0] == 'punct' and prev[1] in ('.', '?.')
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
                if prev is not None and (prev[0] in ('num', 'str')
                                         or prev[0] == 'name' and prev[1] not in WF_EXPRESSION_WORDS
                                         or prev[0] == 'punct' and prev[1] in (')', ']', '}', '}`', '?.')):
                    if toks[k + 1][0] != 'num' or toks[k + 2][:2] != ['punct', ']']:
                        raise ScriptError(f'line {line}: a computed member access other than [number]')
                elif prev is not None and prev[0] == 'punct' and (prev[1] == '{' or prev[1] == ','
                                                                  and opens and opens[-1] == '{'):
                    raise ScriptError(f'line {line}: a computed key')
            if text in _OPEN:
                opens.append(text)
            elif text in ('}', ')', ']', '}`', '}${'):
                if not opens or _OPEN[opens[-1]] != text[0]:
                    raise ScriptError(f'line {line}: brackets do not match')
                opens.pop()
                if text == '}${':
                    opens.append('`${')
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

    def target(where, value):
        if not isinstance(value, dict):
            problems.append(f'{where} is missing: name its model and options.effort (nothing is inherited)')
            return
        efforts = _efforts_in(value.get('options'), where, problems)
        check_pick(where, value.get('model'), efforts, value.get('driverKind'), rows, False, problems, matched)

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


# ---------- drift: does Claude Code's managed settings run the hook?


def _covers(matcher, name):
    """Claude Code's matcher rule: empty or '*' is every tool; only letters, digits, _, -, spaces, | and , is a
    list of exact names; anything else is a JavaScript regular expression searched in the name."""
    if matcher in (None, '', '*'):
        return True
    if not isinstance(matcher, str):
        return False
    if re.fullmatch(r'[\w\- |,]+', matcher, re.A):
        return name in [w.strip() for w in re.split(r'[|,]', matcher)]
    try:
        return re.search(matcher, name) is not None
    except re.error:
        return False


def entry_problems(entry):
    """Why one PreToolUse entry is not the guard's (None when it is): its matcher covers every checked tool and its
    only hook runs exactly HOOK_COMMAND, synchronously, with Claude Code's default timeout, and blocks on failure."""
    if not isinstance(entry, dict) or set(entry) != {'matcher', 'hooks'}:
        return 'it is not exactly {"matcher", "hooks"}'
    missed = [n for n in CHECKED_TOOLS if not _covers(entry['matcher'], n)]
    if missed:
        return f'its matcher misses {", ".join(missed)}'
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
    allow_other_hooks = False
    try:
        cfg, policy = load_policy(machine_path, owner_uid)
    except PolicyError as e:
        problems.append(f'the policy cannot be used, so every checked call is refused: {e}')
    else:
        allow_other_hooks = cfg['allowOtherHooks']
        print(f'policy {cfg["policyFile"]}: mode {policy["mode"]}, {len(policy["allow"])} rows')
    try:
        files = managed_files(prefix + managed_dir(), owner_uid)
    except PolicyError as e:
        problems.append(str(e))
    else:
        problems += [f'{path}: {error}' for path, _, error in files if error]
        problems += settings_problems([(path, data) for path, data, error in files if not error], allow_other_hooks)
    for p in problems:
        print(f'PROBLEM: {p}')
    print('self-check: ' + ('OK' if not problems else f'{len(problems)} problems'))
    return 0 if not problems else 1
