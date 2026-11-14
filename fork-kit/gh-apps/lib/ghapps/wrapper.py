"""The gh-as wrapper (/usr/local/bin/gh-as). Runs as the agent, with the agent's own rights, and does three things
the privileged side must not do: finds the repository of the current folder (with /usr/bin/git, never one from
PATH), picks the app set up for it, and reads any file a gh flag names (as the agent) to send it on stdin. Then it
starts the privileged gh-as through sudo, which checks everything again.

  gh-as [--repo OWNER/NAME | --app NAME] [--] gh <command> ...
  gh-as --list          the apps set up on this machine
  gh-as --self-check    checks the installed files against the install record
"""
import os, stat, subprocess, sys

from . import common as c, policy
from .common import Refused

FILE_LIMIT = 25 * 1024 * 1024
USAGE = ('usage: gh-as [--repo OWNER/NAME | --app NAME] [--] gh <command> ...\n'
         '       gh-as --list | --self-check')


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


def main(argv):
    args = list(argv)
    if args in (['--list'], ['--self-check']):
        if args == ['--list']:
            return list_apps()
        from . import install
        return install.self_check_main()
    repo = app_name = None
    try:
        while args and args[0] in ('--repo', '--app'):
            if len(args) < 2:
                break
            if args[0] == '--repo':
                repo = args[1]
            else:
                app_name = args[1]
            args = args[2:]
        if args[:1] == ['--']:
            args = args[1:]
        if not args or args[0] != 'gh' or (repo and app_name):
            print(USAGE, file=sys.stderr)
            return 2
        args = args[1:]
        if app_name:
            app = c.load_app(app_name)
        else:
            app = c.app_for_repo(repo or origin_repo(os.getcwd()))
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
