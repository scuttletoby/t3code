# Fork fleet runners

Fork only. Fork tests (`.github/workflows/fork-tests.yml`) can run its Linux jobs
on this repository's own machines (the fleet) instead of GitHub-hosted
Ubuntu 24.04. The macOS job and every other fork workflow stay GitHub-hosted.

No fleet runner waits for work. A dispatcher on each fleet host starts a
one-job runner for one queued job of a trusted run, and that runner is gone
after the job. A job that names a fleet label by itself finds no runner.

| File                                       | What it does                                                                                                                                                                                                                                                                    |
| ------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `.github/fork-fleet.json`                  | Every fact about the repository and its fleet: the setting's name, the trust rule (workflows, actors, branches), the pools (label, slots, heartbeat variable, launch command, which pool `fleet` mode queues on), the jobs (shards, change gates, memory), and the time limits. |
| `.github/scripts/fork-fleet.py`            | `route`, `watch`, `sweep`, `dispatch` and `gate`. Standard library only. It holds no repository facts.                                                                                                                                                                          |
| `.github/scripts/fork-fleet-launch.sh`     | Runs on a fleet host, as root, for `dispatch`: `start`, `ready`, `active`, `fits`, `stop`, `prune` and `refresh-net` of one-job runners. Its `refresh` keeps the runner template new (host step 7).                                                                             |
| `.github/scripts/fork-fleet-refresh.*`     | The hourly timer and the sandboxed service of `refresh` (installed as `t3code-fleet-refresh`, host step 7). `fork-fleet-refresh.test.py` tests both, with a fake release server.                                                                                                |
| `.github/scripts/fork-fleet-job-paths.txt` | The one mount rule: the host's system paths every fleet job sees (read-only). The launcher's `job_fs` reads it; the per-OS walls ask the launcher (`paths`).                                                                                                                    |
| `.github/scripts/fork-fleet-hook.sh`       | Each fleet runner's job-started hook. It runs `gate` from the installed copy.                                                                                                                                                                                                   |
| `.github/scripts/fork-fleet.test.py`       | The tests, and checks that the workflows match the config. Run by the `workflows` job.                                                                                                                                                                                          |
| `.github/workflows/fork-tests.yml`         | The `Route` step of `changes` feeds each Linux job's `runs-on`. The `fleet_watch` job bounds the wait for a fleet runner.                                                                                                                                                       |
| `.github/workflows/fork-fleet.yml`         | The sweep, every 10 minutes (GitHub-hosted).                                                                                                                                                                                                                                    |
| `.github/workflows/fork-release.yml`       | Grants `actions: write` to the called Fork tests, because `fleet_watch` asks for it. A release run is always GitHub-hosted.                                                                                                                                                     |

No upstream file changes. Upstream's `ci.yml` and `release.yml` are untouched, so
the fork-follow sweep and the drift check behave as before.

**Owner decision (2026-11-12): upstream sync on GitHub-hosted runners only.**
The upstream sync (`fork-follow.yml`, with `fork-checks.yml` and the sweep
`fork-fleet.yml`) runs only on GitHub-hosted runners, never on fleet or other
self-hosted ones, whatever `CI_RUNNERS` says. This is enforced, not just written
down: `test_the_upstream_sync_runs_on_github_hosted_runners_only` in
`fork-fleet.test.py` fails when any job of those workflows gets a `runs-on`
other than a fixed GitHub-hosted image, or when the release they call could
route.

## The setting: `CI_RUNNERS`

A repository variable. If it is unset or empty, the mode is `auto`.

| Value         | Trusted run                                                                                                                                                                                                                                                                                                                                                                 | Untrusted run |
| ------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------- |
| `auto`        | Each job that will run goes to a pool whose dispatcher reports a free slot. Pools are tried in config order (`pc`, then `vm`), jobs in config order (longest first). The other jobs run GitHub-hosted. A pool counts only with a fresh heartbeat (at most 180 s old); none: a log line; old or unreadable (dispatcher down, token expired): an error annotation. Never red. | GitHub-hosted |
| `fleet`       | Every job that will run goes to the fleet: pools with a free slot first, then the rest queue on the pool marked `waits` (`vm`). A shard whose change gate is off only prints a skip, so it runs GitHub-hosted. A fleet job that waits 2 hours cancels the run with an error.                                                                                                | GitHub-hosted |
| `github`      | Everything GitHub-hosted, as before the fleet. Fleet jobs already queued when the setting changed are cancelled and re-run GitHub-hosted by the sweep after 10 minutes.                                                                                                                                                                                                     | GitHub-hosted |
| anything else | The `Route` step fails, so `changes` and the run are red. A typo must not quietly mean `auto`.                                                                                                                                                                                                                                                                              | (same)        |

The route calls no API and reads no secret. In `auto` it reads only the pools'
heartbeat variables (`FLEET_BEACON_PC`, `FLEET_BEACON_VM`), which each
dispatcher writes: `{"at": <unix time>, "free": <free slots>}`.

## The trust rule

A run is **trusted** when all of these hold:

- the workflow is `fork-tests.yml` of `scuttletoby/t3code`;
- the actor _and_ the triggering actor are `scuttletoby`;
- the run is a push to `main` of a commit the upstream sync did not make, or a
  pull request whose head and base are both this repository and which
  `scuttletoby` opened.

Everything else is untrusted. That includes pull requests from other
repositories, bots, other events, the sweep's own re-runs (triggered by
`github-actions`), and `workflow_call`/`workflow_dispatch` release runs.
The repository is public, so a pull request from another fork would otherwise
run its own code on the owner's machines. Owner rule: pull requests from outside
the fork never run on the fleet.

The trust rule decides where a run's jobs run, not what may merge. Merging is
decided by required checks; the one a pull request cannot rewrite is the head
gate ([Gate trust](../docs/fork/gate-trust.md)). The gate always runs
GitHub-hosted, so its App key is never on a fleet host.

**The upstream sync's push runs GitHub-hosted.** Owner decision: upstream's
code runs on GitHub-hosted runners only. The daily sync (`fork-follow.yml`)
moves `main` with `FORK_PUSH_TOKEN`, the owner's own token, so the push run that
follows has the owner as actor and would pass every check above. So
`fork-follow-push.sh advance` first pushes the tag
`fork-follow/synced/<sha>` (`upstream_sync.tag_prefix` in `fork-fleet.json`)
for the commit it moves `main` to, and moves `main` only when that push
worked. The rule asks this repository, anonymously
(`git ls-remote --exit-code`), whether the pushed commit has that tag: if it
has, or if the answer cannot be read, the run is untrusted. An upstream commit
cannot fake its way past this: only a holder of push rights here (the owner,
the sync's token) can make a tag in this repository, and nothing in a commit
(its message, author, files) or upstream (its branches, tags, pull requests)
makes or removes one. The only thing a tag does is send a run GitHub-hosted.
Keep the tags: a run can be re-run for 30 days. What this does not cover: once
synced, upstream's code is in `main`, so the owner's later pull requests and
pushes, which build on it, run it on the fleet.

### Three lines of defence

1. **`route`** sends an untrusted run GitHub-hosted. This is only a convenience:
   a pull request can change the workflow, so nothing relies on it.
2. **The dispatcher** is the root of the design. It runs as root on the fleet
   host, from a root-owned copy of `main`'s script and config. It starts a
   runner only when all of these hold:
   - the queued job asks for exactly two labels: a pool's label and the per-run
     label `route` made for that job, with the attempt the job's `runs-on` adds
     (`<pool label>-<job key>-<16 random hex>-a<attempt>`);
   - GitHub's own record of the run passes the trust rule. The dispatcher reads
     the run, not anything a job wrote, and reads each pull request from
     `pulls/<n>`. A pull request run from a fork lists no pull request of this
     repository, so it is refused;
   - the job's attempt is the run's attempt that it read, and the attempt in the
     job's label (`-a<attempt>`, see "Races") is that attempt too;
   - no other queued job, of any run, could be given that runner. A runner gets
     only those two labels (plus GitHub's defaults, which the check counts too).

   The runner is a one-job (JIT) registration named `<pool label>-<job id>`.
   It runs from a fresh copy of a root-owned runner template, in a transient
   systemd unit, as the runner user of a free slot (`RUNNER_USERS`, one user per
   slot): no other unit runs as that user at the same time. Its environment
   comes from a root-only file: the JIT config, the run and attempt it was made
   for, `HOME` and `TMPDIR` (both inside the copy) and the hook.
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
- **Containment:** each runner uses a single copy, and its `HOME` and `TMPDIR`
  are in that copy. The copy's top directory is its runner user's alone (0700),
  so the other slot's user cannot read its JIT config, tokens or caches, or
  change how it starts. Each unit has its own `/tmp`, `/var/tmp`, `/dev/shm`
  and `/run` (memory file systems that go with the unit) and its own IPC. It
  sees no `/home`, no other user's processes, and the rest of the system
  read-only. The unit's whole process group is killed when it stops, and a unit
  lives at most 1 hour. A runner user has no sudo, no docker group, no lingering
  and no cron or at. Before a user is given a new job, the launcher kills every
  process left of it and removes every copy it ran in (with its home). So a job
  leaves nothing for the next job: not a process, not a file in its home, not
  one in shared memory. Root never deletes inside a directory a runner user can
  still write: a copy is removed only once no unit can write it. The launcher
  refuses two slots that are one user, or a runner user in any other group or
  lingering.
- **Network:** the `vm` pool runs on the owner's workspace machine, not on a machine
  of its own. So each runner user has a network namespace of its own
  (`fork-fleet-<user>`) with its own loopback: a job reaches none of the host's
  loopback services (T3's server, web GUIs, databases) and no abstract socket of
  it. Its one link goes out through the host to the public internet only. The
  nft table `inet fork_fleet` refuses the host itself, RFC 1918 (the LAN and its
  other machines), CGNAT `100.64.0.0/10` (Tailscale and the like), link-local,
  the other reserved ranges, all of IPv6, and the host's own public IPv4 address
  (the router's admin page and every port forwarded back into the home answer
  there; looked up at `OWN_ADDRESS_URLS`, two by default, before every start
  and on a dispatcher pass once the last answer is `OWN_ADDRESS_SECONDS` old,
  2 minutes by default, counted from when it was asked). Every address any lookup gives is blocked (two
  WAN links each show another one), and the last 4 stay blocked, so one lookup
  that is down opens nothing. When the address changes while jobs run,
  `refresh-net` re-applies the table and adds the new address to each running
  unit's `IPAddressDeny`. The unit's `IPAddressDeny` refuses the same again, in
  the kernel, should the table ever go. The launcher (re)makes the namespace and
  the table at every start and starts no runner when either fails or no lookup
  answers; a dispatcher pass whose `refresh-net` fails starts nothing on that
  pool (the last block stays) until it works again.
- **Walls (every pool):** the unit gets no `AF_VSOCK` socket (a VM's channel to
  its host, not namespaced; WSL2's way to Windows), and none through the two
  ways around that filter: `io_uring` (fails with ENOSYS, so tools use plain
  calls) and 32-bit system calls (they end the process). Host step 6 proves the
  network and the walls from inside a runner.
- **Files and devices (every pool), one rule:** a job's unit has a root
  folder of its own, empty but for symlinks. Of the host's files it sees only
  the paths in `fork-fleet-job-paths.txt` (`/usr`, `/etc`, the `/bin` and `/lib`
  links), each read-only and one mount (nothing mounted under it), the
  launcher's `ALLOW_PATHS`, the hook and its own copy. So a share or drive
  mounted anywhere else (`/mnt`, `/media`, `/srv`, `/data`, a Windows drive at
  `/c`) is not there. `MountFlags=private`: a mount the host makes after the job
  started never reaches it. Each path is bound from its real path, every
  symlink in it resolved first, and that real path is checked too: a path
  under a linked folder (`/opt/x -> /c`, `/home` or a share) is refused or
  checked where it really is. `/run`, `/tmp`, `/var/tmp` and `/dev/shm` are new
  and empty, so no socket of the host's (X11, PulseAudio, WSL's interop) is
  there. `PrivateDevices=yes`: `/dev` holds only `null`, `zero`, `full`,
  `random`, `urandom`, `tty` and the job's own ptys; no GPU (`/dev/dri`,
  `/dev/dxg`), disk, `kvm`, `fuse` or `tun`. Git, Node, Java and the Docker
  client need none of these; a job never gets the Docker daemon (its socket is
  root on the host). Step 6 proves each of these from inside a runner, and
  that each bound path is the one the launcher checked. The per-OS walls ask
  the launcher (`fork-fleet-launch.sh paths`, and `tools` for the
  `systemd-run` and `systemctl` it runs) instead of keeping a list of their
  own. A systemd drop-in that changes the job units (`service.d`,
  `fork-.service.d` or `fork-fleet-*.service.d` in any unit folder,
  `system.control` and the generators' among them) fails them (`job-units`).
  So does an empty `IPAddressDeny=` (systemd's reset), except in the one file
  systemd writes for refresh-net's `set-property` on a running job unit
  (`/run/systemd/transient/<unit>.d/50-IPAddressDeny.conf`), and there only
  when the list after it still denies every range the launcher denies and all
  the unit itself was started with (the host's own address among them); a
  slice the job units sit in (`system.slice`, `-.slice`, every slice's
  `slice.d`) that allows more than localhost (a unit takes its slices' rules,
  and allow beats deny); a stop timeout that is endless or longer than 5
  minutes (a job that ignores SIGTERM would run on past its limit), in a
  drop-in, or as systemd's own default (`system.conf`: the first of `/etc`,
  `/run`, `/usr/local/lib` and `/usr/lib`, and every `system.conf.d`) while a
  job unit an older launcher started is still loaded, or while the installed
  launcher is such an older one (the walls read its `start()`: the launcher
  gives each job unit its own stop timeouts, 90 s, so the default decides
  nothing for the units it starts now; an endless one of a unit's own fails
  too); and a
  launcher setting that runs another program than systemd's own. A list counts
  only in the section systemd reads it in (`[Service]`, `[Slice]`), as systemd
  drops it anywhere else. On WSL2 they also read every property the launcher gives a job's unit
  (each way `systemd-run` takes one), of the launcher they ship with only:
  its sha256 is pinned in the `wsl2` walls file, and any other launcher
  cannot be told and fails (no reading of bash can follow every way a changed
  one could hand a unit more). A launcher change moves the pin in the same
  pull request: it needs the security lens, and the pin test also runs cheap
  tripwires on the launcher's text (a second `systemd-run`, `eval` or
  `source` even spelt with quotes or backslashes, a command word built from
  expansions such as `${e}al`, a `#` inside quotes, a function defined twice,
  binds made outside `job_view`), so that none of the edits the scan cannot follow comes back
  with a moved pin unseen. A bind that `paths` did not make, or
  `PrivateDevices=` that is not yes, fails them, and a property they cannot
  read fails them too. One rule for every folder on the way to a bound path,
  `/` too: it is root's (or the launcher user's), and only its owner may write
  it, or it is sticky; in a sticky folder others may
  write, the next folder on the way must be root's or the launcher user's; a
  symlink there must also be root's, in a folder of root's. Otherwise another
  user, or any member of the folder's group, could repoint the link, or swap a
  folder for one. The refusal names the fix (`chmod go-w <folder>`, or
  `chmod +t <folder>`).
  Before a new kind of host joins a pool (a Windows PC's WSL2 first),
  `sudo bash .github/scripts/fork-fleet-walls-proof.sh` runs step 6 there with
  throwaway users, then each network block alone, prints the cgroup version,
  and undoes all it made. It refuses a host with any part of a pool, running or
  not.

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
- **Re-run after a cancel (fixed).** A run is cancelled before its fleet job
  started, so its runner is still idle, and the owner re-runs it. Without more,
  the new job asks for the same labels, so GitHub could give it the old idle
  runner, made for attempt 1, and the gate would kill it. Each job's `runs-on`
  now adds the attempt to its label (`-a<attempt>`), so a job of attempt 2 can
  never be given a runner made for attempt 1. The dispatcher starts a runner
  only for a label whose attempt is the run's. This is structural: it does not
  depend on stopping the idle runner in time (that still happens, as cleanup).
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
- **An outdated template stops the fleet (fail closed).** A runner older than
  GitHub's newest `actions/runner` release would update itself in its copy
  before the job. The fleet does not rely on that. The dispatcher reads the
  newest release once an hour, and the launcher's `ready` compares it with the
  template's `.fork-fleet-version` (written by `refresh`, host step 7). When the template
  is older, or has no version file, no runner starts on that pool. The
  heartbeat then reports `free: 0` with a `why` that says to refresh the
  template, so `auto` goes GitHub-hosted. Running jobs and cleanup go on. The
  refresh timer (host step 7) fixes this within about an hour; to fix it at once, run
  `fork-fleet-launch.sh refresh`. If the release cannot be read, the last
  version read is kept; before the first read, nothing starts. With no template
  at all, `ready` says so and no runner starts.
- **Per-pool locks.** Each dispatcher takes `dispatch-<pool>.lock` in
  `--lock-dir` (default `/run/fork-fleet`) for each pool it serves. Two
  dispatchers can serve different pools, never the same one.
- **One pool's host fails.** A pass that cannot reach a pool's host (the
  launcher fails or cannot be run) leaves that pool alone for the rest of the
  pass: no cleanup, no start, and its heartbeat is `free: 0` with the reason.
  The other pools go on as usual.
- The sweep adds 144 short runs a day to the Actions list ("Fork fleet sweep").
  The repository is public, so they cost no minutes.
- Upstream-sync runs, and pull requests opened by anyone but `scuttletoby`
  (bots included), always run GitHub-hosted. Allowing them is a change to
  `trusted` and an owner decision.

### Other projects' configs (not used by this fork)

The fork's own config has a hosted label and only dispatcher pools. A project
that uses the router (or the `fleet-route` action) may also set:

- **`"hosted"`** must be a GitHub-hosted label. The config check, and `route`
  again at run time, refuse a generic self-hosted label (`self-hosted`,
  `linux`, `x64`, ...), a static pool's label, a dispatcher pool's label or run
  label, and a retired label: untrusted runs go to `hosted`.
- **Static pools** (`"static": true`, runners registered by hand) have no
  dispatcher and no job-started gate, so a pull request from a fork could edit
  its workflow to name their labels. They serve **private repositories only**:
  the config must say `"private": true`, and `route` uses no static pool unless
  the event says `repository.private: true` (unknown refuses, with an error on
  the run page). A static pool's labels may not be a dispatcher pool's label or
  a retired label.
- **`"hosted": false`** (no GitHub-hosted runners). Each kind then needs a
  static pool, and `upstream_sync` is refused. Every job that is not routed (a
  change gate off, a manual, scheduled or bot run, an untrusted run of this
  repository) goes to that kind's always-on static pool, never to
  `ubuntu-latest`. A run that may hold another repository's code (a fork's pull
  request, `pull_request_target` from a fork, `workflow_run`, any other event)
  turns `route` red with a clear error: it is never skipped and never
  self-hosted.
- **`route --with-attempt` and "Re-run failed jobs".** A re-run of failed jobs
  does not re-run `route`, and a run label serves one attempt only. So `route`
  also writes `fallback`: the always-on static pool for a trusted run, else
  where the run's jobs went (`hosted`); never a dispatcher pool's label. A job
  whose `github.run_attempt` is not the `attempt` output uses `fallback`. The
  `fleet-route` action's README has the `runs-on` expression.

## Host setup (the workspace machine, the `vm` pool)

Run as root on the host. Nothing here prints a secret. Use the `main` commit
that has this change. This host is the owner's workspace, not a machine of its own: the
runners' network (step 1b, the launcher, step 6) is what keeps jobs off its
services and its LAN. Steps 1b to 3 each run in a subshell with
`set -euo pipefail`, so the first failing command stops that step (and leaves
your shell open). Re-install the files (step 3) after any change on `main` to
the script, the config, the hook or the launcher, then
`systemctl restart t3code-fleet-dispatcher`. Until then, the host keeps the old
rule.

```sh
# 1. One runner user per slot (the vm pool has 2): no home, no sudo, no docker group, no lingering, no cron or at.
#    Safe to run again: an existing user is kept, and each deny line is added once.
for u in fork-runner-1 fork-runner-2; do
  id "$u" >/dev/null 2>&1 || useradd --no-create-home --home-dir /nonexistent --user-group --shell /usr/sbin/nologin "$u"
  id "$u"                                        # expect only its own group: no sudo, no docker
  loginctl disable-linger "$u" 2>/dev/null || true
  for f in cron at; do                           # with an /etc/$f.allow, just leave the user out of it
    [ -e /etc/$f.allow ] || grep -qx "$u" /etc/$f.deny 2>/dev/null || echo "$u" >> /etc/$f.deny
  done
done
# The launcher refuses a runner user that is in another group, lingers, or is the same user as another slot.
# A shared user from an earlier install is no longer used: id fork-runner >/dev/null 2>&1 && userdel -r fork-runner

# 1b. What the runners' network needs (the launcher makes each runner user's namespace and the nft table itself,
#     at every start, and starts nothing without them): nftables, IPv4 forwarding on, and the jobs' resolv.conf
#     (public nameservers: this host's resolver is on its loopback, which a runner's namespace does not have).
#     NET_BASE (step 4; default 10.204, the runners' 10.204.0.0/16) must be a range nothing else on the host uses
#     (another sandbox, a VPN, Docker networks). Safe to run again.
(
  set -euo pipefail
  command -v nft >/dev/null || apt-get install -y nftables
  printf 'net.ipv4.ip_forward = 1\n' > /etc/sysctl.d/60-fork-fleet.conf
  sysctl -q -p /etc/sysctl.d/60-fork-fleet.conf
  install -d -m 700 /etc/fork-fleet
  printf 'nameserver 1.1.1.1\nnameserver 9.9.9.9\n' > /etc/fork-fleet/resolv.conf
  chmod 644 /etc/fork-fleet/resolv.conf
  ! ip -4 route | grep -q '^10\.204\.' || echo "10.204 is in use here: pick another NET_BASE in step 4"
)

# 2. The runner template: an unconfigured actions/runner release, owned by root. The launcher's `refresh` makes it,
#    checked by sha256, and keeps it new (step 7, right after step 4: it needs the launcher and its settings). Until
#    then the pool stays closed ("there is no runner template"). It lives alone in a folder of its own,
#    /opt/fork-fleet/template/runner-template (the launcher's default TEMPLATE): the refresh service may write only
#    that folder. /opt and /var/lib must be on one filesystem: each copy hard-links the template (the launcher checks it).

# 3. The fleet files, from one pinned main commit. All of them are fetched first and installed only when each came;
#    SOURCE is written last, so it never names a commit whose files are not all in place. Another project that
#    carries these files: point `base` below at that project's repository and use a commit of its main.
(
  set -euo pipefail
  sha=FULL_MAIN_SHA
  [[ $sha =~ ^[0-9a-f]{40}$ ]] || { echo "set sha first"; exit 1; }
  tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
  base=https://raw.githubusercontent.com/scuttletoby/t3code/$sha/.github
  files="fork-fleet.json:fork-fleet.json:644 scripts/fork-fleet.py:fork-fleet.py:644
         scripts/fork-fleet-hook.sh:hook.sh:755 scripts/fork-fleet-launch.sh:fork-fleet-launch.sh:755
         scripts/fork-fleet-desk.py:fork-fleet-desk.py:644 scripts/fork-fleet-desk-wire.py:fork-fleet-desk-wire.py:644
         scripts/fork-fleet-desk-seed.py:fork-fleet-desk-seed.py:644
         scripts/fork-fleet-desk-key.py:fork-fleet-desk-key.py:644
         scripts/fork-fleet-refresh.service:fork-fleet-refresh.service:644
         scripts/fork-fleet-refresh.timer:fork-fleet-refresh.timer:644
         scripts/fork-fleet-job-paths.txt:fork-fleet-job-paths.txt:644"
  for f in $files; do
    IFS=: read -r src dst mode <<<"$f"
    curl -fsSLo "$tmp/$dst" "$base/$src"
  done
  # It loads and its config passes: the gate then gets as far as its refusal outside a runner.
  out=$(python3 -I "$tmp/fork-fleet.py" --config "$tmp/fork-fleet.json" gate </dev/null 2>&1 || true)
  grep -q 'no Runner.Worker above this hook' <<<"$out" || { printf '%s\n' "$out"; exit 1; }
  bash -n "$tmp/hook.sh" && bash -n "$tmp/fork-fleet-launch.sh"
  out=$(python3 -I "$tmp/fork-fleet-desk.py" 2>&1 || true)   # the desk link loads its wire format
  grep -q 'usage: serve POOL' <<<"$out" || { printf '%s\n' "$out"; exit 1; }
  install -d -m 755 /opt/fork-fleet
  for f in $files; do
    IFS=: read -r src dst mode <<<"$f"
    install -o root -g root -m "$mode" "$tmp/$dst" "/opt/fork-fleet/$dst"
  done
  echo "$sha" > /opt/fork-fleet/SOURCE
)
python3 -I /opt/fork-fleet/fork-fleet.py --config /opt/fork-fleet/fork-fleet.json gate </dev/null; echo "exit $?"
# ^ expect an ::error:: line, "no Runner.Worker above this hook" and exit 1: the gate refuses outside a runner

# 4. The launcher's settings and the dispatcher's token (root only).
install -d -m 700 /etc/fork-fleet
printf 'RUNNER_USERS=fork-runner-1,fork-runner-2\n' > /etc/fork-fleet/launch.conf
chmod 600 /etc/fork-fleet/launch.conf
( umask 077; read -rsp 'token: ' t; printf '%s\n' "$t" > /etc/fork-fleet/token ); echo
# RUNNER_USERS: one user per slot of the pool (step 1). WORK_ROOT (/var/lib/fork-fleet/runners): the launcher makes
# it 0711. Every directory above it must let the runner users through (o+x), or no runner can reach its copy.
# WORK_ROOT, HOOK and RESOLV_CONF are bound into every job: never under /home, /root, /mnt, /media or /srv (refused,
# also through a symlink). A bind mount is not seen (it has no link to resolve): never bind-mount a data folder at
# one of them, or at a folder above one (prune also deletes runner copies in WORK_ROOT). The hook's folder must be
# root's and writable by root alone (start refuses a shared one such as /var/tmp), and every symlink on the way to
# these paths root's, in a folder of root's; every folder on the way (`/` too) root's, and only its owner may write
# it (or sticky, and then the folder below it root's or the launcher user's; not group-writable), a plain folder as
# much as a link's.
# NET_BASE (10.204), RESOLV_CONF (/etc/fork-fleet/resolv.conf), TEMPLATE (/opt/fork-fleet/template/runner-template)
# and RUNNER_MIN (none: the oldest runner version refresh may install): add a line only to change one (step 1b).
# OWN_ADDRESS_URLS (https://api.ipify.org,https://checkip.amazonaws.com): where this host's public IPv4 address is
# looked up before every start and every dispatcher pass; every address any of them gives is blocked, and no answer
# at all starts nothing. Add a line only to use other https lookups. OWN_ADDRESS_SECONDS (120): a dispatcher pass
# reuses the last answer for that long (0: it looks up on every pass).
# ALLOW_PATHS (empty): more paths jobs see read-only, comma-separated (a tool cache); never one that holds data
# (/home, /root, /mnt, /media, /srv are refused, also through a symlink), never under a folder others (its group or all) may write.
# JOB_PATHS (fork-fleet-job-paths.txt beside the launcher): the system paths every job sees; leave it.

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

# 6. Prove the runners' network, from inside a runner: check-net runs a probe as a free runner user, in the very
#    sandbox a runner gets. The URL (public) must answer; each HOST:PORT must be closed from inside. The targets are
#    every IPv4 service this host listens on, from each of its addresses: its loopback services, its LAN address,
#    its Tailscale (100.64.0.0/10) address, its Docker bridges. check-net refuses a target the host itself does not
#    reach (its refusal inside would prove nothing): if a listener just closed, run it again. Expect "ok" on every
#    line and exit 0. Run it again after any network change on the host, and whenever a new service listens.
#    It also proves, inside, that this host's own public address is refused, that AF_VSOCK, io_uring and 32-bit
#    system calls are closed, that only the pseudo devices are there, only the allowed mounts, none that receives
#    the host's later mounts, nothing in /mnt and /media, and no socket of the host's ("host:" lines first say which
#    of these this host has outside: where it has none, the refusal inside shows nothing there).
mapfile -t addrs < <(ip -4 -o addr show | awk '{ sub("/.*", "", $4); print $4 }')
mapfile -t targets < <(ss -4ltnH | awk -v addrs="${addrs[*]}" '{
    n = split($4, hp, ":"); h = hp[1]; p = hp[n]; sub(/%.*/, "", h)
    if (h == "0.0.0.0" || h == "*") { k = split(addrs, a, " "); for (i = 1; i <= k; i++) print a[i] ":" p } else print h ":" p
  }' | sort -u)
/opt/fork-fleet/fork-fleet-launch.sh check-net https://github.com "${targets[@]}"; echo "exit $?"

# 7. The runner template and its hourly refresh. `refresh` reads the newest actions/runner release, downloads its
#    tarball, checks the release's sha256 before unpacking, unpacks it beside the template and swaps the two in one
#    rename under the launch lock: running jobs keep their copies, and a cut download, a bad checksum or any other
#    failure leaves the old template. The same version: nothing (one API call). Safe to run by hand at any time (a
#    second refresh waits for the first). The timer runs it every hour at a random minute in the first 10, and after
#    boot when a run was missed; a failed run is tried again the next hour. The service is sandboxed: it writes only
#    the template's own folder /opt/fork-fleet/template (with TEMPLATE elsewhere, add a drop-in: the unit file says
#    how); /run/fork-fleet, the root launcher's, is read-only to it (it opens the launch lock the dispatcher's first
#    prune made: right after a boot, before that, it says so and the next hour's run goes on). It reaches the network
#    by IP only, and has 1 GiB of memory, one CPU and 64 tasks.
#    refresh refuses a template whose folder holds anything else (such as the launcher: root runs it).
#    The tarball is treated as hostile even after its sha256 passed (a compromised release would pass it): refresh
#    refuses absolute names, "..", links that lead outside, device files, FIFOs, sockets and sparse files, a tarball
#    over 4 GiB or 100000 entries, headers over 64 KiB, names over 4096 and links that take too long to follow; it
#    strips setuid, setgid and group/other write. `start` refuses such files in a template too.
#    Versions: only forward, and by at most one major version at a time (a bigger jump is refused: no real release
#    does that). A template NEWER than the newest release (one GitHub withdrew) is rolled back to it by the next run,
#    but never below the version it replaced (.fork-fleet-floor; without one, one major version down) nor below
#    RUNNER_MIN (step 4). After a rollback the dispatcher may still hold the withdrawn version for up to an hour (it
#    reads the newest release hourly): the pool stays closed until its next read.
#    Roll back further or jump by hand (the pool is closed in between; running jobs keep their copies):
#      rm -rf --one-file-system /opt/fork-fleet/template/runner-template && /opt/fork-fleet/fork-fleet-launch.sh refresh
#    Re-run the two install lines after step 3 brings new unit files.
#    A host set up before the template had its own folder (TEMPLATE=/opt/fork-fleet/runner-template in launch.conf):
#    refresh refuses it ("not only the template") until it is moved, once; start and ready keep working meanwhile.
#    Under the launch lock, so no runner copies it half-way (a start in that second fails once and is started again).
#    Safe to run again, also after it stopped half-way (the move only while the old one is there), and it removes the
#    temp folders a refresh killed under the old layout left beside the launcher:
#      install -d -m 755 /opt/fork-fleet/template && install -d -m 700 /run/fork-fleet && flock /run/fork-fleet/launch.lock sh -c '{ [ ! -e /opt/fork-fleet/runner-template ] || mv -T /opt/fork-fleet/runner-template /opt/fork-fleet/template/runner-template; } && sed -i /^TEMPLATE=/d /etc/fork-fleet/launch.conf && rm -rf --one-file-system /opt/fork-fleet/.runner-template.refresh.*'
/opt/fork-fleet/fork-fleet-launch.sh refresh                   # the first template (and its folder), or the newest one now
/opt/fork-fleet/template/runner-template/bin/installdependencies.sh   # once, and again if a release needs new libraries:
                                                               # refresh installs no system packages
install -o root -g root -m 644 /opt/fork-fleet/fork-fleet-refresh.service /etc/systemd/system/t3code-fleet-refresh.service
install -o root -g root -m 644 /opt/fork-fleet/fork-fleet-refresh.timer /etc/systemd/system/t3code-fleet-refresh.timer
systemctl daemon-reload && systemctl enable --now t3code-fleet-refresh.timer
systemctl list-timers t3code-fleet-refresh.timer               # its next run
journalctl -u t3code-fleet-refresh -n 20                       # what each refresh did
```

Do not name the dispatcher's unit `fork-fleet-*`: that prefix is for runner
units. Runner units are separate transient units, so restarting the dispatcher
leaves running jobs alone.

**The `pc` pool** is a desk pool: the owner's PC, through the desk link below.
It stays off until its own dispatcher unit runs (`t3code-fleet-dispatcher-pc`,
"One dispatcher per desk pool") and the PC's desk key is added: without its
dispatcher, `pc` has no heartbeat and `route` never counts it. While the PC
sleeps or is off, `launch` exits 3 at once and its jobs go elsewhere. A host
that ran an older `pc` dispatcher on `/opt/fork-fleet/launch-pc` first stops
that dispatcher and lets its runners finish (this config's cleanup skips a pool
that is down, so it never stops or prunes them); then it may delete the
wrapper: nothing reads it.

### The desk link (a desktop pool)

A desktop accepts no connection and holds no GitHub token. Its desk agent dials
out to the always-on host over ssh, as a dedicated unprivileged user (default
`fleet-desk`, never root), with a key that may only run `serve` for its own
pool. The pool's dispatcher on the always-on host sends the launcher's verbs
through that one session (`fork-fleet-desk.py`, its wire format
`fork-fleet-desk-wire.py`). Once per always-on host, as root (step 3 installs
these files and the key tool `fork-fleet-desk-key.py`):

```sh
# The desk-link user: a real shell (sshd runs the forced command through it, so nologin would close the link) and no
# password ('*': none, but not locked; a locked '!' account can be refused by sshd). Its key lines limit it.
useradd --system --create-home --home-dir /var/lib/fleet-desk --shell /bin/sh fleet-desk
usermod -p '*' fleet-desk
# The socket directory, at every boot: owned by that user, its group is the socket group (who may connect).
echo 'd /run/fork-fleet-desk 0710 fleet-desk fleet-desk -' > /etc/tmpfiles.d/fork-fleet-desk.conf
systemd-tmpfiles --create /etc/tmpfiles.d/fork-fleet-desk.conf
# Per desk pool: the desktop's public key, as the desk-link user, with the key tool fork-fleet-desk-key.py
# (step 3 installs it).
sudo -u fleet-desk -H /usr/bin/python3 -I /opt/fork-fleet/fork-fleet-desk-key.py --config /opt/fork-fleet/fork-fleet.json add POOL < desk-POOL.pub
```

The line it writes (and the only kind of line that user's key file holds) is
`restrict,command="/usr/bin/python3 -I /opt/fork-fleet/fork-fleet-desk.py serve POOL" ssh-ed25519 AAAA... fork-fleet-desk:POOL`
in `~fleet-desk/.ssh/authorized_keys`. The pool's launch command in the
project's fleet config is
`"launch": ["python3", "-I", "/opt/fork-fleet/fork-fleet-desk.py", "launch", "POOL"]`.
Another user name: use it in all three places above, and give the dispatcher
`Environment=FORK_FLEET_DESK_USER=NAME`. Another socket group: the group in the
tmpfiles line; the desk-link user must be in it, and so must the dispatcher's
user when that is not root.

A desk pool's entry in a project's fleet config (made-up names; `seeds` is
optional, see "Seeds" below):

```json
{
  "name": "desktop",
  "label": "fleet-desktop-example",
  "slots": 2,
  "beacon": "FLEET_BEACON_DESKTOP",
  "launch": ["python3", "-I", "/opt/fork-fleet/fork-fleet-desk.py", "launch", "desktop"],
  "waits": false
}
```

`waits: false`: a desktop may sleep, so a job never waits for it. `slots` at
most the desktop's own `slots` (its machine file). The pool's name is the one in
the desktop's machine file and in the key line.

`serve` keeps one session per pool (a lock), listens on a 0660 socket in
`/run/fork-fleet-desk` (it lets in only root, its own user and the socket
group), and pings the agent; `launch` fails at once (exit 3) when no session is
live, so a sleeping desktop's pool is down for the pass and `route` sends its
jobs elsewhere. The dispatcher says so once, then every 10 minutes with a count,
and once more when the desktop answers again after more than one pass (a single
missed pass logs nothing when it answers). A verb with no answer in its time
exits 75: it may or may not have run, and the next pass's `active` tells.

**One dispatcher per desk pool.** Run each desk pool's dispatcher as its own
unit (a copy of step 5's unit named `t3code-fleet-dispatcher-POOL`, with
`dispatch --pool POOL`), never in the vm pool's unit: one dispatcher serves its
pools one after another, and a desktop that answers each verb just inside its
time could hold a pass for minutes, so the vm pool would wait behind the least
trusted machine. A second dispatcher for a pool that one already serves refuses
to start (the lock `/run/fork-fleet/dispatch-POOL.lock`) and its unit keeps
failing every 30 s: stop it.

**From an older desk link** (serve as root, the key in root's
authorized_keys): remove that key line and `PermitRootLogin
forced-commands-only`, do the steps above, and restart the host or remove the
old `/run/fork-fleet-desk` first (`serve` refuses a lock file or directory left
by root, with a message that says so). A desk agent of an older wire version
gets exit 76 and a line that says to upgrade it.

**The desktop's side** (`fork-fleet-desk-agent.py`, as root on the desktop; on
WSL2 inside the distro, which runs systemd). Install it and
`fork-fleet-desk-wire.py` from the same pinned commit as step 3, beside the
launcher and the walls (`fork-fleet-walls.py`) in `/opt/fork-fleet`; the
launcher and its setup are as on a vm-pool host. The agent runs the launcher
beside the walls (one kit: the one those walls read), so `desk.conf` needs no
`LAUNCH` line; an old one that names another launcher makes the agent refuse
to run (exit 2) until the line is gone. The agent fixes the kit when it starts,
in its real path: a link above it that an upgrade repoints changes nothing
until the agent restarts, so restart it after an upgrade. The desk kit
([fork-kit/desk](../fork-kit/desk/README.md)) does all of this, with a PASS/FAIL
check and an uninstall; by hand, once per desktop (`/etc/fork-fleet` root's and
0700, not a link: the kit's `check` and install hold it to that):

```sh
install -d -m 700 /etc/fork-fleet
ssh-keygen -t ed25519 -N '' -C desk-POOL -f /etc/fork-fleet/desk-key   # desk-key.pub goes to the always-on host
# The always-on host's own key, from that host (`cat /etc/ssh/ssh_host_ed25519_key.pub`), as one line;
# with another PORT the name is [HOST]:PORT. Nothing else is ever trusted.
echo 'HOST ssh-ed25519 AAAA...' > /etc/fork-fleet/desk-known_hosts
printf 'POOL=POOL\nHOST=HOST\n' > /etc/fork-fleet/desk.conf && chmod 600 /etc/fork-fleet/desk.conf
python3 -I /opt/fork-fleet/fork-fleet-desk-agent.py check
install -m 644 fork-fleet-desk-agent.service /etc/systemd/system/
systemctl daemon-reload && systemctl enable --now fork-fleet-desk-agent
```

The agent checks each verb again against the wire rules (anything else ends
the session), runs `start` only after `fork-fleet-walls.py check` passes, gives
the launcher and the walls only `PATH` and `LC_ALL` (they read their own
settings files), kills a verb at its limit (answer 75) and every running verb
when the session ends, and dials again with backoff and jitter (at once after
a sleep, also on WSL2, where only the wall clock jumps). One agent per desktop
(a lock in `/run/fork-fleet-desk-agent`); the other settings and their
defaults are in the script's header. `check-net` (step 6) never comes over the
link, so the always-on host cannot use it to probe the desktop's own network:
run it by hand on the desktop.

**What jobs need on the host.** The jobs run without sudo, so the host must
already have:

- `git`, `curl`, `unzip`, `python3` (3.8 or later, at `/usr/bin/python3`),
  `pkg-config` and a C/C++ toolchain;
- `libsecret-1-dev` (Build and Test check `pkg-config --exists libsecret-1`);
- `xvfb` (Build checks for `xvfb-run`);
- Chromium's system libraries for the browser tests
  (`npx playwright install-deps chromium`, once, as an admin).

Bun, Node and Rust are installed per job by the setup actions, into the job's
own home, which goes with its copy. Leave a few tens of GB of disk for
`/var/lib/fork-fleet/runners`.

#### Seeds (warm caches for a desk pool)

A job's copy starts empty, so each job would download its tools and
dependencies again. A desk pool may list **seeds** in its fleet config entry,
ready-made caches the desktop keeps and its jobs start from:

```json
"seeds": [{"name": "gradle", "every_hours": 6, "max_age_hours": 72, "max_mb": 4096}]
```

`every_hours` is how often a desktop takes a new one, `max_age_hours` (at
least `every_hours`) the age, from when it was made, at which it is dropped,
`max_mb` its largest size (at most 16384). Nothing in the fleet makes a seed: on the always-on
host the project's own timer or job writes a tar of a cache folder's contents
to `/var/lib/fork-fleet/seed-files/POOL/NAME.tar` (a folder per pool, so two
pools never share a file), by a temp file and a rename. A file changed in place
while it is sent (size, mtime or ctime) is refused; one replaced by a rename while
it is sent is fine (the old one is sent whole). A file dated more than five minutes in the future, older
than `max_age_hours` or above `max_mb` is refused. Both folders and the files are
root's (or the project's), readable by the desk-link user and writable by no one
else (the owner itself is not checked, only that no one else may write); never
put a secret in a seed: every job of the pool reads it. **Make a seed only from
trusted code (the default branch), never in a job that runs pull request code:**
every later job of the pool starts from it, so a poisoned seed reaches them all.
A file replaced by a rename while another hard link to the old one stays (a
backup copy, say) is still "in place" for this check and is refused (exit 4):
keep no second link to a seed file.

The desktop asks for them with the desk link's key, in an ssh session of its
own: the same forced command sends every command that starts with `seed` to
`fork-fleet-desk-seed.py` (which step 3 installs; its header has the exit
codes), never to a session. It answers `seeds` with the pool's list and
`seed NAME` with that one file, only a name of that list, and a trailer with its
size and SHA-256; anything else is refused. One stream per pool at a time; a
desktop that takes less than 64 KiB in two minutes, or not the whole file within
ten minutes plus one second per 256 KiB, is cut off and the next one may go.
So a link must carry at least about 256 KiB a second (2 Mbit/s) for a large
seed: at 100 KiB a second, a seed above about 96 MiB is always cut off. Size
`max_mb` to the desktop's link. A desktop just fast enough not to stall can hold
its pool's lock for a long time (16 GiB at 256 KiB a second is about 18 hours);
only that pool's own seed streams wait, nothing else. The host does not limit
how often a desktop asks: the key belongs to the pool's own desktop, one stream
per pool runs at a time, and each is cut off at its deadline, so the most it
costs is one file's reads and upload at a time.

The desktop's side is the same script's `run`, hourly from
`t3code-fleet-seed.timer` (the desk kit installs both). It takes only the
names its own installed fleet config lists for its pool, and holds them to
its own `every_hours` and `max_age_hours` and the smaller of its own and the
host's `max_mb`: a host cannot make a desktop take more. Each new seed is
unpacked with Python's tar data filter and a limit on what it would make on
disk (each member's size plus 4 KiB for it and for each folder it makes, all
within `max_mb`; a size below 0, or a size on a member that is not a file, is
refused; at most 200,000 members and folders; no member more than 64
folders deep or with a name over 1 KiB) and on memory (members are let go as
they are unpacked; the extended pax or long-name headers before one member, and
the global ones of the whole tar, at most 1 MiB; sparse members are refused, so
make a seed without `--sparse`; a hard link must name a file unpacked before
it, and a link whose path is already there, or that cannot be made, is refused,
never copied from its target, which tarfile would look for in the rest of the
stream), checked against the trailer (its size
and date must be the list's too), checked again for links that lead out of
it, made readable by every job (a cache's 0600 files too), and swapped in by a
rename; ssh's stderr and the list are read with a limit. A seed that fails in
any way is named in the log and the run goes on with the next; old copies
are pruned at the start and the end of every run (so a run killed by its unit's
memory or time limit cannot pile them up). A run fetches for at most two hours
in all (the unit allows three), each seed at most 30 minutes. A seed torn again
and again is tried again after about 1, 2, 4, 8, 16 and then every 24 hours (a
link too slow for it does not spend every hour on it; a fetch that kills the
run counts as torn; the times are approximate: the timer's 10 minute random
delay and a 5 minute margin can make a wait up to an hour longer); another
copy on the host is tried at once, but only once per wait (the try counts, so a
host that remakes the seed every hour still meets the back-off). One torn on
every try for more than a day makes the run exit 1, so `desk-install check` shows FAIL until it is
fetched (`/var/lib/fork-fleet/seeds/.torn.json` names it and why); so does a
run that cannot write that file (a full disk). A host that gives no list (down,
an older kit, a half-done install) means no new seeds: each one here stays until
its age drops it, the torn record is left as it is, and the run exits 0 (a host
down for good shows in `desk-install check` only as seeds that age out). A
desktop whose own fleet config is broken or lacks its pool, or whose Python's
tarfile lacks the private parts the limits above rely on, fetches nothing and
exits 2 (`desk-install check` shows the seed service's last result), but still
drops each seed past the `max_age_hours` it was fetched with. Jobs see
`/var/lib/fork-fleet/seeds/NAME` read-only (the launcher's `ALLOW_PATHS`;
the unpacking and old copies beside it, `.new` and `.v`, are visible too and
hold the same data), copy what they need from it when it is there, and run
cold when it is not. A replaced copy is kept 24 hours for a job still reading
it and removed by the next hourly prune, so it lives up to about 25 hours;
there is no free-space check before unpacking (a full disk ends that seed's
fetch and keeps the old one). **Disk budget per seed:** up to `max_mb` x
(25 / `every_hours`, rounded up, + 2): the copy in use, the copies replaced in
the last 25 hours, and one being unpacked (`every_hours` 1 and `max_mb` 4096:
up to about 108 GiB; `every_hours` 6: about 28 GiB). Size `max_mb` and `every_hours` to the
desktop's free disk. The state table (a torn download, the host down,
clocks that disagree, a run that stopped half-way) is in the script's header.

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

Set it up on the workspace machine next to the real install, with its own paths:

- Install the same five files under `/opt/fork-fleet-proof/`. In its
  `fork-fleet.json`, set `repository` to the proof repository, keep one pool
  `proof` with label `fleet-proof-t3code`, `slots: 1`, `waits: true`, beacon
  `FLEET_BEACON_PROOF` and launch
  `["/usr/bin/env", "FORK_FLEET_LAUNCH_CONF=/etc/fork-fleet-proof/launch.conf", "/opt/fork-fleet-proof/fork-fleet-launch.sh"]`,
  and one job `{"job": "lint", "memory_mb": 1024}`.
- In `/etc/fork-fleet-proof/launch.conf`, set `RUNNER_USERS=fork-runner-1,fork-runner-2`,
  `TEMPLATE=/opt/fork-fleet/template/runner-template`,
  `WORK_ROOT=/var/lib/fork-fleet-proof/runners`, `RUN_DIR=/run/fork-fleet-proof`
  and `HOOK=/opt/fork-fleet-proof/hook.sh`.
- Store a token for the proof repository at `/etc/fork-fleet-proof/token`, with
  the same permissions as above.
- Run the dispatcher by hand when a step says so:
  `python3 -I /opt/fork-fleet-proof/fork-fleet.py --config /opt/fork-fleet-proof/fork-fleet.json dispatch --token-file /etc/fork-fleet-proof/token --lock-dir /run/fork-fleet-proof`.

The proof repository's `.github/workflows/fork-tests.yml` needs two jobs. The
first writes `labels=["fleet-proof-t3code","fleet-proof-t3code-lint-<16 hex>-a${{ github.run_attempt }}"]`
with a random hex. The second, `lint`, has
`runs-on: ${{ fromJSON(needs.<first>.outputs.labels) }}` and these steps:

- `echo job ran`;
- a step that must fail. It tries to write the template through its copy:
  `! sh -c 'echo x >> "$GITHUB_WORKSPACE/../../../run.sh"' && ! touch "$GITHUB_WORKSPACE/../../../bin/proof"`;
- an `if: always()` step that runs `touch "$HOME/fleet-proof-always-ran"` (the
  home is in the runner's copy, which stays until the dispatcher prunes it).

Then check each of these:

1. **A trusted push runs.** Push to `main` with the dispatcher running. The job
   log shows `fork-fleet: allowed`, and the template step passes, so the runner
   user cannot write the template. While the job runs, `ls /run/fork-fleet-proof/`
   shows no `.env` file: the environment file is gone once the runner started.
   Within a minute after the job, `systemctl list-units 'fork-fleet-*'` shows no
   proof unit, and the dispatcher has pruned the copy (its home with it).
2. **A refused job runs no `always()` step.** Stop the dispatcher. Push again, so
   `lint` queues. Start a runner by hand that is made for another run, with the
   job's two labels from its log (the launcher picks a free runner user):

   ```sh
   jit=$(gh api -X POST repos/OWNER/PROOF/actions/runners/generate-jitconfig -f name=fleet-proof-t3code-1 \
     -F runner_group_id=1 -f 'labels[]=fleet-proof-t3code' -f 'labels[]=LABEL_FROM_THE_JOB' -f work_folder=_work \
     --jq .encoded_jit_config)
   printf 'ACTIONS_RUNNER_INPUT_JITCONFIG=%s\nFORK_FLEET_RUN=1\nFORK_FLEET_ATTEMPT=1\n' "$jit" |
     FORK_FLEET_LAUNCH_CONF=/etc/fork-fleet-proof/launch.conf /opt/fork-fleet-proof/fork-fleet-launch.sh start fleet-proof-t3code-1
   unset jit
   ```

   The job is failed, none of its steps ran, and
   `ls /var/lib/fork-fleet-proof/runners/fleet-proof-t3code-1/home/` shows no
   `fleet-proof-always-ran`. The live log shows the
   gate's `this runner was made for run 1`. The saved log may lack it, because
   the worker died. Start the dispatcher again: it prunes the copy.

3. **A bare label finds no runner.** A second workflow, not in `trusted.workflows`,
   with `runs-on: [fleet-proof-t3code]`, stays queued, and the dispatcher starts
   nothing for it. Push to `main` while it waits: the dispatcher holds that run's
   `lint` back and logs which job blocks it. Cancel both.
4. **A re-run gets a runner of its own attempt.** Re-run only the `lint` job of
   the first run, as the owner. Its label ends in `-a2`, it gets a new runner,
   and the gate logs `attempt 2`.
5. **A cancelled run's idle runner takes no re-run.** Stop the dispatcher. Push,
   so `lint` queues with a label ending in `-a1`, and cancel the run. Start a
   runner by hand for it, as in check 2, but with `FORK_FLEET_RUN=<that run>`
   and the job's `-a1` label: it stays idle, its job is gone. Re-run the run as
   the owner. The new `lint` asks for `-a2` and stays queued: the idle runner
   never takes it. Start the dispatcher: it starts a runner for attempt 2, which
   passes, and stops the idle one.
6. **No slot reaches the other.** Set the proof pool to `slots: 2` and restart
   the dispatcher. Give `lint` one more step before the others:
   `echo leftover > "$HOME/planted"; echo leftover > /dev/shm/planted-$RANDOM; (setsid sleep 3000 &); sleep 120`,
   and before it `! ls /dev/shm/planted-* && ! ls "$HOME/planted"` and the
   network check `curl -fsS -o /dev/null https://github.com && ! timeout 5 bash -c 'exec 3<>/dev/tcp/127.0.0.1/3773' && ! timeout 5 bash -c 'exec 3<>/dev/tcp/LAN_ADDRESS/22'`
   with the host's LAN address for `LAN_ADDRESS` (the public internet answers;
   T3 on the host's loopback and the host's LAN address do not). Push to
   `main`, then push again within a minute, so two runs hold both slots. While
   both sleep, as root:
   `systemctl show -p User,WorkingDirectory 'fork-fleet-*'` shows two different
   users, and `setpriv --reuid=fork-runner-1 --regid=fork-runner-1 --clear-groups ls <fork-runner-2's WorkingDirectory>`
   fails with "Permission denied" (and the other way round). After both jobs,
   push again: the new job's first check passes (no file from the earlier job in
   its home or `/dev/shm`, which is the unit's own), and while it runs that check, `pgrep -af 'sleep 3000'`
   prints nothing. Set `slots: 1` again.

Delete the proof install (`/opt/fork-fleet-proof`, `/etc/fork-fleet-proof`,
`/var/lib/fork-fleet-proof`) and the proof repository's runners afterwards.

## Failure table

The question for each row: what happens when this runs again, stops half-way,
or meets existing, old or empty state? GitHub (runs, jobs, runner registrations,
the heartbeat variables) and the host (systemd units, copies) hold all state. The
dispatcher keeps only pacing in memory, so a restart starts over cleanly.

| Part        | Situation                                                                                                                     | What happens                                                                                                                                                                                                                                                                                              |
| ----------- | ----------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Setting     | `CI_RUNNERS` unset or empty                                                                                                   | `auto`.                                                                                                                                                                                                                                                                                                   |
| Setting     | Misspelled                                                                                                                    | `Route` fails, so the run is red, and the message says why. Fix the variable and re-run.                                                                                                                                                                                                                  |
| Route       | No heartbeat, an old one (over 180 s) or a garbled one                                                                        | That pool counts no free slot. `auto`: GitHub-hosted, with a note (a `::notice::` on the run page when the pool has a heartbeat at all). `fleet`: queue on `vm`.                                                                                                                                          |
| Route       | A pool its dispatcher closed or reports down (a new actions/runner release, a host failure)                                   | A `::notice::` on the run page names the pool and the reason, so `auto` never goes GitHub-hosted unseen for days. `auto`: GitHub-hosted. `fleet`: queue on `vm`.                                                                                                                                          |
| Route       | The `changes` outputs cannot be read                                                                                          | Every routed job counts as running, with a warning.                                                                                                                                                                                                                                                       |
| Route       | Untrusted run (fork's pull request, bot, other actor, release run, the upstream sync's push)                                  | GitHub-hosted in every mode. A release run skips `Route` and is GitHub-hosted.                                                                                                                                                                                                                            |
| Route       | A push to `main` of a commit tagged `fork-follow/synced/<sha>` (the upstream sync moved `main` to it)                         | Untrusted: GitHub-hosted in every mode; the dispatcher makes no runner for it and the gate refuses it. When the tag cannot be read (GitHub or git fails), the run is untrusted too.                                                                                                                       |
| Route       | The sync stops between its tag and `main` (killed, the `main` push refused)                                                   | The tag names a commit `main` does not have yet: harmless. The next sync tags it again (a no-op) and then moves `main`. A sync whose tag push fails does not move `main` and is red.                                                                                                                      |
| Route       | Re-run of failed jobs                                                                                                         | `route` does not run again. `auto` (attempt differs): GitHub-hosted. `fleet`: the old labels, while the setting is still `fleet`. `github` now: GitHub-hosted.                                                                                                                                            |
| Route       | Re-run of all jobs                                                                                                            | `route` runs again with a new label: fleet as in a new run if the owner re-ran it, GitHub-hosted if the sweep or someone else did.                                                                                                                                                                        |
| Route       | Re-run of a run from before this change                                                                                       | That commit's workflow has no `Route`: GitHub-hosted.                                                                                                                                                                                                                                                     |
| Watch       | A job waits past the bound                                                                                                    | The run is cancelled with an error naming the job. `auto`: the sweep re-runs it GitHub-hosted within 10 minutes.                                                                                                                                                                                          |
| Watch       | A routed job never shows up (a matrix job skipped)                                                                            | After 2 minutes, the watch counts only the jobs it saw.                                                                                                                                                                                                                                                   |
| Watch       | GitHub truncated, garbled or failed a reply                                                                                   | A warning. It tries again in 30 s, and after the bound it stops watching without turning red. The sweep is the fallback.                                                                                                                                                                                  |
| Watch       | Re-run attempt                                                                                                                | It watches its own attempt's jobs only.                                                                                                                                                                                                                                                                   |
| Sweep       | Stops half-way (cancelled, did not re-run)                                                                                    | The next sweep finds the cancelled run, whose fleet job waited 10 minutes and never started, and re-runs it.                                                                                                                                                                                              |
| Sweep       | A cancel that takes over 2 minutes                                                                                            | It is left for the next sweep.                                                                                                                                                                                                                                                                            |
| Sweep       | Runs again on a run it re-ran                                                                                                 | The re-run's triggering actor is `github-actions`, so the run is untrusted and left alone. Each run is re-run at most once.                                                                                                                                                                               |
| Sweep       | Two sweeps at once (manual and cron)                                                                                          | The concurrency group runs them one at a time.                                                                                                                                                                                                                                                            |
| Sweep       | A stranger's run with fleet jobs                                                                                              | Left alone: never cancelled or re-run.                                                                                                                                                                                                                                                                    |
| Sweep       | A person cancels a run                                                                                                        | Stands if a fleet runner had the job. A run cancelled while a fleet job had already waited 10 minutes is re-run, as the sweep would have done.                                                                                                                                                            |
| Sweep       | Branch moved on or deleted                                                                                                    | Not re-run.                                                                                                                                                                                                                                                                                               |
| Sweep       | Runner lost mid-job (VM restart)                                                                                              | `auto`: re-run GitHub-hosted. `fleet`: stays red, and the owner re-runs it.                                                                                                                                                                                                                               |
| Sweep       | A test fails on a fleet runner                                                                                                | Red, as on GitHub-hosted. Not re-run.                                                                                                                                                                                                                                                                     |
| Sweep       | GitHub trouble on one run                                                                                                     | A warning, the sweep fails, and the next one tries again.                                                                                                                                                                                                                                                 |
| Sweep       | `fleet` mode, a job waits 2 hours                                                                                             | The run is cancelled with an error, even a re-run of failed jobs that has no watch. Not re-run.                                                                                                                                                                                                           |
| Dispatcher  | Crash after `generate-jitconfig`, before `start`                                                                              | An offline registration with no unit. The next pass deletes it, and the pass after starts the job's runner again (at most 3 tries per job, then it is left to the watch and the sweep).                                                                                                                   |
| Dispatcher  | Crash after `start`                                                                                                           | The unit is there. The next pass sees it and starts nothing twice.                                                                                                                                                                                                                                        |
| Dispatcher  | Launcher killed half-way through copying the template                                                                         | No unit. The next pass deletes the registration, `prune` removes the half copy and any environment file, and the job's runner is started again.                                                                                                                                                           |
| Dispatcher  | A pool's label changed: offline registrations under its old label                                                             | Deleted when the pool lists the old label in `retired` (and `retired_labels`); never online ones or another pool's. An idle runner there is stopped like any other. A label two pools retire, or a pool's label in either list, is refused. Without `retired` they stay (delete them by hand).            |
| Dispatcher  | An idle runner under a pool's old label whose unit is on another host                                                         | Left alone: it holds no slot here, and `stop` would fail on every pass (#342).                                                                                                                                                                                                                            |
| Dispatcher  | Restart, or a host reboot                                                                                                     | It reads everything again from GitHub and systemd. The lock directory under `/run` is made again after a boot. After a reboot, a job that was running fails as a lost runner (`auto`: the sweep re-runs it GitHub-hosted). A job still queued has its offline registration deleted and gets a new runner. |
| Dispatcher  | Two dispatchers for one pool                                                                                                  | The second one fails on that pool's lock (`dispatch-<pool>.lock`). Two dispatchers for different pools both run.                                                                                                                                                                                          |
| Dispatcher  | The job went away before its runner took it (cancelled)                                                                       | The idle runner is stopped after 3 passes, or at once if another queued job asks for all of its labels. A re-run's job never can: its label names its own attempt.                                                                                                                                        |
| Dispatcher  | A queued job whose label names another attempt than its run                                                                   | No runner: the gate would refuse it on any runner. The watch or the sweep handles it.                                                                                                                                                                                                                     |
| Dispatcher  | Re-run between reading the run and its jobs                                                                                   | Skipped for one pass, then a runner is made for the new attempt.                                                                                                                                                                                                                                          |
| Dispatcher  | The PC or VM has no memory                                                                                                    | No start, and the heartbeat shows 0 free slots. `auto`: new runs go GitHub-hosted.                                                                                                                                                                                                                        |
| Dispatcher  | GitHub fails during a pass                                                                                                    | A warning. The pass stops, and the next one, 20 s later, starts over.                                                                                                                                                                                                                                     |
| Dispatcher  | One pool's host fails (launcher error, cannot be run)                                                                         | That pool is left alone for the rest of the pass: no cleanup, no start, heartbeat `free: 0` with the reason. Other pools go on.                                                                                                                                                                           |
| Dispatcher  | The runner template is older than the newest release, or has no `.fork-fleet-version`                                         | No runner starts on that pool; cleanup goes on. Heartbeat `free: 0` with a `why` that says to refresh the template (host step 7: the timer does it within about an hour). `auto`: GitHub-hosted.                                                                                                          |
| Dispatcher  | Token file missing, readable by anyone but root, or empty                                                                     | It refuses to start. `systemctl status` shows why.                                                                                                                                                                                                                                                        |
| Dispatcher  | Down for a while                                                                                                              | Heartbeats go stale after 180 s. `auto`: GitHub-hosted. `fleet`: jobs queue, and the watch reports after 2 hours.                                                                                                                                                                                         |
| Gate        | Run, attempt or trust rule does not match                                                                                     | `Runner.Worker` is killed before the first step: the job fails and no `always()` step runs.                                                                                                                                                                                                               |
| Gate        | Broken installed config                                                                                                       | It refuses every job, failing closed. The dispatcher stops too. Re-install from `main`.                                                                                                                                                                                                                   |
| Gate        | Installed script that does not even start (a syntax error)                                                                    | The dispatcher fails on start too, so no runner starts. Restart the dispatcher after each re-install, so it never runs an older copy than the hook.                                                                                                                                                       |
| Gate        | No `FORK_FLEET_RUN` (a runner not made by the dispatcher)                                                                     | Refused.                                                                                                                                                                                                                                                                                                  |
| Launcher    | Template configured (`.env` and the like, even a symlink to nothing), a symlink, or writable by others                        | It refuses to start a runner, and the dispatcher logs the reason.                                                                                                                                                                                                                                         |
| Refresh     | The template's folder holds anything else (a host set up before it had a folder of its own)                                   | Refused before any download; `start` and `ready` keep working. Host step 7 gives the one-time move.                                                                                                                                                                                                       |
| Refresh     | The newest release is below the template's floor (`.fork-fleet-floor`, or one major version down) or `RUNNER_MIN`             | Refused; the template stays. Host step 7 says how to take it by hand.                                                                                                                                                                                                                                     |
| Refresh     | A hostile tarball: fanned-out symlinks, a huge long-name or pax header, a sparse file, a name over 4096                       | Refused quickly, before the header is read into memory. The service also has `MemoryMax=1G`, `CPUQuota=100%`, `TasksMax=64`.                                                                                                                                                                              |
| Dispatcher  | Template rolled back (a withdrawn release)                                                                                    | The dispatcher may still hold the withdrawn version for up to an hour: no runner starts on that pool until its next hourly read. Heals by itself.                                                                                                                                                         |
| Launcher    | `launch.conf` or the hook writable by anyone but root                                                                         | It refuses.                                                                                                                                                                                                                                                                                               |
| Launcher    | Bad input (an extra line, a key twice, a malformed value)                                                                     | It refuses before touching anything.                                                                                                                                                                                                                                                                      |
| Launcher    | A name that ran already                                                                                                       | It refuses. `prune` removes the old copy once its unit is gone.                                                                                                                                                                                                                                           |
| Launcher    | `systemd-run` fails                                                                                                           | The environment file is removed on exit. The copy stays until `prune`.                                                                                                                                                                                                                                    |
| Launcher    | A runner that never gets its job, or hangs                                                                                    | Its unit stops after 1 hour (`MAX_SECONDS`), and its whole process group is killed.                                                                                                                                                                                                                       |
| Launcher    | A job left a process (double fork, `setsid`), or files in its home, `/tmp` or `/dev/shm`                                      | `/tmp`, `/var/tmp`, `/dev/shm` and `/run` were the unit's own and went with it. Before its runner user is used again, every process of that user is killed and its old copies (home inside) are deleted. If a process or copy stays, the launcher refuses to start.                                       |
| Launcher    | The runners' network cannot be made (no `nft`, IPv4 forwarding off, no nameserver in `RESOLV_CONF`, an `ip` or `nft` failure) | It refuses to start a runner and says why; the dispatcher marks the pool down (`free: 0` with the reason). Host step 1b, then step 6.                                                                                                                                                                     |
| Launcher    | The `fork_fleet` table removed or changed while runners run                                                                   | The unit's `IPAddressDeny` still refuses the same ranges in the kernel. The next start makes the table again.                                                                                                                                                                                             |
| Launcher    | `RUNNER_USERS` names one user twice, root, a user in another group, or a lingering one                                        | It refuses.                                                                                                                                                                                                                                                                                               |
| Launcher    | Every runner user has a unit                                                                                                  | It refuses: no slot is free.                                                                                                                                                                                                                                                                              |
| Launcher    | `TEMPLATE` and `WORK_ROOT` on different filesystems                                                                           | It refuses, with a message: `cp -al` cannot hard-link across filesystems.                                                                                                                                                                                                                                 |
| Host setup  | Step 1 or 2 run again                                                                                                         | Step 1 keeps existing users and adds each deny line once. Step 2 stops the dispatcher, swaps the template with `mv -T`, and starts it again; a running job keeps its own copy.                                                                                                                            |
| Host setup  | Step 1b, 2 or 3 fails half-way (a download, the checksum, a check)                                                            | The step stops at that command (`set -euo pipefail`). Step 2 has not touched the running template or the dispatcher yet; step 3 has installed nothing and not written `SOURCE`.                                                                                                                           |
| Host setup  | Step 6 says a service is reachable from inside                                                                                | Do not use the pool: stop the dispatcher until step 6 passes.                                                                                                                                                                                                                                             |
| Old state   | Leftover copies or environment files from an old install                                                                      | `prune` removes any whose unit is gone, on every pass.                                                                                                                                                                                                                                                    |
| Old state   | Old `FLEET_RUNNERS_TOKEN` secret, or a standing runner from the first design (a `<host>-t3code` runner)                       | Unused. Delete both. A standing runner is what this design removes, because any job asking for its labels can reach it. The fork has no runners today.                                                                                                                                                    |
| Old state   | Installed copy older than `main`                                                                                              | It applies the old rule until re-installed (host step 3). `SOURCE` names the commit.                                                                                                                                                                                                                      |
| Empty state | No runs, no runners, no variables yet                                                                                         | The dispatcher creates the heartbeat variables on its first pass. `route` counts no slot until then.                                                                                                                                                                                                      |
