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

- Every fork setting lives in
  [`packages/contracts/src/forkSettings.ts`](../../packages/contracts/src/forkSettings.ts), the fork's
  own file; upstream's `settings.ts` takes its fields with one spread per struct. Only
  `automaticUpdates` stays inline in `settings.ts`, where upstream's pending setting of the same name
  will be. A patch's fork fields are applied by
  [`packages/shared/src/forkSettingsPatch.ts`](../../packages/shared/src/forkSettingsPatch.ts), each by
  its rule in `FORK_PATCH_RULES`.
- [`forkDefaults.test.ts`](../../packages/contracts/src/forkDefaults.test.ts) reads the fork's keys
  from `forkSettings.ts` (no comments, no hand-kept list), and fails when one has no row in
  `forkDefaults.ts`, a row has no setting, or a default differs from its row. A rebase that takes
  upstream's `true` turns it red, and fork-follow publishes nothing on a red test.
- [`forkSettings.test.ts`](../../packages/contracts/src/forkSettings.test.ts) checks that each fork
  field reaches upstream's structs unchanged (an upstream key of the same name would hide it), that
  every fork setting has a patch field and a patch rule, and pins the count of fork lines in
  `settings.ts`; `forkSettingsPatch.test.ts` pins them in `packages/shared/src/serverSettings.ts`.
- [`serverSettings.forkIntervals.test.ts`](../../apps/server/src/serverSettings.forkIntervals.test.ts)
  finds every interval (Duration) setting by walking the settings schema, the fork's and upstream's,
  and fails when one does not survive a patch or a save and load: upstream's patch merge and its
  `ATOMIC_SETTINGS_KEYS` list each interval by hand, and a missed one is saved as a broken object.
  The same schema walk ([`forkSettingsIntervals.ts`](../../packages/shared/src/forkSettingsIntervals.ts))
  makes a save refuse an interval no run could use and a load read one as its default.
- The seam check ([`fork-seam-check.cjs`](../../.github/scripts/fork-seam-check.cjs)) refuses any
  line the fork adds to an upstream file beyond the seam lines pinned in
  [`.github/fork-seams.json`](../../.github/fork-seams.json), with or without a comment. It runs on
  every pull request (Fork tests, "Workflow lint and upstream drift") and in fork-follow before and
  after each rebase, against the upstream commit the series sits on.
- [`serverSettings.forkDefaults.test.ts`](../../apps/server/src/serverSettings.forkDefaults.test.ts)
  loads the settings the way a server does and checks every fork setting is at its row's default
  with no file, `{}`, a file an upstream build wrote, a file cut off half-way and a file with a bad
  value. A file that cannot be read gives no settings at all, and the update loop then does nothing
  (`autoUpdate.test.ts`).

### Adding a fork setting

1. In `forkSettings.ts`, add the field with its default to `ForkServerSettingsFields`, its patch
   field to `ForkServerSettingsPatchFields` and its rule to `FORK_PATCH_RULES` (`replace`,
   `merge-fields`, or `{ mergeEntries: <defaults> }` for a map whose `null` entry removes it). A
   setting a project may override also goes in `FORK_PROJECT_SCOPED_KEYS`,
   `ForkProjectOverrideFields` and `ForkProjectOverridePatchFields`; a client setting in
   `ForkClientSettingsFields`. Upstream's files stay as they are.
2. Add a row to `forkDefaults.ts`: default as the settings file writes it, `off`, a reason in plain
   words, and the fork pull request or issue that decided it.
3. The default is off (upstream's behaviour) unless the owner decided otherwise; `off: false` needs
   that decision as `decidedIn`.
4. An agent tool may not write it: `FORK_AGENT_WRITABLE_KEYS` stays empty unless a pull request gives
   the reason.
5. Add its row and section to [README.md](README.md) and name it in
   [FORK_CHANGES.md](../../FORK_CHANGES.md); `packages/shared/src/forkDocs.test.ts` fails until both
   match the row in `forkDefaults.ts`.

A key written straight into `settings.ts` is refused by the seam check, whatever its comment says.

### State: runs again, stops half-way, old or empty state

| State on the machine                                                          | Result                                                                                                                                                                                                                                                                                                             |
| ----------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| No settings file, `{}`, or a file that cannot be parsed                       | Every fork setting at its default: updates off, also when an admin policy defaults them on (only a locked policy value turns them on).                                                                                                                                                                             |
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
change, or a fork line beyond the seams, it stops and opens one issue. Then:

1. Rebase by hand as the workflow's report says. Never take upstream's side of a fork setting's
   default: keep the fork's value (and the `Fork:` comment of the inline `automaticUpdates`).
2. Check that the fork's lines in upstream files are only the seams:
   `node .github/scripts/fork-seam-check.cjs <upstream tag> HEAD`. A refused line moves into the
   fork's own file (a setting into `forkSettings.ts`); a changed seam line is fixed in
   `.github/fork-seams.json`.
3. Run the pin tests before pushing:
   `vp test run packages/contracts/src/forkDefaults.test.ts packages/contracts/src/forkSettings.test.ts packages/shared/src/forkSettingsPatch.test.ts apps/server/src/serverSettings.forkDefaults.test.ts apps/server/src/serverSettings.forkSettings.test.ts apps/server/src/serverSettings.forkIntervals.test.ts apps/server/src/cloud/autoUpdateSettings.test.ts apps/server/src/cloud/autoUpdate.test.ts`.
4. Push with `--force-with-lease` and start fork-follow once, as its report says.

### The sync that brings upstream's updater

When upstream's server updater (upstream PRs 17585 and 17586) reaches a nightly, the rebase stops on
the `automaticUpdates` line in `settings.ts`. That stop is wanted. Three fork pull requests follow, in
this order, so that "half-way" always means "no automatic update":

1. **Remove** the fork's update scheduler and its work gate (keep the gate itself while
   `serve --hold-work`, below, uses it), and add a start-up step that deletes a
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

## Trying a build on a copy: `serve --hold-work`

`t3 serve --hold-work --base-dir <copy>` starts a server on a copy of a T3 home with the work gate
(`apps/server/src/workAdmission.ts`) held from boot until exit. Use it to look at a new build's
threads, settings and migrations before it replaces a running T3. It is refused for the background
service.

Held (nothing of it starts until the server restarts without the flag):

- agent turns, steers and rollbacks (outbox effects stay pending, unclaimed);
- thread launches and worktree preparation;
- every scheduler source: scheduled tasks, webhook coalescing, waits, usage-limit recovery and
  usage refresh;
- work an automatic update held (its marker stays);
- automatic project pull;
- following and stopping delegated tasks in linked environments (owed stops stay owed);
- the startup analytics heartbeat;
- T3 Connect. A full copy carries the running server's Connect link (its environment id, relay link
  and tunnel). The held server starts no tunnel, registers none with the relay and releases none when
  it stops, so remote access stays with the running server. It publishes no agent activity (thread
  state) to the relay under that id. A client's request to link, change the link, unlink or answer
  the relay is refused with the reason, and so is every CLI unlink on that home (`t3 connect unlink`,
  `t3 connect logout`, `t3 up` turning Connect off) while the held server runs: unlinking ends in a
  relay delete of the shared environment id, which would remove the running server's link too.
  The stored link files are kept as they are (`apps/server/src/cloud/holdWorkConnect.ts`).

  The CLI can only tell a copy is held while its server runs. Never run `t3 connect unlink` or
  `t3 connect logout` against a stopped copy's home: it removes the running server's link. Delete
  the copy's home instead.

Still runs:

- startup recovery on the copy's database, exactly as on any boot: runs that were mid-turn end as
  interrupted, process-bound starts are cancelled, a user's unsent message goes back to the queue,
  and continuations are written (they stay pending);
- provider status checks, which start the provider CLIs (`claude`, `codex app-server`) without a
  turn. Run the copy with an empty `HOME` and a bare `PATH` if they must not start;
- the UI, WebSocket and HTTP API, and timers that only change thread state (pull request watch and
  sync, auto-settle);
- anything a user asks for directly, such as a commit message or a pull request guide;
- reading the copy's Connect status;
- the periodic Connect reachability check: read-only requests to the stored public address and the
  tunnel network. It restarts only a tunnel this server started, and a held server starts none.

Startup refuses to serve when the gate is not held or a part that starts work (outbox, thread launch,
recovery, scheduler) does not use it. The check runs before activation, so nothing parked has run.
