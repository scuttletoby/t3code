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
  python3 -I fork-fleet-desk-seed.py [--config FILE] run
                     the desktop, as root, hourly (t3code-fleet-seed.timer), with the agent's settings (FILE, default
                     its CONF). Only names its own installed fleet config lists for its POOL, held to its own
                     every_hours and max_age_hours and the smaller of its own and the host's max_mb. Each new seed is
                     unpacked with tarfile's data filter into a folder of its own, checked against the trailer, walked
                     (a link out of it is refused; all made readable), flushed, and swapped in by rename: SEEDS/NAME is a link to
                     .v/NAME.STAMP/data, root's (folders 0755, files no one else may write), unpacked in .new
                     (one folder: a rename never crosses a mount of the unit's sandbox). Jobs see SEEDS read-only
                     (the launcher's ALLOW_PATHS): a job uses /var/lib/fork-fleet/seeds/NAME when it is there and runs
                     cold when it is not.

Each run (one at a time: a lock) meets this state:
  the same seed on the host (bytes and MADE)   nothing is fetched
  a new one under every_hours after the last   nothing yet
  a torn copy (the stream cut or past FETCH_WAIT, a trailer that does not match, more than max_mb, a tar member
  that would leave its folder or is a device)  the copy is removed, the old seed stays, the next hour tries again
  no seed (never fetched, the host down)       jobs run cold
  older than max_age_hours, made over SKEW in the future (the clocks disagree), or no longer in the pool's list
                                               the link is removed: jobs run cold
  a label that is missing or unreadable        the link is removed and the seed fetched again in the same run
  a run that stopped half-way                  SEEDS/.new is emptied by the next run; a link is the old one or the new
  a replaced version                           kept KEEP_OLD after it was replaced, for a job still reading it (a job
                                               runs at most the launcher's MAX_SECONDS, 1 h by default), then removed

The forced command sends every command that starts with `seed` here. The host side exits 2 for any other command,
3 while another stream for the pool runs, 4 when the file may not be sent (missing, a link, in a folder or a file
others may write, above max_mb, older than max_age_hours or dated in the future, or changed while it was sent: made
in place, not by rename; a file replaced by rename while it is sent is fine, the old one is sent whole), 1 when the
desktop takes less than WRITE bytes in STALL seconds or the whole file not by its deadline (GRACE seconds plus
MIN_RATE bytes a second): the pool's lock is then let go.
Standard library only.
"""
import argparse, fcntl, hashlib, importlib.util, json, math, os, re, shutil, signal, stat, subprocess, sys
import tarfile, threading, time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))


def module(name, file):
    spec = importlib.util.spec_from_file_location(name, os.path.join(HERE, file))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


FILES = '/var/lib/fork-fleet/seed-files'   # the always-on host's: POOL/NAME.tar, made by the project
SEEDS = '/var/lib/fork-fleet/seeds'        # the desktop's: what jobs see
CONFIG = os.path.join(HERE, 'fork-fleet.json')
NAME = re.compile(r'[a-z0-9][a-z0-9_-]{0,39}')
LINK = re.compile(r'\.v/((?P<name>[a-z0-9][a-z0-9_-]{0,39})\.[0-9]{1,20})/data')
LIST_WAIT, FETCH_WAIT = 30, 30 * 60   # seconds for the list, and for one seed's whole stream
LIST_MOST, ERR_KEEP, MEMBERS = 1 << 20, 4096, 200_000   # the list's bytes, ssh's stderr kept, a seed's tar members
KEEP_OLD, SKEW = 24 * 3600, 3600       # SKEW: how far the host's clock may be ahead of the desktop's
PIECE, WRITE, STALL, AHEAD = 1 << 20, 64 << 10, 120, 300   # AHEAD: seconds a file's date may be ahead of now
GRACE, MIN_RATE = 600, 256 << 10   # a stream's deadline: GRACE seconds plus one second per MIN_RATE bytes
ITEM = {'everyHours': 'every_hours', 'maxAgeHours': 'max_age_hours', 'maxMb': 'max_mb'}


class Refused(Exception):
    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


# ---------------------------------------------------------------- the always-on host (serve's forced command)

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


# ---------------------------------------------------------------- the desktop

class Torn(Exception):
    pass


class Summed:
    """The ssh stream as tarfile reads it: counted, hashed, cut past `most` bytes."""

    def __init__(self, raw, most):
        self.raw, self.most, self.size, self.sha = raw, most, 0, hashlib.sha256()

    def read(self, n=PIECE):
        data = self.raw.read(n if n is not None and n >= 0 else PIECE)
        self.size += len(data)
        if self.size > self.most:
            raise Torn(f'more than {self.most >> 20} MB (max_mb)')
        self.sha.update(data)
        return data


def ssh(agent, conf, *words):
    return agent.ssh_argv(conf)[:-2] + list(words)   # the agent's ssh, another command than `serve POOL`


class Tail(threading.Thread):
    """Reads a pipe to its end and keeps only its last ERR_KEEP bytes (ssh's stderr: the host may write without
    end; the trailer is its last line)."""

    def __init__(self, f):
        super().__init__(daemon=True)
        self.f, self.data = f, b''
        self.start()

    def run(self):
        while chunk := os.read(self.f.fileno(), 65536):
            self.data = (self.data + chunk)[-ERR_KEEP:]

    def text(self):
        self.join(5)
        return ' '.join(self.data.decode('utf-8', 'replace').split())[-300:] or 'nothing on stderr'


def listing(agent, conf):
    """{name: item} of the host's list (at most LIST_MOST bytes); Torn when it cannot be had or is not well formed."""
    try:
        p = subprocess.Popen(ssh(agent, conf, 'seeds'), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, env=agent.ENV, start_new_session=True)
    except OSError as e:
        raise Torn(f'no ssh: {e}') from None
    late = threading.Event()
    timer = threading.Timer(LIST_WAIT, lambda: (late.set(), p.kill()))
    timer.start()
    out = b''
    try:
        err = Tail(p.stderr)
        while len(out) <= LIST_MOST and (chunk := os.read(p.stdout.fileno(), 65536)):
            out += chunk
    finally:
        timer.cancel()
        p.kill()
        code = p.wait()
        p.stdout.close()
    if late.is_set():
        raise Torn(f'no list in {LIST_WAIT} s')
    if len(out) > LIST_MOST:
        raise Torn(f'the host\'s list is above {LIST_MOST} bytes')
    if code:
        raise Torn(f'no list (exit {code}): {err.text()}')
    try:
        got = json.loads(out, parse_constant=no_constant)
        assert isinstance(got, dict) and got.get('v') == 1 and isinstance(got.get('seeds'), list)
        out = {}
        for s in got['seeds']:
            assert isinstance(s, dict) and isinstance(s.get('name'), str) and NAME.fullmatch(s['name'])
            assert all(whole(s.get(k)) and s[k] > 0 for k in ITEM) and ('made' not in s or whole(s.get('bytes'))
                                                                       and whole(s['made']))
            out[s['name']] = s
        return out
    except (ValueError, AssertionError):
        raise Torn('the host\'s list is not well formed') from None


def no_constant(name):
    raise ValueError(f'{name} is not JSON')


def whole(x):
    return isinstance(x, int) and not isinstance(x, bool) and x >= 0


def guarded(most):
    """tarfile's data filter, and a limit on what the tar says it holds: members' sizes together at most MOST
    bytes (a sparse member is small in the stream, not on disk) and at most MEMBERS members (each is kept in memory)."""
    seen = [0, 0]

    def check(member, path):
        seen[0] += 1
        seen[1] += member.size
        if seen[0] > MEMBERS or seen[1] > most:
            raise Torn(f'the tar holds more than {MEMBERS} members or {most >> 20} MB (max_mb)')
        return tarfile.data_filter(member, path)
    return check


def settle(data):
    """After unpacking: every entry a folder, a plain file or a link that stays inside DATA (a check of its own,
    besides the data filter, which some Python releases let links slip past); folders and files readable by all
    (jobs run as other users; a cache may hold 0600 files), writable by root only. Torn otherwise."""
    top = os.path.realpath(data)
    for d, dirs, names in os.walk(data):
        for n in dirs + names:
            path = os.path.join(d, n)
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                if os.path.commonpath([top, os.path.realpath(path)]) != top:
                    raise Torn(f'{os.path.relpath(path, data)} is a link that leads out of the seed')
            elif stat.S_ISDIR(st.st_mode):
                os.chmod(path, (st.st_mode & 0o755) | 0o555)
            elif stat.S_ISREG(st.st_mode):
                os.chmod(path, (st.st_mode & 0o755) | 0o444)
            else:
                raise Torn(f'{os.path.relpath(path, data)} is not a folder, a file or a link')


def fetch(agent, conf, item, root, now):
    """The seed `item` (its maxMb: the smaller of the host's and the desktop's own) into a new version, swapped in;
    its label. Torn (or OSError, TarError) leaves the old one."""
    name, stamp, most = item['name'], time.time_ns(), item['maxMb'] << 20
    new = os.path.join(root + '/.new', f'{name}.{stamp}')
    os.mkdir(new, 0o755)
    os.mkdir(os.path.join(new, 'data'), 0o755)
    p = subprocess.Popen(ssh(agent, conf, 'seed', name), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, env=agent.ENV, start_new_session=True)
    timer = threading.Timer(FETCH_WAIT, p.kill)
    timer.start()
    try:
        err = Tail(p.stderr)
        got = Summed(p.stdout, most)
        with tarfile.open(fileobj=got, mode='r|') as t:
            t.extractall(os.path.join(new, 'data'), filter=guarded(most))
        while got.read(PIECE):   # the tar's end blocks and padding count in the trailer's size and sha256
            pass
        code = p.wait(timeout=30)
    finally:
        timer.cancel()
        p.kill()
        p.wait()
        p.stdout.close()
    err.join(5)
    last = (err.data.decode('utf-8', 'replace').strip().splitlines() or [''])[-1].split()
    if code or len(last) != 5 or last[:4] != ['seed', name, str(got.size), got.sha.hexdigest()] or \
            not re.fullmatch(r'[0-9]{1,20}', last[4]):
        raise Torn(f'exit {code}, {got.size} bytes, the trailer does not match: {err.text()}')
    settle(os.path.join(new, 'data'))
    label = {'v': 1, 'name': name, 'bytes': got.size, 'sha256': got.sha.hexdigest(), 'made': int(last[4]),
             'fetched': int(now()), 'maxAgeHours': item['maxAgeHours']}
    with open(os.path.join(new, 'label.json'), 'x', encoding='utf-8') as f:
        json.dump(label, f)
    os.sync()                                # the new version is on disk before any link names it
    version = os.path.join(root, '.v', os.path.basename(new))
    os.rename(new, version)
    link = os.path.join(root, f'.{name}.link')
    remove(link)
    os.symlink(f'.v/{name}.{stamp}/data', link)
    old = current(root, name)[0]
    if old:
        os.utime(old, (now(), now()))        # replaced now: kept KEEP_OLD for a job still reading it
    os.replace(link, os.path.join(root, name))
    return label


def current(root, name):
    """(version folder or None, label or None) of the link SEEDS/NAME."""
    try:
        m = LINK.fullmatch(os.readlink(os.path.join(root, name)))
    except OSError:
        return None, None
    if not m or m['name'] != name:
        return None, None
    version = os.path.join(root, '.v', m[1])
    try:
        with open(os.path.join(version, 'label.json'), encoding='utf-8') as f:
            label = json.load(f)
        ok = isinstance(label, dict) and label.get('name') == name and all(
            whole(label.get(k)) for k in ('bytes', 'made', 'fetched', 'maxAgeHours'))
    except (OSError, ValueError):
        ok = False
    return version, label if ok else None


def remove(path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if stat.S_ISDIR(st.st_mode):
        shutil.rmtree(path)
    else:
        os.unlink(path)


def drop(root, name, version, now):
    if version and os.path.isdir(version):
        os.utime(version, (now(), now()))    # first: a run that stops here leaves it linked, never pruned early
    remove(os.path.join(root, name))


def own_seeds(conf, config):
    """{name: seed} of this desktop's pool in its own installed fleet config: the only names it takes, and the
    every_hours, max_age_hours and max_mb it holds them to (the host's list never raises them)."""
    try:
        return pool_seeds(conf['POOL'], config)
    except Refused as e:
        raise Torn(f'its own {e}') from None


def run(agent, conf, root=SEEDS, now=time.time, say=print, config=CONFIG):
    if not hasattr(tarfile, 'data_filter'):   # releases with known filter bypasses pass this: settle() checks again
        raise agent.Bad('this Python has no tarfile data filter (3.12, or 3.8.17 and later): no seeds')
    try:
        own = own_seeds(conf, config)
    except Torn as e:
        raise agent.Bad(f'{e}: no seeds') from None
    os.umask(0o022)
    for d in (root, root + '/.new', root + '/.v'):   # root: made by the unit (StateDirectory) or here
        os.makedirs(d, 0o755, exist_ok=True)
    with open(os.path.join(root + '/.new', '.lock'), 'a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            say('another seed run is running')
            return 1
        for e in os.listdir(root + '/.new'):   # a run that stopped half-way
            if e != '.lock':
                remove(os.path.join(root + '/.new', e))
        for e in os.listdir(root):   # a link made but not yet swapped in
            if e.startswith('.') and e.endswith('.link'):
                remove(os.path.join(root, e))
        try:
            host = listing(agent, conf)
            items = {n: {**host[n], 'everyHours': s['every_hours'], 'maxAgeHours': s['max_age_hours'],
                         'maxMb': min(host[n]['maxMb'], s['max_mb'])} for n, s in own.items() if n in host}
        except Torn as e:
            items = None
            say(f'seeds: {e}: each seed stays until its age drops it')
        for name in sorted(n for n in os.listdir(root) if NAME.fullmatch(n)):
            version, label = current(root, name)
            if label is None:
                why = 'its label is missing or cannot be read (fetched again now)'
            elif name not in own:
                why = 'no longer one of the pool\'s seeds'
            else:
                age = now() - label['made']
                hours = own[name]['max_age_hours']
                why = (f'older than {hours} h (max_age_hours)' if age > hours * 3600
                       else 'made in the future (the clocks disagree)' if age < -SKEW else None)
            if why:
                drop(root, name, version, now)
                say(f'seed {name}: dropped, {why}; jobs run cold until a new one comes')
        for name, item in sorted((items or {}).items()):
            label = current(root, name)[1]
            if 'made' not in item:
                say(f'seed {name}: the host offers none ({item.get("why", "no reason")}): '
                    f'{"the one here stays" if label else "jobs run cold"}')
                continue
            if label and (label['bytes'], label['made']) == (item['bytes'], item['made']):
                continue
            if label and 0 <= now() - label['fetched'] < item['everyHours'] * 3600:
                continue
            if not -SKEW <= now() - item['made'] <= item['maxAgeHours'] * 3600:
                say(f'seed {name}: the host\'s is too old or made in the future (the clocks disagree): not fetched')
                continue
            try:
                got = fetch(agent, conf, item, root, now)
                say(f'seed {name}: {got["bytes"] >> 20} MB, sha256 checked, swapped in')
            except (Torn, OSError, ValueError, tarfile.TarError, subprocess.SubprocessError) as e:
                for e2 in os.listdir(root + '/.new'):
                    if e2 != '.lock':
                        remove(os.path.join(root + '/.new', e2))
                say(f'seed {name}: keeps {"the old one" if label else "none"}: {e.__class__.__name__}: '
                    f'{str(e)[:300]}; tried again next hour')
        prune(root, now)
    return 0


def prune(root, now):
    """Versions no link names, KEEP_OLD after they were replaced or dropped."""
    linked = {current(root, n)[0] for n in os.listdir(root) if NAME.fullmatch(n)}
    for e in os.listdir(root + '/.v'):
        path = os.path.join(root, '.v', e)
        if path not in linked and now() - os.lstat(path).st_mtime > KEEP_OLD:
            remove(path)


def main(argv=None):
    a = argparse.ArgumentParser(description='a desk pool\'s warm caches, the desktop\'s side (as root)')
    agent = module('fork_fleet_desk_agent', 'fork-fleet-desk-agent.py')
    a.add_argument('--config', default=agent.CONF)
    a.add_argument('verb', choices=('run',))
    a = a.parse_args(argv)
    try:
        return run(agent, agent.load(a.config))
    except agent.Bad as e:
        print(f'fork-fleet-desk-seed: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
