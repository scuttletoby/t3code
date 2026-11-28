"""agent_guards.fast: the fast path of the hooks that run on every Bash call (bin/agent-guards-fast).

Each Bash call runs actions-disable-guard, capacity-pipe-guard, leak-guard and memory-index-guard. Started as a
script, Python compiles a hook's whole file on every call (a script's bytecode is never cached), and
actions-disable-guard is 5,000 lines: ~30 ms of compiling and ~10 ms of regular expressions before it reads the command, on an idle machine,
and many times that under memory pressure, up to the hook's timeout, which refuses the call.

The settings entry of these hooks runs `python3 -IBS agent-guards-fast <the hook file>` (agent_guards.wiring). This
module reads the event, and:
  - answers "pass" itself (exit 0, nothing written) only where the hook would pass without a word, decided by a
    check of a few lines that mirrors the hook's own first step (FAST below);
  - else runs the hook file itself, as __main__, on the same event: from the bytecode the installer wrote beside
    the file the hook really is (versions/<sha>/bin/__pycache__, not beside the /usr/local/bin link), and only a
    checked-hash .pyc of exactly that file's text (hook_code); anything else is compiled from the source as before.
The fast check and its read of stdin have FAST_BUDGET seconds (an alarm, as the guard's own read of stdin has): past
that the hook runs on what was read and the rest of stdin, on its own clock, and answers (the guard refuses at its
5 s budget; a fast check never refuses).
The hook decides everything the fast check does not; its answer is the answer. Anything unexpected in the fast
check (a broken event, a machine file problem to report, a missing lib part) goes to the hook, never to a pass.
tests/fast.test.py runs every case of the hooks' tests both ways and expects the same answer.
"""
import io, json, os, re, signal, sys

TEXT_TOOLS = ('Bash', 'Monitor')   # the tools whose tool_input.command is shell text
# Seconds for reading stdin and the fast check (ms when all is well). Past it the hook decides on its own clock: the
# guard counts its 5 s budget from the process start (/proc), so it still answers within it, as without this file.
FAST_BUDGET = 3.0


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


# the programs leak-guard reads as a post or a push: a command without one of these names anywhere in its text (quotes
# and backslashes taken out first, so that g''h and g\\h count) holds no post
LEAK_NAMES = re.compile(r'(?<![A-Za-z0-9_])(gh|git|curl|wget|https?|xhs?)(?![A-Za-z0-9_])')


def leak(data, env):
    """leak-guard's own first step: a Bash or Monitor command that names no posting program, or another tool."""
    from agent_guards import config
    settings, problem = config.load('leakGuard')
    if problem:   # the guard says so in its answer
        return False
    ev = json.loads(_text(data))
    if not isinstance(ev, dict):
        return False
    if ev.get('tool_name') not in {'Bash', 'Monitor'}:
        return True
    cmd = (ev.get('tool_input') or {}).get('command')
    if not isinstance(cmd, str):
        return True
    plain = cmd.replace('\\\n', '').replace('\\', '').replace("'", '').replace('"', '')
    return not LEAK_NAMES.search(plain)


FAST = {'actions-disable-guard': actions_disable, 'capacity-pipe-guard': capacity_pipe,
        'memory-index-guard': memory_index, 'leak-guard': leak}


class Late(BaseException):
    """FAST_BUDGET is up: the hook decides (a BaseException, so no `except Exception` in a fast check takes it)."""


ARMED = []


def _late(*_):
    if ARMED:
        raise Late()


def alarm(seconds):
    """Arm (seconds > 0) or disarm the FAST_BUDGET alarm (one-shot)."""
    if not hasattr(signal, 'SIGALRM'):
        return
    if seconds > 0:
        ARMED[:] = [True]
        signal.signal(signal.SIGALRM, _late)
        signal.setitimer(signal.ITIMER_REAL, seconds)
    else:
        del ARMED[:]
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, signal.SIG_DFL)


def read_stdin(chunks):
    """Read stdin to its end into `chunks` (a list: what was read stays there when Late cuts the read)."""
    while True:
        b = os.read(0, 1 << 16)
        if not b:
            return b''.join(chunks)
        chunks.append(b)


class _Stdin(io.RawIOBase):
    """The hook's stdin: the bytes the fast path read, then whatever is still to come on fd 0 (nothing, unless
    FAST_BUDGET cut the read: the hook then reads the rest itself, under its own alarm)."""

    def __init__(self, head):
        super().__init__()
        self.head = head

    def readable(self):
        return True

    def readinto(self, b):
        if not self.head:
            data = os.read(0, len(b))
        else:
            data, self.head = self.head[:len(b)], self.head[len(b):]
        b[:len(data)] = data
        return len(data)


def passes(hook, data, env):
    """True only when the fast check of this hook is sure the hook would pass without a word."""
    check = FAST.get(os.path.basename(hook))
    if check is None:
        return False
    try:
        return check(data, env) is True
    except Exception:   # noqa: BLE001 - the hook decides
        return False


def hook_code(hook):
    """The hook's code object. From bytecode only when the .pyc beside the file the hook really is (the link
    /usr/local/bin/<hook> resolved: versions/<sha>/bin/__pycache__/, where the installer wrote it, in the manifest)
    is a checked-hash .pyc (PEP 552 flags 0b11) of exactly this source, made by this Python, owned by the source's
    owner and writable by nobody else. Python itself would also take an unchecked-hash .pyc, or a timestamp one whose
    mtime and size match, without looking at the source, and it looks next to the link: neither is used here.
    Anything else (no .pyc, another Python's cache tag, a changed source, an odd file) compiles the source as
    `python3 -IB <hook>` does, so the answer is the same, only slower."""
    real = os.path.realpath(hook)
    with open(real, 'rb') as f:
        source, src = f.read(), os.fstat(f.fileno())
    try:
        import _imp, marshal
        bx = sys.modules['_frozen_importlib_external']   # importlib's own file functions, already loaded
        key = getattr(_imp, 'pyc_magic_number_token', None)   # 3.14: the key of source_hash; before: the magic
        with open(bx.cache_from_source(real), 'rb') as f:
            st, head = os.fstat(f.fileno()), f.read(16)
            if (head[:4] == bx.MAGIC_NUMBER and head[4:8] == b'\x03\x00\x00\x00'
                    and head[8:] == _imp.source_hash(bx._RAW_MAGIC_NUMBER if key is None else key, source)
                    and st.st_uid == src.st_uid and not st.st_mode & 0o022):
                code = marshal.loads(f.read())
                if isinstance(code, type(hook_code.__code__)):
                    return code
    except Exception:   # noqa: BLE001 - no usable bytecode: the source
        pass
    return compile(source, hook, 'exec', dont_inherit=True)


def run_hook(hook, data, args=()):
    """Run the hook file as __main__ with `data` (then the rest of fd 0) on its stdin and `args` as its arguments,
    as `python3 -IB <hook>` would (its sys.exit ends the process here; __file__ is the path it was given)."""
    stdin = sys.stdin
    code = hook_code(hook)
    sys.stdin = io.TextIOWrapper(io.BufferedReader(_Stdin(data)), encoding=stdin.encoding, errors=stdin.errors,
                                 newline='\n')
    mod = type(sys)('__main__')
    mod.__file__, mod.__loader__, mod.__spec__ = hook, None, None
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
    hook, args, chunks = argv[0], argv[1:], []
    alarm(FAST_BUDGET)   # before stdin is read: a read or a check that hangs goes to the hook, never to a pass
    try:
        data = read_stdin(chunks)
        ok = not args and passes(hook, data, os.environ)
        alarm(0)
    except Late:   # also one that fires while the alarm is turned off: the timer is one-shot, so this is the last
        alarm(0)
        data, ok = b''.join(chunks), False
    return 0 if ok else run_hook(hook, data, args)
