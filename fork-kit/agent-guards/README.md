# agent-guards: Claude Code hooks that guard every agent on a machine

Six Claude Code hooks and a lookup command. Each hook stops one mistake that agents made on a shared machine, at
the moment they make it. A line in the agents' instructions did not stop any of them. The hooks are general: a project gives only its facts
(file paths, a command name, a budget) in the machine file's `"agent-guards"` section.

Linux, Python 3 standard library only. One file per hook in [bin/](bin/), the shared parts in
[lib/agent_guards/](lib/agent_guards/).

## The hooks

| Hook                    | Event (matcher)                                                                                      | Refuses                                                                                                                                                                                                                                                                                                                                                                                                                                                                                      | If the hook is missing or fails                                                                         |
| ----------------------- | ---------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------- |
| `actions-disable-guard` | PreToolUse (`Bash\|Monitor`), 10 s, `onFailure: block`                                               | Disabling a GitHub Actions workflow or Actions itself, or deleting or weakening branch protection or rulesets (`enforcement` set to `disabled` or `evaluate`), on any repo (`gh workflow disable`, `gh api .../actions/workflows/<id>/disable`, ...). Also a listed script run with its listed flag (`refuseScripts`), and starting a nested agent session that runs without this machine's hooks (`claude --bare`, `--safe-mode`, `CLAUDE_CODE_SIMPLE`, `codex`, by name, path or package). | **Refuses** (exit 2): every Bash and Monitor command is refused until it is back, with a line saying so |
| `capacity-pipe-guard`   | PreToolUse (`Bash\|Monitor`), 10 s                                                                   | A pipe out of a capacity check (`t3-capacity 2048 \| tail -1 && heavy-step`): the pipe hides the check's NOT OK exit                                                                                                                                                                                                                                                                                                                                                                         | Passes (exit 1, "hook skipped, commands are NOT checked")                                               |
| `memory-index-guard`    | PreToolUse (`Write\|Edit\|MultiEdit`) and PostToolUse (`Write\|Edit\|MultiEdit\|Bash\|Monitor`), 5 s | An auto-memory index (`<project>/memory/MEMORY.md`) over its budget: a write that would make it too big (Pre), or one that did, through any tool (Post, once per change)                                                                                                                                                                                                                                                                                                                     | Passes (exit 1, with a line)                                                                            |
| `bg-guard`              | Stop, 5 s                                                                                            | Ending a turn while a Bash command the agent started in the background still runs (the end of it may never wake the agent). At most `maxBlocks` times per session                                                                                                                                                                                                                                                                                                                            | Passes (exit 1, with a line)                                                                            |
| `agent-slots`           | UserPromptSubmit and Stop, 5 s                                                                       | Only for the orchestrator sessions listed in `sessionsFile`: adds "agents alive, slots free, ready queue" as context; blocks a stop once while slots are free and the ready queue has items                                                                                                                                                                                                                                                                                                  | Passes (exit 1, with a line)                                                                            |
| `facts-hook`            | SessionStart and PreCompact, 5 s                                                                     | Nothing; it adds context: at session start the things in the state tables for the session and "check the state table before diagnosing or redoing something" (`facts <words>`); before compaction, keep the things' names, not their rows. See [FACTS.md](FACTS.md)                                                                                                                                                                                                                          | Passes (exit 1, "the state tables are NOT shown")                                                       |

How each hook answers is what Claude Code reads: PreToolUse refuses with exit 2 and the reason on stderr. PostToolUse
and Stop block with `{"decision": "block", "reason": ...}` on stdout. UserPromptSubmit and SessionStart add `additionalContext`; PreCompact
prints text that is added to the compaction instructions.
Exit 0 with nothing on stdout means pass. The messages and exit codes are the same as these hooks had before they
moved into this kit, so an agent sees no change. Each hook's file says what it reads and what it does not, and where
its tests are.

**Why one fails closed and the others open.** `actions-disable-guard` guards against a step that cannot be undone
by an agent (Actions turned off on a repo). If it is gone or broken, refusing every Bash command is the safe answer,
and the message says so. The other five guard mistakes or only add context. Refusing every call because a helper is gone would stop all
work on the machine. The settings entry does both (`agent-guards hooks` prints it):

```sh
# closed
g=/usr/local/bin/actions-disable-guard; [ -r "$g" ] && [ -s "$g" ] || { echo "actions-disable-guard: $g is missing or empty: every Bash and Monitor command is refused until it is back" >&2; exit 2; }; f=/usr/local/bin/agent-guards-fast; [ -r "$f" ] && [ -s "$f" ] || f=; /usr/bin/python3 -IBS ${f:+"$f"} "$g" || { echo "actions-disable-guard: the guard failed (exit $?): the command is refused" >&2; exit 2; }
# open
g=/usr/local/bin/capacity-pipe-guard; [ -r "$g" ] || { echo "capacity-pipe-guard: $g is missing: hook skipped, commands are NOT checked" >&2; exit 1; }; f=/usr/local/bin/agent-guards-fast; [ -r "$f" ] && [ -s "$f" ] || f=; exec /usr/bin/python3 -IBS ${f:+"$f"} "$g"
```

A hook that runs past its timeout is ended by Claude Code. For the open hooks the call then goes on. The
`actions-disable-guard` entry sets `"onFailure": "block"`, so a timeout or a hook that cannot start refuses the
call. Claude Code honours `onFailure` from version 2.1.295 on; an older one ignores it, and there a hook that fails
or times out lets the call run, so run 2.1.295 or later. The guard also answers by itself within 5 s of its start (it
refuses when it runs out of time with a guarded word unread), well inside its 10 s.

**The fast path** (#1094). Every Bash call runs actions-disable-guard, capacity-pipe-guard and memory-index-guard.
A Python script is compiled on every start, and actions-disable-guard is 5,000 lines: ~55 ms a call on an idle
machine, far more under memory pressure, up to the timeout that refuses the call. So their entries start the hook
through `agent-guards-fast` (bin/, [lib/agent_guards/fast.py](lib/agent_guards/fast.py)) when it is there. It reads
the event and passes the call itself only where the hook surely passes it without a word (the guard's own fast exit
for a command of plain words, [quick.py](lib/agent_guards/quick.py), a checked copy of it; no capacity command's
name in the text; no Write to a `MEMORY.md`, an index unchanged since the session's state file). Everything else,
and anything odd (a broken event, a machine file problem to report), runs the hook file on the same event. Its
bytecode is the one the installer wrote in the version copy (`versions/<sha>/bin/__pycache__/`, in the manifest): the
link `/usr/local/bin/<hook>` is resolved first, and only a checked-hash `.pyc` of exactly that file's text, owned
like it and writable by no one else, is used (`fast.hook_code`); a `.pyc` beside the link is never looked at.
Anything else is compiled from the source as before: same answers, ~30 ms slower for actions-disable-guard. That
includes bytecode of another Python: the installer compiles with the Python that runs it (`/usr/bin/python3` by its
first line), so an install run by another one, or a Python upgrade, means compiling from source until the next
`agent-guards-install install`. Reading the event and the fast check have 3 s (an alarm, as the guard's own read
has); past that the hook answers on its own clock (the guard refuses at its 5 s budget). Without
`agent-guards-fast`, or with an empty one, the entry runs the hook as before. Python runs with `-S` (no site; the
hooks use the standard library only). `tests/fast.test.py` runs the hooks' test cases both ways.

**Tools.** The Bash and Monitor tools both run shell text (`tool_input.command`), so the command hooks match both.
`agent-guards check` fails a hook that is wired for Bash only (MISSING ... Monitor).

**Nested agent sessions.** Managed hooks run in every Claude Code session, a nested `claude -p` too, whatever its
own settings say (`disableAllHooks` in a user's or a `--settings` file does not turn managed hooks off). A bare
session (`claude --bare`, `CLAUDE_CODE_SIMPLE`) and Codex (`codex`, `codex exec`, `codex review` ...) run none of
them, so `actions-disable-guard` refuses starting one (`codex login`, `logout`, `help`, `completion`, `mcp` (setup,
not a session; `mcp-server` is refused) and `--version` pass). `claude --safe-mode` is refused too: it runs managed
hooks, but not hooks a user wired by hand in user settings (the `agent-guards hooks` route, below). The guard knows
claude and codex by name, by their program path (`.../claude/versions/<v>`, the `@anthropic-ai/claude-code` and
`@openai/codex` packages), as a link to or a copy of the installed program, through `npx`, `bunx`, `pnpm dlx`, `npm
exec`, `bun x` and `node <their cli>`, behind `$(which claude)`, and inside `python -c`, `node -e`, `at`,
`systemd-run` and the other wrappers it reads for `gh`. It sees `CLAUDE_CODE_SIMPLE` in the command, through `env
-S`, in a file the command sources, in `BASH_ENV` or `--rcfile`, and in the agents' user's shell start files
(`~/.bashrc` ...; files those read in turn are not followed). This is a list of forms, so it is not complete: see
the next section.

**The owner's approval.** Only the owner can let one refused command through, as root, in the folder the command
will run in (or with `--cwd DIR`):

```sh
sudo agent-guards approve --hours 1 --command 'gh workflow disable ci.yml -R owner/repo'
#   prints a code; keeps only hashes of the code and the command, the folder and the end time, in a root-owned file
sudo agent-guards approve --revoke    # ends it now
```

The approval is for that one command (compared after taking the marker off its end; runs of blanks count as one),
run in that folder, once, before the end time. The agent runs it with `#owner-approved:actions-disable:<code>` at its
end. The first run records the use in the guard's state folder (`actionsDisable.stateDir`); a second run, or any
other command carrying the code, is refused. So a code an agent sees in a transcript, the app's database or `ps`
opens nothing else. A new `approve` replaces the approval before it; an approval file of an earlier draft (version
1, not bound to a command) is no approval. The use record lives in the agents' user's folder (the hook runs as that
user): an agent that sets out to can remove it, and then can only repeat the approved command in that folder until
the end time. The file is
`/etc/fork-kit/agent-guards-approval.json` (read under the machine file's rules: root-owned, not writable by group or
others, no link), at most 24 hours. An earlier draft used a session environment variable; every nested session
inherited it, so it is not read any more. Anyone who can become root without a password (sudo `NOPASSWD`, the
`docker` group) can write the file: `agent-guards self-check` warns about both for the agents' user.

## What the guards are and are not

The guards stop **mistakes and casual misuse** by an agent: a command typed from habit, a step copied from an old
note, an agent that takes a short cut it was told not to take. Each refusal says why, and the agent goes another
way. They are **not a security boundary against a hostile agent**. They read shell text before it runs, and text
can always be built so that no reader sees what it does until it runs: words made at run time, a program the guard
does not know (another agent program, a script, an MCP tool that starts a session), a file read by a file the
command sources. A guard that claimed to stop those would be claiming more than the code does.

The real boundary is **the GitHub permissions of the token the agent holds**. The recommended setup: give agents a
fine-grained token for the repos they work on, without the Actions (write) and Administration (write) permissions,
so turning off a workflow, Actions, or a ruleset fails at GitHub whatever command asks for it; keep the token that
can do that with the owner. The guards then catch the mistake earlier, with a reason, and the token holds when they
miss one.

## Settings (the machine file)

Every key is optional. With no machine file, or no `"agent-guards"` section, each hook uses the defaults below.
These are the values the hooks had before. The file is `/etc/fork-kit/machine.json`
([common/machine_file.py](../common/machine_file.py): owned by root, not writable by others, so an agent cannot
change a guard's facts). Each hook reads only its own part (`actionsDisable`, `capacityPipe`, ...). A key in it that
is broken (a wrong type, a bad path) keeps its default; an unknown key is ignored; a bad `refuseScripts` row is
dropped and the good rows kept (the same for a bad `facts.projects` or `facts.records` row). The hook says so to the agent (a `systemMessage`, and context for PreToolUse) and on
stderr, on every call it handles, until the file is fixed: loud on purpose. A Bash call can carry the note more than
once, once from each hook that has a problem in its part (`actionsDisable` and `capacityPipe` before it,
`memoryIndex` after it). A broken or unknown part of another hook changes nothing for it. `agent-guards self-check`
checks the whole section strictly and names every problem.

| Key                            | Default                                | What it is                                                                                                                                            |
| ------------------------------ | -------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `actionsDisable.stateDir`      | `~/.local/state/actions-disable-guard` | The refusal log (`log`) and a parse cache                                                                                                             |
| `actionsDisable.refuseScripts` | `[]`                                   | `{"name": "garden.py", "flag": "--fix", "why": "..."}`: also refuse running that script with that flag (a project's own tool that disables workflows) |
| `capacityPipe.commands`        | `["t3-capacity"]`                      | The capacity checks: commands that answer OK (exit 0) or NOT OK (exit 1)                                                                              |
| `memoryIndex.maxChars`         | `6000`                                 | Characters in the whole index at most                                                                                                                 |
| `memoryIndex.maxLine`          | `160`                                  | Characters in one line at most                                                                                                                        |
| `memoryIndex.stateDir`         | `~/.cache/memory-index-guard`          | One file per session: the index as last seen                                                                                                          |
| `bgGuard.skipSessionsFiles`    | `[]`                                   | Files of session ids (one per line, `#` comments) that are never blocked, such as orchestrators that are woken another way                            |
| `bgGuard.maxBlocks`            | `3`                                    | Blocks per session at most, so a background job left on purpose never loops                                                                           |
| `bgGuard.stateDir`             | `~/.local/state/bg-guard`              | One block count per session (raised under a lock)                                                                                                     |
| `agentSlots.sessionsFile`      | `""` (the hook does nothing)           | The orchestrator session ids it acts for                                                                                                              |
| `agentSlots.queue`             | `""`                                   | A markdown file; the ready list is the `- ` lines under `readyHeading`                                                                                |
| `agentSlots.readyHeading`      | `"## Ready"`                           |                                                                                                                                                       |
| `agentSlots.maxAlive`          | `10`                                   | Agent processes at most                                                                                                                               |
| `agentSlots.processName`       | `"claude"`                             | The process name that is counted                                                                                                                      |
| `agentSlots.note`              | `""`                                   | Extra text for the context line (a build-agent limit, say)                                                                                            |
| `facts.dirs`                   | `[]`                                   | State tables for every session: a table file, or a folder whose `STATE.md` is the table ([FACTS.md](FACTS.md))                                        |
| `facts.projects`               | `[]`                                   | `{"match", "dirs", "text", "name"}`: more tables for one project (`match`: its remote as `host/owner/repo`, or a folder; `name`: its record folder)   |
| `facts.staleDays`              | `30`                                   | `facts --check` reports a row whose `Updated` is older                                                                                                |
| `facts.maxRows`                | `60`                                   | `facts --check` reports a table with more rows                                                                                                        |
| `facts.indexLines`             | `40`                                   | Things the session-start context shows at most                                                                                                        |
| `facts.indexChars`             | `4000`                                 | Characters of the whole session-start context at most                                                                                                 |
| `facts.records`                | `[]`                                   | `{"path", "projectsDir", "shared"}`: record repos that `facts <words>` searches after the tables ([FACTS.md](FACTS.md))                               |

Paths are absolute or start with `~/` (the home of the user the hook runs as). An example is in
[machine.example.json](../common/machine.example.json).

**State and logs.** Each hook keeps going when its state folder or log cannot be written (a full disk, a folder
that is not writable): the answer never depends on it. `memory-index-guard` writes its state whole (a temporary
file, then a rename). `bg-guard` counts under a lock, so two Stop hooks at once count two blocks.
`actions-disable-guard` appends one line per refusal. `agent-slots` and `facts-hook` write nothing.

## Install

As root, from a release commit of the fork (the shared [bootstrap](../common/bootstrap.sh) checks that it is on the
protected `ci-release` branch first):

```sh
sh fork-kit-bootstrap.sh --kit agent-guards --repo OWNER/FORK --sha RELEASE_SHA [--settings | --no-settings]
```

The installer ([sbin/agent-guards-install](sbin/agent-guards-install), on the shared
[kit_install.py](../common/kit_install.py)) copies the release to `/usr/local/libexec/agent-guards/versions/<sha>/`,
self-tests the copy, and links `/usr/local/bin/<hook>`, `/usr/local/bin/agent-guards` and `/usr/local/bin/facts` to it. It is safe to run
again and to stop at any step. `facts` is a common name: when `/usr/local/bin/facts` is something other than the kit's
link, the installer leaves it alone and says so (a warning; the install goes on, and uninstall does not remove it or
wait for it to stop running). Installing a release from before `facts` was added leaves `/usr/local/bin/facts` as a
link to a file that is not there (`facts` then says "No such file") until a newer release is installed again or the
kit is uninstalled.

`--settings` also writes the hooks' entries as a Claude Code **managed settings drop-in**,
`/etc/claude-code/managed-settings.d/agent-guards.json`. Every Claude Code session of every user on the machine
reads it, and only root can change it. It is written after the links, so every hook it names is there. Sessions
already running keep the hooks they started with. `--no-settings` removes it; the choice is kept, so a run with
neither keeps the last one (a first install: no drop-in). The installer refuses to overwrite a drop-in of that name
it did not write.

Uninstall refuses while a settings file the kit did not write (a user's `~/.claude/settings.json`, or the project
`.claude/settings*.json` of a folder a running session works in) still names a hook: removing the file would make
that entry fail. Otherwise it removes the drop-in first, then refuses while a Claude Code session started before
that still runs (it has the hooks loaded); run it again when those have ended. It finds sessions by the real path of
their program (`/proc/<pid>/exe`, the script node runs), so a renamed link to Claude Code counts too. When it once
saw a hand-wired entry, or the drop-in was never on, every session older than the uninstall counts, since such a
session may have read an entry that is gone now. Run uninstall from a terminal: from inside a session that has the
hooks it refuses, because that session would lose them.
To take the hooks out of new sessions at once and keep the files, run the installer with `--no-settings` (the
rollback).

Without `--settings`, put the entries in a settings file by hand: `agent-guards hooks` prints the `"hooks"` object.
A hook that is in both the drop-in and a user's settings runs twice. That is harmless for the refusing hooks, but
`bg-guard` then counts two blocks per stop, so remove the old entries soon after.

## Check

```sh
agent-guards self-check                      # the machine file section, the hook files and links, the drop-in, an open approval, ways to root
agent-guards check                           # run every hook wired in the drop-in: one case it must refuse, one it must pass
agent-guards check --settings ~/.claude/settings.json      # the same for hooks wired in any settings file
agent-guards-install self-check              # the installed files are the ones the installer wrote
```

`check` runs each wired command the way Claude Code does (`/bin/sh -c`, the event on stdin) and prints PASS or FAIL
for each case, and MISSING for a hook that is not wired. It changes nothing on the machine: the memory index and the
sessions are made up in a temporary folder, the hooks run with `HOME` there (their logs and caches go with it), and
the state files in configured folders are removed afterwards. For `bg-guard` it starts a `sleep` in the background
under a stand-in parent named `claude`. As root, `check --settings FILE` reads the file only when it is safe to run
what it names: root-owned, not writable by group or others, no link on the way.

## General vs project

Everything in this kit is general. What a project or a machine adds is only its facts in the machine file: the
capacity command's name, the memory budget, the orchestrator's session file and queue, a script of its own that
must not run with a given flag, and where its state tables are. The tables themselves live with the machine or the
project they describe, never in the kit. Nothing in the hooks names a project, a person or a path of one machine.

## Tests

`tests/*.test.py`, run by the fork's CI (`.github/fork-ci-tests.txt`), with no root, no network and no installed
kit. They cover each hook's refuse and pass cases, and what happens when state is missing or old. That means: a
machine file that is missing, empty, garbled or not root-owned; a log or state folder that is missing or not
writable; two hooks at once; very long commands; and commands that only mention a guarded word (echo, a heredoc
into a file, a commit message). The settings entries are run as `/bin/sh -c` with the hook missing and with the
hook failing. `tests/fast.test.py` runs the hooks' cases through the fast path and through the hook, and expects the
same answer.
