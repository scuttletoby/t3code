# golive: move a T3 boot service to a new build, safely

`t3-golive` moves a T3 home that runs as a boot service from its current build to a newer one (for example a
fork build), with a cold copy of the data first and a way back. It is built in steps; this folder has the
**core**, the **copy**, the way **back** and **status** so far:

| Part              | File                        | What it does                                                 |
| ----------------- | --------------------------- | ------------------------------------------------------------ |
| config            | `lib/golive/config.py`      | reads the `golive` section of the machine file               |
| lock and journal  | `lib/golive/journal.py`     | one run at a time; each step journalled "started" and "done" |
| live T3           | `lib/golive/live.py`        | finds the running server from its own files and process      |
| `check`, `settle` | `lib/golive/check.py`       | read only: may this home move to the target build now?       |
| OS adapter        | `lib/golive/osadapter.py`   | systemd user units only; other systems refuse until written  |
| `copy`, restore   | `lib/golive/coldcopy.py`    | the practice stop: a cold copy; the restore primitive        |
| `back`, `status`  | `lib/golive/back.py`        | the way back (W1-W3); read only: is T3 up and right?         |
| kept runtime      | `lib/golive/keptruntime.py` | the old build's runtime, kept for W1 (upstream prunes it)    |

Later parts (rehearse, switch) come in their own PRs. The owner's runbook is `docs/fork/go-live.md`. Install from `ci-release` comes with them.

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
  PATH) is ok, is of the target version, and shows no fork feature switched on. It runs only beside a T3 that the
  checks above found running steadily (service active, its own server live, no update pending, no marker): a
  report reading the home while T3 stops keeps SQLite from tidying its WAL at the stop (nothing is lost, but the WAL
  with its frames and `-shm` stay, and a copy takes that WAL without its `-shm`). Otherwise it is not run, and says
  so. The live server is read again after it: if T3 stopped, restarted or began an update meanwhile (the owner or
  the auto-updater), the report's answer is not used and check refuses;
- the machine file section is complete, and no golive run needs settling (see below).

Exit 0: ready (the plan is printed), or nothing to do, and only when no check failed: a refusal always wins.
"Nothing to do" is either "the live server, started by the boot service, already runs the target" or "no
database, and nothing ever ran here as a service" (no boot service, no runtime files, no markers: use the normal
installer). A half switch (the service state names the target, no server of it runs) and a half restore (no
database, yet the home was a service: a copy moved aside is named) are refused and named. Exit 1: refused, with
every reason. Exit 2: the kit cannot run here (no or bad machine file, or no adapter for this OS).
`--json` prints the result as JSON, a service-manager error included.

**Counts: report against report.** The report counts every project and every thread that is not deleted:
sub-agent and archived threads included, across all projects. `t3 thread list` lists one project and leaves
sub-agents out, so its numbers are never what a count is compared with. A later step that checks nothing was lost
compares one report with another made the same way (`live.compare_counts(before, after)`: every count named that
differs, and a count either report could not read is never equal), for example the copy's report against the
same data after the target build migrated it. `check`'s plan prints the counts it read.

The later `switch` runs this check inside its own run: `check.run_check(cfg, adapter, own_run=run)` takes no
lock of its own (the run holds it exclusively, so there is no gap) and does not count that run as unfinished.
A run that does not hold the lock on this `stateDir` is refused (`ValueError`), so this opens the lock to no one
else.

## `t3-golive copy [--owner-says-go]`: the practice stop

The first change the kit makes, and a rehearsal of the stop the switch will need. It runs detached (a transient
user unit, printed with how to follow it), because stopping T3 kills everything inside its service, a T3
terminal included; `--here` runs it in the calling process and is refused inside the boot service. In order:

1. **Checks** (no change yet): `check` must pass (or say nothing to do: then it refuses), the database must be
   a file, not a link (a copy takes files and would hold only the link), no restore may have stopped half-way
   (`userdata/golive-restore-unfinished.json`, see below), the backup disk must hold the copy plus the floor,
   the `stateDir` disk must keep the floor (the journal and guard log live there), and a quiet command must be
   set unless the owner says go.
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
(`coldcopy.incomplete_copies`) and the files earlier restores kept (`coldcopy.kept_files`, each as big as what
it kept), and a refusal for free disk counts and names both, so the owner can remove them. The database link
and the restore record are looked at again after the quiet wait and at the last look before the stop. A
refusal before T3 stopped (every started step done) ends the run
"refused" and needs no settling; any failure after T3 stopped needs `settle`, after T3 was started again.

Kills and stops: SIGTERM is handled like a kill (the guard puts T3 back). A kill during the quiet wait changed
nothing and needs no settling. After a power loss no guard runs: the enabled service starts T3 at boot, the post
hooks never run, and the journal asks for `settle`. More in the failure table below.

Restore primitive (`coldcopy.restore(run, copy_dir, home, from_home=..., version=...)`, for the later `rehearse`
and `back`; only inside a journalled step, with the home's server stopped): takes only a copy whose manifest
names that home and live version (else `WrongCopy`), reads the copy back against its manifest first, refuses
while any server holds the ownership lock, and needs free disk for the copy's files plus a kept copy of each
home file they replace plus the floor (all these checks run again under the ownership lock, just before the
first change). In order, each step fsynced:

1. temp files a restore stopped half-way left (`.<name>.golive-restore-<time>...`) are removed: a temp is only
   renamed to its real name once whole, and the copy folder still holds what it held;
2. the database family and `settings.json` are copied in under temp names, then read back from disk against
   the manifest;
3. every home file it will replace (`-wal`, `-journal` and `-shm` included) is **kept**: copied (not hard-linked)
   to `<name>.golive-aside-<time>`, read back, then renamed to that name;
4. a record, `userdata/golive-restore-unfinished.json`, is written (temp file, fsync, rename): it names the kept
   files that are the old home;
5. the home's companions (`-shm`, `-journal`, `-wal`) are removed;
6. the copies are renamed in, database first;
7. the record is removed: the restore is done.

A stop at any point leaves the old or the new database with no WAL or its own WAL, never a WAL without its
database or with the other database, and every old file kept. Running it again finishes the job. A rerun after
a stop in steps 5-7 finds the record: the home is then half-way (for example the old database file without its
WAL), not the old home, so the rerun names the old home from the record (`movedAside` in its result) and keeps
again only home files that hold something new (`alsoKept`: what a T3 started after the stop wrote). The record
is rewritten to name those too before anything leaves the home, so a third run still names them. Free disk for
a rerun counts only the files it will keep. A record that cannot be read refuses the restore before any
data changes. A record whose kept files were all moved back by hand (a kept `-shm` need not go back) is replaced,
and the home is taken as the old home (`staleRecord` in the result; `staleKept` names that record's kept files
still on disk, such as what earlier reruns kept, which stay until the owner removes them). Kept files copied back
(still there, the home holding the same bytes) leave the home exactly as after a restore stopped right after
keeping them: the record stays and a rerun keeps nothing again. A record put back only in part (say the database
moved back but not its `-wal`, or a kept file gone from both its kept name and the home, or gone while the home's
file of that name holds the copy's bytes by the copy's manifest SHA256, so it was deleted, or gone while a copy the
record names cannot be read and the home's file is not the kept bytes by the record's SHA256, so it cannot be told
whether it was moved back or deleted) refuses before any data changes, and so does one put back in full, by moving
or by copying, while the home still holds a `-wal`, `-journal` or `-shm` the old home did not have. The refusal
names each such file and whose it is:

- **the copy's** `-wal`/`-journal` (the SHA256 in the copy's manifest), left from the restore stopped half-way:
  remove it, and **do not remove the record** (next to the old database it would serve or checkpoint another
  database's pages); then run the restore again;
- **the copy's with more commits after its frames**: a `-wal` that begins with the copy's `-wal` header (salts
  included). A T3 ran on the half-way home, the copy's database, before the kept files were put back: move it out of
  userdata, **do not remove the record**, run the restore again. What that T3 wrote was on the copy's database,
  not the old home's;
- **cannot be told**: any other. A T3 that ran on the put-back home with the record still there would have written
  it (then it holds that T3's newest commits: keep it and remove the record), but so would a T3 on the half-way
  home whose WAL started over after a checkpoint (then it must go: move it out, keep the record, run the restore
  again). The refusal says how to tell them apart on scratch copies, never in the home: the database alone and with
  that `-wal`, opened with `sqlite3`, and their row counts compared. That works with the copy folder gone too. If
  still unsure: move the file out of userdata (never delete it), keep the record and run the restore again;
  nothing is lost;
- a **`-shm` with no `-wal`** of the half-way home beside it: only SQLite's index of a WAL, always safe to remove.

Finish putting the kept files back, or remove the record once the home is right. A `copy` refused for the record
names the same files in its own refusal. "Before any data changes": the restore takes the home's ownership lock first, which creates an
empty `server-owner.sqlite` when the home has none (as T3 itself does); nothing else changes. A rerun after
a stop between the record's removal and the end of the caller's step finds no record and a home that is exactly the
copy's: it keeps nothing (`alreadyRestored`), so the restored data is never named as the old home. While the record
is there, a copy refuses.

**The caller must keep T3 from starting until the restore step is done** (the later `rehearse` and `back` will:
the service stopped, and not started at boot before the step ends). If T3 does start after a stop in steps 5-7
(a power loss, then the enabled service at boot), it opens the old database file without its WAL (missing the
WAL's commits, and after a crash in the middle of a checkpoint the file alone may be torn), or the copy's
without its WAL. The kept files are independent copies, so they stay exactly the old home. Settling that means
running the restore again, or, with T3 stopped, first removing the home's `-wal`, `-journal` and `-shm`, then
putting back the kept files the record names and removing the record. Putting back drops the half-way home: if
a T3 ran on it after the stop, run the restore again instead (it keeps what that T3 wrote). The kit never
removes kept files; `kept_files` lists them, and a restore's refusal for free disk names them, with the ones the
record of a restore stopped half-way names listed apart as files that must stay.

## Failure table

The state question for every row: what happens when this runs again, stops half-way, or meets existing, old or
empty state?

| Situation                                                                           | What happens                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ----------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Runs again while one runs                                                           | `journal.Busy` (flock); the running one is named only if its pid is alive                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Runs again after a good copy                                                        | A new folder `t3-copy-<time>-<version>`; never over an old one (refused before T3 stops if the name exists)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Runs again after a failed or killed copy                                            | Refused (`Unfinished`) until `t3-golive settle`; `check` names where it stopped                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Killed during the quiet wait (before the first step)                                | Nothing changed; no settling needed; `check` only notes it and the next copy runs                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Killed before T3 stops (guard up, hooks maybe run)                                  | No end in the journal. T3 was never stopped; the guard runs the post hooks if the pre hooks ran                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Killed while T3 is stopped (mid-copy, before the manifest)                          | The guard starts T3 again and runs the post hooks. The copy folder has no manifest, so it is never used; later copies name it. The next run asks for `settle`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Killed during the restart                                                           | The copy is whole (manifest written). The guard starts T3 if it is not active and runs the post hooks. The next run asks for `settle`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| SIGTERM (e.g. `systemctl stop` of the run's unit)                                   | Same as a kill: the guard puts T3 back                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Power loss                                                                          | No guard after boot. The enabled service starts T3 at boot; the post hooks never run (jobs stay paused until the owner looks); the journal asks for `settle`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| Guard does not come up (no "waiting" line within 30 s)                              | Refused before the pre hooks; T3 never stopped; nothing to settle. The message names `journalctl --user -u <guard unit>` and `<stateDir>/guard.log`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| Guard died before the last check                                                    | Post hooks run; refused; T3 never stopped; the message says where to look                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      |
| Guard dies after the last check (or during the copy)                                | A known gap, not closed: T3 stops without a guard. The run itself still starts T3 again; only if the run is killed too does T3 stay down until the next boot or a manual `systemctl --user start t3code.service`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Auto-updater moved T3 during the wait or the pre hooks                              | Refused (post hooks run if the pre hooks ran); nothing copied, so no copy is labelled with the old version                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Update lands between the last look and the stop                                     | No copy taken. T3 is started again on the build the service now starts (no wait for the old one); the run says which build runs, ends "failed" and needs settling                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| ... the update was in its trial when T3 stopped                                     | Stopping T3 interrupts upstream's update. A stop during its trial: the launcher starts the trial again on the next start. A stop during its rollback: the launcher records `rollback-interrupted` and deals with it on the next start. The copy refuses while an update is pending, so this needs the update to begin in that same gap                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| A crash restarted T3 on the same build before the stop                              | The copy is taken: the build, and so the label, is the same                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Error during copy or read-back (disk full, hash differs, too long)                  | T3 is started again and verified, post hooks run, no manifest, the run ends "failed" and needs settling. If the restart fails too, the first error is raised and the later one is kept as a note (also in the journal end line)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Journal disk full at the restart                                                    | T3 is started before its journal line is written, so it still runs; the run ends with the error and needs settling. The `stateDir` disk must keep the floor free before the run starts                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| Disk filled during the quiet wait                                                   | Free space is checked again after the wait; refused, nothing changed. The refusal names incomplete copy folders and kept files that could be removed                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| Not quiet within `quietWaitSeconds`                                                 | Refused, nothing changed, nothing to settle                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| Pre hook fails, or busy again after the pre hooks                                   | Post hooks run; refused; T3 never stopped; nothing to settle                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                   |
| ... and then a post hook fails                                                      | Ends "failed" and needs settling (jobs may still be paused)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| A server still owns the home after the stop                                         | Gives up after the stop wait, copies nothing, starts T3, ends "failed"                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| No quiet command set                                                                | Refused unless `--owner-says-go`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| Empty home (no database, nothing ran)                                               | `check` says nothing to do, so copy refuses (nothing to protect)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                               |
| A folder in the home cannot be read                                                 | Refused before anything changes                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Old state: copy folder with no manifest, or files that differ from it               | `read_manifest`/`verify_copy`/`restore` raise `Incomplete`; never used, never deleted; each copy names those with no manifest                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  |
| Old state: a copy of another home or another live version                           | `read_manifest`/`plan_restore`/`restore` raise `WrongCopy`                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Old state: stale `-shm`, logs, runtime record, a restore's temp files or kept files | Left out of the copy and of its free-space need                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| Restore stops half-way (at any copy, link, unlink or rename)                        | The home holds the old or the new database, with its own WAL or none; never a WAL without its database or with the other database. Every old file is kept as an independent copy. The failed step needs `settle`; running it again removes the temp files and finishes from the untouched copy (tested at every stop point, also on a real WAL-mode SQLite database)                                                                                                                                                                                                                                                                                                                                                                                                                                                           |
| ... and T3 starts before anyone looks                                               | The caller must prevent it (see above). If it happens anyway, T3 opens the old database file or the copy's without its WAL, and its writes change only the home: the kept files stay the old home (tested at every stop point)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                 |
| ... and the restore runs again                                                      | Before anything leaves the home, the home is still the old home: the rerun keeps it again. After that, the record names the old home: the rerun reports that set as `movedAside`, keeps only changed home files (`alsoKept`, so a T3's writes are not lost; the record is rewritten to name them, so a third run still does), and never names the half-way home as the old one (tested at every stop point, and the stop between the WAL removal and the database rename). Free disk counts only the files the rerun keeps, so a rerun fits where the first run fit                                                                                                                                                                                                                                                            |
| ... record of the stopped restore cannot be read                                    | The restore refuses before any data changes; the owner looks at the kept files, puts the home right and removes the record                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| ... all kept files put back by hand, record left                                    | Moved back (a kept `-shm` need not go back): the record is replaced; the home is kept as the old home (`staleRecord`; `staleKept` names that record's kept files still on disk, such as what earlier reruns kept). Copied back (the kept files still there, the home holding their bytes): the same state as a restore stopped right after keeping them; the record stays and a rerun keeps nothing again                                                                                                                                                                                                                                                                                                                                                                                                                      |
| ... kept files put back only in part                                                | Refused before any data changes, by `plan_restore` too: the message names the kept files gone and those not back in the home, and a kept file deleted by mistake: gone from both its kept name and the home, or gone while the home's file of that name holds the copy's bytes (a `-shm` may go). When a copy the record names cannot be read (pruned) and the home's file is not the kept bytes, moved back and deleted cannot be told apart: refused too, with how to compare on scratch copies. Finish putting them back, or remove the record once the home is right                                                                                                                                                                                                                                                       |
| ... put back in full, a half-way `-wal`/`-journal`/`-shm` left                      | All kept files back (moved or copied), but the home still holds a `-wal`, `-journal` or `-shm` the old home did not have: refused before any data changes, by `plan_restore` and by `copy` too, naming each file and whose it is. The copy's (its manifest SHA256): remove it, do NOT remove the record, run the restore again. The copy's with a T3's commits after it (it begins with the copy's WAL header): move it out, do NOT remove the record, run the restore again. Any other cannot be told (a T3 on the put-back home, or one on the half-way home whose WAL started over): both readings, and how to tell them apart on scratch copies, the copy folder not needed; if still unsure, move it out (never delete), keep the record, run the restore again. A `-shm` with no `-wal` beside it: always safe to remove |
| ... killed after the record is removed, before the step ends                        | No record, and the home is exactly the copy's: the rerun keeps nothing (`alreadyRestored`), so the restored data is never named as the old home. The old home is the first run's kept set (`kept_files` lists it)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| A copy while a restore record is there                                              | Refused before T3 stops: the home is half-way. If the kept files are back, remove the record (first any `-wal`/`-journal` of the copy beside the old database: the refusal names them, as a restore run would); otherwise run the restore again                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                |
| `check` while T3 is stopping, stopped, or marked for a restart or update            | The report is not run (it would keep SQLite from tidying the WAL at the stop); check refuses with the other reasons                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                            |
| T3 stops, restarts or begins an update while `check`'s report reads                 | The live server is read again after the report: changed, so the answer is not used and check refuses ("T3 steady during the report"); run it again once T3 runs. Inside `copy` the report has ended before the stop step, and none runs until T3 is started again                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |
| Database is a link (upstream allows a linked database)                              | Refused before T3 stops: a copy takes files only and would hold the link, not the database                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| Kept files pile up (one set per restore, each as big as the database)               | Never copied, never removed by the kit. Each copy names and counts them; a refusal for free disk names them (a restore's refusal lists the ones the record of a stopped restore names apart, as files that must stay)                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                          |
| `copy --here` from a T3 terminal                                                    | Refused: stopping T3 would kill it half-way                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |

## `t3-golive back [--way w1|w2|w3] [--lose-work-since <time>] [--here]`

Without `--way` it is read only and says which way back is possible now, and why: W1 needs a finished copy of this
home (`manifest.json`; incomplete copies are named, never used) taken under an older build whose t3 is still whole
in `runtime/versions/<v>/` (`.install-complete`) or kept by the kit (below); W2 needs a fork build live
(`.fork.N`) and its base nightly (the version without `.fork.N`) installed in the home or staged in `stageDir` like
the target (checksums checked); W3 needs an upstream stable newer than that base, installed or staged. With `--way`
it runs detached (a transient unit; stopping or restarting T3 kills a T3 terminal), `--here` in the calling process
(refused inside the boot service).

- **W1, restore and downgrade** (loses everything since the copy): by hand it is refused unless
  `--lose-work-since` names the copy's time exactly as `back` prints it (`createdAt` of the manifest), and it first
  prints the project and thread counts at the copy (journalled by `copy`) against the live build's report now
  (threads that only got new messages are not in the counts). Then, each a journalled step: the plan; the old
  runtime put back from the kit's kept copy if upstream pruned it; the **pre hooks**; **disable** and stop the boot
  service (no start at boot on a half-restored home); the restore primitive (the migrated database and
  `settings.json` are kept aside, never deleted; the copy stays); `<home>/runtime/versions/<old>/t3 update <old>
  --allow-downgrade --yes --base-dir <home>` (it enables and starts the service); verify: a new server of the old
  build, named by the service state, the service enabled; the **post hooks**. `restore_and_downgrade(run, ...,
  automatic=True)` is the same function for the switch's own way back, inside the switch's run, with no time typed
  (`hooks=False` when the switch's run runs the hooks itself).
- **A W1 that is killed or fails after its stop leaves T3 stopped and disabled** (not started at boot), on purpose:
  the home may be half-restored. `status` says DOWN and names the run; nothing starts T3 again until a
  `t3-golive back --way w1` ends ok. Run the same back again: it finishes where the stopped one got to.
- **W2, the same-base upstream nightly** (loses nothing): the fork adds no migrations, so the upstream nightly the
  fork build is cut from runs the migrated database as it is: between the pre and the post hooks,
  `<that t3> update <base> --allow-downgrade --yes`, then the same verify. **Read from the code, untried on a live
  home**: the tests use a stub t3. Upstream's update installs the version from its release feed unless it is
  already whole in `runtime/versions`, so W2 needs the network; staging it means adding its archive's line to
  `stageDir/SHA256SUMS` beside the target's, not replacing the file.
- **W3, an upstream stable newer than the base**: the same, with that stable.

**The pre and post hooks** (`preHooks`, `postHooks` of the machine file) run around stopping and starting T3 as in
the copy run: a failed pre hook runs the post hooks and refuses before T3 is touched; the post hooks run after the
verify, and after a failure too. A killed back runs no post hook (back has no guard): background jobs stay paused
until a back runs again (it runs both again; each hook must be safe twice).

### The kept runtime (`lib/golive/keptruntime.py`)

Every upstream `t3 update` ends by pruning the whole runtimes in `runtime/versions` that nothing names any more
(`prunePinnedRuntimes({keep: [currentVersion, targetVersion]})` in `apps/server/src/cli/update.ts`). So the switch's
own update removes the old build's runtime, and W2 and W3 remove it too: without more, W1 would be gone after the
switch. The switch therefore keeps a copy first: `keptruntime.keep(run, cfg, <old>)`, inside one of its journalled
steps before its update (`keptruntime.plan_keep(cfg, <old>, floor_bytes)` is the read-only check before its first
step), copies `<home>/runtime/versions/<old>/` to `<stateDir>/kept-runtimes/<old>/runtime/` with `kept.json` (every
entry's kind, size, mode, SHA256, link target) written last and the folder renamed into place whole. W1 checks it
entry by entry (`keptruntime.check`) and only then copies it back into `runtime/versions/<old>/` (a fresh
`.install-complete`, so upstream's 1 h grace covers it, and upstream uses it as installed instead of downloading).
Upstream never looks in `stateDir`, so no update removes it, and the kit deletes none.

| Situation                                                                            | What happens                                                                                                                                                                                                                                                                                                                                                                                              |
| ------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Run twice                                                                            | The second finds the build the first back of the same way verified live (service state names it, no update or marker) and no run left half-way since: "nothing to do", no stop, no update                                                                                                                                                                                                                 |
| Another way than the last good back (W1 after W2 or W3)                              | Not "nothing to do": W1 runs when it is possible (the kept runtime is put back; the copy is restored over the data W2 ran on, which is kept aside), else it is refused naming why (no kept runtime, the copy, the build)                                                                                                                                                                                  |
| A back after a switch or back left half-way                                          | Never "nothing to do": it finishes the stopped back's plan, or takes the way afresh; a refusal ends "failed", so the home still needs settling                                                                                                                                                                                                                                                            |
| Stopped half-way (killed, failed, power loss)                                        | Another `back` may follow it (`journal.run(may_follow=('back', 'switch'))`); it finishes the stopped back's journalled plan: the put-back, the restore and the update are safe to run again (tested after the stop, the move-aside, mid-copy, mid-put-back, mid-update). Any other command still asks for `settle`. A stopped W2 or W3 looks for its t3 again first (refused, naming it, when it is gone) |
| ... and the follow-up is refused (a wrong time, another way)                         | It ends "failed", not "refused", so the half-way home still needs settling; another way than the stopped one is refused, naming the way to run again                                                                                                                                                                                                                                                      |
| ... and the machine boots meanwhile                                                  | W1 disabled the service before the stop, so T3 does not start on a half-restored home                                                                                                                                                                                                                                                                                                                     |
| No copy, an incomplete one, one of another home                                      | W1 not possible (named); a copy of another home is never listed                                                                                                                                                                                                                                                                                                                                           |
| The newest copy was taken under the build running now, or its t3 is not kept         | W1 not possible, named. Older copies are not used in its place (they lose more)                                                                                                                                                                                                                                                                                                                           |
| The kept runtime is missing, unfinished (no `kept.json`) or damaged (a file differs) | Never used: W1 not possible, named, refused before any change. `keep` run again sets a damaged one aside (`<version>.bad-<time>`) and keeps anew; a stopped keep leaves only `.partial-<version>`, redone by the next keep                                                                                                                                                                                |
| W2 with no base nightly installed or staged                                          | Refused before any change, naming what is missing                                                                                                                                                                                                                                                                                                                                                         |
| The guard timer (switch) fires `back` during a manual back                           | Refused by the kit's lock (`journal.Busy`): one back at a time                                                                                                                                                                                                                                                                                                                                            |
| A journal from before this kit (no `way` in a back's plan)                           | No "nothing to do" from it; the way is taken afresh from what is on disk                                                                                                                                                                                                                                                                                                                                  |

## `t3-golive status`

Read only: it holds the kit's lock shared like `check` but makes no lock file or state folder when there is none
(no golive run ever took the lock then). It checks: the boot service (active, enabled, serves this home); the
journal (no run left half-way: a back or switch that stopped or failed is named, with "run the same back again");
the service state (the version the last golive run verified, no update pending); no marker; the live server
(`server-runtime.json`, a live pid of that version, started by the service); HTTP answers on `127.0.0.1` at the port
the server recorded (never another host its `origin` may name); the t3 launcher links upstream recorded
(`runtime/launchers`) lead to that version; and the live fork build's report: read cleanly, no pending or unknown
migration, project and thread counts not below the newest `reportCounts` in the journal (the newest run that
journalled any: a copy after the switch replaces the switch's). It lists the fork features that are on, the provider
instances, the copies on disk with their sizes (complete, incomplete, kept files) and the kept runtimes (and whether
each is usable): the kit deletes none. The first line is one of **T3 is up:**, **T3 is up but not right**, **T3 is
restarting or starting** (a golive run holds the lock, a marker, the service activating or stopping, or no server
yet), **T3 is DOWN**, or **T3 state unknown** (the service manager did not answer, or status itself failed); exit 0
only for `T3 is up:` (every check passed). An error inside it is a failed check, never a crash or an OK. No report
runs unless T3 runs steadily, and none for a build that is not a fork build (an upstream build after W2 or W3: the
counts are then not checked, said in a note).

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
python3 -I -B fork-kit/golive/tests/back.test.py       # back W1-W3 and status, a stub t3 for every build
python3 -I -B fork-kit/golive/tests/contract.test.py   # upstream names the kit reads still exist
python3 -I -B fork-kit/common/tests/machine_file.test.py
```

The tests fake the service manager (stub `systemctl` and `systemd-run`) and the T3 home (a temporary folder,
with a copy of `sleep` standing in for the server; `copy.test.py` fakes stop and start on it too). They need no root and no systemd, and never touch a real
T3 home, unit or `~/.t3`.
