"""agent_guards.approval: the owner's approval of ONE refused actions-disable-guard command, as a root-owned file.

The owner (root) runs `sudo agent-guards approve [--hours N] [--cwd DIR] --command '<the exact command>'`. That
makes a random code, prints it once, and keeps in PATH (root-owned, mode 0644, written whole and renamed into
place) only the code's SHA-256, the SHA-256 of the normalised command, the folder it runs in and an end time. The
command passes the guard when it is that command (marker taken off its end, runs of blanks as one), it ends with
`#owner-approved:actions-disable:<code>`, it runs in that folder, the end time has not come, and the approval was
not used before: the first pass records its use (a file named by the code's hash in the guard's state folder,
made with O_EXCL; a second hook run for the same tool call, with the same tool_use_id, passes too). The approval
is then used up. `sudo agent-guards approve --revoke` removes the file.

Why bound to the command: the code travels in the command text, so it lands in the session's transcript, the app's
database and `ps`, which every agent of the same user can read. Bound to one command in one folder, a code seen
there opens nothing else: no other workflow, repo or act. The use record is in the agents' user's own folder (the
hook runs as that user and cannot write a root file), so an agent that sets out to can remove it; that lets it
repeat only the act the owner approved, in that folder, until the end time.

Why a file and not an environment variable: a variable set when a session starts is inherited by every process of
that session, a nested `claude -p` or `codex exec` too. An agent cannot write the file (it is root's, in a
root-owned folder), so it cannot approve itself. Who can become root without a password (sudo NOPASSWD, the docker
group) can write it: `agent-guards self-check` warns about that.

The file is read under the machine file's rules (fork-kit/common/machine_file.py: every folder on the way and the
file root-owned, not writable by group or others, no link). Anything wrong with it (not there, bad owner, bad JSON,
an older version such as the version 1 file of an earlier draft, the time over) is no approval: the guard refuses
as it would without one. Plain Python 3.
"""
import base64, datetime as dt, hashlib, hmac, json, os, re, secrets, sys, tempfile

_kit = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
_common = os.path.join(_kit, 'common')
sys.path.insert(0, _common if os.path.isfile(os.path.join(_common, 'machine_file.py'))
                else os.path.join(os.path.dirname(_kit), 'common'))
import machine_file  # noqa: E402

PATH = '/etc/fork-kit/agent-guards-approval.json'
OWNER_UID = 0   # tests only
VERSION = 2   # 1: an earlier draft, not bound to a command (counts as no approval)
MARK = '#owner-approved:actions-disable:'
CODE_RE = re.compile(r'#owner-approved:actions-disable:([a-z2-7]{20})(?![A-Za-z0-9])')
END_RE = re.compile(r'(?:^|(?<=\s))#owner-approved:actions-disable:([a-z2-7]{20})\s*\Z')   # the marker ends the command
KEYS = {'version', 'sha256', 'command', 'cwd', 'until'}
MAX_HOURS = 24


def _hash(code):
    return hashlib.sha256(code.encode('ascii')).hexdigest()


def _now(now):
    return now if now is not None else dt.datetime.now(dt.timezone.utc).timestamp()


def read(path=None, owner_uid=None):
    """The approval record {"version", "sha256", "command", "cwd", "until"}, or (None, why not)."""
    path = path or PATH
    try:
        text = machine_file.read_protected_text(path, OWNER_UID if owner_uid is None else owner_uid)
        data = json.loads(text)
    except machine_file.MachineFileError as e:
        return None, str(e)
    except ValueError as e:
        return None, f'{path}: not valid JSON ({e})'
    if not isinstance(data, dict) or data.get('version') != VERSION or set(data) != KEYS \
            or not all(isinstance(data[k], str) and re.fullmatch(r'[0-9a-f]{64}', data[k]) for k in ('sha256', 'command')) \
            or not isinstance(data['cwd'], str) or not os.path.isabs(data['cwd']) \
            or type(data['until']) not in (int, float):
        return None, f'{path}: not an approval file of this kit (version {VERSION})'
    return data, None


def normalise(cmd):
    """The command the owner approves or an agent runs, as it is compared: a marker at its end taken off, runs of
    blanks (spaces, tabs, line breaks) as one space, no blanks at either end."""
    m = END_RE.search(cmd)
    return ' '.join((cmd[:m.start()] if m else cmd).split())


def command_hash(cmd):
    return hashlib.sha256(normalise(cmd).encode('utf-8')).hexdigest()


def approved(cmd, cwd=None, now=None, use_id=None, used_dir=None):
    """(True, None) when the command is the one a valid approval names, ends with its code, runs in its folder, and
    the approval was not used before (its use is then recorded in used_dir: the approval is used up); else (False,
    why not, or None when the command carries no code at all). use_id: the tool call's id (a second hook run for
    the same call passes too); used_dir: the folder of use records (None: the use cannot be recorded, so no pass)."""
    if not isinstance(cmd, str) or not CODE_RE.search(cmd):
        return False, None
    data, why = read()
    if data is None:
        return False, why
    if _now(now) >= data['until']:
        return False, 'the approval is over (its time has passed)'
    m = END_RE.search(cmd)
    if not m:
        return False, 'the marker must end the command (the code is for one exact command, with nothing after it)'
    if not hmac.compare_digest(_hash(m.group(1)), data['sha256']):
        return False, 'the code in the command is not the one the owner approved'
    if not hmac.compare_digest(command_hash(cmd), data['command']):
        return False, ('the approval is for another command: it passes only the exact command the owner approved '
                       '(`sudo agent-guards approve --command ...`)')
    if os.path.realpath(cwd or '') != os.path.realpath(data['cwd']):
        return False, f'the approval is for a command run in {data["cwd"]}, not in {cwd}'
    return use(data['sha256'], use_id, used_dir)


def use(code_sha, use_id, used_dir):
    """Record the one use of an approval: a file named by the code's hash, made whole (a temp file, then a link,
    which fails when the name is taken). (True, None) for the first use, or for the same tool call again."""
    if not used_dir:
        return False, 'the guard has no state folder to record the use of the approval in (stateDir)'
    me = (use_id if isinstance(use_id, str) and use_id else 'none-' + secrets.token_hex(8))[:200]
    path = os.path.join(used_dir, f'approval-used-{code_sha[:32]}')
    tmp = None
    try:
        os.makedirs(used_dir, 0o700, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.approval-used.', dir=used_dir)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(me)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp, path)
            return True, None
        except FileExistsError:
            with open(path, encoding='utf-8', errors='replace') as f:
                if f.read() == me:
                    return True, None
            return False, 'the approval was used already (it is for one run of one command): ask the owner again'
    except OSError as e:
        return False, f'the use of the approval cannot be recorded in {used_dir} ({e.strerror or e})'
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def new_code():
    """About 100 random bits as 20 letters a-z and digits 2-7 (easy to read out and type)."""
    return base64.b32encode(secrets.token_bytes(15)).decode('ascii').lower()[:20]


def write(code, hours, command, cwd, path=None, now=None):
    """Write the approval of `command` run in `cwd` for `code`, ending in `hours`, atomically (temp file in the
    same folder, then rename). The approval before it, used or not, is gone with it."""
    if not (0 < hours <= MAX_HOURS):
        raise ValueError(f'--hours must be above 0 and at most {MAX_HOURS}')
    if not isinstance(command, str) or not normalise(command):
        raise ValueError('the command to approve is empty')
    if not isinstance(cwd, str) or not os.path.isabs(cwd):
        raise ValueError('the folder the command runs in must be an absolute path')
    path = path or PATH
    folder = os.path.dirname(path)
    if not os.path.isdir(folder):
        os.makedirs(folder, 0o755, exist_ok=True)
    data = {'version': VERSION, 'sha256': _hash(code), 'command': command_hash(command),
            'cwd': os.path.realpath(cwd), 'until': int(_now(now) + hours * 3600)}
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
