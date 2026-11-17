#!/usr/bin/env python3
"""A workspace a self-hosted runner reuses is emptied before actions/checkout, and a git config fork-ci did not expect
is refused before fork-ci's own git calls (README.md).

    git_clean.py prepare [--path DIR]...          first step of a job, before actions/checkout
    git_clean.py check --path DIR [--require]     before git reads a checkout (the actions that read one run this)

An earlier job on the same runner may have run pull request code in the workspace, and that code can write
`.git/config` keys that actions/checkout does not reset: `core.fsmonitor` (a command git runs during checkout's own git
calls), `core.hooksPath`, `attr.tree` with a merge driver, an include of another file. The next job would run them
before any of its own steps runs pull request code.

prepare: on a self-hosted runner (RUNNER_ENVIRONMENT is not `github-hosted`) every entry of GITHUB_WORKSPACE is
removed, symlinks as links (never what they point to), so nothing outside the workspace is touched. A
GitHub-hosted runner starts each job on a fresh disk, so nothing is removed there. Then `check` runs on each path.

check: `DIR/.git` must be a folder (not a .git file or symlink, which point git elsewhere), with no `commondir`, and
every key of its config (and config.worktree) must be one actions/checkout writes, with a value of the kind it
writes (ALLOWED). The include it writes for its token is allowed only to a git-credentials file in RUNNER_TEMP that
holds nothing but the token header. Anything else is refused by name: nothing is repaired in place.

Exit 0: done; 2: refused (the step fails, so the job's checkout and git calls never run).
"""
import argparse, os, re, stat, subprocess, sys, tempfile

# Every fork-ci git call that reads a checkout runs with this environment and these flags (the actions' modules carry
# copies: git_clean.test.py pins them to these). No global or system config (an earlier job may have written
# ~/.gitconfig on a self-hosted runner); no hooks, fsmonitor command, commit-graph or replace refs from the repository.
GIT_ENV = {'GIT_CONFIG_GLOBAL': os.devnull, 'GIT_CONFIG_NOSYSTEM': '1'}
GIT_SAFE = ('-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false', '-c', 'core.commitGraph=false',
            '--no-replace-objects')

BOOL = r'(?i:true|false|yes|no|on|off|1|0)'
UUID = r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
CREDENTIALS = 'credentials'   # a value checked by credentials_problem, not by a pattern
HEADER = (r'http\.https://[^/\s]+/\.extraheader', r'AUTHORIZATION: basic [A-Za-z0-9+/=]+')
# (key, value): what actions/checkout (v4 to v7) and `git init` write, as `git config --list` prints a key (section and
# name lower case, a subsection as written). Everything else is refused, among it every key that runs a command or
# changes what git reads: core.fsmonitor, core.hooksPath, core.sshCommand, core.pager, core.attributesFile, attr.tree,
# merge/diff/filter drivers, include and other includeIf, url.*.insteadOf, alias.*, remote uploadpack.
ALLOWED = tuple((re.compile(k), v if v == CREDENTIALS else re.compile(v)) for k, v in (
    (r'core\.repositoryformatversion', r'[01]'),
    (r'core\.(filemode|logallrefupdates|ignorecase|precomposeunicode|symlinks|sparsecheckout|sparsecheckoutcone)',
     BOOL),
    (r'core\.bare', r'(?i:false|no|off|0)'),
    (r'extensions\.objectformat', r'sha1|sha256'),
    (r'extensions\.worktreeconfig', BOOL),
    (r'extensions\.partialclone', r'origin'),
    (r'gc\.auto', r'0'),
    (r'remote\.origin\.url', r'https://[^\s@]+|git@[A-Za-z0-9.-]+:[^\s]+'),
    (r'remote\.origin\.fetch', r'\+?refs/[^\s:]+:refs/[^\s:]+'),
    (r'remote\.origin\.promisor', BOOL),
    (r'remote\.origin\.partialclonefilter', r'[A-Za-z0-9:=+-]+'),
    (r'branch\.[^\s]+\.remote', r'origin'),
    (r'branch\.[^\s]+\.merge', r'refs/[^\s]+'),
    HEADER,
    (r'includeif\.gitdir:.+\.path', CREDENTIALS),
    (r'lfs\.repositoryformatversion', r'0'),
    (r'filter\.lfs\.clean', r'git-lfs clean -- %f'),
    (r'filter\.lfs\.smudge', r'git-lfs smudge -- %f'),
    (r'filter\.lfs\.process', r'git-lfs filter-process'),
    (r'filter\.lfs\.required', BOOL),
    (r'submodule\.[^\s]+\.url', r'https://[^\s@]+'),
    (r'submodule\.[^\s]+\.active', BOOL),
    (r'user\.(name|email)', r'[^\x00-\x1f\x7f]*'),
))
HEADER_ONLY = ((re.compile(HEADER[0]), re.compile(HEADER[1])),)
REFUSED = 2


def say(text):
    print(text, flush=True)


def entries(path, env):
    """[(key, value)] of one config file read by git itself (no includes followed, no other config), or raises
    ValueError with git's reason."""
    with tempfile.TemporaryDirectory(prefix='git-clean-') as empty:
        clean = {'PATH': env.get('PATH') or os.defpath, 'LC_ALL': 'C', 'HOME': empty, 'GIT_TERMINAL_PROMPT': '0',
                 'GIT_CEILING_DIRECTORIES': os.path.dirname(empty), **GIT_ENV}
        r = subprocess.run(['git', 'config', '--file', path, '--no-includes', '--null', '--list'], cwd=empty,
                           env=clean, capture_output=True, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise ValueError(r.stderr.decode('utf-8', 'replace').strip()[:300] or f'git config exit {r.returncode}')
    out = []
    for item in r.stdout.decode('utf-8', 'surrogateescape').split('\0'):
        if item:
            key, nl, value = item.partition('\n')
            out.append((key, value if nl else 'true'))   # `[core] bare` (no =) means true
    return out


def credentials_problem(value, env):
    """Why an includeIf value is not the token file actions/checkout writes, or None."""
    temp = env.get('RUNNER_TEMP') or ''
    names = [r'/github/runner_temp']   # the same file as a container job sees it
    if temp:
        names.append(re.escape(os.path.realpath(temp)))
        names.append(re.escape(temp))
    if not re.fullmatch(rf'(?:{"|".join(names)})/git-credentials-{UUID}\.config', value):
        return 'which is not a git-credentials file of actions/checkout in RUNNER_TEMP'
    return None


def file_problems(path, rules, env, seen=None):
    """[why] for one config file: keys outside `rules`, values not of their kind, and the included token file."""
    try:
        found = entries(path, env)
    except ValueError as e:
        return [f'{path} cannot be read as git config ({e})']
    problems = []
    for key, value in found:
        rule = next((v for k, v in rules if k.fullmatch(key)), None)
        if rule is None:
            problems.append(f'{path} holds {key}, which actions/checkout never writes')
        elif rule == CREDENTIALS:
            why = credentials_problem(value, env)
            if why:
                problems.append(f'{path} holds {key} = {value!r}, {why}')
            elif os.path.lexists(value) and value not in (seen or ()):
                problems += file_problems(value, HEADER_ONLY, env, {*(seen or ()), value})
        elif not rule.fullmatch(value):
            problems.append(f'{path} holds {key} = {value[:80]!r}, not a value actions/checkout writes')
    return problems


def problems(repo, env, require=False):
    """[why] git at `repo` would read a config fork-ci did not expect; [] when it would not."""
    dot = os.path.join(repo, '.git')
    if not os.path.lexists(dot):
        return [f'{repo} holds no .git: check it out first (actions/checkout)'] if require else []
    if os.path.islink(dot) or not os.path.isdir(dot):
        return [f'{dot} is not a folder (a .git file or symlink points git to another repository)']
    if os.path.lexists(os.path.join(dot, 'commondir')):
        return [f'{dot} holds commondir, which makes git read the config of another repository']
    found = []
    for name in ('config', 'config.worktree'):
        path = os.path.join(dot, name)
        if os.path.lexists(path):
            found += file_problems(path, ALLOWED, env)
    return found


def workspace(env):
    """(the workspace's real path, None) or (None, why it is refused)."""
    ws = env.get('GITHUB_WORKSPACE') or ''
    if not ws or not os.path.isabs(ws):
        return None, f'GITHUB_WORKSPACE {ws!r} is not an absolute path'
    if os.path.islink(ws) or not os.path.isdir(ws):
        return None, f'GITHUB_WORKSPACE {ws} is not a folder (or is a symlink)'
    real = os.path.realpath(ws)
    if real == '/':
        return None, 'GITHUB_WORKSPACE is /'
    runner = env.get('RUNNER_WORKSPACE') or ''
    if runner:
        top = os.path.realpath(runner)
        if os.path.commonpath([real, top]) != top or real == top:
            return None, f'GITHUB_WORKSPACE {real} is not inside RUNNER_WORKSPACE {top}'
    for name in ('HOME', 'RUNNER_TEMP', 'RUNNER_TOOL_CACHE'):
        other = env.get(name) or ''
        if other and os.path.isabs(other):
            o = os.path.realpath(other)
            if os.path.commonpath([real, o]) == real:
                return None, f'GITHUB_WORKSPACE {real} holds {name} ({o})'
    here = os.path.realpath(os.path.dirname(os.path.abspath(__file__)))
    if os.path.commonpath([real, here]) == real:
        return None, f'GITHUB_WORKSPACE {real} holds this action ({here})'
    return real, None


def remove(path):
    """Remove one entry without following a symlink; a folder is made writable first (a read-only one too)."""
    st = os.lstat(path)
    if not stat.S_ISDIR(st.st_mode):
        os.unlink(path)
        return
    if st.st_mode & 0o700 != 0o700:
        os.chmod(path, stat.S_IMODE(st.st_mode) | 0o700)
    with os.scandir(path) as it:
        names = [e.path for e in it]
    for p in names:
        remove(p)
    os.rmdir(path)


def wipe(env):
    """Empty the workspace on a self-hosted runner. None when done or not needed, else why it stopped (refused, or a
    delete failed: what is left is removed by the next run)."""
    if (env.get('RUNNER_ENVIRONMENT') or '') == 'github-hosted':
        say('git-clean: a GitHub-hosted runner starts on a fresh disk: nothing to remove')
        return None
    real, why = workspace(env)
    if why:
        return f'refused to empty the workspace: {why}'
    with os.scandir(real) as it:
        names = sorted(e.path for e in it)
    for p in names:
        try:
            remove(p)
        except OSError as e:
            return f'could not remove {getattr(e, "filename", None) or p} ({e.strerror or e}): the workspace is half empty'
    say(f'git-clean: emptied the workspace {real} ({len(names)} entries)')
    return None


def inside(env, path):
    """(the checkout folder, None) or (None, why): `path` relative to GITHUB_WORKSPACE, and inside it."""
    ws = env.get('GITHUB_WORKSPACE') or os.getcwd()
    if os.path.isabs(path):
        return None, f'path {path} must be relative, inside the workspace'
    full = os.path.normpath(os.path.join(ws, path))
    base = os.path.realpath(ws)
    if os.path.commonpath([os.path.realpath(full), base]) != base:
        return None, f'path {path} must be inside the workspace {ws}'
    return full, None


def check(env, paths, require=False):
    bad = []
    for p in paths:
        repo, why = inside(env, p)
        bad += [why] if why else problems(repo, env, require)
    for why in bad:
        say(f'::error::git-clean: {why}. On a self-hosted runner run fork-ci/actions/git-clean before actions/checkout')
    return REFUSED if bad else 0


def main(argv=None, env=None):
    env = os.environ if env is None else env
    p = argparse.ArgumentParser(prog='git_clean.py', description=__doc__.split('\n\n', 1)[0])
    sub = p.add_subparsers(dest='cmd', required=True)
    pre = sub.add_parser('prepare', help='empty a self-hosted workspace, then check')
    pre.add_argument('--path', action='append', default=None, help='a checkout path to check (default .)')
    chk = sub.add_parser('check', help='refuse a git config actions/checkout does not write')
    chk.add_argument('--path', action='append', required=True)
    chk.add_argument('--require', action='store_true', help='a missing .git is refused too')
    a = p.parse_args(argv)
    paths = [x for line in (a.path or ['.']) for x in line.splitlines() if x.strip()] or ['.']
    if a.cmd == 'prepare':
        why = wipe(env)
        if why:
            say(f'::error::git-clean: {why}')
            return REFUSED
        return check(env, paths)
    return check(env, paths, a.require)


if __name__ == '__main__':
    sys.exit(main())
