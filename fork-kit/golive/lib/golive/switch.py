"""`t3-golive switch`: move the live T3 home to the staged target build (design 4.4), `verify` it (4.5), and go
back on its own (W1, 4.6) when verify fails or runs out of time. A guard (a second detached unit, `back --auto`)
waits for the run to end and, by a timer, looks again at the deadline: if the journal says neither "verified" nor
"rolled back" it puts T3 back, so a crashed kit cannot leave T3 down. (A reboot ends both transient units: the
enabled service then starts the build the unit names, and the journal asks for `back --auto --run <id>` by hand.)

Every change is made inside a journalled step (journal.py); the refusals all come first.
"""
import datetime, json, os, shutil, stat, subprocess, sys, time, urllib.error, urllib.request

from golive import check as check_mod, coldcopy, journal, live, osadapter

MIN_LIMIT = 600     # verify's time limit: 3 x the rehearsal's time, at least this (seconds)
GUARD_MARGIN = 600  # the guard timer fires this long after the latest the run should have ended
FINAL = {'verify': 'verified', 'back-verify': 'rolled back', 'back-start': 'old build runs'}


def _utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


def _t3(binary, args, timeout):
    """(exit code, output) of a t3 build run directly (never from PATH). Tests replace it."""
    try:
        p = subprocess.run([binary, *args], capture_output=True, text=True, timeout=timeout, errors='replace',
                           stdin=subprocess.DEVNULL, cwd='/')
    except (OSError, subprocess.TimeoutExpired) as e:
        return -1, str(e)
    return p.returncode, p.stdout + p.stderr


def passed_rehearsal(cfg, from_version):
    """The record of the last `rehearse` run if it passed for this target and from-version and is fresh; Refused
    otherwise. Until `rehearse` lands (go-live PR 5) this is the one place that defines the record: the rehearse
    run ends "ok" and its step "result" is done with {fromVersion, targetVersion, migrations: the ids the target
    runs, seconds: how long the target took to migrate and get ready on the clone}."""
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
    if data.get('fromVersion') != from_version:
        raise journal.Refused(f'the last rehearsal moved t3 {data.get("fromVersion")}, the live T3 runs '
                              f'{from_version}: rehearse again')
    ended = datetime.datetime.strptime(last['end']['at'], '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=datetime.timezone.utc)
    if _utcnow() - ended > datetime.timedelta(hours=cfg.rehearsal_max_age_hours):
        raise journal.Refused(f'the last rehearsal ({last["end"]["at"]}) is too old (rehearsalMaxAgeHours '
                              f'{cfg.rehearsal_max_age_hours}): rehearse again')
    if not (isinstance(data.get('migrations'), list) and type(data.get('seconds')) is int):
        raise journal.Refused('the rehearsal record has no migration list or time: rehearse again')
    return data


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


def _look(cfg, adapter, version, before, binary, back):
    """What is wrong with the live T3 now ([] when nothing): design 4.5, for `version`. Going forward the
    migrations must all have run and the counts must not shrink; going back the database must be the copy's."""
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
        report = live.fork_report(binary, cfg.home)
    except live.NotLive as e:
        return [f'report: {e}']
    db = report.get('database') if isinstance(report.get('database'), dict) else {}
    expect = before['migrations'] if back else []
    if db.get('pendingMigrations') != expect:
        said.append(f'pending migrations {db.get("pendingMigrations")}, expected {expect}')
    counts = db.get('counts') if isinstance(db.get('counts'), dict) else {}
    if back:
        said += live.compare_counts({'database': {'counts': before['counts']}}, report)
    else:
        said += [f'{k}: {before["counts"].get(k)} before, {counts.get(k)} now' for k in live.COUNTS
                 if not (type(counts.get(k)) is int and counts[k] >= before['counts'].get(k, 0))]
    fork = report.get('fork') if isinstance(report.get('fork'), dict) else {}
    if fork.get('featuresOn') != []:
        said.append(f'fork features on: {fork.get("featuresOn")}')
    if check_mod.providers_sum(report) != before['providers']:
        said.append('the provider instances differ from before the switch')
    if report.get('ok') is not True:
        said.append('the report is not ok')
    changed = check_mod.server_changed(cfg, adapter, server)
    return said + ([changed] if changed else [])


def verify(cfg, adapter, version, before, seconds, back=False):
    """Look until all of design 4.5 holds or `seconds` pass: (True, facts) or (False, {'problems'})."""
    binary = live.staged_runtime(cfg.stage_dir, cfg.target_version)
    end = time.monotonic() + seconds
    while True:
        said = _look(cfg, adapter, version, before, binary, back)
        if not said:
            return True, {'version': version}
        if time.monotonic() >= end:
            return False, {'problems': said[:8]}
        time.sleep(coldcopy.POLL)


def _copy_extras(cfg, unit_path, dest, version, deadline):
    """Into dest/extra: the old build's runtime folder, service-state.json and the unit file; entries hashed."""
    rt = os.path.join(cfg.home, 'runtime')
    sources = [(os.path.join(rt, 'versions', version), f'runtime/versions/{version}'),
               (os.path.join(rt, 'service-state.json'), 'runtime/service-state.json')]
    if unit_path:
        sources.append((unit_path, f'unit/{os.path.basename(unit_path)}'))
    entries = []
    for src, rel in sources:
        items = [(src, rel)]
        for d, dirs, names in (os.walk(src, onerror=coldcopy._raise) if os.path.isdir(src) else ()):
            dirs.sort()
            items += [(os.path.join(d, n), f'{rel}/{os.path.relpath(os.path.join(d, n), src)}')
                      for n in sorted(dirs + names)]
        for path, r in items:
            out, st = os.path.join(dest, 'extra', r), os.lstat(path)
            os.makedirs(os.path.dirname(out), 0o700, exist_ok=True)
            if stat.S_ISLNK(st.st_mode):
                os.symlink(os.readlink(path), out)
            elif stat.S_ISDIR(st.st_mode):
                os.makedirs(out, 0o700, exist_ok=True)
            elif stat.S_ISREG(st.st_mode):
                size, digest = coldcopy._copy_file(path, out, deadline, stat.S_IMODE(st.st_mode))
                entries.append({'path': r, 'from': path, 'bytes': size, 'sha256': digest})
    return entries


def _tree_bytes(path):
    return sum(os.lstat(os.path.join(d, n)).st_size for d, _, names in os.walk(path) for n in names)


def _update(cfg, adapter, binary, version, timeout, downgrade=False):
    code, out = _t3(binary, ['update', version, '--base-dir', cfg.home, *(['--allow-downgrade'] if downgrade
                                                                          else []), '--yes'], timeout)
    if code != 0:
        raise RuntimeError(f'{binary} update {version} exited {code}: {out.strip()[-300:]}')
    if not adapter.service()['active']:
        adapter.start()


def _way_back(run, cfg, adapter, rec, seconds):
    """W1, automatic mode (inside the switch run: nothing new exists yet). Go-live PR 7 takes this over as its
    `back` W1. Stop; put the copy's database and settings back (restore keeps the migrated ones aside); the old
    runtime from the copy if a prune took it; `<old t3> update <old> --allow-downgrade --yes`; links back; verify.
    Each step can run again: the restore starts again from the untouched copy."""
    home, old = cfg.home, rec['version']
    with run.step('back-stop'):
        adapter.stop()
        coldcopy.hold_owner_lock(os.path.join(home, 'userdata'), coldcopy.STOP_WAIT).close()
    with run.step('back-restore', copy=rec['copy']) as facts:
        facts.update(coldcopy.restore(run, rec['copy'], home, from_home=home, version=old))
    with run.step('back-downgrade', version=old) as facts:
        rt = os.path.join(home, 'runtime', 'versions', old)
        facts['runtimeFromCopy'] = not os.path.isfile(os.path.join(rt, '.install-complete'))
        if facts['runtimeFromCopy']:
            tmp = f'{rt}.golive-{os.getpid()}'
            shutil.copytree(os.path.join(rec['copy'], 'extra', 'runtime', 'versions', old), tmp, symlinks=True)
            if os.path.lexists(rt):
                os.rename(rt, f'{rt}{coldcopy.ASIDE}{coldcopy._stamp()}')
            os.rename(tmp, rt)
        _update(cfg, adapter, os.path.join(rt, 't3'), old, seconds, downgrade=True)
        facts['links'] = _repoint(run, home, old)
    with run.step('back-verify') as facts:
        ok, found = verify(cfg, adapter, old, rec, max(seconds, coldcopy.START_WAIT), back=True)
        facts.update(found, result='rolled back' if ok else 'failed')
    if not ok:
        raise RuntimeError(f'the way back did not verify: {"; ".join(found["problems"])}')
    return f'rolled back to t3 {old} from {rec["copy"]}'


def _put_back(run, cfg, adapter, steps):
    """After a failed or killed switch: what its journal says it got to decides. Past the update: W1. Stopped
    before it: start the old build again (nothing was changed). Returns what was done, or None."""
    started = {s['step'] for s in steps if s['phase'] == 'started'}
    rec = next((s['data'] for s in steps if (s['step'], s['phase']) == ('stopping', 'started')), None)
    if rec is None:
        return None
    if started & {'update', 'back-stop'}:
        return _way_back(run, cfg, adapter, rec, rec['limit'])
    with run.step('back-start') as facts:
        stopped = not adapter.service()['active']  # active: the stop never happened, the old server still runs
        if stopped:
            adapter.start()
        again = coldcopy._wait_started(cfg, adapter, rec['version'], rec['pid'] if stopped else None)
        facts.update(version=again['version'], pid=again['pid'], result=FINAL['back-start'])
    return f'started t3 {again["version"]} again; nothing was changed'


def final(run):
    """True when a switch run needs no putting back: it ended ok or refused, was settled, or its journal says
    "verified", "rolled back" or that the old build runs again."""
    if run['settled'] is not None or (run['end'] and run['end']['result'] in ('ok', 'refused')):
        return True
    return any(s['phase'] == 'done' and s['step'] in FINAL and (s['data'] or {}).get('result') == FINAL[s['step']]
               for s in run['steps'])


def run_switch(cfg, adapter, owner_says_go=False, say=print):
    with journal.run(cfg.state_dir, 'switch') as run:
        code, result = check_mod.run_check(cfg, adapter, own_run=run)
        if code == 0 and result.get('nothing') == 'already-on-target':
            say(f'the live T3 already runs {cfg.target_version}: nothing to do')
            return {'nothing': 'already-on-target'}
        if code != 0 or result.get('nothing'):
            raise journal.Refused('; '.join([f'{c["name"]}: {c["detail"]}' for c in result['checks']
                                             if not c['ok']] + result['info']) or 'check refused')
        server = result['live']
        rehearsal = passed_rehearsal(cfg, server['version'])
        if result.get('pendingMigrations') != rehearsal['migrations']:
            raise journal.Refused(f'the target would run migrations {result.get("pendingMigrations")}, the '
                                  f'rehearsal ran {rehearsal["migrations"]}: rehearse again')
        why = coldcopy._home_problem(cfg)
        if why is not None:
            raise journal.Refused(f'{why}; nothing was changed')
        runtime = os.path.join(cfg.home, 'runtime', 'versions', server['version'])
        need = coldcopy._plan_total(cfg) + _tree_bytes(runtime)
        coldcopy._free_check(cfg, need)
        if cfg.quiet_command is None and not owner_says_go:
            raise journal.Refused('the machine file has no golive.quietCommand: set one, or pass --owner-says-go')
        if not owner_says_go:
            coldcopy.wait_quiet(cfg, server['version'], say)
            coldcopy._free_check(cfg, need)
        why = check_mod.server_changed(cfg, adapter, server)
        if why is not None:
            raise journal.Refused(f'{why}; nothing was changed')
        dest = os.path.join(cfg.backup_dir, f't3-switch-{coldcopy._stamp()}-{server["version"]}')
        if os.path.lexists(dest):
            raise journal.Refused(f'{dest} already exists; a copy never goes over another: try again in a second')
        limit = max(MIN_LIMIT, 3 * rehearsal['seconds'])
        rec = {'version': server['version'], 'pid': server['pid'], 'copy': dest, 'limit': limit,
               'migrations': rehearsal['migrations'], 'counts': result.get('counts') or {},
               'providers': result.get('providers')}
        with run.step('guard') as facts:
            unit = facts['unit'] = f't3-golive-switch-{os.getpid()}-{os.urandom(3).hex()}'
            back = [coldcopy.KIT_BIN, 'back', '--auto', '--run', run.id]
            after = (coldcopy.STOP_WAIT + cfg.stop_limit_seconds + 2 * limit + 3 * coldcopy.HOOK_TIMEOUT
                     + coldcopy.START_WAIT + GUARD_MARGIN)
            try:
                adapter.run_detached(unit, back + ['--wait'], guard_after_seconds=after, guard_argv=back)
                facts['waiting'] = coldcopy._guard_waiting(cfg.state_dir, run.id)
            except osadapter.AdapterError as e:
                facts.update(waiting=False, error=str(e))
        if not facts['waiting']:
            raise journal.Refused(f'the guard {unit} did not come up within {coldcopy.GUARD_WAIT} s'
                                  f'{": " + facts["error"] if "error" in facts else ""}; T3 was not stopped')
        failed = coldcopy._hooks(run, 'pre-hooks', cfg.pre_hooks, cfg.home)
        if failed is None and not owner_says_go:
            ok, detail = coldcopy.quiet_now(cfg, server['version'])
            failed = None if ok else f'not quiet any more: {detail}'
        failed = failed or check_mod.server_changed(cfg, adapter, server) or coldcopy._home_problem(cfg)
        if failed is None and not coldcopy._guard_alive(adapter, unit):
            failed = f'the guard {unit} is not running any more'
        if failed is not None:
            again = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
            if again:
                raise RuntimeError(f'{failed}; T3 was not stopped; then a post hook failed: {again}')
            raise journal.Refused(f'{failed}; T3 was not stopped')
        say(f'stopping {osadapter.UNIT} (t3 {server["version"]}) to switch to {cfg.target_version}')
        error, verified, lock = None, False, None
        try:
            with run.step('stopping', **rec):
                adapter.stop()
                coldcopy._wait_stopped(adapter, server['pid'])
                lock = coldcopy.hold_owner_lock(os.path.join(cfg.home, 'userdata'), coldcopy.STOP_WAIT)
                coldcopy._same_after_stop(cfg, server)
            deadline = coldcopy._clock() + cfg.stop_limit_seconds
            with run.step('cold-copy', copy=dest) as facts:
                entries, total, left_out = coldcopy.copy_userdata(run, cfg.home, dest, deadline)
                extras = _copy_extras(cfg, adapter.service()['unitPath'], dest, server['version'], deadline)
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
                    'runId': run.id, 'home': cfg.home, 'liveVersion': server['version'], 'bytes': total,
                    'files': entries, 'leftOut': left_out, 'extras': extras})
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
            lock.close()
            lock = None
            with run.step('update', version=cfg.target_version) as facts:
                _update(cfg, adapter, live.staged_runtime(cfg.stage_dir, cfg.target_version), cfg.target_version,
                        limit)
                facts['links'] = _repoint(run, cfg.home, cfg.target_version)
            with run.step('verify') as facts:
                verified, found = verify(cfg, adapter, cfg.target_version, rec, limit)
                facts.update(found, result='verified' if verified else 'failed')
            if not verified:
                error = RuntimeError(f'verify failed: {"; ".join(found["problems"])}')
        except Exception as e:
            error = e
        finally:
            if lock is not None:
                lock.close()
        errors = [error] if error else []
        if error is not None:
            try:
                runs, _ = journal.read(cfg.state_dir)
                did = _put_back(run, cfg, adapter, runs[-1]['steps'])
                if did:
                    errors[0] = RuntimeError(f'{error}; {did}')
            except Exception as e:
                errors[0] = RuntimeError(f'{error}; then putting T3 back failed: {type(e).__name__}: {e}')
            errors[0].__cause__ = error
        failed = coldcopy._hooks(run, 'post-hooks', cfg.post_hooks, cfg.home)
        if failed:
            errors.append(RuntimeError(f'post hook failed: {failed}'))
        if errors:
            for later in errors[1:]:
                errors[0].add_note(f'then: {type(later).__name__}: {later}')
            raise errors[0]
        say(f'verified: the live T3 runs {cfg.target_version}; the copy is {dest}')
        return {'verified': cfg.target_version, 'copy': dest}


def back_auto(cfg, adapter, run_id, wait, say=print):
    """The switch's guard. With wait (started beside the run) it waits for the run to end; without (the timer, or
    by hand) it does nothing while any run holds the lock. Then, unless the run is final or no longer the last
    one, it puts T3 back as _put_back says, runs the post hooks if the run paused the jobs and did not resume them,
    and journals both into that run. A journal it cannot read fails open: T3 is started if it is not active, and
    the post hooks run. Logs to <stateDir>/guard.log; returns what it did."""
    if wait:
        try:
            coldcopy._guard_log(cfg.state_dir, {'run': run_id, 'waiting': True})
        except OSError:
            pass
    did = []
    try:
        with journal.lock(cfg.state_dir, 'back --auto', exclusive=True, wait=wait):
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
            if runs is not None and (mine is None or mine['settled'] or final(mine) and _resumed(mine)):
                did.append(f'nothing to do: run {run_id} is final or not the last run')
            elif mine is not None:
                r = journal.Run(cfg.state_dir, 'switch')
                r.id, error = run_id, None
                try:
                    if not final(mine):
                        did.append(_put_back(r, cfg, adapter, mine['steps']) or 'T3 was never stopped')
                except Exception as e:
                    error = e
                    did.append(f'putting T3 back failed: {type(e).__name__}: {e}')
                if not _resumed(mine):
                    did.append(coldcopy._hooks(r, 'post-hooks', cfg.post_hooks, cfg.home) or 'post hooks ran')
                if mine['end'] is None:
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
        print(f't3-golive: the switch failed: {type(e).__name__}: {e}{notes}\n{journal.SETTLE_HINT}', file=err)
        return 1
    return 0 if result else 1
