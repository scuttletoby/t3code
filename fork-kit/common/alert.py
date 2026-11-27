"""alert: the one alert channel the kits of a machine share (fork-kit/common/README.md, "The alert channel").

The machine file's "alert" section names one command; a kit with something to say runs it with the text on stdin
and the kit and event in its environment. A kit may still have its own alert command (update-policy's
`alertCommand`): that one wins where it is set, so a machine file from before this section works as before.

  read(path=None, owner_uid=0)   the checked section, or None when the machine file has none
  from_file(machine)             the same from a machine_file.read_file() the kit already holds
  environment(kit, event)        the plain environment the command runs with
  command_problem(argv)          why a command cannot be an alert command, or None

Plain Python 3 standard library; the alert itself is run by each kit (its own runner, timeout and log)."""
import os, re, socket
import machine_file
from machine_file import REQUIRED

SECTION = 'alert'
TIMEOUT = 60   # seconds, unless the section says otherwise
# the range of timeoutSeconds. A kit runs its alerts inside a systemd unit with a time limit (update-policy: 5 min,
# and 90 s for an ExecStopPost=): one alert must never use up a unit's time, or the run is killed before it saves
# that the alert failed. Each kit also caps all the alerts of one run together (update-policy: tick.ALERT_BUDGET).
MIN_TIMEOUT, MAX_TIMEOUT = 5, 120
# values that look like credentials: GitHub, OpenAI-style and Slack tokens, and long hex or base64 runs. A secret in
# the alert command's argv is readable by every user in /proc while it runs: keep it in a root-only file the
# command reads.
TOKEN_RES = (re.compile(r'(gh[pousr]_|github_pat_)[A-Za-z0-9_]{8,}'), re.compile(r'\bsk-[A-Za-z0-9_-]{16,}'),
             re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{8,}'), re.compile(r'\b[0-9a-fA-F]{32,}\b'),
             re.compile(r'(?<![/A-Za-z0-9+_=-])(?=[A-Za-z0-9+_=-]*[0-9])(?=[A-Za-z0-9+_=-]*[a-z])'
                        r'(?=[A-Za-z0-9+_=-]*[A-Z])[A-Za-z0-9+_=-]{32,}(?![/A-Za-z0-9+_=-])'))
EVENT_RE = re.compile(r'^[a-z][a-z0-9-]{0,63}\Z')


def command_problem(value):
    """None when `value` is a usable alert command: a program (absolute path) and plain arguments, none of which
    looks like a token."""
    if not value:
        return 'must name a program (an absolute path) and its arguments'
    if not all(isinstance(x, str) and x and '\0' not in x for x in value):
        return 'must be a list of strings'
    for x in value[1:]:
        if any(r.search(x) for r in TOKEN_RES):
            return ('an argument looks like a token or key: every user can read the arguments while the command runs; '
                    'keep the secret in a root-only file the command reads')
    return None if os.path.isabs(value[0]) else 'its first string must be an absolute path to a program'


SPEC = {
    'command': (list, REQUIRED, command_problem),
    'timeoutSeconds': (int, TIMEOUT, lambda v: None if MIN_TIMEOUT <= v <= MAX_TIMEOUT
                       else f'must be from {MIN_TIMEOUT} to {MAX_TIMEOUT}'),
}


def read(path=None, owner_uid=0):
    """The "alert" section with its defaults, or None when the machine file (or the section) is not there. A broken
    section raises machine_file.MachineFileError like any other: a typo is never taken for "no channel"."""
    return from_file(machine_file.read_file(path=path, owner_uid=owner_uid, optional=True))


def from_file(machine):
    """read() on a machine_file.MachineFile a kit has read already (one read for its own section and this one)."""
    section = machine.section(SECTION, optional=True)
    return None if section is None else machine_file.fields(section, SPEC, f'{machine.path}: {SECTION}')


def environment(kit, event):
    """FORK_KIT_ALERT_KIT, FORK_KIT_ALERT_EVENT, FORK_KIT_ALERT_HOST, and a plain PATH and LANG: nothing else of the
    caller's environment reaches the command."""
    if not EVENT_RE.match(event or ''):
        raise ValueError(f'bad alert event {event!r}')
    return dict(FORK_KIT_ALERT_KIT=kit, FORK_KIT_ALERT_EVENT=event, FORK_KIT_ALERT_HOST=socket.gethostname(),
                PATH='/usr/sbin:/usr/bin:/sbin:/bin', LANG='C.UTF-8')


__all__ = ['SECTION', 'TIMEOUT', 'MIN_TIMEOUT', 'MAX_TIMEOUT', 'TOKEN_RES', 'command_problem', 'read', 'from_file',
           'environment']
