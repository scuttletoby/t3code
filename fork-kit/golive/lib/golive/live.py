"""What the kit reads about a T3 home and its running server; it writes nothing.

File names and shapes are upstream's (tests/contract.test.py checks they still exist in the server code):
<home>/runtime/service-state.json, .restart-pending, .service-stopping, db-backup/<id>/.restore-pending,
versions/<v>/t3; <home>/userdata/server-runtime.json and statev2.sqlite.
"""
import glob, hashlib, json, os, platform, re, subprocess, sys, tarfile, tempfile

from golive.config import EXACT_VERSION

DATABASE = 'statev2.sqlite'
REPORT_VERSION = 1
REPORT_TIMEOUT = 120


class NotLive(Exception):
    pass


def compare_versions(left, right):
    """Upstream's compareExactServiceVersions (serviceProtocol.ts): -1, 0 or 1."""
    def parse(v):
        v = v.split('+', 1)[0]
        core, _, pre = v.partition('-')
        return [int(x) for x in (core.split('.') + ['0', '0'])[:3]], (pre.split('.') if pre else [])
    (a, ap), (b, bp) = parse(left), parse(right)
    if a != b:
        return -1 if a < b else 1
    if not ap or not bp:
        return 0 if len(ap) == len(bp) else (1 if not ap else -1)
    for x, y in zip(ap, bp):
        if x == y:
            continue
        xn, yn = x.isdigit(), y.isdigit()
        if xn and yn:
            return -1 if int(x) < int(y) else 1
        if xn != yn:
            return -1 if xn else 1
        return -1 if x < y else 1
    return 0 if len(ap) == len(bp) else (-1 if len(ap) < len(bp) else 1)


def service_state(home):
    """(activeVersion, pendingUpdate or None) from runtime/service-state.json; NotLive when missing or bad."""
    path = os.path.join(home, 'runtime', 'service-state.json')
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
    except FileNotFoundError:
        raise NotLive(f'{path} is missing: this home was not installed as a boot service') from None
    except (OSError, ValueError) as e:
        raise NotLive(f'{path} cannot be read: {e}') from None
    if not isinstance(state, dict):
        state = {}
    active = state.get('activeVersion')
    if state.get('protocol') not in (3, 4) or not isinstance(active, str) or not EXACT_VERSION.match(active):
        raise NotLive(f'{path} is not a service state this kit knows (protocol 3 or 4)')
    update = state.get('update')
    return active, (update if isinstance(update, dict) and update.get('status') == 'pending' else None)


def markers(home):
    """Upstream markers that mean an update, a stop or a restore is in flight."""
    runtime = os.path.join(home, 'runtime')
    found = [os.path.join(runtime, m) for m in ('.restart-pending', '.service-stopping')
             if os.path.lexists(os.path.join(runtime, m))]
    return found + sorted(glob.glob(os.path.join(glob.escape(runtime), 'db-backup', '*', '.restore-pending')))


def _proc(pid):
    """(exe real path, parent pid) of a live process, or None."""
    try:
        exe = os.readlink(f'/proc/{pid}/exe')
        with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as f:
            stat = f.read()
    except OSError:
        return None
    if exe.endswith(' (deleted)'):
        exe = exe[:-len(' (deleted)')]
    return exe, int(stat[stat.rindex(')') + 2:].split()[1])


def live_server(home, service_main_pid):
    """The server serving `home`, found from its own runtime file and its process (never from PATH):
    {'pid', 'version', 'binary', 'port'}. NotLive with the reason when anything disagrees."""
    path = os.path.join(home, 'userdata', 'server-runtime.json')
    try:
        with open(path, encoding='utf-8') as f:
            rt = json.load(f)
    except FileNotFoundError:
        raise NotLive(f'{path} is missing: no server is running for this home') from None
    except (OSError, ValueError) as e:
        raise NotLive(f'{path} cannot be read: {e}') from None
    if not isinstance(rt, dict):
        rt = {}
    pid = rt.get('pid')
    if rt.get('version') != 1 or type(pid) is not int or pid <= 0:
        raise NotLive(f'{path} is not a server runtime record this kit knows')
    proc = _proc(pid)
    if proc is None:
        raise NotLive(f'the server pid {pid} in {path} is not running')
    exe, ppid = proc
    versions = os.path.realpath(os.path.join(home, 'runtime', 'versions'))
    m = re.fullmatch(re.escape(versions) + r'/([^/]+)/t3', exe)
    if m is None:
        raise NotLive(f'pid {pid} runs {exe}, not a runtime of this home')
    if service_main_pid is None or ppid != service_main_pid:
        raise NotLive(f'pid {pid} is not started by the boot service (its parent is {ppid}, '
                      f'the service runs {service_main_pid})')
    version = m.group(1)
    if rt.get('serverVersion') not in (None, version):
        raise NotLive(f'{path} says version {rt.get("serverVersion")} but the process runs {version}')
    return {'pid': pid, 'version': version, 'binary': exe, 'port': rt.get('port')}


def database_bytes(home):
    """Size of the database with its WAL, or None when there is no database."""
    db = os.path.join(home, 'userdata', DATABASE)
    if not os.path.isfile(db):
        return None
    wal = db + '-wal'
    return os.path.getsize(db) + (os.path.getsize(wal) if os.path.isfile(wal) else 0)


def platform_key():
    arch = {'x86_64': 'x64', 'amd64': 'x64', 'aarch64': 'arm64', 'arm64': 'arm64'}.get(platform.machine().lower())
    os_name = 'linux' if sys.platform.startswith('linux') else sys.platform
    return f'{os_name}-{arch}' if arch else None


def _sha256(stream):
    h = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1 << 20), b''):
        h.update(chunk)
    return h.hexdigest()


def staged_runtime(stage_dir, version):
    """Check the staged release: SHA256SUMS lists the archive and its hash matches, and the extracted runtime in
    <stage>/<version>/ holds exactly the archive's files, byte for byte. Returns the runtime's t3 path.
    Raises NotLive with the reason. Reads only; the archive is read as a stream."""
    key = platform_key()
    if key is None or key.startswith('win32'):
        raise NotLive(f'no release archive for this machine ({sys.platform} {platform.machine()})')
    archive = f't3-{version}-{key}.tar.gz'
    sums = os.path.join(stage_dir, 'SHA256SUMS')
    try:
        with open(sums, encoding='utf-8') as f:
            listed = {m.group(2): m.group(1).lower() for m in
                      (re.match(r'^([0-9a-fA-F]{64})\s+\*?(\S.*)$', line.strip()) for line in f) if m}
    except OSError as e:
        raise NotLive(f'{sums} cannot be read: {e}') from None
    if archive not in listed:
        raise NotLive(f'{archive} is not listed in {sums}')
    archive_path = os.path.join(stage_dir, archive)
    try:
        with open(archive_path, 'rb') as f:
            if _sha256(f) != listed[archive]:
                raise NotLive(f'{archive_path} does not match its SHA256SUMS line')
    except OSError as e:
        raise NotLive(f'{archive_path} cannot be read: {e}') from None
    runtime = os.path.join(stage_dir, version)
    seen = set()
    try:
        with tarfile.open(archive_path, 'r:gz') as tar:
            for member in tar:
                parts = member.name.split('/', 1)
                if len(parts) < 2 or not parts[1] or member.isdir():
                    continue
                rel = parts[1]
                if rel.startswith('/') or '..' in rel.split('/') or not member.isfile():
                    raise NotLive(f'{archive} holds {rel}, which is not a plain file')
                seen.add(rel)
                local = os.path.join(runtime, rel)
                try:
                    with open(local, 'rb') as f:
                        same = _sha256(f) == _sha256(tar.extractfile(member))
                except OSError:
                    raise NotLive(f'{local} is missing: extract {archive} into {runtime} '
                                  '(tar -xzf ... --strip-components=1)') from None
                if not same:
                    raise NotLive(f'{local} differs from the file in {archive}')
    except (tarfile.TarError, EOFError) as e:
        raise NotLive(f'{archive_path} cannot be read as an archive: {e}') from None
    extra = sorted(os.path.relpath(os.path.join(d, n), runtime) for d, _, names in os.walk(runtime)
                   for n in names) if os.path.isdir(runtime) else []
    extra = [e for e in extra if e not in seen]
    if extra:
        raise NotLive(f'{runtime} holds files that are not in {archive}: {", ".join(extra[:5])}')
    binary = os.path.join(runtime, 't3')
    if 't3' not in seen or not os.access(binary, os.X_OK):
        raise NotLive(f'{binary} is not an executable from {archive}')
    return binary


def fork_report(binary, home):
    """Run `<binary> __fork-report --base-dir <home>` (fork PR "read-only report command": it writes nothing,
    takes no lock and is safe beside a running server) with an empty HOME and a bare PATH. Returns the report."""
    with tempfile.TemporaryDirectory(prefix='golive-report-') as empty_home:
        try:
            p = subprocess.run([binary, '__fork-report', '--base-dir', home], capture_output=True, text=True,
                               timeout=REPORT_TIMEOUT, stdin=subprocess.DEVNULL, cwd=empty_home,
                               env={'HOME': empty_home, 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8'})
        except (OSError, subprocess.TimeoutExpired) as e:
            raise NotLive(f'the report command failed: {e}') from None
    out = p.stdout
    start = re.search(r'^\{', out, re.M)  # log lines before the JSON are tolerated
    try:
        report, _ = json.JSONDecoder().raw_decode(out[start.start():]) if start else (None, 0)
    except ValueError:
        report = None
    if not isinstance(report, dict):
        raise NotLive(f'the report command printed no report (exit {p.returncode}): {p.stderr.strip()[:300]}')
    if report.get('reportVersion') != REPORT_VERSION:
        raise NotLive(f'the report has version {report.get("reportVersion")}; this kit reads {REPORT_VERSION}')
    return report
