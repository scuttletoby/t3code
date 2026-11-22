"""Tests of lens.py and config.py (run: python3 fork-ci/actions/pr-queue/lens.test.py).

testdata/lens-corpus.jsonl holds, one case per line, every call the source project's lens tests made to its copy of
these rules, with that copy's answer (README.md, "Where the rules come from"): 2021 cases, replayed here against lens.py. Logins and the
project name in it were replaced (owner, proj); the replaced corpus was checked against the source copy before it was
committed, with the source's override prefix set to `proj ready --head`. Where the port differs from the source on
purpose, the replay says so (Corpus).
"""
import copy, json, os, random, re, sys, unittest

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402
import review_judge  # noqa: E402

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
        wrong, changed, opt_out, no_marker, held, folded = [], 0, 0, [], [], []
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
            if c['fn'] == 'reviewed_head' and got == [False, None, 'no lens comments'] \
                    and all(not lens.has_marker(p.get('body') or '') or SOURCE.override_sha(p) for p in posts):
                no_marker.append(c['args'])   # issue #865: its only look-alike is no lens report, as if never posted
                continue
            if c['fn'] == 'lens_problem' and c['out'] is None and (got or '').startswith(lens.OPT_OUT_READ):
                opt_out += 1   # issue #77: a hex `read at` below `not a lens:` blocks now
                continue
            if c['fn'] == 'lens_header' and lens.nfkc(c['args'][0]) != c['args'][0] and c['out'] \
                    and c['out'][1] is None \
                    and got == [c['out'][0], lens.READ_AT_RE.search(lens.nfkc(c['args'][0]))[1]]:
                folded.append(c['args'][0])   # issue #989: `read at：<sha>` reads its sha; the source read none
                continue
            if c['fn'] == 'reviewed_head' and any(lens.nfkc(p.get('body') or '') != p.get('body') for p in posts) \
                    and got[:2] == c['out'][:2] == [True, None] and lens.SHORT in got[2]:
                folded.append(posts)   # issue #989: the same answer (reads nothing), now because that read is short
                continue
            body = c['args'][0].get('body') or '' if c['fn'] == 'lens_problem' else ''
            if c['fn'] == 'lens_problem' and got is None and not lens.has_marker(body) \
                    and SOURCE.looks_problem(*c['args']) == c['out']:
                no_marker.append(body)   # issue #865: no lens marker, no lens report (named, never blocking)
                continue
            if c['fn'] == 'lens_problem' and SOURCE.report_problem(*c['args'])[1] == lens.HELD \
                    and got.endswith(f'; it looks like one: {c["out"]}'):
                held.append(body)   # issue #865: no lens marker but an open finding: it blocks as before, with the fix
                continue
            wrong.append(f'{c["fn"]}{json.dumps(c["args"])[:160]}: got {got!r}, source {c["out"]!r}')
        self.assertEqual(wrong, [], f'{len(wrong)} of {len(cases)} differ; first: {wrong[:3]}')
        self.assertEqual(changed, 54, 'answers changed on purpose (a short read or an edited lens comment)')
        self.assertEqual(opt_out, 2, 'answers changed on purpose (a hex read at below not a lens:, issue #77)')
        self.assertEqual(len(no_marker), 6 + 3, 'answers changed on purpose (no lens marker, issue #865)')
        self.assertEqual(len(held), 1, 'old state: a report with an open finding and no marker blocks as before')
        self.assertEqual(len(folded), 2, 'answers changed on purpose (a line is read in NFKC, issue #989)')
        self.assertIn('read at\uff1aabc0abc', folded[0])   # a short sha: as a read it still reads nothing
        self.assertIn('finding: major', held[0])
        # a lens heading, `safe to merge`, report fields: never a `lens:` line or a read
        for body in (b for b in no_marker if isinstance(b, str)):
            self.assertNotRegex(body, r'(?im)^\W*lens\s*:|read[\s-]*at\b\W*[0-9a-f]{7}')


class Gaps(unittest.TestCase):
    """Rules the corpus does not pin (a mutation of each passed it)."""
    def test_a_lens_heading_counts_only_in_the_first_five_plain_lines(self):
        top = ['Summary', 'one', 'two', 'three']
        self.assertIsNotNone(SOURCE.looks_problem(post('\n'.join(top + ['## Lens correctness', 'text']))))
        self.assertIsNone(SOURCE.looks_problem(post('\n'.join(top + ['four', '## Lens correctness', 'text']))))

    def test_a_hex_word_without_a_digit_is_no_sha(self):
        self.assertIsNone(SOURCE.lens_problem(post('Notes\n```\nlens: effaced\n```')))
        self.assertIn(lens.EMBEDDED, SOURCE.lens_problem(post('Notes\n```\nlens: effaced1\n```')))

    def test_a_second_header_in_any_field_form_below_a_valid_one_counts(self):   # issue #303 point 3
        first, hex = f'lens: tests · reviewer: x · read at: {SHA_B}\n\n', NotALens.HEX
        for second in (f'Lens — security · read at: {hex}', f'lens= security {hex[:10]}',
                       f'**Lens** – security ({hex[:7]})', f'- lens=security read at {hex}',
                       f'lens: security reviewed at {hex[:10]}'):
            with self.subTest(second=second):
                self.assertIn('2 times', SOURCE.lens_problem(post(first + second)))
                self.assertEqual(SOURCE.reviewed_head(pr(post(first + second)))[:2], (True, None))
        for note in ('Lens — security: nothing found', 'the security lens — see above', f'see lens= {hex} later',
                     f"    LENS = 'lens: correctness · reviewer: a · read at: {hex[:7]}'"):   # test code in a report
            with self.subTest(note=note):   # no sha, or the field is not at the line start: still one valid read
                self.assertIsNone(SOURCE.lens_problem(post(first + note)))

    def test_plain_lines_follow_githubs_fences(self):   # issue #303 point 3: the same fence rule as unquoted_lines
        self.assertEqual(lens.plain_lines('a\n```\nx\n```js\nb\n```\nc'), ['a', 'c'])   # ```js is code, not a close
        self.assertEqual(lens.plain_lines('a\n```x```\nb'), ['a', '```x```', 'b'])   # inline code, no fence
        self.assertEqual(lens.plain_lines('a\n~~~ x ~~~\nb\n~~~\nc'), ['a', 'c'])   # a tilde info string may hold one
        self.assertEqual(lens.plain_lines('a\n````\n```\nb\n````\nc'), ['a', 'c'])
        self.assertEqual(lens.plain_lines('a\n```\nb'), ['a'])   # unclosed: to the end
        # the rendered `safe to merge` line below the inner fence is plain text, and blocks
        self.assertIsNotNone(SOURCE.looks_problem(post('Notes\n```\nx\n```py\ny\n```\nsafe to merge: no\n```')))


class NotALens(unittest.TestCase):
    """Issue #77: below a `not a lens:` first line, a lens read written another way (no colon after `lens`, another
    field spelling) looks like a report too; only a `>` quote keeps such a line from counting. What a comment looks
    like is looks_problem; it blocks only with the lens marker (issue #865, class Marker)."""
    HEX = '7838aaec7c3cb4b6deaafa3d8cd701dd6f737f9a'

    def problem(self, body):
        return SOURCE.looks_problem(post(body))

    def test_a_lens_line_without_the_colon_below_the_opt_out_blocks(self):
        for read in (self.HEX, self.HEX[:10], 'abc1234'):
            for line in (f'lens correctness, read at {read}', f'Lens — correctness · read at: {read}',
                         f'lens=correctness read at {read}', f'read at: {read}', f'the round the lens read at {read}'):
                for body in (f'not a lens: round 2 fixes\n{line}', f'not a lens: notes\n\n```\n{line}\n```',
                             f'not a lens: notes\n    {line}', f'not a lens: {line}'):
                    with self.subTest(body=body):
                        self.assertIsNotNone(self.problem(body))

    def test_another_field_spelling_with_a_sha_blocks_with_or_without_the_opt_out(self):
        for line in (f'Lens — correctness ({self.HEX[:10]})', f'lens= correctness {self.HEX}',
                     f'lens – correctness at {self.HEX[:7]}'):
            for body in (f'not a lens: notes\n{line}', f'Notes\n{line}'):
                with self.subTest(body=body):
                    self.assertIn(lens.EMBEDDED, self.problem(body))

    def test_a_quoted_read_or_one_without_a_sha_stays_clear(self):
        for body in (f'not a lens: round 2 fixes\n> lens correctness, read at {self.HEX}',
                     f'not a lens: notes\n>> correctness, read at: {self.HEX}',
                     'not a lens: notes\nthe correctness lens read at the old head; read at: <sha> is the format',
                     'not a lens: fix round for the lens findings\nLens — correctness, see above',
                     f'not a lens: fixed in {self.HEX}\nlens-check passed'):
            with self.subTest(body=body):
                self.assertIsNone(self.problem(body))

    def test_a_quoted_lens_field_with_a_sha_blocks_in_any_spelling(self):   # as `lens:` did before: EMBEDDED
        for field in ('lens:', 'Lens —', 'lens='):
            with self.subTest(field=field):
                self.assertIn(lens.EMBEDDED, self.problem(f'not a lens: notes\n> {field} correctness at {self.HEX}'))

    def test_a_quote_marker_inside_a_fence_is_code_not_a_quote(self):
        self.assertIsNotNone(self.problem(f'not a lens: notes\n```\n> read at {self.HEX}\n```'))

    def test_a_read_in_inline_code_below_the_opt_out_blocks(self):   # the line is read cleaned, not raw
        self.assertIsNotNone(self.problem(f'not a lens: notes\nread at: `{self.HEX}`'))

    def test_a_quote_marker_indented_four_spaces_is_code_not_a_quote(self):
        self.assertIsNotNone(self.problem(f'not a lens: notes\n\n    > read at {self.HEX}'))

    def test_the_queues_quote_of_any_lens_field_never_reads_as_a_lens_line(self):   # l283-state: leave_off quotes it
        for line in (f'Lens — security ({self.HEX[:10]}) NOT SAFE', f'lens= security {self.HEX}',
                     f'lens – security at {self.HEX[:7]}', f'lens: security read at: {self.HEX}'):
            with self.subTest(line=line):
                quoted = lens.no_lens_text(line)
                self.assertFalse(lens.loose_header(lens.norm_line(quoted)), quoted)
                self.assertIsNone(self.problem(f'Auto-merge OFF: {quoted}'), quoted)

    def test_the_queues_quote_of_nested_markup_never_reads_as_a_lens_line(self):   # issue #303 point 1
        for field in (':', '=', '—', '–'):
            for line in (f'lens<<a>b>{field} sec {self.HEX[:10]} NOT SAFE', f'lens[[](z)](w){field} sec {self.HEX}',
                         f'lens<<<a>b>c>{field} sec {self.HEX[:10]}', f'lens[[[](z)](y)](w){field} {self.HEX}',
                         f'read<<a>b> at {self.HEX}', f'safe<<a>b> to merge: no ({self.HEX[:10]})'):
                with self.subTest(line=line):
                    quoted = lens.no_lens_text(line)
                    for narrow in (False, True):
                        self.assertFalse(lens.loose_header(lens.norm_line(quoted, narrow)), quoted)
                    self.assertIsNone(self.problem(f'pr_queue turned auto-merge OFF: {quoted}'), quoted)

    def test_the_queues_quote_of_any_text_reads_the_same_when_cleaned_again(self):   # issue #303 point 1
        rnd = random.Random(303)
        parts = ['lens', 'read', ' at', 'safe', ' to ', 'merge', ':', '=', '—', '–', '<', '>', '<a>', '<b ', '[', ']',
                 '](x)', '(', ')', '!', '*', '`', '_', '|', ' ', '\u00a0', 'x', self.HEX[:10], '#']
        for _ in range(20000):
            text = ''.join(rnd.choice(parts) for _ in range(rnd.randrange(1, 16)))
            quoted = lens.no_lens_text(text)
            with self.subTest(text=text):
                for narrow in (False, True):
                    self.assertEqual(lens.clean_inline(quoted, narrow), quoted)
                self.assertIsNone(self.problem(f'pr_queue turned auto-merge OFF: {quoted}'))

    def test_a_note_names_a_sha_on_its_own_line_not_after_a_lens_field(self):   # issue #303 point 2: the README rule
        self.assertIn(lens.EMBEDDED, self.problem(f'not a lens: round 2\nthe security lens — fixed in {self.HEX[:8]}'))
        quoted = f'not a lens: round 2\n> the security lens — fixed in {self.HEX[:8]}'   # a quote does not help
        self.assertIn(lens.EMBEDDED, self.problem(quoted))
        self.assertIsNone(self.problem(f'not a lens: round 2\nthe security lens — fixed in:\n{self.HEX[:8]}'))
        self.assertIsNone(self.problem(f'not a lens: round 2\nthe security lens: fixed.\n> read at {self.HEX}'))

    def test_a_quote_marker_after_a_tab_is_code_not_a_quote(self):   # #283 bypass M9: `\s*>` would clear it
        self.assertIsNotNone(self.problem(f'not a lens: notes\n\n\t> read at {self.HEX}'))

    def test_only_a_fence_of_the_same_kind_and_length_closes_one(self):   # #283 bypass M10: any fence line closed
        for body in (f'not a lens: notes\n~~~\n```\n> read at {self.HEX}\n~~~',
                     f'not a lens: notes\n````\n```\n> read at {self.HEX}\n````',
                     f'not a lens: notes\n```\n~~~\n> read at {self.HEX}\n```'):
            with self.subTest(body=body):
                self.assertIn(lens.OPT_OUT_READ, self.problem(body))

    def test_a_fence_line_with_an_info_string_does_not_close_a_fence(self):   # issue #303 point 3, GFM
        for close in ('``` y', '```js', '~~~ y'):
            body = (f'not a lens: r2\n{close[:3]}\nx\n{close}\n> correctness lens NOT SAFE read at {self.HEX}\n'
                    f'{close[:3]}')
            with self.subTest(close=close):
                self.assertIn(lens.OPT_OUT_READ, self.problem(body))
        self.assertIsNone(self.problem(f'not a lens: r2\n```\nx\n```  \n> read at {self.HEX}'))   # blanks only: closes

    def test_a_longer_closing_fence_closes_one_and_only_blanks_may_follow_it(self):   # GFM; #591 lens mutants
        self.assertIsNone(self.problem(f'not a lens: r2\n```\nx\n````\n> read at {self.HEX}'))
        # a no-break space is no space or tab: the fence stays open, the quote is code
        self.assertIn(lens.OPT_OUT_READ, self.problem(f'not a lens: r2\n```\nx\n```\u00a0\n> read at {self.HEX}'))

    def test_a_fence_opens_and_closes_only_after_at_most_3_spaces(self):   # GFM; #591 lens mutants
        self.assertIn(lens.OPT_OUT_READ, self.problem(f'not a lens: r2\n   ```\n> read at {self.HEX}'))
        self.assertIsNone(self.problem(f'not a lens: r2\n    ```\n\n> read at {self.HEX}'))   # indented code, no fence
        self.assertIn(lens.OPT_OUT_READ,
                      self.problem(f'not a lens: r2\n```\nx\n    ```\n> read at {self.HEX}\n```'))

    def test_the_way_out_names_the_quote_and_never_reads_as_a_report(self):
        self.assertIn('`>`', lens.NO_HEADER_WAY_OUT)
        self.assertEqual(lens.no_lens_text(lens.NO_HEADER_WAY_OUT, keep_markdown=True), lens.NO_HEADER_WAY_OUT)


class Marker(unittest.TestCase):
    """Issue #865: a comment is a lens report only when it carries the lens marker (lens.has_marker: a line starting
    `lens:`, a lens field naming a sha, or `read at <hex>`). One that only looks like a report without it is no lens
    report: it neither reads nor blocks, and not_lens_reports names it (the head check's summary), unless it holds an
    open finding (a blocker or major that is not none, or NOT SAFE): then it blocks, as a NO_HEADER entry whose reason
    names the missing marker and the fix (report_problem kind HELD). A lens report that is not one valid read still
    blocks."""
    READ = f'lens: correctness · reviewer: x · read at: {SHA_B}\nstate: fine\nblocker: none\nmajor: none'
    NEAR = ('## Lens A\nblocker: none\nmajor: none', '## Lens correctness\nsafe to merge: yes', 'lgtm, safe to merge',
            'covered: a.py\nchecks: ran the tests', 'The merge-path lens: nothing found',
            '## Lens A, round 2\nblocker: none\nmajor: none\nminor: a typo',
            '## Lens A, round 2\n\ncovered: x\nchecks: y\n\nThe old code was not safe to run twice.',   # no "to merge"
            '## Lens A, round 2\n\ncovered: x\n\n```\nNot safe to merge: an example\n```')                # in a fence
    HELD = ('## Lens correctness, round 2\nblocker: a re-run loses data\nmajor: none',
            '**Lens correctness** (round 2)\nverdict: not safe to merge',
            'NOT SAFE to merge (code lens, round 2)\nmore below',
            'Code lens, round 2\nsafe to merge: no\nmajor: the retry drops a row', '## Lens A\nmajor: x is cut',
            'covered: a.py\nchecks: ran them\nfinding: blocker a.py:3 loses data',
            # round 2: NOT SAFE on the line under the heading (the title line), or in a sentence further down
            '## Lens correctness, round 2\n\n**NOT SAFE to merge**\n\ncovered: x\nchecks: y',
            '## Merge-path review\n\nNOT SAFE TO MERGE\n\ncovered: x\nchecks: y',
            '## Lens merge-path, round 2\n\nNot safe to merge: the gate passes an unread head.',
            '## Lens correctness, round 2\n\ncovered: x\nchecks: y\n\nNot safe to merge: the gate passes an unread head.')
    BROKEN = (f'Here is my review.\n{READ}',                                   # the header not on the first line
              f'{READ}\nlens: state · reviewer: x · read at: {SHA_B}',         # two lenses in one comment
              'lens: <name> · reviewer: x · read at: <sha>\nmajor: none',     # a placeholder name: names no lens
              f'## Lens A\nread at: {SHA_B}\nmajor: none',                     # a lens heading that names a read
              f'reviewer: lens-a · read at: {SHA_B[:7]}',                       # a read, short sha, no lens field
              f'Report — lens: code at {NotALens.HEX[:10]}',                    # a lens field naming a sha mid-line
              f'```\n{READ}\n```')                                             # in a code block

    def setUp(self):
        self.lines = []
        self.r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',), log=self.lines.append)

    def test_a_near_miss_is_no_lens_report_and_is_named(self):
        for body in self.NEAR:
            with self.subTest(body=body):
                self.assertFalse(lens.has_marker(body))
                self.assertIsNotNone(self.r.looks_problem(post(body)), 'it looks like a report')
                self.assertIsNone(self.r.lens_problem(post(body)), 'but blocks nothing')
                self.assertIn(lens.NO_MARKER, self.r.not_lens_report(post(body)))
                self.assertEqual(self.r.reviewed_head(pr(post(body))), (False, None, 'no lens comments'))
                p = pr(post(self.READ), post(body, url='https://github.test/c2', when='2026-01-02T00:00:00Z'))
                self.assertEqual(self.r.reviewed_head(p), (True, SHA_B, 'every lens read it'))
                self.assertEqual([w for w, _ in self.r.not_lens_reports(p)], ['https://github.test/c2'])
                self.assertEqual(self.r.lens_scan(p)[1], [('correctness', SHA_B)], 'no round either')

    def test_a_lens_report_with_a_broken_body_still_blocks(self):   # fail closed: never a pass
        for body in self.BROKEN:
            with self.subTest(body=body):
                self.assertTrue(lens.has_marker(body))
                self.assertIsNotNone(self.r.lens_problem(post(body)))
                self.assertIsNone(self.r.not_lens_report(post(body)))
                p = pr(post(self.READ), post(body, when='2026-01-02T00:00:00Z'))
                self.assertEqual(self.r.reviewed_head(p)[:2], (True, None))
                self.assertEqual(self.r.not_lens_reports(p), [])

    def test_a_valid_report_and_a_plain_comment_read_as_before(self):   # old comments on open PRs: same answers
        self.assertIsNone(self.r.lens_problem(post(self.READ)))
        self.assertIsNone(self.r.not_lens_report(post(self.READ)))
        self.assertIsNone(self.r.not_lens_report(post('Thanks, merging after CI.')))
        self.assertEqual(self.r.reviewed_head(pr(post(self.READ))), (True, SHA_B, 'every lens read it'))
        opt_out = f'not a lens: round 2\n> read at {SHA_B}'
        self.assertEqual((self.r.lens_problem(post(opt_out)), self.r.not_lens_report(post(opt_out))), (None, None))

    def test_every_problem_with_the_marker_is_the_old_answer(self):   # the marker only ever drops a block
        for body in self.NEAR + self.HELD + self.BROKEN + (self.READ, 'Thanks', f'not a lens: x\nread at: {SHA_B}'):
            with self.subTest(body=body):
                looks, now = self.r.looks_problem(post(body)), self.r.lens_problem(post(body))
                if lens.has_marker(body):
                    self.assertEqual(now, looks)
                elif looks and review_judge.findings(body, unmarked=True):   # an open finding without the marker blocks
                    self.assertTrue(now.startswith(lens.NO_MARKER) and now.endswith(f'; it looks like one: {looks}'))
                else:
                    self.assertIsNone(now)
                self.assertEqual(self.r.not_lens_report(post(body)) is not None, looks is not None and now is None)

    def test_an_unmarked_report_with_an_open_finding_blocks_and_names_the_fix(self):   # issue #865 round 1
        for body in self.HELD:
            with self.subTest(body=body):
                self.assertFalse(lens.has_marker(body))
                why = self.r.lens_problem(post(body))
                self.assertTrue(why.startswith(lens.NO_MARKER), why)
                self.assertIn(lens.REPOST, why)
                self.assertEqual(self.r.report_problem(post(body))[1], lens.HELD)
                self.assertIsNone(self.r.not_lens_report(post(body)))
                alone = pr(post(body, url='https://github.test/c2'))
                self.assertEqual(self.r.reviewed_head(alone)[:2], (True, None), 'no required lens: still held')
                self.assertEqual([w for w, _ in self.r.unmarked_held(alone)], ['https://github.test/c2'])
                for older, newer in ((post(self.READ), post(body, when='2026-01-02T00:00:00Z')),
                                     (post(body), post(self.READ, when='2026-01-02T00:00:00Z'))):
                    p = pr(older, newer)   # newest wins never lets an older clean read through, and a newer one
                    reviewed, sha, why = self.r.reviewed_head(p)   # does not clear it (keyed by the comment)
                    self.assertEqual((reviewed, sha), (True, None))
                    self.assertIn(lens.NO_MARKER, why)
                    self.assertEqual(self.r.reviewed_head(p), (reviewed, sha, why), 'run again: the same answer')
                    self.assertEqual(self.r.not_lens_reports(p), [])
                    self.assertEqual(self.r.lens_scan(p)[1], [('correctness', SHA_B)], 'no round of its own')
                self.assertEqual(self.lines, [], 'a blocking entry is in the verdict, not only in the log')

    def test_not_safe_under_the_heading_or_in_a_sentence_holds(self):   # issue #865 round 2: main blocked these
        for body in self.HELD[-4:]:
            with self.subTest(body=body):
                self.assertEqual(review_judge.findings(body), [], 'read as a marked report, no finding: the old hole')
                self.assertEqual(review_judge.findings(body, unmarked=True)[0][0], 'blocker')
                self.assertEqual(self.r.report_problem(post(body))[1], lens.HELD)
                p = pr(post(self.READ), post(body, url='https://github.test/c2', when='2026-01-02T00:00:00Z'))
                self.assertEqual(self.r.reviewed_head(p)[:2], (True, None), 'an older clean read never wins')
                self.assertEqual(self.r.reviewed_head(p), self.r.reviewed_head(p), 'run again: the same answer')
        self.assertEqual(review_judge.findings(self.NEAR[-2], unmarked=True), [], 'not safe to run twice: no verdict')
        self.assertEqual(review_judge.findings(self.NEAR[-1], unmarked=True), [], 'in a code block: an example')

    def test_a_held_comment_edited_to_hold_no_open_finding_stops_holding(self):   # the same as deleting it
        for body in (self.HELD[0], self.HELD[-1]):
            with self.subTest(body=body):
                edited = (body.replace('a re-run loses data', 'none') if 'blocker:' in body
                          else body.replace('Not safe to merge', 'Safe to merge'))
                p = pr(post(self.READ), post(edited, url='https://github.test/c2', when='2026-01-02T00:00:00Z',
                                             includesCreatedEdit=True))
                self.assertEqual(self.r.reviewed_head(p), (True, SHA_B, 'every lens read it'))
                self.assertEqual([w for w, _ in self.r.not_lens_reports(p)], ['https://github.test/c2'])

    def test_the_queue_posts_a_held_reason_readable_and_lens_free(self):   # round 2: it read "lens-  · reviewer:"
        for text in (lens.REPOST_PLAIN, lens.HELD_WAY_OUT):
            self.assertEqual(lens.no_lens_text(text, keep_markdown=True), text)
        for body in (self.HELD[0], self.HELD[-1]):
            with self.subTest(body=body):
                why = self.r.lens_problem(post(body))
                self.assertIn(lens.REPOST, why)
                out = lens.no_lens_text(' '.join(why.split()))
                self.assertIn(lens.REPOST_PLAIN, out)
                self.assertNotIn('lens-  ·', out)
                self.assertIn(lens.HELD_WAY_OUT.replace('`', ''), out, 'the way out stays readable')
                self.assertNotIn('not a lens-', out)
                self.assertTrue(lens._lens_free(out), out)
                self.assertIsNone(self.r.looks_problem(post(out)), 'the queue comment never looks like a report')

    def test_an_unmarked_report_with_an_open_finding_posted_twice_holds_twice(self):
        p = pr(post(self.READ), post(self.HELD[0], url='https://github.test/c2', when='2026-01-02T00:00:00Z'),
               post(self.HELD[0], url='https://github.test/c3', when='2026-01-03T00:00:00Z'))
        self.assertEqual([w for w, _ in self.r.unmarked_held(p)], ['https://github.test/c2', 'https://github.test/c3'])
        self.assertEqual(sum(k.startswith(lens.NO_HEADER) for k in self.r.lens_scan(p)[0]), 2)

    def test_the_ways_out_of_an_unmarked_report_with_an_open_finding(self):
        held = self.HELD[0]
        gone = pr(post(self.READ))   # deleted: GitHub no longer lists it
        self.assertEqual(self.r.reviewed_head(gone), (True, SHA_B, 'every lens read it'))
        opted = post(f'not a lens: round 2 notes\n{held}', when='2026-01-02T00:00:00Z', includesCreatedEdit=True)
        self.assertEqual(self.r.reviewed_head(pr(post(self.READ), opted)), (True, SHA_B, 'every lens read it'))
        marked = post(f'lens: correctness · reviewer: x · read at: {SHA_B}\n{held}', when='2026-01-02T00:00:00Z',
                      includesCreatedEdit=True)   # the marker edited in: an edited report reads nothing, post anew
        reviewed, sha, why = self.r.reviewed_head(pr(post(self.READ), marked))
        self.assertEqual((reviewed, sha), (True, None))
        self.assertIn(lens.EDITED, why)
        for flags in ({'includesCreatedEdit': True}, {'isMinimized': True}):   # edited or hidden: still holds
            with self.subTest(flags=flags):
                p = pr(post(self.READ), post(held, when='2026-01-02T00:00:00Z', **flags))
                self.assertEqual(self.r.reviewed_head(p)[:2], (True, None))

    def test_the_marker_is_read_both_ways_and_in_any_markdown(self):
        for body in ('**Lens:** a', '## lens: a', '- lens: a', '> lens: a', '    lens: a', f'| read at | {SHA_B} |',
                     f'read at: `{SHA_B[:7]}`', f'lens<a title="<">: x · read at<a title="<">: {SHA_B}',
                     'Lens — a', 'lens= a'):
            with self.subTest(body=body):
                self.assertTrue(lens.has_marker(body))
        for body in ('lensing: a', 'the lens: a', 'read at: the old head', 'lenses: a', 'not a lens: a', 'LENS = x'):
            with self.subTest(body=body):
                self.assertFalse(lens.has_marker(body))

    def test_edited_and_hidden_near_misses_stay_no_report(self):
        for flags in ({'includesCreatedEdit': True}, {'isMinimized': True}, {'includesCreatedEdit': ...}):
            with self.subTest(flags=flags):
                p = pr(post(self.READ), post(self.NEAR[0], when='2026-01-02T00:00:00Z', **flags))
                self.assertEqual(self.r.reviewed_head(p), (True, SHA_B, 'every lens read it'))
                self.assertEqual(len(self.r.not_lens_reports(p)), 1)

    def test_a_report_edited_to_drop_its_marker_is_as_if_deleted(self):   # GitHub keeps no old text: only now counts
        dropped = post(self.NEAR[0].replace('Lens A', 'Lens correctness'), when='2026-01-02T00:00:00Z',
                       includesCreatedEdit=True)
        self.assertEqual(self.r.reviewed_head(pr(post(self.READ), dropped)), (True, SHA_B, 'every lens read it'))
        dropped = post(self.HELD[0], when='2026-01-02T00:00:00Z', includesCreatedEdit=True)   # but an open finding
        self.assertEqual(self.r.reviewed_head(pr(post(self.READ), dropped))[:2], (True, None))
        kept = post(self.READ.replace(SHA_B, SHA_A), when='2026-01-02T00:00:00Z', includesCreatedEdit=True)
        reviewed, sha, why = self.r.reviewed_head(pr(post(self.READ), kept))
        self.assertEqual((reviewed, sha), (True, None), 'an edited report with its marker still reads nothing')
        self.assertIn(lens.EDITED, why)

    def test_two_comments_for_one_lens_and_head(self):
        later = post(self.READ.replace('major: none', 'major: x is cut'), when='2026-01-02T00:00:00Z')
        found, history = self.r.lens_scan(pr(post(self.READ), later))
        self.assertEqual(list(found), ['correctness'])
        self.assertIs(found['correctness'][3], later, 'the newer report is the one judged')
        self.assertEqual(history, [('correctness', SHA_B)] * 2)
        near = post(f'## Lens correctness\nmajor: none\nsafe to merge: yes', when='2026-01-02T00:00:00Z')
        found, history = self.r.lens_scan(pr(post(self.READ), near))
        self.assertIs(found['correctness'][3]['body'], self.READ, 'a near miss never replaces the report')
        self.assertEqual(history, [('correctness', SHA_B)])

    def test_a_near_miss_is_logged_once_and_quotes_only_its_start(self):
        p = pr(post(self.NEAR[0], url='https://github.test/c1'))
        self.r.reviewed_head(p)
        self.r.reviewed_head(p)
        self.assertEqual(len(self.lines), 1, self.lines)
        self.assertTrue(self.lines[0].startswith(f'{lens.NOT_LENS} (https://github.test/c1): {lens.NO_MARKER}'))

    def test_a_strangers_near_miss_is_still_only_logged_as_ignored(self):
        p = pr(stranger(self.NEAR[0], url='https://github.test/s1'))
        self.assertEqual(self.r.not_lens_reports(p), [])
        self.assertEqual(len(self.lines), 1)
        self.assertIn('ignored a lens-looking post https://github.test/s1', self.lines[0])


class FullWidth(unittest.TestCase):
    """Issue #989: a header written with the full-width colon `：` (U+FF1A), or another compatibility form NFKC folds
    (`﹕` U+FE55, full-width letters and digits), was neither a header, nor the marker, nor a look-alike: the comment
    counted for nothing. norm_line folds each line with NFKC first, as the findings rules (review_judge.fold) already
    do, so it reads as the plain form it renders like."""
    FW = f'lens： code · reviewer： x · read at： {SHA_B}\nblocker： none\nmajor： none'
    MIXED = f'lens： code · reviewer: x · read at: {SHA_B}\nblocker: none\nmajor: none'   # blocked as broken before
    WIDE = f'ｌｅｎｓ﹕ code · reviewer﹕ x · ｒｅａｄ ａｔ﹕ {"ｂ" * 40}\nblocker: none\nmajor: none'

    def setUp(self):
        self.r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',), log=lambda _: None)

    def test_a_full_width_header_is_a_header_and_a_read(self):
        for body in (self.FW, self.MIXED, self.WIDE):
            for narrow in (False, True):
                with self.subTest(body=body, narrow=narrow):
                    self.assertEqual(lens.lens_header(body, narrow), ('code', SHA_B))
            with self.subTest(body=body):
                self.assertTrue(lens.has_marker(body))
                self.assertEqual(self.r.report_problem(post(body)), (None, None))
                self.assertEqual(self.r.reviewed_head(pr(post(body))), (True, SHA_B, 'every lens read it'))

    def test_a_broken_full_width_report_blocks_and_is_never_silent(self):   # the marker is seen: fail closed
        for body in (f'Here is my review.\n{self.FW}', f'{self.FW}\nlens： state · reviewer： x · read at： {SHA_B}',
                     'lens： <name> · reviewer： x · read at： <sha>\nmajor： none', f'```\n{self.FW}\n```'):
            with self.subTest(body=body):
                self.assertTrue(lens.has_marker(body))
                self.assertEqual(self.r.report_problem(post(body))[1], lens.BROKEN)
                p = pr(post(Marker.READ), post(body, when='2026-01-02T00:00:00Z'))
                self.assertEqual(self.r.reviewed_head(p)[:2], (True, None))

    def test_a_full_width_look_alike_without_the_marker_is_named(self):   # neither silent nor a read
        for body in ('## Ｌｅｎｓ correctness\nｓａｆｅ ｔｏ ｍｅｒｇｅ： yes', '## Ｌｅｎｓ correctness\nblocker: none\nmajor: none'):
            with self.subTest(body=body):
                self.assertFalse(lens.has_marker(body))
                self.assertIn(lens.NO_MARKER, self.r.not_lens_report(post(body)))

    def test_old_state_an_older_full_width_read_does_not_outvote_a_newer_one(self):   # comments posted before the fix
        newer = post(Marker.READ.replace(SHA_B, SHA_A).replace('correctness', 'code'), when='2026-01-02T00:00:00Z')
        self.assertEqual(self.r.reviewed_head(pr(post(self.FW), newer)), (True, SHA_A, 'every lens read it'))
        older = post(Marker.READ.replace(SHA_B, SHA_A).replace('correctness', 'code'), when='2025-12-31T00:00:00Z')
        self.assertEqual(self.r.reviewed_head(pr(older, post(self.FW))), (True, SHA_B, 'every lens read it'))
        edited = post(self.FW, includesCreatedEdit=True)   # edited after posting: a lens that read nothing
        reviewed, sha, why = self.r.reviewed_head(pr(edited))
        self.assertEqual((reviewed, sha), (True, None))
        self.assertIn(lens.EDITED, why)

    def test_runs_again_with_the_same_answer(self):   # the gate keeps no state: a re-run reads the comments afresh
        p = pr(post(self.FW))
        self.assertEqual(self.r.reviewed_head(p), self.r.reviewed_head(copy.deepcopy(p)))

    def test_text_the_queue_posts_never_reads_as_a_lens_form(self):   # no_lens_text folds what the reader folds
        for text in ('lens： x', f'read at： {SHA_B[:12]}', f'ｒｅａｄ ａｔ {SHA_B[:12]}', 'ｓａｆｅ ｔｏ ｍｅｒｇｅ',
                     'lens﹦ x', f'`lens： x · read at： {SHA_B}`'):
            for keep in (False, True):
                with self.subTest(text=text, keep=keep):
                    out = lens.no_lens_text(text, keep_markdown=keep)
                    self.assertFalse(lens.has_marker(out), out)
                    self.assertIsNone(self.r.looks_problem(post(f'pr_queue turned auto-merge OFF: {out}')), out)


class Markdown(unittest.TestCase):
    """clean_inline keeps the source's answers (`</?[A-Za-z][^>]*>` tags, `!?\\[([^\\]]*)\\]\\([^)]*\\)` links) in linear time."""

    def test_a_tag_with_a_lt_in_a_quoted_attribute_still_hides_nothing(self):   # GitHub renders it as a header
        body = f'lens<a title="<">: correctness · read at<a title="<">: {SHA_A}\n\nsafe<a title="<"> to merge: no'
        self.assertEqual(lens.clean_inline('lens<a title="<">: x'), 'lens: x')
        pr = {'comments': [post(body)], 'reviews': []}
        self.assertEqual(SOURCE.reviewed_head(pr)[:2], (True, None),
                         'never unseen: the source reading shows a header, the narrow one none, so it reads nothing')

    def test_a_header_only_the_narrow_reading_shows_is_still_read(self):   # GitHub renders it as visible text
        old = '1234567' + 'a' * 33   # a digit first: `<1234567...>` is no tag either way
        valid = post(f'lens: correctness · reviewer: x · read at: {SHA_B}', when='2026-01-01T00:00:00Z')
        for body in (f'<lens: security · reviewer: x · read at: <{old}></b>',
                     f'note x<y lens: security · reviewer: x · read at: <{old}> <br>'):
            with self.subTest(body):
                stale = post(body, when='2026-01-01T00:01:00Z')
                self.assertIsNotNone(SOURCE.lens_problem(stale), 'one reading shows a header, the other none')
                self.assertEqual(SOURCE.reviewed_head(pr(valid, stale, commits=(old, SHA_B)))[:2], (True, None),
                                 'a lens that read nothing: the PR is not reviewed at its head')
        self.assertEqual(SOURCE.reviewed_head(pr(valid, commits=(old, SHA_B)))[:2], (True, SHA_B))

    def test_a_comment_counts_only_when_both_readings_give_the_same_header(self):
        both = post(f'lens: <b>correctness</b> · reviewer: x · read at: {SHA_B}')
        self.assertIsNone(SOURCE.lens_problem(both))
        self.assertEqual(lens.lens_header(both['body']), lens.lens_header(both['body'], narrow=True))
        one = post(f'lens<a title="<">: correctness · read at<a title="<">: {SHA_B}')
        self.assertIsNone(lens.lens_header(one['body'], narrow=True))
        self.assertIn('depending on how its HTML', SOURCE.lens_problem(one), 'the other way round: flagged too')

    def test_a_second_header_only_the_source_reading_shows_is_still_flagged(self):
        """The wide (source) reading's own problem check: main's narrow reading sees one header here, the source two."""
        old = 'c' * 40
        body = (f'lens: tests · reviewer: x · read at: {SHA_B}\n\n'
                f'lens<a title="<">: correctness · read at<a title="<">: {old}')
        self.assertIsNone(SOURCE.read_problem(body, narrow=True), 'the narrow reading alone sees nothing wrong')
        self.assertIn('2 times', SOURCE.lens_problem(post(body)))
        self.assertEqual(SOURCE.reviewed_head(pr(post(body), commits=(old, SHA_B)))[:2], (True, None))

    def test_the_scans_give_the_source_regexes_answers(self):
        import random
        tag, link = re.compile(r'</?[A-Za-z][^>]*>'), re.compile(r'!?\[([^\]]*)\]\([^)]*\)')
        rnd = random.Random(7)
        parts = ['<', '>', '[', ']', '(', ')', '!', '/', 'a', 'B', ' ', '"', '*', '`', '|', '_', 'lens', ':', '](', '<a', '</b>', '![']
        for _ in range(100000):
            s = ''.join(rnd.choice(parts) for _ in range(rnd.randint(0, 16)))
            want = re.sub(r'[*`]', '', link.sub(r'\1', tag.sub('', s))).replace('|', ' ')
            want = re.sub(r'(?<![^\W_])_+|_+(?![^\W_])', '', want)
            self.assertEqual(lens.clean_inline(s), want, repr(s))


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

    def test_advice_whose_narrow_reading_holds_a_lens_word_is_cleaned(self):   # issue #303: the reader reads both ways
        text = '<lens= x 0123abcd99 <q></b>'   # one tag read the wide way; `<lens= x 0123abcd99` read the narrow way
        self.assertEqual(lens.clean_inline(text), '')
        out = lens.no_lens_text(text, keep_markdown=True)
        self.assertNotEqual(out, text)
        self.assertIsNone(SOURCE.lens_problem(post(f'pr_queue turned auto-merge OFF: {out}')))
        self.assertIsNotNone(SOURCE.lens_problem(post(f'pr_queue turned auto-merge OFF: {text}')))


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

    def test_an_author_whose_type_is_bot_never_counts(self):   # GraphQL drops the `[bot]` suffix from the login
        r = lens.Review((), 'tool ready --head', 'OWNER', ('OWNER',))
        body = f'lens: correctness · reviewer: x · read at: {SHA_A}'
        for author in ({'login': 'helper', 'is_bot': True}, {'login': 'helper', '__typename': 'Bot'}):
            p = pr(post(body, author=author))
            self.assertEqual(r.reviewed_head(p), (False, None, 'no lens comments'), author)
        for author in ({'login': 'helper', 'is_bot': False}, {'login': 'helper', '__typename': 'User'}):
            self.assertEqual(r.reviewed_head(pr(post(body, author=author))), (True, SHA_A, 'every lens read it'))

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
        self.assertIsNotNone(self.R.looks_problem(post('lgtm, safe to merge')), 'the same text from the owner looks')
        self.assertIsNotNone(self.R.lens_problem(post(f'Hi\nlens: other · read at: {SHA_B}')), 'this one blocks')

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
        for junk in (f'Review\nlens: x · read at: {SHA_B}', f'lens: correctness · read at {SHA_B[:7]}'):   # hidden too
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

    def test_a_trusted_authors_long_blank_or_marker_runs_cost_no_time(self):   # r4 sec: 30-60 s each before
        import time
        shapes = ('lens: a' + ' ' * 65536 + 'b',                                   # FIELD_RE: one blank run per start
                  f'lens: a · read at: {SHA_A}\nread at' + ' ' * 65536 + 'x',      # READ_AT_RE: \\s*:?\\s* split a run
                  f'lens: a · read at: {SHA_A}\nread at' + '\u00a0' * 65536 + 'x',
                  'lens: a' + ' r1' * 21000, 'lens: a' + ' lens' * 13000)           # lens_key: one pass per marker
        for body in shapes:
            start = time.monotonic()
            self.head(post(body))
            self.assertLess(time.monotonic() - start, 5, body[:20])

    def test_a_trusted_authors_runs_of_leading_markers_cost_no_time(self):   # issue #72: 0.3-0.7 s each before
        import time
        shapes = ('> ' * 32768, '>' * 65536, '- ' * 32768, '+ ' * 32768, '1. ' * 21845, '[x] ' * 16384, '# ' * 32768,
                  '~~~ ' * 16384, '> - 1. # [x] ' * 5000 + f'lens: a · read at: {SHA_A}')
        for body in shapes:   # end to end: generous, the rest is unchanged code and a busy host is slow
            start = time.monotonic()
            self.head(post(body))
            self.assertLess(time.monotonic() - start, 5, body[:20])
        start = time.monotonic()   # norm_line alone: 5-13 ms per shape; the old loop took 0.3-0.7 s each
        for body in shapes:
            lens.norm_line(body)
        self.assertLess(time.monotonic() - start, 0.5)

    def test_one_pass_strips_the_markers_the_old_loop_stripped(self):   # issue #72: same answers, one pass
        def loop(line):   # the source's norm_line: one leading marker per pass
            s, prev = lens.clean_inline(line), None
            while s != prev:
                prev = s
                s = re.sub(r'^\s*(?:>|[-+•](?=\s|$)|\d{1,9}[.)](?=\s|$)|#{1,6}(?=\s|$)|~{3,}|\[[ xX]\](?=\s|$))\s*', '', s)
            return s.strip()
        import random
        rnd = random.Random(72)
        parts = ('>', '> ', '-', '- ', '+ ', '• ', '1.', '1. ', '12) ', '1234567890. ', '#', '# ', '####### ', '~~', '~~~',
                 '~~~~ ', '[x] ', '[ ]', '[X]x', ' ', '\t', 'lens: a', 'read at ', 'x', '-x', '#x', '*', '`')
        lines = [''.join(rnd.choice(parts) for _ in range(rnd.randint(0, 12))) for _ in range(3000)]
        lines += ['~' * 65536 + 'x', '> ' * 1000 + '1.x', '#' * 7 + ' lens: a']
        self.assertEqual([lens.norm_line(x) for x in lines], [loop(x) for x in lines])

    def test_many_capped_stranger_posts_cost_no_time(self):   # r4 sec: 0.76 s per 4096-char post before
        import time
        body = 'lens: a' + ' ' * (lens.STRANGER_SCAN - 9) + 'b'
        posts = [stranger(body, url=f'https://github.test/pr/7#c{i}') for i in range(20)]
        start = time.monotonic()
        self.assertEqual(self.head(self.OWN, *posts), (True, SHA_A, 'every lens read it'))
        self.assertLess(time.monotonic() - start, 5)

    def test_a_malformed_lens_comment_blocks_until_deleted_edited_or_overridden(self):
        # r3: the advice "re-post it" could not work: the malformed comment is its own entry, keyed by the comment
        fence = '`' * 3
        bad = post(f'{fence}\nlens: correctness · reviewer: x · read at: {SHA_A}\n{fence}', url='https://github.test/c1')
        good = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-02T00:00:00Z')
        self.assertEqual(self.head(bad, good)[:2], (True, None), 'a correct re-post leaves the old one in force')
        self.assertEqual(self.head(dict(bad, isMinimized=True), good)[:2], (True, None), 'hiding is not enough')
        self.assertEqual(self.head(good), (True, SHA_A, 'every lens read it'), 'deleted')
        edited = dict(bad, body='not a lens: an old draft, see the next comment', includesCreatedEdit=True)
        self.assertEqual(self.head(edited, good), (True, SHA_A, 'every lens read it'), 'its first line edited')
        kept = dict(bad, body=f'not a lens: draft\nlens: correctness · reviewer: x · read at: {SHA_A}')
        self.assertEqual(self.head(kept, good)[:2], (True, None), 'the opt-out does not clear a header line left in it')
        over = post(f'tool ready --head {SHA_A}: read it', when='2026-01-03T00:00:00Z')
        self.assertEqual(self.head(bad, good, over), (True, SHA_A, 'every lens read it'), 'a newer override')
        self.assertIn('delete that comment (hiding it is not enough)', lens.NO_HEADER_WAY_OUT)

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


class Reviews(unittest.TestCase):
    """PR reviews take part like comments (lens_posts): ordered by submittedAt, by the same author rules. Their edit and
    hidden flags come only from the caller's own read (lens.REVIEW_FLAGS): gh pr view never fetches them for reviews
    and prints `includesCreatedEdit: false` anyway (issue #56)."""
    R = WhoCounts.R

    def review(self, body, when='2026-01-02T00:00:00Z', **extra):
        """A review as gh pr view prints it: an id, the unfetched default includesCreatedEdit false, no isMinimized."""
        self.n = getattr(self, 'n', 0) + 1
        p = post(body, when=..., **{'id': f'PRR_{self.n}', **extra})
        p['submittedAt'] = when
        return p

    @staticmethod
    def read(*reviews, edited=False, hidden=False, typename='User'):
        """REVIEW_FLAGS as review_flags reads them, the same flags for each review."""
        return {r['id']: {'includesCreatedEdit': edited, 'isMinimized': hidden, 'url': f'https://x/{r["id"]}',
                          'author': {'__typename': typename, 'login': 'owner'}} for r in reviews}

    def head(self, comments=(), reviews=(), flags=None):
        """flags None: the explicit read says every review is unedited and shown; ... leaves REVIEW_FLAGS out."""
        pr = {'headRefOid': SHA_B, 'comments': list(comments), 'reviews': list(reviews)}
        if flags is not ...:
            pr[lens.REVIEW_FLAGS] = self.read(*reviews) if flags is None else flags
        return self.R.reviewed_head(pr)

    def test_a_counted_review_is_a_read(self):
        self.assertEqual(self.head(reviews=[self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}')]),
                         (True, SHA_B, 'every lens read it'))

    def test_the_newest_of_comments_and_reviews_counts_by_submitted_at(self):
        old = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')
        newer = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}', when='2026-01-02T00:00:00Z')
        self.assertEqual(self.head([old], [newer]), (True, SHA_B, 'every lens read it'))
        older = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}', when='2025-12-31T00:00:00Z')
        self.assertEqual(self.head([old], [older]), (True, SHA_A, 'every lens read it'))

    def test_gh_pr_views_unedited_flag_on_a_review_is_never_trusted(self):   # issue #56
        r = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}')
        self.assertIs(r['includesCreatedEdit'], False)   # what gh prints for an edited review too
        other = self.review('lgtm')
        for flags in (..., {}, self.read(other), {r['id']: {'includesCreatedEdit': False}},
                      {r['id']: {'includesCreatedEdit': 'false', 'isMinimized': False}}, {r['id']: None}, [r['id']]):
            with self.subTest(flags=flags):
                reviewed, sha, why = self.head(reviews=[r], flags=flags)
                self.assertEqual((reviewed, sha), (True, None))
                self.assertIn(lens.FLAGS_UNREAD, why)
        no_id = {k: v for k, v in r.items() if k != 'id'}
        self.assertEqual(self.head(reviews=[no_id], flags={None: self.read(r)[r['id']]})[:2], (True, None))
        self.assertEqual(self.head(reviews=[r], flags=self.read(r)), (True, SHA_B, 'every lens read it'))

    def test_an_edited_or_hidden_review_reads_nothing_whatever_gh_printed(self):
        r = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}')
        for kw, text in (({'edited': True}, lens.EDITED), ({'hidden': True}, lens.HIDDEN)):
            reviewed, sha, why = self.head(reviews=[r], flags=self.read(r, **kw))
            self.assertEqual((reviewed, sha), (True, None), kw)
            self.assertIn(text, why)

    def test_a_review_override_counts_only_with_its_flags_read_unedited(self):
        lens_read = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')
        ov = self.review(f'proj ready --head {SHA_B}: read the rest', authorAssociation='OWNER')
        r = lens.Review((), 'proj ready --head', 'OWNER', ('OWNER',))
        pr = {'headRefOid': SHA_B, 'comments': [lens_read], 'reviews': [ov]}
        self.assertEqual(r.reviewed_head(pr), (True, SHA_A, 'every lens read it'))
        self.assertEqual(r.reviewed_head({**pr, lens.REVIEW_FLAGS: self.read(ov, edited=True)}),
                         (True, SHA_A, 'every lens read it'))
        self.assertEqual(r.reviewed_head({**pr, lens.REVIEW_FLAGS: self.read(ov)}), (True, SHA_B, 'every lens read it'))

    def test_a_review_by_a_bot_account_never_counts(self):   # GitHub's author type, whatever the login
        own = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')
        bot = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}', author={'login': 'helper'})
        self.assertEqual(self.head([own], [bot], flags=self.read(bot, typename='Bot')),
                         (True, SHA_A, 'every lens read it'))
        self.assertEqual(self.head([own], [bot], flags=self.read(bot)), (True, SHA_B, 'every lens read it'))

    def test_a_strangers_review_is_ignored(self):
        own = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')
        forged = self.review(f'lens: correctness · reviewer: x · read at: {SHA_B}', authorAssociation='NONE')
        self.assertEqual(self.head([own], [forged]), (True, SHA_A, 'every lens read it'))
        self.assertEqual(self.head(reviews=[forged]), (False, None, 'no lens comments'))

    def test_a_counted_authors_look_alike_review_blocks(self):
        own = post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z')
        reviewed, sha, why = self.head([own], [self.review(f'Review\nlens: design · read at: {SHA_A}')])
        self.assertEqual((reviewed, sha), (True, None))
        self.assertIn(lens.NO_HEADER, why)


class InlineComments(unittest.TestCase):
    """Inline review comments (on a line of the diff) take part like comments (lens_posts). gh pr view lists none of
    them: the caller reads them and passes them as lens.REVIEW_COMMENTS, each with its own flags and author type."""
    R = WhoCounts.R
    READ_A, READ_B = (f'lens: correctness · reviewer: x · read at: {sha}' for sha in (SHA_A, SHA_B))

    @staticmethod
    def inline(body, when='2026-01-02T00:00:00Z', typename='User', **extra):
        return post(body, when=when, **{'author': {'__typename': typename, 'login': 'someone'}, 'isMinimized': False,
                                        **extra})

    def head(self, comments=(), inline=..., r=None):
        pr = {'headRefOid': SHA_B, 'comments': list(comments), 'reviews': [], lens.REVIEW_FLAGS: {}}
        if inline is not ...:
            pr[lens.REVIEW_COMMENTS] = inline
        return (r or self.R).reviewed_head(pr)

    def test_a_counted_inline_comment_is_a_read(self):
        self.assertEqual(self.head(inline=[self.inline(self.READ_B)]), (True, SHA_B, 'every lens read it'))

    def test_none_read_is_the_same_as_none_there(self):   # old state: a caller that reads no inline comments
        own = post(self.READ_A)
        for inline in (..., None, []):
            self.assertEqual(self.head([own], inline), (True, SHA_A, 'every lens read it'))
            self.assertEqual(self.head(inline=inline), (False, None, 'no lens comments'))

    def test_the_newest_of_comments_and_inline_comments_counts(self):
        own = post(self.READ_A, when='2026-01-02T00:00:00Z')
        self.assertEqual(self.head([own], [self.inline(self.READ_B, when='2026-01-03T00:00:00Z')])[1], SHA_B)
        self.assertEqual(self.head([own], [self.inline(self.READ_B, when='2026-01-01T00:00:00Z')])[1], SHA_A)

    def test_a_counted_authors_inline_look_alike_blocks(self):
        own = post(self.READ_B)
        for body in (f'Review\nlens: design · read at: {SHA_B}', f'lens: design · read at: {SHA_B[:7]}'):
            reviewed, sha, why = self.head([own], [self.inline(body)])
            self.assertEqual((reviewed, sha), (True, None), body)

    def test_an_edited_or_hidden_inline_lens_reads_nothing(self):
        for change, text in (({'includesCreatedEdit': True}, lens.EDITED), ({'isMinimized': True}, lens.HIDDEN),
                             ({'includesCreatedEdit': ...}, lens.EDITED)):   # not said: never "unedited"
            reviewed, sha, why = self.head(inline=[self.inline(self.READ_B, **change)])
            self.assertEqual((reviewed, sha), (True, None), change)
            self.assertIn(text, why)

    def test_a_strangers_or_a_bots_inline_comment_is_ignored(self):
        own = post(self.READ_A)
        for forged in (self.inline(self.READ_B, authorAssociation='NONE'), self.inline(self.READ_B, typename='Bot'),
                       self.inline('safe to merge', authorAssociation='CONTRIBUTOR'),
                       self.inline('safe to merge', typename='Bot')):
            self.assertEqual(self.head([own], [forged]), (True, SHA_A, 'every lens read it'))
            self.assertEqual(self.head(inline=[forged]), (False, None, 'no lens comments'))

    def test_an_inline_override_vouches_like_any_other(self):
        r = lens.Review((), 'proj ready --head', 'OWNER', ('OWNER',))
        own = post(self.READ_A)
        ov = self.inline(f'proj ready --head {SHA_B}: read the rest')
        self.assertEqual(self.head([own], [ov], r), (True, SHA_B, 'every lens read it'))
        for change in ({'includesCreatedEdit': True}, {'isMinimized': True}, {'authorAssociation': 'MEMBER'},
                       {'when': '2025-12-31T00:00:00Z'}):
            self.assertEqual(self.head([own], [self.inline(ov['body'], **change)], r)[1], SHA_A, change)


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

    def test_delete_branch_is_optional_and_never_turns_deletion_on(self):   # old configs stay valid (#stacked PRs)
        self.assertFalse(self.parse(delete_branch=...).delete_branch)
        self.assertTrue(self.parse(delete_branch=True).delete_branch, 'read, so a project can see it is ignored')

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


if __name__ == '__main__':
    unittest.main()
