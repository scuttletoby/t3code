"""The OS adapter: the few things the kit asks of the service manager. Only systemd (user units) exists yet; on
any other system for_this_os() refuses with a plain message until its adapter is written.

The boot service is upstream's own unit (apps/server/src/cloud/bootService.ts: `t3code.service`, with
`Environment=T3CODE_HOME=<home>`). Commands are found on PATH; only tests change SYSTEMCTL and SYSTEMD_RUN.
"""
import re, shutil, subprocess, sys

UNIT = 't3code.service'
SYSTEMCTL = 'systemctl'
SYSTEMD_RUN = 'systemd-run'
TIMEOUT = 30
CGROUP_FILE = '/proc/self/cgroup'  # tests only change it
_UNIT_NAME = re.compile(r'^[A-Za-z0-9_.@-]{1,200}$')


class Unsupported(Exception):
    pass


class AdapterError(Exception):
    pass


def unquote_systemd(raw):
    """Upstream's unquoteSystemdValue (serviceProtocol.ts)."""
    raw = raw.strip()
    if len(raw) >= 2 and raw.startswith('"') and raw.endswith('"'):
        raw = raw[1:-1].replace('\\"', '"').replace('\\\\', '\\')
    return raw.replace('%%', '%')


def unit_home(unit_text):
    """The T3 home a unit serves (its T3CODE_HOME), or None."""
    m = re.search(r'^Environment=T3CODE_HOME=(.*)$', unit_text, re.M)
    return unquote_systemd(m.group(1)) if m else None


class SystemdUser:
    name = 'systemd (user)'

    def _run(self, argv):
        try:
            p = subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, stdin=subprocess.DEVNULL)
        except (OSError, subprocess.TimeoutExpired) as e:
            raise AdapterError(f'{argv[0]} failed: {e}') from None
        if p.returncode != 0:
            raise AdapterError(f'{" ".join(argv[:4])} exited {p.returncode}: {p.stderr.strip()[:300]}')
        return p.stdout

    def service(self):
        """{'loaded', 'active', 'enabled', 'mainPid', 'unitPath', 'home'} of the boot service; read only."""
        out = self._run([SYSTEMCTL, '--user', 'show', UNIT,
                         '--property=LoadState,ActiveState,UnitFileState,FragmentPath,MainPID'])
        props = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
        unit_path = props.get('FragmentPath') or None
        home = None
        if unit_path:
            try:
                with open(unit_path, encoding='utf-8') as f:
                    home = unit_home(f.read())
            except OSError:
                home = None
        try:
            main_pid = int(props.get('MainPID', '0'))
        except ValueError:
            main_pid = 0
        return {'loaded': props.get('LoadState') == 'loaded', 'active': props.get('ActiveState') == 'active',
                'enabled': props.get('UnitFileState') == 'enabled', 'mainPid': main_pid or None,
                'unitPath': unit_path, 'home': home}

    def inside_service(self):
        """True when this process runs inside the boot service (a T3 terminal or agent): stopping the service would
        kill it half-way, so a command that stops T3 must run detached instead. Unreadable: True."""
        try:
            with open(CGROUP_FILE, encoding='utf-8') as f:
                return any(UNIT in line.rstrip('\n').split(':', 2)[-1].split('/') for line in f)
        except OSError:
            return True  # cannot tell: treat it as inside (fail closed)

    def stop(self):
        self._run([SYSTEMCTL, '--user', 'stop', UNIT])

    def start(self):
        self._run([SYSTEMCTL, '--user', 'start', UNIT])

    def run_detached(self, unit, argv, guard_after_seconds=None, guard_argv=None):
        """Run argv as a transient user unit that outlives its caller (a restart of T3 kills anything inside it).
        With a guard, a second transient timer runs guard_argv after guard_after_seconds, whatever happens to the
        first (a crash or a reboot cannot leave T3 down)."""
        for name in (unit, f'{unit}-guard'):
            if not _UNIT_NAME.match(name):
                raise AdapterError(f'not a safe unit name: {name!r}')
        if guard_argv is not None:
            if not (isinstance(guard_after_seconds, int) and guard_after_seconds > 0):
                raise AdapterError('a guard needs a time in whole seconds')
            self._run([SYSTEMD_RUN, '--user', '--quiet', '--collect', f'--unit={unit}-guard',
                       f'--on-active={guard_after_seconds}s', '--timer-property=AccuracySec=1s', '--', *guard_argv])
        self._run([SYSTEMD_RUN, '--user', '--quiet', '--collect', f'--unit={unit}', '--', *argv])


def for_this_os():
    if not sys.platform.startswith('linux'):
        raise Unsupported(f'the go-live kit has no service adapter for {sys.platform} yet; it changes nothing here')
    if shutil.which(SYSTEMCTL) is None or shutil.which(SYSTEMD_RUN) is None:
        raise Unsupported('the go-live kit needs systemd (systemctl and systemd-run); none was found')
    return SystemdUser()
