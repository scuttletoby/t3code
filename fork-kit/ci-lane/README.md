# ci-lane: the own CI lane, in shadow beside Actions

Issue #1157, phase 2 of the staged design (option C). The fork's own tests (`.github/scripts`, `fork-ci`,
`fork-kit`) already have a runner (`.github/scripts/fork-ci-tests.cjs`), a test list and per-test records. This kit
runs those same calls for every trusted head on fleet machines, inside the fleet's walls, and posts one
**non-required** check per head (`fork-ci-own` by default) from a CI App of its own. Actions stays the engine and
the required check; the lane only runs beside it until the parity report says it gives the same answers, no slower.

Plain Python 3 standard library. Linux; the jobs run through the fleet launcher (`.github/scripts/fork-fleet-launch.sh`).

## One plan, two runners

`.github/fork-ci-plan.json` names every `node .github/scripts/fork-ci-tests.cjs ARGS` call of Fork tests: its part
name, the job and step it is in, the job's check name (`{shard}` for a matrix shard) and its shards.
`.github/scripts/fork-ci-plan.test.py` fails when the plan and the workflow differ in any call, so the lane can never
run another set of tests than Actions. A part with `"lane": false` stays on Actions and says why (root, a systemd
user manager, the app's dependencies). The lane reads the plan **at the commit it tests**; a commit without one, or
with one this kit cannot read, is not run (`neutral`: Actions counts).

## What one tick does

`ci-lane tick`, one at a time (a lock in the state folder):

1. **Collect.** For each running job, the launcher's `lane-result`: `done` is judged (below); `gone` (the machine
   died, the unit was removed) is **lost**; still running past its lease (`leaseMinutes`, 40 by default) is stopped
   and lost. A launcher that cannot answer leaves the job to its lease.
2. **Heads.** One GraphQL call: the default branch's tip and the 100 open pull requests updated last. Only trusted
   heads count: the tip, and heads of pull requests from branches of this repository. A pull request from another
   fork is never run.
3. **Superseded.** A head that is no longer a head (a new push to its branch, a closed PR, a new tip) has its running
   jobs stopped and its check completed as `cancelled`. With more than 100 open PRs, a head that only fell off the
   list is left alone.
4. **New heads** (at most 3 a tick, the default branch first): GitHub's check runs of our App on the commit first;
   then the plan at that commit; then the check is posted `in_progress`; only then are its jobs queued.
5. **Start** queued jobs, up to `maxJobs` at once, the default branch first: the launcher's `lane-start`. A start
   refused three times is lost.
6. **Finish.** A head whose jobs are all final gets its verdict on its check and moves to the history.
7. **Parity.** Up to 2 finished heads a tick get the Actions side (below).

Every key is the commit, `<part>:<shard>` and the attempt; the launcher's unit is
`lane-<commit:12>-<part>-<shard>-<attempt>`.

### What the check can never claim

- `success` needs, for **every** planned job of the head: a job this lane started itself, exit 0, and a records file
  (`fork-ci-tests.cjs`'s) of **this** commit (all 40 characters), this part (`job` is `lane-<part>`), this shard and
  this attempt (`run_attempt`), in which every owed test has a passing result and none failed. Anything less is
  `failure`.
- The check is posted `in_progress` before any job starts, so a crash leaves "not passed", never a pass.
- A real test failure is `failure`; a lost retry of another part never hides it.
- A lost job gets **one** retry (attempt 2); lost again, Actions counts for that part and the head is `neutral`,
  never `success`. Each such loss is an `ALERT` line.
- Only check runs of the configured App (`appId`) with this name on this commit count as this lane's own.

## Runs again, stops half-way, old or empty state

- **Runs again:** a tick that finds nothing new posts and starts nothing. A finished head (in the history, or with a
  completed check on GitHub) is never run again.
- **Stopped half-way:** the journal is written once at the end of the tick (to a temporary file, then renamed).
  - Stopped before the check was posted: the head is planned again next tick; nothing ran.
  - Stopped after the check was posted (or the journal was lost while jobs ran): the next tick finds our
    `in_progress` check, reuses it, and runs the jobs as attempt 2: the one retry. Lost again: Actions counts. The
    old attempt's units carry other names: their `RuntimeMaxSec` ends them, and the launcher's prune lets go of an
    ended one no `lane-result` read an hour later, so it does not hold its runner user for good.
  - Stopped after `lane-start` but before the journal: the next tick's start of that name is refused (the unit is
    there); `lane-result` shows it running or done, so the lane adopts it (its lease from then) and never starts
    it twice. A refused start with no unit of that name counts as a try, as before.
  - Stopped after `lane-result` read a done job but before the journal: the launcher kept that answer, so the
    next tick gets the same one, not `gone`.
  - A verdict or a cancel that GitHub refused is posted again next tick.
- **Installer:** safe to run again (same release: nothing changes) and to stop at any step (the next run ends it);
  the timer units are only written once the release they run is the one in use, never enabled by it, and a unit
  file this kit did not write is refused and kept.
- **Old or empty state:** a missing, broken or other-version journal is read as empty; GitHub's check runs (the
  lasting record) rebuild it as above. A check belongs to one commit, so it cannot meet a newer head.

## Parity report

`ci-lane parity [--json]` compares each finished head's jobs with the Actions step that runs the same call (the
plan's `check` and `step`, in the newest complete run of the plan's workflow on that commit). passed = `success`,
failed = `failure`; a job that fell back to Actions, or a step that was skipped, cancelled or is missing, is not
compared. A difference the config's `explained` names (`"<commit>:<part>:<shard>": "why"`) is explained. The report
ends with the phase 3 gate: 200 compared heads, no unexplained difference, and the lane's median part time not over
Actions'. Actions' side costs two calls per head; a head whose run is not complete is looked at again every 15
minutes, at most 12 times.

## Work panel rows

`ci-lane status --json` prints one row per job of each open head (commit, what, part, attempt, state, start, time,
note): the rows the T3 work panel (#1136) shows.

## Config

`~/.config/ci-lane/config.json` of the service user that runs the timer (`$XDG_CONFIG_HOME` if set; `--config`):

```json
{
  "version": 1,
  "repo": "OWNER/REPO",
  "mode": "shadow",
  "checkName": "fork-ci-own",
  "appId": 123456,
  "auth": { "ghAs": "/usr/local/bin/gh-as" },
  "launch": ["/usr/bin/sudo", "-n", "/opt/fork-fleet/fork-fleet-launch.sh"],
  "maxJobs": 2,
  "leaseMinutes": 40,
  "explained": {}
}
```

- `mode`: `shadow`, or `off` (nothing runs; Actions counts for everything). Phase 3 adds the required verdict.
- `appId` and `auth.ghAs`: the CI App (Checks write; Actions, Contents and Pull requests read; this repository only),
  through the gh-apps kit's `gh-as`, so its key stays root's: this user, the jobs and the agents never read it.
- `launch`: the fleet launcher's argv (with `sudo -n`, as the dispatcher runs it).

State: `~/.local/state/ci-lane/` (`$XDG_STATE_HOME`; `--state-dir`): `state.json` (open heads, the last 500 finished
ones for parity) and `lock`. Exit codes: 0 fine, 1 a pass with errors (each logged), 2 a config problem.

## The launcher's side

The lane calls three verbs of the fleet launcher (`.github/scripts/fork-fleet-launch.sh`, installed by the desk kit
as `/opt/fork-fleet/fork-fleet-launch.sh`), as root through `sudo -n`:

- `lane-start NAME`, stdin exactly `FORK_LANE_SHA=<40 hex>`, `FORK_LANE_PART=<part>`, `FORK_LANE_SHARD=<i>/<n>`,
  `FORK_LANE_ATTEMPT=<1|2>`, and NAME exactly `lane-<sha:12>-<part>-<i>-<attempt>`: starts the job in the same
  walls as a runner job, through the same one `systemd-run` (a free runner user of its own, its network namespace,
  sandbox and limits, `RuntimeMaxSec`), and returns once it started. No runner and no template: an empty copy, and
  the lane job (the launcher's `LANE_JOB`, written root's into the unit's own root, so nothing a runner user can
  write decides what runs) makes an anonymous checkout of that commit from the launcher's `LANE_REPO` (launch.conf:
  the lane names a commit, never where it comes from), reads the plan at that checkout and runs that part's call
  with `FORK_CI_TEST_SHARD`, and `FORK_CI_TEST_RECORDS` in the copy with `GITHUB_JOB=lane-<part>` and
  `GITHUB_RUN_ATTEMPT=<attempt>`. It exits 100 only when that call exited 0, anything else 1 to 99, so the unit always
  ends `failed` and systemd keeps it (`CollectMode=inactive`), with its exit, and ends whatever the job left running.
- `lane-result NAME`: one JSON line, `{"state": "running"}`, `{"state": "gone"}`, or
  `{"state": "done", "exit": N, "records": {...} or null}`. N is the exit as systemd saw it, from outside the job:
  0 only for the job's pass code with no timeout, OOM kill or signal; 128 + the signal for a killed job. The records
  are read by root only once nothing of the job is left, and only from one plain `.json` file of the runner user's
  own in the copy's `out` folder (no link, no second name), that parses as an object; else `null` (judged
  `failed`). The answer is kept root's (`RUN_DIR/NAME.result`) before the unit is let go (`reset-failed`), so a lane
  that stopped before writing its journal gets the same answer again, not `gone`; prune removes it a day later.
- `stop NAME` (the launcher's existing verb): for a lane job, also lets go of a unit that had ended.

A lane job holds its runner user from its start until `lane-result` read it (or `stop`). The dispatcher's pools and
the lane draw from the same `RUNNER_USERS`, so give the host at least the pools' slots plus `maxJobs` of them. The
host's `/usr` (the jobs' view) needs `git`, `python3` and a Node the fork's scripts run with: a lane job sets up
nothing itself, and a part whose tools are missing fails there, which the parity report shows.

## Install

As root, with the bootstrap (`fork-kit/common/bootstrap.sh`): `--kit ci-lane --repo OWNER/REPO --sha RELEASE_SHA
[--timer | --no-timer]`, or `ci-lane-install install --source KIT_FOLDER --sha RELEASE_SHA [--timer | --no-timer]`.
It copies the kit into `/usr/local/libexec/ci-lane/versions/<sha>/`, links `/usr/local/bin/ci-lane`, and with
`--timer` writes the user units `ci-lane.service` and `ci-lane.timer` (a tick every 2 minutes) in
`/etc/systemd/user/`. Written is not enabled: nothing runs until the service user enables the timer itself. Safe to
run again and to stop at any step; `uninstall` removes it all (the users disable the timer first).

The owner's steps, in this order:

1. The CI App (Checks write; Actions, Contents and Pull requests read; this repository only), its key in the gh-apps
   kit, and `gh-as` allowed for the service user.
2. On the fleet host: the launcher release with these verbs (the walls pin moves with it),
   `LANE_REPO=https://github.com/OWNER/REPO` in `/etc/fork-fleet/launch.conf`, and enough `RUNNER_USERS` (above).
3. A service user with no other rights, and a sudoers rule for it that allows exactly these three:
   `/opt/fork-fleet/fork-fleet-launch.sh lane-start *`, `... lane-result *` and `... stop lane-*`.
4. The kit with `--timer`, that user's `~/.config/ci-lane/config.json` (Config, above), `loginctl enable-linger`
   for it, then as it: `systemctl --user daemon-reload && systemctl --user enable --now ci-lane.timer`.

To stop the lane: `mode: "off"` in the config (the next tick does nothing), or disable the timer. Jobs that run keep
their lease; a done one keeps its runner user until a tick reads it, or by hand
`sudo /opt/fork-fleet/fork-fleet-launch.sh stop NAME`.

## Not here yet

- Creating the CI App and its installation, the sudoers rule, and enabling the timer (owner steps, above).
- The T3 work panel side of the rows (#1136).

## Tests

`python3 -I -B fork-kit/ci-lane/tests/<name>.test.py`: `lane` (the tick on a fake GitHub and launcher, the verdict
rules with a RedCheck, the state question), `parity`, `parts` (the plan reader, the gh-as client, the launcher
contract, against the launcher's own text), `install` (the installer and its units, with a RedCheck), and
`.github/scripts/fork-ci-plan.test.py` (the plan against the workflow). The launcher's verbs: the `Launcher` class
of `.github/scripts/fork-fleet.test.py` (`lane` tests, on a fake systemd) and its `LaneJob` class (the job itself).
