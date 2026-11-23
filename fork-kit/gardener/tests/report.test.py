"""The report issue (missing, closed, edited by hand, its marker removed, a GitHub error), what goes into it (the
allow-list: nothing local reaches the issue; the size cap) and the judgment step (a fake local model over HTTP, one
that is not there and one that does not speak HTTP).
GitHub is a fake (gardener_fixture.FakeGitHub); the `gh api` wrapper is run against a fake gh script.

  python3 -I -B fork-kit/gardener/tests/report.test.py
"""
import http.server, json, os, socket, subprocess, sys, threading, unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import judge, report  # noqa: E402

GOOD = {'id': 'demo.thing', 'severity': 'warn', 'where': 'docs/a.md', 'text': 'a thing', 'fix': ''}
PART = report.marker('example') + '\nnew report\n'


class Publish(unittest.TestCase):

    def setUp(self):
        self.gh = gf.FakeGitHub()

    def test_missing_issue_is_created(self):
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART), (1, 'created'))
        self.assertEqual(self.gh.calls, ['search', 'search', 'create'])        # by title, then by marker
        self.assertEqual(self.gh.issues[1]['title'], 'Gardener report: example')

    def test_the_remembered_issue_is_edited_without_a_search(self):
        n = self.gh.add(report.marker('example') + '\nold\n')
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART, known_number=n), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['issue', 'edit'])
        self.assertEqual(self.gh.issues[n]['body'], PART)

    def test_a_forgotten_issue_is_found_by_its_title(self):
        """GitHub search may not index the hidden marker (an HTML comment): the title is searched first."""
        self.gh.body_search_blind = True
        self.gh.add('another issue')
        n = self.gh.add('text\n' + report.marker('example') + '\nold\n')
        self.gh.add(report.marker('other-project') + '\n', title='Gardener report: other-project')
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['search', 'edit'])

    def test_a_forgotten_issue_renamed_by_hand_is_found_by_its_marker(self):
        self.gh.add('another issue')
        n = self.gh.add('text\n' + report.marker('example') + '\nold\n', title='Docs work this week')
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['search', 'search', 'edit'])

    def test_two_open_report_issues_always_pick_the_oldest(self):
        self.gh.add('x')
        first = self.gh.add(report.marker('example'))
        self.gh.add(report.marker('example'))
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART)[0], first)

    def test_a_closed_report_issue_stays_closed_and_a_new_one_is_opened(self):
        n = self.gh.add(report.marker('example') + '\nold\n', state='closed')
        number, how = report.publish(self.gh, gf.REPO, 'example', PART, known_number=n)
        self.assertEqual((number, how), (2, 'created'))
        self.assertEqual(self.gh.issues[n]['body'], report.marker('example') + '\nold\n')
        self.assertEqual(self.gh.issues[n]['state'], 'closed')

    def test_an_issue_whose_marker_was_removed_by_hand_is_left_alone(self):
        n = self.gh.add('I took this issue over for something else')
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART, known_number=n), (2, 'created'))
        self.assertEqual(self.gh.issues[n]['body'], 'I took this issue over for something else')

    def test_a_deleted_remembered_issue_falls_back_to_the_search(self):
        n = self.gh.add(report.marker('example'))
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART, known_number=99), (n, 'updated'))
        self.assertEqual(self.gh.calls, ['issue', 'search', 'edit'])

    def test_text_written_above_the_marker_by_hand_is_kept(self):
        n = self.gh.add('Owner note: ignore the docs warnings this week.\n\n' + report.marker('example') +
                        '\nold report\n')
        report.publish(self.gh, gf.REPO, 'example', PART, known_number=n)
        self.assertEqual(self.gh.issues[n]['body'],
                         'Owner note: ignore the docs warnings this week.\n\n' + PART)

    def test_a_long_hand_written_text_is_cut_so_the_body_fits(self):
        n = self.gh.add('H' * 70000 + report.marker('example') + '\nold\n')
        part = report.marker('example') + '\n' + 'p' * 40000 + '\n'
        report.publish(self.gh, gf.REPO, 'example', part, known_number=n)
        body = self.gh.issues[n]['body']
        self.assertLessEqual(len(body), report.MAX_BODY)
        self.assertTrue(body.endswith(part))                            # the gardener's part is whole
        self.assertIn('the text above was cut', body)
        self.assertTrue(body.startswith('H' * 1000))

    def test_a_pull_request_with_the_marker_is_not_the_report(self):
        n = self.gh.add(report.marker('example'))
        self.gh.issues[n]['pull_request'] = {}
        self.assertEqual(report.publish(self.gh, gf.REPO, 'example', PART, known_number=n)[1], 'created')

    def test_a_github_error_on_search_or_write_is_raised(self):
        for what in ('search', 'create', 'edit'):
            with self.subTest(what=what):
                self.gh = gf.FakeGitHub()
                if what == 'edit':
                    self.gh.add(report.marker('example'))
                self.gh.fail = what
                with self.assertRaises(report.GitHubError):
                    report.publish(self.gh, gf.REPO, 'example', PART)


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
        self.assertEqual(g.search_open('o/r', 'Gardener report: x', 'title'), [{'number': 3}])
        self.assertEqual(g.create('o/r', 'T', 'B')['number'], 4)
        self.assertEqual(g.edit('o/r', 4, 'B2')['number'], 4)
        with open(log) as f:
            calls = [json.loads(ln) for ln in f]
        self.assertEqual(calls[0][0][:4], ['api', '--method', 'GET', calls[0][0][3]])
        self.assertIn('is%3Aopen', calls[0][0][3])
        self.assertIn('in%3Abody', calls[0][0][3])
        self.assertIn('in%3Atitle', calls[1][0][3])
        self.assertEqual(calls[2], [['api', '--method', 'POST', 'repos/o/r/issues', '--input', '-'],
                                    json.dumps({'title': 'T', 'body': 'B'})])
        self.assertEqual(calls[3][0][2:4], ['PATCH', 'repos/o/r/issues/4'])

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
        return dict(dict(GOOD, check='c'), **kw)

    def test_local_paths_are_redacted(self):
        root, home = '/srv/u/repo', '/srv/u'
        f = self.finding(where='/srv/u/repo/docs/a.md', text='see /srv/u/notes and /srv/u/repo', fix='')
        body = report.render_issue('example', '2026-11-23', [f], ['note about /srv/u/repo/x'], 'judge saw /srv/u/x',
                                   root=root, home=home, text_checks={'c'})
        self.assertNotIn('/srv/u', body)
        self.assertIn('| docs/a.md |', body)
        self.assertIn('see <path> and .', body)
        self.assertIn('note about ./x', body)
        self.assertIn('judge saw <path>', body)

    def test_table_cells_are_escaped_and_sorted_by_severity(self):
        fs = [self.finding(severity='info', id='i'), self.finding(severity='error', id='e', text='a | b\nc')]
        for body in (report.render_issue('example', '2026-11-23', fs, [], text_checks={'c'}),
                     report.render_local('example', '2026-11-23', fs, [])):
            self.assertLess(body.index('| error |'), body.index('| info |'))
            self.assertIn('a \\| b c', body)
            self.assertIn('1 error(s), 0 warning(s), 1 note(s)', body)

    def test_no_findings(self):
        body = report.render_issue('example', '2026-11-23', [], [])
        self.assertTrue(body.startswith(report.marker('example') + '\n'))
        self.assertIn('No findings.', body)
        self.assertNotIn('| Severity', body)

    def test_the_body_stays_under_githubs_limit(self):
        fs = [self.finding(id=f'n.{i}', text='x' * 1500) for i in range(200)]
        body = report.render_issue('example', '2026-11-23', fs, ['a note'], 'j' * 8000, text_checks={'c'})
        self.assertLess(len(body), report.MAX_PART)
        self.assertIn('more row(s) not shown (the issue is full)', body)
        self.assertIn('### Judgment', body)
        self.assertEqual(report.render_local('example', '2026-11-23', fs, []).count('| warn |'), 200)

    def test_only_paths_inside_the_repository_are_shown(self):
        root = '/srv/u/repo'
        cases = {'docs/a.md': 'docs/a.md', 'docs/a.md:12': 'docs/a.md:12', '/srv/u/repo/b/c.py:3': 'b/c.py:3',
                 '.': '.', '': '', '/srv/u/other/x.md': None, '../x': None, '~/notes.md': None,
                 '/etc/passwd': None, 'docs/a b.md': None, 'example-owner/app: .github/workflows/ci.yml':
                 'example-owner/app: .github/workflows/ci.yml', 'someone/private: x.yml': None,
                 'example-owner/app: /srv/u/x': None}
        for where, want in cases.items():
            with self.subTest(where=where):
                self.assertEqual(report.public_where(where, root, 'example-owner/app'), want)

    def test_a_finding_outside_the_repository_is_only_counted(self):
        fs = [self.finding(where='/srv/u/.claude/projects/-srv-u--repo/memory/plans.md', text='orphan plans.md'),
              self.finding(where='/srv/u/.claude/projects/-srv-u--repo/memory/b.md', text='orphan b.md'),
              self.finding(where='', text='private-host is down', id='x.y'),
              dict(GOOD, check='c', id='gardener.check-failed', where='', own=True, text='exited with code 1',
                   detail='ssh: connect to host private-host port 22')]
        issue = report.render_issue('example', '2026-11-23', fs, [], root='/srv/u/repo', home='/srv/u')
        local = report.render_local('example', '2026-11-23', fs, [])
        for secret in ('plans.md', 'b.md', 'private-host', '-srv-u-', '/srv/u'):
            self.assertNotIn(secret, issue)
        self.assertIn('| warn | c | 2 finding(s), in the local report only | <outside repo> |', issue)
        self.assertIn('| warn | c | 1 finding(s), in the local report only |  |', issue)
        self.assertNotIn('x.y', issue)                                  # a hidden finding's id is not shown
        self.assertIn('gardener.check-failed: exited with code 1', issue)
        self.assertIn('report.md', issue)                               # where the rest is
        for shown in ('plans.md', 'private-host is down', 'ssh: connect to host private-host'):
            self.assertIn(shown, local)

    def test_the_root_is_replaced_only_as_a_whole_folder(self):
        for text, want in (('/srv/u/repo2/client-acme-plan.md', '<path>'), ('/srv/u/repo/docs/a.md', './docs/a.md'),
                           ('in /srv/u/repo.', 'in ..'), ('/srv/u/repo: x', '.: x'), ('/srv/u/repository', '<path>'),
                           ('a/srv/u/x', 'a<path>')):
            with self.subTest(text=text):
                self.assertEqual(report.public_text(text, '/srv/u/repo', '/srv/u'), want)

    def test_a_project_checks_text_is_shown_only_with_public_text(self):
        fs = [self.finding(check='proj', id='host.box1.down', where='docs/a.md', text='build-box is down',
                           fix='ssh build-box'),
              self.finding(check='proj', id='host.box1.down', where='docs/a.md', text='build-box2 is down'),
              self.finding(check='general', id='g.x', where='docs/b.md', text='budget passed')]
        issue = report.render_issue('example', '2026-11-23', fs, [], root='/srv/u/repo', home='/srv/u',
                                    text_checks={'general'})
        self.assertNotIn('build-box', issue)
        self.assertIn('| warn | proj | host.box1.down: 2 finding(s), text in the local report only (the check is not '
                      'set to publicText) | docs/a.md |  |', issue)
        self.assertIn('g.x: budget passed', issue)
        shown = report.render_issue('example', '2026-11-23', fs, [], root='/srv/u/repo', home='/srv/u',
                                    text_checks={'general', 'proj'})
        self.assertIn('host.box1.down: build-box is down', shown)

    def test_counts_only_shows_no_text_id_where_or_judgment(self):
        fs = [self.finding(check='general', id='g.x', where='docs/b.md', text='budget passed'),
              self.finding(check='general', id='g.y', where='docs/c.md', text='other'),
              dict(GOOD, check='c', id='gardener.check-failed', where='', own=True, text='exited with code 1')]
        issue = report.render_issue('example', '2026-11-23', fs, ['a note'], 'the model says x', root='/srv/u/repo',
                                    home='/srv/u', text_checks={'general', 'c'}, counts_only=True)
        for gone in ('g.x', 'g.y', 'budget', 'docs/', 'the model says', 'exited with code'):
            self.assertNotIn(gone, issue)
        self.assertIn('| warn | general | 2 finding(s), in the local report only |  |  |', issue)
        self.assertIn(report.COUNTS_ONLY, issue)
        self.assertIn('a note', issue)

    def test_masks(self):
        for text, gone in (('see http://100.64.1.2:8080/v1 now', '100.64'), ('mail a.b@example.org', 'a.b@'),
                           ('host 10.0.0.5 down', '10.0.0.5'), ('at fe80::1:2', 'fe80'),
                           ('box nas.local and pc.tail1234.ts.net', 'nas'), ('in ~/secret/x', 'secret'),
                           ('memory -home-user--proj', 'user'), ('file /opt/x/y', '/opt'),
                           ('ssh admin@build-box failed', 'build-box'), ('ip 2001:db8:a1e0::1 up', '2001'),
                           ('ip 2001:db8:a1e0::1 up', 'a1e0'), ('bind :: now', '::'), ('lo ::1', '::1'),
                           (r'C:\Users\alice\secret\plan.txt', 'alice'), ('C:/Users/alice/x', 'alice'),
                           (r'share \\nas\private\x', 'nas'), ('a/srv/u/x y', '/srv'), ('a~/secret/x', 'secret')):
            with self.subTest(text=text):
                self.assertNotIn(gone, report.public_text(text, '/srv/u/repo', '/srv/u'))
        for kept in ('origin/main is ahead', 'docs/a.md', 'at 12:30:45', 'std::vector', 'a.b c'):
            with self.subTest(kept=kept):
                self.assertEqual(report.public_text(kept, '/srv/u/repo', '/srv/u'), kept)


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

    def git(self, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@example.invalid', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        subprocess.run(['git', '-C', self.tmp, *args], check=True, capture_output=True, env=env)

    def repo_files(self):
        """The project root is a folder of a larger repository: docs/a.md tracked; .env ignored; notes.txt
        untracked; secret.txt tracked too, but outside the root."""
        os.makedirs(os.path.join(self.root, 'docs'))
        for rel, text in (('docs/a.md', 'A' * 500), ('.gitignore', '.env\n'), ('.env', 'TOKEN=ignored-secret'),
                          ('notes.txt', 'untracked-secret'), ('../secret.txt', 'outside the root')):
            with open(os.path.join(self.root, rel), 'w') as f:
                f.write(text)
        self.git('init', '-q', '-b', 'main')
        self.git('add', 'repo/docs/a.md', 'repo/.gitignore', 'secret.txt')
        self.git('commit', '-q', '-m', 'one')

    def test_the_model_gets_findings_and_only_tracked_files_inside_the_root(self):
        self.repo_files()
        endpoint, seen = self.serve({'choices': [{'message': {'content': ' looks real \n'}}]})
        fs = [dict(GOOD, check='c', where=w) for w in ('docs/a.md:3', '../secret.txt', '/etc/passwd', '.env',
                                                      'notes.txt')]
        answer, note = judge.judge(self.model(endpoint), fs, self.root)
        self.assertEqual(answer, 'looks real')
        self.assertEqual(note, 'judgment: model reachable; it read 5 finding(s) and 1 file(s)')
        self.assertNotIn('127.0.0.1', note)
        path, req = seen[0]
        self.assertEqual(path, '/v1/chat/completions')
        self.assertEqual((req['model'], req['temperature']), ('local-model', 0))
        user = req['messages'][1]['content']
        self.assertIn('File docs/a.md:', user)
        self.assertIn('A' * 100, user)
        self.assertNotIn('A' * 101, user)                               # cut to maxFileBytes
        for secret in ('outside the root', 'root:x:', 'ignored-secret', 'untracked-secret'):
            self.assertNotIn(secret, user)

    def test_no_model_no_findings_and_bad_answers(self):
        self.assertEqual(judge.judge(None, [GOOD], self.root), (None, None))
        endpoint, seen = self.serve({'choices': []})
        self.assertEqual(judge.judge(self.model(endpoint), [], self.root)[0], None)
        self.assertEqual(seen, [])                                      # nothing to judge: no request
        for answer, status in (({'choices': []}, 200), ({'error': 'x'}, 500),
                               ({'choices': [{'message': {'content': '  '}}]}, 200), ([1], 200)):
            with self.subTest(answer=answer):
                endpoint, _ = self.serve(answer, status)
                got, note = judge.judge(self.model(endpoint), [dict(GOOD, check='c')], self.root)
                self.assertIsNone(got)
                self.assertIn('judgment: model unreachable or no usable answer', note)
                self.assertNotIn('127.0.0.1', note)

    def junk_server(self, reply):
        """A socket that answers every connection with reply and closes (a wrong port: ssh, not HTTP)."""
        srv = socket.socket()
        srv.bind(('127.0.0.1', 0))
        srv.listen(4)
        self.addCleanup(srv.close)

        def serve():
            while True:
                try:
                    conn, _ = srv.accept()
                except OSError:
                    return
                with conn:
                    conn.recv(65536)
                    conn.sendall(reply)
        threading.Thread(target=serve, daemon=True).start()
        return f'http://127.0.0.1:{srv.getsockname()[1]}/v1'

    def test_an_endpoint_that_does_not_speak_http_is_a_note(self):
        for reply in (b'SSH-2.0-OpenSSH_9.6\r\n', b'HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n{"cho'):
            with self.subTest(reply=reply):
                got, note = judge.judge(self.model(self.junk_server(reply)), [dict(GOOD, check='c')], self.root)
                self.assertIsNone(got)
                self.assertIn('model unreachable', note)

    def test_an_unreachable_model_is_a_note_and_the_run_goes_on(self):
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
        self.assertIn('judgment: model unreachable', body)
        self.assertNotIn(str(port), body)                               # the endpoint is never shown
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
    'the root is shown as "."': (
        'report', "            text = re.sub(re.escape(path) + _END, short, text)", '            pass',
        'Render.test_local_paths_are_redacted'),
    'the root is replaced only as a whole folder': (
        'report', "            text = re.sub(re.escape(path) + _END, short, text)",
        '            text = text.replace(path, short)', 'Render.test_the_root_is_replaced_only_as_a_whole_folder'),
    'a project check shows its text only with publicText': (
        'report', "        elif not f.get('own') and f['check'] not in text_checks:", '        elif False:',
        'Render.test_a_project_checks_text_is_shown_only_with_public_text'),
    'counts only hides everything but counts': (
        'report', '        if counts_only:\n            key', '        if False:\n            key',
        'Render.test_counts_only_shows_no_text_id_where_or_judgment'),
    'counts only drops the judgment': (
        'report', 'if judgment and not counts_only else None', 'if judgment else None',
        'Render.test_counts_only_shows_no_text_id_where_or_judgment'),
    'a hidden finding shows no id': (
        'report', "            key = (f['severity'], f['check'], '', OUTSIDE if w is None",
        "            key = (f['severity'], f['check'], f['id'], OUTSIDE if w is None",
        'Render.test_a_finding_outside_the_repository_is_only_counted'),
    'text in the issue is masked': (
        'report', "    for rx, sub in _MASKS:\n        text = rx.sub(sub, text)\n", '',
        'Render.test_masks'),
    'a where outside the repository is not shown': (
        'report', "    if w == '..' or w.startswith('../') or not _SAFE_REL.fullmatch(w):\n        return None\n", '',
        'Render.test_only_paths_inside_the_repository_are_shown'),
    'a finding outside the repository is only counted': (
        'report', "        elif w is None or (w == '' and not f.get('own')):",
        "        elif False:", 'Render.test_a_finding_outside_the_repository_is_only_counted'),
    'judge reads only files inside the root': (
        'judge', 'not full.startswith(real_root + os.sep) or ', '',
        'Judge.test_the_model_gets_findings_and_only_tracked_files_inside_the_root'),
    'judge reads only tracked files': (
        'judge', '        if not tracked(real_root, rel):\n            continue\n', '',
        'Judge.test_the_model_gets_findings_and_only_tracked_files_inside_the_root'),
    'judge: any failure is a note': (
        'judge', '    except Exception as e:', '    except (OSError, ValueError, KeyError, IndexError, TypeError) as e:',
        'Judge.test_an_endpoint_that_does_not_speak_http_is_a_note'),
    'the issue part is capped': (
        'report', '            if len(row) + 1 > budget:\n                break\n', '',
        'Render.test_the_body_stays_under_githubs_limit'),
    'a long hand-written text is cut': (
        'report', '    if len(head) > room:', '    if False:',
        'Publish.test_a_long_hand_written_text_is_cut_so_the_body_fits'),
    'the forgotten issue is searched by title first': (
        'report', "((f'Gardener report: {name}', 'title'), (f'gardener-report:{name}', 'body'))",
        "((f'gardener-report:{name}', 'body'),)", 'Publish.test_a_forgotten_issue_is_found_by_its_title'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        gf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
