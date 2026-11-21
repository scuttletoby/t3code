"""agent_guards.config: the machine file's "agent-guards" section, and what every hook does with a section that is
missing, empty, garbled or not safe: its built-in defaults (never a weaker guard), and one line on stderr.

  python3 -I -B fork-kit/agent-guards/tests/config.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import guards_fixture as fx  # noqa: E402
from guards_fixture import config  # noqa: E402

EXAMPLE = os.path.join(os.path.dirname(fx.KIT), 'common', 'machine.example.json')
QUIET_EVENT = {'hook_event_name': 'Stop', 'session_id': 'config-test', 'tool_name': 'Read', 'tool_input': {}}


class Load(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-config-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.addCleanup(fx.use_machine, fx.NO_MACHINE)

    def load(self, hook, section=None, text=None, mode=0o644):
        fx.use_machine(fx.machine_file(self.tmp, section, text, mode))
        return config.load(hook)

    def test_no_file_or_no_section_is_the_defaults_with_no_problem(self):
        fx.use_machine(fx.NO_MACHINE)
        for hook in config.HOOKS:
            self.assertEqual(config.load(hook), (config.defaults(hook), None))
            self.assertEqual(self.load(hook), (config.defaults(hook), None))
        self.assertEqual(config.defaults('memoryIndex'), {'maxChars': 6000, 'maxLine': 160,
                                                          'stateDir': '~/.cache/memory-index-guard'})
        self.assertEqual(config.defaults('capacityPipe'), {'commands': ['t3-capacity']})
        self.assertEqual(config.defaults('agentSlots')['sessionsFile'], '')     # does nothing until configured

    def test_a_section_fills_in_what_it_leaves_out(self):
        got, problem = self.load('memoryIndex', {'memoryIndex': {'maxChars': 4000}})
        self.assertIsNone(problem)
        self.assertEqual(got, {'maxChars': 4000, 'maxLine': 160, 'stateDir': '~/.cache/memory-index-guard'})
        got, problem = self.load('bgGuard', {'memoryIndex': {'maxChars': 4000}})
        self.assertEqual((got, problem), (config.defaults('bgGuard'), None))

    def test_the_example_section_loads(self):
        with open(EXAMPLE) as f:
            section = json.load(f)['agent-guards']
        for hook in config.HOOKS:
            got, problem = self.load(hook, section)
            self.assertIsNone(problem, hook)
            for key, value in section.get(hook, {}).items():
                self.assertEqual(got[key], value)
        self.assertEqual(set(section), set(config.HOOKS))      # the example shows every hook's keys
        for hook in config.HOOKS:
            self.assertEqual(set(section[hook]), set(config.SPEC[hook]), hook)

    def test_a_broken_file_or_section_gives_the_defaults_and_names_the_problem(self):
        cases = {
            'empty file': dict(text=''),
            'garbled JSON': dict(text='{"version": 1, "agent-guards": {'),
            'not an object': dict(text='[]'),
            'no version': dict(text='{"agent-guards": {}}'),
            'a newer version': dict(text='{"version": 99, "agent-guards": {}}'),
            'a duplicate key': dict(text='{"version": 1, "agent-guards": {}, "agent-guards": {}}'),
            'section not an object': dict(section=[]),
            'unknown hook': dict(section={'memoryIndx': {}}),
            'unknown key': dict(section={'memoryIndex': {'maxchars': 1}}),
            'wrong type': dict(section={'memoryIndex': {'maxChars': '6000'}}),
            'a bool for a number': dict(section={'memoryIndex': {'maxChars': True}}),
            'zero': dict(section={'memoryIndex': {'maxLine': 0}}),
            'a relative path': dict(section={'memoryIndex': {'stateDir': 'cache/x'}}),
            'others can write it': dict(section={}, mode=0o666),
        }
        for name, kw in cases.items():
            with self.subTest(name):
                got, problem = self.load('memoryIndex', **kw)
                self.assertEqual(got, config.defaults('memoryIndex'))
                self.assertTrue(problem)

    def test_each_hook_checks_its_own_keys(self):
        bad = {
            'actionsDisable': [{'refuseScripts': [{'name': '../x.py', 'flag': '--fix'}]},
                               {'refuseScripts': [{'name': 'x.py', 'flag': 'fix'}]},
                               {'refuseScripts': [{'name': '.py', 'flag': '--fix'}]},
                               {'refuseScripts': [{'name': 'x.py', 'flag': '--fix', 'when': 'always'}]},
                               {'refuseScripts': 'x.py'}],
            'capacityPipe': [{'commands': []}, {'commands': ['a b']}, {'commands': ['/bin/x']}, {'commands': 'x'}],
            'bgGuard': [{'skipSessionsFiles': ['rel']}, {'skipSessionsFiles': ['']}, {'maxBlocks': -1}],
            'agentSlots': [{'processName': 'a b'}, {'maxAlive': 0}, {'queue': 'QUEUE.md'}],
        }
        for hook, sections in bad.items():
            for s in sections:
                with self.subTest(hook=hook, section=s):
                    got, problem = self.load(hook, {hook: s})
                    self.assertEqual(got, config.defaults(hook))
                    self.assertTrue(problem)

    def test_load_all_names_each_problem_once(self):
        fx.use_machine(fx.machine_file(self.tmp, text='{'))
        settings, problems = config.load_all()
        self.assertEqual(settings, {h: config.defaults(h) for h in config.HOOKS})
        self.assertEqual(len(problems), 1)

    def test_expand(self):
        self.assertEqual(config.expand('~/x/y', home='/h'), '/h/x/y')
        self.assertEqual(config.expand('/abs', home='/h'), '/abs')
        self.assertEqual(config.expand(''), '')


class Hooks(unittest.TestCase):
    """Every hook, run as Claude Code runs it, with a broken machine file: it still answers (its defaults) and says
    so on stderr; with a good file or none it says nothing."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='agent-guards-config-hooks-')
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.env = dict(os.environ, HOME=self.tmp)

    def test_a_broken_section_is_named_on_stderr_and_the_defaults_used(self):
        machine = fx.machine_file(self.tmp, text='{"version": 1, "agent-guards": ')
        for hook in fx.wiring.HOOKS:
            with self.subTest(hook):
                r = fx.run_hook(hook, QUIET_EVENT, machine, env=self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(f'{hook}: the machine file\'s "agent-guards" section cannot be used', r.stderr)
                self.assertIn('built-in defaults used', r.stderr)

    def test_a_good_file_or_none_is_quiet(self):
        good = fx.machine_file(self.tmp, {'memoryIndex': {'maxChars': 5000}})
        for machine in (fx.NO_MACHINE, good):
            for hook in fx.wiring.HOOKS:
                with self.subTest(hook=hook, machine=machine):
                    r = fx.run_hook(hook, QUIET_EVENT, machine, env=self.env)
                    self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_a_copy_without_the_lib_keeps_the_defaults_and_says_so(self):
        """State: a copy that stopped half-way (the hook file is there, its lib is not)."""
        bare = os.path.join(self.tmp, 'bin')
        os.makedirs(bare)
        for hook in fx.wiring.HOOKS:
            with self.subTest(hook):
                shutil.copy(os.path.join(fx.BIN, hook), bare)
                r = subprocess.run([sys.executable, '-I', '-B', os.path.join(bare, hook)], capture_output=True,
                                   text=True, input=json.dumps(QUIET_EVENT), timeout=60, env=self.env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn("cannot load the kit's config code", r.stderr)

    def test_a_usage_error_never_refuses(self):
        """An argument: exit 1 with a usage line (the call runs). actions-disable-guard ignores arguments, as it
        always did, and answers the event."""
        for hook in fx.wiring.HOOKS:
            with self.subTest(hook):
                r = fx.run_hook(hook, QUIET_EVENT, args=('--install',), env=self.env)
                if hook == 'actions-disable-guard':
                    self.assertEqual((r.returncode, r.stdout), (0, ''))
                    continue
                self.assertEqual(r.returncode, 1)
                self.assertIn(f'usage: {hook}', r.stderr)


if __name__ == '__main__':
    unittest.main()
