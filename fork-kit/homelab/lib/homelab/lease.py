"""homelab.lease: who may use a shared test machine now (fork-kit/homelab/README.md, Leases).

One lease per machine, in the machine file's leaseDir: <name>.json holds the holder and the time it ends, and
<name>.lock is the lock every change takes (flock), so two takers at once never both win. A lease ends by itself
at its time: an expired lease is free for anyone. Taking a lease you hold renews it; taking one another holder
holds is refused (Held), with who holds it and until when. The file is replaced in one step (write beside it, then
rename), so a reader never sees half a lease, and a take stopped half-way leaves the old lease or the new one; its
temp file is removed by the next take or release of that machine, under the lock.

Every file is reached through one open handle on the folder (dir_fd), so a folder swapped for a link while a command
runs is not followed. A folder anyone can write (o+w) is refused: anyone could forge or remove a lease there.

Leases are an agreement between agents, not a wall: anyone who can write the folder can remove a lease. Plain
Python 3 standard library (fcntl: Linux and macOS).
"""
import contextlib, errno, fcntl, json, os, re, stat, time

from homelab import config

HOLDER_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$')
FILE_MODE = 0o664       # the folder is shared by the agents' group (README, Leases)
MAX_BYTES = 64 * 1024
MAX_NOTE = 200
_OPEN = os.O_NOFOLLOW | os.O_CLOEXEC


class LeaseError(Exception):
    """The lease cannot be read or changed; the message says why."""


class Held(LeaseError):
    """Another holder holds the machine's lease now."""

    def __init__(self, lease):
        self.lease = lease
        super().__init__(f'{lease["machine"]} is leased by {lease["holder"]} until {stamp(lease["until"])}')


def stamp(t):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(t))


def check_holder(holder):
    if not isinstance(holder, str) or not HOLDER_RE.match(holder):
        raise LeaseError(f'holder {holder!r}: must start with a letter or digit, then letters, digits and . _ : / @ '
                         '+ -, at most 128 (for example project/agent-name)')


def check_note(note):
    if not isinstance(note, str) or len(note) > MAX_NOTE or not note.isprintable():
        raise LeaseError(f'--note: one printable line of at most {MAX_NOTE} characters (it is shown in other '
                         'agents\' terminals)')


def open_dir(cfg):
    """An open handle (fd) on the lease folder, checked: there, a folder, not a link, and not writable by everyone.
    The caller closes it."""
    d = cfg['leaseDir']
    try:
        fd = os.open(d, os.O_RDONLY | os.O_DIRECTORY | _OPEN)
    except FileNotFoundError:
        raise LeaseError(f'lease folder {d} is not there; the owner makes it, as root: '
                         f'install -d -m 2775 -g AGENTS_GROUP {d}') from None
    except OSError as e:
        if e.errno in (errno.ELOOP, errno.ENOTDIR):
            raise LeaseError(f'lease folder {d} is ' + ('a symbolic link' if os.path.islink(d) else 'not a folder')
                             ) from None
        raise LeaseError(f'lease folder {d}: cannot be opened ({e.strerror})') from None
    if os.fstat(fd).st_mode & stat.S_IWOTH:
        os.close(fd)
        raise LeaseError(f'lease folder {d} is writable by everyone, so anyone could forge or remove a lease; the '
                         f'owner makes it group-only, as root: chmod 2775 {d}')
    return fd


@contextlib.contextmanager
def _dir(cfg):
    fd = open_dir(cfg)
    try:
        yield fd
    finally:
        os.close(fd)


def writable(d, dir_fd):
    """Can this user make and remove files in the opened folder?"""
    if os.access in os.supports_dir_fd:
        return os.access('.', os.W_OK | os.X_OK, dir_fd=dir_fd)
    return os.access(d, os.W_OK | os.X_OK)


def leasable(cfg, name):
    m = config.find(cfg, name)
    if not m['lease']:
        raise LeaseError(f'{name} cannot be leased (it has no "lease" in the machine file)')
    return m


@contextlib.contextmanager
def _locked(d, dir_fd, name):
    path = os.path.join(d, f'{name}.lock')
    try:                                      # O_NONBLOCK: a FIFO planted here must not make the open wait forever
        fd = os.open(f'{name}.lock', os.O_RDONLY | os.O_CREAT | os.O_NONBLOCK | _OPEN, FILE_MODE, dir_fd=dir_fd)
    except OSError as e:
        why = 'is a symbolic link' if e.errno == errno.ELOOP else f'cannot be opened ({e.strerror})'
        raise LeaseError(f'{path}: {why}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise LeaseError(f'{path}: is not a plain file; look at it, then remove it')
        with contextlib.suppress(OSError):
            os.fchmod(fd, FILE_MODE)          # whoever made it: the other agents must open it too
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)                          # frees the lock


def _sweep(d, dir_fd, name):
    """Remove the temp files of takes of `name` that stopped half-way, whatever their process id. Called under the
    machine's lock: a take writes its temp file only while it holds that lock, so none of them is still in use."""
    stale = re.compile(rf'\.{re.escape(name)}\.json\.\d+\.tmp')
    for n in os.listdir(dir_fd if os.listdir in os.supports_fd else d):
        if stale.fullmatch(n):
            with contextlib.suppress(FileNotFoundError):
                os.unlink(n, dir_fd=dir_fd)   # a link is removed, never followed


def _read(d, dir_fd, name):
    """The lease in <name>.json as written, or None when there is none."""
    path = os.path.join(d, f'{name}.json')
    try:
        fd = os.open(f'{name}.json', os.O_RDONLY | os.O_NONBLOCK | _OPEN, dir_fd=dir_fd)
    except FileNotFoundError:
        return None
    except OSError as e:
        why = 'is a symbolic link' if e.errno == errno.ELOOP else f'cannot be read ({e.strerror})'
        raise LeaseError(f'{path}: {why}') from None
    with os.fdopen(fd, 'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            raise LeaseError(f'{path}: is not a plain file')
        data = f.read(MAX_BYTES + 1)
    try:
        lease = json.loads(data.decode('utf-8'))
        ok = (isinstance(lease, dict) and lease.get('version') == 1 and lease.get('machine') == name
              and isinstance(lease.get('holder'), str)
              and all(type(lease.get(k)) in (int, float) for k in ('since', 'until')))
    except (ValueError, UnicodeDecodeError):
        ok = False
    if not ok or len(data) > MAX_BYTES:
        raise LeaseError(f'{path}: not a lease this kit wrote; look at it, then remove it to free {name}')
    return lease


def _write(dir_fd, name, lease):
    tmp = f'.{name}.json.{os.getpid()}.tmp'   # none is left: _sweep ran under this lock
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _OPEN, FILE_MODE, dir_fd=dir_fd)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            os.fchmod(f.fileno(), FILE_MODE)
            json.dump(lease, f, indent=1, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, f'{name}.json', src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _user():
    import pwd
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def take(cfg, name, holder, now, minutes=None, note=''):
    """Take or renew the lease of machine `name` for `holder`, for `minutes` (the machine's default when None).
    Returns (lease, renewed, expired): renewed is True when holder held it already; expired is the earlier lease
    that had ended, or None. Raises Held when another holder holds it now, LeaseError for anything else."""
    check_holder(holder)
    check_note(note)
    m = leasable(cfg, name)
    rules = m['lease']
    minutes = rules['defaultMinutes'] if minutes is None else minutes
    if type(minutes) is not int or not 1 <= minutes <= rules['maxMinutes']:
        raise LeaseError(f'{name}: a lease is 1 to {rules["maxMinutes"]} minutes (asked: {minutes})')
    d = cfg['leaseDir']
    with _dir(cfg) as dir_fd, _locked(d, dir_fd, name):
        _sweep(d, dir_fd, name)
        cur = _read(d, dir_fd, name)
        live = cur is not None and cur['until'] > now
        if live and cur['holder'] != holder:
            raise Held(cur)
        lease = {'version': 1, 'machine': name, 'holder': holder, 'user': _user(), 'note': note,
                 'since': cur['since'] if live else now, 'until': now + minutes * 60}
        _write(dir_fd, name, lease)
    return lease, live, (cur if cur is not None and not live else None)


def release(cfg, name, holder, now):
    """Give back holder's lease of `name`. Returns the lease given back, or None when there was no live lease (an
    expired one is removed). Raises Held when another holder holds it now."""
    check_holder(holder)
    leasable(cfg, name)
    d = cfg['leaseDir']
    with _dir(cfg) as dir_fd, _locked(d, dir_fd, name):
        _sweep(d, dir_fd, name)
        cur = _read(d, dir_fd, name)
        if cur is None:
            return None
        if cur['until'] > now and cur['holder'] != holder:
            raise Held(cur)
        os.unlink(f'{name}.json', dir_fd=dir_fd)
        return cur if cur['until'] > now else None


def status(cfg, now):
    """[(machine, lease or None)] for every machine that can be leased, in file order; an expired lease is None."""
    out = []
    with _dir(cfg) as dir_fd:
        for m in cfg['machines']:
            if m['lease']:
                cur = _read(cfg['leaseDir'], dir_fd, m['name'])
                out.append((m, cur if cur is not None and cur['until'] > now else None))
    return out
