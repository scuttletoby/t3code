"""claudelogin.creds: what claude-login reads of a Claude Code credentials file: the login state and the expiry
fields, never a token or the file's text (fork-kit/claude-login/README.md, Secrets)."""
import json, time

READS = 3   # a credentials file being rewritten by a refresh is read again, up to this many times


def number(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else None


def parse_creds(path):
    """(state, facts) of one read: state 'ok', 'signed_out' (no file, no login in it) or 'unreadable' (not JSON, or
    half-written at this moment). facts holds only expiresAt and end (refreshTokenExpiresAt), in ms, or None."""
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return 'signed_out', {}
    except (OSError, ValueError):
        return 'unreadable', {}
    o = data.get('claudeAiOauth') if isinstance(data, dict) else None
    if not isinstance(o, dict) or not o.get('refreshToken'):
        return ('signed_out' if isinstance(data, dict) else 'unreadable'), {}
    return 'ok', {'expiresAt': number(o.get('expiresAt')), 'end': number(o.get('refreshTokenExpiresAt'))}


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
