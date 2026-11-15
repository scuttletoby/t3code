#!/usr/bin/env python3
"""Fork only: warm caches ("seeds") for a desk pool's jobs (.github/fork-fleet.md, "Seeds").

A project lists the caches its desk pool's jobs may start from in the pool's `seeds` (fork-fleet.json): a name, how
often a desktop takes a new one (every_hours), the age at which it is dropped (max_age_hours, from when it was made)
and its largest size (max_mb). On the always-on host something of the project's own (a timer, a job) makes each one:
a tar of a folder's contents at FILES/NAME.tar, written to a temp file and renamed. Nothing here makes one. The
desktop pulls them with the desk link's key, in an ssh session of its own beside the agent's:

  ssh ... seeds      the forced command (fork-fleet-desk.py serve POOL, the desk-link user) sees it in
                     SSH_ORIGINAL_COMMAND and prints the pool's list (one JSON object; `bytes` and `made` of each
                     seed the host offers now, `why` for one it does not)
  ssh ... seed NAME  streams FILES/NAME.tar: only a name of that list, never a path the desktop names. The last
                     stderr line is the trailer `seed NAME BYTES SHA256 MADE` (MADE: the file's mtime, epoch seconds)

The host side exits 2 for any other command, 3 while another stream for the pool runs, 4 when the file may not be
sent (missing, a link, in a folder or a file others may write, above max_mb, older than max_age_hours, or changed
while it was sent: made in place, not by rename). Standard library only.
"""
import fcntl, hashlib, importlib.util, json, os, re, stat, sys, time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FILES = '/var/lib/fork-fleet/seed-files'   # the always-on host's: NAME.tar, made by the project
CONFIG = os.path.join(HERE, 'fork-fleet.json')
PIECE = 1 << 20
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


def offered(seed, files, now):
    """An open fd and the stat of FILES/NAME.tar; Refused (4) when it may not be sent."""
    path = os.path.join(files, seed['name'] + '.tar')
    try:
        st = os.lstat(files)
        if not stat.S_ISDIR(st.st_mode) or st.st_mode & 0o022:
            raise Refused(f'{files} must be a folder (not a link) that only its owner may write', 4)
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        raise Refused(f'{path}: {"not made yet" if isinstance(e, FileNotFoundError) else e.strerror}', 4) from None
    st = os.fstat(fd)
    hours = seed['max_age_hours']
    why = ('not a plain file' if not stat.S_ISREG(st.st_mode) else 'others may write it' if st.st_mode & 0o022
           else f'above max_mb ({seed["max_mb"]})' if st.st_size > seed['max_mb'] << 20
           else f'older than max_age_hours ({hours})' if now - st.st_mtime > hours * 3600 else None)
    if why:
        os.close(fd)
        raise Refused(f'{path}: {why}', 4)
    return fd, st


def offer(pool, command, lock_dir, out=1, err=sys.stderr, config=CONFIG, files=FILES, now=time.time):
    try:
        if os.geteuid() == 0:
            raise Refused('serve never runs as root', 2)
        word, _, name = command.partition(' ')
        seeds = pool_seeds(pool, config)
        if command == 'seeds':
            got = []
            for s in seeds.values():
                item = {'name': s['name'], **{k: s[v] for k, v in ITEM.items()}}
                try:
                    fd, st = offered(s, files, now())
                    os.close(fd)
                    item.update(bytes=st.st_size, made=int(st.st_mtime))
                except Refused as e:
                    item['why'] = str(e)
                got.append(item)
            os.write(out, (json.dumps({'v': 1, 'seeds': got}) + '\n').encode())
            return 0
        if word != 'seed' or name not in seeds:
            raise Refused(f'refused: {command[:60]!r} (only `seeds`, or `seed NAME` of pool {pool}\'s seeds)', 2)
        lock = os.open(os.path.join(lock_dir, f'{pool}.seed.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise Refused(f'another seed stream for pool {pool} runs', 3) from None
            fd, st = offered(seeds[name], files, now())
            try:
                sha, sent = send(fd, out)
                after = os.fstat(fd)
            finally:
                os.close(fd)
        finally:
            os.close(lock)
        if sent != st.st_size or (after.st_size, after.st_mtime_ns) != (st.st_size, st.st_mtime_ns):
            raise Refused(f'{name}: it changed while it was sent (made in place, not by rename?)', 4)
        print(f'seed {name} {sent} {sha} {int(st.st_mtime)}', file=err, flush=True)
        return 0
    except Refused as e:
        print(f'fork-fleet-desk seed: {e}', file=err, flush=True)
        return e.code


def send(fd, out):
    sha, sent = hashlib.sha256(), 0
    while data := os.read(fd, PIECE):
        sha.update(data)
        sent += len(data)
        view = memoryview(data)
        while view:
            view = view[os.write(out, view):]
    return sha.hexdigest(), sent
