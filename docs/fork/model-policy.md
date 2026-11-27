# Model policy: who checks what, and the way back

Fork only. The model policy decides which models and efforts agents may pick (the setting
[`server.modelPolicy`](README.md#servermodelpolicy), **Settings → Fork → Models agents may use**).
This page says which part checks each way an agent can start or change an agent, how to make the
check one an agent cannot turn off, and how to roll it back. The rules a pick must meet are in the
setting's section of the [README](README.md#servermodelpolicy); the hook's are in
[fork-kit/model-guard](../../fork-kit/model-guard/README.md).

## One check per route

Each route gets one check. The T3 server checks everything that reaches it. The model-guard hook,
which runs inside Claude Code, is for what the server cannot see. On a machine where T3's managed
policy locks `modelPolicy` (the lock step below), the hook leaves the T3 tools in the first two rows
to the server. On a machine without that lock, it checks them too, against its own policy file, so
skipping the lock step never leaves those routes checked only by a policy an agent can edit.

| Route                                                                                                                                                                         | Checked by                                                                                                                                   |
| ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `delegate_task`, `create_threads`, `t3_thread_launch`, `t3_thread_configure` (here and on linked machines)                                                                    | the server, by the route's own check                                                                                                         |
| every other thread command an agent's MCP call dispatches with a model, a thread fork (`t3_thread_transfer`), and `run_scheduled_task_now` on a task that starts a new thread | the server's backstop (below)                                                                                                                |
| Claude's `Agent` tool and a skill that starts a sub-agent or names a model, in a session T3 opened                                                                            | T3's own PreToolUse hook, which reads the server's policy (the model-guard hook reads `Agent` too: it cannot tell T3's sessions from others) |
| Claude's `Agent` tool in a Claude Code session T3 did not open, `Workflow` scripts, `RemoteTrigger`                                                                           | the model-guard hook                                                                                                                         |
| a project's default model (`t3_project_create`, `t3_project_update`) and `schedule_task`                                                                                      | the model-guard hook (the owner left these to the hook, [#769](https://github.com/scuttletoby/t3code/issues/769))                            |

### The backstop

Every thread command passes one place in the server (`ThreadManagementService.dispatch`). Before a
command is dispatched there, the backstop (`apps/server/src/fork/modelPolicyBackstop.ts`) checks it
against the policy when all of these hold:

- it comes from an agent's MCP call: a thread bound to the session here, or a linked machine's call
  that names its agent's thread;
- that call's route did not check its picks itself (the four routes in the table's first row do);
- the command names a model (a new thread, a model change, a message, a queued run's edit or a
  delegated task with its own model, a task sent to a linked machine) or forks a thread, which runs
  on its source thread's model;
- it is not a replay: a command that already has a receipt (a retry of an accepted `delegate_task`,
  `create_threads` or `t3_thread_launch` call, say) starts nothing new, so it is answered from its
  receipt as the first time, whoever checked it then, and is not checked or logged again after a
  policy change.

The backstop reads the dispatching service's own settings, not the call's context, so a call path
without the settings service is still checked. With the policy `off` it reads nothing, not even a
fork's source thread.

So a new tool that starts a thread is checked without its own check, and a fork of a thread whose
model is outside the rows is refused in `enforce` (start the thread with `t3_thread_launch` or
`delegate_task` and a model from the rows instead). `advise` only logs the pick, as a dispatch has
no reply to carry a note. Each pick is logged once as "model policy pick", with route
`agent command`. A refusal reaches the agent as upstream's `orchestration_error` (not the routes'
`model_unavailable`), cut at 1000 characters, so past that the rows are shortened to the first
ones and how many more there are, never the fix at the end. Not read: the owner's client, and work
the server starts by itself outside an agent's call (a scheduled run on its schedule or webhook, an
automatic handoff, a provider's own sub-agents).

- **`run_scheduled_task_now`:** a task that starts a new thread runs on the task's own model, which
  the owner may have picked, as the calling agent's pick: outside the rows, the run fails with route
  `scheduled task`, and the task's last run error is the refusal, which says to change the task's
  model with `schedule_task` (or that the owner runs it). A task bound to a thread sends a message
  without a model and runs on that thread's model, as `t3_thread_send` does, so it is not read.
- **A route's mark:** a route that checked its picks marks its `/mcp` request, and the backstop
  leaves every command of that request alone. A JSON-RPC batch is one request, so one call in it
  that checked its picks leaves the batch's other calls unread. An agent able to send raw batches
  with its session's token has easier ways out of the policy; the mark is per request, not per call.
- **Linked machines:** each machine checks a pick against its own rows. The sending machine's
  routes check `t3_thread_launch` and `t3_thread_configure` before they go; a `t3_thread_send` with
  a model or a `t3_thread_transfer` fork sent to a linked machine is checked only there, and not at
  all while that machine's policy is `off`.

## A check an agent cannot turn off

The server reads the policy from its `settings.json`, which an agent with a shell on the machine
can edit. To stop that, lock it in T3's managed policy, a file only root can change:
`/etc/t3code/policy.json` on Linux, `/Library/Application Support/T3Code/policy.json` on macOS,
`C:\Program Files\T3Code\policy.json` on Windows. A locked `modelPolicy` wins over the settings file
and cannot be changed in Settings; it is locked as a whole value, not one field inside it.

The hook leaves the server's routes to T3 only while that file is root-owned, not writable by group
or others, not a link, and locks `modelPolicy` at the hook's mode or a stricter one, with no row or
effort the hook's own rows do not cover. In any other state it keeps checking them. So give both the
same rows, and switch both to `enforce` together.

### The lock step (each machine, as root)

Linux. The rows are the fork's start rows (#126), in `advise`, as in the hook's paste-ready
install. T3 reads the managed policy only when it starts. It refuses to start while the file is not
valid (a key it does not know counts), so write it with this command, not by hand. A policy file that is
already there is kept (`set -C`): merge the `modelPolicy` entry into its `settings` instead.

```sh
umask 022 && mkdir -p /etc/t3code
( set -C; cat > /etc/t3code/policy.json ) <<'JSON' || echo "kept the policy already in /etc/t3code/policy.json: add the modelPolicy entry to its settings"
{
  "schemaVersion": 1,
  "organization": "Machine owner",
  "settings": {
    "modelPolicy": {
      "locked": true,
      "value": {
        "mode": "advise",
        "allow": [
          { "driver": "claudeAgent", "model": "claude-opus-5-5", "efforts": ["medium", "high", "xhigh"], "defaultEffort": "high" },
          { "driver": "claudeAgent", "model": "claude-fable-5-2", "efforts": ["medium", "high", "xhigh"], "defaultEffort": "high" }
        ]
      }
    }
  }
}
JSON
python3 -c 'import json; json.load(open("/etc/t3code/policy.json"))' && echo "policy.json is JSON"
model-guard --self-check   # "T3 server routes (...): left to T3" once the lock holds; "checked here too: <why>" otherwise
```

Then restart T3 (as its own service, or the app), so it reads the lock. **Settings → Fork → Models
agents may use** can no longer change it. For macOS, use the same file at
`/Library/Application Support/T3Code/policy.json`. Do not also deploy a configuration profile there:
T3 refuses to start when it finds both.

**Later, enforce:** in the same file, `"mode": "advise"` becomes `"mode": "enforce"`. Switch the hook's
own policy to `enforce` at the same time (its README, "Later, enforce"), then restart T3. A lock at
`advise` under an `enforce` hook does not count, so the hook goes on checking the server's routes
until both say `enforce`.

## The way back

- **Turn it down at once:** set `"mode": "advise"` (agents are only told) or `"mode": "off"` in
  `server.modelPolicy`, in Settings or in `settings.json` (read again at once). While the managed
  policy locks it, root changes it there instead, and T3 reads it at its next start. A locked mode
  below the hook's makes the hook check the server's routes again, from its next call.
- **Undo the lock step:** as root, remove the `modelPolicy` entry (or the whole file, if it holds
  nothing else) and restart T3. The hook checks the server's routes again from its next call.
- **The hook:** `/usr/local/sbin/model-guard-install uninstall`, as root (see its README). Or set
  its own policy file's `mode` to `off`.
- **The backstop alone:** revert the pull request that adds it. Its two lines in
  `ThreadManagementService.ts` are listed in `.github/fork-seams.json`; with them gone, the
  routes in the table's second row are not checked by the server.
