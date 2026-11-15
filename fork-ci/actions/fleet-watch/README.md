# fleet-watch

A composite action for a project that routes jobs with [fleet-route](../fleet-route/README.md). A dispatcher pool is a
machine that comes and goes (a desktop): its runner can stop half-way, lose its network or be killed for memory, and
a job can wait for it longer than the run should. fleet-watch makes that cost one re-run on the always-on runners, not
a red run a person has to re-run:

| Verb      | When                                     | What it does                                                                                       |
| --------- | ---------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `watch`   | a run of a trusted workflow completed    | re-runs the run's failed jobs once when a job on a dispatcher pool was cut off and no job failed by itself |
| `sweep`   | every few minutes                        | cancels a run whose job has waited too long for a dispatcher pool; does `watch` for a red run of the last hour that no watch re-ran |
| `pending` | a report of a red run, before it reports | `pending=true` only when this red attempt will be re-run (or a newer attempt runs): skip the report |

A job on a dispatcher pool counts as **cut off** when it:

- waited `stuck_seconds` (in fleet mode, on the pool that waits: `fleet_stuck_seconds`) and never got a runner;
- had a runner and ended red with no failed step (the runner stopped, or lost its network);
- failed only in `Set up job` or `Set up runner` (the runner's set-up, or its gate refused the job);
- failed in a step whose log has an error line `Process completed with exit code 137` (killed, most often for
  memory) or one matching the config's `watch.errors`.

Any other red job (a failed step of its own, on any runner) means **the change failed**: that red stands and nothing
is re-run. A job cancelled while it ran (by a person, or a newer push) stands too.

## Guarantees

- **Never twice.** The re-run is started with the job's `GITHUB_TOKEN`, so its triggering actor is the
  `github-actions` bot, which is never a trusted actor: fleet-watch leaves that attempt alone whatever it does, and
  fleet-route sends its jobs to `fallback` (they never wait for a dispatcher pool again). fleet-watch also never
  re-runs attempt 3 or later. Do not give it a person's token: its re-runs would count as that person's.
- **Only what the config trusts.** A run is touched only when it is of the config's `repository`, one of
  `trusted.workflows`, a push to `trusted.push_branches` or a pull request from this repository, and both its actor
  and its triggering actor are in `trusted.actors`.
- **No state of its own.** GitHub's record of each run (its latest attempt, who started it, its jobs) is the only
  state, so it may run again, stop half-way, or start on an empty repository: a second watch of one run finds the
  re-run going (GitHub refuses a second one) and does nothing; a watch that never ran is done by the next sweep; a
  run that ended more than 24 hours ago, or whose branch has a newer run of another commit, is never re-run.
- **Bounded.** At most 200 GitHub calls per step (the rest waits for the next sweep), one page of runs per workflow
  and state, and a job's log only when its red would otherwise stand.
- **Unsure means red.** `pending` answers `true` only when it is sure; an error, a missing token or an unreadable log
  is `false` or an empty output. Test it with `== 'true'`.

## Usage

Copy [`fleet-watch-caller.example.yml`](fleet-watch-caller.example.yml) to `.github/workflows/` and fill it in. The
token needs `actions: write` only (and `contents: read` for the checkout of the config). Use it instead of the
router's own `sweep` (`.github/scripts/fork-fleet.py sweep`), not beside it.

A red-run report (for example one that files an issue when the default branch goes red) asks first:

```yaml
      - id: pending
        uses: scuttletoby/t3code/fork-ci/actions/fleet-watch@<release sha> # ci/vN
        continue-on-error: true # a failed step leaves pending empty: report the red
        with:
          config: .github/fleet.json
          verb: pending
          run-id: ${{ github.event.workflow_run.id }}
          attempt: ${{ github.event.workflow_run.run_attempt }}
      - if: steps.pending.outputs.pending != 'true'
        run: ./report-red.sh
```

The re-run's own red is the next attempt's to report: check once that your report runs for a re-run's completion
too.

## Config

The fleet config of fleet-route, with an optional `watch` section:

```json
"watch": {
  "followers": ["fleet_watch", "required"],
  "errors": ["No space left on device"]
}
```

| Field       | Default | Meaning                                                                                                            |
| ----------- | ------- | ------------------------------------------------------------------------------------------------------------------ |
| `followers` | none    | job names whose red only reports other jobs (a summary job, the job that cancels a run that waits too long); a matrix job `name (…)` matches its name |
| `errors`    | none    | regular expressions for a job log's error lines that mean the machine broke the job, not the change               |

List every job that fails because another one did, or its red counts as the change's and nothing is re-run.
