"""schedule-kick late-check: the first job of a listed workflow ends GitHub's late `schedule` run for a slot that
schedule-kick already started. A fake GitHub (in the process, and a local HTTP server for the command the README's
snippet runs), a temporary folder for the event and output files. No network.

  python3 -I -B fork-kit/schedule-kick/tests/late.test.py

The slot a late run belongs to is read from the run's own created time, never from now. A check that cannot tell
lets the run go ahead (skip=false, with a warning). RedCheck breaks each rule in a copy of lib/schedulekick/ and
shows its test then fails.
"""
import http.server, io, json, os, re, shutil, subprocess, sys, tempfile, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kick_fixture import KIT, FakeApi, at, red_check  # noqa: E402
from schedulekick import github, late  # noqa: E402

THIS = os.path.abspath(__file__)
BIN = os.path.join(KIT, 'bin', 'schedule-kick')
REPO = 'example-owner/app'
RUN_ID = 777


class FakeRunApi(FakeApi):
    """FakeApi and the one run the check runs in."""

    def __init__(self, created, branch='main', workflow_id=42):
        super().__init__(at('2026-03-10T13:00:00'))
        self.this_run = {'id': RUN_ID, 'created_at': created, 'head_branch': branch, 'workflow_id': workflow_id,
                         'event': 'schedule'}

    def run(self, repo, run_id):
        self.calls.append(('run', repo, run_id))
        return self.this_run


class Temp(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='schedule-kick-late-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.output = os.path.join(self.tmp, 'output')
        open(self.output, 'w').close()

    def env(self, cron='17 4 * * *', event_name='schedule', kicked='17 4 * * *', **over):
        path = os.path.join(self.tmp, 'event.json')
        with open(path, 'w') as f:
            json.dump({'schedule': cron} if cron is not None else {}, f)
        env = {'GITHUB_EVENT_NAME': event_name, 'GITHUB_EVENT_PATH': path, 'GITHUB_REPOSITORY': REPO,
               'GITHUB_RUN_ID': str(RUN_ID), 'GITHUB_RUN_ATTEMPT': '1', 'GITHUB_OUTPUT': self.output,
               'SCHEDULE_KICK_TOKEN': 'ghs_example', 'SCHEDULE_KICK_CRON': kicked}
        env.update(over)
        return {k: v for k, v in env.items() if v is not None}

    def check(self, env, api):
        out, err = io.StringIO(), io.StringIO()
        code = late.main(env, out, err, api_factory=lambda: api)
        self.out, self.err = out.getvalue(), err.getvalue()
        return code

    def skip(self):
        with open(self.output) as f:
            return f.read()


class Decide(Temp):
    def test_a_late_run_after_the_slots_dispatch_ends_here(self):
        api = FakeRunApi('2026-03-10T12:45:10Z')                                # 8.5 hours late
        api.add_run(REPO, 42, '2026-03-10T04:25:02Z', 'workflow_dispatch', 'main')
        self.assertEqual(self.check(self.env(), api), 0)
        self.assertEqual(self.skip(), 'skip=true\n')
        self.assertIn('since this run\'s slot 2026-03-10T04:17:00Z', self.out)
        self.assertEqual(api.lists(), [('list', REPO, 42, '2026-03-10T04:17:00Z')])

    def test_the_slot_comes_from_the_runs_created_time_not_from_now(self):
        """A run queued long before its check starts still belongs to the slot of its created time."""
        api = FakeRunApi('2026-03-10T04:17:40Z')                                # on time, the check starts later
        api.add_run(REPO, 42, '2026-03-09T04:22:00Z', 'workflow_dispatch', 'main')     # yesterday's slot
        self.check(self.env(), api)
        self.assertEqual(self.skip(), 'skip=false\n')
        self.assertEqual(api.lists()[0][3], '2026-03-10T04:17:00Z')

    def test_only_a_dispatch_on_the_same_branch_since_the_slot_counts(self):
        for event, branch, created in (('workflow_dispatch', 'main', '2026-03-10T04:16:59Z'),   # before the slot
                                       ('workflow_dispatch', 'other', '2026-03-10T05:00:00Z'),
                                       ('schedule', 'main', '2026-03-10T05:00:00Z'),
                                       ('pull_request', 'main', '2026-03-10T05:00:00Z'),
                                       ('push', 'main', '2026-03-10T05:00:00Z')):
            with self.subTest(event=event, branch=branch, created=created):
                open(self.output, 'w').close()
                api = FakeRunApi('2026-03-10T12:45:10Z')
                api.add_run(REPO, 42, created, event, branch)
                api.ignore_filter = True                                        # GitHub's filters not trusted
                api.runs_since = lambda *a, real=api.runs_since, **kw: real(*a[:4])   # and no event filter
                self.check(self.env(), api)
                self.assertEqual(self.skip(), 'skip=false\n')

    def test_a_dispatch_at_the_slot_second_counts(self):
        api = FakeRunApi('2026-03-10T12:45:10Z')
        api.add_run(REPO, 42, '2026-03-10T04:17:00Z', 'workflow_dispatch', 'main')
        self.check(self.env(), api)
        self.assertEqual(self.skip(), 'skip=true\n')

    def test_a_dispatch_after_100_other_runs_is_still_found(self):
        """GitHub is asked for workflow_dispatch runs only: 100 push runs since the slot do not hide the dispatch."""
        api = FakeRunApi('2026-03-10T12:45:10Z')
        api.add_run(REPO, 42, '2026-03-10T04:25:02Z', 'workflow_dispatch', 'main')
        for n in range(github.LIST_LIMIT):
            api.add_run(REPO, 42, f'2026-03-10T05:{n // 60:02d}:{n % 60:02d}Z', 'push', 'main')
        self.check(self.env(), api)
        self.assertEqual(self.skip(), 'skip=true\n')

    def test_a_run_of_another_schedule_line_never_skips(self):
        """Lines `0 3 * * *` (not kicked) and `0 4 * * *` (kicked): line 3's late run comes at 09:00, after the
        kick's 04:05 dispatch for line 4. That dispatch is not line 3's run: the 03:00 slot must still run."""
        for cron, want in (('0 3 * * *', 'skip=false\n'), ('0 4 * * *', 'skip=true\n'), ('0  4 * * *', 'skip=true\n')):
            with self.subTest(cron=cron):
                open(self.output, 'w').close()
                api = FakeRunApi('2026-03-10T09:00:00Z')
                api.add_run(REPO, 42, '2026-03-10T04:05:00Z', 'workflow_dispatch', 'main')
                self.check(self.env(cron=cron, kicked='0 4 * * *'), api)
                self.assertEqual(self.skip(), want)
        api = FakeRunApi('2026-03-10T09:00:00Z')
        self.check(self.env(cron='0 3 * * *', kicked='0 4 * * *'), api)
        self.assertEqual(api.calls, [])                                         # not even asked
        self.assertIn("this run's line '0 3 * * *' is not the kicked line '0 4 * * *'", self.out)

    def test_a_re_run_never_skips(self):
        """GitHub keeps the first attempt's created time, so a re-run of a run that really ran would look late."""
        for attempt in ('2', '3', None):
            with self.subTest(attempt=attempt):
                open(self.output, 'w').close()
                api = FakeRunApi('2026-03-10T04:17:40Z')
                api.add_run(REPO, 42, '2026-03-11T04:25:02Z', 'workflow_dispatch', 'main')   # the next slot's
                self.check(self.env(GITHUB_RUN_ATTEMPT=attempt), api)
                self.assertEqual(self.skip(), 'skip=false\n')
                self.assertEqual(api.calls, [])

    def test_a_run_that_is_not_a_schedule_run_never_skips(self):
        for name in ('workflow_dispatch', 'push', None):
            with self.subTest(event=name):
                open(self.output, 'w').close()
                api = FakeRunApi('2026-03-10T12:45:10Z')
                api.add_run(REPO, 42, '2026-03-10T12:44:00Z', 'workflow_dispatch', 'main')
                self.check(self.env(event_name=name), api)
                self.assertEqual(self.skip(), 'skip=false\n')
                self.assertEqual(api.calls, [])

    def test_a_check_that_cannot_tell_lets_the_run_go_ahead(self):
        cases = {
            'no token': dict(SCHEDULE_KICK_TOKEN=None),
            'a bad token': dict(SCHEDULE_KICK_TOKEN='two words'),
            'no cron': dict(cron=None),
            'a cron it does not read': dict(cron='17 4 * * MON'),
            'no kicked line': dict(kicked=None),
            'a kicked line it does not read': dict(kicked='@daily'),
            'no event file': dict(GITHUB_EVENT_PATH=os.path.join(tempfile.gettempdir(), 'none', 'event.json')),
            'a bad run id': dict(GITHUB_RUN_ID='1; x'),
            'a bad repository': dict(GITHUB_REPOSITORY='owner/..'),
        }
        for name, over in cases.items():
            with self.subTest(case=name):
                open(self.output, 'w').close()
                api = FakeRunApi('2026-03-10T12:45:10Z')
                api.add_run(REPO, 42, '2026-03-10T04:25:02Z', 'workflow_dispatch', 'main')
                self.assertEqual(self.check(self.env(**over), api), 0)
                self.assertEqual(self.skip(), 'skip=false\n')
                self.assertIn('::warning title=schedule-kick late-check::', self.out)
                if 'kicked' in over:
                    self.assertIn('SCHEDULE_KICK_CRON is not the kicked cron line', self.out)

    def test_an_api_error_lets_the_run_go_ahead(self):
        api = FakeRunApi('2026-03-10T12:45:10Z')
        api.fail['list'] = github.ApiError('GET ...: HTTP 403 Resource not accessible\n::error::x\r::set-output '
                                           'name=skip::true 100%0A')
        self.check(self.env(), api)
        self.assertEqual(self.skip(), 'skip=false\n')
        warning = [line for line in self.out.splitlines() if line.startswith('::')]      # \n and \r both split
        self.assertEqual(len(warning), 1)                                        # no second command from it
        self.assertIn('HTTP 403 Resource not accessible%0A::error::x%0D::set-output name=skip::true 100%250A',
                      warning[0])

    def test_a_run_without_its_fields_lets_the_run_go_ahead(self):
        api = FakeRunApi('2026-03-10T12:45:10Z', workflow_id='42')
        self.check(self.env(), api)
        self.assertEqual(self.skip(), 'skip=false\n')
        self.assertIn('no created time, branch or workflow id', self.out)

    def test_outside_github_actions_it_refuses(self):
        self.assertEqual(self.check(self.env(GITHUB_OUTPUT=None), FakeRunApi('2026-03-10T12:45:10Z')), 2)
        self.assertIn('GITHUB_OUTPUT is not set', self.err)


class Answers(http.server.BaseHTTPRequestHandler):
    seen, answers = [], {}

    def do_GET(self):
        Answers.seen.append((self.path, self.headers.get('Authorization')))
        code, data = Answers.answers.get(self.path.split('?')[0], (404, {'message': 'Not Found'}))
        raw = json.dumps(data).encode()
        self.send_response(code)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *a):
        pass


class Snippet(Temp):
    """The README's paste-ready job, run as GitHub Actions would run its step (the command and the env it names),
    against a local fake of the API."""

    def snippet(self):
        with open(os.path.join(KIT, 'README.md')) as f:
            text = f.read()
        section = text[text.index('## Late schedule runs'):]
        return re.search(r'```yaml\n(.*?)```', section, re.S).group(1)

    def test_the_readme_job_is_complete(self):
        yaml = self.snippet()
        for line in ("    if: github.event_name == 'schedule'", '      actions: read',
                     '      skip: ${{ steps.check.outputs.skip }}', '      - id: check',
                     '          SCHEDULE_KICK_TOKEN: ${{ github.token }}', '          persist-credentials: false',
                     '          SCHEDULE_KICK_CRON: "KICKED_CRON"',
                     '    needs: late', "    if: ${{ !cancelled() && needs.late.outputs.skip != 'true' }}"):
            self.assertIn(line + '\n', yaml)

    def test_a_job_after_another_job_keeps_its_failure_gate(self):
        """`!cancelled()` drops GitHub's implicit success(): a job that needs a job other than `late` must check
        that job's result itself, or it runs after that job failed. A job that needs only `late` loses no gate."""
        jobs = re.split(r'\n  ([a-z]+):\n', '\n' + self.snippet().split('jobs:', 1)[1])[1:]
        jobs = dict(zip(jobs[::2], jobs[1::2]))
        self.assertEqual(sorted(jobs), ['late', 'more', 'work'])
        for name, body in jobs.items():
            if name == 'late':
                continue
            with self.subTest(job=name):
                needs = re.search(r'\n?    needs: \[?([a-z, ]+)\]?\n', body).group(1).replace(' ', '').split(',')
                cond = re.search(r'    if: \$\{\{ (.*) \}\}\n', body).group(1).split(' && ')
                self.assertEqual(cond[0], '!cancelled()')                     # else skipped when late is
                if needs == ['late']:
                    self.assertEqual(cond[1:], ["needs.late.outputs.skip != 'true'"])
                else:
                    self.assertNotIn('late', needs)
                    self.assertEqual(cond[1:], [f"needs.{job}.result == 'success'" for job in needs])

    def serve(self):
        srv = http.server.HTTPServer(('127.0.0.1', 0), Answers)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        return f'http://127.0.0.1:{srv.server_address[1]}'

    def test_the_token_api_asks_for_dispatch_runs_on_the_branch_only(self):
        """In this process (RedCheck's copy of the code), the real TokenApi against a local fake of the API."""
        base = self.serve()
        Answers.seen = []
        Answers.answers = {
            f'/repos/{REPO}/actions/runs/{RUN_ID}': (200, {'id': RUN_ID, 'created_at': '2026-03-10T12:45:10Z',
                                                         'head_branch': 'main', 'workflow_id': 42}),
            f'/repos/{REPO}/actions/workflows/42/runs': (200, {'workflow_runs': []})}
        self.assertEqual(late.main(dict(self.env(), GITHUB_API_URL=base), io.StringIO(), io.StringIO()), 0)
        self.assertEqual(self.skip(), 'skip=false\n')
        self.assertIn('event=workflow_dispatch', Answers.seen[1][0])
        self.assertIn('branch=main', Answers.seen[1][0])

    def test_the_readme_command_skips_a_late_run_and_not_an_on_time_one(self):
        command = re.search(r'\n        run: (python3 .*)\n', self.snippet()).group(1).split()
        self.assertEqual(command[:3], ['python3', '-I', '-B'])
        self.assertTrue(command[3].endswith('fork-kit/schedule-kick/bin/schedule-kick'), command)
        base = self.serve()
        dispatch = {'created_at': '2026-03-10T04:25:02Z', 'event': 'workflow_dispatch', 'head_branch': 'main'}
        for created, runs, want in (('2026-03-10T12:45:10Z', [dispatch], 'skip=true\n'),
                                    ('2026-03-10T04:17:40Z', [], 'skip=false\n')):
            with self.subTest(created=created):
                Answers.seen = []
                Answers.answers = {
                    f'/repos/{REPO}/actions/runs/{RUN_ID}': (200, {'id': RUN_ID, 'created_at': created,
                                                                 'head_branch': 'main', 'workflow_id': 42}),
                    f'/repos/{REPO}/actions/workflows/42/runs': (200, {'workflow_runs': runs})}
                open(self.output, 'w').close()
                env = dict(self.env(), GITHUB_API_URL=base, PATH='/usr/bin:/bin')
                r = subprocess.run([sys.executable, *command[1:3], BIN, *command[4:]], capture_output=True,
                                   text=True, env=env, timeout=120, stdin=subprocess.DEVNULL)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(self.skip(), want)
                self.assertEqual(Answers.seen[0], (f'/repos/{REPO}/actions/runs/{RUN_ID}', 'Bearer ghs_example'))
                self.assertIn('event=workflow_dispatch', Answers.seen[1][0])
                self.assertIn('branch=main', Answers.seen[1][0])


MUTATIONS = {
    'only schedule runs are checked': ('late', "    if env.get('GITHUB_EVENT_NAME') != 'schedule':", '    if False:',
                                       'Decide.test_a_run_that_is_not_a_schedule_run_never_skips'),
    'the slot of the run\'s created time': ('late', '    slot = line.last_slot(created)',
                                            '    slot = line.last_slot(created.now(created.tzinfo))',
                                            'Decide.test_a_late_run_after_the_slots_dispatch_ends_here'),
    'a dispatch since the slot': ('late', "and when is not None and when >= slot:", "and when is not None:",
                                  'Decide.test_only_a_dispatch_on_the_same_branch_since_the_slot_counts'),
    'a dispatch on the same branch': ('late', "row['event'] == 'workflow_dispatch' and row['branch'] == branch and",
                                      "row['event'] == 'workflow_dispatch' and",
                                      'Decide.test_only_a_dispatch_on_the_same_branch_since_the_slot_counts'),
    'only a dispatch': ('late', "        if row['event'] == 'workflow_dispatch' and", '        if True and',
                        'Decide.test_only_a_dispatch_on_the_same_branch_since_the_slot_counts'),
    'cannot tell: the run goes ahead': ('late', '    except Exception as e:          # noqa: BLE001 - undecided',
                                        '    except ArithmeticError as e:          # noqa: BLE001 - undecided',
                                        'Decide.test_an_api_error_lets_the_run_go_ahead'),
    'one workflow command only': ('late', ".replace('\\n', '%0A')", '', 'Decide.test_an_api_error_lets_the_run_go_ahead'),
    'a re-run always runs': ('late', "    if env.get('GITHUB_RUN_ATTEMPT') != '1':", '    if False:',
                             'Decide.test_a_re_run_never_skips'),
    'only the kicked line': ('late', '    if line.line != kicked.line:', '    if False:',
                             'Decide.test_a_run_of_another_schedule_line_never_skips'),
    'the kicked line must be read': ('late', "    except ValueError as e:\n        raise Undecided(f'SCHEDULE_KICK_CRON",
                                     "    except ArithmeticError as e:\n        raise Undecided(f'SCHEDULE_KICK_CRON",
                                     'Decide.test_a_check_that_cannot_tell_lets_the_run_go_ahead'),
    'a dispatch at the slot second': ('late', 'when is not None and when >= slot:', 'when is not None and when > slot:',
                                      'Decide.test_a_dispatch_at_the_slot_second_counts'),
    'dispatch runs asked for': ('late', "api.runs_since(repo, workflow, branch, slot, event='workflow_dispatch')",
                                'api.runs_since(repo, workflow, branch, slot)',
                                'Decide.test_a_dispatch_after_100_other_runs_is_still_found'),
    'dispatch runs asked for (token)': ('github', "                                        **({'event': event} if event else {})})",
                                        '                                        })',
                                        'Snippet.test_the_token_api_asks_for_dispatch_runs_on_the_branch_only'),
    'no command from a \\r': ('late', ".replace('\\r', '%0D')", '', 'Decide.test_an_api_error_lets_the_run_go_ahead'),
    'a % is escaped': ('late', ".replace('%', '%25')", '', 'Decide.test_an_api_error_lets_the_run_go_ahead'),
    'run fields checked': ('late', '    if created is None or not isinstance(branch, str) or type(workflow) is not int:',
                           '    if False:', 'Decide.test_a_run_without_its_fields_lets_the_run_go_ahead'),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        red_check(self, THIS, MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
