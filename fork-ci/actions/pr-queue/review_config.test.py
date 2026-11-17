"""Tests of review_config.py and the `review` key of config.py (run: python3 fork-ci/actions/pr-queue/review_config.test.py)."""
import copy, json, os, re, sys, unittest
from unittest import mock

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import review_config as rc  # noqa: E402

RECOMMENDED = os.path.join(HERE, 'review.recommended.json')
with open(os.path.join(HERE, 'review.vectors.json'), encoding='utf-8') as _f:
    VECTORS_CONFIGS = json.load(_f)['configs']
README = os.path.join(HERE, 'README.md')
BASE = {
    'required_checks': ['build', 'test'], 'merge_method': 'squash', 'bot_logins': ['review-bot'],
    'lens_author_associations': ['OWNER'], 'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
    'head_check': 'test',
}


def recommended():
    with open(RECOMMENDED, encoding='utf-8') as f:
        return json.load(f)


def refusal(block):
    """The ConfigError text parse_review raises for `block`; fails the test when it parses."""
    try:
        rc.parse_review(block)
    except config.ConfigError as e:
        return str(e)
    raise AssertionError(f'parsed, should be refused: {json.dumps(block)[:200]}')


class Recommended(unittest.TestCase):
    def test_the_shipped_example_parses(self):
        r = rc.parse_review(recommended())
        self.assertEqual(r.version, 1)
        self.assertEqual(r.mode, 'shadow')
        self.assertEqual([x.name for x in r.rules], ['data', 'docs', 'code'])
        self.assertEqual(r.rules[0], rc.Rule('data', 'critical', ('src/storage/**', 'migrations/**'),
                                             ('data-1', 'data-2')))
        self.assertEqual(r.rules[1].lenses, ())
        self.assertEqual(r.lens('code'), rc.Lens('code', '.github/lenses/code.md'))
        self.assertIsNone(r.lens('nope'))
        self.assertEqual(r.min_lenses, rc.MinLenses(2, 1, 0))
        self.assertEqual(r.rounds, rc.Rounds(3, 4, 'approach'))
        self.assertIs(r.findings, True)
        self.assertIs(r.state_line, True)

    def test_owner_numbers(self):
        """Owner decisions: critical = two lenses, approach lens from round 3, cap 4."""
        r = rc.parse_review(recommended())
        self.assertEqual((r.min_lenses.critical, r.rounds.approach_at, r.rounds.max), (2, 3, 4))

    def test_in_a_whole_config(self):
        cfg = config.parse(json.dumps(dict(BASE, review=recommended())))
        self.assertEqual(cfg.review, rc.parse_review(recommended()))

    def test_example_paths_are_made_up(self):
        """The fork is public: the shipped example holds only these made-up paths (a change here is a deliberate one)."""
        r = rc.parse_review(recommended())
        self.assertEqual(sorted({g for rule in r.rules for g in rule.paths} | {x.prompt for x in r.lenses}),
                         ['**', '**/*.md', '.github/lenses/approach.md', '.github/lenses/code.md',
                          '.github/lenses/data.md', 'docs/**', 'migrations/**', 'src/storage/**'])

    def test_readme_names_every_key(self):
        with open(README, encoding='utf-8') as f:
            text = f.read()
        section = text[text.index('## Review rules'):text.index('## Lens comments')]
        for k in rc.KEYS:
            self.assertIn(f'`{k}`', section)
        self.assertIn('release first, then pin', section)
        self.assertIn('`.github/workflows/**`', section)


class StateQuestion(unittest.TestCase):
    """Runs again, meets old, empty or newer state."""

    def test_config_without_review_is_todays(self):
        cfg = config.parse(json.dumps(BASE))
        self.assertIsNone(cfg.review)
        self.assertEqual(cfg, config.Config(
            required_checks=frozenset(['build', 'test']), merge_method='squash', bot_logins=('review-bot',),
            lens_author_associations=('OWNER',), override_prefix='tool ready --head', override_association='OWNER',
            ready_command='tool ready {pr}', push_command='tool push', lens_format_doc='docs/review.md',
            head_check='test'))

    def test_empty_object_names_every_missing_key(self):
        why = refusal({})
        for k in rc.KEYS:
            self.assertIn(f'{k} is missing', why)

    def test_null_and_list_are_refused(self):
        self.assertIn('must be an object', refusal(None))
        self.assertIn('must be an object', refusal([]))
        with self.assertRaisesRegex(config.ConfigError, r'review is null: must be an object'):
            config.parse(json.dumps(dict(BASE, review=None)))

    def test_newer_version_is_named_alone(self):
        block = dict(recommended(), version=2, rules='a v2 shape', extra=True)
        why = refusal(block)
        self.assertIn('version 2 is not 1', why)
        self.assertIn('update the pinned release', why)
        self.assertNotIn('unknown key', why)
        self.assertNotIn('rules', why)

    def test_odd_versions(self):
        for v in (0, '1', 1.0, True, None):
            self.assertIn('is not 1', refusal(dict(recommended(), version=v)), v)
            self.assertNotIn('update the pinned', refusal(dict(recommended(), version=v)))

    def test_key_written_twice(self):
        text = json.dumps(dict(BASE, review=recommended()))
        text = text.replace('"mode": "shadow"', '"mode": "enforce", "mode": "shadow"')
        with self.assertRaisesRegex(config.ConfigError, 'key written twice: mode'):
            config.parse(text)

    def test_only_light_rules_matching_is_allowed_but_the_catch_all_is_not_light(self):
        """A light rule before the catch-all may need no lens; a light catch-all would leave every new folder with
        none."""
        self.assertEqual(rc.parse_review(recommended()).rules[1].weight, 'light')
        block = recommended()
        block['rules'][2]['weight'] = 'light'
        block['rules'][2]['lenses'] = []
        self.assertIn('is the catch-all rule (a glob that matches every path, such as **) and must not be light',
                      refusal(block))

    def test_same_lens_key_in_lenses(self):
        block = recommended()
        block['lenses']['Data-1 r2'] = {'prompt': '.github/lenses/data.md'}
        self.assertIn("'data-1' and 'Data-1 r2' are one lens to the gate", refusal(block))

    def test_same_lens_key_in_a_rule_is_one_lens(self):
        block = recommended()
        block['lenses'] = {'data r1': {'prompt': 'a.md'}, 'code': {'prompt': 'c.md'}, 'approach': {'prompt': 'p.md'}}
        block['rules'][0]['lenses'] = ['data r1', 'data r2']
        why = refusal(block)
        self.assertIn("rules[0] ('data'): critical needs 2 different lenses, found 1", why)
        self.assertIn("'data r2' has no entry in lenses", why)

    def test_unknown_key_refusal_names_review_on_an_older_release(self):
        """An older release has no `review` row: it refuses the whole config and names the key."""
        old = {k: v for k, v in config.KEYS.items() if k != 'review'}
        with mock.patch.object(config, 'KEYS', old):
            with self.assertRaisesRegex(config.ConfigError, r"unknown key 'review'"):
                config.parse(json.dumps(dict(BASE, review=recommended())))

    def test_parsing_twice_gives_an_equal_value(self):
        text = json.dumps(dict(BASE, review=recommended()))
        a, b = config.parse(text), config.parse(text)
        self.assertEqual(a, b)
        self.assertEqual(hash(a), hash(b))
        self.assertIsNot(a.review, b.review)

    def test_frozen(self):
        r = rc.parse_review(recommended())
        with self.assertRaises(Exception):
            r.mode = 'enforce'
        self.assertIsInstance(r.rules, tuple)
        self.assertIsInstance(r.rules[0].paths, tuple)

    def test_input_is_not_changed(self):
        block = recommended()
        before = copy.deepcopy(block)
        rc.parse_review(block)
        config.parse(json.dumps(dict(BASE, review=block)))
        self.assertEqual(block, before)


class Refusals(unittest.TestCase):
    def test_every_problem_at_once(self):
        block = recommended()
        block['mode'] = 'loud'
        block['findings'] = 'yes'
        block['rules'][0]['lenses'] = ['data-1']
        block['rounds']['max'] = 2
        block['surprise'] = 1
        why = refusal(block)
        for part in ("unknown key 'surprise'", 'mode is "loud"', 'findings is "yes"',
                     'critical needs 2 different lenses, found 1', 'rounds.max 2 is below rounds.approach_at 3'):
            self.assertIn(part, why)

    def test_unknown_and_missing_keys_inside(self):
        block = recommended()
        block['rules'][0]['weigth'] = 'critical'
        del block['rules'][0]['weight']
        block['lenses']['code']['model'] = 'x'
        block['rounds'].pop('approach_lens')
        block['min_lenses'].pop('light')
        why = refusal(block)
        self.assertIn("rules[0]: unknown key 'weigth'", why)
        self.assertIn('rules[0]: weight is missing', why)
        self.assertIn("lenses['code']: unknown key 'model'", why)
        self.assertIn('rounds: approach_lens is missing', why)
        self.assertIn('min_lenses: light is missing', why)

    def test_names_a_lens(self):
        for bad in ('<lens>', 'r2', 're-check', ' code', '', '***'):
            block = recommended()
            block['lenses'][bad] = {'prompt': 'x.md'}
            self.assertIn('names no lens', refusal(block), bad)
        block = recommended()
        block['rules'][2]['lenses'] = ['code', 'r3']
        self.assertIn('rules[2] (\'code\').lenses: "r3" names no lens', refusal(block))
        block = recommended()
        block['rounds']['approach_lens'] = '<approach>'
        self.assertIn('rounds.approach_lens "<approach>" names no lens', refusal(block))

    def test_lens_without_entry(self):
        block = recommended()
        block['rules'][2]['lenses'] = ['Code']
        self.assertIn("'Code' has no entry in lenses", refusal(block))
        block = recommended()
        block['rounds']['approach_lens'] = 'design'
        self.assertIn("rounds.approach_lens 'design' has no entry in lenses", refusal(block))

    def test_no_catch_all(self):
        block = recommended()
        block['rules'][2]['paths'] = ['src/**']
        self.assertIn("no rule has the catch-all path '**'", refusal(block))

    def test_rule_after_catch_all(self):
        block = recommended()
        block['rules'].append({'name': 'late', 'weight': 'critical', 'paths': ['db/**'], 'lenses': ['data-1', 'data-2']})
        self.assertIn("rules[3] ('late') comes after the catch-all rule", refusal(block))

    def test_every_path_glob_is_the_catch_all_however_written(self):
        """`**/*`, `*/**`, `**/?*` match every path like `**`: such a rule first and light would need no lens anywhere,
        and a critical rule below it could never match (both were accepted when only the text `**` counted)."""
        for g in ('**/*', '*/**', '**/?*', '**/**', '**/*?*'):
            block = recommended()
            block['rules'].insert(0, {'name': 'all', 'weight': 'light', 'paths': ['docs/**', g], 'lenses': []})
            self.assertIn("rules[0] ('all') is the catch-all rule", refusal(block), g)
            block = recommended()
            block['rules'].insert(0, {'name': 'all', 'weight': 'normal', 'paths': [g], 'lenses': ['code']})
            why = refusal(block)
            self.assertIn("rules[1] ('data') comes after the catch-all rule", why, g)
            block = recommended()
            block['rules'][2]['paths'] = [g]
            self.assertEqual(rc.parse_review(block).rules[2].paths, (g,), g)

    def test_globs_that_miss_some_path_are_no_catch_all(self):
        for g in ('*', '*/*/**', '**/??', '**/*.md', 'src/**', '**/a*', '?', '**/?', '?/**', '**/?/**', '**/??*'):
            self.assertFalse(rc.matches_every_path(g), g)
            block = recommended()
            block['rules'][2]['paths'] = [g]
            self.assertIn("no rule has the catch-all path '**'", refusal(block), g)
        for g in ('**', '**/*', '*/**', '**/*/**', '**/*?', '**/?*', '**/*?*', '**/**/*'):
            self.assertTrue(rc.matches_every_path(g), g)

    def test_lens_name_the_gate_reads_as_another(self):
        """`data *2*` is `data 2` to the gate (it drops Markdown before reading the name); `x reviewer: y` is `x`."""
        for bad in ('data *2*', 'data `2`', 'code reviewer: me', 'code read at: abcdef1', '_code_ more'):
            block = recommended()
            block['lenses'][bad] = {'prompt': '.github/lenses/data.md'}
            self.assertIn(f'lens name {json.dumps(bad)} reads as', refusal(block), bad)
        block = recommended()
        block['lenses']['data 2'] = {'prompt': '.github/lenses/data.md'}
        block['lenses']['data reviewer 2'] = {'prompt': '.github/lenses/data.md'}
        block['rules'][0]['lenses'] = ['data 2', 'data reviewer 2']
        rc.parse_review(block)

    def test_unknown_key_in_min_lenses_is_named_not_a_crash(self):
        block = recommended()
        block['min_lenses']['huge'] = 3
        why = refusal(block)
        self.assertIn("min_lenses: unknown key 'huge'", why)

    def test_no_critical_rule(self):
        block = recommended()
        del block['rules'][0]
        self.assertIn('no critical rule', refusal(block))

    def test_empty_lenses_only_on_light(self):
        for weight in ('critical', 'normal'):
            block = recommended()
            block['rules'][0]['weight'] = weight
            block['rules'][0]['lenses'] = []
            self.assertIn(f'is {weight} with no lenses', refusal(block))
        block = recommended()
        block['min_lenses']['light'] = 1
        block['min_lenses']['normal'] = 1
        self.assertIn("rules[1] ('docs'): light needs 1 different lens, found 0", refusal(block))

    def test_min_lenses_values(self):
        for bad in (-1, 11, '2', True, 1.5):
            block = recommended()
            block['min_lenses']['normal'] = bad
            self.assertIn('min_lenses.normal is', refusal(block), bad)
        block = recommended()
        block['min_lenses'] = {'critical': 1, 'normal': 2, 'light': 0}
        self.assertIn('critical >= normal >= light', refusal(block))

    def test_rounds(self):
        for k, bad in (('max', 0), ('max', 21), ('approach_at', '3'), ('approach_at', False)):
            block = recommended()
            block['rounds'][k] = bad
            self.assertIn(f'rounds.{k} is', refusal(block))
        block = recommended()
        block['rounds'] = {'approach_at': 4, 'max': 4, 'approach_lens': 'approach'}
        self.assertEqual(rc.parse_review(block).rounds, rc.Rounds(4, 4, 'approach'))
        block['rounds']['max'] = 3
        self.assertIn('rounds.max 3 is below rounds.approach_at 4', refusal(block))

    def test_rule_shape(self):
        block = recommended()
        block['rules'][1]['name'] = 'data'
        block['rules'][0]['weight'] = 'heavy'
        block['rules'][2]['paths'] = []
        block['rules'][2]['lenses'] = 'code'
        why = refusal(block)
        self.assertIn("rules[1]: rule name 'data' is written twice", why)
        self.assertIn('weight is "heavy"', why)
        self.assertIn('paths is []', why)
        self.assertIn('lenses is "code"', why)
        block = recommended()
        block['rules'][0]['lenses'] = ['data-1', 'data-2', 'data-1']
        self.assertIn('has a name written twice', refusal(block))
        block = recommended()
        block['rules'][0]['paths'] = ['a/**', 'a/**']
        self.assertIn('has a glob written twice', refusal(block))
        self.assertIn('rules is []', refusal(dict(recommended(), rules=[])))
        self.assertIn('rules[0] is "x": must be an object', refusal(dict(recommended(), rules=['x'])))
        for bad in ('', ' data', 'a\nb', 3):
            block = recommended()
            block['rules'][0]['name'] = bad
            self.assertIn('rules[0].name is', refusal(block), bad)

    def test_config_names_review_and_other_problems_together(self):
        block = recommended()
        block['mode'] = 'loud'
        with self.assertRaises(config.ConfigError) as e:
            config.parse(json.dumps(dict(BASE, merge_method='fast', review=block)), 'repo/.github/queue.json')
        why = str(e.exception)
        self.assertTrue(why.startswith('repo/.github/queue.json: '), why)
        self.assertIn('merge_method is "fast"', why)
        self.assertIn('review: mode is "loud"', why)

    def test_lenses_shape(self):
        self.assertIn('lenses is {}', refusal(dict(recommended(), lenses={})))
        block = recommended()
        block['lenses']['code'] = '.github/lenses/code.md'
        self.assertIn("lenses['code'] is", refusal(block))


class Globs(unittest.TestCase):
    GOOD = ('**', 'src/**', '**/*.md', 'a/**/b', '*.py', 'docs/?/x', '.github/workflows/**', 'a/b.c-d_e/f',
            '**/**', 'src/*/test/**')
    BAD = ('/src/**', 'src/**.py', 'a**', '**b/c', 'src/{a,b}/**', 'src/[ab]/**', '!src/**', 'src\\x', 'a//b',
           'a/', './a', 'a/../b', '..', '.', '', ' a', 'a\nb', 5, None)

    def test_dialect(self):
        for g in self.GOOD:
            self.assertIsNone(rc.glob_problem(g), g)
        for g in self.BAD:
            self.assertIsNotNone(rc.glob_problem(g), g)

    def test_a_bad_glob_refuses_the_config(self):
        block = recommended()
        block['rules'][0]['paths'] = ['src/{storage,db}/**', '/abs/**']
        why = refusal(block)
        self.assertIn('"src/{storage,db}/**" has a character the dialect does not have', why)
        self.assertIn('"/abs/**" must be repo-relative', why)

    def test_prompt_paths(self):
        for good in ('.github/lenses/data.md', 'review/x.md', 'a'):
            self.assertIsNone(rc.prompt_problem(good), good)
        for bad in ('/etc/x.md', '../x.md', 'a/../x.md', 'lenses/*.md', 'a?.md', '', 'a\\b.md', 3):
            self.assertIsNotNone(rc.prompt_problem(bad), bad)
        block = recommended()
        block['lenses']['code'] = {'prompt': '../outside.md'}
        why = refusal(block)
        self.assertIn("lenses['code'].prompt \"../outside.md\" has an empty, . or .. segment", why)
        self.assertNotIn('no entry', why)   # the entry's own problem, named once

    def test_readme_dialect_matches(self):
        """The README names the characters the parser refuses."""
        with open(README, encoding='utf-8') as f:
            text = f.read()
        self.assertTrue(re.search(r'no braces, character classes, `!`\s+negation or backslashes', text))


class RuleOrder(unittest.TestCase):
    """#348 item 3: a changed path belongs to the first rule that matches it, so a lighter rule above a heavier one
    takes the paths both match. `**/*.md` (light) above `src/**` (critical) judged `src/notes.md` light, with no lens.
    Refused in enforce, a warning in shadow (the rules decide nothing there yet). The catch-all rule is the default, so
    a lighter rule above it is the normal case (docs/** light above a normal **), never a problem."""

    @staticmethod
    def block(mode, *rules):
        block = recommended()
        block['mode'] = mode
        block['rules'] = [{'name': 'vault', 'weight': 'critical', 'paths': ['vault/**'],
                           'lenses': ['data-1', 'data-2']}]
        block['rules'] += [{'name': n, 'weight': w, 'paths': list(p), 'lenses': ['data-1', 'data-2'] if w == 'critical'
                           else ['code'] if w == 'normal' else []} for n, w, p in rules] + [
            {'name': 'rest', 'weight': 'normal', 'paths': ['**'], 'lenses': ['code']}]
        return block

    def test_a_lighter_rule_above_a_heavier_one_it_can_shadow_is_refused_in_enforce(self):
        for light, heavy in ((('docs', 'light', ['**/*.md']), ('src', 'critical', ['src/**'])),
                             (('docs', 'light', ['docs/**', '**/*.json']), ('cfg', 'critical', ['config/*.json'])),
                             (('api', 'normal', ['src/api/*.py']), ('auth', 'critical', ['src/*/a?.py'])),
                             (('notes', 'light', ['src/**/notes/**']), ('code', 'normal', ['src/core/**']))):
            with self.subTest(light=light, heavy=heavy):
                why = refusal(self.block('enforce', light, heavy))
                self.assertIn(f'rule {light[0]!r} ({light[1]}) comes before the heavier rule {heavy[0]!r} '
                              f'({heavy[1]})', why)
                self.assertIn('put the heavier rule first', why)
                fixed = rc.parse_review(self.block('enforce', heavy, light))   # heavier first: the same paths, safe
                self.assertEqual(fixed.warnings, ())

    def test_in_shadow_it_is_a_warning_and_the_block_parses(self):
        r = rc.parse_review(self.block('shadow', ('docs', 'light', ['**/*.md']), ('src', 'critical', ['src/**'])))
        self.assertEqual(len(r.warnings), 1, r.warnings)
        self.assertIn("rule 'docs' (light) comes before the heavier rule 'src' (critical)", r.warnings[0])
        self.assertIn("'**/*.md' and 'src/**' both match a path such as", r.warnings[0])

    def test_rules_whose_globs_never_meet_may_stand_in_any_order(self):
        for light, heavy in ((('docs', 'light', ['docs/**']), ('src', 'critical', ['src/**'])),
                             (('md', 'light', ['**/*.md']), ('py', 'critical', ['src/**/*.py'])),
                             (('one', 'light', ['a/?']), ('two', 'critical', ['a/??'])),
                             (('x', 'light', ['a/*/b']), ('y', 'critical', ['a/b'])),
                             (('img', 'light', ['**/*.png']), ('sql', 'critical', ['db/*.sql']))):
            with self.subTest(light=light, heavy=heavy):
                self.assertEqual(rc.parse_review(self.block('enforce', light, heavy)).warnings, ())

    def test_lighter_rules_above_the_catch_all_are_the_normal_case(self):
        for block in (recommended(), *(json.loads(json.dumps(c)) for c in VECTORS_CONFIGS.values())):
            for mode in ('shadow', 'enforce'):
                with self.subTest(mode=mode, rules=[r['name'] for r in block['rules']]):
                    self.assertEqual(rc.parse_review({**block, 'mode': mode}).warnings, ())

    def test_equal_weights_and_a_heavier_rule_above_are_fine(self):
        self.assertEqual(rc.parse_review(self.block('enforce', ('a', 'critical', ['src/**']),
                                                    ('b', 'critical', ['**/*.md']), ('c', 'light', ['**/*.md'])))
                         .warnings, ())

    def test_a_same_weight_rule_above_without_a_lens_of_the_lower_one_is_refused_in_enforce(self):   # #374 item 1
        # `src/**/*.py` critical [data-1, approach] above `src/**/*` critical [data-1, data-2]: `src/x.py` belongs to
        # the first rule and needs only its lenses, never data-2.
        def same(mode, first, second):
            block = self.block(mode)
            block['rules'][1:1] = [{'name': 'py', 'weight': 'critical', 'paths': ['src/**/*.py'], 'lenses': first},
                                   {'name': 'src', 'weight': 'critical', 'paths': ['src/**/*'], 'lenses': second}]
            return block
        why = refusal(same('enforce', ['data-1', 'approach'], ['data-1', 'data-2']))
        self.assertIn("rule 'py' (critical) comes before the rule 'src' of the same weight without its lens 'data-2'",
                      why)
        self.assertIn("'src/**/*.py' and 'src/**/*' both match a path such as", why)
        warned = rc.parse_review(same('shadow', ['data-1', 'approach'], ['data-1', 'data-2'])).warnings
        self.assertEqual(len(warned), 1, warned)
        self.assertIn("without its lens 'data-2'", warned[0])
        # the first rule holding every lens of the lower one (more, or in another order, is fine): safe. Names match
        # exactly: two names the gate reads as one lens are refused in `lenses` already.
        for first, second in ((['data-1', 'data-2', 'approach'], ['data-1', 'data-2']),
                              (['data-2', 'data-1'], ['data-1', 'data-2'])):
            with self.subTest(first=first, second=second):
                self.assertEqual(rc.parse_review(same('enforce', first, second)).warnings, ())
        # globs that never meet: any lenses
        block = same('enforce', ['data-1', 'approach'], ['data-1', 'data-2'])
        block['rules'][1]['paths'] = ['lib/**/*.py']
        self.assertEqual(rc.parse_review(block).warnings, ())

    def test_only_the_match_every_globs_of_the_catch_all_are_exempt(self):   # #374 item 2
        block = self.block('enforce', ('md', 'light', ['**/*.md']))
        block['rules'][-1] = {'name': 'all', 'weight': 'critical', 'paths': ['src/**', '**'],
                              'lenses': ['data-1', 'data-2']}
        why = refusal(block)
        self.assertIn("rule 'md' (light) comes before the heavier rule 'all' (critical), and '**/*.md' and 'src/**'",
                      why)
        block['rules'][-1]['paths'] = ['**']   # the match-every glob alone: the default for what no rule above takes
        self.assertEqual(rc.parse_review(block).warnings, ())
        # a same-weight catch-all with more lenses: its ** is exempt, its explicit glob is not
        block = self.block('enforce', ('py', 'normal', ['**/*.py']))
        block['rules'][-1] = {'name': 'rest', 'weight': 'normal', 'paths': ['**'], 'lenses': ['code', 'approach']}
        self.assertEqual(rc.parse_review(block).warnings, ())
        block['rules'][-1]['paths'] = ['src/**', '**']
        self.assertIn("rule 'py' (normal) comes before the rule 'rest' of the same weight without its lens "
                      "'approach'", refusal(block))

    def test_the_shadow_warning_says_enforce_refuses_it(self):   # #374 item 3: switching locks every head out
        r = rc.parse_review(self.block('shadow', ('docs', 'light', ['**/*.md']), ('src', 'critical', ['src/**'])))
        self.assertTrue(r.warnings[0].endswith('mode enforce refuses this config: fix it before switching'),
                        r.warnings[0])
        why = refusal(self.block('enforce', ('docs', 'light', ['**/*.md']), ('src', 'critical', ['src/**'])))
        self.assertNotIn('before switching', why)

    def test_enforce_order_problems_judges_a_block_as_enforce_would(self):   # #374 item 3: the head's new block
        bad = self.block('enforce', ('docs', 'light', ['**/*.md']), ('src', 'critical', ['src/**']))
        for mode in ('enforce', 'shadow'):
            with self.subTest(mode=mode):
                got = rc.enforce_order_problems({**bad, 'mode': mode})
                self.assertEqual(len(got), 1, got)
                self.assertIn("rule 'docs' (light) comes before the heavier rule 'src' (critical)", got[0])
                self.assertNotIn('before switching', got[0])
        for good in (self.block('enforce', ('src', 'critical', ['src/**']), ('docs', 'light', ['**/*.md'])),
                     recommended(), None, [], {}, {**bad, 'rules': 'x'}, {**bad, 'version': 2}):
            with self.subTest(good=json.dumps(good)[:60]):
                self.assertEqual(rc.enforce_order_problems(good), [])   # no order to judge: not this check's

    def test_every_problem_is_named_at_once(self):
        why = refusal(self.block('enforce', ('docs', 'light', ['**/*.md']), ('api', 'normal', ['src/**']),
                                 ('src', 'critical', ['src/**'])))
        self.assertIn("rule 'docs' (light) comes before the heavier rule 'api' (normal)", why)
        self.assertIn("rule 'docs' (light) comes before the heavier rule 'src' (critical)", why)
        self.assertIn("rule 'api' (normal) comes before the heavier rule 'src' (critical)", why)

    def test_overlap_of_two_globs(self):
        for a, b, meet in (('**/*.md', 'src/**', True), ('src/**', '**/*.md', True), ('**', 'x', True),
                           ('a/*', 'a/b/c', False), ('a/**', 'a', True), ('*.md', '*.py', False),
                           ('*a*', '*b*', True), ('a*', '*b', True), ('a?', '?b', True), ('a?', 'abc', False),
                           ('?', '??', False), ('*', '??', True), ('a/**/b', '**/c/**', True),
                           ('a/**/b', 'c/**', False), ('x*y*z', '*q*', True), ('docs/*.md', 'docs/x/**', False)):
            with self.subTest(a=a, b=b):
                self.assertEqual(rc.globs_meet(a, b), meet)
                self.assertEqual(rc.globs_meet(b, a), meet)

    def test_overlap_is_quick_on_long_globs(self):
        import time
        a, b = '/'.join(['**', '*a*'] * 40), '/'.join(['*b*', '**'] * 40)
        start = time.monotonic()
        self.assertTrue(rc.globs_meet(a, b))
        self.assertFalse(rc.globs_meet('*' * 200 + 'x', '*' * 200 + 'y'))
        self.assertLess(time.monotonic() - start, 2)


if __name__ == '__main__':
    unittest.main()
