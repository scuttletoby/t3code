"""Tests of review_judge.py (run: python3 fork-ci/actions/pr-queue/review_judge.test.py).

Which lens reads a change needs by the `review` block's path rules. The shared vectors (review.vectors.json, `cases`)
are the contract both sides test; the rest here: reading git's name-status output, and that a rule set the parser
would refuse, or a diff line nobody knows, never judges.
"""
import dataclasses, json, os, sys, unittest

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import review_config as rc  # noqa: E402
import review_judge as rj  # noqa: E402

with open(os.path.join(HERE, 'review.vectors.json'), encoding='utf-8') as _f:
    VECTORS = json.load(_f)
CONFIG_PATH = VECTORS['config_path']
CONFIGS = {name: rc.parse_review(block) for name, block in VECTORS['configs'].items()}


def name_status(changes):
    """git diff --name-status -z output for the vector's changes."""
    return ''.join(f + '\0' for change in changes for f in change)


def judged(case):
    return rj.judge(CONFIGS[case['config']], rj.parse_name_status(name_status(case['changes'])), CONFIG_PATH)


class Vectors(unittest.TestCase):
    def test_every_shared_case(self):
        self.assertGreaterEqual(len(VECTORS['cases']), 20)
        for case in VECTORS['cases']:
            with self.subTest(case['name']):
                j = judged(case)
                self.assertEqual(j.weight, case['weight'])
                self.assertEqual(list(j.lenses), case['lenses'])
                self.assertEqual([h.rule for h in j.hits], case['hits'])
                self.assertEqual(list(j.protected), case['protected'])

    def test_the_vectors_cover_the_cases_the_brief_names(self):
        cases = VECTORS['cases']
        statuses = {c[0][0] for case in cases for c in case['changes']}
        self.assertLessEqual({'A', 'C', 'D', 'M', 'R', 'T'}, statuses)
        self.assertTrue(any(case['weight'] == 'light' for case in cases), 'a light-only change')
        self.assertTrue(any(case['changes'] == [] for case in cases), 'an empty diff')
        protected = {p for case in cases for p in case['protected']}
        self.assertIn(CONFIG_PATH, protected)
        self.assertTrue(any(p.startswith('.github/workflows/') for p in protected))
        prompts = {x.prompt for r in CONFIGS.values() for x in r.lenses}
        self.assertTrue(protected & prompts, 'a lens prompt file')
        self.assertTrue(all(rj.catch_all(r) for r in CONFIGS.values()), 'no path can match no rule')

    def test_every_config_is_a_whole_valid_review_block(self):   # the TypeScript parser reads the same blocks
        for name, block in VECTORS['configs'].items():
            with self.subTest(name):
                base = {'required_checks': ['t'], 'merge_method': 'squash', 'bot_logins': [],
                        'lens_author_associations': ['OWNER'],
                        'override': {'prefix': 'ok', 'author_association': 'OWNER'}, 'ready_command': 'r {pr}',
                        'push_command': 'p', 'lens_format_doc': 'd', 'head_check': 't', 'review': block}
                self.assertEqual(config.parse(json.dumps(base)).review, CONFIGS[name])

    def test_the_same_input_gives_the_same_judgement(self):   # runs again: nothing stored, nothing read but the input
        for case in VECTORS['cases']:
            self.assertEqual(judged(case), judged(case))

    def test_the_summary_line_names_weight_lenses_and_why(self):
        case = next(c for c in VECTORS['cases'] if c['name'] == 'the config file itself is critical')
        text = judged(case).text()
        self.assertTrue(text.startswith('critical, lenses data-1, data-2 (1 changed path; '), text)
        self.assertIn('protected: .github/pr-queue.json', text)
        empty = next(c for c in VECTORS['cases'] if c['changes'] == [] and c['config'] == 'basic')
        self.assertIn('code (normal): no changed path', judged(empty).text())
        light = next(c for c in VECTORS['cases'] if c['weight'] == 'light')
        self.assertTrue(judged(light).text().startswith('light, lenses none'))

    def test_a_long_list_of_paths_is_cut_in_the_summary(self):
        changes = [('M', (f'src/app/f{i}.py',)) for i in range(5000)]
        j = rj.judge(CONFIGS['basic'], changes, CONFIG_PATH)
        self.assertEqual((j.weight, j.paths), ('normal', 5000))
        self.assertIn('and 4995 more', j.text())
        self.assertLess(len(j.text()), 400)

    def test_a_config_path_written_with_dot_slash_is_the_same_file(self):
        j = rj.judge(CONFIGS['basic'], [('M', (CONFIG_PATH,))], './' + CONFIG_PATH)
        self.assertEqual((j.weight, j.protected), ('critical', (CONFIG_PATH,)))


class NameStatus(unittest.TestCase):
    def test_reads_every_status_git_prints_for_two_trees(self):
        text = 'M\0a\0A\0b c\0D\0d\0T\0t\0R100\0old\0new\0C075\0src\0dst\0'
        self.assertEqual(rj.parse_name_status(text), [('M', ('a',)), ('A', ('b c',)), ('D', ('d',)), ('T', ('t',)),
                                                      ('R', ('old', 'new')), ('C', ('src', 'dst'))])

    def test_no_change_is_an_empty_list(self):
        self.assertEqual(rj.parse_name_status(''), [])

    def test_a_path_with_a_tab_newline_or_quote_is_read_whole(self):   # -z: git does not quote
        self.assertEqual(rj.parse_name_status('M\0a\tb\nc"d\0'), [('M', ('a\tb\nc"d',))])

    def test_anything_else_is_refused(self):
        for text in ('M\0a', 'X\0a\0', 'U\0a\0', 'B\0a\0', 'R\0a\0b\0', 'Rxx\0a\0b\0', 'R100\0a\0', 'M\0\0',
                     'M100\0a\0', 'MM\0a\0', '\0', 'R10000\0a\0b\0'):
            with self.subTest(text=text):
                with self.assertRaises(rj.JudgeError):
                    rj.parse_name_status(text)


class Refusals(unittest.TestCase):
    """A rule set the parser refuses never reaches the judge; if one is built by hand, the judge refuses too."""
    def test_a_path_no_rule_matches_is_refused(self):
        rules = CONFIGS['basic']
        no_catch_all = dataclasses.replace(rules, rules=rules.rules[:2])
        with self.assertRaises(rj.JudgeError) as e:
            rj.judge(no_catch_all, [('M', ('src/app/a.py',))], CONFIG_PATH)
        self.assertIn("no rule matches the changed path 'src/app/a.py'", str(e.exception))
        with self.assertRaises(rj.JudgeError):
            rj.judge(no_catch_all, [], CONFIG_PATH)   # an empty change with no catch-all rule

    def test_a_protected_path_with_no_critical_rule_is_refused(self):
        rules = CONFIGS['basic']
        no_critical = dataclasses.replace(rules, rules=rules.rules[1:])
        self.assertEqual(rj.judge(no_critical, [('M', ('src/a.py',))], CONFIG_PATH).weight, 'normal')
        for path in (CONFIG_PATH, '.github/lenses/code.md', '.github/workflows/x.yml'):
            with self.subTest(path):
                with self.assertRaises(rj.JudgeError) as e:
                    rj.judge(no_critical, [('M', (path,))], CONFIG_PATH)
                self.assertIn('no critical rule', str(e.exception))

    def test_the_parser_refuses_those_rule_sets_first(self):
        block = VECTORS['configs']['basic']
        for rules in (block['rules'][:2], block['rules'][1:]):
            with self.assertRaises(config.ConfigError):
                rc.parse_review({**block, 'rules': rules})


if __name__ == '__main__':
    unittest.main()
