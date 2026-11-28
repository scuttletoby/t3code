# The fork: settings and install

Fork only. This fork is T3 Code with a thin set of changes on top; every change is listed in
[FORK_CHANGES.md](../../FORK_CHANGES.md). This page lists every fork setting: what it does, its
default, and how to switch it on. Then it says how to install the fork today.

The rule: a machine whose settings never name a fork setting behaves as upstream's T3 Code. Every
fork switch is off by default, except one read-only refresh of usage limits.

## Where settings live

Server settings are in `settings.json` in the server's `userdata` folder (by default
`~/.t3/userdata/settings.json`). The server reads the file again when it changes; no restart is
needed. Fork settings also have rows on the app's own **Settings → Fork** page (named below), grouped by
what a person wants to do (plan usage and machine load, waiting, models and tools, then one section each for handoff,
review rules, assistant, playtest sessions and updates), rare options folded under **Advanced**, and one line per
feature at the top saying whether it is on, by the same rules as the fork report below
(on a server, a project's override or one thread's handoff entry counts too). A project's overrides are set in **Settings** with that project chosen at the top ("Applying settings
for …", see [project settings](../user/project-settings.md)).

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

| Setting                                                                      | Default                                 | Off | Decided in                                                 |
| ---------------------------------------------------------------------------- | --------------------------------------- | --- | ---------------------------------------------------------- |
| [`server.automaticUpdates`](#serverautomaticupdates)                         | `false`                                 | yes | [#41](https://github.com/scuttletoby/t3code/issues/41)     |
| [`server.automaticUpdatesQuietCommand`](#serverautomaticupdatesquietcommand) | `""`                                    | yes | [#5](https://github.com/scuttletoby/t3code/issues/5)       |
| [`server.activityProbe`](#serveractivityprobe)                               | `false`                                 | yes | [#509](https://github.com/scuttletoby/t3code/issues/509)   |
| [`server.usagePacing`](#serverusagepacing)                                   | `{}`                                    | yes | [#52](https://github.com/scuttletoby/t3code/issues/52)     |
| [`server.usagePacingGate`](#serverusagepacinggate)                           | `"off"`                                 | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)     |
| [`server.usagePacingLowerEffort`](#serverusagepacinglowereffort)             | `{}`                                    | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)     |
| [`server.waitFor`](#serverwaitfor)                                           | `{"enabled":false}`                     | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)     |
| [`server.waitChecks`](#serverwaitchecks)                                     | `{}`                                    | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)     |
| [`server.playtest`](#serverplaytest)                                         | `{"enabled":false}`                     | yes | [#260](https://github.com/scuttletoby/t3code/issues/260)   |
| [`server.reviewRules`](#serverreviewrules)                                   | `{"preset":"off"}`                      | yes | [#291](https://github.com/scuttletoby/t3code/issues/291)   |
| [`server.modelPolicy`](#servermodelpolicy)                                   | `{"mode":"off","allow":[]}`             | yes | [#268](https://github.com/scuttletoby/t3code/issues/268)   |
| [`server.placement`](#serverplacement)                                       | `{"mode":"off"}`                        | yes | [#134](https://github.com/scuttletoby/t3code/issues/134)   |
| [`server.placementNeeds`](#serverplacementneeds)                             | `[]`                                    | yes | [#134](https://github.com/scuttletoby/t3code/issues/134)   |
| [`server.agentSlots`](#serveragentslots)                                     | `{"enabled":false}`                     | no  | [#84](https://github.com/scuttletoby/t3code/issues/84)     |
| [`server.headlessUsageRefreshInterval`](#serverheadlessusagerefreshinterval) | `600000`                                | no  | [#42](https://github.com/scuttletoby/t3code/issues/42)     |
| [`server.mcpProfiles`](#servermcpprofiles)                                   | `{}`                                    | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`server.mcpProfile`](#servermcpprofile)                                     | `"all"`                                 | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`server.mcpSubagentProfile`](#servermcpsubagentprofile)                     | `"inherit"`                             | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`server.mcpThreadProfiles`](#servermcpthreadprofiles)                       | `{}`                                    | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`server.mcpCarriedProfiles`](#servermcpcarriedprofiles)                     | `"apply"`                               | yes | [#640](https://github.com/scuttletoby/t3code/issues/640)   |
| [`server.handoff`](#serverhandoff)                                           | `{"mode":"off"}`                        | yes | [#215](https://github.com/scuttletoby/t3code/issues/215)   |
| [`server.threadRoles`](#serverthreadroles)                                   | `{"assistant":null,"orchestrators":{}}` | yes | [#138](https://github.com/scuttletoby/t3code/issues/138)   |
| [`server.assistant`](#serverassistant)                                       | `{"enabled":false}`                     | yes | [#879](https://github.com/scuttletoby/t3code/issues/879)   |
| [`server.stopFinishedChildren`](#serverstopfinishedchildren)                 | `{"enabled":false}`                     | yes | [#925](https://github.com/scuttletoby/t3code/issues/925)   |
| [`server.lanes`](#serverlanes)                                               | `{"enabled":false}`                     | yes | [#1156](https://github.com/scuttletoby/t3code/issues/1156) |
| [`projectOverride.waitChecks`](#projectoverridewaitchecks)                   | not set                                 | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)     |
| [`projectOverride.playtest`](#projectoverrideplaytest)                       | not set                                 | yes | [#260](https://github.com/scuttletoby/t3code/issues/260)   |
| [`projectOverride.reviewRules`](#projectoverridereviewrules)                 | not set                                 | yes | [#291](https://github.com/scuttletoby/t3code/issues/291)   |
| [`projectOverride.mcpProfile`](#projectoverridemcpprofile)                   | not set                                 | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`projectOverride.mcpSubagentProfile`](#projectoverridemcpsubagentprofile)   | not set                                 | yes | [#145](https://github.com/scuttletoby/t3code/issues/145)   |
| [`projectOverride.handoff`](#projectoverridehandoff)                         | not set                                 | yes | [#215](https://github.com/scuttletoby/t3code/issues/215)   |
| [`projectOverride.placementNeeds`](#projectoverrideplacementneeds)           | not set                                 | yes | [#134](https://github.com/scuttletoby/t3code/issues/134)   |
| [`client.calmSidebarEnabled`](#clientcalmsidebarenabled)                     | `false`                                 | yes | [#139](https://github.com/scuttletoby/t3code/issues/139)   |
| [`client.ownerQuestionsPage`](#clientownerquestionspage)                     | `true`                                  | no  | [#828](https://github.com/scuttletoby/t3code/issues/828)   |
| [`client.workPanelEnabled`](#clientworkpanelenabled)                         | `false`                                 | yes | [#1136](https://github.com/scuttletoby/t3code/issues/1136) |

For `waitFor`, `agentSlots`, `playtest`, `stopFinishedChildren` and `lanes` only the `enabled` switch is pinned, and for `handoff`
and `placement` only `mode`; their other fields are tuning that does nothing while the switch is off.

## Each setting

### `server.automaticUpdates`

A server installed as a boot service (`t3 service install`) updates itself to the newest fork release
once no thread is working. Off: it updates only when someone runs `t3 update` or presses **Update**.
Upstream has a setting of this name pending (not on its `main` yet); the fork keeps `false` through every sync.

Switch on: **Settings → Fork**, section Automatic updates, "Update automatically", or in `settings.json`:
`"automaticUpdates": true`. An
admin policy with updates `"off"` still wins. Details: [updates.md](updates.md).

### `server.automaticUpdatesQuietCommand`

An optional program asked before each automatic update; a non-zero exit means "not now". Used only
while `automaticUpdates` is on. Empty: no program is asked.

Set: **Settings → Fork**, section Automatic updates, under Advanced, "Ask a program before updating", or in `settings.json`:
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
Fork**, section Plan usage pacing, one row per account by its name (an entry for an account that is gone
is listed with **Remove**).

`burnFirstWindows` limits `burnFirst` to some window kinds (`session`, `weekly`, `monthly`,
`other`); the rest keep the even pace. An extra account that may spend its week fast but keeps its
5-hour windows paced:

```json
{
  "usagePacing": {
    "<instance id>": { "burnFirst": true, "burnFirstWindows": ["weekly", "monthly"] }
  }
}
```

Without the list, `burnFirst` spends every window first (as before the list existed). A name that is
not a window kind is ignored on load (that window keeps its pace; a warning is logged) and refused by
a save. In the app, an account with **Spend first** on shows one box per window kind; its summary
names the kinds it spends first. `__fork-report` shows them under `usagePacingBurnFirstWindows`.

A key an entry does not know (a typo such as `burnFirstWindow`) is ignored on load with a warning
that names it; with `burnFirst` on and the list lost that way, every window burns first. The next
settings save writes the entry back as it was read: unknown keys and names are gone from the file.

### `server.usagePacingGate`

What happens when an agent starts a task with `delegate_task`. `"off"`: nothing, as upstream.
`"advise"`: the task starts as asked and the reply carries a note about the pace. `"enforce"`: a task
on an account that is ahead of its pace moves to another account of the same provider, runs at a
lower effort (`usagePacingLowerEffort`), or is refused for now and the agent asks again later.

A `delegate_task` child that a linked machine sends here runs on this machine's accounts, so this
machine's own setting decides it, before its agent slot, as for a task started here: the
`usagePacingLowerEffort` step applies here, once, and the child starts at the lowered effort (an
effort this machine's `modelPolicy` does not allow keeps the checked one). It never moves to another account: the reply does not offer one, as this machine cannot tell whether
the project there allows it. A refusal has the code `usage_pacing_hold`, which the sending machine
gets back as it is; the owner's own launches from a linked machine are not paced.

Switch on: **Settings → Fork**, section Plan usage pacing, "Pace agents by plan usage", or in `settings.json`:
`"usagePacingGate": "advise"` (try `advise` before `enforce`).

### `server.usagePacingLowerEffort`

For `"enforce"`: what each effort level turns into while an account is ahead of pace and no other
account has room, for example `{ "high": "medium", "medium": "medium" }`. A task at a level not
listed is held instead. Empty: no effort is ever lowered.

Set: **Settings → Fork**, section Plan usage pacing, under Advanced, "Lower effort when ahead of pace" (pairs like `high=medium`; a level no
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
messages of that check are still sent. Each refused message is logged once per server run, not on
each later check (also when another thread holds its id); after a restart a message still refused
is logged once more, so a refusal whose log line a stop cut off is still logged. A reminder sent
before per-task ids (one id for the whole set) is split into its tasks from its text, so after an
upgrade a task it named is not reminded again; only tasks past the first ten of such a message,
which it never named, may be reminded once more. Such a message whose text cannot be read is left
out alone: its tasks may be reminded once more, and the parent's other reminders still go. The check
at start and a check after an event never run at once for one parent. It never goes to a stopped or
archived thread, and never for results older than a day. A parent is busy, and waits for its own run
end, while a run of it goes or a question of it is open (an open question holds the reminder until
that run ends); background work it left running does not hold it. At start, every parent with a
result unread from the last day is checked once, also when upstream had reserved no wake for it.
Upstream's wake can land just before the reminder, so the message says the result _may_ not have
arrived.

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

Switch on: **Settings → Fork**, section Agent waits (shown while a server is connected), "Let agents wait for events", or in `settings.json`:
`"waitFor": { "enabled": true }`.

### `server.waitChecks`

Named commands an agent can wait on by name (`wait_for` with a check). Each runs as a program with
fixed arguments, no shell; exit 0 means the event happened. It runs unsandboxed in the project folder
with the server's environment, so use fixed programs that run no files from the repository (no test
runners or build scripts). Only the person can add one; agents cannot.

Add one: **Settings → Fork**, section Custom wait checks, **Add check**, or in `settings.json`:

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
arguments, run without a shell; T3 passes the session's facts as environment variables. An agent
starts, pushes, checks and ends its thread's session with the `playtest` tool (its own tool group, `playtest`); the
thread controls come in a later pull request. The steps' contract and the tool are in
[playtest.md](playtest.md).

In the app: **Settings → Fork**, section Playtest sessions: the switch (it stays off until all four steps are
set), the steps as JSON under **Edit steps** (checked as a save is), and the limits under Advanced.

Only the person can change it; agents cannot. A save that is on without all four steps, repeats a
push step's name (or names one `claim`, `renew` or `release`), names a program by anything but its
full path on the server's OS, or has a field this build does not know is refused. A hand-edited
value that is not valid, or has a field this build does not know (a newer build's limit, a typo),
reads as off (with a warning in the server log that names what is wrong, such as the unknown
field), and the next save of any setting writes the default in its place. So after moving back to an older build, copy the playtest value first: the older
build's next save replaces a newer build's value. Other fields: how often a claim is renewed (`renewMinutes`), when an idle
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
the machine's half. While it is on, the T3 tool `lens_start` (below) reads it; while it is off that
tool is not listed, refused if called and not found by `tool_search`.

`preset` is `off` (nothing is listed, changed or checked) or `recommended`, which fills in the
fork's defaults: Claude (`claudeAgent`) with `claude-opus-5-5` at `high` effort, the `approach`
lens on `claude-fable-5-2` at `high`, the state question "What happens when this runs again, stops
half-way, or meets existing, old or empty state?", small findings fixed in the pull request or
dropped (`minors: "same-round"`, the review kit's default), the config at `.github/pr-queue.json`
and reports of at most 60000 characters. A field written out wins over the preset's (`lensRunners`
per lens); a field written out while `preset` is `off` does nothing. A fork update may change the
preset's values; write the fields out to keep them fixed. Efforts an agent may not use are the model
policy's, not this setting's.

Fields: `runner` and each `lensRunners` entry are `{driver, model, effort}` (a driver kind, not a
provider instance), and a lens name is not a built-in object key such as `constructor`;
`stateQuestion` is 20 to 600 characters; `minors` is `same-round` (the review kit's default and
rule, written once in `MINORS` in `fork-kit/review/lib/review_kit.py`), `issue` or `free`;
`configPath` is relative to the repository, without `..`; `reportMaxChars` is a whole number
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

**`lens_start {pr, head?, afterCi?}`** (T3 tool, in the `orchestration` group of MCP profiles)
starts the review lenses a pull request's head still needs. It reads the config at the base
(`configPath`), the changed paths and the lens comments, plans with the shared review rules
(`apps/server/src/fork/reviewRules/lensStart.ts`) and starts one read-only child per lens through
`delegate_task` (role `review`), so the agent slot gate, usage pacing and the model policy apply.
The server writes each prompt: the read-only frame, the lens prompt file from the base, the state
question word for word with "answer it in a line starting `state:`", the `blocker:` / `major:` /
`minor:` lines and, from round 2, "check each earlier finding first, then the changes since". The
id of a child is `lens/<repo>#<pr>/<head>/<lens>`; a repeat returns the running child, a lens whose
newest comment read this head starts nothing, and a child that ended without a read starts again as
the next attempt (at most 3 per head). The children started are kept in
`<stateDir>/lens-children.json` (an unreadable file reads as none). It refuses: review rules off, a
round above the config's `rounds.max`, a runner the model policy refuses, a checkout of the pull
request's branch with uncommitted or unpushed commits, a head that moved, and anything it cannot
read (it starts nothing rather than too few). In `enforce` mode the config's path rules name the
lenses; T3 does not read a gate caller's `required-lenses` input, so outside `enforce` it refuses.

With `afterCi: true` the call returns at once: it registers a wait (`server.waitFor` must be on) on
the config's `required_checks` other than its `head_check`, at that head. When they pass, the wake
is sent to the thread and the server then starts the lenses in the run that wake starts (a
delegated child needs its parent's run); a red check, a new head (`moved`) or the deadline starts
nothing and the wake says why. A config whose only required check is its `head_check` has no CI to
wait for, so the lenses start at once. A problem starting them comes as a second message. The action
is part of the stored wait, so a restart before its delivery was written runs it again; the start is
idempotent. It runs at most 90 s per delivery (the delivery is tried again after that), so a slow
GitHub or thread does not stall other waits. Like the gate, lens_start skips a review or inline
comment that is not submitted yet, and dates an inline comment by when it was published.

**`lens_report {pr, body}`** (T3 tool, in the same group, hidden and refused while review rules are
off) is how a lens child posts its report. Only a thread lens_start started as a child (its newest
record in `lens-children.json`) may call it, and only for a pull request that child reads. T3
writes the first line, `lens: <lens> · reviewer: <model> · read at: <head>`, from that record and
the model the child runs on, so a child never types a lens name or a sha. T3 posts the report as a
new comment only while the pull request is open and still at that head, and only if the gate would
read it as one valid read with a findings section; otherwise it refuses and says why. The report
may not hold its own `lens:` or `read at` line, and it must fit `reportMaxChars` with T3's line.
The same shared vectors (`written` in `review.vectors.json`) test this writer and the gate. A repeat
finds the earlier comment and posts nothing new, including after a post whose answer was lost (the
tool then says `failed`: call it again). One report posts at a time, so two calls at once post
once. Nothing is stored.

### Slot filler route (`/api/fork/delegate`)

Lets a program start a delegated task as a given parent thread, so a script (fork-kit/slot-filler)
fills free agent slots and the parent is still woken by each child's end (#870). There is no setting:
the route needs a session token with `orchestration:read` and `orchestration:operate`
(`t3 auth session issue --scopes orchestration:read,orchestration:operate --token-only`), and a
browser request (one with `Origin`) gets a plain `404`.

- `POST /api/fork/delegate` `{parentThreadId, requestId, wake?, task}` runs the server's own
  `delegate_task` (async) with `task` as its input, as the parent thread, the way `lens_start` does.
  Every check of `delegate_task` applies: the slot gate, the model policy, linked environments,
  providers. The `requestId` is looked up first (and again after a refusal): one that
  already started answers with its task whatever the parent's state, so a caller that lost the answer
  asks again safely; one refused for good at its dispatch answers `request_rejected`. With `lookupOnly: true` it
  stops there (`not_found` when nothing started yet). A parent with no
  active run then gets one short queued message (`wake`, sent once per parent, request id and half hour) so it
  has one; the start waits up to 40 s for that run. It answers `200` `{state: "started", taskId,
  childThreadId}`, or `{state: "refused", code, message}` with `404` (`parent_missing`), `409` (any
  other refusal, such as `slots_full`) or `400` (a bad body).
- `GET /api/fork/delegate?parentThreadId=...` (`orchestration:read`) answers `{children,
  parentSettled}`: the parent's children (`taskId, title, status, live, environmentId, childThreadId,
  providerInstanceId, model`) and whether it was settled by hand.

### `server.modelPolicy`

Which models (and efforts) agents may pick, per provider driver. `mode` is `off` (agents pick any
model, as upstream's do), `advise` (agents are only told) or `enforce` (a pick off the list is
refused). Each `allow` row names a `driver` and a `model`; `efforts` (non-empty) limits the efforts
for that model, and a row without it checks only the model. `defaultEffort` must be one of the
row's `efforts`; `useFor` is a short note shown to agents when they pick.

The server checks each agent pick (#731; agents only, #125): `delegate_task`, `create_threads`,
`t3_thread_launch` and `t3_thread_configure` from an agent's thread, here and before a call is sent
to a linked machine. The machine there checks an agent's launch or model change against its own
policy too (the link names the agent's thread; a call the owner's client sends there is not checked),
so each machine refuses an agent's pick outside its own rows, whatever the sending machine's policy
says. An agent is
a caller whose session is bound to a thread; the owner's client has none, and anything that reaches
the server without such a session counts as a person, so the model guard hook is the backstop.
Claude's own Agent tool (Task in older Claude Code) is checked too, as each call is made (#128): T3
gives every Claude session it opens one PreToolUse hook that reads the policy on each call. The call
must name a model and, for a row with `efforts`, its `effort`. The Agent tool names only aliases
(`sonnet`, `opus`, `haiku`, `fable`): the alias is read as the full id this machine's Claude model
list maps it to, the same id T3 shows for the sub-agent, and a row matches when it names either the
alias (as `delegate_task` reads a row saved as `opus`) or that full id. The refusal says which alias
and effort pass, or that none does and the row's model can still run through `delegate_task`. The
mapping is T3's list for Claude, shared by every Claude instance on the machine; a Claude Code whose
own mapping differs (an older binary, or an alias override in its environment) runs a model the
check did not read. A name the list does not map is read as given (so it matches no row). A
built-in sub-agent type that names no model (one that would run on its own, often smaller, model) is
refused in `enforce`. A `fork` call is read the same way, as the owner decided for every call and
as the model guard reads it, although Claude runs a fork on the session's own model and effort and
ignores the model and effort it names. A skill that starts a sub-agent is checked too, on Claude's
Skill tool: a skill whose frontmatter says `context: fork`, or names `model` or `effort`, must name a
model from the rows in `model` (and its effort in `effort`), read like an Agent call (an alias or a
full id); a skill that runs inline and names neither runs on the session's model and is not checked.
T3 finds the skill as it lists Claude's skills (managed, the config folder's, the project's, and
plugin skills; first name wins). A skill it cannot find or read (one bundled with Claude Code, an MCP
server's, a command, one in another folder or with frontmatter it cannot parse) is refused in
`enforce` and runs with a note in `advise`. `enforce` refuses with the rows, `advise` runs it with a
note to the agent, an inside pick gets its row's `useFor`, and each call is logged as "model policy
pick" with route `Agent` or `Skill`. A check that fails refuses the call; while a policy is on when
the process opens, so does a hook that times out (`onFailure: "block"`). So while a policy is on,
in `advise` too, Claude Code 2.1.295 or newer is required: an older one refuses to open every new
query, with an error that asks to remove `onFailure: "block"` from the hooks (a person cannot; turn
the policy off or update Claude Code). With the policy off it is left out. The hook is fixed
when the Claude process opens: a process started before this version runs without it until T3
opens a new one (a new profile, a restart). Workflow `agent()` calls are not read here (the script
is code); the model guard reads them. On the MCP routes, a pick is inside when it
names a model as a row names it (#128; an alias such as `opus` is not read as its model there, and
the refusal names the id to use) that a row of its provider's driver lists (#127) and, for a row with
`efforts`, names one of them (`ultracode` counts as its own effort; a row without `efforts` allows
none) under the option id that driver reads it from (#780): a built-in driver's own id (`effort` for
Claude and FX, `reasoningEffort` for Codex, Grok, Kiro and Muse, `variant` for OpenCode, `thinking`
for Pi), else the effort control the machine's own catalog marks for the model on that driver's
instances (an ACP agent's `thought_level`, a Cursor model's `effort`). Under another id the driver
ignores it and runs its default level, so such a pick is refused (`reasoning: high` alone for
Claude), and so is a pick for a driver whose id the catalog does not mark (not read from the agent
yet, or the model not listed there): the level it would run cannot be told; the refusal says to
start that agent on the machine once, or to pick a model whose driver has a fixed id. Every effort
option the pick names counts, and one that holds no effort name (`{effort: true}`, which runs the
driver's own default) is refused. Every other option must be one this machine's model list
describes for the model (the instance's list; for a call sent to a linked machine, any list here),
or `ultracode` (#793): a driver can still apply an option the policy does not read, after the
checked ones (Kiro's `effortLevel`, Grok's native `thought_level`, an ACP agent's `_t3/session-mode`
that the list hides because it repeats the thought levels, or a `model` option written after the
model). Such a pick is refused in `enforce` (pick the model in the model field; the option is not
allowed while the policy is on) and reported in `advise`, as the model guard does. An option that
picks an agent or a mode is refused even when the list describes it (#802), as the model guard's
`DRIVER_MODEL_OPTION_IDS` does: OpenCode's `agent` (an OpenCode agent can carry its own model),
Kiro's `mode`, and an ACP `_t3/session-mode` on any driver (each brings its own prompt, tools and
settings the policy cannot see); without a known driver each of them is refused. So a second
effort option cannot hide a higher effort, and an option cannot change the model. A pick that leaves the ultracode option out runs at the model's default for it, so a model
whose catalog default is ultracode reads as ultracode. Without a known driver (a call for a linked machine; a `driverKind` the agent names is its
own claim and is not read), every row naming the model must allow it (the refusal names the drivers
whose rows refuse it), and each row's driver needs the effort under its own id as above (so a pick
for an ACP model this machine's catalog does not describe is refused here), every built-in driver's
effort option and every control this machine's catalog marks for the model is read too (a decoy
cannot hide a higher effort), a model this machine's catalog turns ultracode on for by default is
read both with and without it, and an alias gets the same "name the full id" hint as here. This
machine knows only its own catalog (#802): the machine there may turn ultracode on by default for a
Claude model where this one does not, so on a Claude row a pick that leaves the ultracode option out
is read both ways too (name `ultracode: false` or `true`); a control only the agent there has is
refused here (this machine cannot tell whether it sets the effort; start that agent here once so its
list describes it). When several instances of one driver here name a model's effort control
differently (two ACP agents listing the same model), the pick must name the effort under each id,
as which instance runs it is not known; the refusal names every id and why. This fails closed: an
instance whose list does not describe one of them refuses it there in turn, so pick a model whose
driver has a fixed effort id, or start the task where one instance runs it. The
machine there checks the pick again with its own catalog (#780). In `enforce` a pick outside is refused with the rows; in
`advise` it runs, and `delegate_task`'s reply says what `enforce` would refuse (the other routes
only log it). A pick inside gets its row's `useFor` in `delegate_task`'s reply. Each checked pick is
logged as "model policy pick" with its model as asked, so one model's picks can be counted; the
target the usage pacer moves a task to is checked too and logged as "model policy paced pick", the
model it ran on. An effort the pacer lowers (`usagePacingLowerEffort`) to one the row does not allow
keeps the checked effort instead, with a warning in the server log naming both settings: the policy
wins over the pacer, as the agent cannot fix that pick. A retry of an accepted call is not checked again (it starts nothing new), except a
`t3_thread_launch` retry that names a model: that model wins over the first one, so it is checked
like a new launch. A call sent to a linked machine is checked each time it is sent, a retry too: after
a policy change a retry is refused even when the first call already started its task there (that
task runs on, with no record here). Not checked by the server: a project's default model (`t3_project_create`,
`t3_project_update`) and `schedule_task`'s model, which a later run uses; the model guard hook
(`fork-kit/model-guard`) refuses an agent's such call in `enforce`. The owner's own picks (in the app, or `t3_thread_launch` from the owner's client)
are never refused; Settings marks the server's model picks outside the rows ("outside policy"),
each read on the instance of the machine whose settings hold it (#802; the default `claudeAgent` is
on every machine, with its own catalog).

Left (#731): a `t3_thread_launch` retry with the same key that names no model is not checked again.
After a policy change, the retry of an idle launch (one without a message) that adds a message starts
its run on the model checked under the old policy; so does an agent's retry of an idle launch the
owner's client made with that key. A retry that names a model is checked again: in `advise` it is
logged a second time (that model's picks count twice), and if the policy got stricter in between it
is refused although the first call's thread exists. Each needs the key of an earlier launch.

While the policy is on, a child that `delegate_task` (here or on a linked machine) or
`create_threads` starts with a call that names its model also keeps only the options the call names,
none of its parent's others (an agent, a session mode, a service tier, an effort). With `off` it
keeps upstream's way: a call that names the parent's model takes the parent's options under its own.

Only the person can change it; agents cannot. A save must give both `mode` and `allow` (it replaces
the stored value whole) and is refused for a bad row, a `defaultEffort` not in its row's `efforts`,
or an unknown key such as a misspelt `effort`. A hand-edited value that is not valid, an unknown key
included, reads as `enforce` with nothing allowed (with a warning in the server log): every agent pick
is refused, unless the value's own text names the mode `advise`, which only warns. Settings that
cannot be read at all refuse every agent pick too, also with the mode `off` (the mode cannot be
seen). A settings.json that does not parse fails closed the same way (upstream then runs on its
defaults, where the policy would be off; the server log says the model policy cannot be read), and a
file that parses but fails on another key keeps its own policy. The value is kept as written, and
saves of other settings write it back unchanged until the person saves this setting.

In the app: **Settings → Fork**, section Models agents may use (server only; a project cannot override it): the
mode, and **Edit allowed models** for the `allow` rows as JSON, checked as a save is. An unreadable
value is shown with a warning and the kept text; a save there replaces it. **Edit allowed models**
then starts from the value's own rows and saves only once the person picks the mode, so a save never
turns it into the fail-closed Enforce. A row whose driver no provider here has is flagged, and so is
a row whose model is an alias in its driver's model lists on a selected machine (`opus`; each
machine is read on its own lists, as its server reads only its own, so a model's id on one machine
does not hide an alias on another), or a name Claude Code resolves by itself (`opus`, `sonnet`,
`haiku`, `fable`) that no list there holds as a model's id: the driver resolves an alias, so the row would follow it when a provider moves it to a new model (#127). With no
rows saved, **Edit allowed models** starts from the fork's start rows (#126: `claude-opus-5-5` and
`claude-fable-5-2`, each `medium`, `high` or `xhigh`, default `high`), saved only when the person
saves them; they are not the setting's default, so a file without rows keeps meaning none.

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

### `server.placement`

What this machine has, and how it places delegated work on linked machines. This machine's facts
read (`t3_environment_read`, below) reads `labels` and runs `factsCommand`; the placing rows add
the rest. Fields, each with its default:

- `mode`: `off` (an agent picks a machine as upstream's do), `advise` or `enforce`.
- `labels`: `[]`. What this machine has, as free words (`"gpu"`, `"ci-runner"`, `"owner-logins"`):
  each is one word of at most 64 characters, at most 64 labels. They are stored trimmed and
  lower-cased (`"GPU"` is `"gpu"`), a repeat dropped. There is no Unicode normalisation: `café`
  written with a combining accent and without one are two labels.
- `factsCommand`: `""`. Empty, or the absolute path of one program on this server's OS (`/…` on
  Linux and macOS; `C:\…` or `\\server\…` on Windows). It is a path, never a command line: a
  space is part of the path, and no shell runs it. A relative path or a control character is not
  valid; a save that sets another OS's path is refused, and placing reads one in the file as `off` (below).
  Exit 0 means free for work, and each line it prints is one more label (trimmed and lower-cased as
  for `labels`; a line that is not one label is skipped, at most 64); any other exit, a program that
  cannot start, or no answer within 5 seconds means not available, with the reason. The machine
  that runs it checks the path again on its own OS first and runs it with no arguments; on Windows
  only an `.exe` runs, since a `.cmd`, `.bat` or script needs a shell, and that holds for the file
  the server's lookup would start, not only the text. A Windows server whose PATHEXT has no `.EXE`
  refuses every `factsCommand`: its lookup would try `facts.exe` plus each PATHEXT ending, so a
  `facts.exe.CMD` planted after the check could run through a shell. A failed run's reason is the
  first line of its error output, else of its output. The runner is in the fork
  (`apps/server/src/fork/factsCommand.ts`); one run serves every facts read for 15 seconds (reads
  at the same time share it). Example programs for Linux, macOS and Windows:
  [fork-kit/placement-facts](../../fork-kit/placement-facts/README.md).
- `never`: `[]`. Label groups that must not meet on one machine, such as
  `[["ci-runner", "owner-logins"]]`; each group has at least two different labels after
  lower-casing (`["GPU", "gpu"]` is one label, so not a group).
- `weights`: `{}`. Linked machine id (one word, no spaces) to `prefer`, `normal`, `less` or
  `manual` (upstream's load-balancing words); a machine without one is `normal`.

A value that is not valid (a bad field, a wrong type, a field this build does not know) reads as
`off`, with a warning in the log and the reason kept beside it for Settings to show; the rest of the
file still loads. The person's text is kept as written until the key is saved. A `factsCommand`
that is not a full path on this server's OS (a hand edit) is not converted by the load: Settings
shows it as written. Placing work reads `placement` only through the server's
`readPlacementForThisOs` (`apps/server/src/fork/placementRead.ts`), which reads it as `off`, kept
as written with the reason, each time; the machine that runs `factsCommand` checks the path again
before it runs it. A save that names only some fields keeps
the others; over a kept unreadable value only a save of every field is taken (it replaces the text),
and a partial one is refused ("placement is not valid; save the whole setting"), so it cannot drop a
`never` group. A newer build's field reads as `off` here and is kept through saves of other keys; a
save of `placement` on this build drops it. Only a person changes it: no agent tool can, and a
managed policy can lock it as a whole value (not one field inside it, as for `modelPolicy`).
"Person-only" means no T3 tool takes it; an agent with a shell can still edit the settings file or
call the settings RPC, as for `modelPolicy`.

Threat notes for the rows that act on it (P4 and later). `placement` (and `factsCommand` with it)
is a guard against mistakes, not against an agent that sets out to get around it: an agent with a
shell on a machine can change that machine's `placement`, point `factsCommand` at its own program,
or start work there without `delegate_task`. `factsCommand` runs with the server's own rights, so
only a program the person put there belongs in it. What a linked machine reports about itself
(labels, `available`, the counts) is taken as that machine's word; a machine whose answer the
schemas refuse is refused, never read as busy or free.

```json
{
  "placement": { "mode": "advise", "labels": ["gpu"], "never": [["ci-runner", "owner-logins"]] }
}
```

### `server.placementNeeds`

Labels a machine must have to take a project's work, for each project without its own
(`projectOverride.placementNeeds`). Same label rules as `placement.labels`. Default `[]`: no needs.
A value that is not valid fails closed: that work goes to no machine ("this project's
placementNeeds setting is not valid: …; fix it in Settings"), never to any machine as `[]` would.
The reason is kept beside it and logged, and the person's text is written back unchanged by saves
of other settings until this key is saved, which replaces it. A managed policy can lock it as a
whole list; the lock also holds each project's own needs.

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
  minutes while it waits, upstream's missed-run rule applies. "Run now" fails with
  `Not run, no agent slot: ` and the reason and changes nothing; over MCP `run_scheduled_task_now`
  answers `slots_full`. A webhook delivery waits the same way and starts once when a slot is free;
  it ends without a run only if its task is paused, deleted, replaced or its trigger changed while
  it waits. A save while it waits that changes what a run uses (prompt, title, project, thread,
  mode, model, workspace, schedule; a save in the same millisecond too) is seen at its next retry,
  and so is a save between the stored-delivery worker's read of the task and the run's own read,
  wait or not: it is not run on the task or prompt from before the save, and not dropped. It stays
  stored, and the stored-delivery worker reads the task again, renders the prompt again and waits
  again if no slot is free ([#719](https://github.com/scuttletoby/t3code/issues/719),
  [#750](https://github.com/scuttletoby/t3code/issues/750)). A write that changes none of that (a
  new webhook token, a pause undone before the next retry) does not hand it back, and a retry that
  cannot read the task asks again at the next one. The first such retry logs a warning (again only
  after a good read); a delivery whose task stays unreadable for 60 retries in a row (5 minutes,
  counted in retries so a sleeping host does not end it early) ends without a run, saying why and
  logging the last cause, so later deliveries of the task do not wait behind it
  ([#756](https://github.com/scuttletoby/t3code/issues/756),
  [#763](https://github.com/scuttletoby/t3code/issues/763)). Not covered: a save in the instant
  between the last check and the start, or between a direct delivery's read (no coalescing window)
  and its first try for a slot: upstream's read-then-run window, as for any run. Closing the direct
  one needs no new upstream line: it would change the existing fork seam line on upstream's
  direct-delivery call (`runSlots.directDelivery(...)` in `ScheduledTaskService.ts`) and its entry
  in `.github/fork-seams.json`, to pass in the template the delivery was rendered from. It is left,
  as the window is milliseconds wide. Every waiting delivery is in the store, so a restart keeps it
  and it runs once: a direct one is moved there when it is first refused. If that store fails, the
  delivery waits in memory; a save then stores it, and if the store fails again it ends without a
  run, saying why. A scheduled run or a webhook takes a slot only when no agent that waits for one
  holds it. A run on its schedule and a webhook delivery that wait keep a place in the gate's one
  line (below), a run at its due time and a delivery at the time it was stored, so a waiter that
  joins later never passes them. The place goes when the run starts, when the delivery is given up
  (task paused, deleted, replaced or retriggered) or handed back to the stored-delivery worker
  (which asks again at the row's due time), and within 5 s of its task being paused, deleted or no
  longer due (moved to a later time). "Run now" asks once, from the back of the line.
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
  wake-up as an agent of its own, so slot waits met in one sweep are each woken. That held slot
  lives in the gate's memory only: a wake-up that itself waits for a slot keeps it for
  `reserveSeconds` at most, and a restart drops it, so that wake-up then asks like any other start
  (late, never over the limit). A receiver found stopped frees its held slot at once.
- The fork's delegated-result reminder and its orphan scan (a parent that is idle and still owed a
  child's result is sent one reminder) take a slot the same way. Refused, nothing is sent: the
  reminder is asked again every 5 s, however long it takes, and a refusal is not counted as a
  failed try (logged once). Its message and command ids are fixed per result, so a restart's
  startup scan finds the result still owed, waits again and sends it once. An orphan scan parent
  that cannot be read is asked again every 10 minutes, logged once, at most as many times in a row
  as the scan's own attempts, then left to the next start. Once a waiting parent's reminder goes,
  its linked threads are listed again, so an orphan that showed meanwhile is named too.
- Upstream's pull request watch wake-ups (checks passed or failed, a review, merged or closed,
  "stopped watching") into a thread that shows no run take a slot the same way
  (`apps/server/src/fork/pullRequestWatchSlot.ts`). Refused, the update fails and nothing is
  stored or sent: upstream's watch reactor reads the pull request again on its next sweep (every
  2 minutes), finds the same news against the stored watch and sends it then, once. A restart
  finds it again from the stored watch. A watch that ends because its pull request cannot be read
  keeps its "stopped watching" wake-up the same way, and is not ended without it. Into a thread
  that shows a run a wake-up only queues: no slot. While a wake-up waits, upstream's reactor logs a
  warning and reads the pull request in full (its details and activity) on every sweep, so a host
  kept full with several watched pull requests spends GitHub API calls and log lines every 2
  minutes per watch until a slot frees. A watch that no refusal renews for 10 minutes (removed, or
  its thread gone) is forgotten by the gate's wrapper.
- Upstream's usage-limit resume ("Continue where you left off." into each thread that failed on a
  limit, at its reset) takes a slot the same way (`apps/server/src/fork/limitResumeSlot.ts`), so a
  reset does not start every failed thread at once. Refused, nothing is sent: the thread stays
  failed and armed, and upstream's recovery worker asks again on its next tick (every 5 s; logged
  once per resume). An armed resume has no time limit, a restart finds it from the stored thread,
  and its id is fixed per failed run and reset, so it is sent once.
- One line, oldest first ([#738](https://github.com/scuttletoby/t3code/issues/738)): every
  wake-up that asks again while refused keeps a place in the gate's line from its first ask (the
  PR-watch wake-up, the usage-limit resume, a wait's message, a waiting scheduled run or webhook
  delivery, and the four below), and so does every slot wait, at its own age. A slot that frees
  goes to the oldest waiter that can use it: a start without a place (`delegate_task`, a send,
  "run now") and every younger waiter count the waiters ahead of them as starts. A thread with two
  places (a question wake and a continuation) counts from its oldest. A waiter whose own account is
  full (the account of its last ask: its thread may have moved) holds nothing from the others, a
  thread that shows a run only queues, and a thread with a held slot starts on it. A free slot is kept for the oldest waiter for 15 s (a slot wait: 2
  sweeps) without it asking; if it does not come back by then, the waiters behind it go first until
  it asks again, still in its place. So a waiter that asks seldom (the PR-watch wake-up, at
  upstream's 2-minute sweep) or that died leaves a slot idle 15 s at most; the PR-watch wake-up
  gets a slot only when one is free as it asks, as before the line. The fork could hold a refused
  PR-watch wake-up and ask every 5 s itself (as it does for idle compaction); it does not, because
  upstream's reactor waits for that dispatch, so holding it would delay the other watches' news,
  and a retry of its own would race upstream's next sync for the thread. A waiter leaves the line
  when it is let through, when its wake is no longer owed (its thread gone, its question answered, a
  handoff that is no longer due), or when it has not asked again for 30 s (a PR-watch wake-up: 5
  minutes; a slot wait: 3 sweeps); places that stopped asking are dropped at the next ask, under
  any limit. A refusal names the waiters in line ahead apart from the running agents ("2 agents
  are running on this machine and 1 start is waiting in line ahead of this one"), since
  `wait_list` shows only what runs, starts or is held. The line is memory only: after a restart
  each waiter joins again at its next ask. Held question wakes ask at once, in their file's order,
  but at the time of that ask (the file keeps no age): a slot wait made before the restart, or a
  scheduled run due before it, comes first.
- The bound-thread and message routes decide "running or idle" from the gate's own count in the
  same step that takes the slot. When the thread's run ends between that check and the send, the
  queued message starts the next run: the call then holds a slot until that run shows, for the
  start grace (`startGraceSeconds`) at most, so no start ever goes past the gate.
- A call that asks another machine to start an agent (`delegate_task`, `t3_thread_launch` or
  `t3_thread_send` with an `environmentId`) is decided by that machine's gate, the one that would
  run the agent; this machine's gate is not asked. A machine on a build without this gate starts
  it as before. A `delegate_task` child is first paced there by that machine's
  `usagePacingGate`, so a held task takes no slot.

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

These start a run in an idle thread on their own, and take a slot the same way
([#724](https://github.com/scuttletoby/t3code/issues/724)). Into a thread that shows a run each
only queues: no slot.

- The fork's context handoff, and its one resend of a failed handoff run
  (`apps/server/src/fork/handoff/handoffDispatch.ts`, after its busy check). Refused, nothing is
  sent: it asks again every 5 s or at the thread's next event (logged once), deciding again each
  time without a new settle, and a thread that moved on drops it. Its text (a read of the last
  runs' turns) is built only once it has a slot. Like every handoff timer it is
  memory only: a restart drops a waiting handoff, and the thread's next turn end decides again. A
  waiting resend of a failed handoff run is dropped too, with no log: the failed run's text stays
  in the thread, and the next turn end decides again.
- Upstream's provider continuation ("Background task completed": an agent that ended its turn
  while its own background work ran starts again when that work ends;
  `apps/server/src/fork/continuationSlot.ts`). The slot is asked off upstream's queue, each
  request in a fiber of its own, never inside an adapter's `dispatchIfCurrent`, which holds the
  adapter's own lock: a slow count read delays only that request, never upstream's queue. Refused,
  it is asked again every 5 s (logged once); the adapter offers no second one meanwhile. A granted
  slot is held until upstream's dispatch of that request ends (it may wait behind its own thread's
  earlier dispatch), then counts until its run shows, for the start grace at most; a request
  upstream never dispatches holds it 10 minutes at most. A delegated result is left as above. It waits in memory, as upstream's queue does: a
  restart loses it, as the restart ends the provider session whose background work it reports
  (Claude's buffered output lives in that process).
- A delegated task's question to its idle parent (upstream's question reactor;
  `apps/server/src/fork/workerWakeSlots.ts`). Refused, nothing is sent: the wake is kept in
  `fork-held-wakes.json` in the server's state folder and asked again every 5 s (logged once). It
  leaves the file once sent, or when the question is answered, its task ends or the parent is
  archived or deleted. A restart reads the file and sends it once; upstream's fixed command id
  keeps a send cut off before the file write from making a second message. Saves run one at a
  time, each writing what is held then, synced to disk before the rename and the folder after it.
  A file that does not decode is moved to `fork-held-wakes.json.corrupt-<ms>` beside it and
  logged, so the next save does not write over it (when that move fails, the file is handled as
  one that cannot be read, below: kept as it is, nothing written over it); an entry that does not decode is logged and
  left out. A file that cannot be checked or read (an I/O error, not "missing") is logged once and
  never written over: new holds stay in memory, and every 5 s it is read again, its wakes then
  going first. A held wake whose parent or question cannot be read waits, logged once.
- Upstream's idle compaction (same file), after the handoff owner's check. Refused, it is held in
  upstream's own timer and asked again every 5 s in its place in line (logged once), while the
  fork reads again that the thread is still idle at that run and at least a minute of its prompt
  cache window is left (upstream's `idleCompactionWindow`; half the 2-minute lead upstream sends
  it with). Once not, it is skipped (logged): a late compaction costs a full uncached read. A
  check that cannot be read skips it too, logged with its cause. A newer completed run replaces it, as upstream's timer does; a
  restart drops it (upstream).

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

Switch on: **Settings → Fork**, section Limit on running agents, "Limit running agents", or in `settings.json`:
`"agentSlots": { "enabled": true, "maxPerMachine": 4 }`. With `enabled` and no limits set, nothing is
refused.

`mode` says how the gate decides. `"fixed"`: by the numbers above, nothing else. `"observe"`: by the
same numbers, and a capacity rule (`packages/shared/src/capacity/`) runs beside each decision on live
memory readings and only records what it would have answered: one log line per decision, and the
last result with counts under `slotRule` in `wait_list`. It never changes or delays a decision; a
reading that fails or takes over 2 seconds counts as "unreadable". While switched off the gate keeps
no records of new agents, so the rule counts each agent allowed in the last `startGraceSeconds` as
starting. `"enforce"`: the capacity rule decides. Every agent still starting owes its full cost
(two starts in one second both count, and a held slot owes a writer's), costs come from the rule
file (light 384 MB, writer 768 MB), and an agent's kind comes from its task `role` (`research`,
`review` and `design` are light, anything else or no role is a writer). A role can be wrong: a
running agent that uses more than a light cost counts as a writer, and so does one whose memory
cannot be read, whatever its role (it is taken to use a light cost already, so it is never counted
twice). So where agent memory cannot be read, every agent counts as a writer (768 MB, of which 384
are taken as in use): on macOS, Windows and Linux without systemd, and for Codex threads, which
share one scope, that caps writers at about 2 to 4 on a small machine. It is the strict side, by
design. The floors (2 agents, 1 job) always start, whatever the readings, unless real readings say
"severe". The agent ceiling is 3 per core, at most 64, only on Linux with cgroup v2, agents in their
own scopes and the kernel's OOM counter; anywhere else it stays at 10 agents and 4 jobs. After an
OOM kill (`oom_kill` in `/proc/vmstat` rises) or with swap under 10% free and filling, the gate
falls back to at most 10 agents and 4 jobs. Swap is "filling" when its free MB fell by more than the
rule's `swapFillingMb` since the reading it compares with: that reading is kept, and replaced by a
newer one only once it is at least 25 seconds old, so a burst of asks a second apart never hides
slow filling. Only the kernel's own OOM kills count: a killer that runs as a program (earlyoom,
systemd-oomd) moves no counter, so its kills trip nothing and raise nothing (the next readings still
see the memory it freed). A kill inside an agents' slice also raises each agent cost by 25% (a kill
elsewhere, such as a build box at its own cap, does not; where the slices cannot be read, at most 2
raises). Ten minutes of calm, whole readings lift the fallback and keep the raises (so kills about
11 minutes apart keep their raise); each further calm spell takes off one. The gate reads the
machine every 30 seconds as well as at each ask. A restarted server starts in the fallback and at
"elevated", which lifts after about a minute of calm readings (no stall, memory above the margin,
swap not filling: swap that is full of old pages does not count). Readings that fail count as
"elevated": the floors still start, and after 2 minutes without a whole reading agents are counted
against the fixed 10 until one comes back. A reading that fails, or one with a part that cannot be
read (a box's pressure file), after the level was "ok" lifts again like a start (memory above the
margin, not 150 % of it), so it never keeps the gate "elevated" for good; while the level is up from
pressure seen, such a reading keeps the 150 % bar. A reading of the 30-second timer that fails or
crashes is logged and the timer goes on. Settings that cannot be read give the fixed 10 too (with a
warning), never no limit.
Typed limits (`maxPerMachine`, ...) still apply while `enabled`. Without `mode`, a
switched-on gate is `"fixed"` (a file from an older build decides exactly as before) and a
switched-off one is `"enforce"`; a mode this build does not know (from a newer build) is `"fixed"`.
`"mode": "fixed"` turns the rule off and stops the memory reads at each `delegate_task`.

### `server.headlessUsageRefreshInterval`

How often the server reads usage limits while no app is open, in milliseconds. On by default every
10 minutes, so pacing has current numbers on a machine nobody is looking at. Read only: it changes no
thread, setting or install. Never more often than the provider status refresh.

Switch off in `settings.json`: `"headlessUsageRefreshInterval": 0`.

### `server.mcpProfiles`

Named MCP profiles. A profile says which outside MCP servers (`servers`) and which groups of T3's
own tools (`t3Tools`) a thread gets; each is `"all"` or a list. Three profiles are built in: `all`
(everything, today's behaviour), `t3-only` (no outside servers, every T3 tool) and `assistant` (see
[assistant.md](assistant.md): no outside servers or built-in tools, three T3 tool groups); a profile cannot
take their names, and one written in by hand makes that built-in refuse threads until it is removed
(a save may send `null` under a built-in name to remove it) or renamed.

A Claude thread applies the profile's `servers`: `[]` (and `t3-only`) starts Claude without any MCP
server of its own (its config files, plugins and claude.ai connectors), and a list keeps only the
servers it names, written as Claude lists them (`claude.ai Calendar`). T3's own tools stay. A Claude
thread whose profile cannot be applied does not start its turn and says why: a missing or broken
profile, a parent thread that cannot be read, a `settings.json` T3 cannot read (when only a
provider secret cannot be read, the profile comes from `settings.json` alone and the server log
says so; with no profile set there, the thread gets `all` as before), or a session that
still shows a server the profile leaves out, or does not list its servers within 5 minutes. A
managed Claude policy on the machine or `--mcp-config` in the Claude launch arguments can make
Claude load a server T3's list leaves out.

Every thread applies the profile's `t3Tools`, on any provider: T3's own MCP server judges each
request of the thread's MCP credential. A T3 tool outside the profile's groups is left out of
`tools/list`, left out of `tool_search`'s results, and refused when called, with a plain reason and
its fix (the call gets a JSON-RPC error: ask the owner to add the group to the profile, or to pick
a profile that has it). The server matches a call by the tool's exact name, and refuses a JSON-RPC
batch whole, so no other way reaches it. `request_secret`, `send_to_user` and `tool_search` stay in
every profile, and `[]` keeps only them. A tool in no group is refused by any profile but `all`,
and the refusal says that is a T3 bug to report (no setting fixes it); a test keeps every T3 tool
in exactly one group, and every group with a toolkit. The groups are fixed as each turn starts (as a Claude query opens, or as a turn
on another provider starts, also a resumed turn and a compaction): a running turn, and a message
sent into it, keep the set it started with, and a change applies from the thread's next turn. A
Claude thread whose groups changed starts a fresh Claude process for its next turn (resumed), so
Claude lists T3's tools again; that costs one prompt-cache miss. Another provider may keep the
list it read before: a tool it still shows is refused when called, and one newly kept is there the
next time it lists. The sets live in the server's memory, one per thread, not per credential (a
credential reused after a change gets the thread's new set from its next turn), at most 4096 of
them: past that, the thread that went longest without a turn loses its set, and its next request
works it out again from the settings as they are then, so a change made since applies to it within
its running turn (never wider than the settings). After a restart, or for a thread
whose session lists its tools before its first turn, the set is worked out from the settings at
the first request. A thread whose profile cannot be worked out (a missing or broken profile, a
parent that cannot be read, settings that cannot be read, a thread T3 cannot read because it was
deleted) keeps only the tools every profile keeps and is told why, never `all`. Such a refusal
worked out by a request (not by a turn) is reused for 5 seconds, so a list request reads the
settings once, and is never kept as the thread's set: once the read works, the next request gets
the real set. A delegated child
is cut down to its parent's groups like its servers, so it never gets more. Clients from outside
a thread (signed in with OAuth) have no profile.

Profiles are not a sandbox. Only requests that come from a thread are filtered: a request with no
thread (an OAuth client, the `t3 thread` CLI, a script run from a shell with the owner's access)
gets every T3 tool. So a profile limits what an agent is offered and may call as its thread, not
what an agent with a shell can reach. A thread that keeps the `orchestration` group can also ask a
wider thread to act for it (`t3_thread_send`).

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
covered below; a T3 from before those checks sends them unchecked too, and each arrives here with
no calling thread: a fork gets its project's profile cut to its source's, a change to a task is
recorded as saved from another machine, and a run now is checked against the task's record (both
below). A thread on `all`, and the owner's own client, launch as before.

The same rule holds for the other ways such a thread starts a thread on this machine
([#693](https://github.com/scuttletoby/t3code/issues/693)). `schedule_task` is refused when the
task's runs would start a new thread (not bound to the caller) whose profile in the task's project
is not within the caller's; a task bound to the caller runs in it and keeps its profile. A change
to a task whose runs start new threads (also one made before this check), and unbinding a task from
the caller, are refused the same way: delete the task, bind it to the caller
(`bindToCurrentThread: true`), or ask the owner. `run_scheduled_task_now` on such a task is refused
too. A retry of a `schedule_task` create that was accepted before is checked again, so it is
refused when the profiles no longer allow it, though the task exists. These refusals are checked
again by the scheduled task service on the task it reads as it saves, so a change another caller
makes in between is seen ([#699](https://github.com/scuttletoby/t3code/issues/699)). A change that
races the owner's move of the task's thread to another project (which moves the task with it) is
refused when the move lands before the service reads the task, so it cannot put the task back in its
old project without its thread; the task stays as the move left it, and is read and changed again
([#704](https://github.com/scuttletoby/t3code/issues/704)). A move that lands after that read, in
the short window before the row is written, is not seen: the task goes back to its old project and
keeps its thread (a bound task stays bound, so its runs get no wider). The app's own save is not
checked this way: the project the owner saves is the one they picked. A narrowed thread also
cannot change the prompt, project or thread of a task bound to another thread whose profile is not
within its own (that thread would run its words); reading, renaming, pausing it and changing its
schedule still work. The `schedule_task` handler checks that before the service is called, so a
refused change does not use up the webhook secretRef it carries.

Each save records who made it: the owner (the app, or an MCP client of this machine with no
thread), a thread here and its profile then, or another machine. Another machine does not say the
profile of its thread, so its save records none. The record is `scheduled-task-mcp-profiles.json` in
the server's state folder (the fork changes no database schema). A scheduled run,
`run_scheduled_task_now` or a webhook run that starts a new thread is refused when that thread would
get more than the recorded profile, for example after the owner widened the project's profile. It
is also refused when the task was saved from another machine, and when an agent made it and it has no
entry (saved before this record, or the file was lost or an older copy put back). The owner saves
the task once in the app to allow it; a task the owner made with no entry runs as before. A
webhook delivery that waited for an agent slot is checked again as it starts. If the task was saved
again while it waited, it is read again and checked on the task as saved
([#719](https://github.com/scuttletoby/t3code/issues/719)): a save the record allows runs it, once,
with the new prompt. A scheduled run or `run_scheduled_task_now` whose read a save overtook (a
short window as it starts) is refused, saying so; the next run uses the task as it is now. A
refused scheduled run is recorded as failed with the reason and moves on to its next time; a
one-time task then has no next time, so after the owner's save it is run now or given a new time
(the reason says so). A task whose row changed after its record (a save
stopped half-way) is refused until it is saved again, and a record file that cannot be read refuses
saves and runs that start a new thread until it is fixed or removed (the server log warns once while
it stays so, and again if it breaks again after a good read). Runs into a bound thread keep that
thread's profile and are not held up. That includes a task with `threadMode: "continue"` that has
no thread yet: its first run is checked, later runs go into the thread that run made, and if the
owner widens the project, that thread and those runs widen with it.
A change saved from another machine is not checked against the thread a task is bound to
here: a current sender refuses a narrowed thread's change first, but a T3 from before that check
forwards it. A thread bound while a save is being made (the task's first run binding the thread it
made, or another thread binding the task to itself) is kept by that save and not checked against
it ([#719](https://github.com/scuttletoby/t3code/issues/719)): the first run's thread gets the
project's profile the save was checked on, and a thread that binds the task to itself is one a
narrowed thread can already send words to: `schedule_task` and `t3_thread_send` are both in the
`orchestration` T3 tool group, so a profile that keeps one keeps the other, and a profile without
it can do neither.
An owner-made task that a narrowed thread changed runs as the owner's again if its entry is dropped
from the file, which takes owner-level access to the state folder. Entries of deleted tasks are
dropped at each start.
`t3_thread_transfer`'s fork is refused when the fork would get more than the caller's profile: a
fork gets its project's profile cut to its source's, so forking the caller itself always goes
through, and a fork of a thread whose parent cannot be read is refused. On
another machine, a fork, a change to a scheduled task and `run_scheduled_task_now` are refused for
such a thread, as a thread started there gets that machine's profile.

A Codex thread applies the profile's `servers` too. Codex runs one app-server for all of an
instance's threads, so T3 sets the servers per thread, as it sets its own `t3-code` entry: when it
loads a thread (starts, resumes or forks it), it asks that app-server which MCP servers are set up
for the thread's folder (Codex's `config.toml`, a project's `.codex/config.toml`, and servers added
by `-c mcp_servers…` in the Codex launch arguments) and gives that thread
`mcp_servers.<name>.enabled = false` for each one the profile leaves out, and `features.apps = false`
unless the profile names `codex_apps` (Codex's connectors server, which it adds with a ChatGPT
sign-in and which that list never shows). T3's own servers stay: the `t3-code` and
`t3-code-direct` entries T3 sends with the load. A server of the owner's own config that has one of
those names but that T3 did not send is switched off like any other. Other threads on the same
app-server keep their own sets. Codex's own sub-agents copy their parent's loaded config, these
settings included. Before each turn (and before a goal is set running) T3 reads the thread's live
servers from Codex and does not start the turn, saying why and how to go on (pick `all` for the
thread, or fix the profile), when one the profile leaves out is still switched on. Turns Codex
starts on its own (a goal's next turn, a sub-agent's turn) are checked as they start: one that
fails is stopped, the goal is paused, and the turn ends failed with the reason. A turn fails this
check when the profile was narrowed while Codex had the thread loaded (Codex reads a thread's
config only as it loads it, so the change counts once it loads the thread again: after it has been
idle a while, or after T3 restarts) and for a server a Codex plugin adds, which T3 cannot switch off
this way. A turn is also refused when the profile is missing or broken, Codex's servers cannot be listed, a server's name cannot be written as a config key, the
thread is not in T3's store (read 4 times over about 2 seconds first, as a thread T3 has just
made can reach the store a moment after Codex asks for it), or (once any thread on that app-server has a narrower profile) Codex
asks to load or run a thread T3 cannot match to one of its own. Codex answers the server list only
once the thread's servers have started, and T3 waits up to 30 seconds for each read: a kept server
slower to start than that refuses the turn, which can be sent again once it is up. When a resume is
refused (for example a read failed once), T3 does not move the thread to a fresh Codex session,
which would lose its history: the turn fails, and sending it again resumes the thread. A thread on
`all` sends Codex nothing extra, but T3 still works out its profile from the settings and the
thread store at each load and turn, so a thread missing from the store refuses there too. A turn already running when the profile is narrowed keeps its servers
until it ends. A Codex thread's T3 tool groups are cut on T3's own server, as on every provider
(above); each Codex turn fixes them as it starts, also one Codex starts on its own (a goal's next
turn). A sub-agent's turn, and a goal set while a turn may run, are checked but never fix the set:
the sub-agent uses its parent's T3 thread, and must not change the set of the parent's running
turn. While a goal's turn has its set fixed, T3's server holds that thread's requests, so the turn's
first T3 tool call already gets the new set. The hold covers only working out the T3 tool set: the
check of the thread's outside servers runs after it, so T3's own tools already carry the new set
while that check runs. A hold lasts 10 seconds at most from when the turn started (a list request
waits once, not once per tool); one still there then is dropped, and the thread keeps only the T3
tools every profile keeps, with a "try again" reason, until the late check or the thread's next
turn fixes its set. A hold is taken only by a check that runs, and released however it ends.

Codex is checked at each call too, where it lets T3 see one
([#814](https://github.com/scuttletoby/t3code/issues/814)). A request the app sends to one MCP
server of a thread (`mcpServer/tool/call`, `mcpServer/resource/read`, for MCP Apps) is refused when
the thread's profile leaves that server out, and an MCP server's elicitation (also Codex's
approval of an MCP tool call, when its approval policy asks for one) is declined then, with a
warning in the server log. `mcpServerStatus/list` is not refused: it only reads which servers are
on. Once any thread on an app-server has a narrower profile, those requests on a Codex thread T3
cannot match to one of its own are refused too. A Codex sub-agent T3 did not see spawned (for
example one from before an app-server restart) is matched by asking Codex for its parent, at its
resume and at its turn; a turn on a thread T3 still cannot match waits up to 5 seconds for the
spawn to show, then is stopped like a refused turn.

Edges of the Codex turn check. It runs as Codex reports the turn started, so a turn Codex starts on
its own can run briefly (a model step, possibly a call to an outside MCP server when no approval
is asked) before it is stopped; T3's own tools already get the turn's set. A turn that ends before
its check does is not stopped: its goal is still paused, without a message. Requests to T3's server
that come from no thread get every T3 tool, as above (a known limit, fork issue #918). A server a Codex plugin adds is not switched off: every turn is refused while it
is on. Switching it off through the plugin's own config key is not done, as a key Codex does not
accept can make it refuse to load the thread at all.

Only Claude and Codex threads apply a profile's outside servers. A thread on any other provider
would get every outside MCP server whatever its profile says (T3's own tool groups apply on every
provider, above). So its turn is not started when its profile leaves out any outside server
([#672](https://github.com/scuttletoby/t3code/issues/672)): the machine that
runs the turn checks the thread's profile as the turn starts (also a resumed turn, and a compaction,
which some providers run as a `/compact` prompt) and refuses it, saying why and how to go on (switch
the thread to a Claude or Codex instance, or pick `all`, or a profile whose servers are `"all"`, for
it). A profile whose `servers` is `"all"` and whose `t3Tools` is a list runs on every provider. That covers every way a thread gets there: a
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
machine (see below), so the task is not made only to be refused there; that holds also for a
profile that only cuts T3 tools, as the other machine may run a T3 from before the T3 tool filter.

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

A profile may also say `"builtinTools": "none"` (optional; absent, or `"all"`, keeps the provider's
built-in tools as before). Such a thread gets no shell, file, web or other built-in tool, only MCP
tools (T3's own, and the outside servers its `servers` keeps): a base for an assistant that reads
the owner's data without changing files. Only Claude applies it: its query opens with no built-in
tool (`tools: []`, and every built-in Claude names in `disallowedTools`), and a call to any tool
that is not an MCP tool is refused at the call, with the reason. A thread whose profile changes to
it starts a fresh Claude process for its next turn, so a process that still has its tools is not
reused. Every other provider (Codex too) cannot run without its built-in tools, so a turn there is
not started and says why (switch the thread to a Claude instance, or pick another profile); Codex
also stops a turn it starts on its own for a goal, but only soon after that turn starts, so its first
tool calls may already run. A child keeps `none` from its parent (never more
than its parent), a delegated task carries it to the other machine, and a delegation refuses, before
sending, a non-Claude instance there, as for any profile narrower than `all`. A T3 from before this
field reads a profile or carried task with it as one it cannot read and refuses it, never giving it
the tools; a machine on such a T3 that takes delegated tasks accepts the task, then refuses its
turn, so a thread that does nothing is left there (never one with more tools). Any other value makes
the profile broken.

`builtinTools` removes only the provider's own tools. For a really tool-less (read-only) thread,
also give `t3Tools` a list of only the groups it needs (leave out `orchestration`, `playtest`,
`worktree`, `preview` and other groups that act: they can ask another thread to act, run build
steps, change worktrees or run page script), and `servers` a
list of read-only servers: an outside MCP server may wrap a shell or write files. Not a sandbox: a
Claude setting that turns hooks off, or launch arguments that add tools, can weaken it on a machine
the owner set up so.

```json
{ "mcpProfiles": { "lean": { "servers": [], "t3Tools": ["orchestration", "threads"] } } }
```

In the app: **Settings → Fork**, section Tool profiles. It lists the built-ins (read-only)
and each named profile, and adds, edits and deletes named ones: `servers` is all or a list of
names (the editor offers the names other profiles use and takes any other; no list of the
machine's servers reaches the app yet), `t3Tools` all or a list of groups, and a "No built-in tools" box sets `builtinTools: "none"`. `request_secret`,
`send_to_user` and `tool_search` always stay. A built-in name cannot be taken, and a profile that
the default, the sub-agent rule, a project or a thread pick names cannot be deleted: the panel
says which, and offers to clear the thread picks that name it; the server refuses such a delete
too, naming who uses the profile. An entry this version cannot read
(fields from a newer build) shows as broken and cannot be edited there, only deleted; one that
names T3 tool groups this version does not know shows as from a newer version, the same way. The
default profile and the sub-agent rule have the usual project-override control, and each thread's
pick (under Advanced) is listed by title, changed or cleared. Adding a pick saves nothing until a profile is
chosen next to the thread id; the panel says what the pick gives the thread, and when that is more
than the parent's cut-down set (a pick is never cut down to the parent's). A blank pick from a hand
edit shows as broken, as it is ignored, and can be cleared. Every save sends only the entries it changes (a pick
names one thread), so another profile or another thread's pick is never overwritten, and a change
applies from the thread's next message. A client without permission to change settings sees the
rows but cannot use them; the server refuses such a save anyway.

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
500). `source` says who writes the handoff: `none` (the default: T3's state and the recent
conversation only), `agent` (the old session, in one turn; `instructions` replaces the built-in
text) or `command` (a program given as `command`, run without a shell in the project folder; it
must only read and be safe to run twice; its output is the handoff). Settings saved before `none`
existed have no `source` and read as `none`, so they start no write turns until `agent` is chosen.
`sizeTokens` (150,000) is the size backstop, `idleTokens` (75,000; `0` never) the
smallest idle thread handed off before its cache expires, and `modelSelection` the new session's
model (not set: the thread's current one). A save with a bad value, a field T3 does not know (a
typo), or `source: command` without a command, is refused; a hand-edited value that is not valid reads as off (with a warning in the
server log), never as `handoff`. The server also refuses a save that sets a new `command` (here or
in a project's `handoff`) whose program is not a full path to a file on the server, or lies inside a
git checkout an agent could edit; the script it hands a known interpreter, a module by name, a
program `env` finds on the `PATH`, and runners such as `make` or `npm` are refused too. It guards
against mistakes, not attacks: [handoff.md](handoff.md) lists what it does not catch. A command
already stored is not checked again. The command gets a short list of the server's environment.

In the app: **Settings → Fork**, section Handoff instead of compaction: the mode, each thread's own mode (added by thread
id, shown with its title where this client knows the thread), and under Advanced the sizes, who
writes the handoff (with its command) and the instructions. `modelSelection` has no row yet; a save
there keeps it as stored.

```json
{ "handoff": { "mode": "handoff", "threads": { "<thread id>": "compact" } } }
```

### `server.threadRoles`

Which threads hold a role, for the calm sidebar: the `assistant` thread (or `null`), and in
`orchestrators` one thread per project (project id to thread id). A role is a pointer in the
settings, not a tag on the thread, so a project has at most one orchestrator and the roles are a
setting the person changes ([#138](https://github.com/scuttletoby/t3code/issues/138)). The default,
no roles, changes nothing. This setting alone shows nothing: the calm sidebar
(`client.calmSidebarEnabled`) reads it.

Only the person can change it; agents cannot (the settings tool refuses it as person-only). A save
that sets a pointer anew is refused unless the thread exists and is not deleted, is a top-level
thread (not a sub-agent, a side chat or a task delegated from a linked machine) and, for an
orchestrator, belongs to the project its entry names. Where the server cannot read threads, such a
save is refused too, never let through. A pointer the save leaves as it was is not checked again,
so an old pointer to a thread deleted since blocks no other save; whoever reads the roles checks the
thread and treats a failing one as no role. An archived thread can take a role, but readers treat an
archived or deleted thread with a role as no role. An entry for a project removed since stays in the
file (no sweep clears it) until a save removes it; readers skip it.

A save changes the roles field by field: a left-out field keeps its value, `assistant: null`
removes the assistant, and each `orchestrators` entry is set or (`null`) removed on its own, so two
saves for different projects keep both. A save with a bad id is refused. The file is read leniently:
a missing or non-object value reads as no roles, a bad `assistant` as none, and a bad orchestrator
entry drops alone, so a hand edit never resets the other settings. A dropped entry is gone from the
file after the next save of any setting, since the save writes the settings as read.

```json
{
  "threadRoles": { "assistant": "<thread id>", "orchestrators": { "<project id>": "<thread id>" } }
}
```

### `server.assistant`

`enabled` offers **Make assistant** in **Settings → Fork**, section Assistant: it makes a thread in
the Scratch project, picks the built-in MCP profile `assistant` for it and points
`threadRoles.assistant` at it, in two saves from the person (the pick first, before the thread
exists). Off (the default): no such button; a
thread already named the assistant stays as it is. Only the person can change it. Anything but
`{"enabled": true}` in the file reads as off; a save with a bad value is refused. The thread
`threadRoles.assistant` names never gets more than the profile `assistant`, whatever its pick (a
managed policy that owns `mcpThreadProfiles` can drop the pick). More in
[assistant.md](assistant.md).

### `server.stopFinishedChildren`

Stops a finished delegated child's provider process (a Claude process holds about 150-300 MB) once
its thread has been idle for `graceSeconds` (default 60), instead of keeping it until upstream's own
idle release 30 minutes later. The stop is upstream's idle release, so the thread keeps its history
and the next message to it (a follow-up from its parent, `t3_thread_send`, a person) starts a new
process as any resumed thread does.

- When: the child's task ends (`delegate_task` here) or the child's run ends, then `graceSeconds`
  of quiet. A run that starts in that time cancels it; that run's end starts it again. Nothing
  polls: a child found busy is checked again at its next run end, request change or background-work
  change.
- Never stopped: a thread with a run (also a queued or silent one), a pending question or approval,
  background work, a task still open (its own, or one it delegated itself), or a `wait_for` wait
  that would wake it (or a wait store that cannot be read). The last check runs in the same step as upstream's release, which also keeps a
  session a turn just started on.
- Only a child's own process: one provider process per thread (Claude, and others that run one per
  thread). A shared process (Codex, OpenCode) serves other threads and is left to upstream. A
  provider's own subagent (not `delegate_task`) shares its parent's process and is never touched.
- A child a linked machine delegated here is stopped by this machine's setting when its run is over
  (the process runs here); the parent's machine stops nothing there.
- Timers live in memory. A restart ends every provider session anyway; after it, a child's next run
  end arms again. A second stop of the same child finds nothing live and does nothing.

Switch on: **Settings → Fork**, section Stop finished sub-agents, or in `settings.json`:
`"stopFinishedChildren": { "enabled": true, "graceSeconds": 60 }`. Anything but `"enabled": true`
reads as off, and a bad `graceSeconds` as 60; a save with a bad value is refused.

### `server.lanes`

Lanes under a project's orchestrator
([#1156](https://github.com/scuttletoby/t3code/issues/1156)): the orchestrator stays the one thread
the person talks to, and each lane (a long-lived child thread) owns one kind of work, such as the
pull request queue, with its own workers. So far: the settings, the lane store and the lane tools
below.

- `enabled` (off): no lane can be switched on, whatever is defined.
- `definitions`: changes to the lanes that exist. The built-in lanes `pr-queue` (owns `pr`) and
  `issues` (owns `issue`) exist without a word here. An entry by name adds a lane or replaces a
  built-in one whole; `false` removes a built-in lane. The orchestrator only switches a defined lane
  on and off; it cannot make one. A name is lower-case words joined by `-` (`pr-queue`), at most 40
  characters. Each lane has a `description` (what it does, the start of its thread's brief), `owns`
  (the kinds of item it may claim, `pr` and/or `issue`: at least one, each once) and `driver`
  (`agent`, the default and for now the only one).

Only the person can change it. A save changes `enabled` and each lane on its own: a definition sets
it, `false` removes a built-in lane, and `null` drops the entry (a built-in lane is back as built),
so two saves for different lanes keep both. A save with a bad name or definition, or `false` for a
lane that is not built in, is refused. The file is read leniently: a value that is not an object, or
a `definitions` that is not an object, reads as the built-in lanes; anything but `"enabled": true`
as off; and a bad name or definition (another driver included) drops alone. A record that names a
lane no longer defined keeps the name; whatever acts on it treats the lane as unknown.

An item a lane claims is written `pr:<owner>/<repo>#<n>` or `issue:<owner>/<repo>#<n>`, owner and
repository in lower case (GitHub ignores case, so one item has one reference); `.` and `..` are
never an owner or repository.

**The lane store** (part 2) keeps each project's lanes in `<stateDir>/lanes/<projectId>.json`,
written whole and atomically after each change, one change at a time, so it survives a restart:

- **Registry:** each lane that ran, `running` or `stopped`, and the thread that acts for it now
  with its **generation**. A start or a replacement (a new thread for a lane whose thread died or
  was switched out) takes the next generation; a number is never used twice, so a replaced thread
  can no longer act for the lane: its steps are refused. Repeating a start or a replacement that
  already happened changes nothing, so a step half-done can be run again.
- **Claims:** one owner per item (`pr:owner/repo#12`, `issue:owner/repo#3`; owner and repository
  in any case are one item, kept lower-case), and only an item of a kind the lane `owns`. A second
  lane's claim is refused while the first lane runs. The claims of a lane whose thread died stay
  its own and move to its next thread. A claim of a lane stopped, no longer defined, or no longer
  owning that kind of item is stale: the next claim on the item takes it. Stopping a lane releases
  its claims.
- **Outbox:** escalations for the main orchestrator, kept until delivered and keyed by lane and
  trimmed id: the same lane's id twice is kept once while it waits (once delivered, the id is free
  again), and at 200 waiting a new one is refused (never dropped).
- **Notes:** short reports for the board; each lane keeps its last 50.

While `enabled` is off every step is refused and no lane file is read or written: claims on disk
wait unchanged until lanes are on again. A lane file that exists but cannot be read (not JSON,
another version, a broken entry) counts as unknown, not empty: every step for that project is
refused and the file is kept until it reads again. A file that cannot be written (disk,
permissions) refuses the step as `store-unwritable` and changes nothing. No step writes a file a
later read would refuse: text that could not be read back (empty, over 2000 characters, an item
that is not one) is refused, and every new state is checked before it is written. Lane and item
names are looked up as the file's own keys only, so a name like `constructor` finds nothing. Run
records name the lane a run worked for (a lane's thread, or one of its workers) in `lane`; for a
stopped lane, only runs that ended by its stop.

**The lane tools** (part 3), in the T3 tool group `lanes`, are listed and callable only while
`enabled` is on (off: hidden from `tools/list` and `tool_search`, refused when called). Each acts on
the calling thread's project, and each repeats safely:

- `lane_start`: switches a defined lane on with a thread of the project acting for it (usually a
  child the orchestrator started with `delegate_task`; a thread acts for at most one running lane,
  and never for a lane it starts itself), and returns its generation. That thread passes the lane
  and generation to `lane_claim` and `lane_release`; a thread that is not the lane's current one
  is refused.
- `lane_stop`: switches a lane off and releases its claims. The thread that started the lane stops
  it (another lane's thread or worker is refused with `not-starter`); once that thread is no longer
  a live thread of the project (deleted, or moved to another project), or none is recorded, any
  thread of the project stops it, so a lane never outlives everyone who may stop it.
- `lane_board`: every defined lane (`off` when it never ran, `undefined` when settings no longer
  define it) with its thread and generation, the claims (stale ones marked), the escalations
  waiting and each lane's last 3 notes, and who started each lane (`startedBy`). Settings that
  cannot be read: refused with `settings-unreadable` (not `lanes-off`), not a board of `undefined`
  lanes.

Rollback: the lane file keeps its version 1 with `startedBy` as an optional key. A server from
before it decodes the file and drops the key on its next write; that lane then has no starter
recorded, so any thread of the project may stop it (as before the key existed). Nothing is lost
or locked.

- `lane_claim`, `lane_release`: from the lane's thread, one owner per item, as the store above.

```json
{
  "lanes": {
    "enabled": true,
    "definitions": {
      "issues": false,
      "ops": { "description": "Keeps the machines and CI healthy.", "owns": ["issue"] }
    }
  }
}
```

### `projectOverride.waitChecks`

A project's own list of wait checks. It replaces the server's list for that project as a whole (a
project lists every check it uses). Not set: the project uses the server's checks.

Set: **Settings → Fork**, choose the project at the top, then **Custom wait checks**.

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

### `projectOverride.placementNeeds`

A project's own needs, for example `["gpu"]`. It replaces the server's `placementNeeds` for that
project as a whole; `[]` says the project needs nothing. Not set: the server's. A project value that
is not valid fails closed as the server's does: that project's work goes to no machine (never the
server's needs, never none), and its text is kept. A project save that leaves this key out (an older
client) keeps the stored value, and one that sends back the kept text unchanged keeps it too (other
text is refused); only `null` clears it, as for `playtest`.

### `client.calmSidebarEnabled`

Turns on the calm sidebar on this device: the threads in `server.threadRoles` go to the top, and the
other top-level threads fold into one closed group with a count of those waiting for you
([#139](https://github.com/scuttletoby/t3code/issues/139)). Off (the default): upstream's sidebar.
It is a client setting, so each device chooses: calm on a phone, the full list on a desktop. A
client settings file without it reads as off.

### `client.ownerQuestionsPage`

On (the default, the owner's decision in [#828](https://github.com/scuttletoby/t3code/issues/828)):
the Issues entry opens **Waiting on you**, the open owner questions of every project on every
connected server, instead of upstream's Issues page. Off: upstream's page. It changes which page the
Issues entry opens, nothing else; upstream's page files stay as they are (one seam in
`apps/web/src/routes/_chat.issues.tsx`). A client setting, so each device chooses; a client settings
file without it reads as on. Settings → Fork → Issues page.

### `client.workPanelEnabled`

Turns on the work panel on this device
([#1136](https://github.com/scuttletoby/t3code/issues/1136)): in a thread's side panel, right
after its sub-agents, a CI part. It has one queue line per repository (workflow runs queued and
running, and the oldest queued run's age and link; a re-run is dated by its own start), then each
pull request linked to the thread or a thread under it: queued, running, failed and passed runs on
its head (its watched commit, else the runs GitHub ties to it or its branch in its own repository),
the oldest queued wait and a link to the run. These numbers come from one shared read per server
(`fork.workPanel`, `apps/server/src/fork/workPanel`), kept only in memory. The server asks GitHub
(with its `gh` login) at most once a minute in all, for every viewer and every repository, counted
from each call's start; a viewer who leaves mid-call does not free the minute. The minute goes to
the read that has waited longest among every repository any viewer asked for in the last five
minutes, so two panels on different repositories share it. A call that takes over 45 seconds counts
as no answer. Each repository is read three ways in turn: its latest 100 runs, and every queued and
every running run, which also finds a re-run of an older run (GitHub's `queued` list leaves out a
re-run that is `waiting` for an approval, `requested` or `pending`; outside the latest page, those
are missed). So one panel's three repositories (those of the thread and its
sub-agents with the most pull requests) are each read fully every nine minutes; with more
repositories in view at once, a line goes unknown for part of each cycle. It waits for the budget's
reset when less than a fifth of the rate limit is left or GitHub refuses a read for it, and 2, 4, 8
and then 15 minutes when GitHub does not answer. Counts older than 10 minutes are not shown. A pull
request the read has no runs for falls back to what the pull request watch saw (failed and passed
checks, and whether checks are pending), then to its last sync. A part with no answer says unknown
and why, never 0: also a machine that answered before and then fails, or whose last answer arrived
more than three minutes ago; the ages count on from when the answer arrived. Only github.com repositories are read. Off (the
default): upstream's side panel. A client settings file without it reads as off.

### A setting the fork removed

An early draft had a client setting for one "anchor machine" for new work. It was removed before it
shipped: upstream's load balancing does the same. Settings saved with it still load. See
[anchor-machine.md](anchor-machine.md).

## Machine facts on `t3_environment_read`

Placement design row P3 ("facts out"). `t3_environment_read` returns two optional fields beside
upstream's, for this machine and for a linked one (the read is forwarded there as before):

- `hostResources`: CPU load, cores and memory, the snapshot the app's load balancing already
  samples. This is the result field of upstream PR #34293, which was closed upstream without
  merging; the fork carries the field (same name and schema) from its own file,
  `apps/server/src/fork/environmentFacts.ts`. Not carried as it was: its export in
  `McpHttpServer.ts`, its test lines in upstream's test files and its new tool description, since
  the fork adds no lines to those files (its tests are in fork test files instead), and the tool's
  dependency on the sampler (the fork's file finds it, and the slot gate, without one, so no
  upstream test needs a new mock).
- `placement`: `labels` (that machine's `placement.labels`, then each line its `factsCommand`
  printed, trimmed, lower-cased and without repeats), `available` and `reason`, and, from the
  agent slot gate's own status read (the one `wait_list` shows):
  `runningAgents` (running, starting or holding a slot), `slotsFree` (`null`: no machine limit),
  `memoryFloorMb` (`agentSlots.minFreeMemoryMb`, `null`: none) and `freeMemoryMb`. `factsAt` is
  when they were read. `available` is `false` when every machine slot is in use or free memory is
  below the floor, or when the machine's own word is no: its `factsCommand` said not available
  (row P4; a non-zero exit, no answer within 5 seconds, or no process runner on that server), or
  its `placement` setting is not valid there (then `labels` is empty too). Each "no" keeps its
  reason, the slot gate's first; the counts stay. With `agentSlots` off it is `true`, with a
  reason and no counts: the machine sets no limit and does not count its agents. With no
  `factsCommand` set, only the slots decide.

When the slot gate cannot be read (its count read fails or takes longer than its own limit, the
gate is not running, or the settings cannot be read), `placement` says `available: false` with a
reason and gives no count, so an agent never sees a guessed "slots free". A linked machine on an
older T3 leaves both fields out, which means "no facts", never a failed read. The host sample has its
own limit (2 seconds), and it and the slot count are read at the same time. With agent slots off, a
slow sampler leaves out `hostResources` without delaying `placement`. With them on, the gate's own
count reads the same sample for its memory floor, so a hung sampler also gives `placement` "not
available" (the gate could not count) after 6 seconds, the same answer the gate gives at launch. Facts are never stored: each read measures again, except a `factsCommand` answer, which is kept
in memory for 15 seconds (a restart forgets it, and a run every reader stopped waiting for is
stopped and kept by none). `factsAt` is the time of the read, so a cached `factsCommand` answer
in it can be up to about 15 seconds older. A slow `factsCommand` delays the read by up to its 5
seconds, or about 6 for a program that ignores the stop (it is killed one second after it); it
runs beside the slot and host reads. Its answer is read when its output closes, so a program that
leaves a background child holding the output reads "did not answer within 5 seconds".
The gate's status read does its own clean-up first (it drops used-up slot holds of waits, the same
step `wait_list` runs); the facts read gives that status read at most 6 seconds (the gate's own
count limit plus one), so a read can take that long on a busy machine, and it changes nothing but
those spent holds. `available` and `slotsFree` count the
machine limit and the memory floor only: the per-account limits of `agentSlots` are not in them, so
a machine with free slots can still refuse a task for one account; the later placement rows check
the account at launch (the gate does). The fields are part of the tool's result, not of the tool
list an agent loads, so the tool context budget is unchanged.

When placement reads a linked machine's answer (`factsFromAnswer` in the same file), it decodes it
with these schemas: no answer (the machine did not answer) or neither field is "no facts", as
above; an answer they refuse is "facts not
valid", and the machine is refused, even when it is named. This machine with no CPU and memory
sample (no sampler, or one that cannot measure CPU) is still a place for work when every other
rule passes, ranked last; a linked machine without them is not. Its weight still counts (`manual`
or an unknown word keeps it out), and so does upstream's 5 % free-memory limit when memory was
measured. With agent slots on, its gate checks again at launch; with them off, nothing checks,
as in upstream's own launch on this machine.

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

- [about.md](about.md): what the fork is, how it follows upstream, every feature in one line
  and what is planned.
- [go-live.md](go-live.md): the runbook for moving a boot-service machine to the fork, the way back,
  and switching features on one at a time.
- [updates.md](updates.md): update defaults, how a test pins them, the nightly sync runbook,
  `serve --hold-work`.
- [anchor-machine.md](anchor-machine.md): one machine for new work, with upstream load balancing.
- [machine-move.md](machine-move.md): runbook template for moving an agent machine to new hardware
  (inventory, set-up, caches, the switch).
- [gate-trust.md](gate-trust.md): what the head gate proves, and the owner's steps.
- [host-tools.md](host-tools.md): design for agents on a person's own computer (no code yet).
- [playtest.md](playtest.md): the playtest adapter contract (steps, environment, exit codes).
- [handoff.md](handoff.md): handoff instead of compaction (when, the quiet rule, the message).
- [assistant.md](assistant.md): the assistant thread (setup, its profile, what it cannot do yet).
- [run-records.md](run-records.md): one record per agent run (usage, time, labels) and the weekly
  summary.
- [delegate-prepare.md](delegate-prepare.md): set up a checkout (merge, install, baseline tests)
  by script before a delegated agent starts.
- [`.github/fork-fleet.md`](../../.github/fork-fleet.md): the fleet runners and `CI_RUNNERS`.
- [`fork-ci/README.md`](../../fork-ci/README.md) and [`fork-kit/README.md`](../../fork-kit/README.md):
  CI actions and host kits.
