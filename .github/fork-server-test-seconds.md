# Test Server shard seconds

Fork tests' `Test Server` jobs (6 shards) run the server's test files with upstream's
`vp run test:cached`, one vitest process per file. Upstream cuts the shards with
`apps/server/src/testUtils/weightedShardSequencer.ts` and its table `shardWeights.json`. That table
lacks most fork files (about 480 of 690), and each missing file counts 0.6 s while the real median
is about 2.9 s. So one shard ran about 290 s while the others ran about 180 s.

`.github/scripts/fork-server-shards.cjs` cuts the same file list (upstream's own vitest discovery)
the same way, longest first onto the lightest shard, ties by name. It uses
`.github/fork-server-test-seconds.json` for the weights and prints one shard's files, and the Test
step passes exactly those files to `test:cached`. Upstream's files are not changed.

## What the table holds

- `files`: each file's cost in seconds, keyed relative to `apps/server` as upstream's table is. A
  file's cost is vitest's `Duration` for its process: start, import and tests. It is the median over
  the Test Server jobs of the green pushes to main listed in `runs`.
- A test file the table doesn't have yet (a new test) counts as the table's median.
- An entry for a file that is gone is ignored.
- If the table is missing, not JSON, empty, not in seconds, or has a negative or non-number time,
  the split is exactly upstream's: its table, with 0.6 s for a missing file. The job log warns why.

To see the split without running anything:

    node .github/scripts/fork-server-shards.cjs --plan 6

## Refresh

When the shards' Test step times drift apart, refresh the table from the Test Server logs of the
last green pushes to main. Save them first (`gh` with read access to the Actions logs):

    dir=$(mktemp -d)
    for run in $(gh run list -w fork-tests.yml -b main -e push -s success -L 5 --json databaseId -q '.[].databaseId'); do
      for job in $(gh api "repos/{owner}/{repo}/actions/runs/$run/jobs?per_page=100" \
          -q '.jobs[] | select(.name | startswith("Test Server")) | .id'); do
        gh api "repos/{owner}/{repo}/actions/jobs/$job/logs" --allow-escape-sequences > "$dir/$run-$job.log"
      done
    done
    node .github/scripts/fork-server-shards.cjs --refresh --logs "$dir"

The script itself never calls `gh`: fork scripts reach it only through `fork-gh-guard.sh`.
The same logs always give the same file. Commit it in a PR of its own. The table and the script are
never fork-owned (`!` in `.github/fork-owned-paths.txt`), so that PR runs Test Server with the new
split.
