"""gardener: run each project's checks once a day and keep one rolled-up report issue per project
(fork-kit/gardener/README.md).

  gardener run [--project NAME] [--dry-run] [--force] [--timer]
      run the checks of every project (or one) and write its report issue.
      A project that already ran today (its done marker) is skipped unless --force.
      --dry-run: print the report instead; nothing is written (no issue, no done marker, no state), and checks set to
                 fix are run with fix off.
      --timer:   what the timer passes: a project runs only once its "schedule" time has passed today and, when it
                 has "quietHours", only inside them.
  gardener check
      read the config and print each project and its checks; no check runs, no network.

The config is the machine file's "gardener" section. Exit codes: 0 fine, 1 a project's run or report failed (it
runs again on the next tick), 2 the config is refused.
"""
import datetime, errno, fcntl, json, os, sys

from gardener import config, judge, report, runner

STATE_VERSION = 1


def now():
    """The current time, aware; GARDENER_TEST_NOW (ISO, with offset) sets it, for tests."""
    fixed = os.environ.get('GARDENER_TEST_NOW')
    if fixed:
        return datetime.datetime.fromisoformat(fixed)
    return datetime.datetime.now(datetime.timezone.utc)


def local(project, t):
    if project['timeZone']:
        import zoneinfo
        return t.astimezone(zoneinfo.ZoneInfo(project['timeZone']))
    return t.astimezone()


def _minutes(hhmm):
    h, m = hhmm.split(':')
    return int(h) * 60 + int(m)


def in_window(project, t):
    """(True, '') when the timer may run the project at local time t, else (False, why)."""
    lt = local(project, t)
    m = lt.hour * 60 + lt.minute
    if m < _minutes(project['schedule']):
        return False, f'not yet: it runs after {project["schedule"]}'
    if project['quietHours']:
        a, b = (_minutes(x) for x in project['quietHours'].split('-'))
        inside = a <= m < b if a < b else (m >= a or m < b)
        if not inside:
            return False, f'outside its quiet hours {project["quietHours"]}'
    return True, ''


class State:
    """<stateDir>/<project>/state.json: {"version": 1, "doneDay": "YYYY-MM-DD", "issue": N, "lastRun": ISO}.
    Missing, broken or of another version: read as empty, with a note in the report."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, 'state.json')
        self.note = None
        self.data = {}
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get('version') != STATE_VERSION:
                raise ValueError(f'not version {STATE_VERSION}')
            self.data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.note = f'the state file could not be read ({type(e).__name__}): started from empty state'

    def done_day(self):
        d = self.data.get('doneDay')
        return d if isinstance(d, str) else None

    def issue(self):
        n = self.data.get('issue')
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else None

    def save(self, **changes):
        self.data.update(changes, version=STATE_VERSION)
        write_atomic(self.path, json.dumps(self.data, indent=1, sort_keys=True))


def write_atomic(path, text):
    """path holds the old text or the new, never half; a failed write leaves no temporary file."""
    tmp = f'{path}.tmp-{os.getpid()}'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def lock(folder):
    """An open lock file held by this process, or None when another run holds it."""
    fd = os.open(os.path.join(folder, 'lock'), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        os.close(fd)
        if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise
    return fd


def say(msg):
    print(f'gardener: {msg}', flush=True)


def run_project(cfg, project, args, t, gh_factory):
    """0 done or skipped, 1 failed."""
    name = project['name']
    today = local(project, t).date().isoformat()
    folder = os.path.join(config.expand(cfg['stateDir']), name)
    if not args.dry_run:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        held = lock(folder)
        if held is None:
            say(f'{name}: another run is going; skipped')
            return 0
    else:
        held = None
    try:
        state = State(folder)
        if not args.dry_run and not args.force and state.done_day() == today:
            say(f'{name}: ran today already ({today}); nothing to do (--force runs it again)')
            return 0
        if args.timer and not args.force:
            ok, why = in_window(project, t)
            if not ok:
                say(f'{name}: {why}; skipped')
                return 0
        notes = [state.note] if state.note else []
        findings = []
        for check in project['checks']:
            c = dict(check, fix=check['fix'] and not args.dry_run)
            got, ok = runner.run(project, c, today, cfg['gh'])
            findings += got
            say(f'{name}: check {check["id"]}: ' + (f'{len(got)} finding(s)' if ok else 'failed'))
        root = config.expand(project['root']) if project['root'] else ''
        answer, note = judge.judge(project['model'], findings, root)
        if note:
            notes.append(note)
        part = report.render(name, today, findings, notes, answer, root=root, home=report.home_dir())
        if args.dry_run:
            print(part, end='')
            say(f'{name}: dry run: {len(findings)} finding(s); nothing written')
            return 0
        where = None
        if project['reportRepo']:
            try:
                number, how = report.publish(gh_factory(cfg['gh']), project['reportRepo'], name, part,
                                             state.issue())
            except report.GitHubError as e:
                say(f'{name}: the report could not be written: {e}; runs again on the next tick')
                return 1
            where = f'{project["reportRepo"]}#{number} ({how})'
            state.save(issue=number)          # at once: a stop before the done marker still finds this issue
        report_path = os.path.join(folder, 'report.md')
        write_atomic(report_path, part)
        # the done marker last: a run stopped before here runs again on the next tick
        state.save(doneDay=today, lastRun=t.isoformat(timespec='seconds'), findings=len(findings))
        say(f'{name}: {len(findings)} finding(s); report {where or report_path}')
        return 0
    finally:
        if held is not None:
            os.close(held)


def main(argv, gh_factory=report.GitHub):
    import argparse
    parser = argparse.ArgumentParser(prog='gardener', description=__doc__.split('\n\n')[0],
                                     formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = parser.add_subparsers(dest='cmd', required=True)
    r = sub.add_parser('run')
    r.add_argument('--project')
    r.add_argument('--dry-run', action='store_true')
    r.add_argument('--force', action='store_true')
    r.add_argument('--timer', action='store_true')
    sub.add_parser('check')
    args = parser.parse_args(argv)
    try:
        cfg = config.load()
    except config.MachineFileError as e:
        print(f'gardener: config refused: {e}', file=sys.stderr)
        return 2
    projects = cfg['projects']
    if args.cmd == 'check':
        for p in projects:
            say(f'{p["name"]}: report to {p["reportRepo"] or "(the state folder only)"}, after {p["schedule"]}'
                + (f' in {p["quietHours"]}' if p['quietHours'] else '')
                + (f', model {p["model"]["model"]}' if p['model'] else ''))
            for c in p['checks']:
                say(f'  {c["id"]}: {c["name"] or c["path"]}' + (' (fix)' if c['fix'] else '')
                    + f', timeout {c["timeoutSeconds"]} s')
        return 0
    if args.project:
        projects = [p for p in projects if p['name'] == args.project]
        if not projects:
            print(f'gardener: no project {args.project!r} in the config', file=sys.stderr)
            return 2
    t = now()
    code = 0
    for p in projects:
        try:
            code |= run_project(cfg, p, args, t, gh_factory)
        except OSError as e:                 # one project's broken state folder never stops the others
            say(f'{p["name"]}: failed: {e}')
            code = 1
    return code
