"""machine_file: read one kit's section of the private machine file (fork-kit/common/README.md).

The machine file holds the facts of one machine (limits, unit names, patterns) that the public kits must not
contain. There is one per machine, at a fixed path per OS:

  Linux    /etc/fork-kit/machine.json
  macOS    /Library/Application Support/fork-kit/machine.json
  Windows  %ProgramData%\\fork-kit\\machine.json

Shape: {"version": 1, "<kit folder name>": {...}, ...}, camelCase keys. Rules, all checked here:

- root-owned and not writable by group or others (the file and its folder); neither may be a symbolic link, and
  the file must be a plain file of at most 1 MiB. These checks are POSIX only: on Windows the file is refused;
- a JSON object with no key given twice whose "version" is a whole number this loader knows (1);
- no secrets: a key whose name says secret, token, password or private key holds only "file:<path>";
- a kit reads "version" and its own section only; a missing section, an unknown key in it or a value of the
  wrong type is refused, with the reason named.

Tools never write it. Kits call read_section() and then fields() for each object in their section.
Plain Python 3 standard library; each kit's installer copies this file into its version folder.
"""
import errno, json, os, re, stat, sys

SUPPORTED_VERSION = 1
SECRET_KEY = re.compile(r'(secret|token|password|passwd|privatekey|private_key|apikey|api_key)', re.I)
REQUIRED = object()
MAX_BYTES = 1 << 20


class MachineFileError(Exception):
    """The machine file cannot be used; the message names the file and the reason."""


def default_path(platform=None):
    platform = platform or sys.platform
    if platform.startswith('linux'):
        return '/etc/fork-kit/machine.json'
    if platform == 'darwin':
        return '/Library/Application Support/fork-kit/machine.json'
    if platform in ('win32', 'cygwin'):
        return os.path.join(os.environ.get('ProgramData') or r'C:\ProgramData', 'fork-kit', 'machine.json')
    raise MachineFileError(f'no machine file path for platform {platform!r}')


def _check_owner(path, what, st, owner_uid):
    if st.st_uid != owner_uid:
        raise MachineFileError(f'{path}: {what} is owned by uid {st.st_uid}, not {owner_uid} (root)')
    if st.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise MachineFileError(f'{path}: {what} is writable by group or others (mode {stat.S_IMODE(st.st_mode):o})')


def _read(path, owner_uid):
    """The file's text. The folder is checked without following a link; the file is opened without following one,
    and the opened file itself (not the path, which could be swapped in between) is checked."""
    if os.name == 'nt':
        raise MachineFileError(f'{path}: not read on Windows, where its owner and permission checks do not exist yet')
    folder = os.path.dirname(path) or '.'
    st = os.lstat(folder)
    if stat.S_ISLNK(st.st_mode):
        raise MachineFileError(f'{path}: {folder} is a symbolic link')
    _check_owner(path, folder, st, owner_uid)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | getattr(os, 'O_CLOEXEC', 0))
    except OSError as e:
        if e.errno == errno.ELOOP:
            raise MachineFileError(f'{path}: is a symbolic link') from None
        raise
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise MachineFileError(f'{path}: is not a plain file')
        _check_owner(path, path, st, owner_uid)
        f = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    with f:
        data = f.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise MachineFileError(f'{path}: larger than {MAX_BYTES} bytes')
    try:
        return data.decode('utf-8')
    except UnicodeDecodeError as e:
        raise MachineFileError(f'{path}: not UTF-8 ({e})') from None


def _no_duplicates(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError(f'key {k!r} is given twice')
        out[k] = v
    return out


def _check_no_secrets(value, where):
    if isinstance(value, dict):
        for k, v in value.items():
            if SECRET_KEY.search(str(k)) and not (isinstance(v, str) and v.startswith('file:')):
                raise MachineFileError(f'{where}.{k}: looks like a secret; the machine file holds only '
                                       f'"file:<path>" references to secrets')
            _check_no_secrets(v, f'{where}.{k}')
    elif isinstance(value, list):
        for i, v in enumerate(value):
            _check_no_secrets(v, f'{where}[{i}]')


def read_section(kit, path=None, owner_uid=0):
    """The kit's section (a dict). Raises MachineFileError for every rule above. `path` and `owner_uid` are for
    tests; shipped commands use the default path and root."""
    path = path or default_path()
    try:
        text = _read(path, owner_uid)
    except FileNotFoundError:
        raise MachineFileError(f'{path}: not there (the owner writes it; see fork-kit/common/README.md)') from None
    except OSError as e:
        raise MachineFileError(f'{path}: cannot be read ({e.strerror or e})') from None
    try:
        data = json.loads(text, object_pairs_hook=_no_duplicates)
    except ValueError as e:
        raise MachineFileError(f'{path}: not valid JSON ({e})') from None
    except RecursionError:
        raise MachineFileError(f'{path}: nested too deeply') from None
    if not isinstance(data, dict):
        raise MachineFileError(f'{path}: must hold a JSON object')
    version = data.get('version')
    if type(version) is not int:
        raise MachineFileError(f'{path}: "version" must be a whole number (it is {version!r})')
    if version > SUPPORTED_VERSION:
        raise MachineFileError(f'{path}: version {version} is newer than this kit knows ({SUPPORTED_VERSION}); '
                               f'update the kit')
    if version < 1:
        raise MachineFileError(f'{path}: bad version {version}')
    if kit not in data:
        raise MachineFileError(f'{path}: no "{kit}" section')
    section = data[kit]
    if not isinstance(section, dict):
        raise MachineFileError(f'{path}: "{kit}" must be a JSON object')
    _check_no_secrets(section, f'{path}: {kit}')
    return section


def fields(obj, spec, where):
    """Check one object against spec {key: (types, default or REQUIRED, check or None)} and return it with the
    defaults filled in. Unknown keys, missing required keys, wrong types and failed checks are refused.
    `check(value)` returns an error text, or None when the value is fine. bool is never taken for a number."""
    if not isinstance(obj, dict):
        raise MachineFileError(f'{where}: must be a JSON object')
    unknown = sorted(set(obj) - set(spec))
    if unknown:
        raise MachineFileError(f'{where}: unknown key {unknown[0]!r} (known: {", ".join(sorted(spec))})')
    out = {}
    for key, (types, default, check) in spec.items():
        if key not in obj:
            if default is REQUIRED:
                raise MachineFileError(f'{where}: "{key}" is missing')
            out[key] = default
            continue
        value = obj[key]
        types = types if isinstance(types, tuple) else (types,)
        if (isinstance(value, bool) and bool not in types) or not isinstance(value, types):
            names = ' or '.join(t.__name__ for t in types)
            raise MachineFileError(f'{where}.{key}: must be {names} (it is {value!r})')
        problem = check(value) if check else None
        if problem:
            raise MachineFileError(f'{where}.{key}: {problem}')
        out[key] = value
    return out
