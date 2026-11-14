"""gh-apps-install: installs, upgrades, checks and removes the gh-apps kit on a Linux host. Runs as root.

  gh-apps-install install --source DIR --sha SHA --agent-user USER [--gh-path /usr/bin/gh]
  gh-apps-install uninstall
  gh-apps-install self-check

Usually started by bootstrap.sh, which first checks that SHA is a commit on the fork's protected release branch.
Safe to run again. The steps, in this order (an early stop leaves the old tool working and nothing new reachable;
no step leaves a key readable by an agent or a broken sudoers file):

  users    the run user (ghrun) and keys user (ghkeys): system users, no login, no home
  folders  root-owned folders; keys root:ghkeys 750; cache ghkeys 700; run folders ghrun 700; config.json
  tool     the code in versions/<sha> (copied to a temporary folder, hashed, renamed), then the link current
           switched in one step, then the fixed links gh-as and gh-mint
  selftest the installed files against their hashes, and one test token for each app already set up
  sudoers  one file, checked with visudo -cf before it is moved into place
  wrapper  /usr/local/bin/gh-as
  cleanup  each agent's old copies (~/.cache/gh-apps, ~/.local/bin/gh-as and gh-app-token), removed as that agent;
           old keys in ~/.config/gh-apps count as leaked: they are not imported, only reported
  record   /etc/gh-apps/installed.tsv: every path, kind, owner, mode and hash (what self-check reads)

Uninstall removes the sudoers file first, then everything the record and the fixed layout name, keys included,
and the two users.
"""
import contextlib, grp, hashlib, json, os, pwd, re, shutil, stat, subprocess, sys, tempfile

from . import common as c
from .common import Refused

STEPS = ('users', 'folders', 'tool', 'selftest', 'sudoers', 'wrapper', 'cleanup', 'record')
VISUDO = '/usr/sbin/visudo'
USERADD, USERDEL, GROUPDEL = '/usr/sbin/useradd', '/usr/sbin/userdel', '/usr/sbin/groupdel'
NOLOGIN = '/usr/sbin/nologin'
SOURCE_FILES = ('bin/gh-as', 'libexec/gh-as', 'libexec/gh-mint', 'sbin/gh-apps-admin', 'sbin/gh-apps-install',
                'lib/ghapps/__init__.py', 'lib/ghapps/common.py', 'lib/ghapps/policy.py', 'lib/ghapps/mint.py',
                'lib/ghapps/run.py', 'lib/ghapps/wrapper.py', 'lib/ghapps/install.py', 'lib/ghapps/admin.py',
                'README.md')
EXECUTABLE = {'bin/gh-as', 'libexec/gh-as', 'libexec/gh-mint', 'sbin/gh-apps-admin', 'sbin/gh-apps-install'}
OLD_CACHE = ('.cache/gh-apps',)
OLD_TOOLS = ('.local/bin/gh-as', '.local/bin/gh-app-token')
OLD_KEYS = '.config/gh-apps'


def say(msg):
    print(f'gh-apps-install: {msg}', file=sys.stderr)


def layout():
    """(path, owner, group, mode) of every folder the kit owns, parents first."""
    R, K = c.RUN_USER, c.KEYS_USER
    return [(c.ETC, 'root', 'root', 0o755), (c.APPS, 'root', 'root', 0o755), (c.KEYS, 'root', K, 0o750),
            (c.LIBEXEC, 'root', 'root', 0o755), (c.VERSIONS, 'root', 'root', 0o755),
            (c.VARLIB, 'root', 'root', 0o755), (c.RUN, R, R, 0o700), (c.EMPTY, 'root', 'root', 0o555),
            (c.CACHE, K, K, 0o700)]


def links():
    """(link, target) of the fixed links; targets inside the kit are relative where they can be."""
    return [(c.PRIV_AS, 'current/libexec/gh-as'), (c.MINT, 'current/libexec/gh-mint'),
            (c.ADMIN, c.p(c.LIBEXEC) + '/current/sbin/gh-apps-admin'),
            (c.WRAPPER, c.p(c.LIBEXEC) + '/current/bin/gh-as')]


class Ops:
    """Everything that touches the system's users or runs a system tool. Tests use a fake subclass."""

    def user_exists(self, name):
        return c.ids(name) is not None

    def add_system_user(self, name):
        subprocess.run([USERADD, '--system', '--user-group', '--no-create-home', '--home-dir', '/nonexistent',
                        '--shell', NOLOGIN, name], check=True, env={'PATH': c.SAFE_PATH}, stdin=subprocess.DEVNULL)

    def del_user(self, name):
        if c.ids(name) is not None:
            subprocess.run([USERDEL, name], check=True, env={'PATH': c.SAFE_PATH}, stdin=subprocess.DEVNULL)
        if c.gid_of(name) is not None:
            subprocess.run([GROUPDEL, name], check=True, env={'PATH': c.SAFE_PATH}, stdin=subprocess.DEVNULL)

    def groups_of(self, user):
        pw = pwd.getpwnam(user)
        return {grp.getgrgid(g).gr_name for g in os.getgrouplist(user, pw.pw_gid) if _gr(g)}

    def home_of(self, user):
        return pwd.getpwnam(user).pw_dir

    def chown(self, path, owner, group):
        uid = c.need_ids(owner)[0]
        gid = c.gid_of(group)
        if gid is None:
            raise Refused(f'group {group} does not exist')
        os.chown(path, uid, gid, follow_symlinks=False)

    def visudo_ok(self, path):
        args = [VISUDO, '-c', '-q'] + (['-f', path] if path else [])
        return subprocess.run(args, env={'PATH': c.SAFE_PATH}, stdin=subprocess.DEVNULL,
                              capture_output=True).returncode == 0

    def run_as(self, user, cmd):
        """Run cmd as user (its own groups only), clean environment, from /. Returns (code, stdout, stderr)."""
        pw = pwd.getpwnam(user)
        groups = [g for g in os.getgrouplist(user, pw.pw_gid)]
        proc = subprocess.run(cmd, user=pw.pw_uid, group=pw.pw_gid, extra_groups=groups, cwd='/',
                              env={'PATH': c.SAFE_PATH, 'LANG': 'C.UTF-8', 'HOME': pw.pw_dir},
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=180)
        return proc.returncode, proc.stdout.decode('utf-8', 'replace'), proc.stderr.decode('utf-8', 'replace')


def _gr(gid):
    try:
        grp.getgrgid(gid)
        return True
    except KeyError:
        return False


# ---------- small file helpers (root writes only into root-owned folders)

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for block in iter(lambda: f.read(1 << 16), b''):
            h.update(block)
    return h.hexdigest()


def ensure_dir(ops, path, owner, group, mode):
    real = c.p(path)
    try:
        st = os.lstat(real)
        if not stat.S_ISDIR(st.st_mode):
            raise Refused(f'{path} exists and is not a folder (a link or file): remove it by hand and run again')
    except FileNotFoundError:
        os.makedirs(os.path.dirname(real), mode=0o755, exist_ok=True)
        os.mkdir(real, 0o700)
    ops.chown(real, owner, group)
    os.chmod(real, mode)


def write_file(ops, path, text, owner='root', group='root', mode=0o644):
    """Atomic: a temporary file in the same folder, owner and mode set, then renamed over the old one."""
    real = c.p(path)
    folder = os.path.dirname(real)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.' + os.path.basename(real) + '.', suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        ops.chown(tmp, owner, group)
        os.chmod(tmp, mode)
        os.replace(tmp, real)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def switch_link(path, target):
    """Point path at target in one step (a new link renamed over the old one). A folder there is refused."""
    real = c.p(path)
    if os.path.lexists(real) and not os.path.islink(real):
        raise Refused(f'{path} exists and is not a link: remove it by hand (an old copy?) and run again')
    if os.path.islink(real) and os.readlink(real) == target:
        return
    os.makedirs(os.path.dirname(real), mode=0o755, exist_ok=True)
    tmp = f'{real}.new-link'
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    os.symlink(target, tmp)
    os.replace(tmp, real)


# ---------- the version folder

def read_manifest(folder):
    out = {}
    with open(os.path.join(folder, 'MANIFEST.sha256'), encoding='utf-8') as f:
        for line in f:
            digest, _, rel = line.rstrip('\n').partition('  ')
            out[rel] = digest
    return out


def tree_problems(folder):
    """What differs between a version folder and its MANIFEST: changed, missing or extra files, or a file or
    folder that is not root-owned or that others may write."""
    problems = []
    try:
        want = read_manifest(folder)
    except OSError as e:
        return [f'{folder}: no readable MANIFEST.sha256 ({e.strerror})']
    seen = set()
    for dirpath, dirnames, filenames in os.walk(folder):
        for name in dirnames + filenames:
            path = os.path.join(dirpath, name)
            st = os.lstat(path)
            if st.st_uid != c.ROOT_UID or st.st_mode & 0o022 or stat.S_ISLNK(st.st_mode):
                problems.append(f'{path}: not root-owned, writable by others, or a link')
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), folder)
            if rel == 'MANIFEST.sha256':
                continue
            seen.add(rel)
            if rel not in want:
                problems.append(f'{rel}: not in the manifest')
            elif sha256_file(os.path.join(dirpath, name)) != want[rel]:
                problems.append(f'{rel}: changed since install')
    problems += [f'{rel}: missing' for rel in sorted(set(want) - seen)]
    return problems


def copy_tool(ops, source, sha):
    for rel in SOURCE_FILES:
        if not os.path.isfile(os.path.join(source, rel)):
            raise Refused(f'--source {source}: {rel} is missing (point it at the fork-kit/gh-apps folder)')
    versions = c.p(c.VERSIONS)
    dest = os.path.join(versions, sha)
    if os.path.lexists(dest):
        if not os.path.islink(dest) and os.path.isdir(dest) and not tree_problems(dest) \
                and set(read_manifest(dest)) == set(SOURCE_FILES):
            return dest
        say(f'versions/{sha} is incomplete or changed: made again')
        shutil.rmtree(dest) if not os.path.islink(dest) else os.unlink(dest)
    for name in os.listdir(versions):   # a run that stopped while copying
        if name.startswith('.tmp-'):
            shutil.rmtree(os.path.join(versions, name), ignore_errors=True)
    tmp = tempfile.mkdtemp(prefix=f'.tmp-{sha[:12]}-', dir=versions)
    lines = []
    for rel in sorted(SOURCE_FILES):
        src, dst = os.path.join(source, rel), os.path.join(tmp, rel)
        os.makedirs(os.path.dirname(dst), mode=0o755, exist_ok=True)
        with open(src, 'rb') as f:
            data = f.read()
        with open(dst, 'wb') as f:
            f.write(data)
        os.chmod(dst, 0o755 if rel in EXECUTABLE else 0o644)
        lines.append(f'{hashlib.sha256(data).hexdigest()}  {rel}')
    with open(os.path.join(tmp, 'MANIFEST.sha256'), 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')
    os.chmod(os.path.join(tmp, 'MANIFEST.sha256'), 0o644)
    for dirpath, dirnames, filenames in os.walk(tmp):
        for name in dirnames:
            os.chmod(os.path.join(dirpath, name), 0o755)
        for name in dirnames + filenames:
            ops.chown(os.path.join(dirpath, name), 'root', 'root')
    os.chmod(tmp, 0o755)
    ops.chown(tmp, 'root', 'root')
    os.rename(tmp, dest)
    return dest


# ---------- sudoers

def sudoers_text(agents):
    for a in agents:
        if not c.USER_RE.match(a):
            raise Refused(f'bad agent user name {a!r}')
    defaults = f'env_reset, !setenv, secure_path="{c.SAFE_PATH}"'
    return (f'# gh-apps kit: written by gh-apps-install. Do not edit: run the installer again.\n'
            f'Defaults!{c.PRIV_AS} {defaults}\n'
            f'Defaults!{c.MINT} {defaults}\n'
            + ''.join(f'{a} ALL=({c.RUN_USER}) NOPASSWD: {c.PRIV_AS}\n' for a in agents)
            + f'{c.RUN_USER} ALL=({c.KEYS_USER}) NOPASSWD: {c.MINT}\n')


INCLUDEDIR_RE = re.compile(r'^\s*[#@]includedir\s+/etc/sudoers\.d\s*$', re.M)


def place_sudoers(ops, agents):
    main_file = c.p(c.SUDOERS_INCLUDE)
    try:
        with open(main_file, encoding='utf-8') as f:
            if not INCLUDEDIR_RE.search(f.read()):
                raise Refused(f'{c.SUDOERS_INCLUDE} does not include /etc/sudoers.d; add "@includedir '
                              '/etc/sudoers.d" with visudo and run again')
    except OSError as e:
        raise Refused(f'cannot read {c.SUDOERS_INCLUDE}: {e.strerror}') from None
    text = sudoers_text(agents)
    real = c.p(c.SUDOERS)
    if os.path.islink(real):
        raise Refused(f'{c.SUDOERS} is a link: remove it by hand')
    if os.path.isfile(real):
        with open(real, encoding='utf-8') as f:
            if f.read() == text:
                return
    new = real + '.new'   # sudo never reads a file whose name has a dot in it
    with contextlib.suppress(FileNotFoundError):
        os.unlink(new)
    fd = os.open(new, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o440)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    ops.chown(new, 'root', 'root')
    os.chmod(new, 0o440)
    if not ops.visudo_ok(new):
        os.unlink(new)
        raise Refused('the new sudoers file did not pass visudo -cf; nothing was placed')
    os.replace(new, real)
    if not ops.visudo_ok(None):
        os.unlink(real)
        raise Refused('sudo\'s whole configuration did not pass visudo -c with the new file; it was removed again')


# ---------- the install record and self-check

def record_rows(sha):
    rows = []

    def add(path, kind, owner, group, mode, check):
        rows.append('\t'.join([path, kind, owner, group, f'{mode:o}', check]))
    for path, owner, group, mode in layout():
        add(path, 'dir', owner, group, mode, '-')
    add(c.CONFIG, 'file', 'root', 'root', 0o644, sha256_file(c.p(c.CONFIG)))
    version = f'{c.VERSIONS}/{sha}'
    add(version, 'tree', 'root', 'root', 0o755, sha256_file(c.p(version) + '/MANIFEST.sha256'))
    add(c.CURRENT, 'link', 'root', 'root', 0o777, f'versions/{sha}')
    for link, target in links():
        add(link, 'link', 'root', 'root', 0o777, target)
    add(c.SUDOERS, 'file', 'root', 'root', 0o440, sha256_file(c.p(c.SUDOERS)))
    return rows


def self_check():
    """Problems found (an empty list is a pass). Works as root and as an agent (an agent cannot read the sudoers
    file, so its hash is checked only by root)."""
    path = c.p(c.INSTALLED)
    try:
        c.trusted_chain(path, 'install record')
        with open(path, encoding='utf-8') as f:
            rows = [line.rstrip('\n').split('\t') for line in f if line.strip() and not line.startswith('#')]
    except (OSError, Refused) as e:
        return [f'{c.INSTALLED}: {e} (is the kit installed? run the installer)']
    problems = []
    for row in rows:
        if len(row) != 6:
            problems.append(f'{c.INSTALLED}: a bad row')
            continue
        rel, kind, owner, group, mode, check = row
        real = c.p(rel)
        try:
            st = os.lstat(real)
        except FileNotFoundError:
            problems.append(f'{rel}: missing')
            continue
        uid, gid = c.ids(owner)[0] if c.ids(owner) else None, c.gid_of(group)
        if owner == 'root':
            uid = c.ROOT_UID
        want_kind = {'dir': stat.S_ISDIR, 'tree': stat.S_ISDIR, 'file': stat.S_ISREG, 'link': stat.S_ISLNK}[kind]
        if not want_kind(st.st_mode):
            problems.append(f'{rel}: not a {kind}')
            continue
        if kind == 'link':
            if os.readlink(real) != check:
                problems.append(f'{rel}: points to {os.readlink(real)}, not {check}')
            continue
        if st.st_uid != uid or (group != 'root' and st.st_gid != gid) or f'{stat.S_IMODE(st.st_mode):o}' != mode:
            problems.append(f'{rel}: owner, group or mode is not {owner}:{group} {mode}')
        if kind == 'file':
            try:
                if sha256_file(real) != check:
                    problems.append(f'{rel}: changed since install')
            except PermissionError:
                pass   # the sudoers file, read as an agent
        if kind == 'tree':
            problems += [f'{rel}: {p_}' for p_ in tree_problems(real)]
            try:
                if sha256_file(real + '/MANIFEST.sha256') != check:
                    problems.append(f'{rel}/MANIFEST.sha256: changed since install')
            except OSError:
                pass
    for path in (c.WRAPPER, c.PRIV_AS, c.MINT):
        try:
            c.trusted_chain(c.p(path), 'installed program')
        except Refused as e:
            problems.append(str(e))
    return problems


def self_check_main():
    problems = self_check()
    if problems:
        for p_ in problems:
            print(f'gh-apps self-check: {p_}', file=sys.stderr)
        return 1
    real = os.path.realpath(c.p(c.CURRENT))
    print(f'gh-apps self-check: ok (version {os.path.basename(real)}; {len(c.app_names())} app(s) set up)')
    return 0


# ---------- install / uninstall

def check_inputs(ops, source, sha, agent, gh_path):
    if os.geteuid() != c.ROOT_UID:
        raise Refused('run this as root')
    if not c.SHA_RE.match(sha or ''):
        raise Refused('--sha must be the 40-character release commit')
    if not os.path.isdir(source or ''):
        raise Refused('--source must be the fork-kit/gh-apps folder of that release')
    if not (isinstance(agent, str) and c.USER_RE.match(agent)) or agent in ('root', c.RUN_USER, c.KEYS_USER):
        raise Refused(f'--agent-user {agent!r}: must be the agents\' own user, not root or a gh-apps user')
    ids = c.ids(agent)
    if ids is None:
        raise Refused(f'--agent-user {agent}: no such user')
    if ids[0] == 0 or ids[0] == c.ROOT_UID:
        raise Refused(f'--agent-user {agent}: is root')
    if not os.path.isabs(gh_path) or os.path.basename(gh_path) != 'gh':
        raise Refused('--gh-path must be the absolute path of gh')
    c.trusted_chain(c.p(gh_path), 'gh')


def install(source, sha, agent, gh_path=c.DEFAULT_GH, ops=None, stop_after=None):
    ops = ops or Ops()
    check_inputs(ops, source, sha, agent, gh_path)

    def done(step):
        if stop_after == step:
            say(f'stopped after step {step} (as asked)')
            return True
        return False

    # users
    for name in (c.RUN_USER, c.KEYS_USER):
        if not ops.user_exists(name):
            ops.add_system_user(name)
            say(f'made system user {name}')
    if ops.groups_of(agent) & {c.RUN_USER, c.KEYS_USER}:
        raise Refused(f'{agent} is in group {c.RUN_USER} or {c.KEYS_USER}: take it out, then run again')
    if done('users'):
        return
    # folders and config
    for path, owner, group, mode in layout():
        ensure_dir(ops, path, owner, group, mode)
    old = {}
    with contextlib.suppress(Refused):
        old = c.load_config() if os.path.lexists(c.p(c.CONFIG)) else {}
    agents = sorted(set(old.get('agent_users', [])) | {agent})
    write_file(ops, c.CONFIG, json.dumps({'version': 1, 'gh_path': gh_path, 'agent_users': agents}, indent=2) + '\n')
    if done('folders'):
        return
    # tool
    copy_tool(ops, source, sha)
    switch_link(c.CURRENT, f'versions/{sha}')
    for link, target in links()[:3]:   # gh-as, gh-mint (sudo's fixed paths) and the root-only admin command
        switch_link(link, target)
    if done('tool'):
        return
    # self-test
    problems = tree_problems(os.path.realpath(c.p(c.CURRENT)))
    for path in (c.PRIV_AS, c.MINT):
        try:
            c.trusted_chain(c.p(path), 'installed program')
        except Refused as e:
            problems.append(str(e))
    if problems:
        raise Refused('self-test failed, sudoers not placed: ' + '; '.join(problems))
    apps = c.app_names()
    for name in apps:
        code, _out, err = ops.run_as(c.KEYS_USER, [c.p(c.MINT), '--app', name, '--check'])
        if code != 0:
            raise Refused(f'self-test: app {name} gets no token, sudoers not placed: {c.redact(err).strip()}')
    if not apps:
        say('no app set up yet: gh-as will refuse until the owner runs gh-apps-admin add-key (owner step)')
    if done('selftest'):
        return
    place_sudoers(ops, agents)
    if done('sudoers'):
        return
    switch_link(*links()[3])
    if done('wrapper'):
        return
    for user in agents:
        cleanup_old(ops, user)
    if done('cleanup'):
        return
    rows = record_rows(sha)
    write_file(ops, c.INSTALLED, '# path\tkind\towner\tgroup\tmode\tsha256 or link target\n'
               + '\n'.join(rows) + '\n')
    print(f'gh-apps installed: release {sha}')
    for line in open(os.path.join(c.p(c.VERSIONS), sha, 'MANIFEST.sha256'), encoding='utf-8'):
        print('  ' + line.rstrip('\n'))


def cleanup_old(ops, user):
    try:
        home = ops.home_of(user)
    except KeyError:
        return
    for rel in OLD_CACHE:
        path = os.path.join(home, rel)
        if os.path.lexists(path):
            ops.run_as(user, ['/bin/rm', '-rf', '--', path])
            say(f'removed the old token cache {path}')
    for rel in OLD_TOOLS:
        path = os.path.join(home, rel)
        if os.path.lexists(path):
            ops.run_as(user, ['/bin/rm', '-f', '--', path])
            say(f'removed the old copy {path}')
    path = os.path.join(home, OLD_KEYS)
    if os.path.lexists(path):
        say(f'{path} holds old App keys the agent could read: they count as leaked and were NOT imported. Make a '
            'new key for each App, delete the old ones on GitHub, then delete this folder.')


def uninstall(ops=None):
    ops = ops or Ops()
    if os.geteuid() != c.ROOT_UID:
        raise Refused('run this as root')
    for path in (c.SUDOERS, c.SUDOERS + '.new'):   # first: nothing reachable any more
        with contextlib.suppress(FileNotFoundError):
            os.unlink(c.p(path))
    for link, _ in links():
        if os.path.islink(c.p(link)):
            os.unlink(c.p(link))
    for path in (c.LIBEXEC, c.VARLIB, c.CACHE, c.ETC):
        real = c.p(path)
        if os.path.islink(real):
            os.unlink(real)
        elif os.path.isdir(real):
            shutil.rmtree(real)
    for name in (c.RUN_USER, c.KEYS_USER):
        ops.del_user(name)
    say('removed: sudoers file, programs, links, config, keys, token cache, run folders and the two users')


def main(argv):
    import argparse
    ap = argparse.ArgumentParser(prog='gh-apps-install', allow_abbrev=False)
    sub = ap.add_subparsers(dest='cmd', required=True)
    i = sub.add_parser('install', allow_abbrev=False)
    i.add_argument('--source', required=True)
    i.add_argument('--sha', required=True)
    i.add_argument('--agent-user', required=True)
    i.add_argument('--gh-path', default=c.DEFAULT_GH)
    sub.add_parser('uninstall')
    sub.add_parser('self-check')
    a = ap.parse_args(argv)
    try:
        if a.cmd == 'install':
            install(os.path.abspath(a.source), a.sha, a.agent_user, a.gh_path)
        elif a.cmd == 'uninstall':
            uninstall()
        else:
            return self_check_main()
    except Refused as e:
        say(f'refused: {e}')
        return 1
    except (OSError, subprocess.CalledProcessError) as e:
        say(f'failed: {e}; run the same command again when it is fixed')
        return 1
    return 0
