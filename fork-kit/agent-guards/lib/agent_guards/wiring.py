"""agent_guards.wiring: the settings entries of the kit's hooks.

Each hook is one command in Claude Code's settings, at the installed path (/usr/local/bin/<hook>). The command
first checks that the hook file is there, so a missing hook fails the way its kind must, and says so on stderr:

  actions-disable-guard  fails CLOSED: exit 2 (every Bash and Monitor command is refused until the hook is back),
                         also when the hook process itself fails (`|| exit 2`) and when the file is empty (an
                         empty Python file would answer nothing, which lets the call run). It answers itself within 5 s of
                         its start; should Claude Code still cut it off at its timeout, the entry's
                         "onFailure": "block" refuses the call (without it a timed-out hook lets the call run).
  capacity-pipe-guard,   fail OPEN: exit 1, which Claude Code shows as a hook error and then runs the call. These
  memory-index-guard,    guard mistakes, not attacks; refusing every call because a helper is gone would stop all
  bg-guard, agent-slots, work on the machine.
  facts-hook

actions-disable-guard, capacity-pipe-guard and memory-index-guard run on every Bash call, so their entries start
the hook through agent-guards-fast (FAST) when that file is there: it passes what the hook surely passes and runs the
hook file on the same event for everything else, from bytecode the installer wrote (agent_guards.fast). Without it
the entry runs the hook file as before. Python starts with -S (no site: the hooks use the standard library only).

The same entries go in the managed settings drop-in the installer writes (--settings) or, by hand, in any settings
file (`agent-guards hooks`). Monitor runs shell text like Bash, so every hook that reads a command reads it for
Monitor too.
"""
import json

BIN = '/usr/local/bin'
DROP_IN_NAME = 'agent-guards.json'
MANAGED_DIR = '/etc/claude-code'                  # Claude Code's managed settings folder on Linux
DROP_IN = f'{MANAGED_DIR}/managed-settings.d/{DROP_IN_NAME}'

HOOKS = ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard', 'bg-guard', 'agent-slots',
         'facts-hook')
CLOSED = {'actions-disable-guard'}               # a missing or failed hook refuses
FAST = 'agent-guards-fast'                       # the fast path (bin/agent-guards-fast), linked beside the hooks
FAST_HOOKS = ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard')   # run on every Bash call
WHAT_IS_LOST = {
    'actions-disable-guard': 'every Bash and Monitor command is refused until it is back',
    'capacity-pipe-guard': 'hook skipped, commands are NOT checked',
    'memory-index-guard': 'hook skipped, the memory index is NOT checked',
    'bg-guard': 'hook skipped, background commands are NOT checked',
    'agent-slots': 'hook skipped, agent slots are NOT counted',
    'facts-hook': 'hook skipped, the state tables are NOT shown',
}
# (event, matcher or None, [(hook, timeout)]): one settings entry each, in this order
ENTRIES = (
    ('PreToolUse', 'Write|Edit|MultiEdit', [('memory-index-guard', 5)]),
    ('PreToolUse', 'Bash|Monitor', [('actions-disable-guard', 10)]),
    ('PreToolUse', 'Bash|Monitor', [('capacity-pipe-guard', 10)]),
    ('PostToolUse', 'Write|Edit|MultiEdit|Bash|Monitor', [('memory-index-guard', 5)]),
    ('UserPromptSubmit', None, [('agent-slots', 5)]),
    ('Stop', None, [('agent-slots', 5), ('bg-guard', 5)]),
    ('SessionStart', None, [('facts-hook', 5)]),
    ('PreCompact', None, [('facts-hook', 5)]),
)


def command(hook, bin_dir=BIN, python='/usr/bin/python3'):
    """The shell command of one hook's settings entry."""
    path = f'{bin_dir}/{hook}'
    # f: the fast path when it is there and not empty (an older install, a missing or empty file: the hook as before)
    fast, run = ((f'f={bin_dir}/{FAST}; [ -r "$f" ] && [ -s "$f" ] || f=; ', f'{python} -IBS ${{f:+"$f"}} "$g"')
                 if hook in FAST_HOOKS else ('', f'{python} -IB "$g"'))
    if hook in CLOSED:
        return (f'g={path}; [ -r "$g" ] && [ -s "$g" ] || {{ echo "{hook}: $g is missing or empty: {WHAT_IS_LOST[hook]}" >&2; exit 2; }}; '
                f'{fast}{run} || {{ echo "{hook}: the guard failed (exit $?): the command is refused" >&2; '
                'exit 2; }')
    return (f'g={path}; [ -r "$g" ] || {{ echo "{hook}: $g is missing: {WHAT_IS_LOST[hook]}" >&2; exit 1; }}; '
            f'{fast}exec {run}')


def hooks(bin_dir=BIN, python='/usr/bin/python3'):
    """The "hooks" object of a settings file with every hook of the kit."""
    out = {}
    for event, matcher, items in ENTRIES:
        entry = {'matcher': matcher} if matcher else {}
        entry['hooks'] = [dict({'type': 'command', 'command': command(h, bin_dir, python), 'timeout': t},
                               **({'onFailure': 'block'} if h in CLOSED else {})) for h, t in items]
        out.setdefault(event, []).append(entry)
    return out


def drop_in_text(bin_dir=BIN):
    return json.dumps({'hooks': hooks(bin_dir)}, indent=2) + '\n'


def commands_in(settings):
    """[(event, matcher, command)] of every command hook in a settings object."""
    out = []
    for event, entries in ((settings or {}).get('hooks') or {}).items():
        for e in entries if isinstance(entries, list) else []:
            for h in (e.get('hooks') or []) if isinstance(e, dict) else []:
                if isinstance(h, dict) and isinstance(h.get('command'), str):
                    out.append((event, e.get('matcher'), h['command']))
    return out


def matches(matcher, tool):
    """Whether a settings entry's matcher takes this tool, as Claude Code reads it: none, "" or "*" take every tool;
    letters, digits, _ and | only are an exact list; anything else is a regular expression."""
    import re
    if tool is None or matcher in (None, '', '*'):
        return True
    if re.fullmatch(r'[A-Za-z0-9_|]+', matcher):
        return tool in matcher.split('|')
    try:
        return re.search(matcher, tool) is not None
    except re.error:
        return False


def find(settings, hook, event, tool=None):
    """The command of `hook` for `event` in a settings object (the first one naming it, in an entry whose matcher
    takes `tool` when one is given), or None. A name is found as a whole word of a path (`/x/bg-guard`,
    `bg-guard;`), not inside another name."""
    import re
    pat = re.compile(r'(^|[/\s="\'])' + re.escape(hook) + r'($|[\s;"\'])')
    for ev, matcher, cmd in commands_in(settings):
        if ev == event and pat.search(cmd) and matches(matcher, tool):
            return cmd
    return None


def ours(settings):
    """True for a settings object this kit wrote: only "hooks", and every command one of the kit's hooks."""
    if not isinstance(settings, dict) or set(settings) != {'hooks'}:
        return False
    import re
    cmds = commands_in(settings)
    kit = re.compile(r'^g=' + re.escape(BIN) + r'/(' + '|'.join(re.escape(h) for h in HOOKS) + r'); ')
    return bool(cmds) and all(kit.match(c) for _e, _m, c in cmds)
