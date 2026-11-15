#!/usr/bin/env python3
"""Fork only: the desk link's key tool, on the always-on host. Doc: .github/fork-fleet-desk-key.md.

Desk agents log in as one dedicated, unprivileged desk-link user (USER, or --user), never as root. Run this as that
user (`sudo -u fleet-desk -H /usr/bin/python3 -I ...`). It refuses root; any other user; a user in an admin group
(ADMIN_GROUPS); a system account (uid below 1000) other than the default name; a $HOME that is not the user's home
in the password database (sshd reads that one); and a ~/.ssh, authorized_keys or (to make ~/.ssh) home that user does
not own. The user needs a shell that can run a command (sshd runs the forced command through it, so nologin would
close the link); `restrict` and the forced command are what limit it.

Each desk pool (a pool of fork-fleet.json whose launch runs DESK `launch` for itself) has at most one line in that
user's ~/.ssh/authorized_keys: its desktop's ed25519 key, `restrict` (no pty, no forwarding of any kind) and forced
to SERVE POOL. This tool writes and removes only those lines, which end in the comment `fork-fleet-desk:POOL`;
every other byte of the file stays as it was. Lines are split at \\n only, as sshd does.

  desk-key [--config FILE] [--user NAME] add POOL < KEY.pub
      Adds POOL's line for the one ssh-ed25519 public key on stdin (its comment is dropped). Already there: nothing
      changes. Refused, file untouched: not exactly one valid ed25519 public key; a pool that is not a desk pool;
      POOL already has another line (both are shown: `remove` first); the key is on any other line (sshd uses the
      first line a key matches, so that line would decide what the key may do).
  desk-key [--config FILE] [--user NAME] status [POOL]
      POOL's line, or each desk pool's, and every line of a pool the config no longer has (a retired pool).
  desk-key [--config FILE] [--user NAME] remove POOL
      Removes POOL's lines, also when POOL has left the config (a retired desktop). None: nothing changes.
(desk-key = /usr/bin/python3 -I fork-fleet-desk-key.py)

Writes go to a temporary file in ~/.ssh with the file's mode (a new file: 0600), then a rename, under a lock on
~/.ssh; a file another program changed meanwhile is left alone (run again). A symlinked ~/.ssh or authorized_keys
is refused. Standard library, POSIX.
"""
import argparse, base64, fcntl, glob, grp, hashlib, importlib.util, os, pwd, re, stat, struct, sys, tempfile

sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
_spec = importlib.util.spec_from_file_location('fork_fleet', os.path.join(HERE, 'fork-fleet.py'))
ff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ff)

# The desk link's program and its forced command: .github/scripts/fork-fleet-desk.py (row D2) owns both. Change
# them only together with that file; `status` then shows every old line as wrong. An absolute interpreter: sshd's
# PATH for the forced command is not this tool's to trust.
PYTHON = '/usr/bin/python3'
DESK = '/opt/fork-fleet/fork-fleet-desk.py'
SERVE = f'{PYTHON} -I {DESK} serve'
USER = 'fleet-desk'   # the desk-link user's default name (D2's serve runs as it too)
ADMIN_GROUPS = frozenset(('root', 'sudo', 'wheel', 'admin', 'adm', 'docker', 'lxd', 'libvirt', 'systemd-journal'))
SYSTEM_UID_MAX = 999  # a uid at or below this is a system account: only USER may be one
KEY_TYPE = 'ssh-ed25519'
MARK = 'fork-fleet-desk:'
TMP = '.authorized_keys.desk-key.'
MAX_KEY = 16384
B64 = re.compile(r'[A-Za-z0-9+/]+={0,2}')
TYPE_WORD = re.compile(r'(ssh|ecdsa|sk)-[a-z0-9@.-]+')   # a key type in the comment: a second key on the line
CTRL = re.compile(r'[\x00-\x08\x0a-\x1f\x7f]')            # control bytes but tab: sshd and this tool may disagree
LINE = re.compile(rb'[^\n]*\n|[^\n]+')                     # sshd splits at \n only; a lone \r stays in its line

# Exit codes (also in --help and the doc).
OK, PROBLEM, REFUSED, MISSING, ERROR = 0, 1, 2, 3, 4
CODES = f'''exit codes:
  {OK}  done; status: every line is the exact line this version writes
  {PROBLEM}  status or remove found a problem: a wrong line (run remove, then add), a line of a retired pool (run
     remove POOL), or a line naming a pool that this tool cannot read (edit it by hand)
  {REFUSED}  refused, nothing changed: bad input, unknown pool, wrong user, unsafe file, bad config, bad usage
  {MISSING}  status: a desk pool has no line (and nothing else is wrong)
  {ERROR}  error: an unexpected failure (for example a full disk); the file is the old one or the new one, never half
'''


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
    if not text.strip() or any(ord(c) < 32 or ord(c) == 127 for c in text.replace('\t', ' ')) or '�' in text:
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


def group_names(pw):
    out = set()
    for gid in os.getgrouplist(pw.pw_name, pw.pw_gid):
        try:
            out.add(grp.getgrgid(gid).gr_name)
        except KeyError:
            pass
    return out


def check_user(name):
    """The desk-link user's home (from the password database), or Refused."""
    euid = os.geteuid()
    if euid == 0:
        raise Refused(f'never as root: the desk link key goes to the desk-link user ({name}): sudo -u {name} -H ...')
    try:
        pw = pwd.getpwuid(euid)
    except KeyError:
        pw = None
    if pw is None or pw.pw_name != name:
        raise Refused(f'run this as the desk-link user {name!r}, not {pw and pw.pw_name!r}: sudo -u {name} -H ...')
    if euid <= SYSTEM_UID_MAX and name != USER:
        raise Refused(f'{name} is a system account (uid {euid}): of those, only the default desk-link user {USER} '
                      'may hold the link (doc: the desk-link user)')
    admin = sorted(group_names(pw) & ADMIN_GROUPS)
    if admin:
        raise Refused(f'{name} is in the admin group(s) {", ".join(admin)}: the desk link needs its own '
                      'unprivileged user')
    home = os.environ.get('HOME')
    if home and os.path.realpath(home) != os.path.realpath(pw.pw_dir):
        raise Refused(f'HOME is {home}, but sshd reads {name}\'s home {pw.pw_dir}: run with sudo -u {name} -H')
    return pw.pw_dir


def owned(path, what):
    if os.stat(path, follow_symlinks=False).st_uid != os.geteuid():
        raise Refused(f'{path} ({what}) does not belong to this user')


def strict(path, what):
    if os.stat(path, follow_symlinks=False).st_mode & 0o022:
        raise Refused(f'{path} ({what}) is writable by group or others: sshd (StrictModes) would not use it')


def line_for(pool, blob):
    return f'restrict,command="{SERVE} {pool}" {KEY_TYPE} {blob} {MARK}{pool}'


def fingerprint(blob):
    return 'SHA256:' + base64.b64encode(hashlib.sha256(base64.b64decode(blob)).digest()).decode().rstrip('=')


def text_of(line):
    t = line.decode('utf-8', 'surrogateescape')
    t = t[:-1] if t.endswith('\n') else t
    return t[:-1] if t.endswith('\r') else t


def active(line):
    t = text_of(line).strip()
    return bool(t) and not t.startswith('#')


def fields(line):
    """(options, key type, blob, comment) as sshd reads the line; None for a blank or # line, or one with a control
    byte inside (sshd and this tool could read that one differently, so it is never one of this tool's lines)."""
    t = text_of(line).lstrip(' \t')
    if not t or t.startswith('#') or CTRL.search(t):
        return None
    opts = ''
    if not TYPE_WORD.fullmatch(re.split(r'[ \t]', t, maxsplit=1)[0]):   # options first: they end at a space outside ""
        quoted, i = False, 0
        while i < len(t) and (quoted or t[i] not in ' \t'):
            if t[i] == '\\' and t[i + 1:i + 2] == '"':
                i += 1
            elif t[i] == '"':
                quoted = not quoted
            i += 1
        opts, t = t[:i], t[i:].lstrip(' \t')
    parts = re.split(r'[ \t]+', t, maxsplit=2)
    if len(parts) < 2:
        return None
    return opts, parts[0], parts[1], (parts[2] if len(parts) > 2 else '').rstrip(' \t')


def mark_of(line):
    """The pool of a desk line (key type ssh-ed25519, comment exactly fork-fleet-desk:POOL), or None."""
    f = fields(line)
    name = f[3][len(MARK):] if f and f[1] == KEY_TYPE and f[3].startswith(MARK) else None
    return name if name and ff.POOL.fullmatch(name) else None


def marked(line, pool):
    return mark_of(line) == pool


def blob_of(line):
    """The key blob of a desk line, or None."""
    f = fields(line)
    return f[2] if f and mark_of(line) else None


def names(line, pool=None):
    """True when an active line names fork-fleet-desk:POOL (any pool when None) without being a desk line."""
    pat = re.escape(MARK + pool) + r'(?![a-z0-9_-])' if pool else re.escape(MARK)
    return active(line) and mark_of(line) is None and re.search(pat, text_of(line)) is not None


def elsewhere(lines, blob, pool):
    return [ln for ln in lines if active(ln) and blob in text_of(ln).split() and not marked(ln, pool)]


class Keys:
    """~/.ssh/authorized_keys of the home `home`, locked while open."""

    def __init__(self, home, make):
        self.dir = os.path.join(home, '.ssh')
        self.path = os.path.join(self.dir, 'authorized_keys')
        if make and not os.path.lexists(self.dir):
            owned(os.path.realpath(home), 'home')
            try:
                os.mkdir(self.dir, 0o700)
            except FileExistsError:   # another run made it meanwhile: the checks below apply to it as to any
                pass
        if os.path.islink(self.dir) or os.path.lexists(self.dir) and not os.path.isdir(self.dir):
            raise Refused(f'{self.dir} is a symlink or not a directory')
        if os.path.isdir(self.dir):
            owned(self.dir, '~/.ssh')
            strict(self.dir, '~/.ssh')
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
        strict(self.path, 'authorized_keys')
        fd = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, 'rb') as f:
            return st, f.read()

    def lines(self):
        return LINE.findall(self.raw)

    def write(self, data):
        # Under the lock, every temp file there is from an earlier run that stopped half-way: remove it. Anything
        # else with that name (a directory: never made by this tool) is skipped, so it can never block a write.
        for old in glob.glob(os.path.join(glob.escape(self.dir), TMP + '*')):
            try:
                if not stat.S_ISDIR(os.lstat(old).st_mode):
                    os.unlink(old)
            except FileNotFoundError:
                pass
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
        return OK
    if other:
        raise Refused('this key is already on another line, which sshd would use for it:'
                      + ''.join(f'\n  {text_of(ln)}' for ln in other))
    if mine:
        shown = ''.join(f'\n  has:  {text_of(ln)}' for ln in mine)
        raise Refused(f'{pool} already has a line (remove it first):{shown}\n  want: {want}')
    end = b'' if not keys.raw or keys.raw.endswith(b'\n') else b'\n'
    keys.write(keys.raw + end + want.encode() + b'\n')
    print(f'desk-key: {pool}: added ({fingerprint(blob)})')
    return OK


def unreadable(lines, pool=None):
    """Lines that name a desk pool but are not desk lines this tool can manage: shown, never touched."""
    odd = [ln for ln in lines if names(ln, pool)]
    for ln in odd:
        print(f'desk-key: a line names {MARK}{pool or "..."} but is not one this tool can read or remove; '
              f'edit it by hand:\n  {text_of(ln)!r}')
    return odd


def remove(keys, pool):
    lines = keys.lines()
    keep = [ln for ln in lines if not marked(ln, pool)]
    if len(keep) == len(lines):
        print(f'desk-key: {pool}: no line, nothing to remove')
    else:
        keys.write(b''.join(keep))
        print(f'desk-key: {pool}: removed {len(lines) - len(keep)} line(s)')
    return PROBLEM if unreadable(lines, pool) else OK


def status(keys, pools, known, only=None):
    codes = [OK]
    lines = keys.lines()
    for pool in pools:
        mine = [ln for ln in lines if marked(ln, pool)]
        blob = blob_of(mine[0]) if len(mine) == 1 else None
        if pool not in known:
            if mine:
                print(f'desk-key: key for retired pool {pool} still allows a login: run desk-key remove {pool}'
                      + ''.join(f'\n  {text_of(ln)}' for ln in mine))
                codes.append(PROBLEM)
        elif not mine:
            print(f'desk-key: {pool}: no line')
            codes.append(MISSING)
        elif blob and text_of(mine[0]) == line_for(pool, blob) and not elsewhere(lines, blob, pool):
            print(f'desk-key: {pool}: ok ({fingerprint(blob)})')
        else:
            why = 'two or more lines' if len(mine) > 1 else 'not the line this version writes, or its key is on ' \
                                                            'another line too'
            print(f'desk-key: {pool}: wrong ({why}); remove, then add:' + ''.join(f'\n  {text_of(ln)}' for ln in mine))
            codes.append(PROBLEM)
    if unreadable(lines, only):
        codes.append(PROBLEM)
    return PROBLEM if PROBLEM in codes else max(codes)


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(prog='desk-key', description=__doc__.split('\n\n', 1)[1],
                                 epilog=CODES, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--config', default=ff.CONFIG)
    ap.add_argument('--user', default=USER, help=f'the desk-link user this must run as (default {USER})')
    ap.add_argument('verb', choices=('add', 'status', 'remove'))
    ap.add_argument('pool', nargs='?')
    args = ap.parse_args(argv)   # bad usage: exit 2, as any refusal
    keys = None
    try:
        if args.pool is None and args.verb != 'status':
            raise Refused(f'{args.verb} needs a POOL')
        if args.pool is not None and not ff.POOL.fullmatch(args.pool):
            raise Refused(f'{args.pool[:40]!r} is not a pool name')
        try:
            known = desk_pools(ff.load(args.config))
        except (OSError, ValueError) as e:
            raise Refused(f'no valid fleet config: {e}') from None
        if args.verb == 'add' and args.pool not in known:
            raise Refused(f'{args.pool!r} is not a desk pool of {args.config} '
                          f'(desk pools: {", ".join(known) or "none"})')
        blob = parse_key((stdin or sys.stdin.buffer).read(MAX_KEY + 1)) if args.verb == 'add' else None
        keys = Keys(check_user(args.user), make=args.verb == 'add')
        if args.pool is not None and args.pool not in known and not any(
                marked(ln, args.pool) or names(ln, args.pool) for ln in keys.lines()):
            raise Refused(f'{args.pool!r} is not a desk pool of {args.config} and has no line '
                          f'(desk pools: {", ".join(known) or "none"})')
        if args.verb == 'add':
            return add(keys, args.pool, blob)
        if args.verb == 'remove':
            return remove(keys, args.pool)
        retired = sorted({mark_of(ln) for ln in keys.lines() if mark_of(ln)} - set(known))
        if not (args.pool or known or retired):
            print(f'desk-key: no desk pools in {args.config}')
        return status(keys, [args.pool] if args.pool else known + retired, known, args.pool)
    except Refused as e:
        print(f'desk-key: refused: {e}', file=sys.stderr)
        return REFUSED
    except Exception as e:   # noqa: BLE001 - one plain line and its own exit code, not a traceback
        print(f'desk-key: error: {type(e).__name__}: {e}', file=sys.stderr)
        return ERROR
    finally:
        if keys:
            keys.close()


if __name__ == '__main__':
    sys.exit(main())
