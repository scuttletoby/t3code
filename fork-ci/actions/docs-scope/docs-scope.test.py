"""docs_scope.py: a docs-only change builds and tests nothing; jobs run only for the files that reach them.
Real git repos: GitHub's refs/pull/N/merge is a merge commit (HEAD^1 = the base, HEAD^2 = the PR head), built here
with `git merge --no-ff`. Anything unclear must mean a full run.
Run: python3 fork-ci/actions/docs-scope/docs-scope.test.py
"""
import contextlib, json, os, re, shutil, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True   # the Globs test imports docs_scope.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'docs_scope.py')
ACTION = os.path.join(HERE, 'action.yml')
README = os.path.join(HERE, 'README.md')
CONFIG_PATH = '.github/docs-scope.json'
ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
REGISTRY_PATH = 'tools/registry.json'
REGISTRY = {'modules': [
    {'id': 'site', 'kind': 'plugin', 'inputs': ['plugins/site/README.md']},
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
        for path, text in files.items():   # a bytes path or text is written as those bytes
            full = os.path.join(os.fsencode(self.d), os.fsencode(path))
            if text is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'wb') as f:
                f.write(text if isinstance(text, bytes) else text.encode())

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

    def scope(self, event='pull_request', base='', config=CONFIG_PATH, sha=None, head_sha=None):
        """(docs_only, jobs, stdout). The runner's view: $GITHUB_SHA is HEAD and the event's pull_request.head.sha
        is HEAD^2 (HEAD for a commit that is not a merge), unless the test says otherwise ('' = unset)."""
        out, event_path = os.path.join(self.d, '.output'), os.path.join(self.d, '.event.json')
        head = self.git('rev-parse', 'HEAD')
        parents = self.git('rev-list', '--parents', '-n', '1', 'HEAD').split()
        head_sha = (parents[2] if len(parents) == 3 else head) if head_sha is None else head_sha
        env = {**os.environ, **ENV, 'GITHUB_OUTPUT': out, 'GITHUB_SHA': head if sha is None else sha}
        if head_sha:
            with open(event_path, 'w') as f:
                json.dump({'pull_request': {'head': {'sha': head_sha}}}, f)
            env['GITHUB_EVENT_PATH'] = event_path
        r = subprocess.run([sys.executable, '-I', SCRIPT, '--config', config, '--event', event, '--base', base],
                           cwd=self.d, capture_output=True, env=env)
        stdout = r.stdout.decode()   # strict: the script prints UTF-8 only, whatever the file names hold
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        with open(out, 'rb') as f:
            written = f.read().decode()
        for f in (out, event_path):
            os.remove(f) if os.path.exists(f) else None
        m = re.fullmatch(r'docs_only=(true|false)\njobs=(\{.*\})\nreason=([^\n]+)\n', written)
        self.assertTrue(m, written)
        self.assertEqual(stdout.startswith('docs-only: '), m[1] == 'true', stdout)
        self.assertEqual(m[3], stdout.splitlines()[0])
        for line in stdout.splitlines():   # a file name never starts a line: no ::workflow-command::
            self.assertRegex(line, r'^(docs-only|full run|scoped run|not docs-only|job [^:]+):', stdout)
            self.assertTrue(line.isprintable(), repr(line))
        return m[1] == 'true', json.loads(m[2]), stdout

    def reset(self):
        self.git('checkout', '-q', '-f', 'main')
        self.git('reset', '-q', '--hard', self.base)
        for b in ('pr', 'pr-real'):
            self.git('branch', '-q', '-D', b) if self.git('branch', '--list', b) else None

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
        self.check('plugins/site/README.md')

    def test_an_md_newly_listed_in_the_registry_by_main(self):   # read at the base too, not only the head
        self.pr({'docs/new.md': 'doc'}, main_after={REGISTRY_PATH: json.dumps(
            {'modules': REGISTRY['modules'] + [{'id': 'docs-site', 'inputs': ['docs/']}]})})
        self.assertIn('docs-site inputs: docs', self.assertDocsOnly(False))


class RegistryAtTheBase(Repo):
    """A pull request cannot free a file by deleting its entry: the base's entries count. Here the registry change
    itself does not decide (it is docs, or a job's own path), so only the registry read can (an entry the PR adds
    counts too: never_docs_from is read at the head as well)."""
    config = {'docs': ['**'], 'never_docs_from': CONFIG['never_docs_from'],
              'jobs': {'plugins': {'paths': ['plugins/**', 'tools/**']}, 'engine': CONFIG['jobs']['engine']}}

    def drop(self, module_id):
        return json.dumps({'modules': [m for m in REGISTRY['modules'] if m['id'] != module_id]})

    def test_a_pr_that_drops_a_never_docs_entry_does_not_free_the_file(self):
        self.pr({REGISTRY_PATH: self.drop('site'), 'plugins/site/README.md': 'y'})
        docs, _, out = self.scope()
        self.assertFalse(docs, out)
        self.assertIn('site inputs: plugins/site/README.md', out)

    def test_a_pr_that_drops_a_jobs_entry_still_runs_the_job(self):
        self.pr({REGISTRY_PATH: self.drop('content-locale'), 'plugins/locale/common/src/main/java/Wire.java': 'x'})
        self.assertEqual(self.scope()[1], {'plugins': 'true', 'engine': 'true'})

    def test_a_pr_that_adds_a_never_docs_entry_counts_it_at_the_head(self):   # never_docs_from read at both sides
        self.pr({REGISTRY_PATH: json.dumps({'modules': REGISTRY['modules'] + [{'id': 'manual',
                                                                                'inputs': ['docs/manual.md']}]}),
                 'docs/manual.md': 'y'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'true'}), out)
        self.assertIn('manual inputs: docs/manual.md', out)


class RegistryAtTheHead(Repo):
    """The PR's own registry entries may add a job, never cancel "a file no job names runs every job": the rules that
    judge a PR come from the base. The registry sits in a job's paths (the common case), so its change alone does not
    run every job."""
    config = {**CONFIG, 'jobs': {'plugins': {'paths': ['plugins/**']},
                                 'engine': {**CONFIG['jobs']['engine'], 'paths': ['engine/**', 'tools/**']}}}

    def add(self, *modules):
        return json.dumps({'modules': REGISTRY['modules'] + list(modules)})

    def test_a_head_only_entry_cannot_claim_a_file_no_job_names(self):   # even innocently: a new module lists it
        self.pr({REGISTRY_PATH: self.add({'id': 'root', 'kind': 'engine-module', 'inputs': ['build.gradle']}),
                 'build.gradle': 'x'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'true'}), out)
        self.assertIn('full run: build.gradle is not a docs file', out.splitlines()[0])
        self.assertIn('which no job names at the base: every job runs', out.splitlines()[0])

    def test_a_head_only_entry_still_adds_its_job(self):
        self.pr({REGISTRY_PATH: self.add({'id': 'new', 'kind': 'engine-module', 'inputs': ['plugins/new/X.java']}),
                 'plugins/new/X.java': 'x'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'true'}), out)
        self.assertIn('job engine: run (plugins/new/X.java matches new inputs', out)


class Submodules(Repo):
    """A gitlink bump is never docs, whatever .gitmodules (at the base, so in the checkout) or the runner's git config
    says about ignoring submodules."""

    def setUp(self):
        super().setUp()
        os.makedirs(os.path.join(self.d, 'lib'))   # an uninitialised submodule: an empty folder
        self.write({'.gitmodules': '[submodule "lib"]\n\tpath = lib\n\turl = ./lib\n\tignore = all\n'})
        self.git('update-index', '--add', '--cacheinfo', f'160000,{"1" * 40},lib')
        self.base = self.commit('a submodule that .gitmodules ignores')

    def bump(self):
        self.git('checkout', '-q', '-b', 'pr', self.base)
        self.git('update-index', '--cacheinfo', f'160000,{"2" * 40},lib')
        self.commit('bump lib', {'README.md': 'y'})
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')

    def test_a_submodule_bump_with_ignore_all_in_gitmodules(self):
        self.bump()
        self.assertIn('lib is a symlink or submodule', self.assertDocsOnly(False))

    def test_a_submodule_bump_with_diff_ignore_submodules_in_the_git_config(self):
        self.write({'.gitmodules': None})   # only the runner's config hides it now (the base still has .gitmodules)
        self.git('rm', '-q', '--cached', '.gitmodules')
        self.base = self.commit('no .gitmodules')
        self.git('config', 'diff.ignoreSubmodules', 'all')
        self.bump()
        self.assertIn('lib is a symlink or submodule', self.assertDocsOnly(False))


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

    def test_registry_entries_that_could_never_match_are_a_full_run(self):   # never fail open on a list
        site = {'id': 'site', 'kind': 'plugin', 'inputs': ['plugins/site/README.md']}
        for name, modules in (
                ('a renamed field', [{**site, 'inputs': None, 'input': ['plugins/site/README.md']}]),
                ('an object without the field', [site, {'id': 'other', 'kind': 'plugin'}]),
                ('./', [{**site, 'inputs': ['./plugins/site/README.md']}]),
                ('a leading /', [{**site, 'inputs': ['/plugins/site/README.md']}]),
                ('..', [{**site, 'inputs': ['plugins/../plugins/site/README.md']}]),
                ('a wildcard', [{**site, 'inputs': ['plugins/**/*.md']}]),
                ('a number', [{**site, 'inputs': [5]}]),
                ('no object of the kind a job keeps', None)):
            with self.subTest(name):
                self.reset()
                engine = [m for m in REGISTRY['modules'] if m['kind'] == 'engine-module'] if modules else []
                modules = [{k: v for k, v in m.items() if v is not None} for m in modules or [site]] + engine
                self.base = self.commit('registry', {REGISTRY_PATH: json.dumps({'modules': modules})})
                self.pr({'plugins/site/README.md': 'y'})
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn('cannot be read as configured', out)


class MergeCommit(Repo):
    """On pull_request, HEAD must be GitHub's merge commit before HEAD^1 is trusted as the base: a checkout of a PR
    head whose last commit merged main in would make the PR's code vanish from both diffs and read the config from
    the PR."""

    def setUp(self):
        super().setUp()
        self.git('checkout', '-q', '-b', 'pr', self.base)
        self.commit('code + a looser config', {'app/Main.java': 'class Main { int x; }',
                                               CONFIG_PATH: json.dumps({'docs': ['**']})})
        self.git('checkout', '-q', 'main')
        self.commit('docs on main', {'README.md': 'y'})
        self.git('checkout', '-q', 'pr')
        self.git('merge', '-q', '--no-ff', '-m', 'merge main into pr', 'main')
        self.pr_head = self.git('rev-parse', 'HEAD')
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
        self.github_sha = self.git('rev-parse', 'HEAD')   # what GitHub's refs/pull/N/merge would hold
        self.git('checkout', '-q', '--detach', self.pr_head)   # but the caller checked out the PR head

    def test_the_real_merge_commit_is_a_full_run(self):   # the check does not refuse the right checkout
        self.git('checkout', '-q', '--detach', self.github_sha)
        self.assertEqual(self.scope(head_sha=self.pr_head)[:2], (False, {'plugins': 'true', 'engine': 'true'}))

    def test_a_pr_head_checkout_is_a_full_run(self):
        docs, jobs, out = self.scope(sha=self.github_sha, head_sha=self.pr_head)
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('is not the commit this run is for', out)

    def test_a_merge_whose_second_parent_is_not_the_pr_head_is_a_full_run(self):
        docs, jobs, out = self.scope(sha=self.pr_head, head_sha=self.pr_head)
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn("not the event's pull_request.head.sha", out)

    def test_no_github_sha_or_event_is_a_full_run(self):
        self.git('checkout', '-q', '--detach', self.github_sha)
        for sha, head_sha, why in (('', self.pr_head, 'unset'), (None, '', 'event file')):
            with self.subTest(why):
                docs, jobs, out = self.scope(sha=sha, head_sha=head_sha)
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn(why, out)

    def test_a_push_run_checks_github_sha_too(self):
        self.git('checkout', '-q', 'main')
        docs, jobs, out = self.scope('push', self.base, sha=self.pr_head)
        self.assertEqual((docs, jobs), (False, {}), out)


class NeverCrashes(Repo):
    """Every error is a full run with outputs. A crash leaves no outputs: a `needs:` job that is skipped reads as
    passing on a required check."""

    def test_a_file_name_that_is_not_utf8(self):
        self.pr({b'docs/\xff.md': 'x'})
        docs, jobs, out = self.scope()
        self.assertFalse(docs, out)
        self.assertIn('not UTF-8', out)
        self.assertIn('docs/\\udcff.md', out)

    def test_a_registry_at_the_head_that_is_not_utf8_or_too_deep(self):
        for name, text in (('not utf-8', b'\xff'), ('too deep', '[' * 200000)):
            with self.subTest(name):
                self.reset()
                self.pr({REGISTRY_PATH: text, 'README.md': 'y'})
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn('cannot be read as configured', out)

    def test_an_error_nobody_foresaw(self):   # the last net: main() turns any exception into a full run
        sys.path.insert(0, HERE)
        try:
            import docs_scope
        finally:
            sys.path.remove(HERE)
        out = os.path.join(self.d, '.output')

        def broken(*a):
            raise RuntimeError('boom')
        old = os.environ.get('GITHUB_OUTPUT')
        os.environ['GITHUB_OUTPUT'] = out
        try:
            with open(os.devnull, 'w') as devnull, contextlib.redirect_stdout(devnull):
                self.assertEqual(docs_scope.main(['--config', CONFIG_PATH, '--event', 'pull_request'], run=broken), 0)
        finally:
            os.environ.pop('GITHUB_OUTPUT') if old is None else os.environ.__setitem__('GITHUB_OUTPUT', old)
        with open(out) as f:
            self.assertEqual(f.read(), 'docs_only=false\njobs={}\nreason=full run: docs-scope failed '
                                       '(RuntimeError: boom)\n')


class OddNames(Repo):
    def test_a_newline_name_in_a_source_set_is_not_docs(self):
        self.pr({'p/src/main/resources/he\nlp.md': 'x'})
        self.assertDocsOnly(False)

    def test_a_newline_name_in_a_job_folder_runs_that_job_by_its_paths(self):
        self.pr({'plugins/x\ny.java': 'x'})
        docs, jobs, out = self.scope()
        self.assertEqual(jobs, {'plugins': 'true', 'engine': 'false'}, out)
        self.assertIn('matches its paths', out)

    def test_a_name_cannot_inject_a_workflow_command_or_an_output(self):   # scope() checks every line
        self.pr({'a\ndocs_only=true\njobs={}\n::warning::injected\n.java': 'x', 'README.md': 'y'})
        docs, _, out = self.scope()
        self.assertFalse(docs, out)
        self.assertIn('a\\ndocs_only=true', out)


class ConfigAtTheBase(Repo):
    """The rules come from the base commit and the code from the action's pinned folder, so a pull request cannot
    loosen what judges it (the general half of a gate that ran the PR's own copy)."""

    def test_a_pr_that_loosens_its_own_config_still_runs_in_full(self):
        self.pr({CONFIG_PATH: json.dumps({'docs': ['**'], 'jobs': {}}), 'engine/A.java': 'class A {}'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'true'}), out)   # the base's jobs, all

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
                    json.dumps({'docs': ['*.md'], 'jobs': {'j': {'path': ['x/**']}}}),
                    json.dumps({'docs': ['*.md'], 'never_docs': [{'glob': 5, 'why': 'x'}]})):   # was a crash
            with self.subTest(bad=bad):
                self.reset()
                self.commit('bad config', {CONFIG_PATH: bad})
                self.git('checkout', '-q', '-b', 'pr')
                self.commit('docs', {'README.md': bad})
                self.git('checkout', '-q', 'main')
                self.git('merge', '-q', '--no-ff', '-m', 'merge', 'pr')
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn('config', out)


    def test_a_bad_source_entry_names_itself(self):   # wrong types and typos in a *_from entry: a full run, why
        source = CONFIG['never_docs_from'][0]
        for bad, why in (({'field': 5}, 'needs a plain file path'), ({'field': []}, 'needs a plain file path'),
                         ({'where': ['kind']}, 'needs a plain file path'), ({'label': 3}, 'needs a plain file path'),
                         ({'list': None}, 'needs a plain file path'), ({'file': './' + REGISTRY_PATH}, 'needs a plain'),
                         ({'field': ['inputs', 'inptus']}, "no kept object has the field(s) ['inptus']"),
                         ({'where': {'kind': 'plugn'}}, "no kept object has the field(s) ['inputs']"),
                         ({'wehre': {'kind': 'plugin'}}, 'no other keys than')):   # a typo key drops no filter
            with self.subTest(bad=bad):
                self.reset()
                self.commit('bad source', {CONFIG_PATH: json.dumps({**CONFIG, 'never_docs_from': [{**source, **bad}]})})
                self.pr({'README.md': 'y'})
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn(why, out)

    def test_an_unknown_key_in_a_paths_from_entry(self):   # "wehre" would drop the kind filter: the job runs more
        engine = CONFIG['jobs']['engine']
        source = {**{k: v for k, v in engine['paths_from'][0].items() if k != 'where'}, 'wehre': {'kind': 'x'}}
        self.commit('typo', {CONFIG_PATH: json.dumps(
            {**CONFIG, 'jobs': {**CONFIG['jobs'], 'engine': {**engine, 'paths_from': [source]}}})})
        self.pr({'README.md': 'y'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('jobs.engine.paths_from entry', out)


class LooseConfig(Repo):
    config = {'docs': ['**']}   # everything is docs, except the config itself

    def test_the_config_file_is_never_docs(self):
        self.pr({CONFIG_PATH: json.dumps({'docs': ['**'], 'jobs': {}})})
        self.assertIn('the docs-scope config', self.assertDocsOnly(False))

    def test_a_config_path_that_is_not_plain_is_a_full_run(self):   # ./x would dodge "the config is never docs"
        self.pr({CONFIG_PATH: json.dumps({'docs': ['**'], 'jobs': {}})})
        for config in ('./' + CONFIG_PATH, '.github//docs-scope.json', '/' + CONFIG_PATH):
            with self.subTest(config):
                docs, jobs, out = self.scope(config=config)
                self.assertEqual((docs, jobs), (False, {}), out)
                self.assertIn('not a plain repository path', out)


class Jobs(Repo):
    def test_a_job_runs_for_its_paths_and_skips_otherwise(self):
        self.pr({'plugins/a/src/main/java/A.java': 'class A {}'})
        docs, jobs, out = self.scope()
        self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'false'}), out)
        self.assertIn('job engine: skip', out)

    def test_docs_in_a_job_folder_do_not_run_it(self):   # code elsewhere makes it a full run, not an engine build
        self.pr({'engine/README.md': 'y', 'plugins/a/A.java': 'class A {}'})
        self.assertEqual(self.scope()[1], {'plugins': 'true', 'engine': 'false'})

    def test_a_file_no_job_names_runs_every_job(self):   # unclear which job it reaches: all of them
        for path in ('app/Main.java', '.github/workflows/ci.yml', CONFIG_PATH):
            with self.subTest(path):
                self.reset()
                self.pr({path: json.dumps(CONFIG) + ' '})
                docs, jobs, out = self.scope()
                self.assertEqual((docs, jobs), (False, {'plugins': 'true', 'engine': 'true'}), out)
                self.assertIn('every job runs', out.splitlines()[0])

    def test_a_config_change_runs_every_job_even_if_a_job_names_it(self):
        cfg = {**CONFIG, 'jobs': {**CONFIG['jobs'], 'plugins': {'paths': ['plugins/**', '.github/**']}}}
        self.base = self.commit('a job names .github', {CONFIG_PATH: json.dumps(cfg)})
        self.pr({CONFIG_PATH: json.dumps(cfg) + ' '})
        self.assertEqual(self.scope()[1], {'plugins': 'true', 'engine': 'true'})

    def test_an_md_input_in_a_job_folder_runs_it(self):
        self.pr({'engine/core/src/main/resources/help.md': 'y'})
        self.assertEqual(self.scope()[1]['engine'], 'true')

    def test_a_scoped_run_says_so_and_a_full_run_says_full(self):   # the reason matches what the jobs do
        self.pr({'engine/A.java': 'class A {}'})
        docs, jobs, out = self.scope()
        self.assertEqual(jobs, {'plugins': 'false', 'engine': 'true'}, out)
        self.assertTrue(out.startswith('scoped run: engine/A.java is not a docs file; only the jobs'), out)
        self.reset()
        self.pr({'engine/A.java': 'class A {}', 'app/Main.java': 'x'})
        docs, jobs, out = self.scope()
        self.assertEqual(jobs, {'plugins': 'true', 'engine': 'true'}, out)
        self.assertTrue(out.startswith('full run: app/Main.java is not a docs file and 1 more, which no job'), out)

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
                self.reset()
                self.pr({path: 'class X {}'})
                docs, jobs, out = self.scope()
                self.assertEqual(jobs['engine'], runs, out)

    def test_a_registry_entry_of_another_kind_does_not_run_the_job(self):
        self.pr({'plugins/site/README.md': 'y'})   # an input of a "plugin", not of an "engine-module"
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

    def test_a_base_off_the_first_parent_line_is_a_full_run(self):   # a merged fork PR's head was never tested
        self.commit('code A (its run went red)', {'engine/A.java': 'class A {}'})
        self.git('checkout', '-q', '-b', 'fork-main')
        fork_pr_head = self.commit('docs (its pull_request run passed)', {'README.md': 'y'})
        self.git('checkout', '-q', 'main')
        self.git('merge', '-q', '--no-ff', '-m', 'merge the fork PR', 'fork-main')
        self.commit('docs push', {'docs/a.md': 'z'})
        docs, jobs, out = self.scope('push', fork_pr_head)
        self.assertEqual((docs, jobs), (False, {}), out)
        self.assertIn('not on the first-parent line', out)
        self.assertEqual(self.scope('push', self.base)[1], {'plugins': 'false', 'engine': 'true'})   # main's green

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
                ('a.b+c', ['a.b+c'], ['aXb+c']),
                ('**/src/**', ['p/src/a\nb.md'], ['p/s\nrc/a']),   # ** crosses a newline in a name
                ('app/**', ['app/x\ny.java'], ['ap\np/x'])):
            for p in yes:
                self.assertTrue(glob_re(pattern).match(p), (pattern, p))
            for p in no:
                self.assertFalse(glob_re(pattern).match(p), (pattern, p))


class Readme(unittest.TestCase):
    def test_the_last_green_query_takes_push_runs_only(self):   # `branch` matches a fork PR's head branch "main" too
        with open(README) as f:
            queries = re.findall(r'/runs\?([^"\s]*)', f.read())
        self.assertTrue(queries)
        for q in queries:
            self.assertLessEqual({'branch=main', 'status=success', 'event=push'}, set(q.split('&')), q)


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
