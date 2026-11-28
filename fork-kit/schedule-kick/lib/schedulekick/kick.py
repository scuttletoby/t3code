"""schedule-kick: start listed GitHub workflows at their cron times by workflow_dispatch, because GitHub starts
`schedule` runs hours late (fork-kit/schedule-kick/README.md). A systemd timer runs `schedule-kick tick` every few
minutes; each tick, for each entry of the config:

1. slot = the entry's last cron slot at or before now minus the grace (graceMinutes, 5 by default: GitHub's own
   run, when it comes on time, is then found and nothing is sent);
2. that slot already handled (the state file says so): nothing, no API call;
3. after an error, wait (5 minutes, doubling, at most an hour) before the next call;
4. one runs-list call: a `schedule` or `workflow_dispatch` run of the workflow on the entry's ref, created at or
   after the slot, means the slot is done (a pull_request or push run, or one on another branch, does not);
   otherwise one workflow_dispatch;
5. record the slot (state file, written whole and renamed into place).

The run list, not the state file, is what stops a second run: a dispatch sent but not recorded (stopped half-way),
a lost or broken state file, or a clock that jumped back all end at step 4, which finds the run. Missed slots
while the host was off end in one run for the latest slot only. A rate limit (or under MIN_REMAINING calls left)
stops the tick and pauses all entries until GitHub's reset (at most an hour). GitHub's late `schedule` run for a slot
already started here ends early with `schedule-kick late-check` (late.py) as the workflow's first job.

Two more parts share the config, the GitHub login and the API budget: `heads` (heads.py, in the same tick) sends
one workflow_dispatch for a trusted head that has no run of the workflow, and `keepOn` (keepon.py, `schedule-kick
keep-on`, its own timer) turns the named workflows back on when something switches them off.
"""
import argparse, datetime, fcntl, json, os, re, stat, sys

from . import cron as cronmod
from . import heads as headsmod
from .github import ApiError, GhAsApi, TokenApi, iso

UTC = datetime.timezone.utc
VERSION = 1
MAX_ENTRIES = 50
MAX_CONFIG_BYTES = 256 * 1024
MAX_INPUTS = 10
BACKOFF = datetime.timedelta(minutes=5)
MAX_WAIT = datetime.timedelta(hours=1)
MIN_REMAINING = 100
PAUSE = datetime.timedelta(minutes=15)      # a limit without a usable reset time
GRACE_MINUTES = 5
TICK_MINUTES = 5                            # the timer's period (systemd/schedule-kick.timer)
MAX_GRACE_MINUTES = 60
COUNTED_EVENTS = ('schedule', 'workflow_dispatch')
TRUSTED_OWNERS = (0,)                       # who may own gh-as and the folders above it (root)

REPO_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9._-]{1,100}\Z')
WORKFLOW_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,99}\.ya?ml\Z')
REF_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z')
INPUT_RE = re.compile(r'^[A-Za-z_][A-Za-z0-9_-]{0,99}\Z')
ENTRY_KEYS = {'repo', 'workflow', 'cron', 'ref', 'inputs', 'graceMinutes'}
HEADS_KEYS = {'repo', 'workflow', 'graceMinutes', 'maxPerPass'}
KEEP_ON_KEYS = {'repo', 'workflows'}
MAX_HEADS = 10
MAX_KEEP_ON = 10
MAX_KEEP_ON_WORKFLOWS = 20

USAGE = """usage: schedule-kick tick [--config FILE] [--state-dir DIR]
       schedule-kick keep-on [--config FILE] [--state-dir DIR]
       schedule-kick check [--config FILE]
       schedule-kick status [--keep-on] [--state-dir DIR]
       schedule-kick late-check

tick    start each listed workflow whose last cron slot has no run yet (one workflow_dispatch at most per entry),
        then, for each `heads` entry, dispatch the workflow for trusted heads that have no run (at most maxPerPass)
keep-on turn each workflow a `keepOn` entry names back on if it was switched off (never switches one off)
check   read the config and print each entry's last slot; no network
status  print the last tick's status file (--keep-on: the last keep-on pass's); exit 1 unless it was fine
late-check  in a GitHub Actions job of a listed workflow: skip=true in $GITHUB_OUTPUT when this is the first
        attempt of a `schedule` run of the kicked line ($SCHEDULE_KICK_CRON) and a workflow_dispatch run on its
        branch was created since its slot (README, Late schedule runs)

FILE defaults to $XDG_CONFIG_HOME/schedule-kick/config.json (~/.config/...), DIR to
$XDG_STATE_HOME/schedule-kick (~/.local/state/...). See fork-kit/schedule-kick/README.md.
Exit: 0 fine, 1 an entry failed or the tick broke (the next tick tries again), 2 the config was refused
(nothing sent)."""


class ConfigError(Exception):
    pass


class Entry:
    def __init__(self, repo, workflow, cron, ref, inputs, grace=GRACE_MINUTES):
        self.repo, self.workflow, self.cron, self.ref, self.inputs = repo, workflow, cron, ref, inputs
        self.grace = datetime.timedelta(minutes=grace)
        self.key = f'{repo.lower()} {workflow}'

    def slot(self, now):
        """The slot a tick at `now` handles: the last one at least the grace ago."""
        return self.cron.last_slot(now - self.grace)


class HeadsEntry:
    def __init__(self, repo, workflow, grace, per_pass):
        self.repo, self.workflow, self.per_pass = repo, workflow, per_pass
        self.grace = datetime.timedelta(minutes=grace)
        self.key = f'heads {repo.lower()} {workflow}'


class KeepOnEntry:
    def __init__(self, repo, workflows):
        self.repo, self.workflows = repo, workflows


class Config:
    def __init__(self, path, auth, entries, heads=(), keep_on=()):
        self.path, self.auth, self.entries = path, auth, entries
        self.heads, self.keep_on = list(heads), list(keep_on)


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


def plain(text):
    """A string with no control character (C0, DEL, C1, the line and paragraph separators) and no lone surrogate:
    safe in argv, a path or JSON."""
    return isinstance(text, str) and not any(ord(c) < 0x20 or 0x7f <= ord(c) <= 0x9f or ord(c) in (0x2028, 0x2029)
                                             or 0xd800 <= ord(c) <= 0xdfff for c in text)


def _safe_folder(st):
    """A folder above gh-as that only a trusted owner can change (root's sticky /tmp is fine)."""
    if st.st_uid not in TRUSTED_OWNERS:
        return False
    return not st.st_mode & 0o022 or (st.st_mode & stat.S_ISVTX and st.st_uid == 0)


def gh_as_path(path):
    """The real path of the gh-apps kit's gh-as: a program owned by root (TRUSTED_OWNERS) that group and others
    cannot change, in folders only root can change. A user's own copy (~/.local/bin/gh-as) is refused: it could be
    anything, a shim with the person's own gh login too."""
    if os.path.basename(path) != 'gh-as':
        raise ConfigError(f'auth.ghAs {path}: must be the gh-apps kit\'s gh-as, never gh itself')
    real = os.path.realpath(path)
    try:
        st = os.stat(real)
    except OSError:
        raise ConfigError(f'auth.ghAs {path}: no such program') from None
    if os.path.basename(real) != 'gh-as' or not stat.S_ISREG(st.st_mode) or not os.access(real, os.X_OK):
        raise ConfigError(f'auth.ghAs {path}: no such program (it is {real})')
    if st.st_uid not in TRUSTED_OWNERS or st.st_mode & 0o022:
        raise ConfigError(f'auth.ghAs {path}: {real} must be root-owned and not writable by group or others '
                          '(the gh-apps kit installs it so); a user\'s own copy is never used')
    folder = os.path.dirname(real)
    while True:
        try:
            safe = _safe_folder(os.stat(folder))
        except OSError:
            safe = False
        if not safe:
            raise ConfigError(f'auth.ghAs {path}: folder {folder} can be changed by someone other than root')
        if folder == os.path.dirname(folder):
            break
        folder = os.path.dirname(folder)
    return real


def _auth(raw):
    if not isinstance(raw, dict) or len(raw) != 1 or set(raw) - {'tokenFile', 'ghAs'}:
        raise ConfigError('auth must be exactly one of {"tokenFile": "/abs/path"} or {"ghAs": "/abs/path/gh-as"} '
                          '(a person\'s own gh login is never used)')
    (kind, path), = raw.items()
    if not plain(path) or not os.path.isabs(path):
        raise ConfigError(f'auth.{kind} must be an absolute path (no control characters)')
    if kind == 'tokenFile':
        read_token(path)                                        # checked now; read again for each tick
        return ('tokenFile', path)
    return ('ghAs', gh_as_path(path))


def _entry(raw, n, default_grace=GRACE_MINUTES):
    where = f'entries[{n}]'
    if not isinstance(raw, dict):
        raise ConfigError(f'{where} must be an object')
    unknown = set(raw) - ENTRY_KEYS
    if unknown:
        raise ConfigError(f'{where}: unknown key {sorted(unknown)[0]!r} (keys: {", ".join(sorted(ENTRY_KEYS))})')
    for key in ('repo', 'workflow', 'cron', 'ref'):
        if not isinstance(raw.get(key), str):
            raise ConfigError(f'{where}.{key} is missing or not a string')
    if not REPO_RE.match(raw['repo']) or raw['repo'].split('/')[1] in ('.', '..'):
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
        if not INPUT_RE.match(key) or not isinstance(value, str) or len(value) > 1000 or not plain(value):
            raise ConfigError(f'{where}.inputs.{key}: names are letters, digits, _ and -; values are one-line '
                              'strings without control characters (write true, not a JSON true)')
    wait, gap = grace(raw, where, default_grace), line.min_gap()
    if gap is not None and gap < wait + TICK_MINUTES:
        raise ConfigError(f'{where}.cron {raw["cron"]!r}: slots {gap} minutes apart, less than graceMinutes ({wait}) '
                          f'+ the {TICK_MINUTES}-minute tick: each dispatch would land after the next slot and count '
                          f'for it, so only every other slot would run. Lower graceMinutes to '
                          f'{max(gap - TICK_MINUTES, 0)} (0 is safe with late-check); a line with slots under '
                          f'{TICK_MINUTES} minutes apart cannot be kicked at all')
    return Entry(raw['repo'], raw['workflow'], line, raw['ref'], dict(inputs), wait)


def _repo(raw, where):
    if not isinstance(raw.get('repo'), str) or not REPO_RE.match(raw['repo']) or raw['repo'].split('/')[1] in ('.', '..'):
        raise ConfigError(f'{where}.repo {raw.get("repo")!r} is not OWNER/NAME')
    return raw['repo']


def _workflow(value, where):
    if not isinstance(value, str) or not WORKFLOW_RE.match(value):
        raise ConfigError(f'{where} {value!r} is not a workflow file name (name.yml)')
    return value


def _object(raw, where, keys):
    if not isinstance(raw, dict):
        raise ConfigError(f'{where} must be an object')
    unknown = set(raw) - keys
    if unknown:
        raise ConfigError(f'{where}: unknown key {sorted(unknown)[0]!r} (keys: {", ".join(sorted(keys))})')


def _heads_entry(raw, n):
    where = f'heads[{n}]'
    _object(raw, where, HEADS_KEYS)
    repo, workflow = _repo(raw, where), _workflow(raw.get('workflow'), f'{where}.workflow')
    wait = raw.get('graceMinutes', headsmod.GRACE_MINUTES)
    if type(wait) is not int or not 0 <= wait <= headsmod.MAX_GRACE_MINUTES:
        raise ConfigError(f'{where}.graceMinutes must be a whole number of minutes, 0 to {headsmod.MAX_GRACE_MINUTES}')
    per_pass = raw.get('maxPerPass', headsmod.PER_PASS)
    if type(per_pass) is not int or not 1 <= per_pass <= headsmod.MAX_PER_PASS:
        raise ConfigError(f'{where}.maxPerPass must be a whole number, 1 to {headsmod.MAX_PER_PASS}')
    return HeadsEntry(repo, workflow, wait, per_pass)


def _keep_on_entry(raw, n):
    where = f'keepOn[{n}]'
    _object(raw, where, KEEP_ON_KEYS)
    repo, workflows = _repo(raw, where), raw.get('workflows')
    if not isinstance(workflows, list) or not 1 <= len(workflows) <= MAX_KEEP_ON_WORKFLOWS:
        raise ConfigError(f'{where}.workflows must be a list of 1 to {MAX_KEEP_ON_WORKFLOWS} workflow file names')
    names = [_workflow(w, f'{where}.workflows[{i}]') for i, w in enumerate(workflows)]
    if len(set(names)) != len(names):
        raise ConfigError(f'{where}.workflows names a workflow twice')
    return KeepOnEntry(repo, names)


def _list(raw, key, most, parse, ident):
    items = raw.get(key, [])
    if not isinstance(items, list) or len(items) > most:
        raise ConfigError(f'{key} must be a list of at most {most} entries')
    out, seen = [], set()
    for n, item in enumerate(items):
        entry = parse(item, n)
        if ident(entry) in seen:
            raise ConfigError(f'{key}[{n}]: {ident(entry)} is listed twice')
        seen.add(ident(entry))
        out.append(entry)
    return out


def grace(raw, where, default):
    value = raw.get('graceMinutes', default)
    if type(value) is not int or not 0 <= value <= MAX_GRACE_MINUTES:
        raise ConfigError(f'{where}.graceMinutes must be a whole number of minutes, 0 to {MAX_GRACE_MINUTES}')
    return value


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
    unknown = set(raw) - {'version', 'auth', 'entries', 'graceMinutes', 'heads', 'keepOn', '//'}
    if unknown:
        raise ConfigError(f'{path}: unknown key {sorted(unknown)[0]!r}')
    if raw.get('version') != VERSION:
        raise ConfigError(f'{path}: version must be {VERSION}')
    try:
        auth = _auth(raw.get('auth'))
        default_grace = grace(raw, 'config', GRACE_MINUTES)
        entries = raw.get('entries', [])
        if not isinstance(entries, list) or len(entries) > MAX_ENTRIES:
            raise ConfigError(f'entries must be a list of at most {MAX_ENTRIES} entries')
        out, seen = [], set()
        for n, item in enumerate(entries):
            entry = _entry(item, n, default_grace)
            if entry.key in seen:
                raise ConfigError(f'entries[{n}]: {entry.repo} {entry.workflow} is listed twice (one entry per '
                                  'workflow: the run list cannot tell two entries apart)')
            seen.add(entry.key)
            out.append(entry)
        heads = _list(raw, 'heads', MAX_HEADS, _heads_entry, lambda h: h.key)
        keep_on = _list(raw, 'keepOn', MAX_KEEP_ON, _keep_on_entry, lambda k: k.repo.lower())
        if not (out or heads or keep_on):
            raise ConfigError('nothing to do: give entries, heads or keepOn (at least one entry)')
    except ConfigError as e:
        raise ConfigError(f'{path}: {e}') from None
    return Config(path, auth, out, heads, keep_on)


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
    if not isinstance(raw.get('heads', {}), dict):
        warn(f'state file {path}: its heads part is broken; read as empty (the check suites guard)')
        raw['heads'] = {}
    return raw


def waiting_until(text, now):
    """The time in `text` while it is still ahead, but at most MAX_WAIT ahead (later means the clock jumped back:
    the wait is dropped, not kept for days); else None."""
    until = parse_time(text)
    if until is not None and now < until <= now + MAX_WAIT:
        return until
    return None


# -- the tick -------------------------------------------------------------------------------------------------------

def epoch(seconds):
    """An epoch time GitHub gave, as a UTC time; None when it is missing or no time at all."""
    try:
        return datetime.datetime.fromtimestamp(seconds, UTC) if seconds is not None else None
    except (OverflowError, ValueError, OSError, TypeError):
        return None


def counts(row, entry, slot):
    """A run that is this slot's run: `schedule` or `workflow_dispatch`, on the entry's ref, since the slot. A
    pull_request or push run (anyone can make one) or a run on another branch never holds off the dispatch."""
    return row['event'] in COUNTED_EVENTS and row['branch'] == entry.ref and row['time'] >= slot


def fail(state, key, now, message):
    """An error for one entry: it waits (BACKOFF, doubling, at most MAX_WAIT) before its next call."""
    old = state['entries'].get(key, {})
    failures = int(old.get('failures', 0)) + 1 if type(old.get('failures', 0)) is int else 1
    delay = min(BACKOFF * 2 ** min(failures - 1, 10), MAX_WAIT)
    state['entries'][key] = dict(old, failures=failures, retryAfter=iso(now + delay), error=message)


def kick_one(entry, api, state, now):
    """(result, message) for one entry; state['entries'][key] updated in place."""
    slot = entry.slot(now)
    old = state['entries'].get(entry.key, {})
    if old.get('slot') == iso(slot) and old.get('cron') == entry.cron.line:
        return 'done', f'slot {iso(slot)} already {old.get("result", "handled")}'
    wait = waiting_until(old.get('retryAfter'), now)
    if wait is not None:
        return 'waiting', f'after {old.get("failures", 1)} error(s), next try after {iso(wait)}'
    try:
        rows = api.runs_since(entry.repo, entry.workflow, entry.ref, slot)
        for row in rows:
            row['time'] = parse_time(row['created'])
        if any(row['time'] is None for row in rows):
            raise ApiError(f'runs of {entry.repo} {entry.workflow}: a run without a readable created time')
        if any(counts(row, entry, slot) for row in rows):
            result = 'ran'
        else:
            api.dispatch(entry.repo, entry.workflow, entry.ref, entry.inputs)
            result = 'dispatched'
    except ApiError as e:
        fail(state, entry.key, now, str(e))
        if e.rate_limited:
            pause(state, now, now + datetime.timedelta(seconds=e.retry_after) if e.retry_after
                  else epoch(e.reset))
        return 'error', str(e)
    state['entries'][entry.key] = {'cron': entry.cron.line, 'slot': iso(slot), 'result': result, 'at': iso(now)}
    return result, f'slot {iso(slot)}: ' + ('a run exists' if result == 'ran' else
                                             f'workflow_dispatch sent ({entry.ref})')


def pause(state, now, until=None):
    """Every entry waits until GitHub's reset: PAUSE without one (or one already past), at most MAX_WAIT."""
    if until is None or until <= now:
        until = now + PAUSE
    state['pauseUntil'] = iso(min(until, now + MAX_WAIT))


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
    state['heads'] = {k: v for k, v in state.get('heads', {}).items() if k in {h.key for h in cfg.heads}}
    status, failed = {}, False
    paused = waiting_until(state.get('pauseUntil'), now)

    def limited(e):
        pause(state, now, now + datetime.timedelta(seconds=e.retry_after) if e.retry_after else epoch(e.reset))

    jobs = [(e.key, f'{e.repo} {e.workflow}', lambda e=e: kick_one(e, api, state, now),
             lambda e, m: fail(state, e.key, now, m), e) for e in cfg.entries]
    jobs += [(h.key, f'{h.repo} {h.workflow} heads',
              lambda h=h: headsmod.reconcile(h, api, state, now, parse_time, waiting_until, BACKOFF, MAX_WAIT,
                                             lambda: write_json(state_path, state), limited),
              lambda h, m: headsmod.fail(state, h.key, now, m, BACKOFF, MAX_WAIT), h) for h in cfg.heads]
    for key, label, work, on_fault, item in jobs:
        if paused is not None:
            status[key] = {'result': 'paused', 'message': f'GitHub API budget: paused until {iso(paused)}'}
            continue
        before = json.dumps(state, sort_keys=True)
        try:
            result, message = work()
        except Exception as e:      # noqa: BLE001 - one entry's fault never stops the entries after it
            result, message = 'error', f'internal error: {type(e).__name__}: {e}'
            on_fault(item, message)
        remaining = getattr(api, 'remaining', None)
        if result != 'done' and remaining is not None and remaining < MIN_REMAINING:
            pause(state, now, epoch(getattr(api, 'reset', None)))
            message += f' (only {remaining} API calls left: pausing)'
        status[key] = {'result': result, 'message': message}
        line = f'schedule-kick: {label}: {result}: {message}'
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


def broken(e, state_dir, err):
    """The tick broke (a fault outside one entry): say so on stderr and in the status file, so `status` and
    `systemctl --user --failed` show it; the next tick tries again."""
    message = f'the tick broke: {type(e).__name__}: {e}'
    print(f'schedule-kick: {message}', file=err)
    try:
        write_json(os.path.join(state_folder(state_dir), 'status.json'),
                   {'version': VERSION, 'at': iso(_now()), 'ok': False, 'error': message, 'entries': {}})
    except Exception as e2:         # noqa: BLE001 - nothing more to do than say so
        print(f'schedule-kick: cannot write the status file either: {e2}', file=err)
    return 1


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
    parser.add_argument('command', choices=('tick', 'keep-on', 'check', 'status', 'late-check'))
    parser.add_argument('--config')
    parser.add_argument('--state-dir')
    parser.add_argument('--keep-on', dest='keep_on_status', action='store_true')
    try:
        a = parser.parse_args(argv)
    except SystemExit:
        print(USAGE, file=err)
        return 2
    if a.command == 'late-check':
        from . import late
        return late.main(os.environ, out, err)
    state_dir = a.state_dir or default_state_dir()
    if a.keep_on_status and a.command != 'status':
        print(USAGE, file=err)
        return 2
    if a.command == 'status':
        name = 'keep-on-status.json' if a.keep_on_status else 'status.json'
        try:
            with open(os.path.join(state_dir, name), encoding='utf-8') as f:
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
    if a.command == 'keep-on':
        if not cfg.keep_on:
            print(f'schedule-kick keep-on: no keepOn in {cfg.path}: nothing to do', file=out)
            return 0
        try:
            from . import keepon
            folder = state_folder(state_dir)
            return keepon.run(cfg, folder, api_factory(cfg), _now(), out, err, write_json, waiting_until, pause,
                              epoch, MIN_REMAINING)
        except ConfigError as e:
            return refuse(str(e), None, err)
        except Exception as e:      # noqa: BLE001 - a broken pass must not leave an old `ok: true` behind
            print(f'schedule-kick keep-on: the pass broke: {type(e).__name__}: {e}', file=err)
            try:
                write_json(os.path.join(state_folder(state_dir), 'keep-on-status.json'),
                           {'version': VERSION, 'at': iso(_now()), 'ok': False,
                            'error': f'{type(e).__name__}: {e}', 'targets': {}})
            except Exception:       # noqa: BLE001
                pass
            return 1
    if a.command == 'check':
        now = _now()
        for e in cfg.entries:
            print(f'{e.repo} {e.workflow} ({e.cron.line}, ref {e.ref}, grace {e.grace.seconds // 60} min): '
                  f'slot to handle now {iso(e.slot(now))}', file=out)
        for h in cfg.heads:
            print(f'{h.repo} {h.workflow}: heads without a run get one dispatch after {h.grace.seconds // 60} min, '
                  f'at most {h.per_pass} a pass', file=out)
        for k in cfg.keep_on:
            print(f'{k.repo}: keep-on turns back on {", ".join(k.workflows)}', file=out)
        print(f'config fine: {len(cfg.entries)} entries, {len(cfg.heads)} heads, {len(cfg.keep_on)} keepOn, '
              f'auth {cfg.auth[0]}', file=out)
        return 0
    try:
        api = api_factory(cfg)
        return tick(cfg, state_dir, api, out=out, err=err)
    except ConfigError as e:
        return refuse(str(e), None, err)
    except Exception as e:          # noqa: BLE001 - a broken tick must not leave an old `ok: true` behind
        return broken(e, state_dir, err)
