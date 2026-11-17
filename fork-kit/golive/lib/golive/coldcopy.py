"""`t3-golive copy`: the practice stop. At a quiet point it stops the boot service, copies the T3 home's data
(userdata/: the database with its WAL, settings and the other small files) into a new folder in backupDir, reads
both sides back (SHA256 must match), writes the manifest LAST, starts the same build again and checks it runs.

A copy folder without a finished manifest is incomplete and is never used (incomplete_copies lists them; the kit
deletes no data). A copy never goes over an old one, and restore() keeps a copy of every file it replaces under a
name with a time in it (kept_files lists them), and records which kept files are the old home until it is done.

Every change is made inside a journalled step (journal.py); the checks and the wait for a quiet point come first.
A guard (a second detached unit, started before T3 stops) waits for the run to end however it ends and starts T3
again if the run left it stopped, so a killed copy cannot leave T3 down.
"""
import datetime, hashlib, json, os, sqlite3, stat, subprocess, sys, time

from golive import check as check_mod, journal, live, osadapter

KIT_BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))), 'bin',
                       't3-golive')
MANIFEST, MANIFEST_VERSION = 'manifest.json', 1
OWNER_LOCK = 'server-owner.sqlite'  # upstream's ownership lock (serverOwnership.ts); never copied, never deleted
# Not copied: logs, this server's runtime record and ownership lock, and SQLite's -shm: it is only a shared-memory
# index that SQLite rebuilds from the WAL when the first connection opens the database, so a cold copy never needs it.
# Also never copied: a restore's leftover temp files (.<name>.golive-restore-<time>, from a restore stopped half-way)
# and the old files a restore kept (<name>.golive-aside-<time>): old data, not the home's, and as big as the database,
# and the record of a restore stopped half-way (RESTORE_MARK; a copy refuses while it is there).
RESTORE_TEMP, ASIDE = '.golive-restore-', '.golive-aside-'
RESTORE_MARK = 'golive-restore-unfinished.json'  # in userdata/ from before a restore's first removal until it is done
SKIP = frozenset({'logs', 'server-runtime.json', OWNER_LOCK, OWNER_LOCK + '-journal', RESTORE_MARK})
QUIET_GAP, STOP_WAIT, START_WAIT, POLL = 60, 120, 180, 1.0  # seconds; tests make them small
GUARD_WAIT = 30  # seconds for the guard to say it waits, before T3 may stop
GUARD_LOG = 'guard.log'
HOOK_TIMEOUT, QUIET_TIMEOUT, CHUNK = 120, 30, 8 << 20
DEFAULT_RESTORE = (live.DATABASE, 'settings.json')
MB = 1024 * 1024
_clock = time.monotonic  # the stop limit's clock; tests change it


class Incomplete(Exception):
    """A copy that must not be used: no finished manifest, or files that do not match it."""


class WrongCopy(Exception):
    """A finished copy, but of another home or another T3 version than the caller names."""


def _stamp():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')


def _fsync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _hash(path, deadline=None, drop_cache=False):
    """SHA256 of a file, read from disk (with drop_cache, the page cache is dropped first where the OS allows)."""
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        if drop_cache and hasattr(os, 'posix_fadvise'):
            os.posix_fadvise(f.fileno(), 0, 0, os.POSIX_FADV_DONTNEED)
        for chunk in iter(lambda: f.read(CHUNK), b''):
            _in_time(deadline)
            h.update(chunk)
    return h.hexdigest()


def _in_time(deadline):
    if deadline is not None and _clock() > deadline:
        raise TimeoutError('stopLimitSeconds passed; T3 is started again and this copy stays incomplete')


def _copy_file(src, dst, deadline, mode):
    """Copy src to a new file dst (never over one) and return (bytes, sha256 of what was read); fsynced."""
    h, size = hashlib.sha256(), 0
    fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC, 0o600)
    try:
        with open(src, 'rb') as f:
            for chunk in iter(lambda: f.read(CHUNK), b''):
                _in_time(deadline)
                h.update(chunk)
                size += len(chunk)
                view = memoryview(chunk)
                while view:
                    view = view[os.write(fd, view):]
        os.fchmod(fd, mode)
        os.fsync(fd)
    finally:
        os.close(fd)
    return size, h.hexdigest()


def _raise(error):
    raise error  # os.walk's onerror: an unreadable folder stops the copy instead of being skipped in silence


def plan(userdata, skip=frozenset()):
    """What a copy takes from userdata/: [{'path', 'kind', 'bytes', 'mode'[, 'target']}] top-down, and the total
    bytes. Plain files, folders and links; anything else (a socket, a fifo) is left out and named. A folder that
    cannot be read raises OSError (a copy that says complete must hold everything)."""
    entries, total, left_out = [], 0, []
    for d, dirs, names in os.walk(userdata, onerror=_raise):
        rel_d = os.path.relpath(d, userdata)
        for n in sorted(dirs + names):
            rel = n if rel_d == '.' else f'{rel_d}/{n}'
            if rel_d == '.' and (n in SKIP or n in skip or n.endswith('-shm') or ASIDE in n
                                 or (n.startswith('.') and RESTORE_TEMP in n)):
                if n in dirs:
                    dirs.remove(n)
                continue
            st = os.lstat(os.path.join(d, n))
            mode = stat.S_IMODE(st.st_mode)
            if stat.S_ISLNK(st.st_mode):
                entries.append({'path': rel, 'kind': 'link', 'target': os.readlink(os.path.join(d, n))})
            elif stat.S_ISDIR(st.st_mode):
                entries.append({'path': rel, 'kind': 'dir', 'mode': mode})
            elif stat.S_ISREG(st.st_mode):
                entries.append({'path': rel, 'kind': 'file', 'bytes': st.st_size, 'mode': mode})
                total += st.st_size
            else:
                left_out.append(rel)
        dirs.sort()
    return entries, total, left_out


def copy_userdata(run, home, dest, deadline, skip=frozenset()):
    """The cold copy (in a step): every planned entry into dest/userdata, hashing what is read. dest is new.
    skip: more top-level names to leave out (rehearse's clone leaves out secrets/)."""
    run.require_step('cold copy')
    userdata = os.path.join(home, 'userdata')
    entries, total, left_out = plan(userdata, skip)
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    os.mkdir(dest, 0o700)  # FileExistsError: never over an old copy
    os.mkdir(os.path.join(dest, 'userdata'), 0o700)
    for e in entries:
        src, dst = os.path.join(userdata, e['path']), os.path.join(dest, 'userdata', e['path'])
        if e['kind'] == 'dir':
            os.mkdir(dst, 0o700)
        elif e['kind'] == 'link':
            os.symlink(e['target'], dst)
        else:
            size, e['sha256'] = _copy_file(src, dst, deadline, e['mode'])
            if size != e['bytes']:
                raise RuntimeError(f'{src} changed size while T3 was stopped ({e["bytes"]} -> {size} bytes)')
    for d, dirs, _ in os.walk(os.path.join(dest, 'userdata'), onerror=_raise):
        _fsync_dir(d)
    _fsync_dir(dest)
    _fsync_dir(os.path.dirname(dest))
    return entries, total, left_out


def read_back(home, dest, entries, deadline):
    """Read both sides from disk again: the copy and the source must both match the hash taken while copying."""
    for e in entries:
        if e['kind'] != 'file':
            continue
        for side in (os.path.join(dest, 'userdata', e['path']), os.path.join(home, 'userdata', e['path'])):
            got = _hash(side, deadline, drop_cache=True)
            if got != e['sha256']:
                raise RuntimeError(f'read-back: {side} has sha256 {got}, the copy read {e["sha256"]}')


def write_manifest(run, dest, manifest):
    """The last write of a copy: manifest.json through a temp file and a rename, fsynced."""
    run.require_step('manifest')
    tmp = os.path.join(dest, f'.{MANIFEST}.tmp')
    with open(tmp, 'x', encoding='utf-8') as f:
        json.dump(manifest, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, os.path.join(dest, MANIFEST))
    _fsync_dir(dest)


def read_manifest(copy_dir, home=None, live_version=None):
    """The manifest of a finished copy; Incomplete when it has none, or one this kit cannot read. With home and
    live_version, WrongCopy unless the copy was taken of that home while that T3 version served it."""
    path = os.path.join(copy_dir, MANIFEST)
    try:
        with open(path, encoding='utf-8') as f:
            m = json.load(f)
    except FileNotFoundError:
        raise Incomplete(f'{copy_dir} has no {MANIFEST}: the copy is incomplete and is never used') from None
    except (OSError, ValueError) as e:
        raise Incomplete(f'{path} cannot be read ({e}): the copy is not used') from None
    if not (isinstance(m, dict) and m.get('manifestVersion') == MANIFEST_VERSION and m.get('complete') is True
            and isinstance(m.get('files'), list)):
        raise Incomplete(f'{path} is not a finished manifest of version {MANIFEST_VERSION}: the copy is not used')
    if home is not None and m.get('home') != os.path.normpath(home):
        raise WrongCopy(f'{copy_dir} is a copy of {m.get("home")}, not of {os.path.normpath(home)}')
    if live_version is not None and m.get('liveVersion') != live_version:
        raise WrongCopy(f'{copy_dir} was taken under t3 {m.get("liveVersion")}, not {live_version}: the data may '
                        'not match that build')
    return m


def verify_copy(copy_dir):
    """Read a finished copy back against its manifest; its manifest, or Incomplete naming the first difference."""
    m = read_manifest(copy_dir)
    for e in m['files']:
        if isinstance(e, dict) and e.get('kind') == 'file':
            path = os.path.join(copy_dir, 'userdata', str(e.get('path')))
            try:
                got = _hash(path, drop_cache=True)
            except OSError as err:
                raise Incomplete(f'{path} cannot be read ({err}): the copy is not used') from None
            if got != e.get('sha256'):
                raise Incomplete(f'{path} does not match its manifest: the copy is not used')
    return m


def incomplete_copies(backup_dir):
    """Read only: [(folder, bytes)] of the copy folders (t3-copy-*) in backup_dir with no finished manifest, left
    by a copy killed or failed half-way. They are never used, and the kit deletes no data: the owner removes them
    (a copy names them, and a refusal for free disk counts them)."""
    try:
        names = sorted(n for n in os.listdir(backup_dir) if n.startswith('t3-copy-'))
    except FileNotFoundError:
        return []
    found = []
    for n in names:
        folder = os.path.join(backup_dir, n)
        if os.path.islink(folder) or not os.path.isdir(folder):
            continue
        try:
            read_manifest(folder)
            continue
        except Incomplete:
            pass
        size = 0
        for d, _, files in os.walk(folder):
            for f in files:
                try:
                    size += os.lstat(os.path.join(d, f)).st_size
                except OSError:
                    pass
        found.append((folder, size))
    return found


def kept_files(home):
    """Read only: [(path, bytes)] of the files restores kept in userdata/ (<name>.golive-aside-<time>[-n]), each
    as big as what it kept. Never copied and never removed by the kit: the owner removes them once sure, except
    those a restore stopped half-way names in RESTORE_MARK (the old home until that restore is run again)."""
    userdata = os.path.join(home, 'userdata')
    try:
        names = sorted(n for n in os.listdir(userdata) if ASIDE in n and not n.startswith('.'))
    except FileNotFoundError:
        return []
    found = []
    for n in names:
        try:
            found.append((os.path.join(userdata, n), os.lstat(os.path.join(userdata, n)).st_size))
        except FileNotFoundError:
            pass
    return found


def stopped_restore(home, copy_dir=None):
    """Read only: the record (RESTORE_MARK) a restore stopped half-way left in userdata/, or None. Its 'oldHome'
    names the kept files that are the home as it was before that restore; its 'alsoKept' (when there) what reruns
    kept from the half-way home; its 'names' the files that restore put back; its 'sums' the SHA256 of each kept
    file; its 'copy' and 'alsoCopies' the copies it and its reruns restored from. None as well when the old home
    was moved back by hand (see _read_record; copy_dir, the copy about to be restored, counts as one of the record's
    copies). RuntimeError when the record cannot be read (which kept files are the old home is then unknown), or
    when the old home was put back only in part or with a file of the half-way home left: the owner looks."""
    record, stale = _read_record(home, copy_dir)
    return None if stale else record


def _file_sum(path):
    """SHA256 of a regular file, None for anything else or nothing."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return None
    except FileNotFoundError:
        return None
    return _hash(path, drop_cache=True)


def _copy_sums(mark, copy_dir=None):
    """({name: {SHA256}} of the files the copies a restore record names (and copy_dir) hold, by their manifests;
    [those copies whose manifest cannot be read, so what they hold is unknown]; {name: {first WAL_HEADER bytes}} of
    each -wal those copies hold that still matches its manifest SHA256)."""
    copies = [mark.get('copy')] + list(mark.get('alsoCopies', [])) + ([copy_dir] if copy_dir else [])
    sums, unread, heads = {}, [], {}
    for c in dict.fromkeys(os.path.normpath(c) if isinstance(c, str) else c for c in copies):
        try:
            if not isinstance(c, str):
                raise Incomplete('the record names no copy')
            m = read_manifest(c)
        except Incomplete:
            unread.append(c if isinstance(c, str) else '(the record names no copy)')
            continue
        for e in m['files']:
            if isinstance(e, dict) and e.get('kind') == 'file' and isinstance(e.get('path'), str):
                sums.setdefault(e['path'], set()).add(e.get('sha256'))
                if e['path'].endswith('-wal') and '/' not in e['path']:
                    head = _wal_head(os.path.join(c, 'userdata', e['path']), e.get('sha256'))
                    if head is not None:
                        heads.setdefault(e['path'], set()).add(head)
    return sums, unread, heads


WAL_HEADER = 32  # SQLite's WAL header: magic, version, page size, checkpoint count, the two salts, its checksum


def _wal_head(path, sha256=None):
    """The WAL header of a regular file at path (None when it is shorter, not a regular file, or, with sha256, not
    those bytes). The salts in it are new each time SQLite starts a WAL over: a WAL that begins with a copy's header
    holds that copy's frames, whatever was appended after them."""
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode) or (sha256 is not None and _hash(path) != sha256):
            return None
        with open(path, 'rb') as f:
            head = f.read(WAL_HEADER)
    except OSError:
        return None
    return head if len(head) == WAL_HEADER else None


def _them(names, one='it', more='them'):
    return one if len(names) == 1 else more


def _read_record(home, copy_dir=None):
    """(record, stale) of RESTORE_MARK: (None, False) when there is none. Stale when the old home was put back by
    hand in full by moving: every file 'oldHome' names is gone from its kept name and back in the home (a kept -shm
    need not go back). A kept file gone while the home's file of that name holds the bytes of a copy's file (by the
    copy's manifest; one the record's copies restored, or copy_dir) and the kept file did not ('sums') is not moved
    back: it was deleted, and the home holds the half-way home's file. When a copy the record names cannot be read,
    a kept file gone is taken as moved back only when the home's file holds the kept file's bytes ('sums'). And the
    home then holds no -wal, -journal or -shm of a database the record names that the old home did not have: such a
    file is the half-way home's (the copy's WAL, or what a T3 started after the stop wrote), and next to the old
    database it would serve, or on the first checkpoint write, another database's pages.
    No kept file gone, and each (but a -shm) holding the same bytes as the home's file of its name: copied back, or
    a restore stopped once its kept files were written. The home is then the old home and the record stays live
    (a rerun keeps nothing again), but the same check for a file the old home did not have runs.
    RuntimeError, before any data changes, when the record cannot be read, or the old home is put back only in part (a
    rerun would keep that mix as the old home and name no more the kept files still out, such as the WAL with the
    commits), or a file of the half-way home is left beside it; the message names the files and whose each is: the
    copy's by its SHA256 (remove it, keep the record), the copy's with more frames by its WAL header (move it out,
    keep the record), a stray -shm (always safe to remove), or one that cannot be told (both readings, and how to
    tell them apart on scratch copies)."""
    userdata = os.path.join(home, 'userdata')
    path = os.path.join(userdata, RESTORE_MARK)
    try:
        with open(path, encoding='utf-8') as f:
            mark = json.load(f)
    except FileNotFoundError:
        return None, False
    except (OSError, ValueError) as e:
        mark = e
    def names_ok(v, aside=True):
        return isinstance(v, list) and all(isinstance(n, str) and (ASIDE in n or not aside) and '/' not in n
                                           and n not in ('', '.', '..') for n in v)
    if not (isinstance(mark, dict) and names_ok(mark.get('oldHome')) and names_ok(mark.get('alsoKept', []))
            and names_ok(mark.get('names', list(DEFAULT_RESTORE)), aside=False)
            and isinstance(mark.get('sums', {}), dict)
            and all(isinstance(v, str) for v in mark.get('sums', {}).values())
            and isinstance(mark.get('alsoCopies', []), list)
            and all(isinstance(c, str) for c in mark.get('alsoCopies', []))):
        raise RuntimeError(f'{path} cannot be read ({mark if isinstance(mark, Exception) else "not a record"}): a '
                           'restore stopped half-way and which kept files are the old home is unknown; look at the '
                           f'*{ASIDE}* files, put the home right, then remove it')
    def base(n):
        return n.split(ASIDE)[0]
    had = {base(n) for n in mark['oldHome']}
    foreign = [c for name in mark.get('names', DEFAULT_RESTORE) for c in _companions(name)
               if c not in had and os.path.lexists(os.path.join(userdata, c))]
    gone = [n for n in mark['oldHome'] if not os.path.lexists(os.path.join(userdata, n))]
    if not gone:
        # Nothing moved back. Every kept file (a -shm need not) the same bytes as the home's: copied back, or a
        # restore stopped right after keeping. Then only a file the old home did not have can be wrong (fork #244).
        kept = [n for n in mark['oldHome'] if not base(n).endswith('-shm')]
        if foreign and kept and all(_same_file(os.path.join(userdata, n), os.path.join(userdata, base(n)))
                                    for n in kept):
            _refuse_foreign(path, userdata, mark, copy_dir, foreign)
        return mark, False
    sums, unread, _ = _copy_sums(mark, copy_dir)
    kept_sums = mark.get('sums', {})
    def copys(name, digest):
        return digest is not None and digest in sums.get(name, ())
    out = [n for n in mark['oldHome'] if n not in gone and not base(n).endswith('-shm')
           and not _same_file(os.path.join(userdata, n), os.path.join(userdata, base(n)))]
    lost = [n for n in gone if not base(n).endswith('-shm') and not os.path.lexists(os.path.join(userdata, base(n)))]
    lost_copy = []  # deleted: the home's file of that name is the copy's, and the kept one was not
    unknown = []  # a copy cannot be read, and the home's file is not the kept bytes: moved back or deleted?
    for n in gone:
        if base(n).endswith('-shm') or n in lost:
            continue
        digest = _file_sum(os.path.join(userdata, base(n)))
        if copys(base(n), digest) and kept_sums.get(n) != digest:
            lost_copy.append(n)
        elif unread and digest is not None and kept_sums.get(n) != digest:
            unknown.append(n)
    if out or lost or lost_copy or unknown:
        said = ([f'{", ".join(out)} not back in the home'] if out else []) \
            + ([f'{", ".join(lost)} gone from the home too'] if lost else []) \
            + ([f'{", ".join(lost_copy)} not moved back: the home\'s '
                f'{", ".join(base(n) for n in lost_copy)} {_them(lost_copy, "holds", "hold")} the bytes of the '
                f'copy\'s file (its manifest SHA256), not the kept {_them(lost_copy, "one", "ones")}, so '
                f'{_them(lost_copy)} was deleted'] if lost_copy else []) \
            + ([f'{", ".join(unknown)}: the home\'s {", ".join(base(n) for n in unknown)} '
                f'{_them(unknown, "does", "do")} not hold the kept bytes (the SHA256 in the record), and with no '
                f'readable manifest in {", ".join(unread)} it cannot be told whether {_them(unknown, "it", "they")} '
                f'{_them(unknown, "was", "were")} moved back and written since, or deleted with the half-way home\'s '
                f'file left in {_them(unknown, "its", "their")} place; to tell a database or its -wal, ' + _SCRATCH]
               if unknown else [])
        raise RuntimeError(f'{path} names the kept files of the old home, but they are only partly put back '
                           f'({", ".join(gone)} gone from the kept files; ' + '; '.join(said) + '): '
                           'finish putting the kept files back (a rerun then names any -wal, -journal or -shm left '
                           'in the home that the old home did not have), or remove the record once the home is '
                           'right; nothing was changed')
    if foreign:
        _refuse_foreign(path, userdata, mark, copy_dir, foreign)
    return mark, True


_SCRATCH = ('open scratch copies, never the files in the home: copy the database alone into one empty folder and the '
            'database with the -wal into another, open each with sqlite3 and compare the row counts (PRAGMA '
            'integrity_check too): the old home\'s WAL gives a whole database with what it holds alone plus newer '
            'rows; a WAL of the copy\'s database gives the copy\'s rows, or fewer than the database alone')


def _refuse_foreign(path, userdata, mark, copy_dir, foreign):
    """Raise the RuntimeError that names each -wal, -journal or -shm in the home the old home did not have, and
    whose it is: the copy's (its manifest SHA256), the copy's with more frames after them (a -wal that begins with
    the header of a copy's -wal: a T3 ran on the half-way home), a stray -shm, or one that cannot be told."""
    sums, unread, heads = _copy_sums(mark, copy_dir)
    wals = [c for c in foreign if not c.endswith('-shm')]
    theirs = [c for c in wals if sums.get(c, set()) & {_file_sum(os.path.join(userdata, c))} - {None}]
    mixed = [c for c in wals if c not in theirs and c.endswith('-wal')
             and _wal_head(os.path.join(userdata, c)) in heads.get(c, ())]
    groups = {'copy': theirs, 'mixed': mixed, 'unsure': [c for c in wals if c not in theirs + mixed], 'stray': []}
    for c in foreign:
        if c.endswith('-shm'):
            wal = c[:-len('-shm')] + '-wal'
            for key in ('unsure', 'mixed', 'copy'):
                if wal in groups[key]:
                    groups[key].append(c)
                    break
            else:
                groups['stray'].append(c)
    said = []
    if groups['copy']:
        g = groups['copy']
        said.append(f'{", ".join(g)} {_them(g, "is", "are")} the copy\'s (the same SHA256 as in its manifest; '
                    f'a -shm goes with its -wal), left from the restore stopped half-way: remove {_them(g)}, '
                    'and do NOT remove the record (beside the old database the copy\'s WAL would serve or '
                    'checkpoint another database\'s pages); then run the restore again (the record is then '
                    'stale)')
    if groups['mixed']:
        g = groups['mixed']
        said.append(f'{", ".join(g)} {_them(g, "is", "are")} the copy\'s WAL with more commits after its frames '
                    '(it begins with the header, salts included, of the copy\'s -wal; a -shm goes with its -wal): '
                    'a T3 ran on the half-way home, the copy\'s database, before the kept files were put back. '
                    'Beside the old database it would serve or checkpoint the copy\'s pages: move '
                    f'{_them(g)} out of userdata (keep {_them(g)} only if you want what that T3 wrote on the copy\'s '
                    'database; it is not the old home\'s), do NOT remove the record, then run the restore again '
                    '(the record is then stale)')
    if groups['unsure']:
        g = groups['unsure']
        why = (f'no readable manifest in {", ".join(unread)}, so the copy\'s files are unknown' if unread else
               'not its SHA256, not its WAL header')
        said.append(f'whose {", ".join(g)} {_them(g, "is", "are")} cannot be told ({why}). Either a T3 ran on the '
                    'put-back home with the record still there and wrote it: then it holds that T3\'s newest '
                    'commits: keep it and remove the record. Or a T3 ran on the half-way home (the copy\'s database) '
                    'before the put-back and its WAL started over: then it must not stay beside the old database: '
                    'move it out of userdata, keep the record and run the restore again. To tell, ' + _SCRATCH
                    + '. If you still cannot tell, or no copy is left to compare with: move it out of userdata (do '
                    'not delete it), keep the record and run the restore again: nothing is lost, the moved file '
                    'keeps what it holds')
    if groups['stray']:
        g = groups['stray']
        said.append(f'{", ".join(g)} {_them(g, "is a -shm", "are -shm files")} with no -wal of the half-way '
                    'home beside it, only SQLite\'s index of a WAL, rebuilt when the database opens: always '
                    'safe to remove' + ('' if len(said) else ' (then run the restore again)'))
    raise RuntimeError(f'{path} names the kept files of the old home and they are back, but the home also '
                       'holds files the old home did not have: ' + '; '.join(said) + '; nothing was changed')


def _record_names(record):
    """The kept files a record of a restore stopped half-way names (its old home, then what reruns kept)."""
    return (record or {}).get('oldHome', []) + (record or {}).get('alsoKept', [])


def _same_file(a, b):
    """True when a and b are both regular files with the same bytes, or both links to the same target."""
    try:
        sa, sb = os.lstat(a), os.lstat(b)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(sa.st_mode) and stat.S_ISLNK(sb.st_mode):
        return os.readlink(a) == os.readlink(b)
    return (stat.S_ISREG(sa.st_mode) and stat.S_ISREG(sb.st_mode) and sa.st_size == sb.st_size
            and _hash(a, drop_cache=True) == _hash(b, drop_cache=True))


def _free_name(folder, name):
    """name, or name-2, name-3 ... : the first that does not exist in folder (an aside never goes over another)."""
    candidate, n = name, 1
    while os.path.lexists(os.path.join(folder, candidate)):
        n += 1
        candidate = f'{name}-{n}'
    return candidate


def _family(name):
    """A database with its WAL and rollback journal (both copied when present) and -shm (never copied)."""
    return [name, f'{name}-wal', f'{name}-journal'] if name.endswith('.sqlite') else [name]


def hold_owner_lock(userdata, wait_seconds):
    """Take upstream's ownership lock of a T3 home (an exclusive SQLite transaction on server-owner.sqlite, which
    holds no data) and return the connection holding it: while it is held, no T3 server can start on this home.
    Raises TimeoutError when a server still holds it after wait_seconds."""
    path = os.path.join(userdata, OWNER_LOCK)
    if not os.path.exists(path):
        os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_CLOEXEC, 0o644))  # as upstream creates it
    end = time.monotonic() + wait_seconds
    while True:
        conn = sqlite3.connect(path, timeout=0, isolation_level=None)
        try:
            conn.execute('BEGIN EXCLUSIVE')
            return conn
        except sqlite3.OperationalError:
            conn.close()
            if time.monotonic() >= end:
                raise TimeoutError(f'a T3 server still holds {path} after {wait_seconds} s') from None
            time.sleep(POLL)


def plan_restore(copy_dir, home, *, from_home, version, names=DEFAULT_RESTORE, floor_bytes=0):
    """Read only: may this copy be restored into `home`? `from_home` is the home the copy must have been taken of
    (the same home for `back`, the live home for a rehearsal) and `version` the T3 build that will start on the
    restored data: it must be the build the copy was taken under (WrongCopy otherwise). Returns
    ({path: manifest entry}, [files to put back]); raises Incomplete (no finished manifest, no database, a file
    that differs from the manifest), ValueError (a name that is not a top-level userdata file), RuntimeError (the
    record of a restore stopped half-way cannot be read or was put back only in part, see stopped_restore) or
    OSError (not enough free disk: the copy's files plus a kept copy of each home file restore() will keep (see
    _to_keep) plus the floor; the temp files of a restore stopped half-way count as free, since restore() removes
    them first; the refusal names the files earlier restores kept that may be removed, and apart from them the
    kept files the record of a restore stopped half-way names, which must stay). A caller runs it as a
    pre-check before its restore step, so a refusal changes nothing and needs no settling; restore() runs it
    again under the ownership lock (so the disk check and the record check hold for the restore itself)."""
    m = read_manifest(copy_dir, from_home, version)
    files = {e.get('path'): e for e in m['files'] if isinstance(e, dict) and e.get('kind') == 'file'}
    userdata = os.path.join(home, 'userdata')
    prior = stopped_restore(home, copy_dir)
    want = []
    for name in names:
        if '/' in name or name in ('', '.', '..'):
            raise ValueError(f'restore takes top-level userdata files only, not {name!r}')
        if name == live.DATABASE and name not in files:
            raise Incomplete(f'{copy_dir} holds no {name}')
        want += [n for n in _family(name) if n in files]
    for n in want:
        got = _hash(os.path.join(copy_dir, 'userdata', n), drop_cache=True)
        if got != files[n].get('sha256'):
            raise Incomplete(f'{copy_dir}/userdata/{n} does not match its manifest: the copy is not used')
    keep = _to_keep(userdata, names, prior, files, want)[0]
    kept = sum(os.lstat(os.path.join(userdata, n)).st_size for n in keep)
    leftover = sum(os.lstat(os.path.join(userdata, n)).st_size for n in _leftover_temps(userdata))
    need = sum(files[n]['bytes'] for n in want) + kept + floor_bytes
    if _free_bytes(userdata) + leftover < need:
        named = [os.path.join(userdata, n) for n in _record_names(prior)]
        note = _room_note((), [(p, b) for p, b in kept_files(home) if p not in named])
        if named:
            note += ('; the record of the restore stopped half-way names these kept files (the old home, and what '
                     f'reruns kept): they must stay until it is done: {", ".join(named)}')
        raise OSError(f'not enough free disk in {userdata} for the restore ({need // MB} MB needed: the copy, a '
                      f'kept copy of each file it replaces that holds something not kept yet, the floor){note}')
    return files, want


def _companions(name):
    """A database's files that only mean something next to it: -shm, -journal, -wal (empty for other files)."""
    return [f'{name}-shm', f'{name}-journal', f'{name}-wal'] if name.endswith('.sqlite') else []


def _replaced(userdata, names):
    """The home files a restore of `names` replaces or removes, each database before its companions."""
    return [n for name in names for n in [name] + _companions(name) if os.path.lexists(os.path.join(userdata, n))]


def _leftover_temps(userdata):
    """Temp files of a restore stopped half-way (.<name>.golive-restore-<time>...): never in use (a temp is renamed
    to its real name only once it is whole) and never the only copy of anything (the copy folder holds it, or the
    home file it was being kept from is still there)."""
    return sorted(n for n in os.listdir(userdata) if n.startswith('.') and RESTORE_TEMP in n
                  and stat.S_ISREG(os.lstat(os.path.join(userdata, n)).st_mode))


def _to_keep(userdata, names, prior, files, want):
    """(home files a restore keeps in step 2, whether the home already holds exactly the copy's files).
    With no record (prior None): every file it replaces, unless the home is exactly the copy's files (`want`, same
    bytes): a restore that stopped after removing its record, before its step ended. Then nothing is kept, so the
    restored data is never named as the old home (the old home is the kept set that restore made; kept_files lists
    it). With a record: only files that hold something new (see _unchanged), against the kept files the record
    names and the copy's."""
    replaced = _replaced(userdata, names)
    if prior is None:
        if sorted(replaced) == sorted(want) and all(_unchanged(userdata, n, (), files[n]) for n in replaced):
            return [], True
        return replaced, False
    named = {}
    for k in _record_names(prior):
        named.setdefault(k.split(ASIDE)[0], []).append(k)
    return [n for n in replaced if not _unchanged(userdata, n, named.get(n, ()), files.get(n))], False


def _unchanged(userdata, n, kept_names, copied):
    """True when home file n holds exactly one of the kept files kept_names or the copy's file (manifest entry
    `copied`): a rerun has nothing new to keep from it."""
    path = os.path.join(userdata, n)
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode):
        return False
    digest = None
    if copied is not None and copied.get('bytes') == st.st_size:
        digest = _hash(path, drop_cache=True)
        if digest == copied.get('sha256'):
            return True
    for kept_name in kept_names:
        kept = os.path.join(userdata, kept_name)
        try:
            kst = os.lstat(kept)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(kst.st_mode) and kst.st_size == st.st_size:
            digest = digest or _hash(path, drop_cache=True)
            if digest == _hash(kept, drop_cache=True):
                return True
    return False


def _write_mark(userdata, record, stamp):
    """Write RESTORE_MARK through a temp file (a restore temp: a rerun removes it if this stops) and a rename."""
    tmp = os.path.join(userdata, _free_name(userdata, f'.{RESTORE_MARK}{RESTORE_TEMP}{stamp}'))
    with open(tmp, 'x', encoding='utf-8') as f:
        json.dump(record, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.rename(tmp, os.path.join(userdata, RESTORE_MARK))
    _fsync_dir(userdata)


def _keep(userdata, n, stamp):
    """Keep home file n as <n>.golive-aside-<time>: an independent copy, written under a temp name, fsynced and read
    back, then renamed (a link is kept as a link). Not a hard link: a T3 started on the home after a restore stopped
    half-way writes to the home's files, and must not change the kept ones. Returns (kept name, its SHA256 or None
    for a link)."""
    src = os.path.join(userdata, n)
    st = os.lstat(src)
    tmp = os.path.join(userdata, _free_name(userdata, f'.{n}{RESTORE_TEMP}{stamp}-aside'))
    digest = None
    if stat.S_ISLNK(st.st_mode):
        os.symlink(os.readlink(src), tmp)
    else:
        size, digest = _copy_file(src, tmp, None, stat.S_IMODE(st.st_mode))
        if size != st.st_size or _hash(tmp, drop_cache=True) != digest:
            raise RuntimeError(f'{tmp} does not read back as {src}')
    name = _free_name(userdata, f'{n}{ASIDE}{stamp}')
    os.rename(tmp, os.path.join(userdata, name))
    return name, digest


def restore(run, copy_dir, home, *, from_home, version, names=DEFAULT_RESTORE, floor_bytes=0):
    """The restore primitive (used by `rehearse` and `back`), in a step, with the home's server stopped: puts
    userdata/<name> from a finished copy back into `home` (see plan_restore for from_home and version). Refused
    (TimeoutError) while any server holds the home's ownership lock. Returns the step's facts.

    Order, so that a stop at any point loses nothing and never leaves a WAL without its database or a database
    with another database's WAL:
    0. temp files of an earlier restore stopped half-way are removed (see _leftover_temps);
    1. the copy's files are copied in under temp names (.<name>.golive-restore-<time>), then read back from disk;
    2. every home file this replaces is kept first: an fsynced copy <name>.golive-aside-<time> (see _keep). From
       here on the old files are kept whatever happens, even if T3 starts on the home before the restore ends;
    3. RESTORE_MARK is written: it names the kept files that are the old home;
    4. the database's -shm, -journal and -wal leave the home, then the files the copy does not hold. The home now
       has its own database file without its WAL: older, and not a database to serve (the WAL may hold commits,
       and after a crash in the middle of a checkpoint the file alone may be torn until the WAL is replayed);
    5. the copy's database renames over the home's (atomic), then its WAL and journal go in;
    6. RESTORE_MARK is removed: the restore is done.
    Running it again starts again from the untouched copy. A rerun after a stop in steps 4-6 finds RESTORE_MARK:
    the home is then half-way, not the old home, so its result names the old home from the record ('movedAside')
    and it keeps only home files that hold anything new ('alsoKept': what a T3 started after the stop wrote; the
    record is rewritten to name them, and a rerun's copy ('alsoCopies'), before anything leaves the home, so a
    later rerun names them too). The record holds each kept file's SHA256 ('sums'). A record put back only in part,
    or with a -wal, -journal or -shm the old home did not have, refuses before any data change (see _read_record);
    one moved back in full is stale: the home is the old home ('staleRecord', and 'staleKept' names the record's kept
    files still there). A rerun after a stop between step 6 and
    the end of the caller's step finds no record and the home exactly the copy's: it keeps nothing
    ('alreadyRestored'), so the restored data is never named as the old home.
    The CALLER keeps T3 from starting until the step is done (a T3 started after a stop in steps 4-6 serves that
    older database file or the copy's without its WAL; the kept files stay the old home either way): settling such
    a stop means running the restore again, or, before T3 starts, removing the home's -wal, -journal and -shm,
    then putting the kept files RESTORE_MARK names back and removing it (that drops the half-way home; if a T3 ran
    on it after the stop, run the restore again instead: it keeps what that T3 wrote)."""
    run.require_step('restore')
    userdata = os.path.join(home, 'userdata')
    mark_path = os.path.join(userdata, RESTORE_MARK)
    lock = hold_owner_lock(userdata, 0)
    try:  # the checks (disk included) under the lock, so nothing changes between them and the restore
        files, want = plan_restore(copy_dir, home, from_home=from_home, version=version, names=names,
                                   floor_bytes=floor_bytes)
        prior, stale = _read_record(home, copy_dir)
        stale_kept = [n for n in _record_names(prior) if os.path.lexists(os.path.join(userdata, n))] if stale else []
        prior = None if stale else prior
        stamp, temps, kept, sums = _stamp(), {}, [], {}
        cleaned = _leftover_temps(userdata)
        for n in cleaned:
            os.unlink(os.path.join(userdata, n))
        for n in want:
            tmp = os.path.join(userdata, _free_name(userdata, f'.{n}{RESTORE_TEMP}{stamp}'))
            _, digest = _copy_file(os.path.join(copy_dir, 'userdata', n), tmp, None, files[n]['mode'])
            if digest != files[n]['sha256'] or _hash(tmp, drop_cache=True) != digest:
                raise Incomplete(f'{tmp} does not read back as the manifest says')
            temps[n] = tmp
        _fsync_dir(userdata)
        keep, already = _to_keep(userdata, names, prior, files, want)  # nothing new in the others
        for n in keep:
            name, digest = _keep(userdata, n, stamp)
            kept.append(name)
            if digest:
                sums[name] = digest
            _fsync_dir(userdata)
        if prior is None:
            _write_mark(userdata, {'oldHome': kept, 'at': stamp, 'copy': copy_dir, 'names': list(names),
                                   'sums': sums}, stamp)
        else:  # a rerun: the record names what it kept and its copy too, before anything leaves the home
            copies = [prior.get('copy')] + prior.get('alsoCopies', [])
            new_copy = os.path.normpath(copy_dir) not in [os.path.normpath(c) for c in copies if isinstance(c, str)]
            if kept or new_copy:
                prior = {**prior, 'alsoKept': prior.get('alsoKept', []) + kept,
                         'sums': {**prior.get('sums', {}), **sums},
                         'alsoCopies': prior.get('alsoCopies', []) + ([copy_dir] if new_copy else [])}
                _write_mark(userdata, prior, stamp)
        for name in names:
            for n in _companions(name) + ([name] if name not in temps else []):
                if os.path.lexists(os.path.join(userdata, n)):
                    os.unlink(os.path.join(userdata, n))  # kept under its aside name
        _fsync_dir(userdata)
        for name in names:
            for n in _family(name):  # the database first, then its WAL and journal
                if n in temps:
                    os.rename(temps[n], os.path.join(userdata, n))
                    _fsync_dir(userdata)
        os.unlink(mark_path)
        _fsync_dir(userdata)
    finally:
        lock.close()
    result = {'copy': copy_dir, 'restored': want, 'movedAside': prior['oldHome'] if prior else kept,
              'alsoKept': prior.get('alsoKept', []) if prior else [], 'removedTemps': cleaned}
    if already:
        result['alreadyRestored'] = ('the home already held exactly the copy\'s files (a restore that stopped after '
                                     'it was done): nothing was kept')
    if stale:
        result['staleRecord'] = ('the kept files the record of a stopped restore named were put back by hand: the '
                                 'home was kept as the old home'
                                 + (f'; the kept files that record named and that are still there (copies of what '
                                    f'was put back, and what earlier reruns kept from the half-way home) stay '
                                    f'until the owner removes them: {", ".join(stale_kept)}' if stale_kept else ''))
        result['staleKept'] = stale_kept
    return result


# --- the practice stop ---

def _run_command(path, home, timeout, env_extra=None):
    """(ok, detail) of running an absolute command with no arguments in the T3 home."""
    try:
        p = subprocess.run([path], cwd=home, capture_output=True, text=True, timeout=timeout, errors='replace',
                           stdin=subprocess.DEVNULL, env={**os.environ, **(env_extra or {})})
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, f'{path}: {e}'
    detail = (p.stdout.strip() or p.stderr.strip())[-300:]
    return p.returncode == 0, f'{path} exited {p.returncode}' + (f': {detail}' if detail else '')


def quiet_now(cfg, live_version):
    """One sample of the machine's quiet command (fork setting automaticUpdatesQuietCommand's contract)."""
    return _run_command(cfg.quiet_command, cfg.home, QUIET_TIMEOUT, {
        'T3CODE_AUTO_UPDATE_CURRENT_VERSION': live_version, 'T3CODE_AUTO_UPDATE_TARGET_VERSION': cfg.target_version})


def wait_quiet(cfg, live_version, say):
    """Read only. Returns when the quiet command said quiet twice, QUIET_GAP seconds apart; journal.Refused when
    quietWaitSeconds pass first."""
    end, first, last = time.monotonic() + cfg.quiet_wait_seconds, None, ''
    while True:
        ok, last = quiet_now(cfg, live_version)
        now = time.monotonic()
        if ok and first is not None and now - first >= QUIET_GAP:
            return
        first = (first if first is not None else now) if ok else None
        if now >= end:
            raise journal.Refused(f'no quiet point within {cfg.quiet_wait_seconds} s (last: {last}); nothing '
                                  'was changed')
        say(f'waiting for a quiet point: {last}')
        time.sleep(max(0.0, min(QUIET_GAP if ok else POLL * 30, end - now)))


def _hooks(run, name, paths, home):
    with run.step(name) as facts:
        facts['ran'] = []
        for path in paths:
            ok, detail = _run_command(path, home, HOOK_TIMEOUT)
            facts['ran'].append(detail)
            if not ok:
                facts['failed'] = detail
                return detail
    return None


def _wait_started(cfg, adapter, version, old_pid):
    """T3 runs again: the service is active and its new server, of `version` (None: whatever build the service
    starts), wrote its runtime record."""
    end, why = time.monotonic() + START_WAIT, ''
    while time.monotonic() < end:
        svc = adapter.service()
        try:
            if svc['active']:
                server = live.live_server(cfg.home, svc['mainPid'])
                if server['pid'] != old_pid and version in (None, server['version']) and not live.markers(cfg.home):
                    return server
                why = f'server pid {server["pid"]}, version {server["version"]}'
            else:
                why = f'{osadapter.UNIT} is not active'
        except live.NotLive as e:
            why = str(e)
        time.sleep(POLL)
    raise RuntimeError(f'T3 did not come back within {START_WAIT} s: {why}')


def _wait_stopped(adapter, old_pid):
    end = time.monotonic() + STOP_WAIT
    while time.monotonic() < end:
        if not adapter.service()['active'] and live._proc(old_pid) is None:
            return
        time.sleep(POLL)
    raise RuntimeError(f'{osadapter.UNIT} did not stop within {STOP_WAIT} s')


def _existing(path):
    while not os.path.isdir(path):
        path = os.path.dirname(path)
    return path


def _device(path):
    return os.stat(path).st_dev


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _free_check(cfg, total, incomplete=(), kept=()):
    """Refused unless the backup disk holds the copy plus the floor and the stateDir disk (the journal, the guard
    log) keeps the floor free; a disk holding both needs the copy plus one floor. A refusal names the incomplete
    copy folders (never used) and the files restores kept that the owner may remove to make room."""
    need = {}
    for folder, size in ((cfg.backup_dir, total), (cfg.state_dir, 0)):
        path = _existing(folder)
        need.setdefault(_device(path), [path, cfg.free_floor_mb * MB])[1] += size
    for path, want in need.values():
        free = _free_bytes(path)
        if free < want:
            raise journal.Refused(f'{path}: {free // MB} MB free, needs {want // MB} MB (the copy {total // MB} MB '
                                  f'on the backup disk, floor {cfg.free_floor_mb} MB); nothing was changed'
                                  f'{_room_note(incomplete, kept)}')


def _room_note(incomplete, kept):
    """'; <what the owner may remove by hand>' naming incomplete copy folders and kept files, or ''."""
    note = ''
    if incomplete:
        note += (f'; {len(incomplete)} incomplete copy folder(s), never used, hold '
                 f'{sum(b for _, b in incomplete) // MB} MB and may be removed by hand: '
                 f'{", ".join(f for f, _ in incomplete)}')
    if kept:
        note += (f'; {len(kept)} file(s) earlier restores kept (old data) hold {sum(b for _, b in kept) // MB} MB '
                 f'and may be removed by hand once no longer needed: {", ".join(f for f, _ in kept)}')
    return note


def _home_problem(cfg):
    """Why this home cannot be copied as it is (None when it can): a database that is a link (a copy takes the
    files of userdata/, so it would hold the link, not the database), or a restore stopped half-way (the home is
    neither the old one nor the copy's)."""
    userdata = os.path.join(cfg.home, 'userdata')
    links = [n for n in _family(live.DATABASE) if os.path.islink(os.path.join(userdata, n))]
    if links:
        return (f'{", ".join(links)} in {userdata} is a link: the copy takes files only and would hold the link, '
                'not the database')
    if os.path.lexists(os.path.join(userdata, RESTORE_MARK)):
        try:  # fork #244: name what is left of the half-way home here, not only in a restore's refusal
            stopped_restore(cfg.home)
            looked = ''
        except (RuntimeError, OSError) as e:
            looked = f'. What the home holds now: {e}'
        return (f'a restore stopped half-way ({os.path.join(userdata, RESTORE_MARK)} is there): if the kept files '
                'it names are back in the home, remove it (first remove any -wal or -journal of the copy left beside '
                f'the old database: a restore run names them); otherwise run the restore again; then copy{looked}')
    return None


def _plan_total(cfg):
    try:
        return plan(os.path.join(cfg.home, 'userdata'))[1]
    except OSError as e:
        raise journal.Refused(f'the T3 home cannot be read in full ({e}); nothing was changed') from None


_server_changed = check_mod.server_changed  # the live server re-read; check runs it after its report


class UpdateAtStop(RuntimeError):
    """The service state changed between the last look and the stop: no copy; T3 starts on the build it names."""


def _same_after_stop(cfg, server):
    """In the stop step, once no server can start: the build the service state names, with no update pending, is
    still the one the checks passed. This is the guard against a wrong label: a server that ran in between of
    another build means the auto-updater moved T3, and it names that build here (or marks the update pending)
    before it starts it. UpdateAtStop otherwise. (A server of the same build that ran in between, a crash
    restarted by the service, changes nothing the copy says, so its pid is not compared: upstream removes the
    runtime record on a clean stop, so such a check could only ever run after a crash.)"""
    active, pending = live.service_state(cfg.home)
    if active != server['version'] or pending:
        raise UpdateAtStop(f'the service state changed at the stop (now t3 {active}'
                           f'{", an update to " + str(pending.get("targetVersion")) + " pending" if pending else ""});'
                           ' the copy is not taken')


def _guard_log(state_dir, record):
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    with open(os.path.join(state_dir, GUARD_LOG), 'a', encoding='utf-8') as f:
        f.write(json.dumps({'at': _stamp(), **record}) + '\n')
        f.flush()
        os.fsync(f.fileno())


def _guard_waiting(state_dir, run_id):
    """True once the guard of this run logged that it waits (within GUARD_WAIT seconds)."""
    end = time.monotonic() + GUARD_WAIT
    while True:
        try:
            with open(os.path.join(state_dir, GUARD_LOG), encoding='utf-8') as f:
                for line in f:
                    try:
                        rec = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(rec, dict) and rec.get('run') == run_id and rec.get('waiting') is True:
                        return True
        except FileNotFoundError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(POLL)


def _guard_alive(adapter, unit):
    try:
        return adapter.unit_active(unit)
    except osadapter.AdapterError:
        return False


def _restart(run, cfg, adapter, server, say, version):
    """Start T3 again FIRST and journal it after (a full disk must not keep T3 down), then verify that a new server
    of `version` runs (None: of whatever build the service starts). Returns (error or None, the new server)."""
    try:
        adapter.start()
        start_error = None
    except BaseException as e:
        start_error = e
    try:
        with run.step('start-service', startedBeforeThisLine=True) as facts:
            if start_error is not None:
                facts['error'] = str(start_error)
                raise start_error
        with run.step('verify') as facts:
            again = _wait_started(cfg, adapter, version, server['pid'])
            facts.update(pid=again['pid'], version=again['version'])
        say(f't3 {again["version"]} runs again (pid {again["pid"]})')
    except BaseException as e:
        if start_error is not None and e is not start_error:  # the journal failed too: keep why the start failed
            e.add_note(f'and the start had failed: {type(start_error).__name__}: {start_error}')
        return e, None
    return None, again


def run_copy(cfg, adapter, owner_says_go=False, say=print):
    """The practice stop, in this process. Returns the result dict; raises journal.Refused (nothing changed or
    all undone), journal.Busy / Unfinished, or the error that ended a run (it then needs settling). When more
    than one thing failed, the first error is raised with the others as notes."""
    with journal.run(cfg.state_dir, 'copy') as run:
        code, result = check_mod.run_check(cfg, adapter, own_run=run)
        failed = [f'{c["name"]}: {c["detail"]}' for c in result['checks'] if not c['ok']]
        if code != 0 or result.get('nothing'):
            raise journal.Refused('; '.join(failed + result['info']) or 'check refused')
        server = result['live']
        why = _home_problem(cfg)
        if why is not None:
            raise journal.Refused(f'{why}; nothing was changed')
        incomplete, kept = incomplete_copies(cfg.backup_dir), kept_files(cfg.home)
        if incomplete or kept:
            say(_room_note(incomplete, kept)[2:])
        _free_check(cfg, _plan_total(cfg), incomplete, kept)  # before the wait: no use waiting hours for no room
        if cfg.quiet_command is None and not owner_says_go:
            raise journal.Refused('the machine file has no golive.quietCommand: set one, or pass --owner-says-go')
        if not owner_says_go:
            wait_quiet(cfg, server['version'], say)
            _free_check(cfg, _plan_total(cfg), incomplete, kept)  # and again after it: the wait may have taken hours
        why = _server_changed(cfg, adapter, server) or _home_problem(cfg)
        if why is not None:
            raise journal.Refused(f'{why}; nothing was changed')
        dest = os.path.join(cfg.backup_dir, f't3-copy-{_stamp()}-{server["version"]}')
        if os.path.lexists(dest):
            raise journal.Refused(f'{dest} already exists; a copy never goes over another: try again in a second')
        with run.step('guard') as facts:
            unit = facts['unit'] = f't3-golive-guard-{os.getpid()}-{os.urandom(3).hex()}'
            try:
                adapter.run_detached(unit, [KIT_BIN, 'guard', '--run', run.id])
                facts['waiting'] = _guard_waiting(cfg.state_dir, run.id)
            except osadapter.AdapterError as e:
                facts.update(waiting=False, error=str(e))
        look = f'look at: journalctl --user -u {unit}, and {os.path.join(cfg.state_dir, GUARD_LOG)}'
        if not facts['waiting']:
            raise journal.Refused(f'the guard {unit} did not come up within {GUARD_WAIT} s'
                                  f'{": " + facts["error"] if "error" in facts else ""}; T3 was not stopped; {look}')
        failed_hook = _hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed_hook is None and not owner_says_go:
            ok, detail = quiet_now(cfg, server['version'])
            failed_hook = None if ok else f'not quiet any more: {detail}'
        if failed_hook is None:  # the last look before the stop
            failed_hook = _server_changed(cfg, adapter, server) or _home_problem(cfg)
        if failed_hook is None and not _guard_alive(adapter, unit):
            failed_hook = f'the guard {unit} is not running any more ({look})'
        if failed_hook is not None:
            again = _hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if again:  # jobs may stay paused: not a clean refusal, the owner must look (settle)
                raise RuntimeError(f'{failed_hook}; T3 was not stopped; then a post hook failed: {again}')
            raise journal.Refused(f'{failed_hook}; T3 was not stopped')
        say(f'stopping {osadapter.UNIT} (t3 {server["version"]}, pid {server["pid"]}) for the copy')
        error, lock, stopping = None, None, False
        try:
            with run.step('stop-service', version=server['version'], pid=server['pid'], ownerSaysGo=owner_says_go):
                stopping = True
                adapter.stop()
                _wait_stopped(adapter, server['pid'])
                lock = hold_owner_lock(os.path.join(cfg.home, 'userdata'), STOP_WAIT)
                _same_after_stop(cfg, server)
            deadline = _clock() + cfg.stop_limit_seconds
            with run.step('cold-copy', dest=dest) as facts:
                entries, total, left_out = copy_userdata(run, cfg.home, dest, deadline)
                facts.update(files=sum(e['kind'] == 'file' for e in entries), bytes=total, leftOut=left_out)
            with run.step('read-back') as facts:
                read_back(cfg.home, dest, entries, deadline)
                facts['sha256'] = {e['path']: e['sha256'] for e in entries if e['path'] in _family(live.DATABASE)}
            with run.step('manifest') as facts:
                write_manifest(run, dest, {
                    'manifestVersion': MANIFEST_VERSION, 'complete': True, 'createdAt': _stamp(), 'runId': run.id,
                    'home': cfg.home, 'liveVersion': server['version'], 'bytes': total, 'files': entries,
                    'leftOut': left_out})
                facts['manifest'] = os.path.join(dest, MANIFEST)
                facts['reportCounts'] = result.get('counts')  # read by check before the wait; `back` compares
        except BaseException as e:
            error = e
        finally:
            if lock is not None:
                lock.close()
        errors = [error] if error is not None else []
        if stopping:
            # After an update caught at the stop the service starts the build its state names (or upstream's
            # launcher finishes or rolls back the pending update): wait for whichever build, and say which runs.
            updated = isinstance(error, UpdateAtStop)
            restart_error, again = _restart(run, cfg, adapter, server, say, None if updated else server['version'])
            if restart_error is not None:
                errors.append(restart_error)
            elif updated:
                error.add_note(f'T3 runs again, on t3 {again["version"]} (pid {again["pid"]})')
        try:  # a journal that cannot be written stops this too: the guard then runs the post hooks
            failed_hook = _hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if failed_hook:
                errors.append(RuntimeError(f'post hook failed: {failed_hook}'))
        except BaseException as e:
            errors.append(e)
        if errors:
            first = errors[0]
            for later in errors[1:]:
                first.add_note(f'then: {type(later).__name__}: {later}')
            raise first
        return {'copy': dest, 'bytes': total, 'liveVersion': server['version'], 'leftOut': left_out,
                'incompleteCopies': [f for f, _ in incomplete], 'keptFiles': [f for f, _ in kept]}


def guard(cfg, adapter, run_id):
    """Started detached by a copy run before T3 stops. Logs that it waits (the run stops T3 only after this line),
    then waits for that run to end however it ends (the lock frees when its holder dies), then starts T3 if the
    run had stopped it and left it stopped and the service is enabled (a copy never disables it; a back W1 that
    stopped disables it on purpose), and runs the post hooks if the run ran the pre hooks but not the post hooks. A
    journal it cannot read fails open: T3 is started if it is not active and enabled, and the post hooks run (each
    is safe twice). It changes nothing else and writes no journal line (the run, ended or killed, stays as it is for
    the owner). Logs to <stateDir>/guard.log. Returns what it did."""
    try:
        _guard_log(cfg.state_dir, {'run': run_id, 'waiting': True})
    except OSError:
        pass  # the run then does not see this guard and refuses before T3 stops
    with journal.lock(cfg.state_dir, 'guard', exclusive=True, wait=True):
        did = []
        try:
            runs, _ = journal.read(cfg.state_dir)
            mine = [r for r in runs if r['id'] == run_id]
            steps = {(s['step'], s['phase']) for s in mine[0]['steps']} if mine else set()
            stopped = ('stop-service', 'started') in steps
            paused = ('pre-hooks', 'started') in steps and ('post-hooks', 'done') not in steps
        except (journal.Damaged, OSError) as e:  # Newer included
            mine, stopped, paused = [], True, True
            did.append(f'journal unreadable, so T3 is put back as if the run had stopped it: {e}')
        if stopped:
            try:
                svc = adapter.service()
                if not svc['active'] and not svc['enabled']:  # a copy never disables it; a back W1 does, on purpose
                    did.append(f'{osadapter.UNIT} is not enabled: left stopped (a back W1 that stopped may hold it)')
                elif not svc['active']:
                    adapter.start()
                    did.append(f'started {osadapter.UNIT}')
            except osadapter.AdapterError as e:
                did.append(f'could not start {osadapter.UNIT}: {e}')
        if paused:
            did += [_run_command(p, cfg.home, HOOK_TIMEOUT)[1] for p in cfg.post_hooks]
        try:
            _guard_log(cfg.state_dir, {'run': run_id, 'found': bool(mine), 'did': did})
        except OSError:
            pass
        return did


def main_copy(cfg, adapter, here, owner_says_go, out=sys.stdout, err=sys.stderr):
    if not here:
        unit = f't3-golive-copy-{_stamp().lower()}'
        argv = [KIT_BIN, 'copy', '--here'] + (['--owner-says-go'] if owner_says_go else [])
        try:
            adapter.run_detached(unit, argv)
        except osadapter.AdapterError as e:
            print(f't3-golive: {e}', file=err)
            return 1
        print(f'started detached as {unit}: it waits for a quiet point (up to {cfg.quiet_wait_seconds} s), then '
              f'stops T3 for the copy and starts it again. Follow it with: journalctl --user -u {unit} -f',
              file=out)
        return 0
    if adapter.inside_service():
        print(f't3-golive: this runs inside {osadapter.UNIT}; stopping it would kill this copy half-way. Run '
              '`t3-golive copy` without --here (it runs detached).', file=err)
        return 1
    try:
        result = run_copy(cfg, adapter, owner_says_go, say=lambda line: print(line, file=out, flush=True))
    except journal.Refused as e:
        print(f'Refused: {e}', file=err)
        return 1
    except (journal.Busy, journal.Unfinished, journal.Damaged) as e:
        print(f't3-golive: {e}', file=err)
        return 1
    except Exception as e:
        notes = ''.join(f'\n{n}' for n in getattr(e, '__notes__', ()))
        print(f't3-golive: the copy failed: {type(e).__name__}: {e}{notes}\nT3 was started again if the run got that '
              f'far; the copy folder (if any) has no manifest and is never used. {journal.SETTLE_HINT}', file=err)
        return 1
    print(f'copy done: {result["copy"]} ({result["bytes"] // MB} MB, t3 {result["liveVersion"]}); read back, '
          'manifest written, T3 runs again', file=out)
    return 0
