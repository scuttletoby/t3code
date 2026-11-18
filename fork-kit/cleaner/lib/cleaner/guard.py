"""The hard refusals: what the cleaner never deletes, whatever a rule or the machine file says.

Every rule asks `examine` about each path before it plans a delete, and the engine asks again just before the
delete itself. A path may go only when every check here passes; a check that cannot get its answer refuses with
"could not read". Nothing here follows a symbolic link, and nothing here changes a file.
"""
import fnmatch, os, stat

from .config import REGENERABLE
from .facts import Processes, Unreadable, git, git_code, worktree_list

LOCK_NAMES = ('*.lock', 'lock', '.lock', '*.lck')
# Names that look like they hold a key, a token or a password. Compared without regard to upper and lower case.
CREDENTIAL_NAMES = ('.env', '.env.*', '*.env', 'id_rsa*', 'id_dsa*', 'id_ecdsa*', 'id_ed25519*', '*.pem', '*.key',
                    '*.p12', '*.pfx', '*.jks', '*.keystore', '*token*', '*secret*', '*credential*', '*password*',
                    '*passwd*', '.netrc', '.pypirc', '.git-credentials', '*.kdbx')
KEEP_FILE = 'KEEP'
EMPTY = Processes()


class Refusal:
    """Why a path stays: a short code that is the same for every path kept for that reason, and the detail."""

    def __init__(self, code, detail=''):
        self.code, self.detail = code, detail

    def __repr__(self):
        return f'Refusal({self.code!r}, {self.detail!r})'


class Look:
    """What `examine` found: the refusal (None: it may go), the size on disk and the newest change in it."""

    def __init__(self, refusal=None, size=0, newest=0.0):
        self.refusal, self.size, self.newest = refusal, size, newest


def changed(st):
    """When an entry last changed: the later of its content time and its inode time. A copy or an unpacked archive
    keeps old content times, but its inode times say when it arrived."""
    return max(st.st_mtime, st.st_ctime)


def device(st):
    return st.st_dev


def owner(st):
    return st.st_uid


def matches(name, globs):
    name = name.lower()
    return any(fnmatch.fnmatchcase(name, glob) for glob in globs)


def kept_name(cfg, name):
    return matches(name, cfg.must_keep_names)


def _keep_reason(path):
    """The first line of the KEEP file directly in a folder; None when there is no such file."""
    keep = os.path.join(path, KEEP_FILE)
    if not os.path.lexists(keep):
        return None
    try:
        fd = os.open(keep, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, 'rb') as f:
            line = f.read(300).decode('utf-8', 'replace').strip().splitlines()
    except OSError:
        line = []
    return line[0].strip() if line and line[0].strip() else 'no reason given'


def _git_refusal(path, where, bare):
    """For a git folder inside a path a rule wants to delete: does it hold anything that exists only here?"""
    shown = os.path.relpath(where, path)
    try:
        if not bare and git(where, 'status', '--porcelain', '--untracked-files=normal'):
            return Refusal('git: uncommitted or untracked', shown)
        if git_code(where, 'rev-parse', '-q', '--verify', 'refs/stash', bare=bare) == 0:
            return Refusal('git: stash entries', shown)
        worktrees = worktree_list(where, bare)
        if len(worktrees) > 1:
            return Refusal('git: worktrees', f'{shown} has, or is, a linked worktree')
        if git(where, 'rev-list', '--max-count=1', '--all', '--reflog', '--not', '--remotes', bare=bare):
            return Refusal('git: unpushed commits', shown)
    except Unreadable as ex:
        return Refusal('could not read', f'git in {shown}: {ex}')
    return None


def _check(cfg, facts, path, kind, look):
    root = os.path.dirname(path)
    if os.path.dirname(path) not in cfg.roots() or os.path.normpath(path) != path:
        return Refusal('not in a configured root', 'only what is directly in a configured folder may go')
    try:
        st, rst = os.lstat(path), os.lstat(root)
    except OSError as ex:
        return Refusal('could not read', f'{ex.filename or path}: {ex.strerror}')
    if stat.S_ISLNK(st.st_mode):
        return Refusal('symbolic link', 'a link is never followed and never removed')
    if os.path.realpath(root) != root:
        return Refusal('symbolic link', f'{root} is reached through a link')
    if not (stat.S_ISDIR(st.st_mode) or stat.S_ISREG(st.st_mode)):
        return Refusal('not a file or folder', 'a socket, pipe or device')
    uid, dev = os.getuid(), device(rst)
    if owner(st) != uid:
        return Refusal('another owner', f'uid {owner(st)}')
    if device(st) != dev:
        return Refusal('another filesystem', 'a mount point')

    parts = tuple(path.split('/')[1:])
    for pattern in cfg.must_keep:
        # Equal, inside (the path is longer) or a holder (the path is shorter): all three are kept.
        if all(fnmatch.fnmatchcase(part, glob) for part, glob in zip(parts, pattern)):
            return Refusal('must keep', '/' + '/'.join(pattern))
    for part in parts:
        if kept_name(cfg, part):
            return Refusal('must keep', part)
    name = parts[-1]
    if kind == 'path':             # a worktree is named after its branch, and "fix-token-refresh" holds no token
        if matches(name, CREDENTIAL_NAMES):
            return Refusal('credential name', name)
        if matches(name, LOCK_NAMES):
            return Refusal('lock file', name)
    folder = stat.S_ISDIR(st.st_mode)
    keep = _keep_reason(path) if folder else None
    if keep is not None:
        return Refusal('KEEP file', keep)

    try:
        processes = facts.processes()
    except Unreadable as ex:
        return Refusal('could not read', f'the process list ({ex})')
    if path in processes.busy:
        return Refusal('in use', 'a process runs in it, has a file in it open or names it')
    try:
        units = facts.units()
    except Unreadable as ex:
        return Refusal('could not read', f'a systemd unit ({ex})')
    if path in units:
        return Refusal('in use', 'a systemd unit names it')

    look.size, look.newest = st.st_blocks * 512, changed(st)
    if not folder:
        if st.st_ino in processes.locks:
            return Refusal('in use', 'a process holds a lock on it')
        return None

    # The whole tree, never through a link. `names`: credential-looking names count (not below a git folder or a
    # folder of installed packages, where a file called "token.js" is somebody's code). `plain`: not in a git folder.
    gits, counted = [], set()
    stack = [(path, kind == 'path', True)]
    while stack:
        here, names, plain = stack.pop()
        try:
            with os.scandir(here) as it:
                entries = list(it)
        except OSError as ex:
            return Refusal('could not read', f'{ex.filename or here}: {ex.strerror}')  # a folder
        found = {entry.name for entry in entries}
        if plain and {'HEAD', 'objects', 'refs'} <= found:          # a bare repository
            gits.append((here, True))
            names = plain = False
        for entry in entries:
            shown = os.path.relpath(entry.path, path)
            try:
                est = entry.stat(follow_symlinks=False)
            except OSError as ex:
                return Refusal('could not read', f'{shown}: {ex.strerror}')
            if owner(est) != uid:
                return Refusal('another owner', f'{shown} (uid {owner(est)})')
            if device(est) != dev:
                return Refusal('another filesystem', f'{shown} is a mount point')
            if kept_name(cfg, entry.name):
                return Refusal('must keep', shown)
            if names and matches(entry.name, CREDENTIAL_NAMES):
                return Refusal('credential name', shown)
            if entry.inode() in processes.locks:
                return Refusal('in use', f'a process holds a lock on {shown}')
            look.newest = max(look.newest, changed(est))
            twice = est.st_nlink > 1 and not stat.S_ISDIR(est.st_mode)          # a hard link counts once
            if not twice or (est.st_dev, est.st_ino) not in counted:
                look.size += est.st_blocks * 512
            if twice:
                counted.add((est.st_dev, est.st_ino))
            if stat.S_ISBLK(est.st_mode) or stat.S_ISCHR(est.st_mode):
                return Refusal('not a file or folder', f'{shown} is a device')
            is_git = plain and entry.name == '.git' and not stat.S_ISLNK(est.st_mode)
            if is_git:
                gits.append((here, False))
            if stat.S_ISDIR(est.st_mode):
                quiet = is_git or entry.name in REGENERABLE
                stack.append((entry.path, names and not quiet, plain and not is_git))

    if kind == 'path':
        for where, bare in gits:
            refusal = _git_refusal(path, where, bare)
            if refusal is not None:
                return refusal
        return None
    # A worktree or a clone: the rule has checked its own git folder; any other one inside is not the rule's to judge.
    nested = [os.path.relpath(where, path) for where, bare in gits if bare or where != path]
    if nested:
        return Refusal('another git folder inside', ', '.join(sorted(nested)[:3]))
    return None


def examine(cfg, facts, path, kind='path'):
    """Look at one path a rule wants to delete. `kind` is 'path' (a file or folder of no known kind: every git
    folder in it must be clean and pushed), 'worktree' or 'clone' (the worktrees rule has asked git itself)."""
    look = Look()
    look.refusal = _check(cfg, facts, path, kind, look)
    return look


def refusal(cfg, facts, path, kind='path'):
    return examine(cfg, facts, path, kind).refusal


def measure(path):
    """The size on disk of a file or folder, never through a link; what cannot be read counts as nothing."""
    try:
        total = os.lstat(path).st_blocks * 512
    except OSError:
        return 0
    counted, stack = set(), [path]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    try:
                        est = entry.stat(follow_symlinks=False)
                    except OSError:
                        continue
                    if stat.S_ISDIR(est.st_mode):
                        stack.append(entry.path)
                    elif est.st_nlink > 1:
                        if (est.st_dev, est.st_ino) in counted:
                            continue
                        counted.add((est.st_dev, est.st_ino))
                    total += est.st_blocks * 512
        except OSError:
            continue
    return total
