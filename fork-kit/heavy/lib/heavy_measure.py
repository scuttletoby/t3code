"""heavy's measured history and the capacity front: size each start by what its kind of job was seen to need.

  heavy --measure <kind> [--] <command> [args...]     run as usual; record the job's memory under <kind>
  heavy capacity --kinds FILE --check PROGRAM [--state-dir DIR] [--] [<kind> | <MB>] [args...]
  heavy capacity --kinds FILE --show [--json] [--state-dir DIR] [<kind>...]

Recording: a job run with --measure (or queued with --kind, unless --measure names another kind) has its scope
looked at every second while it runs (every 0.2 s until the scope is seen). Each look reads memory.current,
memory.peak (the kernel's high-water mark since the scope began) and `used` (memory.current less file cache the
kernel can drop, at least its anon, plus its swap). The job's `need` is the highest over all looks of
used + (memory.peak - memory.current): a spike between two looks still counts, less only the droppable file cache
that grew after it. The scope's files are gone (ENODEV) as soon as its last process ends, so growth after the last
look (under a second) is missed. When the command ends one JSON line goes to ~/.local/state/heavy/measured.jsonl
(the user's home from the password database; HEAVY_STATE_DIR names another folder for --measure, for tests): the
kind, usedMb, peakMb, needMb, how long it ran, how it ended (exited or stopped) and its exit code. Lines are
appended with one write each (two jobs that end together keep both lines); past 256 KiB the file is rotated to
measured.jsonl.1 (at most two files). A nested heavy and HEAVY_KEEP=1 record nothing.

capacity: the front of the machine's capacity check. The check program gets the caller's arguments unchanged,
except that a first argument that is a kind name becomes the number of MB that kind needs:

  no argument, an empty one, or a whole number    passed on unchanged (every caller of today keeps working)
  a kind of the kinds file          the highest `measure` figure of its last recentRuns runs (stopped runs too: a
                                    run that was stopped needed at least that) x (1 + marginPercent/100), rounded
                                    up to roundMb; never below the kind's minMb (default: its fallbackMb), never
                                    below its fallbackMb while it has fewer than minRuns runs, at most its maxMb
                                    (default 2 x fallbackMb)
  any other word                    refused (exit 2): a typo must not reach a check that would read it as 0

The front reads the history of the user that runs it (or --state-dir), never HEAVY_STATE_DIR: a test setting that
leaks into an agent's shell must not size real starts. A history that cannot be read, or whose lines are mostly
not records, is not trusted: every kind gets its fallbackMb. A kinds file that cannot be read or has a wrong key is
refused (exit 2), so a caller's `capacity <kind> && start` never starts on a broken config. The check runs with
HEAVY_CAPACITY_KIND (the kind, or empty) and HEAVY_CAPACITY_FRONT=1; a check that calls the front again is refused
(exit 2). heavy's exit code is the check's.

The kinds file is the project's: its own file, or the "capacity" object of a larger project config. Standard
library only.
"""
import datetime, errno, fcntl, json, math, os, pwd, re, stat, sys, time

VERSION = 1
MB = 1 << 20
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}\Z')
WHOLE_RE = re.compile(r'^[0-9]+\Z')
HISTORY = 'measured.jsonl'
LOCK = 'measured.lock'
MAX_BYTES = 256 << 10           # rotate past this; at most this file and one rotated file are kept
READ_MAX = 1 << 20              # a reader reads at most the last MiB of each file
MAX_LINE = 4000                 # one record is far smaller; one write of it is never split by another writer
SAMPLE_SECONDS = 1              # look at the scope this often while the job runs (two small reads)
FIRST_LOOK = 0.2                # until the scope's memory.peak is open, look this often (a short job is caught)
FIRST_TRIES = 50                # ... at most this many times (10 s), then every SAMPLE_SECONDS
CORRUPT_SHARE = 10              # more than one bad line and more than 1 in this many: the history is not trusted
READ_TRIES = 5                  # a read that meets a rotation half-way starts again, at most this many times
MAX_RECENT = 1000               # recentRuns at most this (the window stays small; two files hold about 2500 lines)
FRONT_ENV = 'HEAVY_CAPACITY_FRONT'
EXIT_USAGE = 2

KINDS_TOP = {
    'version': (int, None),
    'marginPercent': ((int, float), 20),
    'minRuns': (int, 5),
    'recentRuns': (int, 50),
    'roundMb': (int, 128),
    'measure': (str, 'need'),
    'kinds': (dict, None),
}
# minMb: the kind's floor (default its fallbackMb). agent: the ask starts an agent, whose own memory heavy never
# sees, so the floor is the fallbackMb itself. measured: false for a kind that nothing runs under heavy.
KIND_KEYS = {'fallbackMb': (int, None), 'minMb': (int, None), 'maxMb': (int, None), 'agent': (bool, False),
             'measured': (bool, True), 'note': (str, None)}
MEASURES = ('need', 'used', 'peak')


class Refused(Exception):
    pass


def say(msg):
    try:
        sys.stderr.write(f'heavy: {msg}\n')
        sys.stderr.flush()
    except OSError:
        pass


def home():
    """The user's home from the password database ($HOME is the caller's environment)."""
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except KeyError:
        return os.path.expanduser('~')


def default_dir():
    return os.path.join(home(), '.local', 'state', 'heavy')


def state_dir():
    """Where --measure records: HEAVY_STATE_DIR (tests: never the user's real history), else default_dir(). The
    capacity front never uses this: it reads default_dir() or its own --state-dir."""
    d = os.environ.get('HEAVY_STATE_DIR')
    if d:
        if not os.path.isabs(d):
            raise Refused(f'HEAVY_STATE_DIR {d!r} is not an absolute path')
        return d
    return default_dir()


def check_kind(kind):
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise Refused(f'bad kind {kind!r} (lower-case letters, digits, _ . -; at most 40)')
    return kind


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------- the history ----------

def append(folder, record):
    """Append one record as one line with one write (O_APPEND: lines of two writers never mix). A file that does not
    end in a newline (a writer cut off half-way) gets one first, so the cut line costs only itself. Returns None, or
    a note when the line was written but the file could not be rotated; raises when the line was not written."""
    line = json.dumps(record, sort_keys=True, separators=(',', ':')).encode() + b'\n'
    if len(line) > MAX_LINE:
        raise ValueError('record too long')
    os.makedirs(folder, mode=0o700, exist_ok=True)
    path = os.path.join(folder, HISTORY)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f'{path} is not a plain file')
        if st.st_size and _last_byte(path) not in (b'\n', None):
            line = b'\n' + line
        if os.write(fd, line) != len(line):
            raise OSError(errno.EIO, f'short write to {path}')
        size = st.st_size + len(line)
    finally:
        os.close(fd)
    if size > MAX_BYTES:
        try:
            rotate(folder)
        except OSError as e:
            return f'recorded, but {path} could not be rotated ({e.strerror or e})'
    return None


def _last_byte(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        size = os.fstat(fd).st_size
        return os.pread(fd, 1, size - 1) if size else None
    finally:
        os.close(fd)


def rotate(folder):
    """measured.jsonl -> measured.jsonl.1 (the older one goes). One rotator at a time (a lock nobody waits for); a
    writer that still has the old file open appends to the rotated one, so its line is kept. A lock file that cannot
    be opened (a folder in its place, no permission) does not stop the rotation: the history stays bounded, and two
    rotators at once can at worst drop the older file's runs (fewer runs: never a smaller ask than the floor)."""
    path = os.path.join(folder, HISTORY)
    try:
        fd = os.open(os.path.join(folder, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except OSError:
        fd = None
    try:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return                      # another writer rotates now
        _rotate_if_big(path)
    finally:
        if fd is not None:
            os.close(fd)


def _rotate_if_big(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISREG(st.st_mode) and st.st_size > MAX_BYTES:     # not rotated by someone just before
        os.replace(path, path + '.1')


def _open_ro(path):
    """A read-only fd of a plain file, None when it is not there. Raises OSError when it is there but cannot be read
    (a folder, a link, no permission)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise OSError(errno.EINVAL, f'{path} is not a plain file')
    return fd


def _tail(fd):
    """The last READ_MAX bytes of an open file (from a line start)."""
    if fd is None:
        return b''
    size = os.fstat(fd).st_size
    start = max(0, size - READ_MAX)
    data = os.pread(fd, size - start, start)
    if start:
        data = data.partition(b'\n')[2]     # the first line was cut by the window: not a bad line
    return data


def _read_both(path):
    """The rotated file and the current one, as one consistent view: both are opened (the current one first) and
    the current name must still be the file opened, else a rotation came in between and the read starts again
    (without this a reader could see the same file twice and miss the older one). None after READ_TRIES."""
    for _ in range(READ_TRIES):
        cur = old = None
        try:
            cur = _open_ro(path)
            old = _open_ro(path + '.1')
            if cur is not None:
                try:
                    same = os.lstat(path).st_ino == os.fstat(cur).st_ino
                except FileNotFoundError:
                    same = False
                if not same:
                    continue
            return _tail(old) + b'\n' + _tail(cur)
        finally:
            for fd in (cur, old):
                if fd is not None:
                    os.close(fd)
    return None


def _record(raw):
    try:
        r = json.loads(raw)
    except ValueError:
        return None
    if not (isinstance(r, dict) and r.get('v') == VERSION and isinstance(r.get('kind'), str)
            and KIND_RE.match(r['kind']) and r.get('end') in ('exited', 'stopped')):
        return None
    for key in ('usedMb', 'peakMb', 'needMb'):
        v = r.get(key)
        if v is not None and not (type(v) is int and 0 < v <= 1 << 22):
            return None
    if r.get('usedMb') is None and r.get('peakMb') is None and r.get('needMb') is None:
        return None
    code = r.get('code')
    if code is not None and not (type(code) is int and -128 <= code <= 255):
        return None
    return r


def read_history(folder):
    """(records oldest first, note or None). None instead of the records when the history is not trusted: it cannot
    be read, or more than one line and more than 1 in CORRUPT_SHARE is not a record. No history: ([], None)."""
    path = os.path.join(folder, HISTORY)
    try:
        data = _read_both(path)
    except OSError as e:
        return None, f'the history {path} cannot be read ({e.strerror or e}): every kind uses its fallback'
    if data is None:
        return None, f'the history {path} kept rotating while it was read: every kind uses its fallback'
    good, bad = [], 0
    for raw in data.split(b'\n'):
        if not raw.strip():
            continue
        r = _record(raw)
        if r is None:
            bad += 1
        else:
            good.append(r)
    if bad > 1 and bad * CORRUPT_SHARE > bad + len(good):
        return None, (f'the history {path} has {bad} lines that are not records (of {bad + len(good)}): not '
                      f'trusted, every kind uses its fallback')
    note = f'{bad} line(s) of {path} skipped (not records; a writer cut off half-way)' if bad else None
    return good, note


# ---------- the kinds file ----------

def _check_fields(obj, spec, where):
    unknown = sorted(set(obj) - set(spec))
    if unknown:
        raise Refused(f'{where}: unknown key {unknown[0]!r}')
    out = {}
    for key, (types, default) in spec.items():
        if key not in obj:
            if default is None and key in ('version', 'kinds', 'fallbackMb'):
                raise Refused(f'{where}: {key!r} is missing')
            out[key] = default
            continue
        v = obj[key]
        if (isinstance(v, bool) and types is not bool) or not isinstance(v, types):
            raise Refused(f'{where}: {key!r} has the wrong type')
        out[key] = v
    return out


def load_kinds(path):
    """The kinds file (its own file, or the "capacity" object of a project config), checked; Refused when it cannot
    be read or any key is wrong."""
    if not path or not os.path.isabs(path):
        raise Refused(f'--kinds must be an absolute path (got {path!r})')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f'cannot read the kinds file {path}: {e}')
    if isinstance(data, dict) and isinstance(data.get('capacity'), dict):
        data, path = data['capacity'], path + ' (capacity)'
    if not isinstance(data, dict):
        raise Refused(f'{path}: must hold a JSON object')
    cfg = _check_fields(data, KINDS_TOP, path)
    if cfg['version'] != VERSION:
        raise Refused(f'{path}: version {cfg["version"]} is not {VERSION} (update the kit, or the file)')
    if not 0 <= cfg['marginPercent'] <= 400:
        raise Refused(f'{path}: marginPercent must be 0 to 400')
    for key, low in (('minRuns', 1), ('recentRuns', 1), ('roundMb', 1)):
        if cfg[key] < low:
            raise Refused(f'{path}: {key} must be at least {low}')
    if cfg['recentRuns'] > MAX_RECENT:
        raise Refused(f'{path}: recentRuns must be at most {MAX_RECENT}')
    if cfg['measure'] not in MEASURES:
        raise Refused(f'{path}: measure must be one of {", ".join(MEASURES)}')
    if not cfg['kinds']:
        raise Refused(f'{path}: kinds is empty')
    kinds = {}
    for name, spec in cfg['kinds'].items():
        where = f'{path}: kinds.{name}'
        if not KIND_RE.match(name) or WHOLE_RE.match(name):
            raise Refused(f'{where}: a kind is lower-case letters, digits, _ . - (at most 40), not only digits')
        if not isinstance(spec, dict):
            raise Refused(f'{where}: must be an object')
        k = _check_fields(spec, KIND_KEYS, where)
        if not 64 <= k['fallbackMb'] <= 1 << 22:
            raise Refused(f'{where}: fallbackMb must be 64 to {1 << 22}')
        if k['minMb'] is None:
            k['minMb'] = k['fallbackMb']
        if not 64 <= k['minMb'] <= k['fallbackMb']:
            raise Refused(f'{where}: minMb must be 64 to fallbackMb')
        if k['agent'] and k['minMb'] != k['fallbackMb']:
            raise Refused(f'{where}: an agent kind cannot have a minMb below its fallbackMb (heavy never sees the '
                          f'agent itself, so its runs cannot show it needs less)')
        if k['maxMb'] is not None and not k['fallbackMb'] <= k['maxMb'] <= 1 << 22:
            raise Refused(f'{where}: maxMb must be at least fallbackMb')
        kinds[name] = k
    cfg['kinds'] = kinds
    return cfg


# ---------- the amount ----------

def _figure(r, measure):
    """A record's figure for `measure`. `need` falls back to the record's other figures (the higher), so an older
    record never counts as smaller than it was."""
    if measure == 'need':
        vals = [r.get('needMb'), r.get('usedMb')] if r.get('needMb') else [r.get('usedMb'), r.get('peakMb')]
        vals = [v for v in vals if v]
        return max(vals) if vals else None
    return r.get(measure + 'Mb')


def amount(kind, cfg, records, trusted_note=None):
    """(MB, how) for a kind of the checked kinds file; records None = history not trusted (fallback).

    The highest figure of the kind's last recentRuns runs, stopped ones too (they were often stopped for growing:
    what they used is a lower bound of what they needed), plus the margin. The highest, not a percentile: a few
    big runs among many small ones are exactly the ones the check must leave room for. Never below the kind's
    floor (minMb, default its fallbackMb), never below the fallbackMb with fewer than minRuns runs, at most maxMb."""
    spec = cfg['kinds'][kind]
    fallback, floor = spec['fallbackMb'], spec['minMb']
    top = spec['maxMb'] if spec['maxMb'] is not None else 2 * fallback
    if not spec['measured']:
        return fallback, f'fallback {fallback} MB (not measured: nothing of this kind runs under heavy)'
    if records is None:
        return fallback, f'fallback {fallback} MB ({trusted_note or "history not trusted"})'
    measure = cfg['measure']
    runs = [(r['end'], _figure(r, measure)) for r in records if r['kind'] == kind]
    runs = [(end, v) for end, v in runs if v][-cfg['recentRuns']:]
    agent = ' (an agent start: the agent itself is never measured)' if spec['agent'] else ''
    if not runs:
        return fallback, f'fallback {fallback} MB (0 measured runs, {cfg["minRuns"]} needed){agent}'
    high = max(v for _, v in runs)
    r = cfg['roundMb']
    measured = int(math.ceil(high * (100 + cfg['marginPercent']) / 100 / r)) * r
    stopped = sum(end == 'stopped' for end, _ in runs)
    how = (f'highest {measure} {high} MB of {len(runs)} runs ({stopped} stopped) + {cfg["marginPercent"]:g}%'
           f' = {measured} MB')
    if len(runs) < cfg['minRuns']:
        base, why = fallback, f'fallback {fallback} MB until {cfg["minRuns"]} runs'
    else:
        base, why = floor, f'floor {floor} MB'
    mb = max(base, measured)
    how += f'; {why}{agent}'
    if mb > top:
        mb, how = top, how + f'; capped at maxMb {top}'
    return mb, how


def amounts(cfg, folder, kinds=None):
    records, note = read_history(folder)
    out = {}
    for kind in kinds or sorted(cfg['kinds']):
        mb, how = amount(kind, cfg, records, note)
        out[kind] = {'mb': mb, 'how': how, 'fallbackMb': cfg['kinds'][kind]['fallbackMb']}
    return out, note


# ---------- recording a job ----------

def _read_int(fd):
    try:
        return int(os.pread(fd, 64, 0).split()[0])
    except (OSError, ValueError, IndexError):
        return None


class Measurer:
    """Looks at a job's scope while it runs and appends one record when it ends. It has the queue's Sampler
    interface (interval, sample, record, record_soon; heavy sets `status` to the command's wait status before it
    records) and calls the queue's own sampler too when the job is queued. It never raises: a measurement that
    fails is a line on stderr, never a failed job."""

    def __init__(self, box, scope, kind, folder, used_of, inner=None, now=time.time, monotonic=time.monotonic,
                 out=say):
        self.box, self.scope, self.kind, self.folder, self.used_of = box, scope, kind, folder, used_of
        self.inner, self.now, self.monotonic, self.out = inner, now, monotonic, out
        self.peak_fd = self.current_fd = None
        self.peak = self.used = self.need = self.tries = 0
        self.done, self.status = False, None
        self.start = monotonic()

    @property
    def interval(self):
        if self.peak_fd is None and self.tries < FIRST_TRIES:
            return FIRST_LOOK
        return min(SAMPLE_SECONDS, self.inner.interval) if self.inner is not None else SAMPLE_SECONDS

    def sample(self):
        if self.inner is not None:
            self.inner.sample()
        self.tries += 1
        self._look()

    def _look(self):
        """One look: memory.current, then memory.peak, then `used` (in this order a figure read later is never the
        smaller for it). The job's need at this look is used + (peak - current): what it holds now that cannot be
        dropped, plus how far it was above its current size at its high-water mark."""
        try:
            cg = self.box.folder(self.scope)
            if cg is None and self.tries < FIRST_TRIES:
                self.box.refresh()
                cg = self.box.folder(self.scope)
            if cg is None:
                return
            if self.peak_fd is None:
                try:
                    self.peak_fd = os.open(os.path.join(cg, 'memory.peak'), os.O_RDONLY | os.O_CLOEXEC)
                    self.current_fd = os.open(os.path.join(cg, 'memory.current'), os.O_RDONLY | os.O_CLOEXEC)
                except OSError:
                    pass                # no scope yet, or a kernel without memory.peak: `used` alone
            current = _read_int(self.current_fd) if self.current_fd is not None else None
            peak = _read_int(self.peak_fd) if self.peak_fd is not None else None     # None once the scope is gone
            if peak:
                self.peak = max(self.peak, peak)
            used = self.used_of(cg)
            if used:
                self.used = max(self.used, used)
                above = peak - current if peak and current is not None and peak > current else 0
                self.need = max(self.need, used + above)
        except Exception:
            pass

    def record(self, wait=None):
        if self.inner is not None:
            self.inner.record(wait)
        self._append('exited')

    def record_soon(self):
        if self.inner is not None:
            self.inner.record_soon()
        self._append('stopped')

    def _append(self, end):
        if self.done:
            return
        self.done = True
        self._look()                    # a scope with leftovers still answers
        for name in ('peak_fd', 'current_fd'):
            fd = getattr(self, name)
            if fd is not None:
                os.close(fd)
                setattr(self, name, None)
        mb = lambda b: int(math.ceil(b / MB)) if b else None
        try:
            code = os.waitstatus_to_exitcode(self.status) if self.status is not None else None
        except ValueError:
            code = None
        rec = {'v': VERSION, 'at': _iso(self.now()), 'kind': self.kind, 'end': end,
               'seconds': round(self.monotonic() - self.start, 1), 'code': code,
               'usedMb': mb(self.used), 'peakMb': mb(self.peak), 'needMb': mb(max(self.need, self.used))}
        if rec['usedMb'] is None and rec['peakMb'] is None:
            return                      # the scope was never seen (a very short job): nothing to learn
        try:
            note = append(self.folder, rec)
        except Exception as e:
            self.out(f'could not record the memory of {self.kind} ({e})')
            return
        if note:
            self.out(note)


# ---------- the verb ----------

USAGE = ('usage: heavy capacity --kinds FILE --check PROGRAM [--state-dir DIR] [--] [<kind> | <MB>] [args...]\n'
         '       heavy capacity --kinds FILE --show [--json] [--state-dir DIR] [<kind>...]')


def _options(argv):
    opts, rest = {}, list(argv)
    while rest and rest[0].startswith('--'):
        word = rest.pop(0)
        if word == '--':
            break
        key, eq, value = word.partition('=')
        if key in ('--show', '--json'):
            if eq:
                raise Refused(f'{key} takes no value')
            opts[key] = True
        elif key in ('--kinds', '--check', '--state-dir'):
            if not eq:
                if not rest:
                    raise Refused(f'{key} needs a value')
                value = rest.pop(0)
            opts[key] = value
        elif key == '--help':
            opts[key] = True
        else:
            raise Refused(f'unknown option {key}')
    return opts, rest


def capacity_main(argv, environ=None, execv=os.execve):
    environ = dict(os.environ if environ is None else environ)
    try:
        opts, rest = _options(argv)
        if opts.get('--help'):
            print(__doc__.split('\n\n')[0] + '\n\n' + USAGE)
            return 0
        if environ.get(FRONT_ENV) == '1':
            raise Refused('the capacity check called `heavy capacity` again (a loop): --check must name the '
                          'check itself, not the command that runs this front')
        if '--kinds' not in opts:
            raise Refused('--kinds FILE is required (the project\'s kinds and fallback amounts)')
        if opts.get('--show'):
            return _show(opts, rest)
        check = opts.get('--check')
        if not check or not os.path.isabs(check):
            raise Refused('--check must be the absolute path of the capacity check')
        if not (os.path.isfile(check) and os.access(check, os.X_OK)):
            raise Refused(f'the capacity check {check} is not a program that can be run')
        kind = ''
        if rest and rest[0] != '' and not WHOLE_RE.match(rest[0]):     # '' keeps the check's own default
            if not KIND_RE.match(rest[0]):
                raise Refused(f'{rest[0]!r} is neither a whole number of MB nor a kind')
            cfg = load_kinds(opts['--kinds'])
            kind = rest[0]
            if kind not in cfg['kinds']:
                raise Refused(f'unknown kind {kind!r} (kinds: {", ".join(sorted(cfg["kinds"]))})')
            found, _note = amounts(cfg, front_dir(opts), [kind])
            rest = [str(found[kind]['mb'])] + rest[1:]
    except Refused as e:
        say(f'capacity: {e}')
        print(USAGE, file=sys.stderr)
        return EXIT_USAGE
    env = dict(environ, **{FRONT_ENV: '1', 'HEAVY_CAPACITY_KIND': kind})
    sys.stdout.flush()
    try:
        execv(check, [check, *rest], env)
    except OSError as e:
        say(f'capacity: cannot run {check}: {e.strerror or e}')
        return EXIT_USAGE
    return 0                            # only a test's execv returns


def front_dir(opts):
    """The history the front reads: --state-dir, else the user's own (HEAVY_STATE_DIR is ignored here)."""
    d = opts.get('--state-dir')
    if d is None:
        return default_dir()
    if not os.path.isabs(d):
        raise Refused(f'--state-dir must be an absolute path (got {d!r})')
    return d


def _show(opts, kinds):
    cfg = load_kinds(opts['--kinds'])
    for k in kinds:
        if k not in cfg['kinds']:
            raise Refused(f'unknown kind {k!r}')
    folder = front_dir(opts)
    found, note = amounts(cfg, folder, kinds or None)
    if opts.get('--json'):
        print(json.dumps({'v': VERSION, 'history': os.path.join(folder, HISTORY), 'note': note, 'kinds': found},
                         indent=2, sort_keys=True))
        return 0
    for k, v in found.items():
        print(f'{k:<20} {v["mb"]:>7} MB  {v["how"]}')
    if note:
        print(f'note: {note}')
    return 0
