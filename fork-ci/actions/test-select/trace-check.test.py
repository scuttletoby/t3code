"""trace_check.py: every tracked file a test touches under strace must pick that test when it alone changes.
Run: python3 -B fork-ci/actions/test-select/trace-check.test.py (the end-to-end tests skip without strace).
"""
import contextlib, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location('trace_check_api', os.path.join(HERE, 'trace_check.py'))
TC = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TC)
ACTION = os.path.join(HERE, 'trace', 'action.yml')
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}


def log(text):
    return io.StringIO(textwrap.dedent(text).lstrip('\n'))


class Touched(unittest.TestCase):
    """The strace log parser: only calls that succeeded, each path resolved the way the kernel did."""

    def test_open_relative_to_the_start_folder(self):
        got = TC.touched(log('''
            100 openat(AT_FDCWD, "a/b.txt", O_RDONLY|O_CLOEXEC) = 3</r/a/b.txt>
        '''), '/r')
        self.assertEqual(got, {'/r/a/b.txt': 'reads'})

    def test_failed_calls_count_not(self):
        got = TC.touched(log('''
            100 openat(AT_FDCWD, "gone.txt", O_RDONLY) = -1 ENOENT (No such file or directory)
            100 newfstatat(AT_FDCWD, "also.txt", 0x7ffd, 0) = -1 ENOENT (No such file or directory)
        '''), '/r')
        self.assertEqual(got, {})

    def test_chdir_moves_the_base_of_later_paths(self):
        got = TC.touched(log('''
            100 chdir("data") = 0
            100 openat(AT_FDCWD, "x.txt", O_RDONLY) = 3
        '''), '/r')
        self.assertEqual(got, {'/r/data/x.txt': 'reads'})

    def test_fchdir_by_its_folder(self):
        got = TC.touched(log('''
            100 fchdir(4</r/deep/er>) = 0
            100 openat(AT_FDCWD, "x.txt", O_RDONLY) = 3
        '''), '/r')
        self.assertEqual(got, {'/r/deep/er/x.txt': 'reads'})

    def test_a_child_starts_in_its_parents_folder(self):   # and a later chdir of the parent does not move it
        got = TC.touched(log('''
            100 chdir("sub") = 0
            100 clone(child_stack=NULL, flags=CLONE_CHILD_SETTID|SIGCHLD) = 101
            100 chdir("/elsewhere") = 0
            101 execve("./tool.sh", ["./tool.sh"], 0x7ffd /* 3 vars */) = 0
        '''), '/r')
        self.assertEqual(got['/r/sub/tool.sh'], 'runs')

    def test_unfinished_and_resumed_lines_join(self):
        got = TC.touched(log('''
            100 openat(AT_FDCWD, "slow.txt", O_RDONLY <unfinished ...>
            101 openat(AT_FDCWD, "other.txt", O_RDONLY) = 3
            100 <... openat resumed>) = 4
        '''), '/r')
        self.assertEqual(sorted(got), ['/r/other.txt', '/r/slow.txt'])

    def test_a_folder_descriptor_is_the_base(self):
        got = TC.touched(log('''
            100 openat(5</r/pkg>, "mod.py", O_RDONLY) = 3
        '''), '/r')
        self.assertEqual(got, {'/r/pkg/mod.py': 'reads'})

    def test_escaped_names(self):
        got = TC.touched(log(r'''
            100 openat(AT_FDCWD, "caf\303\251 \"q\".txt", O_RDONLY) = 3
        '''), '/r')
        self.assertEqual(got, {'/r/café "q".txt': 'reads'})

    def test_the_opened_file_as_resolved(self):   # -y: through a link, the target is touched too
        got = TC.touched(log('''
            100 openat(AT_FDCWD, "link.txt", O_RDONLY) = 3</r/real.txt>
        '''), '/r')
        self.assertEqual(got, {'/r/link.txt': 'reads', '/r/real.txt': 'reads'})

    def test_stat_checks_and_a_read_wins(self):
        got = TC.touched(log('''
            100 newfstatat(AT_FDCWD, "a.txt", {st_mode=S_IFREG|0644, st_size=1, ...}, 0) = 0
            100 access("b.txt", R_OK) = 0
            100 openat(AT_FDCWD, "a.txt", O_RDONLY) = 3
            100 newfstatat(AT_FDCWD, "a.txt", {st_mode=S_IFREG|0644, st_size=1, ...}, 0) = 0
        '''), '/r')
        self.assertEqual(got, {'/r/a.txt': 'reads', '/r/b.txt': 'checks'})


class Repo(unittest.TestCase):
    def setUp(self):
        self.d = os.path.realpath(tempfile.mkdtemp(prefix='trace-check-'))
        self.addCleanup(shutil.rmtree, self.d, True)

    def write(self, files):
        for path, text in files.items():
            full = os.path.join(self.d, path)
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write(text)

    def commit(self):
        for a in (['init', '-q', '-b', 'main'], ['add', '-A'], ['commit', '-qm', 'x']):
            subprocess.run(['git', *a], cwd=self.d, check=True, capture_output=True, env={**os.environ, **ENV})

    def check(self, config, **kw):
        self.write({'.github/test-select.json': json.dumps(config)})
        self.commit()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = TC.main(['--root', self.d, '--jobs', '1', *kw.get('args', [])])
        return rc, out.getvalue()


HIDDEN = '''\
import json, os, subprocess, unittest
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
name = json.load(open(os.path.join(ROOT, 'data', 'index.json')))['file']   # a name from data
os.chdir(os.path.join(ROOT, 'data'))
subprocess.run(['cat', name], check=True, stdout=subprocess.DEVNULL)    # read by a child, from another folder
'''


class Picks(Repo):
    """picks(): the same decision test-select makes for a pull request that changes one file."""

    def setUp(self):
        super().setUp()
        self.write({'tests/test_a.py': 'import os\nopen(os.path.join(os.path.dirname(__file__), "in.txt"))\n',
                    'tests/in.txt': '', 'tests/test_b.py': 'import subprocess\nsubprocess.run(["git", "ls-files"])\n',
                    'other.txt': '', 'tests/_help.py': '', 'tools/alone.py': '',
                    'tests/test_c.py': 'import sys\nsys.path.insert(0, sys.argv[0])\nimport alone\n'})
        self.commit()
        files = set(subprocess.run(['git', 'ls-files'], cwd=self.d, capture_output=True, text=True).stdout.split())
        self.graph = TC.ts.Graph(TC.ts.Repo(self.d, files), [])

    def picks(self, test, rel, always_run=(), rules=()):
        return TC.picks('c.json', list(rules), dict.fromkeys(always_run, 'why'), self.graph, test, rel)

    def test_an_input(self):
        self.assertEqual(self.picks('tests/test_a.py', 'tests/in.txt'), 'takes it in')
        self.assertIsNone(self.picks('tests/test_a.py', 'other.txt'))

    def test_always_all(self):
        self.assertEqual(self.picks('tests/test_a.py', 'tests/_help.py', rules=[('tests/_*.py', 'helper')]),
                         'every test (helper)')

    def test_always_run(self):
        self.assertEqual(self.picks('tests/test_a.py', 'other.txt', always_run=['tests/test_a.py']), 'always_run')

    def test_a_test_test_select_always_picks(self):
        self.assertEqual(self.picks('tests/test_b.py', 'other.txt'), 'always')

    def test_a_test_whose_inputs_run_every_test(self):   # reported, never a crash of the whole check
        self.assertRegex(self.picks('tests/test_c.py', 'other.txt'), r'^every test \(.*import alone')


@unittest.skipUnless(shutil.which('strace'), 'strace is not installed here (the trace tests need it)')
class EndToEnd(Repo):
    """A real strace run of real tests."""

    def setUp(self):
        super().setUp()
        self.write({'tests/test_hidden.py': HIDDEN, 'data/index.json': '{"file": "hidden.txt"}',
                    'data/hidden.txt': 'x\n',
                    'tests/test_seen.py': 'import os\nopen(os.path.join(os.path.dirname(__file__), "seen.txt"))\n',
                    'tests/seen.txt': ''})

    def test_a_file_read_by_a_name_from_data_is_a_miss(self):
        rc, out = self.check({'tests': ['tests/test_*.py']})
        self.assertEqual(rc, 1, out)
        self.assertIn('miss: tests/test_hidden.py reads data/hidden.txt;', out)
        self.assertNotIn('test_seen.py reads', out)   # its input is named in its code: picked, no miss
        self.assertNotIn('data/index.json', out.split('miss:', 1)[1])   # named in its code: picked
        self.assertIn('trace-check: 2 tests, 1 misses, 0 not traced', out)
        rc, out = self.check({'tests': ['tests/test_*.py'], 'inputs': [   # a declaration that misses it
            {'test': 'tests/test_hidden.py', 'reads': ['data/*.md'], 'why': 'wrong'}]})
        self.assertEqual(rc, 1, out)
        self.assertIn('miss: tests/test_hidden.py reads data/hidden.txt;', out)

    def test_always_run_covers_it(self):
        rc, out = self.check({'tests': ['tests/test_*.py'],
                              'always_run': [{'glob': 'tests/test_hidden.py', 'why': 'reads names from data'}]})
        self.assertEqual(rc, 0, out)
        self.assertIn('trace-check: 2 tests, 0 misses, 0 not traced', out)

    def test_only(self):
        rc, out = self.check({'tests': ['tests/test_*.py']}, args=['--only', 'tests/test_seen.py'])
        self.assertEqual(rc, 0, out)
        self.assertIn('trace-check: 1 tests, 0 misses', out)

    def test_a_failing_test_is_not_checked(self):   # what it never reached, it never touched: never clean
        for code in ('import os\nopen(os.path.join(os.path.dirname(__file__), "seen.txt"))\nraise SystemExit(3)\n',
                     'import missing_module_xyz\nopen("data/hidden.txt")\n'):
            with self.subTest(code):
                self.write({'tests/test_seen.py': code})
                rc, out = self.check({'tests': ['tests/test_seen.py']})
                self.assertEqual(rc, 1, out)
                self.assertIn('tests/test_seen.py: not checked: the test failed under the trace (exit ', out)
                self.assertIn('trace-check: 1 tests, 0 misses, 1 not traced or not checked', out)

    def test_a_skipped_test_is_not_checked(self):   # unittest's skips: a runner without what the test needs
        self.write({'tests/test_seen.py': 'import unittest\nclass T(unittest.TestCase):\n'
                    '    @unittest.skip("no gh here")\n    def test_x(self):\n        open("data/hidden.txt")\n'
                    'unittest.main()\n'})
        rc, out = self.check({'tests': ['tests/test_seen.py']})
        self.assertEqual(rc, 1, out)
        self.assertIn('tests/test_seen.py: not checked: 1 skipped under the trace', out)

    def test_only_that_matches_no_test_fails(self):   # a check of nothing never passes
        rc, out = self.check({'tests': ['tests/test_*.py']}, args=['--only', 'nope/*.py'])
        self.assertEqual(rc, 1, out)
        self.assertIn('trace-check: no test to trace', out)

    def test_declared_inputs_cover_it(self):   # the config's inputs: what the test reads that the code does not show
        rc, out = self.check({'tests': ['tests/test_*.py'], 'inputs': [
            {'test': 'tests/test_hidden.py', 'reads': ['data/*.txt'], 'why': 'reads names from data'}]})
        self.assertEqual(rc, 0, out)
        self.assertIn('trace-check: 2 tests, 0 misses, 0 not traced', out)

    def test_a_test_that_cannot_be_traced_fails_the_check(self):
        rc, out = self.check({'tests': ['tests/test_seen.py']}, args=['--run', 'true {test}'])
        self.assertEqual(rc, 1, out)
        self.assertIn('tests/test_seen.py: the trace never shows the test file read', out)
        self.assertIn('1 not traced', out)


class NoStrace(Repo):
    def test_fails_without_strace(self):
        self.write({'tests/test_a.py': ''})
        with mock.patch.object(TC.shutil, 'which', return_value=None):
            rc, out = self.check({'tests': ['tests/test_a.py']})
        self.assertEqual(rc, 1)
        self.assertIn('strace is not on PATH; nothing was checked', out)


class Action(unittest.TestCase):
    text = open(ACTION).read()

    def test_runs_only_the_releases_script(self):
        runs = re.findall(r'^\s*run: (.*)$', self.text, re.M)
        self.assertEqual(runs, ['python3 -I "$GITHUB_ACTION_PATH/../trace_check.py" --config "$CONFIG" --run "$RUN" '
                                '--jobs "$JOBS"'])
        self.assertNotIn('uses:', self.text)

    def test_inputs_reach_the_script_by_env_only(self):
        self.assertNotIn('${{', re.search(r'^\s*run: (.*)$', self.text, re.M)[1])


if __name__ == '__main__':
    unittest.main(verbosity=1)
