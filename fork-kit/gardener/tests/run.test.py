"""gardener run: the state question (empty, missing, broken, old and newer state; a second run the same day; a run
stopped half-way; a report issue that cannot be written), the check contract (a check that hangs, prints junk or
without end, exits non-zero or cannot start never stops the run; nor does one project's failure stop the next), dry
run, the lock, the timer window, what reaches the public issue, and the config. Project checks are small scripts written by each test;
GitHub is a fake (gardener_fixture.FakeGitHub). No root, no network.

  python3 -I -B fork-kit/gardener/tests/run.test.py
"""
import json, os, subprocess, sys, time, unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import gardener_fixture as gf  # noqa: E402
from gardener_fixture import cli, config, contract, report, runner  # noqa: E402

GOOD = {'id': 'demo.thing', 'severity': 'warn', 'where': 'docs/a.md', 'text': 'a thing to look at',
        'fix': 'look at it'}


class State(gf.Temp):

    def setUp(self):
        super().setUp()
        self.ran = os.path.join(self.tmp, 'ran')
        self.check = self.script('demo', f'''
            import json, sys
            sys.stdin.read()
            open({self.ran!r}, 'a').write('x')
            print(json.dumps({GOOD!r}))
        ''')
        self.write_machine(self.project([{'path': self.check, 'publicText': True}]))

    def runs(self):
        """How often the check ran."""
        try:
            with open(self.ran) as f:
                return len(f.read())
        except FileNotFoundError:
            return 0

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
        self.assertEqual((st['version'], st['doneDay'], st['issue'], st['findings'], st['reportPending'],
                          st['reportTries']), (1, '2026-11-23', 1, 1, False, 0))
        body = self.gh.issues[1]['body']
        self.assertIn(report.marker('example'), body)
        self.assertIn('demo.thing: a thing to look at', body)
        with open(os.path.join(self.folder(), 'issue.md')) as f:
            self.assertEqual(f.read(), body)
        with open(os.path.join(self.folder(), 'report.md')) as f:
            local = f.read()
        self.assertIn('Gardener report (local, in full)', local)
        self.assertIn(f'demo.thing: a thing to look at | docs/a.md', local)
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

    def test_stopped_after_the_issue_write_finds_the_same_issue_and_sends_it_alone(self):
        """A stop between the issue write and saving its number: the done marker is already there, so the next tick
        runs no check; it finds that issue by its title and edits it (no second issue)."""
        real = cli.State.save

        def stop_on_issue_number(st, **changes):
            if 'issue' in changes:
                raise KeyboardInterrupt('stopped (test)')
            return real(st, **changes)
        with mock.patch.object(cli.State, 'save', stop_on_issue_number):
            with self.assertRaises(KeyboardInterrupt):
                self.run_cli('run')
        st = self.state()
        self.assertEqual((st['doneDay'], st['reportPending']), ('2026-11-23', True))
        self.assertNotIn('issue', st)
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.runs(), 1)                                # the checks ran once
        self.assertEqual(self.gh.writes(), ['create', 'edit'])
        self.assertEqual(len(self.gh.issues), 1)
        self.assertEqual((self.state()['issue'], self.state()['reportPending']), (1, False))
        code, out = self.run_cli('run')
        self.assertIn('ran today already', out)
        self.assertEqual(self.gh.writes(), ['create', 'edit'])

    def test_stopped_before_the_state_is_written_leaves_no_temp_files(self):
        def fail_replace(src, dst):
            raise OSError(28, 'No space left on device')
        with mock.patch.object(cli.os, 'replace', fail_replace):
            code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('example: failed', out)
        self.assertEqual(os.listdir(self.folder()), ['lock'])           # no state, no temporary file
        self.assertEqual(self.gh.calls, [])                             # the files first, then GitHub
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.gh.writes(), ['create'])
        self.assertEqual(self.state()['doneDay'], '2026-11-23')

    def test_a_github_failure_marks_the_day_and_the_next_tick_sends_the_issue_alone(self):
        self.gh.fail = 'create'
        code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('rate limit', out)
        st = self.state()
        self.assertEqual((st['doneDay'], st['reportPending'], st['reportTries']), ('2026-11-23', True, 1))
        self.gh.fail = None
        self.gh.calls.clear()
        self.assertEqual(self.run_cli('run')[0], 0)
        self.assertEqual(self.runs(), 1)                                # no check ran again
        self.assertEqual(self.gh.writes(), ['create'])
        self.assertEqual((self.state()['reportPending'], self.state()['issue']), (False, 1))

    def test_a_report_issue_that_cannot_be_written_is_tried_three_times_a_day(self):
        self.gh.fail = 'create'
        for n in range(cli.MAX_REPORT_TRIES):
            self.assertEqual(self.run_cli('run')[0], 1)
            self.assertEqual(self.state()['reportTries'], n + 1)
        self.assertFalse(self.state()['reportPending'])
        calls = len(self.gh.calls)
        code, out = self.run_cli('run')
        self.assertEqual(code, 0)
        self.assertIn('ran today already', out)
        self.assertEqual(len(self.gh.calls), calls)                     # no more GitHub calls today
        self.assertEqual(self.runs(), 1)
        self.gh.fail = None
        with mock.patch.dict(os.environ, {'GARDENER_TEST_NOW': '2026-11-24T03:30:00+00:00'}):
            self.assertEqual(self.run_cli('run')[0], 0)                 # the next day: a whole run again
        self.assertEqual(self.runs(), 2)
        self.assertEqual((self.state()['reportTries'], self.state()['issue']), (0, 1))

    def test_a_newer_state_file_is_refused_and_kept(self):
        text = json.dumps({'version': 2, 'doneDay': '2026-11-20', 'issue': 7, 'newThing': [1]})
        self.write_state(text)
        code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('newer gardener (version 2)', out)
        with open(os.path.join(self.folder(), 'state.json')) as f:
            self.assertEqual(f.read(), text)
        self.assertEqual((self.gh.calls, self.runs()), ([], 0))

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
        with open(os.path.join(self.state_dir, 'example', 'report.md')) as f:
            self.assertIn('fix is True', f.read())                      # a real run: fix on
        snapshot = {n: os.stat(os.path.join(self.state_dir, 'example', n)).st_mtime_ns
                    for n in os.listdir(os.path.join(self.state_dir, 'example'))}
        before = self.state()
        code, out = self.run_cli('run', '--dry-run')                    # after the day's run it still prints
        self.assertEqual(code, 0)
        self.assertIn(report.marker('example'), out)
        self.assertEqual(self.state(), before)
        self.assertEqual({n: os.stat(os.path.join(self.state_dir, 'example', n)).st_mtime_ns
                          for n in os.listdir(os.path.join(self.state_dir, 'example'))}, snapshot)
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

    def parts(self, out):
        """(the issue part, the local report) of a dry run's output."""
        head, _, local = out.partition('dry run: the local report.md would get:')
        return head.partition('dry run: the report issue would get:')[2], local

    def failed_rows(self, out):
        return [ln for ln in self.parts(out)[1].splitlines() if '| gardener.check-failed:' in ln]

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

    def test_a_check_that_prints_without_end_is_stopped_at_the_cap(self):
        """Like `yes` as a check, in a process with little memory: read as it comes, stopped at the cap within
        seconds (not at its timeout), one finding; the next project still runs."""
        yes = self.script('yes', 'import sys\nsys.stdin.read()\nwhile True:\n    sys.stdout.write("y\\n" * 4096)\n')
        self.write_machine(self.project([{'path': yes, 'timeoutSeconds': 120}], name='first'),
                           self.project([{'path': self.finding_script('demo', GOOD)}], name='second'))
        code = (f'import resource, sys\n'
                f'resource.setrlimit(resource.RLIMIT_AS, (400 << 20, 400 << 20))\n'
                f'sys.path.insert(0, {gf.LIB!r})\n'
                f'from gardener import cli, config\n'
                f'config.MACHINE_PATH, config.OWNER_UID = {self.machine!r}, {os.getuid()}\n'
                f'sys.exit(cli.main(["run", "--dry-run"]))\n')
        start = time.monotonic()
        r = subprocess.run([sys.executable, '-I', '-B', '-c', code], capture_output=True, text=True, timeout=200,
                           env=dict(os.environ, GARDENER_TEST_NOW=gf.NOW), stdin=subprocess.DEVNULL)
        self.assertLess(time.monotonic() - start, 30)
        self.assertEqual(r.returncode, 0, r.stdout[-2000:] + r.stderr[-2000:])
        self.assertIn(f'first: check yes: failed: printed more than {contract.MAX_OUTPUT} bytes', r.stdout)
        self.assertIn('second: dry run: 1 finding(s)', r.stdout)

    def test_an_error_in_the_gardener_on_one_check_is_one_finding(self):
        real = runner.run

        def boom(project, check, *a):
            if check['id'] == 'first':
                raise MemoryError()
            return real(project, check, *a)
        with mock.patch.object(runner, 'run', boom):
            out = self.run_checks({'path': self.finding_script('one', GOOD), 'id': 'first'})
        rows = self.failed_rows(out)
        self.assertEqual(len(rows), 1, out)
        self.assertIn('the gardener could not run it (MemoryError)', rows[0])

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
        issue, local = self.parts(out)
        self.assertIn('gardener.check-failed: exited with code 1 (the database is gone)', local)
        self.assertIn('gardener.check-failed: exited with code 1 |', issue)
        self.assertNotIn('the database is gone', issue)                 # stderr: the local report only
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
        cache = os.path.join(self.state_dir, 'example', 'cache', 'spy')
        self.assertEqual(ctx, {'version': contract.VERSION, 'check': 'spy', 'options': {'a': 1}, 'fix': False,
                               'today': '2026-11-23', 'gh': 'gh', 'cwd': self.root, 'cacheDir': cache,
                               'dryRun': False,
                               'project': {'name': 'example', 'repo': 'example-owner/app', 'root': self.root}})
        self.assertFalse(os.path.exists(cache))                        # the check makes it when it needs it
        self.assertEqual(self.run_cli('run', '--dry-run')[0], 0)
        with open(os.path.join(self.tmp, 'ctx.json')) as f:
            self.assertIs(json.load(f)['dryRun'], True)


class Privacy(gf.Temp):
    """The report repository may be public: each general check (and a project check) run on things outside the
    repository, with host names, addresses and private names around; none of it reaches the issue. The local
    report.md has it all."""

    def git(self, repo, *args):
        env = dict(os.environ, GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid', GIT_COMMITTER_NAME='t',
                   GIT_COMMITTER_EMAIL='t@example.invalid', GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
        subprocess.run(['git', '-C', repo, *args], check=True, capture_output=True, env=env)

    def write(self, path, text):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(text)

    def test_nothing_local_reaches_the_issue(self):
        t = self.tmp
        # context-budget: a memory index in an encoded home folder, outside the repository
        mem = os.path.join(t, 'claude', 'projects', '-home-someone--secret-proj', 'memory')
        self.write(os.path.join(mem, 'MEMORY.md'), '- [Gone](client-acme-plan.md) — x\n')
        self.write(os.path.join(mem, 'acquisition-notes.md'), 'o')
        self.write(os.path.join(self.root, 'AGENTS.md'), 'x' * 50)
        # facts: the machine's table (outside) and the project's own (inside)
        facts = self.script('facts', f'''
            print('facts check: {t}/machine-facts/STATE.md:12: "VM900 build-box": Updated 2026-01-01 is 326 days '
                  'old (staleDays 30): check the state, then edit the row')
            print('facts check: {self.root}/facts/STATE.md:5: "Report kit": Updated 2026-01-01 is 326 days old '
                  '(staleDays 30): check the state, then edit the row')
            print('facts check: 2 finding(s)')
            raise SystemExit(1)
        ''')
        # worktrees: a merged branch whose worktree is outside the repository
        self.git(self.root, 'init', '-q', '-b', 'main')
        self.git(self.root, 'add', 'AGENTS.md')
        self.git(self.root, 'commit', '-q', '-m', 'one')
        self.git(self.root, 'checkout', '-q', '-b', 'client-acme-merger')
        self.write(os.path.join(self.root, 'b.txt'), 'b')
        self.git(self.root, 'add', 'b.txt')
        self.git(self.root, 'commit', '-q', '-m', 'b')
        self.git(self.root, 'checkout', '-q', 'main')
        self.git(self.root, 'merge', '-q', '--ff-only', 'client-acme-merger')
        self.git(self.root, 'worktree', 'add', '-q', os.path.join(t, 'trees', 'w'), 'client-acme-merger')
        # workflows-off: the report repository and a private one
        gh = self.script('gh', '''
            import json, sys
            print(json.dumps({"workflows": [{"name": "Off", "path": ".github/workflows/off.yml",
                                             "state": "disabled_manually"}]}))
        ''')
        # install-drift: a kit record under a fake host
        host = os.path.join(t, 'host')
        self.write(os.path.join(host, 'etc', 'kit-secret', 'installed.tsv'), '/opt/kit-secret/tool\tfile\t' + '0' * 64 + '\n')
        # a project check: stderr with a host and address, and findings naming local things
        leaky = self.script('leaky', f'''
            import json, sys
            sys.stdin.read()
            for f in [{{"id": "host.payroll-box.down", "severity": "warn", "where": "{t}/elsewhere/payroll.md",
                        "text": "payroll"}},
                      {{"id": "p.in", "severity": "warn", "where": "docs/a.md:3",
                        "text": "see http://100.64.0.1:8080/v1 and {t}/x and nas.local",
                        "fix": "ssh admin@10.0.0.7 or mail someone@example.org"}}]:
                print(json.dumps(f))
            sys.stderr.write("ssh: connect to host build-box.lan (10.1.2.3) port 22: refused\\n")
            sys.exit(255)
        ''')
        quiet = self.script('quiet', f'''
            import json, sys
            sys.stdin.read()
            print(json.dumps({{"id": "p.in", "severity": "warn", "where": "docs/a.md:3",
                              "text": "see http://100.64.0.1:8080/v1 and {t}/x and nas.local",
                              "fix": "ssh admin@10.0.0.7 or mail someone@example.org"}}))
            print(json.dumps({{"id": "p.out", "severity": "warn", "where": "{t}/elsewhere/payroll.md",
                              "text": "payroll"}}))
        ''')
        sock = __import__('socket').socket()
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
        sock.close()
        self.write_machine(self.project(
            [{'name': 'context-budget', 'options': {'files': {'AGENTS.md': 40}, 'memoryIndexes': [
                os.path.join(mem, 'MEMORY.md')]}},
             {'name': 'facts', 'options': {'command': facts}},
             {'name': 'worktrees'},
             {'name': 'workflows-off', 'options': {'repos': ['example-owner/app', 'someone/private-repo']}},
             {'name': 'install-drift', 'options': {'etc': '/etc'}},
             {'path': leaky}, {'path': quiet, 'publicText': True}],
            model={'endpoint': f'http://127.0.0.1:{port}/v1', 'model': 'm', 'timeoutSeconds': 5}), gh=gh)
        with mock.patch.dict(os.environ, {'GARDENER_TEST_PREFIX': host}):
            code, out = self.run_cli('run')
        self.assertEqual(code, 0, out)
        issue = self.gh.issues[1]['body']
        with open(os.path.join(self.state_dir, 'example', 'report.md')) as f:
            local = f.read()
        secrets = ('-home-someone', 'secret-proj', 'client-acme', 'acquisition-notes', 'VM900', 'build-box',
                   'private-repo', 'kit-secret', 'payroll', '100.64', '10.0.0.7', '10.1.2.3', 'admin@',
                   'someone@', 'nas.local', 'refused', str(port), t, os.path.expanduser('~'))
        for secret in secrets:
            self.assertNotIn(secret, issue)
        for kept in ('client-acme', 'VM900', 'build-box.lan', 'private-repo', 'kit-secret', 'payroll',
                     'acquisition-notes', '10.0.0.7'):
            self.assertIn(kept, local)
        for shown in ('| AGENTS.md |', 'facts/STATE.md:5', 'Report kit', 'example-owner/app: .github/workflows/off.yml',
                      'docs/a.md:3', 'gardener.check-failed: exited with code 255', 'judgment: model unreachable',
                      '<outside repo>', 'in the local report only', 'see <url> and <path> and <host>',
                      'ssh <user@host> or mail <user@host>'):
            self.assertIn(shown, issue)
        self.assertNotIn('host.', issue)                                # no hidden finding's id

    def test_a_private_project_reporting_to_a_public_repository_shows_counts_only(self):
        """Visibility is read once per repository; a private project repository (or one not set, or unreadable)
        and a public report repository: counts only, no judgment."""
        demo = self.finding_script('demo', GOOD)
        cases = (('example-owner/private-app', {'example-owner/reports': False, 'example-owner/private-app': True}, True),
                 ('example-owner/open-app', {'example-owner/reports': False, 'example-owner/open-app': False}, False),
                 ('example-owner/unknown-app', {'example-owner/reports': False}, True),
                 ('', {'example-owner/reports': False}, True),
                 ('example-owner/private-app', {'example-owner/reports': True}, False))
        for repo, visibility, counts in cases:
            with self.subTest(repo=repo, visibility=visibility):
                self.gh.visibility = visibility
                self.gh.calls.clear()
                self.write_machine(self.project([{'path': demo, 'publicText': True}], repo=repo,
                                                reportRepo='example-owner/reports'))
                code, out = self.run_cli('run', '--dry-run')
                self.assertEqual(code, 0, out)
                issue = out.partition('dry run: the report issue would get:')[2].partition('dry run: the local')[0]
                self.assertEqual(report.COUNTS_ONLY in issue, counts, issue)
                self.assertEqual('a thing to look at' in issue, not counts, issue)
                self.assertEqual(self.gh.writes(), [])
                self.assertLessEqual(self.gh.calls.count('private'), 2)


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
        for when, runs in (('2026-11-23T12:00:00+00:00', False), ('2026-11-23T21:59:00+00:00', False),
                           ('2026-11-23T22:00:00+00:00', True), ('2026-11-24T05:59:00+00:00', True),
                           ('2026-11-24T06:00:00+00:00', False)):
            with self.subTest(when=when):
                self.gh = gf.FakeGitHub()
                code, out = self.at(when, '--timer', '--project', 'example')
                self.assertEqual(code, 0)
                self.assertEqual(bool(self.gh.writes()), runs, out)
                if runs:
                    os.unlink(os.path.join(self.state_dir, 'example', 'state.json'))

    def test_a_window_past_midnight_is_one_day_and_runs_once(self):
        """The day is the window's start: 23:00 and 01:00 are the same window, one run. A schedule inside the window
        holds the timer until then; a tick missed in the hour before midnight still runs after it."""
        self.write_machine(self.project([{'path': self.check}], quietHours='22:00-06:00', schedule='23:00'))
        self.assertEqual(self.at('2026-11-23T22:30:00+00:00', '--timer')[0], 0)
        self.assertEqual(self.gh.writes(), [])                          # before the schedule
        for when in ('2026-11-23T23:10:00+00:00', '2026-11-24T00:10:00+00:00', '2026-11-24T05:10:00+00:00'):
            self.assertEqual(self.at(when, '--timer')[0], 0)
        self.assertEqual(self.gh.writes(), ['create'])                  # once in the window
        self.assertEqual(self.state()['doneDay'], '2026-11-23')
        self.at('2026-11-24T23:10:00+00:00', '--timer')
        self.assertEqual(self.gh.writes(), ['create', 'edit'])          # the next window
        self.assertEqual(self.state()['doneDay'], '2026-11-24')
        os.unlink(os.path.join(self.state_dir, 'example', 'state.json'))
        self.gh = gf.FakeGitHub()
        self.at('2026-11-26T01:00:00+00:00', '--timer')                 # 23:00-24:00 missed: 01:00 still runs
        self.assertEqual(self.gh.writes(), ['create'])
        self.assertEqual(self.state()['doneDay'], '2026-11-25')

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
                    {'checks': [{'name': 'facts', 'publicText': True}]},
                    {'checks': [{'path': good, 'publicText': 'yes'}]},
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
        self.assertIn('example: report to example-owner/app, any time of day, model local', out)
        self.assertIn('worktrees: worktrees (fix), timeout 300 s', out)
        self.assertIn('demo: ', out)
        self.assertFalse(os.path.exists(self.state_dir))

    def test_an_unknown_project_is_refused(self):
        self.write_machine(self.project([]))
        self.assertEqual(self.run_cli('run', '--project', 'other')[0], 2)

    def test_an_error_of_any_kind_in_one_project_does_not_stop_the_next(self):
        real = report.render_issue

        def boom(name, *a, **kw):
            if name == 'first':
                raise MemoryError()
            return real(name, *a, **kw)
        self.write_machine(self.project([{'path': self.finding_script('demo', GOOD)}], name='first'),
                           self.project([{'path': self.finding_script('demo2', GOOD)}], name='second'))
        with mock.patch.object(report, 'render_issue', boom):
            code, out = self.run_cli('run')
        self.assertEqual(code, 1)
        self.assertIn('first: failed: MemoryError', out)
        self.assertEqual(self.state('second')['doneDay'], '2026-11-23')

    def test_a_report_repository_changed_the_same_day_runs_again_for_the_new_one(self):
        """A part rendered (and allow-listed) for one report repository never goes to another; the old repository's
        issue number is not used in the new one."""
        demo = self.finding_script('demo', GOOD)
        self.gh.fail = 'create'
        self.write_machine(self.project([{'path': demo, 'publicText': True}], reportRepo='example-owner/old-reports'))
        self.assertEqual(self.run_cli('run')[0], 1)
        self.assertTrue(self.state()['reportPending'])
        self.gh.fail = None
        self.gh.visibility = {'example-owner/new-reports': True}
        self.write_machine(self.project([{'path': demo, 'publicText': True}], reportRepo='example-owner/new-reports'))
        code, out = self.run_cli('run')
        self.assertEqual(code, 0, out)
        self.assertIn('the report repository changed today', out)
        self.assertEqual([i['repo'] for i in self.gh.issues.values()], ['example-owner/new-reports'])
        st = self.state()
        self.assertEqual((st['reportRepo'], st['reportPending'], st['issue']), ('example-owner/new-reports', False, 1))
        self.assertIn('ran today already', self.run_cli('run')[1])

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
        'cli', 'not args.force and state.done_day() == today', 'not args.force and False',
        'State.test_second_run_the_same_day_does_nothing_and_force_runs_again'),
    'a pending report is sent alone on the next tick': (
        'cli', "            if state.data.get('reportPending') is True and project['reportRepo']:",
        '            if False:', 'State.test_stopped_after_the_issue_write_finds_the_same_issue_and_sends_it_alone'),
    'the done marker comes before the issue write': (
        'cli', "reportTries=0, reportRepo=project['reportRepo'])\n",
        "reportTries=0, reportRepo=project['reportRepo']) if False else None\n",
        'State.test_a_github_failure_marks_the_day_and_the_next_tick_sends_the_issue_alone'),
    'the issue is tried at most MAX_REPORT_TRIES a day': (
        'cli', 'reportPending=tries < MAX_REPORT_TRIES)', 'reportPending=True)',
        'State.test_a_report_issue_that_cannot_be_written_is_tried_three_times_a_day'),
    'a newer state is refused': (
        'cli', '        if state.newer and not args.dry_run:', '        if False:',
        'State.test_a_newer_state_file_is_refused_and_kept'),
    'a dry run writes nothing': (
        'cli', "        if args.dry_run:\n            say(f'{name}: dry run", "        if False:\n            say(f'{name}: dry run",
        'DryRun.test_dry_run_prints_and_writes_nothing_even_after_a_run'),
    'a dry run turns fix off': (
        'cli', "check['fix'] and not args.dry_run", "check['fix']",
        'DryRun.test_dry_run_prints_and_writes_nothing_even_after_a_run'),
    'a hung check is stopped': (
        'runner', "time.monotonic() + check['timeoutSeconds']", "time.monotonic() + check['timeoutSeconds'] * 15",
        'Checks.test_a_check_that_hangs_is_stopped_with_its_children'),
    'the hung check\'s group is killed': (
        'runner', 'start_new_session=True', 'start_new_session=False',
        'Checks.test_a_check_that_hangs_is_stopped_with_its_children'),
    'junk is one failure, not half a report': (
        'runner', '        except ValueError as e:\n', '        except ValueError as e:\n            continue\n',
        'Checks.test_a_check_that_prints_junk'),
    'the window holds the timer': (
        'cli', '        if args.timer and not args.force and why_not:', '        if False:',
        'Window.test_the_timer_waits_for_the_schedule_time'),
    'output is read up to the cap': (
        'runner', "                    if len(out) > contract.MAX_OUTPUT:\n                        return out, err, 'too-much'\n",
        '', 'Checks.test_a_check_that_prints_without_end_is_stopped_at_the_cap'),
    'an error on one check is one finding': (
        'cli', '            except Exception as e:             # a MemoryError too', '            except OSError as e:               #',
        'Checks.test_an_error_in_the_gardener_on_one_check_is_one_finding'),
    'an error in one project never stops the next': (
        'cli', '        except Exception as e:               # anything', '        except OSError as e:                 #',
        'Config.test_an_error_of_any_kind_in_one_project_does_not_stop_the_next'),
    'the issue is built from the allow-list': (
        'cli', "        part = report.render_issue(name, today, findings, notes, answer, root, home, project['reportRepo'],\n"
               "                                   text_checks, counts, project['repo'])",
        "        part = report.render_local(name, today, findings, notes, answer)",
        'Privacy.test_nothing_local_reaches_the_issue'),
    'a project check\'s text is public only with publicText': (
        'cli', "        text_checks = {c['id'] for c in project['checks'] if c['name'] or c['publicText']}",
        "        text_checks = {c['id'] for c in project['checks']}", 'Privacy.test_nothing_local_reaches_the_issue'),
    'a private project to a public report repository: counts only': (
        'cli', '        return not pr or gh.private(pr) is not False', '        return False',
        'Privacy.test_a_private_project_reporting_to_a_public_repository_shows_counts_only'),
    'an unreadable visibility is counts only': (
        'cli', '    except report.GitHubError:\n        return True', '    except report.GitHubError:\n        return False',
        'Privacy.test_a_private_project_reporting_to_a_public_repository_shows_counts_only'),
    'a changed report repository runs again': (
        'cli', "        moved = (state.data.get('reportRepo') or '') != project['reportRepo']", '        moved = False',
        'Config.test_a_report_repository_changed_the_same_day_runs_again_for_the_new_one'),
    'publicText only for project checks': (
        'config', "    if c['name'] and c['publicText']:", '    if False:',
        'Config.test_typos_and_bad_values_are_refused_whole'),
    'the day of a window past midnight is its start': (
        'cli', "    day = (lt - datetime.timedelta(minutes=pos)).date().isoformat()", "    day = lt.date().isoformat()",
        'Window.test_a_window_past_midnight_is_one_day_and_runs_once'),
    'a held lock skips': (
        'cli', '        if held is None:\n            say', '        if False:\n            say',
        'State.test_a_held_lock_skips_the_project'),
}


class RedCheck(unittest.TestCase):

    def test_each_guard_has_a_test_that_fails_without_it(self):
        gf.red_check(self, os.path.abspath(__file__), MUTATIONS)


if __name__ == '__main__':
    unittest.main(verbosity=2)
