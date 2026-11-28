"""keep-on: turn the named workflows back on when something switches them off (fork-kit/schedule-kick/README.md,
Keep-on).

Something outside this repository's control sets a few workflows to `disabled_manually` on a 20-minute grid, and
GitHub drops every event of a workflow while it is off. `schedule-kick keep-on`, run by its own timer every few
seconds, makes one workflow-list call per `keepOn` entry and turns back on (PUT .../enable) each workflow the entry
names that is `disabled_manually` or `disabled_inactivity`. Every re-enable is one ALERT line in the journal and
counts in keep-on-status.json; a target it cannot turn back on (missing, a state it does not know, an API refusal)
is an error line, exit 1, and an alert through the machine's alert channel (fork-kit/common/README.md), at most once
an hour for each target.

It never switches a workflow off, and it never touches a workflow its entry does not name: the GitHub client has no
call that switches one off (github.py), and only the named rows are read.
"""
import datetime, fcntl, json, os, subprocess

from .github import ApiError, iso

TURNED_ON = ('disabled_manually', 'disabled_inactivity')   # the off states keep-on turns back on
ALERT_EVERY = datetime.timedelta(hours=1)                    # one alert channel message per target and hour at most
VERSION = 1
KIT = 'schedule-kick'
EVENT = 'workflow-off'


def _load(path, warn):
    try:
        with open(path, encoding='utf-8') as f:
            raw = json.load(f)
    except FileNotFoundError:
        return {'version': VERSION, 'targets': {}}
    except (OSError, ValueError) as e:
        warn(f'keep-on state {path} cannot be read ({e}); starting from empty state')
        return {'version': VERSION, 'targets': {}}
    if not isinstance(raw, dict) or raw.get('version') != VERSION or not isinstance(raw.get('targets'), dict):
        warn(f'keep-on state {path} is not version {VERSION} state; starting from empty state')
        return {'version': VERSION, 'targets': {}}
    raw['targets'] = {k: v for k, v in raw['targets'].items() if isinstance(v, dict)}
    return raw


def channel_alert(text, read_channel=None, run=subprocess.run):
    """Send `text` through the machine file's alert channel. (sent, why): (False, reason) without a channel or when
    the command fails; never raises."""
    try:
        if read_channel is None:
            import alert as alert_channel                       # fork-kit/common/alert.py, copied beside the kit
            section, env = alert_channel.read(), alert_channel.environment(KIT, EVENT)
        else:
            section, env = read_channel()
        if section is None:
            return False, 'no alert channel in the machine file (journal only)'
        r = run(section['command'], input=text, capture_output=True, text=True, env=env,
                timeout=section.get('timeoutSeconds', 60))
        return (True, '') if r.returncode == 0 else (False, f'alert command exit {r.returncode}')
    except Exception as e:      # noqa: BLE001 - an alert that cannot be sent is logged, never fatal
        return False, f'alert not sent: {type(e).__name__}: {e}'


def run(cfg, folder, api, now, out, err, write_json, waiting_until, pause, epoch, min_remaining, send=channel_alert):
    """One keep-on pass over every keepOn entry. Returns the exit code."""
    lock = open(os.path.join(folder, 'keep-on.lock'), 'a')
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('schedule-kick keep-on: another pass is running; this one does nothing', file=out)
            return 0
        return _pass(cfg, folder, api, now, out, err, write_json, waiting_until, pause, epoch, min_remaining, send)
    finally:
        lock.close()


def _pass(cfg, folder, api, now, out, err, write_json, waiting_until, pause, epoch, min_remaining, send):
    path = os.path.join(folder, 'keep-on.json')
    state = _load(path, lambda m: print(f'schedule-kick keep-on: {m}', file=err))
    keys = {f'{e.repo.lower()} {w}' for e in cfg.keep_on for w in e.workflows}
    state['targets'] = {k: v for k, v in state['targets'].items() if k in keys}
    status, failed = {}, False
    paused = waiting_until(state.get('pauseUntil'), now)

    def problem(key, label, message):
        t = state['targets'].setdefault(key, {})
        line = f'schedule-kick keep-on: {label}: error: {message}'
        print(line, file=err)
        last = t.get('alertedAt')
        if not (isinstance(last, str) and last > iso(now - ALERT_EVERY) and last <= iso(now)):
            sent, why = send(f'{label}: keep-on cannot keep it on: {message}')
            t['alertedAt'] = iso(now)
            if not sent:
                print(f'schedule-kick keep-on: {label}: {why}', file=err)
        t['error'] = message
        status[key] = {'result': 'error', 'message': message}

    for entry in cfg.keep_on:
        targets = [(f'{entry.repo.lower()} {w}', f'{entry.repo} {w}', w) for w in entry.workflows]
        if paused is not None:
            for key, _, _ in targets:
                status[key] = {'result': 'paused', 'message': f'GitHub API budget: paused until {iso(paused)}'}
            continue
        try:
            rows = api.workflows(entry.repo)
        except ApiError as e:
            for key, label, _ in targets:
                problem(key, label, f'cannot list the workflows: {e}')
            failed = True
            if e.rate_limited:
                pause(state, now, now + datetime.timedelta(seconds=e.retry_after) if e.retry_after
                      else epoch(e.reset))
                paused = waiting_until(state.get('pauseUntil'), now)
            continue
        by_file = {}
        for r in rows:
            if r['path'].startswith('.github/workflows/'):
                by_file.setdefault(r['path'][len('.github/workflows/'):], r)
        for key, label, workflow in targets:
            r = by_file.get(workflow)
            if r is None:
                problem(key, label, 'no such workflow in the repository (renamed or deleted? fix the config)')
                failed = True
                continue
            if r['state'] == 'active':
                t = state['targets'].setdefault(key, {})
                t.pop('error', None)
                status[key] = {'result': 'on', 'message': 'active'}
                continue
            if r['state'] not in TURNED_ON:
                problem(key, label, f'state {r["state"]}: keep-on turns on only {" and ".join(TURNED_ON)}')
                failed = True
                continue
            try:
                api.enable(entry.repo, r['id'])
            except ApiError as e:
                problem(key, label, f'was {r["state"]}, and turning it back on failed: {e}')
                failed = True
                if e.rate_limited:
                    pause(state, now, epoch(e.reset))
                continue
            t = state['targets'].setdefault(key, {})
            t.pop('error', None)
            t['reenabled'] = int(t.get('reenabled', 0)) + 1 if type(t.get('reenabled', 0)) is int else 1
            t['lastReenabled'] = iso(now)
            t['lastState'] = r['state']
            message = f'was {r["state"]}: turned back on (re-enable number {t["reenabled"]} since the state began)'
            print(f'schedule-kick keep-on: ALERT {label}: {message}', file=out)
            status[key] = {'result': 're-enabled', 'message': message}
        remaining = getattr(api, 'remaining', None)
        if remaining is not None and remaining < min_remaining:
            pause(state, now, epoch(getattr(api, 'reset', None)))
            paused = waiting_until(state.get('pauseUntil'), now)
    if paused is not None:
        print(f'schedule-kick keep-on: GitHub API budget: paused until {iso(paused)}', file=err)
    else:
        state.pop('pauseUntil', None)
    write_json(path, state)
    write_json(os.path.join(folder, 'keep-on-status.json'),
               {'version': VERSION, 'at': iso(now), 'ok': not failed, 'config': cfg.path, 'targets': status})
    return 1 if failed else 0
