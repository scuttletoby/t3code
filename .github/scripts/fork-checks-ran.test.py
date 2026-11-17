#!/usr/bin/env python3
"""Tests of fork-checks-ran.py: a fake GitHub (in process, and a local HTTP server for the real client), a fake clock.
Nothing reaches GitHub. Run: python3 <this>."""
import http.server, importlib.util, json, os, re, subprocess, sys, tempfile, threading, unittest, urllib.parse

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'fork-checks-ran.py')
CONFIG = os.path.join(HERE, '..', 'fork-actions-policies.json')
_spec = importlib.util.spec_from_file_location('fork_checks_ran', SCRIPT)
fcr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fcr)

SHA = 'a' * 40
WF = '.github/workflows/fork-checks.yml'
CFG = {'workflow': WF, 'check': 'No database schema changes', 'outside_check': 'Outside pull request',
       'wait_seconds': 600, 'poll_seconds': 20}


def job(name, status='completed', conclusion='success'):
    return {'name': name, 'status': status, 'conclusion': conclusion}


def wr(rid, jobs, path=WF):
    return {'id': rid, 'path': path, 'jobs': jobs}


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
    """GitHub's runs of fork-checks.yml on SHA (one list per poll, the last one repeats) and each run's jobs."""

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
            return {'workflow_runs': [{'id': r['id'], 'path': r['path']} for r in self.current]}
        m = re.fullmatch(r'actions/runs/(\d+)/jobs', url.path)
        if m and q['filter'] == ['all']:
            return {'jobs': next(r['jobs'] for r in self.current if r['id'] == int(m.group(1)))}
        raise AssertionError(path)


def check(fake, event='pull_request', outside='false', sha=SHA, cfg=CFG):
    clock, out = Clock(), []
    code = fcr.check({'EVENT_NAME': event, 'HEAD_SHA': sha, 'OUTSIDE_PR': outside}, dict(cfg), fake,
                     clock.now, clock.sleep, out.append)
    return code, '\n'.join(out), clock


class Check(unittest.TestCase):
    def test_not_a_pull_request_skips_with_a_log_line(self):
        for event in ('push', 'merge_group', 'schedule', 'workflow_dispatch', 'workflow_call'):
            fake = Fake([[]])
            code, out, _ = check(fake, event=event)
            self.assertEqual(code, 0, out)
            self.assertIn(f'not a pull request ({event})', out)
            self.assertEqual(fake.paths, [])

    def test_check_there_at_once_passes_without_sleeping(self):
        code, out, clock = check(Fake([[wr(1, [job(CFG['check'], 'in_progress', None)])]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [])
        self.assertIn('fork-checks ran', out)

    def test_queued_or_failed_counts_as_ran(self):
        for status, conclusion in (('queued', None), ('completed', 'failure'), ('completed', 'cancelled')):
            code, out, _ = check(Fake([[wr(1, [job(CFG['check'], status, conclusion)])]]))
            self.assertEqual(code, 0, f'{status} {conclusion}: {out}')

    def test_run_path_with_a_ref_suffix_matches(self):
        self.assertEqual(check(Fake([[wr(1, [job(CFG['check'])], WF + '@refs/heads/main')]]))[0], 0)

    def test_appears_later_passes_after_bounded_polls(self):
        code, out, clock = check(Fake([[], [wr(1, [])], [wr(1, [job(CFG['check'])])]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [20, 20])

    def test_never_appears_fails_after_the_cap_with_the_policy_message(self):
        code, out, clock = check(Fake([[]]))
        self.assertEqual(code, 1)
        self.assertIn('fork-checks did not run: check the Actions policy (#401)', out)
        self.assertLessEqual(sum(clock.slept), CFG['wait_seconds'])
        self.assertGreaterEqual(sum(clock.slept), CFG['wait_seconds'] - CFG['poll_seconds'])

    def test_blocked_run_without_jobs_fails_after_the_cap(self):
        # What a blocked event leaves: a failed run of fork-checks.yml with no job in it.
        code, out, _ = check(Fake([[{'id': 5, 'path': WF, 'jobs': []}]]))
        self.assertEqual(code, 1)
        self.assertIn('fork-checks did not run', out)

    def test_a_run_of_another_workflow_does_not_count(self):
        fake = Fake([[wr(2, [job(CFG['outside_check'])], '.github/workflows/fork-tests.yml')]])
        code, out, _ = check(fake, outside='true')
        self.assertEqual(code, 1, out)
        self.assertNotIn('actions/runs/2/jobs', ' '.join(fake.paths))

    def test_outside_pull_request_looks_for_the_outside_check(self):
        fake = Fake([[wr(1, [job(CFG['check'], 'completed', 'skipped'), job(CFG['outside_check'], 'completed', 'failure')])]])
        code, out, _ = check(fake, outside='true')
        self.assertEqual(code, 0, out)
        self.assertIn('"Outside pull request"', out)
        code, out, _ = check(Fake([[wr(1, [job(CFG['check'])])]]), outside='true')
        self.assertEqual(code, 1, out)

    def test_own_pull_request_ignores_the_outside_check(self):
        self.assertEqual(check(Fake([[wr(1, [job(CFG['outside_check'])])]]))[0], 1)

    def test_only_skipped_fails_at_once(self):
        code, out, clock = check(Fake([[wr(1, [job(CFG['check'], 'completed', 'skipped')])]]))
        self.assertEqual(code, 1, out)
        self.assertIn('skipped', out)
        self.assertEqual(clock.slept, [])

    def test_skipped_in_one_run_and_ran_in_another_passes(self):
        fake = Fake([[wr(1, [job(CFG['check'], 'completed', 'skipped')]), wr(3, [job(CFG['check'])])]])
        self.assertEqual(check(fake)[0], 0)

    def test_stops_reading_runs_at_the_first_that_ran(self):
        fake = Fake([[wr(1, [job(CFG['check'])]), wr(2, [job(CFG['check'])])]])
        self.assertEqual(check(fake)[0], 0)
        self.assertNotIn('actions/runs/2/jobs', ' '.join(fake.paths))

    def test_transient_error_is_retried(self):
        fake = Fake([[wr(1, [job(CFG['check'])])]], errors=[fcr.Transient('HTTP 502'), None, None])
        code, out, _ = check(fake)
        self.assertEqual(code, 0, out)
        self.assertIn('HTTP 502', out)

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
        self.assertEqual(check(Fake([[]]), sha='main')[0], 1)
        self.assertEqual(check(Fake([[]]), outside='maybe')[0], 1)
        self.assertEqual(check(Fake([[]]), cfg={**CFG, 'wait_seconds': 0})[0], 1)
        self.assertEqual(check(Fake([[]]), cfg={k: v for k, v in CFG.items() if k != 'check'})[0], 1)

    def test_real_config_has_the_fields(self):
        with open(CONFIG) as f:
            cfg = json.load(f)['fork_checks']
        self.assertEqual(fcr.config_problems(cfg), [])
        self.assertLessEqual(cfg['wait_seconds'], 900, 'the cap must fit the job timeout')


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
    """The script as the workflow runs it, against a local HTTP server: the real client and its error classes."""

    @classmethod
    def setUpClass(cls):
        cls.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def script(self, replies, event='pull_request', cfg=None):
        Handler.replies, Handler.seen = list(replies), []
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump({'fork_checks': cfg or CFG}, f)
        try:
            return subprocess.run(
                [sys.executable, '-I', SCRIPT, '--config', f.name], capture_output=True, text=True, timeout=60,
                env={'PATH': os.environ['PATH'], 'EVENT_NAME': event, 'HEAD_SHA': SHA, 'OUTSIDE_PR': 'false',
                     'GITHUB_TOKEN': 'tok', 'GITHUB_REPOSITORY': 'o/r',
                     'GITHUB_API_URL': f'http://127.0.0.1:{self.server.server_port}'})
        finally:
            os.unlink(f.name)

    def test_push_needs_no_token_or_call(self):
        r = self.script([], event='push')
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('not a pull request (push)', r.stdout)
        self.assertEqual(Handler.seen, [])

    def test_found(self):
        r = self.script([(200, {}, {'workflow_runs': [{'id': 7, 'path': WF}]}),
                         (200, {}, {'jobs': [job(CFG['check'])]})])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(Handler.seen[0][1], 'Bearer tok')
        self.assertTrue(Handler.seen[0][0].startswith('/repos/o/r/actions/workflows/fork-checks.yml/runs?head_sha=' + SHA), Handler.seen)

    def test_not_found_fails(self):
        r = self.script([(200, {}, {'workflow_runs': []})] * 3, cfg={**CFG, 'wait_seconds': 2, 'poll_seconds': 1})
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('::error::fork-checks did not run: check the Actions policy (#401)', r.stdout)

    def test_forbidden_fails_at_once(self):
        r = self.script([(403, {}, {'message': 'Resource not accessible by integration'})])
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('Resource not accessible by integration', r.stdout)
        self.assertEqual(len(Handler.seen), 1)

    def test_rate_limited_then_found(self):
        r = self.script([(403, {'X-RateLimit-Remaining': '0', 'X-RateLimit-Reset': '0'}, {'message': 'API rate limit'}),
                         (200, {}, {'workflow_runs': [{'id': 7, 'path': WF}]}),
                         (200, {}, {'jobs': [job(CFG['check'])]})], cfg={**CFG, 'poll_seconds': 1})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn('rate limit', r.stdout)

    def test_server_error_retried(self):
        r = self.script([(502, {}, {'message': 'bad gateway'}),
                         (200, {}, {'workflow_runs': [{'id': 7, 'path': WF}]}),
                         (200, {}, {'jobs': [job(CFG['check'])]})], cfg={**CFG, 'poll_seconds': 1})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


if __name__ == '__main__':
    unittest.main()
