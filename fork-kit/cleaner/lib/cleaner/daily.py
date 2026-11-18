"""`fork-cleaner daily`: what the kit's one timer runs (systemd/fork-cleaner.timer), set by `cleaner.daily`.

mode "compare" (the default): `plan` (nothing deleted), the report, and the compare with the old tools' dry runs.
mode "run": `run` (deletes), then the report. It refuses (exit code 2, nothing deleted) while a timer named in
`oldTimers` is enabled or active, or when systemd cannot say: two cleaners must never both delete. "compare" runs
whatever the old timers do, because it deletes nothing.

Everything goes to `outDir`: plan-<day>.txt (or run-<day>.txt) and compare-<day>.txt, written whole or not at all and
kept `keepDays` days, and report.log, which the report appends its line to before it moves its mark (report.py).
Only files of those names are ever removed from the folder. The old tools' logs and state are never read or changed.
"""
import os, re, subprocess, tempfile, time

from . import compare as comparing, engine, report as reporting
from .facts import Facts, Unreadable

SYSTEMCTL = '/usr/bin/systemctl'
OFF_ENABLED = ('disabled', 'masked', 'masked-runtime', 'not-found')
OFF_ACTIVE = ('inactive', 'failed')
DATED = re.compile(r'^(?:plan|run|compare)-(\d{4}-\d\d-\d\d)\.txt$')
REPORT_FILE = 'report.log'


def unit_state(unit, run=subprocess.run, systemctl=SYSTEMCTL):
    """(is-enabled, is-active) of a user unit as systemd answers. Raises Unreadable when it does not."""
    answers = []
    for question in ('is-enabled', 'is-active'):
        try:
            done = run([systemctl, '--user', question, unit], stdin=subprocess.DEVNULL, capture_output=True,
                       text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as ex:
            raise Unreadable(f'systemctl --user {question} {unit}: {ex}') from None
        answer = done.stdout.strip()
        if not answer or '\n' in answer:
            raise Unreadable(f'systemctl --user {question} {unit}: no answer (exit {done.returncode}: '
                             f'{done.stderr.strip()[:120]})')
        answers.append(answer)
    return tuple(answers)


def old_timers_on(cfg, run=subprocess.run, systemctl=SYSTEMCTL):
    """The old timers that are on, as 'unit (is-enabled, is-active)'. Raises Unreadable."""
    on = []
    for unit in cfg.old_timers:
        enabled, active = unit_state(unit, run, systemctl)
        if enabled not in OFF_ENABLED or active not in OFF_ACTIVE:
            on.append(f'{unit} ({enabled}, {active})')
    return on


def write_whole(path, lines):
    fd, new = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.writelines(line + '\n' for line in lines)
        os.replace(new, path)
    except BaseException:
        try:
            os.unlink(new)
        except OSError:
            pass
        raise


def trim(cfg, at):
    """Removes this command's own dated files older than keepDays at the time `at`. Returns the names it could not
    remove."""
    cut = time.strftime('%Y-%m-%d', time.gmtime(at - cfg.daily_keep_days * 86400))
    failed = []
    for name in sorted(os.listdir(cfg.daily_dir)):
        found = DATED.match(name)
        if found and found.group(1) < cut:
            try:
                os.unlink(os.path.join(cfg.daily_dir, name))     # a name in the folder: unlink never follows a link
            except FileNotFoundError:
                pass
            except OSError as ex:
                failed.append(f'{name} ({ex.strerror})')
    return failed


def daily(cfg, say=print, facts=Facts, run=subprocess.run, systemctl=SYSTEMCTL, report=reporting.report, now=time.time):
    """Returns the exit code: 0, 1 when a step could not read something (or the compare found a path only the
    cleaner would delete), 2 when it refused."""
    mode = cfg.daily_mode
    if mode == 'run':
        try:
            on = old_timers_on(cfg, run, systemctl)
        except Unreadable as ex:
            say(f'fork-cleaner daily: refuses to delete: cannot tell whether the old timers are on ({ex})')
            return 2
        if on:
            say(f'fork-cleaner daily: refuses to delete while an old timer is on: {", ".join(on)}. Disable it '
                '(systemctl --user disable --now <timer>) or set cleaner.daily.mode to "compare"')
            return 2
    try:
        os.makedirs(cfg.daily_dir, mode=0o700, exist_ok=True)
    except OSError as ex:
        say(f'fork-cleaner daily: cannot make {cfg.daily_dir} ({ex.strerror}): nothing done')
        return 1
    at = now()
    today = time.strftime('%Y-%m-%d', time.gmtime(at))
    if mode == 'compare' and cfg.old_timers:
        try:
            on = old_timers_on(cfg, run, systemctl)
            say(f'fork-cleaner daily (compare, deletes nothing): old timers on: {", ".join(on) or "none"}')
        except Unreadable as ex:
            say(f'fork-cleaner daily (compare, deletes nothing): cannot tell whether the old timers are on ({ex})')
    codes, done = [], []

    lines = []
    code = engine.execute(cfg, facts(cfg), mode == 'run', say=lines.append)
    codes.append(code)
    step = 'run' if mode == 'run' else 'plan'
    path = os.path.join(cfg.daily_dir, f'{step}-{today}.txt')
    try:
        write_whole(path, lines)
        done.append(f'{step} exit {code} ({path})')
    except OSError as ex:
        codes.append(1)
        done.append(f'{step} exit {code} (could not write {path}: {ex.strerror})')
    if code == 2:
        say(f'fork-cleaner daily ({mode}): ' + '; '.join(done))
        for line in lines[-3:]:
            say(line)
        return 2

    code = report(cfg, say, out=os.path.join(cfg.daily_dir, REPORT_FILE))
    codes.append(code)
    done.append(f'report exit {code}')

    if mode == 'compare' and cfg.sources:
        out = []
        code = comparing.compare(cfg, None, out.append, run, plan_lines=lines)
        codes.append(code)
        path = os.path.join(cfg.daily_dir, f'compare-{today}.txt')
        try:
            write_whole(path, out)
            done.append(f'compare exit {code} ({path})')
        except OSError as ex:
            codes.append(1)
            done.append(f'compare exit {code} (could not write {path}: {ex.strerror})')
        for line in out:
            if not line.startswith('  '):
                say(line)
    try:
        failed = trim(cfg, at)
    except OSError as ex:
        failed = [f'{cfg.daily_dir} ({ex.strerror})']
    if failed:
        codes.append(1)
        done.append('could not remove old files: ' + ', '.join(failed))
    say(f'fork-cleaner daily ({mode}): ' + '; '.join(done))
    return max(codes)
