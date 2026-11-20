"""The `dedupe` rule (#486): files with the same content in the folders the machine file names become hard links of
one of them, so the content is stored once. It deletes no content and no name: every file keeps its name, its bytes,
its mode, owner, group and extended attributes; only how they are stored changes. Off unless `dedupe.folders` names
a folder.

Files are the same only when their bytes are, compared byte for byte just before the link (the sha256 of the plan
only finds the candidates). Both must also have the same mode, owner, group and extended attributes (ACLs too):
hard links share them, so a link never gives a file another file's. The modification time is the one thing that may
differ: the duplicate then shows the time of the file it now shares.

One name is replaced in two steps that each leave a whole file under every name:

1. a new hard link of the kept file is made under a hidden part name beside the duplicate (`.<name>.<8 hex>
   .cleaner-link`);
2. the part is renamed over the duplicate, which the kernel does in one step: the name shows the duplicate, then
   the kept file, never nothing.

So a run killed between the two leaves the duplicate as it was and a part that is one more name of the kept file;
the next run removes that part (a name whose content another name still holds) and links again. A part whose other
names are all gone holds the only copy of its content and stays (named in the plan).

What it never takes: links (never followed), other filesystems and mount points (a hard link cannot cross one
either), other owners' files, credential and lock names, must-keep names, folders with a KEEP file, git folders
(git needs its files as they are), files written in place (databases and their journals, logs: a hard link would
share every later write between the names), files changed in the last `days`, files in use (open, locked, named by
a process or a systemd unit), immutable or append-only files, files with more than one name already (linking one
of their names frees nothing), and files under MIN_BYTES. A file that changes while it is read or compared is kept
as it is. config.py refuses folders in or holding a must-keep path and folders another rule takes.

What a person must know: two names of one file share later writes. A program that writes into one of them in place
(not by writing a new file and renaming it, as editors and most tools do) changes both. That is why only files
nobody changed for `days` (at least config.MIN_COLD_DAYS) and no files that programs write in place are taken.
"""
import errno, hashlib, os, re, secrets, stat

from . import guard
from .compress import CHUNK, READ, _days, _in_use, _same, _stuck, human, xattrs
from .facts import Unreadable, Untouched
from .guard import CREDENTIAL_NAMES, KEEP_FILE, LOCK_NAMES, Refusal, matches

RULE = 'dedupe'
DAY = 86400
MIN_BYTES = 64 * 1024               # a smaller file frees too little to be worth a read of every one of its size
PART = '.cleaner-link'
PART_NAME = re.compile(r'^\.(.+)\.[0-9a-f]{8}' + re.escape(PART) + '$')
NAME_MAX = 255
# Names programs write in place: databases and their journals, logs, append-only records.
IN_PLACE = ('*.db', '*.db-*', '*.db3', '*.sqlite', '*.sqlite3', '*.sqlite-*', '*.sqlite3-*', '*-wal', '*-shm',
            '*-journal', '*.journal', '*.log', '*.log.*', '*.jsonl', '*.ndjson', '*.pid')
SIDECARS = ('-wal', '-shm', '-journal')


# --- what may be linked -------------------------------------------------------------------------------------------
def _start(cfg, facts, folder):
    """The folder's own check: (stat, None) when it may be walked, (None, Refusal) when it is kept whole, (None,
    None) when it is not there."""
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
    folder.dev = st.st_dev
    return st, None


def _folder_refusal(cfg, facts, folder, here, st, names):
    """Why the folder `here` (the configured folder or one in it) and everything in it stays; None when it may be
    looked into. `st` is its lstat, `names` its entries."""
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return Refusal('symbolic link', f'{here} is a link or not a folder: never followed')
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'{here} (uid {guard.owner(st)})')
    from .config import must_keep_match
    kept = must_keep_match(cfg.must_keep, here)
    if kept is not None:
        return Refusal('must keep', '/' + '/'.join(kept))
    if here != folder.path:
        if st.st_dev != folder.dev:
            return Refusal('another filesystem', f'{here} is a mount point')
        name = os.path.basename(here)
        if guard.kept_name(cfg, name):
            return Refusal('must keep', name)
        if matches(name, CREDENTIAL_NAMES):
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
        return Refusal('git folder', f'{here}: git needs its files as they are')
    return _stuck(here)


def _file_answer(cfg, facts, folder, path, st, names):
    """For one regular file: None when the rule does not look at it (small, changed lately, a part file), a Refusal
    when it is kept, or the reason it may be linked."""
    name = os.path.basename(path)
    if name.endswith(PART) or st.st_size < MIN_BYTES:
        return None
    age = facts.now - guard.changed(st)
    if age < folder.days * DAY:
        return None
    if guard.kept_name(cfg, name):
        return Refusal('must keep', name)
    if matches(name, CREDENTIAL_NAMES):
        return Refusal('credential name', name)
    if matches(name, LOCK_NAMES):
        return Refusal('lock file', name)
    if matches(name, IN_PLACE) or any(name + s in names for s in SIDECARS):
        return Refusal('written in place', 'a database, journal or log: a hard link would share its next write')
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'uid {guard.owner(st)}')
    if not st.st_mode & stat.S_IRUSR:
        return Refusal('unreadable', f'its owner may not read it (mode {stat.S_IMODE(st.st_mode):04o})')
    if len(os.fsencode(name)) + len('..12345678' + PART) > NAME_MAX:
        return Refusal('name too long', 'no room for the name of its part')
    refusal = _stuck(path)
    if refusal is not None:
        return refusal
    refusal = _in_use(facts, folder, path, st)
    if refusal is not None:
        return refusal
    return f'not changed for {_days(int((age + 1) // DAY))} ({folder.path}: {_days(folder.days)})'


def _part_answer(cfg, facts, folder, path, st):
    """For a part file a run left: the reason to remove it, or a Refusal when it holds the only copy of its content
    or may not be removed."""
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'uid {guard.owner(st)}')
    if st.st_nlink < 2:
        return Refusal('part without its file', 'its other names are gone, so it holds the only copy of its content: '
                                                'left as it is (rename it, or delete it by hand)')
    refusal = _in_use(facts, folder, path, st)
    if refusal is not None:
        return refusal
    return f'a link an earlier run made and did not rename ({st.st_nlink - 1} other names hold its content)'


def _look(st):
    look = guard.Look(size=st.st_blocks * 512, newest=guard.changed(st))
    look.ident = (st.st_dev, st.st_ino)
    look.stamp = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return look


def _meta(fd):
    """What hard links share besides the bytes, of an open file: mode, owner, group, extended attributes (values)."""
    st = os.fstat(fd)
    attrs = tuple(sorted((name, os.getxattr(fd, name)) for name in xattrs(fd)))
    return (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, attrs)


def _digest(path, st):
    """(sha256 of its bytes, its _meta), read through one descriptor; Refusal when it changed while it was read or
    could not be read."""
    try:
        fd = os.open(path, READ)
    except OSError as ex:
        return Refusal('could not read', ex.strerror)
    try:
        if not _same(os.fstat(fd), _look(st)):
            return Refusal('changed', 'it changed while it was read: it stays as it is')
        digest = hashlib.sha256()
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            digest.update(chunk)
        if not _same(os.fstat(fd), _look(st)):
            return Refusal('changed', 'it changed while it was read: it stays as it is')
        return digest.hexdigest(), _meta(fd)
    except OSError as ex:
        return Refusal('could not read', ex.strerror)
    finally:
        os.close(fd)


def _walk(cfg, facts, folder, plan, measure, files, parts):
    """Every regular file in the folder: candidates into `files` [(folder, path, st, answer)], part files into
    `parts`, what is kept whole into the plan."""
    from .rules import Kept
    st, refusal = _start(cfg, facts, folder)
    if refusal is not None:
        plan.kept.append(Kept(folder.path, RULE, refusal.code, refusal.detail))
        return
    if st is None:                  # not there: nothing to do
        return
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
            plan.kept.append(Kept(here, RULE, refusal.code, refusal.detail, guard.measure(here) if measure else None))
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
            elif stat.S_ISREG(est.st_mode) and PART_NAME.match(entry.name):
                parts.append((folder, entry.path, est))
            elif stat.S_ISREG(est.st_mode):
                answer = _file_answer(cfg, facts, folder, entry.path, est, names)
                if answer is not None:
                    files.append((folder, entry.path, est, answer))


def plan_dedupe(cfg, facts, plan, measure):
    from .rules import Item, Kept
    if not cfg.dedupe_folders:
        return
    files, parts = [], []
    for folder in cfg.dedupe_folders:
        _walk(cfg, facts, folder, plan, measure, files, parts)
    for folder, path, st in parts:
        answer = _part_answer(cfg, facts, folder, path, st)
        if isinstance(answer, Refusal):
            plan.kept.append(Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512))
        else:
            item = Item('link-part', path, RULE, answer, 0, _look(st))      # frees nothing: another name holds it
            item.folder, item.ctx = folder, (cfg, facts)
            plan.items.append(item)
    # The names of each file seen in the folders (parts too). A file with more names than that has a name
    # outside them: it is never the file kept, or a link would join a copy here to a file elsewhere.
    seen = {}
    for entry in files + parts:
        ident = (entry[2].st_dev, entry[2].st_ino)
        seen[ident] = seen.get(ident, 0) + 1
    # Only files of one size on one filesystem can be the same; only those are read.
    by_size = {}
    for entry in files:
        by_size.setdefault((entry[2].st_dev, entry[2].st_size), []).append(entry)
    for (_, size), group in sorted(by_size.items()):
        if len({(e[2].st_dev, e[2].st_ino) for e in group}) < 2:
            continue
        same = {}
        for folder, path, st, answer in group:
            if isinstance(answer, Refusal):
                plan.kept.append(Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512))
                continue
            got = _digest(path, st)
            if isinstance(got, Refusal):
                plan.kept.append(Kept(path, RULE, got.code, got.detail, st.st_blocks * 512))
                continue
            same.setdefault(got[0], []).append((folder, path, st, answer, got[1]))
        for digest, matched in sorted(same.items()):
            _link_group(cfg, facts, plan, digest, matched, seen)


def _link_group(cfg, facts, plan, digest, matched, seen):
    """Files with the same sha256: each set of them with the same metadata keeps one file (of those with every
    name in the folders: the one with the most names, then the oldest), and every other file with one name becomes
    a link of it."""
    from .rules import Item, Kept
    if len({(e[2].st_dev, e[2].st_ino) for e in matched}) < 2:
        return                      # names of one file already
    by_meta = {}
    for entry in matched:
        by_meta.setdefault(entry[4], []).append(entry)
    for meta, entries in sorted(by_meta.items(), key=lambda kv: min(e[1] for e in kv[1])):
        inodes = {}
        for entry in entries:
            inodes.setdefault((entry[2].st_dev, entry[2].st_ino), []).append(entry)
        if len(inodes) < 2:
            if len(by_meta) > 1:
                other = next(e[1] for m, es in sorted(by_meta.items()) if m != meta for e in es)
                for _, path, st, _, _ in entries:
                    plan.kept.append(Kept(path, RULE, 'other metadata', f'the same bytes as {other}, but another mode, '
                                          'owner, group or extended attributes: a link would give it those',
                                          st.st_blocks * 512))
            continue
        inside = [es for ident, es in inodes.items() if es[0][2].st_nlink <= seen[ident]]
        keeper = None
        if inside:
            keeper = min(inside, key=lambda es: (-es[0][2].st_nlink, es[0][2].st_mtime_ns, min(e[1] for e in es)))
            kfolder, kpath, kst = min(keeper, key=lambda e: e[1])[:3]
        for ident, names in sorted(inodes.items(), key=lambda kv: min(e[1] for e in kv[1])):
            if names is keeper:
                continue
            for folder, path, st, answer, _ in names:
                if st.st_nlink > 1:
                    why = ('some of them outside dedupe.folders: a link would join files elsewhere'
                           if st.st_nlink > seen[ident] else 'linking one of them frees nothing')
                    plan.kept.append(Kept(path, RULE, 'hard-linked', f'{st.st_nlink} names, {why}', st.st_blocks * 512))
                    continue
                item = Item('dedupe', path, RULE, f'the same bytes as {kpath}, {answer}', st.st_blocks * 512, _look(st))
                item.folder, item.keeper, item.keeper_folder = folder, kpath, kfolder
                item.keeper_look, item.digest, item.ctx = _look(kst), digest, (cfg, facts)
                plan.items.append(item)


# --- the last check -----------------------------------------------------------------------------------------------
def _recheck_path(cfg, facts, folder_path, path):
    """(folder, lstat, names of its folder) of `path` read again from its configured folder down, or a Refusal."""
    folder = next((f for f in cfg.dedupe_folders if f.path == folder_path), None)
    if folder is None:
        return Refusal('not a candidate', 'its folder is no longer in dedupe.folders')
    st, refusal = _start(cfg, facts, folder)
    if refusal is not None:
        return refusal
    if st is None:
        return Refusal('not a candidate', f'{folder.path} is gone')
    rel = os.path.relpath(os.path.dirname(path), folder.path)
    if rel.startswith('..'):
        return Refusal('not a candidate', f'not in {folder.path}')
    here, hst, names = folder.path, st, set()
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
        fst = os.lstat(path)
    except FileNotFoundError:
        return Refusal('not a candidate', f'{path} is gone')
    except OSError as ex:
        return Refusal('could not read', f'{path}: {ex.strerror}')
    if not stat.S_ISREG(fst.st_mode):
        return Refusal('not a candidate', f'{path} is no longer a file')
    return folder, fst, names


def _keeper_same(st, look):
    """The kept file as the plan saw it. Its inode time is not compared: linking an earlier duplicate of the same
    group in this run changed it (a new name); its content time, size and identity still say it is the same."""
    return (st.st_dev, st.st_ino) == look.ident and (st.st_size, st.st_mtime_ns) == look.stamp[:2]


def recheck(cfg, facts, item):
    """Everything about one item read again: the folders from the configured folder down, the duplicate and the
    kept file. None when it may still be done."""
    got = _recheck_path(cfg, facts, item.folder.path, item.path)
    if isinstance(got, Refusal):
        return got
    folder, st, names = got
    if item.kind == 'link-part':
        if not _same(st, item.look):
            return Refusal('not a candidate', 'it changed since the plan')
        answer = _part_answer(cfg, facts, folder, item.path, st)
        return answer if isinstance(answer, Refusal) else None
    if not _same(st, item.look) or st.st_nlink != 1:
        return Refusal('not a candidate', 'it changed, got another name, or was replaced since the plan')
    answer = _file_answer(cfg, facts, folder, item.path, st, names)
    if answer is None:
        return Refusal('not a candidate', 'the rule no longer takes it')
    if isinstance(answer, Refusal):
        return answer
    got = _recheck_path(cfg, facts, item.keeper_folder.path, item.keeper)
    if isinstance(got, Refusal):
        return Refusal(got.code, f'the kept file {item.keeper}: {got.detail}')
    kfolder, kst, knames = got
    if not _keeper_same(kst, item.keeper_look):
        return Refusal('not a candidate', f'the kept file {item.keeper} changed or was replaced since the plan')
    answer = _file_answer(cfg, facts, kfolder, item.keeper, kst, knames)
    if answer is None or isinstance(answer, Refusal):
        why = f'{answer.code}: {answer.detail}' if answer is not None else 'the rule no longer takes it'
        return Refusal(answer.code if answer is not None else 'not a candidate', f'the kept file {item.keeper}: {why}')
    return None


# --- doing it -----------------------------------------------------------------------------------------------------
def _equal(a, b, size):
    """True when the two open files hold the same `size` bytes, compared byte for byte from the start."""
    os.lseek(a, 0, os.SEEK_SET)
    os.lseek(b, 0, os.SEEK_SET)
    left = size + 1                 # one byte more: a file that grew is not the same
    while left > 0:
        x, y = os.read(a, min(CHUNK, left)), os.read(b, min(CHUNK, left))
        if x != y:
            return False
        if not x:
            return left == 1
        left -= len(x)
    return False


def _stat_at(parent, name):
    try:
        return os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        return None


def _unlink_part(parent, part, ident):
    """Removes the part only when it is still the link this run made (another name of the kept file)."""
    st = _stat_at(parent, part)
    if st is not None and (st.st_dev, st.st_ino) == ident and st.st_nlink >= 2:
        os.unlink(part, dir_fd=parent)
    os.fsync(parent)


def link(item):
    """Replaces the duplicate item.path by a hard link of item.keeper once both hold the same bytes, compared byte
    for byte. Raises Untouched when the duplicate is still there as it was."""
    from .rules import _open_parent
    cfg, facts = item.ctx
    path, name = item.path, os.path.basename(item.path)
    kname = os.path.basename(item.keeper)
    kident = item.keeper_look.ident
    try:
        parent = _open_parent(path)
    except OSError as ex:
        raise Untouched(f'{path}: its folder: {ex.strerror}: nothing changed') from None
    try:
        try:
            kparent = _open_parent(item.keeper)
        except OSError as ex:
            raise Untouched(f'{item.keeper}: its folder: {ex.strerror}: nothing changed') from None
        try:
            _compare(parent, name, kparent, kname, item)
            # Closed before the last look at the process list: the cleaner's own descriptors would count as uses.
            facts.forget()
            for folder, which, at, base in ((item.folder, path, parent, name), (item.keeper_folder, item.keeper, kparent, kname)):
                st = _stat_at(at, base)
                if st is None:
                    raise Untouched(f'{which} is gone: nothing changed')
                refusal = _in_use(facts, folder, which, st)
                if refusal is not None:
                    raise Untouched(f'{which}: {refusal.code}: {refusal.detail}: nothing changed')
            part = f'.{name}.{secrets.token_hex(4)}{PART}'
            try:
                os.link(kname, part, src_dir_fd=kparent, dst_dir_fd=parent, follow_symlinks=False)
            except OSError as ex:
                raise Untouched(f'{path}: a link of {item.keeper}: {ex.strerror}: nothing changed') from None
        finally:
            os.close(kparent)
        pst = _stat_at(parent, part)
        if pst is None or (pst.st_dev, pst.st_ino) != kident:
            os.fsync(parent)
            raise Untouched(f'{path}: its part {part} is not a link of {item.keeper}: the duplicate stays')
        # Read just before the rename: an editor's save onto the duplicate since the comparison is seen, and the
        # duplicate stays. What is left is the time between this read and the rename, microseconds.
        now = _stat_at(parent, name)
        if now is None or not _same(now, item.look) or now.st_nlink != 1:
            _unlink_part(parent, part, kident)
            raise Untouched(f'{path} changed while it was compared: it stays as it is')
        try:
            os.rename(part, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as ex:
            _unlink_part(parent, part, kident)
            raise Untouched(f'{path}: {ex.strerror}: the duplicate stays') from None
        os.fsync(parent)
    finally:
        os.close(parent)
    item.result = (f'{path} ({human(item.size)}, {RULE}: {item.why}; now a hard link of {item.keeper}, compared byte '
                   'for byte)')


def _compare(parent, name, kparent, kname, item):
    """Untouched unless the duplicate and the kept file are still what the plan saw and hold the same bytes, mode,
    owner, group and extended attributes."""
    path = item.path
    try:
        a = os.open(name, READ, dir_fd=parent)
    except OSError as ex:
        raise Untouched(f'{path}: {ex.strerror}: nothing changed') from None
    try:
        try:
            b = os.open(kname, READ, dir_fd=kparent)
        except OSError as ex:
            raise Untouched(f'{item.keeper}: {ex.strerror}: nothing changed') from None
        try:
            ast, bst = os.fstat(a), os.fstat(b)
            if not stat.S_ISREG(ast.st_mode) or not _same(ast, item.look) or ast.st_nlink != 1:
                raise Untouched(f'{path} is not what the last check saw: nothing changed')
            if not stat.S_ISREG(bst.st_mode) or not _keeper_same(bst, item.keeper_look):
                raise Untouched(f'{item.keeper} is not what the last check saw: nothing changed')
            try:
                equal = _equal(a, b, ast.st_size)
                meta = _meta(a) == _meta(b)
            except OSError as ex:
                raise Untouched(f'{path}: {ex.strerror}: nothing changed') from None
            if not _same(os.fstat(a), item.look) or not _keeper_same(os.fstat(b), item.keeper_look):
                raise Untouched(f'{path} or {item.keeper} changed while they were compared: nothing changed')
            if not equal:
                raise Untouched(f'{path} does not hold the same bytes as {item.keeper} (compared byte for byte): '
                                'nothing changed')
            if not meta:
                raise Untouched(f'{path} has another mode, owner, group or extended attributes than {item.keeper}: '
                                'nothing changed')
        finally:
            os.close(b)
    finally:
        os.close(a)


def remove_part(item):
    """Removes a part an earlier run left, only while another name still holds its content."""
    from .rules import _open_parent
    try:
        parent = _open_parent(item.path)
    except OSError as ex:
        raise Untouched(f'{item.path}: its folder: {ex.strerror}: nothing removed') from None
    try:
        name = os.path.basename(item.path)
        st = _stat_at(parent, name)
        if st is None or (st.st_dev, st.st_ino) != item.look.ident or st.st_nlink < 2:
            raise Untouched(f'{item.path} is not what the last check saw, or it is the last name of its content: '
                            'nothing removed')
        try:
            os.unlink(name, dir_fd=parent)
        except OSError as ex:
            raise Untouched(f'{item.path}: {ex.strerror}: nothing removed') from None
        os.fsync(parent)
    finally:
        os.close(parent)
