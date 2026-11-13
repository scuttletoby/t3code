# gradle-cache

The Maven local repository a Gradle build reads (`-Dmaven.repo.local`), built from a reviewed lock
on every run: the same bytes on a fresh runner and on a warm one, and never an older jar from `~/.m2`.

- **The lock** (a project file, reviewed in pull requests) lists every file by path and sha256.
- **The bytes** are assets of a GitHub release in the same repository, named `<sha256>-<file name>`.
  The job's own token can read them, so a runner needs no other secret.
- **The cache** is a per-runner folder of files named by sha256. It saves downloads only: the asset
  list is read on every run, so a deleted asset is red on a warm runner too. A cache that cannot be
  made, read or written (another user's, read-only, a full disk) is a warning, and the files are
  downloaded.

## Use in a workflow

Pin the action by full SHA (the commit a `ci/vN` tag names):

```yaml
- id: m2
  uses: scuttletoby/t3code/fork-ci/actions/gradle-cache@<sha> # ci/vN
  with:
    lock: engine/m2-lock.json # project fact
    cache-dir: ~/.cache/my-ci-m2 # optional: keep an existing warm cache
- run: ./gradlew --offline -Dmaven.repo.local="${{ steps.m2.outputs.maven-repo-local }}" build
```

| Input                | Default                         | Meaning                                                                                                                                                |
| -------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `lock`               | required                        | The lock file, relative to the workspace.                                                                                                              |
| `out`                | `$RUNNER_TEMP/gradle-cache-m2`  | The folder to stage into: `$RUNNER_TEMP/gradle-cache*` or a folder inside one. It is removed first.                                                    |
| `cache-dir`          | `~/.cache/fork-ci-gradle-cache` | The runner's cache: an absolute path or `~/...` (expanded to the runner user's home). A relative path is refused. Runners on one machine may share it. |
| `cache-max-age-days` | `30`                            | An entry no row of this lock uses is removed after this many days unused.                                                                              |
| `repository`         | `github.repository`             | Where the release is.                                                                                                                                  |
| `token`              | `github.token`                  | Needs `contents: read`.                                                                                                                                |

Output `maven-repo-local`: the staged folder.

## The lock

```json
{
  "release": "m2-jars",
  "files": [
    { "path": "org/example/engine/1.0/engine-1.0.jar", "sha256": "<64 hex>", "release": "m2-jars" }
  ]
}
```

A row's `release` says which release holds its asset (default: the lock's `release`). `publish`
uploads new versions to the lock's `release` and writes a `release` into every row; a row without
one gets the lock's only when its asset is there, otherwise `publish` refuses.

## By hand: publish, check, prune

Run these on a machine whose `~/.m2` has the version, with `gh` logged in:

```bash
S=fork-ci/actions/gradle-cache/gradle_cache.py   # from a checkout of the pinned commit
python3 -I $S publish --lock engine/m2-lock.json --repo OWNER/REPO org.example:engine:1.1   # then commit the lock
python3 -I $S check --lock engine/m2-lock.json --repo OWNER/REPO                           # needs GH_TOKEN
python3 -I $S prune-lock --lock engine/m2-lock.json --keep org.example:engine:1.1 org.example:engine:1.0
```

- `publish` uploads the version's jar, pom, module and `maven-metadata-local.xml` (no sources or
  javadoc), then writes the rows.
  - A second run changes nothing. A version named twice gets one set of rows.
  - A row whose asset is gone or unfinished gets that asset uploaded again.
  - A path the lock already has with other bytes is refused, because a version is never reused.
- `check` downloads nothing. Run it on a schedule, so a deleted asset shows up before a build needs it.
- `prune-lock` keeps the rows of the named versions only. Name every version a build or a rollback
  may still need. It never deletes assets, so older commits still stage.

**Never delete an asset** of a lock that any commit you may re-run still uses: that commit's stage
turns red.

**A full release** (GitHub allows 1000 assets per release), in this order:

1. `publish` a version the lock already has. It uploads nothing new; it writes the current
   `release` into every row that has none (a lock from before per-row releases has none).
2. Set the lock's `release` to a new tag.
3. `publish` the new version. The new release is created only when a new row needs it.

Older rows keep their `release`, and `publish` of an older version still repairs its assets in that
older release, never in the new one. If step 1 was skipped, `publish` refuses before any upload and
names the rows that have no `release` of their own; set the lock's `release` back and start at step 1.

## Troubleshooting

| Message                                                                                                    | Cause                                                                                                                                                                      | What to do                                                                                                                                                                                                                             |
| ---------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `... has no asset <sha>-<file>`                                                                            | The asset was deleted, or a lock row was written without its upload.                                                                                                       | Run `publish` for that version from a machine whose `~/.m2` has the same bytes. Or upload the runner's cached copy: `cp <cache>/<sha> <sha>-<file> && gh release upload <tag> <sha>-<file>`.                                           |
| `the upload of ... never finished`                                                                         | An upload was killed.                                                                                                                                                      | Run `publish` for that version: it replaces the asset (`--clobber`).                                                                                                                                                                   |
| `the asset has digest ..., the lock says ...`                                                              | Other bytes were uploaded under that name.                                                                                                                                 | Delete that asset, then `publish` the version from the machine that has the locked bytes.                                                                                                                                              |
| `the download has sha256 ...`                                                                              | As above, for an asset with no listed digest.                                                                                                                              | As above.                                                                                                                                                                                                                              |
| `GitHub answered 404`                                                                                      | The release is gone, or the token cannot read it.                                                                                                                          | Check `permissions: contents: read`. If the release is gone, recreate it with `publish` for every locked version.                                                                                                                      |
| `... is not empty`                                                                                         | The script was called directly on a folder that has files.                                                                                                                 | Use the action, which empties `out` first, or pass a new folder.                                                                                                                                                                       |
| `... cannot be read` / `no "release"` / `bad path` / `listed twice`                                        | The lock is broken.                                                                                                                                                        | Fix the lock in the pull request. Nothing is staged from a lock that cannot be read.                                                                                                                                                   |
| `GitHub allows 1000 per release (asset limit)`                                                             | The release is full.                                                                                                                                                       | Follow "A full release" above (publish a locked version first, then move `release`).                                                                                                                                                   |
| `... rows have no "release" of their own and the release ... has no asset for them` / `does not exist yet` | The lock's `release` was moved before every row had its own, or such a row's asset was deleted. `publish` cannot tell which older release holds it, so it changed nothing. | Moved: set `release` back and follow "A full release". Deleted: write `"release": "<tag>"` into those rows (the tag the asset was in), then `publish` the version to put it back. Never re-upload an old version into the new release. |
| `a version is never reused`                                                                                | `~/.m2` has other bytes for a version the lock pins.                                                                                                                       | Publish a new version number.                                                                                                                                                                                                          |
| `out ... must be a folder RUNNER_TEMP/gradle-cache*`                                                       | `out` is outside `RUNNER_TEMP`, is `RUNNER_TEMP` itself, or names another folder there (the runner's own `_*` folders, another tool's).                                    | Use `$RUNNER_TEMP/gradle-cache-<name>`, or leave `out` empty.                                                                                                                                                                          |
| `cache dir ... must be an absolute path`                                                                   | `cache-dir` is relative (it would be a folder in the checkout, wiped on every run).                                                                                        | Use an absolute path or `~/...`.                                                                                                                                                                                                       |
| `WARN: cache ... cannot be used / read / written`                                                          | The cache dir is another user's, read-only, or the disk is full. Stage still works.                                                                                        | Fix the folder's owner or free space; until then every run downloads.                                                                                                                                                                  |

## Tests

```bash
python3 fork-ci/actions/gradle-cache/gradle_cache.test.py
```

The fork's `workflows` job runs this test (`.github/fork-ci-tests.txt`). No test reaches GitHub or
runs `gh`.
