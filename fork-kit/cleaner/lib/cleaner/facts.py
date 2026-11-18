"""What the cleaner reads about the machine: the process list, systemd units, git, GitHub and Docker.

One rule for every reader here: no answer means keep. A reader that cannot give a full answer raises Unreadable,
and whoever asked keeps the thing it asked about. Nothing in this file changes anything, except remove_worktree and
remove_volume at the very end, which the engine calls after every check has passed again.
"""
import calendar, json, os, re, subprocess, time

UNIT_DIRS = ('/etc/systemd/system', '/run/systemd/system', '/run/systemd/transient', '/etc/systemd/user',
             '~/.config/systemd/user', '/run/user/{uid}/systemd/user', '/run/user/{uid}/systemd/transient')
GIT, GH, DOCKER = 'git', 'gh', 'docker'    # tests put stubs here
PR_LIMIT = 1000
PROCESS_SECONDS = 2      # the process list is read again when it is older than this
COMMAND_SECONDS = 120
DELETED = ' (deleted)'
PATH_IN_TEXT = re.compile(r'''/[^\s"';,=|&<>()]*''')
OID = re.compile(r'^[0-9a-f]{40}([0-9a-f]{24})?$')
LOCK_INODE = re.compile(r'^[0-9a-f]+:[0-9a-f]+:(\d+)$')
GIT_ENV_KEPT = ('GIT_CONFIG_GLOBAL', 'GIT_CONFIG_NOSYSTEM', 'GIT_EXEC_PATH')


class Unreadable(Exception):
    """A fact could not be read in full. The message names what; whoever asked keeps what it asked about."""


# --- processes ---------------------------------------------------------------------------------------------------
class Processes:
    """`busy`: every path a process uses, and every folder above each. `locks`: the inodes with a held file lock."""

    def __init__(self, busy=(), locks=()):
        self.busy, self.locks = set(busy), set(locks)


def ancestors(path):
    """The path and every folder above it."""
    out = []
    while True:
        out.append(path)
        parent = os.path.dirname(path)
        if parent == path:
            return out
        path = parent


def links(folder):
    """Where the links in a /proc folder point; none when the folder cannot be read (another user's process)."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        try:
            out.append(os.readlink(os.path.join(folder, name)))
        except OSError:
            pass
    return out


def _command_paths(args, cwd):
    """Every path a command line may name: whole arguments, the value after `=`, and paths inside longer text
    (`sh -c "cd /x; ./run"`). A relative argument is read from the working folder. Too many is fine: a path named
    here is only ever kept."""
    out = []
    for arg in args:
        values = [arg] + ([arg.split('=', 1)[1]] if '=' in arg else [])
        for value in values:
            if value.startswith('/'):
                out.append(value)
            elif cwd and value and not value.startswith('-') and '\n' not in value:
                out.append(os.path.join(cwd, value))
        for text in (arg, arg.replace(':', ' ')):
            out.extend(PATH_IN_TEXT.findall(text))
    return out


def read_processes(proc='/proc', me=None):
    """Processes for everything now running. The cleaner sees its own user's processes in full and of other users
    what the kernel shows it (usually the command line). Raises Unreadable when the list, the lock list or the
    socket list cannot be read, or the list does not even hold the cleaner itself."""
    me = os.getpid() if me is None else me
    try:
        pids = [name for name in os.listdir(proc) if name.isdigit()]
        with open(os.path.join(proc, 'locks')) as f:
            lock_lines = f.read().splitlines()
        with open(os.path.join(proc, 'net', 'unix')) as f:
            socket_lines = f.read().splitlines()[1:]
    except OSError as ex:
        raise Unreadable(f'{ex.filename or proc}: {ex.strerror}') from None
    paths, seen = set(), set()
    for pid in pids:
        base = os.path.join(proc, pid)
        cwd = None
        for name in ('cwd', 'root', 'exe'):
            try:
                target = os.readlink(os.path.join(base, name))
            except OSError:
                continue
            paths.add(target)
            if name == 'cwd':
                cwd = target
                seen.add(int(pid))
        paths.update(links(os.path.join(base, 'fd')))
        try:
            with open(os.path.join(base, 'cmdline'), 'rb') as f:
                args = [a.decode('utf-8', 'surrogateescape') for a in f.read().split(b'\0') if a]
            paths.update(_command_paths(args, cwd))
        except OSError:
            pass
        try:
            with open(os.path.join(base, 'maps'), errors='surrogateescape') as f:
                for line in f:
                    fields = line.split(None, 5)
                    if len(fields) == 6:
                        paths.add(fields[5].rstrip('\n'))
        except OSError:
            pass
    if me not in seen:
        raise Unreadable(f'{proc} does not show this process ({me})')
    for line in socket_lines:
        fields = line.split(None, 7)
        if len(fields) == 8:
            paths.add(fields[7])
    busy = set()
    for path in paths:
        if not path.startswith('/'):
            continue
        if path.endswith(DELETED):
            path = path[:-len(DELETED)]
        path = os.path.normpath(path)
        while path not in busy:
            busy.add(path)
            path = os.path.dirname(path)
    locks = set()
    for line in lock_lines:
        for field in line.split():
            found = LOCK_INODE.match(field)
            if found:
                locks.add(int(found.group(1)))
    return Processes(busy, locks)


# --- systemd units ------------------------------------------------------------------------------------------------
def read_units(dirs=None, home=None):
    """Every path a systemd unit file names, and every folder above each. A folder that is not there is fine; a
    unit file that cannot be read raises Unreadable."""
    if dirs is None:
        home = home or os.path.expanduser('~')
        dirs = [d.replace('~', home, 1).format(uid=os.getuid()) for d in UNIT_DIRS]
    named = set()
    for top in dirs:
        stack = [top]
        while stack:
            folder = stack.pop()
            try:
                with os.scandir(folder) as it:
                    entries = list(it)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as ex:
                raise Unreadable(f'{folder}: {ex.strerror}') from None
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                        continue
                    if not entry.is_file():          # a link to a unit elsewhere is read; a masked or dead one is not
                        continue
                    with open(entry.path, errors='surrogateescape') as f:
                        text = f.read(1 << 20)
                except FileNotFoundError:
                    continue
                except OSError as ex:
                    raise Unreadable(f'{entry.path}: {ex.strerror}') from None
                for path in PATH_IN_TEXT.findall(text.replace(':', ' ')):
                    path = os.path.normpath(path)
                    while path not in named:
                        named.add(path)
                        path = os.path.dirname(path)
    return named


# --- commands -----------------------------------------------------------------------------------------------------
def _run(argv, what, cwd=None, env=None, stdin=None):
    try:
        return subprocess.run(argv, cwd=cwd, env=env, input=stdin, capture_output=True, text=True,
                              errors='surrogateescape', timeout=COMMAND_SECONDS,
                              stdin=subprocess.DEVNULL if stdin is None else None)
    except FileNotFoundError:
        raise Unreadable(f'{what} is not installed or its folder is gone') from None
    except subprocess.TimeoutExpired:
        raise Unreadable(f'{what} gave no answer in {COMMAND_SECONDS} s') from None
    except OSError as ex:
        raise Unreadable(f'{what}: {ex.strerror}') from None


def _first_line(text):
    return (text.strip().splitlines() or ['no message'])[0][:200]


# --- git ----------------------------------------------------------------------------------------------------------
def _git(cwd, args, bare, stdin):
    """Git never looks for a repository above `cwd`, takes no optional lock (so reading changes nothing), runs no
    file-system monitor and asks nothing."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') or k in GIT_ENV_KEPT}
    env.update(GIT_CEILING_DIRECTORIES=os.path.dirname(cwd), GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    argv = [GIT, '--no-optional-locks', '--no-pager', '-c', 'core.fsmonitor=false', *(['--git-dir=.'] if bare else []), *args]
    return _run(argv, 'git', cwd, env, stdin)


def git(cwd, *args, bare=False, stdin=None):
    """The output of a git command that must succeed; Unreadable when it does not."""
    done = _git(cwd, args, bare, stdin)
    if done.returncode != 0:
        raise Unreadable(f'{args[0]}: exit {done.returncode}: {_first_line(done.stderr)}')
    return done.stdout


def git_code(cwd, *args, bare=False):
    """The exit code of a git command that answers yes (0) or no (1); Unreadable for anything else."""
    done = _git(cwd, args, bare, None)
    if done.returncode not in (0, 1):
        raise Unreadable(f'{args[0]}: exit {done.returncode}: {_first_line(done.stderr)}')
    return done.returncode


def worktree_list(cwd, bare=False):
    """`git worktree list --porcelain` as a list of dicts; the first is the main worktree. A flag without a value
    (bare, detached, locked) holds ''."""
    entries = []
    for block in git(cwd, 'worktree', 'list', '--porcelain', bare=bare).split('\n\n'):
        entry = {}
        for line in block.splitlines():
            key, _, value = line.partition(' ')
            entry[key] = value
        if 'worktree' in entry:
            entries.append(entry)
    if not entries:
        raise Unreadable('worktree list: no worktree named')
    return entries


def origin_url(cwd):
    """remote.origin.url as the repository's own config has it (no insteadOf rewrite); '' when it has none."""
    url = ''
    for entry in git(cwd, 'config', '--local', '--null', '--list').split('\0'):
        key, _, value = entry.partition('\n')
        if key == 'remote.origin.url':
            url = value
    return url


def present(cwd, oids):
    """Those of `oids` that this repository has as objects."""
    oids = sorted(set(oids))
    if not oids:
        return set()
    out = git(cwd, 'cat-file', '--batch-check', stdin='\n'.join(oids) + '\n').splitlines()
    if len(out) != len(oids):
        raise Unreadable('cat-file: did not answer for every commit')
    return {oid for oid, line in zip(oids, out) if not line.endswith(' missing')}


def reflog(cwd, *what):
    """Every commit a reflog names: of HEAD (this worktree's own), or of e.g. --branches."""
    return sorted({line for line in git(cwd, 'log', '-g', '--format=%H', *what).split() if OID.match(line)})


def remote_heads(cwd):
    """The branch heads origin has right now: a live answer, not the clone's remote-tracking refs, which go stale."""
    heads = set()
    for line in git(cwd, 'ls-remote', '--heads', 'origin').splitlines():
        oid = line.split('\t')[0]
        if not OID.match(oid):
            raise Unreadable(f'ls-remote: a line it cannot use: {line[:80]!r}')
        heads.add(oid)
    return heads


def lonely(cwd, positive, negative):
    """The first commit reachable from `positive` (revisions or options) and from nothing in `negative`; '' when
    there is none. Everything goes through standard input, so the lists may be long."""
    lines = [*positive, '--not', *negative]
    return git(cwd, 'rev-list', '--max-count=1', '--stdin', stdin='\n'.join(lines) + '\n').strip()


def remove_worktree(common_dir, path):
    """`git worktree remove`, never with --force: git itself refuses a worktree with changed or untracked files."""
    git(common_dir, 'worktree', 'remove', path, bare=True)


# --- GitHub -------------------------------------------------------------------------------------------------------
def valid_pr(row, state):
    return (isinstance(row, dict) and type(row.get('number')) is int and isinstance(row.get('headRefName'), str)
            and bool(row['headRefName']) and isinstance(row.get('headRefOid'), str) and bool(OID.match(row['headRefOid']))
            and (state != 'merged' or isinstance(row.get('mergedAt'), str)))


def github_prs(repo):
    """{'merged': rows, 'open': rows} for one GitHub repository, each row with number, headRefName, headRefOid and
    mergedAt. Raises Unreadable when gh fails, prints something else, or a list may be cut off: a missing open pull
    request would look like "no open pull request"."""
    out = {}
    for state in ('merged', 'open'):
        done = _run([GH, 'pr', 'list', '-R', repo, '--state', state, '--limit', str(PR_LIMIT),
                     '--json', 'number,headRefName,headRefOid,mergedAt'], 'GitHub (gh)')
        if done.returncode != 0:  # gh
            raise Unreadable(f'GitHub (gh pr list: {_first_line(done.stderr)})')
        try:
            rows = json.loads(done.stdout)
        except ValueError:
            raise Unreadable('GitHub (gh pr list did not print JSON)') from None
        if not isinstance(rows, list):
            raise Unreadable('GitHub (gh pr list did not print a list)')
        for row in rows:
            if not valid_pr(row, state):
                raise Unreadable(f'GitHub (gh pr list printed a row it cannot use: {str(row)[:120]})')
        # A cut-off merged list only hides old merged pull requests, and "no merged pull request" keeps.
        if state == 'open' and len(rows) >= PR_LIMIT:
            raise Unreadable(f'GitHub ({repo} has {PR_LIMIT} or more open pull requests: the list may be cut off)')
        out[state] = rows
    return out


def merged_seconds(row):
    """When a pull request merged, in seconds since 1970; None when the row does not say."""
    try:
        return calendar.timegm(time.strptime(row['mergedAt'], '%Y-%m-%dT%H:%M:%SZ'))
    except (KeyError, TypeError, ValueError):
        return None  # no merge time


# --- Docker -------------------------------------------------------------------------------------------------------
class Volume:
    def __init__(self, name, created, mountpoint):
        self.name, self.created, self.mountpoint = name, created, mountpoint


def _docker(*args):
    done = _run([DOCKER, *args], 'Docker')
    if done.returncode != 0:  # docker
        raise Unreadable(f'Docker (docker {" ".join(args[:2])}: {_first_line(done.stderr)})')
    return done.stdout


def _docker_time(text):
    """Seconds since 1970 for Docker's CreatedAt (2026-01-02T03:04:05Z or with +01:00)."""
    found = re.match(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$', text)
    if not found:
        raise Unreadable(f'Docker (a volume has a creation time it cannot read: {text[:40]!r})')
    seconds = calendar.timegm(time.strptime(found.group(1), '%Y-%m-%dT%H:%M:%S'))
    zone = found.group(2)
    if zone != 'Z':
        offset = int(zone[1:3]) * 3600 + int(zone[4:6]) * 60
        seconds -= offset if zone[0] == '+' else -offset
    return seconds


def docker_dangling():
    """The volumes no container uses, running or stopped (Docker's own "dangling" list), as Volume objects."""
    names = _docker('volume', 'ls', '-q', '--filter', 'dangling=true').split()
    if not names:
        return []
    volumes = []
    for line in _docker('volume', 'inspect', '-f', '{{.Name}}\t{{.CreatedAt}}\t{{.Mountpoint}}', *names).splitlines():
        fields = line.split('\t')
        if len(fields) != 3 or fields[0] not in names:
            raise Unreadable(f'Docker (docker volume inspect printed a line it cannot use: {line[:80]!r})')
        volumes.append(Volume(fields[0], _docker_time(fields[1]), os.path.normpath(fields[2]) if fields[2].startswith('/') else None))
    if len(volumes) != len(names):
        raise Unreadable('Docker (docker volume inspect did not answer for every volume)')
    return volumes


def remove_volume(name):
    """`docker volume rm`, never with --force: Docker itself refuses a volume a container uses."""
    _docker('volume', 'rm', name)


# --- one view of it all -------------------------------------------------------------------------------------------
class Facts:
    """What one run knows. The process list is read again when it is older than PROCESS_SECONDS, so the last check
    before each delete sees the machine as it is then; units and pull request lists are read once per run. A
    reader that failed keeps failing for as long as its answer would have been kept."""

    def __init__(self, cfg, processes=None, units=None, prs=None, dangling=None, now=None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self._read_processes = processes or read_processes
        self._read_units = units or (lambda: read_units(home=cfg.home))
        self._read_prs = prs or github_prs
        self.dangling = dangling or docker_dangling
        self._processes = self._units = None
        self._prs = {}

    @staticmethod
    def _answer(read):
        try:
            return read()
        except Unreadable as ex:
            return ex

    @staticmethod
    def _give(answer):
        if isinstance(answer, Unreadable):
            raise answer
        return answer

    def processes(self):
        if self._processes is None or time.monotonic() - self._processes[0] >= PROCESS_SECONDS:
            self._processes = (time.monotonic(), self._answer(self._read_processes))
        return self._give(self._processes[1])

    def units(self):
        if self._units is None:
            self._units = (self._answer(self._read_units),)
        return self._give(self._units[0])

    def prs(self, repo):
        if repo not in self._prs:
            self._prs[repo] = self._answer(lambda: self._read_prs(repo))
        return self._give(self._prs[repo])
