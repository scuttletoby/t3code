#!/usr/bin/env python3
"""Fork only: warm caches ("seeds") for a desk pool's jobs (.github/fork-fleet.md, "Seeds").

A project lists the caches its desk pool's jobs may start from in the pool's `seeds` (fork-fleet.json): a name, how
often a desktop takes a new one (every_hours), the age at which it is dropped (max_age_hours, from when it was made)
and its largest size (max_mb). On the always-on host something of the project's own (a timer, a job) makes each one:
a tar of a folder's contents at FILES/POOL/NAME.tar (a folder per pool: pools never share a file), written to a temp
file and renamed. Nothing here makes one. The desktop pulls them with the desk link's key, in an ssh session of its
own beside the agent's:

  ssh ... seeds      the forced command (fork-fleet-desk.py serve POOL, the desk-link user) sees it in
                     SSH_ORIGINAL_COMMAND and prints the pool's list (one JSON object; `bytes` and `made` of each
                     seed the host offers now, `why` for one it does not)
  ssh ... seed NAME  streams FILES/POOL/NAME.tar: only a name of that list, never a path the desktop names. The last
                     stderr line is the trailer `seed NAME BYTES SHA256 MADE` (MADE: the file's mtime, epoch seconds)

The forced command sends every command that starts with `seed` here. The host side exits 2 for any other command,
3 while another stream for the pool runs, 4 when the file may not be sent (missing, a link, in a folder or a file
others may write, above max_mb, older than max_age_hours or dated in the future, or changed while it was sent: made
in place, not by rename; a file replaced by rename while it is sent is fine, the old one is sent whole), 1 when the
desktop takes less than WRITE bytes in STALL seconds or the whole file not by its deadline (GRACE seconds plus
MIN_RATE bytes a second): the pool's lock is then let go.
Standard library only.
"""
import fcntl, hashlib, importlib.util, json, math, os, signal, stat, sys, time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FILES = '/var/lib/fork-fleet/seed-files'   # the always-on host's: POOL/NAME.tar, made by the project
CONFIG = os.path.join(HERE, 'fork-fleet.json')
PIECE, WRITE, STALL, AHEAD = 1 << 20, 64 << 10, 120, 300   # AHEAD: seconds a file's date may be ahead of now
GRACE, MIN_RATE = 600, 256 << 10   # a stream's deadline: GRACE seconds plus one second per MIN_RATE bytes
ITEM = {'everyHours': 'every_hours', 'maxAgeHours': 'max_age_hours', 'maxMb': 'max_mb'}


class Refused(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def pool_seeds(pool, config):
    try:
        cfg = module('fork_fleet', 'fork-fleet.py').load(config)
    except (OSError, ValueError) as e:
        raise Refused(f'the fleet config: {e}', 2) from None
    for p in cfg['pools']:
        if p['name'] == pool:
            return {s['name']: s for s in p.get('seeds', [])}
    raise Refused(f'pool {pool} is not in {config}', 2)


def offered(seed, folder, now):
    """An open fd and the stat of FOLDER/NAME.tar (FOLDER: FILES/POOL); Refused (4) when it may not be sent."""
    path = os.path.join(folder, seed['name'] + '.tar')
    try:
        for d in (os.path.dirname(folder), folder):
            st = os.lstat(d)
            if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
                raise Refused(f'{d} must be a folder (not a link) that only its owner may write', 4)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise Refused(f'{path}: {"not made yet" if isinstance(e, FileNotFoundError) else e.strerror}', 4) from None
    st = os.fstat(fd)
    hours = seed['max_age_hours']
    why = ('not a plain file' if not stat.S_ISREG(st.st_mode) else 'others may write it' if st.st_mode & 0o022
           else f'above max_mb ({seed["max_mb"]})' if st.st_size > seed['max_mb'] << 20
           else f'older than max_age_hours ({hours})' if now - st.st_mtime > hours * 3600
           else 'dated in the future' if st.st_mtime > now + AHEAD else None)
    if why:
        os.close(fd)
        raise Refused(f'{path}: {why}', 4)
    return fd, st


def offer(pool, command, lock_dir, out=1, err=sys.stderr, config=CONFIG, files=FILES, now=time.time):
    try:
        if os.geteuid() == 0:
            raise Refused('serve never runs as root', 2)
        word, _, name = command.partition(' ')
        seeds, folder = pool_seeds(pool, config), os.path.join(files, pool)
        if command == 'seeds':
            got = []
            for s in seeds.values():
                item = {'name': s['name'], **{k: s[v] for k, v in ITEM.items()}}
                try:
                    fd, st = offered(s, folder, now())
                    os.close(fd)
                    item.update(bytes=st.st_size, made=int(st.st_mtime))
                except Refused as e:
                    item['why'] = str(e)
                got.append(item)
            os.write(out, (json.dumps({'v': 1, 'seeds': got}) + '\n').encode())
            return 0
        if word != 'seed' or name not in seeds:
            raise Refused(f'refused: {command[:60]!r} (only `seeds`, or `seed NAME` of pool {pool}\'s seeds)', 2)
        lock_path = os.path.join(lock_dir, f'{pool}.seed.lock')
        try:
            lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        except OSError as e:
            raise Refused(f'{lock_path}: {e.strerror} (a leftover of another user? remove it)', 1) from None
        try:
            if os.fstat(lock).st_uid != os.geteuid():
                raise Refused(f'{lock_path} belongs to uid {os.fstat(lock).st_uid}, not this user: remove it', 1)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refused(f'another seed stream for pool {pool} runs', 3) from None
            fd, st = offered(seeds[name], folder, now())
            try:
                sha, sent = send(fd, out, st.st_size)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
        # A rename over NAME.tar unlinks the open file (st_nlink 0), which changes its ctime but not its bytes:
        # only a file still in place must keep its ctime (a rewrite with the old mtime put back).
        same = ('st_size', 'st_mtime_ns') + (('st_ctime_ns',) if after.st_nlink else ())
        if sent != st.st_size or any(getattr(after, k) != getattr(st, k) for k in same):
            raise Refused(f'{name}: it changed while it was sent (made in place, not by rename?)', 4)
        print(f'seed {name} {sent} {sha} {int(st.st_mtime)}', file=err, flush=True)
        return 0
    except Refused as e:
        print(f'fork-fleet-desk seed: {e}', file=err, flush=True)
        return e.code


class Stalled(Exception):
    pass


def stalled(*_):
    raise Stalled


def send(fd, out, size, stall=None):
    """Sends at most SIZE bytes of FD (the size that was checked: a file that grows is cut there, and then fails the
    changed-file check) to OUT; Refused (1) when OUT takes less than WRITE bytes in STALL seconds, or not all of it
    within GRACE seconds plus SIZE / MIN_RATE."""
    stall = STALL if stall is None else stall
    sha, sent = hashlib.sha256(), 0
    deadline = time.monotonic() + GRACE + size / MIN_RATE
    old = signal.signal(signal.SIGALRM, stalled)
    try:
        while sent < size and (data := os.read(fd, min(PIECE, size - sent))):
            sha.update(data)
            sent += len(data)
            view = memoryview(data)
            while view:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise Stalled
                signal.alarm(max(1, min(stall, math.ceil(left))))   # each piece that moves starts the wait again
                view = view[os.write(out, view[:WRITE]):]
    except Stalled:
        if time.monotonic() >= deadline:
            raise Refused(f'the desktop did not take {size} bytes within {GRACE + size / MIN_RATE:.0f} seconds '
                          f'(at least {MIN_RATE >> 10} KiB a second): stopped', 1) from None
        raise Refused(f'the desktop took less than {WRITE >> 10} KiB in {stall} seconds: stopped', 1) from None
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, old)
    return sha.hexdigest(), sent
