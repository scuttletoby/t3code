"""The `compress-cold` rule: a file in a configured folder that nobody has changed or read for that folder's days
is replaced by a zstd copy of itself (`<name>.zst`), so it keeps its content and takes less room (#487, #468).

It deletes no content. The original goes only after its copy is written, synced, read back by `zstd -t` and
decompressed again to the same bytes (sha256 and length of the whole content: for a tar archive that covers every
file in it, not just their number). The copy is written under a hidden part name in the same folder and gets the
file's name plus `.zst` only once it is checked, with a hard link that never replaces a file; the original is
removed after that. So at every moment the original or a checked copy is there, never neither:

- killed while writing: the part file stays beside the original; the next run removes it and compresses again;
- killed between the link and the removal: both are there; the next run checks the copy against the original
  again and only then removes the original;
- a file that is in use, changed, or opened at any point is kept as it was; a disk too full for a whole copy
  plus `keepFreeGB` is refused before anything is written; zstd missing keeps everything ("could not read").

What it never takes, in a configured folder: links (never followed), folders with a KEEP file, git folders, other
filesystems and mount points, other owners' files, credential and lock names, databases with a journal beside them,
hard-linked files, files already compressed, and files under MIN_BYTES. It also skips, with a line in the plan, what
it could not finish: a file its owner may not read, an immutable or append-only file or folder, a file whose group,
ACL or extended attributes the copy could not keep, and every folder on a filesystem mounted `noatime` (reads are
not seen there, so "not read" cannot be known). The machine file can only name folders and days
(config.COMPRESS_SPEC); config.py refuses folders the rule may never take.
"""
import errno, fcntl, hashlib, os, re, secrets, shutil, stat, struct, subprocess, tempfile, threading

from . import guard
from .config import COMPRESS_NEVER_NAMES, matches
from .facts import Unreadable, Untouched
from .guard import CREDENTIAL_NAMES, LOCK_NAMES, KEEP_FILE, Refusal

RULE = 'compress-cold'
DAY = 86400
ZSTD = 'zstd'                       # found on PATH; tests point it at a stub or a missing name
LEVEL = ('-q', '-10', '-T1')        # one thread: the machine's other work comes first
# Already compressed: skipped, so a second run finds nothing new to do.
DONE = ('*.zst', '*.gz', '*.tgz', '*.xz', '*.txz', '*.bz2', '*.tbz2', '*.lz4', '*.lzma', '*.zip', '*.7z', '*.br',
        '*.rar', '*.jar', '*.whl')
# The pieces of a database that live beside it: a file with one of these, or one of these, stays as it is.
SIDECARS = ('-wal', '-shm', '-journal')
MIN_BYTES = 16 * 1024               # smaller files free too little to be worth a copy
PART = '.cleaner-part'
PART_NAME = re.compile(r'^\.(.+)\.zst\.[0-9a-f]{8}' + re.escape(PART) + '$')
NAME_MAX = 255
SAMPLE, SAMPLES = 64 * 1024, 16 * 1024 * 1024   # the plan's estimate reads this much of each file, this much in all
CHUNK = 1024 * 1024
READ = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK | getattr(os, 'O_NOATIME', 0)
# Extended attributes a copy can carry, set by the file's owner: the user's own and the POSIX ACL. A file with any
# other (security.*, trusted.*) is skipped: the copy would lose it.
COPYABLE_XATTRS = ('user.*', 'system.posix_acl_access')
# chattr flags (linux/fs.h) that make the original's removal fail: the file or its folder is skipped.
FS_IOC_GETFLAGS = (2 << 30) | (struct.calcsize('l') << 16) | (ord('f') << 8) | 1
FS_IMMUTABLE_FL, FS_APPEND_FL = 0x10, 0x20
STUCK_FLAGS = ((FS_IMMUTABLE_FL, 'immutable'), (FS_APPEND_FL, 'append-only'))


def deadline(size):
    """Seconds one zstd step may take for a file of `size` bytes: ten minutes plus one second per MiB, far more than
    zstd needs. A zstd that hangs would else hold the run lock for good; one that runs out of time is stopped, its
    copy removed, and the original stays."""
    return 600 + size / (1 << 20)


def flags(path):
    """The chattr flags of the file or folder at `path` (0 when the filesystem has none)."""
    fd = os.open(path, READ)
    try:
        got = fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack('i', 0))
    except OSError as ex:
        if ex.errno in (errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            return 0
        raise
    finally:
        os.close(fd)
    return struct.unpack('i', got[:4])[0]


def _stuck(path):
    """A Refusal when the file or folder at `path` is immutable or append-only (its removal would fail every run)."""
    try:
        got = flags(path)
    except OSError as ex:
        return Refusal('could not read', f'{path}: its flags: {ex.strerror}')
    for flag, word in STUCK_FLAGS:
        if got & flag:
            return Refusal(word, f'{path} is {word} (chattr): its original could not be removed')
    return None


def xattrs(target):
    """The names of the extended attributes of an open file or a path (none when the filesystem has none)."""
    try:
        return os.listxattr(target, **({} if isinstance(target, int) else {'follow_symlinks': False}))
    except OSError as ex:
        if ex.errno in (errno.ENOTSUP, errno.EOPNOTSUPP):
            return []
        raise


def free_bytes(fd):
    """Bytes the user may still write on the filesystem of the open folder `fd`."""
    vfs = os.fstatvfs(fd)
    return vfs.f_bavail * vfs.f_frsize


def zstd():
    """The zstd program, or None when it is not installed."""
    return shutil.which(ZSTD)


def human(n):
    from .engine import human as engine_human
    return engine_human(n)


def last_touched(st):
    """When a file was last changed or read: its content, inode and access times (guard.changed and the atime)."""
    return max(guard.changed(st), st.st_atime)


def _days(n):
    return f'{n} day' if n == 1 else f'{n} days'


# --- what may be compressed ---------------------------------------------------------------------------------------
def _folder_refusal(cfg, facts, folder, here, st, names):
    """Why the folder `here` (inside the cold folder, or it) and everything in it stays; None when it may be looked
    into. `st` is its lstat, `names` its entries."""
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return Refusal('symbolic link', f'{here} is a link or not a folder: never followed')
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'{here} (uid {guard.owner(st)})')
    if here != folder.path:
        if st.st_dev != folder.dev:
            return Refusal('another filesystem', f'{here} is a mount point')
        if matches(os.path.basename(here), CREDENTIAL_NAMES):
            return Refusal('credential name', here)
        if matches(os.path.basename(here).lower(), COMPRESS_NEVER_NAMES):
            return Refusal('log archive', f'{here}: a log archive stays as it is (#468)')
    try:
        points = facts.mounts().points
    except Unreadable as ex:
        return Refusal('could not read', f'the mount list ({ex})')
    if here != folder.path and here in points:
        return Refusal('another filesystem', f'{here} is a mount point')
    if KEEP_FILE in names:
        return Refusal('KEEP file', f'{guard._keep_reason(os.path.join(here, KEEP_FILE))} ({here})')
    if '.git' in names or {'HEAD', 'objects', 'refs'} <= set(names):
        return Refusal('git folder', f'{here}: git keeps its own objects compressed, and git needs its files as they are')
    return _stuck(here)


def _in_use(facts, folder, path, st):
    try:
        processes = facts.processes()
    except Unreadable as ex:
        return Refusal('could not read', f'the process list ({ex})')
    if processes.uses(path):
        return Refusal('in use', 'a process has it open or names it')
    if st.st_ino in processes.locks:
        return Refusal('in use', 'a process holds a lock on it')
    try:
        units = facts.units()
    except Unreadable as ex:
        return Refusal('could not read', f'a systemd unit ({ex})')
    here = path
    while True:
        if here in units:
            return Refusal('in use', f'a systemd unit names {here}')
        if here == folder.path or here == '/':
            return None
        here = os.path.dirname(here)


def _file_answer(cfg, facts, folder, path, st, names):
    """For one regular file in a cold folder: None when the rule does not take it (already compressed, small, not
    cold), a Refusal when it is kept, or the reason it may be compressed."""
    name = os.path.basename(path)
    if matches(name, DONE) or name.endswith(PART) or st.st_size < MIN_BYTES:
        return None
    age = facts.now - last_touched(st)
    if age < folder.days * DAY:
        return None
    if matches(name, CREDENTIAL_NAMES):
        return Refusal('credential name', name)
    if matches(name, LOCK_NAMES):
        return Refusal('lock file', name)
    if name.endswith(SIDECARS) or any(name + s in names for s in SIDECARS):
        return Refusal('database', 'a database file with its journal beside it: the two stay together, as they are')
    if st.st_nlink > 1:
        return Refusal('hard-linked', f'{st.st_nlink} names: a copy would take room the other names still hold')
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'uid {guard.owner(st)}')
    if len(os.fsencode(name)) + len('.zst.12345678' + PART) + 1 > NAME_MAX:
        return Refusal('name too long', 'no room for the name of its copy')
    refusal = _keepable(path, st)
    if refusal is not None:
        return refusal
    refusal = _in_use(facts, folder, path, st)
    if refusal is not None:
        return refusal
    return f'not changed or read for {_days(int((age + 1) // DAY))} ({folder.path}: {_days(folder.days)})'


def _keepable(path, st):
    """A Refusal when the rule could not finish with this file of the user's own: it may not read it, its removal
    would fail, or its copy could not keep its group, ACL or extended attributes. Each would fail every run; it is
    skipped and named instead."""
    if not st.st_mode & stat.S_IRUSR:
        return Refusal('unreadable', f'its owner may not read it (mode {stat.S_IMODE(st.st_mode):04o}): it stays as it is')
    if st.st_gid != os.getegid() and st.st_gid not in os.getgroups():
        return Refusal('group not kept', f'its group (gid {st.st_gid}) is not one of the user\'s: its copy could not keep it')
    try:
        names = xattrs(path)
    except OSError as ex:
        return Refusal('could not read', f'its extended attributes: {ex.strerror}')
    other = [n for n in names if not matches(n, COPYABLE_XATTRS)]
    if other:
        return Refusal('attributes not kept', f'{", ".join(sorted(other))}: its copy could not keep them')
    return _stuck(path)


def _twin(path):
    """(twin, refusal): twin is the lstat of `<path>.zst` when it is a regular file of the user's own with one name
    (a copy an earlier run linked and did not finish with), None when the name is free; refusal when the name is
    something else."""
    try:
        st = os.lstat(path + '.zst')
    except FileNotFoundError:
        return None, None
    except OSError as ex:
        return None, Refusal('could not read', f'{path}.zst: {ex.strerror}')
    if stat.S_ISREG(st.st_mode) and st.st_nlink == 2 and guard.owner(st) == os.getuid() and _linked_part(path, st):
        return None, Refusal('name taken', f'{os.path.basename(path)}.zst is the copy of a run that stopped after naming '
                                           'it; its part file goes in this run, and the next run checks the copy '
                                           'against the original and removes the original')
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or guard.owner(st) != os.getuid():
        return None, Refusal('name taken', f'{os.path.basename(path)}.zst is there and is no copy this rule made')
    return st, None


def _linked_part(path, st):
    """True when a part file of `path` beside it is the same file as `st` (a run stopped between the link and the
    part's removal)."""
    folder, name = os.path.split(path)
    try:
        names = os.listdir(folder)
    except OSError:
        return False
    for other in names:
        found = PART_NAME.match(other)
        if found is None or found.group(1) != name:
            continue
        try:
            pst = os.lstat(os.path.join(folder, other))
        except OSError:
            continue
        if (pst.st_dev, pst.st_ino) == (st.st_dev, st.st_ino):
            return True
    return False


def _look(st):
    look = guard.Look(size=st.st_blocks * 512, newest=guard.changed(st))
    look.ident = (st.st_dev, st.st_ino)
    look.stamp = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return look


def _part_answer(folder, path, names):
    """For a part file a run left: the reason to remove it, or a Refusal when its original is not beside it."""
    found = PART_NAME.match(os.path.basename(path))
    if found is None:
        return None
    original = found.group(1)
    if original in names or original + '.zst' in names:
        return 'a copy an earlier run stopped writing (the original or its checked copy is still there)'
    return Refusal('part without its original', f'neither {original} nor {original}.zst is beside it: left as it is')


def _item(cfg, facts, folder, path, st, names):
    """(Item, None), (None, Kept) or (None, None) for one regular file."""
    from .rules import Item, Kept
    if os.path.basename(path).endswith(PART):
        answer = _part_answer(folder, path, names)
        if answer is None:
            return None, None
        if isinstance(answer, Refusal):
            return None, Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512)
        if guard.owner(st) != os.getuid():
            return None, Kept(path, RULE, 'another owner', f'uid {guard.owner(st)}', st.st_blocks * 512)
        refusal = _in_use(facts, folder, path, st)
        if refusal is not None:
            return None, Kept(path, RULE, refusal.code, refusal.detail, st.st_blocks * 512)
        item = Item('part', path, RULE, answer, st.st_blocks * 512, _look(st))
        item.folder = folder
        return item, None
    answer = _file_answer(cfg, facts, folder, path, st, names)
    if answer is None:
        return None, None
    if not isinstance(answer, Refusal):
        twin, taken = _twin(path)
        free = None if twin is not None or taken is not None else _free(os.path.dirname(path))
        if taken is not None:
            answer = taken
        elif free is not None and free < st.st_size + cfg.keep_free:
            answer = Refusal('disk nearly full', f'{human(free)} free: a whole copy and keepFreeGB '
                                                 f'({human(cfg.keep_free)}) do not fit; the original stays')
    if isinstance(answer, Refusal):
        return None, Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512)
    item = Item('compress', path, RULE, answer, st.st_blocks * 512, _look(st))
    item.folder, item.twin, item.estimate = folder, twin, None
    return item, None


def _free(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return free_bytes(fd)
    finally:
        os.close(fd)


def _start(cfg, facts, folder):
    """The cold folder's own check: (stat, None) when it may be walked, (None, Refusal) when it is kept whole,
    (None, None) when it is not there."""
    path = folder.path
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None, None
    except OSError as ex:
        return None, Refusal('could not read', f'{path}: {ex.strerror}')
    if os.path.realpath(path) != path:
        return None, Refusal('symbolic link', f'{path} is, or is reached through, a link: never followed')
    try:
        mounts = facts.mounts()
    except Unreadable as ex:
        return None, Refusal('could not read', f'the mount list ({ex})')
    bound = mounts.bound(path, st.st_dev)
    if bound is not None:
        return None, Refusal('in use', f'a bind mount shows {bound} of its filesystem somewhere else (a container or a sandbox)')
    # relatime (the usual default) updates the access time at the first read after a change, and at least once a
    # day: with days >= 7 (config.MIN_COLD_DAYS) a file read in that time always looks read. noatime never does.
    options = mounts.options_of(path)
    if options is None:
        return None, Refusal('could not read', f'the mount options of {path}: no mount holds it')
    if 'noatime' in options:
        return None, Refusal('noatime', 'its filesystem is mounted noatime, so reads are never seen and "not read" '
                                        'cannot be known: nothing in it is compressed (mount it relatime)')
    folder.dev = st.st_dev
    return st, None


def plan_compress(cfg, facts, plan, measure):
    from .rules import Kept
    if not cfg.cold_folders:
        return
    program = zstd()
    items = []
    for folder in cfg.cold_folders:
        st, refusal = _start(cfg, facts, folder)
        if refusal is None and st is not None and program is None:
            refusal = Refusal('could not read', f'{ZSTD} is not installed: nothing in it is compressed')
        if refusal is not None:
            plan.kept.append(Kept(folder.path, RULE, refusal.code, refusal.detail))
            continue
        if st is None:              # not there: nothing to do
            continue
        stack = [(folder.path, st)]
        while stack:
            here, hst = stack.pop()
            try:
                with os.scandir(here) as it:
                    entries = sorted(it, key=lambda e: e.name)
            except OSError as ex:
                plan.kept.append(Kept(here, RULE, 'could not read', f'{ex.filename or here}: {ex.strerror}'))
                continue
            names = {entry.name for entry in entries}
            refusal = _folder_refusal(cfg, facts, folder, here, hst, names)
            if refusal is not None:
                plan.kept.append(Kept(here, RULE, refusal.code, refusal.detail,
                                      guard.measure(here) if measure else None))
                continue
            for entry in entries:
                try:
                    est = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as ex:
                    plan.kept.append(Kept(entry.path, RULE, 'could not read', ex.strerror))
                    continue
                if stat.S_ISDIR(est.st_mode):
                    stack.append((entry.path, est))
                elif stat.S_ISREG(est.st_mode):
                    item, kept = _item(cfg, facts, folder, entry.path, est, names)
                    if item is not None:
                        item.ctx = (cfg, facts)
                        items.append(item)
                    elif kept is not None:
                        plan.kept.append(kept)
    if measure:
        estimate([item for item in items if item.kind == 'compress' and item.twin is None])
    plan.items += items


def estimate(items):
    """Sets each item's `estimate` (bytes it would free) from one zstd run over a sample of every file: the start
    of each, up to SAMPLES in all. A sample that cannot be read leaves the estimate unknown (None)."""
    program = zstd()
    if not items or program is None:
        return
    taken, chunks = 0, []
    for item in items:
        if taken >= SAMPLES:
            break
        try:
            fd = os.open(item.path, READ)
            try:
                chunk = os.pread(fd, min(SAMPLE, SAMPLES - taken), 0)
            finally:
                os.close(fd)
        except OSError:
            continue
        chunks.append(chunk)
        taken += len(chunk)
    data = b''.join(chunks)
    if not data:
        return
    try:
        done = subprocess.run([program, *LEVEL, '-c'], input=data, capture_output=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        return
    if done.returncode != 0 or not done.stdout:
        return
    ratio = min(len(done.stdout) / len(data), 1.0)
    for item in items:
        item.estimate = int(item.size * (1 - ratio))


# --- the last check -----------------------------------------------------------------------------------------------
def recheck(cfg, facts, item):
    """Everything about one item read again: the folders from the cold folder down, the file, the room on the disk.
    None when it may still be done."""
    folder = next((f for f in cfg.cold_folders if f.path == item.folder.path), None)
    if folder is None:
        return Refusal('not a candidate', 'its folder is no longer in compressCold.folders')
    st, refusal = _start(cfg, facts, folder)
    if refusal is not None:
        return refusal
    if st is None:
        return Refusal('not a candidate', f'{folder.path} is gone')
    rel = os.path.relpath(os.path.dirname(item.path), folder.path)
    if rel.startswith('..'):
        return Refusal('not a candidate', f'not in {folder.path}')
    here, hst = folder.path, st
    for part in [None] + ([] if rel == '.' else rel.split('/')):
        if part is not None:
            here = os.path.join(here, part)
        try:
            hst = os.lstat(here) if part is not None else hst
            names = set(os.listdir(here)) if stat.S_ISDIR(hst.st_mode) and not stat.S_ISLNK(hst.st_mode) else set()
        except OSError as ex:
            return Refusal('could not read', f'{here}: {ex.strerror}')
        refusal = _folder_refusal(cfg, facts, folder, here, hst, names)
        if refusal is not None:
            return refusal
    try:
        fst = os.lstat(item.path)
    except FileNotFoundError:
        return Refusal('not a candidate', 'it is gone')
    except OSError as ex:
        return Refusal('could not read', f'{item.path}: {ex.strerror}')
    if not stat.S_ISREG(fst.st_mode):
        return Refusal('not a candidate', 'it is no longer a file')
    new, kept = _item(cfg, facts, folder, item.path, fst, names)
    if kept is not None:
        return Refusal(kept.code, kept.detail)
    if new is None or new.kind != item.kind:
        return Refusal('not a candidate', 'it changed or was read, or the rule no longer takes it')
    item.look, item.size = new.look, new.size
    if item.kind == 'compress':
        item.twin = new.twin
    return None


# --- doing it -----------------------------------------------------------------------------------------------------
class _Step:
    """One zstd run, stopped when it takes longer than `seconds`."""

    def __init__(self, args, stdin, stdout, seconds, what):
        program = zstd()
        if program is None:
            raise Untouched(f'{ZSTD} is not installed: the original stays')
        self.what, self.seconds, self.late = what, seconds, False
        self.err = tempfile.TemporaryFile()
        try:
            self.proc = subprocess.Popen([program, *args], stdin=stdin, stdout=stdout, stderr=self.err, close_fds=True)
        except OSError as ex:
            self.err.close()
            raise Untouched(f'{ZSTD}: {ex.strerror}: the original stays') from None
        self.timer = threading.Timer(seconds, self._stop)
        self.timer.daemon = True
        self.timer.start()

    def _stop(self):
        self.late = True
        self.proc.kill()

    def kill(self):
        """Stops it after an error of the caller's own."""
        self.timer.cancel()
        self.proc.kill()
        self.proc.wait()
        self.err.close()

    def finish(self):
        """Waits for it; raises Untouched when it failed or ran out of time."""
        try:
            code = self.proc.wait()
            self.timer.cancel()
            if self.late:
                raise Untouched(f'{ZSTD} {self.what} took longer than {self.seconds:.0f} s and was stopped: the original stays')
            if code != 0:
                self.err.seek(0)
                text = self.err.read(500).decode('utf-8', 'replace').strip().splitlines()
                raise Untouched(f'{ZSTD} {self.what} failed ({text[-1] if text else f"exit {code}"}): the original stays')
        finally:
            self.err.close()


def _pack(src, dst, size):
    """Compresses the open file `src` into the open file `dst`; returns (sha256, length) of what it read."""
    os.lseek(src, 0, os.SEEK_SET)
    digest, length = hashlib.sha256(), 0
    step = _Step([*LEVEL, '-c'], subprocess.PIPE, dst, deadline(size), 'compress')
    try:
        while True:
            chunk = os.read(src, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
            step.proc.stdin.write(chunk)
        step.proc.stdin.close()
    except BrokenPipeError:         # zstd stopped reading: finish() says why, or the read-back shows the gap
        try:
            step.proc.stdin.close()
        except BrokenPipeError:
            pass
    except BaseException:
        step.kill()
        raise
    step.finish()
    return digest.hexdigest(), length


def _content(fd, size):
    """(sha256, length) of what the zstd file at `fd` holds, after `zstd -t` has tested it. Its pages are dropped
    from the page cache first (it was synced), so both read what is on the disk, not what the kernel still holds."""
    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
    os.lseek(fd, 0, os.SEEK_SET)
    _Step(['-q', '-t'], fd, subprocess.DEVNULL, deadline(size), 'test (-t)').finish()
    os.lseek(fd, 0, os.SEEK_SET)
    digest, length = hashlib.sha256(), 0
    step = _Step(['-q', '-d', '-c'], fd, subprocess.PIPE, deadline(size), 'decompress')
    try:
        while True:
            chunk = step.proc.stdout.read(CHUNK)
            if not chunk:
                break
            digest.update(chunk)
            length += len(chunk)
    except BaseException:
        step.kill()
        raise
    finally:
        step.proc.stdout.close()
    step.finish()
    return digest.hexdigest(), length


def _hash(fd):
    os.lseek(fd, 0, os.SEEK_SET)
    digest, length = hashlib.sha256(), 0
    while True:
        chunk = os.read(fd, CHUNK)
        if not chunk:
            return digest.hexdigest(), length
        digest.update(chunk)
        length += len(chunk)


def _same(st, look):
    return (st.st_dev, st.st_ino) == look.ident and (st.st_size, st.st_mtime_ns, st.st_ctime_ns) == look.stamp


def _ident(parent, name):
    """(dev, ino) of `name` in the folder, or None when it is not there."""
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None
    return (st.st_dev, st.st_ino)


def _unlink_if(parent, name, ident):
    """Removes `name` from the folder only when it is still the file `ident` names."""
    if _ident(parent, name) == ident:
        os.unlink(name, dir_fd=parent)


def compress(item):
    """Replaces item.path by its checked zstd copy. Raises Untouched when the original is still there as it was
    (nothing to do by hand); it never removes the original before a checked copy has its name."""
    from .rules import _open_parent
    cfg, facts = item.ctx
    path, name = item.path, os.path.basename(item.path)
    target = name + '.zst'
    try:
        parent = _open_parent(path)
    except OSError as ex:
        raise Untouched(f'{path}: its folder: {ex.strerror}: nothing changed') from None
    try:
        try:
            src = os.open(name, READ, dir_fd=parent)
        except OSError as ex:
            raise Untouched(f'{path}: {ex.strerror}: nothing changed') from None
        try:
            st = os.fstat(src)
            if not stat.S_ISREG(st.st_mode) or not _same(st, item.look):
                raise Untouched(f'{path} is not what the last check saw: nothing changed')
            if item.twin is not None:
                copy = _check_twin(parent, target, src, st.st_size)
            else:
                free = free_bytes(parent)
                if free < st.st_size + cfg.keep_free:
                    raise Untouched(f'disk nearly full ({human(free)} free, a whole copy and keepFreeGB '
                                    f'{human(cfg.keep_free)} do not fit): the original stays')
                copy = _write_copy(parent, name, target, src, st, item.look)
        finally:
            # Closed before the last look at the process list: the cleaner's own descriptor would count as a use.
            os.close(src)
        # The copy has its name. The original goes only if nothing opened or changed it meanwhile, and both names
        # are still the files this run checked (read just before the removal: an editor's atomic save onto either
        # name is seen). What is left is the time between these reads and the unlink, microseconds; a read lease
        # (F_SETLEASE) would close it only while the cleaner holds the file open, which the process list counts.
        facts.forget()
        refusal = _in_use(facts, item.folder, path, st)
        try:
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
        except FileNotFoundError:
            now = None
        named = _ident(parent, target)
        if refusal is not None or now is None or not _same(now, item.look) or named != copy:
            if item.twin is None:
                _unlink_if(parent, target, copy)
                os.fsync(parent)
            if refusal is not None:
                why = f'{refusal.code}: {refusal.detail}'
            elif named != copy:
                why = f'{target} is not the copy this run checked any more'
            else:
                why = 'it changed while it was compressed'
            raise Untouched(f'{path}: {why}: the original stays' + (', its copy was removed' if item.twin is None else ''))
        try:
            os.unlink(name, dir_fd=parent)
        except OSError as ex:
            raise Untouched(f'{path}: {ex.strerror}: it could not be removed after its checked copy {target} got its '
                            'name: both stay, and the next run checks the copy again') from None
        os.fsync(parent)
        try:
            packed = os.stat(target, dir_fd=parent, follow_symlinks=False).st_blocks * 512
        except OSError:
            packed = None
    finally:
        os.close(parent)
    item.size = max(st.st_blocks * 512 - (packed or 0), 0)       # what it freed
    item.result = (f'{path} ({human(item.size)}, {RULE}: {item.why}; now {target}, {human(packed)}, '
                   'read back and checked)')


def _keep_metadata(src, dst, st):
    """Gives the copy the original's group, mode, ACL and extended attributes (COPYABLE_XATTRS; _keepable skipped
    files with others). The group first: a chown may clear set-id bits, which the mode then sets again."""
    if st.st_gid != os.fstat(dst).st_gid:
        os.fchown(dst, -1, st.st_gid)
    os.fchmod(dst, stat.S_IMODE(st.st_mode))
    for attr in xattrs(src):
        if not matches(attr, COPYABLE_XATTRS):
            raise Untouched(f'it has the extended attribute {attr}, which its copy could not keep: the original stays')
        os.setxattr(dst, attr, os.getxattr(src, attr))


def _write_copy(parent, name, target, src, st, look):
    """Writes, syncs and checks the copy under a part name, then links it to `target`. Returns its ident."""
    part = f'.{name}.zst.{secrets.token_hex(4)}{PART}'
    try:
        dst = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    except OSError as ex:
        raise Untouched(f'its copy {part}: {ex.strerror}: the original stays') from None
    try:
        try:
            want = _pack(src, dst, st.st_size)
            os.fsync(dst)
            check = os.open(part, READ, dir_fd=parent)
            try:
                if (os.fstat(check).st_ino, os.fstat(check).st_dev) != (os.fstat(dst).st_ino, os.fstat(dst).st_dev):
                    raise Untouched(f'its copy {part} changed while it was written: the original stays')
                got = _content(check, st.st_size)
            finally:
                os.close(check)
            if got != want:
                raise Untouched(f'its copy {part} does not hold the same bytes ({got[1]} read back, {want[1]} '
                                'written): the original stays')
            if not _same(os.fstat(src), look):
                raise Untouched('it changed while it was compressed: the original stays')
            _keep_metadata(src, dst, st)
            os.utime(dst, ns=(st.st_atime_ns, st.st_mtime_ns))
            os.fsync(dst)
            ident = (os.fstat(dst).st_dev, os.fstat(dst).st_ino)
        except OSError as ex:
            raise Untouched(f'its copy {part}: {ex.strerror or ex}: the original stays') from None
        finally:
            os.close(dst)
        try:
            os.link(part, target, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
        except FileExistsError:
            raise Untouched(f'{target} appeared while the copy was written: the original stays') from None
        except OSError as ex:
            raise Untouched(f'{target}: {ex.strerror}: the original stays') from None
    finally:
        try:
            os.unlink(part, dir_fd=parent)
        except FileNotFoundError:
            pass
        os.fsync(parent)
    return ident


def _check_twin(parent, target, src, size):
    """A copy an earlier run linked and did not finish with: it must hold the original's bytes exactly."""
    try:
        fd = os.open(target, READ, dir_fd=parent)
    except OSError as ex:
        raise Untouched(f'{target}: {ex.strerror}: the original stays') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Untouched(f'{target} is not a copy this rule made: the original stays')
        if _content(fd, size) != _hash(src):
            raise Untouched(f'{target} beside it holds other bytes than the original: both stay (compare them by hand)')
        return (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def remove_part(item):
    from .rules import remove_tree
    remove_tree(item.path, item.look)
