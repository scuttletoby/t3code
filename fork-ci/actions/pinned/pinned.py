#!/usr/bin/env python3
"""pinned: run the fork's CI code outside a workflow, at the commit a project pins, checked by git blob id.

A workflow runs a fork action with `uses: <owner>/<repo>/fork-ci/actions/<name>@<sha>`. A project's tools that run
outside a workflow (a deploy script, a local check) need the same files from the same commit. This module fetches
them, checks them, caches them and runs them. The project's only fact is its pin config (a JSON file, path given by
the caller):

  {"repo": "<owner>/<repo>", "sha": "<40 hex>", "tag": "ci/vN",
   "files": {"<action>/<file>": "<git blob id at sha>", ...}}

A file is fetched from raw.githubusercontent.com (a public repository: no token) into <cache>/<sha>/<action>/<file>,
and its blob id is checked on every use: a cached copy that differs (a killed write, a hand edit, another job on the
runner) is fetched again, and fetched bytes that differ are refused. A file the config does not name, or no network
and no good copy: FetchError, and the caller fails or falls back to its full answer, never to a guessed one. Each use
also removes .part- files older than PART_AGE (a killed fetch), and the folders of other shas that nothing used for
CACHE_DAYS and the run folders a killed run left that long ago. load() and run run the bytes that were checked, never
a second read of the cache file, and refuse a file that would load its neighbours (neighbour_loads): those would be
cache files nothing checked. Both refuse a cache under a folder someone else could write (shared_above).

  python3 -I pinned.py --config .github/fork-ci.json [--cache DIR] run <action> <file> [args...]
runs that pinned script (python3 -I) with the arguments after its name, from a private copy of the checked bytes.
  python3 -I pinned.py --config .github/fork-ci.json files
prints the "files" map at the config's sha (fetched, not checked): for a pin bump, move "sha" and "tag", paste it.
README.md next to this file: the Python API and the bootstrap a project copies to load this module.
"""
import argparse, ast, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile, time, types, urllib.request
try:
    import grp, pwd
except ImportError:   # no owners (Windows): shared_above checks nothing there
    grp = pwd = None

NAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9._-]*\Z')   # \Z: no trailing newline
SHA = re.compile(r'[0-9a-f]{40}')
PART_AGE = 3600     # seconds: an older .part- file is a killed fetch (a live one takes seconds)
CACHE_DAYS = 14     # another sha's folder that nothing used for this long goes (a rerun of an old run fetches again)
# modules that load code from a path (site: a .pth file runs code; builtins: its __import__ and exec; the private
# twins of importlib, imp and ctypes; pydoc.importfile; unittest's discover; doctest.testfile)
BY_PATH = ('importlib', 'runpy', 'imp', 'zipimport', 'pkgutil', 'site', 'pkg_resources', 'ctypes', 'builtins',
           '_frozen_importlib', '_frozen_importlib_external', '_imp', '_ctypes', 'pydoc', 'unittest', 'doctest')
SYS_IMPORT = ('path', 'meta_path', 'path_hooks', 'path_importer_cache', 'modules')   # the sys names imports go by
# sys names that give the file's own path: frames (f_code.co_filename), tracebacks (tb_frame), the command line
SYS_FRAME = ('_getframe', '_current_frames', '_getframemodulename', 'exc_info', 'exception', 'last_traceback',
             'last_value', 'last_exc', 'settrace', 'setprofile', 'orig_argv')
SYS_FROM = ('argv', 'exit', 'stdin', 'stdout', 'stderr', 'executable', 'platform', 'version', 'version_info',
            'maxsize')   # the only names `from sys import` may take (argv: only as argv[k] or argv[k:], k >= 1)
# where a neighbour's path starts: the file's own path, or the importer (__builtins__ holds __import__)
OWN_PATH = ('__file__', '__spec__', '__loader__', '__cached__', '__import__', '__builtins__', 'globals', 'locals')
OWN_MODULE = ('__main__', 'inspect', 'traceback')   # modules that give the running file's own path
OWN_ATTR = ('co_filename', 'f_code', 'f_globals', 'f_back', 'tb_frame', 'gi_frame', 'cr_frame', 'ag_frame',
            '__code__', '__globals__', '__traceback__')   # a function's or an exception's way to its frame or file
RUN = '.run-'   # a run's private folder in the cache (a killed run leaves it; prune removes it after CACHE_DAYS)


def default_cache():
    return os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'fork-ci-pinned')


class FetchError(Exception):
    pass


class Unreachable(FetchError):
    """The fetch itself failed (no network, GitHub down, HTTP error): not a wrong blob id."""


def good_repo(repo):
    """owner/name, neither part '.' or '..' (it goes into a URL path)."""
    return (isinstance(repo, str) and re.fullmatch(r'[\w.-]+/[\w.-]+', repo) is not None
            and not any(part in ('.', '..') for part in repo.split('/')))


def repo_and_sha(config):
    """The pin config (a path) as a dict with a good "repo" and a 40-hex "sha" (a full commit sha, never a name)."""
    try:
        with open(config, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise FetchError(f'{config}: cannot be read as JSON ({e})')
    if not (isinstance(c, dict) and good_repo(c.get('repo')) and isinstance(c.get('sha'), str)
            and SHA.fullmatch(c['sha'])):
        raise FetchError(f'{config}: needs "repo" (owner/name) and "sha" (40 hex)')
    return c


def pin(config):
    """{"repo", "sha", "tag", "files"} from the pin config file (a path)."""
    c = repo_and_sha(config)
    files = c.get('files')
    if not (isinstance(files, dict) and all(isinstance(v, str) and SHA.fullmatch(v) for v in files.values())):
        raise FetchError(f'{config}: needs "files", {{"<action>/<file>": "<git blob id>"}} (pinned.py files prints it)')
    return c


def private(cache):
    """The cache folder, made 0700 when missing and checked before anything in it is read or written: the README
    bootstrap's rule, so `pinned.py --cache C run` takes exactly the folders load_pinned takes. A symlink, not a folder,
    another user's, or one the group or others can write is refused, never chmodded: someone else could already have
    put a symlink, a FIFO or a file in it that prune or a read would act on. (The bytes run are checked either way.)"""
    if not os.path.lexists(cache):
        missing, up = [], os.path.dirname(os.path.abspath(cache))
        while not os.path.lexists(up):
            missing.append(up)
            up = os.path.dirname(up)
        for up in reversed(missing):   # 0700 like the cache, whatever the umask: run checks the folders above it
            try:
                os.mkdir(up, 0o700)
            except FileExistsError:
                pass
        try:
            os.makedirs(cache, mode=0o700)
        except FileExistsError:   # another run made it first (two first runs at once): the check below decides
            pass
    st = os.lstat(cache)
    if not stat.S_ISDIR(st.st_mode) or hasattr(os, 'getuid') and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        raise FetchError(f'cache folder {cache} is not a folder of your own that only you can write (a symlink, '
                         "another user's, or group- or world-writable): remove it or pass --cache with another")


def own_group(gid):
    """Whether gid is your own private group: your primary group, named as you, with no listed member and no other
    user's primary group. Debian and Ubuntu give each user one (and umask 002, so ~/.cache is often 0775); Debian's
    OpenSSH trusts a group-writable folder of yours in such a group (StrictModes) for the same reason. None (falsy:
    not one) when any lookup fails, whatever it raises."""
    try:
        me, group, users = pwd.getpwuid(os.getuid()), grp.getgrgid(gid), pwd.getpwall()
    except Exception:   # KeyError, OSError, or whatever an NSS module raises: cannot tell, so not private
        return None
    return (me.pw_gid == gid and group.gr_name == me.pw_name and not group.gr_mem
            and all(u.pw_uid == me.pw_uid for u in users if u.pw_gid == gid))


ACLS = ('system.posix_acl_access', 'system.nfs4_acl')


def acl(folder):
    """Whether folder has an ACL (True), none (False), or None when we cannot tell: no listxattr here (not Linux),
    or it fails (a file system without xattrs, an unreadable folder). With an ACL, a folder's group bits are the ACL
    mask, not its group's: a 0775 folder of yours can be writable by a user or group the ACL names."""
    listxattr = getattr(os, 'listxattr', None)
    if listxattr is None:
        return None
    try:
        return not set(ACLS).isdisjoint(listxattr(folder))
    except OSError:
        return None


def shared_above(cache):
    """The folders above the cache (by the path given and by its real path) that someone else could write: not ours
    or root's, world-writable (a sticky /tmp too), or group-writable unless it is ours, has no ACL (acl: the group
    bits may be its mask) and its group is our own private group (own_group). run's copy is
    <cache>/.run-X/<action>/<file>, so a script's `../../..` is the folder above the cache, where such a someone could
    plant a neighbour. A missing folder is skipped: private() makes it 0700 below a folder checked here. [] where there
    are no owners (Windows)."""
    if not hasattr(os, 'getuid'):
        return []
    bad, seen = [], set()
    for p in (os.path.abspath(cache), os.path.realpath(cache)):
        up = os.path.dirname(p)
        while up not in seen:
            seen.add(up)
            try:
                st = os.stat(up)   # a symlink is judged by its target; its own folder is judged as an ancestor
            except FileNotFoundError:
                up = os.path.dirname(up)
                continue
            except OSError as e:   # a folder above it has no x (EACCES): it cannot be checked, so it is refused
                raise FetchError(unenterable(up, cache, e))
            if (st.st_uid not in (0, os.getuid()) or st.st_mode & 0o002
                    or st.st_mode & 0o020
                    and not (st.st_uid == os.getuid() and acl(up) is False and own_group(st.st_gid))):
                bad.append(up)
            up = os.path.dirname(up)
    return bad


def unenterable(up, cache, error):
    """Why a folder above the cache could not be checked, naming the folder that cannot be entered and the fix."""
    blocked = os.path.dirname(up)   # the lowest folder whose stat works is the one without x
    while True:
        try:
            st = os.stat(blocked)
            break
        except OSError:
            if os.path.dirname(blocked) == blocked:
                st = None
                break
            blocked = os.path.dirname(blocked)
    fix = (f'chmod u+x {blocked}' if st is not None and st.st_uid == os.getuid()
           else 'pass --cache with a folder whose every parent you can enter and only you or root can write')
    return (f'{blocked} cannot be entered ({error.strerror or error}), so {up} above the cache {cache} cannot be '
            f'checked: it could be someone else\'s, so this cache is refused. Fix: {fix}')


def fix_above(up):
    """How to fix a folder shared_above found: chmod a folder of yours, use another cache under anyone else's."""
    st = os.stat(up)
    if st.st_uid != os.getuid():
        return 'pass --cache with a folder whose every parent only you or root can write'
    if st.st_mode & 0o002:
        return f'chmod go-w {up}'
    has = acl(up)
    if has:
        return (f'{up} has an ACL, so its group bits are the ACL mask, not its group: remove the ACL '
                f'(setfacl -b {up}) or chmod g-w {up}')
    if has is None:
        return f'cannot tell if {up} has an ACL: chmod g-w {up}'
    if own_group(st.st_gid) is None:
        return f'cannot tell if the group of {up} is private: chmod g-w {up}'
    return f'chmod g-w {up}'


def read_regular(path):
    """The bytes of path if it is a regular file (no symlink followed, no FIFO or device opened), else None."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_BINARY', 0))
    with os.fdopen(fd, 'rb') as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read()


def blob_id(data):
    """git's blob id of these bytes (what `git hash-object` and `git ls-tree` print)."""
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def fetch(repo, sha, action, name):
    url = f'https://raw.githubusercontent.com/{repo}/{sha}/fork-ci/actions/{action}/{name}'
    try:
        with urllib.request.urlopen(url, timeout=60) as r:
            return r.read()
    except OSError as e:   # HTTPError and URLError too
        raise Unreachable(f'cannot fetch {url}: {e}')


def prune(cache, keep, now=None):
    """Remove .part- files older than PART_AGE, and the folders of other shas unused for CACHE_DAYS (their folder's
    mtime: every use touches it) and run folders that old (a killed run's). Only 40-hex and .run- folders are removed.
    Never fails the caller: another process may remove a file first."""
    now = time.time() if now is None else now
    try:
        shas = os.listdir(cache)
    except OSError:
        return
    for sha in shas:
        top = os.path.join(cache, sha)
        try:
            if not stat.S_ISDIR(os.lstat(top).st_mode):   # a symlink is never followed out of the cache
                continue
            if (sha != keep and (SHA.fullmatch(sha) or sha.startswith(RUN))
                    and now - os.stat(top).st_mtime > CACHE_DAYS * 86400):
                shutil.rmtree(top, ignore_errors=True)
                continue
            for folder, _, names in os.walk(top):
                for n in names:
                    f = os.path.join(folder, n)
                    if n.startswith('.part-') and now - os.lstat(f).st_mtime > PART_AGE:
                        os.remove(f)
        except OSError:
            continue


def verified(config, action, name, cache=None):
    """(local path, bytes) of fork-ci/actions/<action>/<name> at the pinned sha, whose blob id is the config's."""
    if not (isinstance(action, str) and isinstance(name, str) and NAME.match(action) and NAME.match(name)):
        raise FetchError(f'not a plain action or file name: {action}/{name}')
    c = pin(config)
    want = c['files'].get(f'{action}/{name}')
    if want is None:
        raise FetchError(f'{action}/{name} is not in {config} "files" (its blob id: pinned.py --config {config} '
                         'files, after adding the name)')
    cache = cache or default_cache()
    private(cache)
    prune(cache, c['sha'])
    top = os.path.join(cache, c['sha'])
    folder = os.path.join(top, action)
    dest = os.path.join(folder, name)
    try:
        data = read_regular(dest)
        if data is not None and blob_id(data) == want:
            os.utime(top)   # in use: prune keeps it
            return dest, data
    except OSError:
        pass
    data = fetch(c['repo'], c['sha'], action, name)
    if blob_id(data) != want:
        raise FetchError(f'{action}/{name} at {c["sha"][:10]} has blob id {blob_id(data)}, but {config} says {want}'
                         f' (the config does not match the pinned commit: pinned.py --config {config} files)')
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=folder, prefix='.part-')
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, dest)   # whole or not at all: a killed fetch leaves only a .part- file (pruned after PART_AGE)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.utime(top)   # in use: prune keeps it
    return dest, data


def neighbour_loads(data):
    """What in these Python bytes would load code from the file's own folder, which in the cache holds files nothing
    checked. load() and run refuse a file with any: they run one checked file, never its neighbours.
      - sys.path: sys (also os.sys, `from os import sys`) used for an import name (SYS_IMPORT: pr-queue/pr_queue.py
        puts its folder first, then imports config and lens), or used as anything but `sys.<other name>` (an alias's
        .path, getattr, vars, `s = sys`);
      - a module that loads code from a path or swaps the import (BY_PATH), or a relative import;
      - the file's own path (OWN_PATH: __file__, __spec__, __loader__, __cached__, globals, locals; sys.argv except
        argv[k] or argv[k:] with k >= 1; SYS_FRAME; OWN_ATTR; vars(); __main__, inspect, traceback) or the importer
        (__import__, __builtins__): every way to name a neighbour (exec(open(...)), a subprocess, os.chdir,
        site.addsitedir, a data file) starts from it. `from sys import` takes only SYS_FROM.
    Other imports come from the caller's own path (the standard library, an installed PyYAML), as any import of the
    caller's does. A static check of the usual spellings, never complete (a constant or built string passed to getattr,
    eval or exec, stdlib introspection, an absolute path such as /proc/self/cmdline pass it), so load() and run also puts its copy in a private folder inside the
    checked cache and refuses a cache with a folder above it that someone else could write (shared_above): every `..`
    of the copy is ours or root's."""
    try:
        tree = ast.parse(data)
    except (SyntaxError, ValueError):
        return ['(does not parse)']
    found, sys_names, argv_names = set(), {'sys'}, set()
    for node in ast.walk(tree):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
            sys_names.update(a.asname for a in node.names if a.name == 'sys' and a.asname)
        elif isinstance(node, ast.ImportFrom):
            names = ['.' * node.level + (node.module or '')]
            sys_names.update(a.asname or a.name for a in node.names if a.name == 'sys')   # from os import sys
            if node.module == 'sys':
                for a in node.names:
                    if a.name in (*SYS_IMPORT, '*'):
                        found.add('sys.path')
                    elif a.name not in SYS_FROM:
                        found.add(f'sys.{a.name}')
                    elif a.name == 'argv':
                        argv_names.add(a.asname or a.name)
        found.update(n for n in names if n.startswith('.') or n.split('.')[0] in (*BY_PATH, *OWN_MODULE))
    parent = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}

    def index(node):
        return node.slice.value if isinstance(node.slice, getattr(ast, 'Index', ())) else node.slice

    def past_0(i):   # a constant k >= 1, or a slice [k:] that starts there
        i = i.lower if isinstance(i, ast.Slice) else i
        return isinstance(i, ast.Constant) and type(i.value) is int and i.value >= 1

    for node in ast.walk(tree):
        if isinstance(node, ast.Subscript):   # argv[0], sys.argv[0]: the script's own path
            argv = (isinstance(node.value, ast.Attribute) and node.value.attr == 'argv'
                    or isinstance(node.value, ast.Name) and node.value.id in ('argv', *argv_names))
            if argv and isinstance(index(node), ast.Constant) and index(node).value == 0:
                found.add('argv[0]')
            if isinstance(index(node), ast.Constant) and index(node).value == 'sys':   # os.__dict__['sys']
                found.add('sys.path')
        if (isinstance(node, ast.Name) and node.id in argv_names
                or isinstance(node, ast.Attribute) and node.attr == 'argv'
                and (isinstance(node.value, ast.Name) and node.value.id in sys_names
                     or isinstance(node.value, ast.Attribute) and node.value.attr == 'sys')):
            p = parent.get(node)   # sys's argv: only argv[k], argv[k:] (k >= 1) or len(argv)
            if not (isinstance(p, ast.Subscript) and p.value is node and past_0(index(p))
                    or isinstance(p, ast.Call) and isinstance(p.func, ast.Name) and p.func.id == 'len'):
                found.add('sys.argv')
        if isinstance(node, ast.Attribute) and node.attr in OWN_ATTR:
            found.add(node.attr)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id == 'vars' and not node.args:   # vars() is locals()
                found.add('vars()')
            if (node.func.id == 'getattr' and len(node.args) > 1 and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == 'sys'):
                found.add('sys.path')
        if isinstance(node, ast.Name) and node.id in OWN_PATH:
            found.add(node.id)
        elif (isinstance(node, ast.Name) and node.id in sys_names
              or isinstance(node, ast.Attribute) and node.attr == 'sys'):   # os.sys
            p = parent.get(node)
            if isinstance(p, ast.Attribute) and p.attr in SYS_FRAME:
                found.add(f'sys.{p.attr}')
            elif not (isinstance(p, ast.Attribute) and p.attr not in SYS_IMPORT and not p.attr.startswith('__')):
                found.add('sys.path')
    return sorted(found)


def self_contained(config, action, name, cache):
    """verified(), refused when the file would load its neighbours (neighbour_loads), which nothing checked."""
    dest, data = verified(config, action, name, cache)
    found = neighbour_loads(data)
    if found:
        raise FetchError(f'{action}/{name} uses {", ".join(found)}: it would load files next to it that were not '
                         'checked, so load and run refuse it (they run one self-contained file)')
    return dest, data


def path(config, action, name, cache=None):
    """The local path of fork-ci/actions/<action>/<name> at the pinned sha, checked (fetched again when it differs).
    The file can change after this returns (another job on the runner): to run it, use load() or run()."""
    return verified(config, action, name, cache)[0]


def refuse_shared_above(cache, caller):
    """FetchError when a folder above the cache is someone else's to write (shared_above), with each one's fix."""
    shared = shared_above(cache)
    if shared:
        raise FetchError(f'{", ".join(shared)} above the cache {cache} can be written by someone else (not yours or '
                         "root's, world-writable, a sticky temp folder too, or group-writable by a group that is not "
                         "your own private one, or through an ACL): a script's `..` reaches it, so "
                         f"{caller} refuses this cache. Fix: "
                         + '; '.join(dict.fromkeys(fix_above(up) for up in shared)))


def load(config, action, module, cache=None):
    """The pinned fork module (e.g. load(cfg, 'gradle-cache', 'gradle_cache')), run from the bytes that were checked.
    Its __file__ is <cache>/<sha>/<action>/<file>, so its `../../..` is the folder above the cache: refused as in run."""
    cache = cache or default_cache()
    refuse_shared_above(cache, 'load')   # before private() makes any folder
    dest, data = self_contained(config, action, module + '.py', cache)
    mod = types.ModuleType(f'fork_ci_{module}')
    mod.__file__ = dest
    exec(compile(data, dest, 'exec'), mod.__dict__)
    return mod


def run(config, action, name, args=(), cache=None, runner=None):
    """Run the pinned script with python3 -I and args, from a private copy (<cache>/.run-X/<action>/<name>) of the
    bytes that were checked; its exit code. The copy goes after the run (a killed run's goes after CACHE_DAYS)."""
    cache = cache or default_cache()   # never None: mkdtemp(dir=None) is the shared temp folder
    refuse_shared_above(cache, 'run')   # before private() makes any folder: a refused cache leaves nothing behind
    private(cache)
    _, data = self_contained(config, action, name, cache)
    # 0700, ours alone: the bytes checked are the bytes run. In the checked cache, never the shared temp folder: the
    # script's `..` is this folder, `../..` the 0700 cache, and above it the folders shared_above found ours or root's.
    run_dir = tempfile.mkdtemp(prefix=RUN, dir=cache)
    try:
        folder = os.path.join(run_dir, action)
        os.mkdir(folder, 0o700)
        script = os.path.join(folder, name)
        with open(script, 'wb') as f:
            f.write(data)
        return (runner or subprocess.run)([sys.executable, '-I', script, *args]).returncode
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def files(config):
    """The "files" map at the config's sha: each named file fetched and hashed (for a pin bump: the blob ids are not
    checked, the repo and the full sha are)."""
    c = repo_and_sha(config)
    names = c.get('files') or {}
    if not isinstance(names, dict):
        raise FetchError(f'{config}: "files" is not a map of "<action>/<file>" names')
    out = {}
    for k in names:
        action, _, name = k.partition('/')
        if not (NAME.match(action) and NAME.match(name)):
            raise FetchError(f'{config}: "files" key {k!r} is not <action>/<file>')
        out[k] = blob_id(fetch(c['repo'], c['sha'], action, name))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(prog='pinned.py', description=__doc__.split('\n')[0])
    p.add_argument('--config', required=True, help='the pin config: {"repo", "sha", "tag", "files"}')
    p.add_argument('--cache', default=None, help=f'the cache folder (default {default_cache()})')
    p.add_argument('command', choices=('run', 'files'))
    p.add_argument('rest', nargs=argparse.REMAINDER, help='run: <action> <file> [args...]')
    a = p.parse_args(sys.argv[1:] if argv is None else argv)
    try:
        if a.command == 'files':
            if a.rest:
                p.error('files takes no arguments')
            print(json.dumps(files(a.config), indent=2))
            return 0
        if len(a.rest) < 2:
            p.error('run needs <action> <file> [args...]')
        return run(a.config, a.rest[0], a.rest[1], a.rest[2:], a.cache)
    except (FetchError, OSError) as e:
        print(f'pinned: {e}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
