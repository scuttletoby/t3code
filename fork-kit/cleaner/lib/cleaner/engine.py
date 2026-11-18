"""`fork-cleaner plan` and `fork-cleaner run`.

`plan` prints what `run` would delete and what it keeps, and changes nothing: no file, no log, no lock. `run`
takes the lock, plans, reads everything about each item once more and only then deletes it, with one log line per
delete. What could not be read is kept, and makes the command end with exit code 1.
"""
import fcntl, os, tempfile, time

import machine_file

from . import config, rules
from .facts import Facts, Unreadable

USAGE = 'usage: fork-cleaner plan | fork-cleaner run   (fork-cleaner --help says more)'
HELP = '''fork-cleaner plan   print what a run would delete and what it keeps; changes nothing
fork-cleaner run    delete it, one log line per delete

What may go is decided by three rules (worktrees, docker, tmp) and the "cleaner" section of the machine file.
What must stay is decided by the code and cannot be turned off. Exit code: 0 done, 1 something could not be read or
deleted (it was kept), 2 wrong command or no usable machine file (nothing was done).'''
NEW = os.O_CREAT | os.O_NOFOLLOW        # the lock and the log are made when missing, and are never reached by a link


class Busy(Exception):
    """Another run holds the lock."""


class LogError(Exception):
    """The log could not be written: the run stops, because no delete may go unlogged."""


def human(n):
    if n is None:
        return 'size not known'
    if n < 1024:
        return f'{n} B'
    for unit in ('KB', 'MB', 'GB'):
        n /= 1024
        if n < 1024:
            break
    return f'{n:.1f} {unit}'


def sized(things):
    """The sizes of items or kept entries, added up; Docker does not tell the size of a volume."""
    known = [t.size for t in things if t.size is not None]
    if len(known) == len(things):
        return human(sum(known))
    return f'{human(sum(known))} and {len(things) - len(known)} of unknown size' if known else human(None)


def count(n, word):
    return f'{n} {word}' if n == 1 else f'{n} {word}s'


def one_line(text):
    return text.replace('\\', '\\\\').replace('\n', '\\n').replace('\r', '\\r') if ('\n' in text or '\r' in text) else text


class Log:
    """The lock that makes `run` one at a time, and the log it appends to. Both are private to the user."""

    def __init__(self, cfg):
        self.cfg = cfg
        os.makedirs(os.path.dirname(cfg.log_file), mode=0o700, exist_ok=True)
        self.lock = os.open(cfg.lock_file, os.O_RDWR | NEW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = os.open(cfg.log_file, os.O_RDWR | os.O_APPEND | NEW, 0o600)
        except BlockingIOError:
            os.close(self.lock)
            raise Busy() from None
        except OSError:
            os.close(self.lock)
            raise

    def write(self, line):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        try:
            os.write(self.fd, f'{stamp} {line}\n'.encode('utf-8', 'surrogateescape'))
        except OSError as ex:
            raise LogError(ex.strerror) from None

    def close(self):
        """Keeps the newest lines when the log grew past its limit, then lets the lock go."""
        try:
            os.lseek(self.fd, 0, os.SEEK_SET)
            with os.fdopen(self.fd, 'rb') as f:
                lines = f.read().splitlines(keepends=True)
            if len(lines) > self.cfg.log_lines:
                fd, new = tempfile.mkstemp(dir=os.path.dirname(self.cfg.log_file), prefix='cleaner.log.')
                with os.fdopen(fd, 'wb') as f:
                    f.writelines(lines[-self.cfg.log_lines:])
                os.replace(new, self.cfg.log_file)
        except OSError:
            pass                        # a log that could not be trimmed is still a log
        finally:
            os.close(self.lock)


def delete_all(cfg, facts, items, really, out):
    """Goes through the plan's items; returns how many deletes failed. Each item gets `done`: deleted, refused or
    failed (None in a plan)."""
    failed = 0
    for item in sorted(items, key=lambda i: i.label):
        told = f'{item.label} ({human(item.size)}, {item.rule}: {item.why})'
        if not really:
            out(f'would delete {told}')
            continue
        refusal = rules.recheck(cfg, facts, item)
        if refusal is not None:
            item.done, item.refusal = 'refused', refusal
            out(f'REFUSED {item.label} ({item.rule}: {refusal.code}{": " + refusal.detail if refusal.detail else ""})')
            continue
        try:
            rules.delete(item)
        except (OSError, Unreadable) as ex:
            failed += 1
            item.done = 'failed'
            out(f'FAILED {item.label} ({item.rule}: {ex})')
            continue
        item.done = 'deleted'
        out(f'deleted {item.label} ({human(item.size)}, {item.rule}: {item.why})')
    return failed


def _groups(things, key):
    groups = {}
    for thing in things:
        groups.setdefault(key(thing), []).append(thing)
    return sorted(groups.items())


def _execute(cfg, facts, really, say, log):
    def out(line):
        line = one_line(line)
        say(line)
        if log is not None:
            log.write(line)

    plan = rules.plan(cfg, facts, measure=not really)
    for kept in sorted(plan.kept, key=lambda k: (k.rule, k.path)):
        shown = f'docker volume {kept.path}' if kept.rule == 'docker' and kept.path != 'docker' else kept.path
        say(one_line(f'kept {shown} ({kept.rule}: {kept.code}{": " + kept.detail if kept.detail else ""})'))
    failed = delete_all(cfg, facts, plan.items, really, out)

    refused = [item for item in plan.items if item.done == 'refused']
    gone = [item for item in plan.items if item.done == ('deleted' if really else None)]
    verb = 'deleted' if really else 'would delete'
    for (rule, kind), group in _groups(gone, lambda i: (i.rule, i.kind)):
        say(f'  {rule}: {verb} {count(len(group), kind)} ({sized(group)})')
    for (rule, kind), group in _groups([i for i in plan.items if i.done == 'failed'], lambda i: (i.rule, i.kind)):
        say(f'  {rule}: failed {count(len(group), kind)} ({sized(group)})')
    for (rule, code), group in _groups(plan.kept, lambda k: (k.rule, k.code)):
        say(f'  {rule}: kept {len(group)} ({sized(group)}): {code}')
    for (rule, code), group in _groups(refused, lambda i: (i.rule, i.refusal.code)):
        say(f'  {rule}: kept {len(group)} ({sized(group)}): {code} (at the last check before the delete)')
    kept = len(plan.kept) + len(refused)
    out(f'total: {verb} {len(gone)} ({sized(gone)}), kept {kept}' + (f', failed {failed}' if really else ''))

    unread = any(k.code == 'could not read' for k in plan.kept)
    unread = unread or any(item.refusal.code == 'could not read' for item in refused)
    return 1 if unread or failed else 0


def execute(cfg, facts, really, say=print):
    """One plan (really=False) or one run. Returns the exit code."""
    try:
        log = Log(cfg) if really else None
    except Busy:
        say(f'fork-cleaner: another run holds {cfg.lock_file}: nothing deleted')
        return 1
    except OSError as ex:
        say(f'fork-cleaner: cannot open the log {cfg.log_file} or its lock ({ex.strerror}): nothing deleted')
        return 1
    try:
        return _execute(cfg, facts, really, say, log)
    except LogError as ex:
        say(f'fork-cleaner: stopped, the log {cfg.log_file} cannot be written ({ex})')
        return 1
    finally:
        if log is not None:
            log.close()


def main(argv, say=print):
    if argv == ['--help']:
        say(HELP)
        return 0
    if argv not in (['plan'], ['run']):
        say(USAGE)
        return 2
    try:
        cfg = config.load()
    except machine_file.MachineFileError as ex:
        say(f'fork-cleaner: {ex}')
        return 2  # config
    return execute(cfg, Facts(cfg), argv == ['run'], say)
