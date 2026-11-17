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
password, `ghp_...`-shaped words). Inline code (`sh -c`, `python3 -c`, `node -e`) shows only its first word, when
that looks like a program name, and its length (`sh -c [mysql ... 35 chars]`): a short password in it
(`-phunter2`, `-u admin:hunter2`) is never shown. Container names, images and bad labels are cut to 60 characters and masked
the same way:

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
| `/etc/fork-kit/machine.json`                    | written by the owner, never the kit: sweep settings                       |
| `~/.local/state/heavy-sweep/`                   | each user's sweep log, last report and lock                               |

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
  (without `--sweep-timer`) keeps them and says so.
- **Old state:** a plain-file `/usr/local/bin/heavy` (the one-line wrapper from before) is kept and put back by
  uninstall. A link there that points elsewhere, a folder where the kit expects its own, or a slice unit in
  `/etc/systemd/user/` the kit did not write is refused with a message, never overwritten. Config values stay
  unless an option names a new one.
- **Empty state:** a fresh machine installs with the defaults. heavy then refuses to run commands until the slice
  has a unit with a memory limit (install with limit options, or add the unit).

## Tests

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
