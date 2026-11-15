# Updates and fork settings: defaults, and the nightly sync

Fork only. What a machine running this fork does about updates and fork features when nobody chose
anything, how a test keeps it that way, and the steps for the upstream sync that changes the updater.

## The rule: nothing changes unasked

A machine whose settings file does not name a fork setting behaves as upstream's T3 Code would. In
particular it never updates itself: `automaticUpdates` is `false` unless its owner turned it on
(owner decision [#41](https://github.com/scuttletoby/t3code/issues/41)). Upstream's pending updater
uses the same setting name with the default `true`; the fork keeps `false` through every sync.

Every fork setting, its default, the reason and where it was decided are listed in one place:
[`packages/contracts/src/forkDefaults.ts`](../../packages/contracts/src/forkDefaults.ts). All are off
except `headlessUsageRefreshInterval`, which reads usage limits every 10 minutes while no client
watches; it changes nothing on the machine, and `0` turns it off.

### What guards it

- [`forkDefaults.test.ts`](../../packages/contracts/src/forkDefaults.test.ts) finds each fork setting
  in `packages/contracts/src/settings.ts` by the `Fork:` comment on its line, and fails when one has
  no row in `forkDefaults.ts`, a row has no setting, or a default differs from its row. A rebase that
  takes upstream's `true` turns it red, and fork-follow publishes nothing on a red test.
- [`serverSettings.forkDefaults.test.ts`](../../apps/server/src/serverSettings.forkDefaults.test.ts)
  loads the settings the way a server does and checks every fork setting is at its row's default
  with no file, `{}`, a file an upstream build wrote, a file cut off half-way and a file with a bad
  value. A file that cannot be read gives no settings at all, and the update loop then does nothing
  (`autoUpdate.test.ts`).

### Adding a fork setting

1. Put a comment that contains `Fork:` on the line above the key in `ServerSettings`,
   `ProjectSettingsOverrides` or `ClientSettingsSchema`.
2. Add a row to `forkDefaults.ts`: default as the settings file writes it, `off`, a reason in plain
   words, and the fork pull request or issue that decided it.
3. The default is off (upstream's behaviour) unless the owner decided otherwise; `off: false` needs
   that decision as `decidedIn`.

A key without the `Fork:` comment is invisible to the test. The sync steps below check for that.

### State: runs again, stops half-way, old or empty state

| State on the machine                                                          | Result                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No settings file, `{}`, or a file that cannot be parsed                       | Every fork setting at its default: updates off.                                                                                                                                                                                                                                                                    |
| A file that cannot be read at all                                             | No settings are served; the update loop skips every tick.                                                                                                                                                                                                                                                          |
| A machine that ran an upstream build in between                               | Upstream writes only values that differ from its defaults, and its updater defaults on, so the key is not in the file: back on the fork, updates are off. An upstream "off" is written as `false` and stays off.                                                                                                   |
| A file saved before the rename (`autoUpdateServer`, `autoUpdateQuietCommand`) | Read under the new names; the next save writes only the new names.                                                                                                                                                                                                                                                 |
| Both old and new names                                                        | The new name wins.                                                                                                                                                                                                                                                                                                 |
| An admin policy that defaults updates on                                      | The user's stored choice wins over the policy default, including an old-name "off" ([#44](https://github.com/scuttletoby/t3code/issues/44)). The first save keeps an old-name choice that equals the policy default, so it survives the policy's removal ([#51](https://github.com/scuttletoby/t3code/issues/51)). |
| A save stopped half-way                                                       | Settings writes are atomic: the file is the old one or the new one.                                                                                                                                                                                                                                                |

An admin policy with `limits.updates.mode: "off"` (upstream's) refuses updates on a machine whatever
its settings say; that is the hard "never update this machine" switch.

## Nightly sync runbook

fork-follow (`.github/workflows/fork-follow.yml`) rebases the fork onto upstream's newest nightly
once a day, tests, and publishes only when the tests are green. On a conflict, a red test or a schema
change it stops and opens one issue. Then:

1. Rebase by hand as the workflow's report says. Never take upstream's side of a fork setting's
   default: keep the fork's value and its `Fork:` comment.
2. Check that no fork setting lost its marker:
   `git diff <upstream tag> HEAD -- packages/contracts/src/settings.ts` must show a `Fork:` comment
   above every key the fork adds.
3. Run the pin tests before pushing:
   `vp test run packages/contracts/src/forkDefaults.test.ts apps/server/src/serverSettings.forkDefaults.test.ts apps/server/src/cloud/autoUpdateSettings.test.ts`.
4. Push with `--force-with-lease` and start fork-follow once, as its report says.

### The sync that brings upstream's updater

When upstream's server updater (upstream PRs 17585 and 17586) reaches a nightly, the rebase stops on
the `automaticUpdates` line in `settings.ts`. That stop is wanted. Three fork pull requests follow, in
this order, so that "half-way" always means "no automatic update":

1. **Remove** the fork's update scheduler and its work gate, and add a start-up step that deletes a
   leftover held-work marker and logs what it held. First check on upstream's merged code that a
   scheduled run or a message that arrives during the restart is neither lost nor started twice; if
   it is, keep the gate and record it as a known fork difference. After this step alone the fork has
   no automatic update; the manual Update button still works.
2. **Rebase** onto upstream's updater. Keep `false` on the conflict line and its `Fork:` comment. The
   pin tests must be green. The fork's only difference in upstream's updater files is that one line.
3. **Re-attach** the fork's add-ons around upstream's update window (hours window, quiet command), use
   upstream's settings row and delete the fork's. Say in the release notes that "Update
   automatically" now also updates provider CLIs.

Steps 2 and 3 go into the same release.

The first update that crosses this sync is done by hand on each machine, at a moment when it is
quiet: the old build may leave work held for the restart in a marker file the new build no longer
reads, and a quiet machine holds nothing. The `automaticUpdates` value is kept: a machine that had it
on stays on, one without the key stays off.

The old-name rename (`apps/server/src/cloud/autoUpdateSettings.ts`) stays after the sync, since old
settings files may still exist; it moves with the add-ons.
