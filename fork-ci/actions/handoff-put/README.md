# handoff-put and handoff-get

A pair of composite actions that hand a file from one job to a later job **of the same run**, keyed on the commit both
jobs checked out. Use it when a job that runs anyway (a build) can write a file that a light job (a check) would
otherwise have to queue for a build runner to make.

- **handoff-put** (in the producer job, after the file is written): wraps the file as
  `{"format": "fork-ci-handoff/1", "name", "sha", "payload"}`, with `sha` this checkout's HEAD, and uploads it as the
  artifact `<name>-<sha>` (pinned `actions/upload-artifact`, `retention-days` 1 by default, `overwrite: true`).
  Output `ok` is `'true'` only once the artifact is uploaded.
- **handoff-get** (in the consumer job, after its checkout): downloads `<name>-<sha>` of this run for this checkout's
  HEAD (pinned `actions/download-artifact`) and writes the payload to `path` **only when the `sha` inside the file is
  this checkout's HEAD**, the name inside is `name` and the format is `fork-ci-handoff/1`. The artifact's name alone
  is not trusted. Output `ok` is `'true'` only then.

Neither action fails its step for a hand-off it could not make or find: that is `ok` `'false'` with a `reason`, and
the consumer falls back to making the file itself. The actions check the commit, name and format only; **the project
checks the payload** (its own keys and shape) before it uses it.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)). A full example, with
the consumer's fallback, is [`handoff-caller.example.yml`](handoff-caller.example.yml) next to this file:

```yaml
jobs:
  build:
    runs-on: [self-hosted, build]
    outputs:
      handoff: ${{ steps.handoff.outputs.ok }}
    steps:
      - uses: scuttletoby/t3code/fork-ci/actions/git-clean@<release sha> # ci/vN
      - uses: actions/checkout@<sha>
      - run: ./build.sh --write out/handoff.txt
      - id: handoff
        continue-on-error: true
        uses: scuttletoby/t3code/fork-ci/actions/handoff-put@<release sha> # ci/vN
        with:
          name: my-file
          path: out/handoff.txt

  check:
    needs: build
    if: ${{ !cancelled() }}
    runs-on: ${{ needs.build.outputs.handoff == 'true' && 'ubuntu-latest' || fromJSON('["self-hosted","build"]') }}
    steps:
      - uses: actions/checkout@<sha>
      - id: handoff
        if: needs.build.outputs.handoff == 'true'
        uses: scuttletoby/t3code/fork-ci/actions/handoff-get@<release sha> # ci/vN
        with:
          name: my-file
          path: out/handoff.txt
      # steps.handoff.outputs.ok != 'true': make the file here (see the example)
```

### How the consumer picks its runner

The consumer job reads the producer job's output (the put step's `ok`) in its `runs-on`:

- **`'true'`**: the light runner. Its get step then finds the file.
- **anything else** (`'false'`, or empty): its old runner, where it makes the file itself as it did before the
  hand-off. Empty is what a producer that failed, was cancelled half-way, was cut off with its runner, or skipped the
  put step (a draft, a path filter) leaves. So compare with `== 'true'`, never `!= 'false'`: empty must mean the old
  runner.

Three rules keep a missing hand-off from turning the check red by itself:

- **`if: ${{ !cancelled() }}`** on the consumer, so it still runs (on its old runner) when the producer failed:
  GitHub counts a skipped required job as passing, and a consumer skipped with its producer would never run.
- **`continue-on-error: true`** on the put step, so a broken hand-off (a full disk, a failed upload) never fails the
  build. The step already reports such a failure as `ok` `'false'`; this also covers a runner without `python3`.
- **The consumer's job id and `name:` stay the same** on both runners (the runner is not in the name), so the
  required check is the same check.

One case cannot fall back: the producer said `'true'`, but on the light runner the get finds nothing (the artifact
expired: a re-run of the consumer alone more than `retention-days` later, or someone deleted it). The light runner
cannot make the file, so the example fails that job with "re-run all jobs": a re-run of all jobs runs the producer
again.

## Inputs and outputs

handoff-put:

| Input            | Default | Meaning                                                                           |
| ---------------- | ------- | --------------------------------------------------------------------------------- |
| `name`           |         | the hand-off's name: 1-60 of `A-Z a-z 0-9 . _ -`, starting with a letter or digit |
| `path`           |         | the file, relative to the workspace: UTF-8 text, at most 16 MiB, not a symlink    |
| `retention-days` | `1`     | days the artifact is kept (1-90)                                                  |

| Output     | Meaning                                              |
| ---------- | ---------------------------------------------------- |
| `ok`       | `'true'` only when the artifact was uploaded         |
| `artifact` | `<name>-<sha>` (empty when nothing was staged)       |
| `sha`      | this checkout's HEAD, the commit the hand-off is for |
| `reason`   | one line saying what happened                        |

handoff-get:

| Input  | Meaning                                                                                                     |
| ------ | ----------------------------------------------------------------------------------------------------------- |
| `name` | the name given to handoff-put                                                                               |
| `path` | where to write the payload, relative to the workspace; a file already there is removed first (not a folder) |

| Output   | Meaning                                                |
| -------- | ------------------------------------------------------ |
| `ok`     | `'true'` only when `path` holds this commit's hand-off |
| `sha`    | this checkout's HEAD                                   |
| `reason` | one line saying what happened                          |

Both need `git` and `python3` (standard library only) and a checkout at the workspace root. Like the other fork-ci
actions that run git, each first refuses a `.git` config that `actions/checkout` does not write
([git-clean](../git-clean/README.md)); here a refusal is `ok` `'false'`, not a failed step, and runs no git.

## State

| Case                                                              | What happens                                                                                                                                                              |
| ----------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| a re-run of the producer (same run, same commit)                  | `overwrite: true` replaces the earlier attempt's artifact (without it the upload of an existing name fails)                                                               |
| a re-run of the consumer alone                                    | the artifact of the earlier attempt is still this run's: found, while `retention-days` lasts                                                                              |
| a producer stopped half-way                                       | the file is written under a temporary name and renamed when whole, and only then is it marked staged; the upload runs only after that. No upload, no hand-off: `ok` empty |
| an upload that failed                                             | `ok` `'false'`                                                                                                                                                            |
| a consumer stopped half-way                                       | the payload is written under a temporary name and renamed when whole: `path` holds the whole payload or nothing                                                           |
| no artifact for this commit (draft, failed producer, new head)    | `ok` `'false'`, `path` absent                                                                                                                                             |
| a file in `<name>-<sha>` whose `sha` or `name` inside is another  | refused: `ok` `'false'`, `path` absent, a warning                                                                                                                         |
| a garbled, cut-short, non-JSON or other-format file, or a symlink | refused, as above                                                                                                                                                         |
| a download that reported failure, whatever it left                | not read: `ok` `'false'`                                                                                                                                                  |
| an older file at `path` before the step                           | removed first, so after the step `path` exists only when `ok` is `'true'`                                                                                                 |

Each download goes to a new empty folder under `RUNNER_TEMP`, so nothing an earlier step or job left is read.

## Trust

The artifact is the producer job's output, and the producer job runs the code of the commit under test (on a pull
request, the pull request's code). So the payload is as trusted as that code, and no more:

- **Only this run's artifacts.** handoff-get passes no `run-id`, `repository` or token to download-artifact, so it
  reads only the artifacts of the run it is in. A pull request can upload artifacts only to its own runs.
- **Only this commit.** The `sha` inside must be the consumer's HEAD; a file uploaded under the right name for an
  older or another commit is refused.
- **A pull request gains nothing it did not have.** In a `pull_request` run the consumer job runs the pull request's
  workflow and code too, so a pull request that forges the hand-off could as well change the consumer's steps. The
  check result means what it meant before.
- **Do not hand off into a job with more rights.** When the consumer has secrets or a write token that the producer
  does not (`pull_request_target`, `workflow_run`, a job behind an Environment), the payload is data from untrusted
  code: parse it as data, check its keys and shape, never run it or put it in a shell command or `$GITHUB_ENV`.

## Tests

```sh
python3 fork-ci/actions/handoff-put/handoff.test.py
```

They play each `action.yml` step by step as a runner plays a composite action, with the upload and download actions
replaced by a fake artifact store of one run: the round trip, a wrong sha inside, a missing artifact, garbled files,
a re-run of the producer (overwrite) and of the consumer alone, a producer or consumer cut off half-way, a git-clean
refusal, bad inputs, the pins, and the example's runner choice.
