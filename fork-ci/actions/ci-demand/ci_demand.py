#!/usr/bin/env python3
"""How much CI did a repository run, per day: read-only numbers from the GitHub Actions API.

For every UTC day of a date range (a run belongs to the day it was created): CI hours (the sum of every job's
started -> completed time, all attempts), runs per workflow file, queue wait (job created -> started; p50, p90, max)
per runner label set, and the share of completed runs that ended cancelled or failed.

  python3 -I ci_demand.py (--repo OWNER/REPO | --config FILE) (--days N | --since DAY --until DAY)
                          --cache DIR [--max-requests N] [--reserve N] [--json]

A re-run's attempts count on the day the run was created (a re-run of a run created before the range is not
counted), and queue wait includes time a job waits for an environment approval, not only for a runner.

--days N is the N full UTC days before today. --config reads `ciDemand.repo` from a JSON file of the project.
It only ever sends GET requests (`gh api -X GET`): it never writes to GitHub. Rate limit: it stops before
X-RateLimit-Remaining falls below --reserve (default 20% of X-RateLimit-Limit, so a workflow token's 1000
an hour works too) and after --max-requests requests; on 403/429 it waits Retry-After or
the reset time when that is short (MAX_WAIT), else it stops. A stop prints what it has, marked INCOMPLETE (exit 3):
run it again with the same --cache and it goes on from the pages it already saved. A cache that could not be
written is in the result (`cache_write_failed`), and then a stop does not say to run again: the pages
that failed were not saved.

Cache (--cache, a folder the caller owns): a page is saved only when it cannot change any more: a job page when the
run and all its jobs are completed (keyed by run id and attempt, so a re-run is a new page); a run-list page when
every run in it is completed and its window ended over an hour ago, and then reused for LIST_MAX_AGE (a later re-run
changes the run). Where the OS has user ids (not Windows), a folder another user owns is refused and the folder is
made 0700 (a filesystem where chmod has no effect is refused). Writes are a `.part-` file then a rename. A file
that is unreadable, half-written, damaged (also nested too deep, a page field missing or of the wrong type, or a run
or job in it not completed or without a field the counting reads), saved for another page or in another FORMAT is
fetched again; a failed write only costs the cache; `.part-` files older than an hour go (one that cannot be removed
is reported, also as `cache_cleanup_failed` in --json, and the run goes on).
Exit: 0 complete, 2 bad arguments (a config file of the wrong shape too) or an API error (an answer with a field
missing or of the wrong type too), 3 incomplete (budget).
"""
import argparse, contextlib, datetime as dt, hashlib, json, math, os, re, subprocess, sys, time

MAX_WAIT = 120          # seconds: a longer rate-limit wait stops the run (INCOMPLETE) instead
LIST_MAX_AGE = 12 * 3600
PART_AGE = 3600
PAGE = 100
SEARCH_CAP = 1000       # the runs list returns at most this many runs for one `created` query
RETRIES = 3
FORMAT = 1              # cache file format: a file of another format (older or newer tool) is fetched again
RESERVE_PCT = 20        # default --reserve: this share of X-RateLimit-Limit (1000 when the limit is not given)
sleep = time.sleep      # tests replace these two
now = time.time


class Budget(Exception):
    """The request cap or the rate-limit reserve is reached: stop and print what is there."""


class ApiError(Exception):
    pass


def shown(text):
    """One printable line (names come from the API)."""
    return ''.join(c if c.isprintable() else ascii(c)[1:-1] for c in str(text))


def parse_include(out):
    """(status, headers, body) of `gh api --include` stdout; None when it is not an HTTP answer."""
    head, sep, body = out.partition('\r\n\r\n') if '\r\n\r\n' in out else out.partition('\n\n')
    lines = head.splitlines()
    m = re.match(r'HTTP/\S+ (\d{3})', lines[0]) if lines else None
    if not sep or not m:
        return None
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(':')
        headers[k.strip().lower()] = v.strip()
    return int(m.group(1)), headers, body


class Api:
    """GET-only `gh api` with a request cap, a rate-limit reserve, back-off and a page cache."""

    def __init__(self, cache, max_requests, reserve, gh='gh'):
        self.cache, self.max_requests, self.reserve, self.gh = cache, max_requests, reserve, gh
        self.requests = self.hits = 0
        self.stop = self.save_failed = self.cleanup_failed = None
        os.makedirs(cache, mode=0o700, exist_ok=True)   # pages of a private repository: kept private
        getuid = getattr(os, 'getuid', None)   # None on Windows: no owner or mode bits to check there
        if getuid is not None:
            if os.stat(cache).st_uid != getuid():
                raise ValueError(f'--cache {cache} belongs to another user')
            if os.stat(cache).st_mode & 0o077:   # made earlier by someone with a wider umask
                os.chmod(cache, 0o700)
                if os.stat(cache).st_mode & 0o077:
                    raise ValueError(f'--cache {cache} cannot be made private (0700)')
        for name in os.listdir(cache):   # a killed write leaves a .part- file; it is never read
            p = os.path.join(cache, name)
            try:
                if name.startswith('.part-') and now() - os.path.getmtime(p) > PART_AGE:
                    os.unlink(p)
            except FileNotFoundError:
                pass   # another run removed it first
            except OSError as e:   # a read-only folder or a directory: only the clean-up failed (a save says its own)
                self.cleanup_failed = self.cleanup_failed or str(e)
        if self.cleanup_failed:   # also in --json (cache_cleanup_failed)
            print(f'ci-demand: old .part- file not removed ({shown(self.cleanup_failed)}); going on', file=sys.stderr)

    def _path(self, key):
        return os.path.join(self.cache, hashlib.sha256(key.encode()).hexdigest()[:40] + '.json')

    def cached(self, key, kind, max_age=None):
        try:
            with open(self._path(key), encoding='utf-8') as f:
                doc = json.load(f)
        except (OSError, ValueError, RecursionError):   # RecursionError: nested too deep
            return None
        if not (isinstance(doc, dict) and doc.get('v') == FORMAT and doc.get('key') == key
                and page_ok(doc.get('body'), kind) and saved_ok(doc['body'], kind)):   # damaged: fetched again
            return None
        saved = doc.get('saved')
        try:   # float(): json.load reads NaN, Infinity and any-size integers; a huge one raises OverflowError
            date_ok = not isinstance(saved, bool) and isinstance(saved, (int, float)) and math.isfinite(float(saved)) \
                and 0 <= saved <= now() + 60
        except OverflowError:
            date_ok = False
        if not date_ok:
            return None   # a damaged date (NaN, infinite, huge, before 1970 or in the future): fetched again
        if max_age is not None and now() - saved > max_age:
            return None
        self.hits += 1
        return doc['body']

    def save(self, key, body):
        """Atomic: a `.part-` file, then a rename. A failed write is reported once and only costs the cache."""
        part = os.path.join(self.cache, f'.part-{os.getpid()}-{hashlib.sha256(key.encode()).hexdigest()[:12]}')
        try:
            with open(part, 'w', encoding='utf-8') as f:
                json.dump({'v': FORMAT, 'key': key, 'saved': now(), 'body': body}, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(part, self._path(key))
        except OSError as e:
            with contextlib.suppress(OSError):
                os.unlink(part)
            self.write_failed(e)

    def write_failed(self, e):
        """Reported once; the run goes on without saving."""
        if not self.save_failed:
            self.save_failed = str(e)
            print(f'ci-demand: cache not written ({shown(e)}); going on without it', file=sys.stderr)

    def get(self, endpoint, kind):
        """The decoded JSON of one GET (a `kind` page, see page_ok); Budget when the cap or reserve would be crossed."""
        for attempt in range(RETRIES + 1):
            if self.requests >= self.max_requests:
                raise Budget(f'--max-requests {self.max_requests} reached')
            self.requests += 1
            r = subprocess.run([self.gh, 'api', '-X', 'GET', '--include', endpoint], capture_output=True, text=True,
                               encoding='utf-8', errors='replace')   # not the locale's code page (Windows)
            got = parse_include(r.stdout)
            if got is None:   # network or gh trouble: no HTTP answer
                if attempt == RETRIES:
                    raise ApiError(f'gh api {endpoint}: {shown(r.stderr.strip()[:300])}')
                sleep(2 ** (attempt + 1))
                continue
            status, h, body = got
            remaining = int(h['x-ratelimit-remaining']) if h.get('x-ratelimit-remaining', '').isdigit() else None
            if status == 200:
                reserve = self.reserve_for(h)
                if remaining is not None and remaining < reserve:
                    self.stop = f'rate limit: {remaining} left, reserve {reserve}'
                try:
                    doc = json.loads(body)
                except (ValueError, RecursionError):
                    raise ApiError(f'{endpoint}: not JSON')
                if not page_ok(doc, kind):
                    raise ApiError(f'{endpoint}: unexpected answer (a field missing or of the wrong type)')
                return doc
            secondary = status in (403, 429) and 'rate limit' in body.lower()
            if status in (403, 429) and (h.get('retry-after') or remaining == 0 or secondary):
                if h.get('retry-after', '').isdigit():
                    wait = int(h['retry-after'])
                elif remaining == 0 and h.get('x-ratelimit-reset', '').isdigit():
                    wait = int(h['x-ratelimit-reset']) - int(now()) + 1
                else:
                    wait = 60   # GitHub's advice for a secondary limit without Retry-After
                if wait > MAX_WAIT or attempt == RETRIES:
                    raise Budget(f'rate limited ({status}), wait {wait}s')
                sleep(max(wait, 1))
                continue
            if status >= 500 and attempt < RETRIES:
                sleep(2 ** (attempt + 1))
                continue
            raise ApiError(f'{endpoint}: HTTP {status} {shown(body[:200])}')
        raise ApiError(f'{endpoint}: gave up')   # not reached

    def reserve_for(self, headers):
        """--reserve, or by default RESERVE_PCT of the answer's limit (a workflow token has only 1000 an hour)."""
        if self.reserve is not None:
            return self.reserve
        limit = headers.get('x-ratelimit-limit', '')
        return int(limit) * RESERVE_PCT // 100 if limit.isdigit() else 1000

    def check(self):
        """Budget once the last answer said the reserve is reached (the answer itself was still used)."""
        if self.stop:
            raise Budget(self.stop)


def iso(t):
    return t.strftime('%Y-%m-%dT%H:%M:%SZ')


def ts(s):
    if not s:
        return None
    t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def is_count(n):
    return isinstance(n, int) and not isinstance(n, bool) and n >= 0


def is_date(s, needed=False):
    """A date as GitHub writes it (2026-11-01T12:00:00Z; not the other ISO 8601 forms fromisoformat reads, as the day
    is its first 10 characters); or, when not needed, empty (ts() gives None)."""
    if not s:
        return not needed
    if not (isinstance(s, str) and re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z', s)):
        return False
    try:
        return ts(s) is not None
    except ValueError:   # a month 13 or a day 32
        return False


def run_ok(r):
    return isinstance(r, dict) and is_count(r.get('id')) and is_date(r.get('created_at'), needed=True) \
        and isinstance(r.get('path') or '', str)


def job_ok(j):
    if not isinstance(j, dict):
        return False
    labels = j.get('labels') or []
    return isinstance(labels, list) and all(isinstance(x, str) for x in labels) \
        and all(is_date(j.get(k)) for k in ('created_at', 'started_at', 'completed_at'))


def page_ok(body, kind):
    """A runs-list (`kind` 'workflow_runs') or jobs ('jobs') page that has the fields the counting uses, of their
    types (a saved file can hold anything; a missing field would count as no runs or no jobs)."""
    if not (isinstance(body, dict) and is_count(body.get('total_count')) and isinstance(body.get(kind), list)):
        return False
    return all(map(run_ok if kind == 'workflow_runs' else job_ok, body[kind]))


SAVED_FIELDS = {'workflow_runs': ('status', 'conclusion'),
                'jobs': ('status', 'conclusion', 'runner_name', 'started_at', 'completed_at')}


def saved_ok(body, kind):
    """A saved page (page_ok already) still meets the rule it was saved by: every run or job in it completed, with
    every field the counting reads (the API always sends them, null where empty). A job that reached a runner needs
    its start and end, else it would count as 0 seconds."""
    for x in body[kind]:
        if x.get('status') != 'completed' or not all(k in x for k in SAVED_FIELDS[kind]):
            return False
        if kind == 'jobs' and x['conclusion'] != 'skipped' and x['runner_name'] \
                and not (is_date(x['started_at'], needed=True) and is_date(x['completed_at'], needed=True)):
            return False
    return True


def list_runs(api, repo, start, end, out):
    """Every run created in [start, end] into `out` (by id). A window over the API's 1000-run cap is split in two."""
    base = f'repos/{repo}/actions/runs?created={iso(start)}..{iso(end)}&per_page={PAGE}'
    settled = end < dt.datetime.fromtimestamp(now(), dt.timezone.utc) - dt.timedelta(hours=1)
    page, seen = 1, 0
    while True:
        ep = f'{base}&page={page}'
        body = api.cached(ep, 'workflow_runs', LIST_MAX_AGE) if settled else None
        if body is None:
            body = api.get(ep, 'workflow_runs')
            if settled and all(r.get('status') == 'completed' for r in body.get('workflow_runs', [])):
                api.save(ep, body)
        total = body.get('total_count', 0)
        if total > SEARCH_CAP and end - start > dt.timedelta(seconds=1):
            mid = start + (end - start) / 2
            mid = mid.replace(microsecond=0)
            list_runs(api, repo, start, mid, out)
            list_runs(api, repo, mid + dt.timedelta(seconds=1), end, out)
            return
        runs = body.get('workflow_runs', [])
        for r in runs:
            out[r['id']] = r
        seen += len(runs)
        api.check()
        if not runs or seen >= total:
            return
        page += 1


def run_jobs(api, repo, run):
    """Every job of every attempt of one run."""
    jobs, page = [], 1
    while True:
        ep = f'repos/{repo}/actions/runs/{run["id"]}/jobs?filter=all&per_page={PAGE}&page={page}'
        key = f'{ep}#attempt={run.get("run_attempt", 1)}'
        body = api.cached(key, 'jobs')
        if body is None:
            body = api.get(ep, 'jobs')
            if run.get('status') == 'completed' and all(j.get('status') == 'completed' for j in body.get('jobs', [])):
                api.save(key, body)
        jobs += body.get('jobs', [])
        api.check()
        if not body.get('jobs') or len(jobs) >= body.get('total_count', 0):
            return jobs
        page += 1


def pct(values, p):
    """Nearest-rank percentile of a non-empty list."""
    s = sorted(values)
    return s[max(0, -(-len(s) * p // 100) - 1)]


def label_key(job):
    return ','.join(job.get('labels') or []) or '(none)'


def summarize(runs, jobs_by_run):
    """{day: {...}} from runs and the jobs of the runs that have them (a run without jobs is counted as missing)."""
    days = {}
    for r in runs.values():
        day = r['created_at'][:10]
        d = days.setdefault(day, {'runs': 0, 'workflows': {}, 'completed': 0, 'cancelled': 0, 'failed': 0,
                                  'seconds': 0, 'waits': {}, 'never_started': 0, 'still_queued': 0,
                                  'jobs_missing': 0})
        d['runs'] += 1
        wf = os.path.basename(r.get('path') or '') or shown(r.get('name', '?'))
        d['workflows'][wf] = d['workflows'].get(wf, 0) + 1
        if r.get('status') == 'completed':
            d['completed'] += 1
            d['cancelled'] += r.get('conclusion') == 'cancelled'
            d['failed'] += r.get('conclusion') in ('failure', 'timed_out', 'startup_failure')
        jobs = jobs_by_run.get(r['id'])
        if jobs is None:
            d['jobs_missing'] += r.get('conclusion') != 'skipped'
            continue
        for j in jobs:
            if j.get('conclusion') == 'skipped':
                continue
            created, started, completed = ts(j.get('created_at')), ts(j.get('started_at')), ts(j.get('completed_at'))
            if not j.get('runner_name'):   # never reached a runner: cancelled while queued, or queued now
                if j.get('status') == 'completed':
                    d['never_started'] += 1
                else:
                    d['still_queued'] += 1
                continue
            if created and started:
                d['waits'].setdefault(label_key(j), []).append(max(0, (started - created).total_seconds()))
            if started and completed:
                d['seconds'] += max(0, (completed - started).total_seconds())
    return days


def runs_needing_jobs(runs):
    """Skipped runs ran no job: no request for them."""
    return [r for r in sorted(runs.values(), key=lambda r: r['id']) if r.get('conclusion') != 'skipped']


def wait_stats(values):
    return {'n': len(values), 'p50': pct(values, 50), 'p90': pct(values, 90), 'max': max(values)}


def report(days):
    """JSON-ready numbers per day, plus 'total'."""
    out, total = {}, None
    for day in sorted(days):
        d = days[day]
        out[day] = row(d)
        if total is None:
            total = {k: (dict(v) if isinstance(v, dict) else v) for k, v in d.items()}
            total['waits'] = {k: list(v) for k, v in d['waits'].items()}
        else:
            for k, v in d.items():
                if k == 'workflows':
                    for w, n in v.items():
                        total[k][w] = total[k].get(w, 0) + n
                elif k == 'waits':
                    for lab, vals in v.items():
                        total[k].setdefault(lab, []).extend(vals)
                else:
                    total[k] += v
    if total is not None:
        out['total'] = row(total)
    return out


def row(d):
    share = lambda n: round(100 * n / d['completed'], 1) if d['completed'] else None
    return {'ci_hours': round(d['seconds'] / 3600, 2), 'runs': d['runs'], 'workflows': dict(sorted(d['workflows'].items())),
            'cancelled_pct': share(d['cancelled']), 'failed_pct': share(d['failed']),
            'queue_wait_s': {lab: wait_stats(v) for lab, v in sorted(d['waits'].items())},
            'never_started_jobs': d['never_started'], 'still_queued_jobs': d['still_queued'],
            'runs_without_jobs': d['jobs_missing']}


def fmt_s(s):
    s = int(s)
    return f'{s // 3600}h{s % 3600 // 60:02d}m' if s >= 3600 else f'{s // 60}m{s % 60:02d}s'


def text(out):
    lines = []
    for day, r in out.items():
        lines.append(f'{day}: {r["ci_hours"]} CI hours, {r["runs"]} runs, cancelled {r["cancelled_pct"]}%, '
                     f'failed {r["failed_pct"]}%' + (f', {r["runs_without_jobs"]} runs without jobs (INCOMPLETE)'
                                                     if r['runs_without_jobs'] else ''))
        lines.append('  runs: ' + ', '.join(f'{shown(w)} {n}' for w, n in r['workflows'].items()))
        for lab, w in r['queue_wait_s'].items():
            lines.append(f'  wait {shown(lab)}: n {w["n"]}, p50 {fmt_s(w["p50"])}, p90 {fmt_s(w["p90"])}, '
                         f'max {fmt_s(w["max"])}')
        if r['never_started_jobs'] or r['still_queued_jobs']:
            lines.append(f'  jobs that never reached a runner: {r["never_started_jobs"]} (cancelled in queue), '
                         f'{r["still_queued_jobs"]} still queued')
    return '\n'.join(lines)


def day_range(args, today):
    if args.days is not None:
        if args.since or args.until:
            raise ValueError('--days or --since/--until, not both')
        if args.days < 1:
            raise ValueError('--days must be at least 1')
        return today - dt.timedelta(days=args.days), today - dt.timedelta(days=1)
    if not (args.since and args.until):
        raise ValueError('give --days N, or --since DAY and --until DAY')
    since, until = (dt.date.fromisoformat(x) for x in (args.since, args.until))
    if until < since:
        raise ValueError('--until is before --since')
    return since, until


def repo_of(args):
    if bool(args.repo) == bool(args.config):
        raise ValueError('give --repo or --config, one of them')
    repo = args.repo
    if args.config:
        with open(args.config, encoding='utf-8-sig') as f:   # utf-8-sig: a BOM (some Windows editors) is fine
            try:
                doc = json.load(f)
            except (ValueError, RecursionError) as e:
                raise ValueError(f'{args.config}: not JSON ({e})')
        section = doc.get('ciDemand') if isinstance(doc, dict) else None
        repo = section.get('repo') if isinstance(section, dict) else None
        if not repo or not isinstance(repo, str):
            raise ValueError(f'{args.config}: no ciDemand.repo (a text "OWNER/REPO" in a "ciDemand" object)')
    if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo):
        raise ValueError(f'not OWNER/REPO: {shown(repo)}')
    return repo


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    ap.add_argument('--repo')
    ap.add_argument('--config')
    ap.add_argument('--days', type=int)
    ap.add_argument('--since')
    ap.add_argument('--until')
    ap.add_argument('--cache', required=True)
    ap.add_argument('--max-requests', type=int, default=2000)
    ap.add_argument('--reserve', type=int, help=f'leave this much of the rate limit to others '
                    f'(default {RESERVE_PCT}%% of X-RateLimit-Limit)')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    try:
        repo = repo_of(args)
        since, until = day_range(args, dt.datetime.fromtimestamp(now(), dt.timezone.utc).date())
    except (ValueError, OSError) as e:
        print(f'ci-demand: {shown(e)}', file=sys.stderr)
        return 2
    try:
        api = Api(args.cache, args.max_requests, args.reserve)
    except (ValueError, OSError) as e:
        print(f'ci-demand: {shown(e)}', file=sys.stderr)
        return 2
    runs, jobs, stopped, listed = {}, {}, None, False
    try:
        day = since
        while day <= until:   # one query per day keeps each window small (the 1000-run cap) and its pages cacheable
            start = dt.datetime.combine(day, dt.time(), dt.timezone.utc)
            list_runs(api, repo, start, start + dt.timedelta(seconds=86399), runs)
            day += dt.timedelta(days=1)
        listed = True
        for r in runs_needing_jobs(runs):
            jobs[r['id']] = run_jobs(api, repo, r)
    except Budget as e:
        stopped = str(e)
    except ApiError as e:
        print(f'ci-demand: {e}', file=sys.stderr)
        return 2
    out = report(summarize(runs, jobs))
    missing = sum(r['runs_without_jobs'] for k, r in out.items() if k != 'total')
    head = f'{repo} {since}..{until} (UTC days, by run creation): {api.requests} requests, {api.hits} cached pages'
    if stopped:
        head += (f'; INCOMPLETE: {stopped}, ' + ('' if listed else 'run list not finished, ') + f'{missing} runs without jobs'
                 + ('' if api.save_failed else '; run again with the same --cache'))
    if api.save_failed:   # the pages that failed were not saved: a run again would fetch them again
        head += f'; cache not written ({shown(api.save_failed)})' + (
            ', so a run again asks for the same pages: fix --cache first' if stopped else '')
    if args.json:
        print(json.dumps({'repo': repo, 'since': str(since), 'until': str(until), 'requests': api.requests,
                          'cached': api.hits, 'incomplete': stopped, 'run_list_complete': listed,
                          'cache_write_failed': api.save_failed, 'cache_cleanup_failed': api.cleanup_failed,
                          'days': out}, indent=1))
    else:
        print(head)
        print(text(out))
    return 3 if stopped else 0


if __name__ == '__main__':
    for stream in (sys.stdout, sys.stderr):   # a Windows console's code page cannot show every run name
        if hasattr(stream, 'reconfigure'):
            stream.reconfigure(errors='replace')
    sys.exit(main())
