"""schedule-kick's config, ticks and GitHub calls: a fake GitHub with its own clock (in the process), a local HTTP
server for the token path, a fake gh-as for the App path, a fixed host clock and a temporary folder. No root, no
systemd, no network.

  python3 -I -B fork-kit/schedule-kick/tests/kick.test.py

The state question, answered here: runs again (same slot: no call, no dispatch; two ticks at once), stopped
half-way (dispatch sent, state not written; a stop between entries), old, broken or missing state, host off past
slots (one catch-up only), clock jumps (back and forward), errors (wait, never a tight loop) and the API budget.
RedCheck breaks each rule in a copy of lib/schedulekick/ and shows its test then fails.
"""
import contextlib, datetime, http.server, io, json, os, re, shutil, stat, subprocess, sys, tempfile, threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kick_fixture import KIT, LIB, FakeApi, at, github, kick, red_check  # noqa: E402

THIS = os.path.abspath(__file__)
BIN = os.path.join(KIT, 'bin', 'schedule-kick')
NOW = at('2026-11-21T12:45:30')
R1, R2 = 'example-owner/app', 'example-owner/tools'


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='schedule-kick-test-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, 'state')
        self.gh_as = self.fake_gh_as()

    def fake_gh_as(self, folder=None):
        folder = folder or os.path.join(self.tmp, 'bin')
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'gh-as')
        with open(path, 'w') as f:
            f.write(FAKE_GH_AS.replace('#!python', '#!' + sys.executable, 1))
        os.chmod(path, 0o755)
        return path

    def write_config(self, entries=None, auth=None, raw=None, name='config.json'):
        path = os.path.join(self.tmp, name)
        data = raw if raw is not None else {'version': 1, 'auth': auth if auth is not None else {'ghAs': self.gh_as},
                                             'entries': (
            entries if entries is not None else [{'repo': R1, 'workflow': 'nightly.yml', 'cron': '17 4 * * *',
                                                  'ref': 'main'}])}
        with open(path, 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return path

    def config(self, entries=None, **kw):
        return kick.load_config(self.write_config(entries, **kw))

    def tick(self, cfg, api, now=NOW):
        out, err = io.StringIO(), io.StringIO()
        code = kick.tick(cfg, self.state_dir, api, now=now, out=out, err=err)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def state(self):
        with open(os.path.join(self.state_dir, 'state.json')) as f:
            return json.load(f)

    def status(self):
        with open(os.path.join(self.state_dir, 'status.json')) as f:
            return json.load(f)


# A fake gh-as: answers `gh run list` from runs.json beside it and records every call (argv and environment).
FAKE_GH_AS = r'''#!python
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
def load(name, default):
    try:
        with open(os.path.join(here, name)) as f:
            return json.load(f)
    except FileNotFoundError:
        return default
calls = load('calls.json', [])
calls.append({'argv': sys.argv[1:], 'env': dict(os.environ)})
with open(os.path.join(here, 'calls.json'), 'w') as f:
    json.dump(calls, f)
mode = load('mode.json', {})
if mode.get('fail'):
    sys.stderr.write(mode['fail'])
    sys.exit(1)
args = sys.argv[1:]
assert args[0] == '--repo' and args[2] == 'gh', args
runs = load('runs.json', [])
if args[3:5] == ['run', 'list']:
    since = args[args.index('--created') + 1][2:]
    print(json.dumps([{'createdAt': r} for r in runs if r >= since]))
elif args[3:5] == ['workflow', 'run']:
    import datetime
    runs.append(datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    with open(os.path.join(here, 'runs.json'), 'w') as f:
        json.dump(runs, f)
else:
    sys.exit(f'fake gh-as: {args}')
'''


class Config(Temp):
    def refused(self, needle, **kw):
        with self.assertRaises(kick.ConfigError) as e:
            kick.load_config(self.write_config(**kw))
        self.assertIn(needle, str(e.exception))
        return str(e.exception)

    def entry(self, **over):
        e = {'repo': R1, 'workflow': 'nightly.yml', 'cron': '17 4 * * *', 'ref': 'main'}
        e.update(over)
        return [{k: v for k, v in e.items() if v is not None}]

    def test_the_readme_example_loads(self):
        with open(os.path.join(KIT, 'README.md')) as f:
            text = f.read()
        example = re.search(r'```json\n(.*?)```', text, re.S).group(1)
        raw = json.loads(example.replace('/usr/local/bin/gh-as', self.gh_as))
        cfg = kick.load_config(self.write_config(raw=raw))
        self.assertEqual([(e.repo, e.workflow, e.cron.line, e.ref, e.inputs) for e in cfg.entries],
                         [('OWNER/REPO', 'nightly.yml', '17 4 * * *', 'main', {}),
                          ('OWNER/OTHER', 'sweep.yml', '*/15 * * * *', 'main', {'mode': 'quick'})])
        self.assertEqual(cfg.auth, ('ghAs', self.gh_as))

    def test_a_broken_config_is_refused_whole(self):
        cases = {
            'not JSON': dict(raw='{nope'),
            'must be a JSON object': dict(raw=[]),
            'version must be 1': dict(raw={'version': 2, 'auth': {'ghAs': self.gh_as}, 'entries': []}),
            "unknown key 'entry'": dict(raw={'version': 1, 'auth': {'ghAs': self.gh_as}, 'entry': []}),
            'entries must be a list of 1 to 50': dict(entries=[]),
            'is not OWNER/NAME': dict(entries=self.entry(repo='nope')),
            'not a workflow file name': dict(entries=self.entry(workflow='../x.yml')),
            'is not a branch or tag name': dict(entries=self.entry(ref='a..b')),
            'exactly 5 fields': dict(entries=self.entry(cron='@daily')),
            'never comes': dict(entries=self.entry(cron='0 0 31 2 *')),
            'cron is missing': dict(entries=self.entry(cron=None)),
            'values are one-line strings': dict(entries=self.entry(inputs={'force': True})),
        }
        for needle, kw in cases.items():
            with self.subTest(needle=needle):
                self.refused(needle, **kw)

    def test_an_unknown_entry_key_is_refused(self):
        self.refused("unknown key 'crom'", entries=self.entry(crom='0 0 * * *'))

    def test_one_entry_per_workflow(self):
        two = self.entry() + self.entry(cron='0 1 * * *', repo=R1.upper())
        self.refused('is listed twice', entries=two)

    def test_auth_is_exactly_one_kind_and_never_a_persons_own_login(self):
        self.refused('auth must be exactly one of', auth={})
        self.refused('auth must be exactly one of', raw={'version': 1, 'entries': self.entry()})
        self.refused('auth must be exactly one of', auth={'ghAs': self.gh_as, 'tokenFile': '/x'})
        self.refused('auth must be exactly one of', auth={'gh': '/usr/bin/gh'})
        self.refused('must be an absolute path', auth={'ghAs': 'gh-as'})
        self.refused('no such program', auth={'ghAs': os.path.join(self.tmp, 'none', 'gh-as')})

    def test_gh_as_must_be_gh_as_never_gh(self):
        gh = os.path.join(self.tmp, 'bin', 'gh')
        shutil.copy(self.gh_as, gh)
        self.refused('never gh itself', auth={'ghAs': gh})

    def test_the_token_file_must_be_private_to_this_user(self):
        token = os.path.join(self.tmp, 'token')
        with open(token, 'w') as f:
            f.write('ghp_example\n')
        os.chmod(token, 0o640)
        self.refused('group or others can use it', auth={'tokenFile': token})
        os.chmod(token, 0o600)
        self.assertEqual(kick.load_config(self.write_config(auth={'tokenFile': token})).auth, ('tokenFile', token))
        self.assertEqual(kick.read_token(token), 'ghp_example')

    def test_the_token_file_is_never_read_through_a_link(self):
        token = os.path.join(self.tmp, 'token')
        with open(token, 'w') as f:
            f.write('ghp_example\n')
        os.chmod(token, 0o600)
        link = os.path.join(self.tmp, 'token-link')
        os.symlink(token, link)
        self.refused('without following a link', auth={'tokenFile': link})

    def test_a_token_file_that_is_not_one_token_is_refused(self):
        token = os.path.join(self.tmp, 'token')
        for text in ('', '\n', 'two words\n', 'x' * 5000):
            with self.subTest(text=text[:10]):
                with open(token, 'w') as f:
                    f.write(text)
                os.chmod(token, 0o600)
                self.refused('not one plain token', auth={'tokenFile': token})


class Tick(Temp):
    def setUp(self):
        super().setUp()
        self.api = FakeApi(NOW)
        self.cfg = self.config()

    def test_first_tick_dispatches_once_then_runs_again_send_nothing_and_call_nothing(self):
        self.assertEqual(self.tick(self.cfg, self.api), 0)
        self.assertEqual(self.api.calls, [('list', R1, 'nightly.yml', '2026-11-21T04:17:00Z'),
                                          ('dispatch', R1, 'nightly.yml', 'main', {})])
        self.assertEqual(self.status()['entries'][f'{R1} nightly.yml']['result'], 'dispatched')
        for later in (NOW, NOW + datetime.timedelta(minutes=5), NOW + datetime.timedelta(hours=10)):
            self.assertEqual(self.tick(self.cfg, self.api, now=later), 0)
        self.assertEqual(len(self.api.calls), 2)                      # same slot: no call at all
        self.assertEqual(self.status()['entries'][f'{R1} nightly.yml']['result'], 'done')
        self.assertTrue(self.status()['ok'])

    def test_the_next_slot_dispatches_again(self):
        self.tick(self.cfg, self.api)
        self.api.github_now = at('2026-11-22T04:18:00')
        self.tick(self.cfg, self.api, now=at('2026-11-22T04:18:00'))
        self.assertEqual(len(self.api.dispatches()), 2)
        self.assertEqual(self.api.lists()[-1][3], '2026-11-22T04:17:00Z')

    def test_a_run_since_the_slot_by_any_event_means_no_dispatch(self):
        for event in ('schedule', 'workflow_dispatch', 'push'):
            with self.subTest(event=event):
                shutil.rmtree(self.state_dir, True)
                self.api = FakeApi(NOW)
                self.api.add_run(R1, 'nightly.yml', '2026-11-21T04:17:00Z', event)
                self.assertEqual(self.tick(self.cfg, self.api), 0)
                self.assertEqual(self.api.dispatches(), [])
                self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'ran')
                self.tick(self.cfg, self.api)
                self.assertEqual(len(self.api.lists()), 1)            # recorded: no second call

    def test_a_run_before_the_slot_does_not_count_even_if_github_returns_it(self):
        self.api.add_run(R1, 'nightly.yml', '2026-11-21T04:16:59Z')
        self.api.ignore_filter = True
        self.tick(self.cfg, self.api)
        self.assertEqual(len(self.api.dispatches()), 1)

    def test_a_run_without_a_readable_time_is_an_error_not_a_dispatch(self):
        self.api.add_run(R1, 'nightly.yml', 'yesterday')
        self.api.ignore_filter = True
        self.assertEqual(self.tick(self.cfg, self.api), 1)
        self.assertEqual(self.api.dispatches(), [])
        self.assertIn('created time', self.status()['entries'][f'{R1} nightly.yml']['message'])

    def test_inputs_and_ref_are_sent(self):
        cfg = self.config([{'repo': R2, 'workflow': 'sweep.yaml', 'cron': '*/15 * * * *', 'ref': 'release/v2',
                            'inputs': {'mode': 'quick', 'dry-run': 'true'}}])
        self.tick(cfg, self.api)
        self.assertEqual(self.api.calls, [('list', R2, 'sweep.yaml', '2026-11-21T12:45:00Z'),
                                          ('dispatch', R2, 'sweep.yaml', 'release/v2',
                                           {'mode': 'quick', 'dry-run': 'true'})])

    def test_stopped_half_way_after_the_dispatch_sends_nothing_again(self):
        """The dispatch went out, then the tick died before the state was written."""
        with mock.patch.object(kick, 'write_json', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.tick(self.cfg, self.api)
        self.assertEqual(len(self.api.dispatches()), 1)
        self.assertFalse(os.path.exists(os.path.join(self.state_dir, 'state.json')))
        self.api.github_now = NOW + datetime.timedelta(minutes=5)
        self.assertEqual(self.tick(self.cfg, self.api, now=NOW + datetime.timedelta(minutes=5)), 0)
        self.assertEqual(len(self.api.dispatches()), 1)                # the run list found it
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'ran')

    def test_a_stop_between_entries_keeps_what_is_done(self):
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/5 * * * *', 'ref': 'main'},
                           {'repo': R1, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])

        def die():
            if len(self.api.dispatches()) == 2:
                raise KeyboardInterrupt
        self.api.after_dispatch = die
        with self.assertRaises(KeyboardInterrupt):
            self.tick(cfg, self.api)
        self.api.after_dispatch = None
        self.api.calls.clear()
        self.tick(cfg, self.api)
        self.assertEqual(self.api.calls, [('list', R1, 'b.yml', '2026-11-21T12:45:00Z')])   # a.yml: recorded
        self.assertEqual(self.api.dispatches(), [])

    def test_missing_broken_or_old_state_never_doubles_a_run(self):
        self.tick(self.cfg, self.api)
        path = os.path.join(self.state_dir, 'state.json')
        for text in (None, '{broken', '[]', json.dumps({'version': 0, 'entries': {}}),
                     json.dumps({'version': 1, 'entries': {f'{R1} nightly.yml': 'x'}})):
            with self.subTest(text=text):
                if text is None:
                    os.unlink(path)
                else:
                    with open(path, 'w') as f:
                        f.write(text)
                self.assertEqual(self.tick(self.cfg, self.api), 0)
                self.assertEqual(len(self.api.dispatches()), 1)
                if text not in (None, json.dumps({'version': 1, 'entries': {f'{R1} nightly.yml': 'x'}})):
                    self.assertIn('starting from empty state', self.err)
                self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'ran')

    def test_host_off_past_many_slots_sends_one_run_only(self):
        cfg = self.config([{'repo': R1, 'workflow': 'often.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])
        self.api.github_now = at('2026-11-18T09:00:10')
        self.tick(cfg, self.api, now=at('2026-11-18T09:00:10'))
        self.api.github_now = NOW
        self.assertEqual(self.tick(cfg, self.api), 0)                  # three days later
        self.assertEqual(len(self.api.dispatches()), 2)
        self.tick(cfg, self.api, now=NOW + datetime.timedelta(minutes=1))
        self.assertEqual(len(self.api.dispatches()), 2)

    def test_the_clock_jumping_back_sends_nothing(self):
        cfg = self.config([{'repo': R1, 'workflow': 'often.yml', 'cron': '0 * * * *', 'ref': 'main'}])
        self.tick(cfg, self.api)                                       # slot 12:00, dispatched (GitHub: 12:45)
        self.assertEqual(self.tick(cfg, self.api, now=at('2026-11-21T12:10:00')), 0)
        self.assertEqual(len(self.api.dispatches()), 1)
        self.assertEqual(self.tick(cfg, self.api, now=at('2026-11-21T11:30:00')), 0)
        self.assertEqual(len(self.api.dispatches()), 1)               # slot 11:00: the 12:45 run is after it

    def test_the_clock_jumping_forward_then_back_sends_one_run(self):
        cfg = self.config([{'repo': R1, 'workflow': 'often.yml', 'cron': '0 * * * *', 'ref': 'main'}])
        self.tick(cfg, self.api, now=at('2027-11-21T12:45:00'))       # a year ahead: slot 2027-..12:00
        self.assertEqual(len(self.api.dispatches()), 1)                # GitHub made it at its 12:45 today
        self.assertEqual(self.tick(cfg, self.api), 0)                  # back: slot 12:00 today, run exists
        self.assertEqual(len(self.api.dispatches()), 1)
        self.api.github_now = at('2026-11-21T13:01:00')
        self.tick(cfg, self.api, now=at('2026-11-21T13:01:00'))       # not stuck behind the future slot
        self.assertEqual(len(self.api.dispatches()), 2)

    def test_a_wait_far_ahead_is_dropped_after_the_clock_jumps_back(self):
        self.api.fail['dispatch'] = github.ApiError('HTTP 500')
        self.tick(self.cfg, self.api, now=at('2027-01-01T00:00:00'))  # error: wait until 2027-01-01T00:05
        del self.api.fail['dispatch']
        self.assertEqual(self.tick(self.cfg, self.api), 0)             # back to 2026: the wait is not kept
        self.assertEqual(len(self.api.dispatches()), 2)

    def test_an_error_waits_and_doubles_never_a_tight_loop(self):
        self.api.fail['dispatch'] = github.ApiError('POST ...: HTTP 422 Unexpected inputs')
        self.assertEqual(self.tick(self.cfg, self.api), 1)
        self.assertIn('HTTP 422', self.err)
        status = self.status()
        self.assertFalse(status['ok'])
        self.assertEqual(status['entries'][f'{R1} nightly.yml']['result'], 'error')
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['retryAfter'], '2026-11-21T12:50:30Z')
        calls = len(self.api.calls)
        self.assertEqual(self.tick(self.cfg, self.api, now=NOW + datetime.timedelta(minutes=4)), 0)
        self.assertEqual(len(self.api.calls), calls)                   # waiting: no call
        self.assertEqual(self.status()['entries'][f'{R1} nightly.yml']['result'], 'waiting')
        self.tick(self.cfg, self.api, now=NOW + datetime.timedelta(minutes=6))
        self.assertEqual(len(self.api.calls), calls + 2)               # tried again, failed again
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['retryAfter'], '2026-11-21T13:01:30Z')
        del self.api.fail['dispatch']
        self.tick(self.cfg, self.api, now=NOW + datetime.timedelta(minutes=17))
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'dispatched')
        self.assertNotIn('failures', self.state()['entries'][f'{R1} nightly.yml'])

    def test_one_entry_failing_does_not_stop_the_others(self):
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/5 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])
        real = self.api.runs_since

        def runs_since(repo, workflow, since):
            if repo == R1:
                raise github.ApiError('HTTP 404 Not Found')
            return real(repo, workflow, since)
        self.api.runs_since = runs_since
        self.assertEqual(self.tick(cfg, self.api), 1)
        self.assertEqual([c[1] for c in self.api.dispatches()], [R2])

    def test_a_rate_limit_pauses_every_entry_until_the_reset(self):
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/5 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])
        reset = int((NOW + datetime.timedelta(minutes=20)).timestamp())
        self.api.fail['list'] = github.ApiError('HTTP 403 rate limit', rate_limited=True, reset=reset)
        self.assertEqual(self.tick(cfg, self.api), 1)
        self.assertEqual(len(self.api.calls), 1)                       # the second entry was not called
        self.assertEqual(self.status()['entries'][f'{R2} b.yml']['result'], 'paused')
        del self.api.fail['list']
        self.tick(cfg, self.api, now=NOW + datetime.timedelta(minutes=19))
        self.assertEqual(len(self.api.calls), 1)
        self.tick(cfg, self.api, now=NOW + datetime.timedelta(minutes=21))
        self.assertEqual(len(self.api.dispatches()), 2)
        self.assertNotIn('pauseUntil', self.state())

    def test_few_calls_left_pauses_the_rest(self):
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/5 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])
        self.api.remaining = 99
        self.tick(cfg, self.api)
        self.assertEqual([c[1] for c in self.api.dispatches()], [R1])
        self.assertEqual(self.state()['pauseUntil'], '2026-11-21T13:00:30Z')
        self.assertIn('only 99 API calls left', self.out)

    def test_two_ticks_at_once_the_second_does_nothing(self):
        import fcntl
        os.makedirs(self.state_dir, mode=0o700)
        with open(os.path.join(self.state_dir, 'lock'), 'a') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            self.assertEqual(self.tick(self.cfg, self.api), 0)
        self.assertEqual(self.api.calls, [])
        self.assertIn('another tick is running', self.out)

    def test_the_state_is_written_whole_or_not_at_all(self):
        self.tick(self.cfg, self.api)
        before = self.state()

        def half(data, f, **kw):
            f.write('{"version": 1, "entr')
            f.flush()
            raise OSError(28, 'No space left on device')
        api = FakeApi(at('2026-11-22T05:00:00'))
        with mock.patch.object(json, 'dump', side_effect=half):
            with self.assertRaises(OSError):
                self.tick(self.cfg, api, now=at('2026-11-22T05:00:00'))
        self.assertEqual(self.state(), before)
        self.assertEqual(sorted(os.listdir(self.state_dir)), ['lock', 'state.json', 'status.json'])

    def test_entries_gone_from_the_config_are_dropped_from_the_state(self):
        self.tick(self.cfg, self.api)
        cfg = self.config([{'repo': R2, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main'}])
        self.tick(cfg, self.api)
        self.assertEqual(list(self.state()['entries']), [f'{R2} b.yml'])

    def test_a_changed_cron_is_checked_again(self):
        self.tick(self.cfg, self.api)
        cfg = self.config([{'repo': R1, 'workflow': 'nightly.yml', 'cron': '17 4 * * 0-6', 'ref': 'main'}])
        self.tick(cfg, self.api)
        self.assertEqual(len(self.api.lists()), 2)                     # same slot, other line: one check
        self.assertEqual(len(self.api.dispatches()), 1)

    def test_a_state_folder_that_is_a_link_or_open_to_others_is_refused(self):
        real = os.path.join(self.tmp, 'real')
        os.makedirs(real)
        os.symlink(real, self.state_dir)
        with self.assertRaises(kick.ConfigError):
            self.tick(self.cfg, self.api)
        os.unlink(self.state_dir)
        os.makedirs(self.state_dir)
        os.chmod(self.state_dir, 0o777)
        with self.assertRaises(kick.ConfigError):
            self.tick(self.cfg, self.api)
        self.assertEqual(self.api.calls, [])


class TokenServer(http.server.BaseHTTPRequestHandler):
    seen = []
    answers = {}

    def do_GET(self):
        self.answer()

    def do_POST(self):
        self.answer()

    def answer(self):
        n = int(self.headers.get('Content-Length') or 0)
        body = self.rfile.read(n).decode() if n else None
        TokenServer.seen.append((self.command, self.path, dict(self.headers), body))
        code, headers, data = TokenServer.answers.get((self.command, self.path.split('?')[0]), (404, {}, {}))
        self.send_response(code)
        for k, v in headers.items():
            self.send_header(k, v)
        raw = b'' if data is None else json.dumps(data).encode()
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class Transports(Temp):
    def server(self):
        TokenServer.seen, TokenServer.answers = [], {}
        srv = http.server.HTTPServer(('127.0.0.1', 0), TokenServer)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_address[1]}'

    def test_token_api_lists_with_the_filter_and_dispatches(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        TokenServer.answers = {
            ('GET', runs): (200, {'x-ratelimit-remaining': '4321'},
                            {'total_count': 1, 'workflow_runs': [{'created_at': '2026-11-21T05:00:00Z'}]}),
            ('POST', f'/repos/{R1}/actions/workflows/nightly.yml/dispatches'): (204, {}, None)}
        api = github.TokenApi('tok123', base=base)
        self.assertEqual(api.runs_since(R1, 'nightly.yml', at('2026-11-21T04:17:00')), ['2026-11-21T05:00:00Z'])
        self.assertEqual(api.remaining, 4321)
        api.dispatch(R1, 'nightly.yml', 'main', {'mode': 'quick'})
        (m1, p1, h1, _), (m2, p2, h2, b2) = TokenServer.seen
        self.assertEqual((m1, p1), ('GET', f'{runs}?created=%3E%3D2026-11-21T04%3A17%3A00Z&per_page=5'))
        self.assertEqual(h1['Authorization'], 'Bearer tok123')
        self.assertEqual((m2, json.loads(b2)), ('POST', {'ref': 'main', 'inputs': {'mode': 'quick'}}))

    def test_token_api_reads_a_rate_limit(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        TokenServer.answers = {('GET', runs): (403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1790000000'},
                                               {'message': 'API rate limit exceeded'})}
        with self.assertRaises(github.ApiError) as e:
            github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', NOW)
        self.assertTrue(e.exception.rate_limited)
        self.assertEqual(e.exception.reset, 1790000000)
        TokenServer.answers = {('GET', runs): (403, {'x-ratelimit-remaining': '10'}, {'message': 'Forbidden'})}
        with self.assertRaises(github.ApiError) as e:
            github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', NOW)
        self.assertFalse(e.exception.rate_limited)
        self.assertIn('HTTP 403 Forbidden', str(e.exception))

    def test_token_api_never_follows_a_redirect(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        TokenServer.answers = {('GET', runs): (302, {'Location': base + '/elsewhere'}, {})}
        with self.assertRaises(github.ApiError):
            github.TokenApi('tok123', base=base).runs_since(R1, 'nightly.yml', NOW)
        self.assertEqual([p.split('?')[0] for _, p, _, _ in TokenServer.seen], [runs])

    def test_gh_as_gets_the_exact_commands_and_no_login_of_the_caller(self):
        api = github.GhAsApi(self.gh_as, self.tmp)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'person', 'GITHUB_TOKEN': 'person', 'GH_CONFIG_DIR': '/x'}):
            self.assertEqual(api.runs_since(R1, 'nightly.yml', at('2026-11-21T04:17:00')), [])
            api.dispatch(R1, 'nightly.yml', 'main', {'mode': 'quick', 'x': 'a=b'})
        with open(os.path.join(os.path.dirname(self.gh_as), 'calls.json')) as f:
            calls = json.load(f)
        self.assertEqual([c['argv'] for c in calls], [
            ['--repo', R1, 'gh', 'run', 'list', '--workflow', 'nightly.yml', '--created', '>=2026-11-21T04:17:00Z',
             '--limit', '5', '--json', 'createdAt'],
            ['--repo', R1, 'gh', 'workflow', 'run', 'nightly.yml', '--ref', 'main', '-f', 'mode=quick', '-f',
             'x=a=b']])
        for c in calls:
            self.assertFalse({'GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR'} & set(c['env']), c['env'])

    def test_gh_as_failure_and_rate_limit(self):
        with open(os.path.join(os.path.dirname(self.gh_as), 'mode.json'), 'w') as f:
            json.dump({'fail': 'HTTP 403: API rate limit exceeded for installation'}, f)
        with self.assertRaises(github.ApiError) as e:
            github.GhAsApi(self.gh_as, self.tmp).runs_since(R1, 'nightly.yml', NOW)
        self.assertTrue(e.exception.rate_limited)
        self.assertIn('exit 1', str(e.exception))


class Command(Temp):
    """bin/schedule-kick as the timer runs it: a child Python, the fake gh-as, XDG folders in the temporary one."""

    def run_bin(self, *args, env=None):
        base = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp, 'XDG_CONFIG_HOME': os.path.join(self.tmp, 'cfg'),
                'XDG_STATE_HOME': os.path.join(self.tmp, 'st')}
        return subprocess.run([sys.executable, '-I', '-B', BIN, *args], capture_output=True, text=True,
                              env=dict(base, **(env or {})), timeout=120, stdin=subprocess.DEVNULL)

    def put_config(self, data):
        os.makedirs(os.path.join(self.tmp, 'cfg', 'schedule-kick'), exist_ok=True)
        with open(os.path.join(self.tmp, 'cfg', 'schedule-kick', 'config.json'), 'w') as f:
            json.dump(data, f)

    def test_tick_then_again_through_the_command(self):
        self.put_config({'version': 1, 'auth': {'ghAs': self.gh_as},
                         'entries': [{'repo': R1, 'workflow': 'daily.yml', 'cron': '0 0 * * *', 'ref': 'main'}]})
        self.assertEqual(self.run_bin('check').returncode, 0)
        r = self.run_bin('tick')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('dispatched', r.stdout)
        r = self.run_bin('tick')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('done', r.stdout)
        with open(os.path.join(os.path.dirname(self.gh_as), 'calls.json')) as f:
            self.assertEqual(sum(c['argv'][3:5] == ['workflow', 'run'] for c in json.load(f)), 1)
        r = self.run_bin('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(json.loads(r.stdout)['ok'])
        mode = stat.S_IMODE(os.stat(os.path.join(self.tmp, 'st', 'schedule-kick')).st_mode)
        self.assertEqual(mode, 0o700)

    def test_a_refused_config_sends_nothing_exits_2_and_says_so_in_the_status(self):
        self.put_config({'version': 1, 'entries': [{'repo': R1, 'workflow': 'a.yml', 'cron': '* * * * *',
                                                    'ref': 'main'}]})
        r = self.run_bin('tick', env={'GH_TOKEN': 'person-token'})
        self.assertEqual(r.returncode, 2)
        self.assertIn('refused, nothing sent', r.stderr)
        self.assertNotIn('person-token', r.stdout + r.stderr)
        r = self.run_bin('status')
        self.assertEqual(r.returncode, 1)
        self.assertIn('auth must be exactly one of', json.loads(r.stdout)['error'])
        self.assertFalse(os.path.exists(os.path.join(os.path.dirname(self.gh_as), 'calls.json')))

    def test_help_and_bad_use(self):
        r = self.run_bin('--help')
        self.assertEqual(r.returncode, 0)
        self.assertIn('schedule-kick tick', r.stdout)
        self.assertEqual(self.run_bin('kick').returncode, 2)
        self.assertEqual(self.run_bin('status').returncode, 1)         # no tick yet


MUTATIONS = {
    'same slot: no call': (
        'kick', "    if old.get('slot') == iso(slot) and old.get('cron') == entry.cron.line:", '    if False:',
        'Tick.test_first_tick_dispatches_once_then_runs_again_send_nothing_and_call_nothing'),
    'a changed cron is checked again': (
        'kick', "    if old.get('slot') == iso(slot) and old.get('cron') == entry.cron.line:",
        "    if old.get('slot') == iso(slot):", 'Tick.test_a_changed_cron_is_checked_again'),
    'a run since the slot: no dispatch': (
        'kick', '        if any(t >= slot for t in times):', '        if False:',
        'Tick.test_a_run_since_the_slot_by_any_event_means_no_dispatch'),
    'a run before the slot does not count': (
        'kick', '        if any(t >= slot for t in times):', '        if times:',
        'Tick.test_a_run_before_the_slot_does_not_count_even_if_github_returns_it'),
    'an unreadable run time is an error': (
        'kick', '        if None in times:', '        if False:',
        'Tick.test_a_run_without_a_readable_time_is_an_error_not_a_dispatch'),
    'wait after an error': ('kick', '    if wait is not None:', '    if False:',
                            'Tick.test_an_error_waits_and_doubles_never_a_tight_loop'),
    'a wait far ahead is dropped': ('kick', '    if until is not None and now < until <= now + MAX_WAIT:',
                                    '    if until is not None and now < until:',
                                    'Tick.test_a_wait_far_ahead_is_dropped_after_the_clock_jumps_back'),
    'paused entries are not called': ('kick', '        if paused is not None:\n            status',
                                      '        if False:\n            status',
                                      'Tick.test_a_rate_limit_pauses_every_entry_until_the_reset'),
    'a rate limit stops the tick': (
        'kick', "keeps what is done\n        paused = waiting_until(state.get('pauseUntil'), now)\n",
        "keeps what is done\n", 'Tick.test_a_rate_limit_pauses_every_entry_until_the_reset'),
    'few calls left pauses': (
        'kick', "        if result != 'done' and remaining is not None and remaining < MIN_REMAINING:",
        '        if False:', 'Tick.test_few_calls_left_pauses_the_rest'),
    'one tick at a time': ('kick', '            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)', '            int(0)',
                           'Tick.test_two_ticks_at_once_the_second_does_nothing'),
    'state written after each entry': (
        'kick', '            write_json(state_path, state)       # after each entry', '            pass  #',
        'Tick.test_a_stop_between_entries_keeps_what_is_done'),
    'state written whole': ('kick', "    tmp = os.path.join(folder, f'.{os.path.basename(path)}.tmp-{os.getpid()}')",
                            '    tmp = path', 'Tick.test_the_state_is_written_whole_or_not_at_all'),
    'one entry per workflow': ('kick', '            if entry.key in seen:', '            if False:',
                               'Config.test_one_entry_per_workflow'),
    'unknown entry keys refused': ('kick', '    unknown = set(raw) - ENTRY_KEYS', '    unknown = set()',
                                   'Config.test_an_unknown_entry_key_is_refused'),
    'gh-as, never gh': ("kick", "    if os.path.basename(path) != 'gh-as':", '    if False:',
                        'Config.test_gh_as_must_be_gh_as_never_gh'),
    'exactly one auth': ("kick", "    if not isinstance(raw, dict) or len(raw) != 1 or set(raw) - {'tokenFile', 'ghAs'}:",
                         "    if not isinstance(raw, dict) or not raw:",
                         'Config.test_auth_is_exactly_one_kind_and_never_a_persons_own_login'),
    'token file private': ('kick', '        if st.st_mode & 0o077:', '        if False:',
                           'Config.test_the_token_file_must_be_private_to_this_user'),
    'token file not through a link': ('kick', 'os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK',
                                      'os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK',
                                      'Config.test_the_token_file_is_never_read_through_a_link'),
    'state folder checked': ('kick', '    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:',
                             '    if False:', 'Tick.test_a_state_folder_that_is_a_link_or_open_to_others_is_refused'),
    'no redirect': ('github', '        self.opener = urllib.request.build_opener(_NoRedirect)',
                    '        self.opener = urllib.request.build_opener()',
                    'Transports.test_token_api_never_follows_a_redirect'),
    'rate limit read (token)': ('github', "            limited = e.code == 429 or (e.code == 403 and remaining == '0')",
                                '            limited = False', 'Transports.test_token_api_reads_a_rate_limit'),
    'rate limit read (gh-as)': ('github', "                           'rate limit' in err.lower())",
                                '                           False)', 'Transports.test_gh_as_failure_and_rate_limit'),
    "gh-as: none of the caller's login": ('github', '                         timeout=self.timeout, env=self.env,',
                                          '                         timeout=self.timeout, env=None,',
                                          'Transports.test_gh_as_gets_the_exact_commands_and_no_login_of_the_caller'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        red_check(self, THIS, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
