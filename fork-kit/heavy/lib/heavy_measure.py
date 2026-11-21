"""heavy's measured history and the capacity front: size each start by what its kind of job was seen to need.

  heavy --measure <kind> [--] <command> [args...]     run as usual; record the job's peak memory under <kind>
  heavy capacity --kinds FILE --check PROGRAM [--] [<kind> | <MB>] [args...]
  heavy capacity --kinds FILE --show [--json] [<kind>...]

Recording: a job run with --measure (or queued with --kind, unless --measure names another kind) has its scope's
memory looked at every second while it runs: memory.peak (the kernel's high-water mark since the scope began, so a
spike between two looks still counts) and `used` (memory.current less file cache the kernel can drop, at least its
anon, plus its swap). The scope is gone as soon as its last process ends, and its files with it (reads then fail
with ENODEV): the record holds the last look, so only growth in the last second is missed. When the command ends,
one JSON line goes to
~/.local/state/heavy/measured.jsonl (HEAVY_STATE_DIR names another folder): the kind, both figures in MB, how long
it ran and how it ended. Lines are appended with one write each (two jobs that end together keep both lines) and
the file is rotated to measured.jsonl.1 past 256 KiB (at most two files are kept). A nested heavy (inside another
heavy's scope) and HEAVY_KEEP=1 record nothing: the outer job's scope is not the inner command's, and a kept scope
lives on after the command.

capacity: the front of the machine's capacity check. The check program gets the caller's arguments unchanged,
except that a first argument that is a kind name becomes the number of MB that kind needs:

  no argument, or a whole number    passed on unchanged (what every caller does today keeps working)
  a kind of the kinds file          its amount: the `percentile` of its last `recentRuns` measured runs (runs that
                                    ended by themselves; stopped ones are left out) x (1 + marginPercent/100),
                                    rounded up to roundMb, at least minMb and at most the kind's maxMb (default
                                    2 x fallbackMb); fewer than minRuns runs: its fallbackMb
  any other word                    refused (exit 2): a typo must not reach a check that would read it as 0

A missing history is the first run (fallback). A history that cannot be read, or whose lines are mostly not
records, is not trusted: every kind gets its fallbackMb. Never 0, never a guess. A kinds file that cannot be read
or has a wrong key is refused (exit 2), so a caller's `capacity <kind> && start` never starts on a broken config.
The check runs with HEAVY_CAPACITY_KIND (the kind, or empty) and HEAVY_CAPACITY_FRONT=1 in its environment; a
check that calls the front again is refused (exit 2). heavy's exit code is the check's.

The kinds file is the project's: its own file, or the "capacity" object of a larger project config. Standard
library only.
"""
import datetime, errno, fcntl, json, math, os, re, stat, sys, time

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
FRONT_ENV = 'HEAVY_CAPACITY_FRONT'
EXIT_USAGE = 2

KINDS_TOP = {
    'version': (int, None),
    'percentile': ((int, float), 95),
    'marginPercent': ((int, float), 20),
    'minRuns': (int, 5),
    'recentRuns': (int, 50),
    'roundMb': (int, 128),
    'minMb': (int, 256),
    'measure': (str, 'used'),
    'kinds': (dict, None),
}
KIND_KEYS = {'fallbackMb': (int, None), 'maxMb': (int, None), 'note': (str, None)}
MEASURES = ('used', 'peak')


class Refused(Exception):
    pass


def say(msg):
    try:
        sys.stderr.write(f'heavy: {msg}\n')
        sys.stderr.flush()
    except OSError:
        pass


def state_dir():
    """HEAVY_STATE_DIR (tests: never the user's real history), else ~/.local/state/heavy."""
    d = os.environ.get('HEAVY_STATE_DIR')
    if d:
        if not os.path.isabs(d):
            raise Refused(f'HEAVY_STATE_DIR {d!r} is not an absolute path')
        return d
    return os.path.join(os.path.expanduser('~'), '.local', 'state', 'heavy')


def check_kind(kind):
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise Refused(f'bad kind {kind!r} (lower-case letters, digits, _ . -; at most 40)')
    return kind


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


# ---------- the history ----------

def append(folder, record):
    """Append one record as one line with one write (O_APPEND: lines of two writers never mix). A file that does not
    end in a newline (a writer cut off half-way) gets one first, so the cut line costs only itself."""
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
        rotate(folder)


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
    writer that still has the old file open appends to the rotated one, so its line is kept."""
    path = os.path.join(folder, HISTORY)
    fd = os.open(os.path.join(folder, LOCK), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return                      # another writer rotates now
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return
        if stat.S_ISREG(st.st_mode) and st.st_size > MAX_BYTES:     # not rotated by someone just before
            os.replace(path, path + '.1')
    finally:
        os.close(fd)


def _read_tail(path):
    """The last READ_MAX bytes of a plain file (from a line start), b'' when it is not there. Raises OSError when it
    is there but cannot be read (a folder, a link, no permission)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        return b''
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, f'{path} is not a plain file')
        start = max(0, st.st_size - READ_MAX)
        data = os.pread(fd, st.st_size - start, start)
    finally:
        os.close(fd)
    if start:
        data = data.partition(b'\n')[2]     # the first line was cut by the window: not a bad line
    return data


def _record(raw):
    try:
        r = json.loads(raw)
    except ValueError:
        return None
    if not (isinstance(r, dict) and r.get('v') == VERSION and isinstance(r.get('kind'), str)
            and KIND_RE.match(r['kind']) and r.get('end') in ('exited', 'stopped')):
        return None
    for key in ('usedMb', 'peakMb'):
        v = r.get(key)
        if v is not None and not (type(v) is int and 0 < v <= 1 << 22):
            return None
    if r.get('usedMb') is None and r.get('peakMb') is None:
        return None
    return r


def read_history(folder):
    """(records oldest first, note or None). None instead of the records when the history is not trusted: it cannot
    be read, or more than one line and more than 1 in CORRUPT_SHARE is not a record. No history: ([], None)."""
    path = os.path.join(folder, HISTORY)
    try:
        data = _read_tail(path + '.1') + b'\n' + _read_tail(path)
    except OSError as e:
        return None, f'the history {path} cannot be read ({e.strerror or e}): every kind uses its fallback'
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
        if isinstance(v, bool) or not isinstance(v, types):
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
    if not 50 <= cfg['percentile'] <= 100:
        raise Refused(f'{path}: percentile must be 50 to 100')
    if not 0 <= cfg['marginPercent'] <= 400:
        raise Refused(f'{path}: marginPercent must be 0 to 400')
    for key, low in (('minRuns', 1), ('recentRuns', 1), ('roundMb', 1), ('minMb', 1)):
        if cfg[key] < low:
            raise Refused(f'{path}: {key} must be at least {low}')
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
        if k['maxMb'] is not None and not k['fallbackMb'] <= k['maxMb'] <= 1 << 22:
            raise Refused(f'{where}: maxMb must be at least fallbackMb')
        kinds[name] = k
    cfg['kinds'] = kinds
    return cfg


# ---------- the amount ----------

def percentile(values, pct):
    """Nearest rank: the smallest value with at least pct % of the values at or below it."""
    s = sorted(values)
    return s[max(0, math.ceil(pct / 100 * len(s)) - 1)]


def amount(kind, cfg, records, trusted_note=None):
    """(MB, how) for a kind of the checked kinds file; records None = history not trusted (fallback)."""
    spec = cfg['kinds'][kind]
    fallback = spec['fallbackMb']
    if records is None:
        return fallback, f'fallback {fallback} MB ({trusted_note or "history not trusted"})'
    key = cfg['measure'] + 'Mb'
    runs = [r[key] for r in records if r['kind'] == kind and r['end'] == 'exited' and r.get(key)]
    runs = runs[-cfg['recentRuns']:]
    if len(runs) < cfg['minRuns']:
        return fallback, f'fallback {fallback} MB ({len(runs)} measured runs, {cfg["minRuns"]} needed)'
    p = percentile(runs, cfg['percentile'])
    r = cfg['roundMb']
    mb = max(cfg['minMb'], int(math.ceil(p * (100 + cfg['marginPercent']) / 100 / r)) * r)
    top = spec['maxMb'] if spec['maxMb'] is not None else 2 * fallback
    how = (f'p{cfg["percentile"]:g} {cfg["measure"]} {p} MB of {len(runs)} runs + {cfg["marginPercent"]:g}%'
           f' (fallback {fallback} MB)')
    if mb > top:
        mb, how = top, how + f', capped at maxMb {top}'
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
    interface (interval, sample, record, record_soon) and calls the queue's own sampler too when the job is queued.
    It never raises: a measurement that fails is a line on stderr, never a failed job."""

    def __init__(self, box, scope, kind, folder, used_of, inner=None, now=time.time, monotonic=time.monotonic,
                 out=say):
        self.box, self.scope, self.kind, self.folder, self.used_of = box, scope, kind, folder, used_of
        self.inner, self.now, self.monotonic, self.out = inner, now, monotonic, out
        self.peak_fd, self.peak, self.used, self.tries, self.done = None, 0, 0, 0, False
        self.start = monotonic()

    @property
    def interval(self):
        if self.peak_fd is None and self.tries < FIRST_TRIES:
            return FIRST_LOOK
        return min(SAMPLE_SECONDS, self.inner.interval) if self.inner is not None else SAMPLE_SECONDS

    def sample(self):
        if self.inner is not None:
            self.inner.sample()
        try:
            self.tries += 1
            cg = self.box.folder(self.scope)
            if cg is None and self.tries < FIRST_TRIES:
                self.box.refresh()
                cg = self.box.folder(self.scope)
            if cg is None:
                return
            if self.peak_fd is None:
                try:
                    self.peak_fd = os.open(os.path.join(cg, 'memory.peak'), os.O_RDONLY | os.O_CLOEXEC)
                except OSError:
                    pass                # no scope yet, or a kernel without memory.peak: `used` alone
            if self.peak_fd is not None:
                self.peak = max(self.peak, _read_int(self.peak_fd) or 0)    # None once the scope is gone
            used = self.used_of(cg)
            if used:
                self.used = max(self.used, used)
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
        if self.peak_fd is not None:
            self.peak = max(self.peak, _read_int(self.peak_fd) or 0)    # a scope with leftovers still answers
            os.close(self.peak_fd)
            self.peak_fd = None
        peak = self.peak
        rec = {'v': VERSION, 'at': _iso(self.now()), 'kind': self.kind, 'end': end,
               'seconds': round(self.monotonic() - self.start, 1),
               'usedMb': int(math.ceil(self.used / MB)) if self.used else None,
               'peakMb': int(math.ceil(peak / MB)) if peak else None}
        if rec['usedMb'] is None and rec['peakMb'] is None:
            return                      # the scope was never seen (a very short job): nothing to learn
        try:
            append(self.folder, rec)
        except Exception as e:
            self.out(f'could not record the memory of {self.kind} ({e})')


# ---------- the verb ----------

USAGE = ('usage: heavy capacity --kinds FILE --check PROGRAM [--] [<kind> | <MB>] [args...]\n'
         '       heavy capacity --kinds FILE --show [--json] [<kind>...]')


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
        elif key in ('--kinds', '--check'):
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
        if rest and not WHOLE_RE.match(rest[0]):
            if not KIND_RE.match(rest[0]):
                raise Refused(f'{rest[0]!r} is neither a whole number of MB nor a kind')
            cfg = load_kinds(opts['--kinds'])
            kind = rest[0]
            if kind not in cfg['kinds']:
                raise Refused(f'unknown kind {kind!r} (kinds: {", ".join(sorted(cfg["kinds"]))})')
            found, _note = amounts(cfg, state_dir(), [kind])
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


def _show(opts, kinds):
    cfg = load_kinds(opts['--kinds'])
    for k in kinds:
        if k not in cfg['kinds']:
            raise Refused(f'unknown kind {k!r}')
    folder = state_dir()
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
