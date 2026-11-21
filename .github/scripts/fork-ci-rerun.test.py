#!/usr/bin/env python3
"""Tests of fork-ci-rerun.py (the one automatic re-run of an infrastructure failure), with a fake GitHub that pages
like the real one (the script's own GitHub.pages reads it), and of its wiring in fork-ci-rerun.yml. Run: python3 <this>."""
import base64, copy, io, json, os, re, shutil, subprocess, sys, tempfile, time, unittest, urllib.error, urllib.parse
import importlib.util

sys.dont_write_bytecode = True   # importing the script must not leave __pycache__ in the tree

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
SCRIPT = os.path.join(HERE, 'fork-ci-rerun.py')
_spec = importlib.util.spec_from_file_location('fork_ci_rerun', SCRIPT)
cr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cr)
_spec = importlib.util.spec_from_file_location('fork_ci_pressure', os.path.join(HERE, 'fork-ci-pressure.py'))
probe = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(probe)

CFG = cr.load()
FLEET_CFG = CFG['fleet']
REPO = FLEET_CFG['repository']
OWNER = FLEET_CFG['trusted']['actors'][0]
BRANCH = FLEET_CFG['trusted']['push_branches'][0]
FOLLOWER = CFG['followers'][0]
END_STEP = CFG['probe_end_step']
SKIP_VAR, SKIP_VALUE = CFG['skip_when']['variable'], CFG['skip_when']['is'][0]
TITLE = CFG['notice']['title']
BOT = CFG['notice']['author']
ACTIONS = 'github-actions'   # the triggering actor of GitHub's re-run from a workflow token
HOSTED = CFG['hosted_runner_groups'][0]
T0 = 1_800_000_000
SHA = '0123456789abcdef0123456789abcdef01234567'
NEWER = 'fedcba9876543210fedcba9876543210fedcba98'

# The fleet's trust rule asks git whether the upstream sync made a pushed commit; here, a set.
SYNCED = set()
cr.FLEET.made_by_the_sync = lambda cfg, sha, run=None: (f'commit {sha[:12]} was pushed by the upstream sync'
                                                        if sha in SYNCED else None)

# GitHub's own messages, word for word as its annotations carry them.
LOST = ('The self-hosted runner: fleet-vm-1 lost communication with the server. Verify the machine is running and '
        'has a healthy network connection. Anything in your workflow that terminates the runner process, starves it '
        'for CPU/Memory, or blocks its network access can cause this error.')
HOSTED_LOST = 'The hosted runner: GitHub Actions 7 lost communication with the server. Anything in your workflow...'
SHUTDOWN = 'The runner has received a shutdown signal. This can happen when the runner service is stopped.'
JOB_LIMIT = 'The job running on runner fleet-vm-1 has exceeded the maximum execution time of 15 minutes.'
STEP_LIMIT = "The action 'Test' has timed out after 10 minutes."
CANCEL_BY = f'The run was canceled by @{OWNER}.'
CANCELED = 'The operation was canceled.'
EXIT_1 = 'Process completed with exit code 1.'
EXIT_137 = 'Process completed with exit code 137.'
# The workflow file at the run's commit, as the contents API hands it back: the limits GitHub's messages must match.
WORKFLOW = f"""name: Fork tests
on: push
jobs:
  test:
    name: Test
    strategy:
      matrix:
        shard: [1, 2]
    timeout-minutes: 15
    steps:
      - name: Checkout
      - name: Note memory pressure at the start
        timeout-minutes: 1
        run: |
          python3 x.py
      - name: Test
        timeout-minutes: 10   # the step's own
      - name: '{END_STEP}'
        timeout-minutes: 1
  lint:
    name: Lint
    timeout-minutes: 20
    steps:
      - run: true
"""
MEMORY_WORDS = 'OOM kill: memory.max hit, memory full, the box stalled (full) 90% of the job'


def iso(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def ann(message, level='failure', title='', path='.github'):
    """An annotation as GitHub hands it back. A step's ::error::/::warning:: has the same fields as the runner's own."""
    return {'message': message, 'annotation_level': level, 'title': title, 'path': path}


FIT = 'fit'   # a note's seconds: the fake GitHub puts in the time between the job's own probe steps


def note(host=None, cgroups=None, status='failure', seconds=FIT):
    """A pressure note as fork-ci-pressure.py writes it, decoded as GitHub hands it back."""
    h = {'full_share': 0.0, 'some_share': 0.5, 'full_avg10': 0.0, 'full_avg60': 0.0, 'full_avg300': 0.0,
         'some_avg60': 0.1, 'some_avg300': 0.1, 'available_mb': 9000, 'swap_used_mb': 0, 'oom_kill': 0}
    h.update(host or {})
    boxes = cgroups if cgroups is not None else [box()]
    body = json.dumps({'status': status, 'seconds': seconds, 'host': h, 'cgroups': boxes}, separators=(',', ':'))
    return ann(cr.NOTE_HEAD + body, level='notice', title=cr.NOTE_TITLE)


def fitted(annotations, j):
    """The annotations with a FIT note's seconds set to the middle of the job's probe window (600 without one)."""
    out = []
    for a in annotations:
        if a.get('title') == cr.NOTE_TITLE and a['message'].startswith(cr.NOTE_HEAD):
            try:
                body = json.loads(a['message'][len(cr.NOTE_HEAD):])
            except ValueError:
                body = None   # a garbled note stays as it is
            if isinstance(body, dict) and body.get('seconds') == FIT:
                window = cr.probe_window(CFG, j) if j else None
                body['seconds'] = int(sum(window) / 2) if window else 600
                a = dict(a, message=cr.NOTE_HEAD + json.dumps(body, separators=(',', ':')))
        out.append(a)
    return out


def tracking(number, state='open', user=BOT, body=None, **kw):
    """A tracking issue as GitHub lists it: the bot's, its body starting with the script's marker."""
    return dict({'number': number, 'title': TITLE, 'state': state, 'user': {'login': user}, 'comments': [],
                 'body': cr.ISSUE_MARK + '\nEach comment below ...' if body is None else body}, **kw)


def box(**kw):
    b = {'path': '/fleet.slice/job-1.service', 'high_mb': 6144, 'max_mb': 8192, 'current_mb': 3000,
         'high': 0, 'max': 0, 'oom': 0, 'oom_kill': 0, 'full_share': 0.0}
    b.update(kw)
    return b


CALM = note()
FULL = note(cgroups=[box(oom_kill=1, full_share=30.0)])     # the box stalled and an OOM kill in it
STALLED = note(host={'full_share': 25.0})


def step(name, conclusion='success', status='completed', took=5, at=0):
    return {'name': name, 'status': status, 'conclusion': conclusion,
            'started_at': iso(T0 - 3600 + at), 'completed_at': iso(T0 - 3600 + at + took) if status == 'completed'
            else None}


def ran(test='failure', took=60, end=True):
    """A job that reached its end step (the note is its own)."""
    return [step('Checkout'), step('Note memory pressure at the start', at=5), step('Test', test, took=took, at=10),
            step(END_STEP, at=10 + took) if end else step(END_STEP, None, 'queued')]


def died():
    """A job whose runner went away mid-test: the test step never ended, the end step never ran."""
    return [step('Checkout'), step('Note memory pressure at the start', at=5),
            step('Test', None, 'in_progress', at=10), step(END_STEP, None, 'queued')]


def run(rid=1, attempt=1, conclusion='failure', status='completed', event='push', branch=BRANCH, actor=OWNER,
        by=OWNER, repo=REPO, head_repo=REPO, updated=T0 - 120, created=T0 - 3600, workflow='fork-tests.yml', pr=7):
    r = {'id': rid, 'run_attempt': attempt, 'status': status, 'conclusion': conclusion, 'event': event,
         'head_branch': branch, 'head_sha': SHA, 'path': f'.github/workflows/{workflow}',
         'html_url': f'https://github.example/{repo}/actions/runs/{rid}',
         'repository': {'full_name': repo}, 'head_repository': {'full_name': head_repo},
         'actor': {'login': actor}, 'triggering_actor': {'login': by}, 'updated_at': iso(updated),
         'created_at': iso(created), 'pull_requests': []}
    if event == 'pull_request' and head_repo == repo:
        r['pull_requests'] = [{'number': pr}]   # GitHub lists none for a pull request from another fork
    return r


def job(jid, name='Test (1)', conclusion='failure', steps=None, group='fleet', runner='fleet-vm-1', took=600):
    return {'id': jid, 'name': name, 'conclusion': conclusion, 'status': 'completed',
            'steps': steps if steps is not None else ran(), 'runner_group_name': group, 'runner_name': runner,
            'started_at': iso(T0 - 3600), 'completed_at': iso(T0 - 3600 + took),
            'check_run_url': f'https://api.example/repos/{REPO}/check-runs/{jid}'}


class Fake:
    """GitHub's REST API as fork-ci-rerun.py uses it. Listings page like GitHub's (page/per_page), read by the script's
    own GitHub.pages; a re-run makes a new attempt triggered by github-actions; issues and comments are kept."""

    pages = cr.GitHub.pages   # the real paging code, over this fake's call()

    def __init__(self, runs, jobs=None, annotations=None, heads=None, pulls=None):
        self.runs = {r['id']: r for r in runs}
        self.jobs = jobs or {}
        self.ann = annotations or {}
        self.heads = heads if heads is not None else {BRANCH: SHA, 'feature': SHA}
        self.pulls = pulls if pulls is not None else {7: {'head': {'repo': {'full_name': REPO}},
                                                          'base': {'repo': {'full_name': REPO}},
                                                          'user': {'login': OWNER}}}
        self.issues = []
        self.attempts = {}
        self.calls = []
        self.now = T0
        self.post_fails = None      # an exception to raise for the next re-run POST
        self.lost_reply = False     # the re-run POST reaches GitHub, its reply never comes back
        self.comment_fails = False  # a comment POST fails
        self.on_read = None         # called with the run before GET actions/runs/N answers
        self.workflow = WORKFLOW    # the workflow file at every commit (None: not found)
        self.comments_fail = False  # reading an issue's comments fails

    def call(self, path, method='GET', data=None):
        self.calls.append((method, path))
        base, _, query = path.partition('?')
        q = urllib.parse.parse_qs(query)

        def paged(items):
            if 'page' not in q:
                return copy.deepcopy(items)
            n, per = int(q['page'][0]), int(q['per_page'][0])
            return copy.deepcopy(items[(n - 1) * per:n * per])

        if method == 'POST' and base == 'issues':
            number = max([i['number'] for i in self.issues] + [40]) + 1
            self.issues.append({'number': number, 'title': data['title'], 'labels': data['labels'],
                                'body': data['body'], 'state': 'open', 'user': {'login': BOT}, 'comments': []})
            return {'number': number}
        m = re.fullmatch(r'issues/(\d+)/comments', base)
        if m and method == 'POST':
            if self.comment_fails:
                raise urllib.error.URLError('comment: reset')
            issue = self.issue(int(m[1]))
            c = {'id': 9000 + sum(len(i['comments']) for i in self.issues), 'body': data['body'],
                 'user': {'login': BOT}, 'created_at': iso(self.now)}
            issue['comments'].append(c)
            return copy.deepcopy(c)
        m = re.fullmatch(r'issues/(\d+)', base)
        if m and method == 'PATCH':
            self.issue(int(m[1])).update(data)
            return {}
        if method == 'POST':
            m = re.fullmatch(r'actions/runs/(\d+)/rerun-failed-jobs', path)
            if not m:
                raise AssertionError(f'unexpected POST {path}')
            r = self.runs[int(m[1])]
            if self.post_fails:
                e, self.post_fails = self.post_fails, None
                raise e() if callable(e) and not isinstance(e, BaseException) else e
            if r['status'] != 'completed':
                raise urllib.error.HTTPError(path, 403, 'This workflow is already running', {}, None)
            self.attempts[(r['id'], r['run_attempt'])] = copy.deepcopy(r)
            r.update(run_attempt=r['run_attempt'] + 1, status='queued', conclusion=None,
                     triggering_actor={'login': ACTIONS})
            if self.lost_reply:
                raise urllib.error.URLError('connection reset')
            return {}
        if method != 'GET':
            raise AssertionError(f'unexpected {method} {path}')
        if base == f'contents/.github/workflows/{CFG["workflow"]}':
            assert q['ref'] == [SHA], q
            if self.workflow is None:
                raise urllib.error.HTTPError(path, 404, 'Not Found', {}, None)
            return {'encoding': 'base64', 'content': base64.b64encode(self.workflow.encode()).decode()}
        if base == f'actions/workflows/{CFG["workflow"]}/runs':
            since = cr.stamp(urllib.parse.unquote(q['created'][0]).lstrip('>='))
            listed = sorted((r for r in self.runs.values() if cr.stamp(r['created_at']) >= since),
                            key=lambda r: -r['id'])   # newest first, as GitHub lists them
            return {'total_count': len(listed), 'workflow_runs': paged(listed)}
        m = re.fullmatch(r'actions/runs/(\d+)/jobs', base)
        if m:
            return {'jobs': paged(self.jobs.get(int(m[1]), []))}
        m = re.fullmatch(r'actions/runs/(\d+)', base)
        if m:
            if self.on_read:
                self.on_read(self.runs[int(m[1])])
            return copy.deepcopy(self.runs[int(m[1])])
        m = re.fullmatch(r'actions/runs/(\d+)/attempts/(\d+)', base)
        if m:
            got = self.attempts.get((int(m[1]), int(m[2])))
            if got is None:
                got = dict(self.runs[int(m[1])], run_attempt=int(m[2]), triggering_actor={'login': OWNER})
            return copy.deepcopy(got)
        m = re.fullmatch(r'check-runs/(\d+)/annotations', base)
        if m:
            j = next((j for js in self.jobs.values() for j in js if j['id'] == int(m[1])), None)
            return paged(fitted(self.ann.get(int(m[1]), []), j))
        m = re.fullmatch(r'branches/(.+)', base)
        if m:
            if m[1] not in self.heads:
                raise urllib.error.HTTPError(path, 404, 'Branch not found', {}, None)
            return {'commit': {'sha': self.heads[m[1]]}}
        m = re.fullmatch(r'pulls/(\d+)', base)
        if m:
            return copy.deepcopy(self.pulls[int(m[1])])
        if base == 'issues':
            assert q['state'] == ['open'] and q['creator'] == [BOT], q
            # The script checks the author itself: here the creator filter is not applied.
            listed = [{k: v for k, v in i.items() if k != 'comments'} for i in self.issues if i['state'] == 'open']
            return paged(listed)
        m = re.fullmatch(r'issues/(\d+)/comments', base)
        if m:
            if self.comments_fail:
                raise urllib.error.URLError('comments: reset')
            since = cr.stamp(q['since'][0])
            # GitHub's `since` is by updated_at: an old comment edited lately is listed too.
            return paged([c for c in self.issue(int(m[1]))['comments']
                          if cr.stamp(c.get('updated_at') or c['created_at']) >= since])
        raise AssertionError(f'unexpected GET {path}')

    def issue(self, number):
        return next(i for i in self.issues if i['number'] == number)

    def posts(self):
        return [p for m, p in self.calls if m == 'POST' and p.startswith('actions/')]

    def comments(self):
        return [c['body'] for i in self.issues for c in i['comments']]


def sweep(gh, env=None, run_id=None, cfg=CFG):
    out = io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, 'summary.md')
        code = cr.sweep(cfg, dict(env or {}, GITHUB_STEP_SUMMARY=path), gh, now=lambda: gh.now, run_id=run_id,
                        out=out)
        text = ''
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                text = f.read()
    return code, out.getvalue(), text


def one(annotations, job_kw=None, **kw):
    """A failed run whose one failed job (id 11) carries these annotations, plus a passed job and the follower."""
    jobs = [job(11, **(job_kw or {})), job(12, 'Lint', 'success'), job(13, FOLLOWER, 'failure', steps=[])]
    return Fake([run(**kw)], {1: jobs}, {11: annotations, 13: [ann(EXIT_1)]})


def lost(**kw):
    return one([ann(LOST), ann(CANCELED)], dict(steps=died()), **kw)


class Rerun(unittest.TestCase):
    def assertReran(self, gh, out):
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)
        self.assertIn('::warning title=fork-ci re-run::run 1', out)

    def assertLeft(self, gh, code, out, why=None):
        self.assertEqual((code, gh.posts()), (0, []), out)
        self.assertIn('left red', out)
        if why:
            self.assertIn(why, out)


class Signs(Rerun):
    def test_lost_runner_is_rerun_once_with_the_message_in_the_log(self):
        for message, runner in ((LOST, 'fleet-vm-1'), (SHUTDOWN, 'fleet-vm-1'), (HOSTED_LOST, 'GitHub Actions 7')):
            with self.subTest(message[:30]):
                gh = one([ann(message), ann(CANCELED)], dict(steps=died(), runner=runner))
                code, out, text = sweep(gh)
                self.assertEqual(code, 0, out)
                self.assertReran(gh, out)
                self.assertIn('lost', out)
                self.assertIn('**re-run**', text)

    def test_a_full_box_is_rerun_with_its_numbers_in_the_log(self):
        cases = {
            'box OOM kills while it stalled': FULL,
            'box memory.max hits while it stalled': note(cgroups=[box(max=3, full_share=12.0)]),
            'memory.high throttling while the box stalled': note(cgroups=[box(high=40, full_share=30.0)]),
            'a box with only a high limit': note(cgroups=[box(max_mb=None, oom_kill=2, full_share=15.0)]),
        }
        for label, n in cases.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), n])
                code, out, text = sweep(gh)
                self.assertReran(gh, out)
                self.assertIn('memory full', out)
                self.assertRegex(out, r'machine: full [0-9.]+% some [0-9.]+% of 65 s')
                self.assertIn('box /fleet.slice/job-1.service: max', out)
                self.assertIn('memory full', text)

    def test_one_event_or_a_stall_alone_is_no_memory_sign(self):
        cases = {
            'one OOM kill, the box not stalled': note(cgroups=[box(oom_kill=1, full_share=2.0)]),
            'one memory.max hit, not stalled': note(cgroups=[box(max=1)]),
            'memory.high throttling, not stalled': note(host={'full_share': 2.0}, cgroups=[box(high=40)]),
            'an OOM kill on the machine, not in the box': note(host={'oom_kill': 3, 'full_share': 50.0}),
            'the machine stalled, the box had no event': note(host={'full_share': 50.0},
                                                             cgroups=[box(full_share=50.0)]),
            'a cgroup with no memory limit': note(cgroups=[box(max_mb=None, high_mb=None, oom_kill=4,
                                                               full_share=80.0)]),
            'numbers that are not numbers': note(cgroups=[box(oom_kill=True, max='9', full_share='90')]),
        }
        for label, n in cases.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), n])
                self.assertLeft(gh, *sweep(gh)[:2], 'no infrastructure sign')

    def test_only_the_jobs_own_oom_kill_counts(self):
        """memory.events counts a cgroup's whole subtree: a shared slice's OOM kill can be another job's. The note's
        first cgroup is the job's own (one job a runner at a time): only its OOM kills are the job's, and only its
        own stall says the job waited for memory."""
        own = dict(path='/fleet.slice/job-1.service', high_mb=None, max_mb=None)
        shared = dict(path='/fleet.slice', high_mb=None, max_mb=8192)
        rerun = {
            'its own OOM kill and stall in a stalled slice': [box(**own, oom_kill=1, full_share=30.0),
                                                             box(**shared, oom_kill=1, full_share=30.0)],
            'its own stall while the slice hit memory.max': [box(**own, full_share=30.0),
                                                              box(**shared, max=2, full_share=30.0)],
            'its own stall while the slice was throttled': [box(**own, full_share=30.0),
                                                             box(**dict(shared, high_mb=6144), high=5, full_share=30.0)],
        }
        left = {
            "another job's OOM kill, its own cgroup calm": [box(**own), box(**shared, oom_kill=2, full_share=40.0)],
            "another job's OOM kill while it stalled too": [box(**own, full_share=30.0),
                                                            box(**shared, oom_kill=2, full_share=40.0)],
            'its own OOM kill, its own tasks not stalled': [box(**own, oom_kill=1, full_share=2.0),
                                                            box(**shared, oom_kill=1, full_share=40.0)],
            'its own OOM kill and stall, no box stalled': [box(**own, oom_kill=1, full_share=30.0),
                                                           box(**shared, oom_kill=1, full_share=2.0)],
            'no cgroup of its own (the root)': [box(path='/', oom_kill=1, full_share=40.0)],
            'no cgroup read at all': [],
        }
        for label, boxes in list(rerun.items()) + list(left.items()):
            with self.subTest(label):
                gh = one([ann(EXIT_137), note(cgroups=boxes)])
                code, out, _ = sweep(gh)
                if label in rerun:
                    self.assertReran(gh, out)
                    self.assertIn("the job's own cgroup 30.0%", out)
                else:
                    self.assertLeft(gh, code, out, 'no infrastructure sign')

    def test_each_memory_threshold_is_where_the_config_puts_it(self):
        mf = CFG['memory_full']
        at, under = mf['box_full_share_percent'], mf['box_full_share_percent'] - 0.1
        own = dict(path='/fleet.slice/job-1.service', high_mb=None, max_mb=None)
        shared = dict(path='/fleet.slice', max_mb=8192)
        cases = {
            'the box stalled just enough': ([box(oom_kill=mf['oom_kills'], full_share=at)], True),
            'the box stalled just too little': ([box(oom_kill=mf['oom_kills'], full_share=under)], False),
            'its own cgroup stalled just enough': ([box(**own, oom_kill=1, full_share=at),
                                                    box(**shared, full_share=50.0)], True),
            'its own cgroup stalled just too little': ([box(**own, oom_kill=1, full_share=under),
                                                        box(**shared, full_share=50.0)], False),
            'memory.max hit just enough': ([box(max=mf['max_events'], full_share=50.0)], True),
            'memory.max hit too little': ([box(max=mf['max_events'] - 1, full_share=50.0)], False),
            'memory.high hit just enough': ([box(high=mf['high_events'], full_share=50.0)], True),
            'memory.high hit too little': ([box(high=mf['high_events'] - 1, full_share=50.0)], False),
            'OOM kills just too few': ([box(oom_kill=mf['oom_kills'] - 1, full_share=50.0)], False),
        }
        for label, (boxes, qualifies) in cases.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), note(cgroups=boxes)])
                code, out, _ = sweep(gh)
                (self.assertReran if qualifies else lambda gh, out: self.assertLeft(gh, code, out))(gh, out)

    def test_an_oom_kill_on_a_github_hosted_runner_never_qualifies(self):
        for group in (HOSTED, None, ''):
            with self.subTest(group=group):
                gh = one([ann(EXIT_137), FULL], dict(group=group))
                self.assertLeft(gh, *sweep(gh)[:2])

    def test_a_memory_sign_needs_the_job_and_its_note_to_have_failed(self):
        for kw, status in ((dict(conclusion='cancelled'), 'failure'), ({}, 'cancelled'), ({}, 'success')):
            with self.subTest(kw=kw, status=status):
                n = note(cgroups=[box(oom_kill=1, full_share=30.0)], status=status)
                gh = one([ann(EXIT_137), n], kw)
                self.assertLeft(gh, *sweep(gh)[:2])

    def test_time_limit_while_stalled_is_rerun(self):
        for stall in ({'full_share': 12.0}, {'full_avg300': 15.0}):
            with self.subTest(stall=stall):
                gh = one([ann(JOB_LIMIT), ann(CANCELED), note(host=stall, status='cancelled')],
                         dict(conclusion='cancelled', steps=ran('cancelled', took=880), took=900))
                code, out, _ = sweep(gh)
                self.assertReran(gh, out)
                self.assertIn('limit while stalled', out)
        gh = one([ann(STEP_LIMIT), note(cgroups=[box(full_share=50.0)])], dict(steps=ran('failure', took=600)))
        self.assertReran(gh, sweep(gh)[1])

    def test_a_timed_out_run_is_a_candidate(self):
        gh = one([ann(JOB_LIMIT), STALLED], dict(conclusion='timed_out', steps=ran('cancelled', took=880), took=900),
                 conclusion='timed_out')
        self.assertReran(gh, sweep(gh)[1])

    def test_each_stall_threshold_is_where_the_config_puts_it(self):
        st = CFG['stalled']
        cases = {
            'the machine stalled just enough': (dict(host={'full_share': st['full_share_percent']}), True),
            'the machine stalled just too little': (dict(host={'full_share': st['full_share_percent'] - 0.1}), False),
            'a box stalled just enough': (dict(cgroups=[box(full_share=st['full_share_percent'])]), True),
            'a box stalled just too little': (dict(cgroups=[box(full_share=st['full_share_percent'] - 0.1)]), False),
            'an ancestor box stalled': (dict(cgroups=[box(), box(path='/fleet.slice', full_share=50.0)]), True),
            'the 5-minute average just enough': (dict(host={'full_avg300': st['full_avg300_percent']}), True),
            'the 5-minute average just too little': (dict(host={'full_avg300': st['full_avg300_percent'] - 0.1}),
                                                     False),
            'only the 10-second average high': (dict(host={'full_avg10': 90.0, 'full_avg60': 90.0}), False),
            'only some tasks stalled': (dict(host={'some_share': 90.0, 'some_avg300': 90.0}), False),
        }
        for label, (kw, qualifies) in cases.items():
            with self.subTest(label):
                gh = one([ann(STEP_LIMIT), note(**kw)], dict(steps=ran('failure', took=600)))
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, 'not stalled')

    def test_the_time_limit_slack_is_where_the_config_puts_it(self):
        slack = CFG['limit_slack_seconds']
        cases = {
            'the job ran its minutes less the slack': (JOB_LIMIT, dict(conclusion='cancelled', took=900 - slack,
                                                                      steps=ran('cancelled', took=820)), True),
            'the job ran a second less': (JOB_LIMIT, dict(conclusion='cancelled', took=900 - slack - 1,
                                                          steps=ran('cancelled', took=820)), False),
            'the step ran its minutes less the slack': (STEP_LIMIT, dict(steps=ran('failure', took=600 - slack)),
                                                        True),
            'the step ran a second less': (STEP_LIMIT, dict(steps=ran('failure', took=600 - slack - 1)), False),
        }
        for label, (message, kw, qualifies) in cases.items():
            with self.subTest(label):
                gh = one([ann(message), STALLED], kw)
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, "GitHub's time-limit text, but")

    def test_a_step_limit_needs_the_step_itself_ended_red(self):
        # GitHub ends a step that ran past its limit cancelled or failed; a step that passed or was skipped did not
        # hit it, however long it took.
        for conclusion, qualifies in (('failure', True), ('cancelled', True), ('success', False), ('skipped', False)):
            with self.subTest(conclusion):
                steps = ran('failure', took=600)
                steps[2] = step('Test', conclusion, took=600, at=10)
                gh = one([ann(STEP_LIMIT), STALLED], dict(steps=steps))
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, "no step 'Test' ran 10 min and ended red")

    def test_a_time_limit_message_counts_only_as_a_whole_runner_message(self):
        for label, a in {'inside a longer line': ann('Error: ' + JOB_LIMIT), 'with text after': ann(JOB_LIMIT + ' x'),
                         'as a warning': ann(JOB_LIMIT, 'warning'), 'with a title': ann(JOB_LIMIT, title='Error'),
                         'on a file': ann(JOB_LIMIT, path='src/x.test.ts')}.items():
            with self.subTest(label):
                gh = one([a, ann(CANCELED), STALLED], dict(conclusion='cancelled', steps=ran('cancelled', took=880),
                                                            took=900))
                self.assertLeft(gh, *sweep(gh)[:2])

    def test_a_time_limit_message_needs_the_time_really_spent(self):
        cases = {
            'the job ran 3 of its 15 minutes': (JOB_LIMIT, dict(steps=ran('cancelled', took=170), took=180)),
            'the step ran 1 of its 10 minutes': (STEP_LIMIT, dict(steps=ran('failure', took=60))),
            'the step it names never ran': ("The action 'Other' has timed out after 10 minutes.",
                                            dict(steps=ran('failure', took=600))),
            'another step failed': (JOB_LIMIT, dict(steps=ran('failure', took=880), took=900)),
        }
        for label, (message, kw) in cases.items():
            with self.subTest(label):
                gh = one([ann(message), STALLED], kw)
                self.assertLeft(gh, *sweep(gh)[:2])

    def test_a_time_limit_message_counts_only_with_the_workflows_own_minutes(self):
        """A failing step can print GitHub's exact words with any number of minutes: "after 1 minutes" makes a step
        that ran 10 minutes look 10 times over its limit. Only the workflow's own timeout-minutes counts."""
        cases = {
            "a failing step printing GitHub's timeout line": ("The action 'Test' has timed out after 1 minutes.",
                                                             dict(steps=ran('failure', took=600))),
            'the job form with the wrong minutes': (JOB_LIMIT.replace('15 minutes', '1 minutes'),
                                                    dict(conclusion='cancelled', steps=ran('cancelled', took=880),
                                                         took=900)),
            'a step with no limit of its own': ("The action 'Checkout' has timed out after 1 minutes.",
                                                dict(steps=[step('Checkout', 'failure', took=600)]
                                                     + ran('skipped')[1:])),
        }
        for label, (message, kw) in cases.items():
            with self.subTest(label):
                gh = one([ann(message), note(cgroups=[box(full_share=50.0)])], kw)
                self.assertLeft(gh, *sweep(gh)[:2], 'not GitHub\'s own')
        with self.subTest('a job the workflow does not have'):
            gh = one([ann(STEP_LIMIT), STALLED], dict(name='Gone (1)', steps=ran('failure', took=600)))
            self.assertLeft(gh, *sweep(gh)[:2], 'the workflow gives step \'Test\' None min')
        with self.subTest('the workflow at that commit cannot be read'):
            gh = one([ann(STEP_LIMIT), STALLED], dict(steps=ran('failure', took=600)))
            gh.workflow = None
            code, out, _ = sweep(gh)
            self.assertEqual((code, gh.posts(), gh.comments()), (1, [], []), out)

    def test_the_workflow_reader_reads_every_limit_of_the_real_workflow(self):
        import yaml
        with open(os.path.join(ROOT, '.github/workflows', CFG['workflow']), encoding='utf-8') as f:
            text = f.read()
        real, mine = yaml.safe_load(text)['jobs'], cr.workflow_limits(text)
        self.assertEqual(sorted(mine), sorted(real))
        for key, j in real.items():
            self.assertEqual((mine[key]['name'], mine[key]['minutes']), (j.get('name'), j.get('timeout-minutes')), key)
            self.assertEqual([(s['name'], s['minutes']) for s in mine[key]['steps']],
                             [(s.get('name'), s.get('timeout-minutes')) for s in j.get('steps') or []], key)
        wf = cr.workflow_limits(text)
        self.assertEqual(cr.limit_of(wf, 'Lint', CFG['probe_end_step']), 1)
        self.assertEqual(cr.limit_of(wf, 'Lint'), real['lint']['timeout-minutes'])
        self.assertIsNone(cr.limit_of(wf, 'Lint', 'Check'), 'a step without timeout-minutes has none of its own')

    def test_the_reader_takes_only_values_it_is_sure_of(self):
        cases = {
            'plain': ('10', '10'), 'a comment after it': ("10   # the step's own", '10'),
            'single-quoted': ("'Note it'", 'Note it'), 'a quote in single quotes': ("'it''s'", "it's"),
            'empty single quotes': ("''", ''), 'double-quoted': ('"Test"', 'Test'), 'empty double quotes': ('""', ''),
            'an escape in double quotes': ('"a\\tb"', None), 'a block': ('|', None), 'a folded block': ('>-', None),
            'an anchor': ('&x 10', None), 'a list': ('[1]', None), 'a map': ('{a: 1}', None), 'a lone quote': ("'", None),
            'mismatched quotes': ('\'a"', None), 'a quote only at the end': ("x'", "x'"),
            'a double quote only at the end': ('x"', 'x"'), 'a bar inside': ('a|', 'a|'), 'empty': ('', None),
        }
        for label, (raw, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(cr.scalar(raw), want)
        text = WORKFLOW.replace('    timeout-minutes: 15', '    timeout-minutes: ${{ inputs.minutes || 15 }}') + (
            'env:\n  A: 1\n  B:\n    C:\n      - name: x\n        timeout-minutes: 3\n')
        wf = cr.workflow_limits(text)   # a key after `jobs:` ends the jobs; a limit that is not a number is none
        self.assertEqual((sorted(wf), wf['test']['minutes'], cr.limit_of(wf, 'Test (1)', 'Test')),
                         (['lint', 'test'], None, 10))
        self.assertEqual([s['name'] for s in wf['test']['steps']],
                         ['Checkout', 'Note memory pressure at the start', 'Test', END_STEP])

    def test_a_name_matches_only_as_github_shows_it(self):
        wf = cr.workflow_limits(WORKFLOW)
        self.assertEqual((cr.limit_of(wf, 'Test (1)'), cr.limit_of(wf, 'Test'), cr.limit_of(wf, 'Test (1)', 'Test')),
                         (15, 15, 10))
        self.assertIsNone(cr.limit_of(wf, 'Pre Test (1)'), 'a longer name is another job')
        self.assertIsNone(cr.limit_of(wf, 'Test (1) x'))
        self.assertIsNone(cr.limit_of(wf, 'Test (1)', 'Test (2)'), "a step's name never carries matrix values")
        self.assertIsNone(cr.limit_of(wf, 'Test (1)', 'Pre Test'))

    def test_the_workflow_is_read_once_at_the_runs_own_commit(self):
        gh = Fake([run()])
        limits = cr.workflow_reader(CFG, gh, run())
        self.assertEqual((limits('Test (1)', 'Test'), limits('Test (2)'), limits('Lint')), (10, 15, 20))
        self.assertEqual(sum(1 for _, p in gh.calls if p.startswith('contents/')), 1)
        for sha in ('main', SHA + '&ref=main', 'v' + SHA):   # not exactly one commit: could change under the reader
            with self.assertRaisesRegex(ValueError, 'no commit to read the workflow at'):
                cr.workflow_reader(CFG, gh, dict(run(), head_sha=sha))('Test (1)')
        self.assertEqual(sum(1 for _, p in gh.calls if p.startswith('contents/')), 1)
        real = gh.call
        gh.call = lambda path, method='GET', data=None: dict(real(path, method, data), encoding='utf-8')
        with self.assertRaisesRegex(ValueError, 'not readable through the contents API'):
            cr.workflow_reader(CFG, gh, run())('Test (1)')

    def test_a_job_with_no_note_at_all_is_left_red(self):
        gh = one([ann(EXIT_137)])
        self.assertLeft(gh, *sweep(gh)[:2], 'no pressure note')

    def test_a_stalled_note_without_its_status_is_no_limit_sign(self):
        n = dict(STALLED, message=STALLED['message'].replace('"status":"failure",', ''))
        self.assertNotIn('status', n['message'])
        gh = one([ann(STEP_LIMIT), n], dict(steps=ran('failure', took=600)))
        self.assertLeft(gh, *sweep(gh)[:2], 'garbled')

    def test_time_limit_on_a_calm_machine_is_left_red(self):
        gh = one([ann(JOB_LIMIT), ann(CANCELED), CALM], dict(conclusion='cancelled', steps=ran('cancelled', took=880),
                                                            took=900))
        code, out, text = sweep(gh)
        self.assertLeft(gh, code, out, 'not stalled')
        self.assertIn('left red', text)

    def test_time_limit_without_a_note_is_left_red_fail_closed(self):
        gh = one([ann(JOB_LIMIT), ann(CANCELED)], dict(conclusion='cancelled', took=900,
                                                      steps=ran('cancelled', took=880, end=False)))
        self.assertLeft(gh, *sweep(gh)[:2], 'cannot tell it was stalled')

    def test_a_test_failure_is_never_rerun(self):
        gh = one([ann(EXIT_1), CALM])
        code, out, text = sweep(gh)
        self.assertLeft(gh, code, out, 'Test (1) ended failure: no infrastructure sign')
        self.assertIn('| 1 | left red |', text)

    def test_one_job_without_a_sign_leaves_the_whole_run_red(self):
        jobs = [job(11, steps=died()), job(12, 'Test (2)'), job(13, FOLLOWER, steps=[])]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)], 12: [ann(EXIT_1), CALM]})
        self.assertLeft(gh, *sweep(gh)[:2], 'Test (2) ended failure')

    def test_every_failed_job_with_a_sign_is_rerun_together(self):
        jobs = [job(11, steps=died()), job(12, 'Build and Typecheck'), job(13, FOLLOWER, steps=[])]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)], 12: [ann(EXIT_137), FULL]})
        code, out, _ = sweep(gh)
        self.assertEqual(gh.posts(), ['actions/runs/1/rerun-failed-jobs'], out)
        self.assertIn('Test (1) (failure): lost', out)
        self.assertIn('Build and Typecheck (failure): memory full', out)

    def test_only_a_follower_failed_is_left_red(self):
        gh = Fake([run()], {1: [job(12, 'Lint', 'success'), job(13, FOLLOWER, steps=died())]}, {13: [ann(LOST)]})
        self.assertLeft(gh, *sweep(gh)[:2], 'no failed job of its own')

    def test_a_job_skipped_after_a_failed_one_needs_no_sign(self):
        jobs = [job(11, steps=died()), job(12, 'Lint', 'skipped'), job(13, FOLLOWER, steps=[])]
        gh = Fake([run()], {1: jobs}, {11: [ann(LOST)]})
        self.assertReran(gh, sweep(gh)[1])


class StepOutputIsNoSign(Rerun):
    """A step can print any annotation (::error::GitHub's words, ::notice title=fork-ci-pressure::...), with exactly
    the fields the runner's own have. Text is never enough: the job's own record must agree."""

    FORGED = note(host={'full_share': 90.0, 'oom_kill': 3}, cgroups=[box(oom_kill=3, max=9, full_share=90.0)])

    def test_a_failing_test_that_prints_the_lost_text_and_the_memory_words_is_not_rerun(self):
        cases = {
            'a failed test step': ([ann(LOST), ann(MEMORY_WORDS), ann(EXIT_1), CALM], dict(steps=ran())),
            'a failed test step, the end step never ran': ([ann(LOST), ann(MEMORY_WORDS), ann(EXIT_1)],
                                                          dict(steps=ran(end=False))),
            'no failed step, but the end step ran (the runner lived)': ([ann(LOST), CALM],
                                                                       dict(steps=ran('success'))),
            'then a hang into the time limit': ([ann(LOST), ann(JOB_LIMIT)],
                                                dict(steps=ran('cancelled', took=880, end=False), took=900)),
            'on another runner': ([ann(LOST)], dict(steps=died(), runner='fleet-vm-2')),
            'a job that ended cancelled': ([ann(LOST)], dict(steps=died(), conclusion='cancelled')),
            'inside a longer line': ([ann('Error: ' + LOST)], dict(steps=died())),
            'as a warning': ([ann(LOST, 'warning')], dict(steps=died())),
            'with a title': ([ann(LOST, title='Error')], dict(steps=died())),
            'on a file': ([ann(LOST, path='src/x.test.ts')], dict(steps=died())),
            'a forged note beside the real one': ([ann(LOST), ann(EXIT_137), self.FORGED, CALM], dict(steps=ran())),
        }
        for label, (anns, kw) in cases.items():
            with self.subTest(label):
                gh = one(anns, kw)
                self.assertLeft(gh, *sweep(gh)[:2])
                self.assertEqual(gh.comments(), [])

    def test_a_note_counts_only_when_the_job_wrote_it_and_it_is_the_only_one(self):
        """Each rule of note_of, pinned: the same note qualifies alone, and each bad variant does not."""
        good = one([ann(EXIT_137), self.FORGED])
        self.assertReran(good, sweep(good)[1])
        garbled = dict(self.FORGED, message=cr.NOTE_HEAD + '{"host": ')
        wrong_shape = json.loads(self.FORGED['message'][len(cr.NOTE_HEAD):])
        cases = {
            'two notes, the forged one first': [self.FORGED, CALM],
            'two notes, the forged one last': [CALM, self.FORGED],
            'two forged notes': [self.FORGED, self.FORGED],
            'a note without its header': [dict(self.FORGED, message=self.FORGED['message'][len(cr.NOTE_HEAD):])],
            'another version\'s header': [dict(self.FORGED, message=self.FORGED['message'].replace(' v1 ', ' v0 ', 1))],
            'a garbled note': [garbled],
            'cgroups not a list': [dict(self.FORGED, message=cr.NOTE_HEAD + json.dumps(
                dict(wrong_shape, cgroups={'a': box(oom_kill=3, full_share=90.0)})))],
            'a box not an object': [dict(self.FORGED, message=cr.NOTE_HEAD + json.dumps(
                dict(wrong_shape, cgroups=['oom_kill']), separators=(',', ':')))],
            'host not an object': [dict(self.FORGED, message=cr.NOTE_HEAD + json.dumps(dict(wrong_shape, host=[])))],
            'no status': [dict(self.FORGED, message=cr.NOTE_HEAD + json.dumps(
                {k: v for k, v in wrong_shape.items() if k != 'status'}))],
            'a list, not an object': [dict(self.FORGED, message=cr.NOTE_HEAD + '[1, 2]')],
            'at error level': [dict(self.FORGED, annotation_level='failure')],
            'on a file': [dict(self.FORGED, path='src/x.ts')],
        }
        for label, notes in cases.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137)] + notes)
                code, out, _ = sweep(gh)
                self.assertLeft(gh, code, out)
        for label, steps in {'the end step never ran': ran(end=False),
                             'the end step failed': ran()[:-1] + [step(END_STEP, 'failure')],
                             'no steps listed': []}.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), self.FORGED], dict(steps=steps))
                self.assertLeft(gh, *sweep(gh)[:2], 'did not complete')

    def test_a_note_must_fit_the_runners_own_times_of_the_probe_steps(self):
        """A step can print a note in the runner's own form and keep the end step from writing its own (or have the
        real one dropped at GitHub's cap). Its seconds must fit the time between the two probe steps by the runner's
        own record, which a step that prints a note earlier cannot know."""
        window = cr.probe_window(CFG, job(11))
        self.assertEqual(window, (70 - 10 - CFG['note_slack_seconds'], 75 - 5 + CFG['note_slack_seconds']))
        full = [box(oom_kill=1, full_share=30.0)]
        for seconds, qualifies in ((window[0], True), (window[1], True), (window[0] - 1, False), (window[1] + 1, False),
                                   (600, False), (None, False), ('65', False), (True, False)):
            with self.subTest(seconds=seconds):
                gh = one([ann(EXIT_137), note(cgroups=full, seconds=seconds)])
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, "not its end step's")
        start = 'Note memory pressure at the start'
        for label, steps in {'no start step': [s for s in ran() if s['name'] != start],
                             'two start steps': ran()[:2] + ran()[1:],
                             'a start step that never completed': [s if s['name'] != start else
                                                                   step(start, None, 'in_progress', at=5)
                                                                   for s in ran()]}.items():
            with self.subTest(label):
                gh = one([ann(EXIT_137), note(cgroups=full, seconds=65)], dict(steps=steps))
                self.assertLeft(gh, *sweep(gh)[:2], 'times to check the note against')

    def test_at_githubs_annotation_cap_no_note_is_trusted(self):
        """GitHub keeps the first annotation_cap annotations of a job. The end step's note comes last, so at the cap
        the real one may be gone and a step's printed note stands alone: refused, whatever it says."""
        cap = CFG['annotation_cap']
        lint = [ann(f'line {i}: unused', 'warning', path='src/x.ts') for i in range(cap)]
        for others, qualifies in ((cap - 2, True), (cap - 1, False), (cap, False)):
            with self.subTest(annotations=others + 1):
                gh = one(lint[:others] + [self.FORGED])
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, f"GitHub's cap of {cap}")

    def test_a_stopped_end_probe_leaves_no_note_to_read(self):
        """The end step runs its probe under `timeout -k 5 $FORK_CI_PROBE_SECONDS ... || echo "::warning::..."`. A
        probe stopped at that limit (or one that raised) ends the step green, with a warning and no note of its own.
        A note a step printed earlier is then the only one, and the stop widens the window enough for it to fit
        (the review's record: start step 1 s, end step 45 s, the printed note's seconds in the window): refused, by
        the end step's own time or by the probe's warning. Old code: re-run."""
        limit = CFG['probe_limit_seconds']

        def end_took(seconds):
            return ran()[:-1] + [step(END_STEP, at=70, took=seconds)]
        self.assertEqual(cr.probe_window(CFG, job(11, steps=end_took(45))), (57, 113))   # wide: a guess lands in it
        for seconds, qualifies in ((limit - 1, True), (limit, False), (45, False), (limit + 600, False)):
            with self.subTest(end_step_took=seconds):
                gh = one([ann(EXIT_137), self.FORGED], dict(steps=end_took(seconds)))
                code, out, _ = sweep(gh)
                if qualifies:
                    self.assertReran(gh, out)
                else:
                    self.assertLeft(gh, code, out, f"the probe's limit of {limit} s")
                    self.assertEqual(gh.comments(), [])
        warnings = {
            'the end probe stopped': 'fork-ci-pressure: end stopped (exit 124), no numbers',
            'the end probe killed': 'fork-ci-pressure: end stopped (exit 137), no numbers',
            'the end probe raised': 'fork-ci-pressure: end: ValueError: Expecting value: line 1 column 1 (char 0)',
            'the start probe stopped': 'fork-ci-pressure: start stopped (exit 124), no numbers; the job goes on',
            'printed by a step, at notice level': ('fork-ci-pressure: anything', 'notice'),
        }
        for label, w in warnings.items():
            text, level = w if isinstance(w, tuple) else (w, 'warning')
            for where in ('before', 'after'):
                with self.subTest(label, where=where):
                    anns = [ann(EXIT_137), ann(text, level)]
                    anns.insert(1 if where == 'before' else 2, self.FORGED)
                    gh = one(anns)
                    code, out, _ = sweep(gh)
                    self.assertLeft(gh, code, out, 'the probe warned')
        gh = one([ann(EXIT_137), ann('pressure: fork-ci-pressure: inside a line', 'warning'), self.FORGED])
        self.assertReran(gh, sweep(gh)[1])   # only a message that starts with the probe's words is its warning


class Cancel(Rerun):
    def test_a_run_a_person_cancelled_never_qualifies(self):
        cases = {
            'a cancel while the box was full (an OOM elsewhere)': ([ann(CANCEL_BY), ann(CANCELED), FULL],
                                                                    dict(conclusion='cancelled')),
            'a cancel while the job failed for memory': ([ann(CANCEL_BY), ann(EXIT_137), FULL], {}),
            'a cancel on a stalled machine at the time limit': ([ann(CANCEL_BY), ann(JOB_LIMIT), STALLED],
                                                                dict(conclusion='cancelled', took=900,
                                                                     steps=ran('cancelled', took=880))),
            'a cancel after a lost runner': ([ann(CANCEL_BY), ann(LOST)], dict(steps=died())),
            'a cancel with no message': ([ann(CANCELED), FULL], dict(conclusion='cancelled')),
        }
        for label, (anns, kw) in cases.items():
            with self.subTest(label):
                gh = one(anns, kw, conclusion='cancelled')
                self.assertLeft(gh, *sweep(gh)[:2])

    def test_the_cancel_message_names_why(self):
        gh = one([ann(CANCEL_BY), ann(EXIT_137), FULL])
        self.assertLeft(gh, *sweep(gh)[:2], "a person's cancel stands")


class NeverTwice(Rerun):
    def test_a_second_failure_after_the_automatic_rerun_stays_red(self):
        gh = lost(attempt=2, by=ACTIONS)
        code, out, text = sweep(gh)
        self.assertLeft(gh, code, out, 'a repeat red stays red')
        self.assertFalse([p for _, p in gh.calls if '/jobs' in p or 'pulls/' in p], 'it costs no further read')

    def test_a_person_rerunning_after_the_automatic_rerun_gets_no_second_one(self):
        for bot_attempt in (1, 2):   # the loop reads every earlier attempt, down to the first
            with self.subTest(bot_attempt=bot_attempt):
                gh = lost(attempt=3, by=OWNER)
                gh.attempts[(1, bot_attempt)] = run(attempt=bot_attempt, by=ACTIONS)
                code, out, _ = sweep(gh)
                self.assertLeft(gh, code, out, f'attempt {bot_attempt} was triggered by')
                self.assertIn('never twice', out)

    def test_a_person_rerun_without_an_automatic_one_may_get_it(self):
        gh = lost(attempt=2, by=OWNER)   # attempt 1 by the owner too (the fake's default)
        self.assertReran(gh, sweep(gh)[1])

    def test_the_sweep_runs_again_on_the_run_it_reran(self):
        gh = lost()
        sweep(gh)
        self.assertEqual(len(gh.posts()), 1)
        code, out, _ = sweep(gh)   # the re-run is queued: not completed, nothing to do
        self.assertEqual((code, len(gh.posts()), len(gh.comments())), (0, 1, 1), out)
        gh.runs[1].update(status='completed', conclusion='failure')   # and it failed again, the same way
        code, out, _ = sweep(gh)
        self.assertEqual((code, len(gh.posts()), len(gh.comments())), (0, 1, 1), out)
        self.assertIn('a repeat red stays red', out)

    def test_the_request_sent_twice_github_refuses_the_second(self):
        gh = lost()
        first = copy.deepcopy(gh.runs[1])
        sweep(gh)
        # A second pass (or the fleet sweep) read the run before the first re-ran it and asks again.
        done, how = cr.rerun(gh, first, lambda: self.fail('no second record'))
        self.assertFalse(done)
        self.assertIn('already re-run', how)
        self.assertEqual(len(gh.posts()), 1, 'the second request is not even sent: the run is read again first')

    def test_a_run_started_again_after_it_was_listed_is_not_asked_for(self):
        gh = lost()
        gh.on_read = lambda r: r.update(status='in_progress')   # same attempt, running again (a person's re-run)
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts(), gh.comments()), (0, [], []), out)
        self.assertIn('already re-run or running again', out)

    def test_a_run_rerun_and_red_again_after_it_was_listed_is_not_asked_for(self):
        """Between the listing and the fresh read, another pass re-ran it and attempt 2 already ended red: the listed
        attempt 1 still qualifies, so only the fresh read's attempt number stops an attempt 3."""
        gh = lost()
        gh.on_read = lambda r: r.update(run_attempt=2, status='completed', conclusion='failure',
                                        triggering_actor={'login': ACTIONS})
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts(), gh.comments()), (0, [], []), out)
        self.assertIn('already re-run or running again (attempt 2, completed)', out)

    def test_github_refusing_a_rerun_that_already_started_is_no_trouble(self):
        gh = lost()
        def race():   # another pass re-ran it between this pass's last read and its request
            gh.runs[1].update(run_attempt=2, status='queued', triggering_actor={'login': ACTIONS})
            return urllib.error.HTTPError('x', 403, 'This workflow is already running', {}, None)
        gh.post_fails = race
        code, out, _ = sweep(gh)
        self.assertEqual(code, 0, out)
        self.assertIn('already re-run (GitHub refused a second request: HTTP 403)', out)

    def test_a_refusal_with_the_run_unchanged_is_trouble_and_tried_again_with_one_comment(self):
        gh = lost()
        gh.post_fails = urllib.error.HTTPError('x', 500, 'Server Error', {}, None)
        code, out, text = sweep(gh)
        self.assertEqual(code, 1, out)
        self.assertIn('the next pass tries again', out)
        self.assertIn('not read', text)
        self.assertEqual(len(gh.comments()), 1, 'the reason is on record before the request')
        code, out, _ = sweep(gh)   # the next pass
        self.assertEqual(code, 0, out)
        self.assertEqual((len(gh.posts()), gh.runs[1]['run_attempt'], len(gh.comments())), (2, 2, 1))

    def test_stopped_half_way_the_request_never_reached_github(self):
        gh = lost()
        gh.post_fails = urllib.error.URLError('timed out')
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.runs[1]['run_attempt']), (1, 1), out)
        code, out, _ = sweep(gh)   # the next pass finishes it, once
        self.assertEqual((code, gh.runs[1]['run_attempt']), (0, 2), out)
        sweep(gh)
        self.assertEqual((len(gh.posts()), len(gh.comments())), (2, 1))

    def test_a_reply_lost_after_github_took_the_request_still_leaves_the_why(self):
        gh = lost()
        gh.lost_reply = True   # GitHub made attempt 2; the sweep never heard back
        code, out, _ = sweep(gh)
        self.assertEqual(code, 1, out)
        [comment] = gh.comments()
        self.assertIn('<!-- fork-ci-rerun run=1 attempt=1 what=rerun -->', comment)
        self.assertIn('lost', comment)
        self.assertIn(gh.runs[1]['html_url'], comment)
        gh.lost_reply = False
        for _ in range(2):   # queued, never started: the next passes leave it alone
            code, out, _ = sweep(gh)
            self.assertEqual((code, len(gh.posts())), (0, 1), out)
        gh.runs[1].update(status='completed', conclusion='cancelled')   # it never ran (cancelled while queued)
        code, out, _ = sweep(gh)
        self.assertEqual((len(gh.posts()), len(gh.comments())), (1, 1), out)
        self.assertIn('a repeat red stays red', out)
        self.assertIn('1 automatic re-run(s) asked in the last 24 h', out)

    def test_no_record_no_rerun(self):
        gh = lost()
        gh.comment_fails = True   # the notice cannot be written: the re-run would be hidden, so none
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts()), (1, []), out)
        gh.comment_fails = False
        code, out, _ = sweep(gh)
        self.assertEqual((code, len(gh.posts()), len(gh.comments()), len(gh.issues)), (0, 1, 1, 1), out)


class TheNotice(Rerun):
    def test_every_rerun_is_a_comment_on_one_issue_opened_once(self):
        jobs = {1: [job(11, steps=died()), job(13, FOLLOWER, steps=[])], 2: [job(21), job(23, FOLLOWER, steps=[])]}
        gh = Fake([run(1), run(2, branch='feature', event='pull_request')], jobs,
                  {11: [ann(LOST)], 21: [ann(EXIT_137), FULL]})
        code, out, text = sweep(gh)
        self.assertEqual((code, len(gh.posts()), len(gh.issues)), (0, 2, 1), out)
        [issue] = gh.issues
        self.assertEqual((issue['title'], issue['labels'], issue['user']['login']), (TITLE, CFG['notice']['labels'],
                                                                                    BOT))
        bodies = {re.search(r'run=([0-9]+)', c['body'])[1]: c['body'] for c in issue['comments']}
        self.assertEqual(sorted(bodies), ['1', '2'])
        self.assertIn('what=rerun', bodies['1'])
        second = {'body': bodies['2']}
        self.assertIn('box /fleet.slice/job-1.service: max 8192 MB high 6144 MB', second['body'])
        self.assertIn('OOM kill', second['body'])
        self.assertIn(f'issue #{issue["number"]}', text)
        self.assertIn('2 automatic re-run(s) asked in the last 24 h', text)
        # A later pass, a later run: the same issue.
        gh.runs[3] = run(3, created=T0 - 60)
        gh.jobs[3] = [job(31, steps=died())]
        gh.ann[31] = [ann(LOST)]
        sweep(gh)
        self.assertEqual((len(gh.issues), len(issue['comments'])), (1, 3))

    def test_only_an_open_issue_the_bot_opened_with_the_marker_is_ours(self):
        gh = lost()
        gh.issues = [
            tracking(3, user=OWNER),                                   # a person's, marker and all
            tracking(4, body=f'Seen it.\n{cr.ISSUE_MARK}'),            # the marker not first: quoted, not ours
            tracking(5, state='closed'),
            tracking(6, pull_request={}),
            tracking(7, title='Secret sweep failed', body='The sweep failed.'),   # the bot's other issues
        ]
        sweep(gh)
        [ours] = [i for i in gh.issues if i['comments']]
        self.assertNotIn(ours['number'], (3, 4, 5, 6, 7))
        self.assertEqual((ours['title'], ours['user']['login']), (TITLE, BOT))
        self.assertTrue(ours['body'].startswith(cr.ISSUE_MARK + '\n'))
        # Renaming the issue keeps it ours: the marker, not the title, finds it.
        ours['title'] = 'CI re-runs (renamed)'
        gh.runs[3] = run(3, created=T0 - 60)
        gh.jobs[3] = [job(31, steps=died())]
        gh.ann[31] = [ann(LOST)]
        sweep(gh)
        self.assertEqual((len(gh.issues), len(ours['comments'])), (6, 2))

    def test_a_pass_whose_issue_reply_was_lost_opens_no_second_one(self):
        # GitHub took the POST, the reply never came back: the pass stops red with the issue open. The next pass
        # finds it by its marker and uses it (#707's code found it by its title, which also held here).
        gh = lost()
        real = gh.call
        def call(path, method='GET', data=None):
            got = real(path, method, data)
            if method == 'POST' and path == 'issues':
                raise urllib.error.URLError('issue: reply lost')
            return got
        gh.call = call
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts(), len(gh.issues), gh.comments()), (1, [], 1, []), out)
        gh.call = real
        for _ in range(2):   # runs again: the open issue is found by its marker, never a second one
            code, out, _ = sweep(gh)
            self.assertEqual(code, 0, out)
        self.assertEqual((len(gh.posts()), len(gh.issues), len(gh.comments())), (1, 1, 1), out)

    def test_an_issue_another_pass_opened_since_this_one_loaded_is_used(self):
        gh = lost()
        real = gh.call
        def call(path, method='GET', data=None):
            got = real(path, method, data)
            if method == 'GET' and path.startswith('issues?') and not gh.issues:
                gh.issues.append(tracking(41))   # a dispatched pass opened it just after this pass's load()
            return got
        gh.call = call
        code, out, _ = sweep(gh)
        self.assertEqual((code, [i['number'] for i in gh.issues], len(gh.issues[0]['comments'])), (0, [41], 1), out)
        self.assertNotIn(('POST', 'issues'), gh.calls)

    def test_two_open_issues_are_made_one_by_the_next_pass(self):
        # A pass stopped after opening its own and before closing it as a duplicate: both are open.
        gh = lost()
        gh.issues = [tracking(12), tracking(10)]
        code, out, _ = sweep(gh)
        self.assertEqual(code, 0, out)
        first, second = sorted(gh.issues, key=lambda i: i['number'])
        self.assertEqual((first['state'], len(first['comments'])), ('open', 1))
        self.assertEqual((second['state'], second['state_reason'], [c['body'] for c in second['comments']]),
                         ('closed', 'not_planned', ['Duplicate of #10.']))

    def test_a_duplicate_that_cannot_be_closed_still_leaves_one_record(self):
        # Each pass tries the close again; a close that keeps failing posts no "Duplicate of" (old code: one more
        # every pass, 10 minutes apart), the pass warns, and the close that works posts it once.
        gh = lost()
        gh.issues = [tracking(10), tracking(12)]
        real = gh.call
        def call(path, method='GET', data=None):
            if method == 'PATCH':
                raise urllib.error.URLError('patch: reset')
            return real(path, method, data)
        gh.call = call
        for n in range(3):
            code, out, _ = sweep(gh)
            self.assertEqual((code, len(gh.posts()), len(gh.issue(10)['comments'])), (0, 1, 1), out)
            self.assertEqual((gh.issue(12)['state'], gh.issue(12)['comments']), ('open', []), f'pass {n + 1}')
            self.assertIn('::warning::fork-ci re-run: a second tracking issue could not be closed as a duplicate of '
                          'issue #10 (#12: URLError', out)
        gh.call = real
        for _ in range(2):   # runs again: closes it this time, says so once, and writes nothing twice
            code, out, _ = sweep(gh)
            self.assertNotIn('could not be closed', out)
        self.assertEqual((gh.issue(12)['state'], [c['body'] for c in gh.issue(12)['comments']],
                          len(gh.issue(10)['comments'])), ('closed', ['Duplicate of #10.'], 1))

    def test_every_pass_closes_a_duplicate_even_with_nothing_to_record(self):
        # The runbook's promise: a pass reads the issue for its 24 h count at its end, so the next pass closes an
        # extra one, even with no red run (or only a fleet-mode report) to record.
        gh = Fake([])
        gh.issues = [tracking(12), tracking(10)]
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.issue(10)['state'], gh.issue(12)['state']), (0, 'open', 'closed'), out)
        self.assertIn('(issue #10)', out)

    def test_a_pass_whose_own_duplicate_another_pass_closed_leaves_it_alone(self):
        # Two passes open one each at once; the other pass's load() closes this pass's issue (#41) before this
        # pass reads the open ones again. This pass uses #10 and neither closes #41 again nor posts a second
        # "Duplicate of" (the `mine in ours` guard).
        gh = lost()
        real = gh.call
        def call(path, method='GET', data=None):
            got = real(path, method, data)
            if method == 'POST' and path == 'issues':
                gh.issues.insert(0, tracking(10))
                gh.issue(41).update(state='closed', state_reason='not_planned')   # the other pass's load()
                gh.issue(41)['comments'].append({'id': 1, 'body': 'Duplicate of #10.', 'user': {'login': BOT},
                                                 'created_at': iso(gh.now)})
            return got
        gh.call = call
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts(), len(gh.issue(10)['comments'])), (0, ['actions/runs/1/rerun-failed-jobs'],
                                                                             1), out)
        self.assertEqual([c['body'] for c in gh.issue(41)['comments']], ['Duplicate of #10.'])
        self.assertNotIn(('PATCH', 'issues/41'), gh.calls)

    def test_two_passes_opening_it_at_once_keep_the_lowest(self):
        gh = lost()
        real = gh.call
        def call(path, method='GET', data=None):
            got = real(path, method, data)
            if method == 'POST' and path == 'issues' and len(gh.issues) == 1:
                # A dispatched pass opened one too, just before this one.
                gh.issues.insert(0, tracking(10))
            return got
        gh.call = call
        sweep(gh)
        lowest, mine = sorted(gh.issues, key=lambda i: i['number'])
        self.assertEqual((lowest['number'], len(lowest['comments'])), (10, 1))
        self.assertEqual((mine['state'], mine['comments'][0]['body']), ('closed', 'Duplicate of #10.'))

    def test_the_24_hour_count_reads_every_comment_past_hundreds_of_reruns(self):
        gh = Fake([])
        gh.issues = [tracking(2)]
        comments = gh.issues[0]['comments']
        for n in range(350):   # one every 4 minutes: 350 in the last 24 h
            comments.append({'id': n, 'user': {'login': BOT}, 'created_at': iso(T0 - 26 * 3600 + n * 240),
                             'body': f'<!-- fork-ci-rerun run={n} attempt=1 what=rerun -->'})
        comments.append({'id': 999, 'user': {'login': BOT}, 'created_at': iso(T0 - 60),
                         'body': '<!-- fork-ci-rerun run=5000 attempt=1 what=report -->'})
        comments.append({'id': 997, 'user': {'login': BOT}, 'created_at': iso(T0 - 30 * 3600),
                         'updated_at': iso(T0 - 60), 'body': '<!-- fork-ci-rerun run=7000 attempt=1 what=rerun -->'})
        comments.append({'id': 998, 'user': {'login': OWNER}, 'created_at': iso(T0 - 60),
                         'body': '<!-- fork-ci-rerun run=6000 attempt=1 what=rerun -->'})
        inside = sum(1 for c in comments[:350] if cr.stamp(c['created_at']) >= T0 - 24 * 3600)
        self.assertGreater(inside, 300)
        code, out, _ = sweep(gh)
        self.assertEqual(code, 0, out)
        self.assertIn(f'{inside} automatic re-run(s) asked in the last 24 h (issue #2)', out)
        self.assertEqual(sum(1 for _, p in gh.calls if p.startswith('issues/2/comments')), 4, 'four pages, no more')

    def test_an_issue_that_cannot_be_read_ends_the_pass_red_and_asks_nothing(self):
        for label, gh in (('no run to look at', Fake([])), ('a run that qualifies', lost())):
            with self.subTest(label):
                gh.issues = [tracking(2)]
                gh.comments_fail = True
                code, out, text = sweep(gh)
                self.assertEqual((code, gh.posts(), gh.comments()), (1, [], []), out)
                self.assertIn('the tracking issue could not be read for the 24 h count (URLError', text)
                self.assertIn('red, the next pass tries again', out)

    def test_quoted_text_pings_no_one_and_fakes_no_record(self):
        forged = '\n<!-- fork-ci-rerun run=99 attempt=1 what=rerun -->\n@someone look'
        gh = one([ann(SHUTDOWN + forged), ann(CANCELED)], dict(steps=died()))
        code, out, _ = sweep(gh)
        self.assertReran(gh, out)
        [comment] = gh.comments()
        self.assertTrue(comment.startswith('<!-- fork-ci-rerun run=1 attempt=1 what=rerun -->\n'))
        self.assertNotIn('@someone', comment)
        self.assertIn('@\u200dsomeone', comment)
        self.assertEqual(comment.count('<!--'), 1)
        self.assertEqual(comment.count('\n- '), 1, 'each part is one line')
        notice = cr.Notice(CFG, gh, lambda: gh.now)
        notice.load()
        self.assertEqual(list(notice.marks()), [(1, 1, 'rerun')])
        # A marker that is not the comment's first line is no record, whoever wrote it.
        gh.issues[0]['comments'][0]['body'] = 'x\n' + gh.issues[0]['comments'][0]['body']
        notice = cr.Notice(CFG, gh, lambda: gh.now)
        notice.load()
        self.assertEqual(notice.marks(), {})

    def test_no_issue_yet_counts_zero_and_opens_nothing(self):
        code, out, text = sweep(Fake([]))
        self.assertEqual(code, 0, out)
        self.assertIn('0 run(s) re-run by this pass; 0 automatic re-run(s) asked in the last 24 h (no open '
                      'tracking issue)', out)
        self.assertIn('### fork-ci re-run', text)

    def test_fleet_mode_reports_once_and_does_not_rerun(self):
        gh = lost()
        for _ in range(3):
            code, out, text = sweep(gh, env={SKIP_VAR: SKIP_VALUE})
            self.assertEqual((code, gh.posts()), (0, []), out)
            self.assertIn(f'not re-run ({SKIP_VAR}={SKIP_VALUE})', text)
        self.assertEqual(len(gh.comments()), 1)
        self.assertIn('what=report', gh.comments()[0])
        self.assertIn('re-run it by hand', gh.comments()[0])
        _, first, _ = sweep(lost(), env={SKIP_VAR: SKIP_VALUE})
        self.assertIn('::error title=fork-ci re-run::', first)
        self.assertNotIn('::error', out, 'reported once: no repeated error note')
        self.assertIn('reported already', out)
        self.assertIn('0 automatic re-run(s)', out, 'a report is not a re-run')
        gh = lost()
        sweep(gh, env={SKIP_VAR: 'auto'})
        self.assertEqual(len(gh.posts()), 1)


class Paging(unittest.TestCase):
    """GitHub.pages, the script's real paging code, over a fake call()."""

    class Pager:
        pages = cr.GitHub.pages

        def __init__(self, items, shift_at=None):
            self.items, self.shift_at, self.paths = items, shift_at, []

        def call(self, path):
            self.paths.append(path)
            n = int(re.search(r'[?&]page=([0-9]+)', path)[1])
            if n == self.shift_at:   # two new items arrived: the next page starts two earlier
                self.items = [{'id': -1}, {'id': -2}] + self.items
            return {'list': self.items[(n - 1) * 100:n * 100]}

    def test_every_page_until_a_short_one_each_item_once(self):
        p = self.Pager([{'id': i} for i in range(320)], shift_at=2)
        got = p.pages('x?a=1', 'list')
        self.assertEqual(sorted(i['id'] for i in got), list(range(320)), 'none lost, none twice')
        self.assertEqual(p.paths, [f'x?a=1&per_page=100&page={n}' for n in (1, 2, 3, 4)])

    def test_an_exact_multiple_reads_the_empty_page_after(self):
        p = self.Pager([{'id': i} for i in range(200)])
        self.assertEqual(len(p.pages('x', 'list')), 200)
        self.assertEqual(p.paths[-1], 'x?per_page=100&page=3')

    def test_more_than_most_pages_is_an_error_not_a_short_list(self):
        with self.assertRaises(ValueError):
            self.Pager([{'id': i} for i in range(300)]).pages('x', 'list', most=3)

    def test_a_reply_that_is_not_a_list_is_an_error(self):
        p = self.Pager([])
        p.call = lambda path: {'list': {'id': 1}}
        with self.assertRaises(ValueError):
            p.pages('x', 'list')

    def test_the_sweep_reads_every_page_of_runs(self):
        runs = [run(rid, conclusion='success') for rid in range(2, 352)] + [run(1)]   # the failed one listed last
        gh = Fake(runs, {1: [job(11, steps=died())]}, {11: [ann(LOST)]})
        code, out, _ = sweep(gh)
        self.assertEqual((code, gh.posts()), (0, ['actions/runs/1/rerun-failed-jobs']), out)
        self.assertEqual(sum(1 for _, p in gh.calls if p.startswith('actions/workflows/')), 4)

    def test_a_listing_past_the_page_limit_reruns_nothing_and_says_so(self):
        gh = Fake([run(rid, conclusion='success') for rid in range(1, 1002)])
        code, out, text = sweep(gh)
        self.assertEqual((code, gh.posts()), (1, []), out)
        self.assertIn('not read', text)


class Scope(Rerun):
    def test_untrusted_runs_are_left_alone(self):
        cases = {'a stranger': dict(actor='someone-else', by='someone-else'),
                 'started by a stranger, re-run by the owner': dict(actor='someone-else', attempt=2),
                 'triggered by a stranger': dict(by='someone-else'),
                 'a pull request from a fork': dict(event='pull_request', branch='feature',
                                                    head_repo='someone-else/t3code'),
                 'a push to another branch': dict(branch='feature'),
                 'another repository': dict(repo='someone-else/t3code'),
                 'another event': dict(event='workflow_dispatch'),
                 'another workflow': dict(workflow='other.yml')}
        for label, kw in cases.items():
            with self.subTest(label):
                gh = lost(**kw)
                code, out, _ = sweep(gh)
                self.assertLeft(gh, code, out, 'not a trusted run')
                self.assertFalse([p for _, p in gh.calls if '/jobs' in p], 'an untrusted run costs no job read')

    def test_a_pull_request_someone_else_opened_is_left_alone(self):
        gh = lost(event='pull_request', branch='feature')
        gh.pulls[7]['user'] = {'login': 'someone-else'}
        self.assertLeft(gh, *sweep(gh)[:2], 'a pull request opened by')

    def test_a_push_the_upstream_sync_made_is_left_alone(self):
        SYNCED.add(SHA)
        try:
            gh = lost()
            self.assertLeft(gh, *sweep(gh)[:2], 'upstream sync')
        finally:
            SYNCED.discard(SHA)

    def test_an_own_pull_request_is_rerun(self):
        gh = lost(event='pull_request', branch='feature')
        self.assertReran(gh, sweep(gh)[1])

    def test_a_branch_that_moved_on_or_is_gone_is_not_rerun(self):
        for heads in ({BRANCH: NEWER}, {}):
            with self.subTest(heads=heads):
                gh = lost()
                gh.heads = heads
                self.assertLeft(gh, *sweep(gh)[:2], 'has moved on or is gone')

    def test_runs_that_passed_are_still_running_or_ended_long_ago_cost_nothing(self):
        gh = Fake([run(1, conclusion='success'), run(2, status='in_progress', conclusion=None),
                   run(3, updated=T0 - CFG['catch_up_minutes'] * 60 - 1)], {}, {})
        code, out, text = sweep(gh)
        self.assertEqual(code, 0, out)
        self.assertEqual([p for _, p in gh.calls if not p.startswith(('actions/workflows/', 'issues?'))], [])
        self.assertIn('0 run(s) re-run by this pass', text)

    def test_one_run_by_id_ignores_the_catch_up_window(self):
        gh = lost(updated=T0 - 5 * 3600, created=T0 - 30 * 3600)
        self.assertReran(gh, sweep(gh, run_id=1)[1])
        self.assertFalse([p for _, p in gh.calls if p.startswith('actions/workflows/')])

    def test_a_github_error_on_one_run_does_not_stop_the_others(self):
        gh = Fake([run(1), run(2)], {2: [job(21, steps=died())]}, {21: [ann(LOST)]})
        real = gh.call
        def call(path, method='GET', data=None):
            if path.startswith('actions/runs/1/'):
                raise urllib.error.URLError('reset')
            return real(path, method, data)
        gh.call = call
        code, out, _ = sweep(gh)
        self.assertEqual(code, 1, out)
        self.assertEqual(gh.posts(), ['actions/runs/2/rerun-failed-jobs'])

    def test_a_pipe_in_a_message_keeps_the_summary_table_whole(self):
        gh = one([ann('The hosted runner: a | b lost communication with the server.')],
                 dict(steps=died(), runner='a | b'))
        _, out, text = sweep(gh)
        self.assertIn('a \\| b', text)
        row = [line for line in text.splitlines() if line.startswith('| 1 |')][0]
        self.assertEqual(row.count(' | '), 2, row)


class Config(unittest.TestCase):
    def test_the_real_config_is_valid(self):
        self.assertEqual(cr.check_config({k: v for k, v in CFG.items() if k != 'fleet'}), [])
        r = subprocess.run([sys.executable, '-I', SCRIPT, 'check'], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_github_messages_match_their_sign_as_a_whole_and_nothing_else_does(self):
        def says(kind, message):
            return bool(cr.github_says(CFG, kind, [ann(message)]))
        for m in (LOST, HOSTED_LOST, SHUTDOWN):
            self.assertTrue(says('lost', m) and not says('limit', m), m)
        for m in (JOB_LIMIT, STEP_LIMIT):
            self.assertTrue(says('limit', m) and not says('lost', m), m)
        self.assertTrue(says('cancelled_by', CANCEL_BY))
        for m in (EXIT_1, EXIT_137, CANCELED, 'Error: the test timed out after 5000 ms', 'Error: ' + LOST,
                  JOB_LIMIT + ' Also more.', 'lost communication with the server', 'x ' + CANCEL_BY,
                  'The job has exceeded the maximum execution time of 15m0s'):
            self.assertFalse(any(says(k, m) for k in cr.GROUPS), m)

    def test_bad_configs_are_refused(self):
        base = {k: v for k, v in CFG.items() if k != 'fleet'}

        def bad(change):
            cfg = copy.deepcopy(base)
            change(cfg)
            return cr.check_config(cfg)
        cases = {
            'a bad pattern': lambda c: c['signs']['lost'].append('(['),
            'a pattern that is part of a message': lambda c: c['signs']['lost'].append('lost communication'),
            'a limit pattern without its minutes': lambda c: c['signs']['limit'].append('The job ran too long'),
            'an unknown sign': lambda c: c['signs'].__setitem__('exit', ['The exit']),
            'a missing sign': lambda c: c['signs'].pop('cancelled_by'),
            'no trust file': lambda c: c.__setitem__('trust', '../x'),
            'a zero threshold': lambda c: c['stalled'].__setitem__('full_share_percent', 0),
            'a missing threshold': lambda c: c['memory_full'].pop('max_events'),
            'a string threshold': lambda c: c['memory_full'].__setitem__('oom_kills', '1'),
            'an unknown key': lambda c: c.__setitem__('retries', 2),
            'an old key': lambda c: c.__setitem__('actors', [OWNER]),
            'no skip variable': lambda c: c['skip_when'].pop('variable'),
            'no window': lambda c: c.__setitem__('catch_up_minutes', -1),
            'a listing shorter than the window': lambda c: c.__setitem__('list_hours', 0.5),
            'no hosted group': lambda c: c.__setitem__('hosted_runner_groups', []),
            'no end step': lambda c: c.__setitem__('probe_end_step', ''),
            'no start step': lambda c: c.pop('probe_start_step'),
            'one step for both': lambda c: c.__setitem__('probe_start_step', c['probe_end_step']),
            'no note slack': lambda c: c.__setitem__('note_slack_seconds', 0),
            'no annotation cap': lambda c: c.__setitem__('annotation_cap', None),
            'no probe limit': lambda c: c.pop('probe_limit_seconds'),
            'a zero probe limit': lambda c: c.__setitem__('probe_limit_seconds', 0),
            'a notice by a person': lambda c: c['notice'].__setitem__('author', OWNER),
            'a notice without a title': lambda c: c['notice'].__setitem__('title', ''),
        }
        for label, change in cases.items():
            with self.subTest(label):
                self.assertTrue(bad(change), label)
        with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as f:
            json.dump(dict(base, retries=2), f)
        try:
            r = subprocess.run([sys.executable, '-I', SCRIPT, 'check', '--config', f.name], capture_output=True,
                               text=True)
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unknown key 'retries'", r.stderr)
        finally:
            os.unlink(f.name)

    def test_the_workflow_must_be_one_the_fleet_trusts(self):
        with tempfile.TemporaryDirectory() as d:
            shutil.copy(os.path.join(ROOT, '.github', CFG['trust']), d)
            path = os.path.join(d, 'fork-ci-rerun.json')
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(dict({k: v for k, v in CFG.items() if k != 'fleet'}, workflow='other.yml'), f)
            with self.assertRaisesRegex(ValueError, 'not one the trust config trusts'):
                cr.load(path)

    def test_the_script_holds_no_fact_of_this_repository(self):
        with open(SCRIPT, encoding='utf-8') as f:
            text = f.read().replace('fork-ci-rerun', '')
        facts = [REPO, OWNER, CFG['workflow'], FOLLOWER, END_STEP, TITLE, HOSTED, CFG['trust']]
        facts += [p for ps in CFG['signs'].values() for p in ps]
        for fact in facts:
            self.assertFalse(fact in text, f'the script names {fact!r}')


class ProbeToDecider(unittest.TestCase):
    """The probe's notice, as GitHub decodes it, is the note the decider reads (one contract, both ends)."""

    def test_a_probe_notice_round_trips(self):
        n = {'seconds': 300, 'host': {'full_share': 40.0, 'full_avg300': 1.0, 'oom_kill': 0},
             'cgroups': [{'path': '/a%b\nc', 'max_mb': 4096, 'oom_kill': 2, 'max': 0, 'high': 0,
                          'full_share': 40.0}]}
        out = io.StringIO()
        body = json.dumps({'status': 'failure', **n}, separators=(',', ':'), sort_keys=True)
        print(f'::notice title={probe.NOTE_TITLE}::{probe.command(probe.NOTE_HEAD + body)}', file=out)
        line = out.getvalue().strip()
        title, _, data = line[len('::notice '):].partition('::')
        self.assertEqual(title, f'title={cr.NOTE_TITLE}')
        self.assertNotIn('\n', data)
        message = data.replace('%0A', '\n').replace('%0D', '\r').replace('%25', '%')   # what the runner does
        anns = [ann(EXIT_137), ann(message, 'notice', cr.NOTE_TITLE)]
        j = job(1, steps=ran(took=300))   # its probe steps 297 to 313 s apart: the note's 300 s fit
        got, why = cr.note_of(CFG, j, anns)
        self.assertIsNone(why)
        self.assertEqual(got['cgroups'][0]['path'], '/a%b\nc')
        self.assertEqual(cr.job_sign(CFG, j, anns)[0], 'memory full')
        self.assertIn('not its end step', cr.note_of(CFG, job(1), anns)[1])   # 300 s on a job of 57 to 73 s
        self.assertEqual((probe.NOTE_TITLE, probe.NOTE_HEAD), (cr.NOTE_TITLE, cr.NOTE_HEAD))


class Wiring(unittest.TestCase):
    def setUp(self):
        import yaml
        with open(os.path.join(ROOT, '.github/workflows/fork-ci-rerun.yml'), encoding='utf-8') as f:
            self.wf = yaml.safe_load(f)

    def test_the_workflow_runs_the_sweep_on_a_schedule_with_least_permissions(self):
        wf = self.wf
        self.assertEqual(set(wf[True]), {'schedule', 'workflow_dispatch'})
        self.assertEqual(wf['permissions'], {})
        sweep_job = wf['jobs']['sweep']
        self.assertEqual(sweep_job['permissions'], {'actions': 'write', 'checks': 'read', 'contents': 'read',
                                                    'issues': 'write'})
        self.assertFalse(wf['concurrency']['cancel-in-progress'], 'a pass is never cut off by the next')
        step = sweep_job['steps'][-1]
        self.assertIn('python3 -I .github/scripts/fork-ci-rerun.py sweep', step['run'])
        self.assertEqual(step['env']['CI_RUNNERS'], '${{ vars.CI_RUNNERS }}')
        self.assertEqual(SKIP_VAR, 'CI_RUNNERS')

    def test_the_checkout_has_every_file_the_sweep_reads(self):
        checkout = self.wf['jobs']['sweep']['steps'][0]
        # persist-credentials stays (fork-contracts.test.py, checkout): after it only this script runs, with -I.
        self.assertEqual(len(self.wf['jobs']['sweep']['steps']), 2)
        sparse = checkout['with']['sparse-checkout'].split()
        for path in ('.github/fork-ci-rerun.json', '.github/scripts/fork-ci-rerun.py', f'.github/{CFG["trust"]}',
                     '.github/scripts/fork-fleet.py'):
            self.assertIn(f'/{path}', sparse)

    def test_a_dispatch_is_never_replaced_by_a_scheduled_pass(self):
        # GitHub keeps one pending run per concurrency group: a dispatch has a group of its own.
        group = self.wf['concurrency']['group']
        self.assertEqual(group, "fork-ci-rerun-${{ github.event_name == 'workflow_dispatch' && github.run_id "
                                "|| 'schedule' }}")

    def test_the_sweep_is_offset_from_the_fleet_sweep(self):
        import yaml
        ours = self.wf[True]['schedule'][0]['cron']
        with open(os.path.join(ROOT, '.github/workflows/fork-fleet.yml'), encoding='utf-8') as f:
            fleet = yaml.safe_load(f)[True]['schedule'][0]['cron']
        self.assertEqual((ours.split()[0], fleet.split()[0]), ('5-55/10', '*/10'))


if __name__ == '__main__':
    unittest.main()
