# Going live on the fork: the runbook

Fork only. How a machine that runs T3 Code as a boot service moves to a fork build, checks it, goes back
if it must, and then switches the fork's features on one at a time. Every value below is a placeholder.
What each command does, and every refusal, is in the kit's own page,
[fork-kit/golive/README.md](../../fork-kit/golive/README.md).

## The machine file

Machine facts live in a private file outside the fork, the one every fork kit reads
(`/etc/fork-kit/machine.json`, owned by root). Nothing in it belongs in the repository. Its `golive`
section, with placeholders:

```json
{
  "version": 1,
  "golive": {
    "home": "/path/to/the/t3/home",
    "targetVersion": "0.0.0-nightly.00000000.0.fork.1",
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

`stageDir` holds the fork release (its `SHA256SUMS`, the archive and the archive extracted into
`<stageDir>/<version>/`). For the way back W2 and W3, stage the upstream build the same way beside it: its
archive and extracted folder next to the fork's, and its archive's line added to the same `SHA256SUMS` (one
file for both; do not replace it). Upstream's `update` still installs that build from its release feed unless
it is already in the home, so W2 and W3 need the network.

`preHooks` and `postHooks` pause and resume background jobs that use T3. The kit runs them around every stop
of T3: the copy, the switch and every way back. Each must be safe to run twice.

## Day 0: the switch

1. **Check.** `t3-golive check`. Read only. It must say "Ready" with a plan; any `FAIL` line is a reason
   to stop here. Nothing has changed yet.
2. **Copy.** `t3-golive copy` (or `--owner-says-go` when no quiet command is set). The practice stop: at a
   quiet point T3 stops, its data is copied to a new folder in `backupDir`, read back, and T3 starts again.
   This copy is practice. The way back W1 uses the newest finished copy, which after the switch is the
   switch's own, so do not note this copy's time: `t3-golive back` prints the time W1 needs.
3. **Rehearse.** `t3-golive rehearse`. The target build runs on a clone of the newest copy, in its own home and
   port, never the live home. The rehearsal names the staged archive by its SHA256: the switch installs exactly
   that build and refuses if the staged release changed since (rehearse again then).
4. **Switch.** `t3-golive switch` (or `--owner-says-go`). It runs detached and prints how to follow it. At a
   quiet point T3 stops, its data is copied to a new `t3-switch-*` folder, the rehearsed build is installed
   from `stageDir` (upstream's `update` downloads nothing) and T3 is updated to it. If the new build does not
   come up, the switch takes the way back W1 on its own, once. If that W1 fails after it stopped T3, T3 stays
   down and disabled:
   follow **A killed or failed W1 leaves T3 down** below. Before its update, the switch keeps the old
   build's runtime in `<stateDir>/kept-runtimes/<old version>/`: every upstream `t3 update` removes old
   runtimes from the home, and W1 needs that one. Never delete the kept runtime while W1 may be needed.
5. **Verify.** `t3-golive status`. The first line must start with `T3 is up:` (with the colon; `T3 is up
   but not right` is a failure) and it exits 0: the target version runs, it answers over HTTP, every
   migration is applied, no golive run was left half-way, and the project and thread counts are not below
   the ones from before the switch. `fork features: all off`, and the kept runtime is listed as usable.
6. **By hand.** On each provider account, send one message in one thread and see the answer arrive. Reload
   the app and see it again.
7. **Then go on.** No waiting period. Run `t3-golive status` after each later change and whenever something
   looks off. Anything but `T3 is up:`: read its lines, then decide on the way back below. The other first
   lines are `T3 is up but not right`, `T3 is restarting or starting`, `T3 is DOWN` and `T3 state unknown`.
   Take no `t3-golive copy` until you are sure you will not need W1 (see below).

## The way back

`t3-golive back` (read only) says which way is possible now and why, and for W1 the copy's time to type.
Take one with `t3-golive back --way <w1|w2|w3>`; it runs detached and prints how to follow it. Afterwards:
`t3-golive status` and the by-hand test again. Running the same way again after a good one does nothing;
running it again after one that stopped half-way finishes it.

- **Background jobs.** `back` runs `preHooks` before it stops T3 and `postHooks` once T3 runs again and is
  verified. After a failure, W2 and W3 run `postHooks` too (they never stop T3). A W1 that failed after it
  stopped T3 runs none: the jobs stay paused while T3 is down, until a `back` ends ok (the error and `status`
  say so). A killed `back` runs no post hook either. With no hooks in the machine file, pause the background
  jobs that use T3 by hand before `back`, and resume them after `status` says `T3 is up:`.
- **A killed or failed W1 leaves T3 down.** After its stop, W1 keeps T3 stopped and not started at boot (on
  purpose: the home may be half-restored) until a W1 ends ok. `status` then says `T3 is DOWN`, names the run
  and says what it left. Run the same `t3-golive back --way w1 --lose-work-since <time>` again. If the stopped
  W1 had not finished its update, the rerun restores the copy and downgrades again. If it got past its update,
  the rerun does not stop T3 or restore again: it checks that the old build runs, then runs the post hooks.
  A W1 whose only failure was a post hook leaves T3 up and enabled; the rerun runs only the post hooks. Do not
  start T3 by hand.
- **If the old build does not come up after W1.** When W1 got past its update but the old build does not start,
  run the same W1 again: it only checks again (it never restores twice). When that check has finished and failed
  twice (the check after the update, and the rerun's), W1 has nothing more it can do. `status` then says so,
  and `t3-golive back --way w2` (or `--way w3`) may be taken next; before that, both are refused and say what is
  still missing. A check that was cut off (the back was killed or stopped, or the machine went down) does not
  count as failed: `status` says `was cut off`, the old build may still be coming up, and W1 is run again.
  W2 or W3 taken this way restores nothing: it goes to the upstream build on the data W1 put back, and
  background jobs go on once it ends ok. It is refused when that build is older than the build the copy was
  taken under (a downgrade under newer data), and W3 does not go to the very build W1 could not start. This
  holds for the W1 the switch runs by itself too. After W2 or W3 was begun this way, run that same way again if
  it stops half-way; W1 and the other of the two are refused until it ends ok, and `t3-golive back` lists them
  as not possible.
- **No copy while W1 may still be needed.** W1 always restores the newest finished copy. A `t3-golive copy`
  taken after the switch is the newest one, taken under the fork build, so it hides the switch's own copy and
  `back` then refuses W1. Take no copy until you are sure you will not need W1.

| Way | What it does                                                                  | Loses                                  | Pick it when                                                                                                                                                              |
| --- | ----------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1  | Restores the copy and goes back to the build it was taken under               | Everything since the copy (kept aside) | Day 0, or the data itself is wrong. By hand it needs `--lose-work-since <copy time>`, as `back` prints it. Possible while the kept runtime reads back (`status` lists it) |
| W2  | Goes to the upstream nightly the fork build is cut from, on the data as it is | Nothing                                | The normal way back after day 0. Untried on a live home: it ran only against a stub in the kit's tests                                                                    |
| W3  | Goes to an upstream stable newer than that nightly, on the data as it is      | Nothing                                | Once such a stable exists: a stable rather than a nightly                                                                                                                 |

W1 puts the copy's `settings.json` back; W2 and W3 keep the settings file as it is. W2 and W3 run
upstream's `update`, which removes the fork build's and other old runtimes from the home, but not the kit's
kept runtime: W1 stays possible after W2 or W3 (it restores the copy over the data W2 or W3 ran on, which is
kept aside). One exception: when the build before the switch was the very nightly the fork is cut from, W2
goes back to that same build, and `back` then refuses W1 ("not older than the t3 now"). Upstream's `update` to
the build that already runs may do nothing, so it would not start the T3 that W1 stopped. So if the data
itself is wrong, take W1 first, not W2. Without the kept runtime (deleted, or damaged), W1 is gone after
the switch, W2 or W3, and `back` says so (and `status` fails `way back W1` while a damaged one is there). The kit deletes no copy, no file it moved aside and no kept runtime; `status` lists them
with their sizes, to remove by hand once sure.

## Then: one feature at a time

Each feature is its own setting in the home's `userdata/settings.json`, read again without a restart.
Switch on one and run its check; when the check passes, switch on the next. There is no waiting period.
After each change, `t3-golive status` lists what is on, each with where: `server` (the file's own value,
or a default of the administrator's managed policy), `policy` (on only because the policy locks it),
`project:<id>` (that project's override in `projectSettingsOverrides`), `thread:<id>` (one thread's entry
in the server's `handoff.threads`) or `project:<id>/thread:<id>` (one in a project's). The features are `agentSlots`, `waitFor`,
`usagePacingGate`, `automaticUpdates`, `reviewRules`, `handoff` and `playtest`. Not listed, as none is a
switch: `headlessUsageRefreshInterval` (runs by default in every fork build and only reads usage), MCP
profiles (nothing applies a profile yet), `usagePacing` and `usagePacingLowerEffort` (only the pacing
gate acts on them), `waitChecks` (only `waitFor` runs them) and `automaticUpdatesQuietCommand`.

**Off again** is an edit of that one setting; nothing else changes back. For `reviewRules`, `handoff`
and `playtest` it may take more than one: also remove each project override and each `handoff` thread
entry that `status` names, including those of a deleted project or thread. A `policy` entry is not in
settings.json: only the administrator's policy file clears it. When the policy locks the value the file
already holds, `status` says `server`, and after the edit it says `policy`.

| Step | Setting           | On                                                                  | Check                                                                                                                             | Off again                                                                                    |
| ---- | ----------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------- |
| 0    | none              | -                                                                   | `status`: `fork features: all off`                                                                                                | -                                                                                            |
| 1    | `agentSlots`      | `"agentSlots": { "enabled": true, "maxPerMachine": <n> }`           | A `delegate_task` above `<n>` running agents is refused; below it, starts                                                         | `"enabled": false`                                                                           |
| 2    | `waitFor`         | `"waitFor": { "enabled": true }`                                    | One test wait (a file that appears) arrives once, and survives a restart of T3 between the wait and the event                     | `"enabled": false`                                                                           |
| 3    | `usagePacingGate` | `"usagePacingGate": "advise"`                                       | A `delegate_task` reply carries a pace note; nothing is held or moved                                                             | `"usagePacingGate": "off"`                                                                   |
| 4    | `reviewRules`     | `"reviewRules": { "preset": "recommended" }`, or in one project     | `status` names `reviewRules` where it was set; nothing reads it yet, so no work changes                                           | `"preset": "off"`, and remove each project's `reviewRules`                                   |
| 5    | `playtest`        | One project's `"playtest"`: `"enabled": true` and its four steps    | `status` names `playtest (project:<id>)`; nothing starts a session yet, so no step runs                                           | `"enabled": false`, or remove that project's `playtest`                                      |
| 6    | `handoff`         | `"handoff": { "mode": "handoff" }`, or in one project or one thread | One test thread hands off ([handoff.md](handoff.md)) and its children, watched pull requests and waits keep working               | `"mode": "off"`, and remove each project's `handoff` and each `"handoff"` entry in `threads` |
| 7    | old project tools | -                                                                   | Retire, one at a time, the tools this machine used before the fork for what the features now do; keep each until it is not missed | put the tool back                                                                            |

Later, and each a decision of its own: `"usagePacingGate": "enforce"` (tasks are held, moved or run at a
lower effort; off again: `"advise"`), and `"automaticUpdates": true` (the boot service updates itself to
the newest fork release at a quiet point; off again: `false`). What each setting does:
[the fork's settings](README.md#each-setting).
