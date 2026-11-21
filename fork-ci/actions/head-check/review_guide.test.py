"""Tests the review rules' turn-on guide against the code it describes (run: python3 fork-ci/actions/head-check/review_guide.test.py).

The guide is the pr-queue README's section "Turning it on: release, pin, then config". The fork-ci README and the
example gate caller point to it. Here: the links resolve to its heading, its steps keep their order, the summary rows
it quotes are the rows the gate writes, the releases it names agree with RELEASE-NOTES.md and (where this clone has
the ci/vN tags) with the tags, and each "stopped half-way" state it promises is what head_check and head_gate do: a
config without the block is the gate of before, a key a release does not know refuses the whole config (a gate on a
release that knows the block refuses the pull request that adds it), an empty required-lenses input fails unless the
base is in enforce and is no floor once it may be empty, the rows never fail a head in shadow, what cannot be judged
fails it in both modes, and a pin back is checked by nothing but review. Each such test also reads the guide's own
words for that state, so a guide that promises something else goes red.

No network, no GitHub: the gate's reads are replaced by fakes (git only for a throwaway repository, and for the tags);
the rules and the row text are the real code.
"""
import json, os, re, subprocess, sys, tempfile, unittest
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


def flat(text):
    return ' '.join(text.split())


def step(n):
    """Step n of the guide, whitespace folded."""
    text = guide()
    start = re.search(rf'^{n}\. \*\*', text, re.M)
    end = re.search(rf'^({n + 1}\. \*\*|\*\*Stopped half-way)', text, re.M)
    return flat(text[start.start():end.start()])


def halfway(opening):
    """The guide's "Stopped half-way" bullet that starts with `opening`, whitespace folded."""
    part = guide().split('**Stopped half-way, or run again.**')[1].split('**The owner step.**')[0]
    found = [flat(b) for b in re.split(r'^- ', part, flags=re.M)[1:] if flat(b).startswith(opening)]
    if len(found) != 1:
        raise AssertionError(f'one "Stopped half-way" bullet starting {opening!r}, found {len(found)}')
    return found[0]


def owner_step():
    return flat(guide().split('**The owner step.**')[1])


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
        said = ' '.join(x.strip().lstrip('# ') for x in lines[at - 4:at])
        self.assertIn('In shadow (or with no review block) these names decide', said)
        self.assertIn("In enforce the config's path rules name the lenses and these are required as well", said)
        self.assertIn("empty this only after the base's config is in enforce", said)
        # The floor the empty input drops (EmptyInputFloor checks that it is so).
        self.assertIn('Empty, it is no floor', said)
        self.assertIn('AGENTS.md too) merges with no lens (the guide, step 5)', said)

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

    def test_the_pin_step_moves_the_pin_config_of_tools_outside_a_workflow_too(self):
        """A tool outside a workflow runs the queue's config.py from its own pin config (actions/pinned): on an older
        release it refuses the new key like an old caller does (StoppedHalfWay, a key a release does not know)."""
        pin = step(2)
        self.assertIn("Move the `sha` of the pin config of every tool outside a workflow that runs the queue's code",
                      pin)
        self.assertIn('](../pinned/README.md)', pin)
        self.assertIn('](../../README.md#outside-a-workflow)', pin)
        self.assertIn('"sha"', read(os.path.join(FORK_CI, 'actions', 'pinned', 'README.md')))

    def test_enforce_dispatches_the_backfill_before_arming_and_empties_the_input_only_after(self):
        enforce = step(5)
        self.assertLess(enforce.index('Dispatch the backfill right after it merges'),
                        enforce.index('arm no pull request before the backfill has run'))
        self.assertIn('Nothing checks that gap', enforce)
        self.assertIn("only once the base's config is in `enforce`", enforce)
        bullet = halfway('`enforce` merged, backfill not yet dispatched')
        self.assertIn('the queue can merge on it', bullet)
        self.assertIn('arm nothing before it has run', bullet)

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


def git(*a, cwd=FORK_CI, check=False):
    env = dict(os.environ, GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL=os.devnull, GIT_TERMINAL_PROMPT='0')
    return subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=t@example.invalid', *a], cwd=cwd, env=env,
                          text=True, capture_output=True, check=check)


REVIEW_CONFIG = 'fork-ci/actions/pr-queue/review_config.py'


# fork-ci: may skip ReleaseTags.test_the_path_rules_are_first_in_the_release_the_guide_names
# fork-ci: may skip ReleaseTags.test_override_authors_is_first_in_the_release_after_the_one_the_notes_name
# (CI checks out without the ci/vN tags; these run in a clone that has them.)
class ReleaseTags(unittest.TestCase):
    """The releases the guide and the notes name, against the ci/vN tags. CI checks out without tags (fetch-depth 2,
    no tags), so there these skip and only Releases runs; run them in a clone with the tags (git fetch --tags)."""

    def setUp(self):
        tags = git('tag', '-l', 'ci/v*').stdout.split()
        self.tags = sorted(int(t[len('ci/v'):]) for t in tags if t[len('ci/v'):].isdigit())
        if not self.tags:
            self.skipTest('no ci/vN tags in this clone (CI fetches none): git fetch --tags to run these')

    def source(self, n):
        r = git('show', f'ci/v{n}:{REVIEW_CONFIG}')
        return r.stdout if r.returncode == 0 else None

    def test_the_path_rules_are_first_in_the_release_the_guide_names(self):
        n = int(re.search(r'path rules \(`review`, `shadow` and `enforce`\) are in `ci/v(\d+)` and later',
                          flat(guide()))[1])
        self.assertIn(n, self.tags)
        self.assertIsNotNone(self.source(n), f'ci/v{n} has no review_config.py')
        self.assertTrue(all(self.source(k) is None for k in self.tags if k < n), f'review_config.py before ci/v{n}')
        self.assertIn(f'`ci/v{n - 1}` or older for `review`', step(6))

    def test_override_authors_is_first_in_the_release_after_the_one_the_notes_name(self):
        n = int(re.search(r'First in the release after `ci/v(\d+)`', Releases().entry(740))[1])
        self.assertIn(n, self.tags)
        for k in self.tags:
            text = self.source(k)
            self.assertEqual(text is not None and 'override_authors' in text, k > n, f'ci/v{k}')
        self.assertIn('override_authors', read(os.path.join(QUEUE, 'review_config.py')))


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
        self.assertIn('`; no blocker or major open`', guide())

    def test_the_findings_row_parts_in_the_order_the_guide_gives(self):
        """The approach lens part and the findings: false part, quoted as the guide quotes them, in its order."""
        m = re.search(r'`they would have said: round N of M`, then `([^`]+)` once the round reaches '
                      r'`rounds.approach_at`, then `([^`]+)` with `findings: false`', flat(guide()))
        self.assertIsNotNone(m)
        approach, unjudged = m[1], m[2]
        for name in ('<name>', '<N>'):
            self.assertIn(name, approach)
        approach = re.escape(approach).replace(re.escape('<name>'), r'\S+').replace(re.escape('<N>'), r'\d+')
        review = parsed('shadow').review
        no_findings = config.parse(json.dumps(dict(BASE, review=dict(recommended(), mode='shadow', findings=False))))
        text = review_judge.Rounds(3, True, False, (), ()).text(no_findings.review)
        self.assertRegex(text, '^round 3 of 4' + approach + re.escape(unjudged) + '; no blocker or major open$')
        self.assertEqual(review_judge.Rounds(1, False, False, (), ()).text(review),
                         'round 1 of 4; no blocker or major open')


def run_decide(mode, rounds, verdict=(True, 'every lens read the head', [('code', True, 'read abc')]),
               block=True, required=None, judgement=None, paths=None, base=None, seen=None):
    """head_check.decide with the gate's reads faked: (code, ok, reason, rows). judgement: the path rules' Judgement;
    paths: what path_judgement says instead ((None, why): it cannot judge); base: base_config's side effect instead of
    the parsed config; seen: a list that gets the lenses decide requires (what it hands verdict)."""
    cfg = parsed(mode) if block else config.parse(json.dumps(BASE))
    hit = review_judge.Hit('code', 'normal', ('x.py',))
    judgement = judgement or review_judge.Judgement('normal', ('code',), (hit,), (), 1)
    pr = {'isDraft': False, 'headRefOid': 'a' * 40, 'comments': [], 'reviews': []}

    def no_gh(*a, **k):
        raise AssertionError('GitHub must not be called')

    def judged(pr, review, checkout, required, *a):
        if seen is not None:
            seen.append(dict(required))
        return verdict
    with mock.patch.object(H, 'base_config', **({'side_effect': base} if base else {'return_value': cfg})), \
            mock.patch.object(H, 'merged_config_problem', return_value=None), \
            mock.patch.object(H, 'path_judgement', return_value=paths or (judgement, None)), \
            mock.patch.object(H, 'read_pr', return_value=(pr, None)), \
            mock.patch.object(H, 'review_rounds', return_value=rounds), \
            mock.patch.object(H, 'verdict', side_effect=judged):
        return H.decide('o/p', 7, '.github/pr-queue.json', object(), required or {'code': 'code'}, False, no_gh,
                        lambda s: None, lambda line: None, [])


CONFIG_PATH = '.github/pr-queue.json'


def repository(d, base, head):
    """A throwaway repository with two commits, base then head ({path: text}, each the whole tree): their shas."""
    git('init', '-q', d, check=True)
    shas = []
    for files in (base, head):
        for f in git('ls-files', cwd=d).stdout.split():
            os.remove(os.path.join(d, f))
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, path)), exist_ok=True)
            with open(os.path.join(d, path), 'w', encoding='utf-8') as f:
                f.write(text)
        git('add', '-A', cwd=d, check=True)
        git('commit', '-q', '--allow-empty', '-m', 'x', cwd=d, check=True)
        shas.append(git('rev-parse', 'HEAD', cwd=d, check=True).stdout.strip())
    return tuple(shas)


def merged_problem(base, head):
    """head_check.merged_config_problem, bare mode, on a throwaway repository of these two trees."""
    with tempfile.TemporaryDirectory(prefix='review-guide-') as d:
        shas = repository(d, base, head)
        return H.merged_config_problem(H.Checkout(os.path.join(d, '.git'), bare=shas), CONFIG_PATH)


class StoppedHalfWay(unittest.TestCase):
    """Each state the guide's "Stopped half-way, or run again" promises."""

    def test_pins_moved_no_block_yet_is_the_gate_of_before(self):
        self.assertIn('the gate judges exactly as before', halfway('Pins moved, no config change yet'))
        cfg = config.parse(json.dumps(BASE))
        self.assertIsNone(cfg.review)
        code, ok, _, rows = run_decide('shadow', rounds=None, block=False)
        self.assertEqual((code, ok, rows), (0, True, [('code', True, 'read abc')]))

    def test_a_key_a_release_does_not_know_refuses_the_whole_config(self):
        """What an older release does to a newer key: the whole config is refused, naming the key."""
        self.assertIn('That caller refuses the whole config', halfway('The block added while one caller still pins'))
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

    def test_a_gate_that_knows_the_block_refuses_the_pull_request_that_adds_a_key_it_does_not_know(self):
        """The guide: a gate on ci/v15 or later refuses the block pull request itself. The gate parses the config the
        merge would leave with its own parser; a key it does not know (here `later_key`, as `review` is to ci/v14 or
        `override_authors` to ci/v20) does not parse, so the pull request is refused before it can merge."""
        bullet = halfway('The block added while one caller still pins')
        self.assertIn('a gate on `ci/v15` or later refuses the block pull request itself', bullet)
        self.assertIn('the owner step below', bullet)
        base = {CONFIG_PATH: json.dumps(BASE)}
        why = merged_problem(base, {CONFIG_PATH: json.dumps(dict(BASE, review=dict(recommended(), later_key=1)))})
        self.assertIn('later_key', why or '')
        self.assertIsNone(merged_problem(base, {CONFIG_PATH: json.dumps(dict(BASE, review=recommended()))}))

    def test_a_pin_back_is_checked_by_nothing_but_review(self):
        """Step 6: a pull request that only moves a pin (a workflow file) leaves the config as it was, so the gate's
        merged-config check passes it whatever release it pins; the config may hold a key the old release refuses."""
        self.assertIn('The gate does not check this; it is a review rule', step(6))
        self.assertIn('only the owner step below gets out', step(6))
        with_key = json.dumps(dict(BASE, review=dict(recommended(), override_authors=['owner'])))
        self.assertEqual(config.parse(with_key).review.override_authors, ('owner',))
        caller = '.github/workflows/gate.yml'
        base = {CONFIG_PATH: with_key, caller: 'uses: o/r/fork-ci/actions/head-check@' + 'b' * 40 + '  # ci/v21\n'}
        head = dict(base, **{caller: 'uses: o/r/fork-ci/actions/head-check@' + 'a' * 40 + '  # ci/v20\n'})
        self.assertIsNone(merged_problem(base, head))

    def test_the_enforce_pull_request_needs_only_the_input_lenses(self):
        """Step 5: the pull request that sets enforce is judged under the base's shadow, so the gate requires only the
        input's lenses, not the critical ones its own change (the config, a protected path) would need in enforce."""
        self.assertIn('so ask for the critical lenses: the gate does not require them yet', step(5))
        judgement = review_judge.judge(parsed('shadow').review, [('M', (CONFIG_PATH,))], CONFIG_PATH)
        self.assertEqual((judgement.weight, judgement.lenses), ('critical', ('data-1', 'data-2')))
        for mode, want in (('shadow', {'code'}), ('enforce', {'code', 'data-1', 'data-2'})):
            seen = []
            run_decide(mode, rounds=(review_judge.Rounds(1, False, False, (), ()), None), judgement=judgement,
                       seen=seen)
            self.assertEqual(set(seen[0].values()), want, mode)

    def test_an_empty_input_fails_unless_the_base_is_in_enforce(self):
        bullet = halfway('The input emptied while the base is not in `enforce`')
        self.assertIn('every head fails, the pull request that puts a lens name back too', bullet)
        self.assertIn('The owner step below', bullet)
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

    def test_the_owner_step_reverts_by_hand_then_dispatches_the_backfill(self):
        said = owner_step()
        self.assertIn('An owner pushes the fix straight to the default branch, past the branch ruleset', said)
        self.assertLess(said.index('the revert of the commit that broke it'), said.index('Then dispatch the backfill'))
        self.assertIn('the fix by hand', read(GUIDE_README).split('**Shadow and enforce.**')[1].split('\n\n')[1])

    def test_the_rows_never_fail_a_head_in_shadow(self):
        self.assertIn('The rows never fail a head in `shadow`', step(4))
        open_finding = review_judge.Rounds(5, True, True, (('code', 'major', 'major: x'),),
                                           ('lens code reported a major',))
        code, ok, reason, rows = run_decide('shadow', rounds=(open_finding, None))
        self.assertEqual((code, ok, reason), (0, True, 'every lens read the head'))
        self.assertEqual([good for _, good, _ in rows], [True, True, True])
        self.assertTrue(rows[1][2].startswith('they would have said: round 5 of 4'), rows[1][2])

    def test_the_same_rows_fail_it_in_enforce(self):
        self.assertIn('`enforce` would have failed a head that passed', step(4))
        open_finding = review_judge.Rounds(2, False, False, (('code', 'major', 'major: x'),),
                                           ('lens code reported a major',))
        code, ok, reason, rows = run_decide('enforce', rounds=(open_finding, None))
        self.assertEqual((code, ok), (1, False))
        self.assertIn('lens code reported a major', reason)
        self.assertIs(rows[1][1], False)

    def test_each_thing_the_guide_says_cannot_be_judged_fails_in_both_modes(self):
        """Step 4's list, item by item: a list item with no case here, or a case the list leaves out, goes red."""
        listed = re.search(r'a change the rules cannot judge \(([^)]+)\) fails it in both modes', step(4))
        self.assertIsNotNone(listed)
        items = [x.strip() for x in re.split(r', ', listed[1])]

        def unparsable(checkout, path, problems):
            problems.append('the config does not parse')
            return None
        rounds = (review_judge.Rounds(1, False, False, (), ()), None)
        cases = {
            'no merge base': dict(rounds=rounds, paths=(None, 'no merge base of the base and the head')),
            'a failed diff': dict(rounds=rounds, paths=(None, 'git diff failed')),
            'a config that does not parse': dict(rounds=rounds, base=unparsable),
            'lens comments that cannot be read': dict(rounds=(None, 'GitHub said no')),
        }
        self.assertEqual(items, list(cases))
        for item, kw in cases.items():
            for mode in ('shadow', 'enforce'):
                code, ok, _, _ = run_decide(mode, **kw)
                self.assertEqual((code in (1, H.REFUSED), ok), (True, False), f'{item}, {mode}')
        code, ok, reason, _ = run_decide('shadow', rounds=(None, 'GitHub said no'))
        self.assertIn('cannot be judged, so the head is not passed', reason)
        self.assertIn('lens comments that cannot be read (the rounds cannot be judged)',
                      flat(read(GUIDE_README).split('**Fail closed.**')[1].split('\n\n')[0]))


class EmptyInputFloor(unittest.TestCase):
    """Step 5: once the input may be empty (enforce), it is no floor. A change that only zero-lens rules match needs no
    lens; the guide's rule for agent instruction files keeps them at one."""

    def snippet(self):
        m = re.search(r'```json\n(.*?)\n\s*```', guide(), re.S)
        self.assertIsNotNone(m)
        return json.loads(m[1])

    def judged(self, review, path):
        return review_judge.judge(review, [('M', (path,))], CONFIG_PATH)

    def test_with_the_input_empty_a_markdown_change_needs_no_lens(self):
        enforce = step(5)
        self.assertIn('a change that only rules with `"lenses": []` match needs no lens at all', enforce)
        self.assertIn('matches agent instruction files such as `AGENTS.md` and `CLAUDE.md`', enforce)
        with mock.patch.object(H, 'base_config', return_value=parsed('enforce')):
            self.assertIsNone(G.empty_lenses_problem(object(), CONFIG_PATH))   # the empty input may stand
        review = parsed('enforce').review
        for path in ('AGENTS.md', 'tools/CLAUDE.md', 'docs/notes.txt'):
            judgement = self.judged(review, path)
            self.assertEqual((judgement.weight, judgement.lenses), ('light', ()), path)

    def test_the_guide_rule_keeps_agent_instruction_files_at_one_lens(self):
        self.assertIn('put a rule for the agent instruction files above the `docs` rule', step(5))
        raw = recommended()
        rules = raw['rules']
        at = next(i for i, r in enumerate(rules) if r['name'] == 'docs')
        raw = dict(raw, mode='enforce', rules=rules[:at] + [self.snippet()] + rules[at:])
        review = config.parse(json.dumps(dict(BASE, review=raw))).review   # enforce refuses a bad rule order
        for path in ('AGENTS.md', 'tools/CLAUDE.md'):
            self.assertEqual(self.judged(review, path).lenses, ('code',), path)
        self.assertEqual(self.judged(review, 'README.md').lenses, ())


if __name__ == '__main__':
    unittest.main()
