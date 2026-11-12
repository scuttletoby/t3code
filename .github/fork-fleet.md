# Fork fleet runners

Fork only. Fork tests (`.github/workflows/fork-tests.yml`) can run its Linux jobs
on this repository's own self-hosted runners (the fleet) instead of GitHub-hosted
Ubuntu 24.04. The macOS job and every other fork workflow stay GitHub-hosted.

| File | What it does |
| --- | --- |
| `.github/fork-fleet.json` | Every fact about the repository and its fleet: the setting's name, the token secret, trusted workflows, actors and branches, pools (labels, runner names, memory checks), jobs (shards, change gates, memory). |
| `.github/scripts/fork-fleet.py` | `route`, `gate`, `capacity` and `sweep`. Standard library only. It holds no repository facts. |
| `.github/scripts/fork-fleet.test.py` | The tests, plus checks that the workflows match the config. Run by the `workflows` job. |
| `.github/workflows/fork-tests.yml` | A `route` job (GitHub-hosted) feeds each Linux job's `runs-on`, and each of those jobs runs a memory check after checkout. |
| `.github/workflows/fork-fleet.yml` | The sweep, every 10 minutes (GitHub-hosted). |

No upstream file changes: upstream's `ci.yml` and `release.yml` are untouched, so
the fork-follow sweep and the drift check behave as before.

## The setting: `CI_RUNNERS`

A repository variable. If it is unset, the mode is `auto`.

| Value | Trusted run | Untrusted run |
| --- | --- | --- |
| `auto` (also unset or empty) | Each job that will run gets an idle fleet runner while one is free, in pool order (`pc`, then `vm106`), heaviest jobs first. The other jobs run GitHub-hosted. Anything unclear goes GitHub-hosted with a note: no token, a GitHub error, or a re-run (attempt 2 or later). Auto never turns a run red. | GitHub-hosted |
| `fleet` | Every job that runs goes to the fleet: idle runners first, and the rest queue on the pool marked `waits` (`vm106`). Never GitHub-hosted, even without a token or on a re-run. | GitHub-hosted |
| `github` | Everything GitHub-hosted, as before the fleet. The route makes no API call. | GitHub-hosted |
| anything else | The `route` job fails, so the run is red. A typo must not quietly mean `auto`. | (same) |

**Trusted** means all of these hold:

- the workflow is `fork-tests.yml` of `scuttletoby/t3code`;
- the actor *and* the triggering actor are `scuttletoby`;
- the run is either a push to `main`, or a pull request whose head and base are both this repository and which `scuttletoby` opened.

Everything else is untrusted. That includes pull requests from other
repositories, bots, other events, and `workflow_call`/`workflow_dispatch`
release runs. The repository is public, so a pull request from another fork
would otherwise run its own code on the owner's machines.

The rule is enforced twice:

1. `route` sends untrusted runs GitHub-hosted.
2. Each fleet runner's job-started hook runs the same rule (`gate`) from a copy
   installed from `main`, and refuses any job that breaks it before the job's
   first step. This catches a pull request that edits the workflow to name a
   fleet label itself, and a re-run started by someone else.

Fleet jobs read no secrets: only the GitHub-hosted `route` job sees
`FLEET_RUNNERS_TOKEN`, and a test checks this.

## Owner steps (not done by the pull request)

Run these from a machine where `gh` is logged in as the owner. None of them
prints a secret.

```sh
# 1. The setting. Optional: unset means auto. Use fleet or github to force a mode.
gh variable set CI_RUNNERS -R scuttletoby/t3code --body auto

# 2. The route's read-only token: a fine-grained personal access token whose resource
#    owner is scuttletoby, with "Only select repositories: scuttletoby/t3code" and
#    Repository permissions "Administration: Read-only" (needed to list runners), nothing else.
#    gh prompts for the value; nothing is echoed or saved in shell history.
gh secret set FLEET_RUNNERS_TOKEN -R scuttletoby/t3code

# 3. Check (names only).
gh variable list -R scuttletoby/t3code
gh secret list -R scuttletoby/t3code
```

Without step 2, `auto` runs everything GitHub-hosted and explains why in a
notice. That is safe, but the fleet is never used.

### Registering the runners

Each pool's labels and runner name must match `.github/fork-fleet.json`
(`labels`, `runner_name`).

- **`vm106`**: one always-on runner named `vm106-t3code`, with the default labels
  (`self-hosted`, `Linux`, `X64`) plus `fleet-vm106-t3code`.

  ```sh
  # On the VM, in the runner directory, as the runner's own unprivileged user:
  ./config.sh --unattended --url https://github.com/scuttletoby/t3code \
    --name vm106-t3code --labels fleet-vm106-t3code \
    --token "$(gh api -X POST repos/scuttletoby/t3code/actions/runners/registration-token --jq .token)"
  ```

- **`pc`**: one-job (JIT) registrations named `fleet-pc-t3code` with only the
  label `fleet-pc-t3code`. A new one is made each time the PC is ready for a
  job, so the PC is online only while it waits for work.

  ```sh
  jit=$(gh api -X POST repos/scuttletoby/t3code/actions/runners/generate-jitconfig \
    -f name=fleet-pc-t3code -F runner_group_id=1 -f 'labels[]=fleet-pc-t3code' -f work_folder=_work \
    --jq .encoded_jit_config)
  ./run.sh --jitconfig "$jit"
  ```

  If the PC dies mid-job, its offline registration keeps the name. Delete it before
  making the next one:
  `gh api repos/scuttletoby/t3code/actions/runners --jq '.runners[] | select(.name=="fleet-pc-t3code") | .id'`,
  then `gh api -X DELETE repos/scuttletoby/t3code/actions/runners/<id>`.

### The job-started hook (every fleet runner)

Install the gate from `main`, owned by root and not writable by the runner user:

```sh
sudo install -d -m 755 /opt/fork-fleet
for f in .github/scripts/fork-fleet.py .github/fork-fleet.json; do
  gh api "repos/scuttletoby/t3code/contents/$f?ref=main" -H 'Accept: application/vnd.github.raw' \
    | sudo tee "/opt/fork-fleet/$(basename "$f")" >/dev/null
done
printf '#!/bin/sh\nexec python3 -I /opt/fork-fleet/fork-fleet.py --config /opt/fork-fleet/fork-fleet.json gate\n' \
  | sudo tee /opt/fork-fleet/job-started.sh >/dev/null
sudo chmod 755 /opt/fork-fleet/job-started.sh
# In the runner directory's .env, then restart the runner:
echo 'ACTIONS_RUNNER_HOOK_JOB_STARTED=/opt/fork-fleet/job-started.sh' >> .env
```

Re-install after any change to the trust rule or the config on `main`. Until you
do, the old copy keeps applying the old rule. If the installed copy breaks, the
gate fails closed: it refuses every job, and those jobs stay red until it is fixed.

### What a fleet host needs

The jobs run without sudo, so the host must already have:

- `git`, `curl`, `unzip`, `python3` (3.8 or later), `pkg-config` and a C/C++ toolchain;
- `libsecret-1-dev` (Build and Test check `pkg-config --exists libsecret-1`);
- `xvfb` (Build checks for `xvfb-run`);
- Chromium's system libraries for the browser tests (`npx playwright install-deps chromium`, run once as an admin);
- for `vm106`, the memory check `/usr/local/bin/t3-capacity <MB>`, which exits 0 when that much memory is free for a job.

Bun, Node and Rust are installed per job by the setup actions, in the runner's
home. Leave a few tens of GB of disk for the work folder and caches.

## How the moving parts fit

1. `changes` (upstream's change detector) decides which jobs run.
2. `route` reads `CI_RUNNERS`, applies the trust rule, and in `auto` lists the
   repository's runners. An idle runner is online, not busy, and has the pool's
   labels. Idle runners are handed out to the jobs that will run, in config order.
   The outputs are the `runners` JSON (job key to labels or `ubuntu-24.04`),
   `mode` and `attempt`.
3. Each Linux job's `runs-on` uses `route`'s answer only when `route` ran in this
   attempt or the mode is `fleet`. Otherwise it is `ubuntu-24.04`. So a re-run
   of only the failed jobs in `auto` goes GitHub-hosted instead of reusing
   attempt 1's runner picks.
4. On a fleet runner, the job-started hook (`gate`) re-checks the trust rule.
   After checkout, the step `Wait for memory on a fleet runner` (`capacity`)
   waits up to 5 minutes, checking every 30 s, for the pool's memory check to
   pass for the job's `memory_mb`. On GitHub-hosted, that step does nothing.
5. `sweep`, every 10 minutes, in `auto` and `github` mode, looks at the attempt-1
   runs of the last 24 hours:
   - It cancels a run whose fleet job has been queued 10 minutes
     (`stuck_seconds`) with no runner.
   - It re-runs (all jobs, so attempt 2 is GitHub-hosted) a run with any of
     these problems:
     - a fleet job waited 10 minutes and never started;
     - a fleet runner stopped mid-job (the job failed with no failed step of its own);
     - the only failed step is the memory wait.
   - It never re-runs when the branch has moved on (a newer run exists).
   - It keeps no state between sweeps.

## Failure table

The question for each row: what happens when this runs again, stops half-way,
or meets existing, old or empty state?

| Situation | `auto` | `fleet` |
| --- | --- | --- |
| `CI_RUNNERS` unset or empty | Treated as `auto`. | n/a |
| `CI_RUNNERS` misspelled | `route` fails and the run is red, saying so. Fix the variable and re-run. | (same) |
| `FLEET_RUNNERS_TOKEN` unset, expired or lacking permission | All jobs GitHub-hosted, with a notice or warning naming the secret. Green as usual. | All jobs queue on `vm106`, the waiting pool. The token is only needed to find idle runners. |
| GitHub API slow or down during `route` | GitHub-hosted with a warning (30 s timeout). | Queue on `vm106`. |
| Runner picked by `route` goes offline, or is taken by another run, before the job starts | The job queues. The sweep cancels the run after 10 minutes and re-runs it, and attempt 2 is GitHub-hosted. | The job waits for the runner (up to GitHub's 24-hour queue limit). |
| Runner goes offline mid-job (VM restart, PC shut down) | The job fails with no failed step. The next sweep re-runs the run GitHub-hosted. | The run stays red, and the owner re-runs it. Attempt 2 is still fleet. |
| Not enough memory on `vm106` | The memory step waits 5 minutes, then fails. The sweep re-runs the run GitHub-hosted. | The run stays red, and the owner re-runs it. |
| Memory check missing, or a fleet runner whose name is in no pool | The memory step fails with a clear error. The sweep re-runs GitHub-hosted, because the memory step is the only failed step. | Red until the host is fixed. |
| A test fails on a fleet runner | Red, as on GitHub-hosted. The sweep does not re-run real failures. | (same) |
| Re-run of failed jobs (attempt 2+) | `route` is not re-run, so its old attempt number differs: GitHub-hosted. | `route`'s old answer is kept, so the jobs stay on the fleet. |
| Re-run of all jobs (attempt 2+) | `route` sees attempt 2 and answers GitHub-hosted. | Fleet again. |
| Re-run by someone other than `scuttletoby`, or by the sweep (`github-actions`) | Untrusted triggering actor: GitHub-hosted. The gate would refuse a fleet label anyway. | A re-run of all jobs: GitHub-hosted. A re-run of only failed jobs keeps `route`'s fleet answer, and the gate refuses it: red, and nothing runs on the fleet. |
| Re-run of a run from before this change | That commit's workflow has no `route`: GitHub-hosted, as it was. | (same) |
| Pull request from another repository, or one whose workflow edit names a fleet label | `route` answers GitHub-hosted. A label hard-coded in the pull request's workflow is refused by the gate, which uses `main`'s installed copy and not the pull request's. | (same) |
| Sweep stops half-way (cancelled a run but did not re-run it) | The next sweep finds the cancelled run: its fleet job waited 10 minutes and never started, so it re-runs it. Stateless: GitHub is the only state. | Sweep does nothing. |
| Two sweeps at once (manual and cron) | The concurrency group runs them one at a time. The second sees attempt 2 and leaves the run alone. | (same) |
| A newer push to the branch | Old runs are not re-run (head moved). On pull requests the workflow's concurrency group cancels the older run, as before. | Old runs keep their place in the queue. |
| A person cancels a run | Stands if its fleet jobs had started. A run cancelled while a fleet job had already waited 10 minutes is re-run, just as the sweep would have done (unless the branch moved). | Stands. |
| Switching `fleet` → `auto` while jobs queue | Within 10 minutes the sweep cancels and re-runs them GitHub-hosted. | n/a |
| Switching to `github` | New runs: GitHub-hosted. Queued fleet jobs: cancelled and re-run by the sweep. | n/a |
| Broken `.github/fork-fleet.json` on a pull request | `route` and the config test fail: red on that pull request. | (same) |
| Broken installed gate copy on a runner | It refuses every job (fails closed), and those jobs stay red. Re-install from `main`. | (same) |

## Limits and notes

- `fleet` mode queues every job that runs on one `vm106` runner, one at a time:
  16 Linux jobs in a full run. A long queue can hit GitHub's 24-hour limit for
  queued jobs. `auto` is the everyday mode.
- The sweep adds 144 short runs a day to the Actions list ("Fork fleet sweep").
  The repository is public, so they cost no minutes.
- The fleet jobs take no host-wide lock. The memory check is the only guard
  against overloading a host that also runs other work.
- Upstream-sync runs, and pull requests opened by anyone but `scuttletoby`
  (including bots), always run GitHub-hosted. Allowing them is a change to
  `trusted` and an owner decision.
