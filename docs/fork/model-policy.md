# Model policy: who checks what, and the way back

Fork only. The model policy decides which models and efforts agents may pick (the setting
[`server.modelPolicy`](README.md#servermodelpolicy), **Settings → Fork → Models agents may use**).
This page says which part checks each way an agent can start or change an agent, how to make the
check one an agent cannot turn off, and how to roll it back. The rules a pick must meet are in the
setting's section of the [README](README.md#servermodelpolicy); the hook's are in
[fork-kit/model-guard](../../fork-kit/model-guard/README.md).

## One check per route

Each route gets one check. The T3 server checks everything that reaches it. The model-guard hook,
which runs inside Claude Code, is for what the server cannot see. Until its copies of the server's
routes are removed (#876, M7), the hook checks the first two rows too, against its own policy file.

| Route                                                                                                            | Checked by                                                                                                                                   |
| ---------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `delegate_task`, `create_threads`, `t3_thread_launch`, `t3_thread_configure` (here and on linked machines)       | the server, by the route's own check                                                                                                         |
| every other thread command an agent's MCP call dispatches with a model, and a thread fork (`t3_thread_transfer`) | the server's backstop (below)                                                                                                                |
| Claude's `Agent` tool and a skill that starts a sub-agent or names a model, in a session T3 opened               | T3's own PreToolUse hook, which reads the server's policy (the model-guard hook reads `Agent` too: it cannot tell T3's sessions from others) |
| Claude's `Agent` tool in a Claude Code session T3 did not open, `Workflow` scripts, `RemoteTrigger`              | the model-guard hook                                                                                                                         |
| a project's default model (`t3_project_create`, `t3_project_update`) and `schedule_task`                         | the model-guard hook (the owner left these to the hook, [#769](https://github.com/scuttletoby/t3code/issues/769))                            |

### The backstop

Every thread command passes one place in the server (`ThreadManagementService.dispatch`). Before a
command is dispatched there, the backstop (`apps/server/src/fork/modelPolicyBackstop.ts`) checks it
against the policy when all of these hold:

- it comes from an agent's MCP call: a thread bound to the session here, or a linked machine's call
  that names its agent's thread;
- that call's route did not check its picks itself (the four routes in the table's first row do);
- the command names a model (a new thread, a model change, a message or a delegated task with its
  own model) or forks a thread, which runs on its source thread's model.

So a new tool that starts a thread is checked without its own check, and a fork of a thread whose
model is outside the rows is refused in `enforce` (start the thread with `t3_thread_launch` or
`delegate_task` and a model from the rows instead). `advise` only logs the pick, as a dispatch has
no reply to carry a note. Each pick is logged once as "model policy pick", with route
`agent command`. Not read: the owner's client, and work the server starts by itself outside an
agent's call (a scheduled run, an automatic handoff, a provider's own sub-agents).

## A check an agent cannot turn off

The server reads the policy from its `settings.json`, which an agent with a shell on the machine
can edit. To stop that, lock it in T3's managed policy, a file only root can change:
`/etc/t3code/policy.json` on Linux, `/Library/Application Support/T3Code/policy.json` on macOS,
`C:\Program Files\T3Code\policy.json` on Windows. A locked `modelPolicy` wins over the settings file
and cannot be changed in Settings; it is locked as a whole value, not one field inside it. The
model-guard hook reads its own root-owned policy file; give it the same rows.

## The way back

- **Turn it down at once:** set `"mode": "advise"` (agents are only told) or `"mode": "off"` in
  `server.modelPolicy`, in Settings or in `settings.json` (read again at once). While the managed
  policy locks it, root changes it there instead.
- **The hook:** `/usr/local/sbin/model-guard-install uninstall`, as root (see its README). Or set
  its own policy file's `mode` to `off`.
- **The backstop alone:** revert the pull request that adds it. Its two lines in
  `ThreadManagementService.ts` are listed in `.github/fork-seams.json`; with them gone, the
  routes in the table's second row are not checked by the server.
