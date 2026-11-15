"""heavy sweep: find what a command left behind when nothing else cleans it up, stop the test-shaped part, report
the rest (fork-kit/heavy/README.md, "Leftover sweep").

  heavy sweep               one sweep: stop or report, write the log and the short report
  heavy sweep --dry-run     the same, but stop nothing (also the default until heavy.sweep.stop is true)
  heavy sweep --json        look only (no stop, no log, no lock): {"v": 1, "leftovers": [...], ...} on stdout
  heavy docker-label        print the owner label for a container a test starts: fork-kit.heavy.owner=<owner>
  heavy docker-label --value [--pid PID]   only the value; --pid names the owner process

It finds, for the user it runs as:
  (a) processes whose owner is gone: reparented to the user manager (or pid 1), not a unit's own main or control
      process, not in a unit of heavy.sweep.knownUnits, not in a live heavy scope, older than minAgeSeconds, and
      busy (busyCpuPercent over a one-second sample) or long-lived (longLivedSeconds). A test-shaped one (its
      command line matches heavy.sweep.testPatterns) is stopped with what it started; the others are reported.
      A heavy scope whose heavy is gone (heavy killed with kill -9) is stopped as a whole, as heavy would have
      done; one kept on purpose (HEAVY_KEEP=1) is only reported.
  (b) Docker containers with the label fork-kit.heavy.owner of this user whose owner (a heavy scope, or a pid
      with its start time) is gone: removed with their anonymous volumes. Running containers without the label
      are reported, never touched.
  (c) heavy scopes with no process left in them (a heavy killed while it started): stopped.

Settings: the "heavy" section of the machine file (fork-kit/common/README.md), key "sweep". No file, no
section, an unknown key or a newer version: nothing is checked or stopped, the reason is printed, exit 1.
"""
import contextlib, datetime, fcntl, fnmatch, importlib.machinery, importlib.util, json, os, re, signal, subprocess
import sys, tempfile, time

KIT = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
LABEL = 'fork-kit.heavy.owner'
KEEP_PREFIX = 'heavy-keep-'        # heavy names a scope kept with HEAVY_KEEP=1 heavy-keep-*.scope: never stopped
DEFAULT_REPORT = '~/.local/state/heavy-sweep'
LOG_MAX = 1 << 20
MAX_TEXT = 160


def _load_machine_file():
    """fork-kit/common/machine_file.py: in the version folder once installed, next to the kit in the repository."""
    for folder in (os.path.join(KIT, 'common'), os.path.join(os.path.dirname(KIT), 'common')):
        path = os.path.join(folder, 'machine_file.py')
        if os.path.isfile(path):
            loader = importlib.machinery.SourceFileLoader('fork_kit_machine_file', path)
            spec = importlib.util.spec_from_loader('fork_kit_machine_file', loader)
            mod = importlib.util.module_from_spec(spec)
            loader.exec_module(mod)
            return mod
    raise ImportError('fork-kit/common/machine_file.py not found next to the heavy kit')


machine_file = _load_machine_file()
MachineFileError = machine_file.MachineFileError


def _regexes(value):
    if not all(isinstance(v, str) and v for v in value):
        return 'must be a list of non-empty strings'
    for v in value:
        try:
            re.compile(v)
        except re.error as e:
            return f'{v!r} is not a regular expression ({e})'
    return None


def _globs(value):
    return None if all(isinstance(v, str) and v for v in value) else 'must be a list of non-empty strings'


def _report_dir(value):
    path = os.path.expanduser(value)
    return None if os.path.isabs(path) else 'must be an absolute folder (or start with ~/)'


SPEC = {
    'minAgeSeconds': ((int, float), 600, lambda v: None if v >= 60 else 'must be at least 60'),
    'longLivedSeconds': ((int, float), 7200, lambda v: None if v >= 60 else 'must be at least 60'),
    'busyCpuPercent': ((int, float), 20, lambda v: None if 0 < v <= 10000 else 'must be more than 0'),
    'stop': (bool, False, None),
    'report': (str, DEFAULT_REPORT, _report_dir),
    'knownUnits': (list, [], _globs),
    'knownContainers': (list, [], _globs),
    'testPatterns': (list, [], _regexes),
    'ignorePatterns': (list, [], _regexes),
}
SECTION_SPEC = {'sweep': (dict, machine_file.REQUIRED, None)}


class Refused(Exception):
    pass


class Proc:
    __slots__ = ('pid', 'ppid', 'uid', 'state', 'comm', 'cmdline', 'cgroup', 'unit', 'start', 'ticks', 'age', 'cpu')

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    def text(self):
        cmd = self.cmdline or f'[{self.comm}]'
        cmd = cmd if len(cmd) <= MAX_TEXT else cmd[:MAX_TEXT - 3] + '...'
        return (f'pid {self.pid} {self.comm} ({duration(self.age)} old, cpu {self.cpu:.0f}%, '
                f'unit {self.unit or "?"}): {cmd}')


def duration(seconds):
    s = int(max(0, seconds or 0))
    if s >= 86400:
        return f'{s // 86400}d{s % 86400 // 3600}h'
    if s >= 3600:
        return f'{s // 3600}h{s % 3600 // 60}m'
    if s >= 60:
        return f'{s // 60}m'
    return f'{s}s'


def unit_of(cgroup):
    """The innermost unit (.service or .scope) of a cgroup path, or ''."""
    for part in reversed((cgroup or '').split('/')):
        if part.endswith(('.service', '.scope')):
            return part
    return ''


def is_heavy_scope(unit):
    return unit.startswith('heavy-') and unit.endswith('.scope')


def is_kept_scope(unit):
    return unit.startswith(KEEP_PREFIX) and unit.endswith('.scope')


def parse_owner(value):
    """{'uid': int, 'scope': str} or {'uid': int, 'pid': int, 'start': int}, or None for a bad label."""
    try:
        parts = dict(p.split('=', 1) for p in value.split(','))
        uid = int(parts['uid'])
        if set(parts) == {'uid', 'scope'} and is_heavy_scope(parts['scope']):
            return {'uid': uid, 'scope': parts['scope']}
        if set(parts) == {'uid', 'pid', 'start'}:
            return {'uid': uid, 'pid': int(parts['pid']), 'start': int(parts['start'])}
    except (ValueError, KeyError, AttributeError):
        pass
    return None


def parse_created(text):
    """Docker's Created ('2026-11-12T10:00:00.123456789Z' or with an offset) as epoch seconds, or None."""
    m = re.match(r'^(\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d)(?:\.\d+)?(Z|[+-]\d\d:\d\d)$', text or '')
    if not m:
        return None
    tz = '+00:00' if m.group(2) == 'Z' else m.group(2)
    return datetime.datetime.fromisoformat(m.group(1) + tz).timestamp()


class Sweep:
    # Fixed places; tests point them at fakes.
    proc = '/proc'
    systemctl = None            # found in /usr/bin or /bin
    docker = None               # found in /usr/bin, /bin, /usr/local/bin; '' = not installed
    machine_path = None         # machine_file.default_path()
    owner_uid = 0               # who must own the machine file
    sample_seconds = 1.0
    stop_grace = 5.0

    def __init__(self):
        self.uid = os.getuid()
        self.pid = os.getpid()
        self.clk = os.sysconf('SC_CLK_TCK')
        self._now = time.time
        self._sleep = time.sleep
        self._pidfd_open = getattr(os, 'pidfd_open', None)
        self._pidfd_send = getattr(signal, 'pidfd_send_signal', None)
        self._close = os.close
        self.notes, self.items, self.log_file = [], [], None

    # ---------- settings ----------

    def settings(self):
        section = machine_file.read_section('heavy', self.machine_path, self.owner_uid)
        path = self.machine_path or machine_file.default_path()
        machine_file.fields(section, SECTION_SPEC, f'{path}: heavy')
        cfg = machine_file.fields(section['sweep'], SPEC, f'{path}: heavy.sweep')
        cfg['report'] = os.path.expanduser(cfg['report'])
        cfg['path'] = path
        return cfg

    # ---------- processes ----------

    def boot_time(self):
        with open(f'{self.proc}/stat', encoding='ascii') as f:
            for line in f:
                if line.startswith('btime '):
                    return int(line.split()[1])
        raise Refused(f'{self.proc}/stat has no btime')

    def read_proc(self, pid):
        base = f'{self.proc}/{pid}'
        try:
            with open(f'{base}/stat', encoding='utf-8', errors='replace') as f:
                stat = f.read()
            with open(f'{base}/status', encoding='utf-8', errors='replace') as f:
                status = f.read()
            with open(f'{base}/cmdline', 'rb') as f:
                cmdline = f.read()
            with open(f'{base}/cgroup', encoding='utf-8', errors='replace') as f:
                cgroup = ''.join(line[3:].strip() for line in f if line.startswith('0::'))
        except OSError:
            return None
        try:
            left, right = stat.find('('), stat.rfind(')')
            rest = stat[right + 2:].split()
            uid = next(int(line.split()[1]) for line in status.splitlines() if line.startswith('Uid:'))
            return Proc(pid=int(pid), comm=stat[left + 1:right], state=rest[0], ppid=int(rest[1]),
                        ticks=int(rest[11]) + int(rest[12]), start=int(rest[19]), uid=uid,
                        cmdline=' '.join(a.decode('utf-8', 'replace') for a in cmdline.split(b'\0') if a),
                        cgroup=cgroup, unit=unit_of(cgroup), cpu=0.0)
        except (ValueError, IndexError, StopIteration):
            return None

    def scan(self):
        out = {}
        try:
            names = os.listdir(self.proc)
        except OSError as e:
            raise Refused(f'cannot list {self.proc}: {e.strerror}')
        for name in names:
            if name.isdigit():
                p = self.read_proc(name)
                if p is not None and p.uid == self.uid and p.state != 'Z':
                    out[p.pid] = p
        return out

    def sample(self):
        """Two scans a moment apart: the second, with each process's age and its CPU use in between."""
        first, t0 = self.scan(), time.monotonic()
        self._sleep(self.sample_seconds)
        procs, dt = self.scan(), max(time.monotonic() - t0, self.sample_seconds, 0.001)
        btime, now = self.boot_time(), self._now()
        for p in procs.values():
            p.age = now - (btime + p.start / self.clk)
            before = first.get(p.pid)
            if before is not None and before.start == p.start:
                p.cpu = max(0.0, (p.ticks - before.ticks) / self.clk / dt * 100)
        return procs

    def alive(self, p):
        cur = self.read_proc(p.pid)
        return cur is not None and cur.start == p.start and cur.uid == p.uid and cur.state != 'Z'

    def signal(self, p, sig):
        """Send sig to exactly this process: 'sent', 'gone', or 'reused' (the pid is another process now). The
        pidfd pins the process before its start time is compared, so a pid reused in between is never signalled."""
        if self._pidfd_open is None or self._pidfd_send is None:
            if not self.alive(p):
                return 'gone' if self.read_proc(p.pid) is None else 'reused'
            try:
                os.kill(p.pid, sig)
            except ProcessLookupError:
                return 'gone'
            return 'sent'
        try:
            fd = self._pidfd_open(p.pid)
        except ProcessLookupError:
            return 'gone'
        try:
            cur = self.read_proc(p.pid)
            if cur is None or cur.state == 'Z':
                return 'gone'
            if cur.start != p.start or cur.uid != p.uid:
                return 'reused'
            self._pidfd_send(fd, sig)
            return 'sent'
        except ProcessLookupError:
            return 'gone'
        finally:
            self._close(fd)

    def stop_procs(self, procs):
        """TERM, then KILL after the grace time. Returns (stopped, reused, still running) lists of Proc."""
        reused, sent = [], []
        for p in procs:
            r = self.signal(p, signal.SIGTERM)
            (reused if r == 'reused' else sent).append(p)
        end = time.monotonic() + self.stop_grace
        left = [p for p in sent if self.alive(p)]
        while left and time.monotonic() < end:
            self._sleep(0.1)
            left = [p for p in left if self.alive(p)]
        for p in left:
            if self.signal(p, signal.SIGKILL) == 'reused':
                reused.append(p)
        end = time.monotonic() + 5
        left = [p for p in left if p not in reused and self.alive(p)]
        while left and time.monotonic() < end:
            self._sleep(0.1)
            left = [p for p in left if self.alive(p)]
        stopped = [p for p in sent if p not in left and p not in reused]
        return stopped, reused, left

    # ---------- systemd ----------

    def run_tool(self, argv, timeout=60):
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=timeout)

    def systemctl_path(self):
        if self.systemctl:
            return self.systemctl
        for folder in ('/usr/bin', '/bin'):
            path = os.path.join(folder, 'systemctl')
            if os.access(path, os.X_OK):
                return path
        raise Refused('systemctl not found in /usr/bin or /bin (the sweep needs systemd)')

    def units(self, names):
        """{unit: {MainPID, ControlPID, Description, ActiveState}} from the user manager."""
        names = sorted({n for n in names if n})
        if not names:
            return {}
        try:
            r = self.run_tool([self.systemctl_path(), '--user', 'show', '-p',
                               'Id,MainPID,ControlPID,Description,ActiveState', '--', *names])
        except (OSError, subprocess.SubprocessError) as e:
            raise Refused(f'cannot reach the systemd user manager ({e})')
        if r.returncode != 0:
            err = (r.stderr.strip() or f'exit {r.returncode}').splitlines()[-1]
            raise Refused(f'cannot reach the systemd user manager ({err})')
        out = {}
        for block in r.stdout.split('\n\n'):
            props = dict(line.partition('=')[::2] for line in block.splitlines() if '=' in line)
            if props.get('Id'):
                out[props['Id']] = props
        return out

    def heavy_scope_units(self):
        try:
            r = self.run_tool([self.systemctl_path(), '--user', 'list-units', '--all', '--plain', '--no-legend',
                               '--full', 'heavy-*.scope'])
        except (OSError, subprocess.SubprocessError) as e:
            raise Refused(f'cannot reach the systemd user manager ({e})')
        if r.returncode != 0:
            err = (r.stderr.strip() or f'exit {r.returncode}').splitlines()[-1]
            raise Refused(f'cannot reach the systemd user manager ({err})')
        return [line.split()[0] for line in r.stdout.splitlines() if line.split() and is_heavy_scope(line.split()[0])]

    def stop_unit(self, unit):
        try:
            r = self.run_tool([self.systemctl_path(), '--user', 'stop', '--', unit], timeout=self.stop_grace * 2 + 30)
            self.run_tool([self.systemctl_path(), '--user', 'reset-failed', '--', unit])
            return r.returncode == 0, (r.stderr.strip().splitlines() or [''])[-1]
        except (OSError, subprocess.SubprocessError) as e:
            return False, str(e)

    # ---------- docker ----------

    def docker_path(self):
        if self.docker is not None:
            return self.docker if self.docker and os.access(self.docker, os.X_OK) else ''
        for folder in ('/usr/bin', '/bin', '/usr/local/bin'):
            path = os.path.join(folder, 'docker')
            if os.access(path, os.X_OK):
                return path
        return ''

    def containers(self):
        """Inspected containers: labelled ones (any state) and running ones. None (with a note) when Docker is not
        there or does not answer."""
        docker = self.docker_path()
        if not docker:
            self.note('docker: not installed; containers not checked')
            return None
        try:
            labelled = self.run_tool([docker, 'ps', '-a', '-q', '--no-trunc', '--filter', f'label={LABEL}'])
            running = self.run_tool([docker, 'ps', '-q', '--no-trunc'])
        except (OSError, subprocess.SubprocessError) as e:
            self.note(f'docker: {e}; containers not checked')
            return None
        for r in (labelled, running):
            if r.returncode != 0:
                err = (r.stderr.strip() or f'exit {r.returncode}').splitlines()[-1]
                self.note(f'docker: {err}; containers not checked')
                return None
        ids = sorted(set(labelled.stdout.split()) | set(running.stdout.split()))
        if not ids:
            return []
        try:
            r = self.run_tool([docker, 'inspect', '--type', 'container', *ids])
            data = json.loads(r.stdout or '[]')   # a container gone since `ps` is left out (exit 1, the rest printed)
        except (OSError, subprocess.SubprocessError, ValueError) as e:
            self.note(f'docker inspect: {e}; containers not checked')
            return None
        return data if isinstance(data, list) else []

    def remove_container(self, cid):
        try:
            r = self.run_tool([self.docker_path(), 'rm', '-f', '-v', '--', cid], timeout=120)
        except (OSError, subprocess.SubprocessError) as e:
            return f'not removed ({e})'
        if r.returncode == 0:
            return 'removed'
        err = (r.stderr.strip() or f'exit {r.returncode}').splitlines()[-1]
        return 'already gone' if 'No such container' in err else f'not removed ({err})'

    # ---------- report ----------

    def note(self, text):
        self.notes.append(text)

    def log(self, text):
        if self.log_file is None:
            return
        stamp = datetime.datetime.fromtimestamp(self._now(), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')
        with contextlib.suppress(OSError):
            self.log_file.write(f'{stamp} {text}\n')
            self.log_file.flush()

    # ---------- the sweep ----------

    def find(self, cfg):
        """Everything the sweep would act on: (procs, plan). plan is a list of dicts with 'kind' (process, scope,
        empty-scope, container), 'action' (stop, report, remove) and the reason."""
        listed = self.heavy_scope_units()
        procs = self.sample()
        manager = next((p.pid for p in procs.values() if p.comm == 'systemd'
                        and p.cgroup.endswith(f'/user@{self.uid}.service/init.scope')), None)
        orphan_parents = {1} | ({manager} if manager else set())

        mine = set()                        # this sweep and the processes that started it
        pid = self.pid
        while pid in procs and pid not in mine:
            mine.add(pid)
            pid = procs[pid].ppid
        mine.add(self.pid)

        members = {}
        for p in procs.values():
            if is_heavy_scope(p.unit):
                members.setdefault(p.unit, []).append(p)
        info = self.units({p.unit for p in procs.values() if p.ppid in orphan_parents and not is_heavy_scope(p.unit)})

        children = {}
        for p in procs.values():
            children.setdefault(p.ppid, []).append(p)

        def tree(p):
            out, todo = [], [p]
            while todo:
                q = todo.pop()
                if q.pid in mine or q in out:
                    continue
                out.append(q)
                todo += children.get(q.pid, [])
            return out

        plan, live_scopes, kept_scopes = [], set(), set()
        for unit, ps in sorted(members.items()):
            pids = {p.pid for p in ps}
            if any(p.ppid not in orphan_parents and p.ppid not in pids for p in ps) or pids & mine:
                live_scopes.add(unit)
            elif is_kept_scope(unit):
                kept_scopes.add(unit)
            elif max(p.age for p in ps) >= cfg['minAgeSeconds']:
                plan.append({'kind': 'scope', 'action': 'stop', 'unit': unit, 'procs': ps,
                             'why': 'its heavy is gone'})
            else:
                live_scopes.add(unit)       # young: its heavy may be stopping it right now; next sweep
        for unit in listed:
            if unit not in members and not is_kept_scope(unit):
                plan.append({'kind': 'empty-scope', 'action': 'stop', 'unit': unit, 'procs': [],
                             'why': 'no process left in it'})

        for p in sorted(procs.values(), key=lambda q: q.pid):
            if p.pid in mine or p.pid in (1, manager) or p.ppid not in orphan_parents:
                continue
            if p.unit in live_scopes or (is_heavy_scope(p.unit) and p.unit not in kept_scopes):
                continue                     # a live heavy scope, or a dead one stopped as a whole above
            if any(fnmatch.fnmatchcase(p.unit, g) for g in cfg['knownUnits']):
                continue
            u = info.get(p.unit, {})
            if str(p.pid) in (u.get('MainPID'), u.get('ControlPID')):
                continue                     # the unit's own process (a service's daemon)
            if p.age < cfg['minAgeSeconds'] or any(re.search(r, p.cmdline) for r in cfg['ignorePatterns']):
                continue
            busy, long_lived = p.cpu >= cfg['busyCpuPercent'], p.age >= cfg['longLivedSeconds']
            if not (busy or long_lived):
                continue
            why = ' and '.join(w for w, on in (('busy', busy), ('long-lived', long_lived)) if on)
            test = next((r for r in cfg['testPatterns'] if re.search(r, p.cmdline)), None)
            if p.unit in kept_scopes:
                plan.append({'kind': 'process', 'action': 'report', 'procs': tree(p),
                             'why': f'{why}; kept on purpose (HEAVY_KEEP=1 in {p.unit})'})
            elif test:
                plan.append({'kind': 'process', 'action': 'stop', 'procs': tree(p),
                             'why': f'{why}; test-shaped ({test!r})'})
            else:
                plan.append({'kind': 'process', 'action': 'report', 'procs': tree(p),
                             'why': f'{why}; not test-shaped'})

        stopping = {item['unit'] for item in plan if item['kind'] == 'scope'}
        for c in self.containers() or []:
            item = self.judge_container(c, cfg, procs, members, stopping)
            if item:
                plan.append(item)
        return procs, plan

    def judge_container(self, c, cfg, procs, members, stopping):
        try:
            cid = c['Id'][:12]
            name = (c.get('Name') or '').lstrip('/')
            labels = (c.get('Config') or {}).get('Labels') or {}
            image = (c.get('Config') or {}).get('Image') or '?'
            running = bool((c.get('State') or {}).get('Running'))
        except (KeyError, TypeError, AttributeError):
            return None
        if any(fnmatch.fnmatchcase(name, g) for g in cfg['knownContainers']):
            return None
        created = parse_created(c.get('Created'))
        age = self._now() - created if created is not None else 0
        base = {'kind': 'container', 'id': cid, 'name': name, 'image': image, 'age': age, 'running': running,
                'procs': []}
        if LABEL not in labels:
            if running and age >= cfg['minAgeSeconds']:
                return dict(base, action='report', why='no owner label')
            return None
        owner = parse_owner(labels[LABEL])
        if owner is None:
            return dict(base, action='report', why=f'bad owner label {labels[LABEL]!r}') if running else None
        if owner['uid'] != self.uid:
            return None                      # another user's: their sweep decides
        if 'scope' in owner:
            gone = owner['scope'] not in members or owner['scope'] in stopping
            what = f'owner {owner["scope"]}'
        else:
            p = procs.get(owner['pid'])
            gone = p is None or p.start != owner['start']
            what = f'owner pid {owner["pid"]}'
        if not gone or age < cfg['minAgeSeconds']:
            return None
        return dict(base, action='remove', why=f'{what} is gone')

    def run(self, cfg, act):
        procs, plan = self.find(cfg)
        counts = {'stopped': 0, 'reported': 0, 'removed': 0, 'problems': 0}
        lines = []
        for item in plan:
            ps = item['procs']
            if item['kind'] in ('scope', 'empty-scope'):
                head = f'{item["unit"]}: {item["why"]}' + (f'; {len(ps)} processes' if ps else '')
                if not act:
                    lines.append(f'would stop {head}')
                    self.log(f'would-stop {head}')
                    continue
                self.log(f'stopping {head}')
                for p in ps:
                    self.log(f'  in it: {p.text()}')
                ok, err = self.stop_unit(item['unit'])
                left = [p for p in ps if self.alive(p)]
                if left:
                    _, _, left = self.stop_procs(left)
                if left:
                    counts['problems'] += 1
                    lines.append(f'NOT stopped {head}: {len(left)} still running ({err})')
                else:
                    counts['stopped'] += len(ps) if ps else 0
                    lines.append(f'stopped {head}')
                self.log(f'result {item["unit"]}: ' + (f'{len(left)} still running' if left else 'stopped'))
            elif item['kind'] == 'process':
                top = ps[0]
                more = f' (+{len(ps) - 1} it started)' if len(ps) > 1 else ''
                head = f'{top.text()}{more}; {item["why"]}'
                if item['action'] == 'report' or not act:
                    verb = 'would stop' if item['action'] == 'stop' else 'report only'
                    counts['reported'] += item['action'] == 'report'
                    lines.append(f'{verb}: {head}')
                    self.log(f'{verb.replace(" ", "-")} {head}')
                    continue
                self.log(f'stopping {head}')
                stopped, reused, left = self.stop_procs(ps)
                counts['stopped'] += len(stopped)
                text = f'stopped: {head}'
                if reused:
                    text += f'; {len(reused)} pids now belong to another process, left alone'
                if left:
                    counts['problems'] += 1
                    text += f'; {len(left)} still running: {", ".join(str(p.pid) for p in left)}'
                lines.append(text)
                self.log(f'result pid {top.pid}: {len(stopped)} stopped, {len(reused)} reused, {len(left)} left')
            else:
                head = (f'container {item["id"]} {item["name"]} ({item["image"]}, {duration(item["age"])} old, '
                        f'{"running" if item["running"] else "not running"}); {item["why"]}')
                if item['action'] == 'report' or not act:
                    verb = 'would remove' if item['action'] == 'remove' else 'report only'
                    counts['reported'] += item['action'] == 'report'
                    lines.append(f'{verb}: {head}')
                    self.log(f'{verb.replace(" ", "-")} {head}')
                    continue
                self.log(f'removing {head}')
                result = self.remove_container(item['id'])
                if result in ('removed', 'already gone'):
                    counts['removed'] += result == 'removed'
                else:
                    counts['problems'] += 1
                lines.append(f'{result}: {head}')
                self.log(f'result container {item["id"]}: {result}')
        return plan, counts, lines


def leftovers_json(plan):
    """Section 3 of the free/v1 contract: [{"comm", "count", "cpuPercent"}], busiest first."""
    groups = {}
    for item in plan:
        if item['kind'] not in ('process', 'scope'):
            continue
        for p in item['procs']:
            g = groups.setdefault(p.comm, {'comm': p.comm, 'count': 0, 'cpuPercent': 0.0})
            g['count'] += 1
            g['cpuPercent'] += p.cpu
    out = sorted(groups.values(), key=lambda g: (-g['cpuPercent'], -g['count'], g['comm']))
    for g in out:
        g['cpuPercent'] = round(g['cpuPercent'])
    return out


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def sweep_main(argv, sweep=None, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    if argv[:1] in (['-h'], ['--help']):
        print(__doc__.split('\n\n')[1], file=out)
        return 0
    if any(a not in ('--dry-run', '--json') for a in argv):
        print('usage: heavy sweep [--dry-run] [--json]', file=err)
        return 2
    as_json, dry = '--json' in argv, '--dry-run' in argv
    s = sweep or Sweep()
    try:
        cfg = s.settings()
    except MachineFileError as e:
        return refuse(f'{e}; nothing checked or stopped', as_json, out, err)
    stamp = datetime.datetime.fromtimestamp(s._now(), datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    if as_json:
        try:
            _, plan = s.find(cfg)
        except Refused as e:
            return refuse(f'{e}; nothing checked', True, out, err)
        print(json.dumps({'v': 1, 'at': stamp, 'leftovers': leftovers_json(plan),
                          'stop': [item_json(i) for i in plan if i['action'] in ('stop', 'remove')],
                          'report': [item_json(i) for i in plan if i['action'] == 'report'],
                          'notes': s.notes}, sort_keys=True), file=out)
        return 0

    act = cfg['stop'] and not dry
    folder = cfg['report']
    try:
        os.makedirs(folder, mode=0o700, exist_ok=True)
        lock = open(os.path.join(folder, 'sweep.lock'), 'a+', encoding='utf-8')
    except OSError as e:
        return refuse(f'cannot use the report folder {folder} ({e.strerror}); nothing checked or stopped',
                      False, out, err)
    with lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('heavy sweep: another sweep is running; this one did nothing', file=out)
            return 0
        log_path = os.path.join(folder, 'sweep.log')
        with contextlib.suppress(OSError):
            if os.path.getsize(log_path) > LOG_MAX:
                os.replace(log_path, log_path + '.1')
        try:
            s.log_file = open(log_path, 'a', encoding='utf-8')
        except OSError as e:
            return refuse(f'cannot write {log_path} ({e.strerror}); nothing checked or stopped', False, out, err)
        with s.log_file:
            mode = 'stop' if act else ('dry run (--dry-run)' if dry else f'dry run (heavy.sweep.stop is off in '
                                                                         f'{cfg["path"]})')
            s.log(f'sweep start: {mode}')
            try:
                plan, counts, lines = s.run(cfg, act)
            except Refused as e:
                s.log(f'refused: {e}')
                return refuse(f'{e}; nothing stopped', False, out, err)
            would = sum(1 for i in plan if i['action'] in ('stop', 'remove')) if not act else 0
            summary = (f'heavy sweep {stamp}, {mode}: '
                       + (f'{would} to stop or remove, ' if not act else
                          f'{counts["stopped"]} processes stopped, {counts["removed"]} containers removed, ')
                       + f'{counts["reported"]} reported' + (f', {counts["problems"]} problems'
                                                              if counts['problems'] else ''))
            report = '\n'.join([summary] + [f'  {line}' for line in lines] + [f'  note: {n}' for n in s.notes]) + '\n'
            s.log(f'sweep end: {summary}')
        with contextlib.suppress(OSError):
            write_atomic(os.path.join(folder, 'last-report.txt'), report)
        out.write(report)
        return 1 if counts['problems'] else 0


def item_json(item):
    out = {'kind': item['kind'], 'action': item['action'], 'why': item['why']}
    if item['kind'] in ('scope', 'empty-scope'):
        out['unit'] = item['unit']
    if item['kind'] == 'container':
        out.update(id=item['id'], name=item['name'])
    if item['procs']:
        out['pids'] = [p.pid for p in item['procs']]
        out['comm'] = item['procs'][0].comm
    return out


def refuse(text, as_json, out, err):
    if as_json:
        print(json.dumps({'v': 1, 'error': text}), file=out)
    print(f'heavy sweep: {text}', file=err)
    return 1


def owner_value(pid=None, proc='/proc'):
    """The fork-kit.heavy.owner label value for a container: the heavy scope this runs in (HEAVY_SCOPE, which heavy
    gives its command, or else the own cgroup), or a process (the caller's parent, or pid) with its start time, so a
    reused pid never counts as the owner."""
    uid = os.getuid()
    if pid is None:
        scope = os.environ.get('HEAVY_SCOPE', '')
        if is_heavy_scope(scope) and re.fullmatch(r'[A-Za-z0-9_.:-]+', scope):
            return f'uid={uid},scope={scope}'
        try:
            with open(f'{proc}/self/cgroup', encoding='utf-8') as f:
                leaf = unit_of(''.join(line[3:].strip() for line in f if line.startswith('0::')))
        except OSError:
            leaf = ''
        if is_heavy_scope(leaf):
            return f'uid={uid},scope={leaf}'
        pid = os.getppid()
    with open(f'{proc}/{pid}/stat', encoding='utf-8', errors='replace') as f:
        stat = f.read()
    start = int(stat[stat.rfind(')') + 2:].split()[19])
    return f'uid={uid},pid={pid},start={start}'


def label_main(argv, out=None, err=None):
    out, err = out or sys.stdout, err or sys.stderr
    value_only, pid, args = False, None, list(argv)
    while args:
        a = args.pop(0)
        if a == '--value':
            value_only = True
        elif a == '--pid' and args and args[0].isdigit():
            pid = int(args.pop(0))
        else:
            print('usage: heavy docker-label [--value] [--pid PID]', file=err)
            return 2
    try:
        value = owner_value(pid)
    except (OSError, ValueError, IndexError) as e:
        print(f'heavy docker-label: cannot read the owner process ({e})', file=err)
        return 1
    print(value if value_only else f'{LABEL}={value}', file=out)
    return 0
