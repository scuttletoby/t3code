"""The three rules: `worktrees`, `docker` and `tmp`. A rule only names what it wants gone and why; whether a path
may go at all is the guard's answer (guard.py), and every rule asks it.

`plan` reads and decides, and changes nothing. `recheck` reads everything about one item again, just before its
delete. `delete` is the only code in the kit that removes anything.
"""
import errno, fnmatch, os, re, shutil, stat

from . import guard
from .config import matches
from .facts import (Unreadable, default_branch, git, git_code, local_tags, lonely, merged_seconds, origin_url, present, reflog,
                    remote_refs, remove_volume, remove_worktree, worktree_list)
from .guard import CREDENTIAL_NAMES, Refusal

DAY = 86400
# Names in the temp folder no pattern can take: hidden entries (sockets of the desktop), what the system or a
# login session owns, and agent scratch folders (their own rule, which needs the owner's yes, is not built yet).
TMP_NEVER = ('.*', 'claude-*', 'tmux-*', 'ssh-*', 'systemd-private-*', 'snap-private-tmp', 'snap.*', 'lost+found')
ANONYMOUS = re.compile(r'^[0-9a-f]{64}$')       # the name Docker gives a volume nobody named
EMPTY_PRS = {'merged': [], 'open': []}
GITHUB_URL = re.compile(r'^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$')
# In a worktree's own git folder: an operation somebody started and did not finish.
MARKS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply', 'index.lock')


class Item:
    """One thing a rule wants gone. `kind` is path, worktree, clone or volume; for a volume `path` is its name."""

    def __init__(self, kind, path, rule, why, size):
        self.kind, self.path, self.rule, self.why, self.size = kind, path, rule, why, size
        self.done = None        # after a run: deleted, refused or failed

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
def tmp_answer(cfg, facts, path):
    """(why, look) for one entry of the temp folder, or None when the rule does not take it: no pattern matches
    its name, or it changed too recently."""
    name = os.path.basename(path)
    if matches(name, TMP_NEVER):
        return None
    match = None
    for pattern, days in cfg.tmp_patterns:
        if fnmatch.fnmatchcase(name, pattern):
            match = (pattern, days)
            break
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
            plan.items.append(Item('path', path, 'tmp', why, look.size))


def delete_path(item):
    if stat.S_ISDIR(os.lstat(item.path).st_mode):
        shutil.rmtree(item.path)
    else:
        os.unlink(item.path)


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


def plan_docker(cfg, facts, plan):
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
    return look.refusal


def tree_answer(cfg, facts, repo, path):
    """(kind, refusal, why, size) for one entry of a repository's folder; None when it is not a worktree or clone
    of that repository, and so not this rule's."""
    try:
        kind = tree_kind(path)
    except Unreadable as ex:
        return 'folder', Refusal('could not read', str(ex)), '', 0
    if kind is None:
        return None
    try:
        url = origin_url(path)
    except Unreadable as ex:
        return kind, Refusal('could not read', f'git: {ex}'), '', 0  # whose it is
    if not same_repo(url, repo.github):
        return None
    try:
        prs = facts.prs(repo.github)
    except Unreadable as ex:
        return kind, Refusal('could not read', str(ex)), '', 0  # GitHub
    out = {}
    try:
        refusal = tree_refusal(cfg, facts, repo, path, kind, prs, out)
    except Unreadable as ex:
        refusal = Refusal('could not read', f'git: {ex}')
    return kind, refusal, out.get('why', ''), out['look'].size if 'look' in out else 0


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
            kind, refusal, why, size = answer
            if refusal is not None:
                plan.kept.append(Kept(path, 'worktrees', refusal.code, refusal.detail, guard.measure(path) if measure else None))
            else:
                plan.items.append(Item(kind, path, 'worktrees', why, size))


def recheck_tree(cfg, facts, item):
    for repo in cfg.repos:
        if repo.root == os.path.dirname(item.path):
            answer = tree_answer(cfg, facts, repo, item.path)
            if answer is None or answer[0] != item.kind:
                return Refusal('not a candidate', f'no longer a {item.kind} of {repo.github}')
            return answer[1]
    return Refusal('not a candidate', 'no repository in the machine file has this folder')


def delete_worktree(item):
    """Git removes it, without --force: git refuses on its own when a file changed after the last check, and then
    raises Untouched (nothing removed). The local branch stays in the clone."""
    common = git(item.path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    remove_worktree(common, item.path)


def delete_clone(item):
    """The working files first and .git last, so a clone that is only half gone still has its history. The engine
    notes the delete before it starts; a later run names a half-gone clone "half deleted" (engine.py)."""
    for name in sorted(os.listdir(item.path)):
        if name != '.git':
            target = os.path.join(item.path, name)
            if stat.S_ISDIR(os.lstat(target).st_mode):
                shutil.rmtree(target)
            else:
                os.unlink(target)
    shutil.rmtree(os.path.join(item.path, '.git'))
    os.rmdir(item.path)


# --- all rules ----------------------------------------------------------------------------------------------------
def plan(cfg, facts, measure=False):
    """What every rule wants gone (items) and what it looked at and keeps (kept). `measure` also sizes the kept."""
    out = Plan()
    plan_worktrees(cfg, facts, out, measure)
    plan_docker(cfg, facts, out)
    plan_tmp(cfg, facts, out, measure)
    return out


def recheck(cfg, facts, item):
    """Everything about one item, read again just before its delete: None when it may still go."""
    if item.kind == 'volume':
        return recheck_volume(cfg, facts, item)
    if item.kind in ('worktree', 'clone'):
        return recheck_tree(cfg, facts, item)
    if os.path.dirname(item.path) != cfg.tmp_root:
        return Refusal('not a candidate', 'not in the temp folder of the machine file')
    answer = tmp_answer(cfg, facts, item.path)
    if answer is None:
        return Refusal('not a candidate', 'it changed, or the rule no longer takes it')
    return answer[1].refusal


def delete(item):
    {'path': delete_path, 'worktree': delete_worktree, 'clone': delete_clone, 'volume': lambda i: remove_volume(i.path)}[item.kind](item)
