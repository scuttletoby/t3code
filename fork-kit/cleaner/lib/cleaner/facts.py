"""What the cleaner reads about the machine: the process list, systemd units, git, GitHub and Docker.

One rule for every reader here: no answer means keep. A reader that cannot give a full answer raises Unreadable,
and whoever asked keeps the thing it asked about. Nothing in this file changes anything, except remove_worktree,
remove_branch and remove_volume, which the engine calls after every check has passed again.
"""
import calendar, errno, fnmatch, ipaddress, json, os, re, socket, stat, subprocess, time

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
# A word of a command line or an environment: what a session id is made of. A path splits into its parts.
WORD = re.compile(r'[A-Za-z0-9_.-]+')
# Before and after a path named inside longer text: what may stand next to a whole path, not inside a longer name.
EDGE = r'\s"\'=:;,&|()<>`'


class Proc:
    """One process of the cleaner's own user. `paths`: what it works in or has open (its working folder, its root,
    every open descriptor). `words`: the words of its command line and its environment. `start`: when it started
    (seconds since the epoch; None when not known). `hidden`: a part of it could not be read although it is the
    user's own (a process that is not dumpable, such as gpg-agent: its open files and environment are root's to
    read). `name`: its short name (/proc/<pid>/comm). `parent`: the pid of its parent (from /proc/<pid>/stat, which
    a hidden process shows too); None when not known."""

    def __init__(self, pid, name='', paths=(), words=(), start=None, hidden=False, parent=None):
        self.pid, self.name, self.paths, self.words = pid, name, set(paths), set(words)
        self.start, self.hidden, self.parent = start, hidden, parent


class Processes:
    """`busy`: every path a process uses, and every folder above each. `locks`: the inodes with a held file lock.
    `texts`: the command lines as text, so a path with a space in it (`sh -c 'cd "/tmp/my dir"'`) is seen too.
    `words`: every word of every command line and of the environment of each process the user may read (a session
    id: `--session-id=<id>`, `CLAUDE_CODE_SESSION_ID=<id>`, or a path with it). `own`: the user's own processes,
    one Proc each (the agent-scratch rule asks which session folder each one works in)."""

    def __init__(self, busy=(), locks=(), texts=(), words=(), own=()):
        self.busy, self.locks, self.texts = set(busy), set(locks), list(texts)
        self.words = set(words)
        for text in self.texts:
            self.words.update(WORD.findall(text))
        self.own = list(own)
        for one in self.own:
            self.words.update(one.words)

    def names(self, word):
        """True when a process names `word` (a session id) as a whole word on its command line or in its
        environment."""
        return word in self.words

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


def _boot_time(proc):
    try:
        with open(os.path.join(proc, 'stat')) as f:
            for line in f:
                if line.startswith('btime '):
                    return float(line.split()[1])
    except (OSError, ValueError, IndexError):
        pass
    return None


def _stat(base, boot):
    """(start, parent) of a process from /proc/<pid>/stat: when it started (seconds since the epoch, field 22) and
    its parent's pid (field 4); each None when not known."""
    try:
        with open(os.path.join(base, 'stat'), 'rb') as f:
            fields = f.read().rsplit(b')', 1)[1].split()
    except (OSError, IndexError):
        return None, None
    try:
        parent = int(fields[1])
    except (ValueError, IndexError):
        parent = None
    try:
        start = None if boot is None else boot + int(fields[19]) / os.sysconf('SC_CLK_TCK')
    except (ValueError, IndexError):
        start = None
    return start, parent


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
    paths, seen, texts, words, own = set(), set(), [], set(), []
    uid, boot = os.getuid(), _boot_time(proc)
    for pid in pids:
        base = os.path.join(proc, pid)
        try:
            mine = os.lstat(base).st_uid == uid
        except OSError:
            continue                    # it ended
        one = Proc(int(pid))
        cwd = None
        for name in ('cwd', 'root', 'exe'):
            try:
                target = os.readlink(os.path.join(base, name))
            except OSError as ex:
                one.hidden = one.hidden or (mine and name != 'exe' and ex.errno in (errno.EACCES, errno.EPERM))
                continue
            paths.add(target)           # its folder, root or program
            if name != 'exe':
                one.paths.add(target)
            if name == 'cwd':
                cwd = target
                seen.add(int(pid))
        try:
            fds = os.listdir(os.path.join(base, 'fd'))
        except OSError as ex:
            fds = []
            one.hidden = one.hidden or (mine and ex.errno in (errno.EACCES, errno.EPERM))
        for name in fds:
            try:
                target = os.readlink(os.path.join(base, 'fd', name))
            except OSError:
                continue
            paths.add(target)           # an open file
            one.paths.add(target)
        try:
            with open(os.path.join(base, 'cmdline'), 'rb') as f:
                args = [a.decode('utf-8', 'surrogateescape') for a in f.read().split(b'\0') if a]
            paths.update(_command_paths(args, cwd))
            texts.append(' '.join(args))
            one.words.update(WORD.findall(' '.join(args)))
        except OSError:
            pass
        try:
            # Only the user's own processes show it: what an agent's tools inherit (its session id) is here.
            with open(os.path.join(base, 'environ'), 'rb') as f:
                found = WORD.findall(f.read().decode('utf-8', 'surrogateescape'))
            words.update(found)
            one.words.update(found)
        except OSError as ex:
            one.hidden = one.hidden or (mine and ex.errno in (errno.EACCES, errno.EPERM))
        if mine:
            try:
                with open(os.path.join(base, 'comm'), errors='replace') as f:
                    one.name = f.read().strip()
            except OSError:
                pass
            one.start, one.parent = _stat(base, boot)
            own.append(one)
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
    return Processes(busy, locks, texts, words, own)


# --- mounts -------------------------------------------------------------------------------------------------------
def _unescape(field):
    """A mountinfo path: space, tab, newline and backslash are written as \\040, \\011, \\012 and \\134."""
    return re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), field)


class Mounts:
    """`points`: every mount point of the cleaner's own mount namespace. `lines`: (mount point, device, root) of each
    of them, where root is the folder of its filesystem it shows. `sources`: (device, root) of every mount, in any
    mount namespace the cleaner may read, that shows one folder of a filesystem rather than all of it: the source of
    a bind mount (a container volume, a sandbox). `options`: {mount point: its per-mount options} (`noatime`,
    `relatime`, ...). `fstab`: the mount points /etc/fstab names, mounted now or not; None when it cannot be read.
    A device is "major:minor" as mountinfo writes it."""

    def __init__(self, points=('/',), lines=(), sources=(), options=None, fstab=()):
        self.points, self.lines, self.sources = set(points), list(lines), set(sources)
        self.fstab = None if fstab is None else {os.path.normpath(p) for p in fstab}
        self.options = {point: frozenset(opts) for point, opts in (options or {}).items()}

    def options_of(self, path):
        """The per-mount options of the deepest mount point that holds `path` (a real path, no links in it), or
        None when no known mount point holds it."""
        best = None
        for point in self.options:
            if point == '/' or path == point or path.startswith(point + '/'):
                if best is None or len(point) > len(best):
                    best = point
        return None if best is None else self.options[best]

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


def _mount_lines(path, options=None):
    """[(mount point, device, root)] of one mountinfo file; `options`, a dict, gets {mount point: its per-mount
    options}. Raises Unreadable when it cannot be read or a line is strange."""
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
        point = os.path.normpath(_unescape(fields[4]))
        out.append((point, fields[2], os.path.normpath(_unescape(fields[3]))))
        if options is not None:
            options[point] = fields[5].split(',')      # a later mount on the same point hides the earlier one
    return out


def read_fstab(path='/etc/fstab'):
    """The mount points /etc/fstab names (swap and `none` left out); none when there is no such file, None when it
    cannot be read."""
    try:
        with open(path, errors='surrogateescape') as f:
            lines = f.read().splitlines()
    except FileNotFoundError:
        return set()
    except OSError:
        return None
    points = set()
    for line in lines:
        fields = line.split()
        if len(fields) >= 2 and not fields[0].startswith('#') and fields[1].startswith('/'):
            points.add(os.path.normpath(_unescape(fields[1])))
    return points


def read_mounts(path='/proc/self/mountinfo', proc='/proc', fstab='/etc/fstab'):
    """Every mount point, bind mounts too: a bind mount of a folder on the same filesystem has the same device
    number, so only this list shows it. Also the bind mount sources of every other mount namespace the cleaner can
    read (its own user's processes): a folder a container or a sandbox shows. Raises Unreadable when its own list
    cannot be read or does not even hold /; another process's list that cannot be read is not seen."""
    options = {}
    lines = _mount_lines(path, options)
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
    return Mounts(points, lines, sources, options, read_fstab(fstab))


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


def _gitdir_line(folder):
    """What the `gitdir: ` line of a worktree's `.git` file names, as written (maybe a relative path); None when there
    is no such file, it cannot be read, or it is not one `gitdir: ` line."""
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
    return lines[0][len('gitdir: '):].strip()


def orphaned_worktree(folder):
    """The git folder a worktree's `.git` file names, when no folder is found there: its main repository was
    removed, moved or renamed, or is on a drive that is not mounted now (orphan_doubt tells these apart). None for
    anything else: no `.git` file, one that cannot be read (that stays "could not read"), or a git folder that is
    there."""
    named = _gitdir_line(folder)
    if named is None:
        return None
    gitdir = os.path.normpath(os.path.join(folder, named))
    try:
        os.lstat(gitdir)
    except (FileNotFoundError, NotADirectoryError):
        return gitdir
    except OSError:
        return None             # cannot tell: no answer means keep, as "could not read"
    return None


# Where drives that come and go are mounted: WSL's /mnt/c, udisks' /media/<user>/<label> and /run/media/<user>/<label>.
REMOVABLE = ('/mnt', '/media', '/run/media')


def _admin_entry(places, target):
    """The first worktree admin entry (`<git folder>/worktrees/<name>`) of a repository in `places` (folders that
    may be a clone, or a bare repository) whose `gitdir` file names `target`, the worktree's own `.git` file. None
    when there is none. Raises Unreadable when a folder that is there cannot be read."""
    wanted = {os.path.normpath(target), os.path.realpath(target)}
    for place in places:
        for git_dir in (os.path.join(place, '.git'), place):
            admin = os.path.join(git_dir, 'worktrees')
            try:
                names = sorted(os.listdir(admin))
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as ex:
                raise Unreadable(f'{ex.filename or admin}: {ex.strerror}') from None
            for name in names:
                try:
                    with open(os.path.join(admin, name, 'gitdir'), errors='surrogateescape') as f:
                        named = f.read().strip()
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as ex:
                    raise Unreadable(f'{ex.filename}: {ex.strerror}') from None
                if os.path.normpath(os.path.join(admin, name, named)) in wanted:
                    return os.path.join(admin, name)
    return None


def orphan_doubt(folder, gone, mounts, roots=()):
    """Why the worktree `folder`, whose git folder `gone` is not there (orphaned_worktree), may still have one
    somewhere: its HEAD, index and reflog, maybe a commit only that folder names. None only when it is provably
    orphaned (#519):
    - its `.git` file names the git folder by a full path (a relative one breaks when the worktree is moved);
    - the deepest part of that path that is there is a real folder (no link, broken or not, on the way), on the
      worktree's own filesystem, and is no mount point, so the path is not on a drive that is not mounted now (one
      /etc/fstab names, or one under REMOVABLE with nothing mounted there);
    - no repository in that folder, in the folders right in it or right in `roots` (the configured repository
      folders) has a worktree entry that names this worktree: the repository was not renamed or moved there.
    Raises Unreadable when something it must read cannot be read."""
    named = _gitdir_line(folder)
    if named is None:
        return 'its .git file cannot be read again'
    if not os.path.isabs(named):
        return f'its .git file names its git folder by a relative path ({named}): the worktree may have been moved'
    there = gone
    while True:
        there = os.path.dirname(there)
        try:
            st = os.lstat(there)
            break
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as ex:
            raise Unreadable(f'{ex.filename or there}: {ex.strerror}') from None
    if stat.S_ISLNK(st.st_mode):
        return f'{there} is a symbolic link to what is not there now (a broken link)'
    if not stat.S_ISDIR(st.st_mode):
        return f'{there} is not a folder'
    if os.path.realpath(there) != there:
        return f'{there} is reached through a link'
    try:
        own = os.lstat(folder)
    except OSError as ex:
        raise Unreadable(f'{ex.filename or folder}: {ex.strerror}') from None
    if st.st_dev != own.st_dev:
        return f'{there} is on another filesystem than the worktree'
    if there in mounts.points:
        return f'{there} is a mount point: what is mounted there may not be what was'
    if mounts.fstab is None:
        return 'could not read /etc/fstab: a drive it names may not be mounted now'
    inside = lambda path, top: path == top or path.startswith(top.rstrip('/') + '/')
    for point in sorted(mounts.fstab):
        if point not in mounts.points and inside(gone, point):
            return f'{point} is in /etc/fstab and is not mounted now'
    holder = max((p for p in mounts.points if inside(there, p)), key=len, default='/')
    for top in REMOVABLE:
        if inside(gone, top) and not (holder != top and inside(holder, top)):
            return f'{gone} is under {top}, where drives are mounted, and no drive is mounted there now'
    places = [there]
    for parent in [there, *sorted(set(roots))]:
        try:
            places += [os.path.join(parent, name) for name in sorted(os.listdir(parent))]
        except (FileNotFoundError, NotADirectoryError):
            continue
        except OSError as ex:
            raise Unreadable(f'{ex.filename or parent}: {ex.strerror}') from None
    entry = _admin_entry(places, os.path.join(folder, '.git'))
    if entry is not None:
        return f'{entry} still names it: the repository was moved or renamed (git worktree repair there)'
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


def local_branches(cwd):
    """{name: oid} of the repository's own branches (refs/heads). A branch that is a symbolic ref is left out: it
    names another branch and is no branch of its own."""
    branches = {}
    for line in git(cwd, 'for-each-ref', '--format=%(objectname)%09%(symref)%09%(refname)', 'refs/heads').splitlines():
        oid, symref, ref = (line.split('\t') + ['', ''])[:3]
        if not OID.match(oid) or not ref.startswith('refs/heads/'):
            raise Unreadable(f'for-each-ref: a line it cannot use: {line[:80]!r}')
        if not symref:
            branches[ref[len('refs/heads/'):]] = oid
    return branches


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


def symbolic_refs(cwd):
    """{ref: the ref it points at} of every symbolic ref of the repository (`git symbolic-ref`), such as a branch
    that is only another name of a branch."""
    out = {}
    for line in git(cwd, 'for-each-ref', '--format=%(refname)%09%(symref)').splitlines():
        ref, _, target = line.partition('\t')
        if not ref:
            raise Unreadable(f'for-each-ref: a line it cannot use: {line[:80]!r}')
        if target:
            out[ref] = target
    return out


def branch_config(cwd, branch):
    """[(key, value)] of the repository's own config section of the branch, `branch.<name>.*` (its upstream)."""
    head = 'branch.' + branch + '.'
    out = []
    for entry in git(cwd, 'config', '--local', '--null', '--list').split('\0'):
        key, _, value = entry.partition('\n')
        if key.startswith(head) and '.' not in key[len(head):]:
            out.append((key, value))
    return out


def remove_branch(cwd, branch, oid):
    """Deletes the local branch (and its reflog) with `git update-ref -d` only while it still points at `oid`, the
    commit the last check judged: a branch that moved since is left as it is (Untouched, exit 1). Then its config
    section (`branch.<name>.*`, its upstream), as `git branch -d` does, but only while it is the section of the
    branch just deleted: when a branch of that name is there again, or the section changed after it was read before
    the delete, the section is the new branch's and stays."""
    try:
        before = branch_config(cwd, branch)
    except Unreadable as ex:
        raise Untouched(f'its config section: {ex}: nothing removed') from None
    done = _git(cwd, ('update-ref', '--no-deref', '-d', 'refs/heads/' + branch, oid), False, None)
    if done.returncode != 0:
        refused = done.returncode == 1  # expected oid
        raise (Untouched if refused else Unreadable)(f'update-ref: exit {done.returncode}: {_first_line(done.stderr)}'
                                                     + (': nothing removed' if refused else ''))
    try:
        again = git_code(cwd, 'show-ref', '--verify', '--quiet', 'refs/heads/' + branch) == 0
        if not before or again or branch_config(cwd, branch) != before:   # none, or a new branch's
            return
    except Unreadable as ex:
        raise Unreadable(f'the branch is deleted; its config section branch.{branch} stays: {ex}') from None
    done = _git(cwd, ('config', '--local', '--remove-section', 'branch.' + branch), False, None)
    if done.returncode != 0 and 'no such section' not in done.stderr:
        raise Unreadable(f'the branch is deleted; its config section branch.{branch} stays: config: exit '
                         f'{done.returncode}: {_first_line(done.stderr)}')


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


# --- agent transcripts -------------------------------------------------------------------------------------------
# Where agent tools keep a session's transcript, named after its id: `<id>.jsonl`, and a folder `<id>` of its
# sub-agents. The folders are the machine file's (agentScratch.transcripts). Only their times are read (never their
# content), and nothing here changes them: they are must-keep.
def transcript_folders(globs):
    """The real paths of the folders the patterns match (a link on the way, such as a config folder that is a link,
    is followed). Raises Unreadable when a folder on the way cannot be read, or a pattern matches no folder at all
    (a transcript folder that is not there could be one that moved: no answer is no "no transcript")."""
    out = []
    for pattern in globs:
        found = ['/']
        for part in pattern.strip('/').split('/'):
            nxt = []
            for base in found:
                if not any(c in part for c in '*?['):
                    path = os.path.join(base, part)
                    if os.path.isdir(path):
                        nxt.append(path)
                    continue
                try:
                    names = sorted(os.listdir(base))
                except (FileNotFoundError, NotADirectoryError):
                    continue
                except OSError as ex:
                    raise Unreadable(f'{base}: {ex.strerror}') from None
                for name in names:
                    path = os.path.join(base, name)
                    if fnmatch.fnmatchcase(name, part) and os.path.isdir(path):
                        nxt.append(path)
            found = nxt
        if not found:
            raise Unreadable(f'no folder matches {pattern}')
        for folder in found:
            real = os.path.realpath(folder)
            if real not in out:
                out.append(real)
    return out


def read_transcripts(globs):
    """{session id: the newest change of its transcript files}. Raises Unreadable when a folder cannot be read or a
    pattern matches no folder (transcript_folders)."""
    out = {}
    for folder in transcript_folders(globs):
        try:
            with os.scandir(folder) as it:
                for entry in it:
                    sid = entry.name[:-len('.jsonl')] if entry.name.endswith('.jsonl') else entry.name
                    st = entry.stat(follow_symlinks=False)
                    out[sid] = max(out.get(sid, 0.0), st.st_mtime, st.st_ctime)
        except FileNotFoundError:
            continue
        except OSError as ex:
            raise Unreadable(f'{ex.filename or folder}: {ex.strerror}') from None
    return out


# --- one view of it all -------------------------------------------------------------------------------------------
class Facts:
    """What one run knows. While planning, the process list and the mounts are read again when they are older than
    PROCESS_SECONDS, and units and pull request lists are read once. `forget` drops every answer: the engine calls
    it before the last check of each delete, so that check reads the process list, the mounts, the units, the pull
    request lists and Docker's list anew. A reader that failed keeps failing until then."""

    def __init__(self, cfg, processes=None, units=None, prs=None, dangling=None, now=None, mounts=None, transcripts=None):
        self.cfg = cfg
        self.now = time.time() if now is None else now
        self._read_processes = processes or read_processes
        self._read_mounts = mounts or read_mounts
        self._read_units = units or (lambda: read_units(home=cfg.home))
        self._read_transcripts = transcripts or (lambda: read_transcripts(cfg.transcript_globs))
        self._read_prs = prs or github_prs
        self.dangling = dangling or docker_dangling
        self.forget()

    def forget(self):
        self._processes = self._mounts = self._units = self._transcripts = None
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

    def transcripts(self):
        if self._transcripts is None:
            self._transcripts = (self._answer(self._read_transcripts),)
        return self._give(self._transcripts[0])

    def units(self):
        if self._units is None:
            self._units = (self._answer(self._read_units),)
        return self._give(self._units[0])

    def prs(self, repo):
        if repo not in self._prs:
            self._prs[repo] = self._answer(lambda: self._read_prs(repo))
        return self._give(self._prs[repo])
