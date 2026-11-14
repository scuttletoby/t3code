#!/usr/bin/env python3
"""A merge queue for a repository whose branch ruleset requires pull requests to be up to date with their base
(README.md). Each run brings exactly ONE waiting PR up to date: the oldest open, non-draft PR with auto-merge on that is
BEHIND its base and has no failing required check. Its CI runs, it merges itself, and the run after the next push to
the base moves the queue on. An armed PR that is already up to date holds the queue while its required checks run. One
whose required check ended without passing (cancelled) or never reported cannot merge and nothing re-runs that check:
it is told once per head and passed over, as is a red one, and one that conflicts with its base (DIRTY).

Auto-merge only ever merges a head someone checked. Whether GitHub checks --match-head-commit again after the head
moved is not documented, and "armed" does not say at which head (a plain push keeps auto-merge on), so the queue relies
on neither. For the PR it updates (head H1):
  1. R = the full sha the review vouched for (lens.Review.reviewed_head): the one commit every lens's newest `read at:`
     names, or a newer override comment's; no lens comments: R = H1; lenses that read different commits: R unknown.
     Only comments of the config's lens_author_associations count, shown and unedited: anyone can comment. Inline
     review comments (on a line of the diff) are comments too, read with the reviews' flags (review_flags).
     The base branch, the PR head and R are fetched into the queue's own bare repository (--cache).
  2. The plan goes into STATE (--state-file), then auto-merge OFF; if that fails, nothing else happens (if gh's error
     leaves it unclear whether it is OFF, STATE stays for the next run). R unknown or not on GitHub: the PR stays OFF,
     is not updated and gets a comment, so one such PR cannot stall the queue. A git fetch error changes nothing: the
     PR stays as it was and the next run tries again.
  3. `gh pr update-branch`, then wait until the head moves (at most wait_seconds).
  4. Auto-merge back ON with --match-head-commit H2 only if H2 holds exactly R's change on top of the base (holds()).
     GitHub can refuse that only because the new head's required checks have not started yet: then, and only then,
     the same arm is tried again for at most wait_seconds (arm). Anything else (a push after the lenses, a conflict
     resolution, any gh or git error): it stays OFF with a comment that it needs a lens read and the ready command.
     No new head in time: STATE stays, the next run checks it.
A PR already up to date never reaches these steps: the project's head_check (required) covers its head.
Stopped half-way, the next run finishes that PR first from STATE (recover), against the review and the base as GitHub
shows them then, and touches no other PR until it has re-armed it or said on it why not: a PR is never OFF and silent.
Files the queue trusts are this user's alone: STATE is written 0600 and read only if no one else could have written it;
the cache is used only as the queue made it (own_cache), and git runs there with no hooks and no replace refs.
Exit codes: 0 done; 1 a step failed, the next run tries again; 2 refused to start, nothing touched; 3 a PR may be OFF
and no comment says so yet (STATE stays, the next run finishes it).
    pr_queue.py --repo OWNER/NAME --config FILE --state-file ABS --cache ABS [--lock-file ABS] [--dry-run]
"""
import argparse, fcntl, json, os, re, shutil, stat, subprocess, sys, time

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import lens  # noqa: E402

SHA = re.compile(r'[0-9a-f]{40}')
REPO_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+')
GONE_RE = re.compile(r"not our ref|unadvertised object|couldn't find remote ref|no such remote ref", re.I)
RED_STATES = ('FAILURE', 'ERROR', 'TIMED_OUT')
WAITS_FOR = ('BLOCKED', 'UNSTABLE', 'CLEAN', 'HAS_HOOKS')   # up-to-date merge states whose checks the queue waits for
NEVER = 'never reported'
TESTED_BASE = 'pr_queue tested base'   # the annotation title the tested-base action writes (tested_base.py)
BASE_RED = ('FAILURE', 'ERROR')   # "the base was red": a real failure. CANCELLED, STALE, STARTUP_FAILURE, TIMED_OUT,
# SKIPPED, not reported or running say nothing about the base's code (a busy runner times out or cancels base runs)
UPDATES = 3   # holds() follows at most this many queue updates in a row
GIT_MIN = (2, 38)   # git merge-tree --write-tree
REFUSED, SILENT = 2, 3
# git gets the login from gh (GH_TOKEN, or gh's own login), never from a credential helper of the machine's git config.
GIT_AUTH = ('-c', 'credential.https://github.com.helper=',
            '-c', 'credential.https://github.com.helper=!gh auth git-credential')
# Every git call, whatever the repository's files say: no hooks, no fsmonitor command, no replace refs, and no
# commit-graph (a planted objects/info/commit-graph(s) could change the parents git reports; git rebuilds nothing from it).
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.commitGraph=false',
            '--no-replace-objects')
# The cache's whole config, as the queue writes it: a cache with any other config (a planted `url.*.insteadOf`, helper
# or hook path) is rebuilt, never run.
CACHE_CONFIG = '[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = true\n'
# Files git reads that would make the cache's history or objects other than what was fetched from GitHub: grafts and
# shallow change a commit's parents, alternates adds another repository's objects, and info/attributes can change how
# merge-tree merges a file (merge=union, binary). The queue never writes any of them.
UNTRUSTED_FILES = ('info/grafts', 'shallow', 'objects/info/alternates', 'objects/info/http-alternates',
                   'info/attributes')
VIEW = 'number,state,isDraft,autoMergeRequest,mergeStateStatus,headRefOid,baseRefName,comments,reviews,commits'
LIST = 'number,title,isDraft,autoMergeRequest,mergeStateStatus,createdAt,statusCheckRollup,headRefOid,baseRefName'
# What gh pr view never fetches for reviews (gh 2.102 asks only id, author login, authorAssociation, submittedAt, body,
# state, commit, reactionGroups) yet prints with Go's defaults (`includesCreatedEdit: false`): read with one query of
# our own, by PR, 100 reviews a page. lens.Review trusts a review's flags only from this answer (lens.REVIEW_FLAGS).
# The same query reads what gh pr view does not list at all: each review's inline comments (on a line of the diff;
# every one belongs to a review), 100 a page, for lens.REVIEW_COMMENTS. A review with more is read on with
# MORE_COMMENTS_QUERY, by the review's node id.
INLINE_COMMENTS = ('pageInfo{hasNextPage endCursor} nodes{state body createdAt publishedAt includesCreatedEdit '
                   'isMinimized url authorAssociation author{__typename login}}')
REVIEW_FLAGS_QUERY = ('query($owner:String!,$name:String!,$number:Int!,$after:String){repository(owner:$owner,'
                      'name:$name){pullRequest(number:$number){reviews(first:100,after:$after){pageInfo{hasNextPage '
                      'endCursor} nodes{id includesCreatedEdit isMinimized url author{__typename login} '
                      'comments(first:100){' + INLINE_COMMENTS + '}}}}}}')
MORE_COMMENTS_QUERY = ('query($id:ID!,$after:String!){node(id:$id){... on PullRequestReview{comments(first:100,'
                       'after:$after){' + INLINE_COMMENTS + '}}}}')
REVIEW_PAGES = 50   # pages of 100 reviews or 100 inline comments: a PR with more is not read (fails closed)
# Right after update-branch GitHub can still show the PR as mergeable while the new head's required checks are only
# queued. gh then tries a direct merge instead of turning auto-merge on, and GitHub refuses it: `Required status check
# "build" is queued` (also "in progress", "expected", "pending"; within one sentence). That one refusal is a race, not
# a verdict (arm). Every other refusal (the head moved, a conflict, a failing check, no permission) is final.
CHECK_NOT_DONE_RE = re.compile(r'required status checks?\b[^.\n]*\b(?:is|are) (?:queued|in progress|expected|pending)\b',
                               re.I)


def holds(git, head, read, depth=UPDATES, base='refs/queue/base'):
    """Step 4's rule on commits `git` (a callable running git in one repository) already has: `head` is `read`, or a
    parent P of `head` is on `base` and head's tree is `git merge-tree --write-tree P X` byte for byte, where X is
    `read` or head's other parent when that one holds this itself (an earlier queue update: GitHub merges the new base
    into the head the queue already proved, which can be clean where a direct merge of `read` conflicts). A tree is
    the same or not: a hunk moved to another function, a conflict resolution or a later push is not the same."""
    if head == read:
        return True
    tree = git('rev-parse', '--verify', '-q', head + '^{tree}').stdout.strip()
    parents = git('rev-list', '--parents', '-n', '1', head).stdout.split()[1:]
    for p in parents:
        if git('merge-base', '--is-ancestor', p, base).returncode != 0:
            continue
        sides = [read] + [h for h in parents if h != p and depth > 1 and holds(git, h, read, depth - 1, base)]
        for side in dict.fromkeys(sides):
            m = git('-c', 'merge.directoryRenames=true', 'merge-tree', '--write-tree', p, side)
            if m.returncode == 0 and tree and m.stdout.split()[:1] == [tree]:
                return True
    return False


def connection(found, after):
    """(nodes, next) of one page of a GraphQL connection asked for with `pageInfo{hasNextPage endCursor} nodes`: next is
    None on the last page, the cursor of the next one, or False when GitHub says there are more but gives no new
    cursor. TypeError or KeyError when it is not that shape."""
    page, nodes = found['pageInfo'], found['nodes']
    if not isinstance(nodes, list) or not isinstance(page['hasNextPage'], bool):
        raise TypeError
    if not page['hasNextPage']:
        return nodes, None
    cursor = page.get('endCursor')
    return nodes, cursor if isinstance(cursor, str) and cursor and cursor != after else False


def inline_post(node):
    """An inline review comment as a post lens.Review reads like any PR comment (body, createdAt, author with GitHub's
    own type, authorAssociation, includesCreatedEdit, isMinimized, url), or None for one of a review that is not
    submitted yet (PENDING: only its author sees it, so it is not posted). Its time is when it was published (the
    review's submit), not when it was drafted. TypeError when a field is not what was asked for: a comment nobody could
    read in full must not go missing."""
    texts, flags = ('state', 'body', 'createdAt', 'authorAssociation', 'url'), ('includesCreatedEdit', 'isMinimized')
    if not isinstance(node, dict) or not all(isinstance(node.get(k), bool) for k in flags) \
            or not all(isinstance(node.get(k), str) for k in texts):
        raise TypeError
    if node['state'] == 'PENDING':
        return None
    published, author = node.get('publishedAt'), node.get('author')
    return {'body': node['body'], 'createdAt': published if isinstance(published, str) and published else node['createdAt'],
            'author': author if isinstance(author, dict) else None, 'authorAssociation': node['authorAssociation'],
            'includesCreatedEdit': node['includesCreatedEdit'], 'isMinimized': node['isMinimized'], 'url': node['url']}


def review_flags(gh, repo, n):
    """({review node id: {includesCreatedEdit, isMinimized, url, author}}, [inline review comments], None) for PR n,
    read with REVIEW_FLAGS_QUERY page by page (and MORE_COMMENTS_QUERY for a review with more than 100 inline
    comments); (None, None, why) when any page fails or is not the shape asked for (then nothing about any review or
    inline comment is known, and the caller must not decide). A review node without a boolean includesCreatedEdit and
    isMinimized is left out of the flags: lens.Review counts a review it has no flags for as one that reads nothing.
    Its inline comments are read all the same, as inline_post gives them: gh pr view lists none, so one left out
    would be one nobody reads. Read AFTER gh pr view: a review edited between the two reads then shows here as
    edited, never as an unedited body that is not the one read."""
    owner, name = repo.split('/', 1)
    what = 'reading the reviews\' edit flags and inline comments'
    flags, inline, todo = {}, [], [(None, None)]   # todo: (review node id, or None for the PR's reviews; cursor)
    for _ in range(REVIEW_PAGES):
        if not todo:
            return flags, inline, None
        review, after = todo.pop(0)
        if review is None:
            ask = ('-f', f'query={REVIEW_FLAGS_QUERY}', '-f', f'owner={owner}', '-f', f'name={name}',
                   '-F', f'number={n}', *(['-f', f'after={after}'] if after else []))
        else:
            ask = ('-f', f'query={MORE_COMMENTS_QUERY}', '-f', f'id={review}', '-f', f'after={after}')
        r = gh('api', 'graphql', *ask)
        if r.returncode != 0:
            return None, None, f'{what} failed: {(r.stderr or r.stdout or "").strip()[:200]}'
        try:
            answer = json.loads(r.stdout)
            if answer.get('errors'):
                raise TypeError
            if review is None:
                nodes, more = connection(answer['data']['repository']['pullRequest']['reviews'], after)
                found = [(node.get('id'), node['comments'], None) for node in nodes]
                keys = ('includesCreatedEdit', 'isMinimized', 'url', 'author')
                for node in nodes:
                    if isinstance(node.get('id'), str) and node['id'] and all(isinstance(node.get(k), bool) for k in keys[:2]):
                        flags[node['id']] = {k: node.get(k) for k in keys}
            else:
                more, found = None, [(review, answer['data']['node']['comments'], after)]
            pages = [(more, None)] if more is not None else []
            for of, comments, since in found:
                nodes, next_page = connection(comments, since)
                inline += filter(None, map(inline_post, nodes))
                if next_page is not None and not (isinstance(of, str) and of):
                    raise TypeError   # more inline comments, and no review id to ask for them with
                pages += [(next_page, of)] if next_page is not None else []
        except (ValueError, KeyError, TypeError, AttributeError):
            return None, None, f'{what}: GitHub gave no list of reviews and their inline comments'
        if any(cursor is False for cursor, _ in pages):
            return None, None, f'{what}: GitHub said there are more but gave no next page'
        todo += [(of, cursor) for cursor, of in pages]
    if not todo:
        return flags, inline, None
    return None, None, f'{what}: more than {REVIEW_PAGES} pages (of 100 reviews or inline comments), not all read'


def set_aside(path):
    """Moves `path` out of the way (one rename, so a file inside that cannot be deleted never blocks the name), then
    deletes it. True when `path` is free; a part that could not be deleted stays as `path`.old-* and is said."""
    if os.path.lexists(path):
        aside = f'{path}.old-{time.time_ns()}'
        try:
            os.rename(path, aside)
        except OSError as e:
            print(f'could not move {path} aside: {e}')
            return False
        remove_old(aside)
    return True


def remove_old(*paths):
    """Deletes these set-aside folders and says what it could not delete (left for a person or the next run)."""
    for p in paths:
        failed = []
        if os.path.isdir(p) and not os.path.islink(p):
            if sys.version_info >= (3, 12):
                shutil.rmtree(p, onexc=lambda f, q, e: failed.append(f'{q}: {e}'))
            else:
                shutil.rmtree(p, onerror=lambda f, q, e: failed.append(f'{q}: {e[1]}'))
        else:
            try:
                os.remove(p)
            except OSError as e:
                failed.append(f'{p}: {e}')
        if failed:
            print(f'could not delete all of {p} ({len(failed)} errors, first: {failed[0]}): left there')


def write_text(path, text):
    """Via a synced temp file and os.replace: neither a kill nor a reboot mid-write leaves half a file, and a plan
    written before the disarm is on disk before the disarm. Only this user may read or write it (0600)."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w', opener=lambda p, flags: os.open(p, flags | os.O_NOFOLLOW, 0o600)) as f:
        os.fchmod(f.fileno(), 0o600)   # a .tmp an older run left keeps its mode otherwise
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_dir(path)


def sync_dir(path):
    """The rename or removal of `path` itself on disk: a reboot must not bring back a dropped plan or lose a new one."""
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def git_problem(run=subprocess.run):
    """Why this git cannot run the queue (merge-tree --write-tree needs GIT_MIN), or None."""
    try:
        r = run(['git', 'version'], text=True, capture_output=True)
        out = r.stdout.strip()
    except OSError as e:
        out = str(e)
    m = re.match(r'git version (\d+)\.(\d+)', out)
    if m and tuple(map(int, m.groups())) >= GIT_MIN:
        return None
    return f'git {".".join(map(str, GIT_MIN))} or newer is needed (merge-tree --write-tree); found: {out[:80] or "none"}'


class Queue:
    """One repository's queue. gh, git and sleep are attributes, so tests replace them."""

    def __init__(self, repo, cfg, state, cache, lock, sleep=time.sleep):
        self.repo, self.cfg, self.state, self.cache, self.lock, self.sleep = repo, cfg, state, cache, lock, sleep
        self.remote = f'https://github.com/{repo}.git'
        self.review = lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association,
                                  cfg.lens_author_associations)

    def gh(self, *a):
        return subprocess.run(['gh', *a], text=True, capture_output=True)

    def git(self, *a):
        return subprocess.run(['git', *GIT_SAFE, *GIT_AUTH, '--git-dir', self.cache, *a], text=True, capture_output=True,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})

    # --- step 1 and 4: the queue's own git repository -------------------------------------------------------------
    def cache_problem(self):
        """Why the cache is not the one the queue made, or None. git runs there with the token in its environment, and
        its objects decide what is re-armed: only a directory of this user that no one else can enter, whose config is
        CACHE_CONFIG, that holds none of the files that make git read other objects or history (UNTRUSTED_FILES) and
        that is a bare repository, counts."""
        try:
            st = os.lstat(self.cache)
            if not stat.S_ISDIR(st.st_mode):
                return 'is not a directory'
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                return f'is not this user\'s alone (owner {st.st_uid}, mode {st.st_mode & 0o777:o})'
            with open(os.path.join(self.cache, 'config'), opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as f:
                if f.read() != CACHE_CONFIG:
                    return 'has a config the queue did not write'
            for name in UNTRUSTED_FILES:
                if os.path.lexists(os.path.join(self.cache, name)):
                    return f'holds {name}, which the queue never writes'
        except (OSError, ValueError) as e:
            return f'cannot be checked ({e})'
        if self.git('rev-parse', '--is-bare-repository').stdout.strip() != 'true':
            return 'is not a bare git repository'
        return None

    def own_cache(self):
        """The cache, made on first use (`git init --bare`; the first fetch brings the objects). One that is not the
        queue's own (cache_problem: emptied, half-deleted, someone else's, open to others, another config) is set aside
        and made again, never trusted. Every failure on the way is said and returns False (fetch then knows nothing,
        so nothing is done), never an exception."""
        if os.path.lexists(self.cache):
            problem = self.cache_problem()
            if not problem:
                # A fetch killed half-way leaves a ref lock that fails every later fetch. Only the queue uses the cache,
                # and only while it holds the lock: a lock file here is always a stale one.
                for d, _, files in os.walk(os.path.join(self.cache, 'refs', 'queue')):
                    for f in files:
                        if f.endswith('.lock'):
                            os.remove(os.path.join(d, f))
                return True
            print(f'{self.cache} {problem}: rebuilding it')
        parent, name = os.path.split(self.cache)
        os.makedirs(parent, exist_ok=True)
        remove_old(*(os.path.join(parent, f) for f in sorted(os.listdir(parent))   # CACHE.old-*, CACHE.new.old-*
                     if f.startswith(name + '.') and '.old-' in f))
        new = self.cache + '.new'
        if not (set_aside(self.cache) and set_aside(new)):   # a broken cache; one made half-way
            return False
        try:
            os.mkdir(new, 0o700)   # no one else can enter it, from the start
            os.chmod(new, 0o700)
        except OSError as e:
            print(f'could not make {new}: {e}')
            return False
        r = subprocess.run(['git', *GIT_SAFE, 'init', '-q', '--bare', '--template=', new], text=True,
                           capture_output=True)
        if r.returncode != 0:
            print(f'could not make {self.cache}: {r.stderr.strip()[:300]}')
            return False
        try:
            write_text(os.path.join(new, 'config'), CACHE_CONFIG)
            os.rename(new, self.cache)
        except OSError as e:   # something made the cache between the two steps: the next run sets it aside
            print(f'could not move the new repository to {self.cache}: {e}')
            return False
        return True

    def has(self, sha):
        return bool(SHA.fullmatch(sha or '')) and self.git('cat-file', '-e', sha + '^{commit}').returncode == 0

    def fetch(self, base, n, *shas):
        """The base branch (refs/queue/base), PR n's head and `shas` into the cache. True when every sha is there,
        False when the refs came but a sha is not on GitHub, None when nothing could be fetched (no cache, a git or
        network error: nothing is known)."""
        if not re.fullmatch(r'[\w./-]+', base or '') or not all(SHA.fullmatch(s or '') for s in shas):
            return False
        if not self.own_cache():
            return None
        r = self.git('fetch', '-q', '--no-tags', self.remote, f'+refs/heads/{base}:refs/queue/base',
                     f'+refs/pull/{n}/head:refs/queue/pr{n}')
        if r.returncode != 0:
            print(f'#{n}: git fetch failed: {r.stderr.strip()[:300]}')
            return None
        missing = [s for s in shas if not self.has(s)]
        if missing:   # a read commit no ref reaches any more (GitHub keeps it)
            r = self.git('fetch', '-q', '--no-tags', self.remote, *missing)
            if r.returncode != 0 and not GONE_RE.search(r.stderr):   # a network error is no "not on GitHub"
                print(f'#{n}: git fetch of {", ".join(s[:12] for s in missing)} failed: {r.stderr.strip()[:300]}')
                return None
        return all(self.has(s) for s in shas)

    def same_change(self, base, n, head, read):
        """Step 4's check (holds) on GitHub's commits: True, False (any other content or a git error), or None when
        nothing could be fetched (the caller keeps STATE and tries again). Only full shas count."""
        if not (SHA.fullmatch(head or '') and SHA.fullmatch(read or '')):
            return False
        if head == read:
            return True
        got = self.fetch(base, n, head, read)
        return holds(self.git, head, read) if got else got

    # --- which armed PRs wait, and why ------------------------------------------------------------------------------
    def red_checks(self, pr):
        """{name: rollup item of its newest run} of each required check whose newest run really failed. The ruleset
        takes the newest run of a name, whichever workflow ran it (required_now), and the rollup lists every run on the
        head: an older red must not outvote a newer run. CANCELLED alone is not a failure (a newer push or update
        cancels the old run), but a red that only cancelled runs followed still stands: nothing passed since it."""
        rollup = pr.get('statusCheckRollup') or []
        name_of = lambda c: c.get('name') or c.get('context')
        state_of = lambda c: (c.get('conclusion') or c.get('state') or '').upper()
        when = lambda c: c.get('startedAt') or '9999'
        newest = {}
        for c in lens.latest_checks(rollup):
            name = name_of(c)
            if name in self.cfg.required_checks and (name not in newest or when(c) >= when(newest[name])):
                newest[name] = c
        out = {}
        for name, c in newest.items():
            if state_of(c) == 'CANCELLED':
                runs = [o for o in rollup if name_of(o) == name]
                green = max((when(o) for o in runs if lens.check_state(o) == 'ok'), default='')
                if any(state_of(o) in RED_STATES and when(o) > green for o in runs):
                    out[name] = c
            elif state_of(c) in RED_STATES:
                out[name] = c
        return out

    def failing(self, pr):
        return next(iter(sorted(self.red_checks(pr))), None)

    def required_now(self, rollup):
        """lens.check_state of each required check's newest run, by name: the ruleset takes the newest run of a name,
        whichever workflow ran it."""
        newest = {}
        for c in lens.latest_checks(rollup):
            name, when = c.get('name') or c.get('context'), c.get('startedAt') or '9999'
            if name in self.cfg.required_checks and (name not in newest or when >= newest[name][0]):
                newest[name] = (when, lens.check_state(c))
        return {name: state for name, (_, state) in newest.items()}

    def workflow_runs(self, head):
        """The workflow runs on commit `head` (status, workflowName), or None when GitHub could not say (printed)."""
        if not SHA.fullmatch(head or ''):
            return None
        r = self.gh('run', 'list', '--repo', self.repo, '--commit', head, '--limit', '50', '--json',
                    'status,workflowName')
        if r.returncode != 0:
            print(f'gh run list --commit {head[:12]} failed: {r.stderr.strip()[:200]}')
            return None
        try:
            return json.loads(r.stdout)
        except ValueError:
            print(f'gh run list --commit {head[:12]} gave no JSON')
            return None

    def stuck(self, p):
        """An up-to-date armed PR that cannot merge and that nothing moves on any more: (check, what) for a required
        check whose newest run ended without passing and without a red (CANCELLED, STARTUP_FAILURE, STALE...), or that
        never reported, in both cases only while no workflow runs on the head any more (a newer run queued with no job
        yet may still report it). None: it may still merge (green or running), or GitHub could not say (printed). A
        head with no check and no workflow run at all waits: nothing tells "not started yet" from "never starts"
        (README.md, "The queue")."""
        rollup = p.get('statusCheckRollup') or []
        now = self.required_now(rollup)
        ended = sorted((name, state) for name, state in now.items() if state not in ('ok', 'running'))
        missing = sorted(self.cfg.required_checks - set(now))
        if 'running' in now.values() or not (ended or missing):
            return None
        head = (p.get('headRefOid') or '').lower()
        runs = self.workflow_runs(head)
        if runs is None:   # unknown is not "nothing runs": wait, and say it
            what = (f'required check {ended[0][0]} ended {ended[0][1]}' if ended
                    else f'required check {", ".join(missing)} has not reported')
            print(f'#{p["number"]}: {what}, and which workflows still run on {head[:12] or "its head"} is unknown: '
                  'waiting')
            return None
        if any((x.get('status') or '').lower() != 'completed' for x in runs):
            return None
        if ended:
            return ended[0][0], f'ended {ended[0][1]}'
        if not rollup and not runs:
            print(f'#{p["number"]}: no check and no workflow run on its head yet: waiting')
            return None
        return missing[0], NEVER

    def stuck_fix(self, what, base):
        if what == NEVER:   # a re-run repeats the run's own workflow file, which has no job for this check
            return (f'A re-run cannot add it: a re-run repeats the jobs of the workflow files this head was built '
                    f'with, and none of them reported it. Push again (`{self.cfg.push_command}`) or merge {base} into '
                    'it, so that the current workflows run')
        return 'Nothing re-runs it by itself: re-run the CI run (Actions, "Re-run failed jobs") or push'

    def tell(self, n, head, reason, way_out, still=None):
        """An armed PR the queue skips says why on the PR, once per head and reason. No answer from GitHub: the next
        run tries again. Told means a comment of the queue's own login (viewerDidAuthor) that starts with the mark:
        anyone can post a copy of it on a public repository. `head` is the head the reason was found on (the list):
        after a push the reason is not known for the new head, and a mark for it would silence its real notice.
        `still`: what the PR must still show when it is read for this (the list is older), else nothing is said."""
        pr = self.view(n)
        if not pr:
            print(f'#{n}: could not read it to say why it waits')
            return
        if (pr.get('headRefOid') or '').lower() != (head or '').lower():
            print(f'#{n}: its head moved from {(head or "?")[:12]} since the list: the next run tells it')
            return
        if still and not still(pr):
            print(f'#{n}: it changed since the list ({reason} no longer shows): the next run tells it what it finds')
            return
        told = f'pr_queue: {reason} on {(pr.get("headRefOid") or "?")[:12]},'
        if any(c.get('viewerDidAuthor') is True and (c.get('body') or '').startswith(told)
               for c in pr.get('comments') or []):
            return
        r = self.gh('pr', 'comment', str(n), '--repo', self.repo, '--body',
                    f'{told} so this armed PR waits and the queue passes over it. {way_out}')
        if r.returncode != 0:
            print(f'#{n}: could not comment: {r.stderr.strip()[:300]}')

    def tell_red(self, n, head, check, what='is red', fix='Fix the cause and push'):
        """tell() for a required check: once per head, check and reason."""
        also = '.'
        if check == self.cfg.head_check and what == 'is red':
            also = f', or if a new lens read or override covers this head: `{self.cfg.ready(n)}`.'
        self.tell(n, head, f'required check {check} {what}', fix + also)

    def tell_dirty(self, n, head, base):
        """tell() for an armed PR that conflicts with its base (merge state DIRTY): GitHub cannot merge it and the queue
        cannot update it, and auto-merge stays on without a word, so without this it waits unseen. Once per head: a
        push is the only way out, and a new head that still conflicts is told again. Said only while the PR still
        shows the conflict and is still armed."""
        self.tell(n, head, f'merge conflict with {base}',
                  f'GitHub cannot merge it and the queue cannot update it: merge {base} into it, resolve the conflict '
                  f'and push (`{self.cfg.push_command}`).',
                  still=lambda pr: pr.get('mergeStateStatus') == 'DIRTY' and bool(pr.get('autoMergeRequest')))

    # --- red only because the base it was tested on was red (config update_when_base_was_red) -----------------------
    def tested_base(self, c):
        """The base commit the run of rollup item `c` merged the PR with: its job's TESTED_BASE annotation (the
        tested-base action writes the merge commit's first parent), so a re-run of an old merge and a job that started
        late both name the base they really tested, never a clock. None when unknown (printed)."""
        name = c.get('name') or c.get('context')
        m = re.search(r'/actions/runs/\d+/job/(\d+)', c.get('detailsUrl') or '')
        if not m:
            print(f'{name}: no Actions job to read its tested base from')
            return None
        r = self.gh('api', f'repos/{self.repo}/check-runs/{m[1]}/annotations?per_page=100')
        try:
            notes = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            notes = None
        if not isinstance(notes, list):
            print(f'{name}: reading the annotations of job {m[1]} failed: {r.stderr.strip()[:200] or "no JSON list"}')
            return None
        found = {(a.get('message') or '').strip() for a in notes
                 if isinstance(a, dict) and a.get('title') == TESTED_BASE}
        found = {x for x in found if SHA.fullmatch(x)}
        if len(found) != 1:
            more = ' (it has 100 or more annotations: the note may be on a later page)' if len(notes) >= 100 else ''
            print(f'{name}: job {m[1]} names {len(found)} tested bases{more}')
            return None
        return found.pop()

    def commit_checks(self, expr, cache):
        """(oid, required_now of its checks) of commit `expr` (a sha or refs/heads/<base>), read once per run (`cache`);
        None when GitHub could not say (printed)."""
        if expr in cache:
            return cache[expr]
        owner, name = self.repo.split('/')
        q = ('query($owner:String!,$name:String!,$expr:String!){repository(owner:$owner,name:$name){object(expression:'
             '$expr){... on Commit{oid statusCheckRollup{contexts(first:100){pageInfo{hasNextPage} nodes{__typename '
             '... on CheckRun{name status conclusion startedAt checkSuite{workflowRun{workflow{name}}}} '
             '... on StatusContext{context state}}}}}}}}')
        r = self.gh('api', 'graphql', '-f', f'query={q}', '-f', f'owner={owner}', '-f', f'name={name}',
                    '-f', f'expr={expr}')
        out = None
        try:
            commit = json.loads(r.stdout)['data']['repository']['object'] if r.returncode == 0 else None
            contexts = (commit.get('statusCheckRollup') or {}).get('contexts') or {} if commit else {}
            if r.returncode != 0:
                print(f'reading the checks of {expr[:40]} failed: {r.stderr.strip()[:200]}')
            elif not commit or not SHA.fullmatch(commit.get('oid') or ''):
                print(f'the checks of {expr[:40]}: GitHub gave no commit')
            elif (contexts.get('pageInfo') or {}).get('hasNextPage'):
                print(f'the checks of {expr[:40]}: more than 100, not all read')
            else:
                rollup = [{**x, 'workflowName': (((x.get('checkSuite') or {}).get('workflowRun') or {}).get('workflow')
                                                 or {}).get('name') or ''} for x in contexts.get('nodes') or []]
                out = (commit['oid'], self.required_now(rollup))
        except (ValueError, KeyError, TypeError, AttributeError):
            print(f'the checks of {expr[:40]}: GitHub gave no commit')
        cache[expr] = out
        return out

    def red_from_old_base(self, p, cache):
        """A BEHIND PR whose required checks failed only because the base they were tested on was red. (None, None,
        False) when, for EVERY red required check C, the base commit its run really merged with (tested_base) has a real
        failure of C (BASE_RED) and the base branch has C green now: an update makes a new merge on that green tip.
        Otherwise (why not, check, base_red): `check` is the check that decided, for the notice (None: no one check
        did). base_red is True only when EVERY red check failed on its tested base and `check` failed on the base too
        and is still not green there, so the PR waits for its base, not for its author; one red of the PR's own, or one
        nothing proves, means its author fixes it, and `check` names that one. No state and no loop: after the update
        the next run names a base where C is green, so a red then is the PR's own and it is passed over (told); a re-run
        of the old merge names the old red base again and is updated, which it needs."""
        reds, base = self.red_checks(p), p.get('baseRefName') or ''
        if not re.fullmatch(r'[\w./-]+', base):
            return 'its base branch is unknown', None, False
        tip = self.commit_checks(f'refs/heads/{base}', cache)
        if not tip:
            return f'the checks of {base} are unknown (see above)', None, False
        waits = None
        for check, c in sorted(reds.items()):
            tested = self.tested_base(c)
            if not tested:
                return f'the base its {check} run tested is unknown (see above)', check, False
            then = self.commit_checks(tested, cache)
            if not then:
                return f'the checks of {base} commit {tested[:12]} are unknown (see above)', check, False
            was = then[1].get(check, 'not reported')
            if was not in BASE_RED:   # running, ok, cancelled, skipped, stale...: nothing proves the base was red
                return f'{check} was not red on the {base} commit {tested[:12]} its run tested ({was})', check, False
            now = tip[1].get(check, 'not reported')
            if now != 'ok' and not waits:   # keep looking: a later red of the PR's own is its author's to fix
                waits = (f'{check} failed on {base} {tested[:12]} too and is not green on {base} now ({now})', check,
                         True)
        return waits or (None, None, False)

    # --- steps 2-4 --------------------------------------------------------------------------------------------------
    def view(self, n, reviews=False):
        """PR n as gh shows it, or None. reviews: also its reviews' own flags and its inline review comments
        (review_flags, under lens.REVIEW_FLAGS and lens.REVIEW_COMMENTS), for the review rules; None when they cannot
        be read, so nothing is decided on a review or an inline comment nobody could check. Read for every PR, also
        one gh pr view lists no review for: an inline comment shows only in that read."""
        r = self.gh('pr', 'view', str(n), '--repo', self.repo, '--json', VIEW)
        try:
            pr = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            return None
        if reviews and isinstance(pr, dict):
            flags, inline, why = review_flags(self.gh, self.repo, n)
            if flags is None:
                print(f'#{n}: {why}')
                return None
            pr[lens.REVIEW_FLAGS], pr[lens.REVIEW_COMMENTS] = flags, inline
        return pr

    def new_head(self, n, old):
        for _ in range(self.cfg.wait_seconds // self.cfg.poll_seconds):
            head = ((self.view(n) or {}).get('headRefOid') or '').lower()
            if SHA.fullmatch(head) and head != old:
                return head
            self.sleep(self.cfg.poll_seconds)
        return None

    def leave_off(self, n, why, advice=''):
        """Auto-merge stays OFF; say so on the PR. Never a `read at:` or `lens:` in it: `why` (it quotes gh errors and PR
        text) is cleaned of them and of markdown; `advice`, the queue's own text from the config (a command in
        backticks, a `<sha>` placeholder), keeps its markdown unless it holds one (lens.no_lens_text). 0, or SILENT when
        the comment failed (the caller keeps STATE, so the next run says it)."""
        why = lens.no_lens_text(why) + (' ' + lens.no_lens_text(advice, keep_markdown=True) if advice else '')
        print(f'#{n}: auto-merge left OFF: {why}')
        r = self.gh('pr', 'comment', str(n), '--repo', self.repo, '--body',
                    f'pr_queue turned auto-merge OFF: {why}\nNeeds a lens read of any new commits, then '
                    f'`{self.cfg.ready(n)}`.')
        if r.returncode != 0:
            print(f'#{n}: could not comment either: {r.stderr.strip()[:300]}')
            return SILENT
        return 0

    def read_advice(self, why):
        """The way out when the review names no head, for the queue's comment."""
        if lens.NO_HEADER in why:
            return ('A comment that looks like a lens report counts only with a lens header as its first plain line, '
                    f'not in a code block ({self.cfg.lens_format_doc}). Posting it again that way does not clear '
                    f'the old one: {lens.NO_HEADER_WAY_OUT} someone who read the head posts the override '
                    f'`{self.cfg.override_prefix} <sha>: <why>`.')
        if lens.SHORT in why:
            return f'A lens read names the full 40-hex sha ({self.cfg.lens_format_doc}): re-post it with that.'
        if lens.HIDDEN in why:
            return ('A hidden lens comment reads nothing: that lens posts a new comment, or someone who read the head '
                    f'posts the override `{self.cfg.override_prefix} <sha>: <why>` (also for a lens no longer used).')
        return ''

    def arm(self, n, head):
        """Auto-merge ON for PR n, for `head` only: every try carries --match-head-commit, so GitHub refuses a head that
        moved meanwhile and nothing else is ever armed. Only the refusal CHECK_NOT_DONE_RE names (the new head's
        required checks have not started yet) is tried again: after poll_seconds, then twice that, three times that...
        while the waits together stay within wait_seconds (defaults: 5, 10, 15, 20 s). Returns the last try's result:
        any other refusal at once, and this one when GitHub still said it after the last wait (the caller then leaves
        the PR OFF with a comment). Killed in a wait: STATE is still there, and the next run's recover() arms it."""
        wait = waited = 0
        while True:
            r = self.gh('pr', 'merge', str(n), '--repo', self.repo, '--auto', f'--{self.cfg.merge_method}',
                        *(['--delete-branch'] if self.cfg.delete_branch else []), '--match-head-commit', head)
            wait += self.cfg.poll_seconds
            if r.returncode == 0 or not CHECK_NOT_DONE_RE.search(r.stderr or '') \
                    or waited + wait > self.cfg.wait_seconds:
                return r
            print(f'#{n}: GitHub refused auto-merge for {head[:12]} only because a required check of that head has not '
                  f'started yet ({r.stderr.strip()[:120]}): trying again in {wait} s')
            self.sleep(wait)
            waited += wait

    def arm_failed(self, r):
        """Why arm() failed, for the comment: gh's words, and that the queue did wait when it was the checks race."""
        why = r.stderr.strip()[:200]
        if CHECK_NOT_DONE_RE.search(r.stderr or ''):
            why += f' (GitHub still said so after the queue tried again for {self.cfg.wait_seconds} s)'
        return why

    def update(self, n, dry=False):
        """Bring PR n up to date with its base and re-arm only the change the review read (steps 1-4)."""
        cur = self.view(n, reviews=True)
        if cur is None:
            print(f'#{n}: gh cannot show it now: nothing changed, the next run tries again')
            return 1
        if cur.get('state') != 'OPEN' or cur.get('isDraft') or not cur.get('autoMergeRequest') \
                or cur.get('mergeStateStatus') != 'BEHIND':
            print(f'#{n} changed meanwhile (not open, armed and behind any more): left alone')
            return 0
        old, base = (cur.get('headRefOid') or '').lower(), cur.get('baseRefName') or ''
        reviewed, read, why = self.review.reviewed_head(cur)
        read = read if reviewed else old
        if dry:   # writes nothing, not even the cache: it does not fetch
            print(f'#{n}: would fetch {(read or "?")[:12]} into {self.cache}, turn auto-merge off, update from {base} '
                  f'and re-arm if the new head stays the change read at {(read or "?")[:12]}'
                  + ('' if read else f' ({why})'))
            return 0
        fetched = bool(read) and self.fetch(base, n, read)
        if read and fetched is None:   # nothing changed yet: an armed PR stays as it was, the next run tries again
            print(f'#{n}: could not fetch from GitHub (see above): nothing changed, the next run tries again')
            return 1
        self.write_state({'repo': self.repo, 'pr': n, 'old': old, 'base': base, 'read': read})
        code = self.update_off(n, old, base, read, fetched, why)
        if code != SILENT:
            self.drop_state()
        return code

    def update_off(self, n, old, base, read, fetched, why):
        r = self.gh('pr', 'merge', str(n), '--repo', self.repo, '--disable-auto')
        if r.returncode != 0:
            print(f'#{n}: could not turn auto-merge off, so not updating: {r.stderr.strip()[:300]}')
            cur = self.view(n)   # gh may report an error for a change GitHub applied
            if cur and cur.get('state') == 'OPEN' and cur.get('autoMergeRequest'):
                return 1   # still armed: nothing changed
            print(f'#{n}: auto-merge may be OFF now: STATE stays, the next run finishes it')
            return SILENT
        if not read:
            return self.leave_off(n, f'{why}, so it was not updated: which head the review vouched for is unclear.',
                                  self.read_advice(why))
        if not fetched:
            return self.leave_off(n, f'GitHub does not have the commit read at {read[:12]} (any more), so it was not '
                                     f'updated. Merge {base} into it yourself and push',
                                  f'(`{self.cfg.push_command}`).')
        u = self.gh('pr', 'update-branch', str(n), '--repo', self.repo)
        print(u.stdout.strip() or u.stderr.strip()[:300])
        if u.returncode != 0:
            return self.leave_off(n, f'gh pr update-branch failed: {u.stderr.strip()[:200]}')
        head = self.new_head(n, old)
        if not head:   # GitHub may show the update late: the next run's recover() checks that head
            print(f'#{n}: the head did not move from {old[:12]} within {self.cfg.wait_seconds} s: STATE stays, the '
                  'next run finishes it')
            return SILENT
        same = self.same_change(base, n, head, read)
        if same is None:
            print(f'#{n}: could not fetch {head[:12]} to check it: STATE stays, the next run finishes it')
            return SILENT
        if not same:
            return self.leave_off(n, f'the new head {head[:12]} is not exactly the change read at {read[:12]} merged '
                                     f'with {base} (a push after the lenses, or a conflict resolution?).')
        r = self.arm(n, head)
        if r.returncode != 0:
            return self.leave_off(n, f'turning auto-merge back on for {head[:12]} failed: {self.arm_failed(r)}')
        print(f'#{n}: updated to {head[:12]}, exactly the change read at {read[:12]} merged with {base}: auto-merge '
              'back on for that head only')
        return 0

    # --- STATE and a stopped run ------------------------------------------------------------------------------------
    def write_state(self, st):
        write_text(self.state, json.dumps(st))

    def drop_state(self):
        try:
            os.remove(self.state)
        except FileNotFoundError:
            return
        sync_dir(self.state)

    def tell_once(self, st, n, what):
        """recover() cannot finish PR n yet: say so once on the PR (STATE `told`; the next runs only retry). 1: STATE
        stays and no other PR this run."""
        if not st.get('told'):
            r = self.gh('pr', 'comment', str(n), '--repo', self.repo, '--body',
                        f'pr_queue was stopped half-way and may have left auto-merge OFF here; {what}. The queue '
                        'retries every run and touches no other PR until it has re-armed this one or said why not.')
            if r.returncode == 0:
                self.write_state(dict(st, told=True))
        return 1

    def recover(self):
        """A run stopped after its disarm left STATE: re-arm the PR at a head that passes step 4's check against the
        review as it is now, or leave it OFF with the comment. A PR that is armed again, merged or closed needs nothing.
        0: done, STATE removed. Otherwise STATE stays and the caller ends the run: another PR's update would overwrite
        it."""
        bad = self.state + '.bad'
        if os.path.exists(bad):
            print(f'WARNING: {bad} is an unreadable plan of an earlier run: a PR may still have auto-merge OFF. Look '
                  'at the open PRs without auto-merge, then delete it')
        mine = True
        try:
            with open(self.state, 'rb', opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as f:
                info = os.fstat(f.fileno())   # a plan someone else could have written is no plan of this queue
                mine = info.st_uid == os.getuid() and not info.st_mode & 0o022
                st = json.loads(f.read()) if mine else {}
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            st = {}
        n = st.get('pr') if isinstance(st, dict) else None
        if not isinstance(n, int) or isinstance(n, bool):   # which PR it was is lost: never quietly
            os.replace(self.state, bad)
            sync_dir(bad)
            print(f'ERROR: {"unreadable" if mine else "untrusted (another user could write it)"} {self.state}, kept as '
                  f'{bad}: a PR may have auto-merge OFF and nothing says so')
            return 1
        if st.get('repo', self.repo) != self.repo:   # a plan without `repo` is an older queue's, for this repository
            print(f'ERROR: {self.state} is the plan of a queue run for {st["repo"]}, not {self.repo}: two repositories '
                  'share one --state-file. Nothing touched; give each its own')
            return 1
        cur = self.view(n, reviews=True)
        if cur is None:
            print(f'#{n}: a stopped run left it OFF, and gh cannot show it now: trying again next run, no other PR '
                  'meanwhile')
            return self.tell_once(st, n, 'gh cannot show this PR right now')
        head, base = (cur.get('headRefOid') or '').lower(), cur.get('baseRefName') or ''
        open_off = cur.get('state') == 'OPEN' and not cur.get('autoMergeRequest')
        # The review as it is now, not the read the stopped run stored: a lens posted since then counts. No lens
        # comments: the stored read, the head that run started from.
        reviewed, read, why = self.review.reviewed_head(cur)
        read = read if reviewed else st.get('read')
        same = self.same_change(base, n, head, read) if open_off and not cur.get('isDraft') and read else False
        old = (st.get('old') or '?')[:12]
        if not open_off:
            code = 0
        elif not read:
            code = self.leave_off(n, f'a pr_queue run that was stopped half-way or could not finish turned auto-merge '
                                     f'off at {old}; {why or "it stored no read commit"}, so it was not re-armed: which '
                                     'head the review vouches for is unclear.', self.read_advice(why or ''))
        elif same is None:   # never OFF and silent: say it once on the PR, as for a gh error
            print(f'#{n}: could not fetch from GitHub to check {head[:12]}: trying again next run, no other PR meanwhile')
            return self.tell_once(st, n, f'the queue could not fetch {head[:12]} from GitHub to check it')
        elif same:
            r = self.arm(n, head)
            if r.returncode == 0:
                print(f'#{n}: an earlier run had turned auto-merge off; {head[:12]} is the change read at {read[:12]}: '
                      'auto-merge back on for that head only')
                code = 0
            else:
                code = self.leave_off(n, f'an earlier pr_queue run turned auto-merge off; turning it back on for '
                                         f'{head[:12]} (the change read at {read[:12]}) failed: {self.arm_failed(r)}')
        else:
            code = self.leave_off(n, f'a pr_queue run that was stopped half-way or could not finish turned auto-merge '
                                     f'off at {old}; the head {head[:12]} could not be shown to be the change read at '
                                     f'{read[:12]}.')
        if code != SILENT:
            self.drop_state()
        return code

    # --- one run ----------------------------------------------------------------------------------------------------
    def main(self, dry=False):
        if dry:   # reads only (gh views, STATE), so it needs no lock and never holds up a real run
            return self.dry_run()
        os.makedirs(os.path.dirname(self.lock), exist_ok=True)
        with open(self.lock, 'a') as lock:   # held until this run ends (the kernel drops it if the run is killed)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                print('another pr_queue run is going: leaving it to that one')
                return 0
            code = self.recover()
            if code:
                print('a stopped run is not finished yet: no other PR this run')
                return code
            return self.run(False)

    def dry_run(self):
        """What a real run would do, writing nothing: no lock, no STATE, no cache, no comment, no PR change. A plan in
        STATE is shown, not finished: a real run would finish it first and touch no other PR."""
        try:
            with open(self.state) as f:
                print(f'STATE {self.state}: {f.read()[:300]}\n  a real run finishes this first (recover) and touches '
                      'no other PR until it is done; the list below is what it would do after that')
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f'STATE {self.state} could not be read: {e}')
        return self.run(True)

    def run(self, dry):
        """One sweep in age order: update or wait for the first PR that can move. An UNKNOWN merge state (GitHub is
        still computing it, often for seconds after a push to the base) is listed again every poll_seconds, at most
        wait_seconds, before the PR is passed over, so a younger PR is not updated ahead of it."""
        waits = self.cfg.wait_seconds // self.cfg.poll_seconds
        while True:
            code = self.sweep(dry, waits > 0)
            if code is not None:
                return code
            waits -= 1
            self.sleep(self.cfg.poll_seconds)

    def sweep(self, dry, may_wait):
        """run()'s one look at the list; None: list again (an UNKNOWN merge state, only when may_wait)."""
        limit = self.cfg.list_limit
        r = self.gh('pr', 'list', '--repo', self.repo, '--state', 'open', '--limit', str(limit), '--json', LIST)
        try:
            prs = json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            prs = None
        if not isinstance(prs, list):
            print(f'gh pr list failed: {r.stderr.strip()[:300] or "no JSON list"}')
            return 1
        if len(prs) >= limit:
            print(f'WARNING: gh listed {len(prs)} open PRs, the list_limit: GitHub lists the newest first, so the '
                  'oldest waiting ones may be missing. Raise list_limit (at most 100) or close old PRs')
        cache = {}   # commit_checks: the base's checks read once per run
        for p in sorted(prs, key=lambda p: p.get('createdAt') or ''):
            n = p['number']
            if p.get('isDraft') or not p.get('autoMergeRequest'):
                continue
            head = p.get('headRefOid') or ''
            if p.get('mergeStateStatus') == 'DIRTY':   # before its checks: only a push ends a conflict, and a red
                base = p.get('baseRefName') or ''      # check of this head goes with the head
                base = base if re.fullmatch(r'[\w./-]+', base) else 'its base'
                print(f'skip #{n}: merge conflict with {base} (merge state DIRTY): GitHub cannot merge it and the queue '
                      'cannot update it (its author resolves it)')
                if not dry:
                    self.tell_dirty(n, head, base)
                continue
            bad = self.failing(p)
            if bad and p.get('mergeStateStatus') == 'BEHIND' and self.cfg.update_when_base_was_red:
                base = p.get('baseRefName') or 'its base'
                why, check, base_red = self.red_from_old_base(p, cache)
                if why is None:
                    reds = ', '.join(sorted(self.red_checks(p)))
                    print(f'update #{n} {(p.get("title") or "")[:70]}: required check {reds} failed on a {base} '
                          f'commit where it failed too, and it is green on {base} now')
                    return self.update(n, dry)
                bad = check or bad   # the notice names the check that decided, and is keyed by it
                print(f'skip #{n}: required check {bad} failed and an update would not fix it yet: {why}')
                if base_red:
                    if not dry:
                        self.tell_red(n, head, bad, f'failed as on {base}',
                                      f'{base} failed {bad} too, so this is not this PR\'s fault: the queue '
                                      f'updates it by itself once {bad} is green on {base}')
                    continue
            elif bad:
                print(f'skip #{n}: required check {bad} failed (its author fixes it)')
            if bad:
                if not dry:
                    self.tell_red(n, head, bad)
                continue
            if p.get('mergeStateStatus') != 'BEHIND':
                if p.get('mergeStateStatus') == 'UNKNOWN' and may_wait:
                    print(f'#{n}: merge state UNKNOWN (GitHub is still computing it): listing again in '
                          f'{self.cfg.poll_seconds} s')
                    return None
                if p.get('mergeStateStatus') not in WAITS_FOR:
                    print(f'skip #{n}: merge state {p.get("mergeStateStatus")}, not one the queue updates or waits for')
                    continue
                why = self.stuck(p)
                if why:
                    print(f'skip #{n}: up to date, but required check {why[0]} {why[1]} and nothing re-runs it: it '
                          'cannot merge, so it does not hold the queue')
                    if not dry:
                        self.tell_red(n, head, why[0], why[1],
                                      self.stuck_fix(why[1], p.get('baseRefName') or 'its base'))
                    continue
                print(f'#{n} is up to date and waiting for its checks: queue is moving')
                return 0
            print(f'update #{n} {(p.get("title") or "")[:70]}')
            return self.update(n, dry)
        print('queue empty')
        return 0


def cli(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='pr_queue.py', description='Merge queue: one waiting PR per run (README.md).')
    p.add_argument('--repo', required=True, help='owner/name')
    p.add_argument('--config', required=True, help='the JSON config (README.md, "Config")')
    p.add_argument('--state-file', required=True, help='absolute; the plan of a run that turned auto-merge off')
    p.add_argument('--cache', required=True, help='absolute; the queue\'s own bare git repository')
    p.add_argument('--lock-file', help='absolute; default: the state file + .lock')
    p.add_argument('--dry-run', action='store_true', help='say what a run would do; write nothing')
    a = p.parse_args(argv)
    lock = a.lock_file or a.state_file + '.lock'
    problems = []
    if env.get('RUNNER_ENVIRONMENT') == 'github-hosted':
        problems.append('this is a GitHub-hosted runner: its disk goes with the job, and with it the plan of a run that '
                        'turned auto-merge off (--state-file). Run the queue on a self-hosted runner')
    if not REPO_RE.fullmatch(a.repo):
        problems.append(f'--repo {a.repo!r} is not owner/name')
    work = os.path.realpath(env['GITHUB_WORKSPACE']) if env.get('GITHUB_WORKSPACE') else None
    for flag, path in (('--state-file', a.state_file), ('--cache', a.cache), ('--lock-file', lock)):
        if not os.path.isabs(path):
            problems.append(f'{flag} {path!r} is not an absolute path')
        elif work and (os.path.realpath(path) + os.sep).startswith(work + os.sep):
            problems.append(f'{flag} {path} is inside the job\'s workspace, which the next checkout empties')
    try:
        cfg = config.load(a.config)
    except config.ConfigError as e:
        problems.append(str(e))
    problems += filter(None, [git_problem()])
    if problems:
        print('pr_queue refused to start and touched nothing:\n  ' + '\n  '.join(problems))
        return REFUSED
    return Queue(a.repo, cfg, a.state_file, a.cache, lock).main(a.dry_run)


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)   # a run stopped half-way still shows what it did
    sys.exit(cli())
