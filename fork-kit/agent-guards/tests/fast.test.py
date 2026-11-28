#!/usr/bin/env python3
"""Tests of the hooks' fast path (bin/agent-guards-fast, lib/agent_guards/fast.py and quick.py, #1094).

The contract: for every case the hooks' own tests know, the fast path gives the hook's answer. Where it answers
itself (a pass), the full hook passes too; everywhere else it runs the hook file on the same event.

  node .github/scripts/fork-ci-tests.cjs fork-kit/agent-guards/

(a test runner handed the folder: this file loads actions-disable.test.py, full of the patterns that guard refuses.)
"""
import importlib, importlib.machinery, importlib.util, inspect, json, marshal, os, py_compile, re, shutil, subprocess, sys
import signal, tempfile, threading, time, types, unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import guards_fixture as fx  # noqa: E402
from agent_guards import fast  # noqa: E402

FAST_ENTRY = os.path.join(fx.BIN, 'agent-guards-fast')


def suite(name):
    """One of the hooks' test files as a module (its test classes are used below by attribute, never by name, so
    this file does not run them a second time as they are)."""
    mod = name.replace('-', '_').replace('.', '_')
    loader = importlib.machinery.SourceFileLoader(mod, os.path.join(HERE, name))
    m = types.ModuleType(mod)
    m.__file__ = loader.path
    loader.exec_module(m)
    return m


AD = suite('actions-disable.test.py')
CAP = suite('capacity-pipe.test.py')
MEM = suite('memory-index.test.py')


def quick_for(machine):
    """agent_guards.quick as the fast path loads it, with this machine file (it reads its config on import)."""
    fx.use_machine(machine)
    from agent_guards import quick
    return importlib.reload(quick)


def differences(copy, guard):
    """Where the fast path's copy of the guard's fast exit differs from the guard's own: [name]."""
    out = []
    for name, value in vars(copy).items():
        if name.startswith('__') or inspect.ismodule(value) or name in ('SETTINGS', 'SETTINGS_PROBLEM'):
            continue
        if not hasattr(guard, name):
            out.append(f'{name}: not in the guard')
            continue
        theirs = getattr(guard, name)
        if inspect.isfunction(value):
            same = inspect.isfunction(theirs) and inspect.getsource(value) == inspect.getsource(theirs)
        elif isinstance(value, re.Pattern):
            same = isinstance(theirs, re.Pattern) and (value.pattern, value.flags) == (theirs.pattern, theirs.flags)
        elif name == '_HOME_DIRS':   # a cache: the same kind of thing, its entries are whatever was asked
            same = type(value) is type(theirs)
        else:
            same = type(value) is type(theirs) and value == theirs
        if not same:
            out.append(name)
    return out


def event(cmd, tool='Bash', cwd=None, **more):
    ev = dict({'session_id': 't', 'hook_event_name': 'PreToolUse', 'tool_name': tool,
               'tool_input': {'command': cmd, 'description': 'x'}}, **more)
    if cwd:
        ev['cwd'] = cwd
    return json.dumps(ev).encode()


def slow_event(hook):
    """An event the fast check of `hook` does not pass, so the hook runs."""
    if hook == 'memory-index-guard':
        return json.dumps({'hook_event_name': 'PreToolUse', 'tool_name': 'Write',
                           'tool_input': {'file_path': '/x/MEMORY.md'}}).encode()
    return event('t3-capacity 1 && x' if hook == 'capacity-pipe-guard' else 'pnpm test')


class QuickCopyTest(unittest.TestCase):
    """Can the fast path's copy of actions-disable-guard's fast exit (agent_guards.quick) drift from the guard?"""

    @classmethod
    def setUpClass(cls):
        cls.g = AD.load()
        cls.machine = os.path.join(AD._MACHINE.name, 'machine.json')   # the guard's tests' machine file
        cls.q = quick_for(cls.machine)

    def test_every_name_of_the_copy_is_the_guards(self):
        self.assertEqual(differences(self.q, self.g), [])
        self.assertIn('examplegarden', self.q.QUICK_KEYS)   # the machine file's refuseScripts, read the same way

    def test_the_copy_holds_the_whole_fast_exit(self):
        names = {n for n in vars(self.g) if n.startswith(('quick_', 'QUICK_', '_SED_'))}
        self.assertEqual(names - set(vars(self.q)), set())
        self.assertGreater(len(names), 15)

    def test_a_changed_copy_is_found(self):
        """The comparison is no rubber stamp: a word more, a function changed, a regex changed are each found."""
        for name, mutate in (('QUICK_WORDS', lambda q: q.QUICK_WORDS | {'bash'}),
                             ('QUICK_CHARS', lambda q: re.compile(q.QUICK_CHARS.pattern.replace('<<', '<<<'))),
                             ('quick_glob_names', None)):
            with self.subTest(name):
                m = types.ModuleType('mutant')
                m.__dict__.update(vars(self.q))
                if mutate:
                    setattr(m, name, mutate(self.q))
                else:   # a function with other source (inspect reads it from a file of its own)
                    path, text = os.path.join(AD._MACHINE.name, 'mutant.py'), 'def quick_glob_names(w, h):\n    return 0\n'
                    with open(path, 'w') as f:
                        f.write(text)
                    exec(compile(text, path, 'exec'), m.__dict__)
                self.assertEqual(differences(m, self.g), [name])


class ActionsDisableContractTest(AD.Case):
    """Is every call the fast path passes one the full guard passes too, over every command of the guard's tests,
    and does it pass exactly what the guard's own fast exit passes (no less, so it is as fast as it can be)?"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.q = quick_for(os.path.join(AD._MACHINE.name, 'machine.json'))

    def setUp(self):
        super().setUp()
        for name, text in AD.FILES.items():
            self.write(name, text, 0o755 if name.endswith('.sh') else 0o644)

    def corpus(self):
        out = AD.QUICK_PASS + AD.TRICKY + AD.QUICK_REFUSED + AD.DENY + AD.ALLOW + AD.ALLOW_MORE
        out += [c for cmds in AD.ATTACKS.values() for c in cmds] + [c for c, _ in AD.OVER_REFUSED]
        out += [f'{w} {f}' for w in sorted(self.g.QUICK_WORDS) for f in AD.FILES]
        out += [f'git {s} {f}' for s in sorted(self.g.GIT_SAFE) for f in AD.FILES]
        return list(dict.fromkeys(out))

    def fast(self, cmd, tool='Bash', env=None):
        return fast.actions_disable(event(cmd, tool, self.dir), self.env if env is None else env)

    def test_what_the_fast_path_passes_the_full_guard_passes(self):
        fast_ones = [c for c in self.corpus() if self.fast(c)]
        self.assertGreater(len(fast_ones), 500)
        with mock.patch.object(self.g, 'QUICK', False):
            for cmd in fast_ones:
                with self.subTest(cmd=cmd):
                    self.assertIsNone(self.decide(cmd), cmd)

    def test_it_passes_what_the_guards_fast_exit_passes(self):
        for cmd in self.corpus():
            with self.subTest(cmd=cmd):
                want = self.g.quick_pass(cmd) and not self.g.quick_shadowed(cmd, self.dir, self.env)
                self.assertEqual(self.fast(cmd), bool(want))

    def test_no_refused_case_is_fast(self):
        refused = AD.DENY + [c for cmds in AD.ATTACKS.values() for c in cmds] + [c for c, _ in AD.OVER_REFUSED]
        for cmd in refused:
            with self.subTest(cmd=cmd):
                self.assertFalse(self.fast(cmd), cmd)
                self.assertFalse(self.fast(cmd, 'Monitor'), cmd)

    def test_a_shadowing_file_on_path_goes_to_the_guard(self):
        self.write('.local/bin/ls', 'echo hi\n')
        env = dict(self.env, PATH=os.path.join(self.dir, '.local/bin') + ':/usr/bin:/bin')
        self.assertTrue(self.fast('ls -la'))
        self.assertFalse(self.fast('ls -la', env=env))

    def test_other_tools_pass_and_odd_events_go_to_the_guard(self):
        self.assertTrue(fast.actions_disable(event('gh workflow disable 1', 'Read'), self.env))
        for data in (b'', b'not json', b'[]', b'{"tool_name": "Bash", "tool_input": "x"}', b'\xff\xfe'):
            with self.subTest(data):
                self.assertFalse(fast.passes('/x/actions-disable-guard', data, self.env))
        self.assertFalse(fast.actions_disable(event('ls'), dict(self.env, ACTIONS_DISABLE_GUARD_TEST_CRASH='1')))
        # a tool name that is no name is no shell tool there either (the guard: `not in TOOLS`, then it passes)
        self.assertTrue(fast.actions_disable(b'{"tool_name": ["Bash"]}', self.env))
        self.assertIsNone(self.g.decide('', self.dir, self.env))


class CapacityContractTest(unittest.TestCase):
    """Is every command the fast path passes one capacity-pipe-guard passes, over the guard's own cases?"""

    def test_what_the_fast_path_passes_the_guard_passes(self):
        fx.use_machine(fx.NO_MACHINE)
        g = fx.load_hook('capacity-pipe-guard')
        cases = CAP.REFUSED + CAP.ALLOWED + CAP.NOT_COVERED + AD.QUICK_PASS + AD.TRICKY
        n = 0
        for cmd in cases:
            cmd = cmd if isinstance(cmd, str) else cmd[0]
            with self.subTest(cmd=cmd):
                if fast.capacity_pipe(event(cmd), {}):
                    n += 1
                    self.assertIsNone(g.piped(cmd), cmd)
        self.assertGreater(n, 50)
        for cmd in CAP.REFUSED:
            cmd = cmd if isinstance(cmd, str) else cmd[0]
            self.assertFalse(fast.capacity_pipe(event(cmd), {}), cmd)
        self.assertTrue(fast.capacity_pipe(event('t3-capacity 1 | tail', 'Read'), {}))
        self.assertFalse(fast.passes('/x/capacity-pipe-guard', b'{"tool_name": ["Bash"]}', {}))


class ViaFast:
    """Mixed into a hook's own test class: every hook run of it goes through the fast path."""

    def setUp(self):
        fx.VIA = 'fast'
        self.addCleanup(setattr, fx, 'VIA', None)
        super().setUp()


class MemoryPreViaFast(ViaFast, MEM.Pre):
    """memory-index-guard's PreToolUse tests, through the fast path: the same answers?"""


class MemoryPostViaFast(ViaFast, MEM.Post):
    """memory-index-guard's PostToolUse tests (the state file, a changed index), through the fast path."""


class MemoryRobustViaFast(ViaFast, MEM.Robust):
    """Odd input never breaks a call, through the fast path too."""


class CapacityConfiguredViaFast(ViaFast, CAP.Configured):
    """capacity-pipe-guard with a machine file (other checks, a broken section), through the fast path."""


class CapacityHookViaFast(CAP.Hook):
    """capacity-pipe-guard as Claude Code runs it, started by the real bin/agent-guards-fast: the same answers?"""

    def hook(self, event, raw=None):
        return subprocess.run([sys.executable, '-I', '-B', '-S', FAST_ENTRY, CAP.HOOK],
                              input=json.dumps(event) if raw is None else raw, capture_output=True, text=True,
                              timeout=60)


class EndToEndTest(unittest.TestCase):
    """The hook started through the fast path and started as before: the same exit code and the same answer, for
    a sample of every kind of case of actions-disable-guard (passed fast, passed by the full reading, refused, odd
    input), with the guard's tests' machine file."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-fast-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp}
        self.machine = os.path.join(AD._MACHINE.name, 'machine.json')

    def both(self, name, data):
        out = []
        for via in (None, 'fast'):
            fx.VIA = via
            try:
                r = fx.run_hook(name, data, self.machine, env=self.env, cwd=self.tmp)
            finally:
                fx.VIA = None
            out.append((r.returncode, fx.answer(r), r.stdout))
        return out

    def test_the_same_answer_both_ways(self):
        cases = AD.QUICK_PASS[:8] + AD.DENY[:12] + AD.ALLOW[:8] + AD.TRICKY[:8] + [
            'python3 scripts/dev/example_garden.py --fix', 'pnpm test x.ts', 'ls; node x.js']
        events = [event(c, cwd=self.tmp).decode() for c in cases] + [
            event('gh workflow disable 1', 'Monitor', self.tmp).decode(), event('x', 'Read').decode(),
            '', 'not json', '[]', '{"tool_name": "Bash", "tool_input": "x"}']
        answers = set()
        for data in events:
            with self.subTest(data=data[:120]):
                before, now = self.both('actions-disable-guard', data)
                self.assertEqual(now, before)
                answers.add(before[1])
        self.assertEqual(answers, {'passed', 'refused'})


STAND_IN = '''import sys
print(sys.argv[1:], repr(sys.stdin.read()), sys.stdin.encoding, sys.stdin.errors, sys._getframe().f_code.co_filename)
'''


class LauncherTest(unittest.TestCase):
    """What the fast path does around the check: when it runs the hook, with what, and from which bytecode."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-fast-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.home = os.path.join(self.tmp, 'home')
        os.makedirs(self.home)
        self.env = dict(os.environ, HOME=self.home, PATH='/usr/bin:/bin')

    def stand_in(self, name, text=STAND_IN, folder='hooks'):
        path = os.path.join(self.tmp, folder, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)
        return path

    def via(self, hook, data, machine=fx.NO_MACHINE, args=()):
        argv = [sys.executable, '-I', '-B', '-c', fx.FAST_RUNNER, fx.LIB, machine, str(os.geteuid()), hook, *args]
        return subprocess.run(argv, input=data, capture_output=True, env=self.env, timeout=60)

    def plain(self, hook, data, args=()):
        return subprocess.run([sys.executable, '-I', '-B', hook, *args], input=data, capture_output=True,
                              env=self.env, timeout=60)

    def test_a_pass_never_loads_the_hook_and_anything_else_runs_it(self):
        mem = {'hook_event_name': 'PreToolUse', 'tool_name': 'Write', 'tool_input': {'file_path': '/x/MEMORY.md'}}
        for name, fast_one, slow_one in (
                ('actions-disable-guard', event('ls -la'), event('pnpm test')),
                ('capacity-pipe-guard', event('ls -la'), event('t3-capacity 1 && x')),
                ('memory-index-guard', event('ls -la'), json.dumps(mem).encode())):
            with self.subTest(name):
                hook = self.stand_in(name)
                r = self.via(hook, fast_one)
                self.assertEqual((r.returncode, r.stdout, r.stderr), (0, b'', b''))
                r = self.via(hook, slow_one)
                self.assertEqual((r.returncode, r.stdout), (0, self.plain(hook, slow_one).stdout), r.stderr)
                self.assertIn(repr(slow_one.decode()).encode(), r.stdout)

    def test_the_hook_reads_the_event_as_it_would_from_its_stdin(self):
        hook = self.stand_in('actions-disable-guard')
        for data in (b'{"tool_name": "Bash", "tool_input": {"command": "a\r\nb"}}', b'\xff\xfe not utf-8 \x00',
                     b'', b'{"a": "\xc3\xa9"}' * 5000):
            with self.subTest(data[:40]):
                self.assertEqual(self.via(hook, data).stdout, self.plain(hook, data).stdout)

    def test_a_machine_file_problem_an_unknown_hook_or_arguments_go_to_the_hook(self):
        broken = fx.machine_file(self.tmp, text='{')
        for name in ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard'):
            with self.subTest(name):
                r = self.via(self.stand_in(name), event('ls'), machine=broken)
                self.assertIn(b'"command": "ls"', r.stdout)
        r = self.via(self.stand_in('bg-guard'), event('ls'))
        self.assertIn(b'"command": "ls"', r.stdout)
        r = self.via(self.stand_in('actions-disable-guard'), event('ls'), args=('--help',))
        self.assertTrue(r.stdout.startswith(b"['--help']"), r.stdout)

    def test_without_its_lib_the_entry_runs_the_hook_as_before(self):
        entry = os.path.join(self.tmp, 'lone', 'bin', 'agent-guards-fast')
        os.makedirs(os.path.dirname(entry))
        shutil.copy(FAST_ENTRY, entry)
        hook = self.stand_in('actions-disable-guard')
        r = subprocess.run([sys.executable, '-I', '-B', '-S', entry, hook], input=event('ls'), capture_output=True,
                           env=self.env, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(b'"command": "ls"', r.stdout)
        r = subprocess.run([sys.executable, '-I', '-B', '-S', entry], input=b'', capture_output=True, timeout=60)
        self.assertEqual(r.returncode, 1)

    def test_bytecode_beside_the_hook_is_used_only_while_it_matches(self):
        """A checked-hash .pyc beside the hook (as the installer writes it) is what runs, and only while it was made
        for the file's text: here its code prints FROM-PYC, so which one ran shows."""
        hook = self.stand_in('actions-disable-guard')
        with open(hook, 'rb') as f:
            source = f.read()
        pyc = self.pyc(hook, 3, source, 'print("FROM-PYC")')   # a checked hash-based .pyc (PEP 552)
        self.assertEqual(self.via(hook, event('pnpm test')).stdout, b'FROM-PYC\n')
        with open(hook, 'a') as f:
            f.write('# changed after install\n')
        self.assertIn(b'"command": "pnpm test"', self.via(hook, event('pnpm test')).stdout)
        # and the real thing: py_compile's checked hash .pyc of the file as it is now is taken (its code is named
        # after the file it was compiled from; the source compiled here is named after the path the hook was given)
        py_compile.compile(hook, cfile=pyc, doraise=True, invalidation_mode=py_compile.PycInvalidationMode.CHECKED_HASH)
        os.chmod(pyc, 0o644)   # as the installer does (py_compile takes the source's mode, which follows the umask)
        link = os.path.join(self.tmp, 'links', 'actions-disable-guard')
        os.makedirs(os.path.dirname(link))
        os.symlink(hook, link)
        self.assertEqual(fast.hook_code(link).co_filename, hook)
        os.unlink(pyc)
        self.assertEqual(fast.hook_code(link).co_filename, link)

    def installed(self, name):
        """The installed layout: versions/<sha>/bin/<hook> with the installer's .pyc (crafted: its code prints
        FROM-PYC, so which one ran shows), and the bin folder of links the settings entry names (`"$g"`)."""
        hook = self.stand_in(name, folder='versions/abc/bin')
        with open(hook, 'rb') as f:
            source = f.read()
        self.pyc(hook, 3, source, 'print("FROM-PYC")')
        link = os.path.join(self.tmp, 'usr-local-bin', name)
        os.makedirs(os.path.dirname(link), exist_ok=True)
        os.symlink(os.path.relpath(hook, os.path.dirname(link)), link)
        return hook, link, source

    @staticmethod
    def pyc(path, flags, source, text):
        """A hash-based .pyc (PEP 552) for `path`: flags 3 checked, 1 unchecked; `text` is its code. Mode 0644, as
        the installer writes it (kit_install.bytecode_files), whatever the umask: under a umask of 0002 a new file
        is group-writable, and hook_code rightly never uses such a .pyc."""
        pyc = importlib.util.cache_from_source(path)
        os.makedirs(os.path.dirname(pyc), exist_ok=True)
        with open(pyc, 'wb') as f:
            f.write(importlib.util.MAGIC_NUMBER + flags.to_bytes(4, 'little') + importlib.util.source_hash(source)
                    + marshal.dumps(compile(text, path, 'exec')))
        os.chmod(pyc, 0o644)
        return pyc

    def test_through_the_bin_folder_of_links_only_the_version_copys_checked_pyc_runs(self):
        """Fix round 1 of #1119: the settings entry gives the link /usr/local/bin/<hook>. The bytecode used is the
        installer's, beside the file the link points to; a .pyc planted beside the link (outside the manifest and the
        self-check) never runs, whatever kind it is; an unchecked or writable one in the copy is not used either."""
        for name in ('actions-disable-guard', 'capacity-pipe-guard', 'memory-index-guard'):
            with self.subTest(name):
                hook, link, source = self.installed(name)
                for flags in (1, 3):   # Python takes an unchecked one as it is, a checked one if the source matches
                    self.pyc(link, flags, source, 'print("PLANTED")')
                    self.assertEqual(self.via(link, slow_event(name)).stdout, b'FROM-PYC\n')
                self.pyc(hook, 1, source, 'print("FROM-PYC")')   # unchecked in the copy: the source runs
                r = self.via(link, slow_event(name))
                self.assertNotIn(b'PLANTED', r.stdout)
                self.assertIn(b'"tool_name"', r.stdout)
                os.chmod(self.pyc(hook, 3, source, 'print("FROM-PYC")'), 0o666)   # writable by others: the source
                self.assertIn(b'"tool_name"', self.via(link, slow_event(name)).stdout)
                os.unlink(importlib.util.cache_from_source(hook))   # none (another Python's tag): the source
                self.assertIn(b'"tool_name"', self.via(link, slow_event(name)).stdout)
                os.unlink(link)
                os.symlink(os.path.join(self.tmp, 'versions', 'gone', name), link)   # dangling: no pass, a failure
                r = self.via(link, slow_event(name))
                self.assertEqual((r.returncode, r.stdout), (1, b''))
                self.assertIn(b'FileNotFoundError', r.stderr)

    def test_a_slow_start_never_refuses_a_command_the_fast_path_passes(self):
        """The process is held up 5.5 s before it reads its event (a machine under memory pressure; the event is
        waiting on stdin): past the guard's 5 s budget, the plain command still passes (the fast check never looks at
        the clock)."""
        hook = os.path.join(fx.BIN, 'actions-disable-guard')
        with subprocess.Popen([sys.executable, '-I', '-B', '-c', fx.FAST_RUNNER, fx.LIB, fx.NO_MACHINE,
                               str(os.geteuid()), hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, env=self.env) as p:
            os.kill(p.pid, signal.SIGSTOP)
            p.stdin.write(event('ls -la', cwd=self.tmp))
            p.stdin.close()
            time.sleep(5.5)
            os.kill(p.pid, signal.SIGCONT)
            out, err = p.stdout.read(), p.stderr.read()
            p.wait(timeout=60)
        self.assertEqual((p.returncode, out), (0, b''), err)

    def test_an_event_that_never_comes_gets_the_guards_own_refusal_in_time(self):
        """Fix round 1 of #1119: the fast path reads stdin under its own alarm (FAST_BUDGET), as the guard does. A
        stdin that is never closed goes to the guard, which refuses with its own message well inside the hook's
        10 s (as it does when started as before), instead of hanging until Claude Code ends the hook."""
        hook = os.path.join(fx.BIN, 'actions-disable-guard')
        start = time.monotonic()
        with subprocess.Popen([sys.executable, '-I', '-B', '-c', fx.FAST_RUNNER, fx.LIB, fx.NO_MACHINE,
                               str(os.geteuid()), hook], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, env=self.env) as p:
            p.stdin.write(b'{"tool_name": "Bash", ')   # half an event, and stdin stays open
            p.stdin.flush()
            stop = threading.Timer(15, p.kill)   # a fast path that waits on stdin for ever: no answer
            stop.start()
            self.addCleanup(stop.cancel)
            out = p.stdout.read()
            took = time.monotonic() - start
            p.stdin.close()
            p.wait(timeout=60)
        self.assertIn(b'could not finish reading this command', out)
        self.assertLess(took, 9.5)

if __name__ == '__main__':
    unittest.main()
