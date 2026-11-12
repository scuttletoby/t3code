# gradle-cache

The Maven local repository a Gradle build reads (`-Dmaven.repo.local`), built from a reviewed lock
on every run: the same bytes on a fresh runner and on a warm one, and never an older jar from `~/.m2`.

- **The lock** (a project file, reviewed in pull requests) lists every file by path and sha256.
- **The bytes** are assets of a GitHub release in the same repository, named `<sha256>-<file name>`.
  The job's own token can read them, so a runner needs no other secret.
- **The cache** is a per-runner folder of files named by sha256. It saves downloads only: the asset
  list is read on every run, so a deleted asset is red on a warm runner too.

## Use in a workflow

Pin the action by full SHA (the commit a `ci/vN` tag names):

```yaml
- id: m2
  uses: scuttletoby/t3code/fork-ci/actions/gradle-cache@<sha> # ci/vN
  with:
    lock: engine/m2-lock.json          # project fact
    cache-dir: ~/.cache/my-ci-m2       # optional: keep an existing warm cache
- run: ./gradlew --offline -Dmaven.repo.local="${{ steps.m2.outputs.maven-repo-local }}" build
```

| Input | Default | Meaning |
| --- | --- | --- |
| `lock` | required | The lock file, relative to the workspace. |
| `out` | `$RUNNER_TEMP/gradle-cache-m2` | The folder to stage into. It must be inside `RUNNER_TEMP`; it is removed first. |
| `cache-dir` | `~/.cache/fork-ci-gradle-cache` | The runner's cache. Runners on one machine may share it. |
| `cache-max-age-days` | `30` | An entry no row of this lock uses is removed after this many days unused. |
| `repository` | `github.repository` | Where the release is. |
| `token` | `github.token` | Needs `contents: read`. |

Output `maven-repo-local`: the staged folder.

## The lock

```json
{"release": "m2-jars",
 "files": [{"path": "org/example/engine/1.0/engine-1.0.jar", "sha256": "<64 hex>", "release": "m2-jars"}]}
```

A row's `release` says which release holds its asset (default: the lock's `release`). `publish`
always uploads to the lock's `release` and writes it into every row.

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
  - A second run changes nothing.
  - A row whose asset is gone or unfinished gets that asset uploaded again.
  - A path the lock already has with other bytes is refused, because a version is never reused.
- `check` downloads nothing. Run it on a schedule, so a deleted asset shows up before a build needs it.
- `prune-lock` keeps the rows of the named versions only. Name every version a build or a rollback
  may still need. It never deletes assets, so older commits still stage.

**Never delete an asset** of a lock that any commit you may re-run still uses: that commit's stage
turns red.

**A full release** (GitHub allows 1000 assets per release): set the lock's `release` to a new tag and
publish. Older rows keep their `release`.

## Troubleshooting

| Message | Cause | What to do |
| --- | --- | --- |
| `... has no asset <sha>-<file>` | The asset was deleted, or a lock row was written without its upload. | Run `publish` for that version from a machine whose `~/.m2` has the same bytes. Or upload the runner's cached copy: `cp <cache>/<sha> <sha>-<file> && gh release upload <tag> <sha>-<file>`. |
| `the upload of ... never finished` | An upload was killed. | Run `publish` for that version: it replaces the asset (`--clobber`). |
| `the asset has digest ..., the lock says ...` | Other bytes were uploaded under that name. | Delete that asset, then `publish` the version from the machine that has the locked bytes. |
| `the download has sha256 ...` | As above, for an asset with no listed digest. | As above. |
| `GitHub answered 404` | The release is gone, or the token cannot read it. | Check `permissions: contents: read`. If the release is gone, recreate it with `publish` for every locked version. |
| `... is not empty` | The script was called directly on a folder that has files. | Use the action, which empties `out` first, or pass a new folder. |
| `... cannot be read` / `no "release"` / `bad path` / `listed twice` | The lock is broken. | Fix the lock in the pull request. Nothing is staged from a lock that cannot be read. |
| `GitHub allows 1000 per release (asset limit)` | The release is full. | Set the lock's `release` to a new tag and publish again. |
| `a version is never reused` | `~/.m2` has other bytes for a version the lock pins. | Publish a new version number. |
| `out ... must be a folder inside RUNNER_TEMP` | `out` points outside the job's temp folder. | Use a path under `$RUNNER_TEMP`, or leave `out` empty. |

## Tests

```bash
python3 fork-ci/actions/gradle-cache/gradle_cache.test.py
```

The fork's `workflows` job runs this test (`.github/fork-ci-tests.txt`). No test reaches GitHub or
runs `gh`.
