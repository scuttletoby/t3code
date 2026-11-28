"""claudelogin.creds: what claude-login reads of a Claude Code credentials file: the login state and the expiry
fields, never a token or the file's text (fork-kit/claude-login/README.md, Secrets). read_regular is the one way
the kit reads a file: a regular file only, of a bounded size, never a FIFO that hangs or a link it did not expect."""
import errno, json, math, os, stat, time

READS = 3   # a credentials file being rewritten by a refresh is read again, up to this many times
CREDS_LIMIT = 1 << 16
MAX_MS = 10 ** 14   # a time in ms after this (the year 5138) is no time: bounds Infinity and 1e30 too


def read_regular(path, limit, follow=False):
    """The bytes of a regular file of at most `limit` bytes. Without `follow` a link is refused (ELOOP), also a
    dangling one; a FIFO, a device or a folder is refused without waiting on it. Raises OSError (FileNotFoundError
    when nothing is there)."""
    flags = os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC | (0 if follow else os.O_NOFOLLOW)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(errno.EINVAL, 'not a regular file')
        if st.st_size > limit:
            raise OSError(errno.EFBIG, 'too big')
        chunks, size = [], 0
        while True:
            chunk = os.read(fd, limit + 1 - size)
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                raise OSError(errno.EFBIG, 'too big')
        return b''.join(chunks)
    finally:
        os.close(fd)


def _no_constant(name):
    raise ValueError(f'{name} is not a number JSON allows')


def load_json(data):
    """JSON text as Python, refusing NaN and Infinity (which json.loads would take)."""
    return json.loads(data.decode('utf-8'), parse_constant=_no_constant)


def number(v):
    """A time in ms, or None: a finite number above 0 and below MAX_MS."""
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and 0 < v < MAX_MS
    return v if ok and math.isfinite(v) else None


def parse_creds(path, now=None):
    """(state, facts) of one read: state 'ok', 'ended' (its login's end has passed), 'signed_out' (no file, no login
    in it) or 'unreadable' (not JSON, half-written at this moment, too big, or not a regular file: a link, also a
    dangling one, or a FIFO). facts holds only expiresAt and end (refreshTokenExpiresAt), in ms, or None."""
    try:
        data = load_json(read_regular(path, CREDS_LIMIT))
    except FileNotFoundError:
        return 'signed_out', {}
    except (OSError, ValueError, RecursionError):
        return 'unreadable', {}
    o = data.get('claudeAiOauth') if isinstance(data, dict) else None
    if not isinstance(o, dict) or not o.get('refreshToken'):
        return ('signed_out' if isinstance(data, dict) else 'unreadable'), {}
    facts = {'expiresAt': number(o.get('expiresAt')), 'end': number(o.get('refreshTokenExpiresAt'))}
    now = time.time() * 1000 if now is None else now
    return ('ended' if facts['end'] and facts['end'] <= now else 'ok'), facts


def read_login(acct, cfg):
    """parse_creds, read again (up to READS times, readPause apart) while it is unreadable: a refresh rewrites the
    file, and a read at that moment must not count as a broken file or as no login."""
    for i in range(READS):
        state = parse_creds(acct.cred)
        if state[0] != 'unreadable':
            return state
        if i < READS - 1:
            time.sleep(cfg['readPause'])
    return state
