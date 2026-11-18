"""Law tests of pr_queue.py (run: python3 -I -B fork-ci/actions/pr-queue/pr_queue_laws.test.py).

The contract these keep: fork-ci/laws/README.md. pr_queue.test.py tests each step of the queue by hand; this file
plays generated histories against the real Queue: people arm and disarm, authors push (new content, a merge of main, an
edit of a main file, a force-push back), lenses post reads (of the head, of an older head, of no commit), CI passes,
fails or runs, main moves, GitHub merges, and queue runs come in between, some killed half-way (at a gh call, after a
write GitHub applied, at a git call), some with a gh call that fails or that GitHub applied but reported as failed,
with a push, a main move or a CI result in the middle of the run, with a head GitHub shows late, and with arm
refusals "required check is queued". The start state is EMPTY (no plan, no cache) or OLD (a cache an earlier run left,
maybe emptied, a plan in an older queue's format without `repo`, a `.bad` plan, folders a killed run left). Every run
is a new Queue on the same STATE, cache and lock: what a rerun after a stop sees.

The laws, checked at every gh call and after every run:
  L1 the queue arms only a change someone vouched for: the head it names in --match-head-commit is exactly the lenses'
     common read (with no lens comments: a head the PR had while armed) merged with some commit main had, and it
     never merges itself (every `pr merge` is --disable-auto, or --auto with --match-head-commit).
  L2 while a plan names a PR, the queue writes to no other PR; a run turns auto-merge off on at most one PR.
  L3 update-branch only on a PR this run turned off, while it is off.
  L4 never off and silent: after every run (killed or not), each PR the queue turned off is armed again, was told on
     the PR (leave_off, tell_once), or is named by the plan the next run finishes.
  L5 only allowed moves: the queue turns off only an armed PR and arms only a PR it turned off itself (never one a
     person left off or never armed). The one exception is documented (README.md, "A plan does not say who turned
     auto-merge off"): the PR a stopped run's plan names is re-armed even if a person disarmed it after the stop.
  L6 a stopped run is always finished: one run without faults after any history leaves no plan.

The model of GitHub is real git (a local bare repository; fetch by sha as on GitHub) and a fake `gh`, as in
pr_queue.test.py. The guard below fails the run if anything starts the real gh; nothing reaches the network.
"""
import atexit, os, shlex, shutil, sys, tempfile

# --- guard: first, before anything can start gh or git (as pr_queue.test.py) -----------------------------------------
sys.dont_write_bytecode = True
_GUARD = tempfile.mkdtemp(prefix='pr-queue-laws-guard-')
_GH_RAN = os.path.join(_GUARD, 'gh-ran')
os.makedirs(os.path.join(_GUARD, 'bin'))
os.makedirs(os.path.join(_GUARD, 'gh-config'))
with open(os.path.join(_GUARD, 'bin', 'gh'), 'w') as _f:
    _f.write(f'#!/bin/sh\necho "$*" >> {shlex.quote(_GH_RAN)}\n'
             'echo "pr_queue_laws.test.py: a test started the real gh" >&2\nexit 97\n')
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
        os._exit(97)


atexit.register(_gh_never_ran)

import contextlib, io, json, subprocess, unittest  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, '..', '..', 'laws'))
import config  # noqa: E402
import laws  # noqa: E402
import pr_queue as Q  # noqa: E402
from laws import Broken  # noqa: E402

REPO = 'owner/proj'
CFG = config.parse(json.dumps({
    'required_checks': ['build', 'test', 'review-check'], 'merge_method': 'squash', 'delete_branch': True,
    'bot_logins': ['review-bot'], 'lens_author_associations': ['OWNER', 'COLLABORATOR'],
    'override': {'prefix': 'tool ready --head', 'author_association': 'OWNER'}, 'ready_command': 'tool ready {pr}',
    'push_command': 'tool push', 'lens_format_doc': 'docs/review.md', 'head_check': 'review-check'}))
CHECKS = ('build', 'test', 'review-check')
BOGUS = 'de' * 20   # a full sha that is no commit
LENS = 'lens'   # built at run time: no line of this file starts with the lens marker
OFF_SAID = ('pr_queue turned auto-merge OFF', 'pr_queue was stopped half-way')
QUEUED = 'GraphQL: Required status check "build" is queued (mergePullRequest)'
HUB_ENV = {**os.environ, 'GIT_AUTHOR_NAME': 't', 'GIT_AUTHOR_EMAIL': 't@t', 'GIT_COMMITTER_NAME': 't',
           'GIT_COMMITTER_EMAIL': 't@t', 'GIT_AUTHOR_DATE': '1762473600 +0000',
           'GIT_COMMITTER_DATE': '1762473600 +0000'}   # fixed dates: the same steps make the same shas
SEED, TRIES = 464, 60   # CI: this seed and these tries (fork-ci/laws/README.md); LAWS_SEED / LAWS_TRIES for more

# What the fixed seed must reach (laws.Coverage): a generator that drifts into proving nothing goes red.
MUST_SEE = (
    'start EMPTY', 'start OLD', 'old plan without repo', 'old cache emptied', 'leftover folders',
    'queue armed after update', 'recover armed', 'left OFF with a comment', 'told once (tell_once)',
    'killed with a plan on disk', 'killed after GitHub applied a write', 'killed at a git call',
    'gh call failed', 'applied but reported failed', 'event in the middle of a run', 'head shown late past the polls',
    'arm refused: check queued', 'not exactly the change read', 'lenses disagree', 'merge state DIRTY',
    'merge state UNKNOWN', 'merged by GitHub', 'red check skipped',
)


class Killed(BaseException):
    """The process dies here (the job timeout, kill -9)."""


def ok(out=''):
    return subprocess.CompletedProcess(['gh'], 0, out, '')


def failed(err):
    return subprocess.CompletedProcess(['gh'], 1, '', err)


class PR:
    def __init__(self, n, head, created):
        self.n, self.head, self.shown, self.lag, self.history = n, head, head, 0, [head]
        self.state, self.armed, self.mode = 'OPEN', False, 'never'   # mode: armed, queue-off, human-off, never
        self.armed_heads, self.told = set(), False   # heads it had while armed; queue said OFF since its disarm
        self.comments, self.lenses, self.created, self.unknown = [], [], created, 0


class World:
    """GitHub (a bare repository, PRs, checks, comments) and the queue's folders, with the laws checked as the queue
    calls gh."""

    def __init__(self, folder):
        self.dir, self.hub = folder, os.path.join(folder, 'hub.git')
        self.state = os.path.join(folder, 'state', 'plan.json')
        self.cache = os.path.join(folder, 'cache.git')
        self.lock = os.path.join(folder, 'lock', 'queue.lock')
        subprocess.run(['git', 'init', '-q', '--bare', '-b', 'main', self.hub], check=True, capture_output=True,
                       env=HUB_ENV)
        self.g('config', 'uploadpack.allowAnySHA1InWant', 'true')
        self.blobs, self.files, self.anc, self.base_of, self.merges = {}, {}, {}, {}, {}
        self.made = self.clock = self.runs = 0
        self.main, self.prs, self.checks = [], {}, {}
        self.seen, self.out, self.step_no = set(), '', 0
        self.in_run, self.faults, self.events, self.queued, self.update_lag = False, {}, {}, 0, 0
        self.calls = self.git_calls = 0
        self.disarmed_now = set()

    # --- git on "GitHub" --------------------------------------------------------------------------------------------
    def g(self, *a, input=None, codes=(0,)):
        r = subprocess.run(['git', '--git-dir', self.hub, *a], input=input, text=True, capture_output=True,
                           env=HUB_ENV)
        if r.returncode not in codes:
            raise RuntimeError(f'hub git {a}: {r.stderr}')
        return r

    def now(self):
        self.clock += 1
        c = self.clock
        return f'2026-11-07T{c // 3600:02d}:{c // 60 % 60:02d}:{c % 60:02d}Z'

    def blob(self, text):
        if text not in self.blobs:
            self.blobs[text] = self.g('hash-object', '-w', '--stdin', input=text).stdout.strip()
        return self.blobs[text]

    def commit(self, files, parents, base=None, tree=None):
        """A commit with exactly `files` ({path: blob}); `base`: the main commit its change is made on."""
        if tree is None:
            listing = ''.join(f'100644 blob {b}\t{p}\n' for p, b in sorted(files.items()))
            tree = self.g('mktree', input=listing).stdout.strip()
        self.made += 1
        sha = self.g('commit-tree', tree, *[x for p in parents for x in ('-p', p)], '-m', f'c{self.made}').stdout.strip()
        self.files[sha], self.base_of[sha] = dict(files), base or sha
        self.anc[sha] = frozenset({sha}).union(*(self.anc[p] for p in parents))
        return sha

    def merged(self, a, b):
        """git's clean merge of commits a and b as (tree, files), or None for a conflict."""
        if (a, b) not in self.merges:
            r = self.g('merge-tree', '--write-tree', a, b, codes=(0, 1))
            if r.returncode:
                self.merges[(a, b)] = None
            else:
                tree = r.stdout.split()[0]
                files = {}
                for line in self.g('ls-tree', tree).stdout.splitlines():
                    meta, path = line.split('\t', 1)
                    files[path] = meta.split()[2]
                self.merges[(a, b)] = (tree, files)
        return self.merges[(a, b)]

    def tip(self):
        return self.main[-1]

    def set_main(self, sha):
        self.main.append(sha)
        self.g('update-ref', 'refs/heads/main', sha)
        for p in self.prs.values():
            p.unknown = 0

    def set_head(self, pr, sha, lag=0):
        pr.head = sha
        if sha not in pr.history:
            pr.history.append(sha)
        if lag:
            pr.lag = lag
        else:
            pr.shown, pr.lag = sha, 0
        self.g('update-ref', f'refs/pull/{pr.n}/head', sha)
        if pr.armed:   # GitHub keeps auto-merge on after a push by someone with write access
            pr.armed_heads.add(sha)

    # --- the history's steps ------------------------------------------------------------------------------------------
    def start(self, kind='EMPTY', plan=0, bad=0, leftover=0, cache='good'):
        self.set_main(self.commit({'main.txt': self.blob('main v0\n')}, ()))
        self.open_pr()
        self.open_pr()
        if kind != 'OLD':
            self.seen.add('start EMPTY')
            return
        self.seen.add('start OLD')
        self.human('arm', 2)
        self.main_push('file')
        self.run_queue(())   # an earlier queue run: it makes the cache
        if cache == 'emptied' and os.path.isdir(self.cache):
            shutil.rmtree(self.cache)
            os.mkdir(self.cache)
            self.seen.add('old cache emptied')
        if plan:   # an older queue stopped right after its disarm of #1; its plan has no `repo`
            p = self.prs[1]
            p.armed_heads.add(p.head)
            p.mode, p.told = 'queue-off', False
            Q.write_text(self.state, json.dumps({'pr': 1, 'old': p.head, 'base': 'main', 'read': p.head}))
            self.seen.add('old plan without repo')
        if bad:
            Q.write_text(self.state + '.bad', '{"pr": ')
        if leftover:
            for name in (Q.MERGE_COPY, '.new', '.old-1'):
                os.makedirs(os.path.join(self.cache + name, 'objects'), exist_ok=True)
            self.seen.add('leftover folders')

    def open_pr(self):
        n = len(self.prs) + 1
        if n > 4:
            return
        tip = self.tip()
        sha = self.commit({**self.files[tip], f'p{n}.txt': self.blob(f'pr {n} v0\n')}, (tip,), base=tip)
        self.prs[n] = PR(n, sha, self.now())
        self.g('update-ref', f'refs/pull/{n}/head', sha)

    def open(self, n):
        p = self.prs.get(n)
        return p if p and p.state == 'OPEN' else None

    def human(self, what, n):
        p = self.open(n)
        if not p:
            return
        if what == 'arm' and not p.armed:
            p.armed, p.mode = True, 'armed'
            p.armed_heads.add(p.head)
        elif what == 'disarm' and p.armed:
            p.armed, p.mode = False, 'human-off'

    def push(self, n, kind, lag=0):
        p = self.open(n)
        if not p:
            return
        head, files = p.head, self.files[p.head]
        if kind == 'new':
            sha = self.commit({**files, f'p{n}.txt': self.blob(f'pr {n} v{self.made}\n')}, (head,), self.base_of[head])
        elif kind == 'main_file':   # the PR edits a file main owns: a conflict once main edits it too
            sha = self.commit({**files, 'main.txt': self.blob(f'pr {n} edits main {self.made}\n')}, (head,),
                              self.base_of[head])
        elif kind == 'same':   # the author merges main in: the same change on a newer main
            m = self.merged(head, self.tip())
            if not m or self.tip() in self.anc[head]:
                return
            sha = self.commit(m[1], (head, self.tip()), self.tip(), tree=m[0])
        elif kind == 'back':   # a force-push back to the first head
            sha = p.history[0]
        else:
            raise ValueError(kind)
        self.set_head(p, sha, lag)

    def lens(self, n, name, target):
        p = self.open(n)
        if not p:
            return
        sha = {'head': p.head, 'old': p.history[len(p.lenses) % len(p.history)], 'bogus': BOGUS}[target]
        p.lenses.append((name, sha))
        p.comments.append({'body': f'{LENS}: {name} · reviewer: x · read at: {sha}\nfindings: none',
                           'createdAt': self.now(), 'author': {'login': 'owner'}, 'authorAssociation': 'OWNER',
                           'includesCreatedEdit': False})

    def ci(self, n, outcome):
        p = self.open(n)
        if not p:
            return
        when = self.now()
        self.checks[p.head] = {c: ('' if outcome == 'pending' else 'FAILURE' if outcome == 'fail' and c == 'test'
                                   else 'SUCCESS', when) for c in CHECKS}

    def main_push(self, kind, unknown=0):
        tip = self.tip()
        files = dict(self.files[tip])
        if kind == 'edit':
            files['main.txt'] = self.blob(f'main v{self.made}\n')
        else:
            files[f'm{self.made}.txt'] = self.blob(f'main file {self.made}\n')
        self.set_main(self.commit(files, (tip,)))
        for p in self.prs.values():
            p.unknown = unknown

    def tick(self):
        """GitHub's auto-merge: the first armed PR whose head is up to date and green is squashed onto main."""
        for p in sorted(self.prs.values(), key=lambda p: p.n):
            if p.state == 'OPEN' and p.armed and self.tip() in self.anc[p.head] and self.green(p.head):
                tree = self.g('rev-parse', p.head + '^{tree}').stdout.strip()
                self.set_main(self.commit(self.files[p.head], (self.tip(),), tree=tree))
                p.state, p.armed = 'MERGED', False
                self.seen.add('merged by GitHub')
                return

    def green(self, sha):
        c = self.checks.get(sha) or {}
        return all((c.get(k) or ('',))[0] == 'SUCCESS' for k in CHECKS)

    def apply(self, step):
        kind, *a = step
        if kind == 'arm' or kind == 'disarm':
            self.human(kind, *a)
        elif kind == 'push':
            self.push(*a)
        elif kind == 'lens':
            self.lens(*a)
        elif kind == 'ci':
            self.ci(*a)
        elif kind == 'main':
            self.main_push(*a)
        elif kind == 'open':
            self.open_pr()
        elif kind == 'tick':
            self.tick()
        elif kind == 'run':
            self.run_queue(*a)
        else:
            raise ValueError(f'unknown step {step!r}')

    # --- GitHub as gh shows it --------------------------------------------------------------------------------------
    def shown(self, p):
        if p.lag:
            p.lag -= 1
        else:
            p.shown = p.head
        return p.shown

    def merge_state(self, p, head):
        if p.unknown:
            p.unknown -= 1
            self.seen.add('merge state UNKNOWN')
            return 'UNKNOWN'
        if self.tip() in self.anc[head]:
            return 'CLEAN' if self.green(head) else 'BLOCKED'
        if self.merged(head, self.tip()) is None:
            self.seen.add('merge state DIRTY')
            return 'DIRTY'
        return 'BEHIND'

    def render(self, p, poster=False):
        head = self.shown(p) if p.state == 'OPEN' else p.head
        rollup = [{'__typename': 'CheckRun', 'name': k, 'workflowName': 'ci', 'startedAt': when,
                   'status': 'COMPLETED' if c else 'IN_PROGRESS', 'conclusion': c}
                  for k, (c, when) in sorted((self.checks.get(head) or {}).items())]
        comments = [dict(c, viewerDidAuthor=bool(c.get('mine')) and poster) for c in p.comments]
        return {'number': p.n, 'title': f'pr {p.n}', 'state': p.state, 'isDraft': False,
                'autoMergeRequest': {'mergeMethod': 'SQUASH'} if p.armed else None,
                'mergeStateStatus': self.merge_state(p, head) if p.state == 'OPEN' else 'UNKNOWN',
                'createdAt': p.created, 'statusCheckRollup': rollup, 'headRefOid': head, 'baseRefName': 'main',
                'comments': comments, 'reviews': [],
                'commits': [{'oid': s} for s in p.history if s in self.anc[head] and s not in self.anc[self.tip()]]}

    def gh_call(self, write, a):
        self.calls += 1
        k = self.calls
        for ev in self.events.pop(k, ()):
            self.seen.add('event in the middle of a run')
            self.apply(ev)
        fault = self.faults.get(k)
        if fault == 'kill':
            raise Killed(f'gh call {k}')
        if fault == 'fail':
            self.seen.add('gh call failed')
            return failed('HTTP 502: Bad Gateway')
        r = (self.gh_write if write else self.gh_read)(a)
        if fault == 'kill_after':
            if write:
                self.seen.add('killed after GitHub applied a write')
            raise Killed(f'after gh call {k}')
        if fault == 'lie' and r.returncode == 0:
            self.seen.add('applied but reported failed' if write else 'gh call failed')
            return failed('HTTP 502: Bad Gateway')
        return r

    def gh_read(self, a, poster=False):
        if a[:2] == ('pr', 'list'):
            return ok(json.dumps([self.render(p) for p in self.prs.values() if p.state == 'OPEN']))
        if a[:2] == ('pr', 'view'):
            p = self.prs.get(int(a[2]))
            return ok(json.dumps(self.render(p, poster))) if p else failed('no such PR')
        if a[:2] == ('api', 'graphql') and f'query={Q.REVIEW_FLAGS_QUERY}' in a:
            page = {'pageInfo': {'hasNextPage': False, 'endCursor': None}, 'nodes': []}
            return ok(json.dumps({'data': {'repository': {'pullRequest': {'reviews': page}}}}))
        if a[:2] == ('run', 'list'):
            c = self.checks.get(a[a.index('--commit') + 1]) or {}
            if not c:
                return ok('[]')
            done = 'completed' if all(x for x, _ in c.values()) else 'in_progress'
            return ok(json.dumps([{'status': done, 'workflowName': 'ci'}]))
        raise Broken('unknown gh call', f'the queue read {a[:3]} that this model does not answer')

    def plan_pr(self):
        try:
            with open(self.state) as f:
                return json.load(f).get('pr')
        except (OSError, ValueError, AttributeError):
            return None

    def gh_write(self, a):
        if a[:2] == ('pr', 'view'):   # tell(): the poster's own read (viewerDidAuthor)
            return self.gh_read(a, poster=True)
        if a[:2] not in (('pr', 'merge'), ('pr', 'update-branch'), ('pr', 'comment')):
            raise Broken('unknown gh call', f'the queue wrote {a[:3]} that this model does not answer')
        n = int(a[2])
        p = self.prs[n]
        plan = self.plan_pr()
        if plan is not None and plan != n:
            raise Broken('L2 one PR at a time', f'{" ".join(a[:2])} on #{n} while the plan names #{plan}')
        if a[:2] == ('pr', 'merge'):
            if '--disable-auto' in a:
                return self.disarm(p)
            if '--auto' not in a or '--match-head-commit' not in a:
                raise Broken('L1 arms only what was read', f'a merge call that is no armed match-head: {a}')
            return self.arm(p, a[a.index('--match-head-commit') + 1])
        if a[:2] == ('pr', 'update-branch'):
            return self.update_branch(p)
        body = a[a.index('--body') + 1]
        p.comments.append({'body': body, 'createdAt': self.now(), 'author': {'login': 'queue-bot'},
                           'authorAssociation': 'NONE', 'includesCreatedEdit': False, 'mine': True})
        if body.startswith(OFF_SAID) and p.mode == 'queue-off':
            p.told = True
            self.seen.add('told once (tell_once)' if body.startswith(OFF_SAID[1]) else 'left OFF with a comment')
        return ok()

    def disarm(self, p):
        if not p.armed or p.state != 'OPEN':
            raise Broken('L5 allowed moves', f'the queue turned off #{p.n}, which is {p.mode}, not armed')
        self.disarmed_now.add(p.n)
        if len(self.disarmed_now) > 1:
            raise Broken('L2 one PR at a time', f'one run turned off #{sorted(self.disarmed_now)}')
        p.armed, p.mode, p.told = False, 'queue-off', False
        return ok()

    def vouched(self, p):
        """The commits whose change may be armed: the lenses' common read, or with no lens comments the heads the PR
        had while armed (GitHub would have merged those anyway). Empty when the lenses disagree."""
        if not p.lenses:
            return set(p.armed_heads)
        newest = dict(p.lenses)
        return set(newest.values()) if len(set(newest.values())) == 1 else set()

    def exactly_read(self, head, read):
        """head is read's change (its diff from the main commit it was made on) applied to some main commit."""
        if head not in self.files or read not in self.files:
            return False
        was, now = self.files[self.base_of[read]], self.files[read]
        for m in self.main:
            want = dict(self.files[m])
            for path in set(was) | set(now):
                if was.get(path) != now.get(path):
                    if path in now:
                        want[path] = now[path]
                    else:
                        want.pop(path, None)
            if want == self.files[head]:
                return True
        return False

    def arm(self, p, head):
        if p.mode == 'human-off' and self.in_recover() and self.plan_pr() == p.n:
            self.seen.add('re-armed after a person disarmed it (README)')   # "A plan does not say who turned it off"
        elif p.mode != 'queue-off':
            raise Broken('L5 allowed moves', f'the queue armed #{p.n}, which is {p.mode}: only a PR it turned off')
        reads = self.vouched(p)
        if not any(self.exactly_read(head, r) for r in reads):
            raise Broken('L1 arms only what was read',
                         f'#{p.n} armed at {head[:12]}, which is no change of {sorted(r[:12] for r in reads)} on main')
        if p.state != 'OPEN':
            return failed('Pull request is not open')
        if head != p.head:
            return failed('GraphQL: Head branch was modified. Review and try the merge again. (mergePullRequest)')
        if self.queued:
            self.queued -= 1
            self.seen.add('arm refused: check queued')
            return failed(QUEUED)
        p.armed, p.mode = True, 'armed'
        p.armed_heads.add(head)
        self.seen.add('recover armed' if self.in_recover() else 'queue armed after update')
        return ok()

    def in_recover(self):
        return not self.disarmed_now

    def update_branch(self, p):
        if p.n not in self.disarmed_now or p.armed:
            raise Broken('L3 update only what it turned off', f'update-branch on #{p.n} ({p.mode}, armed {p.armed})')
        tip = self.tip()
        if tip in self.anc[p.head]:
            return failed('HEAD is already up to date with main')
        m = self.merged(p.head, tip)
        if m is None:
            return failed('merge conflict between base and head')
        self.set_head(p, self.commit(m[1], (p.head, tip), tip, tree=m[0]), self.update_lag)
        if self.update_lag > CFG.wait_seconds // CFG.poll_seconds:
            self.seen.add('head shown late past the polls')
        return ok()

    # --- a queue run --------------------------------------------------------------------------------------------------
    def run_queue(self, faults=()):
        self.runs += 1
        self.calls = self.git_calls = 0
        self.faults, self.events, self.queued, self.update_lag, git_kill = {}, {}, 0, 0, None
        for f in faults:
            if f[0] in ('kill', 'kill_after', 'fail', 'lie'):
                self.faults.setdefault(f[1], f[0])
            elif f[0] == 'event':
                self.events.setdefault(f[1], []).append(f[2])
            elif f[0] == 'queued':
                self.queued = f[1]
            elif f[0] == 'lag':
                self.update_lag = f[1]
            elif f[0] == 'git_kill':
                git_kill = f[1]
            else:
                raise ValueError(f'unknown fault {f!r}')
        self.disarmed_now = set()
        q = Q.Queue(REPO, CFG, self.state, self.cache, self.lock, sleep=lambda s: None)
        q.remote = self.hub
        q.gh = lambda *a: self.gh_call(False, a)
        q.gh_write = lambda *a: self.gh_call(True, a)
        real_git = q.git

        def git(*a, **kw):
            self.git_calls += 1
            if self.git_calls == git_kill:
                self.seen.add('killed at a git call')
                raise Killed(f'git call {self.git_calls}')
            return real_git(*a, **kw)
        q.git = git
        killed = None
        out = io.StringIO()
        try:
            with contextlib.redirect_stdout(out):
                q.main()
        except Killed as e:
            killed = e
        finally:
            q.stop_check()
            self.out = out.getvalue()
        if killed and os.path.exists(self.state):
            self.seen.add('killed with a plan on disk')
        for line, kind in (('is not exactly the change read', 'not exactly the change read'),
                           ('did not all read one head', 'lenses disagree'),
                           ('failed (its author fixes it)', 'red check skipped')):
            if line in self.out:
                self.seen.add(kind)
        self.never_silent(f'after run {self.runs}' + (f' (killed: {killed})' if killed else ''))

    def never_silent(self, when):
        plan = self.plan_pr()
        for p in self.prs.values():
            if p.state == 'OPEN' and p.mode == 'queue-off' and not p.told and plan != p.n:
                raise Broken('L4 never off and silent', f'#{p.n} is off since a queue disarm, nothing said it and no '
                                                        f'plan names it, {when}')

    def settle(self):
        """L6: with GitHub showing heads at once and no faults, one run finishes whatever a stopped run left."""
        for p in self.prs.values():
            p.lag = p.unknown = 0
        self.run_queue(())
        if os.path.exists(self.state):
            raise Broken('L6 a stopped run is always finished', f'a run without faults left the plan for '
                                                               f'#{self.plan_pr()}')


# --- generated histories --------------------------------------------------------------------------------------------
def a_step(rng):
    n = rng.randint(1, 4)
    r = rng.random()
    if r < 0.30:
        return ('run', a_faults(rng))
    if r < 0.42:
        return ('push', n, rng.choice(('new', 'new', 'same', 'same', 'main_file', 'back')),
                rng.choice((0, 0, 0, 1, 2)))
    if r < 0.53:
        return ('lens', n, rng.choice(('a', 'a', 'b')), rng.choice(('head', 'head', 'head', 'old', 'bogus')))
    if r < 0.68:
        return ('ci', n, rng.choice(('pass', 'pass', 'pass', 'fail', 'pending')))
    if r < 0.77:
        return ('main', rng.choice(('file', 'file', 'edit')), rng.choice((0, 0, 0, 1, 3)))
    if r < 0.86:
        return ('arm', n)
    if r < 0.89:
        return ('disarm', n)
    if r < 0.96:
        return ('tick',)
    return ('open',)


def a_faults(rng):
    if rng.random() < 0.3:
        return ()
    out = []
    for _ in range(rng.choice((1, 1, 2))):
        r = rng.random()
        k = rng.choice((1, 2, rng.randint(3, 14)))   # call 1 is recover's view when a plan is on disk
        if r < 0.22:
            out.append((rng.choice(('kill', 'kill', 'kill_after')), k))
        elif r < 0.40:
            out.append((rng.choice(('fail', 'lie')), k))
        elif r < 0.47:
            out.append(('git_kill', rng.randint(1, 40)))
        elif r < 0.67:
            ev = rng.choice((('push', rng.randint(1, 3), rng.choice(('new', 'same')), 0),
                             ('main', 'file', 0), ('ci', rng.randint(1, 3), 'pass')))
            out.append(('event', rng.randint(2, 12), ev))
        elif r < 0.80:
            out.append(('queued', rng.choice((1, 2, 6))))
        else:
            out.append(('lag', rng.choice((1, 3, 13))))
    return tuple(out)


def generate(rng):
    if rng.random() < 0.5:
        start = ('start', 'OLD', rng.choice((0, 1, 1)), rng.choice((0, 1)), rng.choice((0, 1)),
                 rng.choice(('good', 'good', 'emptied')))
    else:
        start = ('start', 'EMPTY')
    steps = [start, ('arm', 1), ('main', 'file', 0)]   # most histories start with something to queue
    steps += [a_step(rng) for _ in range(rng.randint(14, 34))]
    return steps


def play(steps, seen=None):
    """Play one history from a fresh world; Broken for a law that does not hold."""
    with tempfile.TemporaryDirectory(prefix='pr-queue-laws-') as d:
        w = World(d)
        try:
            first = steps[0] if steps and steps[0][0] == 'start' else ('start', 'EMPTY')
            w.start(*first[1:])
            for i, s in enumerate(steps):
                w.step_no = i
                if s[0] != 'start':
                    w.apply(s)
            w.settle()
        except Broken as e:
            tail = '\n'.join(w.out.strip().splitlines()[-6:])
            raise Broken(e.law, f'{e.detail} (step {w.step_no}; the last queue run printed:\n{tail})') from None
        finally:
            if seen is not None:
                seen.update(w.seen)


# Histories a law once broke (laws README, "A failure"), and hand-written ones each law must reject when the queue is
# broken that way. Every list here is played on every run, whatever the seed.
FIXED = {
    # an author pushes new content while the queue's update runs: the new head is not the change read
    'push during the update': [
        ('start', 'EMPTY'), ('arm', 1), ('main', 'file', 0),
        ('run', (('event', 4, ('push', 1, 'new', 0)),)),
    ],
    # a run killed after its disarm; a lens reads the merged head; the next run re-arms only that change
    'killed after the disarm, then recovered': [
        ('start', 'EMPTY'), ('lens', 1, 'a', 'head'), ('arm', 1), ('main', 'file', 0),
        ('run', (('kill_after', 5),)), ('push', 1, 'new', 0), ('run', ()),
    ],
    # an older queue's plan without `repo`, an emptied cache and the folders a killed run left
    'old state': [
        ('start', 'OLD', 1, 1, 1, 'emptied'), ('run', ()),
    ],
    # L5 found this; it is the documented limit (README.md, "A plan does not say who turned auto-merge off"): a run
    # stops after its disarm, a person arms and disarms again, and the next run re-arms the change read
    'a person disarms after a stop': [
        ('start', 'EMPTY'), ('arm', 1), ('main', 'file', 0), ('run', (('kill_after', 4),)), ('arm', 1),
        ('disarm', 1), ('run', ()),
    ],
}


class LawsOfTheQueue(unittest.TestCase):
    def test_fixed_histories(self):
        for name, steps in FIXED.items():
            with self.subTest(name):
                seen = set()
                play(steps, seen)
                if name == 'a person disarms after a stop':   # the README's sentence and the code agree
                    self.assertIn('re-armed after a person disarmed it (README)', seen)

    def test_generated_histories(self):
        seen = set()
        played = laws.check('pr-queue', generate, lambda steps: play(steps, seen), seed=SEED, tries=TRIES,
                            rerun=f'python3 -I -B {os.path.relpath(__file__)}')
        if not any(os.environ.get(k) for k in ('LAWS_SEED', 'LAWS_TRIES', 'LAWS_TRY')):   # the CI run: all of it
            cover = laws.Coverage(MUST_SEE)
            for kind in seen:
                cover.saw(kind)
            cover.check()
        self.assertGreater(played, 0)


if __name__ == '__main__':
    unittest.main()
