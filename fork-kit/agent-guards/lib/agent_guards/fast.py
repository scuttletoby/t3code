"""agent_guards.fast: the fast path of the three hooks that run on every Bash call (bin/agent-guards-fast).

Each Bash call runs actions-disable-guard, capacity-pipe-guard and memory-index-guard. Started as a script, Python
compiles a hook's whole file on every call (a script's bytecode is never cached), and actions-disable-guard is
5,000 lines: ~30 ms of compiling and ~10 ms of regular expressions before it reads the command, on an idle machine,
and many times that under memory pressure, up to the hook's timeout, which refuses the call.

The settings entry of these hooks runs `python3 -IBS agent-guards-fast <the hook file>` (agent_guards.wiring). This
module reads the event, and:
  - answers "pass" itself (exit 0, nothing written) only where the hook would pass without a word, decided by a
    check of a few lines that mirrors the hook's own first step (FAST below);
  - else runs the hook file itself, as __main__, on the same event: from the bytecode the installer wrote beside
    it (__pycache__, checked against the file's hash on every load), or compiled as before when there is none.
The hook decides everything the fast check does not; its answer is the answer. Anything unexpected in the fast
check (a broken event, a machine file problem to report, a missing lib part) goes to the hook, never to a pass.
tests/fast.test.py runs every case of the hooks' tests both ways and expects the same answer.
"""
import json, os, sys

TEXT_TOOLS = ('Bash', 'Monitor')   # the tools whose tool_input.command is shell text


def _text(data):
    """The event as the hook reads it from its stdin: decoded the same way."""
    return data.decode(sys.stdin.encoding or 'utf-8', sys.stdin.errors or 'strict')


def actions_disable(data, env):
    """The guard's own fast exit (agent_guards.quick, a checked copy of the guard's), and nothing else: a tool
    that runs no shell text, or a command of plain words quick_pass() takes and no file on PATH shadows."""
    if env.get('ACTIONS_DISABLE_GUARD_TEST_CRASH'):
        return False
    from agent_guards import quick
    if quick.SETTINGS_PROBLEM:   # the guard says so in its answer
        return False
    ev = json.loads(_text(data))
    if not isinstance(ev, dict):
        return False
    if ev.get('tool_name') not in TEXT_TOOLS:
        return True
    cmd = (ev.get('tool_input') or {}).get('command') or ''
    cwd = ev.get('cwd') or os.getcwd()
    return quick.quick_pass(cmd) is True and not quick.quick_shadowed(cmd, cwd, env)


def capacity_pipe(data, env):
    """The guard's own first step: a command that does not hold a capacity check's name anywhere in its text."""
    from agent_guards import config
    settings, problem = config.load('capacityPipe')
    checks = tuple(settings['commands'])
    if problem or not checks or not all(isinstance(c, str) and c for c in checks):
        return False
    ev = json.loads(_text(data))
    if not isinstance(ev, dict):
        return False
    if ev.get('tool_name') not in {'Bash', 'Monitor'}:   # a set, as the guard's (an unhashable name raises there)
        return True
    cmd = (ev.get('tool_input') or {}).get('command')
    return not isinstance(cmd, str) or not any(c in cmd for c in checks)


def memory_index(data, env):
    """PreToolUse: no Write/Edit/MultiEdit of a file named MEMORY.md. PostToolUse: the session's index is not there,
    or it is unchanged since the state file of this session (the guard's own first look, without writing)."""
    from agent_guards import config
    settings, problem = config.load('memoryIndex')
    if problem:
        return False
    ev = json.loads(_text(data))
    if not isinstance(ev, dict):
        return False
    name = ev.get('hook_event_name')
    if name == 'PreToolUse':
        path = (ev.get('tool_input') or {}).get('file_path') or ''
        return ev.get('tool_name') not in ('Write', 'Edit', 'MultiEdit') or os.path.basename(path) != 'MEMORY.md'
    if name != 'PostToolUse':
        return True
    t = ev.get('transcript_path') or ''
    if not t:
        return True
    try:
        st = os.stat(os.path.join(os.path.dirname(t), 'memory', 'MEMORY.md'))
    except OSError:
        return True
    sid = ''.join(c for c in (ev.get('session_id') or 'none') if c.isalnum() or c == '-')[:64]
    try:
        with open(os.path.join(config.expand(settings['stateDir']), sid)) as f:
            last = f.read().strip()
    except OSError:
        return False
    return last == f'{st.st_mtime_ns} {st.st_size}'


FAST = {'actions-disable-guard': actions_disable, 'capacity-pipe-guard': capacity_pipe,
        'memory-index-guard': memory_index}


def read_stdin():
    chunks = []
    while True:
        b = os.read(0, 1 << 16)
        if not b:
            return b''.join(chunks)
        chunks.append(b)


def passes(hook, data, env):
    """True only when the fast check of this hook is sure the hook would pass without a word."""
    check = FAST.get(os.path.basename(hook))
    if check is None:
        return False
    try:
        return check(data, env) is True
    except Exception:   # noqa: BLE001 - the hook decides
        return False


def run_hook(hook, data, args=()):
    """Run the hook file as __main__ with `data` on its stdin and `args` as its arguments, as `python3 -IB <hook>`
    would (its sys.exit ends the process here). SourceFileLoader takes the bytecode beside the file when it is there
    and matches it."""
    import io, types
    from importlib.machinery import SourceFileLoader
    stdin = sys.stdin
    sys.stdin = io.TextIOWrapper(io.BytesIO(data), encoding=stdin.encoding, errors=stdin.errors, newline='\n')
    loader = SourceFileLoader('__main__', hook)
    code = loader.get_code('__main__')
    mod = types.ModuleType('__main__')
    mod.__file__, mod.__loader__, mod.__spec__ = hook, loader, None
    sys.modules['__main__'] = mod
    sys.argv = [hook, *args]
    exec(code, mod.__dict__)
    return 0


def main(argv):
    """argv: the hook file, then its own arguments (none from a settings entry: with any, the hook answers)."""
    if not argv:
        sys.stderr.write('usage: agent-guards-fast HOOK_FILE (hook event JSON on stdin; the settings entry: '
                         '`agent-guards hooks`)\n')
        return 1
    hook, args = argv[0], argv[1:]
    data = read_stdin()
    if not args and passes(hook, data, os.environ):
        return 0
    return run_hook(hook, data, args)
