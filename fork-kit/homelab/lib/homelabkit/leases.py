"""Test-machine leases: who may use a shared test machine now, and until when (fork-kit/homelab/README.md, Leases).

One file per leased machine, <dir>/<machine>.json, in the lease folder the machine file names; any user who can
write that folder (the owner gives the projects' users a group on it) can take a free machine. A lease ends at its
`until` time: after that the machine is free again, whether or not the holder released it. Every change happens
under one lock (flock on <dir>/.lock) and is written to a temporary file first and renamed over the lease file, so
two takers at once never both win and a stop half-way leaves the old lease or the new one, never half a file.

This is a convention between cooperating agents, not a security boundary: anyone who can write the folder can
remove a lease file by hand.
"""
import contextlib, errno, fcntl, json, os, pwd, re, secrets, stat, time

now = time.time             # tests replace it
HOLDER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._@/+-]{0,79}')
MAX_PURPOSE = 200
MAX_LEASE_BYTES = 64 * 1024
LOCK = '.lock'
_OPEN = getattr(os, 'O_CLOEXEC', 0) | os.O_NOFOLLOW


class LeaseError(Exception):
    """The request or the lease folder cannot be used (exit 2); the message names what and why."""


class Refused(Exception):
    """The lease is someone else's, or not there to renew (exit 1)."""


def when(t):
    return time.strftime('%Y-%m-%d %H:%M:%SZ', time.gmtime(t))


def describe(lease):
    return f'{lease["holder"]} ({lease["user"]}) until {when(lease["until"])}'


def _user():
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        return str(os.geteuid())


class Store:
    def __init__(self, folder, machines, default_minutes, max_minutes):
        self.folder, self.machines = folder, list(machines)
        self.default_minutes, self.max_minutes = default_minutes, max_minutes

    # --- the folder and its files

    def _open_dir(self):
        try:
            fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | _OPEN)
        except FileNotFoundError:
            raise LeaseError(f'{self.folder}: not there (the owner makes the lease folder: '
                             'fork-kit/homelab/README.md, Leases)') from None
        except OSError as e:
            if e.errno in (errno.ELOOP, errno.ENOTDIR):
                kind = 'a symbolic link' if os.path.islink(self.folder) else 'not a folder'
                raise LeaseError(f'{self.folder}: is {kind}') from None
            raise LeaseError(f'{self.folder}: cannot be opened ({e.strerror})') from None
        if os.fstat(fd).st_mode & stat.S_IWOTH:
            os.close(fd)
            raise LeaseError(f'{self.folder}: is writable by everyone; give the projects\' users a group instead '
                             '(fork-kit/homelab/README.md, Leases)')
        return fd

    def check_folder(self):
        """None when this user can take leases in the folder, else the problem."""
        try:
            os.close(self._open_dir())
        except LeaseError as e:
            return str(e)
        if not os.access(self.folder, os.W_OK | os.X_OK):
            return f'{self.folder}: this user ({_user()}) cannot write it, so cannot take a lease'
        return None

    @contextlib.contextmanager
    def _locked(self):
        dir_fd = self._open_dir()
        try:
            try:
                lock = os.open(LOCK, os.O_RDONLY | os.O_CREAT | os.O_NONBLOCK | _OPEN, 0o644, dir_fd=dir_fd)
            except OSError as e:
                raise LeaseError(f'{os.path.join(self.folder, LOCK)}: cannot be opened ({e.strerror})') from None
            try:
                if not stat.S_ISREG(os.fstat(lock).st_mode):
                    raise LeaseError(f'{os.path.join(self.folder, LOCK)}: is not a plain file')
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield dir_fd
            finally:
                os.close(lock)
        finally:
            os.close(dir_fd)

    def _read(self, dir_fd, machine):
        """The lease record of `machine` (live or expired), or None. A file that is not a whole lease fails closed:
        nobody can tell who holds the machine, so the owner looks and removes it."""
        name = f'{machine}.json'
        path = os.path.join(self.folder, name)
        try:
            fd = os.open(name, os.O_RDONLY | os.O_NONBLOCK | _OPEN, dir_fd=dir_fd)
        except FileNotFoundError:
            return None
        except OSError as e:
            if e.errno == errno.ELOOP:
                raise LeaseError(f'{path}: is a symbolic link; remove it') from None
            raise LeaseError(f'{path}: cannot be read ({e.strerror})') from None
        with os.fdopen(fd, 'rb') as f:
            if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
                raise LeaseError(f'{path}: is not a plain file; remove it')
            data = f.read(MAX_LEASE_BYTES + 1)
        try:
            lease = json.loads(data.decode('utf-8'))
            if not (isinstance(lease, dict) and lease.get('machine') == machine
                    and isinstance(lease.get('holder'), str) and isinstance(lease.get('user'), str)
                    and all(type(lease.get(k)) in (int, float) for k in ('taken', 'until'))):
                raise ValueError('not a lease of this machine')
        except ValueError as e:
            raise LeaseError(f'{path}: is not a whole lease ({e}); check who uses {machine}, then remove '
                             'the file') from None
        return lease

    def _write(self, dir_fd, machine, lease):
        tmp = f'.{machine}.json.{secrets.token_hex(6)}.tmp'
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN, 0o644, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(lease, f, sort_keys=True)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, f'{machine}.json', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise

    # --- requests

    def _machine(self, machine):
        if machine not in self.machines:
            raise LeaseError(f'unknown machine {machine!r} (the machine file names: {", ".join(self.machines)})')
        return machine

    def _minutes(self, minutes):
        minutes = self.default_minutes if minutes is None else minutes
        if minutes < 1:
            raise LeaseError(f'--minutes must be at least 1 (it is {minutes})')
        if minutes > self.max_minutes:
            raise LeaseError(f'--minutes must be at most {self.max_minutes}, the machine file\'s '
                             f'homelab.leases.maxMinutes (it is {minutes})')
        return minutes

    @staticmethod
    def _holder(holder):
        if not HOLDER.fullmatch(holder):
            raise LeaseError(f'--holder {holder!r}: must be one word of letters, digits and . _ @ / + - '
                             '(at most 80), for example the project and agent name')
        return holder

    @staticmethod
    def live(lease):
        return lease is not None and lease['until'] > now()

    def take(self, machine, holder, minutes, purpose):
        """The new lease, and the expired one it replaces (or None). Refused while the machine is leased, also to
        the same holder (a second take is a mistake; renew extends a lease)."""
        machine, holder, minutes = self._machine(machine), self._holder(holder), self._minutes(minutes)
        if len(purpose) > MAX_PURPOSE or not purpose.isprintable():
            raise LeaseError(f'--purpose: must be one printable line of at most {MAX_PURPOSE} characters')
        with self._locked() as dir_fd:
            old = self._read(dir_fd, machine)
            if self.live(old):
                if old['holder'] == holder:
                    raise Refused(f'{machine} is already leased to {describe(old)}: renew it '
                                  f'(homelab lease renew {machine} --holder {holder}) or release it')
                raise Refused(f'{machine} is leased to {describe(old)}')
            t = int(now())
            lease = {'version': 1, 'machine': machine, 'holder': holder, 'user': _user(), 'purpose': purpose,
                     'taken': t, 'until': t + minutes * 60}
            self._write(dir_fd, machine, lease)
        return lease, old

    def renew(self, machine, holder, minutes):
        machine, holder, minutes = self._machine(machine), self._holder(holder), self._minutes(minutes)
        with self._locked() as dir_fd:
            old = self._read(dir_fd, machine)
            if not self.live(old):
                raise Refused(f'{machine} is not leased (or its lease ended): take it again')
            if old['holder'] != holder:
                raise Refused(f'{machine} is leased to {describe(old)}, not to {holder}')
            lease = dict(old, until=int(now()) + minutes * 60)
            self._write(dir_fd, machine, lease)
        return lease

    def release(self, machine, holder):
        """True when the holder's lease was ended, False when there was none (an expired one is removed too)."""
        machine, holder = self._machine(machine), self._holder(holder)
        with self._locked() as dir_fd:
            old = self._read(dir_fd, machine)
            if self.live(old) and old['holder'] != holder:
                raise Refused(f'{machine} is leased to {describe(old)}, not to {holder}')
            if old is not None:
                os.unlink(f'{machine}.json', dir_fd=dir_fd)
            return self.live(old)

    def lease_of(self, machine):
        """The live lease of `machine`, or None. No lock: a lease file is only ever replaced whole."""
        dir_fd = self._open_dir()
        try:
            lease = self._read(dir_fd, self._machine(machine))
        finally:
            os.close(dir_fd)
        return lease if self.live(lease) else None
