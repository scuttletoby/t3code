"""The report issue (missing, closed, edited by hand, its marker removed, a GitHub error), what goes into it (local
paths redacted, the size cap) and the judgment step (a fake local model over HTTP, and one that is not there).
GitHub is a fake (gardener_fixture.FakeGitHub); the `gh api` wrapper is run against a fake gh script.

  python3 -I -B fork-kit/gardener/tests/report.test.py
"""
import http.server, json, os, sys, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import judge, report  # noqa: E402

GOOD = {'id': 'demo.thing', 'severity': 'warn', 'where': 'docs/a.md', 'text': 'a thing', 'fix': ''}
PART = report.marker('example') + '\nnew report\n'


class Publish(unittest.TestCase):

    def setUp(self):
        self.gh = gf.FakeGitHub()

    def test_missing_issue_is_created(self):
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART), (1, 'created'))
        self.assertEqual(self.gh.calls, ['search', 'create'])
        self.assertEqual(self.gh.issues[1]['title'], 'Gardener report: example')

    def test_the_remembered_issue_is_edited_without_a_search(self):
        n = self.gh.add(report.marker('example') + '\nold\n')
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART, known_number=n), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['issue', 'edit'])
        self.assertEqual(self.gh.issues[n]['body'], PART)

    def test_a_forgotten_issue_is_found_by_its_marker(self):
        self.gh.add('another issue')
        n = self.gh.add('text\n' + report.marker('example') + '\nold\n')
        self.gh.add(report.marker('other-project') + '\n')
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['search', 'edit'])

    def test_two_open_report_issues_always_pick_the_oldest(self):
        self.gh.add('x')
        first = self.gh.add(report.marker('example'))
        self.gh.add(report.marker('example'))
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART)[0], first)

    def test_a_closed_report_issue_stays_closed_and_a_new_one_is_opened(self):
        n = self.gh.add(report.marker('example') + '\nold\n', state='closed')
        number, how = report.publish(self.gh, 'o/r', 'example', PART, known_number=n)
        self.assertEqual((number, how), (2, 'created'))
        self.assertEqual(self.gh.issues[n]['body'], report.marker('example') + '\nold\n')
        self.assertEqual(self.gh.issues[n]['state'], 'closed')

    def test_an_issue_whose_marker_was_removed_by_hand_is_left_alone(self):
        n = self.gh.add('I took this issue over for something else')
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART, known_number=n), (2, 'created'))
        self.assertEqual(self.gh.issues[n]['body'], 'I took this issue over for something else')

    def test_a_deleted_remembered_issue_falls_back_to_the_search(self):
        n = self.gh.add(report.marker('example'))
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART, known_number=99), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['issue', 'search', 'edit'])

    def test_text_written_above_the_marker_by_hand_is_kept(self):
        n = self.gh.add('Owner note: ignore the docs warnings this week.\n\n' + report.marker('example') +
                        '\nold report\n')
        report.publish(self.gh, 'o/r', 'example', PART, known_number=n)
        self.assertEqual(self.gh.issues[n]['body'],
                         'Owner note: ignore the docs warnings this week.\n\n' + PART)

    def test_a_pull_request_with_the_marker_is_not_the_report(self):
        n = self.gh.add(report.marker('example'))
        self.gh.issues[n]['pull_request'] = {}
        self.assertEqual(report.publish(self.gh, 'o/r', 'example', PART, known_number=n)[1], 'created')

    def test_a_github_error_on_search_or_write_is_raised(self):
        for what in ('search', 'create', 'edit'):
            with self.subTest(what=what):
                self.gh = gf.FakeGitHub()
                if what == 'edit':
                    self.gh.add(report.marker('example'))
                self.gh.fail = what
                with self.assertRaises(report.GitHubError):
                    report.publish(self.gh, 'o/r', 'example', PART)


class GhWrapper(gf.Temp):
    """report.GitHub runs `gh api` and turns its failures into GitHubError, naming rate limits."""

    def fake_gh(self, body):
        return self.script('gh', body)

    def test_calls_and_answers(self):
        log = os.path.join(self.tmp, 'log')
        gh = self.fake_gh(f'''
            import json, sys
            data = sys.stdin.read()
            open({log!r}, 'a').write(json.dumps([sys.argv[1:], data]) + '\\n')
            print(json.dumps({{"items": [{{"number": 3}}], "number": 4}}))
        ''')
        g = report.GitHub(gh)
        self.assertEqual(g.search_open('o/r', 'gardener-report:x'), [{'number': 3}])
        self.assertEqual(g.create('o/r', 'T', 'B')['number'], 4)
        self.assertEqual(g.edit('o/r', 4, 'B2')['number'], 4)
        with open(log) as f:
            calls = [json.loads(ln) for ln in f]
        self.assertEqual(calls[0][0][:4], ['api', '--method', 'GET', calls[0][0][3]])
        self.assertIn('is%3Aopen', calls[0][0][3])
        self.assertEqual(calls[1], [['api', '--method', 'POST', 'repos/o/r/issues', '--input', '-'],
                                    json.dumps({'title': 'T', 'body': 'B'})])
        self.assertEqual(calls[2][0][2:4], ['PATCH', 'repos/o/r/issues/4'])

    def test_failures(self):
        for body, want in (('import sys\nsys.stderr.write("HTTP 403: API rate limit exceeded\\n")\nsys.exit(1)',
                            'rate limit: the next run tries again'),
                           ('import sys\nsys.stderr.write("HTTP 404: Not Found\\n")\nsys.exit(1)', 'HTTP 404'),
                           ('print("not json")', 'not JSON')):
            with self.subTest(want=want):
                with self.assertRaises(report.GitHubError) as e:
                    report.GitHub(self.fake_gh(body)).issue('o/r', 1)
                self.assertIn(want, str(e.exception))
        with self.assertRaises(report.GitHubError):
            report.GitHub(os.path.join(self.tmp, 'no-gh')).issue('o/r', 1)


class Render(unittest.TestCase):

    def finding(self, **kw):
        return dict(GOOD, check='c', **kw)

    def test_local_paths_are_redacted(self):
        root, home = '/srv/u/repo', '/srv/u'
        f = self.finding(where='/srv/u/repo/docs/a.md', text='see /srv/u/notes and /srv/u/repo', fix='')
        body = report.render('example', '2026-11-23', [f], ['note about /srv/u/repo/x'], 'judge saw /srv/u/x',
                             root=root, home=home)
        self.assertNotIn('/srv/u', body)
        self.assertIn('./docs/a.md', body)
        self.assertIn('see ~/notes and .', body)
        self.assertIn('note about ./x', body)
        self.assertIn('judge saw ~/x', body)

    def test_table_cells_are_escaped_and_sorted_by_severity(self):
        fs = [self.finding(severity='info', id='i'), self.finding(severity='error', id='e', text='a | b\nc')]
        body = report.render('example', '2026-11-23', fs, [])
        self.assertLess(body.index('| error |'), body.index('| info |'))
        self.assertIn('a \\| b c', body)
        self.assertIn('1 error(s), 0 warning(s), 1 note(s)', body)

    def test_no_findings(self):
        body = report.render('example', '2026-11-23', [], [])
        self.assertTrue(body.startswith(report.marker('example') + '\n'))
        self.assertIn('No findings.', body)
        self.assertNotIn('| Severity', body)

    def test_the_body_stays_under_githubs_limit(self):
        fs = [self.finding(id=f'n.{i}', text='x' * 1500) for i in range(200)]
        body = report.render('example', '2026-11-23', fs, ['a note'], 'j' * 8000)
        self.assertLess(len(body), report.MAX_BODY)
        self.assertIn('more finding(s) not shown (the issue is full)', body)
        self.assertIn('### Judgment', body)


class Judge(gf.Temp):

    def serve(self, answer, status=200):
        seen = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append((self.path, json.loads(self.rfile.read(int(self.headers['Content-Length'])))))
                data = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *a):
                pass
        server = http.server.HTTPServer(('127.0.0.1', 0), H)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return f'http://127.0.0.1:{server.server_port}/v1', seen

    def model(self, endpoint, **kw):
        return dict({'endpoint': endpoint, 'model': 'local-model', 'timeoutSeconds': 10, 'maxFiles': 5,
                     'maxFileBytes': 100}, **kw)

    def test_the_model_gets_findings_and_only_files_inside_the_root(self):
        os.makedirs(os.path.join(self.root, 'docs'))
        with open(os.path.join(self.root, 'docs', 'a.md'), 'w') as f:
            f.write('A' * 500)
        with open(os.path.join(self.tmp, 'secret.txt'), 'w') as f:
            f.write('outside the root')
        endpoint, seen = self.serve({'choices': [{'message': {'content': ' looks real \n'}}]})
        fs = [dict(GOOD, check='c'), dict(GOOD, check='c', where='../secret.txt'),
              dict(GOOD, check='c', where='/etc/passwd')]
        answer, note = judge.judge(self.model(endpoint), fs, self.root)
        self.assertEqual(answer, 'looks real')
        self.assertIn('read 3 finding(s) and 1 file(s)', note)
        path, req = seen[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual((req['model'], req['temperature']), ('local-model', 0))
        user = req['messages'][1]['content']
        self.assertIn('File docs/a.md:', user)
        self.assertIn('A' * 100, user)
        self.assertNotIn('A' * 101, user)                               # cut to maxFileBytes
        self.assertNotIn('outside the root', user)
        self.assertNotIn('root:x:', user)

    def test_no_model_no_findings_and_bad_answers(self):
        self.assertEqual(judge.judge(None, [GOOD], self.root), (None, None))
        endpoint, seen = self.serve({'choices': []})
        self.assertEqual(judge.judge(self.model(endpoint), [], self.root)[0], None)
        self.assertEqual(seen, [])                                      # nothing to judge: no request
        for answer, status in (({'choices': []}, 200), ({'error': 'x'}, 500),
                               ({'choices': [{'message': {'content': '  '}}]}, 200)):
            with self.subTest(answer=answer):
                endpoint, _ = self.serve(answer, status)
                got, note = judge.judge(self.model(endpoint), [dict(GOOD, check='c')], self.root)
                self.assertIsNone(got)
                self.assertIn('judgment skipped', note)

    def test_an_unreachable_model_is_a_note_and_the_run_goes_on(self):
        import socket
        s = socket.socket()
        s.bind(('127.0.0.1', 0))
        port = s.getsockname()[1]
        s.close()                                                       # nothing listens there now
        self.write_machine(self.project([{'path': self.finding_script('demo', GOOD)}],
                                        model={'endpoint': f'http://127.0.0.1:{port}/v1', 'model': 'm',
                                               'timeoutSeconds': 5}))
        code, out = self.run_cli('run')
        self.assertEqual(code, 0, out)
        body = self.gh.issues[1]['body']
        self.assertIn('judgment skipped: the model at', body)
        self.assertNotIn('### Judgment', body)

    def test_the_judgment_goes_into_the_report(self):
        endpoint, _ = self.serve({'choices': [{'message': {'content': 'demo.thing: real; fix the doc first'}}]})
        self.write_machine(self.project([{'path': self.finding_script('demo', GOOD)}],
                                        model={'endpoint': endpoint, 'model': 'm'}))
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertIn('### Judgment (local model)\n\ndemo.thing: real; fix the doc first', self.gh.issues[1]['body'])


MUTATIONS = {
    'a closed issue is not the report': (
        'report', "issue.get('state') == 'open' and ", '',
        'Publish.test_a_closed_report_issue_stays_closed_and_a_new_one_is_opened'),
    'an issue without the marker is not the report': (
        'report', " and marker(name) in (issue.get('body') or ''))", ')',
        'Publish.test_an_issue_whose_marker_was_removed_by_hand_is_left_alone'),
    'hand-written text above the marker is kept': (
        'report', "        head = old_body[:old_body.index(marker(name))]", "        head = ''",
        'Publish.test_text_written_above_the_marker_by_hand_is_kept'),
    'local paths are redacted': (
        'report', "    if home and home != '/':", '    if False:',
        'Render.test_local_paths_are_redacted'),
    'judge reads only files inside the root': (
        'judge', 'not full.startswith(real_root + os.sep) or ', '',
        'Judge.test_the_model_gets_findings_and_only_files_inside_the_root'),
    'the body is capped': (
        'report', '        if len(row) + 1 > budget:\n            break\n', '',
        'Render.test_the_body_stays_under_githubs_limit'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        gf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
