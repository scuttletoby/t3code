"""agent_guards.wiring: the settings entries of the kit's hooks.

Each hook is one command in Claude Code's settings, at the installed path (/usr/local/bin/<hook>). The command
first checks that the hook file is there, so a missing hook fails the way its kind must, and says so on stderr:

  actions-disable-guard  fails CLOSED: exit 2 (every Bash and Monitor command is refused until the hook is back),
                         also when the hook process itself fails (`|| exit 2`) and when the file is empty (an
                         empty Python file would answer nothing, which lets the call run). It answers itself within 5 s of
                         its start; should Claude Code still cut it off at its timeout, the entry's
                         "onFailure": "block" refuses the call (without it a timed-out hook lets the call run).
  leak-guard             fails CLOSED the same way: a personal detail posted to a public repo cannot be taken
                         back (an edit keeps its history).
  capacity-pipe-guard,   fail OPEN: exit 1, which Claude Code shows as a hook error and then runs the call. These
  memory-index-guard,    guard mistakes, not attacks; refusing every call because a helper is gone would stop all
  bg-guard, agent-slots, work on the machine.
  facts-hook, poll-loop-guard,
  read-size-guard,
  batch-nudge

actions-disable-guard, capacity-pipe-guard and memory-index-guard run on every Bash call, so their entries start
the hook through agent-guards-fast (FAST) when that file is there: it passes what the hook surely passes and runs the
hook file on the same event for everything else, from bytecode the installer wrote (agent_guards.fast). Without it
the entry runs the hook file as before. Python starts with -S (no site: the hooks use the standard library only).

The same entries go in the managed settings drop-in the installer writes (--settings, with the machine's
"harnessEnv" as its "env": config.harness_env) or, by hand, in any settings
file (`agent-guards hooks`). Monitor runs shell text like Bash, so every hook that reads a command reads it for
Monitor too, except poll-loop-guard: an until-condition in a Monitor is the right way to wait for a file, so it
is wired for Bash only.
"""
import json

BIN = '/usr/local/bin'
DROP_IN_NAME = 'agent-guards.json'
MANAGED_DIR = '/etc/claude-code'                  # Claude Code's managed settings folder on Linux
DROP_IN = f'{MANAGED_DIR}/managed-settings.d/{DROP_IN_NAME}'

HOOKS = ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard', 'bg-guard', 'agent-slots',
         'facts-hook', 'poll-loop-guard', 'read-size-guard', 'batch-nudge',
         'leak-guard')
CLOSED = {'actions-disable-guard', 'leak-guard'}   # a missing or failed hook refuses
FAST = 'agent-guards-fast'                       # the fast path (bin/agent-guards-fast), linked beside the hooks
FAST_HOOKS = ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard')   # run on every Bash call
WHAT_IS_LOST = {
    'actions-disable-guard': 'every Bash and Monitor command is refused until it is back',
    'capacity-pipe-guard': 'hook skipped, commands are NOT checked',
    'memory-index-guard': 'hook skipped, the memory index is NOT checked',
    'bg-guard': 'hook skipped, background commands are NOT checked',
    'agent-slots': 'hook skipped, agent slots are NOT counted',
    'facts-hook': 'hook skipped, the state tables are NOT shown',
    'poll-loop-guard': 'hook skipped, wait loops and self-kills are NOT checked',
    'read-size-guard': 'hook skipped, Read sizes are NOT checked',
    'batch-nudge': 'hook skipped, no batching hint',
    'leak-guard': 'every Bash and Monitor command is refused until it is back',
}
# (event, matcher or None, [(hook, timeout)]): one settings entry each, in this order
ENTRIES = (
    ('PreToolUse', 'Write|Edit|MultiEdit', [('memory-index-guard', 5)]),
    ('PreToolUse', 'Bash|Monitor', [('actions-disable-guard', 10)]),
    ('PreToolUse', 'Bash|Monitor', [('capacity-pipe-guard', 10)]),
    ('PreToolUse', 'Bash', [('poll-loop-guard', 5)]),
    ('PreToolUse', 'Read', [('read-size-guard', 5)]),
    ('PreToolUse', 'Bash|Monitor', [('leak-guard', 10)]),
    ('PostToolUse', 'Write|Edit|MultiEdit|Bash|Monitor', [('memory-index-guard', 5)]),
    ('PostToolUse', 'Read|Grep|Glob|LS|NotebookRead|Bash', [('batch-nudge', 5)]),
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


def drop_in_text(bin_dir=BIN, env=None):
    """The drop-in: the hooks, and the "env" (config.harness_env) when it has a variable."""
    return json.dumps(dict({'hooks': hooks(bin_dir)}, **({'env': dict(env)} if env else {})), indent=2) + '\n'


def other_env(drop_in):
    """[(path, [names])]: the env variables managed-settings.json and the other managed-settings.d/*.json files set
    (the folders of `drop_in`, a real path). Claude Code merges those with the drop-in key by key, the later file
    winning, so the kit leaves such a variable to them. A file that cannot be read or parsed is skipped."""
    import glob, os
    folder = os.path.dirname(drop_in)
    out = []
    for f in [os.path.join(os.path.dirname(folder), 'managed-settings.json')] + \
            sorted(glob.glob(os.path.join(folder, '*.json'))):
        if os.path.abspath(f) == os.path.abspath(drop_in):
            continue
        try:
            with open(f, encoding='utf-8') as fh:
                env = json.load(fh).get('env')
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(env, dict) and env:
            out.append((f, sorted(env)))
    return out


def env_for(env, drop_in):
    """(the drop-in's env: `env` less what other managed files set, [(path, [names left to it])])."""
    env, left = dict(env), []
    for f, names in other_env(drop_in):
        taken = [n for n in names if n in env]
        if taken:
            left.append((f, taken))
            for n in taken:
                del env[n]
    return env, left


def env_ok(env):
    """An "env" this kit may write: an object of variable names to text."""
    import re
    return isinstance(env, dict) and all(isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', k)
                                         and isinstance(v, str) for k, v in env.items())


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
    """True for a settings object this kit wrote: "hooks", every command one of the kit's hooks, and maybe an "env"
    of text values (a drop-in of an earlier release has no "env")."""
    if not isinstance(settings, dict) or 'hooks' not in settings or set(settings) - {'hooks', 'env'} \
            or ('env' in settings and not env_ok(settings['env'])):
        return False
    import re
    cmds = commands_in(settings)
    kit = re.compile(r'^g=' + re.escape(BIN) + r'/(' + '|'.join(re.escape(h) for h in HOOKS) + r'); ')
    return bool(cmds) and all(kit.match(c) for _e, _m, c in cmds)
