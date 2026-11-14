"""Tests of lens.py and config.py (run: python3 fork-ci/actions/pr-queue/lens.test.py).

testdata/lens-corpus.jsonl holds, one case per line, every call the source project's lens tests made to its copy of
these rules, with that copy's answer (README.md, "Where the rules come from"): 2021 cases, replayed here against lens.py. Logins and the
project name in it were replaced (owner, proj); the replaced corpus was checked against the source copy before it was
committed, with the source's override prefix set to `proj ready --head`. Where the port differs from the source on
purpose, the replay says so (Corpus).
"""
import copy, json, os, re, sys, unittest

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402

CORPUS = os.path.join(HERE, 'testdata', 'lens-corpus.jsonl')
SOURCE = lens.Review(('coderabbitai', 'github-actions', 'copilot'), 'proj ready --head', 'OWNER', ('OWNER',))
SHA_A, SHA_B, SHA_C = 'a' * 40, 'b' * 40, 'c' * 40
VALID = {
    'required_checks': ['build', 'test'], 'merge_method': 'squash', 'delete_branch': True,
    'bot_logins': ['review-bot'], 'lens_author_associations': ['OWNER'],
    'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
    'head_check': 'test',
}
# The port's deliberate changes to recorded reviewed_head answers (README.md, "Where the rules come from"): a read
# shorter than 40 hex, or a lens comment edited after posting, is a lens that read nothing.
SHORT_READ = re.compile(r'read[\s*`_-]*at\b[\s*`:_[]*([0-9a-f]{7,39})(?![0-9a-z])', re.I)
CHANGED = ('not a full 40-hex sha', lens.EDITED)


def post(body, login='someone', when='2026-01-01T00:00:00Z', **extra):
    """A comment as gh shows it, by default from the owner and never edited; a field set to ... is left out."""
    p = {'body': body, 'author': {'login': login}, 'createdAt': when, 'authorAssociation': 'OWNER',
         'includesCreatedEdit': False, **extra}
    return {k: v for k, v in p.items() if v is not ...}


def stranger(body, when='2026-01-02T00:00:00Z', association='NONE', **extra):
    return post(body, login='drive-by', when=when, authorAssociation=association, **extra)


def pr(*comments, head=SHA_B, commits=(SHA_A, SHA_B)):
    return {'headRefOid': head, 'comments': list(comments), 'reviews': [], 'commits': [{'oid': c} for c in commits]}


def as_owner(pr):
    """A recorded PR whose posts carry no GitHub author fields: the source's repository is private, so every one was
    written by an account with write access; posts that carry a field keep what was recorded."""
    pr = copy.deepcopy(pr)
    for p in (pr.get('comments') or []) + (pr.get('reviews') or []):
        if 'authorAssociation' not in p and 'includesCreatedEdit' not in p:
            p.update(authorAssociation='OWNER', includesCreatedEdit=False)
    return pr


class Corpus(unittest.TestCase):
    def test_every_recorded_call_gives_the_source_answer(self):
        """Every recorded answer, except: full_sha is gone (short reads name nothing), and a reviewed_head case whose
        input holds a short read or an edited lens comment may only change to no head, for that reason."""
        with open(CORPUS, encoding='utf-8') as f:
            cases = [json.loads(line) for line in f]
        self.assertEqual(len(cases), 2021)
        fns = {'lens_problem': SOURCE.lens_problem, 'reviewed_head': lambda pr: SOURCE.reviewed_head(as_owner(pr)),
               'override_sha': SOURCE.override_sha, 'lens_header': lens.lens_header, 'no_lens_text': lens.no_lens_text,
               'latest_checks': lens.latest_checks, 'check_state': lens.check_state, 'lens_key': lens.lens_key}
        self.assertEqual({c['fn'] for c in cases}, set(fns) | {'full_sha'})
        wrong, changed = [], 0
        for c in (c for c in cases if c['fn'] in fns):
            got = json.loads(json.dumps(fns[c['fn']](*c['args'])))
            if got == c['out']:
                continue
            posts = (c['args'][0].get('comments') or []) + (c['args'][0].get('reviews') or []) \
                if c['fn'] == 'reviewed_head' else []
            cause = any(SHORT_READ.search(p.get('body') or '') or p.get('includesCreatedEdit') for p in posts)
            if cause and got[:2] == [True, None] and any(w in got[2] for w in CHANGED):
                changed += 1
                continue
            wrong.append(f'{c["fn"]}{json.dumps(c["args"])[:160]}: got {got!r}, source {c["out"]!r}')
        self.assertEqual(wrong, [], f'{len(wrong)} of {len(cases)} differ; first: {wrong[:3]}')
        self.assertEqual(changed, 54, 'answers changed on purpose (a short read or an edited lens comment)')


class Gaps(unittest.TestCase):
    """Rules the corpus does not pin (a mutation of each passed it)."""
    def test_a_lens_heading_counts_only_in_the_first_five_plain_lines(self):
        top = ['Summary', 'one', 'two', 'three']
        self.assertIsNotNone(SOURCE.lens_problem(post('\n'.join(top + ['## Lens correctness', 'text']))))
        self.assertIsNone(SOURCE.lens_problem(post('\n'.join(top + ['four', '## Lens correctness', 'text']))))

    def test_a_hex_word_without_a_digit_is_no_sha(self):
        self.assertIsNone(SOURCE.lens_problem(post('Notes\n```\nlens: effaced\n```')))
        self.assertIn(lens.EMBEDDED, SOURCE.lens_problem(post('Notes\n```\nlens: effaced1\n```')))


class Advice(unittest.TestCase):
    """no_lens_text(keep_markdown=True), for the queue's own advice: the source cleaned it like a gh error, so its
    `<sha>` placeholder and the backticks around a command were dropped from the comment."""
    def test_advice_without_a_lens_word_keeps_its_markdown(self):
        for text in ('push (`tool push`)', 'post the override `tool ready --head <sha>: <why>`.'):
            self.assertEqual(lens.no_lens_text(text, keep_markdown=True), text)
            self.assertNotEqual(lens.no_lens_text(text), text, 'the default still cleans it')

    def test_advice_that_holds_a_lens_word_is_cleaned_like_any_text(self):   # also one markdown hides
        for text, out in (('`lens: x`', 'lens- x'), ('re`ad at`: x', 'read @: x'), ('**safe to merge**', 'safe to land')):
            self.assertEqual(lens.no_lens_text(text, keep_markdown=True), out)


class ProjectRules(unittest.TestCase):
    """What the config changes: the override comment and the bot logins."""
    def test_the_configured_override_prefix_counts_and_another_does_not(self):
        r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',))
        mine = post(f'tool ready --head {SHA_B}: read the merge', authorAssociation='OWNER', includesCreatedEdit=False)
        other = dict(mine, body=f'proj ready --head {SHA_B}: read the merge')
        self.assertEqual(r.override_sha(mine), SHA_B)
        self.assertIsNone(r.override_sha(other))

    def test_an_override_counts_only_from_the_configured_association_and_unedited(self):
        r = lens.Review((), 'tool ready --head', 'MEMBER', ('MEMBER',))
        body = f'tool ready --head {SHA_B}: read it'
        self.assertEqual(r.override_sha(post(body, authorAssociation='MEMBER', includesCreatedEdit=False)), SHA_B)
        for bad in ({'authorAssociation': 'OWNER', 'includesCreatedEdit': False},    # not the configured one
                    {'authorAssociation': 'MEMBER', 'includesCreatedEdit': True},    # edited after posting
                    {'authorAssociation': 'MEMBER', 'includesCreatedEdit': ...}):    # GitHub did not say
            self.assertIsNone(r.override_sha(post(body, **bad)), bad)

    def test_an_override_covers_older_lens_reads(self):
        r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',))
        p = pr(post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z'),
               post(f'tool ready --head {SHA_B}: read the rest', when='2026-01-02T00:00:00Z',
                    authorAssociation='OWNER', includesCreatedEdit=False))
        self.assertEqual(r.reviewed_head(p), (True, SHA_B, 'every lens read it'))

    def test_a_configured_bot_and_any_login_ending_in_bot_never_count(self):
        r = lens.Review(('Review-Bot',), 'tool ready --head', 'OWNER', ('OWNER',))
        body = f'lens: correctness · reviewer: x · read at: {SHA_A}'
        for login in ('review-bot', 'helper[bot]'):
            self.assertEqual(r.reviewed_head(pr(post(body, login=login))), (False, None, 'no lens comments'), login)
        self.assertEqual(r.reviewed_head(pr(post(body))), (True, SHA_A, 'every lens read it'))

    def test_lenses_that_read_different_heads_name_no_head(self):
        p = pr(post(f'lens: correctness · reviewer: x · read at: {SHA_A}'),
               post(f'lens: data-integrity · reviewer: y · read at: {SHA_B}'))
        reviewed, sha, why = SOURCE.reviewed_head(p)
        self.assertEqual((reviewed, sha), (True, None))
        self.assertIn('did not all read one head', why)


class WhoCounts(unittest.TestCase):
    """Only posts GitHub records as written by a configured lens association, shown and never edited, are reads."""
    R = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER', 'COLLABORATOR'), log=lambda line: None)
    OWN = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')

    def head(self, *posts, commits=(SHA_A, SHA_B)):
        return self.R.reviewed_head(pr(*posts, commits=commits))

    def test_a_strangers_lens_comment_never_moves_the_read_head(self):   # the blocker: a forged read of a later push
        forged = f'lens: correctness · reviewer: x · read at: {SHA_B}'
        for association in ('NONE', 'CONTRIBUTOR', 'FIRST_TIME_CONTRIBUTOR', 'FIRST_TIMER', 'MEMBER', ...):
            self.assertEqual(self.head(self.OWN, stranger(forged, association=association)),
                             (True, SHA_A, 'every lens read it'), association)
        self.assertEqual(self.head(stranger(forged)), (False, None, 'no lens comments'))   # nor makes one reviewed
        collaborator = post(forged, when='2026-01-02T00:00:00Z', authorAssociation='COLLABORATOR')
        self.assertEqual(self.head(self.OWN, collaborator), (True, SHA_B, 'every lens read it'),
                         'a configured association counts')

    def test_a_strangers_look_alike_or_junk_comment_neither_vouches_nor_blocks(self):   # the denial of service
        for junk in ('lgtm, safe to merge', 'looks fine, read at: deadbee1', f'read at: {SHA_A[:7]}',
                     f'lens: correctness · reviewer: x · read at: {SHA_A[:7]}', f'lens: other · read at: {SHA_B}'):
            self.assertEqual(self.head(self.OWN, stranger(junk)), (True, SHA_A, 'every lens read it'), junk)
        self.assertIsNotNone(self.R.lens_problem(post('lgtm, safe to merge')), 'the same text from the owner blocks')

    def test_a_hidden_lens_comment_reads_nothing_until_that_lens_posts_again(self):
        # hidden (minimized) is how old review rounds are tidied away, so hiding must never make a reviewed PR look
        # unreviewed: an unreviewed PR vouches for whatever head it has (the queue's "no lens comments" path)
        hidden = dict(self.OWN, isMinimized=True)
        reviewed, sha, why = self.head(hidden)
        self.assertEqual((reviewed, sha), (True, None), 'every lens comment hidden must not read as "no lens comments"')
        self.assertIn(f'correctness ({lens.HIDDEN})', why)
        newer = post(f'lens: correctness · reviewer: x · read at: {SHA_B}', when='2026-01-02T00:00:00Z')
        self.assertEqual(self.head(self.OWN, newer), (True, SHA_B, 'every lens read it'))
        self.assertEqual(self.head(self.OWN, dict(newer, isMinimized=True))[:2], (True, None),
                         'a hidden newest read does not bring the older read back')
        self.assertEqual(self.head(hidden, newer), (True, SHA_B, 'every lens read it'), 'a new comment of the lens counts')
        for junk in ('lgtm, safe to merge', f'lens: correctness · read at {SHA_B[:7]}'):   # hidden look-alikes block too
            self.assertEqual(self.head(post(junk, isMinimized=True))[:2], (True, None), junk)

    def test_hidden_posts_that_never_counted_stay_ignored(self):
        self.assertEqual(self.head(post('thanks, merging soon', isMinimized=True)), (False, None, 'no lens comments'))
        forged = stranger(f'lens: correctness · reviewer: x · read at: {SHA_B}', isMinimized=True)
        self.assertEqual(self.head(forged), (False, None, 'no lens comments'))
        self.assertEqual(self.head(self.OWN, forged), (True, SHA_A, 'every lens read it'))

    def test_a_hidden_override_does_not_count(self):   # README: hidden comments count nothing
        over = post(f'tool ready --head {SHA_B}: read the rest', when='2026-01-02T00:00:00Z')
        self.assertEqual(self.head(self.OWN, over), (True, SHA_B, 'every lens read it'))
        self.assertEqual(self.head(self.OWN, dict(over, isMinimized=True)), (True, SHA_A, 'every lens read it'))

    def test_a_strangers_huge_comment_costs_no_time(self):   # SEC-2 r3 major: anyone can post 64 KB of `[`
        import time
        shapes = ('[' * 65536, '<a' * 32768, '[x](' * 16384, '![' * 32768, ('[' * 1000 + '](') * 60,
                  'lens: correctness · read at: ' + '[' * 65000)
        for body in shapes:
            for who in (stranger(body), post(body)):   # never parsed in full / parsed in linear time
                start = time.monotonic()
                self.head(self.OWN, who)
                self.assertLess(time.monotonic() - start, 5, (body[:20], who.get('authorAssociation')))

    def test_a_lens_comment_whose_author_does_not_count_is_logged(self):
        # a wrong lens_author_associations makes reviewed PRs look unreviewed: the run log must say why
        lines = []
        r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',), log=lines.append)
        collaborator = post(self.OWN['body'], url='https://github.test/pr/7#c1', authorAssociation='COLLABORATOR')
        p = pr(collaborator, stranger('thanks!'), stranger('lgtm, safe to merge', association=...))
        self.assertEqual(r.reviewed_head(p), (False, None, 'no lens comments'))
        self.assertEqual(r.reviewed_head(p), (False, None, 'no lens comments'))
        self.assertEqual(len(lines), 2, lines)   # once per post however often the review is read; non-lens text: none
        self.assertIn('https://github.test/pr/7#c1', lines[0])
        self.assertIn('COLLABORATOR', lines[0])
        self.assertIn('lens_author_associations (OWNER)', lines[0])
        self.assertIn('no author association', lines[1])
        for line in lines:
            self.assertNotRegex(line, r'(?i)read at|safe to merge|lens\s*:', 'a log line quotes no comment text')

    def test_an_edited_lens_comment_read_nothing_until_that_lens_posts_again(self):
        for edited in (True, ...):   # GitHub says it was edited, or does not say
            reviewed, sha, why = self.head(post(self.OWN['body'], includesCreatedEdit=edited))
            self.assertEqual((reviewed, sha), (True, None), edited)
            self.assertIn(f'correctness ({lens.EDITED})', why)
        again = post(f'lens: correctness · reviewer: x · read at: {SHA_B}', when='2026-01-03T00:00:00Z')
        edited = dict(self.OWN, includesCreatedEdit=True)
        self.assertEqual(self.head(edited, again), (True, SHA_B, 'every lens read it'))

    def test_a_short_read_names_no_head(self):   # a force-push can bring a new commit with the same prefix
        for n in (7, 12, 39):
            short = post(f'lens: correctness · reviewer: x · read at: {SHA_B[:n]}')
            for commits in ((SHA_A, SHA_B), (SHA_B,)):
                reviewed, sha, why = self.head(short, commits=commits)
                self.assertEqual((reviewed, sha), (True, None), (n, commits))
                self.assertIn(f'correctness ({SHA_B[:min(n, 12)]}: not a full 40-hex sha)', why)

    def test_a_full_read_need_not_be_a_commit_of_the_pr(self):   # a rebase dropped it; the queue compares content
        read_c = post(f'lens: correctness · reviewer: x · read at: {SHA_C}')
        self.assertEqual(self.head(read_c, commits=(SHA_B,)), (True, SHA_C, 'every lens read it'))

    def test_an_override_counts_whether_or_not_its_association_writes_lenses(self):
        r = lens.Review((), 'tool ready --head', 'OWNER', ('COLLABORATOR',))
        lens_post = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', authorAssociation='COLLABORATOR')
        over = post(f'tool ready --head {SHA_B}: read the rest', when='2026-01-02T00:00:00Z')
        self.assertEqual(r.reviewed_head(pr(lens_post, over)), (True, SHA_B, 'every lens read it'))


class Config(unittest.TestCase):
    def parse(self, **change):
        raw = {**VALID, **change}
        return config.parse(json.dumps({k: v for k, v in raw.items() if v is not ...}))

    def refused(self, *why, **change):
        with self.assertRaises(config.ConfigError) as e:
            self.parse(**change)
        for w in why:
            self.assertIn(w, str(e.exception))

    def test_a_valid_config(self):
        c = self.parse()
        self.assertEqual(c.required_checks, frozenset({'build', 'test'}))
        self.assertEqual((c.override_prefix, c.override_association), ('tool ready --head', 'OWNER'))
        self.assertEqual((c.head_check, c.wait_seconds, c.poll_seconds), ('test', 60, 5))
        self.assertEqual(c.lens_author_associations, ('OWNER',))
        self.assertEqual(c.ready(12), 'tool ready 12')

    def test_required_checks_missing_or_empty_is_an_error(self):
        self.refused('required_checks is missing', required_checks=...)
        self.refused('required_checks is []', required_checks=[])
        self.refused('required_checks', required_checks=['build', 'build'])
        self.refused('required_checks', required_checks='build')

    def test_an_unknown_key_is_an_error(self):
        self.refused("unknown key 'required_check'", required_check=['build'])

    def test_every_problem_is_named_at_once(self):
        self.refused('merge_method', 'delete_branch', merge_method='fast-forward', delete_branch='yes')

    def test_the_override_needs_a_write_access_association(self):
        self.refused('override', override={'prefix': 'x', 'author_association': 'CONTRIBUTOR'})
        self.refused('override', override={'prefix': '', 'author_association': 'OWNER'})
        self.refused('override', override={'prefix': 'x'})

    def test_the_ready_command_names_the_pr(self):
        self.refused('ready_command', ready_command='tool ready')

    def test_head_check_must_be_a_required_check(self):
        self.assertEqual(self.parse(head_check='build').head_check, 'build')
        self.refused("head_check 'lint' is not one of required_checks", head_check='lint')

    def test_head_check_is_required(self):   # without one, a push to an armed, up-to-date PR merges unread
        self.refused('head_check is missing', head_check=...)
        self.refused('head_check is null', head_check=None)

    def test_lens_author_associations_are_required_and_only_ones_a_stranger_cannot_get(self):
        self.assertEqual(self.parse(lens_author_associations=['OWNER', 'MEMBER']).lens_author_associations,
                         ('OWNER', 'MEMBER'))
        for bad in (..., [], ['CONTRIBUTOR'], ['NONE'], ['OWNER', 'OWNER'], 'OWNER'):
            self.refused('lens_author_associations', lens_author_associations=bad)

    def test_a_key_written_twice_is_an_error(self):   # json keeps the last one: a shorter required_checks, quietly
        text = json.dumps(VALID)[:-1] + ', "required_checks": ["build"]}'
        for t in (text, json.dumps(VALID).replace('"author_association": "OWNER"',
                                                  '"author_association": "OWNER", "author_association": "MEMBER"')):
            with self.assertRaises(config.ConfigError) as e:
                config.parse(t)
            self.assertIn('key written twice', str(e.exception))

    def test_timings(self):
        self.refused('wait_seconds', wait_seconds=0)
        self.refused('wait_seconds', wait_seconds=True)
        self.refused('poll_seconds is longer', wait_seconds=10, poll_seconds=20)

    def test_not_json_or_not_an_object(self):
        for text in ('{', '[]', ''):
            with self.assertRaises(config.ConfigError):
                config.parse(text)

    def test_an_unreadable_file_is_an_error(self):
        with self.assertRaises(config.ConfigError) as e:
            config.load(os.path.join(HERE, 'testdata', 'no-such-config.json'))
        self.assertIn('cannot read it', str(e.exception))


if __name__ == '__main__':
    unittest.main()
