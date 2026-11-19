"""heavy's memory queue: a job of a known kind starts only when its memory fits; the others wait in order.

  heavy --kind <name> [--mb N] [--wait S] [--] <command> [args...]
  heavy queue [--json]            what is admitted and what waits, and the memory figures behind it
  heavy waits [--days N] [--json] minutes waited for memory per day (from the wait log)

A job with --kind reserves memory before its scope starts: --mb (the project's start value; default 3072 MB), or
more once a run of the kind was seen to need more (never less). It is admitted when

  reservation <= budget - used - owed     and     reservation <= MemAvailable - vmReserveMb - owed

budget   the slice's memory.high (else memory.max, else machine-file budgetMb) minus headroomMb
used     the slice's memory that cannot simply be dropped: memory.current minus clean file cache, at least its anon,
         plus its swap
owed     for every admitted job: the part of its reservation it has not used yet (its scope's own `used`), so a
         job that has not grown yet still holds its room, and a job that has grown is counted once

and the slice is not under memory pressure (full avg10 > pressureFull10 or some avg60 > pressureSome60), and the
machine has swapFreeMinMb of swap free (not checked on a machine without swap), and then the optional admitCommand
(the machine's own check, argv with {mb} filled in, run under the queue lock with a time limit) exits 0; any other
exit, or no answer in admitTimeoutSeconds, means not now and the job keeps its place (but no room is kept for it
by the jobs behind it until it is admitted; the pass limit still holds for it). Waiting jobs go in order of
arrival. A job that does not fit lets later jobs pass. Once it has waited passLimitSeconds, nothing passes it while
that helps it: while a queued job runs whose end would make room for it. A job that cannot fit even with every
queued job ended (memory outside the queue is short) does not hold the others. A job larger than the whole budget
runs alone: when it is first, nothing else is admitted, the slice is near empty (used <= headroomMb) and the
machine has the whole budget free.

State: one folder per user and slice in /run/user/<uid>/heavy-queue/ (tmpfs: empty after a reboot). Each job holds
an exclusive flock on its own ticket file for its whole life (the command inherits it, so a heavy killed with -9
keeps its reservation until the processes it started are gone); the kernel drops it when the last holder ends in
any way, and whoever takes the queue lock next deletes tickets nobody holds. So a waiter that dies or a job that
crashes frees its place, and nothing in the folder can keep the queue stuck. A live ticket that cannot be read
(garbled, or written by another version) counts as admitted at 3072 MB, with a note.

Waiting polls the real figures every pollSeconds (default 2) and prints the reason and the position when they
change and once a minute. After --wait seconds (default 7200) heavy gives up with exit 76. The queue lock is never
waited for blocking: a waiter gives up on it with the rest of --wait (at least 5 s), the verbs and peaks after 30 s
(+ admitTimeoutSeconds with an admit command), after a stop signal after 2 s; a finished job needs no lock. INT,
TERM and HUP are blocked while waiting and taken with sigtimedwait, so a stop never cuts a lock take in half.

Wait log: every admission, give-up and stop is one JSON line in ~/.local/state/heavy/waits.log (kept to the last
few thousand lines); `heavy waits` sums it per day. Peaks: while an admitted job runs, its scope's `used` is
sampled every few seconds and once more when it ends; the highest sample of every run (failed and killed runs too)
goes into ~/.local/state/heavy/peaks.json (the last 20 per kind). The reservation is the highest of those x 1.15,
rounded up to 128 MB, never below the start value, at most 4x it, and a learned value at most the budget minus
vmReserveMb (one leaky run must not learn a value that is never admitted while other jobs use the slice). Sampling can miss a spike shorter than the
interval, so a kind with short spikes should get a start value above its spikes.

Settings: the optional "queue" object in the machine file's "heavy" section (fork-kit/common/README.md); without
the file or the key, the defaults below hold. Standard library only.
"""
import datetime, errno, fcntl, importlib.machinery, importlib.util, json, math, os, re, signal, stat, sys, time

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
VERSION = 1
MB = 1 << 20
LOCK_RETRY = 0.05           # seconds between two tries of the queue lock
LOCK_TIMEOUT = 30           # the verbs, logging and peaks wait this long for the queue lock (+ admitTimeoutSeconds)
STOP_LOCK_WAIT = 2          # after a stop signal: a short try for the lock (the log line, the peak), never 30 s
HOOK_OUTPUT_GRACE = 1       # seconds an admit command's output may stay open after it exited
LOCK_MIN = 5                # a waiter waits at least this long for the lock, even with --wait 0
STOP_SIGS = (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)
DEFAULT_MB = 3072
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}\Z')
TICKET_RE = re.compile(r'^t-([0-9]{1,15})-([0-9]{1,10})\.json\Z')
EXIT_GAVE_UP = 76
PRINT_EVERY = 60
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


def _argv(value):
    if not value:
        return None                 # [] = no admit command
    if not all(isinstance(w, str) and w and '\0' not in w for w in value):
        return 'must be a list of non-empty strings'
    if not os.path.isabs(value[0]):
        return 'the first word must be an absolute path'
    return None                     # that it can be run is checked per job (ask_hook): a missing one stops that job


def runnable(path):
    return os.path.isfile(path) and os.access(path, os.X_OK)


SETTINGS = {
    'budgetMb': (int, 0, _at_least(0)),                    # 0: from the slice's memory.high / memory.max
    'headroomMb': (int, 512, _at_least(0)),
    'vmReserveMb': (int, 1024, _at_least(0)),
    'swapFreeMinMb': (int, 1024, _at_least(0)),             # 0: no swap check; skipped on a machine without swap
    'pressureFull10': ((int, float), 5, _at_least(0, 100)),
    'pressureSome60': ((int, float), 10, _at_least(0, 100)),
    'passLimitSeconds': ((int, float), 600, _at_least(0)),
    'pollSeconds': ((int, float), 2, _at_least(0.2, 30)),
    'admitCommand': (list, [], _argv),                      # the machine's own check: exit 0 = admit now
    'admitTimeoutSeconds': ((int, float), 20, _at_least(1, 300)),
}
SECTION = {'sweep': (dict, None, None), 'queue': (dict, None, None)}


class Refused(Exception):
    pass


class GaveUp(Exception):
    pass


class LockBusy(TimeoutError):
    """The queue lock was not free in time (an OSError: every caller already handles those)."""


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


def meminfo_figures(meminfo):
    """{'MemAvailable', 'SwapTotal', 'SwapFree'} in bytes; a figure that cannot be read is missing."""
    out = {}
    try:
        for line in _read(meminfo).splitlines():
            key, _, rest = line.partition(':')
            if key in ('MemAvailable', 'SwapTotal', 'SwapFree'):
                try:
                    out[key] = int(rest.split()[0]) * 1024
                except (ValueError, IndexError):
                    pass
    except OSError:
        pass
    return out


def mem_available(meminfo):
    return meminfo_figures(meminfo).get('MemAvailable')


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

    def refresh(self):
        """Ask again for the slice's cgroup while it is not active. It may ask systemd, so never under the lock."""
        if not self.slice_cg and self.resolve is not None:
            self.slice_cg = self.resolve() or ''

    def folder(self, scope=None):
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
        mem = meminfo_figures(self.meminfo)
        return {'budget': budget - cfg['headroomMb'] * MB, 'used': used or 0, 'full10': full10, 'some60': some60,
                'avail': mem.get('MemAvailable'), 'swapTotal': mem.get('SwapTotal'), 'swapFree': mem.get('SwapFree')}

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
    def __init__(self, box, cfg, now=time.time, sleep=None, monotonic=time.monotonic, out=say, hook=None):
        self.box, self.cfg = box, cfg
        self.now, self.sleep, self.monotonic, self.out = now, sleep, monotonic, out
        self.hook = hook or run_hook
        self.lock_fd = None
        self.sigs = None            # the stop signals, while enter() keeps them blocked

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

    def pause(self, seconds):
        """Wait. While enter() holds the stop signals blocked, one that comes ends the wait with Stopped, raised
        here and nowhere else (never from a signal handler, so a lock take or release is never cut in half)."""
        seconds = max(0.0, seconds)
        if not self.sigs:
            (self.sleep or time.sleep)(seconds)
            return
        if self.sleep is None:
            info = signal.sigtimedwait(self.sigs, seconds)
        else:                       # tests: a fake clock, then a look at what is pending
            self.sleep(seconds)
            info = signal.sigtimedwait(self.sigs, 0)
        if info is not None:
            raise Stopped(info.si_signo)

    def stop_pending(self):
        if self.sigs:
            info = signal.sigtimedwait(self.sigs, 0)
            if info is not None:
                raise Stopped(info.si_signo)

    def take(self, deadline):
        """Take the queue lock, trying until `deadline` (monotonic); LockBusy after that. Never blocks in flock."""
        d = self._folder()
        fd = os.open(os.path.join(d, 'lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as e:
                    if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                        raise
                if self.monotonic() >= deadline:
                    raise LockBusy(f'the queue lock ({os.path.join(d, "lock")}) stayed taken')
                self.pause(LOCK_RETRY)
        except BaseException:
            os.close(fd)
            raise
        self.lock_fd = fd

    def drop(self):
        fd, self.lock_fd = self.lock_fd, None
        if fd is not None:
            os.close(fd)

    def lock_wait(self):
        """How long the verbs and the peaks wait for the queue lock: longer than one admit command can hold it."""
        return LOCK_TIMEOUT + (self.cfg['admitTimeoutSeconds'] if self.cfg.get('admitCommand') else 0)

    def __enter__(self):
        self.take(self.monotonic() + self.lock_wait())
        return self

    def __exit__(self, *exc):
        self.drop()

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
                'sinceMono': self.monotonic(), 'state': 'waiting', 'admittedAt': None, 'scope': scope, 'cmd': cmd}
        t = Ticket(name, data, fd)
        self.write(t)
        return t

    def write(self, t):
        raw = json.dumps(t.data, sort_keys=True).encode()
        os.ftruncate(t.fd, 0)
        os.pwrite(t.fd, raw, 0)

    # ----- the decision -----

    def use_of(self, admitted):
        """(owed, used): for the admitted jobs, the part of their reservations not used yet, and what their scopes
        use now."""
        owed = used_now = 0
        for t in admitted:
            used = self.box.scope_used(t.data.get('scope')) or 0
            owed += max(0, t.mb * MB - used)
            used_now += used
        return owed, used_now

    def owed(self, admitted):
        return self.use_of(admitted)[0]

    def waited(self, t):
        """How long ticket t has waited, on the monotonic clock (a wall clock step does not move the pass limit)."""
        mono = t.data.get('sinceMono')
        if isinstance(mono, (int, float)) and not isinstance(mono, bool):
            return self.monotonic() - mono
        return self.now() - t.since

    def decide(self, me, live, facts):
        """(admit?, reason, figures) for ticket `me` among the live tickets, by the queue's own rule (the admit
        command, when there is one, is asked after this)."""
        cfg = self.cfg
        admitted = [t for t in live if t.state != 'waiting']
        waiting = [t for t in live if t.state == 'waiting']
        owed, admitted_used = self.use_of(admitted)
        budget, used, avail = facts['budget'], facts['used'], facts['avail']
        headroom, reserve = cfg['headroomMb'] * MB, cfg['vmReserveMb'] * MB
        slice_room = budget - used - owed
        box_room = None if avail is None else avail - reserve - owed
        room = slice_room if box_room is None else min(slice_room, box_room)
        # the room once every admitted queued job has ended (their use and what they still owe come back)
        room_after = budget - max(0, used - admitted_used)
        if avail is not None:
            room_after = min(room_after, avail + admitted_used - reserve)
        used_after = max(0, used - admitted_used)
        fig = {'budgetMb': _mb(budget), 'usedMb': _mb(used), 'owedMb': _mb(owed), 'roomMb': int(room // MB),
               'availMb': None if avail is None else _mb(avail)}
        ahead = [t for t in waiting if t.seq < me.seq]
        fig['position'], fig['waiting'] = len(ahead) + 1, len(waiting)
        if facts['full10'] > cfg['pressureFull10'] or facts['some60'] > cfg['pressureSome60']:
            return False, (f'the slice is under memory pressure (full avg10 {facts["full10"]:.1f}%, '
                           f'some avg60 {facts["some60"]:.1f}%)'), fig
        swap_total, swap_free = facts.get('swapTotal'), facts.get('swapFree')
        if cfg['swapFreeMinMb'] and swap_total and swap_free is not None and swap_free < cfg['swapFreeMinMb'] * MB:
            return False, f'swap free {_mb(swap_free)} MB is under swapFreeMinMb {cfg["swapFreeMinMb"]} MB', fig

        def need(t):
            return min(t.mb * MB, budget)

        def alone_fits(t, shares):
            """A job larger than the budget: nothing else admitted or ahead of it, the slice near empty, and the
            machine has the whole budget free."""
            return (not admitted and not shares and used <= headroom
                    and (box_room is None or need(t) <= box_room))

        def holding_helps(t, shares):
            """Nothing passes t only while that helps it: queued jobs run, or are about to (the ones ahead of it that
            fit), and t fits once they have all ended. A job that cannot fit even then does not hold the queue."""
            if not admitted and not shares:
                return False
            if t.mb * MB > budget:
                return used_after <= headroom and (avail is None or need(t) <= avail + admitted_used - reserve)
            return need(t) <= room_after

        shares = 0
        for t in ahead:
            # a waiter the admit command refused on its last look does not take its room on its next one either:
            # it gets no share and holds the queue only by the pass limit, as any waiter that does not fit
            refused = t.data.get('hookNo') is True
            if t.mb * MB > budget:
                if not refused and alone_fits(t, shares):
                    return False, f'{t.kind} ({t.mb} MB, larger than the budget) is first and runs alone', fig
            elif not refused and need(t) <= room - shares:
                shares += need(t)       # its share: it takes it on its own next look
                continue
            if self.waited(t) >= cfg['passLimitSeconds'] and holding_helps(t, shares):
                return False, f'{t.kind} ({t.mb} MB) has waited past the pass limit; nothing passes it', fig
        fig['roomMb'] = int((room - shares) // MB)
        if me.mb * MB > budget:
            if alone_fits(me, shares):
                return True, 'larger than the budget: runs alone', fig
            return False, (f'{me.mb} MB is larger than the budget ({fig["budgetMb"]} MB): it runs alone once it is '
                           f'first, nothing else runs, the slice is near empty and the machine has the budget free'), fig
        if need(me) <= room - shares:
            return True, 'fits', fig
        why = f'needs {me.mb} MB, room {max(0, fig["roomMb"])} MB'
        return False, why, fig

    def ask_hook(self, mb):
        """(admit?, reason) from the machine's admit command; no command means yes."""
        argv = self.cfg.get('admitCommand') or []
        if not argv:
            return True, 'fits'
        if not runnable(argv[0]):
            raise Refused(f'heavy.queue.admitCommand: {argv[0]} is not a program this user can run, so this job '
                          f'cannot be checked; not running it (fix the program or remove the setting)')
        code, line = self.hook([w.replace('{mb}', str(mb)) for w in argv], self.cfg['admitTimeoutSeconds'])
        if code == 0:
            return True, 'fits, and the admit command says yes'
        what = f'exit {code}' if code is not None else line
        detail = f': {line}' if code is not None and line else ''
        return False, f'the admit command says not now ({what}{detail})'

    def look(self, t):
        """One look for waiting ticket t (under the lock): the queue's rule, then the admit command. Whether the admit
        command refused it goes into its ticket (hookNo), so the jobs behind it do not keep its room for it."""
        ok, why, fig = self.decide(t, self.tickets(), self.box.facts(self.cfg))
        refused = False
        if ok:
            ok, why = self.ask_hook(t.mb)
            refused = not ok
        if t.data.get('hookNo', False) != refused:
            t.data['hookNo'] = refused
            self.write(t)
        return ok, why, fig

    def reservation(self, kind, start_mb):
        """reservation() for this queue's slice: a learned value stays at most the budget minus vmReserveMb."""
        cap = self.box.facts(self.cfg)['budget'] // MB - self.cfg['vmReserveMb']
        return reservation(kind, start_mb, self.box.state_dir, self.out, cap_mb=cap)

    # ----- waiting -----

    def enter(self, kind, mb, scope, cmd, wait_s, keep_blocked=False):
        """Wait until the job fits; returns its held Ticket. Raises GaveUp after wait_s, Stopped on INT/TERM/HUP.
        The stop signals stay blocked the whole time and are only taken in pause(), so no signal can land between
        taking a lock and keeping its fd. keep_blocked: once admitted they stay blocked (heavy takes them itself,
        so a Ctrl-C just after the admission is no traceback); every other end restores the mask."""
        sigs = [s for s in STOP_SIGS if signal.getsignal(s) != signal.SIG_IGN]
        old_mask = signal.pthread_sigmask(signal.SIG_BLOCK, sigs)
        self.sigs = sigs
        start = self.monotonic()
        lock_deadline = max(start + wait_s, start + LOCK_MIN)
        t = None
        admitted = False
        try:
            with _Locked(self, lock_deadline):
                t = self.create(kind, mb, scope, cmd)
            last, last_print = None, None
            while True:
                self.box.refresh()                      # may ask systemd: outside the lock
                with _Locked(self, lock_deadline):
                    ok, why, fig = self.look(t)
                    waited = self.monotonic() - start
                    if ok:
                        self.stop_pending()             # a stop that came while deciding wins over the admission
                        t.data.update(state='admitted', admittedAt=self.now())
                        self.write(t)
                        self.log(kind, mb, waited, 'admitted')
                        if waited >= 1:
                            self.out(f'admitted {kind} ({mb} MB) after waiting {_dur(waited)} for memory')
                        admitted = True
                        return t
                    if waited >= wait_s:
                        self.log(kind, mb, waited, 'gave-up')
                        whole = ''
                        if mb >= fig['budgetMb']:
                            whole = (f' (its reservation {mb} MB is not below the whole budget of {fig["budgetMb"]} '
                                     f'MB: it runs only when the slice is near empty; lower --mb or raise the '
                                     f"slice's memory.high)")
                        raise GaveUp(f'gave up after waiting {_dur(waited)} for memory: {kind} {why}{whole}')
                key = (why.split(',')[0].split('(')[0], fig['position'])
                if key != last or last_print is None or self.monotonic() - last_print >= PRINT_EVERY:
                    self.out(f'waiting for memory: {kind} {why} (budget {fig["budgetMb"]} MB, used '
                             f'{fig["usedMb"]}, reserved not yet used {fig["owedMb"]}, available '
                             f'{fig["availMb"]}); position {fig["position"]} of {fig["waiting"]}, waited '
                             f'{_dur(waited)}')
                    last, last_print = key, self.monotonic()
                self.pause(min(self.cfg['pollSeconds'], max(0.0, wait_s - waited) + 0.01))
        except Stopped:
            self.sigs = None                            # clean-up below waits without ending on a second signal
            try:
                with _Locked(self, self.monotonic() + STOP_LOCK_WAIT):
                    self.log(kind, mb, self.monotonic() - start, 'stopped')
            except Exception:
                pass
            release(self, t)
            raise
        except LockBusy as e:
            self.sigs = None
            release(self, t)
            raise GaveUp(f'gave up after waiting {_dur(self.monotonic() - start)}: {e}') from None
        except BaseException:
            self.sigs = None
            release(self, t)
            raise
        finally:
            self.sigs = None
            if not (admitted and keep_blocked):
                signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)

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


class _Locked:
    """The queue lock until a deadline (monotonic)."""

    def __init__(self, queue, deadline):
        self.queue, self.deadline = queue, deadline

    def __enter__(self):
        self.queue.take(self.deadline)
        return self.queue

    def __exit__(self, *exc):
        self.queue.drop()


def run_hook(argv, timeout):
    """(exit code, last output line) of the admit command; (None, why) when it could not run, did not answer in
    time, or exited but left a process that holds its output open (its whole process group is then killed). Its
    output is read with a deadline, never until an EOF that may not come. It gets no stdin and an open signal mask."""
    import select, subprocess
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             close_fds=True, start_new_session=True,
                             preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, []))
    except OSError as e:
        return None, f'cannot be run: {e.strerror or e}'

    def cut(why):
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.wait()
        return None, why

    fd, out = p.stdout.fileno(), b''
    deadline = time.monotonic() + timeout
    exited_at = None
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                return cut(f'no answer in {timeout:g} s')
            if p.poll() is not None:
                exited_at = exited_at or now
                if now - exited_at >= HOOK_OUTPUT_GRACE:
                    return cut(f'exit {p.returncode}, but a process it started still holds its output open')
            if select.select([fd], [], [], min(0.1, deadline - now))[0]:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
                out = (out + chunk)[-65536:]
        try:
            p.wait(timeout=max(0.0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            return cut(f'no answer in {timeout:g} s')
    finally:
        p.stdout.close()
    lines = [x.strip() for x in out.decode('utf-8', 'replace').splitlines() if x.strip()]
    return p.returncode, (lines[-1][:200] if lines else '')


def stopped_already():
    """Raise Stopped for an INT/TERM/HUP that is pending (blocked, as enter(keep_blocked=True) leaves them)."""
    sigs = [s for s in STOP_SIGS if signal.getsignal(s) != signal.SIG_IGN]
    info = signal.sigtimedwait(sigs, 0) if sigs else None
    if info is not None:
        raise Stopped(info.si_signo)


def close(t):
    """Drop the ticket's flock (the file stays until the next look deletes it)."""
    if t is not None and t.fd is not None:
        fd, t.fd = t.fd, None
        os.close(fd)


def release(queue, t):
    """The job ended: delete the ticket and drop its flock. The delete needs no queue lock, so a slow admit command
    never delays it and leftovers that inherited the fd (HEAVY_KEEP=1) hold nothing: an unlink is atomic, and a look
    that opened the ticket just before still finds it held (counted once more at most). A crash skips this; the next
    look deletes the ticket instead."""
    if t is None or t.fd is None:
        return
    _unlink(os.path.join(queue.box.queue_dir, t.name))
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


def _dur(seconds):
    seconds = int(seconds)
    if seconds < 60:
        return f'{seconds}s'
    if seconds < 3600:
        return f'{seconds // 60}m{seconds % 60:02d}s'
    return f'{seconds // 3600}h{seconds % 3600 // 60:02d}m'


def _iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


TMP_RE = re.compile(r'^(peaks\.json|waits\.log)\.([0-9]{1,10})\.tmp\Z')


def _replace(path, text):
    """Write a whole file by renaming a temporary one over it. The temporary file is removed when the write fails;
    one left by a writer that was killed is removed by the next writer (_drop_stale_tmp)."""
    _drop_stale_tmp(os.path.dirname(path))
    tmp = f'{path}.{os.getpid()}.tmp'
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            os.write(fd, text.encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        _unlink(tmp)
        raise


def _drop_stale_tmp(folder):
    """Temporary files of writers whose process is gone (killed half-way through a write)."""
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for name in names:
        m = TMP_RE.match(name)
        if not m or int(m.group(2)) == os.getpid():
            continue
        try:
            os.kill(int(m.group(2)), 0)
            continue                    # its writer still runs
        except ProcessLookupError:
            pass
        except OSError:
            continue                    # another user's pid: alive
        _unlink(os.path.join(folder, name))


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


def reservation(kind, start_mb, state_dir, out=say, cap_mb=None):
    """(MB to reserve, how it was chosen): the highest peak of the kind's recent runs x 1.15, rounded up to 128 MB;
    never below the start value (learning only ever raises it), at most 4x it, and at most cap_mb (Queue.reservation:
    the budget minus vmReserveMb), so one run that leaked cannot learn a value that is never admitted."""
    runs = read_peaks(state_dir, out)[0].get(kind, [])
    if not runs:
        return start_mb, 'start value (no measured runs yet)'
    top = max(runs)
    mb = int(math.ceil(top * LEARN_MARGIN / LEARN_ROUND_MB)) * LEARN_ROUND_MB
    if mb <= start_mb:
        return start_mb, f'start value (highest of the last {len(runs)} runs: {top} MB)'
    how = f'learned (highest of the last {len(runs)} runs: {top} MB, x {LEARN_MARGIN})'
    mb = min(start_mb * 4, mb)
    if cap_mb is not None and mb > cap_mb:
        mb = max(start_mb, cap_mb // LEARN_ROUND_MB * LEARN_ROUND_MB)
        how += f', capped at the budget minus vmReserveMb ({mb} MB)'
    return mb, how


def record_peak(queue, kind, peak_mb, wait=None):
    """Add a run's peak to the kind's history (under the queue lock: two jobs ending together keep both). wait: how
    long to try for the lock (default Queue.lock_wait(); after a stop signal STOP_LOCK_WAIT)."""
    d = queue.box.state_dir
    try:
        with _Locked(queue, queue.monotonic() + (queue.lock_wait() if wait is None else wait)):
            kinds, writable = read_peaks(d, queue.out)
            if not writable:
                return
            kinds[kind] = (kinds.get(kind, []) + [int(peak_mb)])[-LEARN_KEEP:]
            os.makedirs(d, mode=0o700, exist_ok=True)
            _replace(os.path.join(d, 'peaks.json'), json.dumps({'v': VERSION, 'kinds': kinds}, sort_keys=True))
    except Exception as e:          # OSError, LockBusy, Refused (heavy's own class)
        queue.out(f'could not record the peak of {kind} ({e})')


class Sampler:
    """The highest `used` of the job's scope while it runs (every SAMPLE_SECONDS and once more when the command
    ends; a spike shorter than that can be missed)."""
    interval = SAMPLE_SECONDS

    def __init__(self, queue, ticket):
        self.queue, self.ticket, self.peak = queue, ticket, 0

    def sample(self):
        try:
            used = self.queue.box.scope_used(self.ticket.data.get('scope'))
        except Exception:
            used = None
        if used:
            self.peak = max(self.peak, used)

    def record(self, wait=None):
        if self.peak:
            record_peak(self.queue, self.ticket.kind, _mb(self.peak), wait)

    def record_soon(self):
        """record() after a stop signal: a short try for the lock, so a "stop now" never waits on the queue."""
        self.record(STOP_LOCK_WAIT)


# ---------- the verbs ----------

def status(queue):
    queue.box.refresh()
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
                       'pressureFull10': facts['full10'], 'pressureSome60': facts['some60'],
                       'swapFreeMb': None if facts['swapFree'] is None else _mb(facts['swapFree'])},
            'settings': dict(queue.cfg),
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
          f'{m["pressureFull10"]:.1f}% some60 {m["pressureSome60"]:.1f}%; swap free {m["swapFreeMb"]} MB')
    if st['settings']['admitCommand']:
        argv = st['settings']['admitCommand']
        bad = '' if runnable(argv[0]) else '  (NOT a program this user can run: queued jobs that fit stop with exit 1)'
        print(f'  admit command: {" ".join(argv)}{bad}')
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
