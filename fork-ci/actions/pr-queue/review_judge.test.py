"""Tests of review_judge.py (run: python3 fork-ci/actions/pr-queue/review_judge.test.py).

Which lens reads a change needs by the `review` block's path rules. The shared vectors (review.vectors.json, `cases`)
are the contract both sides test; the rest here: reading git's name-status output, and that a rule set the parser
would refuse, or a diff line nobody knows, never judges.
"""
import dataclasses, json, os, sys, time, unittest

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
BASIC = CONFIGS['basic']   # findings true, rounds approach_at 3, max 4, approach lens `approach`, no override_authors
PEOPLE = dataclasses.replace(BASIC, override_authors=('a-person',))
HEAD = 'lens: code · reviewer: x · read at: ' + H1 + '\n'
NONE = 'blocker: none\nmajor: none'   # the clean findings section the lens prompts ask for


def rounds(history, reports, decided=False, review=BASIC, change_of=None):
    """judge_rounds with each lens's newest entry a report that counts, unless given as (how, body)."""
    reports = {k: v if isinstance(v, tuple) else (rj.REPORT, v) for k, v in reports.items()}
    return rj.judge_rounds(review, list(history), reports, decided, change_of)


# Findings as the fork's lens reports write them (pull requests #720-#739) and as reviews A and B of #740 showed they
# can be written: each must count as a blocker or major finding.
FINDING = [
    ('heading, numbered item', '### Blocking\n\n1. **The lock is gone.** Two writers race on the row.'),
    ('heading, bold item', '### Major\n**B1. The retry loops forever.**'),
    ('heading with a note', '### Blocking (merge, not safety)\n1. The queue merges an unread head.'),
    ('heading with a long note', '### Major (older than this PR; a follow-up issue is fine)\n- the cache never expires'),
    ('bold inline', '**Blocking:** the lock is gone.'),
    ('bold bare, item under it', '**Major**\n- two writers race'),
    ('plain', 'Blocking: x'),
    ('findings word', 'Major findings: x'),
    ('findings word, list under it', 'Major findings:\n1. x'),
    ('dash list', '- major — x'),
    ('en dash', 'major – x'),
    ('hyphen', 'major - x'),
    ('emoji', '🔴 Major: x'),
    ('emoji, bold', '⚠️ **Blocker**: x'),
    ('numbered label', 'major 2: a retry loops'),
    ('plural with a note', 'Majors (state): stopped half-way leaves a row'),
    ('brackets', '[major] x'),
    ('second label of a line', 'Blocker: none, major: two writers race'),
    ('after a semicolon', 'blocker: none; major: x'),
    ('after and', 'blocker: none and major: x'),
    ('table row', '| severity | finding |\n|---|---|\n| major | x |'),
    ('table row, bold', '| **Blocker** | the lock is gone |'),
    ('table column', '| blocker | major |\n| --- | --- |\n| none | two writers race |'),
    ('fullwidth M (NFKC)', '\uff2dajor: x'),
    ('zero-width space', 'maj\u200bor: x'),
    ('zero-width joiner in heading', '### Bl\u200docking\n1. x'),
    ('soft hyphen', 'ma\u00adjor: x'),
    ('Cyrillic o', 'maj\u043er: x'),
    ('Cyrillic M', '\u041cajor: x'),
    ('Greek omicron', 'bl\u03bfcker: x'),
    ('digit zero for o', 'maj0r: x'),
    ('heading, code under it', '### Major\n```\nthe race is in x\n```'),
    ('heading, nothing under it', '### Major'),
    ('heading, next heading under it', '### Major\n### Minor\n- a typo'),
    ('bare label, nothing under it', 'major:'),
    ('a none that is not one', 'major: none of the retries stop'),
    ('a no that is not one', 'major: no lock on the write path'),
    ('a dash that is not one', 'major: - x'),
    ('see below', 'blocker: see below'),
    ('quoted (read: fail closed)', '> major: quoted from round 1'),
    ('indented (read: fail closed)', '    major: in an indented block'),
    ('HTML tag', '<b>major</b>: x'),
    ('link', '[major](http://e/x): y'),
    ('summary tag', '<details><summary>major: x</summary>'),
    ('narrow read only', 'major (see <b ): x <i>'),   # the wide tag eats the colon
    ('NOT SAFE header', '## Review lens A, head 0123abc: **NOT SAFE**\nblocker: none\nmajor: none'),
    ('NOT SAFE verdict', 'verdict: NOT SAFE\nblocker: none\nmajor: none'),
    ('finding line', 'finding: major src/a.py:3 loses data'),   # another project's lens format
    ('finding line, blocker', '- finding: blocker the lock is gone'),
    ('finding line after blocker: none', 'blocker: none\nfinding: major src/a.py:3 loses data'),
    ('finding line after findings: none', 'findings: none\nfinding: major x'),
    ('recap of a fixed major', NONE + '\n- Major 1 (cap): fixed at the root.'),   # labelled: read as open
    ('recap of a fixed blocker', NONE + '\n**Blocking 1:** fixed.'),
    ('HTML entity', NONE + '\nma&#106;or: x'),
    ('Markdown escape', NONE + '\nmajor\\: x'),
    ('ratio sign for a colon', NONE + '\nmajor\u2236 x'),
    ('a dash glued to none', 'major: none-critical race'),
]
# Lines a clean report writes: no finding, and a findings section.
CLEAN = [
    ('both on one line', 'Blocker: none, major: none'),
    ('none found', 'Major: none found'),
    ('none with a note', 'major: none (only minors)'),
    ('none!', 'major: none!'),
    ('heading, none under it', '### Major\nnone'),
    ('headings, None.', '### Blocking\nNone.\n\n### Major\nNone.'),
    ('bold inline', '**Blocking:** none.\n**Major:** none.'),
    ('a rule between', '### Major\n\n---\n\nNone.'),
    ('table columns', '| blocker | major |\n|---|---|\n| none | none |'),
    ('table columns, blanks round the rule', '| blocker | major |\n  |---|---|  \n| none | none |'),
    ('table row', '| severity | finding |\n|---|---|\n| major | none |'),
    ('n/a', 'major: n/a'), ('zero', 'major: 0'), ('em dash', 'major: —'), ('list', '- majors: none'),
    ('bold no', '**blockers:** no'), ('nothing', 'blocker: nothing'), ('NA', 'major: NA'),
    ('minors do not block', 'major: none\n### Minor\n- a typo\nminor: x\nnit: y'),
    ('code block', 'major: none\n```\nmajor: in code\n### Blocking\n1. x\n```'),
    ('tilde fence', 'blocker: none\n~~~\nblocker: x\n~~~'),
    ('longer fence holds a shorter one', 'major: none\n````\n```\nmajor: x\n```\n````'),
    ('findings: none', 'findings: none'),
    ('Unicode none', '\u041cajor: n\u043ene'),
    ('both, full stops', 'Blocker: none. Major: none.'),
    ('nothing found', 'major: nothing found'),
    ('a minor after it', 'major: none; minor: x'),
    ('prose that says not safe', 'major: none\nThe old code was not safe to run twice; this fixes it.'),
    ('none, a dash, a note', 'major: none — two minors below'),
    ('none, a hyphen, a note', 'major: none - two minors below'),
    ('none in parentheses', 'Major: (none)'),
    ('none and an emoji', 'major: None \u2705'),
    ('only minor finding lines', 'finding: minor src/a.py:3 a typo\nfinding: nit y'),
    ('a recap without a label', NONE + '\nround 1, fixed: the race'),
]
# Each makes a findings section on its own: a blocker and a major label, `findings: none` or `finding:` lines.
SECTION = ['Blocker: none, major: none', '### Blocking\nNone.\n\n### Major\nNone.',
           '**Blocking:** none.\n**Major:** none.', '| blocker | major |\n|---|---|\n| none | none |', 'findings: none',
           'Blocker: none. Major: none.',
           'finding: minor a typo', NONE, '| severity | finding |\n|---|---|\n| blocker | none |\n| major | none |']
# Review A and B of #740 round 2: `blocker: none` kept from the prompt, the major written in a form not read as a label.
# With one label kind only there is no findings section, so each fails closed (no section), never clean.
ONE_KIND = ['F1 (major): x', '1. **Retry drops rows** (major): x', '- F1. Severity: major. x',
            '- F1 — **Severity:** Major — x', '- F1 — major: x', 'F1: major: x', 'One major: x', 'Major 1. x',
            'minor: typo. Also major: x']
ONE_LABEL = ['major: none', 'blocker: none', '### Major\nNone.', '| severity | finding |\n|---|---|\n| major | none |']
# Lines that only mention a label: no finding, and no findings section either.
MENTION = ['fixed: the major of round 1', 'no major found', 'majority: x', 'Major version: 2', 'blockerx: y',
           'major 12345: too long a number', 'the blocking call in x']


class Findings(unittest.TestCase):
    def test_each_real_finding_format_counts(self):
        for what, body in FINDING:
            with self.subTest(what):
                said, section = rj.read_findings(HEAD + body)
                self.assertTrue(said, body)
                self.assertTrue(said[0][0] in ('blocker', 'major'))

    def test_each_clean_format_is_no_finding(self):
        for what, body in CLEAN:
            with self.subTest(what):
                self.assertEqual(rj.read_findings(HEAD + body)[0], [], body)
                self.assertEqual(rj.read_findings(HEAD + NONE + '\n\n' + body), ([], True), body)

    def test_a_section_needs_a_blocker_and_a_major_label(self):   # #740 round 2, A major 1
        for body in SECTION:
            with self.subTest(body):
                self.assertEqual(rj.read_findings(HEAD + body), ([], True))
        for body in ONE_LABEL:
            with self.subTest(body):
                self.assertEqual(rj.read_findings(HEAD + body), ([], False))
        for body in ONE_KIND:
            with self.subTest(body):
                self.assertEqual(rj.read_findings(HEAD + 'blocker: none\n' + body), ([], False))
                r = rounds([('code', H1)], {'code': HEAD + 'blocker: none\n' + body})
                self.assertEqual(len(r.problems), 1)
                self.assertIn('lens code: its newest report has no findings section', r.problems[0])

    def test_a_long_run_of_spaces_takes_linear_time(self):   # #740 round 2: 81-91 s for one 65,000-character line
        n = 200_000
        for line in ('major' + ' ' * n + 'x', 'blocker: none' + ' ' * n + 'x', '| a | b |\n' + ' ' * n + 'x',
                     '| a | b |\n|' + ' ' * n + 'x', '| a | b |\n|-' + ' ' * n + 'x',
                     'not' + ' ' * n + 'safe', 'finding:' + ' ' * n + 'x', 'major: none' + ' \t' * (n // 2) + 'x'):
            with self.subTest(line[:12]):
                start = time.monotonic()
                rj.read_findings(HEAD + line)
                self.assertLess(time.monotonic() - start, 2)

    def test_a_table_does_not_go_on_past_a_fence(self):   # a fence ends the table, as GitHub renders it
        table = '| blocker | major |\n|---|---|\n| none | none |\n'
        self.assertEqual(rj.read_findings(table + '```\ncode\n```\n| a | b |'), ([], True))
        self.assertEqual(len(rj.findings(table + '| a | b |')), 1)   # without the fence: a row of the table

    def test_a_line_that_only_mentions_a_label_is_neither(self):
        for line in MENTION:
            with self.subTest(line):
                self.assertEqual(rj.read_findings(HEAD + line), ([], False))

    def test_the_kind_and_line_are_named(self):
        self.assertEqual(rj.findings('blocker: the lock is gone\n- **Major:** two writers race\nminor: a typo'),
                         [('blocker', 'blocker: the lock is gone'), ('major', 'Major: two writers race')])
        self.assertEqual(rj.findings('### Major\n- race')[0], ('major', 'Major race'))
        self.assertEqual(rj.findings('### Major')[0], ('major', 'Major (nothing under it)'))

    def test_an_unclosed_fence_runs_to_the_end(self):   # GitHub shows the rest as code
        self.assertEqual(rj.read_findings(NONE + '\n```\nmajor: x'), ([], True))

    def test_fold(self):
        self.assertEqual(rj.fold('\uff2da\u200bj\u043er'), 'Major')
        self.assertEqual(rj.fold(None), '')


class Rounds(unittest.TestCase):
    def test_each_different_change_read_is_a_round(self):
        self.assertEqual(rj.review_round([]), 0)
        self.assertEqual(rj.review_round([('code', H1), ('data-1', H1)]), 1)
        self.assertEqual(rj.review_round([('code', H1), ('code', H2), ('data-1', H2), ('code', H3)]), 3)
        self.assertEqual(rj.review_round([('code', None)]), 0)

    def test_a_short_sha_counts_too(self):   # it reads nothing for the head, but it was a read: a round
        self.assertEqual(rj.review_round([('code', 'abcdef1'), ('code', H1)]), 2)

    def test_reads_of_one_change_are_one_round(self):   # #100: a queue update merges the base in, no fix
        change = {H1: 'x', H2: 'x', H3: 'y'}.get
        self.assertEqual(rj.review_round([('code', H1), ('code', H2)], change), 1)
        self.assertEqual(rj.review_round([('code', H1), ('code', H2), ('code', H3)], change), 2)
        self.assertEqual(rj.review_round([('code', H1.upper()), ('code', H1)]), 1)

    def test_a_round_marker_counts_when_it_is_higher(self):   # deleted old rounds: the marker still says it
        self.assertEqual(rj.review_round([('code r3', H1)]), 3)
        self.assertEqual(rj.review_round([('code **r4**', H1), ('data r2', H2)]), 4)   # emphasis stripped: counts
        self.assertEqual(rj.review_round([('code `r5`', H1)]), 5)
        self.assertEqual(rj.review_round([('code r1', H1), ('code r1', H2), ('code', H3)]), 3)
        self.assertEqual(rj.round_marker('r2d2 lens'), 0)
        self.assertEqual(rj.round_marker('r9'), 0)   # a name's first word is its name, never a marker
        self.assertEqual(rj.round_marker('code r\uff13'), 3)   # folded

    def test_no_lens_comment_yet_is_round_zero_and_passes(self):   # empty state
        r = rounds([], {})
        self.assertEqual((r.round, r.approach, r.over_cap, r.findings, r.problems), (0, False, False, (), ()))

    def test_a_major_blocks_until_a_later_round_of_that_lens_clears_it(self):
        r = rounds([('code', H1)], {'code': 'major: two writers race'})
        self.assertEqual(r.findings, (('code', 'major', 'major: two writers race'),))
        self.assertIn('lens code reported a major that no later round cleared', r.problems[0])
        self.assertIn('lens code posts a new report on the fixed head', r.problems[0])
        r = rounds([('code', H1), ('code r2', H2)], {'code': NONE})   # round 2 cleared it
        self.assertEqual((r.round, r.problems), (2, ()))

    def test_a_report_with_no_findings_section_fails_that_lens(self):   # #99: never passes as clean
        r = rounds([('code', H1)], {'code': HEAD + 'Looks good to me.'})
        self.assertEqual(len(r.problems), 1)
        self.assertIn('lens code: its newest report has no findings section', r.problems[0])
        self.assertIn('findings in inline review comments are not read', r.problems[0])
        self.assertEqual(rounds([('code', H1)], {'code': HEAD}).problems, r.problems)   # empty body: the same
        self.assertEqual(rounds([('code', H1)], {'code': HEAD + NONE}).problems, ())
        self.assertEqual(rounds([('code', H1)], {'code': HEAD + 'major: none'}).problems, r.problems)   # one kind

    def test_an_entry_that_reads_nothing_under_an_agents_override_cannot_be_judged(self):
        r = rounds([('code', H1)], {'code': (rj.UNREAD, HEAD + 'major: none')})
        self.assertIn('its findings cannot be read', r.problems[0])

    def test_a_loose_comment_under_an_agents_override_is_judged_by_its_lines(self):   # no section needed
        self.assertEqual(rounds([], {'x': (rj.LOOSE, 'fixed it')}).problems, ())
        self.assertEqual(len(rounds([], {'x': (rj.LOOSE, 'major: still racy')}).findings), 1)

    def test_a_cleared_or_unjudged_entry_is_not_judged_here(self):
        for how in (rj.CLEARED, rj.NOT_JUDGED):   # a person's override; or it fails the head already
            with self.subTest(how):
                self.assertEqual(rounds([('code', H1)], {'code': (how, 'blocker: x')}).problems, ())

    def test_findings_false_judges_no_finding(self):
        r = rounds([('code', H1)], {'code': 'blocker: x'}, review=dataclasses.replace(BASIC, findings=False))
        self.assertEqual(r.problems, ())
        self.assertIn('findings are not judged', r.text(dataclasses.replace(BASIC, findings=False)))

    def test_the_approach_lens_is_required_from_round_approach_at(self):
        self.assertFalse(rounds([('code', H1), ('code', H2)], {'code': NONE}).approach)
        r = rounds([('code', H1), ('code', H2), ('code', H3)], {'code': NONE})
        self.assertTrue(r.approach)
        self.assertIn('approach lens approach required from round 3', r.text(BASIC))
        self.assertFalse(rounds([('code', H1), ('code', H2), ('code', H3)], {}, decided=True).approach)

    def test_a_round_past_the_cap_stops_and_says_what_a_person_does(self):   # #100
        four = [('code', h) for h in (H1, H2, H3, H4)]
        self.assertEqual(rounds(four, {'code': NONE}).problems, ())   # round 4 of 4 may pass
        r = rounds(four + [('code', H5)], {'code': NONE})
        self.assertTrue(r.over_cap)
        self.assertEqual(r.problems, (
            'review round 5 is past the cap of 4 rounds (issue #100): no more rounds; a person decides: close the pull '
            'request, or a person\'s override comment, but no one is listed in review.override_authors yet, so no '
            'override is accepted (issue #78: until agents no longer post as the person\'s login, an override is not '
            'proof of a person). A person\'s override alone is enough, also for a required approach lens that never '
            'posted',))
        r = rounds(four + [('code', H5)], {'code': NONE}, review=PEOPLE)
        self.assertIn('an override comment `override for head <head sha>: <why>` for this head by a-person, newer '
                      'than every lens comment', r.problems[0])
        self.assertEqual(rounds([('code r5', H1)], {'code': NONE}).round, 5)

    def test_a_persons_decision_passes_the_cap_and_waives_the_approach_lens(self):
        five = [('code', h) for h in (H1, H2, H3, H4, H5)]
        r = rounds(five, {'code': (rj.CLEARED, '')}, decided=True, review=PEOPLE)
        self.assertEqual((r.over_cap, r.approach, r.problems), (False, False, ()))

    def test_a_finding_at_the_last_round_says_no_more_rounds(self):
        r = rounds([('code', h) for h in (H1, H2, H3, H4)], {'code': 'major: still racy'})
        self.assertIn('this is round 4 of 4: no more rounds; a person decides', r.problems[0])

    def test_the_same_comments_give_the_same_verdict(self):   # runs again: pure
        args = ([('code', H1), ('code r2', H2)], {'code': 'major: x'})
        self.assertEqual(rounds(*args), rounds(*args))


class OverrideAuthors(unittest.TestCase):
    def test_left_out_or_empty_is_no_person(self):   # #78: agents still post as the owner's login
        self.assertEqual(BASIC.override_authors, ())
        block = VECTORS['configs']['basic']
        self.assertEqual(rc.parse_review({**block, 'override_authors': []}).override_authors, ())
        self.assertEqual(rc.parse_review({**block, 'override_authors': ['a-person', 'B2']}).override_authors,
                         ('a-person', 'B2'))

    def test_a_wrong_list_is_refused(self):
        block = VECTORS['configs']['basic']
        for bad in ('a-person', [''], ['-x'], ['a b'], ['x', 'X'], [1], ['x'] * 21, [f'p{i}' for i in range(21)], None):
            with self.subTest(bad), self.assertRaises(config.ConfigError) as e:
                rc.parse_review({**block, 'override_authors': bad})
            self.assertIn('override_authors', str(e.exception))
        self.assertEqual(len(rc.parse_review({**block, 'override_authors': [f'p{i}' for i in range(20)]})
                             .override_authors), 20)


# --- the fork's own config (issue #730): in shadow, workflow, queue and lens files critical (#82) ----------------
ROOT = os.path.join(HERE, '..', '..', '..')
FORK_CONFIG = '.github/fork-pr-queue.json'


class ForkConfig(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, FORK_CONFIG), encoding='utf-8') as f:
            self.cfg = config.parse(f.read(), FORK_CONFIG)
        self.review = self.cfg.review

    def test_it_parses_in_shadow_with_the_owners_numbers_and_no_person_yet(self):
        r = self.review   # shadow first (README, Turning it on); no override author until #78
        self.assertEqual((r.mode, r.findings, r.rounds.approach_at, r.rounds.max, r.warnings, r.override_authors),
                         ('shadow', True, 3, 4, (), ()))
        self.assertEqual(r.order, ())   # enforce would take it as it is
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
