# fleet-watch

A composite action for a project that routes jobs with [fleet-route](../fleet-route/README.md). A dispatcher pool is a
machine that comes and goes (a desktop): its runner can stop half-way, lose its network or be killed for memory, and
a job can wait for it longer than the run should. fleet-watch makes that cost one re-run on the always-on runners, not
a red run a person has to re-run:

| Verb      | When                                  | What it does                                                                                                                                                                                 |
| --------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `watch`   | a run of a trusted workflow completed | re-runs the run's failed jobs once when a job on a dispatcher pool was cut off and no job failed by itself                                                                                   |
| `sweep`   | every few minutes                     | cancels a run whose job has waited too long for a dispatcher pool; does `watch` for a red run of the last hour that no watch re-ran (when its jobs alone decide); lists red re-runs (`reds`) |
| `pending` | after `watch`, before a red report    | `pending=true` only when a newer attempt of the run is there (the watch's re-run, or a person's): skip the report, that attempt decides                                                      |

A job on a dispatcher pool counts as **cut off** when it:

- waited `stuck_seconds` (in fleet mode, on the pool that waits: `fleet_stuck_seconds`) and never got a runner;
- had a runner and ended `failure` with no failed step (the runner stopped, or lost its network);
- failed only in `Set up job` or `Set up runner` (the runner's set-up, or its gate refused the job);
- failed in a step whose log has an error line `Process completed with exit code 137` (killed, most often for
  memory) or one matching the config's `watch.errors`.

Any other red job (a failed step of its own, on any runner, also in a run the sweep cancelled) means **the change
failed**: that red stands and nothing is re-run. So does a job that hit its own `timeout-minutes` (`timed_out`), and a
job cancelled while it ran (by a person, or a newer push).

In fleet mode too, a cut-off job is re-run once, on fleet-route's `fallback` (never a dispatcher pool): with
`"hosted": false` every run is in fleet mode. The router's own `sweep` re-runs nothing in fleet mode; fleet-watch
replaces it.

Known limits, each costing at most one re-run of a trusted run, onto `fallback`:

- a run a person cancelled after a job had waited past its bound counts as cut off (GitHub's record does not say who
  cancelled a run; fleet-route's `sweep` does the same);
- a step that prints an error line matching `exit code 137` or `watch.errors` itself counts as killed: only runs of
  trusted actors and workflows are read, so only their own code can do that.

## Guarantees

- **Never twice.** The re-run is started with the job's `GITHUB_TOKEN`, so its triggering actor is the
  `github-actions` bot, which is never a trusted actor: fleet-watch leaves that attempt alone whatever it does, and
  fleet-route sends its jobs to `fallback` (they never wait for a dispatcher pool again). fleet-watch also never
  re-runs attempt 3 or later. Do not give it a person's token: its re-runs would count as that person's. Just before
  each re-run or cancel it reads the run again, and goes on only when it is the attempt it planned for, in the same
  state, and still trusted (a re-run another watch started meanwhile is left alone).
- **Only what the config trusts.** A run is touched only when it is of the config's `repository`, one of
  `trusted.workflows`, a push to `trusted.push_branches` or a pull request from this repository, and both its actor
  and its triggering actor are in `trusted.actors`.
- **No state of its own.** GitHub's record of each run (its latest attempt, who started it, its jobs) is the only
  state, so it may run again, stop half-way, or start on an empty repository: a second watch of one run finds the
  re-run going (GitHub refuses a second one) and does nothing; a watch that never ran is done by the next sweep; a
  run that ended more than 24 hours ago, or whose branch has a newer run of another commit, is never re-run.
- **Bounded.** At most 200 GitHub calls per step (the rest waits for the next sweep; each sweep starts at another
  workflow, so one busy workflow cannot use them all every time), one page of runs per workflow and state, 300 jobs
  per attempt (a run with more is unsure: its red stands), and a job's log only when its red would otherwise stand,
  only in `watch` (the sweep's catch-up leaves a run whose logs would decide to its watch, so it reads no log).
- **Unsure means red.** `pending` answers `true` only when GitHub shows a newer attempt; a re-run GitHub refused (or
  not started yet), an error or a missing token is `false` or an empty output. Test it with `== 'true'`.
- **Settings are never guessed.** `watch` and `sweep` fail when `vars` is missing or is not a JSON object, or the
  setting is not a mode; an unset setting is `auto`, as for fleet-route.

## Usage

Copy [`fleet-watch-caller.example.yml`](fleet-watch-caller.example.yml) to `.github/workflows/` and fill it in. The
token needs `actions: write` only (and `contents: read` for the checkout of the config). Use it instead of the
router's own `sweep` (`.github/scripts/fork-fleet.py sweep`), not beside it.

A red-run report (for example one that files an issue when the default branch goes red) goes in the same job, after
the `watch` step, as in the example: `pending` there sees the re-run the watch just started. A report in another
workflow may ask before the watch has re-run: it then reports a red that a re-run may still turn green (the safe
side).

The re-run's own red is the next attempt's to report. GitHub's docs do not say whether the completion of a run
re-run with `GITHUB_TOKEN` fires `workflow_run` (events that `GITHUB_TOKEN` triggers start no workflow, apart from
`workflow_dispatch` and `repository_dispatch`:
[triggering a workflow from a workflow](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/triggering-a-workflow#triggering-a-workflow-from-a-workflow);
the only re-run note for `workflow_run` is that `requested` does not occur:
[`workflow_run`](https://docs.github.com/en/actions/writing-workflows/choosing-when-your-workflow-runs/events-that-trigger-workflows#workflow_run)).
So the sweep lists those red attempts as `reds` for an hour, and the example reports them; when `workflow_run` does
fire, both report one attempt: make the report do each run id and attempt once (for example, look for its earlier
report first).

## Config

The fleet config of fleet-route, with an optional `watch` section:

```json
"watch": {
  "followers": ["fleet_watch", "required"],
  "errors": ["No space left on device"]
}
```

| Field       | Default | Meaning                                                                                                                                               |
| ----------- | ------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `followers` | none    | job names whose red only reports other jobs (a summary job, the job that cancels a run that waits too long); a matrix job `name (…)` matches its name |
| `errors`    | none    | regular expressions for a job log's error lines that mean the machine broke the job, not the change                                                   |

List every job that fails because another one did, or its red counts as the change's and nothing is re-run.
