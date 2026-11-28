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
  later run. Every list the checker reads (main's, the merge-base's, fork-follow's head) is parsed
  the same way: one with a duplicate key stops the run (exit 3), naming the commit and the key, as
  JSON keeps only the last of two equal keys and the other entry would be shown but never apply.
  `pending` maps upstream files
  whose fork lines predate the rule to a ceiling: the most fork lines each may hold, counted from
  upstream (fork-follow fails above it and below it). So a pull request that moves fork lines out
  of a pending file lowers its ceiling in its own list, in the same change: its run refuses a
  change to the file unless its own ceiling is exactly main's, plus the lines it adds, less the
  lines it removes, less the upstream lines it puts back (main's `lost` `gone` less its own), since
  per line text fork lines less gone ones is the copies over upstream's. With none left the entry
  goes, or moves to `files` with `"added": []`. With some left it may move to `files` as a
  declared seam only in a list-only change to that file (its text unchanged), when the new entry's
  `added` lists exactly as many copies as are left, each one the file has, and its `announced`
  none the file has; the run from upstream then holds every one of them to that entry. Which
  lines are the fork's only upstream can tell, so for such a move, and for every seam entry whose
  `added` the pull request's own list changes (a line listed more or less often, or an entry with
  lines added or dropped; a change to `announced` only, or an entry that lists nothing, is not
  one), fork-checks' run fetches upstream's default branch from the URL in main's
  `.github/fork-upstream.json` (never the pull request's copy; `--upstream <url>` by hand; only
  `https://`) read-only (commits only, filter `tree:0`, into
  `refs/fork-seam-check/upstream`, which each fetch overwrites and each run deletes) and holds each such entry's
  `added` to exactly the head's fork lines counted from the merge-base of main and upstream's
  head, both ways: a line listed more often than the fork adds it (a raised count with no copy
  added, or a line upstream has) is spare room, and a line listed less often (dropped while still
  in use) would stop the next fork-follow; both are refused in the pull request's own run. The same
  run holds every `pending` ceiling the own list sets, raises, lowers or drops to the fork lines
  counted from there, every `lost` entry it changes to the lines lost from there (both as
  fork-follow counts them), and a changed `forkFiles` to taking no upstream file the merge-base's
  list did not: raised with no line changed, a ceiling or `lost` count is room for a later change to
  swap upstream lines for fork lines within it. An entry for a file the head does not have is
  refused too. A list-only change that fixes main's list is counted the same way, so it passes. A
  change that changes no such entry fetches nothing. Every way the count cannot run is refused, naming why, never
  skipped: no upstream file on main, a malformed one (or one not `https://`), a shallow clone, an
  upstream that cannot be fetched within two minutes (two tries), one that shares no commit with
  main or holds main itself (the URL names the fork), or a branch without the upstream commit main
  sits on (update the branch). That refusal says it is an upstream, fetch or branch problem, not a
  wrong list: run it again once the cause is fixed ("Re-run failed jobs" on the Fork checks run,
  which runs main's script on the same commits, or push to the branch); to check an entry by hand,
  `node .github/scripts/fork-seam-check.cjs --from-upstream "$(git merge-base origin/main
  upstream/main)" <pull request head>` (upstream's main fetched as `upstream/main`). Fork tests'
  run on the merge commit has only two commits, so it passes `--no-upstream` and leaves the count
  to fork-checks. Left higher, the next run from upstream refuses main for the spare room; set
  lower or dropped, for the lines still there. Neither in `pending` nor moved may a change gain a fork line (an upstream
  line swapped for one is one more), whatever its list says. It reads its own list only to
  refuse, never to widen the gate, and does not hold a list-only change (the file's text
  unchanged), which may correct main. Right after a rebase, below the ceiling is not refused: when upstream takes a fork line word for word, the rebase drops
  it, and the ceiling could not have been lowered first. fork-follow then lowers it itself, in a
  commit of its own on the rebased series ("fork seams: lower counts after the rebase onto …", each
  change in its body and in the run's summary), and checks the lowered list again before it goes
  on. It never raises one. A pending file with no fork line left that a `lost` entry still names
  (a ceiling is 1 or more, and a `lost` entry needs its file in `files` or `pending`) is moved to
  `files` with `"added": []` in that commit, which holds it tighter: no fork line may come back, and
  a pull request may lose none of its lines. The run's summary lists the lowered counts once the
  plan has finished; a run stopped after the lowering pushes nothing. A pull request may change a pending file but not grow it. A
  pull request that changes a pending file's text is counted from upstream in its run
  (fork-checks), against main's ceiling and `lost` entry, even with its list unchanged: within the
  counts its run cannot tell an upstream line from a fork line, so a change that swaps an upstream
  line for a fork line keeps every count it can see. Upstream lines a seam or pending file no longer has (deleted, moved below the code they
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
  (fork-follow lowers a count after a rebase, but never raises one). A pull request may drop a seam
  file's listed seam lines (no more copies than listed) but lose no other line, and may not move a
  seam line: a moved copy of `}` looks the same as upstream's (move one with two pull requests).
  A seam file with a `lost` entry may get upstream's lines back (#968): a pull request that lowers
  that entry in its own list (neither count raised) may add one line per `gone` it lowers, and lose
  one line of main's file per `lines` it lowers beyond those (a line moved or re-indented back). A
  line put back where upstream has it is in order again, so it lowers both: an entry that lowers
  `gone` more than `lines` puts a line back out of place (a fork change to upstream's order) and
  allows nothing. Its run counts the lowered entry from upstream exactly, so a fork line in place of
  upstream's is refused there. With `lost` left as main's, a line put back or moved back is refused
  as an unlisted one, and the refusal says to lower the entry (or, when main's entry changed since
  the branch's merge-base, to update the branch from main).
  A seam's `added` lists only the copies the fork adds: the run from upstream refuses a listed copy
  the fork does not add over upstream (a spare one would let a later change add that line
  unannounced), and right after a rebase that took the line word for word fork-follow drops it in
  its lowering commit instead. So a pull request that drops listed seam lines drops them from its
  own list in the same change, exactly: its run refuses it unless its own `added` lists, for each
  such line, main's copies less the ones it drops (when main's list changed since the branch's
  merge-base, the refusal says to update the branch from main).
  A fork that adopts this checker (or updates an older copy) runs
  `node .github/scripts/fork-seam-check.cjs --from-upstream <upstream base> HEAD` once and fixes
  its list (each spare `added` copy it names) with a list-only pull request before the checker
  lands: fork-follow's run before the rebase refuses a spare copy and never lowers it, so an old
  list with spare copies stops one fork-follow run otherwise. It also adds
  `.github/fork-upstream.json` naming upstream's `https://` git URL: without it, every pull request
  that moves a pending file or changes a seam's `added`, a ceiling, a `lost` entry or `forkFiles`
  is refused.
  Dropped lines are counted by text, so where upstream has a listed line too (`}`), a pull request
  may drop upstream's copy instead of the fork's and its run passes it; fork-follow, which counts
  from upstream, refuses it unless the lines left read the same as with the fork's copy dropped. In a pending file it cannot tell
  upstream's lines from the fork's (moving a fork line out loses one), so there fork-follow counts
  them. A listed path that neither the base nor the head has (upstream
  renamed it) is a note: list both names until the old one is gone from the base. An upstream file
  in no list may gain no line, lose no line and keep its lines in order and indentation (trailing
  blanks and blank lines aside), and no upstream file may be deleted or renamed, except the workflows
  [`fork-removed-workflows.txt`](../../.github/fork-removed-workflows.txt) lists and upstream
  `fork-*.yml`. A submodule pointer counts as a line. `apps/web/src/routeTree.gen.ts` is TanStack's
  output from `apps/web/src/routes/`: in a change that also changes a route file it is not judged
  line by line (a fork route's lines repeat upstream's), and
  [`forkRouteTree.test.ts`](../../apps/web/src/forkRouteTree.test.ts) refuses a committed tree that
  is not the generator's output for the committed routes, with the options `vite.config.ts` passes to
  `tanstackRouter(...)`. On a pull request the skip holds only while that test is main's: a pull
  request that changes it has its tree judged line by line, so change the test in its own pull
  request (and update a branch from before such a change). On a rebase conflict in the tree, take
  either side and regenerate it (`vp run dev` or a build writes it), then commit it. A router
  version bump or a new `tanstackRouter` option can rebase cleanly and still change what the
  generator writes: fork-follow's release tests then stop on `forkRouteTree.test.ts`. Rebase by hand
  (the runbook below), regenerate the tree and commit it into the rebased series before pushing; a
  pull request that changes only the tree is judged line by line and refused. A pull request's run has no upstream commit,
  so it takes files named as the fork's (`fork`, `fork-*`, `forkName*`, `FORK_*`) and `forkFiles`
  for the fork's own; from upstream, a pattern that matches an upstream file fails. Right after a
  rebase it is not refused: fork-follow excludes the file itself in its lowering commit (the
  file's own `forkFiles` entry goes; a fork-named path or one another pattern takes gets `!path`),
  which only narrows what a pull request's run takes for the fork's own. Before a rebase (a list
  on main that lets one through) it still fails: remove the entry or add the `!path` with a pull
  request. Either way a pull request's run then judges the file as upstream's. For the same
  reason a pull request's run counts upstream's copies of a seam line against its allowance: a
  seam line upstream also has (`}`, `/**`) cannot be added by a pull request, only checked by
  fork-follow, so keep seam lines specific. A listed copy main's tip already has is spent too, not
  only one the branch's merge-base has: two branches from one main that each add the one listed
  copy are each green, and once one merges the other is refused ("update the branch from its
  base"). Neither seam check runs on `merge_group`. Two changes that are fine apart but not together
  (a seam line in both, or a pending file each shrinks) are judged together because main's ruleset
  requires a branch to be up to date with main before it merges (#444): the later one is updated
  from main, and that update runs both checks again. A branch made before fork-follow last
  rebased main is refused with one "update the branch" error. Only `--from-upstream` (which fork-follow passes)
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

1. **Announce** (list only, reviewed like any gate change: the `gate` rule of
   [`.github/fork-pr-queue.json`](../../.github/fork-pr-queue.json) lists `.github/fork-seams.json`,
   so a change to the list needs the merge-path and state lenses, and the code lens too when it
   also changes a file that no `gate` path matches): add the lines under `announced` in the
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

A pull request that changes a file and whose list drops an announced line main's list has, in date
(from a branch made before the announcement merged, say), is refused: keep it under `announced`,
or move it to `added` in the change that adds it. Withdraw one on purpose with a list-only pull
request; an expired one may go in any change.

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
a list-only pull request to main once it is pushed (fork-follow's refusal says how). The code
holds this: a pull request's run counts every ceiling and `lost` entry its own list changes from
upstream (above), so a new or raised one passes only when the tree already has those lines, as
after upstream's own change. What else it holds:

- A pull request's run is judged by main's list. Main's `lost` counts are fork-follow's to check,
  and main's ceilings refuse a change that shrinks a pending file without lowering it in its own
  list: a pending file may change but not grow. A new `pending` entry (counted, so only for lines
  the tree already has) does loosen it: that file's lines may then change, though not grow.
- From upstream, fork-follow refuses a ceiling or count above the real count ("lower it", or
  "remove it" when none is left), so room nobody uses does not stay. Right after a rebase it lowers
  it in a commit of its own on the rebased series instead (only lowers: a rise is refused).

When the list or the files under it change, the check fails closed:

- **No list, an empty one, or one it cannot read** (bad JSON, `{}` with no `files`, an unknown
  key such as a newer check's): a pull request's run against a base without the list is refused
  (open it against main); a branch whose merge-base has none is refused ("update the branch"); a
  list it cannot read stops the run with a crash (exit 3), which fork-follow reports as a failed
  check and pushes nothing. A pull request's own list that is malformed is refused too.
- **An old list** (from before `pending`, `lost` or `forkFiles` entries existed): a missing key is
  empty, which is stricter, never looser: every fork line it does not list is refused.
- **An upstream file renamed or deleted**: a pull request that deletes or renames an upstream file
  is refused (except the listed removed workflows); from upstream, a listed file upstream renamed or
  deleted, which neither the base nor the series has any more, is a note (list both names until the
  old one is gone from the base), and under its new
  name it is an unlisted upstream file, held to no fork line; a `pending` file the base lacks is
  refused.
- **Right after a rebase**: a ceiling or count that fell is lowered and committed into the series,
  one that rose is refused with the operator's next step, and the next run judges the lowered list
  from the new base. A re-run of the same rebase builds the same files (the lowering commit with
  them), so it advances to a release an earlier run published from them.

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
2. First, right after the rebase and before the rest of the sync: if upstream changed a composite
   action the fork keeps its own copy of (today `.github/actions/setup-ci-deps`, copied as
   `.github/actions/fork-setup-ci-deps`), port that change into the copy and record it:
   `node .github/scripts/fork-setup-copy-check.cjs` names the change and the `git diff` to read;
   after porting, `node .github/scripts/fork-setup-copy-check.cjs --record`. Fork tests fail
   ("Compare upstream's setup-ci-deps with the fork's copy") until the copy's recorded blob id
   matches upstream's file, so a sync cannot land with the copy behind it.
3. Check that the fork's lines in upstream files are only the seams:
   `node .github/scripts/fork-seam-check.cjs --from-upstream <upstream tag> HEAD`. A refused line moves into the
   fork's own file (a setting into `forkSettings.ts`, a server layer into
   `apps/server/src/fork/forkLayers.ts`); a changed seam line is fixed in
   `.github/fork-seams.json`. A `pending` ceiling or `lost` count that rose with upstream's own
   change (the refusal says "after the rebase") is raised in the rebased series' list (or, once
   pushed, with a list-only pull request to main).
4. Run the pin tests before pushing:
   `vp test run packages/contracts/src/forkDefaults.test.ts packages/contracts/src/forkSettings.test.ts packages/shared/src/forkSettingsPatch.test.ts apps/server/src/serverSettings.forkDefaults.test.ts apps/server/src/serverSettings.forkSettings.test.ts apps/server/src/serverSettings.forkIntervals.test.ts apps/server/src/cloud/autoUpdateSettings.test.ts apps/server/src/cloud/autoUpdate.test.ts apps/web/src/forkRouteTree.test.ts`.
5. Push with `--force-with-lease` and start fork-follow once, as its report says.

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
service, without `--base-dir`, and on the default home (`~/.t3`, also through a symlink), before it
starts or writes anything: a held boot marks its home as a copy (below), and on the live home that
would hold its T3 Connect at every later boot. `T3CODE_HOME` alone does not name the home for it.

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
    The server reads the marker once as it starts, and T3 Connect and agent activity publishing
    share that one read, so they always agree. A marker added or deleted while it runs counts from
    its next start.
  - `t3 connect login`, `link`, `unlink` and `logout`, `t3 connect publish` turning publishing on,
    and `t3 up` turning T3 Connect on or off are refused on that home, running or not, before they
    send anything or change the link or sign-in (the CLI may still create the home's folders and
    signing key, as any command there does). Unlinking ends in a relay delete of the shared
    environment id, which would remove the running server's link too, and signing in can refresh
    the copied sign-in, which the running server shares.

  Anything at that path counts, even an empty file or a dangling symlink, and so does a path that
  cannot be checked (a disk or permission error holds T3 Connect for that boot, on purpose). If the home is not a copy, delete the file and run the command or start the
  server again.

  A copy with no marker is not protected: one that has never booted held, one held-booted only by
  a build from before the marker (its first held boot on a newer build marks it), or one copied with
  only `userdata/` from a marked copy (as the go-live kit's `copy` step copies). The marker sits in
  the home, beside `userdata/`, while the shared identity sits in `userdata/`: such a copy carries
  the identity but not the marker. (That is also why restoring `userdata/` can never bring the
  marker into the live home.) Neither the CLI
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
