"""What the cleaner reads about the machine: the process list, systemd units, git, GitHub and Docker.

One rule for every reader here: no answer means keep. A reader that cannot give a full answer raises Unreadable,
and whoever asked keeps the thing it asked about. Nothing in this file changes anything, except remove_worktree and
remove_volume at the very end, which the engine calls after every check has passed again.
"""
import calendar, ipaddress, json, os, re, socket, stat, subprocess, time

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
# Before and after a path named inside longer text: what may stand next to a whole path, not inside a longer name.
EDGE = r'\s"\'=:;,&|()<>`'


class Processes:
    """`busy`: every path a process uses, and every folder above each. `locks`: the inodes with a held file lock.
    `texts`: the command lines as text, so a path with a space in it (`sh -c 'cd "/tmp/my dir"'`) is seen too."""

    def __init__(self, busy=(), locks=(), texts=()):
        self.busy, self.locks, self.texts = set(busy), set(locks), list(texts)

    def uses(self, path):
        """True when a process uses `path` or something in it, or a command line names it."""
        if path in self.busy:
            return True
        named = re.compile(f'(?:^|(?<=[{EDGE}]))' + re.escape(path) + f'(?=$|[/{EDGE}])')
        return any(named.search(text.replace('\\ ', ' ')) for text in self.texts)


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
    paths, seen, texts = set(), set(), []
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
            texts.append(' '.join(args))
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
    return Processes(busy, locks, texts)


# --- mounts -------------------------------------------------------------------------------------------------------
def _unescape(field):
    """A mountinfo path: space, tab, newline and backslash are written as \\040, \\011, \\012 and \\134."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)


class Mounts:
    """`points`: every mount point of the cleaner's own mount namespace. `lines`: (mount point, device, root) of each
    of them, where root is the folder of its filesystem it shows. `sources`: (device, root) of every mount, in any
    mount namespace the cleaner may read, that shows one folder of a filesystem rather than all of it: the source of
    a bind mount (a container volume, a sandbox). A device is "major:minor" as mountinfo writes it."""

    def __init__(self, points=('/',), lines=(), sources=()):
        self.points, self.lines, self.sources = set(points), list(lines), set(sources)

    def on_filesystem(self, path, dev):
        """Where `path` is on its own filesystem (the root of the deepest mount holding it, plus the rest), or None
        when no mount line of that device holds it (btrfs and overlay give other device numbers than mountinfo)."""
        best = None
        for point, device, root in self.lines:
            if device == dev and (point == '/' or path == point or path.startswith(point + '/')):
                if best is None or len(point) > len(best[0]):
                    best = (point, root)
        if best is None:
            return None
        rest = os.path.relpath(path, best[0])
        return os.path.normpath(os.path.join(best[1], rest)) if rest != '.' else best[1]

    def bound(self, path, st_dev):
        """The first bind mount source that is `path` or inside it: it is shown somewhere else too, maybe in a
        container. None when there is none, or when where `path` lives on its filesystem is not known."""
        dev = f'{os.major(st_dev)}:{os.minor(st_dev)}'
        where = self.on_filesystem(path, dev)
        if where is None:
            return None
        inside = sorted(root for device, root in self.sources
                        if device == dev and (root == where or root.startswith(where.rstrip('/') + '/')))
        return inside[0] if inside else None


def _mount_lines(path):
    """[(mount point, device, root)] of one mountinfo file. Raises Unreadable when it cannot be read or a line is
    strange."""
    try:
        with open(path, errors='surrogateescape') as f:
            lines = f.read().splitlines()
    except OSError as ex:
        raise Unreadable(f'{ex.filename or path}: {ex.strerror}') from None
    out = []
    for line in lines:
        fields = line.split(' ')
        if len(fields) < 10 or '-' not in fields[6:] or not re.match(r'^\d+:\d+$', fields[2]):
            raise Unreadable(f'{path}: a line it cannot use: {line[:80]!r}')
        out.append((os.path.normpath(_unescape(fields[4])), fields[2], os.path.normpath(_unescape(fields[3]))))
    return out


def read_mounts(path='/proc/self/mountinfo', proc='/proc'):
    """Every mount point, bind mounts too: a bind mount of a folder on the same filesystem has the same device
    number, so only this list shows it. Also the bind mount sources of every other mount namespace the cleaner can
    read (its own user's processes): a folder a container or a sandbox shows. Raises Unreadable when its own list
    cannot be read or does not even hold /; another process's list that cannot be read is not seen."""
    lines = _mount_lines(path)
    points = {point for point, _, _ in lines}
    if '/' not in points:
        raise Unreadable(f'{path} does not name /')
    sources = {(device, root) for _, device, root in lines if root != '/'}
    seen = set()
    try:
        pids = sorted((name for name in os.listdir(proc) if name.isdigit()), key=int) if proc else []  # the same answer on any file system
    except OSError:
        pids = []
    for pid in pids:
        try:
            space = os.readlink(os.path.join(proc, pid, 'ns', 'mnt'))
        except OSError:
            continue                    # another user's process, or gone: its mounts are not seen
        if space in seen:
            continue
        seen.add(space)
        try:
            other = _mount_lines(os.path.join(proc, pid, 'mountinfo'))
        except Unreadable:
            continue
        sources.update((device, root) for _, device, root in other if root != '/')
    return Mounts(points, lines, sources)


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
    file-system monitor and asks nothing. Only `ls-remote` may reach another machine: every other command runs with
    every transport off, so a partial clone never fetches a missing object (git 2.44 and later also read
    GIT_NO_LAZY_FETCH); a missing object stays missing, and whoever asked keeps."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_') or k in GIT_ENV_KEPT}
    env.update(GIT_CEILING_DIRECTORIES=os.path.dirname(cwd), GIT_TERMINAL_PROMPT='0', GIT_OPTIONAL_LOCKS='0', LC_ALL='C')
    offline = args[0] != 'ls-remote'
    if offline:
        env['GIT_NO_LAZY_FETCH'] = '1'
    argv = [GIT, '--no-optional-locks', '--no-pager', '-c', 'core.fsmonitor=false',
            *(['-c', 'protocol.allow=never'] if offline else []), *(['--git-dir=.'] if bare else []), *args]
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


def orphaned_worktree(folder):
    """The git folder a worktree's `.git` file names, when that folder is gone (its main repository was removed or
    moved): git cannot answer anything about the worktree, and never will. None for anything else: no `.git` file,
    one that cannot be read (that stays "could not read"), or a git folder that is there."""
    dot = os.path.join(folder, '.git')
    try:
        fd = os.open(dot, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return None
        text = os.read(fd, 4096).decode('utf-8', 'surrogateescape')
    except OSError:
        return None
    finally:
        os.close(fd)
    lines = text.splitlines()
    if len(lines) != 1 or not lines[0].startswith('gitdir: ') or not lines[0][len('gitdir: '):].strip():
        return None
    gitdir = os.path.normpath(os.path.join(folder, lines[0][len('gitdir: '):].strip()))
    try:
        os.lstat(gitdir)
    except (FileNotFoundError, NotADirectoryError):
        return gitdir
    except OSError:
        return None             # cannot tell: no answer means keep, as "could not read"
    return None


def origin_url(cwd):
    """remote.origin.url as the repository's own config has it (no insteadOf rewrite); '' when it has none."""
    url = ''
    for entry in git(cwd, 'config', '--local', '--null', '--list').split('\0'):
        key, _, value = entry.partition('\n')
        if key == 'remote.origin.url':
            url = value
    return url


def present(cwd, oids, bare=False):
    """Those of `oids` that this repository has as objects."""
    oids = sorted(set(oids))
    if not oids:
        return set()
    out = git(cwd, 'cat-file', '--batch-check', stdin='\n'.join(oids) + '\n', bare=bare).splitlines()
    if len(out) != len(oids):
        raise Unreadable('cat-file: did not answer for every commit')
    return {oid for oid, line in zip(oids, out) if not line.endswith(' missing')}


def reflog(cwd, *what):
    """Every commit a reflog names: of HEAD (this worktree's own), or of e.g. --branches."""
    return sorted({line for line in git(cwd, 'log', '-g', '--format=%H', *what).split() if OID.match(line)})


def remote_refs(cwd, remote, *only, bare=False):
    """{ref: oid} of what `remote` has right now (`git ls-remote`, `only` e.g. --heads --tags): a live answer, not
    the clone's remote-tracking refs, which go stale. A peeled tag is its own entry (refs/tags/v1^{})."""
    refs = {}
    for line in git(cwd, 'ls-remote', *only, remote, bare=bare).splitlines():
        oid, tab, ref = line.partition('\t')
        if not OID.match(oid) or not tab or not ref:
            raise Unreadable(f'ls-remote: a line it cannot use: {line[:80]!r}')
        refs[ref] = oid
    return refs


def default_branch(cwd, remote='origin'):
    """The branch `remote`'s HEAD names right now (`git ls-remote --symref`): on GitHub, the default branch."""
    for line in git(cwd, 'ls-remote', '--symref', remote, 'HEAD').splitlines():
        if line.startswith('ref: refs/heads/') and line.endswith('\tHEAD'):
            return line[len('ref: refs/heads/'):-len('\tHEAD')]
    raise Unreadable(f'ls-remote: {remote} named no default branch')


def local_tags(cwd, bare=False):
    """{refs/tags/name: oid} of this repository's own tags (an annotated tag: the tag object)."""
    tags = {}
    for line in git(cwd, 'for-each-ref', '--format=%(objectname) %(refname)', 'refs/tags', bare=bare).splitlines():
        oid, _, ref = line.partition(' ')
        if not OID.match(oid) or not ref.startswith('refs/tags/'):
            raise Unreadable(f'for-each-ref: a line it cannot use: {line[:80]!r}')
        tags[ref] = oid
    return tags


def remotes(cwd, bare=False):
    """The names of the remotes in the repository's own config that have a URL."""
    out = set()
    for entry in git(cwd, 'config', '--local', '--null', '--list', bare=bare).split('\0'):
        key, _, value = entry.partition('\n')
        if key.startswith('remote.') and key.endswith('.url'):
            out.add(key[len('remote.'):-len('.url')])
    return sorted(out)


def remote_urls(cwd, remote, bare=False):
    """Every URL of `remote` as git uses it: each url and pushurl after the user's url.<x>.insteadOf and
    pushInsteadOf rules (`git remote get-url`). `ls-remote` asks the first; a push may go to any."""
    urls = []
    for push in ([], ['--push']):
        urls += [u for u in git(cwd, 'remote', 'get-url', *push, '--all', remote, bare=bare).splitlines() if u]
    if not urls:
        raise Unreadable(f'remote get-url: {remote} has no URL')
    return urls


# Names and addresses that are this machine. A remote there may be what is being deleted, or next to go.
LOCAL_HOSTS = ('localhost', 'localhost.localdomain', 'ip6-localhost', 'ip6-loopback')
SCHEME = re.compile(r'^([A-Za-z][A-Za-z0-9+.-]*)://(?:[^@/?#]*@)?(\[[^\]/]*\]|[^:/?#]*)')
SCP = re.compile(r'^(?:[^@/:]*@)?(\[[^\]/]*\]|[^:/\[]+):')


def this_machine(host):
    host = host.strip('[]').lower().rstrip('.')
    if not host or host in LOCAL_HOSTS or host.endswith('.localhost'):
        return True
    try:
        address = ipaddress.ip_address(host.split('%')[0])
        return address.is_loopback or address.is_unspecified
    except ValueError:
        pass
    name = socket.gethostname().lower()
    return host in (name, name.split('.')[0])


def elsewhere(url):
    """True for a remote on another machine (https://, ssh://, git@host:path). A folder on this machine, a file://
    URL, a remote helper (transport::address) or a host that is this machine (localhost, 127.0.0.1, ::1, its own
    name) may be what is being deleted, so what it has does not count as pushed."""
    if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*::', url):
        return False
    found = SCHEME.match(url)
    if found:
        return found.group(1).lower() != 'file' and not this_machine(found.group(2))
    found = SCP.match(url)
    return bool(found) and not this_machine(found.group(1))


def lonely(cwd, positive, negative, bare=False):
    """The first commit reachable from `positive` (revisions or options) and from nothing in `negative`; '' when
    there is none. Everything goes through standard input, so the lists may be long."""
    lines = [*positive, '--not', *negative]
    return git(cwd, 'rev-list', '--max-count=1', '--stdin', stdin='\n'.join(lines) + '\n', bare=bare).strip()


class Untouched(Unreadable):
    """A delete that was refused before anything was removed."""


def remove_worktree(common_dir, path):
    """`git worktree remove`, never with --force: git itself refuses a worktree with changed or untracked files,
    and then removes nothing (Untouched)."""
    done = _git(common_dir, ('worktree', 'remove', path), True, None)
    if done.returncode != 0:
        # git checks everything before it removes a file and stops with exit 128 (fatal) when a check fails; a
        # failure while removing says "failed to delete", and a killed git has no exit code of its own.
        refused = done.returncode == 128 and 'failed to delete' not in done.stderr
        raise (Untouched if refused else Unreadable)(f'worktree: exit {done.returncode}: {_first_line(done.stderr)}')


# --- GitHub -------------------------------------------------------------------------------------------------------
PR_FIELDS = 'number,headRefName,headRefOid,mergedAt,baseRefName,isCrossRepository'


def valid_pr(row, state):
    return (isinstance(row, dict) and type(row.get('number')) is int and isinstance(row.get('headRefName'), str)
            and bool(row['headRefName']) and isinstance(row.get('headRefOid'), str) and bool(OID.match(row['headRefOid']))
            and isinstance(row.get('baseRefName'), str) and type(row.get('isCrossRepository')) is bool
            and (state != 'merged' or isinstance(row.get('mergedAt'), str)))


def github_prs(repo):
    """{'merged': rows, 'open': rows} for one GitHub repository, each row with the PR_FIELDS (isCrossRepository:
    the head branch is in a fork, not in this repository). Raises Unreadable when gh fails, prints something else, or a list may be cut off: a missing open pull
    request would look like "no open pull request"."""
    out = {}
    for state in ('merged', 'open'):
        done = _run([GH, 'pr', 'list', '-R', repo, '--state', state, '--limit', str(PR_LIMIT),
                     '--json', PR_FIELDS], 'GitHub (gh)')
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


# Docker's sizes are decimal (units.HumanSize): 1 kB is 1000 bytes.
DOCKER_SIZE = re.compile(r'^(\d+(?:\.\d+)?)(B|kB|KB|MB|GB|TB|PB)$')
DOCKER_UNITS = {'B': 1, 'kB': 1000, 'KB': 1000, 'MB': 1000 ** 2, 'GB': 1000 ** 3, 'TB': 1000 ** 4, 'PB': 1000 ** 5}


def docker_volume_sizes():
    """(sizes, folder): {name: bytes} of every volume, named ones too, as Docker itself measures them (`docker
    system df -v`: its folders are root's, so the user cannot walk them), and Docker's own folder, where they are.
    Raises Unreadable when Docker gives no full answer, a size it could not measure included."""
    sizes = {}
    for line in _docker('system', 'df', '-v', '--format', '{{range .Volumes}}{{.Name}}\t{{.Size}}\n{{end}}').splitlines():
        if not line:
            continue
        fields = line.split('\t')
        found = DOCKER_SIZE.match(fields[1]) if len(fields) == 2 else None
        if not found:
            raise Unreadable(f'Docker (docker system df printed a volume size it cannot use: {line[:80]!r})')
        sizes[fields[0]] = int(float(found.group(1)) * DOCKER_UNITS[found.group(2)])
    folder = _docker('info', '--format', '{{.DockerRootDir}}').strip()
    return sizes, folder


# --- one view of it all -------------------------------------------------------------------------------------------
class Facts:
    """What one run knows. While planning, the process list and the mounts are read again when they are older than
    PROCESS_SECONDS, and units and pull request lists are read once. `forget` drops every answer: the engine calls
    it before the last check of each delete, so that check reads the process list, the mounts, the units, the pull
    request lists and Docker's list anew. A reader that failed keeps failing until then."""

    def __init__(self, cfg, processes=None, units=None, prs=None, dangling=None, now=None, mounts=None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self._read_processes = processes or read_processes
        self._read_mounts = mounts or read_mounts
        self._read_units = units or (lambda: read_units(home=cfg.home))
        self._read_prs = prs or github_prs
        self.dangling = dangling or docker_dangling
        self.forget()

    def forget(self):
        self._processes = self._mounts = self._units = None
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

    def mounts(self):
        if self._mounts is None or time.monotonic() - self._mounts[0] >= PROCESS_SECONDS:
            self._mounts = (time.monotonic(), self._answer(self._read_mounts))
        return self._give(self._mounts[1])

    def units(self):
        if self._units is None:
            self._units = (self._answer(self._read_units),)
        return self._give(self._units[0])

    def prs(self, repo):
        if repo not in self._prs:
            self._prs[repo] = self._answer(lambda: self._read_prs(repo))
        return self._give(self._prs[repo])
