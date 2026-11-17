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
