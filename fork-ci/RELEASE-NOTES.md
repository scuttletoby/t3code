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

### head-check: the config a pull request would merge must parse (#388)

The head check (and the gate) now reads the queue config as the merge would leave it: GitHub's merge commit in
checkout mode, `git merge-tree` of the base and the head in bare mode. A pull request is refused (exit 2, with the
reason) when that config:

- does not parse (`config.parse`: not JSON, empty, an unknown or bad field, a key written twice, a bad `review` block
  in any mode), or is in `enforce` with a rule order `enforce` refuses;
- is deleted, or is no longer a file;
- is over the caps on a config a pull request writes: 32 KiB, 128 characters per glob in `review.rules[].paths`, and
  2,048 glob characters in all;
- cannot be read or checked at all (any error is a refusal that names it, never a crash or a pass).

Merged, any of these would leave a base config that refuses every pull request after it, the fix too. A pull request
that leaves the config exactly as the base has it is not read again, so the caps never apply to a base config that is
already bigger. In bare mode, a head that does not merge cleanly into the base is refused too (GitHub cannot merge it
either).

**Upgrade:**

- To delete the queue config, first take its checks out of the branch ruleset; a pull request that deletes it is
  refused.
- A config change that needs more than the caps has to be pushed by an owner.
- After an owner fixes a refused base config by hand, open pull requests no longer need to merge the base first: they
  are judged on what they would merge.

### pr-queue and head-check: stricter rule order in `review` (#378)

A rule of the same weight above another rule that lacks one of its lenses is now a rule-order problem, and only the
match-every globs of the catch-all rule are exempt (its explicit globs, such as `src/**` in `["src/**", "**"]`, are
compared like any other rule's). Shadow warns, `enforce` refuses.

**Upgrade:** a base config already in `enforce` with such an order is refused after the move, and so is every pull
request, the one that fixes it too. Before you move the pin, check the config with the new release (or switch it to
`shadow`); otherwise an owner has to push the fix by hand.
