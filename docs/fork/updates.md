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
  **Never widen an existing interval setting** (to `null` or "off" for "never"); give the new
  encoding a new key. A build from before the widening still follows the old key, reads the value
  as its default, and its next save deletes it, so a rollback and roll forward turn "never" into
  the default. That build cannot tell a newer build's `null` from the `null` older builds wrote
  for an infinite interval. Keeping the stored value on a save that leaves the setting alone is
  not done: it needs a line in upstream's write path, and it would keep a bad value (`-5`, a
  string) in the file for good, where today the next save cleans it. A widened interval turns the
  `outOfReach` check in `serverSettings.forkIntervals.test.ts` red in the build that widens it.
- The seam check ([`fork-seam-check.cjs`](../../.github/scripts/fork-seam-check.cjs)) refuses any
  line the fork adds to an upstream file beyond the seam lines pinned in
  [`.github/fork-seams.json`](../../.github/fork-seams.json), with or without a comment, in every
  upstream file, listed or not. It runs on every pull request (Fork tests, "Workflow lint and
  upstream drift") and in fork-follow before and after each rebase, against the upstream commit
  the series sits on. A pull request is judged by its base's list, never by its own: a new seam,
  `pending` or `forkFiles` entry counts once the owner merges it (a new seam line takes two pull
  requests, [below](#adding-a-seam-line)). Its own list is only parsed: a malformed one (bad JSON,
  an unknown or duplicate key, a `files` or `pending` key that is not a plain repository path, a
  `lost` entry not in the form below, a line in both `added` and `announced`, or none) is refused
  (a repeated `"//"` comment key is a duplicate too: give each comment its own `//name` key), since once merged it would stop every
  later run. `pending` maps upstream files
  whose fork lines predate the rule to a ceiling: the most fork lines each may hold, counted from
  upstream (fork-follow fails above it and below it, so a pull request that moves fork lines out
  also lowers the ceiling in the same change). Right after a rebase, below the ceiling is only a
  note in the run log and the run's summary: when upstream takes a fork line word for word, the
  rebase drops it, and the ceiling could not have been lowered first. The next run, from the new
  base, fails until a pull request lowers it. A pull request may change a pending file but not grow
  it. Upstream lines a seam or pending file no longer has (deleted, moved below the code they
  guard, or re-indented) add no line, so they are counted too, two ways: `lines`, the upstream lines
  it does not keep in order and indentation, and `gone`, those whose text it no longer has at all,
  so deleting a line already counted as moved or re-indented raises `gone`. `lost` maps such
  a file to both (`{"lines": N, "gone": M}`), counted from upstream (none when unlisted) and held
  exactly like a `pending` ceiling. A count is not a set of lines: in a file with a `lost` entry a
  line it counts may move unseen, and in a pending file with one a pull request can put back one
  gone upstream line and delete a different one with both counts unchanged (neither run sees it),
  so keep each count small. Right after a rebase, a count or ceiling that rises came with the
  rebase, not a fork pull request: the refusal tells the operator to rebase by hand and raise it
  in the rebased series, or to push it and raise it with a list-only pull request to main
  (fork-follow cannot commit it until #325 item 1). A pull request may drop a seam file's listed seam lines (no more
  copies than listed) but lose no other line, and may not move a seam line: a moved copy of `}`
  looks the same as upstream's (move one with two pull requests). In a pending file it cannot tell
  upstream's lines from the fork's (moving a fork line out loses one), so there fork-follow counts
  them. A listed path that neither the base nor the head has (upstream
  renamed it) is a note: list both names until the old one is gone from the base. An upstream file
  in no list may gain no line, lose no line and keep its lines in order and indentation (trailing
  blanks and blank lines aside), and no upstream file may be deleted or renamed, except the workflows
  [`fork-removed-workflows.txt`](../../.github/fork-removed-workflows.txt) lists and upstream
  `fork-*.yml`. A submodule pointer counts as a line. A pull request's run has no upstream commit,
  so it takes files named as the fork's (`fork`, `fork-*`, `forkName*`, `FORK_*`) and `forkFiles`
  for the fork's own; from upstream, a pattern that matches an upstream file fails. Then remove the
  entry (or exclude a fork-named path with `!path`): from then on a pull request's run judges the
  file as upstream's, also before the rebase that brings upstream's copy. For the same
  reason a pull request's run counts upstream's copies of a seam line against its allowance: a
  seam line upstream also has (`}`, `/**`) cannot be added by a pull request, only checked by
  fork-follow, so keep seam lines specific. A branch made before fork-follow last rebased main
  is refused with one "update the branch" error. Only `--from-upstream` (which fork-follow passes)
  runs it from an upstream commit and reads the head's list; without it a base with no list is
  refused, and an upstream that is itself a fork with a list is still judged by this fork's list.
- [`serverSettings.forkDefaults.test.ts`](../../apps/server/src/serverSettings.forkDefaults.test.ts)
  loads the settings the way a server does and checks every fork setting is at its row's default
  with no file, `{}`, a file an upstream build wrote, a file cut off half-way and a file with a bad
  value. A file that cannot be read gives no settings at all, and the update loop then does nothing
  (`autoUpdate.test.ts`).

### Adding a seam line

A pull request is judged by main's list, so a change that adds a line to an upstream file and its
seam entry together is refused. It takes two pull requests, each green:

1. **Announce** (list only, reviewed like any gate change): add the lines under `announced` in the
   file's entry, with the day: `{"since": "YYYY-MM-DD", "added": [lines]}`. Set `since` to the day
   this pull request merges (refresh it if the reviews took days): its run warns when the
   announcement it adds would be refused within 3 days. A file with no entry yet gets one with
   `"added": []`. The seam check allows an announced line like an `added` one. The seam-check tests
   (on the pull request's tree) refuse an announced line that is already in its file: a line
   upstream has (`}`) cannot be announced, as a pull request cannot add it. A line is in `added` or
   in `announced`, never both, and holds no line break (one line per item). A file has one
   `announced` block and one `since`: a second announcement in a file that already has one joins
   that block's list (`announced.added`, not the entry's `added`) and sets `since` to its own day,
   which renews the 14 days for the lines already there too (a visible change of `since` for the
   reviewers); left as it was, the new line gets only the days that are left.
2. **Add** the lines, and in the same change move them from `announced` to `added` (drop
   `announced` once empty). Judged by main's list, which announces them, it is green. Keeping the
   announcement fails the tests: kept as it is, the lines are now in the file; copied into `added`
   too, the list is malformed. Review where the line goes, not only the list edit: any pull request
   may spend an announced line, not only the one meant to.

An announcement lasts 14 days. From upstream, fork-follow refuses one more than 14 days older than
the head's commit date (or dated more than a day ahead of it): announce the lines again (set `since`
to the day that pull request merges) or remove the announcement, with a list-only pull request to
main; only then step 2. Step 2 alone is refused as well once main's own commit date is past the
announcement's days. That is so whenever fork-follow's check before the rebase refused it (its head
is main). Its check after the rebase reads a new head, dated when the rebase ran, so an announcement
can expire there while main's date is still inside the days: step 2 alone is then green and fixes it
too. The same commits give the same answer. A pull request's run refuses only a line that an expired
(or dated-ahead) announcement alone allows, dated by the commit whose list judges it (main's, never
the pull request's), so the pull request that re-dates or removes the announcement adds no line and
is green. A line added more often than the announcement lists is refused as any unlisted line is,
announced again or not. In a pull request's run the 14 days are main's commit dates, not the
calendar: while nothing merges, main's date stands still (fork-follow does not push past an expired
announcement), and the next merge moves it. If upstream itself adds an announced line, fork-follow
stops before pushing (pushed, main's tree would fail the seam-check tests for every pull request):
remove the announcement with a list-only pull request to main.

To go back to a seam check from before announcements (#409), remove every announcement first: an
older check refuses the `announced` key, and a list it refuses stops every run.

For example, [#398](https://github.com/scuttletoby/t3code/pull/398) takes one list-only pull
request first. It announces the new cli/connect.ts lines in main's existing entry, keeping the
three old lines #398 replaces in `added`, and adds `apps/server/src/cloud/holdWorkConnect*` to
`forkFiles` (#398 changes them, and a pull request's run takes these fork files, not named as the
fork's, for upstream's until `forkFiles` names them):

```json
"apps/server/src/cli/connect.ts": {
  "why": "…, and the CLI's Connect-on check (requireConnectAllowed) refuses on a held copy too.",
  "added": [
    "import * as HoldWorkConnect from \"../cloud/holdWorkConnect.ts\";",
    "// Fork: a held copy shares the live server's environment id; refuse before the relay delete.",
    "yield* HoldWorkConnect.refuseCliUnlinkWhileHeld(",
    "(yield* ServerConfig.ServerConfig).serverRuntimeStatePath,",
    ");"
  ],
  "announced": {
    "since": "2026-11-19",
    "added": [
      "yield* HoldWorkConnect.refuseCliOnHeldCopy(yield* ServerConfig.ServerConfig);",
      "yield* HoldWorkConnect.refuseCliOnHeldCopy(yield* ServerConfig.ServerConfig); // Fork: a held copy"
    ]
  }
}
```

The three old lines stay listed until #398 merges: a pull request may delete only lines main's
list names, so dropping them first makes #398 red on its own deletions. #398 then drops them from
`added` in its own change, as its tree no longer has them (the seam-check tests refuse a listed line
its file lacks). [#412](https://github.com/scuttletoby/t3code/pull/412)'s file, which has no entry,
gets `"apps/server/src/peer/RemoteDelegation.ts": {"why": "…", "added": [], "announced": {"since":
…, "added": [its 10 lines]}}` in the same list-only pull request. Each pull request then updates
from main and moves its lines to `added`.

A new `forkFiles` pattern needs no announcement: a list-only pull request adds it, and the pull
request that edits those files follows. A `pending` file is not for new fork lines: its lines
predate the rule and only move out. So only the operator adds a `pending` file or raises a ceiling
or a `lost` count, after upstream's own change made the count rise: in the rebased series, or with
a list-only pull request to main once it is pushed (fork-follow's refusal says how). This rule
rests on the owner's review, not on code: a pull request's own list is only parsed, so no check
refuses one that adds a `pending` file or raises a ceiling or count. What the code does hold:

- A pull request's run is judged by main's list and reads no ceiling and no `lost` count: a pending
  file may change but not grow, so a raised ceiling or count gives no pull request a line. A new
  `pending` entry does loosen it: that file's lines may then change, though not grow in number.
- From upstream, fork-follow refuses a ceiling or count above the real count ("lower it", or
  "remove it" when none is left), so room nobody uses does not stay. Right after a rebase that is
  only a note, and the next run refuses it.

A pending file takes no new fork line (a pull request may change its lines but not add more, and a
file cannot be in `files` and `pending`), so a new fork line goes in a seam file, after its lines
are announced.

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
   [FORK_CHANGES.md](../../FORK_CHANGES.md); `packages/shared/src/forkDocs.test.ts` (the README) and
   `.github/scripts/fork-changes-list.test.cjs` (FORK_CHANGES.md) fail until both match the row in
   `forkDefaults.ts`.

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
   `node .github/scripts/fork-seam-check.cjs --from-upstream <upstream tag> HEAD`. A refused line moves into the
   fork's own file (a setting into `forkSettings.ts`, a server layer into
   `apps/server/src/fork/forkLayers.ts`); a changed seam line is fixed in
   `.github/fork-seams.json`. A `pending` ceiling or `lost` count that rose with upstream's own
   change (the refusal says "after the rebase") is raised in the rebased series' list (or, once
   pushed, with a list-only pull request to main).
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

Held (nothing of it starts until the server restarts without the flag, except T3 Connect: see below):

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
  the relay is refused with the reason. The stored link files are kept as they are
  (`apps/server/src/cloud/holdWorkConnect.ts`), with one exception: a managed policy that turns
  T3 Connect off makes the server unlink while it starts, before the hold applies. That removes the
  copy's own link files and sends nothing to the relay; the policy has unlinked the running server
  on that machine already.

  The first held boot writes `hold-work-copy.txt` in the copy's home, and nothing removes it, not
  a normal boot either. While it is there:
  - Every boot of that home holds T3 Connect and agent activity publishing as above, with or without
    `--hold-work`, and logs the marker's path. A copy started normally (`t3 serve` or `t3 up` on its
    home) runs its work but never registers or releases the tunnel under the shared environment id.
    The server reads the marker once as it starts: one added while it runs counts from its next
    start.
  - `t3 connect login`, `link`, `unlink` and `logout`, `t3 connect publish` turning publishing on,
    and `t3 up` turning T3 Connect on or off are refused on that home, running or not, before they
    send anything or change the link or sign-in (the CLI may still create the home's folders and
    signing key, as any command there does). Unlinking ends in a relay delete of the shared
    environment id, which would remove the running server's link too, and signing in can refresh
    the copied sign-in, which the running server shares.

  Anything at that path counts, even an empty file or a dangling symlink, and so does a path that
  cannot be checked. If the home is not a copy, delete the file and run the command or start the
  server again.

  A copy with no marker is not protected: one that has never booted held, or one held-booted only by
  a build from before the marker (its first held boot on a newer build marks it). Neither the CLI
  nor the server can tell it from the running server's home: never run those commands against it or
  start it normally. Delete the copy's home instead.

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
- the held-hooks waker, which reads the copied relay credential but sends only when a tunnel
  connects, and a held server connects none;
- the relay client ledger's clean-up at start, which stops only a recorded tunnel client whose
  owner process is gone (a running server's own is skipped). Local only.

Startup refuses to serve when the gate is not held or a part that starts work (outbox, thread launch,
recovery, scheduler) does not use it. The check runs before activation, so nothing parked has run.
