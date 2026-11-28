#!/usr/bin/env python3
"""mutation_report.py and its action: each module's kill rate beside its baseline and its last runs, one rolling issue
whose trend survives a closed issue, two open copies and a run stopped between two of its writes, and one red issue
while a module has no score. Ported from a project's tests of the same report; FakeGitHub plays gh with state.
Run: python3 fork-ci/actions/mutation-report/mutation_report.test.py
"""
import contextlib, datetime, io, json, os, shutil, subprocess, sys, tempfile, textwrap, unittest

import yaml

sys.dont_write_bytecode = True   # leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'mutation_report.py')
ACTION = os.path.join(HERE, 'action.yml')
sys.path.insert(0, HERE)
import mutation_report as R  # noqa: E402


def result(name, rate, surv=()):
    return {'module': name, 'file': f'{name}.py', 'kill_rate': rate, 'mutants': 10, 'killed': 5, 'timeout': 0,
            'survived': len(surv), 'no_cover': 0,
            'survivors': [{'line': ln, 'what': '`==` -> `!=`', 'code': 'x == y', 'why': 'tests pass'} for ln in surv]}


class Report(unittest.TestCase):
    def test_judge(self):
        base = {'a': {'kill_rate': 60.0}, 'b': {'kill_rate': 50.0}, 'gone': {'kill_rate': 1.0}}
        res = {'a': result('a', 59.9), 'b': result('b', 55.0), 'new': result('new', 10.0),
               'err': {'module': 'err', 'error': 'tests red'}}
        rows, problems = R.judge(['a', 'b', 'new', 'err', 'missing'], res, base)
        self.assertEqual([r[1] for r in rows], ['dropped', 'improved', 'new', 'error', 'missing'])
        text = ' '.join(problems)
        for word in ('err: tests red', 'missing: no result', 'gone: in the baseline'):
            self.assertIn(word, text)
        self.assertNotIn('a:', text)
        self.assertEqual(R.judge(['a'], {'a': result('a', 10.0)}, {'a': base['a']}),
                         ([('a', 'dropped', 'below its baseline 60.0%')], []))
        self.assertEqual(R.judge(['a'], {'a': result('a', 60.0)}, base)[0][0][1], 'ok')

    def test_a_module_with_an_error_shows_the_parts_that_finished(self):
        s = {'file': 'A.java', 'line': 3, 'what': 'f: bug', 'why': 'tests pass', 'code': 'x'}
        r = {'error': 'two: PIT stopped', 'finished_parts': [{'part': 'one', 'mutants': 10, 'killed': 7, 'timeout': 1, 'no_cover': 1, 'survivors': [s]}]}
        text = R.section('java/m', r, 10)
        self.assertIn('ERROR: two: PIT stopped', text)
        self.assertIn('`one`: 8 of 10', text)
        self.assertIn('A.java:3', text)
        self.assertNotIn('kill rate', text.lower())
        self.assertTrue(text.endswith('<!-- /module:java/m -->'), text)   # the marker the next report replaces it by

    def test_the_report_says_how_many_bugs_were_caught_only_by_a_timeout(self):   # they count as caught: the reader must see them
        r = dict(result('m', 80.0), mutants=10, killed=5, timeout=3, survivors=[])   # timing: a count of mutants, not seconds
        self.assertIn('3 of 10 were caught only by a time limit', R.section('java/m', r, 10))
        self.assertNotIn('time limit', R.section('java/m', dict(r, timeout=0), 10))   # timing: a count of mutants, not seconds

    def test_a_result_without_a_score_is_an_error_never_a_pass(self):
        base = {'a': {'kill_rate': 60.0}}
        for r in ({'module': 'a', 'mutants': 0, 'kill_rate': 100.0, 'survived': 0, 'no_cover': 0},
                  {'module': 'a', 'mutants': 5, 'survived': 0, 'no_cover': 0},
                  {'module': 'a', 'mutants': 5, 'kill_rate': None, 'survived': 0, 'no_cover': 0}):
            rows, problems = R.judge(['a'], {'a': r}, base)
            self.assertEqual(rows[0][1], 'error', r)
            self.assertIn('a: no score', ' '.join(problems))
            body = R.render(rows, problems, {'a': r}, base, '', 'u', 'c' * 40, datetime.datetime(2026, 11, 8))
            self.assertIn('❌', body)
            self.assertIn('ERROR: no score', body)
            self.assertNotIn('a', R.suggest({'a': r}, {}, 'c' * 40, datetime.datetime(2026, 11, 8))['modules'])

    def test_left_out_test_files_are_shown(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, 'py.json'), 'w') as f:
            json.dump({'modules': [result('a', 50.0)], 'skipped_tests': {'t/test_x.py': 'fails without any mutation'}}, f)
        skipped = {}
        res = R.load_results([d], skipped)
        rows, problems = R.judge(['a'], res, {'a': {'kill_rate': 50.0}})
        body = R.render(rows, problems, res, {}, '', 'u', 'c' * 40, datetime.datetime(2026, 11, 8), skipped=skipped)
        self.assertIn('⚠️ 1 test file(s) left out this run', body)
        self.assertIn('`t/test_x.py`', body)

    def test_a_red_night_opens_or_comments_on_the_red_issue_and_a_green_night_closes_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = lambda n: os.path.join(d, n)
        with open(p('targets.json'), 'w') as f:
            json.dump({'modules': {'a': {}}}, f)
        with open(p('base.json'), 'w') as f:
            json.dump({'modules': {'a': {'kill_rate': 50.0}}}, f)

        def night(kill_rate, open_red):   # kill_rate None: the module has no score
            with open(p('r.json'), 'w') as f:
                json.dump({'modules': [result('a', kill_rate) if kill_rate is not None
                                       else {'module': 'a', 'error': 'PIT stopped: no progress'}]}, f)
            calls = []

            def run(cmd, stdin=None):
                calls.append((cmd, stdin))
                if cmd[1:3] != ['issue', 'list']:
                    return ''
                red = f'"{R.RED_TITLE}" in:title' in cmd
                return json.dumps([{'number': n, 'title': R.RED_TITLE, 'author': BOT} for n in open_red] if red else [])
            with contextlib.redirect_stdout(io.StringIO()):
                rc = R.main(['--results', p('r.json'), '--baseline', p('base.json'), '--expect', p('targets.json'),
                             '--issue', '--repo', 'o/r', '--commit', 'f' * 40], run=run)
            return rc, [c for c in calls if c[0][1:3] != ['issue', 'list'] and c[0][2] != 'create' or
                        (c[0][2] == 'create' and R.RED_TITLE in c[0])]
        rc, w = night(None, [])   # red, none open: one is created, with no label by default, naming the problem
        self.assertEqual(rc, 1)
        create = [c for c in w if c[0][1:3] == ['issue', 'create'] and R.RED_TITLE in c[0]]
        self.assertEqual(len(create), 1, w)
        self.assertNotIn('--label', create[0][0])   # labels come from the config (ConfigTitles)
        self.assertIn('a: PIT stopped: no progress', create[0][1])
        rc, w = night(None, [12, 7])   # red again: a comment on the oldest, never a second issue
        self.assertEqual([c[0][1:4] for c in w if c[0][1] == 'issue' and c[0][2] in ('comment', 'create')
                          and (c[0][2] == 'comment' or R.RED_TITLE in c[0])], [['issue', 'comment', '7']])
        rc, w = night(55.0, [7])   # green: closed
        self.assertEqual(rc, 0)
        self.assertIn(['issue', 'close', '7'], [c[0][1:4] for c in w])
        rc, w = night(55.0, [])   # green, nothing open: no close, no create
        self.assertFalse([c for c in w if c[0][2] in ('close', 'comment') or R.RED_TITLE in c[0]], w)
        rc, w = night(40.0, [7])
        self.assertEqual(rc, 0)
        self.assertIn(['issue', 'close', '7'], [c[0][1:4] for c in w])

    def test_far_fewer_planted_bugs_than_the_baseline_is_an_error(self):   # a 100% over a 3-line shim is no pass
        base = {'a': {'kill_rate': 60.0, 'mutants': 600}}
        rows, problems = R.judge(['a'], {'a': dict(result('a', 100.0), mutants=2)}, base)
        self.assertEqual(rows[0][1], 'error')
        self.assertIn('only 2 planted bugs, the baseline had 600', ' '.join(problems))
        rows, problems = R.judge(['a'], {'a': dict(result('a', 61.0), mutants=300)}, base)   # half is still a score
        self.assertEqual((rows[0][1], problems), ('improved', []))

    def test_missing_or_broken_baseline(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        self.assertIn('missing', R.load_baseline(os.path.join(d, 'nope.json'))[1])
        with open(os.path.join(d, 'half.json'), 'w') as f:
            f.write('{"modules": {"a": ')
        self.assertIn('unreadable', R.load_baseline(os.path.join(d, 'half.json'))[1])
        with open(os.path.join(d, 'nokr.json'), 'w') as f:
            json.dump({'modules': {'a': {'mutants': 3}}}, f)
        self.assertIn('no kill_rate for: a', R.load_baseline(os.path.join(d, 'nokr.json'))[1])

    def test_a_suggested_baseline_in_the_results_folder_is_not_a_result(self):   # a local rerun into one folder
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        with open(os.path.join(d, 'py.json'), 'w') as f:
            json.dump({'modules': [result('a', 50.0)]}, f)
        with open(os.path.join(d, 'baseline-suggested.json'), 'w') as f:
            json.dump(R.suggest({'a': result('a', 50.0)}, {}, 'c' * 40, datetime.datetime(2026, 11, 8)), f)
        self.assertEqual(list(R.load_results([d])), ['a'])

    def test_render_keeps_a_module_that_did_not_run(self):
        res = {'a': result('a', 70.0, surv=[3, 3, 9])}
        rows, problems = R.judge(['a', 'b'], res, {'a': {'kill_rate': 70.0}, 'b': {'kill_rate': 40.0}})
        old = R.render(*R.judge(['b'], {'b': result('b', 40.0, surv=[5])}, {'b': {'kill_rate': 40.0}}),
                       {'b': result('b', 40.0, surv=[5])}, {}, '', 'u', 'c' * 40, datetime.datetime(2026, 11, 7))
        body = R.render(rows, problems, res, {}, old, 'u', 'd' * 40, datetime.datetime(2026, 11, 8))
        self.assertIn('#### b (not refreshed this run', body)
        self.assertIn('(2026-11-07 run)', body)   # the section says which night it is from
        again = R.render(rows, problems, res, {}, body, 'u', 'e' * 40, datetime.datetime(2026, 11, 9))
        self.assertEqual(again.count('not refreshed'), 1)   # missing two nights: still one marker
        self.assertIn('(2026-11-07 run)', again)
        self.assertIn('b.py:5', body)
        self.assertIn('| 2 | `a.py:3` |', body)   # top list: most survivors per line first
        self.assertIn('<details>', body)
        self.assertIn('❌', body)

    def runs(self, old_body, rates, run_url, day, commit='c' * 40, expect=('a', 'b')):
        res = {n: result(n, r) for n, r in rates.items()}
        base = {n: {'kill_rate': 60.0} for n in expect}
        old, note = R.old_trend(old_body)
        kept = R.trend(old, res, run_url, commit, datetime.datetime(2026, 11, day), base)
        rows, problems = R.judge(list(expect), res, base)
        return R.render(rows, problems, res, base, old_body, run_url, commit, datetime.datetime(2026, 11, day),
                        runs=kept, trend_note=note), kept

    def test_the_trend_keeps_each_run_s_rate_and_shows_a_drop(self):
        body, _ = self.runs('', {'a': 70.0, 'b': 50.0}, 'u/1', 7)
        body, _ = self.runs(body, {'a': 64.5, 'b': 50.0}, 'u/2', 14)
        self.assertIn('| a | **64.5%** | -5.5 | 60.0% |', body)   # since the last run
        self.assertIn('| b | **50.0%** | 0 | 60.0% |', body)
        self.assertIn('| 2026-11-14 | `cccccccccc` | 64.5% | 50.0% |', body)
        self.assertLess(body.index('| 2026-11-14 |'), body.index('| 2026-11-07 |'))   # newest first
        self.assertIn('📉 Below the baseline: b 50.0% (baseline 60.0%)', body)
        body, kept = self.runs(body, {'b': 52.0}, 'u/3', 21)   # a has no score this run: '-', and its last rate stays
        self.assertIn('| 2026-11-21 | `cccccccccc` | - | 52.0% |', body)
        self.assertIn('| b | **52.0%** | +2.0 |', body)
        self.assertEqual([r['run'] for r in kept], ['u/1', 'u/2', 'u/3'])
        body, _ = self.runs(body, {'a': 66.0}, 'u/4', 28)   # compared with a's last scored run, not the empty one
        self.assertIn('| a | **66.0%** | +1.5 |', body)

    def test_a_shrunken_module_keeps_no_rate_in_the_trend(self):
        res = {'a': dict(result('a', 95.0), mutants=4), 'b': result('b', 70.0)}
        base = {'a': {'kill_rate': 60.0, 'mutants': 10}, 'b': {'kill_rate': 60.0, 'mutants': 10}}
        rows, problems = R.judge(['a', 'b'], res, base)
        self.assertEqual([s for _, s, _ in rows], ['error', 'improved'])
        kept = R.trend([], res, 'u/1', 'c' * 40, datetime.datetime(2026, 11, 7), base)
        self.assertEqual(kept[-1]['rates'], {'b': 70.0})
        body = R.render(rows, problems, res, base, '', 'u/1', 'c' * 40, datetime.datetime(2026, 11, 7))
        self.assertIn('| 2026-11-07 | `cccccccccc` | - | 70.0% |', body)

    def test_a_rerun_replaces_its_own_entry_and_the_trend_stays_bounded(self):   # runs again
        body, _ = self.runs('', {'a': 70.0}, 'u/1', 7)
        body, kept = self.runs(body, {'a': 71.0}, 'u/1', 7)
        self.assertEqual([(r['run'], r['rates']) for r in kept], [('u/1', {'a': 71.0})])
        self.assertEqual(body.count('| 2026-11-07 |'), 1)
        for i in range(R.TREND_KEEP + 5):
            body, kept = self.runs(body, {'a': float(i)}, f'u/{i + 10}', 1 + i % 28)
        self.assertEqual(len(kept), R.TREND_KEEP)
        self.assertEqual(kept[-1]['run'], f'u/{R.TREND_KEEP + 14}')

    def test_no_or_unreadable_trend_starts_again_and_says_so(self):   # old or empty state
        self.assertEqual(R.old_trend(''), ([], None))
        self.assertEqual(R.old_trend('an old nightly report without a trend'), ([], None))
        for bad in ('<!-- mutation-trend {not json -->', '<!-- mutation-trend [1, 2] -->',
                    '<!-- mutation-trend [{"run": "u"}] -->'):
            runs, note = R.old_trend(bad)
            self.assertEqual(runs, [])
            self.assertIn('could not be read', note)
        body, kept = self.runs('<!-- mutation-trend {not json -->', {'a': 70.0}, 'u/1', 7)
        self.assertIn('⚠️ the trend kept in the last report could not be read', body)
        self.assertEqual(len(kept), 1)
        self.assertIn('| a | **70.0%** | - |', body)   # no earlier rate: no change shown

    def test_main_writes_the_summary_and_the_trend_file_and_keeps_the_issue_s_trend(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = lambda n: os.path.join(d, n)
        for name, data in (('targets.json', {'modules': {'a': {}}}),
                           ('r.json', {'modules': [result('a', 40.0, surv=[3])]}),
                           ('base.json', {'modules': {'a': {'kill_rate': 50.0}}})):
            with open(p(name), 'w') as f:
                json.dump(data, f)
        old, _ = self.runs('', {'a': 45.0}, 'u/old', 7, expect=('a',))
        with open(p('summary.md'), 'w') as f:
            f.write('earlier step\n')   # GitHub's summary file is shared by the job's steps: appended, never replaced
        calls = []

        def run(cmd, stdin=None):
            calls.append((cmd, stdin))
            return (json.dumps([{'number': 4, 'title': R.TITLE, 'state': 'OPEN', 'body': old, 'author': BOT}])
                    if cmd[1:3] == ['issue', 'list'] else '')
        with contextlib.redirect_stdout(io.StringIO()):
            rc = R.main(['--results', p('r.json'), '--baseline', p('base.json'), '--expect', p('targets.json'),
                         '--issue', '--repo', 'o/r', '--run-url', 'u/new', '--summary', p('summary.md'),
                         '--trend', p('trend.json')], run=run)
        self.assertEqual(rc, 0)   # below the baseline: not a failed job
        with open(p('summary.md')) as f:
            summary = f.read()
        self.assertTrue(summary.startswith('earlier step\n'))
        self.assertIn('| a | **40.0%** | -5.0 | 50.0% |', summary)
        self.assertIn('| 2026-11-07 |', summary)
        self.assertNotIn('Where planted bugs survive', summary)
        with open(p('trend.json')) as f:
            self.assertEqual([(r['run'], r['rates']) for r in json.load(f)['runs']],
                             [('u/old', {'a': 45.0}), ('u/new', {'a': 40.0})])
        edit = [c for c in calls if c[0][1:3] == ['issue', 'edit']]
        self.assertEqual(R.old_trend(edit[0][1])[0][-1]['run'], 'u/new')   # the next run reads it back

    def test_body_fits_github(self):
        res = {'a': result('a', 1.0, surv=range(1, 3000))}
        rows, problems = R.judge(['a'], res, {})
        body = R.render(rows, problems, res, {}, '', 'u', 'c' * 40, datetime.datetime(2026, 11, 8))
        self.assertLessEqual(len(body), R.MAX_BODY)
        self.assertIn('more in the run', body)

    def test_main_edits_the_rolling_issue_or_creates_it(self):
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        p = lambda n: os.path.join(d, n)
        for name, data in (('targets.json', {'modules': {'a': {}}}), ('r.json', {'modules': [result('a', 50.0)]}),
                           ('base.json', {'modules': {'a': {'kill_rate': 50.0}}})):
            with open(p(name), 'w') as f:
                json.dump(data, f)
        for issues, verbs in (([{'number': 9, 'title': R.TITLE, 'state': 'OPEN', 'body': '', 'author': BOT},
                                {'number': 4, 'title': R.TITLE, 'state': 'OPEN', 'body': '', 'author': BOT}],
                               [['issue', 'edit', '4'], ['issue', 'close', '9']]), ([], [['issue', 'create']])):
            calls = []

            def run(cmd, stdin=None):
                calls.append((cmd, stdin))
                return json.dumps(issues) if cmd[1:3] == ['issue', 'list'] else ''
            with contextlib.redirect_stdout(io.StringIO()):
                rc = R.main(['--results', p('r.json'), '--baseline', p('base.json'), '--expect', p('targets.json'),
                             '--issue', '--repo', 'o/r', '--suggest', p('s.json')], run=run)
            self.assertEqual(rc, 0)
            writes = [c for c in calls if c[0][1:3] != ['issue', 'list']]
            # green, no red issue open: nothing to close but a second open copy of the report
            self.assertEqual([w[0][1:1 + len(v)] for w, v in zip(writes, verbs)], verbs)
            self.assertEqual(len(writes), len(verbs), calls)
            self.assertIn('✅', writes[0][1])
        with open(p('s.json')) as f:
            self.assertEqual(json.load(f)['modules']['a']['kill_rate'], 50.0)
        calls = []

        def broken(cmd, stdin=None):   # GitHub down: the files and the verdict still appear; no second issue
            calls.append(cmd)
            raise R.subprocess.CalledProcessError(1, cmd, stderr='HTTP 502')
        out = io.StringIO()
        os.remove(p('s.json'))
        with contextlib.redirect_stdout(out):
            rc = R.main(['--results', p('r.json'), '--baseline', p('base.json'), '--expect', p('targets.json'),
                         '--issue', '--repo', 'o/r', '--suggest', p('s.json'), '--out', p('rep.md')], run=broken)
        self.assertEqual(rc, 1)
        self.assertEqual([c[1:3] for c in calls], [['issue', 'list']] * 2)   # rolling issue, alert: never `create`
        self.assertTrue(os.path.exists(p('s.json')) and os.path.exists(p('rep.md')))
        self.assertIn('could not read the rolling issue: HTTP 502', out.getvalue())
        with open(p('rep.md')) as f:   # the one-run trend says why
            self.assertIn('⚠️ the rolling issue could not be read: the trend here shows this run only', f.read())
        self.assertIn('could not open/close the "mutation run red" issue: HTTP 502', out.getvalue())

        def no_gh(cmd, stdin=None):   # gh missing on the runner: same, no traceback
            raise FileNotFoundError(2, 'No such file', 'gh')
        os.remove(p('rep.md'))
        with contextlib.redirect_stdout(io.StringIO()):
            rc = R.main(['--results', p('r.json'), '--baseline', p('base.json'), '--expect', p('targets.json'),
                         '--issue', '--repo', 'o/r', '--out', p('rep.md')], run=no_gh)
        self.assertEqual(rc, 1)
        self.assertTrue(os.path.exists(p('rep.md')))
        os.remove(p('base.json'))
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(R.main(['--results', p('r.json'), '--baseline', p('base.json'),
                                     '--expect', p('targets.json')]), 1)



BOT_LOGIN = 'app/github-actions'   # how gh names the workflow token's account (GITHUB_TOKEN)
BOT = {'login': BOT_LOGIN, 'is_bot': True}


class FakeGitHub:
    """gh with state: issues {number: {title, state, body, author}}; list, create, edit, reopen, close and comment act
    on it. An issue is the bot's (BOT_LOGIN, what the workflow token writes as) unless a fifth item names another
    author; list honours --author and --limit as GitHub's search does (newest first). `fail_on` names one verb that
    fails once (HTTP 502), as a run stopped between two writes would leave it."""
    def __init__(self, issues=(), fail_on=None, me=BOT_LOGIN):
        self.issues = {n: {'title': t, 'state': st, 'body': b, 'author': (rest or [BOT_LOGIN])[0]}
                       for n, t, st, b, *rest in issues}
        self.calls, self.fail_on, self.me = [], fail_on, me

    def __call__(self, cmd, stdin=None):
        self.calls.append(cmd)
        verb = cmd[2]
        if verb == self.fail_on:
            self.fail_on = None
            raise subprocess.CalledProcessError(1, cmd, stderr='HTTP 502')
        arg = lambda flag: cmd[cmd.index(flag) + 1]
        if verb == 'list':
            state, title = arg('--state'), arg('--search').split('"')[1]
            author = arg('--author') if '--author' in cmd else None
            return json.dumps([{'number': n, 'title': i['title'], 'state': i['state'], 'body': i['body'],
                                'author': {'login': i['author'], 'is_bot': i['author'].startswith('app/')}}
                               for n, i in sorted(self.issues.items(), reverse=True)   # search: newest first
                               if title in i['title'] and state in ('all', i['state'].lower())
                               and author in (None, i['author'])][:int(arg('--limit'))])
        if verb == 'create':
            n = max(self.issues, default=0) + 1
            self.issues[n] = {'title': arg('--title'), 'state': 'OPEN', 'body': stdin, 'author': self.me}
            return f'https://github.com/o/r/issues/{n}\n'
        issue = self.issues[int(cmd[3])]
        if verb == 'edit':
            issue['body'] = stdin
        elif verb == 'reopen':
            issue['state'] = 'OPEN'
        elif verb == 'close':
            issue['state'] = 'CLOSED'
        return ''

    def report_runs(self, n):
        return [r['run'] for r in R.old_trend(self.issues[n]['body'])[0]]

    def reports(self, title=R.TITLE):
        return {n: i['state'] for n, i in self.issues.items() if i['title'] == title and i['author'] == self.me}


def trend_body(runs):
    """An earlier report's body: only its trend marker counts for the next run."""
    return 'an earlier report\n<!-- mutation-trend ' + json.dumps(
        [{'run': run, 'date': f'2026-10-{day:02d}', 'commit': 'c' * 10, 'rates': {'a': 50.0}} for run, day in runs]) + ' -->'


class RollingIssue(unittest.TestCase):
    """Which issue a run writes, and that its trend survives a closed issue, two copies and a run stopped half-way."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='mutation-report-test-')
        self.addCleanup(shutil.rmtree, self.d, True)
        for name, data in (('targets.json', {'modules': {'a': {}}}), ('r.json', {'modules': [result('a', 55.0)]}),
                           ('base.json', {'modules': {'a': {'kill_rate': 50.0}}})):
            with open(self.p(name), 'w') as f:
                json.dump(data, f)

    def p(self, name):
        return os.path.join(self.d, name)

    def report(self, gh, run_url, *extra):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = R.main(['--results', self.p('r.json'), '--baseline', self.p('base.json'), '--expect',
                         self.p('targets.json'), '--issue', '--repo', 'o/r', '--run-url', run_url, *extra], run=gh,
                        now=datetime.datetime(2026, 11, 20))
        return rc, out.getvalue()

    def test_a_closed_report_issue_keeps_the_trend(self):
        """Someone closed the rolling issue: is it reopened with its trend, never replaced by a new one?"""
        gh = FakeGitHub([(5, R.TITLE, 'CLOSED', trend_body([('u/1', 1), ('u/2', 8), ('u/3', 15)]))])
        rc, out = self.report(gh, 'u/new')
        self.assertEqual(rc, 0, out)
        self.assertEqual(gh.reports(), {5: 'OPEN'}, 'no second report issue')
        self.assertEqual(gh.report_runs(5), ['u/1', 'u/2', 'u/3', 'u/new'])
        self.assertIn('| 2026-10-01 |', gh.issues[5]['body'])

    def test_two_open_copies_merge_into_the_oldest_and_a_rerun_changes_nothing_more(self):
        """Two report issues open (a search that lagged, an older release): one trend, the oldest kept, the copy
        closed; run again, nothing is closed or created twice and no run is counted twice."""
        gh = FakeGitHub([(3, R.TITLE, 'OPEN', trend_body([('u/1', 1), ('u/2', 8)])),
                         (8, R.TITLE, 'OPEN', trend_body([('u/2', 8), ('u/3', 15)]))])
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.reports(), {3: 'OPEN', 8: 'CLOSED'})
        self.assertEqual(gh.report_runs(3), ['u/1', 'u/2', 'u/3', 'u/new'])
        before = len(gh.calls)
        self.assertEqual(self.report(gh, 'u/new')[0], 0)   # the same run again
        self.assertEqual([c[2] for c in gh.calls[before:] if c[2] != 'list'], ['edit'])
        self.assertEqual(gh.report_runs(3), ['u/1', 'u/2', 'u/3', 'u/new'])

    def test_a_run_stopped_between_creating_a_new_issue_and_closing_the_old_one(self):
        """An older copy closed with a long trend and a newer one open with one run: the open one is written with
        both trends; the closed one stays closed."""
        gh = FakeGitHub([(3, R.TITLE, 'CLOSED', trend_body([(f'u/{i}', i) for i in range(1, 6)])),
                         (9, R.TITLE, 'OPEN', trend_body([('u/6', 20)]))])
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.reports(), {3: 'CLOSED', 9: 'OPEN'})
        self.assertEqual(gh.report_runs(9), ['u/1', 'u/2', 'u/3', 'u/4', 'u/5', 'u/6', 'u/new'])

    def test_a_run_stopped_between_its_edit_and_its_reopen_is_finished_by_the_next(self):
        """The edit landed, the reopen failed: red now, and the next run reopens the same issue, each run once."""
        gh = FakeGitHub([(5, R.TITLE, 'CLOSED', trend_body([('u/1', 1)]))], fail_on='reopen')
        rc, out = self.report(gh, 'u/new')
        self.assertEqual(rc, 1)
        self.assertIn('could not write the rolling issue: HTTP 502', out)
        self.assertEqual((gh.reports(), gh.report_runs(5)), ({5: 'CLOSED'}, ['u/1', 'u/new']))
        self.assertEqual(self.report(gh, 'u/next')[0], 0)
        self.assertEqual((gh.reports(), gh.report_runs(5)), ({5: 'OPEN'}, ['u/1', 'u/new', 'u/next']))

    def test_a_run_stopped_before_closing_a_copy_is_finished_by_the_next(self):
        gh = FakeGitHub([(3, R.TITLE, 'OPEN', trend_body([('u/1', 1)])), (8, R.TITLE, 'OPEN', trend_body([('u/2', 8)]))],
                        fail_on='close')
        self.assertEqual(self.report(gh, 'u/new')[0], 1)
        self.assertEqual(gh.reports(), {3: 'OPEN', 8: 'OPEN'})
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.reports(), {3: 'OPEN', 8: 'CLOSED'})
        self.assertEqual(gh.report_runs(3), ['u/1', 'u/2', 'u/new'])

    def test_an_open_issue_wins_over_a_newer_closed_one(self):
        gh = FakeGitHub([(3, R.TITLE, 'OPEN', trend_body([('u/1', 1)])), (9, R.TITLE, 'CLOSED', trend_body([('u/2', 8)]))])
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.reports(), {3: 'OPEN', 9: 'CLOSED'})
        self.assertEqual(gh.report_runs(3), ['u/1', 'u/2', 'u/new'])
        self.assertNotIn('reopen', [c[2] for c in gh.calls])

    def test_an_unreadable_trend_in_one_copy_keeps_the_others_and_says_so(self):
        gh = FakeGitHub([(3, R.TITLE, 'CLOSED', 'x <!-- mutation-trend {not json -->'),
                         (5, R.TITLE, 'OPEN', trend_body([('u/1', 1)])),
                         (6, 'Mutation testing: report (old)', 'OPEN', trend_body([('u/x', 2)]))])   # another title
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.report_runs(5), ['u/1', 'u/new'])
        self.assertIn('the trend kept in #3 could not be read: its runs are left out', gh.issues[5]['body'])
        self.assertEqual(gh.issues[6]['state'], 'OPEN')

    def test_a_foreign_issue_with_the_report_s_title_is_never_read_or_written(self):
        """In a public repository anyone can open an issue with the report's title. While the real report is open, the
        outsider's trend (runs dated 9999) must not push the real runs out, and the outsider's issue is not closed as
        a copy; while the real one is closed and the outsider's is open, the real one is reopened, the outsider's never
        edited."""
        fake = trend_body([(f'u/fake{i}', 1) for i in range(30)]).replace('2026-10-01', '9999-12-31')
        for real_state in ('OPEN', 'CLOSED'):
            with self.subTest(real_state):
                gh = FakeGitHub([(5, R.TITLE, real_state, trend_body([(f'u/{i}', i) for i in range(1, 11)])),
                                 (7, R.TITLE, 'OPEN', fake, 'someone'), (8, R.TITLE, 'OPEN', fake, 'app/other-bot')])
                rc, out = self.report(gh, 'u/new')
                self.assertEqual(rc, 0, out)
                self.assertEqual(gh.reports(), {5: 'OPEN'})
                self.assertEqual(gh.report_runs(5), [f'u/{i}' for i in range(1, 11)] + ['u/new'])
                self.assertNotIn('9999', gh.issues[5]['body'])
                for n in (7, 8):
                    self.assertEqual((gh.issues[n]['state'], gh.issues[n]['body']), ('OPEN', fake))
                self.assertFalse([c for c in gh.calls if c[2] != 'list' and c[3:4] in (['7'], ['8'])], gh.calls)

    def test_the_report_is_found_among_more_than_a_hundred_issues_with_its_words(self):
        """A search for the title's words that finds more than 100 issues (one page) must still find the report."""
        gh = FakeGitHub([(1, R.TITLE, 'OPEN', trend_body([('u/1', 1)]))]
                        + [(n, f'{R.TITLE} for module {n}', 'CLOSED', '') for n in range(2, 160)])
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        self.assertEqual(gh.reports(), {1: 'OPEN'}, 'no second report issue')
        self.assertEqual(gh.report_runs(1), ['u/1', 'u/new'])

    def test_find_issues_keeps_only_the_writer_s_own_even_when_the_search_returns_others(self):
        """The search asks for the author; the issues it returns are checked again, so a search that ignored it (or an
        issue without an author) cannot bring a foreign trend in."""
        found = [{'number': 3, 'title': R.TITLE, 'state': 'OPEN', 'body': 'x', 'author': {'login': 'someone'}},
                 {'number': 4, 'title': R.TITLE, 'state': 'OPEN', 'body': 'y'},
                 {'number': 5, 'title': R.TITLE, 'state': 'open', 'body': 'z', 'author': {'login': 'github-actions'}},
                 {'number': 6, 'title': R.TITLE, 'state': 'OPEN', 'body': 'w', 'author': BOT}]
        cmds = []
        run = lambda cmd, stdin=None: cmds.append(cmd) or json.dumps(found)
        self.assertEqual([i['number'] for i in R.find_issues(run, 'o/r', R.TITLE, BOT_LOGIN)], [5, 6])
        self.assertEqual(cmds[0][cmds[0].index('--author') + 1], BOT_LOGIN)
        run = lambda cmd, stdin=None: json.dumps([dict(found[3], number=n) for n in range(1000)])
        with self.assertRaisesRegex(ValueError, 'more than'):   # a full page: the report may be past it
            R.find_issues(run, 'o/r', R.TITLE, BOT_LOGIN)

    def test_trend_cells_from_a_broken_body_are_shown_as_plain_text(self):
        """A trend entry whose date or commit is not one (Markdown, a mention, a table bar, a comment end): the table
        shows '?' and the kept trend cannot end its own HTML comment."""
        bad = ('an earlier report\n<!-- mutation-trend ' + json.dumps(   # '>' escaped, as the marker keeps it
            [{'run': 'u/1 --> <b>', 'date': '[x](https://example.invalid) @someone |', 'commit': '`|@ab <b>',
              'rates': {'a': 50.0}}]).replace('>', '\\u003e') + ' -->')
        gh = FakeGitHub([(5, R.TITLE, 'OPEN', bad)])
        self.assertEqual(self.report(gh, 'u/new')[0], 0)
        body = gh.issues[5]['body']
        self.assertIn('| ? | `?` | 50.0% |', body)
        for text in ('@someone', 'example.invalid', '`|@ab', '<b>'):
            self.assertNotIn(text, body)
        self.assertEqual(gh.report_runs(5), ['u/1 --> <b>', 'u/new'])   # the marker still reads back whole

    def test_no_report_issue_yet_creates_one_and_the_next_run_finds_it(self):
        gh = FakeGitHub()
        self.assertEqual(self.report(gh, 'u/1')[0], 0)
        self.assertEqual(self.report(gh, 'u/2')[0], 0)
        self.assertEqual(gh.reports(), {1: 'OPEN'})
        self.assertEqual(gh.report_runs(1), ['u/1', 'u/2'])


class RedIssueAuthor(unittest.TestCase):
    def test_a_foreign_open_red_issue_is_never_commented_on_or_closed(self):
        t = RollingIssue('test_no_report_issue_yet_creates_one_and_the_next_run_finds_it')
        t.setUp()
        self.addCleanup(t.doCleanups)
        gh = FakeGitHub([(3, R.RED_TITLE, 'OPEN', 'not ours', 'someone')])
        self.assertEqual(t.report(gh, 'u/1')[0], 0)   # green: closes only its own red issue
        self.assertEqual(gh.issues[3]['state'], 'OPEN')
        with open(t.p('r.json'), 'w') as f:
            json.dump({'modules': [{'module': 'a', 'error': 'stopped'}]}, f)
        self.assertEqual(t.report(gh, 'u/2')[0], 1)   # red: opens its own, never comments on the foreign one
        self.assertEqual(gh.reports(R.RED_TITLE), {5: 'OPEN'})   # #4 is the report
        self.assertFalse([c for c in gh.calls if c[2] in ('comment', 'close') and c[3] == '3'], gh.calls)


class ConfigTitles(unittest.TestCase):
    """The project's titles and labels come from the config; a config this release cannot read writes no issue."""
    def setUp(self):
        self.t = RollingIssue('test_no_report_issue_yet_creates_one_and_the_next_run_finds_it')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)

    def config(self, data):
        with open(self.t.p('c.json'), 'w') as f:
            f.write(data if isinstance(data, str) else json.dumps(data))
        return ['--config', self.t.p('c.json')]

    def test_titles_and_labels_from_the_config(self):
        gh = FakeGitHub()
        with open(self.t.p('r.json'), 'w') as f:
            json.dump({'modules': [{'module': 'a', 'error': 'stopped'}]}, f)   # red: the red issue too
        cfg = self.config({'about': 'x', 'title': 'Mutants: weekly', 'red_title': 'mutants red',
                           'labels': ['type:test'], 'red_labels': ['prio:high'], 'author': 'app/my-bot'})
        gh.me = 'app/my-bot'   # the token is that app's
        self.assertEqual(self.t.report(gh, 'u/1', *cfg)[0], 1)
        creates = [c for c in gh.calls if c[2] == 'create']
        self.assertEqual([(c[c.index('--title') + 1], [c[i + 1] for i, x in enumerate(c) if x == '--label'])
                          for c in creates], [('Mutants: weekly', ['type:test']), ('mutants red', ['prio:high'])])
        self.assertIn('the issue "Mutants: weekly"', gh.issues[2]['body'])
        self.assertEqual({c[c.index('--author') + 1] for c in gh.calls if c[2] == 'list'}, {'app/my-bot'})

    def test_a_config_this_release_cannot_read_writes_no_issue_but_the_report(self):
        for data in ({'title': 'x', 'runners': 1}, {'title': ''}, {'title': 'a "b"'}, {'labels': 'x'},
                     {'title': 't', 'red_title': 't'}, '{', '[]',
                     {'author': ''}, {'author': 'a b'}, {'author': 'x"'}, {'author': 5}, {'author': '-x'}):
            with self.subTest(data):
                gh = FakeGitHub()
                out = self.t.p('rep.md')
                if os.path.exists(out):
                    os.remove(out)
                rc, log = self.t.report(gh, 'u/1', *self.config(data), '--out', out)
                self.assertEqual(rc, 1)
                self.assertIn('::error::bad config', log)
                self.assertEqual(gh.calls, [], 'no issue is read or written with a config this cannot read')
                self.assertTrue(os.path.exists(out))
        rc, log = self.t.report(FakeGitHub(), 'u/1', '--config', self.t.p('missing.json'))
        self.assertEqual(rc, 1)
        self.assertIn('cannot read', log)


FAKE_GH = r"""#!/usr/bin/env python3
import json, os, sys
state = os.environ['FAKE_GH_STATE']
issues = json.load(open(state))
cmd = sys.argv[1:]
arg = lambda flag: cmd[cmd.index(flag) + 1]
open(state + '.calls', 'a').write(' '.join(cmd[:3]) + '\n')
verb = cmd[1]
if verb == 'list':
    title, st = arg('--search').split('"')[1], arg('--state')
    author = arg('--author') if '--author' in cmd else None
    print(json.dumps([dict(i, number=int(n), author={'login': i.get('author', 'app/github-actions')})
                      for n, i in issues.items() if title in i['title'] and st in ('all', i['state'].lower())
                      and author in (None, i.get('author', 'app/github-actions'))]))
elif verb == 'create':
    issues[str(len(issues) + 1)] = {'title': arg('--title'), 'state': 'OPEN', 'body': sys.stdin.read()}
elif verb == 'edit':
    issues[cmd[2]]['body'] = sys.stdin.read()
elif verb in ('reopen', 'close'):
    issues[cmd[2]]['state'] = 'OPEN' if verb == 'reopen' else 'CLOSED'
json.dump(issues, open(state, 'w'))
"""


class Action(unittest.TestCase):
    """The action's real step, run with bash as the runner runs it, with a fake gh first on PATH."""
    def setUp(self):
        self.t = RollingIssue('test_no_report_issue_yet_creates_one_and_the_next_run_finds_it')
        self.t.setUp()
        self.addCleanup(self.t.doCleanups)
        os.makedirs(self.t.p('bin'))
        with open(self.t.p('bin/gh'), 'w') as f:
            f.write(FAKE_GH)
        os.chmod(self.t.p('bin/gh'), 0o755)

    def run_step(self, issues, **inputs):
        with open(self.t.p('gh.json'), 'w') as f:
            json.dump(issues, f)
        with open(ACTION, encoding='utf-8') as f:
            (step,) = yaml.safe_load(f)['runs']['steps']
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GITHUB_', 'GH_', 'PYTHON'))}
        env.update(PATH=self.t.p('bin') + os.pathsep + env.get('PATH', ''), FAKE_GH_STATE=self.t.p('gh.json'),
                   GITHUB_ACTION_PATH=HERE, GITHUB_REPOSITORY='o/r', GITHUB_STEP_SUMMARY=self.t.p('summary.md'),
                   RUN_URL='https://github.com/o/r/actions/runs/7', GH_TOKEN='t',
                   RESULTS=f'{self.t.p("r.json")}\n{self.t.p("missing-folder")}\n', BASELINE=self.t.p('base.json'),
                   EXPECT=self.t.p('targets.json'), CONFIG='', ISSUE='true', SUGGEST='', OUT='', TREND='')
        env.update(inputs)
        r = subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', step['run']], env=env,
                           capture_output=True, text=True, cwd=self.t.d)
        with open(self.t.p('gh.json')) as f:
            return r, json.load(f)

    def test_the_step_execs_the_script_with_inputs_by_env_only(self):
        with open(ACTION, encoding='utf-8') as f:
            (step,) = yaml.safe_load(f)['runs']['steps']
        self.assertIn('exec python3 -I "$GITHUB_ACTION_PATH/mutation_report.py"', step['run'])
        self.assertNotIn('${{', step['run'])

    def test_a_closed_report_issue_is_reopened_by_the_step(self):
        r, issues = self.run_step({'4': {'title': R.TITLE, 'state': 'CLOSED', 'body': trend_body([('u/1', 1)])},
                                   '5': {'title': R.TITLE, 'state': 'OPEN', 'body': 'x', 'author': 'someone'}},
                                  TREND=self.t.p('trend.json'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(issues['4']['state'], 'OPEN')
        self.assertEqual((issues['5']['state'], issues['5']['body']), ('OPEN', 'x'))
        self.assertEqual(len([i for i in issues.values() if i['title'] == R.TITLE and 'author' not in i]), 1)
        with open(self.t.p('trend.json')) as f:
            self.assertEqual([x['run'] for x in json.load(f)['runs']], ['u/1', 'https://github.com/o/r/actions/runs/7'])
        with open(self.t.p('summary.md')) as f:
            self.assertIn('| a | **55.0%** |', f.read())

    def test_issue_false_calls_no_gh_and_a_bad_value_runs_nothing(self):
        r, _ = self.run_step({}, ISSUE='false', OUT=self.t.p('rep.md'))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(self.t.p('gh.json.calls')))
        self.assertTrue(os.path.exists(self.t.p('rep.md')))
        r, _ = self.run_step({}, ISSUE='yes')
        self.assertEqual(r.returncode, 2)
        self.assertFalse(os.path.exists(self.t.p('gh.json.calls')))


if __name__ == '__main__':
    unittest.main()
