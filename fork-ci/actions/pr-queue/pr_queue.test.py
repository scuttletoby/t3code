"""Tests of pr_queue.py (run: python3 fork-ci/actions/pr-queue/pr_queue.test.py).

The queue turns auto-merge OFF before it updates a PR from its base and turns it back on, for the new head only, when
that head is exactly the change the review read merged with the base. Every other outcome leaves it OFF with a
comment. For each step: what a rerun, a stop half-way or old state does. Most of these are the source project's queue
tests, ported (README.md, "Where the rules come from"); the rest test what changed in the port.

Real commits in a temporary World: a local bare repository stands in for GitHub, `gh` is a fake, and nothing here can
reach the network or a real gh login (the guard below fails the whole run if anything starts the real `gh`).
"""
import atexit, os, shlex, shutil, sys, tempfile

# --- guard: first, before anything can start gh or git ------------------------------------------------------------
sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
_GUARD = tempfile.mkdtemp(prefix='pr-queue-guard-')
_GH_RAN = os.path.join(_GUARD, 'gh-ran')
os.makedirs(os.path.join(_GUARD, 'bin'))
os.makedirs(os.path.join(_GUARD, 'gh-config'))
with open(os.path.join(_GUARD, 'bin', 'gh'), 'w') as _f:
    _f.write(f'#!/bin/sh\necho "$*" >> {shlex.quote(_GH_RAN)}\n'
             'echo "pr_queue.test.py: a test started the real gh" >&2\nexit 97\n')
os.chmod(os.path.join(_GUARD, 'bin', 'gh'), 0o755)
os.environ['PATH'] = os.path.join(_GUARD, 'bin') + os.pathsep + os.environ.get('PATH', '')
for _k in ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'GH_HOST'):
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

import argparse, contextlib, fcntl, io, json, re, subprocess, unittest, unittest.mock, zlib  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402
import pr_queue as Q  # noqa: E402

REAL_HOLDS = Q.holds   # for a test that patches Q.holds and still needs the real one

REPO = 'owner/proj'
CFG_RAW = {
    'required_checks': ['build', 'test', 'review-check'], 'merge_method': 'squash', 'delete_branch': True,
    'bot_logins': ['review-bot'], 'lens_author_associations': ['OWNER', 'COLLABORATOR'], 'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'},
    'ready_command': 'tool ready {pr}', 'push_command': 'tool push', 'lens_format_doc': 'docs/review.md',
    'head_check': 'review-check',
}
CFG = config.parse(json.dumps(CFG_RAW))
REVIEW = lens.Review(CFG.bot_logins, CFG.override_prefix, CFG.override_association, CFG.lens_author_associations)
H1, H2, H3 = 'a1' * 20, 'b2' * 20, 'c3' * 20   # shas for the lens-read rules only (no git)
TOKENS = {Q.READ_TOKEN: 'run-token', Q.WRITE_TOKEN: 'pat-token'}   # stand-ins: no real token is ever read here
LENS = 'lens'   # built at run time: no line of this file starts with the lens marker
READ_AT = r'(?i)read at\s*:|\blens\s*:'


# --- the World: real commits, a local "GitHub" ----------------------------------------------------------------------
# What git_env sets on every git call of the queue, whatever the job's environment holds.
FORCED = {'GIT_TERMINAL_PROMPT': '0', 'LC_ALL': 'C', **Q.GIT_ENV,
          'GIT_ATTR_SOURCE': '4b825dc642cb6eb9a060e54bf8d69288fbee4904',   # the empty tree
          'GIT_DEFAULT_HASH': 'sha1', 'GIT_DEFAULT_REF_FORMAT': 'files'}   # #457 item 3: what CACHE_CONFIG names
GIT_ENV = {'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't', 'GIT_COMMITTER_EMAIL': 't@t',
           'GIT_EDITOR': 'true'}


class World:
    """A local bare repository standing in for GitHub (every commit it holds can be fetched by sha, as on GitHub) and
    a work clone that builds the commits."""
    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix='pr-queue-world-')
        self.remote, self.work = os.path.join(self.dir, 'github.git'), os.path.join(self.dir, 'work')
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
        """A commit with exactly these files ({path: text}) and these parents; kept on "GitHub" under refs/keep/."""
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
        return self.keep(sha)

    def merge(self, head, base):
        """What `gh pr update-branch` makes: a merge commit (parents head, base) with git's own clean merge result."""
        tree = self.git(self.work, 'merge-tree', '--write-tree', head, base).split()[0]
        return self.keep(self.git(self.work, 'commit-tree', tree, '-p', head, '-p', base, '-m', 'Merge main'))

    def keep(self, sha):
        self.n += 1
        self.git(self.work, 'push', '-q', 'origin', f'{sha}:refs/keep/{self.n}')
        return sha

    def set(self, ref, sha):   # e.g. ('refs/heads/main', M1), ('refs/pull/7/head', H2)
        self.git(self.remote, 'update-ref', ref, sha)


def make_queue(folder, cfg=CFG, sleeps=None, remote=None):
    """A Queue whose STATE, cache and lock are in `folder` (never a real queue's), fetching from `remote`."""
    q = Q.Queue(REPO, cfg, os.path.join(folder, 'state', 'plan.json'), os.path.join(folder, 'cache.git'),
                os.path.join(folder, 'lock', 'queue.lock'), sleep=sleeps.append if sleeps is not None else lambda s: None)
    q.remote = remote or os.path.join(folder, 'no-remote.git')
    return q


def lines(n=30, extra=None, at=None):
    """x.py with n numbered lines; `extra` lines inserted after line `at`."""
    out = [f'line{i}' for i in range(1, n + 1)]
    if extra:
        out[at:at] = extra
    return '\n'.join(out) + '\n'


class Killed(BaseException):
    """The process dies here (the job timeout, kill -9): no except/finally of the code runs."""


def ok(out=''):
    return argparse.Namespace(returncode=0, stdout=out, stderr='')


def failed(err='HTTP 502'):
    return argparse.Namespace(returncode=1, stdout='', stderr=err)


class QueueGh:
    """`gh` for one armed PR #7 that is BEHIND main, at head `h1` (real commits of World `w`). update-branch makes the
    PR head `after` (GitHub shows it after `polls` more views). `fail`: kinds whose gh call fails; `kill_at`: the kind
    where the process dies ('poll': the first view after update-branch). `flags`: the answers to review_flags' pages,
    in turn (none left: the read fails); not given: one page, the reviews as gh shows them (unedited, shown) with the
    `inline` review comments (inline_node) under a review of their own. `arm_says`: what GitHub answers to each arm in
    turn, an error text or None (armed); none left: armed."""
    def __init__(self, w, h1, after, comments=(), polls=0, fail=(), armed=True, behind=True, kill_at=None, reviews=(),
                 flags=None, inline=(), arm_says=()):
        self.w = w
        self.head, self.after, self.armed, self.behind, self.updated = h1, after, (h1 if armed else None), behind, False
        self.on_pr, self.polls, self.fail, self.kill_at = list(comments), polls, set(fail), kill_at
        self.reviews, self.flags, self.inline = list(reviews), flags if flags is None else list(flags), list(inline)
        self.arm_says = list(arm_says)
        self.calls, self.comments = [], []
        w.set('refs/pull/7/head', h1)

    def view(self):
        if self.updated and self.after:
            if self.polls:
                self.polls -= 1
            else:
                self.head = self.after
        return {'number': 7, 'state': 'OPEN', 'isDraft': False, 'baseRefName': 'main', 'headRefOid': self.head,
                'autoMergeRequest': {'mergeMethod': 'SQUASH'} if self.armed else None,
                'mergeStateStatus': 'BEHIND' if self.behind and not self.updated else 'BLOCKED', 'title': 't',
                'createdAt': '2026-11-07T00:00:00Z', 'statusCheckRollup': [], 'comments': self.on_pr,
                'reviews': self.reviews, 'commits': self.commits(), 'labels': []}

    def commits(self):   # the PR's commits as GitHub lists them: head's history that main lacks
        out = self.w.git(self.w.remote, 'rev-list', self.head, '^refs/heads/main')
        return [{'oid': sha} for sha in reversed(out.split())]

    @staticmethod
    def kind(a):
        if a[:2] == ('pr', 'merge'):
            return 'disarm' if '--disable-auto' in a else 'arm'
        return {('pr', 'view'): 'view', ('pr', 'comment'): 'comment', ('pr', 'update-branch'): 'update',
                ('pr', 'list'): 'list'}.get(tuple(a[:2]), ' '.join(a[:2]))

    def __call__(self, *a):
        self.calls.append(a)
        kind = self.kind(a)
        if kind == self.kill_at or (self.kill_at == 'poll' and kind == 'view' and self.updated):
            raise Killed(kind)
        if kind in self.fail:
            return failed('HTTP 502, read at: ' + 'c3' * 20)
        if kind == 'view':
            return ok(json.dumps(self.view()))
        if kind == 'api graphql' and f'query={Q.REVIEW_FLAGS_QUERY}' in a:
            if self.flags is None:
                return flags_page(*self.reviews, inline=self.inline)
            return self.flags.pop(0) if self.flags else failed('no more pages')
        if kind == 'list':
            return ok(json.dumps([self.view()]))
        if kind == 'disarm':
            self.armed = None
        elif kind == 'update':
            self.updated = True
            if self.after:
                self.w.set('refs/pull/7/head', self.after)
        elif kind == 'arm':
            says = self.arm_says.pop(0) if self.arm_says else None
            if says:
                return failed(says)
            self.armed = a[a.index('--match-head-commit') + 1]
        elif kind == 'comment':
            self.comments.append(a[a.index('--body') + 1])
        return ok()

    def order(self):   # what the queue did, without its reads (the view, and the reviews' flags and inline comments)
        return [k for k in map(self.kind, self.calls) if k not in ('view', 'api graphql')]

    def reads(self):   # how often the reviews' flags and inline comments were read
        return sum(self.kind(a) == 'api graphql' for a in self.calls)


def note(body, when, login='owner', association='OWNER', edited=False):
    """A comment as gh shows it: by default the owner's, never edited."""
    return {'body': body, 'createdAt': when, 'author': {'login': login}, 'authorAssociation': association,
            'includesCreatedEdit': edited}


def lens_at(name, sha, when='2026-11-07T10:00:00Z'):
    return note(f'{LENS}: {name} · reviewer: x · read at: {sha}\nfindings: none', when)


def gh_review(body, when, n=1, login='owner', association='OWNER'):
    """A review as gh pr view prints it: includesCreatedEdit false whether or not it was edited (gh never fetches
    it for reviews), no isMinimized, no url."""
    return {'id': f'PRR_{n}', 'body': body, 'submittedAt': when, 'author': {'login': login},
            'authorAssociation': association, 'includesCreatedEdit': False, 'state': 'COMMENTED'}


def inline_node(body, when='2026-11-07T10:00:00Z', login='owner', association='OWNER', edited=False, hidden=False,
                typename='User', state='SUBMITTED', n=1):
    """An inline review comment (on a line of the diff) as REVIEW_FLAGS_QUERY's answer gives it."""
    return {'state': state, 'body': body, 'createdAt': '2026-11-01T00:00:00Z', 'publishedAt': when,
            'includesCreatedEdit': edited, 'isMinimized': hidden, 'url': f'https://x/pull/7#discussion_r{n}',
            'authorAssociation': association, 'author': {'__typename': typename, 'login': login}}


# GraphQL's PageInfo.endCursor is a nullable String: GitHub can say hasNextPage true and give no cursor. `more=NO_CURSOR`
# fakes that page (more=None is the last page).
NO_CURSOR = object()


def page_info(more):
    return {'hasNextPage': more is not None, 'endCursor': None if more is NO_CURSOR else more}


def inline_page(nodes=(), more=None):
    """One page of a review's inline comments; more: the endCursor of a next page (NO_CURSOR: there is one, no cursor)."""
    return {'pageInfo': page_info(more), 'nodes': list(nodes)}


def flags_page(*reviews, edited=False, hidden=False, more=None, typename='User', inline=(), inline_more=None):
    """One page of REVIEW_FLAGS_QUERY's answer; more: the endCursor of a next page. `inline`: inline review comments
    (inline_node), under a review of their own that gh pr view does not list (PRR_inline; inline_more: the endCursor
    of that review's next page of them)."""
    nodes = [{'id': r['id'], 'includesCreatedEdit': edited, 'isMinimized': hidden, 'url': f'https://x/{r["id"]}',
              'author': {'__typename': typename, 'login': 'owner'}, 'comments': inline_page()} for r in reviews]
    if inline or inline_more:
        nodes.append({'id': 'PRR_inline', 'includesCreatedEdit': False, 'isMinimized': False, 'url': 'https://x/PRR_inline',
                      'author': {'__typename': 'User', 'login': 'owner'}, 'comments': inline_page(inline, inline_more)})
    page = page_info(more)
    return ok(json.dumps({'data': {'repository': {'pullRequest': {'reviews': {'pageInfo': page, 'nodes': nodes}}}}}))


def more_comments_page(nodes=(), more=None):
    """One page of MORE_COMMENTS_QUERY's answer: a review's next 100 inline comments."""
    return ok(json.dumps({'data': {'node': {'comments': inline_page(nodes, more)}}}))


def override(sha, when):   # an override comment as the project's ready command posts it
    return dict(note(f'tool ready --head {sha}: arming auto-merge for this head although these lenses did not read it: '
                     f'x. Why:\n\n> read it myself', when), authorAssociation='OWNER', includesCreatedEdit=False)


def run_quiet(fn, *a):
    with contextlib.redirect_stdout(io.StringIO()) as out:
        code = fn(*a)
    return code, out.getvalue()


class Base(unittest.TestCase):
    """main M0 -> M1 (adds lines at the top of x.py: other line numbers for the PR's change); the PR's change C on M0;
    up = what update-branch makes of C (merge of M1); resolved: a merge of M1 whose resolution changed the PR's
    lines."""
    def setUp(self):
        w = self.w = World()
        self.addCleanup(w.close)
        self.sleeps = []
        self.q = make_queue(w.dir, sleeps=self.sleeps, remote=w.remote)
        self.m0 = w.commit({'x.py': lines()})
        self.m1 = w.commit({'x.py': lines(extra=['main 1', 'main 2'], at=0)}, [self.m0])
        w.set('refs/heads/main', self.m1)
        self.c = w.commit({'x.py': lines(extra=['new'], at=20)}, [self.m0])
        self.up = w.merge(self.c, self.m1)
        self.resolved = w.commit({'x.py': lines(extra=['main 1', 'main 2'], at=0).replace('line21', 'new, resolved\nline21')},
                                 [self.c, self.m1])

    def fake(self, after='up', **kw):
        return QueueGh(self.w, kw.pop('h1', self.c), getattr(self, after) if after else None, **kw)

    def update(self, fake, dry=False, q=None):
        q = q or self.q
        q.gh = q.gh_write = fake
        return run_quiet(q.update, 7, dry)

    def plan_exists(self):
        return os.path.exists(self.q.state)


class Update(Base):
    def assertOff(self, f, code, said):
        self.assertIsNone(f.armed, 'auto-merge must stay OFF')
        self.assertNotIn('arm', f.order())
        self.assertEqual(len(f.comments), 1, f.comments)
        self.assertEqual(code, 0)
        self.assertNotRegex(f.comments[0], r'(?i)read at\s*:', 'a queue comment must never count as a lens read')
        self.assertIn(said, f.comments[0])
        self.assertIn('`tool ready 7`', f.comments[0])
        post = note(f.comments[0], 't')   # an author whose lens comments count: a look-alike would block (NO_HEADER)
        self.assertIsNone(REVIEW.lens_problem(post))
        self.assertEqual(REVIEW.reviewed_head({'comments': [post], 'reviews': []}), (False, None, 'no lens comments'))

    def test_same_change_rearms_at_the_new_head_only(self):   # main moved the PR's lines: still the same change
        f = self.fake(polls=2)   # GitHub shows the new head a few polls later
        code, out = self.update(f)
        self.assertEqual(code, 0, out)
        self.assertEqual(f.order(), ['disarm', 'update', 'arm'])
        self.assertEqual(f.armed, self.up)
        arm = next(a for a in f.calls if QueueGh.kind(a) == 'arm')
        self.assertEqual(arm[arm.index('--match-head-commit') + 1], self.up)
        self.assertEqual(self.sleeps, [CFG.poll_seconds] * 2)
        self.assertEqual(f.comments, [])
        self.assertFalse(self.plan_exists())

    def test_disarm_comes_before_the_update(self):
        f = self.fake()
        self.update(f)
        self.assertLess(f.order().index('disarm'), f.order().index('update'))

    def test_a_failed_disarm_means_no_update(self):   # stopped there: the PR is as before (armed, behind); rerun retries
        f = self.fake(fail=('disarm',))
        code, out = self.update(f)
        self.assertEqual(code, 1)
        self.assertEqual(f.order(), ['disarm'])
        self.assertEqual((f.armed, f.updated, f.comments), (self.c, False, []))
        self.assertFalse(self.plan_exists(), 'still armed: nothing to finish')

    def test_a_disarm_github_applied_despite_an_error_keeps_the_plan(self):   # gh's error does not say it failed
        f = self.fake()

        def gh(*a):
            r = f(*a)
            return failed('HTTP 502') if QueueGh.kind(a) == 'disarm' else r
        code, out = self.update(gh)
        self.assertEqual((code, f.armed, f.updated, f.comments), (Q.SILENT, None, False, []), out)
        self.assertTrue(self.plan_exists(), 'OFF and nothing says so yet: the next run finishes it')
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, f.comments), (0, self.c, []), out)

    def test_a_conflict_resolution_stays_off_and_comments(self):
        f = self.fake('resolved')
        code, out = self.update(f)
        self.assertOff(f, code, 'is not exactly the change read')
        self.assertIn('conflict resolution', f.comments[0])

    def test_no_new_head_in_time_keeps_the_plan_for_the_next_run(self):   # GitHub may show the update late
        f = self.fake(after=None)
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (Q.SILENT, None, []), out)
        self.assertEqual(sum(self.sleeps), CFG.wait_seconds)
        self.assertTrue(self.plan_exists(), 'the next run finishes it from STATE')
        f.head = self.up   # GitHub shows the update now
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)

    def test_a_new_head_with_other_content_stays_off(self):   # a force-push meanwhile, not a merge of main
        self.other = self.w.commit({'x.py': lines(extra=['other'], at=20)}, [self.m1])
        f = self.fake('other')
        self.assertOff(f, self.update(f)[0], 'is not exactly the change read')

    def test_a_merge_of_a_commit_that_is_not_on_main_stays_off(self):   # its content was never on main, nor read
        side = self.w.commit({'x.py': lines(extra=['main 1', 'main 2'], at=0), 'evil.sh': 'rm -rf /\n'}, [self.m1])
        self.side = self.w.merge(self.c, side)
        f = self.fake('side')
        self.assertOff(f, self.update(f)[0], 'is not exactly the change read')

    def test_a_fetch_error_changes_nothing(self):   # nothing is known yet, the next run tries again
        self.q.remote = os.path.join(self.w.dir, 'no-such-repo.git')   # never fetched: the cache does not exist yet
        f = self.fake()
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments, f.order()), (1, self.c, [], []), out)
        self.assertFalse(self.plan_exists())

    def test_a_read_commit_that_github_does_not_have_stays_off_and_is_not_updated(self):
        f = self.fake(comments=[lens_at('correctness', 'de' * 20, 't')])
        code, out = self.update(f)
        self.assertOff(f, code, 'GitHub does not have the commit read @')
        self.assertIn('`tool push`', f.comments[0])
        self.assertNotIn('update', f.order())

    def test_a_network_error_on_the_fetch_by_sha_is_no_gone_commit(self):   # None (nothing known), not False
        real = self.q.git

        def git(*a, **kw):
            if 'fetch' in a and not any(x.startswith('+refs/') for x in a):   # after NO_DETACH's -c flags
                return argparse.Namespace(returncode=128, stdout='', stderr='fatal: unable to access: timed out')
            return real(*a, **kw)
        self.w.set('refs/pull/7/head', self.c)
        self.q.git = git
        self.assertIsNone(run_quiet(self.q.fetch, 'main', 7, self.c, 'de' * 20)[0])
        self.q.git = real   # the real "not our ref" of a missing commit: False
        self.assertIs(run_quiet(self.q.fetch, 'main', 7, 'de' * 20)[0], False)

    def test_a_headerless_lens_comment_says_how_to_repost(self):
        fence = '`' * 3
        f = self.fake(comments=[note(f'{fence}\n{LENS}: correctness · reviewer: x · read at: {self.c[:7]}\n{fence}', 't')])
        code, out = self.update(f)
        self.assertOff(f, code, 'unclear')
        self.assertIn('Posting it again that way does not clear the old one', f.comments[0])
        self.assertIn('delete that comment (hiding it is not enough)', f.comments[0])
        self.assertIn('edit it to start with `not a lens` and a colon', f.comments[0])
        self.assertIn('docs/review.md', f.comments[0])
        self.assertIn('`tool ready --head <sha>: <why>`', f.comments[0])

    def test_a_headerless_lens_comment_re_posted_right_still_blocks(self):   # what the advice says, kept true
        fence = '`' * 3
        bad = note(f'{fence}\n{LENS}: correctness · reviewer: x · read at: {self.c[:7]}\n{fence}', 't1')
        f = self.fake(comments=[bad, lens_at('correctness', self.c, 't2')])
        self.assertOff(f, self.update(f)[0], 'unclear')

    def test_failed_update_or_arm_stays_off(self):
        f = self.fake(fail=('update',))
        self.assertOff(f, self.update(f)[0], 'update-branch failed')
        f = self.fake(fail=('arm',))
        self.update(f)
        self.assertIsNone(f.armed)
        self.assertIn('back on', f.comments[0])
        self.assertNotRegex(f.comments[0], r'(?i)read at\s*:', 'the quoted gh error had one')

    def test_a_failed_comment_keeps_the_state(self):   # still OFF and silent: the run fails, the next run says it
        f = self.fake('resolved', fail=('comment',))
        self.assertEqual(self.update(f)[0], Q.SILENT)
        self.assertIsNone(f.armed)
        with open(self.q.state) as s:
            self.assertEqual({k: v for k, v in json.load(s).items() if k in ('repo', 'pr')}, {'repo': REPO, 'pr': 7})

    def test_a_pr_gh_cannot_show_is_left_as_it_was(self):   # the port says why (the source said "changed meanwhile")
        f = self.fake(fail=('view',))
        code, out = self.update(f)
        self.assertEqual((code, f.order(), f.armed), (1, [], self.c))
        self.assertIn('gh cannot show it now', out)
        self.assertNotIn('changed meanwhile', out)

    def test_rerun_after_any_stop_touches_nothing(self):
        # stopped after the disarm (before or after update-branch): no longer armed, so left alone
        for f in (self.fake(armed=False), self.fake(armed=False, behind=False)):
            code, out = self.update(f)
            self.assertEqual((code, f.order(), f.armed), (0, [], None))
        # stopped after the re-arm, or a second run right after a finished one: up to date, armed at up, left alone
        f = self.fake()
        self.update(f)
        f.calls.clear()
        code, out = self.update(f)
        self.assertEqual((code, f.order(), f.armed), (0, [], self.up))

    def test_dry_run_changes_nothing(self):
        f = self.fake()
        code, out = self.update(f, dry=True)
        self.assertEqual((code, f.order(), f.armed), (0, [], self.c))
        self.assertFalse(os.path.exists(self.q.cache), 'no fetch, so no git cache made')
        self.assertIn('would fetch', out)

    def test_dry_run_leaves_an_existing_cache_as_it_was(self):   # a real run may use it at the same time
        self.w.set('refs/pull/7/head', self.c)
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.c)[0])
        cache = self.q.cache
        snap = lambda: sorted((os.path.relpath(os.path.join(d, x), cache), os.path.getmtime(os.path.join(d, x)))
                              for d, _, files in os.walk(cache) for x in files)
        before = snap()
        self.w.set('refs/heads/main', self.up)   # GitHub moved on: a fetch would write new objects and refs
        self.update(self.fake(), dry=True)
        self.assertEqual(snap(), before)

    def test_the_read_head_itself_is_the_same_change(self):   # recover: the run died before update-branch
        self.assertTrue(self.q.same_change('main', 7, self.c, self.c))
        self.assertFalse(self.q.same_change('main', 7, self.c, self.c[:7]), 'only full shas')


class ReviewFlags(Base):   # issue #56: gh pr view prints includesCreatedEdit false for every review
    def lens_review(self, sha, n=1):
        return gh_review(f'{LENS}: correctness · reviewer: x · read at: {sha}\nfindings: none', '2026-11-07T10:00:00Z',
                         n)

    def test_a_review_read_unedited_by_the_flags_query_rearms(self):
        r = self.lens_review(self.c)
        f = self.fake(reviews=[r], flags=[flags_page(r)])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        query = [a for a in f.calls if QueueGh.kind(a) == 'api graphql']
        self.assertEqual(len(query), 1)
        self.assertIn('number=7', query[0])
        self.assertLess(f.calls.index(next(a for a in f.calls if QueueGh.kind(a) == 'view')), f.calls.index(query[0]),
                        'the flags are read after the view')

    def test_an_edited_review_is_not_trusted_although_gh_says_unedited(self):
        r = self.lens_review(self.c)
        f = self.fake(reviews=[r], flags=[flags_page(r, edited=True)])
        code, out = self.update(f)
        self.assertIsNone(f.armed, out)
        self.assertNotIn('arm', f.order())
        self.assertIn(lens.EDITED.split(':')[0], ' '.join(f.comments))

    def test_a_failed_flags_read_changes_nothing(self):   # unknown: never decide; the next run tries again
        r = self.lens_review(self.c)
        for answer in (failed('HTTP 502'), ok('not json'), ok('{"data": {"repository": {"pullRequest": null}}}'),
                       ok(json.dumps({'errors': [{'message': 'x'}], 'data': {'repository': {'pullRequest': {
                           'reviews': {'pageInfo': {'hasNextPage': False}, 'nodes': []}}}}}))):
            with self.subTest(answer=answer):
                f = self.fake(reviews=[r], flags=[answer])
                code, out = self.update(f)
                self.assertEqual((code, f.order(), f.reads(), f.armed, f.comments), (1, [], 1, self.c, []), out)
                self.assertIn('nothing changed', out)
                self.assertFalse(self.plan_exists())

    def test_a_pr_gh_lists_no_review_for_is_read_all_the_same(self):   # an inline comment shows only in that read
        f = self.fake(comments=[lens_at('correctness', self.c)])
        self.assertEqual(self.update(f)[0], 0)
        self.assertEqual((f.reads(), f.armed), (1, self.up))

    def test_a_stopped_run_rereads_the_flags_before_it_rearms(self):   # STATE from a run killed after the disarm
        r = self.lens_review(self.c)
        f = self.fake(reviews=[r], flags=[flags_page(r)], kill_at='update')
        with self.assertRaises(Killed):
            self.update(f)
        self.assertTrue(self.plan_exists())
        f.kill_at, f.flags = None, [failed('HTTP 502')]   # the next run cannot read the flags: nothing is decided
        code, out = run_quiet(self.q.recover)
        self.assertIsNone(f.armed, out)
        self.assertNotIn('arm', f.order())
        f.flags = [flags_page(r)]
        f.comments.clear()
        code, out = run_quiet(self.q.recover)
        self.assertEqual(f.armed, self.c, out)


class ReviewFlagsQuery(unittest.TestCase):
    """review_flags alone: every page, by PR, and any doubt is a failure."""
    def read(self, *answers):
        calls, answers = [], list(answers)

        def gh(*a):
            calls.append(a)
            return answers.pop(0) if answers else failed('no more pages')
        flags, self.inline, why = Q.review_flags(gh, REPO, 7)
        self.assertEqual(flags is None, self.inline is None, 'flags and inline comments are read together or not at all')
        return (flags, why), calls

    def test_more_than_100_reviews_are_read_page_by_page(self):
        reviews = [gh_review('x', 't', n) for n in range(1, 251)]
        (flags, why), calls = self.read(flags_page(*reviews[:100], more='c1'), flags_page(*reviews[100:200], more='c2'),
                                        flags_page(*reviews[200:]))
        self.assertIsNone(why)
        self.assertEqual(set(flags), {r['id'] for r in reviews})
        self.assertEqual([next((x for x in c if x.startswith('after=')), None) for c in calls],
                         [None, 'after=c1', 'after=c2'])
        self.assertTrue(all('owner=owner' in c and 'name=proj' in c and 'number=7' in c for c in calls))

    def test_any_page_that_fails_fails_the_whole_read(self):
        r = gh_review('x', 't')
        for second in (failed('HTTP 502'), ok('{}'), ok('[]'), ok('null')):
            with self.subTest(second=second):
                (flags, why), _ = self.read(flags_page(r, more='c1'), second)
                self.assertIsNone(flags)
                self.assertTrue(why)

    def test_a_next_page_without_a_new_cursor_fails(self):
        r = gh_review('x', 't')
        for cursor in ('', 'c1'):   # GitHub repeats the token: stop at once, not after REVIEW_PAGES calls
            pages = [flags_page(r, more='c1')] + [flags_page(r, more=cursor)] * Q.REVIEW_PAGES
            (flags, why), calls = self.read(*pages)
            self.assertIsNone(flags, cursor)
            self.assertIn('no next page', why)
            self.assertEqual(len(calls), 2, cursor)
        (flags, why), _ = self.read(flags_page(r, more=''))
        self.assertIsNone(flags)

    def test_a_next_page_with_a_null_cursor_fails(self):   # endCursor is nullable: never read as the last page
        r = gh_review('x', 't')
        for pages in ([flags_page(r, more=NO_CURSOR)], [flags_page(r, more='c1'), flags_page(r, more=NO_CURSOR)]):
            (flags, why), calls = self.read(*pages)
            self.assertIsNone(flags, len(pages))
            self.assertIn('no next page', why)
            self.assertEqual(len(calls), len(pages))

    def test_more_pages_than_the_limit_fail(self):
        r = gh_review('x', 't')
        pages = [flags_page(r, more=f'c{i}') for i in range(Q.REVIEW_PAGES + 1)]
        (flags, why), calls = self.read(*pages)
        self.assertIsNone(flags)
        self.assertEqual(len(calls), Q.REVIEW_PAGES)
        self.assertIn('not all read', why)

    def test_a_node_without_both_flags_is_left_out(self):   # lens.Review then counts it as read nothing
        good, bad = gh_review('x', 't', 1), gh_review('x', 't', 2)
        page = json.loads(flags_page(good, bad).stdout)
        page['data']['repository']['pullRequest']['reviews']['nodes'][1].pop('includesCreatedEdit')
        (flags, why), _ = self.read(ok(json.dumps(page)))
        self.assertEqual((set(flags), why), ({'PRR_1'}, None))


class Main(Base):
    def main(self, fake, dry=False, q=None):
        q = q or self.q
        q.gh = q.gh_write = fake
        return run_quiet(q.main, dry)

    @staticmethod
    def listed(n=7, state='BEHIND', armed=True, rollup=()):
        return {'number': n, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'mergeMethod': 'SQUASH'} if armed else None,
                'mergeStateStatus': state, 'createdAt': f'2026-11-07T00:00:0{n % 10}Z', 'statusCheckRollup': list(rollup),
                'headRefOid': 'ab' * 20, 'baseRefName': 'main'}

    @staticmethod
    def listing(fake, prs):
        def gh(*a):
            if a[:2] == ('pr', 'list'):
                fake.calls.append(a)
                return ok(json.dumps(prs))
            return fake(*a)
        return gh

    def test_the_oldest_behind_pr_goes_through_update(self):
        f = self.fake()
        code, out = self.main(self.listing(f, [self.listed()]))
        self.assertEqual((code, f.armed), (0, self.up), out)

    def test_a_rerun_after_a_left_off_pr_moves_on(self):   # the disarmed PR is skipped, it does not stall the queue
        code, out = self.main(self.listing(self.fake(), [self.listed(3, armed=False), self.listed(4, 'CLEAN')]))
        self.assertIn('#4 is up to date', out)

    def test_an_empty_queue_does_nothing(self):
        f = self.fake()
        code, out = self.main(self.listing(f, []))
        self.assertEqual((code, f.order(), f.armed), (0, ['list'], self.c), out)
        self.assertIn('queue empty', out)
        code, out = self.main(self.listing(f, [self.listed(3, armed=False), dict(self.listed(4), isDraft=True)]))
        self.assertEqual(code, 0)
        self.assertIn('queue empty', out, 'no armed, ready PR: nothing to do')

    def test_a_failed_or_odd_listing_fails_the_run(self):
        for r in (failed('HTTP 502'), ok('{"not": "a list"}'), ok('not json')):
            with self.subTest(r=r):
                code, out = self.main(lambda *a: r)
                self.assertEqual(code, 1, out)
                self.assertIn('gh pr list failed', out)

    def test_a_full_listing_warns_that_old_prs_may_be_missing(self):   # GitHub lists the newest PRs first
        q = make_queue(self.w.dir, config.parse(json.dumps({**CFG_RAW, 'list_limit': 2})), remote=self.w.remote)
        f = self.fake()
        code, out = self.main(self.listing(f, [self.listed(3, armed=False), self.listed(4, armed=False)]), q=q)
        self.assertEqual(code, 0)
        self.assertIn('WARNING: gh listed 2 open PRs, the list_limit', out)
        list_call = next(a for a in f.calls if QueueGh.kind(a) == 'list')
        self.assertEqual(list_call[list_call.index('--limit') + 1], '2')
        code, out = self.main(self.listing(f, [self.listed(3, armed=False)]), q=q)
        self.assertNotIn('WARNING', out)

    def test_a_dry_run_takes_no_lock_and_shows_a_stopped_plan(self):   # it reads only: it never holds up a real run
        self.q.write_state({'repo': REPO, 'pr': 5, 'old': self.c, 'base': 'main', 'read': self.c})
        os.makedirs(os.path.dirname(self.q.lock), exist_ok=True)
        with open(self.q.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)   # a real run is going
            f = self.fake()
            code, out = self.main(self.listing(f, [self.listed()]), dry=True)
        self.assertEqual((code, f.order(), f.armed), (0, ['list'], self.c), out)
        self.assertIn('a real run finishes this first', out)
        self.assertIn('#7: would fetch', out)
        self.assertTrue(self.plan_exists(), 'the plan is shown, not finished or removed')
        self.assertFalse(os.path.exists(self.q.cache))

    def test_a_merge_state_the_queue_does_not_handle_is_said(self):   # skipped, never silently (DIRTY: Conflict)
        f = self.fake()
        code, out = self.main(self.listing(f, [self.listed(3, 'DRAFT')]))
        self.assertEqual((code, f.order(), f.armed), (0, ['list'], self.c), out)
        self.assertIn('skip #3: merge state DRAFT', out)

    def test_a_held_lock_runs_nothing(self):   # two runs at once (a push and the timer): the second does nothing
        os.makedirs(os.path.dirname(self.q.lock), exist_ok=True)
        with open(self.q.lock, 'w') as held:
            fcntl.flock(held, fcntl.LOCK_EX)
            f = self.fake()
            code, out = self.main(self.listing(f, [self.listed()]))
        self.assertEqual((code, f.calls), (0, []))
        self.assertIn('another pr_queue run', out)


class ReviewedHead(unittest.TestCase):   # which head the queue keeps
    def test_one_sha_for_every_lens(self):
        pr = {'headRefOid': H2, 'commits': [{'oid': H1}, {'oid': H2}],
              'comments': [lens_at('correctness', H1, 't1'), lens_at('data-integrity', H1, 't2')]}
        self.assertEqual(REVIEW.reviewed_head(pr)[:2], (True, H1), 'an earlier commit of the PR')
        for short in (7, 12, 39):   # even of the head itself: a force-push can bring a commit with the same prefix
            pr = {'headRefOid': H1, 'comments': [lens_at('correctness', H1[:short], 't1'), lens_at('di', H1, 't2')]}
            self.assertEqual(REVIEW.reviewed_head(pr)[:2], (True, None), short)

    def test_lenses_that_read_different_heads_give_no_head(self):
        pr = {'headRefOid': H2, 'commits': [{'oid': H1}, {'oid': H2}],
              'comments': [lens_at('correctness', H1, 't1'), lens_at('data-integrity', H2, 't2')]}
        reviewed, sha, why = REVIEW.reviewed_head(pr)
        self.assertEqual((reviewed, sha), (True, None))
        self.assertIn('did not all read one head', why)

    def test_an_override_covers_only_the_lenses_older_than_it(self):
        lenses = [lens_at('correctness', H1, '2026-11-07T10:00:00Z'), lens_at('di', H1, '2026-11-07T10:00:01Z')]
        pr = {'headRefOid': H2, 'commits': [{'oid': H1}, {'oid': H2}], 'comments': lenses + [override(H2, '2026-11-07T11:00:00Z')]}
        self.assertEqual(REVIEW.reviewed_head(pr)[:2], (True, H2))
        pr['comments'].append(lens_at('correctness r2', H2, '2026-11-07T12:00:00Z'))   # a re-check after it
        self.assertEqual(REVIEW.reviewed_head(pr)[:2], (True, H2), 'correctness and the override say H2: one head')
        pr['comments'][-1] = lens_at('correctness r2', H3, '2026-11-07T12:00:00Z')
        self.assertEqual(REVIEW.reviewed_head(pr)[1], None, 'a newer lens read of another head is not covered')

    def test_no_lens_comments_is_not_reviewed(self):
        self.assertEqual(REVIEW.reviewed_head({'comments': [override(H2, 't')]})[:2], (False, None))

    def test_an_override_quoted_or_by_a_bot_does_not_count(self):
        base = [lens_at('correctness', H1, '2026-11-07T10:00:00Z')]
        quoted = note('> ' + override(H2, 'x')['body'], '2026-11-07T11:00:00Z')
        bot = dict(override(H2, '2026-11-07T11:00:00Z'), author={'login': 'review-bot'})
        for extra in (quoted, bot):
            self.assertEqual(REVIEW.reviewed_head({'headRefOid': H1, 'comments': base + [extra]})[1], H1)


class QueueKeepsTheReadChange(Base):
    def test_lenses_on_different_heads_stay_off_and_are_not_updated(self):
        other = self.w.commit({'x.py': lines(extra=['other'], at=20)}, [self.m0])
        f = self.fake(comments=[lens_at('correctness', self.c, 't1'), lens_at('di', other, 't2')])
        code, out = self.update(f)
        self.assertEqual((f.armed, f.updated), (None, False), out)
        self.assertIn('did not all read one head', f.comments[0])
        self.assertNotRegex(f.comments[0], READ_AT)

    def test_the_head_of_an_override_is_re_armed_after_a_clean_update(self):
        f = self.fake(comments=[lens_at('correctness', self.m0, '2026-11-07T10:00:00Z'),
                                override(self.c, '2026-11-07T11:00:00Z')])
        code, out = self.update(f)
        self.assertEqual((code, f.armed), (0, self.up), out)

    def test_a_second_update_keeps_the_change_read_two_merges_ago(self):   # lenses read C; the queue already merged M1
        m2 = self.w.commit({'x.py': lines(extra=['main 1', 'main 2', 'main 3'], at=0)}, [self.m1])
        self.w.set('refs/heads/main', m2)
        self.up2 = self.w.merge(self.up, m2)
        f = self.fake('up2', h1=self.up, comments=[lens_at('correctness', self.c, 't1'), lens_at('di', self.c, 't2')])
        code, out = self.update(f)
        self.assertEqual((code, f.armed), (0, self.up2), out + str(f.comments))

    def test_no_state_is_left_after_a_finished_update(self):
        for f in (self.fake(), self.fake('resolved'), self.fake(fail=('update',))):
            self.update(f)
            self.assertFalse(self.plan_exists())


class Recover(Base):   # a run killed after its disarm
    def recover(self, fake, state, q=None):
        q = q or self.q
        Q.write_text(q.state, state if isinstance(state, str) else json.dumps(state))   # as the queue writes it
        q.gh = q.gh_write = fake
        return run_quiet(q.recover)

    def plan(self, **kw):
        return dict({'repo': REPO, 'pr': 7, 'old': self.c, 'base': 'main', 'read': self.c}, **kw)

    def saved_plan(self):
        with open(self.q.state) as s:
            return json.load(s)

    def test_armed_again_or_closed_needs_nothing(self):
        f = self.fake()   # still armed: the run was killed before its disarm, or someone re-armed it
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, [f.kind(c) for c in f.calls], f.armed), (0, ['view', 'api graphql'], self.c))
        self.assertFalse(self.plan_exists())

    def test_off_at_the_same_change_is_re_armed_for_that_head(self):
        f = self.fake(armed=False)   # killed before update-branch: the head is still the read commit
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, f.armed, f.comments), (0, self.c, []), out)
        f = self.fake(armed=False, h1=self.up)   # killed after update-branch
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)

    def test_a_plan_without_repo_from_the_source_queue_is_finished(self):   # the source project's STATE format
        legacy = {k: v for k, v in self.plan().items() if k != 'repo'}
        f = self.fake(armed=False, h1=self.up)
        code, out = self.recover(f, legacy)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertFalse(self.plan_exists())

    def test_a_plan_of_another_repository_is_refused_and_touches_nothing(self):   # two queues share one --state-file
        theirs = self.plan(repo='someone/else')
        f = self.fake(armed=False, h1=self.up)
        code, out = self.recover(f, theirs)
        self.assertEqual((code, f.calls, f.armed), (1, [], None), out)
        self.assertIn('ERROR', out)
        self.assertIn('someone/else', out)
        self.assertEqual(self.saved_plan(), theirs, 'that queue still needs its plan')
        f.calls.clear()
        code, out = Main.main(self, Main.listing(f, [Main.listed()]))
        self.assertEqual((code, f.calls), (1, []), 'the run ends: no PR of this repository either')

    def test_off_at_another_change_or_without_a_plan_says_so(self):
        for h1, plan in ((self.resolved, self.plan()), (self.c, self.plan(read=None)), (self.c, self.plan(read=self.c[:7]))):
            f = self.fake(armed=False, h1=h1)
            code, out = self.recover(f, plan)
            self.assertEqual((f.armed, len(f.comments)), (None, 1), (plan, out))
            self.assertIn('stopped half-way', f.comments[0])
            self.assertFalse(self.plan_exists())

    def test_a_lens_posted_after_the_stop_counts(self):   # recover reads the review again, not the stored read
        c1 = self.w.commit({'x.py': lines(extra=['new 1'], at=20)}, [self.m0])
        c2 = self.w.commit({'x.py': lines(extra=['new 1', 'new 2'], at=20)}, [c1])
        up2 = self.w.merge(c2, self.m1)
        before = [lens_at('correctness', c2, '2026-11-07T10:00:00Z'), lens_at('di', c2, '2026-11-07T10:00:01Z')]
        # the stopped run read c2; since then both lenses re-read and vouch only for the older c1
        after = [lens_at('correctness r2', c1, '2026-11-07T12:00:00Z'), lens_at('di r2', c1, '2026-11-07T12:00:01Z')]
        f = self.fake(armed=False, h1=up2, comments=before + after)
        code, out = self.recover(f, self.plan(old=c2, read=c2))
        self.assertEqual((f.armed, len(f.comments)), (None, 1), out)
        self.assertIn(c1[:12], f.comments[0], 'the read the lenses vouch for now')
        self.assertFalse(self.plan_exists())
        # lenses that now disagree: no head is vouched for, so it stays OFF and says why
        f = self.fake(armed=False, h1=up2, comments=before + after[:1])
        code, out = self.recover(f, self.plan(old=c2, read=c2))
        self.assertEqual((f.armed, len(f.comments)), (None, 1), out)
        self.assertIn('did not all read one head', f.comments[0])
        self.assertNotRegex(f.comments[0], READ_AT)
        # every lens re-read the updated head itself: that head is re-armed
        f = self.fake(armed=False, h1=up2, comments=before + [lens_at('correctness r2', up2, '2026-11-07T12:00:00Z'),
                                                             lens_at('di r2', up2, '2026-11-07T12:00:01Z')])
        code, out = self.recover(f, self.plan(old=c2, read=c2))
        self.assertEqual((f.armed, f.comments), (up2, []), out)

    def test_a_lens_comment_edited_after_the_stop_reads_nothing_until_that_lens_posts_again(self):
        lens_comment = lens_at('correctness', self.c, '2026-11-07T10:00:00Z')
        lens_comment['includesCreatedEdit'] = True   # GitHub says it changed, not what: `read at` may name another head
        f = self.fake(armed=False, h1=self.up, comments=[lens_comment])
        code, out = self.recover(f, self.plan())
        self.assertEqual((f.armed, len(f.comments)), (None, 1), out)
        self.assertIn('edited after posting', f.comments[0])
        self.assertNotRegex(f.comments[0], READ_AT)
        f = self.fake(armed=False, h1=self.up, comments=[lens_comment, lens_at('correctness', self.c, '2026-11-07T11:00Z')])
        code, out = self.recover(f, self.plan())
        self.assertEqual((f.armed, f.comments), (self.up, []), 'a new comment of that lens: ' + out)

    def test_a_gh_error_keeps_the_state_and_says_so_once(self):
        f = self.fake(armed=False, fail=('view',))
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, len(f.comments)), (1, 1))
        self.assertTrue(self.plan_exists())
        code, out = self.recover(f, self.saved_plan())
        self.assertEqual((code, len(f.comments)), (1, 1), 'a second failing run does not comment again')
        self.assertNotRegex(f.comments[0], READ_AT)

    def test_a_fetch_error_that_does_not_heal_says_so_once(self):   # never OFF and silent
        self.q.remote = os.path.join(self.w.dir, 'no-such-repo.git')
        f = self.fake(armed=False, h1=self.up)   # killed after update-branch: the new head must be fetched
        for _ in range(3):
            code, out = self.recover(f, self.saved_plan() if self.plan_exists() else self.plan())
            self.assertEqual((code, f.armed, len(f.comments)), (1, None, 1), out)
        self.assertTrue(self.plan_exists())
        self.assertNotRegex(f.comments[0], READ_AT)

    def test_a_stale_ref_lock_in_the_cache_is_removed(self):   # a git fetch killed half-way
        self.w.set('refs/pull/7/head', self.c)
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.c)[0])
        open(os.path.join(self.q.cache, 'refs', 'queue', 'pr7.lock'), 'w').close()
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.up)[0])

    def stuck_folder(self, path):
        """`path` with a file inside that cannot be deleted (its folder is read-only); made writable again at the end."""
        if os.geteuid() == 0:
            self.skipTest('root deletes a file in a read-only folder: no undeletable file to test with')
        stuck = os.path.join(path, 'stuck')
        os.makedirs(stuck)
        open(os.path.join(stuck, 'f'), 'w').close()
        os.chmod(stuck, 0o555)
        self.addCleanup(lambda: [os.chmod(os.path.join(d, 'stuck'), 0o755) for d in self.leftovers() + [path]
                                 if os.path.isdir(os.path.join(d, 'stuck'))])

    def leftovers(self):
        parent, name = os.path.split(self.q.cache)
        return [os.path.join(parent, f) for f in os.listdir(parent) if f.startswith(name + '.') and '.old-' in f]

    def fetch(self, sha):
        return run_quiet(self.q.fetch, 'main', 7, sha)

    def test_a_broken_cache_with_an_undeletable_file_is_set_aside(self):
        self.w.set('refs/pull/7/head', self.c)
        self.stuck_folder(self.q.cache)   # not a bare git repo
        got, out = self.fetch(self.c)
        self.assertTrue(got, out)
        self.assertIn(': rebuilding it', out)
        self.assertIn('could not delete all of', out, 'the part left behind is said, not silent')
        self.assertEqual(len(self.leftovers()), 1)
        self.assertTrue(self.fetch(self.c)[0], 'the next run uses the new cache')

    def test_a_half_made_cache_with_an_undeletable_file_is_set_aside(self):   # a `git init` killed half-way
        self.w.set('refs/pull/7/head', self.c)
        self.stuck_folder(self.q.cache + '.new')
        got, out = self.fetch(self.c)
        self.assertTrue(got, out)
        self.assertIn('could not delete all of', out)
        self.assertEqual(len(self.leftovers()), 1)

    def test_leftovers_are_removed_by_the_next_rebuild(self):   # a stop between the move and the delete
        os.makedirs(self.q.cache + '.old-1/objects')
        os.makedirs(self.q.cache + '.new.old-2/objects')
        os.makedirs(self.q.cache)   # broken: empty
        self.w.set('refs/pull/7/head', self.c)
        got, out = self.fetch(self.c)
        self.assertEqual((got, self.leftovers()), (True, []), out)

    def test_a_cache_that_cannot_be_moved_aside_is_said_and_nothing_is_known(self):   # fetch None: nothing changes
        if os.geteuid() == 0:
            self.skipTest('root renames in a read-only folder')
        ro = os.path.join(self.w.dir, 'ro')
        self.q.cache = os.path.join(ro, 'cache.git')
        os.makedirs(self.q.cache)
        os.chmod(ro, 0o555)
        self.addCleanup(os.chmod, ro, 0o755)
        got, out = self.fetch(self.c)
        self.assertIsNone(got, out)
        self.assertIn('could not move', out)

    def test_the_plan_is_this_users_alone(self):   # STATE decides what is re-armed
        self.assertEqual(self.update(self.fake(after=None))[0], Q.SILENT)
        self.assertEqual(os.stat(self.q.state).st_mode & 0o777, 0o600)

    def test_a_leftover_temp_file_does_not_give_the_plan_its_mode(self):   # write_text's fchmod
        os.makedirs(os.path.dirname(self.q.state), mode=0o700, exist_ok=True)
        with open(self.q.state + '.tmp', 'w') as f:
            f.write('half a plan')
        os.chmod(self.q.state + '.tmp', 0o666)
        Q.write_text(self.q.state, json.dumps(self.plan()))
        self.assertEqual(os.stat(self.q.state).st_mode & 0o777, 0o600)

    def test_a_link_at_the_temp_file_is_never_written_through(self):   # write_text's O_NOFOLLOW
        os.makedirs(os.path.dirname(self.q.state), mode=0o700, exist_ok=True)
        target = os.path.join(self.w.dir, 'elsewhere')
        with open(target, 'w') as f:
            f.write('keep')
        os.symlink(target, self.q.state + '.tmp')
        with self.assertRaises(OSError):
            Q.write_text(self.q.state, json.dumps(self.plan()))
        with open(target) as f:
            self.assertEqual(f.read(), 'keep')
        self.assertFalse(self.plan_exists())

    def test_a_link_at_the_plan_is_kept_aside_and_not_followed(self):   # recover's O_NOFOLLOW
        target = os.path.join(self.w.dir, 'a-plan-elsewhere')
        Q.write_text(target, json.dumps(self.plan()))   # this user's, 0600: followed, it would be trusted
        os.makedirs(os.path.dirname(self.q.state), mode=0o700, exist_ok=True)
        os.symlink(target, self.q.state)
        f = self.fake(armed=False, h1=self.up)
        self.q.gh = self.q.gh_write = f
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.calls, f.armed), (1, [], None), out)
        self.assertIn('ERROR: unreadable', out)
        self.assertTrue(os.path.islink(self.q.state + '.bad'))
        with open(target) as f:
            self.assertEqual(json.load(f), self.plan())

    def test_a_plan_another_user_could_write_is_kept_aside_and_not_followed(self):
        f = self.fake(armed=False, h1=self.up)
        Q.write_text(self.q.state, json.dumps(self.plan()))
        os.chmod(self.q.state, 0o664)   # group-writable: anyone in the group could have written this plan
        self.q.gh = self.q.gh_write = f
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.calls, f.armed), (1, [], None), out)
        self.assertIn('ERROR: untrusted', out)
        self.assertTrue(os.path.exists(self.q.state + '.bad'))
        self.assertFalse(self.plan_exists())

    def test_the_base_comes_from_github_not_from_the_plan(self):   # retargeted since the stop, or a wrong plan
        release = self.w.commit({'x.py': lines(extra=['release'], at=25)}, [self.m0])
        self.w.set('refs/heads/release', release)
        f = self.fake(armed=False, h1=self.up)
        view = f.view
        f.view = lambda: dict(view(), baseRefName='release')   # GitHub: the PR now merges into release
        code, out = self.recover(f, self.plan())
        self.assertEqual((f.armed, len(f.comments)), (None, 1), 'proved against main, merging into release: ' + out)
        f = self.fake(armed=False, h1=self.up)
        code, out = self.recover(f, self.plan(base='release'))
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)

    def test_a_failed_comment_keeps_the_state(self):
        f = self.fake(armed=False, h1=self.resolved, fail=('comment',))
        self.assertEqual(self.recover(f, self.plan())[0], Q.SILENT)
        self.assertTrue(self.plan_exists())

    def test_an_unfinished_recover_ends_the_run(self):   # no other PR may overwrite the STATE
        f = self.fake(armed=False, fail=('view',))
        self.q.write_state(self.plan())
        self.q.gh = self.q.gh_write = f
        self.assertEqual(run_quiet(self.q.main)[0], 1)
        self.assertNotIn('list', f.order())
        self.assertTrue(self.plan_exists())

    def test_an_unreadable_state_is_kept_aside_and_never_silent(self):   # which PR it was is lost
        bad = self.q.state + '.bad'
        for junk in ('{', '[]', '{"pr": "7"}', ''):   # '': a file a reboot cut short
            f = self.fake()
            code, out = self.recover(f, junk)
            self.assertEqual((code, f.calls), (1, []), 'this run is red and touches no PR')
            self.assertIn('ERROR: unreadable', out)
            self.assertFalse(self.plan_exists())
            with open(bad) as b:
                self.assertEqual(b.read(), junk)
        self.q.gh = self.q.gh_write = self.fake()
        code, out = run_quiet(self.q.recover)
        self.assertEqual(code, 0, 'the next runs go on')
        self.assertIn('WARNING: ' + bad, out, 'and say it every run while the file is there')

    def test_the_plan_survives_a_new_process_and_an_emptied_lock_folder(self):   # STATE is its own file
        f = self.fake(after=None)   # the run stops with the PR OFF: GitHub did not show the new head in time
        self.assertEqual(self.update(f)[0], Q.SILENT)
        shutil.rmtree(os.path.dirname(self.q.lock), ignore_errors=True)   # e.g. a lock in /tmp, gone after a reboot
        q = make_queue(self.w.dir, remote=self.w.remote)   # the next run: a new process, the same --state-file
        f.head = self.up
        q.gh = q.gh_write = f
        code, out = run_quiet(q.main)
        self.assertEqual(code, 0, out)
        self.assertEqual((f.armed, f.comments), (self.up, []), 'the next run finishes the PR')
        self.assertFalse(self.plan_exists())


class ObjectsCheckInAnUpdate(Recover):   # #457 fix round: the cache check in a real run
    def plant_corrupt(self, name):
        """A corrupt loose object under `name` in the cache: what an earlier job could leave there."""
        os.makedirs(os.path.join(self.q.cache, 'objects', name[:2]), exist_ok=True)
        with open(os.path.join(self.q.cache, 'objects', name[:2], name[2:]), 'wb') as f:
            f.write(zlib.compress(b'blob 5\0evil\n'))

    def test_the_check_runs_while_github_updates_the_branch(self):   # state lens cost: overlaps the update and wait
        f, seen, foreground = self.fake(polls=2), {}, []
        git = self.q.git

        def counted(*a, **kw):
            if 'fsck' in a:
                foreground.append(a)
            return git(*a, **kw)
        self.q.git = counted

        def gh(*a):
            if QueueGh.kind(a) == 'update':
                seen['check'] = self.q.later
            return f(*a)
        code, out = self.update(gh)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertIsInstance(seen.get('check'), subprocess.Popen, 'started before the branch update')
        self.assertEqual(foreground, [], 'checked once, in the background')
        self.assertIsNone(self.q.later)

    def test_a_run_that_ends_before_the_merge_check_leaves_no_check_running(self):
        procs, real = [], subprocess.Popen

        def popen(*a, **kw):   # subprocess.run's own Popen too: only the check counts
            p = real(*a, **kw)
            if 'fsck' in a[0]:
                procs.append(p)
            return p
        with unittest.mock.patch.object(Q.subprocess, 'Popen', popen):
            code, out = self.update(self.fake(after=None))   # no new head in time: SILENT before the merge check
            self.assertEqual(code, Q.SILENT, out)
            with self.assertRaises(Killed):   # killed while it waits for the new head
                self.update(self.fake(kill_at='poll'))
        self.assertEqual(len(procs), 2)
        self.assertEqual([p.poll() is None for p in procs], [False, False], 'stopped and reaped')
        self.assertIsNone(self.q.later)

    def test_a_broken_object_found_while_github_updates_is_rebuilt_before_the_merge_check(self):
        f = self.fake()
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.c)[0])   # an earlier run's cache
        self.plant_corrupt(self.up)   # the name update-branch's head will have
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertIn('rebuilding it', out)

    def test_the_background_checks_answer_is_read_before_the_merge_check(self):   # no fetch trips over this one
        f, fscks = self.fake(), []
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.c)[0])   # an earlier run's cache
        git = self.q.git

        def counted(*a, **kw):
            if 'fsck' in a:
                fscks.append(a)
            return git(*a, **kw)
        self.q.git = counted
        self.plant_corrupt('ab' * 20)   # a name no fetch needs: only git fsck sees it
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertIn('rebuilding it', out)
        self.assertEqual(len(fscks), 1, 'the background answer, then only the rebuilt cache checked in the foreground')

    def test_a_fetch_a_broken_object_fails_in_recover_is_rebuilt(self):   # trust lens 1: never stuck
        self.fake()
        self.assertTrue(run_quiet(self.q.fetch, 'main', 7, self.c)[0])   # an earlier run's cache
        self.plant_corrupt(self.up)
        f = self.fake(armed=False, h1=self.up)   # killed after update-branch: the new head must be fetched
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertIn('rebuilding it', out)

    def test_an_object_written_while_github_updates_is_never_merged_in(self):   # #465 item 1
        """The background check has passed. While GitHub updates the branch, another process writes main's tree
        under the name of the tree of the change read, and the new head drops the change: merge-tree in the cache
        calls that head the change read merged with main."""
        w = self.w
        tree = w.git(w.remote, 'rev-parse', self.m1 + '^{tree}')
        self.lost = w.keep(w.git(w.work, 'commit-tree', tree, '-p', self.c, '-p', self.m1, '-m', 'lost'))
        f = self.fake(after='lost')

        def gh(*a):
            if QueueGh.kind(a) == 'update':
                self.assertEqual(self.q.later.wait(), 0, 'the background check found the cache healthy')
                victim = w.git(w.remote, 'rev-parse', self.c + '^{tree}')
                body = subprocess.run(['git', '--git-dir', self.q.cache, 'cat-file', 'tree', tree],
                                      capture_output=True, check=True).stdout
                path = os.path.join(self.q.cache, 'objects', victim[:2], victim[2:])
                self.assertTrue(os.path.exists(path), 'a small fetch leaves loose objects')
                os.chmod(path, 0o644)
                with open(path, 'wb') as o:
                    o.write(zlib.compress(b'tree %d\0' % len(body) + body))
                self.assertEqual(Q.merge_tree(self.q.git, self.m1, self.c).stdout.split()[:1], [tree],
                                 'the planted tree does not make merge-tree drop the change: the test reads nothing')
            return f(*a)
        code, out = self.update(gh)
        self.assertEqual((f.armed, len(f.comments)), (None, 1), out)   # left OFF, said why, in the same run
        self.assertIn('is not exactly the change read', f.comments[0])
        self.assertIn('although git fsck passed', out)
        self.assertIn('rebuilding it', out)
        self.assertFalse(self.plan_exists(), 'answered: nothing is left for the next run')
        self.assertFalse(os.path.lexists(self.q.cache + Q.MERGE_COPY))

    def test_a_cache_still_broken_says_so_not_could_not_fetch(self):   # state lens 2, trust nit 2: log and PR comment
        self.q.objects_problem = lambda *a: 'has objects git fsck finds broken or missing (planted)'
        f = self.fake()
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (Q.SILENT, None, []), out)
        self.assertIn('fails git fsck', out)
        self.assertNotIn('could not fetch', out)
        g = self.fake(armed=False, h1=self.up)
        for _ in range(2):
            code, out = self.recover(g, self.saved_plan())
            self.assertEqual((code, g.armed, len(g.comments)), (1, None, 1), out)
            self.assertIn('fails git fsck', out)
            self.assertNotIn('could not fetch', out)
        self.assertIn('fails git fsck', g.comments[0])
        self.assertNotIn('could not fetch', g.comments[0])
        self.assertNotIn(self.w.dir, g.comments[0], 'no runner path in a PR comment')


class WhoseCommentsCount(Base):
    """Anyone can comment on a public repository: only comments of the configured lens associations, shown and never
    edited, are lens reads (lens.Review.counts). Everyone else's neither vouch for a head nor block one."""

    def stranger(self, body, association='NONE'):
        return note(body, '2026-11-07T11:00:00Z', login='drive-by', association=association)

    def test_a_strangers_lens_comment_never_moves_the_read_head(self):
        reviewed = self.w.commit({'x.py': lines(extra=['reviewed'], at=20)}, [self.m0])
        # the lens read `reviewed`; a write-access push made the head C (auto-merge stays on); a stranger "reads" C
        for association in ('NONE', 'CONTRIBUTOR', 'FIRST_TIME_CONTRIBUTOR', 'MEMBER'):
            forged = self.stranger(f'{LENS}: correctness · reviewer: x · read at: {self.c}', association)
            f = self.fake(comments=[lens_at('correctness', reviewed), forged])
            code, out = self.update(f)
            self.assertEqual((f.armed, len(f.comments)), (None, 1), f'{association}: {self.c[:12]} re-armed: {out}')

    def test_a_strangers_junk_comment_cannot_disarm_a_reviewed_pr(self):
        for junk in ('lgtm, safe to merge', f'looks fine, read at: {self.c[:8]}'):
            f = self.fake(comments=[lens_at('correctness', self.c), self.stranger(junk)])
            code, out = self.update(f)
            self.assertEqual((code, f.armed, f.comments), (0, self.up, []), junk + out)

    def test_an_edited_lens_comment_does_not_count(self):   # its author may have moved `read at` to an unread head
        f = self.fake(comments=[dict(lens_at('correctness', self.c), includesCreatedEdit=True)])
        code, out = self.update(f)
        self.assertEqual((f.armed, f.updated, len(f.comments)), (None, False, 1), out)
        self.assertIn('edited after posting', f.comments[0])

    def test_a_hidden_lens_comment_reads_nothing(self):
        hidden = dict(lens_at('correctness', self.c, '2026-11-07T11:00:00Z'), isMinimized=True)
        f = self.fake(comments=[lens_at('correctness', self.m0), hidden])
        code, out = self.update(f)
        self.assertIsNone(f.armed, 'the hidden read of C counted: ' + out)

    def test_a_lens_comment_whose_author_does_not_count_is_named_in_the_run_log(self):
        forged = self.stranger(f'{LENS}: correctness · reviewer: x · read at: {self.c}', 'MEMBER')
        code, out = self.update(self.fake(comments=[lens_at('correctness', self.c), forged]))
        self.assertIn('author association MEMBER is not one of lens_author_associations (OWNER, COLLABORATOR)', out)


class Sec2(Base):
    """Security review round 2: hidden comments. Hiding old review rounds as Outdated is routine; it must not make a
    reviewed PR look unreviewed, since the queue vouches for the head of an unreviewed one."""

    def test_all_lens_comments_hidden_as_outdated_does_not_vouch_for_an_unread_push(self):
        reviewed = self.w.commit({'x.py': lines(extra=['reviewed'], at=20)}, [self.m0])
        # the lens read `reviewed`; a write-access push made the head C (auto-merge stays on); round 1 is hidden
        f = self.fake(comments=[dict(lens_at('correctness', reviewed), isMinimized=True)])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.updated, len(f.comments)), (0, None, False, 1), out)
        self.assertIn('hidden', f.comments[0])
        self.assertIn('`tool ready --head <sha>: <why>`', f.comments[0])

    def test_a_hidden_override_does_not_count(self):
        reviewed = self.w.commit({'x.py': lines(extra=['reviewed'], at=20)}, [self.m0])
        hidden = dict(override(self.c, '2026-11-07T11:00:00Z'), isMinimized=True)
        f = self.fake(comments=[lens_at('correctness', reviewed), hidden])
        code, out = self.update(f)
        self.assertEqual((f.armed, len(f.comments)), (None, 1), out)
        self.assertIn('is not exactly the change read', f.comments[0])   # held against the lens's read, not C


class Sec1R2(Base):
    """Security review 1, round 2: an override taken back by hiding it vouches for nothing."""

    def test_a_hidden_override_vouches_for_nothing(self):
        reviewed = self.w.commit({'x.py': lines(extra=['reviewed'], at=20)}, [self.m0])
        shown = override(self.c, '2026-11-07T11:00:00Z')
        f = self.fake(comments=[lens_at('correctness', reviewed), shown])
        self.assertEqual(self.update(f)[0], 0)
        self.assertEqual(f.armed, self.up, 'the shown override vouches for C')
        f = self.fake(comments=[lens_at('correctness', reviewed), dict(shown, isMinimized=True)])
        code, out = self.update(f)
        self.assertIsNone(f.armed, 'the hidden override re-armed C: ' + out)


class OwnCache(Base):
    """git runs in the cache with the token in its environment, and the cache's objects decide what is re-armed: only
    the cache as the queue made it is used, and no hook runs there (a job on the same runner could plant one)."""

    def fetch(self, sha):
        self.w.set('refs/pull/7/head', sha)
        return run_quiet(self.q.fetch, 'main', 7, sha)

    def test_a_new_cache_is_this_users_alone_with_the_queues_config(self):
        self.assertTrue(self.fetch(self.c)[0])
        self.assertEqual(os.stat(self.q.cache).st_mode & 0o777, 0o700)
        with open(os.path.join(self.q.cache, 'config')) as f:
            self.assertEqual(f.read(), Q.CACHE_CONFIG)
        self.assertFalse(os.path.exists(os.path.join(self.q.cache, 'hooks')), 'no sample hooks either')

    def test_a_planted_hook_never_runs(self):
        self.assertTrue(self.fetch(self.c)[0])
        marker = os.path.join(self.w.dir, 'hook-ran')
        os.makedirs(os.path.join(self.q.cache, 'hooks'), exist_ok=True)
        for name in ('reference-transaction', 'post-index-change', 'fsmonitor-watchman'):
            path = os.path.join(self.q.cache, 'hooks', name)
            with open(path, 'w') as h:
                h.write(f'#!/bin/sh\necho {name} >> {shlex.quote(marker)}\n')
            os.chmod(path, 0o755)
        got, out = self.fetch(self.up)   # moves refs/queue/pr7: a ref transaction
        self.assertTrue(got, out)
        self.assertFalse(os.path.exists(marker), 'a hook in the cache ran with the token in its environment')

    def test_a_cache_with_grafts_shallow_or_alternates_is_rebuilt(self):   # each changes what the objects say
        for name in ('info/grafts', 'shallow', 'objects/info/alternates', 'objects/info/http-alternates',
                     'info/attributes'):   # attributes: `* merge=union` makes a conflict merge clean
            self.assertTrue(self.fetch(self.c)[0])
            path = os.path.join(self.q.cache, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'w') as f:
                f.write(f'{self.c} {self.m1}\n' if name in ('info/grafts', 'shallow') else self.w.dir + '\n')
            got, out = self.fetch(self.up)
            self.assertTrue(got, out)
            self.assertIn(f'holds {name}, which the queue never writes: rebuilding it', out)
            self.assertFalse(os.path.lexists(path), name)

    def test_a_cache_with_another_config_or_open_to_others_is_rebuilt(self):
        self.assertTrue(self.fetch(self.c)[0])
        with open(os.path.join(self.q.cache, 'config'), 'a') as f:   # fetch "GitHub" from somewhere else
            f.write(f'[url "{os.path.join(self.w.dir, "elsewhere.git")}"]\n\tinsteadOf = {self.q.remote}\n')
        got, out = self.fetch(self.up)
        self.assertTrue(got, out)
        self.assertIn('has a config the queue did not write: rebuilding it', out)
        for mode in (0o770, 0o755):   # group-writable, or only readable by others: both are rebuilt
            with self.subTest(mode=oct(mode)):
                os.chmod(self.q.cache, mode)
                got, out = self.fetch(self.c)
                self.assertTrue(got, out)
                self.assertIn("is not this user's alone", out)
                self.assertEqual(os.stat(self.q.cache).st_mode & 0o777, 0o700)

    def init_writes(self, head=None, config=None):
        """subprocess.run, where `git init` also writes `head` to HEAD and adds `config` to the config, as another git
        would (git 3.0: HEAD names main; a reftable or SHA-256 default)."""
        real = subprocess.run

        def run(cmd, **kw):
            r = real(cmd, **kw)
            if 'init' in cmd and r.returncode == 0:
                if head is not None:
                    with open(os.path.join(cmd[-1], 'HEAD'), 'w') as f:
                        f.write(head)
                if config is not None:
                    with open(os.path.join(cmd[-1], 'config'), 'a') as f:
                        f.write(config)
            return r
        return unittest.mock.patch.object(subprocess, 'run', run)

    def test_a_git_whose_init_names_another_branch_still_makes_the_queues_head(self):   # #457 item 2: git 3.0, main
        with self.init_writes(head='ref: refs/heads/main\n'):
            for _ in range(3):   # made once, then kept: never rebuilt for a HEAD git chose
                got, out = self.fetch(self.c)
                self.assertTrue(got, out)
                self.assertNotIn('rebuilding', out)
                with open(os.path.join(self.q.cache, 'HEAD')) as f:
                    self.assertEqual(f.read(), Q.CACHE_HEAD)
                self.assertIsNone(self.q.cache_problem())

    def test_a_head_or_config_that_is_a_link_is_never_followed(self):   # #457 item 4: cache_problem's O_NOFOLLOW
        for name, text in (('HEAD', Q.CACHE_HEAD), ('config', Q.CACHE_CONFIG)):
            with self.subTest(name):
                self.assertTrue(self.fetch(self.c)[0])
                target = os.path.join(self.w.dir, name + '-elsewhere')   # what the queue would write, but elsewhere
                with open(target, 'w') as f:
                    f.write(text)
                path = os.path.join(self.q.cache, name)
                os.remove(path)
                os.symlink(target, path)
                self.assertIn('cannot be checked', self.q.cache_problem())
                got, out = self.fetch(self.up)
                self.assertTrue(got, out)
                self.assertIn('cannot be checked', out)
                self.assertIn('rebuilding it', out)
                self.assertFalse(os.path.islink(path))
                got, out = self.fetch(self.up)   # runs again: kept
                self.assertTrue(got, out)
                self.assertNotIn('rebuilding', out)

    def test_the_jobs_default_formats_never_reach_the_cache(self):   # #457 item 3: git 3.0 defaults, or a job's
        envs, real = [], subprocess.run

        def run(cmd, **kw):
            if 'init' in cmd:
                envs.append(kw.get('env'))
            return real(cmd, **kw)
        job = {'GIT_DEFAULT_HASH': 'sha256', 'GIT_DEFAULT_REF_FORMAT': 'reftable'}
        with unittest.mock.patch.dict(os.environ, job), unittest.mock.patch.object(subprocess, 'run', run):
            for _ in range(2):
                got, out = self.fetch(self.c)
                self.assertTrue(got, out)
                self.assertNotIn('rebuilding', out)
                self.assertEqual(self.q.git('rev-parse', '--show-object-format').stdout.strip(), 'sha1')
        self.assertEqual(len(envs), 1, 'made once')
        self.assertEqual({k: envs[0].get(k) for k in job}, Q.GIT_FORMATS)

    def test_a_git_that_makes_another_format_is_refused_clearly(self):   # #457 item 3: refused, never written over
        for extension in ('\n[extensions]\n\trefStorage = reftable\n', '\n[extensions]\n\tobjectFormat = sha256\n'):
            with self.subTest(extension.split()[1]), self.init_writes(config=extension):
                shutil.rmtree(self.q.cache, ignore_errors=True)
                for _ in range(2):   # runs again: the same refusal, nothing fetched, nothing merged
                    got, out = self.fetch(self.c)
                    self.assertIsNone(got, out)
                    self.assertIn('git init made a repository with extensions.' + extension.split()[1], out)
                    self.assertIn('GIT_DEFAULT_HASH=sha1', out)
                    self.assertFalse(os.path.lexists(self.q.cache))
        got, out = self.fetch(self.c)   # a git that keeps the formats again: made as the queue's
        self.assertTrue(got, out)

    def test_a_git_that_makes_another_format_version_is_refused_clearly(self):   # state lens 4: M6, no [extensions]
        real = subprocess.run

        def run(cmd, **kw):
            r = real(cmd, **kw)
            if 'init' in cmd and r.returncode == 0:
                path = os.path.join(cmd[-1], 'config')
                with open(path) as f:
                    text = f.read()
                with open(path, 'w') as f:
                    f.write(text.replace('repositoryformatversion = 0', 'repositoryformatversion = 1'))
            return r
        with unittest.mock.patch.object(subprocess, 'run', run):
            for _ in range(2):
                got, out = self.fetch(self.c)
                self.assertIsNone(got, out)
                self.assertIn('git init made a repository with format version 1', out)
                self.assertFalse(os.path.lexists(self.q.cache))

    def test_a_git_whose_init_writes_more_config_still_makes_the_queues_config(self):   # state lens 4: M17 (Windows)
        with self.init_writes(config='\n[core]\n\tignorecase = true\n\tsymlinks = false\n'):
            for _ in range(3):   # made once, then kept: never rebuilt for config git chose
                got, out = self.fetch(self.c)
                self.assertTrue(got, out)
                self.assertNotIn('rebuilding', out)
                with open(os.path.join(self.q.cache, 'config')) as f:
                    self.assertEqual(f.read(), Q.CACHE_CONFIG)


def own(comment):
    """A PR comment for a fake `gh pr view`: a plain body is one the queue's own login posted."""
    return comment if isinstance(comment, dict) else {'body': comment, 'viewerDidAuthor': True}


class RedChecks(unittest.TestCase):
    """Only the newest run of a check counts, and an armed PR the queue skips says why on the PR."""
    H = 'd4' * 20

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-red-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d)

    @staticmethod
    def rollup(*runs, name='review-check'):
        return [{'__typename': 'CheckRun', 'workflowName': 'ci', 'name': name, 'startedAt': s, 'conclusion': c}
                for s, c in runs]

    def failing(self, *runs, name='review-check'):
        return self.q.failing({'statusCheckRollup': self.rollup(*runs, name=name)})

    def test_an_older_red_under_a_newer_green_is_not_failing(self):
        self.assertIsNone(self.failing(('2026-11-09T01:00:00Z', 'FAILURE'), ('2026-11-09T02:00:00Z', 'SUCCESS')))

    def test_a_newer_red_over_an_older_green_is_failing(self):
        self.assertEqual(self.failing(('2026-11-09T02:00:00Z', 'FAILURE'), ('2026-11-09T01:00:00Z', 'SUCCESS')),
                         'review-check')

    def test_a_newest_timed_out_run_is_failing_and_a_cancelled_one_is_not(self):
        self.assertEqual(self.failing(('2026-11-09T02:00:00Z', 'TIMED_OUT')), 'review-check')
        self.assertIsNone(self.failing(('2026-11-09T02:00:00Z', 'CANCELLED')))

    def test_a_red_that_only_a_cancelled_run_followed_still_stands(self):
        self.assertEqual(self.failing(('2026-11-09T01:00:00Z', 'FAILURE'), ('2026-11-09T02:00:00Z', 'CANCELLED')),
                         'review-check')

    def test_a_green_that_a_cancelled_run_followed_is_not_red(self):
        self.assertIsNone(self.failing(('2026-11-09T01:00:00Z', 'SUCCESS'), ('2026-11-09T02:00:00Z', 'CANCELLED')))

    def test_a_red_a_green_re_run_then_a_cancelled_run_is_not_red(self):   # r3 corr minor (:283)
        self.assertIsNone(self.failing(('2026-11-09T01:00:00Z', 'FAILURE'), ('2026-11-09T02:00:00Z', 'SUCCESS'),
                                       ('2026-11-09T03:00:00Z', 'CANCELLED')))
        self.assertEqual(self.failing(('2026-11-09T01:00:00Z', 'SUCCESS'), ('2026-11-09T02:00:00Z', 'FAILURE'),
                                      ('2026-11-09T03:00:00Z', 'CANCELLED')), 'review-check', 'red after the green')

    def test_the_newest_run_of_a_name_counts_whichever_workflow_ran_it(self):   # r3 corr minor (:279), required_now
        old_red = dict(self.rollup(('2026-11-09T01:00:00Z', 'FAILURE'))[0], workflowName='a')
        new_green = dict(self.rollup(('2026-11-09T02:00:00Z', 'SUCCESS'))[0], workflowName='b')
        self.assertIsNone(self.q.failing({'statusCheckRollup': [old_red, new_green]}))
        self.assertEqual(self.q.required_now([old_red, new_green]), {'review-check': 'ok'})
        new_red = dict(old_red, startedAt='2026-11-09T03:00:00Z')
        self.assertEqual(self.q.failing({'statusCheckRollup': [new_red, new_green]}), 'review-check')

    def test_a_check_that_is_not_required_is_ignored(self):
        self.assertIsNone(self.failing(('2026-11-09T02:00:00Z', 'FAILURE'), name='lint'))

    def test_two_red_checks_name_the_first_by_name(self):
        rollup = self.rollup(('2026-11-09T02:00:00Z', 'FAILURE'), name='test') + \
            self.rollup(('2026-11-09T02:00:00Z', 'FAILURE'), name='build')
        self.assertEqual(self.q.failing({'statusCheckRollup': rollup}), 'build')

    def sweep(self, state='BEHIND', runs=(('2026-11-09T02:00:00Z', 'FAILURE'),), comments=(), head=None, dry=False,
              fail=(), name='review-check', pushed=None):
        """head: the PR's head in the list; pushed: its head when tell_red reads it (a push in between)."""
        self.posted, self.updated = [], []
        listed = [{'number': 7, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1}, 'mergeStateStatus': state,
                   'createdAt': '2026-11-07T00:00:00Z', 'statusCheckRollup': self.rollup(*runs, name=name),
                   'headRefOid': head or self.H, 'baseRefName': 'main'}]

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:2] == ('pr', 'view'):
                if 'view' in fail:
                    return failed('boom')
                return ok(json.dumps({'headRefOid': pushed or head or self.H, 'comments': [own(c) for c in comments]}))
            if a[:2] == ('pr', 'comment'):
                self.posted.append(a[-1])
                return failed('boom') if 'comment' in fail else ok()
            if a[:2] == ('api', 'graphql'):   # main is green; these runs name no tested base (RedChecksWithTheBaseRule)
                return ok(json.dumps({'data': {'repository': {'object': {'oid': 'e9' * 20, 'statusCheckRollup': {
                    'contexts': {'nodes': [{'__typename': 'CheckRun', 'name': name, 'status': 'COMPLETED',
                                            'conclusion': 'SUCCESS', 'startedAt': '2026-11-09T00:00:00Z'}]}}}}}}))
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append(n) or 0
        return run_quiet(self.q.run, dry)[0]

    def test_a_red_armed_pr_is_told_whether_or_not_it_is_behind(self):
        for state in ('BEHIND', 'BLOCKED', 'UNSTABLE'):
            with self.subTest(state=state):
                self.sweep(state)
                self.assertEqual((len(self.posted), self.updated), (1, []))
                self.assertIn(f'required check review-check is red on {self.H[:12]}', self.posted[0])

    def test_a_red_then_cancelled_armed_pr_is_told_and_does_not_hold_the_queue(self):
        runs = (('2026-11-09T01:00:00Z', 'FAILURE'), ('2026-11-09T02:00:00Z', 'CANCELLED'))
        for state in ('BLOCKED', 'BEHIND'):
            with self.subTest(state=state):
                self.sweep(state, runs=runs)
                self.assertEqual((len(self.posted), self.updated), (1, []))

    def test_the_message_promises_no_re_run(self):   # nothing in the queue re-runs a red check
        for name in ('review-check', 'build'):
            with self.subTest(name=name):
                self.sweep(name=name)
                self.assertNotIn('re-run', self.posted[0])

    def test_only_the_head_check_is_pointed_at_the_ready_command(self):   # the lenses are about the head check alone
        self.sweep()
        self.assertIn('`tool ready 7`', self.posted[0])
        self.sweep(name='build')
        self.assertNotIn('tool ready', self.posted[0])

    def test_it_is_told_once_per_head(self):
        self.sweep()
        first = self.posted[0]
        self.sweep(comments=[first])
        self.assertEqual(self.posted, [])
        self.sweep(comments=[first], head='e5' * 20)
        self.assertEqual(len(self.posted), 1, 'a new head that is red is a new message')

    def test_a_push_after_the_list_posts_nothing(self):   # r3 corr minor (:356): the red is not known on the new head
        new = 'e5' * 20
        code = self.sweep(pushed=new)
        self.assertEqual((code, self.posted), (0, []))
        self.sweep(head=new)
        self.assertIn(f'is red on {new[:12]}', self.posted[0], 'the next run, listing the new head, tells it')

    def test_only_the_queues_own_comment_counts_as_told(self):   # anyone can post a copy of the mark
        self.sweep()
        first = self.posted[0]
        for copy in ({'body': first, 'viewerDidAuthor': False}, {'body': first},
                     {'body': '> ' + first, 'viewerDidAuthor': True}):
            with self.subTest(copy=copy):
                self.sweep(comments=[copy])
                self.assertEqual(self.posted, [first], 'a copy of the mark silenced the queue')

    def test_a_dry_run_posts_nothing(self):
        self.sweep(dry=True)
        self.assertEqual(self.posted, [])

    def test_a_failed_comment_or_read_does_not_stop_the_queue(self):
        for fail in ('comment', 'view'):
            with self.subTest(fail=fail):
                self.assertEqual(self.sweep(fail=(fail,)), 0)

    def test_an_older_red_no_longer_skips_the_pr(self):
        self.sweep(runs=(('2026-11-09T01:00:00Z', 'FAILURE'), ('2026-11-09T02:00:00Z', 'SUCCESS')))
        self.assertEqual((self.posted, self.updated), ([], [7]))

    def test_the_message_can_never_count_as_a_lens(self):
        self.sweep()
        post = note(self.posted[0], 't')   # an author whose lens comments count: a look-alike would block (NO_HEADER)
        self.assertIsNone(REVIEW.lens_problem(post))
        self.assertEqual(REVIEW.reviewed_head({'comments': [post], 'reviews': []}), (False, None, 'no lens comments'))


class StuckUpToDate(unittest.TestCase):
    """An armed, up-to-date PR whose required check ended without passing (cancelled) or never reported cannot merge,
    and nothing re-runs that check. It is told once per head and passed over; it does not hold the queue."""
    H1, H2 = 'e1' * 20, 'f2' * 20

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-stuck-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d)

    @staticmethod
    def checks(**states):   # every required check green, except the ones named (None: the check never reported)
        out = []
        for name in sorted(CFG.required_checks):
            state = states.get(name.replace('-', '_'), 'SUCCESS')
            if state is None:
                continue
            run = {'__typename': 'CheckRun', 'workflowName': 'ci', 'name': name, 'startedAt': '2026-11-11T01:00:00Z',
                   'status': 'COMPLETED', 'conclusion': state}
            if state == 'IN_PROGRESS':
                run.update(status='IN_PROGRESS', conclusion='')
            out.append(run)
        return out

    def sweep(self, first, comments=(), runs='[]', runs_fail=False, dry=False):
        """#1 (oldest): up to date (BLOCKED) with rollup `first`; #2: BEHIND and green."""
        self.posted, self.updated, self.asked = [], [], []
        listed = [{'number': 1, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1}, 'mergeStateStatus': 'BLOCKED',
                   'createdAt': '2026-11-07T00:00:01Z', 'statusCheckRollup': first, 'headRefOid': self.H1,
                   'baseRefName': 'main'},
                  {'number': 2, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1}, 'mergeStateStatus': 'BEHIND',
                   'createdAt': '2026-11-07T00:00:02Z', 'statusCheckRollup': self.checks(), 'headRefOid': self.H2,
                   'baseRefName': 'main'}]

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:2] == ('pr', 'view'):
                return ok(json.dumps({'headRefOid': self.H1, 'comments': [own(c) for c in comments]}))
            if a[:2] == ('pr', 'comment'):
                self.posted.append(a[-1])
                return ok()
            if a[:2] == ('run', 'list'):
                self.asked.append(a[a.index('--commit') + 1])
                return failed('boom') if runs_fail else ok(runs)
            if a[0] == 'api' and '/check-runs?' in a[1] and self.q.cfg.head_check_app:   # the gate App's: none
                return ok('{"total_count": 0, "check_runs": []}')
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append(n) or 0
        return run_quiet(self.q.run, dry)

    def test_a_cancelled_required_check_is_told_and_passed_over(self):
        code, out = self.sweep(self.checks(build='CANCELLED'))
        self.assertEqual((code, self.updated, len(self.posted)), (0, [2], 1), out)
        self.assertIn(f'required check build ended CANCELLED on {self.H1[:12]}', self.posted[0])
        self.assertIn('Re-run failed jobs', self.posted[0])
        self.assertNotIn('tool ready', self.posted[0])

    def test_a_rerun_tells_it_once_per_head_and_still_moves_on(self):
        self.sweep(self.checks(review_check='CANCELLED'))
        first = self.posted[0]
        self.sweep(self.checks(review_check='CANCELLED'), comments=[first])
        self.assertEqual((self.posted, self.updated), ([], [2]))
        self.assertNotIn('tool ready', first, 'a cancelled head check needs a re-run, not new lenses')

    def test_other_ended_states_count_too(self):   # check_state's rule: only an unfinished check is running
        for state in ('STARTUP_FAILURE', 'STALE', 'ACTION_REQUIRED'):
            with self.subTest(state=state):
                self.sweep(self.checks(test=state))
                self.assertEqual(self.updated, [2])
                self.assertIn(f'ended {state}', self.posted[0])
                self.assertIn('Re-run failed jobs', self.posted[0])

    def test_a_re_run_of_the_cancelled_check_holds_the_queue_again(self):   # the newest run counts: it is running
        rerun = self.checks(build='CANCELLED') + [{'__typename': 'CheckRun', 'workflowName': 'ci', 'name': 'build',
                                                   'status': 'QUEUED', 'conclusion': ''}]
        code, out = self.sweep(rerun)
        self.assertEqual((code, self.updated, self.posted), (0, [], []), out)
        self.assertIn('#1 is up to date and waiting', out)

    def test_green_or_running_checks_still_hold_the_queue(self):
        for first in (self.checks(), self.checks(review_check='IN_PROGRESS')):
            with self.subTest(first=len(first)):
                code, out = self.sweep(first)
                self.assertEqual((code, self.updated, self.posted, self.asked), (0, [], [], []), out)

    def test_a_cancelled_check_waits_while_a_workflow_runs_on_the_head(self):   # r3 corr minor (:322)
        # cancel-in-progress: the newer run of the workflow is queued and has no job yet, so it is not in the rollup
        for kw in ({'runs': '[{"status": "queued", "workflowName": "ci"}]'}, {'runs_fail': True}):
            with self.subTest(**kw):
                code, out = self.sweep(self.checks(build='CANCELLED'), **kw)
                self.assertEqual((code, self.updated, self.posted, self.asked), (0, [], [], [self.H1]), out)
        self.assertIn('required check build ended CANCELLED, and which workflows still run', out)

    def test_a_head_no_check_ever_reported_on_is_told_once_its_workflows_ended(self):   # r3 corr minor (:328)
        # every job skipped itself or the workflow ended before any required job: it never merges, so it must not hold
        code, out = self.sweep([], runs='[{"status": "completed", "workflowName": "ci"}]')
        self.assertEqual((self.asked, self.updated, len(self.posted)), ([self.H1], [2], 1), out)
        self.assertIn('required check build never reported', self.posted[0])
        for kw in ({'runs': '[]'}, {'runs': '[{"status": "in_progress", "workflowName": "ci"}]'}, {'runs_fail': True}):
            with self.subTest(**kw):   # nothing ran yet, or still runs, or unknown: it may still get its checks
                code, out = self.sweep([], **kw)
                self.assertEqual((code, self.updated, self.posted), (0, [], []), out)
        self.assertIn('is unknown: waiting', out)
        code, out = self.sweep([], runs='[]')
        self.assertIn('no check and no workflow run on its head yet: waiting', out)

    def test_a_required_check_that_never_reports_is_told_once_nothing_runs(self):
        code, out = self.sweep(self.checks(test=None), runs='[{"status": "completed", "workflowName": "ci"}]')
        self.assertEqual((self.asked, self.updated, len(self.posted)), ([self.H1], [2], 1), out)
        self.assertIn('required check test never reported', self.posted[0])

    def test_a_missing_gate_check_is_told_the_gate_posts_it(self):   # PR #85 corr note: the gate is queued
        # The head gate's runs belong to the default branch, so none lists on the head: while the gate waits (for an
        # earlier run on the head, or a runner) every workflow of the head has ended and the check is missing. Push
        # again is the wrong advice: the gate App posts it, and a dispatch starts it.
        self.q.cfg = config.parse(json.dumps({**CFG_RAW, 'head_check_app': 4242}))
        self.sweep(self.checks(review_check=None), runs='[{"status": "completed", "workflowName": "ci"}]')
        told = self.posted[0]
        self.assertIn('required check review-check not posted by the gate App yet', told)
        self.assertIn('dispatch the gate', told)
        self.assertNotIn('Push again', told)
        self.sweep(self.checks(test=None, review_check=None), runs='[{"status": "completed", "workflowName": "ci"}]')
        self.assertIn('required check test never reported', self.posted[0], 'a workflow check missing too: as before')

    def test_a_check_that_never_reported_is_not_told_to_re_run(self):   # the source told it to re-run
        # A re-run repeats the jobs of the workflow files the head was built with: a check added to the workflows
        # after that never runs on a re-run. Only a push or a merge of the base runs the current workflows.
        self.sweep(self.checks(test=None), runs='[{"status": "completed", "workflowName": "ci"}]')
        told = self.posted[0]
        self.assertNotIn('Re-run failed jobs', told, 'a re-run cannot add a check its workflow files do not have')
        self.assertIn('A re-run cannot add it', told)
        self.assertIn('`tool push`', told)
        self.assertIn('merge main into it', told)

    def test_a_missing_check_waits_while_a_workflow_runs_or_when_runs_are_unknown(self):
        for kw in ({'runs': '[{"status": "in_progress", "workflowName": "ci"}]'}, {'runs_fail': True}, {'runs': 'x'}):
            with self.subTest(**kw):
                code, out = self.sweep(self.checks(test=None), **kw)
                self.assertEqual((code, self.updated, self.posted), (0, [], []), out)
        self.assertIn('is unknown: waiting', out, 'an unknown answer is said, not treated as "nothing runs"')

    def test_a_dry_run_posts_nothing_and_says_what_it_skips(self):
        code, out = self.sweep(self.checks(build='CANCELLED'), dry=True)
        self.assertEqual((self.posted, self.updated), ([], [2]))
        self.assertIn('skip #1: up to date, but required check build ended CANCELLED', out)


class UnknownMergeState(unittest.TestCase):
    """GitHub shows UNKNOWN for a few seconds after a push to the base, when the push's queue run starts: the older PR
    is listed again until its state is known, so a younger one is not updated ahead of it (r3 corr minor, :518)."""
    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-unknown-')
        self.addCleanup(shutil.rmtree, d, True)
        self.sleeps = []
        self.q = make_queue(d, sleeps=self.sleeps)

    def sweep(self, unknown_lists):
        self.updated, lists = [], []

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                lists.append(1)
                first = 'UNKNOWN' if len(lists) <= unknown_lists else 'BEHIND'
                return ok(json.dumps([{'number': n, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1},
                                       'mergeStateStatus': first if n == 1 else 'BEHIND', 'statusCheckRollup': [],
                                       'createdAt': f'2026-11-07T00:00:0{n}Z', 'headRefOid': f'{n}' * 40,
                                       'baseRefName': 'main'} for n in (1, 2)]))
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append(n) or 0
        return run_quiet(self.q.run, False)

    def test_an_older_pr_in_unknown_state_is_listed_again_before_a_younger_one_moves(self):
        code, out = self.sweep(unknown_lists=2)
        self.assertEqual((code, self.updated, self.sleeps), (0, [1], [CFG.poll_seconds] * 2), out)
        self.assertIn('#1: merge state UNKNOWN', out)

    def test_an_unknown_state_that_stays_is_passed_over_after_wait_seconds(self):
        tries = CFG.wait_seconds // CFG.poll_seconds
        code, out = self.sweep(unknown_lists=tries + 5)
        self.assertEqual((code, self.updated, len(self.sleeps)), (0, [2], tries), out)
        self.assertIn('skip #1: merge state UNKNOWN', out)


class RedChecksWithTheBaseRule(RedChecks):
    """Every RedChecks case again with update_when_base_was_red on: a red whose run names no tested base (its job has
    no tested-base step) is the PR's own, as with the rule off."""
    def setUp(self):
        super().setUp()
        self.q.cfg = config.parse(json.dumps({**CFG_RAW, 'update_when_base_was_red': True}))


class RedOnOldBase(unittest.TestCase):
    """update_when_base_was_red: a BEHIND PR red only because the base commit its run merged with was red is updated; a
    red of its own, or a base that proves nothing (cancelled, stale, skipped, running), still skips it. The base comes
    from the run (each job's tested-base annotation), never from clocks: jobs start late, and a re-run tests its old
    merge again. The source project's draft fix, ported (README.md, "Where the rules come from").
    main: M0 (build green) -> M1 (build FAILURE) -> M2 (green again, the tip)."""
    H, H2 = 'a7' * 20, 'b8' * 20
    M0, M1, M2 = 'e0' * 20, 'e1' * 20, 'e2' * 20
    RUN, RERUN = '2026-11-07T22:30:00Z', '2026-11-08T02:20:00Z'

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-base-red-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d, config.parse(json.dumps({**CFG_RAW, 'update_when_base_was_red': True})))

    def main_of(self, m1='FAILURE', tip='SUCCESS', check='build', other=None):
        """{sha: {check: conclusion}} of main's commits; M2 is the tip."""
        return {self.M0: {check: 'SUCCESS', **(other or {})}, self.M1: {check: m1, **(other or {})},
                self.M2: {check: tip, **(other or {})}}

    def sweep(self, prs, main, state='BEHIND', dry=False, notes_fail=False, main_fails=False, more=False, comments=()):
        """prs: [(number, head, {name: [(startedAt, conclusion, tested base or None), ...]})]; main: main_of()."""
        self.posted, self.updated, self.tip_reads, self.api = [], [], 0, 0
        notes, listed = {}, []
        for n, head, runs in prs:
            rollup = []
            for name, rs in runs.items():
                for t, c, base in rs:
                    job = str(1000 + len(notes))
                    notes[job] = [{'title': Q.TESTED_BASE, 'message': base}] if base else []
                    rollup.append({'__typename': 'CheckRun', 'workflowName': 'ci', 'name': name, 'startedAt': t,
                                   'conclusion': c, 'detailsUrl': f'https://github.com/{REPO}/actions/runs/9/job/{job}'})
            listed.append({'number': n, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1},
                           'mergeStateStatus': state, 'createdAt': f'2026-11-07T00:00:0{n}Z', 'statusCheckRollup': rollup,
                           'headRefOid': head, 'baseRefName': 'main'})
        heads = {n: head for n, head, _ in prs}

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:2] == ('pr', 'view'):
                return ok(json.dumps({'headRefOid': heads[int(a[2])], 'comments': [own(c) for c in comments]}))
            if a[:2] == ('pr', 'comment'):
                self.posted.append(a[-1])
                return ok()
            self.api += a[0] == 'api'
            if a[0] == 'api' and a[1].endswith('/annotations?per_page=100'):
                return failed() if notes_fail else ok(json.dumps(notes[a[1].split('/')[-2]]))
            if a[:2] == ('api', 'graphql'):
                if main_fails:
                    return failed()
                expr = next(x[5:] for x in a if x.startswith('expr='))
                sha = self.M2 if expr == 'refs/heads/main' else expr
                self.tip_reads += expr == 'refs/heads/main'
                if sha not in main:
                    return ok(json.dumps({'data': {'repository': {'object': None}}}))
                nodes = [{'__typename': 'CheckRun', 'name': k, 'status': 'COMPLETED' if c else 'IN_PROGRESS',
                          'conclusion': c, 'startedAt': self.RUN, 'checkSuite': {'workflowRun': {'workflow': {'name': 'ci'}}}}
                         for k, c in main[sha].items()]
                return ok(json.dumps({'data': {'repository': {'object': {'oid': sha, 'statusCheckRollup': {
                    'contexts': {'pageInfo': {'hasNextPage': more}, 'nodes': nodes}}}}}}))
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append((n, d)) or 0
        return run_quiet(self.q.run, dry)

    def one(self, runs, main=None, **kw):
        return self.sweep([(7, self.H, runs)], main or self.main_of(), **kw)

    def test_a_red_from_a_red_base_that_is_green_now_is_updated(self):   # the issue
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'SUCCESS', self.M1)]})
        self.assertEqual((code, self.updated, self.posted), (0, [(7, False)], []), out)
        self.assertIn('required check build failed on a main commit where it failed too, and it is green on main now',
                      out)

    def test_off_by_default_the_red_is_the_prs_own_and_nothing_more_is_read(self):   # a project without the notes
        self.q.cfg = CFG
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]})
        self.assertEqual((code, self.updated, len(self.posted), self.api), (0, [], 1, 0), out)
        self.assertIn('Fix the cause and push', self.posted[0])

    def test_a_job_that_started_late_is_judged_by_the_base_it_tested_not_its_clock(self):
        # its jobs queued for hours: it started after M2 went green, but its merge was made on M1
        code, out = self.one({'build': [(self.RERUN, 'FAILURE', self.M1)]})
        self.assertEqual((self.updated, self.posted), ([(7, False)], []), out)
        # the other way round: it started while M1 was the tip, but its merge was made on the green M0 before
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M0)]})
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'build was not red on the main commit {self.M0[:12]} its run tested (ok)', out)

    def test_a_re_run_of_the_old_merge_must_be_updated(self):   # a re-run tests the old red base again
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1), (self.RERUN, 'FAILURE', self.M1)]})
        self.assertEqual((self.updated, self.posted), ([(7, False)], []), out)

    def test_its_red_after_the_update_is_its_own_and_is_told_no_loop(self):   # a real new merge head
        # the update made head H2; its run merged H2 with the green M2 and still failed: its own red, told once
        code, out = self.sweep([(7, self.H2, {'build': [(self.RERUN, 'FAILURE', self.M2)]})], self.main_of())
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'build was not red on the main commit {self.M2[:12]} its run tested (ok)', out)
        self.assertIn(f'required check build is red on {self.H2[:12]}', self.posted[0])
        self.assertIn('Fix the cause and push', self.posted[0])

    def test_a_base_check_that_proves_nothing_is_not_base_was_red(self):
        for m1, said in (('CANCELLED', 'CANCELLED'), ('STALE', 'STALE'), ('STARTUP_FAILURE', 'STARTUP_FAILURE'),
                         ('TIMED_OUT', 'TIMED_OUT'), ('SKIPPED', 'ok'), (None, 'running')):
            with self.subTest(main=m1):
                code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, self.main_of(m1=m1))
                self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
                self.assertIn(f'its run tested ({said})', out)
                self.assertIn('Fix the cause and push', self.posted[0])
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, self.main_of(check='test'))
        self.assertEqual(self.updated, [], out)
        self.assertIn('(not reported)', out)

    def test_a_base_still_red_waits_and_says_the_base_is_the_reason(self):   # not "fix and push"
        for tip, said in (('FAILURE', 'FAILURE'), ('CANCELLED', 'CANCELLED'), (None, 'running')):
            with self.subTest(tip=tip):
                code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, self.main_of(tip=tip))
                self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
                self.assertIn(f'is not green on main now ({said})', out)
                self.assertIn(f'required check build failed as on main on {self.H[:12]}', self.posted[0])
                self.assertIn('not this PR', self.posted[0])
                self.assertIn('updates it by itself once build is green on main', self.posted[0])
                self.assertNotIn('Fix the cause and push', self.posted[0])
        told = self.posted[0]
        self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, self.main_of(tip='FAILURE'), comments=[told])
        self.assertEqual(self.posted, [], 'told once per head')

    def test_every_red_check_must_come_from_the_base(self):   # one red of its own keeps it skipped
        runs = {'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'FAILURE', self.M1)]}
        code, out = self.one(runs, self.main_of(other={'test': 'SUCCESS'}))
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'test was not red on the main commit {self.M1[:12]} its run tested (ok)', out)

    def test_a_red_of_its_own_beside_a_base_still_red_is_its_authors(self):   # r3 major: never "not this PR's fault"
        main = {k: {**v, 'test': 'SUCCESS'} for k, v in self.main_of(tip='FAILURE').items()}
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'FAILURE', self.M1)]}, main)
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'test was not red on the main commit {self.M1[:12]} its run tested (ok)', out)
        self.assertNotIn('not this PR', self.posted[0])
        self.assertIn('Fix the cause and push', self.posted[0])

    def test_the_notice_names_the_prs_own_red_beside_a_base_red_that_sorts_first(self):   # r4 corr minor (:737)
        main = {k: {**v, 'test': 'SUCCESS'} for k, v in self.main_of(tip='FAILURE').items()}   # build: the base's
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'FAILURE', self.M1)]}, main)
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'required check test is red on {self.H[:12]}', self.posted[0])
        self.assertIn(f'skip #7: required check test failed and an update would not fix it yet', out)

    def test_the_wait_notice_is_told_again_when_the_check_it_names_turns_green_on_the_base(self):   # r4 (:729/:361)
        runs = {'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'FAILURE', self.M1)]}
        both = self.main_of(tip='FAILURE', other={'test': 'FAILURE'})   # build and test red on main
        self.one(runs, both)
        first = self.posted[0]
        self.assertIn('once build is green on main', first)
        self.one(runs, both, comments=[first])
        self.assertEqual(self.posted, [], 'told once per head and check')
        fixed = self.main_of(other={'test': 'FAILURE'})   # main fixed build; test is still red there
        code, out = self.one(runs, fixed, comments=[first])
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn(f'required check test failed as on main on {self.H[:12]}', self.posted[0])
        self.assertIn('once test is green on main', self.posted[0])

    def test_the_wait_notice_names_the_check_that_is_red_on_the_base(self):   # r3 minor: not just the first red
        main = self.main_of(other={'test': 'FAILURE'})   # build: red on M1, green now; test: red on M1 and now
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)], 'test': [(self.RUN, 'FAILURE', self.M1)]}, main)
        self.assertEqual((self.updated, len(self.posted)), ([], 1), out)
        self.assertIn('updates it by itself once test is green on main', self.posted[0])

    def test_an_unknown_tested_base_or_base_is_said_and_skipped(self):   # unknown is never "the base was red"
        for kw, base, said in (({}, None, 'tested is unknown'), ({'notes_fail': True}, self.M1, 'HTTP 502'),
                               ({'main_fails': True}, self.M1, 'HTTP 502'), ({}, 'ff' * 20, 'are unknown'),
                               ({'more': True}, self.M1, 'more than 100, not all read')):
            with self.subTest(said=said):
                code, out = self.one({'build': [(self.RUN, 'FAILURE', base)]}, **kw)
                self.assertEqual((code, self.updated, len(self.posted)), (0, [], 1), out)
                self.assertIn(said, out)
                self.assertIn('Fix the cause and push', self.posted[0])

    def test_a_job_with_no_note_two_notes_or_no_job_has_no_tested_base(self):
        url = f'https://github.com/{REPO}/actions/runs/9/job/5'
        for notes, said in (([{'title': Q.TESTED_BASE, 'message': self.M1}, {'title': Q.TESTED_BASE, 'message': self.M0}],
                             'names 2 tested bases'),
                            ([{'title': Q.TESTED_BASE, 'message': 'main'}], 'names 0 tested bases'),
                            ([{'title': 'lint', 'message': 'x'}] * 100, 'may be on a later page')):
            with self.subTest(said=said):
                self.q.gh = self.q.gh_write = lambda *a, n=notes: ok(json.dumps(n))
                got, out = run_quiet(self.q.tested_base, {'name': 'build', 'detailsUrl': url})
                self.assertIsNone(got)
                self.assertIn(said, out)
        got, out = run_quiet(self.q.tested_base, {'name': 'build', 'detailsUrl': 'https://ci.example/build/5'})
        self.assertEqual(got, None)
        self.assertIn('no Actions job', out)

    def test_the_base_is_read_once_per_run(self):
        prs = [(7, self.H, {'build': [(self.RUN, 'FAILURE', self.M0)]}),
               (8, self.H2, {'build': [(self.RUN, 'FAILURE', self.M1)]})]
        code, out = self.sweep(prs, self.main_of())
        self.assertEqual((self.updated, len(self.posted), self.tip_reads), ([(8, False)], 1, 1), out)

    def test_an_up_to_date_red_pr_never_asks_for_the_base(self):   # an update would not change its base
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, state='BLOCKED')
        self.assertEqual((self.updated, len(self.posted), self.api), ([], 1, 0), out)

    def test_a_dry_run_says_it_would_update_and_posts_nothing(self):
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, dry=True)
        self.assertEqual((self.updated, self.posted), ([(7, True)], []), out)
        code, out = self.one({'build': [(self.RUN, 'FAILURE', self.M1)]}, self.main_of(tip='FAILURE'), dry=True)
        self.assertEqual((self.updated, self.posted), ([], []), out)


# --- the source project's lens-review cases for the queue ------------------------------------------------------------
HEAD = 'def wipe(player, island):\n'
READ = HEAD + "    if is_admin(player):\n        log.info('wipe')\n        wipe_island(island)\n"   # only an admin wipes
DEDENTED = HEAD + "    if is_admin(player):\n        log.info('wipe')\n    wipe_island(island)\n"   # every player does
EXTRA = READ + '    refund_all(island)\n'   # a real code change pushed later


class Admin(unittest.TestCase):
    """main M0 -> M1 (another file changed); P: the read change on M0."""
    def setUp(self):
        w = self.w = World()
        self.addCleanup(w.close)
        self.q = make_queue(w.dir, remote=w.remote)
        self.m0 = w.commit({'w.py': HEAD, 'm.txt': 'a\n'})
        self.m1 = w.commit({'w.py': HEAD, 'm.txt': 'b\n'}, [self.m0])
        w.set('refs/heads/main', self.m1)
        self.p = w.commit({'w.py': READ, 'm.txt': 'a\n'}, [self.m0])
        w.set('refs/pull/7/head', self.p)

    def push(self, parent, text):
        return self.w.commit({'w.py': text, 'm.txt': 'a\n'}, [parent])

    def update(self, fake):
        self.q.gh = self.q.gh_write = fake
        return run_quiet(self.q.update, 7)

    def main(self, fake, q=None):
        q = q or self.q
        q.gh = q.gh_write = fake
        return run_quiet(q.main)


class OnlyWhatALensReadIsReArmed(Admin):
    """GitHub's auto-merge request records no head, and GitHub keeps auto-merge on after a push by someone with write
    access, so after a plain push, a web conflict resolution or an applied suggestion the PR is armed at a head no lens
    read. The queue must not re-arm that head plus the base with a fresh pin."""

    def test_a_push_after_ready_is_not_re_armed(self):
        w = self.w
        h1 = self.push(self.p, EXTRA)
        f = QueueGh(w, h1, w.merge(h1, self.m1), comments=[lens_at('data-integrity', self.p),
                                                             lens_at('correctness', self.p)])
        code, out = self.update(f)
        self.assertIsNone(f.armed, f'the queue armed a head with a push no lens read (lenses read {self.p[:7]}): {out}')
        self.assertEqual(len(f.comments), 1, 'auto-merge left off must say so on the PR')

    def test_control_the_head_the_lenses_read_is_still_re_armed(self):
        f = QueueGh(self.w, self.p, self.w.merge(self.p, self.m1), comments=[lens_at('correctness', self.p)])
        self.update(f)
        self.assertEqual(f.armed, f.after)

    def test_control_a_pr_without_lens_comments_is_still_re_armed(self):   # no review: the head it found merges on green
        f = QueueGh(self.w, self.p, self.w.merge(self.p, self.m1))
        self.update(f)
        self.assertEqual(f.armed, f.after)


class WhitespaceIsPartOfTheChange(Admin):
    """The queue compares whole trees, whitespace included: a re-indent (Python, YAML) or a space inside a command is
    another change."""

    def test_a_python_dedent_is_a_different_change(self):
        dedented = self.w.commit({'w.py': DEDENTED, 'm.txt': 'a\n'}, [self.m0])
        self.assertFalse(self.q.same_change('main', 7, self.w.merge(dedented, self.m1), self.p),
                         'wipe_island moved out of the admin check, taken as the change read')

    def test_a_space_in_a_shell_command_is_a_different_change(self):
        a = self.w.commit({'w.py': HEAD, 'm.txt': 'a\n', 'c.sh': 'set -eu\nrm -rf "${DIR:?}"/cache\n'}, [self.m0])
        b = self.w.commit({'w.py': HEAD, 'm.txt': 'a\n', 'c.sh': 'set -eu\nrm -rf "${DIR:?}"/ cache\n'}, [self.m0])
        self.assertFalse(self.q.same_change('main', 7, self.w.merge(b, self.m1), a))

    def test_main_moving_lines_still_counts_as_the_same_change(self):
        m2 = self.w.commit({'w.py': '# header\n# more\n' + HEAD, 'm.txt': 'b\n'}, [self.m1])   # main adds lines above
        self.w.set('refs/heads/main', m2)
        self.assertTrue(self.q.same_change('main', 7, self.w.merge(self.p, m2), self.p))

    def test_a_whitespace_only_push_meanwhile_is_not_re_armed(self):
        # P was read. Between the queue's view and update-branch, someone pushes a dedent on top of P; update-branch
        # merges the base into THAT head.
        pushed = self.push(self.p, DEDENTED)
        f = QueueGh(self.w, self.p, self.w.merge(pushed, self.m1), comments=[lens_at('correctness', self.p)])
        code, out = self.update(f)
        self.assertNotEqual(f.armed, f.after, 'armed a head where every player wipes islands: ' + out)

    def test_a_reindented_python_line_is_not_the_same_change(self):
        base = 'def f(x):\n    if x:\n        y = 1\n    return y\n'
        m0 = self.w.commit({'x.py': base, 'm.txt': 'a\n'})
        read = self.w.commit({'x.py': base.replace('        y = 1\n', '        y = 1\n    return check(x)\n'),
                              'm.txt': 'a\n'}, [m0])
        after = self.w.commit({'x.py': base.replace('        y = 1\n', '        y = 1\n        return check(x)\n'),
                               'm.txt': 'a\n'}, [m0])   # now inside the block above
        m1 = self.w.commit({'x.py': base, 'm.txt': 'b\n'}, [m0])
        self.w.set('refs/heads/main', m1)
        h2 = self.w.merge(after, m1)
        self.w.set('refs/pull/7/head', h2)
        self.assertTrue(self.q.same_change('main', 7, self.w.merge(read, m1), read), 'control: the read change + main')
        self.assertFalse(self.q.same_change('main', 7, h2, read), 'a re-indented Python line passed as the change read')


class AStopHalfWayLeavesATrace(Admin):
    """Killed after the disarm (a job timeout; no gh call has a timeout of its own), the next run must re-arm the PR at
    a checked head or say on it that it is OFF: never OFF and silent, skipped for good."""

    def test_killed_after_the_disarm_then_the_next_run_re_arms_or_says_it_is_off(self):
        for kill_at in ('update', 'poll', 'arm'):
            with self.subTest(kill_at=kill_at):
                f = QueueGh(self.w, self.p, self.w.merge(self.p, self.m1), kill_at=kill_at)
                with self.assertRaises(Killed):
                    self.main(f)
                f.kill_at = None
                q = make_queue(self.w.dir, remote=self.w.remote)   # the next run is a new process
                self.main(f, q)
                self.assertTrue(f.armed == f.head or f.comments,
                                f'killed at {kill_at}, then a rerun: auto-merge OFF at {f.head[:12]} and nothing on '
                                'the PR says so')
                self.assertFalse(os.path.exists(q.state))


ISLAND = ['    island = load(island_id)', '    if island is None:', '        return False',
          "    log.info('island %s', island_id)", '    save(island)', '    return True']
CHECK = '    require_owner(player, island)'


def islands(check_in=None, top=()):
    """w.py: two copy-pasted handlers with the same lines; `check_in` gets the owner check after its 3rd line; `top`:
    lines main added at the top (other line numbers for everything below)."""
    out = list(top)
    for fn in ('wipe_island', 'visit_island'):
        out += [f'def {fn}(player, island_id):'] + ISLAND[:3] + ([CHECK] if fn == check_in else []) + ISLAND[3:] + ['', '']
    return '\n'.join(out) + '\n'


class Islands(unittest.TestCase):
    def setUp(self):
        w = self.w = World()
        self.addCleanup(w.close)
        self.q = make_queue(w.dir, remote=w.remote)
        self.m0 = w.commit({'w.py': islands()})
        self.m1 = w.commit({'w.py': islands(top=['import log', '', ''])}, [self.m0])
        w.set('refs/heads/main', self.m1)
        self.read = w.commit({'w.py': islands('wipe_island')}, [self.m0])   # what the lenses read

    def update(self, fake):
        self.q.gh = self.q.gh_write = fake
        return run_quiet(self.q.update, 7)


class AMovedHunkIsAnotherChange(Islands):
    """A line moved into another function with the same context lines (copy-pasted handlers) is another change: the
    owner check moved from wipe_island to visit_island lets every player wipe islands."""

    def test_the_owner_check_moved_to_another_function_is_not_re_armed(self):
        moved = self.w.commit({'w.py': islands('visit_island')}, [self.read])   # pushed after ready, kept armed
        f = QueueGh(self.w, moved, self.w.merge(moved, self.m1),
                    comments=[lens_at('correctness', self.read), lens_at('data-integrity', self.read)])
        code, out = self.update(f)
        self.assertIsNone(f.armed, 'armed a head whose owner check sits in visit_island, not wipe_island: ' + out)

    def test_control_the_read_change_merged_with_main_is_re_armed(self):
        up = self.w.merge(self.read, self.m1)
        f = QueueGh(self.w, self.read, up, comments=[lens_at('correctness', self.read)])
        self.update(f)
        self.assertEqual(f.armed, up)


class ShortReadsNameNoHead(Islands):
    """A read shorter than 40 hex names nothing, even of the head itself: after a force-push, a new commit can be made
    to share the prefix of the dropped one that was read. Left OFF, the comment says how to post the read."""

    def test_a_short_read_of_the_head_is_left_off_with_the_way_out(self):
        up = self.w.merge(self.read, self.m1)
        f = QueueGh(self.w, self.read, up,
                    comments=[lens_at('correctness', self.read[:7]), lens_at('data-integrity', self.read[:12])])
        code, out = self.update(f)
        self.assertEqual((f.armed, f.updated, len(f.comments)), (None, False, 1), out)
        self.assertIn(lens.SHORT, f.comments[0])
        self.assertIn('re-post it with that', f.comments[0])
        self.assertNotRegex(f.comments[0], READ_AT)

    def test_control_full_reads_of_one_head_are_re_armed(self):
        up = self.w.merge(self.read, self.m1)
        f = QueueGh(self.w, self.read, up,
                    comments=[lens_at('correctness', self.read), lens_at('data-integrity', self.read)])
        code, out = self.update(f)
        self.assertEqual(f.armed, up, out + str(f.comments))

    def test_control_reads_of_two_heads_still_give_no_head(self):
        other = H1[:7] + 'f' * 33
        pr = {'headRefOid': H1, 'commits': [{'oid': H1}, {'oid': other}],
              'comments': [lens_at('correctness', H1), lens_at('data-integrity', other)]}
        self.assertIsNone(REVIEW.reviewed_head(pr)[1])


class TwoPrs:
    """gh for PR #7 (a QueueGh) and PR #9: open, not armed, at h9, left OFF by an earlier stopped run.
    `view_fails`: how many `pr view 9` calls fail first."""
    def __init__(self, seven, h9, view_fails=0):
        self.seven, self.h9, self.view_fails, self.armed9, self.comments9 = seven, h9, view_fails, None, []

    def view9(self):
        return {'number': 9, 'state': 'OPEN', 'isDraft': False, 'baseRefName': 'main', 'headRefOid': self.h9,
                'autoMergeRequest': {'mergeMethod': 'SQUASH'} if self.armed9 else None, 'mergeStateStatus': 'BLOCKED',
                'title': 'nine', 'createdAt': '2026-11-06T00:00:00Z', 'statusCheckRollup': [],
                'comments': [], 'reviews': [], 'commits': [{'oid': self.h9}], 'labels': []}

    def __call__(self, *a):
        if a[:2] == ('pr', 'list'):
            self.seven.calls.append(a)
            return ok(json.dumps([self.view9(), self.seven.view()]))
        if not (a[:1] == ('pr',) and len(a) > 2 and a[2] == '9'):
            return self.seven(*a)
        if a[1] == 'view':
            if self.view_fails:
                self.view_fails -= 1
                return failed()
            return ok(json.dumps(self.view9()))
        if a[1] == 'comment':
            self.comments9.append(a[a.index('--body') + 1])
        elif a[1] == 'merge':
            self.armed9 = None if '--disable-auto' in a else a[a.index('--match-head-commit') + 1]
        return ok()


class AFailedRecoverIsNotDropped(Islands):
    """recover() keeps STATE when gh cannot show the PR; the run must then end, or the next PR's update overwrites
    STATE and the stopped PR stays OFF and silent."""

    def test_pr_9_left_off_by_a_stopped_run_is_re_armed_or_commented_after_a_gh_error(self):
        h9 = self.w.commit({'n.py': 'x\n'}, [self.m1])
        seven = QueueGh(self.w, self.read, self.w.merge(self.read, self.m1), comments=[lens_at('correctness', self.read)])
        f = TwoPrs(seven, h9, view_fails=1)
        self.q.write_state({'repo': REPO, 'pr': 9, 'old': h9, 'base': 'main', 'read': h9})   # killed after PR 9's disarm
        self.q.gh = self.q.gh_write = f
        run_quiet(self.q.main)   # recover: `pr view 9` fails (HTTP 502)
        self.assertTrue(os.path.exists(self.q.state), 'STATE for PR 9 was dropped')
        self.assertEqual((seven.armed, seven.updated), (self.read, False), 'PR 7 was updated meanwhile')
        run_quiet(self.q.main)   # the next run: gh works again
        self.assertTrue(f.armed9 == h9 or f.comments9, 'PR 9: OFF since the stopped run, never re-armed, no comment')
        self.assertFalse(os.path.exists(self.q.state))


# --- gaps found before the source project moved to this queue -------------------------------------------------------
QUEUED = 'GraphQL: Required status check "build" is queued. (mergePullRequest)'
MOVED = 'GraphQL: Head branch was modified. Review and try the merge again. (enablePullRequestAutoMerge)'


class ArmWaitsOutTheQueuedCheckRace(Base):
    """Right after update-branch GitHub can refuse the re-arm with `Required status check "build" is queued`: the new
    head's checks have not started. Only that refusal is tried again, for the same head, bounded by wait_seconds; before,
    the first refusal left a PR that held exactly the read change OFF until a person ran the ready command."""
    def arms(self, f):
        return [a[a.index('--match-head-commit') + 1] for a in f.calls if QueueGh.kind(a) == 'arm']

    def assertLeftOff(self, f, code, said, tries):
        self.assertEqual((code, f.armed, len(f.comments)), (0, None, 1), f.comments)
        self.assertEqual(self.arms(f), [self.up] * tries)
        self.assertIn(said, f.comments[0])
        self.assertIn('`tool ready 7`', f.comments[0])
        self.assertIsNone(REVIEW.lens_problem(note(f.comments[0], 't')), 'a queue comment must never look like a lens')
        self.assertFalse(self.plan_exists(), 'OFF and said on the PR: nothing left to finish')

    def test_a_queued_check_then_success_rearms_the_same_head(self):
        f = self.fake(arm_says=[QUEUED])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertEqual(f.order(), ['disarm', 'update', 'arm', 'arm'])
        self.assertEqual(self.arms(f), [self.up, self.up], 'every try names the head that was checked')
        self.assertEqual(self.sleeps, [CFG.poll_seconds])
        self.assertIn('trying again in 5 s', out)
        self.assertFalse(self.plan_exists())

    def test_every_not_started_yet_wording_is_the_same_race(self):
        for msg in ('Required status check "build" is in progress.', 'Required status check "build" is expected.',
                    'Required status check "build" is pending.', '2 of 2 required status checks are expected.',
                    'GraphQL: Required status check "build" is queued. (mergePullRequest)',
                    'Required status check "test (3.12)" is queued.', 'Required status check "v1.2.3 build" is pending.'):
            with self.subTest(msg=msg):
                self.assertTrue(Q.CHECK_NOT_DONE_RE.search(msg))
                f = self.fake(arm_says=[msg])
                code, out = self.update(f)
                self.assertEqual((code, f.armed, self.arms(f)), (0, self.up, [self.up] * 2), out)

    def test_a_check_that_stays_queued_leaves_it_off_after_bounded_tries(self):
        f = self.fake(arm_says=[QUEUED] * 50)
        code, out = self.update(f)
        self.assertEqual(self.sleeps, [5, 10, 15, 20], 'the waits grow and end')
        self.assertLessEqual(sum(self.sleeps), CFG.wait_seconds)
        self.assertLeftOff(f, code, 'is queued', tries=5)
        self.assertIn('tried again for up to 60 s', f.comments[0])

    def test_the_waits_stay_within_the_configs_wait_seconds(self):
        for wait, poll, waits in ((5, 1, [1, 2]), (5, 5, [5]), (60, 60, [60]), (600, 60, [60, 120, 180, 240]),
                                  (59, 30, [30])):
            with self.subTest(wait=wait, poll=poll):
                sleeps = []
                q = make_queue(self.w.dir, config.parse(json.dumps({**CFG_RAW, 'wait_seconds': wait,
                                                                    'poll_seconds': poll})), sleeps, self.w.remote)
                f = self.fake(arm_says=[QUEUED] * 50, armed=False)
                q.gh = q.gh_write = f
                r = run_quiet(q.arm, 7, self.up)[0]
                self.assertEqual((r.returncode, sleeps, len(self.arms(f))), (1, waits, len(waits) + 1))
                self.assertLessEqual(sum(sleeps), wait)

    def test_any_other_refusal_is_final_at_once(self):
        for msg in (MOVED, 'Pull request is not mergeable', 'HTTP 502', '',
                    'Required status check "build" is failing.', 'Required status check "build" has not succeeded.',
                    'Required status check "build" is failing. The merge queue is expected to run later.',
                'Required status check "test (3.12)" is failing. The merge queue is expected to run later.'):
            with self.subTest(msg=msg):
                self.assertFalse(Q.CHECK_NOT_DONE_RE.search(msg))
                self.sleeps.clear()
                f = self.fake(arm_says=[msg or ' ', QUEUED])   # ' ': the fake arms on an empty answer
                code, out = self.update(f)
                self.assertEqual(self.sleeps, [])
                self.assertLeftOff(f, code, 'back on', tries=1)
                self.assertNotIn('tried again', f.comments[0])

    def test_a_head_that_moved_during_the_wait_is_never_armed(self):   # --match-head-commit on every try
        f = self.fake(arm_says=[QUEUED, MOVED])
        code, out = self.update(f)
        self.assertEqual(self.sleeps, [5])
        self.assertLeftOff(f, code, 'Head branch was modified', tries=2)

    def test_a_run_killed_in_the_wait_is_finished_by_the_next_run(self):   # stops half-way: STATE is still there
        f = self.fake(arm_says=[QUEUED])

        def killed(seconds):
            raise Killed('sleep')
        self.q.sleep = killed
        with self.assertRaises(Killed):
            self.update(f)
        self.assertEqual((f.armed, f.comments), (None, []))
        self.assertTrue(self.plan_exists(), 'the plan must outlive a run killed between two tries')
        self.q.sleep = self.sleeps.append
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertFalse(self.plan_exists())

    def test_the_next_run_waits_out_the_race_too(self):   # recover(): the same arm, the same rule
        self.q.write_state({'repo': REPO, 'pr': 7, 'old': self.c, 'base': 'main', 'read': self.c})
        f = self.fake(armed=False, arm_says=[QUEUED, QUEUED])
        self.q.gh = self.q.gh_write = f
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, f.comments, self.sleeps), (0, self.c, [], [5, 10]), out)
        self.assertFalse(self.plan_exists())

    def test_the_next_run_says_it_once_when_the_check_stays_queued(self):
        self.q.write_state({'repo': REPO, 'pr': 7, 'old': self.c, 'base': 'main', 'read': self.c})
        f = self.fake(armed=False, arm_says=[QUEUED] * 50)
        self.q.gh = self.q.gh_write = f
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, len(f.comments), self.sleeps), (0, None, 1, [5, 10, 15, 20]), out)
        self.assertIn('is queued', f.comments[0])
        self.assertFalse(self.plan_exists())
        code, out = run_quiet(self.q.recover)   # a re-run: no plan, so nothing is tried or said again
        self.assertEqual((code, len(f.comments)), (0, 1))


class HoldsReadsAnyFileName(unittest.TestCase):   # #374 item 5: merge-tree names a conflicted file as git stores it
    """A conflict lists file names in merge-tree's output, and a name need not be UTF-8: a strict decode raised
    UnicodeDecodeError out of holds. Not the same change (a conflict never gives the head's tree), said clearly."""

    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='pr-queue-holds-')
        self.addCleanup(shutil.rmtree, self.d, True)
        World.git(self.d, 'init', '-q', '-b', 'main')

    def commit(self, text, parents=()):
        with open(os.path.join(os.fsencode(self.d), b'bad\xff.txt'), 'w') as f:
            f.write(text)
        World.git(self.d, 'add', '-A')
        tree = World.git(self.d, 'write-tree')
        return World.git(self.d, 'commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', 'c')

    def git(self, *a):   # as Queue.git and head_check's Checkout.git decode: strictly
        return subprocess.run(['git', *a], cwd=self.d, text=True, encoding='utf-8', errors='strict',
                              capture_output=True, env={**os.environ, **GIT_ENV})

    def test_a_conflict_on_a_file_name_that_is_not_utf8_is_no_same_change(self):
        m0 = self.commit('a\n')
        read, m1 = self.commit('read\n', [m0]), self.commit('base\n', [m0])
        World.git(self.d, 'update-ref', 'refs/queue/base', m1)
        head = self.commit('resolved\n', [m1, read])   # a conflict resolution: never the read change alone
        real = subprocess.run(['git', 'merge-tree', '--write-tree', m1, read], cwd=self.d, capture_output=True)
        self.assertEqual(real.returncode, 1)   # a real conflict, naming the file as git stores it
        self.assertIn(b'bad\xff.txt', real.stdout)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIs(Q.holds(self.git, head, read), False)
        said = out.getvalue()
        self.assertIn('git printed bytes that are not UTF-8', said)
        self.assertIn('not taken as the change read', said)
        self.assertIs(Q.holds(self.git, read, read), True)   # the read itself: no git at all


class HoldsIgnoresTheJobsAttributeSource(unittest.TestCase):   # #442 item 1: GIT_ATTR_SOURCE is in GIT_DROP
    """GIT_ATTR_SOURCE makes git read attributes from a tree of its choice. Naming one that holds `* merge=union`
    makes merge-tree union-merge a conflict, so a conflict resolution nobody read would be the merge's own tree."""

    def test_a_conflict_stays_a_conflict_whatever_tree_the_job_names(self):
        w = World()
        self.addCleanup(w.close)
        m0 = w.commit({'c.json': '{"a": 1}\n'})
        read = w.commit({'c.json': '{"a": 2}\n'}, [m0])
        m1 = w.commit({'c.json': '{"a": 3}\n', 'u/.gitattributes': '* merge=union\n'}, [m0])
        attrs = w.git(w.work, 'rev-parse', f'{m1}:u')
        union = subprocess.run(['git', 'merge-tree', '--write-tree', m1, read], cwd=w.work, text=True,
                               capture_output=True, env={**os.environ, 'GIT_ATTR_SOURCE': attrs})
        self.assertEqual(union.returncode, 0, 'no union merge with GIT_ATTR_SOURCE on this git: the test reads nothing')
        head = w.keep(w.git(w.work, 'commit-tree', union.stdout.split()[0], '-p', read, '-p', m1, '-m', 'union'))
        w.set('refs/heads/main', m1)
        w.set('refs/pull/7/head', head)
        q = make_queue(w.dir, remote=w.remote)
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertIs(q.fetch('main', 7, read), True)
            for _ in range(2):   # runs again: the same answer
                with unittest.mock.patch.dict(os.environ, {'GIT_ATTR_SOURCE': attrs}):
                    self.assertIs(Q.holds(q.git, head, read), False)


class HoldsReadsNoAttributesOfTheCache(unittest.TestCase):   # #450 item 1
    """A bare repository's HEAD is an attribute source (git 2.43 to 2.45 read `HEAD:.gitattributes` there), and so is
    `attr.tree` (2.43+). The queue's cache is kept between runs: an earlier job on the runner (the same user) could
    point HEAD at a commit whose `.gitattributes` holds `* merge=union`, and merge-tree would union-merge a conflict.
    Every git call of the queue reads attributes from the empty tree (GIT_ATTR_SOURCE), and the cache's HEAD must be
    the one the queue wrote, never resolving to a commit."""

    def setUp(self):
        w = self.w = World()
        self.addCleanup(w.close)
        m0 = w.commit({'c.json': '{"a": 1}\n'})
        self.read = w.commit({'c.json': '{"a": 2}\n'}, [m0])
        self.m1 = w.commit({'c.json': '{"a": 3}\n'}, [m0])
        self.evil = w.commit({'c.json': '{"a": 3}\n', '.gitattributes': '* merge=union\n'}, [self.m1])
        union = subprocess.run(['git', '-c', f'attr.tree={self.evil}', 'merge-tree', '--write-tree', self.m1,
                                self.read], cwd=w.work, text=True, capture_output=True, env={**os.environ, **GIT_ENV})
        self.assertEqual(union.returncode, 0, 'no union merge through attr.tree on this git: the test reads nothing')
        self.head = w.keep(w.git(w.work, 'commit-tree', union.stdout.split()[0], '-p', self.read, '-p', self.m1,
                                 '-m', 'union'))
        w.set('refs/heads/main', self.m1)
        w.set('refs/pull/7/head', self.head)
        self.q = make_queue(w.dir, remote=w.remote)
        got, out = run_quiet(self.q.fetch, 'main', 7, self.read, self.evil)
        self.assertIs(got, True, out)
        self.cache_git('update-ref', 'refs/queue/evil', self.evil)

    def cache_git(self, *a):
        return World.git(self.w.dir, '--git-dir', self.q.cache, *a)

    def plant_head(self, text):
        with open(os.path.join(self.q.cache, 'HEAD'), 'w') as f:
            f.write(text)

    def test_every_git_call_reads_attributes_from_the_empty_tree(self):
        self.assertEqual(Q.git_env({})['GIT_ATTR_SOURCE'], Q.EMPTY_TREE)
        self.assertEqual(Q.git_env({'GIT_ATTR_SOURCE': self.evil}, 'https://github.com/o/r.git')['GIT_ATTR_SOURCE'],
                         Q.EMPTY_TREE)
        self.assertEqual(Q.EMPTY_TREE, World.git(self.w.dir, 'hash-object', '-t', 'tree', '/dev/null'))
        for _ in range(2):   # runs again: the same answer; attr.tree stands for any source git may read
            self.assertIs(Q.holds(lambda *a: self.q.git('-c', f'attr.tree={self.evil}', *a), self.head, self.read),
                          False)

    def test_a_planted_head_is_no_attribute_source(self):   # the lens's case
        self.plant_head('ref: refs/queue/evil\n')
        for _ in range(2):
            self.assertIs(Q.holds(self.q.git, self.head, self.read), False)

    def test_the_cache_is_made_with_the_queues_head_and_kept_with_it(self):
        with open(os.path.join(self.q.cache, 'HEAD')) as f:
            self.assertEqual(f.read(), Q.CACHE_HEAD)
        self.assertIsNone(self.q.cache_problem())
        self.assertEqual(self.cache_git('symbolic-ref', 'HEAD'), 'refs/heads/master')   # what git init writes, too
        for _ in range(2):   # runs again: kept, not rebuilt
            got, out = run_quiet(self.q.fetch, 'main', 7, self.read)
            self.assertIs(got, True, out)
            self.assertNotIn('rebuilding', out)

    def test_a_cache_whose_head_the_queue_did_not_write_is_rebuilt(self):
        def symlink():
            os.remove(os.path.join(self.q.cache, 'HEAD'))
            os.symlink(os.path.join(self.q.cache, 'refs', 'queue', 'evil'), os.path.join(self.q.cache, 'HEAD'))

        def missing():
            os.remove(os.path.join(self.q.cache, 'HEAD'))

        def born():   # HEAD as written, but its branch made: HEAD names a commit
            self.cache_git('update-ref', 'refs/heads/master', self.evil)

        for name, plant, says in (('a ref to the commit', lambda: self.plant_head('ref: refs/queue/evil\n'), 'HEAD'),
                                  ('a detached HEAD', lambda: self.plant_head(self.evil + '\n'), 'HEAD'),
                                  ('an empty HEAD', lambda: self.plant_head(''), 'HEAD'),
                                  ('a symlink', symlink, 'HEAD'), ('no HEAD', missing, 'HEAD'),
                                  ('the queue\'s HEAD, born', born, 'HEAD names a commit')):
            with self.subTest(name):
                plant()
                self.assertIn(says, self.q.cache_problem() or '')
                got, out = run_quiet(self.q.fetch, 'main', 7, self.read, self.evil)   # the next run
                self.assertIs(got, True, out)
                self.assertIn('rebuilding it', out)
                self.assertIsNone(self.q.cache_problem())
                self.assertIs(Q.holds(self.q.git, self.head, self.read), False)
                self.cache_git('update-ref', 'refs/queue/evil', self.evil)

    def test_a_head_planted_between_two_runs_is_found_by_the_next(self):   # stopped after the fetch, before merge-tree
        self.plant_head('ref: refs/queue/evil\n')
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIs(self.q.same_change('main', 7, self.head, self.read), False)
        self.assertIn('rebuilding it', out.getvalue())


class HoldsChecksTheCachesObjects(unittest.TestCase):   # #457 item 1
    """git checks an object's hash on only some reads, never of a tree or blob merge-tree reads. The cache is kept
    between runs: an earlier job on the runner (the same user) could write another tree or blob under the name of one
    side's, and merge-tree would merge a real conflict clean. Before each merge check the queue runs git fsck on the
    cache; a cache it finds broken is set aside and fetched again, never merged in."""

    def setUp(self):
        w = self.w = World()
        self.addCleanup(w.close)
        m0 = w.commit({'c.json': '{"a": 1}\n'})
        self.read = w.commit({'c.json': '{"a": 2}\n'}, [m0])
        self.m1 = w.commit({'c.json': '{"a": 3}\n'}, [m0])
        # what a planted tree makes merge-tree say: the base's side alone, the change read gone without a word
        self.head = w.keep(w.git(w.work, 'commit-tree', self.m1 + '^{tree}', '-p', self.read, '-p', self.m1,
                                 '-m', 'lost'))
        w.set('refs/heads/main', self.m1)
        w.set('refs/pull/7/head', self.head)
        self.run_again()
        got, out = run_quiet(self.q.fetch, 'main', 7, self.read)
        self.assertIs(got, True, out)
        self.assertIs(Q.holds(self.q.git, self.head, self.read), False, 'a real conflict')

    def run_again(self):
        """The next run: a new Queue on the same cache."""
        self.q = make_queue(self.w.dir, remote=self.w.remote)
        self.fscks, self.merges = [], []   # merges: the repository of each merge-tree (None: the cache)
        git = self.plain_git = self.q.git

        def counted(*a, **kw):
            if 'fsck' in a:
                self.fscks.append(a)
            if 'merge-tree' in a:
                self.merges.append(kw.get('repo'))
            return git(*a, **kw)
        self.q.git = counted

    def cache_git(self, *a):
        return World.git(self.w.dir, '--git-dir', self.q.cache, *a)

    def plant(self, kind='tree'):
        """The read side's tree object replaced by the base side's tree, at the same name (a loose object, as a small
        fetch leaves it: git reads a packed object before a loose one, so a packed one is replaced by a pack). `blob`:
        the read side's c.json replaced by the merge base's (merge-tree reads a blob's content only when the three
        names differ, and then takes the base side's change: clean)."""
        if kind == 'tree':
            victim, donor = self.cache_git('rev-parse', self.read + '^{tree}', self.m1 + '^{tree}').split()
        else:
            victim, donor = self.cache_git('rev-parse', self.read + ':c.json', self.read + '^:c.json').split()
        body = subprocess.run(['git', '--git-dir', self.q.cache, 'cat-file', kind, donor], capture_output=True,
                              check=True).stdout
        self.write_object(victim, kind.encode() + b' %d\0' % len(body) + body)
        self.assertIs(Q.holds(self.plain_git, self.head, self.read), True,
                      f'the planted {kind} does not merge the conflict clean on this git: the test reads nothing')

    def write_object(self, name, raw):
        """`raw` (zlib-compressed) as the loose object `name`, over the one a small fetch left there."""
        path = os.path.join(self.q.cache, 'objects', name[:2], name[2:])
        self.assertTrue(os.path.exists(path), 'a small fetch leaves loose objects')
        os.chmod(path, 0o644)
        with open(path, 'wb') as f:
            f.write(zlib.compress(raw))

    def same_change(self):
        return run_quiet(self.q.same_change, 'main', 7, self.head, self.read)

    def test_a_replaced_tree_object_makes_the_run_refuse(self):   # the lens's case
        for planted in range(2):   # planted again in the rebuilt cache: refused again
            with self.subTest(planted=planted):
                self.plant()
                self.run_again()
                same, out = self.same_change()
                self.assertIs(same, False, out)
                self.assertIn('git fsck', out)
                self.assertIn('rebuilding it', out)
                fsck = subprocess.run(['git', '--git-dir', self.q.cache, 'fsck', '--no-dangling'], capture_output=True)
                self.assertEqual(fsck.returncode, 0, 'the rebuilt cache holds only what was fetched')
                for _ in range(2):   # runs again: the rebuilt cache is kept and gives the same answer
                    self.run_again()
                    same, out = self.same_change()
                    self.assertIs(same, False, out)
                    self.assertNotIn('rebuilding', out)
                    self.assertEqual(len(self.fscks), 1, 'one check before the merge check')

    def test_a_cache_still_broken_after_the_rebuild_is_never_merged(self):   # nothing is known: STATE stays
        self.q.objects_problem = lambda: 'has objects git fsck finds broken (planted)'
        with unittest.mock.patch.object(Q, 'holds', side_effect=AssertionError('merged in a broken cache')):
            for _ in range(2):
                same, out = self.same_change()
                self.assertIsNone(same, out)
                self.assertIn('fetched again', out)

    def test_a_kept_healthy_cache_is_checked_and_kept(self):   # a cache from before the fix: the same files
        for _ in range(2):
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertNotIn('rebuilding', out)
            self.assertEqual(len(self.fscks), 1)
        self.cache_git('repack', '-adq')   # git's own gc packs it: still the queue's
        self.run_again()
        same, out = self.same_change()
        self.assertIs(same, False, out)
        self.assertNotIn('rebuilding', out)

    def test_a_new_empty_cache_is_made_fetched_and_checked(self):
        shutil.rmtree(self.q.cache)
        for _ in range(2):
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertNotIn('rebuilding', out)
            self.assertEqual(len(self.fscks), 1)

    def test_a_fetch_stopped_half_way_leaves_a_cache_that_still_answers(self):   # killed while writing objects
        for name in (os.path.join('objects', 'pack', 'tmp_pack_x1Y2z3'), os.path.join('objects', 'ab', 'tmp_obj_x')):
            path = os.path.join(self.q.cache, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, 'wb') as f:
                f.write(b'PACK\0\0\0\2half')
        for _ in range(2):
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)


    def test_a_replaced_blob_object_makes_the_run_refuse(self):   # state lens 4: not only trees (and no
        self.plant('blob')                                          # --connectivity-only, which misses it)
        for again in range(2):   # rebuilt once, then kept with the same answer
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            (self.assertNotIn if again else self.assertIn)('rebuilding', out)

    def test_a_replaced_object_no_ref_reaches_makes_the_run_refuse(self):   # M16: git fsck says exit 1 alone
        """The change read was force-pushed away: the queue fetched it by sha, so no ref in the cache reaches its
        tree, and git fsck finds the planted tree with exit code 1 only (no missing object: nothing reachable is
        broken). Any non-zero exit is a broken cache."""
        lost = self.w.keep(self.w.git(self.w.work, 'commit-tree', self.m1 + '^{tree}', '-p', self.m1, '-m', 'lost'))
        self.w.set('refs/pull/7/head', lost)
        self.head = lost
        self.run_again()
        got, out = run_quiet(self.q.fetch, 'main', 7, self.read)
        self.assertIs(got, True, out)
        self.assertIs(Q.holds(self.q.git, self.head, self.read), False, 'a real conflict')
        self.plant()
        fsck = subprocess.run(['git', '--git-dir', self.q.cache, *Q.FSCK_IGNORE, 'fsck', '--no-dangling',
                               '--no-reflogs'], capture_output=True)
        self.assertEqual(fsck.returncode, 1, 'the planted tree is reachable: the test reads nothing new')
        self.run_again()
        same, out = self.same_change()
        self.assertIs(same, False, out)
        self.assertIn('rebuilding it', out)

    def odd_base(self):
        """main moves to a commit with time zone +99999: old imports have such commits, `git fetch` takes them and
        plain `git fsck` calls them badTimezone."""
        raw = (f'tree {self.cache_git("rev-parse", self.m1 + "^{tree}")}\nparent {self.m1}\n'
               'author t <t@t> 1700000000 +99999\ncommitter t <t@t> 1700000000 +99999\n\nold import\n')
        odd = self.w.keep(subprocess.run(['git', 'hash-object', '--literally', '-t', 'commit', '-w', '--stdin'],
                                         cwd=self.w.work, input=raw, text=True, capture_output=True,
                                         check=True).stdout.strip())
        self.w.set('refs/heads/main', odd)
        return odd

    def test_old_format_history_github_serves_is_no_broken_cache(self):   # state lens 1: never a refetch loop
        self.odd_base()
        got, out = run_quiet(self.q.fetch, 'main', 7, self.read)
        self.assertIs(got, True, out)
        plain = subprocess.run(['git', '--git-dir', self.q.cache, 'fsck', '--no-dangling'], text=True,
                               capture_output=True)
        self.assertIn('badTimezone', plain.stdout + plain.stderr,
                      'plain git fsck takes this commit: the test reads nothing')
        for _ in range(3):   # runs again: an answer every time, never a rebuild
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertNotIn('rebuilding', out)
            self.assertEqual(len(self.fscks), 1)
        self.plant()   # what the check is for still counts with the old format ignored
        self.run_again()
        same, out = self.same_change()
        self.assertIs(same, False, out)
        self.assertIn('rebuilding it', out)

    def test_a_missing_object_is_still_broken_with_the_old_format_ignored(self):   # FSCK_FORMAT_IDS names no loss
        self.odd_base()
        self.assertIs(run_quiet(self.q.fetch, 'main', 7, self.read)[0], True)
        blob = self.cache_git('rev-parse', self.read + ':c.json')
        os.remove(os.path.join(self.q.cache, 'objects', blob[:2], blob[2:]))
        for again in range(2):
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            (self.assertNotIn if again else self.assertIn)('rebuilding', out)

    def test_a_fresh_download_that_fails_the_same_way_is_not_downloaded_again(self):   # state lens 1: cost
        self.odd_base()   # as a newer git's fsck may refuse what no FSCK_IGNORE id names
        fetches = []
        real_fetch = Q.Queue.fetch

        def fetch(q, *a):
            fetches.append(a)
            return real_fetch(q, *a)
        with unittest.mock.patch.object(Q, 'FSCK_IGNORE', (), create=True), \
                unittest.mock.patch.object(Q.Queue, 'fetch', fetch), \
                unittest.mock.patch.object(Q, 'holds', side_effect=AssertionError('merged in a broken cache')):
            self.run_again()
            same, out = self.same_change()
            self.assertIsNone(same, out)
            self.assertIn('rebuilding it', out)
            self.assertEqual(len(fetches), 2, 'one download from scratch')
            for _ in range(2):   # the next runs: the same lines from a fresh download, so no download again
                fetches.clear()
                self.run_again()
                same, out = self.same_change()
                self.assertIsNone(same, out)
                self.assertNotIn('rebuilding', out)
                self.assertIn('the same way', out)
                self.assertIn('fails git fsck', self.q.unknown)
                self.assertEqual(len(fetches), 1, out)
            with unittest.mock.patch.object(Q, 'holds', REAL_HOLDS):
                self.plant()   # a new broken object is another answer: rebuilt again
            fetches.clear()
            self.run_again()
            same, out = self.same_change()
            self.assertIsNone(same, out)
            self.assertIn('rebuilding it', out)
            self.assertEqual(len(fetches), 2)

    def test_a_fetch_a_broken_object_fails_is_rebuilt_once(self):   # trust lens 1: never stuck on "could not fetch"
        head = self.w.keep(self.w.git(self.w.work, 'commit-tree', self.m1 + '^{tree}', '-p', self.read, '-p', self.m1,
                                      '-m', 'lost again'))
        self.w.set('refs/pull/7/head', head)
        os.makedirs(os.path.join(self.q.cache, 'objects', head[:2]), exist_ok=True)
        path = os.path.join(self.q.cache, 'objects', head[:2], head[2:])
        with open(path, 'wb') as f:   # a corrupt object under the name the next fetch needs
            f.write(zlib.compress(b'blob 5\0evil\n'))
        for _ in range(2):   # the first run rebuilds; the next keeps it and answers the same
            self.run_again()
            same, out = run_quiet(self.q.same_change, 'main', 7, head, self.read)
            self.assertIs(same, False, out)
        self.assertNotIn('rebuilding', out)
        self.assertFalse(os.path.exists(path) and open(path, 'rb').read() == zlib.compress(b'blob 5\0evil\n'))

    def test_a_fetch_that_fails_on_the_network_runs_no_check(self):   # only a cause in the cache is checked
        self.q.remote = 'https://localhost:1/o/r.git'   # nothing listens there: "unable to access"
        self.run_again()
        self.q.remote = 'https://localhost:1/o/r.git'
        same, out = self.same_change()
        self.assertIsNone(same, out)
        self.assertEqual(self.fscks, [])
        self.assertIn('could not fetch', self.q.unknown)

    # --- #465 item 1: an object written after the cache's check and before merge-tree --------------------------------
    def plant_after_the_check(self, plant, times=1):
        """The next run, in which `plant` runs right after the run's first `times` git fscks returned: what another
        process that can write the cache does while the run goes on (in an update the check ends minutes before
        merge-tree)."""
        self.run_again()
        git, left = self.q.git, [times]

        def then_plant(*a, **kw):
            r = git(*a, **kw)
            if 'fsck' in a and left[0]:
                left[0] -= 1
                plant()
            return r
        self.q.git = then_plant

    def assert_merged_outside_the_cache(self):
        self.assertTrue(self.merges, 'a merge check ran')
        self.assertEqual([r for r in self.merges if r != self.q.cache + Q.MERGE_COPY], [],
                         'every merge-tree ran in the copy, never in the cache')
        self.assertFalse(os.path.lexists(self.q.cache + Q.MERGE_COPY), 'and the copy is gone after the check')

    def test_an_object_changed_after_the_check_is_never_merged_in(self):
        for kind in ('tree', 'blob'):
            with self.subTest(kind=kind):
                self.plant_after_the_check(lambda: self.plant(kind))
                same, out = self.same_change()
                self.assertIs(same, False, out)   # the real answer, in the same run
                self.assertIn('although git fsck passed', out)
                self.assertIn('does not hash to its name', out)
                self.assertIn('rebuilding it', out)
                self.assertEqual(len(self.fscks), 2, 'the check that passed, and the rebuilt cache\'s')
                self.assert_merged_outside_the_cache()
                self.assertFalse(os.path.lexists(self.q.known), 'a check that passed leaves no record')
                self.run_again()   # the next run: the rebuilt cache is kept
                same, out = self.same_change()
                self.assertIs(same, False, out)
                self.assertNotIn('rebuilding', out)

    def test_an_object_changed_again_after_the_rebuild_is_not_merged_in(self):
        """Whoever changes the cache keeps at it: the run knows nothing (STATE stays), and the next run's git fsck
        finds what was left there."""
        self.plant_after_the_check(lambda: self.plant('tree'), times=2)
        same, out = self.same_change()
        self.assertIsNone(same, out)
        self.assertIn('rebuilding it', out)
        self.assertIn('fetched again from GitHub, still', out)
        self.assertEqual(self.merges, [], 'no merge-tree ran at all')
        self.assertIn('not what its name says', self.q.unknown)
        self.assertNotIn(self.w.dir, self.q.unknown, 'no runner path in a PR comment')
        with open(self.q.known) as f:
            self.assertRegex(f.read(), r'^merge check: object [0-9a-f]{40} does not hash to its name$')
        self.run_again()   # the next run: git fsck finds the planted tree, one more download, the real answer
        same, out = self.same_change()
        self.assertIs(same, False, out)
        self.assertIn('rebuilding it', out)
        self.assertFalse(os.path.lexists(self.q.known), 'a check that passed drops the record')

    def test_a_download_that_fails_the_merge_check_the_same_way_is_not_downloaded_again(self):
        """History GitHub serves that git fsck passes and the copy refuses (it should not happen): one download from
        scratch, then no more, and a person must look. A later check that passes ends it."""
        fetches, real_fetch = [], Q.Queue.fetch

        def fetch(q, *a):
            fetches.append(a)
            return real_fetch(q, *a)

        def refuse(q, head, read):
            raise Q.Changed('object ' + '12' * 20 + ' does not hash to its name')
        with unittest.mock.patch.object(Q.Queue, 'fetch', fetch):
            with unittest.mock.patch.object(Q.Queue, 'holds_checked', refuse):
                self.run_again()
                same, out = self.same_change()
                self.assertIsNone(same, out)
                self.assertIn('rebuilding it', out)
                self.assertEqual(len(fetches), 2, 'one download from scratch')
                for _ in range(2):
                    fetches.clear()
                    self.run_again()
                    same, out = self.same_change()
                    self.assertIsNone(same, out)
                    self.assertNotIn('rebuilding', out)
                    self.assertIn('a person must look', out)
                    self.assertIn('a person must look', self.q.unknown)
                    self.assertEqual(len(fetches), 1, out)
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertFalse(os.path.lexists(self.q.known))

    def test_a_commit_changed_after_the_check_is_never_merged_in(self):
        """git checks no hash of a commit it walks. main's history with one more parent on an older commit puts a
        commit on the base that never was there: a head that merges the change read into that commit would count."""
        w = self.w
        evil = w.commit({'c.json': '{"a": 1}\n', 'evil.sh': 'x\n'}, [w.git(w.work, 'rev-parse', self.m1 + '^')])
        m2 = w.commit({'c.json': '{"a": 3}\n', 'more': 'y\n'}, [self.m1])
        head = w.merge(self.read, evil)   # parents: the change read, and a commit that is not on main
        w.set('refs/heads/main', m2)
        w.set('refs/pull/7/head', head)
        got, out = run_quiet(self.q.fetch, 'main', 7, self.read)
        self.assertIs(got, True, out)
        self.assertIs(Q.holds(self.plain_git, head, self.read), False, 'evil is not on main')

        def plant():
            body = subprocess.run(['git', '--git-dir', self.q.cache, 'cat-file', 'commit', self.m1],
                                  capture_output=True, check=True).stdout
            body = body.replace(b'\nauthor ', b'\nparent ' + evil.encode() + b'\nauthor ', 1)
            self.write_object(self.m1, b'commit %d\0' % len(body) + body)
            self.assertIs(Q.holds(self.plain_git, head, self.read), True,
                          'git checked the hash of a commit it walked: the test reads nothing')
        self.plant_after_the_check(plant)
        same, out = run_quiet(self.q.same_change, 'main', 7, head, self.read)
        self.assertIs(same, False, out)
        self.assertIn(f'object {self.m1} does not hash to its name', out)
        self.assertIn('rebuilding it', out)
        self.assertEqual(self.merges, [], 'no parent of the head is on main: nothing to merge')

    def test_a_missing_object_git_fsck_does_not_see_is_fetched_again(self):
        """The change read is in the cache by its sha only (no ref reaches it), and one of its blobs is gone: git
        fsck says nothing of an object missing under a commit no ref reaches, and merge-tree stops on it. The run
        downloads the cache again and answers; it does not call the head another change."""
        w = self.w
        m0 = w.commit({'f': lines()})
        main = w.commit({'f': lines(extra=['main'], at=0)}, [m0])
        read = w.commit({'f': lines(extra=['new'], at=20)}, [m0])
        tree = w.git(w.work, 'rev-parse', w.merge(read, main) + '^{tree}')
        head = w.keep(w.git(w.work, 'commit-tree', tree, '-p', main, '-m', 'the change, on main'))
        w.set('refs/heads/main', main)
        w.set('refs/pull/7/head', head)
        self.assertIs(run_quiet(self.q.fetch, 'main', 7, read)[0], True)
        self.assertIs(Q.holds(self.plain_git, head, read), True)
        blob = self.cache_git('rev-parse', read + ':f')
        os.remove(os.path.join(self.q.cache, 'objects', blob[:2], blob[2:]))
        self.assertIsNone(self.q.objects_problem(), 'git fsck sees the missing blob: the test reads nothing')
        self.assertIs(Q.holds(self.plain_git, head, read), False, 'merge-tree does not need the blob')
        self.run_again()
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertIn(f'object {blob} is missing', out)
        self.assertIn('rebuilding it', out)
        self.run_again()
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertNotIn('rebuilding', out)

    def criss_cross(self):
        """A PR whose change read and main have two merge bases (each merged the other's first commit): git merges
        the two into a tree of its own, in no commit, and writes it before the real merge. head: what update-branch
        makes; lost: main's tree with the parents of an update, the change read gone."""
        w = self.w
        m0 = w.commit({'a': '1\n', 'b': '1\n', 'c': '1\n'})
        x = w.commit({'a': '2\n', 'b': '1\n', 'c': '1\n'}, [m0])
        y = w.commit({'a': '1\n', 'b': '2\n', 'c': '1\n'}, [m0])
        main = w.commit({'a': '2\n', 'b': '2\n', 'c': '1\n', 'm': '1\n'}, [y, x])
        read = w.commit({'a': '2\n', 'b': '2\n', 'c': '2\n'}, [x, y])
        self.assertEqual(len(w.git(w.work, 'merge-base', '--all', main, read).split()), 2)
        head = w.merge(read, main)
        lost = w.keep(w.git(w.work, 'commit-tree', main + '^{tree}', '-p', read, '-p', main, '-m', 'lost'))
        w.set('refs/heads/main', main)
        w.set('refs/pull/7/head', head)
        return argparse.Namespace(head=head, read=read, lost=lost)

    def test_two_merge_bases_merge_in_the_copy(self):
        c = self.criss_cross()
        for _ in range(2):
            self.run_again()
            same, out = run_quiet(self.q.same_change, 'main', 7, c.head, c.read)
            self.assertIs(same, True, out)
            self.assertEqual(len(self.fscks), 1)
            self.assert_merged_outside_the_cache()
        self.w.set('refs/pull/7/head', c.lost)
        self.run_again()
        same, out = run_quiet(self.q.same_change, 'main', 7, c.lost, c.read)
        self.assertIs(same, False, out)

    def test_more_merge_bases_than_the_cap_is_not_the_change_read(self):
        c = self.criss_cross()
        self.run_again()
        with unittest.mock.patch.object(Q, 'MERGE_BASES', 1):
            same, out = run_quiet(self.q.same_change, 'main', 7, c.head, c.read)
        self.assertIs(same, False, out)
        self.assertIn('more than 1 merge bases', out)
        self.assertEqual(self.merges, [], 'not merged')

    def clean_update(self):
        """(head, read): a change that merges clean with main, and what update-branch makes of it."""
        w = self.w
        read = w.commit({'c.json': '{"a": 1}\n', 'n/new': 'n\n'}, [w.git(w.work, 'rev-parse', self.m1 + '^')])
        head = w.merge(read, self.m1)
        w.set('refs/pull/7/head', head)
        return head, read

    def taken(self):
        """The next run, with the names of the objects its merge check copies in the list this returns."""
        self.run_again()
        names, real = [], Q.Copy.take

        def take(copy, name, kind):
            if name not in copy.seen:
                names.append(name)
            return real(copy, name, kind)
        patched = unittest.mock.patch.object(Q.Copy, 'take', take)
        patched.start()
        self.addCleanup(patched.stop)
        return names

    def test_what_no_side_changed_is_not_copied(self):   # the cost: a big tree, a small change
        w = self.w
        m0 = w.commit({'f': lines(), 'kept/big': 'big\n', 'kept/more/big': 'more\n', 'gone/old': 'old\n'})
        main = w.commit({'f': lines(extra=['main'], at=0), 'kept/big': 'big\n', 'kept/more/big': 'more\n',
                         'gone/old': 'old\n'}, [m0])
        read = w.commit({'f': lines(extra=['new'], at=20), 'kept/big': 'big\n', 'kept/more/big': 'more\n',
                         'here/old': 'old\n'}, [m0])   # and a folder renamed: all under it is read
        head = w.merge(read, main)
        w.set('refs/heads/main', main)
        w.set('refs/pull/7/head', head)
        names = self.taken()
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertEqual(len(self.merges), 1, 'git missed nothing')
        name = lambda what: w.git(w.work, 'rev-parse', what)   # noqa: E731
        for kept in ('kept', 'kept/big', 'kept/more', 'kept/more/big'):
            self.assertNotIn(name(f'{m0}:{kept}'), names, kept)
        for needed in (f'{m0}:f', f'{main}:f', f'{read}:f', f'{m0}:gone', f'{m0}:gone/old', f'{read}:here',
                       f'{m0}^{{tree}}', f'{main}^{{tree}}', f'{read}^{{tree}}', f'{head}^{{tree}}', m0, main, head):
            self.assertIn(name(needed), names, needed)
        self.assertEqual(len(names), len(set(names)), 'each object once')

    def test_a_tree_that_holds_a_name_twice_is_copied_whole(self):
        """Old history has trees with one name twice, and the cache's git fsck lets them pass (duplicateEntries):
        all under each entry is copied, and the copy answers as git does in the cache."""
        w = self.w

        def twice(files, *parents):   # `d` a file too, next to the folder d
            listed = w.git(w.work, 'ls-tree', w.commit(files, parents)).splitlines()
            blob = w.git(w.work, 'rev-parse', self.m1 + ':c.json')
            body = b''
            for mode, _, oid, entry in [('100644', 'blob', blob, 'd')] + [re.split(r'[ \t]', e, 3) for e in listed]:
                body += mode.lstrip('0').encode() + b' ' + entry.encode() + b'\0' + bytes.fromhex(oid)
            tree = subprocess.run(['git', 'hash-object', '-t', 'tree', '--literally', '-w', '--stdin'], cwd=w.work,
                                  input=body, capture_output=True, check=True).stdout.decode().strip()
            return w.keep(w.git(w.work, 'commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', 'twice'))
        m0 = twice({'d/x': '1\n', 'f': lines()})
        main = twice({'d/x': '1\n', 'f': lines(extra=['main'], at=0)}, m0)
        read = twice({'d/x': '2\n', 'd/in/y': 'y\n', 'f': lines(extra=['new'], at=20)}, m0)
        head = w.keep(w.git(w.work, 'commit-tree', main + '^{tree}', '-p', read, '-p', main, '-m', 'update'))
        w.set('refs/heads/main', main)
        w.set('refs/pull/7/head', head)
        self.assertIs(run_quiet(self.q.fetch, 'main', 7, read)[0], True)
        expected = Q.holds(self.plain_git, head, read)
        names = self.taken()
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, expected, out)
        self.assertNotIn('rebuilding', out)
        self.assertEqual(len(self.fscks), 1)
        for needed in ('{"a": 3}\n', '1\n', '2\n', 'y\n'):   # the file d, and all in the folders d
            blob = subprocess.run(['git', 'hash-object', '--stdin'], input=needed, text=True, capture_output=True,
                                  check=True).stdout.strip()
            self.assertIn(blob, names, needed)

    def test_a_merge_that_misses_an_object_in_the_copy_gets_whole_trees(self):
        """Should the copy ever lack an object a merge reads, git stops (it never guesses): the trees are then
        copied whole and the merge runs once more."""
        head, read = self.clean_update()
        self.run_again()
        with unittest.mock.patch.object(Q.Copy, 'take_merged', lambda copy, roots: None):
            same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertEqual(len(self.merges), 2, 'the merge that stopped, and the one with whole trees')
        self.run_again()   # and with what the merge reads copied: one merge
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertEqual(len(self.merges), 1)
        self.assert_merged_outside_the_cache()

    def test_a_merge_git_calls_clean_with_an_error_is_not_clean(self):
        """A git that cannot read a commit's tree says so and merges it as an empty tree, exit 0."""
        head, read = self.clean_update()
        tree = self.w.git(self.w.work, 'rev-parse', head + '^{tree}')

        def merge_tree(git, p, side):
            git('merge-tree', '--write-tree', p, side)
            return subprocess.CompletedProcess([], 0, tree + '\n', 'error: Could not read ' + '12' * 20 + '\n')
        self.run_again()
        with unittest.mock.patch.object(Q, 'merge_tree', merge_tree):
            same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, False, out)
        self.assertIn('not taken as a clean merge', out)
        self.assertEqual(len(self.merges), 2, 'run once more with whole trees first')

    def test_a_submodule_entry_is_no_missing_object(self):
        """A tree's submodule entry (mode 160000) names a commit of another repository: the cache never holds it and
        no merge reads it."""
        w = self.w

        def with_sub(files, *parents):
            c = w.commit(files, parents)
            listed = w.git(w.work, 'ls-tree', c) + f'\n160000 commit {"12" * 20}\tsub\n'
            tree = subprocess.run(['git', 'mktree'], cwd=w.work, input=listed, text=True, capture_output=True,
                                  check=True).stdout.strip()
            return w.keep(w.git(w.work, 'commit-tree', tree, *sum((['-p', p] for p in parents), []), '-m', 'sub'))
        m0 = with_sub({'a': '1\n', 'b': '1\n'})
        read = with_sub({'a': '2\n', 'b': '1\n'}, m0)
        main = with_sub({'a': '1\n', 'b': '2\n'}, m0)
        moved = w.git(w.work, 'ls-tree', read).replace('12' * 20, '34' * 20) + '\n'   # the change moves the submodule
        tree = subprocess.run(['git', 'mktree'], cwd=w.work, input=moved, text=True, capture_output=True,
                              check=True).stdout.strip()
        read = w.keep(w.git(w.work, 'commit-tree', tree, '-p', m0, '-m', 'sub moved'))
        head = w.merge(read, main)
        self.assertIn('160000 commit ' + '34' * 20, w.git(w.work, 'ls-tree', head))
        w.set('refs/heads/main', main)
        w.set('refs/pull/7/head', head)
        self.run_again()
        same, out = run_quiet(self.q.same_change, 'main', 7, head, read)
        self.assertIs(same, True, out)
        self.assertEqual(len(self.merges), 1)

    def test_a_run_killed_in_the_merge_check_leaves_no_git_running(self):
        procs, real = [], subprocess.Popen

        def popen(*a, **kw):   # subprocess.run's own Popen too: only the object reader counts
            p = real(*a, **kw)
            if '--batch' in a[0]:
                procs.append(p)
            return p
        self.run_again()
        with unittest.mock.patch.object(Q.subprocess, 'Popen', popen), \
                unittest.mock.patch.object(Q, 'holds', side_effect=Killed('merge-tree')):
            with self.assertRaises(Killed):
                self.same_change()
        self.assertEqual([p.poll() is None for p in procs], [False], 'stopped and reaped')

    def test_what_a_killed_merge_check_left_is_deleted_by_the_next(self):   # kill -9: no finally ran
        left = self.q.cache + Q.MERGE_COPY
        elsewhere = os.path.join(self.w.dir, 'elsewhere')
        os.makedirs(os.path.join(elsewhere, 'objects'))

        def a_copy():   # with a tree of main under the name of the change read's: never read
            self.assertIsNone(Q.init_bare(left))
            victim, donor = self.cache_git('rev-parse', self.read + '^{tree}', self.m1 + '^{tree}').split()
            body = subprocess.run(['git', '--git-dir', self.q.cache, 'cat-file', 'tree', donor], capture_output=True,
                                  check=True).stdout
            os.makedirs(os.path.join(left, 'objects', victim[:2]))
            with open(os.path.join(left, 'objects', victim[:2], victim[2:]), 'wb') as f:
                f.write(zlib.compress(b'tree %d\0' % len(body) + body))

        def a_file():
            with open(left, 'w') as f:
                f.write('x')

        def a_link():
            os.symlink(elsewhere, left)
        for leave in (a_copy, a_file, a_link):
            with self.subTest(left=leave.__name__):
                leave()
                self.run_again()
                same, out = self.same_change()
                self.assertIs(same, False, out)
                self.assertNotIn('rebuilding', out)
                self.assert_merged_outside_the_cache()
        self.assertEqual(os.listdir(elsewhere), ['objects'], 'nothing written or deleted through the link')
        self.assertEqual(os.listdir(os.path.join(elsewhere, 'objects')), [])
        self.assertEqual([f for f in os.listdir(self.w.dir) if '.old-' in f], [], 'and nothing set aside is left')

    def test_a_copy_that_cannot_be_made_is_nothing_known(self):   # a full disk, a folder that cannot be made
        def no_room(*a, **kw):
            raise OSError(28, 'No space left on device')
        self.run_again()
        with unittest.mock.patch.object(Q.Copy, 'take', no_room):
            same, out = self.same_change()
        self.assertIsNone(same, out)
        self.assertIn('No space left', out)
        self.assertNotIn('rebuilding', out)   # the cache is not to blame
        self.assertIn('could not copy', self.q.unknown)
        self.assertNotIn(self.w.dir, self.q.unknown)
        self.assertFalse(os.path.lexists(self.q.cache + Q.MERGE_COPY))
        self.assertFalse(os.path.lexists(self.q.known))

    def test_a_reader_that_stops_inside_an_object_is_not_waited_for(self):
        """git cat-file ends in the middle of an object (killed, a pack cut short): the copy says so. It does not wait
        for the rest and does not take the part it got."""
        to, name = os.path.join(self.w.dir, 'copy'), 'a' * 40
        os.makedirs(os.path.join(to, 'objects'))
        half = f"import sys; sys.stdin.readline(); sys.stdout.write('{name} blob 10\\nabc')"
        copy = Q.Copy([sys.executable, '-I', '-c', half], None, to)
        self.addCleanup(copy.close)
        with self.assertRaises(Q.Changed) as e:
            copy.take(name, 'blob')
        self.assertIn('git cat-file stopped', str(e.exception))
        self.assertFalse(os.path.lexists(os.path.join(to, 'objects', name[:2], name[2:])))
        self.assertNotIn(name, copy.seen)

    def test_the_objects_are_copied_after_the_last_check_and_before_the_merge(self):
        """A healthy cache: the answer is as before, and the copy is made after the fetch and its check."""
        order, modes, real = [], [], subprocess.Popen

        def popen(*a, **kw):
            for what in ('fetch', 'fsck', '--batch', 'merge-tree'):
                if what in a[0] and order[-1:] != [what]:
                    order.append(what)
            if 'merge-tree' in a[0]:
                modes.append(os.lstat(self.q.cache + Q.MERGE_COPY).st_mode)
            return real(*a, **kw)
        for _ in range(2):
            self.run_again()
            with unittest.mock.patch.object(Q.subprocess, 'Popen', popen):
                same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertEqual(order, ['fetch', 'fsck', '--batch', 'merge-tree'], out)
            self.assertEqual(modes, [0o40700], 'a folder of its own that no one else can enter')
            self.assert_merged_outside_the_cache()
            order.clear()
            modes.clear()

    # --- #465 item 2: what a download from scratch still failed with is kept next to STATE, not in the cache ----------
    def test_a_record_inside_the_cache_is_never_read(self):
        """An older queue kept the record in the cache (queue-fsck-known), where a job that can write the cache
        could plant one with the right lines: the queue then stopped and asked for a person without a download."""
        self.plant()
        self.assertTrue(self.q.objects_problem())
        with open(os.path.join(self.q.cache, 'queue-fsck-known'), 'w') as f:
            f.write(self.q.fsck_said)
        for again in range(2):
            self.run_again()
            same, out = self.same_change()
            self.assertIs(same, False, out)
            self.assertNotIn('a person must look', out)
            (self.assertNotIn if again else self.assertIn)('rebuilding it', out)
        self.assertFalse(os.path.lexists(self.q.known))

    def test_only_the_queues_own_record_stops_a_download(self):
        self.odd_base()   # as a newer git's fsck may refuse what no FSCK_IGNORE id names
        fetches = []
        real_fetch = Q.Queue.fetch

        def fetch(q, *a):
            fetches.append(a)
            return real_fetch(q, *a)

        def run():
            fetches.clear()
            self.run_again()
            same, out = self.same_change()
            self.assertIsNone(same, out)
            return out
        with unittest.mock.patch.object(Q, 'FSCK_IGNORE', ()), \
                unittest.mock.patch.object(Q.Queue, 'fetch', fetch), \
                unittest.mock.patch.object(Q, 'holds', side_effect=AssertionError('merged in a broken cache')):
            run()
            record, inside = self.q.known, os.path.join(self.q.cache, 'queue-fsck-known')
            self.assertEqual(record, self.q.state + '.fsck-known', 'next to STATE')
            self.assertFalse(os.path.lexists(inside), 'nothing of the queue in the cache but git\'s files')

            def others_can_write():
                os.chmod(record, 0o666)

            def a_link():
                os.replace(record, record + '.real')
                os.symlink(record + '.real', record)

            def someone_elses():
                info = os.stat(record)
                real_fstat = os.fstat

                def fstat(fd):
                    got = real_fstat(fd)
                    if (got.st_dev, got.st_ino) != (info.st_dev, info.st_ino):
                        return got
                    return os.stat_result((*got[:4], got.st_uid + 1, *got[5:]))
                patched = unittest.mock.patch.object(Q.os, 'fstat', fstat)
                patched.start()
                self.addCleanup(patched.stop)

            def an_older_queues():   # the old place: ignored, so one more download, then kept in the new place
                os.replace(record, inside)
            for spoil in (others_can_write, a_link, someone_elses, an_older_queues):
                with self.subTest(spoil=spoil.__name__):
                    self.assertIn('the same way', run())
                    self.assertEqual(len(fetches), 1, 'the record as the queue wrote it: no download')
                    spoil()
                    self.assertIn('rebuilding it', run())
                    self.assertEqual(len(fetches), 2, 'no record of the queue\'s own: one download from scratch')
                    self.assertEqual(os.lstat(record).st_mode & 0o7777, 0o600, 'written again, a file of its own')
                    self.assertFalse(os.path.islink(record))
                    self.assertFalse(os.path.lexists(inside))

    def test_a_healthy_check_drops_an_old_record(self):   # old state: the record of a cache that is healthy now
        Q.write_text(self.q.known, 'error: what an earlier download said')
        self.run_again()
        same, out = self.same_change()
        self.assertIs(same, False, out)
        self.assertFalse(os.path.lexists(self.q.known))
        self.run_again()   # and again with none there
        self.assertIs(self.same_change()[0], False)


class Conflict(unittest.TestCase):
    """An armed PR that conflicts with its base (merge state DIRTY) cannot merge, the queue cannot update it, and
    GitHub leaves auto-merge on without a word. It is told once per head and passed over; before, only the run log
    said it."""
    H, NEW = 'd4' * 20, 'e5' * 20

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-conflict-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d)

    @staticmethod
    def listed(n, state, head, runs=()):
        return {'number': n, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1}, 'mergeStateStatus': state,
                'createdAt': f'2026-11-07T00:00:0{n}Z', 'statusCheckRollup': RedChecks.rollup(*runs), 'headRefOid': head,
                'baseRefName': 'main'}

    def sweep(self, comments=(), head=None, dry=False, fail=(), now=None, runs=(), state='DIRTY'):
        """PR 7 (older, `state`) and PR 8 (behind). now: what `gh pr view 7` shows when the notice is written, if
        other than the list (a push, a resolved conflict, auto-merge turned off)."""
        self.posted, self.updated, self.calls = [], [], []
        head = head or self.H
        listed = [self.listed(7, state, head, runs), self.listed(8, 'BEHIND', 'f6' * 20)]

        def gh(*a):
            self.calls.append(a)
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:3] == ('pr', 'view', '7'):
                if 'view' in fail:
                    return failed('boom')
                shown = {'headRefOid': head, 'mergeStateStatus': 'DIRTY', 'autoMergeRequest': {'m': 1},
                         'comments': [own(c) for c in comments], **(now or {})}
                asked = a[a.index('--json') + 1].split(',')   # gh prints only what was asked for
                return ok(json.dumps({k: v for k, v in shown.items() if k in asked}))
            if a[:3] == ('pr', 'comment', '7'):
                self.posted.append(a[-1])
                return failed('boom') if 'comment' in fail else ok()
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append(n) or 0
        code, self.out = run_quiet(self.q.run, dry)
        return code

    def test_a_conflicted_armed_pr_is_told_and_the_queue_moves_on(self):
        self.assertEqual(self.sweep(), 0)
        self.assertEqual(self.posted, [
            f'pr_queue: merge conflict with main on {self.H[:12]}, so this armed PR waits and the queue passes over '
            'it. GitHub cannot merge it and the queue cannot update it: merge main into it, resolve the conflict and '
            'push (`tool push`).'])
        self.assertEqual(self.updated, [8], 'the next PR is not held up')
        self.assertIn('skip #7: merge conflict with main', self.out)

    def test_it_is_told_once_per_head(self):   # runs again: every push to the base starts a run
        self.sweep()
        first = self.posted[0]
        self.sweep(comments=['an older comment', first])
        self.assertEqual((self.posted, self.updated), ([], [8]))
        self.sweep(comments=[first], head=self.NEW)
        self.assertEqual(len(self.posted), 1, 'a new head that still conflicts is a new message')
        self.assertIn(self.NEW[:12], self.posted[0])

    def test_only_the_queues_own_comment_counts_as_told(self):   # anyone can post a copy of the mark
        self.sweep()
        first = self.posted[0]
        for copy in ({'body': first, 'viewerDidAuthor': False}, {'body': first},
                     {'body': '> ' + first, 'viewerDidAuthor': True}):
            with self.subTest(copy=copy):
                self.sweep(comments=[copy])
                self.assertEqual(self.posted, [first], 'a copy of the mark silenced the queue')

    def test_a_red_notice_of_the_same_head_does_not_count_as_told(self):   # PRs the older queue already wrote on
        red = f'pr_queue: required check build is red on {self.H[:12]}, so this armed PR waits and the queue passes over it.'
        self.sweep(comments=[red])
        self.assertEqual(len(self.posted), 1)

    def test_a_failed_comment_is_tried_again_by_the_next_run(self):   # stopped half-way: nothing remembers "told"
        self.assertEqual(self.sweep(fail=('comment',)), 0)
        self.assertEqual((len(self.posted), self.updated), (1, [8]))
        self.sweep()
        self.assertEqual(len(self.posted), 1, 'no comment on the PR yet: said now')

    def test_a_pr_it_cannot_read_is_passed_over_without_a_word(self):
        self.assertEqual(self.sweep(fail=('view',)), 0)
        self.assertEqual((self.posted, self.updated), ([], [8]))

    def test_nothing_is_said_when_the_pr_changed_since_the_list(self):
        for now in ({'headRefOid': self.NEW}, {'mergeStateStatus': 'BEHIND'}, {'mergeStateStatus': 'UNKNOWN'},
                    {'autoMergeRequest': None}):
            with self.subTest(now=now):
                self.assertEqual(self.sweep(now=now), 0)
                self.assertEqual((self.posted, self.updated), ([], [8]))

    def test_a_dry_run_posts_nothing_and_reads_only_the_list(self):
        self.assertEqual(self.sweep(dry=True), 0)
        self.assertEqual((self.posted, [a[:2] for a in self.calls]), ([], [('pr', 'list')]))
        self.assertIn('skip #7: merge conflict with main', self.out)

    def test_a_conflict_is_said_before_a_red_check_of_the_same_head(self):   # only a push ends it: the red goes too
        self.sweep(runs=(('2026-11-09T02:00:00Z', 'FAILURE'),))
        self.assertEqual(len(self.posted), 1)
        self.assertIn('merge conflict with main', self.posted[0])

    def test_an_odd_base_name_is_not_quoted(self):
        self.q.gh = self.q.gh_write = None
        self.posted = []
        listed = [dict(self.listed(7, 'DIRTY', self.H), baseRefName='x`\nlens: y')]

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:2] == ('pr', 'view'):
                return ok(json.dumps({'headRefOid': self.H, 'mergeStateStatus': 'DIRTY', 'autoMergeRequest': {'m': 1},
                                      'comments': []}))
            self.posted.append(a[-1])
            return ok()
        self.q.gh = self.q.gh_write = gh
        run_quiet(self.q.run, False)
        self.assertIn('merge conflict with its base on', self.posted[0])

    def test_the_message_can_never_count_as_a_lens(self):
        self.sweep()
        post = note(self.posted[0], 't')   # an author whose lens comments count: a look-alike would block (NO_HEADER)
        self.assertIsNone(REVIEW.lens_problem(post))
        self.assertEqual(REVIEW.reviewed_head({'comments': [post], 'reviews': []}), (False, None, 'no lens comments'))

    def test_the_red_notice_reads_as_it_always_did(self):   # old state: a notice an earlier queue posted still counts
        r = RedChecks('sweep')
        r.setUp()
        self.addCleanup(r.doCleanups)
        r.sweep()
        self.assertEqual(r.posted, [
            f'pr_queue: required check review-check is red on {RedChecks.H[:12]}, so this armed PR waits and the queue '
            'passes over it. Fix the cause and push, or if a new lens read or override covers this head: '
            '`tool ready 7`.'])
        r.sweep(comments=r.posted[:])
        self.assertEqual(r.posted, [])


class InlineReviewComments(Base):
    """A lens report, a look-alike or an override posted as an inline review comment (on a line of the diff) is a
    comment like any other. `gh pr view` lists none of them, so before they were never read: a PR whose only lens read
    was inline looked unreviewed, and the queue re-armed whatever head it had."""
    def setUp(self):
        super().setUp()
        self.pushed = self.w.commit({'x.py': lines(extra=['new', 'unread'], at=20)}, [self.c])   # after the lens read
        self.up_pushed = self.w.merge(self.pushed, self.m1)

    def lens(self, sha, name='correctness', **kw):
        return inline_node(f'{LENS}: {name} · reviewer: x · read at: {sha}\nfindings: none', **kw)

    def assertOff(self, f, code, said):
        self.assertEqual((code, f.armed, len(f.comments)), (0, None, 1), f.comments)
        self.assertNotIn('arm', f.order())
        self.assertIn(said, f.comments[0])

    def test_an_inline_lens_read_is_the_read_the_queue_keeps(self):
        f = self.fake(inline=[self.lens(self.c)])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)
        self.assertIn(f'the change read at {self.c[:12]}', out)

    def test_a_push_after_an_inline_lens_read_is_not_re_armed(self):   # the head holds more than the lens read
        f = self.fake('up_pushed', h1=self.pushed, inline=[self.lens(self.c)])
        code, out = self.update(f)
        self.assertOff(f, code, 'is not exactly the change read')
        self.assertTrue(f.updated)

    def test_an_inline_lens_look_alike_blocks_like_any_other(self):
        fence = '`' * 3
        for body in (f'{fence}\n{LENS}: correctness · reviewer: x · read at: {self.c}\n{fence}',
                     'this one is safe to merge', f'{LENS}: correctness · reviewer: x · read at: {self.c[:7]}'):
            with self.subTest(body=body):
                f = self.fake(inline=[inline_node(body)])
                code, out = self.update(f)
                self.assertOff(f, code, 'unclear')
                self.assertFalse(f.updated)

    def test_an_edited_or_hidden_inline_lens_reads_nothing(self):
        for change, why in (({'edited': True}, lens.EDITED), ({'hidden': True}, lens.HIDDEN)):
            with self.subTest(change=change):
                f = self.fake(inline=[self.lens(self.c, **change)])
                code, out = self.update(f)
                self.assertOff(f, code, why.split(':')[0])

    def test_inline_and_plain_lenses_are_read_together(self):
        # the newest read of a lens wins, wherever it was posted
        newer_inline = self.fake('up_pushed', h1=self.pushed, comments=[lens_at('correctness', self.c, '2026-11-07T10:00:00Z')],
                                 inline=[self.lens(self.pushed, when='2026-11-07T11:00:00Z')])
        code, out = self.update(newer_inline)
        self.assertEqual((code, newer_inline.armed, newer_inline.comments), (0, self.up_pushed, []), out)
        older_inline = self.fake('up_pushed', h1=self.pushed, comments=[lens_at('correctness', self.pushed, '2026-11-07T10:00:00Z')],
                                 inline=[self.lens(self.c, when='2026-11-07T09:00:00Z')])
        code, out = self.update(older_inline)
        self.assertEqual((code, older_inline.armed), (0, self.up_pushed), out)
        # two lenses, one inline, that read different heads: unclear
        two = self.fake('up_pushed', h1=self.pushed, comments=[lens_at('correctness', self.pushed)],
                        inline=[self.lens(self.c, name='design')])
        code, out = self.update(two)
        self.assertOff(two, code, 'did not all read one head')

    def test_an_inline_override_vouches_for_its_head(self):
        text = override(self.pushed, 't')['body']
        f = self.fake('up_pushed', h1=self.pushed, comments=[lens_at('correctness', self.c, '2026-11-07T10:00:00Z')],
                      inline=[inline_node(text, when='2026-11-07T11:00:00Z')])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up_pushed, []), out)
        for change in ({'edited': True}, {'hidden': True}, {'association': 'COLLABORATOR'}, {'typename': 'Bot'}):
            with self.subTest(change=change):   # only the override's own author, unedited and shown
                f = self.fake('up_pushed', h1=self.pushed, comments=[lens_at('correctness', self.c, '2026-11-07T10:00:00Z')],
                              inline=[inline_node(text, when='2026-11-07T11:00:00Z', **change)])
                code, out = self.update(f)
                self.assertOff(f, code, 'is not exactly the change read')

    def test_a_strangers_or_a_bots_inline_comment_neither_reads_nor_blocks(self):   # anyone can review a public PR
        for who in ({'association': 'NONE', 'login': 'stranger'}, {'association': 'CONTRIBUTOR', 'login': 'stranger'},
                    {'typename': 'Bot', 'login': 'some-app'}, {'login': 'review-bot'}):
            with self.subTest(who=who):
                f = self.fake('up_pushed', h1=self.pushed, inline=[self.lens(self.c, **who),
                                                                   inline_node('safe to merge', **who)])
                code, out = self.update(f)
                self.assertEqual((code, f.armed, f.comments), (0, self.up_pushed, []), out)

    def test_an_inline_comment_of_an_unsubmitted_review_is_not_posted_yet(self):   # PENDING: only its author sees it
        f = self.fake('up_pushed', h1=self.pushed, inline=[self.lens(self.c, state='PENDING')])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up_pushed, []), out)

    def test_plain_review_talk_on_a_line_changes_nothing(self):   # the usual inline comment
        f = self.fake(inline=[inline_node('nit: rename this'), inline_node('why not a set here?', n=2)])
        code, out = self.update(f)
        self.assertEqual((code, f.armed, f.comments), (0, self.up, []), out)

    def test_inline_comments_that_cannot_be_read_change_nothing(self):   # also for a PR gh lists no review for
        f = self.fake(flags=[failed('HTTP 502')])
        code, out = self.update(f)
        self.assertEqual((code, f.order(), f.armed, f.comments), (1, [], self.c, []), out)
        self.assertIn('nothing changed', out)
        self.assertFalse(self.plan_exists())

    def test_a_stopped_run_reads_the_inline_comments_again_before_it_rearms(self):   # recover(): the review as it is now
        f = self.fake('up_pushed', h1=self.pushed, kill_at='update')
        with self.assertRaises(Killed):
            self.update(f)   # no lens comments yet: the plan stores the head as the read
        self.assertTrue(self.plan_exists())
        f.kill_at, f.inline = None, [self.lens(self.c)]   # posted meanwhile: the lens read less than the head holds
        code, out = run_quiet(self.q.recover)
        self.assertEqual((code, f.armed, len(f.comments)), (0, None, 1), out)
        self.assertIn('could not be shown to be the change read', f.comments[0])
        self.assertFalse(self.plan_exists())

    def test_the_inline_comments_reach_the_rules_with_their_own_flags_and_time(self):
        f = self.fake(inline=[self.lens(self.c, when='2026-11-07T12:00:00Z', n=5)])
        self.q.gh = self.q.gh_write = f
        pr = self.q.view(7, reviews=True)
        self.assertEqual(pr[lens.REVIEW_COMMENTS], [{
            'body': f'{LENS}: correctness · reviewer: x · read at: {self.c}\nfindings: none',
            'createdAt': '2026-11-07T12:00:00Z', 'author': {'__typename': 'User', 'login': 'owner'},
            'authorAssociation': 'OWNER', 'includesCreatedEdit': False, 'isMinimized': False,
            'url': 'https://x/pull/7#discussion_r5'}], 'the time it was published, not drafted')
        self.assertNotIn(lens.REVIEW_COMMENTS, self.q.view(7), 'a plain view reads no reviews')


class InlineCommentsQuery(ReviewFlagsQuery):
    """review_flags' inline comments: every page of every review, and any doubt is a failure of the whole read."""
    test_more_than_100_reviews_are_read_page_by_page = test_any_page_that_fails_fails_the_whole_read = None
    test_a_next_page_without_a_new_cursor_fails = test_more_pages_than_the_limit_fail = None
    test_a_node_without_both_flags_is_left_out = None   # the flags' own tests run once, in ReviewFlagsQuery

    def bodies(self):
        return [c['body'] for c in self.inline]

    def test_the_query_asks_for_what_the_rules_read(self):
        for field in ('state', 'body', 'createdAt', 'publishedAt', 'includesCreatedEdit', 'isMinimized', 'url',
                      'authorAssociation', 'author{__typename login}'):
            self.assertIn(field, Q.INLINE_COMMENTS)
        for query in (Q.REVIEW_FLAGS_QUERY, Q.MORE_COMMENTS_QUERY):
            self.assertIn('comments(first:100', query)
            self.assertIn(Q.INLINE_COMMENTS, query)

    def test_a_review_with_more_than_100_inline_comments_is_read_page_by_page(self):
        nodes = [inline_node(f'c{i}', n=i) for i in range(250)]
        (flags, why), calls = self.read(flags_page(gh_review('x', 't'), inline=nodes[:100], inline_more='i1'),
                                        more_comments_page(nodes[100:200], more='i2'), more_comments_page(nodes[200:]))
        self.assertIsNone(why)
        self.assertEqual(self.bodies(), [f'c{i}' for i in range(250)])
        self.assertEqual(set(flags), {'PRR_1', 'PRR_inline'})
        for call, after in zip(calls[1:], ('i1', 'i2')):
            self.assertIn(f'query={Q.MORE_COMMENTS_QUERY}', call)
            self.assertIn('id=PRR_inline', call)
            self.assertIn(f'after={after}', call)

    def test_more_reviews_and_more_inline_comments_are_both_read(self):
        (flags, why), calls = self.read(
            flags_page(gh_review('x', 't', 1), inline=[inline_node('a')], inline_more='i1', more='r1'),
            flags_page(gh_review('x', 't', 2)), more_comments_page([inline_node('b')]))
        self.assertEqual((why, set(flags), sorted(self.bodies())), (None, {'PRR_1', 'PRR_2', 'PRR_inline'}, ['a', 'b']))
        self.assertEqual(len(calls), 3)

    def test_any_page_of_inline_comments_that_fails_fails_the_whole_read(self):
        for second in (failed('HTTP 502'), ok('{}'), ok('null'), ok('{"data": {"node": null}}'),
                       ok('{"data": {"node": {}}}'), ok('{"data": {"node": {"comments": {"nodes": []}}}}'),
                       ok(json.dumps({'errors': [{'message': 'x'}], **json.loads(more_comments_page().stdout)}))):
            with self.subTest(second=second):
                (flags, why), _ = self.read(flags_page(inline=[inline_node('a')], inline_more='i1'), second)
                self.assertIsNone(flags)
                self.assertTrue(why)

    def test_a_next_page_of_inline_comments_without_a_new_cursor_fails(self):
        for cursor in ('', 'i1'):   # issue #65: GitHub repeats the token: stop at once, not after REVIEW_PAGES calls
            pages = [flags_page(inline=[inline_node('a')], inline_more='i1')] + \
                [more_comments_page([inline_node('b')], more=cursor)] * Q.REVIEW_PAGES
            (flags, why), calls = self.read(*pages)
            self.assertIsNone(flags, cursor)
            self.assertIn('no next page', why)
            self.assertEqual(len(calls), 2, cursor)
        (flags, why), _ = self.read(flags_page(inline=[inline_node('a')], inline_more=''))
        self.assertIsNone(flags)

    def test_a_next_page_of_inline_comments_with_a_null_cursor_fails(self):   # endCursor is nullable
        for pages in ([flags_page(inline=[inline_node('a')], inline_more=NO_CURSOR)],
                      [flags_page(inline=[inline_node('a')], inline_more='i1'),
                       more_comments_page([inline_node('b')], more=NO_CURSOR)]):
            (flags, why), calls = self.read(*pages)
            self.assertIsNone(flags, len(pages))
            self.assertIn('no next page', why)
            self.assertEqual(len(calls), len(pages))

    def test_more_pages_than_the_limit_fail(self):
        pages = [flags_page(inline=[inline_node('a')], inline_more='i0')] + \
            [more_comments_page([inline_node('b')], more=f'i{i}') for i in range(1, Q.REVIEW_PAGES + 1)]
        (flags, why), calls = self.read(*pages)
        self.assertIsNone(flags)
        self.assertEqual(len(calls), Q.REVIEW_PAGES)
        self.assertIn('not all read', why)

    def edited_page(self, change):
        page = json.loads(flags_page(gh_review('x', 't'), inline=[inline_node('a'), inline_node('b', n=2)]).stdout)
        change(page['data']['repository']['pullRequest']['reviews']['nodes'])
        return ok(json.dumps(page))

    def test_an_answer_that_is_not_what_was_asked_for_fails_the_whole_read(self):   # never a comment gone missing
        def drop(key):
            return lambda nodes: nodes[1]['comments']['nodes'][0].pop(key)

        def put(key, value):
            return lambda nodes: nodes[1]['comments']['nodes'][0].update({key: value})
        changes = [drop(k) for k in ('state', 'body', 'createdAt', 'includesCreatedEdit', 'isMinimized', 'url',
                                     'authorAssociation')]
        changes += [put('includesCreatedEdit', None), put('isMinimized', 'no'), put('body', None), put('state', None),
                    lambda nodes: nodes[1]['comments']['nodes'].append(None),
                    lambda nodes: nodes[0].pop('comments'), lambda nodes: nodes[0].update(comments=None),
                    lambda nodes: nodes[0].update(comments={'nodes': []}),
                    lambda nodes: nodes[1]['comments'].update(nodes=None),
                    lambda nodes: nodes[1]['comments'].update(pageInfo={'hasNextPage': None}),
                    lambda nodes: nodes.append(None), lambda nodes: nodes.append('x')]
        for i, change in enumerate(changes):
            with self.subTest(i=i):
                (flags, why), _ = self.read(self.edited_page(change))
                self.assertIsNone(flags)
                self.assertIn('GitHub gave no list', why)

    def test_a_deleted_account_and_a_draft_time_are_read_as_they_are(self):
        def change(nodes):
            nodes[1]['comments']['nodes'][0].update(author=None, publishedAt=None)
        (flags, why), _ = self.read(self.edited_page(change))
        self.assertIsNone(why)
        self.assertEqual((self.inline[0]['author'], self.inline[0]['createdAt']), (None, '2026-11-01T00:00:00Z'))

    def test_the_inline_comments_of_a_review_without_flags_are_still_read(self):   # gh pr view lists none of them
        def change(nodes):
            nodes[1].pop('includesCreatedEdit')
        (flags, why), _ = self.read(self.edited_page(change))
        self.assertEqual((set(flags), why, self.bodies()), ({'PRR_1'}, None, ['a', 'b']))

    def test_more_inline_comments_of_a_review_without_an_id_fail(self):   # nothing to ask for the next page with
        def change(nodes):
            nodes[1]['comments']['pageInfo'].update(hasNextPage=True, endCursor='i1')
            nodes[1].pop('id')
        (flags, why), calls = self.read(self.edited_page(change))
        self.assertEqual((flags, len(calls)), (None, 1))

    def test_an_unsubmitted_reviews_inline_comment_is_left_out(self):
        (flags, why), _ = self.read(flags_page(inline=[inline_node('a', state='PENDING'), inline_node('b', n=2)]))
        self.assertEqual((why, self.bodies()), (None, ['b']))

    def test_no_reviews_at_all_reads_as_none(self):   # empty state
        (flags, why), calls = self.read(flags_page())
        self.assertEqual((flags, self.inline, why, len(calls)), ({}, [], None, 1))


# --- what the port adds ----------------------------------------------------------------------------------------------
class Arm(unittest.TestCase):
    def armed_with(self, **change):
        d = tempfile.mkdtemp(prefix='pr-queue-arm-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d, config.parse(json.dumps({**CFG_RAW, **change})))
        calls = []
        q.gh = q.gh_write = lambda *a: calls.append(a) or ok()
        q.arm(7, H1)
        return calls[0]

    def test_the_merge_method_comes_from_the_config_and_the_queue_never_deletes_a_branch(self):
        self.assertEqual(self.armed_with(), ('pr', 'merge', '7', '--repo', REPO, '--auto', '--squash',
                                             '--match-head-commit', H1))
        self.assertEqual(self.armed_with(merge_method='merge', delete_branch=False),
                         ('pr', 'merge', '7', '--repo', REPO, '--auto', '--merge', '--match-head-commit', H1))
        self.assertIn('--rebase', self.armed_with(merge_method='rebase'))
        self.assertNotIn('--delete-branch', self.armed_with(delete_branch=True))


class StackedGitHub:
    """GitHub and gh for open PRs stacked on each other, as they treat a branch delete. `gh pr merge --auto` on a PR
    GitHub can merge at once merges it directly, and with --delete-branch gh then deletes the head with the token: GitHub
    CLOSES every open PR whose base it was (events base_ref_deleted, closed). The repository's own "Automatically delete
    head branches" setting (`setting`) first retargets those PRs to the merged PR's base, then deletes. A PR that cannot
    merge yet is only armed; GitHub merges it later (merge_armed) and only the setting deletes. gh's merge of a PR that
    is already merged (a run that still saw it open) merges nothing, but with --delete-branch it still deletes the head
    with the token."""
    def __init__(self, prs, setting=True):
        self.prs = {n: dict(head=h, base=b, state='OPEN', armed=False) for n, (h, b) in prs.items()}
        self.branches = {p['head'] for p in self.prs.values()} | {'main'}
        self.setting, self.calls = setting, []

    def delete(self, branch, by_setting):
        for p in self.prs.values():
            if p['state'] == 'OPEN' and p['base'] == branch:
                if by_setting:
                    p['base'] = self.merged_base
                else:
                    p['state'] = 'CLOSED'
        self.branches.discard(branch)

    def merge(self, n, token_delete):
        p = self.prs[n]
        p['state'], self.merged_base = 'MERGED', p['base']
        if token_delete and p['head'] in self.branches:   # gh's own delete runs right after its merge call returns
            self.delete(p['head'], by_setting=False)
        if self.setting and p['head'] in self.branches:
            self.delete(p['head'], by_setting=True)

    def merge_armed(self):
        for n, p in self.prs.items():
            if p['state'] == 'OPEN' and p['armed']:
                self.merge(n, token_delete=False)

    def __call__(self, *a):
        self.calls.append(a)
        n = int(a[2])
        if a[:2] == ('pr', 'merge') and '--auto' in a:
            if self.prs[n]['state'] == 'MERGED':   # "already merged": no merge, but gh's branch delete still runs
                if '--delete-branch' in a and self.prs[n]['head'] in self.branches:
                    self.delete(self.prs[n]['head'], by_setting=False)
            elif self.prs[n]['base'] == 'main':   # up to date and green: gh merges now instead of arming
                self.merge(n, token_delete='--delete-branch' in a)
            else:
                self.prs[n]['armed'] = True
        return ok()


class StackedPrsAreRetargetedNeverClosed(unittest.TestCase):
    """A PR stacked on another (#2 on #1's branch, #3 on #2's) must survive #1's merge: GitHub's setting retargets it
    to main. Before, the queue armed with --delete-branch; on a PR GitHub could merge at once gh merged it and deleted
    the branch with the token, and GitHub closed the PR stacked on it."""
    def queue(self, gh, delete_branch=True):
        d = tempfile.mkdtemp(prefix='pr-queue-stack-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d, config.parse(json.dumps({**CFG_RAW, 'delete_branch': delete_branch})))
        q.gh = q.gh_write = gh
        return q

    def test_a_stacked_child_is_retargeted_by_githubs_setting_not_closed(self):
        gh = StackedGitHub({1: ('work/a', 'main'), 2: ('work/b', 'work/a')})
        self.queue(gh).arm(1, H1)
        self.assertEqual(gh.prs[1]['state'], 'MERGED')
        self.assertEqual((gh.prs[2]['state'], gh.prs[2]['base']), ('OPEN', 'main'), 'the stacked PR was closed')
        self.assertNotIn('work/a', gh.branches, "GitHub's setting still deletes the merged branch")

    def test_a_chain_of_three_survives_each_merge_in_turn(self):
        gh = StackedGitHub({1: ('work/a', 'main'), 2: ('work/b', 'work/a'), 3: ('work/c', 'work/b')})
        q = self.queue(gh)
        q.arm(1, H1)
        self.assertEqual([(gh.prs[n]['state'], gh.prs[n]['base']) for n in (2, 3)], [('OPEN', 'main'), ('OPEN', 'work/b')])
        q.arm(2, H2)
        self.assertEqual((gh.prs[3]['state'], gh.prs[3]['base']), ('OPEN', 'main'))
        q.arm(3, H3)
        self.assertEqual([gh.prs[n]['state'] for n in (1, 2, 3)], ['MERGED'] * 3)

    def test_an_armed_parent_merged_later_by_github_leaves_the_child_open(self):
        gh = StackedGitHub({1: ('work/a', 'release'), 2: ('work/b', 'work/a')})
        gh.branches.add('release')
        self.queue(gh).arm(1, H1)   # not mergeable at once: armed only
        gh.merge_armed()
        self.assertEqual((gh.prs[2]['state'], gh.prs[2]['base']), ('OPEN', 'release'))

    def test_without_the_setting_the_branch_stays_and_the_child_stays_open(self):   # the queue deletes nothing itself
        gh = StackedGitHub({1: ('work/a', 'main'), 2: ('work/b', 'work/a')}, setting=False)
        self.queue(gh).arm(1, H1)
        self.assertIn('work/a', gh.branches)
        self.assertEqual((gh.prs[2]['state'], gh.prs[2]['base']), ('OPEN', 'work/a'))

    def test_arming_again_after_github_merged_it_deletes_nothing_and_closes_nothing(self):   # runs again (#253)
        # #1 is armed only; GitHub merges it later and, with the setting off, its branch stays. A run that still saw #1
        # open arms it again: gh finds it merged, and with --delete-branch it would delete the branch with the token
        # and close #2. The first arm cannot delete (not mergeable at once), so only the second arm is tested here.
        gh = StackedGitHub({1: ('work/a', 'release'), 2: ('work/b', 'work/a')}, setting=False)
        gh.branches.add('release')
        q = self.queue(gh)
        q.arm(1, H1)
        gh.merge_armed()
        self.assertEqual((gh.prs[1]['state'], gh.prs[2]['state']), ('MERGED', 'OPEN'))
        q.arm(1, H1)
        self.assertIn('work/a', gh.branches, 'the second arm deleted the merged branch with the token')
        self.assertEqual((gh.prs[2]['state'], gh.prs[2]['base']), ('OPEN', 'work/a'), 'the stacked PR was closed')


class GitCalls(unittest.TestCase):
    def git_call(self, read=None, write=None, remote=None, job=None, args=('fetch', 'x'), fetch=True):
        d = tempfile.mkdtemp(prefix='pr-queue-git-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d, remote=remote)
        q.read_token, q.write_token = read, write
        seen = []
        env = {'GH_TOKEN': 'machine-token', 'GITHUB_TOKEN': 'other-token', Q.WRITE_TOKEN: 'pat-token', **(job or {})}
        with unittest.mock.patch.object(Q.subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)) or ok()), \
                unittest.mock.patch.dict(os.environ, env):
            q.git(*args, fetch=fetch)
        (argv, kw), = seen
        return q, argv, kw['env']

    def test_the_caches_local_calls_run_no_program_of_the_job(self):   # #450 item 2: only the fetches keep any
        job = {k: 'job-' + k for k in Q.GIT_RUN}
        for args in (('merge-tree', '--write-tree', 'a', 'b'), ('cat-file', '-e', 'a'), ('rev-parse', 'HEAD')):
            with self.subTest(args[0]):
                q, argv, env = self.git_call('run-token', remote=f'https://github.com/{REPO}.git', job=job, args=args,
                                             fetch=False)
                self.assertEqual({k: env[k] for k in Q.GIT_RUN if k in env}, {})

    def test_a_fetch_with_no_login_says_so_by_name(self):   # #450 item 7: not just git's prompt error
        d = tempfile.mkdtemp(prefix='pr-queue-git-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d, remote=f'https://github.com/{REPO}.git')
        for err in ("fatal: could not read Username for 'https://github.com': terminal prompts disabled",
                    "remote: Invalid username or token.\nfatal: Authentication failed for 'https://github.com/o/r.git/'"):
            with self.subTest(err[:30]), unittest.mock.patch.object(q, 'git', lambda *a, **kw: failed(err)), \
                    unittest.mock.patch.object(q, 'own_cache', lambda: True):
                for _ in range(2):   # every run says the same
                    got, out = run_quiet(q.fetch, 'main', 7, 'a' * 40)
                    self.assertIsNone(got)
                    self.assertIn('git fetch failed', out)
                    self.assertIn('git found no login for GitHub', out)
                    self.assertIn('PR_QUEUE_READ_TOKEN', out)
        with unittest.mock.patch.object(q, 'git', lambda *a, **kw: failed('fatal: unable to access: Could not resolve '
                                                                           'host: github.com')), \
                unittest.mock.patch.object(q, 'own_cache', lambda: True):
            got, out = run_quiet(q.fetch, 'main', 7, 'a' * 40)
        self.assertNotIn('no login', out)   # a network error is not blamed on the login

    def test_only_the_queues_two_fetches_are_fetches(self):   # #450 item 2: Queue.fetch marks them, nothing else
        w = World()
        self.addCleanup(w.close)
        m0 = w.commit({'x': '0\n'})
        gone = w.commit({'x': 'gone\n'}, [m0])   # no ref reaches it: the second fetch, by sha
        w.set('refs/heads/main', m0)
        w.set('refs/pull/7/head', m0)
        q = make_queue(w.dir, remote=w.remote)   # a local path: a fetch keeps every GIT_RUN name
        exec_path = subprocess.run(['git', '--exec-path'], text=True, capture_output=True).stdout.strip()
        job = {'GIT_EXEC_PATH': exec_path, 'GIT_SSH_COMMAND': 'false', 'GIT_ALLOW_PROTOCOL': 'file'}
        seen, real = [], subprocess.run

        def run(argv, **kw):
            seen.append((argv, kw.get('env') or {}))
            return real(argv, **kw)
        with unittest.mock.patch.object(Q.subprocess, 'run', run), unittest.mock.patch.dict(os.environ, job), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIs(q.fetch('main', 7, m0, gone), True, out.getvalue())
        fetches = [e for argv, e in seen if 'fetch' in argv]
        local = [(argv, e) for argv, e in seen if 'fetch' not in argv and 'init' not in argv]
        self.assertEqual(len(fetches), 2, seen)
        self.assertTrue(local, seen)
        for e in fetches:
            self.assertEqual({k: e[k] for k in Q.GIT_RUN if k in e}, job)
        for argv, e in local:
            self.assertEqual({k: e[k] for k in Q.GIT_RUN if k in e}, {}, argv)

    def test_the_fetches_run_gc_in_the_foreground(self):   # state lens 3: no detached gc races the git fsck
        w = World()
        self.addCleanup(w.close)
        m0 = w.commit({'x': '0\n'})
        gone = w.commit({'x': 'gone\n'}, [m0])
        w.set('refs/heads/main', m0)
        w.set('refs/pull/7/head', m0)
        q = make_queue(w.dir, remote=w.remote)
        seen, real = [], subprocess.run

        def run(argv, **kw):
            seen.append(argv)
            return real(argv, **kw)
        with unittest.mock.patch.object(Q.subprocess, 'run', run), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertIs(q.fetch('main', 7, m0, gone), True, out.getvalue())
        fetches = [a for a in seen if 'fetch' in a]
        self.assertEqual(len(fetches), 2, seen)
        for argv in fetches:
            before = argv[:argv.index('fetch')]
            for flag in ('gc.autoDetach=false', 'maintenance.autoDetach=false'):
                self.assertIn(flag, before)
                self.assertEqual(before[before.index(flag) - 1], '-c')

    def test_git_gets_the_login_from_gh_only_and_never_prompts(self):
        q, argv, env = self.git_call('run-token', 'pat-token')
        self.assertEqual(argv[:15], ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                     '-c', 'core.commitGraph=false', '-c', 'core.attributesFile=/dev/null',
                                     '--no-replace-objects', '-c', 'credential.https://github.com.helper=',
                                     '-c', 'credential.https://github.com.helper=!gh auth git-credential', '--git-dir'])
        self.assertEqual(argv[15:], [q.cache, 'fetch', 'x'])
        self.assertEqual(env['GIT_TERMINAL_PROMPT'], '0')
        self.assertEqual(env['GIT_ATTR_NOSYSTEM'], '1')

    def test_git_fetches_with_the_read_token_only(self):   # gh auth git-credential answers with GH_TOKEN
        q, argv, env = self.git_call('run-token', 'pat-token')
        self.assertEqual({k: env[k] for k in Q.TOKEN_VARS if k in env}, {'GH_TOKEN': 'run-token'})

    def test_with_no_read_token_git_gets_no_login_at_all(self):   # not the machine's gh login either
        q, argv, env = self.git_call()
        self.assertNotIn('credential.https://github.com.helper=!gh auth git-credential', argv)
        self.assertEqual([k for k in Q.TOKEN_VARS if k in env], [])

    def test_the_cache_fetches_from_github_with_no_program_of_the_job_but_gits_own(self):   # #442 item 2
        # Queue.remote is always https://github.com/...: ssh, a git:// proxy and a login prompt are never needed (the
        # login is gh's, or none at all), and git's default protocols allow https.
        job = {k: 'job-' + k for k in Q.GIT_RUN}
        self.assertEqual(Q.Queue(REPO, CFG, *('/nowhere',) * 3).remote, f'https://github.com/{REPO}.git')
        for read in ('run-token', None):
            with self.subTest(read=read):
                q, argv, env = self.git_call(read, remote=f'https://github.com/{REPO}.git', job=job)
                self.assertEqual({k: env[k] for k in Q.GIT_RUN if k in env}, {'GIT_EXEC_PATH': 'job-GIT_EXEC_PATH'})

    def test_the_empty_helper_resets_any_helper_the_machine_set(self):   # git's rule, checked on this git
        d = tempfile.mkdtemp(prefix='pr-queue-git-')
        self.addCleanup(shutil.rmtree, d, True)
        subprocess.run(['git', 'init', '-q', '--bare', d], check=True)
        r = subprocess.run(['git', '-c', 'credential.https://github.com.helper=store', *Q.GIT_AUTH, '--git-dir', d,
                            'config', '--get-all', 'credential.https://github.com.helper'], text=True, capture_output=True)
        self.assertEqual(r.stdout.splitlines(), ['store', '', '!gh auth git-credential'],
                         'the empty value must come after any machine helper and before gh: git then asks only gh')


class GitVersion(unittest.TestCase):
    @staticmethod
    def problem(out=None, exc=None):
        def run(argv, **kw):
            if exc:
                raise exc
            return ok(out)
        return Q.git_problem(run)

    def test_merge_tree_with_an_attribute_source_needs_2_43(self):   # #450 item 1: never silently without it
        # GIT_ATTR_SOURCE came in 2.41 (an older git ignores it: HEAD's or attr.tree's attributes would merge), and
        # merge-tree with an attribute source crashed until 2.43.
        for old in ('git version 2.34.1', 'git version 2.38.0', 'git version 2.40.1', 'git version 2.42.2'):
            with self.subTest(old):
                said = self.problem(old + '\n')
                self.assertIn('git 2.43 or newer is needed', said)
                self.assertIn('GIT_ATTR_SOURCE', said)
                self.assertIn(old[len('git version '):], said)
        for fine in ('git version 2.43.0', 'git version 2.47.1.windows.1', 'git version 3.0.0'):
            self.assertIsNone(self.problem(fine), fine)

    def test_no_git_or_odd_output_is_a_problem(self):
        self.assertIn('No such file', self.problem(exc=FileNotFoundError(2, 'No such file or directory')))
        self.assertIn('found: none', self.problem(''))
        self.assertIsNotNone(self.problem('version 9'))

    def test_this_git_is_new_enough(self):   # the tests above need merge-tree --write-tree too
        self.assertIsNone(Q.git_problem())

    def test_git_version_and_the_caches_git_init_get_no_token(self):   # local gits: no login, so no token at all
        env = {k: f'{k.lower()}-value' for k in Q.TOKEN_VARS}
        seen = []
        def run(argv, **kw):
            seen.append((argv, kw.get('env')))
            return ok('git version 2.47.0')
        d = tempfile.mkdtemp(prefix='pr-queue-env-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d)
        with unittest.mock.patch.dict(os.environ, env), unittest.mock.patch.object(Q.subprocess, 'run', run):
            Q.git_problem(run)
            q.own_cache()
        inits = [e for argv, e in seen if 'init' in argv]
        self.assertTrue(inits, seen)
        for argv, e in seen[:1] + [(None, e) for e in inits]:
            self.assertIsNotNone(e, 'a git without env= gets every token of this process')
            self.assertEqual([k for k in Q.TOKEN_VARS if k in e], [])


# --- two tokens: reads with the run's GITHUB_TOKEN, writes with the caller's token ----------------------------------
class OnlyWritesUseTheCallersToken(unittest.TestCase):
    """The caller's token (a fine-grained PAT) cannot read check results: a read moved to it fails quietly (the queue
    waits behind a PR it can no longer judge). So every call made with it is one of these, by its source."""
    WRITES = {('pr', 'comment'), ('pr', 'merge'), ('pr', 'update-branch')}

    def test_every_call_with_the_callers_token_is_a_write_or_the_one_view(self):
        import ast
        with open(Q.__file__, encoding='utf-8') as f:
            tree = ast.parse(f.read())
        writes, by_view = [], []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == 'gh_write':
                writes.append(tuple(a.value if isinstance(a, ast.Constant) else '?' for a in node.args[:2]))
            for kw in node.keywords:
                if isinstance(kw.value, ast.Attribute) and kw.value.attr in ('gh_write', 'write_token'):
                    by_view.append((getattr(f, 'attr', '?'), kw.arg, [k.arg for k in node.keywords]))
            if isinstance(f, ast.Attribute) and f.attr == 'run_gh' and node.args and \
                    isinstance(node.args[0], ast.Attribute) and node.args[0].attr == 'write_token':
                self.assertEqual(node.args[-1].value if isinstance(node.args[-1], ast.Constant) else None, 'write')
        self.assertTrue(writes)
        self.assertLessEqual(set(writes), self.WRITES, 'a call other than a write uses the caller\'s token')
        # The one read with it: tell's view (viewerDidAuthor needs the poster's login); VIEW asks for no check result.
        self.assertEqual(by_view, [('view', 'gh', ['gh'])])
        self.assertNotIn('statusCheckRollup', Q.VIEW)

PAT_CANNOT = ('GraphQL: Resource not accessible by personal access token (repository.pullRequests.nodes.0.'
              'statusCheckRollup.contexts.nodes.0)')
RUN_CANNOT = 'GraphQL: Resource not accessible by integration (enablePullRequestAutoMerge)'
WRITES = (('pr', 'merge'), ('pr', 'comment'), ('pr', 'update-branch'), ('pr', 'edit'), ('pr', 'close'))


def reads_checks(a):
    """A gh call that reads check runs or statuses (a fine-grained personal access token may not: GitHub has no Checks
    permission for it)."""
    text = ' '.join(a)
    return (a[:2] == ('pr', 'list') and 'statusCheckRollup' in text) or a[:2] in (('run', 'list'), ('pr', 'checks')) \
        or (a[:1] == ('api',) and ('check-runs' in text or 'statusCheckRollup' in text or '/statuses' in text))


class GitHubTokens:
    """GitHub's token rules around a fake gh: the personal access token cannot read check contexts, the run's
    GITHUB_TOKEN (read-only by the job's permissions) cannot write, and without `run_reads_checks` the run's token
    lacks the checks permission too. calls: (token, args) in order."""
    def __init__(self, fake, run_reads_checks=True):
        self.fake, self.run_reads_checks, self.calls = fake, run_reads_checks, []

    def as_token(self, token):
        def gh(*a):
            self.calls.append((token, a))
            if token == 'pat' and reads_checks(a):
                return failed(PAT_CANNOT)
            if token == 'run' and (a[:2] in WRITES or not self.run_reads_checks and reads_checks(a)):
                return failed(RUN_CANNOT.replace('enablePullRequestAutoMerge', 'statusCheckRollup'))
            return self.fake(*a)
        return gh

    def wire(self, q):
        q.gh, q.gh_write = self.as_token('run'), self.as_token('pat')

    def of(self, token):
        return [a for t, a in self.calls if t == token]


class TwoTokens(Base):
    """Every read uses the run's token and only writes use the caller's (the personal access token cannot read check
    runs: a caller's queue failed every run on its `gh pr list`). Each test fails when the queue reads with the PAT or
    writes with the run's token."""
    def queue_run(self, f, **kw):
        tokens = GitHubTokens(f, **kw)
        tokens.wire(self.q)
        code, out = run_quiet(self.q.main)
        return tokens, code, out

    def test_the_queue_updates_and_rearms_though_the_pat_cannot_read_checks(self):
        f = self.fake(polls=1)
        tokens, code, out = self.queue_run(f)
        self.assertEqual(code, 0, out)
        self.assertEqual(f.order(), ['list', 'disarm', 'update', 'arm'], out)
        self.assertEqual(f.armed, self.up)
        self.assertNotIn('Resource not accessible', out)

    def test_reads_never_use_the_pat(self):
        tokens, code, out = self.queue_run(self.fake())
        self.assertEqual(code, 0, out)
        self.assertEqual([QueueGh.kind(a) for a in tokens.of('pat')], ['disarm', 'update', 'arm'])
        self.assertTrue(any(a[:2] == ('pr', 'list') for a in tokens.of('run')))
        self.assertEqual([a for a in tokens.of('run') if a[:2] in WRITES], [])

    def test_the_branch_update_uses_the_pat(self):   # one by GITHUB_TOKEN would start no workflow
        tokens, code, out = self.queue_run(self.fake())
        self.assertIn(('pat', ('pr', 'update-branch', '7', '--repo', REPO)), tokens.calls)
        self.assertNotIn('update-branch', [a[1] for a in tokens.of('run')])

    def test_a_check_read_the_run_token_may_not_make_stops_the_run_and_changes_nothing(self):
        f = self.fake()
        tokens, code, out = self.queue_run(f, run_reads_checks=False)
        self.assertEqual(code, 1, out)
        self.assertIn('gh pr list failed: GraphQL: Resource not accessible by integration', out)
        self.assertEqual(tokens.of('pat'), [], 'nothing written, and no read retried with the PAT')
        self.assertEqual((f.armed, f.updated, f.comments), (self.c, False, []))
        self.assertFalse(self.plan_exists())


class TellAsThePoster(unittest.TestCase):
    """tell() says why an armed PR waits once per head: "told" is a comment of the queue's own login
    (viewerDidAuthor), and that is the login of the token that posts, not the run's GITHUB_TOKEN."""
    H = 'd4' * 20

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-tell-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d)
        self.posted = []

        def fake_for(token):
            def gh(*a):
                if a[:2] == ('pr', 'view'):   # viewerDidAuthor is true only when the poster reads
                    return ok(json.dumps({'headRefOid': self.H, 'comments': [
                        {'body': b, 'viewerDidAuthor': token == 'pat'} for b in self.posted]}))
                if a[:2] == ('pr', 'comment'):
                    self.posted.append(a[a.index('--body') + 1])
                    return ok()
                raise AssertionError(a)
            return gh
        self.q.gh, self.q.gh_write = fake_for('run'), fake_for('pat')

    def test_a_reason_is_said_once_per_head(self):
        for _ in range(3):
            run_quiet(self.q.tell_red, 7, self.H, 'review-check')
        self.assertEqual(len(self.posted), 1, self.posted)


class GhProcesses(unittest.TestCase):
    """The real gh calls: each gh process gets exactly one token as GH_TOKEN (the read token for gh, the write token for
    gh_write) and no other token of the environment; with no token gh is not started."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='pr-queue-tokens-')
        self.addCleanup(shutil.rmtree, self.d, True)
        os.makedirs(os.path.join(self.d, 'bin'))
        self.log, self.mode = os.path.join(self.d, 'gh.log'), os.path.join(self.d, 'mode')
        fake = os.path.join(self.d, 'bin', 'gh')
        with open(fake, 'w') as f:   # GitHub's rules: the PAT cannot read checks; mode 'no-checks': nor can the run's
            f.write(f'#!{sys.executable} -I\n' + '''import json, os, sys
a, env = sys.argv[1:], os.environ
names = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', 'PR_QUEUE_READ_TOKEN',
         'PR_QUEUE_WRITE_TOKEN')
with open(LOG, 'a') as log:
    log.write(json.dumps({'args': a, 'tokens': {k: env[k] for k in names if k in env}}) + '\\n')
token, text, mode = env.get('GH_TOKEN'), ' '.join(a), open(MODE).read() if os.path.exists(MODE) else ''
checks = 'statusCheckRollup' in text or 'check-runs' in text or a[:2] == ['run', 'list']
if checks and (token == 'pat-token' or mode == 'no-checks'):
    sys.exit(print('GraphQL: Resource not accessible by ' + ('personal access token' if token == 'pat-token'
                   else 'integration') + ' (repository.pullRequests.nodes.0.statusCheckRollup)', file=sys.stderr) or 1)
if a[:2] in (['pr', 'merge'], ['pr', 'comment'], ['pr', 'update-branch']) and token != 'pat-token':
    sys.exit(print('GraphQL: Resource not accessible by integration (updatePullRequestBranch)', file=sys.stderr) or 1)
print('[]' if a[:2] == ['pr', 'list'] else '{}')
'''.replace('LOG', repr(self.log)).replace('MODE', repr(self.mode)))
        os.chmod(fake, 0o755)
        env = {'PATH': os.path.join(self.d, 'bin') + os.pathsep + os.environ['PATH'], 'GH_TOKEN': 'machine-token',
               'GITHUB_TOKEN': 'other-token', Q.READ_TOKEN: 'run-token', Q.WRITE_TOKEN: 'pat-token'}
        patch = unittest.mock.patch.dict(os.environ, env)
        patch.start()
        self.addCleanup(patch.stop)

    def queue(self, read='run-token', write='pat-token'):
        q = make_queue(self.d)
        q.read_token, q.write_token = read, write
        return q

    def logged(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(line) for line in f]

    def test_reads_get_the_run_token_and_writes_the_pat_and_nothing_else(self):
        q = self.queue()
        r = q.gh('pr', 'list', '--repo', REPO, '--json', Q.LIST)
        self.assertEqual((r.returncode, r.stdout.strip()), (0, '[]'), r.stderr)
        self.assertEqual(q.gh_write('pr', 'update-branch', '7', '--repo', REPO).returncode, 0)
        self.assertEqual([(c['args'][:2], c['tokens']) for c in self.logged()],
                         [(['pr', 'list'], {'GH_TOKEN': 'run-token'}), (['pr', 'update-branch'], {'GH_TOKEN': 'pat-token'})])

    def test_the_delete_setting_is_read_with_the_run_token_only(self):   # issue #253
        on, out = run_quiet(self.queue().warn_if_branches_stay)
        self.assertIsNone(on, 'the fake gh answers {}')
        self.assertIn('::warning::pr-queue: could not read', out)
        self.assertEqual([(c['args'][:2], c['tokens']) for c in self.logged()],
                         [(['api', 'graphql'], {'GH_TOKEN': 'run-token'})])

    def test_with_no_token_gh_is_not_started(self):   # never the machine's own gh login
        q = self.queue(None, None)
        self.assertEqual(q.gh('pr', 'list', '--repo', REPO).returncode, 1)
        self.assertEqual(q.gh_write('pr', 'update-branch', '7').returncode, 1)
        self.assertEqual(self.logged(), [])

    def test_a_read_without_the_permission_stops_the_run_with_a_clear_error(self):
        with open(self.mode, 'w') as f:
            f.write('no-checks')
        code, out = run_quiet(self.queue().main)
        self.assertEqual(code, 1, out)
        self.assertIn("::error::pr-queue: the run's GITHUB_TOKEN may not read this (pr list)", out)
        self.assertIn(Q.READ_PERMISSIONS, out)
        self.assertIn('the calling workflow\'s job must grant them too', out)
        self.assertEqual([c['args'][:2] for c in self.logged()], [['pr', 'list']], 'nothing after the failed read')

    def test_the_old_way_fails_as_a_caller_s_queue_did(self):   # the PAT reading the list is what GitHub refuses
        q = self.queue(read='pat-token')
        code, out = run_quiet(q.main)
        self.assertEqual(code, 1, out)
        self.assertIn('Resource not accessible by personal access token', out)


def setting_answer(on):
    """GitHub's answer to DELETE_ON_MERGE_QUERY."""
    return ok(json.dumps({'data': {'repository': {'deleteBranchOnMerge': on}}}))


def gh_answering(answer, calls=None):
    """A Queue.run_gh stand-in: (token, args, which) -> `answer` (raised when it is an exception)."""
    def run_gh(token, a, which):
        if calls is not None:
            calls.append((token, a, which))
        if isinstance(answer, BaseException):
            raise answer
        return answer
    return run_gh


class DeleteOnMergeSetting(unittest.TestCase):
    """Issue #253: the queue never deletes a branch (arm), so it relies on the repository setting "Automatically delete
    head branches". Each run reads it once with the run's read token and warns, never fails, when it is off or cannot
    be read."""
    def read(self, answer):
        d = tempfile.mkdtemp(prefix='pr-queue-setting-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d)
        q.read_token, q.write_token, calls = 'run-token', 'pat-token', []
        q.run_gh = gh_answering(answer, calls)
        on, out = run_quiet(q.warn_if_branches_stay)
        return q, on, out, calls

    def test_on_says_nothing(self):
        _, on, out, calls = self.read(setting_answer(True))
        self.assertEqual((on, out), (True, ''))
        (token, a, which), = calls
        self.assertEqual((token, which, a[:2]), ('run-token', 'read', ('api', 'graphql')), 'the read token, once')
        self.assertIn(f'query={Q.DELETE_ON_MERGE_QUERY}', a)
        self.assertEqual([x for x in a if x.startswith(('owner=', 'name='))], ['owner=owner', 'name=proj'])

    def test_off_is_a_clear_warning(self):
        _, on, out, _ = self.read(setting_answer(False))
        self.assertEqual(on, False)
        self.assertTrue(out.startswith('::warning::pr-queue: the repository setting "Automatically delete head '
                                       'branches" (delete_branch_on_merge) is off, so merged branches stay'), out)
        self.assertIn('Turn it on', out)
        self.assertNotIn('::error::', out)

    def test_a_read_that_fails_or_answers_oddly_is_a_warning_too(self):
        for answer in (failed('GraphQL: Resource not accessible by integration'), failed(''), ok(''), ok('not json'),
                       ok('{}'), ok('{"errors": [{"message": "x"}]}'), setting_answer(None), setting_answer('false'),
                       ok('[]'), OSError('gh not found')):
            with self.subTest(answer=answer):
                q, on, out, _ = self.read(answer)
                self.assertIsNone(on)
                self.assertTrue(out.startswith('::warning::pr-queue: could not read whether the repository setting '
                                               '"Automatically delete head branches" is on'), out)
                self.assertNotIn('::error::', out)
                self.assertEqual(q.said, set(), "a later refused read still prints gh()'s own error")


class Cli(unittest.TestCase):
    """cli() refuses with exit 2 and builds no Queue when it cannot run safely; it names every problem at once."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='pr-queue-cli-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.repo = os.path.join(self.d, 'caller')   # the caller's checkout, as the reusable workflow makes it
        self.git('init', '-q', '-b', 'main')
        self.config = os.path.join(self.repo, '.github', 'pr-queue.json')
        self.commit({'.github/pr-queue.json': json.dumps(CFG_RAW)})
        self.built, self.tokens, self.steps = [], [], []

    def git(self, *a):
        os.makedirs(self.repo, exist_ok=True)
        return subprocess.run(['git', '-C', self.repo, *a], check=True, capture_output=True,
                              env={**os.environ, **GIT_ENV}).stdout

    def write(self, rel, data):
        where = os.path.join(self.repo, rel)
        os.makedirs(os.path.dirname(where), exist_ok=True)
        with open(where, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode())

    def commit(self, files):
        """Commit `files` (path: text or bytes; None deletes it) on the checkout's HEAD; the work tree follows."""
        for rel, data in files.items():
            if data is None:
                self.git('rm', '-q', '--', rel)
            else:
                self.write(rel, data)
                self.git('add', '--', rel)
        self.git('commit', '-q', '--allow-empty', '-m', 'c')

    def cli(self, *extra, repo=REPO, state=None, cache=None, env=None, git=None):
        test = self

        class Built:
            def __init__(self, *a, **kw):
                self.a, self.kw = a, kw

            def warn_if_branches_stay(self):
                test.steps.append('setting')

            def main(self, dry):
                test.steps.append('main')
                test.built.append((self.a, dry))
                test.tokens.append(self.kw)
                return 0
        argv = [f'--repo={repo}', '--config', self.config, '--state-file', state or os.path.join(self.d, 'run', 'plan.json'),
                '--cache', cache or os.path.join(self.d, 'cache.git'), *extra]
        with unittest.mock.patch.object(Q, 'Queue', Built), unittest.mock.patch.object(Q, 'git_problem', lambda: git):
            return run_quiet(Q.cli, argv, {**TOKENS, **(env or {})})

    def refused(self, *why, **kw):
        code, out = self.cli(**kw)
        self.assertEqual((code, self.built), (Q.REFUSED, []), out)
        self.assertIn('refused to start and touched nothing', out)
        for w in why:
            self.assertIn(w, out)
        return out

    def test_a_good_call_builds_the_queue(self):
        self.assertEqual(self.cli()[0], 0)
        (args, dry), = self.built
        state = os.path.join(self.d, 'run', 'plan.json')
        self.assertEqual((args[0], args[2], args[3], args[4], dry),
                         (REPO, state, os.path.join(self.d, 'cache.git'), state + '.lock', False))
        self.assertEqual(args[1], CFG)
        self.cli('--dry-run', '--lock-file', os.path.join(self.d, 'l'), env={'RUNNER_ENVIRONMENT': 'self-hosted'})
        self.assertEqual((self.built[1][0][4], self.built[1][1]), (os.path.join(self.d, 'l'), True))

    def test_the_tokens_reach_the_queue(self):   # reads with the run's token, writes with the caller's
        self.cli()
        self.cli('--dry-run')
        self.assertEqual(self.tokens, [{'read_token': 'run-token', 'write_token': 'pat-token'},
                                       {'read_token': 'run-token', 'write_token': None}])

    def test_every_run_reads_the_delete_setting_once_before_it_starts(self):   # issue #253; dry runs too
        self.cli()
        self.cli('--dry-run')
        self.assertEqual(self.steps, ['setting', 'main'] * 2)
        self.built.clear()
        self.refused('is not owner/name', repo='x')
        self.assertEqual(self.steps, ['setting', 'main'] * 2, 'a refused run reads nothing')

    def test_the_setting_off_or_unreadable_never_changes_the_runs_result(self):   # the real Queue, its run stubbed
        for answer in (setting_answer(False), failed('HTTP 502'), OSError('no gh')):
            for code in (0, 1, Q.SILENT):
                with self.subTest(answer=answer, code=code), \
                        unittest.mock.patch.object(Q.Queue, 'run_gh', staticmethod(gh_answering(answer))), \
                        unittest.mock.patch.object(Q.Queue, 'main', lambda q, dry, code=code: code), \
                        unittest.mock.patch.object(Q, 'git_problem', lambda: None):
                    got, out = run_quiet(Q.cli, ['--repo', REPO, '--config', self.config, '--state-file',
                                                 os.path.join(self.d, 'run', 'plan.json'), '--cache',
                                                 os.path.join(self.d, 'cache.git')], dict(TOKENS))
                    self.assertEqual(got, code, out)
                    self.assertIn('::warning::pr-queue:', out)
                    self.assertNotIn('::error::', out)

    def test_a_missing_or_reused_token_is_refused(self):
        self.refused(f'{Q.READ_TOKEN} is empty', env={Q.READ_TOKEN: ''})
        self.refused(f'{Q.WRITE_TOKEN} is empty', env={Q.WRITE_TOKEN: ''})
        self.refused(f"{Q.WRITE_TOKEN} is the run's GITHUB_TOKEN", env={Q.WRITE_TOKEN: 'run-token'})
        code, out = self.cli('--dry-run', env={Q.READ_TOKEN: ''})
        self.assertEqual((code, self.built), (Q.REFUSED, []), 'a dry run reads: ' + out)
        code, out = self.cli('--dry-run', env={Q.WRITE_TOKEN: ''})
        self.assertEqual(code, 0, 'a dry run writes nothing, so it needs only the read token: ' + out)

    def test_a_github_hosted_runner_is_refused(self):   # its disk, and the plan on it, goes with the job
        self.refused('GitHub-hosted runner', env={'RUNNER_ENVIRONMENT': 'github-hosted'})

    def test_a_repo_that_is_not_owner_name_is_refused(self):
        for repo in ('proj', 'owner/proj/x', '-x/proj', 'owner/pr oj', ''):
            with self.subTest(repo=repo):
                self.built.clear()
                self.refused('is not owner/name', repo=repo)

    def test_relative_paths_are_refused(self):
        self.refused("--state-file 'plan.json' is not an absolute path", "--cache 'cache.git' is not an absolute path",
                     state='plan.json', cache='cache.git')
        code, out = self.cli('--lock-file', 'l')
        self.assertEqual((code, self.built), (Q.REFUSED, []), out)
        self.assertIn("--lock-file 'l' is not an absolute path", out)

    def test_paths_in_the_job_workspace_are_refused(self):   # the next checkout empties it
        ws = os.path.join(self.d, 'ws')
        out = self.refused('--state-file', 'inside the job\'s workspace', state=os.path.join(ws, 'plan.json'),
                           env={'GITHUB_WORKSPACE': ws})
        self.assertIn('--lock-file', out, 'the default lock is next to the plan')
        code, out = self.cli(state=os.path.join(self.d, 'ws2', 'plan.json'), env={'GITHUB_WORKSPACE': ws})
        self.assertEqual(code, 0, 'a sibling folder whose name starts the same is not inside: ' + out)

    def test_a_config_error_is_refused(self):
        self.commit({'.github/pr-queue.json': json.dumps({**CFG_RAW, 'required_check': ['build']})})
        self.refused("unknown key 'required_check'")
        self.commit({'.github/pr-queue.json': None})
        self.refused('has no .github/pr-queue.json')

    def test_the_config_is_read_as_committed_never_from_the_checkouts_file(self):   # #424 item 1
        # A .gitattributes encoding leaves the config's blob as it was (the head check skips it), but a checkout writes
        # the file as UTF-16: the queue must read what git holds, which no checkout filter or attribute changes.
        self.commit({'.gitattributes': '.github/pr-queue.json working-tree-encoding=UTF-16\n'})
        os.remove(self.config)
        self.git('checkout', '--', '.github/pr-queue.json')
        with open(self.config, 'rb') as f:
            self.assertEqual(f.read(2), b'\xff\xfe', 'the checkout wrote UTF-16')
        code, out = self.cli()
        self.assertEqual(code, 0, out)
        self.assertEqual(self.built[-1][0][1], CFG)
        self.write('.github/pr-queue.json', 'not json')   # an edit in the work tree, not committed: never read
        code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)
        os.remove(self.config)   # nor a file missing from the work tree
        code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)

    def test_a_config_changed_between_runs_is_read_anew_and_a_rerun_reads_the_same(self):   # state: nothing kept
        self.cli()
        self.commit({'.github/pr-queue.json': json.dumps({**CFG_RAW, 'merge_method': 'merge'})})
        self.cli()
        self.cli()
        self.assertEqual([b[0][1].merge_method for b in self.built], ['squash', 'merge', 'merge'])

    def test_the_config_read_ignores_global_and_system_git_config(self):   # an earlier job's ~/.gitconfig, say
        broken = os.path.join(self.d, 'broken.gitconfig')
        with open(broken, 'w') as f:
            f.write('[core\n')
        with unittest.mock.patch.dict(os.environ, {'GIT_CONFIG_GLOBAL': broken, 'GIT_CONFIG_SYSTEM': broken,
                                                   'GIT_CONFIG_NOSYSTEM': '0'}):
            code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)

    def test_a_config_that_is_not_utf8_is_refused_never_a_crash(self):   # #424 item 1: was a UnicodeDecodeError
        for name, data in (('UTF-16', json.dumps(CFG_RAW).encode('utf-16')), ('Latin-1', b'{"push_command": "\xe9"}')):
            with self.subTest(name):
                self.commit({'.github/pr-queue.json': data})
                self.refused('.github/pr-queue.json at ', 'not UTF-8 text')

    def test_a_config_that_is_empty_missing_or_no_file_at_head_is_refused(self):   # old, empty or missing state
        self.commit({'.github/pr-queue.json': ''})
        self.refused('not JSON')
        self.commit({'.github/pr-queue.json': None})
        self.write('.github/pr-queue.json', json.dumps(CFG_RAW))   # on disk, never committed: not read
        self.refused('has no .github/pr-queue.json')
        self.commit({'.github/real.json': json.dumps(CFG_RAW)})
        os.symlink('real.json', self.config + '.tmp')
        os.replace(self.config + '.tmp', self.config)
        self.git('add', '--', '.github/pr-queue.json')
        self.git('commit', '-q', '-m', 'symlink')
        self.refused('is a symlink (mode 120000), not a file')
        os.remove(self.config)
        self.commit({'.github/pr-queue.json/x.json': json.dumps(CFG_RAW)})
        self.refused('is a tree (mode 040000), not a file')

    def test_a_tree_that_lists_the_config_twice_is_refused(self):   # git mktree makes one; fsck would flag it
        def hashed(*a, data):
            return subprocess.run(['git', '-C', self.repo, *a], input=data, check=True, capture_output=True,
                                  env={**os.environ, **GIT_ENV}).stdout.decode().strip()
        good = hashed('hash-object', '-w', '--stdin', data=json.dumps(CFG_RAW).encode())
        bad = hashed('hash-object', '-w', '--stdin', data=b'not json')
        for first, second in ((good, bad), (bad, good)):
            with self.subTest(first=first[:7]):
                inner = hashed('mktree', data=f'100644 blob {first}\tpr-queue.json\n'
                                              f'100644 blob {second}\tpr-queue.json\n'.encode())
                root = hashed('mktree', data=f'040000 tree {inner}\t.github\n'.encode())
                self.git('reset', '-q', '--soft', hashed('commit-tree', root, '-m', 'twice', data=b''))
                self.refused('did not list one entry')

    def test_a_config_outside_a_git_checkout_or_before_its_first_commit_is_refused(self):
        outside = os.path.join(self.d, 'outside', 'queue.json')
        os.makedirs(os.path.dirname(outside))
        with open(outside, 'w') as f:
            json.dump(CFG_RAW, f)
        self.config = outside
        self.refused('not in a git checkout')
        os.symlink(os.path.join(self.repo, '.github'), os.path.join(self.d, 'outside', 'link'))
        self.config = os.path.join(self.d, 'outside', 'link', 'pr-queue.json')   # into the checkout from outside
        self.refused('not inside the checkout')
        shutil.rmtree(self.repo)
        self.git('init', '-q', '-b', 'main')
        self.write('.github/pr-queue.json', json.dumps(CFG_RAW))
        self.config = os.path.join(self.repo, '.github', 'pr-queue.json')
        self.refused('has no commit checked out')

    def test_a_config_blob_missing_from_a_partial_clone_is_refused_never_fetched(self):   # #435 item 1
        # actions/checkout's sparse checkout is a --filter=blob:none clone: a pattern that does not match the config's
        # own name ([1] is a glob) leaves its blob on the remote, and cat-file would fetch it with the stored token.
        rel = '.github/q[1].json'
        self.commit({rel: json.dumps(CFG_RAW)})
        remote, pc = os.path.join(self.d, 'remote.git'), os.path.join(self.d, 'pc')
        self.git('clone', '-q', '--bare', self.repo, remote)
        subprocess.run(['git', '-C', remote, 'config', 'uploadpack.allowFilter', 'true'], check=True)
        subprocess.run(['git', 'clone', '-q', '--filter=blob:none', '--no-checkout', 'file://' + remote, pc],
                       check=True, capture_output=True)
        subprocess.run(['git', '-C', pc, 'sparse-checkout', 'set', '--no-cone', rel], check=True, capture_output=True)
        subprocess.run(['git', '-C', pc, 'checkout', '-q', 'main'], check=True, capture_output=True)
        oid = subprocess.run(['git', '-C', pc, 'rev-parse', f'HEAD:{rel}'], check=True, capture_output=True,
                             text=True).stdout.strip()

        def local(sha):   # without fetching it
            return subprocess.run(['git', '-C', pc, 'cat-file', '-e', sha], capture_output=True,
                                  env={**os.environ, 'GIT_NO_LAZY_FETCH': '1', 'GIT_ALLOW_PROTOCOL': 'none'}).returncode == 0
        self.assertFalse(local(oid), 'the clone holds the blob: the test reads nothing missing')
        self.config = os.path.join(pc, rel)
        for _ in range(2):   # runs again: the same refusal, still nothing fetched
            self.refused(f'{rel} at ', 'not in this checkout', 'sparse-checkout pattern')
            self.assertFalse(local(oid), 'the read fetched the blob')
        subprocess.run(['git', '-C', pc, 'sparse-checkout', 'disable'], check=True, capture_output=True)   # the fix
        code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)

    def test_every_git_call_of_the_read_has_a_timeout(self):   # #435 item 1: a stuck git never holds the job
        calls = []

        def run(cmd, **kw):
            calls.append(kw.get('timeout'))
            if 'cat-file' in cmd:
                raise subprocess.TimeoutExpired(cmd, kw.get('timeout'))
            return subprocess.run(cmd, **kw)
        with self.assertRaisesRegex(config.ConfigError, r'pr-queue\.json: git cat-file took over \d+ seconds'):
            Q.committed_config(self.config, run=run)
        self.assertTrue(calls and all(t == Q.GIT_READ_SECONDS for t in calls), calls)

    def test_a_config_path_with_pathspec_magic_reads_only_that_name(self):   # #435 item 2: the exact-name check
        self.commit({'x.json': json.dumps(CFG_RAW)})   # ls-tree -- ':(top)x.json' lists x.json
        self.config = os.path.join(self.repo, ':(top)x.json')
        self.refused('did not list one entry')

    def test_a_leftover_replace_ref_does_not_change_the_config_read(self):   # #435 item 2: GIT_SAFE on the read
        oid = self.git('rev-parse', 'HEAD:.github/pr-queue.json').decode().strip()
        other = subprocess.run(['git', '-C', self.repo, 'hash-object', '-w', '--stdin'], check=True, capture_output=True,
                               input=json.dumps({**CFG_RAW, 'replaced': 1}).encode()).stdout.decode().strip()
        self.git('replace', oid, other)   # a self-hosted runner's .git can outlive a job
        self.assertIn(b'replaced', self.git('cat-file', 'blob', oid), 'the replace ref does nothing')
        code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)

    def envs_of_the_read(self, extra):
        envs = []
        real = subprocess.run

        def run(cmd, **kw):
            envs.append(kw.get('env'))
            return real(cmd, **kw)
        with unittest.mock.patch.dict(os.environ, extra), unittest.mock.patch.object(subprocess, 'run', run):
            code, out = self.cli()
        self.assertEqual((code, self.built[-1][0][1]), (0, CFG), out)
        self.assertTrue(envs and None not in envs, envs)
        return envs

    def test_the_config_read_passes_no_token_to_git(self):   # #435 item 2
        tokens = {k: 'secret-' + k for k in Q.TOKEN_VARS}
        for env in self.envs_of_the_read(tokens):
            self.assertEqual(set(env) & set(tokens), set())
            # never a fetch: git 2.44+ honours the first, an older git still refuses every transport
            self.assertEqual((env.get('GIT_NO_LAZY_FETCH'), env.get('GIT_ALLOW_PROTOCOL')), ('1', 'none'))

    def test_the_config_read_ignores_git_variables_of_the_job(self):   # #435 item 6
        other = os.path.join(self.d, 'other')
        subprocess.run(['git', 'init', '-q', '-b', 'main', other], check=True)
        os.makedirs(os.path.join(other, '.github'))
        with open(os.path.join(other, '.github', 'pr-queue.json'), 'w') as f:
            json.dump({**CFG_RAW, 'merge_method': 'merge'}, f)
        subprocess.run(['git', '-C', other, 'add', '-A'], check=True)
        subprocess.run(['git', '-C', other, 'commit', '-qm', 'other'], check=True, env={**os.environ, **GIT_ENV})
        hostile = {'GIT_DIR': os.path.join(other, '.git'), 'GIT_WORK_TREE': self.repo,
                   'GIT_OBJECT_DIRECTORY': os.path.join(other, '.git', 'objects'), 'GIT_NAMESPACE': 'x',
                   'GIT_ALTERNATE_OBJECT_DIRECTORIES': os.path.join(other, '.git', 'objects'),
                   'GIT_INDEX_FILE': os.path.join(self.d, 'index'), 'GIT_CONFIG_PARAMETERS': "'core.bare'='true'",
                   'GIT_CONFIG_COUNT': '1', 'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'true',
                   'GIT_ICASE_PATHSPECS': '1'}
        for env in self.envs_of_the_read(hostile):
            self.assertEqual(set(env) & set(hostile), set())

    def test_the_queue_drops_one_list_of_git_variables(self):   # #435 item 6: every git call of the queue
        hostile = {k: 'x' for k in Q.GIT_DROP}
        hostile.update(GIT_CONFIG_KEY_3='core.bare', GIT_CONFIG_VALUE_3='true', GIT_TRACE='0', PATH='/bin')
        env = Q.git_env(hostile)
        self.assertEqual(env, {'PATH': '/bin', **FORCED})   # #457 item 4: a local call writes no trace either
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY',
                     'GIT_ALTERNATE_OBJECT_DIRECTORIES', 'GIT_INDEX_FILE', 'GIT_NAMESPACE', 'GIT_CONFIG',
                     'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT', 'GIT_REPLACE_REF_BASE', 'GIT_GRAFT_FILE',
                     'GIT_SHALLOW_FILE', 'GIT_QUARANTINE_PATH', 'GIT_PREFIX', 'GIT_IMPLICIT_WORK_TREE',
                     'GIT_INTERNAL_SUPER_PREFIX', 'GIT_LITERAL_PATHSPECS', 'GIT_GLOB_PATHSPECS', 'GIT_NOGLOB_PATHSPECS',
                     'GIT_ICASE_PATHSPECS', 'GIT_ATTR_SOURCE', 'GIT_EXTERNAL_DIFF'):   # #442 items 1, 2, 6: each name
            with self.subTest(name):
                self.assertIn(name, Q.GIT_DROP)
        q = object.__new__(Q.Queue)   # the cache's git and init
        q.cache, q.read_token, q.remote = os.path.join(self.d, 'cache', 'repo.git'), None, 'https://github.com/o/r.git'
        envs, real = [], subprocess.run

        def run(cmd, **kw):
            envs.append(kw.get('env'))
            return real(cmd, **kw)
        with unittest.mock.patch.dict(os.environ, {'GIT_DIR': self.repo + '/.git', 'GIT_CONFIG_COUNT': '1',
                                                   'GIT_CONFIG_KEY_0': 'core.bare', 'GIT_CONFIG_VALUE_0': 'false'}), \
                unittest.mock.patch.object(subprocess, 'run', run), \
                unittest.mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            self.assertTrue(q.own_cache(), out.getvalue())
            self.assertEqual(q.git('rev-parse', '--is-bare-repository').stdout.strip(), 'true')
        self.assertTrue(envs)
        for env in envs:
            self.assertFalse({'GIT_DIR', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_KEY_0'} & set(env), env)

    def test_programs_and_transports_of_the_job_reach_only_a_fetch_that_may_need_them(self):   # #442 item 2
        names = {'GIT_EXEC_PATH', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS',
                 'GIT_ALLOW_PROTOCOL'}
        self.assertLessEqual(names, set(Q.GIT_RUN))
        job = {**{k: 'x' for k in Q.GIT_RUN}, 'PATH': '/bin'}
        plain = {'PATH': '/bin', **FORCED}
        self.assertEqual(Q.git_env(job), plain)   # a call that reaches no remote runs none of them
        # https with the queue's own login (or none): no ssh, no git:// proxy, no prompt, git's default protocols;
        # git's own helpers (remote-https, index-pack) are found through GIT_EXEC_PATH
        self.assertEqual(Q.git_env(job, 'https://github.com/o/r.git'), {**plain, 'GIT_EXEC_PATH': 'x'})
        self.assertEqual(Q.git_env(job, 'Https://github.com/o/r.git'), {**plain, 'GIT_EXEC_PATH': 'x'})
        for remote in ('origin', '/srv/r.git', 'file:///srv/r.git', 'ssh://git@host/r.git', 'git@host:r.git',
                       'http://host/r.git'):   # #450 item 6: plain http is no GitHub fetch either
            with self.subTest(remote):   # the caller's remote: it may need them, so they stay
                self.assertEqual(Q.git_env(job, remote), {**job, **plain})

    def test_a_fetch_from_github_writes_no_trace(self):   # #450 item 5: the gate puts git's stderr into a public summary
        trace = {k: '1' for k in ('GIT_TRACE', 'GIT_TRACE_CURL', 'GIT_TRACE_PACKET', 'GIT_TRACE_PACK_ACCESS',
                                  'GIT_TRACE2', 'GIT_TRACE2_EVENT', 'GIT_CURL_VERBOSE')}
        trace['GIT_TRACE_REDACT'] = '0'
        job = {**trace, 'PATH': '/bin'}
        for remote in (None, 'https://github.com/o/r.git', 'HTTPS://GitHub.com/o/r.git'):   # #457 item 4: local calls
            with self.subTest(remote):   # (the gate's merge-tree, the queue's), and https in any case
                self.assertEqual(Q.git_env(job, remote), {'PATH': '/bin', **FORCED})
        for remote in ('origin', 'ssh://git@host/r.git'):   # no token of the queue's goes there
            with self.subTest(remote):
                self.assertLessEqual(trace.items(), Q.git_env(job, remote).items())

    def test_only_a_url_or_a_path_is_a_remote_a_token_goes_to(self):   # #457 item 4
        for remote in ('https://github.com/o/r.git', 'HTTPS://github.com/o/r.git', 'origin', '/srv/r.git',
                       'file:///srv/r.git', 'ssh://git@host/r.git', 'git@host:r.git', 'C:/srv/r.git'):
            with self.subTest(remote):
                self.assertIsNone(Q.remote_problem(remote))
        for remote, said in (('', 'empty'), (None, 'empty'), ('http://github.com/o/r.git', 'plain http'),
                             ('HTTP://github.com/o/r.git', 'plain http'), ('ext::sh -c id', 'remote helper'),
                             ('fd::3', 'remote helper'), ('https::github.com/o/r.git', 'remote helper'),
                             ('--upload-pack=touch /tmp/x', 'starts with -'), ('-oProxy', 'starts with -')):
            with self.subTest(remote):
                self.assertIn(said, Q.remote_problem(remote))

    def test_git_speaks_one_language(self):   # #450 item 4: messages are matched and decoded as UTF-8
        env = Q.git_env({'LC_ALL': 'de_DE.ISO-8859-1', 'LANG': 'de_DE', 'LANGUAGE': 'de', 'LC_MESSAGES': 'de_DE'})
        self.assertEqual(env['LC_ALL'], 'C')   # LANGUAGE is ignored under LC_ALL=C (gettext)
        for remote in (None, 'https://github.com/o/r.git', 'origin'):
            with self.subTest(remote):
                self.assertEqual(Q.git_env({'LC_ALL': 'de_DE'}, remote)['LC_ALL'], 'C')

    def test_a_partial_clone_names_the_sparse_pattern_only_for_a_missing_blob(self):   # #450 item 6: cat-file -e
        self.git('config', 'extensions.partialClone', 'origin')
        self.git('config', 'remote.origin.promisor', 'true')
        real = subprocess.run

        def run(cmd, **kw):   # the blob is there, and cat-file blob fails anyway
            if 'cat-file' in cmd and 'blob' in cmd:
                return subprocess.CompletedProcess(cmd, 128, b'', b'fatal: some other failure')
            return real(cmd, **kw)
        for _ in range(2):
            with self.assertRaises(config.ConfigError) as e:
                Q.committed_config(self.config, run=run)
            self.assertIn('some other failure', str(e.exception))
            self.assertNotIn('sparse-checkout', str(e.exception))

    def test_the_config_read_runs_no_program_of_the_job(self):   # #442 item 2: it reaches no remote
        job = {k: os.path.join(self.d, 'nowhere') for k in Q.GIT_RUN}
        for env in self.envs_of_the_read(job):
            self.assertEqual({k: env[k] for k in Q.GIT_RUN if k in env}, {'GIT_ALLOW_PROTOCOL': 'none'})

    def test_a_blob_missing_from_a_full_clone_is_not_blamed_on_a_sparse_pattern(self):   # #442 item 5
        oid = self.git('rev-parse', 'HEAD:.github/pr-queue.json').decode().strip()
        os.remove(os.path.join(self.repo, '.git', 'objects', oid[:2], oid[2:]))   # a lost or broken object
        for _ in range(2):   # runs again: the same refusal
            out = self.refused('.github/pr-queue.json: cannot read .github/pr-queue.json at ')
            self.assertNotIn('sparse-checkout', out)
            self.assertNotIn('not in this checkout', out)

    def test_an_old_git_is_refused(self):
        self.refused('git 2.43 or newer', git='git 2.43 or newer is needed (GIT_ATTR_SOURCE); found: git 2.42')

    def test_every_problem_is_named_at_once(self):
        self.commit({'.github/pr-queue.json': None})
        self.refused('GitHub-hosted', 'is not owner/name', 'not an absolute path', 'has no .github/pr-queue.json',
                     'git 2.43', repo='x', state='p', env={'RUNNER_ENVIRONMENT': 'github-hosted'},
                     git='git 2.43 or newer')


class GateApp(unittest.TestCase):
    """head_check_app: the head gate's App alone posts head_check. GitHub's rollup names no App, so a job of any
    workflow (a pull request can add one) or a commit status of that name would count by name; with the key the queue
    reads that check by app id instead and drops every other item of the name (../head-check/README.md, "The gate")."""
    APP, H1, H2 = 4242, 'd1' * 20, 'd2' * 20

    def setUp(self):
        d = tempfile.mkdtemp(prefix='pr-queue-gate-app-')
        self.addCleanup(shutil.rmtree, d, True)
        self.q = make_queue(d, config.parse(json.dumps({**CFG_RAW, 'head_check_app': self.APP})))

    @staticmethod
    def run_of(name='review-check', conclusion='SUCCESS', typename='CheckRun'):
        if typename == 'StatusContext':
            return {'__typename': typename, 'context': name, 'state': conclusion}
        return {'__typename': typename, 'workflowName': 'ci', 'name': name, 'startedAt': '2026-11-14T01:00:00Z',
                'status': 'COMPLETED', 'conclusion': conclusion}

    def app_run(self, conclusion='success', app=None, status='completed'):
        return {'name': 'review-check', 'status': status, 'conclusion': conclusion,
                'started_at': '2026-11-14T00:00:00Z', 'app': {'id': self.APP if app is None else app}}

    def sweep(self, forged, runs=None, api_fails=False, cfg=None):
        """#1 (oldest) BEHIND: build and test green, plus `forged` (items named review-check from anyone); the gate
        App's runs of review-check on its head are `runs`. #2 BEHIND, all green, read the same way."""
        if cfg is not None:
            self.q.cfg = cfg
        self.updated, self.asked = [], []
        green = [self.run_of('build'), self.run_of('test')]
        listed = [{'number': n, 'title': 't', 'isDraft': False, 'autoMergeRequest': {'m': 1}, 'mergeStateStatus': 'BEHIND',
                   'createdAt': f'2026-11-07T00:00:0{n}Z', 'statusCheckRollup': green + rollup, 'headRefOid': head,
                   'baseRefName': 'main'} for n, head, rollup in ((1, self.H1, forged), (2, self.H2, []))]

        def gh(*a):
            if a[:2] == ('pr', 'list'):
                return ok(json.dumps(listed))
            if a[:2] == ('pr', 'view'):
                return ok(json.dumps({'headRefOid': self.H1, 'comments': []}))
            if a[:2] == ('pr', 'comment'):
                return ok()
            if a[0] == 'api' and '/check-runs?' in a[1]:
                self.asked.append(a[1])
                if api_fails or (runs is None and self.H1 in a[1]):
                    return failed('HTTP 502')
                if runs == 'odd' and self.H1 in a[1]:
                    return ok('{"check_runs": "x"}')
                mine = runs if self.H1 in a[1] else [self.app_run()]
                return ok(json.dumps({'total_count': len(mine), 'check_runs': mine}))
            raise AssertionError(a)
        self.q.gh = self.q.gh_write = gh
        self.q.update = lambda n, d: self.updated.append(n) or 0
        return run_quiet(self.q.run, False)

    def test_a_green_check_of_the_name_from_a_workflow_does_not_pass_it(self):   # the forged check
        for forged in ([self.run_of()], [self.run_of(conclusion='SUCCESS', typename='StatusContext')]):
            with self.subTest(forged=forged[0]['__typename']):
                code, out = self.sweep(forged, runs=[self.app_run('failure')])
                self.assertEqual((code, self.updated), (0, [2]), out)
                self.assertIn('skip #1: required check review-check failed', out)
        code, out = self.sweep([self.run_of()], runs=[self.app_run('failure')], cfg=CFG)
        self.assertEqual((self.updated, self.asked), ([1], []), 'without the key the name alone counts, as before')

    def test_a_red_check_of_the_name_from_a_workflow_does_not_fail_it(self):   # only the App's run decides
        code, out = self.sweep([self.run_of(conclusion='FAILURE')], runs=[self.app_run('success')])
        self.assertEqual((code, self.updated), (0, [1]), out)

    def test_the_check_is_read_by_app_id_newest_only_on_the_head(self):
        self.sweep([], runs=[self.app_run()])
        self.assertIn(f'repos/{REPO}/commits/{self.H1}/check-runs?check_name=review-check&app_id={self.APP}'
                      '&filter=latest&per_page=100', self.asked)

    def test_a_run_of_another_app_in_the_answer_is_dropped(self):   # never trust the filter alone
        code, out = self.sweep([], runs=[self.app_run('failure', app=15368)])
        self.assertEqual((code, self.updated), (0, [1]), 'its red does not count: the check is missing, not red')
        code, out = self.sweep([], runs=[self.app_run('failure')])
        self.assertEqual((code, self.updated), (0, [2]), out)

    def test_an_unreadable_gate_check_skips_that_pr_only(self):   # unknown is never green
        for runs in (None, 'odd'):
            with self.subTest(runs=runs):
                code, out = self.sweep([self.run_of()], runs=runs)
                self.assertEqual((code, self.updated), (0, [2]), out)
                self.assertIn('skip #1: its review-check check from the gate App is unknown', out)

    def test_a_running_gate_check_is_not_red(self):
        code, out = self.sweep([], runs=[self.app_run(None, status='in_progress')])
        self.assertEqual(code, 0, out)
        self.assertNotIn('skip #1', out)

    def test_the_base_checks_count_only_the_apps_run_too(self):   # commit_checks, for update_when_base_was_red
        nodes = [{'__typename': 'CheckRun', 'name': 'review-check', 'status': 'COMPLETED', 'conclusion': c,
                  'startedAt': t, 'checkSuite': {'app': {'databaseId': app}, 'workflowRun': None}}
                 for c, t, app in (('FAILURE', '2026-11-14T00:00:00Z', self.APP), ('SUCCESS', '2026-11-14T02:00:00Z', 15368))]
        nodes.append({'__typename': 'StatusContext', 'context': 'review-check', 'state': 'SUCCESS'})
        sha = 'e9' * 20
        self.q.gh = self.q.gh_write = lambda *a: ok(json.dumps({'data': {'repository': {'object': {'oid': sha, 'statusCheckRollup': {
            'contexts': {'pageInfo': {'hasNextPage': False}, 'nodes': nodes}}}}}}))
        self.assertEqual(self.q.commit_checks('refs/heads/main', {})[1].get('review-check'),
                         lens.check_state(self.run_of(conclusion='FAILURE')))
        self.q.cfg = CFG
        self.assertNotEqual(self.q.commit_checks('refs/heads/main', {})[1].get('review-check'),
                            lens.check_state(self.run_of(conclusion='FAILURE')), 'without the key the name alone counts')

    def test_the_key_is_checked(self):
        for bad in (0, -1, True, '4242', 1.5, 2 ** 60):
            with self.subTest(bad=bad), self.assertRaisesRegex(config.ConfigError, 'head_check_app is'):
                config.parse(json.dumps({**CFG_RAW, 'head_check_app': bad}))
        self.assertIsNone(CFG.head_check_app)

    def test_a_release_without_the_key_refuses_a_config_with_it(self):   # the rollout order: code first, then key
        old = {k: v for k, v in config.KEYS.items() if k != 'head_check_app'}
        with unittest.mock.patch.object(config, 'KEYS', old), \
                self.assertRaisesRegex(config.ConfigError, "unknown key 'head_check_app'"):
            config.parse(json.dumps({**CFG_RAW, 'head_check_app': self.APP}))


if __name__ == '__main__':
    unittest.main()
