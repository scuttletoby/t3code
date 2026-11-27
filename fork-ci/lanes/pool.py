#!/usr/bin/env python3
"""pool: run several test lanes at once when they fit, each holding its test servers through claims.py.

General: the project's config names its servers and ports; nothing here knows a project.

  {
    "claims_dir": ".lanes",                       folder of claims and measurements (relative: to the config's folder)
    "servers": {"api": {"port": 4100}, "db": {"port": 4101}},
    "lane_mb": 800,                               a lane's memory before one is measured
    "reserve_mb": 1024,                           memory left free for everything else
    "max_lanes": 4                                never more lanes than this at once
  }

Admission (admit): a new lane starts only when fewer than max_lanes run and the memory the machine has free now
(MemAvailable, measured) minus reserve_mb holds one more lane's cost. A lane's cost is the largest peak of the last
lanes measured (each run records its children's peak memory), lane_mb until one is. When the free memory cannot be
measured, a lane starts only when none runs (one at a time: the safe old way).

  python3 pool.py run --config lanes.json --lane ID --server NAME [--server NAME]... [--alone [--restart]]
      [--wait-s 3600] -- command...
Waits (polling) until every named server is claimed (shared, or alone with --alone; --restart: a waiting restart goes
first) and the pool admits the lane, then runs the command with LANE_ID and LANE_PORT_<NAME> set, releases on exit
(also on SIGTERM/SIGINT: the command is stopped first) and records the lane's peak memory. Exit: the command's; 75
when it waited --wait-s without a start; 2 for a config it cannot use.
"""
import argparse, json, os, re, resource, signal, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claims as claims_mod   # noqa: E402  (the same release)

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')
KEEP = 20   # measurements kept; the cost is the largest of the last ones
MEASURED = 'measured.json'


class ConfigError(Exception):
    pass


def load_config(path):
    """The project's lane config, checked; claims_dir made absolute."""
    try:
        with open(path, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f'cannot read the lane config {path} ({type(e).__name__}: {e})')
    keys = {'claims_dir', 'servers', 'lane_mb', 'reserve_mb', 'max_lanes'}
    if not isinstance(c, dict) or set(c) - keys or not {'claims_dir', 'servers'} <= set(c):
        raise ConfigError(f'{path}: needs "claims_dir" and "servers", and only the keys {sorted(keys)}')
    servers = c['servers']
    if not isinstance(servers, dict) or not servers:
        raise ConfigError(f'{path}: "servers" must name at least one server')
    ports = {}
    for name, s in servers.items():
        if not NAME.match(name) or not isinstance(s, dict) or set(s) != {'port'} or \
                not isinstance(s['port'], int) or isinstance(s['port'], bool) or not 0 < s['port'] < 65536:
            raise ConfigError(f'{path}: server {name!r} must be {{"port": 1-65535}} with a plain name')
        if s['port'] in ports:
            raise ConfigError(f'{path}: servers {ports[s["port"]]!r} and {name!r} share port {s["port"]}')
        ports[s['port']] = name
    for key, default in (('lane_mb', 800), ('reserve_mb', 1024), ('max_lanes', 4)):
        v = c.get(key, default)
        if not isinstance(v, int) or isinstance(v, bool) or v < (1 if key != 'reserve_mb' else 0):
            raise ConfigError(f'{path}: "{key}" must be a whole number')
        c[key] = v
    if not isinstance(c['claims_dir'], str) or not c['claims_dir']:
        raise ConfigError(f'{path}: "claims_dir" must be a folder')
    c['claims_dir'] = os.path.join(os.path.dirname(os.path.abspath(path)), c['claims_dir'])
    return c


def available_mb(meminfo='/proc/meminfo'):
    """The memory free for new work now (MemAvailable), in MB, or None when it cannot be read."""
    try:
        with open(meminfo) as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1]) // 1024
    except (OSError, ValueError, IndexError):
        return None
    return None


def cost_mb(measured, default):
    """A lane's cost: the largest of the last measured peaks; default before any."""
    peaks = [m for m in measured[-KEEP:] if isinstance(m, int) and m > 0]
    return max(peaks) if peaks else default


def admit(lanes, free_mb, cost, reserve_mb, max_lanes):
    """(ok, why) for one more lane beside `lanes` running ones."""
    if lanes >= max_lanes:
        return False, f'{lanes} lanes run (max_lanes {max_lanes})'
    if free_mb is None:
        return (True, 'free memory unknown: the only lane') if lanes == 0 else \
            (False, f'free memory unknown: one lane at a time ({lanes} running)')
    if free_mb - reserve_mb < cost:
        return False, f'{free_mb} MB free, {reserve_mb} MB kept free, a lane takes {cost} MB'
    return True, f'{free_mb} MB free holds a lane of {cost} MB'


def read_measured(folder):
    try:
        with open(os.path.join(folder, MEASURED), encoding='utf-8') as f:
            got = json.load(f)
        return got if isinstance(got, list) else []
    except (OSError, ValueError):
        return []   # (no measurement yet, or one it cannot read: the config's lane_mb)


def record(folder, peak_mb):
    """Add one lane's measured peak (under the claims lock; the file is written whole)."""
    c = claims_mod.Claims(folder)
    fd = c._locked()
    try:
        got = (read_measured(folder) + [int(peak_mb)])[-KEEP:]
        tmp = os.path.join(folder, f'.tmp-measured-{os.getpid()}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(got, f)
        os.replace(tmp, os.path.join(folder, MEASURED))
    finally:
        os.close(fd)


def env_name(server):
    return 'LANE_PORT_' + re.sub(r'[^A-Za-z0-9]', '_', server).upper()


def take(c, cfg, servers, mode, holder, lane, restart, free=available_mb):
    """Claim every server or none: (tokens or None, why). A refusal gives back what it got (no lane holds one server
    while waiting for another: no deadlock)."""
    cost = cost_mb(read_measured(cfg['claims_dir']), cfg['lane_mb'])
    got = []
    for i, s in enumerate(sorted(servers)):
        gate = (lambda n: admit(n, free(), cost, cfg['reserve_mb'], cfg['max_lanes'])) if i == 0 else None
        token, why = c.claim(s, mode, holder, lane, restart=restart, admit=gate)
        if token is None:
            for t in got:
                c.release(t)
            return None, why
        got.append(token)
    return got, 'claimed ' + ', '.join(f'{s} {mode}' for s in sorted(servers))


def run(argv=None, sleep=time.sleep, clock=time.monotonic):
    p = argparse.ArgumentParser(prog='pool.py run', description='run one lane through the pool')
    p.add_argument('--config', required=True)
    p.add_argument('--lane', required=True)
    p.add_argument('--server', action='append', required=True)
    p.add_argument('--alone', action='store_true')
    p.add_argument('--restart', action='store_true')
    p.add_argument('--wait-s', type=float, default=3600)
    p.add_argument('command', nargs=argparse.REMAINDER)
    a = p.parse_args(argv)
    cmd = a.command[1:] if a.command[:1] == ['--'] else a.command
    try:
        cfg = load_config(a.config)
        unknown = sorted(set(a.server) - set(cfg['servers']))
        if unknown or not cmd or (a.restart and not a.alone):
            raise ConfigError(f'unknown server {unknown[0]!r}' if unknown else
                              'no command to run' if not cmd else '--restart needs --alone')
    except ConfigError as e:
        print(f'pool: {e}', file=sys.stderr)
        return 2
    c, holder, mode = claims_mod.Claims(cfg['claims_dir']), claims_mod.me(), 'alone' if a.alone else 'shared'
    start, pause, said = clock(), 0.2, None
    try:
        while True:
            tokens, why = take(c, cfg, a.server, mode, holder, a.lane, a.restart)
            if tokens:
                break
            if why != said:
                print(f'pool: lane {a.lane} waits: {why}', file=sys.stderr, flush=True)
                said = why
            if clock() - start >= a.wait_s:
                print(f'pool: lane {a.lane} did not start in {a.wait_s:g} s ({why})', file=sys.stderr)
                return 75
            sleep(pause)
            pause = min(pause * 1.5, 5.0)
    except claims_mod.ClaimsError as e:
        print(f'pool: {e}', file=sys.stderr)
        return 2
    finally:
        if a.restart:   # (a granted claim already ended its wait; this ends one a refusal left)
            try:
                for s in a.server:
                    c.withdraw(s, holder, a.lane)
            except claims_mod.ClaimsError:
                pass
    print(f'pool: lane {a.lane}: {why}', file=sys.stderr, flush=True)
    env = {**os.environ, 'LANE_ID': a.lane, **{env_name(s): str(cfg['servers'][s]['port']) for s in a.server}}
    child = None

    def stop(signum, frame):
        if child is not None and child.poll() is None:
            child.terminate()
        raise SystemExit(128 + signum)
    old = {s: signal.signal(s, stop) for s in (signal.SIGTERM, signal.SIGINT)}
    try:
        child = subprocess.Popen(cmd, env=env)
        rc = child.wait()
    except SystemExit as e:
        if child is not None:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
        rc = e.code
    finally:
        for s, h in old.items():
            signal.signal(s, h)
        for t in tokens:
            c.release(t)
    peak = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024   # its largest process, kB on Linux
    if peak > 0:
        try:
            record(cfg['claims_dir'], peak)
        except (OSError, claims_mod.ClaimsError):
            pass
    return rc


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] != ['run']:
        print(__doc__.split('\n\n')[-1].strip(), file=sys.stderr)
        return 2
    return run(argv[1:])


if __name__ == '__main__':
    sys.exit(main())
