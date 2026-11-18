"""`fork-cleaner report`: one line for the daily report: how much of the disk each class holds on this host, and
what the cleaner freed since the last report.

It deletes nothing. The files it writes are its own, beside the log: the mark (`<logFile>.reported`), the last line
of the log it counted, so the next report counts only the lines after it; and the mark's lock
(`<logFile>.reported.lock`), so that two reports at once do not both count the same deletes.

The classes are config.CLASSES, the one list: a rule's class is the entries that rule looks at (rules.tree_of,
rules.tmp_match), a must-keep class is its group of must-keep paths. Sizes are blocks on disk, measured without
following a link and without entering another filesystem or a mount point (a bind mount of the same disk too);
each folder and each hard-linked file counts once in the whole report (in the first class that reaches it). A class
that cannot be read in full says "could not read" and the others still print; a worktree that cannot be read is
named, and the worktrees that can be still count. Docker's volumes are root's, so Docker's own measure is used.

With `--out` the line is written (and synced) before the mark moves: at least once, never lost. A report stopped
between the two (killed, a power cut) leaves the mark where it was, so the next report counts the same deletes
again, and two lines in the file then name overlapping "freed" figures. The mark's own line says from when.
"""
import errno, fcntl, fnmatch, json, os, re, socket, stat, tempfile, time

from . import rules
from .config import CLASSES
from .engine import count, log_problem
from .facts import Unreadable, docker_volume_sizes, read_mounts

GB = 1024 ** 3
LINE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$')
RULES = tuple(source[len('rule:'):] for _, source in CLASSES if isinstance(source, str))
# `deleted <what> (<size>, <rule>: <why>)`, the size as engine.human writes it.
DELETED = re.compile(r' \((\d+ B|\d+\.\d [KMG]B|size not known), (' + '|'.join(RULES) + r'): ')
UNITS = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}
# The other lines a run writes to the log (engine.py): understood, and not a delete.
OTHER_LINES = ('deleting ', 'compressing ', 'FAILED ', 'REFUSED ', 'total: ', 'stopped: ')
MARK_VERSION = 1


def gb(n):
    return f'{n / GB:.1f} GB'


class Sizer:
    """Sizes files and folders for one report. Never follows a link (a link is its own few bytes), never enters
    another filesystem or a mount point inside (another disk, or a bind mount that shows a folder of this disk
    somewhere else: it has the same device number, so only the mount list tells) and counts each folder and each
    file with more than one hard link once, across every class."""

    def __init__(self, mount_points=()):
        self.seen, self.mount_points = set(), frozenset(mount_points)

    def _new(self, st):
        """True when this entry has not been counted yet (a file with one link always is new)."""
        if not stat.S_ISDIR(st.st_mode) and st.st_nlink <= 1:
            return True
        key = (st.st_dev, st.st_ino)
        if key in self.seen:
            return False
        self.seen.add(key)
        return True

    def size(self, path):
        """(bytes, device, problems): what could not be read is listed in problems, never counted as nothing."""
        problems = []
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return 0, None, problems
        except OSError as ex:
            return 0, None, [f'{path}: {ex.strerror}']
        if not self._new(st):
            return 0, st.st_dev, problems
        total, dev = st.st_blocks * 512, st.st_dev
        stack = [path] if stat.S_ISDIR(st.st_mode) else []
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except FileNotFoundError:
                continue                # gone while it was measured: nothing there to count
            except OSError as ex:
                problems.append(f'{folder}: {ex.strerror}')
                continue
            for entry in entries:
                try:
                    est = entry.stat(follow_symlinks=False)
                except FileNotFoundError:
                    continue
                except OSError as ex:
                    problems.append(f'{entry.path}: {ex.strerror}')
                    continue
                if est.st_dev != dev or entry.path in self.mount_points or not self._new(est):
                    continue
                total += est.st_blocks * 512
                if stat.S_ISDIR(est.st_mode):
                    stack.append(entry.path)
        return total, dev, problems


class Sized:
    """One class of the report: bytes per device, what could not be read, or why it was not measured."""

    def __init__(self, name):
        self.name, self.by_dev, self.problems, self.note = name, {}, [], None
        self.unread = []        # entries it could not tell are this class's: named, and the rest still counts

    def add(self, sized):
        size, dev, problems = sized
        self.by_dev[dev] = self.by_dev.get(dev, 0) + size
        self.problems += problems

    @property
    def total(self):
        return sum(self.by_dev.values())


def _names(root, sized):
    """The names in a rule's folder; none when it is not there; a problem when it cannot be read."""
    try:
        return sorted(os.listdir(root))
    except (FileNotFoundError, NotADirectoryError):
        return []
    except OSError as ex:
        sized.problems.append(f'{root}: {ex.strerror}')
        return []


def _lstat(path):
    """The entry itself (a link is not followed); None when it is not there. Raises Unreadable when it cannot tell."""
    try:
        return os.lstat(path)
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError as ex:
        raise Unreadable(f'{path}: {ex.strerror}') from None


def expand(cfg, text):
    """The paths a must-keep pattern names on this host, reached without following a link (* within one part)."""
    found = ['/']
    for part in cfg._expand(text).split('/')[1:]:
        nxt = []
        for base in found:
            st = _lstat(base)
            if st is None or not stat.S_ISDIR(st.st_mode):
                continue                # a link is not followed, and a file holds nothing
            if not any(c in part for c in '*?['):
                nxt.append(os.path.join(base, part))
                continue
            try:
                names = os.listdir(base)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as ex:
                raise Unreadable(f'{base}: {ex.strerror}') from None
            nxt += [os.path.join(base, n) for n in sorted(names) if fnmatch.fnmatchcase(n, part)]
        found = nxt
    return [path for path in found if _lstat(path) is not None]


def measure(cfg, sizer, name, source, docker):
    """One class, measured. `docker` gives (sizes, folder) of Docker's volumes."""
    sized = Sized(name)
    if source is None:
        sized.note = 'no rule yet'
    elif source == 'rule:worktrees':
        for repo in cfg.repos:
            for entry in _names(repo.root, sized):
                path = os.path.join(repo.root, entry)
                try:
                    kind = rules.tree_of(repo, path)
                except Unreadable as ex:
                    sized.unread.append(f'{path} ({ex})')   # one broken worktree does not hide the others
                    continue
                if kind is not None:
                    sized.add(sizer.size(path))
    elif source == 'rule:tmp':
        if cfg.tmp_patterns:                # without a pattern the rule looks at nothing
            for entry in _names(cfg.tmp_root, sized):
                if rules.tmp_match(cfg, entry) is not None:
                    sized.add(sizer.size(os.path.join(cfg.tmp_root, entry)))
    elif source == 'rule:compress-cold':
        for folder in cfg.cold_folders:     # without folders the rule looks at nothing
            if os.path.lexists(folder.path):
                sized.add(sizer.size(folder.path))
    elif source == 'rule:docker':
        if not cfg.docker_enabled:
            sized.note = 'off'              # as in a plan: Docker is not even asked
        else:
            try:
                sizes, folder = docker()
                dev = os.stat(folder).st_dev if folder.startswith('/') else None
            except Unreadable as ex:
                sized.problems.append(str(ex))
            except OSError as ex:
                sized.problems.append(f'Docker\'s folder {folder}: {ex.strerror}')
            else:
                sized.add((sum(sizes.values()), dev, []))
    elif isinstance(source, tuple):
        for text in source:
            try:
                paths = expand(cfg, text)
            except Unreadable as ex:
                sized.problems.append(str(ex))
                continue
            for path in paths:
                sized.add(sizer.size(path))
    else:
        raise ValueError(f'class {name}: unknown source {source!r}')
    return sized


# --- what the cleaner freed ---------------------------------------------------------------------------------------
class Freed:
    def __init__(self):
        self.bytes, self.deletes, self.unknown, self.skipped = 0, 0, 0, 0
        self.compresses = 0     # files compress-cold replaced by a checked copy: what they freed is in bytes
        self.since, self.problem, self.mark, self.mark_problem = None, None, None, None
        self.trimmed = False    # the last report's line is gone: deletes before the oldest line left are not counted


def read_mark(cfg):
    """The last report's mark: {'at': time, 'line': the last log line it counted}; None when there is none. Raises
    Unreadable when there is one it cannot read or use."""
    try:
        fd = os.open(cfg.report_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as ex:
        raise Unreadable(f'{cfg.report_file}: {ex.strerror}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Unreadable(f'{cfg.report_file}: not a regular file')
        with os.fdopen(fd, 'rb') as f:
            fd = None
            text = f.read().decode('utf-8', 'surrogateescape')
    except OSError as ex:
        raise Unreadable(f'{cfg.report_file}: {ex.strerror}') from None
    finally:
        if fd is not None:
            os.close(fd)
    try:
        mark = json.loads(text)
    except ValueError:
        mark = None
    if not (isinstance(mark, dict) and mark.get('version') == MARK_VERSION
            and all(isinstance(mark.get(k), str) for k in ('at', 'line'))):
        raise Unreadable(f'{cfg.report_file}: not a mark this version understands')
    return mark


def write_mark(cfg, line):
    """Writes the mark anew (a new file put in place, never half written). Raises OSError when it cannot."""
    mark = {'version': MARK_VERSION, 'at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'line': line}
    fd, new = tempfile.mkstemp(dir=os.path.dirname(cfg.report_file), prefix='cleaner.reported.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(mark, f)
            f.write('\n')
        os.replace(new, cfg.report_file)
    except BaseException:
        try:
            os.unlink(new)
        except OSError:
            pass
        raise


def deleted_size(rest, verb='deleted'):
    """The size a `deleted` line (or a `compressed` line: what compressing freed) names: bytes, 'unknown' when it
    says "size not known", None when it is not a line of this version."""
    found = DELETED.search(rest, len(verb))
    if not found:
        return None
    if found.group(1) == 'size not known':
        return 'unknown'
    number, unit = found.group(1).split(' ')
    return int(float(number) * UNITS[unit])


def freed(cfg):
    """What the log says the cleaner deleted after the line the last report's mark names (all of the log when the
    mark is gone or its line was trimmed away: every line left is newer, but deletes trimmed away with that line were
    never counted, so the figure is then a floor). No log: nothing freed."""
    out = Freed()
    problem = log_problem(path=cfg.log_file)
    if problem is not None:
        out.problem = f'the log {cfg.log_file} is not the cleaner\'s ({problem})'
        return out
    try:
        fd = os.open(cfg.log_file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return out
    except OSError as ex:
        out.problem = f'the log {cfg.log_file}: {ex.strerror}'
        return out
    try:
        with os.fdopen(fd, 'rb') as f:
            data = f.read()
    except OSError as ex:
        out.problem = f'the log {cfg.log_file}: {ex.strerror}'
        return out
    # A run may be writing its last line right now: a line without its end is left for the next report.
    lines = data.decode('utf-8', 'surrogateescape').split('\n')[:-1]
    try:
        mark = read_mark(cfg)
    except Unreadable as ex:
        mark, out.mark_problem = None, str(ex)
    start = 0
    if mark is not None:
        out.since = f'the last report at {mark["at"]}'
        hits = [i for i, line in enumerate(lines) if line == mark['line']]
        start = hits[-1] + 1 if hits else 0
        out.trimmed = not hits
    elif lines:
        first = LINE.match(lines[0])
        out.since = f'the log began at {first.group(1)}' if first else 'the log began'
    for line in lines[start:]:
        found = LINE.match(line)
        rest = found.group(2) if found else ''
        verb = next((v for v in ('deleted', 'compressed') if rest.startswith(v + ' ')), None) if found else None
        if verb is not None:
            size = deleted_size(rest, verb)
            if size is None:
                out.skipped += 1
            else:
                if verb == 'compressed':
                    out.compresses += 1
                else:
                    out.deletes += 1
                if size == 'unknown':
                    out.unknown += 1
                else:
                    out.bytes += size
        elif not (found and rest.startswith(OTHER_LINES)):
            out.skipped += 1            # a line of an older (or newer) version, or no line of the cleaner's
    out.mark = lines[-1] if lines else None
    return out


# --- the line -----------------------------------------------------------------------------------------------------
def disk(cfg):
    """(used, size, device) of the filesystem the home folder is on: the disk "everything else" is measured on."""
    vfs = os.statvfs(cfg.home)
    return (vfs.f_blocks - vfs.f_bfree) * vfs.f_frsize, vfs.f_blocks * vfs.f_frsize, os.stat(cfg.home).st_dev


def _problems(problems):
    return problems[0] + (f', and {len(problems) - 1} more' if len(problems) > 1 else '')


def mount_points():
    """Every mount point of this mount namespace (bind mounts too)."""
    return read_mounts(proc=None).points


def take_lock(cfg):
    """The report's lock, held until its descriptor closes. None when the log's folder is not there: then there is
    no log and no mark to move. Raises BlockingIOError when another report holds it, OSError when it cannot be taken."""
    try:
        fd = os.open(cfg.report_lock_file, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except FileNotFoundError:
        return None
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BaseException:
        os.close(fd)
        raise
    return fd


def deliver(out, line):
    """Appends the line to the file `out` and waits until it is on disk, and the file's name too when this made the
    file (else a power cut could leave the mark moved and no file). Raises OSError when it cannot."""
    folder = os.path.dirname(out)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_APPEND | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC
    try:
        fd, made = os.open(out, flags), False
    except FileNotFoundError:
        fd, made = os.open(out, flags | os.O_CREAT | os.O_EXCL, 0o600), True
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        os.write(fd, (line + '\n').encode('utf-8', 'surrogateescape'))
        os.fsync(fd)
    finally:
        os.close(fd)
    if made:
        dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)


def report(cfg, say=print, docker=docker_volume_sizes, host=None, disk_of=disk, mounts=mount_points, out=None, mark=True):
    """Prints the line and moves the mark, one report at a time. Returns the exit code: 0, or 1 when anything
    could not be read (that part says so; the rest is still there) or another report is running (then it prints
    no line and the mark stays).
    `out`: a file the line is appended to (and synced) first; the mark moves only once the line is there, so a line
    that went nowhere is counted again by the next report (#491). `mark=False`: the mark stays where it is."""
    try:
        lock = take_lock(cfg)
    except BlockingIOError:
        say(f'fork-cleaner: another report is running (it holds {cfg.report_lock_file}): no report, the mark stays')
        return 1
    except OSError as ex:
        say(f'fork-cleaner: cannot take the report\'s lock {cfg.report_lock_file} ({ex.strerror}): no report')
        return 1
    try:
        return _report(cfg, say, docker, host, disk_of, mounts, lock is not None, out, mark)
    finally:
        if lock is not None:
            os.close(lock)


def _report(cfg, say, docker, host, disk_of, mounts, locked, out=None, mark=True):
    try:
        points, mount_problem = mounts(), None
    except Unreadable as ex:
        points, mount_problem = (), str(ex)
    sizer = Sizer(points)
    classes = [measure(cfg, sizer, name, source, docker) for name, source in CLASSES]
    try:
        used, size, home_dev = disk_of(cfg)
    except OSError as ex:
        used = size = home_dev = None
        disk_problem = f'{cfg.home}: {ex.strerror}'
    else:
        disk_problem = None
    parts = []
    for sized in classes:
        if sized.note is not None:
            parts.append(f'{sized.name} {sized.note}')
        elif sized.problems:
            parts.append(f'{sized.name} could not read ({_problems(sized.problems)})')
        else:
            elsewhere = home_dev is not None and any(dev != home_dev for dev, n in sized.by_dev.items() if n)
            text = f'{sized.name} {gb(sized.total)}' + (' (on another disk)' if elsewhere else '')
            if sized.unread:
                text += f' (could not read {count(len(sized.unread), "worktree")}: {_problems(sized.unread)})'
            parts.append(text)
    if disk_problem is None:
        counted = sum(s.by_dev.get(home_dev, 0) for s in classes)
        unread = any(s.problems or s.unread for s in classes)
        parts.append(f'everything else {gb(max(used - counted, 0))}' + (' (with what could not be read)' if unread else '')
                     + f' ({gb(used)} of {gb(size)} used)')
    else:
        parts.append(f'everything else could not read ({disk_problem})')

    got = freed(cfg)
    if got.problem is not None:
        tail = f'freed could not read ({got.problem})'
    elif got.since is None:
        tail = 'freed 0.0 GB (no delete in the log yet)'
    else:
        counted = f'{got.deletes} delete' + ('' if got.deletes == 1 else 's')
        if got.compresses:
            counted += f', {got.compresses} compressed file' + ('' if got.compresses == 1 else 's')
        if got.unknown:
            counted += f', {got.unknown} of unknown size'
        tail = f'freed since {got.since} ' + ('at least ' if got.trimmed else '') + f'{gb(got.bytes)} ({counted})'
        if got.trimmed:
            tail += '; log trimmed since the last report'
        if got.mark_problem is not None:
            tail += f' (the last report\'s mark could not be read: {got.mark_problem})'
    if got.skipped:
        tail += f'; {got.skipped} log line' + ('' if got.skipped == 1 else 's') + ' not understood, skipped'
    if mount_problem is not None:
        tail += f'; the mount list could not be read ({mount_problem}): a bind mount may be counted twice'
    line = f'disk on {host or socket.gethostname()}: ' + ', '.join(parts) + '; ' + tail
    if out is not None:
        try:
            deliver(out, line)
        except OSError as ex:
            say(line)
            say(f'fork-cleaner: could not write the line to {out} ({ex.strerror}): the mark stays, the next report counts these deletes again')
            return 1
    say(line)

    failed = (any(s.problems or s.unread for s in classes) or disk_problem or got.problem or got.mark_problem
              or mount_problem)
    if mark and got.problem is None and got.mark is not None and locked:
        try:
            write_mark(cfg, got.mark)
        except OSError as ex:
            say(f'fork-cleaner: could not write the mark {cfg.report_file} ({ex.strerror}): the next report counts these deletes again')
            failed = True
    return 1 if failed else 0
