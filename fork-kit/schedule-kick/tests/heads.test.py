"""schedule-kick's heads part (lib/schedulekick/heads.py): every trusted head gets a run. A fake GitHub in the
process (one GraphQL answer a pass, dispatches recorded), a fixed clock and a temporary state folder. No root, no
systemd, no network.

  python3 -I -B fork-kit/schedule-kick/tests/heads.test.py

The state question, answered here: runs again (a head sent once is never sent again, also when its run is slow to
show; a lost state file with the run showing sends nothing), stops half-way (the head is recorded as sent before the
call; a stop after the call, before the state is written, sends nothing again), old, broken or empty state (no runs
at all, an empty repository, a broken heads part), a head force-pushed mid-pass (skipped this pass, the old head
dropped from the state), and API errors (wait and retry; a rate limit or a low budget pauses; a refusal of the
inputs is recorded and not sent again). Trust: a pull request from another fork is never dispatched.
RedCheck breaks each rule in a copy of lib/schedulekick/ and shows its test then fails.
"""
import datetime, io, json, os, shutil, sys, tempfile, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kick_fixture import FakeApi, at, github, heads, kick, red_check  # noqa: E402

THIS = os.path.abspath(__file__)
NOW = at('2026-11-28T03:00:00')
REPO = 'example-owner/app'
WF = 'fork-tests.yml'
PATH = f'/{REPO}/actions/workflows/{WF}'
MAIN, A, B, C, D = ('1' * 40), ('a' * 40), ('b' * 40), ('c' * 40), ('d' * 40)


def suites(*paths, total=None):
    nodes = [{'workflowRun': {'workflow': {'resourcePath': p}}} for p in paths]
    return {'totalCount': len(nodes) if total is None else total, 'nodes': nodes}


def commit(sha, *paths, total=None):
    return {'oid': sha, 'checkSuites': suites(*paths, total=total)}


def pr(number, sha, branch=None, runs=(), cross=False, head_repo=REPO, last=None):
    return {'number': number, 'isCrossRepository': cross, 'headRefName': branch or f'feature-{number}',
            'headRefOid': sha, 'headRepository': {'nameWithOwner': head_repo} if head_repo else None,
            'commits': {'nodes': [{'commit': commit(last or sha, *runs)}]}}


def answer(main_runs=(PATH,), prs=(), total=None, remaining=4000):
    return {'rateLimit': {'remaining': remaining, 'resetAt': '2026-11-28T03:30:00Z'},
            'repository': {'nameWithOwner': REPO,
                           'defaultBranchRef': {'name': 'main', 'target': commit(MAIN, *main_runs)},
                           'pullRequests': {'totalCount': len(prs) if total is None else total,
                                            'nodes': list(prs)}}}


class Heads(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='schedule-kick-heads-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.state_dir = os.path.join(self.tmp, 'state')
        self.api = FakeApi(NOW)
        self.cfg = kick.Config('cfg.json', ('tokenFile', '/x'), [], [kick.HeadsEntry(REPO, WF, 10, 3)])
        self.alerts = []

    def alert(self, text):
        self.alerts.append(text)
        return True, ''

    def tick(self, now, cfg=None):
        out, err = io.StringIO(), io.StringIO()
        code = kick.tick(cfg or self.cfg, self.state_dir, self.api, now=now, out=out, err=err, alert=self.alert)
        return code, out.getvalue() + err.getvalue()

    def sent(self):
        return [(c[3], c[4]['ref']) for c in self.api.dispatches()]

    def status(self):
        with open(os.path.join(self.state_dir, 'status.json')) as f:
            return json.load(f)['entries'][f'heads {REPO} {WF}']

    def state(self):
        with open(os.path.join(self.state_dir, 'state.json')) as f:
            return json.load(f)

    # --- no run at all: wait the grace, then one dispatch each --------------------------------------------------------
    def test_a_head_without_a_run_is_sent_once_after_the_grace(self):
        self.api.answer = answer(main_runs=(), prs=[pr(7, A)])
        self.assertEqual(self.tick(NOW)[0], 0)
        self.assertEqual(self.sent(), [])                                   # first seen: GitHub's run may be coming
        self.tick(NOW + datetime.timedelta(minutes=5))
        self.assertEqual(self.sent(), [])                                   # inside the grace
        code, out = self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.sent(), [('main', MAIN), ('feature-7', A)])  # the default branch first
        self.assertEqual(self.api.dispatches()[0][4], {'ref': MAIN, 'head': 'true'})
        self.assertEqual(self.status()['result'], 'dispatched')

    def test_a_head_with_a_run_of_the_workflow_needs_nothing(self):
        other = f'/{REPO}/actions/workflows/fork-checks.yml'
        self.api.answer = answer(prs=[pr(7, A, runs=(other, PATH.upper())), pr(8, B, runs=(other,))])
        for minutes in (0, 10):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [('feature-8', B)])                   # a run of another workflow does not count

    def test_an_empty_repository_and_no_pull_requests_is_fine(self):
        self.api.answer = {'rateLimit': None, 'repository': {'nameWithOwner': REPO, 'defaultBranchRef': None,
                                                             'pullRequests': {'totalCount': 0, 'nodes': []}}}
        code, out = self.tick(NOW)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.status()['result'], 'ran')

    # --- trust -------------------------------------------------------------------------------------------------------
    def test_a_pull_request_from_another_fork_is_never_sent(self):
        self.api.answer = answer(prs=[pr(7, A, cross=True, head_repo='someone/app'),
                                      pr(8, B, cross=False, head_repo='someone/app'),
                                      pr(9, C, cross=True)])
        for minutes in (0, 10, 20):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [])

    def test_a_pull_request_whose_head_repository_is_gone_is_never_sent(self):
        self.api.answer = answer(prs=[pr(10, D, head_repo=None)])
        for minutes in (0, 10):
            self.assertEqual(self.tick(NOW + datetime.timedelta(minutes=minutes))[0], 0)
        self.assertEqual(self.sent(), [])

    # --- runs again ----------------------------------------------------------------------------------------------------
    def test_a_head_is_sent_once_even_when_its_run_is_slow_to_show(self):
        self.api.answer = answer(prs=[pr(7, A)])
        for minutes in (0, 10, 15, 20, 25):
            self.assertEqual(self.tick(NOW + datetime.timedelta(minutes=minutes))[0], 0)
        self.assertEqual(self.sent(), [('feature-7', A)])

    def test_a_lost_state_file_with_the_run_showing_sends_nothing(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        os.unlink(os.path.join(self.state_dir, 'state.json'))
        self.api.answer = answer(prs=[pr(7, A, runs=(PATH,))])
        for minutes in (15, 30):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [('feature-7', A)])

    def test_two_pull_requests_on_one_commit_get_one_dispatch(self):
        self.api.answer = answer(prs=[pr(7, A, 'one'), pr(8, A, 'two')])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(self.sent(), [('one', A)])

    def test_at_most_max_per_pass_then_the_rest_on_later_passes(self):
        self.api.answer = answer(prs=[pr(n, f'{n:040x}') for n in range(1, 6)])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(len(self.sent()), 3)                               # maxPerPass 3: the updated-last first
        self.assertEqual([s[0] for s in self.sent()], ['feature-1', 'feature-2', 'feature-3'])
        self.tick(NOW + datetime.timedelta(minutes=15))
        self.assertEqual(len(self.sent()), 5)

    def test_one_graphql_call_a_pass_whatever_the_number_of_heads(self):
        self.api.answer = answer(prs=[pr(n, f'{n:040x}') for n in range(1, 40)])
        self.tick(NOW)
        self.assertEqual([c[0] for c in self.api.calls], ['graphql'])
        self.assertEqual(self.api.calls[0][2], {'owner': 'example-owner', 'name': 'app'})

    # --- stops half-way --------------------------------------------------------------------------------------------
    def test_the_head_is_recorded_as_sent_before_the_call(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        seen = []
        self.api.after_dispatch = lambda: seen.append(json.load(open(os.path.join(self.state_dir, 'state.json'))))
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(len(seen), 1)
        self.assertIn(A, seen[0]['heads'][f'heads {REPO.lower()} {WF}']['sent'])

    def test_a_stop_after_the_call_before_the_state_is_written_sends_nothing_again(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)

        def stop():
            raise KeyboardInterrupt('stopped half-way')
        self.api.after_dispatch = stop
        with self.assertRaises(KeyboardInterrupt):
            self.tick(NOW + datetime.timedelta(minutes=10))
        self.api.after_dispatch = None
        for minutes in (15, 20):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [('feature-7', A)])

    def test_a_sent_head_whose_run_never_shows_is_reported_lost_once(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        code, out = self.tick(NOW + datetime.timedelta(minutes=40))
        self.assertEqual(code, 1)
        self.assertIn('ALERT: dispatched but no run after 30 minutes', out)
        code, out = self.tick(NOW + datetime.timedelta(minutes=45))
        self.assertEqual(code, 0, out)                                      # said once; not sent again
        self.assertEqual(self.sent(), [('feature-7', A)])
        self.assertEqual(len(self.alerts), 1)                               # and through the alert channel, once
        self.assertIn(f'#7 {A} (feature-7) was dispatched', self.alerts[0])

    # --- force push mid-pass, old heads --------------------------------------------------------------------------------
    def test_a_head_that_moved_while_github_answered_waits_for_the_next_pass(self):
        self.api.answer = answer(prs=[pr(7, A, last=B)])
        self.tick(NOW)
        code, out = self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(self.sent(), [])
        self.assertIn('#7: its head moved while GitHub answered', out)

    def test_an_old_head_is_dropped_from_the_state(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.api.answer = answer(prs=[pr(7, B, runs=(PATH,))])              # force-pushed; its own run came
        self.tick(NOW + datetime.timedelta(minutes=15))
        bucket = self.state()['heads'][f'heads {REPO.lower()} {WF}']
        self.assertEqual(bucket['seen'], {})
        self.assertIn(A, bucket['sent'])                                    # kept a while: it may come back
        self.tick(NOW + datetime.timedelta(days=heads.MARK_DAYS, minutes=11))
        bucket = self.state()['heads'][f'heads {REPO.lower()} {WF}']
        self.assertEqual((bucket['seen'], bucket['sent']), ({}, {}))

    def test_a_head_missing_from_one_snapshot_keeps_its_lost_mark(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.tick(NOW + datetime.timedelta(minutes=45))                     # lost
        self.api.answer = answer(prs=[pr(7, A, last=B)])                    # one pass: moved while GitHub answered
        self.tick(NOW + datetime.timedelta(minutes=50))
        self.api.answer = answer(prs=[pr(7, A)])
        for minutes in (55, 70, 90):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [('feature-7', A)])

    def test_more_check_suites_than_read_and_none_a_run_sends_nothing(self):
        prs = [pr(7, A)]
        prs[0]['commits']['nodes'][0]['commit']['checkSuites'] = suites('/x/y/actions/workflows/z.yml', total=80)
        self.api.answer = answer(prs=prs)
        self.tick(NOW)
        code, out = self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(self.sent(), [])
        self.assertIn('more check suites than read', out)

    def test_a_broken_heads_state_reads_as_empty(self):
        os.makedirs(self.state_dir, mode=0o700)
        with open(os.path.join(self.state_dir, 'state.json'), 'w') as f:
            json.dump({'version': 1, 'entries': {}, 'heads': ['broken']}, f)
        self.api.answer = answer(prs=[pr(7, A, runs=(PATH,))])
        code, out = self.tick(NOW)
        self.assertEqual(code, 0, out)
        self.assertIn('heads part is broken', out)

    # --- API errors and the budget ---------------------------------------------------------------------------------
    def test_an_api_error_waits_then_tries_again(self):
        self.api.fail['graphql'] = github.ApiError('POST /graphql: HTTP 502', status=502)
        code, out = self.tick(NOW)
        self.assertEqual(code, 1)
        self.tick(NOW + datetime.timedelta(minutes=2))
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'graphql']), 1)     # waits 5 minutes
        del self.api.fail['graphql']
        self.api.answer = answer(prs=[pr(7, A, runs=(PATH,))])
        self.assertEqual(self.tick(NOW + datetime.timedelta(minutes=6))[0], 0)

    def test_a_graphql_error_answer_is_an_error_not_an_empty_list(self):
        with self.assertRaises(github.ApiError):
            github.graphql_data({'errors': [{'message': 'Something went wrong'}], 'data': None}, 'q')
        with self.assertRaises(github.ApiError) as e:
            github.graphql_data({'errors': [{'type': 'RATE_LIMITED', 'message': 'API rate limit exceeded'}]}, 'q')
        self.assertTrue(e.exception.rate_limited)
        self.api.answer = {'repository': None}
        self.assertEqual(self.tick(NOW)[0], 1)

    def test_a_rate_limit_pauses_the_tick(self):
        self.api.fail['graphql'] = github.ApiError('rate limited', rate_limited=True, retry_after=600)
        self.tick(NOW)
        del self.api.fail['graphql']
        self.api.answer = answer()
        code, out = self.tick(NOW + datetime.timedelta(minutes=5))
        self.assertEqual(self.status()['result'], 'paused')
        self.assertEqual(len([c for c in self.api.calls if c[0] == 'graphql']), 1)

    def test_a_low_graphql_budget_pauses(self):
        self.api.answer = answer(remaining=50)
        self.tick(NOW)
        self.assertIn('pauseUntil', self.state())

    def test_a_rest_answer_after_the_graphql_call_does_not_hide_a_low_graphql_budget(self):
        self.api.answer = answer(main_runs=())
        self.tick(NOW)
        self.api.answer = answer(main_runs=(), remaining=50)
        self.api.after_dispatch = lambda: setattr(self.api, 'remaining', 4999)    # the dispatch's REST headers
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(self.sent(), [('main', MAIN)])
        self.assertIn('pauseUntil', self.state())
        self.assertIn('GraphQL API calls left', self.status()['message'])

    def test_a_refusal_of_the_inputs_is_recorded_and_not_sent_again(self):
        # a branch whose workflow file is older than the head input: HTTP 422 "Unexpected inputs provided"
        self.api.answer = answer(prs=[pr(7, A), pr(8, B)])
        self.tick(NOW)
        self.api.fail['dispatch'] = github.ApiError('HTTP 422 Unexpected inputs provided: ["head"]', status=422)
        code, out = self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(code, 1)
        self.assertIn('refused (not sent again)', out)
        del self.api.fail['dispatch']
        self.tick(NOW + datetime.timedelta(minutes=15))
        self.assertEqual(len(self.api.dispatches()), 2)                     # #7 and #8, each tried once
        self.assertEqual(len(self.alerts), 2)                               # each refusal said once

    def test_a_422_for_a_switched_off_workflow_waits_and_tries_again(self):
        # #517: the workflow is off for a few seconds; keep-on turns it back on
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.api.fail['dispatch'] = github.ApiError(
            "POST ...: HTTP 422 Cannot trigger a 'workflow_dispatch' on a disabled workflow", status=422)
        self.assertEqual(self.tick(NOW + datetime.timedelta(minutes=10))[0], 1)
        del self.api.fail['dispatch']
        for minutes in (16, 30):
            self.tick(NOW + datetime.timedelta(minutes=minutes))
        self.assertEqual(self.sent(), [('feature-7', A), ('feature-7', A)])
        self.assertEqual(self.alerts, [])

    def test_refusals_count_toward_max_per_pass(self):
        self.api.answer = answer(prs=[pr(n, f'{n:040x}') for n in range(1, 11)])
        self.tick(NOW)
        self.api.fail['dispatch'] = github.ApiError('HTTP 422 Unexpected inputs provided: ["head"]', status=422)
        self.tick(NOW + datetime.timedelta(minutes=10))
        self.assertEqual(len(self.api.dispatches()), 3)

    def test_a_dispatch_that_fails_otherwise_is_tried_again_after_the_wait(self):
        self.api.answer = answer(prs=[pr(7, A)])
        self.tick(NOW)
        self.api.fail['dispatch'] = github.ApiError('HTTP 500', status=500)
        self.assertEqual(self.tick(NOW + datetime.timedelta(minutes=10))[0], 1)
        del self.api.fail['dispatch']
        self.tick(NOW + datetime.timedelta(minutes=16))
        self.assertEqual(self.sent(), [('feature-7', A), ('feature-7', A)])   # the failed one, then the retry


class Config(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='schedule-kick-heads-cfg-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.token = os.path.join(self.tmp, 'token')
        with open(self.token, 'w') as f:
            f.write('t0ken\n')
        os.chmod(self.token, 0o600)

    def load(self, **raw):
        path = os.path.join(self.tmp, 'config.json')
        with open(path, 'w') as f:
            json.dump({'version': 1, 'auth': {'tokenFile': self.token}, **raw}, f)
        return kick.load_config(path)

    def test_heads_and_keep_on_alone_are_a_config(self):
        cfg = self.load(heads=[{'repo': REPO, 'workflow': WF}], keepOn=[{'repo': REPO, 'workflows': [WF]}])
        self.assertEqual((cfg.entries, cfg.heads[0].per_pass, cfg.heads[0].grace), ([], 3, datetime.timedelta(minutes=10)))
        self.assertEqual(cfg.keep_on[0].workflows, [WF])

    def test_bad_heads_and_keep_on_are_refused(self):
        for raw, words in (
                ({}, 'nothing to do'),
                ({'heads': [{'repo': REPO, 'workflow': WF, 'inputs': {}}]}, "unknown key 'inputs'"),
                ({'heads': [{'repo': REPO, 'workflow': WF, 'maxPerPass': 0}]}, 'maxPerPass'),
                ({'heads': [{'repo': REPO, 'workflow': WF, 'graceMinutes': True}]}, 'graceMinutes'),
                ({'heads': [{'repo': REPO, 'workflow': WF}] * 2}, 'listed twice'),
                ({'heads': [{'repo': 'nope', 'workflow': WF}]}, 'OWNER/NAME'),
                ({'keepOn': [{'repo': REPO, 'workflows': []}]}, 'workflows must be a list'),
                ({'keepOn': [{'repo': REPO, 'workflows': [WF, WF]}]}, 'twice'),
                ({'keepOn': [{'repo': REPO, 'workflows': ['../x.yml']}]}, 'not a workflow file name'),
                ({'keepOn': [{'repo': REPO, 'workflows': [WF]}, {'repo': REPO.upper(), 'workflows': [WF]}]},
                 'listed twice')):
            with self.subTest(raw=raw):
                with self.assertRaises(kick.ConfigError) as e:
                    self.load(**raw)
                self.assertIn(words, str(e.exception))


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        red_check(self, THIS, {
            'the grace': ('heads', 'if now - first < entry.grace or', 'if False or',
                          'Heads.test_a_head_without_a_run_is_sent_once_after_the_grace'),
            'trust: same repository only': ('heads', "if pr.get('isCrossRepository') is not False or",
                                            'if False and', 'Heads.test_a_pull_request_from_another_fork_is_never_sent'),
            'sent once': ('heads', '        if mark is not None:\n', '        if False:\n',
                          'Heads.test_a_head_is_sent_once_even_when_its_run_is_slow_to_show'),
            'recorded before the call': ('heads', '        save()\n', '',
                                         'Heads.test_the_head_is_recorded_as_sent_before_the_call'),
            'a run counts': ('heads', 'return True\n    total', 'return False\n    total',
                             'Heads.test_a_head_with_a_run_of_the_workflow_needs_nothing'),
            'max per pass': ('heads', ' or tried >= entry.per_pass', '',
                             'Heads.test_at_most_max_per_pass_then_the_rest_on_later_passes'),
            'moved head skipped': ('heads', "commit.get('oid') != pr.get('headRefOid')", 'False',
                                   'Heads.test_a_head_that_moved_while_github_answered_waits_for_the_next_pass'),
            'unknown suites': ('heads', "type(total) is int and total <= len(suites['nodes'])", 'True',
                               'Heads.test_more_check_suites_than_read_and_none_a_run_sends_nothing'),
            'refused once': ('heads', 'if permanent(e):', 'if False:',
                             'Heads.test_a_refusal_of_the_inputs_is_recorded_and_not_sent_again'),
            'lost said': ("heads", "mark['result'] = 'lost'", "pass",
                          'Heads.test_a_sent_head_whose_run_never_shows_is_reported_lost_once'),
            'graphql budget read': ('heads', "api.graphql_remaining = limit['remaining']", 'pass',
                                    'Heads.test_a_low_graphql_budget_pauses'),
            'a disabled-workflow 422 retries': ('heads', "e.status == 422 and any(", "e.status == 422 or any(",
                                                'Heads.test_a_422_for_a_switched_off_workflow_waits_and_tries_again'),
            'refusals count': ('heads', '        tried += 1\n', '',
                               'Heads.test_refusals_count_toward_max_per_pass'),
            'marks kept a while': ('heads', "if k in current or (parse_time(v.get('at')) or keep_after) > keep_after}",
                                   'if k in current}',
                                   'Heads.test_a_head_missing_from_one_snapshot_keeps_its_lost_mark'),
            'budgets apart': ('kick', "('GraphQL', 'graphql_remaining', 'graphql_reset')", "('GraphQL', 'remaining', 'reset')",
                              'Heads.test_a_rest_answer_after_the_graphql_call_does_not_hide_a_low_graphql_budget'),
            'graphql errors': ('github', '    if errors:\n        text', '    if False:\n        text',
                               'Heads.test_a_graphql_error_answer_is_an_error_not_an_empty_list'),
        })


if __name__ == '__main__':
    unittest.main(verbosity=2)
