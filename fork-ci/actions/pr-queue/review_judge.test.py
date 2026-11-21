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

    def test_a_config_path_git_reads_but_the_protected_set_would_miss_is_refused(self):   # #348 item 2
        # git show base:<path> reads all of these, but a changed path is never spelled so: the config would be
        # unprotected. Refused with the rule the lens prompt paths follow (after one leading ./).
        for spelled in ('././' + CONFIG_PATH, './.github//pr-queue.json', '.github//pr-queue.json',
                        '.github/./pr-queue.json', '/' + CONFIG_PATH, '.github/x/../pr-queue.json'):
            with self.subTest(spelled):
                with self.assertRaises(rj.JudgeError) as e:
                    rj.judge(CONFIGS['basic'], [('M', ('docs/a.md',))], spelled)
                self.assertIn(f'the config path {spelled!r}', str(e.exception))

    def test_a_config_name_with_glob_characters_is_its_exact_name(self):   # #374 item 4: git reads such names
        # The protected set is compared exactly, never as a glob: `queue[main].json` protects that file and no other.
        for name in ('.github/queue[main].json', '.github/q*.json', '.github/q?.json', '.github/{a,b}.json',
                     '.github/!q.json', '.github/q\\x.json'):
            with self.subTest(name):
                j = rj.judge(CONFIGS['basic'], [('M', ('docs/a.md',)), ('M', (name,))], './' + name)
                self.assertEqual((j.weight, j.protected), ('critical', (name,)))
                j = rj.judge(CONFIGS['basic'], [('M', ('docs/a.md',)), ('M', ('.github/qx.json',))], name)
                self.assertEqual((j.weight, j.protected), ('normal', ()))   # another file the glob would match

    def test_a_path_that_is_not_utf8_is_judged_and_shown_escaped(self):   # #348 item 1: git -z, surrogateescape
        bad = b'src/storage/bad\xff.py'.decode('utf-8', 'surrogateescape')
        j = rj.judge(CONFIGS['basic'], rj.parse_name_status(f'A\0{bad}\0'), CONFIG_PATH)
        self.assertEqual((j.weight, j.lenses), ('critical', ('data-1', 'data-2')))
        self.assertIn('src/storage/bad\\xff.py', j.text())
        j.text().encode('utf-8')   # the summary is written as UTF-8: no lone surrogate may reach it


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


# --- findings and rounds (issues #99, #100): judge_rounds on lens comments ---------------------------------------
H1, H2, H3, H4, H5 = (c * 40 for c in 'abcde')
BASIC = CONFIGS['basic']   # findings true, rounds approach_at 3, max 4, approach lens `approach`


def rounds(history, newest, overridden=(), review=BASIC):
    return rj.judge_rounds(review, list(history), dict(newest), set(overridden))


class Findings(unittest.TestCase):
    def test_blocker_and_major_lines_are_findings_minor_ones_are_not(self):
        body = ('lens: code · reviewer: x · read at: ' + H1 + '\n'
                'blocker: the lock is gone\n- **Major:** two writers race\nminor: a typo\n### major 2: a retry loops\n'
                'Majors (state): stopped half-way leaves a row\n> major: quoted from round 1\n```\nmajor: in code\n```')
        self.assertEqual([k for k, _ in rj.findings(body)], ['blocker', 'major', 'major', 'major', 'major', 'major'])
        self.assertEqual(rj.findings(body)[0], ('blocker', 'blocker: the lock is gone'))

    def test_only_an_explicit_nothing_is_no_finding(self):
        for line in ('major: none', 'Major: None.', 'blocker: nothing', '**blockers:** no', 'major: 0', 'major: n/a',
                     'major: NA', 'major: -', 'major: —', '- majors: none'):
            with self.subTest(line):
                self.assertEqual(rj.findings(line), [])
        for line in ('major:', 'major: no lock on the write path', 'major: none of the retries stop', 'major: - x',
                     'blocker: see below'):
            with self.subTest(line):
                self.assertEqual(len(rj.findings(line)), 1)

    def test_a_line_that_only_mentions_a_finding_is_none(self):
        for line in ('fixed: the major of round 1', 'no major found', 'majority: x', 'Major version: 2', 'blockerx: y',
                     'major 12345: too long a number'):
            with self.subTest(line):
                self.assertEqual(rj.findings(line), [])

    def test_html_and_links_cannot_hide_a_finding(self):   # read both ways, as the lens header is
        for line in ('<b>major</b>: x', '[major](http://e/x): y', '<details><summary>major: x</summary>',
                     'major (see <b ): x <i>'):   # the last only the narrow read finds: the wide tag eats the colon
            with self.subTest(line):
                self.assertEqual(len(rj.findings(line)), 1, line)


class Rounds(unittest.TestCase):
    def test_each_different_full_sha_read_is_a_round(self):
        self.assertEqual(rj.review_round([]), 0)
        self.assertEqual(rj.review_round([('code', H1), ('data-1', H1)]), 1)
        self.assertEqual(rj.review_round([('code', H1), ('code', H2), ('data-1', H2), ('code', H3)]), 3)
        self.assertEqual(rj.review_round([('code', 'abcdef1'), ('code', None)]), 0)   # a short sha or none: no round

    def test_a_round_marker_counts_when_it_is_higher(self):   # deleted old rounds: the marker still says it
        self.assertEqual(rj.review_round([('code r3', H1)]), 3)
        self.assertEqual(rj.review_round([('code **r4**', H1), ('data r2', H2)]), 4)
        self.assertEqual(rj.review_round([('code r1', H1), ('code r1', H2), ('code', H3)]), 3)
        self.assertEqual(rj.round_marker('r2d2 lens'), 0)
        self.assertEqual(rj.round_marker('r9'), 0)   # a name's first word is its name, never a marker

    def test_no_lens_comment_yet_is_round_zero_and_passes(self):   # empty state
        r = rounds([], {})
        self.assertEqual((r.round, r.approach, r.over_cap, r.findings, r.problems), (0, False, False, (), ()))

    def test_a_major_blocks_until_a_later_round_of_that_lens_clears_it(self):
        r = rounds([('code', H1)], {'code': 'major: two writers race'})
        self.assertEqual(r.findings, (('code', 'major', 'major: two writers race'),))
        self.assertIn('lens code reported a major that no later round cleared', r.problems[0])
        self.assertIn('read the fixed head in a new comment', r.problems[0])
        r = rounds([('code', H1), ('code r2', H2)], {'code': 'major: none'})   # round 2 cleared it
        self.assertEqual((r.round, r.problems), (2, ()))

    def test_an_override_newer_than_the_report_clears_its_findings(self):   # #99: only a person's override
        r = rounds([('code', H1), ('data-1', H1)], {'code': 'blocker: x', 'data-1': 'major: y'}, overridden={'code'})
        self.assertEqual([f[0] for f in r.findings], ['data-1'])
        r = rounds([('code', H1)], {'code': 'blocker: x'}, overridden={'code'})
        self.assertEqual(r.problems, ())

    def test_a_lens_that_reads_nothing_is_not_judged_here(self):   # it fails the head already (edited, hidden)
        r = rounds([('code', H1)], {'code': None})
        self.assertEqual(r.problems, ())

    def test_findings_false_judges_no_finding(self):
        r = rounds([('code', H1)], {'code': 'blocker: x'}, review=dataclasses.replace(BASIC, findings=False))
        self.assertEqual(r.problems, ())
        self.assertIn('findings are not judged', r.text(dataclasses.replace(BASIC, findings=False)))

    def test_the_approach_lens_is_required_from_round_approach_at(self):
        self.assertFalse(rounds([('code', H1), ('code', H2)], {'code': ''}).approach)
        r = rounds([('code', H1), ('code', H2), ('code', H3)], {'code': ''})
        self.assertTrue(r.approach)
        self.assertIn('approach lens approach required from round 3', r.text(BASIC))

    def test_a_round_past_the_cap_stops_and_a_person_decides(self):   # #100
        four = [('code', h) for h in (H1, H2, H3, H4)]
        self.assertEqual(rounds(four, {'code': ''}).problems, ())   # round 4 of 4 may pass
        r = rounds(four + [('code', H5)], {'code': ''})
        self.assertTrue(r.over_cap)
        self.assertEqual(r.problems, ('review round 5 is past the cap of 4 rounds (issue #100): no more rounds; a '
                                      'person decides: an override comment for the head, or close the pull request',))
        self.assertEqual(rounds([('code r5', H1)], {'code': ''}).round, 5)

    def test_an_override_covering_every_lens_passes_the_cap_a_newer_round_does_not(self):
        five = [('code', h) for h in (H1, H2, H3, H4, H5)] + [('approach', H5)]
        self.assertEqual(rounds(five, {'code': '', 'approach': ''}, overridden={'code', 'approach'}).problems, ())
        r = rounds(five, {'code': '', 'approach': ''}, overridden={'code'})   # approach posted after the override
        self.assertTrue(r.over_cap)

    def test_a_finding_at_the_last_round_says_no_more_rounds(self):
        r = rounds([('code', h) for h in (H1, H2, H3, H4)], {'code': 'major: still racy'})
        self.assertIn('this is round 4 of 4: no more rounds; a person decides', r.problems[0])

    def test_the_same_comments_give_the_same_verdict(self):   # runs again: pure
        args = ([('code', H1), ('code r2', H2)], {'code': 'major: x'}, {'data-1'})
        self.assertEqual(rounds(*args), rounds(*args))


# --- the fork's own config (issue #730): in enforce, workflow, queue and lens files critical (#82) ----------------
ROOT = os.path.join(HERE, '..', '..', '..')
FORK_CONFIG = '.github/fork-pr-queue.json'


class ForkConfig(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, FORK_CONFIG), encoding='utf-8') as f:
            self.cfg = config.parse(f.read(), FORK_CONFIG)
        self.review = self.cfg.review

    def test_it_parses_in_enforce_with_the_owners_numbers(self):
        r = self.review
        self.assertEqual((r.mode, r.findings, r.rounds.approach_at, r.rounds.max, r.warnings),
                         ('enforce', True, 3, 4, ()))
        self.assertGreaterEqual(r.min_lenses.critical, r.min_lenses.normal + 1)   # #82: one extra review
        for x in r.lenses:
            with self.subTest(x.name):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, x.prompt)), x.prompt)

    def test_workflow_queue_gate_config_and_lens_files_are_critical(self):   # #82
        critical = next(x for x in self.review.rules if x.weight == 'critical')
        for path in ('.github/workflows/fork-head-gate.yml', 'fork-ci/actions/pr-queue/review_judge.py',
                     'fork-ci/actions/head-check/head_check.py', FORK_CONFIG, '.github/fork-lens-state.md',
                     '.github/actions/setup-ci-deps/action.yml', '.github/fork-ci-release-rulesets.json'):
            with self.subTest(path):
                j = rj.judge(self.review, [('M', (path,))], FORK_CONFIG)
                self.assertEqual((j.weight, j.lenses), ('critical', critical.lenses))
                self.assertGreaterEqual(len(j.lenses), 2)
        j = rj.judge(self.review, [('M', ('apps/server/src/x.ts',))], FORK_CONFIG)
        self.assertEqual(j.weight, 'normal')
        self.assertTrue(j.lenses)


if __name__ == '__main__':
    unittest.main()
