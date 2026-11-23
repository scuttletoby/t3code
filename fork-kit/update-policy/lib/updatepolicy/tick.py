"""The restart policy: one tick every 5 minutes (update-policy.timer), and one look after each boot.

decide() is the whole policy and is pure (a test hands in the clock, the state and the facts):

- outside the quiet window, or with no restart pending (/run/reboot-required): nothing;
- role live: never a restart; one alert per window ("restart pending on a live machine");
- dev, personal: one look at the idle facts every retryMinutes while the window lasts. Idle: restart (the reason
  goes to the journal first, and the state file says when and why). Busy: the reasons go to the journal;
- a window that ended with the restart still pending after a busy look: one alert, "restart pending";
- at most one restart per window: none when the machine booted inside the window already, or a restart was asked
  for in it (a restart that failed is reported, never tried again in a loop). A restart asked for that did not
  happen (the boot id is still the same 10 minutes later: the tick was killed, or systemctl said yes and nothing
  came) is one restart-failed alert;
- every tick: apt's effective Unattended-Upgrade::Automatic-Reboot must be false (this kit owns restarts); else one
  alert at once, then one a day while it stays;
- a pending restart is dated by when this boot first saw it (kept in the state), not by the file's time, which
  each new package that needs a restart rewrites.

State: /var/lib/update-policy/state.json, written whole through a rename. Missing, broken or from another version:
read as empty (said in the journal); the worst it costs is one more look or one more alert.
"""
import contextlib, datetime, fcntl, json, os, socket

from updatepolicy import aptconf, settings as settings_mod, window

UTC = datetime.timezone.utc
STATE_DIR = '/var/lib/update-policy'
STATE_VERSION = 1
PENDING = '/run/reboot-required'
PENDING_PKGS = '/run/reboot-required.pkgs'
CONFIG_NOTE_SECONDS = 3600
APT_ALERT_SECONDS = 86400
MISSED_AFTER = datetime.timedelta(minutes=10)
BOOT_ID = '/proc/sys/kernel/random/boot_id'
WINDOW_KEYS = ('window', 'windowEnd', 'lastLook', 'busy', 'alerted', 'noted')


def iso(t):
    return t.astimezone(UTC).isoformat(timespec='seconds').replace('+00:00', 'Z')


def parse(text):
    if not isinstance(text, str):
        return None
    try:
        t = datetime.datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else None


class Pending:
    """A restart the system asks for: since when (the file's time) and for which packages."""

    def __init__(self, since, packages=()):
        self.since, self.packages = since, list(packages)

    def text(self, now):
        days = (now - self.since).total_seconds() / 86400
        pkgs = ', '.join(self.packages[:6]) + (' ...' if len(self.packages) > 6 else '')
        return (f'restart pending since {self.since.astimezone(UTC):%Y-%m-%d %H:%M} UTC ({days:.1f} days)'
                + (f' for {pkgs}' if pkgs else ''))


def first_seen(state, pending, boot):
    """(pending dated by when this boot first saw it, the state's pendingSeen to keep). The file's time is only used
    the first time: notify-reboot-required rewrites the file for every new package. A new boot starts again."""
    if pending is None:
        return None, None
    seen = state.get('pendingSeen')
    at = parse(seen.get('at')) if isinstance(seen, dict) and seen.get('boot') == boot else None
    since = min(at, pending.since) if at else pending.since
    return Pending(since, pending.packages), {'boot': boot, 'at': iso(since)}


def decide(now, s, state, pending, booted, look, boot='', apt_problem=None):
    """(actions, new state). actions: ('log', level, text), ('alert', event, text), ('restart', text). `look` is
    called at most once and returns the busy reasons ([]: idle). `boot` is this boot's id; `apt_problem` why
    unattended-upgrades may restart by itself (None: it does not)."""
    acts, st = [], dict(state)
    pending, seen = first_seen(st, pending, boot)
    if seen:
        st['pendingSeen'] = seen
    else:
        st.pop('pendingSeen', None)
    # unattended-upgrades must not restart by itself: at once, then once a day
    if apt_problem:
        last = parse(st.get('aptAlertAt'))
        if last is None or not last <= now < last + datetime.timedelta(seconds=APT_ALERT_SECONDS):
            acts.append(('alert', 'apt-restarts', apt_problem))
            st['aptAlertAt'] = iso(now)
    else:
        st.pop('aptAlertAt', None)
    # a restart asked for that did not come: same boot, 10 minutes on
    asked = parse(st.get('restartAt'))
    if (asked and st.get('restartBoot') == boot and now >= asked + MISSED_AFTER
            and st.get('restartMissed') != st.get('restartAt')):
        acts.append(('alert', 'restart-failed', f'the restart asked for at {st["restartAt"]} did not happen (this '
                                                f'is still the same boot): {st.get("restartWhy", "")}'))
        st['restartMissed'] = st['restartAt']
    alerted = list(st.get('alerted') or [])
    # a window that ended with the restart still pending after a busy look: say so once
    end = parse(st.get('windowEnd'))
    if (end and now >= end and pending and st.get('busy') and 'pending' not in alerted
            and st.get('restartWindow') != st.get('window')):
        nxt = window.following(now, s)
        acts.append(('alert', 'restart-pending',
                     f'{pending.text(now)}: busy all through the quiet window of {st.get("window")} '
                     f'(last look: {"; ".join(st["busy"])}). Next try: {nxt.id} {nxt.text()}.'))
        alerted.append('pending')
        st['alerted'] = alerted
    w = window.current(now, s)
    if w is None or pending is None:
        return acts, st
    if st.get('window') != w.id:            # a new window: what the last one saw is done with
        st = {k: v for k, v in st.items() if k not in WINDOW_KEYS}
        st.update(window=w.id, windowEnd=iso(w.end), alerted=[], noted=[])
    alerted, noted = list(st.get('alerted') or []), list(st.get('noted') or [])
    if s['role'] not in settings_mod.RESTARTS:
        if 'live' not in alerted:
            acts.append(('alert', 'live-restart-pending',
                         f'{pending.text(now)}. Role {s["role"]}: this machine never restarts by itself; '
                         'restart it in a planned window.'))
            st['alerted'] = alerted + ['live']
        return acts, st
    if booted >= w.start or st.get('restartWindow') == w.id:
        if 'once' not in noted:
            acts.append(('log', 'notice', f'{pending.text(now)}: already restarted (or tried) in this window '
                                          f'({w.text()}); the next try is in the next window'))
            st['noted'] = noted + ['once']
        return acts, st
    last = parse(st.get('lastLook'))
    if last and last <= now < last + datetime.timedelta(minutes=s['retryMinutes']):
        return acts, st
    busy = list(look())
    st['lastLook'] = iso(now)
    if busy:
        st['busy'] = busy
        acts.append(('log', 'info', f'{pending.text(now)}; busy, so no restart now (next look in '
                                    f'{s["retryMinutes"]} min, window {w.text()}): ' + '; '.join(busy)))
        return acts, st
    why = f'{pending.text(now)}; idle in the quiet window {w.text()} (role {s["role"]})'
    st.update(restartWindow=w.id, restartAt=iso(now), restartWhy=why, restartBoot=boot)
    acts.append(('restart', why))
    return acts, st


def decide_booted(now, s, state, booted):
    """After a boot: (actions, new state). Says once that the machine came back from the restart this tool made."""
    acts, st = [], dict(state)
    asked = parse(st.get('restartAt'))
    if asked is None or booted < asked or st.get('backSeen') == st.get('restartAt'):
        return acts, st
    st['backSeen'] = st['restartAt']
    text = (f'back up: booted {iso(booted)}, {int((booted - asked).total_seconds() // 60)} min after the restart '
            f'asked at {st["restartAt"]} ({st.get("restartWhy", "")})')
    acts.append(('log', 'notice', text))
    if s and s['alertOnBoot']:
        acts.append(('alert', 'back-up', text))
    return acts, st


# -- the machine ------------------------------------------------------------------------------------------------------

class Machine:
    """What tick and booted read and do on the host (files under host.ROOT; programs through host.run)."""

    def __init__(self, host, log):
        self.host, self.log = host, log

    def p(self, path):
        return self.host.p(path)

    def now(self):
        return datetime.datetime.now(UTC)

    def pending(self):
        try:
            since = datetime.datetime.fromtimestamp(os.stat(self.p(PENDING)).st_mtime, UTC)
        except FileNotFoundError:
            return None
        except OSError:
            since = self.now()
        pkgs = []
        try:
            with open(self.p(PENDING_PKGS), encoding='utf-8', errors='replace') as f:
                pkgs = sorted({x.strip() for x in f.read(65536).splitlines() if x.strip()})
        except OSError:
            pass
        return Pending(since, pkgs)

    def booted(self):
        with open(self.p('/proc/stat'), encoding='ascii') as f:
            for line in f:
                if line.startswith('btime '):
                    return datetime.datetime.fromtimestamp(int(line.split()[1]), UTC)
        raise OSError('no btime in /proc/stat')

    def boot_id(self):
        with open(self.p(BOOT_ID), encoding='ascii') as f:
            return f.read().strip()

    def last_look(self):
        """Right before the restart: None, or why not (a block lock on shutdown or sleep taken since the look, or
        the locks cannot be read)."""
        from updatepolicy import idle
        try:
            locks = idle.inhibitors(self.host)
        except idle.CannotTell as e:
            return f'not restarted: {e}'
        if locks:
            return 'not restarted: an inhibitor lock appeared after the look: ' + '; '.join(
                f'{settings_mod.one_line(who, 60)} (uid {uid}): {settings_mod.one_line(why, 120)}'
                for who, why, uid in locks[:3])
        return None

    def reboot(self, why):
        """systemctl reboot without --check-inhibitors: that also refuses while another user is logged in, and
        logged-in sessions are no reason to wait. Block locks were read just before (last_look)."""
        code, _out, err = self.host.run(['systemctl', 'reboot', f'--message=update-policy: {why}'[:400]],
                                        timeout=60)
        return None if code == 0 else settings_mod.one_line(err.strip() or f'exit {code}', 300)

    def alert(self, command, event, text):
        """Runs the alert command with the text on stdin; None or why it failed."""
        env = dict(UPDATE_POLICY_EVENT=event, UPDATE_POLICY_HOST=socket.gethostname(),
                   PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8')
        code, _out, err = self.host.run(command, timeout=60, stdin=text + '\n', env=env)
        return None if code == 0 else (err.strip() or f'exit {code}')[:300]

    # state

    def state_path(self):
        return self.p(f'{STATE_DIR}/state.json')

    def read_state(self):
        try:
            with open(self.state_path(), encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as e:
            self.log('warning', f'state file {STATE_DIR}/state.json cannot be read ({e}): starting from empty state')
            return {}
        if not isinstance(data, dict) or data.get('v') != STATE_VERSION:
            self.log('warning', f'state file {STATE_DIR}/state.json is of another version: starting from empty state')
            return {}
        return data

    def write_state(self, st):
        folder = self.p(STATE_DIR)
        os.makedirs(folder, mode=0o755, exist_ok=True)
        tmp = os.path.join(folder, f'.state.{os.getpid()}.tmp')
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(dict(st, v=STATE_VERSION), f, indent=1, sort_keys=True)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.state_path())

    @contextlib.contextmanager
    def lock(self):
        """One tick or boot look at a time; a second one finds it held and does nothing (None)."""
        os.makedirs(self.p(STATE_DIR), mode=0o755, exist_ok=True)
        fd = os.open(self.p(f'{STATE_DIR}/lock'), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
            yield True
        finally:
            os.close(fd)


def run_actions(m, s, acts, st):
    """Carries out decide()'s actions: logs, alerts (recorded in the state), then the state, then a restart."""
    code = 0
    restart = None
    for act in acts:
        if act[0] == 'log':
            m.log(act[1], act[2])
        elif act[0] == 'alert':
            err = send_alert(m, s, act[1], act[2])
            st['lastAlertError'] = err
            code = code or (1 if err else 0)
        else:
            restart = act[1]
    m.write_state(st)
    if restart:
        # the last step: a job that took a block lock after the look stops the restart (what is left is the time
        # between this read and systemd starting the shutdown, well under a second)
        err = m.last_look()
        if not err:
            m.log('notice', f'restarting now: {restart}')
            err = m.reboot(restart)
        if err:
            m.log('err', f'the restart failed: {err}')
            st['restartMissed'] = st.get('restartAt')      # one restart-failed alert per window, not a second one
            st['lastAlertError'] = send_alert(m, s, 'restart-failed', f'the restart failed ({err}): {restart}')
            m.write_state(st)
            code = 1
    return code


def send_alert(m, s, event, text):
    if not s or not s['alertCommand']:
        m.log('warning', f'alert {event} (no alertCommand in the machine file): {text}')
        return None
    lines = text.splitlines() or ['']
    m.log('warning', f'alert {event}: {lines[0]}' + (f' (and {len(lines) - 1} more lines)' if len(lines) > 1 else ''))
    err = m.alert(s['alertCommand'], event, text)
    if err:
        m.log('err', f'the alert command failed: {err}')
    return err


def tick(m, read_settings, look):
    """One tick: 0, or 1 when something failed (the machine file, an alert, the restart)."""
    with m.lock() as mine:
        if not mine:
            m.log('info', 'another update-policy run holds the lock: nothing to do')
            return 0
        pending = m.pending()
        state = m.read_state()
        try:
            s = read_settings()
        except settings_mod.MachineFileError as e:
            if pending is None:
                return 0
            now = m.now()
            last = parse(state.get('configNoteAt'))
            if last is None or not last <= now < last + datetime.timedelta(seconds=CONFIG_NOTE_SECONDS):
                m.log('err', f'{pending.text(now)}, but the machine file cannot be used, so nothing restarts: {e}')
                state['configNoteAt'] = iso(now)
                m.write_state(state)
            return 1
        now = m.now()
        apt_problem = aptconf.restart_problem(aptconf.dump(m.host))
        acts, st = decide(now, s, state, pending, m.booted(), lambda: look(s), m.boot_id(), apt_problem)
        st.pop('configNoteAt', None)
        if acts or st != state:
            return run_actions(m, s, acts, st)
        return 0


def booted(m, read_settings):
    with m.lock() as mine:
        if not mine:
            return 0
        try:
            s = read_settings()
        except settings_mod.MachineFileError:
            s = None
        state = m.read_state()
        acts, st = decide_booted(m.now(), s, state, m.booted())
        if acts:
            return run_actions(m, s, acts, st)
        return 0
