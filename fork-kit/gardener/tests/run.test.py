"""gardener run: the state question (empty, missing, broken, old state; a second run the same day; a run stopped
half-way), the check contract (a check that hangs, prints junk, exits non-zero or cannot start never stops the
run), dry run, the lock, the timer window and the config. Project checks are small scripts written by each test;
GitHub is a fake (gardener_fixture.FakeGitHub). No root, no network.

  python3 -I -B fork-kit/gardener/tests/run.test.py
"""
import json, os, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import cli, config, contract, report, runner  # noqa: E402

GOOD = {'id': 'demo.thing', 'severity': 'warn', 'where': 'docs/a.md', 'text': 'a thing to look at',
        'fix': 'look at it'}


class State(gf.Temp):

    def setUp(self):
        super().setUp()
        self.check = self.finding_script('demo', GOOD)
        self.write_machine(self.project([{'path': self.check}]))

    def folder(self):
        return os.path.join(self.state_dir, 'example')

    def write_state(self, text):
        os.makedirs(self.folder(), exist_ok=True)
        with open(os.path.join(self.folder(), 'state.json'), 'w') as f:
            f.write(text)

    def test_empty_state_runs_creates_the_issue_and_marks_the_day(self):
        code, out = self.run_cli('run')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gh.writes(), ['create'])
        st = self.state()
        self.assertEqual((st['version'], st['doneDay'], st['issue'], st['findings']), (1, '2026-11-23', 1, 1))
        body = self.gh.issues[1]['body']
        self.assertIn(report.marker('example'), body)
        self.assertIn('demo.thing: a thing to look at', body)
        with open(os.path.join(self.folder(), 'report.md')) as f:
            self.assertEqual(f.read(), body)
        self.assertEqual(oct(os.stat(self.folder()).st_mode & 0o777), '0o700')

    def test_second_run_the_same_day_does_nothing_and_force_runs_again(self):
        self.assertEqual(self.run_cli('run')[0], 0)
        calls = list(self.gh.calls)
        code, out = self.run_cli('run')
        self.assertEqual(code, 0)
        self.assertIn('ran today already (2026-11-23)', out)
        self.assertEqual(self.gh.calls, calls)                         # no GitHub call at all
        code, out = self.run_cli('run', '--force')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gh.writes(), ['create', 'edit'])         # the same issue, updated in place
        self.assertEqual(len(self.gh.issues), 1)

    def test_the_next_day_runs_again(self):
        self.assertEqual(self.run_cli('run')[0], 0)
        with mock.patch.dict(os.environ, {'GARDENER_TEST_NOW': '2026-11-24T03:30:00+00:00'}):
            self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.gh.writes(), ['create', 'edit'])
        self.assertEqual(self.state()['doneDay'], '2026-11-24')

    def test_the_day_is_the_projects_local_day(self):
        # 23:30 UTC on the 23rd is the 24th in Tokyo
        self.write_machine(self.project([{'path': self.check}], timeZone='Asia/Tokyo'))
        with mock.patch.dict(os.environ, {'GARDENER_TEST_NOW': '2026-11-23T23:30:00+00:00'}):
            self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.state()['doneDay'], '2026-11-24')

    def test_broken_or_old_state_starts_empty_with_a_note(self):
        for text in ('{not json', '[]', json.dumps({'version': 0, 'doneDay': '2026-11-23', 'issue': 1}),
                     json.dumps({'version': 1, 'doneDay': 5, 'issue': 'x'}), ''):
            with self.subTest(text=text):
                self.gh = gf.FakeGitHub()
                self.write_state(text)
                code, out = self.run_cli('run')
                self.assertEqual(code, 0, out)
                st = self.state()
                self.assertEqual((st['doneDay'], st['issue']), ('2026-11-23', 1))
                if text.startswith('{"version": 1'):
                    continue                                    # right version, wrong values: just ignored
                self.assertIn('the state file could not be read', self.gh.issues[1]['body'])

    def test_missing_state_folder_parent_is_made(self):
        self.write_machine(self.project([{'path': self.check}]), stateDir=os.path.join(self.tmp, 'a', 'b'))
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, 'a', 'b', 'example', 'state.json')))

    def test_stopped_after_the_issue_write_finds_the_same_issue_and_runs_again(self):
        """A stop between the issue write and the done marker: the next tick runs again and edits that issue."""
        real = cli.State.save
        calls = []

        def stop_on_done_marker(st, **changes):
            calls.append(changes)
            if 'doneDay' in changes:
                raise KeyboardInterrupt('stopped (test)')
            return real(st, **changes)
        with mock.patch.object(cli.State, 'save', stop_on_done_marker):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli('run')
        self.assertEqual(self.state().get('issue'), 1)
        self.assertNotIn('doneDay', self.state())
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.gh.writes(), ['create', 'edit'])
        self.assertEqual(self.gh.calls.count('search'), 1)              # the remembered number: no second search
        self.assertEqual(self.state()['doneDay'], '2026-11-23')

    def test_stopped_before_the_state_is_written_leaves_no_temp_files(self):
        def fail_replace(src, dst):
            raise OSError(28, 'No space left on device')
        with mock.patch.object(cli.os, 'replace', fail_replace):
            code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('example: failed', out)
        self.assertEqual(os.listdir(self.folder()), ['lock'])           # no state, no temporary file
        self.assertEqual(self.run_cli('run')[0], 0)                     # finds the issue it wrote by its marker
        self.assertEqual(self.gh.writes(), ['create', 'edit'])
        self.assertEqual(self.state()['doneDay'], '2026-11-23')

    def test_a_github_failure_leaves_no_done_marker_and_exits_1(self):
        self.gh.fail = 'create'
        code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('rate limit', out)
        self.assertFalse(os.path.exists(os.path.join(self.folder(), 'state.json')))
        self.gh.fail = None
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.state()['doneDay'], '2026-11-23')

    def test_a_held_lock_skips_the_project(self):
        os.makedirs(self.folder())
        held = cli.lock(self.folder())
        self.addCleanup(os.close, held)
        code, out = self.run_cli('run')
        self.assertEqual(code, 0)
        self.assertIn('another run is going', out)
        self.assertEqual(self.gh.calls, [])
        self.assertIsNone(cli.lock(self.folder()))

    def test_no_report_repo_writes_the_report_file_only(self):
        self.write_machine(self.project([{'path': self.check}], reportRepo=''))
        code, out = self.run_cli('run')
        self.assertEqual(code, 0, out)
        self.assertEqual(self.gh.calls, [])
        self.assertTrue(os.path.isfile(os.path.join(self.folder(), 'report.md')))
        self.assertNotIn('issue', self.state())


class DryRun(gf.Temp):

    def test_dry_run_prints_and_writes_nothing_even_after_a_run(self):
        fixer = self.script('fixer', '''
            import json, sys
            ctx = json.load(sys.stdin)
            print(json.dumps({"id": "demo.fix", "severity": "info", "text": "fix is " + str(ctx["fix"])}))
        ''')
        self.write_machine(self.project([{'path': fixer, 'fix': True}]))
        code, out = self.run_cli('run', '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('demo.fix: fix is False', out)                    # a fix check runs with fix off
        self.assertIn('nothing written', out)
        self.assertFalse(os.path.exists(self.state_dir))
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertIn('fix is True', self.gh.issues[1]['body'])         # a real run: fix on
        snapshot = sorted(os.listdir(os.path.join(self.state_dir, 'example')))
        before = self.state()
        code, out = self.run_cli('run', '--dry-run')                    # after the day's run it still prints
        self.assertEqual(code, 0)
        self.assertIn(report.marker('example'), out)
        self.assertEqual(self.state(), before)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state_dir, 'example'))), snapshot)
        self.assertEqual(self.gh.writes(), ['create'])

    def test_dry_run_with_a_broken_state_file_does_not_touch_it(self):
        os.makedirs(os.path.join(self.state_dir, 'example'))
        path = os.path.join(self.state_dir, 'example', 'state.json')
        with open(path, 'w') as f:
            f.write('{broken')
        self.write_machine(self.project([{'path': self.finding_script('demo', GOOD)}]))
        code, out = self.run_cli('run', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('the state file could not be read', out)
        with open(path) as f:
            self.assertEqual(f.read(), '{broken')


class Checks(gf.Temp):
    """The check contract: any failure is exactly one finding and the next check still runs."""

    def run_checks(self, *checks):
        good = self.finding_script('good', GOOD)
        self.write_machine(self.project(list(checks) + [{'path': good, 'id': 'after'}]))
        code, out = self.run_cli('run', '--dry-run')
        self.assertEqual(code, 0, out)
        self.assertIn('| warn | after | demo.thing: a thing to look at', out)     # the next check still ran
        return out

    def failed_rows(self, out):
        return [ln for ln in out.splitlines() if '| gardener.check-failed:' in ln]

    def test_a_check_that_hangs_is_stopped_with_its_children(self):
        pidfile = os.path.join(self.tmp, 'child.pid')
        hang = self.script('hang', f'''
            import subprocess, sys, time
            sys.stdin.read()
            child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
            open({pidfile!r}, 'w').write(str(child.pid))
            print('{{"id": "half", "severity": "info", "text": "printed before it hung"}}', flush=True)
            time.sleep(600)
        ''')
        start = time.monotonic()
        out = self.run_checks({'path': hang, 'timeoutSeconds': 1})
        self.assertLess(time.monotonic() - start, 10)
        rows = self.failed_rows(out)
        self.assertEqual(len(rows), 1, out)
        self.assertIn('stopped after its timeout of 1 s', rows[0])
        self.assertNotIn('printed before it hung', out)              # a stopped check's lines are not trusted
        with open(pidfile) as f:
            child = int(f.read())
        for _ in range(50):
            try:
                os.kill(child, 0)
            except ProcessLookupError:
                break
            time.sleep(0.1)
        else:
            self.fail('the hung check\'s child still runs')

    def test_a_check_that_prints_junk(self):
        for name, body in (('text', 'print("hello, I am not JSON")'),
                           ('list', 'print("[1, 2]")'),
                           ('no-text', 'print(\'{"id": "x", "severity": "warn"}\')'),
                           ('bad-severity', 'print(\'{"id": "x", "severity": "fatal", "text": "t"}\')'),
                           ('extra-key', 'print(\'{"id": "x", "severity": "info", "text": "t", "colour": 1}\')'),
                           ('binary', 'import sys; sys.stdout.buffer.write(bytes(range(256)))')):
            with self.subTest(name=name):
                good_then_junk = self.script(name, f'''
                    import sys
                    sys.stdin.read()
                    print('{{"id": "early", "severity": "warn", "text": "before the junk"}}')
                    {body}
                ''')
                out = self.run_checks({'path': good_then_junk, 'id': 'junk'})
                rows = self.failed_rows(out)
                self.assertEqual(len(rows), 1, out)
                self.assertIn('not a finding', rows[0])
                self.assertNotIn('before the junk', out)           # one failure finding, not half a report

    def test_too_much_output_and_too_many_findings(self):
        flood = self.script('flood', 'import sys\nsys.stdin.read()\nsys.stdout.write("x" * (2 << 20))\n')
        out = self.run_checks({'path': flood})
        self.assertIn(f'printed more than {contract.MAX_OUTPUT} bytes', self.failed_rows(out)[0])
        many = self.script('many', '''
            import json, sys
            sys.stdin.read()
            for i in range(250):
                print(json.dumps({"id": "n.%d" % i, "severity": "info", "text": "t"}))
        ''')
        out = self.run_checks({'path': many})
        self.assertIn('gardener.more-findings: 50 more finding(s) not shown', out)
        self.assertEqual(self.failed_rows(out), [])

    def test_a_check_that_exits_non_zero_or_cannot_start(self):
        crash = self.script('crash', 'import sys\nsys.stdin.read()\nraise SystemExit("the database is gone")\n')
        not_exec = self.script('not-exec', 'print("never")\n', mode=0o644)
        out = self.run_checks({'path': crash}, {'path': not_exec},
                              {'path': os.path.join(self.tmp, 'missing')})
        rows = self.failed_rows(out)
        self.assertEqual(len(rows), 3, out)
        self.assertIn('exited with code 1: the database is gone', out)
        self.assertEqual(sum('cannot start' in r for r in rows), 2)

    def test_a_missing_root_is_one_finding_per_check(self):
        self.write_machine(self.project([{'path': self.finding_script('good', GOOD)}],
                                        root=os.path.join(self.tmp, 'gone')))
        code, out = self.run_cli('run', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn('is not a folder', out)

    def test_the_check_gets_the_contract_context_in_its_root(self):
        spy = self.script('spy', f'''
            import json, os, sys
            ctx = json.load(sys.stdin)
            json.dump(dict(ctx, cwd=os.getcwd()), open({os.path.join(self.tmp, 'ctx.json')!r}, 'w'))
        ''')
        self.write_machine(self.project([{'path': spy, 'id': 'spy', 'options': {'a': 1}}]))
        self.assertEqual(self.run_cli('run')[0], 0)
        with open(os.path.join(self.tmp, 'ctx.json')) as f:
            ctx = json.load(f)
        self.assertEqual(ctx, {'version': contract.VERSION, 'check': 'spy', 'options': {'a': 1}, 'fix': False,
                               'today': '2026-11-23', 'gh': 'gh', 'cwd': self.root,
                               'project': {'name': 'example', 'repo': 'example-owner/app', 'root': self.root}})


class Window(gf.Temp):

    def setUp(self):
        super().setUp()
        self.check = self.finding_script('demo', GOOD)

    def at(self, when, *argv):
        with mock.patch.dict(os.environ, {'GARDENER_TEST_NOW': when}):
            return self.run_cli('run', *argv)

    def test_the_timer_waits_for_the_schedule_time(self):
        self.write_machine(self.project([{'path': self.check}], schedule='04:00'))
        code, out = self.at('2026-11-23T03:30:00+00:00', '--timer')
        self.assertEqual(code, 0)
        self.assertIn('not yet: it runs after 04:00', out)
        self.assertEqual(self.gh.calls, [])
        self.assertEqual(self.at('2026-11-23T04:10:00+00:00', '--timer')[0], 0)
        self.assertEqual(self.gh.writes(), ['create'])
        self.assertEqual(self.at('2026-11-23T03:30:00+00:00')[0], 0)    # without --timer: the done marker only
        self.assertEqual(self.gh.writes(), ['create'])

    def test_quiet_hours_that_wrap_past_midnight(self):
        self.write_machine(self.project([{'path': self.check}], quietHours='22:00-06:00'))
        for when, runs in (('2026-11-23T12:00:00+00:00', False), ('2026-11-23T23:00:00+00:00', True),
                           ('2026-11-24T05:59:00+00:00', True), ('2026-11-24T06:00:00+00:00', False)):
            with self.subTest(when=when):
                self.gh = gf.FakeGitHub()
                code, out = self.at(when, '--timer', '--project', 'example')
                self.assertEqual(code, 0)
                self.assertEqual(bool(self.gh.writes()), runs, out)
                if runs:
                    os.unlink(os.path.join(self.state_dir, 'example', 'state.json'))

    def test_force_runs_outside_the_window(self):
        self.write_machine(self.project([{'path': self.check}], schedule='23:00'))
        self.assertEqual(self.at('2026-11-23T03:30:00+00:00', '--timer', '--force')[0], 0)
        self.assertEqual(self.gh.writes(), ['create'])


class Config(gf.Temp):

    def test_a_missing_machine_file_or_section_is_refused(self):
        code, out = self.run_cli('run')
        self.assertEqual(code, 2)
        self.assertIn('config refused', out)
        with open(self.machine, 'w') as f:
            json.dump({'version': 1}, f)
        self.assertEqual(self.run_cli('run')[0], 2)

    def test_typos_and_bad_values_are_refused_whole(self):
        good = self.finding_script('demo', GOOD)
        for bad in ({'checks': [{'path': good, 'timeout': 5}]},
                    {'checks': [{'name': 'not-a-general-check'}]},
                    {'checks': [{'name': 'facts', 'path': good}]},
                    {'checks': [{'path': 'relative/check'}]},
                    {'checks': [{'path': good}, {'path': good}]},
                    {'checks': [], 'schedule': '25:00'},
                    {'checks': [], 'quietHours': '02:00-02:00'},
                    {'checks': [], 'timeZone': 'Nowhere/Town'},
                    {'checks': [], 'model': {'endpoint': 'ftp://x', 'model': 'm'}},
                    {'checks': [], 'model': {'endpoint': 'http://127.0.0.1:1/v1'}},
                    {'checks': [], 'repo': 'not a repo'},
                    {'checks': [], 'name': 'Upper Case'}):
            with self.subTest(bad=bad):
                self.write_machine(self.project(**bad))
                code, out = self.run_cli('run')
                self.assertEqual(code, 2, out)
                self.assertEqual(self.gh.calls, [])
        self.write_machine(self.project([]), self.project([]))
        self.assertEqual(self.run_cli('run')[0], 2)                     # the same name twice

    def test_check_lists_projects_without_running_anything(self):
        self.write_machine(self.project([{'name': 'worktrees', 'fix': True},
                                         {'path': self.finding_script('demo', GOOD)}],
                                        model={'endpoint': 'http://127.0.0.1:9/v1', 'model': 'local'}))
        code, out = self.run_cli('check')
        self.assertEqual(code, 0, out)
        self.assertIn('example: report to example-owner/app, after 00:00, model local', out)
        self.assertIn('worktrees: worktrees (fix), timeout 300 s', out)
        self.assertIn('demo: ', out)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_an_unknown_project_is_refused(self):
        self.write_machine(self.project([]))
        self.assertEqual(self.run_cli('run', '--project', 'other')[0], 2)

    def test_one_projects_failure_does_not_stop_the_next(self):
        bad = self.project([{'path': self.finding_script('demo', GOOD)}], name='first')
        good = self.project([{'path': self.finding_script('demo2', GOOD)}], name='second')
        self.write_machine(bad, good)
        os.makedirs(self.state_dir)
        with open(os.path.join(self.state_dir, 'first'), 'w') as f:      # a file where its folder should be
            f.write('x')
        code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('first: failed', out)
        self.assertEqual(self.state('second')['doneDay'], '2026-11-23')

    def test_the_example_machine_file_section_parses(self):
        here = os.path.dirname(gf.KIT)
        with open(os.path.join(here, 'common', 'machine.example.json')) as f:
            data = json.load(f)
        s = config.parse(data['gardener'], 'example')
        self.assertTrue(s['projects'])
        for p in s['projects']:
            for c in p['checks']:
                self.assertFalse(c['path'].startswith('/home/'), 'placeholders only in the public example')


MUTATIONS = {
    'the done marker skips a second run': (
        'cli', 'state.done_day() == today', 'False',
        'State.test_second_run_the_same_day_does_nothing_and_force_runs_again'),
    'the issue number is saved before the done marker': (
        'cli', '            state.save(issue=number)', '            pass',
        'State.test_stopped_after_the_issue_write_finds_the_same_issue_and_runs_again'),
    'a dry run writes nothing': (
        'cli', '        if args.dry_run:\n            print(part', '        if False:\n            print(part',
        'DryRun.test_dry_run_prints_and_writes_nothing_even_after_a_run'),
    'a dry run turns fix off': (
        'cli', "check['fix'] and not args.dry_run", "check['fix']",
        'DryRun.test_dry_run_prints_and_writes_nothing_even_after_a_run'),
    'a hung check is stopped': (
        'runner', "timeout=check['timeoutSeconds']", "timeout=check['timeoutSeconds'] * 15",
        'Checks.test_a_check_that_hangs_is_stopped_with_its_children'),
    'the hung check\'s group is killed': (
        'runner', 'start_new_session=True', 'start_new_session=False',
        'Checks.test_a_check_that_hangs_is_stopped_with_its_children'),
    'junk is one failure, not half a report': (
        'runner', '        except ValueError as e:\n', '        except ValueError as e:\n            continue\n',
        'Checks.test_a_check_that_prints_junk'),
    'the window holds the timer': (
        'cli', '        if args.timer and not args.force:', '        if False:',
        'Window.test_the_timer_waits_for_the_schedule_time'),
    'a held lock skips': (
        'cli', '        if held is None:\n            say', '        if False:\n            say',
        'State.test_a_held_lock_skips_the_project'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        gf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
