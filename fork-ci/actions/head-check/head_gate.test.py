"""Tests of head_gate.py (run: python3 fork-ci/actions/head-check/head_gate.test.py).

The gate judges a head: every open, ready pull request into the default branch with it, by head_check's rules, on a
bare repository it fetched the default branch tip and the head into. It posts an in-progress check run first and
completes it with the verdict, or with a failure when there is none. For each step: what passes, what fails or is
refused, and what a re-run, a stop half-way and old or empty state do. The rules themselves are head_check.test.py's (it
runs every rule test in bare mode too); here: the guard, the list of heads, the checks of the run (target), the judge
around the rules, start and finish, and the drift warning.

Real commits in head_check.test.py's World (a local bare repository stands in for GitHub); `gh` is a fake. The guard
that fails the run if anything starts the real `gh` is head_check.test.py's, loaded first.
"""
import importlib.util, os, sys

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('head_check_test', os.path.join(HERE, 'head_check.test.py'))
T = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(T)   # first: its guard puts a fake gh on PATH, drops tokens and isolates git

import argparse, base64, io, json, shutil, tempfile, types, unittest, unittest.mock  # noqa: E402

sys.path.insert(0, HERE)
import head_check as H  # noqa: E402
import head_gate as G  # noqa: E402

REPO, ENV_NAME, KEY = T.REPO, 'head-gate', 'HEAD_GATE_APP_KEY'
CHECK = 'review-check'
PAUSES = []   # every pause between two tries of a read: none is slept in a test
G.pause = PAUSES.append


def answer(code=0, out='', err=''):
    return types.SimpleNamespace(returncode=code, stdout=out, stderr=err)


def not_found():
    return answer(1, '', 'gh: Not Found (HTTP 404)')


GRAPHQL_FOUND = {'data': {'repository': {'environment': {'name': ENV_NAME}}}}
GRAPHQL_MISSING = answer(1, json.dumps({'data': {'repository': {'environment': None}}, 'errors': [
    {'type': 'NOT_FOUND', 'message': 'Could not resolve to an Environment with the name head-gate.'}]}),
    'gh: Could not resolve to an Environment with the name head-gate.')


class Seq(list):
    """A route that answers in turn: each call takes the next answer, the last one stays."""


class Api:
    """gh that answers `gh api PATH` from a dict {path: answer or JSON-able data} and records every call. Any other
    call goes to `rest` (head_check.test.py's fake for pr view and the reviews query), or fails the test. `lists`:
    {query: answer or rows} for the paginated list of open pull requests."""
    def __init__(self, routes=None, rest=None, lists=None):
        self.routes, self.rest, self.lists, self.calls = dict(routes or {}), rest, dict(lists or {}), []

    def __call__(self, *a, stdin=None):
        self.calls.append((a, stdin))
        if a[:2] == ('api', 'graphql') and 'graphql' in self.routes:   # the guard's Environment query
            assert a[2:] == ('-f', f'query={G.ENVIRONMENT_QUERY}', '-f', f'owner={REPO.split("/")[0]}', '-f',
                             f'name={REPO.split("/")[1]}', '-f', f'environment={ENV_NAME}'), a
            a = ('api', 'graphql')
        if a[0] == 'api' and len(a) == 2 and a[1] in self.routes:
            v = self.routes[a[1]]
            if isinstance(v, Seq):
                v = v.pop(0) if len(v) > 1 else v[0]
            return v if isinstance(v, types.SimpleNamespace) else answer(0, json.dumps(v))
        if a[:2] == ('api', '--paginate') and a[3:] == ('--jq', G.LIST_JQ) and a[2] in self.lists:
            v = self.lists[a[2]]   # the open pull requests: (number, draft, head sha, base ref), one JSON line each
            return v if isinstance(v, types.SimpleNamespace) else answer(0, ''.join(json.dumps(x) + '\n' for x in v))
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

    def routes(self, policy='default', rules='default', env=None, repo=None, graphql=None):
        if policy == 'default':
            policy = {'protected_branches': False, 'custom_branch_policies': True}
        if rules == 'default':
            rules = [{'name': 'main', 'type': 'branch'}]
        return {f'repos/{REPO}': {'default_branch': 'main'} if repo is None else repo,
                self.ENV_PATH: {'name': ENV_NAME, 'deployment_branch_policy': policy} if env is None else env,
                self.ENV_PATH + '/deployment-branch-policies?per_page=100':
                    rules if isinstance(rules, types.SimpleNamespace) else
                    {'total_count': len(rules), 'branch_policies': rules},
                # GraphQL names the Environment when the REST answer is one; any other env: a missing one, unless given
                'graphql': graphql if graphql is not None else GRAPHQL_FOUND if env is None else GRAPHQL_MISSING}

    def guard(self, secret='false', environment_only=False, **kw):
        gh, out = Api(self.routes(**kw)), []
        code = G.guard(REPO, ENV_NAME, KEY, secret, environment_only, gh, out.append, self.env)
        return code, '\n'.join(out), gh

    def test_only_the_default_branch_and_no_repository_secret_passes(self):
        code, out, _ = self.guard()
        self.assertEqual(code, 0, out)
        self.assertIn('Only main may deploy to head-gate', out)

    def test_only_the_default_branch_and_no_repository_secret_says_ok(self):   # the gate may judge
        code, out, _ = self.guard()
        self.assertEqual((code, self.outputs()), (0, {'guard': 'ok'}), out)

    def test_a_repository_or_organisation_secret_of_the_keys_name_is_refused(self):   # any branch's workflow reads it
        code, out, gh = self.guard(secret='true')
        self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)   # only failures are posted
        self.assertIn(f'secret named {KEY} exists', out)
        self.assertIn('Delete it', self.file('summary.md'))
        self.assertIn('posts a failure on every head', out)

    def test_an_unknown_secret_answer_is_refused(self):   # the workflow did not pass it: never read as "none"
        for secret in ('', 'True', 'false\n::warning::x'):
            with self.subTest(secret=secret):
                code, out, gh = self.guard(secret=secret)
                self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)
                os.remove(self.env['GITHUB_OUTPUT'])
                self.assertIn('could not tell whether a repository secret', out)
                self.assertTrue(all(not line.startswith('::') or line.startswith('::error::head gate')
                                    for line in out.splitlines()), out)

    def test_the_environment_part_alone_skips_the_secret_answer(self):   # the gate job has no such input
        code, out, _ = self.guard(secret='', environment_only=True)
        self.assertEqual(code, 0, out)

    def test_a_missing_environment_is_refused_with_the_steps(self):
        # No output at all: the gate job must not run, as naming the Environment would create it open to all branches.
        for secret in ('false', 'true'):
            with self.subTest(secret=secret):
                code, out, _ = self.guard(env=not_found(), secret=secret)
                self.assertEqual((code, self.file('output')), (1, None), out)
                self.assertIn('does not exist', out)
                self.assertIn('HEAD_GATE_APP_ID', out)
        code, out, _ = self.guard(env=not_found(), environment_only=True)   # the gate job: it exists by then
        self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)

    def test_an_environment_any_branch_may_use_is_refused(self):   # no deployment branch rule at all
        code, out, _ = self.guard(policy=None)
        self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)
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

    def test_unreadable_answers_are_refused(self):   # unknown never judges: the gate posts only failures
        for kw in ({'repo': {}}, {'repo': answer(1, '', 'HTTP 502')}, {'rules': answer(1, '', 'HTTP 403')},
                   {'rules': answer(0, 'not json')}):
            with self.subTest(kw=str(kw)[:60]):
                code, out, _ = self.guard(**kw)
                self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)
                os.remove(self.env['GITHUB_OUTPUT'])
                self.assertIn('::error::head gate:', out)

    def test_an_environment_the_guard_job_cannot_read_is_never_named_by_the_gate_job(self):   # sec85r2 n1
        # It may not exist yet (before setup), and a job that named it would make GitHub create it, open to every
        # branch. A commenter can spend the job token's rate limit, so this must not depend on the error.
        for env in (answer(1, '', 'gh: Server Error (HTTP 502)'), answer(1, '', 'gh: API rate limit exceeded (HTTP 403)'),
                    answer(1, '', 'HTTP 401'), answer(0, 'not json'), answer(0, '[]')):
            for secret in ('false', 'true'):
                for graphql in (GRAPHQL_MISSING, answer(1, '', 'gh: API rate limit exceeded (HTTP 403)'),
                                answer(0, 'not json'), {'data': {'repository': {'environment': {'name': 'other'}}}}):
                    with self.subTest(env=env.stderr or env.stdout, secret=secret, graphql=str(graphql)[:50]):
                        code, out, _ = self.guard(env=env, secret=secret, graphql=graphql)
                        self.assertEqual((code, self.file('output')), (1, None), out)
                        self.assertIn('the gate job does not start and nothing is posted', out)

    def test_graphql_that_names_the_environment_after_a_spent_rest_limit_lets_the_gate_post_failures(self):
        # sec85r R1 kept closed with n1: comment spam spends the job token's REST limit; GraphQL has its own.
        for env in (answer(1, '', 'gh: API rate limit exceeded (HTTP 403)'), answer(1, '', 'gh: Server Error (HTTP 502)')):
            with self.subTest(env=env.stderr):
                code, out, gh = self.guard(env=env, graphql=GRAPHQL_FOUND)
                self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)   # never ok: not read
                self.assertIn('graphql', gh.paths())
                os.remove(self.env['GITHUB_OUTPUT'])
        code, out, gh = self.guard(env=answer(1, '', 'HTTP 401'), graphql=GRAPHQL_FOUND)   # not 5xx/rate: no GraphQL
        self.assertEqual((code, self.file('output'), 'graphql' in gh.paths()), (1, None, False), out)
        for env in (answer(1, '', 'gh: Server Error (HTTP 502)'), answer(0, 'not json')):   # the gate job: it exists
            with self.subTest(env=env.stderr or env.stdout, environment_only=True):
                code, out, _ = self.guard(env=env, environment_only=True)
                self.assertEqual((code, self.outputs()), (1, {'guard': 'fail'}), out)
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_5xx_or_a_rate_limit_is_read_again(self):   # round 2: one API error no longer decides the run
        routes = self.routes()
        env = routes[self.ENV_PATH]
        routes[self.ENV_PATH] = Seq([answer(1, '', 'gh: Server Error (HTTP 502)'),
                                     answer(1, '', 'gh: API rate limit exceeded for installation (HTTP 403)'), env])
        gh, out = Api(routes), []
        del PAUSES[:]
        code = G.guard(REPO, ENV_NAME, KEY, 'false', False, gh, out.append, self.env)
        self.assertEqual((code, self.outputs()), (0, {'guard': 'ok'}), out)
        self.assertEqual((gh.paths().count(self.ENV_PATH), PAUSES), (3, list(G.READ_BACKOFF)))

    def test_reads_are_tried_at_most_three_times_and_only_on_a_5xx_or_a_rate_limit(self):
        for err, calls in (('gh: Server Error (HTTP 503)', 3), ('HTTP 429: too many', 3),
                           ('gh: You have exceeded a secondary rate limit (HTTP 403)', 3),
                           ('gh: Resource not accessible by integration (HTTP 403)', 1), ('HTTP 401', 1)):
            with self.subTest(err=err):
                gh = Api(self.routes(env=answer(1, '', err)))
                code = G.guard(REPO, ENV_NAME, KEY, 'false', True, gh, lambda *_: None, self.env)
                self.assertEqual((code, self.outputs(), gh.paths().count(self.ENV_PATH)), (1, {'guard': 'fail'}, calls))
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_rerun_reads_again_and_writes_nothing(self):   # reads only: the same answer twice
        first = self.guard()
        second = self.guard()
        self.assertEqual(first[:2], second[:2])
        self.assertEqual({a[0] for a, _ in first[2].calls}, {'api'})


# --- the run: what target and judge read ----------------------------------------------------------------------------
GATE_REPO, GATE_SHA = 'tools/gate', 'c0' * 20
CALLER = '.github/workflows/head-gate.yml'
CALLER_REF = f'{REPO}/{CALLER}@refs/heads/main'
CALLER_TEXT = (f'jobs:\n  gate:\n    uses: {GATE_REPO}/.github/workflows/fork-head-gate.yml@{GATE_SHA} # ci/v1\n'
               '    secrets: inherit\n')
LISTED = f'repos/{REPO}/pulls?state=open&per_page=100&base=main'


def caller_file(text=CALLER_TEXT, blob='b1'):
    return {'sha': blob, 'encoding': 'base64', 'content': base64.b64encode(text.encode()).decode()}


def gate_routes(ref, workflow='g1', head_check='t1', pr_queue='t2'):
    """The gate's own files at `ref` (a local gate compares them with the tip): the workflow's blob, the folders' trees."""
    folders = [{'name': name, 'type': 'dir', 'sha': sha} for name, sha in (('head-check', head_check),
                                                                          ('pr-queue', pr_queue)) if sha]
    return {f'repos/{REPO}/contents/.github/workflows/fork-head-gate.yml?ref={ref}':
            {'sha': workflow} if isinstance(workflow, str) else workflow,
            f'repos/{REPO}/contents/fork-ci/actions?ref={ref}': [{'name': 'README.md', 'type': 'file', 'sha': 'r'},
                                                                 *folders]}


def run_routes(tip, caller_sha=None, now=None, then=None):
    """What GitHub says about the default branch and the caller: the caller ran at `caller_sha` (default: the tip);
    `then` and `now` are its file there and at the tip (default: the same file). The gate's own files are the same
    at both (gate_routes)."""
    caller_sha = caller_sha or tip
    routes = {f'repos/{REPO}': {'default_branch': 'main'},
              f'repos/{REPO}/git/ref/heads/main': {'object': {'sha': tip}},
              f'repos/{REPO}/contents/{CALLER}?ref={caller_sha}': caller_file() if then is None else then,
              **gate_routes(tip), **gate_routes(caller_sha)}
    if caller_sha != tip:
        routes[f'repos/{REPO}/contents/{CALLER}?ref={tip}'] = caller_file() if now is None else now
    return routes


def run_args(sha, **kw):
    a = {'repo': REPO, 'sha': sha, 'required_lenses': 'correctness', 'caller_ref': CALLER_REF, 'caller_sha': None,
         'gate_repo': GATE_REPO, 'gate_sha': GATE_SHA, 'run_attempt': '1', 'guard': 'ok', 'environment_guard': 'ok',
         **kw}
    return a


def run_argv(sha, tip, **kw):
    a = run_args(sha, **kw)
    a['caller_sha'] = a['caller_sha'] or tip
    return [x for k, v in a.items() for x in (f'--{k.replace("_", "-")}', v)]


# --- prs ------------------------------------------------------------------------------------------------------------
class Prs(Files, unittest.TestCase):
    H, H2 = 'a1' * 20, 'b2' * 20

    def prs(self, event, pr='', pull=None, listing=None, event_sha='', git_head=None):
        routes = {}
        if pull is not None:
            routes[f'repos/{REPO}/pulls/{pr}'] = pull
        gh, out = Api(routes, lists={f'repos/{REPO}/pulls?state=open&per_page=100': listing or []}), []
        code = G.prs(REPO, event, pr, gh, out.append, self.env, event_sha, git_head)
        return code, '\n'.join(out), gh

    def pull(self, head=None, draft=False, base='main', state='open', merged=False):
        return {'state': state, 'draft': draft, 'merged': merged, 'head': {'sha': head or self.H}, 'base': {'ref': base}}

    def found(self):
        found = json.loads(self.outputs().get('prs', 'null'))
        os.remove(self.env['GITHUB_OUTPUT'])
        return found

    def test_the_events_one_open_ready_pull_request_into_the_default_branch(self):
        for event in G.EVENTS:
            with self.subTest(event=event):
                code, out, _ = self.prs(event, '7', self.pull())
                self.assertEqual((code, self.found()), (0, [{'pr': 7, 'sha': self.H}]), out)

    def test_a_merged_or_missing_pull_request_judges_nothing(self):   # its head is on the default branch now
        for pull in (self.pull(state='closed', merged=True), not_found()):
            with self.subTest(pull=str(pull)[:30]):
                code, out, _ = self.prs('issue_comment', '7', pull)
                self.assertEqual((code, self.found()), (0, []), out)
                self.assertIn('nothing to judge', out)

    def test_a_draft_another_base_or_a_closed_one_gets_its_head_failed(self):   # round 2: a revoked pass never waits
        # A draft (or a pull request into another branch, or a closed one) is never judged, but a pass its head had
        # must not survive until it is ready, retargeted or reopened: target fails the head (no ready one has it).
        for pull in (self.pull(draft=True), self.pull(base='loose'), self.pull(base=None), self.pull(state='closed')):
            with self.subTest(pull=pull):
                code, out, _ = self.prs('pull_request_target', '7', pull)
                self.assertEqual((code, self.found()), (0, [{'pr': 7, 'sha': self.H}]), out)

    def test_an_unreadable_pull_request_is_refused_only_when_its_head_is_unknown(self):
        for pull in (answer(1, '', 'HTTP 502'), {**self.pull(), 'head': {'sha': 'abc'}}):
            with self.subTest(pull=str(pull)[:40]):
                code, out, gh = self.prs('issue_comment', '7', pull)
                self.assertEqual((code, self.file('output')), (1, None), out)
                self.assertIn('Its head is unknown', out)
        self.assertEqual(gh.paths().count(f'repos/{REPO}/pulls/7'), 1, 'a 200 is never read again')

    def test_a_pull_request_read_that_fails_every_try_falls_back_to_the_head_it_knows(self):   # round 2
        down = Seq([answer(1, '', 'gh: Server Error (HTTP 502)')])
        code, out, gh = self.prs('pull_request_target', '7', down, event_sha=self.H.upper())
        self.assertEqual((code, self.found()), (0, [{'pr': 7, 'sha': self.H}]), out)
        self.assertEqual(gh.paths().count(f'repos/{REPO}/pulls/7'), G.READ_TRIES)
        self.assertIn('the head the event carries', out)
        asked = []
        code, out, _ = self.prs('issue_comment', '7', down, git_head=lambda *a: asked.append(a) or (self.H, None))
        self.assertEqual((code, self.found(), asked), (0, [{'pr': 7, 'sha': self.H}], [(REPO, '7', self.env)]), out)
        self.assertIn('refs/pull/7/head by git', out)
        code, out, _ = self.prs('issue_comment', '7', down, git_head=lambda *a: (None, 'git ls-remote: down'))
        self.assertEqual((code, self.file('output')), (1, None), out)   # GitHub is down: start could not post either
        self.assertIn('git ls-remote: down', out)

    def test_one_5xx_on_the_pull_request_read_is_read_again(self):   # round 2 (corr85r, sec85r): no fallback needed
        pull = Seq([answer(1, '', 'gh: Server Error (HTTP 502)'), self.pull()])
        code, out, gh = self.prs('issue_comment', '7', pull)
        self.assertEqual((code, self.found()), (0, [{'pr': 7, 'sha': self.H}]), out)
        self.assertEqual(gh.paths().count(f'repos/{REPO}/pulls/7'), 2)

    def test_the_head_is_read_by_git_without_the_token_on_the_command_line(self):
        seen = []

        def run(argv, **kw):
            seen.append((argv, kw['env']))
            return answer(0, f'{self.H}\trefs/pull/7/head\n')
        with unittest.mock.patch.object(G.subprocess, 'run', run):
            self.assertEqual(G.pull_head_by_git(REPO, '7', {'GH_TOKEN': 'secret-token'}), (self.H, None))
        argv, env = seen[0]
        self.assertEqual(argv[-3:], ['--', f'https://github.com/{REPO}.git', 'refs/pull/7/head'])
        self.assertNotIn('secret-token', ' '.join(argv))
        self.assertEqual((env['GIT_CONFIG_KEY_0'], env['GIT_TERMINAL_PROMPT']), ('http.extraheader', '0'))
        self.assertIn(base64.b64encode(b'x-access-token:secret-token').decode(), env['GIT_CONFIG_VALUE_0'])
        for said in ('', f'{self.H}\trefs/pull/8/head\n', 'abc\trefs/pull/7/head\n'):
            with self.subTest(said=said), unittest.mock.patch.object(G.subprocess, 'run', lambda *a, **k: answer(0, said)):
                self.assertIsNone(G.pull_head_by_git(REPO, '7', {})[0])

    def test_other_events_and_bad_numbers_are_refused(self):
        for event, pr in (('pull_request', '7'), ('push', ''), ('', '7'), ('workflow_dispatch', '7; x'),
                          ('workflow_dispatch', '0'), ('issue_comment', ''), ('pull_request_target', '')):
            with self.subTest(event=event, pr=pr):
                code, out, gh = self.prs(event, pr)
                self.assertEqual((code, gh.calls, self.file('output')), (1, [], None), out)

    def test_the_backfill_takes_each_head_of_an_open_pull_request_once(self):   # drafts' and other bases' get failed
        listing = [(9, False, self.H, 'main'), (3, False, self.H, 'main'), (5, False, self.H2, 'loose'),
                   (4, True, self.H2, 'main'), (6, False, 'c3' * 20, 'main'), (8, True, 'd4' * 20, 'main')]
        code, out, gh = self.prs('workflow_dispatch', '', listing=listing)
        self.assertEqual((code, self.found()), (0, [{'pr': 3, 'sha': self.H}, {'pr': 4, 'sha': self.H2},
                                                    {'pr': 6, 'sha': 'c3' * 20}, {'pr': 8, 'sha': 'd4' * 20}]), out)
        call = gh.calls[-1][0]
        self.assertEqual(call[:3], ('api', '--paginate', f'repos/{REPO}/pulls?state=open&per_page=100'))
        code, out, _ = self.prs('workflow_dispatch', '', listing=[])
        self.assertEqual((code, self.found()), (0, []), out)

    def test_a_backfill_larger_than_a_matrix_is_refused(self):
        listing = [(n, False, f'{n:040x}', 'main') for n in range(1, 258)]
        code, out, _ = self.prs('workflow_dispatch', '', listing=listing)
        self.assertEqual((code, self.file('output')), (1, None), out)
        self.assertIn('at most 256', out)
        code, out, _ = self.prs('workflow_dispatch', '', listing=listing[:256])
        self.assertEqual(code, 0, out)

    def test_an_unreadable_or_odd_listing_is_refused(self):
        for listing in (answer(1, '', 'HTTP 502'), answer(0, '[1, false, "x", "main"]\n'), answer(0, 'x\n'),
                        answer(0, '[true, false, "' + self.H + '", "main"]\n')):
            with self.subTest(listing=listing.stdout[:20]):
                code, out, _ = self.prs('workflow_dispatch', '', listing=listing)
                self.assertEqual((code, self.file('output')), (1, None), out)


# --- target ---------------------------------------------------------------------------------------------------------
class Target(Files, unittest.TestCase):
    H, TIP = 'a1' * 20, 'e5' * 20

    def target(self, listing=None, routes=None, **kw):
        routes = run_routes(self.TIP) if routes is None else routes
        listing = [(7, False, self.H, 'main')] if listing is None else listing
        gh, out = Api(routes, lists={LISTED: listing}), []
        code = G.cli(['target', *run_argv(self.H, self.TIP, **kw)], env=self.env, gh=gh, out=out.append)
        return code, '\n'.join(out), gh

    def test_a_ready_pull_request_into_the_default_branch_is_posted_for(self):
        code, out, gh = self.target()
        self.assertEqual((code, self.outputs()), (0, {'post': 'true', 'base': 'main'}), out)
        self.assertIn('#7', out)
        self.assertTrue(all(a[0] == 'api' and '-X' not in a for a, _ in gh.calls), 'target only reads')

    def failed(self, out, says):
        """target let the run post, with nothing to judge: judge writes a failure (the run's preflight again)."""
        self.assertEqual(self.outputs(), {'post': 'true', 'base': ''}, out)
        self.assertIn('the head gets a failed check', out)
        self.assertIn(says, out)
        os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_head_only_a_draft_or_another_base_has_gets_a_failure(self):   # round 2: a revoked pass never waits
        for listing in ([], [(7, True, self.H, 'main')], [(7, False, self.H, 'loose')], [(7, False, 'b2' * 20, 'main')]):
            with self.subTest(listing=listing):
                code, out, _ = self.target(listing)
                self.assertEqual(code, 0, out)
                self.failed(out, 'no open, ready pull request into main has the head')

    def test_a_guard_that_did_not_pass_gets_a_failure_before_github_is_read(self):   # round 2: guard=fail
        for guard, environment_guard in (('fail', 'ok'), ('', 'ok'), ('ok', 'fail'), ('ok', ''), ('OK', 'ok')):
            with self.subTest(guard=guard, environment_guard=environment_guard):
                code, out, gh = self.target(guard=guard, environment_guard=environment_guard)
                self.assertEqual((code, gh.calls), (0, []), out)
                self.failed(out, 'the guard did not pass')

    def test_a_re_run_gets_a_failure_before_github_is_read(self):   # finding F3; round 2: posts, never silent
        for attempt in ('2', '10', '', '1 '):
            with self.subTest(attempt=attempt):
                code, out, gh = self.target(run_attempt=attempt)
                self.assertEqual((code, gh.calls), (0, []), out)
                self.failed(out, 'dispatch the gate with the pull request number')

    def test_no_required_lens_gets_a_failure_before_github_is_read(self):   # lens minor: none would pass a bare head
        for lenses in ('', '\n \n', '<LENS_NAME>', 'r4'):
            with self.subTest(lenses=lenses):
                code, out, gh = self.target(required_lenses=lenses)
                self.assertEqual((code, gh.calls), (0, []), out)
                self.failed(out, 'required-lenses names no lens')

    def test_bad_arguments_post_nothing(self):   # no head to post on
        for sha, kw in (('ab', {}), (self.H, {'repo': 'not a repo'})):
            with self.subTest(sha=sha, **kw):
                gh, out = Api(), []
                code = G.cli(['target', *run_argv(sha, self.TIP, **kw)], env=self.env, gh=gh, out=out.append)
                self.assertEqual((code, gh.calls, self.outputs()), (1, [], {'post': 'false'}), out)
                os.remove(self.env['GITHUB_OUTPUT'])

    def test_a_caller_the_default_branch_no_longer_has_gets_a_failure(self):   # F3; round 2 (corr85r): not silence
        old = 'd4' * 20
        moved = caller_file(CALLER_TEXT.replace(GATE_SHA, 'c1' * 20), blob='b2')
        cases = {'the caller file changed': run_routes(self.TIP, old, now=caller_file(CALLER_TEXT + '#\n', 'b2')),
                 'no longer pins': run_routes(self.TIP, now=moved, then=moved),
                 'gone from the tip': run_routes(self.TIP, old, now=not_found())}
        for name, routes in cases.items():
            with self.subTest(name):
                code, out, gh = self.target(routes=routes, caller_sha=old if 'pins' not in name else None)
                self.assertEqual(code, 0, out)
                self.failed(out, "not the default branch's current gate")
                self.assertNotIn(LISTED, [a[2] for a, _ in gh.calls if len(a) > 2])
        for ref in (f'{REPO}/{CALLER}@refs/heads/loose', f'other/repo/{CALLER}@refs/heads/main',
                    f'{REPO}/x.yml@refs/heads/main', 'nonsense'):
            with self.subTest(ref=ref):
                code, out, _ = self.target(caller_ref=ref)
                self.assertEqual(code, 0, out)
                self.failed(out, "not the default branch's current gate")

    def test_a_caller_unchanged_since_the_event_still_counts(self):   # the tip moved, the caller file did not
        code, out, _ = self.target(routes=run_routes(self.TIP, 'd4' * 20), caller_sha='d4' * 20)
        self.assertEqual((code, self.outputs()), (0, {'post': 'true', 'base': 'main'}), out)

    def test_a_local_gate_counts_while_its_own_files_are_the_same_at_the_tip(self):   # round 2 (corr85r, sec85r)
        # The gate repository calls its own workflow. The tip moving (any merge) changes nothing the gate runs; only
        # a change to the gate's own files (the workflow, head-check, pr-queue) makes the run an old gate.
        tip, old = self.TIP, 'd4' * 20
        routes = {**run_routes(tip), f'repos/{REPO}/contents/{CALLER}?ref={tip}': caller_file('uses: ./x\n')}
        code, out, _ = self.target(routes=routes, gate_repo=REPO, gate_sha=tip)
        self.assertEqual((code, self.outputs()), (0, {'post': 'true', 'base': 'main'}), out)
        os.remove(self.env['GITHUB_OUTPUT'])
        code, out, _ = self.target(routes=run_routes(tip, old), gate_repo=REPO, gate_sha=old, caller_sha=old)
        self.assertEqual((code, self.outputs()), (0, {'post': 'true', 'base': 'main'}), out)
        self.assertIn('judging', out)
        os.remove(self.env['GITHUB_OUTPUT'])
        for name, changed, says in (('the workflow', {'workflow': 'g2'}, 'fork-head-gate.yml changed'),
                                    ('head-check', {'head_check': 't9'}, 'fork-ci/actions/head-check changed'),
                                    ('pr-queue', {'pr_queue': 't9'}, 'fork-ci/actions/pr-queue changed'),
                                    ('gone', {'pr_queue': None}, 'not all on'),
                                    ('workflow gone', {'workflow': not_found()}, 'not all on'),
                                    ('unreadable', {'workflow': answer(1, '', 'HTTP 502')}, 'could not read the gate')):
            with self.subTest(name):
                code, out, _ = self.target(routes={**run_routes(tip, old), **gate_routes(tip, **changed)},
                                           gate_repo=REPO, gate_sha=old, caller_sha=old)
                self.assertEqual(code, 0, out)
                self.failed(out, says)

    def test_an_unknown_answer_posts_so_that_judge_fails_the_head(self):   # never leave an older pass by silence
        routes = run_routes(self.TIP)
        routes[f'repos/{REPO}/contents/{CALLER}?ref={self.TIP}'] = answer(1, '', 'HTTP 502')
        for kw in ({'routes': routes}, {'listing': answer(1, '', 'HTTP 502')}):
            with self.subTest(kw=list(kw)):
                code, out, _ = self.target(**kw)
                self.assertEqual(code, 0, out)
                self.failed(out, 'HTTP 502')


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

    def routes(self, head, base=None):
        return run_routes(base or self.m1)

    def judge(self, routes, views, *extra, decide=H.decide, remote=None, listing=None, head=None, **kw):
        """views: {number: what gh pr view says} (a single view is #7's); listing: the open pull requests into main
        (default: #7 at the head)."""
        views = views if isinstance(views, dict) and 'isDraft' not in views else {7: views}
        head = head or next(iter(views.values())).get('headRefOid')
        listing = [(n, v.get('isDraft', False), head, 'main') for n, v in views.items()] if listing is None else listing
        fakes = {str(n): T.fake_gh((0, json.dumps(v))) for n, v in views.items()}

        def rest(*a):
            if a[:2] == ('pr', 'view'):
                return fakes[a[2]](*a)
            if a[:2] == ('api', 'graphql'):
                return fakes[next(x.split('=', 1)[1] for x in a if x.startswith('number='))](*a)
            raise AssertionError(a)
        tip = routes[f'repos/{REPO}/git/ref/heads/main']
        tip = tip['object'].get('sha') if isinstance(tip, dict) else self.m1
        gh, out = Api(routes, rest, lists={LISTED: listing}), []
        argv = ['judge', *run_argv(head, tip, **kw), '--config', T.CONFIG_PATH, '--work', self.work,
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
        self.assertEqual((v['sha'], v['conclusion'], v['prs']), (self.read, 'success', [7]))
        self.assertTrue(v['title'].startswith('pass: '), v['title'])
        self.assertIn(f'Judged against the base tip {self.m1}', v['summary'])
        self.assertIsNone(self.file('output'), 'judge only writes the verdict')
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

    def test_the_config_comes_from_the_default_branch_tip_never_the_pull_request(self):
        # The head's copy makes the owner a bot, which would hide the owner's stale lens.
        head = self.w.commit(T.base_files(cfg_raw={**T.CFG_RAW, 'bot_logins': ['owner']},
                                          **{'x.py': T.lines(extra=['unread'], at=20)}), [self.read])
        code, out, _ = self.judge(self.routes(head), T.pr([self.p.lens('correctness', self.read)], head=head))
        self.assertEqual(self.verdict()['conclusion'], 'failure', out)

    def test_the_required_lenses_are_the_callers(self):   # a pull request cannot drop one: it is no file of its own
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        self.judge(self.routes(self.read), view, required_lenses='design')
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

    def test_no_required_lens_is_a_failure_never_a_pass(self):   # lens minor: the old default "" passed a bare head
        head = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['unread'], at=20)}), [self.m1])
        for lenses in ('', '\n'):
            with self.subTest(lenses=lenses):
                code, out, _ = self.judge(self.routes(head), T.pr([], head=head), required_lenses=lenses)
                self.assertEqual((code, self.verdict()['conclusion']), (0, 'failure'), out)
                self.assertIn('required-lenses names no lens', self.verdict()['title'])

    def test_a_draft_never_gets_a_green_check_on_a_head_it_shares(self):   # lens finding 1 (corr85, sec85 F1a)
        # PR #7 (ready, into main) got 'failure': no lens read the head. Its author opens draft #8 at the same head.
        head = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['unread'], at=20)}), [self.read])
        views = {7: T.pr([], head=head), 8: T.pr([], head=head, draft=True)}
        self.judge(self.routes(head), views)
        self.assertEqual((self.verdict()['conclusion'], self.verdict()['prs']), ('failure', [7]))
        self.judge(self.routes(head), {8: views[8]})   # the draft alone: never judged, and judge fails it
        self.assertNotEqual(self.verdict()['conclusion'], 'success')
        self.assertIn('no open, ready pull request into main has the head', self.verdict()['title'])

    def test_a_pull_request_into_another_branch_never_decides_a_head(self):   # lens finding 2 (corr85, sec85 F1b)
        # main counts only the OWNER's lens comments. Branch `loose` (anyone who can push makes it) counts
        # COLLABORATOR comments too. PR #8 (head H -> loose) gets a collaborator's lens comment. Only main's rules and
        # only PRs into main decide H.
        strict = self.w.commit(T.base_files(cfg_raw={**T.CFG_RAW, 'lens_author_associations': ['OWNER']}), [self.m1])
        head = self.w.commit(T.base_files(cfg_raw={**T.CFG_RAW, 'lens_author_associations': ['OWNER']},
                                          **{'x.py': T.lines(extra=['unread'], at=20)}), [strict])
        view = T.pr([self.p.lens('correctness', head, assoc='COLLABORATOR', login='agent')], head=head)
        self.judge(self.routes(head, base=strict), {8: view}, listing=[(8, False, head, 'loose')])
        self.assertNotEqual(self.verdict()['conclusion'], 'success', self.verdict()['title'])
        self.assertIn('not judged', self.verdict()['title'])
        self.judge(self.routes(head, base=strict), {7: view})
        self.assertEqual(self.verdict()['conclusion'], 'failure', 'into main the same comment fails')

    def test_the_strictest_verdict_of_every_pull_request_with_the_head_wins(self):   # lens finding 2, second half
        ok = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        unread = T.pr([], head=self.read)
        for views in ({7: ok, 8: unread}, {7: unread, 8: ok}):
            with self.subTest(order=list(views.values()).index(unread)):
                code, out, gh = self.judge(self.routes(self.read), views)
                v = self.verdict()
                self.assertEqual((code, v['conclusion'], v['prs']), (0, 'failure', [7, 8]), out)
                bad = 8 if views[8] is unread else 7
                self.assertIn(f'#{bad}: required lens without a read', v['title'])
                self.assertIn('#### #7', v['summary'])
                self.assertIn('#### #8', v['summary'])
                self.assertEqual(sorted({a[2] for a, _ in gh.calls if a[:2] == ('pr', 'view')}), ['7', '8'])
        self.judge(self.routes(self.read), {7: ok, 8: ok})
        self.assertEqual(self.verdict()['conclusion'], 'success')

    def test_more_pull_requests_on_one_head_than_the_gate_judges_fail(self):
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        listing = [(n, False, self.read, 'main') for n in range(1, G.MAX_SAME_HEAD + 2)]
        code, out, _ = self.judge(self.routes(self.read), view, listing=listing)
        self.assertEqual((code, self.verdict()['conclusion']), (0, 'failure'), out)
        self.assertIn(f'at most {G.MAX_SAME_HEAD}', self.verdict()['title'])

    def test_judge_checks_the_run_again_and_fails_what_target_would_refuse(self):   # it may change after target
        view = T.pr([self.p.lens('correctness', self.read)], head=self.read)
        moved = caller_file(CALLER_TEXT.replace(GATE_SHA, 'c1' * 20), blob='b2')
        for name, kw in (('a re-run', {'run_attempt': '2'}),
                         ('an old caller', {'routes': run_routes(self.m1, now=moved, then=moved)}),
                         ('an unknown caller', {'routes': {**run_routes(self.m1, 'd4' * 20), f'repos/{REPO}/contents/'
                                                           f'{CALLER}?ref={self.m1}': answer(1, '', 'HTTP 502')},
                                                'caller_sha': 'd4' * 20}),
                         ('an unknown listing', {'listing': answer(1, '', 'HTTP 502')})):
            with self.subTest(name):
                routes = kw.pop('routes', self.routes(self.read))
                code, out, _ = self.judge(routes, view, **kw)
                v = self.verdict()
                self.assertEqual((code, v['sha'], v['conclusion']), (0, self.read, 'failure'), out)
                self.assertTrue(v['title'].startswith('fail: not judged: '), v['title'])

    def test_an_unreadable_default_branch_tip_is_a_failed_verdict(self):
        for base in (answer(1, '', 'HTTP 502'), {'object': {}}, not_found()):
            with self.subTest(base=str(base)[:30]):
                routes = self.routes(self.read)
                routes[f'repos/{REPO}/git/ref/heads/main'] = base
                code, out, _ = self.judge(routes, T.pr([self.p.lens('correctness', self.read)], head=self.read))
                v = self.verdict()
                self.assertEqual((code, v['sha'], v['conclusion']), (0, self.read, 'failure'), out)
                self.assertIn('could not read the tip of main', v['title'])

    def test_a_head_that_moves_during_the_run_fails_on_the_head_it_read(self):   # the new head gets its own run
        new = self.w.commit(T.base_files(**{'x.py': T.lines(extra=['fix', 'later'], at=20)}), [self.read])
        code, out, _ = self.judge(self.routes(self.read), T.pr([self.p.lens('correctness', self.read)], head=new),
                                  head=self.read)
        v = self.verdict()
        self.assertEqual((code, v['sha'], v['conclusion']), (0, self.read, 'failure'), out)
        self.assertIn('a newer head has its own run', v['title'] + v['summary'])
        code, out, _ = self.judge(self.routes(self.read), T.pr([], head=new), head=self.read, listing=[])
        self.assertIn('no open, ready pull request into main has the head', self.verdict()['title'])

    def test_a_fetch_that_fails_is_a_failed_verdict(self):   # GitHub dropped a commit, or no credentials
        code, out, _ = self.judge(self.routes(self.read), T.pr(head=self.read),
                                  remote=os.path.join(self.files, 'nowhere.git'))
        v = self.verdict()
        self.assertEqual((code, v['conclusion']), (0, 'failure'), out)
        self.assertIn('could not fetch the base and the head', v['title'])

    def test_a_crash_in_the_rules_is_a_failed_verdict_that_says_so(self):   # unknown never passes
        def boom(*a, **kw):
            raise RuntimeError('broken')
        code, out, _ = self.judge(self.routes(self.read), T.pr(head=self.read), decide=boom)
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

    def test_a_half_written_verdict_is_written_again_by_the_next_run(self):
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
        self.assertTrue(all(not line.startswith('::') or line.startswith(('::error::#7', '::error::head gate'))
                            for line in out.splitlines()), out)

    def test_bad_arguments_are_a_failed_verdict_before_github_is_read(self):
        for kw in ({'repo': 'not a repo'}, {'sha': 'ab'}, {'sha': 'A1' * 20}):
            with self.subTest(**kw):
                gh, out = Api(), []
                a = argparse.Namespace(**{**run_args(self.read, caller_sha=self.m1), **kw}, config=T.CONFIG_PATH,
                                       work=self.work, verdict=self.verdict_path, remote=self.w.remote)
                self.assertEqual((G.judge(a, gh, out.append, self.env), gh.calls), (0, []))
                self.assertEqual(self.verdict()['conclusion'], 'failure')

    def test_the_commits_come_from_github_by_default(self):
        seen = []
        real = G.judge
        try:
            G.judge = lambda a, *rest: seen.append(a.remote) or 0
            G.cli(['judge', *run_argv(self.read, self.m1), '--config', 'c', '--work', 'w', '--verdict', 'v'], env={})
        finally:
            G.judge = real
        self.assertEqual(seen, [f'https://github.com/{REPO}.git'])

    def test_the_required_lenses_argument_is_required(self):   # lens minor: no default
        argv = [x for x in run_argv(self.read, self.m1) if x not in ('--required-lenses', 'correctness')]
        for cmd in ('target', 'judge'):
            with self.subTest(cmd=cmd), self.assertRaises(SystemExit), \
                    unittest.mock.patch('sys.stderr', new_callable=io.StringIO):
                G.cli([cmd, *argv, '--config', 'c', '--work', 'w', '--verdict', 'v'][:None if cmd == 'judge' else
                                                                                   len(argv) + 1], env={})


# --- start and finish -----------------------------------------------------------------------------------------------
class Checks:
    """GitHub's check runs on the heads, as gh api writes them: POST makes one, PATCH completes one. `down`: how many
    of the next calls fail (an API error)."""
    def __init__(self):
        self.runs, self.calls, self.down = [], [], 0

    def __call__(self, *a, stdin=None):
        self.calls.append((a, stdin))
        if self.down:
            self.down -= 1
            return answer(1, '', 'gh: Server Error (HTTP 502)')
        body = json.loads(stdin)
        if a[:4] == ('api', '-X', 'POST', f'repos/{REPO}/check-runs'):
            self.runs.append({**body, 'id': len(self.runs) + 1})
            return answer(0, json.dumps(self.runs[-1]))
        if a[:3] == ('api', '-X', 'PATCH') and a[3].startswith(f'repos/{REPO}/check-runs/'):
            run = self.runs[int(a[3].rsplit('/', 1)[1]) - 1]
            run.update(body)
            return answer(0, json.dumps(run))
        raise AssertionError(a)

    def newest(self, sha, name=CHECK):
        """What the ruleset and the queue read: the newest check run of the name on the head."""
        mine = [r for r in self.runs if r['head_sha'] == sha and r['name'] == name]
        return (mine[-1].get('conclusion') if mine[-1]['status'] == 'completed' else mine[-1]['status']) if mine else None


class StartFinish(Files, unittest.TestCase):
    SHA = 'ab' * 20

    def setUp(self):
        Files.setUp(self)
        self.checks = Checks()
        self.verdict_path = os.path.join(self.files, 'verdict.json')

    def write(self, v):
        with open(self.verdict_path, 'w', encoding='utf-8') as f:
            f.write(v if isinstance(v, str) else json.dumps(v))

    def verdict(self, conclusion='success', **kw):
        return {'sha': self.SHA, 'prs': [7], 'conclusion': conclusion, 'title': f'{conclusion}: t', 'summary': 's', **kw}

    def start(self, env=None, check=CHECK):
        out = []
        if os.path.exists(self.env['GITHUB_OUTPUT']):
            os.remove(self.env['GITHUB_OUTPUT'])
        code = G.start(REPO, self.SHA, check, self.checks, out.append, {**self.env, **(env or {})}, lambda s: None)
        return code, '\n'.join(out), self.outputs().get('id', '')

    def finish(self, run, verdict=None):
        if verdict is not None:
            self.write(verdict)
        elif os.path.exists(self.verdict_path):
            os.remove(self.verdict_path)
        out = []
        code = G.finish(REPO, self.SHA, run, self.verdict_path, self.checks, out.append, self.env, lambda s: None)
        return code, '\n'.join(out)

    def a_run(self, conclusion=None, down_after_start=0):
        """One gate run on the head: start, judge (a verdict, or none when it stopped), finish."""
        code, out, run = self.start()
        if not run:
            return code, out
        self.checks.down = down_after_start
        return self.finish(run, None if conclusion is None else self.verdict(conclusion))

    def test_start_posts_an_in_progress_check_run_on_the_head_first(self):
        code, out, run = self.start(env={'GITHUB_RUN_ID': '99', 'GITHUB_SERVER_URL': 'https://github.com'})
        self.assertEqual((code, run, len(self.checks.calls)), (0, '1', 1), out)
        a, stdin = self.checks.calls[0]
        self.assertEqual(a, ('api', '-X', 'POST', f'repos/{REPO}/check-runs', '--input', '-'))
        body = json.loads(stdin)
        self.assertEqual({k: body[k] for k in ('name', 'head_sha', 'status', 'details_url')},
                         {'name': CHECK, 'head_sha': self.SHA, 'status': 'in_progress',
                          'details_url': f'https://github.com/{REPO}/actions/runs/99'})
        self.assertNotIn('conclusion', body, 'in progress never passes')

    def test_finish_completes_that_check_run_with_the_verdict(self):
        for conclusion in ('success', 'failure'):
            with self.subTest(conclusion=conclusion):
                code, out = self.a_run(conclusion)
                self.assertEqual((code, self.checks.newest(self.SHA)), (0, conclusion), out)
                a, stdin = self.checks.calls[-1]
                self.assertEqual(a[:4], ('api', '-X', 'PATCH', f'repos/{REPO}/check-runs/{len(self.checks.runs)}'))
                self.assertEqual(json.loads(stdin)['status'], 'completed')

    def test_a_rejudge_that_stops_or_hits_an_api_error_leaves_no_pass(self):   # finding F2 (sec85 test)
        # Run 1 passed the head. Then the override is deleted and run 2 starts: whatever stops it, the newest gate
        # check run on the head must not be the old pass.
        stops = {'judge stopped (no verdict)': {'conclusion': None},
                 'finish hit API errors': {'conclusion': 'failure', 'down_after_start': 3},
                 'judge stopped and finish hit API errors': {'conclusion': None, 'down_after_start': 3}}
        for name, kw in stops.items():
            with self.subTest(name):
                self.checks = Checks()
                self.assertEqual(self.a_run('success')[0], 0)
                self.assertEqual(self.checks.newest(self.SHA), 'success')
                code, out = self.a_run(**kw)
                self.assertEqual(code, 1, 'the job is red too')
                self.assertIn(self.checks.newest(self.SHA), ('failure', 'in_progress'), out)

    def test_a_stop_before_the_verdict_completes_the_check_run_as_a_failure(self):
        for verdict in (None, '{"half": ', [], self.verdict(sha='cd' * 20), self.verdict('neutral'),
                        self.verdict('SUCCESS'), self.verdict(title=None),
                        {k: x for k, x in self.verdict().items() if k != 'summary'}):
            with self.subTest(verdict=str(verdict)[:40]):
                self.checks = Checks()
                _, _, run = self.start()
                code, out = self.finish(run, verdict)
                self.assertEqual((code, self.checks.newest(self.SHA)), (1, 'failure'), out)
                self.assertIn('stopped before its verdict', self.checks.runs[-1]['output']['title'])

    def test_a_start_github_refuses_posts_nothing_and_judges_nothing(self):   # the one case an old verdict stands
        self.checks.down = 3
        code, out, run = self.start()
        self.assertEqual((code, run, self.checks.runs, len(self.checks.calls)), (1, '', [], 3), out)
        self.assertIn('nothing on this head changed', out)

    def test_a_write_that_fails_once_is_tried_again(self):   # both writes may repeat
        self.checks.down = 1
        code, out, run = self.start()
        self.assertEqual((code, run), (0, '1'), out)
        self.checks.down = 2
        code, out = self.finish(run, self.verdict())
        self.assertEqual((code, self.checks.newest(self.SHA)), (0, 'success'), out)

    def test_bad_arguments_make_no_call(self):
        out = []
        self.assertEqual(G.start(REPO, self.SHA, '', self.checks, out.append, self.env), 1)
        self.assertEqual(G.start(REPO, 'ab', CHECK, self.checks, out.append, self.env), 1)
        self.assertEqual(G.finish(REPO, self.SHA, '', self.verdict_path, self.checks, out.append, self.env), 1)
        self.assertEqual(G.finish(REPO, self.SHA, '1; x', self.verdict_path, self.checks, out.append, self.env), 1)
        self.assertEqual(self.checks.calls, [])

    def test_long_texts_are_cut_to_what_github_takes(self):
        _, _, run = self.start()
        self.finish(run, self.verdict('failure', title='t' * 500, summary='s' * 70000))
        body = json.loads(self.checks.calls[-1][1])
        self.assertEqual((len(body['output']['title']), len(body['output']['summary'])), (G.TITLE_MAX, G.SUMMARY_MAX))
        _, _, run = self.start()
        self.assertNotIn('details_url', self.checks.runs[-1], 'no run id: no link')

    def test_a_verdict_text_is_cut_too(self):
        rows = [(f'lens{i}', False, 'x' * 100) for i in range(1000)]
        title, text = G.verdict_text(False, 'r' * 300, [(7, 1, False, 'r', rows)], self.SHA, self.SHA)
        self.assertLessEqual((len(title), len(text)), (G.TITLE_MAX, G.SUMMARY_MAX))
        self.assertTrue(title.startswith('fail: '))
        self.assertIn('(cut: the run log has every row)', text)


# --- the workflow, step by step ------------------------------------------------------------------------------------
class Workflow(Files, unittest.TestCase):
    """fork-head-gate.yml's steps and their conditions (fork-head-gate-workflow.test.py checks the file says the same):
    guard job: guard (continue-on-error), prs, a failing step when guard said nothing; gate job when the guard job
    passed and listed heads: guard --environment-only (continue-on-error), target, (token), start when post=true,
    judge when start posted, finish always after start. Round 2's rule: every way a run ends leaves the head "not
    passed" unless it judged and passed. Each case: run 1 passes H (an owner override), the override is deleted,
    run 2 meets the case, and H must not show run 1's pass."""
    H, TIP, OLD = 'a1' * 20, 'e5' * 20, 'd4' * 20
    ENV_PATH = Guard.ENV_PATH

    def routes(self, pull=None, env=None, tip=None, caller_sha=None, graphql=None, **kw):
        routes = {**Guard.routes(self, env=env, graphql=graphql), **run_routes(tip or self.TIP, caller_sha, **kw)}
        routes[f'repos/{REPO}/pulls/7'] = pull or {'state': 'open', 'draft': False, 'merged': False,
                                                   'head': {'sha': self.H}, 'base': {'ref': 'main'}}
        return routes

    def step(self, name):
        self.env['GITHUB_OUTPUT'] = os.path.join(self.files, name)
        if os.path.exists(self.env['GITHUB_OUTPUT']):   # each run's step has its own outputs
            os.remove(self.env['GITHUB_OUTPUT'])
        return lambda: dict(line.split('=', 1) for line in (self.file(name) or '').splitlines())

    def run_workflow(self, checks, routes, passes, event='issue_comment', listing=None, secret='false',
                     event_sha='', git_head=None, **kw):
        """One run of the reusable workflow; returns what happened, step by step."""
        listing = [(7, False, self.H, 'main')] if listing is None else listing
        gh, out, did = Api(routes, lists={LISTED: listing}), [], []
        guard = self.step('guard')
        G.guard(REPO, ENV_NAME, KEY, secret, False, gh, out.append, self.env)
        listed = self.step('prs')
        prs_code = G.prs(REPO, event, '7', gh, out.append, self.env, event_sha, git_head)
        guard_said = guard().get('guard', '')
        if prs_code != 0 or guard_said == '':
            return did + ['guard job failed'], '\n'.join(out)
        heads = json.loads(listed().get('prs', '[]'))
        if not heads:
            return did + ['nothing to judge'], '\n'.join(out)
        for head in heads:
            envguard = self.step('envguard-' + head['sha'])
            G.guard(REPO, ENV_NAME, KEY, '', True, gh, out.append, self.env)
            argv = run_argv(head['sha'], kw.pop('tip', self.TIP), guard=guard_said,
                            environment_guard=envguard().get('guard', ''), **kw)
            target = self.step('target-' + head['sha'])
            G.cli(['target', *argv], env=self.env, gh=gh, out=out.append)
            if target().get('post') != 'true':
                did.append('posted nothing')
                continue
            start = self.step('start-' + head['sha'])
            G.start(REPO, head['sha'], CHECK, checks, out.append, self.env, lambda s: None)
            run = start().get('id', '')
            if not run:
                did.append('start failed')
                continue
            verdict = os.path.join(self.files, 'verdict.json')
            work = tempfile.mkdtemp(dir=self.files)
            decide = lambda *a: (0 if passes else 1, passes, 'override' if passes else 'no lens read the head', [])
            with unittest.mock.patch.object(G, 'fetch', lambda *a: None):
                G.cli(['judge', *argv, '--config', 'c', '--work', work, '--verdict', verdict], env=self.env, gh=gh,
                      out=out.append, decide=decide, sleep=lambda s: None)
            G.finish(REPO, head['sha'], run, verdict, checks, out.append, self.env, lambda s: None)
            did.append(checks.newest(head['sha']))
        return did, '\n'.join(out)

    def revoke(self, routes, **kw):
        """Run 1 passes H; run 2 (the override deleted) meets `routes`/`kw`. H's newest check run after run 2."""
        checks = Checks()
        did, out = self.run_workflow(checks, self.routes(), True)
        self.assertEqual((did, checks.newest(self.H)), (['success'], 'success'), 'control: run 1 passes H\n' + out)
        did, out = self.run_workflow(checks, routes, False, **kw)
        return checks.newest(self.H), did, out

    def test_a_revocation_is_judged(self):   # control: nothing in the way, the deletion fails H
        newest, did, out = self.revoke(self.routes())
        self.assertEqual((newest, did), ('failure', ['failure']), out)

    def test_one_5xx_or_rate_limit_in_the_guard_job_is_read_again(self):   # corr85r (c), sec85r R1: retried
        for path in (f'repos/{REPO}/pulls/7', Guard.ENV_PATH):
            with self.subTest(path=path):
                routes = self.routes()
                routes[path] = Seq([answer(1, '', 'gh: Server Error (HTTP 502)'),
                                    answer(1, '', 'gh: API rate limit exceeded (HTTP 403)'), routes[path]])
                newest, did, out = self.revoke(routes)
                self.assertEqual((newest, did), ('failure', ['failure']), out)

    def test_a_guard_that_cannot_prove_the_key_still_posts_a_failure(self):   # sec85r R1: rate limited every try
        rules = self.ENV_PATH + '/deployment-branch-policies?per_page=100'
        rate = answer(1, '', 'gh: API rate limit exceeded (HTTP 403)')
        for name, routes in (('any branch may deploy', self.routes(env={'name': ENV_NAME,
                                                                         'deployment_branch_policy': None})),
                             ('the rules rate limited', {**self.routes(), rules: rate}),
                             ('every read but the Environment rate limited',   # it was read: a failure is posted
                              {**{k: rate for k in self.routes()}, self.ENV_PATH: self.routes()[self.ENV_PATH]}),
                             ('every REST read rate limited, GraphQL shows the Environment',   # its own limit
                              {**{k: rate for k in self.routes()}, 'graphql': self.routes()['graphql']})):
            with self.subTest(name):
                newest, did, out = self.revoke(routes, git_head=lambda *a: (self.H, None))
                self.assertEqual(newest, 'failure', out)
                self.assertIn('the guard did not pass', out)
        newest, did, out = self.revoke(self.routes(), secret='true')   # a repository secret appeared
        self.assertEqual(newest, 'failure', out)

    def test_a_pull_request_read_that_fails_every_try_still_finds_the_head(self):   # corr85r (c): fallbacks
        down = Seq([answer(1, '', 'gh: Server Error (HTTP 502)')])
        newest, did, out = self.revoke(self.routes(pull=down), git_head=lambda *a: (self.H, None))
        self.assertEqual(newest, 'failure', out)
        newest, did, out = self.revoke(self.routes(pull=down), event='pull_request_target', event_sha=self.H)
        self.assertEqual(newest, 'failure', out)

    def test_a_local_gate_is_judged_when_only_other_files_changed_on_main(self):   # corr85r (a), sec85r target
        routes = self.routes(caller_sha=self.OLD)
        newest, did, out = self.revoke(routes, gate_repo=REPO, gate_sha=self.OLD, caller_sha=self.OLD)
        self.assertEqual((newest, did), ('failure', ['failure']), out)
        self.assertIn('judging', out)
        routes = {**self.routes(caller_sha=self.OLD), **gate_routes(self.TIP, head_check='t9')}
        newest, did, out = self.revoke(routes, gate_repo=REPO, gate_sha=self.OLD, caller_sha=self.OLD)
        self.assertEqual(newest, 'failure', out)   # the gate itself changed: a failure, not judged
        self.assertIn('fork-ci/actions/head-check changed', out)

    def test_an_old_caller_or_a_re_run_posts_a_failure(self):   # corr85r (b): a failure never passes
        routes = self.routes(caller_sha=self.OLD, now=caller_file(CALLER_TEXT + '# new lens\n', 'b2'))
        for name, kw in (('the caller changed', {'routes': routes, 'caller_sha': self.OLD}),
                         ('a re-run', {'run_attempt': '2'})):
            with self.subTest(name):
                newest, did, out = self.revoke(kw.pop('routes', self.routes()), **kw)
                self.assertEqual((newest, did), ('failure', ['failure']), out)

    def test_a_revocation_while_a_draft_fails_the_head(self):   # corr85r (d): ready starts from red
        for event in ('pull_request_target', 'issue_comment'):
            with self.subTest(event=event):
                draft = {'state': 'open', 'draft': True, 'merged': False, 'head': {'sha': self.H},
                         'base': {'ref': 'main'}}
                newest, did, out = self.revoke(self.routes(pull=draft), event=event,
                                               listing=[(7, True, self.H, 'main')])
                self.assertEqual(newest, 'failure', out)
                self.assertIn('no open, ready pull request into main has the head', out)

    def test_the_cases_that_post_nothing_are_only_those_that_cannot(self):   # the README's list, exactly
        newest, did, out = self.revoke(self.routes(env=not_found()))   # no Environment: no key, no token
        self.assertEqual((newest, did), ('success', ['guard job failed']), out)
        rate = answer(1, '', 'gh: API rate limit exceeded (HTTP 403)')   # the Environment never seen (sec85r2 n1)
        for graphql in (rate, GRAPHQL_MISSING):
            newest, did, out = self.revoke(self.routes(env=rate, graphql=graphql), git_head=lambda *a: (self.H, None))
            self.assertEqual((newest, did), ('success', ['guard job failed']), out)
        down = Seq([answer(1, '', 'gh: Server Error (HTTP 502)')])   # GitHub's API and git both down
        newest, did, out = self.revoke(self.routes(pull=down), git_head=lambda *a: (None, 'git ls-remote: down'))
        self.assertEqual((newest, did), ('success', ['guard job failed']), out)
        checks = Checks()   # the first write fails every try
        self.run_workflow(checks, self.routes(), True)
        checks.down = 3
        did, out = self.run_workflow(checks, self.routes(), False)
        self.assertEqual((did, checks.newest(self.H)), (['start failed'], 'success'), out)


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
