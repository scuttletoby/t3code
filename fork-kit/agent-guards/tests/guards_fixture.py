"""Shared by the agent-guards tests: a machine file of their own, and a hook run the way Claude Code runs it.

A hook reads the machine file at the fixed path (/etc/fork-kit/machine.json); a test never does. It points the
kit's config at a file in a temporary folder (or at one that is not there: the built-in defaults), owned by the
user who runs the test, through config.MACHINE_PATH and config.OWNER_UID, the two names that are there for tests.
In a separate process the same is done by RUNNER, which then runs the hook file as __main__. No root, no network.

A hook's answer never depends on who runs the test: run_hook starts it under a stand-in Claude Code (a /bin/sh named
`claude`) with nothing else running under it, or with no `claude` above it at all. Without that, bg-guard walked up
to the real Claude Code of an agent running the tests and blocked whenever that agent had another command running in
the background (config.test.py failed 4 of 15 that way while several suites ran at once).
"""
import importlib.machinery, importlib.util, json, os, signal, subprocess, sys

sys.dont_write_bytecode = True   # the CI runner has no -B: no __pycache__ beside the hooks

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BIN, LIB = os.path.join(KIT, 'bin'), os.path.join(KIT, 'lib')
sys.path.insert(0, LIB)
from agent_guards import config, wiring  # noqa: E402

NO_MACHINE = '/nonexistent-agent-guards-test/machine.json'   # not there: every hook uses its defaults
# The kit's own state table (facts.KIT_TABLE, COMMANDS.md): a test that is not about it points it at a file that is not
# there through this variable, which only RUNNER reads (the kit never does), so its tables are the test's alone.
KIT_TABLE_VAR = 'AGENT_GUARDS_TEST_KIT_TABLE'
NO_KIT_TABLE = '/nonexistent-agent-guards-test/COMMANDS.md'
RUNNER = '''import os, runpy, sys
lib, machine, uid, hook = sys.argv[1:5]
sys.path.insert(0, lib)
from agent_guards import config
if os.environ.get('AGENT_GUARDS_TEST_KIT_TABLE'):
    from agent_guards import facts
    facts.KIT_TABLE = os.environ['AGENT_GUARDS_TEST_KIT_TABLE']
config.MACHINE_PATH, config.OWNER_UID = machine, int(uid)
sys.argv = [hook] + sys.argv[5:]
runpy.run_path(hook, run_name='__main__')
'''
# the same through the fast path (bin/agent-guards-fast, lib/agent_guards/fast.py), with the config set the same way
FAST_RUNNER = '''import sys
lib, machine, uid, hook = sys.argv[1:5]
sys.path.insert(0, lib)
from agent_guards import config
config.MACHINE_PATH, config.OWNER_UID = machine, int(uid)
from agent_guards import fast
sys.exit(fast.main([hook] + sys.argv[5:]))
'''
VIA = None   # 'fast': run_hook starts every hook through the fast path (tests/fast.test.py runs suites both ways)
# argv: a stand-in Claude Code that runs "$@" as a child (not exec: `; exit $?` after it) and nothing else
STAND_IN = '"$@"; exit $?'
# argv: run argv[1:] with no `claude` above it: the middle process ends at once, so the one below it is re-parented
# (to init or a subreaper) before it starts the command, and reports the command's exit status through a pipe
ORPHAN = '''import os, sys
r, w = os.pipe()
middle = os.fork()
if middle == 0:
    me = os.getpid()
    if os.fork():
        os._exit(0)
    os.close(r)
    while os.getppid() == me:
        os.sched_yield()
    pid = os.fork()
    if pid == 0:
        os.execv(sys.argv[1], sys.argv[1:])
    os.write(w, str(os.waitstatus_to_exitcode(os.waitpid(pid, 0)[1])).encode())
    os._exit(0)
os.close(w)
os.waitpid(middle, 0)
with os.fdopen(r) as f:
    code = f.read()
sys.exit(int(code) if code else 125)
'''


def machine_file(folder, section=None, text=None, mode=0o644):
    """<folder>/machine.json with that "agent-guards" section (or that exact text); its path."""
    path = os.path.join(folder, 'machine.json')
    if text is None:
        data = {'version': 1}
        if section is not None:
            data['agent-guards'] = section
        text = json.dumps(data)
    with open(path, 'w', encoding='utf-8') as f:
        f.write(text)
    os.chmod(path, mode)
    return path


def use_machine(path):
    """This process's hooks (loaded as modules) read `path`, owned by the user who runs the test."""
    config.MACHINE_PATH, config.OWNER_UID = path, os.geteuid()


def load_hook(name, machine=NO_MACHINE):
    """bin/<name> as a module, with its config read from `machine`."""
    use_machine(machine)
    mod_name = name.replace('-', '_')
    loader = importlib.machinery.SourceFileLoader(mod_name, os.path.join(BIN, name))
    spec = importlib.util.spec_from_loader(mod_name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def run_hook(name, event, machine=NO_MACHINE, env=None, cwd=None, timeout=60, args=(), hook=None, parent='claude'):
    """Run bin/<name> (or the file `hook`) in a new process with `event` (a dict, or text) on stdin. Its parent is a
    stand-in Claude Code with nothing else under it (parent='claude'), or it has no `claude` above it (parent=None).
    The whole process group is killed afterwards; past `timeout` that raises subprocess.TimeoutExpired."""
    text = event if isinstance(event, str) else json.dumps(event)
    argv = [sys.executable, '-I', '-B', '-c', FAST_RUNNER if VIA == 'fast' else RUNNER, LIB, machine,
            str(os.geteuid()), hook or os.path.join(BIN, name), *args]
    if parent == 'claude':
        argv, exe = ['claude', '-c', STAND_IN, 'claude', *argv], '/bin/sh'
    elif parent is None:
        argv, exe = [sys.executable, '-I', '-B', '-c', ORPHAN, *argv], None
    else:
        raise ValueError(f'parent: {parent!r}')
    p = subprocess.Popen(argv, executable=exe, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, cwd=cwd, env=env if env is not None else dict(os.environ), start_new_session=True)
    try:
        out, err = p.communicate(text, timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill(p)
        p.communicate()
        raise
    _kill(p)                     # anything the hook left running
    return subprocess.CompletedProcess(argv, p.returncode, out, err)


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except OSError:
        pass


def answer(r):
    """What a finished hook said, as agent_guards.check reads it."""
    from agent_guards import check
    return check._answer(r)
