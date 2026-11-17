# pinned

Not a composite action: a Python module and command for a project's tools that run **outside a workflow** (a
deploy script, a local check, a test). It runs the same fork-ci files a workflow's `uses: ...@<sha>` runs, from the
same commit, checked by git blob id.

The project's only fact is its pin config, a JSON file in the project (for example `.github/fork-ci.json`):

```json
{
  "repo": "<owner>/<repo>",
  "sha": "<release sha, 40 hex>",
  "tag": "ci/vN",
  "files": {
    "pinned/pinned.py": "<git blob id at sha>",
    "docs-scope/docs_scope.py": "<git blob id at sha>"
  }
}
```

`files` names every file under `fork-ci/actions/` the tools use (`<action>/<file>`) with its blob id at `sha`. Pin a
release commit, never a commit of main ([why](../../README.md#pinning)). For a pin bump, move `sha` and `tag`, then
paste what `files` prints.

## What it guarantees

- A file is fetched from `raw.githubusercontent.com/<repo>/<sha>/fork-ci/actions/<action>/<file>` (a public
  repository: no token) into `<cache>/<sha>/<action>/<file>`. Its blob id is checked on **every** use: a cached copy
  that differs (a killed write, a hand edit, another job on the runner) is fetched again, and fetched bytes that
  differ from the config are refused and never kept.
- Writes are whole or not at all: a `.part-` file, then a rename. A killed fetch leaves only a `.part-` file, which is
  never read and is removed after `PART_AGE` (an hour).
- Every use marks its sha's folder used and removes the folders of other shas that nothing used for `CACHE_DAYS`
  (14), and the run folders (`.run-X`) a killed `run` left that long ago. Only 40-hex and `.run-` folders are ever
  removed; the pinned sha's own folder stays, however old.
- `load()` and `run` run the bytes that were checked, never a second read of the cache file (another job on the
  runner may write it between the check and the run).
- `load()` and `run` take one self-contained file: one that touches `sys.path` (or the other `sys` names imports go
  by, also through an alias, `os.sys`, `from os import sys`, `getattr(x, 'sys')` or `x.__dict__['sys']`), imports a
  module that loads code from a path (`importlib`, `runpy`, `site`, `ctypes`, their private twins
  `_frozen_importlib_external`, `_imp`, `_ctypes`, and `pydoc`, `unittest`, `doctest`, ...), uses a relative import,
  or names its own path (`__file__`, `__spec__`, `__loader__`, `__cached__`, `sys.argv` other than `argv[k]`/`argv[k:]` with
  k >= 1, `sys.orig_argv`, `sys._getframe` and the other frame and traceback names, `globals()`, `locals()`, `vars()`,
  `co_filename`, `__code__`, `__traceback__`, `tb_frame`, `__main__`, `inspect`, `traceback`) or the importer
  (`__import__`, `__builtins__`) would load the files next to it, which nothing checked, so it is refused
  (`FetchError`). `from sys import` takes only a short list of names (`argv`, `exit`, `stdout`, ...).
  `pr-queue/pr_queue.py`, `head-check/*.py`, `fleet-watch/fleet_watch.py`, `test-select/test_select.py` and
  `test-select/trace_check.py` are such files: they run only from a checkout or a
  workflow's `uses:`. The check is static and covers the usual spellings, not every one: a constant or built string
  passed to `getattr`, `eval` or `exec`, stdlib introspection (tracing, profiling, `gc`, `tracemalloc`,
  `faulthandler`, the repr of a code or frame object), `compile`, `pickle`, `marshal`, a `logging` record's
  `pathname`, `warnings`, a subprocess of `sys.executable`, and an absolute path (`/proc/self/cmdline`, `/tmp/x`,
  `tempfile.gettempdir()`) pass it, and chasing them would refuse ordinary code. What protects is where the code runs:
  `run` puts its copy in a fresh 0700 folder inside the checked cache (`<cache>/.run-X/<action>/<file>`), `load()`'s
  module has `__file__` `<cache>/<sha>/<action>/<file>`, and both refuse a cache with a folder above it that someone
  else could write (not yours or root's, world-writable: a sticky `/tmp` too, or group-writable; by the path given and
  by its real path), so every `..` of the script is the run folder, the cache, or a folder only you or root can write,
  never the shared temp folder. One group-writable folder is accepted, as Debian's OpenSSH does for `StrictModes`: one
  of yours whose group is your own private group (your primary group, named as you, with no listed member and no other
  user's primary group). Debian and Ubuntu give each user such a group and umask 002, so `~/.cache` is often 0775.
  Not when the folder has an ACL (POSIX or NFSv4), or when it cannot tell (`listxattr` fails or is missing, as on
  macOS): with an ACL the group bits are the ACL mask, so `setfacl -m u:<user>:rwx` on a 0755 folder shows 0775. The
  fix is `setfacl -b <folder>` or `chmod g-w <folder>`.
  Before #305 a copy sat directly in the system temp folder, so `head_check.py`'s
  `../pr-queue` (or `test_select.py`'s `../docs-scope`) was `$TMPDIR/pr-queue`, which any local user can make when
  `$TMPDIR` is `/tmp`; until #331 the run folder was in the system temp folder too, so `../..` reached it; until #339
  a cache in a shared folder (`--cache /tmp/fci`) let `../../..` reach it. None of this protects against a module that
  names an absolute path: no fork-ci module does today, and only review would catch a new one that did.
- A file the config does not name, a bad config, or no network and no good copy: `FetchError` (its subclass
  `Unreachable` for the network alone). Nothing is ever guessed.

## Command

```sh
python3 -I pinned.py --config .github/fork-ci.json [--cache DIR] run <action> <file> [args...]
python3 -I pinned.py --config .github/fork-ci.json files
```

- `run` runs `python3 -I` on a private copy (`<cache>/.run-X/<action>/<file>`) of the checked bytes, with the
  arguments after the file name, and exits with its exit code. A fetch or check failure exits 1 (`pinned: <why>` on stderr).
- `files` prints the `files` map at the config's `sha`, each file fetched and hashed (nothing is checked: this is how
  the map is made).
- `--cache` defaults to `$XDG_CACHE_HOME/fork-ci-pinned` (`~/.cache/fork-ci-pinned`). `run` (and `load()`) refuse a
  cache under a folder someone else could write, before they make any folder, and name each such folder with its fix:
  `chmod g-w <folder>` (or `go-w`) for a folder of yours, another `--cache` under anyone else's (`/tmp/...`). A folder
  above the cache you cannot enter (no `x`) cannot be checked, so it is refused the same way, named with its fix:
  `chmod u+x <folder>` for yours, another `--cache` for anyone else's. Through a symlink, the folder named is the real
  one without `x` (where the link leads), not the link's own folder. A symlink loop or a file on the way above the
  cache is refused too, with another cache as the fix. `load()` names its `cache` argument where `run` says `--cache`.

## Python API

Callers may import exactly these names; anything else in the module may change in any release.

| Name                                             | What it does                                                                       |
| ------------------------------------------------ | ---------------------------------------------------------------------------------- |
| `load(config, action, module, cache=None)`       | the pinned `<action>/<module>.py` as a module, run from the checked bytes          |
| `run(config, action, name, args=(), cache=None)` | runs the pinned script from a private copy of the checked bytes; its exit code     |
| `verified(config, action, name, cache=None)`     | `(local path, bytes)`, checked                                                     |
| `path(config, action, name, cache=None)`         | the checked local path (to read it; to run it use `load` or `run`)                 |
| `files(config)`                                  | the `files` map at the config's `sha` (for a pin bump)                             |
| `pin(config)`                                    | the config, checked                                                                |
| `blob_id(data)`                                  | git's blob id of bytes                                                             |
| `prune(cache, keep)`                             | the cleanup every use runs                                                         |
| `main(argv)`                                     | the command above (a project's adapter passes `['--config', <its config>, *argv]`) |
| `FetchError`, `Unreachable`                      | the errors                                                                         |
| `default_cache()`, `PART_AGE`, `CACHE_DAYS`      | the defaults                                                                       |

`config` is the path of the pin config file. `cache` is the cache folder; a project that used its own cache folder of
the same layout (`<cache>/<sha>/<action>/<file>`) can pass it, and its checked copies are used as they are. The cache folder is kept private, by one rule in this
module and in the bootstrap below: a missing one is made 0700 (two first runs at once both get it), and one that is a
symlink, not a folder, another user's, or group- or world-writable is refused before anything in it is read or
written (`FetchError` here, `ValueError` in the bootstrap; remove it and the next run makes it again). It is never
chmodded: someone else could already have put something in it. Only a regular file is read, and prune never follows a
symlink or opens a special file. Missing folders above it are made 0700 too, by both (so a first run under umask 002
does not make a `~/.cache` that `run` then refuses). `run` and
`load()` also check every folder above the cache (see above): `load()`'s module has `__file__`
`<cache>/<sha>/<action>/<file>`, so its `../../..` is the folder above the cache. `path()` does not: it runs nothing.
Two limits, where the checked bytes still decide what runs: a folder above the cache can change after `run` or `load()`
checked it (only its owner, you or root, can change it); and where there are no owners (Windows) only "a real folder"
is checked, which a junction passes.

## Loading this module: the bootstrap

A project needs this module before it can fetch anything, so it copies the function below once into its own adapter
(for example `scripts/fork_ci.py`) and lists `pinned/pinned.py` in its config's `files`. The bootstrap does only the
check-then-load for this one file, in the same cache layout; everything else (prune, the other files, `run`) is this
module's. `pinned.test.py` runs this exact block.

The bootstrap does **not** check the folders above the cache as `run` and `load()` do: that check (owners, modes, your
private group, ACLs, the real path, folders you cannot enter) is too long to copy, and a shorter one would either refuse
Ubuntu's usual 0775 `~/.cache` or drift from this module's rule. So keep `CACHE` under your home, as the adapter
below does, never under `/tmp`. There the bootstrap makes the cache (0700) and writes `pinned.py` (the checked bytes,
which load nothing next to them), then `P.load` and `P.main([... 'run' ...])` refuse the cache before they fetch or
run anything else.

<!-- bootstrap:start -->

```python
import hashlib, json, os, re, stat, tempfile, types, urllib.request


def load_pinned(config, cache):
    """fork-ci/actions/pinned/pinned.py at the config's sha, checked by its blob id (fork-ci/actions/pinned/README.md).
    Raises OSError (no network, no good copy) or ValueError (a bad config, bytes that are not the config's, or a cache
    folder others could write)."""
    with open(config, encoding='utf-8') as f:
        c = json.load(f)
    if not isinstance(c, dict) or not isinstance(c.get('files'), dict):
        raise ValueError(f'{config}: not a pin config object with "files"')
    want, repo, sha = c['files'].get('pinned/pinned.py'), c.get('repo'), c.get('sha')
    if not (isinstance(repo, str) and re.fullmatch(r'[\w.-]+/[\w.-]+', repo) and '.' not in repo.split('/')
            and '..' not in repo.split('/') and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)
            and isinstance(want, str)):
        raise ValueError(f'{config}: needs "repo", "sha" (40 hex) and "files" with "pinned/pinned.py"')
    if not os.path.lexists(cache):
        missing, up = [], os.path.dirname(os.path.abspath(cache))
        while not os.path.lexists(up):
            missing.append(up)
            up = os.path.dirname(up)
        for up in reversed(missing):   # 0700 like the cache, whatever the umask: pinned's run checks the folders above
            try:
                os.mkdir(up, 0o700)
            except FileExistsError:
                pass
        try:
            os.makedirs(cache, mode=0o700)
        except FileExistsError:   # another run made it first: the check below decides
            pass
    st = os.lstat(cache)   # checked before anything in it is read or written
    if not stat.S_ISDIR(st.st_mode) or hasattr(os, 'getuid') and (st.st_uid != os.getuid() or st.st_mode & 0o022):
        raise ValueError(f'cache folder {cache} is not a folder of your own that only you can write (a symlink, '
                         'another user\'s, or group- or world-writable): remove it or pass another')
    dest = os.path.join(cache, c['sha'], 'pinned', 'pinned.py')
    blob = lambda b: hashlib.sha1(b'blob %d\0' % len(b) + b).hexdigest()
    try:   # a regular file only: no symlink followed, no FIFO waited on
        flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0) | getattr(os, 'O_BINARY', 0)
        with os.fdopen(os.open(dest, flags), 'rb') as f:
            data = f.read() if stat.S_ISREG(os.fstat(f.fileno()).st_mode) else None
    except OSError:
        data = None
    if data is None or blob(data) != want:
        url = f'https://raw.githubusercontent.com/{c["repo"]}/{c["sha"]}/fork-ci/actions/pinned/pinned.py'
        with urllib.request.urlopen(url, timeout=60) as r:
            data = r.read()
        if blob(data) != want:
            raise ValueError(f'{url} has blob id {blob(data)}, but {config} says {want}')
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(dest), prefix='.part-')
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
        os.replace(tmp, dest)   # whole or not at all; pinned.prune removes a killed write's .part- file
    mod = types.ModuleType('fork_ci_pinned')
    mod.__file__ = dest
    exec(compile(data, dest, 'exec'), mod.__dict__)   # the checked bytes, never a second read
    return mod
```

<!-- bootstrap:end -->

A thin adapter then looks like this:

```python
CONFIG = os.path.join(ROOT, '.github/fork-ci.json')   # project facts
CACHE = os.path.join(os.path.expanduser('~/.cache'), 'my-project-fork-ci')
P = load_pinned(CONFIG, CACHE)
docs_scope = P.load(CONFIG, 'docs-scope', 'docs_scope', CACHE)
sys.exit(P.main(['--config', CONFIG, '--cache', CACHE, *sys.argv[1:]]))   # its `run` and `files`
```

## Tests

`pinned.test.py` (no network: every fetch is served by the test). It runs in the fork's CI
(`.github/fork-ci-tests.txt`): `python3 fork-ci/actions/pinned/pinned.test.py`.
