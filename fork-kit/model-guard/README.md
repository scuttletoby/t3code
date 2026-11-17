# model-guard: keep agents' model picks inside a policy

`model-guard` is a Claude Code `PreToolUse` hook. It checks every call where an agent picks the model of another
agent, or could start or change one, against a policy file that the person who runs the machine writes. The checked
tools are one list in [lib/model_guard.py](lib/model_guard.py) (`CLAUDE_TOOLS`, `T3_TOOLS`); the hook's matcher and
the self-check are made from it:

- Claude Code's `Agent` tool (`Task` in older versions) and its `Workflow` tool;
- T3's MCP tools `delegate_task`, `create_threads`, `t3_thread_launch`, `t3_thread_configure`,
  `t3_thread_transfer`, `t3_thread_send`, `t3_thread_send_attachments` and `schedule_task` (under any MCP server
  name).

Every other tool passes untouched. This is version 0 of the fork's model policy: a hook on the agent's side. A later
version checks the same picks inside the T3 server, where it can also see the session's own model.

Linux, Python 3 standard library only. No state: the hook reads the machine file and the policy again on every call.

## Rules

| Case                                                                                            | `off`  | `advise`                     | `enforce`                 |
| ----------------------------------------------------------------------------------------------- | ------ | ---------------------------- | ------------------------- |
| a tool the hook does not check                                                                  | pass   | pass                         | pass                      |
| the machine file or the policy missing, unreadable, not root-owned, invalid                     | refuse | refuse                       | refuse                    |
| the call names no model, or `inherit`                                                           | pass   | pass + what enforce would do | refuse, listing the rows  |
| the model (or, for `Agent` and `Workflow`, an alias) is in no row                               | pass   | pass + note                  | refuse, listing the rows  |
| the call names no `driverKind` and one of the rows of that model does not allow it              | pass   | pass + note                  | refuse, listing the rows  |
| the row has efforts and the call names none, or one not in the row                              | pass   | pass + note                  | refuse, listing the rows  |
| the row has no `efforts` and the call names an effort (any, `max` too)                          | pass   | pass + note                  | refuse, listing the rows  |
| ultracode is on (see below) and the row does not list the effort `ultracode`                    | pass   | pass + note                  | refuse, listing the rows  |
| `create_threads`: any thread breaks a rule above, or `threads` is empty                         | pass   | pass + note                  | refuse, listing the rows  |
| `t3_thread_launch` without `modelSelection`                                                     | pass   | pass + note                  | refuse                    |
| `t3_thread_configure` without `modelSelection` (it only reads)                                  | pass   | pass                         | pass                      |
| `Workflow`: a script the hook cannot read for sure, or an `agent()` call breaking a rule        | pass   | pass + note                  | refuse                    |
| `t3_thread_transfer` with `action: "fork"`                                                      | pass   | pass + note                  | refuse                    |
| a send, send-attachments, schedule or transfer call with a field that looks like a model choice | pass   | pass + note                  | refuse                    |
| the hook input is not a JSON object                                                             | refuse | refuse                       | refuse                    |
| an allowed pick                                                                                 | pass   | pass + the row's `useFor`    | pass + the row's `useFor` |

A broken policy is refused in every mode, `off` too: the guard fails closed, and a file nobody can read is not a
decision to turn it off. "Pass" means the hook says nothing about permission: the call still goes through the
session's own permission rules (the hook never answers "allow"). A refusal names the problem, every allowed row
with its efforts, aliases and `useFor` note, and the policy file.

Where the effort is read:

- `Agent`: its `effort` field; `Workflow`: the `effort` of each `agent()` call;
- the T3 tools: `options` of the target or `modelSelection`, either `{"effort": "high"}` or
  `[{"id": "effort", "value": "high"}]`, under any of the option ids `effort`, `reasoningEffort`,
  `reasoning_effort`, `reasoning`, `thinking_level` (the same list T3's usage pacing gate uses; a test keeps them in
  step). Every effort named must be allowed.
- **Ultracode** is checked as what the server runs. The option `ultracode` (any value but `false`) runs the model in
  ultracode at effort xhigh, and the old effort value `"ultracode"` means xhigh with the option on (unless the option
  is given). Either way the guard counts the effort `ultracode` instead of `xhigh`, so
  `{"effort": "xhigh", "ultracode": true}` needs `ultracode` in the row's `efforts`, and `{"effort": "high",
  "ultracode": true}` needs both `high` and `ultracode`. A test reads this mapping from `packages/shared/src/model.ts`
  so the two stay in step.

What v0 does not check: which provider instance or account a T3 call names (`providerInstanceId`, `environmentId`),
and the model of the session that makes the call.

### Decisions

- **Every pick names its model and effort.** The hook cannot see the parent's model or the provider's catalog, so
  nothing may be inherited: a T3 call without a model, or an `Agent` call without `model` (or with `inherit`), is
  refused.
- **A row without `efforts` allows no effort.** It is for a model with no effort control: a call that names any
  effort for it, `max` too, is refused. To allow efforts, list them; there is no "any effort" row.
- **`Agent` with `subagent_type: "fork"`** is checked like every other `Agent` call: it must name an allowed model
  and effort. Claude Code runs a fork on the parent's own model whatever the call says, so v0 cannot see what a fork
  really runs on. A fork from a session whose model is outside the policy is a known gap until the server-side
  version, which checks the session's model.
- **`Workflow`** runs a script whose `agent(prompt, {model, effort, ...})` calls start agents. The hook reads the
  script as text; it is not a sandbox. It passes only an inline `script` where every `agent()` call is a direct call
  with exactly two arguments, the second an object literal of plain `key: value` pairs whose `model` and `effort`
  are plain strings without escapes, and each of those picks is allowed (the same rules as `Agent`). It refuses in
  `enforce` a `scriptPath` or a saved workflow `name` (the hook cannot read them), a nested `workflow()`, `agent`
  used any other way (renamed, passed on, called through a member), a computed key or member access other than
  `[number]`, a spread, a name that reaches the global object or builds code (`eval`, `Function`, `constructor`,
  `globalThis`, `this`, `import`, `require`, `Reflect`, `Proxy`, `__proto__` and more), letters outside ASCII
  outside strings, brackets that do not match, or a script over 1 MiB. It also refuses **every `/` that does not
  open a `//` or `/* */` comment**: a regular expression literal or a division (use `new RegExp("...")`, or
  multiply). Telling the two apart needs a full JavaScript parser, so the hook does not guess. And it refuses a
  line end other than `\n` or `\r\n` anywhere in the script (a bare `\r`, U+2028 or U+2029), because JavaScript
  ends a `//` comment there too. A `/` inside a string, a template or a comment, and a script with `\r\n` line ends,
  pass.
- **`t3_thread_transfer` fork** starts a thread on the source thread's model, which the hook cannot see, so it is
  refused in `enforce`: start the thread with `t3_thread_launch` and a `modelSelection` instead. `merge_back` and
  `status` start no model and pass.
- **`t3_thread_send`, `t3_thread_send_attachments` and `schedule_task`** run on a model that was already picked
  (the thread's, or the calling thread's or project's for a scheduled task) and take no model of their own. A
  field whose name looks like a model choice on them (or on `t3_thread_transfer`) is refused, because the hook
  cannot tell what a future server would do with it.
- **Aliases.** `Agent` and `Workflow` take short names (`opus`, `sonnet` and so on) that Claude Code maps to a
  model. The hook has no built-in list: a row's `aliases` are the only short names they may use for it, and the
  policy's author names them. Both always run on Claude, so they match only rows of driver `claudeAgent`. The T3
  tools take only the exact `model` of a row; an alias there is refused.
- **Driver.** A T3 call that names `driverKind` must match the row's `driver`. T3's model selection usually names
  no driver; then **every** row that names the model must allow the pick. Example: rows `{driver A, model X,
  efforts [high]}` and `{driver B, model X, efforts [medium]}`, and a call `{model X, effort medium}` with no
  `driverKind`: refused, because row A does not allow it. This is the safe side: the hook cannot tell which driver
  the server will use. The same model name may be in rows of two drivers; one driver may not name a model or alias
  twice.

## The policy file

```json
{
  "mode": "advise",
  "allow": [
    {
      "driver": "claudeAgent",
      "model": "example-large-1-0",
      "efforts": ["medium", "high", "xhigh"],
      "defaultEffort": "high",
      "aliases": ["large"],
      "useFor": "most work: building, reviewing, relays"
    },
    {
      "driver": "exampleDriver",
      "model": "example-plain-1",
      "useFor": "a model with no effort control"
    }
  ]
}
```

[model-policy.example.json](model-policy.example.json) is a full example with made-up model names.

| Key             | Required | What                                                                         |
| --------------- | -------- | ---------------------------------------------------------------------------- |
| `mode`          | yes      | `off`, `advise` or `enforce`                                                 |
| `allow`         | yes      | the rows; an empty list refuses every pick in `enforce`                      |
| `driver`        | yes      | T3's driver kind (`claudeAgent` for Claude)                                  |
| `model`         | yes      | the exact model id T3 uses                                                   |
| `efforts`       | no       | the efforts allowed with it; without the key, a call may name no effort      |
| `defaultEffort` | no       | one of `efforts`; shown in the refusal so the agent knows what to pick       |
| `aliases`       | no       | the short names the `Agent` tool may use for this row                        |
| `useFor`        | no       | what the model is for; shown to the agent with every pick of it (no refusal) |

Rules of the file: JSON with no key given twice and no unknown key; lists not empty and without repeats; texts not
empty. The file must be owned by root, not writable by group or others, and not a link (the same rules as the
machine file), so an agent cannot widen its own policy.

## Turn it on (owner, as root)

1. Write the policy, for example `/etc/fork-kit/model-policy.json`, root-owned, mode 644, in folders that are
   root-owned and not writable by others all the way up to `/`. Start with `"mode": "advise"` (or `"off"`).
2. Name it in the machine file `/etc/fork-kit/machine.json` (see [common](../common/README.md)):

   ```json
   "model-guard": {
     "policyFile": "/etc/fork-kit/model-policy.json"
   }
   ```

   | Key               | Required | What                                                                    |
   | ----------------- | -------- | ----------------------------------------------------------------------- |
   | `policyFile`      | yes      | the policy; an absolute path                                            |
   | `allowOtherHooks` | no       | `true` accepts user and project hooks next to the guard (default false) |

3. Only now install the kit (below). Write the policy and the machine file first: from the installer's settings
   step on, every session runs the hook, and with no usable policy the hook refuses every checked call in every
   mode (`off` included, since the mode lives in the policy). On a machine with running agents, every `Agent`,
   delegate, thread send and schedule call would be refused until steps 1 and 2 are done.

   The installer writes the hook into Claude Code's **managed settings**, which only root can change and which
   every Claude Code and Agent SDK session reads, whatever settings sources it uses
   ([Claude Code docs: managed settings](https://code.claude.com/docs/en/managed-settings)). Its own file there is
   `managed-settings.d/model-guard.json` in `/etc/claude-code` on Linux (`/Library/Application Support/ClaudeCode`
   on macOS, `C:\Program Files\ClaudeCode` on Windows; the kit itself is Linux only today):

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "^(Agent|Task|Workflow|mcp__.+__(delegate_task|create_threads|t3_thread_launch|t3_thread_configure|t3_thread_transfer|t3_thread_send|t3_thread_send_attachments|schedule_task))$",
           "hooks": [
             {
               "type": "command",
               "command": "/usr/local/bin/model-guard || exit 2",
               "onFailure": "block"
             }
           ]
         }
       ]
     }
   }
   ```

   How Claude Code treats a hook that fails (from its [hooks reference](https://code.claude.com/docs/en/hooks)):
   exit 2 blocks the call, and any other non-zero exit lets it through. A `PreToolUse` command hook that reaches
   its `timeout` (600 seconds unless set) is cancelled and does **not** block, and neither does one that cannot
   start, unless the hook has `"onFailure": "block"` (Claude Code 2.1.295 or later). So the entry has both:
   `"onFailure": "block"` blocks the call on a timeout or a hook that cannot start, and `|| exit 2` turns any
   other failure of the guard into a block on older versions too. The hook itself exits 2 on any error. On a
   version older than 2.1.295 a hook that hangs until the timeout lets the call through; the guard does no network
   or slow work, so it answers in well under a second.

4. Turn on `allowManagedHooksOnly` in `managed-settings.json` (the owner's choice):

   ```json
   { "allowManagedHooksOnly": true }
   ```

   Claude Code runs every matching hook of every settings file in parallel, and a user or project hook can change
   a call's input (`updatedInput`) after the guard has seen it. With `allowManagedHooksOnly` only managed hooks
   run, so an agent cannot add such a hook by editing its own settings. It also stops every user and project hook,
   so move the hooks you need into managed settings first. If you accept the risk instead, set
   `"allowOtherHooks": true` in the machine file's section (step 2) and the self-check stops asking for it.
   `allowManagedHooksOnly` closes the hook route only: settings `env` can still change the model (see
   [What `enforce` does not stop](#what-enforce-does-not-stop); owner decision #298).

5. Run `model-guard --self-check`. When advise mode has shown no surprises, set `"mode": "enforce"`. The change
   counts from the next call; nothing needs restarting.

## What `enforce` does not stop

The guard catches honest mistakes: an agent that picks a model or effort the policy does not list. It is not a hard
control against an agent that sets out to get around it. The open routes (as read in Claude Code 2.1.325):

- **Settings `env`.** Claude Code applies the `env` block of its settings files to the session, and some variables
  change the model after the guard has passed the call:
  - `CLAUDE_CODE_SUBAGENT_MODEL`, and `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` (the `Agent` tool's `model` is then
    ignored); `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` replaces the model of every Workflow `agent()` call;
    `ANTHROPIC_DEFAULT_*_MODEL` change what an alias (and so a row's `aliases`) means;
  - the **user** settings file is not filtered at all, and the agent can write it;
  - the **project** and **local** settings files: Claude Code drops `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL` from them
    with a warning, but not `CLAUDE_CODE_SUBAGENT_MODEL`, `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` or
    `ANTHROPIC_DEFAULT_*_MODEL`.

  Managed `env` is applied last and wins, but a pin there forces one model on every sub-agent, which does not fit a
  policy with several rows.

- **Other hooks.** A user or project `PreToolUse` hook can change a call's input after the guard has checked it.
  `allowManagedHooksOnly` (step 4, owner decision #298) closes this route only, not `env`.
- **The Workflow check reads text.** It refuses what it cannot read for sure, but it is not a JavaScript parser or
  a sandbox.

The hard stop is Claude Code's own managed model keys. They are read from managed settings only and applied where
the model is resolved, not on the text of a call: `availableModels` with `enforceAvailableModels` (and
`availableModelsMatch: "exact"`), `deniedModels`, `maxEffortLevel`, and `disableWorkflows` to turn Workflow off.
**Not yet tested here:** whether they bind sub-agents, Workflow agents and T3's sessions on every path must be
tried once. With them in place, the guard stays the layer that explains a refusal, checks T3's own tools and
catches mistakes early.

## Self-check (the drift check)

`model-guard --self-check` (or `model-guard-install self-check`) prints one `PROBLEM:` line for each of these and
exits 1:

- the kit is not installed, an installed file or link differs from the install record, or a row of the record is
  bad;
- the machine file or the policy cannot be used (then every checked call is refused);
- a managed settings file cannot be used: not root-owned, writable by group or others, a link, a folder above it
  unsafe, or not valid JSON;
- no managed `PreToolUse` entry is exactly the guard's: each entry is judged on its own, and the guard's entry must
  have a matcher that covers every checked tool **and** one hook that runs exactly
  `/usr/local/bin/model-guard || exit 2` with `"onFailure": "block"`, nothing more (no `timeout`, no `async`). A
  second entry that mentions the guard but is not its entry is reported too;
- a managed settings file sets `disableAllHooks`;
- `allowManagedHooksOnly` is not `true` in the managed settings (the later file wins, as in Claude Code), unless
  the machine file sets `"allowOtherHooks": true`.

The fork kit has no machine-wide drift check yet; when it has one, it runs this self-check. Until then, run it by
hand after an install or a settings change.

## Files on the host

| Path                                                     | What                                                               |
| -------------------------------------------------------- | ------------------------------------------------------------------ |
| `/usr/local/bin/model-guard`                             | link to `current/bin/model-guard` (the hook)                       |
| `/usr/local/sbin/model-guard-install`                    | link to `current/sbin/model-guard-install`                         |
| `/usr/local/libexec/model-guard/versions/<sha>/`         | the code of one release, with `MANIFEST.sha256` (root 755)         |
| `/usr/local/libexec/model-guard/current`                 | link to the release in use (switched in one step)                  |
| `/etc/claude-code/managed-settings.d/model-guard.json`   | the hook's managed settings entry, written by the kit (root 644)   |
| `/etc/model-guard/installed.tsv`                         | every installed path with its link target or hash (`--self-check`) |
| `/etc/model-guard/install.lock`                          | held while an installer runs; a second one is refused              |
| `/etc/fork-kit/machine.json`                             | written by the owner, never the kit: where the policy is           |
| the policy file                                          | written by the owner, never the kit                                |
| `/etc/claude-code/managed-settings.json`, other settings | the owner's, never written by the kit                              |

## Install (owner, as root)

`RELEASE_SHA` is a commit on the fork's protected `ci-release` branch; the bootstrap refuses anything else.

```
sha=$(git ls-remote https://github.com/FORK_OWNER/FORK refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/FORK_OWNER/FORK/$sha/fork-kit/model-guard/bootstrap.sh" -o /root/model-guard-bootstrap.sh && sh /root/model-guard-bootstrap.sh --repo FORK_OWNER/FORK --sha "$sha"
```

Upgrade: the same line again. Remove: `/usr/local/sbin/model-guard-install uninstall`. It deletes the kit's managed
settings file first, so no session runs the hook once the kit's files go. Any other settings file that still runs
`/usr/local/bin/model-guard || exit 2` refuses every checked call once the kit is gone: take that entry out too.

## Runs again, stops half-way, old or empty state

- **The hook runs again:** it keeps nothing between calls, so the same call with the same files gets the same answer.
- **The files change between calls:** every call reads the machine file and the policy again. A new mode or row
  counts from the next call. A file that is being rewritten and is half-written at that moment is invalid JSON, so
  that call is refused (write the policy to a temporary file and rename it over the old one).
- **Empty, old or invalid files:** a missing machine file, a missing `model-guard` section or `policyFile`, a
  missing, empty or invalid policy, a policy that others can write, or a machine file of a newer version: every
  checked call is refused, naming the file and the reason. An empty `allow` refuses every pick in `enforce`.
- **The installer runs again:** every step checks what is there and only fixes what differs; a second run changes
  nothing. Two installers at once: the second is refused (a lock on `/etc/model-guard/install.lock`).
- **The installer stops half-way:** the steps are check, folders, copy, self-test, switch, links, settings,
  record. A version folder is copied to a temporary name and renamed; a link is made under a temporary name with
  the installer's process id and renamed over the old one; the managed settings file is written to a hidden
  temporary file and renamed. Leftovers of a stopped run are cleared by the next one, and a half-copied or changed
  version is made again. The new copy must refuse a checked call when it finds no policy before it is switched
  in. On a fresh host, nothing runs the hook until the settings step; on an upgrade, the hook in use is always a
  whole version (old or new), and the self-check reports the difference from the record until a run finishes.
  Running the same line again finishes the job.
- **Old state:** a link at `/usr/local/bin/model-guard` that points elsewhere, a file where the kit expects a
  folder, a `current` that is not a link, or a managed settings folder that is not a root-owned folder, or a
  `model-guard.json` there that is not a plain file, is refused with a message, never overwritten. A version file
  that others can write, or that is a link, is made again. The kit never writes the policy, the machine file,
  `managed-settings.json` or any other settings file.
- **Old version folders** stay under `versions/` after an upgrade (a known gap: nothing removes them yet).

## Tests

`python3 -I -B fork-kit/model-guard/tests/guard.test.py`: every rule above with temporary files (the current user
stands in for root). Its red check breaks each refuse rule and each self-check rule in a copy of
`lib/model_guard.py` and shows the test for that rule then fails. Two tests read the server's code: the effort
option ids from `apps/server/src/usage/UsagePacingGate.ts` and the ultracode mapping from
`packages/shared/src/model.ts`.

`python3 -I -B fork-kit/model-guard/tests/install.test.py`: the installer and the self-check on a fake host in a
temporary folder, no root: a fresh install and an upgrade stopped after each step, run again, leftovers, a second
installer, old state, uninstall. Its red check breaks the installer's and the self-check's rules.
