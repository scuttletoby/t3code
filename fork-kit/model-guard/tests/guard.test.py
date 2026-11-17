"""The model-guard hook against every rule of README.md: each refuse path, the modes, the policy file's rules and
what happens when the files change, vanish or break between calls. No root: the files belong to the current user,
who stands in for root. RedCheck breaks each refuse rule in a copy of lib/model_guard.py and shows its test then
fails.

  python3 -I -B fork-kit/model-guard/tests/guard.test.py
"""
import io, json, os, re, shutil, subprocess, sys, tempfile, types, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

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
        for tool in ('Bash', 'Edit', 'mcp__t3-code__run_scheduled_task_now', 'mcp__t3-code__task_status', 'TaskStop'):
            self.assertIsNone(self.hook(tool, {'model': 'anything'}), tool)

    def test_every_t3_tool_under_any_server_name_is_checked(self):
        for name in ('mcp__t3-code__delegate_task', 'mcp__t3__delegate_task', 'mcp__a__b__delegate_task'):
            self.refused(name, self.delegate(None), 'target is missing')

    def test_every_checked_tool_comes_from_the_one_list(self):
        for name in self.m.CHECKED_TOOLS:
            self.assertIsNotNone(self.m.tool_kind(name), name)
            self.assertTrue(self.m._covers(self.m.HOOK_MATCHER, name), name)
        for name in ('Workflow', 'mcp__t3-code__t3_thread_send', 'mcp__t3-code__t3_thread_transfer',
                     'mcp__t3-code__t3_thread_send_attachments', 'mcp__t3-code__schedule_task'):
            self.assertIn(name, self.m.CHECKED_TOOLS)

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

    def test_a_row_without_efforts_lets_no_effort_through(self):
        t = 'mcp__t3-code__delegate_task'
        plain = {'model': 'example-plain', 'driverKind': 'otherDriver'}
        self.passed(t, self.delegate(plain))
        for effort in ('max', 'high', 'low'):
            self.refused(t, self.delegate(dict(plain, options={'effort': effort})), f"effort {effort!r}",
                         'its row lists no efforts')
        self.refused(t, self.delegate(dict(plain, options={'ultracode': True})), "effort 'ultracode'")

    def test_without_a_driver_every_row_of_the_model_must_accept_it(self):
        t = 'mcp__t3-code__delegate_task'
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'otherDriver', 'model': 'example-large-1',
                                                          'efforts': ['medium']}]})
        self.refused(t, self.delegate(GOOD_TARGET), "effort 'high' is not allowed")
        self.passed(t, self.delegate(dict(GOOD_TARGET, driverKind='claudeAgent')))
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'medium'}}))

    def test_ultracode_is_checked_as_the_effort_that_runs(self):
        t = 'mcp__t3-code__delegate_task'
        xhigh = {'mode': 'enforce', 'allow': [dict(LARGE, efforts=['high', 'xhigh'])]}
        self.policy(xhigh)
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'xhigh'}}))
        for options in ({'effort': 'xhigh', 'ultracode': True},
                        [{'id': 'effort', 'value': 'xhigh'}, {'id': 'ultracode', 'value': True}],
                        {'effort': 'ultracode'},                       # the old effort value: xhigh, ultracode on
                        {'effort': 'high', 'ultracode': 'yes'}):
            with self.subTest(options=options):
                self.refused(t, self.delegate({'model': 'example-large-1', 'options': options}),
                             "effort 'ultracode' is not allowed")
        # the old value with ultracode given as off runs plain xhigh, as the server runs it
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'ultracode',
                                                                              'ultracode': False}}))
        self.policy({'mode': 'enforce', 'allow': [dict(LARGE, efforts=['high', 'ultracode'])]})
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'xhigh', 'ultracode': True}}))
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'ultracode'}}))
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'xhigh'}}),
                     "effort 'xhigh'")

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

    # ---- tools that run a thread's existing model

    def test_thread_transfer_fork_is_refused(self):
        t = 'mcp__t3-code__t3_thread_transfer'
        self.refused(t, {'action': 'fork'}, 'fork starts a thread on the source thread\'s model',
                     't3_thread_launch and a modelSelection')
        self.refused(t, {'action': 'fork', 'threadId': 'x', 'title': 'y'}, 'fork starts')
        self.assertIsNone(self.hook(t, {'action': 'status'}))
        self.assertIsNone(self.hook(t, {'action': 'merge_back', 'targetThreadId': 'x'}))
        self.policy(dict(POLICY, mode='advise'))
        self.assertIn('enforce would refuse', self.passed(t, {'action': 'fork'}))

    def test_send_and_schedule_take_no_model(self):
        send = 'mcp__t3-code__t3_thread_send'
        plain = {'threadId': 'x', 'message': 'm', 'mode': 'steer', 'freshContext': True, 'environmentId': 'e',
                 'clientRequestId': 'c'}
        self.assertIsNone(self.hook(send, plain))
        for key, value in (('modelSelection', GOOD_TARGET), ('model', 'large'), ('effort', 'max'),
                           ('options', {'effort': 'max'}), ('target', GOOD_TARGET), ('providerInstanceId', 'a'),
                           ('reasoningEffort', 'max'), ('ultracode', True), ('defaultModel', 'x')):
            with self.subTest(key=key):
                self.refused(send, dict(plain, **{key: value}), f'{key}: t3_thread_send takes no model')
        self.refused('mcp__t3-code__t3_thread_send_attachments', {'threadId': 'x', 'attachments': [],
                                                                  'model': 'x'}, 'takes no model')
        schedule = {'prompt': 'p', 'schedule': {'type': 'once', 'runAt': 'x'}, 'bindToCurrentThread': False,
                    'freshSession': True, 'projectId': 'p', 'title': 't', 'enabled': True}
        self.assertIsNone(self.hook('mcp__t3-code__schedule_task', schedule))
        self.refused('mcp__t3-code__schedule_task', dict(schedule, modelSelection=GOOD_TARGET), 'takes no model')
        self.refused('mcp__t3-code__t3_thread_transfer', {'action': 'status', 'model': 'x'}, 'takes no model')

    # ---- the Workflow tool: a strict reading of its script

    def workflow(self, script, **extra):
        return dict({'script': script}, **extra)

    def test_workflow_every_agent_call_is_checked(self):
        good = '''export const meta = {name: 'x', description: 'y'}
// agent("in a comment", {}) is not a call
const a = await agent(`plain ${1 + 2} prompt`, {label: 'a', model: 'large', effort: "high", schema: {type: 'object'}})
const b = await pipeline(items, x => agent('p ' + x, {model: 'example-deep-2', effort: 'high',}), r => r.y[0] / 2)
const c = log("agent(") || items.map(i => i.agent)
'''
        self.assertIn('is for most work', self.passed('Workflow', self.workflow(good)))
        for script, why in (
                ("agent('x', {model: 'small', effort: 'high'})", "model 'small'"),
                ("agent('x', {model: 'large'})", 'names no effort'),
                ("agent('x', {effort: 'high'})", 'names no model'),
                ("agent('x', {model: 'large', effort: 'max'})", "effort 'max'"),
                ("agent('x', {model: 'plain', effort: 'high'})", "model 'plain'"),
                (good + "\nagent('x', {model: 'large', effort: 'medium'})\nagent('x', {model: 'deep', effort: 'low'})",
                 "Workflow agent() call 4 (line 8): effort 'low'")):
            with self.subTest(script=script):
                self.refused('Workflow', self.workflow(script), why)

    def test_workflow_that_cannot_be_read_is_refused(self):
        ok = "{model: 'large', effort: 'high'}"
        cases = {
            'no script': ({}, 'no inline "script"'),
            'empty script': (self.workflow('  '), 'no inline "script"'),
            'script path': (self.workflow(f"agent('x', {ok})", scriptPath='/x.js'), 'scriptPath runs a script'),
            'saved workflow': ({'name': 'review'}, 'name runs a script'),
            'too large': (self.workflow('//' + 'x' * model_guard.WF_MAX_BYTES), 'larger than'),
        }
        tricks = {
            'options in a variable': "const o = {model: 'large', effort: 'high'}; agent('x', o)",
            'spread options': f"agent('x', {{...o, model: 'large', effort: 'high'}})",
            'computed key': "agent('x', {['model']: 'small', model: 'large', effort: 'high'})",
            'computed key elsewhere': f"const o = {{a: 1, ['ag' + 'ent']: 2}}; agent('x', {ok})",
            'key given twice': "agent('x', {model: 'large', effort: 'high', model: 'small'})",
            'model from code': "agent('x', {model: m, effort: 'high'})",
            'model from a template': "agent('x', {model: `${m}`, effort: 'high'})",
            'escaped text': "agent('x', {model: 'l\\u0061rge', effort: 'high'})",
            'escaped key': "agent('x', {'\\u006dodel': 'small', model: 'large', effort: 'high'})",
            'agent renamed': "const a = agent; a('x', {model: 'small', effort: 'high'})",
            'agent shorthand': "const o = {agent}; o.agent('x', {})",
            'three arguments': f"agent('x', {ok}, more)",
            'one argument': "agent('x')",
            'options plus more': f"agent('x', {ok} || other)",
            'computed member': f"globalAgent['ag' + 'ent']('x', {ok})",
            'eval': f"eval(\"agent('x', {{}})\")",
            'Function': "new Function('return agent')()('x', {})",
            'constructor': "''.constructor.constructor('x')()",
            'globalThis': "globalThis.agent('x', {})",
            'another workflow': "await workflow('other')",
            'proto key': f"agent('x', {{__proto__: o, model: 'large', effort: 'high'}})",
            'unclosed string': "agent('x, {})",
            'non-ascii outside strings': "const \u0430gent = 1",
            'html comment': f"<!-- x\nagent('x', {ok})",
            'ambiguous slash': f"x = {{}} / agent('x', {ok}) /i",
            'unclosed bracket': f"agent('x', {ok}",
            'method key': "agent('x', {model() { return 'small' }, effort: 'high'})",
        }
        for name, script in tricks.items():
            cases[name] = (self.workflow(script), 'the Workflow script cannot be checked')
        for name, (args, why) in cases.items():
            with self.subTest(name):
                self.refused('Workflow', args, why)

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


class ManagedSettings(unittest.TestCase):
    """The drift check: do Claude Code's managed settings run the hook, as one entry of its own?"""
    m = model_guard
    ONLY = ('managed-settings.json', {'allowManagedHooksOnly': True})

    def problems(self, *entries, allow_other_hooks=False, first=ONLY):
        files = [first] if first else []
        files.append(('managed-settings.d/model-guard.json', {'hooks': {'PreToolUse': list(entries)}}))
        return self.m.settings_problems(files, allow_other_hooks)

    def entry(self, matcher=model_guard.HOOK_MATCHER, command=model_guard.HOOK_COMMAND, **extra):
        return dict({'matcher': matcher, 'hooks': [{'type': 'command', 'command': command, 'onFailure': 'block'}]},
                    **extra)

    def test_the_installed_drop_in_has_no_problems(self):
        installed = json.loads(self.m.drop_in_text())
        self.assertEqual(self.m.settings_problems([self.ONLY, ('d/model-guard.json', installed)], False), [])
        self.assertEqual(installed['hooks']['PreToolUse'], [self.m.hook_entry()])
        self.assertEqual(installed['hooks']['PreToolUse'][0]['hooks'][0]['onFailure'], 'block')
        self.assertEqual(self.problems(self.entry(matcher='')), [])
        self.assertEqual(self.problems(self.entry(matcher='*')), [])
        self.assertEqual(self.problems(self.entry(matcher=','.join(self.m.CHECKED_TOOLS))), [])

    def test_the_security_lens_decoy_fails(self):
        """A guard entry on Agent only, and an any-tool entry whose command only mentions the guard."""
        p = self.problems(self.entry(matcher='Agent'),
                          self.entry(matcher='*', command='true # ' + self.m.HOOK_LINK))
        self.assertTrue(any('no managed PreToolUse entry runs the guard' in x for x in p), p)
        self.assertTrue(any('entry 0 runs the guard but is not its entry: its matcher misses Task' in x for x in p), p)
        self.assertTrue(any('entry 1 runs the guard but is not its entry: its hooks are not exactly' in x for x in p),
                        p)

    def test_the_state_lens_split_entries_fail(self):
        """The full matcher with a command that fails open, and the blocking command on another tool."""
        p = self.problems(self.entry(command=self.m.HOOK_LINK), self.entry(matcher='Bash'))
        self.assertTrue(any('no managed PreToolUse entry runs the guard' in x for x in p), p)
        self.assertTrue(any('entry 0' in x and 'hooks are not exactly' in x for x in p), p)
        self.assertTrue(any('entry 1' in x and 'matcher misses Agent' in x for x in p), p)

    def test_an_entry_with_anything_more_fails(self):
        hook = dict(self.m.hook_entry()['hooks'][0])
        changed = lambda **kv: {'matcher': model_guard.HOOK_MATCHER, 'hooks': [dict(hook, **kv)]}  # noqa: E731
        no_block = {k: v for k, v in hook.items() if k != 'onFailure'}
        for name, entry in (
                ('timeout', changed(timeout=1)),
                ('async', changed(**{'async': True})),
                ('fails open', changed(onFailure='continue')),
                ('no onFailure', {'matcher': model_guard.HOOK_MATCHER, 'hooks': [no_block]}),
                ('two hooks', {'matcher': model_guard.HOOK_MATCHER, 'hooks': [hook] * 2}),
                ('extra key', self.entry(x=1)),
                ('no matcher', {'hooks': [hook]}),
                ('not an object', 'x')):
            with self.subTest(name):
                p = self.problems(entry)
                self.assertTrue(any('no managed PreToolUse entry runs the guard' in x for x in p), p)

    def test_a_matcher_that_misses_a_tool_fails(self):
        for matcher, missed in (('Agent|Task', 'Workflow'), ('Agent|Task|Workflow|mcp__.*__delegate_task', 'create'),
                                ('(', 'Agent'), (r'^(Agent|Task|mcp__.+__(delegate_task|create_threads))$', 'Workflow'),
                                ('Agent,Task,Workflow', 'mcp__t3-code__t3_thread_send')):
            with self.subTest(matcher):
                self.assertIn(missed, ' '.join(self.problems(self.entry(matcher=matcher))))

    def test_the_matcher_covers_exactly_the_checked_tools(self):
        cover = lambda name: self.m._covers(self.m.HOOK_MATCHER, name)  # noqa: E731
        for name in ('Agent', 'Task', 'Workflow', 'mcp__t3-code__create_threads', 'mcp__t3__t3_thread_launch',
                     'mcp__t3-code__t3_thread_send', 'mcp__t3-code__t3_thread_transfer'):
            self.assertTrue(cover(name), name)
        for name in ('Bash', 'TaskStop', 'SubAgent', 'mcp__t3-code__task_status', 'mcp__t3-code__delegate_tasks',
                     'mcp__t3-code__run_scheduled_task_now', 'Workflows'):
            self.assertFalse(cover(name), name)

    def test_hooks_switched_off_fail(self):
        for value in (True, 'true', 1, 'yes'):
            with self.subTest(value=value):
                p = self.m.settings_problems([('main', {'allowManagedHooksOnly': True, 'disableAllHooks': value}),
                                              ('d', json.loads(self.m.drop_in_text()))], False)
                self.assertEqual(p, ['main: disableAllHooks is set, so no hook runs'])
        for value in (False, 'false'):
            p = self.m.settings_problems([('main', {'allowManagedHooksOnly': True, 'disableAllHooks': value}),
                                          ('d', json.loads(self.m.drop_in_text()))], False)
            self.assertEqual(p, [])

    def test_only_managed_hooks_unless_the_machine_file_allows_others(self):
        self.assertIn('allowManagedHooksOnly is not true', ' '.join(self.problems(self.entry(), first=None)))
        self.assertEqual(self.problems(self.entry(), first=None, allow_other_hooks=True), [])
        later_off = self.m.settings_problems([self.ONLY, ('d/a.json', {'allowManagedHooksOnly': False}),
                                              ('d/model-guard.json', json.loads(self.m.drop_in_text()))], False)
        self.assertIn('allowManagedHooksOnly is not true', ' '.join(later_off))
        later_on = self.m.settings_problems([('main', {'allowManagedHooksOnly': False}),
                                             ('d/z.json', {'allowManagedHooksOnly': True}),
                                             ('d/model-guard.json', json.loads(self.m.drop_in_text()))], False)
        self.assertEqual(later_on, [])

    def test_a_file_that_is_not_an_object_fails(self):
        p = self.m.settings_problems([('main', [1]), ('d', json.loads(self.m.drop_in_text()))], True)
        self.assertEqual(p, ['main: not a JSON object'])

    def test_managed_files_are_read_in_claude_codes_order_and_protected(self):
        d = os.path.realpath(tempfile.mkdtemp(prefix='model-guard-managed-'))
        self.addCleanup(shutil.rmtree, d, True)
        os.chmod(d, 0o755)
        uid = os.getuid()
        self.assertEqual(self.m.managed_files(d, uid), [])            # nothing there: no files, no error
        os.mkdir(os.path.join(d, 'managed-settings.d'), 0o755)
        for name, text in (('b.json', '{"b": 1}'), ('a.json', '{"a": 1}'), ('.hidden.json', '{'), ('c.txt', '{')):
            with open(os.path.join(d, 'managed-settings.d', name), 'w') as f:
                f.write(text)
            os.chmod(os.path.join(d, 'managed-settings.d', name), 0o644)
        files = self.m.managed_files(d, uid)
        self.assertEqual([(os.path.basename(p), data, err) for p, data, err in files],
                         [('a.json', {'a': 1}, None), ('b.json', {'b': 1}, None)])
        with open(os.path.join(d, 'managed-settings.json'), 'w') as f:
            f.write('{"main": 1}')
        os.chmod(os.path.join(d, 'managed-settings.json'), 0o644)
        self.assertEqual(self.m.managed_files(d, uid)[0][1:], ({'main': 1}, None))
        os.chmod(os.path.join(d, 'managed-settings.d', 'a.json'), 0o666)
        self.assertIn('writable by group or others', self.m.managed_files(d, uid)[1][2])
        os.chmod(os.path.join(d, 'managed-settings.d', 'a.json'), 0o644)
        self.assertIn('owned by uid', str(self.m.managed_files(d, uid + 1)[0][2]))
        with open(os.path.join(d, 'managed-settings.d', 'b.json'), 'w') as f:
            f.write('{')
        self.assertIn('not valid JSON', self.m.managed_files(d, uid)[2][2])
        shutil.rmtree(os.path.join(d, 'managed-settings.d'))
        with open(os.path.join(d, 'managed-settings.d'), 'w') as f:
            f.write('')
        self.assertIn('cannot be listed', self.m.managed_files(d, uid)[0][2])

    def test_managed_folder_per_platform(self):
        self.assertEqual(self.m.managed_dir('linux'), '/etc/claude-code')
        self.assertEqual(self.m.managed_dir('darwin'), '/Library/Application Support/ClaudeCode')
        self.assertEqual(self.m.managed_dir('win32'), 'C:\\Program Files\\ClaudeCode')
        with self.assertRaises(self.m.PolicyError):
            self.m.managed_dir('plan9')


class Contract(unittest.TestCase):
    def test_effort_ids_match_the_servers_list(self):
        """The hook finds the effort under the same option ids as T3's usage pacing gate."""
        path = os.path.join(REPO, 'apps', 'server', 'src', 'usage', 'UsagePacingGate.ts')
        with open(path, encoding='utf-8') as f:
            block = re.search(r'EFFORT_OPTION_IDS[^=]*= new Set\(\[(.*?)\]\)', f.read(), re.S)
        self.assertIsNotNone(block, f'EFFORT_OPTION_IDS not found in {path}: keep the hook\'s list in step')
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', block.group(1))), sorted(model_guard.EFFORT_OPTION_IDS))

    def test_ultracode_maps_like_the_server(self):
        """The old effort value "ultracode" runs at xhigh with the option "ultracode" on, as T3's shared model code
        maps it (packages/shared/src/model.ts withLegacyUltracodeEffort)."""
        path = os.path.join(REPO, 'packages', 'shared', 'src', 'model.ts')
        with open(path, encoding='utf-8') as f:
            body = re.search(r'export function withLegacyUltracodeEffort\b.*?\n}\n', f.read(), re.S)
        self.assertIsNotNone(body, f'withLegacyUltracodeEffort not found in {path}: keep the hook in step')
        body = body.group(0)
        legacy = re.search(r'selection\.id === "effort" && selection\.value === "([^"]+)"\s*\?\s*'
                           r'\{ id: "effort", value: "([^"]+)" \}', body)
        option = re.search(r'getRawSelectionValueById\(selections, "([^"]+)"\) === undefined\s*\?\s*'
                           r'\[\{ id: "([^"]+)", value: true \}\]', body)
        self.assertIsNotNone(legacy, 'the effort mapping changed: keep the hook in step')
        self.assertIsNotNone(option, 'the ultracode option changed: keep the hook in step')
        self.assertEqual(legacy.groups(), (model_guard.ULTRACODE_EFFORT, model_guard.ULTRACODE_RUNS_AT))
        self.assertEqual(option.groups(), (model_guard.ULTRACODE_OPTION_ID, model_guard.ULTRACODE_OPTION_ID))

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

    def test_unknown_arguments_block_with_exit_2(self):
        for argv in (['--selfcheck'], ['--self-check', 'x'], ['-x']):
            r = subprocess.run([sys.executable, '-I', '-B', os.path.join(KIT, 'bin', 'model-guard')] + argv,
                               input='{}', capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 2, argv)
            self.assertIn('unknown arguments', r.stderr)

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
    'Agent is Claude only': ('else [effort], CLAUDE_DRIVER, rows,\n', 'else [effort], None, rows,\n',
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
    'Workflow is checked': ("CLAUDE_TOOLS = {'Agent': 'agent', 'Task': 'agent', 'Workflow': 'workflow'}",
                            "CLAUDE_TOOLS = {'Agent': 'agent', 'Task': 'agent'}",
                            'test_workflow_every_agent_call_is_checked'),
    'Workflow agent() calls checked': ('                check_pick(where, model,', '                (where, model,',
                                       'test_workflow_every_agent_call_is_checked'),
    'Workflow it cannot read is refused': ('    except ScriptError as e:\n        problems.append(',
                                           '    except ScriptError as e:\n        (',
                                           'test_workflow_that_cannot_be_read_is_refused'),
    'Workflow path or name refused': ("            problems.append(f'Workflow {key} runs",
                                      "            (f'Workflow {key} runs",
                                      'test_workflow_that_cannot_be_read_is_refused'),
    'Workflow computed key refused': ("raise ScriptError(f'line {line}: a computed key')",
                                      "pass", 'test_workflow_that_cannot_be_read_is_refused'),
    'Workflow forbidden names': ("            if kind == 'name' and text in WF_FORBIDDEN:",
                                 '            if False:', 'test_workflow_that_cannot_be_read_is_refused'),
    'transfer fork refused': ("    if kind == 't3_thread_transfer' and args.get('action') == 'fork':",
                              '    if False:', 'test_thread_transfer_fork_is_refused'),
    'send and schedule take no model': ('            if isinstance(key, str) and MODEL_FIELD.fullmatch(key):',
                                        '            if False:', 'test_send_and_schedule_take_no_model'),
    'model fields found by name': ("re.compile(r'.*(model|effort|", "re.compile(r'(model|effort|",
                                   'test_send_and_schedule_take_no_model'),
    'ultracode runs as ultracode': ('    if any(v not in (False, None) for v in ultracode):', '    if False:',
                                    'test_ultracode_is_checked_as_the_effort_that_runs'),
    'old ultracode effort is xhigh': ('    if ULTRACODE_EFFORT in efforts:', '    if False:',
                                      'test_ultracode_is_checked_as_the_effort_that_runs'),
    'a row without efforts names none': ("            if efforts:\n                problems.append(f'{where}: effort",
                                         "            if False:\n                problems.append(f'{where}: effort",
                                         'test_a_row_without_efforts_lets_no_effort_through'),
    'no driver: every row accepts': ('    for row in found:\n', '    for row in found[:1]:\n',
                                     'test_without_a_driver_every_row_of_the_model_must_accept_it'),
}
SETTINGS_MUTATIONS = {
    'matcher covers every tool': ('    if missed:\n', '    if False:\n', 'test_a_matcher_that_misses_a_tool_fails'),
    'a bad regex covers nothing': ('    except re.error:\n        return False',
                                   '    except re.error:\n        return True',
                                   'test_a_matcher_that_misses_a_tool_fails'),
    'exactly the guard command': ("    if entry['hooks'] != hook_entry()['hooks']:",
                                  '    if False:', 'test_the_state_lens_split_entries_fail'),
    'nothing more in the entry': ("set(entry) != {'matcher', 'hooks'}", "'hooks' not in entry",
                                  'test_an_entry_with_anything_more_fails'),
    'a guard entry is needed': ('    if not good:\n', '    if False:\n', 'test_the_security_lens_decoy_fails'),
    'disableAllHooks': ("        if 'disableAllHooks' in data and", '        if False and',
                        'test_hooks_switched_off_fail'),
    'only managed hooks': ('    if only_managed is not True and not allow_other_hooks:', '    if False:',
                           'test_only_managed_hooks_unless_the_machine_file_allows_others'),
    'the later file wins': ("            only_managed = data['allowManagedHooksOnly']",
                            "            only_managed = only_managed or data['allowManagedHooksOnly']",
                            'test_only_managed_hooks_unless_the_machine_file_allows_others'),
    'not an object': ("            problems.append(f'{path}: not a JSON object')", '            pass',
                      'test_a_file_that_is_not_an_object_fails'),
    'managed files are protected': ('json.loads(machine_file.read_protected_text(path, owner_uid))',
                                    'json.loads(open(path).read())',
                                    'test_managed_files_are_read_in_claude_codes_order_and_protected'),
    'managed files in order': ('for f in sorted(os.listdir(drop_ins))',
                               'for f in sorted(os.listdir(drop_ins), reverse=True)',
                               'test_managed_files_are_read_in_claude_codes_order_and_protected'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(LIB, encoding='utf-8') as f:
            source = f.read()
        for base, table in ((Guard, MUTATIONS), (ManagedSettings, SETTINGS_MUTATIONS)):
            for rule, (text, broken, test) in table.items():
                with self.subTest(rule=rule):
                    self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                    cls = type('Broken', (base,), {'m': load(source.replace(text, broken))})
                    result = unittest.TestResult()
                    cls(test).run(result)
                    self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
