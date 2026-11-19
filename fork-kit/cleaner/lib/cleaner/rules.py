"""The four rules: `worktrees`, `docker`, `tmp` and `agent-scratch`. A rule only names what it wants gone and why; whether a path
may go at all is the guard's answer (guard.py), and every rule asks it.

`plan` reads and decides, and changes nothing. `recheck` reads everything about one item again, just before its
delete. `delete` is the only code in the kit that removes anything.
"""
import calendar, errno, fnmatch, hashlib, json, os, re, shutil, stat, sys, time

from . import compress, guard
from .config import matches, must_keep_match
from .facts import (DELETED, Unreadable, Untouched, default_branch, elsewhere, git, git_code, local_branches, local_tags,
                    lonely, merged_seconds, origin_url, orphaned_worktree, present, reflog, remote_refs, remote_urls,
                    remove_branch, remove_volume, remove_worktree, symbolic_refs, worktree_list)
from .guard import CREDENTIAL_NAMES, Refusal

DAY = 86400
# Names in the temp folder no pattern can take: hidden entries (sockets of the desktop), what the system or a
# login session owns, and agent scratch folders (the agent-scratch rule's, never the tmp rule's).
TMP_NEVER = ('.*', 'claude-*', 'tmux-*', 'ssh-*', 'systemd-private-*', 'snap-private-tmp', 'snap.*', 'lost+found')
ANONYMOUS = re.compile(r'^[0-9a-f]{64}$')       # the name Docker gives a volume nobody named
EMPTY_PRS = {'merged': [], 'open': []}
GITHUB_URL = re.compile(r'^(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)([^/]+/[^/]+?)(?:\.git)?/?$')
# In a worktree's own git folder: files the commands of somebody working in it write (`fetch` writes FETCH_HEAD).
USE_MARKS = ('HEAD', 'index', 'ORIG_HEAD', 'FETCH_HEAD', 'COMMIT_EDITMSG')
REFLOG_LINE = re.compile(rb'(?:[0-9a-f]{40}|[0-9a-f]{64}) (?:[0-9a-f]{40}|[0-9a-f]{64}) [^<>\n]*<[^<>\n]*> (\d+) [+-]\d{4}(?:\t[^\n]*)?')
# In a worktree's own git folder: an operation somebody started and did not finish.
MARKS = ('MERGE_HEAD', 'CHERRY_PICK_HEAD', 'REVERT_HEAD', 'BISECT_LOG', 'rebase-merge', 'rebase-apply', 'index.lock')


class Item:
    """One thing a rule wants gone. `kind` is path, worktree, clone or volume; for a volume `path` is its name."""

    def __init__(self, kind, path, rule, why, size, look=None, branch=None, oid=None):
        self.kind, self.path, self.rule, self.why, self.size = kind, path, rule, why, size
        self.done = None        # after a run: deleted, refused or failed
        self.look = look        # the guard's look at the last check: the delete removes only what it saw
        self.branch, self.oid = branch, oid     # kind 'branch': `path` is its clone, `oid` the commit last judged

    @property
    def label(self):
        if self.kind == 'branch':
            return branch_label(self.path, self.branch)
        return f'docker volume {self.path}' if self.kind == 'volume' else self.path


class Kept:
    """One thing a rule looked at and keeps, and why."""

    def __init__(self, path, rule, code, detail, size=None):
        self.path, self.rule, self.code, self.detail, self.size = path, rule, code, detail, size


class Plan:
    def __init__(self):
        self.items, self.kept = [], []


def human(n):
    if n is None:
        return 'size not known'
    if n < 1024:
        return f'{n} B'
    for unit in ('KB', 'MB', 'GB'):
        n /= 1024
        if n < 1024:
            break
    return f'{n:.1f} {unit}'


def files_text(n):
    return f'{n} file' if n == 1 else f'{n} files'


def days_text(n):
    return f'{n} day' if n == 1 else f'{n} days'


def _entries(root):
    """The names directly in a rule's folder. (None, None): the folder is not there, and so there is nothing to do."""
    try:
        return sorted(os.listdir(root)), None
    except (FileNotFoundError, NotADirectoryError):
        return None, None
    except OSError as ex:
        return None, Refusal('could not read', f'{root}: {ex.strerror}')


# --- tmp ----------------------------------------------------------------------------------------------------------
def tmp_match(cfg, name):
    """(pattern, days) of the first pattern that takes an entry of the temp folder called `name`; None when no
    pattern matches or no pattern may take it. The disk report's temp class is the entries this takes."""
    if matches(name, TMP_NEVER):
        return None
    match = None
    for pattern, days in cfg.tmp_patterns:
        if fnmatch.fnmatchcase(name, pattern):
            match = (pattern, days)
            break
    return match


def tmp_answer(cfg, facts, path):
    """(why, look) for one entry of the temp folder, or None when the rule does not take it: no pattern matches
    its name, or it changed too recently."""
    match = tmp_match(cfg, os.path.basename(path))
    if match is None:
        return None
    pattern, days = match
    try:
        top = guard.changed(os.lstat(path))
    except FileNotFoundError:
        return None
    except OSError as ex:  # tmp entry
        return '', guard.Look(Refusal('could not read', f'{path}: {ex.strerror}'))
    look = guard.examine(cfg, facts, path, 'path', orphans=True)
    age = facts.now - max(top, look.newest)       # a refusal stops the look early: then the entry's own time counts
    if age < days * DAY:
        return None
    why = f'not changed for {days_text(int((age + 1) // DAY))} ({pattern}: {days_text(days)})'
    for where, gone in look.orphans[:1]:
        why += f'; orphaned worktree{"" if where == "." else " " + where}: its git folder {gone} is gone, so it holds no commit'
    return why, look


def plan_tmp(cfg, facts, plan, measure):
    if not cfg.tmp_patterns:
        return
    names, problem = _entries(cfg.tmp_root)
    if problem:
        plan.kept.append(Kept(cfg.tmp_root, 'tmp', problem.code, problem.detail))
    for name in names or ():
        path = os.path.join(cfg.tmp_root, name)
        answer = tmp_answer(cfg, facts, path)
        if answer is None:
            continue
        why, look = answer
        if look.refusal is not None:
            plan.kept.append(Kept(path, 'tmp', look.refusal.code, look.refusal.detail, guard.measure(path) if measure else None))
        else:
            plan.items.append(Item('path', path, 'tmp', why, look.size, look))


# --- agent-scratch ------------------------------------------------------------------------------------------------
# The scratch folder of one agent session is `<root>/<project>/<session id>` (Claude Code: /tmp/claude-<uid>/<the
# project folder, as a name>/<the session's uuid>). Only folders named like a session id are looked at.
SESSION_NAME = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
SCRATCH = 'agent-scratch'
MANIFEST = 'manifest.json'
NO_FOLLOW = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC


class KeepFirstError(Exception):
    """The copy into the archive could not be made or verified: the session folder stays."""


def _stamp(seconds):
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(seconds))


def _seconds(stamp):
    try:
        return float(calendar.timegm(time.strptime(stamp, '%Y-%m-%dT%H:%M:%SZ')))
    except (TypeError, ValueError, OverflowError):
        return None


class Found:
    """What keep first finds in a session folder. `keep`: (name, size, source) of each file it copies (`source`:
    the parts of the path it is read from, inside the session folder; for an output that is a link, where the link
    leads). `held`: (name, size) of each report over maxFileBytes, which keeps the session whole (it is not copied,
    and nothing may be deleted before it is). `listed`: (name, size, source) of each command output over
    maxFileBytes: not copied, and named in the manifest with its sha256. `out`: (name, link text) of each output
    that is a link out of the session folder (to a sub-agent's transcript, which stays): never followed."""

    def __init__(self):
        self.keep, self.held, self.listed, self.out = [], [], [], []


def _matching(path, pattern, look):
    """(entry path, relative parts, lstat) of each entry the pattern names in the session folder: folders on the
    way are real folders the last check walked, never a link, never a git folder or a clone."""
    bases = [(path, ())]
    for depth, glob in enumerate(pattern):
        last = depth == len(pattern) - 1
        nxt = []
        for base, rel in bases:
            try:
                with os.scandir(base) as it:
                    entries = list(it)
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as ex:
                raise Unreadable(f'{base}: {ex.strerror}') from None
            for entry in entries:
                if entry.name == '.git' or not fnmatch.fnmatchcase(entry.name, glob):
                    continue
                st = entry.stat(follow_symlinks=False)
                if st.st_ino not in look.seen:
                    continue
                if last:
                    nxt.append((entry.path, rel + (entry.name,), st))
                elif stat.S_ISDIR(st.st_mode) and not os.path.lexists(os.path.join(entry.path, '.git')):
                    nxt.append((entry.path, rel + (entry.name,)))
        bases = nxt
    return sorted(bases, key=lambda b: b[1])


def _inside(path, entry_path, look):
    """For an output that is a link: (parts, lstat) of the regular file it leads to when that is in the session
    folder and the last check walked it; None when it leads out (or to anything else)."""
    if os.path.realpath(path) != path:
        return None
    target = os.path.realpath(entry_path)
    if not target.startswith(path + '/'):
        return None
    try:
        st = os.lstat(target)
    except OSError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_ino not in look.seen:
        return None
    return tuple(target[len(path) + 1:].split('/')), st


def keep_first_files(cfg, path, look):
    """What keep first copies, holds or names in a session folder (Found). The machine file's report patterns:
    regular files only, never through a link, never inside a git folder; a report over maxFileBytes holds the
    session. Its output patterns: the same, and a link is resolved only to a regular file inside the session
    folder; an output over maxFileBytes is named, not copied."""
    found, names = Found(), set()
    for outputs, patterns in ((False, cfg.keep_patterns), (True, cfg.keep_outputs)):
        for pattern in patterns:
            for entry_path, rel, st in _matching(path, pattern, look):
                name = '/'.join(rel)
                if name in names:
                    continue
                source = rel
                if outputs and stat.S_ISLNK(st.st_mode):
                    resolved = _inside(path, entry_path, look)
                    if resolved is None:
                        try:
                            found.out.append((name, os.readlink(entry_path)))
                        except OSError:
                            found.out.append((name, '?'))
                        names.add(name)
                        continue
                    source, st = resolved
                if not stat.S_ISREG(st.st_mode):
                    continue
                names.add(name)
                if st.st_size <= cfg.keep_max_file:
                    found.keep.append((name, st.st_size, source))
                elif outputs:
                    found.listed.append((name, st.st_size, source))
                else:
                    found.held.append((name, st.st_size))
    found.keep.sort()
    return found


def _open_session(item):
    """A descriptor of the session folder, opened one name at a time from /, never through a link, and only when
    it is still the folder the last check judged."""
    try:
        parent = _open_parent(item.path)
        try:
            top = os.open(os.path.basename(item.path), DIR_FLAGS, dir_fd=parent)
        finally:
            os.close(parent)
    except OSError as ex:
        raise KeepFirstError(f'{item.path}: {ex.strerror}') from None
    st = os.fstat(top)
    if item.look is None or item.look.ident != (st.st_dev, st.st_ino):
        os.close(top)
        raise KeepFirstError(f'{item.path} is not what the last check saw')
    return top


def _open_in(top, parts, look):
    """A descriptor of the file `parts` in the open session folder: every folder on the way entered by descriptor
    and never through a link, the file opened without following a link, and only a regular file the last check
    walked (its inode)."""
    fd = top
    try:
        for part in parts[:-1]:
            new = os.open(part, DIR_FLAGS, dir_fd=fd)
            if fd != top:
                os.close(fd)
            fd = new
        out = os.open(parts[-1], NO_FOLLOW, dir_fd=fd)
    except OSError as ex:
        raise KeepFirstError(f'{"/".join(parts)}: {ex.strerror}') from None
    finally:
        if fd != top:
            os.close(fd)
    st = os.fstat(out)
    if not stat.S_ISREG(st.st_mode) or st.st_ino not in look.seen:
        os.close(out)
        raise KeepFirstError(f'{"/".join(parts)}: not the regular file the last check saw')
    return out


def _read_fd(fd, sha=None):
    """The whole file (or, with `sha`, only its size, the content fed to `sha`); closes the descriptor."""
    try:
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, 1 << 20)
            if not chunk:
                return size if sha is not None else b''.join(chunks)
            size += len(chunk)
            if sha is None:
                chunks.append(chunk)
            else:
                sha.update(chunk)
    finally:
        os.close(fd)


def _read_whole(path):
    return _read_fd(os.open(path, NO_FOLLOW))


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _read_manifest(folder):
    """The manifest of an archived copy as ({path: sha256} of the copied files, {path: sha256} of the files only
    named); None when there is none or it cannot be used."""
    try:
        manifest = json.loads(_read_whole(os.path.join(folder, MANIFEST)))
        return ({f['path']: f['sha256'] for f in manifest['files']},
                {f['path']: f['sha256'] for f in manifest.get('notCopied', [])})
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _verified(folder, wanted, named=None):
    """True when the archived copy in `folder` (a real folder, never a link) holds every (path, sha256) of
    `wanted`, each file read back, and names every (path, sha256) of `named`."""
    try:
        if not stat.S_ISDIR(os.lstat(folder).st_mode):
            return False
    except OSError:
        return False
    have = _read_manifest(folder)
    if have is None or any(have[0].get(name) != sha for name, sha in wanted.items()):
        return False
    if any(have[1].get(name) != sha for name, sha in (named or {}).items()):
        return False
    try:
        return all(_sha(_read_whole(os.path.join(folder, *name.split('/')))) == sha for name, sha in wanted.items())
    except OSError:
        return False


def _sync_folder(folder):
    fd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _own_folder(path, top):
    """Makes the folder `path` and every missing folder above it (mode 0700), each synced into its parent, so a
    copy in it outlives a power cut. Every folder from `top` (the archive) down to `path` must then be a real folder
    of this user: the copy is never written through a link."""
    missing, here = [], path
    while not os.path.lexists(here):
        missing.append(here)
        here = os.path.dirname(here)
    for folder in reversed(missing):
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        _sync_folder(os.path.dirname(folder))
    here = path
    while True:
        st = os.lstat(here)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise KeepFirstError(f'{here}: a link, not a folder, or not this user\'s: no copy is written there')
        if here == top:
            return
        here = os.path.dirname(here)


def keep_first(cfg, item):
    """Copies the files worth keeping (keep_first_files) of a session folder into the archive, verifies the copy
    and returns what it says about it; None when there is nothing to keep. Raises KeepFirstError when the copy
    cannot be made or read back, or a report is too large to copy: the folder then stays. Runs again safely: a copy
    already in the archive that holds the same files with the same sha256 is used as it is; a half-written copy
    (`.<session>.partial`, the cleaner's own) is thrown away and made again; a different older copy stays, and the
    new one gets the next free name."""
    try:
        found = keep_first_files(cfg, item.path, item.look)
    except Unreadable as ex:
        raise KeepFirstError(str(ex)) from None
    if found.held:
        raise KeepFirstError(f'{found.held[0][0]}: larger than maxFileBytes: the session stays')
    if ([(name, size) for name, size, _ in found.keep] != list(item.look.keep_first) or
            [(name, size) for name, size, _ in found.listed] != list(item.look.keep_named)):
        raise KeepFirstError('what is worth keeping is not what the last check saw: the session stays')
    if not found.keep and not found.listed:
        return None
    project, session = item.path.split('/')[-2:]
    data, named, sizes = {}, {}, {}
    top = _open_session(item)
    try:
        for name, _, source in found.keep:
            data[name] = _read_fd(_open_in(top, source, item.look))
        for name, _, source in found.listed:
            sha = hashlib.sha256()
            sizes[name] = _read_fd(_open_in(top, source, item.look), sha)
            named[name] = sha.hexdigest()
    except OSError as ex:
        raise KeepFirstError(f'{ex.filename or item.path}: {ex.strerror}') from None
    finally:
        os.close(top)
    wanted = {name: _sha(body) for name, body in data.items()}
    total = sum(len(body) for body in data.values())
    folder = os.path.join(cfg.keep_archive, project)
    final = os.path.join(folder, session)
    told = f'{files_text(len(data))} ({human(total)})' + (f', {len(named)} larger named only' if named else '')
    try:
        _own_folder(folder, cfg.keep_archive)
        versions = [final] + [f'{final}.{n}' for n in range(2, 100)]
        for version in versions:
            if not os.path.lexists(version):
                break
            if _verified(version, wanted, named):
                return f'{told} already in {version} (verified)'
        else:
            raise KeepFirstError(f'{final}: 99 different copies are there already')
        partial = os.path.join(folder, f'.{session}.partial')
        if os.path.lexists(partial):
            _remove_own(partial)
        os.mkdir(partial, 0o700)
        for name, body in data.items():
            target = os.path.join(partial, *name.split('/'))
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, body)
                os.fsync(fd)
            finally:
                os.close(fd)
        manifest = {'version': 1, 'rule': SCRATCH, 'source': item.path, 'project': project, 'session': session,
                    'keptAt': _stamp(time.time()),
                    'files': [{'path': name, 'bytes': len(data[name]), 'sha256': wanted[name]} for name in sorted(data)],
                    'notCopied': [{'path': name, 'bytes': sizes[name], 'sha256': named[name],
                                   'why': f'larger than maxFileBytes ({cfg.keep_max_file})'} for name in sorted(named)],
                    'linksOut': [{'path': name, 'to': to, 'why': 'a link out of the session folder: not followed'}
                                 for name, to in found.out]}
        fd = os.open(os.path.join(partial, MANIFEST), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (json.dumps(manifest, indent=1) + '\n').encode())
            os.fsync(fd)
        finally:
            os.close(fd)
        for here, _, _ in os.walk(partial):
            _sync_folder(here)
        if not _verified(partial, wanted, named):
            raise KeepFirstError(f'{partial}: the copy does not read back the same')
        os.rename(partial, version)
        _sync_folder(folder)
    except OSError as ex:
        raise KeepFirstError(f'{ex.filename or folder}: {ex.strerror}') from None
    if not _verified(version, wanted, named):
        raise KeepFirstError(f'{version}: the copy does not read back the same')
    return f'{told} in {version} (verified)'


def _remove_own(partial):
    """Removes a half-written copy of the cleaner's own from the archive, never through a link: a link or a file at
    its name is unlinked as itself (whatever it leads to stays), and a folder is removed by descriptors, which
    refuse a link at the top and never follow one below (shutil.rmtree's fd-based walk)."""
    if not shutil.rmtree.avoids_symlink_attacks:
        raise KeepFirstError(f'{partial}: this system cannot remove a folder without following links')
    st = os.lstat(partial)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(partial)
        return
    shutil.rmtree(partial)


# Processes of the user that hide their open files and environment on purpose (not dumpable, so only root may
# read them) and hold no agent session: a login session's helpers and the key agents. The name excuses only what
# cannot be read: what can be read of such a process (its command line, its sockets) still keeps a session. Any
# other hidden process of the user is judged by its nearest readable ancestor (_judged_by); one with none keeps
# every session ("could not read"). Compared with /proc/<pid>/comm.
HIDDEN_OK = ('systemd', '(sd-pam)', 'gpg-agent', 'ssh-agent', 'scdaemon', 'keyboxd', 'gnome-keyring-d')
# The user's service manager (systemd --user): where an ancestor walk stops, as at pid 1. A hidden process started
# by it, or by pid 1, has no agent above it to be judged by.
USER_MANAGER = 'systemd'


def _judged_by(p, own):
    """The nearest ancestor of the hidden process `p` that can be read in full and is the user's own: what it works
    in, has open and names decides for `p` too (a Codex started by an agent's tool hides itself, and its parent
    names the agent's session). None when there is none short of pid 1 or the user's service manager, or an
    ancestor is not the user's, has ended or cannot be told."""
    by_pid = {q.pid: q for q in own}
    walked, pid = set(), p.parent
    while pid is not None and pid > 1 and pid not in walked:
        walked.add(pid)
        q = by_pid.get(pid)
        if q is None or q.name == USER_MANAGER:
            return None
        if not q.hidden:
            return q
        pid = q.parent
    return None


def session_alive(cfg, facts, path, look):
    """A refusal when the session may still be alive (README, agent-scratch): its transcript changed within
    `days`; a process works in its folder, has something in it open or names its path; a process names its id; a
    process of the user cannot be read in full; or a process works in another session of the same project without
    naming that one, and started before this session last changed (after /clear or /resume a session's process
    works on under a new id). None when it is gone. Asked as late as possible: the plan and the last check ask it
    last, and the run asks it again after keep first, just before the delete."""
    sid, project = os.path.basename(path), os.path.dirname(path)
    days = cfg.scratch_days
    try:
        transcripts = facts.transcripts()
    except Unreadable as ex:
        return Refusal('could not read', f'the transcripts ({ex})')
    said = transcripts.get(sid)
    if said is not None and facts.now - said < days * DAY:
        return Refusal(f'transcript changed under {days_text(days)} ago', f'{_stamp(said)}')
    try:
        processes = facts.processes()
    except Unreadable as ex:
        return Refusal('could not read', f'the process list ({ex})')
    if processes.uses(path):
        return Refusal('session alive', 'a process works in it, has a file or folder in it open, or names its path')
    if processes.names(sid):
        return Refusal('session alive', 'a process names its id (command line or environment)')
    # A hidden process with a readable ancestor is that ancestor's: the ancestor's command line, environment, working
    # folder and open files were asked above and are asked below, like every other process.
    unread = [p for p in processes.own if p.hidden and p.name not in HIDDEN_OK and _judged_by(p, processes.own) is None]
    if unread:
        return Refusal('could not read', f'process {unread[0].pid} ({unread[0].name or "?"}) of this user does not '
                                         'show its open files or environment')
    newest = max(look.newest if look is not None else 0.0, said or 0.0)
    for p in processes.own:
        for where in sorted(p.paths):
            if where.endswith(DELETED):
                where = where[:-len(DELETED)]
            if not where.startswith(project + '/'):
                continue
            other = where[len(project) + 1:].split('/')[0]
            if other == sid or (SESSION_NAME.match(other) and other in p.words):
                continue                # this one (uses), or a session the process names: it is that one's
            if p.start is None or p.start <= newest:
                return Refusal('session alive', f'process {p.pid} ({p.name or "?"}) works in {other} of the same '
                               'project without naming it, and started before this session last changed')
    return None


def scratch_answer(cfg, facts, path, started=None):
    """(why, look) for one session folder, or None when the rule does not take it (changed too recently).
    `started`: an earlier run began to delete it at this time (seconds); then the folders' own times do not count
    (that delete changed them), and nothing that is not a folder may have changed since."""
    sid = os.path.basename(path)
    if not SESSION_NAME.match(sid):
        return None
    try:
        top = guard.changed(os.lstat(path))
    except FileNotFoundError:
        return None
    except OSError as ex:
        return '', guard.Look(Refusal('could not read', f'{path}: {ex.strerror}'))
    days = cfg.scratch_days
    look = guard.examine(cfg, facts, path, 'path')
    if started is None:
        age = facts.now - max(top, look.newest)
        if age < days * DAY:
            return None
        why = f'session gone, not changed for {days_text(int((age + 1) // DAY))}'
    else:
        if look.newest_file > started:
            look.refusal = look.refusal or Refusal('changed since the delete began', f'after {_stamp(started)}')
        why = f'finishing the delete an earlier run began at {_stamp(started)}, session gone'
    if look.refusal is not None:
        return why, look
    # A lock-named file directly in the session folder. Deeper, `yarn.lock` or `Cargo.lock` of a clone is ordinary
    # content; a lock a process holds anywhere in it is the guard's `in use`.
    try:
        with os.scandir(path) as it:
            locks = sorted(entry.name for entry in it if guard.matches(entry.name, guard.LOCK_NAMES))
    except OSError as ex:
        look.refusal = Refusal('could not read', f'{path}: {ex.strerror}')
        return why, look
    if locks:
        look.refusal = Refusal('lock file', some(locks))
        return why, look
    try:
        found = keep_first_files(cfg, path, look)
    except Unreadable as ex:
        look.refusal = Refusal('could not read', str(ex))
        return why, look
    if found.held:
        look.refusal = Refusal('report too large to keep first',
                               f'{some([name for name, _ in found.held])}: more than maxFileBytes '
                               f'({human(cfg.keep_max_file)}); it stays until a person keeps it or it is gone')
        return why, look
    total = sum(size for _, size, _ in found.keep)
    if total > cfg.keep_max_session:
        look.refusal = Refusal('too much to keep first', f'{files_text(len(found.keep))}, {human(total)}: more than '
                                                         f'maxSessionBytes ({human(cfg.keep_max_session)})')
        return why, look
    # The session: asked last, so that the answer is as late as possible.
    look.refusal = session_alive(cfg, facts, path, look)
    if look.refusal is not None:
        return why, look
    look.keep_first = [(name, size) for name, size, _ in found.keep]
    look.keep_named = [(name, size) for name, size, _ in found.listed]
    first = f'keep first: {files_text(len(found.keep))} ({human(total)})' if found.keep else 'nothing to keep first'
    if found.listed:
        first += f', {len(found.listed)} larger output{"s" if len(found.listed) != 1 else ""} named only'
    return f'{why}; {first}', look


def plan_scratch(cfg, facts, plan, measure):
    if not cfg.scratch_enabled:
        return
    projects, problem = _entries(cfg.scratch_root)
    if problem:
        plan.kept.append(Kept(cfg.scratch_root, SCRATCH, problem.code, problem.detail))
    for project in projects or ():
        folder = os.path.join(cfg.scratch_root, project)
        try:
            st = os.lstat(folder)
        except OSError:
            continue
        if stat.S_ISLNK(st.st_mode):
            plan.kept.append(Kept(folder, SCRATCH, 'symbolic link', 'a link is never followed and never removed'))
            continue
        if not stat.S_ISDIR(st.st_mode):
            continue
        names, problem = _entries(folder)
        if problem:
            plan.kept.append(Kept(folder, SCRATCH, problem.code, problem.detail))
        for name in names or ():
            path = os.path.join(folder, name)
            answer = scratch_answer(cfg, facts, path)
            if answer is None:
                continue
            why, look = answer
            if look.refusal is not None:
                plan.kept.append(Kept(path, SCRATCH, look.refusal.code, look.refusal.detail, guard.measure(path) if measure else None))
            else:
                plan.items.append(Item('session', path, SCRATCH, why, look.size, look))


def finish_scratch(cfg, facts, entry):
    """An agent-scratch folder an earlier run began to delete (its journal line): an Item to finish it, or a Kept
    with the reason it stays. The whole folder was judged deletable then; what is left is judged again now."""
    path = entry['path']
    started = _seconds(entry.get('at'))
    if started is None or not cfg.scratch_enabled or not cfg.session_parent(os.path.dirname(path)):
        return None
    answer = scratch_answer(cfg, facts, path, started)
    if answer is None:
        return None
    why, look = answer
    if look.refusal is not None:
        return Kept(path, SCRATCH, look.refusal.code, look.refusal.detail)
    item = Item('session', path, SCRATCH, why, look.size, look)
    item.started = started
    return item


# --- deleting -----------------------------------------------------------------------------------------------------
# Every delete goes through descriptors, one name at a time, and never through a link: a name that is a link now is
# removed as a link, and a folder is entered only when it is the folder the name showed a moment before.
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_parent(path):
    """A descriptor of the folder `path` is in, opened from / one name at a time, never through a link."""
    fd = os.open('/', DIR_FLAGS)
    try:
        for part in os.path.dirname(path).split('/'):
            if part:
                new = os.open(part, DIR_FLAGS, dir_fd=fd)
                os.close(fd)
                fd = new
        return fd
    except BaseException:
        os.close(fd)
        raise


def _clear(top, names, look, shown):
    """Removes `names` from the open folder `top`, each folder with everything in it. Every entry must be one the
    last check saw (its inode, on its device): a folder moved in since, a new mount or a new file stops the delete
    there, so nothing the check did not judge is removed. With `look.checked` (agent scratch: when the last
    liveness check began), an entry whose inode changed since then also stops it: a new file can get the inode
    number of one just deleted (ext4 gives it out again at once), and only its inode time shows it is new. A file
    with several hard links is let through: deleting one of its names changes the inode time of the others."""
    stack = [(top, sorted(names, reverse=True), None, shown)]
    try:
        while stack:
            here, left, done, where = stack[-1]
            if not left:
                stack.pop()
                if done is not None:
                    os.close(here)
                    os.rmdir(done[1], dir_fd=done[0])
                continue
            name = left.pop()
            st = os.stat(name, dir_fd=here, follow_symlinks=False)
            if st.st_dev != look.ident[0] or st.st_ino not in look.seen:  # seen
                raise Unreadable(f'{where}/{name}: not there at the last check (moved in, made or mounted since): the delete stopped')
            if (look.checked is not None and st.st_ctime >= look.checked and  # made since
                    (stat.S_ISDIR(st.st_mode) or (st.st_nlink == 1 and st.st_ino not in look.linked))):
                raise Unreadable(f'{where}/{name}: changed after the last check (a new file can reuse an inode '
                                 'number): the delete stopped')
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=here)
                continue
            sub = os.open(name, DIR_FLAGS, dir_fd=here)
            sst = os.fstat(sub)
            if (sst.st_dev, sst.st_ino) != (st.st_dev, st.st_ino):
                os.close(sub)
                raise Unreadable(f'{where}/{name}: changed while it was deleted: the delete stopped')
            stack.append((sub, sorted(os.listdir(sub), reverse=True), (here, name), f'{where}/{name}'))
    finally:
        for here, _, done, _ in stack:
            if done is not None:
                os.close(here)


# Linux's CLOCK_REALTIME_COARSE (Python names no constant for it): the clock the kernel stamps file times with.
CLOCK_COARSE = getattr(time, 'CLOCK_REALTIME_COARSE', 5)


def clock():
    """Now, by the clock file times are stamped with, so a file changed after this moment never shows an earlier
    time: on Linux the coarse clock (the precise one runs up to a tick ahead of file times). Elsewhere the precise
    clock, a tenth of a second back."""
    if sys.platform.startswith('linux'):
        try:
            return time.clock_gettime(CLOCK_COARSE)
        except OSError:
            pass
    return time.time() - 0.1


def remove_tree(path, look, last=None):
    """Removes the file or folder at `path` if it is still what the last check judged (`look.ident`); else raises
    Untouched and removes nothing. A folder is emptied through its descriptor, `last` (a name in it) after
    everything else, and then removed."""
    if look is None or look.ident is None:
        raise Untouched(f'{path}: no last check to compare with: nothing removed')
    try:
        parent = _open_parent(path)
    except OSError as ex:
        raise Untouched(f'{path}: its folder: {ex.strerror}: nothing removed') from None
    try:
        name = os.path.basename(path)
        try:
            st = os.stat(name, dir_fd=parent, follow_symlinks=False)
            if (st.st_dev, st.st_ino) != look.ident:  # ident
                raise Untouched(f'{path} is not what the last check saw (it was moved, or a link or another folder '
                                'is there now): nothing removed')
            if not stat.S_ISDIR(st.st_mode):
                os.unlink(name, dir_fd=parent)
                return
            top = os.open(name, DIR_FLAGS, dir_fd=parent)
        except Untouched:
            raise
        except OSError as ex:
            raise Untouched(f'{path}: {ex.strerror}: nothing removed') from None
        try:
            tst = os.fstat(top)
            if (tst.st_dev, tst.st_ino) != look.ident:
                raise Untouched(f'{path} changed as it was opened: nothing removed')
            names = os.listdir(top)
            _clear(top, [n for n in names if n != last], look, path)
            _clear(top, [n for n in names if n == last], look, path)
        finally:
            os.close(top)
        os.rmdir(name, dir_fd=parent)
    finally:
        os.close(parent)


def _still(item):
    """Untouched unless the folder at item.path is still the one the last check judged (never through a link)."""
    try:
        parent = _open_parent(item.path)
    except OSError as ex:
        raise Untouched(f'{item.path}: its folder: {ex.strerror}: nothing removed') from None
    try:
        st = os.stat(os.path.basename(item.path), dir_fd=parent, follow_symlinks=False)
    except OSError as ex:
        raise Untouched(f'{item.path}: {ex.strerror}: nothing removed') from None
    finally:
        os.close(parent)
    if item.look is None or (st.st_dev, st.st_ino) != item.look.ident:
        raise Untouched(f'{item.path} is not what the last check saw (it was moved, or a link or another folder is '
                        'there now): nothing removed')


def delete_path(item):
    remove_tree(item.path, item.look)


# --- docker -------------------------------------------------------------------------------------------------------
def volume_answer(cfg, facts, volume):
    """(why, refusal) for one unused anonymous volume."""
    age = facts.now - volume.created
    if age < cfg.docker_days * DAY:
        return '', Refusal(f'under {cfg.docker_days} days old', f'made {days_text(int(max(age, 0) // DAY))} ago')
    try:
        processes = facts.processes()
    except Unreadable as ex:
        return '', Refusal('could not read', f'the process list ({ex})')
    if volume.mountpoint in processes.busy:
        return '', Refusal('in use', 'a process runs in it, has a file in it open or names it')
    return f'anonymous, no container uses it, made {days_text(int(age // DAY))} ago', None


def plan_docker(cfg, facts, plan, measure):
    kept = plan.kept
    if not cfg.docker_enabled:
        return
    try:
        volumes = facts.dangling()
    except Unreadable as ex:
        kept.append(Kept('docker', 'docker', 'could not read', str(ex)))
        return
    for volume in volumes:
        if not ANONYMOUS.match(volume.name):
            continue                    # a named volume is never touched, and never listed
        why, refusal = volume_answer(cfg, facts, volume)
        if refusal is not None:
            kept.append(Kept(volume.name, 'docker', refusal.code, refusal.detail))
        else:
            plan.items.append(Item('volume', volume.name, 'docker', why, None))


def recheck_volume(cfg, facts, item):
    if not cfg.docker_enabled or not ANONYMOUS.match(item.path):
        return Refusal('not a candidate', 'the docker rule does not take it')
    try:
        names = {volume.name: volume for volume in facts.dangling()}
    except Unreadable as ex:
        return Refusal('could not read', str(ex))
    if item.path not in names:
        return Refusal('in use', 'a container uses it now, or it is gone')
    return volume_answer(cfg, facts, names[item.path])[1]


# --- worktrees ----------------------------------------------------------------------------------------------------
def same_repo(url, github):
    found = GITHUB_URL.match(url.strip())
    return bool(found) and found.group(1).lower() == github.lower()


def numbers(rows):
    return ', '.join(f'#{row["number"]}' for row in rows[:5]) + (' ...' if len(rows) > 5 else '')


def some(names):
    return ', '.join(names[:5]) + (f' and {len(names) - 5} more' if len(names) > 5 else '')


def regenerable(repo, path, name):
    """True when an entry git ignores is build output any tool makes again: a link (it holds nothing), or inside a
    folder whose name is on the repository's list, exactly (the list holds plain folder names, never a pattern, so
    no entry can make every ignored file build output). A credential-looking name is never build output."""
    parts = name.rstrip('/').split('/')
    if guard.matches(parts[-1], CREDENTIAL_NAMES):
        return False
    if os.path.islink(os.path.join(path, name.rstrip('/'))):
        return True
    folders = parts if name.endswith('/') else parts[:-1]
    return any(part in repo.regenerable for part in folders)


def tree_kind(path):
    """'clone' for a folder with a .git folder, 'worktree' for one with a .git file, None for anything else.
    Raises Unreadable when it cannot tell (a folder it may not read)."""
    try:
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            return None
        dot = os.lstat(os.path.join(path, '.git'))
    except OSError as ex:
        if ex.errno in (errno.ENOENT, errno.ENOTDIR):
            return None
        raise Unreadable(f'{ex.filename or path}: {ex.strerror}') from None
    return 'clone' if stat.S_ISDIR(dot.st_mode) else 'worktree' if stat.S_ISREG(dot.st_mode) else None


def tree_refusal(cfg, facts, repo, path, kind, prs, out):
    """Why a worktree or clone of the listed repository stays; None when it may go. Raises Unreadable when git
    cannot answer. `out` gets the pull request it is finished by ('why') and the look at its files ('look')."""
    entries = worktree_list(path)
    real = os.path.realpath(path)
    mine = [entry for entry in entries if os.path.realpath(entry['worktree']) == real]
    if len(mine) != 1 or (kind == 'clone') != (mine[0] is entries[0]) or 'bare' in entries[0]:
        raise Unreadable('worktree list: does not name this folder as expected (was it moved?)')
    entry = mine[0]
    if kind == 'clone':
        # A full clone goes only after its worktrees: a later run, when git no longer lists one that is there.
        others = [e['worktree'] for e in entries[1:] if e.get('locked') is not None or os.path.lexists(e['worktree'])]
        if others:
            return Refusal('worktrees hang off it', f'{len(others)}: {some([os.path.basename(o) for o in others])}')
    if entry.get('locked') is not None:
        return Refusal('locked', entry['locked'] or 'no reason given')
    head = entry.get('HEAD', '')
    branch = entry.get('branch', '')
    branch = branch[len('refs/heads/'):] if branch.startswith('refs/heads/') else ''
    if not branch:
        return Refusal('no branch', 'HEAD is detached')
    if not head.strip('0'):
        # The branch has no commit: it was deleted under the worktree, or never had one (`git checkout --orphan`).
        # What is in the worktree (staged files too) is in no commit anywhere.
        return Refusal('no commit', f'{branch} has no commit (deleted under it, or never had one)')
    if repo.branches and not matches(branch, repo.branches):
        return Refusal('branch not listed', f'{branch} matches none of {some(list(repo.branches))}')

    opened = [row for row in prs['open'] if row['headRefName'] == branch]
    if opened:
        return Refusal('open PR', numbers(opened))
    # A branch other pull requests merge into (main, or the lower branch of a stack) is not finished by one of them,
    # and nor is GitHub's default branch, even when no pull request in the list merges into it.
    if branch == default_branch(path):
        return Refusal('base branch', f'{branch} is the default branch on GitHub')
    based = [row for row in prs['open'] + prs['merged'] if row['baseRefName'] == branch]
    if based:
        return Refusal('base branch', f'pull requests merge into {branch}: {numbers(based)}')
    # Only a pull request from this repository: a fork's "main" is not this repository's main.
    merged = [row for row in prs['merged'] if row['headRefName'] == branch and not row['isCrossRepository']]
    if not merged:
        return Refusal('no merged PR', f'no merged pull request of this repository has the branch {branch}')
    here = present(path, [row['headRefOid'] for row in prs['merged'] + prs['open']])
    holding = []
    for row in merged:
        oid = row['headRefOid']
        if oid != head:
            if oid not in here:
                continue                # GitHub's head is a commit this folder does not have: nothing to compare
            if git_code(path, 'merge-base', '--is-ancestor', head, oid) != 0:
                continue
        holding.append(row)
    if not holding:
        return Refusal('commits not in the merged PR', f'HEAD is not in {numbers(merged)}')
    times = [merged_seconds(row) for row in holding]
    if None in times:
        return Refusal('could not read', f'GitHub (no merge time for {numbers([r for r, t in zip(holding, times) if t is None])})')
    age = facts.now - max(times)
    if age < cfg.merged_hours * 3600:
        return Refusal(f'merged under {cfg.merged_hours} h ago', f'{numbers(holding)} merged {int(max(age, 0) // 3600)} h ago')
    out['why'] = f'{numbers(holding)} merged {days_text(int(age // DAY))} ago, nothing in it that is only here'

    changed, ignored = [], []
    fields = git(path, 'status', '--porcelain', '-z', '--untracked-files=normal', '--ignored=matching').split('\0')
    while fields:
        field = fields.pop(0)
        if len(field) < 4:
            continue
        if field[0] in 'RC':
            fields.pop(0)               # a rename or copy is followed by the name it came from
        (ignored if field[:2] == '!!' else changed).append(field[3:])
    if changed:
        return Refusal('uncommitted or untracked', some(changed))
    precious = [name for name in ignored if not regenerable(repo, path, name)]
    if precious:
        return Refusal('ignored files', some(precious))
    # `git status` does not show a change in a file marked skip-worktree (S) or assume-unchanged (lower case).
    tags = [field.split(' ', 1) for field in git(path, 'ls-files', '-v', '-z').split('\0') if field]
    hidden = [field[1] for field in tags if len(field) == 2 and (field[0] == 'S' or field[0].islower())]
    if hidden:
        return Refusal('hidden from git status', some(hidden))
    git_dir = git(path, 'rev-parse', '--absolute-git-dir').strip()
    marks = [mark for mark in MARKS if os.path.lexists(os.path.join(git_dir, mark))]
    if marks:
        return Refusal('unfinished git operation', some(marks))
    own = git(path, 'for-each-ref', '--format=%(refname)', 'refs/worktree', 'refs/bisect', 'refs/rewritten').split()
    if own:
        return Refusal('refs of its own', some(own))
    # A submodule's history is in the git folder (a worktree's own one too), and stays there after `git submodule
    # deinit`: removing the clone or the worktree removes it.
    modules = guard.modules_refusal(path, git_dir)
    if modules is not None:
        return modules

    if kind == 'worktree':
        # Removing a worktree removes its own HEAD log and nothing else of git's: branches, their logs and every
        # other ref stay in the clone. So only a commit that nothing but this HEAD log names would be lost.
        mine = reflog(path, 'HEAD')
        stays = ['--branches', '--tags', '--remotes', *reflog(path, '--branches')]
        alone = lonely(path, mine, stays) if mine else ''
        if alone:  # reflog
            return Refusal('commits only in its reflog', f'{alone[:10]} (made with no branch checked out)')
    else:
        if git_code(path, 'rev-parse', '-q', '--verify', 'refs/stash') == 0:
            return Refusal('stash entries', 'git stash list')
        # Removing a clone removes every branch, tag and log in it. GitHub has a commit when a branch or tag there
        # holds it right now (`git ls-remote`) or a pull request does; the clone's own remote-tracking refs may be
        # stale and do not count.
        local = [url for url in remote_urls(path, 'origin') if not elsewhere(url)]
        if local:  # origin here
            return Refusal('origin on this machine', f'{local[0]}: what it has does not count as on GitHub')
        refs = remote_refs(path, 'origin', '--heads', '--tags')
        has = sorted(here | present(path, refs.values()))
        alone = lonely(path, ['--all', '--reflog'], has)
        if alone:  # clone
            return Refusal('commits GitHub does not have', f'{alone[:10]} (git log --all --reflog)')
        # A tag is more than its commit: an annotated tag has its own message and signature.
        missing = sorted(ref[len('refs/tags/'):] for ref, oid in local_tags(path).items() if refs.get(ref) != oid)
        if missing:
            return Refusal('tags GitHub does not have', some(missing))

    look = guard.examine(cfg, facts, path, kind)
    out['look'] = look
    if look.refusal is not None:
        return look.refusal
    # Somebody may still use it although its pull request merged: an agent's shell between two commands holds no
    # file open. Any change to its files (build output too) or to its own git state says it was used.
    used, what = last_used(path, git_dir, look)
    age = facts.now - used
    if age < cfg.used_hours * 3600:
        return Refusal(f'used under {cfg.used_hours} h ago', f'{what} changed {int(max(age, 0) // 3600)} h ago')
    return None


def last_used(path, git_dir, look):
    """(time, what) of the newest sign of use: a change to anything in the folder (the guard's look, which walks
    all of it, a clone's git folder too), to the git files a command in it writes (USE_MARKS), or the newest time
    written in its own HEAD reflog. The reflog file's own change time does not count: `git gc` rewrites it.
    Raises Unreadable when a reflog line cannot be read."""
    used, what = look.newest, 'a file in it'
    for name in USE_MARKS:
        try:
            when = guard.changed(os.lstat(os.path.join(git_dir, name)))
        except FileNotFoundError:
            continue                    # most are there only after the command that writes them
        except OSError as ex:
            raise Unreadable(f'{name} in its git folder: {ex.strerror}') from None
        if when > used:
            used, what = when, f'git {name}'
    when = reflog_newest(os.path.join(git_dir, 'logs', 'HEAD'))
    if when > used:
        used, what = when, 'its HEAD reflog'
    return used, what


def reflog_newest(path):
    """The newest time a reflog file names; 0 when there is no such file."""
    try:
        with open(path, 'rb') as f:
            lines = f.read().split(b'\n')
    except FileNotFoundError:
        return 0.0
    except OSError as ex:
        raise Unreadable(f'{path}: {ex.strerror}') from None
    newest = 0.0
    for number, line in enumerate(lines, 1):
        found = REFLOG_LINE.fullmatch(line)
        if found:
            newest = max(newest, float(found.group(1)))
        elif line or number < len(lines):  # the text ends with a newline: only the last piece may be empty
            raise Unreadable(f'line {number} of the reflog {path} is no reflog line')
    return newest


def tree_of(repo, path):
    """'worktree' or 'clone' when `path` is one of the listed repository (by its origin URL), the folders this rule
    looks at; None for anything else. Raises Unreadable when it cannot tell. The disk report's worktrees class.
    An orphaned worktree in the repository's folder counts as one (tree_answer keeps it under that name)."""
    kind = tree_kind(path)
    if kind == 'worktree' and orphaned_worktree(path) is not None:
        return kind
    return kind if kind is not None and same_repo(origin_url(path), repo.github) else None


def tree_answer(cfg, facts, repo, path):
    """(kind, refusal, why, look) for one entry of a repository's folder; None when it is not a worktree or clone
    of that repository, and so not this rule's."""
    try:
        kind = tree_kind(path)
    except Unreadable as ex:
        return 'folder', Refusal('could not read', str(ex)), '', None
    if kind is None:
        return None
    gone = orphaned_worktree(path) if kind == 'worktree' else None
    if gone is not None:            # whose it is cannot be read: it is in this repository's folder, and kept
        return kind, Refusal('orphaned worktree', f'its git folder {gone} is gone (the main repository was removed)'), '', None
    try:
        url = origin_url(path)
    except Unreadable as ex:
        return kind, Refusal('could not read', f'git: {ex}'), '', None  # whose it is
    if not same_repo(url, repo.github):
        return None
    try:
        prs = facts.prs(repo.github)
    except Unreadable as ex:
        return kind, Refusal('could not read', str(ex)), '', None  # GitHub
    out = {}
    try:
        refusal = tree_refusal(cfg, facts, repo, path, kind, prs, out)
    except Unreadable as ex:
        refusal = Refusal('could not read', f'git: {ex}')
    return kind, refusal, out.get('why', ''), out.get('look')


def plan_worktrees(cfg, facts, plan, measure):
    for repo in cfg.repos:
        names, problem = _entries(repo.root)
        if problem:
            plan.kept.append(Kept(repo.root, 'worktrees', problem.code, problem.detail))
        for name in names or ():
            path = os.path.join(repo.root, name)
            answer = tree_answer(cfg, facts, repo, path)
            if answer is None:
                continue
            kind, refusal, why, look = answer
            if refusal is not None:
                plan.kept.append(Kept(path, 'worktrees', refusal.code, refusal.detail, guard.measure(path) if measure else None))
            else:
                plan.items.append(Item(kind, path, 'worktrees', why, look.size, look))
            if kind == 'clone' and repo.merged_branches and refusal is not None:   # a clone that goes takes its branches
                plan_branches(cfg, facts, repo, path, plan)


def recheck_tree(cfg, facts, item):
    for repo in cfg.repos:
        if repo.root == os.path.dirname(item.path):
            answer = tree_answer(cfg, facts, repo, item.path)
            if answer is None or answer[0] != item.kind:
                return Refusal('not a candidate', f'no longer a {item.kind} of {repo.github}')
            item.look = answer[3]
            return answer[1]
    return Refusal('not a candidate', 'no repository in the machine file has this folder')


def delete_worktree(item):
    """Git removes it, without --force: git refuses on its own when a file changed after the last check, and then
    raises Untouched (nothing removed). The local branch stays in the clone. The folder must still be the one the
    last check judged, and git itself removes a worktree only when its .git file points back at its own entry in
    the clone, so a link or another folder swapped in is refused."""
    _still(item)
    try:
        common = git(item.path, 'rev-parse', '--path-format=absolute', '--git-common-dir').strip()
    except Unreadable as ex:
        raise Untouched(f'could not read: {ex}: nothing removed') from None
    _still(item)
    remove_worktree(common, item.path)


def delete_clone(item):
    """The working files first and .git last, so a clone that is only half gone still has its history. The engine
    notes the delete before it starts; a later run names a half-gone clone "half deleted" (engine.py)."""
    remove_tree(item.path, item.look, last='.git')


# --- merged branches (the worktrees rule, `mergedBranches`) --------------------------------------------------------
# `git worktree remove` leaves the worktree's branch in its clone. With `mergedBranches` on, a later run deletes such
# a branch of a clone directly in the root once it is finished the way a worktree is, and once nothing it or its
# reflog names would be lost: every one of those commits is on GitHub right now.
def branch_label(clone, branch):
    return f'branch {branch} in {clone}'


def branch_busy(git_dir, branch):
    """True when a worktree of the clone rebases or bisects the branch: it is then detached, and `git worktree list`
    does not name the branch (git's own `branch -d` refuses it too). Raises Unreadable when a file cannot be read."""
    folders = [git_dir]
    try:
        folders += sorted(os.path.join(git_dir, 'worktrees', name) for name in os.listdir(os.path.join(git_dir, 'worktrees')))
    except (FileNotFoundError, NotADirectoryError):
        pass
    except OSError as ex:
        raise Unreadable(f'{ex.filename}: {ex.strerror}') from None
    for folder in folders:
        for name in ('rebase-merge/head-name', 'rebase-apply/head-name', 'BISECT_START'):
            try:
                with open(os.path.join(folder, name), errors='surrogateescape') as f:
                    named = f.read().strip()
            except (FileNotFoundError, NotADirectoryError):
                continue
            except OSError as ex:
                raise Unreadable(f'{ex.filename}: {ex.strerror}') from None
            if named in ('refs/heads/' + branch, branch):
                return True
    return False


def branch_used(git_dir, branch):
    """(time, what) of the newest sign the branch was used: the newest time its reflog names, or a change to its
    loose ref file (a packed branch has none); 0 when neither is there."""
    used, what = reflog_newest(os.path.join(git_dir, 'logs', 'refs', 'heads', *branch.split('/'))), 'its reflog'
    try:
        when = guard.changed(os.lstat(os.path.join(git_dir, 'refs', 'heads', *branch.split('/'))))
    except (FileNotFoundError, NotADirectoryError):
        when = 0.0
    except OSError as ex:
        raise Unreadable(f'its ref file: {ex.strerror}') from None
    return (when, 'its ref') if when > used else (used, what)


def clone_keeps(cfg, clone):
    """Why every branch of a clone stays whatever else holds: the clone is or is in a must-keep path or name, is
    reached through a link, or has a KEEP file at its top. None when nothing does."""
    if must_keep_match(cfg.must_keep, clone) is not None or any(guard.kept_name(cfg, p) for p in clone.split('/') if p):
        return Refusal('must keep', clone)
    if os.path.realpath(clone) != clone:
        return Refusal('symbolic link', f'{clone} is reached through a link')
    keep = os.path.join(clone, guard.KEEP_FILE)
    if os.path.lexists(keep):
        return Refusal('KEEP file', guard._keep_reason(keep))
    return None


def branch_refusal(cfg, facts, clone, git_dir, branch, oid, prs, default, on_github, main):
    """(refusal, why) for one branch: why it stays, or None and the pull request (or default branch) it is finished by.
    `on_github`: the commits this clone has that GitHub holds right now (its refs, its tags, the listed pull requests'
    heads); `main`: the commit of GitHub's default branch. Raises Unreadable when git cannot answer."""
    open_rows = [row for row in prs['open'] if row['headRefName'] == branch]
    if open_rows:
        return Refusal('open PR', numbers(open_rows)), ''
    if branch == default:  # the default branch: a branch
        return Refusal('base branch', f'{branch} is the default branch on GitHub'), ''
    bases = [row for row in prs['open'] + prs['merged'] if row['baseRefName'] == branch]
    if bases:
        return Refusal('base branch', f'pull requests merge into {branch}: {numbers(bases)}'), ''
    finished = [row for row in prs['merged'] if row['headRefName'] == branch and row['isCrossRepository'] is False]
    if not finished:
        return Refusal('no merged PR', f'no merged pull request of this repository has the branch {branch}'), ''
    holding = [row for row in finished if row['headRefOid'] == oid or (
        row['headRefOid'] in on_github and git_code(clone, 'merge-base', '--is-ancestor', oid, row['headRefOid']) == 0)]
    where = 'its tip is in the merged head'
    if not holding and main in on_github and git_code(clone, 'merge-base', '--is-ancestor', oid, main) == 0:
        holding, where = finished, f'its tip is in {default} on GitHub'     # a squash or rebase merge
    if not holding:
        return Refusal('commits not in the merged PR', f'{oid[:10]} is in neither {numbers(finished)} nor {default} on GitHub'), ''
    times = [merged_seconds(row) for row in holding]
    if None in times:
        return Refusal('could not read', f'GitHub (no merge time for {numbers(holding)})'), ''
    since = facts.now - max(times)
    if since < cfg.merged_hours * 3600:
        return Refusal(f'merged under {cfg.merged_hours} h ago', f'{numbers(holding)} merged {int(max(since, 0) // 3600)} h ago'), ''
    # Deleting a branch deletes its reflog: a commit amended or reset away is there and maybe nowhere else.
    lost = lonely(clone, [oid, *reflog(clone, 'refs/heads/' + branch)], sorted(on_github))
    if lost:
        return Refusal('commits GitHub does not have', f'{lost[:10]} (in it or its reflog: git log -g {branch})'), ''
    used, what = branch_used(git_dir, branch)
    if facts.now - used < cfg.used_hours * 3600:
        return Refusal(f'used under {cfg.used_hours} h ago', f'{what} changed {int(max(facts.now - used, 0) // 3600)} h ago'), ''
    return None, (f'{numbers(holding)} merged {days_text(int(since // DAY))} ago, {where}, every commit it and its '
                  'reflog name is on GitHub')


def branch_answers(cfg, facts, repo, clone, only=None):
    """[(branch, oid, refusal, why)] for each local branch of a clone of the repository that a `branches` pattern
    names and no worktree has checked out, rebases or bisects (such a branch is its worktree's: the worktree goes
    first, the branch in a later run). `only`: that branch alone. Raises Unreadable when git or GitHub cannot answer
    for the clone."""
    git_dir = os.path.join(clone, '.git')
    branches = {name: oid for name, oid in local_branches(clone).items()
                if matches(name, repo.branches) and (only is None or name == only)}
    if not branches:
        return []
    out = {entry['branch'][len('refs/heads/'):] for entry in worktree_list(clone)
           if entry.get('branch', '').startswith('refs/heads/')}
    branches = {name: oid for name, oid in branches.items() if name not in out and not branch_busy(git_dir, name)}
    if not branches:
        return []
    refusal = clone_keeps(cfg, clone)
    here_urls = [url for url in remote_urls(clone, 'origin') if not elsewhere(url)] if refusal is None else []
    if here_urls:
        refusal = Refusal('origin on this machine', f'{here_urls[0]}: what it has does not count as on GitHub')
    if refusal is not None:
        return [(name, oid, refusal, '') for name, oid in sorted(branches.items())]
    prs = facts.prs(repo.github)
    default = default_branch(clone)
    refs = remote_refs(clone, 'origin', '--heads', '--tags')
    on_github = present(clone, [row['headRefOid'] for row in prs['merged'] + prs['open']] + list(refs.values()))
    main = refs.get('refs/heads/' + default)
    aliases = {}
    for ref, target in symbolic_refs(clone).items():
        aliases.setdefault(target, []).append(ref)
    answers = []
    for name, oid in sorted(branches.items()):
        named = sorted(aliases.get('refs/heads/' + name, ()))
        if named:   # another name of it: deleting the branch would leave that name pointing at nothing
            answers.append((name, oid, Refusal('symbolic ref names it', f'{named[0]} points at it'), ''))
            continue
        try:
            refusal, why = branch_refusal(cfg, facts, clone, git_dir, name, oid, prs, default, on_github, main)
        except Unreadable as ex:
            refusal, why = Refusal('could not read', f'git: {ex}'), ''
        answers.append((name, oid, refusal, why))
    return answers


def branch_why(why, oid):
    """The log line of a branch delete names the commit it pointed at, as `git branch -d` does: one line undoes it
    (`git fetch origin <oid> && git branch <name> <oid>`; every commit is on GitHub)."""
    return f'{why} (was {oid})'


def plan_branches(cfg, facts, repo, clone, plan):
    try:
        answers = branch_answers(cfg, facts, repo, clone)
    except Unreadable as ex:
        plan.kept.append(Kept(f'branches in {clone}', 'worktrees', 'could not read', str(ex), 0))
        return
    for name, oid, refusal, why in answers:
        if refusal is not None:
            plan.kept.append(Kept(branch_label(clone, name), 'worktrees', refusal.code, refusal.detail, 0))
        else:
            plan.items.append(Item('branch', clone, 'worktrees', branch_why(why, oid), 0, branch=name, oid=oid))


def recheck_branch(cfg, facts, item):
    """The branch read again: the commit it points at now is the one the delete removes it at, and the one its log
    line names. Several repositories may share one folder: the one whose clone it is judges it."""
    for repo in cfg.repos:
        if repo.root == os.path.dirname(item.path) and repo.merged_branches:
            try:
                if tree_kind(item.path) != 'clone':
                    return Refusal('not a candidate', 'no longer a clone')
                if not same_repo(origin_url(item.path), repo.github):
                    continue
                answers = branch_answers(cfg, facts, repo, item.path, only=item.branch)
            except Unreadable as ex:
                return Refusal('could not read', str(ex))
            if not answers:
                return Refusal('not a candidate', 'it is gone, checked out, rebased or bisected, or no pattern names it')
            _, item.oid, refusal, why = answers[0]
            if refusal is None:
                item.why = branch_why(why, item.oid)
            return refusal
    return Refusal('not a candidate', 'no repository in the machine file that deletes merged branches in this folder '
                   'has this clone')


def delete_branch(item):
    remove_branch(item.path, item.branch, item.oid)


# --- all rules ----------------------------------------------------------------------------------------------------
# The rules, by the name each one writes on what it plans: the one list of them. The disk report (report.py) needs
# a class for each (config.CLASSES), and report.test.py fails for a rule here that has none.
RULES = {'worktrees': plan_worktrees, 'docker': plan_docker, 'tmp': plan_tmp, SCRATCH: plan_scratch,
         compress.RULE: compress.plan_compress}
# Kinds whose step never leaves a path half gone (compress.py: the original or its checked copy is always there; a
# branch: one git step on a ref, and its clone stays), so the journal of deletes in progress does not name them.
# WORDS: what the engine says of them (plan, run, done) and NOUNS what it counts them as.
UNJOURNALED = ('compress', 'part', 'branch')
WORDS = {'compress': ('would compress', 'compressing', 'compressed')}
NOUNS = {'compress': 'file', 'part': 'part file', 'branch': 'branch'}


def plan(cfg, facts, measure=False):
    """What every rule wants gone (items) and what it looked at and keeps (kept). `measure` also sizes the kept."""
    out = Plan()
    for planner in RULES.values():
        planner(cfg, facts, out, measure)
    return out


def recheck(cfg, facts, item):
    """Everything about one item, read again just before its delete: None when it may still go."""
    if item.kind == 'volume':
        return recheck_volume(cfg, facts, item)
    if item.kind in ('worktree', 'clone'):
        return recheck_tree(cfg, facts, item)
    if item.kind == 'branch':
        return recheck_branch(cfg, facts, item)
    if item.kind in ('compress', 'part'):
        return compress.recheck(cfg, facts, item)
    if item.kind == 'session':
        if not cfg.session_parent(os.path.dirname(item.path)):
            return Refusal('not a candidate', 'not a session folder of the agent-scratch root of the machine file')
        answer = scratch_answer(cfg, facts, item.path, getattr(item, 'started', None))
        if answer is None:
            return Refusal('not a candidate', 'it changed, or the rule no longer takes it')
        item.look = answer[1]
        return answer[1].refusal
    if os.path.dirname(item.path) != cfg.tmp_root:
        return Refusal('not a candidate', 'not in the temp folder of the machine file')
    answer = tmp_answer(cfg, facts, item.path)
    if answer is None:
        return Refusal('not a candidate', 'it changed, or the rule no longer takes it')
    item.look = answer[1]
    return answer[1].refusal


def delete(item):
    {'path': delete_path, 'session': delete_path, 'worktree': delete_worktree, 'clone': delete_clone, 'branch': delete_branch,
     'volume': lambda i: remove_volume(i.path), 'compress': compress.compress, 'part': compress.remove_part}[item.kind](item)
