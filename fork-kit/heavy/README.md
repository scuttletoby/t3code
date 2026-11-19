# heavy: run builds and tests in a capped slice, and stop what they leave behind

`heavy <command>` runs a command inside a systemd user slice with a memory limit (a "heavy" slice, so a build or a
test server cannot starve the rest of the machine), in a scope of its own. When the command ends, every process it
left running in that scope is stopped, and heavy says so on stderr:

```
heavy: stopped 3 leftover processes (sleep, java)
```

Nothing is printed when nothing was left. The line is there so a leak is seen and fixed where it starts (a test
that forgets its background job, a server a script never stops), instead of piling up for hours.

Linux with systemd (user manager) and cgroup v2 only. Python 3 standard library only.

## Use

| Command                      | What happens                                                                                 |
| ---------------------------- | -------------------------------------------------------------------------------------------- |
| `heavy <command> [args...]`  | runs the command in `heavy-<name>-<random>.scope` in the slice; stops leftovers when it ends |
| `HEAVY_KEEP=1 heavy <cmd>`   | the same in `heavy-keep-<name>-<random>.scope`, but leftovers keep running (a daemon)        |
| `heavy --self-check`         | checks the config, that the slice has a unit with a memory limit, and the installed files    |
| `systemctl --user stop UNIT` | stops what `HEAVY_KEEP=1` kept (the name is in heavy's message)                              |
| `heavy sweep [--dry-run]`    | one leftover sweep (below); the user timer runs it every 5 minutes                           |
| `heavy sweep --json`         | looks only: what a sweep would stop or report, as JSON                                       |
| `heavy docker-label`         | the owner label for a container a test starts (below)                                        |
| `heavy --kind K <cmd>`       | waits in the memory queue until the job fits, then runs it (`--mb`, `--wait`: below)         |
| `heavy queue [--json]`       | what the memory queue runs and holds, and the figures behind it                              |
| `heavy waits [--days N]`     | minutes waited for memory per day (`--json` too)                                             |
| `heavy -- <command>`         | runs a command named like one of the words above                                             |

The command gets the same folder, environment, stdin, stdout, stderr and open files. heavy exits with the command's
exit code, or dies of the same signal (a shell sees `128+N`), so scripts and `make` see what they saw before. The
environment is exactly the caller's (Python's own locale change is not passed on), plus `HEAVY_SCOPE`, the name of
the scope (a test can label what it starts with it). Signal settings are the caller's too: SIGPIPE and SIGXFSZ are
back to the default, so a writer into a closed pipe ends as before. Only `HEAVY_KEEP=1` makes a `heavy-keep-*`
scope (a command named `keep` gets `heavy-keep_-<random>.scope`), so the leftover sweep can tell kept ones apart.

**Signals.** When heavy gets INT, TERM, HUP or QUIT, the command gets it too: from the terminal (Ctrl-C reaches
both) or from heavy (when someone sent it to heavy alone; a signal sent to the whole process group, as `timeout`
or a shell's `kill %1` do, reaches the command once). heavy then waits up to the grace time (30 s by default, so a
command that handles Ctrl-C, a REPL or a server saving its data, has time) for the command, and stops the whole
scope (TERM, then KILL after the grace time again). A second signal stops it at once. A command that handles the
signal and exits keeps its own exit code. USR1 and USR2 are passed on. If heavy is killed with
`kill -9`, the command is killed with it (as before, when heavy and the command were one process); anything the
command had started keeps running in its scope (the leftover sweep below is for that case).

**Never uncapped.** heavy refuses (exit 1, the reason on stderr, the command not run) when the systemd user
manager cannot be reached, when the slice has no unit file (systemd would make an empty one up on the fly, with no
limits), or when the slice has neither `MemoryMax` nor `MemoryHigh`.

**Nested.** `heavy` inside a `heavy` command runs the inner command in the outer scope; the outer heavy cleans it
up and prints the one line. With `HEAVY_KEEP=1`, the inner one gets a scope of its own (outside the outer one).
`HEAVY_KEEP` is in the command's environment, so commands it starts that also use heavy keep their leftovers too.

## Memory queue

One queue for every heavy job of a user on the machine (CI jobs, agent builds, test servers), instead of a fixed
number of build slots: `heavy --kind <name> [--mb N] <command>` reserves the memory the job needs and starts it only
when that fits; the others wait in order. Without `--kind` heavy runs at once, as before (such jobs still count:
their real use is in the slice's figures).

- **What a job reserves:** `--mb` (the project's start value for the kind), else 3072 MB, raised by what the kind
  has used. While a queued job runs, heavy samples its scope's memory every 3 s and once more when the command
  ends; the highest sample of every run goes into the kind's history (`peaks.json`, last 20 runs), failed and
  OOM-killed runs too (they often peak highest). The reservation is the highest of those peaks x 1.15, rounded up
  to 128 MB, never below the start value, at most 4x it, and a learned value at most the budget minus `vmReserveMb`.
  That last cap keeps one run that leaked (and hit the slice's `memory.high`) from learning a value at or above the
  budget, which could then only run while the slice is near empty and so never while plain `heavy` jobs run: the
  kind would give up with 76 again and again and never record a lower peak. It is the one case where the
  reservation can be below a real peak; the slice's `memory.high`/`memory.max` still catch a real overrun. A start
  value above the budget stays as set. Kinds and start values are project facts: they live in the project's config,
  which passes them as `--kind`/`--mb`.
- **Sampling limits:** a spike shorter than 3 s can be missed, and a job that is killed for memory is seen at its
  last sample, not at the moment it was killed. That is why the reservation keeps a margin and never drops under
  the start value: set the start value from a real peak, not a typical run.
- **When it fits:** reservation <= budget - used - owed, and <= MemAvailable - `vmReserveMb` - owed, the slice is
  not under memory pressure, and swap free is at least `swapFreeMinMb` (skipped on a machine without swap). budget
  = the slice's `memory.high` (else `memory.max`, else `budgetMb`) minus `headroomMb`; used = the slice's memory
  that is not clean cache, plus its swap (cache that can be dropped does not count); owed = for each admitted job,
  the part of its reservation its scope has not used yet (so a job that has not grown yet still holds its room, and
  one that has is counted once). Not counted: memory other tools have promised but not used yet, and plain `heavy`
  jobs before they grow (their real use is in `used`). A machine that has its own check for that sets
  `admitCommand`.
- **Admit command (optional):** `admitCommand` is a program and its arguments (`{mb}` becomes the reservation),
  asked after the queue's own rule says yes, under the queue's lock. Exit 0 admits the job; any other exit, or no
  answer within `admitTimeoutSeconds` (its process group is then killed), means not now: the job keeps its place
  and asks again on the next look. Its last output line is shown as the reason. Its output is read with a deadline:
  a command that exits but leaves a process holding its output open is not now too (that process group is
  killed). The refusal goes into the job's ticket: until it is admitted, the jobs behind it do not keep its room for
  it, so a later job that fits (and that the command admits) starts; once it has waited `passLimitSeconds` it holds
  them by the pass-limit rule below, as any job that does not fit. `heavy queue --json` shows the setting
  (`settings.admitCommand`), so a caller can tell whether the machine's own check already runs here.
- **Order:** first come, first served. A job that does not fit lets later jobs that do fit pass until it has waited
  `passLimitSeconds` (10 min, on the monotonic clock); after that nothing passes it while that helps it: while queued
  jobs run (or are about to) whose end would make room for it. A job that would not fit even with every queued job
  ended (other programs use the memory) does not hold the queue; later jobs keep passing it.
- **Larger than the budget:** such a job runs alone: once it is first, nothing else of the queue runs, the slice
  uses no more than the headroom, and the machine has the whole budget free (MemAvailable - `vmReserveMb`). The
  other checks (pressure, swap, admit command) hold for it too.
- **Waiting** is a poll of the real figures every `pollSeconds` (2 s). heavy prints the reason and the position
  when they change and once a minute, for example
  `heavy: waiting for memory: typecheck needs 3072 MB, room 1108 MB (budget 4608 MB, used 3500, reserved not yet
  used 0, available 9000); position 1 of 2, waited 1m00s`. After `--wait` seconds (default 7200) it gives up with
  exit 76 and the line `heavy: gave up after waiting ...` on stderr, and does not run the command. A command that
  exits 76 by itself has no such line: a caller that must tell them apart reads stderr. INT, TERM or HUP while
  waiting end it as they would end the command.
- **The queue's lock is never waited on without an end:** a lock held by a stuck process ends the wait with exit 76
  after `--wait` seconds (at least 5), and `heavy queue` and saving a peak after 30 s plus `admitTimeoutSeconds`
  when an admit command is set (longer than one admit command can hold the lock). A finished job deletes its ticket
  without the lock. INT, TERM and HUP stay blocked while heavy waits in the queue and are taken only between looks,
  never while it holds the lock; once a job is admitted they stay blocked until heavy waits on the command, so one
  that comes in between stops heavy before the command starts, with one line. After a stop signal heavy tries for
  the lock only 2 s (the wait-log line, the peak), so a second Ctrl-C ("stop now") never waits on the queue. The
  admit command is the one thing run under the lock, and `admitTimeoutSeconds` bounds it.
- **Minutes waited:** each admission, give-up and stop is one JSON line in `~/.local/state/heavy/waits.log`;
  `heavy waits` sums it per UTC day (jobs, minutes waited, longest wait, give-ups, minutes per kind). That figure
  decides whether a machine needs more memory.
- **Nested:** a `heavy --kind` inside a heavy command does not queue again: the outer job holds the memory.
- **Limits:** the queue is per user and slice. It does not cap a job at its reservation (a job that grows past it
  is still counted, in `used`).

### Queue settings

The optional `queue` object in the machine file's `heavy` section; without the file or the key the defaults hold:

```json
{
  "version": 1,
  "heavy": {
    "queue": {
      "headroomMb": 512,
      "vmReserveMb": 1024,
      "pressureFull10": 5,
      "pressureSome60": 10,
      "passLimitSeconds": 600,
      "pollSeconds": 2,
      "budgetMb": 0,
      "swapFreeMinMb": 1024,
      "admitCommand": [],
      "admitTimeoutSeconds": 20
    }
  }
}
```

`budgetMb` 0 means the slice's limit; `swapFreeMinMb` 0 turns the swap check off; `admitCommand` `[]` means none
(else a list of strings; the first is an absolute path to a program, for example
`["/usr/local/bin/my-check", "--need", "{mb}"]`). An unknown key or a bad value is refused (heavy then runs no
queued job; the message names the key). Install the release with the queue before you add the key: an older sweep
refuses it.

### The queue runs again, stops half-way, or meets old or empty state

- **A waiter or a job dies** (any way, `kill -9` too): each job holds an exclusive `flock` on its own ticket file
  in `/run/user/<uid>/heavy-queue/<slice>/` for its whole life. The kernel drops the lock with the process; the next
  look (under the queue's lock) deletes tickets nobody holds. Nothing can stay reserved for a process that is gone.
- **Runs again:** a new ticket gets the next place after the live ones; a finished job deletes its ticket.
- **Reboot / empty state:** `/run/user/<uid>` is empty after a reboot; the folder is made again (mode 700, refused
  when it is not the user's own). A missing wait log or peak history starts empty.
- **Old or garbled state:** a live ticket that cannot be read (garbled, or another version's) counts as admitted at
  3072 MB, with a note in `heavy queue`. A broken `peaks.json` starts a new history; one written by a newer heavy is
  neither used nor changed. Links and other files in the folder are not followed.
- **Two callers at the same instant:** taking a place and every admission happen under one `flock` on the queue's
  `lock` file, so two callers always get two places, in order.
- **heavy itself is killed** (`kill -9`) after its job was admitted: the command inherits the ticket's lock, so the
  job keeps its reservation until the processes it started are gone. (Those processes have one more open file than
  before: the ticket.)
- **Stops half-way through a write:** `peaks.json` and `waits.log` are written to `<file>.<pid>.tmp` and renamed;
  a failed write removes its temporary file, and the next writer removes ones whose process is gone.
- **The admit command's program is missing** (removed or not yet installed): settings still load, `heavy queue`
  works and marks it, and jobs the queue's own rule holds keep waiting. A job that reaches the admit command stops
  with exit 1 and a line naming the program, and does not run: without its check it must not start, and waiting
  `--wait` for a program that may never come back would hide the fault. Fix the program or remove the setting.
- **The admit command hangs, or its leftover holds its output:** not now after `admitTimeoutSeconds` (or 1 s after
  it exited); its process group is killed and the job asks again on the next look.
- **One leaky run of a kind:** its peak counts (failed runs too), but the learned value is capped at the budget
  minus `vmReserveMb`, so the kind is still admitted while other jobs use the slice.

## Leftover sweep

heavy cleans up after its own command. Some things still escape: what a command left after heavy was killed with
`kill -9`, test runs started without heavy, and Docker containers (the Docker daemon starts them, so they are never
in heavy's scope). `heavy sweep` finds them. A user timer (`heavy-sweep.timer`, installed with `--sweep-timer`)
runs it every 5 minutes for each user who enables it. It only ever looks at the processes and containers of the
user it runs as.

It finds:

1. **Processes whose owner is gone:** reparented to the user manager (or pid 1), not the main or control process
   of a unit, not in a `knownUnits` unit, not in a live heavy scope, older than `minAgeSeconds`, and busy (over
   `busyCpuPercent` in a one-second sample) or long-lived (older than `longLivedSeconds`). One whose **own
   command** matches a `testPatterns` entry is **stopped**, with what it started in its own unit (TERM, then KILL
   after 5 seconds). All others are **only reported**. A command line matching `ignorePatterns` is left out
   altogether.

   The own command is the program and, for an interpreter (python, node, java, a shell, ...), its script, module,
   jar or `-c` text; for a wrapper that runs one command (`timeout`, `env`, `nohup`, `xvfb-run`, ...), the wrapper
   plus that command's own part. Other arguments are never matched: `claude -p "run pytest"` or `vim a.test.py` is
   not test-shaped, `python3 -m pytest -x` and `bash run.test.sh` are, and a Gradle test worker is matched by its
   main class (`GradleWorkerMain`), not by `gradle`.

   Never stopped, whatever their command line: a session leader or terminal multiplexer (tmux, screen, sshd,
   zellij, a login shell `-bash`, an interactive shell with no script; it can host other sessions), and a heavy
   whose scope is live (heavy itself detached with `nohup heavy ... &` or `setsid`, at any age). When a stopped
   process started one of these, or a process in another unit, that one is left alone with everything below it.

   **A busy test detached without heavy** (`nohup python3 -m pytest &`, `setsid ./run.test.sh`) is stopped once
   it is older than `minAgeSeconds` (10 minutes by default) and stopping is on. Run long tests under heavy, or
   in a unit listed in `knownUnits`.

2. **Heavy scopes whose heavy is gone:** stopped as a whole (as heavy itself would have done). A scope kept with
   `HEAVY_KEEP=1` is never stopped; its busy or long-lived processes are reported. heavy names kept scopes
   `heavy-keep-<name>-<random>.scope` and all others never so (a command named `keep` gets `heavy-keep_-...`).
   A scope that holds a session leader or multiplexer (`heavy tmux`) is only reported.
3. **Empty heavy scopes** (a heavy killed while it started): stopped. One that holds only another user's
   processes is only reported.
4. **Docker containers** with the owner label (below) of this user whose owner is gone: **removed** with their
   anonymous volumes (`docker rm -f -v <full id>`; never a short id, which Docker would first read as a name).
   A container labelled under a kept scope (`HEAVY_KEEP=1 heavy ./db-up.sh`, which exits and leaves the scope
   empty) is **only reported**: remove it by hand when done. Running containers without the label are **only
   reported**, never touched, unless their name matches `knownContainers`.

Before every signal it checks that the pid still belongs to the same process (start time and user, through a
pidfd), so a reused pid is never hit. It never touches itself or the processes that started it.

Each sweep appends to `<report>/sweep.log` (rotated at 1 MiB) and writes a short `<report>/last-report.txt`.
The report folder must be a folder of the sweep's user that no group or other user can write (not a link); else
the sweep refuses. The log, the report and the journal show only a process's own command (above), with the
number of other arguments, and mask anything that looks like a secret (`--token=***`, `PASSWORD=***`, a URL's
password, `ghp_...`-shaped words). Inline code (`sh -c`, `fish --command`, `python3 -c`, `node -e`, `perl`/`ruby -e`,
`php -r`/`-B`/`-R`, and bundled forms such as `bash -lc`, `sh -ec`, `python3 -uc`, `node -pe`, `perl -lne`, `perl -Fx -lane`,
`perl -0777ne`, also behind `timeout`, `stdbuf` or `env -S`) shows only its length (`python3 -c [39 chars]`). For a
shell, whose first word is a program, it also shows that word when it looks like a program name and the code has more
than one word (`bash -lc [mysql ... 35 chars]`). A short password in it (`-phunter2`, `-u admin:hunter2`, code that
is one word) is never shown, and fish's `-C` code is left out with the other arguments. A command line that nests
wrappers or `env -S` more than 8 deep is not read: it shows only its program and is only reported. Container names, images and
bad labels are cut to 60 characters and masked the same way:

```
heavy sweep 2026-01-01T12:00:00Z, stopping: 2 processes stopped, 1 containers removed, 1 reported
  stopped: pid 4012 python3 (25m old, cpu 97%, unit app.service): python3 -m pytest (+1 args) (+1 it started); busy; test-shaped ('pytest')
  removed: container 3f2a9c1b7d4e test-db (mariadb:11, 3h10m old, running); owner pid 3990 is gone
  report only: pid 5120 java (3h0m old, cpu 140%, unit app.service): java -jar server.jar; busy and long-lived; not test-shaped
```

**Dry run first.** Until the owner sets `"stop": true`, a sweep stops and removes nothing and says what it would
do (`would stop`, `would remove`). `heavy sweep --dry-run` does the same whatever the setting. Read a few reports,
add what is yours to `knownUnits`, `knownContainers` or `ignorePatterns`, then turn stopping on.

### Sweep settings

The sweep reads the `heavy` section of the machine file (`/etc/fork-kit/machine.json`, see
[common/README.md](../common/README.md)), key `sweep`. No file, no `heavy` or `sweep` section, an unknown key, a
wrong type or a newer `version`: the sweep checks and stops nothing, prints why, and exits 1. An empty `sweep`
object uses the defaults below (a dry run that reports).

| Key                | Default                      | What                                                              |
| ------------------ | ---------------------------- | ----------------------------------------------------------------- |
| `stop`             | `false`                      | `true`: stop and remove; `false`: dry run                         |
| `minAgeSeconds`    | `600`                        | younger processes, scopes and containers are left alone (>= 60)   |
| `longLivedSeconds` | `7200`                       | older than this counts as long-lived (>= 60)                      |
| `busyCpuPercent`   | `20`                         | more CPU than this (100 = one core) counts as busy                |
| `report`           | `~/.local/state/heavy-sweep` | folder for `sweep.log`, `last-report.txt` and the lock            |
| `knownUnits`       | `[]`                         | unit name globs whose processes are never touched                 |
| `knownContainers`  | `[]`                         | container name globs never touched or reported                    |
| `testPatterns`     | `[]`                         | regular expressions; a match on the own command = test-shaped     |
| `ignorePatterns`   | tmux, screen, login shells   | regular expressions on the whole command line; a match = left out |

The default `ignorePatterns` are `^(tmux|screen|SCREEN)( |:|$)` and `^-` (a login shell). The code never stops a
session leader or multiplexer anyway; the patterns also keep them out of the report. A list of your own replaces
the default, so keep these two in it.

### Owner label for test containers

A test that starts a Docker container labels it with its owner, so the sweep can remove the container once the
owner is gone (and leaves it alone while the owner runs):

```
docker run -d --label "$(heavy docker-label)" mariadb:11
```

`heavy docker-label` prints `fork-kit.heavy.owner=<owner>`; `--value` prints only the value (for an API such as
Testcontainers' `withLabel("fork-kit.heavy.owner", value)`). The owner is the heavy scope when the test runs
under heavy (heavy passes the scope name to the command as `HEAVY_SCOPE`), else the calling process with its
start time. A container whose label names another user is never touched.

**Setup scripts outside heavy.** The owner must be the process that lives as long as the container is in use:
the test, not a setup script, Makefile line or `npm` script that starts the container and exits (the sweep
would then remove it under the running test). So outside heavy, `heavy docker-label` refuses (exit 1) when its
caller is a shell or a script runner (`sh`, `bash`, `make`, `env`, `npm`, ...). A setup script names the test
process with `--owner-pid PID` (`--pid` is the same), for example a script the test runs as its child:

```
docker run -d --label "$(heavy docker-label --owner-pid "$PPID")" mariadb:11
```

`--owner-pid` refuses a process of another user (the sweep sees only its own user's processes and would count
the owner as gone).

### The sweep runs again, stops half-way, or meets old or empty state

| Case                                                                       | What happens                                                                  |
| -------------------------------------------------------------------------- | ----------------------------------------------------------------------------- |
| two sweeps at once                                                         | the second finds the lock held, says so, does nothing, exits 0                |
| a sweep killed half-way                                                    | what it did is in the log; the next sweep finds the rest and finishes         |
| a pid reused before the stop                                               | the start time differs: not signalled, reported as reused                     |
| a container already gone                                                   | reported as already gone, not a problem                                       |
| no Docker, or the daemon down                                              | a note in the report; processes are still swept                               |
| no machine file, a broken one, no section, an unknown key, a newer version | refused with the reason; nothing checked or stopped; exit 1                   |
| the timer fires before the file exists                                     | the same refusal, every 5 minutes, until the owner writes it                  |
| the systemd user manager unreachable                                       | refused; nothing stopped                                                      |
| a scope that does not stop                                                 | its processes are stopped one by one; any still running is a problem (exit 1) |
| a report folder that is a link, a file, another user's or group-writable   | refused; nothing checked or stopped                                           |
| a detached heavy (`nohup heavy ...`) of any age, its scope live            | never stopped or reported                                                     |
| an install stopped half-way, or a rollback to a release without the sweep  | the timer is written only after the new release is in use, and runs the       |
|                                                                            | sweep of the release in use; a release without one skips it (below)           |

## Settings

`/etc/heavy/config.json` (written by the installer):

```json
{ "version": 1, "slice": "heavy.slice", "grace_seconds": 30 }
```

`HEAVY_CONFIG` names another file; `HEAVY_SLICE` and `HEAVY_GRACE` override one value (tests use them;
`HEAVY_SYSTEMCTL` names another systemctl). Without a config file the defaults above apply. `grace_seconds` is how
long a command gets after a signal, and leftovers after TERM, before KILL. If the last check of the scope fails (an
overloaded host: systemctl errors or times out), heavy says it could not check for leftovers, never nothing.
heavy tells a signal sent to its whole process group from one sent to heavy alone with a small helper
process named `grp-sigprobe`. When a service manager stops a service that heavy runs in, it signals each process
of that service one by one; the command, in its own scope, then gets TERM only when the grace time ends.
The limits live in the slice unit, not here: either a unit you already have
(for example `~/.config/systemd/user/heavy.slice`), or one the installer writes to `/etc/systemd/user/` when given
limit options. A user's own unit in `~/.config/systemd/user/` wins over the one in `/etc/systemd/user/`.

## Files on the host

| Path                                            | What                                                                      |
| ----------------------------------------------- | ------------------------------------------------------------------------- |
| `/usr/local/bin/heavy`                          | link to `current/bin/heavy`                                               |
| `/usr/local/sbin/heavy-install`                 | link to `current/sbin/heavy-install`                                      |
| `/usr/local/libexec/heavy/versions/<sha>/`      | the code of one release, with `MANIFEST.sha256` (root 755)                |
| `/usr/local/libexec/heavy/current`              | link to the release in use (switched in one step)                         |
| `/usr/local/libexec/heavy/replaced/`            | a plain `/usr/local/bin/heavy` from before the kit, put back on uninstall |
| `/etc/heavy/config.json`                        | slice name and grace time                                                 |
| `/etc/heavy/installed.tsv`                      | every installed path with its link target or hash (`--self-check`)        |
| `/etc/systemd/user/<slice>`                     | only with limit options: the slice unit                                   |
| `/etc/systemd/user/heavy-sweep.{service,timer}` | only with `--sweep-timer`: the leftover sweep (each user enables it)      |
| `/etc/fork-kit/machine.json`                    | written by the owner, never the kit: sweep and queue settings             |
| `~/.local/state/heavy-sweep/`                   | each user's sweep log, last report and lock                               |
| `/run/user/<uid>/heavy-queue/<slice>/`          | the memory queue: its lock and one ticket per queued job (tmpfs)          |
| `~/.local/state/heavy/`                         | the queue's wait log (`waits.log`) and peak history (`peaks.json`)        |

## Install (owner, as root)

`RELEASE_SHA` is a commit on the fork's protected `ci-release` branch; the bootstrap refuses anything else. Every
merge to main that changes `fork-kit/` makes a new release commit, so the newest one is the head of `ci-release`.

```
sha=$(git ls-remote https://github.com/FORK_OWNER/FORK refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/FORK_OWNER/FORK/$sha/fork-kit/heavy/bootstrap.sh" -o /root/heavy-bootstrap.sh && sh /root/heavy-bootstrap.sh --repo FORK_OWNER/FORK --sha "$sha"
```

It prints the sha, the hash of the download and every installed path. Add `--slice NAME`, `--grace SECONDS`, or
`--memory-high 5G --memory-max 6G --swap-max 1G --cpu-weight 40` to have it write the slice unit (then each user
runs `systemctl --user daemon-reload`). Add `--sweep-timer` to install the leftover sweep's user units (each user
then runs `systemctl --user daemon-reload && systemctl --user enable --now heavy-sweep.timer`; `--no-sweep-timer`
removes them again). Upgrade: the same line again. Remove:
`/usr/local/sbin/heavy-install uninstall` (puts an old plain `/usr/local/bin/heavy` back). Check:
`heavy --self-check` (as the user: config, slice and files) or `heavy-install self-check` (files only).

## Runs again, stops half-way, old or empty state

- **Runs again:** every step checks what is there and only fixes what differs; a second run changes nothing.
- **Stops half-way:** the steps run in this order: check, folders, copy, self-test, config, slice unit, switch,
  links, sweep units, record. A version folder is copied to a temporary name and renamed; a half-copied or changed
  one is made again. The old `/usr/local/bin/heavy` keeps working until the links step, which copies it to
  `replaced/` before the link replaces it. The sweep's units are written only after that, so a timer never runs
  before the release it needs is in use. Running the same line again finishes the job.
- **Rollback:** the timer's service runs `/usr/local/libexec/heavy/current/lib/heavy_sweep.py` (the sweep of the
  release in use, never an older `heavy` without the `sweep` verb), and only when that file exists. Before going
  back to a release without the sweep, run the newer installer with `--no-sweep-timer`; if you did not, the
  timer just does nothing (users run `systemctl --user disable --now heavy-sweep.timer` to clear it). The older
  installer drops the setting from `config.json` but keeps the units; rolling forward again with the usual line
  (without `--sweep-timer`) keeps them and says so. If one of the two units there is not the kit's, that line
  refuses and names the file; remove it, or run with `--no-sweep-timer`.
- **Rollback past the memory queue:** remove the `queue` key from the machine file's `heavy` section first. A
  release from before the queue does not know it: its sweep refuses the whole machine file and says which key. That
  release cannot be changed from here, so the order is the guard: key out, then the older release.
- **Old state:** a plain-file `/usr/local/bin/heavy` (the one-line wrapper from before) is kept and put back by
  uninstall. A link there that points elsewhere, a folder where the kit expects its own, or a slice unit in
  `/etc/systemd/user/` the kit did not write is refused with a message, never overwritten. Config values stay
  unless an option names a new one.
- **Empty state:** a fresh machine installs with the defaults. heavy then refuses to run commands until the slice
  has a unit with a memory limit (install with limit options, or add the unit).

## Tests

`python3 -I -B fork-kit/heavy/tests/queue.test.py`: the memory queue on a fake cgroup tree and meminfo with a fake
clock; real processes where the kernel matters (a waiter killed with -9, a crashed job, two callers at the same
instant). Its red check breaks each admission and clean-up rule and shows the test for it then fails.
`heavy.test.py` runs queued jobs with the real systemd user manager in a private queue folder (`HEAVY_QUEUE_ROOT`,
never the user's live queue): one that runs, one that fails and still leaves its peak, one whose heavy is killed
with -9 (the reservation stays until its command ends) and one give-up (exit 76).

`python3 -I -B fork-kit/heavy/tests/sweep.test.py`: the sweep on a fake host (a fake `/proc`, fake `systemctl`
and `docker` from `tests/fake_tools.py`, a temporary machine file), no root and nothing real swept. Its red check
breaks each stop and report rule in a copy of the sweep and shows the test for that rule then fails. It also builds
scope names through heavy's own `run()` (`HEAVY_KEEP=1` and a command named `keep`) and checks how the sweep
treats them.

`python3 -I -B fork-kit/heavy/tests/install.test.py` (the installer on a fake host in a temporary folder, no root)
and `python3 -I -B fork-kit/heavy/tests/heavy.test.py` (the wrapper with the real systemd user manager and a slice
with a memory limit, `HEAVY_TEST_SLICE`, default `heavy.slice`; it skips without one, and fails instead with
`HEAVY_REAL_TESTS=1`, as in CI). `HEAVY_TEST_BIN=fork-kit/heavy/tests/old-heavy.sh` runs the wrapper tests against
the one-line wrapper from before: every leftover test fails (the red check).
