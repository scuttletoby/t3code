# free/v1: "is this machine free?"

One small contract for every tool that asks whether a machine may take more work: a fleet desktop's `fits`
(the desk kit), the placement `factsCommand`, and `heavy facts` once it exists. A machine answers with one
**free script**; every consumer reads it the same way, so CI and agents never disagree about one machine.

## The script

- Called with **no arguments**, by its **absolute path**, stdin empty, a plain environment (`PATH`, `LANG`),
  from `/`. It answers within **3 seconds** or counts as not available; its whole process group is then ended,
  and so is any process that still holds its stdout. It leaves nothing running: a child that leaves the group
  (`setsid`) and closes stdout is not seen and not ended.
- It and every folder above it belong to root (or to the user the consumer runs as) and no one else can change
  them (a sticky folder such as `/tmp` is fine). Otherwise it is not run. The path is followed one part at a
  time: the folders of the path as written are judged too, and every link on the way must belong to root or to
  that user (another user's link in `/tmp` is refused, as the kernel's `fs.protected_symlinks` would). The script
  is judged and run by the file the path leads to, resolved once. Only the mode bits are read: an ACL that lets
  others write is not seen, so do not set one. A script that could not be ended (a group the consumer may not
  signal) is waited for at most one more second, then left to finish on its own.
- **The exit code is the verdict:** `0` free, `75` busy. Anything else, a timeout, output that is not one JSON
  object (`NaN` and `Infinity` are not JSON), a higher `v`, a `free` that disagrees with the exit code, or facts
  more than 60 seconds from now = **not available**. Busy and not available both count as **not free**.
- **stdout is the detail:** one JSON object of at most 4096 bytes. Unknown fields are ignored.

| Field       | Required             | What                                                                     |
| ----------- | -------------------- | ------------------------------------------------------------------------ |
| `v`         | yes                  | `1`                                                                      |
| `free`      | yes                  | `true` or `false`, the same as the exit code                             |
| `at`        | yes                  | when the facts were taken, ISO 8601 with a zone (`2026-11-15T05:00:00Z`) |
| `reason`    | when `free` is false | short words for a person ("memory pressure 62%", "a game is running")    |
| `labels`    | no                   | names a placement rule can match (`["ci-runner"]`)                       |
| `memory`    | no                   | `budgetMb`, `reservedMb`, `freeMb`, `pressureFull10`, `swapUsedPercent`  |
| `queue`     | no                   | `running`, `waiting`                                                     |
| `leftovers` | no                   | `[{"comm", "count", "cpuPercent"}]`, busiest first                       |

```json
{
  "v": 1,
  "free": false,
  "reason": "memory pressure 62%",
  "at": "2026-11-15T05:00:00Z",
  "labels": ["ci-runner"],
  "memory": {
    "budgetMb": 4608,
    "reservedMb": 3072,
    "freeMb": 1536,
    "pressureFull10": 62,
    "swapUsedPercent": 80
  },
  "queue": { "running": 2, "waiting": 1 },
  "leftovers": [{ "comm": "python3", "count": 73, "cpuPercent": 380 }]
}
```

A consumer that needs a size (the desk `fits MB`) also treats a free answer whose `memory.freeMb` is below that
size as busy. An answer without `memory.freeMb` makes no memory claim; the consumer's own memory rule still holds.

## Pieces

- [free_contract.py](free_contract.py): the one Python reader (`judge` for an answer, `ask` to run a script, and
  `python3 -I free_contract.py ask [--need-mb MB] /path` for shell callers: exit 0 free, 75 busy, 1 not available).
- [free-fixtures.json](free-fixtures.json): the cases every reader, in any language, must agree on.
- Examples in [free/](free/):
  - [memory](free/memory): busy under memory pressure or with swap nearly full. The desk kit installs it as
    `/opt/fork-fleet/free-memory`. Pressure is "all tasks stalled" over 10 seconds, which can stay low while the
    machine swaps hard; the swap rule is there for that.
  - [busy-while-running](free/busy-while-running): busy while a named program runs (a game, say). Copy it and put
    your programs in it. It sees the processes of its own system only: in WSL, Windows programs are not visible.

Several conditions in one script: call the others from it and answer busy when one is busy.

## Using it on a desk pool

Default: no free script; `fits` uses its memory rule alone (MemAvailable minus `RESERVE_MB`). To add one, put
`"freeCommand": "/opt/fork-fleet/free-memory"` (or your own script's path) in the machine file's `desk` section
and install again: the kit writes `FREE_COMMAND` to `launch.conf`, `fits` then also needs a free answer, and
`desk-install check` shows what the script answers now.
