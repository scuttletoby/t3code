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
- A test file the table doesn't have counts as the table's median. The step summary and `--plan`
  say how many there are.
- If the table is missing, not JSON, empty or not in seconds, each test file counts as 1 (the old
  split). The step summary says so.

To see the split without running anything:

    node .github/scripts/fork-test-shards.cjs --plan 2

## Refresh

When the step summary reports many test files that aren't in the table, or the two jobs' times drift
apart, refresh the table from the `Test N` logs of the last green pushes to main. Save the logs first
(this needs `gh` with read access to the Actions logs):

    dir=$(mktemp -d)
    for run in $(gh run list -w fork-tests.yml -b main -e push -s success -L 5 --json databaseId -q '.[].databaseId'); do
      for job in $(gh api "repos/{owner}/{repo}/actions/runs/$run/jobs?per_page=100" \
          -q '.jobs[] | select(.name | test("^Test [0-9]+$")) | .id'); do
        gh api "repos/{owner}/{repo}/actions/jobs/$job/logs" --allow-escape-sequences > "$dir/$run-$job.log"
      done
    done
    node .github/scripts/fork-test-seconds.cjs --logs "$dir"

The script itself never calls `gh`: fork scripts reach `gh` only through `fork-gh-guard.sh`.

- The same logs always give the same file, written in `vp fmt`'s layout.
- A file vitest runs from outside its package's folder (`apps/desktop` includes
  `../../native/kde-snap-shot/src/*.test.ts`) is timed under its own path. The split counts it with
  that package.
- Commit the new table in a PR of its own. The table is never fork-owned (`!` in
  `.github/fork-owned-paths.txt`), so that PR runs the Test jobs with the new split.
