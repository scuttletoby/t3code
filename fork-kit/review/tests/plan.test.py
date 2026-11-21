"""`review_kit.py plan` and the kit settings: the lenses a change needs, judged by the gate's own rules from the
project's review block; and the state question for the files it reads: no file, an empty file, an old config (no
`review` block), a newer version, a broken file, all a plain message and never a traceback; the same input gives the
same output and writes nothing. RedCheck breaks each settings rule in a copy of lib/review_kit.py and shows its test
then fails.

  python3 -I -B fork-kit/review/tests/plan.test.py
"""
import io, json, os, shutil, subprocess, sys, tempfile, types, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
LIB = os.path.join(KIT, 'lib', 'review_kit.py')
GATE = os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue')
sys.path.insert(0, os.path.join(KIT, 'lib'))
import review_kit  # noqa: E402

with open(os.path.join(GATE, 'review.recommended.json'), encoding='utf-8') as f:
    REVIEW = json.load(f)
QUEUE = {'required_checks': ['build', 'test', 'review-check'], 'merge_method': 'squash', 'bot_logins': [],
         'lens_author_associations': ['OWNER'], 'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
         'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
         'head_check': 'review-check'}
GIT = ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
       '-c', 'init.defaultBranch=main']


def changes(*pairs):
    return ''.join('\0'.join(entry) + '\0' for entry in pairs)


def load(source=None):
    if source is None:
        return review_kit
    mod = types.ModuleType('review_kit_broken')
    mod.__file__ = LIB
    exec(compile(source, LIB, 'exec'), mod.__dict__)
    return mod


class Project(unittest.TestCase):
    m = review_kit

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='review-kit-plan-')
        self.addCleanup(shutil.rmtree, self.dir)
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'HOME': self.dir}

    def put(self, rel, value):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(value if isinstance(value, str) else json.dumps(value))
        return path

    def config(self, review=REVIEW, rel='.github/pr-queue.json'):
        return self.put(rel, {**QUEUE, 'review': review} if review is not None else QUEUE)

    def run_plan(self, *args):
        """(exit code, out, err) of the plan in process."""
        out, err = io.StringIO(), io.StringIO()
        code = self.m.run(['plan', '--root', self.dir, *args], out, err)
        return code, out.getvalue(), err.getvalue()

    def plan(self, ch, *extra):
        """The plan with the changes `ch` (name-status -z text)."""
        return self.run_plan('--changes', self.put('changes.bin', ch), *extra)

    def cli(self, *args):
        r = subprocess.run([sys.executable, '-I', '-B', LIB, *args], capture_output=True, text=True, cwd=self.dir,
                           env=self.env, timeout=60)
        return r.returncode, r.stdout, r.stderr

    def git(self, *args):
        subprocess.run([*GIT, '-C', self.dir, *args], check=True, capture_output=True, env=self.env)

    def files(self):
        out = {}
        for root, _, names in os.walk(self.dir):
            for n in names:
                with open(os.path.join(root, n), 'rb') as f:
                    out[os.path.relpath(os.path.join(root, n), self.dir)] = f.read()
        return out

    def plain(self, result, code=2):
        self.assertEqual(result[0], code, result)
        self.assertNotIn('Traceback', result[1] + result[2])
        return result[2]


class Plan(Project):
    def test_a_critical_path_needs_two_independent_lenses(self):
        self.config()
        code, out, err = self.plan(changes(('M', 'src/storage/wallet.ts'), ('M', 'src/app.ts')))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('weight: critical\n', out)
        self.assertIn('lenses: data-1, data-2 (independent: one new read-only agent each, no shared context', out)
        self.assertIn('  data-1: prompt .github/lenses/data.md; runner claude-opus-5-5 high\n', out)
        self.assertIn('note: the review block is in shadow', out)

    def test_a_normal_path_needs_one_lens(self):
        self.config()
        code, out, _ = self.plan(changes(('M', 'src/app.ts')))
        self.assertIn('weight: normal\n', out)
        self.assertIn('lenses: code (one read-only agent)\n', out)

    def test_a_light_path_needs_none(self):
        self.config()
        code, out, _ = self.plan(changes(('M', 'docs/guide.md')))
        self.assertIn('weight: light\n', out)
        self.assertIn('lenses: none (light): CI only\n', out)

    def test_the_state_question_is_in_every_plan(self):
        self.config()
        for ch in (changes(('M', 'docs/guide.md')), changes(('M', 'src/app.ts'))):
            self.assertIn(review_kit.STATE_QUESTION, self.plan(ch)[1])

    def test_rounds_minors_and_the_audit(self):
        self.config()
        out = self.plan(changes(('M', 'src/app.ts')))[1]
        self.assertIn('lens rounds: at most 2; after that the orchestrator checks each fix diff itself; the gate caps '
                      'rounds at 4', out)
        self.assertIn('stuck after the last lens round: one audit, claude-fable-5-2 high', out)
        self.assertIn('minors: minors not fixed when the pull request stops go into one follow-up issue', out)
        self.assertIn('state line: required by config, not enforced by the gate yet', out)

    def test_settings_change_runners_rounds_and_minors(self):
        self.config()
        self.put('.github/review-kit.json', {'version': 1, 'lens_runners': {'data-2': {'model': 'm-2', 'effort': 'xhigh'}},
                                             'lens_rounds': 5, 'minors': 'same-round', 'state_question':
                                             'Does it survive a second run and a crash half-way?'})
        code, out, err = self.plan(changes(('A', 'migrations/0001.sql')))
        self.assertEqual(code, 0, err)
        self.assertIn('  data-2: prompt .github/lenses/data.md; runner m-2 xhigh\n', out)
        self.assertIn('  data-1: prompt .github/lenses/data.md; runner claude-opus-5-5 high\n', out)
        self.assertIn('note: lens_rounds 5 is above the gate\'s rounds.max 4', out)
        self.assertIn('minors: minors are fixed in the same round', out)
        self.assertIn('Does it survive a second run and a crash half-way?', out)
        self.assertNotIn(review_kit.STATE_QUESTION, out)

    def test_another_queue_config_path(self):
        self.config(rel='.github/queue.json')
        self.put('.github/review-kit.json', {'version': 1, 'queue_config': '.github/queue.json'})
        self.assertIn('weight: normal', self.plan(changes(('M', 'src/app.ts')))[1])

    def test_runs_again_the_same_and_writes_nothing(self):
        self.config()
        ch = changes(('R100', 'src/old.ts', 'src/storage/new.ts'))
        before = self.files()
        first = self.plan(ch)
        self.assertEqual(first[0], 0, first)
        self.assertEqual(self.plan(ch), first)
        self.assertEqual(self.files(), {**before, 'changes.bin': ch.encode()})

    def test_base_reads_the_config_and_changes_from_git(self):
        self.config()
        self.put('src/app.ts', 'a\n')
        self.git('init', '-q')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'base')
        self.git('checkout', '-q', '-b', 'topic')
        self.put('src/storage/wallet.ts', 'b\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', 'change')
        self.put('.github/pr-queue.json', '')   # the working tree's config is broken: the plan reads the base's
        first = self.run_plan('--base', 'main')
        self.assertEqual(first[0], 0, first)
        self.assertIn('weight: critical', first[1])
        self.assertEqual(self.run_plan('--base', 'main'), first)
        self.assertEqual(self.cli('plan', '--base', 'main'), first)

    def test_a_base_that_is_no_commit(self):
        self.git('init', '-q')
        self.assertIn('nosuch is not a commit', self.plain(self.cli('plan', '--base', 'nosuch')))


class States(Project):
    """The files the plan reads, in every state they can be in."""

    def test_no_queue_config(self):
        code, out, err = self.plan(changes(('M', 'src/storage/wallet.ts')))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('no queue config .github/pr-queue.json: no review rules', out)
        self.assertIn('weight: normal\nlenses: review (one read-only agent)\n', out)
        self.assertIn('SKILL.md, "Lens prompt"', out)

    def test_an_old_config_without_a_review_block(self):
        self.config(review=None)
        code, out, err = self.plan(changes(('M', 'src/app.ts')))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('has no `review` block (a config from before the review rules)', out)
        self.assertIn('lenses: review (one read-only agent)', out)

    def test_an_empty_queue_config(self):
        self.put('.github/pr-queue.json', '  \n')
        self.assertIn('is empty', self.plain(self.plan(changes(('M', 'a')))))

    def test_a_broken_queue_config(self):
        for text in ('{', '[]', json.dumps({**QUEUE, 'review': {**REVIEW, 'version': 2}}),
                     json.dumps({**QUEUE, 'review': {}}), json.dumps({**QUEUE, 'surprise': 1})):
            with self.subTest(text=text[:40]):
                self.put('.github/pr-queue.json', text)
                self.assertIn('the gate refuses the queue config', self.plain(self.plan(changes(('M', 'a')))))

    def test_a_newer_review_block_names_the_update(self):
        self.config(review={**REVIEW, 'version': 2})
        self.assertIn('version', self.plain(self.plan(changes(('M', 'a')))))

    def test_no_settings_file_means_defaults(self):
        self.config()
        out = self.plan(changes(('M', 'src/app.ts')))[1]
        self.assertIn('no .github/review-kit.json: the kit defaults apply', out)
        self.assertIn('runner claude-opus-5-5 high', out)

    def test_an_empty_settings_file(self):
        self.put('.github/review-kit.json', '')
        self.assertIn('are empty: write {"version": 1}', self.plain(self.plan(changes(('M', 'a')))))

    def test_settings_of_a_newer_version(self):
        self.put('.github/review-kit.json', {'version': 2})
        self.assertIn('this kit reads version 1: update the kit', self.plain(self.plan(changes(('M', 'a')))))

    def test_settings_of_no_version(self):
        self.put('.github/review-kit.json', {'runner': {'model': 'x', 'effort': 'high'}})
        err = self.plain(self.plan(changes(('M', 'a'))))
        self.assertIn('version null', err)
        self.assertNotIn('update the kit', err)

    def test_an_unknown_settings_key(self):
        self.put('.github/review-kit.json', {'version': 1, 'runers': {}})
        self.assertIn("unknown key 'runers'", self.plain(self.plan(changes(('M', 'a')))))

    def test_wrong_settings_values_are_all_named(self):
        self.put('.github/review-kit.json', {'version': 1, 'runner': {'model': 'm', 'effort': 'max'},
                                             'audit_runner': 'fable', 'lens_rounds': 0, 'minors': 'later',
                                             'state_question': ' ', 'queue_config': '',
                                             'lens_runners': {'x': {'model': 'm', 'effort': 'low'}}})
        err = self.plain(self.plan(changes(('M', 'a'))))
        for part in ("runner has effort 'max'", 'audit_runner must be', 'lens_rounds must be', 'minors must be one of',
                     'state_question must be', 'queue_config must be', "lens_runners.x has effort 'low'"):
            with self.subTest(part=part):
                self.assertIn(part, err)

    def test_settings_that_are_no_object_or_no_json(self):
        for text in ('[1]', '{', 'true'):
            with self.subTest(text=text):
                self.put('.github/review-kit.json', text)
                self.plain(self.plan(changes(('M', 'a'))))

    def test_a_named_settings_file_that_is_missing(self):
        self.assertIn('do not exist', self.plain(self.plan(changes(('M', 'a')), '--settings', 'none.json')))

    def test_bad_changes_input(self):
        self.config()
        for ch in ('M\0', 'Q\0a\0', 'R100\0a\0'):
            with self.subTest(ch=ch):
                self.plain(self.plan(ch))
        self.assertIn('cannot be read', self.plain(self.run_plan('--changes', os.path.join(self.dir, 'none'))))

    def test_no_changes(self):
        self.config()
        code, out, err = self.plan('')
        self.assertEqual((code, err), (0, ''), err)
        self.assertIn('weight: ', out)

    def test_plan_needs_one_source_of_changes(self):
        for args in (('plan',), ('plan', '--base', 'main', '--changes', 'x')):
            with self.subTest(args=args):
                self.assertIn('exactly one of', self.plain(self.cli(*args)))


# rule: (text in lib/review_kit.py, the text that breaks it, (test class, the test that must then fail))
MUTATIONS = {
    'an empty settings file is refused': ('    if not text.strip():\n        raise Problem(f\'the kit settings',
                                          '    if False:\n        raise Problem(f\'the kit settings',
                                          (States, 'test_an_empty_settings_file')),
    'unknown settings keys are refused': ("problems = [f'unknown key {k!r} (known: {\", \".join(DEFAULTS)})' for k in raw "
                                          "if k not in DEFAULTS]", 'problems = []',
                                          (States, 'test_an_unknown_settings_key')),
    'forbidden efforts are refused': ("if v['effort'] in forbidden:", 'if False:',
                                      (States, 'test_wrong_settings_values_are_all_named')),
    'a newer settings version names the update': ("+ (': update the kit' if newer else '')", '',
                                                  (States, 'test_settings_of_a_newer_version')),
    'a missing queue config falls back': ("    if text is None:\n        return None, (f'no queue config",
                                          "    if text is None:\n        raise Problem('no config')\n"
                                          "    if False:\n        return None, (f'no queue config",
                                          (States, 'test_no_queue_config')),
    'an old config falls back': ('    if cfg.review is None:\n        return cfg, (', '    if False:\n        return cfg, (',
                                 (States, 'test_an_old_config_without_a_review_block')),
    'the gate\'s refusal is a plain message': ('    except gate_config.ConfigError as e:\n        raise Problem(',
                                               '    except KeyError as e:\n        raise Problem(',
                                               (States, 'test_a_broken_queue_config')),
    'per-lens runners': ("r = settings['lens_runners'].get(name) or settings['runner']", "r = settings['runner']",
                         (Plan, 'test_settings_change_runners_rounds_and_minors')),
    'the state question is in the plan': ("out.append(f'state question (word for word", "(f'state question (word for word",
                                          (Plan, 'test_the_state_question_is_in_every_plan')),
    'two lenses are independent': ("if len(lenses) > 1 else 'one read-only agent')", "if False else 'one read-only agent')",
                                   (Plan, 'test_a_critical_path_needs_two_independent_lenses')),
    'the config is read from the base': ("['git', '-C', root, 'show', f'{base}:{rel}']", "['false']",
                                         (Plan, 'test_base_reads_the_config_and_changes_from_git')),
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
