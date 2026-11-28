# lanes

Several test runs can share a test server at once when they do not interfere. This folder is the general part: a
**pool** that decides whether one more lane fits on the machine, and a **claims rule** that decides who holds each
server, shared or alone. The project part is a small JSON config that names its servers and their ports. Python
standard library only; Linux (`/proc`, `flock`).

## Config

```json
{
  "servers": { "api": { "port": 4100 }, "db": { "port": 4101 } },
  "lane_mb": 800,
  "reserve_mb": 1024,
  "max_lanes": 4
}
```

- `servers`: each server's name (letters, digits, `-`, `_`) and port. Two servers cannot share a port.
- `lane_mb`: a lane's least memory: its cost is never below it, also once the lane is measured.
- `reserve_mb`: memory left free for everything else.
- `max_lanes`: never more lanes at once.

### The host's part

The project's config lives in the repository, so a pull request can edit it. What keeps the host safe comes from
the runner's environment instead:

| Variable                   | Meaning                                             | Default                                                              |
| -------------------------- | --------------------------------------------------- | -------------------------------------------------------------------- |
| `FORK_CI_LANES_DIR`        | the one folder of claims and measurements, absolute | `$XDG_STATE_HOME/fork-ci-lanes`, else `~/.local/state/fork-ci-lanes` |
| `FORK_CI_LANES_MAX_LANES`  | the host's most lanes at once                       | 4                                                                    |
| `FORK_CI_LANES_RESERVE_MB` | the host's least memory kept free                   | 1024                                                                 |
| `FORK_CI_LANES_LANE_MB`    | the host's least cost of a lane                     | 800                                                                  |

A project's config can only tighten these: the lower `max_lanes`, the higher `reserve_mb` and `lane_mb`. A config
that names `claims_dir` is refused, and so is a relative `FORK_CI_LANES_DIR` (it would be one folder per working
folder, and lanes in two checkouts would not see each other).

Every lane on a host must use one folder. When the runners are different users, set `FORK_CI_LANES_DIR` for every
runner to one folder they share: group-writable (for example `chmod 2775`, one group for all runner users) and not
sticky (`+t` is refused: one user could not remove another's dead claim). The lock is taken on a read-only open, and
every file is written readable to all, whatever a runner's umask. Without the setting each user would get the default
folder in its own home, and their lanes would not see each other, so a lane without it is refused (exit 2) while
another user's `pool.py` runs (found by its command line, which every user can read unless `/proc` hides other
users' processes). Server names are shared in that folder, so two projects on one host name
their servers apart. Lanes that share a folder must also share one process view (pid namespace): a holder is
checked by its pid. The pool is a cooperative rule between the host's own runners, not a security boundary.

## Running a lane

Pin a release commit as for the actions ([why](../README.md#pinning)), then:

```sh
python3 -I <fork-ci>/lanes/pool.py run --config .github/lanes.json --lane "$LANE" --server api --server db \
  -- ./run-tests.sh
```

It waits, polling, until each named server is claimed and the pool admits the lane. It then runs the command with
`LANE_ID` and `LANE_PORT_<NAME>` set (`LANE_PORT_DB=4101`; `-` becomes `_`). It releases every claim once none of
the lane runs: when the command ends, what it left running (a child, a server in the background) is stopped
(SIGTERM, SIGKILL 10 s later) first. On SIGTERM, SIGINT or SIGHUP every process of the lane is stopped the same way,
then the exit is 128 + the signal (a signal that comes before the command starts stops it as it starts). A process
that still runs 5 s after its SIGKILL keeps the servers held until it ends. Under an outer `timeout`,
give `-k` at least 10 s (`timeout -k 15 ...`), so `pool.py` stops its command and releases before it is killed
itself. Then it records the lane's peak memory: its largest process, or its process group's sampled total when that
is larger. `--alone` claims
the servers alone (a restart, a reset of their data). `--restart` (with `--alone`) also queues it first in line.
`--wait-s` (default 3600) gives up with exit 75. A config it cannot use, an unknown server or no command exits 2.
Give `--lane` an id that stays the same from run to run (the job's name, not its run number): its measurements are
kept by it.

A lane holds all of its servers or none: when one claim is refused, it gives back the claims it already holds and
tries again later, so two lanes never deadlock over two servers. A refused restart waits first in line on every one
of its servers, each wait with the time the lane began waiting, so the oldest waiting lane is first on all the
servers it needs and a younger lane cannot take one of them meanwhile.

**Its process group.** The lane runs in a process group that `pool.py` leads (it makes one when it does not lead
one, so the caller's group never holds its claims; when the terminal was the caller's, the new group takes it, so
Ctrl-C still reaches the lane, and gives it back at the end). Its claims stay alive while any process of that group
runs. A `pool.py` killed
outright (SIGKILL: nothing released) takes its command with it (the kernel kills the command when `pool.py` dies),
and keeps its servers until the processes that command started end, so no alone lane gets a server under them. Such
a process left hanging holds its servers until it is stopped (the runner stops a job's processes at the job's end).
A command that moves its own processes to another group or session takes them out of
this check. A signal sent to the caller's process group does not reach the lane: send it to `pool.py`.

## Admission (pool.py)

A lane starts only when fewer than `max_lanes` run and the free memory measured now (`MemAvailable`) minus
`reserve_mb`, minus what the lanes already admitted have not used yet, holds one more lane. A lane started a moment
ago is not in `MemAvailable` yet, so each claim notes the lane's cost, and a running lane counts its cost less the
memory its process group uses now. Four lanes started together on room for two let in two. A lane's cost is the
largest of `lane_mb` and its own last 20 measured peaks. The peaks are kept in the host's folder by the config file's
path and the lane id, so one project's light lanes never lower another project's cost. Lanes are counted by holder and lane
name, so two runners that pass one `--lane` are two lanes. When free memory or the measurements cannot be read, a
lane starts only when no other lane runs (one at a time); the next measurement starts the file afresh.

## Claims (claims.py)

| held \ wanted | shared   | alone |
| ------------- | -------- | ----- |
| shared        | together | waits |
| alone         | waits    | waits |

- **A waiting restart goes first.** An alone claim with `restart=True` that cannot be granted leaves a wait entry.
  While it waits, the server grants no new claim, shared or alone, to anyone but the oldest waiter, so a stream of
  shared runs cannot starve it.
- **Liveness and sweep.** A holder is a process, `{"pid", "start", "pgid"}` (its start time from `/proc`, so a
  reused pid is another process; the process group it leads, alive while any process of it runs), or a systemd unit,
  `{"unit"}` (gone when it is `inactive` or `failed`). Every call first removes the claims and waits whose holder is
  gone. A holder it cannot check counts as alive.
- **One lock.** Every call reads and writes under `flock` on `<folder>/.lock`. A claim file is written whole (a
  temporary file renamed into place); a temporary file a crashed writer left is removed at the next call.
- **Garbage is swept, a newer release is judged by its holder.** A claim file this version does not write (junk, a
  wrong shape, a holder with no start time) is removed: it would hold a server for ever. A field it does not know is
  kept and ignored, so a claim is judged by the fields it knows: a release that changes what a claim means (who holds
  a server, the holder's shape, or how its holder is found alive) must raise `v`. A folder it cannot read refuses
  every claim and names the reason. A claim from a newer release (its `v` above this one's) is judged by its holder
  only, the way every release finds one alive: a holder that is gone is swept. A live holder, or one this version
  cannot judge, refuses every claim on the server the claim names (`pool.py` waits, then exits 75); a claim that
  names no server holds nothing and is left alone. Its lane is not counted against `max_lanes`. An empty folder
  holds nothing.

The API: `Claims(folder).claim(server, mode, holder, lane, restart=False, admit=None, keep_wait=False, since=None,
note=None)` returns `(token, why)`, where `token` is `None` when refused. `admit(lanes)` gets one live claim per
lane, with its `note`. `keep_wait` keeps a restart's wait after a grant (a lane that still needs another server).
The other calls are `release(token)`, `queue(server, holder, lane, since=None)` (wait first in line without a
claim), `withdraw(server, holder, lane)` (stop waiting), `sweep()` and `holders(server=None)`.

## The contract tests

[`contract.py`](contract.py) is one test class, `LaneContract`. A project that puts its own code in front of the
claims (its lane ids, its server names, a unit as the holder) runs it against that code, as its docstring shows.
The class covers the conflict table, a waiting restart that is not starved, the oldest waiter first, a dead holder
or waiter swept, an empty, missing or unreadable folder, planted or garbage claim files swept, a newer release's
claim (live, dead, or naming no server), a crashed writer's temporary file, lanes counted by holder, a restart's place kept on every server, a
refused admission and two users of one folder, and a claim with a field this version does not know. The tests control the clock and who is alive: no test sleeps.

## Tests

`python3 -B fork-ci/lanes/lanes.test.py`: the contract against `claims.Claims`, real process liveness, admission,
the host's part of the config, a folder shared by runner users, stopping, and real lanes through `pool.py run` (one
killed with SIGKILL while its command runs, one killed while it stops a command that ignores SIGTERM, one stopped
while its command's child runs, one whose command leaves a process running, one with a terminal as its input, one
beside newer releases' claims).
