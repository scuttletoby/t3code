"""update-policy: restart only when idle, at the quiet hour, by role; and a daily baseline check
(fork-kit/update-policy/README.md).

  update-policy tick        one tick of the restart policy (update-policy.timer runs it every 5 minutes, as root)
  update-policy idle        looks only: is the machine idle now? the busy reasons; exit 0 idle, 75 busy
  update-policy baseline    the daily check: prints only drift, alerts with it; exit 0 none, 1 drift
  update-policy booted      after a boot: says once that a restart this kit made came back (update-policy-boot)
  update-policy status      the role, the window, a pending restart and what the last ticks saw
  update-policy exposure    the daily exposure check: KEV, security fixes waiting, ranked by EPSS; exit 0 none, 1 any
                            (update-policy-exposure.service runs it as an unprivileged user; --cache DIR)
  update-policy exposure-alert   sends the last exposure report through the alert command (as root, after it)
  update-policy test-alert  sends one test alert and says which channel took it; exit 0 sent, 1 none or it failed

The facts of the machine (role, quiet hour, time zone, alert command, ...) are in the "update-policy" section of
/etc/fork-kit/machine.json. Run it as root: it reads every user's cgroups, containers and sockets.
"""
import argparse, datetime, json, os, sys

from updatepolicy import baseline, exposure, idle, settings, tick, window

LEVELS = {'err': 3, 'warning': 4, 'notice': 5, 'info': 6}
EXIT_IDLE, EXIT_BUSY = 0, 75


def to_journal():
    """stdout is the journal (systemd's JOURNAL_STREAM names it: device:inode), not a terminal or a pipe."""
    try:
        st = os.fstat(sys.stdout.fileno())
        return os.environ.get('JOURNAL_STREAM') == f'{st.st_dev}:{st.st_ino}'
    except (OSError, ValueError, AttributeError):
        return False


def log(level, text):
    """One line for the journal: with its priority prefix when stdout is the journal."""
    prefix = f'<{LEVELS[level]}>' if to_journal() else ''
    print(f'{prefix}update-policy: {settings.one_line(text, 4000)}', flush=True)


def make(host=None):
    host = host or idle.Host()
    if host.BIN is None:
        host.BIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__)))),
                                'bin', 'update-policy')
    return host, tick.Machine(host, log)


def status(host, m, read_settings):
    try:
        s = read_settings()
    except settings.MachineFileError as e:
        print(f'machine file: cannot be used ({e})')
        return 1
    now = m.now()
    w = window.current(now, s)
    nxt = window.following(now, s)
    pending = m.pending()
    does = 'restarts when idle in its window' if s['role'] in settings.RESTARTS else 'never restarts; alerts'
    print(f'role: {s["role"]} ({does})')
    print(f'window: {"now, " + w.text() if w else "next " + nxt.id + " " + nxt.text()}')
    print(f'pending: {pending.text(now) if pending else "no restart pending"}')
    st = m.read_state()
    for key in ('window', 'lastLook', 'busy', 'restartAt', 'restartWhy', 'backSeen', 'lastAlertError'):
        if st.get(key):
            print(f'{key}: {st[key]}')
    return 0


def test_alert(m, read_settings):
    """One alert, event "test", through the channel the tick and the baseline use: proves the channel end to end."""
    try:
        s = read_settings()
    except settings.MachineFileError as e:
        print(f'update-policy test-alert: the machine file cannot be used, nothing sent: {e}')
        return 1
    if not s['alertCommand']:
        print('update-policy test-alert: no alert channel (no "alert" section and no update-policy alertCommand in '
              'the machine file): nothing sent')
        return 1
    where = ('the update-policy alertCommand' if s['alertSource'] == 'update-policy'
             else 'the machine file\'s "alert" section')
    err = tick.send_alert(m, s, 'test', 'test alert from update-policy: if you can read this, alerts reach you')
    if err is not None:
        print(f'update-policy test-alert: sent through {where}, and it failed: {err}')
        return 1
    print(f'update-policy test-alert: sent through {where}')
    return 0


def main(argv, host=None, read_settings=None):
    ap = argparse.ArgumentParser(prog='update-policy', description=__doc__.split('\n\n')[0],
                                 epilog='\n\n'.join(__doc__.split('\n\n')[1:]),
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True, metavar='COMMAND')
    sub.add_parser('tick')
    i = sub.add_parser('idle')
    i.add_argument('--json', action='store_true')
    sub.add_parser('baseline')
    sub.add_parser('booted')
    sub.add_parser('status')
    e = sub.add_parser('exposure')
    e.add_argument('--cache', default=exposure.CACHE)
    e2 = sub.add_parser('exposure-alert')
    e2.add_argument('--cache', default=exposure.CACHE)
    sub.add_parser('test-alert')
    d = sub.add_parser('docker-ps')
    d.add_argument('socket')
    a = ap.parse_args(argv)
    host, m = make(host)
    read_settings = read_settings or settings.read
    if a.cmd == 'docker-ps':        # run as the socket's user by idle (never as root on a user's socket)
        names = idle.docker_running(a.socket)
        if isinstance(names, str):
            print(names, file=sys.stderr)
            return 1
        print(json.dumps(names))
        return 0
    if a.cmd == 'exposure':
        return exposure.run(host, read_settings, a.cache)
    if a.cmd == 'exposure-alert':
        return exposure.alert(m, read_settings, a.cache)
    if a.cmd == 'tick':
        return tick.tick(m, read_settings, lambda s: idle.look(host, s))
    if a.cmd == 'booted':
        return tick.booted(m, read_settings)
    if a.cmd == 'baseline':
        return baseline.run(host, m, read_settings)
    if a.cmd == 'status':
        return status(host, m, read_settings)
    if a.cmd == 'test-alert':
        return test_alert(m, read_settings)
    try:
        s = read_settings()
    except settings.MachineFileError as e:
        print(f'update-policy idle: the machine file cannot be used: {e}', file=sys.stderr)
        return 1
    busy = idle.look(host, s)
    if a.json:
        print(json.dumps({'idle': not busy, 'busy': busy,
                          'at': datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds')}))
    else:
        print('idle' if not busy else '\n'.join(f'busy: {b}' for b in busy))
    return EXIT_IDLE if not busy else EXIT_BUSY
