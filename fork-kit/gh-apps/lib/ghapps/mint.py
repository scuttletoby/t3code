"""gh-mint: makes a GitHub App installation token. Runs only as the keys user (ghkeys), started by gh-as through
sudo; agents cannot start it (their one sudoers rule names gh-as, not gh-mint).

  gh-mint --app NAME                    the token on stdout, nothing else (gh-as reads it from a pipe)
  gh-mint --app NAME --check            makes or reuses a token, prints only "ok" and its expiry on stderr
  gh-mint --app NAME --find-installation
                                        the app's installation id for the repo's owner on stdout (the admin
                                        command writes it into the root-owned app config)

The key: /etc/gh-apps/keys/NAME.pem, root:ghkeys, mode 640, in a root:ghkeys 750 folder. The token is limited to
the app's one repository and cached in /var/cache/gh-apps/NAME.token (ghkeys, 600) until 45 minutes before it ends.
The JWT is signed with /usr/bin/openssl (no Python packages needed). Requests never use a proxy or follow a redirect.
"""
import base64, contextlib, datetime as dt, fcntl, http.client, json, os, re, ssl, stat, subprocess, sys, tempfile
import time, urllib.error, urllib.request

from . import common as c
from .common import Refused

JWT_BACKDATE, JWT_LIFETIME = 60, 9 * 60
RENEW_BEFORE = 45 * 60
HTTP_TIMEOUT = 30
API_URL = c.API_URL        # tests only: a local fake
TLS_CONTEXT = None         # tests only: None = the system CA files


def note(msg):
    print(f'gh-mint: {c.redact(msg)}', file=sys.stderr)


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


def key_path(app):
    """The key file, checked: root-owned, group ghkeys, no access for others, in the root:ghkeys 750 folder."""
    folder, path = c.p(c.KEYS), c.p(f'{c.KEYS}/{app["name"]}.pem')
    gid = c.gid_of(c.KEYS_USER)
    for p_, what, want in ((folder, 'folder', 0o750), (path, 'key', 0o640)):
        try:
            st = os.lstat(p_)
        except FileNotFoundError:
            raise Refused(f'app {app["name"]}: no key ({c.KEYS}/{app["name"]}.pem); the owner adds it with '
                          f'gh-apps-admin add-key {app["name"]}') from None
        kind_ok = stat.S_ISDIR(st.st_mode) if what == 'folder' else stat.S_ISREG(st.st_mode)
        if not kind_ok or st.st_uid != c.ROOT_UID or st.st_gid != gid or stat.S_IMODE(st.st_mode) & ~want:
            raise Refused(f'app {app["name"]}: {p_} must be a {what} owned by root, group {c.KEYS_USER}, mode '
                          f'{want:o} or less; nothing was minted')
    return path


def make_jwt(app):
    path = key_path(app)
    now = int(time.time())
    head = b64url(json.dumps({'alg': 'RS256', 'typ': 'JWT'}, separators=(',', ':')).encode())
    body = b64url(json.dumps({'iat': now - JWT_BACKDATE, 'exp': now + JWT_LIFETIME, 'iss': str(app['app_id'])},
                             separators=(',', ':')).encode())
    signing = f'{head}.{body}'.encode()
    try:
        proc = subprocess.run([c.OPENSSL, 'dgst', '-sha256', '-sign', path], input=signing, capture_output=True,
                              env={'PATH': c.SAFE_PATH, 'LANG': 'C.UTF-8'}, timeout=30, cwd='/')
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f'app {app["name"]}: cannot run openssl ({type(e).__name__})') from None
    if proc.returncode != 0 or not proc.stdout:   # never openssl's message: it could quote the key file
        raise Refused(f'app {app["name"]}: cannot sign with its key (is it the app\'s RSA private key?)')
    return f'{head}.{body}.{b64url(proc.stdout)}'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def tls_context():
    if TLS_CONTEXT is not None:
        return TLS_CONTEXT
    paths = ssl.get_default_verify_paths()   # the built-in paths: SSL_CERT_FILE / SSL_CERT_DIR are not read
    cafile = paths.openssl_cafile if os.path.isfile(paths.openssl_cafile or '') else None
    capath = paths.openssl_capath if os.path.isdir(paths.openssl_capath or '') else None
    if not (cafile or capath):
        raise Refused('no system CA certificates found')
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.load_verify_locations(cafile=cafile, capath=capath)
    return ctx


def api(app, method, path, bearer, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(API_URL + path, data=data, method=method, headers={
        'Accept': 'application/vnd.github+json', 'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'gh-apps-mint',
        'Authorization': f'Bearer {bearer}', **({'Content-Type': 'application/json'} if data else {})})
    handlers = [urllib.request.ProxyHandler({}), NoRedirect()]
    if API_URL.startswith('https:'):
        handlers.append(urllib.request.HTTPSHandler(context=tls_context()))
    try:
        with urllib.request.build_opener(*handlers).open(req, timeout=HTTP_TIMEOUT) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            msg = str(json.loads(e.read() or b'{}').get('message', ''))
        except Exception:
            msg = ''
        hint = {401: ' (wrong app id, or the key is not this app\'s: was it rotated? the owner runs '
                     f'gh-apps-admin add-key {app["name"]})',
                404: ' (wrong installation id or app id?)', 422: ' (is the repository in the installation?)'}.get(
            e.code, ' (a redirect: not followed)' if 300 <= e.code < 400 else '')
        raise Refused(f'app {app["name"]}: {method} {path}: HTTP {e.code} {c.redact(msg)[:200]}{hint}') from None
    except (urllib.error.URLError, OSError, http.client.HTTPException) as e:
        raise Refused(f'app {app["name"]}: cannot reach {API_URL}: '
                      f'{c.redact(getattr(e, "reason", None) or type(e).__name__)}') from None
    try:
        return json.loads(raw)
    except ValueError:
        raise Refused(f'app {app["name"]}: {method} {path}: the answer is not JSON') from None


def mint(app):
    if not app['installation_id']:
        raise Refused(f'app {app["name"]} has no installation_id: the owner runs gh-apps-admin add-key again')
    name = app['repo'].split('/')[1]
    out = api(app, 'POST', f'/app/installations/{app["installation_id"]}/access_tokens', make_jwt(app),
              {'repositories': [name]})
    token = out.get('token') if isinstance(out, dict) else None
    try:
        expires = dt.datetime.fromisoformat(str(out['expires_at']).replace('Z', '+00:00')).timestamp()
    except Exception:
        expires = None
    if not isinstance(token, str) or not c.TOKEN_RE.match(token) or expires is None:
        raise Refused(f'app {app["name"]}: GitHub answered without a usable token or expiry')
    repos = out.get('repositories')
    if not (isinstance(repos, list) and len(repos) == 1 and isinstance(repos[0], dict)
            and str(repos[0].get('full_name', '')).lower() == app['repo'].lower()):
        raise Refused(f'app {app["name"]}: GitHub did not limit the token to exactly {app["repo"]}; not used')
    return {'token': token, 'expires_at': expires, 'app_id': app['app_id'],
            'installation_id': app['installation_id'], 'repo': app['repo']}


def cache_folder():
    folder = c.p(c.CACHE)
    try:
        st = os.lstat(folder)
    except FileNotFoundError:
        raise Refused(f'{c.CACHE} does not exist: is the gh-apps kit installed?') from None
    if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid() or stat.S_IMODE(st.st_mode) != 0o700:
        raise Refused(f'{c.CACHE} must be a folder of {c.KEYS_USER} with mode 700')
    return folder


def read_cache(app, path):
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid() or st.st_mode & 0o077:
        note(f'cache {path} is not a private file: replaced')
        return None
    try:
        with open(path, encoding='utf-8') as f:
            entry = json.load(f)
    except (OSError, ValueError):
        entry = None
    if not (isinstance(entry, dict) and isinstance(entry.get('token'), str) and c.TOKEN_RE.match(entry['token'])
            and isinstance(entry.get('expires_at'), (int, float)) and not isinstance(entry['expires_at'], bool)):
        note(f'cache {path} is corrupt: replaced')
        return None
    if (entry.get('app_id'), entry.get('installation_id'), str(entry.get('repo', '')).lower()) != \
            (app['app_id'], app['installation_id'], app['repo'].lower()):
        return None
    if entry['expires_at'] - time.time() < RENEW_BEFORE:
        return None
    return entry


def write_atomic(path, text):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix='.' + os.path.basename(path) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove_stale_tmp(path):
    want = re.compile(r'^\.' + re.escape(os.path.basename(path)) + r'\.[a-z0-9_]{8}\.tmp$')
    for name in os.listdir(os.path.dirname(path)):
        if want.match(name):
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(os.path.dirname(path), name))


def token_for(app):
    folder = cache_folder()
    lock = os.path.join(folder, app['name'] + '.lock')
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)   # agents starting together mint once; the kernel drops it on kill -9
        path = os.path.join(folder, app['name'] + '.token')
        remove_stale_tmp(path)
        entry = read_cache(app, path)
        if entry is None:
            entry = mint(app)
            write_atomic(path, json.dumps(entry))
        return entry
    finally:
        os.close(fd)


def find_installation(app):
    owner = app['repo'].split('/')[0]
    insts = api(app, 'GET', '/app/installations?per_page=100', make_jwt(app))
    if not isinstance(insts, list):
        raise Refused('GET /app/installations did not answer a list')
    hits = [i for i in insts if isinstance(i, dict) and isinstance(i.get('account'), dict)
            and str(i['account'].get('login', '')).lower() == owner.lower()]
    if len(hits) != 1:
        raise Refused(f'app {app["name"]} has {len(hits)} installations on {owner}; it must have exactly one '
                      f'(install it on {app["repo"]}, or pass --installation-id)')
    inst = hits[0].get('id')
    if not (isinstance(inst, int) and not isinstance(inst, bool) and inst > 0):
        raise Refused('the installation has no id')
    return inst


def main(argv):
    mode = None
    name = None
    it = iter(argv)
    for a in it:
        if a == '--app':
            name = next(it, None)
        elif a in ('--check', '--find-installation') and mode is None:
            mode = a
        else:
            print('usage: gh-mint --app NAME [--check | --find-installation]', file=sys.stderr)
            return 2
    try:
        c.require_user(c.KEYS_USER)
        app = c.load_app(name)
        if mode == '--find-installation':
            print(find_installation(app))
            return 0
        entry = token_for(app)
    except Refused as e:
        note(e)
        return 1
    except OSError as e:
        note(f'{e.strerror or type(e).__name__}: nothing was minted')
        return 1
    if mode == '--check':
        until = dt.datetime.fromtimestamp(entry['expires_at'], dt.timezone.utc).strftime('%H:%M UTC')
        note(f'ok: app {app["name"]} has a token for {app["repo"]} until {until}')
        return 0
    sys.stdout.write(entry['token'] + '\n')
    return 0
