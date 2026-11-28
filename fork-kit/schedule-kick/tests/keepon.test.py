"""schedule-kick's keep-on part (lib/schedulekick/keepon.py): turn the named workflows back on. A fake GitHub in the
process (the workflow list and the enable call), a fixed clock, a fake alert channel and a temporary state folder.
No root, no systemd, no network.

  python3 -I -B fork-kit/schedule-kick/tests/keepon.test.py

The rules: only the named workflows, only from the off states it knows, never a switch-off (the client has no such
call); one list call per entry a pass. The state question: runs again (on: no call; two passes at once: the second
does nothing), stops half-way (turned on, state not written: the next pass finds it on), old, broken or empty state
(read as empty), a target that is gone or in a state it does not know (error, alert at most once an hour), and API
errors (error and alert; a rate limit pauses). RedCheck breaks each rule in a copy of lib/schedulekick/ and shows
its test then fails.
"""
import datetime, fcntl, io, json, os, re, shutil, sys, tempfile, unittest, urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kick_fixture import LIB, FakeApi, at, github, keepon, kick, red_check  # noqa: E402

THIS = os.path.abspath(__file__)
NOW = at('2026-11-28T03:11:05')
REPO = 'example-owner/app'


def rows(**states):
    return [{'id': n + 1, 'path': f'.github/workflows/{name}.yml', 'state': state}
            for n, (name, state) in enumerate(states.items())]


class KeepOn(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='schedule-kick-keepon-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.folder = kick.state_folder(os.path.join(self.tmp, 'state'))
        self.api = FakeApi(NOW)
        self.alerts = []
        self.cfg = kick.Config('cfg.json', ('tokenFile', '/x'), [], [],
                               [kick.KeepOnEntry(REPO, ['fork-tests.yml', 'fork-follow.yml'])])

    def send(self, text):
        self.alerts.append(text)
        return True, ''

    def keep_on(self, now=NOW, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        code = keepon.run(cfg or self.cfg, self.folder, self.api, now, out, err, kick.write_json, kick.waiting_until,
                          kick.pause, kick.epoch, kick.MIN_REMAINING, send=self.send)
        return code, out.getvalue() + err.getvalue()

    def status(self):
        with open(os.path.join(self.folder, 'keep-on-status.json')) as f:
            return json.load(f)

    def enables(self):
        return [c[2] for c in self.api.calls if c[0] == 'enable']

    def test_a_switched_off_target_is_turned_back_on_with_an_alert_line(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        code, out = self.keep_on()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.enables(), [1])
        self.assertIn(f'schedule-kick keep-on: ALERT {REPO} fork-tests.yml: was disabled_manually: turned back on', out)
        self.assertEqual(self.status()['targets'][f'{REPO} fork-tests.yml']['result'], 're-enabled')
        self.assertEqual(self.alerts, [])                       # routine: the journal line, no message to the owner

    def test_only_the_named_workflows_are_touched(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'active', 'fork-follow': 'active',
                                                'nightly': 'disabled_manually', 'old': 'disabled_inactivity'})}
        self.assertEqual(self.keep_on()[0], 0)
        self.assertEqual(self.enables(), [])

    def test_inactivity_is_turned_back_on_too_but_no_other_state(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_inactivity', 'fork-follow': 'disabled_fork'})}
        code, out = self.keep_on()
        self.assertEqual(code, 1)
        self.assertEqual(self.enables(), [1])
        self.assertIn('state disabled_fork: keep-on turns on only', out)
        self.assertEqual(len(self.alerts), 1)

    def test_it_never_switches_a_workflow_off(self):
        # neither GitHub client has a call that could: no method, no path, no gh subcommand for it
        for cls in (github.TokenApi, github.GhAsApi):
            self.assertFalse([m for m in dir(cls) if 'disable' in m.lower()], cls)
        for name in ('github.py', 'keepon.py', 'heads.py', 'kick.py'):
            with open(os.path.join(LIB, 'schedulekick', name)) as f:
                code = f.read()
            self.assertIsNone(re.search(r"/disable\b|'disable'|workflow disable", code), name)

    def test_one_list_call_per_entry_a_pass_and_nothing_when_all_are_on(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'active', 'fork-follow': 'active'})}
        for _ in range(3):
            self.assertEqual(self.keep_on()[0], 0)
        self.assertEqual([c[0] for c in self.api.calls], ['workflows'] * 3)

    def test_runs_again_after_a_stop_half_way_finds_it_on(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        real = kick.write_json

        def stop(path, data):
            raise KeyboardInterrupt('stopped before the state was written')
        with self.assertRaises(KeyboardInterrupt):
            keepon.run(self.cfg, self.folder, self.api, NOW, io.StringIO(), io.StringIO(), stop, kick.waiting_until,
                       kick.pause, kick.epoch, kick.MIN_REMAINING, send=self.send)
        self.assertEqual(real.__name__, 'write_json')
        code, out = self.keep_on(NOW + datetime.timedelta(seconds=15))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.enables(), [1])                   # on already: not called again

    def test_two_passes_at_once_the_second_does_nothing(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        with open(os.path.join(self.folder, 'keep-on.lock'), 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
            code, out = self.keep_on()
        self.assertEqual((code, self.api.calls), (0, []))
        self.assertIn('another pass is running', out)

    def test_a_broken_state_file_reads_as_empty(self):
        with open(os.path.join(self.folder, 'keep-on.json'), 'w') as f:
            f.write('{not json')
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        code, out = self.keep_on()
        self.assertEqual(code, 0, out)
        self.assertIn('starting from empty state', out)
        self.assertEqual(self.enables(), [1])

    def test_a_missing_target_is_an_error_with_one_alert_an_hour(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'active'})}
        self.assertEqual(self.keep_on()[0], 1)
        self.assertEqual(self.keep_on(NOW + datetime.timedelta(minutes=30))[0], 1)
        self.assertEqual(len(self.alerts), 1)
        self.assertIn('fork-follow.yml: keep-on cannot keep it on: no such workflow', self.alerts[0])
        self.keep_on(NOW + datetime.timedelta(minutes=61))
        self.assertEqual(len(self.alerts), 2)

    def test_a_failed_turn_on_is_an_error_and_an_alert(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        self.api.fail['enable'] = github.ApiError('PUT ...: HTTP 403 Resource not accessible', status=403)
        code, out = self.keep_on()
        self.assertEqual(code, 1)
        self.assertIn('turning it back on failed', self.alerts[0])
        self.assertFalse(self.status()['ok'])

    def test_a_list_error_is_an_error_and_a_rate_limit_pauses(self):
        self.api.fail['workflows'] = github.ApiError('rate limited', rate_limited=True, retry_after=120)
        self.assertEqual(self.keep_on()[0], 1)
        del self.api.fail['workflows']
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'active', 'fork-follow': 'active'})}
        self.keep_on(NOW + datetime.timedelta(seconds=15))
        self.assertEqual(self.status()['targets'][f'{REPO} fork-tests.yml']['result'], 'paused')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'workflows']), 1)
        self.assertEqual(self.keep_on(NOW + datetime.timedelta(minutes=3))[0], 0)    # the pause is over

    def test_a_rate_limit_on_turning_on_pauses_and_stops_the_pass(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'disabled_manually'})}
        self.api.fail['enable'] = github.ApiError('HTTP 403 rate limit', rate_limited=True, retry_after=300, status=403)
        self.assertEqual(self.keep_on()[0], 1)
        self.assertEqual(len(self.enables()), 1)                # no second call after the limit
        with open(os.path.join(self.folder, 'keep-on.json')) as f:
            self.assertIn('pauseUntil', json.load(f))
        self.keep_on(NOW + datetime.timedelta(seconds=15))
        self.assertEqual([c[0] for c in self.api.calls], ['workflows', 'enable'])   # paused: no call

    def test_a_low_budget_never_shortens_a_rate_limit_pause(self):
        # retry-after 600 s, then the same answer's low budget with a reset in 60 s: the pause stays 600 s
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually', 'fork-follow': 'active'})}
        self.api.fail['enable'] = github.ApiError('HTTP 403 rate limit', rate_limited=True, retry_after=600, status=403)
        self.api.remaining, self.api.reset = 50, (NOW + datetime.timedelta(seconds=60)).timestamp()
        self.keep_on()
        with open(os.path.join(self.folder, 'keep-on.json')) as f:
            self.assertEqual(json.load(f)['pauseUntil'], github.iso(NOW + datetime.timedelta(seconds=600)))

    def test_a_refused_config_leaves_no_old_ok_status(self):
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'active', 'fork-follow': 'active'})}
        self.assertEqual(self.keep_on()[0], 0)
        path = os.path.join(self.tmp, 'config.json')
        with open(path, 'w') as f:
            f.write('{broken')
        self.assertEqual(kick.main(['keep-on', '--config', path, '--state-dir', self.folder], out=io.StringIO(),
                                   err=io.StringIO()), 2)
        self.assertEqual(kick.main(['status', '--keep-on', '--state-dir', self.folder], out=io.StringIO(),
                                   err=io.StringIO()), 1)

    def test_the_alert_channel_without_a_section_is_journal_only_and_never_raises(self):
        self.assertEqual(keepon.channel_alert('x', read_channel=lambda: (None, {})),
                         (False, 'no alert channel in the machine file (journal only)'))
        got = []

        def run(argv, **kw):
            got.append((argv, kw['input'], kw['env']))
            return type('R', (), {'returncode': 0})()
        section = {'command': ['/usr/bin/notify', '--topic', 'm'], 'timeoutSeconds': 30}
        self.assertEqual(keepon.channel_alert('text', read_channel=lambda: (section, {'K': 'v'}), run=run),
                         (True, ''))
        self.assertEqual(got, [(['/usr/bin/notify', '--topic', 'm'], 'text', {'K': 'v'})])

        def boom():
            raise OSError('machine file unreadable')
        self.assertFalse(keepon.channel_alert('x', read_channel=boom)[0])

    def test_the_command_line_runs_keep_on_from_the_config(self):
        token = os.path.join(self.tmp, 'token')
        with open(token, 'w') as f:
            f.write('t0ken\n')
        os.chmod(token, 0o600)
        path = os.path.join(self.tmp, 'config.json')
        with open(path, 'w') as f:
            json.dump({'version': 1, 'auth': {'tokenFile': token},
                       'keepOn': [{'repo': REPO, 'workflows': ['fork-tests.yml']}]}, f)
        self.api.workflow_rows = {REPO: rows(**{'fork-tests': 'disabled_manually'})}
        out, err = io.StringIO(), io.StringIO()
        code = kick.main(['keep-on', '--config', path, '--state-dir', self.folder], api_factory=lambda c: self.api,
                         out=out, err=err)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(self.enables(), [1])
        self.assertEqual(kick.main(['status', '--keep-on', '--state-dir', self.folder], out=io.StringIO(),
                                   err=io.StringIO()), 0)
        with open(path, 'w') as f:
            json.dump({'version': 1, 'auth': {'tokenFile': token},
                       'heads': [{'repo': REPO, 'workflow': 'fork-tests.yml'}]}, f)
        out = io.StringIO()
        self.assertEqual(kick.main(['keep-on', '--config', path, '--state-dir', self.folder],
                                   api_factory=lambda c: self.api, out=out, err=io.StringIO()), 0)
        self.assertIn('no keepOn', out.getvalue())


class Clients(unittest.TestCase):
    """The REST calls of TokenApi, read off a fake opener (no socket), and gh-as's arguments."""

    class Opener:
        def __init__(self, body):
            self.body, self.requests = body, []

        def open(self, req, timeout):
            self.requests.append((req.get_method(), req.full_url, req.data))
            body = self.body

            class R:
                headers = {}

                def read(self, n):
                    return json.dumps(body).encode()

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False
            return R()

    def test_token_api_lists_enables_and_asks_graphql(self):
        api = github.TokenApi('t')
        api.opener = self.Opener({'workflows': [{'id': 5, 'path': '.github/workflows/a.yml', 'state': 'active',
                                                 'name': 'A'}]})
        self.assertEqual(api.workflows(REPO), [{'id': 5, 'path': '.github/workflows/a.yml', 'state': 'active'}])
        api.opener = self.Opener(None)
        api.enable(REPO, 5)
        self.assertEqual(api.opener.requests[0][:2], ('PUT', f'https://api.github.com/repos/{REPO}/actions/workflows/5/enable'))
        api.opener = self.Opener({'data': {'repository': None}})
        self.assertEqual(api.graphql(REPO, 'query', {'owner': 'o'}), {'repository': None})
        method, url, data = api.opener.requests[0]
        self.assertEqual((method, url, json.loads(data)), ('POST', 'https://api.github.com/graphql',
                                                           {'query': 'query', 'variables': {'owner': 'o'}}))

    def test_gh_as_arguments(self):
        calls = []

        def run(argv, **kw):
            calls.append(argv)
            out = '[{"id": 5, "path": ".github/workflows/a.yml", "state": "active"}]' if 'list' in argv \
                else '{"data": {"x": 1}}'
            return type('R', (), {'returncode': 0, 'stdout': out, 'stderr': ''})()
        api = github.GhAsApi('/usr/local/bin/gh-as', '/home/x', run=run)
        api.workflows(REPO)
        api.enable(REPO, 5)
        self.assertEqual(api.graphql(REPO, 'q', {'name': '123'}), {'x': 1})
        self.assertEqual(calls[0][3:], ['gh', 'workflow', 'list', '--all', '--limit', '100', '--json', 'id,path,state'])
        self.assertEqual(calls[1][3:], ['gh', 'workflow', 'enable', '5'])
        self.assertEqual(calls[2][3:], ['gh', 'api', 'graphql', '-f', 'query=q', '-f', 'name=123'])

    def test_gh_as_refusal_keeps_its_http_status(self):
        def run(argv, **kw):
            return type('R', (), {'returncode': 1, 'stdout': '',
                                  'stderr': 'could not create workflow dispatch event: HTTP 422: Unexpected inputs'})()
        with self.assertRaises(github.ApiError) as e:
            github.GhAsApi('/usr/local/bin/gh-as', '/home/x', run=run).dispatch(REPO, 'a.yml', 'b', {'head': 'true'})
        self.assertEqual(e.exception.status, 422)


    def test_token_api_keeps_the_status_of_a_refusal(self):
        class Opener:
            def open(self, req, timeout):
                raise urllib.error.HTTPError(req.full_url, 422, 'Unprocessable', {}, io.BytesIO(b'{"message": "no"}'))
        api = github.TokenApi('t')
        api.opener = Opener()
        with self.assertRaises(github.ApiError) as e:
            api.dispatch(REPO, 'a.yml', 'main', {})
        self.assertEqual(e.exception.status, 422)


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        red_check(self, THIS, {
            'named only': ('keepon', '            r = by_file.get(workflow)\n',
                           "            r = by_file.get(workflow) or next(iter(by_file.values()))\n",
                           'KeepOn.test_a_missing_target_is_an_error_with_one_alert_an_hour'),
            'known off states only': ('keepon', "if r['state'] not in TURNED_ON:", 'if False:',
                                      'KeepOn.test_inactivity_is_turned_back_on_too_but_no_other_state'),
            'on means no call': ('keepon', "if r['state'] == 'active':", 'if False:',
                                 'KeepOn.test_one_list_call_per_entry_a_pass_and_nothing_when_all_are_on'),
            'alert an hour': ('keepon', 'if not (isinstance(last, str) and', 'if True or (isinstance(last, str) and',
                              'KeepOn.test_a_missing_target_is_an_error_with_one_alert_an_hour'),
            'the lock': ('keepon', 'fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'None',
                         'KeepOn.test_two_passes_at_once_the_second_does_nothing'),
            'the pause': ('keepon', '        if paused is not None:\n            for key', '        if False:\n            for key',
                          'KeepOn.test_a_list_error_is_an_error_and_a_rate_limit_pauses'),
            'pause stops the pass': ('keepon', "                    paused = waiting_until(state.get('pauseUntil'), now)\n                continue",
                                     '                    pass\n                continue',
                                     'KeepOn.test_a_rate_limit_on_turning_on_pauses_and_stops_the_pass'),
            'the alert line': ('keepon', 'ALERT {label}', '{label}',
                               'KeepOn.test_a_switched_off_target_is_turned_back_on_with_an_alert_line'),
            'a pause is never shortened': ('kick', '    if current is not None and current > until:\n        return\n',
                                           '',
                                           'KeepOn.test_a_low_budget_never_shortens_a_rate_limit_pause'),
            'status code kept': ('github', 'retry_after, e.code) from None', 'retry_after) from None',
                                 'Clients.test_token_api_keeps_the_status_of_a_refusal'),
        })


if __name__ == '__main__':
    unittest.main(verbosity=2)
