"""The exposure check's feeds: downloaded at most once a day into the cache folder, with a size cap, a timeout, the
server's ETag / Last-Modified (a feed that did not change is not downloaded again) and a sha256 of what was stored.

Per feed NAME the cache holds NAME.data and NAME.meta.json ({url, etag, lastModified, fetchedAt, sha256, size}).
A new download goes to a temporary file, is checked by the feed's own reader (a file it does not understand never
replaces a good copy; the reader returns how many entries it holds, and a download with fewer than FLOOR of the kept
copy's never replaces it either), then renamed into place, then the meta (with that count) is written. Redirects
are followed only to https. So a run stopped half-way leaves the old
copy (or none); a copy whose sha256 does not match its meta is no copy. What happened is returned as a problem
string, never raised: the caller turns it into a "cannot check" line.
"""
import datetime, hashlib, http.client, json, os, tempfile, urllib.error, urllib.request


UTC = datetime.timezone.utc
DAY = datetime.timedelta(hours=20)          # "once a day", with room for a timer that runs a little early
AGENT = 'update-policy-exposure (fork-kit)'
CHUNK = 1 << 16
FLOOR = 0.5


class _HttpsOnly(urllib.request.HTTPRedirectHandler):
    """A redirect to anything but https is refused (a feed is never read over plain http)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.lower().startswith('https://'):
            raise urllib.error.URLError(f'a redirect to {newurl.split("?")[0][:80]} (not https) was refused')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_OPENER = urllib.request.build_opener(_HttpsOnly)


SHRINK_DAYS = 3


class Fetched:
    """path: the copy to read (None: none); problem: why it is not today's (None: fine); fetchedAt: when the copy
    was last confirmed by the server; notice: something to say although all is fine (a shrink accepted)."""

    def __init__(self, path, problem, fetched_at, notice=None):
        self.path, self.problem, self.fetched_at, self.notice = path, problem, fetched_at, notice


def _counts(value):
    """A reader's count (a number, or {part: number}) as {part: number}."""
    if isinstance(value, dict):
        return {str(k): int(v) for k, v in value.items()}
    return {'entries': int(value)}


def _shrunk(new, kept):
    """The parts of `new` under FLOOR of `kept`'s: [(part, new, kept)]."""
    if not isinstance(kept, (int, dict)):
        return []
    kept = _counts(kept)
    return [(k, new.get(k, 0), v) for k, v in sorted(kept.items()) if new.get(k, 0) < FLOOR * v]


def urlopen(url, headers, timeout):
    """(status, headers dict lower-case, readable). 304 is a status, not an error."""
    req = urllib.request.Request(url, headers=dict(headers, **{'User-Agent': AGENT}))
    if not url.lower().startswith('https://'):
        raise urllib.error.URLError('only https is read')
    try:
        r = _OPENER.open(req, timeout=timeout)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, {k.lower(): v for k, v in e.headers.items()}, None
        raise
    return r.status, {k.lower(): v for k, v in r.headers.items()}, r


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(CHUNK), b''):
            h.update(block)
    return h.hexdigest()


def _parse_time(text):
    try:
        t = datetime.datetime.fromisoformat(str(text).replace('Z', '+00:00'))
        return t if t.tzinfo else None
    except ValueError:
        return None


def _meta(cache, name):
    try:
        with open(os.path.join(cache, f'{name}.meta.json'), encoding='utf-8') as f:
            meta = json.load(f)
        return meta if isinstance(meta, dict) else {}
    except (OSError, ValueError):
        return {}


def _good_copy(cache, name, meta):
    path = os.path.join(cache, f'{name}.data')
    try:
        return path if meta.get('sha256') and _sha256(path) == meta['sha256'] else None
    except OSError:
        return None


def _write_json(path, data):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.meta.', suffix='.tmp')
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, sort_keys=True)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def fetch(cache, name, url, check, now, max_bytes, timeout, opener=urlopen):
    """The feed NAME from URL into CACHE. `check(path)` returns how many entries the file holds, and raises on a file
    it does not understand (the problem says why). Returns Fetched."""
    meta = _meta(cache, name)
    if meta.get('url') != url:
        meta = {}                                       # another URL: an old copy is no copy of this feed
    copy = _good_copy(cache, name, meta)
    fetched_at = _parse_time(meta.get('fetchedAt')) if copy else None
    if copy and fetched_at and fetched_at <= now < fetched_at + DAY:
        return Fetched(copy, None, fetched_at)          # once a day
    headers = {}
    if copy and meta.get('etag'):
        headers['If-None-Match'] = meta['etag']
    if copy and meta.get('lastModified'):
        headers['If-Modified-Since'] = meta['lastModified']
    tmp = body = None
    try:
        status, rh, body = opener(url, headers, timeout)
        if status == 304 and copy:
            meta['fetchedAt'] = now.isoformat()
            _write_json(os.path.join(cache, f'{name}.meta.json'), meta)
            return Fetched(copy, None, now)
        if status != 200 or body is None:
            raise OSError(f'HTTP {status}')
        if int(rh.get('content-length') or 0) > max_bytes:
            return Fetched(copy, f'{name}: larger than the cap of {max_bytes >> 20} MB', fetched_at)
        fd, tmp = tempfile.mkstemp(dir=cache, prefix=f'.{name}.', suffix='.tmp')
        size = 0
        with os.fdopen(fd, 'wb') as out:
            while True:
                block = body.read(CHUNK)
                if not block:
                    break
                size += len(block)
                if size > max_bytes:
                    return Fetched(copy, f'{name}: larger than the cap of {max_bytes >> 20} MB', fetched_at)
                out.write(block)
            out.flush()
            os.fsync(out.fileno())
        try:
            entries = _counts(check(tmp))
        except Exception as e:      # noqa: BLE001 - any reader failure means: not understood
            return Fetched(copy, f'{name}: the download is in a format this check does not understand ({e})',
                           fetched_at)
        notice = None
        shrunk = _shrunk(entries, meta.get('count')) if copy else []
        if shrunk:
            # a lasting shrink: the same smaller counts on SHRINK_DAYS days in a row is accepted, and said
            today, seen = now.date().isoformat(), meta.get('shrink') or {}
            yesterday = (now.date() - datetime.timedelta(days=1)).isoformat()
            if seen.get('counts') == entries and seen.get('last') in (today, yesterday):
                days = seen.get('days', 1) + (seen.get('last') == yesterday)
            else:
                days = 1
            what = ', '.join(f'{k} {n} under half of {v}' for k, n, v in shrunk)
            if days < SHRINK_DAYS:
                meta['shrink'] = {'counts': entries, 'last': today, 'days': days}
                _write_json(os.path.join(cache, f'{name}.meta.json'), meta)
                return Fetched(copy, f'{name}: the download holds {what} of the kept copy: kept the old copy (day '
                                     f'{days} of {SHRINK_DAYS}; the same smaller feed {SHRINK_DAYS} days in a row is '
                                     'accepted)', fetched_at)
            notice = (f'{name}: the feed shrank ({what}) and stayed so {SHRINK_DAYS} days in a row: accepted, the '
                      'smaller copy is in use')
        path = os.path.join(cache, f'{name}.data')
        os.replace(tmp, path)
        tmp = None
        meta = {'url': url, 'etag': rh.get('etag'), 'lastModified': rh.get('last-modified'),
                'fetchedAt': now.isoformat(), 'sha256': _sha256(path), 'size': size, 'count': entries}
        _write_json(os.path.join(cache, f'{name}.meta.json'), meta)
        return Fetched(path, None, now, notice)
    except (OSError, ValueError, http.client.HTTPException) as e:
        # the server's or the error's text: the caller makes every line one safe line
        return Fetched(copy, f'{name}: download failed ({getattr(e, "reason", None) or e})', fetched_at)
    finally:
        if body is not None and hasattr(body, 'close'):
            body.close()
        if tmp and os.path.exists(tmp):
            os.unlink(tmp)


def sweep(cache):
    """Temporary files a killed run left (SIGTERM runs no `finally`). Call under the cache's lock."""
    for name in os.listdir(cache):
        if name.startswith('.') and name.endswith('.tmp'):
            try:
                os.unlink(os.path.join(cache, name))
            except OSError:
                pass
