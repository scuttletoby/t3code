"""Tests of head_check.py (run: python3 fork-ci/actions/head-check/head_check.test.py).

The check fails a pull request head that is not the change its review read, with the merge queue's own rules
(../pr-queue: lens.py, pr_queue.holds, config.py). For each rule: what passes, what fails, and what a re-run, a stop
half-way or old or empty state does.

Real commits in a temporary World: a local bare repository stands in for GitHub, the checkout is GitHub's merge commit
of the pull request, `gh` is a fake, and nothing here can reach the network or a real gh login (the guard below fails
the whole run if anything starts the real `gh`).
"""
import atexit, os, shlex, shutil, sys, tempfile

# --- guard: first, before anything can start gh or git ------------------------------------------------------------
sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
_GUARD = tempfile.mkdtemp(prefix='head-check-guard-')
_GH_RAN = os.path.join(_GUARD, 'gh-ran')
os.makedirs(os.path.join(_GUARD, 'bin'))
os.makedirs(os.path.join(_GUARD, 'gh-config'))
with open(os.path.join(_GUARD, 'bin', 'gh'), 'w') as _f:
    _f.write(f'#!/bin/sh\necho "$*" >> {shlex.quote(_GH_RAN)}\n'
             'echo "head_check.test.py: a test started the real gh" >&2\nexit 97\n')
os.chmod(os.path.join(_GUARD, 'bin', 'gh'), 0o755)
os.environ['PATH'] = os.path.join(_GUARD, 'bin') + os.pathsep + os.environ.get('PATH', '')
for _k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST',
           'GITHUB_STEP_SUMMARY', 'GITHUB_OUTPUT'):
    os.environ.pop(_k, None)
os.environ.update(GH_CONFIG_DIR=os.path.join(_GUARD, 'gh-config'), GIT_ALLOW_PROTOCOL='file',
                  GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0')


def _gh_never_ran():
    ran = None
    if os.path.exists(_GH_RAN):
        with open(_GH_RAN) as f:
            ran = f.read()
    shutil.rmtree(_GUARD, ignore_errors=True)
    if ran is not None:
        print(f'FAILED: a test started the real gh: {ran[:500]}', file=sys.stderr)
        sys.stderr.flush()
        os._exit(97)   # unittest may have said OK: this exit code is the answer


atexit.register(_gh_never_ran)

import json, subprocess, types, unittest, unittest.mock  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import head_check as H  # noqa: E402
import config  # noqa: E402  (../pr-queue, put on the path by head_check)
import lens  # noqa: E402

REPO = 'owner/proj'
CONFIG_PATH = '.github/pr-queue.json'
CFG_RAW = {
    'required_checks': ['build', 'review-check'], 'merge_method': 'squash', 'delete_branch': True,
    'bot_logins': ['review-bot'], 'lens_author_associations': ['OWNER', 'COLLABORATOR'],
    'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
    'head_check': 'review-check',
}
CFG = config.parse(json.dumps(CFG_RAW))
LENS = 'lens'   # built at run time: no line of this file starts with the lens marker
A, B, C = 'a1' * 20, 'b2' * 20, 'c3' * 20
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t'}


def review(cfg=CFG, log=None):
    return lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association, cfg.lens_author_associations,
                       log=log or (lambda text: None))


class Posts:
    """Comments with rising createdAt, as GitHub lists them."""
    def __init__(self):
        self.n = 0

    def post(self, body, assoc='OWNER', login='owner', edited=False, hidden=False):
        self.n += 1
        return {'body': body, 'author': {'login': login}, 'authorAssociation': assoc, 'includesCreatedEdit': edited,
                'isMinimized': hidden, 'createdAt': f'2026-11-14T01:{self.n:02d}:00Z',
                'url': f'https://github.com/{REPO}/pull/7#issuecomment-{self.n}'}

    def lens(self, name, sha, **kw):
        return self.post(f'{LENS}: {name} · reviewer: x · read at: {sha}\nfindings: none', **kw)

    def override(self, sha, **kw):
        return self.post(f'tool ready --head {sha}: read it myself', **kw)


def pr(comments=(), head=A, draft=False, reviews=()):
    return {'isDraft': draft, 'headRefOid': head, 'comments': list(comments), 'reviews': list(reviews)}


class FakeCheckout:
    """A checkout whose HEAD merges `head` into `base` and that knows no history: every git call fails, so only a head
    that is the read commit itself can pass (pr_queue.holds finds no tree and no parents)."""
    def __init__(self, head=A, base=C, parents=None):
        self.parents = parents if parents is not None else (base, head)

    def merge_parents(self):
        return self.parents

    def git(self, *a):
        return types.SimpleNamespace(returncode=1, stdout='', stderr='no history here')

    def has(self, sha):
        return True

    fetch = has


def lines(n=30, extra=None, at=None):
    out = [f'line{i}' for i in range(1, n + 1)]
    if extra:
        out[at:at] = extra
    return '\n'.join(out) + '\n'


class World:
    """"GitHub" (a bare repository that serves any commit by sha), a work clone that builds commits, and the job's
    checkout: GitHub's merge commit of a head into a base, fetched and checked out as actions/checkout does."""
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='head-check-world-')
        self.remote, self.work = os.path.join(self.dir, 'github.git'), os.path.join(self.dir, 'work')
        self.co = os.path.join(self.dir, 'checkout')
        self.git(self.dir, 'init', '-q', '--bare', '-b', 'main', self.remote)
        self.git(self.remote, 'config', 'uploadpack.allowAnySHA1InWant', 'true')
        self.git(self.dir, 'init', '-q', '-b', 'main', self.work)
        self.git(self.work, 'remote', 'add', 'origin', self.remote)
        self.n = 0

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    @staticmethod
    def git(cwd, *a):
        r = subprocess.run(['git', *a], cwd=cwd, text=True, capture_output=True, env={**os.environ, **GIT_ENV})
        if r.returncode != 0:
            raise RuntimeError(f'git {a}: {r.stderr}')
        return r.stdout.strip()

    def commit(self, files, parents=(), msg='c'):
        for name in os.listdir(self.work):
            if name != '.git':
                p = os.path.join(self.work, name)
                shutil.rmtree(p) if os.path.isdir(p) else os.remove(p)
        for path, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(self.work, path)) or self.work, exist_ok=True)
            with open(os.path.join(self.work, path), 'w') as f:
                f.write(text)
        self.git(self.work, 'add', '-A')
        tree = self.git(self.work, 'write-tree')
        sha = self.git(self.work, 'commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', msg)
        self.n += 1
        self.git(self.work, 'push', '-q', 'origin', f'{sha}:refs/keep/{self.n}')
        return sha

    def merge(self, head, base, files=None):
        """What `gh pr update-branch` makes: (parents head, base), git's clean merge, or exactly `files`."""
        if files is not None:
            return self.commit(files, [head, base], 'Merge main')
        tree = self.git(self.work, 'merge-tree', '--write-tree', head, base).split()[0]
        sha = self.git(self.work, 'commit-tree', tree, '-p', head, '-p', base, '-m', 'Merge main')
        self.n += 1
        self.git(self.work, 'push', '-q', 'origin', f'{sha}:refs/keep/{self.n}')
        return sha

    def checkout(self, base, head):
        """The job's checkout: GitHub's merge commit (parents base, head), with base and head's history and nothing
        else (no ref reaches a commit the head dropped). Returns the checkout's path."""
        tree = self.git(self.work, 'merge-tree', '--write-tree', '--allow-unrelated-histories', base, head).split()[0]
        merge = self.git(self.work, 'commit-tree', tree, '-p', base, '-p', head, '-m', 'GitHub merge')
        self.git(self.work, 'push', '-q', '-f', 'origin', f'{merge}:refs/pull/7/merge')
        shutil.rmtree(self.co, ignore_errors=True)
        self.git(self.dir, 'init', '-q', '-b', 'main', self.co)
        self.git(self.co, 'remote', 'add', 'origin', self.remote)
        self.git(self.co, 'fetch', '-q', '--no-tags', 'origin', '+refs/pull/7/merge:refs/remotes/pull/7/merge')
        self.git(self.co, 'checkout', '-q', '--detach', merge)
        return self.co


def base_files(cfg_raw=CFG_RAW, **files):
    return {CONFIG_PATH: json.dumps(cfg_raw), 'x.py': lines(), **files}


def fake_gh(*answers, calls=None):
    """gh that answers `pr view` with each answer in turn (the last one repeats): (code, stdout)."""
    calls = [] if calls is None else calls

    def gh(*a):
        calls.append(a)
        if a[:2] != ('pr', 'view'):
            raise AssertionError(f'gh must only read: {a}')
        code, out = answers[min(len(calls) - 1, len(answers) - 1)]
        return types.SimpleNamespace(returncode=code, stdout=out, stderr='boom' if code else '')
    return gh


def run_cli(path, gh, *extra, env=None, sleeps=None):
    out = []
    code = H.cli(['--repo', REPO, '--pr', '7', '--config', CONFIG_PATH, '--path', path, *extra], env=env or {}, gh=gh,
                 sleep=(sleeps.append if sleeps is not None else lambda s: None), out=out.append)
    return code, '\n'.join(out)


# --- the review rules, on a checkout whose merge commit merges the head GitHub reports ----------------------------
class Rules(unittest.TestCase):
    def setUp(self):
        self.p = Posts()

    def verdict(self, comments, head=A, required='', **kw):
        names, problems = H.required_names(required)
        self.assertEqual(problems, [])
        return H.verdict(pr(comments, head=head, **{k: v for k, v in kw.items() if k == 'draft'}), review(),
                         kw.get('checkout') or FakeCheckout(head), names, kw.get('pass_drafts', False))

    def test_every_lens_read_the_head_passes(self):
        ok, why, rows = self.verdict([self.p.lens('correctness', A), self.p.lens('design', A)])
        self.assertTrue(ok, why)
        self.assertEqual([(name, good) for name, good, _ in rows], [('correctness', True), ('design', True)])

    def test_no_lens_comments_passes_unless_a_lens_is_required(self):   # empty state
        ok, why, rows = self.verdict([self.p.post('looks fine, thanks')])
        self.assertTrue(ok, why)
        self.assertEqual(rows, [])
        ok, why, rows = self.verdict([], required='correctness')
        self.assertFalse(ok)
        self.assertIn('correctness', why)
        self.assertEqual(rows, [('correctness', False,
                                 'required: no lens comment of a counted author has this name (lenses found: none)')])

    def test_a_required_lens_must_be_among_the_reads(self):
        ok, why, _ = self.verdict([self.p.lens('correctness', A)], required='correctness\nsecurity')
        self.assertFalse(ok)
        self.assertIn('security', why)
        ok, why, rows = self.verdict([self.p.lens('Correctness r2', A), self.p.lens('security', A)],
                                     required='correctness\nsecurity')
        self.assertTrue(ok, why)
        self.assertIn(('correctness (required)', True), [(n, g) for n, g, _ in rows])

    def test_a_required_lens_name_may_hold_a_comma(self):   # one name per line, trimmed, blank lines skipped
        names, problems = H.required_names('\n  data-integrity (DI-1, DI-2)  \r\n\n security\n')
        self.assertEqual((names, problems), ({'data-integrity (di-1, di-2)': 'data-integrity (DI-1, DI-2)',
                                              'security': 'security'}, []))
        ok, why, _ = self.verdict([self.p.lens('data-integrity (DI-1, DI-2)', A), self.p.lens('security', A)],
                                  required='data-integrity (DI-1, DI-2)\nsecurity')
        self.assertTrue(ok, why)

    def test_a_required_name_no_comment_has_is_named_with_the_lenses_found(self):   # a typo fails, and says why
        ok, why, rows = self.verdict([self.p.lens('correctness', A), self.p.lens('security', A)],
                                     required='correctness\nsecurty')
        self.assertFalse(ok)
        self.assertIn('required lens without a read: securty', why)
        self.assertIn('one name per line', why)
        self.assertIn('lenses found: correctness, security', why)
        self.assertEqual(rows[0], ('securty', False, 'required: no lens comment of a counted author has this name '
                                                     '(lenses found: correctness, security)'))

    def test_a_required_lens_that_reads_nothing_fails(self):
        ok, _, rows = self.verdict([self.p.lens('correctness', A[:12])], required='correctness')
        self.assertFalse(ok)
        self.assertEqual(rows[0][:2], ('correctness (required)', False))

    def test_lens_names_that_name_no_lens_are_refused(self):
        for text in ('<lens>', 'r2', 're-check', '***'):
            with self.subTest(text=text):
                self.assertTrue(H.required_names(text)[1])

    def test_a_draft_passes_only_when_drafts_pass(self):
        stale = [self.p.lens('correctness', B)]
        self.assertTrue(self.verdict(stale, draft=True, pass_drafts=True)[0])
        ok, _, _ = self.verdict(stale, draft=True, checkout=FakeCheckout(A))
        self.assertFalse(ok)

    def test_lenses_on_different_commits_fail(self):
        ok, why, rows = self.verdict([self.p.lens('a', A), self.p.lens('b', B)])
        self.assertFalse(ok)
        self.assertIn('did not all read one head', why)
        self.assertEqual({good for _, good, _ in rows}, {False})

    def test_a_short_sha_reads_nothing(self):   # a later push can share the prefix
        ok, _, rows = self.verdict([self.p.lens('correctness', A[:7])])
        self.assertFalse(ok)
        self.assertIn(lens.SHORT, rows[0][2])

    def test_an_edited_lens_reads_nothing_until_it_posts_again(self):
        ok, _, rows = self.verdict([self.p.lens('correctness', A, edited=True)])
        self.assertFalse(ok)
        self.assertIn(lens.EDITED, rows[0][2])
        self.assertTrue(self.verdict([self.p.lens('correctness', A, edited=True), self.p.lens('correctness', A)])[0])

    def test_a_missing_edit_field_reads_nothing(self):   # fail closed: GitHub did not say "never edited"
        post = self.p.lens('correctness', A)
        del post['includesCreatedEdit']
        self.assertFalse(self.verdict([post])[0])

    def test_a_hidden_lens_reads_nothing(self):
        ok, _, rows = self.verdict([self.p.lens('correctness', A, hidden=True)])
        self.assertFalse(ok)
        self.assertIn(lens.HIDDEN, rows[0][2])

    def test_a_strangers_lens_neither_reads_nor_blocks(self):   # anyone can comment on a public repository
        for assoc in ('NONE', 'CONTRIBUTOR', 'FIRST_TIME_CONTRIBUTOR', None):
            with self.subTest(assoc=assoc):
                stale = self.p.lens('correctness', B, assoc=assoc, login='stranger')
                self.assertTrue(self.verdict([self.p.lens('correctness', A), stale])[0])
                ok, why, _ = self.verdict([self.p.lens('correctness', A, assoc=assoc)], required='correctness')
                self.assertFalse(ok, f'{assoc} vouched for a head')

    def test_a_bot_counts_for_nothing_even_as_owner(self):
        for login in ('review-bot', 'Review-Bot', 'helper[bot]'):
            with self.subTest(login=login):
                self.assertTrue(self.verdict([self.p.lens('correctness', A), self.p.lens('x', B, login=login)])[0])

    def test_an_old_format_report_reads_nothing(self):   # old state: reports from before the fixed grammar
        for body in (f'## Correctness {LENS}\nsafe to merge: yes', f'Review (correctness) read at: {A}',
                     f'```\n{LENS}: correctness · read at: {A}\n```', 'covered: all\nchecks: ran the tests'):
            with self.subTest(body=body):
                ok, _, rows = self.verdict([self.p.lens('correctness', A), self.p.post(body)])
                self.assertFalse(ok)
                self.assertIn(lens.NO_HEADER, ' '.join(name for name, _, _ in rows))

    def test_a_deleted_comment_counts_no_more(self):   # deleted: GitHub no longer lists it
        look_alike = self.p.post('safe to merge')
        good = self.p.lens('correctness', A)
        self.assertFalse(self.verdict([look_alike, good])[0])
        self.assertTrue(self.verdict([good])[0])
        stale, newer = self.p.lens('correctness', B), self.p.lens('correctness', A)
        self.assertTrue(self.verdict([stale, newer])[0])
        self.assertFalse(self.verdict([stale])[0])   # the newer read deleted: the old head's read stands

    def test_a_not_a_lens_note_is_no_lens(self):
        self.assertTrue(self.verdict([self.p.lens('correctness', A), self.p.post(f'not a {LENS}: fixed it')])[0])

    def test_the_owners_newer_override_covers_a_stale_lens(self):
        ok, why, rows = self.verdict([self.p.lens('correctness', B), self.p.override(A)])
        self.assertTrue(ok, why)
        self.assertIn('override', rows[0][2])

    def test_an_override_that_is_not_the_owners_shown_unedited_and_newer_counts_for_nothing(self):
        cases = {'collaborator': dict(assoc='COLLABORATOR'), 'stranger': dict(assoc='NONE'),
                 'edited': dict(edited=True), 'hidden': dict(hidden=True), 'bot': dict(login='helper[bot]')}
        for what, kw in cases.items():
            with self.subTest(what=what):
                self.assertFalse(self.verdict([self.p.lens('correctness', B), self.p.override(A, **kw)])[0])
        older = self.p.override(A)
        self.assertFalse(self.verdict([older, self.p.lens('correctness', B)])[0])

    def test_github_naming_no_head_fails(self):
        for head in (None, '', A[:12], 'z' * 40):
            with self.subTest(head=head):
                self.assertFalse(H.verdict(pr([self.p.lens('c', A)], head=head), review(), FakeCheckout(), {}, False)[0])

    def test_a_checkout_of_another_head_fails(self):   # a newer push: its own run decides
        ok, why, _ = self.verdict([self.p.lens('correctness', A)], checkout=FakeCheckout(B))
        self.assertFalse(ok)
        self.assertIn('newer head', why)

    def test_a_checkout_that_is_no_merge_commit_fails(self):
        ok, why, _ = self.verdict([self.p.lens('correctness', A)], checkout=FakeCheckout(parents=()))
        self.assertFalse(ok)
        self.assertIn('merge commit', why)

    def test_the_rules_are_lens_pys_not_a_copy(self):   # one source: change lens.py's answer, the check follows
        with unittest.mock.patch.object(lens.Review, 'lens_reads', lambda self, p: {'patched': (A, None, None)}), \
                unittest.mock.patch.object(lens.Review, 'reviewed_head', lambda self, p: (True, A, 'every lens read it')):
            ok, _, rows = H.verdict(pr([self.p.lens('c', B)]), review(), FakeCheckout(), {}, False)
        self.assertTrue(ok)
        self.assertEqual([name for name, _, _ in rows], ['patched'])
        with unittest.mock.patch.object(lens.Review, 'lens_reads', lambda self, p: {'c': (None, 'x', None)}), \
                unittest.mock.patch.object(lens.Review, 'reviewed_head', lambda self, p: (True, None, 'x')):
            self.assertFalse(H.verdict(pr([self.p.lens('c', A)]), review(), FakeCheckout(), {}, False)[0])
        self.assertIs(H.lens, lens)
        self.assertEqual(os.path.dirname(os.path.realpath(lens.__file__)), os.path.join(os.path.dirname(HERE), 'pr-queue'))


# --- the same change: real commits ------------------------------------------------------------------------------
class SameChange(unittest.TestCase):
    """GitHub's merge commit is the checkout; the read commit, the base and the head are real commits."""

    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.p = Posts()
        self.m0 = self.w.commit(base_files())
        self.read = self.w.commit(base_files(**{'x.py': lines(extra=['fix'], at=20)}), [self.m0])
        self.m1 = self.w.commit(base_files(**{'y.py': 'y\n'}), [self.m0])

    def check(self, head, read=None, base=None, comments=None, *extra):
        path = self.w.checkout(base or self.m1, head)
        comments = comments if comments is not None else [self.p.lens('correctness', read or self.read)]
        return run_cli(path, fake_gh((0, json.dumps(pr(comments, head=head)))), *extra)

    def test_the_read_commit_itself_passes(self):
        code, out = self.check(self.read)
        self.assertEqual(code, 0, out)

    def test_a_queue_update_of_the_read_commit_passes(self):
        code, out = self.check(self.w.merge(self.read, self.m1))
        self.assertEqual(code, 0, out)
        self.assertIn('merged with the base', out)

    def test_a_second_queue_update_passes(self):
        first = self.w.merge(self.read, self.m1)
        m2 = self.w.commit(base_files(**{'y.py': 'y\n', 'z.py': 'z\n'}), [self.m1])
        code, out = self.check(self.w.merge(first, m2), base=m2)
        self.assertEqual(code, 0, out)

    def test_a_commit_on_top_of_the_read_commit_fails(self):   # a plain push after the review
        head = self.w.commit(base_files(**{'x.py': lines(extra=['fix', 'more'], at=20)}), [self.read])
        code, out = self.check(head)
        self.assertEqual(code, 1, out)
        self.assertIn('::error::', out)

    def test_a_lens_on_an_old_head_fails(self):   # old state: the review read a commit before the last push
        old = self.read
        head = self.w.commit(base_files(**{'x.py': lines(extra=['fix2'], at=20)}), [old])
        code, out = self.check(head, read=old)
        self.assertEqual(code, 1, out)
        self.assertIn(f'not the change read at {old[:12]}', out)

    def test_a_merge_with_an_extra_change_in_it_fails(self):
        evil = self.w.merge(self.read, self.m1, files=base_files(**{'x.py': lines(extra=['fix', 'sneaked'], at=20),
                                                                     'y.py': 'y\n'}))
        self.assertEqual(self.check(evil)[0], 1)

    def test_the_read_commit_merged_with_a_branch_that_is_not_the_base_fails(self):
        other = self.w.commit(base_files(**{'evil.py': 'evil\n'}), [self.m0])
        self.assertEqual(self.check(self.w.merge(self.read, other))[0], 1)

    def test_a_force_pushed_rewrite_fails(self):
        rewritten = self.w.commit(base_files(**{'x.py': lines(extra=['other'], at=20)}), [self.m0])
        self.assertEqual(self.check(rewritten)[0], 1)

    def test_a_read_commit_the_head_dropped_is_fetched_from_github(self):   # an amended message: same change
        amended = self.w.commit(base_files(**{'x.py': lines(extra=['fix'], at=20)}), [self.m0], msg='amended')
        path = self.w.checkout(self.m1, amended)
        self.assertNotEqual(subprocess.run(['git', '-C', path, 'cat-file', '-e', self.read + '^{commit}'],
                                           capture_output=True).returncode, 0)
        code, out = run_cli(path, fake_gh((0, json.dumps(pr([self.p.lens('c', self.read)], head=amended)))))
        self.assertEqual(code, 0, out)

    def test_a_read_commit_github_does_not_have_fails(self):
        code, out = self.check(self.w.merge(self.read, self.m1), read='e' * 40)
        self.assertEqual(code, 1, out)
        self.assertIn('could not be fetched from origin', out)
        self.assertIn('persist-credentials: false', out)

    def test_the_pull_requests_gitattributes_cannot_make_a_conflict_clean(self):
        # The read change and the base both change line 20; the read commit also asks for merge=union. The queue's
        # bare cache reads no attributes, so the conflict stands there: the check must agree, although the checkout's
        # work tree holds that .gitattributes.
        attrs = {'.gitattributes': 'x.py merge=union\n'}
        read = self.w.commit(base_files(**attrs, **{'x.py': lines().replace('line20', 'X')}), [self.m0])
        m1 = self.w.commit(base_files(**{'x.py': lines().replace('line20', 'Y')}), [self.m0])
        union = base_files(**attrs, **{'x.py': lines().replace('line20', 'Y\nX')})
        head = self.w.merge(read, m1, files=union)
        code, out = self.check(head, read=read, base=m1)
        self.assertEqual(code, 1, out)

    def test_the_config_is_the_bases_not_the_pull_requests(self):
        # The head's copy makes the owner a bot, which would hide the owner's stale lens (no lens comments: pass).
        head = self.w.commit(base_files(cfg_raw={**CFG_RAW, 'bot_logins': ['owner']},
                                        **{'x.py': lines(extra=['unread'], at=20)}), [self.read])
        code, out = self.check(head)
        self.assertEqual(code, 1, out)

    def test_a_base_without_the_config_is_refused(self):
        bare_base = self.w.commit({'other.py': 'o\n'}, [])
        path = self.w.checkout(bare_base, self.read)
        code, out = run_cli(path, fake_gh((0, json.dumps(pr([])))))
        self.assertEqual(code, H.REFUSED, out)
        self.assertIn('has no config', out)

    def test_a_shallow_or_tampered_checkout_is_refused(self):
        for name, text, says in (('shallow', self.m1 + '\n', 'fetch-depth: 0'), ('info/grafts', '', 'info/grafts'),
                                 ('objects/info/alternates', '', 'alternates')):
            with self.subTest(name=name):
                path = self.w.checkout(self.m1, self.read)
                target = os.path.join(path, '.git', name)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, 'w') as f:
                    f.write(text)
                calls = []
                code, out = run_cli(path, fake_gh((0, json.dumps(pr([]))), calls=calls))
                self.assertEqual(code, H.REFUSED, out)
                self.assertIn(says, out)
                self.assertEqual(calls, [])

    def test_a_checkout_that_is_not_a_merge_commit_is_refused(self):   # refused (2) before GitHub is read, not failed
        path = self.w.checkout(self.m1, self.read)
        self.w.git(path, 'checkout', '-q', '--detach', self.read)
        calls = []
        code, out = run_cli(path, fake_gh((0, json.dumps(pr([self.p.lens('c', self.read)], head=self.read))),
                                          calls=calls))
        self.assertEqual(code, H.REFUSED, out)
        self.assertIn("not GitHub's two-parent merge commit", out)
        self.assertEqual(calls, [])


# --- reading GitHub, the outputs, re-runs and stops -------------------------------------------------------------
class Runs(unittest.TestCase):
    def setUp(self):
        self.w = World()
        self.addCleanup(self.w.close)
        self.p = Posts()
        self.m0 = m0 = self.w.commit(base_files())
        self.read = self.w.commit(base_files(**{'x.py': lines(extra=['fix'], at=20)}), [m0])
        self.path = self.w.checkout(m0, self.read)
        self.good = json.dumps(pr([self.p.lens('correctness', self.read)], head=self.read))
        self.stale = json.dumps(pr([self.p.lens('correctness', m0)], head=self.read))
        self.files = tempfile.mkdtemp(prefix='head-check-files-')
        self.addCleanup(shutil.rmtree, self.files, True)
        self.env = {'GITHUB_STEP_SUMMARY': os.path.join(self.files, 'summary.md'),
                    'GITHUB_OUTPUT': os.path.join(self.files, 'output')}

    def read_file(self, name):
        path = os.path.join(self.files, name)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return f.read()

    def test_a_gh_error_fails_after_the_retries(self):
        calls, sleeps = [], []
        code, out = run_cli(self.path, fake_gh((1, ''), calls=calls), env=self.env, sleeps=sleeps)
        self.assertEqual(code, 1)
        self.assertEqual(len(calls), H.ATTEMPTS)
        self.assertEqual(sleeps, [H.RETRY_SECONDS] * (H.ATTEMPTS - 1))
        self.assertIn('::error::', out)
        self.assertEqual(self.read_file('output'), 'result=fail\n')

    def test_one_failed_read_is_retried(self):
        self.assertEqual(run_cli(self.path, fake_gh((1, ''), (0, self.good)))[0], 0)

    def test_an_answer_that_is_not_a_pr_fails(self):
        for text in ('', 'null', '[]', '{}', '{"isDraft": "no", "comments": [], "reviews": []}',
                     json.dumps(dict(pr(), comments=None)), json.dumps(dict(pr(), reviews=None))):
            with self.subTest(text=text):
                self.assertEqual(run_cli(self.path, fake_gh((0, text)))[0], 1)

    def test_pass_and_fail_reach_the_exit_code_summary_and_output(self):
        code, out = run_cli(self.path, fake_gh((0, self.good)), env=self.env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.read_file('output'), 'result=pass\n')
        self.assertIn('| correctness | ok |', self.read_file('summary.md'))
        os.remove(self.env['GITHUB_OUTPUT'])
        code, out = run_cli(self.path, fake_gh((0, self.stale)), env=self.env)
        self.assertEqual(code, 1, out)
        self.assertEqual(self.read_file('output'), 'result=fail\n')
        self.assertIn('| correctness | fail |', self.read_file('summary.md'))

    def test_a_rerun_on_the_same_head_gives_the_same_answer_and_writes_nothing_else(self):
        refs = self.w.git(self.path, 'for-each-ref')
        for answer in (self.good, self.stale):
            with self.subTest(pass_=answer is self.good):
                calls = []
                first = run_cli(self.path, fake_gh((0, answer), calls=calls))
                self.assertEqual(run_cli(self.path, fake_gh((0, answer), calls=calls)), first)
                self.assertEqual({c[:2] for c in calls}, {('pr', 'view')})   # reads only: no comment, no edit
        self.assertEqual(self.w.git(self.path, 'for-each-ref'), refs)
        self.assertEqual(self.w.git(self.path, 'status', '--porcelain'), '')

    def test_a_run_stopped_before_the_answer_leaves_nothing(self):
        def dies(*a):
            raise KeyboardInterrupt   # the job is cancelled while gh runs
        with self.assertRaises(KeyboardInterrupt):
            run_cli(self.path, dies, env=self.env)
        self.assertIsNone(self.read_file('output'))
        self.assertIsNone(self.read_file('summary.md'))

    def test_refusals_touch_nothing_and_name_every_reason(self):
        calls = []
        out = []
        code = H.cli(['--repo', 'not a repo', '--pr', '', '--config', CONFIG_PATH, '--path', self.path,
                      '--required-lenses', '<name>'], env=self.env, gh=fake_gh((0, self.good), calls=calls),
                     out=out.append)
        self.assertEqual(code, H.REFUSED)
        self.assertEqual(calls, [])
        self.assertIsNone(self.read_file('output'))
        for part in ('--repo', '--pr', 'names no lens'):
            self.assertIn(part, out[0])

    def test_a_bad_config_on_the_base_is_refused(self):
        bad = self.w.commit(base_files(cfg_raw={**CFG_RAW, 'lens_author_associations': ['NONE']}), [self.m0])
        path = self.w.checkout(bad, self.read)
        code, out = run_cli(path, fake_gh((0, self.good)))
        self.assertEqual(code, H.REFUSED, out)
        self.assertIn('lens_author_associations', out)

    def test_no_text_from_a_comment_starts_a_workflow_command(self):
        name = 'x%0A::warning::y\r::error::z\n::error::w'
        self.assertNotIn('\n', H.escape(name))
        self.assertNotIn('\r', H.escape(name))
        self.assertIn('%250A', H.escape(name))
        answer = json.dumps(pr([self.p.post(f'{LENS}: {name} · read at: {self.read}\n::error::forged')],
                               head=self.read))
        code, out = run_cli(self.path, fake_gh((0, answer)))
        self.assertTrue(all(not line.startswith('::') or line.startswith('::error::#7') for line in out.splitlines()),
                        out)


# --- the action -------------------------------------------------------------------------------------------------
class Action(unittest.TestCase):
    """action.yml's step, run as Actions runs it, with a fake python3 that prints its arguments."""

    def setUp(self):
        import yaml
        with open(os.path.join(HERE, 'action.yml')) as f:
            self.action = yaml.safe_load(f)
        self.bin = tempfile.mkdtemp(prefix='head-check-bin-')
        self.addCleanup(shutil.rmtree, self.bin, True)
        with open(os.path.join(self.bin, 'python3'), 'w') as f:
            f.write('#!/bin/sh\nfor a in "$@"; do printf "%s\\n" "$a"; done\n')
        os.chmod(os.path.join(self.bin, 'python3'), 0o755)

    def run_step(self, **env):
        step = self.action['runs']['steps'][0]
        base = {'REPO': REPO, 'PR': '7', 'CONFIG': CONFIG_PATH, 'CHECKOUT_PATH': '.', 'REQUIRED_LENSES': '',
                'PASS_DRAFTS': 'false', 'GITHUB_ACTION_PATH': HERE}
        return subprocess.run(['bash', '--noprofile', '--norc', '-eo', 'pipefail', '-c', step['run']],
                              env={'PATH': self.bin + os.pathsep + os.environ['PATH'], **base, **env},
                              capture_output=True, text=True)

    def test_it_runs_the_check_isolated_with_every_input(self):
        r = self.run_step(REQUIRED_LENSES='a\nb', PASS_DRAFTS='true')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.split('\n')[:2], ['-I', os.path.join(HERE, 'head_check.py')])
        args = r.stdout.split('\n')[2:]
        self.assertEqual(args[args.index('--config') + 1], CONFIG_PATH)   # a repository path, read from the base
        self.assertEqual(args[args.index('--required-lenses') + 1], 'a')
        self.assertIn('--pass-drafts', args)
        self.assertNotIn('--pass-drafts', self.run_step().stdout.split('\n'))

    def test_a_pass_drafts_that_is_not_true_or_false_is_refused(self):
        r = self.run_step(PASS_DRAFTS='yes')
        self.assertEqual(r.returncode, 2)
        self.assertIn('::error::', r.stdout)

    def test_the_refusal_does_not_echo_the_pass_drafts_value(self):   # a newline in it could start a command
        r = self.run_step(PASS_DRAFTS='yes\n::warning::forged')
        self.assertEqual(r.returncode, 2)
        self.assertNotIn('forged', r.stdout + r.stderr)

    def test_the_defaults_are_the_pull_request_events(self):
        inputs = self.action['inputs']
        self.assertEqual(inputs['pr']['default'], '${{ github.event.pull_request.number }}')
        self.assertEqual(inputs['pass-drafts']['default'], 'false')
        self.assertTrue(inputs['config']['required'])
        self.assertEqual(self.action['outputs']['result']['value'], '${{ steps.check.outputs.result }}')


class ReadmeExample(unittest.TestCase):
    """README.md's usage example: the triggers a base change needs, and the action pinned by sha."""

    def setUp(self):
        import re, yaml
        with open(os.path.join(HERE, 'README.md')) as f:
            text = f.read()
        blocks = re.findall(r'```yaml\n(.*?)```', text, re.S)
        self.assertEqual(len(blocks), 1)
        self.example = yaml.safe_load(blocks[0])
        self.text = blocks[0]

    def test_the_triggers_rerun_it_on_a_base_change_and_on_ready(self):
        types = self.example[True]['pull_request']['types']   # yaml reads `on` as True
        for kind in ('opened', 'synchronize', 'reopened', 'ready_for_review', 'edited'):
            self.assertIn(kind, types)

    def test_the_action_is_pinned_by_a_full_sha_never_by_a_local_path(self):
        import re
        uses = [s['uses'] for job in self.example['jobs'].values() for s in job['steps'] if 'uses' in s]
        mine = [u for u in uses if 'head-check' in u]
        self.assertEqual(len(mine), 1, uses)
        self.assertRegex(mine[0], r'^[^./][^@]*/fork-ci/actions/head-check@<release sha of ci/vN>$')
        self.assertNotIn('uses: ./', self.text)
        self.assertTrue(re.search(r'head-check@<release sha of ci/vN> # ci/vN', self.text))


if __name__ == '__main__':
    unittest.main()
