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
PR_ENV = {'EVENT_NAME': 'pull_request', 'HEAD_SHA': SHA, 'BASE_SHA': BASE, 'OUTSIDE_PR': 'false', 'PR_NUMBER': '7',
          'DEFAULT_BRANCH': 'main', 'GITHUB_REPOSITORY': 'o/r', 'GITHUB_REF': 'refs/pull/7/merge',
          'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/fork-tests.yml@refs/pull/7/merge'}
GIT_ENV = {'GIT_CONFIG_NOSYSTEM': '1', 'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_TERMINAL_PROMPT': '0',
           'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@example.invalid',
           'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@example.invalid'}


def job(name, status='completed', conclusion='success', steps=None):
    j = {'name': name, 'status': status, 'conclusion': conclusion,
         'html_url': f'https://github.invalid/job/{abs(hash(name)) % 1000}'}
    return j if steps is None else {**j, 'steps': steps}


def step(name, status='completed', conclusion='success'):
    return {'name': name, 'status': status, 'conclusion': conclusion}


# The check job's steps (fork-checks.yml): a real check skips the copy step; a copy runs only it.
def real_steps(status='completed', conclusion='success'):
    return [step('Set up job'), step(fcr.COPY_STEP, 'completed', 'skipped'),
            step('Refuse schema and migration changes', status, conclusion)]


def copy_steps(status='completed', conclusion='success'):
    return [step('Set up job'), step(fcr.COPY_STEP, status, conclusion)]


REPO_ID, OTHER_ID = 101, 202   # GitHub's ids of this repository (o/r) and of another one


def pr(number, ref='feature', repo_id=REPO_ID):
    """A pull_requests entry as GitHub gives it: its head repo has an id, a short name and an API url, no full_name."""
    return {'id': 5000 + number, 'number': number, 'url': f'https://api.github.invalid/repos/o/r/pulls/{number}',
            'head': {'ref': ref, 'sha': SHA, 'repo': {'id': repo_id, 'name': 'r',
                                                      'url': 'https://api.github.invalid/repos/o/r'}},
            'base': {'ref': 'main', 'sha': BASE, 'repo': {'id': REPO_ID, 'name': 'r',
                                                         'url': 'https://api.github.invalid/repos/o/r'}}}


def wr(rid, jobs, path=WF, event='pull_request_target', status='completed', prs=(7,), head_repo='o/r', branch='feature',
       conclusion=None):
    """A run of fork-checks.yml on SHA; by default one of pull request #7 (PR_ENV) from this repository's branch
    `feature`. prs: numbers (entries from that branch) or whole entries. conclusion: the run's own."""
    return {'id': rid, 'path': path, 'event': event, 'status': status, 'conclusion': conclusion, 'jobs': jobs,
            'head_branch': branch,
            'pull_requests': [p if isinstance(p, dict) else pr(p, branch) for p in prs],
            'head_repository': {'full_name': head_repo, 'id': REPO_ID if head_repo == 'o/r' else OTHER_ID}}


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

    def __init__(self, polls, errors=(), pulls=None):
        self.polls, self.errors, self.paths = list(polls), list(errors), []
        self.current = []
        self.pulls = pulls          # the open pull requests a head run looks up (pulls?head=OWNER:BRANCH)

    def get(self, path):
        self.paths.append(path)
        if self.errors:
            e = self.errors.pop(0)
            if e is not None:
                raise e
        url = urllib.parse.urlsplit(path)
        q = urllib.parse.parse_qs(url.query)
        if url.path == 'pulls' and self.pulls is not None:
            assert q['state'] == ['open'] and q['head'] == ['o:feature'], path
            return self.pulls
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

    # --- a head run (#1157): a dispatch from the head-runs reconciler, judged as its branch's pull request's run -------
    HEAD_ENV = {'EVENT_NAME': 'workflow_dispatch', 'HEAD_RUN': 'true', 'GITHUB_REF': 'refs/heads/feature',
                'GITHUB_SHA': SHA, 'HEAD_SHA': '', 'BASE_SHA': '', 'PR_NUMBER': '', 'OUTSIDE_PR': 'false',
                'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/fork-tests.yml@refs/heads/feature'}

    @staticmethod
    def open_pr(number=7, repo='o/r', ref='feature'):
        return {'number': number, 'head': {'ref': ref, 'sha': SHA, 'repo': {'full_name': repo}},
                'base': {'ref': 'main', 'sha': BASE, 'repo': {'full_name': 'o/r'}}}

    def test_a_head_run_is_judged_as_its_pull_requests_run(self):
        fake = Fake([[wr(1, [job(CFG['check'], steps=real_steps())])]], pulls=[self.open_pr()])
        code, out, _ = check(fake, **self.HEAD_ENV)
        self.assertEqual(code, 0, out)
        self.assertIn("judged as pull request #7's run", out)
        self.assertTrue(fake.paths[0].startswith('pulls?'))

    def test_a_head_run_whose_fork_checks_failed_fails(self):
        fake = Fake([[wr(1, [job(CFG['check'], conclusion='failure', steps=real_steps(conclusion='failure'))])]],
                    pulls=[self.open_pr()])
        self.assertEqual(check(fake, **self.HEAD_ENV)[0], 1)

    def test_a_head_run_without_exactly_one_open_pull_request_fails(self):
        for pulls in ([], [self.open_pr(7), self.open_pr(8)], [self.open_pr(repo='x/r')],
                      [self.open_pr(ref='other')], {'message': 'odd'}):
            with self.subTest(pulls=pulls):
                fake = Fake([[]], pulls=pulls)
                code, out, _ = check(fake, **self.HEAD_ENV)
                self.assertEqual(code, 1, out)
                self.assertIn('#430', out)
                self.assertEqual(len(fake.paths), 1)            # the lookup only: no fork-checks run is read

    def test_a_head_run_whose_lookup_fails_fails_closed(self):
        fake = Fake([[]], errors=[fcr.Transient('HTTP 502 on pulls')], pulls=[])
        code, out, _ = check(fake, **self.HEAD_ENV)
        self.assertEqual(code, 1)
        self.assertIn('failing closed', out)

    def test_a_head_run_on_the_default_branch_needs_no_check(self):
        fake = Fake([[]], pulls=[])
        code, out, _ = check(fake, **{**self.HEAD_ENV, 'GITHUB_REF': 'refs/heads/main',
                                      'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/fork-tests.yml@refs/heads/main'})
        self.assertEqual(code, 0, out)
        self.assertEqual(fake.paths, [])

    def test_head_run_from_another_workflow_takes_the_direct_run_path(self):
        # HEAD_RUN=true only counts in fork-tests.yml's own run: another workflow's dispatch fails as before (#430)
        fake = Fake([[]], pulls=[self.open_pr()])
        code, out, _ = check(fake, **{**self.HEAD_ENV,
                                      'GITHUB_WORKFLOW_REF': 'o/r/.github/workflows/other.yml@refs/heads/feature'})
        self.assertEqual(code, 0, out)                      # a called or other workflow's run: nothing to check
        self.assertEqual(fake.paths, [])
        code, out, _ = check(fake, **{**self.HEAD_ENV,
                                      'GITHUB_WORKFLOW_REF': 'x/r/.github/workflows/fork-tests.yml@refs/heads/feature'})
        self.assertEqual(code, 1, out)
        self.assertEqual(fake.paths, [])

    def test_a_dispatch_on_a_branch_that_is_no_head_run_still_fails(self):
        fake = Fake([[]], pulls=[self.open_pr()])
        code, out, _ = check(fake, **{**self.HEAD_ENV, 'HEAD_RUN': 'false'})
        self.assertEqual(code, 1, out)
        self.assertEqual(fake.paths, [])

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
        # A re-run replays the same event, so it skips again: the message must not offer one (#433 fix round 1).
        self.assertNotIn('re-run it', out)
        self.assertIn('a re-run skips it again', out)
        self.assertIn('push again', out)

    def test_newest_run_decides(self):
        # An `edited` event runs fork-checks again on the same head: the newest run is the one that counts.
        for runs, want in (([wr(9, [job(CFG['check'], 'completed', 'failure')]), wr(3, [job(CFG['check'])])], 1),
                           ([wr(3, [job(CFG['check'], 'completed', 'failure')]), wr(9, [job(CFG['check'])])], 0),
                           ([wr(3, [job(CFG['check'])]), wr(9, [job(CFG['check'], 'completed', 'cancelled')])], 1)):
            code, out, _ = check(Fake([runs]))
            self.assertEqual(code, want, f'{[r["id"] for r in runs]}: {out}')

    def test_a_newer_run_that_skipped_every_job_is_passed_over(self):
        # A title or body edit (`edited`, base unchanged) skips the check, so that run ends "skipped" (#1090): the
        # newest run that checked decides, red or green, and the skipped run's jobs are never asked for.
        def edit(rid):
            return wr(rid, [job(CFG['check'], 'completed', 'skipped'), job(CFG['outside_check'], 'completed', 'skipped')],
                      conclusion='skipped')
        for older, want in ((wr(3, [job(CFG['check'], 'completed', 'failure')], conclusion='failure'), 1),
                            (wr(3, [job(CFG['check'])], conclusion='success'), 0),
                            (wr(3, [job(CFG['check'], 'completed', 'cancelled')], conclusion='cancelled'), 1)):
            fake = Fake([[edit(9), edit(8), older]])
            code, out, _ = check(fake)
            self.assertEqual(code, want, out)
            self.assertNotIn('skipped (', out)
            asked = ' '.join(fake.paths)
            self.assertIn('actions/runs/3/jobs', asked)
            self.assertNotIn('actions/runs/9/jobs', asked)
            self.assertNotIn('actions/runs/8/jobs', asked)
            self.assertLessEqual(len(fake.paths), fcr.API_CALLS_PER_POLL)

    def test_a_newer_skipped_run_waits_for_an_older_run_still_going(self):
        going = wr(3, [job(CFG['check'], 'in_progress', None)], status='in_progress')
        code, out, clock = check(Fake([[wr(9, [job(CFG['check'], 'completed', 'skipped')], conclusion='skipped'), going],
                                       [wr(9, [job(CFG['check'], 'completed', 'skipped')], conclusion='skipped'),
                                        wr(3, [job(CFG['check'])], conclusion='success')]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(clock.slept), 1)

    def test_when_every_run_skipped_every_job_the_newest_decides_and_fails(self):
        skipped = [wr(rid, [job(CFG['check'], 'completed', 'skipped')], conclusion='skipped') for rid in (9, 3)]
        fake = Fake([skipped])
        code, out, clock = check(fake)
        self.assertEqual(code, 1, out)
        self.assertIn('was skipped', out)
        self.assertIn('actions/runs/9/jobs', ' '.join(fake.paths))
        self.assertEqual(clock.slept, [])

    def test_a_run_that_is_still_going_or_ended_otherwise_is_not_passed_over(self):
        # Only a finished run whose own conclusion is "skipped" is passed over; a newer run in any other state decides.
        for newer in (wr(9, [job(CFG['check'], 'completed', 'failure')], conclusion='failure'),
                      wr(9, [job(CFG['check'], 'completed', 'cancelled')], conclusion='cancelled'),
                      wr(9, [], status='completed', conclusion='failure')):
            code, out, _ = check(Fake([[newer, wr(3, [job(CFG['check'])], conclusion='success')]]))
            self.assertEqual(code, 1, out)

    def test_a_skipped_check_in_a_run_not_yet_finished_waits(self):
        # The check is marked skipped before its run is marked completed: still going, not a failure (the run may yet
        # end "skipped" and be passed over).
        older = wr(3, [job(CFG['check'])], conclusion='success')
        code, out, clock = check(Fake([
            [wr(9, [job(CFG['check'], 'completed', 'skipped')], status='in_progress'), older],
            [wr(9, [job(CFG['check'], 'completed', 'skipped')], conclusion='skipped'), older]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(clock.slept), 1)

    def test_a_copy_decides_like_a_real_check(self):
        # fork-ci-result reads the newest run, a copy too: it ended as the real check did.
        for conclusion, want in (('success', 0), ('failure', 1)):
            code, out, _ = check(Fake([[wr(9, [job(CFG['check'], 'completed', conclusion, copy_steps('completed',
                                                                                                    conclusion))]),
                                        wr(3, [job(CFG['check'], steps=real_steps())])]]))
            self.assertEqual(code, want, out)

    def test_reads_the_latest_attempt_only(self):
        fake = Fake([[wr(1, [job(CFG['check'])])]])
        self.assertEqual(check(fake)[0], 0)
        self.assertTrue(any('filter=latest' in p for p in fake.paths), fake.paths)

    # --- which runs count -------------------------------------------------------------------------------------------
    def test_only_pull_request_target_runs_count(self):
        # A pull request that adds push: or pull_request: to its own fork-checks.yml gets a same-named job from its
        # own copy (#430 item 2). Only pull_request_target runs the base branch's copy.
        for event in ('pull_request', 'push', 'workflow_dispatch', 'schedule', None):
            fake = Fake([[wr(4, [job(CFG['check'])], event=event)]])
            code, out, _ = check(fake)
            self.assertEqual(code, 1, f'{event}: {out}')
            self.assertNotIn('actions/runs/4/jobs', ' '.join(fake.paths), event)

    # --- only this pull request's runs count (#433 fix round 1, finding 1 of both lenses) --------------------------
    def test_a_newer_passing_run_of_another_pull_request_on_the_same_head_does_not_pass(self):
        # A second pull request at the same commit, against a base branch whose fork-checks.yml is a no-op.
        for runs in ([wr(3, [job(CFG['check'], 'completed', 'failure')]), wr(9, [job(CFG['check'])], prs=(11,))],
                     [wr(9, [job(CFG['check'])], prs=(11,))]):
            fake = Fake([runs])
            code, out, _ = check(fake)
            self.assertEqual(code, 1, out)
            self.assertNotIn('actions/runs/9/jobs', ' '.join(fake.paths))

    def test_a_newer_skipped_run_of_an_outside_pull_request_on_the_same_head_does_not_turn_it_red(self):
        # Anyone can open a pull request from their own fork at a public commit; its run skips the job.
        for outsider in (wr(9, [job(CFG['check'], 'completed', 'skipped')], prs=(), head_repo='x/r'),
                         wr(9, [job(CFG['check'], 'completed', 'skipped')], prs=(12,), head_repo='x/r'),
                         wr(9, [job(CFG['check'], 'completed', 'skipped')], prs=(7,), head_repo='x/r')):
            fake = Fake([[outsider, wr(3, [job(CFG['check'])])]])
            code, out, _ = check(fake)
            self.assertEqual(code, 0, out)
            self.assertNotIn('actions/runs/9/jobs', ' '.join(fake.paths))

    def test_a_run_with_an_empty_or_missing_pull_request_list_never_passes(self):
        # GitHub leaves pull_requests empty on runs of a pull request from another repository: such a run cannot be
        # tied to this pull request, so it does not count, and with no other run the job fails at the cap.
        bare = wr(5, [job(CFG['check'])], prs=())
        for run in (bare, {**bare, 'pull_requests': None}, {k: v for k, v in bare.items() if k != 'pull_requests'},
                    {**bare, 'head_repository': None}, {k: v for k, v in bare.items() if k != 'head_repository'},
                    {**bare, 'pull_requests': [{'number': '7'}]}, {**bare, 'pull_requests': [{}]}):
            fake = Fake([[run]])
            code, out, clock = check(fake)
            self.assertEqual(code, 1, (run, out))
            self.assertIn('fork-checks did not run', out)
            self.assertNotIn('actions/runs/5/jobs', ' '.join(fake.paths))

    # --- GitHub lists a pull request on runs whose head SHA OR head branch matches it (#443) ------------------------
    def test_a_second_pull_request_from_another_branch_at_the_same_commit_does_not_count(self):
        # PR #11 from branch `other` at the same commit, against a base whose fork-checks.yml is a no-op: GitHub may
        # list this PR (#7, branch `feature`) on #11's run too. Only the entry from the run's own branch counts.
        other = wr(9, [job(CFG['check'])], branch='other', prs=(pr(7, 'feature'), pr(11, 'other')))
        for runs in ([wr(3, [job(CFG['check'], 'completed', 'failure')]), other], [other]):
            fake = Fake([runs])
            code, out, _ = check(fake)
            self.assertEqual(code, 1, out)
            self.assertNotIn('actions/runs/9/jobs', ' '.join(fake.paths))
        # ... while this PR's own run, which lists #11 too, still counts for #7.
        own = wr(3, [job(CFG['check'])], prs=(pr(11, 'other'), pr(7, 'feature')))
        self.assertEqual(check(Fake([[other, own]]))[0], 0)

    def test_an_entry_whose_head_is_another_repository_does_not_count(self):
        code, out, _ = check(Fake([[wr(5, [job(CFG['check'])], prs=(pr(7, 'feature', OTHER_ID),))]]))
        self.assertEqual(code, 1, out)
        self.assertIn('fork-checks did not run', out)

    def test_a_run_whose_head_repository_has_another_name_does_not_count(self):
        # The id match alone already ties the entry to the run; the run's head repository must also be this one by
        # name (GITHUB_REPOSITORY), kept as defence in depth (#449). A foreign name with this repository's id fails.
        run = {**wr(5, [job(CFG['check'])]), 'head_repository': {'full_name': 'x/r', 'id': REPO_ID}}
        fake = Fake([[run]])
        code, out, _ = check(fake)
        self.assertEqual(code, 1, out)
        self.assertIn('fork-checks did not run', out)
        self.assertNotIn('actions/runs/5/jobs', ' '.join(fake.paths))

    def test_missing_head_fields_fail_closed(self):
        # Without the head branch and repository on both sides the entry cannot be tied to this run: never counted.
        entry, run = pr(7), wr(5, [job(CFG['check'])])
        bad_entries = [{k: v for k, v in entry.items() if k != 'head'}, {**entry, 'head': None}, {**entry, 'head': 'feature'},
                       {**entry, 'head': {**entry['head'], 'ref': None}}, {**entry, 'head': {**entry['head'], 'ref': ''}},
                       {**entry, 'head': {k: v for k, v in entry['head'].items() if k != 'ref'}},
                       {**entry, 'head': {k: v for k, v in entry['head'].items() if k != 'repo'}},
                       {**entry, 'head': {**entry['head'], 'repo': None}},
                       {**entry, 'head': {**entry['head'], 'repo': {'name': 'r'}}},
                       {**entry, 'head': {**entry['head'], 'repo': {'id': str(REPO_ID)}}},
                       {**entry, 'head': {**entry['head'], 'repo': {'id': True}}}]
        bad_runs = [{**run, 'pull_requests': [e]} for e in bad_entries]
        bad_runs += [{k: v for k, v in run.items() if k != 'head_branch'}, {**run, 'head_branch': None},
                     {**run, 'head_branch': ''}, {**run, 'head_repository': {'full_name': 'o/r'}},
                     {**run, 'head_repository': {'full_name': 'o/r', 'id': None}}]
        # Both sides missing must not match each other either (None == None).
        bad_runs += [{**run, 'head_branch': None, 'pull_requests': [{**entry, 'head': {**entry['head'], 'ref': None}}]},
                     {**run, 'head_branch': '', 'pull_requests': [{**entry, 'head': {**entry['head'], 'ref': ''}}]},
                     {**run, 'head_repository': {'full_name': 'o/r', 'id': True},
                      'pull_requests': [{**entry, 'head': {**entry['head'], 'repo': {'id': True}}}]},
                     {**run, 'head_repository': {'full_name': 'o/r'},
                      'pull_requests': [{**entry, 'head': {**entry['head'], 'repo': {'name': 'r'}}}]}]
        for bad in bad_runs:
            fake = Fake([[bad]])
            code, out, _ = check(fake)
            self.assertEqual(code, 1, (bad, out))
            self.assertIn('fork-checks did not run', out)
            self.assertNotIn('actions/runs/5/jobs', ' '.join(fake.paths), bad)
        self.assertEqual(check(Fake([[run]]))[0], 0)   # the same run, whole, passes

    def test_no_run_of_this_pull_request_fails_with_a_clear_message(self):
        code, out, _ = check(Fake([[wr(9, [job(CFG['check'])], prs=(11,))]]))
        self.assertEqual(code, 1, out)
        self.assertIn('for pull request #7 from this repository', out)
        self.assertIn('runs of other pull requests on the same commit do not count', out)
        self.assertIn('close and reopen', out)

    def test_one_poll_makes_at_most_the_budgeted_api_calls(self):
        # The contract's timeout budget counts API_CALLS_PER_POLL calls after the cap (fork-contracts.test.py).
        runs = [wr(20 - i, [job(CFG['check'])], prs=(11,)) for i in range(5)] + [wr(3, [job(CFG['check'])])]
        fake = Fake([runs])
        self.assertEqual(check(fake)[0], 0)
        self.assertLessEqual(len(fake.paths), fcr.API_CALLS_PER_POLL)

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
                          ({'BASE_SHA': 'b' * 39}, 'BASE_SHA'), ({'OUTSIDE_PR': 'maybe'}, 'OUTSIDE_PR'),
                          ({'PR_NUMBER': ''}, 'PR_NUMBER'), ({'PR_NUMBER': '7a'}, 'PR_NUMBER'),
                          ({'PR_NUMBER': '0'}, 'PR_NUMBER'), ({'PR_NUMBER': '07'}, 'PR_NUMBER'),
                          ({'GITHUB_REPOSITORY': ''}, 'GITHUB_REPOSITORY')):
            code, out, _ = check(passing(), **env)
            self.assertEqual((code, what in out), (1, True), (env, out))
        self.assertEqual(check(passing(), cfg={**CFG, 'wait_seconds': 0})[0], 1)
        self.assertEqual(check(passing(), cfg={k: v for k, v in CFG.items() if k != 'check'})[0], 1)

    def test_a_true_or_false_wait_is_not_a_number(self):
        # bool is an int in Python: `wait_seconds: true` would be a 1 s cap (#433 fix round 1, state finding 5).
        for k in ('wait_seconds', 'poll_seconds'):
            for v in (True, False):
                self.assertEqual(fcr.config_problems({**CFG, k: v}), [f'fork_checks.{k} is not a positive whole number'])
                code, out, _ = check(Fake([[wr(1, [job(CFG['check'])])]]), cfg={**CFG, k: v})
                self.assertEqual(code, 1, (k, v, out))

    # --- time: the cap covers reading the config (#433 fix round 1, nit of both lenses) --------------------------------
    def test_the_cap_counts_from_before_the_config_is_read(self):
        clock, out = Clock(), []

        def slow_config(base):
            clock.t += 590   # a slow git fetch of the base commit
            return dict(CFG)
        code = fcr.check(PR_ENV, Fake([[]]), slow_config, clock.now, clock.sleep, out.append)
        self.assertEqual(code, 1, out)
        self.assertLessEqual(clock.t - 1000.0, CFG['wait_seconds'], out)

    def test_api_calls_use_the_budgeted_timeout(self):
        seen = []

        def urlopen(req, timeout=None):
            seen.append(timeout)
            raise fcr.urllib.error.URLError('down')
        real, fcr.urllib.request.urlopen = fcr.urllib.request.urlopen, urlopen
        try:
            with self.assertRaises(fcr.Transient):
                fcr.GitHub('t', 'o/r', 'http://127.0.0.1:9').get('x')
        finally:
            fcr.urllib.request.urlopen = real
        self.assertEqual(seen, [fcr.API_TIMEOUT])

    def test_real_config_has_the_fields(self):
        with open(CONFIG) as f:
            cfg = json.load(f)['fork_checks']
        self.assertEqual(fcr.config_problems(cfg), [])



COPY_ENV = {'HEAD_SHA': SHA, 'DEFAULT_SHA': BASE, 'PR_NUMBER': '7', 'GITHUB_RUN_ID': '20', 'GITHUB_REPOSITORY': 'o/r'}


def copy_check(fake, cfg=CFG, **env):
    clock, out, asked = Clock(), [], []

    def config(sha):
        asked.append(sha)
        if isinstance(cfg, Exception):
            raise cfg
        return dict(cfg)
    code = fcr.copy({**COPY_ENV, **env}, fake, config, clock.now, clock.sleep, out.append)
    return code, '\n'.join(out), clock, asked


def real(rid, status='completed', conclusion='success'):
    return wr(rid, [job(CFG['check'], status, conclusion if status == 'completed' else None,
                        real_steps(status, conclusion if status == 'completed' else None))],
              status=status, conclusion=conclusion if status == 'completed' else None)


def copied(rid, status='completed', conclusion='success'):
    return wr(rid, [job(CFG['check'], status, conclusion if status == 'completed' else None,
                        copy_steps(status, conclusion if status == 'completed' else None))],
              status=status, conclusion=conclusion if status == 'completed' else None)


class Copy(unittest.TestCase):
    """--copy: fork-checks.yml's check job on a title or body edit ends as the newest earlier real check (#1134)."""

    def test_copies_the_newest_earlier_real_check(self):
        for older, want in ((real(3), 0), (real(3, conclusion='failure'), 1), (real(3, conclusion='cancelled'), 1)):
            code, out, clock, asked = copy_check(Fake([[older]]))
            self.assertEqual(code, want, out)
            self.assertEqual(clock.slept, [])
            self.assertEqual(asked, [BASE])   # the config of DEFAULT_SHA

    def test_a_base_change_that_went_red_stays_red_after_a_body_edit(self):
        # The #1134 blocker: passed against the side branch (3), failed against main after the retarget (5); a body
        # edit (this run, 20) must not read as passed.
        code, out, _, _ = copy_check(Fake([[real(5, conclusion='failure'), real(3)]]))
        self.assertEqual(code, 1, out)
        self.assertIn('ended failure', out)

    def test_this_run_and_later_runs_are_not_read(self):
        fake = Fake([[real(25, conclusion='failure'), copied(20), real(3)]])
        code, out, _, _ = copy_check(fake)
        self.assertEqual(code, 0, out)
        asked = ' '.join(fake.paths)
        self.assertNotIn('actions/runs/25/', asked)
        self.assertNotIn('actions/runs/20/', asked)

    def test_earlier_copies_are_passed_over(self):
        # A copy copies a real check, never another copy (a copy that timed out must not stand for the real check).
        for copies, older, want in (([copied(9, conclusion='failure')], real(3), 0),
                                    ([copied(9), copied(8)], real(3, conclusion='failure'), 1),
                                    ([copied(9, 'in_progress')], real(3), 0)):
            code, out, _, _ = copy_check(Fake([[*copies, older]]))
            self.assertEqual(code, want, out)

    def test_waits_for_the_real_check_still_running(self):
        code, out, clock, _ = copy_check(Fake([[copied(9, 'in_progress'), real(3, 'in_progress')], [real(3)]]))
        self.assertEqual(code, 0, out)
        self.assertEqual(clock.slept, [CFG['poll_seconds']])

    def test_a_job_that_cannot_be_told_apart_waits_while_it_runs_and_fails_once_ended(self):
        # Its copy step still queued (a real job lists it queued too), or no steps yet: never passed over.
        queued = wr(9, [job(CFG['check'], 'in_progress', None,
                            [step('Set up job'), step(fcr.COPY_STEP, 'queued', None)])], status='in_progress')
        code, out, clock, _ = copy_check(Fake([[queued, real(3)], [real(9, conclusion='failure'), real(3)]]))
        self.assertEqual((code, len(clock.slept)), (1, 1), out)
        for ended in (wr(9, [job(CFG['check'], 'completed', 'success', [])]), wr(9, [job(CFG['check'])])):
            code, out, _, _ = copy_check(Fake([[ended, real(3)]]))
            self.assertEqual(code, 1, out)
            self.assertIn('not clearly a real check', out)

    def test_a_real_check_of_an_older_fork_checks_yml_is_copied(self):
        # Runs made before this change have no copy step: a job with steps but none of that name is real.
        old = wr(3, [job(CFG['check'], steps=[step('Set up job'), step('Refuse schema and migration changes')])])
        code, out, _, _ = copy_check(Fake([[old]]))
        self.assertEqual(code, 0, out)

    def test_nothing_to_copy_fails_at_once(self):
        for runs in ([], [copied(9)], [wr(4, [job(CFG['check'], steps=real_steps())], prs=(8,))],
                     [wr(3, [job(CFG['check'], steps=real_steps())], event='pull_request')]):
            code, out, clock, _ = copy_check(Fake([runs]))
            self.assertEqual((code, clock.slept), (1, []), out)
            self.assertIn('nothing to copy', out)

    def test_a_finished_run_without_the_check_fails(self):
        code, out, _, _ = copy_check(Fake([[wr(9, [], conclusion='failure'), real(3)]]))
        self.assertEqual(code, 1, out)

    def test_never_finishing_fails_after_the_cap(self):
        code, out, clock, _ = copy_check(Fake([[real(3, 'in_progress')]]))
        self.assertEqual(code, 1, out)
        self.assertIn('did not finish', out)
        self.assertLessEqual(sum(clock.slept), fcr.COPY_WAIT_SECONDS)

    def test_api_errors_fail_closed(self):
        code, out, _, _ = copy_check(Fake([[real(3)]], errors=[fcr.Fatal('HTTP 403 on x')]))
        self.assertEqual(code, 1, out)
        code, out, clock, _ = copy_check(Fake([[real(3)]], errors=[fcr.Transient('HTTP 502 on x'), None]))
        self.assertEqual((code, len(clock.slept)), (0, 1), out)
        fake = Fake([[real(3)]], errors=[fcr.Transient('HTTP 502 on x')] * 1000)
        code, out, _, _ = copy_check(fake)
        self.assertEqual(code, 1, out)
        self.assertIn('HTTP 502', out)

    def test_bad_input_or_config_fails(self):
        for env in ({'HEAD_SHA': 'x'}, {'DEFAULT_SHA': ''}, {'PR_NUMBER': ''}, {'GITHUB_RUN_ID': 'x'},
                    {'GITHUB_REPOSITORY': ''}):
            code, out, _, _ = copy_check(Fake([[real(3)]]), **env)
            self.assertEqual(code, 1, (env, out))
        code, out, _, _ = copy_check(Fake([[real(3)]]), cfg=fcr.ConfigError('no file'))
        self.assertEqual(code, 1, out)
        code, out, _, _ = copy_check(Fake([[real(3)]]), cfg={**CFG, 'poll_seconds': 0})
        self.assertEqual(code, 1, out)


class BaseConfig(unittest.TestCase):
    """The config comes from the pull request's base commit (#430 item 1): fork-checks runs the base branch's copy,
    so a pull request that renames its job or file must still be judged by the names that copy produces."""

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

    def test_git_calls_stay_within_the_budget(self):
        # The contract's timeout budget counts GIT_CALLS calls of GIT_TIMEOUT s each (fork-contracts.test.py).
        origin = os.path.join(self.tmp, 'origin')
        base, head = make_repo(origin, [{fcr.CONFIG_PATH: policies()}, {fcr.CONFIG_PATH: policies(check='Schema')}])
        git(origin, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        git(origin, 'config', 'uploadpack.allowFilter', 'true')
        work = os.path.join(self.tmp, 'work')
        os.makedirs(work)
        git(work, 'init', '-q')
        git(work, 'remote', 'add', 'origin', 'file://' + origin)
        git(work, 'fetch', '-q', '--depth=1', 'origin', head)
        timeouts, real = [], fcr.subprocess.run

        def run(*a, **kw):
            timeouts.append(kw.get('timeout'))
            return real(*a, **kw)
        fcr.subprocess.run = run
        try:
            self.assertEqual(self.load(work, base)['check'], CFG['check'])
        finally:
            fcr.subprocess.run = real
        self.assertEqual(timeouts, [fcr.GIT_TIMEOUT] * fcr.GIT_CALLS)

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
