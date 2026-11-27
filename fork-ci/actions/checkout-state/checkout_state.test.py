#!/usr/bin/env python3
"""checkout_state.py and its action: no job on a self-hosted runner leaves checkout state (a sparse or partial clone, an
unclean checkout) in the shared work folder for the next job. Ported from a project's tests of the same rule; the
project keeps only its config file and one "this repository scans clean" test.
Rule pins each case the rule must see; Action runs the action's real `run:` text with bash, the way the runner does;
State pins what happens on a second run and on old, empty or broken state (each fails closed, exit 2).
Run: python3 fork-ci/actions/checkout-state/checkout_state.test.py
"""
import json, os, re, shutil, subprocess, sys, tempfile, textwrap, unittest

import yaml

sys.dont_write_bytecode = True   # the tests import checkout_state.py: leave no __pycache__ in the action folder

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, 'checkout_state.py')
ACTION = os.path.join(HERE, 'action.yml')
sys.path.insert(0, HERE)
import checkout_state as cs  # noqa: E402

CO = 'actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1'


def lint(*args, cwd=None):
    return subprocess.run([sys.executable, '-I', SCRIPT, *args], capture_output=True, text=True, cwd=cwd)


class Rule(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='checkout-state-test-')
        self.addCleanup(shutil.rmtree, self.root, True)
        os.makedirs(os.path.join(self.root, '.github', 'workflows'))

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(text))

    def scan(self, config=None):
        return cs.scan(self.root, cs.parse_config(config or {}))[0]

    def job_with(self, step_yaml, runs_on='[self-hosted, example-light]'):
        steps = textwrap.indent(textwrap.dedent(step_yaml), ' ' * 6)
        self.write('.github/workflows/w.yml', f'on: push\njobs:\n  j:\n    runs-on: {runs_on}\n    steps:\n{steps}')
        return self.scan()

    def test_the_rule_sees_each_kind_of_state(self):
        for w in ({'sparse-checkout': 'a.py\nb.py\n', 'sparse-checkout-cone-mode': False},
                  {'sparse-checkout': 'a.py', 'path': 'kick'},   # a path of its own is inside the work folder too
                  {'filter': 'blob:none'}, {'clean': False}, {'clean': 'false'}, {'clean': 'no'}, {'clean': 0},
                  {'clean': '${{ false }}'}, {'clean': '${{ inputs.clean }}'}):
            self.assertTrue(cs.problems({'steps': [{'uses': CO, 'with': w}]}), w)
            self.assertTrue(cs.problems({'steps': [{'uses': CO.replace('actions/checkout', 'Actions/Checkout'), 'with': w}]}))
        for run in ('git sparse-checkout set --no-cone /x', 'git config core.sparseCheckout true',
                    'git fetch --filter=blob:none origin main', 'git config CORE.SPARSECHECKOUT true',
                    'git config extensions.partialClone origin', 'git config remote.origin.promisor true',
                    'git config remote.origin.partialCloneFilter blob:none'):
            self.assertTrue(cs.problems({'steps': [{'run': run}]}), run)
        for w in (None, {}, {'fetch-depth': 0}, {'ref': 'main', 'clean': True}, {'clean': 'true'}, {'clean': 'TRUE'},
                  {'sparse-checkout': ''}, {'path': 'other'}, {'persist-credentials': False}):
            self.assertEqual(cs.problems({'steps': [{'uses': CO, 'with': w}]}), [], w)
        self.assertEqual(cs.problems({'steps': [{'run': 'gh run list --json conclusion'}]}), [])

    def test_inputs_are_read_as_the_runner_and_actions_checkout_read_them(self):
        """Input names ignore case; any sparse-checkout or filter value is used (an unquoted false is the pattern
        'false'); an empty clean is unset, so true; a git command continued on the next line is one command."""
        for step in ('Sparse-Checkout: a.py', 'FILTER: blob:none', 'Clean: false', 'sparse-checkout: false',
                     'sparse-checkout: 0', 'filter: false'):
            with self.subTest(step):
                self.assertTrue(self.job_with(f'- uses: {CO}\n  with:\n    {step}\n'), step)
        for step in ('clean:', "clean: ''", 'Clean: true'):
            with self.subTest(step):
                self.assertEqual(self.job_with(f'- uses: {CO}\n  with:\n    {step}\n'), [], step)
        self.assertTrue(cs.problems({'steps': [{'run': 'git fetch \\\n  --filter=blob:none origin main'}]}))

    def test_clean_yes_or_on_is_not_true(self):
        """YAML 1.1 (PyYAML) reads these as true; GitHub hands the action the string, and actions/checkout
        cleans only on exactly true (any case)."""
        for value in ('yes', 'Yes', 'on', 'ON', 'y', 'off', 'no', 'n', '1', "'true, really'"):
            with self.subTest(value):
                bad = self.job_with(f'- uses: {CO}\n  with:\n    clean: {value}\n')
                self.assertTrue(bad, value)
        for value in ('true', 'True', 'TRUE', "'true'", '"TRUE"', "' true '", "'tRuE'"):
            with self.subTest(value):
                self.assertEqual(self.job_with(f'- uses: {CO}\n  with:\n    clean: {value}\n'), [], value)

    def test_the_on_key_is_read_as_on(self):
        self.write('.github/workflows/w.yml', 'on: push\njobs: {}\n')
        self.assertIn('on', cs.load_yaml(os.path.join(self.root, '.github/workflows/w.yml')))

    def test_a_local_composite_action_is_read(self):
        """a step `uses: ./x` runs x/action.yml's steps in the same work folder."""
        self.write('acts/sparse/action.yml', f'''\
            runs:
              using: composite
              steps:
                - uses: {CO}
                  with:
                    sparse-checkout: a.py
                    sparse-checkout-cone-mode: false
            ''')
        self.write('acts/fine/action.yaml', '''\
            runs:
              using: composite
              steps:
                - run: echo hi
                  shell: bash
            ''')
        self.write('acts/nested/action.yml', '''\
            runs:
              using: composite
              steps:
                - uses: ./acts/sparse
            ''')
        self.write('acts/loop/action.yml', '''\
            runs:
              using: composite
              steps:
                - uses: ./acts/loop/
            ''')
        self.write('acts/runs/action.yml', '''\
            runs:
              using: composite
              steps:
                - run: git sparse-checkout set --no-cone /x
                  shell: bash
            ''')
        self.write('acts/node/action.yml', 'runs:\n  using: node20\n  main: index.js\n')
        self.assertEqual(self.job_with('- uses: ./acts/fine\n'), [])
        self.assertEqual(self.job_with('- uses: ./acts/loop\n'), [], 'a loop is read once, not for ever')
        for uses in ('./acts/sparse', './acts/sparse/', './acts/nested', './acts/runs'):
            bad = self.job_with(f'- uses: {uses}\n')
            self.assertTrue(bad, uses)
            self.assertTrue('acts/sparse' in bad[0] or 'acts/runs' in bad[0], (uses, bad))
        for uses, why in (('./acts/node', 'not a composite'), ('./acts/missing', 'no action.yml'),
                          ('./../outside', 'outside'), ('./acts/../../x', 'outside')):
            bad = self.job_with(f'- uses: {uses}\n')
            self.assertTrue(bad, uses)
            self.assertIn(why, bad[0])
        self.assertEqual(self.job_with('- uses: ./acts/sparse\n', runs_on='ubuntu-latest'), [], 'a fresh machine')

    def test_a_reusable_workflow_on_a_shared_runner_needs_its_exact_pin_listed(self):
        uses = 'o/r/.github/workflows/q.yml@' + 'a' * 40
        entry = {'workflow': '.github/workflows/x.yml', 'job': 'q', 'uses': uses, 'why': 'reviewed: nothing left'}
        def bad(job_name, job_uses, runs_on='["self-hosted", "example-light"]'):
            self.write('.github/workflows/x.yml', f'on: push\njobs:\n  {job_name}:\n    uses: {job_uses}\n'
                                                  f'    with:\n      runs-on: \'{runs_on}\'\n')
            return self.scan({'reusable': [entry]})
        def unlisted(job_name, job_uses):
            return [p for p in bad(job_name, job_uses) if f'x.yml/{job_name}: reusable workflow' in p]
        self.assertEqual(bad('q', uses), [])
        self.assertTrue(unlisted('q', uses.split('@')[0] + '@main'))   # a new pin is read again first
        self.assertTrue(unlisted('other', uses))
        self.assertTrue(unlisted('q', './.github/workflows/y.yml'))
        # a runs-on input is only a value; with no review saying the callee runs on it, it is shared
        self.assertTrue([p for p in bad('q2', 'o/r/.github/workflows/y.yml@v1', 'ubuntu-latest') if 'y.yml' in p])

    def test_a_reusable_workflow_runs_on_its_input_only_when_its_review_says_so(self):
        """only a reviewed entry's runner-input makes the caller's value decide where the job runs."""
        uses = 'o/r/.github/workflows/q.yml@' + 'b' * 40
        def scan(with_yaml, **extra):
            entry = {'workflow': '.github/workflows/x.yml', 'job': 'q', 'uses': uses, 'why': 'reviewed', **extra}
            self.write('.github/workflows/x.yml', f'on: push\njobs:\n  q:\n    uses: {uses}\n    with:\n'
                                                  f'      {with_yaml}\n')
            bad, seen, _ = cs.scan(self.root, cs.parse_config({'reusable': [entry]}))
            return bad, seen
        self.assertEqual(scan('runs-on: ubuntu-latest'), ([], 1), 'no runner-input: shared, its entry still read')
        self.assertEqual(scan('runs-on: ubuntu-latest', **{'runner-input': 'runs-on'}), ([], 0), 'a fresh machine')
        self.assertEqual(scan('Runs-On: ubuntu-24.04', **{'runner-input': 'runs-on'}), ([], 0), 'inputs ignore case')
        self.assertEqual(scan('machine: ubuntu-latest', **{'runner-input': 'machine'}), ([], 0))
        self.assertEqual(scan('machine: ubuntu-latest', **{'runner-input': 'Machine'}), ([], 0))
        for with_yaml, name in (("runs-on: '[\"self-hosted\", \"example-light\"]'", 'runs-on'),
                                ('runs-on: ubuntu-latest', 'machine'),   # the reviewed input is not the one handed
                                ('runs-on: ${{ vars.RUNNER }}', 'runs-on'), ('other: 1', 'runs-on')):
            with self.subTest(with_yaml=with_yaml, name=name):
                self.assertEqual(scan(with_yaml, **{'runner-input': name}), ([], 1))
        # a job the review says is on a fresh machine still needs that review: without it, it is shared and refused
        self.write('.github/workflows/x.yml', f'on: push\njobs:\n  q:\n    uses: {uses}\n    with:\n'
                                              f'      runs-on: ubuntu-latest\n')
        bad, seen, _ = cs.scan(self.root, cs.parse_config({}))
        self.assertEqual(seen, 1)
        self.assertTrue(bad and 'reusable workflow' in bad[0], bad)

    def test_an_action_from_another_repository_needs_its_exact_pin_listed(self):
        """a remote action's steps cannot be read here, so it needs a review, like a reusable workflow."""
        uses = 'o/r/acts/x@' + 'c' * 40
        reviewed = {'actions': [{'uses': uses, 'why': 'reviewed: one read-only step'}]}
        def scan(step_uses, config=None, runs_on='[self-hosted, example-light]'):
            self.write('.github/workflows/w.yml', f'on: push\njobs:\n  j:\n    runs-on: {runs_on}\n    steps:\n'
                                                  f'      - uses: {step_uses}\n')
            return self.scan(config)
        self.assertEqual(scan(uses, reviewed), [])
        for step_uses in (uses, uses.split('@')[0] + '@main', 'o/r/acts/y@' + 'c' * 40, 'docker://alpine:3',
                          'actions-rs/toolchain@v1', 'actionsx/evil@v1'):   # not GitHub's own actions/ org
            with self.subTest(step_uses):
                bad = [p for p in scan(step_uses, reviewed) if 'from another repository' in p]
                self.assertTrue(bad if step_uses != uses else not bad, (step_uses, bad))
        self.assertTrue([p for p in scan(uses) if 'from another repository' in p], 'no config: refused')
        for step_uses in ('actions/upload-artifact@v4', 'Actions/download-artifact@v4'):   # GitHub's own: trusted
            self.assertEqual(scan(step_uses), [], step_uses)
        self.assertEqual(scan(uses, runs_on='ubuntu-latest'), [], 'a fresh machine')
        # an entry nothing on a shared runner uses is reported, as a reusable entry is
        bad = scan(uses, reviewed, runs_on='ubuntu-latest')
        self.assertEqual(len(bad), 1)
        self.assertIn('no job on a shared runner uses it', bad[0])
        # a local composite action that uses a remote one: read through, refused unless reviewed, and counts as a use
        self.write('acts/wrap/action.yml', f'runs:\n  using: composite\n  steps:\n    - uses: {uses}\n')
        bad = scan('./acts/wrap')
        self.assertTrue(bad and './acts/wrap' in bad[0] and 'from another repository' in bad[0], bad)
        self.assertEqual(scan('./acts/wrap', reviewed), [])

    def test_a_command_handed_to_an_action_as_an_input_is_checked(self):
        """a review is of the action, not the call, and an action may run an input as a command or script
        (a reviewed action's `run`, a path it hands git, actions/github-script's `script`, a local composite's
        `${{ inputs.cmd }}`, a reusable workflow's input): every `with:` value but actions/checkout's is read as a
        run."""
        uses = 'o/r/acts/x@' + 'c' * 40
        reviewed = {'actions': [{'uses': uses, 'why': 'reviewed: runs its run input'}]}
        self.write('acts/wrap/action.yml', 'runs:\n  using: composite\n  steps:\n    - run: ${{ inputs.cmd }}\n'
                                           '      shell: bash\n')
        def scan(step_yaml, config=None, runs_on='[self-hosted, example-light]'):
            steps = textwrap.indent(textwrap.dedent(step_yaml), ' ' * 6)
            self.write('.github/workflows/w.yml', f'on: push\njobs:\n  j:\n    runs-on: {runs_on}\n    steps:\n{steps}')
            return self.scan(config)
        for step in (f'- uses: {uses}\n  with:\n    run: git sparse-checkout set x && python3 t.py\n',
                     f'- uses: {uses}\n  with:\n    path: "x; git config core.sparseCheckout true"\n',
                     f'- uses: {uses}\n  with:\n    Run: |\n      git fetch \\\n        --filter=blob:none origin\n',
                     '- uses: ./acts/wrap\n  with:\n    cmd: git sparse-checkout set x\n',
                     "- uses: actions/github-script@v7\n  with:\n"
                     "    script: exec.exec('git', ['sparse-checkout', 'set', 'a'])\n",
                     "- uses: Actions/github-script@v7\n  with:\n"
                     "    script: exec.exec('git clone --filter=blob:none x')\n"):
            with self.subTest(step):
                bad = scan(step, reviewed)
                self.assertTrue(bad and ' with ' in bad[0], bad)
                self.assertFalse([p for p in scan(step, reviewed, runs_on='ubuntu-latest') if ' with ' in p],
                                 'a fresh machine')
        for step in (f'- uses: {uses}\n  with:\n    run: python3 t.py\n    depth: 3\n    on: true\n',
                     "- uses: actions/github-script@v7\n  with:\n    script: core.info('hi')\n",
                     '- uses: ./acts/wrap\n  with:\n    cmd: echo hi\n'):
            with self.subTest(step):
                self.assertEqual(scan(step, reviewed if uses in step else None), [])
        self.assertEqual(scan(f'- uses: {CO}\n  with:\n    path: sparse-checkout\n'), [], 'checkout: its inputs only')
        # a reusable workflow's input is handed to steps this cannot read too
        rw = 'o/r/.github/workflows/q.yml@' + 'a' * 40
        entry = {'workflow': '.github/workflows/x.yml', 'job': 'q', 'uses': rw, 'why': 'reviewed'}
        for value, n in (('git sparse-checkout set a', 1), ('make test', 0)):
            with self.subTest(value):
                self.write('.github/workflows/x.yml', f'on: push\njobs:\n  q:\n    uses: {rw}\n    with:\n'
                                                      f'      cmd: {value}\n')
                bad = [p for p in cs.scan(self.root, cs.parse_config({'reusable': [entry]}))[0] if 'x.yml' in p]
                self.assertEqual(len(bad), n, bad)

    def test_a_runs_on_that_is_no_runner_is_reported(self):
        """true or a number as a job's runs-on, or as a reviewed runner-input's value, is a problem line,
        not a traceback; the job counts as shared."""
        for value in ('true', '5', '[self-hosted, 3]'):
            with self.subTest(value):
                self.write('.github/workflows/w.yml', f'on: push\njobs:\n  j:\n    runs-on: {value}\n'
                                                      f'    steps:\n      - run: echo hi\n')
                bad, seen, _ = cs.scan(self.root, cs.parse_config({}))
                self.assertEqual(seen, 1)
                self.assertTrue(len(bad) == 1 and 'w.yml/j: runs-on' in bad[0], bad)
        os.remove(os.path.join(self.root, '.github/workflows/w.yml'))
        uses = 'o/r/.github/workflows/q.yml@' + 'b' * 40
        entry = {'workflow': '.github/workflows/x.yml', 'job': 'q', 'uses': uses, 'why': 'r', 'runner-input': 'runs-on'}
        for value in ('true', '7'):
            with self.subTest(value):
                self.write('.github/workflows/x.yml', f'on: push\njobs:\n  q:\n    uses: {uses}\n    with:\n'
                                                      f'      runs-on: {value}\n')
                bad, seen, _ = cs.scan(self.root, cs.parse_config({'reusable': [entry]}))
                self.assertEqual(seen, 1)
                self.assertTrue(len(bad) == 1 and 'x.yml/q: runs-on' in bad[0], bad)
        r = lint('--root', self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertNotIn('Traceback', r.stderr)

    def test_an_entry_nothing_uses_is_reported(self):
        entry = {'workflow': '.github/workflows/gone.yml', 'job': 'q', 'uses': 'o/r/.github/workflows/q.yml@v1',
                 'why': 'x'}
        self.write('.github/workflows/w.yml', 'on: push\njobs: {}\n')
        bad = self.scan({'reusable': [entry]})
        self.assertEqual(len(bad), 1)
        self.assertIn('nothing uses', bad[0])

    def test_the_config_adds_folders_and_is_checked(self):
        self.write('more/flows/a.yml', f'on: push\njobs:\n  j:\n    runs-on: self-hosted\n    steps:\n'
                                       f'      - uses: {CO}\n        with:\n          filter: blob:none\n')
        self.assertEqual(self.scan(), [])
        bad, _, dirs = cs.scan(self.root, cs.parse_config({'workflows': ['more/flows/*.yml']}))
        self.assertEqual(len(bad), 1)
        self.assertEqual(dirs, {'more/flows'})
        for config in ({'workflows': 'x'}, {'workflows': ['/abs/*.yml']}, {'workflows': ['../x/*.yml']},
                       {'reusable': [{'workflow': 'a', 'job': 'b', 'uses': 'c'}]},
                       {'reusable': [{'workflow': 'a', 'job': 'b', 'uses': 'c', 'why': ' '}]}, {'other': 1},
                       {'reusable': [{'workflow': 'a', 'job': 'b', 'uses': 'c', 'why': 'd', 'runner-input': ''}]},
                       {'reusable': [{'workflow': 'a', 'job': 'b', 'uses': 'c', 'why': 'd', 'runner': 'runs-on'}]},
                       {'actions': 'x'}, {'actions': [{'uses': 'o/r@v1'}]}, {'actions': [{'uses': 'o/r@v1', 'why': ''}]},
                       {'actions': [{'uses': 'o/r@v1', 'why': 'd', 'job': 'j'}]},
                       {'actions': [{'uses': './acts/x', 'why': 'd'}]},
                       {'actions': [{'uses': 'actions/upload-artifact@v4', 'why': 'd'}]},
                       {'actions': [{'uses': 'o/r@v1', 'why': 'd'}, {'uses': 'o/r@v1', 'why': 'e'}]}):
            with self.subTest(config):
                self.assertRaises(ValueError, cs.parse_config, config)

    def test_a_repository_without_a_config_scans_its_workflows(self):
        self.write('.github/workflows/w.yml', f'on: push\njobs:\n  j:\n    runs-on: self-hosted\n    steps:\n'
                                              f'      - uses: {CO}\n        with:\n          clean: false\n')
        self.assertEqual(len(cs.scan(self.root, cs.load_config(self.root))[0]), 1)
        r = lint('--root', self.root)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('clean', r.stdout)
        with open(os.path.join(self.root, '.github', 'checkout-state.json'), 'w') as f:
            json.dump({'workflows': 5}, f)
        r = lint('--root', self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)

    def test_only_githubs_own_runners_count_as_fresh(self):
        for job in ({'runs-on': 'ubuntu-latest'}, {'runs-on': ['ubuntu-24.04']}, {'runs-on': 'windows-2025'},
                    {'runs-on': 'macos-15'}):
            self.assertFalse(cs.shared_runner(job), job)
        for job in ({'runs-on': ['self-hosted', 'example-light']}, {'runs-on': 'self-hosted'},
                    {'runs-on': "${{ fromJSON('[\"ubuntu-latest\"]') }}"},   # an expression: could be any runner
                    {'runs-on': ['ubuntu-latest', 'example-light']}, {'runs-on': {'group': 'x'}}, {},
                    {'uses': './.github/workflows/x.yml', 'with': {'runs-on': '["self-hosted", "example-light"]'}},
                    {'uses': 'owner/repo/.github/workflows/x.yml@v1'},
                    {'uses': 'owner/repo/.github/workflows/x.yml@v1', 'with': {'runs-on': 'ubuntu-latest'}}):
            self.assertTrue(cs.shared_runner(job), job)
        job = {'uses': 'owner/repo/.github/workflows/x.yml@v1', 'with': {'runs-on': 'ubuntu-latest'}}
        self.assertFalse(cs.shared_runner(job, {'runner-input': 'runs-on'}))


class Repo(unittest.TestCase):
    """A temp repository with one workflow; self.lint() runs the command line on it."""
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix='checkout-state-test-')
        self.addCleanup(shutil.rmtree, self.root, True)

    def write(self, rel, text):
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write(textwrap.dedent(text))

    def workflow(self, with_yaml='fetch-depth: 1', uses=None):
        step = f'- uses: {uses}\n' if uses else f'- uses: {CO}\n  with:\n    {with_yaml}\n'
        self.write('.github/workflows/w.yml', 'on: push\njobs:\n  j:\n    runs-on: [self-hosted, example-light]\n'
                                              '    steps:\n' + textwrap.indent(step, ' ' * 6))


def read_action():
    with open(ACTION, encoding='utf-8') as f:
        return yaml.safe_load(f)


class Action(Repo):
    """The action's real step, run with bash as the runner runs it (inputs by env, GITHUB_ACTION_PATH)."""
    def run_action(self, config='', root='.'):
        (step,) = read_action()['runs']['steps']
        env = {k: v for k, v in os.environ.items() if not k.startswith(('GITHUB_', 'PYTHON'))}
        env.update(GITHUB_ACTION_PATH=HERE, CONFIG=config, ROOT=root)
        return subprocess.run(['bash', '-e', '-o', 'pipefail', '-c', step['run']], cwd=self.root, env=env,
                              capture_output=True, text=True)

    def test_the_action_is_one_bash_step_of_python3_isolated_from_its_own_folder(self):
        """Does the action run only its pinned script, isolated (no code from the checkout)?"""
        action = read_action()
        (step,) = action['runs']['steps']
        self.assertEqual((action['runs']['using'], step['shell']), ('composite', 'bash'))
        self.assertIn('python3 -I "$GITHUB_ACTION_PATH/checkout_state.py"', step['run'])
        self.assertNotIn('${{', step['run'], 'inputs reach the step by env only')

    def test_clean_passes_state_fails_and_a_config_is_found_or_named(self):
        """Does the step exit 0 on a clean repository, 1 on state, and read the default or the named config?"""
        self.workflow()
        r = self.run_action()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.workflow('sparse-checkout: a.py')
        r = self.run_action()
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn('w.yml/j: step 0: sparse-checkout', r.stdout)
        uses = 'o/r/acts/x@' + 'c' * 40
        self.workflow(uses=uses)
        self.assertEqual(self.run_action().returncode, 1, 'no config: an unreviewed action is refused')
        self.write('ci/state.json', json.dumps({'actions': [{'uses': uses, 'why': 'reviewed'}]}))
        self.assertEqual(self.run_action(config='ci/state.json').returncode, 0)
        self.write('.github/checkout-state.json', json.dumps({'actions': [{'uses': uses, 'why': 'reviewed'}]}))
        self.assertEqual(self.run_action().returncode, 0, 'the default config is read')

    def test_a_second_run_says_the_same_and_leaves_nothing(self):
        """Run twice: is the answer the same, and is no file left in the checkout or the action folder?"""
        self.workflow('filter: blob:none')
        def listing(d):
            return sorted(os.path.relpath(os.path.join(a, n), d) for a, ds, fs in os.walk(d) for n in ds + fs)
        before = (listing(self.root), listing(HERE))
        first, second = self.run_action(), self.run_action()
        self.assertEqual((first.returncode, first.stdout), (second.returncode, second.stdout))
        self.assertEqual(first.returncode, 1)
        self.assertEqual((listing(self.root), listing(HERE)), before)


class State(Repo):
    """Old, empty or broken state: the lint never reads what it cannot judge as clean (exit 2, or a problem line)."""
    def test_a_config_that_is_a_broken_link_is_refused(self):
        """A default config that is a broken link (a moved file, a checkout without it) is not "no config"."""
        self.workflow()
        os.symlink('gone.json', os.path.join(self.root, '.github', 'checkout-state.json'))
        r = lint('--root', self.root)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('broken link', r.stdout)
        r = lint('--root', self.root, '--config', os.path.join(self.root, '.github', 'checkout-state.json'))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('broken link', r.stdout)

    def test_a_config_a_newer_release_wrote_or_that_is_not_json_is_refused(self):
        """A key this release does not know (a newer release's), an empty or non-JSON file: exit 2, no traceback."""
        self.workflow()
        for text in ('{"workflows": [], "runners": {}}', '', '{"workflows": [', '[]', '\udcff'):
            with self.subTest(text):
                with open(os.path.join(self.root, '.github', 'checkout-state.json'), 'w',
                          errors='surrogateescape') as f:
                    f.write(text)
                r = lint('--root', self.root)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn('bad config', r.stdout)
                self.assertNotIn('Traceback', r.stderr)

    def test_no_workflow_file_is_no_verdict(self):
        """An empty checkout or a wrong root must not read as clean."""
        for root in (self.root, os.path.join(self.root, 'missing')):
            with self.subTest(root):
                r = lint('--root', root)
                self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
                self.assertIn('cannot judge', r.stdout)
        os.makedirs(os.path.join(self.root, '.github', 'workflows'))
        self.assertEqual(lint('--root', self.root).returncode, 2, 'an empty workflows folder')
        self.write('a-file', 'x')
        self.assertEqual(lint('--root', os.path.join(self.root, 'a-file')).returncode, 2, 'a root that is a file')

    def test_a_workflow_that_is_a_broken_link_empty_or_not_yaml_is_reported(self):
        """A workflow file the lint cannot read is a problem line (exit 1), not a skip; an empty one has no jobs."""
        self.workflow()
        self.write('.github/workflows/empty.yml', '')
        self.assertEqual(lint('--root', self.root).returncode, 0)
        for name, make in (('gone.yml', lambda p: os.symlink('nowhere.yml', p)),
                           ('bad.yml', lambda p: self.write(os.path.relpath(p, self.root), 'jobs: [\n'))):
            with self.subTest(name):
                path = os.path.join(self.root, '.github', 'workflows', name)
                make(path)
                r = lint('--root', self.root)
                self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
                self.assertIn(f'{name}: cannot be read', r.stdout)
                os.remove(path)

    def test_a_config_that_predates_a_new_pin_names_both(self):
        """A branch whose config predates the action it now uses (a pin bump): the new pin is refused and the old
        entry is reported as unused, so the review is redone at the new pin."""
        old, new = 'o/r/acts/x@' + 'a' * 40, 'o/r/acts/x@' + 'b' * 40
        self.write('.github/checkout-state.json', json.dumps({'actions': [{'uses': old, 'why': 'reviewed at a'}]}))
        self.workflow(uses=new)
        r = lint('--root', self.root)
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertRegex(r.stdout, re.escape(f'action {new} from another repository'))
        self.assertRegex(r.stdout, re.escape(f'config: actions entry {old}: no job on a shared runner uses it'))

    def test_no_pyyaml_is_no_verdict(self):
        """A runner without PyYAML: exit 2 with what to install, never a traceback or a pass."""
        self.workflow()
        code = ('import runpy, sys; sys.modules["yaml"] = None; sys.argv = ["checkout_state.py", "--root", sys.argv[1]];'
                ' runpy.run_path(%r, run_name="__main__")' % SCRIPT)
        r = subprocess.run([sys.executable, '-S', '-c', code, self.root], capture_output=True, text=True)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn('PyYAML is not installed', r.stdout)
        self.assertNotIn('Traceback', r.stderr)


if __name__ == '__main__':
    unittest.main()
