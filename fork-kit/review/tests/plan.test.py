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
        # every rule hit gives its lenses: the two critical reads, and the app file's own `code` read (issue 82)
        self.assertIn('lenses: data-1, data-2, code (independent: one new read-only agent each, no shared context', out)
        self.assertIn('  data-1: prompt .github/lenses/data.md; runner claude-opus-5-5 high\n', out)
        self.assertIn('  data-2: prompt .github/lenses/data.md; runner claude-opus-5-5 high\n', out)
        self.assertIn('  code: prompt .github/lenses/code.md; runner claude-opus-5-5 high\n', out)
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
        self.assertIn(f'minors: {review_kit.MINORS["same-round"]}\n', out)
        self.assertIn('state line: required by config, not enforced by the gate yet', out)

    def test_minors_default_to_same_round_and_issue_still_works(self):
        # #1006: with no `minors` key the plan says the same-round rule; a project that sets `issue` keeps it.
        self.assertEqual(review_kit.DEFAULTS['minors'], 'same-round')
        self.assertIn('never become an issue', review_kit.MINORS['same-round'])
        self.config()
        for settings, want, other in ((None, 'same-round', 'issue'), ({'version': 1}, 'same-round', 'issue'),
                                      ({'version': 1, 'minors': 'issue'}, 'issue', 'same-round')):
            with self.subTest(settings=settings):
                if settings is not None:   # None runs first: no settings file at all
                    self.put('.github/review-kit.json', settings)
                code, out, err = self.plan(changes(('M', 'src/app.ts')))
                self.assertEqual(code, 0, err)
                self.assertIn(f'minors: {review_kit.MINORS[want]}\n', out)
                self.assertNotIn(review_kit.MINORS[other], out)
        self.assertIn('minors: minors not fixed when the pull request stops go into one follow-up issue', out)

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
        self.assertIn(f'minors: {review_kit.MINORS["same-round"]}\n', out)
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

    def commit(self, message, *paths):
        for rel in paths:
            self.put(rel, f'{message}\n')
        self.git('add', '-A')
        self.git('commit', '-q', '-m', message)

    def test_a_base_config_that_is_not_utf8_is_refused_as_the_gate_refuses_it(self):   # #797 item 1
        path = self.config()
        with open(path, 'rb') as f:
            data = f.read().replace(b'docs/review.md', b'docs/r\xe9view.md')   # Latin-1, valid JSON otherwise
        with open(path, 'wb') as f:
            f.write(data)
        self.git('init', '-q')
        self.commit('base', 'src/app.ts')
        self.git('checkout', '-q', '-b', 'topic')
        self.commit('change', 'src/x.ts')
        with self.assertRaises(review_kit.gate_config.ConfigError):   # the gate's reader, on the same bytes
            review_kit.gate_config.parse(data, 'config')
        self.assertIn('not UTF-8', self.plain(self.run_plan('--base', 'main')))
        self.assertIn('not UTF-8', self.plain(self.cli('plan', '--base', 'main')))

    def test_base_reads_every_merge_base_as_the_gate_does(self):   # #797 item 2: criss-cross merges
        self.config()
        self.git('init', '-q')
        self.commit('root', 'src/app.ts')
        self.git('checkout', '-q', '-b', 'x')
        self.commit('x', 'src/storage/x.ts')
        self.git('checkout', '-q', '-b', 'y', 'main')
        self.commit('y', 'src/y.ts')
        self.git('checkout', '-q', '-B', 'main', 'x')
        self.git('merge', '-q', '--no-ff', '-m', 'main takes y', 'y')
        self.git('checkout', '-q', '-b', 'topic', 'y')
        self.git('merge', '-q', '--no-ff', '-m', 'topic takes x', 'x')
        self.commit('topic', 'src/t.ts')
        bases = subprocess.run([*GIT, '-C', self.dir, 'merge-base', '--all', 'main', 'topic'], capture_output=True,
                               text=True, env=self.env).stdout.split()
        self.assertEqual(len(bases), 2, 'the fixture needs two merge bases')
        code, out, err = self.cli('plan', '--base', 'main')
        self.assertEqual((code, err), (0, ''), out)
        # from one merge base git sees two paths (src/t.ts and one of the others); the gate sees all three
        self.assertIn('3 changed paths', out)
        self.assertIn('weight: critical', out)

    def test_base_reads_paths_whatever_the_repository_git_settings(self):   # #797 item 2: unusual git settings
        self.config()
        self.git('init', '-q')
        self.commit('base', 'src/app.ts', 'src/sub/a.ts')
        self.git('checkout', '-q', '-b', 'topic')
        self.commit('change', 'src/storage/wallet.ts', 'src/sub/a.ts')
        self.git('config', 'diff.relative', 'true')   # from src/sub, a plain git diff shows only src/sub's paths
        out, err = io.StringIO(), io.StringIO()
        code = self.m.run(['plan', '--root', os.path.join(self.dir, 'src', 'sub'), '--base', 'main'], out, err)
        self.assertEqual((code, err.getvalue()), (0, ''), out.getvalue())
        self.assertIn('2 changed paths', out.getvalue())
        self.assertIn('weight: critical', out.getvalue())

    def base_repo(self):
        """A repository with the config on main and one more commit on topic (checked out)."""
        self.config()
        self.git('init', '-q')
        self.commit('base', 'src/app.ts')
        self.git('checkout', '-q', '-b', 'topic')
        self.commit('change', 'src/storage/wallet.ts')

    def test_a_base_without_the_config_falls_back(self):
        self.git('init', '-q')
        self.commit('base', 'src/app.ts')
        self.git('checkout', '-q', '-b', 'topic')
        self.commit('change', 'src/storage/wallet.ts')
        code, out, err = self.run_plan('--base', 'main')
        self.assertEqual((code, err), (0, ''), out)
        self.assertIn('no queue config .github/pr-queue.json at ', out)
        self.assertIn('weight: normal\nlenses: review (one read-only agent)', out)

    def test_a_base_config_git_cannot_read_is_a_problem(self):   # #957 item 1
        self.base_repo()
        blob = subprocess.run([*GIT, '-C', self.dir, 'rev-parse', 'main:.github/pr-queue.json'], capture_output=True,
                              text=True, env=self.env, check=True).stdout.strip()
        os.remove(os.path.join(self.dir, '.git', 'objects', blob[:2], blob[2:]))   # as a partial clone offline
        err = self.plain(self.run_plan('--base', 'main'))
        self.assertIn('.github/pr-queue.json is in ', err)
        self.assertIn('git cannot read it', err)

    def test_a_repository_the_gate_would_refuse_is_refused(self):   # #957 item 2: grafts change merge bases
        self.base_repo()
        for name in ('info/grafts', 'objects/info/alternates', 'shallow'):
            with self.subTest(name=name):
                path = os.path.join(self.dir, '.git', name)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write('')
                err = self.plain(self.run_plan('--base', 'main'))
                self.assertIn(f'cannot be read as the gate reads it', err)
                self.assertIn(name, err)
                os.remove(path)
        self.assertEqual(self.run_plan('--base', 'main')[0], 0)

    def test_a_linked_worktree_is_checked_in_the_common_git_folder(self):   # #957 item 2
        self.base_repo()
        wt = os.path.join(self.dir, 'wt')
        self.git('worktree', 'add', '-q', '--detach', wt, 'topic')
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(self.m.run(['plan', '--root', wt, '--base', 'main'], out, err), 0, err.getvalue())
        self.assertIn('weight: critical', out.getvalue())
        with open(os.path.join(self.dir, '.git', 'info', 'grafts'), 'w', encoding='utf-8') as f:
            f.write('')
        out, err = io.StringIO(), io.StringIO()
        self.assertEqual(self.m.run(['plan', '--root', wt, '--base', 'main'], out, err), 2, out.getvalue())
        self.assertIn('info/grafts', err.getvalue())


class States(Project):
    """The files the plan reads, in every state they can be in."""

    def test_no_queue_config(self):
        code, out, err = self.plan(changes(('M', 'src/storage/wallet.ts')))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('no queue config .github/pr-queue.json: no review rules', out)
        self.assertIn('weight: normal\nlenses: review (one read-only agent)\n', out)
        self.assertIn('SKILL.md, "Lens prompt"', out)

    def test_no_queue_config_names_settings_under_another_name(self):   # #957 item 4
        self.put('.github/fork-review-kit.json', {'version': 1, 'queue_config': '.github/fork-pr-queue.json'})
        self.config(rel='.github/fork-pr-queue.json')
        code, out, err = self.plan(changes(('M', 'src/storage/wallet.ts')))
        self.assertEqual((code, err), (0, ''))
        self.assertIn('no queue config .github/pr-queue.json', out)
        self.assertIn('.github/fork-review-kit.json looks like kit settings under another name: if it is this '
                      'project\'s, pass --settings .github/fork-review-kit.json', out)
        code, out, err = self.plan(changes(('M', 'src/storage/wallet.ts')), '--settings', '.github/fork-review-kit.json')
        self.assertEqual((code, err), (0, ''))
        self.assertNotIn('looks like kit settings', out)
        self.assertIn('weight: critical', out)

    def test_the_hint_only_when_no_settings_were_read(self):   # #957 item 4
        self.put('.github/fork-review-kit.json', {'version': 1})
        self.put('.github/review-kit.json', {'version': 1})   # the project's own settings: nothing missing
        out = self.plan(changes(('M', 'a')))[1]
        self.assertIn('no queue config', out)
        self.assertNotIn('looks like kit settings', out)

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
        for minors in ('later', None, ['issue'], {'issue': True}):
            self.put('.github/review-kit.json', {'version': 1, 'runner': {'model': 'm', 'effort': 'max'},
                                                 'audit_runner': 'fable', 'lens_rounds': 0, 'minors': minors,
                                                 'state_question': ' ', 'queue_config': '',
                                                 'lens_runners': {'x': {'model': 'm', 'effort': 'low'}}})
            err = self.plain(self.plan(changes(('M', 'a'))))
            for part in ("runner has effort 'max'", 'audit_runner must be', 'lens_rounds must be',
                         'minors must be one of', 'state_question must be', 'queue_config must be',
                         "lens_runners.x has effort 'low'"):
                with self.subTest(minors=minors, part=part):
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
    'a base config is read as bytes': ('    return r.stdout\n', "    return r.stdout.decode('utf-8', 'replace')\n",
                                       (Plan, 'test_a_base_config_that_is_not_utf8_is_refused_as_the_gate_refuses_it')),
    'a base config git cannot read is a problem': ("        raise Problem(f'{rel} is in {base[:12]} but git cannot read it",
                                                   "        return None\n        raise Problem(f'{rel} is in {base[:12]} "
                                                   "but git cannot read it", (Plan, 'test_a_base_config_git_cannot_read_is_a_problem')),
    'a base without the config falls back': ('    if not r.stdout:\n        return None\n',
                                             "    if not r.stdout:\n        raise Problem('no config')\n",
                                             (Plan, 'test_a_base_without_the_config_falls_back')),
    "the gate's repository check": ('        if why:\n            raise Problem(f\'the repository at {root} cannot be read',
                                    '        if False:\n            raise Problem(f\'the repository at {root} cannot be read',
                                    (Plan, 'test_a_repository_the_gate_would_refuse_is_refused')),
    'the common git folder': ("'--git-common-dir'", "'--absolute-git-dir'",
                              (Plan, 'test_a_linked_worktree_is_checked_in_the_common_git_folder')),
    'the settings hint': ('    if others:\n        cfg_note +=', '    if False:\n        cfg_note +=',
                          (States, 'test_no_queue_config_names_settings_under_another_name')),
    'the hint only without settings': ("others = other_settings(root) if cfg is None and note else []",
                                       "others = other_settings(root) if cfg is None else []",
                                       (States, 'test_the_hint_only_when_no_settings_were_read')),
    'the gate\'s changed paths': ('found, why = head_check.changed_paths(checkout, base, head)',
                                  "found, why = review_judge.parse_name_status(subprocess.run(['git', '-C', root, "
                                  "'diff', '--name-status', '-z', '-M', f'{base}...HEAD'], capture_output=True)"
                                  ".stdout.decode()), None",
                                  (Plan, 'test_base_reads_paths_whatever_the_repository_git_settings')),
    'the config is read from the base': ("['git', '-C', root, 'show', f'{base}:{path}']", "['false']",
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
