"""The model-guard hook against every rule of README.md: each refuse path, the modes, the policy file's rules and
what happens when the files change, vanish or break between calls. No root: the files belong to the current user,
who stands in for root. RedCheck breaks each refuse rule in a copy of lib/model_guard.py and shows its test then
fails.

  python3 -I -B fork-kit/model-guard/tests/guard.test.py
"""
import contextlib, io, json, os, re, shutil, signal, subprocess, sys, tempfile, types, unittest
from unittest import mock

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
# ultracode: false is named because T3 may turn ultracode on for a Claude model when the call leaves it out
GOOD_TARGET = {'model': 'example-large-1', 'options': {'effort': 'high', 'ultracode': False}}
NO_ULTRA = {'id': 'ultracode', 'value': False}
# an ACP agent: its reasoning control is its own option id (packages/provider-acp sessionConfig.ts, role "reasoning")
AGENT = {'driver': 'acpDriver', 'model': 'example-agent', 'efforts': ['low', 'medium'],
         'effortOptionIds': ['thought_level'], 'otherOptionIds': ['mode']}


@contextlib.contextmanager
def deadline(seconds):
    """Fail with TimeoutError instead of hanging (a read that waits on a pipe, in a broken copy of the lib)."""
    def stop(*_):
        raise TimeoutError(f'still running after {seconds} seconds')
    old = signal.signal(signal.SIGALRM, stop)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


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
        self.t3_path = os.path.join(self.dir, 't3-policy.json')   # T3's managed policy: none until a test writes it
        self.records = []   # the owner's record (the system log outside tests)

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
        self.assertEqual(self.m.run_hook(io.StringIO(text), out, self.machine_path, os.getuid(),
                                           record=self.records.append, t3_policy=self.t3_path), 0)
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
            self.assertTrue(re.search(self.m.HOOK_MATCHER, name), name)
        for name in ('Workflow', 'RemoteTrigger', 'mcp__t3-code__t3_project_create', 'mcp__t3-code__t3_project_update',
                     'mcp__t3-code__t3_thread_send', 'mcp__t3-code__t3_thread_transfer',
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

    # ---- the tools the T3 server checks, while T3's managed policy locks its policy

    def t3_lock(self, mode='enforce', rows=None, locked=True, file_mode=0o644):
        """T3's managed policy, locking modelPolicy at `mode` with `rows` (rows within the guard's by default)."""
        rows = rows if rows is not None else [{'driver': 'claudeAgent', 'model': 'example-large-1',
                                               'efforts': ['high']}]
        self.write(self.t3_path, {'schemaVersion': 1, 'organization': 'Owner', 'settings': {
            'modelPolicy': {'value': {'mode': mode, 'allow': rows}, 'locked': locked}}})
        os.chmod(self.t3_path, file_mode)

    OUTSIDE = {'task': 'x', 'target': {'model': 'other', 'options': {'effort': 'high'}}}

    def test_a_server_route_is_left_to_t3_while_its_managed_policy_locks_it(self):
        """The T3 server checks these routes against the locked policy, which an agent cannot change: the guard's
        copy of the check would only repeat it. Every other checked tool stays the guard's."""
        self.t3_lock()
        for tool in ('delegate_task', 'create_threads', 't3_thread_launch', 't3_thread_configure',
                     't3_thread_transfer'):
            self.assertIsNone(self.hook(f'mcp__t3-code__{tool}', self.OUTSIDE), tool)
        self.passed('mcp__t3-code__delegate_task', self.delegate(GOOD_TARGET))
        self.refused('Agent', {'model': 'other', 'effort': 'high'}, "model 'other'")
        self.refused('mcp__t3-code__t3_project_create', {'defaultModelSelection': {'model': 'other'}}, "'other'")
        self.refused('mcp__t3-code__schedule_task', {'model': 'other'}, 'takes no model')
        # A lock at a stricter mode than the guard's counts too.
        self.policy(dict(POLICY, mode='advise'))
        self.assertIsNone(self.hook('mcp__t3-code__delegate_task', self.OUTSIDE))

    def test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes(self):
        """Fail closed: each state where an agent could turn the server's check off, or where it would allow more
        than the guard does, keeps the guard's own check. The allowed pick passes in each."""
        tool = 'mcp__t3-code__delegate_task'
        link = os.path.join(self.dir, 'elsewhere.json')
        states = {
            'missing': lambda: None,
            'not locked': lambda: self.t3_lock(locked=False),
            'writable by others': lambda: self.t3_lock(file_mode=0o666),
            'a link': lambda: (self.t3_lock(), os.rename(self.t3_path, link), os.symlink(link, self.t3_path)),
            'not JSON': lambda: self.write(self.t3_path, '{"settings":'),
            'no modelPolicy': lambda: self.write(self.t3_path, {'schemaVersion': 1, 'settings': {}}),
            'no rows': lambda: self.write(self.t3_path, {'settings': {'modelPolicy': {
                'value': {'mode': 'enforce'}, 'locked': True}}}),
            'locked at advise': lambda: self.t3_lock(mode='advise'),
            'locked at off': lambda: self.t3_lock(mode='off'),
            'a row the guard lacks': lambda: self.t3_lock(rows=[{'driver': 'claudeAgent', 'model': 'other',
                                                                 'efforts': ['high']}]),
            'an effort the guard lacks': lambda: self.t3_lock(rows=[{'driver': 'claudeAgent',
                                                                     'model': 'example-large-1',
                                                                     'efforts': ['high', 'max']}]),
        }
        for state, make in states.items():
            with self.subTest(state=state):
                for path in (self.t3_path, link):
                    if os.path.lexists(path):
                        os.unlink(path)
                make()
                self.refused(tool, self.OUTSIDE, "model 'other'")
                self.passed(tool, self.delegate(GOOD_TARGET))

    def test_a_lock_file_root_does_not_own_is_no_lock(self):
        self.t3_lock()
        self.assertIsNone(self.m.server_lock(POLICY, self.t3_path, os.getuid()))
        self.assertIn('not usable', self.m.server_lock(POLICY, self.t3_path, os.getuid() + 1))

    def test_the_self_check_says_who_checks_the_server_routes(self):
        """The owner sees whether the lock step is done: without it the guard checks the server's routes too."""
        lock = self.dir + self.m.t3_policy_path()
        def check():
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.m.self_check(self.machine_path, os.getuid(), prefix=self.dir)
            return out.getvalue()
        self.assertIn('T3 server routes (delegate_task, create_threads', check())
        self.assertIn('checked here too', check())
        os.makedirs(os.path.dirname(lock), mode=0o755, exist_ok=True)
        self.t3_path = lock
        self.t3_lock()
        self.assertIn('left to T3, whose managed policy locks modelPolicy', check())

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
                     "names 'effort' twice")
        self.refused(t, self.delegate({'model': 'example-deep-2', 'options': {
            'effort': 'high', 'reasoningEffort': 'medium', 'ultracode': False}}), "effort 'medium'")

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
                                      'options': [{'id': 'effort', 'value': 'medium'}, NO_ULTRA]}))
        self.passed(t, self.delegate({'model': 'example-deep-2', 'options': {'effort': 'high', 'thinking_level': 'high',
                                                                             'ultracode': False}}))
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
                                                          'efforts': ['medium'], 'effortOptionIds': ['effort']}]})
        self.refused(t, self.delegate(GOOD_TARGET), "effort 'high' is not allowed")
        self.passed(t, self.delegate(dict(GOOD_TARGET, driverKind='claudeAgent')))
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'medium', 'ultracode': False}}))

    def test_a_model_selection_never_trusts_driver_kind(self):
        """#589 round 2: a ModelSelection (t3_thread_launch, t3_thread_configure, a project's default) has no
        driverKind; T3 drops the key and runs the pick on the named instance. So a driverKind there is refused, and
        the pick is checked against every row of its model, the ultracode rule included. Policy from both reviews:
        one model in rows of two drivers that allow different efforts."""
        two = {'mode': 'enforce', 'allow': [LARGE, DEEP, {'driver': 'otherDriver', 'model': 'example-large-1',
                                                          'efforts': ['xhigh', 'max'], 'effortOptionIds': ['effort']}]}
        self.policy(two)
        paths = {'t3_thread_launch': lambda sel: {'title': 'x', 'modelSelection': sel},
                 't3_thread_configure': lambda sel: {'threadId': 't', 'modelSelection': sel},
                 't3_project_create': lambda sel: {'title': 'p', 'defaultModelSelection': sel},
                 't3_project_update': lambda sel: {'projectId': 'p', 'defaultModelSelection': sel}}
        for path, wrap in paths.items():
            tool = 'mcp__t3-code__' + path
            for driver in ('otherDriver', 'claudeAgent', None, '', 7):
                for effort in ('max', 'high'):
                    with self.subTest(path=path, driver=driver, effort=effort):
                        # the reviews' call: Claude runs it at max, which its own row does not allow
                        self.refused(tool, wrap({'instanceId': 'claudeAgent', 'model': 'example-large-1',
                                                 'driverKind': driver,
                                                 'options': {'effort': effort, 'ultracode': False}}),
                                     'driverKind: T3 ignores it on a model selection')
            for effort, why in (('max', "effort 'max' is not allowed"), ('high', "effort 'high' is not allowed"),
                                ('xhigh', "effort 'xhigh' is not allowed")):
                with self.subTest(path=path, effort=effort):     # no effort both rows allow
                    self.refused(tool, wrap({'instanceId': 'claudeAgent', 'model': 'example-large-1',
                                             'options': {'effort': effort, 'ultracode': False}}), why)
        # rows that agree: the pick passes, and the Claude row's ultracode rule still holds
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'otherDriver', 'model': 'example-large-1',
                                                          'efforts': ['high', 'max'], 'effortOptionIds': ['effort']}]})
        for path, wrap in paths.items():
            tool = 'mcp__t3-code__' + path
            with self.subTest(path=path, agree=True):
                self.passed(tool, wrap({'instanceId': 'a', 'model': 'example-large-1',
                                        'options': {'effort': 'high', 'ultracode': False}}))
                self.refused(tool, wrap({'instanceId': 'a', 'model': 'example-large-1',
                                         'options': {'effort': 'high'}}), 'must name ultracode')
                self.refused(tool, wrap({'instanceId': 'a', 'model': 'example-large-1', 'driverKind': 'otherDriver',
                                         'options': {'effort': 'high', 'ultracode': False}}), 'T3 ignores it')
        # delegate_task and create_threads keep driverKind: T3 refuses an instance of another driver there
        self.policy(two)
        other = {'model': 'example-large-1', 'driverKind': 'otherDriver', 'options': {'effort': 'max'}}
        claude = dict(GOOD_TARGET, driverKind='claudeAgent')
        self.passed('mcp__t3-code__delegate_task', self.delegate(other))
        self.passed('mcp__t3-code__delegate_task', self.delegate(claude))
        self.passed('mcp__t3-code__create_threads', {'threads': [{'target': other}, {'target': claude}]})
        self.refused('mcp__t3-code__delegate_task', self.delegate(dict(claude, options={'effort': 'max',
                                                                                         'ultracode': False})),
                     "effort 'max' is not allowed")
        self.refused('mcp__t3-code__create_threads', {'threads': [{'target': dict(other, driverKind='claudeAgent')}]},
                     "effort 'max' is not allowed")

    def test_ultracode_is_checked_as_the_effort_that_runs(self):
        t = 'mcp__t3-code__delegate_task'
        xhigh = {'mode': 'enforce', 'allow': [dict(LARGE, efforts=['high', 'xhigh'])]}
        self.policy(xhigh)
        self.passed(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'xhigh', 'ultracode': False}}))
        for options in ({'effort': 'xhigh', 'ultracode': True},
                        [{'id': 'effort', 'value': 'xhigh'}, {'id': 'ultracode', 'value': True}],
                        {'effort': 'ultracode'},                       # the old effort value: xhigh, ultracode on
                        {'effort': ' ultracode '}):                    # T3 trims the value
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
                                                         'options': [{'id': 'effort', 'value': 'high'}, NO_ULTRA]}})

    def test_thread_configure_is_checked_only_when_it_sets_a_model(self):
        t = 'mcp__t3-code__t3_thread_configure'
        self.assertIsNone(self.hook(t, {'threadId': 'x'}))
        self.refused(t, {'modelSelection': None}, 'modelSelection is missing')
        self.refused(t, {'modelSelection': {'instanceId': 'a', 'model': 'other', 'options': {'effort': 'high'}}},
                     "model 'other'")
        self.passed(t, {'modelSelection': {'provider': 'a', 'model': 'example-large-1', 'options': GOOD_TARGET['options']}})

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
const b = await pipeline(items, x => agent('p ' + x, {model: 'example-deep-2', effort: 'high',}), r => r.y[0] * 0.5)
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
            'agent passed on': "const g = (f, p, o) => f(p, {model: 'small', effort: 'high'}); g(agent, 'p', {ok})"
                               .replace('{ok}', ok),
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

    def test_workflow_hole_scripts_of_the_lenses_and_the_audit_are_refused(self):
        # PR #275 round-2 lenses and audit: each of these passed the old regex-or-division guess, and JavaScript runs
        # the hidden agent() call with a model the policy does not allow.
        bad = '{model: "small", effort: "max"}'
        slash = {
            'of as a name': f"let of = 4; of / 1; agent('x', {bad}); 1 / 1",
            'of divides a call': f"let p; const of = 1;\nof / (p = agent('x', {bad})) / 1;\nawait p",
            'of /=': f"let of = 1;\nof /= agent('x', {bad}) / 1",
            'property named like a keyword': f"const o = {{typeof: 4}}; o.typeof / 1; agent('x', {bad}); 1 / 1",
            'Array.of': f"let p; Array.of / (p = agent('x', {bad})) / 1; await p",
            'o.return': f"let p; const o = {{return: 1}}; o.return / (p = agent('x', {bad})) / 1; await p",
            'method named if': f"const a = {{if: x => 4}}; a.if(1) / 1; agent('x', {bad}); 1 / 1",
            'for await header': f"for await (const x of []) /'/; agent('x', {bad}); // '",
            'debugger then a line break': f"debugger\n/'/; agent('x', {bad}); //'",
            'break then a line break': f"for (;;) {{ break\n/'/; agent('x', {bad}); //'\n}}",
            'continue then a line break': f"for (const i of [1]) {{ continue\n/'/; agent('x', {bad}); //'\n}}",
            'class extends': f"class A extends /'/.constructor {{}}; agent('x', {bad}); //'",
        }
        line_ends = {
            'bare CR ends a comment': f"// c\ragent('x', {bad})",
            'U+2028 ends a comment': f"// c\u2028agent('x', {bad})",
            'U+2029 ends a comment': f"// c\u2029agent('x', {bad})",
            'after an allowed call': f"agent('a', {{model: 'large', effort: 'high'}}) // ok\u2028agent('x', {bad})",
            'bare CR in a string': "agent('x\r', {model: 'large', effort: 'high'})",
        }
        for cases, why in ((slash, 'no regular expression literals or / operators'),
                           (line_ends, 'a line end other than')):
            for name, script in cases.items():
                with self.subTest(name):
                    self.refused('Workflow', self.workflow(script), why)

    def test_workflow_of_and_keyword_properties_before_a_bracket(self):
        """#329: "of" is the for-of keyword only in a for (...) header after the binding; elsewhere, and a keyword
        after . or ?., is a name, so a [ after it is a member access (refused unless [number])."""
        ok = "{model: 'large', effort: 'high'}"
        for script in ("const of = {x: 1}; const y = of['x']",
                       "const o = {return: [1]}; const y = o.return['a']",
                       "const o = {of: {}}; const y = o.of['x']",
                       "const o = {}; const y = o?.typeof['x']",
                       "for (const of of [1]) { of['x'] }",
                       "for (const x of y) { of['x'] }",
                       "for (const x of of['x']) {}",
                       "f(of['x'])"):
            with self.subTest(script):
                self.refused('Workflow', self.workflow(script), 'a computed member access other than [number]')
        for script in (f"for (const m of ['a', 'b']) {{ await agent(m, {ok}) }}",
                       f"for await (const m of ['a']) {{ agent(m, {ok}) }}",
                       f"for (const [a, b] of [[1, 2]]) {{ agent('x', {ok}) }}",
                       f"for (const {{a}} of [{{a: 1}}]) {{ agent('x', {ok}) }}",
                       f"for (const of of ['a']) {{ agent(of, {ok}) }}",
                       f"for (x.y of [1]) {{}} agent('x', {ok})",
                       f"for (of[0] of [1]) {{}} agent('x', {ok})",
                       f"for (const x of f(y)) {{ for (const z of ['a']) {{ agent(z, {ok}) }} }}",
                       f"const t = typeof [1]; const s = `${{[1].length}}`; agent('x', {ok})"):
            with self.subTest(script):
                self.assertIn('is for most work', self.passed('Workflow', self.workflow(script)))

    def test_workflow_division_and_regular_expressions_are_refused(self):
        for script in ("const h = 4 / 2", "let h = 4; h /= 2", "const ok = /^a+$/.test(s)", "s.split(/,/)",
                       "x = {} / 2", "agent('x', {model: 'large', effort: 'high'}); const r = 1/2"):
            with self.subTest(script):
                reason = self.refused('Workflow', self.workflow(script), 'the Workflow script cannot be checked')
                self.assertIn('use new RegExp("...") or multiply instead', reason)

    def test_workflow_slash_in_strings_templates_and_comments_and_crlf_pass(self):
        script = ("// a comment with / and /re/g and 1 / 2\r\n"
                  "/* a block / comment\r\n   over two lines */\r\n"
                  "const a = await agent('a/b // not a comment /* nor this */', {model: 'large', effort: 'high'})\r\n"
                  "const t = `path/${a.x}/more ${\"x/y\"} ${'/'}`\r\n"
                  "const s = \"a/b\" + 'c/d' + `/*`\r\n")
        self.assertIn('is for most work', self.passed('Workflow', self.workflow(script)))
        self.assertIn('is for most work', self.passed('Workflow', self.workflow(script.replace('\r\n', '\n'))))

    # ---- RemoteTrigger and the project tools (#329)

    def test_remote_trigger_may_only_read(self):
        for action in ('list', 'get', 'list_runs', 'get_run_log'):
            self.assertIsNone(self.hook('RemoteTrigger', {'action': action, 'trigger_id': 'x'}), action)
        for args in ({'action': 'create', 'body': {'model': 'example-large-1'}}, {'action': 'update', 'trigger_id': 'x'},
                     {'action': 'run', 'trigger_id': 'x'}, {'action': 'create_webhook_trigger'}, {'action': 'LIST'},
                     {}, {'action': ['list']}):
            with self.subTest(args):
                self.refused('RemoteTrigger', args, 'cloud routine', '(only list, get, list_runs, get_run_log pass)')
        self.refused(None, raw='{"tool_name": "RemoteTrigger", "tool_input": [1]}')
        self.policy(dict(POLICY, mode='advise'))
        self.assertIn('enforce would refuse', self.passed('RemoteTrigger', {'action': 'run'}))
        self.policy(dict(POLICY, mode='off'))
        self.assertIsNone(self.hook('RemoteTrigger', {'action': 'run'}))

    def test_project_default_model_is_checked_when_given(self):
        for name in ('t3_project_create', 't3_project_update'):
            t = 'mcp__t3-code__' + name
            with self.subTest(name):
                self.assertIsNone(self.hook(t, {'title': 'p', 'workspaceRoot': '/w'}))       # left out: no model set
                self.assertIsNone(self.hook(t, {'projectId': 'p', 'defaultModelSelection': None}))  # null: cleared
                self.refused(t, {'projectId': 'p', 'defaultModelSelection': {'instanceId': 'a', 'model': 'other',
                                                                           'options': {'effort': 'high'}}},
                             "model 'other'", 'defaultModelSelection')
                self.refused(t, {'defaultModelSelection': {'instanceId': 'a', 'model': 'example-large-1'}},
                             'names no effort')
                self.refused(t, {'defaultModelSelection': 'example-large-1'}, 'defaultModelSelection is missing')
                self.passed(t, {'projectId': 'p', 'defaultModelSelection': dict(GOOD_TARGET, instanceId='a')})

    def test_t3_picks_must_name_ultracode_unless_the_row_allows_it(self):
        """T3 takes a Claude model's catalog default for ultracode when a call leaves it out (on, where the default
        effort is ultracode), so a pick whose row does not allow ultracode says ultracode: false itself."""
        t = 'mcp__t3-code__delegate_task'
        plain = {'model': 'example-large-1', 'options': {'effort': 'high'}}
        self.refused(t, self.delegate(plain), "options must name ultracode (false): without it 'example-large-1' can "
                     'run ultracode (inherited from the parent thread', '"ultracodeOption": false')
        self.refused(t, self.delegate(dict(plain, options=[{'id': 'effort', 'value': 'high'}])), 'must name ultracode')
        self.refused(t, self.delegate(dict(plain, driverKind='claudeAgent')), 'must name ultracode')
        self.passed(t, self.delegate(GOOD_TARGET))
        self.passed(t, self.delegate(dict(plain, options=[{'id': 'effort', 'value': 'high'}, NO_ULTRA])))
        self.refused(t, self.delegate(dict(plain, options={'effort': 'high', 'ultracode': True})), "effort 'ultracode'")
        # another driver's model: T3's Claude default does not apply
        self.assertIsNone(self.hook(t, self.delegate({'model': 'example-plain', 'driverKind': 'otherDriver'})))
        self.assertIsNone(self.hook(t, self.delegate(dict(plain, model='example-plain', driverKind='otherDriver',
                                                          options=None))))
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'otherDriver', 'model': 'example-large-1'}]})
        self.assertIsNone(self.hook(t, self.delegate({'model': 'example-large-1', 'driverKind': 'otherDriver'})))
        self.policy(POLICY)
        # every T3 tool that picks a model
        for tool, args in (('create_threads', {'threads': [{'target': plain}]}),
                           ('t3_thread_launch', {'modelSelection': plain}),
                           ('t3_thread_configure', {'modelSelection': plain}),
                           ('t3_project_update', {'defaultModelSelection': plain})):
            with self.subTest(tool):
                self.refused('mcp__t3-code__' + tool, args, 'must name ultracode')
        # a row that allows ultracode needs no word on it; Claude Code's Agent tool has no ultracode option
        self.passed('Agent', {'model': 'large', 'effort': 'high'})
        self.policy({'mode': 'enforce', 'allow': [dict(LARGE, efforts=['high', 'ultracode'])]})
        self.passed(t, self.delegate(plain))

    def test_a_row_without_the_ultracode_option_needs_none(self):
        """#589 review: T3 offers some Claude models no ultracode option and refuses one named for them ("Unknown
        option ultracode"). Their row says "ultracodeOption": false, and a pick then need not, and cannot, name it."""
        t = 'mcp__t3-code__delegate_task'
        old = dict(DEEP, ultracodeOption=False)
        self.policy({'mode': 'enforce', 'allow': [LARGE, old]})
        plain = {'model': 'example-deep-2', 'options': {'effort': 'high'}}
        for tool, args in (('delegate_task', self.delegate(plain)),
                           ('delegate_task', self.delegate(dict(plain, driverKind='claudeAgent'))),
                           ('create_threads', {'threads': [{'target': plain}]}),
                           ('t3_thread_launch', {'modelSelection': plain}),
                           ('t3_thread_configure', {'modelSelection': plain}),
                           ('t3_project_create', {'defaultModelSelection': plain}),
                           ('t3_project_update', {'defaultModelSelection': plain})):
            with self.subTest(tool):
                self.passed('mcp__t3-code__' + tool, args)
        # it still runs only its own efforts: ultracode on (or the old effort value) is refused
        for options in ({'effort': 'high', 'ultracode': True}, {'effort': 'ultracode'}, {'effort': 'xhigh'}):
            with self.subTest(options=options):
                self.refused(t, self.delegate(dict(plain, options=options)), 'is not allowed')
        # the other rows keep the rule
        self.refused(t, self.delegate({'model': 'example-large-1', 'options': {'effort': 'high'}}),
                     'must name ultracode')
        self.assertIn('no ultracode option', self.refused(t, self.delegate({'model': 'x'})))
        # the key is checked: only Claude rows, never with the effort ultracode, true/false only
        for row, why in ((dict(PLAIN, ultracodeOption=False), 'only claudeAgent rows have an ultracode option'),
                         (dict(old, efforts=['high', 'ultracode']), 'cannot run the effort ultracode'),
                         (dict(DEEP, ultracodeOption='no'), 'ultracodeOption: must be bool')):
            with self.subTest(row=row):
                self.policy({'mode': 'enforce', 'allow': [row]})
                self.refused(t, self.delegate(plain), why)
        self.policy({'mode': 'enforce', 'allow': [dict(DEEP, ultracodeOption=True)]})
        self.refused(t, self.delegate(plain), 'must name ultracode')

    def test_options_are_read_the_way_t3_reads_them(self):
        """#589 review: T3 trims option ids and text values (packages/contracts/src/model.ts) and takes the first
        entry of an id; it drops values that are neither text nor true/false and uses the default. The guard reads
        them the same way, and refuses an id given twice after trimming and a value T3 would drop, on every path
        that takes options."""
        paths = {'delegate_task': lambda sel: self.delegate(sel),
                 'create_threads': lambda sel: {'threads': [{'target': sel}]},
                 't3_thread_launch': lambda sel: {'modelSelection': sel},
                 't3_thread_configure': lambda sel: {'modelSelection': sel},
                 't3_project_create': lambda sel: {'defaultModelSelection': sel},
                 't3_project_update': lambda sel: {'defaultModelSelection': sel}}
        refused = {
            # lens B's two: max and ultracode past a check that compared ids exactly
            'a spaced effort id first': ({' effort': 'max', 'effort': 'high', 'ultracode': False}, "names 'effort' twice"),
            'a spaced ultracode id': ({'effort': 'high', ' ultracode': True, 'ultracode': False},
                                      "names 'ultracode' twice"),
            'a spaced id alone': ({'effort': 'high', '\tultracode\n': True}, "effort 'ultracode' is not allowed"),
            'JavaScript trims U+FEFF': ({'\ufeffeffort': 'max', 'ultracode': False}, "effort 'max' is not allowed"),
            'a list twice': ([{'id': 'effort', 'value': 'high'}, {'id': 'effort ', 'value': 'max'}, NO_ULTRA],
                             "names 'effort' twice"),
            'an empty id': ({'effort': 'high', ' ': 'x', 'ultracode': False}, 'not a non-empty text'),
            'a list id that is not a text': ([{'id': 1, 'value': 'high'}], 'not a non-empty text'),
            'ultracode null': ({'effort': 'high', 'ultracode': None}, 'ultracode must be true or false'),
            'ultracode 0': ({'effort': 'high', 'ultracode': 0}, 'ultracode must be true or false'),
            'ultracode as text': ({'effort': 'high', 'ultracode': 'false'}, 'ultracode must be true or false'),
            'an effort T3 drops': ({'effort': None, 'ultracode': False}, 'is not a non-empty text or true/false'),
            'an empty effort': ({'effort': '  ', 'ultracode': False}, 'is not a non-empty text or true/false'),
        }
        for path, wrap in paths.items():
            for name, (options, why) in refused.items():
                with self.subTest(path=path, case=name):
                    self.refused('mcp__t3-code__' + path, wrap({'model': 'example-large-1', 'options': options}), why)
            passing = ({' effort ': ' high ', ' ultracode': False},
                       [{'id': ' effort', 'value': 'high\u00a0'}, {'id': '\u3000ultracode', 'value': False}])
            for options in passing:
                with self.subTest(path=path, options=options):
                    self.passed('mcp__t3-code__' + path, wrap({'model': ' example-large-1\ufeff', 'options': options}))
        # Python trims \x1c-\x1f, JavaScript does not: T3 sees another model, so the guard does too
        self.refused('mcp__t3-code__delegate_task', self.delegate(dict(GOOD_TARGET, model='example-large-1\x1f')),
                     'is not allowed')

    def test_a_drivers_own_reasoning_option_is_read_or_refused(self):
        """#601: an ACP driver names its reasoning control by its own option id. A row of a driver other than Claude
        lists such ids in effortOptionIds (checked as efforts) or otherOptionIds (passed); any other id is refused,
        so a pick cannot name an allowed effort and also raise the agent's own reasoning control."""
        self.policy({'mode': 'enforce', 'allow': [LARGE, AGENT, PLAIN]})
        paths = {'delegate_task': lambda sel: self.delegate(dict(sel, driverKind='acpDriver')),
                 'delegate_task (no driver)': lambda sel: self.delegate(sel),
                 'create_threads': lambda sel: {'threads': [{'target': sel}]},
                 't3_thread_launch': lambda sel: {'modelSelection': sel},
                 't3_thread_configure': lambda sel: {'modelSelection': sel},
                 't3_project_create': lambda sel: {'defaultModelSelection': sel},
                 't3_project_update': lambda sel: {'defaultModelSelection': sel}}
        passing = ({'thought_level': 'medium'}, {'effort': 'low', 'thought_level': 'low', 'mode': 'plan'},
                   [{'id': ' thought_level', 'value': 'low'}, {'id': 'mode ', 'value': 'ask'}],
                   {'thought_level': 'low', 'ultracode': False})
        refused = (({'thought_level': 'high'}, "effort 'high' is not allowed"),
                   ({'effort': 'low', 'thought_level': 'high'}, "effort 'high' is not allowed"),
                   ({' thought_level ': 'high'}, "effort 'high' is not allowed"),
                   ({'thought_level': 'low', 'ultracode': True}, "effort 'ultracode' is not allowed"),
                   ({'effort': 'low', 'reasoning_level': 'max'}, "option 'reasoning_level' is not one the guard can "
                                                                 'read for acpDriver'),
                   ({'mode': 'plan'}, 'names no effort'),
                   # #618 review (both lenses): the driver never reads the shared id, so it ran its own default
                   ({'effort': 'low', 'mode': 'plan'}, "names no effort for 'example-agent' under 'thought_level'"),
                   ([{'id': 'effort', 'value': 'low'}, {'id': 'Thought_Level', 'value': 'max'}],
                    "option 'Thought_Level' is not one"))
        for path, wrap in paths.items():
            tool = 'mcp__t3-code__' + path.split(' ')[0]
            for options in passing:
                with self.subTest(path=path, options=options):
                    self.passed(tool, wrap({'model': 'example-agent', 'options': options}))
            for options, why in refused:
                with self.subTest(path=path, options=options):
                    self.refused(tool, wrap({'model': 'example-agent', 'options': options}), why)
            # a row that lists no ids reads only the shared effort ids: the old way through is closed
            if path != 'delegate_task':
                with self.subTest(path=path, row='plain'):
                    self.refused(tool, wrap({'model': 'example-plain', 'options': {'thought_level': 'max'}}),
                                 "option 'thought_level' is not one the guard can read", 'effortOptionIds')
                    self.passed(tool, wrap({'model': 'example-plain'}))
        # Claude rows are not touched: T3 checks their options against its own list
        self.passed('mcp__t3-code__delegate_task',
                    self.delegate(dict(GOOD_TARGET, options={'effort': 'high', 'ultracode': False, 'thinking': True})))
        # the same model in a Claude row and an ACP row, no driver: each row reads the options its own way, and
        # every row must accept them. The ACP row wants thought_level, the Claude row refuses it (#618 review), so
        # no model selection passes; delegate_task with a driverKind picks one row.
        claude = {'driver': 'claudeAgent', 'model': 'example-agent', 'efforts': ['high']}
        self.policy({'mode': 'enforce', 'allow': [claude, dict(AGENT, efforts=['low', 'high'])]})
        t = 'mcp__t3-code__t3_thread_launch'
        self.refused(t, {'modelSelection': {'model': 'example-agent', 'options': {'effort': 'high', 'ultracode': False}}},
                     "names no effort for 'example-agent' under 'thought_level'")
        self.refused(t, {'modelSelection': {'model': 'example-agent', 'options': {
            'effort': 'high', 'thought_level': 'high', 'ultracode': False}}}, "option 'thought_level' is not one T3's "
                     'claudeAgent reads')
        self.refused(t, {'modelSelection': {'model': 'example-agent', 'options': {
            'effort': 'high', 'other': 'x', 'ultracode': False}}}, "option 'other' is not one")
        d = 'mcp__t3-code__delegate_task'
        self.passed(d, self.delegate({'model': 'example-agent', 'driverKind': 'claudeAgent',
                                      'options': {'effort': 'high', 'ultracode': False}}))
        self.passed(d, self.delegate({'model': 'example-agent', 'driverKind': 'acpDriver',
                                      'options': {'effort': 'high', 'thought_level': 'high'}}))
        self.refused(d, self.delegate({'model': 'example-agent', 'driverKind': 'acpDriver',
                                       'options': {'thought_level': 'max'}}), "effort 'max' is not allowed")
        # advise says it and refuses nothing
        self.policy({'mode': 'advise', 'allow': [AGENT]})
        note = self.passed(t, {'modelSelection': {'model': 'example-agent', 'options': {'thought_level': 'high'}}})
        self.assertIn("effort 'high' is not allowed", note)

    def test_option_id_keys_are_checked(self):
        """#601: effortOptionIds and otherOptionIds are for rows of other drivers, never name ultracode, and an
        effort id is never an "other" option. A broken row refuses every pick (fail closed)."""
        t = 'mcp__t3-code__delegate_task'
        for row, why in ((dict(LARGE, effortOptionIds=['thought_level']), 'only rows of other drivers take it'),
                         (dict(LARGE, otherOptionIds=['mode']), 'only rows of other drivers take it'),
                         (dict(AGENT, effortOptionIds=['ultracode']), 'ultracode is always read as the effort'),
                         (dict(AGENT, otherOptionIds=['ultracode']), 'ultracode is always read as the effort'),
                         (dict(AGENT, otherOptionIds=['thought_level']), "'thought_level' is an effort option id"),
                         (dict(AGENT, otherOptionIds=['reasoning']), "'reasoning' is an effort option id"),
                         (dict(AGENT, effortOptionIds=[' thought_level']), 'without spaces around them'),
                         (dict(AGENT, effortOptionIds='thought_level'), 'effortOptionIds: must be list'),
                         (dict(AGENT, otherOptionIds=[1]), 'otherOptionIds'),
                         (dict(AGENT, effortOptionIds=[]), 'effortOptionIds'),
                         # #618 review: a row with own effort ids but no efforts could pass no pick that names them
                         ({k: v for k, v in AGENT.items() if k != 'efforts'}, 'the row lists no efforts'),
                         # T3's session mode can repeat the reasoning levels (lens A)
                         (dict(AGENT, otherOptionIds=['_t3/session-mode']), "'_t3/session-mode' is T3's session mode"),
                         # a model or reasoning control is never an "other" option (lens B)
                         (dict(AGENT, otherOptionIds=['mode', 'model']), "'model' names a model or reasoning"),
                         (dict(AGENT, otherOptionIds=['configModelId']), "'configModelId' names a model"),
                         (dict(AGENT, otherOptionIds=['reasoning_mode']), "'reasoning_mode' names a model"),
                         (dict(AGENT, otherOptionIds=['ThoughtLevel']), "'ThoughtLevel' names a model"),
                         (dict(AGENT, otherOptionIds=['thinkingBudget']), "'thinkingBudget' names a model"),
                         (dict(AGENT, otherOptionIds=['effortHint']), "'effortHint' names a model")):
            with self.subTest(row=row):
                self.policy({'mode': 'advise', 'allow': [row]})
                self.refused(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low'}}), why)
        self.policy({'mode': 'enforce', 'allow': [AGENT]})
        self.passed(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low'}}))
        self.refused(t, self.delegate({'model': 'x'}), 'acpDriver example-agent (low|medium; effort option ids '
                     'thought_level; other option ids mode)')
        # an agent whose session mode is its reasoning: the mode is an effort id, checked and required
        self.policy({'mode': 'enforce', 'allow': [dict(AGENT, effortOptionIds=['thought_level', '_t3/session-mode'])]})
        self.passed(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low',
                                                                            '_t3/session-mode': 'low'}}))
        self.refused(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low',
                                                                             '_t3/session-mode': 'max'}}),
                     "effort 'max' is not allowed")
        self.refused(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low'}}),
                     "under '_t3/session-mode'")

    def test_a_pick_names_its_effort_where_the_driver_reads_it(self):
        """#618 review (both lenses, blocking): T3's ACP adapter applies only the ids the agent's session exposes, so
        a pick that names an allowed effort under a shared id and leaves the row's own reasoning id out runs the
        agent's default, or on delegate_task the parent's level (resolveTarget lays the given options over the
        parent's). So a row with effortOptionIds wants each of them, and a Claude row wants "effort" (the one id T3's
        Claude driver reads its effort from, claudeModelOptions.ts). On every path both lenses used."""
        two = dict(AGENT, effortOptionIds=['thought_level', 'reasoning_mode'])
        codex = {'driver': 'codexLike', 'model': 'example-coder', 'efforts': ['low'],
                 'effortOptionIds': ['reasoningEffort']}
        self.policy({'mode': 'enforce', 'allow': [LARGE, AGENT, codex,
                                                   dict(two, model='example-agent-two')]})
        paths = {'delegate_task': lambda sel, drv: self.delegate(dict(sel, driverKind=drv)),
                 'delegate_task (no driver)': lambda sel, drv: self.delegate(sel),
                 'create_threads': lambda sel, drv: {'threads': [{'target': sel}]},
                 't3_thread_launch': lambda sel, drv: {'title': 'x', 'modelSelection': sel},
                 't3_thread_configure': lambda sel, drv: {'threadId': 't', 'modelSelection': sel},
                 't3_project_create': lambda sel, drv: {'title': 'p', 'defaultModelSelection': sel},
                 't3_project_update': lambda sel, drv: {'projectId': 'p', 'defaultModelSelection': sel}}
        cases = (
            # (model, driver, options, None when it passes or the refusal's words)
            ('example-agent', 'acpDriver', {'effort': 'low'}, "names no effort for 'example-agent' under "
                                                               "'thought_level', the option acpDriver reads it from"),
            ('example-agent', 'acpDriver', {'reasoning': 'low'}, "under 'thought_level'"),
            ('example-agent', 'acpDriver', [{'id': 'effort', 'value': 'low'}, {'id': 'mode', 'value': 'plan'}],
             "under 'thought_level'"),
            ('example-agent', 'acpDriver', {'thought_level': 'low'}, None),
            ('example-agent-two', 'acpDriver', {'thought_level': 'low'}, "under 'reasoning_mode'"),
            ('example-agent-two', 'acpDriver', {'reasoning_mode': 'low', 'effort': 'low'}, "under 'thought_level'"),
            ('example-agent-two', 'acpDriver', {'reasoning_mode': 'low', 'thought_level': 'low'}, None),
            ('example-agent-two', 'acpDriver', {'reasoning_mode': 'max', 'thought_level': 'low'}, "effort 'max'"),
            # a driver that reads one of the shared ids: list it, and only it counts
            ('example-coder', 'codexLike', {'effort': 'low'}, "under 'reasoningEffort', the option codexLike"),
            ('example-coder', 'codexLike', {'reasoningEffort': 'low'}, None),
            ('example-coder', 'codexLike', {'reasoningEffort': 'low', 'effort': 'max'}, "effort 'max'"),
            # Claude reads "effort" only
            ('example-large-1', 'claudeAgent', {'thinking_level': 'high', 'ultracode': False},
             "names no effort for 'example-large-1' under 'effort', the option claudeAgent reads it from"),
            ('example-large-1', 'claudeAgent', {'reasoning': 'high', 'ultracode': False}, "under 'effort'"),
            ('example-large-1', 'claudeAgent', {'effort': 'high', 'reasoning': 'high', 'ultracode': False}, None),
            ('example-large-1', 'claudeAgent', {'effort': 'high', 'reasoning': 'max', 'ultracode': False},
             "effort 'max'"),
        )
        for path, wrap in paths.items():
            tool = 'mcp__t3-code__' + path.split(' ')[0]
            for model, driver, options, why in cases:
                with self.subTest(path=path, model=model, options=options):
                    args = wrap({'model': model, 'options': options}, driver)
                    if why is None:
                        self.passed(tool, args)
                    else:
                        self.refused(tool, args, why)
        # advise says it, refuses nothing; runs again with the same answer
        self.policy({'mode': 'advise', 'allow': [AGENT]})
        args = {'modelSelection': {'model': 'example-agent', 'options': {'effort': 'low'}}}
        note = self.passed('mcp__t3-code__t3_thread_launch', args)
        self.assertIn("under 'thought_level'", note)
        self.assertEqual(note, self.passed('mcp__t3-code__t3_thread_launch', args))

    def test_an_old_policy_this_round_refuses_fails_closed(self):
        """State: a policy written for the first #601 round (a session mode as an "other" option, own effort ids on a
        row without efforts) stays on the machine after the update. It refuses every pick, in advise mode too, says
        which key to change, and does so every time it runs."""
        t = 'mcp__t3-code__t3_thread_launch'
        pick = {'modelSelection': {'model': 'example-large-1', 'options': {'effort': 'high', 'ultracode': False}}}
        for row, why in ((dict(AGENT, otherOptionIds=['_t3/session-mode']), "'_t3/session-mode' is T3's session mode"),
                         ({k: v for k, v in AGENT.items() if k != 'efforts'}, 'the row lists no efforts')):
            with self.subTest(why=why):
                self.policy({'mode': 'enforce', 'allow': [LARGE, row]})
                first = self.refused(t, pick, why)
                self.assertEqual(first, self.refused(t, pick, why))
                self.policy({'mode': 'advise', 'allow': [LARGE, row]})
                self.refused(t, pick, why, 'fails closed')

    def test_claude_rows_refuse_ids_claude_does_not_read(self):
        """#618 review (lens B major, lens A minor): a model selection has no driver, and T3 runs it on the named
        instance. A model only in Claude rows, on another driver's instance offering the same slug, would read an id
        such as thought_level as its reasoning, unchecked. So a Claude row takes only the ids T3's Claude driver reads
        (and the shared effort ids, checked as efforts)."""
        self.policy({'mode': 'enforce', 'allow': [LARGE, AGENT]})
        paths = {'delegate_task': lambda sel: self.delegate(dict(sel, driverKind='claudeAgent')),
                 'delegate_task (no driver)': lambda sel: self.delegate(sel),
                 'create_threads': lambda sel: {'threads': [{'target': sel}]},
                 't3_thread_launch': lambda sel: {'instanceId': 'any', 'modelSelection': sel},
                 't3_thread_configure': lambda sel: {'modelSelection': sel},
                 't3_project_create': lambda sel: {'defaultModelSelection': sel},
                 't3_project_update': lambda sel: {'defaultModelSelection': sel}}
        base = {'effort': 'high', 'ultracode': False}
        for path, wrap in paths.items():
            tool = 'mcp__t3-code__' + path.split(' ')[0]
            for extra in ({'thought_level': 'max'}, {'mode': 'x'}, {'Effort': 'max'}, {'variant': 'max'}):
                with self.subTest(path=path, extra=extra):
                    self.refused(tool, wrap({'model': 'example-large-1', 'options': dict(base, **extra)}),
                                 f"option {next(iter(extra))!r} is not one T3's claudeAgent reads")
            for extra in ({}, {'thinking': True}, {'fastMode': False}, {'contextWindow': '1m'},
                          {'reasoningEffort': 'medium'}):
                with self.subTest(path=path, extra=extra):
                    self.passed(tool, wrap({'model': 'example-large-1', 'options': dict(base, **extra)}))

    # ---- #629: the effort id of each of T3's built-in drivers, and drivers the guard knows none for

    PICK_PATHS = {'delegate_task': lambda sel, drv: {'task': 'x', 'target': dict(sel, driverKind=drv)},
                  'delegate_task (no driver)': lambda sel, drv: {'task': 'x', 'target': sel},
                  'create_threads': lambda sel, drv: {'threads': [{'target': sel}]},
                  't3_thread_launch': lambda sel, drv: {'title': 'x', 'modelSelection': sel},
                  't3_thread_configure': lambda sel, drv: {'threadId': 't', 'modelSelection': sel},
                  't3_project_create': lambda sel, drv: {'title': 'p', 'defaultModelSelection': sel},
                  't3_project_update': lambda sel, drv: {'projectId': 'p', 'defaultModelSelection': sel}}

    def on_every_path(self, cases):
        """(model, driver, options or None for none, None when it passes or the refusal's words) on every T3 path."""
        for path, wrap in self.PICK_PATHS.items():
            tool = 'mcp__t3-code__' + path.split(' ')[0]
            for model, driver, options, why in cases:
                with self.subTest(path=path, model=model, options=options):
                    sel = {'model': model} if options is None else {'model': model, 'options': options}
                    args = wrap(sel, driver)
                    if why is None:
                        self.passed(tool, args)
                    else:
                        self.refused(tool, args, *([why] if isinstance(why, str) else why))

    def test_a_built_in_drivers_effort_id_is_known_without_the_row_listing_it(self):
        """#629 (major): a Codex or OpenCode row without effortOptionIds let {"effort": "low"} through, and the driver
        never reads that id, so it ran its default or the parent's level. The guard now knows the id each built-in
        driver reads (DRIVER_EFFORT_IDS, read from T3's code by a contract test) and wants the effort there."""
        rows = [{'driver': 'codex', 'model': 'example-codex', 'efforts': ['low', 'medium']},
                {'driver': 'opencode', 'model': 'example-oc', 'efforts': ['low']},
                {'driver': 'pi', 'model': 'example-pi', 'efforts': ['low']},
                {'driver': 'fx', 'model': 'example-fx', 'efforts': ['low']},
                {'driver': 'grok', 'model': 'example-grok', 'efforts': ['low']},
                # the README's old advice for Codex: list reasoningEffort; still works, and is the same id
                {'driver': 'codex', 'model': 'example-codex-listed', 'efforts': ['low'],
                 'effortOptionIds': ['reasoningEffort']},
                # an own id on top of the pinned one: both are wanted
                {'driver': 'opencode', 'model': 'example-oc-two', 'efforts': ['low'], 'effortOptionIds': ['depth']}]
        self.policy({'mode': 'enforce', 'allow': rows})
        self.on_every_path((
            ('example-codex', 'codex', {'effort': 'low'}, "names no effort for 'example-codex' under "
                                                          "'reasoningEffort', the option codex reads it from"),
            ('example-codex', 'codex', {'reasoning': 'low'}, "under 'reasoningEffort'"),
            ('example-codex', 'codex', {}, "under 'reasoningEffort'"),
            ('example-codex', 'codex', None, "under 'reasoningEffort'"),
            ('example-codex', 'codex', {'reasoningEffort': 'low'}, None),
            ('example-codex', 'codex', [{'id': 'reasoningEffort', 'value': 'medium'}], None),
            ('example-codex', 'codex', {'reasoningEffort': 'high'}, "effort 'high' is not allowed"),
            ('example-codex', 'codex', {'reasoningEffort': 'low', 'effort': 'max'}, "effort 'max'"),
            ('example-codex', 'codex', {'reasoningEffort': 'low', 'ultracode': True}, "effort 'ultracode'"),
            ('example-codex', 'codex', {'reasoningEffort': 'low', 'variant': 'max'}, "option 'variant' is not one"),
            ('example-oc', 'opencode', {'effort': 'low'}, "under 'variant', the option opencode reads it from"),
            ('example-oc', 'opencode', {'variant': 'low'}, None),
            ('example-oc', 'opencode', {'variant': 'max'}, "effort 'max' is not allowed"),
            ('example-oc', 'opencode', {'variant': 'low', 'agent': 'build'}, "option 'agent' is not one"),
            ('example-pi', 'pi', {'effort': 'low'}, "under 'thinking', the option pi reads it from"),
            ('example-pi', 'pi', {'thinking': 'low'}, None),
            ('example-pi', 'pi', {'thinking': 'max'}, "effort 'max' is not allowed"),
            ('example-fx', 'fx', {'effort': 'low'}, None),
            ('example-fx', 'fx', {'reasoningEffort': 'low'}, "under 'effort', the option fx reads it from"),
            ('example-fx', 'fx', {'effort': 'max'}, "effort 'max' is not allowed"),
            ('example-grok', 'grok', {'reasoningEffort': 'low'}, None),
            ('example-grok', 'grok', {'effort': 'low'}, "under 'reasoningEffort'"),
            ('example-codex-listed', 'codex', {'reasoningEffort': 'low'}, None),
            ('example-codex-listed', 'codex', {'effort': 'low'}, "under 'reasoningEffort'"),
            ('example-oc-two', 'opencode', {'variant': 'low'}, "under 'depth'"),
            ('example-oc-two', 'opencode', {'depth': 'low'}, "under 'variant'"),
            ('example-oc-two', 'opencode', {'depth': 'low', 'variant': 'low'}, None),
            ('example-oc-two', 'opencode', {'depth': 'max', 'variant': 'low'}, "effort 'max'"),
        ))
        # a row of a built-in driver without efforts still takes a pick that names none
        self.policy({'mode': 'enforce', 'allow': [{'driver': 'codex', 'model': 'example-codex'}]})
        self.on_every_path((('example-codex', 'codex', None, None),
                            ('example-codex', 'codex', {'reasoningEffort': 'low'}, 'its row lists no efforts')))

    def test_a_driver_the_guard_knows_no_effort_id_for_fails_closed(self):
        """#629 (major): a row with efforts of a driver whose reasoning id the guard does not know (Cursor,
        Antigravity, the ACP registry: the agent or model names it; any driver not built in) and no effortOptionIds
        refuses every T3 pick, whatever it names, and says to list the id. A row with the id, or without efforts,
        works as before."""
        for driver in model_guard.AGENT_NAMED_EFFORT_DRIVERS + ('customDriver',):
            row = {'driver': driver, 'model': 'example-x', 'efforts': ['low']}
            self.policy({'mode': 'enforce', 'allow': [row]})
            why = (f'the guard does not know which option id {driver} reads the effort', 'effortOptionIds')
            self.on_every_path(tuple(('example-x', driver, options, why) for options in (
                {'effort': 'low'}, {'reasoningEffort': 'low'}, {'thinking_level': 'low'}, {}, None,
                [{'id': 'effort', 'value': 'low'}])))
            self.policy({'mode': 'enforce', 'allow': [dict(row, effortOptionIds=['thought_level'])]})
            self.on_every_path((('example-x', driver, {'thought_level': 'low'}, None),
                                ('example-x', driver, {'effort': 'low'}, "under 'thought_level'")))
            self.policy({'mode': 'enforce', 'allow': [{'driver': driver, 'model': 'example-x'}]})
            self.on_every_path((('example-x', driver, None, None),
                                ('example-x', driver, {'effort': 'low'}, 'its row lists no efforts')))
        # the Agent tool and Workflow name Claude models: not touched
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'cursor', 'model': 'example-x', 'efforts': ['low']}]})
        self.passed('Agent', {'model': 'example-large-1', 'effort': 'high', 'prompt': 'x'})
        # the same model in a Claude row: without a driver every row must accept the pick, so it is refused too
        self.policy({'mode': 'enforce', 'allow': [LARGE, {'driver': 'cursor', 'model': 'example-large-1',
                                                          'efforts': ['high']}]})
        self.refused('mcp__t3-code__t3_thread_launch', {'modelSelection': {
            'model': 'example-large-1', 'options': {'effort': 'high', 'ultracode': False}}}, 'does not know')
        self.passed('mcp__t3-code__delegate_task', self.delegate(dict(GOOD_TARGET, driverKind='claudeAgent')))
        # advise says it and refuses nothing
        self.policy({'mode': 'advise', 'allow': [{'driver': 'cursor', 'model': 'example-x', 'efforts': ['low']}]})
        note = self.passed('mcp__t3-code__t3_thread_launch', {'modelSelection': {
            'model': 'example-x', 'options': {'effort': 'low'}}})
        self.assertIn('effortOptionIds', note)

    def test_the_self_check_names_rows_with_no_known_effort_id(self):
        """#629: the self-check lists each row whose every T3 pick is refused for want of an effort id, in advise and
        enforce, so the owner adds effortOptionIds before turning enforce on; nothing in mode off."""
        rows = [LARGE, PLAIN, AGENT, {'driver': 'codex', 'model': 'example-codex', 'efforts': ['low']},
                {'driver': 'cursor', 'model': 'example-cursor', 'efforts': ['low']},
                {'driver': 'cursor', 'model': 'example-cursor-listed', 'efforts': ['low'], 'effortOptionIds': ['x']},
                {'driver': 'customDriver', 'model': 'example-custom', 'efforts': ['low']}]
        for mode in ('advise', 'enforce'):
            with self.subTest(mode=mode):
                problems = self.m.effort_id_problems(self.m.parse_policy(json.dumps({'mode': mode, 'allow': rows}), 'p'))
                self.assertEqual(len(problems), 2, problems)
                self.assertIn("cursor 'example-cursor' lists efforts", problems[0])
                self.assertIn('effortOptionIds', problems[0])
                self.assertIn("customDriver 'example-custom'", problems[1])
                # runs again with the same answer
                self.assertEqual(problems, self.m.effort_id_problems(
                    self.m.parse_policy(json.dumps({'mode': mode, 'allow': rows}), 'p')))
        self.assertEqual(self.m.effort_id_problems(self.m.parse_policy(json.dumps({'mode': 'off', 'allow': rows}),
                                                                        'p')), [])
        self.assertEqual(self.m.effort_id_problems(self.m.parse_policy(json.dumps({'mode': 'enforce', 'allow': []}),
                                                                        'p')), [])

    def test_a_drivers_effort_or_model_id_is_never_an_other_option(self):
        """#629: a built-in driver's own effort id is an effort id, and an OpenCode agent (which may set its own
        model) is never an option that changes neither. A broken row refuses every pick (fail closed)."""
        t = 'mcp__t3-code__delegate_task'
        for row, why in (({'driver': 'opencode', 'model': 'example-oc', 'efforts': ['low'],
                           'otherOptionIds': ['variant']}, "'variant' is an effort option id"),
                         ({'driver': 'pi', 'model': 'example-oc', 'efforts': ['low'],
                           'otherOptionIds': ['thinking']}, "'thinking' is an effort option id"),
                         ({'driver': 'codex', 'model': 'example-oc', 'otherOptionIds': ['reasoningEffort']},
                          "'reasoningEffort' is an effort option id"),
                         ({'driver': 'opencode', 'model': 'example-oc', 'efforts': ['low'],
                           'otherOptionIds': ['agent']}, "'agent' names a model or reasoning control"),
                         # minor 2 of #629: each word of NOT_OTHER has a case of its own
                         (dict(AGENT, otherOptionIds=['instanceId']), "'instanceId' names a model"),
                         (dict(AGENT, otherOptionIds=['providerName']), "'providerName' names a model"),
                         (dict(AGENT, otherOptionIds=['driverKind']), "'driverKind' names a model"),
                         (dict(AGENT, otherOptionIds=['ultracodeMode']), "'ultracodeMode' names a model")):
            with self.subTest(row=row):
                self.policy({'mode': 'advise', 'allow': [row]})
                self.refused(t, self.delegate({'model': 'example-oc', 'options': {'variant': 'low'}}), why)
        # another driver may name an agent option as one that changes neither
        self.policy({'mode': 'enforce', 'allow': [dict(AGENT, otherOptionIds=['mode', 'agent'])]})
        self.passed(t, self.delegate({'model': 'example-agent', 'options': {'thought_level': 'low', 'agent': 'x'}}))

    def test_a_built_in_driver_in_another_case_is_refused(self):
        """#638 review (lens B minor 1): a row of driver "Codex" with effortOptionIds ["thought_level"] passed
        {"thought_level": "low"} on a pick with no driver, which T3 may run on a codex instance; Codex ignores that id
        and runs its default. A row naming a built-in driver kind in another case or with spaces around it now makes
        the policy invalid (every checked call refused, in every mode, the same every time); a driver that only
        resembles one is a custom driver, as before."""
        typo = {'driver': 'Codex', 'model': 'example-oc', 'efforts': ['low'], 'effortOptionIds': ['thought_level']}
        t = 'mcp__t3-code__t3_thread_launch'
        pick = {'modelSelection': {'model': 'example-oc', 'options': {'thought_level': 'low'}}}
        for kind in model_guard.BUILT_IN_DRIVER_KINDS:
            for spelling in {kind.upper(), kind.capitalize(), kind.lower(), f' {kind}', f'{kind}\u00a0'} - {kind}:
                row = dict(typo, driver=spelling)
                for mode in model_guard.MODES:
                    with self.subTest(driver=spelling, mode=mode):
                        self.policy({'mode': mode, 'allow': [LARGE, row]})
                        why = (f"driver: {spelling!r} is not T3's driver kind {kind!r}", self.policy_path)
                        first = self.refused(t, pick, *why)
                        self.assertEqual(first, self.refused(t, pick, *why))
                        self.refused('Agent', {'model': 'large', 'effort': 'high'}, *why)
        # the exact kind is fine, and its pick is checked with the kind's own ids
        self.policy({'mode': 'enforce', 'allow': [dict(typo, driver='codex')]})
        self.refused(t, pick, "under 'reasoningEffort'")
        self.passed(t, {'modelSelection': {'model': 'example-oc', 'options': {'thought_level': 'low',
                                                                              'reasoningEffort': 'low'}}})
        # a custom driver whose name only contains a built-in kind is not a typo of one
        for custom in ('codex2', 'myCodex', 'open-code'):
            self.policy({'mode': 'enforce', 'allow': [dict(typo, driver=custom)]})
            self.passed(t, pick)

    def test_an_agent_option_is_never_an_effort_id(self):
        """#638 review (lens A minor 1, lens B minor 3): an opencode row listing "agent" in effortOptionIds let a pick
        name an OpenCode agent ({"variant": "low", "agent": "build"} passed), and a Kiro "mode" switches Kiro's agent.
        Both are refused in effortOptionIds and in otherOptionIds of their driver's row; the policy is then invalid,
        every checked call refused, in advise too. Another driver may use the same ids."""
        t = 'mcp__t3-code__delegate_task'
        pick = self.delegate({'model': 'example-oc', 'options': {'variant': 'low', 'agent': 'build'}})
        for row, why in (({'driver': 'opencode', 'model': 'example-oc', 'efforts': ['low', 'build'],
                           'effortOptionIds': ['agent']}, "effortOptionIds: 'agent' picks an agent of opencode"),
                         ({'driver': 'opencode', 'model': 'example-oc', 'efforts': ['low'],
                           'effortOptionIds': ['depth', 'agent']}, "effortOptionIds: 'agent' picks"),
                         ({'driver': 'kiro', 'model': 'example-oc', 'efforts': ['low'],
                           'effortOptionIds': ['mode']}, "effortOptionIds: 'mode' picks an agent of kiro"),
                         ({'driver': 'kiro', 'model': 'example-oc', 'efforts': ['low'],
                           'otherOptionIds': ['mode']}, "otherOptionIds: 'mode' names a model or reasoning")):
            for mode in ('advise', 'enforce'):
                with self.subTest(row=row, mode=mode):
                    self.policy({'mode': mode, 'allow': [row]})
                    first = self.refused(t, pick, why, self.policy_path)
                    self.assertEqual(first, self.refused(t, pick, why, self.policy_path))
        # without the key the agent is an id the guard cannot read: refused
        self.policy({'mode': 'enforce', 'allow': [{'driver': 'kiro', 'model': 'example-oc', 'efforts': ['low']}]})
        self.on_every_path((('example-oc', 'kiro', {'reasoningEffort': 'low', 'mode': 'x'}, "option 'mode' is not one"),
                            ('example-oc', 'kiro', {'reasoningEffort': 'low'}, None)))
        # an ACP agent's own "agent" or "mode" may be an effort id of its row
        self.policy({'mode': 'enforce', 'allow': [dict(AGENT, effortOptionIds=['agent'], otherOptionIds=['mode'])]})
        self.passed(t, self.delegate({'model': 'example-agent', 'options': {'agent': 'low', 'mode': 'x'}}))

    def test_the_true_false_rule_is_for_claude_rows_only(self):
        """#638 review (lens B minor 6): thinking and fastMode must be true/false on a Claude row only. On a row of
        another driver they are that driver's own ids: Pi's effort, or an option its row lists."""
        self.policy({'mode': 'enforce', 'allow': [
            dict(AGENT, otherOptionIds=['fastMode']),
            {'driver': 'fx', 'model': 'example-fx', 'efforts': ['low'], 'otherOptionIds': ['fastMode']}]})
        self.on_every_path((
            ('example-agent', 'acpDriver', {'thought_level': 'low', 'fastMode': 'on'}, None),
            ('example-fx', 'fx', {'effort': 'low', 'fastMode': 'on'}, None),
            ('example-fx', 'fx', {'effort': 'low', 'fastMode': True}, None)))

    def test_claude_booleans_take_only_true_or_false(self):
        """#629 (minor): T3's Claude driver reads thinking and fastMode only as true or false. Another driver may read
        the same id as its reasoning level (Pi's "thinking", Mistral Vibe's ACP "thinking"), and a model selection runs
        on whichever instance it names, so a text there is refused on a Claude row."""
        self.policy({'mode': 'enforce', 'allow': [LARGE]})
        base = {'effort': 'high', 'ultracode': False}
        cases = [('example-large-1', 'claudeAgent', dict(base, **{key: value}),
                  f'options.{key} must be true or false')
                 for key in model_guard.CLAUDE_BOOLEAN_IDS for value in ('high', 'max', 'true', ' low ')]
        cases += [('example-large-1', 'claudeAgent', [{'id': 'effort', 'value': 'high'}, NO_ULTRA,
                                                      {'id': 'thinking', 'value': 'max'}], 'options.thinking must be'),
                  ('example-large-1', 'claudeAgent', [{'id': 'effort', 'value': 'high'}, NO_ULTRA,
                                                      {'id': ' fastMode ', 'value': 'on'}], 'options.fastMode must be')]
        cases += [('example-large-1', 'claudeAgent', dict(base, **{key: value}), None)
                  for key in model_guard.CLAUDE_BOOLEAN_IDS for value in (True, False)]
        cases += [('example-large-1', 'claudeAgent', dict(base, thinking=True, fastMode=False), None)]
        # a value that is neither a text nor true/false is refused before, on every row (T3 drops it)
        cases += [('example-large-1', 'claudeAgent', dict(base, thinking=value), 'is not a non-empty text or true/false')
                  for value in (1, None, [], {})]
        self.on_every_path(cases)
        # on a row of another driver a text there is read as that driver's own: Pi's effort
        self.policy({'mode': 'enforce', 'allow': [{'driver': 'pi', 'model': 'example-pi', 'efforts': ['low']}]})
        self.on_every_path((('example-pi', 'pi', {'thinking': 'low'}, None),
                            ('example-pi', 'pi', {'thinking': True}, "effort True")))

    def test_old_pick_shapes_claude_rows_now_refuse(self):
        """#629 (minor 3), state: picks that passed before #618 and are refused now, every time, naming the key to
        add: ultracode alone (T3 maps it to xhigh with ultracode on, but no "effort" is named), and a Claude pick
        naming its effort only under thinking_level or reasoningEffort, ids T3's Claude driver never reads."""
        self.policy({'mode': 'enforce', 'allow': [dict(LARGE, efforts=['high', 'ultracode'])]})
        cases = (('example-large-1', 'claudeAgent', {'ultracode': True}, "under 'effort'"),
                 ('example-large-1', 'claudeAgent', {'thinking_level': 'high', 'ultracode': False}, "under 'effort'"),
                 ('example-large-1', 'claudeAgent', {'reasoningEffort': 'high', 'ultracode': False}, "under 'effort'"),
                 ('example-large-1', 'claudeAgent', {'effort': 'xhigh', 'ultracode': True}, None),
                 ('example-large-1', 'claudeAgent', {'effort': 'high', 'reasoningEffort': 'high',
                                                     'ultracode': False}, None))
        self.on_every_path(cases)
        self.on_every_path(cases)   # runs again: the same answers

    def test_an_old_policy_of_a_built_in_driver_now_fails_closed(self):
        """#629, state: a policy written before this change stays on the machine. A Codex row without effortOptionIds
        (its picks named "effort", which Codex ignores) now wants reasoningEffort; a Cursor row without the key
        refuses every pick. Each says what to change, in enforce and advise, the same every time it runs."""
        codex = {'driver': 'codex', 'model': 'example-codex', 'efforts': ['low']}
        cursor = {'driver': 'cursor', 'model': 'example-cursor', 'efforts': ['low']}
        t = 'mcp__t3-code__t3_thread_launch'
        for row, why in ((codex, "under 'reasoningEffort'"), (cursor, 'effortOptionIds')):
            pick = {'modelSelection': {'model': row['model'], 'options': {'effort': 'low'}}}
            with self.subTest(driver=row['driver']):
                self.policy({'mode': 'enforce', 'allow': [LARGE, row]})
                first = self.refused(t, pick, why)
                self.assertEqual(first, self.refused(t, pick, why))
                self.policy({'mode': 'advise', 'allow': [LARGE, row]})
                note = self.passed(t, pick)
                self.assertIn(why, note)
                self.assertEqual(note, self.passed(t, pick))
                self.policy({'mode': 'off', 'allow': [LARGE, row]})
                self.assertIsNone(self.hook(t, pick))
        # an empty policy file or one with no rows is unchanged: refused, or every pick refused
        self.policy('')
        self.refused(t, {'modelSelection': {'model': 'example-codex', 'options': {'reasoningEffort': 'low'}}})
        self.policy({'mode': 'enforce', 'allow': []})
        self.refused(t, {'modelSelection': {'model': 'example-codex', 'options': {'reasoningEffort': 'low'}}},
                     'not allowed')

    # ---- #600: T3's model list against "ultracodeOption": false rows (a self-check report)

    def t3_list(self, name, models, profiles=None, cache=True):
        """Write a T3 model list: T3's cache ({fetchedAtMs, manifest}) or the manifest itself."""
        def select(*choices):
            return {'capabilities': {'optionDescriptors': [
                {'id': 'effort', 'type': 'select', 'options': [{'id': c, 'label': c} for c in choices]}]}}
        profiles = dict({'new': select('high', 'xhigh', 'ultracode'), 'old': select('high'),
                         'toggle': {'capabilities': {'optionDescriptors': [select('high')['capabilities'][
                             'optionDescriptors'][0], {'id': 'ultracode', 'type': 'boolean'}]}},
                         'bare': {}, 'empty': {'capabilities': {}}, 'none': {'capabilities': {'optionDescriptors': []}}},
                        **(profiles or {}))
        manifest = {'version': 1, 'providers': {'claudeAgent': {'profiles': profiles, 'models': models}}}
        path = os.path.join(self.dir, name)
        self.write(path, {'fetchedAtMs': 1, 'manifest': manifest} if cache else manifest)
        return path

    def drift(self, paths, mode='enforce', rows=None):
        policy = self.m.parse_policy(json.dumps({'mode': mode, 'allow': rows or [
            LARGE, dict(DEEP, ultracodeOption=False)]}), 'p')
        return self.m.ultracode_option_problems(policy, paths)

    def test_t3_model_list_drift_is_reported(self):
        """#600: T3 reloads its model list every hour. The self-check reads it and reports an "ultracodeOption":
        false row whose model now has the ultracode option, has no option list (T3 then checks no options and keeps
        an inherited ultracode), or is not in the list (a custom model); a list it cannot read is said, never a pass."""
        deep = 'example-deep-2'
        def one(models, **kw):
            return self.drift([self.t3_list('t3.json', models, **kw)])
        self.assertEqual(one([{'slug': deep, 'profile': 'old'}]), [])
        self.assertEqual(one([{'slug': deep, 'profile': 'none'}]), [])
        self.assertEqual(one([{'slug': deep, 'profile': 'old'}], cache=False), [])     # a manifest itself
        self.assertEqual(one([{'slug': ' ' + deep, 'profile': ' old'}]), [])           # T3 trims
        # T3's order: the entry whose slug is the model wins over one whose alias is it, either way round
        alias_of = {'slug': 'example-deep-alt', 'aliases': [deep], 'profile': 'new'}
        self.assertEqual(one([{'slug': deep, 'profile': 'old'}, alias_of]), [])
        self.assertEqual(one([alias_of, {'slug': deep, 'profile': 'old'}]), [])
        cases = {
            'the effort ultracode': ([{'slug': deep, 'profile': 'new'}], 'offers it the ultracode option'),
            'the ultracode toggle': ([{'slug': deep, 'profile': 'toggle'}], 'offers it the ultracode option'),
            'by an alias': ([{'slug': 'example-deep-latest', 'aliases': ['EXAMPLE-DEEP-2'], 'profile': 'new'}],
                            'offers it the ultracode option'),
            'the worst entry': ([{'slug': deep, 'profile': 'bare'}, {'slug': deep, 'profile': 'new'}],
                                'offers it the ultracode option'),
            'the worst entry first': ([{'slug': deep, 'profile': 'new'}, {'slug': deep, 'profile': 'bare'}],
                                      'offers it the ultracode option'),
            'no capabilities': ([{'slug': deep, 'profile': 'bare'}], 'gives it no option list'),
            'no option list': ([{'slug': deep, 'profile': 'empty'}], 'gives it no option list'),
            'an unknown profile': ([{'slug': deep, 'profile': 'gone'}], 'gives it no option list'),
            'no profile': ([{'slug': deep}], 'gives it no option list'),
            'a custom model': ([{'slug': 'example-large-1', 'profile': 'new'}], "is not in T3's model list"),
            'an empty list': ([], "is not in T3's model list"),
        }
        for name, (models, why) in cases.items():
            with self.subTest(name):
                problems = one(models)
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f"'{deep}' has \"ultracodeOption\": false", problems[0])
                self.assertIn(why, problems[0])
                self.assertIn('take "ultracodeOption": false out of its policy row', problems[0])
        # runs again: the same answer, nothing stored
        stale = self.t3_list('stale.json', [{'slug': deep, 'profile': 'new'}])
        self.assertEqual(self.drift([stale]), self.drift([stale]))
        # several lists: each is read, each says its own
        good = self.t3_list('good.json', [{'slug': deep, 'profile': 'old'}])
        problems = self.drift([good, stale, os.path.join(self.dir, 'gone.json')])
        self.assertEqual(len(problems), 2, problems)
        self.assertIn(stale, problems[0])
        self.assertIn('gone.json cannot be read', problems[1])
        # advise reports too (enforce would act on the row); off checks nothing; no false row needs no list
        self.assertEqual(len(self.drift([stale], mode='advise')), 1)
        self.assertEqual(self.drift([stale], mode='off'), [])
        self.assertEqual(self.drift(None, rows=[LARGE]), [])
        # an old machine file names no list: said, never a silent pass
        problems = self.drift(None)
        self.assertEqual(len(problems), 1)
        self.assertIn('names no T3 model list ("t3ModelManifests")', problems[0])

    def test_a_t3_model_list_that_cannot_be_used_is_said(self):
        """#600: missing, a link, not a plain file, too large, half written (T3 stopped mid-write) or not a model
        list: the self-check says so and names the rows it could not check."""
        good = self.t3_list('good.json', [{'slug': 'example-deep-2', 'profile': 'old'}])
        link = os.path.join(self.dir, 'link.json')
        os.symlink(good, link)
        fifo = os.path.join(self.dir, 'fifo')
        os.mkfifo(fifo)
        def raw(name, text):
            path = os.path.join(self.dir, name)
            self.write(path, text)
            return path
        with open(good, encoding='utf-8') as f:
            whole = f.read()
        cases = {
            'missing': (os.path.join(self.dir, 'none.json'), 'cannot be read'),
            'a link': (link, 'cannot be read'),
            'a folder': (self.dir, 'is not a plain file'),
            'a pipe': (fifo, 'is not a plain file'),
            'half written': (raw('half.json', whole[:len(whole) // 2]), 'is not valid JSON'),
            'empty': (raw('empty.json', ''), 'is not valid JSON'),
            'version 2': (raw('v2.json', {'version': 2, 'providers': {}}), 'is not a T3 model manifest of version 1'),
            'version true': (raw('vt.json', {'version': True, 'providers': {}}), 'is not a T3 model manifest'),
            'a list': (raw('list.json', []), 'is not a T3 model manifest'),
            'no claudeAgent': (raw('nc.json', {'version': 1, 'providers': {}}), 'has no claudeAgent model list'),
            # a manifest key next to a version: read as a manifest of that version, never unwrapped as T3's cache
            'a manifest with a version': (raw('mv.json', {'version': 2, 'manifest': json.loads(whole)['manifest']}),
                                          'is not a T3 model manifest of version 1'),
            'models not a list': (raw('nm.json', {'version': 1, 'providers': {'claudeAgent': {
                'models': {}, 'profiles': {}}}}), 'has no claudeAgent model list'),
            'no profiles': (raw('np.json', {'fetchedAtMs': 1, 'manifest': {'version': 1, 'providers': {
                'claudeAgent': {'models': []}}}}), 'has no claudeAgent model list'),
        }
        for name, (path, why) in cases.items():
            with self.subTest(name):
                with deadline(10):   # a pipe with no writer: a read that waits would hang, not fail
                    problems = self.drift([path])
                self.assertEqual(len(problems), 1, problems)
                self.assertIn(f'T3 model list {path} {why}', problems[0])
                self.assertIn("'example-deep-2' the ultracode option", problems[0])
        with mock.patch.object(self.m, 'T3_MANIFEST_MAX', 10):
            self.assertIn('is larger than 10 bytes', self.drift([good])[0])
        self.assertEqual(self.drift([good]), [])

    # ---- #643: the levels a Claude row allows against the levels T3's model list offers (a self-check report)

    def offers(self, paths, rows, mode='enforce'):
        return self.m.effort_offer_problems(
            self.m.parse_policy(json.dumps({'mode': mode, 'allow': rows}), 'p'), paths)

    def test_t3_model_list_levels_are_compared(self):
        """#643: a level a Claude row allows that its model does not offer: T3's delegate_task refuses a pick of it,
        but a launch, a configure or a project default starts the model with no level, so it runs its default. The
        self-check reports each such level; a model T3 gives no option list is reported, one not in the list is a
        note (nothing to compare)."""
        large = 'example-large-1'
        def row(efforts, model=large, **kw):
            return dict({'driver': 'claudeAgent', 'model': model, 'efforts': efforts}, **kw)
        other = {'capabilities': {'optionDescriptors': [   # xhigh only as a choice of another select
            {'id': 'effort', 'type': 'select', 'options': [{'id': 'high', 'label': 'high'}]},
            {'id': 'contextWindow', 'type': 'select', 'options': [{'id': 'xhigh', 'label': 'xhigh'}]}]}}
        def one(models, rows, **kw):
            return self.offers([self.t3_list('t3.json', models, profiles={'other': other}, **kw)], rows)
        new = [{'slug': large, 'profile': 'new'}]     # high, xhigh and ultracode
        # every allowed level offered: nothing to say
        for rows in ([row(['high', 'xhigh'])], [row(['high', 'ultracode'])], [row(['high'])]):
            with self.subTest(rows=rows):
                self.assertEqual(one(new, rows), ([], []))
        self.assertEqual(one([{'slug': large, 'profile': 'toggle'}], [row(['high', 'ultracode'])]), ([], []))
        self.assertEqual(one([{'slug': ' ' + large, 'profile': ' new'}], [row(['high'])]), ([], []))   # T3 trims
        self.assertEqual(one(new, [row(['high'])], cache=False), ([], []))                           # a manifest itself
        cases = {
            'one level': (new, row(['medium', 'high']), 'allows medium', 'take it out'),
            'two levels': (new, row(['low', 'medium', 'high']), 'allows low, medium', 'take them out'),
            'the default': (new, row(['medium', 'high'], defaultEffort='medium'), 'allows medium',
                            "efforts and defaultEffort"),
            'ultracode without the option': ([{'slug': large, 'profile': 'old'}], row(['high', 'ultracode']),
                                             'allows ultracode', 'take it out'),
            'by an alias': ([{'slug': 'example-large-latest', 'aliases': ['EXAMPLE-LARGE-1'], 'profile': 'old'}],
                            row(['high', 'xhigh']), 'allows xhigh', 'take it out'),
            'the worst entry': ([{'slug': large, 'profile': 'new'}, {'slug': large, 'profile': 'old'}],
                                row(['high', 'xhigh']), 'allows xhigh', 'take it out'),
            'the worst entry first': ([{'slug': large, 'profile': 'old'}, {'slug': large, 'profile': 'new'}],
                                      row(['high', 'xhigh']), 'allows xhigh', 'take it out'),
            'an empty option list': ([{'slug': large, 'profile': 'none'}], row(['high']), 'allows high',
                                     'take it out'),
            'a level of another select': ([{'slug': large, 'profile': 'other'}], row(['high', 'xhigh']),
                                          'allows xhigh', 'take it out'),
            'no capabilities': ([{'slug': large, 'profile': 'bare'}], row(['high']), 'gives it no option list',
                                'T3 checks no level'),
            'no option list': ([{'slug': large, 'profile': 'empty'}], row(['high']), 'gives it no option list',
                               'T3 checks no level'),
            'an unknown profile': ([{'slug': large, 'profile': 'gone'}], row(['high']), 'gives it no option list',
                                   'T3 checks no level'),
        }
        for name, (models, given, why, fix) in cases.items():
            with self.subTest(name):
                problems, notes = one(models, [given])
                self.assertEqual(len(problems), 1, problems)
                self.assertEqual(notes, [])
                self.assertIn(f"'{large}' ", problems[0])
                self.assertIn(why, problems[0])
                self.assertIn(fix, problems[0])
        # a model not in a list that has the others (a custom model): a note, no problem
        custom = 'example-custom-1'
        problems, notes = one(new, [row(['high']), row(['high'], model=custom)])
        self.assertEqual(problems, [])
        self.assertEqual(len(notes), 1)
        self.assertIn(f"'{custom}' is not in T3's model list", notes[0])
        # a list with none of the rows' models (an empty list, or likely the wrong file): a problem, not notes only
        for models in ([], [{'slug': 'example-other', 'profile': 'new'}]):
            with self.subTest(models=models):
                problems, notes = one(models, [row(['high']), row(['high'], model=custom)])
                self.assertEqual(len(problems), 1, problems)
                self.assertEqual(notes, [])
                self.assertIn(f"has none of '{large}', '{custom}'", problems[0])
        # T3's order (resolveClaudeCatalogModel): the entry whose slug is the model, and only without one the first
        # entry whose alias is it; a valid row is never reported because another entry names the model too
        alias_of = {'slug': 'example-large-alt', 'aliases': [large], 'profile': 'old'}
        for models in ([{'slug': large, 'profile': 'new'}, alias_of], [alias_of, {'slug': large, 'profile': 'new'}]):
            with self.subTest(models=models):
                self.assertEqual(one(models, [row(['high', 'xhigh'])]), ([], []))
        first, second = ({'slug': f'example-large-{n}', 'aliases': ['example-large'], 'profile': p}
                         for n, p in ((7, 'new'), (8, 'old')))
        self.assertEqual(one([first, second], [row(['high', 'xhigh'], model='example-large')]), ([], []))
        problems, _ = one([second, first], [row(['high', 'xhigh'], model='example-large')])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('allows xhigh', problems[0])
        # an effort select that offers ultracode but no xhigh: T3 puts xhigh in its place, so xhigh is offered
        uc = {'capabilities': {'optionDescriptors': [{'id': 'effort', 'type': 'select', 'options': [
            {'id': c, 'label': c} for c in ('low', 'high', 'ultracode')]}]}}
        for rows in ([row(['high', 'xhigh'])], [row(['high', 'ultracode'])]):
            with self.subTest(rows=rows):
                self.assertEqual(self.offers([self.t3_list('uc.json', [{'slug': large, 'profile': 'uc'}],
                                                           profiles={'uc': uc})], rows), ([], []))
        # an "effort" that is not a select offers no level, as T3 reads none from it
        multi = {'capabilities': {'optionDescriptors': [{'id': 'effort', 'type': 'multiSelect', 'options': [
            {'id': 'high', 'label': 'high'}]}]}}
        problems, _ = self.offers([self.t3_list('multi.json', [{'slug': large, 'profile': 'multi'}],
                                                profiles={'multi': multi})], [row(['high'])])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn('allows high', problems[0])
        # rows without efforts and rows of other drivers are not compared (T3's model list has option lists for
        # Claude models only); a policy with none of them needs no list
        others = [PLAIN, AGENT, {'driver': 'codex', 'model': 'example-codex', 'efforts': ['low']},
                  {'driver': 'claudeAgent', 'model': 'example-plain-claude'}]
        self.assertEqual(one([], others), ([], []))
        self.assertEqual(self.offers(None, others), ([], []))
        # runs again: the same answer, nothing stored
        stale = self.t3_list('stale.json', new)
        self.assertEqual(self.offers([stale], [row(['medium', 'high'])]), self.offers([stale], [row(['medium', 'high'])]))
        # several lists: each is read, each says its own; one that cannot be read is said, never a pass
        good = self.t3_list('good.json', [{'slug': large, 'profile': 'old'}])
        problems, _ = self.offers([good, stale, os.path.join(self.dir, 'gone.json')], [row(['high', 'xhigh'])])
        self.assertEqual(len(problems), 2, problems)
        self.assertIn(good, problems[0])
        self.assertIn('gone.json cannot be read', problems[1])
        self.assertIn(f"'{large}' offer the levels their rows allow", problems[1])
        # stopped half-way: T3 was writing its list
        half = os.path.join(self.dir, 'half.json')
        with open(stale, encoding='utf-8') as f:
            self.write(half, f.read()[:40])
        problems, _ = self.offers([half], [row(['high'])])
        self.assertEqual(len(problems), 1, problems)
        self.assertIn(f'T3 model list {half} is not valid JSON', problems[0])
        # advise reports too; off checks nothing
        self.assertEqual(len(self.offers([stale], [row(['medium', 'high'])], mode='advise')[0]), 1)
        self.assertEqual(self.offers([stale], [row(['medium', 'high'])], mode='off'), ([], []))
        self.assertEqual(self.offers(None, [row(['medium', 'high'])], mode='off'), ([], []))
        # an old machine file names no list: said, never a silent pass
        problems, _ = self.offers(None, [row(['high'])])
        self.assertEqual(len(problems), 1)
        self.assertIn('names no T3 model list ("t3ModelManifests")', problems[0])
        self.assertIn(f"'{large}'", problems[0])

    def test_t3_model_lists_in_the_machine_file_are_checked(self):
        """#600: "t3ModelManifests" is a list of absolute paths, each once; anything else refuses every call, like
        every bad machine file key. The lists are only read by the self-check: a call never depends on them."""
        t = 'mcp__t3-code__delegate_task'
        for value in ([], ['t3.json'], [1], ['/a', '/a'], '/a', None, {'a': 1}, ['/a\0b']):
            with self.subTest(value=value):
                self.machine({'policyFile': self.policy_path, 't3ModelManifests': value})
                self.refused(t, self.delegate(GOOD_TARGET), 't3ModelManifests')
        self.machine({'policyFile': self.policy_path + '\0'})   # a clear refusal, not a crash (lens A)
        self.refused(t, self.delegate(GOOD_TARGET), 'policyFile: must be an absolute path')
        self.machine({'policyFile': self.policy_path, 't3ModelManifests': [os.path.join(self.dir, 'none.json')]})
        self.passed(t, self.delegate(GOOD_TARGET))

    def test_the_policy_may_not_live_in_the_guards_own_folder(self):
        """An uninstall removes everything in the guard's /etc folder: a policy there would go with it."""
        for where in ('/etc/model-guard/policy.json', '/etc/model-guard', '/etc/fork-kit/../model-guard/p.json'):
            with self.subTest(where):
                self.machine({'policyFile': where})
                self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'must not be in /etc/model-guard')
        self.machine({'policyFile': self.policy_path})
        self.passed('Agent', {'model': 'large', 'effort': 'high'})

    def test_the_guards_folder_is_found_through_links(self):
        """#589 round 2: /etc may be a link (/etc -> /private/etc on macOS), and so may a folder on the policy's
        path; either way a policy that really lives in the guard's folder is refused."""
        real = os.path.join(self.dir, 'private-etc', 'model-guard')
        os.makedirs(real)
        os.symlink(os.path.join(self.dir, 'private-etc'), os.path.join(self.dir, 'etc'))
        os.symlink(real, os.path.join(self.dir, 'into-guard'))
        # the folder there (an installed kit), then not made yet (before the first install: found by name alone)
        for exists in (True, False):
            if not exists:
                os.rmdir(real)
            for etc_dir in (real, os.path.join(self.dir, 'etc', 'model-guard')):
                for where in (os.path.join(real, 'p.json'), os.path.join(self.dir, 'etc', 'model-guard', 'p.json'),
                              os.path.join(self.dir, 'into-guard', 'p.json'), os.path.join(self.dir, 'into-guard')):
                    with self.subTest(exists=exists, etc_dir=etc_dir, where=where), \
                            mock.patch.object(self.m, 'ETC_DIR', etc_dir):
                        self.machine({'policyFile': where})
                        self.refused('Agent', {'model': 'large', 'effort': 'high'}, f'must not be in {etc_dir}')
                with mock.patch.object(self.m, 'ETC_DIR', etc_dir):
                    self.machine({'policyFile': self.policy_path})
                    self.passed('Agent', {'model': 'large', 'effort': 'high'})
        self.assertFalse(self.m.in_folder(os.path.join(self.dir, 'private-etc', 'model-guard-2', 'p.json'), real))

    def test_the_guards_folder_is_found_by_identity(self):
        """A case-insensitive file system spells one folder many ways (/etc/Model-Guard is /etc/model-guard on
        macOS), so names alone miss it. Simulated here: a second folder that the file system reports as the same."""
        guard_dir, other = os.path.join(self.dir, 'guard'), os.path.join(self.dir, 'Guard-Spelled')
        os.makedirs(os.path.join(guard_dir, 'sub'))
        os.makedirs(os.path.join(other, 'sub'))
        real_stat = os.stat
        def same(path, *a, **k):   # noqa: E306
            return real_stat(guard_dir if os.path.normpath(path) == other else path, *a, **k)
        self.assertFalse(self.m.in_folder(os.path.join(other, 'sub', 'p.json'), guard_dir))
        with mock.patch('os.stat', same):
            for where in (other, os.path.join(other, 'p.json'), os.path.join(other, 'sub', 'p.json')):
                with self.subTest(where):
                    self.assertTrue(self.m.in_folder(where, guard_dir))
            self.assertFalse(self.m.in_folder(os.path.join(self.dir, 'elsewhere', 'p.json'), guard_dir))
        # a folder that does not exist yet is checked by name only
        self.assertFalse(self.m.in_folder(os.path.join(other, 'p.json'), os.path.join(self.dir, 'missing')))
        self.assertTrue(self.m.in_folder(os.path.join(self.dir, 'missing', 'p.json'), os.path.join(self.dir, 'missing')))

    # ---- the policy's version (#329)

    def test_a_newer_policy_says_update_the_kit_in_every_mode(self):
        for policy in (dict(POLICY, version=1), POLICY):
            self.policy(policy)
            self.passed('Agent', {'model': 'large', 'effort': 'high'})
        for mode in ('off', 'advise', 'enforce'):
            with self.subTest(mode):
                self.policy(dict(POLICY, mode=mode, version=2))
                self.refused('Agent', {'model': 'large', 'effort': 'high'}, self.policy_path,
                             '.version: 2 is newer than this kit knows (1); update the kit')
        # checked before the keys: a newer policy's new key names the version, not the key
        self.policy({'version': 3, 'mode': 'enforce', 'allow': [dict(LARGE, driverKinds=['x'])], 'deny': []})
        self.refused('Agent', {'model': 'large', 'effort': 'high'}, 'update the kit')
        for version, why in ((0, 'must be 1 or more'), (-1, 'must be 1 or more'), (True, 'must be int'),
                             ('2', 'must be int'), (1.0, 'must be int'), (None, 'must be int')):
            with self.subTest(version=version):
                self.policy(dict(POLICY, version=version))
                self.refused('Agent', {'model': 'large', 'effort': 'high'}, '.version: ' + why)

    # ---- the owner's record (#329)

    def test_refusals_and_advise_notes_are_recorded(self):
        bad = json.dumps({'tool_name': 'Agent', 'tool_input': {'model': 'small', 'effort': 'high'}, 'session_id': 's-1'})
        self.refused(None, raw=bad)
        self.assertEqual(len(self.records), 1, self.records)
        self.assertTrue(self.records[0].startswith('session s-1: enforce: refused Agent: '), self.records)
        self.assertIn("model 'small'", self.records[0])
        self.passed('Agent', {'model': 'large', 'effort': 'high'})       # a passing call: nothing
        self.assertIsNone(self.hook('Bash', {'model': 'small'}))         # an unchecked tool: nothing
        self.assertEqual(len(self.records), 1)
        self.policy(dict(POLICY, mode='advise'))
        self.passed('Agent', {'model': 'small', 'effort': 'high'})
        self.assertTrue(self.records[-1].startswith("advise: enforce would refuse Agent: the Agent call: model 'small'"),
                        self.records)
        self.policy(dict(POLICY, mode='off'))
        self.assertIsNone(self.hook('Agent', {'model': 'small'}))
        self.assertEqual(len(self.records), 2)
        os.unlink(self.policy_path)
        self.refused('Agent', {})
        self.assertIn('the model policy cannot be used', self.records[-1])
        self.assertIn(self.policy_path, self.records[-1])
        self.refused(None, raw='[1]')
        self.assertEqual(self.records[-1], 'model-guard refused: the hook input is not a JSON object')
        self.policy(POLICY)
        self.refused('Agent', {'model': 'x' * 10000, 'effort': 'high'})
        self.assertEqual(len(self.records[-1]), self.m.RECORD_MAX)

    def test_a_record_that_fails_never_changes_the_answer(self):
        def broken(line):
            raise OSError('the log is gone')
        payload = json.dumps({'tool_name': 'Agent', 'tool_input': {'model': 'small', 'effort': 'high'}})
        for mode, want in (('enforce', 'deny'), ('advise', None)):
            with self.subTest(mode):
                self.policy(dict(POLICY, mode=mode))
                out = io.StringIO()
                self.assertEqual(self.m.run_hook(io.StringIO(payload), out, self.machine_path, os.getuid(),
                                                 record=broken), 0)
                answer = json.loads(out.getvalue())['hookSpecificOutput']
                self.assertEqual(answer.get('permissionDecision'), want, answer)

    def test_the_default_record_is_the_system_log(self):
        calls = []
        fake = types.ModuleType('syslog')
        fake.LOG_USER, fake.LOG_WARNING = 8, 4
        fake.openlog = lambda ident, option, facility: calls.append(('open', ident, facility))
        fake.syslog = lambda priority, text: calls.append(('log', priority, text))
        payload = json.dumps({'tool_name': 'Agent', 'tool_input': {'model': 'small', 'effort': 'high'}})
        with mock.patch.dict(sys.modules, {'syslog': fake}):
            out = io.StringIO()
            self.m.run_hook(io.StringIO(payload), out, self.machine_path, os.getuid())
            self.assertEqual(calls[0], ('open', 'model-guard', 8))
            self.assertEqual(calls[1][:2], ('log', 4))
            self.assertIn('enforce: refused Agent', calls[1][2])

            def gone(priority, text):
                raise OSError('no log socket')
            fake.syslog = gone
            out = io.StringIO()
            self.m.run_hook(io.StringIO(payload), out, self.machine_path, os.getuid())
            self.assertEqual(json.loads(out.getvalue())['hookSpecificOutput']['permissionDecision'], 'deny')

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
        self.assertEqual(self.problems(self.entry()), [])

    def test_the_security_lens_decoy_fails(self):
        """A guard entry on Agent only, and an entry with the full matcher whose command only mentions the guard."""
        p = self.problems(self.entry(matcher='Agent'),
                          self.entry(command='true # ' + self.m.HOOK_LINK))
        self.assertTrue(any('no managed PreToolUse entry runs the guard' in x for x in p), p)
        self.assertTrue(any('entry 0 runs the guard but is not its entry: its matcher is not exactly' in x for x in p), p)
        self.assertTrue(any('entry 1 runs the guard but is not its entry: its hooks are not exactly' in x for x in p),
                        p)

    def test_the_state_lens_split_entries_fail(self):
        """The full matcher with a command that fails open, and the blocking command on another tool."""
        p = self.problems(self.entry(command=self.m.HOOK_LINK), self.entry(matcher='Bash'))
        self.assertTrue(any('no managed PreToolUse entry runs the guard' in x for x in p), p)
        self.assertTrue(any('entry 0' in x and 'hooks are not exactly' in x for x in p), p)
        self.assertTrue(any('entry 1' in x and 'matcher is not exactly' in x for x in p), p)

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

    def test_a_matcher_other_than_the_guards_fails(self):
        """Only the exact matcher passes: no reading of Claude Code's (JavaScript) matcher rules, which a hand-written
        matcher could get past (Python re vs JavaScript, one server name), and an older kit's matcher is reported."""
        old_kit = self.m.HOOK_MATCHER.replace('|RemoteTrigger', '').replace('|t3_project_create|t3_project_update', '')
        self.assertNotEqual(old_kit, self.m.HOOK_MATCHER)
        for matcher in ('', '*', None, 'Agent|Task', ',', ','.join(self.m.CHECKED_TOOLS), '(', old_kit,
                        self.m.HOOK_MATCHER.replace('mcp__.+__', 'mcp__t3-code__'), self.m.HOOK_MATCHER + ' ',
                        '|'.join(self.m.CHECKED_TOOLS)):
            with self.subTest(matcher):
                self.assertIn('its matcher is not exactly', ' '.join(self.problems(self.entry(matcher=matcher))))

    def test_the_matcher_covers_exactly_the_checked_tools(self):
        """The matcher is a regular expression that means the same in JavaScript and Python."""
        cover = lambda name: re.search(self.m.HOOK_MATCHER, name) is not None  # noqa: E731
        for name in ('Agent', 'Task', 'Workflow', 'RemoteTrigger', 'mcp__t3-code__create_threads',
                     'mcp__t3__t3_thread_launch', 'mcp__t3-code__t3_thread_send', 'mcp__t3-code__t3_thread_transfer',
                     'mcp__t3-code__t3_project_create', 'mcp__other__t3_project_update'):
            self.assertTrue(cover(name), name)
        for name in ('Bash', 'TaskStop', 'SubAgent', 'mcp__t3-code__task_status', 'mcp__t3-code__delegate_tasks',
                     'mcp__t3-code__run_scheduled_task_now', 'Workflows', 'RemoteTriggers', 'mcp__t3-code__t3_project_read',
                     'mcp__t3-code__t3_project_delete'):
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

    # ---- Claude Code's own model keys (#329, live-tested; README "Claude Code's model keys")

    OPUS = {'driver': 'claudeAgent', 'model': 'example-opus-5', 'efforts': ['high', 'ultracode'], 'aliases': ['opus']}

    def policy(self, mode='enforce', allow=None):
        allow = [LARGE, DEEP, PLAIN, self.OPUS] if allow is None else allow
        return self.m.parse_policy(json.dumps({'mode': mode, 'allow': allow}), 'policy.json')

    def test_model_settings_come_from_the_claude_rows(self):
        want = self.m.model_settings(self.policy())
        self.assertEqual(want['availableModels'], ['example-deep-2', 'example-large-1', 'example-opus-5'])
        self.assertIs(want['enforceAvailableModels'], True)
        self.assertEqual(want['maxEffortLevel'], 'xhigh')   # ultracode runs at xhigh, above high
        self.assertEqual(want['env'], {'CLAUDE_CODE_SUBAGENT_MODEL': '', 'CLAUDE_CODE_SUBAGENT_MODEL_FORCE': '',
                                       'CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL': '', 'CLAUDE_CODE_SUBAGENT_EFFORT': '',
                                       'ANTHROPIC_DEFAULT_OPUS_MODEL': 'example-opus-5'})
        self.assertEqual(self.m.model_settings(self.policy(allow=[dict(LARGE, efforts=['max', 'low'], defaultEffort='low')]))
                         ['maxEffortLevel'], 'max')
        for allow in ([dict(LARGE, efforts=['turbo'], defaultEffort='turbo')], [{'driver': 'claudeAgent', 'model': 'm'}]):
            self.assertNotIn('maxEffortLevel', self.m.model_settings(self.policy(allow=allow)))

    def test_every_claude_code_alias_is_pinned(self):
        """#589 review: each of Claude Code's own aliases gets its pin, not only opus; other aliases get none."""
        self.assertEqual(self.m.FAMILY_ALIASES, ('opus', 'sonnet', 'haiku', 'fable'))
        for alias in ('opus', 'sonnet', 'haiku', 'fable'):
            with self.subTest(alias):
                env = self.m.model_settings(self.policy(allow=[dict(DEEP, aliases=[alias, 'mine'])]))['env']
                self.assertEqual(env[f'ANTHROPIC_DEFAULT_{alias.upper()}_MODEL'], 'example-deep-2')
                self.assertEqual(sorted(k for k in env if k.startswith('ANTHROPIC_')),
                                 [f'ANTHROPIC_DEFAULT_{alias.upper()}_MODEL'])

    def test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put(self):
        policy = self.policy()
        want = self.m.model_settings(policy)
        p = self.m.model_settings_problems([('main', {'allowManagedHooksOnly': True})], policy)
        self.assertEqual(len(p), 1, p)
        for part in ("managed settings miss Claude Code's own model limits", 'availableModels is None',
                     'enforceAvailableModels is not true', "maxEffortLevel is None, not the rows' highest effort 'xhigh'",
                     "env CLAUDE_CODE_SUBAGENT_MODEL is None, not ''", "env ANTHROPIC_DEFAULT_OPUS_MODEL is None",
                     'Put these keys in managed-settings.json: ' + json.dumps(want, sort_keys=True)):
            self.assertIn(part, p[0])
        main = {k: v for k, v in want.items() if k != 'env'}
        good = [('main', dict(main, availableModels=list(reversed(want['availableModels'])))),
                ('d/a.json', [1]), ('d/b.json', {'env': want['env']}), ('d/c.json', {'env': {'OTHER': '1'}})]
        self.assertEqual(self.m.model_settings_problems(good, policy), [])
        for name, later, why in (
                ('a later file turns it off', {'enforceAvailableModels': False}, 'enforceAvailableModels is not true'),
                ('one model more', {'availableModels': want['availableModels'] + ['x']}, 'availableModels is'),
                ('higher effort', {'maxEffortLevel': 'max'}, "maxEffortLevel is 'max'"),
                ('a sub-agent model', {'env': {'CLAUDE_CODE_SUBAGENT_MODEL': 'x'}},
                 "env CLAUDE_CODE_SUBAGENT_MODEL is 'x'"),
                ('an alias moved', {'env': {'ANTHROPIC_DEFAULT_OPUS_MODEL': 'y'}}, 'ANTHROPIC_DEFAULT_OPUS_MODEL is'),
                ('not texts', {'availableModels': want['availableModels'] + [None]}, 'not a list of texts'),
                ('not a list', {'availableModels': 'example-opus-5'}, 'not a list of texts')):
            with self.subTest(name):
                self.assertIn(why, ' '.join(self.m.model_settings_problems(good + [('d/z.json', later)], policy)))
        # Claude Code may join the lists of all managed files: an earlier, wider list is reported too
        wider = [('main', dict(main, availableModels=want['availableModels'] + ['x'])),
                 ('d/b.json', {'env': want['env'], 'availableModels': want['availableModels']})]
        self.assertIn("in all files", ' '.join(self.m.model_settings_problems(wider, policy)))
        narrower = [('main', dict(main, availableModels=want['availableModels'][:1])),
                    ('d/b.json', {'env': want['env'], 'availableModels': want['availableModels']})]
        self.assertEqual(self.m.model_settings_problems(narrower, policy), [])

    def test_model_keys_are_needed_only_in_enforce_with_claude_rows(self):
        for mode in ('off', 'advise'):
            self.assertEqual(self.m.model_settings_problems([], self.policy(mode)), [])
        self.assertEqual(self.m.model_settings_problems([], self.policy(allow=[PLAIN])), [])
        self.assertEqual(self.m.model_settings_problems([], self.policy(allow=[])), [])
        self.assertNotEqual(self.m.model_settings_problems([], self.policy()), [])


class Contract(unittest.TestCase):
    def test_effort_ids_match_the_servers_list(self):
        """The hook finds the effort under the same option ids as T3's usage pacing gate."""
        path = os.path.join(REPO, 'apps', 'server', 'src', 'usage', 'UsagePacingGate.ts')
        with open(path, encoding='utf-8') as f:
            block = re.search(r'EFFORT_OPTION_IDS[^=]*= new Set\(\[(.*?)\]\)', f.read(), re.S)
        self.assertIsNotNone(block, f'EFFORT_OPTION_IDS not found in {path}: keep the hook\'s list in step')
        self.assertEqual(sorted(re.findall(r'"([^"]+)"', block.group(1))), sorted(model_guard.EFFORT_OPTION_IDS))

    def test_driver_effort_ids_are_read_by_the_servers_model_policy(self):
        """#731, #793: the server's model policy check (packages/contracts/src/modelPolicy.ts) requires a pick's effort
        under the id each built-in driver reads it from, as the hook does: its MODEL_POLICY_DRIVER_EFFORT_IDS is
        DRIVER_EFFORT_IDS entry for entry, both ways (a changed id on either side fails here; a general id such as
        effort no longer hides a wrong driver entry). Kept here (not in an app test) so app code names no fork-owned
        path (#254); the workflow runs this test when either side changes."""
        path = os.path.join(REPO, 'packages', 'contracts', 'src', 'modelPolicy.ts')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        general = re.search(r'export const MODEL_POLICY_EFFORT_OPTION_IDS[^=]*= \[(.*?)\];', source, re.S)
        drivers = re.search(r'const MODEL_POLICY_DRIVER_EFFORT_IDS[^=]*= \{(.*?)\};', source, re.S)
        self.assertIsNotNone(general, f'MODEL_POLICY_EFFORT_OPTION_IDS not found in {path}: keep the lists in step')
        self.assertIsNotNone(drivers, f'MODEL_POLICY_DRIVER_EFFORT_IDS not found in {path}: keep the lists in step')
        general = set(re.findall(r'"([^"]+)"', general.group(1)))
        drivers = dict(re.findall(r'(\w+): "([^"]+)"', drivers.group(1)))
        self.assertIn('effort', general)
        self.assertEqual(len(model_guard.DRIVER_EFFORT_IDS), 8)
        # Every entry, both ways: the server requires the effort under its entry, so each must be the guard's.
        self.assertEqual(drivers, model_guard.DRIVER_EFFORT_IDS,
                         'MODEL_POLICY_DRIVER_EFFORT_IDS and the guard\'s DRIVER_EFFORT_IDS differ: keep them in step')

    def test_agent_picking_options_are_the_servers_model_policy_ones(self):
        """#802: while a policy is on, the server refuses the options that pick an agent or a mode, as the hook does: its
        MODEL_POLICY_AGENT_OPTION_IDS (packages/contracts/src/modelPolicy.ts) is DRIVER_MODEL_OPTION_IDS entry for entry,
        and its MODEL_POLICY_SESSION_MODE_OPTION_ID is ACP's session mode (a server test keeps that one ACP's own
        constant). Kept here (not in an app test) so app code names no fork-owned path (#254)."""
        path = os.path.join(REPO, 'packages', 'contracts', 'src', 'modelPolicy.ts')
        with open(path, encoding='utf-8') as f:
            source = f.read()
        block = re.search(r'\bconst MODEL_POLICY_AGENT_OPTION_IDS[^=]*= \{(.*?)\};', source, re.S)
        mode = re.search(r'export const MODEL_POLICY_SESSION_MODE_OPTION_ID = "([^"]+)";', source)
        self.assertIsNotNone(block, f'MODEL_POLICY_AGENT_OPTION_IDS not found in {path}: keep the lists in step')
        self.assertIsNotNone(mode, f'MODEL_POLICY_SESSION_MODE_OPTION_ID not found in {path}: keep the ids in step')
        server = {driver: tuple(re.findall(r'"([^"]+)"', ids))
                  for driver, ids in re.findall(r'(\w+): \[([^\]]*)\]', block.group(1))}
        self.assertTrue(server, 'MODEL_POLICY_AGENT_OPTION_IDS reads as empty: keep the lists in step')
        self.assertEqual(server, model_guard.DRIVER_MODEL_OPTION_IDS,
                         'MODEL_POLICY_AGENT_OPTION_IDS and the guard\'s DRIVER_MODEL_OPTION_IDS differ: keep them in step')
        self.assertEqual(mode.group(1), model_guard.ACP_SESSION_MODE_ID)

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

    def test_the_bundled_t3_model_list_reads_as_the_self_check_reads_it(self):
        """#600: T3's own model list (the one it ships, and caches in <stateDir>/model-manifest.json as {fetchedAtMs,
        manifest}) has the shape the self-check reads: a model with the effort ultracode and one without."""
        with open(os.path.join(REPO, 'apps', 'server', 'src', 'provider', 'ModelManifest.ts'), encoding='utf-8') as f:
            source = f.read()
        self.assertIn('path.join(config.stateDir, "model-manifest.json")', source, 'the cache moved: update the README')
        self.assertRegex(source, r'fetchedAtMs: Schema\.Number,\s*manifest:', 'the cache shape changed')
        catalog, why = model_guard.read_t3_manifest(
            os.path.join(REPO, 'apps', 'server', 'src', 'provider', 'model-manifest.json'))
        self.assertIsNone(why)
        self.assertEqual(model_guard.ultracode_option_in(catalog, 'claude-opus-5-5'), 'option')
        self.assertEqual(model_guard.ultracode_option_in(catalog, 'claude-opus-4-7'), 'none')
        self.assertEqual(model_guard.ultracode_option_in(catalog, 'example-custom'), 'missing')
        # #643: the levels each Claude model offers read from the same list
        levels, why = model_guard.efforts_offered_in(catalog, 'claude-opus-5-5')
        self.assertIsNone(why)
        self.assertLessEqual({'low', 'medium', 'high', 'xhigh', 'max', 'ultracode'}, levels)
        levels, _ = model_guard.efforts_offered_in(catalog, 'claude-opus-4-5')
        self.assertIn('high', levels)
        self.assertNotIn('xhigh', levels)
        self.assertEqual(model_guard.efforts_offered_in(catalog, 'opus')[0],
                         model_guard.efforts_offered_in(catalog, 'claude-opus-5-5')[0])     # by an alias
        self.assertEqual(model_guard.efforts_offered_in(catalog, 'example-custom'), (None, 'missing'))

    def test_t3_model_list_has_option_lists_for_claude_models_only(self):
        """#643: the self-check compares only Claude rows' levels because T3's model list gives option lists only to
        Claude models (the others' come from the provider while T3 runs). If another driver gains them, compare its
        rows too and update the README ("list only the levels the model offers")."""
        with open(os.path.join(REPO, 'apps', 'server', 'src', 'provider', 'model-manifest.json'), encoding='utf-8') as f:
            providers = json.load(f)['providers']
        with_lists = sorted(driver for driver, catalog in providers.items()
                            if any((p.get('capabilities') or {}).get('optionDescriptors')
                                   for p in (catalog.get('profiles') or {}).values()))
        self.assertEqual(with_lists, [model_guard.CLAUDE_DRIVER])

    def test_claude_rows_take_the_option_ids_t3_ships_for_claude(self):
        """#618 review: a Claude row takes only the option ids T3's Claude models have (the select "effort", read as
        the effort, and the others) plus ultracode, which T3 adds to the models whose efforts include it."""
        with open(os.path.join(REPO, 'apps', 'server', 'src', 'provider', 'model-manifest.json'), encoding='utf-8') as f:
            claude = json.load(f)['providers']['claudeAgent']
        ids, selects = set(), set()
        for profile in claude['profiles'].values():
            for d in profile['capabilities']['optionDescriptors']:
                ids.add(d['id'])
                if d['id'] == 'effort':
                    selects.add(d['type'])
        self.assertEqual(ids | {model_guard.ULTRACODE_OPTION_ID}, set(model_guard.CLAUDE_OPTION_IDS),
                         "T3's Claude option ids changed: update CLAUDE_OPTION_IDS and the README")
        self.assertEqual(selects, {'select'})
        self.assertIn(model_guard.CLAUDE_EFFORT_ID, model_guard.EFFORT_OPTION_IDS)
        # the id Claude reads its effort from, and the ids it reads only as true/false, from T3's code (#618 review,
        # #629)
        with open(os.path.join(REPO, 'apps', 'server', 'src', 'claudeModelOptions.ts'), encoding='utf-8') as f:
            source = f.read()
        effort = re.findall(r'rawEffort = getModelSelectionStringOptionValue\(selection, "([^"]+)"\)', source)
        self.assertEqual(effort, [model_guard.CLAUDE_EFFORT_ID], "T3's Claude effort id changed: update the guard")
        booleans = re.findall(r'getModelSelectionBooleanOptionValue\(selection, "([^"]+)"\)', source)
        self.assertEqual(sorted(booleans), sorted(model_guard.CLAUDE_BOOLEAN_IDS),
                         "T3's Claude true/false ids changed: update CLAUDE_BOOLEAN_IDS and the README")
        self.assertLessEqual(set(booleans), set(model_guard.CLAUDE_OPTION_IDS))

    # #629: each entry of T3's BUILT_IN_DRIVERS: the file that names its driver kind, and for a driver with a fixed
    # effort id the files where it reads the effort (the {} in each pattern is the id). A driver T3 adds fails the test
    # until it is put here and in DRIVER_EFFORT_IDS or AGENT_NAMED_EFFORT_DRIVERS.
    READ = r'getModelSelectionStringOptionValue\(\s*[\w.]+,\s*"{}"\s*\)'
    BUILT_IN = {
        'ClaudeDriver': ('apps/server/src/provider/Drivers/ClaudeDriver.ts',
                         [('apps/server/src/claudeModelOptions.ts', READ)]),
        'CodexDriver': ('apps/server/src/provider/Drivers/CodexDriver.ts',
                        [('apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts', READ)]),
        'FxDriver': ('packages/provider-fx/src/client.ts', [
            ('packages/provider-fx/src/server/acpSupport.ts', r'const FX_EFFORT_CONFIG_ID = "{}";'),
            ('packages/provider-fx/src/server/acpSupport.ts',
             r'getModelSelectionStringOptionValue\(modelSelection, FX_EFFORT_CONFIG_ID\)')]),
        'GrokDriver': ('packages/provider-grok/src/client.ts', [
            ('packages/provider-grok/src/server/adapter.ts', r'const GROK_REASONING_EFFORT_OPTION_ID = "{}";'),
            ('packages/provider-grok/src/server/adapter.ts',
             r'getModelSelectionStringOptionValue\(\s*modelSelection,\s*GROK_REASONING_EFFORT_OPTION_ID,?\s*\)')]),
        'KiroDriver': ('packages/provider-kiro/src/client.ts', [('packages/provider-kiro/src/server/adapter.ts', READ)]),
        'MuseDriver': ('packages/provider-muse/src/client.ts', [('packages/provider-muse/src/server/adapter.ts', READ)]),
        # OpenCode reads its effort in one helper, openCodeVariant, which both adapters call
        'OpenCodeDriver': ('packages/provider-opencode/src/client.ts', [
            ('packages/provider-opencode/src/server/OpenCodeRuntime.ts',
             r'export function openCodeVariant\(selection: ModelSelection\)[^{]*\{\s*'
             r'const variant = getModelSelectionStringOptionValue\(selection, "{}"\);'),
            ('packages/provider-opencode/src/server/adapter.ts',
             r'OpenCodeRuntime\.openCodeVariant\(turn\.modelSelection\)'),
            ('packages/provider-opencode/src/server/v2/adapter.ts', r'OpenCodeRuntime\.openCodeVariant\(selection\)')]),
        'PiDriver': ('packages/provider-pi/src/client.ts', [('packages/provider-pi/src/server/adapter.ts', READ)]),
        'CursorDriver': ('packages/provider-cursor/src/client.ts', None),
        'AntigravityDriver': ('apps/server/src/provider/Drivers/AntigravityDriver.ts', None),
        'AcpRegistryDriver': ('packages/provider-acp-registry/src/client.ts', None),
    }

    def read_repo(self, path):
        with open(os.path.join(REPO, *path.split('/')), encoding='utf-8') as f:
            return f.read()

    def test_each_built_in_driver_reads_its_effort_where_the_guard_looks(self):
        """#629 (major): the guard pins the effort id of each of T3's built-in drivers. Read from T3's code: the list
        of drivers, each one's kind, and where each with a fixed id reads its effort. It checks that the read is
        there, not that it wins: a T3 change that reads a new id first (X ?? reasoningEffort) still passes. That is
        safe today, because a pick naming the new id is refused as unread unless the row lists it, and NOT_OTHER keeps
        names with effort, reason or think out of otherOptionIds."""
        block = re.search(r'BUILT_IN_DRIVERS[^=]*= \[(.*?)\];',
                          self.read_repo('apps/server/src/provider/builtInDrivers.ts'), re.S)
        self.assertIsNotNone(block, 'BUILT_IN_DRIVERS not found: keep the guard in step')
        names = re.findall(r'\b(\w+Driver)\b', block.group(1))
        # #638 review (lens A minor 2): an entry not written as a plain name (a spread, a call, a condition) would
        # hide drivers from the list above: anything but names, commas, spaces and comments fails the test
        rest = re.sub(r'//[^\n]*|/\*.*?\*/', '', block.group(1), flags=re.S)
        rest = re.sub(r'\b\w+Driver\b|[,\s]', '', rest)
        self.assertEqual(rest, '', "BUILT_IN_DRIVERS holds an entry the test cannot read: read it, and put each "
                         'driver it adds in DRIVER_EFFORT_IDS or AGENT_NAMED_EFFORT_DRIVERS, and here')
        self.assertEqual(sorted(names), sorted(self.BUILT_IN), "T3's built-in drivers changed: put each one in "
                         'DRIVER_EFFORT_IDS or AGENT_NAMED_EFFORT_DRIVERS, and here')
        kinds = {}
        for name, (kind_file, reads) in self.BUILT_IN.items():
            with self.subTest(driver=name):
                found = re.findall(r'ProviderDriverKind\.make\("([^"]+)"\)', self.read_repo(kind_file))
                self.assertEqual(len(found), 1, f'{kind_file}: the driver kind of {name} is not found once')
                kind = kinds[name] = found[0]
                if reads is None:
                    self.assertIn(kind, model_guard.AGENT_NAMED_EFFORT_DRIVERS)
                    self.assertNotIn(kind, model_guard.DRIVER_EFFORT_IDS)
                    continue
                effort_id = model_guard.DRIVER_EFFORT_IDS[kind]
                for path, pattern in reads:
                    self.assertRegex(self.read_repo(path), pattern.replace('{}', re.escape(effort_id)),
                                     f'{path}: {kind} no longer reads its effort from {effort_id!r}: update '
                                     'DRIVER_EFFORT_IDS and the README')
        self.assertEqual(sorted(kinds.values()), sorted(set(model_guard.DRIVER_EFFORT_IDS) |
                                                        set(model_guard.AGENT_NAMED_EFFORT_DRIVERS)))
        self.assertFalse(set(model_guard.DRIVER_EFFORT_IDS) & set(model_guard.AGENT_NAMED_EFFORT_DRIVERS))
        self.assertEqual(kinds['ClaudeDriver'], model_guard.CLAUDE_DRIVER)
        # an OpenCode agent may set its own model: the guard never takes it as an option that changes neither
        self.assertRegex(self.read_repo('packages/provider-opencode/src/server/adapter.ts'),
                         r'getModelSelectionStringOptionValue\(turn\.modelSelection, "agent"\)')
        # a Kiro mode switches Kiro's agent (#638 review, lens B minor 3)
        self.assertRegex(self.read_repo('packages/provider-kiro/src/server/acpSupport.ts'),
                         r'export const KIRO_AGENT_CONFIG_ID = "mode";')
        self.assertRegex(self.read_repo('packages/provider-kiro/src/server/adapter.ts'),
                         r'getModelSelectionStringOptionValue\(modelSelection, KIRO_AGENT_CONFIG_ID\)')
        self.assertEqual(model_guard.DRIVER_MODEL_OPTION_IDS, {'kiro': ('mode',), 'opencode': ('agent',)})
        self.assertEqual(sorted(model_guard.BUILT_IN_DRIVER_KINDS), sorted(kinds.values()))

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
    'the self-check names the lock': ("'left to T3, whose managed policy locks modelPolicy' if why is None",
                                      "'left to T3, whose managed policy locks modelPolicy' if False",
                                      'test_the_self_check_says_who_checks_the_server_routes'),
    'server routes left to a locked T3 policy': ('    if kind in SERVER_CHECKED_TOOLS and lock is not None and lock(policy) is None:\n        return None\n', '',
        'test_a_server_route_is_left_to_t3_while_its_managed_policy_locks_it'),
    'only a server route is left to T3': ('    if kind in SERVER_CHECKED_TOOLS and lock', '    if lock',
        'test_a_server_route_is_left_to_t3_while_its_managed_policy_locks_it'),
    'the lock must say locked': ("entry.get('locked') is not True", 'False',
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'the lock must have a mode and rows': ("if locked_mode not in MODES or not isinstance(value.get('allow'), list):", 'if False:',
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'the lock must be at least as strict': ('if MODES.index(locked_mode) < MODES.index(mode):', 'if False:',
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'the locked rows must be within the guard rows': ('    if wider is not None:\n', '    if False:\n',
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'the locked efforts must be within the guard row': (" and all(e in h['efforts'] for e in efforts)", '',
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'the lock file is read as a protected file': ('machine_file.read_protected_text(path, owner_uid=owner_uid)', "open(path, encoding='utf-8').read()",
        'test_without_a_usable_lock_the_guard_keeps_checking_the_server_routes'),
    'broken policy fails closed': ("        _record(record, payload, reason)\n        return _out('deny', reason)",
                                   "        _record(record, payload, reason)\n        return _out(None, reason)",
                                   'test_missing_policy_file_refuses_and_names_it'),
    'enforce refuses': ("if policy['mode'] == 'enforce':", "if False:",
                        'test_delegate_task_model_and_effort_must_be_in_a_row'),
    'model must be in a row': ('    if not found:\n', '    if False:\n',
                               'test_delegate_task_model_and_effort_must_be_in_a_row'),
    'inherit is no model': (" or js_trim(model) == 'inherit':", ':', 'test_agent_must_name_an_allowed_model_and_effort'),
    'effort must be named': ('        if not efforts:\n', '        if False:\n',
                             'test_agent_must_name_an_allowed_model_and_effort'),
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
    'Workflow is checked': ("'Task': 'agent', 'Workflow': 'workflow',", "'Task': 'agent',",
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
    'Workflow: every / that opens no comment refused': (
        '            fail(WF_SLASH)\n', "            toks.append(['punct', '/', line, None])\n            i += 1\n",
        'test_workflow_division_and_regular_expressions_are_refused'),
    'Workflow: the hole scripts hit the / rule': (
        '            fail(WF_SLASH)\n', "            toks.append(['punct', '/', line, None])\n            i += 1\n",
        'test_workflow_hole_scripts_of_the_lenses_and_the_audit_are_refused'),
    'Workflow: odd line ends refused': ('    if odd:\n', '    if False:\n',
                                        'test_workflow_hole_scripts_of_the_lenses_and_the_audit_are_refused'),
    'Workflow forbidden names': ("            if kind == 'name' and text in WF_FORBIDDEN:",
                                 '            if False:', 'test_workflow_that_cannot_be_read_is_refused'),
    'transfer fork refused': ("    if kind == 't3_thread_transfer' and args.get('action') == 'fork':",
                              '    if False:', 'test_thread_transfer_fork_is_refused'),
    'send and schedule take no model': ('            if isinstance(key, str) and MODEL_FIELD.fullmatch(key):',
                                        '            if False:', 'test_send_and_schedule_take_no_model'),
    'model fields found by name': ("re.compile(r'.*(model|effort|", "re.compile(r'(model|effort|",
                                   'test_send_and_schedule_take_no_model'),
    'ultracode runs as ultracode': ('    if True in ultracode:', '    if False:',
                                    'test_ultracode_is_checked_as_the_effort_that_runs'),
    'old ultracode effort is xhigh': ('    if ULTRACODE_EFFORT in efforts:', '    if False:',
                                      'test_ultracode_is_checked_as_the_effort_that_runs'),
    'a row without efforts names none': ("            if efforts:\n                problems.append(f'{where}: effort",
                                         "            if False:\n                problems.append(f'{where}: effort",
                                         'test_a_row_without_efforts_lets_no_effort_through'),
    'no driver: every row accepts': ('    for row in found:\n', '    for row in found[:1]:\n',
                                     'test_without_a_driver_every_row_of_the_model_must_accept_it'),
    # #329
    'RemoteTrigger is checked': (", 'RemoteTrigger': 'remote'}", '}', 'test_remote_trigger_may_only_read'),
    'RemoteTrigger may only read': ('        if action not in REMOTE_READ_ACTIONS:', '        if False:',
                                    'test_remote_trigger_may_only_read'),
    'project tools are checked': ("'schedule_task', 't3_project_create',\n            't3_project_update')",
                                  "'schedule_task')", 'test_project_default_model_is_checked_when_given'),
    'project default model checked': ("    elif kind in ('t3_project_create', 't3_project_update') and",
                                      '    elif False and', 'test_project_default_model_is_checked_when_given'),
    'ultracode must be named': ('and ULTRACODE_OPTION_ID not in (key for key, _ in pairs)', 'and False',
                                'test_t3_picks_must_name_ultracode_unless_the_row_allows_it'),
    'only rows without ultracode': ("and ULTRACODE_EFFORT not in (r['efforts'] or ())", '',
                                    'test_t3_picks_must_name_ultracode_unless_the_row_allows_it'),
    'only Claude picks need ultracode': ('        if driver in (None, CLAUDE_DRIVER) and', '        if',
                                         'test_t3_picks_must_name_ultracode_unless_the_row_allows_it'),
    # #589 review: rows without T3's ultracode option, and options read the way T3 reads them
    'a row without the ultracode option needs none': ("r['model'] == js_trim(model) and r['ultracodeOption']",
                                                       "r['model'] == js_trim(model) and True",
                                                       'test_a_row_without_the_ultracode_option_needs_none'),
    'ultracodeOption only on Claude rows': ("        if not row['ultracodeOption'] and row['driver'] != CLAUDE_DRIVER:",
                                            '        if False:', 'test_a_row_without_the_ultracode_option_needs_none'),
    'ultracodeOption false cannot run ultracode': (
        "        if not row['ultracodeOption'] and ULTRACODE_EFFORT in", '        if False and ULTRACODE_EFFORT in',
        'test_a_row_without_the_ultracode_option_needs_none'),
    'option ids are trimmed': ("        name = js_trim(key) if isinstance(key, str) else ''",
                               "        name = key if isinstance(key, str) else ''",
                               'test_options_are_read_the_way_t3_reads_them'),
    'option values are trimmed': ('            value = js_trim(value)\n', '            pass\n',
                                  'test_options_are_read_the_way_t3_reads_them'),
    'an id twice is refused': ('        if name in seen:\n', '        if False:\n',
                               'test_options_are_read_the_way_t3_reads_them'),
    'an empty id is refused': ('        if not name:\n', '        if False:\n',
                               'test_options_are_read_the_way_t3_reads_them'),
    'a value T3 drops is refused': ('        elif not isinstance(value, bool):\n', '        elif False:\n',
                                    'test_options_are_read_the_way_t3_reads_them'),
    'ultracode must be true or false': ('        if name == ULTRACODE_OPTION_ID and not isinstance(value, bool):',
                                        '        if False:', 'test_options_are_read_the_way_t3_reads_them'),
    "JavaScript's trim": ('    return text.strip(JS_SPACE)', '    return text.strip()',
                          'test_options_are_read_the_way_t3_reads_them'),
    'the policy is not in the guard\'s /etc folder': (
        "        if in_folder(v, ETC_DIR):", '        if False:',
        'test_the_policy_may_not_live_in_the_guards_own_folder'),
    'newer policy: update the kit': ("and data['version'] > POLICY_VERSION:", 'and False:',
                                     'test_a_newer_policy_says_update_the_kit_in_every_mode'),
    'policy version is 1 or more': ('None if v >= 1 else', 'None if True else',
                                    'test_a_newer_policy_says_update_the_kit_in_every_mode'),
    'enforce refusals recorded': ("        _record(record, payload, f'enforce: refused", "        (record, payload, f'",
                                  'test_refusals_and_advise_notes_are_recorded'),
    'advise notes recorded': ("    _record(record, payload, f'advise:", "    (record, payload, f'",
                              'test_refusals_and_advise_notes_are_recorded'),
    'policy errors recorded': ('        _record(record, payload, reason)\n', '        (record, payload, reason)\n',
                               'test_refusals_and_advise_notes_are_recorded'),
    'bad input recorded': ('        _record(record, None, reason)\n', '        (record, None, reason)\n',
                           'test_refusals_and_advise_notes_are_recorded'),
    'record is cut': ('        record(line[:RECORD_MAX])', '        record(line)',
                      'test_refusals_and_advise_notes_are_recorded'),
    'a failing record changes nothing': ('    except Exception:  # noqa: BLE001', '    except ZeroDivisionError:  # noqa: BLE001',
                                         'test_a_record_that_fails_never_changes_the_answer'),
    'the hook records to the system log': ('owner_uid=0, record=syslog_record, t3_policy=None)', 'owner_uid=0, record=None, t3_policy=None)',
                                           'test_the_default_record_is_the_system_log'),
    'Workflow: of[ is a member access': ("and (prev[1] != 'of' or k - 1 in of_words)", '',
                                         'test_workflow_of_and_keyword_properties_before_a_bracket'),
    'Workflow: a keyword after a dot is a name': ('in WF_EXPRESSION_WORDS \\\n                    and not dot_before(k - 1)',
                                                  'in WF_EXPRESSION_WORDS \\\n                    and True',
                                                  'test_workflow_of_and_keyword_properties_before_a_bracket'),
    'Workflow: the for-of keyword': ('                of_words.add(k)\n', '                pass\n',
                                     'test_workflow_of_and_keyword_properties_before_a_bracket'),
    'Workflow: only the first of': ("and not heads[-1]['of'] and", 'and',
                                    'test_workflow_of_and_keyword_properties_before_a_bracket'),
    'Workflow: agent only as a direct call': ("                if k + 1 >= len(toks) or toks[k + 1][:2] != ['punct', '(']:",
                                              '                if False:', 'test_workflow_that_cannot_be_read_is_refused'),
    # #589 round 2
    'a model selection refuses driverKind': ("        if selection and 'driverKind' in value:",
                                             '        if False:', 'test_a_model_selection_never_trusts_driver_kind'),
    'launch is a model selection': ("        target('modelSelection', args.get('modelSelection'), selection=True)\n"
                                    "    elif kind == 't3_thread_configure'",
                                    "        target('modelSelection', args.get('modelSelection'))\n"
                                    "    elif kind == 't3_thread_configure'",
                                    'test_a_model_selection_never_trusts_driver_kind'),
    'configure is a model selection': ("the call only reads the thread\n        target('modelSelection', "
                                       "args.get('modelSelection'), selection=True)",
                                       "the call only reads the thread\n        target('modelSelection', "
                                       "args.get('modelSelection'))", 'test_a_model_selection_never_trusts_driver_kind'),
    'a project default is a model selection': ("args.get('defaultModelSelection'), selection=True)",
                                               "args.get('defaultModelSelection'))",
                                               'test_a_model_selection_never_trusts_driver_kind'),
    'the guard folder resolved': ("    names = {os.path.normpath(folder), os.path.realpath(folder)}",
                                  "    names = {os.path.normpath(folder)}", 'test_the_guards_folder_is_found_through_links'),
    'the policy path resolved': ("    for name in (os.path.normpath(path), real):", "    for name in (os.path.normpath(path),):",
                                 'test_the_guards_folder_is_found_through_links'),
    'the guard folder by identity': ("            if os.path.samestat(os.stat(real), want):",
                                     "            if False:", 'test_the_guards_folder_is_found_by_identity'),
    # #601: a driver's own option ids
    'unread option ids refused': ('            if unread:\n', '            if False:\n',
                                  'test_a_drivers_own_reasoning_option_is_read_or_refused'),
    "a row's effort ids are read": ('effort_ids = EFFORT_OPTION_IDS + own', 'effort_ids = EFFORT_OPTION_IDS',
                                    'test_a_drivers_own_reasoning_option_is_read_or_refused'),
    "a row's effort ids must be named": ("            if missing and row['efforts'] is not None:",
                                         '            if False:',
                                         'test_a_pick_names_its_effort_where_the_driver_reads_it'),
    'a Claude pick names effort': ('own, effort_ids = (CLAUDE_EFFORT_ID,), EFFORT_OPTION_IDS',
                                   'own, effort_ids = (), EFFORT_OPTION_IDS',
                                   'test_a_pick_names_its_effort_where_the_driver_reads_it'),
    'Claude rows take only ids Claude reads': ("            if unread and row['driver'] == CLAUDE_DRIVER:",
                                               '            if False:',
                                               'test_claude_rows_refuse_ids_claude_does_not_read'),
    'effort ids need efforts': ("        if effort_ids and row['efforts'] is None:", '        if False:',
                                'test_option_id_keys_are_checked'),
    'the session mode is not other': ('        if ACP_SESSION_MODE_ID in other_ids:', '        if False:',
                                      'test_option_id_keys_are_checked'),
    'other ids name no model or reasoning': ('        if named:\n', '        if False:\n',
                                             'test_option_id_keys_are_checked'),
    "a row's effort ids count as efforts": ('            efforts = _efforts_in(pairs, effort_ids)',
                                            '            efforts = _efforts_in(pairs)',
                                            'test_a_drivers_own_reasoning_option_is_read_or_refused'),
    'option id keys only off Claude': ("            if ids and row['driver'] == CLAUDE_DRIVER:", '            if False:',
                                       'test_option_id_keys_are_checked'),
    'option id keys never ultracode': ('            if ULTRACODE_OPTION_ID in ids:', '            if False:',
                                       'test_option_id_keys_are_checked'),
    'an effort id is no other id': ('        if both:\n', '        if False:\n', 'test_option_id_keys_are_checked'),
    'option ids are trimmed': ("    if problem is None and any(v != js_trim(v) for v in value):", '    if False:',
                               'test_option_id_keys_are_checked'),
    # #600: T3's model list against "ultracodeOption": false rows
    'the option is reported': ("            if state == 'option':", '            if False:',
                               'test_t3_model_list_drift_is_reported'),
    'no option list is reported': ("            elif state == 'no list':", '            elif False:',
                                   'test_t3_model_list_drift_is_reported'),
    'a missing model is reported': ("            elif state == 'missing':", '            elif False:',
                                    'test_t3_model_list_drift_is_reported'),
    'no named list is reported': ('    if not manifest_paths:\n        return [', '    if not manifest_paths:\n'
                                  '        return []\n        return [', 'test_t3_model_list_drift_is_reported'),
    'an unusable list is reported': ("        if why:\n            problems.append(f'T3 model list",
                                     "        if why:\n            continue\n            problems.append(f'T3 model list",
                                     'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'the effort choice counts': ("any(isinstance(c, dict) and c.get('id') == ULTRACODE_EFFORT for c in choices)",
                                 'False', 'test_t3_model_list_drift_is_reported'),
    'aliases count': ("isinstance(a, str) and js_trim(a).lower() == model.lower() for a in aliases)",
                      'False for a in aliases)', 'test_t3_model_list_drift_is_reported'),
    'the worst entry wins': ("            found = 'no list'", "            return 'no list'",
                             'test_t3_model_list_drift_is_reported'),
    'no links': ("os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0)", 'os.O_RDONLY | os.O_NONBLOCK',
                 'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'plain files only': ('        if not stat.S_ISREG(os.fstat(fd).st_mode):', '        if False:',
                         'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'a size limit': ('    if len(raw) > T3_MANIFEST_MAX:', '    if False:',
                     'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'version 1 only': ("data.get('version') != 1 or isinstance(data.get('version'), bool)", 'False',
                       'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'off needs no list': ("    if not rows or policy['mode'] == 'off':", '    if not rows:',
                          'test_t3_model_list_drift_is_reported'),
    # #643: the levels a Claude row allows against T3's model list
    'unoffered levels are reported': ('                missing = [e for e in row[\'efforts\'] if e not in offered]',
                                      '                missing = []', 'test_t3_model_list_levels_are_compared'),
    'a level is offered only by each entry': ('        offered = levels if offered is None else offered & levels',
                                              '        offered = levels if offered is None else offered | levels',
                                              'test_t3_model_list_levels_are_compared'),
    'only the effort select offers levels': ("d.get('id') == CLAUDE_EFFORT_ID\n", "True\n",
                                             'test_t3_model_list_levels_are_compared'),
    'ultracode by its option': ('            levels.add(ULTRACODE_EFFORT)', '            pass',
                                'test_t3_model_list_levels_are_compared'),
    'a model without options is reported': ('    if None in entries:\n        return None, \'no list\'',
                                            '    if None in entries:\n        return frozenset(), None',
                                            'test_t3_model_list_levels_are_compared'),
    'levels: no named list is reported': ('    if not manifest_paths:\n        return ([f\'',
                                          '    if not manifest_paths:\n        return [], []\n        return ([f\'',
                                          'test_t3_model_list_levels_are_compared'),
    'levels: an unusable list is reported': ('        if why is not None:', '        if False:',
                                             'test_t3_model_list_levels_are_compared'),
    'levels: off needs no list': ("    if policy['mode'] == 'off' or not rows:", '    if not rows:',
                                  'test_t3_model_list_levels_are_compared'),
    'levels: only a select offers levels': ("\n                  and d.get('type') == 'select' and", ' and',
                                             'test_t3_model_list_levels_are_compared'),
    'levels: ultracode stands for xhigh': ('            levels.add(ULTRACODE_RUNS_AT)', '            pass',
                                           'test_t3_model_list_levels_are_compared'),
    'levels: a list with none of the models': ("        if all(state == 'missing' for _, state in found.values()):",
                                               '        if False:', 'test_t3_model_list_levels_are_compared'),
    'a slug before an alias': ('    named = named or [e for e in entries if alias_of(e)][:1]',
                               '    named = named + [e for e in entries if alias_of(e)][:1]',
                               'test_t3_model_list_levels_are_compared'),
    'only the first alias': ('    named = named or [e for e in entries if alias_of(e)][:1]',
                             '    named = named or [e for e in entries if alias_of(e)]',
                             'test_t3_model_list_levels_are_compared'),
    'levels: Claude rows only': ("r['driver'] == CLAUDE_DRIVER and r['efforts']]", "r['efforts']]",
                                 'test_t3_model_list_levels_are_compared'),
    'the machine file paths are checked': (
        "        if not v or any(not isinstance(p, str) or not os.path.isabs(p) or '\\0' in p for p in v):",
        '        if False:', 'test_t3_model_lists_in_the_machine_file_are_checked'),
    'no NUL in a machine file list': ("or not os.path.isabs(p) or '\\0' in p for p in v", 'or not os.path.isabs(p) for p in v',
                                      'test_t3_model_lists_in_the_machine_file_are_checked'),
    'no NUL in the policy path': ("        if not os.path.isabs(v) or '\\0' in v:", '        if not os.path.isabs(v):',
                                  'test_t3_model_lists_in_the_machine_file_are_checked'),
    'a pipe never blocks the read': ('os.O_RDONLY | os.O_NONBLOCK | ', 'os.O_RDONLY | ',
                                     'test_a_t3_model_list_that_cannot_be_used_is_said'),
    'the first entry with the option decides': ("            return 'option'\n", "            found = 'option'\n",
                                                'test_t3_model_list_drift_is_reported'),
    "only T3's own cache is unwrapped": ("'manifest' in data and 'version' not in data", "'manifest' in data",
                                        'test_a_t3_model_list_that_cannot_be_used_is_said'),
    # #629: the effort id of each built-in driver; drivers with none known fail closed
    'a built-in driver reads its pinned id': ("    known = DRIVER_EFFORT_IDS.get(row['driver'])\n", '    known = None\n',
                                              'test_a_built_in_drivers_effort_id_is_known_without_the_row_listing_it'),
    'the row ids add to the pinned one': ("+ tuple(row['effortOptionIds'] or ())))", '))',
                                          'test_a_built_in_drivers_effort_id_is_known_without_the_row_listing_it'),
    'no known id fails closed': ("                if not own and row['efforts'] is not None:", '                if False:',
                                 'test_a_driver_the_guard_knows_no_effort_id_for_fails_closed'),
    'the self-check names rows with no id': ("for row in policy['allow'] if row['efforts'] is not None and not "
                                             "row_effort_ids(row)]", "for row in policy['allow'] if False]",
                                             'test_the_self_check_names_rows_with_no_known_effort_id'),
    'the self-check needs no id in off': ("    if policy['mode'] == 'off':\n        return []\n    return [f'",
                                          "    return [f'", 'test_the_self_check_names_rows_with_no_known_effort_id'),
    'the pinned id is no other option': ("\n                or i == DRIVER_EFFORT_IDS.get(row['driver'])]", ']',
                                         'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'an OpenCode agent is no other option': (" or i in DRIVER_MODEL_OPTION_IDS.get(row['driver'], ())]", ']',
                                             'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'NOT_OTHER instance': ("r'model|effort|reason|think|thought|ultracode|provider|driver|instance', re.I)", "r'model|effort|reason|think|thought|ultracode|provider|driver', re.I)",
                           'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'NOT_OTHER provider': ("r'model|effort|reason|think|thought|ultracode|provider|driver|instance', re.I)", "r'model|effort|reason|think|thought|ultracode|driver|instance', re.I)",
                           'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'NOT_OTHER driver': ("r'model|effort|reason|think|thought|ultracode|provider|driver|instance', re.I)", "r'model|effort|reason|think|thought|ultracode|provider|instance', re.I)",
                         'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'NOT_OTHER ultracode': ("r'model|effort|reason|think|thought|ultracode|provider|driver|instance', re.I)", "r'model|effort|reason|think|thought|provider|driver|instance', re.I)",
                            'test_a_drivers_effort_or_model_id_is_never_an_other_option'),
    'Claude booleans': ("            if text and row['driver'] == CLAUDE_DRIVER:", '            if False:',
                        'test_claude_booleans_take_only_true_or_false'),
    'the true/false rule is for Claude rows only': ("            if text and row['driver'] == CLAUDE_DRIVER:",
                                                    "            if text and row['driver'] != 'pi':",
                                                    'test_the_true_false_rule_is_for_claude_rows_only'),
    # #638 review
    'a built-in kind in another case is refused': ('        if twin:\n', '        if False:\n',
                                                   'test_a_built_in_driver_in_another_case_is_refused'),
    'the kind is compared trimmed': ("k.casefold() == js_trim(row['driver']).casefold()]",
                                     "k.casefold() == row['driver'].casefold()]",
                                     'test_a_built_in_driver_in_another_case_is_refused'),
    'an agent option is no effort id': ('        if agents:\n', '        if False:\n',
                                        'test_an_agent_option_is_never_an_effort_id'),
    'a Kiro mode picks its agent': ("DRIVER_MODEL_OPTION_IDS = {'kiro': ('mode',), 'opencode': ('agent',)}",
                                    "DRIVER_MODEL_OPTION_IDS = {'opencode': ('agent',)}",
                                    'test_an_agent_option_is_never_an_effort_id'),
    'Claude booleans are only true or false': ("and not isinstance(value, bool)]", 'and isinstance(value, int)]',
                                               'test_claude_booleans_take_only_true_or_false'),
}
SETTINGS_MUTATIONS = {
    'the matcher is exactly the guard\'s': ("    if entry['matcher'] != HOOK_MATCHER:", '    if False:',
                                          'test_a_matcher_other_than_the_guards_fails'),
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
    # #329: Claude Code's own model keys
    'model keys only in enforce': ("    if policy['mode'] != 'enforce':\n        return []",
                                   "    if False:\n        return []",
                                   'test_model_keys_are_needed_only_in_enforce_with_claude_rows'),
    'availableModels checked': ("elif not lists or sorted(set(lists[-1])) != want['availableModels'] \\",
                                "elif False and sorted(set(lists[-1])) != want['availableModels'] \\",
                                'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'no earlier file names more models': ("or not {m for v in lists for m in v} <= set(want['availableModels']):",
                                          'or False:', 'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'availableModels must hold texts': (
        "    if any(not isinstance(v, list) or not all(isinstance(m, str) for m in v) for v in lists):",
        '    if False:', 'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'enforceAvailableModels checked': ("    if have.get('enforceAvailableModels') is not True:", '    if False:',
                                       'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'maxEffortLevel checked': ("have.get('maxEffortLevel') != want['maxEffortLevel']:", 'False:',
                               'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'env checked': ('        if env.get(name) != value:', '        if False:',
                    'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'later model keys win': ('                have[key] = data[key]', '                have.setdefault(key, data[key])',
                             'test_model_keys_missing_or_different_are_one_problem_with_the_keys_to_put'),
    'alias pins': ("                env[f'ANTHROPIC_DEFAULT_{alias.upper()}_MODEL'] = r['model']", '                pass',
                   'test_model_settings_come_from_the_claude_rows'),
    'neutral env': ("    env = {name: '' for name in NEUTRAL_ENV}", '    env = {}',
                    'test_model_settings_come_from_the_claude_rows'),
    'highest effort': ('max(known, key=EFFORT_ORDER.index)', 'min(known, key=EFFORT_ORDER.index)',
                       'test_model_settings_come_from_the_claude_rows'),
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
