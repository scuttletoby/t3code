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
  2. The PR is read right before the disarm: off by then (someone else's switch) or closed, it is left as it is.
     The plan goes into STATE (--state-file), unsure until GitHub answers the disarm with success, then auto-merge
     OFF; if that fails, nothing else happens: still armed, nothing changed; OFF, the queue cannot tell whether a
     person turned it off, so it stays OFF with a comment and an unsure plan is never re-armed; unclear whether it is
     OFF, STATE stays for the next run. What GitHub answers for turning off an auto-merge that is already off is not
     documented: if success, a person's switch-off at the moment of the call counts as the queue's own (README.md,
     "no compare-and-set for auto-merge"). R unknown or not on GitHub: the PR stays OFF,
     is not updated and gets a comment, so one such PR cannot stall the queue. A git fetch error changes nothing: the
     PR stays as it was and the next run tries again.
  3. The PR is read again: armed again (or closed) since the disarm, it is not updated and STATE stays, so the next
     run decides from what GitHub shows then (a person's arm, or GitHub showing the disarm late). Then
     `gh pr update-branch`, and wait until the head moves (at most wait_seconds).
  4. Auto-merge back ON with --match-head-commit H2 only if H2 holds exactly R's change on top of the base (holds()).
     GitHub can refuse that only because the new head's required checks have not started yet: then, and only then,
     the same arm is tried again for at most wait_seconds (arm). Anything else (a push after the lenses, a conflict
     resolution, any gh or git error): it stays OFF with a comment that it needs a lens read and the ready command.
     No new head in time: STATE stays, the next run checks it.
A PR already up to date never reaches these steps: the project's head_check (required) covers its head.
Stopped half-way, the next run finishes that PR first from STATE (recover), against the review and the base as GitHub
shows them then, and touches no other PR until it has re-armed it or said on it why not: a PR is never OFF and silent.
Two tokens (cli): every read (the PR list with its checks, check runs, workflow runs, reviews, git fetch) uses the
run's GITHUB_TOKEN (PR_QUEUE_READ_TOKEN), read-only by the job's permissions (README.md, "Use it"); a personal access
token cannot read check runs at all. The caller's token (PR_QUEUE_WRITE_TOKEN) is used only where it must be (gh_write):
the branch update (one by GITHUB_TOKEN starts no workflow), auto-merge on (the merge it makes must start the base's
push workflows) and off, the queue's comments, and the one read that asks whether the queue already said something
(viewerDidAuthor means the token's own login).
Files the queue trusts are this user's alone: STATE is written 0600 and read only if no one else could have written it;
the cache is used only as the queue made it (own_cache), and git runs there with no hooks and no replace refs.
Exit codes: 0 done; 1 a step failed, the next run tries again; 2 refused to start, nothing touched; 3 a PR may be OFF
and no comment says so yet (STATE stays, the next run finishes it).
    PR_QUEUE_READ_TOKEN=... PR_QUEUE_WRITE_TOKEN=... \
    pr_queue.py --repo OWNER/NAME --config FILE --state-file ABS --cache ABS [--lock-file ABS] [--dry-run]
"""
import argparse, fcntl, hashlib, json, os, re, secrets, shutil, signal, stat, subprocess, sys, tempfile, threading, \
    time, urllib.parse, zlib

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
GATE_NOT_YET = 'not posted by the gate App yet'
TESTED_BASE = 'pr_queue tested base'   # the annotation title the tested-base action writes (tested_base.py)
BASE_RED = ('FAILURE', 'ERROR')   # "the base was red": a real failure. CANCELLED, STALE, STARTUP_FAILURE, TIMED_OUT,
# SKIPPED, not reported or running say nothing about the base's code (a busy runner times out or cancels base runs)
UPDATES = 3   # holds() follows at most this many queue updates in a row
GIT_MIN = (2, 43)   # merge-tree --write-tree (2.38) with GIT_ATTR_SOURCE (2.41; merge-tree crashed with it until 2.43)
REFUSED, SILENT = 2, 3
READ_TOKEN, WRITE_TOKEN = 'PR_QUEUE_READ_TOKEN', 'PR_QUEUE_WRITE_TOKEN'
READ_LOGIN = f"{READ_TOKEN} (the run's read token: contents read)"
# Never passed on: each gh and git gets exactly one token, as GH_TOKEN (gh_env).
TOKEN_VARS = ('GH_TOKEN', 'GITHUB_TOKEN', 'GH_ENTERPRISE_TOKEN', 'GITHUB_ENTERPRISE_TOKEN', READ_TOKEN, WRITE_TOKEN)
READ_PERMISSIONS = 'actions, checks, contents, issues, pull-requests and statuses: read'
# GitHub's answer to a token without the permission: "Resource not accessible by integration" (GITHUB_TOKEN, an App)
# or "... by personal access token".
NO_PERMISSION_RE = re.compile(r'Resource not accessible by', re.I)
# git gets the login from gh (GH_TOKEN: the run's read token), never from a credential helper of the machine's git config.
GIT_AUTH = ('-c', 'credential.https://github.com.helper=',
            '-c', 'credential.https://github.com.helper=!gh auth git-credential')
# Every git call, whatever the repository's files say: no hooks, no fsmonitor command, no replace refs, and no
# commit-graph (a planted objects/info/commit-graph(s) could change the parents git reports; git rebuilds nothing from it).
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.commitGraph=false',
            '-c', 'core.attributesFile=/dev/null', '--no-replace-objects')
# ... and no global or system config or attributes: on a self-hosted runner an earlier job may have written
# ~/.gitconfig or ~/.config/git/attributes. GIT_SAFE and GIT_ENV are the same as ../git-clean/git_clean.py's
# (git_clean.test.py pins them to its).
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1', 'GIT_ATTR_NOSYSTEM': '1'}
# The tree git reads `.gitattributes` from, for every call git_env makes: the empty tree, so no attribute file of any
# commit counts. Without it a bare repository's HEAD (git 2.43 to 2.45 read `HEAD:.gitattributes` there) or an
# `attr.tree` is one: `* merge=union` there makes merge-tree's conflict clean. Git knows the empty tree in every
# repository, even one with no objects. GIT_ATTR_SOURCE is read from git 2.41 (an older git ignores it without a word,
# so git_problem refuses it: GIT_MIN). Kept apart from GIT_ENV, which other actions share (git_clean.test.py).
EMPTY_TREE = '4b825dc642cb6eb9a060e54bf8d69288fbee4904'
GIT_ATTRS = {'GIT_ATTR_SOURCE': EMPTY_TREE}
# What a new repository is made with, whatever the job's environment or git's own default says (git 3.0 plans SHA-256
# and reftable): the queue's cache and the gate's repository hold SHA-1 objects and files refs, as GitHub's commits,
# EMPTY_TREE and CACHE_CONFIG (format 0, no extensions) are. git reads GIT_DEFAULT_REF_FORMAT from 2.45 (an older one
# makes files refs anyway); init_bare refuses a repository a git made otherwise.
GIT_FORMATS = {'GIT_DEFAULT_HASH': 'sha1', 'GIT_DEFAULT_REF_FORMAT': 'files'}
# One language for git's messages: the queue and the gate match them (GONE_RE, NO_LOGIN_RE) and read them as UTF-8.
GIT_LANG = {'LC_ALL': 'C'}
# Never passed on to git (git_env): variables of the job that point git at another repository, object store, index,
# config, attribute tree (GIT_ATTR_SOURCE: git_env sets its own, GIT_ATTRS) or pathspec rule than the call names, or
# run another diff program. The job's environment is the owner's, but each git call says alone
# what it reads.
GIT_DROP = ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_COMMON_DIR', 'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
            'GIT_INDEX_FILE', 'GIT_NAMESPACE', 'GIT_CONFIG', 'GIT_CONFIG_PARAMETERS', 'GIT_CONFIG_COUNT',
            'GIT_REPLACE_REF_BASE', 'GIT_GRAFT_FILE', 'GIT_SHALLOW_FILE', 'GIT_QUARANTINE_PATH', 'GIT_PREFIX',
            'GIT_IMPLICIT_WORK_TREE', 'GIT_INTERNAL_SUPER_PREFIX', 'GIT_LITERAL_PATHSPECS', 'GIT_GLOB_PATHSPECS',
            'GIT_NOGLOB_PATHSPECS', 'GIT_ICASE_PATHSPECS', 'GIT_ATTR_SOURCE', 'GIT_EXTERNAL_DIFF')
GIT_DROP_PREFIXES = ('GIT_CONFIG_KEY_', 'GIT_CONFIG_VALUE_')
# The programs git runs and the transports it may use, as the job's environment chooses them. Dropped by git_env from
# a call that reaches no remote, and (all but GIT_EXEC_PATH, where git finds its own https helper and index-pack) from
# a fetch from an https URL: its login is GIT_AUTH's or none, and ssh, the git:// proxy and a protocol list are never
# needed there. A fetch from another remote (a checkout's origin, an owner's ssh URL) keeps them: it may need them.
# Only a call that fetches names its remote: the cache's and the gate's local calls (merge-tree, cat-file, rev-parse)
# run none of them, GIT_EXEC_PATH included.
GIT_RUN = ('GIT_EXEC_PATH', 'GIT_SSH', 'GIT_SSH_COMMAND', 'GIT_PROXY_COMMAND', 'GIT_ASKPASS', 'SSH_ASKPASS',
           'GIT_ALLOW_PROTOCOL')
# Traces: they write an https fetch's headers (the login) and its talk with GitHub to stderr or a file, and the gate
# puts git's stderr into a public summary (a merge-tree's too). Dropped from every call but a fetch from another
# remote, which carries no token of ours (remote_problem refuses plain http, where GitHub's redirect would ask for it).
GIT_TRACE_PREFIXES = ('GIT_TRACE', 'GIT_CURL_VERBOSE')
GIT_READ_SECONDS = 60   # each git call of the config read
# The cache's HEAD, as the queue writes it (what `git init --template=` writes): a branch that is never made, so HEAD
# names no commit and no tree. A cache with any other HEAD (a ref or sha an earlier job planted) is rebuilt.
CACHE_HEAD = 'ref: refs/heads/master\n'
# git's words for a fetch that needed a login it did not have (none given, or one GitHub refused)
NO_LOGIN_RE = re.compile(r'could not read (Username|Password)|terminal prompts disabled|Authentication failed', re.I)
# git's words for a fetch the network failed (curl's errors, a transfer cut off). Any other failed fetch may be the
# cache's fault (an object git cannot read under a name the fetch needs), so the cache is checked (fetch_checked).
NETWORK_RE = re.compile(r'unable to access|RPC failed|Could not resolve host', re.I)
# git fsck's message ids for an object whose hash is right but whose content is in an old or odd format: a commit
# with time zone +99999, a zero-padded date or a missing space; a tree with entries out of order or twice; a
# .gitmodules or .gitattributes git would not check out. git fetch takes them (transfer.fsckObjects is off) and old
# imports hold them, but plain git fsck calls them errors: a cache with such history would fail every check and be
# downloaded again on every run. objects_problem ignores them. What the check is for stays an error, since none of
# it is a message id: an object that does not hash to its name, a missing or unreadable object, a broken link, a
# corrupt pack or index. So do the ids for a commit git cannot parse (bad parent or tree line, no tree), an object it
# could not read, and the tags the queue never fetches (--no-tags). Each id is one git 2.43 (GIT_MIN) knows: git
# refuses to run with an unknown one.
FSCK_FORMAT_IDS = ('badDate', 'badDateOverflow', 'badEmail', 'badName', 'badTimezone', 'missingAuthor',
                   'missingCommitter', 'missingEmail', 'missingNameBeforeEmail', 'missingSpaceBeforeDate',
                   'missingSpaceBeforeEmail', 'multipleAuthors', 'zeroPaddedDate', 'duplicateEntries', 'treeNotSorted',
                   'gitmodulesBlob', 'gitmodulesLarge', 'gitmodulesName', 'gitmodulesPath', 'gitmodulesSymlink',
                   'gitmodulesUpdate', 'gitmodulesUrl', 'gitattributesBlob', 'gitattributesLarge',
                   'gitattributesLineLength')
FSCK_IGNORE = tuple(a for i in FSCK_FORMAT_IDS for a in ('-c', f'fsck.{i}=ignore'))
# A fetch's automatic gc runs before the fetch returns, never detached: a repack in the background deletes packs and
# loose objects while the next git fsck reads them (a false "broken", and a download from scratch).
NO_DETACH = ('-c', 'gc.autoDetach=false', '-c', 'maintenance.autoDetach=false')
# STATE + this, next to the queue's plan and never in the cache: what the cache's check said right after a download
# from scratch that it still failed (git fsck's lines, or the merge check's one line). The same words on a later run
# are GitHub's own history, not an earlier job's object: that run does not download it all again. An older queue kept
# it in the cache (`queue-fsck-known`), where whoever can write the cache could plant one: that file is never read.
FSCK_KNOWN = '.fsck-known'
# Beside STATE: an unreadable plan kept aside (STATE.bad, STATE.bad-<ns>: keep_bad), and which of those were told once
# through notify_command (report_bad).
BAD, TOLD_BAD = '.bad', '.told-bad'
# CACHE + this: the merge check's own repository (Queue.holds_checked), made new for each check and deleted after it.
MERGE_COPY = '.merge'
# How long the merge check's object reader (Copy: one `git cat-file --batch`) may owe one answer, the line that names
# an object or its next 1 MiB, before it is stopped and the run knows nothing (STATE stays). An answer takes
# milliseconds; this is for a reader that hangs. The run's other git calls have no limit but the job's.
CAT_SECONDS = 120
# A merge check follows at most this many merge bases (bases_of): a history with more is not taken as the change read.
MERGE_BASES = 8
# A commit as git reads it: the tree line, then the parent lines right after it.
COMMIT_RE = re.compile(rb'tree ([0-9a-f]{40})\n((?:parent [0-9a-f]{40}\n)*)')
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
# A quoted check name may hold dots (`"test (3.12)"`); outside quotes a dot ends the sentence.
# The repository setting "Automatically delete head branches" (cli reads it once per run): the queue never deletes a
# branch itself (arm), so with the setting off merged branches stay. GraphQL, not REST: GET /repos leaves the setting
# out for a token without push access, and the run's GITHUB_TOKEN reads only.
DELETE_ON_MERGE_QUERY = 'query($owner:String!,$name:String!){repository(owner:$owner,name:$name){deleteBranchOnMerge}}'
CHECK_NOT_DONE_RE = re.compile(r'required status checks?\b(?:"[^"\n]*"|[^."\n])*\b(?:is|are) '
                               r'(?:queued|in progress|expected|pending)\b', re.I)


def merge_tree(git, p, side):
    """git's merge of the commits `p` and `side`: exit 0 and the tree's name when clean."""
    return git('-c', 'merge.directoryRenames=true', 'merge-tree', '--write-tree', p, side)


def holds(git, head, read, depth=UPDATES, base='refs/queue/base', merge=None):
    """Step 4's rule on commits `git` (a callable running git in one repository) already has: `head` is `read`, or a
    parent P of `head` is on `base` and head's tree is `git merge-tree --write-tree P X` byte for byte, where X is
    `read` or head's other parent when that one holds this itself (an earlier queue update: GitHub merges the new base
    into the head the queue already proved, which can be clean where a direct merge of `read` conflicts). A tree is
    the same or not: a hunk moved to another function, a conflict resolution or a later push is not the same.

    git's output is decoded strictly by the callers. A conflict lists the conflicted files by name, and a name need
    not be UTF-8: that is no same change (a clean merge prints only the tree id), said once, never a crash.

    `merge`: what runs each merge-tree, as merge_tree(git, p, side) does (the queue's copies the objects that merge
    reads first: Queue.holds_checked)."""
    if head == read:
        return True
    try:
        return _holds(git, head, read, depth, base, merge or (lambda p, side: merge_tree(git, p, side)))
    except UnicodeDecodeError as e:
        print(f'::warning::pr-queue: git printed bytes that are not UTF-8 while checking {head[:12]} against the read '
              f'{read[:12]} (a conflict naming a file whose name is not UTF-8: {e.reason}, byte {e.start}): the head '
              'is not taken as the change read')
        return False


def _holds(git, head, read, depth, base, merge):
    if head == read:
        return True
    tree = git('rev-parse', '--verify', '-q', head + '^{tree}').stdout.strip()
    parents = git('rev-list', '--parents', '-n', '1', head).stdout.split()[1:]
    for p in parents:
        if git('merge-base', '--is-ancestor', p, base).returncode != 0:
            continue
        sides = [read] + [h for h in parents if h != p and depth > 1
                          and _holds(git, h, read, depth - 1, base, merge)]
        for side in dict.fromkeys(sides):
            m = merge(p, side)
            if m.returncode == 0 and tree and m.stdout.split()[:1] == [tree]:
                return True
    return False


class Changed(Exception):
    """An object the merge check needs is not in the cache as its name says: missing, unreadable or other content."""


class Copy:
    """The objects of one merge check, copied out of the cache into a repository of their own (`to`, new and empty)
    with every hash checked on the way. git checks no hash of a tree or blob merge-tree reads nor of a commit a walk
    reads, and the cache's git fsck ends before the merge check (in an update, minutes before): whoever can write the
    cache could change an object in between. One `git cat-file --batch` (`argv`, `env`) hands each object out the way
    any git call finds it; this process hashes what it got (`<type> <size>\\0<content>`, SHA-1) and writes it to `to`
    as a loose object only when that is its name. The merge check then runs in `to`: it reads only objects hashed
    here, and nothing written to the cache after an object was copied can reach it. The trees git itself makes there
    (of two or more merge bases) are written by git into `to`, where nothing was planted under their names.

    Changed: an object is missing, cannot be read or does not hash to its name. OSError: `to` cannot be written, or
    (TimeoutError) cat-file gave no answer for CAT_SECONDS, which says nothing about the cache's objects."""

    def __init__(self, argv, env, to):
        self.to, self.seen, self.folders = to, set(), set()
        self.tree_of, self.trees, self.whole, self.walked = {}, {}, set(), set()   # commit: its tree; tree: entries
        self.cat = subprocess.Popen(argv, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL)
        self.asked, self.stalled, self.done = None, False, threading.Event()   # asked: since when said() waits
        self.watcher = threading.Thread(target=self.watch, daemon=True)
        self.watcher.start()

    def watch(self):
        """Stops a cat-file that owes one answer (said) for more than CAT_SECONDS: git opens a loose object with a
        plain open(), so a FIFO planted under an object's name would hold it, and with it the run and the queue's
        lock, until the job's time limit. The reads themselves are not touched: a stopped cat-file can only give
        less than was asked for, never other bytes. Between two answers (while git merges) nothing is counted."""
        while not self.done.wait(min(1.0, CAT_SECONDS / 4)):
            asked = self.asked
            if asked is not None and time.monotonic() - asked > CAT_SECONDS:
                self.stalled = True
                self.cat.kill()
                return

    def close(self):
        """cat-file stopped and reaped (a run that is killed outright closes its stdin, which ends it too)."""
        self.done.set()
        self.watcher.join()
        self.cat.kill()
        for pipe in (self.cat.stdin, self.cat.stdout):
            try:
                pipe.close()
            except OSError:
                pass
        self.cat.wait()

    def said(self, name, n=None):
        """cat-file's next line (after asking for `name`), or its next `n` bytes; Changed when it gives less, and
        TimeoutError when it gave less because it was stopped after CAT_SECONDS without an answer (watch)."""
        self.asked = time.monotonic()
        try:
            if n is None:
                self.cat.stdin.write(name.encode() + b'\n')
                self.cat.stdin.flush()
                got = self.cat.stdout.readline()
            else:
                got = self.cat.stdout.read(n)
        except (OSError, ValueError) as e:
            got = e
        finally:
            self.asked = None
        if self.stalled:   # whatever came before it was stopped: never taken, and no word on the object
            raise TimeoutError(f'git cat-file gave no answer for {CAT_SECONDS:g} s (object {name}) and was stopped')
        if isinstance(got, Exception):
            raise Changed(f'object {name} cannot be read (git cat-file: {got})') from None
        if n is not None and len(got) != n:
            raise Changed(f'object {name} cannot be read (git cat-file stopped)')
        return got

    def take(self, name, kind):
        """Object `name`, which must be a `kind`, copied if it was not yet: its content when it is a commit or a
        tree copied now, else None."""
        if name in self.seen:
            return None
        # No test pins the name echo, the type or the newline after the content below: a wrong name or type fails
        # the hash (the type is in the hashed header), a lost newline fails the next header. Kept as a second net.
        line = self.said(name).split()
        if len(line) != 3 or line[0] != name.encode() or not line[2].isdigit():
            raise Changed(f'object {name} is missing or cannot be read')
        if line[1] != kind.encode():
            raise Changed(f'object {name} is a {line[1][:20].decode("ascii", "replace")}, not a {kind}')
        left = int(line[2])
        header = b'%s %d\0' % (line[1], left)
        hashed, packed, kept = hashlib.sha1(header), zlib.compressobj(1), []
        folder = os.path.join(self.to, 'objects', name[:2])
        if folder not in self.folders:
            os.makedirs(folder, exist_ok=True)
            self.folders.add(folder)
        path = os.path.join(folder, name[2:])
        with open(path + '.new', 'wb') as out:   # git may have written this name itself (a merge's result)
            out.write(packed.compress(header))
            while left:
                part = self.said(name, min(left, 1 << 20))
                left -= len(part)
                hashed.update(part)
                out.write(packed.compress(part))
                if kind != 'blob':
                    kept.append(part)
            out.write(packed.flush())
        if self.said(name, 1) != b'\n':
            raise Changed(f'object {name} cannot be read (git cat-file said something else)')
        if hashed.hexdigest() != name:
            raise Changed(f'object {name} does not hash to its name')
        os.replace(path + '.new', path)
        self.seen.add(name)
        return None if kind == 'blob' else b''.join(kept)

    def take_commits(self, *tips):
        """Every commit these reach. A history in which each commit hashes to its name is the one GitHub sent: every
        walk in `to` (rev-list, merge-base, merge-tree's merge bases) then reads real parents."""
        todo = list(tips)
        while todo:
            name = todo.pop()
            body = self.take(name, 'commit')
            if body is None:
                if name not in self.tree_of:
                    raise Changed(f'object {name} is not a commit')
                continue
            links = COMMIT_RE.match(body)
            if not links:
                raise Changed(f'object {name} is not a commit git can read')
            self.tree_of[name] = links.group(1).decode()
            todo += [line.split()[1].decode() for line in links.group(2).splitlines()]

    def tree(self, name):
        """{entry name: [(mode, object name), ...]} of tree `name` (one entry per name, but for an old tree that
        holds a name twice), copied on first use."""
        if name not in self.trees:
            body = self.take(name, 'tree')
            if body is None:
                raise Changed(f'object {name} is not a tree')
            entries, at = {}, 0
            try:
                while at < len(body):
                    space = body.index(b' ', at)
                    end = body.index(b'\0', space)
                    oid = body[end + 1:end + 21]
                    if len(oid) != 20:
                        raise ValueError
                    entries.setdefault(body[space + 1:end], []).append((int(body[at:space], 8), oid.hex()))
                    at = end + 21
            except ValueError:
                raise Changed(f'object {name} is not a tree git can read') from None
            self.trees[name] = entries
        return self.trees[name]

    def take_entry(self, mode, name):
        """One tree entry's object; True for a tree. A submodule's commit (mode 160000) is another repository's:
        the cache never holds it and no merge reads it."""
        if stat.S_IFMT(mode) == 0o160000:
            return False
        if stat.S_ISDIR(mode):
            self.tree(name)
            return True
        self.take(name, 'blob')
        return False

    def take_whole(self, root):
        """Tree `root` and everything in it."""
        todo = [root]
        while todo:
            name = todo.pop()
            if name not in self.whole:
                self.whole.add(name)
                todo += [oid for entry in self.tree(name).values() for mode, oid in entry if self.take_entry(mode, oid)]

    def take_merged(self, roots):
        """What a merge of the commits with these root trees (the two sides and every merge base) can read: the
        root trees, and below them every entry that is not the same object with the same mode in all of them, with
        all that is under such an entry in each. An entry that is the same in all of them git takes as it is, without
        reading it or anything under it (in a merge of merge bases too: the tree that makes has the same entry
        there), and a rename is found only among paths that differ."""
        todo = [tuple(roots)]
        while todo:
            at = todo.pop()
            if at in self.walked:
                continue
            self.walked.add(at)
            listed = [self.tree(t) if t else {} for t in at]
            for name in set().union(*listed):
                here = [entries.get(name) for entries in listed]
                if all(h == here[0] for h in here):
                    continue
                below = []
                for h in here:
                    trees = [oid for mode, oid in h or () if self.take_entry(mode, oid)]
                    if len(h or ()) > 1:   # a name held twice: all of each, nothing left out
                        for oid in trees:
                            self.take_whole(oid)
                    below.append(trees[0] if len(trees) == 1 and len(h) == 1 else None)
                if any(below):
                    todo.append(tuple(below))


def bases_of(git, p, side):
    """The commits whose trees `git merge-tree p side` reads besides the two: their merge bases. Two or more of them
    git first merges into one, pair by pair, and each of those merges reads its own merge bases, which are among the
    merge bases of the pairs: so those are added too, until none is new. None when there are more than MERGE_BASES."""
    def pair(a, b):
        return [c for c in git('merge-base', '--all', a, b).stdout.split() if SHA.fullmatch(c)]
    found, done = list(dict.fromkeys(pair(p, side))), set()
    while len(found) > 1:
        pairs = [(a, b) for i, a in enumerate(found) for b in found[i + 1:] if (a, b) not in done]
        if not pairs:
            break
        for a, b in pairs:
            done.add((a, b))
            found += [c for c in pair(a, b) if c not in found]
            if len(found) > MERGE_BASES:
                return None
    return found


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


def read_own(path):
    """The text of a record the queue wrote, or None: only a regular file of this user that no one else can write,
    never a link (as STATE). Missing, unreadable or anyone else's: None."""
    try:
        with open(path, 'rb', opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW | os.O_NONBLOCK)) as f:
            info = os.fstat(f.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
                return None
            return f.read().decode('utf-8', 'replace')
    except OSError:
        return None


def sync_dir(path):
    """The rename or removal of `path` itself on disk: a reboot must not bring back a dropped plan or lose a new one."""
    fd = os.open(os.path.dirname(path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def no_token_env():
    """This process's environment without any token: for a local git that needs no login."""
    return {k: v for k, v in os.environ.items() if k not in TOKEN_VARS}


def notify_env():
    """The environment notify_command runs with: the queue's, without any token (TOKEN_VARS, and every name with TOKEN
    in it, such as the runner's ACTIONS_RUNTIME_TOKEN): the command is the project's, and the write token can merge."""
    return {k: v for k, v in os.environ.items() if k not in TOKEN_VARS and 'TOKEN' not in k.upper()}


def repo_page(link, page):
    """Whether `link` (GitHub's text: a status targetUrl can be anything) is a page under `page`
    (https://github.com/<repo>/, any case) and stays there in a browser: printable ASCII only (no space, control
    character or NUL, which would also stop the command from starting), no backslash (browsers read it as /), and no
    path segment that is . or .. once percent-decoded, nor an encoded / or \\ (%2f, %5c)."""
    if not (isinstance(link, str) and re.fullmatch(r'[!-~]+', link) and link.lower().startswith(page.lower())):
        return False
    path = re.split(r'[?#]', link[len(page):], maxsplit=1)[0]
    if '\\' in path or re.search(r'%(2f|5c)', path, re.I):
        return False
    return not any(urllib.parse.unquote(seg) in ('.', '..') for seg in path.split('/'))


NOTIFY_OUTPUT = 4000   # characters of the command's output the run log shows
NOTIFY_BUDGET = 300    # seconds all notify_command runs of one run may take together: the job stops at 20 minutes


def notify_tree(root):
    """The processes to stop with a notify_command (pid `root`, not yet waited for) that ran too long: its session,
    which holds a child that made a process group of its own (coreutils timeout does), and every process still linked
    to one of those by its parent, which holds a child that made a session of its own (setsid) while its parent runs.
    Read from /proc; only `root` where there is none (stop_notify then kills root's process group as well). A child whose parent already ended (a daemon) is beyond it: the
    runner's end-of-job cleanup stops that one (README.md, "Notify\")."""
    procs = {}
    try:
        names = os.listdir('/proc')
    except OSError:
        return {root}
    for d in names:
        if not d.isdigit():
            continue
        try:
            with open(f'/proc/{d}/stat', 'rb') as f:
                stat_line = f.read().decode('ascii', 'replace')
            fields = stat_line[stat_line.rindex(')') + 2:].split()   # state ppid pgrp session ...
            procs[int(d)] = (int(fields[1]), int(fields[3]))
        except (OSError, ValueError, IndexError):   # it ended while read
            continue
    tree = {root} | {pid for pid, (_, sid) in procs.items() if sid == root}
    while True:
        more = {pid for pid, (ppid, _) in procs.items() if ppid in tree} - tree
        if not more:
            return tree
        tree |= more


def stop_notify(root):
    """Kill notify_tree(root): stop each process first, then kill them all, so that none forks or moves away (a child
    whose parent is killed first leaves the tree) between the read and the kill. Then root's process group goes too, in
    one kill: without /proc that is all there is to find, and never less than the group. Safe: root leads its own session
    (start_new_session), so its group is never the queue's, and its pid, not yet waited for, cannot be reused."""
    stopped = set()
    for _ in range(20):   # a process that forks on every stop cannot keep it going forever
        new = notify_tree(root) - stopped
        if not new:
            break
        for pid in new:
            try:
                os.kill(pid, signal.SIGSTOP)
            except OSError:   # it ended, or is not the queue user's
                pass
        stopped |= new
    for pid in stopped:
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        os.killpg(root, signal.SIGKILL)
    except OSError:   # the group has no process left
        pass


def git_env(env, remote=None):
    """`env` for a git call: without GIT_DROP, with GIT_ENV, GIT_ATTRS, GIT_FORMATS, GIT_LANG and no prompt. `remote`:
    None for a call that reaches no remote, else the URL or remote name it fetches from, which decides what of GIT_RUN
    it keeps. Only a fetch from a remote that is not https keeps the traces (GIT_TRACE_PREFIXES). Tokens are the
    caller's to drop."""
    drop, prefixes = set(GIT_DROP), GIT_DROP_PREFIXES
    if remote is None:
        drop.update(GIT_RUN)
        prefixes += GIT_TRACE_PREFIXES
    elif remote.lower().startswith('https://'):   # git reads the scheme in any case: HTTPS:// is https too
        drop.update(set(GIT_RUN) - {'GIT_EXEC_PATH'})
        prefixes += GIT_TRACE_PREFIXES
    return {**{k: v for k, v in env.items() if k not in drop and not k.startswith(prefixes)},
            'GIT_TERMINAL_PROMPT': '0', **GIT_LANG, **GIT_ENV, **GIT_ATTRS, **GIT_FORMATS}


def remote_problem(remote):
    """Why a fetch that carries the run's token (GIT_AUTH: gh's login for https://github.com) must not go to
    `remote`, or None. Plain http keeps the job's traces (git_env), and GitHub redirects it to https, where git then
    asks the helper for the token; a `transport::address` remote runs whichever remote helper it names on any address,
    https included. An ssh URL, a path or a file:// URL gets no token."""
    if not remote:
        return 'the remote is empty'
    if remote.startswith('-'):
        return f'the remote {remote[:100]} starts with -: git would read it as an option'
    if remote.lower().startswith('http://'):
        return (f'the remote {remote[:100]} is plain http: GitHub would redirect it to https and git would hand the '
                'token over with the traces kept. Use https')
    if re.match(r'[A-Za-z][A-Za-z0-9+.-]*::', remote):
        return f'the remote {remote[:100]} names a remote helper (transport::address): give a URL or a path'
    return None


def init_bare(path):
    """Makes the empty folder `path` a bare repository as the queue's cache and the gate's repository are:
    `git init --bare --template=` (no hooks, no sample files), then CACHE_CONFIG and CACHE_HEAD written over what git
    wrote, never left to git's defaults (git 3.0 plans `main`). None, or why it could not be made. A repository git made
    in another format than SHA-1 objects with files refs (format 0, no extensions: a git that ignored GIT_FORMATS) is
    refused, never written over: CACHE_CONFIG would then name a format its refs or objects are not in."""
    r = subprocess.run(['git', *GIT_SAFE, 'init', '-q', '--bare', '--template=', path], text=True,
                       capture_output=True, env=git_env(no_token_env()))
    if r.returncode != 0:
        return f'git init failed: {r.stderr.strip()[:300]}'
    try:
        with open(os.path.join(path, 'config'), encoding='utf-8', errors='replace') as f:
            made = f.read()
        version = re.search(r'^\s*repositoryformatversion\s*=\s*(\S*)', made, re.M | re.I)
        section = re.search(r'^\s*\[\s*extensions\s*\]([^[]*)', made, re.M | re.I)
        if section or (version and version.group(1) != '0'):
            said = ', '.join(f'extensions.{k}={v}' for k, v in
                             re.findall(r'^\s*([\w.-]+)\s*=\s*(\S*)', section.group(1) if section else '', re.M))
            said = said or (f'format version {version.group(1)}' if version else 'an [extensions] section')
            return (f'git init made a repository with {said[:200]}: the '
                    'queue reads only SHA-1 objects and files refs (format 0, no extensions), and this git ignored '
                    'GIT_DEFAULT_HASH=sha1 and GIT_DEFAULT_REF_FORMAT=files')
        write_text(os.path.join(path, 'config'), CACHE_CONFIG)
        write_text(os.path.join(path, 'HEAD'), CACHE_HEAD)
    except OSError as e:
        return f'could not read or write its config: {e}'
    return None


def fetch_failed(stderr, login):
    """What a failed fetch says: git's own words, and when they are about a login, which one was missing or refused
    (`login`: where the run's token comes from)."""
    said = f'git fetch failed: {stderr.strip()[:300]}'
    if NO_LOGIN_RE.search(stderr):
        said += f' (git found no login for GitHub, or GitHub refused it: check {login})'
    return said


def git_problem(run=subprocess.run):
    """Why this git cannot run the queue (merge-tree --write-tree that reads attributes from GIT_ATTRS needs GIT_MIN),
    or None."""
    try:
        r = run(['git', 'version'], text=True, capture_output=True, env=no_token_env())
        out = r.stdout.strip()
    except OSError as e:
        out = str(e)
    m = re.match(r'git version (\d+)\.(\d+)', out)
    if m and tuple(map(int, m.groups())) >= GIT_MIN:
        return None
    return (f'git {".".join(map(str, GIT_MIN))} or newer is needed (merge-tree --write-tree that honours GIT_ATTR_SOURCE); '
            f'found: {out[:80] or "none"}')


def committed_config(path, run=None):
    """The config at `path` as the commit checked out there (HEAD) holds it: its blob, read with git, never the file. A
    checkout writes that file through filters and `.gitattributes` (a `working-tree-encoding` makes it UTF-16, an
    `ident` rewrites it), which a pull request can change without changing the config's own entry, so the head check
    would pass it. A path outside a git checkout, a commit without the config, an entry that is no regular file
    (a symlink, a submodule, a folder), a blob the checkout does not hold or that is not UTF-8 JSON is a ConfigError,
    never a crash. Nothing is fetched: a partial clone's missing blob would come from origin with the token the
    checkout stored."""
    full = os.path.abspath(path)
    where = os.path.dirname(full)
    while not os.path.isdir(where):   # the folder is missing when the commit has no config: git still says so
        where = os.path.dirname(where)

    def git(cwd, *a):
        try:   # no lazy fetch (git 2.44+), and no transport at all for an older git that ignores the variable
            r = (run or subprocess.run)(['git', *GIT_SAFE, '-C', cwd, *a], capture_output=True,
                                        timeout=GIT_READ_SECONDS, env={**git_env(no_token_env()),
                                                                       'GIT_NO_LAZY_FETCH': '1',
                                                                       'GIT_ALLOW_PROTOCOL': 'none'})
        except subprocess.TimeoutExpired:
            raise config.ConfigError(f'{path}: git {a[0]} took over {GIT_READ_SECONDS} seconds') from None
        except OSError as e:
            raise config.ConfigError(f'{path}: cannot run git to read it: {e}') from None
        return r.returncode, r.stdout, os.fsdecode(r.stderr or b'').strip()[:200]
    code, out, err = git(where, 'rev-parse', '--show-toplevel')
    if code != 0:
        raise config.ConfigError(f'{path}: not in a git checkout ({err}): the queue reads the config as the '
                                 'checked-out commit holds it, never the file')
    top, head, rest = os.path.realpath(os.fsdecode(out.rstrip(b'\n'))), full, []
    while os.path.realpath(head) != top:   # the path's own names below the top, links in them not followed
        head, name = os.path.split(head)
        if not name:
            raise config.ConfigError(f'{path}: not inside the checkout {top}')
        rest.insert(0, name)
    rel = '/'.join(rest)
    code, out, err = git(top, 'rev-parse', '--verify', '-q', 'HEAD^{commit}')
    sha = os.fsdecode(out).strip()
    if code != 0 or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise config.ConfigError(f'{path}: the checkout {top} has no commit checked out')
    code, out, err = git(top, 'ls-tree', '-z', sha, '--', rel)
    records = [x for x in out.split(b'\0') if x]
    if code != 0 or not records:
        raise config.ConfigError(f'{path}: the commit checked out ({sha[:12]}) has no {rel}'
                                 + (f': {err}' if err else ''))
    meta, _, name = records[0].partition(b'\t')
    fields = meta.decode('ascii', 'replace').split(' ')
    if len(records) != 1 or name != os.fsencode(rel) or len(fields) != 3:
        raise config.ConfigError(f'{path}: git ls-tree {sha[:12]} -- {rel} did not list one entry')
    mode, kind, oid = fields
    if mode not in ('100644', '100755'):   # git lists a blob in no other mode as a file
        what = {'120000': 'symlink', '160000': 'submodule', '040000': 'tree'}.get(mode, kind[:20])
        raise config.ConfigError(f'{path}: at {sha[:12]} {rel} is a {what} (mode {mode}), not a file (mode 100644 or '
                                 '100755): make it a regular file')
    code, out, err = git(top, 'cat-file', 'blob', oid)
    if code != 0:
        # a partial clone (a sparse checkout's) leaves out the blobs its pattern does not check out
        partial = git(top, 'config', '--get-regexp', r'^(extensions\.partialclone|remote\..*\.promisor)$')[0] == 0
        if partial and git(top, 'cat-file', '-e', oid)[0] != 0:
            raise config.ConfigError(f'{path}: cannot read {rel} at {sha[:12]}: its blob {oid[:12]} is not in this '
                                     'checkout (the queue fetches nothing). In a partial clone, check the config out: '
                                     'the sparse-checkout pattern must match its name, which glob characters '
                                     f'([ ] * ?), a leading ! or #, a trailing space or a backslash keep it from doing '
                                     f'({err})')
        raise config.ConfigError(f'{path}: cannot read {rel} at {sha[:12]}: {err}')
    return config.parse(out, f'{path} at {sha[:12]}')


class Queue:
    """One repository's queue. gh (reads), gh_write (writes), git and sleep are attributes, so tests replace them."""

    def __init__(self, repo, cfg, state, cache, lock, sleep=time.sleep, read_token=None, write_token=None):
        self.repo, self.cfg, self.state, self.cache, self.lock, self.sleep = repo, cfg, state, cache, lock, sleep
        self.known = state + FSCK_KNOWN
        self.read_token, self.write_token, self.said = read_token, write_token, set()
        self.remote = f'https://github.com/{repo}.git'
        self.later, self.fetch_error, self.fsck_said = None, '', ''   # check_later, fetch, objects_problem
        self.rebuilt = False   # this run downloaded the cache from scratch (fetch_again): never a second time
        self.unknown = 'the queue could not fetch from GitHub'   # why fetch_checked knows nothing (PR comment words)
        self.review = lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association,
                                  cfg.lens_author_associations)

    @staticmethod
    def gh_env(token):
        """The environment for one gh or git: this process's, with no token but `token` as GH_TOKEN."""
        return {**no_token_env(), 'GH_TOKEN': token}

    @staticmethod
    def run_gh(token, a, which):
        """gh with exactly `token`; without one gh is not started (never the machine's own login)."""
        if not token:
            return subprocess.CompletedProcess(['gh', *a], 1, '', f'pr_queue: no {which} token, so gh was not started')
        return subprocess.run(['gh', *a], text=True, capture_output=True, env=Queue.gh_env(token))

    def gh(self, *a):
        """A read, with the run's token (read-only: it can change nothing)."""
        r = self.run_gh(self.read_token, a, 'read')
        if r.returncode != 0 and NO_PERMISSION_RE.search(r.stderr or ''):
            self.say_once('read', f"::error::pr-queue: the run's GITHUB_TOKEN may not read this ({' '.join(a[:2])}): "
                                  f'the queue job needs {READ_PERMISSIONS}, and the calling workflow\'s job must grant '
                                  'them too (a reusable workflow gets no more than its caller gives). The queue '
                                  'decides nothing on a read that failed')
        return r

    def gh_write(self, *a):
        """A write, or the one read that must be the poster's (tell), with the caller's token."""
        r = self.run_gh(self.write_token, a, 'write')
        if r.returncode != 0 and NO_PERMISSION_RE.search(r.stderr or ''):
            self.say_once('write', f"::error::pr-queue: the queue's token (the `token` secret) may not do this "
                                   f"({' '.join(a[:2])}): it needs contents, pull requests and issues: write, and "
                                   'workflows: write when the base brings changes to workflow files')
        return r

    def warn_if_branches_stay(self):
        """Read the repository setting "Automatically delete head branches" once, with the run's read token, and warn
        when it is off or cannot be read. Never fails the run and decides nothing: the queue works the same either way,
        only merged branches pile up (README.md, "Config", delete_branch). run_gh, not gh: a refused read here must not
        print gh()'s ::error:: (or use up its say_once). Returns the setting, or None when unknown."""
        owner, name = self.repo.split('/', 1)
        why = ''
        try:
            r = self.run_gh(self.read_token, ('api', 'graphql', '-f', f'query={DELETE_ON_MERGE_QUERY}',
                                              '-f', f'owner={owner}', '-f', f'name={name}'), 'read')
            if r.returncode != 0:
                why = (r.stderr or r.stdout or '').strip()[:200] or f'gh exited {r.returncode}'
            else:
                answer = json.loads(r.stdout)
                on = answer['data']['repository']['deleteBranchOnMerge']
                if not isinstance(on, bool):
                    raise TypeError
                if not on:
                    print('::warning::pr-queue: the repository setting "Automatically delete head branches" '
                          '(delete_branch_on_merge) is off, so merged branches stay: the queue never deletes a branch '
                          'itself. Turn it on (Settings, General, Pull Requests); GitHub then retargets pull requests '
                          'stacked on a merged branch to its base before it deletes the branch')
                return on
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            why = why or f'GitHub gave no answer of that shape ({type(e).__name__})'
        print('::warning::pr-queue: could not read whether the repository setting "Automatically delete head branches" '
              f'is on ({why}). The queue never deletes a branch itself: if the setting is off, merged branches stay')
        return None

    def say_once(self, key, text):
        if key not in self.said:
            self.said.add(key)
            print(text)

    def git(self, *a, fetch=False, errors='strict', repo=None):
        """git in the cache, with the run's read token, and with no token at all when there is none (never gh's own
        login of the machine). `fetch`: the call fetches from the remote (only Queue.fetch's two), so it keeps what of
        the job's GIT_RUN the remote may need; every other call runs none of it (git_env). `errors`: how git's output
        is decoded (UTF-8). `repo`: another repository than the cache (the merge check's own: holds_checked)."""
        argv, env = self.git_call(*a, fetch=fetch, repo=repo)
        return subprocess.run(argv, text=True, encoding='utf-8', errors=errors, capture_output=True, env=env)

    def git_call(self, *a, fetch=False, repo=None):
        """The argv and environment of Queue.git's call."""
        token = self.read_token
        env = self.gh_env(token) if token else no_token_env()
        return (['git', *GIT_SAFE, *(GIT_AUTH if token else GIT_AUTH[:2]), '--git-dir', repo or self.cache, *a],
                git_env(env, self.remote if fetch else None))

    # --- step 1 and 4: the queue's own git repository -------------------------------------------------------------
    def cache_problem(self):
        """Why the cache is not the one the queue made, or None. git runs there with the token in its environment, and
        its objects decide what is re-armed: only a directory of this user that no one else can enter, whose config is
        CACHE_CONFIG and whose HEAD is CACHE_HEAD naming no commit (a HEAD is an attribute source in a bare repository),
        that holds none of the files that make git read other objects or history (UNTRUSTED_FILES) and that is a bare
        repository, counts."""
        try:
            st = os.lstat(self.cache)
            if not stat.S_ISDIR(st.st_mode):
                return 'is not a directory'
            if st.st_uid != os.getuid() or st.st_mode & 0o077:
                return f'is not this user\'s alone (owner {st.st_uid}, mode {st.st_mode & 0o777:o})'
            with open(os.path.join(self.cache, 'config'), opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as f:
                if f.read() != CACHE_CONFIG:
                    return 'has a config the queue did not write'
            with open(os.path.join(self.cache, 'HEAD'), opener=lambda p, f: os.open(p, f | os.O_NOFOLLOW)) as f:
                if f.read() != CACHE_HEAD:
                    return 'has a HEAD the queue did not write'
            for name in UNTRUSTED_FILES:
                if os.path.lexists(os.path.join(self.cache, name)):
                    return f'holds {name}, which the queue never writes'
        except (OSError, ValueError) as e:
            return f'cannot be checked ({e})'
        if self.git('rev-parse', '--is-bare-repository').stdout.strip() != 'true':
            return 'is not a bare git repository'
        if self.git('rev-parse', '-q', '--verify', 'HEAD').returncode != 1:   # 1: no such commit, as the queue made it
            return 'has a branch at HEAD: HEAD names a commit (the queue never makes its branch)'
        return None

    def own_cache(self):
        """The cache, made on first use (`git init --bare`; the first fetch brings the objects). One that is not the
        queue's own (cache_problem: emptied, half-deleted, someone else's, open to others, another config) is set aside
        and made again, never trusted. Every failure on the way is said and returns False (fetch then knows nothing,
        so nothing is done), never an exception."""
        if os.path.lexists(self.cache):
            problem = self.cache_problem()
            if not problem:
                return self.drop_stale_locks()
            print(f'{self.cache} {problem}: rebuilding it')
        parent, name = os.path.split(self.cache)
        try:
            os.makedirs(parent, exist_ok=True)
            old = [os.path.join(parent, f) for f in sorted(os.listdir(parent))   # CACHE.old-*, CACHE.new.old-*
                   if f.startswith(name + '.') and '.old-' in f]
        except OSError as e:   # EACCES, EROFS, ENOSPC: nothing made, the next run tries again
            print(f'could not make or list {parent}, the folder of {self.cache}: {e}')
            return False
        remove_old(*old)
        new = self.cache + '.new'
        if not (set_aside(self.cache) and set_aside(new)):   # a broken cache; one made half-way
            return False
        try:
            os.mkdir(new, 0o700)   # no one else can enter it, from the start
            os.chmod(new, 0o700)
        except OSError as e:
            print(f'could not make {new}: {e}')
            return False
        problem = init_bare(new)
        if problem:
            print(f'could not make {self.cache}: {problem}')
            return False
        try:
            os.rename(new, self.cache)
        except OSError as e:   # something made the cache between the two steps: the next run sets it aside
            print(f'could not move the new repository to {self.cache}: {e}')
            return False
        return True

    def drop_stale_locks(self):
        """A fetch killed half-way leaves a ref lock that fails every later fetch. Only the queue uses the cache, and
        only while it holds the lock: a lock file here is always a stale one. True when none is left; one that cannot
        be deleted, or refs that cannot be listed, are said and return False (own_cache). No refs/queue yet (a cache
        whose first fetch failed) has no lock: nothing to do."""
        top = os.path.join(self.cache, 'refs', 'queue')

        def stop(e):
            if not (isinstance(e, FileNotFoundError) and e.filename == top):
                raise e
        path = None
        try:
            for d, _, files in os.walk(top, onerror=stop):
                for f in files:
                    if f.endswith('.lock'):
                        path = os.path.join(d, f)
                        os.remove(path)
                        path = None
        except OSError as e:
            print(f'could not delete the stale ref lock {path}: {e}' if path else
                  f'could not look for stale ref locks in {self.cache}: {e}')
            return False
        return True

    def objects_problem(self, r=None):
        """Why the cache's objects are not what GitHub sent, or None. git checks an object's hash on only some reads
        (rev-parse, merge-base and merge-tree of a commit; never a tree or blob merge-tree reads, nor a commit rev-list
        walks), and the cache is kept between runs: a tree or blob an earlier job wrote under the name of one side's
        (a loose object where the real one is loose too, as a small fetch leaves it, or a pack of its own) makes a real
        conflict merge clean. git fsck checks the hash of every object, loose and packed, and that each ref's history
        is complete; an old format GitHub serves is no problem (FSCK_FORMAT_IDS). It reads every object, on one core:
        about 30 s for 270,000 objects. `r`: the answer of a git fsck already run (check_later). Keeps git fsck's
        lines in self.fsck_said (fetch_checked compares them)."""
        if r is None:
            r = self.git(*FSCK_IGNORE, 'fsck', '--no-dangling', '--no-reflogs', '--no-progress', errors='replace')
        if r.returncode == 0:
            self.fsck_said = ''
            return None
        said = [line for line in (r.stderr + r.stdout).splitlines() if line.strip()]
        self.fsck_said = '\n'.join(sorted(set(said)))
        return f'has objects git fsck finds broken or missing ({said[0][:200] if said else f"exit {r.returncode}"})'

    def check_later(self):
        """objects_problem's git fsck, started in the background (update(), right after its fetch): it reads the cache
        while the disarm, the branch update and the wait for the new head run, none of which touches the cache. The
        next fetch_checked waits for its answer before it fetches, and update() stops it if the run ends first."""
        argv, env = self.git_call(*FSCK_IGNORE, 'fsck', '--no-dangling', '--no-reflogs', '--no-progress')
        try:
            self.later = subprocess.Popen(argv, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                                          encoding='utf-8', errors='replace')
        except OSError as e:   # checked in the foreground then
            print(f'could not start git fsck in the background: {e}')

    def checked_before(self):
        """The background check's answer: None when none ran, '' for a healthy cache, else the problem."""
        p, self.later = self.later, None
        if p is None:
            return None
        out, err = p.communicate()
        return self.objects_problem(subprocess.CompletedProcess(p.args, p.returncode, out, err)) or ''

    def stop_check(self):
        """The background check, if one still runs, stopped and reaped."""
        p, self.later = self.later, None
        if p is not None:
            p.kill()
            p.communicate()

    def known_broken(self):
        """What the cache's check said right after its download from scratch (self.known), or None. Only a record the
        queue wrote counts (read_own). Any other is no record, so the cache is downloaded once more and the record
        written again."""
        return read_own(self.known)

    def keep_known(self, said):
        try:
            write_text(self.known, said)
        except OSError as e:
            print(f'could not keep what the check of {self.cache} said in {self.known}: {e}')

    def forget_known(self):
        """A check the cache passed: an older record says nothing about this cache any more."""
        try:
            os.remove(self.known)
        except FileNotFoundError:
            pass
        except OSError as e:
            print(f'could not delete {self.known}: {e}')

    def has(self, sha):
        return bool(SHA.fullmatch(sha or '')) and self.git('cat-file', '-e', sha + '^{commit}').returncode == 0

    def fetch(self, base, n, *shas):
        """The base branch (refs/queue/base), PR n's head and `shas` into the cache. True when every sha is there,
        False when the refs came but a sha is not on GitHub, None when nothing could be fetched (no cache, a git or
        network error: nothing is known)."""
        self.fetch_error = ''   # git fetch's words when it failed (fetch_checked)
        if not re.fullmatch(r'[\w./-]+', base or '') or not all(SHA.fullmatch(s or '') for s in shas):
            return False
        if not self.own_cache():
            return None
        r = self.git(*NO_DETACH, 'fetch', '-q', '--no-tags', self.remote, f'+refs/heads/{base}:refs/queue/base',
                     f'+refs/pull/{n}/head:refs/queue/pr{n}', fetch=True)
        if r.returncode != 0:
            print(f'#{n}: {fetch_failed(r.stderr, READ_LOGIN)}')
            self.fetch_error = r.stderr
            return None
        missing = [s for s in shas if not self.has(s)]
        if missing:   # a read commit no ref reaches any more (GitHub keeps it)
            r = self.git(*NO_DETACH, 'fetch', '-q', '--no-tags', self.remote, *missing, fetch=True)
            if r.returncode != 0 and not GONE_RE.search(r.stderr):   # a network error is no "not on GitHub"
                print(f'#{n}: {fetch_failed(r.stderr, READ_LOGIN)} (of {", ".join(s[:12] for s in missing)})')
                self.fetch_error = r.stderr
                return None
        return all(self.has(s) for s in shas)

    def fetch_checked(self, base, n, *shas, later=False):
        """fetch, with the cache's objects checked (objects_problem) before anything reads them: True, False or None
        as fetch returns them, and None for a cache that is still broken after a download from scratch. A cache found
        broken (by this run's earlier background check, after the fetch, or after a fetch that failed for a cause
        that is neither the network nor the login) is set aside and downloaded from scratch once, then checked again.
        A cache whose check gives the same lines as right after its own download from scratch (self.known) is not
        downloaded again, and neither is one this run already downloaded from scratch (self.rebuilt: it passed its
        check then, so it was written since; nothing is kept in self.known, and the next run downloads it). `later`:
        the check after a good fetch runs in the background (check_later). self.unknown says why None, in words for a
        PR comment (no runner paths). What the check passes is still not what the merge check reads: that runs on a
        copy whose hashes are checked as it is made (holds_checked)."""
        self.unknown = 'the queue could not fetch from GitHub'
        before = self.checked_before()
        problem = before
        if not problem:
            got = self.fetch(base, n, *shas)
            if got is None and self.fetch_error and not (NO_LOGIN_RE.search(self.fetch_error)
                                                         or NETWORK_RE.search(self.fetch_error)):
                problem = self.objects_problem()
            elif got and before is None:
                if later:
                    self.check_later()
                    return got
                problem = self.objects_problem()
            if not problem:
                return got
        if self.fsck_said and self.fsck_said == self.known_broken():
            print(f'#{n}: {self.cache} {problem}: the same way as right after its download from scratch, so the '
                  'history GitHub serves fails this git fsck. Not downloaded again and not merged in: a person must '
                  'look')
            self.unknown = ("the queue's git cache fails git fsck the same way as right after a download from "
                            "GitHub (the run's log says why), so a person must look")
            return None
        if self.rebuilt:
            print(f'#{n}: {self.cache} {problem}, after this run already downloaded it from scratch: not downloaded a '
                  'second time in one run and not merged in')
            self.unknown = ("the queue's git cache fails git fsck again after this run fetched it again from GitHub "
                            "(the run's log says why)")
            return None
        return self.fetch_again(base, n, shas, problem)

    def fetch_again(self, base, n, shas, problem):
        """The cache, which `problem`, set aside and downloaded from scratch, then checked (objects_problem): as
        fetch returns, and None for one that is still broken (its lines kept: self.known). At most once per run:
        the callers ask self.rebuilt first."""
        self.rebuilt = True
        print(f'{self.cache} {problem}: rebuilding it')
        got = self.fetch(base, n, *shas) if set_aside(self.cache) else None
        if got and (problem := self.objects_problem()):
            print(f'#{n}: {self.cache}, fetched again from GitHub, still {problem}: not merged in')
            self.keep_known(self.fsck_said)
            self.unknown = ("the queue's git cache fails git fsck even when fetched again from GitHub "
                            "(the run's log says why)")
            return None
        return got

    def same_change(self, base, n, head, read):
        """Step 4's check (holds) on GitHub's commits: True, False (any other content or a git error), or None when
        nothing is known (nothing could be fetched, or the cache's objects are not GitHub's: the caller keeps STATE
        and tries again). Only full shas count.

        The check runs on a copy of the objects it reads, each hashed as it is copied (holds_checked). An object that
        is missing or not what its name says, though git fsck passed, was written while this run went on: the cache is
        downloaded from scratch and the check made again, as for a cache git fsck finds broken. One that fails in a
        cache this run downloaded from scratch (for this or, before, for its git fsck: self.rebuilt, so one download
        per run) says None, and its line is kept (self.known) so the next run does not download it all once more."""
        if not (SHA.fullmatch(head or '') and SHA.fullmatch(read or '')):
            return False
        if head == read:
            return True
        got = self.fetch_checked(base, n, head, read)   # the whole cache: finds and rebuilds a broken one
        for _ in range(2):   # the check, and once more after the run's one download from scratch
            if not got:
                return got
            try:
                same = self.holds_checked(head, read)
            except OSError as e:   # no room, no folder, or a reader that hangs: nothing is known, the cache is kept
                print(f'#{n}: could not copy the objects of the merge check out of {self.cache}: {e}')
                self.unknown = "the queue could not copy the objects of its merge check (the run's log says why)"
                return None
            except Changed as e:
                problem = (f'has an object the merge check reads that is missing or not what its name says, although '
                           f'git fsck passed ({e})')
                said = f'merge check: {e}'
                if self.rebuilt or said == self.known_broken():
                    if self.rebuilt:
                        print(f'#{n}: {self.cache}, fetched again from GitHub, still {problem}: not merged in')
                        self.keep_known(said)
                    else:
                        print(f'#{n}: {self.cache} {problem}: the same way as right after its download from '
                              'scratch. Not downloaded again and not merged in: a person must look')
                    self.unknown = ("the queue's git cache has an object that is missing or not what its name says, "
                                    "although git fsck passed and even when fetched again from GitHub (the run's log "
                                    "says why), so a person must look")
                    return None
                got = self.fetch_again(base, n, (head, read), problem)
            else:
                self.forget_known()
                return same
        # Not reached while fetch_again counts its download (self.rebuilt). Should that ever break: nothing is known.
        print(f'#{n}: the merge check of {self.cache} failed again after a download from scratch: not merged in')
        self.unknown = "the queue's merge check failed again after its git cache was fetched again from GitHub"
        return None

    def holds_checked(self, head, read):
        """holds() in a repository of its own (CACHE + MERGE_COPY, deleted before this returns) that holds only what
        this call copied out of the cache and hashed (Copy): every commit head, read and the base reach, and for each
        merge-tree the trees and blobs that merge can read, copied right before it. Should git still miss an object
        there, all of the merged commits' trees are copied and the merge is run once more; a merge git calls clean
        with an error on stderr is never taken as clean. Changed: an object is not what its name says. OSError: the
        copy cannot be made. A folder a killed run left is deleted first."""
        base = self.git('rev-parse', '--verify', '-q', 'refs/queue/base^{commit}').stdout.strip()
        if not SHA.fullmatch(base):
            raise Changed('refs/queue/base names no commit')
        to = self.cache + MERGE_COPY
        if not set_aside(to):
            raise OSError(f'{to} is in the way')
        try:
            os.mkdir(to, 0o700)   # new, and no one else can enter it
            os.chmod(to, 0o700)
            problem = init_bare(to)
            if problem:
                raise OSError(problem)
            copy = Copy(*self.git_call('cat-file', '--batch'), to)
            try:
                copy.take_commits(head, read, base)

                def git(*a, **kw):
                    for arg in a:   # git names a commit's tree (holds: rev-parse) only when the tree is there
                        if arg.endswith('^{tree}') and arg[:-len('^{tree}')] in copy.tree_of:
                            copy.tree(copy.tree_of[arg[:-len('^{tree}')]])
                    return self.git(*a, repo=to, **kw)

                def merge(p, side):
                    bases = bases_of(git, p, side)
                    if bases is None:
                        print(f'::warning::pr-queue: {p[:12]} and {side[:12]} have more than {MERGE_BASES} merge '
                              'bases (with those of each pair of them): not merged, so the head is not taken as the '
                              'change read')
                        return subprocess.CompletedProcess(['git', 'merge-tree'], 1, '', '')
                    roots = [copy.tree_of[c] for c in (p, side, *bases)]
                    copy.take_merged(roots)
                    m = merge_tree(git, p, side)
                    # A merge or a conflict says nothing on stderr. Anything else with a merge base: git stopped, or
                    # went on without an object (a git that cannot read a commit's tree merges it as an empty one).
                    if bases and (m.returncode not in (0, 1) or m.stderr.strip()):
                        for root in roots:
                            copy.take_whole(root)
                        m = merge_tree(git, p, side)
                    if m.returncode == 0 and m.stderr.strip():
                        print(f'::warning::pr-queue: git merge-tree of {p[:12]} and {side[:12]} called the merge clean '
                              f'and said {m.stderr.strip()[:300]!r}: not taken as a clean merge')
                        m.returncode = 1
                    return m
                return holds(git, head, read, base=base, merge=merge)
            finally:
                copy.close()
        finally:
            remove_old(to)

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

    def gate_rollup(self, rollup, head, n):
        """The rollup with only the gate App's runs of head_check (config head_check_app), or None when they could not
        be read (printed). GitHub's rollup names no App, so a job or a commit status of that name from anyone else
        would count by name: every head_check item is dropped and the App's newest run, read by app id, put in its
        place. Without the key the rollup is returned as it is."""
        app, name = self.cfg.head_check_app, self.cfg.head_check
        if not app:
            return rollup
        if not SHA.fullmatch(head or ''):
            print(f'#{n}: no head to read the {name} check of')
            return None
        q = urllib.parse.urlencode({'check_name': name, 'app_id': app, 'filter': 'latest', 'per_page': 100})
        r = self.gh('api', f'repos/{self.repo}/commits/{head}/check-runs?{q}')
        try:
            runs = json.loads(r.stdout)['check_runs'] if r.returncode == 0 else None
            if not isinstance(runs, list) or not all(isinstance(c, dict) for c in runs):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            print(f'#{n}: reading the {name} check of App {app} failed: {r.stderr.strip()[:200] or "no check_runs"}')
            return None
        mine = [{'__typename': 'CheckRun', 'name': name, 'status': (c.get('status') or '').upper(),
                 'conclusion': (c.get('conclusion') or '').upper() or None, 'startedAt': c.get('started_at'),
                 'workflowName': ''} for c in runs
                if c.get('name') == name and (c.get('app') or {}).get('id') == app]
        return [c for c in rollup or [] if (c.get('name') or c.get('context')) != name] + mine

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
        gate = self.cfg.head_check if self.cfg.head_check_app else None
        if missing == [gate]:   # no workflow of the head posts it: the gate's run lists on the default branch
            return gate, GATE_NOT_YET
        return next(m for m in missing if m != gate), NEVER

    def stuck_fix(self, what, base):
        if what == GATE_NOT_YET:   # the head gate's App posts it from a run of the default branch, not of this head
            return ('The head gate posts it as its App, from a run of the default branch: on the next push, '
                    'ready-for-review, base change or issue comment, and after any earlier gate run on the same head. '
                    'If none comes, dispatch the gate with this pull request number (Actions, the gate caller, '
                    '"Run workflow")')
        if what == NEVER:   # a re-run repeats the run's own workflow file, which has no job for this check
            return (f'A re-run cannot add it: a re-run repeats the jobs of the workflow files this head was built '
                    f'with, and none of them reported it. Push again (`{self.cfg.push_command}`) or merge {base} into '
                    'it, so that the current workflows run')
        return 'Nothing re-runs it by itself: re-run the CI run (Actions, "Re-run failed jobs") or push'

    def tell(self, n, head, reason, way_out, still=None, kind='red', link=None):
        """An armed PR the queue skips says why on the PR, once per head and reason. No answer from GitHub: the next
        run tries again. Told means a comment of the queue's own login (viewerDidAuthor) that starts with the mark:
        anyone can post a copy of it on a public repository. `head` is the head the reason was found on (the list):
        after a push the reason is not known for the new head, and a mark for it would silence its real notice.
        `still`: what the PR must still show when it is read for this (the list is older), else nothing is said.
        Once the comment is posted, notify_command runs (notify) with `kind` and `link`: the comment is the record that
        this head and reason were told, on GitHub, so a later run, a restart or another runner never runs it again.
        So while a notify_command is set and this run's NOTIFY_BUDGET is spent, nothing is posted: the next run, with a
        fresh budget, posts it and runs the command. No notice is lost, and none runs twice."""
        pr = self.view(n, gh=self.gh_write)   # viewerDidAuthor: the login that posts the queue's comments
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
        if self.cfg.notify_command and self.notify_left() < 1:   # the comment would say it was told, and wake no one
            print(f"#{n}: this run's notify commands used their {NOTIFY_BUDGET} s, so it does not post this notice "
                  'yet: the next run tells it (and runs notify_command)')
            return
        r = self.gh_write('pr', 'comment', str(n), '--repo', self.repo, '--body',
                          f'{told} so this armed PR waits and the queue passes over it. {way_out}')
        if r.returncode != 0:
            print(f'#{n}: could not comment: {r.stderr.strip()[:300]}')
            return
        self.notify(n, (pr.get('headRefOid') or '').lower(), kind, reason, link)

    def notify(self, n, head, kind, reason, link=None):
        """Run the config's notify_command for a notice tell() just posted, or for an unreadable plan report_bad() just
        recorded (n 0, head ''): argv + repo, PR, head, kind, reason, link (README.md, "Notify"). Never part of the
        queue's result: a command that is missing, fails or runs longer than notify_timeout_seconds (then it and its
        processes are killed: stop_notify) is a warning, and the run goes on.
        All commands of a run share NOTIFY_BUDGET seconds; tell() posts no notice once that is spent, so the next run
        posts and runs it. A command that fails, hangs or cannot start is not tried again: the posted comment says it
        was told, and a later run must not wake anyone again."""
        cmd = self.cfg.notify_command
        if not cmd:
            return
        page = f'https://github.com/{self.repo}/'
        if not repo_page(link, page):
            link = f'{page}pull/{n}'
        argv = [*cmd, self.repo, str(n), head, kind, reason, link]
        # A bad-plan notice has no PR and no PR comment: its record is STATE.told-bad (report_bad).
        who, stands = (f'#{n}', 'The PR comment stands') if n else \
            ('unreadable plans', f'{self.state + TOLD_BAD} says it was told')
        left = self.notify_left()
        if left < 1:   # tell() posts nothing then; a direct call still stays inside the budget
            print(f'::warning::pr-queue: notify_command for {who} ({kind}) not run: this run\'s notify commands took '
                  f'{NOTIFY_BUDGET} s together. {stands}; nothing is tried again')
            return
        limit = min(self.cfg.notify_timeout_seconds, left)
        sys.stdout.flush()
        start, code, ran = time.monotonic(), None, False
        try:
            with tempfile.TemporaryFile() as out:   # a file, not a pipe: a helper it leaves running cannot hold the run
                p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                     env=notify_env(), start_new_session=True)
                ran = True
                try:
                    code = p.wait(timeout=limit)
                except subprocess.TimeoutExpired:
                    stop_notify(p.pid)
                    p.wait()
                    code = None
                out.seek(0)
                said = out.read(NOTIFY_OUTPUT + 1).decode('utf-8', 'replace')
        except (OSError, ValueError) as e:   # cannot start, no temp file (a full disk), or its output cannot be read
            what = 'ran, but its output could not be read' if ran else 'could not start'
            print(f'::warning::pr-queue: notify_command for {who} ({kind}) {what}: {e}. {stands}; nothing is '
                  'tried again')
            return
        finally:
            self.notify_spent = getattr(self, 'notify_spent', 0.0) + time.monotonic() - start
        if said:
            # Its output is not the queue's: no workflow command from it, neither ::cmd:: (the prefix: the runner
            # reads it only at a line's start) nor the older ##[cmd] (read anywhere in a line), so the runner stops
            # reading commands until a token the command cannot know.
            # The resume line is printed whatever happens to the lines before it (an encoding error on a non-UTF-8
            # stdout), or the runner would ignore every later ::warning:: of the step.
            resume, shown = secrets.token_hex(16), None
            print(f'::stop-commands::{resume}')
            try:
                for line in said[:NOTIFY_OUTPUT].splitlines():
                    print(f'  notify| {line}')
            except (OSError, ValueError) as e:
                shown = e
            finally:
                print(f'::{resume}::')
            if shown:
                print(f'::warning::pr-queue: notify_command for {who} ({kind}): its output could not all be shown: '
                      f'{shown}')
        if code is None:
            print(f'::warning::pr-queue: notify_command for {who} ({kind}) ran longer than {limit:.3g} s and was '
                  f'stopped. {stands}; nothing is tried again')
        elif code != 0:
            print(f'::warning::pr-queue: notify_command for {who} ({kind}) exited {code}. {stands}; nothing is tried '
                  'again')
        else:
            print(f'{who}: notify_command ran for {kind}' + (f' on {head[:12]}' if head else ''))

    def notify_left(self):
        """Seconds of NOTIFY_BUDGET this run's notify commands have not used yet (one Queue per run)."""
        return NOTIFY_BUDGET - getattr(self, 'notify_spent', 0.0)

    def tell_red(self, n, head, check, what='is red', fix='Fix the cause and push', kind='red', link=None):
        """tell() for a required check: once per head, check and reason."""
        also = '.'
        if check == self.cfg.head_check and what == 'is red':
            also = f', or if a new lens read or override covers this head: `{self.cfg.ready(n)}`.'
        self.tell(n, head, f'required check {check} {what}', fix + also, kind=kind, link=link)

    @staticmethod
    def check_link(p, check):
        """The page of the newest run of required check `check` on the listed PR `p` (its detailsUrl, or a status's
        targetUrl), for notify; None when it has none (notify then links the PR)."""
        runs = [c for c in lens.latest_checks(p.get('statusCheckRollup') or [])
                if (c.get('name') or c.get('context')) == check]
        if not runs:
            return None
        c = max(runs, key=lambda c: c.get('startedAt') or '9999')
        return c.get('detailsUrl') or c.get('targetUrl')

    def tell_dirty(self, n, head, base):
        """tell() for an armed PR that conflicts with its base (merge state DIRTY): GitHub cannot merge it and the queue
        cannot update it, and auto-merge stays on without a word, so without this it waits unseen. Once per head: a
        push is the only way out, and a new head that still conflicts is told again. Said only while the PR still
        shows the conflict and is still armed."""
        self.tell(n, head, f'merge conflict with {base}',
                  f'GitHub cannot merge it and the queue cannot update it: merge {base} into it, resolve the conflict '
                  f'and push (`{self.cfg.push_command}`).',
                  still=lambda pr: pr.get('mergeStateStatus') == 'DIRTY' and bool(pr.get('autoMergeRequest')),
                  kind='conflict')

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
             '... on CheckRun{name status conclusion startedAt checkSuite{app{databaseId} workflowRun{workflow{name}}}} '
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
                if self.cfg.head_check_app:   # only the gate App's runs of head_check count (gate_rollup)
                    rollup = [x for x in rollup if (x.get('name') or x.get('context')) != self.cfg.head_check
                              or (x.get('__typename') == 'CheckRun' and ((x.get('checkSuite') or {}).get('app')
                                                                         or {}).get('databaseId') == self.cfg.head_check_app)]
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
    def view(self, n, reviews=False, gh=None):
        """PR n as gh shows it, or None. reviews: also its reviews' own flags and its inline review comments
        (review_flags, under lens.REVIEW_FLAGS and lens.REVIEW_COMMENTS), for the review rules; None when they cannot
        be read, so nothing is decided on a review or an inline comment nobody could check. Read for every PR, also
        one gh pr view lists no review for: an inline comment shows only in that read. gh: the call to read with
        (default the read token's)."""
        r = (gh or self.gh)('pr', 'view', str(n), '--repo', self.repo, '--json', VIEW)
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
        # One line: a multi-line gh error could start a line with `covered:`, `checks:` or a heading naming a lens.
        why = lens.no_lens_text(' '.join(why.split()))
        why += ' ' + lens.no_lens_text(advice, keep_markdown=True) if advice else ''
        print(f'#{n}: auto-merge left OFF: {why}')
        r = self.gh_write('pr', 'comment', str(n), '--repo', self.repo, '--body',
                          f'pr_queue turned auto-merge OFF: {why}\nNeeds a lens read of any new commits, then '
                          f'`{self.cfg.ready(n)}`.')
        if r.returncode != 0:
            print(f'#{n}: could not comment either: {r.stderr.strip()[:300]}')
            return SILENT
        return 0

    def leave_unsure(self, n):
        """Auto-merge is OFF and the queue cannot tell whether it or a person turned it off (its disarm reported an
        error, or the run stopped there): it is never turned back on, so a person's switch stands. Not updated. 0, or
        SILENT when the comment failed (the caller keeps STATE, so the next run says it)."""
        print(f'#{n}: auto-merge is OFF, maybe by a person: left OFF and not updated')
        r = self.gh_write('pr', 'comment', str(n), '--repo', self.repo, '--body',
                          'pr_queue left auto-merge OFF: it is off, and the queue cannot tell whether its own call '
                          'or a person turned it off (that call reported an error, or the run stopped there), so it '
                          'did not update this PR and does not turn auto-merge back on. To queue it again: '
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
            # never --delete-branch: when GitHub can merge at once, gh merges and then deletes the branch with the
            # token, and GitHub CLOSES every open PR stacked on it; the repository's own "Automatically delete head
            # branches" setting retargets them to this PR's base first (README.md, "Config", delete_branch)
            r = self.gh_write('pr', 'merge', str(n), '--repo', self.repo, '--auto', f'--{self.cfg.merge_method}',
                              '--match-head-commit', head)
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
            why += f' (GitHub still said so after the queue tried again for up to {self.cfg.wait_seconds} s)'
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
        try:
            fetched = bool(read) and self.fetch_checked(base, n, read, later=True)
            if read and fetched is None:   # nothing changed yet: an armed PR stays as it was, the next run tries again
                print(f'#{n}: {self.unknown} (see above): nothing changed, the next run tries again')
                return 1
            # Read right before the disarm (the fetch above can take long): off by now is someone else's switch, not
            # the queue's to undo. This, not the disarm's answer, tells a person's switch-off from the queue's own:
            # what GitHub answers for turning off an auto-merge that is already off is not documented (README.md).
            pre = self.view(n)
            if pre is None:
                print(f'#{n}: gh cannot show it now: nothing changed, the next run tries again')
                return 1
            if pre.get('state') != 'OPEN' or not pre.get('autoMergeRequest'):
                print(f'#{n}: auto-merge was turned off by someone else (or it is not open any more) right before the '
                      'queue would turn it off: left as it is, not updated')
                return 0
            plan = {'repo': self.repo, 'pr': n, 'old': old, 'base': base, 'read': read}
            self.write_state(dict(plan, unsure=True))   # until the disarm answers OK: maybe a person turned it off
            code = self.update_off(plan, fetched, why)
        finally:
            self.stop_check()
        if code != SILENT:
            self.drop_state()
        return code

    def update_off(self, plan, fetched, why):
        n, old, base, read = plan['pr'], plan['old'], plan['base'], plan['read']
        r = self.gh_write('pr', 'merge', str(n), '--repo', self.repo, '--disable-auto')
        if r.returncode != 0:
            print(f'#{n}: could not turn auto-merge off, so not updating: {r.stderr.strip()[:300]}')
            cur = self.view(n)   # gh may report an error for a change GitHub applied
            if cur and cur.get('state') == 'OPEN' and cur.get('autoMergeRequest'):
                return 1   # still armed: nothing changed
            if cur and cur.get('state') == 'OPEN':
                return self.leave_unsure(n)
            print(f'#{n}: auto-merge may be OFF now: STATE stays, the next run finishes it')
            return SILENT
        self.write_state(plan)   # the queue's own disarm: the plan may re-arm it
        if not read:
            return self.leave_off(n, f'{why}, so it was not updated: which head the review vouched for is unclear.',
                                  self.read_advice(why))
        if not fetched:
            return self.leave_off(n, f'GitHub does not have the commit read at {read[:12]} (any more), so it was not '
                                     f'updated. Merge {base} into it yourself and push',
                                  f'(`{self.cfg.push_command}`).')
        now = self.view(n)   # a person may have turned it back on (or closed it) since the disarm
        if now is None:
            print(f'#{n}: gh cannot show it now, so not updating: STATE stays, the next run finishes it')
            return SILENT
        if now.get('state') != 'OPEN' or now.get('autoMergeRequest'):
            # a person armed it again (or closed it), or GitHub shows the queue's own disarm late: which one, this read
            # cannot say. The plan (the queue's own disarm) stays, and the next run decides from what GitHub shows
            # then: armed or closed, nothing to do; off, its own disarm, re-armed as any plan
            print(f'#{n}: armed again or not open right after the queue turned auto-merge off (a person, or GitHub '
                  'showing the disarm late): not updated, STATE stays, the next run decides')
            return SILENT
        u = self.gh_write('pr', 'update-branch', str(n), '--repo', self.repo)
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
            print(f'#{n}: {self.unknown}, so {head[:12]} was not checked: STATE stays, the next run finishes it')
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
            r = self.gh_write('pr', 'comment', str(n), '--repo', self.repo, '--body',
                              f'pr_queue was stopped half-way and may have left auto-merge OFF here; {what}. The queue '
                              'retries every run and touches no other PR until it has re-armed this one or said why not.')
            if r.returncode == 0:
                self.write_state(dict(st, told=True))
        return 1

    def recover(self):
        """finish_plan(), then report_bad(): a plan this run kept as unreadable is named in this run already, and the
        earlier ones are named even when finish_plan() fails (an error, not a kill: that runs nothing more)."""
        try:
            code = self.finish_plan()
        except Exception:
            self.report_bad()
            raise
        self.report_bad()
        return code

    def keep_bad(self):
        """STATE moved aside as an unreadable plan, never over an earlier one (#934): STATE.bad, or STATE.bad-<ns>
        when that name is taken (an older queue's or an earlier run's, kept until a person deletes it). A name
        STATE.told-bad holds is taken too, even with no file left: report_bad() would take a new plan there for the one
        it already told of. The name is made first as an empty file (O_EXCL: never one that is there), then STATE is
        moved onto that file of its own, so no other file is overwritten; a hard link would not do (another user's
        STATE: protected_hardlinks). A run killed between the two leaves STATE and that empty file, which later runs
        name as one more kept plan. Only a run holding the lock moves one. The name it got."""
        told, base = {os.path.basename(t) for t in self.told_bad()}, self.state + BAD
        bad, ns = base, time.time_ns()
        while True:
            if os.path.basename(bad) not in told and not os.path.lexists(bad):
                try:
                    os.close(os.open(bad, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600))
                    break
                except FileExistsError:   # made after the check
                    pass
            bad, ns = f'{base}-{ns}', ns + 1
        try:
            os.replace(self.state, bad)
        except OSError:
            try:
                os.remove(bad)
            except OSError:
                pass
            raise
        sync_dir(bad)
        return bad

    def told_bad(self):
        """The kept plans STATE.told-bad says were told (report_bad); [] for no record, or one the queue did not
        write."""
        try:
            told = json.loads(read_own(self.state + TOLD_BAD) or '[]')
        except ValueError:
            return []
        return [t for t in told if isinstance(t, str)] if isinstance(told, list) else []

    def bad_plans(self):
        """The unreadable plans kept beside STATE (keep_bad: STATE.bad first, then STATE.bad-<ns> oldest first), links
        too, or None when the folder cannot be listed (said)."""
        folder, name = os.path.split(self.state)
        try:
            names = os.listdir(folder)
        except FileNotFoundError:
            return []
        except OSError as e:
            print(f'::warning::pr-queue: could not list {folder} for unreadable plans of earlier runs: {e}')
            return None
        first = [name + BAD] if name + BAD in names else []
        later = sorted((int(f[len(name + BAD) + 1:]), f) for f in names
                       if re.fullmatch(re.escape(name + BAD) + r'-[0-9]+', f))
        return [os.path.join(folder, f) for f in first + [f for _, f in later]]

    def report_bad(self):
        """Every unreadable plan kept beside STATE is named in every run's log as a ::warning:: (shown on the run's
        page on GitHub), and told once per file through notify_command (kind bad-plan; README.md, "Notify"). The
        record of which were told is STATE.told-bad (read_own), written before the command runs, as a PR notice's
        comment is: a stop between the two tells no one, none is told twice. No such file left: the record goes, so a
        later one with the same name is told again. A record that cannot be written tells no one yet: the next run
        tries again, never every run."""
        bads, record = self.bad_plans(), self.state + TOLD_BAD
        if bads is None:
            return
        if not bads:
            try:
                os.remove(record)
            except FileNotFoundError:
                pass
            except OSError as e:
                print(f'could not delete {record}: {e}')
            return
        for bad in bads:
            print(f'::warning::pr-queue: {bad} is an unreadable plan of an earlier run: a PR may still have auto-merge '
                  'OFF. Look at the open PRs without auto-merge, then delete it')
        if not self.cfg.notify_command:
            return
        told = self.told_bad()
        new = [b for b in bads if b not in told]
        if not new:
            return
        try:
            write_text(record, json.dumps(bads))
        except OSError as e:
            print(f'::warning::pr-queue: could not write {record} ({e}): notify_command not run for {", ".join(new)} '
                  'yet, the next run tries again')
            return
        self.notify(0, '', 'bad-plan', f'unreadable plan of an earlier run kept as {", ".join(new)}: a PR may still '
                    'have auto-merge OFF', f'https://github.com/{self.repo}/pulls')

    def finish_plan(self):
        """A run stopped after its disarm left STATE: re-arm the PR at a head that passes step 4's check against the
        review as it is now, or leave it OFF with the comment. A PR that is armed again, merged or closed needs nothing.
        0: done, STATE removed. Otherwise STATE stays and the caller ends the run: another PR's update would overwrite
        it."""
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
            bad = self.keep_bad()
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
        unsure = st.get('unsure') is True   # a plan without it (older queues) is the queue's own disarm
        same = self.same_change(base, n, head, read) if open_off and not cur.get('isDraft') and read and not unsure \
            else False
        old = (st.get('old') or '?')[:12]
        if not open_off:
            code = 0
        elif unsure:
            code = self.leave_unsure(n)
        elif not read:
            code = self.leave_off(n, f'a pr_queue run that was stopped half-way or could not finish turned auto-merge '
                                     f'off at {old}; {why or "it stored no read commit"}, so it was not re-armed: which '
                                     'head the review vouches for is unclear.', self.read_advice(why or ''))
        elif same is None:   # never OFF and silent: say it once on the PR, as for a gh error
            print(f'#{n}: {self.unknown}, so {head[:12]} was not checked: trying again next run, no other PR meanwhile')
            return self.tell_once(st, n, f'{self.unknown}, so it could not check {head[:12]}')
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
            rollup = self.gate_rollup(p.get('statusCheckRollup'), head.lower(), n)
            if rollup is None:
                print(f'skip #{n}: its {self.cfg.head_check} check from the gate App is unknown (see above)')
                continue
            p = {**p, 'statusCheckRollup': rollup}
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
                                      f'updates it by itself once {bad} is green on {base}',
                                      kind='base-red', link=self.check_link(p, bad))
                    continue
            elif bad:
                print(f'skip #{n}: required check {bad} failed (its author fixes it)')
            if bad:
                if not dry:
                    self.tell_red(n, head, bad, link=self.check_link(p, bad))
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
                                      self.stuck_fix(why[1], p.get('baseRefName') or 'its base'),
                                      kind='stuck', link=self.check_link(p, why[0]))
                    continue
                print(f'#{n} is up to date and waiting for its checks: queue is moving')
                return 0
            print(f'update #{n} {(p.get("title") or "")[:70]}')
            return self.update(n, dry)
        print('queue empty')
        return 0


def own_name(path):
    """`path` without what follows its last name (`/`, `/.`): the queue makes its other files by adding to the path
    (CACHE.merge, CACHE.new, CACHE.old-*, STATE.lock, STATE.tmp, STATE.fsck-known, STATE.bad*, STATE.told-bad), and
    after `CACHE/` those would be inside the cache, where set_aside deletes what is in the way. Nothing else is
    changed: a `..` is the kernel's to follow (it may come after a link)."""
    while True:
        head, tail = os.path.split(path)
        if tail not in ('', '.') or head == path:
            return path
        path = head


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
    a.state_file, a.cache = own_name(a.state_file), own_name(a.cache)
    lock = own_name(a.lock_file or a.state_file + '.lock')
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
        elif os.path.basename(path) in ('', '..'):   # / or a path that ends in ..
            problems.append(f'{flag} {path!r} does not end in a name of its own: the queue makes its other files '
                            'beside it, by adding to that name')
        elif work and (os.path.realpath(path) + os.sep).startswith(work + os.sep):
            problems.append(f'{flag} {path} is inside the job\'s workspace, which the next checkout empties')
    try:
        cfg = committed_config(a.config)
    except config.ConfigError as e:
        problems.append(str(e))
    read, write = env.get(READ_TOKEN) or '', env.get(WRITE_TOKEN) or ''
    if not read:
        problems.append(f"{READ_TOKEN} is empty: every read uses the run's GITHUB_TOKEN (the action sets it)")
    if not a.dry_run and not write:
        problems.append(f"{WRITE_TOKEN} is empty: the branch update, auto-merge and comments use the caller's token")
    elif not a.dry_run and write == read:
        problems.append(f"{WRITE_TOKEN} is the run's GITHUB_TOKEN, whose branch updates start no workflow")
    problems += filter(None, [git_problem()])
    if problems:
        print('pr_queue refused to start and touched nothing:\n  ' + '\n  '.join(problems))
        return REFUSED
    q = Queue(a.repo, cfg, a.state_file, a.cache, lock, read_token=read, write_token=None if a.dry_run else write)
    q.warn_if_branches_stay()   # once per run; a warning at most, never the run's result
    return q.main(a.dry_run)


if __name__ == '__main__':
    sys.stdout.reconfigure(line_buffering=True)   # a run stopped half-way still shows what it did
    sys.exit(cli())
