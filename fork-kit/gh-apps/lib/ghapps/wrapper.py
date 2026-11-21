"""The gh-as wrapper (/usr/local/bin/gh-as). Runs as the agent, with the agent's own rights, and does three things
the privileged side must not do: finds the repository of the current folder (with /usr/bin/git, never one from
PATH), picks the app set up for it, and reads any file a gh flag names (as the agent) to send it on stdin. Then it
starts the privileged gh-as through sudo, which checks everything again.

  gh-as [--repo OWNER/NAME | --app NAME] [--] gh <command> ...
  gh-as NAME -- gh <command> ...      the same as --app NAME (the form of the older per-user gh-as)
  gh-as --list          the apps set up on this machine
  gh-as --self-check    checks the installed files against the install record
  gh-as --help          this text

--repo=OWNER/NAME and --app=NAME work too. A GH_REPO in the caller's environment never reaches gh (gh always gets
the app's repository), so one that names another repository is refused rather than silently ignored.
"""
import os, stat, subprocess, sys

from . import common as c, policy
from .common import Refused

FILE_LIMIT = 25 * 1024 * 1024
USAGE = ('usage: gh-as [--repo OWNER/NAME | --app NAME] [--] gh <command> ...\n'
         '       gh-as NAME -- gh <command> ...\n'
         '       gh-as --list | --self-check | --help')


def note(msg):
    print(f'gh-as: {c.redact(msg)}', file=sys.stderr)


def origin_repo(cwd):
    """owner/name of the 'origin' remote of the repository that holds cwd, read with the fixed git."""
    try:
        proc = subprocess.run([c.GIT, '-C', cwd, 'config', '--get', 'remote.origin.url'], stdin=subprocess.DEVNULL,
                              capture_output=True, timeout=30,
                              env={'PATH': c.SAFE_PATH, 'LANG': 'C.UTF-8', 'HOME': os.path.expanduser('~'),
                                   'GIT_CONFIG_NOSYSTEM': '1'})
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f'cannot run {c.GIT} ({type(e).__name__}); pass --repo OWNER/NAME') from None
    url = proc.stdout.decode('utf-8', 'replace').strip()
    if proc.returncode != 0 or not url:
        raise Refused('this folder has no git remote "origin"; pass --repo OWNER/NAME')
    repo = c.norm_repo(url)
    if not repo:
        raise Refused(f'the origin remote {url!r} is not a github.com repository; pass --repo OWNER/NAME')
    return repo


def read_file(path):
    """Read a file the agent named, as the agent (so the agent's own rights decide). Not a terminal or FIFO."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as e:
        raise Refused(f'cannot read {path!r}: {e.strerror}') from None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused(f'{path!r} is not a regular file')
        with os.fdopen(fd, 'rb', closefd=False) as f:
            data = f.read(FILE_LIMIT + 1)
    finally:
        os.close(fd)
    if len(data) > FILE_LIMIT:
        raise Refused(f'{path!r} is larger than {FILE_LIMIT // (1024 * 1024)} MB')
    return data


def stdin_fd(data):
    fd = os.memfd_create('gh-as-file', 0)
    os.write(fd, data)
    os.lseek(fd, 0, os.SEEK_SET)
    return fd


def priv_command(app, repo, args):
    """The privileged call (tests replace this function)."""
    return [c.SUDO, '-n', '-u', c.RUN_USER, '--', c.PRIV_AS, '--app', app, '--repo', repo, '--', 'gh', *args]


def launch(cmd, fd):
    """Start the privileged side in place of this process. fd: the new stdin, or None to keep ours."""
    if fd is not None:
        os.dup2(fd, 0)
        os.close(fd)
    os.execve(cmd[0], cmd, {'PATH': c.SAFE_PATH, 'LANG': 'C.UTF-8', 'TERM': 'dumb'})


def list_apps():
    names = c.app_names()
    if not names:
        print('no GitHub App is set up on this machine (the owner adds one with gh-apps-admin add-key)')
        return 1
    for n in names:
        try:
            a = c.load_app(n)
            print(f'{n}\t{a["repo"]}\tapp {a["app_id"]}')
        except Refused as e:
            print(f'{n}\tbroken: {e}')
    return 0


class Usage(Exception):
    pass


def split_selector(args):
    """The words before the gh command -> (repo, app name, the rest starting at gh). Raises Usage.
    --repo X, --repo=X, --app X, --app=X, or a bare app name followed by -- (gh-as NAME -- gh ...); each at most once,
    and never --repo and an app together."""
    repo = app = None
    args = list(args)
    while args:
        a = args[0]
        if a in ('--repo', '--app') and len(args) > 1:
            flag, value, args = a, args[1], args[2:]
        elif a.startswith(('--repo=', '--app=')):
            flag, _, value = a.partition('=')
            args = args[1:]
        elif a not in ('gh', '--') and not a.startswith('-') and args[1:2] == ['--']:
            flag, value, args = '--app', a, args[1:]
        else:
            break
        if not value or (repo if flag == '--repo' else app) is not None:
            raise Usage()
        if flag == '--repo':
            repo = value
        else:
            app = value
    if args[:1] == ['--']:
        args = args[1:]
    if not args or args[0] != 'gh' or (repo and app):
        raise Usage()
    return repo, app, args[1:]


def check_env_repo(app):
    """gh-as gives gh the app's repository as GH_REPO. A caller's own GH_REPO that names another one means the
    caller expects gh to work there: refused, never silently swapped (an empty one counts as unset, as in gh)."""
    named = os.environ.get('GH_REPO', '')
    if named and (c.norm_repo(named) or '').lower() != app['repo'].lower():
        raise Refused(f'GH_REPO={named[:120]!r} is not {app["repo"]}, the only repository app {app["name"]} may use: '
                      'unset GH_REPO or pick its app with --repo')


def main(argv):
    args = list(argv)
    if args in (['--list'], ['--self-check']):
        if args == ['--list']:
            return list_apps()
        from . import install
        return install.self_check_main()
    if args in (['--help'], ['-h']):
        print(__doc__.strip() + '\n\n' + USAGE)
        return 0
    try:
        try:
            repo, app_name, args = split_selector(args)
        except Usage:
            print(USAGE, file=sys.stderr)
            return 2
        if app_name:
            app = c.load_app(app_name)
        else:
            app = c.app_for_repo(repo or origin_repo(os.getcwd()))
        check_env_repo(app)
        c.require_only_app(app)   # gh-as checks it again; refused here first, with the same words
        parsed = policy.parse(args, app['repo'], 'wrapper')
        fd = None
        if parsed.files:
            (at, prefix, path), = parsed.files
            data = read_file(path)
            args[at] = prefix + '-'
            fd = stdin_fd(data)
        policy.parse(args, app['repo'], 'run')   # what the privileged side will see must pass too
        launch(priv_command(app['name'], app['repo'], args), fd)
        return 0   # only reached when a test replaces launch
    except Refused as e:
        note(e)
        return 1
