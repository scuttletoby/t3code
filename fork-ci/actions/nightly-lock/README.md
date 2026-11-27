# nightly-lock

A composite action that **runs a heavy command while it holds a lock on the machine**, so long and short heavy steps
of scheduled runs on one shared CI machine (for example a weekly mutation run and a nightly soak test) never run at
the same time. The short step passes `first: true` and goes before every step that waits without it.

- **Who goes first.** A `first` caller waits in the kernel and holds a "waiting" mark meanwhile; a caller without
  `first` takes the lock only when it is free and no `first` caller waits (it looks every 30 s and says at its first
  look why it waits). So a `first` caller waits at most for the one step that holds the lock now.
- **A bounded wait.** Set `wait` (seconds) or `until` (an epoch second, for example the end of a nightly window). When
  it runs out the command does not run, the step exits 75, and the output `gave_up` says why and who held the lock. A
  shared `concurrency` group would instead cancel a waiting run silently.
- **Held until everything ended.** The command runs in its own process group with the lock tool as its child
  subreaper. A cancel's signal goes to that group; what the command leaves running gets SIGTERM, 30 s later SIGKILL,
  and only then is the lock let go. Exit code: the command's (128+N when signal N ended it).
- **No stale lock.** The locks are flocks, so the kernel frees them when the holder ends (a cancelled job, a killed
  runner, a reboot). Missing lock files or folder are made again; files an older version left are used as they are.
  `NAME.holder` only names the last holder in the log and decides nothing.

It needs Linux and Python 3 (standard library only). The lock is per machine: only steps on the same machine (one
self-hosted runner host, its `dir`) share it.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
jobs:
  soak: # short: goes first
    runs-on: [self-hosted, example-heavy]
    timeout-minutes: 300 # the lock wait (240) + the test, so a long wait is a red step that names the holder
    steps:
      - uses: actions/checkout@<sha>
      - id: lock
        uses: scuttletoby/t3code/fork-ci/actions/nightly-lock@<release sha> # ci/vN
        with:
          name: example-nightly # every step that shares the lock passes the same
          first: "true"
          wait: "14400"
          run: ./gradlew soakTest
      - if: failure() && steps.lock.outputs.gave_up != ''
        run: echo "did not run, not a test failure: ${{ steps.lock.outputs.gave_up }}"

  mutation: # long: waits for every soak
    runs-on: [self-hosted, example-heavy]
    steps:
      - uses: actions/checkout@<sha>
      - uses: scuttletoby/t3code/fork-ci/actions/nightly-lock@<release sha> # ci/vN
        with:
          name: example-nightly
          until: ${{ env.STOP_AT }} # an epoch second: the night's end
          run: python3 tools/mutate.py
```

| Input   | Default    | Meaning                                                                               |
| ------- | ---------- | ------------------------------------------------------------------------------------- |
| `run`   | (required) | the bash script to run while holding the lock (`bash -eo pipefail`, the job's folder) |
| `first` | `false`    | `true`: the short step, let in before every caller without it                         |
| `wait`  | (empty)    | wait at most this many seconds; set exactly one of `wait` and `until`                 |
| `until` | (empty)    | wait at most until this epoch second (already passed: nothing runs, exit 75)          |
| `name`  | `nightly`  | the lock's name: files `NAME.lock`, `NAME-first.lock`, `NAME.holder` in `dir`         |
| `dir`   | `/tmp`     | the folder of the lock files on the machine; made if missing                          |

| Output    | Value                                                                                |
| --------- | ------------------------------------------------------------------------------------ |
| `gave_up` | set only when the wait ran out (exit 75, nothing ran): one line, why and who held it |

Exit codes: the command's; 75 the wait ran out (nothing ran); 71 the lock cannot be used (the folder is a file or a
broken link; nothing ran); 2 bad inputs.

**Moving from a project's own copy:** pass the copy's lock name as `name` (and its folder as `dir`), so the copy and
the action are one lock while both are in use; then delete the copy.

## Outside a workflow

`nightly_lock.py` is also a command (for example through [pinned](../pinned/README.md)):

```sh
python3 -I nightly_lock.py [--name NAME] [--dir DIR] [--first] (--wait S | --until EPOCH) \
    [--gave-up-note FILE] [--github-output] -- <command...>
```

`--gave-up-note FILE` writes the give-up line to a file (removed at the start, so an old note never stays).

## Tests

```sh
python3 fork-ci/actions/nightly-lock/nightly_lock.test.py
```

Real processes and real flocks in a temporary folder. The test that a waiting `first` caller keeps another step out
of a free lock starts counting only once the tool says it has looked and seen that caller, and the give-up test reads
the reason from the tool's last look, so a slow machine cannot skip the case.
