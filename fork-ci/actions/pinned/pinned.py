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
also removes .part- files older than PART_AGE (a killed fetch) and the folders of other shas that nothing used for
CACHE_DAYS. load() and run run the bytes that were checked, never a second read of the cache file.

  python3 -I pinned.py --config .github/fork-ci.json [--cache DIR] run <action> <file> [args...]
runs that pinned script (python3 -I) with the arguments after its name, from a private copy of the checked bytes.
  python3 -I pinned.py --config .github/fork-ci.json files
prints the "files" map at the config's sha (fetched, not checked): for a pin bump, move "sha" and "tag", paste it.
README.md next to this file: the Python API and the bootstrap a project copies to load this module.
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, types, urllib.request

NAME = re.compile(r'^[A-Za-z0-9_][A-Za-z0-9._-]*$')
SHA = re.compile(r'[0-9a-f]{40}')
PART_AGE = 3600     # seconds: an older .part- file is a killed fetch (a live one takes seconds)
CACHE_DAYS = 14     # another sha's folder that nothing used for this long goes (a rerun of an old run fetches again)


def default_cache():
    return os.path.join(os.environ.get('XDG_CACHE_HOME') or os.path.expanduser('~/.cache'), 'fork-ci-pinned')


class FetchError(Exception):
    pass


class Unreachable(FetchError):
    """The fetch itself failed (no network, GitHub down, HTTP error): not a wrong blob id."""


def pin(config):
    """{"repo", "sha", "tag", "files"} from the pin config file (a path)."""
    try:
        with open(config, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise FetchError(f'{config}: cannot be read as JSON ({e})')
    if not (isinstance(c, dict) and isinstance(c.get('repo'), str) and isinstance(c.get('sha'), str)
            and re.fullmatch(r'[\w.-]+/[\w.-]+', c['repo']) and SHA.fullmatch(c['sha'])):
        raise FetchError(f'{config}: needs "repo" (owner/name) and "sha" (40 hex)')
    files = c.get('files')
    if not (isinstance(files, dict) and all(isinstance(v, str) and SHA.fullmatch(v) for v in files.values())):
        raise FetchError(f'{config}: needs "files", {{"<action>/<file>": "<git blob id>"}} (pinned.py files prints it)')
    return c


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
    """Remove .part- files older than PART_AGE and the folders of other shas unused for CACHE_DAYS (their folder's
    mtime: every use touches it). Only 40-hex folders are removed. Never fails the caller: another process may remove
    a file first."""
    now = time.time() if now is None else now
    try:
        shas = os.listdir(cache)
    except OSError:
        return
    for sha in shas:
        top = os.path.join(cache, sha)
        try:
            if sha != keep and SHA.fullmatch(sha) and now - os.stat(top).st_mtime > CACHE_DAYS * 86400:
                shutil.rmtree(top, ignore_errors=True)
                continue
            for folder, _, names in os.walk(top):
                for n in names:
                    f = os.path.join(folder, n)
                    if n.startswith('.part-') and now - os.stat(f).st_mtime > PART_AGE:
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
    prune(cache, c['sha'])
    top = os.path.join(cache, c['sha'])
    folder = os.path.join(top, action)
    dest = os.path.join(folder, name)
    try:
        with open(dest, 'rb') as f:
            data = f.read()
        if blob_id(data) == want:
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


def path(config, action, name, cache=None):
    """The local path of fork-ci/actions/<action>/<name> at the pinned sha, checked (fetched again when it differs).
    The file can change after this returns (another job on the runner): to run it, use load() or run()."""
    return verified(config, action, name, cache)[0]


def load(config, action, module, cache=None):
    """The pinned fork module (e.g. load(cfg, 'gradle-cache', 'gradle_cache')), run from the bytes that were checked."""
    dest, data = verified(config, action, module + '.py', cache)
    mod = types.ModuleType(f'fork_ci_{module}')
    mod.__file__ = dest
    exec(compile(data, dest, 'exec'), mod.__dict__)
    return mod


def run(config, action, name, args=(), cache=None, runner=None):
    """Run the pinned script with python3 -I and args, from a private copy (mode 0700 folder) of the bytes that were
    checked; its exit code. The copy goes after the run."""
    _, data = verified(config, action, name, cache)
    run_dir = tempfile.mkdtemp(prefix='fork-ci-run-')   # 0700, ours alone: the bytes checked are the bytes run
    try:
        script = os.path.join(run_dir, name)
        with open(script, 'wb') as f:
            f.write(data)
        return (runner or subprocess.run)([sys.executable, '-I', script, *args]).returncode
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


def files(config):
    """The "files" map at the config's sha: each named file fetched and hashed (for a pin bump: nothing is checked)."""
    try:
        with open(config, encoding='utf-8') as f:
            c = json.load(f)
    except (OSError, ValueError) as e:
        raise FetchError(f'{config}: cannot be read as JSON ({e})')
    out = {}
    for k in c.get('files') or {}:
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
