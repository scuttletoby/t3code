# fork-ci release notes

Read this before you move a pin to a new `ci/vN` (see [Pinning](README.md#pinning)). Newest first. Every release
commit holds this file as main had it, so the entries in the new tag's copy that your pinned tag's copy lacks are what
the move brings:

```sh
git diff ci/vOLD ci/vNEW -- fork-ci/RELEASE-NOTES.md
```

An entry says what changed, and under **Upgrade** what to do before or after the move. An entry without one needs
nothing.

## After ci/v14

### lanes: a general test-lane pool and claims rule (#890)

New: [`lanes/`](lanes/README.md). `pool.py run` runs one lane's command once its servers are claimed and the pool
admits it from measured free memory, less what lanes admitted a moment ago have not used yet; `claims.py` is the
rule (shared or alone, a waiting restart first on every server it needs, dead holders swept by pid, start time and
process group or by unit, garbage claim files swept, one lock); `contract.py` holds the contract tests a project runs
against its own adapter. The claims folder and the limits a pull request must not loosen come from the runner's
environment (`FORK_CI_LANES_*`), never from the project's config.

**Upgrade:** none (nothing used it before).

### checkout-state: a lint for checkout state a self-hosted runner keeps (#932)

New action. It fails a workflow whose jobs on a self-hosted runner could leave checkout state in the shared work
folder (a sparse or partial clone, `clean: false`), and asks for a reviewed config entry for each action or reusable
workflow from another repository such a job uses. It fails closed (exit 2) on no PyYAML, no workflow file under its
root, and a config that cannot be read, is a broken link or has a key it does not know. The fleet-route README and
the fleet-watch example now say to drop their sparse checkout (or run git-clean first) on a self-hosted label.

**Upgrade:** a project with its own copy of this lint keeps its config file (same format), runs the action, and
deletes its copy.

### pr-queue, head-check: merge as GitHub's update does; a cancelled re-run no longer hides an old base's red (#924, #1033)

The queue and head-check now merge with `merge.directoryRenames=false` (was `true`), which is how GitHub's update branch
merges (measured): a file added in a folder the other side renamed stays where it was added. So the queue re-arms a
pull request that adds a file in a folder its base renamed after the update, where it was left off with a comment, and
head-check (its same-change test calls the queue's `holds`) now accepts that updated head, where it refused it as not
the change read. Two renames of one folder are still left off. head-check's merged-config check merges the same way: a
base that adds the config in a folder the head renamed now keeps it at its path and is judged on it, where it was
refused as "deletes the config". With `update_when_base_was_red`, a red that only cancelled runs followed is judged by
the base its red run tested, not by the cancelled run's job, which may have no note: such a pull request is now updated
when its base was red and is green now, and the red notice (and `notify_command`'s link) points at that red run, not
the cancelled one.

### pr-queue: every unreadable plan is kept and told once; the cache never raises (#934)

An unreadable or untrusted plan no longer replaces an earlier `STATE.bad`: a later one is kept as
`STATE.bad-<time in ns>`. Every run names each such file in a `::warning::` (shown on the run's page), not only a log
line. With `notify_command` the queue runs that command once for each new one, with kind `bad-plan`, PR `0`, an empty
head and the repository's pull request list as the link; which ones were told is kept in `STATE.told-bad`, removed once
no such file is left, and a name it holds is never given to a new one, so each new one is told. No kept plan
overwrites a file. A `STATE.bad` an older release kept is told once. A cache folder the queue cannot make or list,
or a stale ref lock it cannot delete, is said and the run fetches nothing (as before), without a Python traceback.

**Upgrade:** a `notify_command` that reads `pr` as a pull request number must accept `0` for kind `bad-plan`, or
ignore that kind.

### pr-queue and head-check: only a comment with the lens marker is a lens report (#865)

A comment counts as a lens report only when it carries the lens marker: a line that starts with the lens field
(`lens:`, `lens=`, `lens —`, `lens –`), names a read (`read at` and a hex sha), or has a lens field with a hex sha after
it (Markdown stripped, in code blocks and quotes too). A lens report that is not exactly one valid read still blocks, as
before. A comment that only looks like one without the marker (a `## Lens A` heading, `safe to merge`, `covered:` and
`checks:` lines) and holds no open finding no longer blocks: the run log and the head check's summary name it as
`not a lens report` (a `WARN` row) with the reason. One without the marker that holds an open finding (a `blocker:` or
`major:` line that is not `none`, NOT SAFE on its first two lines or a `verdict:` line, or `not safe to merge` on any
line outside a fenced code block) still blocks, as before, whatever an older or newer read of that lens says: it is
most likely a real report with a broken header. Its reason, first in the head check's, says to post a NEW comment
whose first line is `lens: <name> · reviewer: <who> · read at: <sha>` (editing the marker in does not count) and to
delete the old one or edit it to start with `not a lens` and a colon; it also stops holding once edited so it holds
no open finding (the same people could delete it). The queue posts that reason with the header described in words,
so its comment never looks like a lens report. The head check's check run on the pull request shows the
`not a lens report` row as `warn` too. Comments with the marker read exactly as before. T3's copy (`apps/server/src/fork/reviewRules`) and the review
kit's `check` read the same rule from the shared vectors.

**Upgrade:** a lens report written without the marker now counts for nothing, so a lens that posts one has not
reported: list required lenses (`required-lenses`, or a `review` block in `enforce`) so a missing read still fails the
head check, and have lenses run the kit's `check` before posting. An old comment without the marker that holds an
open finding blocks as it did before this release; one that holds none stops blocking.

### pr-queue: what counts as a `state:` line is gate code (#797)

`review_judge.has_state_line` holds the rule for a lens report's `state:` line (an answer after the colon, outside code
blocks and quotes). The gate still parses `state_line` without enforcing it, so nothing a caller sees changes. The
review kit (fork-kit/review) now imports this rule and the gate's `head_check.changed_paths` instead of its own
copies, so run the kit from a checkout at the release the gate pins (kit README, "Which fork version").

### pr-queue and head-check: every rule hit gives its lenses (#82)

With a `review` block in `enforce`, the required lenses are now the lenses of every rule a changed path counted for,
whatever its weight, not only those of the rules of the heaviest weight hit. An app change plus one critical file needs
the app rule's lens and the critical lenses; before, the critical lenses alone passed it and no lens read the app code.
The weight is still the heaviest hit. T3's copy of the rules (`apps/server/src/fork/reviewRules`) plans the same lenses.
First in the release after `ci/v22`.

**Upgrade:** a config in `enforce` needs more lens reads on mixed changes after the move, and a built-in protected path
(the config, a lens prompt, a workflow) under a lighter rule also needs that rule's lenses. To keep the protected paths
at the critical lenses only, put them in a critical rule. In `shadow` only the summary row changes. `ci/v21` and `ci/v22`
accept the same config but judge mixed changes the old, looser way, so a caller of a config in `enforce` pins this release
or a later one (pr-queue README, "Turning it on: release, pin, then config", step 1).

### pr-queue and head-check: review findings, rounds and `override_authors` (#740)

With a `review` block, the gate also judges the review itself from the lens comments. With `findings: true`, a lens's
newest report needs a findings section (`blocker:` and `major:` lines, or `findings: none`), and a blocker or major in
it fails the head. A round is a lens read of a different own change (a queue update adds none): from
`rounds.approach_at` the approach lens is required, and past `rounds.max` a person decides. The new optional key
`review.override_authors` lists the logins whose override comment counts as a person's (empty or left out: none
does). In `shadow` the check summary only adds a row "findings and rounds (shadow): they would have said: ...". Without
a `review` block nothing changes. First in the release after `ci/v20`.

**Upgrade:** `ci/v20` and older refuse a config with `override_authors`: move every pin first, then add the key
(pr-queue README, "Turning it on: release, pin, then config"). A config already in `enforce` with `findings: true`:
after the move, a required lens whose newest report has no findings section fails the head, so have the lens prompts
write `blocker: none` and `major: none` first, or switch to `shadow` before the move. In both modes a head whose
rounds cannot be judged (an error while reading the lens comments) now fails instead of passing on the old rule.

### handoff-put and handoff-get: a file handed from one job to a later job of the same run (#247)

- New actions [`handoff-put`](actions/handoff-put/README.md) and [`handoff-get`](actions/handoff-get/README.md). put
  wraps a text file as `{format, name, sha, payload}` with the checkout's HEAD and uploads it as the artifact
  `<name>-<sha>` (pinned `actions/upload-artifact`, three days' retention by default, a re-run attempt overwrites
  it, a second producer of the same name in a first attempt is refused with `ok` `'false'`). get
  downloads it from the same run only (pinned `actions/download-artifact`) and writes the payload only when the `sha`
  inside is the checkout's HEAD. Neither fails its step for a missing, refused or forged hand-off: `ok` is `'false'`
  and the consumer falls back to its old `runs-on`, which keeps the fork guard (README, "How the consumer picks its
  runner"). A `path` whose folder is a symlink out of the workspace is refused.
- git-clean's test now also counts an action that runs another action folder's script (handoff-get runs
  handoff-put's `handoff.py`) as one that runs git.

### pr-queue: `notify_command` runs once for each notice on an armed pull request it passes over (#129)

- New optional config keys `notify_command` (an argv list) and `notify_timeout_seconds` (default 30). When set, the
  queue runs the command right after it posted a notice on an armed pull request it passes over (a conflict, a red,
  cancelled or missing required check), with repo, PR number, head, kind, reason and a link as arguments (README,
  "Notify"). The posted comment is the record: a later run, a restart or another runner does not run it again for
  the same head and reason.
- A command that is missing, fails or runs too long is a warning in the run log and never changes what the run does
  or its exit code. No token is passed in its environment, but it runs as the queue's user (README, "Notify").

**Upgrade:** move the pin first, then add `notify_command` to the config: a queue of an older release refuses the
unknown key and touches no pull request.

### git-clean: the wipe stays in the workspace while another process changes it (#428)

- Going back up by `..` must land in the folder the wipe came from (device and inode): a folder moved out of the
  workspace while the wipe is inside it is emptied, and the step stops red there instead of emptying where it landed.
- A read-only folder is made writable without following a symlink swapped in for it (`fchmodat` with
  `AT_SYMLINK_NOFOLLOW`; on glibc older than 2.32, which refuses that for every path, through an `O_PATH` fd as newer
  glibc does itself).
- The workspace is opened from the runner's work folder one name at a time, never through a symlink, so a folder
  swapped for a symlink after the path checks is refused instead of followed. A work folder (`_work`, above
  `RUNNER_WORKSPACE`) that is a symlink is refused.
- A mount point inside the workspace (from `/proc/self/mountinfo`, a bind mount too) refuses the step before anything
  is removed. A folder on another device met during the walk (a btrfs subvolume, a mount made meanwhile) is not
  entered: the step fails with its path.
- Each folder is listed once (a wide folder was re-listed after each child: 6,000 folders took 20 s).
- weakened-tests: the refusal note no longer says the earlier report was for another head (wrong on a re-run).

**Upgrade:** check each self-hosted runner that uses git-clean before you move the pin. Each of these was emptied
before and now fails the step on every job until it is fixed:

- a work folder (`_work`) that is a symlink (for example to a bigger disk): configure the runner with the real path
  (`./config.sh --work <real path>`) or mount the disk there instead;
- a mount point inside a workspace: unmount it;
- a btrfs subvolume inside a workspace: remove it once by hand (`btrfs subvolume delete <path>`).

### pr-queue: a person's auto-merge switch during an update stands (#518)

- The queue reads the pull request right before turning auto-merge off: one that is off by then (or closed) is left
  as it is. It does not rely on GitHub's answer for that: what GitHub answers for turning off an auto-merge that is
  already off is not documented.
- The queue writes its plan as `"unsure": true` before turning auto-merge off and drops that once GitHub answered
  with success. When the turning off answers with an error and the pull request reads back off, or a stopped run left
  an unsure plan, the queue cannot tell whether a person turned it off: it leaves it off, does not update it, and
  says so on the pull request. It never re-arms it, as it did before.
- Right before `gh pr update-branch` the queue reads the pull request again; one armed again (or closed) since the
  queue turned it off is not updated, and the plan stays for the next run, which drops it if GitHub still shows it
  armed or closed and re-arms it if GitHub only showed the queue's own turning off late.
- Not guaranteed (GitHub has no compare-and-set for auto-merge; README, "The queue"): a person's switch-off at the
  very moment of the queue's call, if GitHub answers that call with success; an arm between the last read and
  `gh pr update-branch`; and an arm and switch-off again while the run waits for the new head or before the next run
  finishes a plan.
- A disarm that GitHub applied but gh reported as failed (an HTTP 502) now ends with the pull request off and a
  comment, instead of re-armed by the next run: arm it again with the ready command.

**Upgrade:** nothing. A plan an older queue left has no `unsure` and is finished as before.

### laws: a contract and a small runner for law tests (#464)

- New [`laws/`](laws/README.md): the contract for law tests (a fixed seed in CI, the seed and shortest failing steps
  printed, capped tries, no sleeps, a failure kept as a fixed test before its fix merges) and `laws.py`, its runner
  (Python standard library only, test-side). Projects link to the contract at their pinned tag.
- pr-queue gets its first law test, `pr_queue_laws.test.py`. No action changed.

### pr-queue: the merge check reads a checked copy, never the cache; FSCK_KNOWN out of the cache (#465)

- `git fsck` on the cache ends before the merge check starts (in an update it runs in the background, minutes
  before), and whoever can write the cache could change an object in between. The merge check now runs in a
  repository of its own (`CACHE.merge`, mode `0700`, made new and deleted in every check): each object it reads is
  taken out of the cache through one `git cat-file --batch`, hashed by the queue, and written there only when the
  hash is its name. Copied are the commits the head, the change read and the base reach, and per `merge-tree` the
  trees and blobs that differ between the two sides and their merge bases. Measured on this fork (270,000 objects,
  11,500 commits): about 2 seconds for the commits, 0.1 seconds for a merge of 300 changed files.
- An object that is missing or not what its name says is handled as a failed `git fsck` is: one download from
  scratch and the merge check once more in the same run; a second failure merges nothing in (the run keeps `STATE`
  and the PR comment says so), and a download that fails the same way as the last one is not repeated. One run
  downloads the cache from scratch at most once, whether `git fsck`, a fetch or the merge check asked for it.
- The merge check's object reader has a time limit: a `git cat-file` that owes one answer for 120 seconds is stopped,
  nothing is merged in and `STATE` stays. The queue's other git calls have no limit but the job's.
- If git says anything on stderr or stops in the copy, the whole trees of the merged commits are copied and the merge
  runs once more. A merge git calls clean while it reports an error is not taken as clean (git 2.43 merges a root
  tree it cannot read as an empty one, with exit 0). More than 8 merge bases: not merged, the head is not the change
  read.
- What the last download from scratch failed on is kept in `STATE.fsck-known` (next to the plan, mode `0600`, read
  only as a regular file of the queue's user that no one else can write), no longer in the cache as
  `queue-fsck-known`.
- A `/` or `/.` at the end of `--state-file`, `--cache` or `--lock-file` is dropped, so the copy, `CACHE.new` and
  `CACHE.old-*` are made beside the cache, never inside it; a path that is `/` or ends in `..` is refused (exit 2).

**Upgrade:** nothing to do. A `queue-fsck-known` inside a kept cache is ignored (never read, not deleted): a cache
whose history fails `git fsck` from GitHub itself is downloaded from scratch once more, then the new record stops
the next download. The copy is made beside the cache (`CACHE.merge`), where the queue already sets a broken cache
aside. Only for a queue called with `--cache CACHE/`: a `.new` or `.old-*` an earlier version left inside the cache
is no longer swept: delete it by hand (nothing reads it).

### pr-queue and head-check: the cache's objects are checked before each merge check (#457)

- Before its merge check the queue runs `git fsck` on its cache. git checks an object's hash on only some reads,
  never of a tree or blob `merge-tree` reads: a tree or blob an earlier job stored under one side's name made a real
  conflict merge clean. In an update the check runs in the background, while GitHub updates the branch. A cache
  `git fsck` finds broken, or one a fetch fails on for a reason that is not the network or the login, is downloaded
  from scratch once and checked again; one still broken is not merged in (the run keeps `STATE`, and the PR comment
  says the cache fails `git fsck`). Old formats `git fetch` takes (time zone `+99999`, a tree out of order, an odd
  `.gitmodules`) are ignored; a hash mismatch, a missing object or a corrupt pack never is. A cache that fails the same
  way as right after its download from scratch is not downloaded again: a person must look. A fetch's `git gc` runs
  in the foreground. `git fsck` reads every object on one core: about 28 seconds for 270,000 objects, at most once
  per run.
- The queue's cache and the gate's repository are made with `GIT_DEFAULT_HASH=sha1` and
  `GIT_DEFAULT_REF_FORMAT=files` (every git call gets both), and `HEAD` is written by the queue whatever branch git's
  `init` names (git 3.0 plans `main`). A git that still makes another format (an `[extensions]` section) is refused
  with the extension named: no cache, nothing fetched or merged; in the gate the head's check run fails.
- git calls that reach no remote (the queue's, the gate's `merge-tree`, and `head_check.py` in bare mode) drop
  `GIT_TRACE*` and `GIT_CURL_VERBOSE`, as the fetches from GitHub do; `HTTPS://` counts as https in any case. The
  head check's checkout mode keeps them, as before (a partial clone may fetch from `origin`).
- A plain `http://` remote, a `transport::address` remote and one that starts with `-` are refused: GitHub redirects
  http to https, where git hands the token over, a remote helper runs any program, and git reads `-...` as an
  option. `head_check.py --bare --remote ''` is refused (exit 2) instead of becoming `origin`.
- The gate checks git (2.43 or newer) and its remote before it makes its repository: either problem fails the head's
  check run ("not judged: ..."), with nothing made or fetched.

**Upgrade:** nothing to do. A kept cache is checked on its next merge check (it was made by the same code, so it
passes); a broken one is downloaded from scratch once.

### pr-queue and head-check: no attributes from HEAD; local git calls run no job program (#450)

- Every git call of the queue, the gate and the head check reads attributes from the empty tree
  (`GIT_ATTR_SOURCE=4b825dc642cb6eb9a060e54bf8d69288fbee4904`). In a bare repository git 2.43 to 2.45 read
  `HEAD:.gitattributes`, and git 2.43+ reads `attr.tree`: a cache whose `HEAD` an earlier job pointed at a commit
  holding `* merge=union` made the queue's same-change rule union-merge a conflict.
- The queue's cache must have the `HEAD` the queue writes (`ref: refs/heads/master`, a branch it never makes);
  a cache with another `HEAD`, a symlink, none, or that branch made is rebuilt.
- Only the queue's two fetches keep the job's `GIT_EXEC_PATH` (from GitHub) or `pr_queue.GIT_RUN` (another remote);
  its merge-tree, cat-file and rev-parse calls in the cache run none of them.
- `head_check.py --remote` without `--bare` is refused (exit 2): checkout mode fetches from the checkout's origin.
- git runs with `LC_ALL=C`, so its messages are matched in one language.
- A fetch from GitHub (https) drops `GIT_TRACE*` and `GIT_CURL_VERBOSE`: the trace would write the login header, and
  the gate puts git's error into its public summary.
- A fetch that failed for want of a login says so and names the token (`PR_QUEUE_READ_TOKEN` for the queue, `GH_TOKEN`
  for the gate).

**Upgrade:** the queue, the gate and the head check need git 2.43 or newer (`GIT_ATTR_SOURCE` came in 2.41, and
`merge-tree` with it crashed before 2.43); an older git never merges or judges anything. The queue refuses to start
(exit 2; it reads its config with git first and decides nothing on it), the head check refuses (exit 2), and the gate
fails the head's check run before it makes its repository (#457). A kept queue cache is rebuilt once if its `HEAD` is
not the queue's; nothing else to do.

### pr-queue and head-check: GIT_ATTR_SOURCE and the job's git programs (#442)

- Every git call of the queue and the head check also drops `GIT_ATTR_SOURCE` and `GIT_EXTERNAL_DIFF`. With
  `GIT_ATTR_SOURCE` naming a tree that holds `* merge=union`, the gate's `git merge-tree` and the queue's same-change
  rule union-merged a conflict instead of refusing it.
- The queue's and the gate's git calls drop the job's `GIT_EXEC_PATH`, `GIT_SSH`, `GIT_SSH_COMMAND`,
  `GIT_PROXY_COMMAND`, `GIT_ASKPASS`, `SSH_ASKPASS` and `GIT_ALLOW_PROTOCOL` (`pr_queue.GIT_RUN`) where none can be
  needed: a call that reaches no remote drops all of them, a fetch from GitHub (https) keeps `GIT_EXEC_PATH` only.
  A fetch from another remote (the gate's `--remote`, the head check's origin in checkout mode) keeps them.
- The head check decodes git's output as UTF-8 whatever the job's locale, so a base config that is not UTF-8 is
  "not UTF-8 text" under any locale.
- The queue's config read names the sparse-checkout pattern only when the blob is missing from a partial clone; any
  other `cat-file` failure says git's own error.

**Upgrade:** a job that needs `GIT_ASKPASS` or `GIT_SSH_COMMAND` for the queue's or the gate's fetch from GitHub no
longer gets it: the queue and the gate log in with the run's token (`gh`), as documented.

### pr-queue and head-check: follow-ups of the git config read (#435)

- The queue's config read fetches nothing and is timed. In a partial clone (actions/checkout's sparse checkout) a
  config whose blob the sparse-checkout pattern did not check out was fetched from origin with the token the checkout
  stored, with no timeout. It is now a refusal (exit 2) that names the cause; each git call of the read has 60
  seconds.
- Every git call of the queue and the head check drops the job's `GIT_*` variables that point git at another
  repository, object store, index, config or pathspec rule (`GIT_DIR`, `GIT_WORK_TREE`, `GIT_OBJECT_DIRECTORY`,
  `GIT_ALTERNATE_OBJECT_DIRECTORIES`, `GIT_INDEX_FILE`, `GIT_NAMESPACE`, `GIT_CONFIG_PARAMETERS`, `GIT_CONFIG_COUNT`
  with its `GIT_CONFIG_KEY_*`/`GIT_CONFIG_VALUE_*`, and the like: `pr_queue.GIT_DROP`).
- A base config that is not UTF-8 is a refusal in the head check (exit 2, "not UTF-8 text", as the queue says), not
  a crash (exit 1).
- The case-twin rule also refuses a file, symlink or submodule entry that differs only in case from one of the
  config's folders (`.GitHub` next to `.github/`), and it counts Unicode NFC and NFD forms of a name as one.

**Upgrade:** a caller whose checkout leaves the config's blob out must check the config out. The reusable workflow's
sparse pattern is the config's path, which matches itself unless the name holds `[ ] * ?`, a leading `!` or `#`, a
trailing space or a backslash: rename such a config. A base that already holds a twin of a config folder is not a
lock: pull requests that leave or delete it pass.

### pr-queue: reads its config with git, never from the checkout's file (#424)

The queue now reads its config as the commit checked out at `config` holds it (HEAD): the blob, with `git ls-tree` and
`git cat-file`, never the file on disk. A checkout writes that file through `.gitattributes` (a
`working-tree-encoding=UTF-16` made it UTF-16 and stopped the queue with a `UnicodeDecodeError`; `ident` rewrote it),
and a pull request could add such an attribute without changing the config's own entry, so the head check passed it.
No checkout filter or attribute changes what the queue reads now. A config that is not UTF-8 is a refusal that says so
(exit 2), never a crash.

The head check (and the gate) also refuses a pull request that adds a name differing from the config's path only in
case (`.github/PR-QUEUE.json`, `.GitHub/pr-queue.json`): a checkout on a case-insensitive file system keeps one of the
two, and which one is up to git. A base that already holds one is not a lock: pull requests that leave or delete it
pass.

**Upgrade:** the action's `config` must be a path inside a git checkout whose HEAD commit holds the config as a regular
file (the reusable workflow's checkout does). The queue refuses to start (exit 2) when the path is not in a git
checkout, when the checked-out commit has no config (an edit or a file that is not committed is not read), or when the
config there is a symlink, a submodule entry or a folder.

### head-check: the config a pull request would merge must parse (#388)

The head check (and the gate) now reads the queue config as the merge would leave it: GitHub's merge commit in
checkout mode, `git merge-tree` of the base and the head in bare mode. A pull request is refused (exit 2, with the
reason) when that config:

- does not parse (`config.parse`: not JSON, empty, an unknown or bad field, a key written twice, a bad `review` block
  in any mode), or is in `enforce` with a rule order `enforce` refuses;
- is deleted, or is no longer a regular file (git mode 100644 or 100755): a folder, a symlink or a submodule entry
  is refused, even one whose object is the config's own (the queue reads only a regular file's config);
- is over the caps on a config a pull request writes: 32 KiB, 128 characters per glob in `review.rules[].paths`, and
  2,048 glob characters in all;
- cannot be read or checked at all (any error is a refusal that names it, never a crash or a pass).

Merged, any of these would leave a base config that refuses every pull request after it, the fix too. A pull request
that leaves the config exactly as the base has it is not read again, so the caps never apply to a base config that is
already bigger. In bare mode, a head that does not merge cleanly into the base is refused too (GitHub cannot merge it
either), and so is a `git merge-tree` that takes over 300 seconds.

This narrows the race of two pull requests that each leave a good config but merge into a bad one; it does not close
it. The check judges a head on the base as of its run, and no trigger runs it again when new commits land on the base
(`edited` fires on a retarget or a title or body edit), so a pass from before the first merge still stands for the
second. Only the ruleset's "Require branches to be up to date before merging" closes it: the second pull request is
then behind, and the update that brings it up to date is a new head, judged on the merged config.

**Upgrade:**

- To delete the queue config, first take its checks out of the branch ruleset; a pull request that deletes it is
  refused.
- A config change that needs more than the caps has to be pushed by an owner.
- After an owner fixes a refused base config by hand, open pull requests no longer need to merge the base first: a
  new run judges them on what they would merge. In checkout mode that takes a new event (a push, a reopen, a title or
  body edit): a re-run of the old `pull_request` job checks out the old merge commit again, which holds the old base.
- A base whose config is a symlink or a submodule entry (only an owner's push can leave one now) refuses every pull
  request. A pull request that makes it a regular file again passes (judged as a new config) only when the link's text
  is itself a config that parses. A real symlink's text is a path and a submodule entry names a commit, so the check
  refuses the fix too ("not JSON"), and an owner has to push the fix by hand, as for any base config that does not
  parse.

### head-check gate: reviews and inline review comments run the gate again (#375, #392, #379)

A new example workflow, `review-trigger.example.yml` ("Head gate review trigger"), runs on reviews and inline review
comments and does nothing but finish. The gate caller runs on `workflow_run` of it and judges the head of the open pull
request whose head branch, head repository and head sha are the run's (the number comes from the API, never from the
run). Two open pull requests on one head (one branch into two bases) are judged once, in the job for the lowest number.
Before this, a review left the check showing the older answer until the next push or issue comment. First in `ci/v15`.
After `ci/v20` (#379, #651), a review run whose head fields are missing or not GitHub's form judges nothing with a
warning (before: a plain log line); a head branch git allows but the gate does not look up (an invisible or joining
character, or longer than 255) keeps the plain line, and "no open pull request has the head" says how many GitHub listed
on that branch.

**Upgrade:** nothing breaks if you only move the pin: a caller without the new trigger never runs on `workflow_run`.
To use it, in one pull request on the default branch, after the pin is at `ci/v15` or later: copy
`review-trigger.example.yml` to `.github/workflows/` unchanged, and add the caller's `workflow_run` trigger, its `if`
(with the event clause) and its `concurrency` from `gate-caller.example.yml`. The pin moves first or in that pull
request: a caller with `workflow_run` pinned to an older release (`ci/v14` or before) is refused on that event (its
guard job fails, nothing is posted). Then do the live check once ([head-check README](actions/head-check/README.md),
setup step 8): post a review on one open pull request and check that the gate ran for it. The GitHub fields the lookup
uses are tested only against fakes.

### pr-queue and head-check: stricter rule order in `review` (#378)

A rule of the same weight above another rule that lacks one of its lenses is now a rule-order problem, and only the
match-every globs of the catch-all rule are exempt (its explicit globs, such as `src/**` in `["src/**", "**"]`, are
compared like any other rule's). Shadow warns, `enforce` refuses.

**Upgrade:** a base config already in `enforce` with such an order is refused after the move, and so is every pull
request, the one that fixes it too. Before you move the pin, check the config with the new release (or switch it to
`shadow`); otherwise an owner has to push the fix by hand.
