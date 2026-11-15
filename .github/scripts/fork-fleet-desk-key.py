#!/usr/bin/env python3
"""Fork only: the desk link's key tool, on the always-on host.

Desk agents log in as one dedicated, unprivileged desk-link user (USER, or --user), never as root. Run this as that
user (`sudo -u fleet-desk -H python3 -I ...`): it refuses root, any other user, and a ~/.ssh, authorized_keys or (to
make ~/.ssh) HOME that user does not own. Make the user with no password and a shell that can run a command (sshd
runs the forced command through the login shell, so nologin would close the link); `restrict` and the forced
command are what limit it.

Each desk pool (a pool of fork-fleet.json whose launch runs DESK `launch` for itself) has at most one line in that
user's ~/.ssh/authorized_keys: its desktop's ed25519 key, `restrict` (no pty, no forwarding of any kind) and forced
to SERVE POOL. This tool writes and removes only those lines, which end in the comment `fork-fleet-desk:POOL`;
every other byte of the file stays as it was.

  python3 -I fork-fleet-desk-key.py [--config FILE] [--user NAME] add POOL < KEY.pub
      Adds POOL's line for the one ssh-ed25519 public key on stdin (its comment is dropped). Already there: nothing
      changes. Refused, file untouched: not exactly one valid ed25519 public key; a pool that is not a desk pool;
      POOL already has another line (both are shown: `remove` first); the key is on any other line (sshd uses the
      first line a key matches, so that line would decide what the key may do).
  python3 -I fork-fleet-desk-key.py [--config FILE] [--user NAME] status [POOL]
      POOL's line, or each desk pool's. Exit 0 every one is the exact line this version writes, 3 one has none,
      1 one is wrong (another command, two lines, or its key also on another line): `remove`, then `add`.
  python3 -I fork-fleet-desk-key.py [--config FILE] [--user NAME] remove POOL
      Removes POOL's lines. None: nothing changes.

Writes go to a temporary file in ~/.ssh with the file's mode (a new file: 0600), then a rename, under a lock on
~/.ssh; a file another program changed meanwhile is left alone (run again). A symlinked ~/.ssh or authorized_keys
is refused. Exit 1 on any refusal. Standard library, POSIX. Runbook: .github/fork-fleet.md.
"""
import argparse, base64, fcntl, glob, hashlib, importlib.util, os, pwd, re, stat, struct, sys, tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('fork_fleet', os.path.join(HERE, 'fork-fleet.py'))
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)

# The desk link's program and its forced command: .github/scripts/fork-fleet-desk.py (row D2) owns both. Change
# them only together with that file; `status` then shows every old line as wrong.
DESK = '/opt/fork-fleet/fork-fleet-desk.py'
SERVE = f'python3 -I {DESK} serve'
USER = 'fleet-desk'   # the desk-link user's default name (D2's serve runs as it too)
KEY_TYPE = 'ssh-ed25519'
MARK = 'fork-fleet-desk:'
TMP = '.authorized_keys.desk-key.'
MAX_KEY = 16384
B64 = re.compile(r'[A-Za-z0-9+/]+={0,2}')
TYPE_WORD = re.compile(r'(ssh|ecdsa|sk)-[a-z0-9@.-]+')   # a key type in the comment: a second key on the line


class Refused(Exception):
    pass


def desk_pools(cfg):
    """Names of the pools whose launch command runs the desk program's `launch` for that pool."""
    out = []
    for p in cfg['pools']:
        cmd = p.get('launch') or []
        if any(os.path.basename(x) == os.path.basename(DESK) and cmd[i + 1:i + 3] == ['launch', p['name']]
               for i, x in enumerate(cmd)):
            out.append(p['name'])
    return out


def parse_key(raw):
    """The base64 blob of the one ssh-ed25519 public key in `raw` (bytes). Never echoes the input."""
    if len(raw) > MAX_KEY:
        raise Refused('the key is too long for one public key')
    text = raw.decode('ascii', 'replace')
    if 'PRIVATE KEY' in text:
        raise Refused('that is a private key: give the .pub file, and make a new key pair if it was shared')
    text = text[:-1] if text.endswith('\n') else text
    text = text[:-1] if text.endswith('\r') else text
    if not text.strip() or any(ord(c) < 32 or ord(c) == 127 for c in text.replace('\t', ' ')) or '\ufffd' in text:
        raise Refused('give exactly one public key on one line')
    fields = text.split()
    if len(fields) < 2 or fields[0] != KEY_TYPE or any(TYPE_WORD.fullmatch(w) for w in fields[2:]):
        raise Refused(f'give one {KEY_TYPE} public key ("{KEY_TYPE} AAAA... comment"), no options in front')
    blob = fields[1]
    try:
        data = base64.b64decode(blob, validate=True) if B64.fullmatch(blob) else b''
    except ValueError:
        data = b''
    kind = KEY_TYPE.encode()
    if (len(data) != 4 + len(kind) + 4 + 32 or base64.b64encode(data).decode() != blob
            or data[:4 + len(kind) + 4] != struct.pack('>I', len(kind)) + kind + struct.pack('>I', 32)):
        raise Refused(f'that is not a valid {KEY_TYPE} public key')
    return blob


def check_user(name):
    euid = os.geteuid()
    if euid == 0:
        raise Refused(f'never as root: the desk link key goes to the desk-link user ({name}): sudo -u {name} -H ...')
    try:
        me = pwd.getpwuid(euid).pw_name
    except KeyError:
        me = None
    if me != name:
        raise Refused(f'run this as the desk-link user {name!r}, not {me!r}: sudo -u {name} -H ...')


def owned(path, what):
    if os.stat(path, follow_symlinks=False).st_uid != os.geteuid():
        raise Refused(f'{path} ({what}) does not belong to this user')


def line_for(pool, blob):
    return f'restrict,command="{SERVE} {pool}" {KEY_TYPE} {blob} {MARK}{pool}'


def fingerprint(blob):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip('=')


def text_of(line):
    return line.decode('utf-8', 'surrogateescape').rstrip('\r\n')


def active(line):
    t = text_of(line).strip()
    return bool(t) and not t.startswith('#')


def marked(line, pool):
    return active(line) and text_of(line).split()[-1] == MARK + pool


def blob_of(line):
    """The key blob of a desk line this tool would have written, or None."""
    words = text_of(line).split()
    return words[-2] if len(words) >= 4 and words[-3] == KEY_TYPE else None


def elsewhere(lines, blob, pool):
    return [ln for ln in lines if active(ln) and blob in text_of(ln).split() and not marked(ln, pool)]


class Keys:
    """~/.ssh/authorized_keys, locked while open."""

    def __init__(self, make):
        self.dir = os.path.join(os.path.expanduser('~'), '.ssh')
        self.path = os.path.join(self.dir, 'authorized_keys')
        if make and not os.path.lexists(self.dir):
            owned(os.path.realpath(os.path.dirname(self.dir)), 'HOME')
            os.mkdir(self.dir, 0o700)
        if os.path.islink(self.dir) or os.path.lexists(self.dir) and not os.path.isdir(self.dir):
            raise Refused(f'{self.dir} is a symlink or not a directory')
        if os.path.isdir(self.dir):
            owned(self.dir, '~/.ssh')
        self.fd = os.open(self.dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW) if os.path.isdir(self.dir) else None
        try:
            if self.fd is not None:
                fcntl.flock(self.fd, fcntl.LOCK_EX)
            self.st, self.raw = self.read()
        except BaseException:
            self.close()
            raise

    def read(self):
        if self.fd is None or not os.path.lexists(self.path):
            return None, b''
        st = os.lstat(self.path)
        if not stat.S_ISREG(st.st_mode):
            raise Refused(f'{self.path} is a symlink or not a regular file')
        owned(self.path, 'authorized_keys')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            return st, f.read()

    def lines(self):
        return self.raw.splitlines(keepends=True)

    def write(self, data):
        for old in glob.glob(os.path.join(glob.escape(self.dir), TMP + '*')):   # left by a run that stopped half-way
            os.unlink(old)
        fd, tmp = tempfile.mkstemp(prefix=TMP, dir=self.dir)
        try:
            os.fchmod(fd, stat.S_IMODE(self.st.st_mode) if self.st else 0o600)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            if self.read()[1] != self.raw:
                raise Refused(f'{self.path} changed while this ran: nothing written, run again')
            os.replace(tmp, self.path)
            os.fsync(self.fd)
        except BaseException:
            if os.path.lexists(tmp):
                os.unlink(tmp)
            raise

    def close(self):
        if self.fd is not None:
            os.close(self.fd)


def add(keys, pool, blob):
    want = line_for(pool, blob)
    lines = keys.lines()
    mine = [ln for ln in lines if marked(ln, pool)]
    other = elsewhere(lines, blob, pool)
    if [text_of(ln) for ln in mine] == [want] and not other:
        print(f'desk-key: {pool}: already there ({fingerprint(blob)})')
        return 0
    if other:
        raise Refused('this key is already on another line, which sshd would use for it:'
                      + ''.join(f'\n  {text_of(ln)}' for ln in other))
    if mine:
        shown = ''.join(f'\n  has:  {text_of(ln)}' for ln in mine)
        raise Refused(f'{pool} already has a line (remove it first):{shown}\n  want: {want}')
    end = b'' if not keys.raw or keys.raw.endswith(b'\n') else b'\n'
    keys.write(keys.raw + end + want.encode() + b'\n')
    print(f'desk-key: {pool}: added ({fingerprint(blob)})')
    return 0


def remove(keys, pool):
    lines = keys.lines()
    keep = [ln for ln in lines if not marked(ln, pool)]
    if len(keep) == len(lines):
        print(f'desk-key: {pool}: no line, nothing to remove')
        return 0
    keys.write(b''.join(keep))
    print(f'desk-key: {pool}: removed {len(lines) - len(keep)} line(s)')
    return 0


def status(keys, pools):
    codes = [0]
    lines = keys.lines()
    for pool in pools:
        mine = [ln for ln in lines if marked(ln, pool)]
        blob = blob_of(mine[0]) if len(mine) == 1 else None
        if not mine:
            print(f'desk-key: {pool}: no line')
            codes.append(3)
        elif blob and text_of(mine[0]) == line_for(pool, blob) and not elsewhere(lines, blob, pool):
            print(f'desk-key: {pool}: ok ({fingerprint(blob)})')
        else:
            why = 'two or more lines' if len(mine) > 1 else 'not the line this version writes, or its key is on ' \
                                                            'another line too'
            print(f'desk-key: {pool}: wrong ({why}); remove, then add:' + ''.join(f'\n  {text_of(ln)}' for ln in mine))
            codes.append(1)
    return 1 if 1 in codes else max(codes)


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(prog='desk-key', description=__doc__.split('\n')[0])
    ap.add_argument('--config', default=ff.CONFIG)
    ap.add_argument('--user', default=USER, help=f'the desk-link user this must run as (default {USER})')
    ap.add_argument('verb', choices=('add', 'status', 'remove'))
    ap.add_argument('pool', nargs='?')
    args = ap.parse_args(argv)
    keys = None
    try:
        known = desk_pools(ff.load(args.config))
        if args.pool is None and args.verb != 'status':
            raise Refused(f'{args.verb} needs a POOL')
        if args.pool is not None and args.pool not in known:
            raise Refused(f'{args.pool!r} is not a desk pool of {args.config} '
                          f'(desk pools: {", ".join(known) or "none"})')
        blob = parse_key((stdin or sys.stdin.buffer).read(MAX_KEY + 1)) if args.verb == 'add' else None
        check_user(args.user)
        keys = Keys(make=args.verb == 'add')
        if args.verb == 'add':
            return add(keys, args.pool, blob)
        if args.verb == 'remove':
            return remove(keys, args.pool)
        if not (args.pool or known):
            print(f'desk-key: no desk pools in {args.config}')
        return status(keys, [args.pool] if args.pool else known)
    except (Refused, OSError, ValueError) as e:
        print(f'desk-key: refused: {e}', file=sys.stderr)
        return 1
    finally:
        if keys:
            keys.close()


if __name__ == '__main__':
    sys.exit(main())
