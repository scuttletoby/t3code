"""heavy's memory queue: a job of a known kind starts only when its memory fits; the others wait in order.

  heavy --kind <name> [--mb N] [--wait S] [--] <command> [args...]
  heavy queue [--json]            what is admitted and what waits, and the memory figures behind it
  heavy waits [--days N] [--json] minutes waited for memory per day (from the wait log)

A job with --kind reserves memory before its scope starts: its learned peak once the kind has 3 clean runs, else
--mb (the project's measured start value), else 3072 MB. It is admitted when

  reservation <= budget - used - owed     and     reservation <= MemAvailable - vmReserveMb - owed

budget   the slice's memory.high (else memory.max, else machine-file budgetMb) minus headroomMb
used     the slice's memory that cannot simply be dropped: memory.current minus clean file cache, at least its anon,
         plus its swap (the same rule t3-capacity uses for the box)
owed     for every admitted job: the part of its reservation it has not used yet (its scope's own `used`), so a
         job that has not grown yet still holds its room, and a job that has grown is counted once

and the slice is not under memory pressure (full avg10 > pressureFull10 or some avg60 > pressureSome60). Waiting
jobs go in order of arrival. A job that does not fit lets later, smaller jobs pass, until it has waited
passLimitSeconds; then nothing passes it and it gets the next room that frees (no starvation of big jobs). A job
larger than the whole budget runs alone: when it is first in line and nothing else is admitted.

State: one folder per user and slice in /run/user/<uid>/heavy-queue/ (tmpfs: empty after a reboot). Each job holds
an exclusive flock on its own ticket file for its whole life; the kernel drops it when the process ends in any way,
and whoever takes the queue lock next deletes tickets nobody holds. So a waiter that dies or a job that crashes
frees its place at once, and nothing in the folder can keep the queue stuck. A live ticket that cannot be read
(garbled, or written by another version) counts as admitted at 3072 MB, with a note.

Waiting polls the real figures every pollSeconds (default 2) and prints the reason and the position when they
change and once a minute. After --wait seconds (default 7200) heavy gives up with exit 76.

Wait log: every admission, give-up and stop is one JSON line in ~/.local/state/heavy/waits.log (kept to the last
few thousand lines); `heavy waits` sums it per day. Peaks: while an admitted job runs, its scope's `used` is
sampled every few seconds; after exit 0 the highest sample goes into ~/.local/state/heavy/peaks.json (the last 20
per kind). The learned reservation is the 95th percentile of those x 1.15, rounded up to 128 MB, and kept between
half and 4x the start value.

Settings: the optional "queue" object in the machine file's "heavy" section (fork-kit/common/README.md); without
the file or the key, the defaults below hold. Standard library only.
"""
import datetime, errno, fcntl, importlib.machinery, importlib.util, json, math, os, re, signal, stat, sys, time

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERSION = 1
MB = 1 << 20
DEFAULT_MB = 3072
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}\Z')
TICKET_RE = re.compile(r'^t-([0-9]{1,15})-([0-9]{1,10})\.json\Z')
EXIT_GAVE_UP = 76
PRINT_EVERY = 60
LEARN_MIN_RUNS = 3
LEARN_KEEP = 20
LEARN_MARGIN = 1.15
LEARN_ROUND_MB = 128
WAITS_KEEP_LINES = 5000
WAITS_MAX_BYTES = 1 << 20
SAMPLE_SECONDS = 3


def _load_machine_file():
    """fork-kit/common/machine_file.py: in the version folder once installed, next to the kit in the repository."""
    for folder in (os.path.join(KIT, 'common'), os.path.join(os.path.dirname(KIT), 'common')):
        path = os.path.join(folder, 'machine_file.py')
        if os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader('fork_kit_machine_file', path)
            spec = importlib.util.spec_from_loader('fork_kit_machine_file', loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise ImportError('fork-kit/common/machine_file.py not found next to the heavy kit')


machine_file = _load_machine_file()


def _at_least(low, high=None):
    return lambda v: None if v >= low and (high is None or v <= high) else (
        f'must be at least {low}' + (f' and at most {high}' if high is not None else ''))


SETTINGS = {
    'budgetMb': (int, 0, _at_least(0)),                    # 0: from the slice's memory.high / memory.max
    'headroomMb': (int, 512, _at_least(0)),
    'vmReserveMb': (int, 1024, _at_least(0)),
    'pressureFull10': ((int, float), 5, _at_least(0, 100)),
    'pressureSome60': ((int, float), 10, _at_least(0, 100)),
    'passLimitSeconds': ((int, float), 600, _at_least(0)),
    'pollSeconds': ((int, float), 2, _at_least(0.2, 30)),
}
SECTION = {'sweep': (dict, None, None), 'queue': (dict, None, None)}


class Refused(Exception):
    pass


class GaveUp(Exception):
    pass


class Stopped(Exception):
    def __init__(self, sig):
        super().__init__(sig)
        self.sig = sig


def say(msg):
    try:
        sys.stderr.write(f'heavy: {msg}\n')
        sys.stderr.flush()
    except OSError:
        pass


def settings(machine_path=None, owner_uid=0):
    """The queue settings with defaults filled in; a missing machine file, section or key means the defaults."""
    path = machine_path or machine_file.default_path()
    try:
        section = machine_file.read_section('heavy', machine_path, owner_uid, optional=True)
        if section is None:
            return machine_file.fields({}, SETTINGS, f'{path}: heavy.queue')
        section = machine_file.fields(section, SECTION, f'{path}: heavy')
        return machine_file.fields(section['queue'] or {}, SETTINGS, f'{path}: heavy.queue')
    except machine_file.MachineFileError as e:
        raise Refused(str(e))


def check_kind(kind):
    if not isinstance(kind, str) or not KIND_RE.match(kind):
        raise Refused(f'bad --kind {kind!r} (lower-case letters, digits, _ . -; at most 40)')
    return kind


# ---------- the real figures ----------

def _read(path):
    with open(path, encoding='ascii') as f:
        return f.read()


def _flat(text):
    out = {}
    for line in text.splitlines():
        k, _, v = line.partition(' ')
        try:
            out[k] = int(v)
        except ValueError:
            pass
    return out


def cgroup_used(folder):
    """Bytes the cgroup holds that cannot simply be dropped, or None when it is not there."""
    try:
        current = int(_read(os.path.join(folder, 'memory.current')))
        st = _flat(_read(os.path.join(folder, 'memory.stat')))
    except (OSError, ValueError):
        return None
    try:
        swap = int(_read(os.path.join(folder, 'memory.swap.current')))
    except (OSError, ValueError):
        swap = 0
    droppable = (st.get('active_file', 0) + st.get('inactive_file', 0) - st.get('file_mapped', 0)
                 - st.get('file_dirty', 0) - st.get('file_writeback', 0))
    return max(current - max(droppable, 0), st.get('anon', 0)) + swap


def pressure(folder):
    """(full avg10, some avg60) of the cgroup's memory.pressure; (0, 0) when it cannot be read."""
    full10 = some60 = 0.0
    try:
        for line in _read(os.path.join(folder, 'memory.pressure')).splitlines():
            words = dict(w.split('=', 1) for w in line.split()[1:] if '=' in w)
            if line.startswith('full '):
                full10 = float(words.get('avg10', 0))
            elif line.startswith('some '):
                some60 = float(words.get('avg60', 0))
    except (OSError, ValueError):
        pass
    return full10, some60


def mem_available(meminfo):
    try:
        for line in _read(meminfo).splitlines():
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


class Box:
    """Where the figures come from. heavy builds one from the slice; the tests build one on a fake cgroup tree."""

    def __init__(self, slice_name, slice_cg, limit_bytes, cg_root='/sys/fs/cgroup', meminfo='/proc/meminfo',
                 queue_root=None, state_dir=None, resolve=None):
        self.slice_name = slice_name
        self.slice_cg = slice_cg            # '' while the slice is not active; resolve() asks again
        self.limit_bytes = limit_bytes      # memory.high, else memory.max, of the slice (None: neither)
        self.cg_root = cg_root
        self.meminfo = meminfo
        self.queue_dir = os.path.join(queue_root or f'/run/user/{os.getuid()}/heavy-queue', slice_name)
        self.state_dir = state_dir or os.path.join(os.path.expanduser('~'), '.local', 'state', 'heavy')
        self.resolve = resolve

    def folder(self, scope=None):
        if not self.slice_cg and self.resolve is not None:
            self.slice_cg = self.resolve() or ''
        if not self.slice_cg:
            return None
        return self.cg_root + self.slice_cg + ('/' + scope if scope else '')

    def facts(self, cfg):
        top = self.folder()
        used = cgroup_used(top) if top else 0
        full10, some60 = pressure(top) if top else (0.0, 0.0)
        budget = cfg['budgetMb'] * MB if cfg['budgetMb'] else self.limit_bytes
        if budget is None:
            raise Refused(f'{self.slice_name} has no memory.high or memory.max and the machine file sets no '
                          f'heavy.queue.budgetMb')
        return {'budget': budget - cfg['headroomMb'] * MB, 'used': used or 0, 'full10': full10, 'some60': some60,
                'avail': mem_available(self.meminfo)}

    def scope_used(self, scope):
        folder = self.folder(scope) if scope else None
        return cgroup_used(folder) if folder else None


# ---------- tickets ----------

class Ticket:
    def __init__(self, name, data, fd=None, note=None):
        self.name, self.data, self.fd, self.note = name, data, fd, note
        m = TICKET_RE.match(name)
        self.seq = int(m.group(1))

    def __getattr__(self, key):
        try:
            return self.__dict__['data'][key]
        except KeyError:
            raise AttributeError(key) from None


def _mb(value):
    return int(math.ceil(value / MB))


class Queue:
    def __init__(self, box, cfg, now=time.time, sleep=time.sleep, monotonic=time.monotonic, out=say):
        self.box, self.cfg = box, cfg
        self.now, self.sleep, self.monotonic, self.out = now, sleep, monotonic, out
        self.lock_fd = None

    # ----- the folder and its lock -----

    def _folder(self):
        d = self.box.queue_dir
        parent = os.path.dirname(d)
        base = os.path.dirname(parent)
        if not os.path.isdir(base):
            raise Refused(f'{base} is not there (heavy needs the systemd user manager, which makes it)')
        for folder in (parent, d):
            try:
                os.mkdir(folder, 0o700)
            except FileExistsError:
                pass
            st = os.lstat(folder)
            if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
                raise Refused(f'{folder}: not a folder of this user that only it can change; not using it')
        return d

    def __enter__(self):
        d = self._folder()
        fd = os.open(os.path.join(d, 'lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        self.lock_fd = fd
        return self

    def __exit__(self, *exc):
        os.close(self.lock_fd)
        self.lock_fd = None

    # ----- tickets (only under the lock) -----

    def tickets(self):
        """Live tickets in order; tickets nobody holds any more are deleted on the way."""
        d = self.box.queue_dir
        out = []
        for name in os.listdir(d):
            if not TICKET_RE.match(name):
                continue
            path = os.path.join(d, name)
            try:
                fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
            except FileNotFoundError:
                continue
            except OSError:          # a link or worse: not a ticket of ours
                _unlink(path)
                continue
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except OSError as e:
                    if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                else:                # nobody holds it: its process is gone
                    _unlink(path)
                    continue
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    continue
                raw = os.read(fd, 65536)
            finally:
                os.close(fd)
            out.append(_parse(name, raw))
        out.sort(key=lambda t: t.seq)
        return out

    def create(self, kind, mb, scope, cmd):
        live = self.tickets()
        seq = max([t.seq for t in live], default=0) + 1
        name = f't-{seq}-{os.getpid()}.json'
        path = os.path.join(self.box.queue_dir, name)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        data = {'v': VERSION, 'kind': kind, 'mb': mb, 'pid': os.getpid(), 'seq': seq, 'since': self.now(),
                'state': 'waiting', 'admittedAt': None, 'scope': scope, 'cmd': cmd}
        t = Ticket(name, data, fd)
        self.write(t)
        return t

    def write(self, t):
        raw = json.dumps(t.data, sort_keys=True).encode()
        os.ftruncate(t.fd, 0)
        os.pwrite(t.fd, raw, 0)

    # ----- the decision -----

    def owed(self, admitted):
        total = 0
        for t in admitted:
            used = self.box.scope_used(t.data.get('scope')) or 0
            total += max(0, t.mb * MB - used)
        return total

    def decide(self, me, live, facts):
        """(admit?, reason, position, figures) for ticket `me` among the live tickets."""
        cfg = self.cfg
        admitted = [t for t in live if t.state != 'waiting']
        waiting = [t for t in live if t.state == 'waiting']
        owed = self.owed(admitted)
        budget, used = facts['budget'], facts['used']
        room = budget - used - owed
        if facts['avail'] is not None:
            room = min(room, facts['avail'] - cfg['vmReserveMb'] * MB - owed)
        fig = {'budgetMb': _mb(budget), 'usedMb': _mb(used), 'owedMb': _mb(owed), 'roomMb': int(room // MB),
               'availMb': None if facts['avail'] is None else _mb(facts['avail'])}
        ahead = [t for t in waiting if t.seq < me.seq]
        pos = len(ahead) + 1
        fig['position'], fig['waiting'] = pos, len(waiting)
        if facts['full10'] > cfg['pressureFull10'] or facts['some60'] > cfg['pressureSome60']:
            return False, (f'the slice is under memory pressure (full avg10 {facts["full10"]:.1f}%, '
                           f'some avg60 {facts["some60"]:.1f}%)'), fig
        now = self.now()
        for t in ahead:
            if t.mb * MB > budget:          # runs alone, as soon as it is first and nothing else is admitted
                if t is ahead[0] and not admitted:
                    return False, f'{t.kind} ({t.mb} MB, larger than the budget) is first and runs alone', fig
                if now - t.since >= cfg['passLimitSeconds']:
                    return False, f'{t.kind} ({t.mb} MB) has waited past the pass limit; nothing passes it', fig
                continue
            if t.mb * MB <= room:
                room -= t.mb * MB           # its share: it takes it on its own next look
            elif now - t.since >= cfg['passLimitSeconds']:
                return False, f'{t.kind} ({t.mb} MB) has waited past the pass limit; nothing passes it', fig
        fig['roomMb'] = int(room // MB)
        if me.mb * MB > budget:
            if not ahead and not admitted:
                return True, 'larger than the budget: runs alone', fig
            return False, (f'{me.mb} MB is larger than the budget ({fig["budgetMb"]} MB): it runs alone once it is '
                           f'first and nothing else runs'), fig
        if me.mb * MB <= room:
            return True, 'fits', fig
        why = f'needs {me.mb} MB, room {max(0, fig["roomMb"])} MB'
        return False, why, fig

    # ----- waiting -----

    def enter(self, kind, mb, scope, cmd, wait_s):
        """Wait until the job fits; returns its held Ticket. Raises GaveUp after wait_s, Stopped on a signal."""
        with self:
            t = self.create(kind, mb, scope, cmd)
        start = self.monotonic()
        last, last_print = None, None
        try:
            with _stop_signals():
                while True:
                    with self:
                        facts = self.box.facts(self.cfg)
                        ok, why, fig = self.decide(t, self.tickets(), facts)
                        waited = self.monotonic() - start
                        if ok:
                            t.data.update(state='admitted', admittedAt=self.now())
                            self.write(t)
                            self.log(kind, mb, waited, 'admitted')
                            if waited >= 1:
                                self.out(f'admitted {kind} ({mb} MB) after waiting {_dur(waited)} for memory')
                            return t
                        if waited >= wait_s:
                            self.log(kind, mb, waited, 'gave-up')
                            raise GaveUp(f'gave up after waiting {_dur(waited)} for memory: {kind} {why}')
                    key = (why.split(',')[0], fig['position'])
                    if key != last or last_print is None or self.monotonic() - last_print >= PRINT_EVERY:
                        self.out(f'waiting for memory: {kind} {why} (budget {fig["budgetMb"]} MB, used '
                                 f'{fig["usedMb"]}, reserved not yet used {fig["owedMb"]}, available '
                                 f'{fig["availMb"]}); position {fig["position"]} of {fig["waiting"]}, waited '
                                 f'{_dur(waited)}')
                        last, last_print = key, self.monotonic()
                    self.sleep(min(self.cfg['pollSeconds'], max(0.0, wait_s - waited) + 0.01))
        except Stopped:
            with self:
                self.log(kind, mb, self.monotonic() - start, 'stopped')
            release(self, t)
            raise
        except BaseException:
            release(self, t)
            raise

    # ----- wait log -----

    def log(self, kind, mb, waited, outcome):
        """One line per job in the wait log (under the queue lock: lines never mix, pruning never loses one)."""
        d = self.box.state_dir
        try:
            os.makedirs(d, mode=0o700, exist_ok=True)
            path = os.path.join(d, 'waits.log')
            line = json.dumps({'at': _iso(self.now()), 'kind': kind, 'mb': mb, 'waitedS': round(waited, 1),
                               'outcome': outcome}, sort_keys=True) + '\n'
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                os.write(fd, line.encode())
                size = os.fstat(fd).st_size
            finally:
                os.close(fd)
            if size > WAITS_MAX_BYTES:
                with open(path, encoding='utf-8', errors='replace') as f:
                    keep = f.readlines()[-WAITS_KEEP_LINES:]
                _replace(path, ''.join(keep))
        except OSError as e:
            self.out(f'could not write the wait log ({e})')


def close(t):
    """Drop the ticket's flock (the file stays until the next look deletes it)."""
    if t is not None and t.fd is not None:
        fd, t.fd = t.fd, None
        os.close(fd)


def release(queue, t):
    """The job ended: delete the ticket (under the lock) and drop its flock. A crash skips this; the next look
    deletes the ticket instead."""
    if t is None or t.fd is None:
        return
    try:
        with queue:
            _unlink(os.path.join(queue.box.queue_dir, t.name))
    except (OSError, Refused):
        pass
    close(t)


def _unlink(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass


def _parse(name, raw):
    try:
        data = json.loads(raw.decode('utf-8'))
        if not isinstance(data, dict) or data.get('v') != VERSION:
            raise ValueError(f'version {data.get("v") if isinstance(data, dict) else "?"}')
        if not (isinstance(data.get('kind'), str) and type(data.get('mb')) is int and data['mb'] > 0
                and data.get('state') in ('waiting', 'admitted') and isinstance(data.get('since'), (int, float))):
            raise ValueError('fields')
        return Ticket(name, data)
    except (ValueError, UnicodeDecodeError) as e:
        note = f'ticket {name} cannot be read ({e}); counted as admitted at {DEFAULT_MB} MB'
        return Ticket(name, {'v': None, 'kind': '?', 'mb': DEFAULT_MB, 'state': 'admitted', 'since': 0,
                             'scope': None}, note=note)


class _stop_signals:
    """While waiting: INT, TERM and HUP end the wait cleanly (logged as stopped), unless the caller ignores them."""
    SIGS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)

    def __enter__(self):
        self.old = {}
        for s in self.SIGS:
            if signal.getsignal(s) != signal.SIG_IGN:
                self.old[s] = signal.signal(s, self._raise)
        return self

    @staticmethod
    def _raise(sig, _frame):
        raise Stopped(sig)

    def __exit__(self, *exc):
        for s, h in self.old.items():
            signal.signal(s, h)


def _dur(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def _replace(path, text):
    tmp = f'{path}.{os.getpid()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, text.encode())
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(tmp, path)


# ---------- peaks ----------

def read_peaks(state_dir, out=say):
    """{kind: [mb, ...]}, and whether the file may be written (not when a newer version wrote it)."""
    path = os.path.join(state_dir, 'peaks.json')
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return {}, True
    except (OSError, ValueError) as e:
        out(f'{path} cannot be read ({e}); starting a new peak history')
        return {}, True
    if not isinstance(data, dict) or not isinstance(data.get('kinds'), dict):
        out(f'{path} has an unknown shape; starting a new peak history')
        return {}, True
    if data.get('v') != VERSION:
        if isinstance(data.get('v'), int) and data['v'] > VERSION:
            out(f'{path} was written by a newer heavy (v{data["v"]}); not learning from it or changing it')
            return {}, False
        return {}, True
    kinds = {}
    for k, runs in data['kinds'].items():
        if isinstance(k, str) and KIND_RE.match(k) and isinstance(runs, list):
            kinds[k] = [r for r in runs if type(r) is int and r > 0][-LEARN_KEEP:]
    return kinds, True


def reservation(kind, start_mb, state_dir, out=say):
    """(MB to reserve, how it was chosen)."""
    runs = read_peaks(state_dir, out)[0].get(kind, [])
    if len(runs) < LEARN_MIN_RUNS:
        return start_mb, f'start value ({len(runs)} measured runs)'
    s = sorted(runs)
    p95 = s[min(len(s) - 1, int(math.ceil(0.95 * len(s))) - 1)]
    mb = int(math.ceil(p95 * LEARN_MARGIN / LEARN_ROUND_MB)) * LEARN_ROUND_MB
    mb = max(start_mb // 2, min(start_mb * 4, mb))
    return mb, f'learned from {len(runs)} runs (95th percentile {p95} MB)'


def record_peak(queue, kind, peak_mb):
    """Add a clean run's peak to the kind's history (under the queue lock: two jobs ending together keep both)."""
    d = queue.box.state_dir
    try:
        with queue:
            kinds, writable = read_peaks(d, queue.out)
            if not writable:
                return
            kinds[kind] = (kinds.get(kind, []) + [int(peak_mb)])[-LEARN_KEEP:]
            os.makedirs(d, mode=0o700, exist_ok=True)
            _replace(os.path.join(d, 'peaks.json'), json.dumps({'v': VERSION, 'kinds': kinds}, sort_keys=True))
    except (OSError, Refused) as e:
        queue.out(f'could not record the peak of {kind} ({e})')


class Sampler:
    """The highest `used` of the job's scope while it runs."""
    interval = SAMPLE_SECONDS

    def __init__(self, queue, ticket):
        self.queue, self.ticket, self.peak = queue, ticket, 0

    def sample(self):
        try:
            used = self.queue.box.scope_used(self.ticket.data.get('scope'))
        except (OSError, Refused):
            used = None
        if used:
            self.peak = max(self.peak, used)

    def record(self):
        if self.peak:
            record_peak(self.queue, self.ticket.kind, _mb(self.peak))


# ---------- the verbs ----------

def status(queue):
    with queue:
        live = queue.tickets()
        facts = queue.box.facts(queue.cfg)
        owed = queue.owed([t for t in live if t.state != 'waiting'])
        rows = []
        for t in live:
            used = queue.box.scope_used(t.data.get('scope')) if t.state != 'waiting' else None
            rows.append({'kind': t.kind, 'mb': t.mb, 'state': t.state, 'pid': t.data.get('pid'),
                         'usedMb': None if used is None else _mb(used), 'since': t.data.get('since'),
                         'note': t.note})
    room = facts['budget'] - facts['used'] - owed
    if facts['avail'] is not None:
        room = min(room, facts['avail'] - queue.cfg['vmReserveMb'] * MB - owed)
    return {'v': VERSION, 'at': _iso(queue.now()), 'slice': queue.box.slice_name,
            'memory': {'budgetMb': _mb(facts['budget']), 'usedMb': _mb(facts['used']),
                       'reservedMb': _mb(owed), 'freeMb': int(room // MB),
                       'availableMb': None if facts['avail'] is None else _mb(facts['avail']),
                       'pressureFull10': facts['full10'], 'pressureSome60': facts['some60']},
            'queue': {'running': sum(r['state'] != 'waiting' for r in rows),
                      'waiting': sum(r['state'] == 'waiting' for r in rows)},
            'jobs': rows}


def queue_main(queue, argv):
    if argv not in ([], ['--json']):
        print('usage: heavy queue [--json]', file=sys.stderr)
        return 2
    st = status(queue)
    if argv:
        print(json.dumps(st, sort_keys=True))
        return 0
    m, now = st['memory'], queue.now()
    print(f'{st["slice"]}: budget {m["budgetMb"]} MB, used {m["usedMb"]}, reserved not yet used {m["reservedMb"]}, '
          f'free for new jobs {max(0, m["freeMb"])}; available {m["availableMb"]} MB; pressure full10 '
          f'{m["pressureFull10"]:.1f}% some60 {m["pressureSome60"]:.1f}%')
    n = 0
    for r in st['jobs']:
        age = _dur(now - r['since']) if r['since'] else '?'
        if r['state'] == 'waiting':
            n += 1
            print(f'  waiting #{n}  {r["kind"]:<16} {r["mb"]:>6} MB  pid {r["pid"]}  for {age}')
        else:
            using = '' if r['usedMb'] is None else f'  using {r["usedMb"]} MB'
            print(f'  running     {r["kind"]:<16} {r["mb"]:>6} MB  pid {r["pid"]}  for {age}{using}')
        if r['note']:
            print(f'    note: {r["note"]}')
    if not st['jobs']:
        print('  no queued jobs')
    return 0


def waits(state_dir, days, now):
    """Per UTC day (newest first): jobs, minutes waited, longest wait, gave-ups, minutes per kind."""
    path = os.path.join(state_dir, 'waits.log')
    first = datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date() - datetime.timedelta(days=days - 1)
    out = {}
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            lines = f.readlines()
    except FileNotFoundError:
        lines = []
    for line in lines:
        try:
            e = json.loads(line)
            day = datetime.date.fromisoformat(e['at'][:10])
            kind, waited, outcome = str(e['kind']), float(e['waitedS']), str(e['outcome'])
        except (ValueError, KeyError, TypeError):
            continue                    # a half-written or foreign line
        if day < first:
            continue
        d = out.setdefault(day.isoformat(), {'day': day.isoformat(), 'jobs': 0, 'waitedMinutes': 0.0,
                                             'longestMinutes': 0.0, 'gaveUp': 0, 'kinds': {}})
        d['jobs'] += 1
        d['waitedMinutes'] += waited / 60
        d['longestMinutes'] = max(d['longestMinutes'], waited / 60)
        d['gaveUp'] += outcome == 'gave-up'
        k = d['kinds'].setdefault(kind, {'jobs': 0, 'waitedMinutes': 0.0})
        k['jobs'] += 1
        k['waitedMinutes'] += waited / 60
    rows = sorted(out.values(), key=lambda d: d['day'], reverse=True)
    for d in rows:
        d['waitedMinutes'] = round(d['waitedMinutes'], 1)
        d['longestMinutes'] = round(d['longestMinutes'], 1)
        for k in d['kinds'].values():
            k['waitedMinutes'] = round(k['waitedMinutes'], 1)
    return rows


def waits_main(state_dir, argv, now=None):
    days, as_json, args = 7, False, list(argv)
    while args:
        a = args.pop(0)
        if a == '--json':
            as_json = True
        elif a == '--days' and args and args[0].isdigit() and 1 <= int(args[0]) <= 366:
            days = int(args.pop(0))
        else:
            print('usage: heavy waits [--days N] [--json]', file=sys.stderr)
            return 2
    rows = waits(state_dir, days, time.time() if now is None else now)
    if as_json:
        print(json.dumps({'v': VERSION, 'days': rows}, sort_keys=True))
        return 0
    print('day (UTC)    jobs  waited-min  longest-min  gave-up  by kind (minutes)')
    for d in rows:
        kinds = ', '.join(f'{k} {v["waitedMinutes"]:g}' for k, v in sorted(d['kinds'].items(),
                                                                          key=lambda kv: -kv[1]['waitedMinutes']))
        print(f'{d["day"]}  {d["jobs"]:>5}  {d["waitedMinutes"]:>10.1f}  {d["longestMinutes"]:>11.1f}  '
              f'{d["gaveUp"]:>7}  {kinds}')
    if not rows:
        print(f'(no queued jobs in the last {days} days)')
    return 0
