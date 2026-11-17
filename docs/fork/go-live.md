# Going live on the fork: the runbook

Fork only. How a machine that runs T3 Code as a boot service moves to a fork build, checks it, goes back
if it must, and then switches the fork's features on one at a time. Every value below is a placeholder.
What each command does, and every refusal, is in the kit's own page,
[fork-kit/golive/README.md](../../fork-kit/golive/README.md).

Not in the kit yet: `rehearse` and `switch` (go-live PRs 5 and 6). Until they are merged, the steps that
name them cannot be run; everything else here can.

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
3. **Rehearse** (not in the kit yet). The target build runs on a copy of the data, never the live home.
4. **Switch** (not in the kit yet). The live home moves to the target build; if the new build does not
   come up, the switch takes the way back W1 on its own. Before its update, the switch keeps the old
   build's runtime in `<stateDir>/kept-runtimes/<old version>/`: every upstream `t3 update` removes old
   runtimes from the home, and W1 needs that one. Never delete the kept runtime while W1 may be needed.
5. **Verify.** `t3-golive status`. The first line must start with `T3 is up:` (with the colon; `T3 is up
   but not right` is a failure) and it exits 0: the target version runs, it answers over HTTP, every
   migration is applied, no golive run was left half-way, and the project and thread counts are not below
   the ones from before the switch. `fork features: all off`, and the kept runtime is listed as usable.
6. **By hand.** On each provider account, send one message in one thread and see the answer arrive. Reload
   the app and see it again.
7. **Two days.** Run `t3-golive status` once a day for two days. Anything but `T3 is up:`: read its lines,
   then decide on the way back below. The other first lines are `T3 is up but not right`, `T3 is restarting
   or starting`, `T3 is DOWN` and `T3 state unknown`.

## The way back

`t3-golive back` (read only) says which way is possible now and why, and for W1 the copy's time to type.
Take one with `t3-golive back --way <w1|w2|w3>`; it runs detached and prints how to follow it. Afterwards:
`t3-golive status` and the by-hand test again. Running the same way again after a good one does nothing;
running it again after one that stopped half-way finishes it.

- **Background jobs.** `back` runs `preHooks` before it stops T3 and `postHooks` after T3 runs again (also
  after a failure). With no hooks in the machine file, pause the background jobs that use T3 by hand before
  `back`, and resume them after `status` says `T3 is up:`. A killed `back` runs no post hook: the jobs stay
  paused until `back` runs again.
- **A killed or failed W1 leaves T3 down.** After its stop, W1 keeps T3 stopped and not started at boot (on
  purpose: the home may be half-restored) until a W1 ends ok. `status` then says `T3 is DOWN` and names the
  run. Run the same `t3-golive back --way w1 --lose-work-since <time>` again: it finishes where the stopped
  one got to. Do not start T3 by hand.

| Way | What it does                                                                  | Loses                                  | Pick it when                                                                                                                                                              |
| --- | ----------------------------------------------------------------------------- | -------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| W1  | Restores the copy and goes back to the build it was taken under               | Everything since the copy (kept aside) | Day 0, or the data itself is wrong. By hand it needs `--lose-work-since <copy time>`, as `back` prints it. Possible while the kept runtime reads back (`status` lists it) |
| W2  | Goes to the upstream nightly the fork build is cut from, on the data as it is | Nothing                                | The normal way back after day 0. Untried on a live home: it ran only against a stub in the kit's tests                                                                    |
| W3  | Goes to an upstream stable newer than that nightly, on the data as it is      | Nothing                                | Once such a stable exists: a stable rather than a nightly                                                                                                                 |

W1 puts the copy's `settings.json` back; W2 and W3 keep the settings file as it is. W2 and W3 run
upstream's `update`, which removes the fork build's and other old runtimes from the home, but not the kit's
kept runtime: W1 stays possible after W2 or W3 (it restores the copy over the data W2 or W3 ran on, which is
kept aside). Without the kept runtime (deleted, or damaged), W1 is gone after the switch, W2 or W3, and
`back` says so. The kit deletes no copy, no file it moved aside and no kept runtime; `status` lists them
with their sizes, to remove by hand once sure.

## Then: one feature at a time

Each feature is its own setting in the home's `userdata/settings.json`, read again without a restart.
Switch on one, watch a day or more, then the next. After each change, `t3-golive status` lists what is on.
Each one's **off again** is a single edit of that one setting; nothing else changes back.

| When      | Setting           | On                                                        | Check                                                                                                                             | Off again                  |
| --------- | ----------------- | --------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------- | -------------------------- |
| Day 0     | none              | -                                                         | `status`: `fork features: all off`                                                                                                | -                          |
| Day 1-2   | `agentSlots`      | `"agentSlots": { "enabled": true, "maxPerMachine": <n> }` | A `delegate_task` above `<n>` running agents is refused; below it, starts                                                         | `"enabled": false`         |
| Day 3     | `waitFor`         | `"waitFor": { "enabled": true }`                          | One test wait (a file that appears) arrives once, and survives a restart of T3 between the wait and the event                     | `"enabled": false`         |
| Day 5     | `usagePacingGate` | `"usagePacingGate": "advise"`                             | A `delegate_task` reply carries a pace note; nothing is held or moved                                                             | `"usagePacingGate": "off"` |
| Day 10 on | old project tools | -                                                         | Retire, one at a time, the tools this machine used before the fork for what the features now do; keep each until it is not missed | put the tool back          |

Later, and each a decision of its own: `"usagePacingGate": "enforce"` (tasks are held, moved or run at a
lower effort; off again: `"advise"`), and `"automaticUpdates": true` (the boot service updates itself to
the newest fork release at a quiet point; off again: `false`). What each setting does:
[the fork's settings](README.md#each-setting).
