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
import review_glob  # noqa: E402  (the gate's too)
review_judge = review_kit.review_judge

SHA = '0123456789abcdef0123456789abcdef01234567'
OTHER = '89abcdef0123456789abcdef0123456789abcdef'
CLEAN = f'lens: code · reviewer: a · read at: {SHA}\nstate: a second run does nothing\nblocker: none\nmajor: none\n'
WORDS = ('lens', 'round', 'read', 'short sha', 'clean', 'blocker', 'major', 'no findings section', 'state',
         'no state', 'reads nothing', 'not a lens', 'no lens marker', 'override')


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

    def test_the_state_rule_is_the_gates(self):
        """What counts as a state line is gate code (review_judge.has_state_line), not a second copy in the kit, so
        the kit and the gate read one rule when the gate starts enforcing `state_line` (#797)."""
        self.assertIs(review_kit.has_state_line, review_judge.has_state_line)
        for line, answered in (('state: fine', True), ('**State:** fine', True), ('- state: fine', True),
                               ('state:', False), ('state:   ', False), ('the state is fine', False)):
            with self.subTest(line=line):
                self.assertIs(review_judge.has_state_line(f'lens: code\n{line}\n'), answered)

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
        self.assertEqual(self.verdict(f'Here it is.\nread at: {SHA}'), 'reads nothing')

    def test_a_look_alike_without_the_lens_marker_is_no_lens(self):   # issue #865: named, never blocking
        for body in ('safe to merge, I think', '## Lens A\nblocker: none\nmajor: none', 'covered: a\nchecks: b'):
            with self.subTest(body=body):
                self.assertEqual(self.verdict(body), 'not a lens, no lens marker')
                self.assertEqual(self.m.check(body, 'tool ready --head', SHA), (1, 'not a lens, no lens marker'))

    def test_a_look_alike_without_the_lens_marker_with_an_open_finding_blocks(self):   # issue #865 round 1
        for body in ('## Lens A\nblocker: a re-run loses data\nmajor: none', 'covered: a\nchecks: b\nmajor: a dropped row',
                     'NOT SAFE to merge (lens A)\nmore below'):
            with self.subTest(body=body):
                self.assertEqual(self.verdict(body), 'reads nothing, no lens marker')
                self.assertEqual(self.m.check(body, 'tool ready --head', SHA), (1, 'reads nothing, no lens marker'))


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
    'a short sha is no read': ("if not lens.FULL_SHA.fullmatch(sha or ''):", 'if not sha:',
                               (Doc, 'test_every_example_reads_as_it_says')),
    'the gate flags a looking comment': ('if kind == lens.BROKEN:', 'if False:',
                                         (Check, 'test_a_looking_comment_reads_nothing')),
    'a look-alike without the marker is named': ('if kind == lens.NOT_LENS:', 'if False:',
                                                 (Check, 'test_a_look_alike_without_the_lens_marker_is_no_lens')),
    'a look-alike with an open finding blocks': (
        'if kind == lens.HELD:', 'if False:',
        (Check, 'test_a_look_alike_without_the_lens_marker_with_an_open_finding_blocks')),
    'every example is compared': ('if got != said:', 'if False:', (Doc, 'test_a_broken_example_fails')),
    'the head is lowercased': ("sha != head.lower()", 'sha != head',
                               (Check, 'test_the_head_is_matched_whatever_its_case')),
}

# rule: (text in the gate's review_judge.py, the text that breaks it, the kit test that must then fail): the kit
# reads these rules from the gate, so a broken gate rule must fail the kit's own tests too.
JUDGE = os.path.join(REPO, 'fork-ci', 'actions', 'pr-queue', 'review_judge.py')
GATE_MUTATIONS = {
    'a state line needs an answer': (r"STATE_RE = re.compile(r'state\s*:\s*\S', re.I)",
                                     r"STATE_RE = re.compile(r'state\s*:', re.I)",
                                     (Check, 'test_an_empty_state_line_is_no_state')),
    'a state line outside code and quotes': ('for raw in lens.plain_lines(body))', 'for raw in body.splitlines())',
                                             (Check, 'test_a_state_line_in_a_code_block_or_quote_is_no_state')),
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


PROTECTED_PROBES = ('.github/actions/x/action.yml', '.github/CODEOWNERS', '.github/dependabot.yml',
                    '.github/pr-queue.example.json', '.github/scripts/x.cjs', '.github/workflows', '.github/workflow/x.yml',
                    'github/workflows/x.yml', 'x/.github/workflows/ci.yml', '.gitattributes', '.gitignore', 'package.json',
                    'Dockerfile', 'Makefile', 'scripts/release.sh', 'docs/review.md', 'fork-ci/actions/pr-queue/lens.py',
                    'fork-kit/review/SKILL.md', 'fork-kit/review/eval/lens.md', 'review/x.md', 'lenses/new.md')


def repo_paths():
    """Every file git tracks in this repository (none when git cannot say, and the probes still run)."""
    try:
        out = subprocess.run(['git', '-C', REPO, 'ls-files', '-z'], capture_output=True, check=True, timeout=60).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [p for p in out.decode('utf-8', 'replace').split('\0') if p]


def lens_pr(*posts):
    """A pull request whose comments are `posts` ((body), oldest first) as the gate reads them: written by the owner,
    never edited, not hidden."""
    return {'comments': [{'body': body, 'createdAt': f'2026-01-01T00:00:{i:02d}Z', 'authorAssociation': 'OWNER',
                          'includesCreatedEdit': False, 'author': {'login': 'someone'}}
                         for i, body in enumerate(posts)]}


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
        # The gate's answer, not its source: on every file of this repository and on probe paths next to the listed
        # ones, is_protected says exactly what the skill's list says. A new rule of any form (a constant, an inline
        # prefix, a suffix) that makes one more path protected fails here until the skill names it.
        exact = review_judge.protected_paths(rv, cfg)
        skill_says = lambda p: p in exact or review_glob.matches(review_judge.WORKFLOWS, p)  # noqa: E731
        for path in sorted(set(repo_paths()) | set(PROTECTED_PROBES) | exact):
            with self.subTest(probe=path):
                self.assertEqual(review_judge.is_protected(path, exact), skill_says(path),
                                 'is_protected and SKILL.md "Always critical" disagree on this path')
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

    def test_the_skill_says_what_passes_a_fix_head_after_a_quick_check(self):
        """The gate passes a head only when every lens's newest read names it, or a newer override covers them
        (lens.Review.reviewed_head). The history: round 1 `code` at A, round 2 `code r2` at B, then a nit-only fix C
        with a quick check: the quick check alone holds C, the override for C passes it, and the override alone adds
        no round. The skill must say so: the quick check never replaces the override."""
        rv = recommended()
        a, b, c = (f'{n:040x}' for n in (0xa, 0xb, 0xc))
        prefix = 'tool ready --head'
        review = review_kit.reader(prefix)

        def report(name, sha):
            return f'lens: {name} · reviewer: a · read at: {sha}\nstate: nothing is stored\nblocker: none\nmajor: none\n'

        override = f'{prefix} {c}: read the fix diff and its tests'
        approach = rv.rounds.approach_lens
        rounds = [report('code', a), report('code r2', b)]
        cases = (
            ('quick check alone', rounds + [report('quick-check', c)], None),
            ('quick check and the approach lens', rounds + [report('quick-check', c), report(approach, c)], None),
            ('quick check, approach lens, override', rounds + [report('quick-check', c), report(approach, c), override],
             c),
            ('override alone', rounds + [override], c),
        )
        for name, posts, passes in cases:
            with self.subTest(history=name):
                reviewed, sha, why = review.reviewed_head(lens_pr(*posts))
                self.assertTrue(reviewed)
                self.assertEqual(sha, passes, why)
        with_quick = review_judge.judge_rounds(rv, [('code', a), ('code r2', b), ('quick-check', c)], {})
        self.assertEqual((with_quick.round, with_quick.approach), (3, rv.rounds.approach_at <= 3))
        alone = review_judge.judge_rounds(rv, [('code', a), ('code r2', b)], {})
        self.assertEqual((alone.round, alone.approach), (2, rv.rounds.approach_at <= 2))
        skill = ' '.join(kit_text('SKILL.md').split())
        for words in ('the gate passes a head only when every lens\'s newest read names that head, or an override',
                      'the fix head passes only when every lens reads it again (a new round) or the orchestrator posts '
                      'the override for it (no new round)',
                      '**it does not replace the override**',
                      'On `nit-only: yes` the orchestrator still posts the override for the fix head',
                      'The override alone, with no quick check, adds no round'):
            with self.subTest(says=words):
                self.assertIn(words, skill)
        self.assertNotIn('skip the quick check', skill)
        comments = ' '.join(kit_text('COMMENTS.md').split())
        self.assertIn('on `yes` it still posts the override for the fix head', comments)

    def test_the_skill_says_what_happens_to_a_fix_past_the_last_round(self):
        """A fix after the last lens round that is not nit-only gets no new round: the orchestrator checks it and
        posts the override, and a blocker or major left over counts as stuck (#914)."""
        skill = ' '.join(kit_text('SKILL.md').split())
        for words in ('A fix after the last lens round (`lens_rounds` used up) that is not nit-only',
                      'gets no new lens round: the orchestrator checks that fix diff itself and posts the override for '
                      'the fix head',
                      'A blocker or major left over counts as stuck'):
            with self.subTest(says=words):
                self.assertIn(words, skill)

    def test_the_docs_say_which_override_counts_as_the_gate_reads_it(self):
        """The gate counts an override only from the configured author association, never edited and not hidden
        (lens.Review.override_sha); the skill and COMMENTS.md say all three (#914)."""
        review = review_kit.reader('tool ready --head')
        post = lens_pr(f'tool ready --head {SHA}: read the fix diff and its tests')['comments'][0]
        self.assertEqual(review.override_sha(post), SHA)
        for name, change in (('edited', {'includesCreatedEdit': True}), ('hidden', {'isMinimized': True}),
                             ('another association', {'authorAssociation': 'CONTRIBUTOR'})):
            with self.subTest(override=name):
                self.assertIsNone(review.override_sha({**post, **change}))
        skill = ' '.join(kit_text('SKILL.md').split())
        self.assertIn('it counts only from the configured author association, the queue config\'s '
                      '`override.author_association`, and only while it is never edited and not hidden', skill)
        comments = ' '.join(kit_text('COMMENTS.md').split())
        self.assertIn('counts only from the configured author association, never edited and not hidden', comments)

    def test_the_docs_say_to_run_the_kit_at_the_pinned_release(self):
        """The kit imports the gate's code from its own checkout, so the docs say to run it at the release the
        project's gate pins (#797)."""
        readme = kit_text('README.md')
        self.assertIn('\n## Which fork version\n', readme)
        self.assertIn('run the kit from a checkout at the pinned release', ' '.join(readme.split()))
        for name in ('SKILL.md', 'REVIEW.md'):
            with self.subTest(doc=name):
                self.assertIn('Run the kit from a fork checkout at the release the project\'s gate pins (README.md, '
                              '"Which fork version")', ' '.join(kit_text(name).split()))

    def test_review_md_says_which_decides_the_runner(self):
        """REVIEW.md says whether its file or T3's review rules setting decides the runner (#797): T3's `lens_start`
        reads the setting for the lenses it starts (only in enforce mode), this file for the lenses started by hand.
        When T3's docs stop saying so, this fails and REVIEW.md must be read again."""
        review = ' '.join(kit_text('REVIEW.md').split())
        for words in ("**This file or T3's review rules setting:**", "While it is on, T3's `lens_start` tool reads it",
                      "T3's setting the runner of every lens `lens_start` starts on that machine",
                      'it starts lenses only for a `review` block in `enforce` mode',
                      'this file every lens an agent starts by hand from the skill and what `plan` prints',
                      'the gate does not read either'):
            with self.subTest(says=words):
                self.assertIn(words, review)
        with open(os.path.join(REPO, 'docs', 'fork', 'README.md'), encoding='utf-8') as f:
            t3 = ' '.join(f.read().split())
        self.assertIn('`reviewRules`', t3)
        self.assertIn('While it is on, the T3 tool `lens_start` (below) reads it', t3)
        self.assertIn('so outside `enforce` it refuses', t3)
        self.assertNotIn('Nothing reads it yet', t3)

    def test_the_skill_says_whichever_is_higher(self):
        """review_round takes the higher of the change count and the round marker, never the sum."""
        self.assertEqual(review_judge.review_round([('code', 'a' * 40), ('code r3', 'a' * 40)]), 3)
        self.assertEqual(review_judge.review_round([('code', 'a' * 40), ('code', 'b' * 40), ('code r2', 'c' * 40)]), 3)
        skill = ' '.join(kit_text('SKILL.md').split())
        self.assertIn('the highest `r2`, `r3` marker in a lens name, whichever is higher', skill)


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

    def test_each_gate_rule_the_kit_reads_has_a_kit_test_that_fails_without_it(self):
        with open(JUDGE, encoding='utf-8') as f:
            source = f.read()
        for rule, (text, broken, (base, test)) in GATE_MUTATIONS.items():
            with self.subTest(rule=rule):
                self.assertEqual(source.count(text), 1, f'the red check for {rule!r} no longer matches the gate')
                judge = types.ModuleType('review_judge_broken')
                judge.__file__ = JUDGE
                exec(compile(source.replace(text, broken), JUDGE, 'exec'), judge.__dict__)
                kit = load(inspect.getsource(review_kit))
                kit.has_state_line = judge.has_state_line
                cls = type('Broken', (base,), {'m': kit})
                result = unittest.TestResult()
                cls(test).run(result)
                self.assertFalse(result.wasSuccessful(), f'{test} still passes with the gate rule {rule!r} broken')


if __name__ == '__main__':
    unittest.main(verbosity=2)
