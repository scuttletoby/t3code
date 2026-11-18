"""`fork-cleaner report`: one line for the daily report: how much of the disk each class holds on this host, and
what the cleaner freed since the last report.

It deletes nothing. The one file it writes is its own mark beside the log (`<logFile>.reported`): the last line of
the log it counted, so the next report counts only the lines after it.

The classes are config.CLASSES, the one list: a rule's class is the entries that rule looks at (rules.tree_of,
rules.tmp_match), a must-keep class is its group of must-keep paths. Sizes are blocks on disk, measured without
following a link and without crossing into another filesystem; each folder and each hard-linked file counts once
in the whole report (in the first class that reaches it). A class that cannot be read in full says "could not
read" and the others still print. Docker's volumes are root's, so Docker's own measure is used for them.
"""
import fnmatch, json, os, re, socket, stat, tempfile, time

from . import rules
from .config import CLASSES
from .engine import log_problem
from .facts import Unreadable, docker_volume_sizes

GB = 1024 ** 3
LINE = re.compile(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) (.*)$')
RULES = tuple(source[len('rule:'):] for _, source in CLASSES if isinstance(source, str))
# `deleted <what> (<size>, <rule>: <why>)`, the size as engine.human writes it.
DELETED = re.compile(r' \((\d+ B|\d+\.\d [KMG]B|size not known), (' + '|'.join(RULES) + r'): ')
UNITS = {'B': 1, 'KB': 1024, 'MB': 1024 ** 2, 'GB': 1024 ** 3}
# The other lines a run writes to the log (engine.py): understood, and not a delete.
OTHER_LINES = ('deleting ', 'FAILED ', 'REFUSED ', 'total: ', 'stopped: ')
MARK_VERSION = 1


def gb(n):
    return f'{n / GB:.1f} GB'


class Sizer:
    """Sizes files and folders for one report. Never follows a link (a link is its own few bytes), never enters
    another filesystem (a mount point inside is not this class's disk) and counts each folder and each file with
    more than one hard link once, across every class."""

    def __init__(self):
        self.seen = set()

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
                if est.st_dev != dev or not self._new(est):
                    continue
                total += est.st_blocks * 512
                if stat.S_ISDIR(est.st_mode):
                    stack.append(entry.path)
        return total, dev, problems


class Sized:
    """One class of the report: bytes per device, what could not be read, or why it was not measured."""

    def __init__(self, name):
        self.name, self.by_dev, self.problems, self.note = name, {}, [], None

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
                    sized.problems.append(f'{path}: {ex}')
                    continue
                if kind is not None:
                    sized.add(sizer.size(path))
    elif source == 'rule:tmp':
        if cfg.tmp_patterns:                # without a pattern the rule looks at nothing
            for entry in _names(cfg.tmp_root, sized):
                if rules.tmp_match(cfg, entry) is not None:
                    sized.add(sizer.size(os.path.join(cfg.tmp_root, entry)))
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
        self.since, self.problem, self.mark, self.mark_problem = None, None, None, None


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


def deleted_size(rest):
    """The size a `deleted` line names: bytes, 'unknown' when it says "size not known", None when it is not a
    line of this version."""
    found = DELETED.search(rest, len('deleted'))
    if not found:
        return None
    if found.group(1) == 'size not known':
        return 'unknown'
    number, unit = found.group(1).split(' ')
    return int(float(number) * UNITS[unit])


def freed(cfg):
    """What the log says the cleaner deleted after the line the last report's mark names (all of the log when the
    mark is gone or its line was trimmed away: every line left is newer). No log: nothing freed."""
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
    elif lines:
        first = LINE.match(lines[0])
        out.since = f'the log began at {first.group(1)}' if first else 'the log began'
    for line in lines[start:]:
        found = LINE.match(line)
        rest = found.group(2) if found else ''
        if found and rest.startswith('deleted '):
            size = deleted_size(rest)
            if size is None:
                out.skipped += 1
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


def report(cfg, say=print, docker=docker_volume_sizes, host=None, disk_of=disk):
    """Prints the line and moves the mark. Returns the exit code: 0, or 1 when anything could not be read (that
    part says so; the rest is still there)."""
    sizer = Sizer()
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
            parts.append(f'{sized.name} {gb(sized.total)}' + (' (on another disk)' if elsewhere else ''))
    if disk_problem is None:
        counted = sum(s.by_dev.get(home_dev, 0) for s in classes)
        unread = any(s.problems for s in classes)
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
        if got.unknown:
            counted += f', {got.unknown} of unknown size'
        tail = f'freed since {got.since} {gb(got.bytes)} ({counted})'
        if got.mark_problem is not None:
            tail += f' (the last report\'s mark could not be read: {got.mark_problem})'
    if got.skipped:
        tail += f'; {got.skipped} log line' + ('' if got.skipped == 1 else 's') + ' not understood, skipped'
    say(f'disk on {host or socket.gethostname()}: ' + ', '.join(parts) + '; ' + tail)

    failed = any(s.problems for s in classes) or disk_problem or got.problem or got.mark_problem
    if got.problem is None and got.mark is not None:
        try:
            write_mark(cfg, got.mark)
        except OSError as ex:
            say(f'fork-cleaner: could not write the mark {cfg.report_file} ({ex.strerror}): the next report counts these deletes again')
            failed = True
    return 1 if failed else 0
