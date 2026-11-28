"""schedule-kick's config, ticks and GitHub calls: a fake GitHub with its own clock (in the process), a local HTTP
server for the token path, a fake gh-as for the App path, a fixed host clock and a temporary folder. No root, no
systemd, no network.

  python3 -I -B fork-kit/schedule-kick/tests/kick.test.py

The state question, answered here: runs again (same slot: no call, no dispatch; two ticks at once), stopped
half-way (dispatch sent, state not written; a stop between entries; a tick that breaks), old, broken or missing
state (also with the host clock ahead of GitHub's), host off past slots (one catch-up only), clock jumps (back and
forward), errors (wait, never a tight loop), the API budget, and which runs count as the slot's run.
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
        trust = mock.patch.object(kick, 'TRUSTED_OWNERS', (0, os.getuid()), create=True)   # the fake gh-as: not root's
        trust.start()
        self.addCleanup(trust.stop)
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
    print(json.dumps([r for r in runs if r['createdAt'] >= since]))
elif args[3:5] == ['workflow', 'run']:
    import datetime
    runs.append({'createdAt': datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
                 'event': 'workflow_dispatch', 'headBranch': args[args.index('--ref') + 1]})
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
            'nothing to do: give entries, heads or keepOn': dict(entries=[]),
            'entries must be a list of at most 50': dict(entries=self.entry() * 51),
            'is not OWNER/NAME': dict(entries=self.entry(repo='nope')),
            'not a workflow file name': dict(entries=self.entry(workflow='../x.yml')),
            'is not a branch or tag name': dict(entries=self.entry(ref='a..b')),
            'exactly 5 fields': dict(entries=self.entry(cron='@daily')),
            'never comes': dict(entries=self.entry(cron='0 0 31 2 *')),
            'cron is missing': dict(entries=self.entry(cron=None)),
            'values are one-line strings': dict(entries=self.entry(inputs={'force': True})),
            "'owner/..' is not OWNER/NAME": dict(entries=self.entry(repo='owner/..')),
            "'owner/.' is not OWNER/NAME": dict(entries=self.entry(repo='owner/.')),
            'graceMinutes must be a whole number': dict(entries=self.entry(graceMinutes=61)),
        }
        for needle, kw in cases.items():
            with self.subTest(needle=needle):
                self.refused(needle, **kw)

    def test_repo_names_dot_and_dot_dot_are_refused(self):
        for name in ('.', '..'):
            with self.subTest(name=name):
                self.refused(f"'owner/{name}' is not OWNER/NAME", entries=self.entry(repo=f'owner/{name}'))
        self.assertEqual(kick.load_config(self.write_config(self.entry(repo='owner/.github'))).entries[0].repo,
                         'owner/.github')

    def test_control_characters_are_refused_by_check(self):
        """A NUL in an input crashed every tick (argv cannot hold it) while `check` said the config was fine."""
        for value in ('v\x00w', 'a\rb', 'esc\x1b[31m', 'del\x7f', 'lone\ud800', 'c1\x85', 'line\u2028'):
            with self.subTest(value=value):
                self.refused('without control characters', entries=self.entry(inputs={'k': value}))
                self.assertEqual(kick.main(['check', '--config', self.write_config(self.entry(inputs={'k': value}))],
                                           out=io.StringIO(), err=io.StringIO()), 2)
        self.refused('no control characters', auth={'tokenFile': os.path.join(self.tmp, 'tok\x00en')})
        self.refused('no control characters', auth={'ghAs': self.gh_as + '\x00'})

    def test_grace_minutes_global_and_per_entry(self):
        cfg = kick.load_config(self.write_config(raw={
            'version': 1, 'auth': {'ghAs': self.gh_as}, 'graceMinutes': 10,
            'entries': self.entry() + [{'repo': R2, 'workflow': 'b.yml', 'cron': '*/5 * * * *', 'ref': 'main',
                                        'graceMinutes': 0}]}))
        self.assertEqual([e.grace for e in cfg.entries], [datetime.timedelta(minutes=10), datetime.timedelta(0)])
        self.assertEqual(self.config().entries[0].grace, datetime.timedelta(minutes=5))     # the default
        for bad in (-1, 61, 1.5, '5', True, None):
            with self.subTest(bad=bad):
                self.refused('graceMinutes must be a whole number',
                             entries=[dict(self.entry()[0], graceMinutes=bad)])
        self.refused('config.graceMinutes must be', raw={'version': 1, 'auth': {'ghAs': self.gh_as},
                                                        'graceMinutes': 99, 'entries': self.entry()})

    def test_a_cron_closer_than_the_grace_and_the_tick_is_refused(self):
        """With the default grace, the dispatch for one `*/5` slot lands after the next slot and counts for it:
        only every other slot would run. `check` refuses it and says what to do."""
        for cron, grace, needle in (('*/5 * * * *', None, 'Lower graceMinutes to 0 (0 is safe with late-check)'),
                                    ('*/10 * * * *', 6, 'slots 10 minutes apart, less than graceMinutes (6)'),
                                    ('0,55 0,23 * * *', None, 'slots 5 minutes apart'),     # 23:55, then 00:00
                                    ('* * * * *', 0, 'Lower graceMinutes to 0 (0 is safe with late-check); a line '
                                                     'with slots under 5 minutes apart cannot be kicked at all')):
            with self.subTest(cron=cron, grace=grace):
                entries = self.entry(cron=cron, graceMinutes=grace)
                self.refused(needle, entries=entries)
                self.assertEqual(kick.main(['check', '--config', self.write_config(entries)], out=io.StringIO(),
                                           err=io.StringIO()), 2)
        for cron, grace in (('*/5 * * * *', 0), ('*/10 * * * *', 5), ('0,55 0,23 * * 1,3', None),
                            ('0,55 0,23 * * *', 0)):
            with self.subTest(cron=cron, grace=grace):
                self.assertEqual(self.config(self.entry(cron=cron, graceMinutes=grace)).entries[0].cron.line, cron)

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

    def test_the_token_file_must_be_this_users(self):
        token = os.path.join(self.tmp, 'token')
        with open(token, 'w') as f:
            f.write('ghp_example\n')
        os.chmod(token, 0o600)
        with mock.patch.object(kick.os, 'getuid', return_value=os.getuid() + 1):    # the file is another user's
            self.refused('not by the user this runs as', auth={'tokenFile': token})

    def test_gh_as_must_be_roots_never_a_users_own_copy(self):
        """A user-owned gh-as (an old ~/.local/bin copy) could be anything, a shim with the person's login too."""
        if os.getuid() == 0:
            self.skipTest('as root every file is root\'s')
        with mock.patch.object(kick, 'TRUSTED_OWNERS', (0,)):
            self.refused('must be root-owned', auth={'ghAs': self.gh_as})
        os.chmod(self.gh_as, 0o775)
        self.refused('must be root-owned and not writable by group or others', auth={'ghAs': self.gh_as})
        os.chmod(self.gh_as, 0o755)
        os.chmod(os.path.dirname(self.gh_as), 0o777)
        self.addCleanup(os.chmod, os.path.dirname(self.gh_as), 0o755)
        self.refused('can be changed by someone other than root', auth={'ghAs': self.gh_as})

    def test_gh_as_folders_must_be_roots_whatever_their_mode(self):
        """A folder of another user (mode 755) can have its gh-as swapped by that user, so it is refused too."""
        folder, real_stat = os.path.dirname(self.gh_as), os.stat

        def stat_as_other(path, *a, **kw):
            st = real_stat(path, *a, **kw)
            return mock.Mock(st_uid=os.getuid() + 1, st_mode=stat.S_IFDIR | 0o755) if path == folder else st
        self.config()                                                          # this user's folder: trusted here
        with mock.patch.object(kick.os, 'stat', side_effect=stat_as_other):
            self.refused(f'folder {folder} can be changed by someone other than root', auth={'ghAs': self.gh_as})

    def test_a_link_named_gh_as_to_another_program_is_refused(self):
        """Never plain gh: a link called gh-as whose real file is gh is caught by the real name."""
        gh = os.path.join(self.tmp, 'bin', 'gh')
        shutil.copy(self.gh_as, gh)
        links = os.path.join(self.tmp, 'links')
        os.makedirs(links)
        os.symlink(gh, os.path.join(links, 'gh-as'))
        self.refused(f'no such program (it is {gh})', auth={'ghAs': os.path.join(links, 'gh-as')})

    def test_gh_as_is_used_by_its_real_path(self):
        links = os.path.join(self.tmp, 'links')
        os.makedirs(links)
        os.symlink(self.gh_as, os.path.join(links, 'gh-as'))
        self.assertEqual(self.config(auth={'ghAs': os.path.join(links, 'gh-as')}).auth, ('ghAs', self.gh_as))
        os.symlink(self.gh_as, os.path.join(links, 'gh'))
        self.refused('never gh itself', auth={'ghAs': os.path.join(links, 'gh')})

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
        self.api.github_now = at('2026-11-22T04:23:00')
        self.tick(self.cfg, self.api, now=at('2026-11-22T04:23:00'))          # the slot and its 5-minute grace
        self.assertEqual(len(self.api.dispatches()), 2)
        self.assertEqual(self.api.lists()[-1][3], '2026-11-22T04:17:00Z')

    def test_a_schedule_or_dispatch_run_on_the_ref_since_the_slot_means_no_dispatch(self):
        for event in ('schedule', 'workflow_dispatch'):
            with self.subTest(event=event):
                shutil.rmtree(self.state_dir, True)
                self.api = FakeApi(NOW)
                self.api.add_run(R1, 'nightly.yml', '2026-11-21T04:17:00Z', event, 'main')
                self.assertEqual(self.tick(self.cfg, self.api), 0)
                self.assertEqual(self.api.dispatches(), [])
                self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'ran')
                self.tick(self.cfg, self.api)
                self.assertEqual(len(self.api.lists()), 1)            # recorded: no second call

    def test_a_pull_request_push_or_other_branch_run_never_holds_off_the_dispatch(self):
        """Anyone can make a pull_request run (from a fork, even one that never starts); a push or a run on another
        branch is not the scheduled job either. None of them is the slot's run."""
        for event, branch in (('pull_request', 'main'), ('pull_request_target', 'main'), ('push', 'main'),
                              ('schedule', 'other'), ('workflow_dispatch', 'feature/x')):
            with self.subTest(event=event, branch=branch):
                shutil.rmtree(self.state_dir, True)
                self.api = FakeApi(NOW)
                for minute in range(18, 18 + 30):                     # many of them, all since the slot
                    self.api.add_run(R1, 'nightly.yml', f'2026-11-21T04:{minute:02d}:00Z', event, branch)
                self.assertEqual(self.tick(self.cfg, self.api), 0)
                self.assertEqual(self.api.dispatches(), [('dispatch', R1, 'nightly.yml', 'main', {})])

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
        self.assertEqual(self.api.calls, [('list', R2, 'sweep.yaml', '2026-11-21T12:30:00Z'),
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
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/10 * * * *', 'ref': 'main'},
                           {'repo': R1, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])

        def die():
            if len(self.api.dispatches()) == 2:
                raise KeyboardInterrupt
        self.api.after_dispatch = die
        with self.assertRaises(KeyboardInterrupt):
            self.tick(cfg, self.api)
        self.api.after_dispatch = None
        self.api.calls.clear()
        self.tick(cfg, self.api)
        self.assertEqual(self.api.calls, [('list', R1, 'b.yml', '2026-11-21T12:40:00Z')])   # a.yml: recorded
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

    def test_a_frequent_cron_runs_every_slot(self):
        """One simulated hour of ticks (every 5 minutes, 10 s late) and GitHub's own runs never come: one dispatch
        per slot, never only every other slot (a dispatch that lands after the next slot counts for it)."""
        for cron, grace, want in (('*/5 * * * *', 0, 12), ('*/10 * * * *', 5, 6), ('*/15 * * * *', 5, 4)):
            with self.subTest(cron=cron, grace=grace):
                shutil.rmtree(self.state_dir, True)
                api = FakeApi(NOW)
                cfg = self.config([{'repo': R1, 'workflow': 'often.yml', 'cron': cron, 'ref': 'main',
                                    'graceMinutes': grace}])
                start = at('2026-11-21T09:00:10')
                for n in range(12):
                    api.github_now = start + datetime.timedelta(minutes=5 * n)
                    self.assertEqual(self.tick(cfg, api, now=api.github_now), 0)
                self.assertEqual(len(api.dispatches()), want)

    def test_host_off_past_many_slots_sends_one_run_only(self):
        cfg = self.config([{'repo': R1, 'workflow': 'often.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])
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
        self.api.github_now = at('2026-11-21T13:06:00')
        self.tick(cfg, self.api, now=at('2026-11-21T13:06:00'))       # not stuck behind the future slot
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
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/10 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])
        real = self.api.runs_since

        def runs_since(repo, *a, **kw):
            if repo == R1:
                raise github.ApiError('HTTP 404 Not Found')
            return real(repo, *a, **kw)
        self.api.runs_since = runs_since
        self.assertEqual(self.tick(cfg, self.api), 1)
        self.assertEqual([c[1] for c in self.api.dispatches()], [R2])

    def test_a_rate_limit_pauses_every_entry_until_the_reset(self):
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/10 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])
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
        cfg = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/10 * * * *', 'ref': 'main'},
                           {'repo': R2, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])
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
        cfg = self.config([{'repo': R2, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])
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


class Grace(Temp):
    """GitHub's own run, when it comes on time, is the slot's run: the dispatch waits the grace for it."""

    def setUp(self):
        super().setUp()
        self.api = FakeApi(at('2026-11-21T04:17:30'))
        self.api.add_run(R1, 'nightly.yml', '2026-11-20T04:17:05Z')            # yesterday's slot ran

    def test_an_on_time_schedule_run_is_not_doubled(self):
        cfg = self.config()
        self.tick(cfg, self.api, now=at('2026-11-21T04:17:30'))               # the slot minute: still yesterday's
        self.api.add_run(R1, 'nightly.yml', '2026-11-21T04:19:30Z')           # GitHub's run, on time
        self.tick(cfg, self.api, now=at('2026-11-21T04:20:00'))
        self.tick(cfg, self.api, now=at('2026-11-21T04:25:00'))               # past the grace: it is found
        self.assertEqual(self.api.dispatches(), [])
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml'],
                         {'cron': '17 4 * * *', 'slot': '2026-11-21T04:17:00Z', 'result': 'ran',
                          'at': '2026-11-21T04:25:00Z'})

    def test_no_run_within_the_grace_then_one_dispatch(self):
        cfg = self.config()
        for minute in (17, 20):
            self.tick(cfg, self.api, now=at(f'2026-11-21T04:{minute}:30'))
        self.assertEqual(self.api.dispatches(), [])
        self.api.github_now = at('2026-11-21T04:25:00')
        self.tick(cfg, self.api, now=at('2026-11-21T04:25:00'))
        self.assertEqual(self.api.lists()[-1][3], '2026-11-21T04:17:00Z')
        self.assertEqual(len(self.api.dispatches()), 1)

    def test_grace_zero_dispatches_at_the_slot(self):
        cfg = self.config([{'repo': R1, 'workflow': 'nightly.yml', 'cron': '17 4 * * *', 'ref': 'main',
                            'graceMinutes': 0}])
        self.tick(cfg, self.api, now=at('2026-11-21T04:17:30'))
        self.assertEqual(len(self.api.dispatches()), 1)

    def test_lost_state_with_the_host_clock_ahead_of_githubs_sends_no_second_run(self):
        """The host is 3 minutes ahead: a dispatch at the slot minute would be created before the slot in GitHub's
        time and not be found once the state is gone. The grace puts it after the slot."""
        cfg = self.config()
        for host in ('04:17:30', '04:20:00', '04:25:00'):
            when = at(f'2026-11-21T{host}')
            self.api.github_now = when - datetime.timedelta(minutes=3)
            self.tick(cfg, self.api, now=when)
        self.assertEqual(len(self.api.dispatches()), 1)
        os.unlink(os.path.join(self.state_dir, 'state.json'))                  # state lost
        self.api.github_now = at('2026-11-21T04:27:00')
        self.tick(cfg, self.api, now=at('2026-11-21T04:30:00'))
        self.assertEqual(len(self.api.dispatches()), 1)
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['result'], 'ran')


class Faults(Temp):
    """A fault in a tick: the tick goes on with the next entry, and the status never stays `ok: true`."""

    def setUp(self):
        super().setUp()
        self.api = FakeApi(NOW)
        self.two = self.config([{'repo': R1, 'workflow': 'a.yml', 'cron': '*/10 * * * *', 'ref': 'main'},
                                {'repo': R2, 'workflow': 'b.yml', 'cron': '*/10 * * * *', 'ref': 'main'}])

    def test_a_crash_in_one_entry_does_not_stop_the_next(self):
        real = self.api.runs_since

        def runs_since(repo, *a, **kw):
            if repo == R1:
                raise TypeError('an answer nobody expected')
            return real(repo, *a, **kw)
        self.api.runs_since = runs_since
        self.assertEqual(self.tick(self.two, self.api), 1)
        self.assertEqual([c[1] for c in self.api.dispatches()], [R2])
        status = self.status()
        self.assertFalse(status['ok'])
        self.assertIn('internal error: TypeError', status['entries'][f'{R1} a.yml']['message'])
        self.assertEqual(self.state()['entries'][f'{R1} a.yml']['retryAfter'], '2026-11-21T12:50:30Z')

    def test_a_tick_that_breaks_says_so_in_the_status(self):
        path = self.write_config()
        ok = kick.main(['tick', '--config', path, '--state-dir', self.state_dir], api_factory=lambda cfg: self.api,
                       out=io.StringIO(), err=io.StringIO())
        self.assertEqual(ok, 0)
        self.assertTrue(self.status()['ok'])
        err = io.StringIO()
        with mock.patch.object(kick, 'load_state', side_effect=RuntimeError('disk gone odd')):
            code = kick.main(['tick', '--config', path, '--state-dir', self.state_dir],
                             api_factory=lambda cfg: self.api, out=io.StringIO(), err=err)
        self.assertEqual(code, 1)
        self.assertIn('the tick broke: RuntimeError: disk gone odd', err.getvalue())
        self.assertFalse(self.status()['ok'])
        out = io.StringIO()
        self.assertEqual(kick.main(['status', '--state-dir', self.state_dir], out=out, err=io.StringIO()), 1)
        self.assertIn('the tick broke', json.loads(out.getvalue())['error'])

    def test_a_huge_or_bad_rate_limit_reset_pauses_15_minutes(self):
        for reset in (10 ** 20, -1, 'soon'):
            with self.subTest(reset=reset):
                shutil.rmtree(self.state_dir, True)
                self.api.calls.clear()
                self.api.fail['list'] = github.ApiError('HTTP 403 rate limit', rate_limited=True, reset=reset)
                self.assertEqual(self.tick(self.two, self.api), 1)
                self.assertEqual(self.state()['pauseUntil'], '2026-11-21T13:00:30Z')
                self.assertEqual(len(self.api.calls), 1)

    def test_the_backoff_stops_at_one_hour(self):
        """Without the cap the 5th error waits 80 minutes, a wait the clock-jump rule then drops as too far
        ahead: the entry would be called on every tick."""
        cfg = self.config()
        os.makedirs(self.state_dir, mode=0o700)
        kick.write_json(os.path.join(self.state_dir, 'state.json'), {'version': 1, 'entries': {f'{R1} nightly.yml': {
            'failures': 4, 'retryAfter': '2026-11-21T12:00:00Z', 'error': 'HTTP 500'}}})
        self.api.fail['dispatch'] = github.ApiError('HTTP 500')
        self.tick(cfg, self.api)
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['failures'], 5)
        self.assertEqual(self.state()['entries'][f'{R1} nightly.yml']['retryAfter'], '2026-11-21T13:45:30Z')
        calls = len(self.api.calls)
        self.tick(cfg, self.api, now=NOW + datetime.timedelta(minutes=30))
        self.assertEqual(len(self.api.calls), calls)                           # still waiting

    def test_a_reset_far_ahead_pauses_one_hour_not_never(self):
        reset = int((NOW + datetime.timedelta(hours=3)).timestamp())
        self.api.fail['list'] = github.ApiError('HTTP 403 rate limit', rate_limited=True, reset=reset)
        self.tick(self.two, self.api)
        self.assertEqual(self.state()['pauseUntil'], '2026-11-21T13:45:30Z')
        del self.api.fail['list']
        self.tick(self.two, self.api, now=NOW + datetime.timedelta(minutes=30))
        self.assertEqual(len(self.api.calls), 1)                               # still paused

    def test_few_calls_left_pauses_until_githubs_reset(self):
        self.api.remaining = 99
        self.api.reset = int((NOW + datetime.timedelta(minutes=40)).timestamp())
        self.tick(self.two, self.api)
        self.assertEqual([c[1] for c in self.api.dispatches()], [R1])
        self.assertEqual(self.state()['pauseUntil'], '2026-11-21T13:25:30Z')
        self.tick(self.two, self.api, now=NOW + datetime.timedelta(minutes=20))
        self.assertEqual([c[1] for c in self.api.dispatches()], [R1])          # the 15 minutes are long over

    def test_a_secondary_limit_pauses_for_its_retry_after(self):
        self.api.fail['dispatch'] = github.ApiError('HTTP 403 secondary', rate_limited=True, retry_after=1200)
        self.assertEqual(self.tick(self.two, self.api), 1)
        self.assertEqual(self.state()['pauseUntil'], '2026-11-21T13:05:30Z')
        self.assertEqual(self.status()['entries'][f'{R2} b.yml']['result'], 'paused')


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
        raw = data if isinstance(data, bytes) else b'' if data is None else json.dumps(data).encode()
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
            ('GET', runs): (200, {'x-ratelimit-remaining': '4321', 'x-ratelimit-reset': '1790000000'},
                            {'total_count': 1, 'workflow_runs': [{'created_at': '2026-11-21T05:00:00Z',
                                                                  'event': 'schedule', 'head_branch': 'main'}]}),
            ('POST', f'/repos/{R1}/actions/workflows/nightly.yml/dispatches'): (204, {}, None)}
        api = github.TokenApi('tok123', base=base)
        self.assertEqual(api.runs_since(R1, 'nightly.yml', 'main', at('2026-11-21T04:17:00')),
                         [{'created': '2026-11-21T05:00:00Z', 'event': 'schedule', 'branch': 'main'}])
        self.assertEqual((api.remaining, api.reset), (4321, 1790000000))
        api.dispatch(R1, 'nightly.yml', 'main', {'mode': 'quick'})
        (m1, p1, h1, _), (m2, p2, h2, b2) = TokenServer.seen
        self.assertEqual((m1, p1), ('GET', f'{runs}?created=%3E%3D2026-11-21T04%3A17%3A00Z&branch=main&per_page=100'))
        self.assertEqual(h1['Authorization'], 'Bearer tok123')
        self.assertEqual((m2, json.loads(b2)), ('POST', {'ref': 'main', 'inputs': {'mode': 'quick'}}))

    def test_token_api_reads_a_rate_limit(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        TokenServer.answers = {('GET', runs): (403, {'x-ratelimit-remaining': '0', 'x-ratelimit-reset': '1790000000'},
                                               {'message': 'API rate limit exceeded'})}
        with self.assertRaises(github.ApiError) as e:
            github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
        self.assertTrue(e.exception.rate_limited)
        self.assertEqual(e.exception.reset, 1790000000)
        TokenServer.answers = {('GET', runs): (403, {'x-ratelimit-remaining': '10'}, {'message': 'Forbidden'})}
        with self.assertRaises(github.ApiError) as e:
            github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
        self.assertFalse(e.exception.rate_limited)
        self.assertIn('HTTP 403 Forbidden', str(e.exception))

    def test_token_api_reads_a_secondary_rate_limit(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        for code, headers, message, retry in (
                (403, {'x-ratelimit-remaining': '4000', 'retry-after': '60'}, 'Forbidden', 60),
                (403, {'x-ratelimit-remaining': '4000'}, 'You have exceeded a secondary rate limit.', None),
                (429, {'retry-after': '120'}, 'Too many', 120)):
            with self.subTest(code=code, message=message):
                TokenServer.answers = {('GET', runs): (code, headers, {'message': message})}
                with self.assertRaises(github.ApiError) as e:
                    github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
                self.assertTrue(e.exception.rate_limited)
                self.assertEqual(e.exception.retry_after, retry)

    def test_token_api_drops_a_reset_that_is_no_time(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        for reset in ('9' * 20, '1e9', '-5', '999999999999'):
            with self.subTest(reset=reset):
                TokenServer.answers = {('GET', runs): (403, {'x-ratelimit-remaining': '0',
                                                             'x-ratelimit-reset': reset}, {'message': 'limit'})}
                with self.assertRaises(github.ApiError) as e:
                    github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
                self.assertTrue(e.exception.rate_limited)
                self.assertIsNone(e.exception.reset)

    def test_token_api_reads_at_most_one_mib(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        big = b'{"workflow_runs": [], "pad": "' + b'x' * github.MAX_REPLY + b'"}'
        TokenServer.answers = {('GET', runs): (200, {}, big)}
        with self.assertRaises(github.ApiError) as e:
            github.TokenApi('t', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
        self.assertIn('larger than', str(e.exception))

    def test_token_api_never_follows_a_redirect(self):
        base = self.server()
        runs = f'/repos/{R1}/actions/workflows/nightly.yml/runs'
        TokenServer.answers = {('GET', runs): (302, {'Location': base + '/elsewhere'}, {})}
        with self.assertRaises(github.ApiError):
            github.TokenApi('tok123', base=base).runs_since(R1, 'nightly.yml', 'main', NOW)
        self.assertEqual([p.split('?')[0] for _, p, _, _ in TokenServer.seen], [runs])

    def test_gh_as_gets_the_exact_commands_and_no_login_of_the_caller(self):
        api = github.GhAsApi(self.gh_as, self.tmp)
        with mock.patch.dict(os.environ, {'GH_TOKEN': 'person', 'GITHUB_TOKEN': 'person', 'GH_CONFIG_DIR': '/x'}):
            self.assertEqual(api.runs_since(R1, 'nightly.yml', 'main', at('2026-11-21T04:17:00')), [])
            api.dispatch(R1, 'nightly.yml', 'main', {'mode': 'quick', 'x': 'a=b'})
        with open(os.path.join(os.path.dirname(self.gh_as), 'calls.json')) as f:
            calls = json.load(f)
        self.assertEqual([c['argv'] for c in calls], [
            ['--repo', R1, 'gh', 'run', 'list', '--workflow', 'nightly.yml', '--branch', 'main', '--created',
             '>=2026-11-21T04:17:00Z', '--limit', '100', '--json', 'createdAt,event,headBranch'],
            ['--repo', R1, 'gh', 'workflow', 'run', 'nightly.yml', '--ref', 'main', '-f', 'mode=quick', '-f',
             'x=a=b']])
        for c in calls:
            self.assertFalse({'GH_TOKEN', 'GITHUB_TOKEN', 'GH_CONFIG_DIR'} & set(c['env']), c['env'])

    def test_gh_as_failure_and_rate_limit(self):
        for text in ('HTTP 403: API rate limit exceeded for installation',
                     'HTTP 403: You have exceeded a secondary rate limit'):
            with self.subTest(text=text):
                with open(os.path.join(os.path.dirname(self.gh_as), 'mode.json'), 'w') as f:
                    json.dump({'fail': text}, f)
                with self.assertRaises(github.ApiError) as e:
                    github.GhAsApi(self.gh_as, self.tmp).runs_since(R1, 'nightly.yml', 'main', NOW)
                self.assertTrue(e.exception.rate_limited)
                self.assertIn('exit 1', str(e.exception))


class Command(Temp):
    """bin/schedule-kick as the timer runs it: a child Python, the fake gh-as, XDG folders in the temporary one."""

    def run_bin(self, *args, env=None, trust_me=False):
        """bin/schedule-kick; with trust_me, the same main with this user trusted like root for gh-as (the fake
        gh-as is not root's)."""
        base = {'PATH': '/usr/bin:/bin', 'HOME': self.tmp, 'XDG_CONFIG_HOME': os.path.join(self.tmp, 'cfg'),
                'XDG_STATE_HOME': os.path.join(self.tmp, 'st')}
        program = [BIN] if not trust_me else ['-c', (
            f'import sys; sys.path.insert(0, {LIB!r}); from schedulekick import kick; '
            f'kick.TRUSTED_OWNERS = (0, {os.getuid()}); sys.exit(kick.main(sys.argv[1:]))')]
        return subprocess.run([sys.executable, '-I', '-B', *program, *args], capture_output=True, text=True,
                              env=dict(base, **(env or {})), timeout=120, stdin=subprocess.DEVNULL)

    def put_config(self, data):
        os.makedirs(os.path.join(self.tmp, 'cfg', 'schedule-kick'), exist_ok=True)
        with open(os.path.join(self.tmp, 'cfg', 'schedule-kick', 'config.json'), 'w') as f:
            json.dump(data, f)

    def test_tick_then_again_through_the_command(self):
        self.put_config({'version': 1, 'auth': {'ghAs': self.gh_as},
                         'entries': [{'repo': R1, 'workflow': 'daily.yml', 'cron': '0 0 * * *', 'ref': 'main'}]})
        r = self.run_bin('check')
        self.assertEqual(r.returncode, 2)                                      # a gh-as that is not root's
        self.assertIn('must be root-owned', r.stderr)
        self.assertEqual(self.run_bin('check', trust_me=True).returncode, 0)
        r = self.run_bin('tick', trust_me=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('dispatched', r.stdout)
        r = self.run_bin('tick', trust_me=True)
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
    'a slot run on the ref: no dispatch': (
        'kick', '        if any(counts(row, entry, slot) for row in rows):', '        if False:',
        'Tick.test_a_schedule_or_dispatch_run_on_the_ref_since_the_slot_means_no_dispatch'),
    'a run before the slot does not count': (
        'kick', "row['branch'] == entry.ref and row['time'] >= slot", "row['branch'] == entry.ref",
        'Tick.test_a_run_before_the_slot_does_not_count_even_if_github_returns_it'),
    'only schedule and workflow_dispatch runs count': (
        'kick', "    return row['event'] in COUNTED_EVENTS and row['branch']", "    return True and row['branch']",
        'Tick.test_a_pull_request_push_or_other_branch_run_never_holds_off_the_dispatch'),
    'only runs on the ref count': (
        'kick', "row['event'] in COUNTED_EVENTS and row['branch'] == entry.ref and", "row['event'] in COUNTED_EVENTS and",
        'Tick.test_a_pull_request_push_or_other_branch_run_never_holds_off_the_dispatch'),
    'an unreadable run time is an error': (
        'kick', "        if any(row['time'] is None for row in rows):", '        if False:',
        'Tick.test_a_run_without_a_readable_time_is_an_error_not_a_dispatch'),
    'the grace': ('kick', '        return self.cron.last_slot(now - self.grace)', '        return self.cron.last_slot(now)',
                  'Grace.test_an_on_time_schedule_run_is_not_doubled'),
    'the grace (host clock ahead, state lost)': (
        'kick', '        return self.cron.last_slot(now - self.grace)', '        return self.cron.last_slot(now)',
        'Grace.test_lost_state_with_the_host_clock_ahead_of_githubs_sends_no_second_run'),
    'grace from the config': ('kick', "    value = raw.get('graceMinutes', default)", '    value = default',
                              'Config.test_grace_minutes_global_and_per_entry'),
    'grace checked': ('kick', '    if type(value) is not int or not 0 <= value <= MAX_GRACE_MINUTES:', '    if False:',
                      'Config.test_grace_minutes_global_and_per_entry'),
    'a cron closer than the grace and the tick refused': (
        'kick', '    if gap is not None and gap < wait + TICK_MINUTES:', '    if False:',
        'Config.test_a_cron_closer_than_the_grace_and_the_tick_is_refused'),
    'the grace counts in the gap': (
        'kick', '    if gap is not None and gap < wait + TICK_MINUTES:', '    if gap is not None and gap < TICK_MINUTES:',
        'Config.test_a_cron_closer_than_the_grace_and_the_tick_is_refused'),
    'the advice never names a grace under 0': ('kick', '{max(gap - TICK_MINUTES, 0)}', '{gap - TICK_MINUTES}',
                                                'Config.test_a_cron_closer_than_the_grace_and_the_tick_is_refused'),
    'C1 control characters refused': ('kick', 'or 0x7f <= ord(c) <= 0x9f or', 'or ord(c) == 0x7f or',
                                      'Config.test_control_characters_are_refused_by_check'),
    'line separators refused': ('kick', ' or ord(c) in (0x2028, 0x2029)', '',
                                'Config.test_control_characters_are_refused_by_check'),
    'repo . and .. refused': ('kick', " or raw['repo'].split('/')[1] in ('.', '..'):", ':',
                              'Config.test_repo_names_dot_and_dot_dot_are_refused'),
    'control characters refused': ('kick', "    return isinstance(text, str) and not any(", "    return isinstance(text, str) or not any(",
                                   'Config.test_control_characters_are_refused_by_check'),
    'one crashing entry does not stop the next': (
        'kick', "            result, message = kick_one(entry, api, state, now)\n        except Exception as e:",
        "            result, message = kick_one(entry, api, state, now)\n        except ArithmeticError as e:",
        'Faults.test_a_crash_in_one_entry_does_not_stop_the_next'),
    'a broken tick says so': ('kick', '    except Exception as e:          # noqa: BLE001 - a broken tick',
                              '    except ArithmeticError as e:          # noqa: BLE001 - a broken tick',
                              'Faults.test_a_tick_that_breaks_says_so_in_the_status'),
    'a bad reset is no time': ('kick', '    except (OverflowError, ValueError, OSError, TypeError):', '    except ():',
                               'Faults.test_a_huge_or_bad_rate_limit_reset_pauses_15_minutes'),
    'the backoff cap': ('kick', '    delay = min(BACKOFF * 2 ** min(failures - 1, 10), MAX_WAIT)',
                        '    delay = BACKOFF * 2 ** min(failures - 1, 10)', 'Faults.test_the_backoff_stops_at_one_hour'),
    'the pause cap': ('kick', "    state['pauseUntil'] = iso(min(until, now + MAX_WAIT))", "    state['pauseUntil'] = iso(until)",
                      'Faults.test_a_reset_far_ahead_pauses_one_hour_not_never'),
    'few calls left: until the reset': ('kick', "            pause(state, now, epoch(getattr(api, 'reset', None)))",
                                        '            pause(state, now)',
                                        'Faults.test_few_calls_left_pauses_until_githubs_reset'),
    'secondary limit: retry-after': ('kick', '            pause(state, now, now + datetime.timedelta(seconds=e.retry_after) if e.retry_after',
                                     '            pause(state, now, None if e.retry_after',
                                     'Faults.test_a_secondary_limit_pauses_for_its_retry_after'),
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
    'gh-as root-owned': ('kick', '    if st.st_uid not in TRUSTED_OWNERS or st.st_mode & 0o022:', '    if False:',
                         'Config.test_gh_as_must_be_roots_never_a_users_own_copy'),
    'gh-as folders root-owned': ('kick', '        if not safe:', '        if False:',
                                 'Config.test_gh_as_must_be_roots_never_a_users_own_copy'),
    'gh-as folders owned by root': ('kick', '    if st.st_uid not in TRUSTED_OWNERS:\n        return False',
                                    '    if False:\n        return False',
                                    'Config.test_gh_as_folders_must_be_roots_whatever_their_mode'),
    'gh-as by its real name': ('kick', "    if os.path.basename(real) != 'gh-as' or not stat.S_ISREG(",
                               '    if not stat.S_ISREG(', 'Config.test_a_link_named_gh_as_to_another_program_is_refused'),
    'gh-as by its real path': ('kick', '    return real\n', '    return path\n', 'Config.test_gh_as_is_used_by_its_real_path'),
    "token file this user's": ('kick', '        if st.st_uid != os.getuid():', '        if False:',
                               'Config.test_the_token_file_must_be_this_users'),
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
    'rate limit read (token)': ('github', "                headers.get('x-ratelimit-remaining') == '0' or retry_after",
                                '                False or retry_after', 'Transports.test_token_api_reads_a_rate_limit'),
    'secondary limit read (token)': ('github', "            limited = e.code == 429 or (e.code == 403 and (",
                                     '            limited = (e.code == 403 and (',
                                     'Transports.test_token_api_reads_a_secondary_rate_limit'),
    'secondary limit message (token)': ('github', "                or SECONDARY in detail.lower()))", '                ))',
                                        'Transports.test_token_api_reads_a_secondary_rate_limit'),
    'a reset bound (token)': ('github', "    return value if value <= most else None", '    return value',
                              'Transports.test_token_api_drops_a_reset_that_is_no_time'),
    'reply size cap (token)': ('github', "        if len(data) > MAX_REPLY:\n            raise ValueError",
                               "        if False:\n            raise ValueError",
                               'Transports.test_token_api_reads_at_most_one_mib'),
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
