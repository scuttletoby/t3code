"""agent_guards.config: the machine file's "agent-guards" section, and what every hook does with a section that is
missing, empty, garbled or not safe: its built-in defaults (never a weaker guard), named in its answer (a
systemMessage the agent sees) and on stderr. Each hook reads only its own part, key by key.

  python3 -I -B fork-kit/agent-guards/tests/config.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest, uuid

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
        self.assertEqual(set(section), set(config.HOOKS) | {'harnessEnv'})   # the example shows every hook's keys
        self.assertEqual(config.harness_env(section), (
            {'BASH_MAX_OUTPUT_LENGTH': '12000', 'MAX_MCP_OUTPUT_TOKENS': '8000', 'API_TIMEOUT_MS': '180000',
             'DISABLE_AUTOUPDATER': '1'}, []))
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

    def test_an_unknown_sibling_key_keeps_each_hooks_facts(self):
        """Lens A major: a key no hook knows (a later kit's section, a typo of a hook's name) once made every hook
        drop the whole file. Each hook now gets its own facts, with no problem; self-check names the key."""
        with open(EXAMPLE) as f:
            section = json.load(f)['agent-guards']
        section = dict(section, gardener={'any': 'thing'}, memoryIndx={})
        for hook in config.HOOKS:
            with self.subTest(hook):
                got, problem = self.load(hook, section)
                self.assertIsNone(problem)
                self.assertEqual(got, dict(config.defaults(hook), **section[hook]))
        settings, problems = config.load_all()
        self.assertEqual(settings, {h: dict(config.defaults(h), **section[h]) for h in config.HOOKS})
        self.assertEqual(sorted(p.split("'")[1] for p in problems), ['gardener', 'memoryIndx'])

    def test_another_hooks_broken_part_changes_nothing(self):
        section = {'memoryIndex': {'maxChars': 4000}, 'bgGuard': {'maxBlocks': -1}, 'agentSlots': []}
        got, problem = self.load('memoryIndex', section)
        self.assertEqual((got['maxChars'], problem), (4000, None))
        got, problem = self.load('bgGuard', section)
        self.assertEqual(got, config.defaults('bgGuard'))
        self.assertIn('maxBlocks', problem)

    def test_a_bad_or_unknown_key_keeps_the_others(self):
        got, problem = self.load('memoryIndex', {'memoryIndex': {'maxChars': 4000, 'maxLine': 0, 'maxchars': 1}})
        self.assertEqual(got, dict(config.defaults('memoryIndex'), maxChars=4000))
        self.assertIn('maxLine', problem)
        self.assertIn("'maxchars'", problem)

    def test_a_bad_row_keeps_the_good_rows(self):
        good = {'name': 'garden.py', 'flag': '--fix', 'why': 'x'}
        got, problem = self.load('actionsDisable', {'actionsDisable': {
            'refuseScripts': [good, {'name': '../x.py', 'flag': '--fix'}, {'name': 'b.py', 'flag': '--off'}]}})
        self.assertEqual([r['name'] for r in got['refuseScripts']], ['garden.py', 'b.py'])
        self.assertIn('refuseScripts', problem)

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

    def test_harness_env(self):
        self.assertEqual(config.harness_env({}), ({'BASH_MAX_OUTPUT_LENGTH': '12000', 'MAX_MCP_OUTPUT_TOKENS': '8000',
                                                    'API_TIMEOUT_MS': '180000', 'DISABLE_AUTOUPDATER': '1'}, []))
        self.assertEqual(set(config.HARNESS_ENV), {
            'BASH_MAX_OUTPUT_LENGTH', 'MAX_MCP_OUTPUT_TOKENS', 'API_TIMEOUT_MS', 'DISABLE_AUTOUPDATER',
            'CLAUDE_CODE_AUTO_COMPACT_WINDOW', 'CLAUDE_CODE_PROMPT_CACHE_TTL', 'CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL'})
        env, problems = config.harness_env({'harnessEnv': [
            {'name': 'API_TIMEOUT_MS', 'value': 240000}, {'name': 'DISABLE_AUTOUPDATER', 'value': None},
            {'name': 'MY_FLAG', 'value': ''}, {'name': 'API_TIMEOUT_MS', 'value': '1'},
            {'name': 'CLAUDE_CODE_OAUTH_TOKEN', 'value': 'x'}, {'name': '1BAD', 'value': '1'},
            {'name': 'X', 'value': False}, {'name': 'X', 'value': '1', 'why': 'y'}, 'X=1']})
        self.assertEqual(env, {'BASH_MAX_OUTPUT_LENGTH': '12000', 'MAX_MCP_OUTPUT_TOKENS': '8000',
                               'API_TIMEOUT_MS': '240000', 'MY_FLAG': ''})
        self.assertEqual(len(problems), 6, problems)
        self.assertIn("'API_TIMEOUT_MS' is given twice", problems[0])
        self.assertIn('looks like a secret', problems[1])
        self.assertEqual(config.harness_env({'harnessEnv': {'API_TIMEOUT_MS': '1'}})[0], config.harness_env({})[0])
        self.assertIn('must be a list', config.harness_env({'harnessEnv': {}})[1][0])

    def test_load_all_takes_harness_env_and_names_its_problems(self):
        fx.use_machine(fx.machine_file(self.tmp, {'harnessEnv': [{'name': 'API_TIMEOUT_MS', 'value': '1'}]}))
        self.assertEqual(config.load_all()[1], [])
        fx.use_machine(fx.machine_file(self.tmp, {'harnessEnv': [{'name': 'ANTHROPIC_API_KEY', 'value': '1'}]}))
        problems = config.load_all()[1]
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('agent-guards.harnessEnv', problems[0])

    def test_load_all_names_each_problem_once(self):
        fx.use_machine(fx.machine_file(self.tmp, text='{'))
        settings, problems = config.load_all()
        self.assertEqual(settings, {h: config.defaults(h) for h in config.HOOKS})
        self.assertEqual(len(problems), 1)

    def test_a_notice_is_said_once_a_session_or_every_time_when_it_cannot_be_marked(self):
        ev = {'session_id': str(uuid.uuid4())}
        self.assertTrue(config.first_notice('broken', ev, home=self.tmp))
        self.assertFalse(config.first_notice('broken', ev, home=self.tmp))
        self.assertTrue(config.first_notice('other', ev, home=self.tmp))
        self.assertTrue(config.first_notice('broken', {}, home=self.tmp))
        # the notices folder is a file: no marker can be made, so the notice shows every time
        other = os.path.join(self.tmp, 'other-home')
        os.makedirs(os.path.join(other, '.cache', 'agent-guards'))
        open(os.path.join(other, '.cache', 'agent-guards', 'notices'), 'w').close()
        for _ in range(2):
            self.assertTrue(config.first_notice('broken', ev, home=other))

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

    def test_a_broken_section_is_named_to_the_agent_and_the_defaults_used(self):
        """Lens A/B minor: the fallback was on stderr only, which Claude Code does not show for exit 0. The answer
        now carries it as a systemMessage (and as additionalContext where the event takes one)."""
        machine = fx.machine_file(self.tmp, text='{"version": 1, "agent-guards": ')
        pre = {'hook_event_name': 'PreToolUse', 'session_id': 'config-test', 'tool_name': 'Read',
               'tool_input': {'file_path': '/nonexistent'}}
        for hook in fx.wiring.HOOKS:
            for ev in (QUIET_EVENT, pre):
                with self.subTest(hook=hook, event=ev['hook_event_name']):
                    # a new session each: the speed hooks say it once a session (#1151)
                    r = fx.run_hook(hook, dict(ev, session_id=uuid.uuid4().hex), machine, env=self.env)
                    self.assertEqual(r.returncode, 0, r.stderr)
                    self.assertIn(f'{hook}: the machine file\'s "agent-guards" section cannot be used', r.stderr)
                    self.assertIn('built-in defaults used', r.stderr)
                    out = json.loads(r.stdout)
                    self.assertIn('built-in defaults used', out['systemMessage'])
                    self.assertNotIn('decision', out)
                    if ev is pre:
                        spec = out['hookSpecificOutput']
                        self.assertEqual(spec['hookEventName'], 'PreToolUse')
                        self.assertIn('built-in defaults used', spec['additionalContext'])
                        self.assertNotIn('permissionDecision', spec)

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
                self.assertIn("cannot load the kit's config code", json.loads(r.stdout)['systemMessage'])

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
