# fleet-route

A composite action that decides, for each routed job of a run, **which runner it runs on**: one of the project's
own self-hosted pools (its fleet) or GitHub-hosted. It reads one config file and the repository variables, calls no
API and needs no token. It wraps the router that the fork's own CI uses
([`.github/scripts/fork-fleet.py`](../../../.github/scripts/fork-fleet.py) `route`; design and runbook:
[`.github/fork-fleet.md`](../../../.github/fork-fleet.md)).

What it guarantees:

- **A pull request whose head is another repository never gets a self-hosted runner**, in any mode and whatever the
  heartbeats say. Neither does any other run that breaks the trust rule (an untrusted actor, workflow or branch).
  It goes GitHub-hosted (the `hosted` label, or `ubuntu-latest` when `hosted` is `false`). It is never skipped:
  GitHub counts a skipped required check as passing.
- **Every label it puts out is one something serves**: the `hosted` label, `ubuntu-latest`, a static pool's
  `runs_on`, or a dispatcher pool's label plus this run's own label (the pool's dispatcher makes a one-job runner
  for exactly that job).
- **Fails closed, visibly.** A pool whose heartbeat is old or unreadable (its dispatcher is down, or its token
  expired) counts no free slot and gets an `::error::` annotation naming the pool; its jobs go to the pool that
  waits (with `"hosted": false`) or GitHub-hosted. A missing or broken config fails the step, and the routed jobs'
  `runs-on` falls back to the always-on pool (same repository) or `ubuntu-latest` (see "Usage").

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
      ${{ needs.route.result == 'success' && needs.route.outputs.attempt == github.run_attempt
          && fromJSON(needs.route.outputs.runners).plugins
          || (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)
          && fromJSON('["self-hosted", "proj-build"]')
          || 'ubuntu-latest' }}
```

The `runs-on` reads, in order:

1. the route's labels, when the route job passed **for this attempt**. A re-run of failed jobs does not re-run
   `route`, and a dispatcher's runner is made for one attempt only;
2. otherwise the always-on pool (here `proj-build`), but only for a push or a pull request of this repository;
3. otherwise `ubuntu-latest`.

Without GitHub-hosted runners at all, replace `'ubuntu-latest'` with a label that refuses the job (a skipped job would
count as passing), or keep it and let the job fail on cost limits; either way a pull request from another repository
never reaches the fleet.

The workflow file of a pull request is the pull request's own: a pull request from another repository can change
any `runs-on`. The router only keeps honest workflows off self-hosted runners. The fleet's own guards (a dispatcher
that starts a runner only for a trusted run, the job-started hook) are what keep a changed workflow out, so a
**static** pool (runners registered by hand, always there) is only as safe as the repository's own setting for pull
requests from forks: keep "Require approval for all outside collaborators" on, or the repository private.

### Inputs

| Input    | Default    | Meaning                                                                                                        |
| -------- | ---------- | -------------------------------------------------------------------------------------------------------------- |
| `config` | (required) | the fleet config, a path in the checkout (see "Config")                                                        |
| `vars`   | `{}`       | `${{ toJSON(vars) }}`: the setting (config `variable`) and each dispatcher pool's heartbeat are read from it   |
| `mode`   | (empty)    | `auto`, `fleet` or `github`; overrides the setting. Anything else fails the step (a typo never means `auto`)   |
| `gates`  | (empty)    | a JSON object of change gates; a job whose config `gates` are all `"false"` only prints a skip (GitHub-hosted) |

### Outputs

| Output    | Value                                                                                                   |
| --------- | ------------------------------------------------------------------------------------------------------- |
| `runners` | JSON object: job key (job id, or `<id>-<shard>` for a sharded job) -> runs-on (a label or a label list) |
| `mode`    | the mode applied: `fleet` whenever the config has `"hosted": false`                                     |
| `attempt` | the run attempt the labels were made for                                                                |
| `watch`   | `true` when a job went to a fleet pool                                                                  |

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

| Key                    | Required               | Meaning                                                                                                                |
| ---------------------- | ---------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| `repository`           | yes                    | `owner/name`; a run of any other repository is untrusted                                                               |
| `variable`             | no                     | the repository variable that holds the setting (read from the `vars` input)                                            |
| `hosted`               | yes                    | the GitHub-hosted label (`ubuntu-24.04`), or `false` for none                                                          |
| `trusted`              | yes                    | `workflows` (paths), `actors` (logins) and `push_branches`: who may use the fleet                                      |
| `upstream_sync`        | no                     | `{"tag_prefix": "..."}`: a push commit tagged `<prefix><sha>` stays GitHub-hosted (the fork's upstream sync)           |
| `pools`                | yes                    | tried in order; see below                                                                                              |
| `jobs`                 | yes                    | routed jobs, heaviest first: `job` (id), `memory_mb`, optional `shards` (1-999) and `gates`                            |
| `retired_labels`       | no                     | old pool labels: the watch and sweep still handle a job queued on one (keep a label here for a week after changing it) |
| `stuck_seconds`        | yes                    | auto: how long a fleet job may wait before the watch cancels its run (the sweep re-runs it)                            |
| `fleet_stuck_seconds`  | yes                    | the same in fleet mode (cancelled with an error, not re-run)                                                           |
| `beacon_fresh_seconds` | with a dispatcher pool | a heartbeat older than this counts no free slot, with an `::error::`                                                   |
| `dispatch`             | with a dispatcher pool | the dispatcher's pace: `every_seconds`, `beacon_seconds`, `idle_passes`                                                |

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
| `runs_on` | static     | the labels its runners have, at least one not generic (`self-hosted`, `linux`, `x64`, ...)                            |

A config that breaks any rule above fails the step with the list of problems and puts out nothing.

## Changing pools without stranding runs

- **Old runner labels:** keep the old label on its runners, and in `retired_labels`, for a week after a pool's label
  changes. Runs routed before the change queue on it; the watch and the sweep still recognise them.
- **Queued runs on a removed label:** with the label in `retired_labels`, the watch or the sweep cancels such a job
  after `stuck_seconds` (`fleet_stuck_seconds` in `fleet` mode, with an error); in `auto` the sweep then re-runs
  the run, routed by the new config.

## Tests

`fleet_route.test.py` runs the action's step as a runner does (no token, an empty `GH_CONFIG_DIR`), against the
neutral example above, a broken config and a missing one. The router's own tests are
[`.github/scripts/fork-fleet.test.py`](../../../.github/scripts/fork-fleet.test.py) (class `AnyRepository`, and
`OldBugs` for bugs found in a project's earlier router).
