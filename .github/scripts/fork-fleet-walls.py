#!/usr/bin/python3 -I
"""Fork only: the per-OS walls of a fleet host, as root, before a desktop pool starts a runner (.github/fork-fleet.md).
The launcher's sandbox (fork-fleet-launch.sh) is the same on every OS; what must also hold for it to mean anything
differs per OS (on WSL2: no Windows drive, no Windows programs; on plain Linux: a kernel and a systemd that do not
silently drop a wall). Each OS has one walls file, fork-fleet-walls/<os>, next to this file. An OS without one is
refused: not supported yet.

  fork-fleet-walls.py os         prints the OS name; exit 0 when it has a walls file, else 3
  fork-fleet-walls.py check      before every `start`: exit 0 only when every wall holds; prints the walls that do not
  fork-fleet-walls.py describe   for setup: every wall, PASS or FAIL

Exit: 0 every wall holds, 1 a wall does not hold, 2 a broken or unsafe walls file (or usage), 3 this OS is not
supported yet (native Windows too). Anything but 0 from `check`: no runner starts.

Both verbs run the walls file's `describe` and hold it to the contract; `check` then prints only the FAIL lines. So
`check` passes only on proof that every wall ran: each wall that <os>.fixture.json names (its `fail` keys, the list
of that OS's walls) reported once, `os` among them. A walls file that printed nothing, was cut off, stopped early or
is older than its list: exit 2.

The walls-file contract (fork-fleet-walls-contract.test.py checks it for every file in the folder):
- an executable file named for its OS (OS_NAME), owned by the user that runs this and writable by it alone (as the
  folder and the fixture), with a fixture <os>.fixture.json next to it (a passing fake root, and at least one failing
  change per wall; its `fail` keys are the walls). One file: the folder holds walls files and fixtures only, and a
  Python walls file runs as `#!/usr/bin/python3 -I`, so it imports nothing from beside it;
- argv is exactly one verb, `check` or `describe`; anything else: exit 2 and nothing on stdout;
- stdout lines are `PASS  <wall>  <what>` or `FAIL  <wall>  <what>: <why>` (LINE: two spaces between fields, no
  control characters: a mount's name is escaped, never printed raw); `describe` prints every wall once, `check` only
  the walls that do not hold; exit 0 when none fails, 1 when one does, and nothing else (an uncaught error is not a
  pass);
- one wall is `os`: proof that this machine is that OS, so a file run on the wrong OS fails;
- it reads every path, and runs every program, under $FORK_FLEET_WALLS_ROOT (default: empty, the real /), so the
  tests can hand it a fake machine; an empty root fails closed (exit 1). It changes nothing and uses no network;
- it gets only PATH, LC_ALL, FORK_FLEET_WALLS_ROOT and FORK_FLEET_LAUNCH_CONF (the launcher's conf, when the caller
  set it: what jobs see is the launcher's to say, `fork-fleet-launch.sh paths`, with that conf), no stdin, and TIMEOUT
  seconds.
FORK_FLEET_WALLS_ROOT is a test knob: refused (exit 2) when root runs this, as in real use, and otherwise unless it is
a folder of this user that it alone can write (fake_root). A caller needs no clean environment for it. The launcher
beside this file must be as safe as a walls file (it runs as root), and FORK_FLEET_LAUNCH_CONF a plain absolute path.
Standard library only."""
import glob, json, os, platform, re, stat, subprocess, sys

HERE = os.path.dirname(os.path.realpath(__file__))
WALLS = os.path.join(HERE, 'fork-fleet-walls')
LAUNCHER = os.path.join(HERE, 'fork-fleet-launch.sh')
PLAIN = re.compile(r'(/[A-Za-z0-9._@+-]+)+')
OS_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,31}')
# What a terminal must never get raw from a walls file: control characters (ESC, CR, ...), line and bidi marks.
CONTROL = '\x00-\x1f\x7f-\x9f\u061c\u200e\u200f\u2028\u2029\u202a-\u202e\u2066-\u2069'
LINE = re.compile(rf'(PASS|FAIL)  ([a-z0-9][a-z0-9-]{{0,47}})  ([^\s{CONTROL}][^{CONTROL}]{{0,400}})')
TIMEOUT = 60
PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
# What WSL leaves in every distro, whatever its kernel is called (a kernel of one's own, interop off): these paths,
# and WSL_CMDLINE in /proc/cmdline (set by WSL's own boot, whatever kernel it boots).
WSL_MARKS = ('/run/WSL', '/proc/sys/fs/binfmt_misc/WSLInterop*', '/usr/lib/wsl', '/mnt/wsl')
WSL_CMDLINE = 'WSL_ROOT_INIT'
USAGE = 'usage: fork-fleet-walls.py os | check | describe'


class Broken(Exception):
    """The walls file is unsafe or broke its contract: exit 2, no runner."""


def clean(text):
    """Text for a terminal: every control character (and line or bidi mark) as ?."""
    return re.sub(f'[{CONTROL}]', '?', text)


def read(root, path):
    try:
        with open(root + path, encoding='utf-8', errors='replace') as f:
            return f.read().strip()
    except OSError:
        return None


def fake_root():
    """$FORK_FLEET_WALLS_ROOT: a fake machine for the tests, or '' (the real one). Refused when root runs this (the
    real use: a root caller never gets a fake machine, whatever its environment holds) and otherwise unless it is an
    absolute path to a folder (not a link) of this user that it alone can write, as the walls folder."""
    root = os.environ.get('FORK_FLEET_WALLS_ROOT', '')
    if not root:
        return ''
    if not hasattr(os, 'geteuid') or os.geteuid() == 0:
        raise Broken(f'FORK_FLEET_WALLS_ROOT is set ({clean(root)[:200]!r}): it is for the tests only, never for '
                     'root; unset it')
    if not os.path.isabs(root):
        raise Broken(f'FORK_FLEET_WALLS_ROOT is not an absolute path: {clean(root)[:200]!r}')
    safe(root, 'dir')
    print(f'fork-fleet-walls: FORK_FLEET_WALLS_ROOT: the walls of the fake machine {clean(root)[:200]!r}, not of '
          'this one', file=sys.stderr)
    return root


def detect(root=''):
    """The OS this machine is, as a walls file's name. root: a fake machine (fake_root) or '' (the real one).
    WSL is Linux to its kernel: told apart by its kernel name or WSL's own marks; WSL1's kernel is `...-Microsoft`."""
    ostype = read(root, '/proc/sys/kernel/ostype')
    if ostype is None:   # no Linux /proc: name the system (a fake root that has none is unknown)
        name = 'unknown' if root else {'darwin': 'macos'}.get(platform.system().lower(), platform.system().lower())
    elif ostype != 'Linux':
        name = ostype.lower()
    else:
        release = read(root, '/proc/sys/kernel/osrelease') or ''
        if re.search(r'-Microsoft$', release):
            name = 'wsl1'
        elif ('microsoft' in release.lower() or any(glob.glob(glob.escape(root) + m) for m in WSL_MARKS)
              or WSL_CMDLINE in (read(root, '/proc/cmdline') or '')):
            name = 'wsl2'
        else:
            name = 'linux'
    name = re.sub(r'[^a-z0-9-]+', '-', name).strip('-')[:32]
    return name if OS_NAME.fullmatch(name) else 'unknown'


def safe(path, kind):
    """A path that is what kind says (dir, file: an executable one, data), not a link, owned by this user and writable
    by it alone (as the launcher's)."""
    try:
        st = os.lstat(path)
    except OSError:
        raise Broken(f'{path} is missing')
    if not (stat.S_ISDIR if kind == 'dir' else stat.S_ISREG)(st.st_mode):
        raise Broken(f'{path} is not a {"dir" if kind == "dir" else "file"} (or is a link)')
    if st.st_uid != os.geteuid() or st.st_mode & 0o022:
        raise Broken(f'{path} must belong to uid {os.geteuid()} and be writable by it alone')
    if kind == 'file' and not st.st_mode & 0o100:
        raise Broken(f'{path} is not executable')


def walls_file(name):
    """The walls file of OS name and the walls it must report, or None when that OS has none (not supported yet:
    asked before any safety check, so an OS with no walls is always exit 3)."""
    path = os.path.join(WALLS, name)
    if not OS_NAME.fullmatch(name) or not os.path.lexists(path) or not hasattr(os, 'geteuid'):
        return None
    safe(WALLS, 'dir')
    safe(path, 'file')
    fixture = path + '.fixture.json'
    safe(fixture, 'data')
    safe(LAUNCHER, 'file')
    try:
        with open(fixture, encoding='utf-8') as f:
            walls = json.load(f)['fail']
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise Broken(f'{fixture}: no list of walls (its `fail` map): {e.__class__.__name__}: {e}')
    if not isinstance(walls, dict) or 'os' not in walls or not all(LINE.fullmatch(f'PASS  {w}  x') for w in walls):
        raise Broken(f'{fixture}: its `fail` map does not name the walls (wall names, `os` among them)')
    return path, set(walls)


def run_walls(path, verb, root, want):
    """Runs the walls file's `describe` and holds it to the contract, each wall in want reported once: the lines
    for verb (check: only the FAIL lines), and whether every wall held."""
    env = {'PATH': PATH, 'LC_ALL': 'C'}
    if root:
        env['FORK_FLEET_WALLS_ROOT'] = root
    conf = os.environ.get('FORK_FLEET_LAUNCH_CONF', '')
    if conf:
        if not PLAIN.fullmatch(conf) or re.search(r'/\.\.?(/|$)', conf):
            raise Broken(f'FORK_FLEET_LAUNCH_CONF is not a plain absolute path: {clean(conf)[:200]!r}')
        env['FORK_FLEET_LAUNCH_CONF'] = conf
    name = os.path.basename(path)
    try:
        p = subprocess.run([path, 'describe'], env=env, stdin=subprocess.DEVNULL, capture_output=True,
                           timeout=TIMEOUT, cwd='/')
    except subprocess.TimeoutExpired:
        raise Broken(f'{name} describe took more than {TIMEOUT} s')
    except OSError as e:
        raise Broken(f'{name} describe did not run: {e}')
    err = p.stderr.decode('utf-8', 'replace').strip()
    if err:
        print(re.sub(f'[{CONTROL}]', lambda m: m[0] if m[0] in '\n\t' else '?', err[-2000:]), file=sys.stderr)
    lines = p.stdout.decode('utf-8', 'replace').splitlines()
    bad = next((x for x in lines if not LINE.fullmatch(x)), None)
    if bad is not None:
        raise Broken(f'{name} describe printed a line that is not `PASS|FAIL  <wall>  <what>`: {bad[:200]!r}')
    marks = [LINE.fullmatch(x).group(1, 2) for x in lines]
    walls = [w for _, w in marks]
    fails = [w for mark, w in marks if mark == 'FAIL']
    if len(set(walls)) != len(walls):
        raise Broken(f'{name} describe named a wall twice')
    if set(walls) != want:
        missing, extra = sorted(want - set(walls)), sorted(set(walls) - want)
        raise Broken(f'{name} describe did not report every wall of {name}.fixture.json, and only those (missing: '
                     f'{", ".join(missing) or "none"}; not listed: {", ".join(extra) or "none"}): a walls file that '
                     'was cut off, stopped early or is not the one its list belongs to')
    if p.returncode != (1 if fails else 0):
        raise Broken(f'{name} describe exited {p.returncode} with {len(fails)} walls failing (0 when none fails, '
                     '1 when one does)')
    return [x for x in lines if verb == 'describe' or x.startswith('FAIL')], not fails


def main(argv):
    if len(argv) != 1 or argv[0] not in ('os', 'check', 'describe'):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        root = fake_root()
        name = detect(root)
        found = walls_file(name)
        if found is None:
            print(f'fork-fleet-walls: this OS ({name}) is not supported yet: there are no walls for it '
                  f'(fork-fleet-walls/{name}), so no runner starts here', file=sys.stderr)
            return 3
        if argv[0] == 'os':
            print(name)
            return 0
        lines, held = run_walls(found[0], argv[0], root, found[1])
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
