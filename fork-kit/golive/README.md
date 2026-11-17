# golive: move a T3 boot service to a new build, safely

`t3-golive` moves a T3 home that runs as a boot service from its current build to a newer one (for example a
fork build), with a cold copy of the data first and a way back. It is built in steps; this folder has the
**core**, the **copy** and the **rehearsal** so far:

| Part              | File                      | What it does                                                 |
| ----------------- | ------------------------- | ------------------------------------------------------------ |
| config            | `lib/golive/config.py`    | reads the `golive` section of the machine file               |
| lock and journal  | `lib/golive/journal.py`   | one run at a time; each step journalled "started" and "done" |
| live T3           | `lib/golive/live.py`      | finds the running server from its own files and process      |
| `check`, `settle` | `lib/golive/check.py`     | read only: may this home move to the target build now?       |
| OS adapter        | `lib/golive/osadapter.py` | systemd user units only; other systems refuse until written  |
| `copy`, restore   | `lib/golive/coldcopy.py`  | the practice stop: a cold copy; the restore primitive        |
| `rehearse`        | `lib/golive/rehearse.py`  | the target build tried on a clone of a copy (R0-R5)          |

Later parts (switch, back) come in their own PRs. Install from `ci-release` comes with them.

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

## `t3-golive rehearse [--copy <folder>] [--port <p>] [--json]`

Tries the target build on a **clone** of a finished copy (the newest `t3-copy-*` in `backupDir`, or `--copy`), in
its own T3 home (`<backupDir>/t3-rehearsal/home`) and port. It never stops, starts or writes the live home and
needs no boot service; it runs in the calling process, on Linux only, with Python 3.11 or newer (older: refused
before any step). Every `t3` it runs gets an empty `HOME`, `PATH=/usr/bin:/bin`
and nothing else from the environment, and dies with the run (`PR_SET_PDEATHSIG`).

- **R0, before any step** (refused, nothing changed): the copy has a finished manifest, reads back against it, is
  of this home, and was taken under the build the live service state names now (else "take a new copy"); the target
  is newer; the old build (`<home>/runtime/versions/<copy's version>/t3`) is there; the staged release checks out
  and its `serve --help` has `--hold-work`; the rehearsal folder is apart from the live home (real paths and file
  ids of the home and of its `userdata`, both ways, so a link or a bind path is caught; and by the mount list: a
  bind mount of any folder inside the live home, at the backups folder say, keeps the device and gives no id the
  kit compares, but its mountinfo line names the folder it came from, so the folder the rehearsal folder is on its
  filesystem must be apart from each folder of the live data: the home, where its `userdata` really is (a link
  elsewhere, or its own partition), and every mount at or under either; the mount that holds a path is the last one
  in the list that covers it, as a later mount on a parent hides an earlier one below it); an old one is a real folder (no link) that bears the kit's
  mark `.golive-rehearsal` (or is empty), with no folder in it that is the live home or on another device, no mount
  point at or under it in the system's mount list (`/proc/self/mountinfo`: a bind mount of the same filesystem keeps
  its device id), and no link to another home's ownership lock; no server holds it; the copy is not inside it (nor
  a link into it); the port is free and is neither the live port nor 3773; `backupDir` holds 3 x the copy plus the
  floor. Where the mount list cannot be read or is empty (any OS but Linux, no `/proc`), every run is refused before
  any step: rehearse cannot run on that system at all (it cannot tell a bind mount), and removing an old rehearsal
  folder by hand does not change that. **R0 on the clone**: the clone leaves out
  `secrets/` and the live machine's process records (`device/`, `relay-clients/`), with what a copy never holds
  (`server-runtime.json`, the ownership lock); it reads back against the manifest and is checked to hold none of
  those: a runtime record would point the CLI at the LIVE server; and it is apart from the live data as above (if
  not, the run fails: only the clone was made, nothing ran on it).
- **The clone step** runs the old-folder guard again right before it removes anything (a mount or a swap since R0
  is caught), then removes the old folder with its mark last: it opens the folder without following a link, checks
  it is the one the guard checked (device and inode), and removes its contents through that handle, so a swap of the
  path after the check removes nothing of what is there now.
- **Limits of these checks**: filesystems are told apart only by the mount list's device number, so the same files
  reached through a second mount of their own (NFS mounted twice, a FUSE or bindfs view, a Windows drive mounted
  twice on WSL) are not caught: keep `backupDir` off any such second view of the live home. The guard takes the old
  folder's identity first and then checks by path: a swap still there at its end is refused, a swap away and back
  during it (a same-user process racing the kit) is not. A mount made inside the old folder after the guard (root
  only) is not checked by the removal, and the last `rmdir` goes by path (it only ever removes an empty folder).
- **R1**: `__service-preflight` of the old build and of the fork build on the clone: both `ready`. The fork build's
  read-only report of the clone is the "before" report.
- **R2**: `<fork t3> auth session list --json --base-dir <clone>`, with no server: a read-only command whose
  database layer migrates under the ownership lock. Timed: seconds, database growth, the migrations (the before
  report's `pendingMigrations`). A report after it must show exactly those applied and none pending (or it says no
  migrations were pending), so R3's server never migrates unmeasured.
- **R3**: `<fork t3> serve --hold-work --host 127.0.0.1 --port <p> --no-browser --base-dir <clone>`; ready when
  the clone's `server-runtime.json` names this server's pid, port `<p>` and `workHeld: true`.
- **R4**, report against report (fork #181): the version is the target; applied = before's applied + before's
  pending, none pending or unknown; `live.compare_counts` finds no difference; no fork feature on; no settings
  parse warning; the same provider instances, at least one; the server still runs after the report.
- **R5**: the server is stopped (always, also after a failure); `settings.json` is byte-identical; the old build's
  preflight on the migrated clone says `blocked` (`ready` when there were no migrations; its reason is journalled);
  `coldcopy.restore` puts the copy back into the clone and the old build's preflight says `ready`.

The clone keeps `settings.json` as it is (R5 checks it is unchanged), so the R3 server reads the live provider
settings: the provider program paths and homes (`binaryPath`, `homePath`) and any trace address. Its provider status
checks may start those provider programs against the live provider homes (a login refresh, for example) and send
traces to the same collector. No T3 data of the live home is reached.

The result (from and target version, copy, SHA256 of its manifest, migrations, seconds, growth, port) is the
journal's last step, `result`, of a run that ended `ok`. A rehearsal is a **scratch run**
(`journal.run(..., scratch=True)`): its steps change only `<backupDir>/t3-rehearsal/`, so a run that fails or is
stopped needs no `settle` (`check` notes it); the next run throws the old clone away and makes a new one, never
reusing one half-migrated. A run that needs settling still blocks it.

| Situation                                               | What happens                                                                  |
| ------------------------------------------------------- | ----------------------------------------------------------------------------- |
| Runs again after a good or failed rehearsal             | The old clone (it bears the mark) is removed and redone from the copy         |
| Killed at any step (R0-R5, mid-restore)                 | The server dies with the run; nothing to settle; the next run redoes it       |
| Killed while it removes the old folder                  | The mark goes last: the folder still bears it (or is empty); next run ends it |
| A mount (or a swap to a link) after R0, before removal  | The guard runs again in the clone step: the run fails, nothing is removed     |
| A swap (link, other folder) after that check            | The run fails; what is at the path now is never removed                       |
| A mount point at or under the folder                    | Refused; never removed                                                        |
| No mount list (not Linux, no `/proc`, an empty list)    | Refused before any step: rehearse cannot run there at all                     |
| Python older than 3.11                                  | Refused before any step; an old rehearsal folder is kept                      |
| `--copy` inside the rehearsal folder, or a link into it | Refused before any step: the clone step would remove it                       |
| R2 left migrations pending                              | The run fails before R3: the server would migrate unmeasured                  |
| A server still holds an old rehearsal home              | Refused before any step; stop it (its pid is in that home's runtime record)   |
| `t3-rehearsal` without the mark, or a link              | Refused; never removed                                                        |
| Live home by a link or bind path, or bound in a clone   | Refused (real path; file ids both ways and inside) before any removal         |
| `backupDir` a bind of a folder inside the live home     | Refused before any step (mount list: the folder each is on its filesystem)    |
| `backupDir` a bind from `userdata`'s own partition      | Refused before any step (and from a link's target, any mount under the home)  |
| Copy with no manifest, not matching it, of another home | Refused (`--copy` or `t3-golive copy`)                                        |
| Copy from another build than the live one               | Refused: take a new copy                                                      |
| Port taken between the check and the start              | The server exits; R3 fails, the rerun passes once the port is free            |
| Server never ready, or not saying work is held          | Stopped; the run fails                                                        |
| Server dies during R4, or any R4 check fails            | The run fails naming every problem; the server is stopped                     |

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
python3 -I -B fork-kit/golive/tests/rehearse.test.py   # rehearse R0-R5 with a stub t3 (old and fork build)
python3 -I -B fork-kit/golive/tests/contract.test.py   # upstream names the kit reads still exist
python3 -I -B fork-kit/common/tests/machine_file.test.py
```

The tests fake the service manager (stub `systemctl` and `systemd-run`) and the T3 home (a temporary folder,
with a copy of `sleep` standing in for the server; `copy.test.py` fakes stop and start on it too). They need no root and no systemd, and never touch a real
T3 home, unit or `~/.t3`.
