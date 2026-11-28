#!/usr/bin/env python3
"""pool: run several test lanes at once when they fit, each holding its test servers through claims.py.

General: the project's config names its servers and ports; nothing here knows a project.

  {
    "servers": {"api": {"port": 4100}, "db": {"port": 4101}},
    "lane_mb": 800,                               a lane's memory before one is measured
    "reserve_mb": 1024,                           memory left free for everything else
    "max_lanes": 4                                never more lanes than this at once
  }

The host's part comes from the runner's environment, never from the project's config (a pull request edits that):
FORK_CI_LANES_DIR, the one folder of claims and measurements every lane on the host shares (absolute; default
$XDG_STATE_HOME/fork-ci-lanes, else ~/.local/state/fork-ci-lanes), and FORK_CI_LANES_MAX_LANES,
FORK_CI_LANES_RESERVE_MB and FORK_CI_LANES_LANE_MB, the host's limits (defaults 4, 1024, 800). A project's config can
only tighten them: the lower max_lanes, the higher reserve_mb and lane_mb.

Admission (admit): a new lane starts only when fewer than max_lanes run and the memory the machine has free now
(MemAvailable, measured) minus reserve_mb, minus what the admitted lanes have not used yet (each lane's cost less its
process group's memory now: a lane started a moment ago is not in MemAvailable yet), holds one more lane's cost. A
lane's cost is the largest of lane_mb and the peaks of its own last runs (each run records its peak memory, by its
config file and lane id, so one project's light lanes never lower another's cost: its largest process, or its process
group's sampled total when larger). When the free memory or the measurements cannot be read, a lane starts only when
none runs (one at a time: the safe old way).

  python3 pool.py run --config lanes.json --lane ID --server NAME [--server NAME]... [--alone [--restart]]
      [--wait-s 3600] -- command...
Waits (polling) until every named server is claimed (shared, or alone with --alone; --restart: a waiting restart goes
first, on every named server) and the pool admits the lane, then runs the command with LANE_ID and LANE_PORT_<NAME>
set, releases on exit (also on SIGTERM/SIGINT/SIGHUP: the command is stopped first) and records the lane's peak
memory. The lane runs in its own process group (pool.py makes one when it does not lead one and no terminal is its
input); its claims stay alive while a process of that group runs. A pool.py killed outright (SIGKILL) takes its
command with it (the kernel kills it), and holds its servers until the processes that command started end. Exit: the
command's; 128 + the signal when stopped; 75 when it waited --wait-s without a start (also behind a newer release's
claim on a server it needs); 2 for a config it cannot use.
"""
import argparse, json, os, re, resource, signal, subprocess, sys, time

sys.dont_write_bytecode = True   # leave no __pycache__ beside the release's files
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import claims as claims_mod   # noqa: E402  (the same release)

NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')
KEEP = 20   # measurements kept per lane; the cost is the largest of the last ones
MAX_KEYS = 200   # lanes measured: the longest unmeasured are dropped (lane ids that change every run)
MEASURED = 'measured.json'


class ConfigError(Exception):
    pass


HOST_LIMITS = (('max_lanes', 'FORK_CI_LANES_MAX_LANES', 4, min), ('reserve_mb', 'FORK_CI_LANES_RESERVE_MB', 1024, max),
               ('lane_mb', 'FORK_CI_LANES_LANE_MB', 800, max))


def host_dir(env):
    """The host's one claims folder (from the runner's environment, absolute)."""
    got = env.get('FORK_CI_LANES_DIR')
    if got is not None:
        if not os.path.isabs(got):
            raise ConfigError(f'FORK_CI_LANES_DIR must be an absolute folder (not {got!r}): every lane on the host '
                              'shares it, a relative one would be one per working folder')
        return os.path.normpath(got)
    state = env.get('XDG_STATE_HOME')
    if not state or not os.path.isabs(state):
        home = env.get('HOME') or os.path.expanduser('~')
        if not os.path.isabs(home):
            raise ConfigError('set FORK_CI_LANES_DIR: no absolute home folder to keep the claims in')
        state = os.path.join(home, '.local', 'state')
    return os.path.join(state, 'fork-ci-lanes')


def load_config(path, env=None):
    """The project's lane config, checked, with the host's folder and limits (from env, the runner's environment:
    the project can only tighten the limits)."""
    env = os.environ if env is None else env
    try:
        with open(path, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f'cannot read the lane config {path} ({type(e).__name__}: {e})')
    keys = {'servers', 'lane_mb', 'reserve_mb', 'max_lanes'}
    if isinstance(c, dict) and 'claims_dir' in c:
        raise ConfigError(f'{path}: "claims_dir" is the host\'s, not the project\'s: '
                          'set FORK_CI_LANES_DIR on the runner')
    if not isinstance(c, dict) or set(c) - keys or 'servers' not in c:
        raise ConfigError(f'{path}: needs "servers", and only the keys {sorted(keys)}')
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
    for key, var, default, pick in HOST_LIMITS:
        least = 1 if key != 'reserve_mb' else 0
        try:
            host = int(env.get(var, default))
        except ValueError:
            host = None
        if host is None or host < least:
            raise ConfigError(f'{var} must be a whole number of at least {least}')
        v = c.get(key, host)
        if not isinstance(v, int) or isinstance(v, bool) or v < least:
            raise ConfigError(f'{path}: "{key}" must be a whole number')
        c[key] = pick(v, host)
    c['claims_dir'] = host_dir(env)
    c['config_path'] = os.path.realpath(path)   # (the lane's measurements are kept by it)
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


def _peaks(got):
    return [m for m in got if isinstance(m, int) and not isinstance(m, bool) and m > 0] if isinstance(got, list) else []


def cost_mb(peaks, default):
    """A lane's cost: the largest of default (lane_mb: the floor, also once it is measured) and its last measured
    peaks; None when the measurements cannot be read (never the default on a guess: it may be far below what the
    lane takes)."""
    if peaks is None:
        return None
    return max([default] + _peaks(peaks[-KEEP:] if isinstance(peaks, list) else []))


def measure_key(cfg, lane):
    """Whose peaks: the config file (one per project checkout) and the lane id."""
    return cfg.get('config_path', '') + '\0' + lane


def admit(lanes, free_mb, cost, reserve_mb, max_lanes, pending_mb=0):
    """(ok, why) for one more lane beside `lanes` running ones, which have not used pending_mb of their cost yet."""
    if lanes >= max_lanes:
        return False, f'{lanes} lanes run (max_lanes {max_lanes})'
    if free_mb is None or cost is None:
        unknown = 'free memory unknown' if free_mb is None else 'the measurements cannot be read'
        return (True, f'{unknown}: the only lane') if lanes == 0 else \
            (False, f'{unknown}: one lane at a time ({lanes} running)')
    if free_mb - reserve_mb - pending_mb < cost:
        return False, (f'{free_mb} MB free, {reserve_mb} MB kept free, {pending_mb} MB not used yet by running '
                       f'lanes, a lane takes {cost} MB')
    return True, f'{free_mb} MB free holds a lane of {cost} MB'


PAGE_MB = os.sysconf('SC_PAGE_SIZE') / (1 << 20)


def group_rss_mb(pgid, proc='/proc'):
    """The memory (resident) a process group uses now, in MB; 0 when it cannot be read."""
    total = 0
    try:
        names = os.listdir(proc)
    except OSError:
        return 0
    for n in names:
        if not n.isdigit():
            continue
        try:
            with open(os.path.join(proc, n, 'stat'), 'rb') as f:
                stat = f.read().decode('ascii', 'replace')
            fields = stat[stat.rindex(')') + 2:].split()
            if int(fields[2]) == pgid:
                total += int(fields[21])
        except (OSError, ValueError, IndexError):
            continue
    return int(total * PAGE_MB)


def pending_mb(lanes, rss=group_rss_mb):
    """What admitted lanes have not used of their cost yet: each lane's cost less its group's memory now (a lane
    with no group to measure counts its whole cost)."""
    total = 0
    for c in lanes:
        cost = (c.get('note') or {}).get('cost')
        if not isinstance(cost, int) or isinstance(cost, bool) or cost <= 0:
            continue
        group = (c.get('note') or {}).get('group')
        used = rss(group) if isinstance(group, int) and group > 1 else 0
        total += max(0, cost - used)
    return total


def read_measured(folder):
    """The measured peaks, {measure_key: [peak MB, ...]}: {} before any (and for an earlier release's one host-wide
    list: no lane's own peaks); None when the file cannot be read (one lane at a time until a lane measures again)."""
    try:
        with open(os.path.join(folder, MEASURED), encoding='utf-8') as f:
            got = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else {} if isinstance(got, list) else None


def record(folder, key, peak_mb):
    """Add one lane's measured peak (under the claims lock; the file is written whole; one it cannot read is
    started afresh). Keeps the last KEEP of each lane, and the MAX_KEYS lanes measured last."""
    c = claims_mod.Claims(folder)
    fd = c._locked()
    try:
        got = read_measured(folder) or {}
        got[key] = (_peaks(got.pop(key, None)) + [int(peak_mb)])[-KEEP:]   # (popped: it moves to the end)
        for old in list(got)[:-MAX_KEYS]:
            del got[old]
        tmp = os.path.join(folder, f'{claims_mod.TMP}measured-{os.getpid()}')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(got, f)
        os.replace(tmp, os.path.join(folder, MEASURED))
    finally:
        os.close(fd)


def env_name(server):
    return 'LANE_PORT_' + re.sub(r'[^A-Za-z0-9]', '_', server).upper()


def take(c, cfg, servers, mode, holder, lane, restart, free=available_mb, rss=group_rss_mb, since=None, group=None):
    """Claim every server or none: (tokens or None, why). A refusal gives back what it got (no lane holds one server
    while waiting for another: no deadlock); a refused restart waits first in line on every one of its servers, all
    with one `since` (the time the lane began waiting), so the oldest waiting lane is first on all it needs. Each
    claim notes the lane's cost and its process group (group: measured for what the lane has not used yet)."""
    measured = read_measured(cfg['claims_dir'])
    cost = None if measured is None else cost_mb(measured.get(measure_key(cfg, lane), []), cfg['lane_mb'])
    note = {'cost': cost if cost is not None else cfg['lane_mb'], **({'group': group} if group else {})}
    servers = sorted(servers)
    got = []

    def gate(lanes):
        return admit(len(lanes), free(), cost, cfg['reserve_mb'], cfg['max_lanes'], pending_mb(lanes, rss))
    for i, s in enumerate(servers):
        token, why = c.claim(s, mode, holder, lane, restart=restart, admit=gate if i == 0 else None,
                             keep_wait=restart, since=since, note=note)
        if token is None:
            for t in got:
                c.release(t)
            if restart:
                for other in servers:
                    c.queue(other, holder, lane, since=since)
            return None, why
        got.append(token)
    if restart:
        for s in servers:
            c.withdraw(s, holder, lane)
    return got, 'claimed ' + ', '.join(f'{s} {mode}' for s in servers)


class Stopper:
    """SIGTERM/SIGINT/SIGHUP while a lane runs: stop its command, also when the signal came before the command
    started (it is stopped the moment it starts: never a command run on with no claim or no one to stop it)."""
    SIGNALS = (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)

    def __init__(self, clock=time.monotonic):
        self.signum, self.at, self.proc, self.clock = None, None, None, clock

    def on_signal(self, signum, frame):
        if self.signum is None:
            self.signum, self.at = signum, self.clock()
        self._stop()

    def started(self, proc):
        self.proc = proc
        if self.signum is not None:
            self._stop()

    def _stop(self):
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()

    def overdue(self, grace_s=10):
        return self.signum is not None and self.clock() - self.at >= grace_s

    def code(self, rc):
        return 128 + self.signum if self.signum is not None else rc

    def install(self):
        return {s: signal.signal(s, self.on_signal) for s in self.SIGNALS}


try:
    import ctypes
    _prctl = ctypes.CDLL(None, use_errno=True).prctl
except (ImportError, OSError, AttributeError):
    _prctl = None
PR_SET_PDEATHSIG = 1


def die_with(parent):
    """For the command, before it starts: the kernel kills it (SIGKILL) when pool.py dies, so a pool.py killed outright
    (an outer `timeout -k` shorter than pool.py's own 10 s kill, while a command that ignores SIGTERM hangs) does not
    leave it holding its servers for ever. A pool.py that died before this was set: the command does not start."""
    def preexec():
        if _prctl is not None:
            _prctl(PR_SET_PDEATHSIG, int(signal.SIGKILL), 0, 0, 0)
        if os.getppid() != parent:
            os._exit(128 + signal.SIGKILL)
    return preexec


def own_group():
    """Lead a process group (the lane's command runs in it, so the claims outlive a pool.py killed outright).
    Not when a terminal is the input: the terminal's Ctrl-C must still reach the lane."""
    if os.getpgrp() != os.getpid() and not os.isatty(0):
        try:
            os.setpgid(0, 0)
        except OSError:
            pass   # (a session leader cannot; its group is the holder's then)
    return os.getpgrp() if os.getpgrp() == os.getpid() else None


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
    stopper = Stopper()
    old = stopper.install()
    try:
        return lane(a, cmd, cfg, stopper, sleep, clock)
    finally:
        for s, h in old.items():
            signal.signal(s, h)


def lane(a, cmd, cfg, stopper, sleep, clock):
    group = own_group()
    mode = 'alone' if a.alone else 'shared'
    c = claims_mod.Claims(cfg['claims_dir'])
    start, pause, said, since, holder = clock(), 0.2, None, time.time(), None
    try:
        holder = claims_mod.me()
        while True:
            tokens, why = take(c, cfg, a.server, mode, holder, a.lane, a.restart, since=since, group=group)
            if tokens:
                break
            if stopper.signum is not None:
                return stopper.code(0)
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
        if a.restart and holder is not None:   # (a grant already ended its waits; this ends those a refusal left)
            try:
                for s in a.server:
                    c.withdraw(s, holder, a.lane)
            except claims_mod.ClaimsError:
                pass
    env = {**os.environ, 'LANE_ID': a.lane, **{env_name(s): str(cfg['servers'][s]['port']) for s in a.server}}
    sampled = 0
    try:
        if stopper.signum is not None:
            return stopper.code(0)
        print(f'pool: lane {a.lane}: {why}', file=sys.stderr, flush=True)
        child = subprocess.Popen(cmd, env=env, preexec_fn=die_with(os.getpid()))
        stopper.started(child)
        while True:
            try:
                rc = child.wait(timeout=1)
                break
            except subprocess.TimeoutExpired:
                pass
            if group:
                sampled = max(sampled, group_rss_mb(group))
            if stopper.overdue():
                child.kill()
        rc = stopper.code(rc)
    finally:
        for t in tokens:
            c.release(t)
    # Its peak: its largest process (kB on Linux), or the group's sampled total when larger.
    peak = max(resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss // 1024, sampled)
    if peak > 0:
        try:
            record(cfg['claims_dir'], measure_key(cfg, a.lane), peak)
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
