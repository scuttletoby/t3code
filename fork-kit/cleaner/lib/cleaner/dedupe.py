"""The `dedupe` rule (#486): files with the same content in the folders the machine file names become hard links of
one of them, so the content is stored once. It deletes no content and no name: every file keeps its name, its bytes,
its mode, owner, group, extended attributes and chattr flags; only how they are stored changes. Off unless
`dedupe.folders` names a folder.

Files are the same only when their bytes are, compared byte for byte just before the link (the sha256 of the plan
only finds the candidates). Both must also have the same mode, owner, group, extended attributes (ACLs too) and
chattr flags: hard links share them, so a link never gives a file another file's. The modification time is the one
thing that may differ: the duplicate then shows the time of the file it now shares.

One name is replaced in two steps that each leave a whole file under every name:

1. a new hard link of the kept file is made under a hidden part name beside the duplicate (`.<name>.<8 hex>
   .cleaner-link`);
2. the part is renamed over the duplicate, which the kernel does in one step: the name shows the duplicate, then
   the kept file, never nothing.

So a run killed between the two leaves the duplicate as it was and a part that is one more name of the kept file;
the next run that reads the folders (not one without `dedupe.writtenInPlace`) removes that part (a name whose
content another name still holds) and links again. A part whose other names are all gone holds the only copy of
its content and stays (named in the plan).

What it never takes: links (never followed), other filesystems and mount points (a hard link cannot cross one
either), other owners' files, credential and lock names, must-keep names, folders with a KEEP file and git folders,
in or above a configured folder up to `/` (git needs its files as they are), kinds written in place (IN_PLACE:
databases and their journals, logs, disk images, mail boxes, partial downloads; and the machine file's
`dedupe.writtenInPlace`), files changed in the last `days`, files in use (open, locked, named by a process or a
systemd unit), immutable or append-only files, files with more than one name already (linking one of their names
frees nothing), and files under MIN_BYTES. A file that changes while it is read or compared is kept as it is.
config.py refuses folders in or holding a must-keep path and folders another rule takes.

The kept file is judged by EVERY one of its names: it is kept, and nothing is joined to it, when any of its names
is outside the folders or is a name the rule refuses (a database, a log, a credential, in use through that name).
At the link its names are counted and each one read again, last just before the rename: a name made, removed or
moved since the plan, by anyone but this run, stops it. Files in two configured folders may be linked to each other.

What a person must know: names of one file share every later write INTO any of them. `cp` onto an existing file,
shell `>` and `>>`, `curl -o`, `wget -O`, `unzip -o`, `rsync --inplace`, `dd` and most programs that open a file
for writing change every name. Only a tool that writes a new file and renames it over the name (`rsync` by default,
`git`, package managers, editors that save that way) gives that name its own file again. So only folders of copies
that are written once and then only read may be named; the IN_PLACE list can never be complete.

The inode time (ctime): every link and rename changes the kept file's. The cleaner writes the inode time its own
last link gave each file into its cache (Cache); a file whose inode time is still that one is judged by its content
time, so the cleaner's own links never make a file look new. Any other change of the inode time counts as usual.
"""
import errno, fcntl, hashlib, json, os, re, secrets, signal, stat, struct, tempfile

from . import guard
from .compress import CHUNK, FS_IOC_GETFLAGS, READ, _days, _in_use, _same, _stuck, human, xattrs
from .facts import Unreadable, Untouched
from .guard import CREDENTIAL_NAMES, LOCK_NAMES, Refusal, matches

RULE = 'dedupe'
DAY = 86400
MIN_BYTES = 64 * 1024               # a smaller file frees too little to be worth a read of every one of its size
PART = '.cleaner-link'
PART_NAME = re.compile(r'^\.(.+)\.[0-9a-f]{8}' + re.escape(PART) + '$')
NAME_MAX = 255
# Names programs write in place: databases and their journals, logs, append-only records, virtual machine and disk
# images, swap files, mail boxes, partial downloads a client resumes. One program's own kinds (a game's save files)
# go in the machine file (`dedupe.writtenInPlace`). Never complete: the README says to name only folders of
# write-once copies.
IN_PLACE = ('*.db', '*.db-*', '*.db3', '*.sqlite', '*.sqlite3', '*.sqlite-*', '*.sqlite3-*', '*-wal', '*-shm',
            '*-journal', '*.journal', '*.wal', '*.mdb', '*.ldb', '*.ibd', '*.log', '*.log.*', '*.jsonl', '*.ndjson',
            '*.pid', '*.qcow2', '*.img', '*.raw', '*.vmdk', '*.vdi', '*.vhd', '*.vhdx',
            '*.swp', '*.swap', 'mbox', '*.mbox', '*.part', '*.crdownload', '*.partial', '*.!qb')
SIDECARS = ('-wal', '-shm', '-journal')
# chattr flags a person can set (linux/fs.h FS_FL_USER_MODIFIABLE) plus data journalling, no-copy-on-write, verity
# (read-only, checked) and DAX (`chattr +x`): two files with other flags are not the same (a link would give one the
# other's).
FS_VERITY_FL, FS_DAX_FL = 0x00100000, 0x02000000
FLAGS_MASK = 0x000380FF | 0x00004000 | 0x00800000 | FS_VERITY_FL | FS_DAX_FL
# A machine file older than `dedupe.writtenInPlace`: kinds that an older cleaner refused (and that are not in IN_PLACE
# any more) may be in the folders, so nothing is linked until the owner names them, or `[]`.
MISSING_KINDS = ('dedupe.writtenInPlace is missing from the machine file: nothing here is linked until it lists the '
                 'kinds a program writes in place in these folders, or is [] when there are none')
WARNING = ('dedupe: names of one file share every later write into any of them (cp onto an existing file, shell >, '
           'downloads, most programs): name only folders of copies written once and then only read')


# --- what one plan and its run share ------------------------------------------------------------------------------
class Cache:
    """What the cleaner keeps between runs, beside its log: `digests` {(dev, ino): (size, mtime_ns, ctime_ns,
    sha256)} so a file not changed since is not read again to find candidates (the link still compares byte for
    byte), and `own` {(dev, ino): ctime_ns}, the inode time the cleaner's own last link, rename or part removal gave
    a file. `own` is also appended to a small file at each link, so a run killed half-way still knows it. Missing,
    unreadable or not the user's own: an empty cache (files are read again; files the cleaner linked look as new as
    their inode time until their `days` pass)."""
    VERSION = 1

    def __init__(self, path=None, own_log=None):
        self.path, self.own_log = path, own_log
        self.digests, self.own, self.used, self.recorded = {}, {}, {}, set()
        self.parts = set()          # part names the cleaner is about to make or made, written before each link

    @classmethod
    def load(cls, cfg):
        cache = cls(cfg.dedupe_cache, cfg.dedupe_own)
        text = _read_own_file(cache.path)
        try:
            data = json.loads(text) if text else {}
        except ValueError:
            data = {}
        if isinstance(data, dict) and data.get('version') == cls.VERSION:
            for key, value in (data.get('digests') or {}).items() if isinstance(data.get('digests'), dict) else ():
                ident = _ident_key(key)
                if (ident and isinstance(value, list) and len(value) == 4 and all(isinstance(v, int) for v in value[:3])
                        and isinstance(value[3], str) and re.fullmatch('[0-9a-f]{64}', value[3])):
                    cache.digests[ident] = tuple(value)
            for key, value in (data.get('own') or {}).items() if isinstance(data.get('own'), dict) else ():
                ident = _ident_key(key)
                if ident and isinstance(value, int):
                    cache.own[ident] = value
            cache.parts = {p for p in data.get('parts') or () if isinstance(p, str)} if isinstance(data.get('parts'), list) else set()
        for line in (_read_own_file(cache.own_log) or '').splitlines():
            fields = line.split()
            if len(fields) == 3 and all(f.isdigit() for f in fields):
                cache.own[(int(fields[0]), int(fields[1]))] = int(fields[2])
            elif len(fields) == 2 and fields[0] == 'part':
                try:
                    cache.parts.add(os.fsdecode(bytes.fromhex(fields[1])))
                except ValueError:
                    pass
        return cache

    def _append(self, line):
        try:
            fd = os.open(self.own_log, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError:
            return False
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
            return True
        except OSError:
            return False
        finally:
            os.close(fd)

    def part(self, path):
        """The part name the next link makes, kept before the link: a run killed right after it still knows the part
        is its own. False when it cannot be kept (then no link is made)."""
        self.parts.add(path)
        return self._append(f'part {os.fsencode(path).hex()}\n')

    def record(self, ident, ctime_ns):
        """The inode time the cleaner's own step just gave the file, kept at once (a run killed after it still
        knows it). A failed write only means the file waits its `days`."""
        self.own[ident] = ctime_ns
        self.recorded.add(ident)
        self._append(f'{ident[0]} {ident[1]} {ctime_ns}\n')

    def save(self, seen):
        """Writes the cache in one step (a new file renamed over the old one) after a run: the digests read or
        used in this run, and the own inode times of files this run saw or changed. Returns a problem or None."""
        own = {ident: c for ident, c in self.own.items() if (ident, c) in seen or ident in self.recorded}
        data = {'version': self.VERSION,
                'digests': {f'{d}:{i}': list(v) for (d, i), v in sorted(self.used.items())},
                'own': {f'{d}:{i}': c for (d, i), c in sorted(own.items())},
                'parts': sorted(p for p in self.parts if os.path.lexists(p))}
        folder = os.path.dirname(self.path)
        try:
            fd, new = tempfile.mkstemp(dir=folder, prefix=os.path.basename(self.path) + '.')
            try:
                with os.fdopen(fd, 'w') as f:
                    json.dump(data, f, separators=(',', ':'))
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(new, self.path)
            except BaseException:
                try:
                    os.unlink(new)
                except OSError:
                    pass
                raise
            try:
                os.unlink(self.own_log)
            except FileNotFoundError:
                pass
            dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except OSError as ex:
            return f'dedupe: could not save {self.path} ({ex.strerror}): the next run reads its files again'
        return None


def _ident_key(key):
    parts = key.split(':') if isinstance(key, str) else ()
    return (int(parts[0]), int(parts[1])) if len(parts) == 2 and all(p.isdigit() for p in parts) else None


def _read_own_file(path):
    """The text of the user's own regular file at `path`, or None (missing, a link, someone else's, unreadable)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
            return None
        chunks = []
        while True:
            chunk = os.read(fd, CHUNK)
            if not chunk:
                break
            chunks.append(chunk)
        return b''.join(chunks).decode('utf-8', 'replace')
    except OSError:
        return None
    finally:
        os.close(fd)


class Run:
    """What one plan and its run share. `names` {ident: {path: folder}}: every name of each kept file (checked again,
    all of them, before each link); `ctime` {ident: ctime_ns}: its inode time at the plan, or after this run's own
    last step on it (any other change stops a link); `seen`: (ident, ctime_ns) of every file the walk saw."""

    def __init__(self, cache):
        self.cache = cache
        self.names, self.ctime, self.seen = {}, {}, set()

    def mark(self, ident, ctime_ns):
        self.ctime[ident] = ctime_ns
        self.cache.record(ident, ctime_ns)


def _changed(st, run):
    """When the file last changed (guard.changed), not counting the cleaner's own links: a file whose inode time is
    still the one the cleaner's own last step gave it is judged by its content time."""
    if run is not None and run.cache.own.get((st.st_dev, st.st_ino)) == st.st_ctime_ns:
        return st.st_mtime
    return guard.changed(st)


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
    refusal = guard.above_refusal(path, mounts, folder.layered)
    if refusal is not None:
        return None, refusal
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
    try:
        found = guard.marker(here)
    except OSError as ex:
        return Refusal('could not read', f'{here}: {ex.strerror}')
    if found is not None and found[0] == 'KEEP file':
        return Refusal('KEEP file', f'{found[1]} ({here})')
    if found is not None:
        return Refusal('git folder', f'{here}: git needs its files as they are')
    return _stuck(here)


def _file_answer(cfg, facts, folder, path, st, names, run=None):
    """For one regular file: None when the rule does not look at it (small, changed lately, a part file), a Refusal
    when it is kept, or the reason it may be linked."""
    name = os.path.basename(path)
    if name.endswith(PART) or st.st_size < MIN_BYTES:
        return None
    age = facts.now - _changed(st, run)
    if age < folder.days * DAY:
        return None
    refusal = _name_refusal(cfg, facts, folder, path, st, names)
    if refusal is not None:
        return refusal
    return f'not changed for {_days(int((age + 1) // DAY))} ({folder.path}: {_days(folder.days)})'


def _name_refusal(cfg, facts, folder, path, st, names):
    """Why the file may not be linked, or be linked to, through this name (its age aside); None when it may."""
    name = os.path.basename(path)
    if guard.kept_name(cfg, name):
        return Refusal('must keep', name)
    if matches(name, CREDENTIAL_NAMES):
        return Refusal('credential name', name)
    if matches(name, LOCK_NAMES):
        return Refusal('lock file', name)
    if matches(name, IN_PLACE + cfg.dedupe_in_place) or any(name + s in names for s in SIDECARS):
        return Refusal('written in place', 'a kind written in place (a database, journal, log, disk image or a '
                                           'kind the machine file names): a hard link would share its next write')
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'uid {guard.owner(st)}')
    if not st.st_mode & stat.S_IRUSR:
        return Refusal('unreadable', f'its owner may not read it (mode {stat.S_IMODE(st.st_mode):04o})')
    if len(os.fsencode(name)) + len('..12345678' + PART) > NAME_MAX:
        return Refusal('name too long', 'no room for the name of its part')
    refusal = _stuck(path)
    if refusal is not None:
        return refusal
    return _in_use(facts, folder, path, st)


def _part_answer(cfg, facts, folder, path, st, run):
    """For a part file a run left: the reason to remove it, or a Refusal when it holds the only copy of its content,
    is not one the cleaner made, or may not be removed."""
    if guard.owner(st) != os.getuid():
        return Refusal('another owner', f'uid {guard.owner(st)}')
    if st.st_nlink < 2:
        return Refusal('part without its file', 'its other names are gone, so it holds the only copy of its content: '
                                                'left as it is (rename it, or delete it by hand)')
    if path not in run.cache.parts:
        return Refusal('not a part', 'a name like a part, but not one the cleaner wrote down before it made one (or '
                                     'its cache is gone): left as it is')
    refusal = _in_use(facts, folder, path, st)
    if refusal is not None:
        return refusal
    return f'a link an earlier run made and did not rename ({st.st_nlink - 1} other names hold its content)'


def _look(st):
    look = guard.Look(size=st.st_blocks * 512, newest=guard.changed(st))
    look.ident = (st.st_dev, st.st_ino)
    look.stamp = (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
    return look


def _flags(fd):
    """The chattr flags a person can set, of an open file (0 where the filesystem has none)."""
    try:
        got = fcntl.ioctl(fd, FS_IOC_GETFLAGS, struct.pack('i', 0))
    except OSError as ex:
        if ex.errno in (errno.ENOTTY, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOSYS):
            return 0
        raise
    return struct.unpack('i', got[:4])[0] & FLAGS_MASK


def _meta(fd):
    """What hard links share besides the bytes, of an open file: mode, owner, group, extended attributes (values),
    chattr flags."""
    st = os.fstat(fd)
    attrs = tuple(sorted((name, os.getxattr(fd, name)) for name in xattrs(fd)))
    return (stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid, attrs, _flags(fd))


def _digest(path, st, run):
    """(sha256 of its bytes, its _meta), read through one descriptor; Refusal when it changed while it was read or
    could not be read. The sha256 comes from the cache when the file's identity, size and both times are the ones
    it was read with (it only finds candidates: the link compares byte for byte)."""
    try:
        fd = os.open(path, READ)
    except OSError as ex:
        return Refusal('could not read', ex.strerror)
    try:
        if not _same(os.fstat(fd), _look(st)):
            return Refusal('changed', 'it changed while it was read: it stays as it is')
        ident, stamp = (st.st_dev, st.st_ino), (st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        cached = run.cache.digests.get(ident)
        if cached is not None and tuple(cached[:3]) == stamp:
            hexdigest = cached[3]
        else:
            digest = hashlib.sha256()
            while True:
                chunk = os.read(fd, CHUNK)
                if not chunk:
                    break
                digest.update(chunk)
            hexdigest = digest.hexdigest()
        if not _same(os.fstat(fd), _look(st)):
            return Refusal('changed', 'it changed while it was read: it stays as it is')
        run.cache.used[ident] = stamp + (hexdigest,)
        return hexdigest, _meta(fd)
    except OSError as ex:
        return Refusal('could not read', ex.strerror)
    finally:
        os.close(fd)


def _walk(cfg, facts, folder, plan, measure, files, parts, run):
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
                continue
            if not stat.S_ISREG(est.st_mode):
                continue
            run.seen.add(((est.st_dev, est.st_ino), est.st_ctime_ns))
            if PART_NAME.match(entry.name):
                parts.append((folder, entry.path, est))
            else:
                answer = _file_answer(cfg, facts, folder, entry.path, est, names, run)
                if answer is not None:
                    files.append((folder, entry.path, est, answer))


def plan_dedupe(cfg, facts, plan, measure):
    from .rules import Item, Kept
    if not cfg.dedupe_folders:
        return
    if cfg.dedupe_in_place is None:
        for folder in cfg.dedupe_folders:
            plan.kept.append(Kept(folder.path, RULE, 'could not read', MISSING_KINDS))
        return
    run = Run(Cache.load(cfg))
    plan.after.append(lambda: run.cache.save(run.seen))
    files, parts = [], []
    for folder in cfg.dedupe_folders:
        _walk(cfg, facts, folder, plan, measure, files, parts, run)
    # Every name of each file seen in the folders, with the rule's answer for that name (parts too). A file is kept,
    # and nothing is joined to it, unless the folders hold all its names and the rule takes each one of them.
    answers = {}
    for folder, path, st in parts:
        answer = _part_answer(cfg, facts, folder, path, st, run)
        answers.setdefault((st.st_dev, st.st_ino), []).append((folder, path, answer))
        if isinstance(answer, Refusal):
            plan.kept.append(Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512))
        else:
            item = Item('link-part', path, RULE, answer, 0, _look(st))      # frees nothing: another name holds it
            item.folder, item.ctx, item.run = folder, (cfg, facts), run
            plan.items.append(item)
    for folder, path, st, answer in files:
        answers.setdefault((st.st_dev, st.st_ino), []).append((folder, path, answer))
    for item in plan.items:         # a part's removal is this run's own change of its file (remove_part)
        if item.rule == RULE and item.kind == 'link-part':
            run.names.setdefault(item.look.ident, {p: f for f, p, _ in answers[item.look.ident]})
            run.ctime.setdefault(item.look.ident, item.look.stamp[2])
    # Only files of one size on one filesystem can be the same; only those are read.
    by_size = {}
    for entry in files:
        by_size.setdefault((entry[2].st_dev, entry[2].st_size), []).append(entry)
    linked = False
    for (_, size), group in sorted(by_size.items()):
        if len({(e[2].st_dev, e[2].st_ino) for e in group}) < 2:
            continue
        same = {}
        for folder, path, st, answer in group:
            if isinstance(answer, Refusal):
                plan.kept.append(Kept(path, RULE, answer.code, answer.detail, st.st_blocks * 512))
                continue
            got = _digest(path, st, run)
            if isinstance(got, Refusal):
                plan.kept.append(Kept(path, RULE, got.code, got.detail, st.st_blocks * 512))
                continue
            same.setdefault(got[0], []).append((folder, path, st, answer, got[1]))
        for digest, matched in sorted(same.items()):
            linked = _link_group(cfg, facts, plan, digest, matched, answers, run) or linked
    if linked:
        plan.notes.append(WARNING)


def _names_refusal(names, nlink):
    """Why a file with these (folder, path, answer) names and `nlink` names in all is never the file kept; None when
    it may be."""
    if nlink > len(names):
        return f'{nlink} names, some of them outside dedupe.folders: a link would join files elsewhere'
    refused = next(((path, a) for _, path, a in names if isinstance(a, Refusal)), None)
    if refused is not None:
        return (f'{nlink} names, and the rule keeps {refused[0]} ({refused[1].code}): a link would share that name\'s '
                'writes')
    return None


def _link_group(cfg, facts, plan, digest, matched, answers, run):
    """Files with the same sha256: each set of them with the same metadata keeps one file (of those whose every name
    is in the folders and taken by the rule: the one with the most names, then the oldest), and every other file with
    one name becomes a link of it. True when it plans a link."""
    from .rules import Item, Kept
    if len({(e[2].st_dev, e[2].st_ino) for e in matched}) < 2:
        return False                # names of one file already
    planned = False
    by_meta = {}
    for entry in matched:
        by_meta.setdefault(entry[4], []).append(entry)
    for meta, entries in sorted(by_meta.items(), key=lambda kv: min(e[1] for e in kv[1])):
        inodes = {}
        for entry in entries:
            inodes.setdefault((entry[2].st_dev, entry[2].st_ino), []).append(entry)
        if len(inodes) < 2:
            if len(by_meta) > 1:
                other = next(e[1] for m, es in sorted(by_meta.items(), key=lambda kv: min(e[1] for e in kv[1]))
                             if m != meta for e in es)
                for _, path, st, _, _ in entries:
                    plan.kept.append(Kept(path, RULE, 'other metadata', f'the same bytes as {other}, but another mode, '
                                          'owner, group, extended attributes or chattr flags: a link would give it those',
                                          st.st_blocks * 512))
            continue
        inside = [es for ident, es in inodes.items() if _names_refusal(answers[ident], es[0][2].st_nlink) is None]
        keeper = None
        if inside:
            keeper = min(inside, key=lambda es: (-es[0][2].st_nlink, es[0][2].st_mtime_ns, min(e[1] for e in es)))
            kfolder, kpath, kst = min(keeper, key=lambda e: e[1])[:3]
            kident = (kst.st_dev, kst.st_ino)
        for ident, names in sorted(inodes.items(), key=lambda kv: min(e[1] for e in kv[1])):
            if names is keeper:
                continue
            for folder, path, st, answer, _ in names:
                if st.st_nlink > 1:
                    why = _names_refusal(answers[ident], st.st_nlink) or (f'{st.st_nlink} names, linking one of them '
                                                                          'frees nothing')
                    plan.kept.append(Kept(path, RULE, 'hard-linked', why, st.st_blocks * 512))
                    continue
                if keeper is None:
                    continue
                if kident not in run.names:
                    run.names[kident] = {p: f for f, p, _ in answers[kident]}
                    run.ctime[kident] = kst.st_ctime_ns
                item = Item('dedupe', path, RULE, f'the same bytes as {kpath}, {answer}', st.st_blocks * 512, _look(st))
                item.folder, item.keeper, item.keeper_folder = folder, kpath, kfolder
                item.keeper_look, item.digest, item.ctx, item.run = _look(kst), digest, (cfg, facts), run
                plan.items.append(item)
                planned = True
    return planned


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


def _keeper_same(st, item):
    """The kept file as the plan saw it, or as this run's own last step left it: the same file, size and content
    time, the inode time the plan saw or this run's own last link gave it, and as many names as are known (a name
    made or removed by anyone else since stops the link)."""
    run, look = item.run, item.keeper_look
    return ((st.st_dev, st.st_ino) == look.ident and (st.st_size, st.st_mtime_ns) == look.stamp[:2]
            and st.st_ctime_ns == run.ctime.get(look.ident) and st.st_nlink == len(run.names.get(look.ident, ())))


def _keeper_refusal(cfg, facts, item):
    """Every name of the kept file read again from its configured folder down: each still a name of it, as the plan
    saw it (or this run's own links left it), and each one a name the rule takes. Its age is not judged again: the
    plan did, and since then only this run's own links may have changed its inode time (_keeper_same)."""
    names = item.run.names.get(item.keeper_look.ident, {})
    for kpath, kfolder in sorted(names.items()):
        got = _recheck_path(cfg, facts, kfolder.path, kpath)
        if isinstance(got, Refusal):
            return Refusal(got.code, f'the kept file {item.keeper}: its name {kpath}: {got.detail}')
        _, kst, knames = got
        if not _keeper_same(kst, item):
            return Refusal('not a candidate', f'the kept file {item.keeper} changed, was replaced, or got or lost a '
                                              f'name since the plan ({kpath}: {kst.st_nlink} names, {len(names)} known)')
        refusal = _name_refusal(cfg, facts, kfolder, kpath, kst, knames)
        if refusal is not None:
            return Refusal(refusal.code, f'the kept file {item.keeper}: its name {kpath}: {refusal.detail}')
    if item.keeper not in names:
        return Refusal('not a candidate', f'the kept file {item.keeper} is not a name of it any more')
    return None


def recheck(cfg, facts, item):
    """Everything about one item read again: the folders from the configured folder down, the duplicate and every
    name of the kept file. None when it may still be done."""
    got = _recheck_path(cfg, facts, item.folder.path, item.path)
    if isinstance(got, Refusal):
        return got
    folder, st, names = got
    if item.kind == 'link-part':
        if not _same(st, item.look):
            return Refusal('not a candidate', 'it changed since the plan')
        answer = _part_answer(cfg, facts, folder, item.path, st, item.run)
        return answer if isinstance(answer, Refusal) else None
    if not _same(st, item.look) or st.st_nlink != 1:
        return Refusal('not a candidate', 'it changed, got another name, or was replaced since the plan')
    answer = _file_answer(cfg, facts, folder, item.path, st, names, item.run)
    if answer is None:
        return Refusal('not a candidate', 'the rule no longer takes it')
    if isinstance(answer, Refusal):
        return answer
    return _keeper_refusal(cfg, facts, item)


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


def _mark_names(run, ident):
    """After this run's own change of a file's names: its inode time, read through one of its known names, is this
    run's own (_changed). A name not found: nothing kept, so the file waits its `days` (the safe side)."""
    for name in sorted(run.names.get(ident, ())):
        try:
            st = os.lstat(name)
        except OSError:
            continue
        if (st.st_dev, st.st_ino) == ident:
            run.mark(ident, st.st_ctime_ns)
            return


def _leases_work(parent):
    """True when this folder's filesystem grants a write lease on a file nobody else has open (a file without a
    name, gone when closed): only then does a refused lease mean the file is open. Unknown: True (kept)."""
    try:
        fd = os.open('.', os.O_TMPFILE | os.O_RDWR | os.O_CLOEXEC, 0o600, dir_fd=parent)
    except (OSError, AttributeError):
        return True
    try:
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
        return True
    except OSError:
        return False
    finally:
        os.close(fd)


class _Leases:
    """Write leases (fcntl F_SETLEASE) on the duplicate and the kept file, from after the last read of the process
    list until the rename. The kernel grants one only while no other descriptor of the file is open (through any
    name, by any process), and any later open breaks it. So a file opened in that time is seen at the last check and
    the duplicate stays. Both files are looked at once more after the leases (a write before a lease was taken shows
    in their times). What is left: an open between the last look at the leases and the rename (a few system calls:
    the names and the part are read in between); a name of the kept file moved after the last look at the part (one
    system call). Where the filesystem or the process takes no leases (a thread, leases off), the run goes on without
    them: the process list was read just before, and a write that sets the content time back between the look
    before the link and the look after it (one system call), or in the same tick of the inode clock as the link (a
    few milliseconds), is not seen."""

    def __init__(self):
        self.fds, self.old = [], None

    def take(self, parent, name, ident, which):
        if self.old is None:
            try:
                # A broken lease sends SIGIO, whose default ends the process: ignored while leases are held.
                self.old = signal.signal(signal.SIGIO, signal.SIG_IGN)
            except ValueError:
                self.old = False        # not the main thread: no leases
        if self.old is False:
            return
        try:
            fd = os.open(name, READ, dir_fd=parent)
        except OSError as ex:
            raise Untouched(f'{which}: {ex.strerror}: nothing changed') from None
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != ident:
            os.close(fd)
            raise Untouched(f'{which} is not what the last check saw: nothing changed')
        try:
            fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_WRLCK)
        except OSError as ex:
            os.close(fd)
            if ex.errno in (errno.EAGAIN, errno.EBUSY) and _leases_work(parent):
                raise Untouched(f'{which}: in use: another descriptor of it is open (a process has it open): '
                                'nothing changed') from None
            return                      # this filesystem or process takes no leases
        self.fds.append((fd, which))

    def broken(self):
        """The first file whose lease another open broke, or None."""
        for fd, which in self.fds:
            if fcntl.fcntl(fd, fcntl.F_GETLEASE) != fcntl.F_WRLCK:
                return which
        return None

    def release(self):
        for fd, _ in self.fds:
            try:
                fcntl.fcntl(fd, fcntl.F_SETLEASE, fcntl.F_UNLCK)
            except OSError:
                pass
            os.close(fd)
        self.fds = []
        if self.old not in (None, False):
            signal.signal(signal.SIGIO, self.old)
        self.old = None


def link(item):
    """Replaces the duplicate item.path by a hard link of item.keeper once both hold the same bytes, compared byte
    for byte. Raises Untouched when the duplicate is still there as it was."""
    from .rules import _open_parent
    cfg, facts = item.ctx
    run = item.run
    path, name = item.path, os.path.basename(item.path)
    kname = os.path.basename(item.keeper)
    kident = item.keeper_look.ident
    knames = run.names[kident]
    leases = _Leases()
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
            # The folders of every name again too: a KEEP file or a git folder made while the bytes were compared.
            facts.forget()
            for fpath, fdir in [(path, item.folder)] + sorted(knames.items()):
                got = _recheck_path(cfg, facts, fdir.path, fpath)
                if isinstance(got, Refusal):
                    raise Untouched(f'{fpath}: {got.code}: {got.detail}: nothing changed')
            st = _stat_at(parent, name)
            if st is None:
                raise Untouched(f'{path} is gone: nothing changed')
            refusal = _in_use(facts, item.folder, path, st)
            if refusal is not None:
                raise Untouched(f'{path}: {refusal.code}: {refusal.detail}: nothing changed')
            for kpath, kfolder in sorted(knames.items()):      # every name of the kept file
                try:
                    kst = os.lstat(kpath)
                except OSError:
                    kst = None
                if kst is None or not _keeper_same(kst, item):
                    raise Untouched(f'{item.keeper}: its name {kpath} is not what the last check saw: nothing changed')
                refusal = _in_use(facts, kfolder, kpath, kst)
                if refusal is not None:
                    raise Untouched(f'{item.keeper}: its name {kpath}: {refusal.code}: {refusal.detail}: nothing changed')
            leases.take(parent, name, item.look.ident, path)
            leases.take(kparent, kname, kident, item.keeper)
            part = f'.{name}.{secrets.token_hex(4)}{PART}'
            if not run.cache.part(os.path.join(os.path.dirname(path), part)):
                raise Untouched(f'{run.cache.own_log}: could not be written, so a part could not be named: nothing changed')
            # Both files once more, after both leases and right before the link: a write since the comparison (an
            # open, write and close before a lease was taken, even one that set the content time back) changed the
            # size, the content time or the inode time. From the lease on, a write needs an open, which breaks it.
            now, kst = _stat_at(parent, name), _stat_at(kparent, kname)
            if now is None or not _same(now, item.look) or now.st_nlink != 1:
                raise Untouched(f'{path} is not what the last check saw: nothing changed')
            if kst is None or not _keeper_same(kst, item):
                raise Untouched(f'{item.keeper} is not what the last check saw: nothing changed')
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
        run.mark(kident, pst.st_ctime_ns)       # the part's link: this run's own change, so a kill still knows it
        # Read just before the rename, in this order: the duplicate (an editor's save onto it since the comparison),
        # the leases (an open of either file), every name of the kept file (each still a name of it: one moved,
        # renamed or replaced is seen), and last the part: the kept file's size and content time as compared and its
        # inode time as this run's link left it (any write, link, rename or removal of any of its names since changes
        # that), and as many names as known.
        now = _stat_at(parent, name)
        why = None
        if now is None or not _same(now, item.look) or now.st_nlink != 1:
            why = f'{path} changed while it was compared'
        else:
            opened = leases.broken()
            if opened is not None:
                why = f'{opened} was opened while it was linked'
        if why is None:
            for kpath in sorted(knames):
                try:
                    kst = os.lstat(kpath)
                except OSError:
                    kst = None
                if kst is None or (kst.st_dev, kst.st_ino) != kident:
                    why = f'the kept file {item.keeper}: its name {kpath} was moved or replaced while it was linked'
                    break
        if why is None:
            pst = _stat_at(parent, part)
            if pst is None or (pst.st_dev, pst.st_ino) != kident or pst.st_nlink != len(knames) + 1:
                why = f'the kept file {item.keeper} got or lost a name while it was linked'
            elif ((pst.st_size, pst.st_mtime_ns) != item.keeper_look.stamp[:2]
                  or pst.st_ctime_ns != run.ctime.get(kident)):
                why = f'the kept file {item.keeper} changed while it was linked'
        if why is not None:
            _unlink_part(parent, part, kident)
            _mark_names(run, kident)
            raise Untouched(f'{why}: it stays as it is')
        try:
            os.rename(part, name, src_dir_fd=parent, dst_dir_fd=parent)
        except OSError as ex:
            _unlink_part(parent, part, kident)
            _mark_names(run, kident)
            raise Untouched(f'{path}: {ex.strerror}: the duplicate stays') from None
        os.fsync(parent)
        knames[path] = item.folder
        done = _stat_at(parent, name)
        if done is not None and (done.st_dev, done.st_ino) == kident:
            run.mark(kident, done.st_ctime_ns)
    finally:
        leases.release()
        os.close(parent)
    item.result = (f'{path} ({human(item.size)}, {RULE}: {item.why}; now a hard link of {item.keeper}, compared byte '
                   'for byte, both unchanged at the last look before the rename)')


def _compare(parent, name, kparent, kname, item):
    """Untouched unless the duplicate and the kept file are still what the plan saw and hold the same bytes, mode,
    owner, group, extended attributes and chattr flags."""
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
            if not stat.S_ISREG(bst.st_mode) or not _keeper_same(bst, item):
                raise Untouched(f'{item.keeper} is not what the last check saw: nothing changed')
            try:
                equal = _equal(a, b, ast.st_size)
                meta = _meta(a) == _meta(b)
            except OSError as ex:
                raise Untouched(f'{path}: {ex.strerror}: nothing changed') from None
            if not _same(os.fstat(a), item.look) or not _keeper_same(os.fstat(b), item):
                raise Untouched(f'{path} or {item.keeper} changed while they were compared: nothing changed')
            if not equal:
                raise Untouched(f'{path} does not hold the same bytes as {item.keeper} (compared byte for byte): '
                                'nothing changed')
            if not meta:
                raise Untouched(f'{path} has another mode, owner, group, extended attributes or chattr flags than '
                                f'{item.keeper}: nothing changed')
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
    # The removal is this run's own change of the file's names and inode time: a kept file it is a name of goes on
    # with one name fewer and the new time.
    run, ident = item.run, item.look.ident
    run.names.get(ident, {}).pop(item.path, None)
    _mark_names(run, ident)
