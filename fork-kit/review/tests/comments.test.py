"""COMMENTS.md against the gate's own parser: every example reads as the line above it says, the examples cover every
verdict word, the doc's links resolve, and `review_kit.py check` and `doc` give the same answers on the command line.
A broken example, or a stated verdict that does not match, fails. RedCheck breaks each rule of the comment check in a
copy of lib/review_kit.py and shows its test then fails.

  python3 -I -B fork-kit/review/tests/comments.test.py
"""
import inspect, json, os, re, subprocess, sys, tempfile, shutil, types, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
LIB = os.path.join(KIT, 'lib', 'review_kit.py')
DOC = os.path.join(KIT, 'COMMENTS.md')
sys.path.insert(0, os.path.join(KIT, 'lib'))
import review_kit  # noqa: E402
import review_config  # noqa: E402  (the gate's, on the path review_kit set)
review_judge = review_kit.review_judge

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


TEMPLATE_DOCS = ('SKILL.md', os.path.join('eval', 'lens.md'), 'COMMENTS.md')
FENCE_RE = re.compile(r'^(`{3,})[a-z]*\s*$')
PLACEHOLDER_RE = re.compile(r'<([^<>\n]*)>')


def kit_text(name):
    with open(os.path.join(KIT, name), encoding='utf-8') as f:
        return f.read()


def recommended():
    """review.recommended.json through the gate's own parser."""
    with open(os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue', 'review.recommended.json'), encoding='utf-8') as f:
        return review_config.parse_review(json.load(f))


def templates(text):
    """Every fenced block whose first line is a lens header with a `<placeholder>`: the report shapes the docs tell a
    lens to write (the checked examples of COMMENTS.md hold real shas, so they are not templates)."""
    lines, out, i = text.splitlines(), [], 0
    while i < len(lines):
        m = FENCE_RE.match(lines[i])
        if not m:
            i += 1
            continue
        body, i = [], i + 1
        while i < len(lines) and lines[i].strip() != m.group(1):
            body.append(lines[i])
            i += 1
        i += 1
        if body and re.match(r'lens\s*:', body[0], re.I) and PLACEHOLDER_RE.search(body[0]):
            out.append('\n'.join(body))
    return out


def fill(template):
    """A template as a lens would fill it on a clean read: a sha placeholder takes a full sha, a choice
    (`<blocker|major|minor>`) its last, lightest option, anything else plain text."""
    def one(m):
        inner = m.group(1)
        if 'sha' in inner:
            return SHA
        if '|' in inner:
            return inner.split('|')[-1].strip()
        return 'text'
    return PLACEHOLDER_RE.sub(one, template)


class SkillFormat(unittest.TestCase):
    """The report shape the skill, the eval lens and COMMENTS.md tell a lens to write is a clean read with a state line
    to the gate's own parser, and the skill's tiers and state question are the gate's and the kit's: the skill text and
    the gate cannot drift apart again."""

    def test_every_documented_report_shape_reads_clean_with_the_gates_parser(self):
        for name in TEMPLATE_DOCS:
            found = templates(kit_text(name))
            with self.subTest(doc=name):
                self.assertTrue(found, f'{name} no longer shows the report shape')
            for t in found:
                with self.subTest(doc=name, header=t.splitlines()[0]):
                    words = review_kit.verdict(fill(t), 'tool ready --head', SHA)
                    self.assertEqual(words[1:], ['read', 'clean', 'state'])
                    self.assertEqual(review_kit.check(fill(t), 'tool ready --head', SHA)[0], 0)

    def test_a_shape_without_its_state_or_findings_lines_fails(self):
        t = templates(kit_text('SKILL.md'))[0]
        no_state = '\n'.join(x for x in t.splitlines() if not x.startswith('state:'))
        self.assertIn('no state', review_kit.verdict(fill(no_state), 'tool ready --head', SHA))
        no_blocker = '\n'.join(x for x in t.splitlines() if not x.startswith('blocker:'))
        self.assertIn('no findings section', review_kit.verdict(fill(no_blocker), 'tool ready --head', SHA))

    def test_the_skill_carries_the_state_question_word_for_word(self):
        for name in ('SKILL.md', os.path.join('eval', 'lens.md'), 'REVIEW.md'):
            with self.subTest(doc=name):
                self.assertIn(review_kit.STATE_QUESTION, ' '.join(kit_text(name).split()))

    def test_the_skill_tiers_are_the_gates_weights_and_recommended_lens_counts(self):
        import json
        import review_config
        gate = os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue')
        with open(os.path.join(gate, 'review.recommended.json'), encoding='utf-8') as f:
            mins = json.load(f)['min_lenses']
        rows = re.findall(r'^\| (\w+) +\|[^|]*\| ([^|]*)\|\s*$', kit_text('SKILL.md'), re.M)
        tiers = {w: cell for w, cell in rows if w in review_config.WEIGHTS}
        self.assertEqual(list(tiers), list(review_config.WEIGHTS))
        for weight, cell in tiers.items():
            with self.subTest(weight=weight):
                self.assertEqual(int(re.search(r'\*\*(\d+)', cell).group(1)), mins[weight])

    def test_the_skill_runner_text_is_the_kits_default(self):
        """The tier table's `Opus 5.5 high` and §5's runner defaults are the kit's DEFAULTS, read, not copied."""
        skill = ' '.join(kit_text('SKILL.md').split())
        for key in ('runner', 'audit_runner'):
            r = review_kit.DEFAULTS[key]
            with self.subTest(setting=key):
                self.assertIn(f"`{key}` (default `{r['model']}`, effort `{r['effort']}`)", skill)
        r = review_kit.DEFAULTS['runner']
        m = re.fullmatch(r'claude-([a-z]+)-(\d+)-(\d+)', r['model'])
        self.assertTrue(m, f"the default runner {r['model']!r} has no display name rule here")
        shown = f"{m.group(1).capitalize()} {m.group(2)}.{m.group(3)} {r['effort']}"
        rows = re.findall(r'^\| (\w+) +\|[^|]*\| ([^|]*)\|\s*$', kit_text('SKILL.md'), re.M)
        with_lenses = [(w, cell) for w, cell in rows if w in review_config.WEIGHTS and 'lens' in cell]
        self.assertTrue(with_lenses)
        for weight, cell in with_lenses:
            with self.subTest(weight=weight):
                self.assertIn(shown, cell)

    def test_the_skill_names_the_gates_protected_paths(self):
        """The skill's always-critical list is the gate's: the config, every lens prompt and the workflows glob, and
        nothing else (a new kind of protected path in the gate fails here until the skill names it)."""
        rv = recommended()
        cfg = '.github/pr-queue.json'
        self.assertEqual(review_judge.protected_paths(rv, cfg), {cfg, *(x.prompt for x in rv.lenses)})
        used = set(re.findall(r'\b[A-Z][A-Z_]{2,}\b', inspect.getsource(review_judge.is_protected)))
        self.assertEqual(used, {'WORKFLOWS'}, 'is_protected reads another list: name it in SKILL.md, "Always critical"')
        skill = ' '.join(kit_text('SKILL.md').split())
        line = re.search(r'\*\*Always critical\*\*.*?\.(?= |$)', skill)   # to the sentence's end
        self.assertTrue(line, 'SKILL.md no longer lists the always-critical paths')
        for words in ('the queue config file', 'every lens prompt file', f'`{review_judge.WORKFLOWS}`'):
            with self.subTest(path=words):
                self.assertIn(words, line.group(0))
        # the gate really judges them critical, even under a light rule (`**/*.md` is light in the recommended rules)
        for path in (cfg, rv.lenses[0].prompt, '.github/workflows/ci.yml'):
            with self.subTest(judged=path):
                self.assertEqual(review_judge.judge(rv, [('M', (path,))], cfg).weight, 'critical')
        light = re.search(r'^\| light +\|([^|]*)\|', kit_text('SKILL.md'), re.M).group(1)
        self.assertNotIn('config', light)

    def test_the_skill_counts_rounds_as_the_gate(self):
        """A quick check is a lens read of a new head: one more gate round, and from `rounds.approach_at` the approach
        lens is required unless a listed person decided. The skill names the gate's own round keys and the recommended
        approach_at as review_config parses it."""
        rv = recommended()
        skill = ' '.join(kit_text('SKILL.md').split())
        for key in review_config.ROUND_KEYS:
            with self.subTest(key=key):
                self.assertIn(f'`rounds.{key}`', skill)
        said = re.search(r'`rounds\.approach_at` of the review block \((\d+) in review\.recommended\.json', skill)
        self.assertTrue(said, 'SKILL.md no longer says the recommended rounds.approach_at')
        self.assertEqual(int(said.group(1)), rv.rounds.approach_at)
        for words in ('the gate counts it as a round', '`override_authors`',
                      "An orchestrator's override never replaces the approach lens"):
            with self.subTest(says=words):
                self.assertIn(words, skill)
        # the gate's own round count: rounds before the quick check, then the quick check on a new head
        n = rv.rounds.approach_at
        history = [('code' if i == 0 else f'code r{i + 1}', f'{i:040x}') for i in range(n - 1)]
        before = review_judge.judge_rounds(rv, history, {})
        self.assertEqual((before.round, before.approach), (n - 1, False))
        history.append(('quick-check', f'{n:040x}'))
        after = review_judge.judge_rounds(rv, history, {})
        self.assertEqual((after.round, after.approach), (n, True))
        self.assertFalse(review_judge.judge_rounds(rv, history, {}, decided=True).approach)


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
