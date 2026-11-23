"""pc-agents: the facts, the files this kit owns and the checks, shared by `pc-agents setup` and `pc-agents check`.

Every path is read and written under ROOT, which is '' (the real machine) except in the tests, where
PC_AGENTS_ROOT names a fake one; root never gets a fake one. Standard library only."""
import hashlib, ipaddress, json, os, re, signal, stat, subprocess, time

import machine_file

KIT = 'pc-agents'
ROOT = os.environ.get('PC_AGENTS_ROOT', '')
if ROOT and os.geteuid() == 0:
    raise SystemExit('pc-agents: PC_AGENTS_ROOT is for the tests only, never for root; unset it')
OWNER = os.geteuid() if ROOT else 0   # who must own every file the kit writes
HOME, USER = '/home/agent', 'agent'
NOLOGIN = '/usr/sbin/nologin'   # the agent's login shell: no shell for `wsl -u agent` or su; T3 gets SHELL from its unit
OPT, STATE = '/opt/pc-agents', '/etc/pc-agents'
RECORD = STATE + '/installed.tsv'   # sha256 <TAB> path of every file this kit wrote (a path may have two mid-write)
PENDING = STATE + '/restart-pending'   # setup changed something T3 runs with: restart T3 before removing this
BOOT_ID = '/proc/sys/kernel/random/boot_id'   # the marker holds this boot's id and CLOCK_MONOTONIC, like systemd's
AS_AGENT = 'pc-agents-as-agent'   # name prefix of `pc-agents as-agent`'s transient units (check --enforce stops them)
SETTLE, SETTLE_STEP = 15, 1   # after a (re)start, setup watches T3 this many seconds, every SETTLE_STEP, before done
LOCK_WAIT, LOCK_STEP = 300, 5   # setup retries a dpkg or apt-get step that found a lock held this long, every LOCK_STEP
LOADED = '/run/pc-agents/firewall.loaded'   # sha256 of the firewall file and `nft list` of it, saved when loaded
T3 = OPT + '/t3'   # T3's runtime, downloaded, checked and unpacked by root (never run by it): the agent cannot change it
UNIT, WALLS, UPDATE = 't3code-agents.service', 'pc-agents-walls', 'pc-agents-update'
UNITS = (UNIT, WALLS + '.service', WALLS + '.timer', UPDATE + '.service', UPDATE + '.timer')
TIMERS = (WALLS + '.timer', UPDATE + '.timer')   # enabled and running with T3 (the update one does nothing when off)
UPDATE_STATE = STATE + '/auto-update.json'   # the automatic update's journal and last result (root's, never the agent's)
LOCK = '/run/pc-agents/setup.lock'   # setup and the automatic update take it: never two at once
KEEP_VERSIONS = 3   # T3 version folders kept after an automatic update: the new one, the one before (rollback), one more
UPDATE_TRIES = 3   # a switch to a new version stopped half-way this often is rolled back, not finished
SETUP_TRIES = 3   # a release whose switch failed for another reason than T3 itself this often is not tried again
QUIET_WAIT_DAYS = 7   # an update that waited this long (for a quiet point, or for GitHub's rate limit) is an alert
DB_DIR = '/.t3/userdata'   # under HOME: T3's state database (T3CODE_HOME=HOME/.t3; apps/server/src/config.ts)
DB_FILES = ('statev2.sqlite', 'statev2.sqlite-wal', 'statev2.sqlite-shm', 'state.sqlite', 'state.sqlite-wal',
            'state.sqlite-shm')   # the database and its write-ahead log, and the older one a start imports
DB_BACKUP = '/var/lib/pc-agents/db-backup'   # root's alone: T3's database as it was before the last switch
# root's alone: T3's database as a rollback found it, set aside before the copy is put back (a folder per rollback,
# DB_REPLACED-<UTC time>-<version>; the kit never removes one)
DB_REPLACED = '/var/lib/pc-agents/db-replaced'
HTTP_WAIT, HTTP_STEP = 180, 5   # after an update's restart, T3 must answer with its new version within HTTP_WAIT s
GITHUB_API = 'https://api.github.com'
# The pinned Sigstore public-good roots (Fulcio's certificate authority, Rekor's log key) the attestation check trusts
TRUST = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'sigstore-trust.json')
RELEASE_WORKFLOW = 'fork-release.yml'   # the only workflow whose builds an automatic update installs, from main
RELEASE_CALLERS = ('fork-release.yml', 'fork-follow.yml')   # what may run it: itself, or fork-follow's release job
ARCHIVE_MAX, SUMS_MAX, JSON_MAX = 1 << 30, 1 << 16, 16 << 20   # largest download the kit takes of each (bytes)
UNPACKED_MAX = 4 << 30   # the most an archive may unpack to (bytes)
CHECK = OPT + '/current/sbin/pc-agents'
WALLS_PY = OPT + '/current/walls/fork-fleet-walls.py'
# Walls of fork-fleet-walls/wsl2 (and /linux) that are about the fleet launcher's job units. Here the job unit is T3's
# own unit, whose sandbox this kit writes and checks itself (the unit file must be exactly the kit's, with no drop-in).
UNIT_WALLS = {'mounts', 'sockets', 'userns'}
# The kinds of machine (the machine file's "platform"): a WSL2 distro of its own (the default: a machine file from
# before the key) or a plain Linux box that is also the owner's (issue #1027). On linux the WSL steps are left out
# (wsl.conf, the binfmt mask, the /mnt/wsl and /run/WSL hides, the host's own resolv.conf) and so are the masks of the
# owner's cron, at and ssh: T3's unit cannot use cron or at (NoNewPrivileges: crontab and at are setgid), and sshd
# refuses the agent (SSHD_CONF). Every other wall is the same on both.
PLATFORMS = ('wsl2', 'linux')
MASKS = ['/etc/binfmt.d/WSLInterop.conf'] + ['/etc/systemd/system/' + u for u in
                                            ('cron.service', 'atd.service', 'ssh.service', 'ssh.socket')]
SSHD_CONF = '/etc/ssh/sshd_config.d/pc-agents.conf'   # linux: sshd refuses the agent (a key it wrote, a forward)
# Sockets that hand out root (a container engine's API): the agent must not be able to reach one (linux and wsl2)
ROOT_SOCKETS = ('/run/docker.sock', '/var/run/docker.sock', '/run/containerd/containerd.sock',
                '/run/podman/podman.sock')
# A helper the agent may run through sudo (sudoHelpers): an absolute plain path, each folder named, then plain
# arguments (or "" for none: sudo's own way to say no arguments). No wildcard, no ALL, no `..`, no list.
HELPER = re.compile(r'(/(?!\.\.?(?:/| |$))[A-Za-z0-9._+-]+)+( (""|(?!ALL( |$))[A-Za-z0-9._=:/@+-]+))*')
# Every folder systemd reads system units and drop-ins from (systemd.unit(5)); the kit's units live in the first.
UNIT_DIRS = ('/etc/systemd/system', '/etc/systemd/system.control', '/run/systemd/system.control',
             '/run/systemd/transient', '/run/systemd/generator.early', '/etc/systemd/system.attached',
             '/run/systemd/system', '/run/systemd/system.attached', '/run/systemd/generator',
             '/usr/local/lib/systemd/system', '/usr/lib/systemd/system', '/lib/systemd/system',
             '/run/systemd/generator.late')
BLOCKED4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
            '224.0.0.0/4', '240.0.0.0/4')
BLOCKED6 = ('fc00::/7', 'fe80::/10', 'ff00::/8')
MODES = ('approval-required', 'auto-accept-edits', 'auto', 'full-access')
# Only settings every T3 the kit can run knows (T3 does not start on a policy naming a setting it lacks). Updates are
# off through limits.updates (all versions; the server's self-update refuses), not the newer automaticUpdates.
KIT_SETTINGS = {'linkedEnvironmentsEnabled': True}
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?')
# The oldest T3 that applies a managed policy (0.1.7; `t3 pair` is older): an older one would run with none of the
# kit's locks, updates on, and quietly. Its pre-releases are older than it (semver), as some nightlies are.
OLDEST_T3 = (0, 1, 7)
# Debian packages setup installs when missing: the agent firewall's nft; libatomic1, which Node (and so most agent
# tools) needs to start, while the agent has no sudo to install it; unzip, which install steps of packages use.
PACKAGES = ('nftables', 'libatomic1', 'unzip')
HEAD = '# pc-agents kit: written by `pc-agents setup`; an edit is refused at its next run and fails the walls check\n'

# What the kit runs from a release: source path -> (path in the version folder, mode). The walls are the merged
# fleet walls of the same release, not a copy: fork-fleet-walls.py reads its folder and the launcher beside it.
CODE = {'fork-kit/pc-agents/sbin/pc-agents': ('sbin/pc-agents', 0o755),
        'fork-kit/pc-agents/lib/pc_agents.py': ('lib/pc_agents.py', 0o644),
        'fork-kit/common/machine_file.py': ('lib/machine_file.py', 0o644),
        '.github/scripts/fork-fleet-walls.py': ('walls/fork-fleet-walls.py', 0o755),
        '.github/scripts/fork-fleet-launch.sh': ('walls/fork-fleet-launch.sh', 0o755),
        '.github/scripts/fork-fleet-job-paths.txt': ('walls/fork-fleet-job-paths.txt', 0o644),
        'fork-kit/pc-agents/lib/attestation.py': ('lib/attestation.py', 0o644),
        'fork-kit/pc-agents/lib/sigstore-trust.json': ('lib/sigstore-trust.json', 0o644)}
for _w, _m in (('linux', 0o755), ('wsl2', 0o755), ('linux.fixture.json', 0o644), ('wsl2.fixture.json', 0o644)):
    CODE['.github/scripts/fork-fleet-walls/' + _w] = ('walls/fork-fleet-walls/' + _w, _m)


def is_ip_list(value, net, glob=False, most=64):
    try:
        ips = [(ipaddress.ip_network if net else ipaddress.ip_address)(v) for v in value]
    except (TypeError, ValueError) as e:
        return f'not a list of IP {"networks" if net else "addresses"} ({e})'
    if not 0 < len(ips) <= most and not (net and not ips):
        return f'1 to {most} entries'
    return 'only public addresses (a private one is blocked)' if glob and not all(i.is_global for i in ips) else None


SPEC = {
    'organization': (str, machine_file.REQUIRED,
                     lambda v: None if v.strip() and len(v) <= 80 else '1 to 80 characters, not blank'),
    't3Version': (str, machine_file.REQUIRED, lambda v: 'an exact version, no v' if not VERSION.fullmatch(v) else
                  None if new_enough(v) else '%d.%d.%d or newer, not a pre-release of it (the first T3 with the '
                  'managed policy)' % OLDEST_T3),
    't3ReleaseBaseUrl': (str, machine_file.REQUIRED,
                         lambda v: None if re.fullmatch(r'https://[A-Za-z0-9./_-]+', v) else 'an https URL'),
    'port': (int, machine_file.REQUIRED, lambda v: None if 1024 <= v <= 65535 else '1024 to 65535'),
    'agentUid': (int, 1100, lambda v: None if 1000 <= v < 60000 else '1000 to 59999'),
    'maxRuntimeMode': (str, machine_file.REQUIRED, lambda v: None if v in MODES else 'one of ' + ', '.join(MODES)),
    'lockedSettings': (dict, {}, lambda v: next(filter(None, (locked_problem(p, x) for p, x in v.items())), None)),
    'memoryMax': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'[1-9]\d*[KMGT]', v) else 'like 8G'),
    'cpuQuota': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'[1-9]\d*%', v) else 'like 400%'),
    'nameservers': (list, machine_file.REQUIRED, lambda v: is_ip_list(v, False, True, 3)),
    'allow': (list, [], lambda v: is_ip_list(v, True)),
    'peers': (list, machine_file.REQUIRED, lambda v: is_ip_list(v, True, most=16) if v else '1 to 16 entries'),
    'tailscaleTag': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'tag:[a-z0-9-]{1,50}', v) else 'tag:name'),
    'devPorts': (str, '4100-4199', lambda v: None if port_range(v) else 'LOW-HIGH, 1024 to 65535, like 4100-4199'),
    'autoUpdate': (dict, {'enabled': False, 'follow': ''}, lambda v: auto_update_problem(v)),
    'platform': (str, 'wsl2', lambda v: None if v in PLATFORMS else 'wsl2 or linux'),
    'sudoHelpers': (list, [], lambda v: helpers_problem(v)),
    'gpu': (bool, False, None),
}
REPO_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/(?!\.+$)[A-Za-z0-9._-]{1,100}')   # not `.` or `..`
AUTO_UPDATE = {'enabled': (bool, machine_file.REQUIRED, None),
               'follow': (str, '', lambda v: None if not v or REPO_NAME.fullmatch(v) else 'the fork as OWNER/NAME')}


def helpers_problem(value):
    """Why sudoHelpers is not a list of at most 8 helpers (HELPER), or None."""
    if len(value) > 8:
        return 'at most 8 helpers'
    bad = next((h for h in value if not isinstance(h, str) or not HELPER.fullmatch(h)), None)
    return None if bad is None else (f'{bad!r}: an absolute path of plain names, then plain arguments (no wildcard, '
                                     'no ALL, no list), as the sudo rule names it')


def auto_update_problem(value):
    """Why the autoUpdate object is not {"enabled": bool, "follow": "OWNER/NAME"} (follow needed when enabled)."""
    try:
        got = machine_file.fields(value, AUTO_UPDATE, 'autoUpdate')
    except machine_file.MachineFileError as e:
        return str(e).removeprefix('autoUpdate').lstrip('.: ')
    return 'needs "follow": the fork as OWNER/NAME' if got['enabled'] and not got['follow'] else None


def new_enough(version):
    """Whether VERSION version is OLDEST_T3 or newer, in semver order: a pre-release ranks below its release. The
    fork's stable build of a release (`-fork.<n>`, fork-release.yml) is that release; its nightlies are pre-releases."""
    m = VERSION.fullmatch(version)
    numbers = tuple(map(int, m.groups()[:3]))
    return numbers > OLDEST_T3 or numbers == OLDEST_T3 and (not m[4] or bool(re.fullmatch(r'-fork\.[1-9]\d*', m[4])))


def port_range(text):
    """(low, high) of a 'LOW-HIGH' port range of 1024..65535, or None."""
    m = re.fullmatch(r'([1-9]\d{3,4})-([1-9]\d{3,4})', text)
    low, high = (int(m[1]), int(m[2])) if m else (0, 0)
    return (low, high) if 1024 <= low <= high <= 65535 else None


def loopback_ports(c):
    """The TCP ports the agent may listen on and reach on localhost: T3's port and devPorts (nft and the unit's
    SocketBindAllow=). Written apart unless the range holds T3's port (nft refuses overlapping set elements)."""
    low, high = port_range(c['devPorts'])
    return ([] if low <= c['port'] <= high else [str(c['port'])]) + [f'{low}-{high}']


def locked_problem(path, value):
    """Why T3 would refuse to lock path to value, where that is cheap to tell (T3 checks the rest and does not start
    on a policy it cannot apply: managedSettings.ts), or None. An object is a complete value: a whole setting
    (`agentSlots`) or one entry of a record (`providerInstances.codex-work`), never a part of one."""
    segments = path.split('.')
    if not re.fullmatch(r'[a-z]\w*(\.[A-Za-z0-9_-]+)*', path) or segments[0] in KIT_SETTINGS:
        return f'{path}: set by the kit itself, or not a settings path'
    if segments[0] == 'providerInstances' and len(segments) > 2:
        return f'{path}: lock providerInstances.{segments[1]} as a whole (T3 refuses one field of a provider instance)'
    if isinstance(value, dict) and len(segments) > 2:
        return (f'{path}: an object only as a whole setting ({segments[0]}) or a record entry; lock one field per '
                'dotted path instead (T3 does not start on a partial object)')
    return None


def config():
    section = machine_file.read_section(KIT, ROOT + machine_file.default_path() if ROOT else None, OWNER)
    where = f'{machine_file.default_path()}: {KIT}'
    c = machine_file.fields(section, SPEC, where)
    c['autoUpdate'] = machine_file.fields(c['autoUpdate'], AUTO_UPDATE, where + '.autoUpdate')
    if c['autoUpdate']['enabled'] and c['t3ReleaseBaseUrl'].rstrip('/') != fork_downloads(c):
        raise machine_file.MachineFileError(
            f'{where}.t3ReleaseBaseUrl: is {c["t3ReleaseBaseUrl"]}, but autoUpdate follows the releases of '
            f'{c["autoUpdate"]["follow"]}: set it to {fork_downloads(c)} (an update installs from there what it finds '
            'there), or turn autoUpdate off')
    return c


def fork_downloads(c):
    """Where the followed fork's release files are (GitHub's release downloads)."""
    return f'https://github.com/{c["autoUpdate"]["follow"]}/releases/download'


def semver_key(version):
    """A sort key in semver order, as T3's updaters compare (packages/shared/src/semver.ts compareSemverVersions):
    X.Y.Z as numbers, a pre-release below its release, its parts one by one (numbers as numbers and below words,
    words as text, a shorter list first when the rest is equal). The fork's nightly X.Y.Z-nightly.D.R.fork.N sorts
    above its stable X.Y.Z-fork.N ("nightly" > "fork"), as in .github/scripts/fork-channel.sh."""
    m = VERSION.fullmatch(version)
    parts = m[4][1:].split('.') if m[4] else []
    return (tuple(map(int, m.groups()[:3])), not parts,
            tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in parts))


def release_track(version):
    """T3's update track of a version (cliReleaseChannelOf): nightly or preview for X.Y.Z-nightly|preview.D.N,
    stable for every other one, the fork's builds too."""
    m = re.fullmatch(r'[^-+]+-(nightly|preview)\.\d{8}\.\d+', version)
    return m[1] if m else 'stable'


def latest_version(latest):
    """The version of the followed fork's latest release (github.com/OWNER/NAME/releases/latest as JSON: GitHub's
    latest is published and not a pre-release; fork-release.yml marks each release it publishes latest) when T3 can
    take it: tagged vX.Y.Z[-...] and on the stable track; else None."""
    tag = latest.get('tag_name') if isinstance(latest, dict) else None
    version = tag[1:] if isinstance(tag, str) and tag.startswith('v') else ''
    return version if VERSION.fullmatch(version) and release_track(version) == 'stable' else None


def arch():
    """The release archives' name for this machine (scripts/build-cli-archive.ts), or None."""
    return {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(os.uname().machine.lower())


def runtime(c):
    """T3's version folder (root's, install_t3 unpacks it); the unit runs its `t3`."""
    return f'{T3}/runtime/versions/{c["t3Version"]}'


def masks(c):
    """Units the kit masks: WSL's binfmt handler, cron, at, ssh (WSL2 only: on linux they are the owner's) and the
    agent's systemd user manager (with it masked, no login of the agent, no linger and no user unit runs anything
    outside T3's unit)."""
    return (MASKS if c['platform'] == 'wsl2' else []) + [f'/etc/systemd/system/user@{c["agentUid"]}.service']


def dropin_dirs(unit):
    """Every drop-in folder name systemd reads for unit: its own, its type's and each dash prefix's."""
    name, kind = unit.rsplit('.', 1)
    parts = name.split('-')
    return [unit + '.d', kind + '.d'] + ['-'.join(parts[:i]) + f'-.{kind}.d' for i in range(1, len(parts))]


# What the unit hides of WSL's own paths: Windows' drives, WSL's sockets, X and its GPU driver folders (/usr/lib/wsl).
HIDDEN = 'InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix -/usr/lib/wsl -/run/tailscale'
# `gpu` on (README "GPU access"): those lines instead, in this order, and nothing else changes. /usr/lib/wsl becomes an
# empty read-only tmpfs with only WSL's GPU libraries (lib: libcuda.so.1, libdxcore.so, nvidia-smi, ...) and the
# Windows GPU driver store (drivers: NVIDIA's container toolkit needs libcuda.so.1.1, nvcubins.bin and more from it)
# bound in, read-only. /dev/dxg (WSL's GPU channel to Windows) needs two lines: PrivateDevices=yes leaves it out of
# the unit's /dev (BindPaths puts the node back) and makes the device policy closed (DeviceAllow opens this one
# device). A missing path or device is skipped ("-"), so T3 still starts on a PC whose WSL has no GPU.
GPU = ('InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix -/run/tailscale',
       'TemporaryFileSystem=/usr/lib/wsl:ro',
       'BindReadOnlyPaths=-/usr/lib/wsl/lib -/usr/lib/wsl/drivers',
       'BindPaths=-/dev/dxg',
       'DeviceAllow=/dev/dxg rw')


# linux, `gpu` on (#1027): NVIDIA's device nodes instead, and nothing else (a plain box has no /dev/dxg and no
# /usr/lib/wsl; its driver's libraries are in /usr, which the unit already reads). Same two lines per node as /dev/dxg
# (BindPaths puts the node in the unit's private /dev, DeviceAllow opens it), each skipped when missing ("-"). The list
# is fixed, not read from /dev by setup: the driver makes some nodes only on first use (nvidia-uvm, nvidia-caps), and
# a unit that changed with /dev would fail its file wall (and so stop T3) each time one appears. Cards: the first
# NVIDIA_CARDS. /dev/nvidia-caps (MIG access files, root's alone by default) is bound as a folder, so it is opened by
# its device class (the driver's "nvidia-caps" in /proc/devices), which holds just those files.
NVIDIA_CARDS = 16
NVIDIA = tuple(f'/dev/nvidia{i}' for i in range(NVIDIA_CARDS)) + \
    ('/dev/nvidiactl', '/dev/nvidia-modeset', '/dev/nvidia-uvm', '/dev/nvidia-uvm-tools')
NVIDIA_CAPS = '/dev/nvidia-caps'
LINUX_GPU = ('BindPaths=' + ' '.join('-' + p for p in NVIDIA + (NVIDIA_CAPS,)),
             *(f'DeviceAllow={p} rw' for p in NVIDIA),
             'DeviceAllow=char-nvidia-caps rw')
GPU_NODE = re.compile(r'/dev/(dxg|nvidia[^/]*|nvidia-caps/[^/]+)')   # a GPU device node, of either kind


def gpu_lines(text, platform='wsl2'):
    """Whether a unit text has GPU access: True (every GPU line of platform: wsl2's not HIDDEN), False (no GPU line;
    wsl2: HIDDEN) or None (a mix, or the other kind's lines: neither the kit's unit with the setting on nor off)."""
    lines = set(text.splitlines())
    if platform == 'wsl2':
        has = [g in lines for g in GPU]
        if any(g in lines for g in LINUX_GPU):
            return None
        return True if all(has) and HIDDEN not in lines else False if not any(has) and HIDDEN in lines else None
    has = [g in lines for g in LINUX_GPU]
    return None if any(g in lines for g in GPU[1:]) or any(has) and not all(has) else all(has)


def gpu_granted(c, path):
    """Whether T3's unit gives path (a GPU_NODE) with the machine file's `gpu` setting as it is."""
    if not c['gpu']:
        return False
    if c['platform'] == 'wsl2':
        return path == '/dev/dxg'
    return path in NVIDIA or path.startswith(NVIDIA_CAPS + '/')


def sandbox(c):
    """The unit's walls, one `Key=value` each: T3's unit runs under them, and so does `pc-agents as-agent`. On linux
    the owner's homes (and /run/user) are hidden, not only read-only, removable media too, and the unit alone reads
    the machine file's nameservers (the host's resolver is the owner's; the firewall drops the loopback stub)."""
    if c['platform'] == 'wsl2':
        home = ['ProtectHome=read-only', f'ReadWritePaths={HOME}']
        hide = GPU if c['gpu'] else (HIDDEN,)
    else:
        home = ['ProtectHome=tmpfs', f'BindPaths={HOME}', f'BindReadOnlyPaths={STATE}/resolv.conf:/etc/resolv.conf']
        hide = ('InaccessiblePaths=-/mnt -/media -/run/media -/tmp/.X11-unix -/run/tailscale -/run/docker.sock '
                '-/run/containerd', *(LINUX_GPU if c['gpu'] else ()))
    return [
        'UMask=0077',
        f'MemoryMax={c["memoryMax"]}',
        f'CPUQuota={c["cpuQuota"]}',
        'NoNewPrivileges=yes',
        'ProtectSystem=strict',
        *home,
        'PrivateTmp=yes',
        'PrivateDevices=yes',
        'PrivateIPC=yes',
        'ProtectProc=invisible',
        *hide,
        f'IPAddressDeny={" ".join(BLOCKED4 + BLOCKED6)}',
        f'IPAddressAllow={" ".join(c["peers"] + c["allow"])}',
        'SocketBindDeny=any',
        *('SocketBindAllow=tcp:' + p for p in loopback_ports(c)),
        'RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK',
        'SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS',
        'SystemCallArchitectures=native',
        'RestrictNamespaces=yes',
        'RestrictSUIDSGID=yes',
        'RestrictRealtime=yes',
        'LockPersonality=yes',
        'ProtectKernelTunables=yes',
        'ProtectKernelModules=yes',
        'ProtectKernelLogs=yes',
        'ProtectControlGroups=yes',
        'ProtectClock=yes',
        'ProtectHostname=yes',
    ]


def render(c):
    """{path: (text, mode)} of every file the kit owns, from the config alone (the same every run)."""
    settings = {**KIT_SETTINGS, **c['lockedSettings']}
    policy = {'schemaVersion': 1, 'organization': c['organization'],
              'settings': {k: {'value': v, 'locked': True} for k, v in settings.items()},
              'limits': {'maxRuntimeMode': c['maxRuntimeMode'], 'connect': False, 'webhookTasks': False,
                         'browserCookieImport': False, 'updates': {'mode': 'off'}}}
    allow4 = [a for a in c['allow'] if ':' not in a]
    allow6 = [a for a in c['allow'] if ':' in a]
    nft = HEAD + 'table inet pc_agents\ndelete table inet pc_agents\ntable inet pc_agents {\n\tchain agent_out {\n' + \
        '\t\ttype filter hook output priority filter; policy accept;\n' + \
        f'\t\tmeta skuid != {c["agentUid"]} accept\n\t\tct state established,related accept\n' + \
        f'\t\toifname "lo" tcp dport {{ {", ".join(loopback_ports(c))} }} accept\n\t\toifname "lo" drop\n' + \
        (f'\t\tip daddr {{ {", ".join(allow4)} }} accept\n' if allow4 else '') + \
        (f'\t\tip6 daddr {{ {", ".join(allow6)} }} accept\n' if allow6 else '') + \
        f'\t\tip daddr {{ {", ".join(BLOCKED4)} }} drop\n\t\tip6 daddr {{ {", ".join(BLOCKED6)} }} drop\n\t}}\n}}\n'
    walls = '\n'.join(sandbox(c))
    unit = HEAD + f'''[Unit]
Description=T3 Code for agents, walled (pc-agents kit)
Wants=network-online.target
After=network-online.target
# any failure but a policy T3 cannot apply (77, below): give up after 5 starts in 10 minutes
StartLimitIntervalSec=600
StartLimitBurst=5

[Service]
User={USER}
Group={USER}
Environment=HOME={HOME} T3CODE_HOME={HOME}/.t3 T3CODE_PORT={c["port"]} T3CODE_LAN=true
Environment=PATH={HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin SHELL=/bin/bash
ExecStartPre=+{CHECK} load-firewall
ExecStartPre=+{CHECK} check
ExecStart={runtime(c)}/t3 serve
Restart=on-failure
RestartSec=30
# 77: T3 cannot apply the managed policy; a restart cannot fix that
RestartPreventExitStatus=77
{walls}

[Install]
WantedBy=multi-user.target
'''
    wsl = HEAD + ('[boot]\nsystemd=true\n[automount]\nenabled=false\nmountFsTab=false\n[interop]\nenabled=false\n'
                  'appendWindowsPath=false\n[network]\ngenerateResolvConf=false\n[user]\ndefault=root\n')
    sd = '/etc/systemd/system/'
    resolv = HEAD + ''.join(f'nameserver {n}\n' for n in c['nameservers'])
    host = {'/etc/wsl.conf': (wsl, 0o644), '/etc/resolv.conf': (resolv, 0o644)} if c['platform'] == 'wsl2' else \
        {STATE + '/resolv.conf': (resolv, 0o644), SSHD_CONF: (HEAD + f'DenyUsers {USER}\n', 0o644)}
    return {
        **host,
        '/etc/t3code/policy.json': (json.dumps(policy, indent=2, sort_keys=True) + '\n', 0o644),
        STATE + '/firewall.nft': (nft, 0o644),
        sd + UNIT: (unit, 0o644),
        # A check that does not succeed, even one that cannot start or crashes before it can stop T3, stops T3.
        # After T3's unit: a check started while T3 is still starting (slow network at boot) waits for that start,
        # instead of failing on a firewall not loaded yet and stopping T3 before it ran.
        sd + WALLS + '.service': (HEAD + f'[Unit]\nDescription=pc-agents walls check\nAfter={UNIT}\n\n[Service]\n'
                                  f'Type=oneshot\nTimeoutStartSec=5min\nExecStart={CHECK} check --enforce\n'
                                  'ExecStopPost=/bin/sh -c \'[ "$$SERVICE_RESULT" = success ] || '
                                  f'exec /usr/bin/systemctl stop {UNIT} "{AS_AGENT}-*.service"\'\n', 0o644),
        sd + WALLS + '.timer': (HEAD + '[Unit]\nDescription=pc-agents walls check every 2 minutes\n\n[Timer]\n'
                                'OnActiveSec=1min\nOnUnitActiveSec=2min\nAccuracySec=10s\n\n[Install]\n'
                                'WantedBy=timers.target\n', 0o644),
        # The automatic update (autoUpdate in the machine file; off: it says so and does nothing). Root's, from the
        # installed kit: it reads only root's files, the release list and T3's own answer. Not After= T3's unit: it
        # restarts T3 itself (through setup). Its time limit covers the download's 30 minutes, setup and the check.
        sd + UPDATE + '.service': (HEAD + '[Unit]\nDescription=pc-agents automatic T3 update (autoUpdate)\n'
                                   'Wants=network-online.target\nAfter=network-online.target\n\n[Service]\n'
                                   f'Type=oneshot\nTimeoutStartSec=2h\nExecStart={CHECK} auto-update\n', 0o644),
        # Hourly, spread over 20 minutes; 10 minutes after a start of the distro (finishes an update a stop cut).
        sd + UPDATE + '.timer': (HEAD + '[Unit]\nDescription=pc-agents automatic T3 update, hourly\n\n[Timer]\n'
                                 'OnActiveSec=10min\nOnUnitActiveSec=1h\nRandomizedDelaySec=20min\n\n[Install]\n'
                                 'WantedBy=timers.target\n', 0o644),
    }


def sha(text):
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with open(ROOT + path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 20), b''):
            h.update(block)
    return h.hexdigest()


def record():
    """{path: {sha256, ...}} of the record: one hash per path, two while setup replaces a file (old and new)."""
    out = {}
    try:
        with open(ROOT + RECORD, encoding='utf-8') as f:
            for line in f:
                if '\t' in line:
                    h, path = line.rstrip('\n').split('\t', 1)
                    out.setdefault(path, set()).add(h)
    except FileNotFoundError:
        pass
    return out


def save(path, text):
    """Write a small state file whole (new file, one rename) unless it already says text."""
    real = ROOT + path
    try:
        with open(real, encoding='utf-8') as f:
            if f.read() == text:
                return
    except (FileNotFoundError, UnicodeDecodeError):
        pass
    os.makedirs(os.path.dirname(real), mode=0o755, exist_ok=True)
    with open(real + '.new', 'w', encoding='utf-8') as f:
        f.write(text)
    os.replace(real + '.new', real)


def nft_listing():
    r = run(['/usr/sbin/nft', 'list', 'table', 'inet', 'pc_agents'])
    return r.stdout if r.returncode == 0 else None


def load_firewall():
    """Load firewall.nft and save its hash with what the kernel then lists, so the check can tell that the loaded
    table is this file's (any change by anyone, or an older file left loaded, differs). Returns why not, or None."""
    body = file_sha(STATE + '/firewall.nft')
    r = run(['/usr/sbin/nft', '-f', STATE + '/firewall.nft'])
    listing = nft_listing() if r.returncode == 0 else None
    if listing is None:
        return f'cannot load {STATE}/firewall.nft: {(r.stderr or r.stdout).strip()[-300:]}'
    save(LOADED, f'{body}\n{listing}')
    return None


def lstat(path):
    try:
        return os.lstat(ROOT + path)
    except FileNotFoundError:
        return None


def safe(path, st, kind):
    """Why path (stat st) is not what kind says (S_ISREG etc.), owned by OWNER, writable by it alone; or None."""
    if st is None:
        return f'{path} is missing'
    if not kind(st.st_mode) or st.st_uid != OWNER or st.st_mode & 0o022:
        return f'{path} is not owned by uid {OWNER} and writable by it alone (or is the wrong kind of file)'
    return None


def users():
    """{name: (uid, gid)} of the passwd file, and {group: [members]}."""
    def rows(path):
        try:
            with open(ROOT + path, encoding='utf-8') as f:
                return [line.rstrip('\n').split(':') for line in f if line.count(':') >= 3]
        except FileNotFoundError:
            return []
    return ({r[0]: (int(r[2]), int(r[3])) for r in rows('/etc/passwd') if r[2].isdigit() and r[3].isdigit()},
            {r[0]: [m for m in r[3].split(',') if m] for r in rows('/etc/group')})


def passwd_field(name, index):
    """Field index of name's line in the passwd file (5: home, 6: login shell), or None."""
    try:
        with open(ROOT + '/etc/passwd', encoding='utf-8') as f:
            return next((r[index] for r in (line.rstrip('\n').split(':') for line in f)
                         if r[0] == name and len(r) > index), None)
    except FileNotFoundError:
        return None


def login_shell(name):
    """name's login shell in the passwd file, or None."""
    return passwd_field(name, 6)


def clock():
    """(this boot's id, CLOCK_MONOTONIC in microseconds): systemd's *TimestampMonotonic clock, which no clock change
    or WSL time sync moves."""
    with open(ROOT + BOOT_ID, encoding='ascii') as f:
        return f.read().strip(), time.monotonic_ns() // 1000


def marker():
    """The restart marker's text: when setup last changed something T3 runs with (boot id, monotonic microseconds)."""
    boot, now = clock()
    return f'boot_id={boot} monotonic_us={now}\nsetup changed what T3 runs with; the next good run restarts T3 and ' \
        'removes this. Until then `pc-agents check` fails while a T3 started before this time runs.\n'


def props(text):
    """`systemctl show` output as a dict."""
    return dict(line.split('=', 1) for line in text.splitlines() if '=' in line)


def restart_problem():
    """Why a T3 runs that started before setup's last change (a restart is pending), or None. No marker, or no T3
    process (stopped, or still in its ExecStartPre check): nothing stale runs. Anything unreadable fails."""
    if lstat(PENDING) is None:
        return None
    r = run(['/usr/bin/systemctl', 'show', '-p', 'MainPID,ExecMainStartTimestampMonotonic', UNIT])
    got = props(r.stdout)
    if r.returncode or not re.fullmatch(r'\d+', got.get('MainPID', '')):
        return f'cannot tell whether T3 runs ({(r.stderr or r.stdout).strip()[-200:]})'
    if got['MainPID'] == '0':
        return None
    try:
        with open(ROOT + PENDING, encoding='utf-8', errors='replace') as f:
            m = re.match(r'boot_id=(\S+) monotonic_us=(\d+)\n', f.read())
        boot = clock()[0]
    except OSError as e:
        return f'cannot read {PENDING} or the boot id ({e})'
    started = got.get('ExecMainStartTimestampMonotonic', '')
    if not m or not re.fullmatch(r'[1-9]\d*', started):
        return f'a restart is pending ({PENDING}) and T3\'s start time or the marker\'s cannot be read'
    if m[1] == boot and int(started) <= int(m[2]):   # a marker of an earlier boot is older than any T3 now
        return f'T3 runs as started before setup\'s last change ({PENDING}): run pc-agents setup again'
    return None


def stop_on_signals():
    """SIGTERM and SIGHUP end this process like Ctrl-C does, through run()'s clean-up (its child's group is killed)."""
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    for s in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(s, stop)


def helper_rule(c, line):
    """Whether a sudoers line is exactly `agent ALL=(root) NOPASSWD: H[, H]...`, each H an allow-listed helper
    (sudoHelpers, word for word: sudo runs a command named without arguments with any) that is root's alone. Nothing
    wider: no other user, host, run-as user or tag, no wildcard, no alias."""
    m = re.fullmatch(rf'{USER}\s+ALL\s*=\s*\(root\)\s*NOPASSWD:\s*(.+)', line)
    if not m:
        return False
    helpers = [' '.join(h.split()) for h in m[1].split(',')]
    return all(h in c['sudoHelpers'] and owned_alone(h.split()[0]) for h in helpers)


def owned_alone(path):
    """Whether path, a plain file, and every folder above it are the kit's owner's, writable by it alone."""
    parts = path.split('/')
    folders = ['/'.join(parts[:i]) or '/' for i in range(1, len(parts))]
    return safe(path, lstat(path), stat.S_ISREG) is None and \
        all(safe(d, lstat(d), stat.S_ISDIR) is None for d in folders)


def sudo_problem(c):
    """Why sudo could give the agent more (a sudoers line naming it, its group or uid, or every user; Defaults for it
    or for a helper of it), or None. Harmless inside the unit (NoNewPrivileges), but a way out from anywhere else.
    Only an allow-listed helper's rule passes (helper_rule): the owner's, kept as it is, never widened."""
    who = re.compile(rf'(^|[\s,=:!(])(%?{USER}|%?#{c["agentUid"]}|ALL)($|[\s,=:)])')
    helpers = {h.split()[0] for h in c['sudoHelpers']}
    found = []
    names = ['/etc/sudoers'] + (['/etc/sudoers.d/' + n for n in sorted(os.listdir(ROOT + '/etc/sudoers.d'))]
                                if os.path.isdir(ROOT + '/etc/sudoers.d') else [])
    for path in names:
        try:
            with open(ROOT + path, encoding='utf-8', errors='replace') as f:
                lines = f.read().replace('\\\n', ' ').splitlines()
        except (FileNotFoundError, IsADirectoryError):
            continue
        for line in lines:
            line = re.sub(r'#(?!include)(?!\d).*', '', line).strip()
            if line.startswith(('@include', '#include')):
                continue
            if line.startswith('Defaults'):   # Defaults:agent (a user list), Defaults!helper (a command list)
                bound = re.match(r'Defaults([:!])\s*(\S+)', line)
                if bound and (who.search(bound[2]) if bound[1] == ':' else set(bound[2].split(',')) & helpers):
                    found.append(f'{path}: {line[:80]}')
                continue
            first = line.split(None, 1)[0] if line else ''
            # a user list (first field) or an alias naming the agent; `root ALL=(ALL) ALL` names root, not the agent
            if line and (who.search(first) and not helper_rule(c, line) or
                         re.match(r'User_Alias\b', line) and who.search(line.split('=', 1)[-1])):
                found.append(f'{path}: {line[:80]}')
    return found and 'sudo rules that cover the agent: ' + '; '.join(found[:3])


def user_problem(c, need_agent):
    """Why this distro is someone else's (another login user: WSL2 only; on a plain Linux box the owner's users are
    expected), or the agent is not the kit's (its uid, home, primary group, any other group), or None. An agent made
    by hand is kept when it is the kit's, never changed to be (setup only takes its login shell away)."""
    people, groups = users()
    others = sorted(n for n, (uid, _) in people.items() if 1000 <= uid < 60000 and n != USER)
    if others and c['platform'] == 'wsl2':
        return f'this distro already has login users ({", ".join(others)}): use a fresh distro for pc-agents'
    if USER not in people:
        return f'no user {USER}' if need_agent else None
    if people[USER][0] != c['agentUid']:
        return f'user {USER} has uid {people[USER][0]}, not {c["agentUid"]}: not the kit\'s'
    if (home := passwd_field(USER, 5)) != HOME:
        return f'user {USER}\'s home is {home}, not {HOME}: not the kit\'s'
    if people[USER][1] != gid(USER):
        return f'user {USER}\'s primary group is gid {people[USER][1]}, not its own group {USER}: not the kit\'s'
    extra = sorted(g for g, members in groups.items() if USER in members)
    return f'user {USER} is in other groups ({", ".join(extra)}): it must have none' if extra else None


def gid(group):
    """group's gid in the group file, or None."""
    try:
        with open(ROOT + '/etc/group', encoding='utf-8') as f:
            return next((int(r[2]) for r in (line.rstrip('\n').split(':') for line in f)
                         if r[0] == group and len(r) > 2 and r[2].isdigit()), None)
    except FileNotFoundError:
        return None


def run(argv, timeout=120, env=None, inherit=True):
    """Run argv (argv[0] under ROOT) in a process group of its own. Past timeout, or when this process stops (Ctrl-C,
    or SIGTERM and SIGHUP once stop_on_signals() ran), the whole group is killed (a download's curl too); a timeout
    returns exit 124 and says so in stderr. env: variables added to this process's own (inherit), or its whole
    environment (not inherit)."""
    p = subprocess.Popen([ROOT + argv[0]] + argv[1:], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True, start_new_session=True,
                         env=({**os.environ, **env} if inherit else dict(env)) if env or not inherit else None)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException as e:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        try:
            out, err = p.communicate(timeout=10)
        except subprocess.TimeoutExpired:   # something left the group but holds the pipes: stop reading
            out, err = '', ''
        if not isinstance(e, subprocess.TimeoutExpired):
            raise
        return subprocess.CompletedProcess(argv, 124, out, f'{err}\n{argv[0]} took more than {timeout} s; stopped it')
    return subprocess.CompletedProcess(argv, p.returncode, out, err)
