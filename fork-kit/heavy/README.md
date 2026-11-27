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
| `heavy --wait S <command>`   | the same; waits at most S seconds (default 7200) for the capacity rule first (below)         |
| `HEAVY_KEEP=1 heavy <cmd>`   | the same in `heavy-keep-<name>-<random>.scope`, but leftovers keep running (a daemon)        |
| `heavy --self-check`         | checks the config, the slice's limit and the installed files; warns on another `t3-capacity` |
| `systemctl --user stop UNIT` | stops what `HEAVY_KEEP=1` kept (the name is in heavy's message)                              |
| `heavy sweep [--dry-run]`    | one leftover sweep (below); the user timer runs it every 5 minutes                           |
| `heavy sweep --json`         | looks only: what a sweep would stop or report, as JSON                                       |
| `heavy leftovers`            | looks only: each leftover older than a limit, with its owner and age (below)                 |
| `heavy docker-label`         | the owner label for a container a test starts (below)                                        |
| `heavy --kind K <cmd>`       | waits in the memory queue until the job fits, then runs it (`--mb`, `--wait`: below)         |
| `heavy queue [--json]`       | what the memory queue runs and holds, and the figures behind it                              |
| `heavy waits [--days N]`     | minutes waited for memory per day (`--json` too)                                             |
| `heavy --measure K <cmd>`    | runs it as usual and records its peak memory under the kind K (below)                        |
| `heavy capacity ...`         | the front of the machine's capacity check: a kind becomes its measured MB (below)            |
| `heavy -- <command>`         | runs a command named like one of the words above                                             |

The command gets the same folder, environment, stdin, stdout, stderr and open files. heavy exits with the command's
exit code, or dies of the same signal (a shell sees `128+N`), so scripts and `make` see what they saw before. The
environment is exactly the caller's (Python's own locale change is not passed on), plus `HEAVY_SCOPE`, the name of
the scope (a test can label what it starts with it), and `HEAVY_MARKS`, which folders heavy reads the notes of
`heavy docker-label` from (as device:inode). Signal settings are the caller's too: SIGPIPE and SIGXFSZ are
back to the default, so a writer into a closed pipe ends as before. Only `HEAVY_KEEP=1` makes a `heavy-keep-*`
scope (a command named `keep` gets `heavy-keep_-<random>.scope`), so the leftover sweep can tell kept ones apart.

**Signals.** When heavy gets INT, TERM, HUP or QUIT, the command gets it too: from the terminal (Ctrl-C reaches
both) or from heavy (when someone sent it to heavy alone; a signal sent to the whole process group, as `timeout` or
a shell's `kill %1` do, reaches the command once). heavy then waits up to the grace time (30 s by default, so a
command that handles Ctrl-C, a REPL or a server saving its data, has time) for the command, and stops the whole
scope (TERM, then KILL after the grace time again) and removes its containers. A second signal stops it at once. A
command that handles the signal and exits keeps its own exit code. USR1 and USR2 are passed on. If heavy is killed
with `kill -9`, the command is killed with it (as before, when heavy and the command were one process), and heavy's
**guard** stops the rest of the scope (below).

**Everything it started.** When the command ends, and after a signal to heavy, heavy stops every process left in its
scope: children, double-forked daemons (`setsid`, `nohup`, `( cmd & )`) and anything else in it, since they all stay
in the scope's cgroup. Then it removes the Docker containers the command labelled as its own (`heavy docker-label`,
below) with their anonymous volumes, and says so: `heavy: removed 1 leftover container (test-db)`. Only a container
whose owner label names this user and this exact scope is touched; others, and containers without the label, never.
heavy asks Docker only after a run that labelled a container (`heavy docker-label` leaves a note for the scope in
`/run/user/<uid>/heavy/labelled/`, a folder only this user can write; when that note cannot reach heavy, because
`/run/user` is full or a sandbox has a `/run/user` of its own, in `~/.local/state/heavy/labelled/`, which heavy
reads too; the home is `$HOME`, else the password database's, and only one this user owns and no one else can
write (not `/`, and not in a folder someone else could swap it in: one not root's or this user's, or one others can
write that is not sticky), so heavy run as root with a user's `HOME` (`sudo -E heavy ...`) or `HOME=/tmp` uses
root's own home and never makes a root-owned folder in the user's; `~/.local` must be a real folder (not a link, even to a folder
of the user's: kept fail-closed) of the user's that no one else can write; an older heavy run as root with your
`HOME` may have left a root-owned `~/.local` or `~/.local/state`, and a umask of 002 a `~/.local` of mode 775
(heavy then skips it and says so):
`sudo chown "$USER:" ~/.local && sudo chown -R "$USER:" ~/.local/state && chmod go-w ~/.local` gives them back;
heavy tells `docker-label` which folders it reads in `HEAVY_MARKS`; without either folder heavy asks after every
run), so a run with no containers costs no `docker ps`. No Docker, or none this user can reach (a
rootless Docker through `docker context use` counts as reachable): nothing to do, nothing said. Docker down: one
line (`could not remove the containers of ...`); the leftover sweep looks again. `HEAVY_KEEP=1` keeps the scope's
containers with it, unless heavy was stopped by a signal. When systemd cannot stop the scope, heavy stops what is
left itself: TERM only to a process still in the scope (checked through a pidfd, so a pid that ended and was reused
by another process is never hit; without pidfds, before Linux 5.3 or Python 3.9, no TERM and no grace wait, only
a 1 s look for processes that end anyway), then
the kernel's `cgroup.kill` (it also kills a `sudo` or setuid process the command started in its scope, which a plain
kill could not reach: the scope is the job's own). Without `cgroup.kill` (before Linux 5.14) the KILL goes through a
pidfd too; what is still running then is named, and the leftover sweep looks again.

**The guard.** heavy starts a small helper (named `scope-guard`) in a session of its own, outside the scope, that
waits for heavy. It is not heavy's child (a double fork), so a kill of heavy's process tree (`pkill -9 -P`, a tool
that walks children) misses it. The command refuses to start when heavy is already gone. heavy tells the guard
"done" after its own clean-up, and ends only once the guard has ended. When heavy goes away without that (`kill -9`,
a crash, an out-of-memory kill), the guard does heavy's clean-up: it stops the scope and removes its containers, and
prints `heavy: heavy was killed: stopped 2 leftover processes (sleeper)`. It ignores the signals heavy handles (a
terminal's Ctrl-C, a service stop's TERM) and a kill of heavy's process group, holds no file of heavy's but stderr
(so a pipe after heavy ends once it is done), and is never in the scope it cleans. Not for `HEAVY_KEEP=1` (a kept
scope outlives heavy on purpose) or a nested heavy (the outer one has a guard). Only a kill of the guard too
(`kill -9` on both, by pid) leaves the scope; the leftover sweep finds it then.

**Capacity first.** Before the command starts, heavy asks the machine's capacity rule itself (`heavy facts
--fresh`, below: the same answer as `t3-capacity`), so a job started right after a NOT OK (`check ; test`, or a
check whose exit code nobody read) still waits. A job without `--kind` waits until the rule says free for one job
of the rule's `unknownKinds.job`; heavy says why every time that changes and once a minute (`heavy: waiting for
capacity: ...`) and gives up after `--wait` seconds (default 7200) with exit 76 and `heavy: gave up after waiting
... for capacity`, without running the command. A queued job (`--kind`) asks it in the queue (below). Not asked: a
nested heavy (the outer one holds the room). Anything but a clear "free" (busy, no answer within 5 s, a broken
answer) is not free. It is one check with `t3-capacity`'s (`lib/heavy_facts.py` `checked`), so a NOT OK holds 20 s
for the same question in heavy too, whichever of them read it (below). Two waiters without `--kind` are admitted one
at a time: each asks under the user's queue lock, and one told free keeps the lock until `heavy facts` can count
its new scope (at most 5 s), so both never take the same room. A kit whose `heavy facts` can never answer (the
capacity rule not installed with it, a broken `heavy` section of the machine file) refuses at once (exit 1, `heavy:
the capacity check cannot run: ...`) instead of waiting `--wait` for nothing; a broken `heavy.queue` section is a
warning, and the wait goes on with the default queue settings (a queued job is refused, as its admission needs
them). The machine file's `heavy.queue.capacityCheck: false` turns it off.

**Never uncapped.** heavy refuses (exit 1, the reason on stderr, the command not run) when the systemd user
manager cannot be reached, when the slice has no unit file (systemd would make an empty one up on the fly, with no
limits), or when the slice has neither `MemoryMax` nor `MemoryHigh`.

**Nested.** `heavy` inside a `heavy` command runs the inner command in the outer scope; the outer heavy cleans it
up and prints the one line. With `HEAVY_KEEP=1`, the inner one gets a scope of its own (outside the outer one).
`HEAVY_KEEP` is in the command's environment, so commands it starts that also use heavy keep their leftovers too.

## Memory queue

One queue for every heavy job of a user on the machine (CI jobs, agent builds, test servers), instead of a fixed
number of build slots: `heavy --kind <name> [--mb N] <command>` reserves the memory the job needs and starts it only
when that fits; the others wait in order. Without `--kind` a job does not queue: it waits only for the capacity
rule (Capacity first, above), and still counts (its real use is in the slice's figures, and `heavy facts` counts
its scope as a job).

- **What a job reserves:** `--mb` (the project's start value for the kind), else 3072 MB, raised by what the kind
  has used. While a queued job runs, heavy samples its scope's memory every second and once more when the command
  ends; the highest sample of every run goes into the kind's history (`peaks.json`, last 20 runs), failed and
  OOM-killed runs too (they often peak highest). The reservation is the highest of those peaks x 1.15, rounded up
  to 128 MB, never below the start value, at most 4x it, and a learned value at most the budget minus `vmReserveMb`.
  That last cap keeps one run that leaked (and hit the slice's `memory.high`) from learning a value at or above the
  budget, which could then only run while the slice is near empty and so never while plain `heavy` jobs run: the
  kind would give up with 76 again and again and never record a lower peak. It is the one case where the
  reservation can be below a real peak; the slice's `memory.high`/`memory.max` still catch a real overrun. A start
  value above the budget stays as set. Kinds and start values are project facts: they live in the project's config,
  which passes them as `--kind`/`--mb`.
- **Sampling limits:** a spike shorter than a second can be missed, and a job that is killed for memory is seen at its
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
- **Capacity rule:** after the queue's own rule says yes, the job must also be free by the machine's capacity rule
  (`heavy facts --kind <kind> --mb <reservation> --fresh`; a fresh reading, so a job admitted a second ago counts).
  Not free is not now, as for the admit command (below: the job keeps its place, the jobs behind it keep no room
  for it). `capacityCheck: false` turns it off.
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
  is still counted, in `used`). Jobs without `--kind` take no place in it: they wait only for the capacity rule, so
  a steady stream of them can keep a large queued job waiting until it gives up (exit 76). Give such jobs a
  `--kind` when they must wait in order.

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
      "admitTimeoutSeconds": 20,
      "capacityCheck": true
    }
  }
}
```

`budgetMb` 0 means the slice's limit; `swapFreeMinMb` 0 turns the swap check off; `capacityCheck` false: heavy
does not ask the capacity rule before a job starts; `admitCommand` `[]` means none
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
  job keeps its reservation until the processes it started are gone, which heavy's guard sees to at once. (Those
  processes have one more open file than before: the ticket; the guard holds none.)
- **Stops half-way through a write:** `peaks.json` and `waits.log` are written to `<file>.<pid>.tmp` and renamed;
  a failed write removes its temporary file, and the next writer removes ones whose process is gone.
- **The admit command's program is missing** (removed or not yet installed): settings still load, `heavy queue`
  works and marks it, and jobs the queue's own rule holds keep waiting. A job that reaches the admit command stops
  with exit 1 and a line naming the program, and does not run: without its check it must not start, and waiting
  `--wait` for a program that may never come back would hide the fault. Fix the program or remove the setting.
- **The capacity rule gives no answer** (`heavy facts` broken, slow, or the machine file broken): not free, so
  the job waits and gives up after `--wait` with exit 76, the reason in the last waiting line. Never a start on a
  missing answer. A kit or machine on which it can never answer free (the rule not installed, a broken machine
  file, the Basic tier from the machine's cgroups: no cgroup v2, neither a user manager nor `user.slice`, or a heavy
  slice that cannot be listed) refuses at once with exit 1 and a line saying why, instead of waiting `--wait` for
  nothing.
- **A live ticket that cannot be read** names no scope, so its job's scope is counted again as a job without
  `--kind`: counted twice, the safe side.
- **The admit command hangs, or its leftover holds its output:** not now after `admitTimeoutSeconds` (or 1 s after
  it exited); its process group is killed and the job asks again on the next look.
- **One leaky run of a kind:** its peak counts (failed runs too), but the learned value is capped at the budget
  minus `vmReserveMb`, so the kind is still admitted while other jobs use the slice.

## Measured need and the capacity front

Before an agent starts a build, a test run or a server it asks the machine's capacity check whether there is room
(`<check> 1536 && start`). The number it asks for was a guess per kind of job. `heavy --measure` writes down what
each run really used, and `heavy capacity` turns a kind name into an amount from those runs. The rule is to fail
toward asking for more: the amount never goes below the kind's floor (by default its old fixed number), and big
runs, stopped ones included, always count.

**Recording.** `heavy --measure <kind> <command>` (and `heavy --kind <kind> ...`, which queues; `--measure` there
names another kind for the record) looks at the job's scope every second while it runs (every 0.2 s until the
scope is seen). Each look reads `memory.current`, `memory.peak` (the kernel's high-water mark since the scope
began) and `used` (what the queue counts: `memory.current` less file cache the kernel can drop, at least the anon
memory, plus swap). The job's **need** is the highest, over all looks, of `used + (memory.peak - memory.current)`:
what it holds that cannot be dropped, plus how far above its current size it ever was. When the command ends one
line is appended to `~/.local/state/heavy/measured.jsonl` (the home folder comes from the password database, not
`$HOME`; `HEAVY_STATE_DIR` names another absolute folder for `--measure`, for tests):

```
{"at":"2026-11-21T10:00:00Z","code":0,"end":"exited","kind":"node-tests","needMb":1180,"peakMb":1210,"seconds":84.2,"usedMb":930,"v":1}
```

`end` is `exited` (the command ended by itself) or `stopped` (heavy was signalled and stopped it); `code` is the
command's exit code (negative: the signal it died of), so fast failures can be told apart later. A nested heavy and
`HEAVY_KEEP=1` record nothing. A failed write is one `heavy:` line on stderr, never a failed job.

**What the need can miss** (the margin is the only cover for these):

- growth after the last look: the scope's files fail with `ENODEV` as soon as its last process ends, so up to one
  second at the very end of a job;
- a spike followed by growth of droppable file cache before the next look: the need subtracts the cache held at
  that look, so it is short of the spike by the cache that grew after it (`memory.peak` counts anon memory and
  file cache together; the kernel keeps no peak of anon memory alone);
- on a kernel without `memory.peak` (before 5.19), any spike shorter than one second (the need is then `used`);
- memory outside the scope: the agent that runs the command, and anything the command starts outside heavy.

`"measure": "used"` uses the sampled `used` alone (misses every spike shorter than a second), `"measure": "peak"`
the kernel's peak (never misses a spike while the scope lives, but counts file cache too, so an IO-heavy job's
peak grows towards the slice limit).

**The capacity front.** `heavy capacity --kinds FILE --check PROGRAM [--] [<kind> | <MB>] [args...]` runs the
check `PROGRAM` (absolute path) with the caller's arguments, except that a first argument that is a kind becomes
its amount in MB:

- **none, an empty one, or a whole number:** the check gets the same arguments (an empty one stays empty, so a
  check's own default still applies), and the kinds file is not even read;
- **a kind of the kinds file:** its amount (below), then the other arguments;
- **any other word:** nothing runs, exit 2 (a typo must not reach a check that would read it as 0).

The amount of a kind:

1. its runs: the last `recentRuns` (at most 1000) records of the kind, **stopped runs too** (a run that was stopped,
   often for growing, needed at least what it had);
2. the **highest** figure of those runs, plus `marginPercent`, rounded up to `roundMb` (the highest, not a
   percentile: two big runs among 48 small ones are the ones the check must leave room for);
3. never below the kind's **floor** `minMb` (default: its `fallbackMb`), and never below its `fallbackMb` while it
   has fewer than `minRuns` runs (no runs: the `fallbackMb`);
4. at most the kind's `maxMb` (default twice its `fallbackMb`), so one runaway run cannot block a kind for good.

So by default a kind's amount only ever rises above its old fixed number; a project that trusts its measurements
lowers a kind's floor on purpose (`"minMb": 2048`), and a few tiny or mis-tagged runs still cannot pull it below that.

**Agent kinds.** When the ask starts an agent (a writer that will run `npm test`), the measured command is not
everything that starts: the agent itself (a few hundred MB, outside heavy) is never in a scope. Mark such a kind
`"agent": true`; its floor is then its `fallbackMb` and cannot be set lower (the kinds file is refused), because no
run can show that the agent needs less. Its measured runs only raise it. A kind that nothing runs under heavy (a
read-only reviewer) is `"measured": false`: it always gets its `fallbackMb`, and `--show` says so.

The front reads the history of the user that runs it, or the folder `--state-dir DIR` names; it never reads
`HEAVY_STATE_DIR`, so a test setting that leaks into an agent's shell cannot size real starts. The check runs with
`HEAVY_CAPACITY_KIND` (the kind, or empty) and `HEAVY_CAPACITY_FRONT=1`; its stdout and exit code are heavy's. A
check that calls the front again (`HEAVY_CAPACITY_FRONT=1` already set) is refused, exit 2.
`heavy capacity --kinds FILE --show [--json] [<kind>...]` prints each kind's amount and how it was reached.

**Two histories, two questions.** The queue's `peaks.json` (above) decides when a queued job starts inside
heavy.slice; `measured.jsonl` decides what the capacity check is asked before an agent starts something. Neither
reads the other, so the same kind name can have two figures: the queue's for its reservation, the front's for the
check. Nothing picks one over the other.

**The kinds file** is the project's, not the kit's: its own file, or the `"capacity"` object of a larger project
config. Example ([capacity-kinds.example.json](capacity-kinds.example.json)):

```json
{
  "version": 1,
  "marginPercent": 20,
  "minRuns": 5,
  "recentRuns": 50,
  "roundMb": 128,
  "measure": "need",
  "kinds": {
    "review": { "fallbackMb": 1024, "agent": true, "measured": false },
    "node-tests": { "fallbackMb": 1536, "agent": true },
    "gradle-build": { "fallbackMb": 3072, "minMb": 2048, "maxMb": 6144 }
  }
}
```

Only `version` and `kinds` (each with `fallbackMb`) are required; the other values are the defaults shown. An
unknown key, a wrong type or a value out of range refuses the file (exit 2 for a kind; a plain number still works).

**Switching a machine to it** (owner): see the pull request that added this section for paste-ready steps. In
short: install, write the kinds file, put a front script between the agents' capacity command and the check, and
only then let agents ask by kind. **Going back is the other order:** first agents ask with numbers again, then the
front goes. A check reached without the front gets a word: a `sh`/`dash` check aborts on it (fails closed, every
start waits), a `bash` check reads it as 0 (fails open). A check should therefore refuse a first argument that is
not a whole number itself, for example as its first line:

```sh
case "${1-}" in *[!0-9]*) echo "NOT OK: '$1' is not a number of MB (a kind needs the capacity front)" >&2; exit 2 ;; esac
```

### The history runs again, stops half-way, or meets old or empty state

- **First run, no history:** every kind gets its `fallbackMb` until it has `minRuns` measured runs.
- **Missing or unreadable history** (gone, a folder or a link in its place, no permission): missing is the first
  run; unreadable is not trusted, and every kind gets its `fallbackMb`. Never 0.
- **Corrupt history:** a line that is not a record of this version is skipped. More than one such line and more
  than 1 in 10: the whole history is not trusted (fallback for every kind) until rotation drops the bad lines.
- **Two writers at once:** each record is one `write` of one line to a file opened with `O_APPEND`, so lines of
  jobs that end at the same instant never mix and none is lost.
- **A writer cut off half-way** (a line with no newline): the next writer starts on a new line, so only the cut
  line is lost.
- **A job killed before it records** (heavy killed with -9): no line, and the history is as before; the guard
  still stops the scope. A job stopped by a signal to heavy is recorded as `stopped` and counts.
- **Runs again forever:** past 256 KiB the file is renamed to `measured.jsonl.1` (the older one goes), under a lock
  nobody waits for, after checking the size again under the lock; at most two files, 512 KiB. A lock file that
  cannot be opened does not stop the rotation (the history stays bounded); a rotation that fails is a note, the
  record is kept. A reader reads at most the last MiB of each file.
- **A read during a rotation:** the reader opens the current file, then the rotated one, then checks the current
  name is still the file it opened; if a rotation came in between it reads again (at most 5 times, then the
  history is not trusted: fallback), so it never sees one file twice or misses the older one.

## Leftover sweep

heavy cleans up after its own command, also when it is killed (its guard, above). Some things still escape: a scope
whose heavy and guard were both killed, test runs started without heavy, and Docker containers without the owner
label (the Docker daemon starts them, so they are never in heavy's scope). `heavy sweep` finds them. A user timer
(`heavy-sweep.timer`, installed with `--sweep-timer`) runs it every 5 minutes for each user who enables it. It only
ever looks at the processes and containers of the user it runs as.

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

It also removes the old notes of `heavy docker-label` (above) whose scope is gone, which heavy and its guard both
killed with -9, or a kept scope that ended, leave behind (in `~/.local/state/heavy/labelled/` they would stay for
good): only an empty file named like a heavy scope that systemd did not list when the sweep began, and that was
already `minAgeSeconds` old then, in a folder heavy itself would use (`labelled/`, `heavy/` and the base each a
real folder of the user's that no one else can write). A live run's note stays.

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

**Look only: `heavy leftovers`.** Names everything above that is older than a limit, each with its owner and
age, and stops, removes and writes nothing (no log, no report, no lock), so a capacity check or a person can run it
before a heavy job, as often as wanted:

```
$ heavy leftovers --older-than 1h
heavy leftovers 2026-01-01T12:00:00Z: 2 older than 1h0m, look only, default rules (no heavy.sweep settings)
  would stop heavy-pytest-0a1b2c3d.scope (3h0m old): its heavy is gone; 2 processes
  report only: container 3f2a9c1b7d4e old-db (mariadb:11, 1d5h old, running); no owner label
```

`--older-than` takes seconds or `30m`, `2h`, `1d`; the default is `minAgeSeconds`. One limit for all: an idle
leftover is named as soon as it is older than the limit (the sweep waits for `longLivedSeconds` before it counts
an idle one). The owner is the unit a process runs in, the heavy scope itself, or a container's owner label (`no
label` for one without). `would stop` and `would remove` say what a sweep with stopping on would do; the rest is
only reported. It works without the `heavy.sweep` settings (the defaults, which have no `testPatterns`) and
refuses a broken machine file like the sweep. `--json` gives `{"v": 1, "olderThanSeconds", "defaults", "items":
[{"kind", "action", "owner", "ageSeconds", "why", ...}], "notes"}` (an empty scope has `"ageSeconds": null`).

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
start time. The scope is the one `heavy docker-label` runs in (its own cgroup): a process that left heavy's scope
(`systemd-run --scope` from inside the command) still has `HEAVY_SCOPE` but gets the process owner, since heavy
would remove its container when the scope ends (or is refused under a shell, which exits at once: pass
`--owner-pid`, below); `HEAVY_SCOPE` counts only when the cgroup cannot be read. A
container whose label names another user is never touched. Under heavy, heavy itself removes
the container when its command ends or heavy is killed (above); the sweep is for a label with an owner pid and for
a scope whose heavy and guard are both gone.

**Setup scripts outside heavy.** The owner must be the process that lives as long as the container is in use:
the test, not a setup script, Makefile line or `npm` script that starts the container and exits (the sweep
would then remove it under the running test). So outside heavy, `heavy docker-label` refuses (exit 1) when its
caller is a shell or a script runner (`sh`, `bash`, `make`, `env`, `npm`, ...), and prints nothing: `--label ""`
then makes `docker run` fail, so no container starts without an owner (with `--value`, check the exit code: an
empty label value is accepted by Docker, and the sweep only reports such a container, and only while it runs). A
setup script names the test process with `--owner-pid PID` (`--pid` is the same), for example a script the test
runs as its child:

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
| `heavy leftovers` with no machine file, no section or no `sweep` key       | the defaults; it only looks                                                   |
| `heavy leftovers` run again, or while a sweep runs                         | the same answer (no lock, no state of its own); never in the sweep's way      |
| no machine file, a broken one, no section, an unknown key, a newer version | refused with the reason; nothing checked or stopped; exit 1                   |
| the timer fires before the file exists                                     | the same refusal, every 5 minutes, until the owner writes it                  |
| the systemd user manager unreachable                                       | refused; nothing stopped                                                      |
| a scope that does not stop                                                 | its processes are stopped one by one; any still running is a problem (exit 1) |
| a report folder that is a link, a file, another user's or group-writable   | refused; nothing checked or stopped                                           |
| a detached heavy (`nohup heavy ...`) of any age, its scope live            | never stopped or reported                                                     |
| an install stopped half-way, or a rollback to a release without the sweep  | the timer is written only after the new release is in use, and runs the       |
|                                                                            | sweep of the release in use; a release without one skips it (below)           |

## Facts: may one more piece of work start?

`heavy facts [--kind KIND] [--mb MB] [--fresh]` answers with one free/v1 object (`fork-kit/common/FREE.md`): exit
0 free, 75 busy, anything else not available. It runs the capacity rule (`packages/shared/src/capacity`, installed
as `capacity/` next to the kit; `lib/capacity_rule.py` is the kit's evaluator of it and passes the same vectors as
the TypeScript one) on a ledger read from the kernel. Memory, swap, PSI, cores and container limits (the cgroup
root's `memory.max` and `cpu.max`) come from `lib/capacity_readings.py`, the port of the shared reader that runs the
same `capacity-readings.json` cases. Pieces: heavy's live queue tickets (admitted jobs with their scope's demand;
waiting ones are counted only; every ticket is a job: of the rule's kind for its name, `build` is `job-build`,
else of a kind the rule does not know, `unknownKinds.job`, never an agent), heavy's scopes in the slice with no
live ticket (jobs without `--kind`: each one job of `unknownKinds.job` with its scope's demand, counted once), T3's
agent scopes (every scope right below an
`app-t3code*.slice` of the user manager, whatever its name, an escaped label too; a `t3code-*.scope` elsewhere
below the manager counts as well, with a note; no kind label yet, so each counts as a writer), and the same two
of every other user, read from their user managers' cgroups (so a CI runner under its own user sees this user's
work, and the other way round). Learned job costs come from the queue's `peaks.json`; a job owes its kind's cost, or
heavy's own reservation for it (its `--mb` or learned peak) when that is more (the rule counts a piece's declared
size as a floor of its cost). The object adds `fits` (how many more of each kind would start, one
after another, up to the group's ceiling), the memory budget, the queue and notes.

- **--kind K:** does the rule admit one piece of K (`agent-writer`, `job-build`, or heavy's own `build`). **--mb
  MB** alone: do MB fit the free budget at level "ok"; with --kind, the piece declares MB (a floor of its cost) and
  the rule alone decides, its job floor too (one job may start above the free budget while nothing else runs and
  the level is not severe).
- **T3 down or not installed:** no agent scopes, still an answer from the kernel.
- **Tiers:** full with cgroup v2 and a systemd user manager. Machine with cgroup v2 and `user.slice` but no user
  manager of the caller's (a CI runner's user without lingering or a login session): it has no agents or heavy
  jobs of its own, every user's are counted as usual, and so are other people's reservations (`tier: "machine"`, a
  note, and the reason says "machine-wide numbers"). Its limits:
  - It reads only each person's default `~/.config/t3-capacity/reserve.d`, not a `T3_CAPACITY_RESERVE_DIR` they
    set for themselves.
  - It reads the people of `/etc/passwd` with a UID from `UID_MIN` to `UID_MAX`, and anyone with a `user.slice`
    session. Not read: root and system users (below `UID_MIN`), people known only through NSS (LDAP, sssd) who have
    no session now, homes that are `/` or not absolute, and, for a person with no session now, a home behind an
    automount trigger (`autofs`) that is not mounted: reading it on every call would mount every home. Their
    reservations count again from their next login (then the home is mounted). A mount table that cannot be read
    or is longer than 4 MiB (a note), or a `user.slice` that cannot be listed (sessions cannot be known, a note):
    every home is read, as before. Mount points and homes are compared as the file system names them, so a
    non-ASCII home (`\303\274` in the mount table) matches its mount.
  - Each `.conf` there must be a plain file: a link (stow-style dotfiles) is refused and makes the tier Basic.
  - A person's `reserve.d` it cannot read (a home closed to others, mode 750 or 700), or a user with a session but
    no home it can find, makes it Basic.
  - A caller that is not root cannot see another person's working folders, so their reservations hold their whole
    size, and their agents' debts are counted too. That is stricter than a caller with a user manager, never looser.

  Without cgroup v2, or without both a user manager and `user.slice` (Basic), agents and other users' work cannot
  be counted, so the answer is never free and every `fits` is 0 (a note says why). An other user's folder that
  exists but cannot be listed is Basic too. heavy's own check refuses at once where the machine's cgroups make it
  Basic (Memory queue, above), as no wait would change them.

- **Unreadable signals** (PSI, a box with a live piece but without its `memory.pressure`, or with no folder at
  all) give "elevated", never "ok"; a missing MemAvailable or a container limit that cannot be read gives busy for
  every kind. A box with no live piece of its own passes no box pressure (as the server's observer). A piece whose
  `memory.stat` is cut short or broken owes its whole cost.
- **Reservations:** files `~/.config/t3-capacity/reserve.d/*.conf` (or `$T3_CAPACITY_RESERVE_DIR`), lines
  `<MB or kind> <absolute folder> [until=YYYY-MM-DD] [ttl=DUR] [idle=DUR]` (DUR: `90s`, `30m`, `2h`, `1d`). A kind
  in place of MB (`build /x/app/`) is sized by the rule's cost of one piece of it, learned from heavy's peaks for a
  job kind (an unknown name: the job default), so the room kept follows what that work really used. `ttl=` ends the
  line DUR after its file was last written (the writer touches the file to keep it). `idle=` lets it hold nothing
  once no process of its user works in its folder (or below it, in a deeper reservation's folder too) and its file
  was not written for DUR; it applies only where
  those processes can be seen (the caller's own reservations, or any for root), else the line holds. A bad
  `ttl=`/`idle=` or another word after the folder is a note and the line holds as without it. The folder matches itself and what is below it, with or without a
  trailing `/`, never a sibling sharing its first letters (`/x/app` is not `/x/app-x`); its real path (links
  resolved) matches too. A reservation holds back its MB less what its user's processes working in that folder
  already use, and less what agents and heavy jobs with a process working there still owe the rule (their cost
  less their demand, `owedMb`): that room is already held as their debt, so it is counted once (one idle 300 MB
  agent under a 1536 MB reservation holds 1536 MB in all, not 1536 + 768 - 300). Each process counts toward one
  reservation only (the deepest folder that holds it, then the first by file name), and a piece's debt is split
  over its processes by their use, so with two or nested reserved folders nothing is taken off twice. A caller working
  inside the folder (its real working folder) is not held back by its own reservation. Ended, broken or unreadable
  lines hold nothing and give a note. Every person's reservations count in the full tier too (as in the machine
  tier, above), so one person's reservation holds against another person's work; in the full tier one that cannot
  be read (a home closed to others, as most are by default) is a note, not "never free": make
  `~/.config/t3-capacity/reserve.d` and the folders above it readable to others for it to be seen.
- **Holds (machine file):** a cgroup outside the user managers, such as a CI runners' slice, is not in the ledger.
  `heavy.capacity.holds` names such cgroups; one that uses more than `inUseFromMb` (anon + shmem + swap) keeps its
  budget (its `memory.max`, or `budgetMb` when it has none) less the RAM it uses (anon + shmem: `memory.max` limits
  RAM only, so swap is left out), as a reservation named by its `label`, for every user except its own (`users`).
  Not there: nothing runs in it. There without a memory controller (no `memory.stat`): its whole `budgetMb` is held,
  with a note. Its use cannot be read: the whole budget is held, with a note. A reservation file named like a hold
  (`<label>.conf`) gives a note; both hold.
- **Cache:** `/run/user/<uid>/heavy-facts-v<version>-<hash>.json`, one file per kit version and settings (slice,
  queue, reservations folder, user, holds), so two callers of one user with other settings, or an old and a new
  kit, never share one and each one's hold-down steps down on its own (or one file, `$HEAVY_FACTS_CACHE`), 0600,
  read again after 5 s (`--fresh`: now). A user without `/run/user/<uid>` (no user manager) uses
  `heavy-facts-<uid>/` in the temporary folder (`$TMPDIR`, else `/tmp`, never the working folder), made 0700. When
  that one is a link, another user's or open to others (someone took the name first), or there is no temporary
  folder this user can write, it uses `~/.cache/heavy-facts-<host>-<machine>/` (made 0700; `<machine>`: the
  first 16 characters of `/etc/machine-id`, else of the boot id, so two machines of one home and host name never
  share it; the previous kit's `heavy-facts-<host>/` is never read or removed) with a note, if the home (`$HOME`,
  else the password database's) is this user's, no one else can write it, and no folder above it lets someone else
  swap it (one not root's or this user's, or one others can write that is not sticky like `/tmp`). None of these: no
  cache (a note). The folder is opened once without following a link, and every cache and hold file is reached
  through that open folder, so one swapped for a link after its check is never followed. Pressure levels carry from it (at most
  5 minutes old) so calm must last before a level drops. Without a cache folder, with a cache file that is there
  but cannot be used (broken, from the future, other settings in `$HEAVY_FACTS_CACHE`), or with no file of its own
  yet but another cache file of this user's written in the last 5 minutes beside it (another version, as once after
  an update, or other settings), nothing carries, so the call starts "elevated" (only the rule's floors admit; never
  straight to "ok"). Cache files of other settings or versions older than 5 minutes are removed.

Two thin callers come with it:

- `bin/t3-capacity [--kind KIND] [MB]`: the older host check's output (first line `available=..MB swapfree=..MB
  load=..`, available less the rule's whole reservation hold, `reserved=` what admitted jobs and agents may still
  grow into (so `available - reserved` counts each once), last line `OK` or `NOT OK: why`, exit 0 or 1), now
  from `heavy facts`. MB defaults to 3072. **Disk floor (#982):** the first line ends with `disk=<GB>GB`, the free
  disk of the work filesystem (`heavy.disk.path`, the home folder by default; what this user may still write). Under
  `heavy.disk.floorGb` (25 GB by default; 0 turns it off: the disk is not read at all and the first line ends with
  `disk=off`) it is NOT OK, and the reason is in the first line too (`disk=12GB<25GB`) as well as the last
  (`NOT OK: disk: 12 GB free on /home/u, under the floor of 25 GB (heavy.disk.floorGb)`, the memory's reason after it when that is NOT OK as well). A disk it cannot read
  (`disk=?`) or a broken `heavy.disk` is NOT OK too, naming it. The disk is read on each call: no hold, no cache. The installer links `/usr/local/bin/t3-capacity` to it, so the command
  has one source, the release in use (a host's own plain copy there only with `--replace-t3-capacity`: Install,
  below); `heavy --self-check` warns when the `t3-capacity` found on PATH is another copy (its readings can differ:
  an older copy, for one, counts no reservations). **A NOT OK holds 20 s** for the same question (kind and MB): an
  OK read within that time is `NOT OK: held: ...`, so a check that flips between OK and NOT OK within seconds never
  lets work start just before it turns NOT OK again (the time starts at each NOT OK read from the machine; a held
  answer does not extend it). heavy's own check before a job holds the same way (Capacity first, above). It is kept
  in `t3-capacity-hold.json` in heavy facts' cache folder, read and written under `t3-capacity-hold.lock` (two NOT
  OKs at once are both kept; a lock that stays taken 2 s: the answer without the hold, with a note); one that
  cannot be read (broken, another owner's, a link, open to others) holds 20 s from when it last changed and is
  written again, and when it cannot be written either (a folder it may not write) it holds no longer than that; a
  time in the future (the clock went back) holds and starts again, and where it cannot start again (nothing can be
  written) it does not hold, with a note, so no hold lasts past 20 s; no cache folder: no hold, with a note.
- `bin/agent-slots` (not linked onto PATH: a host wires it in itself): a Claude Code hook (UserPromptSubmit, Stop)
  for orchestrator sessions: free agent slots are the smaller of the process limit and the machine's `fits` for
  its kind. `--help` prints its usage: heavy-install's self-test starts each of the three commands of a new copy
  (`bin/heavy`, `bin/t3-capacity`, `bin/agent-slots`) with it before the copy is switched in.

A `heavy facts` that does not answer within 5 s (it is killed), fails or prints something else is never free for
either caller.

**CPU brake (#1047).** Memory alone admitted a build on a machine whose every core was taken by work outside heavy
(containers its user had started). The rule's `cpu` (`capacity-rule.json`) brakes one more job: busy when the
CPU pressure (`some avg10`) is at least `pressureSomeAvg10Pct` (80). The pressure is the machine's
(`/proc/pressure/cpu`), or in a container with a CPU limit (the cgroup root's `cpu.max`) the container's own
(`cpu.pressure` there): the host can be busy while the container's share is free. Only where no pressure is read
does the 1-minute load decide, at `loadPerCorePercent` (150) of the host's online CPUs (the load is the host's; it
counts tasks waiting for I/O too, so it never overrides a pressure reading). It brakes
jobs and MB alone (which asks for a job), never agents, which mostly wait on their model; the job kinds' `fits` are
then 0, and the reason names the reading (`NOT OK: cpu pressure 93% (some avg10; the brake is 80%)`). A missing or
broken CPU PSI or load brakes nothing (the memory rule still decides). The shared TypeScript evaluator does not read
`cpu`: it brakes the kit's callers (`heavy`, `t3-capacity`) only. A cache from an older
kit is not read (cache version 5), so the first call after an update starts "elevated", as after any update.

**Containers outside heavy.** Their memory is counted where it is used: the machine's available memory (and a
container's own `memory.max`, read as the reader's container cap) already leaves out what every container holds
now. What a container may still grow into is not: most have no limit, and their cgroups sit in places that differ
by engine (rootful Docker's `system.slice/docker-<id>.scope`, rootless Docker's under its user's manager, Podman's
`libpod-*`, Kubernetes' `kubepods`), so no cheap and general read gives a budget. To keep room for them, give their
slice a hold (`heavy.capacity.holds`, below) where it is one fixed cgroup outside the user managers, or a
reservation (`reserve.d`) for their work folder.

**Swap trend, per read.** Swap is "filling" when its free MB fell by more than `swapFillingMb` (64) since the last
read. That limit is per read, not per second: `heavy facts` reads at most every 5 s (its cache) and carries the last
reading up to 5 minutes, the T3 server reads every 30 s and on each ask, so swap that fills slowly can be "filling"
for one and not for the other. A machine with the kit from before the swap trend keeps the old swap rule until
`heavy-install` runs again from a newer source.

### Facts run again, stop half-way, or meet old or empty state

- **No user manager** (no `/run/user/<uid>`): Machine tier, and the cache in the temporary folder, so the level
  carries and can reach "ok" as with one.
- **Runs again:** within 5 s the cache answers; two callers in the same second both answer and leave one valid
  cache (each writes its own temporary file and renames it).
- **Stops half-way:** a temporary cache file is left; the next run removes ones older than a minute.
- **Old, empty, broken or foreign cache** (another owner, group or other access, a link, a time in the future, an
  unknown key or version (an older kit's cache: version 2 without each reservation's `owedMb`, version 3 with piece
  caps in the machine tier), or written with
  other settings: slice, queue or reservations folder): read again from the kernel. A cache that cannot be written
  gives a note.
- **Cost file** (`peaks.json`) missing, empty or broken: the rule's start costs, with the queue's note.
- **Reservation file cut short** (its writer stopped half-way): its whole lines hold, the cut line is a note.
  A reservation whose processes are all gone still holds all its MB (the room is kept for them), until its
  `until=` date, its `ttl=` or `idle=` time, or until the line is removed. A writer that stopped renewing its file
  (killed, crashed) leaves a line with `ttl=` that ends by itself; one without it holds until it is removed.
- **Machine file** missing, or without `heavy` or `capacity`: no holds. Broken (not JSON, an unknown key, a wrong
  value, a `user.slice` cgroup, a label twice, a cgroup in or the same as another hold's, more than 8 holds,
  writable by others): `heavy facts` gives no answer, so
  `t3-capacity` says `NOT OK` naming the key. A changed file is read again at once (the cache key holds it).

### Settings: the machine file's `heavy.disk`

```json
"heavy": { "disk": { "floorGb": 25, "path": "~" } }
```

| Key       | Default | What                                                                                   |
| --------- | ------- | -------------------------------------------------------------------------------------- |
| `floorGb` | 25      | `t3-capacity` is NOT OK with less free disk than this (GB); 0: no floor, disk not read |
| `path`    | `~`     | a folder on the work filesystem (absolute, or `~` / `~/...`: the caller's home folder) |

Runs again: each call reads the disk afresh, so a freed disk is OK on the next call. Old or empty state: a machine
file without `disk` gets the defaults; the sweep, the queue and `heavy facts` accept the key and leave it to
`t3-capacity`.

### Settings: the machine file's `heavy.capacity`

Optional, in `/etc/fork-kit/machine.json` (`fork-kit/common/README.md`):

```json
"heavy": {
  "capacity": {
    "holds": [{ "label": "ci-slice", "cgroup": "ci.slice/ci-runners.slice", "users": ["ci-*"] }]
  }
}
```

| Key in a hold | Default  | What                                                                             |
| ------------- | -------- | -------------------------------------------------------------------------------- |
| `label`       | required | the name in `reservations:` (lower-case letters, digits, `_ . -`; at most 40)    |
| `cgroup`      | required | its folder below the cgroup root; never below `user.slice` (counted already)     |
| `inUseFromMb` | 1024     | held only while it uses more than this                                           |
| `budgetMb`    | 6144     | its budget when its `memory.max` is `max`, cannot be read or is not there        |
| `users`       | `[]`     | user name patterns (`fnmatch`) whose own budget it is: never held back from them |

At most 8 holds, none in another's cgroup (it would hold the same room twice). The sweep and the queue accept the
`capacity` key and leave it to `heavy facts`.

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

| Path                                                       | What                                                                      |
| ---------------------------------------------------------- | ------------------------------------------------------------------------- |
| `/usr/local/bin/heavy`                                     | link to `current/bin/heavy`                                               |
| `/usr/local/sbin/heavy-install`                            | link to `current/sbin/heavy-install`                                      |
| `/usr/local/bin/t3-capacity`                               | link to `current/bin/t3-capacity`                                         |
| `/usr/local/libexec/heavy/versions/<sha>/`                 | the code of one release, with `MANIFEST.sha256` (root 755)                |
| `/usr/local/libexec/heavy/current`                         | link to the release in use (switched in one step)                         |
| `/usr/local/libexec/heavy/replaced/`                       | a plain `/usr/local/bin/heavy` from before the kit, put back on uninstall |
| `/usr/local/libexec/heavy-kept/`                           | a plain `/usr/local/bin/t3-capacity` (a host's copy), put back likewise   |
| `/etc/heavy/config.json`                                   | slice name and grace time                                                 |
| `/etc/heavy/installed.tsv`                                 | every installed path with its link target or hash (`--self-check`)        |
| `/etc/systemd/user/<slice>`                                | only with limit options: the slice unit                                   |
| `/etc/systemd/user/heavy-sweep.{service,timer}`            | only with `--sweep-timer`: the leftover sweep (each user enables it)      |
| `/etc/fork-kit/machine.json`                               | written by the owner, never the kit: sweep and queue settings             |
| `~/.local/state/heavy-sweep/`                              | each user's sweep log, last report and lock                               |
| `/run/user/<uid>/heavy-queue/<slice>/`                     | the memory queue: its lock and one ticket per queued job (tmpfs)          |
| `~/.local/state/heavy/`                                    | the queue's wait log (`waits.log`) and peak history (`peaks.json`),       |
|                                                            | measured runs for `heavy capacity` (`measured.jsonl`, `.1`, `.lock`)      |
| `/run/user/<uid>/heavy-facts-*.json`                       | `heavy facts`' 5 s cache (tmpfs), one per version and settings            |
| `/tmp/heavy-facts-<uid>/heavy-facts-*.json`                | the same, for a user without `/run/user/<uid>` (no user manager)          |
| `~/.cache/heavy-facts-<host>-<machine>/heavy-facts-*.json` | the same, when the temporary one is taken or cannot be written            |
| `<that folder>/t3-capacity-hold.json`                      | `t3-capacity`'s 20 s hold of a NOT OK                                     |
| `~/.config/t3-capacity/reserve.d/*.conf`                   | each user's reservations for `heavy facts` and its callers                |

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
`/usr/local/sbin/heavy-install uninstall` (puts an old plain `/usr/local/bin/heavy` and `/usr/local/bin/t3-capacity`
back). Check: `heavy --self-check` (as the user: config, slice and files, and a warning when `t3-capacity` on the
user's PATH is not the kit's) or `heavy-install self-check` (files only).

`/usr/local/bin/t3-capacity`: with nothing there, or the kit's link, the installer links it. A plain file there is a
host's own copy, which the host's scripts may read or compare: the installer leaves it as it is and says so (in a
note and again in its closing `installed release` line), until it is run once with `--replace-t3-capacity` (it
keeps the copy in `/usr/local/libexec/heavy-kept/`, links the kit's, and later runs keep the link without the
option). A link there that the kit did not make is left alone the same way; with `--replace-t3-capacity` it is
refused (remove it by hand first: there is no file to keep). A host whose own installer wrote that copy first changes it to stop writing
that path and to run `heavy-install` (or check that the path is the link to
`/usr/local/libexec/heavy/current/bin/t3-capacity`): a copy over the link would land in the release's own folder
(`heavy-install self-check` then says `changed since install`), and one written beside it and renamed over it puts
a plain copy back (`heavy --self-check` says the link differs and warns about the copy).

## Runs again, stops half-way, old or empty state

- **Runs again:** every step checks what is there and only fixes what differs; a second run changes nothing.
- **Stops half-way:** the steps run in this order: check, folders, copy, self-test, config, slice unit, switch,
  links, sweep units, record. A version folder is copied to a temporary name and renamed; a half-copied or changed
  one is made again. The old `/usr/local/bin/heavy` and `/usr/local/bin/t3-capacity` keep working until the links
  step, which copies each to its kept folder before its link replaces it in one rename. The sweep's units are
  written only after that, so a timer never runs before the release it needs is in use. Running the same line again
  finishes the job, and removes a temporary link (`<path>.new-link`) a stopped run left. Uninstall puts each kept
  copy back over the kit's link in one rename (never a moment without the command), then removes what a stopped run
  left: a `.restore` copy, a `.new-link`, a half-kept `heavy-kept/*.tmp`, and `heavy-kept/` once it is empty or its
  copy is back.
- **Rollback:** the timer's service runs `/usr/local/libexec/heavy/current/lib/heavy_sweep.py` (the sweep of the
  release in use, never an older `heavy` without the `sweep` verb), and only when that file exists. Before going
  back to a release without the sweep, run the newer installer with `--no-sweep-timer`; if you did not, the
  timer just does nothing (users run `systemctl --user disable --now heavy-sweep.timer` to clear it). The older
  installer drops the setting from `config.json` but keeps the units; rolling forward again with the usual line
  (without `--sweep-timer`) keeps them and says so. If one of the two units there is not the kit's, that line
  refuses and names the file; remove it, or run with `--no-sweep-timer`.
- **Rollback past `heavy.capacity`:** remove the `capacity` key from the machine file's `heavy` section first; a
  release from before it refuses the whole section (as below).
- **Rollback past the memory queue:** remove the `queue` key from the machine file's `heavy` section first. A
  release from before the queue does not know it: its sweep refuses the whole machine file and says which key. That
  release cannot be changed from here, so the order is the guard: key out, then the older release.
- **Rollback past the `t3-capacity` link:** the link names `current/bin/t3-capacity`, the form every installer from
  ci/v23 on knows as its own, so a rollback to any of them switches `t3-capacity` together with `heavy`, with no note
  (#1095). The trade-off is a rollback to ci/v20 or older, which has no `bin/t3-capacity`: its install switches
  `current` and the link then points at a missing file (every `t3-capacity N && …` gate fails). So before installing
  ci/v20 or older, run this release's `heavy-install uninstall` first: it puts the host's old copy back (from
  `/usr/local/libexec/heavy-kept/`) or, where there was none, removes the link, and the older release then leaves
  `/usr/local/bin/t3-capacity` alone. If the older install ran first, either line mends it: this release's install
  (rolling forward) or its uninstall. The old copy is kept in `/usr/local/libexec/heavy-kept/`, outside
  `/usr/local/libexec/heavy` and `/etc/heavy`, because an older installer's uninstall deletes those two folders whole
  (and puts the newest file of `replaced/` back as `heavy`). An uninstall from ci/v22 or older does not know the
  `t3-capacity` link, so it leaves it broken: run this release's `sbin/heavy-install uninstall` from its source folder, as root,
  which removes the broken link and puts the kept copy back, or install again.
- **Old state:** a plain-file `/usr/local/bin/heavy` (the one-line wrapper from before) is kept and put back by
  uninstall; a plain `/usr/local/bin/t3-capacity` (a host's own copy) is left alone, or with `--replace-t3-capacity`
  kept and put back by uninstall. A link at `t3-capacity` that points elsewhere is left alone (refused with
  `--replace-t3-capacity`). A link at `heavy` or `heavy-install` that points elsewhere, a folder where the kit
  expects its own, or a slice unit in `/etc/systemd/user/` the kit did not write is refused with a message, never
  overwritten. Config
  values stay unless an option names a new one.
- **Empty state:** a fresh machine installs with the defaults. heavy then refuses to run commands until the slice
  has a unit with a memory limit (install with limit options, or add the unit).

## Tests

`python3 -I -B fork-kit/heavy/tests/queue.test.py`: the memory queue on a fake cgroup tree and meminfo with a fake
clock; real processes where the kernel matters (a waiter killed with -9, a crashed job, two callers at the same
instant). Its red check breaks each admission and clean-up rule and shows the test for it then fails.
`heavy.test.py` runs queued jobs with the real systemd user manager in a private queue folder (`HEAVY_QUEUE_ROOT`,
never the user's live queue): one that runs, one that fails and still leaves its peak, one whose heavy is killed
with -9 (its guard stops the scope, and the reservation ends with it) and one give-up (exit 76). Every heavy it
runs has `HOME`, `HEAVY_STATE_DIR`, `HEAVY_QUEUE_ROOT` and `HEAVY_FACTS_CACHE` in its test folder (checked before
each run), so a measured run never writes the tester's real history (the password database's home).

`python3 -I -B fork-kit/heavy/tests/measure.test.py`: the measured history and the capacity front in temporary
folders (`HEAVY_STATE_DIR`, `--state-dir`), a fake scope folder and a fake check script, no systemd: first run,
missing, unreadable and corrupt history, eight writers at once, a writer and a job killed with -9, rotation (its
re-check, a lock that cannot be opened, a read in the middle of one), the amount rules (stopped runs, the highest
of 50, floors, agent kinds), the kinds file and every refusal. `HEAVY_TEST_KIT=<older fork-kit/heavy>` runs it
against an older kit (the red check). `heavy.test.py` records real runs (`--measure`, a queued `--kind`, nested,
killed with -9) into a temporary `HEAVY_STATE_DIR`.

`python3 -I -B fork-kit/heavy/tests/sweep.test.py`: the sweep on a fake host (a fake `/proc`, fake `systemctl`
and `docker` from `tests/fake_tools.py`, a temporary machine file), no root and nothing real swept. Its red check
breaks each stop and report rule in a copy of the sweep and shows the test for that rule then fails. It also builds
scope names through heavy's own `run()` (`HEAVY_KEEP=1` and a command named `keep`) and checks how the sweep
treats them.

`python3 -I -B fork-kit/heavy/tests/capacity_rule.test.py`: the kit's evaluator against every vector and format
case, and its reader against every `capacity-readings.json` case, of `packages/shared/src/capacity` (the files the
TypeScript tests read); each number of the rule, changed by +1 and changed by -1, turns a vector red. `python3 -I -B fork-kit/heavy/tests/facts.test.py`: `heavy facts`, `t3-capacity` and
`agent-slots` on a fake `/proc` and `/sys` (`HEAVY_FACTS_ROOT`), a temporary queue, home and cache, and fake slow
or failing `heavy` scripts. The test-only settings (`HEAVY_FACTS_ROOT`, `T3_CAPACITY_HEAVY`, `AGENT_SLOTS_HEAVY`,
`AGENT_SLOTS_ALIVE`, and heavy's `HEAVY_CHECK_HEAVY`: the `heavy facts` its capacity check asks) are read only while `HEAVY_TESTING=1` is set too, so one that leaks into a real caller's
environment changes nothing. Its red checks take out of a copy of `lib/heavy_facts.py` the agent-scope count,
unknown kinds as jobs and the folder's trailing `/`, and show the test for each then fails.

`python3 -I -B fork-kit/heavy/tests/install.test.py` (the installer on a fake host in a temporary folder, no root)
and `python3 -I -B fork-kit/heavy/tests/heavy.test.py` (the wrapper with the real systemd user manager and a slice
with a memory limit, `HEAVY_TEST_SLICE`, default `heavy.slice`; it skips without one, and fails instead with
`HEAVY_REAL_TESTS=1`, as in CI). `HEAVY_TEST_BIN=fork-kit/heavy/tests/old-heavy.sh` runs the wrapper tests against
the one-line wrapper from before: every leftover test fails (the red check). With `HEAVY_TEST_BIN` set to the
`bin/heavy` of an older kit (`git archive <commit> fork-kit | tar -x -C <folder>`), the tests of what came later
fail: a heavy killed with -9 (the guard stops a double-forked child and removes a container), a container a test
started, a rerun of the same test after a kill (the same container name), the guard's name, a tree kill of heavy
and its children (the guard is no child), a `kill -9` of heavy's process group (the guard's own session), the
guard ignoring the signals heavy handles, a kept scope that gets no guard, a signal to heavy that removes the
container of a command outliving it (after the grace time and at a second signal), a looser `docker ps` filter
(heavy's own exact label check), Docker asked only after a run that labelled a container, a stop by hand that
never signals a pid no longer in the scope, a heavy gone before its command starts, and a Docker context. Container tests use
a fake `docker` (`HEAVY_DOCKER`, `tests/fake_tools.py`), never the machine's. Run `heavy.test.py` itself without
heavy: inside a heavy scope the heavy under test would run nested, so it refuses there; every command it runs gets
a heavy scope of its own. Its jobs ask a fake capacity check that answers free at once (`HEAVY_CHECK_HEAVY`, its
hold in the test folder), so no test waits on the tester's busy machine; one test asks the kit's own check, one a
kit copy without the capacity rule (it must refuse within seconds), and two waiters show they are admitted one at
a time. A heavy that does not end in time fails its test with what it said so far, and every later test then fails
at once, so a hang shows in a minute, never at CI's job limit.
