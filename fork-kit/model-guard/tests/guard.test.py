"""The model-guard hook against every rule of README.md: each refuse path, the modes, the policy file's rules and
what happens when the files change, vanish or break between calls. No root: the files belong to the current user,
who stands in for root. RedCheck breaks each refuse rule in a copy of lib/model_guard.py and shows its test then
fails.

  python3 -I -B fork-kit/model-guard/tests/guard.test.py
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, types, unittest

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
LIB = os.path.join(KIT, 'lib', 'model_guard.py')
sys.path.insert(0, os.path.join(KIT, 'lib'))
import model_guard  # noqa: E402

LARGE = {'driver': 'claudeAgent', 'model': 'example-large-1', 'efforts': ['medium', 'high'], 'defaultEffort': 'high',
         'aliases': ['large'], 'useFor': 'most work'}
DEEP = {'driver': 'claudeAgent', 'model': 'example-deep-2', 'efforts': ['high'], 'aliases': ['deep']}
PLAIN = {'driver': 'otherDriver', 'model': 'example-plain', 'aliases': ['plain']}
POLICY = {'mode': 'enforce', 'allow': [LARGE, DEEP, PLAIN]}
GOOD_TARGET = {'model': 'example-large-1', 'options': {'effort': 'high'}}


def load(source=None):
    if source is None:
        return model_guard
    mod = types.ModuleType('model_guard_broken')
    mod.__file__ = LIB
    exec(compile(source, LIB, 'exec'), mod.__dict__)
    return mod


class Guard(unittest.TestCase):
    m = model_guard

    def setUp(self):
        self.umask = os.umask(0o022)
        self.dir = os.path.realpath(tempfile.mkdtemp(prefix='model-guard-test-'))
        os.chmod(self.dir, 0o755)
        self.addCleanup(self.close)
        self.machine_path = os.path.join(self.dir, 'machine.json')
        self.policy_path = os.path.join(self.dir, 'policy.json')
        self.machine({'policyFile': self.policy_path})
        self.policy(POLICY)

    def close(self):
        os.umask(self.umask)
        shutil.rmtree(self.dir, ignore_errors=True)

    def write(self, path, data):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))

    def machine(self, section):
        self.write(self.machine_path, {'version': 1, 'model-guard': section})

    def policy(self, data):
        self.write(self.policy_path, data)

    def hook(self, tool, tool_input=None, raw=None):
        """The hook's printed answer (a dict), or None when it printed nothing."""
        out = io.StringIO()
        text = raw if raw is not None else json.dumps({'tool_name': tool, 'tool_input': tool_input or {}})
        self.assertEqual(self.m.run_hook(io.StringIO(text), out, self.machine_path, os.getuid()), 0)
        return json.loads(out.getvalue())['hookSpecificOutput'] if out.getvalue() else None

    def refused(self, tool, tool_input=None, *parts, raw=None):
        out = self.hook(tool, tool_input, raw)
        self.assertIsNotNone(out, 'the call passed')
        self.assertEqual(out.get('permissionDecision'), 'deny', out)
        for part in parts:
            self.assertIn(part, out['permissionDecisionReason'])
        return out['permissionDecisionReason']

    def passed(self, tool, tool_input=None):
        out = self.hook(tool, tool_input)
        self.assertNotIn('permissionDecision', out or {}, out)
        return (out or {}).get('additionalContext')

    def delegate(self, target):
        return {'task': 'x', 'target': target} if target is not None else {'task': 'x'}

    # ---- which tools

    def test_unchecked_tools_pass_even_without_a_policy(self):
        os.unlink(self.machine_path)
        for tool in ('Bash', 'Edit', 'mcp__t3-code__t3_thread_send', 'mcp__t3-code__task_status', 'TaskStop'):
            self.assertIsNone(self.hook(tool, {'model': 'anything'}), tool)

    def test_every_t3_tool_under_any_server_name_is_checked(self):
        for name in ('mcp__t3-code__delegate_task', 'mcp__t3__delegate_task', 'mcp__a__b__delegate_task'):
            self.refused(name, self.delegate(None), 'target is missing')

    # ---- the files (fail closed)

    def test_no_machine_file_refuses_and_names_it(self):
        os.unlink(self.machine_path)
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, self.machine_path, 'fails closed')

    def test_no_section_or_no_policy_file_key_refuses(self):
        self.write(self.machine_path, {'version': 1})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'no "model-guard" section')
        self.machine({})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, '"policyFile" is missing')
        self.machine({'policyFile': 'policy.json'})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'must be an absolute path')

    def test_missing_policy_file_refuses_and_names_it(self):
        os.unlink(self.policy_path)
        self.refused('mcp__t3-code__delegate_task', self.delegate(GOOD_TARGET), self.policy_path, 'not there')

    def test_policy_file_others_can_write_or_a_link_refuses(self):
        os.chmod(self.policy_path, 0o666)
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'writable by group or others')
        os.chmod(self.policy_path, 0o644)
        link = os.path.join(self.dir, 'link.json')
        os.symlink(self.policy_path, link)
        self.machine({'policyFile': link})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'symbolic link')

    def test_an_invalid_policy_refuses_in_every_mode_and_names_the_reason(self):
        cases = {
            'not json': ('{', 'not valid JSON'),
            'empty file': ('', 'not valid JSON'),
            'not an object': ([], 'must be a JSON object'),
            'no mode': ({'allow': []}, '"mode" is missing'),
            'no allow': ({'mode': 'off'}, '"allow" is missing'),
            'bad mode': ({'mode': 'on', 'allow': []}, 'must be one of off, advise, enforce'),
            'unknown key': ({'mode': 'off', 'allow': [], 'deny': []}, "unknown key 'deny'"),
            'key twice': ('{"mode": "off", "mode": "enforce", "allow": []}', "'mode' is given twice"),
            'row not object': ({'mode': 'off', 'allow': ['x']}, 'allow[0]: must be a JSON object'),
            'row no model': ({'mode': 'off', 'allow': [{'driver': 'd'}]}, '"model" is missing'),
            'row empty model': ({'mode': 'off', 'allow': [{'driver': 'd', 'model': ' '}]}, 'must not be empty'),
            'row unknown key': ({'mode': 'off', 'allow': [dict(LARGE, effort='high')]}, "unknown key 'effort'"),
            'efforts empty': ({'mode': 'off', 'allow': [dict(LARGE, efforts=[])]}, 'one or more'),
            'efforts twice': ({'mode': 'off', 'allow': [dict(LARGE, efforts=['high', 'high'])]}, 'twice'),
            'efforts not text': ({'mode': 'off', 'allow': [dict(LARGE, efforts=[1])]}, 'non-empty texts'),
            'default not in efforts': ({'mode': 'off', 'allow': [dict(LARGE, defaultEffort='max')]},
                                       "'max' is not one of its efforts"),
            'same row twice': ({'mode': 'off', 'allow': [LARGE, LARGE]}, 'is also in allow[0]'),
            'alias of two rows': ({'mode': 'off', 'allow': [LARGE, dict(DEEP, aliases=['large'])]},
                                  'is also in allow[0]'),
            'alias is another model': ({'mode': 'off', 'allow': [LARGE, dict(DEEP, aliases=['example-large-1'])]},
                                       'is also in allow[0]'),
        }
        for name, (policy, reason) in cases.items():
            with self.subTest(name):
                self.policy(policy)
                self.refused('Agent', {'model': 'large', 'effort': 'high'}, reason, self.policy_path)

    def test_the_same_name_in_two_drivers_is_fine(self):
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'otherDriver', 'model': 'example-large-1'}]})
        self.passed('Agent', {'model': 'large', 'effort': 'high'})

    # ---- modes

    def test_mode_off_lets_everything_through_silently(self):
        self.policy(dict(POLICY, mode='off'))
        self.assertIsNone(self.hook('Agent', {}))
        self.assertIsNone(self.hook('mcp__t3-code__delegate_task', self.delegate(None)))

    def test_advise_refuses_nothing_and_says_what_enforce_would_do(self):
        self.policy(dict(POLICY, mode='advise'))
        note = self.passed('mcp__t3-code__delegate_task', self.delegate({'model': 'other-model'}))
        self.assertIn('enforce would refuse', note)
        self.assertIn("model 'other-model'", note)
        self.assertIn('is for most work', self.passed('Agent', {'model': 'large', 'effort': 'high'}))

    def test_enforce_with_no_rows_refuses_every_pick(self):
        self.policy({'mode': 'enforce', 'allow': []})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'none (every pick is refused)')

    # ---- delegate_task, create_threads, t3_thread_launch, t3_thread_configure

    def test_delegate_task_must_name_model_and_effort(self):
        t = 'mcp__t3-code__delegate_task'
        self.refused(t, self.delegate(None), 'target is missing', 'Allowed rows: claudeAgent example-large-1')
        self.refused(t, self.delegate({'options': {'effort': 'high'}}), 'names no model')
        self.refused(t, self.delegate({'model': 'example-large-1'}), 'names no effort')
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': {}}), 'names no effort')
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': []}), 'names no effort')
        self.refused(t, self.delegate({'model': 'inherit', 'options': {'effort': 'high'}}), 'names no model')

    def test_delegate_task_model_and_effort_must_be_in_a_row(self):
        t = 'mcp__t3-code__delegate_task'
        self.refused(t, self.delegate({'model': 'other', 'options': {'effort': 'high'}}), "model 'other'",
                     'not allowed', 'use for most work')
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'max'}}),
                     "effort 'max' is not allowed")
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': [{'id': 'effort', 'value': 'high'},
                                                                              {'id': 'effort', 'value': 'low'}]}),
                     "effort 'low'")
        self.refused(t, self.delegate({'model': 'example-deep-2', 'options': {'reasoningEffort': 'medium'}}),
                     "effort 'medium'")

    def test_delegate_task_aliases_are_only_for_the_agent_tool(self):
        self.refused('mcp__t3-code__delegate_task', self.delegate({'model': 'large', 'options': {'effort': 'high'}}),
                     "model 'large'")

    def test_delegate_task_driver_kind_must_match_the_row(self):
        t = 'mcp__t3-code__delegate_task'
        self.refused(t, self.delegate(dict(GOOD_TARGET, driverKind='otherDriver')), 'of driver otherDriver')
        self.passed(t, self.delegate(dict(GOOD_TARGET, driverKind='claudeAgent')))

    def test_delegate_task_allowed_picks_pass_with_the_rows_note(self):
        t = 'mcp__t3-code__delegate_task'
        self.assertEqual(self.passed(t, self.delegate(GOOD_TARGET)), 'model-guard: example-large-1 is for most work.')
        self.passed(t, self.delegate({'model': ' example-large-1 ', 'providerInstanceId': 'any-account',
                                      'options': [{'id': 'effort', 'value': 'medium'}]}))
        self.passed(t, self.delegate({'model': 'example-deep-2', 'options': {'thinking_level': 'high'}}))
        self.assertIsNone(self.hook(t, self.delegate({'model': 'example-plain', 'driverKind': 'otherDriver'})))

    def test_options_that_are_not_options_are_refused(self):
        self.refused('mcp__t3-code__delegate_task', self.delegate(dict(GOOD_TARGET, options='high')),
                     'neither an object nor a list')

    def test_create_threads_checks_every_thread(self):
        t = 'mcp__t3-code__create_threads'
        self.refused(t, {'threads': []}, 'threads is missing or empty')
        self.refused(t, {}, 'threads is missing or empty')
        self.refused(t, {'threads': [{'target': GOOD_TARGET}, {'prompt': 'x'}]}, 'threads[1].target is missing')
        self.refused(t, {'threads': [{'target': GOOD_TARGET}, 'x']}, 'threads[1].target is missing')
        self.passed(t, {'threads': [{'target': GOOD_TARGET}, {'target': GOOD_TARGET}]})

    def test_thread_launch_must_name_its_model(self):
        t = 'mcp__t3-code__t3_thread_launch'
        self.refused(t, {'title': 'x'}, 'modelSelection is missing')
        self.refused(t, {'title': 'x', 'modelSelection': {'instanceId': 'a', 'model': 'example-large-1'}},
                     'names no effort')
        self.passed(t, {'title': 'x', 'modelSelection': {'instanceId': 'a', 'model': 'example-large-1',
                                                         'options': [{'id': 'effort', 'value': 'high'}]}})

    def test_thread_configure_is_checked_only_when_it_sets_a_model(self):
        t = 'mcp__t3-code__t3_thread_configure'
        self.assertIsNone(self.hook(t, {'threadId': 'x'}))
        self.refused(t, {'modelSelection': None}, 'modelSelection is missing')
        self.refused(t, {'modelSelection': {'instanceId': 'a', 'model': 'other', 'options': {'effort': 'high'}}},
                     "model 'other'")
        self.passed(t, {'modelSelection': {'provider': 'a', 'model': 'example-large-1', 'options': {'effort': 'high'}}})

    # ---- the native Agent tool

    def test_agent_must_name_an_allowed_model_and_effort(self):
        for tool in ('Agent', 'Task'):
            with self.subTest(tool):
                self.refused(tool, {'prompt': 'x'}, 'names no model')
                self.refused(tool, {'model': 'inherit', 'effort': 'high'}, 'names no model')
                self.refused(tool, {'model': 'large'}, 'names no effort')
                self.refused(tool, {'model': 'large', 'effort': 'max'}, "effort 'max'")
                self.refused(tool, {'model': 'small', 'effort': 'high'}, "model 'small'")
                self.refused(tool, {'model': 'plain'}, "model 'plain'")  # another driver's alias: not Claude
                self.passed(tool, {'model': 'large', 'effort': 'high'})
                self.passed(tool, {'model': 'example-deep-2', 'effort': 'high'})

    def test_agent_fork_is_checked_like_every_other_agent_call(self):
        self.refused('Agent', {'subagent_type': 'fork', 'prompt': 'x'}, 'names no model')
        self.refused('Agent', {'subagent_type': 'fork', 'model': 'large'}, 'names no effort')
        self.passed('Agent', {'subagent_type': 'fork', 'model': 'deep', 'effort': 'high'})

    # ---- the hook's input

    def test_input_that_is_not_a_json_object_refuses(self):
        self.refused(None, raw='not json')
        self.refused(None, raw='[1]')

    # ---- state: runs again, files change, vanish or break between calls

    def test_runs_again_with_the_same_answer_and_stores_nothing(self):
        before = sorted(os.listdir(self.dir))
        call = ('mcp__t3-code__delegate_task', self.delegate({'model': 'other', 'options': {'effort': 'high'}}))
        self.assertEqual(self.hook(*call), self.hook(*call))
        self.assertEqual(sorted(os.listdir(self.dir)), before)

    def test_every_call_reads_the_files_again(self):
        call = ('Agent', {'model': 'deep', 'effort': 'medium'})
        self.refused(*call)
        self.policy({'mode': 'enforce', 'allow': [dict(DEEP, efforts=['medium', 'high'])]})
        self.passed(*call)
        self.policy(dict(POLICY, mode='off'))
        self.assertIsNone(self.hook(*call))
        os.unlink(self.policy_path)
        self.refused(*call, 'not there')
        self.policy('{"mode": "off", "allow": [')  # a half-written file
        self.refused(*call, 'not valid JSON')
        self.policy(POLICY)
        self.machine({'policyFile': os.path.join(self.dir, 'gone.json')})
        self.refused(*call, 'gone.json')

    def test_a_newer_machine_file_refuses(self):
        self.write(self.machine_path, {'version': 2, 'model-guard': {'policyFile': self.policy_path}})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'update the kit')


class Registration(unittest.TestCase):
    """The drift check: is the hook installed in each settings file the machine file lists?"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='model-guard-reg-')
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.path = os.path.join(self.dir, 'settings.json')

    def settings(self, matcher=model_guard.HOOK_MATCHER, command=model_guard.HOOK_COMMAND, **extra):
        entry = {'matcher': matcher, 'hooks': [{'type': 'command', 'command': command, 'timeout': 10}]}
        with open(self.path, 'w') as f:
            json.dump(dict({'hooks': {'PreToolUse': [entry]}}, **extra), f)
        return model_guard.registration_problems(self.path)

    def test_the_documented_entry_has_no_problems(self):
        self.assertEqual(self.settings(), [])
        self.assertEqual(self.settings(matcher=''), [])

    def test_missing_hook_or_file_is_a_problem(self):
        self.assertIn('cannot be read', model_guard.registration_problems(self.path)[0])
        self.assertIn('no PreToolUse hook runs', self.settings(command='/usr/local/bin/other')[0])
        self.assertIn('disableAllHooks', self.settings(disableAllHooks=True)[0])

    def test_a_matcher_that_misses_a_tool_is_a_problem(self):
        self.assertIn('misses Task, mcp__t3-code__delegate_task', ' '.join(self.settings(matcher='Agent')))
        self.assertIn('t3_thread_configure', ' '.join(self.settings(matcher='Agent|Task|mcp__.*__delegate_task')))

    def test_a_command_that_fails_open_is_a_problem(self):
        self.assertIn('exit 2', self.settings(command=model_guard.HOOK_LINK)[0])

    def test_the_matcher_covers_exactly_the_checked_tools(self):
        cover = lambda name: model_guard._covers(model_guard.HOOK_MATCHER, name)  # noqa: E731
        for name in ('Agent', 'Task', 'mcp__t3-code__create_threads', 'mcp__t3__t3_thread_launch'):
            self.assertTrue(cover(name), name)
        for name in ('Bash', 'TaskStop', 'SubAgent', 'mcp__t3-code__task_status', 'mcp__t3-code__delegate_tasks'):
            self.assertFalse(cover(name), name)


class Contract(unittest.TestCase):
    def test_effort_ids_match_the_servers_list(self):
        """The hook finds the effort under the same option ids as T3's usage pacing gate."""
        path = os.path.join(REPO, 'apps', 'server', 'src', 'usage', 'UsagePacingGate.ts')
        with open(path, encoding='utf-8') as f:
            block = re.search(r'EFFORT_OPTION_IDS[^=]*= new Set\(\[(.*?)\]\)', f.read(), re.S)
        self.assertIsNotNone(block, f'EFFORT_OPTION_IDS not found in {path}: keep the hook\'s list in step')
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', block.group(1))), sorted(model_guard.EFFORT_OPTION_IDS))

    def test_the_example_policy_is_valid(self):
        with open(os.path.join(KIT, 'model-policy.example.json'), encoding='utf-8') as f:
            policy = model_guard.parse_policy(f.read(), 'model-policy.example.json')
        self.assertTrue(policy['allow'])


class Command(unittest.TestCase):
    def test_a_broken_guard_blocks_with_exit_2(self):
        """A guard that cannot run blocks the call (exit 2), it never lets it through."""
        with tempfile.TemporaryDirectory() as d:
            os.mkdir(os.path.join(d, 'bin'))
            shutil.copy(os.path.join(KIT, 'bin', 'model-guard'), os.path.join(d, 'bin'))  # no lib next to it
            r = subprocess.run([sys.executable, '-I', '-B', os.path.join(d, 'bin', 'model-guard')],
                               input='{"tool_name": "Agent"}', capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn('model-guard refused', r.stderr)

    def test_help(self):
        r = subprocess.run([sys.executable, '-I', '-B', os.path.join(KIT, 'bin', 'model-guard'), '--help'],
                           capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
        self.assertEqual(r.returncode, 0)
        self.assertIn('--self-check', r.stdout)


# rule: (text in lib/model_guard.py, what it becomes, the test that must then fail)
MUTATIONS = {
    'broken policy fails closed': ("except PolicyError as e:\n        return _out('deny',",
                                   "except PolicyError as e:\n        return _out(None,",
                                   'test_missing_policy_file_refuses_and_names_it'),
    'enforce refuses': ("if policy['mode'] == 'enforce':", "if False:",
                        'test_delegate_task_model_and_effort_must_be_in_a_row'),
    'model must be in a row': ('    if not found:\n', '    if False:\n',
                               'test_delegate_task_model_and_effort_must_be_in_a_row'),
    'inherit is no model': (" or model == 'inherit':", ':', 'test_agent_must_name_an_allowed_model_and_effort'),
    'effort must be named': ('        if not efforts:\n', '        if False:\n',
                             'test_delegate_task_must_name_model_and_effort'),
    'effort must be in the row': ('        if bad:\n', '        if False:\n',
                                  'test_delegate_task_model_and_effort_must_be_in_a_row'),
    'driver must match': ("(driver is None or r['driver'] == driver)", 'True',
                          'test_delegate_task_driver_kind_must_match_the_row'),
    'aliases only for Agent': ("(agent and model in (r['aliases'] or ()))", "(model in (r['aliases'] or ()))",
                               'test_delegate_task_aliases_are_only_for_the_agent_tool'),
    'Agent is Claude only': ('CLAUDE_DRIVER, rows,', 'None, rows,',
                             'test_agent_must_name_an_allowed_model_and_effort'),
    'every thread checked': ('        if not isinstance(threads, list) or not threads:\n',
                             '        if False:\n', 'test_create_threads_checks_every_thread'),
    'target must be there': ("        if not isinstance(value, dict):\n            problems.append",
                             "        if not isinstance(value, dict):\n            return\n            problems.append",
                             'test_thread_launch_must_name_its_model'),
    'configure with a model is checked': ("    elif kind == 't3_thread_configure' and 'modelSelection' in args:",
                                          "    elif False:",
                                          'test_thread_configure_is_checked_only_when_it_sets_a_model'),
    'options must be options': ("        problems.append(f'{where}.options is neither", "        (f'",
                                'test_options_that_are_not_options_are_refused'),
    'bad input refused': ("        out = _out('deny', reason)", '        out = None',
                          'test_input_that_is_not_a_json_object_refuses'),
    'mode must be known': ("    if policy['mode'] not in MODES:", '    if False:',
                           'test_an_invalid_policy_refuses_in_every_mode_and_names_the_reason'),
    'default effort in efforts': ("if row['defaultEffort'] is not None and", 'if False and',
                                  'test_an_invalid_policy_refuses_in_every_mode_and_names_the_reason'),
    'a name means one row': ("            if (row['driver'], name) in names:", '            if False:',
                             'test_an_invalid_policy_refuses_in_every_mode_and_names_the_reason'),
    'no key twice': ('json.loads(text, object_pairs_hook=_no_duplicates)', 'json.loads(text)',
                     'test_an_invalid_policy_refuses_in_every_mode_and_names_the_reason'),
    'policy file is protected': ('machine_file.read_protected_text(cfg[\'policyFile\'], owner_uid)',
                                 "open(cfg['policyFile']).read()",
                                 'test_policy_file_others_can_write_or_a_link_refuses'),
}
REG_MUTATIONS = {
    'matcher covers every tool': ('    if missed:\n', '    if False:\n',
                                  'test_a_matcher_that_misses_a_tool_is_a_problem'),
    'command blocks when missing': ("    if not any(str(h.get('command', '')).strip() == HOOK_COMMAND",
                                    "    if False and any(str(h.get('command', '')).strip() == HOOK_COMMAND",
                                    'test_a_command_that_fails_open_is_a_problem'),
    'disableAllHooks': ("    if data.get('disableAllHooks') is True:", '    if False:',
                        'test_missing_hook_or_file_is_a_problem'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(LIB, encoding='utf-8') as f:
            source = f.read()
        for base, table in ((Guard, MUTATIONS), (Registration, REG_MUTATIONS)):
            for rule, (text, broken, test) in table.items():
                with self.subTest(rule=rule):
                    self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                    broken_mod = load(source.replace(text, broken))
                    if base is Guard:
                        cls = type('Broken', (Guard,), {'m': broken_mod})
                    else:
                        cls = type('Broken', (Registration,), {})
                        old, model_guard.registration_problems = (model_guard.registration_problems,
                                                                  broken_mod.registration_problems)
                    result = unittest.TestResult()
                    try:
                        cls(test).run(result)
                    finally:
                        if base is Registration:
                            model_guard.registration_problems = old
                    self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
