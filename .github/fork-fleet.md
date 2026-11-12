# Fork fleet runners

Fork only. Fork tests (`.github/workflows/fork-tests.yml`) can run its Linux jobs
on this repository's own machines (the fleet) instead of GitHub-hosted
Ubuntu 24.04. The macOS job and every other fork workflow stay GitHub-hosted.

No fleet runner waits for work. A dispatcher on each fleet host starts a
one-job runner for one queued job of a trusted run, and that runner is gone
after the job. A job that names a fleet label by itself finds no runner.

| File | What it does |
| --- | --- |
| `.github/fork-fleet.json` | Every fact about the repository and its fleet: the setting's name, the trust rule (workflows, actors, branches), the pools (label, slots, heartbeat variable, launch command, which pool `fleet` mode queues on), the jobs (shards, change gates, memory), and the time limits. |
| `.github/scripts/fork-fleet.py` | `route`, `watch`, `sweep`, `dispatch` and `gate`. Standard library only. It holds no repository facts. |
| `.github/scripts/fork-fleet-launch.sh` | Runs on a fleet host, as root, for `dispatch`: `start`, `active`, `fits`, `stop` and `prune` of one-job runners. |
| `.github/scripts/fork-fleet-hook.sh` | Each fleet runner's job-started hook. It runs `gate` from the installed copy. |
| `.github/scripts/fork-fleet.test.py` | The tests, and checks that the workflows match the config. Run by the `workflows` job. |
| `.github/workflows/fork-tests.yml` | The `Route` step of `changes` feeds each Linux job's `runs-on`. The `fleet_watch` job bounds the wait for a fleet runner. |
| `.github/workflows/fork-fleet.yml` | The sweep, every 10 minutes (GitHub-hosted). |
| `.github/workflows/fork-release.yml` | Grants `actions: write` to the called Fork tests, because `fleet_watch` asks for it. A release run is always GitHub-hosted. |

No upstream file changes. Upstream's `ci.yml` and `release.yml` are untouched, so
the fork-follow sweep and the drift check behave as before.

## The setting: `CI_RUNNERS`

A repository variable. If it is unset or empty, the mode is `auto`.

| Value | Trusted run | Untrusted run |
| --- | --- | --- |
| `auto` | Each job that will run goes to a pool whose dispatcher reports a free slot. Pools are tried in config order (`pc`, then `vm`), jobs in config order (longest first). The other jobs run GitHub-hosted. A pool counts only with a fresh heartbeat (at most 180 s old). No heartbeat, an old one or an unreadable one means GitHub-hosted, with a note. `auto` never turns a run red. | GitHub-hosted |
| `fleet` | Every job that will run goes to the fleet: pools with a free slot first, then the rest queue on the pool marked `waits` (`vm`). A shard whose change gate is off only prints a skip, so it runs GitHub-hosted. A fleet job that waits 2 hours cancels the run with an error. | GitHub-hosted |
| `github` | Everything GitHub-hosted, as before the fleet. Fleet jobs already queued when the setting changed are cancelled and re-run GitHub-hosted by the sweep after 10 minutes. | GitHub-hosted |
| anything else | The `Route` step fails, so `changes` and the run are red. A typo must not quietly mean `auto`. | (same) |

The route calls no API and reads no secret. In `auto` it reads only the pools'
heartbeat variables (`FLEET_BEACON_PC`, `FLEET_BEACON_VM`), which each
dispatcher writes: `{"at": <unix time>, "free": <free slots>}`.

## The trust rule

A run is **trusted** when all of these hold:

- the workflow is `fork-tests.yml` of `scuttletoby/t3code`;
- the actor *and* the triggering actor are `scuttletoby`;
- the run is a push to `main`, or a pull request whose head and base are both
  this repository and which `scuttletoby` opened.

Everything else is untrusted. That includes pull requests from other
repositories, bots, other events, the sweep's own re-runs (triggered by
`github-actions`), and `workflow_call`/`workflow_dispatch` release runs.
The repository is public, so a pull request from another fork would otherwise
run its own code on the owner's machines. Owner rule: pull requests from outside
the fork never run on the fleet.

### Three lines of defence

1. **`route`** sends an untrusted run GitHub-hosted. This is only a convenience:
   a pull request can change the workflow, so nothing relies on it.
2. **The dispatcher** is the root of the design. It runs as root on the fleet
   host, from a root-owned copy of `main`'s script and config. It starts a
   runner only when all of these hold:
   - the queued job asks for exactly two labels: a pool's label and the per-run
     label `route` made for that job (`<pool label>-<job key>-<16 random hex>`);
   - GitHub's own record of the run passes the trust rule. The dispatcher reads
     the run, not anything a job wrote, and reads each pull request from
     `pulls/<n>`. A pull request run from a fork lists no pull request of this
     repository, so it is refused;
   - the job's attempt is the run's attempt that it read (see "Races");
   - no other queued job, of any run, could be given that runner. A runner gets
     only those two labels (plus GitHub's defaults, which the check counts too).

   The runner is a one-job (JIT) registration named `<pool label>-<job id>`.
   It runs from a fresh copy of a root-owned runner template, as a user of its
   own, in a transient systemd unit. Its environment comes from a root-only
   file: the JIT config, the run and attempt it was made for, and the hook.
   So a job that names `fleet-vm-t3code` by itself, from a changed workflow or a
   fork's pull request, stays queued with no runner. No standing self-hosted
   runner exists that a label could reach.
3. **The gate** is the job-started hook. It runs before the job's first step and
   before any container setup. It refuses a job if it is not the run and attempt
   its runner was made for, or if the job breaks the trust rule. To refuse, it
   **kills the job's `Runner.Worker`** (SIGKILL). An exit code alone is not
   enough, because the runner treats the hook as a pre-job step and still runs
   later `if: always()` and `if: failure()` steps. With the worker killed, the
   runner marks the job failed and runs no later step. If the gate itself
   crashes, it refuses.

Two more things limit what a job can do:

- **#12:** jobs of a pull request from another repository do not run at all.
  The fork's approval policy is `all_external_contributors`, so a stranger's
  run first waits for the owner's approval.
- **Containment:** each runner uses a single copy. The unit's whole process
  group is killed when it stops, and a unit lives at most 1 hour. The runner
  user has no sudo, no docker group, no lingering and no cron or at, so nothing
  a job starts outlives its unit.

Fleet jobs read no secrets (a test checks this). The fleet's GitHub token lives
only on the fleet host, readable by root.

### Why the gate kills the worker (actions/runner, `main`, read 2026-11-12)

- The hook path is read from the worker's process environment, and the hook is
  added to the pre-job steps with condition `always()`. Container setup comes
  after it (`src/Runner.Worker/JobExtension.cs` ~303-315). A workflow cannot set
  the hook.
- The hook's environment starts empty. It gets the process environment plus the
  runtime `GITHUB_*`/`RUNNER_*` values, not the workflow's `env:`
  (`src/Runner.Worker/JobHookProvider.cs:74`). So `FORK_FLEET_RUN` and
  `FORK_FLEET_ATTEMPT` from the root-only file can be trusted.
- Later steps evaluate their own `if:`. After a failed hook, `always()` and
  `failure()` steps still run, which was the s1 blocker.
- When the worker dies, the listener marks the job failed, and nothing of it
  runs again (`src/Runner.Listener/JobDispatcher.cs:1179-1219`).
- The runner reads `ACTIONS_RUNNER_INPUT_JITCONFIG` from its environment and then
  deletes it (`src/Runner.Listener/CommandSettings.cs:112-138`). The JIT config
  never appears in argv.
- The runner loads `.env` from its own directory and lets it override its
  environment (`src/Runner.Listener/Program.cs:172-194`). This is why the
  launcher refuses a template that has a `.env` (or `.path`, `.runner`,
  `.credentials`). Each copy is used for one job only.

`GateProcess` in `fork-fleet.test.py` models this. A process named
`Runner.Worker` runs the installed hook, then an `always()` step that writes a
marker. A refused job must leave no marker. A mutant gate that only exits 1
does leave the marker, and that turns the test red.

## How a run flows

1. `changes` (upstream's change detector) decides which jobs run. Its last
   step, `Route`, writes `runners` (job key to labels or `ubuntu-24.04`),
   `mode`, `attempt` and `watch`.
2. Each Linux job's `runs-on` uses that answer only when the setting is not
   `github` and either `route` ran in this attempt or the mode was `fleet` and
   still is. Otherwise it is `ubuntu-24.04`. So in `auto`, a re-run of failed
   jobs goes GitHub-hosted instead of reusing attempt 1's picks. A re-run after
   the owner switched to `github` or `auto` follows the new setting.
3. The dispatcher (`dispatch`, every 20 s) lists queued and in-progress runs and
   their queued jobs. It cleans up leftovers, then starts at most one runner per
   job, as long as the pool has slots and the host has memory (`fits`: the job's
   `memory_mb` plus the runners started this pass, above a 2 GB reserve). It
   writes the pool's heartbeat when the free count changes, and at least once a
   minute.
4. On the runner, the job-started hook (`gate`) re-checks the run, the attempt
   and the trust rule before the first step.
5. `fleet_watch` runs in each run that sent a job to the fleet. Every 30 s it
   reads its own attempt's jobs. A fleet job queued with no runner for
   `stuck_seconds` (10 minutes; in `fleet` mode `fleet_stuck_seconds`, 2 hours)
   cancels the run with an error that names the job. It ends when every routed
   fleet job has a runner.
6. `sweep`, every 10 minutes, reads the `fork-tests.yml` runs of the last 24
   hours (the latest attempt of each). It leaves an untrusted run alone, by the
   same rule the dispatcher uses. In `auto` and `github` mode:
   - it cancels a run whose fleet job has been queued 10 minutes, if
     `fleet_watch` has not done so already;
   - it re-runs a run (all jobs: GitHub-hosted, since the sweep is not a trusted
     actor) when a fleet job waited 10 minutes and never started, or when a
     fleet job failed with no failed step of its own (its runner was lost). A
     run a person cancelled after a fleet runner took it stays cancelled;
   - it never re-runs when the branch has moved on or is gone.

   In `fleet` mode it re-runs nothing. It cancels, with an error, a run whose
   fleet job waited 2 hours. A re-run of failed jobs has no `fleet_watch`, so
   this is its only bound. The sweep keeps no state between passes.

## Races and limits

- **Re-run race (fixed).** The owner re-runs a run between the dispatcher
  reading the run and reading its jobs. The job then belongs to attempt 2,
  but its runner would be made for attempt 1, so the gate would kill the job.
  The dispatcher now skips a job whose `run_attempt` differs from the run it
  read, and starts its runner on the next pass.
- **Two runs in `auto` may both count one free slot.** Both route a job to the
  same pool before the heartbeat drops. The extra job waits for the slot. If the
  slot is not free within 10 minutes, `fleet_watch` cancels the run, and the
  sweep re-runs it GitHub-hosted. The run is slower, but it does not stay red.
- **A stranger's queued job that could take a fleet runner** blocks that
  runner's launch. Examples are a job asking only for `self-hosted` or
  `fleet-vm-t3code`, or one copying a run's label from its public log. The
  trusted job then waits and falls back as above (`auto`: GitHub-hosted after 10
  minutes; `fleet`: an error after 2 hours). The dispatcher's journal names the
  job that blocked it. A stranger's run is not even queued until the owner
  approves it (`all_external_contributors`), and #12 skips its jobs unless the
  pull request removed that check.
- **A full `fleet`-mode run on the `vm` pool's 2 slots** queues up to 16 Linux
  jobs, two at a time. A slow run can come near the 2-hour bound. `auto` is the
  everyday mode.
- **No `Runner.Worker` above the hook.** This only happens if a runner version
  renamed its worker. The gate then can only exit 1, and says so in the job log.
  Later `always()` steps would run. The real-runner check below catches this
  before the fleet is used.
- **Runner updates.** A runner older than GitHub's newest release updates itself
  when it starts. That happens inside its single-use copy: it is slower, and
  the template stays as it is. Keep the template current by hand (host step 2).
- **The runner user's home persists between jobs.** Tool caches such as rustup
  live there. Only trusted jobs ever run, so this is the owner's code carrying
  over to the owner's next job. `rm -rf` it to start clean.
- The sweep adds 144 short runs a day to the Actions list ("Fork fleet sweep").
  The repository is public, so they cost no minutes.
- Upstream-sync runs, and pull requests opened by anyone but `scuttletoby`
  (bots included), always run GitHub-hosted. Allowing them is a change to
  `trusted` and an owner decision.

## Host setup (VM106, the `vm` pool)

Run as root on the host. Nothing here prints a secret. Use the `main` commit
that has this change. Re-install the files (step 3) after any change on `main`
to the script, the config, the hook or the launcher, then
`systemctl restart t3code-fleet-dispatcher`. Until then, the host keeps the old
rule.

```sh
# 1. The runner user: no sudo, no docker group, no lingering, no cron or at.
useradd --create-home --shell /usr/sbin/nologin fork-runner
id fork-runner                                   # expect no sudo or docker group
loginctl disable-linger fork-runner
for f in cron at; do                             # with an /etc/$f.allow, just leave fork-runner out of it
  [ -e /etc/$f.allow ] || echo fork-runner >> /etc/$f.deny
done

# 2. The runner template: an unconfigured actions/runner release, owned by root, checked by sha256.
#    Take the version and the linux-x64 sha256 from https://github.com/actions/runner/releases
ver=X.Y.Z; sum=SHA256_FROM_THE_RELEASE_PAGE
tmp=$(mktemp -d)
curl -fsSLo "$tmp/runner.tgz" "https://github.com/actions/runner/releases/download/v$ver/actions-runner-linux-x64-$ver.tar.gz"
echo "$sum  $tmp/runner.tgz" | sha256sum -c -
rm -rf /opt/fork-fleet/runner-template.new && install -d -m 755 /opt/fork-fleet/runner-template.new
tar -xzf "$tmp/runner.tgz" -C /opt/fork-fleet/runner-template.new --no-same-owner
chmod -R go-w /opt/fork-fleet/runner-template.new
/opt/fork-fleet/runner-template.new/bin/installdependencies.sh   # once per host: the runner's own libraries
rm -rf /opt/fork-fleet/runner-template && mv /opt/fork-fleet/runner-template.new /opt/fork-fleet/runner-template
ls -a /opt/fork-fleet/runner-template            # no .env, .path, .runner or .credentials (never run config.sh here)

# 3. The fleet files, from one pinned main commit.
sha=FULL_MAIN_SHA
base=https://raw.githubusercontent.com/scuttletoby/t3code/$sha/.github
install -d -m 755 /opt/fork-fleet
for f in fork-fleet.json:fork-fleet.json:644 scripts/fork-fleet.py:fork-fleet.py:644 \
         scripts/fork-fleet-hook.sh:hook.sh:755 scripts/fork-fleet-launch.sh:fork-fleet-launch.sh:755; do
  IFS=: read -r src dst mode <<<"$f"
  curl -fsSLo "$tmp/$dst" "$base/$src" && install -o root -g root -m "$mode" "$tmp/$dst" "/opt/fork-fleet/$dst"
done
echo "$sha" > /opt/fork-fleet/SOURCE
python3 -I /opt/fork-fleet/fork-fleet.py --config /opt/fork-fleet/fork-fleet.json gate </dev/null; echo "exit $?"
# ^ expect an ::error:: line, "no Runner.Worker above this hook" and exit 1: the gate refuses outside a runner

# 4. The launcher's settings and the dispatcher's token (root only).
install -d -m 700 /etc/fork-fleet
printf 'RUNNER_USER=fork-runner\nTEMPLATE=/opt/fork-fleet/runner-template\n' > /etc/fork-fleet/launch.conf
chmod 600 /etc/fork-fleet/launch.conf
( umask 077; read -rsp 'token: ' t; printf '%s\n' "$t" > /etc/fork-fleet/token ); echo
# WORK_ROOT (/var/lib/fork-fleet/runners): the launcher makes it 0711. Every directory above it must let
# fork-runner through (o+x), or no runner can reach its copy.

# 5. The dispatcher, for the vm pool only.
cat > /etc/systemd/system/t3code-fleet-dispatcher.service <<'EOF'
[Unit]
Description=t3code fork fleet dispatcher (vm pool)
Wants=network-online.target
After=network-online.target

[Service]
ExecStart=/usr/bin/python3 -I /opt/fork-fleet/fork-fleet.py --config /opt/fork-fleet/fork-fleet.json dispatch --pool vm
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload && systemctl enable --now t3code-fleet-dispatcher
journalctl -u t3code-fleet-dispatcher -n 20      # each runner it starts or skips is logged here
rm -rf "$tmp"
```

Do not name the dispatcher's unit `fork-fleet-*`: that prefix is for runner
units. Runner units are separate transient units, so restarting the dispatcher
leaves running jobs alone.

**The `pc` pool** stays off until `/opt/fork-fleet/launch-pc` exists: a wrapper
that runs `fork-fleet-launch.sh` on the PC over ssh. Run the dispatcher with
`--pool vm` until then. Without its dispatcher, `pc` has no heartbeat, and
`route` never counts it. Whether and how the PC joins is an owner decision.

**What jobs need on the host.** The jobs run without sudo, so the host must
already have:

- `git`, `curl`, `unzip`, `python3` (3.8 or later, at `/usr/bin/python3`),
  `pkg-config` and a C/C++ toolchain;
- `libsecret-1-dev` (Build and Test check `pkg-config --exists libsecret-1`);
- `xvfb` (Build checks for `xvfb-run`);
- Chromium's system libraries for the browser tests
  (`npx playwright install-deps chromium`, once, as an admin).

Bun, Node and Rust are installed per job by the setup actions. Leave a few tens
of GB of disk for `/var/lib/fork-fleet/runners` and the runner user's home.

## Owner steps on the repository

Run these from a machine where `gh` is logged in as the owner. None of them
prints a secret.

```sh
# 1. The setting. Optional: unset means auto. Use fleet or github to force a mode.
gh variable set CI_RUNNERS -R scuttletoby/t3code --body auto

# 2. The dispatcher's token: a fine-grained personal access token with resource owner scuttletoby,
#    "Only select repositories: scuttletoby/t3code", and these repository permissions only:
#      Administration: Read and write   (make, list and delete the one-job runners)
#      Actions: Read-only               (runs and jobs)
#      Variables: Read and write        (the heartbeats FLEET_BEACON_*)
#      Pull requests: Read-only         (the trust rule reads each pull request)
#    Store it on the host only (host step 4). It is not a repository secret.

# 3. The old route token is no longer used. Delete it if it was set.
gh secret delete FLEET_RUNNERS_TOKEN -R scuttletoby/t3code

# 4. Check (names only). Approval for outside contributors must stay all_external_contributors.
gh variable list -R scuttletoby/t3code
gh api repos/scuttletoby/t3code/actions/permissions/fork-pr-contributor-approval --jq .approval_policy
```

## Real-runner check (before the fleet takes the fork's jobs)

The tests model the runner. This check runs the real one. Use a **throwaway
private repository** under the owner's account, never the public fork with a
stranger. Creating that repository is the owner's call. Its only collaborator is
the owner.

Set it up on VM106 next to the real install, with its own paths:

- Install the same four files under `/opt/fork-fleet-proof/`. In its
  `fork-fleet.json`, set `repository` to the proof repository, keep one pool
  `proof` with label `fleet-proof-t3code`, `slots: 1`, `waits: true`, beacon
  `FLEET_BEACON_PROOF` and launch
  `["/usr/bin/env", "FORK_FLEET_LAUNCH_CONF=/etc/fork-fleet-proof/launch.conf", "/opt/fork-fleet-proof/fork-fleet-launch.sh"]`,
  and one job `{"job": "lint", "memory_mb": 1024}`.
- In `/etc/fork-fleet-proof/launch.conf`, set `RUNNER_USER=fork-runner`,
  `TEMPLATE=/opt/fork-fleet/runner-template`,
  `WORK_ROOT=/var/lib/fork-fleet-proof/runners`, `RUN_DIR=/run/fork-fleet-proof`
  and `HOOK=/opt/fork-fleet-proof/hook.sh`.
- Store a token for the proof repository at `/etc/fork-fleet-proof/token`, with
  the same permissions as above.
- Run the dispatcher by hand when a step says so:
  `python3 -I /opt/fork-fleet-proof/fork-fleet.py --config /opt/fork-fleet-proof/fork-fleet.json dispatch --token-file /etc/fork-fleet-proof/token --lock /run/fork-fleet-proof/dispatch.lock`.

The proof repository's `.github/workflows/fork-tests.yml` needs two jobs. The
first writes `labels=["fleet-proof-t3code","fleet-proof-t3code-lint-<16 hex>"]`
with a random hex. The second, `lint`, has
`runs-on: ${{ fromJSON(needs.<first>.outputs.labels) }}` and these steps:

- `echo job ran`;
- a step that must fail. It tries to write the template through its copy:
  `! sh -c 'echo x >> "$GITHUB_WORKSPACE/../../../run.sh"' && ! touch "$GITHUB_WORKSPACE/../../../bin/proof"`;
- an `if: always()` step that runs `touch ~/fleet-proof-always-ran`.

Then check each of these:

1. **A trusted push runs.** Push to `main` with the dispatcher running. The job
   log shows `fork-fleet: allowed`, and the template step passes, so the runner
   user cannot write the template. While the job runs, `ls /run/fork-fleet-proof/`
   shows no `.env` file: the environment file is gone once the runner started.
   Within a minute after the job, `systemctl list-units 'fork-fleet-*'` shows no
   proof unit, and the dispatcher has pruned the copy. Remove
   `~fork-runner/fleet-proof-always-ran` afterwards.
2. **A refused job runs no `always()` step.** Stop the dispatcher. Push again, so
   `lint` queues. Start a runner by hand that is made for another run, with the
   job's two labels from its log:

   ```sh
   jit=$(gh api -X POST repos/OWNER/PROOF/actions/runners/generate-jitconfig -f name=fleet-proof-t3code-1 \
     -F runner_group_id=1 -f 'labels[]=fleet-proof-t3code' -f 'labels[]=LABEL_FROM_THE_JOB' -f work_folder=_work \
     --jq .encoded_jit_config)
   printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\nFORK_FLEET_RUN=1\nFORK_FLEET_ATTEMPT=1\n' "$jit" |
     FORK_FLEET_LAUNCH_CONF=/etc/fork-fleet-proof/launch.conf /opt/fork-fleet-proof/fork-fleet-launch.sh start fleet-proof-t3code-1
   unset jit
   ```

   The job is failed, none of its steps ran, and
   `~fork-runner/fleet-proof-always-ran` does not exist. The live log shows the
   gate's `this runner was made for run 1`. The saved log may lack it, because
   the worker died. Start the dispatcher again: it prunes the copy.
3. **A bare label finds no runner.** A second workflow, not in `trusted.workflows`,
   with `runs-on: [fleet-proof-t3code]`, stays queued, and the dispatcher starts
   nothing for it. Push to `main` while it waits: the dispatcher holds that run's
   `lint` back and logs which job blocks it. Cancel both.
4. **A re-run keeps its runner per attempt.** Re-run only the `lint` job of the
   first run, as the owner. It keeps attempt 1's labels, gets a new runner, and
   the gate logs `attempt 2`.

Delete the proof install (`/opt/fork-fleet-proof`, `/etc/fork-fleet-proof`,
`/var/lib/fork-fleet-proof`) and the proof repository's runners afterwards.

## Failure table

The question for each row: what happens when this runs again, stops half-way,
or meets existing, old or empty state? GitHub (runs, jobs, runner registrations,
the heartbeat variables) and the host (systemd units, copies) hold all state. The
dispatcher keeps only pacing in memory, so a restart starts over cleanly.

| Part | Situation | What happens |
| --- | --- | --- |
| Setting | `CI_RUNNERS` unset or empty | `auto`. |
| Setting | Misspelled | `Route` fails, so the run is red, and the message says why. Fix the variable and re-run. |
| Route | No heartbeat, an old one (over 180 s) or a garbled one | That pool counts no free slot. `auto`: GitHub-hosted, with a note. `fleet`: queue on `vm`. |
| Route | The `changes` outputs cannot be read | Every routed job counts as running, with a warning. |
| Route | Untrusted run (fork's pull request, bot, other actor, release run) | GitHub-hosted in every mode. A release run skips `Route` and is GitHub-hosted. |
| Route | Re-run of failed jobs | `route` does not run again. `auto` (attempt differs): GitHub-hosted. `fleet`: the old labels, while the setting is still `fleet`. `github` now: GitHub-hosted. |
| Route | Re-run of all jobs | `route` runs again with a new label: fleet as in a new run if the owner re-ran it, GitHub-hosted if the sweep or someone else did. |
| Route | Re-run of a run from before this change | That commit's workflow has no `Route`: GitHub-hosted. |
| Watch | A job waits past the bound | The run is cancelled with an error naming the job. `auto`: the sweep re-runs it GitHub-hosted within 10 minutes. |
| Watch | A routed job never shows up (a matrix job skipped) | After 2 minutes, the watch counts only the jobs it saw. |
| Watch | GitHub truncated, garbled or failed a reply | A warning. It tries again in 30 s, and after the bound it stops watching without turning red. The sweep is the fallback. |
| Watch | Re-run attempt | It watches its own attempt's jobs only. |
| Sweep | Stops half-way (cancelled, did not re-run) | The next sweep finds the cancelled run, whose fleet job waited 10 minutes and never started, and re-runs it. |
| Sweep | A cancel that takes over 2 minutes | It is left for the next sweep. |
| Sweep | Runs again on a run it re-ran | The re-run's triggering actor is `github-actions`, so the run is untrusted and left alone. Each run is re-run at most once. |
| Sweep | Two sweeps at once (manual and cron) | The concurrency group runs them one at a time. |
| Sweep | A stranger's run with fleet jobs | Left alone: never cancelled or re-run. |
| Sweep | A person cancels a run | Stands if a fleet runner had the job. A run cancelled while a fleet job had already waited 10 minutes is re-run, as the sweep would have done. |
| Sweep | Branch moved on or deleted | Not re-run. |
| Sweep | Runner lost mid-job (VM restart) | `auto`: re-run GitHub-hosted. `fleet`: stays red, and the owner re-runs it. |
| Sweep | A test fails on a fleet runner | Red, as on GitHub-hosted. Not re-run. |
| Sweep | GitHub trouble on one run | A warning, the sweep fails, and the next one tries again. |
| Sweep | `fleet` mode, a job waits 2 hours | The run is cancelled with an error, even a re-run of failed jobs that has no watch. Not re-run. |
| Dispatcher | Crash after `generate-jitconfig`, before `start` | An offline registration with no unit. The next pass deletes it, and the pass after starts the job's runner again (at most 3 tries per job, then it is left to the watch and the sweep). |
| Dispatcher | Crash after `start` | The unit is there. The next pass sees it and starts nothing twice. |
| Dispatcher | Launcher killed half-way through copying the template | No unit. The next pass deletes the registration, `prune` removes the half copy and any environment file, and the job's runner is started again. |
| Dispatcher | Restart, or a host reboot | It reads everything again from GitHub and systemd. The lock directory under `/run` is made again after a boot. After a reboot, a job that was running fails as a lost runner (`auto`: the sweep re-runs it GitHub-hosted). A job still queued has its offline registration deleted and gets a new runner. |
| Dispatcher | Two dispatchers for one host | The second one fails on the lock. |
| Dispatcher | The job went away before its runner took it (cancelled) | The idle runner is stopped after 3 passes, or at once if another queued job could take it. |
| Dispatcher | Re-run between reading the run and its jobs | Skipped for one pass, then a runner is made for the new attempt. |
| Dispatcher | The PC or VM has no memory | No start, and the heartbeat shows 0 free slots. `auto`: new runs go GitHub-hosted. |
| Dispatcher | GitHub or the host fails during a pass | A warning. The pass stops, and the next one, 20 s later, starts over. |
| Dispatcher | Token file missing, readable by anyone but root, or empty | It refuses to start. `systemctl status` shows why. |
| Dispatcher | Down for a while | Heartbeats go stale after 180 s. `auto`: GitHub-hosted. `fleet`: jobs queue, and the watch reports after 2 hours. |
| Gate | Run, attempt or trust rule does not match | `Runner.Worker` is killed before the first step: the job fails and no `always()` step runs. |
| Gate | Broken installed config | It refuses every job, failing closed. The dispatcher stops too. Re-install from `main`. |
| Gate | Installed script that does not even start (a syntax error) | The dispatcher fails on start too, so no runner starts. Restart the dispatcher after each re-install, so it never runs an older copy than the hook. |
| Gate | No `FORK_FLEET_RUN` (a runner not made by the dispatcher) | Refused. |
| Launcher | Template configured (`.env` and the like), a symlink, or writable by others | It refuses to start a runner, and the dispatcher logs the reason. |
| Launcher | `launch.conf` or the hook writable by anyone but root | It refuses. |
| Launcher | Bad input (an extra line, a key twice, a malformed value) | It refuses before touching anything. |
| Launcher | A name that ran already | It refuses. `prune` removes the old copy once its unit is gone. |
| Launcher | `systemd-run` fails | The environment file is removed on exit. The copy stays until `prune`. |
| Launcher | A runner that never gets its job, or hangs | Its unit stops after 1 hour (`MAX_SECONDS`), and its whole process group is killed. |
| Old state | Leftover copies or environment files from an old install | `prune` removes any whose unit is gone, on every pass. |
| Old state | Old `FLEET_RUNNERS_TOKEN` secret, or a standing runner from the first design (`vm106-t3code`) | Unused. Delete both. A standing runner is what this design removes, because any job asking for its labels can reach it. The fork has no runners today. |
| Old state | Installed copy older than `main` | It applies the old rule until re-installed (host step 3). `SOURCE` names the commit. |
| Empty state | No runs, no runners, no variables yet | The dispatcher creates the heartbeat variables on its first pass. `route` counts no slot until then. |
