"""capacity_readings: the machine's live state as the capacity rule's input, the kit's reader.

Mirrors packages/shared/src/capacityReadings.ts function by function (change both together); both answer every case
of packages/shared/src/capacity/capacity-readings.json the same way (tests/capacity_rule.test.py runs them). Files
and commands come through a host, so the cases run on a fake one: a host has `platform`, `read_file(path)` (a dict
{'text': ...} or {'error': 'missing' | 'unsupported' | 'unreadable'}), `run(file, args)` (stdout or None),
`total_mem_bytes()`, `free_mem_bytes()` and `cpu_count()`. A failed or half reading is None or 'unreadable': never
free room. Plain Python 3 standard library.
"""
import errno, math, os, re, subprocess, sys

MIB = 1048576
CGROUP = '/sys/fs/cgroup'
MAX_SAFE = 2 ** 53 - 1
A = re.ASCII


def _whole(text):
    """Digits only and a safe integer, else None."""
    if text is None or not re.fullmatch(r'[0-9]+', text, A):
        return None
    value = int(text)
    return value if value <= MAX_SAFE else None


def _mb_of(n):
    return None if n is None else math.floor(float(n) / MIB)


def _read_text(host, path):
    try:
        got = host.read_file(path)
    except Exception:
        return {'error': 'unreadable'}
    return got if isinstance(got, dict) and ('text' in got or 'error' in got) else {'error': 'unreadable'}


def _text_of(host, path):
    got = _read_text(host, path)
    return got['text'] if 'text' in got else None


def _meminfo_bytes(text, key):
    m = re.search(rf'^{re.escape(key)}:[ \t]+([0-9]+) kB$', text, re.M | A)
    kb = _whole(m.group(1) if m else None)
    return None if kb is None else kb * 1024


def _stat_bytes(text, key):
    m = re.search(rf'^{re.escape(key)} ([0-9]+)$', text, re.M | A)
    return _whole(m.group(1) if m else None)


def _one_number(text):
    if text is None:
        return None
    m = re.fullmatch(r'([0-9]+)\n?', text, A)
    return _whole(m.group(1) if m else None)


def _psi_line(kind, text):
    m = re.search(rf'^{kind} avg10=([0-9]+\.[0-9]+) avg60=[0-9]+\.[0-9]+ avg300=[0-9]+\.[0-9]+ total=[0-9]+$',
                  text, re.M | A)
    return m.group(1) if m else None


def _psi_of(read):
    """None when the file is not there or PSI is turned off; 'unreadable' when it fails, is broken or cut short."""
    if 'error' in read:
        return None if read['error'] in ('missing', 'unsupported') else 'unreadable'
    some, full = _psi_line('some', read['text']), _psi_line('full', read['text'])
    if some is None or full is None:
        return 'unreadable'
    some, full = float(some), float(full)
    return {'someAvg10Pct': some, 'fullAvg10Pct': full} if some <= 100 and full <= 100 else 'unreadable'


def cgroup_path(path):
    """A safe cgroup path (/a.slice/b.scope): below the root, never leaving the cgroup tree; else None."""
    if not isinstance(path, str) or not path.startswith('/') or '\0' in path:
        return None
    parts = [p for p in path.split('/') if p != '']
    return None if not parts or any(p in ('.', '..') for p in parts) else ''.join('/' + p for p in parts)


def _cgroup_version(host):
    v2 = _read_text(host, f'{CGROUP}/cgroup.controllers')
    if 'text' in v2:
        return 'v2'
    if v2['error'] != 'missing':
        return 'unreadable'
    v1 = _read_text(host, f'{CGROUP}/memory/memory.stat')
    if 'text' in v1:
        return 'v1'
    return None if v1['error'] == 'missing' else 'unreadable'


def _container_memory(host, cgroup, total_bytes):
    """The cgroup root's memory limit: None none, 'unreadable' unknown, else {limitBytes, usedBytes}."""
    def used(current, stat, inactive_key):
        now = _one_number(current)
        inactive = None if stat is None else _stat_bytes(stat, inactive_key)
        return None if now is None or inactive is None else max(0, now - inactive)
    if cgroup == 'unreadable':
        return 'unreadable'
    if cgroup == 'v2':
        mx = _read_text(host, f'{CGROUP}/memory.max')
        if 'error' in mx:
            return None if mx['error'] == 'missing' else 'unreadable'
        if re.fullmatch(r'max\n?', mx['text']):
            return None
        limit = _one_number(mx['text'])
        if limit is None:
            return 'unreadable'
        return {'limitBytes': limit, 'usedBytes': used(_text_of(host, f'{CGROUP}/memory.current'),
                                                        _text_of(host, f'{CGROUP}/memory.stat'), 'inactive_file')}
    if cgroup == 'v1':
        lim = _read_text(host, f'{CGROUP}/memory/memory.limit_in_bytes')
        if 'error' in lim:
            return None if lim['error'] == 'missing' else 'unreadable'
        if re.fullmatch(r'[0-9]{16,}\n?', lim['text'], A):        # "no limit" is a huge number
            return None
        limit = _one_number(lim['text'])
        if limit is None:
            return 'unreadable'
        if total_bytes is not None and limit >= total_bytes:
            return None
        return {'limitBytes': limit,
                'usedBytes': used(_text_of(host, f'{CGROUP}/memory/memory.usage_in_bytes'),
                                  _text_of(host, f'{CGROUP}/memory/memory.stat'), 'total_inactive_file')}
    return None


def _container_cpus(host, cgroup):
    if cgroup == 'unreadable':
        return 'unreadable'
    if cgroup != 'v2':
        return None
    mx = _read_text(host, f'{CGROUP}/cpu.max')
    if 'error' in mx:
        return None if mx['error'] == 'missing' else 'unreadable'
    if re.fullmatch(r'max [0-9]+\n?', mx['text'], A):
        return None
    m = re.fullmatch(r'([0-9]+) ([0-9]+)\n?', mx['text'], A)
    q, p = (_whole(m.group(1)), _whole(m.group(2))) if m else (None, None)
    return 'unreadable' if q is None or p is None or p == 0 or q == 0 else q / p


def _node_number(read):
    try:
        value = read()
    except Exception:
        return None
    ok = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) \
        and float(value).is_integer() and 0 < value <= MAX_SAFE
    return int(value) if ok else None


NO_DEMAND = {'reading': 'demand', 'instead': 'no per-piece memory without cgroups: every piece owes its full cost'}


def _read_linux(host):
    meminfo = _text_of(host, '/proc/meminfo') or ''
    total_bytes, avail_bytes = _meminfo_bytes(meminfo, 'MemTotal'), _meminfo_bytes(meminfo, 'MemAvailable')
    cgroup = _cgroup_version(host)
    container = _container_memory(host, cgroup, total_bytes)
    if container == 'unreadable':
        avail_bytes = None
    elif container is not None:
        if total_bytes is not None:
            total_bytes = min(total_bytes, container['limitBytes'])
        avail_bytes = None if container['usedBytes'] is None or avail_bytes is None else \
            min(avail_bytes, max(0, container['limitBytes'] - container['usedBytes']))
    total_mb, avail_mb = _mb_of(total_bytes), _mb_of(avail_bytes)
    swap_total, swap_free = _mb_of(_meminfo_bytes(meminfo, 'SwapTotal')), _mb_of(_meminfo_bytes(meminfo, 'SwapFree'))
    if swap_total is None or swap_free is None or swap_free > swap_total:
        swap = 'unreadable'
    else:
        swap = None if swap_total == 0 else {'totalMb': swap_total, 'freeMb': swap_free}
    psi = _psi_of(_read_text(host, '/proc/pressure/memory'))
    cpus, host_cpus = _container_cpus(host, cgroup), _node_number(host.cpu_count)
    cores = None if cpus == 'unreadable' or host_cpus is None else (host_cpus if cpus is None else
                                                                     min(host_cpus, cpus))
    gaps = []
    if psi is None:
        gaps.append({'reading': 'psi',
                     'instead': 'kernel without PSI (or PSI turned off): available memory against the margin only'})
    if cgroup is None:
        gaps.append(NO_DEMAND)
    return {'totalMb': total_mb,
            'availableMb': None if avail_mb is not None and total_mb is not None and avail_mb > total_mb else avail_mb,
            'cpuCores': cores, 'swap': swap, 'psi': psi, 'osLevel': None, 'cgroup': cgroup, 'gaps': gaps}


def _darwin_available_bytes(output):
    m = re.search(r'page size of ([0-9]+) bytes', output, A)
    page = _whole(m.group(1) if m else None)

    def pages(name):
        p = re.search(rf'^Pages {name}:\s+([0-9]+)\.$', output, re.M | A)
        return _whole(p.group(1) if p else None)
    free, inactive, speculative = pages('free'), pages('inactive'), pages('speculative')
    if page is None or page == 0 or free is None or inactive is None or speculative is None:
        return None
    n = (free + inactive + speculative) * page
    return n if n <= MAX_SAFE else None


DARWIN_LEVELS = {'1': 'ok', '2': 'elevated', '4': 'severe'}


def _run(host, file, args):
    try:
        return host.run(file, list(args))
    except Exception:
        return None


def _read_darwin(host):
    gaps = [{'reading': 'psi', 'instead': 'no PSI: the OS memory pressure level'},
            {'reading': 'swap', 'instead': 'swap files grow on demand (no fixed size): counted as no swap (wider '
                                           'margin)'}, NO_DEMAND]
    total_bytes = _node_number(host.total_mem_bytes)
    vm_stat = _run(host, '/usr/bin/vm_stat', [])
    avail_bytes = None if vm_stat is None else _darwin_available_bytes(vm_stat)
    if avail_bytes is None:
        avail_bytes = _node_number(host.free_mem_bytes)
        gaps.append({'reading': 'available', 'instead': 'vm_stat failed: free pages only (an under-read)'})
    level = _run(host, '/usr/sbin/sysctl', ['-n', 'kern.memorystatus_vm_pressure_level'])
    level = level.strip() if isinstance(level, str) else None
    total_mb, avail_mb = _mb_of(total_bytes), _mb_of(avail_bytes)
    return {'totalMb': total_mb,
            'availableMb': None if avail_mb is not None and total_mb is not None and avail_mb > total_mb else avail_mb,
            'cpuCores': _node_number(host.cpu_count), 'swap': None, 'psi': None,
            'osLevel': DARWIN_LEVELS.get(level, 'unreadable'), 'cgroup': None, 'gaps': gaps}


def _read_node_only(host):
    total_mb, avail_mb = _mb_of(_node_number(host.total_mem_bytes)), _mb_of(_node_number(host.free_mem_bytes))
    return {'totalMb': total_mb,
            'availableMb': None if avail_mb is not None and total_mb is not None and avail_mb > total_mb else avail_mb,
            'cpuCores': _node_number(host.cpu_count), 'swap': None, 'psi': None, 'osLevel': None, 'cgroup': None,
            'gaps': [{'reading': 'swap', 'instead': 'Node does not give the page file: counted as no swap (wider '
                                                    'margin)'},
                     {'reading': 'psi', 'instead': 'no PSI: available memory against the margin only'},
                     {'reading': 'osLevel', 'instead': 'no OS pressure level from Node: available memory against the '
                                                       'margin only'}, NO_DEMAND]}


def read_machine(host):
    """Reads the machine once: total, available, cores, swap, PSI, OS level, cgroup version, gaps."""
    if host.platform == 'linux':
        return _read_linux(host)
    if host.platform == 'darwin':
        return _read_darwin(host)
    return _read_node_only(host)


def read_box_psi(host, machine, box):
    """A box's memory PSI (/a.slice): None where the machine has no PSI or no cgroup v2; 'unreadable' when the box's
    file is missing, turned off, broken or cut short, or the cgroup version is unknown."""
    if machine['psi'] is None:
        return None
    if machine['cgroup'] == 'unreadable':
        return 'unreadable'
    if machine['cgroup'] != 'v2':
        return None
    path = cgroup_path(box)
    if path is None:
        return 'unreadable'
    psi = _psi_of(_read_text(host, f'{CGROUP}{path}/memory.pressure'))
    return 'unreadable' if psi is None else psi


def read_piece_demand(host, cgroup, scope):
    """One piece's memory from its cgroup (/a.slice/b.scope): {'gone': True} when its cgroup ended, else
    {gone: False, demandMb (None: owes its full cost), currentMb, peakMb}. A missing swap part counts 0."""
    unknown = {'gone': False, 'demandMb': None, 'currentMb': None, 'peakMb': None}
    path = cgroup_path(scope)
    if cgroup is None or cgroup == 'unreadable' or path is None:
        return unknown
    v2 = cgroup == 'v2'
    base = f'{CGROUP}{path}' if v2 else f'{CGROUP}/memory{path}'
    stat = _read_text(host, f'{base}/memory.stat')
    if 'error' in stat:
        return {'gone': True} if stat['error'] == 'missing' else unknown
    anon = _stat_bytes(stat['text'], 'anon' if v2 else 'total_rss')
    shmem = _stat_bytes(stat['text'], 'shmem' if v2 else 'total_shmem')
    swap = _one_number(_text_of(host, f'{base}/memory.swap.current')) if v2 else \
        _stat_bytes(stat['text'], 'total_swap')
    current = _one_number(_text_of(host, f'{base}/{"memory.current" if v2 else "memory.usage_in_bytes"}'))
    peak = _one_number(_text_of(host, f'{base}/{"memory.peak" if v2 else "memory.max_usage_in_bytes"}'))
    return {'gone': False, 'demandMb': None if anon is None or shmem is None else _mb_of(anon + shmem + (swap or 0)),
            'currentMb': _mb_of(current), 'peakMb': _mb_of(peak)}


def file_error(code):
    """How a failed read is sorted (errno): only "does not exist" is missing, only "turned off in the kernel" is
    unsupported; every other error is unreadable, so an EACCES on memory.max never reads as "no limit"."""
    if code in (errno.ENOENT, errno.ENOTDIR):
        return 'missing'
    if code == errno.EOPNOTSUPP:
        return 'unsupported'
    return 'unreadable'


class RealHost:
    """This machine; `root` (tests only) is a folder that holds a fake /proc and /sys."""

    def __init__(self, root=''):
        self.root = root
        self.platform = {'darwin': 'darwin', 'win32': 'win32'}.get(sys.platform, 'linux' if
                                                                   sys.platform.startswith('linux') else sys.platform)

    def read_file(self, path, limit=1 << 20):
        try:
            with open(self.root + path, 'rb') as f:
                return {'text': f.read(limit).decode('utf-8', 'replace')}
        except OSError as e:
            return {'error': file_error(e.errno)}

    def run(self, file, args):
        try:
            p = subprocess.run([file, *args], capture_output=True, timeout=2, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.SubprocessError):
            return None
        return p.stdout.decode('utf-8', 'replace') if p.returncode == 0 else None

    @staticmethod
    def total_mem_bytes():
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')

    @staticmethod
    def free_mem_bytes():
        return os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_AVPHYS_PAGES')

    def cpu_count(self):
        if self.root:                               # tests: the fake tree's online CPUs
            n = 0
            for part in self.read_file('/sys/devices/system/cpu/online').get('text', '').strip().split(','):
                lo, _, hi = part.partition('-')
                n += int(hi or lo) - int(lo) + 1
            return n
        return len(os.sched_getaffinity(0)) if hasattr(os, 'sched_getaffinity') else os.cpu_count()
