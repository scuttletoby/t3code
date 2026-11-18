"""`fork-cleaner plan` and `fork-cleaner run`.

`plan` prints what `run` would delete and what it keeps, and changes nothing: no file, no log, no lock. `run`
takes the lock, plans, and for each item reads everything about it once more (the process list, mounts, units,
pull request lists and Docker's list are read anew), then writes "deleting X" to the log and to the journal of
deletes in progress, deletes it, and writes "deleted X" (or FAILED). What could not be read is kept, and makes the
command end with exit code 1.

The journal (`<logFile>.deleting`) names each delete that started and has not ended. A run that was killed, or a
delete that failed part-way, leaves its line there; the next plan or run names that path "half deleted", never
judges or deletes it again, and says what to do. A delete git refused before it removed anything leaves no line.
"""
import fcntl, json, os, re, stat, tempfile, time

import machine_file

from . import config, rules
from .facts import Facts, Unreadable, Untouched

USAGE = 'usage: fork-cleaner plan | fork-cleaner run   (fork-cleaner --help says more)'
HELP = '''fork-cleaner plan   print what a run would delete and what it keeps; changes nothing
fork-cleaner run    delete it, one log line per delete

What may go is decided by three rules (worktrees, docker, tmp) and the "cleaner" section of the machine file.
What must stay is decided by the code and cannot be turned off. Exit code: 0 done, 1 something could not be read or
deleted (it was kept), 2 wrong command or no usable machine file (nothing was done).'''
NEW = os.O_CREAT | os.O_NOFOLLOW        # the lock and the log are made when missing, and are never reached by a link
STAMP = re.compile(rb'^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ ')   # how every line of the cleaner's log starts
HALF = ('an earlier run stopped while deleting it, so it may be partly gone; nothing judges it again until you '
        'remove it by hand, move it away, or (to have it judged again) delete its line from {journal}')


class Busy(Exception):
    """Another run holds the lock."""


class LogError(Exception):
    """The log or the journal could not be written: the run stops, because no delete may go unlogged."""


class ForeignLog(Exception):
    """`logFile` names a file the cleaner did not make: it is never written to or cut down."""


def log_problem(fd=None, path=None):
    """Why the file at `fd` (or `path`) is not a log of the cleaner's; None when it is, or when it is not there yet
    (path) or empty. The cleaner's log is a regular file of the user's own whose first line starts with a time."""
    try:
        st = os.fstat(fd) if fd is not None else os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as ex:
        return ex.strerror
    if not stat.S_ISREG(st.st_mode):
        return 'not a regular file'
    if st.st_uid != os.getuid():
        return 'another owner'
    if st.st_size == 0:
        return None
    try:
        if fd is not None:
            first = os.pread(fd, 32, 0)
        else:
            rd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            try:
                first = os.read(rd, 32)
            finally:
                os.close(rd)
    except OSError as ex:
        return ex.strerror
    return None if STAMP.match(first) else 'its first line is not a line of the cleaner\'s log'


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
        problem = log_problem(path=cfg.log_file)        # before anything is made next to a file that is not the log
        if problem is not None:
            raise ForeignLog(problem)
        os.makedirs(os.path.dirname(cfg.log_file), mode=0o700, exist_ok=True)
        self.lock = os.open(cfg.lock_file, os.O_RDWR | NEW, 0o600)
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            self.fd = os.open(cfg.log_file, os.O_RDWR | os.O_APPEND | NEW | os.O_NONBLOCK, 0o600)
        except BlockingIOError:
            os.close(self.lock)
            raise Busy() from None
        except OSError:
            os.close(self.lock)
            raise
        problem = log_problem(fd=self.fd)
        if problem is not None:
            os.close(self.fd)
            os.close(self.lock)
            raise ForeignLog(problem)

    def write(self, line):
        stamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())
        try:
            os.write(self.fd, f'{stamp} {line}\n'.encode('utf-8', 'surrogateescape'))
        except OSError as ex:
            raise LogError(f'the log {self.cfg.log_file}: {ex.strerror}') from None

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


# --- the journal of deletes in progress ---------------------------------------------------------------------------
def read_journal(cfg):
    """The deletes an earlier run started and did not end, as dicts (kind, path, rule, at). Raises Unreadable when
    the journal is there but cannot be read in full."""
    try:
        fd = os.open(cfg.journal_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return []
    except OSError as ex:
        raise Unreadable(f'{cfg.journal_file}: {ex.strerror}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unreadable(f'{cfg.journal_file}: not a regular file')
        with os.fdopen(fd, 'rb') as f:
            fd = None
            text = f.read().decode('utf-8', 'surrogateescape')
    except OSError as ex:
        raise Unreadable(f'{cfg.journal_file}: {ex.strerror}') from None
    finally:
        if fd is not None:
            os.close(fd)
    entries = []
    for line in text.splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            entry = None
        if not (isinstance(entry, dict) and all(isinstance(entry.get(k), str) for k in ('kind', 'path', 'rule'))):
            raise Unreadable(f'{cfg.journal_file}: a line it cannot use: {line[:80]!r}')
        entries.append(entry)
    return entries


def write_journal(cfg, entries):
    """Writes the whole journal anew (a new file put in place, so it is never half written); no file when empty.
    Raises LogError when it cannot."""
    try:
        if not entries:
            try:
                os.unlink(cfg.journal_file)
            except FileNotFoundError:
                pass
            return
        fd, new = tempfile.mkstemp(dir=os.path.dirname(cfg.journal_file), prefix='cleaner.journal.')
        try:
            with os.fdopen(fd, 'w', encoding='utf-8', errors='surrogateescape') as f:
                f.writelines(json.dumps(entry) + '\n' for entry in entries)
                f.flush()
                os.fsync(f.fileno())
            os.replace(new, cfg.journal_file)
        except BaseException:
            try:
                os.unlink(new)
            except OSError:
                pass
            raise
    except OSError as ex:
        raise LogError(f'the journal {cfg.journal_file}: {ex.strerror}') from None


def half_deleted(cfg, entries):
    """(kept, left): a Kept for each path a delete started on that is still there, and the journal lines still
    true. A volume, or a path that is gone, has nothing left to name."""
    kept, left = [], []
    for entry in entries:
        if entry['kind'] == 'volume' or not os.path.lexists(entry['path']):
            continue
        left.append(entry)
        kept.append(rules.Kept(entry['path'], entry['rule'], 'half deleted',
                               f'since {entry.get("at", "?")}: ' + HALF.format(journal=cfg.journal_file)))
    return kept, left


# --- deleting -----------------------------------------------------------------------------------------------------
def delete_all(cfg, facts, items, really, out, log=None, journal=None):
    """Goes through the plan's items; returns how many deletes failed. Each item gets `done`: deleted, refused or
    failed (None in a plan). `journal` is the list of journal lines to keep (a run only)."""
    failed = 0
    for item in sorted(items, key=lambda i: i.label):
        told = f'{item.label} ({human(item.size)}, {item.rule}: {item.why})'
        if not really:
            out(f'would delete {told}')
            continue
        facts.forget()                  # the last check reads the machine anew, not what the plan saw
        refusal = rules.recheck(cfg, facts, item)
        if refusal is not None:
            item.done, item.refusal = 'refused', refusal
            out(f'REFUSED {item.label} ({item.rule}: {refusal.code}{": " + refusal.detail if refusal.detail else ""})')
            continue
        log.write(one_line(f'deleting {told}'))           # a failed write stops the run before the delete
        entry = {'kind': item.kind, 'path': item.path, 'rule': item.rule,
                 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}
        journal.append(entry)
        write_journal(cfg, journal)
        try:
            rules.delete(item)
        except (OSError, Unreadable) as ex:
            failed += 1
            item.done = 'failed'
            if isinstance(ex, Untouched) or (item.kind != 'volume' and not os.path.lexists(item.path)):
                journal.remove(entry)   # nothing was removed, or all of it was: nothing is half gone
                write_journal(cfg, journal)
            out(f'FAILED {item.label} ({item.rule}: {ex})' + (' (half deleted: see the next plan)' if entry in journal else ''))
            continue
        journal.remove(entry)
        write_journal(cfg, journal)
        item.done = 'deleted'
        out(f'deleted {told}')
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

    try:
        entries = read_journal(cfg)
    except Unreadable as ex:
        if really:
            out(f'stopped: the journal of deletes in progress cannot be read ({ex}): nothing deleted')
            return 1
        say(one_line(f'kept everything an earlier run may have half deleted (could not read: {ex})'))
        entries, unread_journal = [], True
    else:
        unread_journal = False
    half, journal = half_deleted(cfg, entries)
    if really and journal != entries:
        write_journal(cfg, journal)
    plan = rules.plan(cfg, facts, measure=not really)
    if unread_journal:              # a plan only: anything may be half deleted, so nothing would be deleted
        plan.kept += [rules.Kept(item.path, item.rule, 'could not read', 'the journal of deletes in progress cannot '
                                 'be read, and it may name this as half deleted', item.size)
                      for item in plan.items if item.kind != 'volume']
        plan.items = [item for item in plan.items if item.kind == 'volume']
    halves = {kept.path for kept in half}
    plan.items = [item for item in plan.items if item.kind == 'volume' or item.path not in halves]
    plan.kept = [kept for kept in plan.kept if kept.rule == 'docker' or kept.path not in halves] + half
    for kept in sorted(plan.kept, key=lambda k: (k.rule, k.path)):
        shown = f'docker volume {kept.path}' if kept.rule == 'docker' and kept.path != 'docker' else kept.path
        say(one_line(f'kept {shown} ({kept.rule}: {kept.code}{": " + kept.detail if kept.detail else ""})'))
    failed = delete_all(cfg, facts, plan.items, really, out, log, journal)

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

    unread = unread_journal or any(k.code in ('could not read', 'half deleted') for k in plan.kept)
    unread = unread or any(item.refusal.code == 'could not read' for item in refused)
    return 1 if unread or failed else 0


def execute(cfg, facts, really, say=print):
    """One plan (really=False) or one run. Returns the exit code."""
    if not really:
        problem = log_problem(path=cfg.log_file)
        if problem is not None:
            say(f'fork-cleaner: logFile {cfg.log_file} is not the cleaner\'s log ({problem}): a run would refuse it')
            return 2  # log
    try:
        log = Log(cfg) if really else None
    except Busy:
        say(f'fork-cleaner: another run holds {cfg.lock_file}: nothing deleted')
        return 1
    except ForeignLog as ex:
        say(f'fork-cleaner: logFile {cfg.log_file} is not the cleaner\'s log ({ex}): nothing deleted, nothing written')
        return 2
    except OSError as ex:
        say(f'fork-cleaner: cannot open the log {cfg.log_file} or its lock ({ex.strerror}): nothing deleted')
        return 1
    try:
        return _execute(cfg, facts, really, say, log)
    except LogError as ex:
        say(f'fork-cleaner: stopped, cannot write {ex}: nothing more deleted')
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
    if os.geteuid() == 0:
        # As root `~` is root's home, so the must-keep paths would not name the user's agent folders.
        say('fork-cleaner: refuses to run as root; run it as the user whose files it cleans')
        return 2  # root
    try:
        cfg = config.load()
    except machine_file.MachineFileError as ex:
        say(f'fork-cleaner: {ex}')
        return 2  # config
    return execute(cfg, Facts(cfg), argv == ['run'], say)
