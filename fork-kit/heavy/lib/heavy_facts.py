"""heavy facts: may this machine take one more piece of work? The capacity rule's answer as a free/v1 object.

  heavy facts [--kind KIND] [--mb MB] [--fresh]

Prints one free/v1 object (fork-kit/common/FREE.md) and exits 0 free, 75 busy, 1 when it cannot answer. --kind asks
for one piece of KIND: a kind of capacity-rule.json, a heavy job kind the rule knows as job-KIND (build: job-build),
else a heavy job of a kind the rule does not know (the rule's unknownKinds.job: never cheap). --mb alone asks whether
MB fit the free budget at level "ok"; with --kind it is the piece's declared MB and must fit too. Neither: the rule's
unknownKinds.job. "fits" says for every kind how many more pieces of it the rule admits now, one after another
(capacityFill up to the group's ceiling).

The ledger comes from the kernel only, so the answer stands with T3 down or not installed. Memory, swap, PSI, cores
and container limits come from lib/capacity_readings.py (the shared reader's port: the same answers as
capacityReadings.ts). Pieces: heavy's admitted jobs (its live queue tickets; every ticket is a job, of the rule's
kind for its name or else unknownKinds.job; demand from the job's scope) and T3's agent scopes (any scope right below
an `app-t3code*` slice, a `t3code-*.scope` elsewhere with a note; a scope has no kind label, so it counts as
unknownKinds.other: a writer), plus the
other users' heavy jobs and agent scopes read from their user managers' cgroups (unknownKinds.job and writers).
Full tier: cgroup v2 and this user's systemd user manager. Machine tier: cgroup v2 and user.slice, but no user
manager of this user's (a CI runner's user: no lingering, no login session): it has no agent or heavy job scopes of
its own, every user's are counted as above, and so are every person's reservations (other_reserve_dirs: their
~/.config/t3-capacity/reserve.d; one that cannot be read, or not within HOME_SECONDS, #998, makes it basic), so it
gets the same answer as a caller with a user manager (a note and the reason say "machine-wide numbers"). Basic tier
(no cgroup v2, or neither a user manager nor user.slice): agents and other users' work cannot be counted, so the
answer is never free (a note says why). A signal that is
there but unreadable is "elevated": then only the rule's floors admit, never the budget; facts that cannot be read
admit nothing.

Reservations: lines `<MB or kind> <folder> [until=YYYY-MM-DD] [ttl=DUR] [idle=DUR]` (#861: reservation_files,
release_idle) in *.conf files of $T3_CAPACITY_RESERVE_DIR, else
~/.config/t3-capacity/reserve.d. A folder matches itself and what is below it, never a sibling that shares its
first letters (`/x/app` is not `/x/app-x`); its real path (links resolved) matches too. Each holds MB less what
its user's processes working in the folder use (RssAnon + RssShmem + VmSwap), and less what the agents and heavy
jobs with a process working in it still owe the rule (their cost less their demand: that room is held as their debt
already, so it is counted once, #923). A process counts toward one reservation only (the deepest folder holding it,
then the first by name), and a piece's debt is split over its processes by their use, so no debt is taken off two
reservations (#945). It does not hold back a caller whose working folder is in it; past its until date or ttl, or
broken, it holds nothing (a note says so). Every person's readable reservations count in the full tier too (#800).
The machine file's heavy.capacity.reserveDirs (#844) names a folder others can read for a person whose home they
cannot: it is read for that person in place of their home's, and by that person as their own. Its holds
(held_cgroups) are cgroups outside the user managers, or a slice in a user's own manager (#949).

Readings are kept 5 seconds in /run/user/<uid>/heavy-facts-v<version>-<hash of the settings>.json, one file per
version and settings, so callers of one user with other settings (slice, queue, reservations folder) or another
kit version never read each other's file and each one's hold-down steps down on its own (#945) ($HEAVY_FACTS_CACHE
names one file instead; --fresh reads again; a user without /run/user/<uid>, as one with no user manager, uses
$TMPDIR or /tmp /heavy-facts-<uid>/, a folder only it can use; when that name is taken (someone else made it first)
or there is no temporary folder it can write, ~/.cache/heavy-facts-<host>-<machine id>/ in a home only it can change
and that no one else could swap (no machine id: -boot-<boot id>, removed a day after its last write, #998); none of
these is no cache, #951). The folder is opened once
(O_DIRECTORY|O_NOFOLLOW) and every file is reached through that handle, so a folder swapped for a link after its
check is never followed (#951). The
file is written whole through a rename; a cache that is old, from the future, broken, of another version, of other
settings or not this user's own is read again. The pressure level (and the swap free its trend needs; a state
without it waits one more read before it can step down) carries from one reading to the next (the rule's
hysteresis) while the last one is at most 5 minutes old; without a cache folder, with a cache file that is there but
cannot be used, or with no file of its own yet but another caller's written in the last 5 minutes beside it (another
version after an update, other settings), nothing can carry, so the level starts "elevated" (it never drops straight
to ok). Cache files of other settings or versions older than 5 minutes are removed. Learned
job costs: heavy's peaks (lib/heavy_queue.py reservation(); missing or broken: start). A job owes its kind's cost, or
heavy's own reservation for it when that is more (its ticket's mb is the piece's declared size, a floor of its cost
in the rule, #800).
"""
import datetime, errno, fcntl, fnmatch, hashlib, json, math, os, pwd, re, select, signal, stat, subprocess, sys, time

CACHE_SECONDS, CARRY_SECONDS, ANSWER_SECONDS, STALE_TMP_SECONDS = 5, 300, 5, 60
EXIT_FREE, EXIT_BUSY, EXIT_ERROR = 0, 75, 1
CACHE_VERSION = 5                               # 3: reservations owedMb (#923); 4: full-tier pieceCaps (#951); 5: cpu (#1047)
AGENT_SCOPE_RE = re.compile(r'^t3code-[A-Za-z0-9_.:\\-]+\.scope\Z')
SCOPE_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}\.scope\Z')
TICKET_RE = re.compile(r'^t-[0-9]{1,15}-[0-9]{1,10}\.json\Z')
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,59}\Z')
USER_RE = re.compile(r'^user-([0-9]{1,10})\.slice\Z')
HOLD_LABEL_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}\Z')
CG_PART_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}\Z')
MANAGER_HOLD_RE = re.compile(r'^user\.slice/user-([0-9]{1,10})\.slice/user@([0-9]{1,10})\.service/[^/]')
CACHE_FILE_RE = re.compile(r'^heavy-facts(-v[0-9]{1,4}-[0-9a-f]{16})?\.json\Z')   # an older kit's: heavy-facts.json
CACHE_KEYS = {'v', 'key', 'atMs', 'tier', 'facts', 'boxPsi', 'reservations', 'learned', 'carry', 'notes', 'load1',
              'cpu', 'queue', 'pressure'}
USERS = '/sys/fs/cgroup/user.slice'
MOUNTS_LIMIT = 1 << 22                          # the mount table read for automounts (#996: longer is a note)
RUN_USER = '/run/user'                          # the cache's folder: /run/user/<uid>
TMP_ROOT = None                                 # without it: <TMP_ROOT>/heavy-facts-<uid> (None: temp_root())
TMP_DIRS = ('/tmp',)                            # temp_root() after $TMPDIR


def test_env(name):
    """A test-only setting (#800): read only while HEAVY_TESTING=1 is set too, so one that leaks into an agent's or a
    service's environment (HEAVY_FACTS_ROOT, AGENT_SLOTS_ALIVE, T3_CAPACITY_HEAVY, AGENT_SLOTS_HEAVY) changes
    nothing a real caller sees."""
    return os.environ.get(name) if os.environ.get('HEAVY_TESTING') == '1' else None


MIB = 1024 * 1024
ROOT = test_env('HEAVY_FACTS_ROOT') or ''       # tests: /proc and /sys are read under this folder
BASIC_WHY = 'basic tier: no cgroup v2, or neither a systemd user manager for this user nor user.slice, so agents and ' \
            'other users\' work cannot be counted: never free here'
MACHINE_WHY = 'machine tier: no systemd user manager for this user, so it has no agents or heavy jobs of its own; ' \
              'judged on the machine-wide numbers (memory, swap, pressure, holds, every person\'s reservations, every ' \
              'user\'s heavy jobs and agents)'


def _read(path, limit=1 << 16):
    with open(ROOT + path, 'rb') as f:
        return f.read(limit).decode('ascii', 'replace')


def _read_names(path, limit):
    """A file of path names (the mount table, /etc/passwd) as text, each byte kept as the file system names it
    (os.fsdecode: a non-ASCII name matches the same name from anywhere else), and whether it is longer than limit
    (#996 item 4)."""
    with open(ROOT + path, 'rb') as f:
        raw = f.read(limit + 1)
    return os.fsdecode(raw[:limit]), len(raw) > limit


def load1():
    try:
        return float(_read('/proc/loadavg').split()[0])
    except (OSError, ValueError, IndexError):
        return None


def cpu_reading(host):
    """The CPU readings of the brake (#1047): {'pressureSome10': the "some avg10" CPU pressure in percent (the share
    of time some runnable task waited for a CPU) or None, 'hostCores': the CPUs the kernel has online (what
    /proc/loadavg is a load of) or None}. In a container with a CPU limit (the cgroup root's cpu.max), its own
    cpu.pressure, not the host's /proc/pressure/cpu: the host can be busy while the container's share is free. Only
    the some line: a kernel before 5.13 writes no full line for the CPU. None: missing, turned off or unreadable."""
    capped = cr.is_num(rd._container_cpus(host, 'v2'))
    read = rd._read_text(host, rd.CGROUP + '/cpu.pressure' if capped else '/proc/pressure/cpu')
    some = rd._psi_line('some', read['text']) if 'text' in read else None
    online = rd._read_text(host, '/sys/devices/system/cpu/online')
    cores = 0
    for part in online.get('text', '').strip().split(',') if 'text' in online else ():
        lo, _, hi = part.partition('-')
        cores = cores + int(hi or lo) - int(lo) + 1 if lo.isdigit() and (hi or lo).isdigit() else None
        if cores is None:
            break
    return {'pressureSome10': float(some) if some is not None and float(some) <= 100 else None,
            'hostCores': cores or None}


def cpu_busy(rule, cpu, load):
    """Why the CPU is too busy for one more job (the rule's optional `cpu`, #1047), or None. Memory alone missed a
    machine whose CPU was taken by work outside heavy (containers its user started). CPU pressure decides where it
    is read (at or above pressureSomeAvg10Pct); the 1-minute load only where it is not (at or above
    loadPerCorePercent of the host's online CPUs: the load is the host's, and it counts tasks waiting for I/O too, so
    it never overrides a pressure reading). A missing reading brakes nothing: the memory rule still decides."""
    c, cpu = rule.get('cpu'), cpu or {}
    if not c:
        return None
    psi, cores = cpu.get('pressureSome10'), cpu.get('hostCores')
    if psi is not None:
        return f'cpu pressure {psi:g}% (some avg10; the brake is {c["pressureSomeAvg10Pct"]:g}%)' \
            if psi >= c['pressureSomeAvg10Pct'] else None
    if cr.is_num(load) and cr.is_num(cores) and cores > 0 and load * 100 >= cores * c['loadPerCorePercent']:
        return f'cpu load {load:g} on {cores:g} cores (no CPU pressure reading; the brake is ' \
            f'{c["loadPerCorePercent"]}% of the cores)'
    return None


def cg(path):
    """A /sys/fs/cgroup/... folder as the shared reader names it (below the cgroup root)."""
    return path[len(rd.CGROUP):]


def slice_path(name):
    """systemd's folder of a slice: a-b.slice is a.slice/a-b.slice."""
    parts = name[:-len('.slice')].split('-')
    return '/'.join('-'.join(parts[:i + 1]) + '.slice' for i in range(len(parts)))


def agent_scopes(manager):
    """(T3's agent scope folders, the outermost app-t3code slice folder or None, notes) under the user manager's
    cgroup. Every scope right below an `app-t3code*` slice is an agent, whatever its name (an escaped label, `\x2d`,
    too); a `t3code-*.scope` anywhere else below the manager (T3 run under a slice of another name) counts as one too,
    with a note, so a scope outside the usual place is never left out (#800)."""
    scopes, box, notes = [], None, []
    for dirpath, dirs, _files in os.walk(ROOT + manager):
        rel = dirpath[len(ROOT):]
        name = os.path.basename(rel)
        parent = os.path.basename(os.path.dirname(rel))
        if name.startswith('app-t3code') and name.endswith('.slice') and (box is None or len(rel) < len(box)):
            box = rel
        if name.endswith('.scope') and rel != manager and (
                parent.startswith('app-t3code') and parent.endswith('.slice') or AGENT_SCOPE_RE.match(name)):
            scopes.append(rel)
            if not (parent.startswith('app-t3code') and parent.endswith('.slice')):
                notes.append(f'agent scope {name[:80]} is not below an app-t3code slice: counted as an agent')
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.endswith(('.slice', '.scope', '.service'))]
    return sorted(scopes), box, notes


def tickets(queue_dir):
    """heavy's live tickets, read only: a ticket no heavy holds is skipped (heavy itself removes it), a live one
    that cannot be read is an admitted job of an unknown kind."""
    out = []
    try:
        names = sorted(n for n in os.listdir(queue_dir) if TICKET_RE.match(n))
    except OSError:
        return out
    for name in names:
        try:
            fd = os.open(os.path.join(queue_dir, name), os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        except OSError:
            continue
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
                continue                            # nobody holds it: that heavy is gone
            except OSError as e:
                if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN):
                    continue
            try:
                data = json.loads(os.read(fd, 65536).decode('utf-8'))
                if not (isinstance(data, dict) and isinstance(data.get('kind'), str)
                        and data.get('state') in ('waiting', 'admitted')):
                    raise ValueError
            except ValueError:
                # #981: it names no scope, so read_machine counts its job's scope again as a plain job (no ticket
                # names it): counted twice, the safe side; it cannot be told which scope is its own
                data = {'kind': '', 'state': 'admitted'}
            out.append(data)
        finally:
            os.close(fd)
    return out


def ask_kind(rule, kind):
    """The rule's kind for an asked name: itself, job-NAME when the rule has that, else a heavy job of a kind the rule
    does not know (unknownKinds.job): `heavy facts` is heavy's verb, so an unknown name is a job, never a writer."""
    if kind in rule['kinds']:
        return kind
    return 'job-' + kind if 'job-' + kind in rule['kinds'] else rule['unknownKinds']['job']


def ticket_kind(rule, kind):
    """A heavy ticket is always a job: the rule's job kind for its name (NAME or job-NAME), else unknownKinds.job."""
    for k in (kind, 'job-' + kind):
        if k and k in rule['kinds'] and rule['kinds'][k]['group'] == 'job':
            return k
    return rule['unknownKinds']['job']


def folder_prefixes(folder):
    """A reservation's folder as prefixes that end in exactly one '/': as written and with its links resolved."""
    out = []
    for f in (folder, os.path.realpath(folder)):
        p = f.rstrip('/') + '/'
        if p not in out:
            out.append(p)
    return out


def under(cwd, prefixes):
    """Is the working folder cwd the reservation's folder or below it (never a sibling sharing its first letters)?"""
    c = cwd.rstrip('/') + '/'
    return any(c.startswith(p) for p in prefixes)


DURATION_RE = re.compile(r'^([0-9]{1,6})([smhd])\Z')


def duration(text):
    """Seconds of `30m`, `2h`, `1d`, `90s`; ValueError for anything else."""
    m = DURATION_RE.match(text)
    if not m:
        raise ValueError(f'{text!r} is not a duration like 30m, 2h, 1d')
    return int(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[m.group(2)]


def reservation_files(cfg_dir, now, owner=None, size=None):
    """The reservations in one reserve.d: ([{name, prefixes, mb, useMb, owedMb, uid, idleAt, procs}], notes, blind).
    owner: None for the caller's own folder (missing or unreadable files hold nothing, as before); (name, uid) for
    another user's (the machine tier): a folder or file that is there but cannot be read is 'blind' (never free),
    and only plain files are read (no FIFO can stop the reading). A line is `<MB or kind> <folder> [until=DATE]
    [ttl=DUR] [idle=DUR]` (#861): a kind is sized by size(kind), the rule's cost for it (learned from heavy's peaks);
    ttl ends it DUR after its file was last written (touch it to renew); idle (see release_idle) lets it hold
    nothing once nobody works in its folder and its file was not written for DUR. An unknown word is ignored with a
    note, never read as a smaller size."""
    today = now.date()
    out, notes, blind = [], [], []
    tag = f'{owner[0]}:' if owner else ''
    try:
        files = sorted(f for f in os.listdir(cfg_dir) if f.endswith('.conf'))
    except (FileNotFoundError, NotADirectoryError):
        return out, notes, blind
    except OSError as e:
        if owner:
            blind.append(f'reservations of user {owner[0]} cannot be read ({e.strerror or e}): never free here')
        return out, notes, blind
    for f in files:
        try:
            fd = os.open(os.path.join(cfg_dir, f), os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK |
                         (os.O_NOFOLLOW if owner else 0))
            try:
                st = os.fstat(fd)
                if owner and not stat.S_ISREG(st.st_mode):
                    raise OSError(errno.EINVAL, 'not a plain file')
                data = os.read(fd, 1 << 16)
                if len(data) == 1 << 16:            # longer than that: its whole lines only
                    data = data[:data.rfind(b'\n') + 1]
                lines = data.decode('utf-8').splitlines()
            finally:
                os.close(fd)
        except (OSError, UnicodeDecodeError) as e:
            if owner:
                blind.append(f'reservation {tag}{f} cannot be read ({e.__class__.__name__}): never free here')
            else:
                notes.append(f'reservation {f} cannot be read ({e.__class__.__name__}): holds nothing')
            continue
        for n, line in enumerate(lines, 1):
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            until = [w[6:] for w in words[2:] if w.startswith('until=')]
            try:
                if not (len(words) >= 2 and words[1].startswith('/')
                        and (words[0].isdigit() or size is not None and KIND_RE.match(words[0]))):
                    raise ValueError('not "<MB or kind> <absolute folder>"')
                if until and datetime.date.fromisoformat(until[0]) < today:
                    notes.append(f'reservation {tag}{f}:{n} ended {until[0]}: holds nothing')
                    continue
                mb = int(words[0]) if words[0].isdigit() else size(words[0])
            except ValueError as e:
                notes.append(f'reservation {tag}{f}:{n} ignored ({e})')
                continue
            # ttl= and idle= only ever let a line hold less once they are read whole: a bad one is a note and the
            # line holds as if it were not there (never less than before #861)
            ends = {}
            for w in words[2:]:
                key, _eq, value = w.partition('=')
                if key in ('ttl', 'idle'):
                    try:
                        ends[key] = st.st_mtime + duration(value)
                    except ValueError as e:
                        notes.append(f'reservation {tag}{f}:{n}: {key}= not read ({e}): it holds as without it')
            if ends.get('ttl') is not None and ends['ttl'] < now.timestamp():
                notes.append(f'reservation {tag}{f}:{n} ended: its ttl= since its file was last written is over; '
                             f'holds nothing (touch the file to renew it)')
                continue
            idle_at = ends.get('idle')
            out.append({'name': tag + f[:-5], 'prefixes': folder_prefixes(words[1]), 'mb': mb,
                        'useMb': 0, 'owedMb': 0, 'uid': owner[1] if owner else os.getuid(), 'idleAt': idle_at,
                        'procs': 0})
    return out, notes, blind


def login_range():
    """The user ids of people (UID_MIN..UID_MAX of /etc/login.defs; 1000..60000 when not set there)."""
    lo, hi = 1000, 60000
    try:
        for line in _read('/etc/login.defs').splitlines():
            words = line.split()
            if len(words) >= 2 and words[0] in ('UID_MIN', 'UID_MAX') and words[1].isdigit():
                lo, hi = (int(words[1]), hi) if words[0] == 'UID_MIN' else (lo, int(words[1]))
    except OSError:
        pass
    return lo, hi


def autofs_points():
    """(the mount points and their fstype from this machine's mount table, for on_autofs(); a note or None). [] when
    it cannot be read or is longer than MOUNTS_LIMIT (#996 item 4: a home whose mount line was cut off read as not
    mounted; the note names it): then every home is read, as before #951. A point's octal escapes (\\040 for a space,
    \\303\\274 for ü) are undone byte by byte and the bytes decoded as the file system names them, as the homes
    from /etc/passwd are, so a non-ASCII home matches its own mount."""
    try:
        with open(ROOT + '/proc/self/mountinfo', 'rb') as f:
            raw = f.read(MOUNTS_LIMIT + 1)
    except OSError:
        return [], None
    if len(raw) > MOUNTS_LIMIT:
        return [], f'the mount table is longer than {MOUNTS_LIMIT >> 20} MiB: every home is read (autofs ones too)'
    out = []
    for line in raw.splitlines():
        left, sep, right = line.partition(b' - ')
        f = left.split()
        if sep and len(f) >= 5:
            point = os.fsdecode(re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m.group(1), 8) & 0xff]), f[4]))
            out.append((point, os.fsdecode(right.split()[0]) if right.split() else ''))
    return out, None


def on_autofs(home, mounts):
    """True when the deepest mount covering `home` is an automount trigger: reading it would mount it (#951, an autofs
    home that is mounted already is its own deeper mount and is read)."""
    deepest = None
    for point, fstype in mounts:
        if home == point or home.startswith(point.rstrip('/') + '/'):
            if deepest is None or len(point) >= len(deepest[0]):
                deepest = (point, fstype)
    return deepest is not None and deepest[1] == 'autofs'


def other_reserve_dirs(uid, listed=None):
    """For the machine tier (#844): every other person's reserve.d, ([(user name, uid, folder)], blind, notes).
    listed: {user name: folder} of the machine file's heavy.capacity.reserveDirs (#844): that folder is read for that
    person in place of their home's (a home closed to others then needs no opening), also for a person without a
    home or session here (uid -1 when the name is not known). The
    people are the /etc/passwd entries in login_range() and every user.slice user in it; a user with a session but no
    home that can be found is 'blind'. Root and system users are not read (their homes are closed to others). A
    person without a session whose home is behind an automount trigger is not read (#951: each read would mount
    every home); one with a session is, so a reservation counts again from their next login, as with NSS users."""
    lo, hi = login_range()
    homes, blind = {}, []
    try:
        for line in _read_names('/etc/passwd', 1 << 22)[0].splitlines():
            f = line.split(':')
            if len(f) >= 7 and f[2].isdigit() and lo <= int(f[2]) <= hi and int(f[2]) != uid:
                homes.setdefault(int(f[2]), (f[0], f[5]))
    except OSError:
        pass
    notes = []
    try:
        names = os.listdir(ROOT + USERS)
    except OSError as e:
        # #996 item 6: sessions that cannot be known are not "no session": every home is read, as before #951
        names, sessions = [], None
        notes.append(f'{USERS} cannot be listed ({e.strerror or e}): sessions cannot be known, every home is read '
                     f'(autofs ones too)')
    else:
        sessions = {int(m.group(1)) for m in map(USER_RE.match, names) if m}
    for name in sorted(names):
        m = USER_RE.match(name)
        u = int(m.group(1)) if m else None
        if u is None or u == uid or not lo <= u <= hi or u in homes:
            continue
        try:
            pw = pwd.getpwuid(u)
            homes[u] = (pw.pw_name, pw.pw_dir)
        except KeyError:
            blind.append(f'user {u} has a session but no home that can be found: its reservations cannot be read')
    listed = dict(listed or {})
    mounts, note = autofs_points() if sessions is not None else ([], None)
    notes += [note] if note else []
    out, skipped = [], []
    for u, (n, h) in sorted(homes.items()):
        if n in listed:
            out.append((n, u, ROOT + listed.pop(n)))
            continue
        if not os.path.isabs(h) or h == '/':
            continue
        if mounts and u not in sessions and on_autofs(os.path.normpath(h), mounts):
            skipped.append(n)
            continue
        out.append((n, u, os.path.join(ROOT + h, '.config', 't3-capacity', 'reserve.d')))
    if skipped:
        notes.append(f'reservations not read for {len(skipped)} people without a session whose home is not mounted '
                     f'(autofs): {", ".join(skipped[:5])}{" ..." if len(skipped) > 5 else ""}')
    for n, folder in sorted(listed.items()):        # named in the machine file, not a person read above
        try:
            u = pwd.getpwnam(n).pw_uid
        except KeyError:
            u = -1
            notes.append(f'reserveDirs: user {n} is not known here: its reservations hold for everyone')
        if u != uid:
            out.append((n, u, ROOT + folder))
    return out, blind, notes


HOME_SECONDS = 1                                # #998 item 4: one home's reserve.d (a hung network home gives up)
HOMES_SECONDS = 2.5                             # all of them, well inside ANSWER_SECONDS


def read_homes(dirs, now, size):
    """#998 item 4: reservation_files() of every (name, uid, folder) in dirs, in order, each within HOME_SECONDS and
    all within HOMES_SECONDS, so one hung network or autofs home never stalls `heavy facts`. They are read in a child
    process (a read stuck in the kernel cannot be stopped in this one) that holds none of this process's files (its
    output pipes, any lock), so a child that never ends keeps nothing open; one that does not answer in time is
    killed and a new one reads the homes after its home. A home not read in time is 'blind', as one that cannot be
    read is. Without a child (fork refused): read here, as before #998, with a note."""
    out, i, deadline = [], 0, time.monotonic() + HOMES_SECONDS
    while i < len(dirs):
        r = w = None
        try:
            r, w = os.pipe()
            pid = os.fork()
        except OSError as e:
            for fd in (r, w):
                if fd is not None:
                    os.close(fd)
            rest = [reservation_files(folder, now, (name, u), size) for name, u, folder in dirs[i:]]
            rest[0][1].insert(0, f'homes read without a time limit (no child process: {e.strerror or e})')
            return out + rest
        if pid == 0:                                # the child: never returns
            try:
                os.close(r)
                null = os.open(os.devnull, os.O_RDWR)
                for fd in (0, 1, 2):
                    os.dup2(null, fd)
                os.closerange(3, w)
                os.closerange(w + 1, 1 << 16)
                for name, u, folder in dirs[i:]:
                    line = json.dumps(reservation_files(folder, now, (name, u), size)).encode() + b'\n'
                    while line:
                        line = line[os.write(w, line):]
            finally:
                os._exit(0)
        os.close(w)
        buf, got, gone = b'', [], False
        poll = select.poll()
        poll.register(r, select.POLLIN)
        try:
            while i + len(got) < len(dirs) and not gone:
                wait = min(HOME_SECONDS, deadline - time.monotonic())
                if wait <= 0 or not poll.poll(wait * 1000):
                    break                           # this home hung: the child is given up
                chunk = os.read(r, 1 << 16)
                gone = not chunk
                buf += chunk
                *lines, buf = buf.split(b'\n')
                got += [json.loads(line) for line in lines]
        finally:
            os.close(r)
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass
            for _ in range(20):                     # reaped when it ends soon; one stuck in the kernel is left
                try:
                    if os.waitpid(pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.01)
        out += [tuple(x) for x in got]
        i += len(got)
        if i < len(dirs):                           # the home it was reading did not answer (in time)
            name = dirs[i][0]
            late = not gone and time.monotonic() >= deadline
            why = ('its reader stopped' if gone else
                   f'not read within {HOMES_SECONDS if late else HOME_SECONDS:g} s: a hung network home?')
            out.append(([], [], [f'reservations of user {name} cannot be read ({why}): never free here']))
            i += 1
            if late:                                # no time left: the rest are not read either
                out += [([], [], [f'reservations of user {n} cannot be read (not read within {HOMES_SECONDS:g} s): '
                                  f'never free here']) for n, _, _ in dirs[i:]]
                break
    return out


def best(res, uid, cwd):
    """The one reservation a process of `uid` working in `cwd` counts toward: the deepest folder of that user's that
    holds cwd, the first by name when two hold it as deep (#945: never two, so nothing is taken off twice)."""
    found, depth = None, -1
    for r in res:
        if r['uid'] != uid:
            continue
        d = max((len(p) for p in r['prefixes'] if under(cwd, [p])), default=-1)
        if d > depth:
            found, depth = r, d
    return found


def count_in_reservations(res, owners, debts):
    """Each reservation's useMb (what its user's processes working in it use: RssAnon + RssShmem + VmSwap) and
    owedMb (#923: what counted pieces with a process working in it still owe). Every process counts toward one
    reservation only (best()), and a piece's debt is split over its processes by their use (equally when they use
    nothing), each share going to that process's reservation, so a piece's debt is taken off once at most (#945).
    owners: {pid: piece index}; debts: {piece index: MB}. A process whose working folder cannot be read (another
    user's, for a caller that is not root) counts toward none: its reservation keeps holding that room."""
    if not res:
        return
    uids = {r['uid'] for r in res}
    procs = {}                                      # pid: (uid, cwd or None, kB)
    for pid in os.listdir(ROOT + '/proc'):
        try:
            if not pid.isdigit():
                continue
            uid = os.stat(f'{ROOT}/proc/{pid}').st_uid
            if uid not in uids and pid not in owners:
                continue
            st = dict(line.split(':', 1) for line in _read(f'/proc/{pid}/status').splitlines() if ':' in line)
            kb = sum(int(st.get(k, '0 kB').split()[0]) for k in ('RssAnon', 'RssShmem', 'VmSwap'))
        except (OSError, ValueError, IndexError):
            continue                                # gone meanwhile, or not readable
        try:
            cwd = os.readlink(f'{ROOT}/proc/{pid}/cwd')
        except OSError:
            cwd = None
        procs[pid] = (uid, cwd, kb)
    for pid, (uid, cwd, kb) in procs.items():
        r = best(res, uid, cwd) if cwd else None
        if r:
            r['useMb'] += kb / 1024
        for o in res if cwd else ():                # works in every folder of that user's that holds cwd: an outer
            if o['uid'] == uid and under(cwd, o['prefixes']):   # one with idle= is not idle (its use goes to one)
                o['procs'] = o.get('procs', 0) + 1
    members = {}
    for pid, i in owners.items():
        if pid in procs:
            members.setdefault(i, []).append(pid)
    for i, pids in members.items():
        if not debts.get(i):
            continue
        total = sum(procs[p][2] for p in pids)
        for p in pids:
            uid, cwd, kb = procs[p]
            r = best(res, uid, cwd) if cwd else None
            if r:
                r['owedMb'] += debts[i] * (kb / total if total else 1 / len(pids))


def release_idle(res, now_s):
    """#861: a reservation with idle=DUR holds nothing once no process of its user works in its folder and its file
    was not written for DUR (the room is kept for work that comes back soon, not for good). Only where the processes
    can be seen: its user's own, or any for root; another user's processes in it cannot be seen by a caller that is
    not root, so their idle= is not applied there (it holds, stricter). Returns notes."""
    notes = []
    for r in res:
        if r.get('idleAt') is None or r['procs'] or now_s < r['idleAt']:
            continue
        if r['uid'] != os.getuid() and os.geteuid() != 0:
            continue
        notes.append(f'reservation {r["name"]} is idle (nobody works in its folder, its file not written since '
                     f'{int(now_s - r["idleAt"])} s past its idle=): holds nothing')
        r['mb'] = 0
    return notes


def _hold_cgroup(value):
    parts = value.split('/')
    if len(parts) > 16 or any(p in ('.', '..') or not CG_PART_RE.match(p) for p in parts):
        return 'must be a folder below the cgroup root, like "ci.slice/ci-runners.slice"'
    if parts[0] == 'user.slice':
        # #949: a slice in a user's own systemd manager (a "CI light" slice for small tooling steps) may be held; the
        # rest of user.slice is the ledger's (heavy jobs and agents), which would be counted twice
        m = MANAGER_HOLD_RE.match(value)
        if not m or m.group(1) != m.group(2) or not all(p.endswith('.slice') for p in parts[3:]):
            return ('below user.slice only a slice in a user manager, like '
                    '"user.slice/user-1000.slice/user@1000.service/ci-light.slice": the rest of user.slice is '
                    'counted already (heavy jobs and agents: it would count them twice)')
        if parts[3] == 'app.slice' or parts[3].startswith('app-'):
            return 'is in app.slice, where T3\'s agents run and are counted already (it would count them twice)'
    return None


def _manager_slice(cgroup):
    """The slice path inside its user manager of a user-manager hold (#949), or None for another hold."""
    return '/'.join(cgroup.split('/')[3:]) if MANAGER_HOLD_RE.match(cgroup) else None


def _users(value):
    ok = len(value) <= 32 and all(isinstance(g, str) and 0 < len(g) <= 64 for g in value)
    return None if ok else 'must be a list of at most 32 user name patterns (like "ci-*")'


def _label(value):
    return None if HOLD_LABEL_RE.match(value) else 'lower-case letters, digits, _ . -; at most 40'


def hold_spec(required):
    """One hold of heavy.capacity.holds (machine_file.fields spec; required: machine_file.REQUIRED)."""
    return {'label': (str, required, _label), 'cgroup': (str, required, _hold_cgroup),
            'inUseFromMb': (int, 1024, lambda v: None if 0 <= v <= 1 << 24 else 'must be 0 to 16777216'),
            'budgetMb': (int, 6144, lambda v: None if 1 <= v <= 1 << 24 else 'must be 1 to 16777216'),
            'users': (list, [], _users)}


MAX_HOLDS = 8
MAX_RESERVE_DIRS = 8
USER_NAME_RE = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9_.-]{0,31}\Z')


def _reserve_dir(value):
    parts = value.split('/')
    ok = (value.startswith('/') and value != '/' and len(value) <= 512 and not value.endswith('/')
          and all(p not in ('.', '..') for p in parts[1:]) and '' not in parts[1:])
    return None if ok else 'must be an absolute folder, like "/var/lib/t3-capacity/owner/reserve.d"'


def reserve_dir_spec(required):
    """One entry of heavy.capacity.reserveDirs (#844): the folder that holds one person's reservations."""
    return {'user': (str, required, lambda v: None if USER_NAME_RE.match(v) else 'must be a user name'),
            'dir': (str, required, _reserve_dir)}


def capacity_settings(mf, section_spec, path=None, owner_uid=0, heavy_slice=None):
    """heavy.capacity of the machine file (mf: fork-kit/common/machine_file.py): {'holds': [...]}. A missing file,
    section or key is no holds; anything broken raises mf.MachineFileError naming the key. heavy_slice: heavy's
    slice, whose jobs are counted already, so a user-manager hold (#949) in, on or around it is refused."""
    where = path or mf.default_path()
    section = mf.read_section('heavy', path, owner_uid, optional=True)
    if section is None:
        return {'holds': [], 'reserveDirs': []}
    section = mf.fields(section, section_spec, f'{where}: heavy')
    cap = mf.fields(section.get('capacity') or {}, {'holds': (list, [], None), 'reserveDirs': (list, [], None)},
                    f'{where}: heavy.capacity')
    if len(cap['holds']) > MAX_HOLDS:
        raise mf.MachineFileError(f'{where}: heavy.capacity.holds: at most {MAX_HOLDS}')
    spec = hold_spec(mf.REQUIRED)
    holds = []
    for i, h in enumerate(cap['holds']):
        h = mf.fields(h, spec, f'{where}: heavy.capacity.holds[{i}]')
        if any(h['label'] == o['label'] for o in holds):
            raise mf.MachineFileError(f'{where}: heavy.capacity.holds[{i}]: label {h["label"]!r} is there twice')
        for o in holds:                             # one in the other: the same room would be held twice
            a, b = sorted((h['cgroup'], o['cgroup']), key=len)
            if (b + '/').startswith(a + '/'):
                raise mf.MachineFileError(f'{where}: heavy.capacity.holds[{i}]: cgroup {h["cgroup"]!r} is in or the '
                                          f'same as {o["cgroup"]!r} of hold {o["label"]!r} (held twice)')
        inner = _manager_slice(h['cgroup'])
        if inner and heavy_slice:
            short, long = sorted((inner, slice_path(heavy_slice)), key=len)
            if (long + '/').startswith(short + '/'):
                raise mf.MachineFileError(f'{where}: heavy.capacity.holds[{i}]: cgroup {h["cgroup"]!r} is in, on or '
                                          f'around heavy\'s slice {heavy_slice}, whose jobs are counted already '
                                          f'(held twice)')
        holds.append(h)
    if len(cap['reserveDirs']) > MAX_RESERVE_DIRS:
        raise mf.MachineFileError(f'{where}: heavy.capacity.reserveDirs: at most {MAX_RESERVE_DIRS}')
    dirs, spec = [], reserve_dir_spec(mf.REQUIRED)
    for i, d in enumerate(cap['reserveDirs']):
        d = mf.fields(d, spec, f'{where}: heavy.capacity.reserveDirs[{i}]')
        if any(d['user'] == o['user'] for o in dirs):
            raise mf.MachineFileError(f'{where}: heavy.capacity.reserveDirs[{i}]: user {d["user"]!r} is there twice')
        dirs.append(d)
    return {'holds': holds, 'reserveDirs': dirs}


DISK_FLOOR_GB = 25     # #982: below this much free disk on the work filesystem t3-capacity says NOT OK


def _disk_path(value):
    return None if value == '~' or value.startswith(('/', '~/')) else 'must be an absolute folder (or ~, ~/...)'


DISK_SPEC = {'floorGb': (int, DISK_FLOOR_GB, lambda v: None if 0 <= v <= 1 << 20 else 'must be 0 (off) to 1048576'),
             'path': (str, '~', _disk_path)}


def disk_settings(mf, section_spec, path=None, owner_uid=0):
    """heavy.disk of the machine file: {'floorGb', 'path'}, the defaults when the file, section or key is missing.
    Anything broken raises mf.MachineFileError naming the key."""
    where = path or mf.default_path()
    section = mf.read_section('heavy', path, owner_uid, optional=True)
    section = mf.fields(section or {}, section_spec, f'{where}: heavy')
    return mf.fields(section.get('disk') or {}, DISK_SPEC, f'{where}: heavy.disk')


def disk_free_mb(folder):
    """MB this user may still write on the filesystem of folder (statvfs f_bavail: root's reserve left out). Tests
    set HEAVY_FACTS_DISK_FREE_MB (with HEAVY_TESTING=1). Raises OSError when it cannot be read."""
    fake = test_env('HEAVY_FACTS_DISK_FREE_MB')
    if fake:
        return int(fake)
    vfs = os.statvfs(folder)
    return vfs.f_bavail * vfs.f_frsize // MIB


def disk_check(hq):
    """t3-capacity's disk floor (#982): (the token for its first line, why it is NOT OK or None). Free disk below
    heavy.disk.floorGb on the filesystem of heavy.disk.path is NOT OK; so is a disk or a setting it cannot read.
    A floor of 0 is off: the disk is not read at all (`disk=off`)."""
    mf = hq.machine_file
    try:
        s = disk_settings(mf, hq.SECTION, ROOT + mf.default_path() if ROOT else None, os.getuid() if ROOT else 0)
    except Exception as e:
        return 'disk=?', f'{e.__class__.__name__}: {e}'
    if s['floorGb'] == 0:
        return 'disk=off', None
    folder = os.path.expanduser(s['path'])
    try:
        free = disk_free_mb(folder)
    except OSError as e:
        return 'disk=?', f'cannot read the free space of {folder} ({e.strerror or e})'
    gb, floor = free // 1024, s['floorGb']
    if free < floor * 1024:
        return f'disk={gb}GB<{floor}GB', f'{gb} GB free on {folder}, under the floor of {floor} GB (heavy.disk.floorGb)'
    return f'disk={gb}GB', None


def user_name():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def held_cgroups(host, holds, user):
    """The machine file's holds: a cgroup outside the user managers (a CI runners' slice, say), or a slice in a user's
    own manager (#949: a "CI light" slice), that is in use keeps its budget less what it uses, as a reservation, for
    every user but its own (`users`; a user-manager slice holds for its own user too unless `users` names them, so
    that user's builds keep its room). ([reservation], notes)"""
    out, notes = [], []
    for h in holds:
        if any(fnmatch.fnmatchcase(user, g) for g in h['users']):
            continue                                # the caller's own budget is not held back from it
        folder = f'{rd.CGROUP}/{h["cgroup"]}'
        if not os.path.isdir(ROOT + folder):
            continue                                # not there: nothing runs in it
        use = rd.read_piece_demand(host, 'v2', cg(folder))
        if use.get('gone'):
            if not os.path.isdir(ROOT + folder):
                continue                            # ended between the two looks: nothing runs in it
            # There, but no memory.stat: the memory controller is off for it (its parent's cgroup.subtree_control),
            # so its use and limit cannot be known: it holds its whole budgetMb.
            notes.append(f'hold {h["label"]}: no memory controller (no memory.stat): holds its whole budget '
                         f'{h["budgetMb"]} MB')
            out.append({'name': h['label'], 'prefixes': [], 'mb': h['budgetMb'], 'useMb': 0, 'owedMb': 0})
            continue
        if use['demandMb'] is None:
            notes.append(f'hold {h["label"]}: its memory.stat cannot be read: holds its whole budget')
        elif use['demandMb'] <= h['inUseFromMb']:
            continue                                # not in use
        try:
            raw = _read(folder + '/memory.max').strip()
            mb = h['budgetMb'] if raw == 'max' else int(raw) // (1 << 20)
        except (OSError, ValueError):
            mb = h['budgetMb']
            notes.append(f'hold {h["label"]}: its memory.max cannot be read: budget {mb} MB')
        # In use counts its swap (a swapped-out slice has run), but memory.max limits RAM only (swap has its own
        # memory.swap.max): the room it can still take is its budget less the RAM it uses, so its swap is left out
        # here (missing or unreadable swap: 0, as in the shared reader).
        swap = rd._mb_of(rd._one_number(rd._text_of(host, folder + '/memory.swap.current'))) or 0
        ram = None if use['demandMb'] is None else max(0, use['demandMb'] - swap)
        out.append({'name': h['label'], 'prefixes': [], 'mb': mb, 'useMb': ram or 0, 'owedMb': 0})
    return out, notes


def scope_pids(folder):
    """The processes (pids as text) in a cgroup folder and the cgroups below it; none where it cannot be read."""
    pids = set()
    for dirpath, _dirs, _files in os.walk(ROOT + folder):
        try:
            with open(os.path.join(dirpath, 'cgroup.procs'), 'rb') as f:
                pids.update(f.read(1 << 20).decode('ascii', 'replace').split())
        except OSError:
            pass
    return pids


def debt(rule, learned, piece, total_mb):
    """What a piece still owes the rule: its cost less its demand (capacity_rule.decision's owed part for it)."""
    cost = cr.cost_of(rule, cr.capacity_kind(rule, piece['kind']), learned, piece.get('declaredMb'))['mb']
    d = piece['demandMb']
    d = math.floor(d) if cr.is_num(d) and total_mb is not None and 0 <= d <= total_mb else 0
    return max(0, cost - d)


def scope_demand(host, folder):
    """A live piece's demand in MB (None: owes its whole cost), or False when its cgroup has ended."""
    d = rd.read_piece_demand(host, 'v2', cg(folder))
    return False if d['gone'] else d['demandMb']


def other_users(rule, host, uid, heavy_rel, owners, base):
    """The other users' heavy jobs and T3 agent scopes, from their user managers' cgroups (readable by every user):
    (pieces, jobs, agents, blind, notes). A folder that exists but cannot be listed is a note and 'blind'. owners gets
    {pid: base + the piece's index} for their processes (count_in_reservations)."""
    pieces, jobs, agents, blind, notes = [], 0, 0, [], []
    try:
        names = sorted(os.listdir(ROOT + USERS))
    except FileNotFoundError:
        return pieces, jobs, agents, blind, notes
    except OSError as e:
        return pieces, jobs, agents, [f'{USERS} cannot be listed ({e.strerror})'], notes
    for name in names:
        m = USER_RE.match(name)
        if not m or int(m.group(1)) == uid:
            continue
        manager = f'{USERS}/{name}/user@{m.group(1)}.service'
        box = manager + heavy_rel
        try:
            scopes = sorted(n for n in os.listdir(ROOT + box) if SCOPE_RE.match(n))
        except FileNotFoundError:
            scopes = []
        except OSError as e:
            blind.append(f'{box} cannot be listed ({e.strerror})')
            continue
        for n in scopes:
            demand = scope_demand(host, f'{box}/{n}')
            if demand is not False:
                owners.update(dict.fromkeys(scope_pids(f'{box}/{n}'), base + len(pieces)))
                pieces.append({'kind': rule['unknownKinds']['job'], 'demandMb': demand})
                jobs += 1
        found, _box, more = agent_scopes(manager)
        notes += [f'user {m.group(1)}: {n}' for n in more]
        for s in found:
            demand = scope_demand(host, s)
            if demand is not False:
                owners.update(dict.fromkeys(scope_pids(s), base + len(pieces)))
                pieces.append({'kind': rule['unknownKinds']['other'], 'demandMb': demand})
                agents += 1
    return pieces, jobs, agents, blind, notes


def _small_text(path):
    """(text, None) or (None, 'missing') or (None, why it cannot be read): one small cgroup or /proc file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, 'missing'
    except OSError as e:
        return None, e.strerror or str(e)
    try:
        return os.read(fd, 1 << 16).decode('utf-8', 'replace'), None
    except OSError as e:
        return None, e.strerror or str(e)
    finally:
        os.close(fd)


OUTSIDE_WHY = 'cannot be used (it leaves the cgroup tree: outside this cgroup namespace)'


def own_caps(uid):
    """#951: the memory caps of the cgroups the caller's work starts under, below the cgroup root (the shared
    reader caps the root itself): with this user's manager (the full tier: heavy jobs and agents start below it) the
    manager and the slices above it, else (the machine tier: work runs where the caller runs) the caller's own
    cgroup (/proc/self/cgroup) and every one above it. Only capped ones, in order from the root: {'path', 'limitMb'
    (the smaller of memory.max and memory.high; None: it cannot be read), 'roomMb' (the limit less its use not
    counting clean cache that can go, as the shared reader counts the root; None: its use cannot be read), 'why'}.
    A cgroup without the files (no memory controller there) has no cap."""
    manager = f'{USERS}/user-{uid}.slice/user@{uid}.service'
    if os.path.isdir(ROOT + manager):
        own = manager[len(rd.CGROUP):]
    else:
        text, err = _small_text(ROOT + '/proc/self/cgroup')
        if err == 'missing':                        # no /proc (a test machine): nothing below the root to cap it
            return []
        if err:
            return [{'path': '/proc/self/cgroup', 'limitMb': None, 'roomMb': None, 'why': f'cannot be read ({err})'}]
        own = next((line[3:] for line in text.splitlines() if line.startswith('0::')), None)
        if own is None:                             # cgroup v1 alone: the basic tier
            return []
    path = rd.cgroup_path(own)
    if path is None and re.fullmatch(r'/+', own):   # the root (a namespace's): the shared reader's
        return []
    if path is None:                                # #996 item 5: above the namespace's root (/..): unreadable, busy
        return [{'path': own[:200], 'limitMb': None, 'roomMb': None, 'why': OUTSIDE_WHY}]
    caps, parts = [], path.split('/')[1:]
    for i in range(1, len(parts) + 1):
        rel = '/' + '/'.join(parts[:i])
        folder = ROOT + rd.CGROUP + rel
        limit = why = None
        for name in ('memory.max', 'memory.high'):
            text, err = _small_text(f'{folder}/{name}')
            if err == 'missing':
                continue
            value = None if err else text.strip()
            if value == 'max':
                continue
            if err or not re.fullmatch(r'[0-9]{1,19}', value or '', re.ASCII):
                why = f'{name} cannot be read ({err or "not a number"})'
                break
            limit = int(value) if limit is None else min(limit, int(value))
        if why:
            caps.append({'path': rel, 'limitMb': None, 'roomMb': None, 'why': why})
            continue
        if limit is None:
            continue
        current, err = _small_text(f'{folder}/memory.current')
        stat, err2 = _small_text(f'{folder}/memory.stat')
        used = None
        if not err and not err2 and re.fullmatch(r'[0-9]{1,19}\n?', current, re.ASCII):
            inactive = re.search(r'^inactive_file ([0-9]{1,19})$', stat, re.M | re.ASCII)
            used = None if inactive is None else max(0, int(current) - int(inactive.group(1)))
        caps.append({'path': rel, 'limitMb': limit // MIB, 'why': None if used is not None else
                     'its use cannot be read', 'roomMb': None if used is None else max(0, limit - used) // MIB})
    return caps


def apply_caps(m, caps):
    """#951: the machine's total and available memory held to the caps the work starts under (own_caps); notes."""
    notes = []
    for c in caps:
        if c['limitMb'] is None:
            m['availableMb'] = None
            notes.append(f'memory cap of {c["path"]}: {c["why"]}: no room known (busy for every kind)')
            continue
        if m['totalMb'] is not None:
            m['totalMb'] = min(m['totalMb'], c['limitMb'])
        if c['roomMb'] is None:
            m['availableMb'] = None
            notes.append(f'memory cap of {c["path"]}: {c["limitMb"]} MB; {c["why"]}: no room known (busy for every '
                         f'kind)')
        else:
            if m['availableMb'] is not None:
                m['availableMb'] = min(m['availableMb'], c['roomMb'])
            notes.append(f'memory cap of {c["path"]}: {c["limitMb"]} MB, {c["roomMb"]} MB of it free (the work '
                         f'starts under it)')
    return notes


def cgroup_tier(cgroup, uid):
    """The tier the machine's cgroups allow, from their structure alone (one source for read_machine and ready):
    'full' with cgroup v2 and this user's own manager; 'machine' with cgroup v2 and user.slice but no manager of its
    own (#844: a CI runner's user: nothing of its own to count, everyone else's as usual); else 'basic'. cgroup is
    capacity_readings' version ('v2', 'v1', 'unreadable' or None)."""
    if cgroup != 'v2':
        return 'basic'
    if os.path.isdir(ROOT + f'{USERS}/user-{uid}.slice/user@{uid}.service'):
        return 'full'
    return 'machine' if os.path.isdir(ROOT + USERS) else 'basic'


def read_machine(rule, where, carry, now):
    """This machine's readings now: what the cache keeps (the same for every caller with these settings)."""
    notes = []
    uid = os.getuid()
    host = rd.RealHost(ROOT)
    m = rd.read_machine(host)
    manager = f'{USERS}/user-{uid}.slice/user@{uid}.service'
    tier = cgroup_tier(m['cgroup'], uid)
    mine = tier == 'full'                           # this user's own agents and heavy jobs can be read
    full = tier != 'basic'
    heavy_rel = '/' + slice_path(where['slice'])
    heavy_box = manager + heavy_rel
    pieces, running, waiting, owners, ticketed = [], 0, 0, {}, set()
    for t in tickets(where['queueDir']):
        if isinstance(t.get('scope'), str):
            ticketed.add(t['scope'])
        if t['state'] == 'waiting':
            waiting += 1
            continue
        running += 1
        scope = t.get('scope')
        demand = None
        if mine and isinstance(scope, str) and SCOPE_RE.match(scope):
            # an admitted job whose scope is not there (yet) owes its whole cost: it is never dropped
            demand = rd.read_piece_demand(host, 'v2', cg(heavy_box + '/' + scope)).get('demandMb')
            owners.update(dict.fromkeys(scope_pids(heavy_box + '/' + scope), len(pieces)))
        pieces.append({'kind': ticket_kind(rule, t['kind']), 'demandMb': demand, 'declaredMb': t.get('mb')})
    if mine:
        # a plain heavy (no --kind: no ticket) and a kept scope are jobs too: every scope in this user's heavy slice
        # that no live ticket names is one of the rule's unknownKinds.job, as another user's are (other_users)
        try:
            plain = sorted(n for n in os.listdir(ROOT + heavy_box) if SCOPE_RE.match(n) and n not in ticketed)
        except FileNotFoundError:
            plain = []
        except OSError as e:
            plain = []
            notes.append(f'{heavy_box} cannot be listed ({e.strerror}): plain heavy jobs not counted')
            full, tier = False, 'basic'
        for n in plain:
            demand = scope_demand(host, f'{heavy_box}/{n}')
            if demand is not False:
                owners.update(dict.fromkeys(scope_pids(f'{heavy_box}/{n}'), len(pieces)))
                pieces.append({'kind': rule['unknownKinds']['job'], 'demandMb': demand})
                running += 1
    own_jobs = running
    agents, agent_box = 0, None
    if mine:
        scopes, agent_box, more = agent_scopes(manager)
        notes += more
        for s in scopes:
            demand = scope_demand(host, s)
            if demand is not False:
                owners.update(dict.fromkeys(scope_pids(s), len(pieces)))
                pieces.append({'kind': rule['unknownKinds']['other'], 'demandMb': demand})   # no kind label: a writer
                agents += 1
    if full:
        more, jobs, other_agents, blind, more_notes = other_users(rule, host, uid, heavy_rel, owners, len(pieces))
        notes += more_notes
        pieces += more
        running, agents = running + jobs, agents + other_agents
        if jobs or other_agents:
            notes.append(f'other users: {jobs} heavy job(s), {other_agents} agent(s) (counted)')
        if blind:
            full, tier = False, 'basic'
            notes += blind
    learned_notes = []
    learned = where['learned'](learned_notes)
    def size(kind):        # a reservation by kind (#861): the rule's cost of one piece of it, learned from peaks
        return cr.cost_of(rule, ask_kind(rule, kind), learned, None)['mb']
    res, more, _ = reservation_files(where['reserveDir'], now, size=size)
    notes += more
    listed = {d['user']: d['dir'] for d in where.get('reserveDirs', ())}
    if where['user'] in listed:
        # #844: the caller's own folder in the machine file is its too (unless it is the one just read)
        try:
            again = os.path.samefile(ROOT + listed[where['user']], where['reserveDir'])
        except OSError:
            again = False
        if not again:
            more_res, more, _ = reservation_files(ROOT + listed[where['user']], now, size=size)
            res += more_res
            notes += more
    if tier != 'basic':
        # #945: a caller with no user manager is often another user (a CI runner, root), whose own reserve.d is not
        # the owner's: read every person's, so it never takes the room they keep. One it cannot read: never free.
        # #800: the full tier reads them too, so one person's reservation holds against every other person's work;
        # there one it cannot read (a home closed to others, as most are by default) is a note, as before #800 it
        # was not read at all: a person who wants it seen makes ~/.config/t3-capacity/reserve.d readable to others.
        dirs, blind, more = other_reserve_dirs(uid, listed)
        notes += more
        for more_res, more, cannot in read_homes(dirs, now, size):
            res += more_res
            notes += more
            blind += cannot
        if blind and tier == 'machine':
            full, tier = False, 'basic'
            notes += blind
        elif blind:
            notes += [b.replace(': never free here', '') + ': not counted' for b in blind[:8]]
    # #923, #945: a piece working in a reservation's folder owes its debt once, not there too, and never in two
    count_in_reservations(res, owners, {i: debt(rule, learned, pieces[i], m['totalMb']) for i in set(owners.values())})
    notes += release_idle(res, now.timestamp())
    held, more = held_cgroups(host, where['holds'], where['user'])
    notes += [f'reservation {n}.conf has the name of a machine-file hold: both hold; rename the file'
              for n in sorted({r['name'] for r in res} & {h['label'] for h in where['holds']})]
    res += held
    notes += more
    notes += [f'{g["reading"]}: {g["instead"]}' for g in m['gaps']]
    if tier == 'basic':
        notes.append(BASIC_WHY)
    elif tier == 'machine':
        notes.append(MACHINE_WHY)
    notes += learned_notes
    # A box with no live piece of its own passes no box pressure (as the server's observer); one that has some is
    # read by the shared reader: missing or broken is "unreadable".
    box_psi = {'job': rd.read_box_psi(host, m, cg(heavy_box)) if full and mine and own_jobs else None,
               'agent': rd.read_box_psi(host, m, cg(agent_box)) if full and agent_box and agents else None}
    return {'v': CACHE_VERSION, 'key': where['key'], 'atMs': int(now.timestamp() * 1000),
            'tier': tier,
            'facts': {'totalMb': m['totalMb'], 'availableMb': m['availableMb'], 'cpuCores': m['cpuCores'],
                      'pieceCaps': tier == 'full', 'swap': m['swap'], 'psi': m['psi'], 'osLevel': m['osLevel'],
                      'pieces': pieces},
            'boxPsi': box_psi, 'reservations': res, 'learned': learned, 'carry': carry,
            'notes': notes, 'load1': load1(), 'cpu': cpu_reading(host),
            'queue': {'running': running, 'waiting': waiting, 'agents': agents}}


def no_carry():
    """The pressure state when none can be carried on safely: "elevated" (the rule's floors only), so the level
    never drops straight to ok past the hold-down."""
    return {g: {'level': 'elevated', 'calmSinceMs': None} for g in ('agent', 'job')}


def read_cache(folder, name, now_ms, key):
    """(readings while fresh, else None; the pressure state to carry on, else None) from the file `name` in the open
    `folder` (a Folder: never its path again, #951). Anything odd is not trusted. No
    file, or one older than CARRY_SECONDS: nothing to carry (None). A file that is there but cannot be used (another
    version, as once after an update or while an old heavy writes it too; other settings, broken, from the future,
    not this user's own): no_carry(), so the hold-down is never skipped (#945). No file of its own yet, but another
    cache file of this user's beside it written in the last CARRY_SECONDS (another version just after an update, or
    other settings): no_carry() too; from its own file on, it steps down by its own hold-down."""
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=folder.fd)
    except FileNotFoundError:
        return None, (no_carry() if other_caches(folder, name, recent=True) else None)
    except OSError:
        return None, no_carry()
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077 or not stat.S_ISREG(st.st_mode):
            return None, no_carry()
        data = cr.loads(os.read(fd, 1 << 20).decode('utf-8'))
    except (OSError, ValueError):
        return None, no_carry()
    finally:
        os.close(fd)
    if not (isinstance(data, dict) and data.get('v') == CACHE_VERSION and set(data) == CACHE_KEYS
            and data['key'] == key and type(data['atMs']) is int and isinstance(data['pressure'], dict)
            and isinstance(data['facts'], dict)):
        return None, no_carry()
    age = now_ms - data['atMs']
    if age < 0:
        return None, no_carry()
    carry = data['pressure'] if age <= CARRY_SECONDS * 1000 else None
    return (data if age < CACHE_SECONDS * 1000 and carry is not None else None), carry


def other_caches(folder, own_name, recent):
    """The names of the other cache files of this user's (any version or settings, an older kit's heavy-facts.json
    too) beside `own_name` in the open `folder`, written in the last CARRY_SECONDS (recent) or before (not recent). A
    file from the future is recent."""
    try:
        names = os.listdir(folder.fd)
    except OSError:
        return []
    out = []
    for name in names:
        if name == own_name or not CACHE_FILE_RE.match(name):
            continue
        try:
            st = os.lstat(name, dir_fd=folder.fd)
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and \
                (time.time() - st.st_mtime <= CARRY_SECONDS) == recent:
            out.append(name)
    return out


def write_cache(folder, base, data):
    """Whole or not at all: a unique temporary file renamed over the file `base` in the open `folder` (every step
    through its handle, #951). A temporary file left by a writer that stopped half-way is removed once it is a minute
    old, and this user's cache files of other settings or versions once they are past the carry window (they can
    carry nothing then), so they do not pile up."""
    fd_dir = folder.fd
    for name in os.listdir(fd_dir):
        if name.startswith(base + '.') and name.endswith('.tmp'):
            try:
                if time.time() - os.lstat(name, dir_fd=fd_dir).st_mtime > STALE_TMP_SECONDS:
                    os.unlink(name, dir_fd=fd_dir)
            except OSError:
                pass
    for old in other_caches(folder, base, recent=False):    # other settings or versions, past the carry window
        try:
            os.unlink(old, dir_fd=fd_dir)
        except OSError:
            pass
    tmp = f'{base}.{os.getpid()}.{os.urandom(4).hex()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=fd_dir)
    try:
        os.write(fd, json.dumps(data, separators=(',', ':')).encode())
    finally:
        os.close(fd)
    os.replace(tmp, base, src_dir_fd=fd_dir, dst_dir_fd=fd_dir)


def own(r, cwds):
    return any(under(c, r['prefixes']) for c in cwds if c)


def facts_for(readings, group, cwds):
    """The rule's facts for a piece of the group, as its caller sees them (its own reservations left out)."""
    res = [{'mb': r['mb'], 'useMb': r['useMb'] + r['owedMb']} for r in readings['reservations'] if not own(r, cwds)]
    return dict(readings['facts'], boxPsi=readings['boxPsi'][group], reservations=res)


def answer(rule, readings, kind=None, need_mb=None, cwds=()):
    """(exit code, the free/v1 object) for one caller."""
    now_ms, learned, carry = readings['atMs'], readings['learned'], readings['carry'] or {}
    kind = ask_kind(rule, kind) if kind else (rule['unknownKinds']['job'] if need_mb is None else None)
    group = rule['kinds'][kind]['group'] if kind else 'job'
    ask = {'kind': kind or rule['unknownKinds']['job']}
    if need_mb is not None:
        ask['declaredMb'] = need_mb
    d = cr.decision(rule, facts_for(readings, group, cwds), ask, learned, carry.get(group), now_ms)
    basic = readings['tier'] not in ('full', 'machine')
    # with a kind the rule alone decides (its floor too: a declared size is already a floor of the cost); MB alone
    # asks only whether they fit the free budget at level ok
    roomy = not basic and (d['admit'] if kind else d['level'] == 'ok' and d['freeMb'] is not None
                           and need_mb <= d['freeMb'])
    # the CPU brake (#1047): jobs only (MB alone asks for a job), never past what memory refused
    busy_cpu = cpu_busy(rule, readings.get('cpu'), readings['load1'])
    free = roomy and not (busy_cpu and group == 'job')
    fits = {}
    for k, spec in rule['kinds'].items():
        g = spec['group']
        fits[k] = 0 if basic or busy_cpu and g == 'job' else cr.fill(rule, facts_for(readings, g, cwds),
                                          [{'kind': k, 'count': rule['ceilings'][g]}], learned, carry.get(g),
                                          now_ms)['admitted'][0]
    facts, swap = readings['facts'], readings['facts']['swap']
    swap = swap if isinstance(swap, dict) and swap['totalMb'] > 0 else None
    avail = None if facts['availableMb'] is None else math.floor(facts['availableMb'])
    out = {'v': 1, 'free': free, 'at': datetime.datetime.fromtimestamp(now_ms / 1000, datetime.timezone.utc)
           .isoformat(timespec='seconds'), 'kind': kind, 'level': d['level'], 'tier': readings['tier'], 'fits': fits,
           'memory': {'availableMb': avail,
                      'budgetMb': None if d['marginMb'] is None or avail is None else avail - d['marginMb'],
                      'reservedMb': None if d['owedMb'] is None else d['owedMb'] + d['reservedMb'],
                      'freeMb': d['freeMb'], 'marginMb': d['marginMb'], 'owedMb': d['owedMb'],
                      'holdMb': d['reservedMb'], 'costMb': d['costMb'],
                      'swapFreeMb': math.floor(swap['freeMb']) if swap else 0,
                      'swapUsedPercent': round(100 - 100 * swap['freeMb'] / swap['totalMb'], 1) if swap else None,
                      'pressureFull10': facts['psi']['fullAvg10Pct'] if isinstance(facts['psi'], dict) else None},
           'queue': readings['queue'], 'cpu': {'cores': facts['cpuCores'], 'load1': readings['load1'],
                                               'pressureSome10': (readings.get('cpu') or {}).get('pressureSome10')},
           'reservations': [{'name': r['name'], 'mb': r['mb'], 'useMb': math.floor(r['useMb']),
                             'owedMb': math.floor(r['owedMb']), 'own': own(r, cwds)} for r in readings['reservations']],
           'notes': list(readings['notes'])}
    if not free and roomy:
        out['reason'] = f'{kind}: {busy_cpu}' if kind else busy_cpu
    elif not free:
        why = 'basic tier' if basic and (d['admit'] if kind else d['level'] == 'ok') else \
            (d['reason'] if kind else f'{need_mb} MB')
        out['reason'] = (f'{kind}: {why}' if kind else why) + \
            f' ({d["freeMb"]} MB free, {need_mb if need_mb is not None else d["costMb"]} MB needed, level ' \
            f'{d["level"]}{", machine-wide numbers: no user manager" if readings["tier"] == "machine" else ""})'
    while len(json.dumps(out)) > 4000 and (out['notes'] or out['reservations']):
        (out['notes'] or out['reservations']).pop()
    return (EXIT_FREE if free else EXIT_BUSY), out


def options(argv):
    opts, rest = {}, list(argv)
    while rest:
        key, eq, value = rest.pop(0).partition('=')
        if key == '--fresh' and not eq and key not in opts:
            opts[key] = True
        elif key in ('--kind', '--mb') and key not in opts and (eq or rest):
            opts[key] = value if eq else rest.pop(0)
        else:
            return None
    if not KIND_RE.match(opts.get('--kind', 'x')) or not re.match(r'^[0-9]{1,7}\Z', opts.get('--mb', '0')):
        return None
    return opts


def temp_root():
    """$TMPDIR, else /tmp (TMP_DIRS): the first absolute folder this user can write, else None. Never the working
    folder, where tempfile.gettempdir() ends up when none can be written (#945)."""
    for d in (os.environ.get('TMPDIR'),) + TMP_DIRS:
        if d and os.path.isabs(d) and os.path.isdir(d) and os.access(d, os.W_OK | os.X_OK):
            return d
    return None


def cache_name(key):
    """One cache file per version and settings (#945): two callers of one user with other settings, or an old and a
    new kit, that took turns on one file read each other's as unusable and kept each other "elevated" for good."""
    return f'heavy-facts-v{CACHE_VERSION}-{hashlib.sha256(key.encode()).hexdigest()[:16]}.json'


O_FOLDER = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Folder:
    """A folder held open (#951): every later step in it goes through its handle (dir_fd), never its path again, so
    a folder swapped for another (or for a link) after its check is never followed. path: for notes only."""

    def __init__(self, fd, path):
        self.fd, self.path = fd, path

    def at(self, name):
        return os.path.join(self.path, name)

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def open_folder(name, shown, parent=None, make=False, private=True):
    """(Folder, None) or (None, why): the folder `name` (in the open folder fd `parent`, else a path), opened once
    with O_DIRECTORY | O_NOFOLLOW (a link or a file is refused), made with mode 700 when missing (make); private: it
    must be this user's and closed to everyone else (one planted first by another user is refused)."""
    fd = None
    try:
        if make:
            try:
                os.mkdir(name, 0o700, dir_fd=parent)
            except FileExistsError:
                pass
        fd = os.open(name, O_FOLDER, dir_fd=parent)
        st = os.fstat(fd)
    except OSError as e:
        if fd is not None:
            os.close(fd)
        return None, f'{shown} cannot be made or opened as a folder ({e.strerror or e})'
    if private and (st.st_uid != os.getuid() or st.st_mode & 0o077):
        os.close(fd)
        return None, f'{shown} is not a folder only this user can use'
    return Folder(fd, shown), None


def swappable(st, uid):
    """True when someone else could swap what is in a folder with this stat: not root's or this user's, or a group or
    others can write it and it is not sticky (in a sticky one only an entry's owner can rename or remove it)."""
    # bin/heavy and lib/heavy_sweep.py others_can_swap hold the same rule: tests/facts.test.py OneRule checks they agree
    return st.st_uid not in (0, uid) or bool(st.st_mode & 0o022 and not st.st_mode & stat.S_ISVTX)


def open_home():
    """(fd of this user's home, its path) or (None, why) (#951 points 2 and 4): $HOME, else the password database's
    home, links resolved once, then opened one part at a time from / with O_NOFOLLOW (a part swapped for a link
    after that is refused). Every folder above it must be root's or this user's and, when others can write it,
    sticky: no one else can swap a part. The home itself must be this user's, closed to writes by others, not /."""
    uid = os.getuid()
    homes = [os.environ.get('HOME') or '']
    try:
        homes.append(pwd.getpwuid(uid).pw_dir)
    except KeyError:
        pass
    whys = []
    for home in dict.fromkeys(h for h in homes if h and os.path.isabs(h)):
        home = os.path.realpath(home)
        parts = [p for p in home.split('/') if p]
        if not parts:
            whys.append('the home is /')
            continue
        fd, at = None, '/'
        try:
            fd = os.open('/', O_FOLDER)
            for part in parts:
                if swappable(os.fstat(fd), uid):
                    raise PermissionError(f'{at} can be changed by others (not sticky)')
                nxt = os.open(part, O_FOLDER, dir_fd=fd)
                os.close(fd)
                fd, at = nxt, os.path.join(at, part)
            st = os.fstat(fd)
            if st.st_uid != uid or st.st_mode & 0o022:
                raise PermissionError(f'{home} is not this user\'s, or others can write it')
            fd, out = None, fd
            return out, home
        except OSError as e:
            whys.append(e.args[0] if isinstance(e.args[0], str) else f'{at}: {e.strerror or e}')
        finally:
            if fd is not None:
                os.close(fd)
    return None, '; '.join(whys) or 'no home folder'


def host_name():
    """This machine's host name, made safe for a folder name."""
    return re.sub(r'[^A-Za-z0-9_.-]', '_', os.uname().nodename)[:64] or 'host'


MACHINE_IDS = (('/etc/machine-id', ''), ('/var/lib/dbus/machine-id', ''), ('/proc/sys/kernel/random/boot_id', 'boot-'))
STALE_BOOT_SECONDS = 86400                      # #998 item 3: a boot folder not written for this long is removed


def machine_tag():
    """This machine for a cache folder in the home, which other machines may share (one cache per machine): the host
    name and (#996 item 3: clones and cloud images keep one host name) the machine id (systemd's, else D-Bus's),
    else `boot-` and the boot id (a new one each boot: drop_stale_boot_folders removes the old ones, #998 item 3);
    none (no machine id, or "uninitialized" on a first boot, and no /proc): the host name alone."""
    for path, mark in MACHINE_IDS:
        try:
            ident = _read(path, 128).strip().lower().replace('-', '')
        except OSError:
            continue
        if re.fullmatch(r'[0-9a-f]{32}', ident):
            return f'{host_name()}-{mark}{ident[:16]}'
    return host_name()


def drop_stale_boot_folders(cache, keep):
    """#998 item 3: remove this kit's boot-id folders of this host name in the open ~/.cache (cache) other than
    `keep` whose files were not written for STALE_BOOT_SECONDS (the newest file's time; an empty folder's own time):
    each boot without a machine id made one, and nothing read them again. Only a folder of plain files is removed;
    anything else (the previous kit's host-only folder, a machine-id folder, another host name's, a link, a folder
    with a folder in it, one that cannot be read) is left as it is. Best effort, never an error: a later call
    finishes what one stopped half-way (the files left keep their old times)."""
    prefix = f'heavy-facts-{host_name()}-boot-'
    try:
        names = [n for n in os.listdir(cache.fd)
                 if n.startswith(prefix) and n != keep and re.fullmatch(r'[0-9a-f]{16}', n[len(prefix):])]
    except OSError:
        return
    limit = time.time() - STALE_BOOT_SECONDS
    for name in names:
        try:
            fd = os.open(name, O_FOLDER, dir_fd=cache.fd)
        except OSError:
            continue                                # a link, a file, gone, closed: not touched
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid():
                continue
            files = [(n, os.stat(n, dir_fd=fd, follow_symlinks=False)) for n in os.listdir(fd)]
            if any(not stat.S_ISREG(f.st_mode) for _, f in files):
                continue
            if max([f.st_mtime for _, f in files] or [st.st_mtime]) >= limit:
                continue                            # written lately: a machine with this host name may use it
            for n, _ in files:
                os.unlink(n, dir_fd=fd)
            os.rmdir(name, dir_fd=cache.fd)
        except OSError:
            continue
        finally:
            os.close(fd)


def home_cache():
    """(Folder, None) or (None, why): ~/.cache/heavy-facts-<machine_tag()>, a folder only this user can use in its
    home (open_home), each folder made when missing. ~/.cache must be this user's and closed to writes by others. The
    previous kit's ~/.cache/heavy-facts-<host> is never read (another machine of this home may still run that kit,
    so it is not removed either); this kit's stale boot-id folders are (drop_stale_boot_folders)."""
    home, where = open_home()
    if home is None:
        return None, f'no home for it ({where})'
    try:
        cache, why = open_folder('.cache', os.path.join(where, '.cache'), parent=home, make=True, private=False)
    finally:
        os.close(home)
    if cache is None:
        return None, why
    try:
        st = os.fstat(cache.fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o022:
            return None, f'{cache.path} is not this user\'s, or others can write it'
        name = f'heavy-facts-{machine_tag()}'
        drop_stale_boot_folders(cache, name)
        return open_folder(name, cache.at(name), parent=cache.fd, make=True)
    finally:
        cache.close()


def cache_folder(run):
    """(Folder, a note or None) or (None, why there is none), held open (#951). /run/user/<uid> when it is there;
    else (#844: a user with no user manager and no login session has none) a folder only this user can use in the
    temporary folder, made if missing; one that is a link, another user's or open to others (planted first) or no
    temporary folder at all: a folder only this user can use in its home (home_cache, #951: a name taken by another
    user no longer denies the cache), with a note; neither: no cache, and so no carry."""
    if os.path.isdir(run):
        folder, why = open_folder(run, run)
        if folder:
            return folder, None
        whys = [why]
    else:
        whys = []
    root = TMP_ROOT or temp_root()
    if not root:
        whys.append('no temporary folder this user can write ($TMPDIR, /tmp)')
    else:
        name = f'heavy-facts-{os.getuid()}'
        try:
            parent = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        except OSError as e:
            whys.append(f'{root} cannot be opened ({e.strerror or e})')
        else:
            try:
                folder, why = open_folder(name, os.path.join(root, name), parent=parent, make=True)
            finally:
                os.close(parent)
            if folder:
                return folder, None
            whys.append(why)
    folder, why = home_cache()
    if folder:
        return folder, f'{"; ".join(whys)}: the cache is in {folder.path}'
    return None, f'no cache: {"; ".join(whys + [why])}: every call starts "elevated"'


HOLD_SECONDS = 20                               # #861: a NOT OK holds this long for the same question
HOLD_FILE = 't3-capacity-hold.json'
HOLD_LOCK = 't3-capacity-hold.lock'           # one hold at a time (its read, then its write)
HOLD_LOCK_SECONDS = 2


def given_folder(path):
    """(Folder, None) or (None, why): a folder named by the caller ($HEAVY_FACTS_CACHE's, a test's), opened once."""
    try:
        return Folder(os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC), path), None
    except OSError as e:
        return None, f'{path} cannot be opened ({e.strerror or e})'


def state_folder():
    """(the open folder of heavy facts' cache, None) or (None, why there is none): $HEAVY_FACTS_CACHE's folder, else
    cache_folder()'s."""
    if os.environ.get('HEAVY_FACTS_CACHE'):
        return given_folder(os.path.dirname(os.path.abspath(os.environ['HEAVY_FACTS_CACHE'])))
    folder, why = cache_folder(f'{RUN_USER}/{os.getuid()}')
    return (folder, None) if folder else (None, why)


def _hold_lock(folder):
    """The hold's own lock (#861 round 1: two callers' read-modify-write of the hold file must not lose a NOT OK):
    (fd or None, a note or None). Waits HOLD_LOCK_SECONDS at most, never blocking in flock; without it the hold goes
    on unlocked, with a note (a hold is never a reason not to answer)."""
    path = folder.at(HOLD_LOCK)
    try:
        fd = os.open(HOLD_LOCK, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600, dir_fd=folder.fd)
    except OSError as e:
        return None, f'the hold lock {path} cannot be opened ({e.strerror}): held without it'
    deadline = time.monotonic() + HOLD_LOCK_SECONDS
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return fd, None
        except OSError as e:
            if e.errno not in (errno.EWOULDBLOCK, errno.EAGAIN) or time.monotonic() >= deadline:
                os.close(fd)
                return None, f'the hold lock {path} stayed taken {HOLD_LOCK_SECONDS:g} s: held without it'
        time.sleep(0.01)


def hold(question, verdict, now_ms=None, folder=None):
    """#861: hysteresis for a capacity check that flips between OK and NOT OK within seconds, so a "check, then
    start" never starts work just before the answer turns NOT OK again. A NOT OK for a question (kind and MB) holds
    HOLD_SECONDS: within that time the same question answers NOT OK, however the machine reads. The time starts at
    each NOT OK read from the machine (a held one does not extend it). (verdict, why it was held or None, a note or
    None). One caller at a time (the hold lock), so two NOT OKs at once are both kept. A hold file that cannot be
    read (broken, an older format, another owner's, a link, open to others) holds the question asked HOLD_SECONDS from
    when that file last changed (its ctime, which no caller can set), and is written again; so a broken file in a
    folder that cannot be written holds for HOLD_SECONDS, never for good. One that cannot even be looked at is
    ignored, with a note. A time in the future (a clock that went back) holds and starts again; one that cannot start
    again (its folder cannot be written) does not hold, with a note, so no hold lasts past HOLD_SECONDS (#981). No
    folder to keep it in: no hold, with a note (as before #861)."""
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    folder, why = state_folder() if folder is None else given_folder(folder)
    if folder is None:
        return verdict, None, f'no hold of a NOT OK ({why})'
    try:                                        # the folder is opened once: the lock and the file are in the same one
        lock, lock_note = _hold_lock(folder)
        try:
            verdict, why, note = _hold(question, verdict, now_ms, folder)
        finally:
            if lock is not None:
                os.close(lock)
    finally:
        folder.close()
    return verdict, why, '; '.join(n for n in (lock_note, note) if n) or None


def _hold(question, verdict, now_ms, folder):
    busy, broken, notes, path = {}, None, [], folder.at(HOLD_FILE)
    try:
        fd = os.open(HOLD_FILE, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=folder.fd)
        try:
            st = os.fstat(fd)
            if st.st_uid != os.getuid() or st.st_mode & 0o077 or not stat.S_ISREG(st.st_mode):
                raise ValueError('not a private file of this user')
            data = cr.loads(os.read(fd, 1 << 16).decode('utf-8'))
        finally:
            os.close(fd)
        if not (isinstance(data, dict) and data.get('v') == 1 and isinstance(data.get('busy'), dict)
                and all(type(v) is int for v in data['busy'].values())):
            raise ValueError('not a hold file of this version')
        busy = data['busy']
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        try:
            # when it last changed: what it may still hold from
            broken = int(os.lstat(HOLD_FILE, dir_fd=folder.fd).st_ctime * 1000)
        except OSError as e:
            notes.append(f'the hold file {path} cannot be read or looked at ({e.strerror}): ignored')
        else:
            busy = {question: broken}
    at = busy.get(question)
    why = None
    if verdict == 'free' and at is not None and (at > now_ms or now_ms - at < HOLD_SECONDS * 1000):
        why = (f'held: the hold file {path} cannot be read, so a NOT OK of the last {HOLD_SECONDS} s may be in it'
               if broken is not None else
               f'held: NOT OK {max(0, now_ms - at) // 1000} s ago for this question; OK needs {HOLD_SECONDS} s '
               f'without one (a check that flips must not let work start just before it turns NOT OK)')
    future = at is not None and at > now_ms
    if verdict != 'free' or future:
        busy[question] = now_ms                     # a NOT OK read now (or a future state): start again
    keep = {k: v for k, v in busy.items() if 0 <= now_ms - v < HOLD_SECONDS * 1000}
    if keep != busy or broken is not None or verdict != 'free' or future:     # a future one starts again on disk
        try:
            write_cache(folder, HOLD_FILE, {'v': 1, 'busy': keep})  # whole or not at all; old temporary files go too
        except OSError as e:
            notes.append(f'the hold of a NOT OK was not written ({e.strerror or e})')
            if future and verdict == 'free':
                # #981: a future time that cannot start again would hold until the clock comes back (an hour back:
                # an hour held); with nothing written it cannot be bounded to HOLD_SECONDS, so it does not hold
                why = None
                notes.append('a NOT OK in the future (the clock went back) cannot start again here: not held')
    return ('busy' if why else verdict), why, '; '.join(notes) or None


def facts_main(argv, slice_name, queue_root, hq, now=None):
    """The `heavy facts` verb. slice_name, queue_root: heavy's settings; hq: lib/heavy_queue.py (learned costs)."""
    if argv in (['--help'], ['-h']):            # also the installer's self-test: the rule must load from this copy
        try:
            cr.shipped_rule()
        except (OSError, ValueError) as e:
            print(f'heavy facts: the capacity rule cannot be read: {e}', file=sys.stderr)
            return 1
        print('usage: heavy facts [--kind KIND] [--mb MB] [--fresh]\n  one free/v1 object: may one more piece of '
              'work start? exit 0 free, 75 busy, other not available')
        return 0
    opts = options(argv)
    if opts is None:
        print('usage: heavy facts [--kind KIND] [--mb MB] [--fresh]', file=sys.stderr)
        return 2
    try:
        rule = cr.shipped_rule()
        run = f'{RUN_USER}/{os.getuid()}'
        state_dir = hq.Box(slice_name, '', None).state_dir

        def learned(notes):
            out = {}
            for k, spec in rule['kinds'].items():
                if spec['group'] == 'job' and not spec.get('plusDeclared'):
                    out[k] = {'mb': hq.reservation(k[4:] if k.startswith('job-') else k, spec['startMb'], state_dir,
                                                   out=notes.append)[0]}
            return out
        where = {'slice': slice_name, 'learned': learned,
                 'queueDir': os.path.join(queue_root or run + '/heavy-queue', slice_name),
                 'reserveDir': os.environ.get('T3_CAPACITY_RESERVE_DIR') or os.path.join(
                     os.path.expanduser('~'), '.config', 't3-capacity', 'reserve.d')}
        # the machine file: under HEAVY_FACTS_ROOT too (tests: a fake machine, which also fakes all of /proc and /sys)
        mf = hq.machine_file
        settings = capacity_settings(mf, hq.SECTION, ROOT + mf.default_path() if ROOT else None,
                                     os.getuid() if ROOT else 0, slice_name)
        where['holds'], where['reserveDirs'] = settings['holds'], settings['reserveDirs']
        where['user'] = user_name()
        where['key'] = '\n'.join((slice_name, where['queueDir'], where['reserveDir'], where['user'],
                                  json.dumps(where['holds'], sort_keys=True),
                                  json.dumps(where['reserveDirs'], sort_keys=True)))
        given = os.environ.get('HEAVY_FACTS_CACHE')
        if given:
            name = os.path.basename(given)
            folder, why = given_folder(os.path.dirname(os.path.abspath(given)))
        else:
            name = cache_name(where['key'])
            folder, why = cache_folder(run)
        try:
            now = now or datetime.datetime.now(datetime.timezone.utc)
            now_ms = int(now.timestamp() * 1000)
            # Without a cache folder nothing carries the pressure level from one call to the next: start every call
            # "elevated" (the rule's floors only) so the level never drops straight to ok past the hold-down.
            readings, carry = read_cache(folder, name, now_ms, where['key']) if folder else (None, no_carry())
            if readings is None or opts.get('--fresh'):
                readings = read_machine(rule, where, carry, now)
                readings['pressure'] = {g: cr.level(rule, dict(readings['facts'], boxPsi=readings['boxPsi'][g]),
                                                    (carry or {}).get(g), now_ms)[1] for g in ('agent', 'job')}
                if why:
                    readings['notes'].append(why)
                if folder:
                    try:
                        write_cache(folder, name, readings)
                    except OSError as e:
                        readings['notes'].append(f'cache not written ({e.strerror or e})')
        finally:
            if folder:
                folder.close()
        # #951: the caps the caller's work starts under, per call (callers in other cgroups share the cache, which
        # keeps the machine's readings and pressure carry: a key per cap would start each new one "elevated")
        caps = own_caps(os.getuid())
        if caps:
            readings = dict(readings, facts=dict(readings['facts']), notes=list(readings['notes']))
            readings['notes'] += apply_caps(readings['facts'], caps)
        code, out = answer(rule, readings, opts.get('--kind'), int(opts['--mb']) if '--mb' in opts else None,
                           (os.getcwd(),))
    except Exception as e:      # a reading this code did not expect: no answer, never "free"
        print(f'heavy: facts: {e.__class__.__name__}: {e}', file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(out, separators=(',', ':')))
    return code


def ask(heavy, kind=None, mb=None, timeout=ANSWER_SECONDS, fresh=False):
    """For the callers (t3-capacity, agent-slots, heavy's own start, #866): run `heavy facts`, at most `timeout`
    seconds, and judge the answer with free/v1's one reader. (verdict 'free', 'busy' or 'unavailable', reason, the
    object or None). fresh: read the machine now, not the 5 s cache (heavy's start: a job admitted a second ago must
    count)."""
    fc = load('free_contract', 'common/free_contract.py')
    argv = [heavy, 'facts'] + (['--kind', kind] if kind else []) + (['--mb', str(mb)] if mb is not None else []) + \
        (['--fresh'] if fresh else [])
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             start_new_session=True)
    except OSError as e:
        return 'unavailable', f'heavy facts cannot run ({e.strerror})', None
    try:
        out, err = p.communicate(timeout=timeout)
        code = p.returncode
    except subprocess.TimeoutExpired:
        out, err, code = b'', b'', None
    finally:
        try:
            os.killpg(p.pid, signal.SIGKILL)        # nothing it started outlives the answer
        except OSError:
            pass
    if code is None:
        p.communicate()
        return 'unavailable', f'heavy facts gave no answer within {timeout:g} s', None
    verdict, why, obj = fc.judge(code, out, datetime.datetime.now(datetime.timezone.utc))
    if verdict == 'unavailable':
        last = err.decode('utf-8', 'replace').strip().splitlines()[-1:]
        why = f'heavy facts: {why}' + (f' ({last[0][:200]})' if last else '')
    return verdict, why, obj


def question(kind, mb):
    """The hold's key for one question: the same kind and MB is the same question for every caller."""
    return f'{kind or ""}:{"" if mb is None else mb}'


def checked(heavy, kind=None, mb=None, fresh=False, timeout=ANSWER_SECONDS):
    """The one capacity check of t3-capacity and of heavy's own start (#866): ask(), then hold() for the same
    question, so a NOT OK holds HOLD_SECONDS for each of them alike (#861). (verdict, why, the object or None); a
    note of the hold goes into the object's notes."""
    verdict, why, d = ask(heavy, kind, mb, timeout, fresh)
    verdict, held, note = hold(question(kind, mb), verdict)
    if note and d is not None and isinstance(d.get('notes'), list):     # another script's object may have none
        d['notes'].append(note)
    return verdict, held or why, d


def ready(hq, slice_name=None):
    """None when this kit's `heavy facts` can answer at all, else why not: what no wait would fix (the capacity rule
    not shipped with the kit, a machine file whose heavy section is broken, a basic tier machine: #981). heavy asks
    this before it waits for capacity, so a kit or machine that can never answer free refuses at once instead of
    waiting --wait (7200 s) for nothing. slice_name: heavy's slice, whose folder in this user's manager must be
    listable when it is there (read_machine's other structural cause of the basic tier). A caller outside its cgroup
    namespace (own_caps: OUTSIDE_WHY, #998 item 2) is refused too: its own caps can never be read. The basic tier from
    reservations or sessions that cannot be read is not refused here: a person can fix those while a job waits."""
    try:
        cr.shipped_rule()
        mf = hq.machine_file
        capacity_settings(mf, hq.SECTION, ROOT + mf.default_path() if ROOT else None, os.getuid() if ROOT else 0,
                          slice_name)
    except Exception as e:
        return f'{e.__class__.__name__}: {e}'
    uid = os.getuid()
    cgroup = rd.read_machine(rd.RealHost(ROOT))['cgroup']
    tier = cgroup_tier(cgroup, uid)
    if tier == 'basic':
        what = 'no cgroup v2' if cgroup != 'v2' else 'neither a systemd user manager for this user nor user.slice'
        return f'basic tier ({what}): heavy facts is never free on this machine'
    # #998 item 2: a caller outside its cgroup namespace (`0::/..`, the machine tier) is busy for every kind, and
    # no wait changes where it runs
    outside = [c['path'] for c in own_caps(uid) if c['why'] == OUTSIDE_WHY]
    if outside:
        return f'this cgroup ({outside[0]}) {OUTSIDE_WHY}: heavy facts is never free here'
    if tier == 'full' and slice_name:
        box = f'{USERS}/user-{uid}.slice/user@{uid}.service/{slice_path(slice_name)}'
        try:
            os.listdir(ROOT + box)
        except FileNotFoundError:
            pass                                    # no heavy job ran yet: its first scope makes it
        except OSError as e:
            return f'basic tier ({box} cannot be listed: {e.strerror}): heavy facts is never free on this machine'
    return None


_LIB = os.path.dirname(os.path.realpath(__file__))
sys.path[:0] = [] if _LIB in sys.path else [_LIB]
import kit_load  # noqa: E402  (the kit's one loader, #800 item 5)


def load(name, rel):
    """A kit file of this kit (kit_load.load: the installed version folder, or next to the kit in the repository)."""
    return kit_load.load(name, rel, __file__)


cr = load('capacity_rule', 'lib/capacity_rule.py')
rd = load('capacity_readings', 'lib/capacity_readings.py')
