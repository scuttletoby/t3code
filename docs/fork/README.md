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
`t3 __fork-report --base-dir <home>` prints it as JSON.

## Settings

The defaults, "off" and "decided in" below come from one table in the code,
[`packages/contracts/src/forkDefaults.ts`](../../packages/contracts/src/forkDefaults.ts). A test
(`packages/shared/src/forkDocs.test.ts`) fails when this table and that one differ, or a setting has
no section below. "Off" means the default leaves the machine as upstream's would be.

| Setting                                                                      | Default             | Off | Decided in                                             |
| ---------------------------------------------------------------------------- | ------------------- | --- | ------------------------------------------------------ |
| [`server.automaticUpdates`](#serverautomaticupdates)                         | `false`             | yes | [#41](https://github.com/scuttletoby/t3code/issues/41) |
| [`server.automaticUpdatesQuietCommand`](#serverautomaticupdatesquietcommand) | `""`                | yes | [#5](https://github.com/scuttletoby/t3code/issues/5)   |
| [`server.usagePacing`](#serverusagepacing)                                   | `{}`                | yes | [#52](https://github.com/scuttletoby/t3code/issues/52) |
| [`server.usagePacingGate`](#serverusagepacinggate)                           | `"off"`             | yes | [#73](https://github.com/scuttletoby/t3code/issues/73) |
| [`server.usagePacingLowerEffort`](#serverusagepacinglowereffort)             | `{}`                | yes | [#73](https://github.com/scuttletoby/t3code/issues/73) |
| [`server.waitFor`](#serverwaitfor)                                           | `{"enabled":false}` | yes | [#84](https://github.com/scuttletoby/t3code/issues/84) |
| [`server.waitChecks`](#serverwaitchecks)                                     | `{}`                | yes | [#84](https://github.com/scuttletoby/t3code/issues/84) |
| [`server.agentSlots`](#serveragentslots)                                     | `{"enabled":false}` | yes | [#84](https://github.com/scuttletoby/t3code/issues/84) |
| [`server.headlessUsageRefreshInterval`](#serverheadlessusagerefreshinterval) | `600000`            | no  | [#42](https://github.com/scuttletoby/t3code/issues/42) |
| [`projectOverride.waitChecks`](#projectoverridewaitchecks)                   | not set             | yes | [#84](https://github.com/scuttletoby/t3code/issues/84) |

For `waitFor` and `agentSlots` only the `enabled` switch is pinned; their other fields are tuning
that does nothing while the switch is off.

## Each setting

### `server.automaticUpdates`

A server installed as a boot service (`t3 service install`) updates itself to the newest fork release
once no thread is working. Off: it updates only when someone runs `t3 update` or presses **Update**.
Upstream has a setting of the same name that defaults on; the fork keeps `false` through every sync.

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

Switch on: **Settings → Waits**, "Let agents wait for events", or in `settings.json`:
`"waitFor": { "enabled": true }`.

### `server.waitChecks`

Named commands an agent can wait on by name (`wait_for` with a check). Each runs as a program with
fixed arguments, no shell; exit 0 means the event happened. It runs unsandboxed in the project folder
with the server's environment, so use fixed programs that run no files from the repository (no test
runners or build scripts). Only the person can add one; agents cannot.

Add one: **Settings → Wait checks → Add check**, or in `settings.json`:

```json
{ "waitChecks": { "build-done": { "argv": ["test", "-f", "/tmp/build.done"] } } }
```

Used only while `waitFor` is on.

### `server.agentSlots`

Limits on running agents: `delegate_task` is refused above `maxPerMachine` agents on this server,
above `maxPerAccount` per provider account, or below `minFreeMemoryMb` of free memory. Threads
started in other ways are counted but never refused. With `waitFor` on, an agent can wait for a slot
instead of retrying.

Switch on: **Settings → Agent slots**, "Limit running agents", or in `settings.json`:
`"agentSlots": { "enabled": true, "maxPerMachine": 4 }`. With `enabled` and no limits set, nothing is
refused.

### `server.headlessUsageRefreshInterval`

How often the server reads usage limits while no app is open, in milliseconds. On by default every
10 minutes, so pacing has current numbers on a machine nobody is looking at. Read only: it changes no
thread, setting or install. Never more often than the provider status refresh.

Switch off in `settings.json`: `"headlessUsageRefreshInterval": 0`.

### `projectOverride.waitChecks`

A project's own list of wait checks. It replaces the server's list for that project as a whole (a
project lists every check it uses). Not set: the project uses the server's checks.

Set: **Settings**, choose the project at the top, then **Wait checks**.

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

A server run this way is not updated by `t3 update` or a boot service; update it with `git pull` and
a rebuild.

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
- [`.github/fork-fleet.md`](../../.github/fork-fleet.md): the fleet runners and `CI_RUNNERS`.
- [`fork-ci/README.md`](../../fork-ci/README.md) and [`fork-kit/README.md`](../../fork-kit/README.md):
  CI actions and host kits.
