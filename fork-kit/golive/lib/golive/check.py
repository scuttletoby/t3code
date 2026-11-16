"""The command line (`check`, `settle`, `copy`; coldcopy.py has the copy) and `t3-golive check`: may this T3 home move to the target build now?

check is read only: it never stops, starts or writes to a T3 home, never opens its database (the target build's
read-only report does that) and never runs a `t3` from PATH. It writes only the kit's own lock file.
Exit 0: ready (the plan is printed), or nothing to do (the live server already runs the target; no database and
nothing ever ran here as a service), and only when no check failed. Exit 1: refused, with every reason.
Exit 2: the kit cannot run here (no config, no adapter for this OS).

settle records that the owner put the home right after a run that stopped half-way or ended with an error (or
moves a damaged journal aside), so changing runs may start again. It writes only the kit's own journal.
"""
import argparse, json, os, sys

import machine_file
from golive import config as config_mod, journal, live, osadapter

MB = 1024 * 1024


def _same_dir(unit_home, home):
    """The unit's home names this folder: an absolute path (a relative one would be read from the service's own
    folder, not ours) to the same folder, a link to it included."""
    try:
        return os.path.isabs(unit_home) and os.path.samefile(unit_home, home)
    except (OSError, TypeError):
        return False


def run_check(cfg, adapter, own_run=None):
    """(exit code, result dict).

    Alone it holds the kit's lock shared, so it never runs beside a changing run. A changing command that must
    pass the check first (the later `switch`) passes its own open journal.run() as own_run: the check then runs
    under that run's exclusive lock, with no gap between check and change, and does not count that run itself as
    unfinished. own_run must be a run that holds the lock on this stateDir right now (ValueError otherwise), so
    no one else gets past the lock this way.
    """
    checks, info = [], []
    result = {'ok': False, 'home': cfg.home, 'target': cfg.target_version, 'checks': checks, 'info': info}

    def add(name, ok, detail):
        checks.append({'name': name, 'ok': ok, 'detail': detail})
        return ok

    if own_run is not None:
        try:
            same = os.path.samefile(own_run.state_dir, cfg.state_dir)
        except OSError:
            same = False
        if not (own_run.holds_lock and same):
            raise ValueError('own_run must be an open journal.run() on this stateDir')
        if own_run.in_step is not None:  # fork #186: a refused pre-check must not leave a run to settle
            raise ValueError(f'the check runs before the first step, not inside step "{own_run.in_step}"')
        return _checks(cfg, adapter, result, add, info, own_run.id)
    try:
        with journal.lock(cfg.state_dir, 'check', exclusive=False):
            return _checks(cfg, adapter, result, add, info, None)
    except journal.Busy as e:
        add('one run at a time', False, str(e))
        return 1, result


def _journal(cfg, add, info, own_id):
    try:
        runs, torn = journal.read(cfg.state_dir)
    except journal.Damaged as e:  # Newer included: "update the kit"
        return add('journal', False, str(e))
    if torn:
        info.append(torn)
    runs = [r for r in runs if r['id'] != own_id]
    last = runs[-1] if runs else None
    if last is not None and journal.needs_settling(last):
        return add('journal', False, f'{journal.describe(last)}; {journal.SETTLE_HINT}')
    if last is not None and last['end'] is not None and last['end']['result'] != 'ok' and not last['settled']:
        info.append(f'last run: {last["command"]} ended {last["end"]["result"]} before its first step: '
                    f'{last["end"]["detail"]}')
    elif last is not None and last['end'] is None and not last['settled']:
        info.append(f'last run: {last["command"]} (pid {last["pid"]}) stopped before its first step; it changed '
                    'nothing')
    return add('journal', True, 'no run to settle')


def _checks(cfg, adapter, result, add, info, own_id):
    """Every check, in an order where a refusal always wins: "nothing to do" (exit 0) is answered only when no
    check has failed, and "already on the target" only from a live server started by the boot service."""
    _journal(cfg, add, info, own_id)

    def all_ok():
        return all(c['ok'] for c in result['checks'])

    if not os.path.isdir(cfg.home):
        add('T3 home', False, f'{cfg.home} does not exist')
        return 1, result
    try:
        svc = adapter.service()
    except osadapter.AdapterError as e:
        add('boot service', False, f'the service manager did not answer: {e}')
        return 1, result
    db_bytes = live.database_bytes(cfg.home)
    found = live.traces(cfg.home)

    aside = live.database_aside(cfg.home)
    if db_bytes is None and not svc['loaded'] and not found and not aside:
        # Nothing ran here as a service: no database, no boot service, no runtime files, no markers.
        if not os.path.isdir(os.path.join(cfg.home, 'userdata')):
            add('T3 home', False, f'{cfg.home} holds no userdata folder: it is not a T3 home (is "home" right?)')
        elif all_ok():
            add('T3 home', True, 'no database, no boot service and no server')
            result.update(ok=True, nothing='no-database')
            info.append(f'no database in {cfg.home}: nothing to protect; use the normal installer')
            return 0, result
        return 1, result

    if not svc['loaded']:
        add('boot service', False, f'T3 is not run as a boot service here ({osadapter.UNIT} is not installed); '
                                   'the kit covers boot-service installs only')
    elif not _same_dir(svc['home'], cfg.home):
        add('boot service', False, f'{osadapter.UNIT} serves {svc["home"]!r}, not {cfg.home}')
    elif not (svc['active'] and svc['enabled']):
        add('boot service', False, f'{osadapter.UNIT} must be enabled and running '
                                   f'(active: {svc["active"]}, enabled: {svc["enabled"]})')
    else:
        add('boot service', True, f'{osadapter.UNIT} serves this home')

    server = None
    try:
        server = live.live_server(cfg.home, svc['mainPid'])
        result['live'] = server
        add('live server', True, f't3 {server["version"]}, pid {server["pid"]}, port {server["port"]}')
    except live.NotLive as e:
        add('live server', False, str(e))

    try:
        active, pending = live.service_state(cfg.home)
        if pending:
            add('no update in flight', False, f'an update to {pending.get("targetVersion")} is pending')
        elif server is not None and active != server['version']:
            add('no update in flight', False, f'the service state says {active}, the server runs {server["version"]}')
        else:
            add('no update in flight', True, f'service state: {active}')
        if server is None and active == cfg.target_version:
            add('no half switch', False, f'the service state says {active} is active but no server of it runs: '
                                         'a switch stopped half-way or the new build does not start; '
                                         'start the service or go back first')
    except live.NotLive as e:
        add('no update in flight', False, str(e))
    marks = live.markers(cfg.home)
    add('no markers', not marks, ', '.join(marks) if marks else 'no restart, stop or restore marker')

    if db_bytes is None:
        has = ([f'{osadapter.UNIT} is installed'] if svc['loaded'] else []) + found + [f'found {a}' for a in aside]
        add('database', False, f'no {live.DATABASE} in {os.path.join(cfg.home, "userdata")}, yet this home has: '
                               f'{", ".join(has)}: a restore may have stopped half-way (database moved aside); do not install '
                                 'over it, put it back first')
        return 1, result

    if server is not None:
        order = live.compare_versions(cfg.target_version, server['version'])
        if order == 0:
            if all_ok():
                result.update(ok=True, nothing='already-on-target')
                info.append(f'the live T3 already runs {cfg.target_version}: nothing to do')
                return 0, result
            info.append(f'the live T3 already runs {cfg.target_version}')
            return 1, result
        add('target is newer', order > 0, f'{server["version"]} -> {cfg.target_version}')

    binary = None
    try:
        binary = live.staged_runtime(cfg.stage_dir, cfg.target_version)
        add('target staged', True, f'{binary} matches the release checksums')
    except live.NotLive as e:
        add('target staged', False, str(e))

    _disk(cfg, db_bytes, add)

    if binary is not None:
        try:
            report = live.fork_report(binary, cfg.home)
            _report(cfg, report, result, add)
        except live.NotLive as e:
            add('report', False, str(e))

    ok = all_ok()
    result['ok'] = ok
    if ok:
        result['plan'] = _plan(cfg, server['version'], db_bytes, result.get('counts'))
    return (0 if ok else 1), result


def _disk(cfg, db_bytes, add):
    floor = cfg.free_floor_mb * MB
    backup_parent = cfg.backup_dir
    while not os.path.isdir(backup_parent) and os.path.dirname(backup_parent) != backup_parent:
        backup_parent = os.path.dirname(backup_parent)
    home_fs, backup_fs = os.stat(cfg.home).st_dev, os.stat(backup_parent).st_dev

    def free(path):
        st = os.statvfs(path)
        return st.f_bavail * st.f_frsize

    if home_fs == backup_fs:
        need, have = 2 * db_bytes + floor, free(cfg.home)
        add('free disk', have >= need, f'{have // MB} MB free, need {need // MB} MB (2 x database + floor)')
    else:
        need = db_bytes + floor
        for label, path in (('free disk (home)', cfg.home), ('free disk (backups)', backup_parent)):
            have = free(path)
            add(label, have >= need, f'{have // MB} MB free, need {need // MB} MB (database + floor)')


def _report(cfg, report, result, add):
    version = report.get('version')
    add('report: target build', version == cfg.target_version, f'the staged build says it is {version}')
    fork = report.get('fork') if isinstance(report.get('fork'), dict) else {}
    on = fork.get('featuresOn')
    if not isinstance(on, list):
        add('fork features off', False, 'the report has no fork.featuresOn list')
    else:
        add('fork features off', not on, f'switched on in settings: {", ".join(on)}' if on else 'all off')
    problems = report.get('problems') if isinstance(report.get('problems'), list) else []
    named = '; '.join(f'{p.get("code")}: {p.get("message")}' for p in problems if isinstance(p, dict))
    readable = report.get('ok') is True
    add('report: home readable', readable,
        'database and settings read cleanly' if readable else named or 'the report says it is not ok')
    db = report.get('database') if isinstance(report.get('database'), dict) else {}
    result['counts'] = db.get('counts')
    result['pendingMigrations'] = db.get('pendingMigrations')


def _plan(cfg, live_version, db_bytes, counts):
    return [
        f'stop {osadapter.UNIT} at a quiet point (later command)',
        f'cold copy of the database ({db_bytes // MB} MB) and small files to a new folder in {cfg.backup_dir}',
        f'update {live_version} -> {cfg.target_version} with the staged build, then verify'
        + (f' (threads now: {counts.get("threads")}, projects: {counts.get("projects")})'
           if isinstance(counts, dict) else ''),
        'expected downtime: unknown until a rehearsal of this target has run',
    ]


def _print(code, result, out):
    for c in result['checks']:
        print(f'{"ok  " if c["ok"] else "FAIL"}  {c["name"]}: {c["detail"]}', file=out)
    for line in result['info']:
        print(f'note  {line}', file=out)
    if result.get('plan'):
        print('Ready. Plan:', file=out)
        for n, step in enumerate(result['plan'], 1):
            print(f'  {n}. {step}', file=out)
    elif code == 1:
        print('Refused: nothing was changed.', file=out)


def _settle(cfg, reason, out, err):
    try:
        what, value = journal.settle(cfg.state_dir, reason)
    except (journal.Busy, journal.Newer, ValueError) as e:
        print(f't3-golive: {e}', file=err)
        return 1
    if what == 'settled':
        print(f'settled: {journal.describe(value)}; reason: {reason.strip()}', file=out)
    elif what == 'moved':
        print(f'moved the damaged journal aside to {value}; the next run starts a new journal', file=out)
    else:
        print('nothing to settle: the last run ended ok (or there is none)', file=out)
    return 0


def main(argv, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog='t3-golive', description='Move a T3 boot service to a new build.')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('check', help='read only: may the live T3 move to the target now?',
                       description='Read only: may the live T3 move to the target build now? Exit 0: ready, or '
                                   'nothing to do. 1: refused, with every reason. 2: the kit cannot run here.')
    p.add_argument('--json', action='store_true', help='print the result as JSON')
    p = sub.add_parser('settle', help='after a run stopped half-way or failed: say the home is right again',
                       description='Record that you put the T3 home right after a run that stopped half-way or '
                                   'ended with an error (or move a damaged journal aside), so runs may start '
                                   'again. Writes only the kit\'s journal.')
    p.add_argument('--reason', required=True, help='what you did to put the T3 home right')
    p = sub.add_parser('copy', help='the practice stop: at a quiet point, stop T3, copy its data, start it again',
                       description='At a quiet point: stop the boot service, copy the T3 home\'s userdata (database '
                                   'with its WAL, settings, small files) to a new folder in backupDir, read both '
                                   'sides back, write the manifest last, start the same build again and verify it. '
                                   'Runs detached (a transient unit) unless --here.')
    p.add_argument('--owner-says-go', action='store_true',
                   help='do not wait for a quiet point (the owner says now is fine)')
    p.add_argument('--here', action='store_true', help='run in this process (refused inside the boot service)')
    p = sub.add_parser('guard', help=argparse.SUPPRESS)
    p.add_argument('--run', required=True)
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load()
    except machine_file.MachineFileError as e:
        print(f't3-golive: {e}\nThe machine file needs a "golive" section like this (placeholder values):\n'
              f'{config_mod.TEMPLATE}', file=err)
        return 2
    if args.command == 'settle':
        return _settle(cfg, args.reason, out, err)
    try:
        adapter = osadapter.for_this_os()
    except osadapter.Unsupported as e:
        print(f't3-golive: {e}', file=err)
        return 2
    if args.command in ('copy', 'guard'):
        from golive import coldcopy  # it uses run_check from this module
        if args.command == 'guard':
            coldcopy.guard(cfg, adapter, args.run)
            return 0
        return coldcopy.main_copy(cfg, adapter, args.here, args.owner_says_go, out, err)
    try:
        code, result = run_check(cfg, adapter)
    except Exception as e:  # fork #187: --json always prints JSON; never reads as ok
        code, result = 1, {'ok': False, 'home': cfg.home, 'target': cfg.target_version, 'info': [],
                           'checks': [{'name': 'check ran', 'ok': False, 'detail': f'{type(e).__name__}: {e}'}]}
    if args.json:
        print(json.dumps(result, indent=2), file=out)
    else:
        _print(code, result, out)
    return code
