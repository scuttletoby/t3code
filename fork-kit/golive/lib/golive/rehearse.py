"""`t3-golive rehearse`: the target build tried on a CLONE of a finished copy, in its own T3 home and port
(R0-R5, README.md). It never stops, starts or writes the live T3 home. R2 migrates with `auth session list`: a
read-only command whose database layer (layerCliConfig) migrates under the ownership lock (there is no
`t3 project list`). A run is a scratch run (journal.run(scratch=True)): it changes only <backupDir>/t3-rehearsal/,
which the next run throws away and redoes, so a stop needs no settling. The old folder is removed only under
_guard_old (in R0 and again right before the removal), its mark last, so a run killed half-way leaves a folder the
next run still knows as its own.
"""
import ctypes, json, os, shutil, signal, socket, stat, subprocess, tempfile, time

from golive import coldcopy, journal, live, osadapter

REHEARSAL, MARK = 't3-rehearsal', '.golive-rehearsal'
# Left out of the clone, on top of what a copy never holds (coldcopy.SKIP): secrets, and the live machine's process
# records (device hub/daemon and relay clients), which a rehearsal server could act on (a daemon stop, a reap).
NOT_CLONED = frozenset({'secrets', 'device', 'relay-clients'})
NEVER_IN_CLONE = ('server-runtime.json', coldcopy.OWNER_LOCK, 'secrets', 'device', 'relay-clients')  # R0 checks
LIVE_DEFAULT_PORT = 3773
POLL, READY_WAIT, STOP_WAIT, T3_TIMEOUT, MIGRATE_TIMEOUT, MB = 0.5, 300, 60, 120, 3600, 1024 * 1024
MIGRATE = ('auth', 'session', 'list', '--json')  # R2: read-only, offline, migrates (tests/contract.test.py)


def _env(empty):
    return {'HOME': empty, 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'}  # nothing else from this environment


class Failed(Exception):
    """The rehearsal ran and found a problem, or could not finish. Only the clone changed: nothing to settle."""


def _die_with_parent():
    """In the child: SIGKILL when the kit dies (Linux PR_SET_PDEATHSIG), so a killed run leaves no server behind."""
    ctypes.CDLL(None, use_errno=True).prctl(1, signal.SIGKILL)


def _t3(argv, timeout=T3_TIMEOUT):
    """Run a t3 build with an empty HOME and a bare PATH, nothing else from this environment."""
    with tempfile.TemporaryDirectory(prefix='golive-rehearse-') as empty:
        try:
            return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                                  cwd=empty, env=_env(empty), preexec_fn=_die_with_parent)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise Failed(f'{" ".join(argv[:3])} failed: {e}') from None


def _preflight(binary, home, protocol, version):
    """The status ('ready' or 'blocked') the build's __service-preflight gives for the clone's database."""
    p = _t3([binary, '__service-preflight', '--database-path', os.path.join(home, 'userdata', live.DATABASE),
             '--launcher-protocol', str(protocol)])
    try:
        got = json.loads(p.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        got = None
    if not (isinstance(got, dict) and got.get('status') in ('ready', 'blocked') and got.get('version') == version):
        raise Failed(f'the preflight of t3 {version} gave no answer of that build (exit {p.returncode}): '
                     f'{(p.stdout + p.stderr).strip()[-300:]}')
    return got['status'], got.get('reason')


def _ids(path):
    """(st_dev, st_ino) of path and of each folder above it: the same folder under any name, a bind path too."""
    out, path = set(), os.path.abspath(path)
    while True:
        try:
            st = os.stat(path)
            out.add((st.st_dev, st.st_ino))
        except OSError:
            pass
        if os.path.dirname(path) == path:
            return out
        path = os.path.dirname(path)


def _overlap(path, other):
    """True unless `path` and `other` (which exists) are apart: neither inside the other, by real path and file id."""
    real, other_real = os.path.realpath(path), os.path.realpath(other)
    st = os.stat(other)
    try:
        mine = os.stat(path)
    except FileNotFoundError:
        mine = None
    return (os.path.commonpath([real, other_real]) in (real, other_real) or (st.st_dev, st.st_ino) in _ids(path)
            or (mine is not None and (mine.st_dev, mine.st_ino) in _ids(other)))


def _not_live(path, home):
    """Refused unless `path` and the live home are apart: neither inside the other, by real path, by the file id of
    the home and of its userdata (a bind of either keeps its id), and by the mount list (_not_bound_live)."""
    userdata = os.path.join(home, 'userdata')
    if _overlap(path, home) or (os.path.isdir(userdata) and _overlap(path, userdata)):
        raise journal.Refused(f'{path} is the live T3 home {home}, or inside it or around it; a rehearsal runs only '
                              'on a clone apart from it; nothing was changed')
    _not_bound_live(path, home)


def _not_bound_live(path, home):
    """Refused when the mount list shows `path` and the live home as the same folder of one filesystem, or one inside
    the other: a bind of any folder inside the live home (at the backups folder, say) keeps the device and gives no
    file id the kit compares, but its mount names the folder it came from. Refused too when the list cannot tell."""
    try:
        entries = osadapter.mounts()
    except osadapter.Unsupported as e:
        raise journal.Refused(f'{e}: the kit cannot tell whether {path} is a bind mount of a folder of the live T3 '
                              'home; nothing was changed') from None
    mine = osadapter.source(os.path.realpath(path), entries)
    live_one = osadapter.source(os.path.realpath(home), entries)
    if mine is None or live_one is None:
        raise journal.Refused(f'the mount list {osadapter.MOUNTINFO} holds no mount of {path} or of {home}: the kit '
                              'cannot tell whether one is a bind mount of the other; nothing was changed')
    if mine[0] == live_one[0] and os.path.commonpath([mine[1], live_one[1]]) in (mine[1], live_one[1]):
        raise journal.Refused(f'{path} is, by a bind mount, {mine[1]} of device {mine[0]}: the live T3 home {home} '
                              f'({live_one[1]}), or inside it or around it; a rehearsal runs only on a clone apart '
                              'from it; nothing was changed')


def _port_free(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def _choose_port(cfg, port):
    taken = {LIVE_DEFAULT_PORT}
    try:
        with open(os.path.join(cfg.home, 'userdata', 'server-runtime.json'), encoding='utf-8') as f:
            taken.add(json.load(f).get('port'))
    except (OSError, ValueError, AttributeError):
        pass
    if port is None:
        while port is None or port in taken:
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                port = s.getsockname()[1]
    if port in taken:
        raise journal.Refused(f'port {port} is the live port (or T3\'s default); nothing was changed')
    if not _port_free(port):
        raise journal.Refused(f'port {port} is in use on 127.0.0.1; nothing was changed')
    return port


def _newest_copy(cfg):
    names = sorted(n for n in os.listdir(cfg.backup_dir) if n.startswith('t3-copy-')) if os.path.isdir(
        cfg.backup_dir) else []
    if not names:
        raise journal.Refused(f'no copy in {cfg.backup_dir}: run `t3-golive copy` first; nothing was changed')
    return os.path.join(cfg.backup_dir, names[-1])


def _prechecks(cfg, copy_dir, port):
    """R0 before any step (journal.Refused, nothing changed): the copy, the builds, the rehearsal home, the port."""
    copy_dir, root = os.path.normpath(copy_dir or _newest_copy(cfg)), os.path.join(cfg.backup_dir, REHEARSAL)
    try:
        m = coldcopy.read_manifest(copy_dir, cfg.home)
        coldcopy.verify_copy(copy_dir)
    except (coldcopy.Incomplete, coldcopy.WrongCopy) as e:
        raise journal.Refused(f'{e}; pass --copy <a finished copy> or run `t3-golive copy`; nothing was '
                              'changed') from None
    old_version = m.get('liveVersion')
    try:
        active, _ = live.service_state(cfg.home)
        with open(os.path.join(cfg.home, 'runtime', 'service-state.json'), encoding='utf-8') as f:
            protocol = json.load(f)['protocol']
    except (live.NotLive, OSError, ValueError, KeyError) as e:
        raise journal.Refused(f'the live service state cannot be read: {e}; nothing was changed') from None
    if active != old_version:
        raise journal.Refused(f'{copy_dir} was taken under t3 {old_version}, but the live home now runs t3 {active}: '
                              'take a new copy; nothing was changed')
    if live.compare_versions(cfg.target_version, old_version) <= 0:
        raise journal.Refused(f'the target {cfg.target_version} is not newer than the copy\'s t3 {old_version}')
    old = os.path.join(cfg.home, 'runtime', 'versions', old_version, 't3')
    if not (os.path.isfile(old) and os.access(old, os.X_OK)):
        raise journal.Refused(f'{old} (the old build) is missing; nothing was changed')
    try:
        fork, target_sha = live.staged_build(cfg.stage_dir, cfg.target_version)
    except live.NotLive as e:
        raise journal.Refused(f'{e}; nothing was changed') from None
    helped = _t3([fork, 'serve', '--help'])
    if '--hold-work' not in helped.stdout + helped.stderr:
        raise journal.Refused(f'{fork} has no `serve --hold-work`: R3 would start work on the clone; nothing was '
                              'changed')
    _guard_old(cfg, root)
    if _overlap(root, copy_dir):  # the clone step would remove the copy (or write into it)
        raise journal.Refused(f'the copy {copy_dir} is inside the rehearsal folder {root} (or around it, or a link '
                              'into it): the clone step would remove it; move the copy out; nothing was changed')
    files = [e for e in m['files'] if isinstance(e, dict) and e.get('kind') == 'file']
    need = 3 * sum(e.get('bytes', 0) for e in files) + cfg.free_floor_mb * MB  # clone, growth, restore's kept files
    if coldcopy._free_bytes(coldcopy._existing(cfg.backup_dir)) < need:
        raise journal.Refused(f'{cfg.backup_dir} needs {need // MB} MB free for the rehearsal; nothing was changed')
    return {'copy': copy_dir, 'manifest': m, 'from': old_version, 'protocol': protocol, 'old': old, 'fork': fork,
            'targetSha256': target_sha, 'root': root, 'home': os.path.join(root, 'home'),
            'port': _choose_port(cfg, port)}


def _guard_old(cfg, root):
    """Refused unless the rehearsal folder is apart from the live home and an old one may be removed: a real folder
    (no link) that bears the kit's mark (or is empty: a removal killed after the mark), with no folder in it that is
    the live home or on another device, no mount point at or under it in the system's mount list (a bind mount of
    the same filesystem keeps st_dev; rmtree would go into it), no link to another home's lock, and no server on it.
    Where the mount list cannot be read (another OS, no /proc) an old folder is never removed. Returns the old
    folder's (st_dev, st_ino) as checked, for _remove; None when there is none."""
    _not_live(root, cfg.home)
    try:
        here = os.lstat(root)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(here.st_mode):
        raise journal.Refused(f'{root} is a link or not a folder: move it away; nothing was changed')
    try:
        marked = stat.S_ISREG(os.lstat(os.path.join(root, MARK)).st_mode)
    except FileNotFoundError:
        marked = False
    if not marked and os.listdir(root):
        raise journal.Refused(f'{root} was not made by a rehearsal (no {MARK} in it): move it away; nothing was '
                              'changed')
    live_ids = {(st.st_dev, st.st_ino) for st in (os.stat(cfg.home), os.stat(os.path.join(cfg.home, 'userdata')))}
    for d, _, _ in os.walk(root):
        st = os.lstat(d)
        if (st.st_dev, st.st_ino) in live_ids or st.st_dev != here.st_dev:
            raise journal.Refused(f'{d} in the old rehearsal folder is the live T3 home or a mount point: unmount '
                                  'it; nothing was changed')
    try:
        points = osadapter.mount_points()
    except osadapter.Unsupported as e:
        raise journal.Refused(f'{e}: the old rehearsal folder {root} is removed only when no mount can be in it; '
                              'remove it by hand; nothing was changed') from None
    real = os.path.realpath(root)
    inside = [m for m in points if os.path.commonpath([real, os.path.normpath(m)]) == real]
    if inside:
        raise journal.Refused(f'{", ".join(inside)} in the old rehearsal folder is a mount point: unmount it; '
                              'nothing was changed')
    old_userdata = os.path.join(root, 'home', 'userdata')
    if os.path.isdir(old_userdata):
        if os.path.realpath(os.path.join(old_userdata, coldcopy.OWNER_LOCK)) != os.path.join(
                real, 'home', 'userdata', coldcopy.OWNER_LOCK):
            raise journal.Refused(f'{old_userdata} or its {coldcopy.OWNER_LOCK} is a link (it would lead to another '
                                  'home\'s lock): move the old rehearsal folder away; nothing was changed')
        try:
            coldcopy.hold_owner_lock(old_userdata, 0).close()
        except TimeoutError:
            raise journal.Refused(f'a T3 server still holds the old rehearsal home {old_userdata} (see its '
                                  'server-runtime.json): stop it; nothing was changed') from None
    return here.st_dev, here.st_ino


def _remove(root, checked):
    """Remove the old rehearsal folder that _guard_old checked (`checked`: its (st_dev, st_ino)), its mark last: a
    run killed half-way leaves a folder that still bears the mark (or an empty one), which the next run removes under
    the same guard. The folder is opened without following a link, must be the one checked, and is emptied through
    that handle (rmtree's own walk never follows a link either): a swap of the path after the check, to a link or to
    another folder, removes nothing of what is there now."""
    try:
        fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise Failed(f'{root} changed after the check (a link now, or not a folder: {e.strerror}): nothing was '
                     'removed') from None
    try:
        st = os.fstat(fd)
        if (st.st_dev, st.st_ino) != checked:
            raise Failed(f'{root} changed after the check (another folder now): nothing was removed')
        for name in os.listdir(fd):
            if name != MARK:
                if stat.S_ISDIR(os.lstat(name, dir_fd=fd).st_mode):
                    shutil.rmtree(name, dir_fd=fd)
                else:
                    os.unlink(name, dir_fd=fd)
        try:
            os.unlink(MARK, dir_fd=fd)
        except FileNotFoundError:
            pass
    finally:
        os.close(fd)
    try:
        os.rmdir(root)  # only ever an empty folder
    except OSError as e:
        raise Failed(f'{root} changed during the removal ({e.strerror}): the old folder was emptied through its '
                     'own handle and what is at that path now was left as it is; move it away') from None


def _clone(run, cfg, p):
    """The clone step: the old rehearsal folder (it bears MARK) goes, a new one is made; read back vs the manifest.
    The guard runs again right before the removal: a mount or a swap since R0 is caught."""
    try:
        checked = _guard_old(cfg, p['root'])
    except journal.Refused as e:
        raise Failed(f'the clone step: {e}') from None
    if checked is not None:
        _remove(p['root'], checked)
    os.makedirs(p['root'], mode=0o700)
    with open(os.path.join(p['root'], MARK), 'w', encoding='utf-8') as f:
        json.dump({'run': run.id, 'copy': p['copy']}, f)
    entries = coldcopy.copy_userdata(run, p['copy'], p['home'], None, NOT_CLONED)[0]
    left = coldcopy.SKIP | NOT_CLONED
    want = {e['path']: e.get('sha256') for e in p['manifest']['files']
            if isinstance(e, dict) and e.get('kind') == 'file' and str(e.get('path')).split('/')[0] not in left}
    if {e['path']: e['sha256'] for e in entries if e['kind'] == 'file'} != want:
        raise Failed(f'the clone in {p["home"]} does not read back as the copy\'s manifest')


def _r0_clone(cfg, p):
    """R0 on the clone itself, apart from how it was made: nothing in it may lead to the live T3."""
    _not_live(p['home'], cfg.home)
    found = [n for n in NEVER_IN_CLONE if os.path.lexists(os.path.join(p['home'], 'userdata', n))]
    if found:
        raise Failed(f'the clone holds {", ".join(found)}: it could reach the live T3; nothing ran on it')


def _report(p):
    try:
        return live.fork_report(p['fork'], p['home'])
    except live.NotLive as e:
        raise Failed(str(e)) from None


def _db(report):
    return report.get('database') if isinstance(report.get('database'), dict) else {}


def _applied(database):
    return [m.get('id') for m in database.get('appliedMigrations') or [] if isinstance(m, dict)]


def _instances(report):
    s = report.get('settings') if isinstance(report.get('settings'), dict) else {}
    return sorted(str(i.get('id')) for i in s.get('providerInstances') or [] if isinstance(i, dict))


def _r4(cfg, before, after):
    """Every R4 problem, report against report (fork #181), [] when all hold."""
    said = []
    if after.get('version') != cfg.target_version:
        said.append(f'version: the server reports {after.get("version")}, not {cfg.target_version}')
    applied, now = _applied(_db(before)), _applied(_db(after))
    expected = applied + list(_db(before).get('pendingMigrations') or [])
    if now != expected or _db(after).get('pendingMigrations') or _db(after).get('unknownMigrations'):
        said.append(f'migrations: applied {now[len(applied):]}, expected {expected[len(applied):]}, still pending '
                    f'{_db(after).get("pendingMigrations")}, unknown {_db(after).get("unknownMigrations")}')
    said += [f'counts: {d}' for d in live.compare_counts(before, after)]
    fork = after.get('fork') if isinstance(after.get('fork'), dict) else {}
    if fork.get('featuresOn') != []:
        said.append(f'fork features on: {fork.get("featuresOn")}')
    settings = after.get('settings') if isinstance(after.get('settings'), dict) else {}
    if settings.get('parseWarning') is not None:
        said.append(f'settings parse warning: {settings.get("parseWarning")}')
    if not _instances(before) or _instances(after) != _instances(before):
        said.append(f'provider instances: {_instances(after)} after, {_instances(before)} in the copy')
    return said


def _serve(p, empty):
    log = open(os.path.join(p['root'], 'server.log'), 'wb')
    try:
        return subprocess.Popen([p['fork'], 'serve', '--hold-work', '--host', '127.0.0.1', '--port', str(p['port']),
                                 '--no-browser', '--base-dir', p['home']], stdin=subprocess.DEVNULL, stdout=log,
                                stderr=subprocess.STDOUT, cwd=empty, preexec_fn=_die_with_parent, env=_env(empty))
    finally:
        log.close()


def _wait_ready(p, server):
    """The server wrote its runtime record in the clone: its own pid, the port asked for, work held."""
    end, path = time.monotonic() + READY_WAIT, os.path.join(p['home'], 'userdata', 'server-runtime.json')
    while time.monotonic() < end:
        if server.poll() is not None:
            raise Failed(f'the rehearsal server exited before it was ready (exit {server.returncode}; a port taken '
                         f'since the check?): see {os.path.join(p["root"], "server.log")}')
        try:
            with open(path, encoding='utf-8') as f:
                rt = json.load(f)
        except (OSError, ValueError):
            rt = {}
        if isinstance(rt, dict) and rt.get('pid') == server.pid:
            if rt.get('port') != p['port'] or rt.get('workHeld') is not True:
                raise Failed(f'the rehearsal server runs on port {rt.get("port")} with workHeld '
                             f'{rt.get("workHeld")}, not port {p["port"]} with work held')
            return
        time.sleep(POLL)
    raise Failed(f'the rehearsal server was not ready within {READY_WAIT} s')


def _stop(server):
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(STOP_WAIT)
        except subprocess.TimeoutExpired:
            server.kill()
    return server.wait()


def run_rehearse(cfg, copy_dir=None, port=None, say=print):
    """The rehearsal, in this process. Returns the result (also the journal's "result" step); raises
    journal.Refused (nothing changed), journal.Busy / Unfinished, or Failed (only the clone changed)."""
    began = time.monotonic()
    with journal.run(cfg.state_dir, 'rehearse', scratch=True) as run:
        p = _prechecks(cfg, copy_dir, port)
        result = {'copy': p['copy'], 'copySha256': coldcopy._hash(os.path.join(p['copy'], coldcopy.MANIFEST)),
                  'fromVersion': p['from'], 'targetVersion': cfg.target_version, 'targetSha256': p['targetSha256'],
                  'port': p['port']}
        with run.step('clone', copy=p['copy'], home=p['home']):
            _clone(run, cfg, p)
        _r0_clone(cfg, p)
        settings = coldcopy._file_sum(os.path.join(p['home'], 'userdata', 'settings.json'))
        with run.step('preflight') as facts:
            for name, binary, version in (('old', p['old'], p['from']), ('fork', p['fork'], cfg.target_version)):
                facts[name], why = _preflight(binary, p['home'], p['protocol'], version)
                if facts[name] != 'ready':
                    raise Failed(f'R1: the preflight of t3 {version} on the clone says {facts[name]}: {why}')
            before = _report(p)
        result['migrations'] = list(_db(before).get('pendingMigrations') or [])
        with run.step('migrate') as facts:
            size, t0 = live.database_bytes(p['home']), time.monotonic()
            done = _t3([p['fork'], *MIGRATE, '--base-dir', p['home']], MIGRATE_TIMEOUT)
            result['migrateSeconds'] = round(time.monotonic() - t0, 3)
            result['bytesGrown'] = live.database_bytes(p['home']) - size
            facts.update(seconds=result['migrateSeconds'], bytesGrown=result['bytesGrown'], exit=done.returncode)
            if done.returncode != 0:
                raise Failed(f'R2: {" ".join(MIGRATE)} exited {done.returncode}: {done.stderr.strip()[-300:]}')
            migrated = _db(_report(p))  # R2 must have migrated: R3's server would else migrate unmeasured
            facts['pendingAfter'] = migrated.get('pendingMigrations')
            if (_applied(migrated) != _applied(_db(before)) + result['migrations']
                    or migrated.get('pendingMigrations') != [] or migrated.get('unknownMigrations')):
                raise Failed(f'R2: {" ".join(MIGRATE)} left the clone with applied {_applied(migrated)[-5:]} (last '
                             f'5), pending {migrated.get("pendingMigrations")}, unknown '
                             f'{migrated.get("unknownMigrations")}; expected {result["migrations"]} applied: the '
                             'server would migrate unmeasured')
        say(f'R2: migrated {result["migrations"]} in {result["migrateSeconds"]} s' if result['migrations'] else
            'R2: no migrations were pending')
        server, problems = None, []
        with tempfile.TemporaryDirectory(prefix='golive-rehearse-home-') as empty:
            try:
                with run.step('serve', port=p['port']) as facts:
                    t0, server = time.monotonic(), _serve(p, empty)
                    facts['pid'] = server.pid
                    _wait_ready(p, server)
                    result['readySeconds'] = facts['seconds'] = round(time.monotonic() - t0, 3)
                with run.step('checks') as facts:
                    problems = _r4(cfg, before, _report(p))
                    if server.poll() is not None:
                        problems.append(f'the rehearsal server died during the checks (exit {server.returncode})')
                    facts['problems'] = problems
            finally:
                if server is not None:
                    with run.step('stop-server') as facts:
                        facts['exit'] = _stop(server)
        if problems:
            raise Failed('R4: ' + '; '.join(problems))
        with run.step('old-build-blocked') as facts:
            if coldcopy._file_sum(os.path.join(p['home'], 'userdata', 'settings.json')) != settings:
                raise Failed('R5: settings.json changed during the rehearsal')
            facts['old'], facts['reason'] = _preflight(p['old'], p['home'], p['protocol'], p['from'])
            want = 'blocked' if result['migrations'] else 'ready'
            if facts['old'] != want:
                raise Failed(f'R5: the old build\'s preflight on the migrated clone says {facts["old"]}; it should be '
                             f'{want}{"" if result["migrations"] else " (no migrations)"} (reason: {facts["reason"]})')
            result['oldBuildOnMigrated'] = facts['old']
        with run.step('restore') as facts:
            facts.update(coldcopy.restore(run, p['copy'], p['home'], from_home=cfg.home, version=p['from']))
            facts['old'], why = _preflight(p['old'], p['home'], p['protocol'], p['from'])
            if facts['old'] != 'ready':
                raise Failed(f'R5: the old build\'s preflight on the restored clone says {facts["old"]}: {why}')
        try:  # the switch installs the archive this sum names: it must be the one that ran here, start to end
            again = live.staged_build(cfg.stage_dir, cfg.target_version)[1]
        except live.NotLive as e:
            raise Failed(f'the staged target cannot be read again after the rehearsal: {e}') from None
        if again != p['targetSha256']:
            raise Failed(f'the staged target changed during the rehearsal (archive sha256 {p["targetSha256"]} -> '
                         f'{again}): rehearse again')
        result['totalSeconds'] = round(time.monotonic() - began, 3)
        with run.step('result') as facts:
            facts.update(result)
        return result


def main(cfg, copy_dir, port, as_json, out, err):
    try:
        say = lambda line: print(line, file=err if as_json else out, flush=True)  # noqa: E731
        result = run_rehearse(cfg, copy_dir, port, say=say)
    except journal.Refused as e:
        print(f'Refused: {e}', file=err)
        return 1
    except (journal.Busy, journal.Unfinished, journal.Damaged, Failed) as e:
        print(f't3-golive: rehearse: {e}', file=err)
        return 1
    print(json.dumps(result, indent=2) if as_json else
          f'rehearsal passed: t3 {result["fromVersion"]} -> {result["targetVersion"]} on a clone of {result["copy"]}; '
          f'migrations {result["migrations"]} in {result["migrateSeconds"]} s, server ready in '
          f'{result["readySeconds"]} s', file=out)
    return 0
