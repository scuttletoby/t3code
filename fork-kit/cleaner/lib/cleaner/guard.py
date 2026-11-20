"""The hard refusals: what the cleaner never deletes, whatever a rule or the machine file says.

Every rule asks `examine` about each path before it plans a delete, and the engine asks again just before the
delete itself. A path may go only when every check here passes; a check that cannot get its answer refuses with
"could not read". Nothing here follows a symbolic link, and nothing here changes a file. The in-use checks
(processes, units, locks) come last, after the slow walk and git, so they see the machine as late as possible.
"""
import fnmatch, os, stat

from .config import REGENERABLE, must_keep_match
from .facts import (Processes, Unreadable, elsewhere, git, git_code, local_tags, lonely, mount_id_of, orphan_doubt,
                    orphaned_worktree, own_mounts, present, remote_refs, remote_urls, remotes, worktree_list)

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
    """What `examine` found: the refusal (None: it may go), the size on disk and the newest change in it. `ident`
    is (device, inode) of the path itself as the check saw it, and `seen` the inodes of everything in it: the delete
    removes only what has these (rules.remove_tree)."""

    def __init__(self, refusal=None, size=0, newest=0.0):
        self.refusal, self.size, self.newest = refusal, size, newest
        self.newest_file = 0.0      # the newest change of anything in it that is not a folder (a delete changes folders)
        self.keep_first = []        # agent-scratch: (relative path, size) of what is copied to the archive first
        self.keep_named = []        # agent-scratch: (relative path, size) of the outputs too large to copy: named only
        self.ident, self.seen = None, set()
        self.linked = set()         # the inodes in `seen` of files with several hard links
        self.checked = None         # agent-scratch: when the last liveness check began (rules.clock); see _clear
        self.orphans = []           # tmp: (where, its gone git folder) of each orphaned worktree passed over in it


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


def is_device(st):
    return stat.S_ISBLK(st.st_mode) or stat.S_ISCHR(st.st_mode)


def _keep_reason(keep, dir_fd=None):
    """The first line of a KEEP file (`keep`: in the open folder `dir_fd` when given); 'no reason given' when it is
    empty or cannot be read."""
    try:
        fd = os.open(keep, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dir_fd)
        with os.fdopen(fd, 'rb') as f:
            line = f.read(300).decode('utf-8', 'replace').strip().splitlines()
    except OSError:
        line = []
    return line[0].strip() if line and line[0].strip() else 'no reason given'


BARE = ('HEAD', 'objects', 'refs')     # a bare repository: all three in one folder
ABOVE_LIMIT = 1000                   # folders the walk above may read; more (a loop of mounts?) refuses
# Local filesystems whose mount root tells the whole story: a mount of one shows the folder `root` of the one
# filesystem the device holds, so the folders above that folder are read where another mount shows them, or the
# walk refuses. Every other type keeps the folder unless its entry says `layered`: an overlay or a union (its upper
# and lower folders), bindfs and other FUSE mounts that show folders of another filesystem, an encrypting stack
# (ecryptfs, gocryptfs), a network or VM share (nfs, cifs, smb3, 9p, virtiofs, sshfs, rclone: the folders above the
# export are on the far side), an automount point that is not mounted (autofs), and every type nobody listed here.
# Their mount root is `/` or says nothing about this machine, so a work tree's `.git` or a KEEP file above what they
# show would be missed.
LOCAL = ('ext2', 'ext3', 'ext4', 'xfs', 'btrfs', 'bcachefs', 'f2fs', 'zfs', 'jfs', 'nilfs2', 'tmpfs', 'devtmpfs',
         'ramfs', 'vfat', 'msdos', 'exfat', 'ntfs', 'ntfs3', 'hfsplus', 'fuseblk')
# Filesystems whose folders do not have the device number mountinfo names (btrfs and bcachefs: one per subvolume;
# overlay: its own scheme). On every other type the two must be the same.
OTHER_DEVICES = ('btrfs', 'bcachefs', 'overlay')


def marker(here, fd=None):
    """What marks the folder `here` as one compress-cold and dedupe leave whole, with everything under it: ('KEEP
    file', its reason) for an entry named KEEP of any kind (a file, a link, a folder), ('git folder', None) for a
    `.git` of any kind (a folder; a file of a linked worktree or a submodule) or a bare repository (`HEAD`, `objects`
    and `refs`), None for neither. One check, the same in the folders above a configured folder and in and under
    it: each name is looked up (lstat, never followed), so on a folder that folds case `keep` counts too. With `fd`
    (the folder opened with O_PATH), the names are looked up in that open folder, not by the path again: the
    markers read are those of the folder the caller checked. Raises OSError when a name cannot be looked up: that
    is not "not there"."""
    def at(name):
        return _lstat_or_none(name, fd) if fd is not None else _lstat_or_none(os.path.join(here, name))
    st = at(KEEP_FILE)
    if st is not None:
        if stat.S_ISDIR(st.st_mode):
            return 'KEEP file', 'a folder named KEEP'
        return 'KEEP file', _keep_reason(KEEP_FILE, fd) if fd is not None else _keep_reason(os.path.join(here, KEEP_FILE))
    if at('.git') is not None:
        return 'git folder', None
    if all(at(n) is not None for n in BARE):
        return 'git folder', None
    return None


def _open_folder(here):
    """`here` opened to be named, not read (O_PATH). O_NOFOLLOW: a link put in its place after the caller's lstat
    is opened as the link itself, and O_DIRECTORY then refuses it (ENOTDIR), so the folder checked is never the
    one a link points to. Raises OSError."""
    return os.open(here, os.O_PATH | os.O_NOFOLLOW | os.O_DIRECTORY | os.O_CLOEXEC)


def _where(fd):
    """(mount id, device, inode) of the open folder `fd` as the kernel gives them. Raises OSError or Unreadable."""
    st = os.fstat(fd)
    return mount_id_of(fd), st.st_dev, st.st_ino


def _where_now(here):
    """(mount id, device, inode) of the folder `here` as the kernel finds it now, never through a link. Raises
    OSError or Unreadable."""
    fd = _open_folder(here)
    try:
        return _where(fd)
    finally:
        os.close(fd)


def _mount_refusal(path, here, mounts, index, fd=None):
    """When the list is this process's own (`mounts.own`): why the folder `here` is not on the mount the list says
    holds it (line `index`), now; else None. The mount id the kernel gives the folder must be that line's, and so
    must its device where the type gives folders the device mountinfo names (not OTHER_DEVICES). A mount taken
    away, put over it or moved after the list was read, so the folder read is not the one the list names, keeps
    it. `fd`: the folder already open (_open_folder); else it is opened by its path now. Returns (refusal, what
    the kernel gave: compare it after the read)."""
    if not mounts.own:
        return None, None
    try:
        now = _where_now(here) if fd is None else _where(fd)
    except OSError as ex:
        return Refusal('could not read', f'{here}: {ex.strerror}'), None
    except Unreadable as ex:
        return Refusal('could not read', f'the mount {here} is on ({ex})'), None
    want, kind, device = mounts.id_of(index), mounts.type_of(index), mounts.lines[index][1]
    if (want is None or now[0] != want
            or (kind is None or kind not in OTHER_DEVICES) and f'{os.major(now[1])}:{os.minor(now[1])}' != device):
        return Refusal('could not read', f'{here} is not on the mount the mount list names ({mounts.lines[index][0]}, '
                                         f'{device}) now: the list is old, or a mount was taken away or put over it, '
                                         f'so what is above {path} is not known'), None
    return None, now


def above_refusal(path, mounts, layered=False):
    """A KEEP file or a git folder (`marker`) in a folder above the configured folder `path`: everything under it is
    kept, as below it (a work tree's files are git's; a KEEP file keeps its whole folder). None when there is none.
    The rules that only look from a configured folder down (compress-cold, dedupe) ask this at the plan and again
    at their last checks.

    "Above" is every folder above it up to `/`, and also every folder above it on its own filesystem: a mount that
    shows one folder of a filesystem (a bind mount, a btrfs subvolume) hides the folders above that one, and git
    still sees the files there. Those are read where another mount of the same filesystem (`mounts.lines`) shows
    them. When no mount here shows them, it refuses ("could not read"), except above a whole btrfs subvolume
    (`mounts.whole`) that sits directly in the btrfs top folder (its path has one part: `/@`, `/@home`): that top
    folder holds the subvolumes, not a user's files. A subvolume deeper down (`/@/home`, `/srv/repo/data`) has
    ordinary folders above it, which are read like any other or refuse. A folder above that cannot be read refuses
    too, and so does a place the mount list names that is not a folder now (the list is old, or another mount
    covers it): its markers cannot be read there.

    Every folder the walk passes must be on a local filesystem whose mount root tells the whole story (LOCAL); on
    any other type (an overlay, a FUSE or network share, an unknown type) the folders above what it shows are not
    seen, so it refuses. `layered` (the folder's `layered` key) takes that refusal away for a folder its owner has
    checked; every other check still holds.

    With the process's own mount list (`mounts.own`), every folder the walk passes is also checked against it, once
    before its markers are read and once after: the kernel must find it on the mount the list says holds it, and
    find the same folder both times (`_mount_refusal`). Its markers are read through the folder opened for the
    first check, so they are that folder's. A mount taken away, put over a folder or moved after the list was read
    keeps it. Mount ids are used again once freed, so at the end the list is read once more (`own_mounts`): each
    folder must still be held by the same line (mount point, device, root, type, id and parent id); any change, a
    mount with a reused id among them, keeps it."""
    stack, seen, used = [(path, False)], set(), []
    while stack:
        here, look = stack.pop()
        if (here, look) in seen:
            continue
        seen.add((here, look))
        if len(seen) > ABOVE_LIMIT:
            return Refusal('could not read', f'more than {ABOVE_LIMIT} folders above {path} through its mounts')
        index = mounts.held(here)
        if index is None:
            return Refusal('could not read', f'no mount in the mount list holds {here}')
        used.append((here, index))
        point, device, root = mounts.lines[index]
        kind = mounts.type_of(index)
        if kind not in LOCAL and not layered:
            shown = 'a filesystem of no known type' if kind is None else kind
            return Refusal('could not read', f'{here} is on {shown} at {point}, which is not a local filesystem whose '
                                             f'mount root shows all of it (an overlay, a FUSE or network share): a KEEP '
                                             'file or a git folder above what it shows could not be seen (name the '
                                             'folder with "layered": true once you have checked them)')
        if look:
            try:
                hst = os.lstat(here)
            except OSError as ex:
                return Refusal('could not read', f'{here}: {ex.strerror}')
            if not stat.S_ISDIR(hst.st_mode):
                return Refusal('could not read', f'{here} is a link or not a folder now, where '
                                                 f'the mount list shows a folder above {path}')
        refusal = _read_folder(path, here, mounts, index, look)
        if refusal is not None:
            return refusal
        if here == point and root != '/':
            # `here` shows the folder `root` of its filesystem: the folder above that one is above it too.
            if not root.startswith('/'):
                return Refusal('could not read', f'{here} shows {root} of {device}, not a folder')
            up = os.path.dirname(root)
            places = mounts.shown_at(device, up)
            if not places and not ((device, root) in mounts.whole and up == '/'):
                return Refusal('could not read', f'{here} shows the folder {root} of its filesystem ({device}), and no '
                                                 f'mount here shows the folder above it ({up}): a KEEP file or a git '
                                                 'folder there could not be seen')
            stack.extend((place, True) for place in reversed(places))
        if here != '/':
            stack.append((os.path.dirname(here), True))
    return _still_true(path, mounts, used)


def _read_folder(path, here, mounts, index, look):
    """The kernel check of one folder of the walk (`_mount_refusal`) and, for a folder above (`look`), its markers,
    read through the folder that check opened, then the check again by its path: the refusal, or None."""
    fd = None
    if mounts.own:
        try:
            fd = _open_folder(here)
        except OSError as ex:
            return Refusal('could not read', f'{here}: {ex.strerror}')
    try:
        refusal, before = _mount_refusal(path, here, mounts, index, fd)
        if refusal is not None or not look:
            return refusal
        try:
            found = marker(here, fd)
        except OSError as ex:
            return Refusal('could not read', f'{here}: {ex.strerror}')
        if found is not None and found[0] == 'KEEP file':
            return Refusal('KEEP file', f'{found[1]} ({here}, above {path})')
        if found is not None:
            return Refusal('git folder', f'{here}, above {path}: git needs its files as they are')
        refusal, after = _mount_refusal(path, here, mounts, index)
        if refusal is not None:
            return refusal
        if after != before:
            return Refusal('could not read', f'{here} changed while its markers were read (another folder or '
                                             f'mount is there now): what is above {path} is not known')
        return None
    finally:
        if fd is not None:
            os.close(fd)


def _still_true(path, mounts, used):
    """With the process's own list: each folder of `used` ((folder, index of its line)) is held, in the list read
    again now, by a line with the same mount point, device, root, type, mount id and parent id. A mount id the
    kernel gave again to a new mount (it hands out the lowest free one) matches the old line's id but not its root
    or device; a mount moved, put over or taken away changes which line holds a folder. Any of that keeps it."""
    if not mounts.own:
        return None
    try:
        again = own_mounts()
    except Unreadable as ex:
        return Refusal('could not read', f'the mount list, again ({ex})')

    def line(of, i):
        return of.lines[i], None if of.ids is None else of.ids[i], of.type_of(i)
    for here, index in used:
        now = again.held(here)
        if now is None or line(again, now) != line(mounts, index):
            return Refusal('could not read', f'the mount list changed while the folders above {path} were read '
                                             f'(at {here}): what is above it is not known')
    return None


def _lstat_or_none(path, dir_fd=None):
    """The lstat of `path` (in the open folder `dir_fd` when given), None when nothing is there; any other error is
    raised (it is not "nothing there")."""
    try:
        return os.lstat(path, dir_fd=dir_fd)
    except FileNotFoundError:
        return None


def _unpushed(where, shown, bare):
    """A refusal when a commit or a tag of this git folder is on no other machine. A remote counts only with a
    live answer (`git ls-remote`), never by the folder's own remote-tracking refs, which go stale when a branch is
    deleted or force-pushed there; a remote that is a folder on this machine does not count at all."""
    has, tags = set(), {}
    for name in remotes(where, bare):
        # Judged by the URLs git really uses (after the user's insteadOf rules), each of them.
        if not all(elsewhere(url) for url in remote_urls(where, name, bare)):
            continue
        refs = remote_refs(where, name, bare=bare)
        has.update(refs.values())
        for ref, oid in refs.items():
            tags.setdefault(ref, set()).add(oid)
    alone = lonely(where, ['--all', '--reflog'], sorted(present(where, has, bare)), bare)
    if alone:  # tmp
        return Refusal('git: unpushed commits', f'{shown}: {alone[:10]} (git log --all --reflog)')
    missing = sorted(ref[len('refs/tags/'):] for ref, oid in local_tags(where, bare).items() if oid not in tags.get(ref, ()))
    if missing:
        return Refusal('git: tags not pushed', f'{shown}: {", ".join(missing[:5])}')
    return None


def module_repos(git_dir):
    """The repositories git keeps under <git_dir>/modules: each submodule's own history, which stays there after
    `git submodule deinit` has emptied the submodule's folder. Never through a link. Raises Unreadable when a folder
    there cannot be read, or `modules` is not a plain folder."""
    top = os.path.join(git_dir, 'modules')
    try:
        st = os.lstat(top)
    except FileNotFoundError:
        return []
    except OSError as ex:
        raise Unreadable(f'{top}: {ex.strerror}') from None
    if not stat.S_ISDIR(st.st_mode):
        raise Unreadable(f'{top} is a link or not a folder: the submodules in it are not judged')
    out, stack = [], [top]
    while stack:
        here = stack.pop()
        try:
            with os.scandir(here) as it:
                entries = [entry for entry in it if entry.is_dir(follow_symlinks=False) or entry.name == 'HEAD']
        except FileNotFoundError:
            continue
        except OSError as ex:
            raise Unreadable(f'{ex.filename or here}: {ex.strerror}') from None
        if {'HEAD', 'objects', 'refs'} <= {entry.name for entry in entries}:
            out.append(here)
            stack.append(os.path.join(here, 'modules'))       # its own submodules
            continue
        stack.extend(entry.path for entry in entries if entry.name != 'HEAD')
    return sorted(out)


def modules_refusal(path, git_dir):
    """For every submodule repository under a git folder (rules.py asks for a clone's and a worktree's own): does
    it hold anything that exists only here?"""
    try:
        found = module_repos(git_dir)
    except Unreadable as ex:
        return Refusal('could not read', f'git: {ex}')
    for where in found:
        refusal = _git_refusal(path, where, True)
        if refusal is not None:
            return refusal
    return None


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
        return _unpushed(where, shown, bare)
    except Unreadable as ex:
        return Refusal('could not read', f'git in {shown}: {ex}')


def _in_use(facts, path, inodes):
    """The last checks, so that they see the machine as late as possible: processes, systemd units, file locks."""
    try:
        processes = facts.processes()
    except Unreadable as ex:
        return Refusal('could not read', f'the process list ({ex})')
    if processes.uses(path):
        return Refusal('in use', 'a process runs in it, has a file in it open or names it')
    try:
        units = facts.units()
    except Unreadable as ex:
        return Refusal('could not read', f'a systemd unit ({ex})')
    if path in units:
        return Refusal('in use', 'a systemd unit names it')
    held = [shown for ino, shown in inodes if ino in processes.locks]
    if held:  # locks
        return Refusal('in use', f'a process holds a lock on {held[0]}')
    return None


def _check(cfg, facts, path, kind, look, orphans):
    root = os.path.dirname(path)
    if not (root in cfg.roots() or cfg.session_parent(root)) or os.path.normpath(path) != path:
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
    try:
        mounts = facts.mounts()
    except Unreadable as ex:
        return Refusal('could not read', f'the mount list ({ex})')
    inside = sorted(m for m in mounts.points if m == path or m.startswith(path + '/'))
    if inside:  # bind mounts have the device number of what they show
        return Refusal('another filesystem', f'{os.path.relpath(inside[0], root)} is a mount point')
    source = mounts.bound(path, st.st_dev)
    if source is not None:  # bind source
        return Refusal('in use', f'a bind mount shows {source} of its filesystem somewhere else (a container or a sandbox)')

    kept = must_keep_match(cfg.must_keep, path)
    if kept is not None:
        # Equal, inside (the path is longer) or a holder (the path is shorter): all three are kept.
        return Refusal('must keep', '/' + '/'.join(kept))
    parts = tuple(path.split('/')[1:])
    for part in parts:
        if kept_name(cfg, part):
            return Refusal('must keep', part)
    name = parts[-1]
    if kind == 'path':             # a worktree is named after its branch, and "fix-token-refresh" holds no token
        if matches(name, CREDENTIAL_NAMES):
            return Refusal('credential name', name)
        if matches(name, LOCK_NAMES):
            return Refusal('lock file', name)
    look.size, look.newest = st.st_blocks * 512, changed(st)
    look.ident = (st.st_dev, st.st_ino)
    inodes = [(st.st_ino, name)]
    if not stat.S_ISDIR(st.st_mode):
        return _in_use(facts, path, inodes)

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
            if plain and entry.name == KEEP_FILE and not stat.S_ISDIR(est.st_mode):
                return Refusal('KEEP file', f'{_keep_reason(entry.path)} ({shown})' if here != path else _keep_reason(entry.path))
            if names and matches(entry.name, CREDENTIAL_NAMES):
                return Refusal('credential name', shown)
            inodes.append((est.st_ino, shown))
            look.seen.add(est.st_ino)
            look.newest = max(look.newest, changed(est))
            if not stat.S_ISDIR(est.st_mode):
                look.newest_file = max(look.newest_file, changed(est))
            twice = est.st_nlink > 1 and not stat.S_ISDIR(est.st_mode)          # a hard link counts once
            if not twice or (est.st_dev, est.st_ino) not in counted:
                look.size += est.st_blocks * 512
            if twice:
                counted.add((est.st_dev, est.st_ino))
                look.linked.add(est.st_ino)
            if is_device(est):
                return Refusal('not a file or folder', f'{shown} is a device')
            if plain and entry.name == '.git':
                if not (stat.S_ISDIR(est.st_mode) or stat.S_ISREG(est.st_mode)):
                    # A link to a git folder elsewhere: git work hangs off it that this check cannot see whole.
                    return Refusal('could not read', f'{shown} is a symbolic link or not a folder: its git folder is not judged')
                gits.append((here, False))
            is_git = plain and entry.name == '.git'
            if stat.S_ISDIR(est.st_mode):
                quiet = is_git or entry.name in REGENERABLE
                stack.append((entry.path, names and not quiet, plain and not is_git))

    if kind == 'path':
        for where, bare in gits:
            gone = None if bare else orphaned_worktree(where)
            if gone is not None:
                # A worktree whose git folder is not there. Only when that folder is provably gone (not moved, not
                # on a drive that is not mounted now: orphan_doubt) did its commits, index and HEAD go with it, so
                # the folder holds files and no commit (#519). The tmp rule (`orphans`) then judges it as a plain
                # folder; any other asker keeps it under its own name, no error, as no run will change it.
                shown = os.path.relpath(where, path)
                if not orphans:
                    return Refusal('orphaned worktree', f'{shown}: its git folder {gone} is gone')
                try:
                    doubt = orphan_doubt(where, gone, mounts, [repo.root for repo in cfg.repos])
                except Unreadable as ex:
                    return Refusal('could not read', f'{shown}: whether its git folder {gone} is gone ({ex})')
                if doubt is not None:
                    return Refusal('unknown parent', f'{shown}: its git folder {gone} is not there, and may be '
                                                     f'elsewhere: {doubt}')
                look.orphans.append((os.path.relpath(where, path), gone))
                continue
            refusal = _git_refusal(path, where, bare)
            if refusal is None and (bare or os.path.isdir(os.path.join(where, '.git'))):
                refusal = modules_refusal(path, where if bare else os.path.join(where, '.git'))
            if refusal is not None:
                return refusal
    else:
        # A worktree or a clone: the rule has checked its own git folder; any other one inside is not the rule's.
        nested = [os.path.relpath(where, path) for where, bare in gits if bare or where != path]
        if nested:
            return Refusal('another git folder inside', ', '.join(sorted(nested)[:3]))
    return _in_use(facts, path, inodes)


def examine(cfg, facts, path, kind='path', orphans=False):
    """Look at one path a rule wants to delete. `kind` is 'path' (a file or folder of no known kind: every git
    folder in it must be clean and pushed), 'worktree' or 'clone' (the worktrees rule has asked git itself).
    `orphans` (the tmp rule): a worktree in it whose git folder is provably gone holds no commit and is passed over
    (see `orphan_doubt`)."""
    look = Look()
    look.refusal = _check(cfg, facts, path, kind, look, orphans)
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
