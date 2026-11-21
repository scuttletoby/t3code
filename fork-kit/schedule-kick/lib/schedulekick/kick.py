"""schedule-kick: start listed GitHub workflows at their cron times by workflow_dispatch, because GitHub starts
`schedule` runs hours late (fork-kit/schedule-kick/README.md). A systemd timer runs `schedule-kick tick` every few
minutes; each tick, for each entry of the config:

1. slot = the entry's last cron slot at or before now (UTC);
2. that slot already handled (the state file says so): nothing, no API call;
3. after an error, wait (5 minutes, doubling, at most an hour) before the next call;
4. one runs-list call: a run of the workflow created at or after the slot (any event: schedule, by hand, ours)
   means the slot is done; otherwise one workflow_dispatch;
5. record the slot (state file, written whole and renamed into place).

The run list, not the state file, is what stops a second run: a dispatch sent but not recorded (stopped half-way),
a lost or broken state file, or a clock that jumped back all end at step 4, which finds the run. Missed slots
while the host was off end in one run for the latest slot only. A rate limit (or under MIN_REMAINING calls left)
stops the tick and pauses all entries until GitHub's reset (at most an hour).
"""
import argparse, datetime, fcntl, json, os, re, stat, sys

from . import cron as cronmod
from .github import ApiError, GhAsApi, TokenApi, iso

UTC = datetime.timezone.utc
VERSION = 1
MAX_ENTRIES = 50
MAX_CONFIG_BYTES = 256 * 1024
MAX_INPUTS = 10
BACKOFF = datetime.timedelta(minutes=5)
MAX_WAIT = datetime.timedelta(hours=1)
MIN_REMAINING = 100

REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\Z')
WORKFLOW_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml\Z')
REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z')
INPUT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,99}\Z')
ENTRY_KEYS = {'repo', 'workflow', 'cron', 'ref', 'inputs'}

USAGE = """usage: schedule-kick tick [--config FILE] [--state-dir DIR]
       schedule-kick check [--config FILE]
       schedule-kick status [--state-dir DIR]

tick    start each listed workflow whose last cron slot has no run yet (one workflow_dispatch at most per entry)
check   read the config and print each entry's last slot; no network
status  print the last tick's status file; exit 1 unless it was fine

FILE defaults to $XDG_CONFIG_HOME/schedule-kick/config.json (~/.config/...), DIR to
$XDG_STATE_HOME/schedule-kick (~/.local/state/...). See fork-kit/schedule-kick/README.md.
Exit: 0 fine, 1 an entry failed (the next tick tries again), 2 the config was refused (nothing sent)."""


class ConfigError(Exception):
    pass


class Entry:
    def __init__(self, repo, workflow, cron, ref, inputs):
        self.repo, self.workflow, self.cron, self.ref, self.inputs = repo, workflow, cron, ref, inputs
        self.key = f'{repo.lower()} {workflow}'


class Config:
    def __init__(self, path, auth, entries):
        self.path, self.auth, self.entries = path, auth, entries


def _now():
    return datetime.datetime.now(UTC)


def parse_time(text):
    """An ISO time GitHub or this tool wrote, as an aware UTC time; None if it is not one."""
    if not isinstance(text, str):
        return None
    try:
        when = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return when.astimezone(UTC) if when.tzinfo else None


# -- config ---------------------------------------------------------------------------------------------------------

def read_token(path):
    """The token in `path`: a plain file of this user, readable by nobody else, one word. Refused otherwise."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
    except OSError as e:
        raise ConfigError(f'tokenFile {path}: cannot open it without following a link ({e.strerror})') from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise ConfigError(f'tokenFile {path}: not a plain file')
        if st.st_uid != os.getuid():
            raise ConfigError(f'tokenFile {path}: owned by uid {st.st_uid}, not by the user this runs as')
        if st.st_mode & 0o077:
            raise ConfigError(f'tokenFile {path}: group or others can use it (mode {st.st_mode & 0o777:o}); '
                              'chmod 600 it')
        data = os.read(fd, 4097)
    finally:
        os.close(fd)
    try:
        token = data.decode('ascii').strip()
    except UnicodeDecodeError:
        raise ConfigError(f'tokenFile {path}: not one plain token') from None
    if len(data) > 4096 or not token or not re.fullmatch(r'[\x21-\x7e]+', token):
        raise ConfigError(f'tokenFile {path}: not one plain token')
    return token


def _auth(raw):
    if not isinstance(raw, dict) or len(raw) != 1 or set(raw) - {'tokenFile', 'ghAs'}:
        raise ConfigError('auth must be exactly one of {"tokenFile": "/abs/path"} or {"ghAs": "/abs/path/gh-as"} '
                          '(a person\'s own gh login is never used)')
    (kind, path), = raw.items()
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ConfigError(f'auth.{kind} must be an absolute path')
    if kind == 'tokenFile':
        read_token(path)                                        # checked now; read again for each tick
        return ('tokenFile', path)
    if os.path.basename(path) != 'gh-as':
        raise ConfigError(f'auth.ghAs {path}: must be the gh-apps kit\'s gh-as, never gh itself')
    if not (os.path.isfile(path) and os.access(path, os.X_OK)):
        raise ConfigError(f'auth.ghAs {path}: no such program')
    return ('ghAs', path)


def _entry(raw, n):
    where = f'entries[{n}]'
    if not isinstance(raw, dict):
        raise ConfigError(f'{where} must be an object')
    unknown = set(raw) - ENTRY_KEYS
    if unknown:
        raise ConfigError(f'{where}: unknown key {sorted(unknown)[0]!r} (keys: {", ".join(sorted(ENTRY_KEYS))})')
    for key in ('repo', 'workflow', 'cron', 'ref'):
        if not isinstance(raw.get(key), str):
            raise ConfigError(f'{where}.{key} is missing or not a string')
    if not REPO_RE.match(raw['repo']):
        raise ConfigError(f'{where}.repo {raw["repo"]!r} is not OWNER/NAME')
    if not WORKFLOW_RE.match(raw['workflow']):
        raise ConfigError(f'{where}.workflow {raw["workflow"]!r} is not a workflow file name (name.yml)')
    if not REF_RE.match(raw['ref']) or '..' in raw['ref'] or raw['ref'].endswith('/'):
        raise ConfigError(f'{where}.ref {raw["ref"]!r} is not a branch or tag name')
    try:
        line = cronmod.Cron(raw['cron'])
    except cronmod.CronError as e:
        raise ConfigError(f'{where}.cron: {e}') from None
    if line.last_slot(_now()) is None:
        raise ConfigError(f'{where}.cron {raw["cron"]!r} never comes')
    inputs = raw.get('inputs', {})
    if not isinstance(inputs, dict) or len(inputs) > MAX_INPUTS:
        raise ConfigError(f'{where}.inputs must be an object of at most {MAX_INPUTS} inputs')
    for key, value in inputs.items():
        if not INPUT_RE.match(key) or not isinstance(value, str) or len(value) > 1000 or '\n' in value:
            raise ConfigError(f'{where}.inputs.{key}: names are letters, digits, _ and -; values are one-line '
                              'strings (write true, not a JSON true)')
    return Entry(raw['repo'], raw['workflow'], line, raw['ref'], dict(inputs))


def load_config(path):
    """The config, every part checked; ConfigError (nothing is sent) for anything it does not know."""
    try:
        with open(path, 'rb') as f:
            data = f.read(MAX_CONFIG_BYTES + 1)
    except OSError as e:
        raise ConfigError(f'{path}: cannot read it ({e.strerror})') from None
    if len(data) > MAX_CONFIG_BYTES:
        raise ConfigError(f'{path}: larger than {MAX_CONFIG_BYTES} bytes')
    try:
        raw = json.loads(data)
    except ValueError as e:
        raise ConfigError(f'{path}: not JSON ({e})') from None
    if not isinstance(raw, dict):
        raise ConfigError(f'{path}: must be a JSON object')
    unknown = set(raw) - {'version', 'auth', 'entries', '//'}
    if unknown:
        raise ConfigError(f'{path}: unknown key {sorted(unknown)[0]!r}')
    if raw.get('version') != VERSION:
        raise ConfigError(f'{path}: version must be {VERSION}')
    try:
        auth = _auth(raw.get('auth'))
        entries = raw.get('entries')
        if not isinstance(entries, list) or not entries or len(entries) > MAX_ENTRIES:
            raise ConfigError(f'entries must be a list of 1 to {MAX_ENTRIES} entries')
        out, seen = [], set()
        for n, item in enumerate(entries):
            entry = _entry(item, n)
            if entry.key in seen:
                raise ConfigError(f'entries[{n}]: {entry.repo} {entry.workflow} is listed twice (one entry per '
                                  'workflow: the run list cannot tell two entries apart)')
            seen.add(entry.key)
            out.append(entry)
    except ConfigError as e:
        raise ConfigError(f'{path}: {e}') from None
    return Config(path, auth, out)


def make_api(cfg):
    kind, path = cfg.auth
    if kind == 'tokenFile':
        return TokenApi(read_token(path))
    return GhAsApi(path, os.path.expanduser('~'))


# -- state ----------------------------------------------------------------------------------------------------------

def state_folder(path):
    """The state folder, made if missing (0700); refused if it is a link or another user's."""
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
        st = os.lstat(path)
    except OSError as e:
        raise ConfigError(f'state folder {path}: {e.strerror}') from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o022:
        raise ConfigError(f'state folder {path}: not a folder of this user that only it can write')
    return path


def write_json(path, data):
    """Written whole or not at all: a temporary file in the same folder, flushed, then renamed over `path`."""
    folder = os.path.dirname(path)
    tmp = os.path.join(folder, f'.{os.path.basename(path)}.tmp-{os.getpid()}')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dfd = os.open(folder, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def load_state(path, warn):
    """The state file; a missing one is empty, and a broken or unknown one is reported and read as empty (it only
    saves API calls: the run list still stops a second run)."""
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {'version': VERSION, 'entries': {}}
    except (OSError, ValueError) as e:
        warn(f'state file {path} cannot be read ({e}); starting from empty state (the run list guards)')
        return {'version': VERSION, 'entries': {}}
    if not isinstance(raw, dict) or raw.get('version') != VERSION or not isinstance(raw.get('entries'), dict):
        warn(f'state file {path} is not version {VERSION} state; starting from empty state (the run list guards)')
        return {'version': VERSION, 'entries': {}}
    raw['entries'] = {k: v for k, v in raw['entries'].items() if isinstance(v, dict)}
    return raw


def waiting_until(text, now):
    """The time in `text` while it is still ahead, but at most MAX_WAIT ahead (later means the clock jumped back:
    the wait is dropped, not kept for days); else None."""
    until = parse_time(text)
    if until is not None and now < until <= now + MAX_WAIT:
        return until
    return None


# -- the tick -------------------------------------------------------------------------------------------------------

def kick_one(entry, api, state, now):
    """(result, message) for one entry; state['entries'][key] updated in place."""
    slot = entry.cron.last_slot(now)
    old = state['entries'].get(entry.key, {})
    if old.get('slot') == iso(slot) and old.get('cron') == entry.cron.line:
        return 'done', f'slot {iso(slot)} already {old.get("result", "handled")}'
    wait = waiting_until(old.get('retryAfter'), now)
    if wait is not None:
        return 'waiting', f'after {old.get("failures", 1)} error(s), next try after {iso(wait)}'
    try:
        created = api.runs_since(entry.repo, entry.workflow, slot)
        times = [parse_time(c) for c in created]
        if None in times:
            raise ApiError(f'runs of {entry.repo} {entry.workflow}: a run without a readable created time')
        if any(t >= slot for t in times):
            result = 'ran'
        else:
            api.dispatch(entry.repo, entry.workflow, entry.ref, entry.inputs)
            result = 'dispatched'
    except ApiError as e:
        failures = int(old.get('failures', 0)) + 1 if isinstance(old.get('failures', 0), int) else 1
        delay = min(BACKOFF * 2 ** min(failures - 1, 10), MAX_WAIT)
        state['entries'][entry.key] = dict(old, failures=failures, retryAfter=iso(now + delay), error=str(e))
        if e.rate_limited:
            reset = datetime.datetime.fromtimestamp(e.reset, UTC) if e.reset else None
            pause(state, now, reset)
        return 'error', str(e)
    state['entries'][entry.key] = {'cron': entry.cron.line, 'slot': iso(slot), 'result': result, 'at': iso(now)}
    return result, f'slot {iso(slot)}: ' + ('a run exists' if result == 'ran' else
                                             f'workflow_dispatch sent ({entry.ref})')


def pause(state, now, until=None):
    if until is None or not now < until <= now + MAX_WAIT:
        until = now + datetime.timedelta(minutes=15)
    state['pauseUntil'] = iso(until)


def tick(cfg, state_dir, api, now=None, out=sys.stdout, err=sys.stderr):
    """One tick over every entry. Returns the exit code."""
    now = now or _now()
    folder = state_folder(state_dir)
    lock = open(os.path.join(folder, 'lock'), 'a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('schedule-kick: another tick is running; this one does nothing', file=out)
            return 0
        return _tick(cfg, folder, api, now, out, err)
    finally:
        lock.close()


def _tick(cfg, folder, api, now, out, err):
    state_path = os.path.join(folder, 'state.json')
    state = load_state(state_path, lambda m: print(f'schedule-kick: {m}', file=err))
    keys = {e.key for e in cfg.entries}
    state['entries'] = {k: v for k, v in state['entries'].items() if k in keys}
    status, failed = {}, False
    paused = waiting_until(state.get('pauseUntil'), now)
    for entry in cfg.entries:
        if paused is not None:
            status[entry.key] = {'result': 'paused', 'message': f'GitHub API budget: paused until {iso(paused)}'}
            continue
        before = json.dumps(state, sort_keys=True)
        result, message = kick_one(entry, api, state, now)
        remaining = getattr(api, 'remaining', None)
        if result != 'done' and remaining is not None and remaining < MIN_REMAINING:
            pause(state, now)
            message += f' (only {remaining} API calls left: pausing)'
        status[entry.key] = {'result': result, 'message': message}
        line = f'schedule-kick: {entry.repo} {entry.workflow}: {result}: {message}'
        print(line, file=err if result == 'error' else out)
        failed |= result == 'error'
        if json.dumps(state, sort_keys=True) != before:
            write_json(state_path, state)       # after each entry: a stop half-way keeps what is done
        paused = waiting_until(state.get('pauseUntil'), now)
    if paused is not None:
        print(f'schedule-kick: GitHub API budget: paused until {iso(paused)}', file=err)
    else:
        state.pop('pauseUntil', None)           # over, or a time the clock jumped back from
    write_json(state_path, state)
    write_json(os.path.join(folder, 'status.json'),
               {'version': VERSION, 'at': iso(now), 'ok': not failed, 'config': cfg.path, 'entries': status})
    return 1 if failed else 0


# -- command line ---------------------------------------------------------------------------------------------------

def default_config():
    base = os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config')
    return os.path.join(base, 'schedule-kick', 'config.json')


def default_state_dir():
    base = os.environ.get('XDG_STATE_HOME') or os.path.expanduser('~/.local/state')
    return os.path.join(base, 'schedule-kick')


def refuse(message, state_dir, out_err):
    """Config refused: say so on stderr (the journal) and in the status file; nothing is sent."""
    print(f'schedule-kick: refused, nothing sent: {message}', file=out_err)
    if state_dir:
        try:
            folder = state_folder(state_dir)
            write_json(os.path.join(folder, 'status.json'),
                       {'version': VERSION, 'at': iso(_now()), 'ok': False, 'error': message, 'entries': {}})
        except (ConfigError, OSError) as e:
            print(f'schedule-kick: cannot write the status file either: {e}', file=out_err)
    return 2


def main(argv, api_factory=make_api, out=sys.stdout, err=sys.stderr):
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(USAGE, file=out)
        return 0
    parser = argparse.ArgumentParser(prog='schedule-kick', add_help=False)
    parser.add_argument('command', choices=('tick', 'check', 'status'))
    parser.add_argument('--config')
    parser.add_argument('--state-dir')
    try:
        a = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=err)
        return 2
    state_dir = a.state_dir or default_state_dir()
    if a.command == 'status':
        try:
            with open(os.path.join(state_dir, 'status.json'), encoding='utf-8') as f:
                status = json.load(f)
        except (OSError, ValueError) as e:
            print(f'schedule-kick: no readable status in {state_dir} ({e})', file=err)
            return 1
        print(json.dumps(status, indent=1, sort_keys=True), file=out)
        return 0 if status.get('ok') is True else 1
    try:
        cfg = load_config(a.config or default_config())
    except ConfigError as e:
        return refuse(str(e), state_dir if a.command == 'tick' else None, err)
    if a.command == 'check':
        now = _now()
        for e in cfg.entries:
            print(f'{e.repo} {e.workflow} ({e.cron.line}, ref {e.ref}): last slot {iso(e.cron.last_slot(now))}',
                  file=out)
        print(f'config fine: {len(cfg.entries)} entries, auth {cfg.auth[0]}', file=out)
        return 0
    try:
        api = api_factory(cfg)
        return tick(cfg, state_dir, api, out=out, err=err)
    except ConfigError as e:
        return refuse(str(e), None, err)
