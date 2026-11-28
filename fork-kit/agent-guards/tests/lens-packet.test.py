#!/usr/bin/python3 -IB
"""Tests of fork-kit/agent-guards/bin/fork-lens-packet (one file with what a review lens needs to start: the pull
request, its SHAs, the diff, the touched lines with context, the tests, the linked issues and the state question).

A fixture repo (a bare `origin` with refs/pull/N/head, and a clone that has only main) and a stand-in `gh` on PATH
that answers `pr view` and `issue view` from JSON files. No network, no root.

  python3 -I -B fork-kit/agent-guards/tests/lens-packet.test.py
"""
import json, os, shutil, subprocess, sys, tempfile, unittest

sys.dont_write_bytecode = True
KIT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CMD = os.path.join(KIT, 'bin', 'fork-lens-packet')

SECTIONS = ('## Pull request', '## Commits', '## Lens questions', '## State question', '## Linked issues',
            '## Tests added or changed', '## Touched lines', '## Diff')

GH = '''#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
d = os.environ['GH_FIXTURES']
if len(args) >= 3 and args[0] in ('pr', 'issue') and args[1] == 'view':
    p = os.path.join(d, f'{args[0]}-{args[2]}.json')
    if os.path.exists(p):
        with open(p) as f:
            print(f.read())
        sys.exit(0)
    print(f'GraphQL: Could not resolve to an issue or pull request with the number of {args[2]}.', file=sys.stderr)
    sys.exit(1)
print('gh stand-in: unexpected call ' + ' '.join(args), file=sys.stderr)
sys.exit(1)
'''

LENS = '''# Lens: state and tests

You review one pull request.

- Ask every time: what happens when this runs again, stops half-way, or meets
  existing, old or empty state?
- New tests should fail on the old code. Check one.

Write the report as one comment.
'''


def numbered(prefix, n):
    return ''.join(f'{prefix} line {i}\n' for i in range(1, n + 1))


class Packet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix='lens-packet-test-')
        t = cls.tmp
        cls.env = dict(os.environ, HOME=t, TMPDIR=os.path.join(t, 'tmp'), GH_FIXTURES=os.path.join(t, 'gh'),
                       GIT_CONFIG_NOSYSTEM='1', GIT_AUTHOR_NAME='t', GIT_AUTHOR_EMAIL='t@example.invalid',
                       GIT_COMMITTER_NAME='t', GIT_COMMITTER_EMAIL='t@example.invalid',
                       GIT_AUTHOR_DATE='2026-01-01T00:00:00Z', GIT_COMMITTER_DATE='2026-01-01T00:00:00Z')
        for d in ('bin', 'tmp', 'gh'):
            os.makedirs(os.path.join(t, d))
        gh = os.path.join(t, 'bin', 'gh')
        with open(gh, 'w') as f:
            f.write(GH)
        os.chmod(gh, 0o755)
        cls.env['PATH'] = os.path.join(t, 'bin') + os.pathsep + os.environ.get('PATH', '')

        src = os.path.join(t, 'src')
        cls.git(None, 'init', '-q', '-b', 'main', src)
        cls.write(src, 'app/store.py', numbered('store', 100))
        cls.write(src, 'app/util.py', numbered('util', 60))
        cls.write(src, 'tests/util.test.py', 'def test_old():\n    pass\n')
        cls.write(src, 'README.md', 'readme\n')
        cls.write(src, '.github/fork-lens-state.md', LENS)
        cls.git(src, 'add', '-A')
        cls.git(src, 'commit', '-q', '-m', 'base')
        cls.git(src, 'checkout', '-q', '-b', 'feature')
        util = numbered('util', 60).replace('util line 30\n', 'util line 30 changed\n')
        cls.write(src, 'app/util.py', util)
        store = numbered('store', 100).replace('store line 80\n',
                                               'store line 80\n    os.replace(tmp, path)  # retry on crash\n')
        cls.write(src, 'app/store.py', store)
        cls.write(src, 'tests/util.test.py', 'def test_old():\n    pass\n\n\ndef test_util_changed():\n    pass\n')
        cls.write(src, 'tests/store.test.py', 'def test_runs_twice():\n    pass\n')
        cls.git(src, 'add', '-A')
        cls.git(src, 'commit', '-q', '-m', 'feature')
        cls.head = cls.git(src, 'rev-parse', 'HEAD').strip()
        cls.git(src, 'checkout', '-q', 'main')
        cls.merge_base = cls.git(src, 'rev-parse', 'HEAD').strip()
        cls.write(src, 'README.md', 'readme changed on main after the branch\n')
        cls.git(src, 'commit', '-q', '-am', 'main moves on')
        cls.base = cls.git(src, 'rev-parse', 'HEAD').strip()

        origin = os.path.join(t, 'origin.git')
        cls.git(None, 'clone', '-q', '--bare', src, origin)
        cls.git(origin, 'update-ref', 'refs/pull/7/head', cls.head)
        cls.git(origin, 'update-ref', 'refs/pull/8/head', cls.head)
        cls.git(origin, 'branch', '-q', '-D', 'feature')
        cls.work = os.path.join(t, 'work')
        cls.git(None, 'clone', '-q', origin, cls.work)

        def pr(n, body):
            return {'number': n, 'title': f'Pull request {n}', 'body': body,
                    'url': f'https://github.com/example/repo/pull/{n}', 'baseRefName': 'main',
                    'baseRefOid': cls.base, 'headRefName': 'feature', 'headRefOid': cls.head}
        cls.fixture('pr-7', pr(7, 'Adds the store write.\n\n## State question\nnone\n\nFixes #5\nPart of #9\n'))
        cls.fixture('pr-8', pr(8, 'No issue named here.'))
        cls.fixture('issue-5', {'number': 5, 'title': 'The store loses writes', 'url': 'u',
                                'body': 'start of issue ' + 'x' * 5000 + ' END-OF-ISSUE'})

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, True)

    @classmethod
    def git(cls, cwd, *args):
        return subprocess.run(['git', *args], cwd=cwd, env=cls.env, check=True, text=True,
                              stdout=subprocess.PIPE).stdout

    @staticmethod
    def write(root, rel, text):
        p = os.path.join(root, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, 'w') as f:
            f.write(text)

    @classmethod
    def fixture(cls, name, data):
        with open(os.path.join(cls.tmp, 'gh', name + '.json'), 'w') as f:
            json.dump(data, f)

    def run_cmd(self, *args):
        return subprocess.run([sys.executable, '-I', '-B', CMD, *args], cwd=self.work, env=self.env, text=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)

    def packet(self, *args):
        r = self.run_cmd(*args)
        self.assertEqual(r.returncode, 0, r.stderr)
        path = r.stdout.strip()
        self.assertTrue(os.path.isabs(path), r.stdout)
        with open(path) as f:
            return path, f.read()

    def test_every_section_present(self):
        """Does one run write every section, with the SHAs, the diff from the merge base, the touched lines with
        context from the head, the tests, the linked issue and the state flags?"""
        path, text = self.packet('7', '-R', 'example/repo')
        self.assertFalse(os.path.realpath(path).startswith(os.path.realpath(self.work) + os.sep))
        for s in SECTIONS:
            self.assertEqual(text.count('\n' + s + '\n'), 1, s)   # once, also when the body has the same heading
        self.assertEqual([text.index('\n' + s + '\n') for s in SECTIONS],
                         sorted(text.index('\n' + s + '\n') for s in SECTIONS))
        self.assertIn('Pull request 7', text)
        self.assertIn('Adds the store write.', text)
        for sha in (self.base, self.merge_base, self.head):
            self.assertIn(sha, text)
        diff = text[text.index('\n## Diff\n'):]
        self.assertIn('+util line 30 changed', diff)
        self.assertNotIn('readme changed on main', text)   # merge-base..head, not base..head
        touched = text[text.index('\n## Touched lines\n'):text.index('\n## Diff\n')]
        self.assertIn('app/util.py', touched)
        self.assertIn('10-50', touched)                        # line 30 with 20 lines each side
        self.assertIn('util line 10\n', touched)
        self.assertNotIn('util line 9\n', touched)
        self.assertIn('util line 30 changed', touched)
        self.assertIn(f'read at: {self.head}', text)            # the lens report's first line, ready
        tests = text[text.index('\n## Tests added or changed\n'):text.index('\n## Touched lines\n')]
        self.assertIn('tests/store.test.py', tests)
        self.assertIn('added', tests)
        self.assertIn('tests/util.test.py', tests)
        self.assertIn('test_runs_twice', tests)
        self.assertIn('test_util_changed', tests)
        issues = text[text.index('\n## Linked issues\n'):text.index('\n## Tests added or changed\n')]
        self.assertIn('#5', issues)
        self.assertIn('The store loses writes', issues)
        self.assertNotIn('END-OF-ISSUE', issues)              # first 3000 characters only
        self.assertIn('#9', issues)                           # cannot be read: a note, not a failure
        state = text[text.index('\n## State question\n'):text.index('\n## Linked issues\n')]
        for q in ('runs again', 'stops half-way', 'old or empty state'):
            self.assertIn(q, state)
        self.assertIn('app/store.py', state)
        self.assertNotIn('app/util.py', state)
        lens = text[text.index('\n## Lens questions\n'):text.index('\n## State question\n')]
        self.assertIn('fork-lens-state.md', lens)
        self.assertIn('New tests should fail on the old code', lens)

    def test_truncation_note_over_cap(self):
        """Over the size cap, is the diff cut on a line with a note saying how much was shown and how to get the
        rest; and under it, is there no note?"""
        _, text = self.packet('7', '-R', 'example/repo', '--max-diff', '300')
        diff = text[text.index('\n## Diff\n'):]
        self.assertIn('truncated', diff)
        self.assertIn(f'git diff {self.merge_base} {self.head}', diff)
        self.assertNotIn('+util line 30 changed', diff)
        _, full = self.packet('7', '-R', 'example/repo')
        self.assertNotIn('truncated', full[full.index('\n## Diff\n'):])

    def test_no_linked_issue(self):
        """Does a pull request whose body names no issue still get a packet, saying so?"""
        _, text = self.packet('8', '-R', 'example/repo')
        issues = text[text.index('\n## Linked issues\n'):text.index('\n## Tests added or changed\n')]
        self.assertIn('No linked issue', issues)
        for s in SECTIONS:
            self.assertIn(s, text)

    def test_twice_same_file(self):
        """Does a second run give the same path and the same bytes (nothing time-dependent, no leftovers)?"""
        p1, t1 = self.packet('7', '-R', 'example/repo')
        p2, t2 = self.packet('7', '-R', 'example/repo')
        self.assertEqual(p1, p2)
        self.assertEqual(t1, t2)
        self.assertEqual(os.listdir(os.path.dirname(p1)).count(os.path.basename(p1)), 1)
        self.assertEqual([n for n in os.listdir(os.path.dirname(p1)) if '.tmp' in n], [])

    def test_refuses_out_dir_inside_repo(self):
        """Is a packet never written inside the repo, even when asked to?"""
        r = self.run_cmd('7', '-R', 'example/repo', '--out-dir', os.path.join(self.work, 'packets'))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertFalse(os.path.exists(os.path.join(self.work, 'packets')))
        self.assertEqual(r.stdout, '')

    def test_unknown_pr_fails(self):
        """Is a pull request gh cannot read exit 2 with gh's reason, and no file?"""
        r = self.run_cmd('99', '-R', 'example/repo')
        self.assertEqual(r.returncode, 2)
        self.assertIn('Could not resolve', r.stderr)
        self.assertEqual(r.stdout, '')


if __name__ == '__main__':
    unittest.main()
