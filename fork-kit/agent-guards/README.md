# agent-guards: Claude Code hooks that guard every agent on a machine

Five Claude Code hooks. Each one stops one mistake that agents made on a shared machine, at the moment they make it.
A line in the agents' instructions did not stop any of them. The hooks are general: a project gives only its facts
(file paths, a command name, a budget) in the machine file's `"agent-guards"` section.

Linux, Python 3 standard library only. One file per hook in [bin/](bin/), the shared parts in
[lib/agent_guards/](lib/agent_guards/).

## The hooks

| Hook                    | Event (matcher)                                                    | Refuses                                                                                                                                                                                                                                         | If the hook is missing or fails                                                            |
| ----------------------- | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| `actions-disable-guard` | PreToolUse (`Bash`), 10 s                                          | Disabling a GitHub Actions workflow or Actions itself, or deleting or weakening branch protection or rulesets, on any repo (`gh workflow disable`, `gh api .../actions/workflows/<id>/disable`, ...). Also a listed script run with its listed flag (`refuseScripts`). | **Refuses** (exit 2): every Bash command is refused until it is back, with a line saying so |
| `capacity-pipe-guard`   | PreToolUse (`Bash\|Monitor`), 10 s                                 | A pipe out of a capacity check (`t3-capacity 2048 \| tail -1 && heavy-step`): the pipe hides the check's NOT OK exit                                                                                                                             | Passes (exit 1, "hook skipped, commands are NOT checked")                                  |
| `memory-index-guard`    | PreToolUse (`Write\|Edit\|MultiEdit`) and PostToolUse (`Write\|Edit\|MultiEdit\|Bash`), 5 s | An auto-memory index (`<project>/memory/MEMORY.md`) over its budget: a write that would make it too big (Pre), or one that did, through any tool (Post, once per change)                                                                         | Passes (exit 1, with a line)                                                               |
| `bg-guard`              | Stop, 5 s                                                          | Ending a turn while a Bash command the agent started in the background still runs (the end of it may never wake the agent). At most `maxBlocks` times per session                                                                                | Passes (exit 1, with a line)                                                               |
| `agent-slots`           | UserPromptSubmit and Stop, 5 s                                     | Only for the orchestrator sessions listed in `sessionsFile`: adds "agents alive, slots free, ready queue" as context; blocks a stop once while slots are free and the ready queue has items                                                       | Passes (exit 1, with a line)                                                               |

How each hook answers is what Claude Code reads: PreToolUse refuses with exit 2 and the reason on stderr. PostToolUse
and Stop block with `{"decision": "block", "reason": ...}` on stdout. UserPromptSubmit adds `additionalContext`.
Exit 0 with nothing on stdout means pass. The messages and exit codes are the same as these hooks had before they
moved into this kit, so an agent sees no change. Each hook's file says what it reads and what it does not, and where
its tests are.

**Why one fails closed and the others open.** `actions-disable-guard` guards against a step that cannot be undone
by an agent (Actions turned off on a repo). If it is gone or broken, refusing every Bash command is the safe answer,
and the message says so. The other four guard mistakes. Refusing every call because a helper is gone would stop all
work on the machine. The settings entry does both (`agent-guards hooks` prints it):

```sh
# closed
g=/usr/local/bin/actions-disable-guard; [ -r "$g" ] || { echo "actions-disable-guard: $g is missing: every Bash command is refused until it is back" >&2; exit 2; }; /usr/bin/python3 -IB "$g" || { echo "actions-disable-guard: the guard failed (exit $?): the command is refused" >&2; exit 2; }
# open
g=/usr/local/bin/capacity-pipe-guard; [ -r "$g" ] || { echo "capacity-pipe-guard: $g is missing: hook skipped, commands are NOT checked" >&2; exit 1; }; exec /usr/bin/python3 -IB "$g"
```

A hook that runs past its timeout is ended by Claude Code, and the call goes on (no hook here sets `onFailure`).
`actions-disable-guard` answers by itself within 5 s of its start (it refuses when it runs out of time with a
guarded word unread), well inside its 10 s.

## Settings (the machine file)

Every key is optional. With no machine file, or no `"agent-guards"` section, each hook uses the defaults below.
These are the values the hooks had before. The file is `/etc/fork-kit/machine.json`
([common/machine_file.py](../common/machine_file.py): owned by root, not writable by others, so an agent cannot
change a guard's facts). A section that is broken (a wrong type, an unknown key, a bad path) is not used: the hook
uses its defaults and writes one line on stderr. `agent-guards self-check` names the problem. A broken section
drops the extra `refuseScripts` rules too, so fix one at once.

| Key                             | Default                                 | What it is                                                                                                                            |
| ------------------------------- | --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `actionsDisable.stateDir`       | `~/.local/state/actions-disable-guard`  | The refusal log (`log`) and a parse cache                                                                                             |
| `actionsDisable.refuseScripts`  | `[]`                                    | `{"name": "garden.py", "flag": "--fix", "why": "..."}`: also refuse running that script with that flag (a project's own tool that disables workflows) |
| `capacityPipe.commands`         | `["t3-capacity"]`                       | The capacity checks: commands that answer OK (exit 0) or NOT OK (exit 1)                                                              |
| `memoryIndex.maxChars`          | `6000`                                  | Characters in the whole index at most                                                                                                  |
| `memoryIndex.maxLine`           | `160`                                   | Characters in one line at most                                                                                                         |
| `memoryIndex.stateDir`          | `~/.cache/memory-index-guard`           | One file per session: the index as last seen                                                                                          |
| `bgGuard.skipSessionsFiles`     | `[]`                                    | Files of session ids (one per line, `#` comments) that are never blocked, such as orchestrators that are woken another way           |
| `bgGuard.maxBlocks`             | `3`                                     | Blocks per session at most, so a background job left on purpose never loops                                                          |
| `bgGuard.stateDir`              | `~/.local/state/bg-guard`               | One block count per session (raised under a lock)                                                                                     |
| `agentSlots.sessionsFile`       | `""` (the hook does nothing)            | The orchestrator session ids it acts for                                                                                              |
| `agentSlots.queue`              | `""`                                    | A markdown file; the ready list is the `- ` lines under `readyHeading`                                                                |
| `agentSlots.readyHeading`       | `"## Ready"`                            |                                                                                                                                       |
| `agentSlots.maxAlive`           | `10`                                    | Agent processes at most                                                                                                               |
| `agentSlots.processName`        | `"claude"`                              | The process name that is counted                                                                                                      |
| `agentSlots.note`               | `""`                                    | Extra text for the context line (a build-agent limit, say)                                                                            |

Paths are absolute or start with `~/` (the home of the user the hook runs as). An example is in
[machine.example.json](../common/machine.example.json).

**State and logs.** Each hook keeps going when its state folder or log cannot be written (a full disk, a folder
that is not writable): the answer never depends on it. `memory-index-guard` writes its state whole (a temporary
file, then a rename). `bg-guard` counts under a lock, so two Stop hooks at once count two blocks.
`actions-disable-guard` appends one line per refusal. `agent-slots` writes nothing.

## Install

As root, from a release commit of the fork (the shared [bootstrap](../common/bootstrap.sh) checks that it is on the
protected `ci-release` branch first):

```sh
sh fork-kit-bootstrap.sh --kit agent-guards --repo OWNER/FORK --sha RELEASE_SHA [--settings | --no-settings]
```

The installer ([sbin/agent-guards-install](sbin/agent-guards-install), on the shared
[kit_install.py](../common/kit_install.py)) copies the release to `/usr/local/libexec/agent-guards/versions/<sha>/`,
self-tests the copy, and links `/usr/local/bin/<hook>` and `/usr/local/bin/agent-guards` to it. It is safe to run
again and to stop at any step.

`--settings` also writes the hooks' entries as a Claude Code **managed settings drop-in**,
`/etc/claude-code/managed-settings.d/agent-guards.json`. Every Claude Code session of every user on the machine
reads it, and only root can change it. It is written after the links, so every hook it names is there. Sessions
already running keep the hooks they started with. `--no-settings` removes it; the choice is kept, so a run with
neither keeps the last one (a first install: no drop-in). The installer refuses to overwrite a drop-in of that name
it did not write. Uninstall removes the drop-in first, then the hooks.

Without `--settings`, put the entries in a settings file by hand: `agent-guards hooks` prints the `"hooks"` object.
A hook that is in both the drop-in and a user's settings runs twice. That is harmless for the refusing hooks, but
`bg-guard` then counts two blocks per stop, so remove the old entries soon after.

## Check

```sh
agent-guards self-check                      # the machine file section, the hook files, the drop-in
agent-guards check                           # run every hook wired in the drop-in: one case it must refuse, one it must pass
agent-guards check --settings ~/.claude/settings.json      # the same for hooks wired in any settings file
agent-guards-install self-check              # the installed files are the ones the installer wrote
```

`check` runs each wired command the way Claude Code does (`/bin/sh -c`, the event on stdin) and prints PASS or FAIL
for each case, and MISSING for a hook that is not wired. It changes nothing on the machine: the memory index and the
sessions are made up in a temporary folder, and their state files are removed afterwards. For `bg-guard` it starts a
`sleep` in the background under a stand-in parent named `claude`.

## General vs project

Everything in this kit is general. What a project or a machine adds is only its facts in the machine file: the
capacity command's name, the memory budget, the orchestrator's session file and queue, and a script of its own that
must not run with a given flag. Nothing in the hooks names a project, a person or a path of one machine.

## Tests

`tests/*.test.py`, run by the fork's CI (`.github/fork-ci-tests.txt`), with no root, no network and no installed
kit. They cover each hook's refuse and pass cases, and what happens when state is missing or old. That means: a
machine file that is missing, empty, garbled or not root-owned; a log or state folder that is missing or not
writable; two hooks at once; very long commands; and commands that only mention a guarded word (echo, a heredoc
into a file, a commit message). The settings entries are run as `/bin/sh -c` with the hook missing and with the
hook failing.
