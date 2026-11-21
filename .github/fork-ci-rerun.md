# Fork CI re-run: one automatic re-run for infrastructure failures only

A failed `Fork tests` run gets **one** automatic "re-run failed jobs" (GitHub's
own), only when every failed job shows a known infrastructure sign. Never for a
test failure, never twice: a repeat red is left red, and it goes to an issue
and its fix first.

| Part                                                | What it does                                                                                                                                                                      |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/scripts/fork-ci-pressure.py`               | Two steps in each app job of `fork-tests.yml` (the routable jobs of `fork-fleet.json`): `start` after the checkout, `end` last (`if: always()`). Facts only, never fails the job. |
| `.github/scripts/fork-ci-rerun.py`                  | The decision, with every rule in one place, and the re-run.                                                                                                                       |
| `.github/fork-ci-rerun.json`                        | Every fact and number the decision uses: repository, workflow, trusted actors and branches, followers, sign patterns, thresholds, the fleet-mode switch.                          |
| `.github/workflows/fork-ci-rerun.yml`               | Runs the decision every 10 minutes (minutes 5, 15, ...), GitHub-hosted, `actions: write`, `checks: read`. `workflow_dispatch` with `run_id` checks one run.                       |
| `fork-ci-rerun.test.py`, `fork-ci-pressure.test.py` | Each sign, each refusal, the state cases below, the wiring.                                                                                                                       |

## The numbers in the job log

The `end` step prints one line in every app job, passed or not:

```
fork-ci pressure: job failure; 612 s since start; memory stall full 14.2% some 30.1% of the job;
full avg10/60/300 22.0/18.4/12.9%; available 310 MB, swap used 1024 MB, OOM kills 1;
cgroup /fleet.slice/fork-fleet-x.service: high None MB max None MB use 2900 MB, events high None max None oom_kill None, full 15.0%; ...
```

- **stall full / some**: the share of the job's time in which all (full) or
  some tasks of the machine waited for memory (`/proc/pressure/memory`, from
  its `total` counters between `start` and `end`).
- **avg10/60/300**: the kernel's own full-stall averages at the end.
- **OOM kills**: `/proc/vmstat` `oom_kill` since `start`.
- **cgroup**: the job's own cgroup and each ancestor with a memory limit (the
  box: a slice or unit with `MemoryHigh` or `MemoryMax`), with limit, use,
  `memory.events` (high, max, oom_kill) since `start` and its own stall share.

A job that did not succeed also leaves the same numbers as one notice
annotation (`fork-ci pressure`), which the decision reads from GitHub after the
run. `null` (or `None` in the line) is a number that could not be read: no
start sample, a reboot in between, a file the runner cannot see.

## The rules (fork-ci-rerun.py)

1. Only a trusted run: this repository, `fork-tests.yml`, started and
   triggered by a listed actor, a push to a listed branch or a pull request
   from a branch of this repository.
2. Never twice: if any attempt of the run was triggered by someone else (the
   re-run is triggered by `github-actions`), the run is left red.
3. Each failed job (failure, cancelled, timed out) except the followers
   (`fork-ci-result`, which needs every job) must show a sign:
   - **lost**: GitHub says the runner lost communication with the server, or
     received a shutdown signal. Enough on its own (no note needed: the job
     never reached its last step).
   - **memory full**: the note shows an OOM kill on the machine or in the box,
     the box hitting `memory.max`, or `memory.high` throttling while stalled
     (full) at least `high_events_with_full_share_percent` of the job.
   - **limit while stalled**: GitHub says the job or a step ran past its time
     limit, and the note shows the machine or the box stalled (full) at least
     `full_share_percent` of the job, or a 5-minute full average at least
     `full_avg300_percent` at the end.
4. One failed job without a sign leaves the run red: a test failure, a cancel
   by a person or a newer push, a job with no note or a garbled one, more than
   one note. Fail closed.
5. The branch must still be at the run's commit (a newer push tests it anyway).
6. With `CI_RUNNERS=fleet` (`skip_when`), a run that qualifies is reported with
   an error annotation and not re-run: a re-run of failed jobs keeps its fleet
   labels and would wait for fleet runners that refuse the bot's attempt
   (`fork-fleet.md`). Re-run it by hand.

## Visible and counted

- Each re-run is a **warning annotation** on the `Fork CI re-run` run, with
  the job names, the signs, GitHub's message and the numbers.
- Each pass writes a **job summary**: a table of every red run of the last hour
  and what was done (re-run, left red and why), and the count of runs whose
  latest attempt was an automatic re-run in the last 24 hours (this one's and
  the fleet sweep's). A stalled machine keeps showing up there and in the
  failed job's own log line; the re-run does not hide it.

## State

Nothing is kept between passes: each one reads GitHub.

| Case                                              | What happens                                                                                                                     |
| ------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------- |
| A pass runs again on a run it re-ran              | The run is queued or running (skipped), or ended again with an attempt by `github-actions`: left red.                            |
| Two passes (or this and the fleet sweep) race     | The run is read again just before the request. If GitHub refuses the second request and the run has a new attempt, that is fine. |
| A pass stops before its request reached GitHub    | The run is unchanged; the next pass (within `catch_up_minutes`) sends it once.                                                   |
| A pass stops after GitHub took the request        | The new attempt exists: the next passes see it queued or running, or later red with an attempt by `github-actions`. Never twice. |
| An old run, from before the probe steps           | No note: only a lost runner can qualify. A time limit or a failure without the note is left red.                                 |
| A run that ended more than `catch_up_minutes` ago | Not looked at (except with `run_id`).                                                                                            |
| GitHub errors on one run                          | A warning; the other runs are still handled, and the pass ends red so the error is seen. The next pass tries again.              |

## Overlap with the fleet sweep

`fork-fleet.yml`'s sweep also re-runs (the whole run, GitHub-hosted) a run
whose fleet runner was lost, in `auto` mode. Both stay at one re-run per run:
each leaves alone a run with an attempt by `github-actions`, and GitHub refuses
a re-run while one is going. They are offset by 5 minutes.

## Limits

- No live proof before merge: scheduled workflows run from the default branch.
  After merge, dispatch `Fork CI re-run` once and read its summary.
- An OOM kill by a user-space killer (earlyoom, systemd-oomd) is not in
  `memory.events` or `oom_kill`: it shows only as a stall, if at all.
- GitHub keeps at most 10 notices per step and 50 annotations per job: on a
  job with very many annotations the note can be dropped. The job then reads
  as "no note" and stays red (fail closed).
- A trusted run's own code could print a forged note. Only trusted runs are
  read, and two notes in one job are refused.
- A job a step's own time limit failed counts as `limit` only with a stall; a
  slow test on a calm machine stays red.
