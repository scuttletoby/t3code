# The fork: settings and install

Fork only. This fork is T3 Code with a thin set of changes on top; every change is listed in
[FORK_CHANGES.md](../../FORK_CHANGES.md). This page lists every fork setting: what it does, its
default, and how to switch it on. Then it says how to install the fork today.

The rule: a machine whose settings never name a fork setting behaves as upstream's T3 Code. Every
fork switch is off by default, except one read-only refresh of usage limits.

## Where settings live

Server settings are in `settings.json` in the server's `userdata` folder (by default
`~/.t3/userdata/settings.json`). The server reads the file again when it changes; no restart is
needed. Fork settings also have rows on the app's own **Settings → Fork** page (named below), with
one line per feature at the top saying whether it is on, by the same rules as the fork report below
(on a server, a project's override or one thread's handoff entry counts too). A project's overrides are set in **Settings** with that project chosen at the top ("Applying settings
for …", see [project settings](../user/project-settings.md)). An agent's tools cannot change any
fork setting.

To check what a T3 home has switched on without changing anything, the fork's hidden command
`t3 __fork-report --base-dir <home>` prints it as JSON, each feature with where it is on (the server,
the managed policy's lock, a project's override or one thread's handoff entry). It reads this machine's
managed policy as a start does for settings (defaults and locks); the policy's provider limits are not
applied, and no fork feature depends on them. It never migrates, takes no lock and creates no file, with the one exception below.

Not covered: a T3 that is stopping. Run the report while T3 is running or stopped. A clean stop at
the instant the report opens the database leaves an empty `statev2.sqlite-wal` behind
([#179](https://github.com/scuttletoby/t3code/issues/179)): harmless, since T3 reuses it, but a file
the report created. The report does not remove it: a T3 starting at that moment may already be
using it. When the first open or read fails, the report checks and reads once more, at once; only a
second failure is reported. A stop still tidying the database can fail that second read too: then the
problem names the T3 server process still using the home, and a run once it has stopped reads the
stopped database.

A home with only the old V1 database (`state.sqlite`) reports its database `missing` and names the V1
file in `database.v1Import`: a start would import it. A `userdata` folder the report may enter but not
write (or a home without one that cannot take a new folder) is a problem, since a start would fail.

## Settings

The defaults, "off" and "decided in" below come from one table in the code,
[`packages/contracts/src/forkDefaults.ts`](../../packages/contracts/src/forkDefaults.ts). A test
(`packages/shared/src/forkDocs.test.ts`) fails when this table and that one differ, or a setting has
no section below. "Off" means the default leaves the machine as upstream's would be.

| Setting                                                                      | Default                     | Off | Decided in                                               |
| ---------------------------------------------------------------------------- | --------------------------- | --- | -------------------------------------------------------- |
| [`server.automaticUpdates`](#serverautomaticupdates)                         | `false`                     | yes | [#41](https://github.com/scuttletoby/t3code/issues/41)   |
| [`server.automaticUpdatesQuietCommand`](#serverautomaticupdatesquietcommand) | `""`                        | yes | [#5](https://github.com/scuttletoby/t3code/issues/5)     |
| [`server.activityProbe`](#serveractivityprobe)                               | `false`                     | yes | [#509](https://github.com/scuttletoby/t3code/issues/509) |
| [`server.usagePacing`](#serverusagepacing)                                   | `{}`                        | yes | [#52](https://github.com/scuttletoby/t3code/issues/52)   |
| [`server.usagePacingGate`](#serverusagepacinggate)                           | `"off"`                     | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)   |
| [`server.usagePacingLowerEffort`](#serverusagepacinglowereffort)             | `{}`                        | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)   |
| [`server.waitFor`](#serverwaitfor)                                           | `{"enabled":false}`         | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.waitChecks`](#serverwaitchecks)                                     | `{}`                        | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.playtest`](#serverplaytest)                                         | `{"enabled":false}`         | yes | [#260](https://github.com/scuttletoby/t3code/issues/260) |
| [`server.reviewRules`](#serverreviewrules)                                   | `{"preset":"off"}`          | yes | [#291](https://github.com/scuttletoby/t3code/issues/291) |
| [`server.modelPolicy`](#servermodelpolicy)                                   | `{"mode":"off","allow":[]}` | yes | [#268](https://github.com/scuttletoby/t3code/issues/268) |
| [`server.agentSlots`](#serveragentslots)                                     | `{"enabled":false}`         | no  | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.headlessUsageRefreshInterval`](#serverheadlessusagerefreshinterval) | `600000`                    | no  | [#42](https://github.com/scuttletoby/t3code/issues/42)   |
| [`server.mcpProfiles`](#servermcpprofiles)                                   | `{}`                        | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpProfile`](#servermcpprofile)                                     | `"all"`                     | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpSubagentProfile`](#servermcpsubagentprofile)                     | `"inherit"`                 | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpThreadProfiles`](#servermcpthreadprofiles)                       | `{}`                        | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpCarriedProfiles`](#servermcpcarriedprofiles)                     | `"apply"`                   | yes | [#640](https://github.com/scuttletoby/t3code/issues/640) |
| [`server.handoff`](#serverhandoff)                                           | `{"mode":"off"}`            | yes | [#215](https://github.com/scuttletoby/t3code/issues/215) |
| [`projectOverride.waitChecks`](#projectoverridewaitchecks)                   | not set                     | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`projectOverride.playtest`](#projectoverrideplaytest)                       | not set                     | yes | [#260](https://github.com/scuttletoby/t3code/issues/260) |
| [`projectOverride.reviewRules`](#projectoverridereviewrules)                 | not set                     | yes | [#291](https://github.com/scuttletoby/t3code/issues/291) |
| [`projectOverride.mcpProfile`](#projectoverridemcpprofile)                   | not set                     | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`projectOverride.mcpSubagentProfile`](#projectoverridemcpsubagentprofile)   | not set                     | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`projectOverride.handoff`](#projectoverridehandoff)                         | not set                     | yes | [#215](https://github.com/scuttletoby/t3code/issues/215) |

For `waitFor`, `agentSlots` and `playtest` only the `enabled` switch is pinned, and for `handoff`
only `mode`; their other fields are tuning that does nothing while the switch is off.

## Each setting

### `server.automaticUpdates`

A server installed as a boot service (`t3 service install`) updates itself to the newest fork release
once no thread is working. Off: it updates only when someone runs `t3 update` or presses **Update**.
Upstream has a setting of this name pending (not on its `main` yet); the fork keeps `false` through every sync.

Switch on: **Settings → Fork**, section Updates, "Update automatically", or in `settings.json`:
`"automaticUpdates": true`. An
admin policy with updates `"off"` still wins. Details: [updates.md](updates.md).

### `server.automaticUpdatesQuietCommand`

An optional program asked before each automatic update; a non-zero exit means "not now". Used only
while `automaticUpdates` is on. Empty: no program is asked.

Set: **Settings → Fork**, section Updates, "Quiet check before an update", or in `settings.json`:
`"automaticUpdatesQuietCommand": "/path/to/quiet-check"`. Details:
[updates.md](updates.md).

### `server.activityProbe`

Lets a program on the same machine ask the running server "is T3 busy?" without touching its data:
`GET /api/fork/activity` answers with the running, queued and waiting turns, pending approvals and
questions, background tasks and running terminal commands, counted as the automatic updater counts
them, and `quiet: true` only when all are zero. It writes nothing and needs no sign-in, so it answers
only a program on this machine: a loopback peer whose request carries no proxy headers (a Tailscale
Serve or tunnel request) and no browser markers (`Origin`, `sec-fetch-site`, `sec-fetch-dest`, a
`sec-fetch-mode` other than Node's `cors`, or a `Host` that is not loopback). The caller is checked
first: every other caller gets a plain `404 Not Found` that names neither the setting nor the fork,
whether the probe is on or off. Off, a local caller gets a 404 that says so, which the reader reports
as `off`.

Limits:

- A TCP-level forward to loopback adds no headers and lets its client choose the `Host`, so it looks
  local: `ssh -L`/`-R`, `tailscale serve --tcp`, socat, editor port forwarding. Whoever can open one
  can already reach this machine's loopback.
- The probe is asked on loopback, so the server must listen on loopback or on all addresses. A server
  bound to one LAN or tailnet address only can never be probed: the reader reports `unreachable`.
- **Advisory, not a gate an agent cannot pass.** The reader takes the port and the pid from
  `server-runtime.json`, which anything running as the T3 user can rewrite, agents included, and the
  `pid` in the answer is the server's own claim. An agent can point the reader at a port of its own
  that answers `quiet: true` with T3's pid, and the reader exits 0. The pid check only catches a
  port another program reused by accident. A gate that must hold against agents has to take the port
  and pid from a source agents cannot write (passed in by a root-owned caller, or a root-owned
  file), or check that the listening socket on that port belongs to that pid (on Linux: the socket
  inode for the port in `/proc/net/tcp` and `/proc/net/tcp6` against the links in `/proc/<pid>/fd`).

Switch on in `settings.json`: `"activityProbe": true` (read on every request, no restart). The
program to call is `t3 __fork-report --base-dir <home> --activity`: it finds the server from the
home's `server-runtime.json`, asks it, prints one line of JSON (`quiet`, then `reason`, then `state`:
`quiet`, `busy`, `stopped`, `off`, `unreachable` or `unknown`) and exits 0 only for `quiet`. Every
other state exits 1, so a quiet check that runs it fails closed: a server still starting (it answers
nothing until it accepts commands), stopping, gone, too old to know the route, or with the probe off
counts as busy. Only `quiet` and `busy` come from the server; `stopped` means no live process holds
the home. A quiet check wraps it in a script with no arguments, for example
`exec /path/to/t3 __fork-report --base-dir /path/to/home --activity`.

### `server.usagePacing`

Per provider instance (account): `reservePercent` keeps that many points of each limit window free
for the person, released over the window's last 12 hours; `burnFirst` spends this account first;
`noLimits` says the account has no usage limits (an API key). An account without an entry has no
reserve. Only the pacing gate (`usagePacingGate`) acts on it.

Set in `settings.json`:

```json
{ "usagePacing": { "<instance id>": { "reservePercent": 20 } } }
```

The key is the provider instance id, for example `codex` or `codex_work`. In the app: **Settings →
Fork**, section Usage pacing, one row per account by its name (an entry for an account that is gone
is listed with **Remove**).

### `server.usagePacingGate`

What happens when an agent starts a task with `delegate_task`. `"off"`: nothing, as upstream.
`"advise"`: the task starts as asked and the reply carries a note about the pace. `"enforce"`: a task
on an account that is ahead of its pace moves to another account of the same provider, runs at a
lower effort (`usagePacingLowerEffort`), or is refused for now and the agent asks again later.

Switch on: **Settings → Fork**, section Usage pacing, "Usage pacing gate", or in `settings.json`:
`"usagePacingGate": "advise"` (try `advise` before `enforce`).

### `server.usagePacingLowerEffort`

For `"enforce"`: what each effort level turns into while an account is ahead of pace and no other
account has room, for example `{ "high": "medium", "medium": "medium" }`. A task at a level not
listed is held instead. Empty: no effort is ever lowered.

Set: **Settings → Fork**, "Lower effort when ahead of pace" (pairs like `high=medium`; a level no
model on the server offers is refused), or in `settings.json`: `"usagePacingLowerEffort": { "high": "medium" }`.

### `server.waitFor`

Waits: an agent calls `wait_for` to wait for a pull request (checks done or merged), a file (there or
gone), a system unit, a named check command (`waitChecks`) or a free agent slot, and its thread is
woken with the result. `wait_list` and `wait_cancel` list and end them. While off, waits are neither
judged nor delivered; listing and cancelling still work. Other fields set deadlines, limits per
thread and how often each kind is checked.

The same switch turns on the delegated result reminder: when a `delegate_task` child ends and its
result has still not reached the idle parent a couple of minutes later (upstream's own wake did not
land), the parent gets one queued message naming those tasks. A result counts as unread while it is
owed, and also when it has no delivery state at all: an old row, or a `mode: "wait"` task whose
acknowledgement failed. Each task is reminded once, also across restarts and when another result
becomes owed later (the later message names only the new task); a message names at most ten tasks.
A message the thread refuses holds back only its own tasks (until the set changes); the other
messages of that check are still sent. A reminder sent before per-task ids (one id for the whole
set) is split into its tasks from its text, so after an upgrade a task it named is not reminded
again; only tasks past the first ten of such a message, which it never named, may be reminded once
more. The check at start and a check after an event never run at once for one parent.
It never goes to a stopped or archived thread, and never for results older than a day. A parent is busy, and
waits for its own run end, while a run of it goes or a question of it is open (an open question
holds the reminder until that run ends); background work it left running does not hold it. At
start, every parent with a result unread from the last day is checked once, also when upstream had
reserved no wake for it. Upstream's wake can land just before the reminder, so the message says the
result _may_ not have arrived.

The server makes a thread in a linked environment before it records the task here, so a server that
stops in between leaves a thread there that no task here follows. At start (a few minutes after,
and again every ten minutes while an environment does not answer fully or a parent cannot be read,
six scans at most), each linked environment's threads made before the start (with a minute's
tolerance for a clock that runs ahead) and changed in the last day are read. A parent that one
names as its `delegatedFrom` but no task of it follows gets a queued message naming them, with the
tools to read and stop them. Each such thread is named once: a machine that answers late, or a
later start, names only the ones not named yet. A busy parent (a question to a person included)
reads it after its current run. What is still unchecked after the last scan is logged by name and
scanned again at the next start. Off on a held start (`--hold-work`) and while Linked environments
is off. This scan stands in for an upstream gap: upstream's `delegate_task` also launches there
before it records here, and its fix (record before the launch) is not merged yet. When an upstream
sync brings that fix, slim the scan to threads lost before it, or drop it.

`task_list` (always there, read only) lists a thread's tasks with an unread result and how many of
its children run now per place, from their live status; a remote task whose stop there is still
owed is read there too. An environment that does not answer once is not asked again in the same
call. With `checkLinked`, it also lists such orphans, from each environment's newest 500 threads
(`next` names an environment with more, and one whose later page did not answer). Which slot view to trust: `task_list` for this
thread's own children (it reads each one's live status), `wait_list` for whether `delegate_task`
will be let through (it shows what the slot gate counts against `server.agentSlots`, every agent on
the machine or account).

Switch on: **Settings → Fork**, section Waits (shown while a server is connected), "Let agents wait for events", or in `settings.json`:
`"waitFor": { "enabled": true }`.

### `server.waitChecks`

Named commands an agent can wait on by name (`wait_for` with a check). Each runs as a program with
fixed arguments, no shell; exit 0 means the event happened. It runs unsandboxed in the project folder
with the server's environment, so use fixed programs that run no files from the repository (no test
runners or build scripts). Only the person can add one; agents cannot.

Add one: **Settings → Fork**, section Wait checks, **Add check**, or in `settings.json`:

```json
{ "waitChecks": { "build-done": { "argv": ["test", "-f", "/tmp/build.done"] } } }
```

Used only while `waitFor` is on.

### `server.playtest`

Playtest sessions: a person tries an agent's unfinished work on a real test target (a test server,
a staging site) while the agent changes it, round after round, without a pull request per round.
T3 runs the project's adapter, four kinds of step only the person sets: `claim` takes the target,
`renew` keeps the claim alive, `push` (one or more named steps) sends a round to the target, and
`release` puts the target back. Each step is a program given by its absolute path with fixed
arguments, run without a shell; T3 passes the session's facts as environment variables. Nothing
starts a session yet: the session store and the worker (renew, the idle and time limits, ends for
gone threads) are there, and the tool and the thread controls come in later pull requests. The steps' contract is in [playtest.md](playtest.md).

In the app: **Settings → Fork**, section Playtest: the switch (it stays off until all four steps are
set), the steps as JSON under **Edit steps** (checked as a save is), and the limits under Advanced.

Only the person can change it; agents cannot. A save that is on without all four steps, repeats a
push step's name (or names one `claim`, `renew` or `release`), or names a program by anything but
its full path on the server's OS is refused. A hand-edited
value that is not valid reads as off (with a warning in the server log), and the next save of any
setting writes the default in its place; fields a newer build added are dropped the same way (as for
`waitChecks`). Other fields: how often a claim is renewed (`renewMinutes`), when an idle
session ends (`idleMinutes`) and the longest session (`maxHours`).

```json
{
  "playtest": {
    "enabled": true,
    "claim": { "argv": ["/usr/local/bin/game-playtest", "claim"] },
    "renew": { "argv": ["/usr/local/bin/game-playtest", "renew"], "timeoutSeconds": 30 },
    "push": [
      { "name": "guard", "argv": ["/usr/local/bin/game-playtest", "guard"] },
      {
        "name": "deploy",
        "argv": ["/usr/local/bin/game-playtest", "deploy"],
        "timeoutSeconds": 600
      }
    ],
    "release": { "argv": ["/usr/local/bin/game-playtest", "release"], "timeoutSeconds": 600 }
  }
}
```

### `server.reviewRules`

How this machine runs a review lens on a pull request: which provider driver, model and effort run
it, and the state question every review asks. The repository's own config file says what a pull
request needs (its lenses, critical paths, rounds) and the CI gate enforces it; this setting is only
the machine's half. Nothing reads it yet: the review runner comes in a later pull request.

`preset` is `off` (nothing is listed, changed or checked) or `recommended`, which fills in the
fork's defaults: Claude (`claudeAgent`) with `claude-opus-5-5` at `high` effort, the `approach`
lens on `claude-fable-5-2` at `high`, the state question "What happens when this runs again, stops
half-way, or meets existing, old or empty state?", small findings left after round 2 filed as
issues (`minors: "issue"`), the config at `.github/pr-queue.json` and reports of at most 60000
characters. A field written out wins over the preset's (`lensRunners` per lens); a field written
out while `preset` is `off` does nothing. A fork update may change the preset's values; write the
fields out to keep them fixed. Efforts an agent may not use are the model policy's, not this
setting's.

Fields: `runner` and each `lensRunners` entry are `{driver, model, effort}` (a driver kind, not a
provider instance), and a lens name is not a built-in object key such as `constructor`;
`stateQuestion` is 20 to 600 characters; `minors` is `same-round`, `issue` or
`free`; `configPath` is relative to the repository, without `..`; `reportMaxChars` is a whole number
from 1000 to 65000.

Only the person can change it; agents cannot. A save must name `preset` (it replaces the stored
value whole) and is refused for a bad field or an unknown key. A hand-edited value that is not
valid (an unknown preset or key from a newer build, a bad runner) reads as off, never half on, with
a warning in the server log; it is kept as written, and saves of other settings write it back
unchanged until the person saves this setting.

In the app: Settings, Fork, Review rules shows whether the rules are on and the values a lens
would run with (each marked as the preset's or set by hand), with "Use recommended review rules"
and "Turn off" buttons; it asks first before a button drops hand-set fields or an unreadable value.

```json
{
  "reviewRules": { "preset": "recommended" }
}
```

### `server.modelPolicy`

Which models (and efforts) agents may pick, per provider driver. `mode` is `off` (agents pick any
model, as upstream's do), `advise` (agents are only told) or `enforce` (a pick off the list is
refused). Each `allow` row names a `driver` and a `model`; `efforts` (non-empty) limits the efforts
for that model, and a row without it checks only the model. `defaultEffort` must be one of the
row's `efforts`; `useFor` is a short note shown to agents when they pick. The check comes in a later
pull request. Today the server reads only whether the policy is on (`advise` or `enforce`): then a
child that `delegate_task` (here or on a linked machine) or `create_threads` starts with a call that
names its model keeps only the options the call names, none of its parent's others (an agent, a
session mode, a service tier, an effort). With `off` it keeps upstream's way: a call that names the
parent's model takes the parent's options under its own.

Only the person can change it; agents cannot. A save must give both `mode` and `allow` (it replaces
the stored value whole) and is refused for a bad row, a `defaultEffort` not in its row's `efforts`,
or an unknown key such as a misspelt `effort`. A hand-edited value that is not valid, an unknown key
included, reads as `enforce` with nothing allowed (with a warning in the server log); it is kept as
written, and saves of other settings write it back unchanged until the person saves this setting.

In the app: **Settings → Fork**, section Model policy (server only; a project cannot override it): the
mode, and **Edit allowed models** for the `allow` rows as JSON, checked as a save is. An unreadable
value is shown with a warning and the kept text; a save there replaces it. **Edit allowed models**
then starts from the value's own rows and saves only once the person picks the mode, so a save never
turns it into the fail-closed Enforce. A row whose driver no provider here has is flagged.

```json
{
  "modelPolicy": {
    "mode": "enforce",
    "allow": [
      {
        "driver": "claudeAgent",
        "model": "claude-sonnet-5-5",
        "efforts": ["medium", "high"],
        "defaultEffort": "high",
        "useFor": "everyday coding"
      },
      { "driver": "codex", "model": "gpt-mini" }
    ]
  }
}
```

### `server.agentSlots`

Limits on running agents: a new agent is refused above `maxPerMachine` agents on this server,
above `maxPerAccount` per provider account, or below `minFreeMemoryMb` of free memory. With
`waitFor` on, an agent can wait for a slot instead of retrying.

These routes start a new agent run and go through the same gate, with the same count
(`apps/server/src/fork/agentSlotRoutes.ts`):

- `delegate_task`, `create_threads` and `t3_thread_launch` are refused with `slots_full`. A thread
  made without a prompt (or a launch without a message or attachment) starts no agent and is not
  gated. A retry of an accepted call (same `clientRequestId`) that already started its run is let
  through; one that stopped before its run is counted again. `create_threads` checks the whole
  batch first: when the threads with a prompt need more free slots than there are, none is created.
  If a slot is still taken between two threads, the refusal names the threads already created.
- A scheduled task's run on its schedule (interval, once, fixed time) that is refused stays due: it
  is tried again on each poll (every 5 s) and starts, once, when a slot is free. Its history shows
  one note, `Waiting for an agent slot: ` and the current reason, until it runs (the note goes when
  the task is paused or moved to a later time); its run count does not
  move, and it is one run however long it waited. A fixed-time run that waits is not dropped as
  missed while the server runs (its due time follows it); if the server is down more than 10
  minutes while it waits, upstream's missed-run rule applies. "Run now" fails with `Not run, no agent slot: ` and the reason and changes
  nothing; over MCP `run_scheduled_task_now` answers `slots_full`. A webhook delivery waits the
  same way and starts once when a slot is free; it ends without a run only if its task is paused,
  deleted, replaced or its trigger changed while it waits (other edits are not seen: it runs with
  the task as it was when it began to wait). Every waiting delivery is in the store, so a restart
  keeps it and it runs once: a direct one (no coalescing window) is moved there when it is first
  refused. A scheduled run or a webhook takes a slot only when no agent that waits for one holds
  it; there is no order between routes, so on a host kept full it can wait long.
- A task bound to a thread whose run is active only queues its prompt there: no new run, so no slot
  and no wait. A bound thread that is idle starts a run and is gated, counted on the thread's
  current provider.
- An agent's message into an existing thread, `t3_thread_send` or `t3_thread_send_attachments`
  ([#711](https://github.com/scuttletoby/t3code/issues/711)): into a thread that shows no run it
  starts one, so it takes a slot like `delegate_task`; refused, it answers `slots_full` with
  `delegate_task`'s wording ("This call sent nothing to thread …" and, with `waitFor` on, the
  `wait_for` call to make before ending the turn) and nothing is sent. Into a thread that shows a run it only
  queues: no slot, also when full. A receiver that is gone, deleted or archived is left to the
  send, which refuses it with its own error; one that cannot be read is refused
  (`orchestration_error`, nothing sent). The check is the last step before the send, after
  upstream's own checks of access and the thread; a send that still fails after an allowed check
  holds its slot until the start grace ends (it fails safe: never an agent without a slot). A
  retry with the same `clientRequestId` into a thread that has gone idle, on a full host, is
  refused although upstream would answer it as the earlier send: this call sends nothing, and the
  earlier call's message stays sent (the refusal says "this call", not that nothing was ever sent).
  A `mode: "restart"` send into a thread whose run waits in `t3_thread_wait` is let through as a
  message into a running thread (that run is not counted while it waits), so the run it starts can
  go one over the limit until it ends.
- A wait's message (`wait_for` result, a child that finished, a lost-wait notice) that would wake
  a thread that shows no run takes a slot the same way. Refused, nothing is sent and the wake-up
  waits for a slot however long it takes: it is never marked late and never dropped, also long
  past its deadline. A wait's message is asked again every 5 s (logged once); a lost-wait notice
  every 60 s, as its other retries are. The wait stays `delivering` in the wait store with its
  message id and command id, so a restart keeps it: the new server sends that same message at
  once if a slot is free (it asks the gate again), and the message's found-check and command
  receipt keep it from ever being sent twice. A `delivering` wait cannot be cancelled
  (`wait_cancel` stops `waiting` waits only), so a wake-up that waits for a slot is sent once a
  slot frees. A wake-up keeps the thread's own held slot (from a `slot` wait) for the agent it
  wakes up to start, and every other thread's held slot whose wake-up is still to come counts that
  wake-up as an agent of its own, so slot waits met in one sweep are each woken.
- The fork's delegated-result reminder and its orphan scan (a parent that is idle and still owed a
  child's result is sent one reminder) take a slot the same way. Refused, nothing is sent: the
  reminder is asked again every 5 s, however long it takes, and a refusal is not counted as a
  failed try (logged once). Its message and command ids are fixed per result, so a restart's
  startup scan finds the result still owed, waits again and sends it once.
- Upstream's pull request watch wake-ups (checks passed or failed, a review, merged or closed,
  "stopped watching") into a thread that shows no run take a slot the same way
  (`apps/server/src/fork/pullRequestWatchSlot.ts`). Refused, the update fails and nothing is
  stored or sent: upstream's watch reactor reads the pull request again on its next sweep (every
  2 minutes), finds the same news against the stored watch and sends it then, once. A restart
  finds it again from the stored watch. A watch that ends because its pull request cannot be read
  keeps its "stopped watching" wake-up the same way, and is not ended without it. Into a thread
  that shows a run a wake-up only queues: no slot.
- Upstream's usage-limit resume ("Continue where you left off." into each thread that failed on a
  limit, at its reset) takes a slot the same way (`apps/server/src/fork/limitResumeSlot.ts`), so a
  reset does not start every failed thread at once. Refused, nothing is sent: the thread stays
  failed and armed, and upstream's recovery worker asks again on its next tick (every 5 s; logged
  once per resume). An armed resume has no time limit, a restart finds it from the stored thread,
  and its id is fixed per failed run and reset, so it is sent once.
- There is no order between wake-ups, and none between them and other routes: a wake-up holds no
  place in line and asks again on its own timer, so a busy host can pass one over again and again
  while other starts take the slots that free.
- The bound-thread and message routes decide "running or idle" from the gate's own count in the
  same step that takes the slot. When the thread's run ends between that check and the send, the
  queued message starts the next run: the call then holds a slot until that run shows, for the
  start grace (`startGraceSeconds`) at most, so no start ever goes past the gate.
- A call that asks another machine to start an agent (`delegate_task`, `t3_thread_launch` or
  `t3_thread_send` with an `environmentId`) is decided by that machine's gate, the one that would
  run the agent; this machine's gate is not asked. A machine on a build without this gate starts
  it as before.

Only an agent caller is told it can wait for a slot (`wait_for`) and end its turn. A start with no
caller here (another machine's call, a scheduled run) is told plainly what happened: the machine
has no free slot, or could not count its agents.

These are not gated, on purpose:

- A message a person sends from the composer (or any client of the owner's that is not an agent
  tool): the person decides, and a person is never refused for slots.
- `t3_thread_transfer`: a fork copies context into a new thread and runs no turn; merge_back
  leaves the context for the target's next run. The first message into the fork is a
  `t3_thread_send`, gated as above.
- A delegated task's result delivered to its parent: this mostly takes the slot the finished child
  gives back. Restart recovery resumes runs that were running when the server stopped.
  `t3_worktree_handoff` continues the same agent's work after its own turn.

These four are not gated yet. Each can **start a run in an idle thread with no slot**, so a full
host can go over its limit through them. Gating them is follow-up #724:

- The fork's context handoff (`apps/server/src/fork/handoff/HandoffService.ts`): the idle-timer
  trigger starts a run with fresh context after the thread's turn ended. It is off by default.
- Upstream's provider continuation ("Background task completed"): an agent that ended its turn
  while its own background work ran starts again when that work ends.
- Upstream's delegated question replies: a child's question answered back into an idle parent.
- Upstream's idle compaction: a compaction run started in an idle thread.

The calling thread is left out of the count (it waits on its child). A start no thread on this
machine asks for (a scheduled run, the owner's client, a call from another machine) has no caller, so
nothing is left out. An allowed start counts at once: until its run shows, for at most
`startGraceSeconds` (default 30 s) once its thread is known. A start that fails before its thread
is known gives its slot back at once; one that never ends holds it 10 minutes at most.

A running agent is a thread whose shell shows an active run (settled or archived threads too), as
upstream's own shell read decides (`apps/server/src/slots/ActiveRunShells.ts`); a test checks that
for every run status. The fork's part of that read names no index, so a renamed, dropped or partial
index only makes it slower (a test checks that SQLite still uses upstream's index). Upstream's own
shell read does name the index. When the running agents cannot be counted (that read fails, crashes
or takes longer than 5 s), a delegation that needs the count is refused with `slots_full` and a
message saying the thread list could not be read and nothing was started; the caller can try again
in a moment. An empty thread list is a good read, not a failure. A slot wait is never woken while
the count cannot be read. A refused call writes nothing of its own; it may still clear, in the wait
store, a held slot that an earlier allowed call used up. At most one count read runs at a time: a
caller that needs the count while a read runs joins that read, so a slow store never piles up reads.
An answer that comes more than 5 s after its read started is not used, so no count is older than
that. While a read never ends, delegations that need the count are refused even if other database
readers are free, until it ends or the server restarts. Delegations go through the gate one at a
time, so with a stuck store queued callers wait about 5 s each in turn (the third one about 15 s)
and are then refused.

Switch on: **Settings → Fork**, section Agent slots, "Limit running agents", or in `settings.json`:
`"agentSlots": { "enabled": true, "maxPerMachine": 4 }`. With `enabled` and no limits set, nothing is
refused.

`mode` says how the gate decides. `"fixed"`: by the numbers above, nothing else. `"observe"`: by the
same numbers, and a capacity rule (`packages/shared/src/capacity/`) runs beside each decision on live
memory readings and only records what it would have answered: one log line per decision, and the
last result with counts under `slotRule` in `wait_list`. It never changes or delays a decision; a
reading that fails or takes over 2 seconds counts as "unreadable". While switched off the gate keeps
no records of new agents, so the rule counts each agent allowed in the last `startGraceSeconds` as
starting. Without `mode`, a switched-on
gate is `"fixed"` (a file from an older build decides exactly as before) and a switched-off one is
`"observe"`; a mode this build does not know (from a newer build) is `"fixed"`. Not "off" for that
reason: with the default the server reads memory files at each `delegate_task`; `"mode": "fixed"`
stops it.

### `server.headlessUsageRefreshInterval`

How often the server reads usage limits while no app is open, in milliseconds. On by default every
10 minutes, so pacing has current numbers on a machine nobody is looking at. Read only: it changes no
thread, setting or install. Never more often than the provider status refresh.

Switch off in `settings.json`: `"headlessUsageRefreshInterval": 0`.

### `server.mcpProfiles`

Named MCP profiles. A profile says which outside MCP servers (`servers`) and which groups of T3's
own tools (`t3Tools`) a thread gets; each is `"all"` or a list. Two profiles are built in: `all`
(everything, today's behaviour) and `t3-only` (no outside servers, every T3 tool); a profile cannot
take their names, and one written in by hand makes that built-in refuse threads until it is removed
(a save may send `null` under a built-in name to remove it) or renamed.

A Claude thread applies the profile's `servers`: `[]` (and `t3-only`) starts Claude without any MCP
server of its own (its config files, plugins and claude.ai connectors), and a list keeps only the
servers it names, written as Claude lists them (`claude.ai Calendar`). T3's own tools stay. A Claude
thread whose profile cannot be applied does not start its turn and says why: a missing or broken
profile, a parent thread that cannot be read, a `settings.json` T3 cannot read (when only a
provider secret cannot be read, the profile comes from `settings.json` alone and the server log
says so; with no profile set there, the thread gets `all` as before), a profile whose
`t3Tools` is not `"all"` (the T3 tool filter comes in a later pull request), or a session that
still shows a server the profile leaves out, or does not list its servers within 5 minutes. A
managed Claude policy on the machine or `--mcp-config` in the Claude launch arguments can make
Claude load a server T3's list leaves out.

A thread whose profile is narrower than `all` cannot start a top-level thread on this machine that
gets more than its own profile ([#681](https://github.com/scuttletoby/t3code/issues/681)).
`t3_thread_launch` and `create_threads` from it are refused, before any thread is made, when the new
thread's profile (its project's `mcpProfile`, else the server's; `create_threads` makes its thread in
the caller's own project) is not within the launcher's: a profile is within another when its servers
and T3 tools are a part of the other's. Both profiles come from one settings read. A launcher, or a
new thread, whose profile cannot be worked out (a project that names a profile no setting defines)
is refused, never taken as `all`. Each call is checked: a retry of a `t3_thread_launch` that was
accepted before is checked again, so it is refused when the profiles no longer allow it, though its
thread exists; a retry of an accepted `create_threads` call is left as it resolves, as its thread is
already made. `t3_thread_launch` with `scratch: true` makes the (empty) scratch project before the
check, so a refused launch can leave that project behind; it makes no thread. A launch on another
machine, and a new `schedule_task` there, are refused for such a thread too, as that machine gives
the thread its own profile; `delegate_task` carries the launcher's profile instead. That refusal is
made by the sending machine: a T3 from before this check sends a narrowed thread's launch to a
machine of this version unchecked, and there it arrives with no calling thread and gets that
machine's profile. Editing or running a scheduled task on another machine, and a fork there, are
covered below. A thread on `all`, and the owner's own client, launch as before.

The same rule holds for the other ways such a thread starts a thread on this machine
([#693](https://github.com/scuttletoby/t3code/issues/693)). `schedule_task` is refused when the
task's runs would start a new thread (not bound to the caller) whose profile in the task's project
is not within the caller's; a task bound to the caller runs in it and keeps its profile. A change
to a task whose runs start new threads (also one made before this check), and unbinding a task from
the caller, are refused the same way: delete the task, bind it to the caller
(`bindToCurrentThread: true`), or ask the owner. `run_scheduled_task_now` on such a task is refused
too. These refusals are checked again by the scheduled task service on the task it reads as it
saves, so a change another caller makes in between is seen
([#699](https://github.com/scuttletoby/t3code/issues/699)). Such a thread also cannot change the
prompt, project or thread of a task bound to another thread whose profile is not within its own
(that thread would run its words); reading, renaming, pausing it and changing its schedule still
work. The `schedule_task` handler checks that before the service is called, so a refused change
does not use up the webhook secretRef it carries.

Each save records who made it: the owner (the app, or an MCP client of this machine with no
thread), a thread here and its profile then, or another machine. Another machine does not say the
profile of its thread, so its save records none. The record is `scheduled-task-mcp-profiles.json` in
the server's state folder (the fork changes no database schema). A scheduled run,
`run_scheduled_task_now` or a webhook run that starts a new thread is refused when that thread would
get more than the recorded profile, for example after the owner widened the project's profile. It
is also refused when the task was saved from another machine, and when an agent made it and it has no
entry (saved before this record, or the file was lost or an older copy put back). The owner saves
the task once in the app to allow it; a task the owner made with no entry runs as before. A
webhook delivery that waited for an agent slot is checked again as it starts. A refused scheduled run
is recorded as failed with the reason and moves on to its next time; a one-time task then has no
next time, so after the owner's save it is run now or given a new time (the reason says so). A task
whose row changed after its record (a save stopped half-way) is refused until it is saved again, and
a record file that cannot be read refuses saves and runs that start a new thread until it is fixed
or removed. Runs into a bound thread keep that thread's profile and are not held up. That includes
a task with `threadMode: "continue"` that has no thread yet: its first run is checked, later runs go
into the thread that run made, and if the owner widens the project, that thread and those runs widen
with it. A change saved from another machine is not checked against the thread a task is bound to
here: a current sender refuses a narrowed thread's change first, but a T3 from before that check
forwards it. Entries of deleted tasks are dropped at each start.
`t3_thread_transfer`'s fork is refused when the fork would get more than the caller's profile: a
fork gets its source's profile cut to the source's, so forking the caller itself always goes through, and a fork of a thread whose parent cannot be read is refused. On
another machine, a fork, a change to a scheduled task and `run_scheduled_task_now` are refused for
such a thread, as a thread started there gets that machine's profile.

Only Claude threads apply profiles. A Codex thread, or one on any other provider, would get every
MCP server and every T3 tool whatever its profile says. So its turn is not started when its profile
is narrower than `all` ([#672](https://github.com/scuttletoby/t3code/issues/672)): the machine that
runs the turn checks the thread's profile as the turn starts (also a resumed turn, and a compaction,
which some providers run as a `/compact` prompt) and refuses it, saying why and how to go on (switch
the thread to a Claude instance, or pick `all` for it). That covers every way a thread gets there: a
thread switched to another provider (`t3_thread_configure`, or in the app), a thread made by
`t3_thread_launch` in a project whose profile is narrower, a task delegated on this machine (a child
gets its parent's profile, or `mcpSubagentProfile`), and a task another machine sent, also one from
a T3 from before carrying, which carries nothing and gets this machine's profile for its role. A
profile that cannot be worked out (a parent thread that cannot be read, settings that cannot be read
for a carried task) refuses the turn there too, as on Claude. The check is read again for each turn,
so a profile change, or a switch back to Claude, counts from the next turn. A thread whose profile
is `all` runs on any provider as before: the check changes only those three calls, and the session
reads as the provider keeps it (its status and capabilities stay live). One change for such a
thread: a turn on another provider refuses when the profile cannot be worked out (the cases above),
where it used to run, and the settings are read for each such turn. A delegation also refuses,
before anything is sent, a narrower profile to a Codex (or other non-Claude) instance on another
machine (see below), so the task is not made only to be refused there.

Each tool call is checked too: a call to an MCP server the profile leaves out (also one an agent
file declares, or one added while the session runs) is refused at the moment of the call: Claude
gets the reason as the call's result, and the turn goes on. That covers its tools, its resources
(a resource listing that names no server is refused, as it would list every server's), and a
`Skill` call naming `mcp__<server>__…`, the form of a skill an MCP server serves. Where Claude does
not name the server it calls, the tool name's prefix is matched, so a server named like
`docs__x`, or `my_docs` under a profile that names `my.docs`, passes. With the `all` profile
nothing is checked. A
profile change applies from the thread's next turn: a running turn, and a message sent into it,
keep the session it started with.

A task delegated to another machine carries its parent's profile along
([#532](https://github.com/scuttletoby/t3code/issues/532)), and that machine cuts the task's own
profile for its role (`mcpSubagentProfile`, else `mcpProfile`) down to it, as it cuts a child to
its parent: `t3-only` or `[]` stays as it is, two lists keep only the server names both name, and it
never gets more than that machine's own profile gives it. Server names are matched as written, so
the two machines should use the same names. An owner's pick for the task there (`mcpThreadProfiles`)
stands, as on one machine. A carried profile that machine cannot read (one from a newer T3 with a
field it does not know) refuses the task's turn rather than giving it more. So does a parent thread
that cannot be read there, whatever the settings: it may be such a task, so its sub-agents are
refused rather than given `all`, unless the owner picked a profile for that very thread. Agents do
not get the carried profile in any tool's input, but `t3_thread_read` on the task shows it.

A machine on a T3 from before this cannot apply a carried profile, so a thread whose profile is
narrower than `all` does not delegate to it: the call is refused before anything is sent there,
and says to update T3 on that machine. Before sending, the delegating machine asks the other at
`GET /api/fork/features` with that link's own token; the route answers only a linked machine's
token, never a browser, and any other caller gets a plain 404. Only `mcpProfileCarry: true` counts:
an older T3 answers that path with its app page, which reads as "cannot", and no answer in time
(the whole answer, its body too) refuses the task too. A redirect is not followed, and only a 200
answer counts. It asks again for each task, so an update or a downgrade there counts at once.
A machine whose owner set `mcpCarriedProfiles` to `refuse`, or whose settings cannot be read now,
answers `mcpProfileCarry: false`, so the call is refused the same way, before anything is sent, and
says that machine does not take such a task now. When the machine says it applies one, the
delegating machine then asks its `orchestrator_capabilities` (through the link, as any call there)
which driver the task's provider instance runs, and refuses the call unless it is Claude: an
instance it does not list, or no answer, refuses it too
([#661](https://github.com/scuttletoby/t3code/issues/661)). A call that names a driver
(`target.driverKind`) and no instance has that list asked once: upstream picks the instance there
from the list by that driver, and the check uses it. A call that names an instance is looked up in
the list as before, also with a driver named, so an instance that machine does not list is refused.
A thread with `all` delegates to any machine and any instance there, without asking. A task that
arrives from a machine on an older T3 carries nothing, so it gets this machine's profile for its
role as before, and the server log says so.

```json
{ "mcpProfiles": { "lean": { "servers": [], "t3Tools": ["orchestration", "threads"] } } }
```

Only the person can change MCP profile settings; agents cannot.

### `server.mcpProfile`

The profile a top-level thread gets when nobody picked one for it. `all` (the default) gives every
thread everything, as today. A name with no profile is refused when saved, and a thread that meets
one (for example after a hand edit) is refused instead of getting `all`.

### `server.mcpSubagentProfile`

The profile a delegated child gets when nobody picked one for it. `inherit` (the default) gives it
its parent's profile. A child never gets more than its parent, also a task delegated from another
machine (see `server.mcpProfiles`).

### `server.mcpThreadProfiles`

The person's pick of profile per thread, by thread id. A pick wins over every default. At most 500
picks are kept: a save that would add more is refused until old picks are removed. No pick is ever
dropped to make room.

### `server.mcpCarriedProfiles`

Whether this machine takes tasks that another machine delegated with a profile narrower than `all`
([#640](https://github.com/scuttletoby/t3code/issues/640)). `apply` (the default) cuts such a task
down to the carried profile, as `server.mcpProfiles` says. `refuse` takes no part in carrying: the
features route answers `mcpProfileCarry: false`, so a narrower-profile thread on another machine is
refused before it sends. A task that arrives with one anyway (sent before the setting changed), or
with a profile this machine cannot read, does not start its turn, nor do its sub-agents, and says
why (a sub-agent's refusal gives the reason at the top of the chain). Whichever value is set, a
narrower task on a Codex or other non-Claude instance here does not start its turn, as only Claude
threads apply profiles (see `server.mcpProfiles`), and a sender of this version does not send one. Neither value is an
"ignore the carried profile" value, as a thread could then get more tools by delegating to this
machine. A task that carries `all`, or nothing (from an older T3), runs as before, and the owner's
pick for a task here (`mcpThreadProfiles`) still stands. It is read again for each turn and each
ask, so a change counts from the next one. Only `apply` (in any case, with spaces around it), or
`null`, reads as `apply`; any other value, such as a typo of `refuse` or an empty one, reads as `refuse`, and the
file's other settings keep their values.

`settings.json` is checked on each read too, the same way for a Claude turn, for a delegation's
check of its own thread's profile, and for the features route's answer. When it does not decode
(also when it already did not decode when the server loaded it, which gives every setting its
default), this setting reads as `refuse`, as the file may say so: a narrower carried task is
refused, and the route answers `false`. A managed policy that locks this setting wins over that.
This machine's own threads do not get a wider profile from a broken file
([#681](https://github.com/scuttletoby/t3code/issues/681)). The MCP profile keys (`mcpProfiles`,
`mcpProfile`, `mcpSubagentProfile`, `mcpThreadProfiles` and each project's `mcpProfile` and
`mcpSubagentProfile`) are read from the file on each read, over the settings the server holds
(which can still be an earlier load's, such as a broken file's defaults until it reloads). A file
that is a JSON object but does not decode (a wrong value in another setting) gives the keys its text
holds, read the way a full decode reads them; a valid file that names none of them sets none, so
`all`. A whole file with a JSON syntax typo (a missing comma, a stray quote) whose text names none
of them sets none too, so `all`, and T3 keeps working as upstream does, also after a restart.
"Whole" means its braces, outside strings and comments, close back to the start exactly at its
last character, a `}`. Text that is not a JSON object, names none of them and is not whole (empty,
cut off before its MCP keys, garbage, a list) cannot say, so the server keeps the MCP profile keys
it last read in this run; a server started on such a file has none, and refuses every turn,
delegation and launch until the file is fixed. A missing file is a fresh install (the settings
the server holds, `all` by default) only while the server has read no file's keys since it started;
once it has, a missing file (an editor that saves by renaming the old file away, or a delete) keeps
the keys last read too, until the file is back or the server restarts. Kept keys read carried MCP profiles as `refuse`, and a managed
policy's locks stay over them. Text that names one of them but cannot be read as a JSON object
(cut off half-way through a write, a whole file with a typo, a list, a project map that is not a
map), or a file that cannot be read from disk at all, refuses the same way, saying why. The next
turn reads the file again. When the settings cannot be read in full (they read provider secrets too), the MCP
profile settings come from `settings.json` alone; when that cannot be read either, a turn or a
delegation is refused and the route answers `false`, never anything wider. Only the person can
change it; agents cannot.

```json
{ "mcpCarriedProfiles": "refuse" }
```

### `server.handoff`

Handoff instead of compaction: when a long thread gets big, or sits idle while its prompt cache is
about to run out, T3 starts a fresh session in the same thread with a written handoff, so the
thread's children, watched pull requests and waits keep working. How and when:
[handoff.md](handoff.md).

Only the person can change it; agents cannot. `mode` is `off` (the default: upstream's compaction
settings apply as they are), `handoff` (upstream's idle compaction then skips the thread, so
only the handoff acts on it), or `compact` (the same as `off`, so one thread can opt out
of its project's `handoff`). `threads` maps a thread id to a mode and wins over `mode` (at most
500). `source` says who writes the handoff: `agent` (the old session, in one turn; `instructions`
replaces the built-in text) or `command` (a program given as `command`, run without a shell; it
must only read). Neither source writes a handoff yet; until they do, every handoff uses T3's state
and the recent conversation only, and `source` sets only how early the idle handoff starts. `sizeTokens` (150,000) is the size backstop, `idleTokens` (75,000; `0` never) the
smallest idle thread handed off before its cache expires, and `modelSelection` the new session's
model (not set: the thread's current one). A save with a bad value, a field T3 does not know (a
typo), or `source: command` without a command, is refused; a hand-edited value that is not valid reads as off (with a warning in the
server log), never as `handoff`.

In the app: **Settings → Fork**, section Handoff: the mode, each thread's own mode (added by thread
id, shown with its title where this client knows the thread), and under Advanced the sizes, who
writes the handoff (with its command) and the instructions. `modelSelection` has no row yet; a save
there keeps it as stored.

```json
{ "handoff": { "mode": "handoff", "threads": { "<thread id>": "compact" } } }
```

### `projectOverride.waitChecks`

A project's own list of wait checks. It replaces the server's list for that project as a whole (a
project lists every check it uses). Not set: the project uses the server's checks.

Set: **Settings → Fork**, choose the project at the top, then **Wait checks**.

### `projectOverride.playtest`

A project's own playtest adapter. It replaces the server's `playtest` for that project as a whole:
a project sets every step it uses. Not set: the project uses the server's. A project value that is
not valid reads as off for that project; it never falls back to the server's adapter. A project save
that leaves this key out (an older client) keeps the stored value; only `null` clears it, and the
fork's `clearProjectSettingsOverridesWithForkKeys` sends that. A `null` written into the file by
hand means the same: it reads as the key left out (not set, the server's), and the next save drops
it. This changed with [#302](https://github.com/scuttletoby/t3code/issues/302): before, a
hand-written project `null` read as off, so a file that still holds one (no save of that project
since) now takes the server's adapter, or for `handoff` the server's mode, and can turn playtest or
handoff on for that project. Write `{"enabled": false}` (`handoff`: `{"mode": "off"}`) to keep a
project off. Unlike the server's value it is
saved with every field written out, so a project that is off says so in the file. A repository's `t3.json` cannot set
it: that file is in the repository, which agents edit.

### `projectOverride.reviewRules`

A project's own review rules. It replaces the server's `reviewRules` for that project as a whole:
`{"preset": "off"}` turns review rules off for one project on a machine where they are on, and a
project with `recommended` does not take the server's fields written out. Not set: the server's. A
project value that is not valid reads as off for that project, never as the server's, and is kept
as written. A project save takes that kept text back only unchanged: a save that sends other
unreadable text, or text for a project whose stored value reads well (a client that has not reloaded
since the value was fixed elsewhere), is refused. A project save that leaves this key out (an older
client) keeps the stored value; only `null` clears it, as for `playtest`. A lens named like a
built-in object key (`constructor`) is not valid.

### `projectOverride.mcpProfile`

A project's own default profile for its top-level threads. Not set (absent or `""`): the server's
`mcpProfile`. A project save that leaves this key out (an older client) keeps the stored value
(a blank one, from a hand edit, is dropped instead); only `""` clears it (the server then stores nothing, so the project inherits again). Upstream's
"reset to inherited" helper clears by leaving the key out, so it does not clear these two keys; a
screen resets them with the fork's `clearProjectSettingsOverridesWithForkKeys`
(`@t3tools/shared/forkSettingsPatch`), which sends `""`. The same holds for `mcpSubagentProfile`.

### `projectOverride.mcpSubagentProfile`

A project's own profile rule for delegated children (`inherit` or a profile name). Not set: the
server's `mcpSubagentProfile`.

### `projectOverride.handoff`

A project's own handoff settings. It replaces the server's `handoff` for that project as a whole.
Not set: the project uses the server's. A project value that is not valid reads as off for that
project, and the next save of the project writes it back as a full `off` value; it never falls back
to the server's value. A thread's own mode in the project's or the server's `threads` wins over
either `mode`, the project's entry first. A project save that leaves this key out (an
older client) keeps the stored value; only `null` clears it (also when written by hand, as for
`playtest`, including the change for old files).

### A setting the fork removed

An early draft had a client setting for one "anchor machine" for new work. It was removed before it
shipped: upstream's load balancing does the same. Settings saved with it still load. See
[anchor-machine.md](anchor-machine.md).

## Install the fork today

There is no one-line installer for the fork yet: `t3.codes/install.sh` and upstream's docs install
upstream's T3 Code. Two ways work today.

**From a fork release (Linux x64).** The [fork's releases](https://github.com/scuttletoby/t3code/releases)
have the CLI archive for Linux x64 and Windows x64, and desktop apps for Linux, Windows and macOS.
The desktop apps are not signed, so macOS and Windows warn before they open. On Linux, the
installer script takes a mirror and an exact version:

```sh
curl -fsSL https://raw.githubusercontent.com/scuttletoby/t3code/main/scripts/install.sh \
  | T3CODE_VERSION=<version without the v> \
    T3CODE_RELEASE_BASE_URL=https://github.com/scuttletoby/t3code/releases/download sh
```

`t3 update` on a fork build then looks only at the fork's releases. Every fork switch starts off.

**From source.** Same as upstream's [development setup](../operations/development.md#first-checkout),
with this repository:

```sh
git clone https://github.com/scuttletoby/t3code
cd t3code && vp i && vp run build:desktop
node apps/server/dist/bin.mjs
```

A build made this way does not know it is the fork: its `t3 update` and `t3 service install` would
fetch upstream's releases. Do not use them on it; update it with `git pull` and a rebuild.

**A machine that already runs T3 as a boot service.** Do not switch it by hand. The go-live kit
([fork-kit/golive](../../fork-kit/golive/README.md)) is for this, step by step in
[go-live.md](go-live.md): today it has `check`, the practice `copy`, the way `back` and `status`. Its
rehearsal on the copy and its switch are not written yet, and the kits are not yet installable from
the release branch. Until then, try a build on a copy with
`t3 serve --hold-work --base-dir <copy>` ([updates.md](updates.md#trying-a-build-on-a-copy-serve---hold-work)).

Not there yet: a fork installer URL, a macOS CLI archive, signed desktop apps, and the go-live
switch.

## Other fork pages

- [go-live.md](go-live.md): the runbook for moving a boot-service machine to the fork, the way back,
  and switching features on one at a time.
- [updates.md](updates.md): update defaults, how a test pins them, the nightly sync runbook,
  `serve --hold-work`.
- [anchor-machine.md](anchor-machine.md): one machine for new work, with upstream load balancing.
- [gate-trust.md](gate-trust.md): what the head gate proves, and the owner's steps.
- [host-tools.md](host-tools.md): design for agents on a person's own computer (no code yet).
- [playtest.md](playtest.md): the playtest adapter contract (steps, environment, exit codes).
- [handoff.md](handoff.md): handoff instead of compaction (when, the quiet rule, the message).
- [run-records.md](run-records.md): one record per agent run (usage, time, labels) and the weekly
  summary.
- [`.github/fork-fleet.md`](../../.github/fork-fleet.md): the fleet runners and `CI_RUNNERS`.
- [`fork-ci/README.md`](../../fork-ci/README.md) and [`fork-kit/README.md`](../../fork-kit/README.md):
  CI actions and host kits.
