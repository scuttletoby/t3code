"""pc-agents: the facts, the files this kit owns and the checks, shared by `pc-agents setup` and `pc-agents check`.

Every path is read and written under ROOT, which is '' (the real machine) except in the tests, where
PC_AGENTS_ROOT names a fake one; root never gets a fake one. Standard library only."""
import hashlib, ipaddress, json, os, pwd, re, subprocess

import machine_file

KIT = 'pc-agents'
ROOT = os.environ.get('PC_AGENTS_ROOT', '')
if ROOT and os.geteuid() == 0:
    raise SystemExit('pc-agents: PC_AGENTS_ROOT is for the tests only, never for root; unset it')
OWNER = os.geteuid() if ROOT else 0   # who must own every file the kit writes
HOME, USER = '/home/agent', 'agent'
OPT, STATE = '/opt/pc-agents', '/etc/pc-agents'
RECORD = STATE + '/installed.tsv'   # sha256 <TAB> path of every file this kit wrote
UNIT, WALLS = 't3code-agents.service', 'pc-agents-walls'
CHECK = OPT + '/current/sbin/pc-agents'
WALLS_PY = OPT + '/current/walls/fork-fleet-walls.py'
# Walls of fork-fleet-walls/wsl2 that are about the fleet launcher's job units. Here the job unit is T3's own unit,
# whose sandbox this kit writes and checks itself (the unit file must be exactly the kit's, with no drop-in).
UNIT_WALLS = {'mounts', 'sockets', 'userns'}
MASKS = ['/etc/binfmt.d/WSLInterop.conf'] + ['/etc/systemd/system/' + u for u in
                                            ('cron.service', 'atd.service', 'ssh.service', 'ssh.socket')]
UNIT_DIRS = ('/etc/systemd/system', '/run/systemd/system', '/usr/local/lib/systemd/system', '/usr/lib/systemd/system',
             '/lib/systemd/system', '/run/systemd/transient', '/run/systemd/generator.early')
BLOCKED4 = ('0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
            '224.0.0.0/4', '240.0.0.0/4')
BLOCKED6 = ('fc00::/7', 'fe80::/10', 'ff00::/8')
MODES = ('approval-required', 'auto-accept-edits', 'auto', 'full-access')
KIT_SETTINGS = {'linkedEnvironmentsEnabled': True, 'automaticUpdates': False}
VERSION = re.compile(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(-[0-9A-Za-z.-]+)?')
HEAD = '# pc-agents kit: written by `pc-agents setup`; an edit is refused at its next run and fails the walls check\n'

# What the kit runs from a release: source path -> (path in the version folder, mode). The walls are the merged
# fleet walls of the same release, not a copy: fork-fleet-walls.py reads its folder and the launcher beside it.
CODE = {'fork-kit/pc-agents/sbin/pc-agents': ('sbin/pc-agents', 0o755),
        'fork-kit/pc-agents/lib/pc_agents.py': ('lib/pc_agents.py', 0o644),
        'fork-kit/common/machine_file.py': ('lib/machine_file.py', 0o644),
        '.github/scripts/fork-fleet-walls.py': ('walls/fork-fleet-walls.py', 0o755),
        '.github/scripts/fork-fleet-launch.sh': ('walls/fork-fleet-launch.sh', 0o755),
        '.github/scripts/fork-fleet-job-paths.txt': ('walls/fork-fleet-job-paths.txt', 0o644),
        'scripts/install.sh': ('share/install.sh', 0o644)}
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
    'organization': (str, machine_file.REQUIRED, lambda v: None if 0 < len(v) <= 80 else '1 to 80 characters'),
    't3Version': (str, machine_file.REQUIRED, lambda v: None if VERSION.fullmatch(v) else 'an exact version, no v'),
    't3ReleaseBaseUrl': (str, machine_file.REQUIRED,
                         lambda v: None if re.fullmatch(r'https://[A-Za-z0-9./_-]+', v) else 'an https URL'),
    'port': (int, machine_file.REQUIRED, lambda v: None if 1024 <= v <= 65535 else '1024 to 65535'),
    'agentUid': (int, 1100, lambda v: None if 1000 <= v < 60000 else '1000 to 59999'),
    'maxRuntimeMode': (str, machine_file.REQUIRED, lambda v: None if v in MODES else 'one of ' + ', '.join(MODES)),
    'lockedSettings': (dict, {}, lambda v: next((f'{k}: set by the kit itself, or not a settings path' for k in v
                                                 if k in KIT_SETTINGS or not re.fullmatch(r'[a-z]\w*(\.\w+)*', k)), None)
                       or next((f'{k}: lock one field per dotted path ({k}.field), not an object (T3 takes an object '
                                'only as the whole setting and does not start on a policy it cannot apply)'
                                for k, x in v.items() if isinstance(x, dict)), None)),
    'memoryMax': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'[1-9]\d*[KMGT]', v) else 'like 8G'),
    'cpuQuota': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'[1-9]\d*%', v) else 'like 400%'),
    'nameservers': (list, machine_file.REQUIRED, lambda v: is_ip_list(v, False, True, 3)),
    'allow': (list, [], lambda v: is_ip_list(v, True)),
    'tailscaleTag': (str, machine_file.REQUIRED, lambda v: None if re.fullmatch(r'tag:[a-z0-9-]{1,50}', v) else 'tag:name'),
}


def config():
    section = machine_file.read_section(KIT, ROOT + machine_file.default_path() if ROOT else None, OWNER)
    return machine_file.fields(section, SPEC, f'{machine_file.default_path()}: {KIT}')


def runtime(c):
    return f'{HOME}/.t3/runtime/versions/{c["t3Version"]}/t3'


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
        f'\t\tmeta skuid != {c["agentUid"]} accept\n\t\tct state established,related accept\n\t\toifname "lo" accept\n' + \
        (f'\t\tip daddr {{ {", ".join(allow4)} }} accept\n' if allow4 else '') + \
        (f'\t\tip6 daddr {{ {", ".join(allow6)} }} accept\n' if allow6 else '') + \
        f'\t\tip daddr {{ {", ".join(BLOCKED4)} }} drop\n\t\tip6 daddr {{ {", ".join(BLOCKED6)} }} drop\n\t}}\n}}\n'
    unit = HEAD + f'''[Unit]
Description=T3 Code for agents, walled (pc-agents kit)
Wants=network-online.target
After=network-online.target

[Service]
User={USER}
Group={USER}
Environment=HOME={HOME} T3CODE_HOME={HOME}/.t3 T3CODE_PORT={c["port"]} T3CODE_LAN=true
Environment=PATH={HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStartPre=+/usr/sbin/nft -f {STATE}/firewall.nft
ExecStartPre=+{CHECK} check
ExecStart={runtime(c)} serve
Restart=on-failure
RestartSec=30
UMask=0077
MemoryMax={c["memoryMax"]}
CPUQuota={c["cpuQuota"]}
NoNewPrivileges=yes
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={HOME}
PrivateTmp=yes
PrivateDevices=yes
PrivateIPC=yes
ProtectProc=invisible
InaccessiblePaths=-/mnt -/run/WSL -/tmp/.X11-unix -/usr/lib/wsl
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK
SystemCallFilter=~io_uring_setup:ENOSYS io_uring_enter:ENOSYS io_uring_register:ENOSYS
SystemCallArchitectures=native
RestrictNamespaces=yes
RestrictSUIDSGID=yes
RestrictRealtime=yes
LockPersonality=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes

[Install]
WantedBy=multi-user.target
'''
    wsl = HEAD + ('[boot]\nsystemd=true\n[automount]\nenabled=false\nmountFsTab=false\n[interop]\nenabled=false\n'
                  f'appendWindowsPath=false\n[network]\ngenerateResolvConf=false\n[user]\ndefault={USER}\n')
    sd = '/etc/systemd/system/'
    return {
        '/etc/wsl.conf': (wsl, 0o644),
        '/etc/resolv.conf': (HEAD + ''.join(f'nameserver {n}\n' for n in c['nameservers']), 0o644),
        '/etc/t3code/policy.json': (json.dumps(policy, indent=2, sort_keys=True) + '\n', 0o644),
        STATE + '/firewall.nft': (nft, 0o644),
        sd + UNIT: (unit, 0o644),
        sd + WALLS + '.service': (HEAD + f'[Unit]\nDescription=pc-agents daily walls check\n\n[Service]\nType=oneshot\n'
                                  f'ExecStart={CHECK} check --enforce\n', 0o644),
        sd + WALLS + '.timer': (HEAD + '[Unit]\nDescription=pc-agents daily walls check\n\n[Timer]\nOnCalendar=daily\n'
                                'Persistent=true\n\n[Install]\nWantedBy=timers.target\n', 0o644),
    }


def sha(text):
    return hashlib.sha256(text.encode() if isinstance(text, str) else text).hexdigest()


def record():
    try:
        with open(ROOT + RECORD, encoding='utf-8') as f:
            return dict(reversed(line.rstrip('\n').split('\t', 1)) for line in f if '\t' in line)
    except FileNotFoundError:
        return {}


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


def user_problem(c, need_agent):
    """Why this distro is someone else's (another login user, an agent that is not the kit's), or None."""
    people, groups = users()
    others = sorted(n for n, (uid, _) in people.items() if 1000 <= uid < 60000 and n != USER)
    if others:
        return f'this distro already has login users ({", ".join(others)}): use a fresh distro for pc-agents'
    if USER not in people:
        return f'no user {USER}' if need_agent else None
    if people[USER][0] != c['agentUid']:
        return f'user {USER} has uid {people[USER][0]}, not {c["agentUid"]}: not the kit\'s'
    extra = sorted(g for g, members in groups.items() if USER in members)
    return f'user {USER} is in other groups ({", ".join(extra)}): it must have none' if extra else None


def run(argv, **kw):
    return subprocess.run([ROOT + argv[0]] + argv[1:], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                          timeout=120, **kw)
