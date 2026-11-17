"""`t3-golive back`: the way back from a fork build (W1, W2, W3); `t3-golive status`: is T3 up and right?

W1 restore and downgrade (loses everything since the copy): the t3 the copy was taken under is put back into the
home's runtime/versions from the kit's kept copy when upstream pruned it (keptruntime: every `t3 update` prunes old
runtimes, so the switch keeps one first), the pre hooks run, the boot service is disabled (no start at boot on a
half-restored home) and stopped, the newest finished copy of this home is restored (coldcopy.restore: the migrated
database and settings.json are kept aside, never deleted; the copy stays where it is), then that t3 runs
`update <old> --allow-downgrade --yes` (it enables and starts the service), that build is verified, and the post
hooks run (after a failure too, as in the copy run). A W1 that is stopped or fails after the stop leaves T3 stopped
and not started at boot, on purpose, until a back ends ok. By hand it needs `--lose-work-since <the copy's time>`;
the switch calls restore_and_downgrade(automatic=True) inside its own run.
W2 (loses nothing): the fork adds no migrations, so the upstream nightly a fork build is cut from (its version
without `.fork.N`) runs the migrated database as it is. Read from the code, untried on a live home.
W3: an upstream stable newer than that base, once one is installed or staged. W2 and W3 run that build's
`update <version> --allow-downgrade --yes` on the data as it is, between the pre and the post hooks.

A back may follow a back or switch that stopped half-way (journal.run may_follow): it finishes that back's plan
(journalled in its "plan" step); the restore, the put-back and the update are safe to run again. A back after a good
back of the same way, with nothing unfinished since, finds the build that back verified running and does nothing.
"""
import functools, http.client, json, os, re, subprocess, sys, urllib.error, urllib.request

from golive import check as check_mod, coldcopy, journal, keptruntime, live, osadapter
from golive.config import EXACT_VERSION
from golive.keptruntime import installed_t3

UPDATE_TIMEOUT, HTTP_TIMEOUT, MB = 900, 5, 1024 * 1024
T3_STUB = None  # tests only: a program run in place of any t3 the kit runs
FOLLOWS = ('back', 'switch')
_FORK = re.compile(r'\.fork\.\d+$')


def base_of(version):
    """The upstream nightly a fork build is cut from (`.fork.N` off its version), or None for another build."""
    base = _FORK.sub('', version or '')
    return base if base != version and '-nightly.' in base else None


def old_t3(cfg, version):
    """(t3 path in the home, kept folder or None, None) when W1 can run the t3 of `version`: installed whole in the
    home, or kept by the kit (keptruntime) and put back before the downgrade; else (None, None, why)."""
    t3 = installed_t3(cfg.home, version)
    if t3:
        return t3, None, None
    kept, why = keptruntime.check(cfg, version)
    if kept:
        return keptruntime.home_t3(cfg.home, version), kept, None
    return None, None, (f'the t3 {version} it was taken under is not kept: not in '
                        f'{os.path.join(cfg.home, "runtime", "versions")} (every upstream `t3 update` prunes old '
                        f'runtimes: the switch, W2 and W3 each run one) and {why}')


def _upstream_t3(cfg, version):
    """(t3 or None, where or why) of an upstream build: installed in the home, or staged with matching checksums."""
    t3 = installed_t3(cfg.home, version)
    if t3:
        return t3, 'installed in the home'
    try:
        return live.staged_runtime(cfg.stage_dir, version), f'staged in {cfg.stage_dir}'
    except live.NotLive as e:
        return None, str(e)


def copies(cfg):
    """Read only: [(folder, manifest)] of the finished copies of this home in backupDir, newest first."""
    try:
        names = [n for n in os.listdir(cfg.backup_dir) if n.startswith('t3-copy-')]
    except FileNotFoundError:
        return []
    found = []
    for n in names:
        try:
            found.append((os.path.join(cfg.backup_dir, n), coldcopy.read_manifest(os.path.join(cfg.backup_dir, n),
                                                                                cfg.home)))
        except (coldcopy.Incomplete, coldcopy.WrongCopy):
            pass
    return sorted(found, key=lambda f: str(f[1].get('createdAt')), reverse=True)


def ways(cfg, adapter):
    """Read only: {'w1'|'w2'|'w3': {'ok', 'why', 'version', 't3', ...}}: which way back is possible now, and why."""
    try:
        active = live.service_state(cfg.home)[0]
    except live.NotLive:
        active = None
    found, out = copies(cfg), {}
    if not found:
        bad = coldcopy.incomplete_copies(cfg.backup_dir)
        out['w1'] = {'ok': False, 'why': f'no finished copy of this home in {cfg.backup_dir}' + (
            f'; incomplete copies are never used: {", ".join(f for f, _ in bad)}' if bad else '')}
    else:
        folder, m = found[0]
        old, at = m.get('liveVersion'), m.get('createdAt')
        t3, kept, why = old_t3(cfg, old)
        w1 = out['w1'] = {'ok': False, 'version': old, 't3': t3, 'copy': folder, 'kept': kept}
        if t3 and active and live.compare_versions(old, active) >= 0:
            w1['why'] = f'the newest copy, {folder}, was taken under t3 {old}, not older than the t3 {active} now'
        elif t3 is None:
            w1['why'] = f'the newest copy is {folder}, but {why}'
        else:
            w1.update(ok=True, why=f'restore {folder} (taken {at} under t3 {old}) and go back to t3 {old}'
                                   + (f' (its runtime is put back from {kept})' if kept else '')
                                   + f': everything since {at} is lost; by hand: --lose-work-since {at}')
    base = base_of(active)
    if base is None:
        why = f'the service runs t3 {active}, not a fork build (.fork.N): W2 and W3 go from a fork build to upstream'
        out['w2'], out['w3'] = {'ok': False, 'why': why}, {'ok': False, 'why': why}
        return out
    t3, where = _upstream_t3(cfg, base)
    out['w2'] = {'ok': bool(t3), 'version': base, 't3': t3, 'why': (
        f'the upstream nightly t3 {base} the fork build is cut from, on the data as it is ({where}); untried live'
        if t3 else f't3 {base} is not available: {where}')}
    names = set()
    for folder in (cfg.stage_dir, os.path.join(cfg.home, 'runtime', 'versions')):
        try:
            names.update(os.listdir(folder))
        except OSError:
            pass
    stable = sorted((v for v in names if EXACT_VERSION.match(v) and '-' not in v.split('+')[0]
                     and live.compare_versions(v, base) > 0), key=functools.cmp_to_key(live.compare_versions))
    out['w3'] = {'ok': False, 'why': f'no upstream stable newer than {base} is installed or staged'}
    for v in reversed(stable):
        t3, where = _upstream_t3(cfg, v)
        if t3:
            out['w3'] = {'ok': True, 'version': v, 't3': t3, 'why': f'the upstream stable t3 {v} ({where})'}
            break
        out['w3']['why'] += f'; t3 {v}: {where}'
    return out


def _server(cfg, adapter):
    """The live server of the boot service, or None."""
    try:
        svc = adapter.service()
        return live.live_server(cfg.home, svc['mainPid']) if svc['active'] else None
    except (live.NotLive, osadapter.AdapterError):
        return None


def live_report(cfg, adapter):
    """(report, None), from the live fork build, read only beside a T3 running steadily; else (None, why)."""
    try:
        svc = adapter.service()
        server = live.live_server(cfg.home, svc['mainPid']) if svc['active'] else None
        active, pending = live.service_state(cfg.home)
    except (live.NotLive, osadapter.AdapterError) as e:
        return None, str(e)
    if server is None or pending or active != server['version'] or live.markers(cfg.home):
        return None, 'T3 is not running steadily (stopped, restarting or updating), so no report reads its home'
    if not _FORK.search(server['version']):
        return None, f't3 {server["version"]} is not a fork build: it has no report'
    try:
        report = live.fork_report(T3_STUB or server['binary'], cfg.home)
    except live.NotLive as e:
        return None, str(e)
    changed = check_mod.server_changed(cfg, adapter, server)
    return (None, changed) if changed else (report, None)


def _facts(run, key):
    """The newest value of `key` in a journalled run's step facts, or None."""
    found = [s['data'][key] for s in run['steps'] if isinstance(s.get('data'), dict) and key in s['data']]
    return found[-1] if found else None


def _counts(report):
    db = report.get('database') if isinstance(report, dict) else None
    return db.get('counts') if isinstance(db, dict) and isinstance(db.get('counts'), dict) else {}


def _since_copy(cfg, adapter, copy_dir, at):
    runs = journal.read(cfg.state_dir)[0]
    before = next((_facts(r, 'reportCounts') for r in reversed(runs) if any(
        s['step'] == 'cold-copy' and (s['data'] or {}).get('dest') == copy_dir for s in r['steps'])), None)
    report, why = live_report(cfg, adapter)
    lost = f'every thread, message and setting since {at} is lost (kept aside, not deleted)'
    if not isinstance(before, dict) or report is None:
        return f'counts since the copy: cannot tell ({why or "the copy journalled no counts"}); {lost}'
    now = _counts(report)
    said = [f'{k}: {before.get(k)} at the copy, {now.get(k)} now' for k in ('projects', 'threads')]
    return f'{"; ".join(said)} (threads that only got new messages are not counted); {lost}'


def _stamp(text):
    return re.sub(r'[-:]', '', str(text).strip())


def restore_and_downgrade(run, cfg, adapter, copy_dir, *, automatic, lose_work_since=None, say=print, hooks=True):
    """W1 in `run` (a back run, or the switch's own automatic way back). By hand (automatic=False) it is refused,
    before any change, unless lose_work_since is the copy's time, and it first says how the counts moved since the
    copy. Then: put the old runtime back from the kit's kept copy if upstream pruned it, the pre hooks, disable and
    stop the service, restore the copy, `<old t3> update <old> --allow-downgrade --yes`, verify, the post hooks
    (hooks=False: the caller's run runs them itself). Returns {'version', 'pid'}."""
    try:
        m = coldcopy.read_manifest(copy_dir, cfg.home)
    except (coldcopy.Incomplete, coldcopy.WrongCopy) as e:
        raise journal.Refused(f'{e}; nothing was changed') from None
    old, at = m.get('liveVersion'), m.get('createdAt')
    t3, kept, why = old_t3(cfg, old)
    if t3 is None:
        raise journal.Refused(f'{why}; nothing was changed')
    if not automatic:
        if lose_work_since is None:
            raise journal.Refused(f'W1 loses everything since the copy was taken ({at}); to go on, give '
                                  f'--lose-work-since {at}')
        if _stamp(lose_work_since) != _stamp(at):
            raise journal.Refused(f'--lose-work-since {lose_work_since} does not match the copy\'s time {at}; '
                                  'nothing was changed')
        say(_since_copy(cfg, adapter, copy_dir, at))
    try:
        coldcopy.plan_restore(copy_dir, cfg.home, from_home=cfg.home, version=old, floor_bytes=cfg.free_floor_mb * MB)
    except (coldcopy.Incomplete, coldcopy.WrongCopy, RuntimeError, OSError, ValueError) as e:
        raise journal.Refused(f'the copy cannot be restored: {e}; nothing was changed') from None
    with run.step('plan', way='w1', version=old, t3=t3, copy=copy_dir, at=at, automatic=automatic, kept=kept):
        pass
    with run.step('runtime', version=old) as facts:
        facts.update(keptruntime.put_back(run, cfg, old))

    def stop_restore_update():
        server = _server(cfg, adapter)
        say('stopping T3 for W1: until this back ends ok, T3 stays stopped and is not started at boot (on purpose: '
            'the home may be half-restored); if it stops or fails, run the same back again')
        with run.step('stop-service', pid=server and server['pid']):
            adapter.disable()  # first: from here T3 must not start at boot until the update enables it again
            adapter.stop()
            coldcopy._wait_stopped(adapter, server['pid'] if server else 0)
        with run.step('restore', copy=copy_dir) as facts:
            facts.update(coldcopy.restore(run, copy_dir, cfg.home, from_home=cfg.home, version=old,
                                          floor_bytes=cfg.free_floor_mb * MB))
        return _update(run, cfg, adapter, t3, old, say)
    return _hooked(run, cfg, hooks, stop_restore_update)


def _hooked(run, cfg, hooks, body):
    """body (it stops or restarts T3) between the machine's pre hooks and post hooks, as the copy run runs them
    (coldcopy.run_copy): a failed pre hook runs the post hooks and refuses before T3 is touched; the post hooks run
    after body however it ends, a failure included. A kill runs no post hook: the next back runs both again (each
    hook must be safe twice, config.py)."""
    if hooks:
        failed = coldcopy._hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed is not None:
            again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if again:  # jobs may stay paused: not a clean refusal
                raise RuntimeError(f'pre hook failed: {failed}; T3 was not stopped; then a post hook failed: {again}')
            raise journal.Refused(f'pre hook failed: {failed}; T3 was not stopped')
    try:
        result = body()
    except BaseException as e:
        if hooks:
            try:
                again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
                if again:
                    e.add_note(f'then a post hook failed: {again}')
            except BaseException as later:
                e.add_note(f'then: {type(later).__name__}: {later}')
        raise
    if hooks:
        again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
        if again:
            raise RuntimeError(f't3 {result["version"]} runs, verified, but a post hook failed: {again}')
    return result


def _update(run, cfg, adapter, t3, version, say):
    """`<t3> update <version> --allow-downgrade --yes` (it installs the version if needed, points the service at it,
    enables and starts it), then verify: that version runs, the service state names it, the service is enabled."""
    before = _server(cfg, adapter)
    old_pid = before['pid'] if before and before['version'] != version else 0
    with run.step('update', t3=t3, version=version) as facts:
        argv = [T3_STUB or t3, 'update', version, '--allow-downgrade', '--yes', '--base-dir', cfg.home]
        try:
            p = subprocess.run(argv, capture_output=True, text=True, errors='replace', timeout=UPDATE_TIMEOUT,
                               stdin=subprocess.DEVNULL, cwd=cfg.home)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise RuntimeError(f'{t3} update failed: {e}') from None
        facts['exit'] = p.returncode
        if p.returncode != 0:
            raise RuntimeError(f'{t3} update {version} exited {p.returncode}: {(p.stderr or p.stdout).strip()[-300:]}')
    with run.step('verify') as facts:
        again = coldcopy._wait_started(cfg, adapter, version, old_pid)
        if live.service_state(cfg.home)[0] != version or not adapter.service()['enabled']:
            raise RuntimeError(f't3 {version} runs, but the service state does not name it or {osadapter.UNIT} is '
                               'not enabled')
        facts.update(version=version, pid=again['pid'])
    say(f't3 {version} runs again (pid {again["pid"]}), verified')
    return {'version': version, 'pid': again['pid']}


def _steady_on(cfg, adapter, version):
    server = _server(cfg, adapter)
    try:
        active, pending = live.service_state(cfg.home)
    except live.NotLive:
        return False
    return bool(server) and server['version'] == active == version and not pending and not live.markers(cfg.home)


def run_back(cfg, adapter, way, *, lose_work_since=None, automatic=False, say=print):
    """Take the way back `way` ('w1', 'w2', 'w3') in this process. Returns {'version', 'nothing'?}; raises
    journal.Refused (nothing changed), Busy (another run holds the lock: a guard timer firing during a manual back
    included), Unfinished, or the error that ended the run (running back again finishes it)."""
    with journal.run(cfg.state_dir, 'back', may_follow=FOLLOWS) as run:
        runs = journal.read(cfg.state_dir)[0][:-1]
        following = bool(runs) and journal.needs_settling(runs[-1])  # a switch or back left half-way
        verified = [r for r in runs if _facts(r, 'version') and any(
            s['step'] == 'verify' and s['phase'] == 'done' for s in r['steps'])]
        last = verified[-1] if verified else None
        if (not following and last and last['command'] == 'back' and _facts(last, 'way') == way
                and _steady_on(cfg, adapter, _facts(last, 'version'))):
            say(f'already back: t3 {_facts(last, "version")} runs, verified by the {way} back of {last["began"]}; '
                'nothing to do')
            return {'version': _facts(last, 'version'), 'nothing': True}
        plan = None
        for r in reversed(runs):  # the back (or its followers) that stopped half-way: finish its plan
            if not (journal.needs_settling(r) and r['command'] in FOLLOWS):
                break
            plan = plan or (_facts(r, 'way') and {k: _facts(r, k) for k in ('way', 'version', 't3', 'copy')})
        if plan and plan['way'] != way:
            raise journal.Refused(f'the last back ({plan["way"]}) stopped half-way: run `t3-golive back --way '
                                  f'{plan["way"]}` again to finish it')
        if plan and way != 'w1':  # its t3 may be gone since (unstaged, pruned): look again
            t3, where = _upstream_t3(cfg, plan['version'])
            if t3 is None:
                raise journal.Refused(f'the stopped back\'s t3 {plan["version"]} is not available any more: {where}; '
                                      'stage it again, then run this again')
            plan['t3'] = t3
        if plan is None:
            w = ways(cfg, adapter)[way]
            if not w['ok']:
                raise journal.Refused(f'{way} is not possible now: {w["why"]}')
            plan = {'way': way, **{k: w.get(k) for k in ('version', 't3', 'copy')}}
        if way == 'w1':
            return restore_and_downgrade(run, cfg, adapter, plan['copy'], automatic=automatic,
                                         lose_work_since=lose_work_since, say=say)
        with run.step('plan', **plan):
            pass
        return _hooked(run, cfg, True, lambda: _update(run, cfg, adapter, plan['t3'], plan['version'], say))


# --- status: read only ---

def _d(value):
    return value if isinstance(value, dict) else {}


def _http(cfg, server):
    """Asks this machine only (127.0.0.1 and the port the server recorded), whatever host its origin names."""
    url = f'http://127.0.0.1:{int(server["port"])}/'
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never through a proxy
    try:
        with opener.open(url, timeout=HTTP_TIMEOUT) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code  # an answer all the same (a login page, say)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return False, f'{url} does not answer: {e}'
    return True, f'{url} answers (HTTP {code})'


def _links(cfg, expected, add, notes):
    """The launchers upstream recorded (runtime/launchers): each link must lead to the expected build."""
    try:
        with open(os.path.join(cfg.home, 'runtime', 'launchers'), encoding='utf-8') as f:
            links = [line.strip() for line in f if line.strip()]
    except OSError:
        links = []
    versions = os.path.realpath(os.path.join(cfg.home, 'runtime', 'versions'))
    for link in links:
        m = re.fullmatch(re.escape(versions) + r'/([^/]+)/t3', os.path.realpath(link))
        if os.path.islink(link) and m:
            add('t3 link', m.group(1) == expected, f'{link} -> t3 {m.group(1)}')
        else:
            notes.append(f't3 launcher {link}: not a link to a runtime of this home')
    if not links:
        notes.append('no t3 launcher link recorded in runtime/launchers')


def _report(cfg, adapter, add, notes, runs):
    report, why = live_report(cfg, adapter)
    if report is None:
        return notes.append(why) if 'not a fork build' in why else add('report', False, why)
    db, fork = _d(report.get('database')), _d(report.get('fork'))
    add('report', report.get('ok') is True, 'the live build reads its home cleanly' if report.get('ok') is True
        else '; '.join(str(_d(p).get('message')) for p in report.get('problems') or []) or 'not ok')
    pending, unknown = db.get('pendingMigrations'), db.get('unknownMigrations')
    add('migrations', pending == [] and unknown == [], f'{len(db.get("appliedMigrations") or [])} applied; pending: '
        f'{pending or "none"}; unknown: {unknown or "none"}')
    before = next((_facts(r, 'reportCounts') for r in reversed(runs) if isinstance(_facts(r, 'reportCounts'), dict)),
                  None)
    now = _counts(report)
    if before is None:
        notes.append('no counts from before the switch in the journal to compare with')
    else:
        low = [f'{k}: {now.get(k)} now, {before.get(k)} before' for k in ('projects', 'threads')
               if not (type(now.get(k)) is int and type(before.get(k)) is int and now[k] >= before[k])]
        add('counts', not low, '; '.join(low) or f'projects {now["projects"]}, threads {now["threads"]}: not below '
                                                 'the counts before the switch')
    on = fork.get('featuresOn')
    notes.append(f'fork features on: {", ".join(on)}' if on else 'fork features: all off')
    notes.append(f'fork switches: {json.dumps(_d(fork.get("features")), sort_keys=True)}')
    insts = [_d(i) for i in _d(report.get('settings')).get('providerInstances') or []]
    notes.append('provider instances: ' + (', '.join(f'{i.get("id")} ({i.get("driver")}, '
                                                       f'{"on" if i.get("enabled") else "off"})' for i in insts)
                                           or 'none'))


def _status(cfg, adapter, add, notes, seen):
    """The checks; returns 'up', 'restarting', 'down' or 'unknown' (the live server goes in seen['server'])."""
    try:
        svc = adapter.service()
    except osadapter.AdapterError as e:
        add('service', False, f'the service manager did not answer: {e}')
        return 'unknown'
    add('service', svc['loaded'] and svc['active'] and svc['enabled'] and check_mod._same_dir(svc['home'], cfg.home),
        f'{osadapter.UNIT} {svc.get("state")}, enabled: {svc["enabled"]}, serves {svc["home"]}')
    try:
        runs = journal.read(cfg.state_dir)[0]
    except journal.Damaged as e:  # a journal from a newer kit included
        runs = []
        add('journal', False, str(e))
    else:
        open_run = runs[-1] if runs and journal.needs_settling(runs[-1]) else None
        if open_run is None:
            add('journal', True, 'no golive run was left half-way')
        elif open_run['command'] in FOLLOWS:
            add('journal', False, f'{journal.describe(open_run)}; run the same `t3-golive back` again: it finishes '
                'where that run stopped (a W1 stopped after its stop leaves T3 stopped and not started at boot until '
                'then)')
        else:
            add('journal', False, f'{journal.describe(open_run)}; {journal.SETTLE_HINT}')
    expected = next((_facts(r, 'version') for r in reversed(runs) if any(
        s['step'] == 'verify' and s['phase'] == 'done' for s in r['steps'])), None)
    try:
        active, pending = live.service_state(cfg.home)
        expected = expected or active
        add('service state', active == expected and not pending, f'names t3 {active}'
            + (f', expected t3 {expected} (the last build a golive run verified)' if active != expected else '')
            + (f'; an update to {_d(pending).get("targetVersion")} is pending' if pending else ''))
    except live.NotLive as e:
        add('service state', False, str(e))
    marks = live.markers(cfg.home)
    add('no markers', not marks, ', '.join(marks) or 'no restart, stop or restore marker')
    if marks or svc.get('state') in ('activating', 'deactivating', 'reloading'):
        return 'restarting'
    if not svc['active']:
        return 'down'
    try:
        server = live.live_server(cfg.home, svc['mainPid'])
    except live.NotLive as e:
        add('live server', False, str(e))
        return 'restarting'
    seen['server'] = server
    add('live server', server['version'] == expected, f't3 {server["version"]}, pid {server["pid"]}, port '
        f'{server["port"]}' + (f'; expected t3 {expected}' if server['version'] != expected else ''))
    add('http', *_http(cfg, server))
    _links(cfg, expected, add, notes)
    _report(cfg, adapter, add, notes, runs)
    return 'up'


def status(cfg, adapter):
    """Read only (it holds the kit's lock shared, like check): (exit code, lines). Exit 0 only when T3 is up and
    every check passed; a golive run holding the lock, a marker or a service starting or stopping read as
    restarting; anything that goes wrong in here is a failed check, never a crash or an OK."""
    checks, notes, verdict, seen = [], [], 'unknown', {}

    def add(name, ok, detail):
        checks.append((bool(ok), name, detail))

    try:
        with journal.lock(cfg.state_dir, 'status', exclusive=False, create=False):
            verdict = _status(cfg, adapter, add, notes, seen)
    except journal.Busy as e:
        add('no golive run', False, f'{e}: T3 may be stopping or restarting')
        verdict = 'restarting'
    except Exception as e:
        add('status ran', False, f'{type(e).__name__}: {e}')
    try:
        for folder, m in copies(cfg):
            notes.append(f'copy {folder}: taken {m.get("createdAt")} under t3 {m.get("liveVersion")}, '
                         f'{(m.get("bytes") or 0) / MB:.1f} MB')
        notes += [f'incomplete copy (never used) {f}: {b / MB:.1f} MB' for f, b in
                  coldcopy.incomplete_copies(cfg.backup_dir)]
        notes += [f'kept by a restore {f}: {b / MB:.1f} MB' for f, b in coldcopy.kept_files(cfg.home)]
        notes += [f'kept runtime of t3 {v} (for W1) {f}: {b / MB:.1f} MB' + (f'; NOT usable: {why}' if why else '')
                  for v, f, b, why in keptruntime.listing(cfg)]
    except Exception as e:  # a hand-edited manifest included: a note, never a crash
        notes.append(f'the copies cannot be listed: {type(e).__name__}: {e}')
    ok = verdict == 'up' and all(c[0] for c in checks)
    failed = ', '.join(name for good, name, _ in checks if not good)
    server = seen.get('server') or {}
    on = f': t3 {server.get("version")} (pid {server.get("pid")}, port {server.get("port")})'
    head = {'up': 'T3 is up' + (on if ok else f' but not right ({failed}){on}'),
            'restarting': f'T3 is restarting or starting ({failed})', 'down': f'T3 is DOWN ({failed or "stopped"})',
            'unknown': f'T3 state unknown ({failed})'}[verdict]
    lines = [head] + [f'{"ok  " if good else "FAIL"}  {name}: {detail}' for good, name, detail in checks]
    return (0 if ok else 1), lines + [f'note  {n}' for n in notes] + ['note  the kit deletes no copy or kept file']


def main(args, cfg, adapter, out=sys.stdout, err=sys.stderr):
    if args.command == 'status':
        code, lines = status(cfg, adapter)
        print('\n'.join(lines), file=out)
        return code
    if args.way is None:
        for way, w in ways(cfg, adapter).items():
            print(f'{way.upper()} {"possible" if w["ok"] else "not possible"}: {w["why"]}', file=out)
        print('Take one with: t3-golive back --way w1|w2|w3 (W1 also needs --lose-work-since <the copy\'s time>)',
              file=out)
        return 0
    if not args.here:
        unit = f't3-golive-back-{coldcopy._stamp().lower()}'
        argv = [coldcopy.KIT_BIN, 'back', '--way', args.way, '--here'] + (
            ['--lose-work-since', args.lose_work_since] if args.lose_work_since else [])
        try:
            adapter.run_detached(unit, argv)
        except osadapter.AdapterError as e:
            print(f't3-golive: {e}', file=err)
            return 1
        print(f'started detached as {unit}. Follow it with: journalctl --user -u {unit} -f; then t3-golive status'
              + ('. If it is stopped or fails after it stopped T3, T3 stays stopped and is not started at boot until '
                 'the same back is run again and ends ok' if args.way == 'w1' else ''), file=out)
        return 0
    if adapter.inside_service():
        print(f't3-golive: this runs inside {osadapter.UNIT}; stopping it would kill this back half-way. Run '
              '`t3-golive back` without --here (it runs detached).', file=err)
        return 1
    try:
        result = run_back(cfg, adapter, args.way, lose_work_since=args.lose_work_since,
                          say=lambda line: print(line, file=out, flush=True))
    except journal.Refused as e:
        print(f'Refused: {e}', file=err)
        return 1
    except (journal.Busy, journal.Unfinished, journal.Damaged) as e:
        print(f't3-golive: {e}', file=err)
        return 1
    except Exception as e:
        notes = ''.join(f'\n{n}' for n in getattr(e, '__notes__', ()))
        down = ('\nIf it got past the stop, T3 stays stopped and is not started at boot (on purpose: the home may be '
                'half-restored) until a `t3-golive back --way w1` ends ok.' if args.way == 'w1' else '')
        print(f't3-golive: back failed: {type(e).__name__}: {e}{notes}{down}\nRun the same `t3-golive back` again: '
              'it finishes where this one stopped. `t3-golive status` says where T3 is.', file=err)
        return 1
    print('nothing to do' if result.get('nothing') else f'back on t3 {result["version"]}, verified', file=out)
    return 0
