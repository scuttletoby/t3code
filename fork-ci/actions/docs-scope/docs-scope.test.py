"""docs_scope.py: a docs-only change builds and tests nothing; jobs run only for the files that reach them.
Real git repos: GitHub's refs/pull/N/merge is a merge commit (HEAD^1 = the base, HEAD^2 = the PR head), built here
with `git merge --no-ff`. Anything unclear must mean a full run.
Run: python3 fork-ci/actions/docs-scope/docs-scope.test.py
"""
import json, os, re, shutil, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True   # the Globs test imports docs_scope.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'docs_scope.py')
ACTION = os.path.join(HERE, 'action.yml')
CONFIG_PATH = '.github/docs-scope.json'
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
REGISTRY_PATH = 'tools/registry.json'
REGISTRY = {'modules': [
    {'id': 'site', 'kind': 'plugin', 'inputs': ['net/site/README.md']},
    {'id': 'content-locale', 'kind': 'engine-module', 'inputs': ['plugins/locale/common/src/main/java/Wire.java']},
    {'id': 'content-travel', 'kind': 'engine-module', 'inputs': ['plugins/travel/common/src/main/java']},
]}
# A project's rules, as a caller would write them (the general half of a docs-only gate).
CONFIG = {
    'docs': ['**/*.md'],
    'never_docs': [
        {'glob': 'src/**', 'why': 'inside a source set'},
        {'glob': '**/src/**', 'why': 'inside a source set'},
        {'glob': 'resources/**', 'why': 'inside a resources folder'},
        {'glob': '**/resources/**', 'why': 'inside a resources folder'},
        {'glob': '**/tests/**', 'why': 'inside a test folder'},
        {'glob': '**/test/**', 'why': 'inside a test folder'},
        {'glob': '**/fixtures/**', 'why': 'inside a fixture folder'},
        {'glob': 'skills/review/SKILL.md', 'why': 'read by a tooling test'},
        {'glob': 'plugins/RELEASE-CLAIMS.md', 'why': 'read by the release tool'},
    ],
    'never_docs_from': [{'file': REGISTRY_PATH, 'list': 'modules', 'field': 'inputs', 'label': 'id',
                         'why': 'listed in the registry'}],
    'jobs': {
        'plugins': {'paths': ['plugins/**']},
        'engine': {'paths': ['engine/**', 'plugins/items/**'],
                   'paths_from': [{'file': REGISTRY_PATH, 'list': 'modules', 'where': {'kind': 'engine-module'},
                                   'field': 'inputs', 'label': 'id'}]},
    },
}


class Repo(unittest.TestCase):
    config = CONFIG

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='docs-scope-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.git('init', '-q', '-b', 'main')
        self.write({'README.md': 'x', 'app/Main.java': 'class Main {}', REGISTRY_PATH: json.dumps(REGISTRY),
                    CONFIG_PATH: json.dumps(self.config)})
        self.base = self.commit('base')

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
        """The PR branch gets one commit per push ({path: text, None = delete}); main may move on; then GitHub's
        merge commit (main + PR head) is checked out."""
        self.git('checkout', '-q', '-b', 'pr', self.base)
        for files in pushes:
            self.commit('push', files)
        self.git('checkout', '-q', 'main')
        if main_after:
            self.commit('main moved', main_after)
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')

    def scope(self, event='pull_request', base=''):
        """(docs_only, jobs, stdout)."""
        out = os.path.join(self.d, '.output')
        r = subprocess.run([sys.executable, '-I', SCRIPT, '--config', CONFIG_PATH, '--event', event, '--base', base],
                           cwd=self.d, text=True, capture_output=True, env={**os.environ, **ENV, 'GITHUB_OUTPUT': out})
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(out) as f:
            written = f.read()
        os.remove(out)
        m = re.fullmatch(r'docs_only=(true|false)\njobs=(\{.*\})\nreason=([^\n]+)\n', written)
        self.assertTrue(m, written)
        self.assertEqual(r.stdout.startswith('docs-only: '), m[1] == 'true', r.stdout)
        self.assertEqual(m[3], r.stdout.splitlines()[0])
        return m[1] == 'true', json.loads(m[2]), r.stdout

    def assertDocsOnly(self, expected):
        docs, _, out = self.scope()
        self.assertEqual(docs, expected, out)
        return out


class DocsOnly(Repo):
    def test_only_md_files_is_docs_only(self):
        self.pr({'README.md': 'y', 'docs/guide.md': 'new'})
        self.assertDocsOnly(True)

    def test_md_and_code_is_a_full_run(self):
        self.pr({'README.md': 'y', 'app/Main.java': 'class Main { int x; }'})
        self.assertDocsOnly(False)

    def test_main_moving_on_with_code_does_not_count_against_the_pr(self):   # base..merge = the PR's change only
        self.pr({'README.md': 'y'}, main_after={'app/Main.java': 'class Main { int y; }'})
        self.assertDocsOnly(True)


class EveryPushCounts(Repo):
    def test_a_code_push_then_a_docs_push_is_a_full_run(self):   # never the last push alone
        self.pr({'app/Util.java': 'class Util {}'}, {'README.md': 'y'})
        self.assertDocsOnly(False)

    def test_code_added_and_removed_again_leaves_a_docs_only_change(self):   # the whole change is what merges
        self.pr({'app/Util.java': 'class Util {}'}, {'app/Util.java': None, 'README.md': 'y'})
        self.assertDocsOnly(True)

    def test_a_rename_from_java_to_md_is_a_full_run(self):   # --no-renames: the deleted .java is listed
        self.pr({'app/Main.java': None, 'app/Main.md': 'class Main {}'})
        self.assertIn('app/Main.java', self.assertDocsOnly(False))

    def test_a_code_change_main_already_has_still_counts(self):   # base..merge misses it; merge base..PR head has it
        same = {'app/Main.java': 'class Main { int same; }'}
        self.pr({**same, 'README.md': 'y'}, main_after=same)
        self.assertDocsOnly(False)


class BuildAndTestInputs(Repo):
    def check(self, path):
        self.pr({path: 'doc'})
        self.assertIn('build or test input', self.assertDocsOnly(False), path)

    def test_md_in_a_source_set(self):
        self.check('plugins/x/src/main/resources/help.md')

    def test_md_in_a_top_level_source_set(self):
        self.check('src/help.md')

    def test_md_in_a_resources_folder(self):
        self.check('net/resources/motd.md')

    def test_md_in_a_test_folder(self):
        self.check('net/testing/livelike/tests/fixture.md')

    def test_md_in_a_fixtures_folder(self):
        self.check('x/fixtures/case.md')

    def test_a_named_file_read_by_a_test(self):
        self.check('skills/review/SKILL.md')

    def test_a_named_file_read_by_a_tool(self):
        self.check('plugins/RELEASE-CLAIMS.md')

    def test_an_md_listed_in_the_registry(self):
        self.check('net/site/README.md')

    def test_an_md_newly_listed_in_the_registry_by_main(self):   # read at the base too, not only the head
        self.pr({'docs/new.md': 'doc'}, main_after={REGISTRY_PATH: json.dumps(
            {'modules': [{'id': 'docs-site', 'inputs': ['docs/']}]})})
        self.assertDocsOnly(False)

    def test_a_pr_that_drops_a_registry_input_does_not_free_it(self):   # the base's entry still counts
        self.pr({REGISTRY_PATH: json.dumps({'modules': []}), 'net/site/README.md': 'y'})
        self.assertDocsOnly(False)


class FailsClosed(Repo):
    def test_events_without_a_base_are_full_runs(self):
        self.pr({'README.md': 'y'})
        for event in ('push', 'workflow_dispatch', ''):
            docs, jobs, out = self.scope(event)
            self.assertEqual((docs, jobs), (False, {}), out)
            self.assertIn('without a base', out)

    def test_a_head_that_is_not_a_merge_commit(self):
        self.commit('docs on main', {'README.md': 'y'})
        self.assertIn('not a two-parent merge commit', self.assertDocsOnly(False))

    def test_no_merge_base(self):
        self.git('checkout', '-q', '--orphan', 'pr')
        self.git('rm', '-rqf', '.')
        self.commit('unrelated', {'README.md': 'z'})
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '--allow-unrelated-histories', '-X', 'theirs', '-m', 'merge', 'pr')
        self.assertIn('no merge base', self.assertDocsOnly(False))

    def test_an_empty_change(self):
        self.pr({})
        self.assertIn('touches no file', self.assertDocsOnly(False))

    def test_an_md_symlink(self):
        self.git('checkout', '-q', '-b', 'pr')
        os.symlink('app/Main.java', os.path.join(self.d, 'link.md'))
        self.commit('symlink')
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
        self.assertIn('symlink', self.assertDocsOnly(False))

    def test_a_registry_at_neither_side(self):
        self.git('checkout', '-q', '-b', 'pr2', self.base)
        self.git('rm', '-q', REGISTRY_PATH)
        main = self.commit('no registry on this side branch')
        self.git('checkout', '-q', '-b', 'pr3', main)
        self.commit('docs', {'README.md': 'w'})
        self.git('checkout', '-q', 'pr2')
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr3')
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('neither the base nor the head', out)

    def test_a_registry_that_does_not_parse_as_configured(self):
        self.pr({'README.md': 'y'}, main_after={REGISTRY_PATH: json.dumps({'mods': []})})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('cannot be read as configured', out)


class ConfigAtTheBase(Repo):
    """The rules come from the base commit and the code from the action's pinned folder, so a pull request cannot
    loosen what judges it (the general half of a gate that ran the PR's own copy)."""

    def test_a_pr_that_loosens_its_own_config_still_runs_in_full(self):
        self.pr({CONFIG_PATH: json.dumps({'docs': ['**'], 'jobs': {}}), 'engine/A.java': 'class A {}'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'false', 'engine': 'true'}), out)   # the base's jobs

    def test_no_config_at_the_base(self):   # the PR that adds the config runs in full
        self.git('rm', '-q', CONFIG_PATH)
        self.base = self.commit('no config')
        self.pr({'README.md': 'y', CONFIG_PATH: json.dumps(CONFIG)})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('no config', out)

    def test_a_bad_config_at_the_base(self):
        for bad in ('{', '[]', json.dumps({'docs': ['**/*.md'], 'never_doc': []}),   # a typo drops no rule quietly
                    json.dumps({'never_docs': []}), json.dumps({'docs': '**/*.md'}),
                    json.dumps({'docs': ['*.md'], 'never_docs': [{'glob': 'x'}]}),
                    json.dumps({'docs': ['*.md'], 'never_docs_from': [{'file': 'r.json', 'field': 'f'}]}),
                    json.dumps({'docs': ['*.md'], 'jobs': {'j': {'path': ['x/**']}}})):
            with self.subTest(bad=bad):
                self.git('checkout', '-q', 'main')
                self.git('reset', '-q', '--hard', self.base)
                self.git('branch', '-q', '-D', 'pr') if self.git('branch', '--list', 'pr') else None
                self.commit('bad config', {CONFIG_PATH: bad})
                self.git('checkout', '-q', '-b', 'pr')
                self.commit('docs', {'README.md': bad})
                self.git('checkout', '-q', 'main')
                self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn('config', out)


class LooseConfig(Repo):
    config = {'docs': ['**']}   # everything is docs, except the config itself

    def test_the_config_file_is_never_docs(self):
        self.pr({CONFIG_PATH: json.dumps({'docs': ['**'], 'jobs': {}})})
        self.assertIn('the docs-scope config', self.assertDocsOnly(False))


class Jobs(Repo):
    def test_a_job_runs_for_its_paths_and_skips_otherwise(self):
        self.pr({'plugins/a/src/main/java/A.java': 'class A {}'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'false'}), out)
        self.assertIn('job engine: skip', out)

    def test_docs_in_a_job_folder_do_not_run_it(self):   # code elsewhere makes it a full run, not an engine build
        self.pr({'engine/README.md': 'y', 'app/Main.java': 'class Main { int x; }'})
        self.assertEqual(self.scope()[1], {'plugins': 'false', 'engine': 'false'})

    def test_an_md_input_in_a_job_folder_runs_it(self):
        self.pr({'engine/core/src/main/resources/help.md': 'y'})
        self.assertEqual(self.scope()[1]['engine'], 'true')

    def test_docs_only_skips_every_job(self):
        self.pr({'README.md': 'y'})
        self.assertEqual(self.scope()[:2], (True, {'plugins': 'false', 'engine': 'false'}))

    def test_a_folder_added_to_a_jobs_paths_runs_it(self):   # a shared folder another job builds against
        self.pr({'plugins/items/common/Item.java': 'class Item {}'})
        self.assertEqual(self.scope()[1]['engine'], 'true')

    def test_a_jobs_paths_from_a_registry_filtered_by_kind(self):   # the registry names what a module compiles
        for path, runs in (('plugins/locale/common/src/main/java/Wire.java', 'true'),
                           ('plugins/locale/common/src/main/java/Other.java', 'false'),
                           ('plugins/travel/common/src/main/java/a/Leg.java', 'true')):
            with self.subTest(path=path):
                self.git('checkout', '-q', 'main')
                self.git('reset', '-q', '--hard', self.base)
                self.git('branch', '-q', '-D', 'pr') if self.git('branch', '--list', 'pr') else None
                self.pr({path: 'class X {}'})
                docs, jobs, out = self.scope()
                self.assertEqual(jobs['engine'], runs, out)

    def test_a_registry_entry_of_another_kind_does_not_run_the_job(self):
        self.pr({'net/site/README.md': 'y'})   # an input of a "plugin", not of an "engine-module"
        self.assertEqual(self.scope()[1]['engine'], 'false')


class OtherEvents(Repo):
    def test_a_push_with_a_base_reads_its_jobs_and_is_never_docs_only(self):
        self.commit('code', {'engine/A.java': 'class A {}'})
        docs, jobs, out = self.scope('push', self.base)
        self.assertEqual((docs, jobs), (False, {'plugins': 'false', 'engine': 'true'}), out)

    def test_a_docs_push_skips_every_job_but_is_not_docs_only(self):
        self.commit('docs', {'plugins/README.md': 'y'})
        docs, jobs, out = self.scope('push', self.base)
        self.assertEqual((docs, jobs), (False, {'plugins': 'false', 'engine': 'false'}), out)
        self.assertIn('not in docs_only_events', out)

    def test_docs_only_events_can_name_push(self):
        cfg = {**CONFIG, 'docs_only_events': ['pull_request', 'push']}
        self.base = self.commit('push may be docs-only', {CONFIG_PATH: json.dumps(cfg)})
        self.commit('docs', {'README.md': 'y'})
        self.assertTrue(self.scope('push', self.base)[0])

    def test_a_base_that_is_not_a_commit(self):
        self.commit('docs', {'README.md': 'y'})
        docs, jobs, out = self.scope('push', '0' * 40)
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('not a commit here', out)


class Globs(unittest.TestCase):
    def test_globs(self):
        sys.path.insert(0, HERE)
        try:
            from docs_scope import glob_re
        finally:
            sys.path.remove(HERE)
        for pattern, yes, no in (
                ('**/*.md', ['a.md', 'x/y/a.md'], ['a.mdx', 'a.md/b']),
                ('*.md', ['a.md'], ['x/a.md']),
                ('**/test/**', ['test/a', 'x/test/b/c'], ['x/tests/a', 'xtest/a']),
                ('src/**', ['src/a', 'src/a/b'], ['x/src/a', 'srcx/a']),
                ('a?.md', ['ab.md'], ['a/.md', 'abc.md']),
                ('a.b+c', ['a.b+c'], ['aXb+c'])):
            for p in yes:
                self.assertTrue(glob_re(pattern).match(p), (pattern, p))
            for p in no:
                self.assertFalse(glob_re(pattern).match(p), (pattern, p))


class Action(unittest.TestCase):
    """action.yml runs only this folder's script (the caller's pinned SHA), never a file from the checkout."""
    text = open(ACTION).read()

    def test_runs_only_its_own_script(self):
        runs = re.findall(r'^\s*run: (.*)$', self.text, re.M)
        self.assertEqual(runs, ['python3 -I "$GITHUB_ACTION_PATH/docs_scope.py" --config "$CONFIG" '
                                '--event "$EVENT" --base "$BASE"'])
        self.assertNotIn('uses:', self.text)
        self.assertIn('using: composite', self.text)

    def test_inputs_reach_the_script_by_env_only(self):   # never ${{ }} inside run: (script injection)
        run = re.search(r'^\s*run: (.*)$', self.text, re.M)[1]
        self.assertNotIn('${{', run)

    def test_declares_every_output_the_script_writes(self):
        for name in ('docs_only', 'jobs', 'reason'):
            self.assertIn(f'value: ${{{{ steps.scope.outputs.{name} }}}}', self.text)


if __name__ == '__main__':
    unittest.main(verbosity=1)
