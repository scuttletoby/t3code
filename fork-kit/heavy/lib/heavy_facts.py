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
kind for its name or else unknownKinds.job; demand from the job's scope) and T3's agent scopes (`t3code-*.scope`
under an `app-t3code*` slice; a scope has no kind label, so it counts as unknownKinds.other: a writer), plus the
other users' heavy jobs and agent scopes read from their user managers' cgroups (unknownKinds.job and writers).
Full tier: cgroup v2 and this user's systemd user manager. Machine tier: cgroup v2 and user.slice, but no user
manager of this user's (a CI runner's user: no lingering, no login session): it has no agent or heavy job scopes of
its own, every user's are counted as above, and so are every person's reservations (other_reserve_dirs: their
~/.config/t3-capacity/reserve.d; one that cannot be read makes it basic), so it gets the same answer as a caller with
a user manager (a note and the reason say "machine-wide numbers"). Basic tier (no cgroup v2, or neither a user manager nor user.slice):
agents and other users' work cannot be counted, so the answer is never free (a note says why). A signal that is
there but unreadable is "elevated": then only the rule's floors admit, never the budget; facts that cannot be read
admit nothing.

Reservations: lines `<MB> <folder> [until=YYYY-MM-DD]` in *.conf files of $T3_CAPACITY_RESERVE_DIR, else
~/.config/t3-capacity/reserve.d. A folder matches itself and what is below it, never a sibling that shares its
first letters (`/x/app` is not `/x/app-x`); its real path (links resolved) matches too. Each holds MB less what
its user's processes working in the folder use (RssAnon + RssShmem + VmSwap), and less what the agents and heavy
jobs with a process working in it still owe the rule (their cost less their demand: that room is held as their debt
already, so it is counted once, #923). A process counts toward one reservation only (the deepest folder holding it,
then the first by name), and a piece's debt is split over its processes by their use, so no debt is taken off two
reservations (#945). It does not hold back a caller whose working folder is in it; past its until date, or broken,
it holds nothing (a note says so).

Readings are kept 5 seconds in /run/user/<uid>/heavy-facts-v<version>-<hash of the settings>.json, one file per
version and settings, so callers of one user with other settings (slice, queue, reservations folder) or another
kit version never read each other's file and each one's hold-down steps down on its own (#945) ($HEAVY_FACTS_CACHE
names one file instead; --fresh reads again; a user without /run/user/<uid>, as one with no user manager, uses
$TMPDIR or /tmp /heavy-facts-<uid>/, a folder only it can use: one that is not, or no such temporary folder, is no
cache), written whole through a rename; a cache that is old, from the future, broken, of another version, of other
settings or not this user's own is read again. The pressure level (and the swap free its trend needs; a state
without it waits one more read before it can step down) carries from one reading to the next (the rule's
hysteresis) while the last one is at most 5 minutes old; without a cache folder, with a cache file that is there but
cannot be used, or with no file of its own yet but another caller's written in the last 5 minutes beside it (another
version after an update, other settings), nothing can carry, so the level starts "elevated" (it never drops straight
to ok). Cache files of other settings or versions older than 5 minutes are removed. Learned
job costs: heavy's peaks (lib/heavy_queue.py reservation(); missing or broken: start). A job owes its kind's cost,
not heavy's own reservation for it (heavy's queue holds that one back itself).
"""
import datetime, errno, fcntl, fnmatch, hashlib, json, math, os, pwd, re, signal, stat, subprocess, sys, time

CACHE_SECONDS, CARRY_SECONDS, ANSWER_SECONDS, STALE_TMP_SECONDS = 5, 300, 5, 60
EXIT_FREE, EXIT_BUSY, EXIT_ERROR = 0, 75, 1
CACHE_VERSION = 3                               # 3: reservations carry owedMb (#923)
AGENT_SCOPE_RE = re.compile(r'^t3code-[A-Za-z0-9_.:\\-]+\.scope\Z')
SCOPE_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}\.scope\Z')
TICKET_RE = re.compile(r'^t-[0-9]{1,15}-[0-9]{1,10}\.json\Z')
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,59}\Z')
USER_RE = re.compile(r'^user-([0-9]{1,10})\.slice\Z')
HOLD_LABEL_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,39}\Z')
CG_PART_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}\Z')
CACHE_FILE_RE = re.compile(r'^heavy-facts(-v[0-9]{1,4}-[0-9a-f]{16})?\.json\Z')   # an older kit's: heavy-facts.json
CACHE_KEYS = {'v', 'key', 'atMs', 'tier', 'facts', 'boxPsi', 'reservations', 'learned', 'carry', 'notes', 'load1',
              'queue', 'pressure'}
USERS = '/sys/fs/cgroup/user.slice'
RUN_USER = '/run/user'                          # the cache's folder: /run/user/<uid>
TMP_ROOT = None                                 # without it: <TMP_ROOT>/heavy-facts-<uid> (None: temp_root())
TMP_DIRS = ('/tmp',)                            # temp_root() after $TMPDIR


def test_env(name):
    """A test-only setting (#800): read only while HEAVY_TESTING=1 is set too, so one that leaks into an agent's or a
    service's environment (HEAVY_FACTS_ROOT, AGENT_SLOTS_ALIVE, T3_CAPACITY_HEAVY, AGENT_SLOTS_HEAVY) changes
    nothing a real caller sees."""
    return os.environ.get(name) if os.environ.get('HEAVY_TESTING') == '1' else None


ROOT = test_env('HEAVY_FACTS_ROOT') or ''       # tests: /proc and /sys are read under this folder
BASIC_WHY = 'basic tier: no cgroup v2, or neither a systemd user manager for this user nor user.slice, so agents and ' \
            'other users\' work cannot be counted: never free here'
MACHINE_WHY = 'machine tier: no systemd user manager for this user, so it has no agents or heavy jobs of its own; ' \
              'judged on the machine-wide numbers (memory, swap, pressure, holds, every person\'s reservations, every ' \
              'user\'s heavy jobs and agents)'


def _read(path, limit=1 << 16):
    with open(ROOT + path, 'rb') as f:
        return f.read(limit).decode('ascii', 'replace')


def load1():
    try:
        return float(_read('/proc/loadavg').split()[0])
    except (OSError, ValueError, IndexError):
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


def reservation_files(cfg_dir, today, owner=None):
    """The reservations in one reserve.d: ([{name, prefixes, mb, useMb, owedMb, uid}], notes, blind). owner: None
    for the caller's own folder (missing or unreadable files hold nothing, as before); (name, uid) for another
    user's (the machine tier): a folder or file that is there but cannot be read is 'blind' (never free), and only
    plain files are read (no FIFO can stop the reading)."""
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
                if owner and not stat.S_ISREG(os.fstat(fd).st_mode):
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
                if not (words[0].isdigit() and len(words) >= 2 and words[1].startswith('/')):
                    raise ValueError('not "<MB> <absolute folder>"')
                if until and datetime.date.fromisoformat(until[0]) < today:
                    notes.append(f'reservation {tag}{f}:{n} ended {until[0]}: holds nothing')
                    continue
            except ValueError as e:
                notes.append(f'reservation {tag}{f}:{n} ignored ({e})')
                continue
            out.append({'name': tag + f[:-5], 'prefixes': folder_prefixes(words[1]), 'mb': int(words[0]),
                        'useMb': 0, 'owedMb': 0, 'uid': owner[1] if owner else os.getuid()})
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


def other_reserve_dirs(uid):
    """For the machine tier (#844): every other person's reserve.d, ([(user name, uid, folder)], blind). The people
    are the /etc/passwd entries in login_range() and every user.slice user in it; a user with a session but no home
    that can be found is 'blind'. Root and system users are not read (their homes are closed to others)."""
    lo, hi = login_range()
    homes, blind = {}, []
    try:
        for line in _read('/etc/passwd', 1 << 22).splitlines():
            f = line.split(':')
            if len(f) >= 7 and f[2].isdigit() and lo <= int(f[2]) <= hi and int(f[2]) != uid:
                homes.setdefault(int(f[2]), (f[0], f[5]))
    except OSError:
        pass
    try:
        names = os.listdir(ROOT + USERS)
    except OSError:
        names = []
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
    out = [(n, u, os.path.join(ROOT + h, '.config', 't3-capacity', 'reserve.d'))
           for u, (n, h) in sorted(homes.items()) if os.path.isabs(h) and h != '/']
    return out, blind


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


def _hold_cgroup(value):
    parts = value.split('/')
    if len(parts) > 16 or any(p in ('.', '..') or not CG_PART_RE.match(p) for p in parts):
        return 'must be a folder below the cgroup root, like "ci.slice/ci-runners.slice"'
    if parts[0] == 'user.slice':
        return 'is below user.slice, whose heavy jobs and agents are counted already (it would count them twice)'
    return None


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


def capacity_settings(mf, section_spec, path=None, owner_uid=0):
    """heavy.capacity of the machine file (mf: fork-kit/common/machine_file.py): {'holds': [...]}. A missing file,
    section or key is no holds; anything broken raises mf.MachineFileError naming the key."""
    where = path or mf.default_path()
    section = mf.read_section('heavy', path, owner_uid, optional=True)
    if section is None:
        return {'holds': []}
    section = mf.fields(section, section_spec, f'{where}: heavy')
    cap = mf.fields(section.get('capacity') or {}, {'holds': (list, [], None)}, f'{where}: heavy.capacity')
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
        holds.append(h)
    return {'holds': holds}


def user_name():
    try:
        return pwd.getpwuid(os.getuid()).pw_name
    except KeyError:
        return str(os.getuid())


def held_cgroups(host, holds, user):
    """The machine file's holds: a cgroup outside the user managers (a CI runners' slice, say) that is in use keeps
    its budget less what it uses, as a reservation, for every user but its own (`users`). ([reservation], notes)"""
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


def read_machine(rule, where, carry, now):
    """This machine's readings now: what the cache keeps (the same for every caller with these settings)."""
    notes = []
    uid = os.getuid()
    host = rd.RealHost(ROOT)
    m = rd.read_machine(host)
    manager = f'{USERS}/user-{uid}.slice/user@{uid}.service'
    v2 = m['cgroup'] == 'v2'
    mine = v2 and os.path.isdir(ROOT + manager)     # this user's own agents and heavy jobs can be read
    # #844: no user manager of its own (a CI runner's user): nothing of its own to count, everyone else's as usual
    tier = 'full' if mine else 'machine' if v2 and os.path.isdir(ROOT + USERS) else 'basic'
    full = tier != 'basic'
    heavy_rel = '/' + slice_path(where['slice'])
    heavy_box = manager + heavy_rel
    pieces, running, waiting, owners = [], 0, 0, {}
    for t in tickets(where['queueDir']):
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
    res, more, _ = reservation_files(where['reserveDir'], now.date())
    notes += more
    if tier == 'machine':
        # #945: a caller with no user manager is often another user (a CI runner, root), whose own reserve.d is not
        # the owner's: read every person's, so it never takes the room they keep. One it cannot read: never free.
        dirs, blind = other_reserve_dirs(uid)
        for name, u, folder in dirs:
            more_res, more, cannot = reservation_files(folder, now.date(), (name, u))
            res += more_res
            notes += more
            blind += cannot
        if blind:
            full, tier = False, 'basic'
            notes += blind
    # #923, #945: a piece working in a reservation's folder owes its debt once, not there too, and never in two
    count_in_reservations(res, owners, {i: debt(rule, learned, pieces[i], m['totalMb']) for i in set(owners.values())})
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
                      'pieceCaps': full, 'swap': m['swap'], 'psi': m['psi'], 'osLevel': m['osLevel'],
                      'pieces': pieces},
            'boxPsi': box_psi, 'reservations': res, 'learned': learned, 'carry': carry,
            'notes': notes, 'load1': load1(), 'queue': {'running': running, 'waiting': waiting, 'agents': agents}}


def no_carry():
    """The pressure state when none can be carried on safely: "elevated" (the rule's floors only), so the level
    never drops straight to ok past the hold-down."""
    return {g: {'level': 'elevated', 'calmSinceMs': None} for g in ('agent', 'job')}


def read_cache(path, now_ms, key):
    """(readings while fresh, else None; the pressure state to carry on, else None). Anything odd is not trusted. No
    file, or one older than CARRY_SECONDS: nothing to carry (None). A file that is there but cannot be used (another
    version, as once after an update or while an old heavy writes it too; other settings, broken, from the future,
    not this user's own): no_carry(), so the hold-down is never skipped (#945). No file of its own yet, but another
    cache file of this user's beside it written in the last CARRY_SECONDS (another version just after an update, or
    other settings): no_carry() too; from its own file on, it steps down by its own hold-down."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except FileNotFoundError:
        return None, (no_carry() if other_caches(path, recent=True) else None)
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


def other_caches(path, recent):
    """The other cache files of this user's (any version or settings, an older kit's heavy-facts.json too) beside
    `path`, written in the last CARRY_SECONDS (recent) or before (not recent). A file from the future is recent."""
    folder, own_name = os.path.split(path)
    try:
        names = os.listdir(folder or '.')
    except OSError:
        return []
    out = []
    for name in names:
        if name == own_name or not CACHE_FILE_RE.match(name):
            continue
        try:
            st = os.lstat(os.path.join(folder, name))
        except OSError:
            continue
        if stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and \
                (time.time() - st.st_mtime <= CARRY_SECONDS) == recent:
            out.append(os.path.join(folder, name))
    return out


def write_cache(path, data):
    """Whole or not at all: a unique temporary file renamed over the cache. A temporary file left by a writer that
    stopped half-way is removed once it is a minute old, and this user's cache files of other settings or versions
    once they are past the carry window (they can carry nothing then), so they do not pile up."""
    folder, base = os.path.split(path)
    for name in os.listdir(folder or '.'):
        if name.startswith(base + '.') and name.endswith('.tmp'):
            try:
                if time.time() - os.lstat(os.path.join(folder, name)).st_mtime > STALE_TMP_SECONDS:
                    os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    for old in other_caches(path, recent=False):    # other settings or versions, past the carry window: no use
        try:
            os.unlink(old)
        except OSError:
            pass
    tmp = f'{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, json.dumps(data, separators=(',', ':')).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


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
    free = not basic and (d['admit'] if kind else d['level'] == 'ok') and (
        need_mb is None or d['freeMb'] is not None and need_mb <= d['freeMb'])
    fits = {}
    for k, spec in rule['kinds'].items():
        g = spec['group']
        fits[k] = 0 if basic else cr.fill(rule, facts_for(readings, g, cwds),
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
           'queue': readings['queue'], 'cpu': {'cores': facts['cpuCores'], 'load1': readings['load1']},
           'reservations': [{'name': r['name'], 'mb': r['mb'], 'useMb': math.floor(r['useMb']),
                             'owedMb': math.floor(r['owedMb']), 'own': own(r, cwds)} for r in readings['reservations']],
           'notes': list(readings['notes'])}
    if not free:
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


def cache_path(run, key):
    """(the cache file, None) or (None, why there is none). /run/user/<uid> when it is there; else (#844: a user with
    no user manager and no login session has none) a folder only this user can use in the temporary folder, made if
    missing: one that is a link, another user's or open to others (planted first) is no cache, and so no carry."""
    if os.path.isdir(run):
        return os.path.join(run, cache_name(key)), None
    root = TMP_ROOT or temp_root()
    if not root:
        return None, 'no cache: no temporary folder this user can write ($TMPDIR, /tmp): every call starts "elevated"'
    folder = os.path.join(root, f'heavy-facts-{os.getuid()}')
    try:
        try:
            os.mkdir(folder, 0o700)
        except FileExistsError:
            pass
        st = os.lstat(folder)
    except OSError as e:
        return None, f'no cache: {folder} cannot be made ({e.strerror or e}): every call starts "elevated"'
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        return None, f'no cache: {folder} is not a folder only this user can use: every call starts "elevated"'
    return os.path.join(folder, cache_name(key)), None


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
        where['holds'] = capacity_settings(mf, hq.SECTION, ROOT + mf.default_path() if ROOT else None,
                                           os.getuid() if ROOT else 0)['holds']
        where['user'] = user_name()
        where['key'] = '\n'.join((slice_name, where['queueDir'], where['reserveDir'], where['user'],
                                  json.dumps(where['holds'], sort_keys=True)))
        cache, why = (os.environ['HEAVY_FACTS_CACHE'], None) if os.environ.get('HEAVY_FACTS_CACHE') else \
            cache_path(run, where['key'])
        now = now or datetime.datetime.now(datetime.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        # Without a cache folder nothing carries the pressure level from one call to the next: start every call
        # "elevated" (the rule's floors only) so the level never drops straight to ok past the hold-down.
        readings, carry = read_cache(cache, now_ms, where['key']) if cache else (None, no_carry())
        if readings is None or opts.get('--fresh'):
            readings = read_machine(rule, where, carry, now)
            readings['pressure'] = {g: cr.level(rule, dict(readings['facts'], boxPsi=readings['boxPsi'][g]),
                                                (carry or {}).get(g), now_ms)[1] for g in ('agent', 'job')}
            if cache:
                try:
                    write_cache(cache, readings)
                except OSError as e:
                    readings['notes'].append(f'cache not written ({e.strerror or e})')
            elif why:
                readings['notes'].append(why)
        code, out = answer(rule, readings, opts.get('--kind'), int(opts['--mb']) if '--mb' in opts else None,
                           (os.getcwd(),))
    except Exception as e:      # a reading this code did not expect: no answer, never "free"
        print(f'heavy: facts: {e.__class__.__name__}: {e}', file=sys.stderr)
        return EXIT_ERROR
    print(json.dumps(out, separators=(',', ':')))
    return code


def ask(heavy, kind=None, mb=None, timeout=ANSWER_SECONDS):
    """For the callers (t3-capacity, agent-slots): run `heavy facts`, at most `timeout` seconds, and judge the answer
    with free/v1's one reader. (verdict 'free', 'busy' or 'unavailable', reason, the object or None)."""
    fc = load('free_contract', 'common/free_contract.py')
    argv = [heavy, 'facts'] + (['--kind', kind] if kind else []) + (['--mb', str(mb)] if mb is not None else [])
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


def load(name, rel):
    """A kit file: in the installed version folder, or next to the kit in the repository (fork-kit/<rel>)."""
    import importlib.machinery, importlib.util
    kit = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    for path in (os.path.join(kit, rel), os.path.join(os.path.dirname(kit), rel)):
        if os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader(name, path)
            mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, loader))
            loader.exec_module(mod)
            return mod
    raise ImportError(f'{rel} not found in {kit} or next to it')


cr = load('capacity_rule', 'lib/capacity_rule.py')
rd = load('capacity_readings', 'lib/capacity_readings.py')
