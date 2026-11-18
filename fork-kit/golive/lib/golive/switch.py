"""`t3-golive switch`: move the live T3 home to the staged target build (design 4.4), `verify` it (4.5), and go
back on its own (W1, 4.6) when verify fails or runs out of time.

The build that runs is the one the rehearsal ran: the switch installs the staged archive itself (its sha256 must be
the one `rehearse` journalled; the files are checked against it again after the copy into the home), so upstream's
`t3 update` finds the target already installed and never downloads it; verify ends by checking that the runtime the
server runs is still byte for byte that archive. Before any stop it keeps the old build's runtime outside the home
(keptruntime.keep: the update prunes it); the way back is #397's W1, back.restore_and_downgrade(automatic=True).

A guard (a second detached unit, `t3-golive switch-guard --run <id> --wait`, plus a timer that runs it again without
--wait) waits for the kit's lock and acts only on a switch run that was killed (no end line): it finishes what the
journal says (the old build started again, or W1 once), runs the post hooks, and ends the run "failed". It never
repeats a way back that ran: once W1 stopped T3, T3 stays stopped and is not started at boot until `t3-golive back
--way w1` ends ok (#397's W1 contract). A reboot ends both units; the journal then asks for `t3-golive back` by hand.

Every change is made inside a journalled step (journal.py); the refusals all come first.
"""
import datetime, json, math, os, shutil, subprocess, sys, time, urllib.error, urllib.request

from golive import back as back_mod, check as check_mod, coldcopy, journal, keptruntime, live, osadapter

MIN_LIMIT = 600     # verify's time limit: 3 x the rehearsal's time, at least this (seconds)
GUARD_MARGIN = 600  # the guard timer fires this long after the latest the run should have ended
FINAL = {'verify': 'verified', 'rolled-back': 'rolled back', 'back-start': 'old build runs'}
INSTALL = '.golive-install-'  # not a version name: upstream's install and prune pass it by
MB = coldcopy.MB
# The update runs with this environment only: no T3CODE_RELEASE_BASE_URL (or anything else) can point it elsewhere.
UPDATE_ENV = ('HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR', 'XDG_RUNTIME_DIR',
              'DBUS_SESSION_BUS_ADDRESS', 'T3CODE_INSTALL_BIN_DIR')
W1_HOLDS = ('T3 stays stopped and is not started at boot, and background jobs stay paused (on purpose: the home may '
            'be half-restored), until `t3-golive back --way w1` ends ok; `t3-golive status` says where it stopped')


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _t3(binary, args, timeout):
    """(exit code, output) of a t3 build run directly (never from PATH). Tests replace it."""
    env = {k: v for k, v in os.environ.items() if k in UPDATE_ENV}
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout, errors='replace',
                           stdin=subprocess.DEVNULL, cwd='/', env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return p.returncode, p.stdout + p.stderr


def _report(binary, home):
    return live.fork_report(back_mod.T3_STUB or binary, home)


def passed_rehearsal(cfg, from_version, target_sha):
    """The record of the last `rehearse` run if it passed for this target build (the staged archive's sha256) and
    from-version and is fresh; Refused otherwise. It reads the record `rehearse` (go-live PR 5) writes: the run ends
    "ok" and its step "result" is done with {fromVersion, targetVersion, targetSha256: the staged archive it ran,
    migrations: the ids the target runs, migrateSeconds, readySeconds: how long the target took to migrate and get
    ready on the clone}. The returned record adds `seconds`, their sum rounded up."""
    runs, _ = journal.read(cfg.state_dir)
    tried = [r for r in runs if r['command'] == 'rehearse']
    if not tried:
        raise journal.Refused('no rehearsal in the journal: run `t3-golive rehearse` first')
    last = tried[-1]
    data = next((s['data'] for s in last['steps'] if (s['step'], s['phase']) == ('result', 'done')), None)
    if not (last['end'] and last['end']['result'] == 'ok' and isinstance(data, dict)):
        raise journal.Refused(f'the last rehearsal (began {last["began"]}) did not pass: rehearse again')
    if data.get('targetVersion') != cfg.target_version:
        raise journal.Refused(f'the last rehearsal was for another target ({data.get("targetVersion")}): rehearse '
                              f'{cfg.target_version}')
    if data.get('targetSha256') != target_sha:
        raise journal.Refused(f'the last rehearsal ran another build of {cfg.target_version} (staged archive sha256 '
                              f'{data.get("targetSha256")}, staged now {target_sha}): rehearse again')
    if data.get('fromVersion') != from_version:
        raise journal.Refused(f'the last rehearsal moved t3 {data.get("fromVersion")}, the live T3 runs '
                              f'{from_version}: rehearse again')
    ended = datetime.datetime.strptime(last['end']['at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    age = _utcnow() - ended
    if age < datetime.timedelta(0):
        raise journal.Refused(f'the last rehearsal ended {last["end"]["at"]}, after the clock\'s now: check the clock, '
                              'then rehearse again')
    if age > datetime.timedelta(hours=cfg.rehearsal_max_age_hours):
        raise journal.Refused(f'the last rehearsal ({last["end"]["at"]}) is too old (rehearsalMaxAgeHours '
                              f'{cfg.rehearsal_max_age_hours}): rehearse again')
    took = [data.get('migrateSeconds'), data.get('readySeconds')]
    if not (isinstance(data.get('migrations'), list)
            and all(type(t) in (int, float) and 0 <= t < float('inf') for t in took)):
        raise journal.Refused('the rehearsal record has no migration list or time: rehearse again')
    return dict(data, seconds=math.ceil(sum(took)))


def launchers(home):
    """The `t3` links into this home's runtimes: those upstream recorded in runtime/launchers, and its default
    ($T3CODE_INSTALL_BIN_DIR or ~/.local/bin)/t3, as upstream's prune finds them."""
    try:
        with open(os.path.join(home, 'runtime', 'launchers'), encoding='utf-8') as f:
            found = [line.strip() for line in f if line.strip()]
    except OSError:
        found = []
    found.append(os.path.join(os.environ.get('T3CODE_INSTALL_BIN_DIR') or os.path.expanduser('~/.local/bin'), 't3'))
    versions = os.path.realpath(os.path.join(home, 'runtime', 'versions')) + os.sep
    return [p for p in dict.fromkeys(found) if os.path.islink(p) and os.path.realpath(p).startswith(versions)]


def _repoint(run, home, version):
    """Point every link into this home's runtimes at `version` (a new link renamed over the old one)."""
    run.require_step('repoint the t3 links')
    target, moved = os.path.join(home, 'runtime', 'versions', version, 't3'), []
    for path in launchers(home):
        if os.path.realpath(path) != os.path.realpath(target):
            tmp = f'{path}.golive-{os.getpid()}'
            os.symlink(target, tmp)
            os.replace(tmp, path)
            moved.append(path)
    return moved


def answers(port):
    """True when something answers HTTP on 127.0.0.1:<port> (any status)."""
    if type(port) is not int:
        return False
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        opener.open(f'http://127.0.0.1:{port}/', timeout=5).close()
    except urllib.error.HTTPError:
        return True
    except (OSError, ValueError):
        return False
    return True


def _identity(cfg, archive, sha):
    """[] when the home's runtime of the target holds exactly the staged archive the rehearsal ran (sha), and
    upstream's .install-complete; else what differs."""
    runtime = keptruntime.runtime_dir(cfg.home, cfg.target_version)
    try:
        got = live.runtime_matches(archive, runtime, extra_ok=(keptruntime.SENTINEL,))
    except live.NotLive as e:
        return [f'the installed target is not the rehearsed build: {e}']
    if got != sha:
        return [f'{archive} is not the rehearsed build any more (sha256 {got}, rehearsed {sha})']
    if keptruntime.installed_t3(cfg.home, cfg.target_version) is None:
        return [f'{runtime} is not a whole runtime (.install-complete)']
    return []


def _look(cfg, adapter, version, before, binary, build=None):
    """What is wrong with the live T3 now ([] when nothing): design 4.5, for `version`. The migrations must all have
    run, the counts must not shrink from the stopped home's before the update, the provider instances must be those,
    and (build=(archive, sha)) the server must run the rehearsed build, byte for byte."""
    try:
        svc = adapter.service()
        if not svc['active']:
            return [f'{osadapter.UNIT} is not active']
        server = live.live_server(cfg.home, svc['mainPid'])
        active, pending = live.service_state(cfg.home)
    except (live.NotLive, osadapter.AdapterError) as e:
        return [str(e)]
    said = [f'the server runs t3 {server["version"]}'] if server['version'] != version else []
    if active != version or pending:
        said.append(f'the service state names t3 {active}{" with an update pending" if pending else ""}')
    said += [f'marker {m}' for m in live.markers(cfg.home)]
    if not answers(server['port']):
        said.append(f'nothing answers HTTP on port {server["port"]}')
    want = os.path.realpath(os.path.join(cfg.home, 'runtime', 'versions', version, 't3'))
    said += [f'the t3 link {p} points at {os.path.realpath(p)}' for p in launchers(cfg.home)
             if os.path.realpath(p) != want]
    if said:
        return said
    try:
        report = _report(binary, cfg.home)
    except live.NotLive as e:
        return [f'report: {e}']
    db = report.get('database') if isinstance(report.get('database'), dict) else {}
    if db.get('pendingMigrations') != []:
        said.append(f'pending migrations {db.get("pendingMigrations")}, expected []')
    counts = db.get('counts') if isinstance(db.get('counts'), dict) else {}
    said += [f'{k}: {before["counts"].get(k)} before, {counts.get(k)} now' for k in live.COUNTS
             if not (type(counts.get(k)) is int and counts[k] >= before['counts'][k])]
    fork = report.get('fork') if isinstance(report.get('fork'), dict) else {}
    if fork.get('featuresOn') != []:
        said.append(f'fork features on: {fork.get("featuresOn")}')
    if check_mod.providers_sum(report) != before['providers']:
        said.append('the provider instances differ from before the switch')
    if report.get('ok') is not True:
        said.append('the report is not ok')
    changed = check_mod.server_changed(cfg, adapter, server)
    said += [changed] if changed else []
    if not said and build is not None:
        said += _identity(cfg, *build)
    return said


def verify(cfg, adapter, version, before, seconds, build=None):
    """Look until all of design 4.5 holds or `seconds` pass: (True, facts) or (False, {'problems'})."""
    binary = keptruntime.home_t3(cfg.home, version)  # the installed target: build= checks it is the rehearsed one
    end = time.monotonic() + seconds
    while True:
        said = _look(cfg, adapter, version, before, binary, build)
        if not said:
            return True, {'version': version}
        if time.monotonic() >= end:
            return False, {'problems': said[:8]}
        time.sleep(coldcopy.POLL)


def _copy_extras(cfg, unit_path, dest, deadline):
    """Into dest/extra: service-state.json and the unit file, entries hashed (the old runtime is kept by
    keptruntime.keep, outside the copy)."""
    sources = [(os.path.join(cfg.home, 'runtime', 'service-state.json'), 'runtime/service-state.json')]
    if unit_path:
        sources.append((unit_path, f'unit/{os.path.basename(unit_path)}'))
    entries = []
    for src, rel in sources:
        out = os.path.join(dest, 'extra', rel)
        os.makedirs(os.path.dirname(out), 0o700, exist_ok=True)
        size, digest = coldcopy._copy_file(src, out, deadline, 0o600)
        entries.append({'path': rel, 'from': src, 'bytes': size, 'sha256': digest})
    return entries


def _tree_bytes(path):
    return sum(os.lstat(os.path.join(d, n)).st_size for d, _, names in os.walk(path) for n in names)


def _disk(cfg, copy_bytes, keep_bytes, install_bytes):
    """Refused unless each disk keeps the floor free after what lands on it: the copy (backupDir), the kept old
    runtime (stateDir) and the installed target (the home)."""
    need = {}
    for folder, size in ((cfg.backup_dir, copy_bytes), (cfg.state_dir, keep_bytes), (cfg.home, install_bytes)):
        path = coldcopy._existing(folder)
        entry = need.setdefault(coldcopy._device(path), [path, cfg.free_floor_mb * MB, []])
        entry[1] += size
        entry[2].append(f'{os.path.basename(folder)} {size // MB} MB')
    for path, want, what in need.values():
        free = coldcopy._free_bytes(path)
        if free < want:
            raise journal.Refused(f'{path}: {free // MB} MB free, needs {want // MB} MB ({", ".join(what)}, floor '
                                  f'{cfg.free_floor_mb} MB); nothing was changed')


def _install_target(run, cfg, archive, sha):
    """Inside a step: put the staged runtime into <home>/runtime/versions/<target>/ as upstream's install leaves it
    (the archive's files and .install-complete naming the version), so the update finds it installed and downloads
    nothing. Copied to a staging folder first, checked against the archive (sha) there, renamed into place whole.
    A target folder already there is used when it is that build, else set aside, never deleted. Safe to run again."""
    run.require_step('install the target build')
    v = cfg.target_version
    target, versions = keptruntime.runtime_dir(cfg.home, v), os.path.join(cfg.home, 'runtime', 'versions')
    if os.path.isdir(target) and not os.path.islink(target) and not _identity(cfg, archive, sha):
        return {'runtime': target, 'installed': False}
    staging = os.path.join(versions, f'{INSTALL}{v}')
    if os.path.lexists(staging):  # this kit's own unfinished copy from an install that was stopped
        shutil.rmtree(staging)
    src = os.path.join(cfg.stage_dir, v)
    keptruntime._copy_tree(src, staging, keptruntime._entries(src))
    with open(os.path.join(staging, keptruntime.SENTINEL), 'w', encoding='utf-8') as f:
        f.write(f'{v}\n')
        f.flush()
        os.fsync(f.fileno())
    os.chmod(staging, 0o755)
    try:
        got = live.runtime_matches(archive, staging, extra_ok=(keptruntime.SENTINEL,))
    except live.NotLive as e:
        raise RuntimeError(f'the copy of the staged target does not match its archive: {e}') from None
    if got != sha:
        raise RuntimeError(f'{archive} changed since the rehearsal (sha256 {got}, rehearsed {sha}): not installed')
    facts = {'runtime': target, 'installed': True}
    if os.path.lexists(target):
        aside = os.path.join(versions, f'.golive-aside-{v}-{keptruntime._stamp()}')
        os.rename(target, aside)
        facts['setAside'] = aside
    os.rename(staging, target)
    keptruntime._fsync_dir(versions)
    if _identity(cfg, archive, sha):
        raise RuntimeError(f'{target} is not the rehearsed build after the install: {_identity(cfg, archive, sha)}')
    return facts


def _update(cfg, adapter, binary, version, timeout):
    code, out = _t3(binary, ['update', version, '--base-dir', cfg.home, '--yes'], timeout)
    if code != 0:
        raise RuntimeError(f'{binary} update {version} exited {code}: {out.strip()[-300:]}')
    if not adapter.service()['active']:
        adapter.start()


def _started(steps):
    return {s['step'] for s in steps if s['phase'] == 'started'}


def w1_holds(steps):
    """True when a W1 in these steps stopped T3 and did not end ok: T3 stays stopped and disabled, and the jobs
    paused, until `t3-golive back --way w1` ends ok (#397's W1 contract)."""
    done = {s['step'] for s in steps if s['phase'] == 'done'}
    return 'stop-service' in _started(steps) and 'rolled-back' not in done


def _put_back(run, cfg, adapter, steps, say):
    """After a failed or killed switch: what its journal says it got to decides. Its update started: W1, once
    (back.restore_and_downgrade, automatic); never again once a W1 stopped T3. Stopped before it: start the old
    build again (nothing in the home was changed but the target's runtime folder). Returns what was done, or None
    when T3 was never stopped."""
    rec = next((s['data'] for s in steps if (s['step'], s['phase']) == ('stopping', 'started')), None)
    if rec is None:
        return None
    started = _started(steps)
    if 'stop-service' in started:
        raise RuntimeError(f'the way back W1 already stopped T3 in this run and did not end ok, so it is not run '
                           f'again from here: {W1_HOLDS}')
    if 'update' in started:  # the forward update (a W1 plan comes only after it)
        say(f'going back to t3 {rec["version"]} (W1: restore {rec["copy"]}, then downgrade)')
        got = back_mod.restore_and_downgrade(run, cfg, adapter, rec['copy'], automatic=True, hooks=False, say=say)
        with run.step('rolled-back', version=got['version'], copy=rec['copy']) as facts:
            facts.update(version=got['version'], pid=got['pid'], result=FINAL['rolled-back'])
        return f'rolled back to t3 {got["version"]} from {rec["copy"]}'
    with run.step('back-start') as facts:
        stopped = not adapter.service()['active']  # active: the stop never happened, the old server still runs
        if stopped:
            adapter.start()
        again = coldcopy._wait_started(cfg, adapter, rec['version'], rec['pid'] if stopped else None)
        facts.update(version=again['version'], pid=again['pid'], result=FINAL['back-start'])
    return f'started t3 {again["version"]} again; nothing it reads was changed'


def final(run):
    """True when a switch run needs no putting back: it ended ok or refused, was settled, or its journal says
    "verified", "rolled back" or that the old build runs again."""
    if run['settled'] is not None or (run['end'] and run['end']['result'] in ('ok', 'refused')):
        return True
    return any(s['phase'] == 'done' and s['step'] in FINAL and (s['data'] or {}).get('result') == FINAL[s['step']]
               for s in run['steps'])


def guard_after(cfg, limit):
    """When the guard's timer fires, in seconds from the guard step: after the latest the run can end (each wait
    it has at its longest; the restore and the copy have the copy's limit), plus a margin. The timer's guard waits
    for the kit's lock, so a run still going then is waited for, never acted on."""
    hooks = (len(cfg.pre_hooks) + len(cfg.post_hooks)) * coldcopy.HOOK_TIMEOUT
    forward = (2 * coldcopy.STOP_WAIT + cfg.stop_limit_seconds + live.REPORT_TIMEOUT + coldcopy.HOOK_TIMEOUT
               + 2 * limit)
    way_back = coldcopy.STOP_WAIT + cfg.stop_limit_seconds + back_mod.UPDATE_TIMEOUT + coldcopy.START_WAIT
    return hooks + forward + way_back + GUARD_MARGIN


def _prechecks(cfg, adapter, owner_says_go, say, run):
    """Every refusal before the first step. Returns what the switch needs."""
    code, result = check_mod.run_check(cfg, adapter, own_run=run)
    if code == 0 and result.get('nothing') == 'already-on-target':
        return None
    if code != 0 or result.get('nothing'):
        raise journal.Refused('; '.join([f'{c["name"]}: {c["detail"]}' for c in result['checks']
                                         if not c['ok']] + result['info']) or 'check refused')
    server = result['live']
    counts = result.get('counts')
    if not (isinstance(counts, dict) and all(type(counts.get(k)) is int for k in live.COUNTS)):
        raise journal.Refused(f'the live report gave no counts ({counts}): the switch cannot tell what it must keep; '
                              'nothing was changed')
    try:
        binary, sha = live.staged_build(cfg.stage_dir, cfg.target_version)
        archive = live.staged_archive(cfg.stage_dir, cfg.target_version)[0]
    except live.NotLive as e:
        raise journal.Refused(f'{e}; nothing was changed') from None
    rehearsal = passed_rehearsal(cfg, server['version'], sha)
    if result.get('pendingMigrations') != rehearsal['migrations']:
        raise journal.Refused(f'the target would run migrations {result.get("pendingMigrations")}, the '
                              f'rehearsal ran {rehearsal["migrations"]}: rehearse again')
    why = coldcopy._home_problem(cfg)
    if why is not None:
        raise journal.Refused(f'{why}; nothing was changed')
    try:
        keep_bytes = keptruntime.plan_keep(cfg, server['version'], cfg.free_floor_mb * MB)
    except ValueError as e:
        raise journal.Refused(f'{e}; nothing was changed') from None
    install_bytes = _tree_bytes(os.path.join(cfg.stage_dir, cfg.target_version))
    _disk(cfg, coldcopy._plan_total(cfg), keep_bytes, install_bytes)
    if cfg.quiet_command is None and not owner_says_go:
        raise journal.Refused('the machine file has no golive.quietCommand: set one, or pass --owner-says-go')
    if not owner_says_go:
        coldcopy.wait_quiet(cfg, server['version'], say)
        _disk(cfg, coldcopy._plan_total(cfg), keep_bytes, install_bytes)
    why = check_mod.server_changed(cfg, adapter, server)
    if why is not None:
        raise journal.Refused(f'{why}; nothing was changed')
    dest = os.path.join(cfg.backup_dir, f't3-switch-{coldcopy._stamp()}-{server["version"]}')
    if os.path.lexists(dest):
        raise journal.Refused(f'{dest} already exists; a copy never goes over another: try again in a second')
    return {'server': server, 'binary': binary, 'archive': archive, 'sha': sha, 'rehearsal': rehearsal, 'dest': dest}


def _before(cfg, binary, migrations):
    """The stopped home's facts the verify compares with, read under the owner lock (nothing can write): raises
    unless the report is whole and says what the rehearsal said."""
    report = _report(binary, cfg.home)
    db = report.get('database') if isinstance(report.get('database'), dict) else {}
    counts = db.get('counts') if isinstance(db.get('counts'), dict) else {}
    fork = report.get('fork') if isinstance(report.get('fork'), dict) else {}
    facts = {'reportCounts': {k: counts.get(k) for k in live.COUNTS}, 'providers': check_mod.providers_sum(report),
             'pendingMigrations': db.get('pendingMigrations'), 'featuresOn': fork.get('featuresOn')}
    if not all(type(counts.get(k)) is int for k in live.COUNTS):
        raise RuntimeError(f'the stopped home\'s report gave no counts ({counts})')
    if report.get('ok') is not True or fork.get('featuresOn') != []:
        raise RuntimeError(f'the stopped home\'s report is not ok or has fork features on ({fork.get("featuresOn")})')
    if db.get('pendingMigrations') != migrations:
        raise RuntimeError(f'the stopped home would run migrations {db.get("pendingMigrations")}, the rehearsal ran '
                           f'{migrations}')
    return facts


def run_switch(cfg, adapter, owner_says_go=False, say=print):
    with journal.run(cfg.state_dir, 'switch') as run:
        p = _prechecks(cfg, adapter, owner_says_go, say, run)
        if p is None:
            say(f'the live T3 already runs {cfg.target_version}: nothing to do')
            return {'nothing': 'already-on-target'}
        server, dest, rehearsal = p['server'], p['dest'], p['rehearsal']
        old, build = server['version'], (p['archive'], p['sha'])
        with run.step('keep-runtime', version=old) as facts:  # the update prunes the old runtime W1 needs
            try:
                facts.update(keptruntime.keep(run, cfg, old))
            except Exception as e:  # a failed keep changed nothing live: the step is done, the run refuses
                facts['error'] = why = f'{type(e).__name__}: {e}'
        if 'error' in facts:
            raise journal.Refused(f'keeping the old build {old} failed: {why}; T3 was not stopped')
        limit = max(MIN_LIMIT, 3 * rehearsal['seconds'])
        rec = {'version': old, 'pid': server['pid'], 'copy': dest, 'limit': limit,
               'migrations': rehearsal['migrations'], 'targetSha256': p['sha']}
        with run.step('guard') as facts:
            unit = facts['unit'] = f't3-golive-switch-{os.getpid()}-{os.urandom(3).hex()}'
            guard = [coldcopy.KIT_BIN, 'switch-guard', '--run', run.id]
            try:
                adapter.run_detached(unit, guard + ['--wait'], guard_after_seconds=guard_after(cfg, limit),
                                     guard_argv=guard)
                facts['waiting'] = coldcopy._guard_waiting(cfg.state_dir, run.id)
            except osadapter.AdapterError as e:
                facts.update(waiting=False, error=str(e))
        if not facts['waiting']:
            raise journal.Refused(f'the guard {unit} did not come up within {coldcopy.GUARD_WAIT} s'
                                  f'{": " + facts["error"] if "error" in facts else ""}; T3 was not stopped')
        failed = coldcopy._hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed is None and not owner_says_go:
            ok, detail = coldcopy.quiet_now(cfg, old)
            failed = None if ok else f'not quiet any more: {detail}'
        failed = failed or check_mod.server_changed(cfg, adapter, server) or coldcopy._home_problem(cfg)
        if failed is None and not coldcopy._guard_alive(adapter, unit):
            failed = f'the guard {unit} is not running any more'
        if failed is not None:
            again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if again:
                raise RuntimeError(f'{failed}; T3 was not stopped; then a post hook failed: {again}')
            raise journal.Refused(f'{failed}; T3 was not stopped')
        say(f'stopping {osadapter.UNIT} (t3 {old}) to switch to {cfg.target_version}')
        error, lock = None, None
        try:
            with run.step('stopping', **rec):
                adapter.stop()
                coldcopy._wait_stopped(adapter, server['pid'])
                lock = coldcopy.hold_owner_lock(os.path.join(cfg.home, 'userdata'), coldcopy.STOP_WAIT)
                coldcopy._same_after_stop(cfg, server)
            deadline = coldcopy._clock() + cfg.stop_limit_seconds
            with run.step('cold-copy', dest=dest, copy=dest) as facts:
                entries, total, left_out = coldcopy.copy_userdata(run, cfg.home, dest, deadline)
                extras = _copy_extras(cfg, adapter.service()['unitPath'], dest, deadline)
                facts.update(bytes=total, leftOut=left_out, extras=len(extras))
            with run.step('read-back'):
                coldcopy.read_back(cfg.home, dest, entries, deadline)
                for e in extras:
                    for side in (os.path.join(dest, 'extra', e['path']), e['from']):
                        if coldcopy._hash(side, deadline, drop_cache=True) != e['sha256']:
                            raise RuntimeError(f'read-back: {side} differs from what the copy read')
            with run.step('manifest'):
                coldcopy.write_manifest(run, dest, {
                    'manifestVersion': coldcopy.MANIFEST_VERSION, 'complete': True, 'createdAt': coldcopy._stamp(),
                    'runId': run.id, 'home': cfg.home, 'liveVersion': old, 'bytes': total,
                    'files': entries, 'leftOut': left_out, 'extras': extras})
            with run.step('before') as facts:  # read now: nothing changed the home since the copy
                facts.update(_before(cfg, p['binary'], rehearsal['migrations']))
            before = {'counts': facts['reportCounts'], 'providers': facts['providers']}
            with run.step('old-preflight') as facts:
                with open(os.path.join(dest, 'extra', 'runtime', 'service-state.json'), encoding='utf-8') as f:
                    protocol = json.load(f)['protocol']
                code, out = _t3(server['binary'], ['__service-preflight', '--database-path',
                                                   os.path.join(dest, 'userdata', live.DATABASE),
                                                   '--launcher-protocol', str(protocol)], coldcopy.HOOK_TIMEOUT)
                start = out.rfind('{"')
                said = json.loads(out[start:]) if start >= 0 else {}
                facts['status'] = said.get('status')
                if said.get('status') != 'ready':
                    raise RuntimeError(f'the old build\'s preflight on the copy says {said.get("status")} (exit '
                                       f'{code}): {said.get("reason") or out.strip()[-200:]}')
            with run.step('install-target', version=cfg.target_version, sha256=p['sha']) as facts:
                facts.update(_install_target(run, cfg, *build))
            lock.close()
            lock = None
            with run.step('update', version=cfg.target_version) as facts:
                _update(cfg, adapter, keptruntime.home_t3(cfg.home, cfg.target_version), cfg.target_version, limit)
                facts['links'] = _repoint(run, cfg.home, cfg.target_version)
            with run.step('verify', version=cfg.target_version) as facts:  # done only when verified
                verified, found = verify(cfg, adapter, cfg.target_version, before, limit, build)
                if not verified:
                    facts.update(found)
                    raise RuntimeError(f'verify failed: {"; ".join(found["problems"])}')
                facts.update(found, sha256=p['sha'], result=FINAL['verify'])
        except Exception as e:
            error = e
        finally:
            if lock is not None:
                lock.close()
        errors = [error] if error else []
        if error is not None:
            try:
                did = _put_back(run, cfg, adapter, journal.read(cfg.state_dir)[0][-1]['steps'], say)
                if did:
                    errors[0] = RuntimeError(f'{error}; {did}')
            except Exception as e:
                notes = ''.join(f'; {n}' for n in getattr(e, '__notes__', ()))
                errors[0] = RuntimeError(f'{error}; then putting T3 back failed: {type(e).__name__}: {e}{notes}')
            errors[0].__cause__ = error
        if error is not None and w1_holds(journal.read(cfg.state_dir)[0][-1]['steps']):
            errors[0].add_note(f'no post hook ran: {W1_HOLDS}')
        else:
            failed = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if failed:
                errors.append(RuntimeError(f'post hook failed: {failed}'))
        if errors:
            for later in errors[1:]:
                errors[0].add_note(f'then: {type(later).__name__}: {later}')
            raise errors[0]
        say(f'verified: the live T3 runs {cfg.target_version}; the copy is {dest}')
        return {'verified': cfg.target_version, 'copy': dest}


def guard(cfg, adapter, run_id, wait, say=print):
    """The switch's guard (`t3-golive switch-guard`). It waits for the kit's lock (beside the run, and as the timer:
    a run, or a back, still going is waited for), then acts only on the switch run `run_id` if it is the last run
    and was killed (no end line; a run that ended put T3 back itself, or said loudly why not): it puts T3 back as
    _put_back says (W1 at most once: never after a W1 stopped T3), runs the post hooks if the run paused the jobs
    and no W1 holds them, and ends the run "failed". A journal it cannot read fails open: T3 is started if it is
    not active, and the post hooks run. Logs to <stateDir>/guard.log; returns what it did."""
    if wait:
        try:
            coldcopy._guard_log(cfg.state_dir, {'run': run_id, 'waiting': True})
        except OSError:
            pass
    did = []
    try:
        with journal.lock(cfg.state_dir, 'switch-guard', exclusive=True, wait=True):
            try:
                runs, _ = journal.read(cfg.state_dir)
            except (journal.Damaged, OSError) as e:
                runs = None
                did.append(f'journal unreadable, so T3 is started if it is not active: {e}')
                if not adapter.service()['active']:
                    adapter.start()
                    did.append(f'started {osadapter.UNIT}')
                did += [coldcopy._run_command(p, cfg.home, coldcopy.HOOK_TIMEOUT)[1] for p in cfg.post_hooks]
            mine = runs[-1] if runs and runs[-1]['id'] == run_id else None
            if runs is not None and (mine is None or mine['end'] is not None or mine['settled']):
                did.append(f'nothing to do: run {run_id} ended, was settled or is not the last run')
            elif mine is not None:
                r = journal.Run(cfg.state_dir, 'switch')
                r.id = run_id
                try:
                    if not final(mine):
                        did.append(_put_back(r, cfg, adapter, mine['steps'], say) or 'T3 was never stopped')
                except Exception as e:
                    notes = ''.join(f'; {n}' for n in getattr(e, '__notes__', ()))
                    did.append(f'putting T3 back failed: {type(e).__name__}: {e}{notes}')
                steps = journal.read(cfg.state_dir)[0][-1]['steps']
                if w1_holds(steps):
                    did.append(f'no post hook ran: {W1_HOLDS}')
                elif not _resumed(mine):
                    did.append(coldcopy._hooks(r, 'post-hooks', cfg.post_hooks, cfg.home) or 'post hooks ran')
                r._append('end', result='failed', detail=f'stopped half-way; the guard: {"; ".join(did)}'[:500])
    except journal.Busy as e:
        did.append(f'nothing done: {e}')
    for line in did:
        say(line)
    try:
        coldcopy._guard_log(cfg.state_dir, {'run': run_id, 'did': did})
    except OSError:
        pass
    return did


def _resumed(run):
    steps = {(s['step'], s['phase']) for s in run['steps']}
    return ('pre-hooks', 'started') not in steps or ('post-hooks', 'done') in steps


def main_switch(cfg, adapter, here, owner_says_go, out=sys.stdout, err=sys.stderr):
    if not here:
        unit = f't3-golive-switch-{coldcopy._stamp().lower()}'
        try:
            adapter.run_detached(unit, [coldcopy.KIT_BIN, 'switch', '--here']
                                 + (['--owner-says-go'] if owner_says_go else []))
        except osadapter.AdapterError as e:
            print(f't3-golive: {e}', file=err)
            return 1
        print(f'started detached as {unit}. Follow it with: journalctl --user -u {unit} -f', file=out)
        return 0
    if adapter.inside_service():
        print(f't3-golive: this runs inside {osadapter.UNIT}; stopping it would kill the switch half-way. Run '
              '`t3-golive switch` without --here (it runs detached).', file=err)
        return 1
    try:
        result = run_switch(cfg, adapter, owner_says_go, say=lambda line: print(line, file=out, flush=True))
    except (journal.Refused, journal.Busy, journal.Unfinished, journal.Damaged) as e:
        print(f't3-golive: {e}', file=err)
        return 1
    except Exception as e:
        notes = ''.join(f'\n{n}' for n in getattr(e, '__notes__', ()))
        print(f't3-golive: the switch failed: {type(e).__name__}: {e}{notes}\n`t3-golive status` says where T3 is; '
              f'{journal.SETTLE_HINT}', file=err)
        return 1
    return 0 if result else 1
