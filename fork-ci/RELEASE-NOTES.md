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

### pr-queue and head-check: stricter rule order in `review` (#378)

A rule of the same weight above another rule that lacks one of its lenses is now a rule-order problem, and only the
match-every globs of the catch-all rule are exempt (its explicit globs, such as `src/**` in `["src/**", "**"]`, are
compared like any other rule's). Shadow warns, `enforce` refuses.

**Upgrade:** a base config already in `enforce` with such an order is refused after the move, and so is every pull
request, the one that fixes it too. Before you move the pin, check the config with the new release (or switch it to
`shadow`); otherwise an owner has to push the fix by hand.
