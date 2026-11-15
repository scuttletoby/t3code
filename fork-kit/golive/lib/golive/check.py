"""`t3-golive check`: may this T3 home move to the target build now? Read only: it never stops, starts or writes to
a T3 home, never opens its database (the target build's read-only report does that) and never runs a `t3` from
PATH. It writes only the kit's own lock file.

Exit 0: ready (the plan is printed), or nothing to do (already on the target; no database to protect).
Exit 1: refused, with every reason. Exit 2: the kit cannot run here (no config, no adapter for this OS).
"""
import argparse, json, os, sys

import machine_file
from golive import config as config_mod, journal, live, osadapter

MB = 1024 * 1024


def _same_dir(a, b):
    try:
        return os.path.samefile(a, b) and os.path.realpath(a) == os.path.realpath(b)
    except OSError:
        return False


def run_check(cfg, adapter):
    """(exit code, result dict). Holds the kit's lock shared, so it never runs beside a changing run."""
    checks, info = [], []
    result = {'ok': False, 'home': cfg.home, 'target': cfg.target_version, 'checks': checks, 'info': info}

    def add(name, ok, detail):
        checks.append({'name': name, 'ok': ok, 'detail': detail})
        return ok

    try:
        with journal.lock(cfg.state_dir, 'check', exclusive=False):
            return _checks(cfg, adapter, result, add, info)
    except journal.Busy as e:
        add('one run at a time', False, str(e))
        return 1, result


def _checks(cfg, adapter, result, add, info):
    try:
        runs, torn = journal.read(cfg.state_dir)
        if torn:
            info.append(torn)
        last = runs[-1] if runs else None
        if last is not None and last['end'] is None:
            add('journal', False, journal.describe(last) + '; the command that was running must settle it first')
        else:
            add('journal', True, 'no run stopped half-way')
            if last is not None and last['end']['result'] != 'ok':
                info.append(f'last run: {last["command"]} ended {last["end"]["result"]}: {last["end"]["detail"]}')
    except journal.Damaged as e:
        add('journal', False, str(e))

    if not os.path.isdir(cfg.home):
        add('T3 home', False, f'{cfg.home} does not exist')
        return 1, result
    db_bytes = live.database_bytes(cfg.home)
    if db_bytes is None:
        result.update(ok=True, nothing='no-database')
        info.append(f'no database in {cfg.home}: nothing to protect; use the normal installer')
        return 0, result

    svc = adapter.service()
    if not svc['loaded']:
        add('boot service', False, f'T3 is not run as a boot service here ({osadapter.UNIT} is not installed); '
                                   'the kit covers boot-service installs only')
    elif svc['home'] is None or not _same_dir(svc['home'], cfg.home):
        add('boot service', False, f'{osadapter.UNIT} serves {svc["home"]!r}, not {cfg.home}')
    elif not (svc['active'] and svc['enabled']):
        add('boot service', False, f'{osadapter.UNIT} must be enabled and running '
                                   f'(active: {svc["active"]}, enabled: {svc["enabled"]})')
    else:
        add('boot service', True, f'{osadapter.UNIT} serves this home')

    live_version = None
    try:
        server = live.live_server(cfg.home, svc['mainPid'])
        live_version = server['version']
        result['live'] = server
        add('live server', True, f't3 {server["version"]}, pid {server["pid"]}, port {server["port"]}')
    except live.NotLive as e:
        add('live server', False, str(e))

    try:
        active, pending = live.service_state(cfg.home)
        live_version = live_version or active
        if pending:
            add('no update in flight', False, f'an update to {pending.get("targetVersion")} is pending')
        elif active != live_version:
            add('no update in flight', False, f'the service state says {active}, the server runs {live_version}')
        else:
            add('no update in flight', True, f'service state: {active}')
    except live.NotLive as e:
        add('no update in flight', False, str(e))
    found = live.markers(cfg.home)
    add('no markers', not found, ', '.join(found) if found else 'no restart, stop or restore marker')

    if live_version is not None:
        order = live.compare_versions(cfg.target_version, live_version)
        if order == 0:
            result.update(ok=True, nothing='already-on-target')
            info.append(f'the live T3 already runs {cfg.target_version}: nothing to do')
            return 0, result
        add('target is newer', order > 0, f'{live_version} -> {cfg.target_version}')

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

    ok = all(c['ok'] for c in result['checks'])
    result['ok'] = ok
    if ok:
        result['plan'] = _plan(cfg, live_version, db_bytes, result.get('counts'))
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


def main(argv, out=sys.stdout, err=sys.stderr):
    parser = argparse.ArgumentParser(prog='t3-golive', description='Move a T3 boot service to a new build.')
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('check', help='read only: may the live T3 move to the target now?',
                       description='Read only: may the live T3 move to the target build now? Exit 0: ready, or '
                                   'nothing to do. 1: refused, with every reason. 2: the kit cannot run here.')
    p.add_argument('--json', action='store_true', help='print the result as JSON')
    args = parser.parse_args(argv)
    try:
        cfg = config_mod.load()
    except machine_file.MachineFileError as e:
        print(f't3-golive: {e}\nThe machine file needs a "golive" section like this (placeholder values):\n'
              f'{config_mod.TEMPLATE}', file=err)
        return 2
    try:
        adapter = osadapter.for_this_os()
    except osadapter.Unsupported as e:
        print(f't3-golive: {e}', file=err)
        return 2
    try:
        code, result = run_check(cfg, adapter)
    except osadapter.AdapterError as e:
        print(f't3-golive: the service manager did not answer: {e}', file=err)
        return 1
    if args.json:
        print(json.dumps(result, indent=2), file=out)
    else:
        _print(code, result, out)
    return code
