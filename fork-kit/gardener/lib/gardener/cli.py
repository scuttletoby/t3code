"""gardener: run each project's checks once a day and keep one rolled-up report issue per project
(fork-kit/gardener/README.md).

  gardener run [--project NAME] [--dry-run] [--force] [--timer]
      run the checks of every project (or one) and write its report issue.
      A project that already ran today (its done marker) is skipped unless --force.
      --dry-run: print the report instead; nothing is written (no issue, no done marker, no state), and checks set to
                 fix are run with fix off.
      --timer:   what the timer passes: a project runs only once its "schedule" time has passed today and, when it
                 has "quietHours", only inside them (its day then starts when they start).
  gardener check
      read the config and print each project and its checks; no check runs, no network.

The config is the machine file's "gardener" section. Exit codes: 0 fine, 1 a project's run failed (it runs again on
the next tick) or its report issue could not be written (the next ticks try the issue alone, MAX_REPORT_TRIES a
day), 2 the config is refused.
"""
import datetime, errno, fcntl, json, os, sys

from gardener import config, judge, report, runner

STATE_VERSION = 1
MAX_REPORT_TRIES = 3         # writes of one day's report issue: the run's own and two on later ticks


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


def day_and_window(project, t):
    """(the project's day at t, 'YYYY-MM-DD'; None when the timer may run it now, else why not).

    Without quietHours the day is the local date and the timer runs the project once the schedule time (if any) has
    passed. With quietHours the day starts when they start (22:00-06:00: 01:00 on the 24th is still the 23rd's
    window, so a window that wraps past midnight is one run, not two; a tick missed at 23:00 runs at 01:00), the
    timer runs only inside them, and a schedule time inside them holds it until then (one outside them is not
    used)."""
    lt = local(project, t)
    m = lt.hour * 60 + lt.minute
    s = _minutes(project['schedule']) if project['schedule'] else None
    hold = f'not yet: it runs after {project["schedule"]}'
    if not project['quietHours']:
        return lt.date().isoformat(), (hold if s is not None and m < s else None)
    a, b = (_minutes(x) for x in project['quietHours'].split('-'))
    length, pos = (b - a) % 1440, (m - a) % 1440
    day = (lt - datetime.timedelta(minutes=pos)).date().isoformat()
    if pos >= length:
        return day, f'outside its quiet hours {project["quietHours"]}'
    if s is not None and (s - a) % 1440 < length and pos < (s - a) % 1440:
        return day, hold
    return day, None


class State:
    """<stateDir>/<project>/state.json: {"version": 1, "doneDay": "YYYY-MM-DD", "issue": N, "lastRun": ISO,
    "findings": N, "reportPending": bool, "reportTries": N, "reportRepo": "OWNER/REPO"}. A reportRepo other
    than the config's (changed by hand) is not done today: the day runs again for the new repository, so a part
    rendered for one repository never goes to another. Missing, broken or of an older version: read as empty,
    with a note in the report. Of a NEWER version (a downgraded kit): `newer` is set and a run refuses, so the newer
    state is never overwritten."""

    def __init__(self, folder):
        self.folder = folder
        self.path = os.path.join(folder, 'state.json')
        self.note = None
        self.newer = None
        self.data = {}
        try:
            with open(self.path, encoding='utf-8') as f:
                data = json.load(f)
            v = data.get('version') if isinstance(data, dict) else None
            if isinstance(v, int) and not isinstance(v, bool) and v > STATE_VERSION:
                self.newer = v
                self.note = f'the state file is of a newer gardener (version {v}): a run refuses until it is upgraded'
                return
            if v != STATE_VERSION:
                raise ValueError(f'not version {STATE_VERSION}')
            self.data = data
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            self.note = f'the state file could not be read ({type(e).__name__}): started from empty state'

    def done_day(self):
        d = self.data.get('doneDay')
        return d if isinstance(d, str) else None

    def count(self, key):
        n = self.data.get(key)
        return n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0

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
    today, why_not = day_and_window(project, t)
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
        if state.newer and not args.dry_run:
            say(f'{name}: the state file is of a newer gardener (version {state.newer}); refused, nothing changed: '
                'upgrade the gardener kit')
            return 1
        moved = (state.data.get('reportRepo') or '') != project['reportRepo']
        if moved and state.done_day() == today and not args.dry_run:
            say(f'{name}: the report repository changed today; it runs again for the new one')
        if moved:
            state.data.pop('issue', None)      # that number is of the old report repository
        if not args.dry_run and not args.force and state.done_day() == today and not moved:
            if state.data.get('reportPending') is True and project['reportRepo']:
                return send_report(cfg, project, state, folder, gh_factory)
            say(f'{name}: ran today already ({today}); nothing to do (--force runs it again)')
            return 0
        if args.timer and not args.force and why_not:
            say(f'{name}: {why_not}; skipped')
            return 0
        notes = [state.note] if state.note else []
        findings = []
        for check in project['checks']:
            c = dict(check, fix=check['fix'] and not args.dry_run)
            try:
                got, ok = runner.run(project, c, today, cfg['gh'], os.path.join(folder, 'cache', check['id']),
                                     args.dry_run)
            except Exception as e:             # a MemoryError too: one check never stops the run
                got, ok = [runner.failed(check, 'the gardener could not run it', type(e).__name__)], False
            findings += got
            say(f'{name}: check {check["id"]}: ' + (f'{len(got)} finding(s)' if ok else f'failed: {got[0]["text"]}'
                                                    + (f' ({got[0]["detail"]})' if got[0].get('detail') else '')))
        root = config.expand(project['root']) if project['root'] else ''
        home = report.home_dir()
        text_checks = {c['id'] for c in project['checks'] if c['name'] or c['publicText']}
        counts = counts_only(cfg, project, gh_factory)
        public = report.public_findings(findings, root, home, project['reportRepo'], text_checks, counts,
                                        project['repo'])
        answer, note = judge.judge(project['model'], public, root)
        if note:
            notes.append(note)
        part = report.render_issue(name, today, findings, notes, answer, root, home, project['reportRepo'],
                                   text_checks, counts, project['repo'])
        full = report.render_local(name, today, findings, notes, answer)
        if args.dry_run:
            say(f'{name}: dry run: the report issue would get:')
            print(part, end='')
            say(f'{name}: dry run: the local report.md would get:')
            print(full, end='')
            say(f'{name}: dry run: {len(findings)} finding(s); nothing written')
            return 0
        report_path = os.path.join(folder, 'report.md')
        write_atomic(report_path, full)
        if project['reportRepo']:
            write_atomic(os.path.join(folder, 'issue.md'), part)
        # The done marker before the issue write: a stop or a GitHub error from here on never runs the checks (and
        # their fixes) again today; the next ticks send the saved issue part alone (send_report).
        state.save(doneDay=today, lastRun=t.isoformat(timespec='seconds'), findings=len(findings),
                   reportPending=bool(project['reportRepo']), reportTries=0, reportRepo=project['reportRepo'])
        if not project['reportRepo']:
            say(f'{name}: {len(findings)} finding(s); report {report_path}')
            return 0
        return send_report(cfg, project, state, folder, gh_factory)
    finally:
        if held is not None:
            os.close(held)


def counts_only(cfg, project, gh_factory):
    """True when the issue may show counts only: the report repository is public and the project's repository is
    private, not set, or of a visibility GitHub does not tell (fail closed). Two reads at most; none when the report
    goes to the project's own repository or to none."""
    rr, pr = project['reportRepo'], project['repo']
    if not rr or (pr and pr.lower() == rr.lower()):
        return False
    try:
        gh = gh_factory(cfg['gh'])
        if gh.private(rr) is True:
            return False
        return not pr or gh.private(pr) is not False
    except report.GitHubError:
        return True


def send_report(cfg, project, state, folder, gh_factory):
    """Write the day's saved issue part (issue.md) to the report issue. 0 written (or no tries left today), 1 not."""
    name = project['name']
    tries = state.count('reportTries')
    if tries >= MAX_REPORT_TRIES:
        say(f'{name}: the report issue could not be written ({tries} tries today); the next day\'s run tries again. '
            f'The report is in {os.path.join(folder, "report.md")}')
        return 0
    try:
        with open(os.path.join(folder, 'issue.md'), encoding='utf-8') as f:
            part = f.read()
    except OSError as e:
        state.save(reportPending=False)
        say(f'{name}: the saved report part is gone ({type(e).__name__}); the next day\'s run writes the issue')
        return 1
    try:
        number, how = report.publish(gh_factory(cfg['gh']), project['reportRepo'], name, part, state.issue())
    except report.GitHubError as e:
        tries += 1
        state.save(reportTries=tries, reportPending=tries < MAX_REPORT_TRIES)
        say(f'{name}: the report issue could not be written: {e}; ' +
            (f'the next tick tries the issue again ({MAX_REPORT_TRIES - tries} more today)' if tries < MAX_REPORT_TRIES
             else 'no more tries today'))
        return 1
    state.save(issue=number, reportPending=False, reportTries=0)
    say(f'{name}: {state.count("findings")} finding(s); report {project["reportRepo"]}#{number} ({how})')
    return 0


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
            say(f'{p["name"]}: report to {p["reportRepo"] or "(the state folder only)"}, '
                + (f'after {p["schedule"]}' if p['schedule'] else 'any time of day')
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
        except Exception as e:               # anything (a broken state folder, MemoryError) never stops the others
            say(f'{p["name"]}: failed: {type(e).__name__}: {e}')
            code = 1
    return code
