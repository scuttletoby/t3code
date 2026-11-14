"""Shared parts of the gh-apps kit: fixed paths, the two service users, file checks, redaction.

Nothing here reads an environment variable or a command-line option to find a file: every path is fixed below.
Only the test suite changes PREFIX, ROOT_UID and FAKE_IDS, by setting them on the imported module.
"""
import grp, json, os, pwd, re, stat

PREFIX = ''          # tests only: every fixed path below is read under this folder
ROOT_UID = 0         # tests only: who must own the trusted files (root on a real host)
FAKE_IDS = None      # tests only: {user or group name: (uid, gid)} instead of the password database

RUN_USER = 'ghrun'   # runs gh-as and gh; cannot read the keys
KEYS_USER = 'ghkeys' # owns the token cache; its group may read the keys

ETC = '/etc/gh-apps'
CONFIG = ETC + '/config.json'
APPS = ETC + '/apps'
KEYS = ETC + '/keys'
INSTALLED = ETC + '/installed.tsv'
LIBEXEC = '/usr/local/libexec/gh-apps'
VERSIONS = LIBEXEC + '/versions'
CURRENT = LIBEXEC + '/current'
PRIV_AS = LIBEXEC + '/gh-as'
MINT = LIBEXEC + '/gh-mint'
WRAPPER = '/usr/local/bin/gh-as'
ADMIN = '/usr/local/sbin/gh-apps-admin'
VARLIB = '/var/lib/gh-apps'
RUN = VARLIB + '/run'
EMPTY = VARLIB + '/empty'
CACHE = '/var/cache/gh-apps'
SUDOERS = '/etc/sudoers.d/gh-apps'
SUDOERS_INCLUDE = '/etc/sudoers'
SUDO = '/usr/bin/sudo'
GIT = '/usr/bin/git'
OPENSSL = '/usr/bin/openssl'
DEFAULT_GH = '/usr/bin/gh'
SAFE_PATH = '/usr/sbin:/usr/bin:/sbin:/bin'
API_URL = 'https://api.github.com'
HOST = 'github.com'

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}\Z')
USER_RE = re.compile(r'^[a-z_][a-z0-9_-]{0,31}\Z')
REPO_RE = re.compile(r'^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})/[A-Za-z0-9._-]{1,100}\Z')
SHA_RE = re.compile(r'^[0-9a-f]{40}\Z')
TOKEN_RE = re.compile(r'^[A-Za-z0-9_.-]{20,255}\Z')
SECRET_RE = re.compile(r'(?:gh[pousr]_[A-Za-z0-9_]{10,}|github_pat_[A-Za-z0-9_]{10,}|'
                       r'eyJ[A-Za-z0-9_-]{5,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|v1\.[0-9a-f]{20,}|'
                       r'-----BEGIN[A-Z ]*PRIVATE KEY-----)')


class Refused(Exception):
    pass


def p(path):
    """A fixed path as it is on this host (tests read it under PREFIX)."""
    return PREFIX + path


def redact(text, *tokens):
    text = str(text)
    for t in tokens:
        if t:
            text = text.replace(t, '[redacted]')
    return SECRET_RE.sub('[redacted]', text)


def ids(name):
    """(uid, primary gid) of a user, or None."""
    if FAKE_IDS is not None:
        return FAKE_IDS.get(name)
    try:
        pw = pwd.getpwnam(name)
    except KeyError:
        return None
    return pw.pw_uid, pw.pw_gid


def gid_of(group):
    if FAKE_IDS is not None:
        v = FAKE_IDS.get(group)
        return v[1] if v else None
    try:
        return grp.getgrnam(group).gr_gid
    except KeyError:
        return None


def need_ids(name):
    v = ids(name)
    if v is None:
        raise Refused(f'user {name} does not exist: is the gh-apps kit installed? (gh-apps-admin self-check)')
    return v


def require_user(name):
    """The running process must be this service user (sudo -u NAME): never the agent, never root."""
    uid, _ = need_ids(name)
    if os.geteuid() != uid or os.getuid() != uid:
        raise Refused(f'this runs only as {name} (through sudo), not as uid {os.getuid()}')


def trusted_chain(path, what, file_owners=None):
    """Every folder from / down to path, and path itself, is owned by root and writable by no one else, both for the
    path as given (the folders that hold its links) and for its real path, which is returned. file_owners: other
    uids that may own the file itself."""
    real = os.path.realpath(path)
    parts = [real]
    for start in (os.path.dirname(real), os.path.dirname(os.path.abspath(path))):   # the link's own folders too
        d = start
        while True:
            parts.append(d)
            if d == os.path.dirname(d):
                break
            d = os.path.dirname(d)
    stop = os.path.realpath(PREFIX) if PREFIX else '/'
    for i, part in enumerate(parts):
        if PREFIX and not (part + os.sep).startswith(stop + os.sep):
            break   # tests: only the part under the test's folder is checked
        try:
            st = os.stat(part)
        except OSError:
            raise Refused(f'{what} {path} is missing') from None
        owners = (ROOT_UID,) + (tuple(file_owners) if i == 0 and file_owners else ())
        if st.st_uid not in owners or st.st_mode & 0o022:
            raise Refused(f'{what} {path}: {part} must be owned by root and writable by no one else')
    return real


def read_trusted_json(path, what):
    real = trusted_chain(path, what)
    st = os.lstat(real)
    if not stat.S_ISREG(st.st_mode):
        raise Refused(f'{what} {path} is not a regular file')
    try:
        with open(real, encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError) as e:
        raise Refused(f'{what} {path} is unreadable: {type(e).__name__}') from None


def load_config():
    path = p(CONFIG)
    if not os.path.lexists(path):
        raise Refused(f'{CONFIG} does not exist: the gh-apps kit is not installed')
    data = read_trusted_json(path, 'config')
    if not isinstance(data, dict) or data.get('version') != 1:
        raise Refused(f'{CONFIG}: not a version 1 gh-apps config')
    gh = data.get('gh_path', DEFAULT_GH)
    agents = data.get('agent_users')
    if not isinstance(gh, str) or not os.path.isabs(gh):
        raise Refused(f'{CONFIG}: gh_path must be an absolute path')
    if not (isinstance(agents, list) and agents and all(isinstance(a, str) and USER_RE.match(a) for a in agents)):
        raise Refused(f'{CONFIG}: agent_users must be a non-empty list of user names')
    return {'gh_path': gh, 'agent_users': agents}


def app_names():
    folder = p(APPS)
    try:
        names = sorted(os.listdir(folder))
    except FileNotFoundError:
        return []
    return [n[:-5] for n in names if n.endswith('.json') and NAME_RE.match(n[:-5])]


def load_app(name):
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise Refused(f'bad app name {name!r} (lower-case letters, digits, - and _)')
    path = p(f'{APPS}/{name}.json')
    if not os.path.lexists(path):
        raise Refused(f'no app {name!r} ({APPS}/{name}.json does not exist; the owner adds it with '
                      'gh-apps-admin add-key)')
    data = read_trusted_json(path, 'app config')
    if not isinstance(data, dict):
        raise Refused(f'{APPS}/{name}.json is not a JSON object')
    app_id, inst, repo = data.get('app_id'), data.get('installation_id'), data.get('repo')
    if not (isinstance(app_id, int) and not isinstance(app_id, bool) and app_id > 0):
        raise Refused(f'{APPS}/{name}.json: app_id must be a number')
    if inst is not None and not (isinstance(inst, int) and not isinstance(inst, bool) and inst > 0):
        raise Refused(f'{APPS}/{name}.json: installation_id must be a number')
    if not isinstance(repo, str) or not REPO_RE.match(repo):
        raise Refused(f'{APPS}/{name}.json: repo must be "owner/name"')
    return {'name': name, 'app_id': app_id, 'installation_id': inst, 'repo': repo}


def norm_repo(text):
    """'owner/name', 'github.com/owner/name' or a github.com remote URL -> 'owner/name'; anything else -> None."""
    if not isinstance(text, str):
        return None
    r = text.strip()
    host = HOST
    m = re.match(r'^(?:(?:https|ssh|git)://(?:[^@/]+@)?([^/]+)/|[^@/\s]+@([^:/\s]+):)(.+?)(?:\.git)?/?$', r)
    if m:
        host, r = (m.group(1) or m.group(2)).split(':')[0], m.group(3)
    parts = r.split('/')
    if len(parts) == 3:
        host, parts = parts[0], parts[1:]
    r = '/'.join(parts)
    if host.lower() != HOST:
        return None
    return r if REPO_RE.match(r) else None


def require_only_app(app):
    """One App per repository, so an agent can never pick a stronger one: refused when any other app file is for
    the same repository, or is broken, so that it could be (a broken file loads again once its drift is fixed)."""
    others = []
    for n in app_names():
        if n == app['name']:
            continue
        try:
            if load_app(n)['repo'].lower() == app['repo'].lower():
                others.append(n)
        except Refused:
            others.append(f'{n} (broken)')
    if others:
        raise Refused(f'app {app["name"]}: another app file is, or could be, for {app["repo"]} '
                      f'({", ".join(others)}); one app per repository, so gh-as refuses until the owner removes '
                      'or fixes it (gh-apps-admin list, remove-app)')


def app_for_repo(repo):
    want = norm_repo(repo)
    if not want:
        raise Refused(f'{repo!r} is not a github.com repository ("owner/name")')
    names = app_names()
    if not names:
        raise Refused('no GitHub App is set up on this machine (the owner adds one with gh-apps-admin add-key). '
                      'There is no fallback to another login')
    hits = []
    for n in names:
        app = load_app(n)
        if app['repo'].lower() == want.lower():
            hits.append(app)
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise Refused(f'{want}: more than one app is set up for it ({", ".join(a["name"] for a in hits)}); name one')
    raise Refused(f'no GitHub App is set up for {want}. There is no fallback to another login: ask the owner to '
                  'add one, or name a repository that has one')
