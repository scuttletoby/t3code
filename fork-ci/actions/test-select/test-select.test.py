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
    'tests': ['tests/test_*.py', 'tools/sub/test_*.py', 'tests/test_*.sh'],
    'always_all': [{'glob': 'tests/run_tests.py', 'why': 'the test runner'},
                   {'glob': 'tests/_*.py', 'why': 'a shared test helper'},
                   {'glob': 'tests/fixtures/**', 'why': 'shared fixtures'}],
    'import_roots': ['lib'],
}
PRELUDE = 'import os, sys\nHERE = os.path.dirname(os.path.abspath(__file__))\nROOT = os.path.dirname(HERE)\n'
LINE = re.compile(r'^(selected \d+ of \d+ test files|no tool test touched: |all \d+ test files: |run [^:]+: (takes in |always \())')


def py(text):
    return PRELUDE + textwrap.dedent(text)


class Link(str):
    """A symlink's target, for Repo.write."""
    @property
    def target(self):
        return str(self)


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
                    'tools/sub/test_sub.py': 'import unittest\n', 'tests/test_sh.sh': 'true\n',
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
            if isinstance(text, Link):   # a tracked symlink
                if os.path.lexists(full):
                    os.remove(full)
                os.symlink(text.target, full)
                continue
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

    def always(self, code, why, base=None, test='tests/test_named.py', change='tools/alone.py'):
        """A test file with this code always runs (a change it does not reach still picks it), saying why."""
        self.base({test: py(code) if test.endswith('.py') else code, **(base or {})})
        self.pr({change: '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, [test]), out)
        self.assertIn(f'run {test}: always (', out)
        self.assertIn(why, out)
        return out

    def never(self, code, base, change, test='tests/test_named.py'):
        """A test file with this code is not chosen for this change (and is not an always-run)."""
        self.base({test: py(code), **base})
        self.pr({change: '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, []), out)


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
        self.assertTrue(out.startswith('no tool test touched: 0 of 4 test files take in the 1 changed paths'), out)

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
        for f in ('tools/sub/near.py', 'tools/far.py', 'tools/sub/__init__.py'):   # from . import: its __init__ runs
            with self.subTest(f):
                self.setUp()   # a fresh repository
                self.assertEqual(self.picks(base, {f: '# y\n'}), ['tools/sub/test_rel.py'])

    def test_dunder_import_of_a_written_name(self):
        base = {'tests/test_dyn.py': py("sys.path.insert(0, os.path.join(ROOT, 'tools'))\nm = __import__('mod')\n"),
                'tools/mod.py': ''}
        self.assertEqual(self.picks(base, {'tools/mod.py': '# y\n'}), ['tests/test_dyn.py'])

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

    COMMON = {'tests/common.py': 'import os\nTOP = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))\n',
              'conf/a.ini': '', 'conf/b.ini': ''}

    def test_a_name_imported_from_a_repository_module(self):   # from m import ROOT: m's value of ROOT
        self.one("from common import TOP\nopen(os.path.join(TOP, 'conf', 'a.ini'))\n", self.COMMON, 'conf/a.ini',
                 ['conf/b.ini'])

    def test_an_attribute_of_an_imported_repository_module(self):   # import m as x; x.ROOT
        self.one("import common as c\nopen(os.path.join(c.TOP, 'conf', 'a.ini'))\n", self.COMMON, 'conf/a.ini',
                 ['conf/b.ini'])

    def test_an_import_cycle_ends(self):   # each module's value of the other's name: unknown inside the cycle
        self.one("from cyc_a import A\nopen(os.path.join(A, 'a.ini'))\n",
                 {'tests/cyc_a.py': 'from cyc_b import B\nA = B + "/conf"\n',
                  'tests/cyc_b.py': 'from cyc_a import A\nB = "/x" if A else "conf/.."\n', 'conf/a.ini': ''},
                 'conf/a.ini')

    def test_the_imported_module_not_its_package(self):   # from pkg.mod import P: pkg/mod.py's P, not pkg/__init__'s
        self.base({'tests/pkg/__init__.py': 'P = "conf/b.ini"\n', 'tests/pkg/mod.py': 'P = "conf/a.ini"\n',
                   'tests/test_named.py': py('from pkg.mod import P\n'), 'conf/a.ini': '', 'conf/b.ini': ''})
        graph = TS.Graph(TS.Repo(self.d, set(self.git('ls-files').splitlines())), [])
        pf = graph.pyfile('tests/test_named.py')
        self.assertEqual(graph.imported(pf, 0, 'pkg.mod', 'P', 0), {'conf/a.ini'})
        self.assertEqual(graph.imported(pf, 0, 'pkg', 'P', 0), {'conf/b.ini'})
        self.assertEqual(graph.imported(pf, 0, 'json', 'X', 0), {TS.UNKNOWN})   # not a repository module

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
        for code in ("def f(name):\n    return os.path.join(ROOT, 'plugins', name)\n",
                     # the unknown part starts a name ('{name}.yml'): the known folder still holds
                     "def f(name):\n    return os.path.join(ROOT, 'plugins', f'{name}.yml')\n",
                     "def f(name):\n    return os.path.join(ROOT, 'plugins') + '/' + f'{name}.yml'\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'plugins/a/x.yml': ''}, 'plugins/b/new.yml', ['tools/other.py'])

    def test_an_unknown_rest_in_a_folder_the_file_is_inside_always_runs_it(self):   # HERE / name, ROOT / name
        for code, where in (("def f(name):\n    open(os.path.join(HERE, name))\n", 'tests/'),
                            ("def f(name):\n    open(os.path.join(ROOT, name))\n", 'the repository root/'),
                            ("def f(name):\n    open(f'{HERE}/{name}.json')\n", 'tests/'),
                            ("def f(name):\n    open(os.path.join(HERE, f'{name}.json'))\n", 'tests/'),
                            ("def f(name):\n    open(os.path.join(ROOT, f'{name}.json'))\n", 'the repository root/'),
                            ("def f(n):\n    open('/'.join([HERE, n]))\n", 'tests/'),
                            ("def f(n):\n    open('{}/{}'.format(HERE, n))\n", 'tests/'),
                            ("def f(n):\n    open('%s/%s' % (HERE, n))\n", 'tests/')):
            with self.subTest(code):
                self.setUp()
                self.always(code, f'a path under {where} it cannot work out')

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

    def test_a_path_with_an_unknown_start_keeps_its_known_rest(self):   # any place that ends with that rest
        for code in ("def load(base):\n    return open(os.path.join(base, 'data', 'b.json'))\nload(ROOT)\n",
                     "x = ROOT\nx += '/data/b.json'\nopen(x)\n",
                     "from pathlib import Path\n(Path.cwd() / 'data' / 'b.json').read_text()\n",
                     "open(os.path.join(os.getcwd(), 'data/b.json'))\n",
                     "d = ROOT\nfor part in ('data', 'b.json'):\n    d = os.path.join(d, part)\nopen(d)\n",
                     "open(os.path.join(os.environ['APP_ROOT'], 'data', 'b.json'))\n",
                     "open(os.environ.get('APP_ROOT', '/opt/app') + '/data/b.json')\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'data/b.json': '', 'data/other.json': ''}, 'data/b.json', ['data/other.json'])

    def test_a_tool_joining_onto_its_parameter(self):   # the test passes the working directory, the tool joins on it
        self.one("sys.path.insert(0, os.path.join(ROOT, 'tools'))\nimport lint\nlint.lint(os.getcwd())\n",
                 {'tools/lint.py': 'import os\ndef lint(root):\n    return open(os.path.join(root, "docs", "rules.md"))\n',
                  'docs/rules.md': '', 'docs/other.md': ''}, 'docs/rules.md', ['docs/other.md'])

    def test_a_folder_listed_by_a_process(self):   # grep -r, find, ls, rg: a new file there picks the test
        for code in ("import subprocess\ndef f(x):\n    subprocess.run(['grep', '-rl', x, 'docs'])\n",
                     "import subprocess\nsubprocess.run(['find', 'docs', '-name', '*.md'], cwd=ROOT)\n",
                     "import subprocess\nsubprocess.run('ls docs | wc -l', shell=True)\n",
                     "import subprocess\nsubprocess.run(['rg', '--files', os.path.join(ROOT, 'docs')])\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'docs/a.md': '', 'lib/b.md': ''}, 'docs/new.md', ['lib/b.md'])

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

    def test_or_and_if_else(self):   # every side of `or` and of `x if c else y`
        # (each side a folder: only the join after it names the file, so the side must be followed)
        for code in ("D = os.environ.get('A') or os.path.join(ROOT, 'conf')\nopen(os.path.join(D, 'a.ini'))\n",
                     "D = os.path.join(ROOT, 'conf') if os.environ.get('A') else 1\nopen(os.path.join(D, 'a.ini'))\n",
                     "D = 1 if os.environ.get('A') else os.path.join(ROOT, 'conf')\nopen(os.path.join(D, 'a.ini'))\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini', ['conf/b.ini'])

    def test_a_global_assigned_in_a_function(self):   # global P: the module's P, read anywhere
        self.one("P = 'b.ini'\ndef f():\n    global P\n    P = 'a.ini'\nopen(os.path.join(ROOT, 'conf', P))\n",
                 {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini')

    def test_a_global_read_past_an_enclosing_function(self):   # not the P of the function around it
        self.one("P = 'a.ini'\ndef g():\n    P = 'x.ini'\n    def f():\n        global P\n"
                 "        open(os.path.join(ROOT, 'conf', P))\n", {'conf/a.ini': '', 'conf/x.ini': ''}, 'conf/a.ini')

    def test_an_absolute_right_side_of_join_wins(self):
        self.one("open(os.path.join(os.environ['X'], ROOT + '/conf/a.ini'))\n", {'conf/a.ini': ''}, 'conf/a.ini')

    def test_a_shell_word_for_its_own_folder_is_not_all_of_it(self):   # cd tools/: a base, not every file
        self.one("SH = os.path.join(ROOT, 'tools', 'go.sh')\n", {'tools/go.sh': 'cd ../tools && sh go.sh\n',
                 'tools/other.sh': ''}, 'tools/go.sh', ['tools/other.sh'])

    def test_a_loop_over_a_written_out_sequence(self):   # each element, not the whole folder
        for code in ("for n in ('a.json', 'b.json'):\n    open(os.path.join(HERE, n))\n",
                     "NAMES = ['a.json', 'b.json']\nfor n in sorted(NAMES):\n    open(os.path.join(HERE, n))\n",
                     "X = [os.path.join(HERE, n) for n in ('a.json',) + ('b.json',)]\n",
                     "for k, n in {'x': 'a.json', 'y': 'b.json'}.items():\n    open(f'{HERE}/{n}')\n",
                     "for i, n in enumerate(('a.json', 'b.json')):\n    open(HERE + '/' + n)\n",
                     "for src, dst in (('a.json', '/etc/a'), ('b.json', '/etc/b')):\n    open(os.path.join(HERE, src))\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'tests/a.json': '', 'tests/b.json': '', 'tests/c.json': ''}, 'tests/a.json',
                         ['tests/c.json'])

    def test_join_format_and_percent(self):   # '/'.join, str.format, %
        for code in ("open('/'.join([ROOT, 'conf', 'a.ini']))\n", "open('{}/conf/{n}'.format(ROOT, n='a.ini'))\n",
                     "open('{0}/conf/a.ini'.format(ROOT))\n", "open('%s/conf/%s' % (ROOT, 'a.ini'))\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'conf/a.ini': '', 'conf/b.ini': ''}, 'conf/a.ini', ['conf/b.ini'])

    def test_python_dash_m_and_runpy(self):   # the module from the working directory (the root)
        for code in ("import subprocess\nsubprocess.run([sys.executable, '-m', 'tools.pkg.mod'])\n",
                     "import subprocess\nsubprocess.run(['python3', '-I', '-m', 'tools.pkg.mod'])\n",
                     "import subprocess\nsubprocess.run('cd x && python3 -m tools.pkg.mod', shell=True)\n",
                     "import runpy\nrunpy.run_module('tools.pkg.mod')\n"):
            with self.subTest(code):
                self.setUp()
                self.one(code, {'tools/pkg/__init__.py': '', 'tools/pkg/mod.py': '', 'tools/pkg/other.py': ''},
                         'tools/pkg/mod.py', ['tools/pkg/other.py'])

    def test_a_shell_script_runs_python_dash_m(self):
        self.one("SH = os.path.join(ROOT, 'tools', 'go.sh')\n",
                 {'tools/go.sh': 'python3 -m tools.mod "$@"\n', 'tools/mod.py': ''}, 'tools/mod.py')

    def test_shell_paths_from_the_scripts_own_folder(self):   # $HERE/x, ${DIR}/x, $(dirname "$0")/x, ...
        for sh in ('HERE="$(cd "$(dirname "$0")" && pwd)"\n. "$HERE/lib.sh"\n',
                   'DIR=$(dirname "${BASH_SOURCE[0]}")\nsource "${DIR}/lib.sh"\n',
                   '. "$(dirname "$0")/lib.sh"\n',
                   'HERE=$(dirname "$(readlink -f "$0")")\n. $HERE/lib.sh\n',
                   'HERE="${BASH_SOURCE%/*}"\n. "$HERE/lib.sh"\n',
                   'ROOT="$(cd "$(dirname "$0")/.." && pwd)"\n. "$ROOT/tools/lib.sh"\n',
                   'HERE="$(dirname "$0")"\nTOP="$HERE/.."\n. "$TOP/tools/lib.sh"\n',
                   'ROOT=$(git rev-parse --show-toplevel)\n. "$ROOT/tools/lib.sh"\n'):
            with self.subTest(sh):
                self.setUp()
                self.one("SH = os.path.join(ROOT, 'tools', 'run.sh')\n",
                         {'tools/run.sh': sh, 'tools/lib.sh': '', 'tools/other.sh': ''}, 'tools/lib.sh',
                         ['tools/other.sh'])

    def test_a_sys_path_module_in_two_places_is_both(self):   # Python's order is not worked out: both count
        base = {'a/mod.py': '', 'b/mod.py': '', 'c/mod.py': ''}
        code = "sys.path.insert(0, os.path.join(ROOT, 'b'))\nsys.path.append(os.path.join(ROOT, 'a'))\nimport mod\n"
        for change in ('a/mod.py', 'b/mod.py'):
            with self.subTest(change):
                self.setUp()
                self.one(code, base, change, ['c/mod.py'])


class FailClosed(Repo):
    """An input the action cannot place always runs the test files that reach it, saying why."""

    def test_git_ls_files(self):   # a repository-wide lint: any new file can break it
        for code in ("import subprocess\nsubprocess.run(['git', 'ls-files', '-z'], cwd=ROOT)\n",
                     "import subprocess\ndef git(*a):\n    return subprocess.run(['git', *a])\ngit('ls-files', 'x/')\n",
                     "import subprocess\nsubprocess.run('git -C . ls-files | grep x', shell=True)\n",
                     "import subprocess\nsubprocess.run(['git', 'grep', '-l', 'reload'])\n"):
            with self.subTest(code):
                self.setUp()
                self.always(code, 'lists the repository (git ls-files)')

    def test_git_ls_files_of_a_temporary_repository_is_not(self):
        self.never("import subprocess, tempfile\nD = tempfile.mkdtemp()\nsubprocess.run(['git', 'ls-files'], cwd=D)\n",
                   {}, 'tools/alone.py')

    def test_git_ls_files_in_a_shell_test(self):
        self.always('git ls-files -- scripts | xargs grep -l reload\n', 'tests/test_lint.sh: lists the repository',
                    test='tests/test_lint.sh')

    def test_a_tool_that_lists_is_left_to_the_trace_check(self):   # its caller passes the folder: mostly a temp repo
        for tool in ({'tools/lint.sh': 'git ls-files -- scripts | xargs grep -l reload\n'},
                     {'tools/lint.py': 'import subprocess\ndef f(d):\n    subprocess.run(["git", "ls-files"], cwd=d)\n'
                                       '    return os.listdir(d)\n'}):
            with self.subTest(tool=list(tool)):
                self.setUp()
                self.never(f"SH = os.path.join(ROOT, {list(tool)[0]!r})\n", tool, 'tools/alone.py')

    def test_walking_the_root(self):
        for code in ("for d, _, fs in os.walk(ROOT): pass\n", "from pathlib import Path\nPath(ROOT).rglob('*.py')\n",
                     "import glob\nglob.glob('**/*.sh', recursive=True)\nos.listdir()\n"):
            with self.subTest(code):
                self.setUp()
                self.always(code, 'lists the repository root')

    def test_a_glob_from_the_working_directory_matches_changed_paths(self):
        self.one_pick("import glob\nglob.glob('**/*.sh', recursive=True)\n", 'deep/x/new.sh')

    def one_pick(self, code, change):
        self.base({'tests/test_named.py': py(code)})
        self.pr({change: '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tests/test_named.py']), out)
        self.assertNotIn('always (', out)

    def test_listing_a_folder_it_cannot_work_out(self):
        self.always("def f(d):\n    return os.listdir(d)\n", 'lists a folder it cannot work out')

    def test_a_process_listing_a_folder_it_cannot_work_out(self):
        for code, why in (("import subprocess\ndef f(d):\n    subprocess.run(['grep', '-rl', 'x', d])\n", '(grep)'),
                          ("import subprocess\ndef f(d):\n    subprocess.run(f'find {d} -name x', shell=True)\n",
                           '(find)'),
                          ("import subprocess\nsubprocess.run(['find', '.', '-name', '*.py'])\n",
                           'lists the working directory (find)'),
                          ("import subprocess\ndef f(*a):\n    subprocess.run(['ls', *a])\n", '(ls with *args)')):
            with self.subTest(code):
                self.setUp()
                self.always(code, why)

    def test_a_process_listing_a_temporary_folder_or_reading_one_file_is_not(self):
        for code in ("import subprocess, tempfile\nd = tempfile.mkdtemp()\nsubprocess.run(['find', d, '-type', 'f'])\n",
                     "import subprocess, tempfile\nsubprocess.run(['ls'], cwd=tempfile.mkdtemp())\n",
                     "import subprocess\nsubprocess.run(['grep', '-l', 'x', os.path.join(ROOT, 'docs/a.md')])\n",
                     "def t(self):\n    self.assertEqual(out, 'ls docs failed')\n",
                     "FAKES = {'find': sys.argv[1], 'id': '#!/bin/sh'}\n"   # a loop target, not an argument list
                     "for name, body in FAKES.items():\n    pass\n"):
            with self.subTest(code):
                self.setUp()
                self.never(code, {'docs/a.md': ''}, 'docs/new.md')

    def test_listing_a_temporary_folder_is_not(self):
        for code in ("import tempfile\nos.listdir(tempfile.mkdtemp())\n",
                     "import tempfile\nwith tempfile.TemporaryDirectory() as d:\n    os.listdir(os.path.join(d, 'x'))\n",
                     "import tempfile\nclass T:\n    def setUp(self):\n        self.d = tempfile.mkdtemp()\n"
                     "    def t(self):\n        os.walk(self.d)\n"):
            with self.subTest(code):
                self.setUp()
                self.never(code, {}, 'tools/alone.py')

    def test_a_loop_variable_it_cannot_work_out(self):   # the first caller's copy loop over names from data
        self.always("import json\nfor n in json.load(open(os.path.join(HERE, 'x.json'))):\n"
                    "    open(os.path.join(HERE, n))\n", 'a path under tests/ it cannot work out',
                    {'tests/x.json': '[]'})

    def test_python_dash_m_by_a_name_it_cannot_work_out(self):
        for code in ("import subprocess\ndef f(m):\n    subprocess.run([sys.executable, '-m', m])\n",
                     "import runpy\ndef f(m):\n    runpy.run_module(m)\n"):
            with self.subTest(code):
                self.setUp()
                self.always(code, 'runs a module by a name it cannot work out')

    def test_git_dash_m_is_not_python(self):
        self.never("import subprocess\ndef f(m):\n    subprocess.run(['git', 'commit', '-m', m])\n", {},
                   'tools/alone.py')

    def test_a_shell_path_from_a_variable_it_cannot_work_out(self):
        self.always('. "$SOMEWHERE/lib.sh"\n', 'a path from a variable it cannot work out', {'tests/lib.sh': ''},
                    test='tests/test_run.sh')

    def shell_test(self, text, change='tools/alone.py'):
        self.base({'tests/test_run.sh': text, 'tests/lib.sh': ''})
        self.pr({change: '# y\n'})
        return self.select()

    def test_a_shell_variable_with_only_dots_is_not(self):   # "$tmp/." names no file of the repository
        self.assertEqual(self.shell_test('cp -r "$tmp/." "$out"\n')[1], [])

    def test_a_shell_variable_path_outside_the_repository_is_not(self):   # $OUT/report.txt names no file here
        self.assertEqual(self.shell_test('echo x > "$OUT/report.txt"\n')[1], [])

    def test_text_that_only_mentions_ls_files_is_not(self):   # a docstring, a message, a comparison: never run
        for code in ('def t():\n    """git ls-files still works."""\n',
                     "import unittest\nclass T(unittest.TestCase):\n    def t(self, err):\n"
                     "        self.assertIn('git ls-files -- x/ failed', err)\n",
                     "def f(c):\n    return c[:2] == ('git', 'ls-files')\n"):
            with self.subTest(code=code):
                self.setUp()
                self.never(code, {}, 'tools/alone.py')

    def test_ast_walk_is_not_a_listing(self):
        self.never("import ast\nfor n in ast.walk(ast.parse(open(__file__).read())): pass\n", {}, 'tools/alone.py')

    def test_a_temporary_folder_through_a_helper(self):   # .name, enterContext, a lambda, a method, git -C
        for code in ("import tempfile\nos.listdir(tempfile.TemporaryDirectory().name)\n",
                     "import tempfile, unittest\nclass T(unittest.TestCase):\n    def setUp(self):\n"
                     "        self.d = self.enterContext(tempfile.TemporaryDirectory())\n"
                     "    def t(self):\n        os.listdir(self.d)\n",
                     "import tempfile\nT = tempfile.mkdtemp()\np = lambda *a: os.path.join(T, *a)\nos.listdir(p('x'))\n",
                     "import tempfile\nclass T:\n    def setUp(self):\n        self.tmp = tempfile.mkdtemp()\n"
                     "    def path(self, p):\n        return os.path.join(self.tmp, p.lstrip('/'))\n"
                     "    def t(self):\n        os.listdir(self.path('/run'))\n",
                     "import subprocess, tempfile\nd = tempfile.mkdtemp()\nsubprocess.run(['git', '-C', d, 'ls-files'])\n"):
            with self.subTest(code=code):
                self.setUp()
                self.never(code, {}, 'tools/alone.py')

    def test_the_repository_through_a_helper(self):   # the same helpers over the repository: always
        for code in ("p = lambda *a: os.path.join(ROOT, *a)\nos.listdir(p('tools'))\n",
                     "def where():\n    return ROOT\nos.listdir(where())\n",
                     "import subprocess\nsubprocess.run(['git', '-C', ROOT, 'ls-files'])\n"):
            with self.subTest(code=code):
                self.setUp()
                self.always(code, 'tests/test_named.py')

    def test_more_values_than_the_cap(self):   # never a silent drop: the folder they share, or always
        names = ', '.join(repr(f'n{i}.json') for i in range(TS.CAP + 1))
        code = f"for n in ({names}):\n    open(os.path.join(ROOT, 'conf', n))\n"
        self.one_pick(code, 'conf/other.json')
        names = ', '.join(repr(f'{i}x') for i in range(TS.CAP + 1))   # no start they share
        self.setUp()
        self.always(f"for n in ({names}):\n    open(n + '.json')\n", f'more than {TS.CAP} values', {'3x.json': ''})
        self.setUp()   # text that names nothing here ('k=v' lines) is not a path
        self.never(f"for n in ({names}):\n    open(n + '.json')\n", {}, 'tools/alone.py')

    def test_a_value_deeper_than_the_limit(self):
        chain = ''.join(f"A{i + 1} = A{i} + 'x'\n" for i in range(TS.DEPTH + 2))
        self.always("A0 = ROOT + '/conf/'\n" + chain + f"open(A{TS.DEPTH + 2})\n", f'deeper than {TS.DEPTH} steps')


class ImportedRoot(Repo):
    def test_an_imported_root_with_an_unknown_rest_always_runs(self):   # open(os.path.join(m.ROOT, name))
        self.always("import common as c\nimport json\nfor p in json.load(open('l.json')):\n"
                    "    open(os.path.join(c.TOP, p))\n", 'a path under the repository root/',
                    base={'tests/common.py': NamedPaths.COMMON['tests/common.py'], 'l.json': '[]'})


class Symlinks(Repo):
    """A tracked symlink counts as its target (a change to the target picks every test that reaches the link)."""

    def test_a_symlinked_file(self):
        base = {'tests/test_link.py': py("sys.path.insert(0, os.path.join(ROOT, 'tools'))\nimport a\n"),
                'real/a.py': 'X = 1\n', 'tools/a.py': Link('../real/a.py'), 'real/b.py': ''}
        for change in ('real/a.py', 'tools/a.py'):
            with self.subTest(change):
                self.setUp()
                self.assertEqual(self.picks(base, {change: '# y\n' if change == 'real/a.py' else Link('../real/b.py')}),
                                 ['tests/test_link.py'])

    def test_a_symlinked_folder(self):
        base = {'tests/test_link.py': py("sys.path.insert(0, os.path.join(ROOT, 'tools', 'lib'))\nimport m\n"),
                'real/lib/m.py': 'X = 1\n', 'tools/lib': Link('../real/lib'), 'real/lib/n.py': ''}
        self.assertEqual(self.picks(base, {'real/lib/m.py': '# y\n'}), ['tests/test_link.py'])
        self.setUp()   # the link moved to another folder: what the test loads changed
        self.assertEqual(self.picks({**base, 'other/m.py': ''}, {'tools/lib': Link('../other')}), ['tests/test_link.py'])
        self.setUp()   # the same through a folder above the link: the file it loads is under a link
        base = {**base, 'tests/test_link.py': py("sys.path.insert(0, os.path.join(ROOT, 'tools'))\nfrom lib import m\n")}
        self.assertEqual(self.picks({**base, 'other/m.py': ''}, {'tools/lib': Link('../other')}), ['tests/test_link.py'])

    def test_a_listed_symlinked_folder_takes_in_its_target(self):
        base = {'tests/test_link.py': py("os.listdir(os.path.join(ROOT, 'tools', 'lib'))\n"),
                'real/lib/m.json': '', 'tools/lib': Link('../real/lib')}
        self.assertEqual(self.picks(base, {'real/lib/new.json': '# y\n'}), ['tests/test_link.py'])


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


class AlwaysRun(Repo):
    config = {**CONFIG, 'always_run': [{'glob': 'tests/test_lint*.py', 'why': 'scans every script'}]}

    def setUp(self):
        super().setUp()
        self.write({CONFIG_PATH: json.dumps(self.config)})

    def test_an_always_run_test_runs_on_any_change(self):
        self.base({'tests/test_lint.py': 'import unittest\n'})
        self.pr({'tools/alone.py': 'X = 2\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tests/test_lint.py']), out)
        self.assertIn('run tests/test_lint.py: always (always_run: scans every script)', out)

    def test_an_always_run_glob_that_names_no_test_fails_the_step(self):   # a renamed lint must be renamed there
        self.base({})
        _, _, err = self.select(rc=1)
        self.assertIn("always_run 'tests/test_lint*.py' names no test file", err)


class Declared(Repo):
    """The config's inputs: what a test reads that its code does not show (a folder its caller passes)."""
    config = {**CONFIG, 'inputs': [{'test': 'tests/test_d*.py', 'reads': ['data/**', 'docs/*.md'],
                                    'why': 'lists the folder its helper is given'}]}
    code = py("def f(d):\n    return os.listdir(d)\nf(os.path.join(ROOT, 'data'))\n")

    def setUp(self):
        super().setUp()
        self.write({CONFIG_PATH: json.dumps(self.config)})

    def test_a_declared_test_runs_when_what_it_reads_changes(self):
        for change, picked in (('data/x/new.json', True), ('docs/a.md', True), ('docs/sub/a.md', False),
                               ('tools/alone.py', False)):
            with self.subTest(change):
                self.setUp()
                self.base({'tests/test_d.py': self.code, 'data/a.json': '', 'docs/a.md': ''})
                self.pr({change: '# y\n'})
                everything, chosen, out = self.select()
                self.assertEqual((everything, chosen), (False, ['tests/test_d.py'] if picked else []), out)
                if picked:
                    self.assertIn('(declared in inputs)', out)

    def test_a_mark_from_another_file_still_runs_it(self):   # only the test file's own marks give way
        self.base({'tests/test_d.py': self.code + "sys.path.insert(0, os.path.join(ROOT, 'tools'))\nimport lister\n",
                   'tools/lister.py': 'import os\nos.listdir(".")\n'})
        self.pr({'tools/alone.py': '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tests/test_d.py']), out)
        self.assertIn('lists the repository root', out)

    def test_an_undeclared_test_still_always_runs(self):
        self.base({'tests/test_d.py': '', 'tests/test_e.py': self.code})
        self.pr({'tools/alone.py': '# y\n'})
        everything, chosen, out = self.select()
        self.assertEqual((everything, chosen), (False, ['tests/test_e.py']), out)
        self.assertIn('lists a folder it cannot work out', out)

    def test_a_declaration_that_names_no_test_or_is_malformed_fails_the_step(self):
        for inputs in ([{'test': 'tests/test_gone.py', 'reads': [], 'why': 'x'}],
                       [{'test': 'tests/test_plain.py', 'reads': 'data/**', 'why': 'x'}],
                       [{'test': 'tests/test_plain.py', 'reads': ['data/**']}]):
            with self.subTest(inputs):
                self.write({CONFIG_PATH: json.dumps({**CONFIG, 'inputs': inputs})})
                self.base({})
                self.select(rc=1)


class Config(Repo):
    def test_a_config_it_cannot_read_fails_the_step(self):   # no test list: red, never zero tests
        for text in ('{', '{"tests": []}', '{"tests": ["t"], "always": []}', '{"tests": ["t"], "always_all": ["x"]}',
                     '{"tests": ["tests/*.py"], "always_run": ["tests/test_plain.py"]}',
                     '{"tests": ["tests/*.py"], "import_roots": ["../x"]}'):
            with self.subTest(text):
                self.write({CONFIG_PATH: text})
                self.base({})
                self.select(rc=1)

    def test_a_tests_glob_that_matches_no_file_fails_the_step(self):   # a moved folder: red, never fewer tests
        self.write({CONFIG_PATH: json.dumps({**CONFIG, 'tests': ['tests/test_*.py', 'moved/test_*.py']})})
        self.base({})
        for event in ('pull_request', 'push'):
            _, _, err = self.select(event=event, rc=1)
            self.assertIn("tests glob 'moved/test_*.py' matches no file", err)

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
        self.assertEqual(chosen[:3], ['tools/sub/test_b.py', 'tools/sub/test_sub.py', 'tests/test_a.py'])

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
        self.assertEqual(runs, ['exec python3 -I "$GITHUB_ACTION_PATH/../git-clean/git_clean.py" check --path . --require',   # the git-clean check of this pinned SHA
                                'python3 -I "$GITHUB_ACTION_PATH/test_select.py" --config "$CONFIG" --event "$EVENT"'])
        self.assertNotIn('uses:', self.text)
        self.assertIn('using: composite', self.text)

    def test_inputs_reach_the_script_by_env_only(self):   # never ${{ }} inside run: (script injection)
        for run in re.findall(r'^\s*run: (.*)$', self.text, re.M):
            self.assertNotIn('${{', run)

    def test_declares_every_output_the_script_writes(self):
        for name in ('all', 'count', 'total', 'list', 'reason'):
            self.assertIn(f'value: ${{{{ steps.select.outputs.{name} }}}}', self.text)


if __name__ == '__main__':
    unittest.main(verbosity=1)
