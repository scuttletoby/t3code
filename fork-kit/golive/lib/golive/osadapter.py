"""The OS adapter: the few things the kit asks of the service manager. Only systemd (user units) exists yet; on
any other system for_this_os() refuses with a plain message until its adapter is written. The mount list
(mounts, mount_points) is read on Linux only; elsewhere it raises Unsupported, so a caller that deletes refuses.

The boot service is upstream's own unit (apps/server/src/cloud/bootService.ts: `t3code.service`, with
`Environment=T3CODE_HOME=<home>`). Commands are found on PATH; only tests change SYSTEMCTL and SYSTEMD_RUN.
"""
import collections, os, re, shutil, subprocess, sys

UNIT = 't3code.service'
SYSTEMCTL = 'systemctl'
SYSTEMD_RUN = 'systemd-run'
TIMEOUT = 30
CGROUP_FILE = '/proc/self/cgroup'  # tests only change it
MOUNTINFO = '/proc/self/mountinfo'  # tests only change it
_UNIT_NAME = re.compile(r'^[A-Za-z0-9_.@-]{1,200}$')
_PROPERTY = re.compile(r'^[A-Za-z]{1,40}=[A-Za-z0-9.]{1,40}$')


class Unsupported(Exception):
    pass


class Unreadable(Unsupported):
    """The mount list is there, but reading it failed this time (too many open files, out of memory, ...)."""


class AdapterError(Exception):
    pass


# One line of the mount list: mount id and parent id (fields 1 and 2), device major:minor, the folder of that filesystem
# mounted (field 4: '/' unless a bind of a subfolder), mount point (field 5)
Mount = collections.namedtuple('Mount', 'id parent dev root point')


def unquote_systemd(raw):
    """Upstream's unquoteSystemdValue (serviceProtocol.ts)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return raw.replace('%%', '%')


def unit_home(unit_text):
    """The T3 home a unit serves (its T3CODE_HOME), or None."""
    m = re.search(r'^Environment=T3CODE_HOME=(.*)$', unit_text, re.M)
    return unquote_systemd(m.group(1)) if m else None


class SystemdUser:
    name = 'systemd (user)'

    def _run(self, argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AdapterError(f'{argv[0]} failed: {e}') from None
        if p.returncode != 0:
            raise AdapterError(f'{" ".join(argv[:4])} exited {p.returncode}: {p.stderr.strip()[:300]}')
        return p.stdout

    def service(self):
        """{'loaded', 'active', 'enabled', 'mainPid', 'unitPath', 'home'} of the boot service; read only."""
        out = self._run([SYSTEMCTL, '--user', 'show', UNIT,
                         '--property=LoadState,ActiveState,UnitFileState,FragmentPath,MainPID'])
        props = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        unit_path = props.get('FragmentPath') or None
        home = None
        if unit_path:
            try:
                with open(unit_path, encoding='utf-8') as f:
                    home = unit_home(f.read())
            except OSError:
                home = None
        try:
            main_pid = int(props.get('MainPID', '0'))
        except ValueError:
            main_pid = 0
        return {'loaded': props.get('LoadState') == 'loaded', 'active': props.get('ActiveState') == 'active',
                'enabled': props.get('UnitFileState') == 'enabled', 'mainPid': main_pid or None,
                'state': props.get('ActiveState'),
                'unitPath': unit_path, 'home': home}

    def inside_service(self):
        """True when this process runs inside the boot service (a T3 terminal or agent): stopping the service would
        kill it half-way, so a command that stops T3 must run detached instead. Unreadable: True."""
        try:
            with open(CGROUP_FILE, encoding='utf-8') as f:
                return any(UNIT in line.rstrip('\n').split(':', 2)[-1].split('/') for line in f)
        except OSError:
            return True  # cannot tell: treat it as inside (fail closed)

    def own_unit(self):
        """The systemd unit this process runs in (the last field of its cgroup path, e.g. `name.service`), or
        None when that cannot be read."""
        try:
            with open(CGROUP_FILE, encoding='utf-8') as f:
                fields = [line.rstrip('\n').split(':', 2) for line in f]
        except OSError:
            return None
        # systemd's own hierarchy: cgroup v2's single line, or v1's name=systemd line
        names = {f[2].rsplit('/', 1)[-1] for f in fields if len(f) == 3 and f[1] in ('', 'name=systemd')}
        return names.pop() if len(names) == 1 and '' not in names else None

    def unit_active(self, unit):
        """True while a transient unit (the copy's guard) runs; read only."""
        if not _UNIT_NAME.match(unit):
            raise AdapterError(f'not a safe unit name: {unit!r}')
        out = self._run([SYSTEMCTL, '--user', 'show', unit, '--property=ActiveState'])
        return any(line.strip() in ('ActiveState=active', 'ActiveState=activating') for line in out.splitlines())

    def stop(self):
        self._run([SYSTEMCTL, '--user', 'stop', UNIT])

    def start(self):
        self._run([SYSTEMCTL, '--user', 'start', UNIT])

    def disable(self):
        """Not started at boot (`back` keeps T3 off a half-restored home; upstream's update enables it again)."""
        self._run([SYSTEMCTL, '--user', 'disable', UNIT])

    def run_detached(self, unit, argv, guard_after_seconds=None, guard_argv=None, properties=()):
        """Run argv as a transient user unit that outlives its caller (a restart of T3 kills anything inside it),
        with the unit `properties` (Name=value). With a guard, a second transient timer runs guard_argv after
        guard_after_seconds, whatever happens to the first (a crash or a reboot cannot leave T3 down)."""
        for name in (unit, f'{unit}-guard'):
            if not _UNIT_NAME.match(name):
                raise AdapterError(f'not a safe unit name: {name!r}')
        for prop in properties:
            if not _PROPERTY.match(prop):
                raise AdapterError(f'not a safe unit property: {prop!r}')
        if guard_argv is not None:
            if not (isinstance(guard_after_seconds, int) and guard_after_seconds > 0):
                raise AdapterError('a guard needs a time in whole seconds')
            self._run([SYSTEMD_RUN, '--user', '--quiet', '--collect', f'--unit={unit}-guard',
                       f'--on-active={guard_after_seconds}s', '--timer-property=AccuracySec=1s', '--', *guard_argv])
        self._run([SYSTEMD_RUN, '--user', '--quiet', '--collect', f'--unit={unit}',
                   *(f'--property={prop}' for prop in properties), '--', *argv])


def _unescape(field):
    return os.fsdecode(re.sub(rb'\\([0-7]{3})', lambda m: bytes([int(m.group(1), 8) & 0xFF]), field))


def mounts():
    """Every mount this process sees, in the list's order, as Mount rows, octal escapes undone. Linux
    /proc/self/mountinfo only. Raises Unsupported when there is no list to read (another OS, no /proc, an empty or
    garbled list), Unreadable when reading it failed this time (the OSError named): a caller that deletes or decides
    by it must refuse."""
    if not sys.platform.startswith('linux'):
        raise Unsupported(f'the go-live kit cannot read the mount list on {sys.platform} yet')
    try:
        with open(MOUNTINFO, 'rb') as f:
            lines = f.read().splitlines()
    except FileNotFoundError as e:
        raise Unsupported(f'the mount list {MOUNTINFO} cannot be read: {e}') from None
    except OSError as e:
        raise Unreadable(f'reading the mount list {MOUNTINFO} failed: {e}') from None
    out = []
    for line in lines:
        fields = line.split(b' ')
        if len(fields) < 5 or not fields[4].startswith(b'/') or not (fields[0].isdigit() and fields[1].isdigit()):
            raise Unsupported(f'the mount list {MOUNTINFO} has a line the kit cannot read: {line[:200]!r}')
        out.append(Mount(int(fields[0]), int(fields[1]), os.fsdecode(fields[2]), _unescape(fields[3]),
                         _unescape(fields[4])))
    if not out:
        raise Unsupported(f'the mount list {MOUNTINFO} is empty')
    return out


def mount_points():
    """Every mount point this process sees (field 5 of mounts()), a bind mount of a folder on the same filesystem
    too. Raises Unsupported as mounts() does: a caller about to delete a folder must then refuse."""
    return [m.point for m in mounts()]


def source(path, entries):
    """(device, folder inside that filesystem) that `path` (a real path) is, by the mount on top at it. A bind of
    `<home>/userdata` at `/b` makes `/b/x` ('8:1', '<home>/userdata/x'). None when no mount holds it (a list without
    '/'). The top is found by the mount tree (Mount.id, Mount.parent), never by the list's order: a moved mount keeps
    its old place in the list, and MOVE_MOUNT_BENEATH or propagation puts a later mount under an earlier one. From the
    one mount at or above `path` with no parent in the list, go into its child mount at or above `path` with the
    shortest mount point (it hides any child below it; a mount on the same point is a child of the one it covers),
    until there is none. Raises Unsupported when the tree cannot tell: more than one such mount without a parent (a
    parent missing from the list), a parent that does not hold the child's point, a cycle, two children on one
    point, a mount id twice: a caller must refuse."""
    def cannot_tell(why):
        return Unsupported(f'the mount list {MOUNTINFO} cannot tell which mount is on top at {path}: {why}')
    ids = [m.id for m in entries]
    if len(set(ids)) != len(ids):
        raise cannot_tell('a mount id is listed twice')
    cover = {}
    for m in entries:
        point = os.path.normpath(m.point)
        if os.path.commonpath([path, point]) == point:
            cover[m.id] = m._replace(point=point)
    if not cover:
        return None
    roots = [m for m in cover.values() if m.parent == m.id or m.parent not in ids]
    if len(roots) != 1:
        raise cannot_tell(f'{len(roots)} of its mounts have no parent in the list, not one (a parent missing)')
    for m in cover.values():
        seen = set()
        while m is not roots[0]:
            if m.id in seen:
                raise cannot_tell(f'mount {m.id} is its own ancestor (a cycle)')
            seen.add(m.id)
            up = cover.get(m.parent)
            if up is None or len(up.point) > len(m.point):
                raise cannot_tell(f'the parent {m.parent} of mount {m.id} ({m.point}) does not hold its point')
            m = up
    top = roots[0]
    while True:
        kids = [m for m in cover.values() if m.parent == top.id and m is not top]
        if not kids:
            return top.dev, os.path.normpath(os.path.join(top.root, os.path.relpath(path, top.point)))
        shortest = min(len(m.point) for m in kids)
        kids = [m for m in kids if len(m.point) == shortest]
        if len(kids) > 1:
            raise cannot_tell(f'mounts {", ".join(str(m.id) for m in kids)} on one point have one parent {top.id}')
        top = kids[0]


def for_this_os():
    if not sys.platform.startswith('linux'):
        raise Unsupported(f'the go-live kit has no service adapter for {sys.platform} yet; it changes nothing here')
    if shutil.which(SYSTEMCTL) is None or shutil.which(SYSTEMD_RUN) is None:
        raise Unsupported('the go-live kit needs systemd (systemctl and systemd-run); none was found')
    return SystemdUser()
