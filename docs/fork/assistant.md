# The assistant

Fork only. The assistant is one long thread you talk to across projects. It can read threads, and
start and message work in your projects. It has no shell, no file tools and no web. Later pull
requests let it find each project's orchestrator by itself; mail and calendar come later as
simple tools ([#879](https://github.com/scuttletoby/t3code/issues/879)).

## Making it

1. **Settings → Fork**, section Assistant: switch on **Offer Make assistant** (`assistant.enabled`).
2. Press **Make assistant**. T3 makes a thread named "Assistant" in the Scratch project (the project
   with no folder), then in one settings save picks the MCP profile `assistant` for it
   (`mcpThreadProfiles`) and names it the assistant (`threadRoles.assistant`). The app opens it.

The button needs exactly one environment selected at the top and a Claude provider that is on and
available. The thread gets Claude's default model; change it in the thread as usual.

Only you can do this: both keys are person-only, so no agent's tool can name a thread the assistant
or give it a profile. The server checks the role as for any `threadRoles` save: the thread must
exist and be a top-level thread. If that save is refused, the new thread is removed again while it
is still empty.

## The profile `assistant`

A built-in MCP profile, like `all` and `t3-only`:

- `servers: []`: no outside MCP servers.
- `builtinTools: "none"`: no shell, file or web tools of the provider. Only Claude can apply this, so
  a turn on any other provider is refused and says why.
- `t3Tools`: the groups `orchestration`, `threads` and `projects`, plus the tools every profile
  keeps (`send_to_user`, `request_secret`, `tool_search`).

Any thread can pick it, not only the assistant. A sub-agent of the assistant gets at most the same.
A profile you write into `mcpProfiles` under the name `assistant` makes the built-in refuse its
threads until you remove it (the rule for every built-in name).

Not yet: the assistant cannot launch a thread into a project whose profile is wider than its own
(the launch check refuses a narrower thread starting a wider one). It can message threads there.
A later pull request settles how the assistant starts work.

## When things change

- **Made twice:** the second press makes a new thread and moves the pointer to it. The old thread
  keeps its `assistant` profile pick and stays as it is; delete or archive it if you like.
- **Its thread archived or deleted:** Settings shows it as archived or gone, and readers treat it as
  no assistant. The pointer and the profile pick stay in the file until a new Make assistant (or a
  settings save) replaces them.
- **Profile edited by hand:** an entry `mcpProfiles.assistant` makes the assistant's turns refused
  (fail closed) until it is removed; the switch and pointer are not touched.
- **Settings unreadable:** a bad `assistant` value reads as off; a bad `threadRoles` as no roles; a
  thread whose profile cannot be read is refused, never given every tool.
- **An older client or server:** an older fork server does not know the profile `assistant`, so
  it refuses the save (the empty thread is removed) and refuses the turns of a thread already set
  to it, never giving it more. An older client has no Assistant section and shows the pick as a
  profile it does not know.
