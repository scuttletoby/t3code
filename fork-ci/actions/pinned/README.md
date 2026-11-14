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
  (14). Only 40-hex folders are ever removed; the pinned sha's own folder stays, however old.
- `load()` and `run` run the bytes that were checked, never a second read of the cache file (another job on the
  runner may write it between the check and the run).
- A file the config does not name, a bad config, or no network and no good copy: `FetchError` (its subclass
  `Unreachable` for the network alone). Nothing is ever guessed.

## Command

```sh
python3 -I pinned.py --config .github/fork-ci.json [--cache DIR] run <action> <file> [args...]
python3 -I pinned.py --config .github/fork-ci.json files
```

- `run` runs `python3 -I` on a private copy (a fresh 0700 folder) of the checked bytes, with the arguments after the
  file name, and exits with its exit code. A fetch or check failure exits 1 (`pinned: <why>` on stderr).
- `files` prints the `files` map at the config's `sha`, each file fetched and hashed (nothing is checked: this is how
  the map is made).
- `--cache` defaults to `$XDG_CACHE_HOME/fork-ci-pinned` (`~/.cache/fork-ci-pinned`).

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
the same layout (`<cache>/<sha>/<action>/<file>`) can pass it, and its checked copies are used as they are. The cache folder must be private: one another user owns
or others can write is refused (FetchError), and prune never follows a symlink or opens a special file in it.

## Loading this module: the bootstrap

A project needs this module before it can fetch anything, so it copies the function below once into its own adapter
(for example `scripts/fork_ci.py`) and lists `pinned/pinned.py` in its config's `files`. The bootstrap does only the
check-then-load for this one file, in the same cache layout; everything else (prune, the other files, `run`) is this
module's. `pinned.test.py` runs this exact block.

<!-- bootstrap:start -->

```python
import hashlib, json, os, re, tempfile, types, urllib.request


def load_pinned(config, cache):
    """fork-ci/actions/pinned/pinned.py at the config's sha, checked by its blob id (fork-ci/actions/pinned/README.md).
    Raises OSError (no network, no good copy) or ValueError (a bad config, or bytes that are not the config's)."""
    with open(config, encoding='utf-8') as f:
        c = json.load(f)
    if not isinstance(c, dict) or not isinstance(c.get('files'), dict):
        raise ValueError(f'{config}: not a pin config object with "files"')
    want, repo, sha = c['files'].get('pinned/pinned.py'), c.get('repo'), c.get('sha')
    if not (isinstance(repo, str) and re.fullmatch(r'[\w.-]+/[\w.-]+', repo) and '.' not in repo.split('/')
            and '..' not in repo.split('/') and isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{40}', sha)
            and isinstance(want, str)):
        raise ValueError(f'{config}: needs "repo", "sha" (40 hex) and "files" with "pinned/pinned.py"')
    dest = os.path.join(cache, c['sha'], 'pinned', 'pinned.py')
    blob = lambda b: hashlib.sha1(b'blob %d\0' % len(b) + b).hexdigest()
    try:
        with open(dest, 'rb') as f:
            data = f.read()
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
