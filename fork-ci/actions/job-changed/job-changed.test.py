"""job_changed.py and its action: a workflow change counts for a job only when its parsed block, a job it needs or the
workflow-level on/env/defaults/permissions changed; anything unreadable or unclear means changed.
Ported from a project's tests of the same rule. Every case runs the action's real `run:` text with bash in a real git
repo (inputs passed as the action passes them, by env), the way the runner does, and reads $GITHUB_OUTPUT.
Run: python3 fork-ci/actions/job-changed/job-changed.test.py
"""
import contextlib, io, os, re, shutil, subprocess, sys, tempfile, unittest, unittest.mock

import yaml

sys.dont_write_bytecode = True   # the tests import job_changed.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, '..', '..', '..'))
ACTION = os.path.join(HERE, 'action.yml')
sys.path.insert(0, HERE)
import job_changed   # noqa: E402

# A caller's GIT_DIR, GIT_WORK_TREE, GIT_INDEX_FILE ... would point every git call here (and the step's) at another repo.
for _k in [k for k in os.environ if k.startswith('GIT_')]:
    del os.environ[_k]

ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
       'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
JOBS = ('build', 'plugins')
CI = '.github/workflows/ci.yml'
# A workflow with a route job the two heavy jobs need, and a job they do not need.
FIXTURE = """# a workflow
name: ci
on:
  pull_request: { branches: [main] }
concurrency: { group: ci-x }
permissions: { contents: read }
env:
  BASE: x
jobs:
  route:
    runs-on: [self-hosted, light]
    outputs: { plugins: "${{ steps.r.outputs.plugins }}" }
    steps:
      - run: python3 route.py
  build:
    needs: route
    runs-on: [self-hosted, build]
    steps:
      - run: ./gradlew build
  plugins:
    needs: [route]
    runs-on: [self-hosted, build]
    steps: &steps
      - name: build
        run: ./gradlew build ciJars
  tooling:
    runs-on: [self-hosted, build]
    steps:
      - run: python3 tests.py
"""
NONE, BOTH = {'build': 'false', 'plugins': 'false'}, {'build': 'true', 'plugins': 'true'}


def step():
    """(env, run) of the action's one step."""
    with open(ACTION) as f:
        steps = yaml.safe_load(f)['runs']['steps']
    assert len(steps) == 1
    return steps[0]['env'], steps[0]['run']


class Repo(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='job-changed-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.git('init', '-q', '-b', 'main')
        self.base = self.commit({CI: FIXTURE, 'src/A.java': 'class A {}', 'docs/README.md': 'a'})

    def git(self, *a):
        r = subprocess.run(['git', *a], cwd=self.d, text=True, capture_output=True, env={**os.environ, **ENV})
        if r.returncode:
            raise RuntimeError(f'git {a}: {r.stderr}')
        return r.stdout.strip()

    def commit(self, files):
        for path, text in files.items():
            full = os.path.join(self.d, path)
            if text is None:
                os.remove(full)
                continue
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with open(full, 'w') as f:
                f.write(text)
        self.git('add', '-A')
        self.git('commit', '-q', '--allow-empty', '-m', 'c')
        return self.git('rev-parse', 'HEAD')

    def edit(self, old, new):
        assert old in FIXTURE, old
        self.commit({CI: FIXTURE.replace(old, new, 1)})

    def outputs(self, job, base=None, **inputs):
        """{name: value} the action writes to $GITHUB_OUTPUT, its run text run with bash -e as the runner runs it."""
        out = os.path.join(self.d, '.output')
        open(out, 'w').close()
        env, run = step()
        given = {'base': self.base if base is None else base, 'head': 'HEAD', 'workflow': CI, 'job': job, 'paths': '',
                 **inputs}
        values = {k: given[re.fullmatch(r'\$\{\{ inputs\.(\w+) \}\}', v)[1]] for k, v in env.items()}
        r = subprocess.run(['bash', '--noprofile', '--norc', '-e', '-c', run], cwd=self.d, text=True,
                           capture_output=True, env={**os.environ, **ENV, **values, 'GITHUB_OUTPUT': out,
                                                     'GITHUB_ACTION_PATH': HERE})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.log = r.stdout
        with open(out) as f:
            return dict(line.split('=', 1) for line in f.read().splitlines() if '=' in line)

    def runs(self, job, base=None, **inputs):
        return self.outputs(job, base, **inputs).get('changed')

    def builds(self):
        return {j: self.runs(j) for j in JOBS}


class Workflow(Repo):
    def test_a_comment_or_blank_line_builds_nothing(self):
        self.edit('# a workflow\n', '# a workflow, now explained\n\n# more\n')
        self.assertEqual(self.builds(), NONE)

    def test_the_same_file_builds_nothing(self):
        self.commit({'src/A.java': 'class A { int x; }'})
        self.assertEqual(self.outputs('plugins'), {'changed': 'false', 'workflow': 'false',
                                                   'reason': f'unchanged: {CI} is the same at both commits'})

    def test_a_job_they_do_not_need_builds_nothing(self):
        self.edit('python3 tests.py', 'python3 tests.py --fast')
        self.assertEqual(self.builds(), NONE)
        self.assertEqual(self.runs('tooling'), 'true')

    def test_a_pr_that_only_deletes_another_jobs_lines_builds_nothing(self):
        self.edit('  tooling:\n    runs-on: [self-hosted, build]\n    steps:\n      - run: python3 tests.py\n', '')
        self.assertEqual(self.builds(), NONE)

    def test_quoting_flow_style_and_key_order_build_nothing(self):
        self.edit('    needs: [route]\n    runs-on: [self-hosted, build]\n',
                  '    runs-on:\n      - "self-hosted"\n      - build\n    needs:\n      - route\n')
        self.assertEqual(self.builds(), NONE)

    def test_name_and_concurrency_build_nothing(self):
        self.commit({CI: FIXTURE.replace('name: ci', 'name: CI').replace('group: ci-x', 'group: ci-y')})
        self.assertEqual(self.builds(), NONE)

    def test_its_own_block_builds_only_that_job(self):
        self.edit('ciJars', 'ciJars productionBuild')
        self.assertEqual(self.builds(), {'build': 'false', 'plugins': 'true'})
        self.assertIn('job plugins changed', self.outputs('plugins')['reason'])
        self.edit('    runs-on: [self-hosted, build]\n    steps:\n      - run: ./gradlew build\n',
                  '    runs-on: [self-hosted, pc]\n    steps:\n      - run: ./gradlew build\n')
        self.assertEqual(self.builds(), {'build': 'true', 'plugins': 'false'})

    def test_the_route_job_they_need_builds_both(self):
        self.edit('python3 route.py', 'python3 route.py --pc')
        self.assertEqual(self.builds(), BOTH)
        self.assertIn('job route (which plugins needs) changed', self.outputs('plugins')['reason'])

    def test_a_job_needed_indirectly_builds(self):
        self.commit({CI: FIXTURE.replace('  route:\n', '  setup:\n    runs-on: x\n    steps: [{ run: a }]\n'
                                         '  route:\n    needs: setup\n', 1)})
        self.base = self.git('rev-parse', 'HEAD')
        self.commit({CI: FIXTURE.replace('  route:\n', '  setup:\n    runs-on: x\n    steps: [{ run: b }]\n'
                                         '  route:\n    needs: setup\n', 1)})
        self.assertEqual(self.builds(), BOTH)
        self.assertEqual(self.runs('tooling'), 'false')

    def test_workflow_env_permissions_and_on_build_both(self):
        for old, new in (('BASE: x', 'BASE: y'), ('contents: read', 'contents: write'),
                         ('branches: [main]', 'branches: [main, next]'),
                         ('env:\n  BASE: x\n', 'defaults: { run: { shell: sh } }\nenv:\n  BASE: x\n')):
            with self.subTest(new=new):
                self.edit(old, new)
                self.assertEqual(self.builds(), BOTH)

    def test_an_unknown_top_level_key_builds_both(self):
        self.edit('name: ci\n', 'name: ci\nsomething-new: { x: 1 }\n')
        self.assertEqual(self.builds(), BOTH)

    def test_a_parse_error_builds_both(self):
        self.edit('    steps: &steps\n', '    steps: &steps\n   bad: [\n')
        self.assertEqual(self.builds(), BOTH)

    def test_not_a_workflow_builds_both(self):
        for text in ('just text\n', '- a list\n', 'jobs: [a]\n', ''):
            with self.subTest(text=text):
                self.commit({CI: text})
                self.assertEqual(self.builds(), BOTH)

    def test_an_alias_without_its_anchor_builds_both(self):
        self.edit('    steps:\n      - run: python3 tests.py\n', '    steps: *nowhere\n')
        self.assertEqual(self.builds(), BOTH)

    def test_an_anchor_edit_in_the_job_that_defines_it_builds_that_job(self):
        self.edit('    steps:\n      - run: python3 tests.py\n', '    steps: *steps\n')   # tooling uses plugins' steps
        self.assertEqual(self.builds(), NONE)
        self.edit('    steps: &steps\n      - name: build\n', '    steps: &steps\n      - name: build all\n')
        self.assertEqual(self.builds(), {'build': 'false', 'plugins': 'true'})

    def test_an_anchor_change_reaches_the_job_that_uses_it(self):
        # The anchor lives in a job neither heavy job needs (lint); plugins only uses it through an alias.
        lint = ('  lint:\n    runs-on: [self-hosted, light]\n    env: &gradle { GRADLE_OPTS: "-Xmx2g" }\n'
                '    steps:\n      - run: lint\n  plugins:\n    env: *gradle\n')
        self.base = self.commit({CI: FIXTURE.replace('  plugins:\n', lint, 1)})
        self.commit({CI: FIXTURE.replace('  plugins:\n', lint.replace('-Xmx2g', '-Xmx4g'), 1)})
        self.assertEqual(self.builds(), {'build': 'false', 'plugins': 'true'})
        self.base = self.git('rev-parse', 'HEAD')   # the lint job's own steps reach no heavy job
        lint_v = lint.replace('-Xmx2g', '-Xmx4g').replace('run: lint', 'run: lint -v')
        self.commit({CI: FIXTURE.replace('  plugins:\n', lint_v, 1)})
        self.assertEqual(self.builds(), NONE)

    def test_yaml_1_1_values_python_calls_equal_still_build(self):
        # safe_load reads 1 and true (and yes), 0 and false, 0755 and 493 as equal Python values; the runner does not.
        for old, new in (('1', 'true'), ('yes', 'true'), ('0', 'false'), ('0755', '493')):
            with self.subTest(old=old, new=new):
                with_env = lambda v: FIXTURE.replace('ciJars\n', f'ciJars\n        env: {{ MODE: {v} }}\n', 1)
                self.base = self.commit({CI: with_env(old)})
                self.commit({CI: with_env(new)})
                self.assertEqual(self.builds(), {'build': 'false', 'plugins': 'true'})

    def test_a_quote_that_changes_the_type_builds(self):
        # Same text, another type for the runner: unquoted 3.10 is the number 3.1 (setup-python's README warns about it),
        # 1 an int, true a bool, null nothing. Quoting makes each a string, so the job can see a different value.
        for old, new in (('[3.10]', "['3.10']"), ('1', "'1'"), ('true', "'true'"), ('010', '"010"'), ('0x10', "'0x10'"),
                         ('1.0', "'1.0'"), ('null', "'null'"), ('3.10', '3.1'), ('1', '1.0')):
            with self.subTest(old=old, new=new):
                with_env = lambda v: FIXTURE.replace('ciJars\n', f'ciJars\n        env: {{ PY: {v} }}\n', 1)
                self.base = self.commit({CI: with_env(old)})
                self.commit({CI: with_env(new)})
                self.assertEqual(self.builds(), {'build': 'false', 'plugins': 'true'})

    def test_requoting_a_string_comments_and_key_order_build_nothing(self):
        # The same type and the same text: 'x' -> "x" -> x for a string, a comment, a blank line, swapped keys.
        doc = lambda v, tail: FIXTURE.replace('ciJars\n', f'ciJars\n        env: {v}\n', 1) + tail
        self.base = self.commit({CI: doc("{ A: 'x', B: '3.10', C: 'true' }", '')})
        for v, tail in (('{ A: "x", B: "3.10", C: "true" }', ''), ('{ C: "true", A: x, B: "3.10" }', '# a note\n\n'),
                        ("\n          B: '3.10' # py\n\n          A: x\n          C: 'true'", '')):
            with self.subTest(v=v):
                self.commit({CI: doc(v, tail)})
                self.assertEqual(self.builds(), NONE)

    def test_a_side_only_the_typed_view_cannot_read_builds(self):
        # BaseLoader reads 2026-13-45, !custom and !!int x as text; the typed view cannot build them. The edit is in the
        # tooling job, which the heavy jobs do not need, so only the fail-closed rule makes them build.
        for bad in ('2026-13-45', '!custom x', '!!int x'):
            with self.subTest(bad=bad):
                self.edit('python3 tests.py', f'python3 tests.py\n        env: {{ D: {bad} }}')
                self.assertEqual(self.builds(), BOTH)
                self.base = self.git('rev-parse', 'HEAD')

    def test_a_renamed_or_missing_job_builds(self):
        self.edit('  plugins:\n', '  plugins-new:\n')
        self.assertEqual(self.runs('plugins'), 'true')
        self.edit('  route:\n', '  router:\n')   # a job they need is gone
        self.assertEqual(self.builds(), BOTH)

    def test_a_job_name_the_workflow_never_had_builds_when_it_changes(self):   # a typo in the `job` input
        self.edit('python3 tests.py', 'python3 tests.py -v')
        self.assertEqual(self.runs('plugin'), 'true')
        self.assertIn('missing on one side', self.outputs('plugin')['reason'])

    def test_a_new_or_deleted_workflow_builds(self):
        self.commit({CI: None})
        self.assertEqual(self.builds(), BOTH)
        self.base = self.git('rev-parse', 'HEAD')
        self.commit({CI: FIXTURE})
        self.assertEqual(self.builds(), BOTH)

    def test_a_wrong_workflow_path_builds(self):   # never "the file did not change": it is in neither commit
        self.commit({'src/A.java': 'class A { int x; }'})
        for wf in ('.github/workflows/c.yml', '/etc/passwd', '../ci.yml', ''):
            with self.subTest(wf=wf):
                self.assertEqual(self.runs('plugins', workflow=wf), 'true')

    def test_a_dot_slash_workflow_path_is_the_same_file(self):
        self.edit('python3 tests.py', 'python3 tests.py -v')
        self.assertEqual(self.runs('plugins', workflow='./' + CI), 'false')
        self.assertEqual(self.runs('plugins', workflow='./' + CI, paths=r'^\.github/'), 'false')

    def test_no_base_or_head_builds(self):   # first push, force push, a base that is gone: never guess from HEAD~1
        for base in ('', '0' * 40, 'no-such-ref', '--output=x'):
            with self.subTest(base=base):
                o = self.outputs('plugins', base)
                self.assertEqual((o['changed'], o['workflow']), ('true', 'true'))
                self.assertIn('not a commit in this checkout', o['reason'])
        self.assertEqual(self.runs('plugins', head='0' * 40), 'true')

    def test_a_shallow_checkout_without_the_base_builds(self):
        first = self.base
        self.commit({'src/A.java': 'class A { int x; }'})
        clone = os.path.join(self.d, 'shallow')
        subprocess.run(['git', 'clone', '-q', '--depth', '1', f'file://{self.d}', clone], check=True, env={**os.environ, **ENV})
        self.d = clone
        self.assertEqual(self.runs('plugins', first), 'true')

    def test_with_an_unrelated_ci_change_nothing_builds(self):
        self.commit({CI: FIXTURE.replace('python3 tests.py', 'python3 tests.py -v'), 'docs/README.md': 'z'})
        self.assertEqual(self.builds(), NONE)

    def test_the_log_escapes_names_and_the_output_has_one_line(self):
        job = 'plugins\n::error::x\nchanged=false'
        self.edit('python3 tests.py', 'python3 tests.py -v')
        o = self.outputs(job)
        self.assertEqual(o['changed'], 'true')
        self.assertNotIn('\n::error', self.log)
        self.assertTrue(o['reason'].startswith('changed: '), o)


class Paths(Repo):
    def test_a_path_that_matches_builds(self):
        self.commit({'src/A.java': 'class A { int x; }'})
        o = self.outputs('plugins', paths='# the sources\n^src/\n\n^lib/')
        self.assertEqual((o['changed'], o['workflow']), ('true', 'false'))
        self.assertEqual(o['reason'], 'changed: src/A.java matches ^src/')

    def test_no_path_that_matches_builds_nothing(self):
        self.commit({'docs/README.md': 'new text'})
        self.assertEqual(self.runs('plugins', paths='^src/'), 'false')

    def test_the_workflow_file_counts_only_through_its_job_compare(self):
        self.edit('python3 tests.py', 'python3 tests.py -v')
        self.assertEqual(self.runs('plugins', paths='^\\.github/\n.*'), 'false')

    def test_a_rename_out_of_the_matched_folder_builds(self):   # rename detection would list only the new path
        self.commit({'src/A.java': None, 'docs/A.java': 'class A {}'})
        self.assertEqual(self.runs('plugins', paths='^src/'), 'true')

    def test_a_bad_expression_builds(self):
        self.commit({'docs/README.md': 'new text'})
        o = self.outputs('plugins', paths='^src/(')
        self.assertEqual(o['changed'], 'true')
        self.assertIn('not a regular expression', o['reason'])

    def test_a_paths_value_that_looks_like_an_option_is_a_value(self):
        self.commit({'docs/README.md': 'new text'})
        self.assertEqual(self.runs('plugins', paths='--help'), 'false')

    def test_a_failed_diff_builds(self):   # only the diff fails: never "nothing changed"
        real = job_changed.git
        fake = lambda *a: None if a[0] == 'diff' else real(*a)
        cwd = os.getcwd()
        os.chdir(self.d)
        try:
            changed, wf, lines = job_changed.decide(self.base, 'HEAD', CI, 'plugins', ['^src/'], run=fake)
        finally:
            os.chdir(cwd)
        self.assertEqual((changed, wf), (True, True))
        self.assertIn('git diff', lines[0])

    def test_workflow_alone_for_a_test_scope_caller(self):
        # A test-scope step needs "does the workflow change reach this job", apart from the build paths (a caller's
        # test scope widened to every test on any edit of the workflow file).
        self.commit({CI: FIXTURE.replace('python3 tests.py', 'python3 tests.py -v'), 'src/A.java': 'class A { int x; }'})
        o = self.outputs('plugins', paths='^src/')
        self.assertEqual((o['changed'], o['workflow']), ('true', 'false'))
        self.edit('ciJars', 'ciJars -x')
        self.assertEqual(self.outputs('plugins', paths='^src/')['workflow'], 'true')


class Units(unittest.TestCase):
    def test_no_pyyaml_counts_the_file_and_warns(self):
        saved = sys.modules.get('yaml')
        sys.modules['yaml'] = None   # import yaml raises ImportError
        try:
            self.assertIn('PyYAML', job_changed.workflow_reason(FIXTURE, FIXTURE, 'plugins'))
            with tempfile.TemporaryDirectory() as d:
                out = os.path.join(d, 'out')
                ids = {'rev-parse:a:x': 'old', 'rev-parse:b:x': 'new'}
                fake = lambda *a: ids.get(':'.join(a[0:1] + a[-1:]), 'c') if a[0] == 'rev-parse' else FIXTURE
                log = io.StringIO()
                with contextlib.redirect_stdout(log), unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': out}):
                    job_changed.main(['--base', 'a', '--head', 'b', '--workflow', 'x', '--job', 'plugins'], run=fake)
                with open(out) as f:
                    self.assertIn('changed=true\nworkflow=true\nreason=changed: x counts for job plugins: PyYAML', f.read())
                self.assertIn('::warning title=job-changed::PyYAML', log.getvalue())
        finally:
            sys.modules['yaml'] = saved

    def test_an_unforeseen_error_still_writes_changed(self):
        def boom(*a):
            raise OSError('disk')
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, 'out')
            with contextlib.redirect_stdout(io.StringIO()), unittest.mock.patch.dict(os.environ, {'GITHUB_OUTPUT': out}):
                self.assertEqual(job_changed.main(['--base', 'a', '--workflow', 'x', '--job', 'j'], run=boom), 0)
            with open(out) as f:
                self.assertIn('changed=true\nworkflow=true\nreason=changed: job-changed failed (OSError: disk)', f.read())

    def test_needs_cycles_and_strings_end(self):
        jobs = {'a': {'needs': 'b'}, 'b': {'needs': ['a', 'c']}, 'c': {}}
        self.assertEqual(job_changed.needed(jobs, 'a'), {'a', 'b', 'c'})
        with self.assertRaises(KeyError):
            job_changed.needed({'a': {'needs': 'gone'}}, 'a')

    def test_deep_nesting_counts(self):
        deep = 'jobs:\n  a:\n    env: ' + '[' * 3000 + ']' * 3000 + '\n'
        self.assertIsNotNone(job_changed.workflow_reason(deep, deep + '# x\n', 'a'))


class RealWorkflows(unittest.TestCase):
    """The rule on this repository's own workflows, not only on the fixture."""
    def test_every_job_of_every_fork_workflow_parses_and_a_comment_changes_nothing(self):
        folder = os.path.join(ROOT, '.github', 'workflows')
        names = sorted(n for n in os.listdir(folder) if n.startswith('fork-') and n.endswith('.yml'))
        self.assertTrue(names)
        for name in names:
            with open(os.path.join(folder, name)) as f:
                text = f.read()
            for job in job_changed.load(text, yaml)['jobs']:
                with self.subTest(workflow=name, job=job):
                    self.assertIsNone(job_changed.workflow_reason(text, text + '\n# a note\n', job))


class Action(unittest.TestCase):
    text = open(ACTION).read()

    def test_runs_only_its_own_script_with_inputs_by_env(self):   # never ${{ }} inside run: (script injection)
        env, run = step()
        self.assertTrue(run.startswith('python3 -I "$GITHUB_ACTION_PATH/job_changed.py" '), run)
        self.assertNotIn('${{', run)
        self.assertNotIn('uses:', self.text)
        for name in env:
            self.assertIn(f'="${name}"', run)   # --x="$X": a value starting with - is never an option

    def test_outputs_come_from_the_step(self):
        outputs = yaml.safe_load(self.text)['outputs']
        self.assertEqual({k: v['value'] for k, v in outputs.items()},
                         {k: f'${{{{ steps.changed.outputs.{k} }}}}' for k in ('changed', 'workflow', 'reason')})


if __name__ == '__main__':
    unittest.main()
