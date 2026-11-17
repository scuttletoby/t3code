#!/usr/bin/env python3
"""Tests of fork-checks-ran.py: a fake GitHub (in process, and a local HTTP server for the real client), a fake clock,
throwaway git repositories for the base commit's config. Nothing reaches GitHub. Run: python3 <this>."""
import http.server, importlib.util, json, os, re, shutil, subprocess, sys, tempfile, threading, unittest, urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'fork-checks-ran.py')
CONFIG = os.path.join(HERE, '..', 'fork-actions-policies.json')
_spec = importlib.util.spec_from_file_location('fork_checks_ran', SCRIPT)
fcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fcr)

SHA, BASE = 'a' * 40, 'b' * 40
WF = '.github/workflows/fork-checks.yml'
CFG = {'workflow': WF, 'check': 'No database schema changes', 'outside_check': 'Outside pull request',
       'wait_seconds': 600, 'poll_seconds': 20}
PR_ENV = {'EVENT_NAME': 'pull_request', 'HEAD_SHA': SHA, 'BASE_SHA': BASE, 'OUTSIDE_PR': 'false',
          'DEFAULT_BRANCH': 'main', 'GITHUB_REPOSITORY': 'o/r', 'GITHUB_REF': 'refs/pull/7/merge',
          'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/fork-tests.yml@refs/pull/7/merge'}
GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def job(name, status='completed', conclusion='success'):
    return {'name': name, 'status': status, 'conclusion': conclusion,
            'html_url': f'https://github.invalid/job/{abs(hash(name)) % 1000}'}


def wr(rid, jobs, path=WF, event='pull_request_target', status='completed'):
    return {'id': rid, 'path': path, 'event': event, 'status': status, 'jobs': jobs}


class Clock:
    def __init__(self):
        self.t, self.slept = 1000.0, []

    def now(self):
        return self.t

    def sleep(self, s):
        assert s > 0, s
        self.slept.append(s)
        self.t += s


class Fake:
    """GitHub's runs of fork-checks.yml on SHA (one list per poll, the last one repeats) and each run's jobs (the
    latest attempt only: the script must ask for filter=latest)."""

    def __init__(self, polls, errors=()):
        self.polls, self.errors, self.paths = list(polls), list(errors), []
        self.current = []

    def get(self, path):
        self.paths.append(path)
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == 'actions/workflows/fork-checks.yml/runs':
            assert q['head_sha'] == [SHA], path
            self.current = self.polls.pop(0) if len(self.polls) > 1 else self.polls[0]
            return {'workflow_runs': [{k: v for k, v in r.items() if k != 'jobs'} for r in self.current]}
        m = re.fullmatch(r'actions/runs/(\d+)/jobs', url.path)
        if m and q['filter'] == ['latest']:
            return {'jobs': next(r['jobs'] for r in self.current if r['id'] == int(m.group(1)))}
        raise AssertionError(path)


def check(fake, cfg=CFG, **env):
    clock, out, asked = Clock(), [], []

    def config(base):
        asked.append(base)
        return dict(cfg)
    code = fcr.check({**PR_ENV, **env}, fake, config, clock.now, clock.sleep, out.append)
    return code, '\n'.join(out), clock


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, env={**os.environ, **GIT_ENV}, capture_output=True, text=True,
                          check=True).stdout.strip()


def make_repo(path, commits):
    """A repository at path with one commit per dict of {path: text or None (deleted)}; returns the commit ids."""
    os.makedirs(path)
    git(path, 'init', '-q')
    shas = []
    for files in commits:
        for name, text in files.items():
            full = os.path.join(path, name)
            if text is None:
                git(path, 'rm', '-q', name)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(text if isinstance(text, bytes) else text.encode())
            git(path, 'add', name)
        git(path, 'commit', '-q', '--allow-empty', '-m', f'commit {len(shas)}')
        shas.append(git(path, 'rev-parse', 'HEAD'))
    return shas


def policies(**fork_checks):
    return json.dumps({'fork_checks': {**CFG, **fork_checks}})


class Check(unittest.TestCase):
    # --- not a pull request (#430 item 3) ---------------------------------------------------------------------------
    def test_direct_run_on_the_default_branch_needs_no_check(self):
        for event in ('push', 'workflow_dispatch'):
            fake = Fake([[]])
            code, out, _ = check(fake, EVENT_NAME=event, GITHUB_REF='refs/heads/main',
                                 GITHUB_WORKFLOW_REF='o/r/.github/workflows/fork-tests.yml@refs/heads/main')
            self.assertEqual(code, 0, out)
            self.assertIn(f'not a pull request ({event})', out)
            self.assertEqual(fake.paths, [])

    def test_direct_run_on_another_ref_fails(self):
        # Its fork-ci-result lands on that ref's tip, which may be an open pull request's head.
        for event, ref in (('workflow_dispatch', 'refs/heads/feature'), ('push', 'refs/heads/feature'),
                           ('workflow_dispatch', 'refs/tags/v1'), ('schedule', 'refs/heads/main2')):
            fake = Fake([[]])
            code, out, _ = check(fake, EVENT_NAME=event, GITHUB_REF=ref,
                                 GITHUB_WORKFLOW_REF=f'o/r/.github/workflows/fork-tests.yml@{ref}')
            self.assertEqual(code, 1, f'{event} {ref}: {out}')
            self.assertIn('#430', out)
            self.assertIn('dispatch fork-tests.yml on main with ref=<branch>', out)
            self.assertEqual(fake.paths, [])

    def test_called_run_needs_no_check(self):
        # fork-release.yml calls fork-tests.yml: its check runs are named "<caller job> / ... / fork-ci-result".
        for event, ref in (('workflow_dispatch', 'refs/heads/feature'), ('schedule', 'refs/heads/main')):
            fake = Fake([[]])
            code, out, _ = check(fake, EVENT_NAME=event, GITHUB_REF=ref, DEFAULT_BRANCH='',
                                 GITHUB_WORKFLOW_REF=f'o/r/.github/workflows/fork-follow.yml@{ref}')
            self.assertEqual(code, 0, out)
            self.assertIn('called by .github/workflows/fork-follow.yml', out)
            self.assertEqual(fake.paths, [])

    def test_direct_run_that_cannot_be_placed_fails(self):
        for env in ({'DEFAULT_BRANCH': ''}, {'GITHUB_WORKFLOW_REF': ''}, {'GITHUB_WORKFLOW_REF': 'x/y/fork-tests.yml@a'},
                    {'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/fork-tests.yml'}):
            code, out, _ = check(Fake([[]]), EVENT_NAME='workflow_dispatch', GITHUB_REF='refs/heads/main', **env)
            self.assertEqual(code, 1, f'{env}: {out}')

    # --- the pull request's fork-checks job must succeed (#430 item 4) ------------------------------------------------
    def test_success_at_once_passes_without_sleeping(self):
        code, out, clock = check(Fake([[wr(1, [job(CFG['check'])])]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [])
        self.assertIn('passed', out)

    def test_still_running_waits_for_it(self):
        fake = Fake([[wr(1, [job(CFG['check'], 'queued', None)], status='queued')],
                     [wr(1, [job(CFG['check'], 'in_progress', None)], status='in_progress')],
                     [wr(1, [job(CFG['check'])])]])
        code, out, clock = check(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [20, 20])

    def test_not_finished_at_the_cap_fails_saying_so(self):
        code, out, clock = check(Fake([[wr(1, [job(CFG['check'], 'in_progress', None)], status='in_progress')]]))
        self.assertEqual(code, 1, out)
        self.assertIn('did not finish', out)
        self.assertIn('re-run', out)
        self.assertLessEqual(sum(clock.slept), CFG['wait_seconds'])

    def test_failed_or_cancelled_fails_at_once_with_a_way_out(self):
        for conclusion in ('failure', 'cancelled', 'timed_out', 'neutral', 'action_required', 'stale', None):
            code, out, clock = check(Fake([[wr(1, [job(CFG['check'], 'completed', conclusion)])]]))
            self.assertEqual(code, 1, f'{conclusion}: {out}')
            self.assertEqual(clock.slept, [], conclusion)
            self.assertIn('https://github.invalid/job/', out)
            self.assertIn('push again', out)

    def test_only_skipped_fails_at_once(self):
        code, out, clock = check(Fake([[wr(1, [job(CFG['check'], 'completed', 'skipped')])]]))
        self.assertEqual(code, 1, out)
        self.assertIn('skipped', out)
        self.assertEqual(clock.slept, [])

    def test_newest_run_decides(self):
        # An `edited` event runs fork-checks again on the same head: the newest run is the one that counts.
        for runs, want in (([wr(9, [job(CFG['check'], 'completed', 'failure')]), wr(3, [job(CFG['check'])])], 1),
                           ([wr(3, [job(CFG['check'], 'completed', 'failure')]), wr(9, [job(CFG['check'])])], 0),
                           ([wr(3, [job(CFG['check'])]), wr(9, [job(CFG['check'], 'completed', 'cancelled')])], 1)):
            code, out, _ = check(Fake([runs]))
            self.assertEqual(code, want, f'{[r["id"] for r in runs]}: {out}')

    def test_reads_the_latest_attempt_only(self):
        fake = Fake([[wr(1, [job(CFG['check'])])]])
        self.assertEqual(check(fake)[0], 0)
        self.assertTrue(any('filter=latest' in p for p in fake.paths), fake.paths)

    # --- which runs count -------------------------------------------------------------------------------------------
    def test_only_pull_request_target_runs_count(self):
        # A pull request that adds push: or pull_request: to its own fork-checks.yml gets a same-named job from its
        # own copy (#430 item 2). Only pull_request_target runs main's copy.
        for event in ('pull_request', 'push', 'workflow_dispatch', 'schedule', None):
            fake = Fake([[wr(4, [job(CFG['check'])], event=event)]])
            code, out, _ = check(fake)
            self.assertEqual(code, 1, f'{event}: {out}')
            self.assertNotIn('actions/runs/4/jobs', ' '.join(fake.paths), event)

    def test_a_pull_request_target_run_next_to_a_no_op_one_decides(self):
        fake = Fake([[wr(8, [job(CFG['check'])], event='push'),
                      wr(5, [job(CFG['check'], 'completed', 'failure')])]])
        self.assertEqual(check(fake)[0], 1)

    def test_run_path_with_a_ref_suffix_matches(self):
        self.assertEqual(check(Fake([[wr(1, [job(CFG['check'])], WF + '@refs/heads/main')]]))[0], 0)

    def test_a_run_of_another_workflow_does_not_count(self):
        fake = Fake([[wr(2, [job(CFG['check'])], '.github/workflows/fork-tests.yml')]])
        code, out, _ = check(fake)
        self.assertEqual(code, 1, out)
        self.assertNotIn('actions/runs/2/jobs', ' '.join(fake.paths))

    def test_appears_later_passes_after_bounded_polls(self):
        code, out, clock = check(Fake([[], [wr(1, [], status='queued')], [wr(1, [job(CFG['check'])])]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [20, 20])

    def test_never_appears_fails_after_the_cap_with_the_policy_message_and_a_way_out(self):
        code, out, clock = check(Fake([[]]))
        self.assertEqual(code, 1)
        self.assertIn('fork-checks did not run: check the Actions policy (#401)', out)
        self.assertIn('push again', out)
        self.assertIn('close and reopen', out)
        self.assertLessEqual(sum(clock.slept), CFG['wait_seconds'])
        self.assertGreaterEqual(sum(clock.slept), CFG['wait_seconds'] - CFG['poll_seconds'])

    def test_blocked_run_without_jobs_fails_at_once(self):
        # What a blocked event leaves: a finished run of fork-checks.yml with no job in it.
        code, out, clock = check(Fake([[wr(5, [], status='completed')]]))
        self.assertEqual(code, 1)
        self.assertIn('fork-checks did not run', out)
        self.assertIn('close and reopen', out)
        self.assertEqual(clock.slept, [])

    def test_outside_pull_request_is_refused_at_once(self):
        # fork_checks_ran is skipped for them (its if:); its only fork-checks job, Outside pull request, always fails.
        fake = Fake([[wr(1, [job(CFG['outside_check'])])]])
        code, out, _ = check(fake, OUTSIDE_PR='true')
        self.assertEqual(code, 1, out)
        self.assertIn('outside', out)
        self.assertEqual(fake.paths, [])

    # --- the API ------------------------------------------------------------------------------------------------------
    def test_transient_error_is_retried(self):
        fake = Fake([[wr(1, [job(CFG['check'])])]], errors=[fcr.Transient('HTTP 502'), None, None])
        code, out, _ = check(fake)
        self.assertEqual(code, 0, out)
        self.assertIn('HTTP 502', out)

    def test_a_good_poll_after_an_error_clears_it(self):
        # Mutant M11 of #420: the cap message must name what the last poll saw, not an error two polls back.
        fake = Fake([[]], errors=[fcr.Transient('HTTP 502')])
        code, out, _ = check(fake)
        self.assertEqual(code, 1)
        self.assertIn('fork-checks did not run: check the Actions policy (#401)', out)
        self.assertNotIn('last GitHub API call failed', out)

    def test_persistent_transient_error_fails_closed_naming_it(self):
        fake = Fake([[]], errors=[fcr.Transient('HTTP 503')] * 1000)
        code, out, clock = check(fake)
        self.assertEqual(code, 1)
        self.assertIn('could not be confirmed', out)
        self.assertIn('HTTP 503', out)
        self.assertLessEqual(sum(clock.slept), CFG['wait_seconds'])

    def test_rate_limit_waits_for_the_reset_within_the_cap(self):
        fake = Fake([[wr(1, [job(CFG['check'])])]], errors=[fcr.RateLimited('rate limit', 1000.0 + 90), None, None])
        code, out, clock = check(fake)
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [90])

    def test_rate_limit_past_the_cap_fails_closed(self):
        fake = Fake([[]], errors=[fcr.RateLimited('rate limit', 1000.0 + 5000)])
        code, out, clock = check(fake)
        self.assertEqual(code, 1)
        self.assertIn('rate limit', out)
        self.assertLessEqual(sum(clock.slept), CFG['wait_seconds'])

    def test_fatal_api_error_fails_at_once(self):
        code, out, clock = check(Fake([[]], errors=[fcr.Fatal('HTTP 404 on actions/workflows/fork-checks.yml/runs')]))
        self.assertEqual(code, 1)
        self.assertIn('HTTP 404', out)
        self.assertIn('#401', out)
        self.assertEqual(clock.slept, [])

    def test_unexpected_reply_fails_closed(self):
        class Odd:
            def get(self, path):
                return {'something': []}
        code, out, _ = check(Odd())
        self.assertEqual(code, 1)
        self.assertIn('could not be confirmed', out)

    def test_bad_input_fails(self):
        # Each against a passing fork-checks run, so only the bad input can fail it.
        def passing():
            return Fake([[wr(1, [job(CFG['check'])])]])
        self.assertEqual(check(passing())[0], 0)
        for env, what in (({'HEAD_SHA': 'main'}, 'HEAD_SHA'), ({'BASE_SHA': ''}, 'BASE_SHA'),
                          ({'BASE_SHA': 'b' * 39}, 'BASE_SHA'), ({'OUTSIDE_PR': 'maybe'}, 'OUTSIDE_PR')):
            code, out, _ = check(passing(), **env)
            self.assertEqual((code, what in out), (1, True), (env, out))
        self.assertEqual(check(passing(), cfg={**CFG, 'wait_seconds': 0})[0], 1)
        self.assertEqual(check(passing(), cfg={k: v for k, v in CFG.items() if k != 'check'})[0], 1)

    def test_real_config_has_the_fields(self):
        with open(CONFIG) as f:
            cfg = json.load(f)['fork_checks']
        self.assertEqual(fcr.config_problems(cfg), [])


class BaseConfig(unittest.TestCase):
    """The config comes from the pull request's base commit (#430 item 1): fork-checks runs main's copy, so a pull
    request that renames its job or file must still be judged by the names main's copy produces."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='fork-checks-ran-')

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def load(self, repo, sha):
        return fcr.base_config(sha, cwd=repo, env={**os.environ, **GIT_ENV})

    def test_rename_pull_request_is_judged_by_the_base_config(self):
        repo = os.path.join(self.tmp, 'r')
        base, head = make_repo(repo, [
            {fcr.CONFIG_PATH: policies(), WF: 'on: pull_request_target\n'},
            {fcr.CONFIG_PATH: policies(workflow='.github/workflows/fork-checks2.yml', check='Schema'),
             WF: None, '.github/workflows/fork-checks2.yml': 'on: pull_request_target\n'}])
        self.assertEqual(git(repo, 'rev-parse', 'HEAD'), head)   # the checkout holds the renamed config
        cfg = self.load(repo, base)
        self.assertEqual((cfg['workflow'], cfg['check']), (WF, CFG['check']))
        clock, out = Clock(), []
        code = fcr.check({**PR_ENV, 'BASE_SHA': base}, Fake([[wr(1, [job(CFG['check'])])]]),
                         lambda sha: self.load(repo, sha), clock.now, clock.sleep, out.append)
        self.assertEqual(code, 0, out)

    def test_missing_config_on_the_base_fails_clearly(self):
        repo = os.path.join(self.tmp, 'r')
        base, _ = make_repo(repo, [{'README': 'x'}, {fcr.CONFIG_PATH: policies()}])
        with self.assertRaises(fcr.ConfigError) as e:
            self.load(repo, base)
        self.assertIn(f'no {fcr.CONFIG_PATH} on the base commit {base}', str(e.exception))
        out = []
        code = fcr.check({**PR_ENV, 'BASE_SHA': base}, Fake([[]]), lambda sha: self.load(repo, sha),
                         Clock().now, Clock().sleep, out.append)
        self.assertEqual(code, 1)
        self.assertIn('on the base commit', '\n'.join(out))

    def test_undecodable_config_on_the_base_fails_clearly(self):
        for i, text in enumerate(('{', '[]', '{"fork_checks": 3}', '{"other": {}}', b'{"fork_checks": "\xff"}')):
            repo = os.path.join(self.tmp, f'r{i}')
            base, = make_repo(repo, [{fcr.CONFIG_PATH: text}])
            with self.assertRaises(fcr.ConfigError, msg=text) as e:
                self.load(repo, base)
            self.assertIn('cannot read', str(e.exception))

    def test_base_commit_is_fetched_when_the_checkout_lacks_it(self):
        origin = os.path.join(self.tmp, 'origin')
        base, head = make_repo(origin, [{fcr.CONFIG_PATH: policies()}, {fcr.CONFIG_PATH: policies(check='Schema')}])
        git(origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        git(origin, 'config', 'uploadpack.allowFilter', 'true')
        work = os.path.join(self.tmp, 'work')
        os.makedirs(work)
        git(work, 'init', '-q')
        git(work, 'remote', 'add', 'origin', 'file://' + origin)
        git(work, 'fetch', '-q', '--depth=1', 'origin', head)
        self.assertNotEqual(subprocess.run(['git', 'cat-file', '-e', base + '^{commit}'], cwd=work,
                                           capture_output=True).returncode, 0)
        self.assertEqual(self.load(work, base)['check'], CFG['check'])

    def test_base_commit_that_cannot_be_fetched_fails(self):
        repo = os.path.join(self.tmp, 'r')
        make_repo(repo, [{fcr.CONFIG_PATH: policies()}])
        with self.assertRaises(fcr.ConfigError) as e:
            self.load(repo, 'c' * 40)
        self.assertIn('cannot fetch the base commit', str(e.exception))


class Handler(http.server.BaseHTTPRequestHandler):
    replies = []   # (status, headers, body) per request, in order
    seen = []

    def do_GET(self):
        Handler.seen.append((self.path, self.headers.get('Authorization')))
        status, headers, body = Handler.replies.pop(0)
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):
        pass


class Real(unittest.TestCase):
    """The script as the workflow runs it (its config from the base commit of a real repository), against a local
    HTTP server: the real client and its error classes."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.tmp = tempfile.mkdtemp(prefix='fork-checks-ran-real-')
        cls.repo = os.path.join(cls.tmp, 'r')
        cls.base, = make_repo(cls.repo, [{fcr.CONFIG_PATH: policies()}])
        cls.quick = os.path.join(cls.tmp, 'q')   # a base config with a 2 s cap and 1 s polls
        cls.quick_base, = make_repo(cls.quick, [{fcr.CONFIG_PATH: policies(wait_seconds=2, poll_seconds=1)}])

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def script(self, replies, event='pull_request', fast=False):
        Handler.replies, Handler.seen = list(replies), []
        return subprocess.run(
            [sys.executable, '-I', SCRIPT], capture_output=True, text=True, timeout=60,
            cwd=self.quick if fast else self.repo,
            env={'PATH': os.environ['PATH'], 'HOME': self.tmp, **GIT_ENV, **PR_ENV, 'EVENT_NAME': event,
                 'BASE_SHA': self.quick_base if fast else self.base,
                 'GITHUB_REF': 'refs/heads/main' if event != 'pull_request' else PR_ENV['GITHUB_REF'],
                 'GITHUB_TOKEN': 'tok', 'GITHUB_API_URL': f'http://127.0.0.1:{self.server.server_port}'})

    def test_push_needs_no_token_or_call(self):
        r = self.script([], event='push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('not a pull request (push)', r.stdout)
        self.assertEqual(Handler.seen, [])

    def test_found(self):
        r = self.script([(200, {}, {'workflow_runs': [wr(7, [])]}), (200, {}, {'jobs': [job(CFG['check'])]})])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Handler.seen[0][1], 'Bearer tok')
        self.assertTrue(Handler.seen[0][0].startswith('/repos/o/r/actions/workflows/fork-checks.yml/runs?head_sha=' + SHA), Handler.seen)

    def test_not_found_fails(self):
        r = self.script([(200, {}, {'workflow_runs': []})] * 3, fast=True)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::error::fork-checks did not run: check the Actions policy (#401)', r.stdout)

    def test_forbidden_fails_at_once(self):
        r = self.script([(403, {}, {'message': 'Resource not accessible by integration'})])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('Resource not accessible by integration', r.stdout)
        self.assertEqual(len(Handler.seen), 1)

    def test_rate_limited_then_found(self):
        r = self.script([(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '0'}, {'message': 'API rate limit'}),
                         (200, {}, {'workflow_runs': [wr(7, [])]}),
                         (200, {}, {'jobs': [job(CFG['check'])]})], fast=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('rate limit', r.stdout)

    def test_server_error_retried(self):
        r = self.script([(502, {}, {'message': 'bad gateway'}),
                         (200, {}, {'workflow_runs': [wr(7, [])]}),
                         (200, {}, {'jobs': [job(CFG['check'])]})], fast=True)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
