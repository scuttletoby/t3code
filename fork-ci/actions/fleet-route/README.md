# fleet-route

A composite action that decides, for each routed job of a run, **which runner it runs on**: one of the project's
own self-hosted pools (its fleet) or GitHub-hosted. It reads one config file and the repository variables, calls no
API and needs no token. It wraps the router that the fork's own CI uses
([`.github/scripts/fork-fleet.py`](https://github.com/scuttletoby/t3code/blob/main/.github/scripts/fork-fleet.py) `route`; design and runbook:
[`.github/fork-fleet.md`](https://github.com/scuttletoby/t3code/blob/main/.github/fork-fleet.md); the router ships in every release, these links point at `main`).

What it guarantees:

- **A run that may hold another repository's code never gets a self-hosted runner**, in any mode and whatever the
  heartbeats say: a pull request or `pull_request_target` whose head is another repository, a `workflow_run`, and
  any event but a push, a pull request, a manual or a scheduled run. It goes to the `hosted` label. With
  `"hosted": false` there is nowhere safe for it, so the step fails red with a clear error. It is never skipped:
  GitHub counts a skipped required check as passing.
- **Only a trusted run uses a dispatcher pool.** A run of this repository that breaks the trust rule (an untrusted
  actor, workflow or branch, a bot) goes to the `hosted` label, or with `"hosted": false` to the always-on static
  pool of its kind. A job whose change gate is off goes there too: with `"hosted": false` nothing goes to
  `ubuntu-latest`.
- **Every label it puts out is one something serves**: the `hosted` label, a static pool's `runs_on`, or a
  dispatcher pool's label plus this run's own label (the pool's dispatcher makes a one-job runner for exactly that
  job). `hosted` may not name a self-hosted, pool or retired label (checked when the config is read and again at run
  time).
- **Static pools serve private repositories only** (see "Config").
- **Fails closed, visibly.** A pool whose heartbeat is old or unreadable (its dispatcher is down, or its token
  expired) counts no free slot and gets an `::error::` annotation naming the pool; its jobs go to the pool that
  waits (with `"hosted": false`) or GitHub-hosted. A missing or broken config fails the step, and the routed jobs'
  `runs-on` falls back (see "Usage").

## Usage

Pin a release commit by its full SHA with its tag as a comment, never a commit of main
([why](../../README.md#pinning)):

```yaml
jobs:
  route:
    runs-on: ubuntu-latest
    permissions: { contents: read }
    outputs:
      runners: ${{ steps.route.outputs.runners }}
      attempt: ${{ steps.route.outputs.attempt }}
      fallback: ${{ steps.route.outputs.fallback }}
    steps:
      - uses: actions/checkout@<sha>
        with:
          # the config as the base branch has it: a pull request cannot change where it runs
          ref: ${{ github.event.pull_request.base.sha || github.sha }}
          sparse-checkout: .github/fleet.json
          sparse-checkout-cone-mode: false
      - id: route
        uses: scuttletoby/t3code/fork-ci/actions/fleet-route@<release sha> # ci/vN
        with:
          config: .github/fleet.json
          vars: ${{ toJSON(vars) }}

  plugins:
    needs: route
    if: ${{ !cancelled() }} # a failed route must not skip the job: a skipped required check counts as passing
    runs-on: >-
      ${{ needs.route.result == 'success'
          && (needs.route.outputs.attempt == github.run_attempt && fromJSON(needs.route.outputs.runners).plugins
              || fromJSON(needs.route.outputs.fallback))
          || (github.event_name == 'push'
              || github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name == github.repository)
          && fromJSON('["self-hosted", "example-build"]')
          || 'ubuntu-latest' }}
```

The `runs-on` reads, in order:

1. the route's labels, when the route job passed **for this attempt**;
2. the route's `fallback`, when it passed for an earlier attempt. A re-run of failed jobs does not re-run `route`,
   and a dispatcher's runner is made for one attempt only, so a job of attempt 2 must never wait on attempt 1's
   label. `fallback` never names a dispatcher pool: for a trusted run, the always-on static pool of its kind (if
   there is one), else the `hosted` label; for any other run, where its jobs went;
3. when the route job failed: the always-on pool (here `example-build`), but **only for a `push` event or a
   `pull_request` event whose head is this repository**. `pull_request_target`, `workflow_run` and every other
   event may run another repository's code, so they never get a self-hosted label here;
4. otherwise `ubuntu-latest`.

`fleet_route.test.py` runs this exact expression (copied from this README) for each of those cases.

Without GitHub-hosted runners at all (`"hosted": false`), the route job is red for a run that may hold another
repository's code; make the route job a required check, so that red blocks the merge. The routed jobs then reach
`'ubuntu-latest'`: replace it with a GitHub-hosted label you have, or keep it and let the job fail; either way such a
run never reaches the fleet.

The workflow file of a pull request is the pull request's own: a pull request from another repository can change
any `runs-on`. The router only keeps honest workflows off self-hosted runners. The fleet's own guards (a dispatcher
that starts a runner only for a trusted run, the job-started hook) are what keep a changed workflow off a dispatcher
pool. A **static** pool (runners registered by hand, always there) has neither, so static pools are for **private
repositories only**: the config must say `"private": true`, and the router uses no static pool unless the event
says the repository is private (unknown counts as public). Keep "Require approval for all outside collaborators" on
as well.

### Inputs

| Input    | Default    | Meaning                                                                                                                 |
| -------- | ---------- | ----------------------------------------------------------------------------------------------------------------------- |
| `config` | (required) | the fleet config, a path in the checkout (see "Config")                                                                 |
| `vars`   | `{}`       | `${{ toJSON(vars) }}`: the setting (config `variable`) and each dispatcher pool's heartbeat are read from it            |
| `mode`   | (empty)    | `auto`, `fleet` or `github`; overrides the setting. Anything else fails the step (a typo never means `auto`)            |
| `gates`  | (empty)    | a JSON object of change gates; a job whose config `gates` are all `"false"` only prints a skip (where unrouted jobs go) |

### Outputs

| Output     | Value                                                                                                   |
| ---------- | ------------------------------------------------------------------------------------------------------- |
| `runners`  | JSON object: job key (job id, or `<id>-<shard>` for a sharded job) -> runs-on (a label or a label list) |
| `mode`     | the mode applied: `fleet` whenever the config has `"hosted": false`                                     |
| `attempt`  | the run attempt the labels were made for                                                                |
| `fallback` | JSON runs-on for a job of another attempt (a re-run of failed jobs); never a dispatcher pool's label    |
| `watch`    | `true` when a job went to a fleet pool                                                                  |

## Modes

The setting is the repository variable named by the config's `variable` (unset: `auto`).

| Mode     | Trusted run                                                                                                              |
| -------- | ------------------------------------------------------------------------------------------------------------------------ |
| `auto`   | each job goes to a pool with a free slot (pools in config order, jobs in config order); the rest GitHub-hosted           |
| `fleet`  | the same, and the rest queue on the pool that `waits` and takes this kind of run                                         |
| `github` | everything GitHub-hosted. With `"hosted": false` there is nothing GitHub-hosted: `fleet`, with an `::error::` annotation |

With `"hosted": false`, `auto` is `fleet`: a job always waits for a fleet runner rather than go nowhere.

## Config

One JSON file in the project's repository. A neutral example: a desktop whose dispatcher makes one-job runners
(at most one job of a run, pushes and draft pull requests only), an always-on build machine, and an always-on runner
for pull requests ready for review.

```json
{
  "repository": "example-org/example-app",
  "variable": "CI_RUNNERS",
  "hosted": false,
  "private": true,
  "trusted": {
    "workflows": [".github/workflows/ci.yml"],
    "actors": ["example-owner"],
    "push_branches": ["main"]
  },
  "pools": [
    {
      "name": "desktop",
      "label": "example-desktop",
      "slots": 2,
      "per_run": 1,
      "takes": ["push", "draft"],
      "beacon": "FLEET_BEACON_DESKTOP",
      "launch": ["/opt/fork-fleet/launch.sh"],
      "waits": false
    },
    {
      "name": "build",
      "static": true,
      "runs_on": ["self-hosted", "example-build"],
      "slots": 2,
      "takes": ["push", "draft"],
      "waits": true
    },
    {
      "name": "ready",
      "static": true,
      "runs_on": ["self-hosted", "example-ready"],
      "slots": 1,
      "takes": ["ready"],
      "waits": true
    }
  ],
  "jobs": [
    { "job": "plugins", "memory_mb": 6000 },
    { "job": "engine", "memory_mb": 4000, "gates": ["engine"] }
  ],
  "stuck_seconds": 900,
  "fleet_stuck_seconds": 7200,
  "beacon_fresh_seconds": 180,
  "dispatch": { "every_seconds": 15, "beacon_seconds": 60, "idle_passes": 3 }
}
```

| Key                    | Required               | Meaning                                                                                                                                  |
| ---------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| `repository`           | yes                    | `owner/name`; a run of any other repository is untrusted                                                                                 |
| `variable`             | no                     | the repository variable that holds the setting (read from the `vars` input)                                                              |
| `hosted`               | yes                    | the GitHub-hosted label (`ubuntu-24.04`), or `false` for none (then a static pool must take each kind of run)                            |
| `private`              | with a static pool     | `true`: the repository is private (static pools serve private repositories only; checked against each event too)                         |
| `trusted`              | yes                    | `workflows` (paths), `actors` (logins) and `push_branches`: who may use the fleet                                                        |
| `upstream_sync`        | no                     | `{"tag_prefix": "..."}`: a push commit tagged `<prefix><sha>` stays GitHub-hosted (the fork's upstream sync); not with `"hosted": false` |
| `pools`                | yes                    | tried in order; see below                                                                                                                |
| `jobs`                 | yes                    | routed jobs, heaviest first: `job` (id), `memory_mb`, optional `shards` (1-999) and `gates`                                              |
| `retired_labels`       | no                     | old pool labels (keep a label here for a week after changing it): never `hosted` or a static pool's label                                |
| `stuck_seconds`        | yes                    | auto: how long a fleet job may wait before the watch cancels its run (the sweep re-runs it)                                              |
| `fleet_stuck_seconds`  | yes                    | the same in fleet mode (cancelled with an error, not re-run)                                                                             |
| `beacon_fresh_seconds` | with a dispatcher pool | a heartbeat older than this counts no free slot, with an `::error::`                                                                     |
| `dispatch`             | with a dispatcher pool | the dispatcher's pace: `every_seconds`, `beacon_seconds`, `idle_passes`                                                                  |

A pool:

| Key       | Pools      | Meaning                                                                                                               |
| --------- | ---------- | --------------------------------------------------------------------------------------------------------------------- |
| `name`    | all        | a-z, 0-9, `_`, `-`                                                                                                    |
| `slots`   | all        | a dispatcher pool: runners at once; a static pool: jobs of one run it takes before the next pool                      |
| `waits`   | all        | `true` on exactly one pool per kind of run: where `fleet` mode queues the rest. It cannot have `per_run`              |
| `takes`   | all        | kinds of run it takes: `push`, `draft` (a draft pull request), `ready` (a pull request ready for review); default all |
| `per_run` | all        | at most this many jobs of one run (the first in `jobs` order, so the heaviest)                                        |
| `label`   | dispatcher | the pool's own label; each job also gets `<label>-<job key>-<16 hex>-a<attempt>`                                      |
| `beacon`  | dispatcher | the repository variable its dispatcher writes its heartbeat to (`{"at": <unix time>, "free": <slots>}`)               |
| `launch`  | dispatcher | the command the dispatcher starts and stops runners with (`.github/fork-fleet.md`)                                    |
| `static`  | static     | `true`: runners registered by hand and always there; no dispatcher, no heartbeat                                      |
| `runs_on` | static     | its runners' labels, at least one not generic (`self-hosted`, `linux`, `x64`, ...); no dispatcher or retired label    |

A config that breaks any rule above fails the step with the list of problems and puts out nothing.

## Changing pools without stranding runs

This action ships `route` only. The fork's own CI also runs the router's `watch` (cancels a run whose fleet job waits
too long) and `sweep` (re-runs it) with a token that may cancel runs; they are not part of this action, and a project
that uses only the action has neither. So:

- **Old runner labels:** keep the old label on its runners, and in `retired_labels`, for a week after a pool's label
  changes. Runs routed before the change queue on it and still find a runner.
- **Queued runs on a removed label** wait until GitHub drops them (24 hours). Cancel such a run by hand and re-run
  it; the re-run is routed by the new config.

## Tests

`fleet_route.test.py` runs the action's step as a runner does (no token, an empty `GH_CONFIG_DIR`), against the
neutral example above, a broken config and a missing one. The router's own tests are
[`.github/scripts/fork-fleet.test.py`](https://github.com/scuttletoby/t3code/blob/main/.github/scripts/fork-fleet.test.py) (class `AnyRepository`, and
`OldBugs` for bugs found in a project's earlier router).
