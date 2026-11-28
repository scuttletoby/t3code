"""ci-lane: the own CI lane in shadow (fork-kit/ci-lane/README.md).

  ci-lane tick [--config FILE] [--state-dir DIR]     one pass (the timer, every minute)
  ci-lane status [--json] [--state-dir DIR]          the open heads and their jobs (the work panel's rows)
  ci-lane parity [--json] [--config FILE] [--state-dir DIR]
                                                     the lane against Actions, and the phase 3 gate

Exit codes: 0 fine, 1 a pass with errors (each logged), 2 a config, state or lock problem.
"""
import argparse, datetime, fcntl, json, os, re, sys

from . import lane, parity
from .github import GhAs

REPO = re.compile(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+')
CHECK_NAME = re.compile(r'[A-Za-z0-9 _().-]{1,60}')
MODES = ('shadow', 'off')
CONFIG_KEYS = {'version', 'repo', 'mode', 'checkName', 'appId', 'auth', 'launch', 'maxJobs', 'leaseMinutes',
               'explained'}


class ConfigError(Exception):
    pass


def home():
    return os.path.expanduser('~')


def default_config():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.join(home(), '.config')
    return os.path.join(base, 'ci-lane', 'config.json')


def default_state():
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(home(), '.local', 'state')
    return os.path.join(base, 'ci-lane')


def read_config(path):
    try:
        with open(path, encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError) as e:
        raise ConfigError(f'{path}: {e}') from None
    if not isinstance(cfg, dict) or not set(cfg) <= CONFIG_KEYS or cfg.get('version') != 1:
        raise ConfigError(f'{path}: version 1 with only {", ".join(sorted(CONFIG_KEYS))}')
    if not isinstance(cfg.get('repo'), str) or not REPO.fullmatch(cfg['repo']):
        raise ConfigError(f'{path}: repo must be OWNER/NAME')
    cfg.setdefault('mode', 'shadow')
    if cfg['mode'] not in MODES:
        raise ConfigError(f'{path}: mode must be one of {", ".join(MODES)} (phase 3 adds the required check)')
    cfg.setdefault('checkName', 'fork-ci-own')
    if not isinstance(cfg['checkName'], str) or not CHECK_NAME.fullmatch(cfg['checkName']):
        raise ConfigError(f'{path}: checkName must be a short plain name')
    if type(cfg.get('appId')) is not int or cfg['appId'] <= 0:
        raise ConfigError(f'{path}: appId (the CI App\'s id, whose check runs are this lane\'s) is required')
    gh_as = (cfg.get('auth') or {}).get('ghAs') if isinstance(cfg.get('auth'), dict) else None
    if not isinstance(gh_as, str) or not os.path.isabs(gh_as):
        raise ConfigError(f'{path}: auth.ghAs must be the absolute path of gh-as')
    launch = cfg.get('launch')
    if not isinstance(launch, list) or not launch or not all(isinstance(a, str) and a for a in launch) \
            or not os.path.isabs(launch[0]):
        raise ConfigError(f'{path}: launch must be an argv starting with an absolute path')
    cfg.setdefault('maxJobs', 2)
    cfg.setdefault('leaseMinutes', 40)
    for key, most in (('maxJobs', 16), ('leaseMinutes', 240)):
        if type(cfg[key]) is not int or not 1 <= cfg[key] <= most:
            raise ConfigError(f'{path}: {key} must be 1 to {most}')
    cfg.setdefault('explained', {})
    if not isinstance(cfg['explained'], dict) or not all(isinstance(v, str) for v in cfg['explained'].values()):
        raise ConfigError(f'{path}: explained maps "<sha>:<part>:<shard>" to why')
    return cfg


def read_state(folder, log):
    """The journal, or an empty one: a missing or broken journal is rebuilt from GitHub's check runs."""
    path = os.path.join(folder, 'state.json')
    try:
        with open(path, encoding='utf-8') as f:
            state = json.load(f)
        if isinstance(state, dict) and state.get('version') == 1 and isinstance(state.get('heads'), dict) \
                and isinstance(state.get('history'), list):
            return state
        log(f'WARN {path}: not a version 1 journal; starting empty (GitHub\'s check runs rebuild it)')
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as e:
        log(f'WARN {path}: {e}; starting empty (GitHub\'s check runs rebuild it)')
    return {'version': 1, 'heads': {}, 'history': []}


def write_state(folder, state):
    path = os.path.join(folder, 'state.json')
    tmp = f'{path}.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=1, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def log_line(text):
    print(text, flush=True)


def rows(state):
    """One row per job of each open head: what the work panel shows (#1136)."""
    out = []
    for sha, head in sorted(state.get('heads', {}).items()):
        for key, job in sorted(head['jobs'].items()):
            out.append({'sha': sha, 'what': head['what'], 'part': key, 'attempt': job['attempt'],
                        'state': job['state'], 'started': job.get('started'), 'seconds': job.get('seconds'),
                        'why': job.get('why')})
    return out


def main(argv, gh=None, launcher=None, now=None, log=log_line):
    p = argparse.ArgumentParser(prog='ci-lane')
    p.add_argument('command', choices=('tick', 'status', 'parity'))
    p.add_argument('--config', default=None)
    p.add_argument('--state-dir', default=None)
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    folder = a.state_dir or default_state()
    now = now or datetime.datetime.now(datetime.timezone.utc)
    if a.command == 'status':
        state = read_state(folder, log)
        got = rows(state)
        print(json.dumps(got, indent=1) if a.json else
              '\n'.join(f'{r["sha"][:12]} {r["what"]} {r["part"]} a{r["attempt"]} {r["state"]}' for r in got)
              or 'no open heads')
        return 0
    try:
        cfg = read_config(a.config or default_config())
    except ConfigError as e:
        log(f'ERROR config: {e}')
        return 2
    if a.command == 'parity':
        r = parity.report(read_state(folder, log), cfg['explained'])
        print(json.dumps(r, indent=1) if a.json else parity.text(r))
        return 0
    if cfg['mode'] == 'off':
        log('mode off: nothing runs on the lane; Actions counts for everything')
        return 0
    os.makedirs(folder, mode=0o700, exist_ok=True)
    lock = open(os.path.join(folder, 'lock'), 'a')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        lock.close()
        log('another tick is running; this one does nothing')
        return 0
    with lock:
        state = read_state(folder, log)
        gh = gh or GhAs(cfg['auth']['ghAs'], home(), cfg['repo'])
        launcher = launcher or lane.Launcher(cfg['launch'])
        errors = lane.Lane(cfg, gh, launcher, state, now, log).tick()
        write_state(folder, state)     # what the tick did is kept even when the parity reads fail
        errors += parity.fetch(state, gh, now, log)
        write_state(folder, state)
    return 1 if errors else 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
