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

import argparse, contextlib, fcntl, io, json, subprocess, unittest, unittest.mock  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import config  # noqa: E402
import lens  # noqa: E402
import pr_queue as Q  # noqa: E402

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
LENS = 'lens'   # built at run time: no line of this file starts with the lens marker
READ_AT = r'(?i)read at\s*:|\blens\s*:'


# --- the World: real commits, a local "GitHub" ----------------------------------------------------------------------
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
    where the process dies ('poll': the first view after update-branch)."""
    def __init__(self, w, h1, after, comments=(), polls=0, fail=(), armed=True, behind=True, kill_at=None):
        self.w = w
        self.head, self.after, self.armed, self.behind, self.updated = h1, after, (h1 if armed else None), behind, False
        self.on_pr, self.polls, self.fail, self.kill_at = list(comments), polls, set(fail), kill_at
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
                'createdAt': '2026-11-07T00:00:00Z', 'statusCheckRollup': [], 'comments': self.on_pr, 'reviews': [],
                'commits': self.commits(), 'labels': []}

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
        if kind == 'list':
            return ok(json.dumps([self.view()]))
        if kind == 'disarm':
            self.armed = None
        elif kind == 'update':
            self.updated = True
            if self.after:
                self.w.set('refs/pull/7/head', self.after)
        elif kind == 'arm':
            self.armed = a[a.index('--match-head-commit') + 1]
        elif kind == 'comment':
            self.comments.append(a[a.index('--body') + 1])
        return ok()

    def order(self):
        return [k for k in map(self.kind, self.calls) if k != 'view']


def note(body, when, login='owner', association='OWNER', edited=False):
    """A comment as gh shows it: by default the owner's, never edited."""
    return {'body': body, 'createdAt': when, 'author': {'login': login}, 'authorAssociation': association,
            'includesCreatedEdit': edited}


def lens_at(name, sha, when='2026-11-07T10:00:00Z'):
    return note(f'{LENS}: {name} · reviewer: x · read at: {sha}\nfindings: none', when)


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
        q.gh = fake
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
        post = {'body': f.comments[0], 'author': {'login': 'owner'}}
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

        def git(*a):
            if a[:1] == ('fetch',) and not any(x.startswith('+refs/') for x in a):
                return argparse.Namespace(returncode=128, stdout='', stderr='fatal: unable to access: timed out')
            return real(*a)
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
        self.assertIn('re-post it', f.comments[0])
        self.assertIn('docs/review.md', f.comments[0])
        self.assertIn('`tool ready --head <sha>: <why>`', f.comments[0])

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


class Main(Base):
    def main(self, fake, dry=False, q=None):
        q = q or self.q
        q.gh = fake
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

    def test_a_merge_state_the_queue_does_not_handle_is_said(self):   # skipped, never silently
        f = self.fake()
        code, out = self.main(self.listing(f, [self.listed(3, 'DIRTY')]))
        self.assertEqual((code, f.order(), f.armed), (0, ['list'], self.c), out)
        self.assertIn('skip #3: merge state DIRTY', out)

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
        q.gh = fake
        return run_quiet(q.recover)

    def plan(self, **kw):
        return dict({'repo': REPO, 'pr': 7, 'old': self.c, 'base': 'main', 'read': self.c}, **kw)

    def saved_plan(self):
        with open(self.q.state) as s:
            return json.load(s)

    def test_armed_again_or_closed_needs_nothing(self):
        f = self.fake()   # still armed: the run was killed before its disarm, or someone re-armed it
        code, out = self.recover(f, self.plan())
        self.assertEqual((code, [f.kind(c) for c in f.calls], f.armed), (0, ['view'], self.c))
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

    def test_a_plan_another_user_could_write_is_kept_aside_and_not_followed(self):
        f = self.fake(armed=False, h1=self.up)
        Q.write_text(self.q.state, json.dumps(self.plan()))
        os.chmod(self.q.state, 0o664)   # group-writable: anyone in the group could have written this plan
        self.q.gh = f
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
        self.q.gh = f
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
        self.q.gh = self.fake()
        code, out = run_quiet(self.q.recover)
        self.assertEqual(code, 0, 'the next runs go on')
        self.assertIn('WARNING: ' + bad, out, 'and say it every run while the file is there')

    def test_the_plan_survives_a_new_process_and_an_emptied_lock_folder(self):   # STATE is its own file
        f = self.fake(after=None)   # the run stops with the PR OFF: GitHub did not show the new head in time
        self.assertEqual(self.update(f)[0], Q.SILENT)
        shutil.rmtree(os.path.dirname(self.q.lock), ignore_errors=True)   # e.g. a lock in /tmp, gone after a reboot
        q = make_queue(self.w.dir, remote=self.w.remote)   # the next run: a new process, the same --state-file
        f.head = self.up
        q.gh = f
        code, out = run_quiet(q.main)
        self.assertEqual(code, 0, out)
        self.assertEqual((f.armed, f.comments), (self.up, []), 'the next run finishes the PR')
        self.assertFalse(self.plan_exists())


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

    def test_a_hidden_lens_comment_counts_like_a_deleted_one(self):
        hidden = dict(lens_at('correctness', self.c, '2026-11-07T11:00:00Z'), isMinimized=True)
        f = self.fake(comments=[lens_at('correctness', self.m0), hidden])
        code, out = self.update(f)
        self.assertIsNone(f.armed, 'the hidden read of C counted: ' + out)


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

    def test_a_cache_with_another_config_or_open_to_others_is_rebuilt(self):
        self.assertTrue(self.fetch(self.c)[0])
        with open(os.path.join(self.q.cache, 'config'), 'a') as f:   # fetch "GitHub" from somewhere else
            f.write(f'[url "{os.path.join(self.w.dir, "elsewhere.git")}"]\n\tinsteadOf = {self.q.remote}\n')
        got, out = self.fetch(self.up)
        self.assertTrue(got, out)
        self.assertIn('has a config the queue did not write: rebuilding it', out)
        os.chmod(self.q.cache, 0o770)
        got, out = self.fetch(self.c)
        self.assertTrue(got, out)
        self.assertIn("is not this user's alone", out)
        self.assertEqual(os.stat(self.q.cache).st_mode & 0o777, 0o700)


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
        self.q.gh = fake
        return run_quiet(self.q.update, 7)

    def main(self, fake, q=None):
        q = q or self.q
        q.gh = fake
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
        self.q.gh = fake
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
        self.q.gh = f
        run_quiet(self.q.main)   # recover: `pr view 9` fails (HTTP 502)
        self.assertTrue(os.path.exists(self.q.state), 'STATE for PR 9 was dropped')
        self.assertEqual((seven.armed, seven.updated), (self.read, False), 'PR 7 was updated meanwhile')
        run_quiet(self.q.main)   # the next run: gh works again
        self.assertTrue(f.armed9 == h9 or f.comments9, 'PR 9: OFF since the stopped run, never re-armed, no comment')
        self.assertFalse(os.path.exists(self.q.state))


# --- what the port adds ----------------------------------------------------------------------------------------------
class Arm(unittest.TestCase):
    def armed_with(self, **change):
        d = tempfile.mkdtemp(prefix='pr-queue-arm-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d, config.parse(json.dumps({**CFG_RAW, **change})))
        calls = []
        q.gh = lambda *a: calls.append(a) or ok()
        q.arm(7, H1)
        return calls[0]

    def test_the_merge_method_and_branch_deletion_come_from_the_config(self):
        self.assertEqual(self.armed_with(), ('pr', 'merge', '7', '--repo', REPO, '--auto', '--squash', '--delete-branch',
                                             '--match-head-commit', H1))
        self.assertEqual(self.armed_with(merge_method='merge', delete_branch=False),
                         ('pr', 'merge', '7', '--repo', REPO, '--auto', '--merge', '--match-head-commit', H1))
        self.assertIn('--rebase', self.armed_with(merge_method='rebase'))


class GitCalls(unittest.TestCase):
    def test_git_gets_the_login_from_gh_only_and_never_prompts(self):
        d = tempfile.mkdtemp(prefix='pr-queue-git-')
        self.addCleanup(shutil.rmtree, d, True)
        q = make_queue(d)
        seen = []
        with unittest.mock.patch.object(Q.subprocess, 'run', lambda argv, **kw: seen.append((argv, kw)) or ok()):
            q.git('fetch', 'x')
        (argv, kw), = seen
        self.assertEqual(argv[:11], ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
                                     '--no-replace-objects', '-c', 'credential.https://github.com.helper=',
                                     '-c', 'credential.https://github.com.helper=!gh auth git-credential', '--git-dir'])
        self.assertEqual(argv[11:], [q.cache, 'fetch', 'x'])
        self.assertEqual(kw['env']['GIT_TERMINAL_PROMPT'], '0')

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

    def test_merge_tree_write_tree_needs_2_38(self):
        self.assertIn('git 2.38 or newer is needed', self.problem('git version 2.34.1\n'))
        self.assertIn('2.34.1', self.problem('git version 2.34.1\n'))
        for fine in ('git version 2.38.0', 'git version 2.47.1.windows.1', 'git version 3.0.0'):
            self.assertIsNone(self.problem(fine), fine)

    def test_no_git_or_odd_output_is_a_problem(self):
        self.assertIn('No such file', self.problem(exc=FileNotFoundError(2, 'No such file or directory')))
        self.assertIn('found: none', self.problem(''))
        self.assertIsNotNone(self.problem('version 9'))

    def test_this_git_is_new_enough(self):   # the tests above need merge-tree --write-tree too
        self.assertIsNone(Q.git_problem())


class Cli(unittest.TestCase):
    """cli() refuses with exit 2 and builds no Queue when it cannot run safely; it names every problem at once."""
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix='pr-queue-cli-')
        self.addCleanup(shutil.rmtree, self.d, True)
        self.config = os.path.join(self.d, 'queue.json')
        with open(self.config, 'w') as f:
            json.dump(CFG_RAW, f)
        self.built = []

    def cli(self, *extra, repo=REPO, state=None, cache=None, env=None, git=None):
        test = self

        class Built:
            def __init__(self, *a):
                self.a = a

            def main(self, dry):
                test.built.append((self.a, dry))
                return 0
        argv = [f'--repo={repo}', '--config', self.config, '--state-file', state or os.path.join(self.d, 'run', 'plan.json'),
                '--cache', cache or os.path.join(self.d, 'cache.git'), *extra]
        with unittest.mock.patch.object(Q, 'Queue', Built), unittest.mock.patch.object(Q, 'git_problem', lambda: git):
            return run_quiet(Q.cli, argv, env if env is not None else {})

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
        with open(self.config, 'w') as f:
            json.dump({**CFG_RAW, 'required_check': ['build']}, f)
        self.refused("unknown key 'required_check'")
        os.remove(self.config)
        self.refused('cannot read it')

    def test_an_old_git_is_refused(self):
        self.refused('git 2.38 or newer', git='git 2.38 or newer is needed (merge-tree --write-tree); found: git 2.34')

    def test_every_problem_is_named_at_once(self):
        os.remove(self.config)
        self.refused('GitHub-hosted', 'is not owner/name', 'not an absolute path', 'cannot read it', 'git 2.38',
                     repo='x', state='p', env={'RUNNER_ENVIRONMENT': 'github-hosted'}, git='git 2.38 or newer')


if __name__ == '__main__':
    unittest.main()
