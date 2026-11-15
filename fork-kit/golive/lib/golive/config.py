"""The kit's machine facts: the "golive" section of the machine file (fork-kit/common/machine_file.py)."""
import os, re

import machine_file

KIT = 'golive'
REQUIRED = ('home', 'targetVersion', 'stageDir', 'backupDir')
OPTIONAL = ('stateDir', 'freeFloorMb')
DEFAULT_FREE_FLOOR_MB = 2048

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
    "freeFloorMb": 2048
  }
}'''


class Config:
    def __init__(self, section):
        machine_file.section_keys(KIT, section, REQUIRED, OPTIONAL)
        for key in REQUIRED + ('stateDir',):
            value = section.get(key)
            if key in section and (not isinstance(value, str) or not value.strip()):
                raise machine_file.MachineFileError(f'"{KIT}.{key}" must be a non-empty string')
            if key != 'targetVersion' and value is not None and not os.path.isabs(value):
                raise machine_file.MachineFileError(f'"{KIT}.{key}" must be an absolute path')
        if not EXACT_VERSION.match(section['targetVersion']):
            raise machine_file.MachineFileError(f'"{KIT}.targetVersion" is not an exact version')
        floor = section.get('freeFloorMb', DEFAULT_FREE_FLOOR_MB)
        if type(floor) is not int or floor < 0:
            raise machine_file.MachineFileError(f'"{KIT}.freeFloorMb" must be a whole number >= 0')
        self.home = os.path.normpath(section['home'])
        self.target_version = section['targetVersion']
        self.stage_dir = os.path.normpath(section['stageDir'])
        self.backup_dir = os.path.normpath(section['backupDir'])
        self.free_floor_mb = floor
        self.state_dir = os.path.normpath(section.get('stateDir') or default_state_dir())


def default_state_dir():
    base = os.environ.get('XDG_STATE_HOME') or os.path.join(os.path.expanduser('~'), '.local', 'state')
    return os.path.join(base, 'fork-kit', KIT)


def load():
    """The config, or MachineFileError naming what is wrong."""
    return Config(machine_file.load_section(KIT))
