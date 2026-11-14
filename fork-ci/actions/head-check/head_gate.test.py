"""Tests of head_gate.py (run: python3 fork-ci/actions/head-check/head_gate.test.py).

The gate judges a pull request with head_check's rules on a bare repository it fetched the base tip and the head into,
and posts one completed check run with its App's token. For each step: what passes, what fails or is refused, and what
a re-run, a stop half-way and old or empty state do. The rules themselves are head_check.test.py's (it runs every rule
test in bare mode too); here: the guard, the list of pull requests, the judge around the rules, the post and the drift
warning.

Real commits in head_check.test.py's World (a local bare repository stands in for GitHub); `gh` is a fake. The guard
that fails the run if anything starts the real `gh` is head_check.test.py's, loaded first.
"""
import importlib.util, os, sys

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('head_check_test', os.path.join(HERE, 'head_check.test.py'))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)   # first: its guard puts a fake gh on PATH, drops tokens and isolates git

import argparse, json, shutil, tempfile, types, unittest  # noqa: E402

sys.path.insert(0, HERE)
import head_check as H  # noqa: E402
import head_gate as G  # noqa: E402

REPO, ENV_NAME, KEY = T.REPO, 'head-gate', 'HEAD_GATE_APP_KEY'
CHECK = 'review-check'


def answer(code=0, out='', err=''):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


def not_found():
    return answer(1, '', 'gh: Not Found (HTTP 404)')


class Api:
    """gh that answers `gh api PATH` from a dict {path: answer or JSON-able data} and records every call. Any other
    call goes to `rest` (head_check.test.py's fake for pr view and the reviews query), or fails the test."""
    def __init__(self, routes=None, rest=None):
        self.routes, self.rest, self.calls = dict(routes or {}), rest, []

    def __call__(self, *a, stdin=None):
        self.calls.append((a, stdin))
        if a[0] == 'api' and len(a) == 2 and a[1] in self.routes:
            v = self.routes[a[1]]
            return v if isinstance(v, types.SimpleNamespace) else answer(0, json.dumps(v))
        if self.rest:
            return self.rest(*a)
        raise AssertionError(f'unexpected gh call: {a}')

    def paths(self):
        return [a[1] for a, _ in self.calls if a[0] == 'api']


class Files:
    """GITHUB_OUTPUT and GITHUB_STEP_SUMMARY in a temporary folder."""
    def setUp(self):
        self.files = tempfile.mkdtemp(prefix='head-gate-files-')
        self.addCleanup(shutil.rmtree, self.files, True)
        self.env = {'GITHUB_OUTPUT': os.path.join(self.files, 'output'),
                    'GITHUB_STEP_SUMMARY': os.path.join(self.files, 'summary.md')}

    def file(self, name):
        path = os.path.join(self.files, name)
        if not os.path.exists(path):
            return None
        with open(path, encoding='utf-8') as f:
            return f.read()

    def outputs(self):
        return dict(line.split('=', 1) for line in (self.file('output') or '').splitlines())


# --- guard ----------------------------------------------------------------------------------------------------------
class Guard(Files, unittest.TestCase):
    ENV_PATH = f'repos/{REPO}/environments/{ENV_NAME}'

    def routes(self, policy='default', rules='default', env=None, repo=None):
        if policy == 'default':
            policy = {'protected_branches': False, 'custom_branch_policies': True}
        if rules == 'default':
            rules = [{'name': 'main', 'type': 'branch'}]
        return {f'repos/{REPO}': {'default_branch': 'main'} if repo is None else repo,
                self.ENV_PATH: {'name': ENV_NAME, 'deployment_branch_policy': policy} if env is None else env,
                self.ENV_PATH + '/deployment-branch-policies?per_page=100':
                    rules if isinstance(rules, types.SimpleNamespace) else
                    {'total_count': len(rules), 'branch_policies': rules}}

    def guard(self, secret='false', environment_only=False, **kw):
        gh, out = Api(self.routes(**kw)), []
        code = G.guard(REPO, ENV_NAME, KEY, secret, environment_only, gh, out.append, self.env)
        return code, '\n'.join(out), gh

    def test_only_the_default_branch_and_no_repository_secret_passes(self):
        code, out, _ = self.guard()
        self.assertEqual(code, 0, out)
        self.assertIn('Only main may deploy to head-gate', out)

    def test_a_repository_or_organisation_secret_of_the_keys_name_is_refused(self):   # any branch's workflow reads it
        code, out, gh = self.guard(secret='true')
        self.assertEqual((code, gh.calls), (1, []), out)
        self.assertIn(f'secret named {KEY} exists', out)
        self.assertIn('Delete it', self.file('summary.md'))

    def test_an_unknown_secret_answer_is_refused(self):   # the workflow did not pass it: never read as "none"
        for secret in ('', 'True', 'false\n::warning::x'):
            with self.subTest(secret=secret):
                code, out, gh = self.guard(secret=secret)
                self.assertEqual((code, gh.calls), (1, []), out)
                self.assertIn('could not tell whether a repository secret', out)
                self.assertTrue(all(not line.startswith('::') or line.startswith('::error::head gate')
                                    for line in out.splitlines()), out)

    def test_the_environment_part_alone_skips_the_secret_answer(self):   # the gate job has no such input
        code, out, _ = self.guard(secret='', environment_only=True)
        self.assertEqual(code, 0, out)

    def test_a_missing_environment_is_refused_with_the_steps(self):
        code, out, _ = self.guard(env=not_found())
        self.assertEqual(code, 1, out)
        self.assertIn('does not exist', out)
        self.assertIn('HEAD_GATE_APP_ID', out)

    def test_an_environment_any_branch_may_use_is_refused(self):   # no deployment branch rule at all
        code, out, _ = self.guard(policy=None)
        self.assertEqual(code, 1, out)
        self.assertIn('Any branch may deploy', out)

    def test_protected_branches_or_an_unknown_policy_is_refused(self):   # cannot be proved to mean only main
        for policy in ({'protected_branches': True, 'custom_branch_policies': False},
                       {'protected_branches': True, 'custom_branch_policies': True},   # an answer GitHub never gives
                       {'protected_branches': False, 'custom_branch_policies': False}, {}, 'x'):
            with self.subTest(policy=policy):
                code, out, _ = self.guard(policy=policy)
                self.assertEqual(code, 1, out)
                self.assertIn('does not know', out)

    def test_any_rule_but_exactly_the_default_branch_is_refused(self):
        main = {'name': 'main', 'type': 'branch'}
        for rules in ([], [main, {'name': 'release', 'type': 'branch'}], [{'name': 'main', 'type': 'tag'}],
                      [{'name': 'ma*', 'type': 'branch'}], [{'name': 'other', 'type': 'branch'}]):
            with self.subTest(rules=rules):
                code, out, _ = self.guard(rules=rules)
                self.assertEqual(code, 1, out)
                self.assertIn('must let only the branch main deploy', out)
        routes = self.routes()   # one rule listed, but GitHub counts more: a second page
        routes[self.ENV_PATH + '/deployment-branch-policies?per_page=100']['total_count'] = 2
        gh, out = Api(routes), []
        self.assertEqual(G.guard(REPO, ENV_NAME, KEY, 'false', False, gh, out.append, self.env), 1)

    def test_a_rule_without_a_type_is_a_branch_rule(self):   # older API answers have no type
        code, out, _ = self.guard(rules=[{'name': 'main'}])
        self.assertEqual(code, 0, out)

    def test_a_default_branch_that_looks_like_a_pattern_is_refused(self):
        code, out, _ = self.guard(repo={'default_branch': 'm[a]in'}, rules=[{'name': 'm[a]in', 'type': 'branch'}])
        self.assertEqual(code, 1, out)

    def test_unreadable_answers_are_refused(self):   # unknown never makes a token
        for kw in ({'repo': {}}, {'repo': answer(1, '', 'HTTP 502')}, {'env': answer(1, '', 'HTTP 502')},
                   {'rules': answer(1, '', 'HTTP 403')}, {'rules': answer(0, 'not json')}):
            with self.subTest(kw=str(kw)[:60]):
                code, out, _ = self.guard(**kw)
                self.assertEqual(code, 1, out)
                self.assertIn('::error::head gate:', out)

    def test_a_rerun_reads_again_and_writes_nothing(self):   # reads only: the same answer twice
        first = self.guard()
        second = self.guard()
        self.assertEqual(first[:2], second[:2])
        self.assertEqual({a[0] for a, _ in first[2].calls}, {'api'})


# --- prs ------------------------------------------------------------------------------------------------------------
class Prs(Files, unittest.TestCase):
    def prs(self, event, pr='', routes=None, listing=None):
        def rest(*a):
            if a[:2] == ('api', '--paginate'):
                return listing if isinstance(listing, types.SimpleNamespace) else answer(0, listing or '')
            raise AssertionError(a)
        gh, out = Api(routes, rest), []
        code = G.prs(REPO, event, pr, gh, out.append, self.env)
        return code, '\n'.join(out), gh

    def test_the_events_one_open_pull_request(self):
        for event in G.EVENTS:
            with self.subTest(event=event):
                os.makedirs(self.files, exist_ok=True)
                code, out, _ = self.prs(event, '7', {f'repos/{REPO}/pulls/7': {'state': 'open'}})
                self.assertEqual((code, self.outputs().get('prs')), (0, '[7]'), out)
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_closed_or_missing_pull_request_judges_nothing(self):   # green: nothing to post on
        for route in ({'state': 'closed'}, not_found()):
            with self.subTest(route=str(route)[:30]):
                code, out, _ = self.prs('issue_comment', '7', {f'repos/{REPO}/pulls/7': route})
                self.assertEqual((code, self.outputs().get('prs')), (0, '[]'), out)
                self.assertIn('nothing to judge', out)
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_an_unreadable_pull_request_is_refused(self):
        code, out, _ = self.prs('pull_request_target', '7', {f'repos/{REPO}/pulls/7': answer(1, '', 'HTTP 502')})
        self.assertEqual((code, self.file('output')), (1, None), out)

    def test_other_events_and_bad_numbers_are_refused(self):
        for event, pr in (('pull_request', '7'), ('push', ''), ('', '7'), ('workflow_dispatch', '7; x'),
                          ('workflow_dispatch', '0'), ('issue_comment', ''), ('pull_request_target', '')):
            with self.subTest(event=event, pr=pr):
                code, out, gh = self.prs(event, pr)
                self.assertEqual((code, gh.calls, self.file('output')), (1, [], None), out)

    def test_the_backfill_lists_every_open_pull_request_every_page(self):
        for listing, want in (('', '[]'), ('\n'.join(map(str, range(101, 0, -1))) + '\n', json.dumps(
                list(range(1, 102)), separators=(',', ':')))):
            with self.subTest(n=len(listing.split())):
                code, out, gh = self.prs('workflow_dispatch', '', listing=listing)
                self.assertEqual((code, self.outputs().get('prs')), (0, want), out)
                call = gh.calls[0][0]
                self.assertIn('--paginate', call)
                self.assertIn(f'repos/{REPO}/pulls?state=open&per_page=100', call)
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_backfill_larger_than_a_matrix_is_refused(self):
        code, out, _ = self.prs('workflow_dispatch', '', listing='\n'.join(map(str, range(1, 258))))
        self.assertEqual((code, self.file('output')), (1, None), out)
        self.assertIn('at most 256', out)
        code, out, _ = self.prs('workflow_dispatch', '', listing='\n'.join(map(str, range(1, 257))))
        self.assertEqual(code, 0, out)

    def test_an_unreadable_or_odd_listing_is_refused(self):
        for listing in (answer(1, '', 'HTTP 502'), '1\nx\n'):
            with self.subTest(listing=str(listing)[:20]):
                code, out, _ = self.prs('workflow_dispatch', '', listing=listing)
                self.assertEqual((code, self.file('output')), (1, None), out)


# --- judge ----------------------------------------------------------------------------------------------------------
class Judge(Files, unittest.TestCase):
    def setUp(self):
        Files.setUp(self)
        self.w = T.World()
        self.addCleanup(self.w.close)
        self.p = T.Posts()
        self.m0 = self.w.commit(T.base_files())
        self.read = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['fix'], at=20)}), [self.m0])
        self.m1 = self.w.commit(T.base_files(**{'y.py': 'y\n'}), [self.m0])
        self.work = os.path.join(self.files, 'runner-temp')
        os.makedirs(self.work)
        self.verdict_path = os.path.join(self.files, 'verdict.json')

    def routes(self, head, base=None, state='open'):
        return {f'repos/{REPO}/pulls/7': {'state': state, 'head': {'sha': head}, 'base': {'ref': 'main'}},
                f'repos/{REPO}/git/ref/heads/main': {'object': {'sha': base or self.m1}}}

    def judge(self, routes, view, *extra, decide=H.decide, remote=None):
        """view: what gh pr view says (head_check's read of the review)."""
        gh, out = Api(routes, T.fake_gh((0, json.dumps(view)))), []
        argv = ['judge', '--repo', REPO, '--pr', '7', '--config', T.CONFIG_PATH, '--work', self.work,
                '--verdict', self.verdict_path, '--remote', remote or self.w.remote, *extra]
        code = G.cli(argv, env=self.env, gh=gh, out=out.append, decide=decide, sleep=lambda s: None)
        return code, '\n'.join(out), gh

    def verdict(self):
        with open(self.verdict_path, encoding='utf-8') as f:
            return json.load(f)

    def test_a_head_its_lenses_read_passes(self):
        code, out, gh = self.judge(self.routes(self.read), T.pr([self.p.lens('correctness', self.read)], head=self.read))
        self.assertEqual(code, 0, out)
        v = self.verdict()
        self.assertEqual((v['sha'], v['conclusion'], v['pr']), (self.read, 'success', 7))
        self.assertTrue(v['title'].startswith('pass: '), v['title'])
        self.assertIn(f'judged against the base tip {self.m1}', v['summary'])
        self.assertEqual(self.outputs(), {'post': 'true', 'sha': self.read, 'base': 'main'})
        self.assertNotIn('result=', self.file('output'), 'the action\'s output is not the gate\'s')
        self.assertNotIn(('pr', 'comment'), {a[:2] for a, _ in gh.calls}, 'judge only reads')
        self.assertTrue(all(a[0] in ('api', 'pr') and (a[0] == 'api' or a[1] == 'view') for a, _ in gh.calls))
        self.assertTrue(all(c[0][:2] != ('api', '-X') for c in gh.calls))

    def test_a_queue_update_of_the_read_commit_passes(self):
        head = self.w.merge(self.read, self.m1)
        code, out, _ = self.judge(self.routes(head), T.pr([self.p.lens('correctness', self.read)], head=head))
        self.assertEqual((code, self.verdict()['conclusion']), (0, 'success'), out)

    def test_a_head_no_lens_read_fails(self):   # a push after the review
        head = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['fix', 'more'], at=20)}), [self.read])
        code, out, _ = self.judge(self.routes(head), T.pr([self.p.lens('correctness', self.read)], head=head))
        self.assertEqual(code, 0, 'a verdict is written: the failure is the check run\'s, not the job\'s')
        v = self.verdict()
        self.assertEqual((v['sha'], v['conclusion']), (head, 'failure'))
        self.assertTrue(v['title'].startswith('fail: '), v['title'])

    def test_the_config_comes_from_the_base_tip_never_the_pull_request(self):
        # The head's copy makes the owner a bot, which would hide the owner's stale lens (no lens comments: pass).
        head = self.w.commit(T.base_files(cfg_raw={**T.CFG_RAW, 'bot_logins': ['owner']},
                                          **{'x.py': T.lines(extra=['unread'], at=20)}), [self.read])
        code, out, _ = self.judge(self.routes(head), T.pr([self.p.lens('correctness', self.read)], head=head))
        self.assertEqual(self.verdict()['conclusion'], 'failure', out)

    def test_the_required_lenses_are_the_callers(self):   # a pull request cannot drop one: it is no file of its own
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        self.judge(self.routes(self.read), view, '--required-lenses', 'design')
        self.assertEqual(self.verdict()['conclusion'], 'failure')
        self.assertIn('required lens without a read: design', self.verdict()['title'])

    def test_a_base_without_the_config_is_a_refused_verdict(self):   # empty state: posted red, says why
        bare_base = self.w.commit({'other.py': 'o\n'}, [])
        code, out, _ = self.judge(self.routes(self.read, base=bare_base), T.pr([], head=self.read))
        self.assertEqual(code, 0, out)
        v = self.verdict()
        self.assertEqual(v['conclusion'], 'failure')
        self.assertTrue(v['title'].startswith('refused: '), v['title'])
        self.assertIn('has no config', v['title'])

    def test_no_lens_comments_pass_only_without_required_lenses(self):   # empty state
        self.judge(self.routes(self.read), T.pr([], head=self.read))
        self.assertEqual(self.verdict()['conclusion'], 'success')
        self.judge(self.routes(self.read), T.pr([], head=self.read), '--required-lenses', 'correctness')
        self.assertEqual(self.verdict()['conclusion'], 'failure')

    def test_a_draft_passes_only_with_pass_drafts(self):
        view = T.pr([], head=self.read, draft=True)
        self.judge(self.routes(self.read), view, '--pass-drafts', '--required-lenses', 'correctness')
        self.assertEqual(self.verdict()['conclusion'], 'success')
        self.judge(self.routes(self.read), view, '--required-lenses', 'correctness')
        self.assertEqual(self.verdict()['conclusion'], 'failure')

    def test_a_closed_or_missing_pull_request_posts_nothing(self):
        for route in ({'state': 'closed', 'head': {'sha': self.read}, 'base': {'ref': 'main'}}, not_found()):
            with self.subTest(route=str(route)[:30]):
                code, out, gh = self.judge({f'repos/{REPO}/pulls/7': route}, T.pr())
                self.assertEqual((code, self.outputs().get('post')), (0, 'false'), out)
                self.assertFalse(os.path.exists(self.verdict_path))
                self.assertEqual(gh.paths(), [f'repos/{REPO}/pulls/7'])
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_head_github_does_not_name_fails_the_job_and_posts_nothing(self):   # no sha to post on
        for route in (answer(1, '', 'HTTP 502'), {'state': 'open', 'head': {}, 'base': {'ref': 'main'}},
                      {'state': 'open', 'head': {'sha': 'abc'}}, answer(0, 'not json')):
            with self.subTest(route=str(route)[:40]):
                code, out, _ = self.judge({f'repos/{REPO}/pulls/7': route}, T.pr())
                self.assertEqual((code, self.file('output')), (1, None), out)
                self.assertFalse(os.path.exists(self.verdict_path))

    def test_an_unreadable_base_tip_is_a_failed_verdict(self):
        routes = self.routes(self.read)
        for base in (answer(1, '', 'HTTP 502'), {'object': {}}, not_found()):
            with self.subTest(base=str(base)[:30]):
                routes[f'repos/{REPO}/git/ref/heads/main'] = base
                code, out, _ = self.judge(routes, T.pr([self.p.lens('correctness', self.read)], head=self.read))
                v = self.verdict()
                self.assertEqual((code, v['sha'], v['conclusion']), (0, self.read, 'failure'), out)
                self.assertIn('could not read the tip of the base branch', v['title'])

    def test_a_base_ref_is_quoted_into_the_path(self):
        routes = self.routes(self.read)
        routes[f'repos/{REPO}/pulls/7']['base']['ref'] = 'rel/1.0#x'
        routes[f'repos/{REPO}/git/ref/heads/rel/1.0%23x'] = {'object': {'sha': self.m1}}
        code, out, gh = self.judge(routes, T.pr([self.p.lens('correctness', self.read)], head=self.read))
        self.assertEqual(self.verdict()['conclusion'], 'success', out)
        self.assertEqual(self.outputs()['base'], 'rel/1.0#x')

    def test_a_head_that_moves_during_the_run_fails_on_the_head_it_read(self):   # the new head gets its own run
        new = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['fix', 'later'], at=20)}), [self.read])
        code, out, _ = self.judge(self.routes(self.read), T.pr([self.p.lens('correctness', self.read)], head=new))
        v = self.verdict()
        self.assertEqual((code, v['sha'], v['conclusion']), (0, self.read, 'failure'), out)
        self.assertIn('a newer head has its own run', v['title'] + v['summary'])

    def test_a_fetch_that_fails_is_a_failed_verdict(self):   # GitHub dropped a commit, or no credentials
        code, out, _ = self.judge(self.routes(self.read), T.pr(), remote=os.path.join(self.files, 'nowhere.git'))
        v = self.verdict()
        self.assertEqual((code, v['conclusion']), (0, 'failure'), out)
        self.assertIn('could not fetch the base and the head', v['title'])

    def test_a_crash_in_the_rules_is_a_failed_verdict_that_says_so(self):   # unknown never passes
        def boom(*a, **kw):
            raise RuntimeError('broken')
        code, out, _ = self.judge(self.routes(self.read), T.pr(), decide=boom)
        v = self.verdict()
        self.assertEqual((code, v['conclusion']), (0, 'failure'), out)
        self.assertIn('the gate failed: RuntimeError: broken', v['title'])

    def test_each_run_makes_its_own_bare_repository(self):   # runs again: never one an earlier run left
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        self.judge(self.routes(self.read), view)
        first = self.verdict()
        before = sorted(os.listdir(self.work))
        poisoned = os.path.join(self.work, before[0], 'info', 'grafts')   # an earlier run's repository, tampered with
        os.makedirs(os.path.dirname(poisoned), exist_ok=True)
        with open(poisoned, 'w') as f:
            f.write(f'{self.read} {self.m1}\n')
        self.judge(self.routes(self.read), view)
        self.assertEqual(self.verdict(), first, 'the same head and base give the same verdict')
        self.assertEqual(len(os.listdir(self.work)), 2)

    def test_a_stop_before_the_post_leaves_only_the_verdict_and_a_rerun_writes_it_again(self):
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        self.judge(self.routes(self.read), view)
        with open(self.verdict_path, 'w') as f:
            f.write('{"half": ')   # the job stopped while writing; the next run starts over
        code, out, _ = self.judge(self.routes(self.read), view)
        self.assertEqual((code, self.verdict()['conclusion']), (0, 'success'), out)

    def test_no_text_from_github_starts_a_workflow_command(self):
        name = 'x%0A::warning::y\r::error::z\n::error::w'
        view = T.pr([self.p.post(f'{T.LENS}: {name} · read at: {self.read}\n::error::forged')], head=self.read)
        code, out, _ = self.judge(self.routes(self.read), view)
        self.assertTrue(all(not line.startswith('::') or line.startswith('::error::#7') for line in out.splitlines()),
                        out)

    def test_bad_arguments_are_refused_before_github_is_read(self):
        for repo, pr in (('not a repo', '7'), (REPO, '0'), (REPO, '7 ')):
            with self.subTest(repo=repo, pr=pr):
                gh, out = Api(), []
                a = argparse.Namespace(repo=repo, pr=pr, config=T.CONFIG_PATH, required_lenses='', pass_drafts=False,
                                       work=self.work, verdict=self.verdict_path, remote=self.w.remote)
                self.assertEqual((G.judge(a, gh, out.append, self.env), gh.calls), (1, []))

    def test_the_commits_come_from_github_by_default(self):
        seen = []
        real = G.judge
        try:
            G.judge = lambda a, *rest: seen.append(a.remote) or 0
            G.cli(['judge', '--repo', REPO, '--pr', '7', '--config', 'c', '--work', 'w', '--verdict', 'v'], env={})
        finally:
            G.judge = real
        self.assertEqual(seen, [f'https://github.com/{REPO}.git'])


# --- post -----------------------------------------------------------------------------------------------------------
class Post(Files, unittest.TestCase):
    SHA = 'ab' * 20

    def write(self, v):
        path = os.path.join(self.files, 'verdict.json')
        with open(path, 'w', encoding='utf-8') as f:
            f.write(v if isinstance(v, str) else json.dumps(v))
        return path

    def post(self, v=None, result=None, env=None, check=CHECK):
        v = {'pr': 7, 'sha': self.SHA, 'conclusion': 'success', 'title': 'pass: ok', 'summary': 's'} if v is None else v
        calls, out = [], []

        def gh(*a, stdin=None):
            calls.append((a, stdin))
            return result or answer(0, '{"id": 1}')
        code = G.post(REPO, check, self.write(v), gh, out.append, {**self.env, **(env or {})})
        return code, '\n'.join(out), calls

    def test_one_call_posts_a_completed_check_run_on_the_judged_head(self):
        code, out, calls = self.post(env={'GITHUB_RUN_ID': '99', 'GITHUB_SERVER_URL': 'https://github.com'})
        self.assertEqual((code, len(calls)), (0, 1), out)
        a, stdin = calls[0]
        self.assertEqual(a, ('api', '-X', 'POST', f'repos/{REPO}/check-runs', '--input', '-'))
        body = json.loads(stdin)
        self.assertEqual(body, {'name': CHECK, 'head_sha': self.SHA, 'status': 'completed', 'conclusion': 'success',
                                'output': {'title': 'pass: ok', 'summary': 's'},
                                'details_url': f'https://github.com/{REPO}/actions/runs/99'})

    def test_a_failed_or_refused_verdict_is_a_failed_check_run(self):
        code, out, calls = self.post({'pr': 7, 'sha': self.SHA, 'conclusion': 'failure', 'title': 'refused: x',
                                      'summary': 's'})
        self.assertEqual((code, json.loads(calls[0][1])['conclusion']), (0, 'failure'))

    def test_an_api_error_is_a_red_job_after_one_call(self):   # stops half-way: nothing or one complete check run
        code, out, calls = self.post(result=answer(1, '', 'HTTP 403: Resource not accessible by integration'))
        self.assertEqual((code, len(calls)), (1, 1), out)
        self.assertIn('Nothing was retried', out)

    def test_a_verdict_judge_did_not_write_makes_no_call(self):
        good = {'pr': 7, 'sha': self.SHA, 'conclusion': 'success', 'title': 't', 'summary': 's'}
        for v in ('{"half": ', [], {**good, 'sha': 'ab'}, {**good, 'conclusion': 'neutral'},
                  {**good, 'conclusion': 'SUCCESS'}, {**good, 'title': None}, {k: x for k, x in good.items() if k != 'summary'}):
            with self.subTest(v=str(v)[:40]):
                code, out, calls = self.post(v)
                self.assertEqual((code, calls), (1, []), out)
        code, out, calls = self.post(check='')
        self.assertEqual((code, calls), (1, []), out)

    def test_a_missing_verdict_makes_no_call(self):
        calls, out = [], []
        code = G.post(REPO, CHECK, os.path.join(self.files, 'none.json'), lambda *a, **k: calls.append(a), out.append,
                      self.env)
        self.assertEqual((code, calls), (1, []))

    def test_long_texts_are_cut_to_what_github_takes(self):
        code, out, calls = self.post({'pr': 7, 'sha': self.SHA, 'conclusion': 'failure', 'title': 't' * 500,
                                      'summary': 's' * 70000})
        body = json.loads(calls[0][1])
        self.assertEqual((len(body['output']['title']), len(body['output']['summary'])), (G.TITLE_MAX, G.SUMMARY_MAX))
        self.assertNotIn('details_url', body, 'no run id: no link')

    def test_a_verdict_text_is_cut_too(self):
        rows = [(f'lens{i}', False, 'x' * 100) for i in range(1000)]
        title, text = G.verdict_text(1, False, 'r' * 300, rows, self.SHA, self.SHA, '7')
        self.assertLessEqual((len(title), len(text)), (G.TITLE_MAX, G.SUMMARY_MAX))
        self.assertTrue(title.startswith('fail: '))
        self.assertIn('(cut: the run log has every row)', text)


# --- drift ----------------------------------------------------------------------------------------------------------
class Drift(Files, unittest.TestCase):
    PATH = f'repos/{REPO}/rules/branches/main?per_page=100'

    def drift(self, rules, app='4242'):
        gh, out = Api({self.PATH: rules}), []
        code = G.drift(REPO, 'main', CHECK, app, gh, out.append, self.env)
        return code, '\n'.join(out)

    @staticmethod
    def required(*checks):
        return [{'type': 'pull_request'},
                {'type': 'required_status_checks', 'parameters': {'required_status_checks': list(checks)}}]

    def test_a_check_bound_to_the_gate_app_is_said_and_never_warned(self):
        code, out = self.drift(self.required({'context': CHECK, 'integration_id': 4242}, {'context': 'build'}))
        self.assertEqual(code, 0)
        self.assertIn('bound to the gate App 4242', out)
        self.assertNotIn('::warning::', out)

    def test_every_drift_is_a_warning_never_a_failure(self):   # the check run is posted either way
        for rules, says in ((self.required({'context': CHECK}), 'from None'),
                            (self.required({'context': CHECK, 'integration_id': 15368}), 'from 15368'),
                            (self.required({'context': CHECK, 'integration_id': 4242},
                                           {'context': CHECK, 'integration_id': 15368}), 'from 15368'),
                            (self.required({'context': 'build'}), 'no ruleset of main requires'),
                            ([], 'no ruleset of main requires'),
                            (answer(1, '', 'HTTP 403'), 'could not read the rules')):
            with self.subTest(says=says):
                code, out = self.drift(rules)
                self.assertEqual(code, 0, out)
                self.assertIn('::warning::head gate:', out)
                self.assertIn(says, out)
        code, out = self.drift([], app='')
        self.assertEqual(code, 0)
        self.assertIn('HEAD_GATE_APP_ID', out)


if __name__ == '__main__':
    unittest.main()
