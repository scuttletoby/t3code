"""ci_demand.py: per-day CI hours, runs per workflow, queue wait per runner label and cancelled/failed share, read
with GET-only `gh api` calls. Every case runs the real command against a fake `gh` on PATH that serves recorded
pages (testdata/) and logs each call: no network.
Run: python3 fork-ci/actions/ci-demand/ci_demand.test.py
"""
import contextlib, copy, io, json, os, shutil, subprocess, sys, tempfile, unittest
from unittest import mock

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import ci_demand as M   # noqa: E402

R = 'repos/acme/widgets/actions/runs'
DAY1 = f'{R}?created=2026-11-01T00:00:00Z..2026-11-01T23:59:59Z&per_page=100&page=1'
DAY2 = f'{R}?created=2026-11-02T00:00:00Z..2026-11-02T23:59:59Z&per_page=100&page=1'
DAY4 = f'{R}?created=2026-11-04T00:00:00Z..2026-11-04T23:59:59Z&per_page=100&page=1'   # "today" at NOW
J101, J201 = (f'{R}/{n}/jobs?filter=all&per_page=100&page=1' for n in (101, 201))
NOW = 1793793600.0   # 2026-11-04T12:00:00Z
with open(os.path.join(HERE, 'testdata', 'two-days.json')) as f:
    PAGES = json.load(f)['pages']

# The fake gh: answers from $FAKE_GH_ROUTES ({endpoint: [answer, ...]}, the n-th call gets the n-th answer, the
# last one repeats), logs argv to $FAKE_GH_LOG. An answer is {"status", "headers", "body"} or {"raw": stdout}; a
# header set to null is left out. Like the real gh, it writes the body as UTF-8 bytes whatever the locale.
FAKE_GH = r'''
import json, os, sys
log = os.environ['FAKE_GH_LOG']
with open(log, 'a') as f:
    f.write(json.dumps(sys.argv[1:]) + '\n')
routes = json.load(open(os.environ['FAKE_GH_ROUTES']))
ep = sys.argv[-1]
calls = sum(1 for line in open(log) if json.loads(line)[-1] == ep)
answers = routes.get(ep) or [{"status": 404, "body": {"message": "Not Found"}}]
a = answers[min(calls, len(answers)) - 1]
if 'raw' in a:
    sys.stdout.write(a['raw']); sys.stderr.write('connection refused\n'); sys.exit(1)
h = {"X-Ratelimit-Limit": "5000", "X-Ratelimit-Remaining": "4000", "X-Ratelimit-Reset": "1793797200", **a.get('headers', {})}
body = a['body'] if isinstance(a['body'], str) else json.dumps(a['body'], ensure_ascii=False)
head = f"HTTP/2.0 {a['status']} X\r\n" + ''.join(f"{k}: {v}\r\n" for k, v in h.items() if v is not None)
sys.stdout.buffer.write((head + "\r\n" + body).encode('utf-8'))
sys.exit(0 if a['status'] == 200 else 1)
'''


def ok(body, **headers):
    return {'status': 200, 'headers': headers, 'body': body}


class Case(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='ci-demand-')
        self.addCleanup(shutil.rmtree, self.d, True)
        bin_ = os.path.join(self.d, 'bin')
        os.makedirs(bin_)
        with open(os.path.join(bin_, 'gh'), 'w') as f:
            f.write(f'#!{sys.executable}\n{FAKE_GH}')
        os.chmod(os.path.join(bin_, 'gh'), 0o755)
        self.log = os.path.join(self.d, 'gh.log')
        self.routes_file = os.path.join(self.d, 'routes.json')
        self.cache = os.path.join(self.d, 'cache')
        env = {'PATH': bin_ + os.pathsep + os.environ['PATH'], 'FAKE_GH_LOG': self.log,
               'FAKE_GH_ROUTES': self.routes_file}
        old = {k: os.environ.get(k) for k in env}
        os.environ.update(env)
        self.addCleanup(lambda: [os.environ.pop(k) if v is None else os.environ.__setitem__(k, v) for k, v in old.items()])
        self.slept = []
        for name, val in (('now', lambda: self.clock), ('sleep', self.slept.append)):
            orig = getattr(M, name)
            setattr(M, name, val)
            self.addCleanup(setattr, M, name, orig)
        self.clock = NOW
        self.serve({k: [ok(v)] for k, v in PAGES.items()})

    def serve(self, routes):
        self.routes = routes
        with open(self.routes_file, 'w') as f:
            json.dump(routes, f)

    def calls(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def run_main(self, *args, days=('--since', '2026-11-01', '--until', '2026-11-02'), repo=('--repo', 'acme/widgets')):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = M.main([*repo, *days, '--cache', self.cache, *args])
        self.err = err.getvalue()
        return code, out.getvalue()

    def numbers(self, *args, **kw):
        code, out = self.run_main('--json', *args, **kw)
        return code, json.loads(out) if out else None


class Numbers(Case):
    def test_per_day_numbers(self):
        code, doc = self.numbers()
        self.assertEqual(code, 0, self.err)
        d1, d2, tot = doc['days']['2026-11-01'], doc['days']['2026-11-02'], doc['days']['total']
        # 30m + 5m + 10m (a cancelled job still used its runner) + 60m + 60m (both attempts of run 104)
        self.assertEqual(d1['ci_hours'], 2.75)
        self.assertEqual(d2['ci_hours'], 0.5)
        self.assertEqual(tot['ci_hours'], 3.25)
        self.assertEqual(d1['workflows'], {'ci.yml': 2, 'labels.yml': 1, 'nightly.yml': 1})
        self.assertEqual(tot['runs'], 5)
        self.assertEqual((d1['cancelled_pct'], d1['failed_pct']), (25.0, 25.0))   # of 4 completed runs
        self.assertEqual((d2['cancelled_pct'], d2['failed_pct']), (0.0, 0.0))
        # build waits 60, 30, 600, 120 s: nearest rank p50 60, p90 600; skipped and never-started jobs are left out
        self.assertEqual(d1['queue_wait_s'], {'self-hosted,build': {'n': 4, 'p50': 60.0, 'p90': 600.0, 'max': 600.0},
                                              'ubuntu-latest': {'n': 1, 'p50': 10.0, 'p90': 10.0, 'max': 10.0}})
        self.assertEqual(tot['queue_wait_s']['self-hosted,build']['n'], 5)
        self.assertEqual(tot['queue_wait_s']['self-hosted,build']['max'], 1200.0)
        self.assertEqual(d1['never_started_jobs'], 1)
        self.assertIsNone(doc['incomplete'])
        self.assertIsNone(doc['cache_write_failed'])

    def test_text_output(self):
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn('2026-11-01: 2.75 CI hours, 4 runs, cancelled 25.0%, failed 25.0%', out)
        self.assertIn('  wait self-hosted,build: n 4, p50 1m00s, p90 10m00s, max 10m00s', out)
        self.assertIn('total: 3.25 CI hours, 5 runs', out)
        self.assertNotIn('INCOMPLETE', out)

    def test_get_only_and_no_request_for_a_skipped_run(self):
        self.run_main()
        calls = self.calls()
        self.assertTrue(calls)
        for c in calls:
            self.assertEqual(c[:4], ['api', '-X', 'GET', '--include'], c)
            self.assertEqual(len(c), 5, c)   # no -f/-F/--input/--method: nothing can be sent
        self.assertFalse([c for c in calls if '/103/' in c[-1]])   # run 103 was skipped: it ran no job
        self.assertEqual(len(calls), 6)

    def test_percentile_is_nearest_rank(self):
        self.assertEqual(M.pct([5], 90), 5)
        self.assertEqual(M.pct([1, 2, 3, 4], 50), 2)
        self.assertEqual(M.pct(list(range(1, 11)), 90), 9)
        self.assertEqual(M.pct(list(range(1, 11)), 91), 10)

    def test_job_still_queued_and_job_in_progress(self):
        p = copy.deepcopy(PAGES)
        p[DAY2]['workflow_runs'][0].update(status='in_progress', conclusion=None)
        j = p[f'{R}/201/jobs?filter=all&per_page=100&page=1']
        j['jobs'][0].update(status='in_progress', completed_at=None)
        j['jobs'].append({**j['jobs'][0], 'status': 'queued', 'runner_name': None, 'started_at': None})
        j['total_count'] = 2
        self.serve({k: [ok(v)] for k, v in p.items()})
        code, doc = self.numbers()
        self.assertEqual(code, 0)
        d2 = doc['days']['2026-11-02']
        self.assertEqual((d2['ci_hours'], d2['still_queued_jobs'], d2['cancelled_pct']), (0.0, 1, None))
        self.assertEqual(d2['queue_wait_s']['self-hosted,build']['n'], 1)   # its wait is known once it started


class Paging(Case):
    def test_runs_and_jobs_over_several_pages(self):
        runs = [{'id': 300 + i, 'path': '.github/workflows/ci.yml', 'status': 'completed', 'conclusion': 'success',
                 'run_attempt': 1, 'created_at': '2026-11-01T08:00:00Z'} for i in range(150)]
        job = {'name': 'j', 'labels': ['x'], 'created_at': '2026-11-01T08:00:00Z', 'started_at': '2026-11-01T08:00:00Z',
               'completed_at': '2026-11-01T08:00:36Z', 'status': 'completed', 'conclusion': 'success', 'runner_name': 'r'}
        routes = {DAY1: [ok({'total_count': 150, 'workflow_runs': runs[:100]})],
                  DAY1.replace('&page=1', '&page=2'): [ok({'total_count': 150, 'workflow_runs': runs[100:]})]}
        for r in runs:
            routes[f'{R}/{r["id"]}/jobs?filter=all&per_page=100&page=1'] = [ok({'total_count': 1, 'jobs': [job]})]
        big = f'{R}/300/jobs?filter=all&per_page=100&page='
        routes[big + '1'] = [ok({'total_count': 120, 'jobs': [job] * 100})]
        routes[big + '2'] = [ok({'total_count': 120, 'jobs': [job] * 20})]
        self.serve(routes)
        code, doc = self.numbers('--max-requests', '500', days=('--since', '2026-11-01', '--until', '2026-11-01'))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(doc['days']['2026-11-01']['runs'], 150)
        self.assertEqual(doc['days']['2026-11-01']['queue_wait_s']['x']['n'], 149 + 120)
        self.assertEqual(doc['days']['2026-11-01']['ci_hours'], round((149 + 120) * 36 / 3600, 2))

    def test_a_window_over_the_1000_run_cap_is_split(self):
        first, second = (f'{R}?created=2026-11-01T00:00:00Z..2026-11-01T11:59:59Z&per_page=100&page=1',
                         f'{R}?created=2026-11-01T12:00:00Z..2026-11-01T23:59:59Z&per_page=100&page=1')
        d1 = PAGES[DAY1]['workflow_runs']
        routes = {k: [ok(v)] for k, v in PAGES.items()}
        routes[DAY1] = [ok({'total_count': 1500, 'workflow_runs': d1})]
        routes[first] = [ok({'total_count': 3, 'workflow_runs': [r for r in d1 if r['created_at'] < '2026-11-01T12']})]
        routes[second] = [ok({'total_count': 1, 'workflow_runs': [r for r in d1 if r['created_at'] >= '2026-11-01T12']})]
        self.serve(routes)
        code, doc = self.numbers()
        self.assertEqual(code, 0, self.err)
        self.assertEqual(doc['days']['2026-11-01']['runs'], 4)
        self.assertIn(first, [c[-1] for c in self.calls()])


class State(Case):
    """Runs again, stops half-way, meets existing, old or empty state."""

    def test_second_run_uses_the_cache_only(self):
        self.assertEqual(self.numbers()[0], 0)
        n = len(self.calls())
        code, doc = self.numbers()
        self.assertEqual((code, len(self.calls()) - n, doc['requests'], doc['cached']), (0, 0, 0, 6))

    def test_unfinished_pages_are_not_cached(self):
        p = copy.deepcopy(PAGES)
        p[DAY2]['workflow_runs'][0].update(status='in_progress', conclusion=None)
        self.serve({k: [ok(v)] for k, v in p.items()})
        self.numbers()
        n = len(self.calls())
        self.numbers()
        again = [c[-1] for c in self.calls()[n:]]
        self.assertEqual(sorted(again), sorted([DAY2, f'{R}/201/jobs?filter=all&per_page=100&page=1']))

    def test_a_rerun_is_fetched_again_once_the_list_page_is_old(self):
        self.numbers()
        p = copy.deepcopy(PAGES)
        p[DAY2]['workflow_runs'][0]['run_attempt'] = 2
        self.serve({k: [ok(v)] for k, v in p.items()})
        n = len(self.calls())
        self.clock = NOW + M.LIST_MAX_AGE + 1
        self.numbers()
        again = sorted(c[-1] for c in self.calls()[n:])
        self.assertEqual(again, sorted([DAY1, DAY2, f'{R}/201/jobs?filter=all&per_page=100&page=1']))

    def test_broken_or_foreign_cache_files_are_fetched_again(self):
        self.numbers()
        files = sorted(os.listdir(self.cache))
        with open(os.path.join(self.cache, files[0]), 'w') as f:
            f.write('{"key": "trunc')                                   # a write cut short by hand
        with open(os.path.join(self.cache, files[1]), 'w') as f:
            json.dump({'key': 'something else', 'body': {}}, f)          # not the page it is named for
        n = len(self.calls())
        code, doc = self.numbers()
        self.assertEqual((code, len(self.calls()) - n), (0, 2))
        self.assertEqual(doc['days']['total']['ci_hours'], 3.25)

    def test_old_part_files_go_and_fresh_ones_stay(self):
        os.makedirs(self.cache)
        old, fresh = os.path.join(self.cache, '.part-1-old'), os.path.join(self.cache, '.part-2-new')
        for p in (old, fresh):
            open(p, 'w').close()
        os.utime(old, (NOW - M.PART_AGE - 5,) * 2)
        os.utime(fresh, (NOW - 10,) * 2)
        self.numbers()
        self.assertFalse(os.path.exists(old))
        self.assertTrue(os.path.exists(fresh))

    def test_stop_half_way_then_run_again_finishes_without_asking_twice(self):
        code, doc = self.numbers('--max-requests', '4')
        self.assertEqual(code, 3)
        self.assertIn('--max-requests 4', doc['incomplete'])
        self.assertTrue(doc['run_list_complete'])
        self.assertEqual(doc['days']['total']['runs_without_jobs'], 2)
        first = [c[-1] for c in self.calls()]
        code, doc = self.numbers()
        self.assertEqual(code, 0)
        second = [c[-1] for c in self.calls()[len(first):]]
        self.assertFalse(set(first) & set(second), 'a saved page was asked for again')
        self.assertEqual(doc['days']['total']['ci_hours'], 3.25)

    def test_stop_inside_the_run_list_says_so(self):
        code, out = self.run_main('--max-requests', '1')
        self.assertEqual(code, 3)
        self.assertIn('INCOMPLETE: --max-requests 1 reached, run list not finished', out)
        self.assertIn('run again with the same --cache', out)   # the page it got was saved
        self.assertNotIn('cache not written', out)

    def test_cache_folder_is_private(self):
        self.numbers()
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o700)

    def test_cache_files_damaged_or_in_another_format_are_fetched_again(self):
        self.assertEqual(self.numbers()[0], 0)
        damage = {'date not a number': lambda d: d.update(saved='yesterday'), 'no date': lambda d: d.pop('saved'),
                  'date in the future': lambda d: d.update(saved=NOW + 86400), 'body not an object': lambda d: d.update(body=[]),
                  'date NaN': lambda d: d.update(saved=float('nan')),   # json.load reads NaN, never stale
                  'date -Infinity': lambda d: d.update(saved=float('-inf')),
                  'older format (none)': lambda d: d.pop('v'), 'older format': lambda d: d.update(v=M.FORMAT - 1),
                  'newer format': lambda d: d.update(v=M.FORMAT + 1)}
        for what, change in damage.items():
            for name in os.listdir(self.cache):
                p = os.path.join(self.cache, name)
                with open(p) as f:
                    doc = json.load(f)
                change(doc)
                with open(p, 'w') as f:
                    json.dump(doc, f)
            code, doc = self.numbers()
            self.assertEqual((code, doc['requests'], doc['cached']), (0, 6, 0), (what, self.err))
            self.assertEqual(doc['days']['total']['ci_hours'], 3.25, what)
        self.assertEqual(self.numbers()[1]['requests'], 0)   # the pages fetched again were saved again

    def test_a_write_that_fails_half_way_leaves_no_page_and_goes_on(self):
        real = json.dump

        def half(obj, f, **kw):   # the disk fills up in the middle of a page
            f.write(json.dumps(obj)[:40])
            raise OSError(28, 'No space left on device')

        with mock.patch.object(M.json, 'dump', half):
            code, doc = self.numbers()
        self.assertIs(json.dump, real)
        self.assertEqual(code, 0, self.err)
        self.assertEqual(doc['days']['total']['ci_hours'], 3.25)
        self.assertEqual(self.err.count('cache not written'), 1, self.err)
        self.assertEqual(os.listdir(self.cache), [])   # no half page under its final name, no .part- left
        self.assertEqual(self.numbers()[1]['requests'], 6)

    def test_a_cache_that_cannot_be_written_shows_in_the_result(self):
        # A read-only folder or a full disk: nothing is saved, so "run again with the same --cache" would fetch the
        # same pages again (and a range over --max-requests would never finish). The result says so, --json too.
        def read_only(src, dst):
            raise OSError(30, 'Read-only file system')

        with mock.patch.object(M.os, 'replace', read_only):
            code, doc = self.numbers()
            self.assertEqual(code, 0, self.err)
            self.assertIn('Read-only file system', doc['cache_write_failed'])
            self.assertIsNone(doc['incomplete'])
            code, doc = self.numbers('--max-requests', '4')
            self.assertEqual(code, 3)
            self.assertIn('Read-only file system', doc['cache_write_failed'])
            code, out = self.run_main('--max-requests', '4')
        self.assertEqual(code, 3)
        head = out.splitlines()[0]
        self.assertIn('INCOMPLETE: --max-requests 4 reached', head)
        self.assertIn('cache not written (', head)
        self.assertNotIn('run again with the same --cache', head)
        self.assertIn('fix --cache first', head)
        self.assertEqual(os.listdir(self.cache), [])

    def test_a_write_killed_half_way_leaves_only_a_part_file(self):
        class Killed(BaseException):   # the process dies: nothing in the tool gets to clean up
            pass

        def half(obj, f, **kw):
            f.write(json.dumps(obj)[:40])
            f.flush()
            raise Killed

        with mock.patch.object(M.json, 'dump', half), self.assertRaises(Killed):
            self.numbers()
        left = os.listdir(self.cache)
        self.assertEqual(len(left), 1)
        self.assertTrue(left[0].startswith('.part-'), left)   # never under the name a later run reads
        code, doc = self.numbers()
        self.assertEqual((code, doc['requests'], doc['days']['total']['ci_hours']), (0, 6, 3.25))

    def test_run_list_of_today_or_the_last_hour_is_not_cached(self):
        run = {'id': 401, 'path': '.github/workflows/ci.yml', 'status': 'completed', 'conclusion': 'skipped',
               'run_attempt': 1, 'created_at': '2026-11-04T01:00:00Z'}
        self.serve({DAY4: [ok({'total_count': 1, 'workflow_runs': [run]})]})
        today = ('--since', '2026-11-04', '--until', '2026-11-04')
        for clock, cached in ((NOW, False), (NOW, False),                       # today, all runs completed
                              (NOW + 12.5 * 3600, False), (NOW + 12.5 * 3600, False),   # ended 30 min ago
                              (NOW + 13.5 * 3600, False), (NOW + 13.5 * 3600, True)):   # ended over an hour ago
            self.clock = clock
            code, doc = self.numbers(days=today)
            self.assertEqual((code, doc['cached'] == 1), (0, cached), clock)

    def test_jobs_page_with_an_unfinished_job_is_not_cached(self):
        p = copy.deepcopy(PAGES)
        p[J201]['jobs'][0].update(status='in_progress', conclusion=None, completed_at=None)   # the run says completed
        self.serve({k: [ok(v)] for k, v in p.items()})
        self.numbers()
        n = len(self.calls())
        self.numbers()
        self.assertEqual([c[-1] for c in self.calls()[n:]], [J201])

    def test_existing_cache_folder_is_made_private(self):
        os.makedirs(self.cache)
        os.chmod(self.cache, 0o755)
        self.assertEqual(self.numbers()[0], 0, self.err)
        self.assertEqual(os.stat(self.cache).st_mode & 0o777, 0o700)

    def test_cache_folder_that_cannot_be_made_private_is_refused(self):
        # FAT, WSL /mnt/c, some network mounts: chmod "works" but the mode stays open to others.
        os.makedirs(self.cache)
        os.chmod(self.cache, 0o755)
        with mock.patch.object(M.os, 'chmod', lambda path, mode: None):
            code, doc = self.numbers()
        self.assertEqual((code, doc, self.calls()), (2, None, []))
        self.assertIn('cannot be made private (0700)', self.err)

    def test_cache_folder_of_another_user_is_refused(self):
        os.makedirs(self.cache)
        other = os.stat(self.cache).st_uid + 1
        with mock.patch.object(os, 'getuid', lambda: other):
            code, doc = self.numbers()
        self.assertEqual((code, doc, self.calls()), (2, None, []))
        self.assertIn('belongs to another user', self.err)

    def test_os_without_user_ids_runs(self):
        # Windows: no os.getuid and no owner or mode bits; the tool must not crash before its first request.
        real = os.getuid
        del os.getuid
        try:
            code, doc = self.numbers()
            self.assertFalse(hasattr(os, 'getuid'))
        finally:
            os.getuid = real
        self.assertEqual(code, 0, self.err)
        self.assertEqual(doc['days']['total']['ci_hours'], 3.25)

    def test_empty_range_and_new_cache_folder(self):
        self.serve({k: [ok({'total_count': 0, 'workflow_runs': []})] for k in (DAY1, DAY2)})
        code, out = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn(': 2 requests, 0 cached pages', out)
        self.assertTrue(os.path.isdir(self.cache))
        code, doc = self.numbers()
        self.assertEqual((code, doc['days']), (0, {}))


class RateLimit(Case):
    def test_429_waits_retry_after_and_goes_on(self):
        self.routes[DAY1] = [{'status': 429, 'headers': {'Retry-After': '7'}, 'body': {'message': 'slow down'}},
                             ok(PAGES[DAY1])]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual((code, self.slept), (0, [7]))
        self.assertEqual(doc['days']['total']['ci_hours'], 3.25)

    def test_reserve_without_a_limit_header_is_1000(self):
        self.routes[DAY2] = [ok(PAGES[DAY2], **{'X-Ratelimit-Limit': None, 'X-Ratelimit-Remaining': '900'})]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual(code, 3, self.err)
        self.assertIn('900 left, reserve 1000', doc['incomplete'])

    def test_secondary_limit_without_retry_after_waits_a_minute(self):
        self.routes[DAY1] = [{'status': 403, 'body': {'message': 'You have exceeded a secondary rate limit'}},
                             ok(PAGES[DAY1])]
        self.serve(self.routes)
        self.assertEqual(self.numbers()[0], 0)
        self.assertEqual(self.slept, [60])

    def test_exhausted_limit_with_a_far_reset_stops_incomplete(self):
        self.routes[DAY2] = [{'status': 403, 'headers': {'X-Ratelimit-Remaining': '0',
                                                         'X-Ratelimit-Reset': str(int(NOW) + 1800)},
                              'body': {'message': 'API rate limit exceeded'}}]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual((code, self.slept), (3, []))
        self.assertIn('rate limited (403)', doc['incomplete'])
        self.assertFalse(doc['run_list_complete'])

    def test_exhausted_limit_with_a_near_reset_waits(self):
        self.routes[DAY2] = [{'status': 403, 'headers': {'X-Ratelimit-Remaining': '0',
                                                         'X-Ratelimit-Reset': str(int(NOW) + 30)},
                              'body': {'message': 'API rate limit exceeded'}}, ok(PAGES[DAY2])]
        self.serve(self.routes)
        self.assertEqual(self.numbers()[0], 0)
        self.assertEqual(self.slept, [31])

    def test_reserve_stops_before_the_limit_runs_out(self):
        self.routes[DAY2] = [ok(PAGES[DAY2], **{'X-Ratelimit-Remaining': '900'})]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual(code, 3)
        self.assertIn('900 left, reserve 1000', doc['incomplete'])
        self.assertEqual(len(self.calls()), 2)   # the page that said so is used, no request after it
        self.assertEqual(doc['days']['2026-11-02']['runs'], 1)
        self.assertEqual(self.numbers('--reserve', '100')[0], 0)

    def test_reserve_reached_on_a_jobs_page_stops_there(self):
        self.routes[J101] = [ok(PAGES[J101], **{'X-Ratelimit-Remaining': '900'})]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual(code, 3)
        self.assertIn('900 left, reserve 1000', doc['incomplete'])
        self.assertEqual([c[-1] for c in self.calls()], [DAY1, DAY2, J101])   # no request after it
        self.assertEqual(doc['days']['total']['runs_without_jobs'], 4)   # 101 too: its run was not finished
        n = len(self.calls())
        self.assertEqual(self.numbers('--reserve', '100')[0], 0)
        self.assertNotIn(J101, [c[-1] for c in self.calls()[n:]])   # the page that said stop was saved

    def test_default_reserve_follows_the_limit_so_a_workflow_token_works(self):
        token = {'X-Ratelimit-Limit': '1000', 'X-Ratelimit-Remaining': '990'}   # GITHUB_TOKEN: 1000 an hour
        self.serve({k: [ok(v, **token)] for k, v in PAGES.items()})
        self.assertEqual(self.numbers()[0], 0, self.err)
        shutil.rmtree(self.cache)
        self.routes = {k: [ok(v, **token)] for k, v in PAGES.items()}
        self.routes[DAY2] = [ok(PAGES[DAY2], **{**token, 'X-Ratelimit-Remaining': '150'})]
        self.serve(self.routes)
        code, doc = self.numbers()
        self.assertEqual(code, 3)
        self.assertIn('150 left, reserve 200', doc['incomplete'])

    def test_server_error_is_retried_and_a_dead_network_fails(self):
        self.routes[DAY1] = [{'status': 502, 'body': 'bad gateway'}, ok(PAGES[DAY1])]
        self.serve(self.routes)
        self.assertEqual(self.numbers()[0], 0)
        self.assertEqual(self.slept, [2])
        shutil.rmtree(self.cache)
        self.serve({DAY1: [{'raw': ''}]})
        code, _ = self.numbers()
        self.assertEqual(code, 2)
        self.assertIn('connection refused', self.err)
        self.assertEqual(len([c for c in self.calls() if c[-1] == DAY1]), 2 + M.RETRIES + 1)

    def test_not_found_or_no_access_fails_without_retry(self):
        self.serve({})
        code, doc = self.numbers()
        self.assertEqual((code, doc), (2, None))
        self.assertIn('HTTP 404', self.err)
        self.assertEqual(len(self.calls()), 1)


class Arguments(Case):
    def test_config_file_names_the_repo(self):
        cfg = os.path.join(self.d, 'fleet.json')
        with open(cfg, 'w') as f:
            json.dump({'ciDemand': {'repo': 'acme/widgets'}, 'other': 1}, f)
        self.assertEqual(self.numbers(repo=('--config', cfg))[0], 0)
        with open(cfg, 'w') as f:
            json.dump({'repo': 'acme/widgets'}, f)   # a top-level repo is some other tool's
        self.assertEqual(self.numbers(repo=('--config', cfg))[0], 2)
        self.assertIn('no ciDemand.repo', self.err)

    def test_config_with_a_utf8_bom_is_read(self):
        cfg = os.path.join(self.d, 'project.json')   # some Windows editors save UTF-8 with a byte-order mark
        with open(cfg, 'w', encoding='utf-8-sig') as f:
            json.dump({'ciDemand': {'repo': 'acme/widgets'}}, f)
        code, doc = self.numbers(repo=('--config', cfg))
        self.assertEqual(code, 0, self.err)
        self.assertEqual(doc['repo'], 'acme/widgets')

    def test_a_locale_that_is_not_utf8_reads_names_and_config_as_utf8(self):
        # The script itself, in the C locale with UTF-8 mode off (ASCII): gh output and the config file are UTF-8
        # whatever the locale (a Windows code page, an old server), so non-ASCII names survive and nothing is refused.
        env = {**os.environ, 'LC_ALL': 'C', 'PYTHONCOERCECLOCALE': '0', 'PYTHONUTF8': '0'}
        probe = subprocess.run([sys.executable, '-I', '-X', 'utf8=0', '-c', 'import locale; print(locale.getencoding())'],
                               env=env, capture_output=True, text=True, check=True).stdout.strip()
        if probe.lower().replace('-', '') in ('utf8', 'utf8mb4'):
            self.skipTest(f'the C locale is UTF-8 here ({probe})')
        p = copy.deepcopy(PAGES)
        p[DAY2]['workflow_runs'][0]['path'] = '.github/workflows/bäck-énd.yml'
        self.serve({k: [ok(v)] for k, v in p.items()})
        cfg = os.path.join(self.d, 'project.json')
        with open(cfg, 'w', encoding='utf-8') as f:
            json.dump({'owner': 'Zoë Ångström', 'ciDemand': {'repo': 'acme/widgets'}}, f, ensure_ascii=False)
        r = subprocess.run([sys.executable, '-I', '-B', '-X', 'utf8=0', os.path.join(HERE, 'ci_demand.py'),
                            '--config', cfg, '--since', '2026-11-01', '--until', '2026-11-02', '--cache', self.cache,
                            '--json'], env=env, capture_output=True)
        self.assertEqual(r.returncode, 0, r.stderr.decode('utf-8', 'replace'))
        doc = json.loads(r.stdout)
        self.assertEqual(doc['days']['2026-11-02']['workflows'], {'bäck-énd.yml': 1})

    def test_days_means_full_utc_days_before_today(self):
        self.routes[DAY1.replace('11-01T', '11-03T')] = [ok({'total_count': 0, 'workflow_runs': []})]
        self.serve(self.routes)
        code, doc = self.numbers(days=('--days', '3'))
        self.assertEqual((code, doc['since'], doc['until']), (0, '2026-11-01', '2026-11-03'))

    def test_bad_arguments(self):
        for repo, days in ((('--repo', 'acme'), ('--days', '1')),
                           (('--repo', 'a/b;rm'), ('--days', '1')),
                           ((), ('--days', '1')),
                           (('--repo', 'a/b'), ('--days', '0')),
                           (('--repo', 'a/b'), ('--days', '1', '--since', '2026-11-01')),
                           (('--repo', 'a/b'), ('--since', '2026-11-02', '--until', '2026-11-01')),
                           (('--repo', 'a/b'), ('--since', '2026-11-02'))):
            self.assertEqual(self.run_main(repo=repo, days=days)[0], 2, (repo, days))
        self.assertEqual(self.calls(), [])


if __name__ == '__main__':
    unittest.main()
