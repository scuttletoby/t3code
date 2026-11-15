"""The kit's machine facts: the "golive" section of the machine file (fork-kit/common/machine_file.py).

Every folder comes from the machine file, never from the environment: a detached `switch` (a transient user unit)
and a `check` from a shell must find the same lock and journal whatever their environments hold.
"""
import os, re

import machine_file

KIT = 'golive'
DEFAULT_FREE_FLOOR_MB = 2048
MACHINE_FILE = None  # tests only: read this file instead of the fixed path
MACHINE_OWNER = 0    # tests only: the uid that must own it (root)

# Upstream's exact service version (apps/server/src/cloud/serviceProtocol.ts, EXACT_SERVICE_VERSION).
_NUM = r'(?:0|[1-9]\d*)'
_PRE = r'(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)'
EXACT_VERSION = re.compile(rf'^{_NUM}\.{_NUM}\.{_NUM}(?:-{_PRE}(?:\.{_PRE})*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$')

TEMPLATE = '''{
  "version": 1,
  "golive": {
    "home": "/path/to/the/t3/home",
    "targetVersion": "0.0.0-example.fork.1",
    "stageDir": "/path/to/the/downloaded/release",
    "backupDir": "/path/to/the/backups",
    "stateDir": "/path/to/the/kit/state",
    "freeFloorMb": 2048,
    "quietCommand": "/path/to/a/quiet/check",
    "preHooks": ["/path/to/pause/background/jobs"],
    "postHooks": ["/path/to/resume/background/jobs"]
  }
}'''


def _absolute(value):
    return None if os.path.isabs(value) else 'must be an absolute path'


def _exact_version(value):
    return None if EXACT_VERSION.match(value) else 'is not an exact version'


def _not_negative(value):
    return None if value >= 0 else 'must be a whole number >= 0'


def _positive(value):
    return None if value > 0 else 'must be a whole number > 0'


def _absolute_list(value):
    bad = [v for v in value if not (isinstance(v, str) and os.path.isabs(v))]
    return f'every entry must be an absolute path (not {bad[0]!r})' if bad else None


SPEC = {
    'home': (str, machine_file.REQUIRED, _absolute),
    'targetVersion': (str, machine_file.REQUIRED, _exact_version),
    'stageDir': (str, machine_file.REQUIRED, _absolute),
    'backupDir': (str, machine_file.REQUIRED, _absolute),
    'stateDir': (str, machine_file.REQUIRED, _absolute),
    'freeFloorMb': (int, DEFAULT_FREE_FLOOR_MB, _not_negative),
    # Same contract as the fork setting automaticUpdatesQuietCommand: absolute path, no arguments, exit 0 = quiet.
    'quietCommand': (str, None, _absolute),
    # Run in order, no arguments, before T3 stops (pre) and after it runs again (post); each must be safe twice.
    'preHooks': (list, (), _absolute_list),
    'postHooks': (list, (), _absolute_list),
    'quietWaitSeconds': (int, 7200, _not_negative),  # how long to wait for a quiet point, then give up
    'stopLimitSeconds': (int, 1800, _positive),      # longest T3 stays stopped for a copy; then it starts again
}
# The kit's own folders; none may be the T3 home or inside it (check promises to write nothing there).
OWN_FOLDERS = ('stateDir', 'backupDir', 'stageDir')


def _inside(path, folder):
    path, folder = os.path.realpath(path), os.path.realpath(folder)
    return path == folder or os.path.commonpath([path, folder]) == folder


class Config:
    def __init__(self, section, where=KIT):
        values = machine_file.fields(section, SPEC, where)
        self.home = os.path.normpath(values['home'])
        self.target_version = values['targetVersion']
        self.stage_dir = os.path.normpath(values['stageDir'])
        self.backup_dir = os.path.normpath(values['backupDir'])
        self.state_dir = os.path.normpath(values['stateDir'])
        self.free_floor_mb = values['freeFloorMb']
        self.quiet_command = values['quietCommand']
        self.pre_hooks, self.post_hooks = tuple(values['preHooks']), tuple(values['postHooks'])
        self.quiet_wait_seconds = values['quietWaitSeconds']
        self.stop_limit_seconds = values['stopLimitSeconds']
        for key in OWN_FOLDERS:
            if _inside(values[key], self.home):
                raise machine_file.MachineFileError(
                    f'{where}.{key}: {values[key]} is the T3 home or inside it; the kit keeps its own files '
                    f'outside {self.home}')


def load():
    """The config, or MachineFileError naming the file and what is wrong."""
    path = MACHINE_FILE or machine_file.default_path()
    return Config(machine_file.read_section(KIT, path, MACHINE_OWNER), f'{path}: {KIT}')
