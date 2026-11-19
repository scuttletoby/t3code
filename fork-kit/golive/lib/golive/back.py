"""`t3-golive back`: the way back from a fork build (W1, W2, W3); `t3-golive status`: is T3 up and right?

W1 restore and downgrade (loses everything since the copy): the t3 the copy was taken under is put back into the
home's runtime/versions from the kit's kept copy when upstream pruned it (keptruntime: every `t3 update` prunes old
runtimes, so the switch keeps one first), the pre hooks run, the boot service is disabled (no start at boot on a
half-restored home) and stopped, the newest finished copy of this home is restored (coldcopy.restore: the migrated
database and settings.json are kept aside, never deleted; the copy stays where it is), then that t3 runs
`update <old> --allow-downgrade --yes` (it enables and starts the service), that build is verified, and the post
hooks run. A W1 that is stopped or fails after the stop leaves T3 stopped and not started at boot, and runs no post
hook (background jobs stay paused), on purpose, until a back ends ok. By hand it needs `--lose-work-since <the
copy's time>`; the switch calls restore_and_downgrade(automatic=True) inside its own run.
W2 (loses nothing): the fork adds no migrations, so the upstream nightly a fork build is cut from (its version
without `.fork.N`) runs the migrated database as it is. Read from the code, untried on a live home.
W3: an upstream stable newer than that base, once one is installed or staged. W2 and W3 run that build's
`update <version> --allow-downgrade --yes` on the data as it is, between the pre and the post hooks.

A back may follow a back or switch that stopped half-way (journal.run may_follow): it finishes that back's plan
(journalled in its "plan" step); the restore, the put-back and the update are safe to run again. A W1 that got past
its update (or its verify) is not redone: while the service state names its build, the rerun only verifies (or
finds it verified) and runs the post hooks, so what T3 served since is never restored over. When that verify
finished and failed twice, the newest verify among them (the old build does not come up; a verify that was cut off
by a kill, a stop or the machine going down is not a failed one), W2 or W3 may follow that W1: neither restores, so
no work is restored over; it goes to the base of the fork build the journal's switch names, never to a build older
than the one whose data that W1 put back, runs no pre hook again, and runs the post hooks only when it ends ok. Only
its own rerun follows it: W1, and the other of W2 and W3, are not run behind it. A back after a good back of the
same way, with nothing unfinished since, finds the build that back verified running and does nothing. `t3-golive
back` lists the ways by the same rule its `--way` goes by (possible, _follow).
"""
import functools, http.client, json, os, re, sys, urllib.error, urllib.request

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
    """Read only: [(folder, manifest)] of the finished copies of this home in backupDir, newest first: those of
    `t3-golive copy` (t3-copy-*) and of the switch (t3-switch-*, taken while T3 was stopped for it).
    createdAt decides first. It is a whole second: copies of the same second go by the place of their runs in the
    journal (the later run is the newer copy), then by folder name, never by the order the folder happens to list
    them in (W1 restores the first one). A run the journal does not hold (no run id, an id that is not text, a
    journal moved aside, damaged, from a newer kit or unreadable) counts as before every run it holds; then the
    folder name decides. With no usable journal that order is fixed but not always the newer copy (two copies whose
    folders were named in one second); in real use it cannot happen, as a copy takes far more than a second."""
    try:
        names = [n for n in os.listdir(cfg.backup_dir) if n.startswith(('t3-copy-', 't3-switch-'))]
    except FileNotFoundError:
        return []
    found = []
    for n in names:
        try:
            found.append((os.path.join(cfg.backup_dir, n), coldcopy.read_manifest(os.path.join(cfg.backup_dir, n),
                                                                                cfg.home)))
        except (coldcopy.Incomplete, coldcopy.WrongCopy):
            pass
    try:
        runs = journal.read(cfg.state_dir)[0]
    except (journal.Damaged, OSError, ValueError, TypeError, RecursionError):  # unreadable counts as damaged
        runs = []
    place = {r['id']: n for n, r in enumerate(runs) if isinstance(r['id'], str)}

    def key(f):
        rid = f[1].get('runId')
        return str(f[1].get('createdAt')), place.get(rid, -1) if isinstance(rid, str) else -1, f[0]
    return sorted(found, key=key, reverse=True)


def ways(cfg, adapter, stuck=None):
    """Read only: {'w1'|'w2'|'w3': {'ok', 'why', 'version', 't3', ...}}: which way back is possible now, and why,
    for a back that finishes nothing left half-way (possible() is what `t3-golive back` lists).
    stuck (_stuck_w1): a W1 left half-way past its update, stuck on its verify; the service state then names
    its old build, so W2 and W3 start from the fork build the journal's switch names (stuck['left']). That is the
    newest fork build a switch of this kit went to, not always the build that W1 went back from (a build put in by
    hand, a copy taken under a later build): W2 and W3 never go to a build older than the one whose data that W1
    put back (its `--allow-downgrade` would run under data a newer build wrote), nor to that build itself."""
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
    base, follows, floor = base_of(active), '', None
    if base is None and stuck:
        base, follows = base_of(stuck['left']), '; it follows the W1 stuck on its verify (nothing is restored again)'
        floor = base_of(stuck['version']) or stuck['version']  # a fork build's data is its base nightly's
    if base is None:
        why = f'the service runs t3 {active}, not a fork build (.fork.N): W2 and W3 go from a fork build to upstream'
        if stuck:
            why += (f' (a W1 to t3 {stuck["version"]} is stuck on its verify, but no switch in the journal names the '
                    'fork build it left, so their build cannot be told)')
        out['w2'], out['w3'] = {'ok': False, 'why': why}, {'ok': False, 'why': why}
        return out
    t3, where = _upstream_t3(cfg, base)
    out['w2'] = {'ok': bool(t3), 'version': base, 't3': t3, 'why': (
        f'the upstream nightly t3 {base} the fork build is cut from, on the data as it is ({where}); untried live'
        + follows if t3 else f't3 {base} is not available: {where}')}
    if stuck and base == stuck['version']:  # an update to the build the service state names may start nothing
        out['w2'] = {'ok': False, 'why': f't3 {base} is the build the stuck W1 could not verify: W2 would run that '
                                         'same build again; take W3, or put the home right and settle'}
    elif floor and _older(base, floor):
        out['w2'] = {'ok': False, 'why': f't3 {base} is older than t3 {stuck["version"]}, the build whose data the '
                                         'stuck W1 put back: W2 would run a downgrade under data a newer build '
                                         'wrote; take W3 if it is possible, or put the home right and settle'}
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
        if stuck and v == stuck['version']:
            t3, where = None, 'the build the stuck W1 could not verify (W3 would run that same build again)'
        elif floor and _older(v, floor):
            t3, where = None, f'older than t3 {stuck["version"]}, the build whose data the stuck W1 put back'
        if t3:
            out['w3'] = {'ok': True, 'version': v, 't3': t3, 'why': f'the upstream stable t3 {v} ({where})' + follows}
            break
        out['w3']['why'] += f'; t3 {v}: {where}'
    return out


def _older(version, than):
    """`version` is older than `than`; a version that cannot be compared counts as older (the refusing side)."""
    try:
        return live.compare_versions(version, than) < 0
    except ValueError:
        return True


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

    stopped, verified = [], []

    def stop_restore_update():
        server = _server(cfg, adapter)
        say('stopping T3 for W1: until this back ends ok, T3 stays stopped and is not started at boot, and background '
            'jobs stay paused (on purpose: the home may be half-restored); if it stops or fails, run the same back '
            'again')
        stopped.append(True)
        with run.step('stop-service', pid=server and server['pid']):
            adapter.disable()  # first: from here T3 must not start at boot until the update enables it again
            adapter.stop()
            coldcopy._wait_stopped(adapter, server['pid'] if server else 0)
        with run.step('restore', copy=copy_dir) as facts:
            facts.update(coldcopy.restore(run, copy_dir, cfg.home, from_home=cfg.home, version=old,
                                          floor_bytes=cfg.free_floor_mb * MB))
        verified.append(_update(run, cfg, adapter, t3, old, say))
        return verified[0]
    try:
        return _hooked(run, cfg, hooks, stop_restore_update, post_on_failure=lambda: not stopped, rerun=W1_RERUN)
    except Exception as e:
        if verified:  # only a post hook failed: T3 runs (said in the error)
            pass
        elif stopped and run._broken == 'verify':
            e.add_note(f'its update to t3 {old} ran, but T3 is not verified (`t3-golive status` says whether it '
                       'runs); the same `t3-golive back --way w1` verifies it again, with no second restore; '
                       + _follow_note(cfg, old))
        elif stopped:
            e.add_note('T3 was stopped for this W1: T3 stays stopped and is not started at boot (on purpose: the '
                       'home may be half-restored) until the same `t3-golive back --way w1` ends ok')
        raise


W1_RERUN = '; run the same `t3-golive back --way w1` again: it runs only the post hooks (no second stop or restore)'
MAY_FOLLOW = '`t3-golive back --way w2` or `--way w3` may follow it (neither restores: no work is restored over)'
# W2 or W3 may follow a W1 past its update only after this many of its verifies finished and failed: its first, and
# that of the rerun (docs/fork/go-live.md: "fails, and fails again on the rerun"). The rerun only verifies, so it is
# the safer first try: a build that starts late, or a service manager that did not answer once, must not send the
# home on to another build.
FAILS_TO_FOLLOW = 2
CUT_OFF = 'was cut off (killed, stopped, or the machine went down before it finished), which does not count as failed'


def _not_yet(fails):
    """When W2 or W3 may follow a W1 past its update that is not stuck yet, after `fails` failed verifies."""
    return (f'if its verify then fails{" again" if fails else ""}'
            f'{", and again on the rerun after that" if FAILS_TO_FOLLOW - fails > 1 else ""}, {MAY_FOLLOW}')


def _follow_note(cfg, version):
    """Said in the error of a W1 to `version` whose verify just failed: whether W2 or W3 may follow it now, read
    from the journal like the next back reads it (this run's own steps included)."""
    try:
        runs = journal.read(cfg.state_dir)[0]
        fails, stuck = _w1_scan(_chain(runs), version)[2], _stuck_w1(runs)
    except Exception:
        return '`t3-golive status` says what may follow it'
    if stuck and stuck['version'] == version:
        return f'its verify finished and failed {fails} times: {MAY_FOLLOW}'
    return _not_yet(fails)


def _hooked(run, cfg, hooks, body, *, pre=True, post_on_failure=lambda: True, rerun=''):
    """body (it stops or restarts T3) between the machine's pre hooks and post hooks, as the copy run runs them
    (coldcopy.run_copy): a failed pre hook runs the post hooks and refuses before T3 is touched; the post hooks run
    after body, and after a failure only while post_on_failure() (W1: T3 was never stopped; once W1 stopped T3, the
    background jobs stay paused until a back ends ok, as after a kill). A kill runs no post hook: the next back runs
    them (each hook must be safe twice, config.py). pre=False: a rerun that only finishes a stopped W1, or a W2 or
    W3 behind a W1 that stopped T3 (that W1 ran the pre hooks)."""
    if hooks and pre:
        failed = coldcopy._hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed is not None:
            again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if again:  # jobs may stay paused: not a clean refusal
                raise RuntimeError(f'pre hook failed: {failed}; T3 was not stopped; then a post hook failed: {again}')
            raise journal.Refused(f'pre hook failed: {failed}; T3 was not stopped')
    try:
        result = body()
    except BaseException as e:
        if hooks and not post_on_failure():
            e.add_note('no post hook ran: background jobs stay paused until the same back ends ok')
        elif hooks:
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
            raise RuntimeError(f't3 {result["version"]} runs, verified, and starts at boot, but a post hook failed: '
                               f'{again}{rerun}')
    return result


def _update(run, cfg, adapter, t3, version, say):
    """`<t3> update <version> --allow-downgrade --yes` (it installs the version if needed, points the service at it,
    enables and starts it), then verify: that version runs, the service state names it, the service is enabled."""
    before = _server(cfg, adapter)
    old_pid = before['pid'] if before and before['version'] != version else 0
    with run.step('update', t3=t3, version=version) as facts:
        argv = [T3_STUB or t3, 'update', version, '--allow-downgrade', '--yes', '--base-dir', cfg.home]
        # the fixed environment (no release server can be pointed elsewhere), and the child dies with this run
        code, out, err = live.run_t3(argv, UPDATE_TIMEOUT, cwd=cfg.home)
        if code is None:
            raise RuntimeError(f'{t3} update failed: {err.strip()[-300:]}')
        facts['exit'] = code
        if code != 0:
            raise RuntimeError(f'{t3} update {version} exited {code}: {(err or out).strip()[-300:]}')
    return _verify(run, cfg, adapter, version, old_pid, say)


def _verify(run, cfg, adapter, version, old_pid, say):
    """That version runs (a server other than old_pid), the service state names it, the service is enabled.
    A verify that finished and failed is journalled as such (a "verify-failed" step): one that was cut off (a kill,
    a stop, the machine going down) leaves no such mark, and only finished failed verifies let W2 or W3 follow a W1
    (_w1_scan). A kill between the failure and its mark reads as cut off: the safe side."""
    try:
        with run.step('verify') as facts:
            again = coldcopy._wait_started(cfg, adapter, version, old_pid)
            if live.service_state(cfg.home)[0] != version or not adapter.service()['enabled']:
                raise RuntimeError(f't3 {version} runs, but the service state does not name it or {osadapter.UNIT} '
                                   'is not enabled')
            facts.update(version=version, pid=again['pid'])
    except Exception:  # not a stop or an interrupt (BaseException): those did not finish the verify
        with run.step('verify-failed', version=version):
            pass
        raise
    say(f't3 {version} runs again (pid {again["pid"]}), verified')
    return {'version': version, 'pid': again['pid']}


def _steady_on(cfg, adapter, version):
    server = _server(cfg, adapter)
    try:
        active, pending = live.service_state(cfg.home)
    except live.NotLive:
        return False
    return bool(server) and server['version'] == active == version and not pending and not live.markers(cfg.home)


def _names(cfg, version):
    """The service state names `version`, with no update pending."""
    try:
        active, pending = live.service_state(cfg.home)
    except live.NotLive:
        return False
    return active == version and not pending


def _chain(runs):
    """The back and switch runs left half-way at the end of `runs`, oldest first (each follows the one before)."""
    chain = []
    for r in reversed(runs):
        if not (journal.needs_settling(r) and r['command'] in FOLLOWS):
            break
        chain.insert(0, r)
    return chain


def _w1_scan(chain, version):
    """(got, verify, fails) of the stopped W1 to `version` in `chain` since its plan. got: None (it never stopped
    T3), 'stopped', 'updated' (its `t3 update` exited 0) or 'verified'; a later stop (a whole rerun) starts it over.
    verify: how its newest verify since (its own or a rerun's) ended when it did not pass: 'failed' (it finished
    and failed: the "verify-failed" mark follows it) or 'cut' (it began and nothing more is journalled: killed,
    stopped, the machine went down; a journal from a kit before the mark reads like this too, the safe side); None
    when it passed or none began. fails: the verifies that finished and failed since its update, or since its last
    verify that passed."""
    steps = [s for r in chain for s in r['steps']]
    plans = [i for i, s in enumerate(steps) if s['step'] == 'plan' and s['phase'] == 'started'
             and _d(s['data']).get('way') == 'w1' and _d(s['data']).get('version') == version]
    got, to_version, verify, fails = None, False, None, 0
    for s in steps[plans[-1]:] if plans else ():
        step, done, data = s['step'], s['phase'] == 'done', _d(s['data'])
        if step == 'stop-service':
            got, verify, fails = 'stopped', None, 0
        elif step == 'update' and not done:
            to_version = data.get('version') == version
        elif step == 'update' and got == 'stopped' and to_version:
            got = 'updated'
        elif step == 'verify' and got in ('updated', 'verified'):
            if not done:
                verify = 'cut'
            elif data.get('version') == version:
                got, verify, fails = 'verified', None, 0
        elif step == 'verify-failed' and not done and verify == 'cut' and data.get('version') == version:
            verify, fails = 'failed', fails + 1  # the mark of the verify that began before it; it counts once
    return got, verify, fails


def _w1_got(chain, version):
    """How far the stopped W1 to `version` in `chain` got: see _w1_scan."""
    return _w1_scan(chain, version)[0]


def _plan_of(chain):
    """{'way', 'version', 't3', 'copy'} of the newest way back begun in `chain`, or None."""
    for r in reversed(chain):
        if _facts(r, 'way'):
            return {k: _facts(r, k) for k in ('way', 'version', 't3', 'copy')}
    return None


def _stuck_w1(runs):
    """{'version', 'left'} when what `runs` ends with, left half-way, is a W1 (a back's, or a switch's automatic
    one) that got past its update and is stuck on its verify, else None: FAILS_TO_FOLLOW verifies finished and
    failed (the W1's own and its reruns'), the newest verify among them. A verify that was cut off is not a failed
    one: the build may be up, or coming up. Only then may W2 or W3 follow it: from its update on, nothing but a
    verify is left of that W1, and neither restores. left: the newest fork build a switch in the journal names (the
    build that W1 went back from), or None."""
    chain = _chain(runs)
    plan = _plan_of(chain)
    if not plan or plan['way'] != 'w1':
        return None
    got, verify, fails = _w1_scan(chain, plan['version'])
    if verify != 'failed' or fails < FAILS_TO_FOLLOW:
        return None
    named = [_d(s['data']).get('version') for r in runs if r['command'] == 'switch' for s in r['steps']]
    return {'version': plan['version'], 'left': next((v for v in reversed(named) if base_of(v)), None)}


def _follow(runs, way):
    """(plan, stuck, why not) of a back `way` behind what `runs` ends with: the one rule that `back --way` (run_back)
    and the list of ways (possible) both go by. plan: the way back left half-way that this back finishes (its own
    way: only its rerun follows it), or None: it makes a plan of its own, from ways(stuck). stuck: the W1 stuck on
    its verify that this W2 or W3 follows (_stuck_w1). why not: refused, with nothing changed: another way was left
    half-way and is not a W1 stuck on its verify (a W1 before its update may have half-restored the home: only W1
    finishes that)."""
    chain = _chain(runs)
    plan = _plan_of(chain)
    if not plan or plan['way'] == way:
        return plan, None, None
    stuck = _stuck_w1(runs)
    if stuck:
        return None, stuck, None
    why = f'the last back ({plan["way"]}) stopped half-way: run `t3-golive back --way {plan["way"]}` again to finish it'
    if plan['way'] == 'w1':
        got, verify, fails = _w1_scan(chain, plan['version'])
        if got in ('updated', 'verified'):
            why += f'; {f"its last verify {CUT_OFF}: " if verify == "cut" else ""}{_not_yet(fails)}'
    return plan, None, why


def _gone(version, where):
    return f'the stopped back\'s t3 {version} is not available any more: {where}; stage it again, then run this again'


def _rerun(cfg, chain, plan):
    """Read only: {'ok', 'why', ...} of the same way again behind its own back left half-way (`plan`): what
    run_back then does."""
    way, version = plan['way'], plan['version']
    if way != 'w1':
        t3, where = _upstream_t3(cfg, version)
        if t3 is None:
            return {'ok': False, 'why': _gone(version, where)}
        return {'ok': True, 'version': version, 't3': t3,
                'why': f'run it again: it runs the update to t3 {version} again ({where})'}
    if _w1_got(chain, version) in ('updated', 'verified') and _names(cfg, version):
        return {'ok': True, 'version': version, 'why': f'run it again: it only verifies t3 {version} and runs the post '
                                                       'hooks (no second stop or restore)'}
    try:
        m = coldcopy.read_manifest(plan['copy'] or '', cfg.home)
    except (coldcopy.Incomplete, coldcopy.WrongCopy) as e:
        return {'ok': False, 'why': f'the W1 left half-way cannot be finished: {e}'}
    old, at = m.get('liveVersion'), m.get('createdAt')
    t3, kept, why = old_t3(cfg, old)
    if t3 is None:
        return {'ok': False, 'why': f'the W1 left half-way cannot be finished: {why}'}
    return {'ok': True, 'version': old, 't3': t3, 'copy': plan['copy'], 'kept': kept,
            'why': f'run it again: it finishes the W1 left half-way: it restores {plan["copy"]} (taken {at} under t3 '
                   f'{old}) and goes back to t3 {old}: everything since {at} is lost; by hand: --lose-work-since {at}'}


def possible(cfg, adapter, runs):
    """Read only: what `t3-golive back` lists: ways(), as `back --way <each>` would find it behind what `runs` ends
    with (_follow): refused behind another way left half-way, or the rerun of its own."""
    out, chain = ways(cfg, adapter, _stuck_w1(runs)), _chain(runs)
    for way in out:
        plan, stuck, why = _follow(runs, way)
        if why:
            out[way] = {'ok': False, 'why': why}
        elif plan:
            out[way] = _rerun(cfg, chain, plan)
    return out


def _resume_w1(run, cfg, adapter, plan, got, say):
    """A stopped W1 that got past its update ('updated') or its verify ('verified'), whose build the service state
    still names: no second stop or restore (T3 may have served new work since). It verifies, unless that build is
    verified and runs steadily, then runs the post hooks. A failure here runs no post hook (that W1 stopped T3)."""
    version = plan['version']
    with run.step('resume', way='w1', version=version, t3=plan['t3'], copy=plan['copy'], got=got):
        pass

    def finish():
        server = _server(cfg, adapter)
        if got == 'verified' and server and _steady_on(cfg, adapter, version) and adapter.service()['enabled']:
            say(f'the stopped W1 got past its verify: t3 {version} runs (pid {server["pid"]}); only the post hooks '
                'are left')
            return {'version': version, 'pid': server['pid']}
        say(f'the stopped W1 got past its update: verifying t3 {version} (the copy is not restored again)')
        return _verify(run, cfg, adapter, version, 0, say)
    try:
        return _hooked(run, cfg, True, finish, pre=False, post_on_failure=lambda: False, rerun=W1_RERUN)
    except Exception as e:
        if run._broken == 'verify':
            e.add_note(f't3 {version} is still not verified; {_follow_note(cfg, version)}')
        raise


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
        # plan: the back (or its followers) that stopped half-way: finish its plan. None behind a W1 stuck on its
        # verify (stuck) that this W2 or W3 follows: its own plan, from what is possible now.
        chain = _chain(runs)
        plan, stuck, why = _follow(runs, way)
        if why:
            raise journal.Refused(why)
        if plan and way == 'w1':
            got = _w1_got(chain, plan['version'])
            if got in ('updated', 'verified') and _names(cfg, plan['version']):
                return _resume_w1(run, cfg, adapter, plan, got, say)
        if plan and way != 'w1':  # its t3 may be gone since (unstaged, pruned): look again
            t3, where = _upstream_t3(cfg, plan['version'])
            if t3 is None:
                raise journal.Refused(_gone(plan['version'], where))
            plan['t3'] = t3
        if plan is None:
            w = ways(cfg, adapter, stuck)[way]
            if not w['ok']:
                raise journal.Refused(f'{way} is not possible now: {w["why"]}')
            plan = {'way': way, **{k: w.get(k) for k in ('version', 't3', 'copy')}}
        if way == 'w1':
            return restore_and_downgrade(run, cfg, adapter, plan['copy'], automatic=automatic,
                                         lose_work_since=lose_work_since, say=say)
        # Behind a W1 that stopped T3 (this run, or the one it reruns, follows a stuck W1): that W1 ran the pre
        # hooks and left the background jobs paused; they stay paused until a back ends ok, so no post hook runs
        # after a failure here.
        held = any(s['step'] == 'stop-service' for r in chain for s in r['steps'])
        with run.step('plan', **plan):
            pass
        if stuck:
            say(f'following the W1 to t3 {stuck["version"]} that is stuck on its verify: {way.upper()} goes to t3 '
                f'{plan["version"]} on the data as that W1 left it; nothing is restored again')
        return _hooked(run, cfg, True, lambda: _update(run, cfg, adapter, plan['t3'], plan['version'], say),
                       pre=not held, post_on_failure=lambda: not held)


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
    notes.append(f'fork features on: {live.features_on(fork)}' if on else 'fork features: all off')
    notes.append(f'fork switches: {json.dumps(_d(fork.get("features")), sort_keys=True)}')
    insts = [_d(i) for i in _d(report.get('settings')).get('providerInstances') or []]
    notes.append('provider instances: ' + (', '.join(f'{i.get("id")} ({i.get("driver")}, '
                                                       f'{"on" if i.get("enabled") else "off"})' for i in insts)
                                           or 'none'))


def _rerun_hint(runs):
    """What status says about a back or switch left half-way: what it left and what the next back does."""
    chain = _chain(runs)
    named = [r for r in chain if _facts(r, 'way')]
    if not named:
        return ('no way back was begun: take a way back (`t3-golive back` lists the possible ones), or put the home '
                f'right and settle: {journal.SETTLE_HINT}')
    way, version = _facts(named[-1], 'way'), _facts(named[-1], 'version')
    again = f'`t3-golive back --way {way}`'
    if way != 'w1':
        return f'run {again} again: it runs the update to t3 {version} again'
    got, verify, fails = _w1_scan(chain, version)
    if verify:
        said = 'did not pass: T3 may be down' if verify == 'failed' else f'{CUT_OFF}: T3 may be up, coming up or down'
        then = (f'. If t3 {version} does not come up, {MAY_FOLLOW}' if _stuck_w1(runs) else f'; {_not_yet(fails)}')
        return (f'W1 got past its update to t3 {version}, but its last verify {said}, and background jobs stay '
                f'paused. Run {again} again: it only verifies and runs the post hooks (no second restore while the '
                f'service state names that build){then}')
    if got == 'verified':
        return (f'W1 got past its verify (t3 {version} runs and starts at boot); only its post hooks are left: '
                f'background jobs may stay paused until {again} runs them (no second stop or restore while the '
                'service state names that build)')
    if got == 'updated':
        return (f'W1 got past its update to t3 {version}; background jobs stay paused until {again} verifies it and '
                'runs the post hooks (no second restore while the service state names that build)')
    if got == 'stopped':
        return (f'W1 stopped T3: T3 stays stopped and is not started at boot, and background jobs stay paused, until '
                f'{again} ends ok (it finishes the restore and the downgrade)')
    return f'W1 did not stop T3; run {again} again to finish it'


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
            add('journal', False, f'{journal.describe(open_run)}; {_rerun_hint(runs)}')
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
        for v, f, b, why in keptruntime.listing(cfg):
            notes.append(f'kept runtime of t3 {v} (for W1) {f}: {b / MB:.1f} MB' + (f'; NOT usable: {why}' if why
                                                                                     else ''))
            if why and EXACT_VERSION.match(v):  # present but damaged: W1's safety net is gone
                add('way back W1', False, f'not available: the kept runtime of t3 {v} is damaged: {why}')
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
        runs, unread = [], None
        try:  # read only, like status: what a back or switch left half-way decides what may follow it
            with journal.lock(cfg.state_dir, 'back', exclusive=False, create=False):
                runs = journal.read(cfg.state_dir)[0]
        except (journal.Busy, journal.Damaged) as e:
            unread = e
        for way, w in possible(cfg, adapter, runs).items():
            print(f'{way.upper()} {"possible" if w["ok"] else "not possible"}: {w["why"]}', file=out)
        if runs and journal.needs_settling(runs[-1]) and runs[-1]['command'] in FOLLOWS:
            print(f'Left half-way: {journal.describe(runs[-1])}; {_rerun_hint(runs)}', file=out)
        if unread:
            print(f'The journal was not read ({unread}): what a run left half-way is not counted in here; see '
                  '`t3-golive status`', file=out)
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
              + ('. If it is stopped or fails after it stopped T3, T3 stays stopped and is not started at boot, and '
                 'background jobs stay paused, until the same back is run again and ends ok' if args.way == 'w1'
                 else ''), file=out)
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
        print(f't3-golive: back failed: {type(e).__name__}: {e}{notes}\nRun the same `t3-golive back` again: it '
              'finishes where this one stopped. `t3-golive status` says where T3 is.', file=err)
        return 1
    print('nothing to do' if result.get('nothing') else f'back on t3 {result["version"]}, verified', file=out)
    return 0
