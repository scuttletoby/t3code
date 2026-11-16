"""`t3-golive copy`: the practice stop. At a quiet point it stops the boot service, copies the T3 home's data
(userdata/: the database with its WAL, settings and the other small files) into a new folder in backupDir, reads
both sides back (SHA256 must match), writes the manifest LAST, starts the same build again and checks it runs.

A copy folder without a finished manifest is incomplete and is never used. Nothing is deleted: a copy never goes
over an old one, and restore() moves the files it replaces aside with a time in their names.

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
# Also never copied: a restore's leftover temp files (.<name>.golive-restore-<time>, from a restore stopped half-way).
SKIP = frozenset({'logs', 'server-runtime.json', OWNER_LOCK, OWNER_LOCK + '-journal'})
RESTORE_TEMP, ASIDE = '.golive-restore-', '.golive-aside-'
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


def plan(userdata):
    """What a copy takes from userdata/: [{'path', 'kind', 'bytes', 'mode'[, 'target']}] top-down, and the total
    bytes. Plain files, folders and links; anything else (a socket, a fifo) is left out and named. A folder that
    cannot be read raises OSError (a copy that says complete must hold everything)."""
    entries, total, left_out = [], 0, []
    for d, dirs, names in os.walk(userdata, onerror=_raise):
        rel_d = os.path.relpath(d, userdata)
        for n in sorted(dirs + names):
            rel = n if rel_d == '.' else f'{rel_d}/{n}'
            if rel_d == '.' and (n in SKIP or n.endswith('-shm') or (n.startswith('.') and RESTORE_TEMP in n)):
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


def copy_userdata(run, home, dest, deadline):
    """The cold copy (in a step): every planned entry into dest/userdata, hashing what is read. dest is new."""
    run.require_step('cold copy')
    userdata = os.path.join(home, 'userdata')
    entries, total, left_out = plan(userdata)
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
    that differs from the manifest), ValueError (a name that is not a top-level userdata file) or OSError (not
    enough free disk). A caller runs it as a pre-check before its restore step, so a refusal changes nothing and
    needs no settling; restore() runs it again."""
    m = read_manifest(copy_dir, from_home, version)
    files = {e.get('path'): e for e in m['files'] if isinstance(e, dict) and e.get('kind') == 'file'}
    userdata = os.path.join(home, 'userdata')
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
    st = os.statvfs(userdata)
    need = sum(files[n]['bytes'] for n in want) + floor_bytes
    if st.f_bavail * st.f_frsize < need:
        raise OSError(f'not enough free disk in {userdata} for the restore ({need // MB} MB needed)')
    return files, want


def _companions(name):
    """A database's files that only mean something next to it: -shm, -journal, -wal (empty for other files)."""
    return [f'{name}-shm', f'{name}-journal', f'{name}-wal'] if name.endswith('.sqlite') else []


def restore(run, copy_dir, home, *, from_home, version, names=DEFAULT_RESTORE, floor_bytes=0):
    """The restore primitive (used by `rehearse` and `back`), in a step, with the home's server stopped: puts
    userdata/<name> from a finished copy back into `home` (see plan_restore for from_home and version). Refused
    (TimeoutError) while any server holds the home's ownership lock. Returns the step's facts.

    Order, so that a stop at any point loses nothing and never leaves a WAL without its database or a database
    with another database's WAL:
    1. the copy's files are copied in under temp names (.<name>.golive-restore-<time>) and checked;
    2. every home file this replaces gets a second name first, a hard link <name>.golive-aside-<time> (fsynced):
       from here on each one is kept whatever happens;
    3. the database's -shm, -journal and -wal leave the home (their aside names keep them), then the files the
       copy does not hold. The home now has its own database without its WAL: an older but whole database;
    4. the copy's database renames over the home's (atomic), then its WAL and journal go in.
    Running it again starts again from the untouched copy; a stop half-way leaves temp files, which a copy skips."""
    run.require_step('restore')
    files, want = plan_restore(copy_dir, home, from_home=from_home, version=version, names=names,
                               floor_bytes=floor_bytes)
    userdata = os.path.join(home, 'userdata')
    lock = hold_owner_lock(userdata, 0)
    try:
        stamp, temps, aside = _stamp(), {}, []
        for n in want:
            tmp = os.path.join(userdata, _free_name(userdata, f'.{n}{RESTORE_TEMP}{stamp}'))
            _, digest = _copy_file(os.path.join(copy_dir, 'userdata', n), tmp, None, files[n]['mode'])
            if digest != files[n]['sha256']:
                raise Incomplete(f'{tmp} does not match the manifest after copying')
            temps[n] = tmp
        _fsync_dir(userdata)
        replaced = [n for name in names for n in [name] + _companions(name)
                    if os.path.lexists(os.path.join(userdata, n))]
        for n in replaced:
            a = _free_name(userdata, f'{n}{ASIDE}{stamp}')
            os.link(os.path.join(userdata, n), os.path.join(userdata, a), follow_symlinks=False)
            aside.append(a)
        _fsync_dir(userdata)
        for name in names:
            for n in _companions(name) + ([name] if name not in temps else []):
                if os.path.lexists(os.path.join(userdata, n)):
                    os.unlink(os.path.join(userdata, n))  # its aside name keeps it
        _fsync_dir(userdata)
        for name in names:
            for n in _family(name):  # the database first, then its WAL and journal
                if n in temps:
                    os.rename(temps[n], os.path.join(userdata, n))
                    _fsync_dir(userdata)
    finally:
        lock.close()
    return {'copy': copy_dir, 'restored': want, 'movedAside': aside}


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
    """T3 runs again: the service is active and its new server, of the same build, wrote its runtime record."""
    end, why = time.monotonic() + START_WAIT, ''
    while time.monotonic() < end:
        svc = adapter.service()
        try:
            if svc['active']:
                server = live.live_server(cfg.home, svc['mainPid'])
                if server['pid'] != old_pid and server['version'] == version and not live.markers(cfg.home):
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


def _free_check(cfg, total):
    """Refused unless the backup disk holds the copy plus the floor and the stateDir disk (the journal, the guard
    log) keeps the floor free; a disk holding both needs the copy plus one floor."""
    need = {}
    for folder, size in ((cfg.backup_dir, total), (cfg.state_dir, 0)):
        path = _existing(folder)
        need.setdefault(_device(path), [path, cfg.free_floor_mb * MB])[1] += size
    for path, want in need.values():
        free = _free_bytes(path)
        if free < want:
            raise journal.Refused(f'{path}: {free // MB} MB free, needs {want // MB} MB (the copy {total // MB} MB '
                                  f'on the backup disk, floor {cfg.free_floor_mb} MB); nothing was changed')


def _plan_total(cfg):
    try:
        return plan(os.path.join(cfg.home, 'userdata'))[1]
    except OSError as e:
        raise journal.Refused(f'the T3 home cannot be read in full ({e}); nothing was changed') from None


def _server_changed(cfg, adapter, server):
    """Why the live server is not the one the checks passed (None when it still is). The auto-updater keeps the
    same quiet contract, so it may restart T3 on a newer build (and migrate its database) while a copy waits."""
    try:
        svc = adapter.service()
        now = live.live_server(cfg.home, svc['mainPid']) if svc['active'] else None
        active, pending = live.service_state(cfg.home)
    except (live.NotLive, osadapter.AdapterError) as e:
        return f'the live server cannot be read again: {e}'
    if now is None:
        return f'{osadapter.UNIT} is not active any more'
    if (now['pid'], now['version']) != (server['pid'], server['version']):
        return (f'the live server changed (t3 {server["version"]} pid {server["pid"]} -> t3 {now["version"]} pid '
                f'{now["pid"]})')
    if active != server['version']:
        return f'the service state now names t3 {active}, not the live {server["version"]}'
    if pending:
        return f'an update to {pending.get("targetVersion")} is pending'
    found = live.markers(cfg.home)
    return f'markers appeared: {", ".join(found)}' if found else None


def _same_after_stop(cfg, server):
    """In the stop step, once no server can start: the T3 that stopped was the one the run checked, and the one
    that will start again is too. RuntimeError otherwise (an update slipped in between the last look and the stop)."""
    active, pending = live.service_state(cfg.home)
    if active != server['version'] or pending:
        raise RuntimeError(f'the service state changed at the stop (now t3 {active}'
                           f'{", an update pending" if pending else ""}); the copy is not taken')
    try:
        with open(os.path.join(cfg.home, 'userdata', 'server-runtime.json'), encoding='utf-8') as f:
            pid = json.load(f).get('pid')
    except (OSError, ValueError, AttributeError):
        return
    if pid != server['pid']:
        raise RuntimeError(f'another server (pid {pid}) ran on the home before the stop; the copy is not taken')


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


def _restart(run, cfg, adapter, server, say):
    """Start T3 again FIRST and journal it after (a full disk must not keep T3 down), then verify it. Returns the
    error, or None."""
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
            again = _wait_started(cfg, adapter, server['version'], server['pid'])
            facts.update(pid=again['pid'], version=again['version'])
        say(f't3 {again["version"]} runs again (pid {again["pid"]})')
    except BaseException as e:
        if start_error is not None and e is not start_error:  # the journal failed too: keep why the start failed
            e.add_note(f'and the start had failed: {type(start_error).__name__}: {start_error}')
        return e
    return None


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
        _free_check(cfg, _plan_total(cfg))  # before the wait: no use waiting hours for a copy that cannot fit
        if cfg.quiet_command is None and not owner_says_go:
            raise journal.Refused('the machine file has no golive.quietCommand: set one, or pass --owner-says-go')
        if not owner_says_go:
            wait_quiet(cfg, server['version'], say)
            _free_check(cfg, _plan_total(cfg))  # and again after it: the wait may have taken hours
        why = _server_changed(cfg, adapter, server)
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
        if not facts['waiting']:
            raise journal.Refused(f'the guard {unit} did not come up within {GUARD_WAIT} s'
                                  f'{": " + facts["error"] if "error" in facts else ""}; T3 was not stopped')
        failed_hook = _hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed_hook is None and not owner_says_go:
            ok, detail = quiet_now(cfg, server['version'])
            failed_hook = None if ok else f'not quiet any more: {detail}'
        if failed_hook is None:  # the last look before the stop
            failed_hook = _server_changed(cfg, adapter, server)
        if failed_hook is None and not _guard_alive(adapter, unit):
            failed_hook = f'the guard {unit} is not running any more'
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
        except BaseException as e:
            error = e
        finally:
            if lock is not None:
                lock.close()
        errors = [error] if error is not None else []
        if stopping:
            restart_error = _restart(run, cfg, adapter, server, say)
            if restart_error is not None:
                errors.append(restart_error)
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
        return {'copy': dest, 'bytes': total, 'liveVersion': server['version'], 'leftOut': left_out}


def guard(cfg, adapter, run_id):
    """Started detached by a copy run before T3 stops. Logs that it waits (the run stops T3 only after this line),
    then waits for that run to end however it ends (the lock frees when its holder dies), then starts T3 if the
    run had stopped it and left it stopped, and runs the post hooks if the run ran the pre hooks but not the post
    hooks. A journal it cannot read fails open: T3 is started if it is not active and the post hooks run (each is
    safe twice). It changes nothing else and writes no journal line (the run, ended or killed, stays as it is for
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
                if not adapter.service()['active']:
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
