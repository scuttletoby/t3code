"""The three rules: `worktrees`, `docker` and `tmp`. A rule only names what it wants gone and why; whether a path
may go at all is the guard's answer (guard.py), and every rule asks it.

`plan` reads and decides, and changes nothing. `recheck` reads everything about one item again, just before its
delete. `delete` is the only code in the kit that removes anything.
"""
import errno, fnmatch, os, re, stat

from . import compress, guard
from .config import matches
from .facts import (Unreadable, Untouched, default_branch, elsewhere, git, git_code, local_tags, lonely, merged_seconds,
                    origin_url, orphaned_worktree, present, reflog, remote_refs, remote_urls, remove_volume,
                    remove_worktree, worktree_list)
from .guard import CREDENTIAL_NAMES, Refusal

DAY = 86400
# Names in the temp folder no pattern can take: hidden entries (sockets of the desktop), what the system or a
# login session owns, and agent scratch folders (their own rule, which needs the owner's yes, is not built yet).
TMP_NEVER = ('.*', 'claude-*', 'tmux-*', 'ssh-*', 'systemd-private-*', 'snap-private-tmp', 'snap.*', 'lost+found')
ANONYMOUS = re.compile(r'^[0-9a-f]{64}$')       # the name Docker gives a volume nobody named
EMPTY_PRS = {'merged': [], 'open': []}
GITHUB_URL = re.compile(r'^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$')
# In a worktree's own git folder: files the commands of somebody working in it write (`fetch` writes FETCH_HEAD).
USE_MARKS = ('HEAD', 'index', 'ORIG_HEAD', 'FETCH_HEAD', 'COMMIT_EDITMSG')
REFLOG_LINE = re.compile(rb'(?:[0-9a-f]{40}|[0-9a-f]{64}) (?:[0-9a-f]{40}|[0-9a-f]{64}) [^<>\n]*<[^<>\n]*> (\d+) [+-]\d{4}(?:\t[^\n]*)?')
# In a worktree's own git folder: an operation somebody started and did not finish.
MARKS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply', 'index.lock')


class Item:
    """One thing a rule wants gone. `kind` is path, worktree, clone or volume; for a volume `path` is its name."""

    def __init__(self, kind, path, rule, why, size, look=None):
        self.kind, self.path, self.rule, self.why, self.size = kind, path, rule, why, size
        self.done = None        # after a run: deleted, refused or failed
        self.look = look        # the guard's look at the last check: the delete removes only what it saw

    @property
    def label(self):
        return f'docker volume {self.path}' if self.kind == 'volume' else self.path


class Kept:
    """One thing a rule looked at and keeps, and why."""

    def __init__(self, path, rule, code, detail, size=None):
        self.path, self.rule, self.code, self.detail, self.size = path, rule, code, detail, size


class Plan:
    def __init__(self):
        self.items, self.kept = [], []


def days_text(n):
    return f'{n} day' if n == 1 else f'{n} days'


def _entries(root):
    """The names directly in a rule's folder. (None, None): the folder is not there, and so there is nothing to do."""
    try:
        return sorted(os.listdir(root)), None
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as ex:
        return None, Refusal('could not read', f'{root}: {ex.strerror}')


# --- tmp ----------------------------------------------------------------------------------------------------------
def tmp_match(cfg, name):
    """(pattern, days) of the first pattern that takes an entry of the temp folder called `name`; None when no
    pattern matches or no pattern may take it. The disk report's temp class is the entries this takes."""
    if matches(name, TMP_NEVER):
        return None
    match = None
    for pattern, days in cfg.tmp_patterns:
        if fnmatch.fnmatchcase(name, pattern):
            match = (pattern, days)
            break
    return match


def tmp_answer(cfg, facts, path):
    """(why, look) for one entry of the temp folder, or None when the rule does not take it: no pattern matches
    its name, or it changed too recently."""
    match = tmp_match(cfg, os.path.basename(path))
    if match is None:
        return None
    pattern, days = match
    try:
        top = guard.changed(os.lstat(path))
    except FileNotFoundError:
        return None
    except OSError as ex:  # tmp entry
        return '', guard.Look(Refusal('could not read', f'{path}: {ex.strerror}'))
    look = guard.examine(cfg, facts, path, 'path')
    age = facts.now - max(top, look.newest)       # a refusal stops the look early: then the entry's own time counts
    if age < days * DAY:
        return None
    return f'not changed for {days_text(int((age + 1) // DAY))} ({pattern}: {days_text(days)})', look


def plan_tmp(cfg, facts, plan, measure):
    if not cfg.tmp_patterns:
        return
    names, problem = _entries(cfg.tmp_root)
    if problem:
        plan.kept.append(Kept(cfg.tmp_root, 'tmp', problem.code, problem.detail))
    for name in names or ():
        path = os.path.join(cfg.tmp_root, name)
        answer = tmp_answer(cfg, facts, path)
        if answer is None:
            continue
        why, look = answer
        if look.refusal is not None:
            plan.kept.append(Kept(path, 'tmp', look.refusal.code, look.refusal.detail, guard.measure(path) if measure else None))
        else:
            plan.items.append(Item('path', path, 'tmp', why, look.size, look))


# --- deleting -----------------------------------------------------------------------------------------------------
# Every delete goes through descriptors, one name at a time, and never through a link: a name that is a link now is
# removed as a link, and a folder is entered only when it is the folder the name showed a moment before.
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_parent(path):
    """A descriptor of the folder `path` is in, opened from / one name at a time, never through a link."""
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in os.path.dirname(path).split('/'):
            if part:
                new = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _clear(top, names, look, shown):
    """Removes `names` from the open folder `top`, each folder with everything in it. Every entry must be one the
    last check saw (its inode, on its device): a folder moved in since, a new mount or a new file stops the delete
    there, so nothing the check did not judge is removed."""
    stack = [(top, sorted(names, reverse=True), None, shown)]
    try:
        while stack:
            here, left, done, where = stack[-1]
            if not left:
                stack.pop()
                if done is not None:
                    os.close(here)
                    os.rmdir(done[1], dir_fd=done[0])
                continue
            name = left.pop()
            st = os.stat(name, dir_fd=here, follow_symlinks=False)
            if st.st_dev != look.ident[0] or st.st_ino not in look.seen:  # seen
                raise Unreadable(f'{where}/{name}: not there at the last check (moved in, made or mounted since): the delete stopped')
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=here)
                continue
            sub = os.open(name, DIR_FLAGS, dir_fd=here)
            sst = os.fstat(sub)
            if (sst.st_dev, sst.st_ino) != (st.st_dev, st.st_ino):
                os.close(sub)
                raise Unreadable(f'{where}/{name}: changed while it was deleted: the delete stopped')
            stack.append((sub, sorted(os.listdir(sub), reverse=True), (here, name), f'{where}/{name}'))
    finally:
        for here, _, done, _ in stack:
            if done is not None:
                os.close(here)


def remove_tree(path, look, last=None):
    """Removes the file or folder at `path` if it is still what the last check judged (`look.ident`); else raises
    Untouched and removes nothing. A folder is emptied through its descriptor, `last` (a name in it) after
    everything else, and then removed."""
    if look is None or look.ident is None:
        raise Untouched(f'{path}: no last check to compare with: nothing removed')
    try:
        parent = _open_parent(path)
    except OSError as ex:
        raise Untouched(f'{path}: its folder: {ex.strerror}: nothing removed') from None
    try:
        name = os.path.basename(path)
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (st.st_dev, st.st_ino) != look.ident:  # ident
                raise Untouched(f'{path} is not what the last check saw (it was moved, or a link or another folder '
                                'is there now): nothing removed')
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=parent)
                return
            top = os.open(name, DIR_FLAGS, dir_fd=parent)
        except Untouched:
            raise
        except OSError as ex:
            raise Untouched(f'{path}: {ex.strerror}: nothing removed') from None
        try:
            tst = os.fstat(top)
            if (tst.st_dev, tst.st_ino) != look.ident:
                raise Untouched(f'{path} changed as it was opened: nothing removed')
            names = os.listdir(top)
            _clear(top, [n for n in names if n != last], look, path)
            _clear(top, [n for n in names if n == last], look, path)
        finally:
            os.close(top)
        os.rmdir(name, dir_fd=parent)
    finally:
        os.close(parent)


def _still(item):
    """Untouched unless the folder at item.path is still the one the last check judged (never through a link)."""
    try:
        parent = _open_parent(item.path)
    except OSError as ex:
        raise Untouched(f'{item.path}: its folder: {ex.strerror}: nothing removed') from None
    try:
        st = os.stat(os.path.basename(item.path), dir_fd=parent, follow_symlinks=False)
    except OSError as ex:
        raise Untouched(f'{item.path}: {ex.strerror}: nothing removed') from None
    finally:
        os.close(parent)
    if item.look is None or (st.st_dev, st.st_ino) != item.look.ident:
        raise Untouched(f'{item.path} is not what the last check saw (it was moved, or a link or another folder is '
                        'there now): nothing removed')


def delete_path(item):
    remove_tree(item.path, item.look)


# --- docker -------------------------------------------------------------------------------------------------------
def volume_answer(cfg, facts, volume):
    """(why, refusal) for one unused anonymous volume."""
    age = facts.now - volume.created
    if age < cfg.docker_days * DAY:
        return '', Refusal(f'under {cfg.docker_days} days old', f'made {days_text(int(max(age, 0) // DAY))} ago')
    try:
        processes = facts.processes()
    except Unreadable as ex:
        return '', Refusal('could not read', f'the process list ({ex})')
    if volume.mountpoint in processes.busy:
        return '', Refusal('in use', 'a process runs in it, has a file in it open or names it')
    return f'anonymous, no container uses it, made {days_text(int(age // DAY))} ago', None


def plan_docker(cfg, facts, plan, measure):
    kept = plan.kept
    if not cfg.docker_enabled:
        return
    try:
        volumes = facts.dangling()
    except Unreadable as ex:
        kept.append(Kept('docker', 'docker', 'could not read', str(ex)))
        return
    for volume in volumes:
        if not ANONYMOUS.match(volume.name):
            continue                    # a named volume is never touched, and never listed
        why, refusal = volume_answer(cfg, facts, volume)
        if refusal is not None:
            kept.append(Kept(volume.name, 'docker', refusal.code, refusal.detail))
        else:
            plan.items.append(Item('volume', volume.name, 'docker', why, None))


def recheck_volume(cfg, facts, item):
    if not cfg.docker_enabled or not ANONYMOUS.match(item.path):
        return Refusal('not a candidate', 'the docker rule does not take it')
    try:
        names = {volume.name: volume for volume in facts.dangling()}
    except Unreadable as ex:
        return Refusal('could not read', str(ex))
    if item.path not in names:
        return Refusal('in use', 'a container uses it now, or it is gone')
    return volume_answer(cfg, facts, names[item.path])[1]


# --- worktrees ----------------------------------------------------------------------------------------------------
def same_repo(url, github):
    found = GITHUB_URL.match(url.strip())
    return bool(found) and found.group(1).lower() == github.lower()


def numbers(rows):
    return ', '.join(f'#{row["number"]}' for row in rows[:5]) + (' ...' if len(rows) > 5 else '')


def some(names):
    return ', '.join(names[:5]) + (f' and {len(names) - 5} more' if len(names) > 5 else '')


def regenerable(repo, path, name):
    """True when an entry git ignores is build output any tool makes again: a link (it holds nothing), or inside a
    folder whose name is on the repository's list, exactly (the list holds plain folder names, never a pattern, so
    no entry can make every ignored file build output). A credential-looking name is never build output."""
    parts = name.rstrip('/').split('/')
    if guard.matches(parts[-1], CREDENTIAL_NAMES):
        return False
    if os.path.islink(os.path.join(path, name.rstrip('/'))):
        return True
    folders = parts if name.endswith('/') else parts[:-1]
    return any(part in repo.regenerable for part in folders)


def tree_kind(path):
    """'clone' for a folder with a .git folder, 'worktree' for one with a .git file, None for anything else.
    Raises Unreadable when it cannot tell (a folder it may not read)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            return None
        dot = os.lstat(os.path.join(path, '.git'))
    except OSError as ex:
        if ex.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise Unreadable(f'{ex.filename or path}: {ex.strerror}') from None
    return 'clone' if stat.S_ISDIR(dot.st_mode) else 'worktree' if stat.S_ISREG(dot.st_mode) else None


def tree_refusal(cfg, facts, repo, path, kind, prs, out):
    """Why a worktree or clone of the listed repository stays; None when it may go. Raises Unreadable when git
    cannot answer. `out` gets the pull request it is finished by ('why') and the look at its files ('look')."""
    entries = worktree_list(path)
    real = os.path.realpath(path)
    mine = [entry for entry in entries if os.path.realpath(entry['worktree']) == real]
    if len(mine) != 1 or (kind == 'clone') != (mine[0] is entries[0]) or 'bare' in entries[0]:
        raise Unreadable('worktree list: does not name this folder as expected (was it moved?)')
    entry = mine[0]
    if kind == 'clone':
        # A full clone goes only after its worktrees: a later run, when git no longer lists one that is there.
        others = [e['worktree'] for e in entries[1:] if e.get('locked') is not None or os.path.lexists(e['worktree'])]
        if others:
            return Refusal('worktrees hang off it', f'{len(others)}: {some([os.path.basename(o) for o in others])}')
    if entry.get('locked') is not None:
        return Refusal('locked', entry['locked'] or 'no reason given')
    head = entry.get('HEAD', '')
    branch = entry.get('branch', '')
    branch = branch[len('refs/heads/'):] if branch.startswith('refs/heads/') else ''
    if not branch:
        return Refusal('no branch', 'HEAD is detached')
    if not head.strip('0'):
        # The branch has no commit: it was deleted under the worktree, or never had one (`git checkout --orphan`).
        # What is in the worktree (staged files too) is in no commit anywhere.
        return Refusal('no commit', f'{branch} has no commit (deleted under it, or never had one)')
    if repo.branches and not matches(branch, repo.branches):
        return Refusal('branch not listed', f'{branch} matches none of {some(list(repo.branches))}')

    opened = [row for row in prs['open'] if row['headRefName'] == branch]
    if opened:
        return Refusal('open PR', numbers(opened))
    # A branch other pull requests merge into (main, or the lower branch of a stack) is not finished by one of them,
    # and nor is GitHub's default branch, even when no pull request in the list merges into it.
    if branch == default_branch(path):
        return Refusal('base branch', f'{branch} is the default branch on GitHub')
    based = [row for row in prs['open'] + prs['merged'] if row['baseRefName'] == branch]
    if based:
        return Refusal('base branch', f'pull requests merge into {branch}: {numbers(based)}')
    # Only a pull request from this repository: a fork's "main" is not this repository's main.
    merged = [row for row in prs['merged'] if row['headRefName'] == branch and not row['isCrossRepository']]
    if not merged:
        return Refusal('no merged PR', f'no merged pull request of this repository has the branch {branch}')
    here = present(path, [row['headRefOid'] for row in prs['merged'] + prs['open']])
    holding = []
    for row in merged:
        oid = row['headRefOid']
        if oid != head:
            if oid not in here:
                continue                # GitHub's head is a commit this folder does not have: nothing to compare
            if git_code(path, 'merge-base', '--is-ancestor', head, oid) != 0:
                continue
        holding.append(row)
    if not holding:
        return Refusal('commits not in the merged PR', f'HEAD is not in {numbers(merged)}')
    times = [merged_seconds(row) for row in holding]
    if None in times:
        return Refusal('could not read', f'GitHub (no merge time for {numbers([r for r, t in zip(holding, times) if t is None])})')
    age = facts.now - max(times)
    if age < cfg.merged_hours * 3600:
        return Refusal(f'merged under {cfg.merged_hours} h ago', f'{numbers(holding)} merged {int(max(age, 0) // 3600)} h ago')
    out['why'] = f'{numbers(holding)} merged {days_text(int(age // DAY))} ago, nothing in it that is only here'

    changed, ignored = [], []
    fields = git(path, 'status', '--porcelain', '-z', '--untracked-files=normal', '--ignored=matching').split('\0')
    while fields:
        field = fields.pop(0)
        if len(field) < 4:
            continue
        if field[0] in 'RC':
            fields.pop(0)               # a rename or copy is followed by the name it came from
        (ignored if field[:2] == '!!' else changed).append(field[3:])
    if changed:
        return Refusal('uncommitted or untracked', some(changed))
    precious = [name for name in ignored if not regenerable(repo, path, name)]
    if precious:
        return Refusal('ignored files', some(precious))
    # `git status` does not show a change in a file marked skip-worktree (S) or assume-unchanged (lower case).
    tags = [field.split(' ', 1) for field in git(path, 'ls-files', '-v', '-z').split('\0') if field]
    hidden = [field[1] for field in tags if len(field) == 2 and (field[0] == 'S' or field[0].islower())]
    if hidden:
        return Refusal('hidden from git status', some(hidden))
    git_dir = git(path, 'rev-parse', '--absolute-git-dir').strip()
    marks = [mark for mark in MARKS if os.path.lexists(os.path.join(git_dir, mark))]
    if marks:
        return Refusal('unfinished git operation', some(marks))
    own = git(path, 'for-each-ref', '--format=%(refname)', 'refs/worktree', 'refs/bisect', 'refs/rewritten').split()
    if own:
        return Refusal('refs of its own', some(own))
    # A submodule's history is in the git folder (a worktree's own one too), and stays there after `git submodule
    # deinit`: removing the clone or the worktree removes it.
    modules = guard.modules_refusal(path, git_dir)
    if modules is not None:
        return modules

    if kind == 'worktree':
        # Removing a worktree removes its own HEAD log and nothing else of git's: branches, their logs and every
        # other ref stay in the clone. So only a commit that nothing but this HEAD log names would be lost.
        mine = reflog(path, 'HEAD')
        stays = ['--branches', '--tags', '--remotes', *reflog(path, '--branches')]
        alone = lonely(path, mine, stays) if mine else ''
        if alone:  # reflog
            return Refusal('commits only in its reflog', f'{alone[:10]} (made with no branch checked out)')
    else:
        if git_code(path, 'rev-parse', '-q', '--verify', 'refs/stash') == 0:
            return Refusal('stash entries', 'git stash list')
        # Removing a clone removes every branch, tag and log in it. GitHub has a commit when a branch or tag there
        # holds it right now (`git ls-remote`) or a pull request does; the clone's own remote-tracking refs may be
        # stale and do not count.
        local = [url for url in remote_urls(path, 'origin') if not elsewhere(url)]
        if local:  # origin here
            return Refusal('origin on this machine', f'{local[0]}: what it has does not count as on GitHub')
        refs = remote_refs(path, 'origin', '--heads', '--tags')
        has = sorted(here | present(path, refs.values()))
        alone = lonely(path, ['--all', '--reflog'], has)
        if alone:  # clone
            return Refusal('commits GitHub does not have', f'{alone[:10]} (git log --all --reflog)')
        # A tag is more than its commit: an annotated tag has its own message and signature.
        missing = sorted(ref[len('refs/tags/'):] for ref, oid in local_tags(path).items() if refs.get(ref) != oid)
        if missing:
            return Refusal('tags GitHub does not have', some(missing))

    look = guard.examine(cfg, facts, path, kind)
    out['look'] = look
    if look.refusal is not None:
        return look.refusal
    # Somebody may still use it although its pull request merged: an agent's shell between two commands holds no
    # file open. Any change to its files (build output too) or to its own git state says it was used.
    used, what = last_used(path, git_dir, look)
    age = facts.now - used
    if age < cfg.used_hours * 3600:
        return Refusal(f'used under {cfg.used_hours} h ago', f'{what} changed {int(max(age, 0) // 3600)} h ago')
    return None


def last_used(path, git_dir, look):
    """(time, what) of the newest sign of use: a change to anything in the folder (the guard's look, which walks
    all of it, a clone's git folder too), to the git files a command in it writes (USE_MARKS), or the newest time
    written in its own HEAD reflog. The reflog file's own change time does not count: `git gc` rewrites it.
    Raises Unreadable when a reflog line cannot be read."""
    used, what = look.newest, 'a file in it'
    for name in USE_MARKS:
        try:
            when = guard.changed(os.lstat(os.path.join(git_dir, name)))
        except FileNotFoundError:
            continue                    # most are there only after the command that writes them
        except OSError as ex:
            raise Unreadable(f'{name} in its git folder: {ex.strerror}') from None
        if when > used:
            used, what = when, f'git {name}'
    when = reflog_newest(os.path.join(git_dir, 'logs', 'HEAD'))
    if when > used:
        used, what = when, 'its HEAD reflog'
    return used, what


def reflog_newest(path):
    """The newest time a reflog file names; 0 when there is no such file."""
    try:
        with open(path, 'rb') as f:
            lines = f.read().split(b'\n')
    except FileNotFoundError:
        return 0.0
    except OSError as ex:
        raise Unreadable(f'{path}: {ex.strerror}') from None
    newest = 0.0
    for number, line in enumerate(lines, 1):
        found = REFLOG_LINE.fullmatch(line)
        if found:
            newest = max(newest, float(found.group(1)))
        elif line or number < len(lines):  # the text ends with a newline: only the last piece may be empty
            raise Unreadable(f'line {number} of its HEAD reflog is no reflog line')
    return newest


def tree_of(repo, path):
    """'worktree' or 'clone' when `path` is one of the listed repository (by its origin URL), the folders this rule
    looks at; None for anything else. Raises Unreadable when it cannot tell. The disk report's worktrees class.
    An orphaned worktree in the repository's folder counts as one (tree_answer keeps it under that name)."""
    kind = tree_kind(path)
    if kind == 'worktree' and orphaned_worktree(path) is not None:
        return kind
    return kind if kind is not None and same_repo(origin_url(path), repo.github) else None


def tree_answer(cfg, facts, repo, path):
    """(kind, refusal, why, look) for one entry of a repository's folder; None when it is not a worktree or clone
    of that repository, and so not this rule's."""
    try:
        kind = tree_kind(path)
    except Unreadable as ex:
        return 'folder', Refusal('could not read', str(ex)), '', None
    if kind is None:
        return None
    gone = orphaned_worktree(path) if kind == 'worktree' else None
    if gone is not None:            # whose it is cannot be read: it is in this repository's folder, and kept
        return kind, Refusal('orphaned worktree', f'its git folder {gone} is gone (the main repository was removed)'), '', None
    try:
        url = origin_url(path)
    except Unreadable as ex:
        return kind, Refusal('could not read', f'git: {ex}'), '', None  # whose it is
    if not same_repo(url, repo.github):
        return None
    try:
        prs = facts.prs(repo.github)
    except Unreadable as ex:
        return kind, Refusal('could not read', str(ex)), '', None  # GitHub
    out = {}
    try:
        refusal = tree_refusal(cfg, facts, repo, path, kind, prs, out)
    except Unreadable as ex:
        refusal = Refusal('could not read', f'git: {ex}')
    return kind, refusal, out.get('why', ''), out.get('look')


def plan_worktrees(cfg, facts, plan, measure):
    for repo in cfg.repos:
        names, problem = _entries(repo.root)
        if problem:
            plan.kept.append(Kept(repo.root, 'worktrees', problem.code, problem.detail))
        for name in names or ():
            path = os.path.join(repo.root, name)
            answer = tree_answer(cfg, facts, repo, path)
            if answer is None:
                continue
            kind, refusal, why, look = answer
            if refusal is not None:
                plan.kept.append(Kept(path, 'worktrees', refusal.code, refusal.detail, guard.measure(path) if measure else None))
            else:
                plan.items.append(Item(kind, path, 'worktrees', why, look.size, look))


def recheck_tree(cfg, facts, item):
    for repo in cfg.repos:
        if repo.root == os.path.dirname(item.path):
            answer = tree_answer(cfg, facts, repo, item.path)
            if answer is None or answer[0] != item.kind:
                return Refusal('not a candidate', f'no longer a {item.kind} of {repo.github}')
            item.look = answer[3]
            return answer[1]
    return Refusal('not a candidate', 'no repository in the machine file has this folder')


def delete_worktree(item):
    """Git removes it, without --force: git refuses on its own when a file changed after the last check, and then
    raises Untouched (nothing removed). The local branch stays in the clone. The folder must still be the one the
    last check judged, and git itself removes a worktree only when its .git file points back at its own entry in
    the clone, so a link or another folder swapped in is refused."""
    _still(item)
    try:
        common = git(item.path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    except Unreadable as ex:
        raise Untouched(f'could not read: {ex}: nothing removed') from None
    _still(item)
    remove_worktree(common, item.path)


def delete_clone(item):
    """The working files first and .git last, so a clone that is only half gone still has its history. The engine
    notes the delete before it starts; a later run names a half-gone clone "half deleted" (engine.py)."""
    remove_tree(item.path, item.look, last='.git')


# --- all rules ----------------------------------------------------------------------------------------------------
# The rules, by the name each one writes on what it plans: the one list of them. The disk report (report.py) needs
# a class for each (config.CLASSES), and report.test.py fails for a rule here that has none.
RULES = {'worktrees': plan_worktrees, 'docker': plan_docker, 'tmp': plan_tmp, compress.RULE: compress.plan_compress}
# Kinds whose step never leaves a path half gone (compress.py: the original or its checked copy is always there),
# so the journal of deletes in progress does not name them. WORDS: what the engine says of them (plan, run, done)
# and NOUNS what it counts them as.
UNJOURNALED = ('compress', 'part')
WORDS = {'compress': ('would compress', 'compressing', 'compressed')}
NOUNS = {'compress': 'file', 'part': 'part file'}


def plan(cfg, facts, measure=False):
    """What every rule wants gone (items) and what it looked at and keeps (kept). `measure` also sizes the kept."""
    out = Plan()
    for planner in RULES.values():
        planner(cfg, facts, out, measure)
    return out


def recheck(cfg, facts, item):
    """Everything about one item, read again just before its delete: None when it may still go."""
    if item.kind == 'volume':
        return recheck_volume(cfg, facts, item)
    if item.kind in ('worktree', 'clone'):
        return recheck_tree(cfg, facts, item)
    if item.kind in ('compress', 'part'):
        return compress.recheck(cfg, facts, item)
    if os.path.dirname(item.path) != cfg.tmp_root:
        return Refusal('not a candidate', 'not in the temp folder of the machine file')
    answer = tmp_answer(cfg, facts, item.path)
    if answer is None:
        return Refusal('not a candidate', 'it changed, or the rule no longer takes it')
    item.look = answer[1]
    return answer[1].refusal


def delete(item):
    {'path': delete_path, 'worktree': delete_worktree, 'clone': delete_clone, 'volume': lambda i: remove_volume(i.path),
     'compress': compress.compress, 'part': compress.remove_part}[item.kind](item)
