#!/usr/bin/env python3
"""Fork only: the desktop's side of the desk link (.github/fork-fleet.md, "The desk link"), the desk agent.

A desktop accepts no connection and holds no GitHub token. Its desk agent, as root on the desktop (on WSL2: inside the
distro), dials out over ssh to the always-on host, where its key may only run `fork-fleet-desk.py serve POOL` as the
desk-link user, and runs each launcher verb that comes back through that session:

  python3 -I fork-fleet-desk-agent.py [--config FILE] run     the link; the unit fork-fleet-desk-agent.service runs it
  python3 -I fork-fleet-desk-agent.py [--config FILE] check   the settings and files; connects to nothing

Each request is checked again here (wire.check_request: only the launcher's verbs, each argument well formed, the
`start` input exactly its three lines; anything else ends the session) and runs as `LAUNCH VERB ARGS` with a fixed
environment (PATH and LC_ALL only: the launcher and the walls read their own settings files, never a variable). A
`start` runs only after `WALLS check` exits 0 (any other exit: no runner, answer 1). A verb still running at its
limit (fork-fleet-desk-wire.py, "Time") is killed with its process group and answered 75 (UNKNOWN); when the session
ends, every running verb is killed. The launcher's own 75 is answered 1. At most MAX_VERBS run at once. Time is
CLOCK_BOOTTIME, which counts a sleep: a desktop that wakes finds its link and its verbs past their time at once.

The link drops (sleep, network, the always-on host restarts): the agent dials again after FIRST s, then BASE s
doubling to MAX s, each with jitter; a wake from sleep dials at once. A wrong wire version (ssh exit 76, or a frame of
another version): the side to upgrade is logged, and the next try is MAX s later.
Exit: 0 stopped (SIGTERM), 1 another agent runs here (RUN_DIR/agent.lock), 2 the settings or a file are unusable.

Settings: FILE (default CONF), KEY=value lines, owned by this user and writable by it alone (as launch.conf):
  POOL, HOST     required: the pool (fork-fleet.json) and the always-on host (a name or an IPv4 address)
  PORT 22, USER fleet-desk (the desk-link user: fork-fleet-desk.py's USER)
  KEY            /etc/fork-fleet/desk-key: the private ed25519 key, this user's only (no group or other bits)
  KNOWN_HOSTS    /etc/fork-fleet/desk-known_hosts: the always-on host's key line; nothing else is ever trusted
  LAUNCH, WALLS  /opt/fork-fleet/fork-fleet-launch.sh and /opt/fork-fleet/fork-fleet-walls.py (run as python3 -I)
  RUN_DIR        /run/fork-fleet-desk-agent (its lock: one agent per desktop), SSH /usr/bin/ssh
Standard library only. Linux (and WSL2) now; another OS is refused by the walls.
"""
import argparse, fcntl, importlib.util, os, queue, random, re, select, signal, stat, subprocess, sys, threading, time

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location('fork_fleet_desk_wire', os.path.join(HERE, 'fork-fleet-desk-wire.py'))
wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire)

CONF = '/etc/fork-fleet/desk.conf'
DEFAULTS = {'PORT': '22', 'USER': 'fleet-desk', 'KEY': '/etc/fork-fleet/desk-key',
            'KNOWN_HOSTS': '/etc/fork-fleet/desk-known_hosts', 'LAUNCH': '/opt/fork-fleet/fork-fleet-launch.sh',
            'WALLS': '/opt/fork-fleet/fork-fleet-walls.py', 'RUN_DIR': '/run/fork-fleet-desk-agent',
            'SSH': '/usr/bin/ssh'}
PATH_KEYS = ('KEY', 'KNOWN_HOSTS', 'LAUNCH', 'WALLS', 'RUN_DIR', 'SSH')
SHAPES = {'POOL': wire.POOL, 'HOST': re.compile(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}'),
          'PORT': re.compile(r'[1-9][0-9]{0,4}'), 'USER': re.compile(r'[a-z_][a-z0-9_-]{0,31}'),
          **{k: re.compile(r'/[A-Za-z0-9._/-]{1,255}') for k in PATH_KEYS}}
PYTHON = '/usr/bin/python3'
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C.UTF-8'}   # all a verb, the walls and ssh get
FIRST, BASE, MAX = (2, 10), 30, 900   # seconds: after a link that was up; then doubling from BASE to MAX
CONNECT, DEAD = 15, 45   # ssh's connect time; no frame in DEAD s (serve pings every 15 s): the link is gone
TERM_BEFORE, KILL_BEFORE = 3, 1   # seconds before a verb's limit: SIGTERM to its process group, then SIGKILL
MAX_VERBS = 8
SLEPT = 10   # BOOTTIME ran this many seconds ahead of MONOTONIC: the desktop slept


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def slept_for():
    return boottime() - time.monotonic()


class Bad(Exception):
    """The settings or a file are unusable: exit 2."""


def owned_safe(path, kind, private=False):
    """`path` is a `kind` ('file' or 'dir') of this user, not a link, that no one else may write (`private`: nor
    read), in a folder of this user or root that no one else may write."""
    for p, want, mask in ((os.path.dirname(path) or '/', 'dir', 0o022), (path, kind, 0o077 if private else 0o022)):
        try:
            st = os.lstat(p)
        except OSError as e:
            raise Bad(f'{p}: {e.strerror}') from None
        ok = stat.S_ISDIR(st.st_mode) if want == 'dir' else stat.S_ISREG(st.st_mode)
        owners = (os.geteuid(),) if p == path else (0, os.geteuid())
        if not ok or st.st_uid not in owners or st.st_mode & mask:
            raise Bad(f'{p} must be a {want} (not a link) of uid {" or ".join(map(str, owners))} that '
                      f'{"only it may read or write" if mask == 0o077 else "only it may write"}')


def load(path):
    owned_safe(path, 'file')
    conf, seen = dict(DEFAULTS), set()
    with open(path, encoding='ascii', errors='replace') as f:
        for n, line in enumerate(f.read().splitlines(), 1):
            if not line.strip() or line.lstrip().startswith('#'):
                continue
            key, _, value = line.partition('=')
            if key not in SHAPES or not SHAPES[key].fullmatch(value) or '..' in value.split('/'):
                raise Bad(f'{path}:{n}: not a setting: {wire.clean(key)[:40]!r}')
            if key in seen:
                raise Bad(f'{path}:{n}: {key} given twice')
            seen.add(key)
            conf[key] = value
    for key in ('POOL', 'HOST'):
        if key not in conf:
            raise Bad(f'{path}: {key} is required')
    if not 1 <= int(conf['PORT']) <= 65535:
        raise Bad(f'{path}: PORT is 1 to 65535')
    owned_safe(conf['KEY'], 'file', private=True)
    owned_safe(conf['KNOWN_HOSTS'], 'file')
    return conf


def ssh_argv(conf):
    """ssh with no config file, no agent and no forwarding, the pinned host key only, ed25519 only."""
    opts = ['BatchMode=yes', 'IdentitiesOnly=yes', 'IdentityAgent=none', 'StrictHostKeyChecking=yes',
            f'UserKnownHostsFile={conf["KNOWN_HOSTS"]}', 'GlobalKnownHostsFile=none', 'HostKeyAlgorithms=ssh-ed25519',
            'UpdateHostKeys=no', 'VerifyHostKeyDNS=no', 'CheckHostIP=no', f'ConnectTimeout={CONNECT}',
            'ServerAliveInterval=15', 'ServerAliveCountMax=3', 'ClearAllForwardings=yes', 'ControlMaster=no',
            'ControlPath=none', 'PermitLocalCommand=no', 'RequestTTY=no', 'LogLevel=ERROR']
    return [conf['SSH'], '-F', 'none', '-T', '-a', '-x', '-i', conf['KEY'], '-p', conf['PORT'], '-l', conf['USER'],
            *[a for o in opts for a in ('-o', o)], '--', conf['HOST'], 'serve', conf['POOL']]


class Ended(Exception):
    pass


class Verb:
    def __init__(self, ident, frame, began):
        self.ident, self.verb, self.args, self.stdin = ident, frame['verb'], frame['args'], frame.get('stdin')
        self.limit = began + wire.limit(self.verb)
        self.proc, self.killed, self.answered, self.lock = None, False, False, threading.Lock()


class Session:
    """One ssh session: hello, then requests, pings and answers until it ends. run() -> (linked, exit code)."""

    def __init__(self, conf, log, stopping):
        self.conf, self.log, self.stopping = conf, log, stopping
        self.out, self.verbs, self.lock = queue.Queue(), {}, threading.Lock()

    def say(self, msg):
        print(f'fork-fleet-desk-agent {self.conf["POOL"]}: {msg}', file=self.log, flush=True)

    def run(self):
        self.proc = subprocess.Popen(ssh_argv(self.conf), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=ENV, start_new_session=True)
        threads = [threading.Thread(target=f, daemon=True) for f in (self.write, self.relay)]
        for t in threads:
            t.start()
        linked, code = False, None
        try:
            self.out.put(wire.encode({'v': wire.VERSION, 'hello': self.conf['POOL']}))
            reader, fd = wire.Reader(), self.proc.stdout.fileno()
            heard = boottime()
            while not self.stopping.is_set():
                if select.select([fd], [], [], 0.5)[0]:
                    data = os.read(fd, 65536)
                    if not data:
                        break
                    heard = boottime()
                    for frame in reader.feed(data):
                        linked = self.frame(frame, linked)
                now = boottime()
                if now - heard > (DEAD if linked else CONNECT + DEAD):
                    raise Ended(f'no word from the always-on host in {round(now - heard)} s (asleep, or the link '
                                'is down)')
                self.police(now)
        except wire.VersionError as e:
            code = wire.WRONG_VERSION
            side = "this desktop's desk agent" if e.got > wire.VERSION else "the always-on host's fork-fleet files"
            self.say(f'the always-on host speaks wire version {e.got}, this agent {wire.VERSION}: upgrade {side}')
        except (Ended, wire.WireError, OSError) as e:
            self.say(f'session ends: {e}')
        finally:
            self.end()
            for t in threads:
                t.join(5)
            for pipe in (self.proc.stdout, self.proc.stderr):
                pipe.close()
        return linked, self.proc.returncode if code is None else code

    def frame(self, frame, linked):
        if not linked:
            wire.check_hello(frame, self.conf['POOL'])
            self.say('linked')
            return True
        if 'ping' in frame:
            self.out.put(wire.encode({'v': wire.VERSION, 'pong': wire.check_ping(frame, 'ping')}))
            return True
        frame = wire.check_request(frame, with_id=True)
        with self.lock:
            if frame['id'] in self.verbs:
                raise wire.WireError(f'request {frame["id"]} is running already')
            if len(self.verbs) >= MAX_VERBS:
                self.out.put(wire.encode(wire.answer(1, stderr=f'fork-fleet-desk-agent: {MAX_VERBS} verbs run '
                                                     'already: not run\n', ident=frame['id'])))
                return True
            v = self.verbs[frame['id']] = Verb(frame['id'], frame, boottime())
        threading.Thread(target=self.do, args=(v,), daemon=True).start()
        return True

    def do(self, v):
        try:
            if v.verb == 'start':
                code, out, err = self.child(v, [PYTHON, '-I', self.conf['WALLS'], 'check'], None, 'WALLS')
                if code != 0:
                    return self.answer(v, 1, '', f'fork-fleet-desk-agent: the walls check exited {code}: no runner '
                                                 f'started\n{out}{err}')
            code, out, err = self.child(v, [self.conf['LAUNCH'], v.verb, *v.args], v.stdin, 'LAUNCH')
            if code == wire.UNKNOWN:
                code, err = 1, err + f'fork-fleet-desk-agent: the launcher itself exited {wire.UNKNOWN}\n'
            self.answer(v, code, out, err)
        except (Bad, OSError) as e:
            self.answer(v, 1, '', f'fork-fleet-desk-agent: not run: {e}\n')
        except Ended:
            pass        # killed: police() or end() answered it

    def child(self, v, argv, stdin, key):
        owned_safe(self.conf[key], 'file')
        with v.lock:
            if v.killed:
                raise Ended()
            v.proc = subprocess.Popen(argv, stdin=subprocess.PIPE if stdin else subprocess.DEVNULL, cwd='/', env=ENV,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        out, err = v.proc.communicate(stdin.encode() if stdin else None)
        if v.killed:
            raise Ended()
        code = v.proc.returncode
        err = err.decode('utf-8', 'replace')
        if code < 0:
            name = 'the walls check' if key == 'WALLS' else v.verb
            code, err = 1, err + f'fork-fleet-desk-agent: {name} died of signal {-code}\n'
        return code, out.decode('utf-8', 'replace'), err

    def answer(self, v, code, out, err):
        with v.lock:
            if v.answered:
                return
            v.answered = True
        with self.lock:
            self.verbs.pop(v.ident, None)
        self.out.put(wire.encode(wire.answer(code, out, err, ident=v.ident)))

    def kill(self, v, sig):
        with v.lock:
            v.killed = True
            if v.proc is not None and v.proc.returncode is None:
                try:
                    os.killpg(v.proc.pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

    def police(self, now):
        """Each verb at its limit: SIGTERM, then SIGKILL, then answered UNKNOWN (also when a pipe is still held)."""
        with self.lock:
            verbs = list(self.verbs.values())
        for v in verbs:
            if now >= v.limit - TERM_BEFORE:
                self.kill(v, signal.SIGTERM if now < v.limit - KILL_BEFORE else signal.SIGKILL)
            if now >= v.limit:
                self.kill(v, signal.SIGKILL)
                self.say(f'{v.verb} ran into its limit of {wire.limit(v.verb)} s: killed')
                self.answer(v, wire.UNKNOWN, '', f'fork-fleet-desk-agent: {v.verb} killed at its limit of '
                                                 f'{wire.limit(v.verb)} s: it may or may not have run\n')

    def end(self):
        """Every running verb killed (TERM, then KILL), ssh ended."""
        with self.lock:
            verbs = list(self.verbs.values())
        for sig in (signal.SIGTERM, signal.SIGKILL):
            for v in verbs:
                self.kill(v, sig)
            deadline = time.monotonic() + 2
            while sig == signal.SIGTERM and time.monotonic() < deadline and \
                    any(v.proc and v.proc.poll() is None for v in verbs):
                time.sleep(0.05)
        if verbs:
            self.say(f'session ended: killed {", ".join(v.verb for v in verbs)}')
        self.out.put(None)
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        for v in verbs:
            if v.proc is not None:
                try:
                    v.proc.wait(5)
                except subprocess.TimeoutExpired:
                    pass

    def write(self):
        while (data := self.out.get()) is not None:
            try:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            except OSError:
                break
        try:
            self.proc.stdin.close()
        except OSError:
            pass

    def relay(self):
        """ssh's own stderr (and serve's log lines): to the log, cleaned, each line cut to 500 characters."""
        for line in self.proc.stderr:
            self.say(f'ssh: {wire.clean(line.decode("utf-8", "replace").rstrip())[:500]}')


def wait(seconds, stopping, now=boottime, gap=slept_for):
    """Waits `seconds` of BOOTTIME; returns early on a stop, or True when the desktop slept meanwhile."""
    until, slept = now() + seconds, gap()
    while not stopping.is_set() and now() < until:
        stopping.wait(min(5.0, max(0.0, until - now())))
        if gap() - slept > SLEPT:
            return True
    return False


def delay(tries, linked, code):
    if code == wire.WRONG_VERSION:
        return MAX
    if linked:
        return random.uniform(*FIRST)
    return min(MAX, BASE * 2 ** tries) * random.uniform(0.5, 1.0)


def run(conf, log=sys.stderr, stopping=None, session=Session):
    stopping = stopping or threading.Event()
    try:
        os.makedirs(conf['RUN_DIR'], 0o700, exist_ok=True)
        owned_safe(conf['RUN_DIR'], 'dir')
        lock = os.open(os.path.join(conf['RUN_DIR'], 'agent.lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError as e:
        raise Bad(f'{conf["RUN_DIR"]}: {e.strerror}') from None
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print('fork-fleet-desk-agent: another desk agent runs on this desktop: one per desktop', file=log)
        return 1
    tries = 0
    while not stopping.is_set():
        began = boottime()
        linked, code = session(conf, log, stopping).run()
        if stopping.is_set():
            break
        up = linked and boottime() - began >= BASE    # a link that drops at once counts as a failure: no hammering
        tries = 0 if up else tries + 1
        seconds = delay(max(0, tries - 1), up, code)
        print(f'fork-fleet-desk-agent {conf["POOL"]}: ssh exited {code}; dialling again in {round(seconds)} s',
              file=log, flush=True)
        if wait(seconds, stopping):
            tries = 0
            print(f'fork-fleet-desk-agent {conf["POOL"]}: the desktop slept: dialling now', file=log, flush=True)
    os.close(lock)
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog='fork-fleet-desk-agent.py')
    p.add_argument('--config', default=CONF)
    p.add_argument('verb', choices=('run', 'check'))
    a = p.parse_args(argv)
    try:
        conf = load(a.config)
        if a.verb == 'check':
            owned_safe(conf['LAUNCH'], 'file')
            owned_safe(conf['WALLS'], 'file')
            print(f'pool {conf["POOL"]}: {conf["USER"]}@{conf["HOST"]}:{conf["PORT"]}, launcher {conf["LAUNCH"]}, '
                  f'walls {conf["WALLS"]}: settings ok')
            return 0
        stopping = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            signal.signal(sig, lambda *_: stopping.set())
        return run(conf, stopping=stopping)
    except Bad as e:
        print(f'fork-fleet-desk-agent: {e}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
