"""Tests the review rules' turn-on guide against the code it describes (run: python3 fork-ci/actions/head-check/review_guide.test.py).

The guide is the pr-queue README's section "Turning it on: release, pin, then config". The fork-ci README and the
example gate caller point to it. Here: the links resolve to its heading, its steps keep their order, the summary rows
it quotes are the rows the gate writes, the releases it names agree with RELEASE-NOTES.md, and each "stopped half-way"
state it promises is what head_check and head_gate do: a config without the block is the gate of before, a key a
release does not know refuses the whole config, an empty required-lenses input fails unless the base is in enforce, the
rows never fail a head in shadow, and a review that cannot be judged fails it in both modes.

No git, no network, no GitHub: the gate's reads are replaced by fakes; the rules and the row text are the real code.
"""
import json, os, re, sys, unittest
from unittest import mock

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
HERE = os.path.dirname(os.path.abspath(__file__))
FORK_CI = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)
import head_check as H  # noqa: E402  (puts ../pr-queue on the path)
import head_gate as G  # noqa: E402
import config  # noqa: E402
import review_config  # noqa: E402
import review_judge  # noqa: E402

QUEUE = os.path.join(FORK_CI, 'actions', 'pr-queue')
GUIDE_README = os.path.join(QUEUE, 'README.md')
FORK_CI_README = os.path.join(FORK_CI, 'README.md')
NOTES = os.path.join(FORK_CI, 'RELEASE-NOTES.md')
EXAMPLE = os.path.join(HERE, 'gate-caller.example.yml')
HEADING = 'Turning it on: release, pin, then config'
BASE = {
    'required_checks': ['build', 'test'], 'merge_method': 'squash', 'bot_logins': ['review-bot'],
    'lens_author_associations': ['OWNER'], 'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
    'head_check': 'test',
}


def read(path):
    with open(path, encoding='utf-8') as f:
        return f.read()


def slug(heading):
    """GitHub's anchor of a Markdown heading: lower case, punctuation dropped, spaces to dashes."""
    return re.sub(r'[^\w\- ]', '', heading.strip().lower()).replace(' ', '-')


def guide():
    """The guide's text: from its heading to the next heading of the same or a higher level."""
    text = read(GUIDE_README)
    m = re.search(r'^### ' + re.escape(HEADING) + r'\n(.*?)(?=^#{2,3} )', text, re.S | re.M)
    if not m:
        raise AssertionError(f'no "### {HEADING}" section in {GUIDE_README}')
    return m.group(1)


def steps(text):
    """The guide's numbered steps: [(number, bold title)]."""
    return [(int(n), t) for n, t in re.findall(r'^(\d+)\. \*\*([^*]+)\*\*', text, re.M)]


def recommended():
    return json.loads(read(os.path.join(QUEUE, 'review.recommended.json')))


def parsed(mode):
    return config.parse(json.dumps(dict(BASE, review=dict(recommended(), mode=mode))))


class Links(unittest.TestCase):
    def test_the_guide_has_its_heading(self):
        self.assertIn(f'\n### {HEADING}\n', read(GUIDE_README))

    def test_the_fork_ci_readme_links_to_the_heading(self):
        want = 'actions/pr-queue/README.md#' + slug(HEADING)
        self.assertEqual(slug(HEADING), 'turning-it-on-release-pin-then-config')
        self.assertIn(f']({want})', read(FORK_CI_README))

    def test_the_example_caller_names_the_heading_and_the_order(self):
        header = ' '.join(x.lstrip('# ').strip() for x in read(EXAMPLE).splitlines() if x.startswith('#'))
        self.assertIn(f'../pr-queue/README.md, "{HEADING}"', header)
        self.assertIn('Pin the release first: an older one refuses the whole config', header)

    def test_the_example_caller_names_the_pull_request_target_policy(self):
        header = ' '.join(x.lstrip('# ').strip() for x in read(EXAMPLE).splitlines() if x.startswith('#'))
        self.assertIn('repository Actions policy allows it for exactly this file', header)
        self.assertIn('keep the file name the policy names', header)

    def test_the_example_caller_says_what_required_lenses_does_in_each_mode(self):
        lines = read(EXAMPLE).splitlines()
        at = next(i for i, x in enumerate(lines) if x.strip().startswith('required-lenses:'))
        said = ' '.join(x.strip().lstrip('# ') for x in lines[at - 2:at])
        self.assertIn('In shadow (or with no review block) these names decide', said)
        self.assertIn("In enforce the config's path rules name the lenses and these are required as well", said)
        self.assertIn("empty this only after the base's config is in enforce", said)

    def test_every_relative_link_of_the_guide_resolves(self):
        text = guide()
        links = re.findall(r'\]\(([^)#\s]+)(#[^)\s]+)?\)', text)
        self.assertTrue(links)
        for path, anchor in links:
            if path.startswith('http'):
                continue
            target = os.path.normpath(os.path.join(QUEUE, path))
            self.assertTrue(os.path.exists(target), path)
            if anchor:
                heads = [slug(h) for h in re.findall(r'^#+ (.+)$', read(target), re.M)]
                self.assertIn(anchor[1:], heads, f'{path}{anchor}')


class Order(unittest.TestCase):
    def test_release_pin_config_read_enforce_roll_back(self):
        self.assertEqual(steps(guide()), [(1, 'Release.'), (2, 'Pin.'), (3, 'Config in `shadow`.'),
                                          (4, 'Read the results.'), (5, 'Enforce.'), (6, 'Roll back')])

    def test_the_pin_step_moves_every_caller(self):
        pin = guide().split('2. **Pin.**')[1].split('3. **')[0]
        for caller in ('`fork-pr-queue.yml`', '`pr-queue` action', 'gate-caller.example.yml', '`head-check`'):
            self.assertIn(caller, pin)
        self.assertIn('dispatch the gate', pin.replace('\n', ' ').replace("gate's", 'gate'))

    def test_enforce_dispatches_the_backfill_and_empties_the_input_only_after(self):
        step = ' '.join(guide().split('5. **Enforce.**')[1].split('6. **')[0].split())
        self.assertIn('dispatch the backfill', step)
        self.assertIn("only once the base's config is in `enforce`", step)

    def test_roll_back_is_the_reverse(self):
        step = ' '.join(guide().split('6. **Roll back**')[1].split('**Stopped')[0].split())
        self.assertLess(step.index('emptied input'), step.index('back to `shadow`'))
        self.assertLess(step.index('back to `shadow`'), step.index('a pin back'))


class Releases(unittest.TestCase):
    """The releases the guide names agree with the release notes."""

    def entry(self, pr):
        notes = read(NOTES)
        found = [e for e in notes.split('\n### ')[1:] if f'(#{pr})' in e.split('\n')[0]]
        self.assertEqual(len(found), 1, f'one release notes entry for #{pr}')
        return ' '.join(found[0].split())

    def test_findings_rounds_and_override_authors_come_after_the_same_release(self):
        entry = self.entry(740)
        first = re.search(r'First in the release after `(ci/v\d+)`', entry)
        self.assertIsNotNone(first, entry)
        text = ' '.join(guide().split())
        self.assertIn(f'Findings, rounds and `override_authors` are in the first release after `{first[1]}`', text)
        self.assertIn(f'`{first[1]}` or older for `override_authors`', text)
        self.assertIn(f'`{first[1]}` and older refuse a config with `override_authors`', entry)

    def test_the_release_notes_upgrade_points_to_the_guide(self):
        self.assertIn(f'pr-queue README, "{HEADING}"', self.entry(740))


class Rows(unittest.TestCase):
    """The rows the guide quotes are the rows the gate writes."""

    def judgement(self):
        return review_judge.Judgement('critical', ('data-1', 'data-2'),
                                      (review_judge.Hit('data', 'critical', ('src/storage/a.py',)),), (), 1)

    def quoted(self, label):
        text = ' '.join(guide().split())
        m = re.search(r'`' + re.escape(label) + r'`: `([^`]+)`', text)
        self.assertIsNotNone(m, label)
        return m[1]

    def as_pattern(self, quoted):
        """A quoted row as a regex: `<...>`, `(...)`, `<weight>` and `N`/`M` stand for any text."""
        out = re.escape(quoted)
        for hole in (re.escape('<weight>'), re.escape('<...>'), re.escape('(...)')):
            out = out.replace(hole, '.+?')
        return out.replace(r'round\ N\ of\ M', r'round \d+ of \d+').replace('round N of M', r'round \d+ of \d+')

    def test_the_path_rules_row(self):
        name, good, text = H.path_row('shadow', self.judgement())
        self.assertEqual(name, 'path rules (shadow)')
        self.assertIs(good, True)
        self.assertRegex(text, '^' + self.as_pattern(self.quoted('path rules (shadow)')) + '$')

    def test_a_config_warning_is_added_as_the_guide_says(self):
        _, _, text = H.path_row('shadow', self.judgement(), ('rule a above rule b',))
        self.assertTrue(text.endswith('; config warning: rule a above rule b'), text)
        self.assertIn('`; config warning: ...`', guide())

    def test_the_findings_and_rounds_row(self):
        quoted = self.quoted('findings and rounds (shadow)')
        rows = run_decide('shadow', rounds=(review_judge.Rounds(2, False, False, (), ()), None))[3]
        name, good, text = rows[1]
        self.assertEqual(name, 'findings and rounds (shadow)')
        self.assertIs(good, True)
        self.assertRegex(text, '^' + self.as_pattern(quoted))
        self.assertTrue(text.endswith('; no blocker or major open'), text)
        self.assertIn('`no blocker or major open`', guide())


def run_decide(mode, rounds, verdict=(True, 'every lens read the head', [('code', True, 'read abc')]),
               block=True, required=None):
    """head_check.decide with the gate's reads faked: (code, ok, reason, rows)."""
    cfg = parsed(mode) if block else config.parse(json.dumps(BASE))
    judgement = review_judge.Judgement('normal', ('code',), (review_judge.Hit('code', 'normal', ('x.py',)),), (), 1)
    pr = {'isDraft': False, 'headRefOid': 'a' * 40, 'comments': [], 'reviews': []}

    def no_gh(*a, **k):
        raise AssertionError('GitHub must not be called')
    with mock.patch.object(H, 'base_config', return_value=cfg), \
            mock.patch.object(H, 'merged_config_problem', return_value=None), \
            mock.patch.object(H, 'path_judgement', return_value=(judgement, None)), \
            mock.patch.object(H, 'read_pr', return_value=(pr, None)), \
            mock.patch.object(H, 'review_rounds', return_value=rounds), \
            mock.patch.object(H, 'verdict', return_value=verdict):
        return H.decide('o/p', 7, '.github/pr-queue.json', object(), required or {'code': 'code'}, False, no_gh,
                        lambda s: None, lambda line: None, [])


class StoppedHalfWay(unittest.TestCase):
    """Each state the guide's "Stopped half-way, or run again" promises."""

    def test_pins_moved_no_block_yet_is_the_gate_of_before(self):
        cfg = config.parse(json.dumps(BASE))
        self.assertIsNone(cfg.review)
        code, ok, _, rows = run_decide('shadow', rounds=None, block=False)
        self.assertEqual((code, ok, rows), (0, True, [('code', True, 'read abc')]))

    def test_a_key_a_release_does_not_know_refuses_the_whole_config(self):
        """What an older release does to a newer key: the whole config is refused, naming the key."""
        with self.assertRaises(config.ConfigError) as e:
            config.parse(json.dumps(dict(BASE, review=dict(recommended(), later_key=True))))
        self.assertIn('later_key', str(e.exception))
        with self.assertRaises(config.ConfigError) as e:
            config.parse(json.dumps(dict(BASE, review=recommended(), later_key=True)))
        self.assertIn('later_key', str(e.exception))

    def test_the_shadow_step_starts_from_the_recommended_block_without_override_authors(self):
        cfg = parsed('shadow')
        self.assertEqual(cfg.review.mode, 'shadow')
        self.assertNotIn('override_authors', recommended())
        self.assertEqual(cfg.review.override_authors, ())

    def test_an_empty_input_fails_unless_the_base_is_in_enforce(self):
        for cfg, want in ((config.parse(json.dumps(BASE)), 'fails'), (parsed('shadow'), 'fails'),
                          (parsed('enforce'), None)):
            with mock.patch.object(H, 'base_config', return_value=cfg):
                problem = G.empty_lenses_problem(object(), '.github/pr-queue.json')
            if want is None:
                self.assertIsNone(problem)
            else:
                self.assertIn('required-lenses names no lens (it is empty)', problem)

    def test_an_unreadable_base_config_fails_an_empty_input_too(self):
        def unreadable(checkout, path, problems):
            problems.append('the config cannot be read')
            return None
        with mock.patch.object(H, 'base_config', side_effect=unreadable):
            self.assertEqual(G.empty_lenses_problem(object(), '.github/pr-queue.json'), 'the config cannot be read')

    def test_the_rows_never_fail_a_head_in_shadow(self):
        open_finding = review_judge.Rounds(5, True, True, (('code', 'major', 'major: x'),),
                                           ('lens code reported a major',))
        code, ok, reason, rows = run_decide('shadow', rounds=(open_finding, None))
        self.assertEqual((code, ok, reason), (0, True, 'every lens read the head'))
        self.assertEqual([good for _, good, _ in rows], [True, True, True])
        self.assertTrue(rows[1][2].startswith('they would have said: round 5 of 4'), rows[1][2])

    def test_the_same_rows_fail_it_in_enforce(self):
        open_finding = review_judge.Rounds(2, False, False, (('code', 'major', 'major: x'),),
                                           ('lens code reported a major',))
        code, ok, reason, rows = run_decide('enforce', rounds=(open_finding, None))
        self.assertEqual((code, ok), (1, False))
        self.assertIn('lens code reported a major', reason)
        self.assertIs(rows[1][1], False)

    def test_a_review_that_cannot_be_judged_fails_in_both_modes(self):
        for mode in ('shadow', 'enforce'):
            code, ok, reason, _ = run_decide(mode, rounds=(None, 'GitHub said no'))
            self.assertEqual((code, ok), (1, False), mode)
            self.assertIn('cannot be judged, so the head is not passed', reason)

    def test_the_same_inputs_twice_give_the_same_answer(self):
        rounds = (review_judge.Rounds(1, False, False, (), ()), None)
        self.assertEqual(run_decide('shadow', rounds=rounds), run_decide('shadow', rounds=rounds))


if __name__ == '__main__':
    unittest.main()
