"""COMMENTS.md against the gate's own parser: every example reads as the line above it says, the examples cover every
verdict word, the doc's links resolve, and `review_kit.py check` and `doc` give the same answers on the command line.
A broken example, or a stated verdict that does not match, fails. RedCheck breaks each rule of the comment check in a
copy of lib/review_kit.py and shows its test then fails.

  python3 -I -B fork-kit/review/tests/comments.test.py
"""
import os, re, subprocess, sys, tempfile, shutil, types, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
LIB = os.path.join(KIT, 'lib', 'review_kit.py')
DOC = os.path.join(KIT, 'COMMENTS.md')
sys.path.insert(0, os.path.join(KIT, 'lib'))
import review_kit  # noqa: E402

SHA = '0123456789abcdef0123456789abcdef01234567'
OTHER = '89abcdef0123456789abcdef0123456789abcdef'
CLEAN = f'lens: code · reviewer: a · read at: {SHA}\nstate: a second run does nothing\nblocker: none\nmajor: none\n'
WORDS = ('lens', 'round', 'read', 'short sha', 'clean', 'blocker', 'major', 'no findings section', 'state',
         'no state', 'reads nothing', 'not a lens', 'override')


def doc():
    with open(DOC, encoding='utf-8') as f:
        return f.read()


def load(source=None):
    if source is None:
        return review_kit
    mod = types.ModuleType('review_kit_broken')
    mod.__file__ = LIB
    exec(compile(source, LIB, 'exec'), mod.__dict__)
    return mod


def cli(*args, stdin=None, cwd=None):
    r = subprocess.run([sys.executable, '-I', '-B', LIB, *args], capture_output=True, text=True, input=stdin,
                       cwd=cwd, timeout=60)
    return r.returncode, r.stdout, r.stderr


class Doc(unittest.TestCase):
    m = review_kit

    def test_the_parser_is_the_gates(self):
        gate = os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue')
        for mod in (review_kit.lens, review_kit.review_judge, review_kit.gate_config):
            self.assertEqual(os.path.dirname(os.path.abspath(mod.__file__)), gate)

    def test_every_example_reads_as_it_says(self):
        prefix, examples = self.m.doc_examples(doc())
        self.assertEqual(prefix, 'tool ready --head')
        self.assertGreaterEqual(len(examples), 15)
        self.assertEqual(self.m.doc_problems(doc()), [])

    def test_the_examples_cover_every_word(self):
        said = ' | '.join(s for _, s, _ in self.m.doc_examples(doc())[1])
        for w in WORDS:
            with self.subTest(word=w):
                self.assertRegex(said, rf'(^|, | \| ){re.escape(w)}\b')

    def test_a_broken_example_fails(self):
        text = doc()   # the first clean example gets a major: its stated verdict no longer holds
        broken = text.replace('major: none\nminor: the error text', 'major: the queue file is cut\nminor: the error text')
        self.assertNotEqual(broken, text)
        problems = self.m.doc_problems(broken)
        self.assertEqual(len(problems), 1)
        self.assertIn('the gate reads `lens code, read, major, state`', problems[0])

    def test_a_wrong_stated_verdict_fails(self):
        text = doc().replace('The gate reads: `lens code, short sha`', 'The gate reads: `lens code, read, clean, state`')
        self.assertEqual(len(self.m.doc_problems(text)), 1)

    def test_a_broken_doc_shape_is_a_plain_problem(self):
        cases = {
            'no prefix line': doc().replace('The examples use the override prefix', 'The prefix is'),
            'an example with no stated verdict': doc() + '\n```text\nlens: x\n```\n',
            'a stated verdict with no example': doc() + '\nThe gate reads: `not a lens`\n',
            'an unclosed example': doc() + '\nThe gate reads: `not a lens`\n\n```text\nhello\n',
            'no examples': 'The examples use the override prefix `x`.\n',
        }
        for name, text in cases.items():
            with self.subTest(name):
                with self.assertRaises(self.m.Problem):
                    self.m.doc_problems(text)

    def test_links_resolve(self):
        for name in ('COMMENTS.md', 'REVIEW.md', 'SKILL.md', 'README.md', os.path.join('eval', 'README.md')):
            path = os.path.join(KIT, name)
            with open(path, encoding='utf-8') as f:
                text = f.read()
            for target in re.findall(r'\]\(([^)#\s]+)(?:#[^)]*)?\)', text):
                if '://' in target:
                    continue
                with self.subTest(doc=name, link=target):
                    self.assertTrue(os.path.exists(os.path.join(os.path.dirname(path), target)))

    def test_the_doc_cites_the_gate_vectors(self):
        self.assertIn('review.vectors.json', doc())
        self.assertTrue(os.path.isfile(os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue', 'review.vectors.json')))


class Check(unittest.TestCase):
    m = review_kit

    def verdict(self, body, head=None):
        return ', '.join(self.m.verdict(body, 'tool ready --head', head))

    def test_a_clean_read_of_the_head_passes(self):
        self.assertEqual(self.m.check(CLEAN, 'tool ready --head', SHA), (0, 'lens code, read, clean, state'))

    def test_another_head_fails(self):
        code, text = self.m.check(CLEAN, 'tool ready --head', OTHER)
        self.assertEqual(code, 1)
        self.assertIn('not this head', text)

    def test_the_head_is_matched_whatever_its_case(self):
        self.assertEqual(self.m.check(CLEAN, 'tool ready --head', SHA.upper())[0], 0)

    def test_a_missing_state_line_fails(self):
        body = CLEAN.replace('state: a second run does nothing\n', '')
        self.assertEqual(self.m.check(body, 'tool ready --head', SHA), (1, 'lens code, read, clean, no state'))

    def test_an_empty_state_line_is_no_state(self):
        body = CLEAN.replace('state: a second run does nothing', 'state:')
        self.assertIn('no state', self.verdict(body))

    def test_a_state_line_in_a_code_block_or_quote_is_no_state(self):
        for wrap in ('```\n{}\n```', '> {}', '    {}'):
            body = CLEAN.replace('state: a second run does nothing', wrap.format('state: fine'))
            with self.subTest(wrap=wrap):
                self.assertIn('no state', self.verdict(body))

    def test_a_major_fails(self):
        body = CLEAN.replace('major: none', 'major: x is cut')
        self.assertEqual(self.m.check(body, 'tool ready --head', SHA)[0], 1)

    def test_no_findings_section_fails(self):
        body = CLEAN.replace('blocker: none\n', '')
        self.assertEqual(self.verdict(body), 'lens code, read, no findings section, state')

    def test_round_markers(self):
        self.assertEqual(self.verdict(CLEAN.replace('lens: code', 'lens: code r3')), 'lens code, round 3, read, clean, state')

    def test_a_header_without_sha(self):
        self.assertEqual(self.verdict('lens: code · reviewer: a\nblocker: none\nmajor: none\n'), 'lens code, no sha')

    def test_an_override(self):
        self.assertEqual(self.verdict(f'tool ready --head {SHA}: read the fix'), 'override')
        self.assertEqual(self.verdict(f'tool ready --head {SHA[:7]}: read the fix'), 'not a lens')

    def test_a_looking_comment_reads_nothing(self):
        self.assertEqual(self.verdict('safe to merge, I think'), 'reads nothing')


class Cli(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='review-kit-test-')
        self.addCleanup(shutil.rmtree, self.dir)

    def put(self, name, text):
        path = os.path.join(self.dir, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(text)
        return path

    def test_doc_passes_and_runs_again_the_same(self):
        first = cli('doc')
        self.assertEqual(first[0], 0, first)
        self.assertRegex(first[1], r'^ok: every example reads as it says \(\d+ examples\)\n$')
        self.assertEqual(cli('doc'), first)

    def test_doc_names_a_broken_example(self):
        path = self.put('c.md', doc().replace('The gate reads: `lens code, short sha`',
                                              'The gate reads: `lens code, read`'))
        code, out, err = cli('doc', path)
        self.assertEqual(code, 1)
        self.assertIn('says `lens code, read`, the gate reads `lens code, short sha`', out)

    def test_check_without_a_config_says_so_and_still_reads(self):
        path = self.put('c.md', CLEAN)
        code, out, err = cli('check', '--root', self.dir, '--head', SHA, path)
        self.assertEqual((code, err), (0, ''))
        self.assertIn('no queue config', out)
        self.assertTrue(out.endswith('lens code, read, clean, state\n'))

    def test_check_reads_stdin(self):
        code, out, _ = cli('check', '--root', self.dir, '-', stdin=CLEAN.replace('major: none', 'major: bad'))
        self.assertEqual(code, 1)
        self.assertIn('lens code, read, major, state', out)

    def test_a_missing_or_unreadable_comment_is_a_plain_message(self):
        for args in (('check', '--root', self.dir, os.path.join(self.dir, 'none.md')), ('check', '--root', self.dir),
                     ('check', '--root', self.dir, '--head'), ('check', '--bogus', 'x'), ('doc', '/no/such/doc.md'),
                     ('nonsense',), ()):
            with self.subTest(args=args):
                code, out, err = cli(*args)
                self.assertEqual(code, 2)
                self.assertNotIn('Traceback', out + err)

    def test_not_utf8_is_a_plain_message(self):
        path = os.path.join(self.dir, 'c.md')
        with open(path, 'wb') as f:
            f.write(b'lens: \xff\xfe')
        code, out, err = cli('check', '--root', self.dir, path)
        self.assertEqual(code, 2)
        self.assertIn('not UTF-8', err)
        self.assertNotIn('Traceback', err)


# rule: (text in lib/review_kit.py, the text that breaks it, the test that must then fail)
MUTATIONS = {
    'the head must match': ("ok = GOOD <= set(words) and 'not this head' not in words",
                            'ok = GOOD <= set(words)', (Check, 'test_another_head_fails')),
    'a state line is needed': ("words.append('state' if has_state_line(body) else 'no state')",
                               "words.append('state')", (Check, 'test_a_missing_state_line_fails')),
    'a state line needs an answer': (r"STATE_RE = re.compile(r'state\s*:\s*\S', re.I)",
                                     r"STATE_RE = re.compile(r'state\s*:', re.I)",
                                     (Check, 'test_an_empty_state_line_is_no_state')),
    'a state line outside code and quotes': ('for raw in lens.plain_lines(body))', 'for raw in body.splitlines())',
                                             (Check, 'test_a_state_line_in_a_code_block_or_quote_is_no_state')),
    'a short sha is no read': ("if not lens.FULL_SHA.fullmatch(sha or ''):", 'if not sha:',
                               (Doc, 'test_every_example_reads_as_it_says')),
    'the gate flags a looking comment': ('if review.lens_problem(post):', 'if False:',
                                         (Check, 'test_a_looking_comment_reads_nothing')),
    'every example is compared': ('if got != said:', 'if False:', (Doc, 'test_a_broken_example_fails')),
    'the head is lowercased': ("sha != head.lower()", 'sha != head',
                               (Check, 'test_the_head_is_matched_whatever_its_case')),
}


class RedCheck(unittest.TestCase):
    def test_each_rule_has_a_test_that_fails_without_it(self):
        with open(LIB, encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, (base, test)) in MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the code')
                cls = type('Broken', (base,), {'m': load(source.replace(text, broken))})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
