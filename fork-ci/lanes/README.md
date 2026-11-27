# lanes

Several test runs can share a test server at once when they do not interfere. This folder is the general part: a
**pool** that decides whether one more lane fits on the machine, and a **claims rule** that decides who holds each
server, shared or alone. The project part is a small JSON config that names its servers and their ports. Python
standard library only; Linux (`/proc`, `flock`).

## Config

```json
{
  "claims_dir": ".lanes",
  "servers": { "api": { "port": 4100 }, "db": { "port": 4101 } },
  "lane_mb": 800,
  "reserve_mb": 1024,
  "max_lanes": 4
}
```

- `claims_dir`: the folder of claims and measurements, shared by every lane on the machine (a relative path is
  from the config's folder). Missing: made on first use.
- `servers`: each server's name (letters, digits, `-`, `_`) and port. Two servers cannot share a port.
- `lane_mb`: a lane's memory before one is measured (default 800).
- `reserve_mb`: memory left free for everything else (default 1024).
- `max_lanes`: never more lanes at once (default 4).

## Running a lane

Pin a release commit as for the actions ([why](../README.md#pinning)), then:

```sh
python3 -I <fork-ci>/lanes/pool.py run --config .github/lanes.json --lane "$LANE" --server api --server db \
  -- ./run-tests.sh
```

It waits, polling, until each named server is claimed and the pool admits the lane. It then runs the command with
`LANE_ID` and `LANE_PORT_<NAME>` set (`LANE_PORT_DB=4101`; `-` becomes `_`). It releases every claim when the
command ends, and also on SIGTERM or SIGINT (the command is stopped first, then the exit is 128 + the signal). Then
it records the lane's peak memory (its largest process). `--alone` claims the servers alone (a restart, a reset of
their data). `--restart` (with `--alone`) also queues it first in line. `--wait-s` (default 3600) gives up with exit 75. A config it cannot use, an unknown server or no command exits 2.

A lane holds all of its servers or none: when one claim is refused, it gives back the claims it already holds and
tries again later, so two lanes never deadlock over two servers.

## Admission (pool.py)

A lane starts only when fewer than `max_lanes` run and the free memory measured now (`MemAvailable`) minus
`reserve_mb` holds one more lane. A lane's cost is the largest peak of the last 20 measured lanes, or `lane_mb`
before any is measured. When free memory cannot be read, a lane starts only when no other lane runs (one at a time).

## Claims (claims.py)

| held \ wanted | shared   | alone |
| ------------- | -------- | ----- |
| shared        | together | waits |
| alone         | waits    | waits |

- **A waiting restart goes first.** An alone claim with `restart=True` that cannot be granted leaves a wait entry.
  While it waits, the server grants no new claim, shared or alone, to anyone but the oldest waiter, so a stream of
  shared runs cannot starve it.
- **Liveness and sweep.** A holder is a process, `{"pid", "start"}` (its start time from `/proc`, so a reused pid
  is another process), or a systemd unit, `{"unit"}` (gone when it is `inactive` or `failed`). Every call first
  removes the claims and waits whose holder is gone. A holder it cannot check counts as alive.
- **One lock.** Every call reads and writes under `flock` on `<claims_dir>/.lock`. A claim file is written whole
  (a temporary file renamed into place).
- **Fails closed.** A folder it cannot read, or a claim file it cannot parse, refuses every claim and names the
  reason. An empty folder holds nothing.

The API: `Claims(folder).claim(server, mode, holder, lane, restart=False, admit=None)` returns `(token, why)`,
where `token` is `None` when refused. The other calls are `release(token)`, `withdraw(server, holder, lane)` (stop
waiting), `sweep()` and `holders(server=None)`.

## The contract tests

[`contract.py`](contract.py) is one test class, `LaneContract`. A project that puts its own code in front of the
claims (its lane ids, its server names, a unit as the holder) runs it against that code, as its docstring shows.
The class covers the conflict table, a waiting restart that is not starved, the oldest waiter first, a dead holder
or waiter swept, an empty, missing or unreadable folder, a claim file it cannot parse, a refused admission and two
users of one folder. The tests control the clock and who is alive: no test sleeps.

## Tests

`python3 -B fork-ci/lanes/lanes.test.py`: the contract against `claims.Claims`, real process liveness, admission,
the config, and real lanes through `pool.py run`.
