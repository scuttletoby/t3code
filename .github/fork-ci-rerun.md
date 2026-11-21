# Fork CI re-run: one automatic re-run for infrastructure failures only

A failed `Fork tests` run gets **one** automatic "re-run failed jobs" (GitHub's
own), only when every failed job shows a known infrastructure sign that
GitHub's own runner message and the job's own step record agree on. Never for
a test failure, never for a person's cancel, never twice: a repeat red is left
red, and it goes to an issue and its fix first. Every re-run is also a comment
on one tracking issue, so a machine that keeps stalling is never hidden.

| Part                                                | What it does                                                                                                                                                                                                                                                                                                                                                                            |
| --------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/scripts/fork-ci-pressure.py`               | Two steps in each app job of `fork-tests.yml` (the routable jobs of `fork-fleet.json`): `start` after the checkout, `end` last (`if: always()`), each with a 1-minute timeout. Facts only: it never fails (it catches every error). Each runs under `timeout` with the `changes` job's `probe_seconds` (a stalled machine can hang it), then warns and goes on, so the tests still run. |
| `.github/scripts/fork-ci-rerun.py`                  | The decision, with every rule in one place, the tracking issue, and the re-run. Uses `fork-fleet.py`'s trust rule.                                                                                                                                                                                                                                                                      |
| `.github/fork-ci-rerun.json`                        | Every fact and number the decision uses: the trust config (`fork-fleet.json`), workflow, followers, windows, the hosted runner group, the probe's two steps and its time limit, the note's time slack, GitHub's annotation cap, sign patterns, thresholds, the fleet-mode switch, the tracking issue.                                                                                   |
| `.github/workflows/fork-ci-rerun.yml`               | Runs the decision every 10 minutes (minutes 5, 15, ...), GitHub-hosted, `actions: write`, `checks: read`, `issues: write`. `workflow_dispatch` with `run_id` checks one run, in a concurrency group of its own.                                                                                                                                                                         |
| `fork-ci-rerun.test.py`, `fork-ci-pressure.test.py` | Each sign, each refusal, each threshold at its edge, the state cases below, the paging, the wiring. Each rule line is pinned: a mutation sweep of the rule functions (each condition forced true and false, each comparison, `and`/`or`, `not`, `fullmatch` and sign changed) turns a test red for every mutant that changes what the code does.                                        |

## The numbers in the job log

The `end` step prints one line in every app job, passed or not:

```
fork-ci-pressure: job failure; 612 s since start; memory stall full 14.2% some 30.1% of the job;
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
annotation (`fork-ci-pressure`), which the decision reads from GitHub after the
run. `null` (or `None` in the line) is a number that could not be read: no
start sample, a reboot in between, a file the runner cannot see.

## The rules (fork-ci-rerun.py)

1. Only a trusted run, by the fleet's own rule (`fork-fleet.py` `run_refused`
   with `fork-fleet.json`): this repository, a trusted workflow, started and
   triggered by a trusted actor, a push to a trusted branch not made by the
   upstream sync, or a pull request a trusted actor opened from a branch of
   this repository. Never looser than what may run on the fleet.
2. Never twice: if any attempt of the run was triggered by someone else (the
   re-run is triggered by `github-actions`), the run is left red.
3. Each failed job (failure, cancelled, timed out) except the followers
   (`fork-ci-result`, which needs every job) must show a sign. **Text is never
   enough**: a step can print any annotation (`::error::` with GitHub's own
   words, `::notice title=fork-ci-pressure::`), with exactly the fields the
   runner's own have. So a message counts only in the runner's form (failure
   level, no title, on `.github`), only as a whole message (`re.fullmatch` of a
   `signs` pattern), and only when the job's own record agrees:
   - **lost**: GitHub says the runner lost communication with the server, or
     received a shutdown signal; the job ended `failure` on that runner, none
     of its steps failed, no time limit was hit, and the probe's end step never
     completed (the runner died before it). No note needed.
   - **memory full**: the job ended `failure` on a runner that is not
     GitHub-hosted (`hosted_runner_groups`), and its own note shows the job's
     own cgroup (the note's first: the runner's unit, one job at a time)
     stalled (full) at least `box_full_share_percent` of the job, a box with
     a memory limit (that cgroup or an ancestor) stalled as much, AND an OOM
     kill in the job's **own** cgroup, or `memory.max` hits or `memory.high`
     throttling in that box. An ancestor's OOM count is never read:
     `memory.events` counts a whole subtree, so a slice several jobs share
     counts the other jobs' kills too. One event without the stall, a
     machine-wide OOM kill, or a job whose own cgroup is the root (`/`) is
     no sign.
   - **limit while stalled**: GitHub says the job or a step ran past its time
     limit; its minutes are that job's (or step's) own `timeout-minutes` in the
     workflow file at the run's commit (read through the contents API; a step
     can print GitHub's words with any number, so the message's number alone
     never counts, and a job or step the reader cannot match stays red); the
     job (or that step) really ran that long (less
     `limit_slack_seconds`); no other step failed; and the note shows the
     machine or a box stalled (full) at least `full_share_percent` of the job,
     or a 5-minute full average at least `full_avg300_percent` at the end.
4. The note counts only when the probe's end step completed (so the job wrote
   its own), the job has fewer annotations than GitHub's cap
   (`annotation_cap`: GitHub keeps only the first 50 of a job, and the end
   step's note comes last, so at the cap the real one may be gone), the end
   step took less than the probe's limit (`probe_limit_seconds`) and no
   annotation starts with `fork-ci-pressure:` (the probe's own warnings: a
   probe stopped at its limit, or one that raised, ends its step green with
   only that warning and no note of its own), it is the job's only one, a
   notice on `.github` with the `fork-ci-pressure v1 ` header and the right
   shape, and its `seconds` fit the time between the two probe steps by the
   runner's own step record (`note_slack_seconds` for whole-second times).
   A note printed well before the end step cannot know when the end step
   will run, so it does not fit. A note printed by the step just before the
   end step can fit (that step knows the start time): only trust stops that
   one (rule 1, and rule 2: once).
5. One failed job without a sign leaves the run red: a test failure, a job
   with no note, a garbled one, two notes. GitHub's "The run was canceled by
   @..." on a failed job leaves it red too: a person's cancel stands, whatever
   the memory was doing. Fail closed.
6. The branch must still be at the run's commit (a newer push tests it anyway).
7. With `CI_RUNNERS=fleet` (`skip_when`), a run that qualifies is reported, once
   (one comment, one error annotation), and not re-run: a re-run of failed jobs
   keeps its fleet labels and would wait for fleet runners that refuse the
   bot's attempt (`fork-fleet.md`). Re-run it by hand.

## Visible and counted

- **The tracking issue** (`notice` in the config): the first re-run opens one
  issue, by `github-actions`, with that title; every later re-run (and every
  fleet-mode report) is one comment on it: the run's link, event, branch and
  commit, each job's sign, GitHub's message and the pressure numbers. Never one
  issue per re-run. The issue's body starts with a marker line
  (`<!-- fork-ci-rerun tracking issue -->`): only an open issue by
  `github-actions` whose body starts with it is used, whatever its title (a
  person's issue never is). Closing it is fine (the next re-run opens a new
  one). A pass looks for it again just before opening one. A comment carries a
  marker line (`<!-- fork-ci-rerun run=N attempt=A
  what=rerun|report -->`); the comment is written **before** the request.
- Each re-run is also a **warning annotation** on the `Fork CI re-run` run.
- Each pass writes a **job summary**: a table of every red run it looked at and
  what was done, and the count of automatic re-runs of the last 24 hours, read
  from the issue's markers (two to a few API calls a pass, however many runs).

## State

Nothing is kept between passes: each one reads GitHub (the runs and the issue).

| Case                                                      | What happens                                                                                                                                                                                                                                                                                                                                             |
| --------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| A pass runs again on a run it re-ran                      | The run is queued or running (skipped), or ended again with an attempt by `github-actions`: left red. No second comment.                                                                                                                                                                                                                                 |
| Two passes (or this and the fleet sweep) race             | The run is read again just before the request. If GitHub refuses the second request and the run has a new attempt, that is fine.                                                                                                                                                                                                                         |
| Two passes open the issue at once                         | Each reads the open issues after opening its own; the lowest number wins and the other closes its own as a duplicate.                                                                                                                                                                                                                                    |
| A pass stops after opening the issue (reply lost)         | No comment, no re-run: the pass ends red. The next pass finds the open issue by its marker and uses it; no second issue.                                                                                                                                                                                                                                 |
| Two tracking issues are open (a pass stopped half-way)    | The next pass, whatever it finds (every pass reads the issue for its 24 h count at its end, a pass with a run to record before it), keeps the lowest and closes the others: closed first, then one "Duplicate of #N." comment. If a close fails, the lowest is still the one used, the pass warns and posts no comment, and each later pass tries again. |
| A closed duplicate held re-run comments                   | Its markers drop out of the 24 h count (only open issues are read), so the count can read one low until the window passes. The count only informs: never-twice comes from the run's own attempts, and the comments stay on the closed issue.                                                                                                             |
| A pass stops before its request reached GitHub            | The comment is there, the run unchanged; the next pass (within `catch_up_minutes`) sends the request once and adds no second comment.                                                                                                                                                                                                                    |
| A pass stops after GitHub took the request (reply lost)   | The comment, written first, says why. The new attempt exists: the next passes see it queued or running, or later red with an attempt by `github-actions`. Never twice.                                                                                                                                                                                   |
| The comment cannot be written                             | No re-run (it would be hidden): a warning, the pass ends red, the next pass tries both.                                                                                                                                                                                                                                                                  |
| The tracking issue cannot be read                         | No re-run (its record cannot be checked) and no count: the pass ends red and says so; the next pass tries again.                                                                                                                                                                                                                                         |
| A dispatch with `run_id` while a scheduled pass waits     | Its own concurrency group: it is never replaced by a scheduled pass.                                                                                                                                                                                                                                                                                     |
| An old run, from before the probe steps                   | No note: only a lost runner can qualify. A time limit or a failure without the note is left red.                                                                                                                                                                                                                                                         |
| A run that ended more than `catch_up_minutes` ago         | Not looked at (except with `run_id`). Runs are listed by creation over `list_hours`, every page read; a listing past 10 pages ends the pass red, nothing re-run.                                                                                                                                                                                         |
| A person re-runs a run created more than `list_hours` ago | Not looked at: GitHub lists runs only by creation time, and a re-run keeps its run's. Reading further back every 10 minutes costs API calls for a rare case; the person who re-ran it is watching it. Dispatch `Fork CI re-run` with its `run_id` to check it.                                                                                           |
| GitHub errors on one run                                  | A warning; the other runs are still handled, and the pass ends red so the error is seen. The next pass tries again.                                                                                                                                                                                                                                      |
| No tracking issue, or it was closed                       | The count reads 0 from a new window; the next re-run opens a new one.                                                                                                                                                                                                                                                                                    |

## Overlap with the fleet sweep

`fork-fleet.yml`'s sweep also re-runs (the whole run, GitHub-hosted) a run
whose fleet runner was lost, in `auto` mode. Both stay at one re-run per run:
each leaves alone a run with an attempt by `github-actions`, and GitHub refuses
a re-run while one is going. They are offset by 5 minutes. The fleet sweep's
own re-runs are not comments on the tracking issue (its own job summary shows
them); that is `fork-fleet.py`'s to change.

## Limits

- No live proof before merge: scheduled workflows run from the default branch.
  After merge, dispatch `Fork CI re-run` once and read its summary. Then check,
  on a real red run, what the jobs API says (a) for the end step after a job
  time limit (does `if: always()` still run it? if not, `limit` never
  qualifies, fail closed) and (b) for a `continue-on-error` step that failed;
  and on a real lost runner, its steps' state.
- A note is the job's own words: a trusted run's code could still print one
  and keep the real end step from writing its own. Two notes are refused; a
  note without its end step's completion, after an end step that ran to the
  probe's limit, or beside a probe warning is not read; and its seconds must
  fit the probe steps' own times, which stops a note printed well before the
  end step. A step that runs just before the end step knows the start time
  and can print a note that fits, then hide the real one (a garbled start
  sample, a fake `python3` first on the path). Only trust stops that: only
  trusted runs are read, and each is re-run once at most, on the issue.
- An OOM kill by a user-space killer (earlyoom, systemd-oomd) is not in
  `memory.events` or `oom_kill`: it shows only as a stall, if at all.
- GitHub keeps at most 10 notices per step and 50 annotations per job: on a
  job with very many annotations the note can be dropped. A job with 50
  annotations (`annotation_cap`) is never read for a note, and stays red
  (fail closed).
- A probe step that hangs (a stalled machine) is stopped after the
  `changes` job's `probe_seconds` (`timeout -k 5 "${{ ... }}"`), with a
  warning, `fork-ci-pressure: start stopped`, and the job goes on. That
  output is the workflow's one copy of the number (a workflow-level `env:`
  is not allowed in `fork-tests.yml`), and `probe_limit_seconds` in the
  config must equal it (tested). A start step stopped that way leaves no
  start sample, so the note has no `seconds` and the job can only qualify as
  `lost`. An end step stopped that way leaves no note, and its job's note is
  never read (rule 4). The 1-minute `timeout-minutes` stays as a backstop.
- Text quoted into the public tracking issue (annotation messages, note
  paths) is made one line with no `@` mention and no HTML, and only a
  comment's first line is read as its marker.
- A job a step's own time limit failed counts as `limit` only with a stall; a
  slow test on a calm machine stays red.
