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
SKIP = frozenset({'logs', 'server-runtime.json', OWNER_LOCK, OWNER_LOCK + '-journal'})
QUIET_GAP, STOP_WAIT, START_WAIT, POLL = 60, 120, 180, 1.0  # seconds; tests make them small
HOOK_TIMEOUT, QUIET_TIMEOUT, CHUNK = 120, 30, 8 << 20
DEFAULT_RESTORE = (live.DATABASE, 'settings.json')
MB = 1024 * 1024
_clock = time.monotonic  # the stop limit's clock; tests change it


class Incomplete(Exception):
    """A copy that must not be used: no finished manifest, or files that do not match it."""


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


def plan(userdata):
    """What a copy takes from userdata/: [{'path', 'kind', 'bytes', 'mode'[, 'target']}] top-down, and the total
    bytes. Plain files, folders and links; anything else (a socket, a fifo) is left out and named."""
    entries, total, left_out = [], 0, []
    for d, dirs, names in os.walk(userdata):
        rel_d = os.path.relpath(d, userdata)
        for n in sorted(dirs + names):
            rel = n if rel_d == '.' else f'{rel_d}/{n}'
            if rel_d == '.' and (n in SKIP or n.endswith('-shm')):
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
    for d, dirs, _ in os.walk(os.path.join(dest, 'userdata')):
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


def read_manifest(copy_dir):
    """The manifest of a finished copy; Incomplete when it has none, or one this kit cannot read."""
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


def plan_restore(copy_dir, home, names=DEFAULT_RESTORE, floor_bytes=0):
    """Read only: may this copy be restored into `home`? Returns ({path: manifest entry}, [files to put back]);
    raises Incomplete (no finished manifest, no database, a file that differs from the manifest), ValueError (a
    name that is not a top-level userdata file) or OSError (not enough free disk). A caller runs it as a pre-check
    before its restore step, so a refusal changes nothing and needs no settling; restore() runs it again."""
    m = read_manifest(copy_dir)
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


def restore(run, copy_dir, home, names=DEFAULT_RESTORE, floor_bytes=0):
    """The restore primitive (used by `rehearse` and `back`), in a step, with the home's server stopped: puts
    userdata/<name> from a finished copy back into `home`. The copy is read back against its manifest first
    (plan_restore); the home's files are moved aside with a time in their name (never deleted), and the copy's
    files go in through temp names and a rename. Running it again starts again from the untouched copy.
    Refused (TimeoutError) while any server holds the home's ownership lock. Returns the step's facts."""
    run.require_step('restore')
    files, want = plan_restore(copy_dir, home, names, floor_bytes)
    userdata = os.path.join(home, 'userdata')
    lock = hold_owner_lock(userdata, 0)
    try:
        stamp, moved = _stamp(), []
        temps = {}
        for n in want:
            tmp = os.path.join(userdata, f'.{n}.golive-restore-{stamp}')
            _, digest = _copy_file(os.path.join(copy_dir, 'userdata', n), tmp, None, files[n]['mode'])
            if digest != files[n]['sha256']:
                raise Incomplete(f'{tmp} does not match the manifest after copying')
            temps[n] = tmp
        for name in names:
            for n in _family(name) + ([f'{name}-shm'] if name.endswith('.sqlite') else []):
                if os.path.lexists(os.path.join(userdata, n)):
                    aside = _free_name(userdata, f'{n}.golive-aside-{stamp}')
                    os.rename(os.path.join(userdata, n), os.path.join(userdata, aside))
                    moved.append(aside)
        for n, tmp in temps.items():
            os.rename(tmp, os.path.join(userdata, n))
        _fsync_dir(userdata)
    finally:
        lock.close()
    return {'copy': copy_dir, 'restored': want, 'movedAside': moved}


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


def run_copy(cfg, adapter, owner_says_go=False, say=print):
    """The practice stop, in this process. Returns the result dict; raises journal.Refused (nothing changed or
    all undone), journal.Busy / Unfinished, or the error that ended a run (it then needs settling)."""
    with journal.run(cfg.state_dir, 'copy') as run:
        code, result = check_mod.run_check(cfg, adapter, own_run=run)
        failed = [f'{c["name"]}: {c["detail"]}' for c in result['checks'] if not c['ok']]
        if code != 0 or result.get('nothing'):
            raise journal.Refused('; '.join(failed + result['info']) or 'check refused')
        server = result['live']
        _, total, left_out = plan(os.path.join(cfg.home, 'userdata'))
        parent = cfg.backup_dir
        while not os.path.isdir(parent):
            parent = os.path.dirname(parent)
        st = os.statvfs(parent)
        if st.f_bavail * st.f_frsize < total + cfg.free_floor_mb * MB:
            raise journal.Refused(f'{parent}: {st.f_bavail * st.f_frsize // MB} MB free, the copy needs '
                                  f'{total // MB} MB + floor {cfg.free_floor_mb} MB')
        if cfg.quiet_command is None and not owner_says_go:
            raise journal.Refused('the machine file has no golive.quietCommand: set one, or pass --owner-says-go')
        if not owner_says_go:
            wait_quiet(cfg, server['version'], say)
        dest = os.path.join(cfg.backup_dir, f't3-copy-{_stamp()}-{server["version"]}')
        if os.path.lexists(dest):
            raise journal.Refused(f'{dest} already exists; a copy never goes over another: try again in a second')
        with run.step('guard') as facts:
            facts['unit'] = f't3-golive-guard-{os.getpid()}-{os.urandom(3).hex()}'
            adapter.run_detached(facts['unit'], [KIT_BIN, 'guard', '--run', run.id])
        failed_hook = _hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed_hook is None and not owner_says_go:
            ok, detail = quiet_now(cfg, server['version'])
            failed_hook = None if ok else f'not quiet any more: {detail}'
        if failed_hook is not None:
            again = _hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            raise journal.Refused(f'{failed_hook}; T3 was not stopped' + (f'; post hook: {again}' if again else ''))
        say(f'stopping {osadapter.UNIT} (t3 {server["version"]}, pid {server["pid"]}) for the copy')
        error, lock, stopping = None, None, False
        try:
            with run.step('stop-service', version=server['version'], pid=server['pid'], ownerSaysGo=owner_says_go):
                stopping = True
                adapter.stop()
                _wait_stopped(adapter, server['pid'])
                lock = hold_owner_lock(os.path.join(cfg.home, 'userdata'), STOP_WAIT)
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
        try:
            if stopping:
                with run.step('start-service'):
                    adapter.start()
                with run.step('verify') as facts:
                    again = _wait_started(cfg, adapter, server['version'], server['pid'])
                    facts.update(pid=again['pid'], version=again['version'])
                say(f't3 {again["version"]} runs again (pid {again["pid"]})')
        finally:
            failed_hook = _hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
        if error is not None:
            raise error
        if failed_hook:
            raise RuntimeError(f'post hook failed: {failed_hook}')
        return {'copy': dest, 'bytes': total, 'liveVersion': server['version'], 'leftOut': left_out}


def guard(cfg, adapter, run_id):
    """Started detached by a copy run before T3 stops. Waits for that run to end however it ends (the lock frees
    when its holder dies), then starts T3 if the run had stopped it and left it stopped, and runs the post hooks
    if the run ran the pre hooks but not the post hooks. It changes nothing else and writes no journal line (the
    run, ended or killed, stays as it is for the owner). Logs to <stateDir>/guard.log. Returns what it did."""
    with journal.lock(cfg.state_dir, 'guard', exclusive=True, wait=True):
        did = []
        try:
            runs, _ = journal.read(cfg.state_dir)
        except journal.Damaged as e:
            runs = []
            did.append(f'journal unreadable, did nothing: {e}')
        mine = [r for r in runs if r['id'] == run_id]
        steps = {(s['step'], s['phase']) for s in mine[0]['steps']} if mine else set()
        if ('stop-service', 'started') in steps:
            try:
                if not adapter.service()['active']:
                    adapter.start()
                    did.append(f'started {osadapter.UNIT}')
            except osadapter.AdapterError as e:
                did.append(f'could not start {osadapter.UNIT}: {e}')
        if ('pre-hooks', 'started') in steps and ('post-hooks', 'done') not in steps:
            did += [_run_command(p, cfg.home, HOOK_TIMEOUT)[1] for p in cfg.post_hooks]
        line = json.dumps({'at': _stamp(), 'run': run_id, 'found': bool(mine), 'did': did}) + '\n'
        with open(os.path.join(cfg.state_dir, 'guard.log'), 'a', encoding='utf-8') as f:
            f.write(line)
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
        print(f't3-golive: the copy failed: {type(e).__name__}: {e}\nT3 was started again if the run got that '
              f'far; the copy folder (if any) has no manifest and is never used. {journal.SETTLE_HINT}', file=err)
        return 1
    print(f'copy done: {result["copy"]} ({result["bytes"] // MB} MB, t3 {result["liveVersion"]}); read back, '
          'manifest written, T3 runs again', file=out)
    return 0
