# The assistant

Fork only. The assistant is one long thread you talk to across projects. It can read threads, and
start and message work in your projects. It has no shell, no file tools and no web of its own. Later pull
requests let it find each project's orchestrator by itself; mail and calendar come later as
simple tools ([#879](https://github.com/scuttletoby/t3code/issues/879)).

## Making it

1. **Settings → Fork**, section Assistant: switch on **Offer Make assistant** (`assistant.enabled`).
2. Press **Make assistant**. T3 first saves the MCP profile pick `assistant` for a new thread id
   (`mcpThreadProfiles`), then makes a thread named "Assistant" with that id in the Scratch project
   (the project with no folder), then names it the assistant (`threadRoles.assistant`). The app
   opens it. The pick comes first so a thread called "Assistant" never runs with a wider profile,
   even if setup stops half-way.

The button needs exactly one environment selected at the top and a Claude provider that is on and
available. The thread gets Claude's default model; change it in the thread as usual.

Only you can do this: both keys are person-only, so no agent's tool can name a thread the assistant
or give it a profile. The server checks the role as for any `threadRoles` save: the thread must
exist and be a top-level thread. If that save is refused, the new thread is removed again while it
is still empty. The button is locked while a managed policy owns `threadRoles` or
`mcpThreadProfiles`.

## The profile `assistant`

A built-in MCP profile, like `all` and `t3-only`:

- `servers: []`: no outside MCP servers.
- `builtinTools: "none"`: no shell, file or web tools of the provider. Only Claude can apply this, so
  a turn on any other provider is refused and says why.
- `t3Tools`: the groups `orchestration`, `threads` and `projects`, plus the tools every profile
  keeps (`send_to_user`, `request_secret`, `tool_search`). These are more than reading and
  messaging:
  - `orchestration`: list, read, message, wait for, interrupt and update threads; delegate tasks
    and launch threads (`delegate_task`, `create_threads`); schedule tasks; link and unlink
    environments.
  - `threads`: launch, configure (model and settings of another thread), transfer, search and
    organize threads.
  - `projects`: list and read projects, and also create, clone (a repository onto disk), update and
    delete them.

  Each of these keeps its own server checks (model policy, launch limits, person-only settings).

Any thread can pick it, not only the assistant. A sub-agent of the assistant gets at most the same.
A profile in `mcpProfiles` under the name `assistant` makes the built-in refuse its threads until
you remove it (the rule for every built-in name).

**Upgrading with your own profile called `assistant`:** before this version that was an ordinary
name. After it, every thread that picks it is refused until the entry is removed or renamed. When
you remove it, those threads get the built-in `assistant` instead, which may differ from yours, so
rename it and move their picks first.

Not yet: the assistant cannot launch a thread into a project whose profile is wider than its own
(the launch check refuses a narrower thread starting a wider one). It can message threads there.
A later pull request settles how the assistant starts work.

## When things change

- **Made twice:** the second press makes a new thread and moves the pointer to it. The old thread
  keeps its `assistant` profile pick and stays as it is; delete or archive it if you like. The
  button is off while a setup runs, so a double click makes one thread.
- **Stops half-way:** after the pick only, nothing is made (a pick for a thread that does not exist
  does nothing). After the thread is made but before the role, an "Assistant" thread is left in
  Scratch with the `assistant` profile and no role; Settings still says there is no assistant.
- **Its thread archived or deleted:** Settings says it is archived, deleted or not on this server
  (the app's thread list holds active threads only), and readers treat it as no assistant. The
  pointer and the profile pick stay in the file until a new Make assistant replaces the pointer.
- **Profile edited by hand:** an entry `mcpProfiles.assistant` makes the assistant's turns refused
  (fail closed) until it is removed; the switch and pointer are not touched.
- **Settings unreadable:** a bad `assistant` value reads as off; a bad `threadRoles` as no roles; a
  thread whose profile cannot be read is refused, never given every tool.
- **An older client or server:** an older fork server does not know the profile `assistant`, so
  it refuses the first save (the pick) and nothing is made, and it refuses the turns of a thread
  already set to it, never giving it more. An older client has no Assistant section and shows the pick as a
  profile it does not know.
