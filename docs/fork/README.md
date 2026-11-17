# The fork: settings and install

Fork only. This fork is T3 Code with a thin set of changes on top; every change is listed in
[FORK_CHANGES.md](../../FORK_CHANGES.md). This page lists every fork setting: what it does, its
default, and how to switch it on. Then it says how to install the fork today.

The rule: a machine whose settings never name a fork setting behaves as upstream's T3 Code. Every
fork switch is off by default, except one read-only refresh of usage limits.

## Where settings live

Server settings are in `settings.json` in the server's `userdata` folder (by default
`~/.t3/userdata/settings.json`). The server reads the file again when it changes; no restart is
needed. Some fork settings also have a row in the app's **Settings** pages (named below). A
project's overrides are set in **Settings** with that project chosen at the top ("Applying settings
for …", see [project settings](../user/project-settings.md)). An agent's tools cannot change any
fork setting.

To check what a T3 home has switched on without changing anything, the fork's hidden command
`t3 __fork-report --base-dir <home>` prints it as JSON. Not covered: run it while T3 is running or
stopped, not while it is stopping. A clean stop at the instant the report opens the database leaves
an empty `statev2.sqlite-wal` (harmless; T3 reuses it) and reports the database as unreadable; run it
again.

## Settings

The defaults, "off" and "decided in" below come from one table in the code,
[`packages/contracts/src/forkDefaults.ts`](../../packages/contracts/src/forkDefaults.ts). A test
(`packages/shared/src/forkDocs.test.ts`) fails when this table and that one differ, or a setting has
no section below. "Off" means the default leaves the machine as upstream's would be.

| Setting                                                                      | Default             | Off | Decided in                                               |
| ---------------------------------------------------------------------------- | ------------------- | --- | -------------------------------------------------------- |
| [`server.automaticUpdates`](#serverautomaticupdates)                         | `false`             | yes | [#41](https://github.com/scuttletoby/t3code/issues/41)   |
| [`server.automaticUpdatesQuietCommand`](#serverautomaticupdatesquietcommand) | `""`                | yes | [#5](https://github.com/scuttletoby/t3code/issues/5)     |
| [`server.usagePacing`](#serverusagepacing)                                   | `{}`                | yes | [#52](https://github.com/scuttletoby/t3code/issues/52)   |
| [`server.usagePacingGate`](#serverusagepacinggate)                           | `"off"`             | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)   |
| [`server.usagePacingLowerEffort`](#serverusagepacinglowereffort)             | `{}`                | yes | [#73](https://github.com/scuttletoby/t3code/issues/73)   |
| [`server.waitFor`](#serverwaitfor)                                           | `{"enabled":false}` | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.waitChecks`](#serverwaitchecks)                                     | `{}`                | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.playtest`](#serverplaytest)                                         | `{"enabled":false}` | yes | [#260](https://github.com/scuttletoby/t3code/issues/260) |
| [`server.agentSlots`](#serveragentslots)                                     | `{"enabled":false}` | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`server.headlessUsageRefreshInterval`](#serverheadlessusagerefreshinterval) | `600000`            | no  | [#42](https://github.com/scuttletoby/t3code/issues/42)   |
| [`server.mcpProfiles`](#servermcpprofiles)                                   | `{}`                | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpProfile`](#servermcpprofile)                                     | `"all"`             | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpSubagentProfile`](#servermcpsubagentprofile)                     | `"inherit"`         | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`server.mcpThreadProfiles`](#servermcpthreadprofiles)                       | `{}`                | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`projectOverride.waitChecks`](#projectoverridewaitchecks)                   | not set             | yes | [#84](https://github.com/scuttletoby/t3code/issues/84)   |
| [`projectOverride.playtest`](#projectoverrideplaytest)                       | not set             | yes | [#260](https://github.com/scuttletoby/t3code/issues/260) |
| [`projectOverride.mcpProfile`](#projectoverridemcpprofile)                   | not set             | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |
| [`projectOverride.mcpSubagentProfile`](#projectoverridemcpsubagentprofile)   | not set             | yes | [#145](https://github.com/scuttletoby/t3code/issues/145) |

For `waitFor`, `agentSlots` and `playtest` only the `enabled` switch is pinned; their other fields
are tuning that does nothing while the switch is off.

## Each setting

### `server.automaticUpdates`

A server installed as a boot service (`t3 service install`) updates itself to the newest fork release
once no thread is working. Off: it updates only when someone runs `t3 update` or presses **Update**.
Upstream has a setting of this name pending (not on its `main` yet); the fork keeps `false` through every sync.

Switch on in `settings.json`: `"automaticUpdates": true`. There is no Settings row for it yet. An
admin policy with updates `"off"` still wins. Details: [updates.md](updates.md).

### `server.automaticUpdatesQuietCommand`

An optional program asked before each automatic update; a non-zero exit means "not now". Used only
while `automaticUpdates` is on. Empty: no program is asked.

Set in `settings.json`: `"automaticUpdatesQuietCommand": "/path/to/quiet-check"`. Details:
[updates.md](updates.md).

### `server.usagePacing`

Per provider instance (account): `reservePercent` keeps that many points of each limit window free
for the person, released over the window's last 12 hours; `burnFirst` spends this account first;
`noLimits` says the account has no usage limits (an API key). An account without an entry has no
reserve. Only the pacing gate (`usagePacingGate`) acts on it.

Set in `settings.json`:

```json
{ "usagePacing": { "<instance id>": { "reservePercent": 20 } } }
```

The key is the provider instance id, for example `codex` or `codex_work`. There is no Settings row for it yet.

### `server.usagePacingGate`

What happens when an agent starts a task with `delegate_task`. `"off"`: nothing, as upstream.
`"advise"`: the task starts as asked and the reply carries a note about the pace. `"enforce"`: a task
on an account that is ahead of its pace moves to another account of the same provider, runs at a
lower effort (`usagePacingLowerEffort`), or is refused for now and the agent asks again later.

Switch on in `settings.json`: `"usagePacingGate": "advise"` (try `advise` before `enforce`). There
is no Settings row for it yet.

### `server.usagePacingLowerEffort`

For `"enforce"`: what each effort level turns into while an account is ahead of pace and no other
account has room, for example `{ "high": "medium", "medium": "medium" }`. A task at a level not
listed is held instead. Empty: no effort is ever lowered.

Set in `settings.json`: `"usagePacingLowerEffort": { "high": "medium" }`.

### `server.waitFor`

Waits: an agent calls `wait_for` to wait for a pull request (checks done or merged), a file (there or
gone), a system unit, a named check command (`waitChecks`) or a free agent slot, and its thread is
woken with the result. `wait_list` and `wait_cancel` list and end them. While off, waits are neither
judged nor delivered; listing and cancelling still work. Other fields set deadlines, limits per
thread and how often each kind is checked.

Switch on: **Settings → Threads**, section Waits (shown while a server is connected), "Let agents wait for events", or in `settings.json`:
`"waitFor": { "enabled": true }`.

### `server.waitChecks`

Named commands an agent can wait on by name (`wait_for` with a check). Each runs as a program with
fixed arguments, no shell; exit 0 means the event happened. It runs unsandboxed in the project folder
with the server's environment, so use fixed programs that run no files from the repository (no test
runners or build scripts). Only the person can add one; agents cannot.

Add one: **Settings → Threads**, section Wait checks, **Add check**, or in `settings.json`:

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

### `server.agentSlots`

Limits on running agents: `delegate_task` is refused above `maxPerMachine` agents on this server,
above `maxPerAccount` per provider account, or below `minFreeMemoryMb` of free memory. Threads
started in other ways are counted but never refused. With `waitFor` on, an agent can wait for a slot
instead of retrying.

Switch on: **Settings → Threads**, section Agent slots, "Limit running agents", or in `settings.json`:
`"agentSlots": { "enabled": true, "maxPerMachine": 4 }`. With `enabled` and no limits set, nothing is
refused.

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
(a save may send `null` under a built-in name to remove it) or renamed. Nothing applies a profile
yet: the provider adapters and the T3 tool filter come in later pull requests.

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
its parent's profile. A child never gets more than its parent.

### `server.mcpThreadProfiles`

The person's pick of profile per thread, by thread id. A pick wins over every default. At most 500
picks are kept: a save that would add more is refused until old picks are removed. No pick is ever
dropped to make room.

### `projectOverride.waitChecks`

A project's own list of wait checks. It replaces the server's list for that project as a whole (a
project lists every check it uses). Not set: the project uses the server's checks.

Set: **Settings**, choose the project at the top, then **Wait checks**.

### `projectOverride.playtest`

A project's own playtest adapter. It replaces the server's `playtest` for that project as a whole:
a project sets every step it uses. Not set: the project uses the server's. A project value that is
not valid reads as off for that project; it never falls back to the server's adapter. A project save
that leaves this key out (an older client) keeps the stored value; only `null` clears it, and the
fork's `clearProjectSettingsOverridesWithForkKeys` sends that. Unlike the server's value it is
saved with every field written out, so a project that is off says so in the file. A repository's `t3.json` cannot set
it: that file is in the repository, which agents edit.

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
([fork-kit/golive](../../fork-kit/golive/README.md)) is for this: today it has only its read-only
`check`. Its copy, rehearsal on the copy, switch and way back are not written yet, and the kits are
not yet installable from the release branch. Until then, try a build on a copy with
`t3 serve --hold-work --base-dir <copy>` ([updates.md](updates.md#trying-a-build-on-a-copy-serve---hold-work)).

Not there yet: a fork installer URL, a macOS CLI archive, signed desktop apps, and the go-live
switch.

## Other fork pages

- [updates.md](updates.md): update defaults, how a test pins them, the nightly sync runbook,
  `serve --hold-work`.
- [anchor-machine.md](anchor-machine.md): one machine for new work, with upstream load balancing.
- [gate-trust.md](gate-trust.md): what the head gate proves, and the owner's steps.
- [host-tools.md](host-tools.md): design for agents on a person's own computer (no code yet).
- [playtest.md](playtest.md): the playtest adapter contract (steps, environment, exit codes).
- [`.github/fork-fleet.md`](../../.github/fork-fleet.md): the fleet runners and `CI_RUNNERS`.
- [`fork-ci/README.md`](../../fork-ci/README.md) and [`fork-kit/README.md`](../../fork-kit/README.md):
  CI actions and host kits.
