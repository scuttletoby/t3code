"""test_select.py: a pull request runs only the test files its change can reach; anything unclear runs every test.
Real git repos: GitHub's refs/pull/N/merge is a merge commit (HEAD^1 = the base, HEAD^2 = the PR head), built here
with `git merge --no-ff`, read the way docs-scope reads it.
Run: python3 fork-ci/actions/test-select/test-select.test.py
"""
import contextlib, importlib.util, io, json, os, re, shutil, subprocess, sys, tempfile, textwrap, unittest
from unittest import mock

sys.dont_write_bytecode = True   # the tests import test_select.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'test_select.py')
ACTION = os.path.join(HERE, 'action.yml')
_spec = importlib.util.spec_from_file_location('test_select_api', SCRIPT)
TS = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(TS)
CONFIG_PATH = '.github/test-select.json'
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
# A project's config, as a caller would write it.
CONFIG = {
    'tests': ['tests/test_*.py', 'tools/sub/test_*.py'],
    'always_all': [{'glob': 'tests/run_tests.py', 'why': 'the test runner'},
                   {'glob': 'tests/_*.py', 'why': 'a shared test helper'},
                   {'glob': 'tests/fixtures/**', 'why': 'shared fixtures'}],
    'import_roots': ['lib'],
}
PRELUDE = 'import os, sys\nHERE = os.path.dirname(os.path.abspath(__file__))\nROOT = os.path.dirname(HERE)\n'
LINE = re.compile(r'^(selected \d+ of \d+ test files|no tool test touched: |all \d+ test files: |run [^:]+: takes in )')


def py(text):
    return PRELUDE + textwrap.dedent(text)


class CleanEnv(unittest.TestCase):
    """Every test starts with no GITHUB_* variable (a runner's own would otherwise reach the script)."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        for key in [k for k in os.environ if k.startswith('GITHUB_') or k == 'RUNNER_TEMP']:
            del os.environ[key]


class Repo(CleanEnv):
    """A repository with a test that touches nothing (tests/test_plain.py) and a tool no test reaches."""
    config = CONFIG

    def setUp(self):
        super().setUp()
        self.d = tempfile.mkdtemp(prefix='test-select-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.git('init', '-q', '-b', 'main')
        self.write({CONFIG_PATH: json.dumps(self.config), 'tests/test_plain.py': 'import unittest\n',
                    'tests/run_tests.py': 'import sys\n', 'tools/alone.py': 'X = 1\n', 'README.md': 'x\n',
                    'lib/.keep': ''})

    def base(self, files):
        """The default branch's tree: the common files plus these; returns its commit."""
        self.basesha = self.commit('base', files)
        return self.basesha

    def git(self, *a):
        r = subprocess.run(['git', *a], cwd=self.d, text=True, capture_output=True, env={**os.environ, **ENV})
        if r.returncode:
            raise RuntimeError(f'git {a}: {r.stderr}')
        return r.stdout.strip()

    def write(self, files):
        for path, text in files.items():
            full = os.path.join(self.d, path)
            if text is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write(text)

    def commit(self, msg='c', files=None):
        if files:
            self.write(files)
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', msg)
        return self.git('rev-parse', 'HEAD')

    def pr(self, *pushes, main_after=None):
        """One PR commit per push ({path: text, None = delete}); main may move on; GitHub's merge is checked out."""
        self.git('checkout', '-q', '-b', 'pr', self.basesha)
        for files in pushes:
            self.commit('push', files)
        self.git('checkout', '-q', 'main')
        if main_after:
            self.commit('main moved', main_after)
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')

    def select(self, event='pull_request', sha=None, head_sha=None, config=CONFIG_PATH, rc=0):
        """(all, chosen, stdout). The runner's view: $GITHUB_SHA is HEAD, the event's pull_request.head.sha HEAD^2."""
        out, event_path = os.path.join(self.d, '.output'), os.path.join(self.d, '.event.json')
        listing = os.path.join(self.d, '.list')
        head = self.git('rev-parse', 'HEAD')
        parents = self.git('rev-list', '--parents', '-n', '1', 'HEAD').split()
        head_sha = (parents[2] if len(parents) == 3 else head) if head_sha is None else head_sha
        with open(event_path, 'w') as f:
            json.dump({'pull_request': {'head': {'sha': head_sha}}}, f)
        env = {**os.environ, **ENV, 'GITHUB_OUTPUT': out, 'GITHUB_SHA': head if sha is None else sha,
               'GITHUB_EVENT_PATH': event_path, 'RUNNER_TEMP': self.d}
        r = subprocess.run([sys.executable, '-I', SCRIPT, '--config', config, '--event', event], cwd=self.d,
                           capture_output=True, env=env)
        stdout = r.stdout.decode()
        self.assertEqual(r.returncode, rc, r.stderr.decode() + stdout)
        if rc:
            return None, None, r.stderr.decode()
        with open(out) as f:
            written = f.read()
        m = re.fullmatch(r'all=(true|false)\ncount=(\d+)\ntotal=(\d+)\nlist=([^\n]+)\nreason=([^\n]+)\n', written)
        self.assertTrue(m, written)
        self.assertEqual(m[4], os.path.join(self.d, 'test-select.txt'))
        with open(m[4]) as f:
            chosen = f.read().splitlines()
        self.assertEqual(int(m[2]), len(chosen))
        self.assertEqual(m[5], stdout.splitlines()[0])
        for line in stdout.splitlines():   # a file name never starts a line: no ::workflow-command::
            self.assertRegex(line, LINE, stdout)
            self.assertTrue(line.isprintable(), repr(line))
        for f in (out, event_path, listing, m[4]):
            os.remove(f) if os.path.exists(f) else None
        return m[1] == 'true', chosen, stdout

    def picks(self, base, *pushes):
        """The test files a PR with these pushes runs on top of this base (never 'every test')."""
        self.base(base)
        self.pr(*pushes)
        everything, chosen, out = self.select()
        self.assertFalse(everything, out)
        return chosen

    def runs_all(self, base, *pushes, why=''):
        self.base(base)
        self.pr(*pushes)
        everything, chosen, out = self.select()
        self.assertTrue(everything, out)
        self.assertTrue(out.startswith('all '), out)
        self.assertIn(why, out.splitlines()[0])
        self.assertEqual(chosen, self.every())
        return out

    def every(self):
        tracked = set(self.git('ls-files').splitlines())
        return TS.universe(self.config['tests'], tracked)


# ---------------------------------------------------------------- what a test file takes in

TOOL_TEST = py('''
    sys.path.insert(0, os.path.join(HERE, '..', 'tools'))
    import tool
''')


class Imports(Repo):
    base_files = {'tests/test_tool.py': TOOL_TEST, 'tools/tool.py': 'import helper\n', 'tools/helper.py': 'X = 1\n'}

    def test_the_test_file_itself(self):
        self.assertEqual(self.picks({}, {'tests/test_plain.py': 'import unittest  # y\n'}), ['tests/test_plain.py'])

    def test_a_tool_no_test_reaches_runs_none_and_says_so(self):
        self.base(self.base_files)
        self.pr({'tools/alone.py': 'X = 2\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, []))
        self.assertTrue(out.startswith('no tool test touched: 0 of 2 test files take in the 1 changed paths'), out)

    def test_a_module_found_through_sys_path_insert_and_what_it_imports(self):
        self.assertEqual(self.picks(self.base_files, {'tools/helper.py': 'X = 2\n'}), ['tests/test_tool.py'])

    def test_sys_path_through_an_alias(self):   # import sys as _sys; _sys.path.insert(...)
        test = TOOL_TEST.replace('import os, sys', 'import os, sys as _sys').replace('sys.path', '_sys.path')
        self.assertEqual(self.picks({**self.base_files, 'tests/test_tool.py': test}, {'tools/helper.py': 'X = 2\n'}),
                         ['tests/test_tool.py'])

    def test_a_sys_path_entry_another_module_adds(self):   # sys.path is one list for the whole process
        base = {'tests/test_late.py': 'import setup_path\nimport late\n',
                'tests/setup_path.py': py("sys.path.append(os.path.join(ROOT, 'tools', 'sub'))\n"),
                'tools/sub/late.py': 'X = 1\n'}
        self.assertEqual(self.picks(base, {'tools/sub/late.py': 'X = 2\n'}), ['tests/test_late.py'])

    def test_a_sibling_module_and_a_package(self):   # the file's own folder; a package's __init__ runs first
        base = {'tests/test_pkg.py': 'import kit.part\nfrom kit import other\n', 'tests/kit/__init__.py': '',
                'tests/kit/part.py': '', 'tests/kit/other.py': ''}
        for f in ('tests/kit/__init__.py', 'tests/kit/part.py', 'tests/kit/other.py'):
            with self.subTest(f):
                self.setUp()   # a fresh repository
                self.assertEqual(self.picks(base, {f: '# y\n'}), ['tests/test_pkg.py'])

    def test_a_relative_import(self):
        base = {'tools/sub/test_rel.py': 'from . import near\nfrom .. import far\n', 'tools/sub/__init__.py': '',
                'tools/sub/near.py': '', 'tools/__init__.py': '', 'tools/far.py': ''}
        for f in ('tools/sub/near.py', 'tools/far.py'):
            with self.subTest(f):
                self.setUp()   # a fresh repository
                self.assertEqual(self.picks(base, {f: '# y\n'}), ['tools/sub/test_rel.py'])

    def test_a_configured_import_root(self):   # code loads from an install path that mirrors lib/
        base = {'tests/test_root.py': 'import shared\n', 'lib/shared.py': ''}
        self.assertEqual(self.picks(base, {'lib/shared.py': '# y\n'}), ['tests/test_root.py'])

    def test_importlib_by_a_literal_name(self):
        base = {'tests/test_il.py': py("import importlib\nsys.path.insert(0, ROOT + '/tools')\n"
                                       "m = importlib.import_module('dyn')\n"), 'tools/dyn.py': ''}
        self.assertEqual(self.picks(base, {'tools/dyn.py': '# y\n'}), ['tests/test_il.py'])

    def test_a_deleted_module_still_counts(self):   # the test that still imports it is chosen, and fails there
        self.assertEqual(self.picks(self.base_files, {'tools/helper.py': None}), ['tests/test_tool.py'])

    def test_a_renamed_tool_runs_the_test_that_names_the_old_path(self):
        self.assertEqual(self.picks(self.base_files, {'tools/helper.py': None, 'tools/helper2.py': 'X = 1\n'}),
                         ['tests/test_tool.py'])

    def test_a_relative_sys_path_entry_from_the_root(self):   # the usual working directory is the root
        base = {'tests/test_cwd.py': 'import sys\nsys.path.insert(0, "tools/sub")\nimport deep\n',
                'tools/sub/deep.py': ''}
        self.assertEqual(self.picks(base, {'tools/sub/deep.py': '# y\n'}), ['tests/test_cwd.py'])

    def test_a_new_test_file_runs(self):   # the config's glob finds it; it is a changed path
        self.assertEqual(self.picks({}, {'tests/test_new.py': 'import unittest\n'}), ['tests/test_new.py'])


class NamedPaths(Repo):
    def one(self, code, base, change, others=()):
        """The test file with this code is chosen for `change` and not for each of `others`."""
        files = {'tests/test_named.py': py(code), **base}
        self.assertEqual(self.picks(files, {change: '# y\n'}), ['tests/test_named.py'], change)
        for o in others:
            with self.subTest(o):
                self.git('checkout', '-q', '-f', 'main')
                self.git('reset', '-q', '--hard', 'HEAD^1')
                self.git('branch', '-q', '-D', 'pr')
                self.pr({o: '# z\n'})
                everything, chosen, out = self.select()
                self.assertEqual((everything, chosen), (False, []), out)

    def test_a_script_it_runs(self):
        self.one("import subprocess\nsubprocess.run([sys.executable, os.path.join(ROOT, 'tools', 'run.py')])\n",
                 {'tools/run.py': '', 'tools/other.py': ''}, 'tools/run.py', ['tools/other.py'])

    def test_what_a_run_script_imports(self):
        self.one("SCRIPT = os.path.join(ROOT, 'tools', 'run.py')\n",
                 {'tools/run.py': 'import dep\n', 'tools/dep.py': ''}, 'tools/dep.py')

    def test_pathlib_parent_and_parents(self):
        self.one("from pathlib import Path\nA = Path(__file__).parent / 'data.json'\n"
                 "B = Path(__file__).resolve().parents[1] / 'conf' / 'b.toml'\n",
                 {'tests/data.json': '', 'conf/b.toml': '', 'conf/c.toml': ''}, 'conf/b.toml', ['conf/c.toml'])

    def test_an_f_string(self):
        self.one("A = f'{ROOT}/conf/{\"a\"}.ini'\n", {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini', ['conf/b.ini'])

    def test_plus(self):
        self.one("B = ROOT + '/conf/' + 'b.ini'\n", {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/b.ini', ['conf/a.ini'])

    def test_a_root_relative_literal(self):   # the usual working directory is the repository root
        self.one("open('conf/a.ini')\n", {'conf/a.ini': ''}, 'conf/a.ini')

    def test_a_listed_folder_takes_in_everything_under_it(self):
        self.one("CASES = os.path.join(HERE, 'cases')\nfor n in os.listdir(CASES): pass\n",
                 {'tests/cases/a.json': ''}, 'tests/cases/new.json')

    def test_a_folder_only_named_is_a_base(self):   # files in it count when named, the rest does not
        self.one("CASES = os.path.join(ROOT, 'cases')\nopen(os.path.join(CASES, 'a.json'))\n",
                 {'cases/a.json': '', 'cases/b.json': ''}, 'cases/a.json', ['cases/b.json'])

    def test_a_glob_pattern_matches_the_changed_paths(self):
        self.one("import glob\nglob.glob(os.path.join(ROOT, 'docs', '*.md'))\n",
                 {'docs/a.md': '', 'docs/a.txt': ''}, 'docs/new.md', ['docs/a.txt', 'docs/x/b.md'])

    def test_a_pattern_that_starts_with_a_wildcard_names_no_place(self):
        self.one("import glob\nglob.glob(os.path.join(ROOT, 'conf', 'a.ini'))\nP = '**/*.ini'\n",
                 {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini', ['conf/b.ini'])

    def test_a_known_folder_with_an_unknown_rest(self):   # ROOT / 'plugins' / name: anything in plugins/
        self.one("def f(name):\n    return os.path.join(ROOT, 'plugins', name)\n",
                 {'plugins/a/x.yml': ''}, 'plugins/b/new.yml', ['tools/other.py'])

    def test_an_unknown_rest_in_a_folder_the_file_is_inside_is_not_all_of_it(self):   # HERE / name, ROOT / name
        self.one("def f(name):\n    open(os.path.join(HERE, name))\n    open(os.path.join(ROOT, name))\n"
                 "open(os.path.join(HERE, 'a.json'))\n",
                 {'tests/a.json': '', 'tests/b.json': ''}, 'tests/a.json', ['tests/b.json', 'tools/other.py'])

    def test_a_message_is_not_a_path(self):   # a space, quote, ( or $ : a message or a shell line
        self.one("def f(e):\n    raise SystemExit(f'{ROOT}/lib/ is missing: {e}')\nopen(ROOT + '/lib/a.py')\n",
                 {'lib/a.py': '', 'lib/b.py': ''}, 'lib/a.py', ['lib/b.py'])

    def test_a_name_is_looked_up_in_its_own_scope(self):   # base in g() is not the base in f()
        self.one("def f():\n    base = 'conf'\n    open(os.path.join(ROOT, base, 'a.ini'))\n"
                 "def g(x):\n    base = 'docs'\n    return base + x\n",
                 {'conf/a.ini': '', 'docs/a.ini': ''}, 'conf/a.ini', ['docs/a.ini'])

    def test_a_name_with_many_assignments_has_all_their_values(self):   # the values reach the path only by name
        for change in ('conf/a.ini', 'docs/a.ini'):
            with self.subTest(change):
                self.setUp()
                self.one("P = 'conf'\nif sys.platform == 'x':\n    P = 'docs'\nopen(os.path.join(ROOT, P, 'a.ini'))\n",
                         {'conf/a.ini': '', 'docs/a.ini': ''}, change)

    def test_an_attribute_on_self(self):   # set in one method, joined in another
        self.one("class T:\n    def setUp(self):\n        self.d = 'conf'\n"
                 "    def test(self):\n        open(os.path.join(ROOT, self.d, 'a.ini'))\n",
                 {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini', ['conf/b.ini'])

    def test_a_shell_script_and_the_files_it_names(self):
        self.one("subprocess = None\nSH = os.path.join(ROOT, 'tools', 'go.sh')\n",
                 {'tools/go.sh': 'python3 tools/dep.py\ncp -r assets/ "$OUT"\n', 'tools/dep.py': 'import dep2\n',
                  'tools/dep2.py': '', 'assets/a.png': ''}, 'tools/dep2.py')

    def test_a_shell_folder_word_takes_in_the_folder(self):
        self.one("SH = os.path.join(ROOT, 'tools', 'go.sh')\n",
                 {'tools/go.sh': 'cp -r assets/img/ "$OUT"\n', 'assets/img/a.png': ''}, 'assets/img/new.png')

    def test_a_script_without_extension_by_its_shebang(self):
        self.one("BIN = os.path.join(ROOT, 'bin', 'tool')\n",
                 {'bin/tool': '#!/usr/bin/env python3\nimport dep\n', 'bin/dep.py': ''}, 'bin/dep.py')

    def test_javascript(self):
        self.one("JS = os.path.join(ROOT, 'tools', 'a.mjs')\n",
                 {'tools/a.mjs': "import x from './b.mjs';\n", 'tools/b.mjs': ''}, 'tools/b.mjs')


# ---------------------------------------------------------------- every test runs (fails closed)

class EveryTest(Repo):
    def test_events_other_than_pull_request(self):   # main pushes, the merge queue, schedules, manual runs
        self.base({})
        self.pr({'tools/alone.py': 'X = 2\n'})
        for event in ('push', 'merge_group', 'schedule', 'workflow_dispatch', 'pull_request_target'):
            with self.subTest(event):
                everything, chosen, out = self.select(event=event)
                self.assertTrue(everything, out)
                self.assertIn(f'a {event} run runs every test', out)
                self.assertEqual(chosen, self.every())

    def test_an_empty_change(self):   # from git (docs-scope's rule) and from a --changed list
        self.runs_all({}, {}, why='the change touches no file')
        changed, out = os.path.join(self.d, '.changed'), os.path.join(self.d, '.out')
        open(changed, 'w').close()
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            TS.main(['--root', self.d, '--event', 'pull_request', '--changed', changed, '--out', out])
        self.assertIn('the change touches no file', stdout.getvalue())
        with open(out) as f:
            self.assertEqual(f.read().splitlines(), self.every())

    def test_a_head_that_is_not_the_merge_commit(self):   # docs-scope's rule: an unclear change is a full run
        self.base({})
        self.pr({'tools/alone.py': 'X = 2\n'})
        head = self.git('rev-parse', 'HEAD')
        for sha, head_sha in ((self.basesha, None), (head, self.basesha)):
            with self.subTest(sha=sha, head_sha=head_sha):
                everything, chosen, out = self.select(sha=sha, head_sha=head_sha)
                self.assertTrue(everything, out)
        self.git('reset', '-q', '--hard', 'HEAD^1')   # a plain commit, not a merge
        self.commit('direct', {'tools/alone.py': 'X = 3\n'})
        everything, _, out = self.select(head_sha='')
        self.assertTrue(everything, out)

    def test_the_config(self):
        self.runs_all({}, {CONFIG_PATH: json.dumps({**CONFIG, 'import_roots': []})}, why='the test-select config')

    def test_a_workflow_file(self):
        self.runs_all({'.github/workflows/ci.yml': 'on: push\n'}, {'.github/workflows/ci.yml': 'on: pull_request\n'},
                      why='a workflow file')

    def test_each_always_all_path(self):
        for path, why in (('tests/run_tests.py', 'the test runner'), ('tests/_guard.py', 'a shared test helper'),
                          ('tests/fixtures/x/y.json', 'shared fixtures')):
            with self.subTest(path):
                self.setUp()
                out = self.runs_all({}, {path: '# y\n'}, why=why)
                self.assertIn(path, out)

    def test_a_test_input_that_does_not_parse(self):
        self.runs_all({'tests/test_tool.py': TOOL_TEST, 'tools/tool.py': 'X = 1\n'}, {'tools/tool.py': 'def (\n'},
                      why='cannot parse tools/tool.py')

    def test_an_import_by_a_name_it_cannot_work_out(self):
        self.runs_all({'tests/test_il.py': 'import importlib\ndef f(n):\n    importlib.import_module(n)\n'},
                      {'tools/alone.py': 'X = 2\n'}, why='a module imported by a name it cannot work out')

    def test_a_repository_module_it_cannot_find(self):   # a sys.path entry it could not work out, say
        self.runs_all({'tests/test_lost.py': 'import sys\nsys.path.insert(0, sys.argv[1])\nimport alone\n'},
                      {'tools/alone.py': 'X = 2\n'}, why='tests/test_lost.py:3: import alone')

    def test_a_third_party_import_is_not_a_reason(self):   # no module of that name in the repository
        self.assertEqual(self.picks({'tests/test_tp.py': 'import yaml\nimport os\n'},
                                    {'tests/test_tp.py': 'import yaml\nimport os\n# y\n'}), ['tests/test_tp.py'])

    def test_an_error_nobody_foresaw(self):
        self.base({})
        self.pr({'tools/alone.py': 'X = 2\n'})
        changed, out = os.path.join(self.d, '.changed'), os.path.join(self.d, '.out')
        with open(changed, 'w') as f:
            f.write('tools/alone.py\n')
        stdout = io.StringIO()
        with mock.patch.object(TS, 'decide', side_effect=RuntimeError('boom')), contextlib.redirect_stdout(stdout):
            TS.main(['--root', self.d, '--event', 'pull_request', '--changed', changed, '--out', out])
        self.assertIn('test-select failed (RuntimeError: boom)', stdout.getvalue())
        with open(out) as f:
            self.assertEqual(f.read().splitlines(), self.every())


class Config(Repo):
    def test_a_config_it_cannot_read_fails_the_step(self):   # no test list: red, never zero tests
        for text in ('{', '{"tests": []}', '{"tests": ["t"], "always": []}', '{"tests": ["t"], "always_all": ["x"]}',
                     '{"tests": ["tests/*.py"], "import_roots": ["../x"]}'):
            with self.subTest(text):
                self.write({CONFIG_PATH: text})
                self.base({})
                self.select(rc=1)

    def test_a_test_file_the_config_names_that_is_gone(self):   # a renamed or deleted test file named literally
        self.write({CONFIG_PATH: json.dumps({**CONFIG, 'tests': ['tests/test_plain.py', 'tests/test_gone.py']})})
        self.base({})
        _, _, err = self.select(rc=1)
        self.assertIn('tests/test_gone.py', err)

    def test_the_list_keeps_config_order(self):
        self.write({CONFIG_PATH: json.dumps({**CONFIG, 'tests': ['tools/sub/test_*.py', 'tests/test_*.py']})})
        self.config = {**CONFIG, 'tests': ['tools/sub/test_*.py', 'tests/test_*.py']}
        self.base({'tools/sub/test_b.py': 'import alone2\n', 'tools/sub/alone2.py': '',
                   'tests/test_a.py': 'import sys\nsys.path.insert(0, "tools/sub")\nimport alone2\n'})
        self.pr({'tools/sub/alone2.py': '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tools/sub/test_b.py', 'tests/test_a.py']), out)
        everything, chosen, _ = self.select(event='push')
        self.assertEqual(chosen[:2], ['tools/sub/test_b.py', 'tests/test_a.py'])

    def test_a_changed_name_cannot_write_a_line_of_its_own(self):
        base = {'tests/test_ls.py': py("for n in os.listdir(os.path.join(ROOT, 'data')): pass\n"), 'data/a': ''}
        self.base(base)
        self.pr({'data/a\n::warning::x': 'y'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tests/test_ls.py']), out)
        self.assertIn('\\n::warning::x', out)


class Action(CleanEnv):
    """action.yml runs only this folder's script (the caller's pinned SHA), never a file from the checkout."""
    text = open(ACTION).read()

    def test_runs_only_its_own_script(self):
        runs = re.findall(r'^\s*run: (.*)$', self.text, re.M)
        self.assertEqual(runs, ['python3 -I "$GITHUB_ACTION_PATH/test_select.py" --config "$CONFIG" --event "$EVENT"'])
        self.assertNotIn('uses:', self.text)
        self.assertIn('using: composite', self.text)

    def test_inputs_reach_the_script_by_env_only(self):   # never ${{ }} inside run: (script injection)
        self.assertNotIn('${{', re.search(r'^\s*run: (.*)$', self.text, re.M)[1])

    def test_declares_every_output_the_script_writes(self):
        for name in ('all', 'count', 'total', 'list', 'reason'):
            self.assertIn(f'value: ${{{{ steps.select.outputs.{name} }}}}', self.text)


if __name__ == '__main__':
    unittest.main(verbosity=1)
