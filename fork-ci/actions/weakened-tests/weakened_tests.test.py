"""weakened_tests.py and its action: the existing tests a pull request deletes, skips or weakens, reported (never a
failure). Every case builds a real git repository with a base and a pull request head; the comment cases run against
a fake `gh` that records each call.
Run: python3 fork-ci/actions/weakened-tests/weakened_tests.test.py
"""
import json, os, re, shutil, stat, subprocess, sys, tempfile, unittest, unittest.mock

import yaml

sys.dont_write_bytecode = True   # the tests import weakened_tests.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import weakened_tests as wt   # noqa: E402

# The runner's GIT_CONFIG_COUNT/KEY/VALUE stay: git's auto maintenance off (.github/scripts/fork-ci-tests.cjs).
for _k in [k for k in os.environ if k.startswith('GIT_')
           and not k.startswith(('GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_'))]:
    del os.environ[_k]
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
os.environ.update(ENV)

CONFIG = '.github/weakened-tests.json'
MARK = wt.MARKER
BOT = 'github-actions[bot]'

PY = '''import pytest


def helper():
    return 1


def test_add():
    assert 1 + 1 == 2
    assert helper() == 1


def test_sub():
    assert 2 - 1 == 1


class TestThing:
    def test_name(self):
        self.assertEqual(name(), "x")
'''

JAVA = '''package demo;

import org.junit.jupiter.api.Test;
import static org.junit.jupiter.api.Assertions.*;

class CalcTest {
    @Test
    void adds() {
        assertEquals(2, Calc.add(1, 1));
    }

    @Test
    void divides() {
        assertThrows(ArithmeticException.class, () -> Calc.div(1, 0));
        assertEquals(2, Calc.div(4, 2));
    }
}
'''

NODE = '''import { describe, expect, it } from "vitest";

describe("calc", () => {
  it("adds", () => {
    expect(add(1, 1)).toBe(2);
  });

  it("subtracts", () => {
    expect(sub(2, 1)).toBe(1);
  });
});
'''

FILES = {'tests/test_calc.py': PY, 'src/test/java/demo/CalcTest.java': JAVA, 'web/calc.test.ts': NODE,
         'src/calc.py': 'def add(a, b):\n    return a + b\n'}


class Repo:
    def __init__(self, case, files=FILES):
        self.d = tempfile.mkdtemp(prefix='weakened-tests-')
        case.addCleanup(shutil.rmtree, self.d, True)
        self.git('init', '-q', '-b', 'main')
        self.base = self.commit(files)

    def git(self, *a):
        r = subprocess.run(['git', *a], cwd=self.d, capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        return r.stdout.strip()

    def commit(self, files=None, delete=(), move=None):
        for old, new in (move or {}).items():
            os.makedirs(os.path.dirname(os.path.join(self.d, new)) or self.d, exist_ok=True)
            self.git('mv', old, new)
        for path in delete:
            self.git('rm', '-q', path)
        for path, text in (files or {}).items():
            full = os.path.join(self.d, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(text if isinstance(text, bytes) else text.encode())
            self.git('add', path)
        self.git('commit', '-q', '--allow-empty', '-m', 'c')
        return self.git('rev-parse', 'HEAD')

    def branch(self, name='pr', start=None):
        self.git('checkout', '-q', '-B', name, start or self.base)

    def edit(self, path, old, new):
        with open(os.path.join(self.d, path)) as f:
            text = f.read()
        assert old in text, old
        return text.replace(old, new, 1)

    def find(self, head, base=None):
        return sorted((i.path, i.name, i.kind) for i in wt.find(base or self.base, head, CONFIG, wt.Git(self.d)).items)


def clock(seconds):
    """git's clock for the commits made inside: a commit's id names its second, so two commits of the same tree a
    second apart are two heads (and a comment that names the head differs)."""
    when = f'@{seconds} +0000'
    return unittest.mock.patch.dict(os.environ, {'GIT_AUTHOR_DATE': when, 'GIT_COMMITTER_DATE': when})


class FakeGh:
    """gh api: the PR (its body), the PR's comments (a list it keeps), POST and PATCH of comments."""

    def __init__(self, body='', comments=None, fail=()):
        self.body, self.comments, self.fail, self.calls = body, list(comments or []), set(fail), []

    def __call__(self, *args, input=None):
        a = list(args)
        self.calls.append((a, input))
        method = a[a.index('-X') + 1] if '-X' in a else 'GET'
        path = next(x for x in a[1:] if x.startswith('repos/'))
        kind = ('pr' if '/pulls/' in path else 'list' if method == 'GET' else method)
        if kind in self.fail:
            return subprocess.CompletedProcess(a, 1, '', 'HTTP 403: Resource not accessible by integration')
        if kind == 'pr':
            return subprocess.CompletedProcess(a, 0, json.dumps({'body': self.body}), '')
        if kind == 'list':
            return subprocess.CompletedProcess(a, 0, ''.join(json.dumps(
                {'id': c['id'], 'login': c['login'], 'body': c['body']}) + '\n' for c in self.comments), '')
        sent = json.loads(input)['body']
        if method == 'POST':
            self.comments.append({'id': 100 + len(self.comments), 'login': BOT, 'body': sent})
        else:
            cid = int(path.rsplit('/', 1)[1])
            next(c for c in self.comments if c['id'] == cid)['body'] = sent
        return subprocess.CompletedProcess(a, 0, '{}', '')

    def writes(self):
        return [(a[a.index('-X') + 1], next(x for x in a if x.startswith('repos/'))) for a, _ in self.calls
                if '-X' in a]

    def ours(self):
        return [c for c in self.comments if c['body'].startswith(MARK)]


class Cli:
    """Runs cli() as the action does and keeps its outputs, summary and log."""

    def __init__(self, case, repo):
        self.case, self.repo = case, repo
        self.files = tempfile.mkdtemp(prefix='weakened-tests-files-')
        case.addCleanup(shutil.rmtree, self.files, True)

    def run(self, head, gh, base=None, extra=()):
        out, summary = os.path.join(self.files, 'out'), os.path.join(self.files, 'summary.md')
        for p in (out, summary):
            open(p, 'w').close()
        log = []
        code = wt.cli(['--config', CONFIG, '--repo', 'o/r', '--pr', '7', '--base', base or self.repo.base,
                       '--head', head, '--path', self.repo.d, *extra],
                      env={'GITHUB_OUTPUT': out, 'GITHUB_STEP_SUMMARY': summary}, gh=gh, out=log.append)
        self.case.assertEqual(code, 0)
        with open(out) as f:
            self.outputs = dict(line.split('=', 1) for line in f.read().splitlines() if '=' in line)
        with open(summary) as f:
            self.summary = f.read()
        self.log = '\n'.join(log)
        return self.outputs


class Deleted(unittest.TestCase):
    def test_a_deleted_python_test_is_listed(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py',
                                                     'def test_sub():\n    assert 2 - 1 == 1\n\n\n', '')})
        self.assertEqual(r.find(head), [('tests/test_calc.py', 'test_sub', 'deleted')])

    def test_a_deleted_test_file_lists_each_of_its_tests(self):
        r = Repo(self)
        r.branch()
        head = r.commit(delete=['src/test/java/demo/CalcTest.java'])
        self.assertEqual(r.find(head), [('src/test/java/demo/CalcTest.java', 'adds', 'deleted'),
                                        ('src/test/java/demo/CalcTest.java', 'divides', 'deleted')])

    def test_a_deleted_node_test_is_listed(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'web/calc.test.ts': r.edit('web/calc.test.ts',
                                                   '  it("subtracts", () => {\n    expect(sub(2, 1)).toBe(1);\n  });\n',
                                                   '')})
        self.assertEqual(r.find(head), [('web/calc.test.ts', 'subtracts', 'deleted')])

    def test_a_commented_out_test_is_deleted(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', 'def test_sub():\n    assert 2 - 1 == 1',
                                                     '# def test_sub():\n#     assert 2 - 1 == 1')})
        self.assertEqual(r.find(head), [('tests/test_calc.py', 'test_sub', 'deleted')])

    def test_a_file_that_is_not_a_test_file_is_not_read(self):
        r = Repo(self)
        r.branch()
        head = r.commit(delete=['src/calc.py'])
        self.assertEqual(r.find(head), [])


class Skipped(unittest.TestCase):
    def test_python_skip_and_xfail(self):
        r = Repo(self)
        r.branch()
        text = r.edit('tests/test_calc.py', 'def test_sub', '@pytest.mark.skip(reason="flaky")\ndef test_sub')
        text = text.replace('    def test_name(self):\n', '    @pytest.mark.xfail\n    def test_name(self):\n')
        head = r.commit({'tests/test_calc.py': text})
        self.assertEqual(r.find(head), [('tests/test_calc.py', 'test_name', 'skipped'),
                                        ('tests/test_calc.py', 'test_sub', 'skipped')])

    def test_a_skip_decorator_belongs_to_the_test_below_it(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', 'def test_sub',
                                                     '@pytest.mark.skipif(True, reason="x")\ndef test_sub')})
        self.assertEqual(r.find(head), [('tests/test_calc.py', 'test_sub', 'skipped')])

    def test_java_disabled_and_assume(self):
        r = Repo(self)
        r.branch()
        text = r.edit('src/test/java/demo/CalcTest.java', '    @Test\n    void adds',
                      '    @Disabled("later")\n    @Test\n    void adds')
        text = text.replace('    void divides() {\n', '    void divides() {\n        assumeTrue(fast());\n')
        head = r.commit({'src/test/java/demo/CalcTest.java': text})
        self.assertEqual(r.find(head), [('src/test/java/demo/CalcTest.java', 'adds', 'skipped'),
                                        ('src/test/java/demo/CalcTest.java', 'divides', 'skipped')])

    def test_node_skip_and_only(self):
        r = Repo(self)
        r.branch()
        text = r.edit('web/calc.test.ts', 'it("adds"', 'it.skip("adds"')
        text = text.replace('it("subtracts"', 'it.only("subtracts"')
        head = r.commit({'web/calc.test.ts': text})
        self.assertEqual(r.find(head), [('web/calc.test.ts', 'adds', 'skipped'),
                                        ('web/calc.test.ts', 'subtracts', 'focused')])

    def test_a_file_level_skip_is_listed_for_the_file(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', 'import pytest\n',
                                                     'import pytest\n\npytestmark = pytest.mark.skip\n')})
        self.assertEqual(r.find(head), [('tests/test_calc.py', wt.FILE, 'skipped')])

    def test_a_skip_that_was_already_there_is_not_listed(self):
        r = Repo(self, {**FILES, 'web/calc.test.ts': NODE.replace('it("adds"', 'it.skip("adds"')})
        r.branch()
        head = r.commit({'web/calc.test.ts': r.edit('web/calc.test.ts', 'it.skip("adds", () => {',
                                                   'it.skip("adds", async () => {')})
        self.assertEqual(r.find(head), [])


class Assertions(unittest.TestCase):
    def test_a_removed_assertion_is_listed(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', '    assert helper() == 1\n', '')})
        self.assertEqual(r.find(head), [('tests/test_calc.py', 'test_add', 'assertion')])

    def test_a_changed_assertion_is_listed_with_its_old_line(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'web/calc.test.ts': r.edit('web/calc.test.ts', 'toBe(2)', 'toBeTruthy()')})
        items = wt.find(r.base, head, CONFIG, wt.Git(r.d)).items
        self.assertEqual([(i.name, i.kind, i.detail) for i in items], [('adds', 'assertion',
                                                                         'expect(add(1, 1)).toBe(2);')])

    def test_java_and_unittest_assertions(self):
        r = Repo(self)
        r.branch()
        text = r.edit('src/test/java/demo/CalcTest.java',
                      '        assertThrows(ArithmeticException.class, () -> Calc.div(1, 0));\n', '')
        py = r.edit('tests/test_calc.py', 'self.assertEqual(name(), "x")', 'self.assertTrue(name())')
        head = r.commit({'src/test/java/demo/CalcTest.java': text, 'tests/test_calc.py': py})
        self.assertEqual(r.find(head), [('src/test/java/demo/CalcTest.java', 'divides', 'assertion'),
                                        ('tests/test_calc.py', 'test_name', 'assertion')])

    def test_reformatting_and_added_assertions_are_not_listed(self):
        r = Repo(self)
        r.branch()
        text = r.edit('tests/test_calc.py', '    assert 1 + 1 == 2\n', '    assert  1 + 1 == 2\n    assert 3 == 3\n')
        head = r.commit({'tests/test_calc.py': text})
        self.assertEqual(r.find(head), [])


class NotListed(unittest.TestCase):
    def test_new_tests_and_new_test_files_are_not_listed(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': PY + '\n\n@pytest.mark.skip\ndef test_new():\n    pass\n',
                         'tests/test_more.py': 'def test_more():\n    assert True\n',
                         'web/new.test.ts': 'it.only("x", () => {});\n'})
        self.assertEqual(r.find(head), [])

    def test_a_renamed_test_file_is_not_deleted(self):
        r = Repo(self)
        r.branch()
        r.commit(move={'tests/test_calc.py': 'tests/unit/test_calc_moved.py',
                       'src/test/java/demo/CalcTest.java': 'src/test/java/demo/CalculatorTest.java'})
        head = r.commit({'src/test/java/demo/CalculatorTest.java': JAVA.replace('class CalcTest', 'class CalculatorTest')})
        self.assertEqual(r.find(head), [])

    def test_a_renamed_file_compares_its_own_lines_too(self):
        r = Repo(self)
        r.branch()
        r.commit(move={'tests/test_calc.py': 'tests/test_renamed.py'})
        head = r.commit({'tests/test_renamed.py': PY.replace('import pytest\n', 'import pytest\npytestmark = pytest.mark.skip\n')})
        self.assertEqual(r.find(head), [('tests/test_renamed.py', wt.FILE, 'skipped')])

    def test_a_renamed_file_still_reports_what_it_weakens(self):
        r = Repo(self)
        r.branch()
        r.commit(move={'tests/test_calc.py': 'tests/test_renamed.py'})
        head = r.commit({'tests/test_renamed.py': PY.replace('    assert helper() == 1\n', '')})
        self.assertEqual(r.find(head), [('tests/test_renamed.py', 'test_add', 'assertion')])

    def test_a_test_moved_to_another_file_is_not_deleted(self):
        r = Repo(self)
        r.branch()
        sub = 'def test_sub():\n    assert 2 - 1 == 1\n\n\n'
        head = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', sub, ''),
                         'tests/test_sub.py': sub})
        self.assertEqual(r.find(head), [])

    def test_only_the_pr_s_own_changes_count(self):
        """A base that moved on (adding an assertion) after the PR branched: the PR does not remove it."""
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_more.py': 'def test_more():\n    assert True\n'})
        r.git('checkout', '-q', 'main')
        base = r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py', '    assert helper() == 1\n',
                                                     '    assert helper() == 1\n    assert helper() > 0\n')})
        self.assertEqual(r.find(head, base), [])


class Parsing(unittest.TestCase):
    def test_java_annotation_arguments_with_parentheses_still_mark_the_test(self):
        java = wt.load_config(None)[0]['java']
        text = ('class T {{\n    @Test\n    {}\n    void adds() {{\n        assertEquals(2, add(1, 1));\n    }}\n\n'
                '    @Test\n    void subs() {{\n        assertEquals(0, sub(1, 1));\n    }}\n}}\n')
        for ann in ('@DisplayName("adds (fast)")', '@CsvSource({"a (x), 1"})', '@EnabledIf("cond()")',
                    '@CsvSource({\n        "a, 1",\n        "b, 2",\n    })', '@Timeout(value = (5))'):
            with self.subTest(ann=ann):
                self.assertEqual([t.name for t in wt.parse(text.format(ann), java)[0]], ['adds', 'subs'])

    def test_a_hostile_test_file_parses_about_as_fast_as_a_plain_one(self):
        """Every line shape that made a test pattern backtrack, timed against a plain test file of a similar size parsed
        in the same process: a busy runner slows both alike, while a backtracking pattern costs seconds to forever on
        these sizes (thousands of times the plain file). Each time is the best of a few runs, so a moment of runner
        load does not count. In a child, so a regression fails instead of hanging (the regex engine cannot be
        interrupted)."""
        child = r"""import json, sys, time
sys.path.insert(0, sys.argv[1])
import weakened_tests as wt
langs = wt.load_config(None)[0]
plain = ('python', 'def test_a(): pass\n' * 100000)
texts = [('node', "it('" + '\\' * 64 + '\n'), ('node', ('it("' + '\\' * 64 + '\n') * 2000),
         ('node', 'it(\n' * 100000), ('java', '@Test(\n' * 100000), ('java', '@Test ' * 20000),
         ('java', '@Test @A("x\n' * 100000), ('java', '@Test @A(' + 'x' * 100000),
         ('node', "it('a', f); " * 100000)]
def best(lang, text):
    times = []
    for _ in range(3):
        t = time.perf_counter()
        wt.parse(text, langs[lang])
        times.append(time.perf_counter() - t)
    return min(times)
print(json.dumps({'plain': best(*plain), 'hostile': [[lang, text[:12], best(lang, text)] for lang, text in texts]}))
"""
        try:
            r = subprocess.run([sys.executable, '-I', '-B', '-c', child, HERE], capture_output=True, text=True,
                               timeout=60)
        except subprocess.TimeoutExpired:
            self.fail('parsing the hostile test file did not finish in 60 s: a test pattern backtracks')
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        for lang, start, took in out['hostile']:
            with self.subTest(lang=lang, start=start):
                self.assertLess(took, 5 * out['plain'], f'{took:.3f} s against {out["plain"]:.3f} s for the plain file')


class Config(unittest.TestCase):
    def test_a_project_adds_patterns_read_from_the_base(self):
        cfg = {'languages': {'java': {'skip': ['@Flaky\\b']}, 'kotlin': {'files': ['**/*Test.kt'],
                                                                         'test': ['@Test\\s+fun\\s+(?P<name>\\w+)']}}}
        kt = 'class KTest {\n    @Test fun works() {\n        check(1)\n    }\n}\n'
        r = Repo(self, {**FILES, CONFIG: json.dumps(cfg), 'src/test/kotlin/KTest.kt': kt})
        r.branch()
        text = r.edit('src/test/java/demo/CalcTest.java', '    @Test\n    void adds', '    @Flaky\n    @Test\n    void adds')
        head = r.commit({'src/test/java/demo/CalcTest.java': text}, delete=['src/test/kotlin/KTest.kt'])
        self.assertEqual(r.find(head), [('src/test/java/demo/CalcTest.java', 'adds', 'skipped'),
                                        ('src/test/kotlin/KTest.kt', 'works', 'deleted')])

    def test_the_pr_s_own_config_is_not_used(self):
        for base_config in (None, {'languages': {'python': {'skip': ['@flaky\\b']}}}):
            with self.subTest(base_config=base_config):
                r = Repo(self, {**FILES, **({CONFIG: json.dumps(base_config)} if base_config else {})})
                r.branch()
                text = r.edit('tests/test_calc.py', '    assert helper() == 1\n', '')
                head = r.commit({CONFIG: json.dumps({'languages': {'python': {'files': ['nothing/**']}}}),
                                 'tests/test_calc.py': text.replace('def test_sub', '@flaky\ndef test_sub')})
                want = [('tests/test_calc.py', 'test_add', 'assertion')]
                if base_config:
                    want.append(('tests/test_calc.py', 'test_sub', 'skipped'))
                self.assertEqual(r.find(head), want)

    def test_a_bad_config_is_could_not_read(self):
        for cfg in ({'langauges': {}}, {'languages': {'go': {'files': ['**/*_test.go']}}},
                    {'languages': {'java': {'skip': ['(']}}}, {'max_bytes': 'big'}, 'not json'):
            with self.subTest(cfg=cfg):
                r = Repo(self, {**FILES, CONFIG: cfg if isinstance(cfg, str) else json.dumps(cfg)})
                r.branch()
                head = r.commit(delete=['tests/test_calc.py'])
                with self.assertRaises(wt.Unclear):
                    r.find(head)

    def test_a_test_pattern_needs_a_name_group(self):
        with self.assertRaises(wt.Unclear):
            wt.load_config(json.dumps({'languages': {'java': {'test': ['@Test']}}}))


class CouldNotRead(unittest.TestCase):
    def test_a_huge_diff_is_could_not_read(self):
        r = Repo(self, {**FILES, CONFIG: json.dumps({'max_bytes': 500})})
        r.branch()
        head = r.commit(delete=['tests/test_calc.py', 'src/test/java/demo/CalcTest.java'])
        with self.assertRaisesRegex(wt.Unclear, 'bytes'):
            r.find(head)

    def test_a_missing_commit_is_could_not_read(self):
        r = Repo(self)
        with self.assertRaises(wt.Unclear):
            r.find('0' * 40)

    def test_no_merge_base_is_could_not_read(self):
        r = Repo(self)
        r.git('checkout', '-q', '--orphan', 'other')
        head = r.commit({'x.txt': 'x'})
        with self.assertRaisesRegex(wt.Unclear, 'merge base'):
            r.find(head)

    def test_a_test_file_that_is_not_utf8_is_listed_as_unreadable(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_calc.py': b'def test_add():\n    assert "\xff" == 1\n'})
        self.assertEqual(r.find(head), [('tests/test_calc.py', wt.FILE, 'unreadable')])

    def test_the_report_says_could_not_read_and_is_posted(self):
        r = Repo(self)
        gh = FakeGh()
        c = Cli(self, r)
        out = c.run('0' * 40, gh)
        self.assertEqual(out['status'], 'could-not-read')
        self.assertEqual(out['count'], '')
        self.assertIn('Could not read', gh.ours()[0]['body'])
        self.assertIn('Could not read', c.summary)
        self.assertNotIn('None of the existing tests', c.summary)

    def test_an_unforeseen_error_is_could_not_read(self):
        """git cannot even start (the folder is gone): an error find() does not foresee is reported, not raised."""
        r = Repo(self)
        gh = FakeGh()
        c = Cli(self, r)
        out = c.run(r.base, gh, extra=('--path', os.path.join(r.d, 'gone')))
        self.assertEqual(out['status'], 'could-not-read')
        self.assertIn('unexpected error', c.summary)
        self.assertIn('Could not read', gh.ours()[0]['body'])


class Refused(unittest.TestCase):
    """git-clean refused the checkout (the action passes --refused): no git runs, status is refused, and the report
    comment of an earlier head is replaced, so it is not read as this head's."""

    def setUp(self):
        self.repo = Repo(self)   # one repository, so one head, for all of a test's runs (#452)

    def run_refused(self, gh, head=None):
        c = Cli(self, self.repo)
        with unittest.mock.patch.object(wt, 'Git', side_effect=AssertionError('git ran')):
            out = c.run(head or self.repo.base, gh, extra=('--refused',))
        return c, out

    def test_status_refused_and_the_old_report_is_replaced(self):
        gh = FakeGh(comments=[{'id': 3, 'login': BOT, 'body': MARK + '\nold list of an earlier head'}])
        c, out = self.run_refused(gh)
        self.assertEqual((out['status'], out['count'], out['missing_why']), ('refused', '', ''))
        self.assertEqual(gh.writes(), [('PATCH', 'repos/o/r/issues/comments/3')])
        self.assertIn('git-clean refused', gh.ours()[0]['body'])
        self.assertNotIn('old list', gh.ours()[0]['body'])
        self.assertIn('git-clean refused', c.summary)

    def test_a_refusal_on_the_head_of_the_earlier_report_does_not_call_it_another_head(self):   # a re-run
        gh = FakeGh(comments=[{'id': 3, 'login': BOT, 'body': MARK + f'\nHead `{self.repo.base[:12]}`: a list'}])
        self.run_refused(gh, self.repo.base)
        body = gh.ours()[0]['body']
        self.assertIn('git-clean refused', body)
        self.assertNotIn('another head', body)
        self.assertIn('replaces any earlier report', body)

    def test_no_comment_yet_posts_none_and_again_edits_nothing(self):
        gh = FakeGh()
        self.run_refused(gh)
        self.assertEqual(gh.writes(), [])   # nothing stale to replace
        gh = FakeGh(comments=[{'id': 3, 'login': BOT, 'body': MARK + '\nold'}])
        with clock(1_700_000_000):
            self.run_refused(gh)
        with clock(1_700_000_001):   # the second run starts a second later: the same head, so the same text
            self.run_refused(gh)   # runs again: no second edit
        self.assertEqual(gh.writes(), [('PATCH', 'repos/o/r/issues/comments/3')])

    def test_a_new_head_edits_the_note_again(self):
        """The note names its head: a push (a commit a second later, even of the same tree) is a new head, and the
        note is edited to name it; this second edit is wanted, not the #452 flake."""
        gh = FakeGh(comments=[{'id': 3, 'login': BOT, 'body': MARK + '\nold'}])
        with clock(1_700_000_000):
            first = self.repo.commit()
        with clock(1_700_000_001):
            second = self.repo.commit()
        self.run_refused(gh, first)
        self.run_refused(gh, first)
        self.run_refused(gh, second)
        self.assertEqual(gh.writes(), [('PATCH', 'repos/o/r/issues/comments/3')] * 2)
        self.assertIn(second[:12], gh.ours()[0]['body'])
        self.assertNotIn(first[:12], gh.ours()[0]['body'])


class Comment(unittest.TestCase):
    def weakened(self, r):
        r.branch()
        return r.commit({'tests/test_calc.py': r.edit('tests/test_calc.py',
                                                     'def test_sub():\n    assert 2 - 1 == 1\n\n\n', '')})

    def test_a_rerun_gives_the_same_list_and_does_not_post_again(self):
        r = Repo(self)
        head = self.weakened(r)
        gh, c = FakeGh(), Cli(self, r)
        first = c.run(head, gh)
        body = gh.ours()[0]['body']
        second = c.run(head, gh)
        self.assertEqual(first, second)
        self.assertEqual(len(gh.ours()), 1)
        self.assertEqual(gh.ours()[0]['body'], body)
        self.assertEqual([m for m, _ in gh.writes()], ['POST'])   # the re-run wrote nothing
        self.assertEqual(first['count'], '1')

    def test_a_force_push_recomputes_and_edits_the_same_comment(self):
        r = Repo(self)
        head = self.weakened(r)
        gh, c = FakeGh(), Cli(self, r)
        c.run(head, gh)
        cid = gh.ours()[0]['id']
        r.branch('pr2')
        fixed = r.commit({'tests/test_more.py': 'def test_more():\n    assert True\n'})
        out = c.run(fixed, gh)
        self.assertEqual(out['count'], '0')
        self.assertEqual(gh.writes(), [('POST', 'repos/o/r/issues/7/comments'),
                                       ('PATCH', f'repos/o/r/issues/comments/{cid}')])
        self.assertEqual(len(gh.ours()), 1)
        self.assertIn('None of the existing tests', gh.ours()[0]['body'])

    def test_nothing_found_and_no_comment_yet_posts_nothing(self):
        r = Repo(self)
        r.branch()
        head = r.commit({'tests/test_more.py': 'def test_more():\n    assert True\n'})
        gh, c = FakeGh(), Cli(self, r)
        out = c.run(head, gh)
        self.assertEqual(out['count'], '0')
        self.assertEqual(gh.writes(), [])
        self.assertIn('None of the existing tests', c.summary)

    def test_only_its_own_marked_comment_is_edited(self):
        r = Repo(self)
        head = self.weakened(r)
        gh = FakeGh(comments=[{'id': 1, 'login': 'someone', 'body': MARK + '\nfake'},
                              {'id': 2, 'login': BOT, 'body': 'an unrelated bot comment'},
                              {'id': 3, 'login': BOT, 'body': MARK + '\nold list'}])
        Cli(self, r).run(head, gh)
        self.assertEqual(gh.writes(), [('PATCH', 'repos/o/r/issues/comments/3')])
        self.assertEqual(gh.comments[0]['body'], MARK + '\nfake')

    def test_why_lines_in_the_pr_body(self):
        r = Repo(self)
        r.branch()
        text = r.edit('tests/test_calc.py', 'def test_sub():\n    assert 2 - 1 == 1\n\n\n', '')
        head = r.commit({'tests/test_calc.py': text.replace('    assert helper() == 1\n', '')})
        gh, c = FakeGh(body='Some change.\n- test_sub: the subtraction is gone, so is its test\n'), Cli(self, r)
        out = c.run(head, gh)
        self.assertEqual((out['count'], out['missing_why']), ('2', '1'))
        body = gh.ours()[0]['body']
        self.assertRegex(body, r'`test_sub`[^\n]*\| yes \|')
        self.assertRegex(body, r'`test_add`[^\n]*\| no \|')
        self.assertIn('Without a why', body)

    def test_a_body_edit_that_adds_the_why_updates_the_comment(self):
        r = Repo(self)
        head = self.weakened(r)
        gh, c = FakeGh(), Cli(self, r)
        self.assertEqual(c.run(head, gh)['missing_why'], '1')
        gh.body = 'test_sub: gone with the feature'
        self.assertEqual(c.run(head, gh)['missing_why'], '0')
        self.assertEqual([m for m, _ in gh.writes()], ['POST', 'PATCH'])

    def test_it_never_fails_the_pr(self):
        r = Repo(self)
        head = self.weakened(r)
        for fail in (('list',), ('POST',), ('pr',), ('pr', 'list', 'POST')):
            with self.subTest(fail=fail):
                c = Cli(self, r)
                out = c.run(head, FakeGh(fail=fail))
                self.assertEqual(out['count'], '1')
                self.assertIn('::warning::', c.log)
                self.assertIn('test_sub', c.summary)

    def test_an_unexpected_error_still_exits_0(self):
        r = Repo(self)
        head = self.weakened(r)

        def boom(*a, input=None):
            raise RuntimeError('boom')
        c = Cli(self, r)
        out = c.run(head, boom)
        self.assertIn('::warning::', c.log)
        self.assertEqual(out['count'], '1')

    def test_names_cannot_break_out_of_the_markdown_or_mention(self):
        node = 'it("pings @team `x` | <b>y</b>", () => {\n  expect(1).toBe(1);\n});\n'
        r = Repo(self, {**FILES, 'web/odd.test.ts': node})
        r.branch()
        head = r.commit(delete=['web/odd.test.ts'])
        gh = FakeGh()
        Cli(self, r).run(head, gh)
        body = gh.ours()[0]['body']
        row = next(line for line in body.splitlines() if 'pings' in line)
        self.assertNotIn('<b>', row)
        self.assertEqual(row.count('|'), 5, row)   # four cells, none split by the name
        self.assertRegex(row, r'`[^`]*@team[^`]*`')   # the @ is inside a code span: no mention

    def test_a_long_list_is_cut_to_fit_a_comment(self):
        many = ''.join(f'def test_{i}():\n    assert {i}\n\n\n' for i in range(3000))
        r = Repo(self, {**FILES, 'tests/test_many.py': many})
        r.branch()
        head = r.commit(delete=['tests/test_many.py'])
        gh = FakeGh()
        out = Cli(self, r).run(head, gh)
        self.assertEqual(out['count'], '3000')
        body = gh.ours()[0]['body']
        self.assertLess(len(body), 65536)
        self.assertIn('more', body)


    def test_a_report_over_max_text_is_cut(self):
        """MAX_ROWS rows of the widest cells is over MAX_TEXT: the report is cut at a line, under the limit."""
        wide = 'x' * 500
        items = [wt.Item(f'{i}{wide}', f'{i}{wide}', 'assertion', wide) for i in range(wt.MAX_ROWS)]
        text = wt.render(items, '', [False] * len(items), '0' * 40, '1' * 40)
        self.assertLessEqual(len(text), wt.MAX_TEXT + 40)
        self.assertTrue(text.endswith('… cut to fit a comment.\n'), text[-80:])


class Guards(unittest.TestCase):
    def test_code_keeps_html_and_backticks_out_of_the_span(self):
        for name in ('a <b>x</b> c', 'a ` b', 'a`` @team ``b'):
            with self.subTest(name=name):
                cell = wt.code(name)
                self.assertNotIn('<', cell)
                self.assertNotIn('>', cell)
                self.assertEqual(cell.count('`'), 2, cell)
                self.assertTrue(cell.startswith('`') and cell.endswith('`'))

    def test_code_caps_a_cell(self):
        cell = wt.code('y' * 1000)
        self.assertEqual(len(cell), wt.CELL + 2)
        self.assertTrue(cell.endswith('…`'))

    def test_the_report_reads_no_clock_and_no_chance(self):
        """A re-run of the same head must write the same text, or it edits the live comment each time (#452): the
        script imports nothing that tells the time or picks at random."""
        import ast
        with open(os.path.join(HERE, 'weakened_tests.py')) as f:
            tree = ast.parse(f.read())
        names = {a.name.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
        names |= {n.module.split('.')[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
        self.assertFalse(names & {'time', 'datetime', 'calendar', 'random', 'secrets', 'uuid'}, names)
        self.assertNotIn('__import__', {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)})

    def test_escape_keeps_a_name_on_one_log_line(self):
        self.assertEqual(wt.escape('tests/test_x\n::error::y\r%.py'), 'tests/test_x%0A::error::y%0D%25.py')


class Action(unittest.TestCase):
    """The action's real run text, with bash, as the runner runs it; gh is a fake on PATH."""

    def test_the_step_runs_the_script_and_never_fails(self):
        with open(os.path.join(HERE, 'action.yml')) as f:
            action = yaml.safe_load(f)
        step = action['runs']['steps'][0]
        r = Repo(self)
        r.branch()
        head = r.commit(delete=['web/calc.test.ts'])
        files = tempfile.mkdtemp(prefix='weakened-tests-bin-')
        self.addCleanup(shutil.rmtree, files, True)
        gh = os.path.join(files, 'gh')
        with open(gh, 'w') as f:
            f.write('#!/bin/sh\necho "$*" >> "$GH_LOG"\ncase "$*" in *pulls*) echo \'{"body": ""}\';; esac\n')
        os.chmod(gh, os.stat(gh).st_mode | stat.S_IEXEC)
        given = {'config': CONFIG, 'repo': 'o/r', 'pr': '7', 'base': r.base, 'head': head, 'path': '.',
                 'token': 'x', 'comment': 'true', 'comment-author': BOT}
        values = {k: given[re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', v)[1]] for k, v in step['env'].items()}
        out = os.path.join(files, 'out')
        open(out, 'w').close()
        for broken in (False, True):
            env = {**os.environ, **values, 'GITHUB_OUTPUT': out, 'GITHUB_ACTION_PATH': HERE, 'GITHUB_WORKSPACE': r.d,
                   'GH_LOG': os.path.join(files, 'gh.log'), 'PATH': files + os.pathsep + os.environ['PATH']}
            if broken:
                env['GITHUB_ACTION_PATH'] = files   # no script there: the step still passes, with a warning
            p = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']],
                               text=True, capture_output=True, env=env, cwd=r.d)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        with open(out) as f:
            self.assertIn('count=2', f.read())
        with open(os.path.join(files, 'gh.log')) as f:
            self.assertIn('repos/o/r/issues/7/comments', f.read())
        self.assertIn('::warning::', p.stdout)

    def test_a_git_config_checkout_never_writes_means_no_report_and_no_git(self):   # fork-ci git-clean
        with open(os.path.join(HERE, 'action.yml')) as f:
            step = yaml.safe_load(f)['runs']['steps'][0]
        r = Repo(self)
        r.branch()
        head = r.commit(delete=['web/calc.test.ts'])
        files = tempfile.mkdtemp(prefix='weakened-tests-bin-')
        self.addCleanup(shutil.rmtree, files, True)
        marker = os.path.join(files, 'ran')
        r.git('config', 'core.fsmonitor', f'touch {marker}; false')
        gh = os.path.join(files, 'gh')   # the comment list holds the report of an earlier head
        with open(gh, 'w') as f:
            f.write('#!/bin/sh\necho "$*" >> "$GH_LOG"\ncase "$*" in *--paginate*) printf \'%s\\n\' '
                    f"'{json.dumps({'id': 3, 'login': BOT, 'body': MARK + ' old'})}';; *PATCH*) cat >> \"$GH_LOG\";; esac\n")
        os.chmod(gh, 0o755)
        given = {'config': CONFIG, 'repo': 'o/r', 'pr': '7', 'base': r.base, 'head': head, 'path': '.',
                 'token': 'x', 'comment': 'true', 'comment-author': BOT}
        values = {k: given[re.fullmatch(r'\$\{\{ inputs\.([\w-]+) \}\}', v)[1]] for k, v in step['env'].items()}
        out = os.path.join(files, 'out')
        open(out, 'w').close()
        p = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail', '-c', step['run']], text=True,
                           capture_output=True, cwd=r.d,
                           env={**os.environ, **values, 'GITHUB_OUTPUT': out, 'GITHUB_ACTION_PATH': HERE,
                                'GITHUB_WORKSPACE': r.d, 'GH_LOG': os.path.join(files, 'gh.log'),
                                'PATH': files + os.pathsep + os.environ['PATH']})
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn('::error::git-clean: ', p.stdout)
        self.assertIn('::warning::weakened-tests: no report', p.stdout)
        self.assertFalse(os.path.exists(marker))
        with open(out) as f:
            self.assertEqual(f.read(), 'status=refused\ncount=\nmissing_why=\n')
        with open(os.path.join(files, 'gh.log')) as f:
            log = f.read()
        self.assertIn('PATCH repos/o/r/issues/comments/3', log)   # the earlier head's report is replaced
        self.assertIn('git-clean refused', log)

    def test_the_example_caller_runs_it_one_at_a_time_with_full_history(self):
        with open(os.path.join(HERE, 'weakened-tests-caller.example.yml')) as f:
            caller = yaml.safe_load(f)
        self.assertEqual(caller[True]['pull_request']['types'], ['opened', 'synchronize', 'reopened', 'edited'])
        self.assertEqual(caller['concurrency']['group'], 'weakened-tests-${{ github.event.pull_request.number }}')
        self.assertIs(caller['concurrency']['cancel-in-progress'], False)
        job = caller['jobs']['report']
        self.assertEqual(job['permissions'], {'contents': 'read', 'pull-requests': 'write'})
        checkout, step = job['steps']
        self.assertEqual(checkout['with']['fetch-depth'], 0)
        self.assertTrue(step['uses'].startswith('scuttletoby/t3code/fork-ci/actions/weakened-tests@'))
        self.assertNotIn('continue-on-error', job)   # the step never fails: nothing to hide


if __name__ == '__main__':
    unittest.main()
