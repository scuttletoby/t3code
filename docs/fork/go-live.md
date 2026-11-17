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
`<stageDir>/<version>/`). For the way back W2 and W3, stage the upstream build the same way beside it.

## Day 0: the switch

1. **Check.** `t3-golive check`. Read only. It must say "Ready" with a plan; any `FAIL` line is a reason
   to stop here. Nothing has changed yet.
2. **Copy.** `t3-golive copy` (or `--owner-says-go` when no quiet command is set). The practice stop: at a
   quiet point T3 stops, its data is copied to a new folder in `backupDir`, read back, and T3 starts again.
   Note the copy's time it prints: it is the time the way back W1 needs.
3. **Rehearse** (not in the kit yet). The target build runs on a copy of the data, never the live home.
4. **Switch** (not in the kit yet). The live home moves to the target build; if the new build does not
   come up, the switch takes the way back W1 on its own.
5. **Verify.** `t3-golive status`. The first line must say `T3 is up`, with no `FAIL` line: the target
   version runs, it answers over HTTP, every migration is applied, and the project and thread counts are
   not below the ones from before the switch. `fork features: all off`.
6. **By hand.** On each provider account, send one message in one thread and see the answer arrive. Reload
   the app and see it again.
7. **Two days.** Run `t3-golive status` once a day for two days. Anything but `T3 is up`: read its lines,
   then decide on the way back below.

## The way back

`t3-golive back` (read only) says which way is possible now and why. Take one with
`t3-golive back --way <w1|w2|w3>`; it runs detached and prints how to follow it. Afterwards:
`t3-golive status` and the by-hand test again. Running `back` again after a good one does nothing;
running it again after one that stopped half-way finishes it.

| Way | What it does                                                                  | Loses                                  | Pick it when                                                                                              |
| --- | ----------------------------------------------------------------------------- | -------------------------------------- | --------------------------------------------------------------------------------------------------------- |
| W1  | Restores the copy and goes back to the build it was taken under               | Everything since the copy (kept aside) | Day 0, or the data itself is wrong. By hand it needs `--lose-work-since <copy time>`, as `back` prints it |
| W2  | Goes to the upstream nightly the fork build is cut from, on the data as it is | Nothing                                | The normal way back after day 0. Untried on a live home: it ran only against a stub in the kit's tests    |
| W3  | Goes to an upstream stable newer than that nightly, on the data as it is      | Nothing                                | Once such a stable exists: a stable rather than a nightly                                                 |

W1 puts the copy's `settings.json` back; W2 and W3 keep the settings file as it is. The kit deletes no
copy and no file it moved aside; `status` lists them with their sizes, to remove by hand once sure.

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
