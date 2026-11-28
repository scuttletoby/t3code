# Test shard seconds

Fork tests' `Test 1` and `Test 2` jobs split the workspace packages (all except `t3` and the web app)
with `.github/scripts/fork-test-shards.cjs`. Every merge runs CI again, so the slower of the two jobs
limits how fast PRs merge. The split is balanced by measured seconds from
`.github/fork-test-seconds.json`.

## What the table holds

- `files`: each test file's test time in seconds, as vitest prints it on the shards. It is the
  median over the green pushes to main listed in `runs`.
- `model`: how a file's test time turns into wall seconds, fitted to the jobs' measured walls.
  - `whole`: packages that run whole, 4 at a time. Their test time overlaps a lot.
  - `slice`: the vitest `--shard` slice of a large package, which runs alone.
  - Each mode has a cost per file (`perFile`) and a cost per second of test time (`perSecond`).

## How the split uses it

- A package run whole costs the sum of its files' costs.
- A large package (100 test files or more) is sliced on every shard. The script cuts the slices the
  way vitest's `--shard` does, so a slice with a slow file counts as slow.
- Whole packages go longest first onto the shard with the fewest seconds so far. Ties go by name,
  so every shard computes the same split.
- A test file the table doesn't have counts as the table's median.
- If the table is missing, not JSON, empty or not in seconds, each test file counts as 1 (the old
  split). The step summary says so.

To see the split without running anything:

    node .github/scripts/fork-test-shards.cjs --plan 2

## Refresh

When `--plan` or the jobs' times drift apart (a new slow test, a package that grew), refresh the
table from the last green pushes to main. This needs `gh` with read access to the repository's
Actions logs:

    node .github/scripts/fork-test-seconds.cjs            # the last 5 runs
    node .github/scripts/fork-test-seconds.cjs --runs 10
    node .github/scripts/fork-test-seconds.cjs --logs <dir>  # logs saved earlier: <runId>-<jobId>.log

The same logs always give the same file. Commit the new table in a PR of its own. The table is never
fork-owned (`!` in `.github/fork-owned-paths.txt`), so that PR runs the Test jobs with the new split.
