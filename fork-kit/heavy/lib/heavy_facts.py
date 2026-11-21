"""heavy facts: may this machine take one more piece of work? The capacity rule's answer as a free/v1 object.

  heavy facts [--kind KIND] [--mb MB] [--fresh]

Prints one free/v1 object (fork-kit/common/FREE.md) and exits 0 free, 75 busy, 1 when it cannot answer. --kind asks
for one piece of KIND: a kind of capacity-rule.json, a heavy job kind the rule knows as job-KIND (build: job-build),
else the rule's unknownKinds. --mb alone asks whether MB fit the free budget at level "ok"; with --kind it is the
piece's declared MB and must fit too. Neither: the rule's unknownKinds.job. "fits" says for every kind how many more
pieces of it the rule admits now, one after another (capacityFill up to the group's ceiling).

The ledger comes from the kernel only, so the answer stands with T3 down or not installed: /proc/meminfo, PSI, heavy's
admitted jobs (its live queue tickets; demand from each job's scope) and T3's agent scopes (`t3code-*.scope` under an
`app-t3code*` slice; a scope has no kind label, so it counts as unknownKinds.other: a writer). Full tier: cgroup v2
and a systemd user manager. Basic tier (either missing): no agent scopes or per-piece demand can be read, so jobs owe
their whole cost and the rule's wider margin applies. A signal that is there but unreadable is "elevated", never
"ok"; facts that cannot be read are busy.

Reservations: lines `<MB> <path prefix> [until=YYYY-MM-DD]` in *.conf files of $T3_CAPACITY_RESERVE_DIR, else
~/.config/t3-capacity/reserve.d. Each holds MB less what this user's processes working under the prefix use
(RssAnon + RssShmem + VmSwap); it does not hold back a caller working under its prefix; past its until date, or
broken, it holds nothing (a note says so).

Readings are kept 5 seconds in /run/user/<uid>/heavy-facts.json ($HEAVY_FACTS_CACHE; --fresh reads again), written
whole through a rename; a cache that is old, from the future, broken, of another version or not this user's own is
read again. The pressure level carries from one reading to the next (the rule's hysteresis) while the last one is at
most 5 minutes old. Learned job costs: heavy's peaks (lib/heavy_queue.py reservation(); missing or broken: start).
"""
import datetime, errno, fcntl, json, math, os, re, signal, stat, subprocess, sys, time

MB = 1 << 20
CACHE_SECONDS, CARRY_SECONDS, ANSWER_SECONDS, STALE_TMP_SECONDS = 5, 300, 5, 60
EXIT_FREE, EXIT_BUSY, EXIT_ERROR = 0, 75, 1
AGENT_SCOPE_RE = re.compile(r'^t3code-[A-Za-z0-9_.:-]+\.scope\Z')
SCOPE_RE = re.compile(r'^[A-Za-z0-9_.:@-]{1,200}\.scope\Z')
TICKET_RE = re.compile(r'^t-[0-9]{1,15}-[0-9]{1,10}\.json\Z')
KIND_RE = re.compile(r'^[a-z0-9][a-z0-9_.-]{0,59}\Z')
CACHE_KEYS = {'v', 'atMs', 'tier', 'facts', 'boxPsi', 'reservations', 'learned', 'carry', 'notes', 'load1', 'queue',
              'pressure'}
ROOT = os.environ.get('HEAVY_FACTS_ROOT', '')     # tests: /proc and /sys are read under this folder


def _read(path, limit=1 << 16):
    with open(ROOT + path, 'rb') as f:
        return f.read(limit).decode('ascii', 'replace')


def meminfo():
    """(totalMb, availableMb, swap): a missing figure is None; swap {totalMb, freeMb} or 'unreadable'."""
    got = {}
    try:
        for line in _read('/proc/meminfo').splitlines():
            key, _, rest = line.partition(':')
            words = rest.split()
            if len(words) == 2 and words[1] == 'kB' and words[0].isdigit():
                got[key] = int(words[0]) / 1024
    except OSError:
        pass
    swap = {'totalMb': got['SwapTotal'], 'freeMb': got['SwapFree']} if 'SwapTotal' in got and 'SwapFree' in got \
        else 'unreadable'
    return got.get('MemTotal'), got.get('MemAvailable'), swap


def box_psi(folder, machine_psi):
    """A box's PSI: None when the box is not there (a slice not started yet) or the kernel has no PSI at all; a box
    without its memory.pressure is 'unreadable' (as in the capacity README's readings table)."""
    if not folder or machine_psi is None or not os.path.isdir(ROOT + folder):
        return None
    return psi(folder + '/memory.pressure') or 'unreadable'


def psi(path):
    """{someAvg10Pct, fullAvg10Pct}; None when the file is not there (no PSI, no such box); 'unreadable' when it is
    there but cannot be read."""
    try:
        text = _read(path)
    except OSError as e:
        return None if e.errno in (errno.ENOENT, errno.EOPNOTSUPP) else 'unreadable'
    out = {}
    for line in text.splitlines():
        words = line.split()
        if words[:1] in (['some'], ['full']):
            try:
                out[words[0] + 'Avg10Pct'] = float(dict(w.split('=', 1) for w in words[1:] if '=' in w)['avg10'])
            except (KeyError, ValueError):
                return 'unreadable'
    return out if len(out) == 2 and all(math.isfinite(v) for v in out.values()) else 'unreadable'


def cpu():
    """(cores this process may use or None, 1-minute load or None)."""
    try:
        n = 0
        for part in _read('/sys/devices/system/cpu/online').strip().split(','):
            lo, _, hi = part.partition('-')
            n += int(hi or lo) - int(lo) + 1
    except (OSError, ValueError):
        n = None
    if not ROOT and hasattr(os, 'sched_getaffinity'):
        n = min(n or 1 << 20, len(os.sched_getaffinity(0)))
    try:
        load = float(_read('/proc/loadavg').split()[0])
    except (OSError, ValueError, IndexError):
        load = None
    return n, load


def demand_mb(folder):
    """anon + shmem + swap of a cgroup folder in MB (what it holds that is not page cache), None when unreadable."""
    try:
        st = dict(line.split() for line in _read(folder + '/memory.stat').splitlines() if len(line.split()) == 2)
        used = int(st['anon']) + int(st.get('shmem', 0))
    except (OSError, ValueError, KeyError):
        return None
    try:
        used += int(_read(folder + '/memory.swap.current'))
    except (OSError, ValueError):
        pass
    return used / MB


def slice_path(name):
    """systemd's folder of a slice: a-b.slice is a.slice/a-b.slice."""
    parts = name[:-len('.slice')].split('-')
    return '/'.join('-'.join(parts[:i + 1]) + '.slice' for i in range(len(parts)))


def agent_scopes(manager):
    """(T3's agent scope folders, the outermost app-t3code slice folder or None) under the user manager's cgroup."""
    scopes, box = [], None
    for dirpath, dirs, _files in os.walk(ROOT + manager):
        rel = dirpath[len(ROOT):]
        name = os.path.basename(rel)
        if name.startswith('app-t3code') and name.endswith('.slice') and (box is None or len(rel) < len(box)):
            box = rel
        if AGENT_SCOPE_RE.match(name) and '/app-t3code' in rel[len(manager):]:
            scopes.append(rel)
            dirs[:] = []
        else:
            dirs[:] = [d for d in dirs if d.endswith(('.slice', '.scope', '.service'))]
    return sorted(scopes), box


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
    """The rule's kind for a name: itself, job-NAME when the rule has that, else the rule's unknownKinds."""
    if kind in rule['kinds']:
        return kind
    return 'job-' + kind if 'job-' + kind in rule['kinds'] else cr.capacity_kind(rule, kind)


def reservations(cfg_dir, today):
    """([{name, prefix, mb, useMb}], notes)."""
    out, notes = [], []
    try:
        files = sorted(f for f in os.listdir(cfg_dir) if f.endswith('.conf'))
    except OSError:
        return out, notes
    for f in files:
        try:
            with open(os.path.join(cfg_dir, f), encoding='utf-8') as fh:
                lines = fh.read(1 << 16).splitlines()
        except (OSError, UnicodeDecodeError) as e:
            notes.append(f'reservation {f} cannot be read ({e.__class__.__name__}): holds nothing')
            continue
        for n, line in enumerate(lines, 1):
            words = line.split()
            if not words or words[0].startswith('#'):
                continue
            until = [w[6:] for w in words[2:] if w.startswith('until=')]
            try:
                if not (words[0].isdigit() and len(words) >= 2 and words[1].startswith('/')):
                    raise ValueError('not "<MB> <absolute path prefix>"')
                if until and datetime.date.fromisoformat(until[0]) < today:
                    notes.append(f'reservation {f}:{n} ended {until[0]}: holds nothing')
                    continue
            except ValueError as e:
                notes.append(f'reservation {f}:{n} ignored ({e})')
                continue
            out.append({'name': f[:-5], 'prefix': words[1], 'mb': int(words[0]), 'useMb': 0})
    if out:
        for pid in os.listdir(ROOT + '/proc'):
            try:
                if not pid.isdigit() or os.stat(f'{ROOT}/proc/{pid}').st_uid != os.getuid():
                    continue
                cwd = os.readlink(f'{ROOT}/proc/{pid}/cwd') + '/'
                st = dict(line.split(':', 1) for line in _read(f'/proc/{pid}/status').splitlines() if ':' in line)
                kb = sum(int(st.get(k, '0 kB').split()[0]) for k in ('RssAnon', 'RssShmem', 'VmSwap'))
            except (OSError, ValueError, IndexError):
                continue                            # gone meanwhile, or not readable
            for r in out:
                if cwd.startswith(r['prefix']):
                    r['useMb'] += kb / 1024
    return out, notes


def read_machine(rule, where, carry, now):
    """This machine's readings now: what the cache keeps (the same for every caller)."""
    notes = []
    uid = os.getuid()
    total, avail, swap = meminfo()
    cores, load = cpu()
    manager = f'/sys/fs/cgroup/user.slice/user-{uid}.slice/user@{uid}.service'
    full = os.path.isfile(ROOT + '/sys/fs/cgroup/cgroup.controllers') and os.path.isdir(ROOT + manager)
    heavy_box = manager + '/' + slice_path(where['slice'])
    pieces, running, waiting = [], 0, 0
    for t in tickets(where['queueDir']):
        if t['state'] == 'waiting':
            waiting += 1
            continue
        running += 1
        scope = t.get('scope')
        readable = full and isinstance(scope, str) and SCOPE_RE.match(scope)
        pieces.append({'kind': ask_kind(rule, t['kind']) if t['kind'] else rule['unknownKinds']['job'],
                       'demandMb': demand_mb(heavy_box + '/' + scope) if readable else None,
                       'declaredMb': t.get('mb')})
    scopes, agent_box = agent_scopes(manager) if full else ([], None)
    for s in scopes:
        pieces.append({'kind': '', 'demandMb': demand_mb(s)})     # no kind label: unknownKinds.other, a writer
    res, more = reservations(where['reserveDir'], now.date())
    notes += more
    if not full:
        notes.append('basic tier: no cgroup v2 or no systemd user manager (wider margin, agents not counted)')
    machine_psi = psi('/proc/pressure/memory')
    return {'v': 1, 'atMs': int(now.timestamp() * 1000), 'tier': 'full' if full else 'basic',
            'facts': {'totalMb': total, 'availableMb': avail, 'cpuCores': cores, 'pieceCaps': full, 'swap': swap,
                      'psi': machine_psi, 'osLevel': None, 'pieces': pieces},
            'boxPsi': {'job': box_psi(heavy_box if full else None, machine_psi),
                       'agent': box_psi(agent_box, machine_psi)},
            'reservations': res, 'learned': where['learned'](notes), 'carry': carry, 'notes': notes,
            'load1': load, 'queue': {'running': running, 'waiting': waiting, 'agents': len(scopes)}}


def read_cache(path, now_ms):
    """(readings while fresh, else None; the pressure state to carry on, else None). Anything odd is not trusted."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError:
        return None, None
    try:
        st = os.fstat(fd)
        if st.st_uid != os.getuid() or st.st_mode & 0o077 or not stat.S_ISREG(st.st_mode):
            return None, None
        data = cr.loads(os.read(fd, 1 << 20).decode('utf-8'))
    except (OSError, ValueError):
        return None, None
    finally:
        os.close(fd)
    if not (isinstance(data, dict) and data.get('v') == 1 and set(data) == CACHE_KEYS and type(data['atMs']) is int
            and isinstance(data['pressure'], dict) and isinstance(data['facts'], dict)):
        return None, None
    age = now_ms - data['atMs']
    carry = data['pressure'] if 0 <= age <= CARRY_SECONDS * 1000 else None
    return (data if age < CACHE_SECONDS * 1000 and carry is not None else None), carry


def write_cache(path, data):
    """Whole or not at all: a unique temporary file renamed over the cache. A temporary file left by a writer that
    stopped half-way is removed once it is a minute old."""
    folder, base = os.path.split(path)
    for name in os.listdir(folder or '.'):
        if name.startswith(base + '.') and name.endswith('.tmp'):
            try:
                if time.time() - os.lstat(os.path.join(folder, name)).st_mtime > STALE_TMP_SECONDS:
                    os.unlink(os.path.join(folder, name))
            except OSError:
                pass
    tmp = f'{path}.{os.getpid()}.{os.urandom(4).hex()}.tmp'
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, json.dumps(data, separators=(',', ':')).encode())
    finally:
        os.close(fd)
    os.replace(tmp, path)


def own(prefix, cwds):
    return any((c.rstrip('/') + '/').startswith(prefix) for c in cwds if c)


def facts_for(readings, group, cwds):
    """The rule's facts for a piece of the group, as its caller sees them (its own reservations left out)."""
    res = [{'mb': r['mb'], 'useMb': r['useMb']} for r in readings['reservations'] if not own(r['prefix'], cwds)]
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
    free = (d['admit'] if kind else d['level'] == 'ok') and (need_mb is None or d['freeMb'] is not None
                                                              and need_mb <= d['freeMb'])
    fits = {}
    for k, spec in rule['kinds'].items():
        g = spec['group']
        fits[k] = cr.fill(rule, facts_for(readings, g, cwds), [{'kind': k, 'count': rule['ceilings'][g]}], learned,
                          carry.get(g), now_ms)['admitted'][0]
    facts, swap = readings['facts'], readings['facts']['swap']
    swap = swap if isinstance(swap, dict) and swap['totalMb'] > 0 else None
    avail = None if facts['availableMb'] is None else math.floor(facts['availableMb'])
    out = {'v': 1, 'free': free, 'at': datetime.datetime.fromtimestamp(now_ms / 1000, datetime.timezone.utc)
           .isoformat(timespec='seconds'), 'kind': kind, 'level': d['level'], 'tier': readings['tier'], 'fits': fits,
           'memory': {'availableMb': avail, 'budgetMb': None if d['marginMb'] is None else avail - d['marginMb'],
                      'reservedMb': None if d['owedMb'] is None else d['owedMb'] + d['reservedMb'],
                      'freeMb': d['freeMb'], 'marginMb': d['marginMb'], 'owedMb': d['owedMb'],
                      'holdMb': d['reservedMb'], 'costMb': d['costMb'],
                      'swapFreeMb': math.floor(swap['freeMb']) if swap else 0,
                      'swapUsedPercent': round(100 - 100 * swap['freeMb'] / swap['totalMb'], 1) if swap else None,
                      'pressureFull10': facts['psi']['fullAvg10Pct'] if isinstance(facts['psi'], dict) else None},
           'queue': readings['queue'], 'cpu': {'cores': facts['cpuCores'], 'load1': readings['load1']},
           'reservations': [{'name': r['name'], 'mb': r['mb'], 'useMb': math.floor(r['useMb']),
                             'own': own(r['prefix'], cwds)} for r in readings['reservations']],
           'notes': list(readings['notes'])}
    if not free:
        out['reason'] = (f'{kind}: {d["reason"]}' if kind else f'{need_mb} MB') + \
            f' ({d["freeMb"]} MB free, {need_mb if need_mb is not None else d["costMb"]} MB needed, level {d["level"]})'
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
        run = f'/run/user/{os.getuid()}'
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
        cache = os.environ.get('HEAVY_FACTS_CACHE') or (run + '/heavy-facts.json' if os.path.isdir(run) else None)
        now = now or datetime.datetime.now(datetime.timezone.utc)
        now_ms = int(now.timestamp() * 1000)
        readings, carry = read_cache(cache, now_ms) if cache else (None, None)
        if readings is None or opts.get('--fresh'):
            readings = read_machine(rule, where, carry, now)
            readings['pressure'] = {g: cr.level(rule, dict(readings['facts'], boxPsi=readings['boxPsi'][g]),
                                                (carry or {}).get(g), now_ms)[1] for g in ('agent', 'job')}
            if cache:
                try:
                    write_cache(cache, readings)
                except OSError as e:
                    readings['notes'].append(f'cache not written ({e.strerror or e})')
        code, out = answer(rule, readings, opts.get('--kind'), int(opts['--mb']) if '--mb' in opts else None,
                           (os.getcwd(), os.environ.get('PWD', '')))
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
