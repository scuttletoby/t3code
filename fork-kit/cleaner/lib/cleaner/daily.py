"""`fork-cleaner daily`: what the kit's one timer runs (systemd/fork-cleaner.timer), set by `cleaner.daily`.

mode "compare" (the default): `plan` (nothing deleted), the report, and the compare with the old tools' dry runs.
mode "run": `run` (deletes), then the report. It refuses (exit code 2, nothing deleted) unless systemd shows every
timer named in `oldTimers` off: two cleaners must never both delete. A timer is asked in the user and the system
scope. It counts as on when it is enabled or active in either, when the service it starts is still running, and
when neither scope knows it (a misspelled name, or a timer of another user: off cannot be proven). systemd that
cannot answer refuses too. "compare" runs whatever the old timers do, because it deletes nothing.

Everything goes to `outDir`: plan-<day>.txt (or run-<day>.txt) and compare-<day>.txt, written whole or not at all and
kept `keepDays` days (a second run the same day writes plan-<day>.2.txt and so on, never over the first), and
report.log, which the report appends its line to before it moves its mark (report.py). Only files of those names are
ever removed from the folder. The old tools' logs and state are never read or changed.

Exit codes (the unit's status): 0 done; 1 something failed (a step could not read something, a file could not be
written or trimmed, the report failed); 2 refused (run mode with an old timer on, or the plan refused); 3 compare
mode only: everything worked and the compare found paths only fork-cleaner would delete ("new only", each a bug until
explained). A 1 hides a 3: a real failure is never shown as "new only". The last line says the same in words.
"""
import errno, os, re, subprocess, tempfile, time

from . import compare as comparing, engine, report as reporting
from .facts import Facts, Unreadable

SYSTEMCTL = '/usr/bin/systemctl'
SCOPES = ('--user', '--system')
OFF_ENABLED = ('disabled', 'masked', 'masked-runtime')
OFF_ACTIVE = ('inactive', 'failed')
DATED = re.compile(r'^(?:plan|run|compare)-(\d{4}-\d\d-\d\d)(?:\.\d+)?\.txt$')
REPORT_FILE = 'report.log'
NEW_ONLY = comparing.NEW_ONLY           # exit code 3


def _ask(run, systemctl, scope, *question):
    """One line systemd answers; raises Unreadable when it gives none."""
    shown = f'systemctl {scope} {" ".join(question)}'
    try:
        done = run([systemctl, scope, *question], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                   timeout=60)
    except (OSError, subprocess.SubprocessError) as ex:
        raise Unreadable(f'{shown}: {ex}') from None
    answer = done.stdout.strip()
    if not answer or '\n' in answer:
        raise Unreadable(f'{shown}: no answer (exit {done.returncode}: {done.stderr.strip()[:120]})')
    return answer


def unit_state(unit, run=subprocess.run, systemctl=SYSTEMCTL):
    """{scope: (is-enabled, is-active, service it starts, that service's is-active)} for each scope that knows the
    timer; {} when neither does. Raises Unreadable when systemd does not answer."""
    states = {}
    for scope in SCOPES:
        enabled = _ask(run, systemctl, scope, 'is-enabled', unit)
        if enabled == 'not-found':
            continue
        active = _ask(run, systemctl, scope, 'is-active', unit)
        service = _ask(run, systemctl, scope, 'show', '-p', 'Unit', '--value', unit)
        states[scope] = (enabled, active, service, _ask(run, systemctl, scope, 'is-active', service))
    return states


def old_timers_on(cfg, run=subprocess.run, systemctl=SYSTEMCTL):
    """The old timers that are on, or that no scope knows, as 'unit (why)'. Raises Unreadable."""
    on = []
    for unit in cfg.old_timers:
        states = unit_state(unit, run, systemctl)
        if not states:
            on.append(f'{unit} (not found in the user or the system scope: off cannot be proven; remove it from '
                      'oldTimers once the old tool is gone)')
        for scope, (enabled, active, service, running) in states.items():
            if enabled not in OFF_ENABLED or active not in OFF_ACTIVE:
                on.append(f'{unit} ({scope[2:]}: {enabled}, {active})')
            elif running not in OFF_ACTIVE:
                on.append(f'{unit} ({scope[2:]}: its {service} is {running})')
    return on


def write_whole(path, lines):
    """Writes the file whole under `path`, or under <name>.2.txt, .3.txt ... when that name is taken: never over a
    file already there. Returns the path it wrote."""
    fd, new = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.')
    stem = path[:-len('.txt')]
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as f:
            f.writelines(line + '\n' for line in lines)
        for n in range(1, 1000):
            name = path if n == 1 else f'{stem}.{n}.txt'
            try:
                os.link(new, name)                  # fails when the name is taken: nothing is replaced
            except FileExistsError:
                continue
            return name
        raise OSError(errno.EEXIST, 'a thousand files of this day already')
    finally:
        try:
            os.unlink(new)
        except OSError:
            pass


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
    """Returns the exit code (the module's text): 0 done, 1 failed, 2 refused, 3 compare found "new only"."""
    mode = cfg.daily_mode
    if mode == 'run':
        try:
            on = old_timers_on(cfg, run, systemctl)
        except Unreadable as ex:
            say(f'fork-cleaner daily: refuses to delete: cannot tell whether the old timers are on ({ex})')
            return 2
        if on:
            say(f'fork-cleaner daily: refuses to delete while an old timer is on: {", ".join(on)}. Disable it '
                '(systemctl --user or --system disable --now <timer>, and wait for its service to end) or set '
                'cleaner.daily.mode to "compare"')
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
        path = write_whole(path, lines)
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
            path = write_whole(path, out)
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
    # A failure (1) wins over "new only" (3): exit 3 always means every step worked.
    final = 1 if any(c not in (0, NEW_ONLY) for c in codes) else NEW_ONLY if NEW_ONLY in codes else 0
    words = {0: 'done', 1: 'something failed', NEW_ONLY: 'done, the compare found new only paths'}[final]
    say(f'fork-cleaner daily ({mode}): ' + '; '.join(done) + f'; exit {final}: {words}')
    return final
