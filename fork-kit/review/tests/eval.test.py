"""The review eval harness on a tiny fixture: a local git repository, a two-case truth set, fake run reports, judge
scores and transcripts. No network (sockets refused). Every command runs again with the same files and output, a
case build stopped half-way is started again, a run map or case that would change under existing results is refused,
and a missing, empty, old or newer config is a plain message. RedCheck breaks each such rule in a copy of
lib/review_eval.py and shows its test then fails.

  python3 -I -B fork-kit/review/tests/eval.test.py
"""
import io, json, os, shutil, socket, subprocess, sys, tempfile, types, unittest
from unittest import mock

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIB = os.path.join(KIT, 'lib', 'review_eval.py')
sys.path.insert(0, os.path.join(KIT, 'lib'))
import review_eval  # noqa: E402

GIT = ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
       '-c', 'init.defaultBranch=main']
PRICE = {'input': 4, 'output': 20, 'cache_write_5m': 5, 'cache_write_1h': 8, 'cache_read': 0.2}


def load(source=None):
    if source is None:
        return review_eval
    mod = types.ModuleType('review_eval_broken')
    mod.__file__ = LIB
    exec(compile(source, LIB, 'exec'), mod.__dict__)
    return mod


def no_network(*_, **__):
    raise AssertionError('the eval opened a network socket')


class Fixture(unittest.TestCase):
    m = review_eval

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='review-eval-test-')
        self.addCleanup(shutil.rmtree, self.dir)
        patch = mock.patch.object(socket, 'socket', no_network)
        patch.start()
        self.addCleanup(patch.stop)
        env = mock.patch.dict(os.environ, {'GIT_CONFIG_NOSYSTEM': '1', 'HOME': self.dir})
        env.start()
        self.addCleanup(env.stop)
        self.repo = os.path.join(self.dir, 'repo')
        os.makedirs(self.repo)
        self.git('init', '-q')
        self.put('repo/src/store.py', 'def save(x):\n    open("db", "w").write(x)\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        self.base = self.head()
        self.put('repo/src/store.py', 'def save(x):\n    open("db", "a").write(x)\n')
        self.put('repo/src/load.py', 'def load():\n    return open("db").read()\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        self.sha = self.head()
        self.truth = {'cases': {
            'A': {'reviewed_sha': self.sha, 'base': self.base,
                  'blocker_major': [{'id': 'A-M1', 'loc': 'src/store.py:2', 'bug': 'append doubles a rerun'},
                                    {'id': 'A-M2', 'loc': 'src/load.py:2', 'bug': 'no file on empty state'}],
                  'minor': [{'id': 'A-m1', 'loc': 'src/load.py:1', 'bug': 'no docstring'}], 'known_other_lens': []},
            'B': {'reviewed_sha': self.sha, 'base': self.base, 'blocker_major': [],
                  'minor': [{'id': 'B-m1', 'loc': 'src/store.py:1', 'bug': 'name'}], 'known_other_lens': []}}}
        self.put('eval/truth.json', json.dumps(self.truth))
        self.put('eval/briefs/A.md', 'Review src/store.py as the lens in lens.md.\n')
        self.cfg = {'version': 1, 'workdir': '../work', 'repo': '../repo', 'truth': 'truth.json', 'briefs': 'briefs',
                    'transcripts': ['../transcripts/*.jsonl'], 'prices': {'model-a': PRICE},
                    'configs': [{'name': 'a-high', 'model': 'model-a', 'effort': 'high'},
                                {'name': 'b-high', 'model': 'model-b', 'effort': 'high'}],
                    'reps': 2, 'seed': 'test'}
        self.config = self.put('eval/eval.json', json.dumps(self.cfg))
        self.work = os.path.join(self.dir, 'work')

    def git(self, *args):
        return subprocess.run([*GIT, '-C', self.repo, *args], check=True, capture_output=True).stdout

    def head(self):
        return self.git('rev-parse', 'HEAD').decode().strip()

    def put(self, rel, text):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def ev(self, *args, config=None):
        out, err = io.StringIO(), io.StringIO()
        code = self.m.run([*args, '--config', config or self.config], out, err)
        return code, out.getvalue(), err.getvalue()

    def ok(self, *args):
        code, out, err = self.ev(*args)
        self.assertEqual((code, err), (0, ''), out)
        return out

    def plain(self, result):
        self.assertEqual(result[0], 2, result)
        self.assertNotIn('Traceback', result[1] + result[2])
        return result[2]

    def tree(self, rel='work'):
        out = {}
        for root, _, names in os.walk(os.path.join(self.dir, rel)):
            for n in names:
                with open(os.path.join(root, n), 'rb') as f:
                    out[os.path.relpath(os.path.join(root, n), self.dir)] = f.read()
        return out

    def runs(self):
        with open(os.path.join(self.work, 'gt', 'runmap.json'), encoding='utf-8') as f:
            return json.load(f)

    def report_all(self, case=None):
        for r in self.runs():
            if case in (None, r['case']):
                self.put(f'work/runs/{r["id"]}.md', f'lens: x · reviewer: RUN-ID {r["id"]} · read at: {self.sha}\n'
                                                    f'finding: major src/store.py:2 append\n')


class Case(Fixture):
    def test_builds_every_case(self):
        out = self.ok('case')
        self.assertIn('A: built, diff', out)
        self.assertIn('B: no brief', out)
        a = os.path.join(self.work, 'cases', 'A')
        with open(os.path.join(a, 'src', 'src', 'store.py')) as f:
            self.assertIn('"a"', f.read())
        with open(os.path.join(a, 'diff.patch')) as f:
            self.assertIn('+    open("db", "a").write(x)', f.read())
        self.assertTrue(os.path.isfile(os.path.join(a, 'diff.stat')))
        self.assertTrue(os.path.isfile(os.path.join(a, 'brief.md')))
        self.assertFalse(os.path.exists(os.path.join(self.work, 'cases', 'B', 'brief.md')))

    def test_runs_again_the_same(self):
        self.ok('case')
        before = self.tree()
        self.assertEqual(self.ok('case'), 'A: already built\nB: already built\n')
        self.assertEqual(self.tree(), before)

    def test_a_build_stopped_half_way_is_started_again(self):
        part = os.path.join(self.work, 'cases', '.A.part')
        os.makedirs(os.path.join(part, 'src'))
        self.put('work/cases/.A.part/src/left-over.txt', 'from a stopped build')
        real = os.replace
        calls = []

        def stop_at_rename(a, b):
            if b == os.path.join(self.work, 'cases', 'A'):
                calls.append(b)
                raise KeyboardInterrupt('stopped before the rename')
            return real(a, b)
        with mock.patch.object(self.m.os, 'replace', stop_at_rename):
            with self.assertRaises(KeyboardInterrupt):
                self.ev('case', 'A')
        self.assertEqual(len(calls), 1)
        self.assertFalse(os.path.exists(os.path.join(self.work, 'cases', 'A')))
        self.assertIn('A: built', self.ok('case', 'A'))
        self.assertFalse(os.path.exists(part))
        self.assertFalse(os.path.exists(os.path.join(self.work, 'cases', 'A', 'src', 'left-over.txt')))

    def test_a_case_built_from_other_commits_is_refused(self):
        self.ok('case', 'A')
        self.truth['cases']['A']['reviewed_sha'] = self.base
        self.put('eval/truth.json', json.dumps(self.truth))
        self.assertIn('was built from other commits', self.plain(self.ev('case', 'A')))

    def test_a_folder_that_is_no_case_folder_is_refused(self):
        os.makedirs(os.path.join(self.work, 'cases', 'A'))
        self.assertIn('was built from other commits (or is not a case folder)', self.plain(self.ev('case', 'A')))

    def test_an_unknown_case_or_commit(self):
        self.assertIn('no case Z', self.plain(self.ev('case', 'Z')))
        self.truth['cases']['A']['reviewed_sha'] = '0' * 40
        self.put('eval/truth.json', json.dumps(self.truth))
        self.assertIn('failed', self.plain(self.ev('case', 'A')))


class Runs(Fixture):
    def test_every_case_config_and_rep_once(self):
        out = self.ok('runs')
        runs = self.runs()
        self.assertEqual(len(runs), 2 * 2 * 2)
        self.assertEqual(len({r['id'] for r in runs}), 8)
        self.assertEqual({(r['case'], r['config'], r['rep']) for r in runs},
                         {(c, x, n) for c in 'AB' for x in ('a-high', 'b-high') for n in (1, 2)})
        self.assertEqual(len(out.splitlines()), 8)

    def test_runs_again_the_same(self):
        first = self.ok('runs')
        before = self.tree()
        self.assertEqual(self.ok('runs'), first)
        self.assertEqual(self.tree(), before)

    def test_the_seed_decides_ids_and_order(self):
        self.ok('runs')
        old = self.runs()
        self.put('eval/eval.json', json.dumps({**self.cfg, 'seed': 'other'}))
        self.ok('runs')   # no reports yet: a new map may replace it
        self.assertNotEqual([r['id'] for r in self.runs()], [r['id'] for r in old])

    def test_a_new_map_under_existing_reports_is_refused(self):
        self.ok('runs')
        self.report_all('A')
        before = self.tree()
        self.put('eval/eval.json', json.dumps({**self.cfg, 'reps': 3}))
        self.assertIn('runs already have reports', self.plain(self.ev('runs')))
        self.assertEqual(self.tree(), before)

    def test_the_prompt_is_filled_in(self):
        self.ok('runs')
        r = self.runs()[0]
        out = self.ok('prompt', r['id'])
        self.assertTrue(out.startswith(f'# model {r["model"]} effort {r["effort"]}\nRUN-ID: {r["id"]}\n'))
        self.assertIn(f'{self.work}/cases/{r["case"]}/brief.md', out)
        for placeholder in ('{id}', '{case}', '{workdir}'):
            self.assertNotIn(placeholder, out)
        self.assertIn('no run r00000000 in the run map', self.plain(self.ev('prompt', 'r00000000')))

    def test_commands_before_the_run_map(self):
        for args in (('prompt', 'r1'), ('judge-prep', 'A'), ('cost',), ('analyze',)):
            with self.subTest(args=args):
                self.assertIn('write it with `runs`', self.plain(self.ev(*args)))


class Judge(Fixture):
    def test_missing_reports_are_named(self):
        self.ok('runs')
        self.report_all('A')
        os.remove(os.path.join(self.work, 'runs', f'{self.runs()[0]["id"]}.md'))
        err = self.plain(self.ev('judge-prep', self.runs()[0]['case']))
        self.assertIn(self.runs()[0]['id'], err)

    def test_neutral_names_and_runs_again_the_same(self):
        self.ok('runs')
        self.report_all()
        self.assertEqual(self.ok('judge-prep', 'A'), f'A: 4 reviews in {self.work}/judge/A\n')
        before = self.tree()
        self.ok('judge-prep', 'A')
        self.assertEqual(self.tree(), before)
        names = sorted(n for n in os.listdir(os.path.join(self.work, 'judge', 'A')) if n.startswith('review-'))
        self.assertEqual(names, ['review-01.md', 'review-02.md', 'review-03.md', 'review-04.md'])
        with open(os.path.join(self.work, 'judge', 'A', 'truth.json')) as f:
            self.assertEqual(json.load(f), self.truth['cases']['A'])
        self.ok('judge-prep', 'B')
        with open(os.path.join(self.work, 'gt', 'judgemap.json')) as f:
            self.assertEqual(sorted(json.load(f)), ['A', 'B'])

    def test_the_judge_prompt(self):
        out = self.ok('judge-prompt', 'A')
        self.assertIn(f'{self.work}/judge/A/truth.json', out)
        self.assertNotIn('{case}', out)
        self.assertIn("no case 'Z'", self.plain(self.ev('judge-prompt', 'Z')))


def transcript(first, entries):
    lines = [json.dumps({'type': 'user', 'message': {'content': first}, 'timestamp': '2026-01-01T00:00:00Z'})]
    lines += [json.dumps(e) for e in entries]
    return '\n'.join(lines) + '\n'


def turn(mid, at, **usage):
    return {'type': 'assistant', 'timestamp': at, 'message': {'id': mid, 'model': 'model-a', 'usage': usage}}


class Cost(Fixture):
    def test_tokens_dollars_and_minutes(self):
        self.ok('runs')
        a = next(r for r in self.runs() if r['model'] == 'model-a')
        b = next(r for r in self.runs() if r['model'] == 'model-b')
        self.put('transcripts/one.jsonl', transcript(f'RUN-ID: {a["id"]}\n\nYou are one reviewer', [
            turn('m1', '2026-01-01T00:01:00Z', input_tokens=1000, output_tokens=100, cache_read_input_tokens=10000,
                 cache_creation={'ephemeral_5m_input_tokens': 2000, 'ephemeral_1h_input_tokens': 0}),
            turn('m1', '2026-01-01T00:01:00Z', input_tokens=1000, output_tokens=100, cache_read_input_tokens=10000,
                 cache_creation={'ephemeral_5m_input_tokens': 2000, 'ephemeral_1h_input_tokens': 0}),   # the same message: once
            turn('m2', '2026-01-01T00:02:00Z', input_tokens=0, output_tokens=900, cache_creation_input_tokens=500),
            turn('m3', '2026-01-01T03:00:00Z', output_tokens=0)]))   # after a long idle gap: not active
        self.put('transcripts/two.jsonl', transcript([{'type': 'text', 'text': f'RUN-ID: {b["id"]}'}], [
            turn('n1', '2026-01-01T00:00:30Z', output_tokens=1)]))
        self.put('transcripts/parent.jsonl', transcript(f'start the runs; RUN-ID: {a["id"]}', [
            turn('p1', '2026-01-01T00:00:30Z', output_tokens=10 ** 6)]))   # mentions a run, is none
        self.put('transcripts/broken.jsonl', '{not json\n')
        out = self.ok('cost')
        self.assertIn('2 runs costed; no transcript: ', out)
        self.assertIn('no price for model-b', out)
        with open(os.path.join(self.work, 'gt', 'cost.json')) as f:
            cost = json.load(f)
        ca = cost[a['id']]
        self.assertEqual((ca['input'], ca['output'], ca['cache_read'], ca['cache_write_5m']), (1000, 1000, 10000, 2500))
        self.assertEqual(ca['usd'], round((1000 * 4 + 1000 * 20 + 2500 * 5 + 10000 * 0.2) / 1e6, 3))
        self.assertEqual(ca['active_min'], 2.0)
        self.assertIsNone(cost[b['id']]['usd'])
        before = self.tree()
        self.assertEqual(self.ok('cost'), out)
        self.assertEqual(self.tree(), before)

    def test_no_transcripts(self):
        self.ok('runs')
        self.assertIn('0 runs costed', self.ok('cost'))


class Analyze(Fixture):
    def scores(self, case, found):
        with open(os.path.join(self.work, 'gt', 'judgemap.json')) as f:
            jm = json.load(f)[case]
        reviews = {review: {'found': found(jm[review]),
                            'findings': [{'n': 1, 'verdict': 'match'}, {'n': 2, 'verdict': 'fp'}]}
                   for review in jm}
        self.put(f'work/judge/{case}/scores.json', json.dumps({'reviews': reviews, 'new_real': []}))

    def test_nothing_judged_yet(self):
        self.ok('runs')
        self.assertEqual(self.ok('analyze'), 'no judged case yet: run judge-prep and the judge first\n')

    def test_recall_worst_pairs_and_the_rule(self):
        self.ok('runs')
        self.report_all()
        self.ok('judge-prep', 'A')
        self.ok('judge-prep', 'B')
        byid = {r['id']: r for r in self.runs()}
        # a-high finds both blocker/majors in one rep and one in the other; b-high finds both every time
        self.scores('A', lambda rid: ['A-M1', 'A-M2', 'A-m1'] if byid[rid]['config'] == 'b-high' or byid[rid]['rep'] == 1
                    else ['A-M1'])
        self.scores('B', lambda rid: ['B-m1'])   # no blocker/major in B: no recall, no crash
        out = self.ok('analyze')
        with open(os.path.join(self.work, 'gt', 'analysis.json')) as f:
            result = json.load(f)
        self.assertEqual(result['judged'], ['A', 'B'])
        self.assertEqual(result['configs']['a-high']['bm_worst'], 0.5)
        self.assertEqual(result['configs']['b-high']['bm_worst'], 1.0)
        self.assertEqual(result['configs']['a-high']['missed'], {'A-M2': 1})
        self.assertEqual(result['pairs']['a-high+a-high']['bm_worst'], 1.0)
        self.assertEqual(result['pick'], 'b-high')
        self.assertIn('rule (the cheapest setting whose worst-case blocker/major recall equals the best): b-high', out)
        before = self.tree()
        self.assertEqual(self.ok('analyze'), out)
        self.assertEqual(self.tree(), before)

    def test_the_cheapest_of_the_best_is_picked(self):
        self.ok('runs')
        self.report_all()
        self.ok('judge-prep', 'A')
        self.scores('A', lambda rid: ['A-M1', 'A-M2'])
        cost = {r['id']: {'usd': 1.0 if r['config'] == 'b-high' else 3.0} for r in self.runs()}
        self.put('work/gt/cost.json', json.dumps(cost))
        self.ok('analyze')
        with open(os.path.join(self.work, 'gt', 'analysis.json')) as f:
            self.assertEqual(json.load(f)['pick'], 'b-high')

    def test_a_case_without_scores_is_named_not_judged(self):
        self.ok('runs')
        self.report_all()
        self.ok('judge-prep', 'A')
        self.ok('judge-prep', 'B')
        self.scores('A', lambda rid: ['A-M1'])
        self.assertIn('not judged: B', self.ok('analyze'))

    def test_scores_that_miss_a_review_are_a_plain_message(self):
        self.ok('runs')
        self.report_all()
        self.ok('judge-prep', 'A')
        self.put('work/judge/A/scores.json', json.dumps({'reviews': {}}))
        self.assertIn('judge the case again', self.plain(self.ev('analyze')))


class Config(Fixture):
    def test_missing_empty_old_newer_and_broken(self):
        cases = {
            'missing': (None, 'does not exist'),
            'empty': ('', 'is empty'),
            'not json': ('{', 'is not JSON'),
            'not an object': ('[]', 'must be a JSON object'),
            'old': (json.dumps({**self.cfg, 'version': 0}), 'this kit reads version 1'),
            'newer': (json.dumps({**self.cfg, 'version': 2}), 'update the kit'),
            'unknown key': (json.dumps({**self.cfg, 'model': 'x'}), "unknown key 'model'"),
            'missing key': (json.dumps({k: v for k, v in self.cfg.items() if k != 'repo'}), "missing key 'repo'"),
            'no configs': (json.dumps({**self.cfg, 'configs': []}), 'configs is empty'),
            'bad config': (json.dumps({**self.cfg, 'configs': [{'name': 'x y', 'model': 'm', 'effort': 'e'}]}),
                           'of plain names'),
            'same names': (json.dumps({**self.cfg, 'configs': [self.cfg['configs'][0]] * 2}), 'share a name'),
            'bad price': (json.dumps({**self.cfg, 'prices': {'m': {'input': 1}}}), 'prices.m must be'),
            'bad reps': (json.dumps({**self.cfg, 'reps': True}), 'reps must be int'),
        }
        for name, (text, message) in cases.items():
            with self.subTest(name):
                path = os.path.join(self.dir, 'eval', f'{name}.json')
                if text is not None:
                    self.put(f'eval/{name}.json', text)
                self.assertIn(message, self.plain(self.ev('runs', config=path)))

    def test_no_config_option(self):
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(self.m.run(['runs'], out, err), 2)
        self.assertIn('give the eval config with --config', err.getvalue())

    def test_a_broken_truth_set(self):
        for text, message in (('', 'is empty'), ('{"cases": {}}', 'at least one case'),
                              ('{"cases": {"A": {"base": "x"}}}', 'case A needs reviewed_sha'),
                              ('{"cases": {"A": {"reviewed_sha": "x", "base": "y", "minor": [1]}}}',
                               'minor must be a list')):
            with self.subTest(text=text):
                self.put('eval/truth.json', text)
                self.assertIn(message, self.plain(self.ev('runs')))

    def test_the_example_config_is_valid(self):
        self.assertEqual(self.m.load_config(os.path.join(KIT, 'eval', 'eval.example.json'))['version'], 1)

    def test_the_command_line(self):
        r = subprocess.run([sys.executable, '-I', '-B', LIB, 'runs', '--config', self.config], capture_output=True,
                           text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(len(r.stdout.splitlines()), 8)
        r = subprocess.run([sys.executable, '-I', '-B', LIB], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn('judge-prep', r.stdout)


# rule: (text in lib/review_eval.py, the text that breaks it, (test class, the test that must then fail))
MUTATIONS = {
    'a stopped build is cleared': ('    if os.path.isdir(tmp):   # a build that stopped part-way: start it again\n'
                                   '        shutil.rmtree(tmp)\n', '',
                                   (Case, 'test_a_build_stopped_half_way_is_started_again')),
    'a built case is kept': ("        print(f'{name}: already built', file=out)\n        return\n",
                             "        print(f'{name}: already built', file=out)\n        shutil.rmtree(final)\n",
                             (Case, 'test_runs_again_the_same')),
    'a case from other commits is refused': ('        if old != stamp:', '        if False:',
                                             (Case, 'test_a_case_built_from_other_commits_is_refused')),
    'a new run map under reports is refused': ('            if have:', '            if False:',
                                               (Runs, 'test_a_new_map_under_existing_reports_is_refused')),
    'the run order is fixed': ("    return sorted(runs, key=lambda r: h(c['seed'], 'order', r['id']))",
                               "    import random\n    random.shuffle(runs)\n    return runs",
                               (Runs, 'test_runs_again_the_same')),
    'only a run\'s own session counts': ('m = RUN_RE.match(_first_user_text(f).lstrip())',
                                         'm = RUN_RE.search(_first_user_text(f))', (Cost, 'test_tokens_dollars_and_minutes')),
    'a message is counted once': ("usage[m.get('id') or e.get('requestId') or len(usage)] = m['usage']",
                                  "usage[len(usage)] = m['usage']", (Cost, 'test_tokens_dollars_and_minutes')),
    'idle gaps are not active': ('if b - a <= gap)', ')', (Cost, 'test_tokens_dollars_and_minutes')),
    'a case without blocker/major has no recall': ("'bm_recall': len(found & bm) / len(bm) if bm else None",
                                                   "'bm_recall': len(found & bm) / len(bm)",
                                                   (Analyze, 'test_recall_worst_pairs_and_the_rule')),
    'the cheapest of the best': ("key=lambda k: (scored[k]['usd_mean'] is None, scored[k]['usd_mean'] or 0, k))",
                                 'key=lambda k: k)', (Analyze, 'test_the_cheapest_of_the_best_is_picked')),
    'a newer config names the update': ("+ (': update the kit' if newer else '')", '',
                                        (Config, 'test_missing_empty_old_newer_and_broken')),
    'unknown config keys are refused': ("problems = [f'unknown key {k!r}' for k in raw if k not in KEYS]",
                                        'problems = []', (Config, 'test_missing_empty_old_newer_and_broken')),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(LIB, encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, (base, test)) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                cls = type('Broken', (base,), {'m': load(source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
