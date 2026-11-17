# ci-demand

Not a composite action: a read-only command that says how much CI a repository ran, per UTC day. Use it to record
a baseline before a CI change and to measure again after it.

```sh
python3 -I ci_demand.py --repo OWNER/REPO --days 7 --cache "$XDG_CACHE_HOME/ci-demand"
python3 -I ci_demand.py --config .github/fleet.json --since 2026-11-01 --until 2026-11-07 --cache DIR --json
```

It needs `gh`, logged in with read access to the repository's Actions. Outside a workflow, run it pinned like any
fork-ci file: `pinned.py --config <pin config> run ci-demand ci_demand.py <args>` ([pinned](../pinned/README.md)).

## What it prints

For each day (a run belongs to the UTC day it was created), then a `total` row:

| Number                   | How it is counted                                                                                            |
| ------------------------ | ------------------------------------------------------------------------------------------------------------ |
| CI hours                 | the sum of `completed_at - started_at` of every job of every attempt; a cancelled job counts the time it ran |
| runs per workflow        | runs by workflow file (`ci.yml`), not by run name                                                            |
| cancelled / failed share | of the day's completed runs (skipped runs included); failed = `failure`, `timed_out`, `startup_failure`      |
| queue wait               | job `created_at -> started_at`: n, p50, p90 (nearest rank), max, per runner label set (`self-hosted,build`)  |
| never reached a runner   | jobs with no runner: cancelled while queued (not in the wait numbers), or still queued now                   |
| runs without jobs        | runs whose jobs were not read because the run stopped early (see below)                                      |

Skipped jobs count nowhere; skipped runs count as runs but cost no request. `--json` prints the same numbers as one
object (`days.<day>`, `days.total`), plus `incomplete`, `run_list_complete` and `cache_write_failed` (see below).

Two choices to keep in mind when comparing before and after:

- **Re-runs count on the day the run was created.** A re-run days later adds its hours to that first day, and a
  re-run of a run created before `--since` is not counted at all.
- **Queue wait includes approval time.** A job that waits for an environment approval (a protection rule) counts that
  time as queue wait, not only the time it waits for a runner.

## Inputs

| Input               | Meaning                                                                                                |
| ------------------- | ------------------------------------------------------------------------------------------------------ |
| `--repo OWNER/REPO` | the repository; or                                                                                     |
| `--config FILE`     | a UTF-8 JSON file of the project with `{"ciDemand": {"repo": "OWNER/REPO"}}` (other keys are ignored)  |
| `--days N`          | the N full UTC days before today; or `--since DAY --until DAY` (both included)                         |
| `--cache DIR`       | required: the caller's folder for saved pages (made 0700 where the OS has modes)                       |
| `--max-requests N`  | stop after N requests (default 2000)                                                                   |
| `--reserve N`       | stop once the rate limit has fewer than N left, so other tools keep working (default 20% of the limit) |

## What it guarantees

- **Read-only.** Every request is `gh api -X GET --include <endpoint>`: no field, body or method that could write.
- **Rate limit.** One runs-list query per day (a day over the API's 1000-run cap is split in halves), then one jobs
  page per run that was not skipped. It stops at `--max-requests` or `--reserve`. A 403/429 waits `Retry-After`,
  the reset time (when it is at most 2 minutes away) or 60 s for a secondary limit; a longer wait stops the run.
  The default reserve is 20% of the token's hourly limit: 1000 of a user's 5000, 200 of a workflow `GITHUB_TOKEN`'s
  1000, so it also runs inside a workflow.
  Server errors and network trouble are retried three times with back-off.
- **Stops half-way, runs again.** A stop prints what it has, says `INCOMPLETE` (and whether the run list was
  finished) and exits 3. Run the same command again later: every page it saved is read from `--cache`, so no page is
  asked for twice. If the cache could not be written (a read-only folder, a full disk), the result says so (in the
  first line, and `cache_write_failed` in `--json`) and a stop does not say to run again: the pages that failed were not saved, so it
  would ask for them again. Fix the cache folder first.
- **Cache.** A jobs page is saved when the run and all its jobs are completed, keyed by run id and attempt (a re-run
  is a new attempt, so a new page). A runs-list page is saved when every run in it is completed and the day ended
  over an hour ago, and is reused for 12 hours (a later re-run changes it). Writes are a `.part-` file then a rename,
  so a run killed mid-write leaves no half page under a name that is read. A file that is broken (bad JSON, nested
  too deep, a bad date, a body that is not an object or has a field of the wrong type, such as `total_count` as
  text), saved for another page or in another cache format (an older or newer version of this tool) is fetched again,
  never a crash. A write that fails (a full disk, a read-only folder, also an old `.part-` file that cannot be
  removed at start) is reported once and the run goes on with the pages it can read (see above). `.part-` files older
  than an hour are removed.
- **Any OS.** It needs Python 3 and `gh`. Where the OS has user ids (Linux, macOS), a cache folder another user owns
  is refused and the folder is made 0700, also when it existed before; on Windows these checks are skipped. So on
  Linux or macOS the cache folder must allow private permissions: one on a filesystem where chmod has no effect (FAT
  or exFAT, WSL's `/mnt/c`, some network mounts) is refused (exit 2). Pick a folder on a normal disk, such as
  `$XDG_CACHE_HOME/ci-demand` or `~/.cache/ci-demand`. `gh` output and the config file are read as UTF-8 whatever the
  locale (a BOM is fine in the config file).
- Exit codes: 0 complete, 2 bad arguments (also a config file that is not JSON or not of the shape above) or an API
  error (404, no access, network down after retries, an answer with a field of the wrong type), 3 incomplete.
  A bad file or answer is one line on stderr, not a traceback.

## General vs project

General: the command, its counting rules and its cache. Project: only the repository (an argument, or `ciDemand.repo`
in the project's own config file) and where the caller keeps the cache.
