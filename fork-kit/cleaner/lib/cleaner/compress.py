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
hard-linked files, files already compressed, and files under MIN_BYTES. The machine file can only name folders and
days (config.COMPRESS_SPEC); config.py refuses folders the rule may never take.
"""
import errno, hashlib, os, re, secrets, shutil, stat, subprocess, tempfile

from . import guard
from .config import matches
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
    return None


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
    refusal = _in_use(facts, folder, path, st)
    if refusal is not None:
        return refusal
    return f'not changed or read for {_days(int((age + 1) // DAY))} ({folder.path}: {_days(folder.days)})'


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
    if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or guard.owner(st) != os.getuid():
        return None, Refusal('name taken', f'{os.path.basename(path)}.zst is there and is no copy this rule made')
    return st, None


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
        bound = facts.mounts().bound(path, st.st_dev)
    except Unreadable as ex:
        return None, Refusal('could not read', f'the mount list ({ex})')
    if bound is not None:
        return None, Refusal('in use', f'a bind mount shows {bound} of its filesystem somewhere else (a container or a sandbox)')
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
def _run(args, stdin, stdout):
    """Runs zstd with these descriptors; raises Untouched with its message when it fails."""
    program = zstd()
    if program is None:
        raise Untouched(f'{ZSTD} is not installed: the original stays')
    err = tempfile.TemporaryFile()      # the caller closes it (`with err:`)
    try:
        proc = subprocess.Popen([program, *args], stdin=stdin, stdout=stdout, stderr=err, close_fds=True)
    except OSError as ex:
        err.close()
        raise Untouched(f'{ZSTD}: {ex.strerror}: the original stays') from None
    return proc, err


def _finish(proc, err, what):
    code = proc.wait()
    if code != 0:
        err.seek(0)
        text = err.read(500).decode('utf-8', 'replace').strip().splitlines()
        raise Untouched(f'{ZSTD} {what} failed ({text[-1] if text else f"exit {code}"}): the original stays')


def _pack(src, dst):
    """Compresses the open file `src` into the open file `dst`; returns (sha256, length) of what it read."""
    os.lseek(src, 0, os.SEEK_SET)
    digest, length = hashlib.sha256(), 0
    proc, err = _run([*LEVEL, '-c'], subprocess.PIPE, dst)
    with err:
        try:
            while True:
                chunk = os.read(src, CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                length += len(chunk)
                proc.stdin.write(chunk)
            proc.stdin.close()
        except BrokenPipeError:
            pass
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        _finish(proc, err, 'compress')
    return digest.hexdigest(), length


def _content(fd):
    """(sha256, length) of what the zstd file at `fd` holds, after `zstd -t` has tested it."""
    os.lseek(fd, 0, os.SEEK_SET)
    proc, err = _run(['-q', '-t'], fd, subprocess.DEVNULL)
    with err:
        _finish(proc, err, 'test (-t)')
    os.lseek(fd, 0, os.SEEK_SET)
    digest, length = hashlib.sha256(), 0
    proc, err = _run(['-q', '-d', '-c'], fd, subprocess.PIPE)
    with err:
        try:
            while True:
                chunk = proc.stdout.read(CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
                length += len(chunk)
        except BaseException:
            proc.kill()
            proc.wait()
            raise
        finally:
            proc.stdout.close()
        _finish(proc, err, 'decompress')
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


def _unlink_if(parent, name, ident):
    """Removes `name` from the folder only when it is still the file `ident` names."""
    try:
        st = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return
    if (st.st_dev, st.st_ino) == ident:
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
                copy = _check_twin(parent, target, src)
            else:
                free = free_bytes(parent)
                if free < st.st_size + cfg.keep_free:
                    raise Untouched(f'disk nearly full ({human(free)} free, a whole copy and keepFreeGB '
                                    f'{human(cfg.keep_free)} do not fit): the original stays')
                copy = _write_copy(parent, name, target, src, st, item.look)
            # The copy has its name. The original goes only if nothing opened or changed it meanwhile.
            facts.forget()
            refusal = _in_use(facts, item.folder, path, st)
            now = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if refusal is not None or not _same(now, item.look) or not _same(os.fstat(src), item.look):
                if item.twin is None:
                    _unlink_if(parent, target, copy)
                    os.fsync(parent)
                why = f'{refusal.code}: {refusal.detail}' if refusal else 'it changed while it was compressed'
                raise Untouched(f'{path}: {why}: the original stays, its copy was removed')
            os.unlink(name, dir_fd=parent)
            os.fsync(parent)
        finally:
            os.close(src)
        try:
            packed = os.stat(target, dir_fd=parent, follow_symlinks=False).st_blocks * 512
        except OSError:
            packed = None
    finally:
        os.close(parent)
    item.size = max(st.st_blocks * 512 - (packed or 0), 0)       # what it freed
    item.result = (f'{path} ({human(item.size)}, {RULE}: {item.why}; now {target}, {human(packed)}, '
                   'read back and checked)')


def _write_copy(parent, name, target, src, st, look):
    """Writes, syncs and checks the copy under a part name, then links it to `target`. Returns its ident."""
    part = f'.{name}.zst.{secrets.token_hex(4)}{PART}'
    try:
        dst = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=parent)
    except OSError as ex:
        raise Untouched(f'its copy {part}: {ex.strerror}: the original stays') from None
    try:
        try:
            want = _pack(src, dst)
            os.fsync(dst)
            check = os.open(part, READ, dir_fd=parent)
            try:
                if (os.fstat(check).st_ino, os.fstat(check).st_dev) != (os.fstat(dst).st_ino, os.fstat(dst).st_dev):
                    raise Untouched(f'its copy {part} changed while it was written: the original stays')
                got = _content(check)
            finally:
                os.close(check)
            if got != want:
                raise Untouched(f'its copy {part} does not hold the same bytes ({got[1]} read back, {want[1]} '
                                'written): the original stays')
            if not _same(os.fstat(src), look):
                raise Untouched('it changed while it was compressed: the original stays')
            os.fchmod(dst, stat.S_IMODE(st.st_mode))
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


def _check_twin(parent, target, src):
    """A copy an earlier run linked and did not finish with: it must hold the original's bytes exactly."""
    try:
        fd = os.open(target, READ, dir_fd=parent)
    except OSError as ex:
        raise Untouched(f'{target}: {ex.strerror}: the original stays') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            raise Untouched(f'{target} is not a copy this rule made: the original stays')
        if _content(fd) != _hash(src):
            raise Untouched(f'{target} beside it holds other bytes than the original: both stay (compare them by hand)')
        return (st.st_dev, st.st_ino)
    finally:
        os.close(fd)


def remove_part(item):
    from .rules import remove_tree
    remove_tree(item.path, item.look)
