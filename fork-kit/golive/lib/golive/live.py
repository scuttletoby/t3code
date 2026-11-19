"""What the kit reads about a T3 home and its running server; it writes nothing.

File names and shapes are upstream's (tests/contract.test.py checks they still exist in the server code):
<home>/runtime/service-state.json, .restart-pending, .service-stopping, db-backup/<id>/.restore-pending,
versions/<v>/t3; <home>/userdata/server-runtime.json and statev2.sqlite.
"""
import ctypes, glob, hashlib, json, os, platform, re, signal, subprocess, sys, tarfile, tempfile

from golive.config import EXACT_VERSION

DATABASE = 'statev2.sqlite'
REPORT_VERSION = 1
REPORT_TIMEOUT = 120


class NotLive(Exception):
    pass


# A t3 build the kit runs gets this environment only: no T3CODE_RELEASE_BASE_URL (or anything else) can point it
# elsewhere.
T3_ENV = ('HOME', 'PATH', 'USER', 'LOGNAME', 'LANG', 'LC_ALL', 'TMPDIR', 'XDG_RUNTIME_DIR',
          'DBUS_SESSION_BUS_ADDRESS', 'T3CODE_INSTALL_BIN_DIR')
PR_SET_PDEATHSIG = 1


def t3_env():
    return {k: v for k, v in os.environ.items() if k in T3_ENV}


def _dies_with(parent):
    """A preexec_fn: the child gets SIGKILL when the process that started it dies (Linux PR_SET_PDEATHSIG), and
    exits at once if that already happened before the request took hold."""
    prctl = ctypes.CDLL(None, use_errno=True).prctl if sys.platform.startswith('linux') else None

    def preexec():
        if prctl is not None and prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0) != 0:
            os._exit(126)
        if os.getppid() != parent:
            os._exit(126)
    return preexec


def _kill_group(p):
    try:
        os.killpg(p.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


def run_t3(argv, timeout, cwd='/'):
    """(exit code, stdout, stderr) of a t3 build run with T3_ENV only. The exit code is None when it could not run
    or ran past `timeout` (then killed). The child runs in its own process group (a Ctrl-C in the terminal does not
    reach it: the run decides) and dies with this process (a killed run never leaves a `t3 update` going beside the
    guard's way back); anything raised while it runs kills its group first."""
    try:
        p = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, errors='replace', cwd=cwd, env=t3_env(), start_new_session=True,
                             preexec_fn=_dies_with(os.getpid()))
    except OSError as e:
        return None, '', str(e)
    try:
        out, err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _kill_group(p)
        out, err = p.communicate()
        return None, out, f'{err}\nkilled: still running after {timeout} s'
    except BaseException:
        _kill_group(p)
        p.wait()
        raise
    return p.returncode, out, err


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
    """(exe path, parent pid) of a live process, or None. A binary deleted or replaced on disk keeps its
    " (deleted)" suffix, so it never matches a runtime path."""
    try:
        exe = os.readlink(f'/proc/{pid}/exe')
        with open(f'/proc/{pid}/stat', encoding='utf-8', errors='replace') as f:
            stat = f.read()
    except OSError:
        return None
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
    if exe.endswith(' (deleted)'):
        raise NotLive(f'pid {pid} runs {exe}: its binary was deleted or replaced on disk')
    if m is None:
        raise NotLive(f'pid {pid} runs {exe}, not a runtime of this home')
    if service_main_pid is None or ppid != service_main_pid:
        raise NotLive(f'pid {pid} is not started by the boot service (its parent is {ppid}, '
                      f'the service runs {service_main_pid})')
    version = m.group(1)
    if rt.get('serverVersion') not in (None, version):
        raise NotLive(f'{path} says version {rt.get("serverVersion")} but the process runs {version}')
    return {'pid': pid, 'version': version, 'binary': exe, 'port': rt.get('port')}


def traces(home):
    """Files that say T3 ran in `home` as a service: its service state, its server runtime file, markers."""
    found = [p for p in (os.path.join(home, 'runtime', 'service-state.json'),
                         os.path.join(home, 'userdata', 'server-runtime.json')) if os.path.lexists(p)]
    return found + markers(home)


def database_aside(home):
    """Other files named like the database (a copy moved aside, for example), not its WAL or index."""
    userdata = os.path.join(home, 'userdata')
    return sorted(p for p in glob.glob(os.path.join(glob.escape(userdata), DATABASE + '?*'))
                  if not p.endswith(('-wal', '-shm', '-journal')))


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


def staged_archive(stage_dir, version):
    """(archive path, sha256) of the staged release archive of `version` for this machine, once SHA256SUMS lists
    it and its bytes match that line. Raises NotLive with the reason. Reads only."""
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
    return archive_path, listed[archive]


class _Hashing:
    """A read-only stream that hashes every byte read through it (tarfile reads the archive once, in order)."""

    def __init__(self, f):
        self.f, self.h = f, hashlib.sha256()

    def read(self, n=-1):
        data = self.f.read(n)
        self.h.update(data)
        return data

    def rest(self):
        for chunk in iter(lambda: self.f.read(1 << 20), b''):
            self.h.update(chunk)
        return self.h.hexdigest()


def runtime_matches(archive_path, runtime, extra_ok=()):
    """Check that the runtime folder holds exactly the archive's files, byte for byte (no links; `extra_ok`: names
    at its top the archive lacks, such as upstream's .install-complete). Returns the sha256 of the archive bytes it
    compared against, read in the same pass, so a caller that wants a given archive compares that sum. Raises
    NotLive. Reads only."""
    archive = os.path.basename(archive_path)
    seen = set()
    try:
        with open(archive_path, 'rb') as raw:
            stream = _Hashing(raw)
            with tarfile.open(fileobj=stream, mode='r|gz') as tar:
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
                        if os.path.islink(local):
                            raise OSError('a link')
                        with open(local, 'rb') as f:
                            same = _sha256(f) == _sha256(tar.extractfile(member))
                    except OSError:
                        raise NotLive(f'{local} is missing: extract {archive} into {runtime} '
                                      '(tar -xzf ... --strip-components=1)') from None
                    if not same:
                        raise NotLive(f'{local} differs from the file in {archive}')
            digest = stream.rest()
    except OSError as e:
        raise NotLive(f'{archive_path} cannot be read: {e}') from None
    except (tarfile.TarError, EOFError) as e:
        raise NotLive(f'{archive_path} cannot be read as an archive: {e}') from None
    extra, links = [], []
    for d, dirs, names in (os.walk(runtime) if os.path.isdir(runtime) else ()):
        for n in dirs + names:
            rel = os.path.relpath(os.path.join(d, n), runtime)
            if os.path.islink(os.path.join(d, n)):
                links.append(rel)  # os.walk does not go into a linked folder, and a linked file is not the archive's
            elif n in names and rel not in seen and rel not in extra_ok:
                extra.append(rel)
    if links:
        raise NotLive(f'{runtime} holds links, which the archive does not: {", ".join(sorted(links)[:5])}')
    extra.sort()
    if extra:
        raise NotLive(f'{runtime} holds files that are not in {archive}: {", ".join(extra[:5])}')
    if 't3' not in seen or not os.access(os.path.join(runtime, 't3'), os.X_OK):
        raise NotLive(f'{os.path.join(runtime, "t3")} is not an executable from {archive}')
    return digest


def staged_build(stage_dir, version):
    """(t3 path, archive sha256) of the staged release: SHA256SUMS lists the archive and its hash matches, and the
    extracted runtime in <stage>/<version>/ holds exactly the archive's files, byte for byte. Raises NotLive."""
    archive_path, digest = staged_archive(stage_dir, version)
    runtime = os.path.join(stage_dir, version)
    if runtime_matches(archive_path, runtime) != digest:
        raise NotLive(f'{archive_path} changed while it was read: stage it again')
    return os.path.join(runtime, 't3'), digest


def staged_runtime(stage_dir, version):
    """The staged release's t3 path, checked as staged_build() checks it. Raises NotLive with the reason."""
    return staged_build(stage_dir, version)[0]


COUNTS = ('projects', 'projectsArchived', 'threads', 'threadsArchived')


def compare_counts(before, after):
    """Fork #181: the differences between the database counts of two fork reports ([] when every count is equal).
    A report counts every project and every thread that is not deleted (sub-agent and archived threads included,
    across all projects), so counts are compared report with report, made the same way (for example the copy's
    against the same data after the target build migrated it), never with `t3 thread list`, which lists one project
    and leaves sub-agents out. A count either report could not read (no report, no database, a schema that lacks
    the table: null) or that is not a whole number cannot be compared, and is named as a difference: unknown is
    never equal."""
    def counts(report):
        db = report.get('database') if isinstance(report, dict) else None
        return db.get('counts') if isinstance(db, dict) and isinstance(db.get('counts'), dict) else {}
    a, b = counts(before), counts(after)
    said = []
    for key in COUNTS:
        x, y = a.get(key), b.get(key)
        if not all(type(v) is int for v in (x, y)):
            said.append(f'{key} cannot be compared ({x} before, {y} after): a report could not count it')
        elif x != y:
            said.append(f'{key}: {x} before, {y} after')
    return said


def features_on(fork):
    """The report's fork features that are on, each with where (`handoff (project:p1, thread:t1)`), so an operator
    knows which project override or thread entry to clear. A report without `featuresWhere` names the features only."""
    where = fork.get('featuresWhere') if isinstance(fork.get('featuresWhere'), dict) else {}
    on = fork.get('featuresOn') if isinstance(fork.get('featuresOn'), list) else []
    return ', '.join(f'{name} ({", ".join(map(str, where[name]))})' if isinstance(where.get(name), list) and where[name]
                     else str(name) for name in on)


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
