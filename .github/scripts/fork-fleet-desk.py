#!/usr/bin/env python3
"""Fork only: the always-on host's side of the desk link (.github/fork-fleet.md, "The desk link").

A desktop accepts no connection and holds no GitHub token: its desk agent dials out to the always-on host over ssh,
as the dedicated, unprivileged desk-link user (USER), with a key that may only run `serve` for its own pool, and the
pool's runners are started through that session.

  python3 -I fork-fleet-desk.py serve POOL
      The ssh forced command, as the desk-link user, never root (its authorized_keys: `restrict,command="FORCED
      POOL" ssh-ed25519 ...`, written by fork-fleet-desk-key.py). Takes DIR/POOL.lock (one session per pool: a
      second one exits 3 at once), reads the agent's hello, then listens on the socket DIR/POOL.sock and passes each
      request to the agent and its answer back. Pings the agent whenever it has heard nothing from it, or sent nothing
      to it, for PING s (the agent drops a link that says nothing for DEAD s); with no word from it in DEAD s, or on
      its end of the session, every request in flight is answered "outcome unknown" and the socket is removed.
  python3 -I fork-fleet-desk.py launch POOL VERB ARGS...
      A pool's `launch` command for `fork-fleet.py dispatch`: ["python3", "-I", ".../fork-fleet-desk.py", "launch",
      POOL]. Sends one launcher verb (`start` with its stdin) through the live session and prints the desktop's
      stdout and stderr and exits with its exit code. No live session: exit 3 at once, nothing sent. No answer in
      the verb's time (fork-fleet-desk-wire.py, "Time"): exit 75, the verb may or may not have run.
  The same forced command asked `seeds` or `seed NAME` (SSH_ORIGINAL_COMMAND): a desktop's warm caches, not a
  session (fork-fleet-desk-seed.py; its lock DIR/POOL.seed.lock).
DIR is /run/fork-fleet-desk, made at boot by systemd-tmpfiles (runbook): owned by the desk-link user, its group the
socket group (who may connect: the dispatcher's user, or root), mode 0710 or 0750. The socket is 0660 in that group,
and serve lets in only root, its own user and members of that group (SO_PEERCRED). Not as root (tests),
$FORK_FLEET_DESK_DIR may name another DIR; for serve that only moves its own socket away from the dispatcher (sshd
passes no such variable unless its AcceptEnv names it). $FORK_FLEET_DESK_USER names the desk-link user for `launch`
(default USER). The link keeps no state of its own: the live session is the state. Wire format:
fork-fleet-desk-wire.py. Standard library only.
"""
import grp, importlib.util, os, pwd, selectors, signal, socket, stat, struct, sys, time

sys.dont_write_bytecode = True   # a root-owned install directory gets no __pycache__
HERE = os.path.dirname(os.path.realpath(__file__))
_spec = importlib.util.spec_from_file_location('fork_fleet_desk_wire', os.path.join(HERE, 'fork-fleet-desk-wire.py'))
wire = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wire)

DIR = '/run/fork-fleet-desk'
USER = 'fleet-desk'          # the desk-link user's default name: the same as fork-fleet-desk-key.py's USER
INSTALLED = '/opt/fork-fleet/fork-fleet-desk.py'
FORCED = f'/usr/bin/python3 -I {INSTALLED} serve'   # + POOL: the forced command, the same as fork-fleet-desk-key.py's SERVE
TMPFILES = f'd {DIR} 0710 {USER} {USER} -'  # the runbook's tmpfiles.d line (default user and socket group)
HELLO = 30        # seconds the agent has for its hello
PING, DEAD = 15, 45
CLIENT = 5        # seconds a socket client has to send its request, and to take its answer


class Refused(Exception):
    def __init__(self, msg, code=1):
        super().__init__(msg)
        self.code = code


def sock_dir(env=os.environ, euid=None):
    euid = os.geteuid() if euid is None else euid
    return DIR if euid == 0 else env.get('FORK_FLEET_DESK_DIR') or DIR


def safe_dir(path, owners, make=False):
    """The socket directory's stat: not a symlink, owned by one of `owners`, its group may at most pass through or
    list it, anyone else nothing."""
    if make:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        except PermissionError:
            raise Refused(f'cannot make {path}: make it at boot with systemd-tmpfiles ("{TMPFILES}", '
                          '.github/fork-fleet.md, "The desk link")') from None
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode) or st.st_uid not in owners or st.st_mode & 0o027:
        raise Refused(f'{path} must be a directory of uid {" or ".join(map(str, owners))}, mode 0710 or 0750 (an '
                      f'older one, of root, 0700? the tmpfiles line "{TMPFILES}" fixes it at the next boot or '
                      '`systemd-tmpfiles --create`)')
    return st


def desk_uid(env):
    """The uid of the desk-link user ($FORK_FLEET_DESK_USER, default USER), or None when there is no such user."""
    try:
        return pwd.getpwnam(env.get('FORK_FLEET_DESK_USER') or USER).pw_uid
    except KeyError:
        return None


def pool_ok(pool):
    if not wire.POOL.fullmatch(pool):
        raise Refused(f'not a pool name: {pool[:40]!r}', 2)


# ---------------------------------------------------------------- serve

def serve(pool, path, inp=0, out=1, log=sys.stderr, ping=PING, dead=DEAD, hello_wait=HELLO, now=time.monotonic):
    pool_ok(pool)
    if os.geteuid() == 0:
        raise Refused(f'serve never runs as root: give the key to the desk-link user ({USER}), .github/fork-fleet.md, '
                      '"The desk link"')
    gid = safe_dir(path, (os.geteuid(),), make=True).st_gid
    lock_path = os.path.join(path, f'{pool}.lock')
    try:
        lock = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    except PermissionError:
        lock = None
    if lock is None or os.fstat(lock).st_uid != os.geteuid():
        if lock is not None:
            os.close(lock)
        raise Refused(f'{lock_path} is not this user\'s (left by an older serve that ran as root?): remove it, or '
                      'restart the host')
    try:
        import fcntl
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Refused(f'another desk session serves pool {pool}', wire.NO_SESSION) from None
        return Session(pool, path, inp, out, log, ping, dead, hello_wait, now, gid).run()
    finally:
        os.close(lock)


class Session:
    def __init__(self, pool, path, inp, out, log, ping, dead, hello_wait, now, gid):
        self.pool, self.inp, self.out, self.log, self.now, self.gid = pool, inp, out, log, now, gid
        self.ping, self.dead, self.hello_wait = ping, dead, hello_wait
        self.sock_path = os.path.join(path, f'{pool}.sock')
        self.reader, self.outbuf, self.sel = wire.Reader(), bytearray(), selectors.DefaultSelector()
        self.pending = {}   # request id: (client socket, verb, deadline)
        self.waiting = {}   # client socket: (bytes so far, deadline)
        self.next_id, self.pings = 1, 0
        self.accept_error = None

    def say(self, msg):
        print(f'fork-fleet-desk serve {self.pool}: {msg}', file=self.log, flush=True)

    def run(self):
        os.set_blocking(self.out, False)
        self.sel.register(self.inp, selectors.EVENT_READ, 'agent')
        listener = None
        try:
            hello = self.agent_frames(self.now() + self.hello_wait)
            wire.check_hello(hello, self.pool)
            self.send(hello)
            try:
                os.unlink(self.sock_path)     # a socket left by a session that died: this one holds the lock
            except FileNotFoundError:
                pass
            listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            old = os.umask(0o177)
            try:
                listener.bind(self.sock_path)
            finally:
                os.umask(old)
            try:                                 # 0660 in the socket group, before anyone may connect (listen)
                os.chown(self.sock_path, -1, self.gid)
            except PermissionError:
                raise Refused(f'cannot give the socket group {self.gid}: this user is not in it') from None
            os.chmod(self.sock_path, 0o660)
            listener.listen(8)
            listener.setblocking(False)
            self.sel.register(listener, selectors.EVENT_READ, 'listen')
            self.say('session open')
            return self.loop(listener)
        except wire.VersionError as e:
            self.say(f'session ends: {e}')
            return wire.WRONG_VERSION
        except (wire.WireError, Refused, OSError) as e:
            self.say(f'session ends: {e}')
            return 1
        finally:
            if listener is not None:
                try:
                    os.unlink(self.sock_path)
                except FileNotFoundError:
                    pass
                listener.close()
            for ident in list(self.pending):
                self.reply(ident, wire.answer(wire.UNKNOWN, stderr=self.unknown(self.pending[ident][1])))
            for client in list(self.waiting):
                self.drop(client)

    def unknown(self, verb):
        return f'fork-fleet-desk: pool {self.pool}: the desk session ended: {verb} may or may not have run\n'

    def agent_frames(self, deadline):
        """The first frame from the agent (the hello), waiting until `deadline`."""
        while self.now() < deadline:
            for key, _ in self.sel.select(max(0.0, deadline - self.now())):
                frames = self.read_agent()
                if len(frames) > 1:
                    raise wire.WireError('frames before the hello was answered')
                if frames:
                    return frames[0]
        raise Refused(f'no hello in {self.hello_wait} s')

    def read_agent(self):
        try:
            data = os.read(self.inp, 65536)
        except BlockingIOError:
            return []
        if not data:
            raise Refused('the desktop ended the session')
        self.heard, self.pings = self.now(), 0
        return self.reader.feed(data)

    def send(self, frame):
        self.outbuf += wire.encode(frame)
        self.sent = self.now()
        self.flush()

    def flush(self):
        try:
            while self.outbuf:
                self.outbuf = self.outbuf[os.write(self.out, self.outbuf):]
        except BlockingIOError:
            pass

    def loop(self, listener):
        self.heard = self.sent = self.now()
        while True:
            for key, _ in self.sel.select(min(1.0, self.ping / 4) if not self.outbuf else 0.05):
                if key.data == 'agent':
                    for frame in self.read_agent():
                        self.from_agent(frame)
                elif key.data == 'listen':
                    self.accept(listener)
                else:
                    self.from_client(key.fileobj)
            self.flush()
            t = self.now()
            if t - self.heard > self.dead:
                raise Refused(f'no word from the desktop in {self.dead} s')
            # also when only the agent talks (answers spaced under PING s): its side counts frames FROM serve
            if t - self.heard > self.ping * (self.pings + 1) or t - self.sent > self.ping:
                self.pings += 1
                self.send({'v': wire.VERSION, 'ping': self.pings})
            for client, (_, deadline) in list(self.waiting.items()):
                if t > deadline:
                    self.drop(client)
            for ident, (_, verb, deadline) in list(self.pending.items()):
                if t > deadline:   # the agent should have killed it and answered by now
                    self.reply(ident, wire.answer(wire.UNKNOWN, stderr=f'fork-fleet-desk: pool {self.pool}: {verb}: '
                                                  'no answer in time: it may or may not have run\n'))

    def from_agent(self, frame):
        if 'pong' in frame:
            wire.check_ping(frame, 'pong')
            return
        got = wire.check_answer(frame, with_id=True)
        ident = got.pop('id')
        if ident in self.pending:
            self.reply(ident, got)
        elif ident >= self.next_id:
            raise wire.WireError(f'an answer to request {ident}, never sent')

    def accept(self, listener):
        """One socket client, if it may (peer_ok). An error (too many open files, a client gone) costs that client
        only, never the session; it is said once until it changes."""
        try:
            client, _ = listener.accept()
        except BlockingIOError:
            return
        except OSError as e:
            if str(e) != self.accept_error:
                self.accept_error = str(e)
                self.say(f'a socket client could not be taken: {e}')
            time.sleep(0.05)                 # the listener stays readable: do not spin
            return
        try:
            _, uid, gid = struct.unpack('3i', client.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
        except OSError:
            client.close()
            return
        if not self.peer_ok(uid, gid):
            client.close()
            return
        client.setblocking(False)
        self.waiting[client] = (b'', self.now() + CLIENT)
        self.sel.register(client, selectors.EVENT_READ, 'client')

    def peer_ok(self, uid, gid):
        """Root, this user, or a member of the socket group (the dispatcher's user, when it is not root)."""
        if uid in (0, os.geteuid()) or gid == self.gid:
            return True
        try:
            return pwd.getpwuid(uid).pw_name in grp.getgrgid(self.gid).gr_mem
        except KeyError:
            return False

    def from_client(self, client):
        buf, deadline = self.waiting[client]
        try:
            data = client.recv(65536)
        except BlockingIOError:
            return
        except OSError:
            self.drop(client)
            return
        buf += data
        if b'\n' not in buf:
            if data and len(buf) < wire.MAX_FRAME:
                self.waiting[client] = (buf, deadline)
            else:
                self.drop(client)
            return
        self.sel.unregister(client)
        del self.waiting[client]
        line, rest = buf.split(b'\n', 1)
        try:
            frame = wire.check_request(wire.decode(line), with_id=False)
            if rest:
                raise wire.WireError('more than one request')
        except wire.WireError as e:
            self.answer_client(client, wire.answer(2, stderr=f'fork-fleet-desk: refused: {e}\n'))
            return
        ident, self.next_id = self.next_id, self.next_id + 1
        self.pending[ident] = (client, frame['verb'], self.now() + wire.serve_seconds(frame['verb']))
        self.send({**frame, 'id': ident})

    def reply(self, ident, frame):
        client = self.pending.pop(ident)[0]
        self.answer_client(client, frame)

    def answer_client(self, client, frame):
        try:
            client.setblocking(True)
            client.settimeout(CLIENT)
            client.sendall(wire.encode(frame))
        except OSError:
            pass   # the client gave up (its own time ran out)
        client.close()

    def drop(self, client):
        self.sel.unregister(client)
        del self.waiting[client]
        client.close()


# ---------------------------------------------------------------- launch

def launch(pool, verb, args, stdin, path, out=sys.stdout, err=sys.stderr, wait=None, env=os.environ):
    """The exit code; the desktop's stdout and stderr written to out and err. Done in `wait` (default
    wire.seconds(verb)) all told: connecting, sending and the answer."""
    began = time.monotonic()
    pool_ok(pool)
    try:
        frame = wire.request(verb, args, stdin if verb == 'start' else None)
    except wire.WireError as e:
        raise Refused(f'refused: {e}', 2) from None
    no_session = Refused(f'pool {pool}: no live desk session (the desktop is off, asleep or not linked): '
                         f'{verb} was not sent', wire.NO_SESSION)
    unknown = Refused(f'pool {pool}: {verb}: no answer from the desktop: it may or may not have run', wire.UNKNOWN)
    deadline = began + (wire.seconds(verb) if wait is None else wait)
    sock_path = os.path.join(path, f'{pool}.sock')
    owners = tuple(u for u in (0, os.geteuid(), desk_uid(env)) if u is not None)
    try:
        owner = safe_dir(path, owners).st_uid
        st = os.lstat(sock_path)
    except FileNotFoundError:
        raise no_session from None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != owner:
        raise Refused(f'{sock_path} is not a socket of uid {owner}')
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(max(0.01, min(CLIENT, deadline - time.monotonic())))
        try:
            s.connect(sock_path)
        except (ConnectionRefusedError, FileNotFoundError):
            raise no_session from None
        except socket.timeout:
            raise Refused(f'pool {pool}: the desk session takes no connection (busy or stuck): {verb} was not sent',
                          wire.NO_SESSION) from None
        except OSError as e:
            raise Refused(f'cannot reach {sock_path}: {e.strerror}') from None
        try:
            s.settimeout(max(0.01, min(CLIENT, deadline - time.monotonic())))
            s.sendall(wire.encode(frame))
            buf = b''
            while b'\n' not in buf:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise unknown
                s.settimeout(left)
                data = s.recv(65536)
                if not data or len(buf) > wire.MAX_FRAME:
                    raise unknown
                buf += data
            got = wire.check_answer(wire.decode(buf.split(b'\n', 1)[0]), with_id=False)
        except (OSError, wire.WireError):
            raise unknown from None
    out.write(got['stdout'])
    err.write(got['stderr'])
    if got['exit'] == wire.NO_SESSION:   # 3 means "nothing was sent" only when the link says it
        err.write(f'fork-fleet-desk: pool {pool}: {verb}: the desktop\'s launcher exited {wire.NO_SESSION}\n')
        return 1
    return got['exit']


def seed_side(pool, asked, env):
    """A desktop's seed session (fork-fleet-desk-seed.py): the same key and forced command, another command."""
    pool_ok(pool)
    if os.geteuid() == 0:
        raise Refused('serve never runs as root', 2)
    path = sock_dir(env)
    safe_dir(path, (os.geteuid(),), make=True)
    spec = importlib.util.spec_from_file_location('fork_fleet_desk_seed', os.path.join(HERE, 'fork-fleet-desk-seed.py'))
    seed = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(seed)
    return seed.offer(pool, asked, path)


def main(argv=None, env=os.environ):
    argv = sys.argv[1:] if argv is None else argv
    try:
        if len(argv) == 2 and argv[0] == 'serve' and (env.get('SSH_ORIGINAL_COMMAND') == 'seeds' or
                                                      env.get('SSH_ORIGINAL_COMMAND', '').startswith('seed ')):
            return seed_side(argv[1], env['SSH_ORIGINAL_COMMAND'], env)
        if len(argv) == 2 and argv[0] == 'serve':
            for sig in (signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda *_: sys.exit(1))   # through serve's finally: the socket goes, clients learn
            return serve(argv[1], sock_dir(env))
        if len(argv) >= 3 and argv[0] == 'launch':
            for f in (sys.stdin, sys.stdout, sys.stderr):   # odd bytes are refused or shown, never a traceback
                f.reconfigure(errors='replace')
            stdin = sys.stdin.read(wire.MAX_FRAME) if argv[2] == 'start' else None
            return launch(argv[1], argv[2], argv[3:], stdin, sock_dir(env), env=env)
        raise Refused('usage: serve POOL | launch POOL VERB ARGS...', 2)
    except Refused as e:
        print(f'fork-fleet-desk: {e}', file=sys.stderr)
        return e.code
    except OSError as e:
        print(f'fork-fleet-desk: {e.filename or ""}: {e.strerror}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
