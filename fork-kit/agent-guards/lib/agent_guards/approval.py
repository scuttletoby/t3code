"""agent_guards.approval: the owner's approval for one actions-disable-guard refusal, as a root-owned file.

The owner (root) runs `sudo agent-guards approve [--hours N]`. That makes a random code, prints it once, and keeps
only its SHA-256 and an end time in PATH (root-owned, mode 0644, written whole and renamed into place). A command
that carries `#owner-approved:actions-disable:<code>` passes the guard while the file holds that code's hash and
the end time has not come. `sudo agent-guards approve --revoke` removes the file.

Why a file and not an environment variable: a variable set when a session starts is inherited by every process of
that session, a nested `claude -p` or `codex exec` too, so an approval meant for one command reached every agent
started under it. The code reaches an agent only when the owner hands it over, and an agent cannot write the file
(it is root's, in a root-owned folder), so it cannot approve itself. Who can become root without a password (sudo
NOPASSWD, the docker group) can write it: `agent-guards self-check` warns about that.

The file is read under the machine file's rules (fork-kit/common/machine_file.py: every folder on the way and the
file root-owned, not writable by group or others, no link). Anything wrong with it (not there, bad owner, bad JSON,
an old version, the time over) is no approval: the guard refuses as it would without one. Plain Python 3.
"""
import base64, datetime as dt, hashlib, hmac, json, os, re, secrets, sys, tempfile

_kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_common = os.path.join(_kit, 'common')
sys.path.insert(0, _common if os.path.isfile(os.path.join(_common, 'machine_file.py'))
                else os.path.join(os.path.dirname(_kit), 'common'))
import machine_file  # noqa: E402

PATH = '/etc/fork-kit/agent-guards-approval.json'
OWNER_UID = 0   # tests only
VERSION = 1
MARK = '#owner-approved:actions-disable:'
CODE_RE = re.compile(r'#owner-approved:actions-disable:([a-z2-7]{20})(?![A-Za-z0-9])')
MAX_HOURS = 24


def _hash(code):
    return hashlib.sha256(code.encode('ascii')).hexdigest()


def _now(now):
    return now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()


def read(path=None, owner_uid=None):
    """The approval record {"version", "sha256", "until"}, or (None, why not)."""
    path = path or PATH
    try:
        text = machine_file.read_protected_text(path, OWNER_UID if owner_uid is None else owner_uid)
        data = json.loads(text)
    except machine_file.MachineFileError as e:
        return None, str(e)
    except ValueError as e:
        return None, f'{path}: not valid JSON ({e})'
    if not isinstance(data, dict) or data.get('version') != VERSION or set(data) != {'version', 'sha256', 'until'} \
            or not isinstance(data['sha256'], str) or not re.fullmatch(r'[0-9a-f]{64}', data['sha256']) \
            or type(data['until']) not in (int, float):
        return None, f'{path}: not an approval file of this kit (version {VERSION})'
    return data, None


def approved(cmd, now=None):
    """(True, None) when the command carries the code of a valid approval; else (False, why not or None when the
    command carries no code at all)."""
    codes = CODE_RE.findall(cmd or '') if isinstance(cmd, str) else []
    if not codes:
        return False, None
    data, why = read()
    if data is None:
        return False, why
    if _now(now) >= data['until']:
        return False, 'the approval is over (its time has passed)'
    if any(hmac.compare_digest(_hash(c), data['sha256']) for c in codes):
        return True, None
    return False, 'the code in the command is not the one the owner approved'


def new_code():
    """About 100 random bits as 20 letters a-z and digits 2-7 (easy to read out and type)."""
    return base64.b32encode(secrets.token_bytes(15)).decode('ascii').lower()[:20]


def write(code, hours, path=None, now=None):
    """Write the approval for `code`, ending in `hours`, atomically (temp file in the same folder, then rename)."""
    if not (0 < hours <= MAX_HOURS):
        raise ValueError(f'--hours must be above 0 and at most {MAX_HOURS}')
    path = path or PATH
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, 0o755, exist_ok=True)
    data = {'version': VERSION, 'sha256': _hash(code), 'until': int(_now(now) + hours * 3600)}
    fd, tmp = tempfile.mkstemp(prefix='.agent-guards-approval.', dir=folder)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f)
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return data


def revoke(path=None):
    """Remove the approval file; True when there was one."""
    try:
        os.unlink(path or PATH)
        return True
    except FileNotFoundError:
        return False
