"""`lens-packet`: one Markdown file per lens. A temp git repo stands in for the clone at the pull request's head (with
the fork's queue config, kit settings and lens prompts copied in), and a fake `gh` on PATH answers from a map of
argument lists. No network, no host changes.

  python3 -I -B fork-kit/review/tests/packet.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True   # the CI runner has no -B; keep the kit folder free of __pycache__

KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.dirname(os.path.dirname(KIT))
SCRIPT = os.path.join(KIT, 'lens-packet')
SLUG = 'o/r'
GIT = ['git', '-c', 'user.name=test', '-c', 'user.email=test@example.invalid', '-c', 'commit.gpgsign=false',
       '-c', 'init.defaultBranch=main']
FAKE_GH = r'''#!/usr/bin/env python3
import json, os, sys
key = ' '.join(sys.argv[1:])
with open(os.environ['FAKE_GH_LOG'], 'a') as f:
    f.write(key + '\n')
with open(os.environ['FAKE_GH_MAP']) as f:
    answers = json.load(f)
if key not in answers:
    sys.stderr.write('fake gh: no answer for ' + key + '\n')
    sys.exit(1)
a = answers[key]
sys.stdout.write(a.get('out', ''))
sys.stderr.write(a.get('err', ''))
sys.exit(a.get('code', 0))
'''
PR_KEY = f'api repos/{SLUG}/pulls/7'
ISSUE_KEY = f'issue view 3 -R {SLUG} --json number,title,state,body,comments'
COMMENTS_KEY = f'api --paginate repos/{SLUG}/issues/7/comments?per_page=100'
LENS_SHA = 'a' * 40


def lines(n, fmt='line {}'):
    return ''.join(fmt.format(i) + '\n' for i in range(1, n + 1))


def comment(i, body, user='someone', when=None):
    return {'id': i, 'body': body, 'user': {'login': user}, 'created_at': when or f'2026-11-0{i}T00:00:00Z',
            'html_url': f'https://example.invalid/c/{i}'}


class Packet(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix='lens-packet-')
        self.addCleanup(shutil.rmtree, self.dir)
        self.repo = os.path.join(self.dir, 'repo')
        self.bin = os.path.join(self.dir, 'bin')
        os.makedirs(self.bin)
        with open(os.path.join(self.bin, 'gh'), 'w') as f:
            f.write(FAKE_GH)
        os.chmod(os.path.join(self.bin, 'gh'), 0o755)
        self.map = os.path.join(self.dir, 'gh.json')
        self.log = os.path.join(self.dir, 'gh.log')
        self.env = {**os.environ, 'GIT_CONFIG_NOSYSTEM': '1', 'HOME': self.dir, 'FAKE_GH_MAP': self.map,
                    'FAKE_GH_LOG': self.log, 'PATH': self.bin + os.pathsep + os.environ.get('PATH', '')}
        self.git('init', '-q', self.repo, cwd=self.dir)
        self.git('config', 'maintenance.auto', 'false')   # newer git flakes on its auto clean-up in a temp repo
        self.git('config', 'gc.auto', '0')
        for rel in ('.github/fork-pr-queue.json', '.github/fork-review-kit.json', '.github/fork-lens-code.md',
                    '.github/fork-lens-state.md', '.github/fork-lens-merge-path.md', '.github/fork-lens-approach.md'):
            os.makedirs(os.path.dirname(os.path.join(self.repo, rel)), exist_ok=True)
            shutil.copyfile(os.path.join(REPO, rel), os.path.join(self.repo, rel))
        self.put('src/a.py', lines(200))
        self.put('src/r1.py', lines(40, 'renamed {}'))
        self.put('old.txt', 'gone\n')
        self.put('img.bin', b'\0\1\2')
        self.base = self.commit('base')
        a = lines(200).replace('line 50\n', 'line fifty\n')
        self.put('src/a.py', a)
        self.put('src/new.py', 'x = 1\ny = 2\nz = 3\n')
        os.remove(os.path.join(self.repo, 'old.txt'))
        self.git('mv', 'src/r1.py', 'src/r2.py')
        self.put('img.bin', b'\0\1\3')
        self.head = self.commit('head')
        self.answers = {
            PR_KEY: self.pr(self.head),
            ISSUE_KEY: {'out': json.dumps({
                'number': 3, 'title': 'The issue', 'state': 'OPEN', 'body': 'Issue body text.\n\n## Issue heading',
                'comments': [{'author': {'login': 'u'}, 'body': f'issue comment {i}', 'createdAt': f'2026-10-0{i}'}
                             for i in range(1, 8)]})},
            COMMENTS_KEY: {'out': json.dumps([
                comment(1, f'lens: code · reviewer: r · read at: {LENS_SHA}\nblocker: none\nmajor: x is wrong'),
                comment(2, 'Looks good to me, a plain comment.'),
                comment(3, 'not a lens: fix round 1 pushed'),
                comment(6, f'lens: state · reviewer: r · read at: {LENS_SHA}\nblocker: none\nmajor: state finding'),
            ])},
        }

    def git(self, *args, cwd=None):
        r = subprocess.run(GIT + list(args), cwd=cwd or self.repo, env=self.env, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        return r.stdout.strip()

    def put(self, rel, value):
        path = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'wb' if isinstance(value, bytes) else 'w') as f:
            f.write(value)

    def commit(self, msg):
        self.git('add', '-A')
        self.git('commit', '-q', '-m', msg)
        return self.git('rev-parse', 'HEAD')

    def pr(self, head, base=None, body='Fixes #3. Part of #7.\n\n## Why\n\n```\n# a code comment\n```\n'):
        return {'out': json.dumps({'number': 7, 'title': 'The change (#3)', 'body': body,
                                   'html_url': f'https://github.com/{SLUG}/pull/7', 'head': {'sha': head},
                                   'base': {'sha': base or self.base, 'ref': 'main'}})}

    def packet(self, *extra, lens='code', slug=True):
        with open(self.map, 'w') as f:
            json.dump(self.answers, f)
        out = os.path.join(self.dir, 'packet.md')
        cmd = [sys.executable, '-I', '-B', SCRIPT, '7', '--lens', lens, '--out', out, '--repo', self.repo,
               *(['-R', SLUG] if slug else []), *extra]
        r = subprocess.run(cmd, env=self.env, capture_output=True, text=True, timeout=60)
        text = None
        if os.path.exists(out):
            with open(out, encoding='utf-8') as f:
                text = f.read()
        return r, text

    def test_the_packet_holds_every_part_and_names_its_head_first(self):
        r, text = self.packet()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines()[0], f'head: {self.head}')
        self.assertEqual(text.splitlines()[0], f'head: {self.head}')
        self.assertIn(f'base: {self.base}', text)
        self.assertIn('**The change (#3)**', text)
        self.assertIn('##### Why', text)                  # body headings sit below the packet's sections
        self.assertIn('\n# a code comment\n', text)        # a fenced line is not a heading
        self.assertNotIn('\n## Why', text)
        self.assertIn('### #3 The issue (open)', text)
        self.assertIn('Issue body text.', text)
        self.assertIn('Last 5 of 7 comments:', text)
        self.assertNotIn('issue comment 2', text)
        for i in range(3, 8):
            self.assertIn(f'issue comment {i}', text)
        self.assertIn('- M src/a.py +1 -1', text)
        self.assertIn('- A src/new.py +3 -0', text)
        self.assertIn('- D old.txt +0 -1', text)
        self.assertIn('- R src/r1.py -> src/r2.py +0 -0', text)
        self.assertIn('- M img.bin (binary)', text)
        self.assertIn('+line fifty', text)
        # src/a.py at the head, 30 lines before and after line 50, numbered
        self.assertIn('lines 20-80', text)
        self.assertIn(' 20  line 20\n', text)
        self.assertIn(' 50  line fifty\n', text)
        self.assertIn(' 80  line 80\n', text)
        self.assertNotIn(' 19  line 19\n', text)
        self.assertNotIn(' 81  line 81\n', text)
        self.assertIn('(new file: all of it is in the diff above)', text)
        with open(os.path.join(REPO, '.github/fork-lens-code.md'), encoding='utf-8') as f:
            self.assertIn(f.read().strip(), text)
        self.assertIn('## Lens prompt: .github/fork-lens-code.md', text)
        self.assertIn('major: x is wrong', text)
        self.assertIn('not a lens: fix round 1 pushed', text)
        self.assertNotIn('a plain comment', text)
        self.assertNotIn('state finding', text)       # another lens's report: counted, not shown
        self.assertIn('1 report(s) of other lenses left out', text)
        sections = [line for line in text.splitlines() if line.startswith('## ')]
        self.assertEqual(sections, ['## Pull request', '## Linked issues', '## Changed files (5)', '## Diff',
                                    '## Around each change (the head, 30 lines before and after)',
                                    '## Lens prompt: .github/fork-lens-code.md',
                                    '## Earlier lens comments on this pull request'])

    def test_the_same_head_and_comments_give_the_same_bytes(self):
        r1, first = self.packet()
        r2, second = self.packet()
        self.assertEqual((r1.returncode, r2.returncode), (0, 0))
        self.assertEqual(first, second)

    def test_each_lens_gets_its_own_prompt(self):
        r, text = self.packet(lens='merge-path')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('## Lens prompt: .github/fork-lens-merge-path.md', text)
        self.assertIn('pr: #7 https://github.com/o/r/pull/7 · lens: merge-path', text)

    def test_all_lenses_adds_the_other_lenses_reports(self):
        r, text = self.packet('--all-lenses')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('state finding', text)
        self.assertIn('major: x is wrong', text)
        self.assertNotIn('left out', text)
        r, text = self.packet(lens='state')
        self.assertIn('state finding', text)
        self.assertNotIn('major: x is wrong', text)

    def test_a_long_diff_is_cut_with_a_note(self):
        r, text = self.packet('--max-diff-lines', '5')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('[cut: 5 of', text)
        self.assertIn(f'git diff {self.base[:12]} {self.head[:12]} -- src/a.py]', text)

    def test_close_changes_share_one_range_and_far_ones_get_their_own(self):
        a = lines(200).replace('line 50\n', 'line fifty\n').replace('line 60\n', 'line sixty\n')
        self.put('src/a.py', a.replace('line 180\n', 'line one-eighty\n'))
        self.answers[PR_KEY] = self.pr(self.commit('more'))
        r, text = self.packet()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('lines 20-90', text)
        self.assertIn('lines 150-200', text)

    def test_a_clone_not_at_the_head_writes_no_packet(self):
        self.answers[PR_KEY] = self.pr('b' * 40)
        r, text = self.packet()
        self.assertEqual(r.returncode, 2)
        self.assertIsNone(text)
        self.assertIn(f'is at {self.head}', r.stderr)
        self.assertIn('check out the head', r.stderr)

    def test_an_unknown_lens_names_the_known_ones(self):
        r, text = self.packet(lens='nope')
        self.assertEqual(r.returncode, 2)
        self.assertIsNone(text)
        self.assertIn("no lens 'nope'", r.stderr)
        self.assertIn('approach, code, merge-path, state', r.stderr)

    def test_a_base_the_clone_lacks_writes_no_packet(self):
        self.answers[PR_KEY] = self.pr(self.head, base='c' * 40)
        r, text = self.packet()
        self.assertEqual(r.returncode, 2)
        self.assertIsNone(text)
        self.assertIn('fetch origin main', r.stderr)

    def test_an_unreadable_issue_is_a_listed_gap_and_ends_red(self):
        self.answers[ISSUE_KEY] = {'code': 1, 'err': 'HTTP 404: Not Found'}
        r, text = self.packet()
        self.assertEqual(r.returncode, 1)
        self.assertIn('## Gaps', text)
        self.assertIn('- issue #3: gh could not read it: HTTP 404: Not Found', text)
        self.assertIn('WARN lens-packet: issue #3', r.stderr)
        self.assertIn('## Lens prompt', text)        # the rest is still there

    def test_unreadable_comments_are_a_gap_not_none(self):
        self.answers[COMMENTS_KEY] = {'code': 1, 'err': 'rate limited'}
        r, text = self.packet()
        self.assertEqual(r.returncode, 1)
        self.assertIn('- pull request comments: gh could not read them: rate limited', text)
        self.assertNotIn('(none)\n', text.split('## Earlier lens comments')[1])

    def test_every_page_of_comments_is_read_and_rounds_are_in_time_order(self):
        page1 = [comment(5, f'lens: code r2 · reviewer: r · read at: {LENS_SHA}\nblocker: none\nmajor: none')]
        page2 = [comment(4, f'lens: code · reviewer: r · read at: {LENS_SHA}\nblocker: none\nmajor: round one')]
        self.answers[COMMENTS_KEY] = {'out': json.dumps(page1) + '\n' + json.dumps(page2)}
        r, text = self.packet()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(text.index('major: round one'), text.index('lens: code r2'))

    def test_no_slug_asks_gh_for_the_clones_repository(self):
        self.answers['repo view --json nameWithOwner --jq .nameWithOwner'] = {'out': SLUG + '\n'}
        r, text = self.packet(slug=False)
        self.assertEqual(r.returncode, 0, r.stderr)
        with open(self.log) as f:
            self.assertEqual(f.readline().strip(), 'repo view --json nameWithOwner --jq .nameWithOwner')

    def test_a_pull_request_gh_cannot_read_writes_no_packet(self):
        del self.answers[PR_KEY]
        r, text = self.packet()
        self.assertEqual(r.returncode, 2)
        self.assertIsNone(text)
        self.assertIn('gh could not read pull request #7', r.stderr)


if __name__ == '__main__':
    unittest.main()
