"""agent_guards.wiring: the settings entries of the kit's hooks.

Each hook is one command in Claude Code's settings, at the installed path (/usr/local/bin/<hook>). The command
first checks that the hook file is there, so a missing hook fails the way its kind must, and says so on stderr:

  actions-disable-guard  fails CLOSED: exit 2 (every Bash command is refused until the hook is back), also when
                         the hook process itself fails (`|| exit 2`). It answers itself within 5 s of its start.
  capacity-pipe-guard,   fail OPEN: exit 1, which Claude Code shows as a hook error and then runs the call. These
  memory-index-guard,    guard mistakes, not attacks; refusing every call because a helper is gone would stop all
  bg-guard, agent-slots  work on the machine.

The same entries go in the managed settings drop-in the installer writes (--settings) or, by hand, in any settings
file (`agent-guards hooks`). Timeouts and matchers are the ones these hooks always had.
"""
import json

BIN = '/usr/local/bin'
DROP_IN_NAME = 'agent-guards.json'
MANAGED_DIR = '/etc/claude-code'                  # Claude Code's managed settings folder on Linux
DROP_IN = f'{MANAGED_DIR}/managed-settings.d/{DROP_IN_NAME}'

HOOKS = ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard', 'bg-guard', 'agent-slots')
CLOSED = {'actions-disable-guard'}               # a missing or failed hook refuses
WHAT_IS_LOST = {
    'actions-disable-guard': 'every Bash command is refused until it is back',
    'capacity-pipe-guard': 'hook skipped, commands are NOT checked',
    'memory-index-guard': 'hook skipped, the memory index is NOT checked',
    'bg-guard': 'hook skipped, background commands are NOT checked',
    'agent-slots': 'hook skipped, agent slots are NOT counted',
}
# (event, matcher or None, [(hook, timeout)]): one settings entry each, in this order
ENTRIES = (
    ('PreToolUse', 'Write|Edit|MultiEdit', [('memory-index-guard', 5)]),
    ('PreToolUse', 'Bash', [('actions-disable-guard', 10)]),
    ('PreToolUse', 'Bash|Monitor', [('capacity-pipe-guard', 10)]),
    ('PostToolUse', 'Write|Edit|MultiEdit|Bash', [('memory-index-guard', 5)]),
    ('UserPromptSubmit', None, [('agent-slots', 5)]),
    ('Stop', None, [('agent-slots', 5), ('bg-guard', 5)]),
)


def command(hook, bin_dir=BIN, python='/usr/bin/python3'):
    """The shell command of one hook's settings entry."""
    path = f'{bin_dir}/{hook}'
    if hook in CLOSED:
        return (f'g={path}; [ -r "$g" ] || {{ echo "{hook}: $g is missing: {WHAT_IS_LOST[hook]}" >&2; exit 2; }}; '
                f'{python} -IB "$g" || {{ echo "{hook}: the guard failed (exit $?): the command is refused" >&2; '
                'exit 2; }')
    return (f'g={path}; [ -r "$g" ] || {{ echo "{hook}: $g is missing: {WHAT_IS_LOST[hook]}" >&2; exit 1; }}; '
            f'exec {python} -IB "$g"')


def hooks(bin_dir=BIN, python='/usr/bin/python3'):
    """The "hooks" object of a settings file with every hook of the kit."""
    out = {}
    for event, matcher, items in ENTRIES:
        entry = {'matcher': matcher} if matcher else {}
        entry['hooks'] = [{'type': 'command', 'command': command(h, bin_dir, python), 'timeout': t} for h, t in items]
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


def find(settings, hook, event):
    """The command of `hook` for `event` in a settings object (the first one naming it), or None. A name is found
    as a whole word of a path (`/x/bg-guard`, `bg-guard;`), not inside another name."""
    import re
    pat = re.compile(r'(^|[/\s="\'])' + re.escape(hook) + r'($|[\s;"\'])')
    for ev, _matcher, cmd in commands_in(settings):
        if ev == event and pat.search(cmd):
            return cmd
    return None


def ours(settings):
    """True for a settings object this kit wrote: only "hooks", and every command one of the kit's hooks."""
    if not isinstance(settings, dict) or set(settings) != {'hooks'}:
        return False
    cmds = commands_in(settings)
    return bool(cmds) and all(any(f'/{h}' in c for h in HOOKS) for _e, _m, c in cmds)
