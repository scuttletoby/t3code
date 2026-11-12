"""Tests of lens.py and config.py (run: python3 fork-ci/actions/pr-queue/lens.test.py).

testdata/lens-corpus.jsonl holds, one case per line, every call the source project's lens tests made to its copy of
these rules, with that copy's answer (README.md, "Where the rules come from"): 2021 cases, replayed here against lens.py. Logins and the
project name in it were replaced (owner, proj); the replaced corpus was checked against the source copy before it was
committed, with the source's override prefix set to `proj ready --head`.
"""
import json, os, sys, unittest

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402

CORPUS = os.path.join(HERE, 'testdata', 'lens-corpus.jsonl')
SOURCE = lens.Review(('coderabbitai', 'github-actions', 'copilot'), 'proj ready --head', 'OWNER')
SHA_A, SHA_B = 'a' * 40, 'b' * 40
VALID = {
    'required_checks': ['build', 'test'], 'merge_method': 'squash', 'delete_branch': True,
    'bot_logins': ['review-bot'], 'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
}


def post(body, login='someone', when='2026-01-01T00:00:00Z', **extra):
    return {'body': body, 'author': {'login': login}, 'createdAt': when, **extra}


def pr(*comments, head=SHA_B, commits=(SHA_A, SHA_B)):
    return {'headRefOid': head, 'comments': list(comments), 'reviews': [], 'commits': [{'oid': c} for c in commits]}


class Corpus(unittest.TestCase):
    def test_every_recorded_call_gives_the_source_answer(self):
        with open(CORPUS, encoding='utf-8') as f:
            cases = [json.loads(line) for line in f]
        self.assertEqual(len(cases), 2021)
        fns = {'lens_problem': SOURCE.lens_problem, 'reviewed_head': SOURCE.reviewed_head,
               'override_sha': SOURCE.override_sha, 'lens_header': lens.lens_header, 'no_lens_text': lens.no_lens_text,
               'latest_checks': lens.latest_checks, 'check_state': lens.check_state, 'lens_key': lens.lens_key,
               'full_sha': lambda read, cands: lens.full_sha(read, set(cands))}
        self.assertEqual({c['fn'] for c in cases}, set(fns))
        wrong = []
        for c in cases:
            got = json.loads(json.dumps(fns[c['fn']](*c['args'])))
            if got != c['out']:
                wrong.append(f'{c["fn"]}{json.dumps(c["args"])[:160]}: got {got!r}, source {c["out"]!r}')
        self.assertEqual(wrong, [], f'{len(wrong)} of {len(cases)} differ; first: {wrong[:3]}')


class Gaps(unittest.TestCase):
    """Rules the corpus does not pin (a mutation of each passed it)."""
    def test_a_lens_heading_counts_only_in_the_first_five_plain_lines(self):
        top = ['Summary', 'one', 'two', 'three']
        self.assertIsNotNone(SOURCE.lens_problem(post('\n'.join(top + ['## Lens correctness', 'text']))))
        self.assertIsNone(SOURCE.lens_problem(post('\n'.join(top + ['four', '## Lens correctness', 'text']))))

    def test_a_hex_word_without_a_digit_is_no_sha(self):
        self.assertIsNone(SOURCE.lens_problem(post('Notes\n```\nlens: effaced\n```')))
        self.assertIn(lens.EMBEDDED, SOURCE.lens_problem(post('Notes\n```\nlens: effaced1\n```')))


class ProjectRules(unittest.TestCase):
    """What the config changes: the override comment and the bot logins."""
    def test_the_configured_override_prefix_counts_and_another_does_not(self):
        r = lens.Review((), 'tool ready --head', 'OWNER')
        mine = post(f'tool ready --head {SHA_B}: read the merge', authorAssociation='OWNER', includesCreatedEdit=False)
        other = dict(mine, body=f'proj ready --head {SHA_B}: read the merge')
        self.assertEqual(r.override_sha(mine), SHA_B)
        self.assertIsNone(r.override_sha(other))

    def test_an_override_counts_only_from_the_configured_association_and_unedited(self):
        r = lens.Review((), 'tool ready --head', 'MEMBER')
        body = f'tool ready --head {SHA_B}: read it'
        self.assertEqual(r.override_sha(post(body, authorAssociation='MEMBER', includesCreatedEdit=False)), SHA_B)
        for bad in ({'authorAssociation': 'OWNER', 'includesCreatedEdit': False},    # not the configured one
                    {'authorAssociation': 'MEMBER', 'includesCreatedEdit': True},    # edited after posting
                    {'authorAssociation': 'MEMBER'}):                                 # GitHub did not say
            self.assertIsNone(r.override_sha(post(body, **bad)), bad)

    def test_an_override_covers_older_lens_reads(self):
        r = lens.Review((), 'tool ready --head', 'OWNER')
        p = pr(post(f'lens: correctness · reviewer: x · read at: {SHA_A}', when='2026-01-01T00:00:00Z'),
               post(f'tool ready --head {SHA_B}: read the rest', when='2026-01-02T00:00:00Z',
                    authorAssociation='OWNER', includesCreatedEdit=False))
        self.assertEqual(r.reviewed_head(p), (True, SHA_B, 'every lens read it'))

    def test_a_configured_bot_and_any_login_ending_in_bot_never_count(self):
        r = lens.Review(('Review-Bot',), 'tool ready --head', 'OWNER')
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
        self.assertEqual((c.head_check, c.wait_seconds, c.poll_seconds), (None, 60, 5))
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
        self.assertEqual(self.parse(head_check='test').head_check, 'test')
        self.refused("head_check 'lint' is not one of required_checks", head_check='lint')

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
