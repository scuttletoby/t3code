#!/usr/bin/env python3
"""Fork only: a job's memory pressure numbers, for the log and for .github/scripts/fork-ci-rerun.py.

  python3 -I fork-ci-pressure.py start   right after a job's checkout: saves a sample to $RUNNER_TEMP.
  python3 -I fork-ci-pressure.py end     the job's last step (if: always(), JOB_STATUS=${{ job.status }}): prints
                                         one log line with the numbers since `start`, and, when the job did not
                                         succeed, the same numbers as one notice annotation (NOTE_TITLE, NOTE_HEAD +
                                         compact JSON) that fork-ci-rerun.py reads from GitHub after the run.

Facts only, no decision: what counts as a stalled or full machine is fork-ci-rerun.py's, from its config. The numbers:
- the machine's memory pressure (/proc/pressure/memory, PSI): the share of the job's time that some or all tasks
  waited for memory (from the `total` counters), and the 10 s, 60 s and 300 s averages at the end;
- /proc/meminfo's available memory and swap in use, and /proc/vmstat's OOM kill count since `start`;
- the job's own cgroup (v2) and each ancestor with a memory limit (the box the job runs in: a systemd slice or
  unit with MemoryHigh or MemoryMax): limit, use, and memory.events' high, max and oom_kill since `start`, and its
  own pressure share.
Never fails the job: a file it cannot read is null, a missing start sample makes every "since start" number null,
and any other error prints a warning and exits 0. Standard library only.
"""
import json, os, sys, time

NOTE_TITLE = 'fork-ci-pressure'
NOTE_HEAD = 'fork-ci-pressure v1 '
SAMPLE = 'fork-ci-pressure-start.json'
EVENTS = ('high', 'max', 'oom', 'oom_kill')


def read(path):
    try:
        with open(path, encoding='utf-8') as f:
            return f.read()
    except (OSError, UnicodeDecodeError):
        return None


def psi(text):
    """/proc/pressure/memory or a cgroup's memory.pressure: {"some": {...}, "full": {...}} (avg in %, total in µs)."""
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        kind, _, rest = line.partition(' ')
        if kind not in ('some', 'full'):
            continue
        fields = {}
        for part in rest.split():
            key, _, value = part.partition('=')
            try:
                fields[key] = float(value) if key.startswith('avg') else int(value)
            except ValueError:
                pass
        out[kind] = fields
    return out or None


def keyed(text, scale=1):
    """'Key: 123 kB' or 'key 123' lines as {key: number}."""
    if text is None:
        return None
    out = {}
    for line in text.splitlines():
        parts = line.replace(':', ' ').split()
        if len(parts) >= 2:
            try:
                out[parts[0]] = int(parts[1]) // scale
            except ValueError:
                pass
    return out


def limit(text):
    """memory.max / memory.high: bytes, or None for 'max' (no limit) or unreadable."""
    text = (text or '').strip()
    return int(text) if text.isdigit() else None


def own_cgroup(proc):
    """The cgroup v2 path of this process ('0::/a/b' in /proc/self/cgroup), or None."""
    for line in (read(os.path.join(proc, 'self/cgroup')) or '').splitlines():
        if line.startswith('0::'):
            return line[3:].strip() or '/'
    return None


def cgroups(proc, root):
    """The own cgroup and every ancestor with a memory limit, innermost first."""
    path = own_cgroup(proc)
    out = []
    while path:
        where = os.path.join(root, path.lstrip('/'))
        high, top = limit(read(os.path.join(where, 'memory.high'))), limit(read(os.path.join(where, 'memory.max')))
        if not out or high is not None or top is not None:
            events = keyed(read(os.path.join(where, 'memory.events')))
            current = read(os.path.join(where, 'memory.current'))
            out.append({'path': path, 'high_bytes': high, 'max_bytes': top,
                        'current_bytes': int(current) if (current or '').strip().isdigit() else None,
                        'events': {k: events[k] for k in EVENTS if k in events} if events else None,
                        'psi': psi(read(os.path.join(where, 'memory.pressure')))})
        if path == '/':
            break
        path = os.path.dirname(path.rstrip('/')) or '/'
    return out


def sample(proc='/proc', root='/sys/fs/cgroup', now=time.time):
    vm = keyed(read(os.path.join(proc, 'vmstat'))) or {}
    return {'t': now(), 'boot': (read(os.path.join(proc, 'sys/kernel/random/boot_id')) or '').strip() or None,
            'psi': psi(read(os.path.join(proc, 'pressure/memory'))),
            'mem_kb': keyed(read(os.path.join(proc, 'meminfo'))), 'oom_kill': vm.get('oom_kill'),
            'cgroups': cgroups(proc, root)}


def share(a, b, kind, seconds):
    """% of `seconds` that `kind` (some/full) pressure counted between two PSI readings, or None."""
    try:
        return round((b[kind]['total'] - a[kind]['total']) / 1e6 / seconds * 100, 1)
    except (KeyError, TypeError, ZeroDivisionError):
        return None


def delta(a, b):
    return b - a if isinstance(a, int) and isinstance(b, int) and b >= a else None


def mb(n):
    return None if n is None else n // (1024 * 1024)


def numbers(start, end):
    """The note: what fork-ci-rerun.py reads. Every 'since start' number is null without a usable start sample."""
    t = start.get('t') if isinstance(start, dict) else None
    same = isinstance(t, (int, float)) and start.get('boot') == end.get('boot') and end['t'] > t
    start = start if same else {}
    seconds = end['t'] - t if same else None
    psi_end, mem = end.get('psi') or {}, end.get('mem_kb') or {}
    full, some = psi_end.get('full') or {}, psi_end.get('some') or {}
    swap = (mem['SwapTotal'] - mem['SwapFree']) // 1024 if 'SwapTotal' in mem and 'SwapFree' in mem else None
    host = {'full_share': share(start.get('psi'), psi_end, 'full', seconds) if same else None,
            'some_share': share(start.get('psi'), psi_end, 'some', seconds) if same else None,
            'full_avg10': full.get('avg10'), 'full_avg60': full.get('avg60'), 'full_avg300': full.get('avg300'),
            'some_avg60': some.get('avg60'), 'some_avg300': some.get('avg300'),
            'available_mb': mem['MemAvailable'] // 1024 if 'MemAvailable' in mem else None, 'swap_used_mb': swap,
            'oom_kill': delta(start.get('oom_kill'), end.get('oom_kill')) if same else None}
    before = {c['path']: c for c in (start.get('cgroups') or [])} if same else {}
    boxes = []
    for c in end.get('cgroups') or []:
        b = before.get(c['path']) or {}
        ev_a, ev_b = b.get('events') or {}, c.get('events') or {}
        boxes.append({'path': c['path'], 'high_mb': mb(c.get('high_bytes')), 'max_mb': mb(c.get('max_bytes')),
                      'current_mb': mb(c.get('current_bytes')),
                      **{k: delta(ev_a.get(k), ev_b.get(k)) for k in EVENTS},
                      'full_share': share(b.get('psi'), c.get('psi'), 'full', seconds) if b else None})
    return {'seconds': None if seconds is None else int(seconds), 'host': host, 'cgroups': boxes}


def line(status, n):
    h = n['host']
    out = [f'fork-ci-pressure: job {status}', f'{n["seconds"]} s since start' if n['seconds'] is not None
           else 'no start sample (numbers since start unknown)',
           f'memory stall full {h["full_share"]}% some {h["some_share"]}% of the job',
           f'full avg10/60/300 {h["full_avg10"]}/{h["full_avg60"]}/{h["full_avg300"]}%',
           f'available {h["available_mb"]} MB, swap used {h["swap_used_mb"]} MB, OOM kills {h["oom_kill"]}']
    for c in n['cgroups']:
        out.append(f'cgroup {c["path"]}: high {c["high_mb"]} MB max {c["max_mb"]} MB use {c["current_mb"]} MB, '
                   f'events high {c["high"]} max {c["max"]} oom_kill {c["oom_kill"]}, full {c["full_share"]}%')
    return '; '.join(out)


def command(text):
    """A workflow command's data, escaped as the runner wants (%, CR, LF)."""
    return text.replace('%', '%25').replace('\r', '%0D').replace('\n', '%0A')


def main(argv, env=os.environ, out=sys.stdout, proc='/proc', root='/sys/fs/cgroup', now=time.time):
    verb = argv[1] if len(argv) == 2 else ''
    temp = env.get('RUNNER_TEMP') or ''
    if verb not in ('start', 'end') or not temp:
        print('::warning::fork-ci-pressure: usage: fork-ci-pressure.py start|end, with RUNNER_TEMP set', file=out)
        return 0
    path = os.path.join(temp, SAMPLE)
    try:
        now_sample = sample(proc, root, now)
        if verb == 'start':
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(now_sample, f)
            print(f'fork-ci-pressure: start sample saved ({len(now_sample["cgroups"])} cgroup(s))', file=out)
            return 0
        try:
            start = json.loads(read(path) or 'null')
        except ValueError:
            start = None
        n = numbers(start if isinstance(start, dict) else {}, now_sample)
        status = (env.get('JOB_STATUS') or 'unknown').strip()
        print(line(status, n), file=out)
        if status != 'success':
            note = json.dumps({'status': status, **n}, separators=(',', ':'), sort_keys=True)
            print(f'::notice title={NOTE_TITLE}::{command(NOTE_HEAD + note)}', file=out)
    except Exception as e:   # never fail the job
        print(f'::warning::fork-ci-pressure: {verb}: {e.__class__.__name__}: {e}', file=out)
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
