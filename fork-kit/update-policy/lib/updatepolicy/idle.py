"""Is this machine idle enough to restart? Looks only; changes nothing.

look(host, settings) returns the reasons it is busy (an empty list: idle). Busy is any of:

- a T3 agent scope with a process in it, under any user's systemd manager (the rule of heavy's `heavy facts`: a
  scope right below an `app-t3code*` slice, or a `t3code-*.scope` anywhere below the manager);
- a heavy job: a scope with a process in it in a user's heavy slice, or a live ticket in a user's heavy queue
  (a job still waiting for memory);
- a running container of the system Docker, or of a listed user's rootless Docker (by name, less the ignored ones);
- a block inhibitor lock on shutdown (`systemd-inhibit --what=shutdown --mode=block CMD`: the one standard way
  for any job to say "do not restart now");
- a package manager at work (apt, apt-get, aptitude, dpkg, unattended-upgrade);
- a busy script of the machine file (free/v1, fork-kit/common/FREE.md) that does not answer "free".

Anything it cannot read counts as busy ("cannot tell"): a restart is never the answer to a question it could not
ask.
"""
import errno, fcntl, json, os, pwd, re, socket, subprocess, sys

from updatepolicy import settings as _settings

AGENT_SCOPE_RE = re.compile(r'^t3code-[A-Za-z0-9_.:\\-]+\.scope\Z')
USER_SLICE_RE = re.compile(r'^user-([0-9]{1,10})\.slice\Z')
TICKET_RE = re.compile(r'^t-[0-9]{1,15}-[0-9]{1,10}\.json\Z')
PACKAGE_TOOLS = frozenset({'apt', 'apt-get', 'aptitude', 'dpkg', 'unattended-upgrade'})
INTERPRETERS = re.compile(r'^(python[0-9.]*|perl|sh|bash)\Z')
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}
DOCKER_LIMIT = 4 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Host:
    """The machine as idle and tick read it. Tests set ROOT (every file path is read under it) and replace the
    methods that run programs or ask sockets."""
    ROOT = ''
    CGROUP_USERS = '/sys/fs/cgroup/user.slice'
    RUN_USER = '/run/user'
    PROC = '/proc'
    SYSTEM_DOCKER = '/run/docker.sock'
    BIN = None              # bin/update-policy, run as a user to ask that user's Docker (cli sets it)

    def p(self, path):
        return self.ROOT + path

    def run(self, argv, timeout=20, user=None, stdin=None, env=None):
        """(exit code, stdout, stderr); exit code None when it could not run or timed out (stderr says why)."""
        extra = {}
        if user is not None:
            extra = dict(user=user.pw_uid, group=user.pw_gid, extra_groups=[])
        try:
            r = subprocess.run(argv, input=stdin, capture_output=True, text=True, timeout=timeout, env=env or ENV,
                               cwd='/', **extra)
        except FileNotFoundError:
            return None, '', f'{argv[0]}: not installed'
        except subprocess.TimeoutExpired:
            return None, '', f'{argv[0]}: no answer within {timeout} s'
        except OSError as e:
            return None, '', f'{argv[0]}: {e.strerror}'
        return r.returncode, r.stdout, r.stderr

    def user(self, name):
        try:
            return pwd.getpwnam(name)
        except KeyError:
            return None

    def docker_names(self, sock, user=None):
        """The names of the running containers of the Docker at `sock`: a list, or a string saying why not. With
        `user` (a pwd entry), asked as that user, never as root: the socket is in a folder that user controls."""
        if not os.path.exists(self.p(sock)):
            return []
        if user is None or user.pw_uid == os.geteuid():
            return docker_running(self.p(sock))
        if os.geteuid() != 0:
            return f'cannot ask {user.pw_name}\'s Docker (run as root)'
        code, out, err = self.run([sys.executable, '-I', '-B', self.BIN, 'docker-ps', sock], timeout=15, user=user)
        if code != 0:
            return (err.strip() or f'exit {code}')[:200]
        try:
            names = json.loads(out)
            if isinstance(names, list) and all(isinstance(n, str) for n in names):
                return names
        except ValueError:
            pass
        return 'a broken answer'

    def free_answer(self, path):
        import free_contract
        verdict, reason, _facts = free_contract.ask(path)
        return verdict, reason


# -- Docker -----------------------------------------------------------------------------------------------------------

def _unchunk(body):
    out, rest = b'', body
    while rest:
        line, _, rest = rest.partition(b'\r\n')
        size = int(line.split(b';')[0].strip() or b'0', 16)
        if size == 0:
            break
        out, rest = out + rest[:size], rest[size + 2:]
    return out


def docker_running(sock, timeout=5):
    """The names of the running containers of the Docker Engine API at the unix socket `sock` (GET
    /containers/json lists running ones only), or a string saying why not."""
    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.settimeout(timeout)
        with s:
            s.connect(sock)
            s.sendall(b'GET /containers/json HTTP/1.0\r\nHost: docker\r\n\r\n')
            data = b''
            while len(data) <= DOCKER_LIMIT:
                chunk = s.recv(65536)
                if not chunk:
                    break
                data += chunk
    except OSError as e:
        return f'cannot ask Docker at {sock} ({e.strerror or e})'
    head, _, body = data.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    if not lines[0].startswith('HTTP/') or lines[0].split(' ')[1:2] != ['200']:
        return f'Docker at {sock} answered {lines[0][:80]!r}'
    if any(x.lower().replace(' ', '') == 'transfer-encoding:chunked' for x in lines[1:]):
        body = _unchunk(body)
    try:
        items = json.loads(body.decode('utf-8'))
        names = []
        for c in items:
            n = (c.get('Names') or [c.get('Id', '?')[:12]])[0]
            names.append(n.lstrip('/'))
        return names
    except (ValueError, AttributeError, TypeError, IndexError, UnicodeDecodeError):
        return f'Docker at {sock} gave a broken answer'


# -- cgroups ----------------------------------------------------------------------------------------------------------

def _populated(folder):
    """A cgroup folder with a process in it (or below). Unreadable: True, the safe side."""
    try:
        with open(os.path.join(folder, 'cgroup.events'), encoding='utf-8') as f:
            for line in f:
                if line.startswith('populated '):
                    return line.split()[1] != '0'
    except OSError:
        pass
    return True


def _slice_path(name):
    """systemd's folder of a slice: a-b.slice is a.slice/a-b.slice."""
    parts = name[:-len('.slice')].split('-')
    return '/'.join('-'.join(parts[:i + 1]) + '.slice' for i in range(len(parts)))


def managers(host):
    """[(uid, folder)] of every user manager's cgroup; raises OSError when user.slice exists but cannot be listed."""
    base = host.p(host.CGROUP_USERS)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    out = []
    for name in names:
        m = USER_SLICE_RE.match(name)
        if m:
            folder = os.path.join(base, name, f'user@{m.group(1)}.service')
            if os.path.isdir(folder):
                out.append((int(m.group(1)), folder))
    return out


def agent_scopes(manager):
    """T3's agent scopes below one user manager's cgroup that have a process in them."""
    found = []
    for dirpath, dirs, _files in os.walk(manager):
        name, parent = os.path.basename(dirpath), os.path.basename(os.path.dirname(dirpath))
        if dirpath != manager and name.endswith('.scope') and (
                parent.startswith('app-t3code') and parent.endswith('.slice') or AGENT_SCOPE_RE.match(name)):
            if _populated(dirpath):
                found.append(name)
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.endswith(('.slice', '.scope', '.service'))]
    return found


def heavy_scopes(manager, slice_name):
    box = os.path.join(manager, _slice_path(slice_name))
    try:
        names = sorted(n for n in os.listdir(box) if n.endswith('.scope'))
    except FileNotFoundError:
        return []
    return [n for n in names if _populated(os.path.join(box, n))]


def live_tickets(host, uid, slice_name):
    """heavy's tickets of one user held by a live heavy (its flock): running and waiting jobs. Opened with no link
    followed, as root in a folder the user controls."""
    count = 0
    try:
        fd = os.open(host.p(f'{host.RUN_USER}/{uid}'), DIR_FLAGS)
    except OSError:
        return 0
    try:
        for part in ('heavy-queue', slice_name):
            try:
                new = os.open(part, DIR_FLAGS, dir_fd=fd)
            except OSError:
                return 0
            os.close(fd)
            fd = new
        for name in sorted(os.listdir(fd)):
            if not TICKET_RE.match(name):
                continue
            try:
                t = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            except OSError:
                continue
            try:
                fcntl.flock(t, fcntl.LOCK_SH | fcntl.LOCK_NB)      # nobody holds it: that heavy is gone
            except OSError as e:
                if e.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
                    count += 1
            finally:
                os.close(t)
    finally:
        os.close(fd)
    return count


# -- processes and locks ----------------------------------------------------------------------------------------------

def package_tools(host):
    """The package managers at work: [(pid, name)]. A script run by an interpreter counts by its script's name
    (unattended-upgrade is a python3 script; its shutdown helper, which waits all day, is another name)."""
    out = []
    try:
        pids = [n for n in os.listdir(host.p(host.PROC)) if n.isdigit()]
    except OSError:
        return out
    for pid in pids:
        try:
            with open(host.p(f'{host.PROC}/{pid}/cmdline'), 'rb') as f:
                argv = [a.decode('utf-8', 'replace') for a in f.read(8192).split(b'\0') if a]
        except OSError:
            continue
        if not argv:
            continue
        names = [os.path.basename(argv[0])]
        if INTERPRETERS.match(names[0]):
            names += [os.path.basename(a) for a in argv[1:] if not a.startswith('-')][:1]
        hit = [n for n in names if n in PACKAGE_TOOLS]
        if hit:
            out.append((int(pid), hit[0]))
    return sorted(out)


def inhibitors(host):
    """Block locks on shutdown: [(who, why, uid)], or a string saying why they cannot be read, or None when this
    machine has no logind to hold any (busctl not installed)."""
    code, out, err = host.run(['busctl', '--json=short', 'call', 'org.freedesktop.login1', '/org/freedesktop/login1',
                               'org.freedesktop.login1.Manager', 'ListInhibitors'], timeout=10)
    if code is None and 'not installed' in err:
        return None
    if code != 0:
        return f'cannot read the inhibitor locks ({(err.strip() or f"exit {code}")[:160]})'
    try:
        rows = json.loads(out)['data'][0]
        found = []
        for what, who, why, mode, uid, _pid in rows:
            if mode == 'block' and 'shutdown' in what.split(':'):
                found.append((who, why, uid))
        return found
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return 'cannot read the inhibitor locks (a broken answer from busctl)'


# -- all of it --------------------------------------------------------------------------------------------------------

def look(host, settings):
    """The reasons the machine is busy now ([]: idle)."""
    busy = []
    try:
        mans = managers(host)
    except OSError as e:
        busy.append(f'cannot tell: {host.CGROUP_USERS} cannot be listed ({e.strerror})')
        mans = []
    for uid, folder in mans:
        try:
            agents = agent_scopes(folder)
            jobs = heavy_scopes(folder, settings['heavySlice'])
        except OSError as e:
            busy.append(f'cannot tell: user {uid}\'s cgroups cannot be read ({e.strerror})')
            continue
        if agents:
            busy.append(f'user {uid}: {len(agents)} T3 agent scope(s) running ({", ".join(agents[:3])})')
        tickets = live_tickets(host, uid, settings['heavySlice'])
        if jobs or tickets:
            busy.append(f'user {uid}: heavy jobs ({len(jobs)} running, {tickets} live ticket(s))')
    docker = []
    if settings['systemDocker']:
        docker.append(('system Docker', host.SYSTEM_DOCKER, None))
    for name in settings['containerUsers']:
        user = host.user(name)
        if user is None:
            busy.append(f'cannot tell: containerUsers names {name}, who is not a user here')
            continue
        docker.append((f'{name}\'s Docker', f'{host.RUN_USER}/{user.pw_uid}/docker.sock', user))
    for label, sock, user in docker:
        names = host.docker_names(sock, user)
        if isinstance(names, str):
            busy.append(f'cannot tell: {label}: {names}')
            continue
        running = [n for n in names if not _settings.ignored_container(settings, n)]
        if running:
            busy.append(f'{label}: {len(running)} running container(s) ({", ".join(sorted(running)[:5])})')
    locks = inhibitors(host)
    if isinstance(locks, str):
        busy.append(f'cannot tell: {locks}')
    elif locks:
        busy.append('inhibitor lock: ' + '; '.join(f'{who} (uid {uid}): {why}' for who, why, uid in locks[:3]))
    tools = package_tools(host)
    if tools:
        busy.append('package manager at work: ' + ', '.join(f'{n} ({pid})' for pid, n in tools[:3]))
    for path in settings['busyScripts']:
        verdict, reason = host.free_answer(path)
        if verdict != 'free':
            busy.append(f'busy script {path}: {verdict}' + (f' ({reason})' if reason else ''))
    return busy
