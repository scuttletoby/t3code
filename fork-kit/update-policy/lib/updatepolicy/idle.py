"""Is this machine idle enough to restart? Looks only; changes nothing (it starts no daemon: a container tool is asked
only while its daemon runs).

look(host, settings) returns the reasons it is busy (an empty list: idle). Busy is any of:

- no cgroup v2 at /sys/fs/cgroup: agents and heavy jobs cannot be counted;
- a T3 agent scope with a process in it, under any user's systemd manager (the rule of heavy's `heavy facts`: a
  scope right below an `app-t3code*` slice, or a `t3code-*.scope` anywhere below the manager);
- T3 running without agent scopes (T3 turns them off when it has no user manager: a system service, no
  XDG_RUNTIME_DIR, a failed systemd-run probe): any T3 server, whatever started it, and any process with T3's
  environment markers, unless it runs under its user's manager with an app-t3code slice there (scopes on);
- a heavy job: a scope with a process in it in a user's heavy slice, or a live ticket in a user's heavy queue;
- a running container or VM: Docker (system, and the rootless one of each of containerUsers), Podman and CRI-O
  (each running container has a conmon), incus, LXD, classic LXC, libvirt (qemu:///system), less ignoreContainers;
- a block inhibitor lock on shutdown or sleep (`systemd-inhibit --what=shutdown --mode=block CMD`); logged-in
  sessions do not count;
- a package manager at work (apt, apt-get, aptitude, dpkg, unattended-upgrade);
- a busy script of the machine file (free/v1, fork-kit/common/FREE.md) that does not answer "free".

Anything it cannot read counts as busy ("cannot tell: ..."): a restart is never the answer to a question it could
not ask. Text from users (inhibitor reasons, container names, a script's answer) is made one safe line.
"""
import errno, fcntl, json, os, pwd, re, socket, subprocess, sys

from updatepolicy import settings as _settings

one_line = _settings.one_line
AGENT_SCOPE_RE = re.compile(r'^t3code-[A-Za-z0-9_.:\\-]+\.scope\Z')
USER_SLICE_RE = re.compile(r'^user-([0-9]{1,10})\.slice\Z')
TICKET_RE = re.compile(r'^t-[0-9]{1,15}-[0-9]{1,10}\.json\Z')
PACKAGE_TOOLS = frozenset({'apt', 'apt-get', 'aptitude', 'dpkg', 'unattended-upgrade'})
INTERPRETERS = re.compile(r'^(python[0-9.]*|perl|sh|bash|node|bun|deno)\Z')
T3_NAMES = frozenset({'t3'})                      # the packaged server and npm's `t3` link
T3_ENTRIES = ('/apps/server/dist/bin.mjs', '/apps/server/src/bin.ts', '/apps/server/scripts/dev.ts')
T3_PACKAGE_ENTRIES = ('/dist/bin.mjs', '/src/bin.ts', '/scripts/dev.ts')     # of a package named "t3"
T3_RUNNERS = frozenset({'npm', 'pnpm', 'bun', 'yarn', 'npx', 'node'})
T3_ENV = (b'T3CODE_HOME=', b'T3CODE_PORT=', b'T3CODE_THREAD_ID=')            # the server's and its agents'
AGENT_CLIS = frozenset({'claude', 'codex', 'opencode', 'cursor-agent', 'grok', 'kiro-cli', 'pi', 'gemini', 'amp',
                        'qwen', 'goose', 'antigravity', 'muse', 'fx'})
ENV = {'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LANG': 'C.UTF-8'}
DOCKER_LIMIT = 4 << 20
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
GONE = (errno.ENOENT, errno.ESRCH)


class Host:
    """The machine as idle and tick read it. Tests set ROOT (every file path is read under it) and replace the
    methods that run programs or ask sockets."""
    ROOT = ''
    CGROUP = '/sys/fs/cgroup'
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
                               cwd='/', errors='replace', **extra)
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

    def kernel_release(self):
        return os.uname().release

    def free_answer(self, path):
        import free_contract
        verdict, reason, _facts = free_contract.ask(path)
        return verdict, reason


class CannotTell(Exception):
    """A fact that cannot be read: the machine counts as busy, with this reason."""


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
        return f'Docker at {sock} answered {one_line(lines[0], 80)}'
    try:
        if any(x.lower().replace(' ', '') == 'transfer-encoding:chunked' for x in lines[1:]):
            body = _unchunk(body)
        items = json.loads(body.decode('utf-8'))
        names = []
        for c in items:
            n = (c.get('Names') or [c.get('Id', '?')[:12]])[0]
            names.append(n.lstrip('/'))
        return names
    except (ValueError, AttributeError, TypeError, IndexError, UnicodeDecodeError):
        return f'Docker at {sock} gave a broken answer'


# -- processes --------------------------------------------------------------------------------------------------------

class Proc:
    __slots__ = ('pid', 'ppid', 'uid', 'comm', 'argv', 'cgroup', 'exe', 'marked')

    def __init__(self, pid, ppid, uid, comm, argv, cgroup, exe='', marked=False):
        self.pid, self.ppid, self.uid, self.comm, self.argv, self.cgroup = pid, ppid, uid, comm, argv, cgroup
        self.exe, self.marked = exe, marked


def _read_proc(host, pid, name, limit=65536):
    with open(host.p(f'{host.PROC}/{pid}/{name}'), 'rb') as f:
        return f.read(limit)


def processes(host):
    """{pid: Proc} of every process. Raises CannotTell when /proc cannot be listed or a process that is there cannot
    be read (one that ended meanwhile is left out)."""
    try:
        pids = [int(n) for n in os.listdir(host.p(host.PROC)) if n.isdigit()]
    except OSError as e:
        raise CannotTell(f'{host.PROC} cannot be listed ({e.strerror})') from None
    out = {}
    for pid in pids:
        try:
            stat = _read_proc(host, pid, 'stat').decode('utf-8', 'replace')
            status = _read_proc(host, pid, 'status').decode('utf-8', 'replace')
            argv = [a.decode('utf-8', 'replace') for a in _read_proc(host, pid, 'cmdline', 8192).split(b'\0') if a]
            try:
                cgroup = _read_proc(host, pid, 'cgroup').decode('utf-8', 'replace')
            except FileNotFoundError:
                cgroup = ''
            environ = b'\0' + _read_proc(host, pid, 'environ', 1 << 20)
            try:
                exe = os.readlink(host.p(f'{host.PROC}/{pid}/exe'))
            except FileNotFoundError:
                exe = ''                    # a kernel thread
        except OSError as e:
            if e.errno in GONE or (e.errno is None and isinstance(e, FileNotFoundError)):
                continue
            raise CannotTell(f'process {pid} cannot be read ({e.strerror})') from None
        try:
            comm = stat[stat.index('(') + 1:stat.rindex(')')]
            ppid = int(stat[stat.rindex(')') + 2:].split()[1])
            uid = int(next(x for x in status.splitlines() if x.startswith('Uid:')).split()[1])
        except (ValueError, IndexError, StopIteration):
            raise CannotTell(f'process {pid}: a broken stat or status') from None
        path = next((x[3:] for x in cgroup.splitlines() if x.startswith('0::')), '')
        marked = any(b'\0' + m in environ for m in T3_ENV)
        out[pid] = Proc(pid, ppid, uid, comm, argv, path, exe, marked)
    return out


def _script_names(argv):
    """The names a command line runs as: the program, and for an interpreter its script."""
    if not argv:
        return []
    names = [os.path.basename(argv[0])]
    if INTERPRETERS.match(names[0]):
        names += [os.path.basename(a) for a in argv[1:] if not a.startswith('-')][:1]
    return names


def package_tools(procs):
    """The package managers at work: [(pid, name)]. A script run by an interpreter counts by its script's name
    (unattended-upgrade is a python3 script; its shutdown helper, which waits all day, is another name)."""
    out = []
    for p in procs.values():
        hit = [n for n in _script_names(p.argv) if n in PACKAGE_TOOLS]
        if hit:
            out.append((p.pid, hit[0]))
    return sorted(out)


def _package_name(host, folder):
    try:
        with open(host.p(os.path.join(folder, 'package.json')), encoding='utf-8') as f:
            return json.loads(f.read(1 << 16)).get('name')
    except (OSError, ValueError, AttributeError):
        return None


def t3_form(host, p):
    """How `p` is a T3 server ('' when it is none): the `t3` binary or link (by name or by exe), any argument that
    is T3's server entry (`apps/server/dist/bin.mjs`, `src/bin.ts`, `scripts/dev.ts`, relative ones resolved against
    the process's folder; in a package named "t3" also `dist/bin.mjs` and the like: npm's install, `npm start`),
    which covers node, bun and the desktop app's Electron running as node, or a package runner (npm, pnpm, bun,
    yarn) starting a package named "t3". Raises CannotTell when its folder cannot be read for a relative entry."""
    names = _script_names(p.argv)
    if any(n in T3_NAMES for n in names) or os.path.basename(p.exe) in T3_NAMES:
        return 'the t3 command'
    cwd = None
    for a in p.argv[1:] or p.argv:
        if not a.endswith(tuple(e[1:] for e in T3_PACKAGE_ENTRIES)):       # 'dist/bin.mjs' as given, too
            continue
        if not os.path.isabs(a):
            if cwd is None:
                try:
                    cwd = os.readlink(host.p(f'{host.PROC}/{p.pid}/cwd'))
                except FileNotFoundError:
                    continue
                except OSError as e:
                    raise CannotTell(f'the folder of process {p.pid} cannot be read ({e.strerror})') from None
            a = os.path.normpath(os.path.join(cwd, a))
        if a.endswith(T3_ENTRIES):
            return f'T3\'s server entry {one_line(a, 120)}'
        for entry in T3_PACKAGE_ENTRIES:
            if a.endswith(entry) and _package_name(host, a[:-len(entry)]) == 't3':
                return f'T3\'s server entry {one_line(a, 120)}'
    runner = [os.path.basename(x) for x in (p.argv[0].split()[:1] + p.argv[1:2])] if p.argv else []
    if any(r in T3_RUNNERS for r in runner) and {'start', 'dev', 'serve'} & set(' '.join(p.argv).split()):
        try:
            cwd = cwd or os.readlink(host.p(f'{host.PROC}/{p.pid}/cwd'))
        except OSError:
            return ''
        if _package_name(host, cwd) == 't3':
            return 'a package runner starting T3'
    return ''


def t3_unscoped(host, procs, sliced):
    """{uid: [(Proc, why)]} of T3's processes that no agent scope holds: T3 servers (any launch form), processes
    with T3's environment markers (agents inherit them; an agent's turn has T3CODE_THREAD_ID) and agent CLIs below
    a server. A process under its user's manager with an app-t3code slice there is left out: its scopes are on
    and counted already."""
    found = {}
    servers = set()
    for p in sorted(procs.values(), key=lambda x: x.pid):
        if p.cgroup.startswith(f'/user.slice/user-{p.uid}.slice/user@{p.uid}.service/') and sliced.get(p.uid):
            continue
        form = t3_form(host, p)
        if form:
            servers.add(p.pid)
        why = form or ('T3\'s environment' if p.marked else '')
        if why:
            found.setdefault(p.uid, []).append((p, why))
    for pid in sorted(servers):
        for k in descendants(procs, pid):
            if k.comm in AGENT_CLIS and not k.marked and not any(q is k for q, _ in found.get(k.uid, [])):
                found.setdefault(k.uid, []).append((k, f'an agent CLI below T3 server {pid}'))
    return found


def descendants(procs, pid):
    kids = {}
    for p in procs.values():
        kids.setdefault(p.ppid, []).append(p)
    out, todo = [], list(kids.get(pid, []))
    while todo:
        p = todo.pop()
        out.append(p)
        todo += kids.get(p.pid, [])
    return out


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
    """[(uid, folder)] of every user manager's cgroup. Raises CannotTell when user.slice exists but cannot be
    listed."""
    base = host.p(host.CGROUP_USERS)
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise CannotTell(f'{host.CGROUP_USERS} cannot be listed ({e.strerror})') from None
    out = []
    for name in names:
        m = USER_SLICE_RE.match(name)
        if m:
            folder = os.path.join(base, name, f'user@{m.group(1)}.service')
            if os.path.isdir(folder):
                out.append((int(m.group(1)), folder))
    return out


def _walk(top):
    """os.walk that raises CannotTell for a folder it cannot list (os.walk alone skips it without a word)."""
    def fail(e):
        raise CannotTell(f'{e.filename} cannot be listed ({e.strerror})')
    return os.walk(top, onerror=fail)


def agent_scopes(manager):
    """(T3's agent scopes below one user manager's cgroup that have a process in it, whether an app-t3code slice
    is there: T3's scopes are on for this user)."""
    found, sliced = [], False
    for dirpath, dirs, _files in _walk(manager):
        name, parent = os.path.basename(dirpath), os.path.basename(os.path.dirname(dirpath))
        if name.startswith('app-t3code') and name.endswith('.slice'):
            sliced = True
        if dirpath != manager and name.endswith('.scope') and (
                parent.startswith('app-t3code') and parent.endswith('.slice') or AGENT_SCOPE_RE.match(name)):
            if _populated(dirpath):
                found.append(name)
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.endswith(('.slice', '.scope', '.service'))]
    return found, sliced


def heavy_scopes(manager, slice_name):
    box = os.path.join(manager, _slice_path(slice_name))
    try:
        names = sorted(n for n in os.listdir(box) if n.endswith('.scope'))
    except FileNotFoundError:
        return []
    except OSError as e:
        raise CannotTell(f'{box} cannot be listed ({e.strerror})') from None
    return [n for n in names if _populated(os.path.join(box, n))]


def live_tickets(host, uid, slice_name):
    """heavy's tickets of one user held by a live heavy (its flock): running and waiting jobs. Opened with no link
    followed, as root in a folder the user controls. A folder that is not there: none. Anything else it cannot
    open, list or lock: CannotTell."""
    where = f'{host.RUN_USER}/{uid}/heavy-queue/{slice_name}'
    count, fd = 0, None
    try:
        fd = os.open(host.p(f'{host.RUN_USER}/{uid}'), DIR_FLAGS)
        for part in ('heavy-queue', slice_name):
            new = os.open(part, DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = new
        for name in sorted(os.listdir(fd)):
            if not TICKET_RE.match(name):
                continue
            try:
                t = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC, dir_fd=fd)
            except FileNotFoundError:
                continue                                    # the job ended meanwhile
            try:
                fcntl.flock(t, fcntl.LOCK_SH | fcntl.LOCK_NB)      # nobody holds it: that heavy is gone
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    raise
                count += 1
            finally:
                os.close(t)
    except FileNotFoundError:
        return count
    except OSError as e:
        raise CannotTell(f'heavy\'s tickets in {where} cannot be read ({e.strerror})') from None
    finally:
        if fd is not None:
            os.close(fd)
    return count


# -- locks ------------------------------------------------------------------------------------------------------------

def inhibitors(host):
    """Block locks on shutdown or sleep: [(who, why, uid)]. Raises CannotTell when they cannot be read (busctl
    missing too). The tick asks again right before it restarts."""
    code, out, err = host.run(['busctl', '--json=short', 'call', 'org.freedesktop.login1', '/org/freedesktop/login1',
                               'org.freedesktop.login1.Manager', 'ListInhibitors'], timeout=10)
    if code != 0:
        raise CannotTell(f'the inhibitor locks cannot be read ({one_line(err.strip() or f"exit {code}", 160)})')
    try:
        rows = json.loads(out)['data'][0]
        found = []
        for what, who, why, mode, uid, _pid in rows:
            if mode == 'block' and {'shutdown', 'sleep'} & set(what.split(':')):
                found.append((who, why, uid))
        return found
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        raise CannotTell('the inhibitor locks cannot be read (a broken answer from busctl)') from None


# -- containers and VMs -----------------------------------------------------------------------------------------------

def _running(procs, comms, uid=None):
    return [p for p in procs.values() if p.comm in comms and (uid is None or p.uid == uid)]


def _csv_running(out):
    """`NAME,STATE` lines (incus and LXD `list --format csv -c ns`): the names that run."""
    return [line.split(',')[0] for line in out.splitlines() if line.strip().upper().endswith(',RUNNING')]


def _conmon_name(p):
    for i, a in enumerate(p.argv):
        if a in ('-n', '--name') and i + 1 < len(p.argv):
            return p.argv[i + 1]
        if a.startswith('--name='):
            return a[7:]
    return f'conmon {p.pid}'


def containers(host, settings, procs):
    """[(label, [names])] of what runs; raises CannotTell for one it cannot ask."""
    out = []
    users = []
    for name in settings['containerUsers']:
        user = host.user(name)
        if user is None:
            raise CannotTell(f'containerUsers names {name}, who is not a user here')
        users.append(user)
    if settings['systemDocker'] and _running(procs, ('dockerd',), 0):      # never wake a socket-activated Docker
        out.append(('system Docker', host.docker_names(host.SYSTEM_DOCKER)))
    for user in users:
        if _running(procs, ('dockerd',), user.pw_uid):
            out.append((f'{user.pw_name}\'s Docker',
                        host.docker_names(f'{host.RUN_USER}/{user.pw_uid}/docker.sock', user)))
    conmons = _running(procs, ('conmon',))
    if conmons:
        out.append(('Podman/CRI-O', [_conmon_name(p) for p in conmons]))
    for daemon, label, argv in ((('incusd',), 'incus', ['incus', 'list', '--format', 'csv', '-c', 'ns']),
                                (('lxd',), 'LXD', ['lxc', 'list', '--format', 'csv', '-c', 'ns'])):
        if _running(procs, daemon):
            code, text, err = host.run(argv, timeout=20)
            out.append((label, _csv_running(text) if code == 0 else f'{argv[0]}: {err.strip() or f"exit {code}"}'))
    code, text, err = host.run(['lxc-ls', '--running', '-1'], timeout=20)
    if code == 0:
        out.append(('LXC', [x.strip() for x in text.splitlines() if x.strip()]))
    elif code is not None or 'not installed' not in err:
        out.append(('LXC', f'lxc-ls: {err.strip() or f"exit {code}"}'))
    if _running(procs, ('libvirtd', 'virtqemud')):
        code, text, err = host.run(['virsh', '-c', 'qemu:///system', 'list', '--name'], timeout=20)
        out.append(('libvirt', [x.strip() for x in text.splitlines() if x.strip()] if code == 0
                    else f'virsh: {err.strip() or f"exit {code}"}'))
    return out


# -- all of it --------------------------------------------------------------------------------------------------------

def _part(busy, fn):
    try:
        return fn()
    except CannotTell as e:
        busy.append(f'cannot tell: {one_line(e, 300)}')
    except OSError as e:
        busy.append(f'cannot tell: {one_line(e, 300)}')
    return None


def look(host, settings):
    """The reasons the machine is busy now ([]: idle)."""
    busy = []
    if not os.path.exists(host.p(f'{host.CGROUP}/cgroup.controllers')):
        busy.append(f'cannot tell: no cgroup v2 at {host.CGROUP} (agents and heavy jobs cannot be counted)')
    procs = _part(busy, lambda: processes(host))
    sliced = {}
    for uid, folder in _part(busy, lambda: managers(host)) or []:
        found = _part(busy, lambda: agent_scopes(folder))
        if found is None:
            continue
        agents, sliced[uid] = found
        if agents:
            busy.append(f'user {uid}: {len(agents)} T3 agent scope(s) running ({one_line(", ".join(agents[:3]))})')
        jobs = _part(busy, lambda: heavy_scopes(folder, settings['heavySlice']))
        tickets = _part(busy, lambda: live_tickets(host, uid, settings['heavySlice']))
        if jobs or tickets:
            busy.append(f'user {uid}: heavy jobs ({len(jobs or [])} running, {tickets or 0} live ticket(s))')
    if procs is not None:
        unscoped = _part(busy, lambda: t3_unscoped(host, procs, sliced))
        for uid, found in sorted((unscoped or {}).items()):
            shown = '; '.join(one_line(f'{p.pid} {p.comm}: {why}', 100) for p, why in found[:4])
            busy.append(f'T3 runs without agent scopes (user {uid}): {len(found)} process(es): {shown}')
        tools = package_tools(procs)
        if tools:
            busy.append('package manager at work: ' + ', '.join(f'{n} ({pid})' for pid, n in tools[:3]))
        for label, names in _part(busy, lambda: containers(host, settings, procs)) or []:
            if isinstance(names, str):
                busy.append(f'cannot tell: {label}: {one_line(names, 200)}')
                continue
            running = [n for n in names if not _settings.ignored_container(settings, n)]
            if running:
                busy.append(f'{label}: {len(running)} running ({one_line(", ".join(sorted(running)[:5]), 200)})')
    locks = _part(busy, lambda: inhibitors(host))
    if locks:
        busy.append('inhibitor lock: ' + '; '.join(f'{one_line(who, 60)} (uid {uid}): {one_line(why, 120)}'
                                                   for who, why, uid in locks[:3]))
    for path in settings['busyScripts']:
        verdict, reason = host.free_answer(path)
        if verdict != 'free':
            busy.append(f'busy script {path}: {verdict}' + (f' ({one_line(reason, 160)})' if reason else ''))
    return busy
