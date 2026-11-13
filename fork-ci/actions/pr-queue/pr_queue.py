#!/usr/bin/env python3
"""A merge queue for a repository whose branch ruleset requires pull requests to be up to date with their base
(README.md). Each run brings exactly ONE waiting PR up to date: the oldest open, non-draft PR with auto-merge on that is
BEHIND its base. Its CI runs, it merges itself, and the run after the next push to the base moves the queue on. An
armed PR that is already up to date holds the queue while its required checks run.

Auto-merge only ever merges a head someone checked. Whether GitHub checks --match-head-commit again after the head
moved is not documented, and "armed" does not say at which head (a plain push keeps auto-merge on), so the queue relies
on neither. For the PR it updates (head H1):
  1. R = the full sha the review vouched for (lens.Review.reviewed_head): the one commit every lens's newest `read at:`
     names, or a newer override comment's; no lens comments: R = H1; lenses that read different commits: R unknown.
     The base branch, the PR head and R are fetched into the queue's own bare repository (--cache).
  2. The plan goes into STATE (--state-file), then auto-merge OFF; if that fails, nothing else happens (if gh's error
     leaves it unclear whether it is OFF, STATE stays for the next run). R unknown or not on GitHub: the PR stays OFF,
     is not updated and gets a comment, so one such PR cannot stall the queue. A git fetch error changes nothing: the
     PR stays as it was and the next run tries again.
  3. `gh pr update-branch`, then wait until the head moves (at most wait_seconds).
  4. Auto-merge back ON with --match-head-commit H2 only if H2 holds exactly R's change on top of the base (holds()).
     Anything else (a push after the lenses, a conflict resolution, any gh or git error): it stays OFF with a comment
     that it needs a lens read and the ready command. No new head in time: STATE stays, the next run checks it.
A PR already up to date never reaches these steps: the project's head_check (if any) covers its head.
Stopped half-way, the next run finishes that PR first from STATE (recover), against the review as it is then, and
touches no other PR until it has re-armed it or said on it why not: a PR is never OFF and silent.
Exit codes: 0 done; 1 a step failed, the next run tries again; 2 refused to start, nothing touched; 3 a PR may be OFF
and no comment says so yet (STATE stays, the next run finishes it).
    pr_queue.py --repo OWNER/NAME --config FILE --state-file ABS --cache ABS [--lock-file ABS] [--dry-run]
"""
import argparse, fcntl, json, os, re, shutil, subprocess, sys, time

sys.dont_write_bytecode = True   # no __pycache__ next to the scripts
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config  # noqa: E402
import lens  # noqa: E402

SHA = re.compile(r'[0-9a-f]{40}')
REPO_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]*/[A-Za-z0-9._-]+')
GONE_RE = re.compile(r"not our ref|unadvertised object|couldn't find remote ref|no such remote ref", re.I)
WAITS_FOR = ('BLOCKED', 'UNSTABLE', 'CLEAN', 'HAS_HOOKS')   # up-to-date merge states whose checks the queue waits for
UPDATES = 3   # holds() follows at most this many queue updates in a row
GIT_MIN = (2, 38)   # git merge-tree --write-tree
REFUSED, SILENT = 2, 3
# git gets the login from gh (GH_TOKEN, or gh's own login), never from a credential helper of the machine's git config.
GIT_AUTH = ('-c', 'credential.https://github.com.helper=',
            '-c', 'credential.https://github.com.helper=!gh auth git-credential')
VIEW = 'number,state,isDraft,autoMergeRequest,mergeStateStatus,headRefOid,baseRefName,comments,reviews,commits'
LIST = 'number,title,isDraft,autoMergeRequest,mergeStateStatus,createdAt,statusCheckRollup,headRefOid,baseRefName'


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
    written before the disarm is on disk before the disarm."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + '.tmp'
    with open(tmp, 'w') as f:
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
        self.review = lens.Review(cfg.bot_logins, cfg.override_prefix, cfg.override_association)

    def gh(self, *a):
        return subprocess.run(['gh', *a], text=True, capture_output=True)

    def git(self, *a):
        return subprocess.run(['git', *GIT_AUTH, '--git-dir', self.cache, *a], text=True, capture_output=True,
                              env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'})

    # --- step 1 and 4: the queue's own git repository -------------------------------------------------------------
    def own_cache(self):
        """The cache, made on first use (`git init --bare`; the first fetch brings the objects). One that is not a bare
        git repository (emptied, half-deleted) is set aside and made again, never trusted. Every failure on the way is
        said and returns False (fetch then knows nothing, so nothing is done), never an exception."""
        if os.path.isdir(self.cache):
            if self.git('rev-parse', '--is-bare-repository').stdout.strip() == 'true':
                # A fetch killed half-way leaves a ref lock that fails every later fetch. Only the queue uses the cache,
                # and only while it holds the lock: a lock file here is always a stale one.
                for d, _, files in os.walk(os.path.join(self.cache, 'refs', 'queue')):
                    for f in files:
                        if f.endswith('.lock'):
                            os.remove(os.path.join(d, f))
                return True
            print(f'{self.cache} is not a bare git repo: rebuilding it')
        parent, name = os.path.split(self.cache)
        os.makedirs(parent, exist_ok=True)
        remove_old(*(os.path.join(parent, f) for f in sorted(os.listdir(parent))   # CACHE.old-*, CACHE.new.old-*
                     if f.startswith(name + '.') and '.old-' in f))
        new = self.cache + '.new'
        if not (set_aside(self.cache) and set_aside(new)):   # a broken cache; one made half-way
            return False
        r = subprocess.run(['git', 'init', '-q', '--bare', new], text=True, capture_output=True)
        if r.returncode != 0:
            print(f'could not make {self.cache}: {r.stderr.strip()[:300]}')
            return False
        try:
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

    # --- steps 2-4 --------------------------------------------------------------------------------------------------
    def view(self, n):
        r = self.gh('pr', 'view', str(n), '--repo', self.repo, '--json', VIEW)
        try:
            return json.loads(r.stdout) if r.returncode == 0 else None
        except ValueError:
            return None

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

    def arm(self, n, head):
        return self.gh('pr', 'merge', str(n), '--repo', self.repo, '--auto', f'--{self.cfg.merge_method}',
                       *(['--delete-branch'] if self.cfg.delete_branch else []), '--match-head-commit', head)

    def update(self, n, dry=False):
        """Bring PR n up to date with its base and re-arm only the change the review read (steps 1-4)."""
        cur = self.view(n)
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
                                  'A comment that looks like a lens report counts only with a lens header as its first '
                                  f'plain line, not in a code block ({self.cfg.lens_format_doc}): re-post it that way, '
                                  'or someone who read the head posts the override '
                                  f'`{self.cfg.override_prefix} <sha>: <why>`.' if lens.NO_HEADER in why else '')
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
            return self.leave_off(n, f'turning auto-merge back on for {head[:12]} failed: {r.stderr.strip()[:200]}')
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
        try:
            with open(self.state) as f:
                st = json.load(f)
        except FileNotFoundError:
            return 0
        except (OSError, ValueError):
            st = {}
        n = st.get('pr') if isinstance(st, dict) else None
        if not isinstance(n, int) or isinstance(n, bool):   # which PR it was is lost: never quietly
            os.replace(self.state, bad)
            sync_dir(bad)
            print(f'ERROR: unreadable {self.state}, kept as {bad}: a PR may have auto-merge OFF and nothing says so')
            return 1
        if st.get('repo', self.repo) != self.repo:   # a plan without `repo` is an older queue's, for this repository
            print(f'ERROR: {self.state} is the plan of a queue run for {st["repo"]}, not {self.repo}: two repositories '
                  'share one --state-file. Nothing touched; give each its own')
            return 1
        cur = self.view(n)
        if cur is None:
            print(f'#{n}: a stopped run left it OFF, and gh cannot show it now: trying again next run, no other PR '
                  'meanwhile')
            return self.tell_once(st, n, 'gh cannot show this PR right now')
        head = (cur.get('headRefOid') or '').lower()
        open_off = cur.get('state') == 'OPEN' and not cur.get('autoMergeRequest')
        # The review as it is now, not the read the stopped run stored: a lens posted since then counts. No lens
        # comments: the stored read, the head that run started from.
        reviewed, read, why = self.review.reviewed_head(cur)
        read = read if reviewed else st.get('read')
        same = self.same_change(st.get('base'), n, head, read) if open_off and not cur.get('isDraft') and read else False
        old = (st.get('old') or '?')[:12]
        if not open_off:
            code = 0
        elif not read:
            code = self.leave_off(n, f'a pr_queue run that was stopped half-way or could not finish turned auto-merge '
                                     f'off at {old}; {why or "it stored no read commit"}, so it was not re-armed: which '
                                     'head the review vouches for is unclear.')
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
                                         f'{head[:12]} (the change read at {read[:12]}) failed: {r.stderr.strip()[:200]}')
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
        for p in sorted(prs, key=lambda p: p.get('createdAt') or ''):
            n = p['number']
            if p.get('isDraft') or not p.get('autoMergeRequest'):
                continue
            if p.get('mergeStateStatus') != 'BEHIND':
                if p.get('mergeStateStatus') not in WAITS_FOR:
                    print(f'skip #{n}: merge state {p.get("mergeStateStatus")}, not one the queue updates or waits for')
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
