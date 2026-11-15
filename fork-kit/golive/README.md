# golive: move a T3 boot service to a new build, safely

`t3-golive` moves a T3 home that runs as a boot service from its current build to a newer one (for example a
fork build), with a cold copy of the data first and a way back. It is built in steps; this folder has the
**core** so far:

| Part             | File                      | What it does                                                 |
| ---------------- | ------------------------- | ------------------------------------------------------------ |
| config           | `lib/golive/config.py`    | reads the `golive` section of the machine file               |
| lock and journal | `lib/golive/journal.py`   | one run at a time; each step journalled "started" and "done" |
| live T3          | `lib/golive/live.py`      | finds the running server from its own files and process      |
| `check`          | `lib/golive/check.py`     | read only: may this home move to the target build now?       |
| OS adapter       | `lib/golive/osadapter.py` | systemd user units only; other systems refuse until written  |

Later parts (copy, rehearse, switch, back) come in their own PRs. Install from `ci-release` comes with them.

## `t3-golive check`

Read only. It never stops, starts or writes to a T3 home, never opens its database and never runs a `t3`
found in `PATH`. It writes only the kit's own lock file. It refuses, with every reason, unless:

- the boot service (`t3code.service`) is installed, enabled, running and serves this home;
- the running server is this home's own runtime (`runtime/versions/<v>/t3`), started by that service;
- no update is pending and there is no restart, stop or restore marker;
- the target version is newer than the live one;
- the target release is staged: `SHA256SUMS` lists the archive, the archive matches, and the extracted
  runtime holds exactly the archive's files;
- free disk is at least 2 x database + floor (database + floor on each disk when backups are on another one);
- the staged build's read-only report (`t3 __fork-report --base-dir <home>`, run with an empty HOME and a bare
  PATH) is ok, is of the target version, and shows no fork feature switched on;
- the machine file section is complete, and no golive run stopped half-way.

Exit 0: ready (the plan is printed), or nothing to do (already on the target, or no database: use the normal
installer). Exit 1: refused. Exit 2: the kit cannot run here (no machine file, or no adapter for this OS).
`--json` prints the result as JSON.

## Machine file

Machine facts live in the one machine file every fork kit reads (`fork-kit/common/machine_file.py`):
`/etc/fork-kit/machine.json` on Linux, owned by root and not writable by group or others (nor its folder).
There is no flag or environment variable to point elsewhere. Placeholder values:

```json
{
  "version": 1,
  "golive": {
    "home": "/path/to/the/t3/home",
    "targetVersion": "0.0.0-example.fork.1",
    "stageDir": "/path/to/the/downloaded/release",
    "backupDir": "/path/to/the/backups",
    "freeFloorMb": 2048
  }
}
```

`stageDir` holds `SHA256SUMS`, the archive `t3-<version>-<platform>.tar.gz`, and the archive extracted into
`<stageDir>/<version>/` (`tar -xzf <archive> -C <stageDir>/<version> --strip-components=1`). Optional:
`stateDir` (lock and journal; default `$XDG_STATE_HOME/fork-kit/golive` or `~/.local/state/fork-kit/golive`).
An unknown key, a missing key or a newer `version` is refused by name.

## Lock and journal

`<stateDir>/lock` is an `flock`, so it is gone when its holder dies; a changing run holds it alone and writes
its pid and command into it, `check` holds it shared. `<stateDir>/journal.jsonl` is append-only: each line is
fsynced before the step it names starts and carries the hash of the line before it. A run killed half-way
leaves a run with no end: every later `check` reports where it stopped, and a changing run refuses to start
until the command that owns that state settles it. A torn last line is reported and ignored; damage anywhere
else refuses.

## Tests

```
python3 -I -B fork-kit/golive/tests/check.test.py      # check on a fake machine
python3 -I -B fork-kit/golive/tests/journal.test.py    # lock, journal, adapter, version order
python3 -I -B fork-kit/golive/tests/contract.test.py   # upstream names the kit reads still exist
python3 -I -B fork-kit/common/tests/machine_file.test.py
```

The tests fake the service manager (stub `systemctl` and `systemd-run`) and the T3 home (a temporary folder,
with a copy of `sleep` standing in for the server). They need no root and no systemd, and never touch a real
T3 home, unit or `~/.t3`.
