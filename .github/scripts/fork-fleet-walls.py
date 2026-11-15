#!/usr/bin/env python3
"""Fork only: the per-OS walls of a fleet host, as root, before a desktop pool starts a runner (.github/fork-fleet.md).
The launcher's sandbox (fork-fleet-launch.sh) is the same on every OS; what must also hold for it to mean anything
differs per OS (on WSL2: no Windows drive, no Windows programs; on plain Linux: a kernel and a systemd that do not
silently drop a wall). Each OS has one walls file, fork-fleet-walls/<os>, next to this file. An OS without one is
refused: not supported yet.

  fork-fleet-walls.py os         prints the OS name; exit 0 when it has a walls file, else 3
  fork-fleet-walls.py check      before every `start`: exit 0 only when every wall holds; prints the walls that do not
  fork-fleet-walls.py describe   for setup: every wall, PASS or FAIL

Exit: 0 every wall holds, 1 a wall does not hold, 2 a broken or unsafe walls file (or usage), 3 this OS is not
supported yet. Anything but 0 from `check`: no runner starts.

The walls-file contract (fork-fleet-walls.test.py checks it for every file in the folder):
- an executable file named for its OS (OS_NAME), owned by the user that runs this and writable by it alone (as the
  folder), with a fixture <os>.fixture.json next to it (a passing fake root, and one failing change per wall);
- argv is exactly one verb, `check` or `describe`; anything else: exit 2 and nothing on stdout;
- stdout lines are `PASS  <wall>  <what>` or `FAIL  <wall>  <what>: <why>` (LINE: two spaces between fields);
  `describe` prints every wall once, `check` only the walls that do not hold; exit 0 when none fails, 1 when one
  does, and nothing else (an uncaught error is not a pass);
- one wall is `os`: proof that this machine is that OS, so a file run on the wrong OS fails;
- it reads every path, and runs every program, under $FORK_FLEET_WALLS_ROOT (default: empty, the real /), so the
  tests can hand it a fake machine; an empty root fails closed (exit 1). It changes nothing and uses no network;
- it gets only PATH, LC_ALL and FORK_FLEET_WALLS_ROOT, no stdin, and TIMEOUT seconds.
Standard library only."""
import glob, os, platform, re, stat, subprocess, sys

HERE = os.path.dirname(os.path.realpath(__file__))
WALLS = os.path.join(HERE, 'fork-fleet-walls')
OS_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')
LINE = re.compile(r'(PASS|FAIL)  ([a-z0-9][a-z0-9-]{0,47})  (\S[^\n]{0,400})')
TIMEOUT = 60
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
# What WSL leaves in every distro, whatever its kernel is called (a kernel of one's own, interop off).
WSL_MARKS = ('/run/WSL', '/proc/sys/fs/binfmt_misc/WSLInterop*', '/usr/lib/wsl', '/mnt/wsl')
USAGE = 'usage: fork-fleet-walls.py os | check | describe'


class Broken(Exception):
    """The walls file is unsafe or broke its contract: exit 2, no runner."""


def read(root, path):
    try:
        with open(root + path, encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return None


def detect(root=None):
    """The OS this machine is, as a walls file's name. root: $FORK_FLEET_WALLS_ROOT (a fake machine) or the real one.
    WSL is Linux to its kernel: told apart by its kernel name or WSL's own marks; WSL1's kernel is `...-Microsoft`."""
    root = os.environ.get('FORK_FLEET_WALLS_ROOT', '') if root is None else root
    ostype = read(root, '/proc/sys/kernel/ostype')
    if ostype is None:   # no Linux /proc: name the system (a fake root that has none is unknown)
        name = 'unknown' if root else {'darwin': 'macos'}.get(platform.system().lower(), platform.system().lower())
    elif ostype != 'Linux':
        name = ostype.lower()
    else:
        release = read(root, '/proc/sys/kernel/osrelease') or ''
        if re.search(r'-Microsoft$', release):
            name = 'wsl1'
        elif 'microsoft' in release.lower() or any(glob.glob(glob.escape(root) + m) for m in WSL_MARKS):
            name = 'wsl2'
        else:
            name = 'linux'
    name = re.sub(r'[^a-z0-9-]+', '-', name).strip('-')[:32]
    return name if OS_NAME.fullmatch(name) else 'unknown'


def safe(path, kind):
    """A path that is what kind says, not a link, owned by this user and writable by it alone (as the launcher's)."""
    try:
        st = os.lstat(path)
    except OSError:
        raise Broken(f'{path} is missing')
    if not (stat.S_ISDIR if kind == 'dir' else stat.S_ISREG)(st.st_mode):
        raise Broken(f'{path} is not a {kind} (or is a link)')
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise Broken(f'{path} must belong to uid {os.geteuid()} and be writable by it alone')
    if kind == 'file' and not st.st_mode & 0o100:
        raise Broken(f'{path} is not executable')


def walls_file(name):
    """The walls file of OS name, or None when that OS has none (not supported yet)."""
    safe(WALLS, 'dir')
    path = os.path.join(WALLS, name)
    if not OS_NAME.fullmatch(name) or not os.path.lexists(path):
        return None
    safe(path, 'file')
    return path


def run_walls(path, verb):
    """Runs the walls file and holds it to the contract: the lines it printed, and whether every wall held."""
    env = {'PATH': PATH, 'LC_ALL': 'C'}
    if os.environ.get('FORK_FLEET_WALLS_ROOT'):
        env['FORK_FLEET_WALLS_ROOT'] = os.environ['FORK_FLEET_WALLS_ROOT']
    name = os.path.basename(path)
    try:
        p = subprocess.run([path, verb], env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=TIMEOUT,
                           cwd='/')
    except subprocess.TimeoutExpired:
        raise Broken(f'{name} {verb} took more than {TIMEOUT} s')
    except OSError as e:
        raise Broken(f'{name} {verb} did not run: {e}')
    err = p.stderr.decode('utf-8', 'replace').strip()
    if err:
        print(err[-2000:], file=sys.stderr)
    lines = p.stdout.decode('utf-8', 'replace').splitlines()
    bad = next((x for x in lines if not LINE.fullmatch(x)), None)
    if bad is not None:
        raise Broken(f'{name} {verb} printed a line that is not `PASS|FAIL  <wall>  <what>`: {bad[:200]!r}')
    marks = [LINE.fullmatch(x).group(1, 2) for x in lines]
    walls = [w for _, w in marks]
    fails = [w for mark, w in marks if mark == 'FAIL']
    if len(set(walls)) != len(walls):
        raise Broken(f'{name} {verb} named a wall twice')
    if verb == 'check' and len(fails) != len(marks):
        raise Broken(f'{name} check printed a PASS line (it prints only the walls that do not hold)')
    if verb == 'describe' and 'os' not in walls:
        raise Broken(f'{name} describe has no wall `os` (proof that this machine is {name})')
    if p.returncode != (1 if fails else 0):
        raise Broken(f'{name} {verb} exited {p.returncode} with {len(fails)} walls failing (0 when none fails, '
                     '1 when one does)')
    return lines, not fails


def main(argv):
    if len(argv) != 1 or argv[0] not in ('os', 'check', 'describe'):
        print(USAGE, file=sys.stderr)
        return 2
    name = detect()
    try:
        path = walls_file(name)
        if path is None:
            print(f'fork-fleet-walls: this OS ({name}) is not supported yet: there are no walls for it '
                  f'(fork-fleet-walls/{name}), so no runner starts here', file=sys.stderr)
            return 3
        if argv[0] == 'os':
            print(name)
            return 0
        lines, held = run_walls(path, argv[0])
    except Broken as e:
        print(f'fork-fleet-walls: {e}: no runner starts here', file=sys.stderr)
        return 2
    for line in lines:
        print(line)
    if not held:
        print(f'fork-fleet-walls: {name}: a wall does not hold (above): no runner starts here', file=sys.stderr)
    return 0 if held else 1


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
