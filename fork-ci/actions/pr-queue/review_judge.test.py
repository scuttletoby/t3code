"""Tests of review_judge.py (run: python3 fork-ci/actions/pr-queue/review_judge.test.py).

Which lens reads a change needs by the `review` block's path rules, the findings a lens report says, and the rounds.
The shared vectors (review.vectors.json: `cases`, `findings`, `scans`, `rounds`) are the contract both sides test; the
rest here: reading git's name-status output, and that a rule set the parser
would refuse, or a diff line nobody knows, never judges.
"""
import dataclasses, html, json, os, re, sys, time, types, unittest, unittest.mock

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402
import review_config as rc  # noqa: E402
import review_judge as rj  # noqa: E402
sys.path.insert(0, os.path.join(HERE, '..', 'head-check'))
import head_check  # noqa: E402  (the `reads` vectors: lens_reads, review_rounds and verdict, as the gate runs them)

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
        self.assertTrue(text.startswith('critical, lenses data-1, data-2, code (1 changed path; '), text)
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
# can be written are shared vectors (review.vectors.json `findings`), so the TypeScript side reads the same lines;
# `group` says which rule each shows. Review A and B of #740 round 2: `blocker: none` kept from the prompt, the major
# written in a form not read as a label (`one-kind`): with one label kind only there is no findings section, so each
# fails closed, never clean.
FINDINGS = VECTORS['findings']


def group(name):
    return [v for v in FINDINGS if v['group'] == name]


class StateLine(unittest.TestCase):
    """has_state_line: what counts as a report's `state:` line (the review kit imports it; the gate parses
    `state_line` but does not enforce it yet)."""

    def test_an_answered_state_line_outside_code_and_quotes(self):
        for body, said in (('state: a second run does nothing', True), ('**State:** fine', True),
                           ('- state: fine', True), ('STATE : fine', True), ('state:', False), ('state:   ', False),
                           ('the state is fine', False), ('```\nstate: fine\n```', False), ('> state: fine', False),
                           ('    state: fine', False)):
            with self.subTest(body=body):
                self.assertIs(rj.has_state_line(f'lens: code\n{body}\nblocker: none\n'), said)


class Findings(unittest.TestCase):
    def test_every_shared_findings_vector(self):
        for v in FINDINGS:
            with self.subTest(v['name']):
                said, section = rj.read_findings(v['body'])
                self.assertEqual([list(f) for f in said], v['findings'])
                self.assertEqual(section, v['section'])

    def test_each_rule_keeps_its_vectors(self):   # a vector dropped from the file is a rule no longer shown
        sizes = {g: len(group(g)) for g in ('finding', 'clean', 'section', 'one-kind', 'one-label', 'mention', 'named',
                                            'python-text')}
        self.assertEqual(sizes, {'finding': 58, 'clean': 37, 'section': 9, 'one-kind': 9, 'one-label': 4,
                                 'mention': 8, 'named': 8, 'python-text': 13})
        self.assertEqual(sum(sizes.values()), len(FINDINGS))

    def test_each_real_finding_format_counts(self):
        for v in group('finding'):
            with self.subTest(v['name']):
                self.assertTrue(v['findings'] and v['findings'][0][0] in ('blocker', 'major'))

    def test_each_clean_format_is_no_finding(self):
        for v in group('clean'):
            with self.subTest(v['name']):
                self.assertEqual(v['findings'], [])
                if v['body'].startswith(HEAD):
                    self.assertEqual(rj.read_findings(HEAD + NONE + '\n\n' + v['body'][len(HEAD):]), ([], True))

    def test_a_section_needs_a_blocker_and_a_major_label(self):   # #740 round 2, A major 1
        for v in group('section'):
            self.assertEqual((v['findings'], v['section']), ([], True), v['name'])
        for v in group('one-label') + group('one-kind') + group('mention'):
            self.assertEqual((v['findings'], v['section']), ([], False), v['name'])
        for v in group('one-kind'):
            with self.subTest(v['name']):
                r = rounds([('code', H1)], {'code': v['body']})
                self.assertEqual(len(r.problems), 1)
                self.assertIn('lens code: its newest report has no findings section', r.problems[0])

    def test_a_table_does_not_go_on_past_a_fence(self):   # a fence ends the table, as GitHub renders it
        table = '| blocker | major |\n|---|---|\n| none | none |\n'
        self.assertEqual(rj.read_findings(table + '```\ncode\n```\n| a | b |'), ([], True))
        self.assertEqual(len(rj.findings(table + '| a | b |')), 1)   # without the fence: a row of the table

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


# Every section of review.vectors.json, replayed whole: the TypeScript side (apps/server/src/reviewRules) runs the same
# sections, and each side fails on a section or a vector field it does not run, so neither can skip one quietly.
SECTIONS = {'about', 'config_path', 'override_prefix', 'globs', 'configs', 'cases', 'findings', 'scans', 'rounds',
            'reads', 'linear', 'py_unicode', 'unicode'}
FIELDS = {'linear': {'name', 'fn', 'parts', 'expect', 'ms'},
          'unicode': {'name', 'body', 'findings', 'section', 'unassigned', 'refused'},
          'reads': {'name', 'posts', 'head', 'same', 'override_authors', 'required', 'expect'},
          'cases': {'name', 'config', 'changes', 'weight', 'lenses', 'hits', 'protected'},
          'findings': {'name', 'group', 'body', 'findings', 'section'},
          'scans': {'name', 'posts', 'history', 'entries', 'not_lens'},
          'rounds': {'name', 'config', 'history', 'reports', 'decided', 'changes', 'override_authors', 'findings',
                     'expect'}}
SCAN = lens.Review((), VECTORS['override_prefix'], 'OWNER', ('OWNER',), log=lambda *_: None)


def vector_pr(posts):
    """The vector's posts as a pull request: each by its `author` (else `someone`), one second apart unless it names its
    `when`; a comment by default, else a `review` (its flags read unless `flags` is false) or an `inline` review
    comment."""
    pr = {'comments': [], 'reviews': [], lens.REVIEW_FLAGS: {}, lens.REVIEW_COMMENTS: []}
    for i, p in enumerate(posts):
        when = p.get('when', f'2026-01-01T00:{i // 60:02d}:{i % 60:02d}Z')
        post = {'body': p['body'], 'author': {'login': p.get('author', 'someone')}, 'authorAssociation': 'OWNER'}
        flags = {'includesCreatedEdit': p.get('edited', False), 'isMinimized': p.get('hidden', False)}
        kind = p.get('kind', 'comment')
        if kind == 'review':
            pr['reviews'].append({**post, 'id': f'R{i}', 'submittedAt': when,
                                  'includesCreatedEdit': False, 'isMinimized': False})   # gh's defaults, never read
            if p.get('flags', True):
                pr[lens.REVIEW_FLAGS][f'R{i}'] = flags
        else:
            pr['comments' if kind == 'comment' else lens.REVIEW_COMMENTS].append({**post, 'createdAt': when, **flags})
    return pr


def scan(posts):
    found, history, notes, _ = SCAN.scan_posts(vector_pr(posts))
    return [list(h) for h in history], {k: [v[1], v[2]] for k, v in found.items()}, [list(n) for n in notes]


def replay_round(v):
    review = dataclasses.replace(CONFIGS[v['config']], override_authors=tuple(v.get('override_authors', ())),
                                 findings=v.get('findings', True))
    changes = v.get('changes')
    r = rj.judge_rounds(review, [tuple(h) for h in v['history']], {k: tuple(e) for k, e in v['reports'].items()},
                        v.get('decided', False), (lambda s: changes.get(s, s)) if changes else None)
    return {'round': r.round, 'approach': r.approach, 'over_cap': r.over_cap,
            'findings': [list(f) for f in r.findings], 'problems': list(r.problems), 'text': r.text(review)}


class _MergeCommit:
    """A checkout of GitHub's merge commit of `head` that knows no history (own_change: each read its own change)."""
    holder = 'the checkout merges'

    def __init__(self, head):
        self.parents = ('0' * 40, head)

    def merge_parents(self):
        return self.parents

    def git(self, *a):
        return types.SimpleNamespace(returncode=1, stdout='', stderr='no history here')

    def has(self, sha):
        return True

    fetch = has


AGAIN_RE = re.compile(r'lens (.+?): its newest report (?:reads nothing|has no findings section)')


def replay_reads(v):
    """What the gate reads of the lens posts at `head`, under the `basic` rules in enforce with findings judged:
    lens.Review.lens_reads (each key's sha), the keys review_rounds fails until that lens posts again (UNREAD, or a
    report with no findings section), and whether head_check.verdict and the rounds pass. `same`: the reads that are
    the head's change merged with the base (head_check.same_change)."""
    head = v['head']
    pr = {**vector_pr(v['posts']), 'isDraft': False, 'headRefOid': head}
    rules = dataclasses.replace(CONFIGS['basic'], mode='enforce', findings=True,
                                override_authors=tuple(v['override_authors']))
    checkout = _MergeCommit(head)
    same = lambda _checkout, h, read: (read == h or read in v['same'], 'the same change')   # noqa: E731
    with unittest.mock.patch.object(head_check, 'same_change', same):
        reads = {k: sha for k, (sha, _, _) in SCAN.lens_reads(pr).items()}
        rounds, why = head_check.review_rounds(rules, SCAN, pr, checkout, VECTORS['override_prefix'])
        ok, _, _ = head_check.verdict(pr, SCAN, checkout, {lens.lens_key(n): n for n in v['required']})
    if rounds is None:
        raise AssertionError(why)
    again = sorted(m.group(1) for p in rounds.problems for m in [AGAIN_RE.match(p)] if m)
    return {'reads': reads, 'again': again, 'passes': ok and not rounds.problems}


class SharedVectors(unittest.TestCase):
    def test_this_side_runs_every_section_and_field(self):
        self.assertEqual(set(VECTORS), SECTIONS)
        for section, fields in FIELDS.items():
            names = [v['name'] for v in VECTORS[section]]
            self.assertEqual(len(names), len(set(names)), f'{section}: two vectors share a name')
            for v in VECTORS[section]:
                self.assertLessEqual(set(v), fields, f'{section}: {v["name"]}')

    def test_every_linear_vector_in_its_time(self):   # #740 round 2: 81-91 s for one 65,000-character line before
        for v in VECTORS['linear']:   # each its own bound: a slow but not hanging path would pass a test timeout
            with self.subTest(v['name']):
                body = ''.join(text * times for text, times in v['parts'])
                start = time.monotonic()
                if v['fn'] == 'read_findings':
                    said, section = rj.read_findings(body)
                    got = [[list(f) for f in said], section]
                else:
                    self.assertEqual(v['fn'], 'lens_problem')
                    got = SCAN.lens_problem({'body': body})
                self.assertLess(time.monotonic() - start, v['ms'] / 1000)
                self.assertEqual(got, v['expect'])

    def test_the_unicode_table_is_this_pythons(self):   # T3 refuses text whose code points its Unicode assigns otherwise
        import unicodedata
        u = VECTORS['py_unicode']
        self.assertEqual(u['version'], unicodedata.unidata_version,
                         'a new Python reads a new Unicode: regenerate py_unicode and T3\'s pyUnicode.ts together')
        ranges, start = [], None
        for cp in range(0x110000):
            assigned = unicodedata.category(chr(cp)) != 'Cn'
            if assigned and start is None:
                start = cp
            if not assigned and start is not None:
                ranges.append((start, cp - 1))
                start = None
        if start is not None:
            ranges.append((start, 0x10ffff))
        self.assertEqual(u['assigned'], ' '.join(f'{a:x}' if a == b else f'{a:x}-{b:x}' for a, b in ranges))
        for v in VECTORS['unicode']:
            with self.subTest(v['name']):
                said, section = rj.read_findings(v['body'])
                self.assertEqual(([list(f) for f in said], section), (v['findings'], v['section']))
                # As written or as its HTML references write it (`&#x1C89;`): the rules read both.
                self.assertEqual(v['unassigned'],
                                 sorted({f'U+{ord(c):04X}' for c in v['body'] + html.unescape(v['body'])
                                         if unicodedata.category(c) == 'Cn'}))
                # What T3 refuses on a runtime that assigns them (a letter, number, mark, format or space there): a
                # subset; the rest (a new symbol) reads alike here and there.
                self.assertLessEqual(set(v['refused']), set(v['unassigned']))

    def test_every_scan(self):   # same comment twice, edited, hidden, old forms, no comment at all
        self.assertGreaterEqual(len(VECTORS['scans']), 31)
        for v in VECTORS['scans']:
            with self.subTest(v['name']):
                self.assertEqual(scan(v['posts']), (v['history'], v['entries'], v.get('not_lens', [])))

    def test_every_reads_vector(self):   # lens_start's copy of lens_reads, review_rounds' UNREAD and verdict
        self.assertGreaterEqual(len(VECTORS['reads']), 10)
        for v in VECTORS['reads']:
            with self.subTest(v['name']):
                self.assertEqual(replay_reads(v), v['expect'])

    def test_every_round(self):
        self.assertGreaterEqual(len(VECTORS['rounds']), 28)
        for v in VECTORS['rounds']:
            with self.subTest(v['name']):
                self.assertEqual(replay_round(v), v['expect'])


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


# --- the fork's own config (issue #730): in enforce, workflow, queue, lens and other CI files critical (#82) ---------
ROOT = os.path.join(HERE, '..', '..', '..')
FORK_CONFIG = '.github/fork-pr-queue.json'
# The one .github file that is not a workflow file or gate setting (#82): a per-pull-request bookkeeping list a CI check
# compares with the tree (fork-ci-tests.cjs: every test file is listed). Most fork pull requests add a line to it; as a
# critical file alone it made the gate lenses (merge-path, state) the only required ones on plain app changes, and no
# lens read the code (the dry run of the switch, issue #82). .github/fork-seams.json was such a list too, but it is
# the upstream-seam guard's list (which fork lines upstream files may hold): #968 makes it critical, and an app change
# with it needs the code lens and the gate lenses (one extra review, below). Every other file under .github stays
# critical: a new one fails this test until it is sorted.
NOT_GATE = ('.github/fork-ci-tests.txt',)
# fork-kit folders that are gate settings (#82): review is the lens-format checker and the lens skill (what a lens
# report must say to count), gh-apps keeps the gate App's key and mints its tokens (the gate-trust design). The
# other kits run machines and agents (placement-facts: example scripts a machine runs for its facts), and the merge
# gate reads none of them.
GATE_KITS = ('gh-apps', 'review')
OTHER_KITS = ('.gitignore', 'README.md', 'agent-guards', 'cleaner', 'common', 'desk', 'gardener', 'golive', 'heavy',
              'model-guard', 'pc-agents', 'placement-facts', 'schedule-kick', 'update-policy')


class ForkConfig(unittest.TestCase):
    def setUp(self):
        with open(os.path.join(ROOT, FORK_CONFIG), encoding='utf-8') as f:
            self.cfg = config.parse(f.read(), FORK_CONFIG)
        self.review = self.cfg.review
        self.critical = next(x for x in self.review.rules if x.weight == 'critical')

    def judge(self, path):
        return rj.judge(self.review, [('M', (path,))], FORK_CONFIG)

    def test_it_parses_in_enforce_with_the_owners_numbers_and_no_person_yet(self):
        r = self.review   # #82 #99 #100 decided 2026-11-14; no override author while agents post as the owner (#78)
        self.assertEqual((r.mode, r.findings, r.rounds.approach_at, r.rounds.max, r.warnings, r.override_authors),
                         ('enforce', True, 3, 4, (), ()))
        self.assertEqual(r.order, ())
        self.assertGreaterEqual(r.min_lenses.critical, r.min_lenses.normal + 1)   # #82: one extra review
        self.assertEqual(r.rounds.approach_lens, 'approach')
        for x in r.lenses:
            with self.subTest(x.name):
                self.assertTrue(os.path.isfile(os.path.join(ROOT, x.prompt)), x.prompt)

    def test_workflow_queue_gate_config_and_lens_files_are_critical(self):   # #82
        for path in ('.github/workflows/fork-head-gate.yml', 'fork-ci/actions/pr-queue/review_judge.py',
                     'fork-ci/actions/head-check/head_check.py', FORK_CONFIG, '.github/fork-lens-state.md',
                     '.github/actions/setup-ci-deps/action.yml', '.github/fork-ci-release-rulesets.json',
                     '.github/fork-actions-policies.json', '.github/scripts/fork-contracts.test.py',
                     '.github/workflows/new.yml', '.github/new-folder/x.json', '.github/new.md', 'fork-ci/new.txt',
                     'fork-kit/review/lib/review_kit.py', 'fork-kit/gh-apps/libexec/x', 'fork-kit/review/SKILL.md',
                     'docs/fork/gate-trust.md', '.github/fork-seams.json'):
            with self.subTest(path):
                j = self.judge(path)
                self.assertEqual((j.weight, j.lenses), ('critical', self.critical.lenses))
                self.assertGreaterEqual(len(j.lenses), 2)
        j = self.judge('apps/server/src/x.ts')
        self.assertEqual((j.weight, j.lenses), ('normal', ('code',)))

    def test_every_github_file_is_critical_but_the_bookkeeping_list(self):   # a new one must be sorted
        top = sorted(os.listdir(os.path.join(ROOT, '.github')))
        self.assertTrue(set(NOT_GATE) <= {f'.github/{x}' for x in top}, 'a bookkeeping list moved: update NOT_GATE')
        for name in top:
            path = f'.github/{name}' + ('/x' if os.path.isdir(os.path.join(ROOT, '.github', name)) else '')
            with self.subTest(path):
                weight = self.judge(path).weight
                if path in NOT_GATE:
                    self.assertEqual(weight, 'normal', f'{path} is a bookkeeping list (NOT_GATE)')
                else:
                    self.assertEqual(weight, 'critical', f'{path} is not in the gate rule: add it to the gate rule of '
                                     f'{FORK_CONFIG} (a workflow file or a gate setting, #82), or to NOT_GATE with why')

    def test_only_the_gate_trust_page_of_docs_fork_is_critical(self):
        # docs/fork/gate-trust.md holds the owner's paste commands that set up the gate App's key and tokens (a gate
        # setting, like fork-kit/gh-apps); the other docs/fork pages are runbooks and stay normal, with the code lens,
        # never a no-lens rule (a changed owner command would merge unread).
        self.assertEqual(self.judge('docs/fork/gate-trust.md').hits[0].rule, self.critical.name)
        for name in sorted(os.listdir(os.path.join(ROOT, 'docs', 'fork'))):
            if name == 'gate-trust.md':
                continue
            with self.subTest(name):
                j = self.judge(f'docs/fork/{name}')
                self.assertEqual((j.weight, j.lenses), ('normal', ('code',)))
        j = rj.judge(self.review, [('M', ('docs/fork/gate-trust.md',)), ('M', ('docs/fork/host-tools.md',))],
                     FORK_CONFIG)
        self.assertEqual((j.weight, j.lenses), ('critical', self.critical.lenses + ('code',)))

    def test_every_fork_kit_folder_is_sorted(self):   # a new kit must be sorted too
        top = sorted(os.listdir(os.path.join(ROOT, 'fork-kit')))
        self.assertTrue(set(GATE_KITS) <= set(top), 'a gate kit moved: update GATE_KITS and the gate rule')
        for name in top:
            path = f'fork-kit/{name}' + ('/x' if os.path.isdir(os.path.join(ROOT, 'fork-kit', name)) else '')
            with self.subTest(path):
                weight = self.judge(path).weight
                if name in GATE_KITS:
                    self.assertEqual(weight, 'critical', f'{path} is a gate kit (GATE_KITS): add it to the gate rule')
                else:
                    self.assertIn(name, OTHER_KITS, f'{path} is new: add it to the gate rule of {FORK_CONFIG} and '
                                  'GATE_KITS (it decides what the gate checks or holds its keys, #82), or to '
                                  'OTHER_KITS with why')
                    self.assertEqual(weight, 'normal', f'{path} is in OTHER_KITS but judged {weight}')

    def test_an_app_change_that_lists_a_test_needs_the_code_lens(self):   # the dry run's refusals
        for extra in NOT_GATE:
            with self.subTest(extra):
                j = rj.judge(self.review, [('M', ('apps/server/src/x.ts',)), ('M', (extra,))], FORK_CONFIG)
                self.assertEqual((j.weight, j.lenses), ('normal', ('code',)))

    def test_a_seam_list_change_needs_the_gate_lenses_and_with_code_the_code_lens(self):   # #968 item 1
        seams = '.github/fork-seams.json'
        j = self.judge(seams)   # an announcement alone (a list-only pull request)
        self.assertEqual((j.weight, j.lenses, j.hits[0].rule), ('critical', ('merge-path', 'state'), self.critical.name))
        j = rj.judge(self.review, [('M', ('apps/server/src/x.ts',)), ('M', (seams,))], FORK_CONFIG)
        self.assertEqual((j.weight, j.lenses), ('critical', ('merge-path', 'state', 'code')))

    def test_an_app_change_plus_a_gate_file_needs_the_code_lens_and_the_gate_lenses(self):   # #82: one EXTRA review
        for gate in ('.github/workflows/ci.yml', '.github/SECURITY.md', 'fork-ci/actions/x.py',
                     'fork-kit/review/lib/review_kit.py'):
            with self.subTest(gate):
                j = rj.judge(self.review, [('M', ('apps/server/src/x.ts',)), ('M', ('.github/fork-seams.json',)),
                                           ('M', (gate,))], FORK_CONFIG)
                self.assertEqual((j.weight, j.lenses), ('critical', ('merge-path', 'state', 'code')))
        self.assertEqual(self.judge('.github/workflows/ci.yml').lenses, ('merge-path', 'state'))   # a gate change alone


if __name__ == '__main__':
    unittest.main()
