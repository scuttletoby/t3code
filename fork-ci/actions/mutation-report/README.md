# mutation-report

A composite action for the last step of a mutation testing job: it **reports each module's kill rate beside its
baseline and its last runs**, in the job summary and in one rolling GitHub issue with a trend table, and keeps one red
issue open while a module has no score.

- **A drop is a number, not a failure.** A kill rate below its baseline is marked as a drop. The step fails only when
  a module has no score (it did not finish, failed, or has fewer than half its baseline's planted bugs), the baseline
  is missing, unreadable or names a module that is no longer a target, the config is bad, or GitHub could not be read
  or written. A run that measured nothing stays loud.
- **The trend survives the issue.** The trend (the last 26 runs) is kept in the rolling issue's body. Every issue with
  the configured title, open or closed, is read and their trends are merged, each run once (by its run URL). The
  oldest open one is written; with none open, the newest closed one is reopened, so closing the issue never restarts
  the trend. A second open copy (a search that lagged, an older version's copy) is closed with a comment, its trend
  carried over. Each write is one call, so a run stopped between two of them leaves state the next run finishes.
- **The red issue.** A red run opens, or comments on, one issue titled `red_title` (a comment notifies; an edited
  rolling issue notifies nobody); the first green run closes it.
- **The baseline changes only by a pull request.** `suggest` writes this run's numbers in the baseline's format.
- A module without a result this run keeps its last section, marked as not refreshed.

It needs Python 3 (standard library only) and `gh`, both on GitHub's runners.

## Usage

Pin a release commit by its full SHA, never a commit of main ([why](../../README.md#pinning)):

```yaml
permissions:
  issues: write
steps:
  # ... the mutation steps write their results into results/
  - if: always()
    uses: scuttletoby/t3code/fork-ci/actions/mutation-report@<release sha> # ci/vN
    with:
      results: results
      baseline: .github/mutation-baseline.json
      expect: |
        tools/mutation/targets.json
      config: .github/mutation-report.json # optional
      suggest: results/baseline-suggested.json
      trend: results/trend.json
```

| Input      | Default        | Meaning                                                                    |
| ---------- | -------------- | -------------------------------------------------------------------------- |
| `results`  | (required)     | results JSON files or folders, one per line; a missing one means no result |
| `baseline` | (required)     | the baseline file                                                          |
| `expect`   | (required)     | targets files whose modules must have a result, one per line               |
| `config`   | (empty)        | the config file; empty: the defaults                                       |
| `issue`    | `true`         | `false`: the summary and the files only, no issue read or written          |
| `suggest`  | (empty)        | write this run's numbers as a baseline file here                           |
| `out`      | (empty)        | also write the report here                                                 |
| `trend`    | (empty)        | write the kept runs here as JSON (for the run's artifact)                  |
| `token`    | `github.token` | a token with issues write                                                  |

No outputs.

## Files

- **Results** (`*.json` in a folder, or a file): `{"modules": [ ... ], "skipped_tests": {"<test file>": "<why>"}}`.
  A module: `module`, `file`, `kill_rate` (percent), `mutants`, `killed`, `timeout`, `survived`, `no_cover`,
  `survivors` (`[{file, line, what, why, code}]`), optional `blocked_calls`; or `module` and `error` (with optional
  `finished_parts`) when it did not finish. A file without a `modules` list (a suggested baseline) is skipped.
- **Targets**: `{"modules": {"<module>": {...}}}`: the module names that must have a result.
- **Baseline**: `{"modules": {"<module>": {"kill_rate": 61.5, "mutants": 600}}}`.
- **Config** (optional; each key has a default):

```json
{
  "title": "Mutation testing: report",
  "red_title": "mutation run red",
  "labels": [],
  "red_labels": []
}
```

A key this release does not know (a newer release's) is a bad config: the report and files are still written, no
issue is read or written, and the step fails. Labels must exist in the repository.

**Moving from a project's own copy:** set `title` and `red_title` to the copy's titles, so the action finds and
continues the copy's issue and trend; then delete the copy.

## Tests

```sh
python3 fork-ci/actions/mutation-report/mutation_report.test.py
```
