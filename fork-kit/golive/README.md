# golive: move a T3 boot service to a new build, safely

`t3-golive` moves a T3 home that runs as a boot service from its current build to a newer one (for example a
fork build), with a cold copy of the data first and a way back. It is built in steps; this folder has the
**core** and the **copy** so far:

| Part              | File                      | What it does                                                 |
| ----------------- | ------------------------- | ------------------------------------------------------------ |
| config            | `lib/golive/config.py`    | reads the `golive` section of the machine file               |
| lock and journal  | `lib/golive/journal.py`   | one run at a time; each step journalled "started" and "done" |
| live T3           | `lib/golive/live.py`      | finds the running server from its own files and process      |
| `check`, `settle` | `lib/golive/check.py`     | read only: may this home move to the target build now?       |
| OS adapter        | `lib/golive/osadapter.py` | systemd user units only; other systems refuse until written  |
| `copy`, restore   | `lib/golive/coldcopy.py`  | the practice stop: a cold copy; the restore primitive        |

Later parts (rehearse, switch, back) come in their own PRs. Install from `ci-release` comes with them.

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
- the machine file section is complete, and no golive run needs settling (see below).

Exit 0: ready (the plan is printed), or nothing to do, and only when no check failed: a refusal always wins.
"Nothing to do" is either "the live server, started by the boot service, already runs the target" or "no
database, and nothing ever ran here as a service" (no boot service, no runtime files, no markers: use the normal
installer). A half switch (the service state names the target, no server of it runs) and a half restore (no
database, yet the home was a service: a copy moved aside is named) are refused and named. Exit 1: refused, with
every reason. Exit 2: the kit cannot run here (no or bad machine file, or no adapter for this OS).
`--json` prints the result as JSON, a service-manager error included.

The later `switch` runs this check inside its own run: `check.run_check(cfg, adapter, own_run=run)` takes no
lock of its own (the run holds it exclusively, so there is no gap) and does not count that run as unfinished.
A run that does not hold the lock on this `stateDir` is refused (`ValueError`), so this opens the lock to no one
else.

## `t3-golive copy [--owner-says-go]`: the practice stop

The first change the kit makes, and a rehearsal of the stop the switch will need. It runs detached (a transient
user unit, printed with how to follow it), because stopping T3 kills everything inside its service, a T3
terminal included; `--here` runs it in the calling process and is refused inside the boot service. In order:

1. **Checks** (no change yet): `check` must pass (or say nothing to do: then it refuses), the backup disk must
   hold the copy plus the floor, the `stateDir` disk must keep the floor (the journal and guard log live there),
   and a quiet command must be set unless the owner says go.
2. **Quiet point** (no change yet): `quietCommand` (the fork setting `automaticUpdatesQuietCommand`'s contract:
   absolute path, no arguments, run in the home with both versions in its environment, exit 0 = quiet) must say
   quiet twice, 60 s apart, within `quietWaitSeconds`; else it refuses. `--owner-says-go` skips the wait. Then
   free space is checked again, and the live server is read again: if its pid, its version, the service
   state's `activeVersion` or a pending update changed during the wait (the auto-updater keeps the same quiet
   contract and may have moved T3 to a newer build), it refuses.
3. **Guard**: a second detached unit (`t3-golive guard`) waits for this run to end however it ends; if the run
   stopped T3 and left it stopped (killed, crashed), it starts T3 again, and it runs the post hooks if the run
   ran the pre hooks but not the post hooks. If it cannot read the journal it fails open: it starts T3 if the
   service is inactive and runs the post hooks, and logs why. It logs to `<stateDir>/guard.log` (first a
   "waiting" line, then what it did) and writes no journal line. The run goes on only after it sees the
   guard's "waiting" line (within 30 s); else it refuses and says where to look
   (`journalctl --user -u <guard unit>` and `<stateDir>/guard.log`).
4. **Pre hooks** (`preHooks`, in order), then one more quiet sample, the live server read again, and the guard
   unit checked still active. Any failure runs the post hooks and refuses: T3 was never stopped. If a post
   hook fails there, the run ends "failed" and needs `settle` (jobs may still be paused).
5. **Stop** the boot service; wait until it is inactive and its server is gone; take upstream's ownership lock
   of the home (`userdata/server-owner.sqlite`), so no server can start on it during the copy; the service
   state must still name the same build, with no update pending. This state check is what keeps a copy from
   carrying the wrong version: the auto-updater names a new build there (or marks the update pending) before it
   starts it. If it changed, no copy is taken and T3 is started on whatever build the service now starts; the
   run says which.
6. **Cold copy** of `userdata/` into a new folder `<backupDir>/t3-copy-<time>-<live version>/` (mode 700; never
   over an old one): the database with its WAL, settings, attachments and the other small files, with their
   modes; links as links. Left out: `logs`, `server-runtime.json`, the ownership lock, SQLite's `-shm` (an index
   SQLite rebuilds from the WAL on the first open), and a restore's temp files and kept old files
   (`*.golive-restore-*`, `*.golive-aside-*`). Each file is hashed while it is copied and fsynced.
7. **Read-back**: both the copy and the source are read from disk again; every SHA256 must match.
8. **Manifest**, written LAST (temp file, fsync, rename): every entry with its size, mode and SHA256.
9. **Start** the same build again, **verify** a new server of the same version runs for the home, then the
   **post hooks**. These run however steps 5-8 ended. The start comes before its journal line, so a full disk
   under the journal cannot keep T3 stopped. `stopLimitSeconds` bounds steps 6-7: past it the copy gives up and
   T3 starts again. When the restart fails as well, the first error is raised and the later ones are kept as
   notes (also in the journal's end line).

**A copy folder without a finished manifest is incomplete and is never used** (`coldcopy.read_manifest`,
`verify_copy`). The kit deletes no data: each copy names the incomplete copy folders it finds
(`coldcopy.incomplete_copies`), and a refusal for free disk counts them, so the owner can remove them. A refusal before T3 stopped (every started step done) ends the run
"refused" and needs no settling; any failure after T3 stopped needs `settle`, after T3 was started again.

Kills and stops: SIGTERM is handled like a kill (the guard puts T3 back). A kill during the quiet wait changed
nothing and needs no settling. After a power loss no guard runs: the enabled service starts T3 at boot, the post
hooks never run, and the journal asks for `settle`. More in the failure table below.

Restore primitive (`coldcopy.restore(run, copy_dir, home, from_home=..., version=...)`, for the later `rehearse`
and `back`; only inside a journalled step, with the home's server stopped): takes only a copy whose manifest
names that home and live version (else `WrongCopy`), reads the copy back against its manifest first, refuses
while any server holds the ownership lock, and needs free disk for the copy's files plus a kept copy of each
home file they replace plus the floor. In order, each step fsynced:

1. temp files a restore stopped half-way left (`.<name>.golive-restore-<time>...`) are removed: a temp is only
   renamed to its real name once whole, and the copy folder still holds what it held;
2. the database family and `settings.json` are copied in under temp names and checked;
3. every home file it will replace (`-wal`, `-journal` and `-shm` included) is **kept**: copied (not hard-linked)
   to `<name>.golive-aside-<time>`, read back, then renamed to that name;
4. the home's companions (`-shm`, `-journal`, `-wal`) are removed;
5. the copies are renamed in, database first.

A stop at any point leaves the old or the new database with no WAL or its own WAL, never a WAL without its
database or with the other database, and every old file kept. Running it again finishes the job. **The caller
must keep T3 from starting until the restore step is done** (the later `rehearse` and `back` will: the service
stopped, and not started at boot before the step ends). If T3 does start after a stop in step 4 or 5 (a power
loss, then the enabled service at boot), it serves the older database without its WAL, or the copy's without
its WAL; the kept files are independent copies, so they stay exactly the old home. Settling that means running
the restore again, or putting the kept files back, with T3 stopped.

## Failure table

The state question for every row: what happens when this runs again, stops half-way, or meets existing, old or
empty state?

| Situation                                                                           | What happens                                                                                                                                                                                                                                                                                                                                                         |
| ----------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Runs again while one runs                                                           | `journal.Busy` (flock); the running one is named only if its pid is alive                                                                                                                                                                                                                                                                                            |
| Runs again after a good copy                                                        | A new folder `t3-copy-<time>-<version>`; never over an old one (refused before T3 stops if the name exists)                                                                                                                                                                                                                                                          |
| Runs again after a failed or killed copy                                            | Refused (`Unfinished`) until `t3-golive settle`; `check` names where it stopped                                                                                                                                                                                                                                                                                      |
| Killed during the quiet wait (before the first step)                                | Nothing changed; no settling needed; `check` only notes it and the next copy runs                                                                                                                                                                                                                                                                                    |
| Killed before T3 stops (guard up, hooks maybe run)                                  | No end in the journal. T3 was never stopped; the guard runs the post hooks if the pre hooks ran                                                                                                                                                                                                                                                                      |
| Killed while T3 is stopped (mid-copy, before the manifest)                          | The guard starts T3 again and runs the post hooks. The copy folder has no manifest, so it is never used; later copies name it. The next run asks for `settle`                                                                                                                                                                                                        |
| Killed during the restart                                                           | The copy is whole (manifest written). The guard starts T3 if it is not active and runs the post hooks. The next run asks for `settle`                                                                                                                                                                                                                                |
| SIGTERM (e.g. `systemctl stop` of the run's unit)                                   | Same as a kill: the guard puts T3 back                                                                                                                                                                                                                                                                                                                               |
| Power loss                                                                          | No guard after boot. The enabled service starts T3 at boot; the post hooks never run (jobs stay paused until the owner looks); the journal asks for `settle`                                                                                                                                                                                                         |
| Guard does not come up (no "waiting" line within 30 s)                              | Refused before the pre hooks; T3 never stopped; nothing to settle. The message names `journalctl --user -u <guard unit>` and `<stateDir>/guard.log`                                                                                                                                                                                                                  |
| Guard died before the last check                                                    | Post hooks run; refused; T3 never stopped; the message says where to look                                                                                                                                                                                                                                                                                            |
| Guard dies after the last check (or during the copy)                                | A known gap, not closed: T3 stops without a guard. The run itself still starts T3 again; only if the run is killed too does T3 stay down until the next boot or a manual `systemctl --user start t3code.service`                                                                                                                                                     |
| Auto-updater moved T3 during the wait or the pre hooks                              | Refused (post hooks run if the pre hooks ran); nothing copied, so no copy is labelled with the old version                                                                                                                                                                                                                                                           |
| Update lands between the last look and the stop                                     | No copy taken. T3 is started again on the build the service now starts (no wait for the old one); the run says which build runs, ends "failed" and needs settling                                                                                                                                                                                                    |
| ... the update was in its trial when T3 stopped                                     | Stopping T3 interrupts upstream's update (its launcher records `rollback-interrupted` and deals with it on the next start). The copy refuses while an update is pending, so this needs the update to begin in that same gap                                                                                                                                          |
| A crash restarted T3 on the same build before the stop                              | The copy is taken: the build, and so the label, is the same                                                                                                                                                                                                                                                                                                          |
| Error during copy or read-back (disk full, hash differs, too long)                  | T3 is started again and verified, post hooks run, no manifest, the run ends "failed" and needs settling. If the restart fails too, the first error is raised and the later one is kept as a note (also in the journal end line)                                                                                                                                      |
| Journal disk full at the restart                                                    | T3 is started before its journal line is written, so it still runs; the run ends with the error and needs settling. The `stateDir` disk must keep the floor free before the run starts                                                                                                                                                                               |
| Disk filled during the quiet wait                                                   | Free space is checked again after the wait; refused, nothing changed. The refusal names incomplete copy folders that could be removed                                                                                                                                                                                                                                |
| Not quiet within `quietWaitSeconds`                                                 | Refused, nothing changed, nothing to settle                                                                                                                                                                                                                                                                                                                          |
| Pre hook fails, or busy again after the pre hooks                                   | Post hooks run; refused; T3 never stopped; nothing to settle                                                                                                                                                                                                                                                                                                         |
| ... and then a post hook fails                                                      | Ends "failed" and needs settling (jobs may still be paused)                                                                                                                                                                                                                                                                                                          |
| A server still owns the home after the stop                                         | Gives up after the stop wait, copies nothing, starts T3, ends "failed"                                                                                                                                                                                                                                                                                               |
| No quiet command set                                                                | Refused unless `--owner-says-go`                                                                                                                                                                                                                                                                                                                                     |
| Empty home (no database, nothing ran)                                               | `check` says nothing to do, so copy refuses (nothing to protect)                                                                                                                                                                                                                                                                                                     |
| A folder in the home cannot be read                                                 | Refused before anything changes                                                                                                                                                                                                                                                                                                                                      |
| Old state: copy folder with no manifest, or files that differ from it               | `read_manifest`/`verify_copy`/`restore` raise `Incomplete`; never used, never deleted; each copy names those with no manifest                                                                                                                                                                                                                                        |
| Old state: a copy of another home or another live version                           | `read_manifest`/`plan_restore`/`restore` raise `WrongCopy`                                                                                                                                                                                                                                                                                                           |
| Old state: stale `-shm`, logs, runtime record, a restore's temp files or kept files | Left out of the copy and of its free-space need                                                                                                                                                                                                                                                                                                                      |
| Restore stops half-way (at any copy, link, unlink or rename)                        | The home holds the old or the new database, with its own WAL or none; never a WAL without its database or with the other database. Every old file is kept as an independent copy. The failed step needs `settle`; running it again removes the temp files and finishes from the untouched copy (tested at every stop point, also on a real WAL-mode SQLite database) |
| ... and T3 starts before anyone looks                                               | The caller must prevent it (see above). If it happens anyway, T3 serves an older database or the copy's without its WAL, and its writes change only the home: the kept files stay the old home (tested at every stop point)                                                                                                                                          |
| `copy --here` from a T3 terminal                                                    | Refused: stopping T3 would kill it half-way                                                                                                                                                                                                                                                                                                                          |

## `t3-golive settle --reason "<what you did>"`

After a run stopped half-way or ended with an error, look at the T3 home and put it right, then run this: it
journals your reason against that run, and changing runs may start again. On a damaged journal it moves the
file aside (`journal.jsonl.damaged-<time>`), and the next run starts a new one. A journal written by a newer kit
is left alone ("update the kit"). It holds the lock alone and writes only the kit's journal; running it again
changes nothing.

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
    "stateDir": "/path/to/the/kit/state",
    "freeFloorMb": 2048,
    "quietCommand": "/path/to/a/quiet/check",
    "preHooks": ["/path/to/pause/background/jobs"],
    "postHooks": ["/path/to/resume/background/jobs"]
  }
}
```

`stageDir` holds `SHA256SUMS`, the archive `t3-<version>-<platform>.tar.gz`, and the archive extracted into
`<stageDir>/<version>/` (`tar -xzf <archive> -C <stageDir>/<version> --strip-components=1`). `stateDir` holds
the lock and the journal; it is required, and no environment variable moves it, so a detached `switch` and a
`check` from a shell always find the same lock and journal. `stateDir`, `backupDir` and `stageDir` must be
outside the T3 home (check writes nothing there). `freeFloorMb` is optional (default 2048). For `copy`:
`quietCommand` (optional; without it `copy` needs `--owner-says-go`), `preHooks` and `postHooks` (optional
lists of absolute paths, run with no arguments; each must be safe to run twice), `quietWaitSeconds` (default
7200, at least 60: two samples 60 s apart) and `stopLimitSeconds` (default 1800). An unknown key, a
missing key, a wrong type or a newer `version` is refused by name (the shared loader, `fork-kit/common`).

## Lock and journal

`<stateDir>/lock` is an `flock`, so it is gone when its holder dies; a changing run holds it alone and writes
its pid and command into it, `check` holds it shared. `<stateDir>/journal.jsonl` is append-only: each line is
fsynced before the step it names starts and carries the hash of the line before it. A run killed half-way
leaves a run with no end; a run that ended with an error after its first step started may have left the home
just as half-applied. Either needs settling: every later `check` refuses and says where it stopped, and a
changing run refuses to start until the owner runs `settle`. (A run that failed or was killed before its first
step changed nothing and is only a note.) The rule every changing command keeps: every change is made inside a journalled
step (kit code calls `run.require_step()` first, so a change outside one is an error), and pre-checks, `check`
included, run before the first step (`run_check` refuses inside a step). A command that stops on purpose after
every step it started is done raises `journal.Refused`: the run ends "refused" and needs no settling. The guard
takes the lock with `wait=True`: it blocks until the run it watches ends. A torn last line is reported and ignored; damage anywhere else refuses, naming the
file and how to settle it; a line from a newer kit refuses with "update the kit".

## Tests

```
python3 -I -B fork-kit/golive/tests/check.test.py      # check on a fake machine
python3 -I -B fork-kit/golive/tests/journal.test.py    # lock, journal, adapter, version order
python3 -I -B fork-kit/golive/tests/copy.test.py       # copy, guard, restore on a fake machine
python3 -I -B fork-kit/golive/tests/contract.test.py   # upstream names the kit reads still exist
python3 -I -B fork-kit/common/tests/machine_file.test.py
```

The tests fake the service manager (stub `systemctl` and `systemd-run`) and the T3 home (a temporary folder,
with a copy of `sleep` standing in for the server; `copy.test.py` fakes stop and start on it too). They need no root and no systemd, and never touch a real
T3 home, unit or `~/.t3`.
