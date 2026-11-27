# model-guard: keep agents' model picks inside a policy

`model-guard` is a Claude Code `PreToolUse` hook. It checks every call where an agent picks the model of another
agent, or could start or change one, against a policy file that the person who runs the machine writes. The checked
tools are one list in [lib/model_guard.py](lib/model_guard.py) (`CLAUDE_TOOLS`, `T3_TOOLS`); the hook's matcher and
the self-check are made from it:

- Claude Code's `Agent` tool (`Task` in older versions), its `Workflow` tool and its `RemoteTrigger` tool (cloud
  routines);
- T3's MCP tools `delegate_task`, `create_threads`, `t3_thread_launch`, `t3_thread_configure`,
  `t3_thread_transfer`, `t3_thread_send`, `t3_thread_send_attachments`, `schedule_task`, `t3_project_create` and
  `t3_project_update` (under any MCP server name).

Every other tool passes untouched. This is version 0 of the fork's model policy: a hook on the agent's side. The T3
server checks an agent's pick too, against its own setting (`modelPolicy`, **Settings → Fork → Model policy**, #731):
`delegate_task`, `create_threads`, `t3_thread_launch` and `t3_thread_configure`, and a thread fork
(`t3_thread_transfer`) by its backstop. Give both the same rows.

**Left to the T3 server once it is locked.** Those five tools (`SERVER_CHECKED_TOOLS`) pass the hook untouched while
T3's managed policy (`/etc/t3code/policy.json` on Linux, `/Library/Application Support/T3Code/policy.json` on macOS)
locks `modelPolicy`, so an agent cannot turn the server's check off and each pick is checked once. The hook reads that
file on every call, under the same rules as its own policy file, and fails closed: it keeps checking the five tools
when the file is missing, not root-owned, writable by group or others, behind a link, not JSON, not locked, locked
without a mode and rows, locked at a mode below the hook's (`advise` under an `enforce` hook, or `off`), or locked
with a row or effort no row of the hook's policy covers. `model-guard --self-check` prints which way it goes. The
lock step is in [docs/fork/model-policy.md](../../docs/fork/model-policy.md).

Linux, Python 3 standard library only. No state: the hook reads the machine file and the policy again on every call.

## Rules

| Case                                                                                               | `off`  | `advise`                     | `enforce`                 |
| -------------------------------------------------------------------------------------------------- | ------ | ---------------------------- | ------------------------- |
| a tool the hook does not check                                                                     | pass   | pass                         | pass                      |
| the machine file or the policy missing, unreadable, not root-owned, invalid, or of a newer version | refuse | refuse                       | refuse                    |
| the call names no model, or `inherit`                                                              | pass   | pass + what enforce would do | refuse, listing the rows  |
| the model (or, for `Agent` and `Workflow`, an alias) is in no row                                  | pass   | pass + note                  | refuse, listing the rows  |
| the call names no `driverKind` and one of the rows of that model does not allow it                 | pass   | pass + note                  | refuse, listing the rows  |
| a model selection (launch, configure, project default) names `driverKind` (see Driver below)       | pass   | pass + note                  | refuse, listing the rows  |
| the row has efforts and the call names none, or one not in the row                                 | pass   | pass + note                  | refuse, listing the rows  |
| the row has no `efforts` and the call names an effort (any, `max` too)                             | pass   | pass + note                  | refuse, listing the rows  |
| ultracode is on (see below) and the row does not list the effort `ultracode`                       | pass   | pass + note                  | refuse, listing the rows  |
| a T3 pick of a Claude model whose row lacks `ultracode` does not name the option `ultracode`       | pass   | pass + note                  | refuse, listing the rows  |
| a T3 option id given twice once trimmed, an empty id, or a value T3 drops (see below)              | pass   | pass + note                  | refuse, listing the rows  |
| a T3 pick of a row of another driver than Claude names an option id the row cannot read (below)    | pass   | pass + note                  | refuse, listing the rows  |
| `create_threads`: any thread breaks a rule above, or `threads` is empty                            | pass   | pass + note                  | refuse, listing the rows  |
| `t3_thread_launch` without `modelSelection`                                                        | pass   | pass + note                  | refuse                    |
| `t3_thread_configure` without `modelSelection` (it only reads)                                     | pass   | pass                         | pass                      |
| `Workflow`: a script the hook cannot read for sure, or an `agent()` call breaking a rule           | pass   | pass + note                  | refuse                    |
| `t3_thread_transfer` with `action: "fork"`                                                         | pass   | pass + note                  | refuse                    |
| `t3_project_create` / `t3_project_update` with a `defaultModelSelection` breaking a rule above     | pass   | pass + note                  | refuse, listing the rows  |
| `RemoteTrigger` with an action other than `list`, `get`, `list_runs`, `get_run_log`                | pass   | pass + note                  | refuse                    |
| a send, send-attachments, schedule or transfer call with a field that looks like a model choice    | pass   | pass + note                  | refuse                    |
| the hook input is not a JSON object                                                                | refuse | refuse                       | refuse                    |
| an allowed pick                                                                                    | pass   | pass + the row's `useFor`    | pass + the row's `useFor` |

A broken policy is refused in every mode, `off` too: the guard fails closed, and a file nobody can read is not a
decision to turn it off. A policy whose `version` is newer than the kit knows is refused the same way, with
"update the kit": it may use rules this kit cannot read. "Pass" means the hook says nothing about permission: the call still goes through the
session's own permission rules (the hook never answers "allow"). A refusal names the problem, every allowed row
with its efforts, aliases and `useFor` note, and the policy file.

**The owner's record.** Every refusal, and every note advise mode adds ("enforce would refuse ..."), is also written
as one line to the system log under the name `model-guard` (`journalctl -t model-guard`, or `/var/log/syslog`),
with the session id when the call has one. So advise mode leaves the owner a list of what enforce would change, not
only notes the agents saw. A log that cannot be written never changes the hook's answer.

Where the effort is read:

- `Agent`: its `effort` field; `Workflow`: the `effort` of each `agent()` call;
- the T3 tools: `options` of the target or `modelSelection`, either `{"effort": "high"}` or
  `[{"id": "effort", "value": "high"}]`, under any of the option ids `effort`, `reasoningEffort`,
  `reasoning_effort`, `reasoning`, `thinking_level` (the same list T3's usage pacing gate uses; a test keeps them in
  step), and on a row of another driver than Claude also the row's own `effortOptionIds` (see **Options of other
  drivers**). Every effort named must be allowed, and the effort must be named under the id the row's driver reads
  it from: for T3's built-in drivers the guard knows that id (below), and on a row of another driver than Claude
  also each of the row's `effortOptionIds`. So `{"thinking_level": "high"}` alone is refused for a Claude model, and
  `{"effort": "low"}` for a Codex model: the driver would run its default or inherited effort.
- **The effort id of each built-in driver** (#629). The guard pins the option id each of T3's built-in drivers
  reads its reasoning level from, and a test reads every one of them, and the list of built-in drivers, from T3's
  code, so a driver T3 adds or changes fails the test until the guard is updated:

  | Driver kind   | Effort id         | Read in                                                             |
  | ------------- | ----------------- | ------------------------------------------------------------------- |
  | `claudeAgent` | `effort`          | `apps/server/src/claudeModelOptions.ts`                             |
  | `codex`       | `reasoningEffort` | `apps/server/src/orchestration-v2/Adapters/CodexAdapterV2.ts`       |
  | `fx`          | `effort`          | `packages/provider-fx/src/server/acpSupport.ts`                     |
  | `grok`        | `reasoningEffort` | `packages/provider-grok/src/server/adapter.ts`                      |
  | `kiro`        | `reasoningEffort` | `packages/provider-kiro/src/server/adapter.ts`                      |
  | `muse`        | `reasoningEffort` | `packages/provider-muse/src/server/adapter.ts`                      |
  | `opencode`    | `variant`         | `packages/provider-opencode/src/server/adapter.ts`, `v2/adapter.ts` |
  | `pi`          | `thinking`        | `packages/provider-pi/src/server/adapter.ts`                        |

  `cursor`, `antigravity` and `acpRegistry` have no fixed id: the model (a Cursor model's parameter) or the agent
  (an ACP agent's `thought_level` config option) names it. A row of one of them, or of any driver not built into
  T3, that lists `efforts` must list the id in `effortOptionIds`; without it the guard cannot tell which level would
  run, so **every T3 pick of the row is refused** (fail closed; advise notes it), and `--self-check` names the row.
  `effortOptionIds` on a built-in driver's row adds ids to the pinned one: a pick must name both.

- **Agent options.** T3's OpenCode driver also reads `agent` (`turnPromptOptions` in its `adapter.ts`), and Kiro's
  `mode` (`KIRO_AGENT_CONFIG_ID` in its `acpSupport.ts`) switches Kiro's agent. An agent brings its own prompt, tools
  and settings, which the guard cannot see (T3 still sends the model, and the effort when the pick names it, so the
  agent cannot change those through the pick; it is refused anyway, fail closed). So an `opencode` row refuses
  `agent`, and a `kiro` row `mode`, in `otherOptionIds` and in `effortOptionIds` (#638 review), and a pick that
  names it is refused as an id the guard cannot read.
- **A child keeps only the options its pick names** (#643). Upstream T3 lays the parent's options under a pick when
  the instance and model are the same. In the fork, while T3's own model policy is on (the server setting
  `modelPolicy` with `mode` `advise` or `enforce`, step 7 below; `off`, the default, keeps upstream's way), a pick
  that names its model gives the child only the options the pick names, on every path that starts a child from a
  parent: `delegate_task` here and on a linked machine, and `create_threads` (`ownOptions` in
  `apps/server/src/fork/delegateChecks.ts`). So nothing the guard did not see (an agent, a Kiro `mode`, an ACP
  session mode, a Codex service tier or access program, a level) reaches the child. A pick with no model keeps the
  parent's options, as upstream does (the guard refuses such a T3 pick).
  `t3_thread_launch` and a project's default take their selection as given and inherit nothing.
  `t3_thread_configure` changes a thread that already runs, so this rule does not apply there: a running Kiro
  session keeps the agent it runs when the selection names no `mode` (`applyKiroAgent` in
  `packages/provider-kiro/src/server/adapter.ts`), so a guarded configure that names model and effort leaves that
  thread's own agent running.
- **Driver kinds are exact.** A row whose `driver` is one of T3's built-in kinds in another case or with spaces
  around it (`Codex`, `" codex"`) makes the policy invalid: a pick with no driver could run on that driver's
  instance, which reads other option ids than the row says (#638 review).
- **Options are read the way T3 reads them** (`packages/contracts/src/model.ts`, `ProviderOptionSelections`): T3
  trims option ids and text values (JavaScript's `trim`, which the hook copies) and takes the first entry of an id.
  So `{" effort": "max", "effort": "high"}` runs `max`. The hook trims the same way and refuses an id given twice
  once trimmed, an empty id, and a value that is neither a non-empty text nor `true`/`false` (T3 drops it and uses
  the default). This holds on every path that takes options: `delegate_task`, `create_threads`,
  `t3_thread_launch`, `t3_thread_configure` and a project's `defaultModelSelection`.
- **Options of other drivers** (#601). An ACP driver names its reasoning control by its own option id (the agent
  marks it `role: "reasoning"`, `packages/provider-acp/src/server/sessionConfig.ts`), which T3's usage pacing gate
  counts but the hook cannot see; and `t3_thread_launch` keeps option ids T3 does not know. So a T3 pick of a row
  whose driver is not `claudeAgent` may name only the five effort ids above, `ultracode`, and the ids its row lists:
  - `effortOptionIds`: the driver's own reasoning controls, read as efforts, so their values must be in `efforts`.
    A pick must name **each** of them (#618 review): T3's ACP adapter applies only the ids the agent's session
    exposes, so a pick that says `{"effort": "low"}` and leaves `thought_level` out runs the agent's default, or on
    `delegate_task` the parent's level (the given options are laid over the parent's). A row with this key must
    list `efforts`. A driver that reads one of the five shared ids (Codex reads `reasoningEffort`) should list that
    id here, so a pick must name it and not another shared id the driver ignores.
  - `otherOptionIds`: options that change neither the model nor its reasoning; passed unchecked, so list only ones
    you have read. Never list a mode whose choices repeat the reasoning levels (some agents' modes mirror their
    thought levels, `mirroredModeChoiceSets` in `sessionConfig.ts`): it sets the reasoning as well, so list it in
    `effortOptionIds`. The policy refuses T3's session mode `_t3/session-mode` here, and any id that contains
    `model`, `effort`, `reason`, `think`, `thought`, `ultracode`, `provider`, `driver` or `instance` (any case).

  A built-in driver's own effort id (the table above) is an effort id, never an `otherOptionIds` entry.
  Listing `_t3/session-mode` in `effortOptionIds` keeps a row's `delegate_task` picks from passing T3 for
  agents whose modes mirror their thought levels. T3 hides that descriptor for them, so its `delegate_task`
  check (`invalidOptionSelections`) refuses the id as "Unknown option". That fails closed, but such a row can
  then be picked only through `t3_thread_launch`, `t3_thread_configure` or a project's default.

  Any other id is refused, and the refusal names the two keys. A `claudeAgent` row takes only the ids T3's Claude
  driver reads (`effort`, `ultracode`, `contextWindow`, `fastMode`, `thinking`; a test keeps them in step with T3's
  bundled model list) and the five shared effort ids: a model selection has no driver and T3 runs it on the named
  instance, so on another driver's instance that offers the same model name an id such as `thought_level` would set
  the reasoning, unchecked. For the same reason `fastMode` and `thinking` must be `true` or `false` there (T3's
  Claude driver reads them only so, a test keeps the two in step): Pi, and some ACP agents such as Mistral Vibe,
  read `thinking` as their reasoning level, so a text value would set it unchecked. When one model name is in a
  Claude row and a row of another driver, a pick with no driver must suit both, which none can if the other row has `effortOptionIds`; use `delegate_task` with
  `driverKind` for that model.

- **Ultracode** is checked as what the server runs. The option `ultracode` (only `true` or `false`; any other value
  is refused, since T3 would use the default) runs the model in
  ultracode at effort xhigh, and the old effort value `"ultracode"` means xhigh with the option on (unless the option
  is given). Either way the guard counts the effort `ultracode` instead of `xhigh`, so
  `{"effort": "xhigh", "ultracode": true}` needs `ultracode` in the row's `efforts`, and `{"effort": "high",
  "ultracode": true}` needs both `high` and `ultracode`. A test reads this mapping from `packages/shared/src/model.ts`
  so the two stay in step.
- **A T3 pick that leaves ultracode out** can still run ultracode, two ways, and the hook sees neither:
  - **inherited from the parent.** `delegate_task` with the parent's model and provider instance starts from the
    parent thread's options and lays the given ones over them (`OrchestratorMcpService.ts`, `resolveTarget`: "Given
    values override inherited"). So `{"effort": "high"}` under a parent that runs ultracode starts a child that
    runs ultracode too. This route was open before the rule;
  - **the catalog default.** T3 fills in a Claude model's default for the option when a call leaves it out, and
    that default is on for a model whose default effort is ultracode
    (`apps/server/src/provider/ClaudeModelCatalog.ts`, `apps/server/src/claudeModelOptions.ts`). No bundled model
    has that default today; a custom or later one may.

  So a T3 pick of a `claudeAgent` model whose row does not list `ultracode` must name the option itself,
  `{"ultracode": false}` (or `[{"id": "ultracode", "value": false}]`), next to its effort. Rows of other drivers,
  and rows that allow `ultracode`, need not.

  **What a T3 call must pass in `enforce`** (`delegate_task`, `create_threads`, `t3_thread_launch`,
  `t3_thread_configure`, a project's `defaultModelSelection`): the model, and in `options` the effort and
  `"ultracode": false`. For example `{"model": "example-large-1-0", "options": {"effort": "high", "ultracode":
  false}}`, with no `driverKind` on a model selection. Agent instructions and briefs that make these calls should
  say so before `enforce` is turned on, or their first call is refused (the refusal says what to add). `Agent` and
  `Workflow` calls have no ultracode option and need nothing more.

  **Models with no ultracode option.** T3 offers the option only to Claude models whose effort list has
  `ultracode` (`ClaudeModelCatalog.ts`, `withUltracodeToggle`), and refuses it for the others ("Unknown option
  ultracode"). The row of such a model says `"ultracodeOption": false`: a T3 pick of it then need not name
  `ultracode` (and cannot). The row may not list the effort `ultracode`, and only `claudeAgent` rows take the key.
  Set it only for a model T3 really offers no ultracode option: on a model that has one, a pick that leaves the
  option out can inherit ultracode from its parent.
  - **The self-check watches such rows** (#600). T3 reloads its model list (the model manifest) from upstream
    about every hour, so a model can gain the ultracode option with no change on the machine. A `false` row then
    lets a pick that leaves the option out run ultracode (inherited from the parent, or the model's default). Name
    T3's model list in the machine file (`t3ModelManifests`, "Turn it on" step 2): `--self-check` reads it and
    reports each `false` row whose model now has the option (the effort choice `ultracode` or the option itself),
    has no option list, or is not in the list, and says so when a list cannot be read or none is named. It is a
    report, not a control: the hook keeps passing such picks until the row is fixed, so run the self-check after
    every T3 update. The user T3 runs as, and so its agents, can rewrite T3's cache and hide a stale row from the
    report; that is acceptable for a report, and the copy of the shipped list (step 2, root's) still shows it. When in doubt, leave the key out: the default is the safe side.
  - **Custom models with no options:** T3 checks no options for a model it has no option list for, so it neither
    drops an inherited `ultracode` nor refuses `ultracode: false`. Do not set `"ultracodeOption": false` for one;
    name `"ultracode": false` in its picks instead.
  - A pick that names `"ultracode": false` passes on a `false` row too, so briefs may keep naming it everywhere.

What v0 does not check:

- which provider instance or account a T3 call names (`instanceId`, `providerInstanceId`, `environmentId`), and the
  model of the session that makes the call. So a model only in one driver's rows that another driver's instance
  also offers runs there with that driver's default reasoning: the effort passes the check under the row's id
  (`effort` for a Claude or Fx row, `variant` for an OpenCode row), and the other driver may ignore it (the guard
  only keeps such a pick from naming an id the row's driver does not read). Pi and OpenCode share the
  `provider/model` form of model names, so this can happen between them.
- on a T3 older than #643, or with T3's `modelPolicy` off, what the child takes over from its parent: such a T3
  keeps the parent's options (an OpenCode `agent`, a Kiro `mode`, an ACP session mode, a service tier) on a child
  whose pick names the same instance and model. With the policy on, the child keeps only the options its pick
  names, on every path (see **A child keeps only the options its pick names**).
- that the model offers each allowed level, for rows of drivers other than Claude: Kiro, Grok and OpenCode v2 skip
  a level the model does not list and run their default. `delegate_task` is safe (T3 refuses such a value);
  launch, configure and a project's default are not. List in `efforts` only the levels the model offers. T3's
  model list has option lists only for Claude models, so the self-check compares only `claudeAgent` rows (#643).
- that a row's `effortOptionIds` are the ids its driver really reads, for a driver whose id the guard does not
  pin (Cursor, Antigravity, the ACP registry, drivers not built into T3): the owner vouches for them by listing them.
- what an `otherOptionIds` option does: the owner vouches for it by listing it.
- between two self-checks, a T3 model list that changed under an `"ultracodeOption": false` row, or that no longer
  offers a level a Claude row allows.

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
- **`t3_project_create` and `t3_project_update`** may set a project's `defaultModelSelection`, which later
  threads of the project start on. When the call gives one it is checked like a `t3_thread_launch` pick; without it
  the call passes (it changes no model).
- **`RemoteTrigger`** creates, changes and fires Claude Code's cloud routines, which run on a model the hook cannot
  see or check. Only its reading actions (`list`, `get`, `list_runs`, `get_run_log`) pass in `enforce`.
- **`t3_thread_send`, `t3_thread_send_attachments` and `schedule_task`** run on a model that was already picked
  (the thread's, or the calling thread's or project's for a scheduled task) and take no model of their own. A
  field whose name looks like a model choice on them (or on `t3_thread_transfer`) is refused, because the hook
  cannot tell what a future server would do with it.
- **Aliases.** `Agent` and `Workflow` take short names (`opus`, `sonnet` and so on) that Claude Code maps to a
  model. The hook has no built-in list: a row's `aliases` are the only short names they may use for it, and the
  policy's author names them. Both always run on Claude, so they match only rows of driver `claudeAgent`. The T3
  tools take only the exact `model` of a row; an alias there is refused.
- **Driver.** Only a `delegate_task` or `create_threads` target has a real `driverKind`: it must match the row's
  `driver`, and T3 refuses an instance of another driver there. A model selection (`t3_thread_launch`,
  `t3_thread_configure`, a project's `defaultModelSelection`) has no driver: T3 drops a `driverKind` key there and
  runs the pick on the named instance, so the hook refuses a model selection that names `driverKind` (even the
  right one) and never reads it. A pick with no driver is checked against **every** row that names the model, the
  ultracode rule included: all of them must allow it. Example: rows `{driver A, model X,
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

| Key               | Required | What                                                                         |
| ----------------- | -------- | ---------------------------------------------------------------------------- |
| `version`         | no       | `1` (the default); a newer one is refused until the kit is updated           |
| `mode`            | yes      | `off`, `advise` or `enforce`                                                 |
| `allow`           | yes      | the rows; an empty list refuses every pick in `enforce`                      |
| `driver`          | yes      | T3's driver kind (`claudeAgent` for Claude)                                  |
| `model`           | yes      | the exact model id T3 uses                                                   |
| `efforts`         | no       | the efforts allowed with it; without the key, a call may name no effort      |
| `defaultEffort`   | no       | one of `efforts`; shown in the refusal so the agent knows what to pick       |
| `aliases`         | no       | the short names the `Agent` tool may use for this row                        |
| `useFor`          | no       | what the model is for; shown to the agent with every pick of it (no refusal) |
| `ultracodeOption` | no       | `false`: T3 offers this Claude model no ultracode option (default `true`)    |
| `effortOptionIds` | no       | other drivers only: their own reasoning option ids, checked as efforts       |
| `otherOptionIds`  | no       | other drivers only: option ids that change neither model nor reasoning       |

Rules of the file: JSON with no key given twice and no unknown key; lists not empty and without repeats; texts not
empty. Option ids have no spaces around them (T3 trims them) and are never `ultracode`, and an effort id (one of
the five, the row's own, or its built-in driver's) is never an `otherOptionIds` id. `effortOptionIds` needs
`efforts`; `otherOptionIds` never holds `_t3/session-mode` or an id naming a model or reasoning control; neither
key holds an `opencode` row's `agent` or a `kiro` row's `mode` (see **Agent options**). A `driver` that is a built-in
kind in another case or with spaces around it is refused (see **Driver kinds are exact**). The file must be owned by root, not writable by group or others, and not a link (the same rules as the
machine file), so an agent cannot widen its own policy.

## Paste-ready install: the fork's two start rows, in advise mode

The short way through "Turn it on" below, with the fork's start rows (#126): Opus 5.5 and Fable 5.2, each at
`medium`, `high` or `xhigh` (default `high`). Run it as root on the agent host. Fill in the two lines at the top first:
`FORK` is the fork's `owner/repo` on GitHub, `T3_HOME` is the folder T3 keeps its state in on this machine.

```sh
FORK=FORK_OWNER/FORK
T3_HOME=/path/to/t3-home
umask 022 && mkdir -p /etc/fork-kit
# 1. The policy, in advise mode: every pick runs; one outside the rows gets a note and a log line.
#    A policy file already there is kept (`set -C`: the write fails instead of replacing it).
( set -C; cat > /etc/fork-kit/model-policy.json ) <<'JSON' || echo "kept the policy already in /etc/fork-kit/model-policy.json"
{
  "mode": "advise",
  "allow": [
    {
      "driver": "claudeAgent",
      "model": "claude-opus-5-5",
      "efforts": ["medium", "high", "xhigh"],
      "defaultEffort": "high",
      "aliases": ["opus"],
      "useFor": "most work: building, routine work, review lenses"
    },
    {
      "driver": "claudeAgent",
      "model": "claude-fable-5-2",
      "efforts": ["medium", "high", "xhigh"],
      "defaultEffort": "high",
      "aliases": ["fable"],
      "useFor": "creative solutions and direction checks: design options, stuck audits, root causes others missed; it costs more, so not routine work"
    }
  ]
}
JSON
# 2. Name it in the machine file; any other section, and any other key of this one, stays.
T3_HOME="$T3_HOME" python3 - <<'PY'
import json, os
path = '/etc/fork-kit/machine.json'
data = json.load(open(path)) if os.path.exists(path) else {'version': 1}
data.setdefault('model-guard', {}).update(
    policyFile='/etc/fork-kit/model-policy.json',
    t3ModelManifests=[os.environ['T3_HOME'] + '/userdata/model-manifest.json'])
with open(path + '.tmp', 'w') as out:
    json.dump(data, out, indent=2)
    out.write('\n')
os.replace(path + '.tmp', path)
PY
# 3. Install the kit (the line from "Install" below), then check it.
sha=$(git ls-remote "https://github.com/$FORK" refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/$FORK/$sha/fork-kit/model-guard/bootstrap.sh" -o /root/model-guard-bootstrap.sh && sh /root/model-guard-bootstrap.sh --repo "$FORK" --sha "$sha"
model-guard --self-check
```

The self-check names anything left to do (for example `allowManagedHooksOnly`, step 4 below, an owner choice).
Then set the same rows in T3 (step 7 below). Watch what advise mode would refuse with `journalctl -t model-guard`
(and T3's server log: "model policy pick" for each agent pick as asked, which also counts each model's picks, and
"model policy paced pick" for the model a `delegate_task` ran on when the usage pacer moved it).

**Later, enforce** (when advise mode has shown no surprises; steps 5 and 6 below say why each part is there):

```sh
sed -i 's/"mode": "advise"/"mode": "enforce"/' /etc/fork-kit/model-policy.json
model-guard --self-check   # prints the Claude Code model keys to put in managed-settings.json; add them, run again
```

and in T3: **Settings → Fork → Model policy**, mode Enforce. Back to advise: the same `sed` the other way round, and
mode Advise in T3; both count from the next call.

## Turn it on (owner, as root)

1. Write the policy, for example `/etc/fork-kit/model-policy.json`, root-owned, mode 644, in folders that are
   root-owned and not writable by others all the way up to `/`. Start with `"mode": "advise"` (or `"off"`). Not in
   `/etc/model-guard`: an uninstall removes that folder, so a policy there is refused, also when the path reaches
   it through a link, another name of `/etc` (`/private/etc` on macOS) or another spelling on a case-insensitive
   file system.
2. Name it in the machine file `/etc/fork-kit/machine.json` (see [common](../common/README.md)):

   ```json
   "model-guard": {
     "policyFile": "/etc/fork-kit/model-policy.json",
     "t3ModelManifests": ["/srv/example/t3-home/userdata/model-manifest.json"]
   }
   ```

   | Key                | Required | What                                                                    |
   | ------------------ | -------- | ----------------------------------------------------------------------- |
   | `policyFile`       | yes      | the policy; an absolute path                                            |
   | `allowOtherHooks`  | no       | `true` accepts user and project hooks next to the guard (default false) |
   | `t3ModelManifests` | no       | T3's model lists the self-check reads (#600); absolute paths            |

   `t3ModelManifests` is needed when the policy has an `"ultracodeOption": false` row or a `claudeAgent` row with
   `efforts` (#643), which is nearly every policy, the example one too (the self-check reports it missing
   otherwise; mode `off` needs none). Name T3's cached list, `model-manifest.json` in T3's state folder
   (`userdata/` under T3's home, or `dev/` for a dev T3, `apps/server/src/config.ts`; T3 reloads it about every
   hour, `apps/server/src/provider/ModelManifest.ts`), for each T3 on the machine, and a copy of the list that T3
   version ships (`apps/server/src/provider/model-manifest.json`). **Copy the shipped list again after every T3
   update**: T3 uses the list it ships while its cache is missing, older than that list or fails T3's own stricter
   schema check (unique model names, each model and profile it names present), and the self-check reads the cache
   more loosely, so it may read a cache T3 ignores. T3 also ignores errors writing its cache, so the list it runs can
   be newer than the file. The self-check reports a row that any named list finds stale. Each must be a plain file, not a link. Only the
   self-check reads them; a call's answer never depends on them.
   **Upgrade the kit before adding the key**: an older kit refuses an unknown key, and so every checked call
   (fail closed).

3. Only now install the kit (below). Write the policy and the machine file first: from the installer's settings
   step on, every session runs the hook, and with no usable policy the hook refuses every checked call in every
   mode (`off` included, since the mode lives in the policy). On a machine with running agents, every `Agent`,
   delegate, thread send and schedule call would be refused until steps 1 and 2 are done. So the installer refuses
   a first install (no `model-guard.json` yet) while no valid policy loads, before it writes anything, and checks
   again with the new version right before it writes `model-guard.json`.

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
           "matcher": "^(Agent|Task|Workflow|RemoteTrigger|mcp__.+__(delegate_task|create_threads|t3_thread_launch|t3_thread_configure|t3_thread_transfer|t3_thread_send|t3_thread_send_attachments|schedule_task|t3_project_create|t3_project_update))$",
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

5. Run `model-guard --self-check`. When advise mode has shown no surprises (`journalctl -t model-guard`), put
   Claude Code's own model keys in `managed-settings.json` (see
   [Claude Code's model keys](#claude-codes-model-keys)). The self-check prints the keys to put there.
6. **Try one T3 session with these keys before you rely on them.** They were tried live only with Claude Code on its
   own. T3 may send Claude Code a model id other than the policy's (a `[1m]` suffix, an alias's API id) and maps
   some efforts itself (for example `xhigh` runs as `max` on some models), so start a T3 thread on each Claude row
   and check that it runs. Make the delegate instructions name `"ultracode": false` (see
   [Rules](#rules)), then set `"mode": "enforce"`. The change counts from the next call; nothing needs restarting.
7. In each T3 on the machine, set its own model policy with the same rows: **Settings → Fork → Model policy**, mode
   `advise`, then **Edit allowed models** (with no rows saved it starts from the fork's start rows, the two of step 1 in the short way above)
   and save. T3 then checks each agent pick on the server (a person's own pick is never refused; Settings marks one
   outside the rows), and a child whose pick names its model keeps only the options the pick names (see
   [Rules](#rules)). Off again: mode `off`. It counts from the next call. A T3 whose settings cannot be read refuses
   every agent pick, also with the mode `off` (it cannot see the mode), and so does one whose settings.json does not
   parse (the server log says so).

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

  Managed `env` is applied last and wins. The managed model keys below set these variables to empty there, which
  Claude Code treats as unset, so a value in any other settings file no longer counts.

- **Other hooks.** A user or project `PreToolUse` hook can change a call's input after the guard has checked it.
  `allowManagedHooksOnly` (step 4, owner decision #298) closes this route only, not `env`.
- **The Workflow check reads text.** It refuses what it cannot read for sure, but it is not a JavaScript parser or
  a sandbox.

The hard stop is Claude Code's own managed model keys (next section). With them in place, the guard stays the layer
that explains a refusal, checks T3's own tools and catches mistakes early.

## Claude Code's model keys

Claude Code reads these keys from managed settings only and applies them where the model is resolved, not on the
text of a call. Tried live once with Claude Code 2.1.325 in print mode, with a managed settings file and the model
each request used read from the request:

- `availableModels` with `enforceAvailableModels: true` binds the main session, `Agent` sub-agents and Workflow
  `agent()` calls: none of them ran on a model outside the list. An alias that `ANTHROPIC_DEFAULT_*_MODEL` maps to
  another model does not get past it either.
- `maxEffortLevel` caps the effort of all three ("Effort 'max' exceeds the cap ...; using 'high'");
  `deniedModels` and `disableWorkflows` bind them too.
- Managed `env` wins over the other settings files, and an empty `CLAUDE_CODE_SUBAGENT_MODEL` is the same as unset.

So in `enforce` the self-check asks for these keys, made from the policy's `claudeAgent` rows, and prints the exact
JSON to put in `managed-settings.json` when they are missing or differ (the installer never writes the owner's
settings). Several managed files: a later file's key wins, and `env` is merged by name. Claude Code may instead
join `availableModels` lists across the files, so the self-check wants the last list to be the rows' models and
no earlier list to name any other. For the example policy:

```json
{
  "availableModels": ["example-deep-2-0", "example-large-1-0", "example-older-0"],
  "enforceAvailableModels": true,
  "maxEffortLevel": "xhigh",
  "env": {
    "CLAUDE_CODE_SUBAGENT_MODEL": "",
    "CLAUDE_CODE_SUBAGENT_MODEL_FORCE": "",
    "CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL": "",
    "CLAUDE_CODE_SUBAGENT_EFFORT": ""
  }
}
```

A row's alias that is one of Claude Code's own (`opus`, `sonnet`, `haiku`, `fable`) adds
`ANTHROPIC_DEFAULT_<ALIAS>_MODEL` set to that row's model, so the alias means what the policy says, whatever Claude
Code's built-in map is after an update. Other aliases are only the guard's. `maxEffortLevel` is the highest effort
of any `claudeAgent` row (`ultracode` counts as `xhigh`), so it caps but cannot tell rows apart: the guard still
checks each row's own efforts. (Claude Code also takes a cap per model, `modelSettings`; the self-check does not
ask for it yet, as it was not tried live.) `deniedModels` and `disableWorkflows` are not asked for: the allow list already
covers the first, and the guard checks Workflow. In `advise` and `off` the self-check does not ask for them, because
they bind in every mode.

Not covered: Claude Code also takes managed settings from a remote server or an OS policy (MDM), and those win over
the files. The self-check reads only the files, so on such a machine check those sources too. T3's own Claude
sessions were not tried live; they run the same Claude Code, so the same keys should apply, but try one
first ("Turn it on", step 6).

## Self-check (the drift check)

`model-guard --self-check` (or `model-guard-install self-check`) prints one `PROBLEM:` line for each of these and
exits 1:

- the kit is not installed, an installed file or link differs from the install record, or a row of the record is
  bad;
- the machine file or the policy cannot be used (then every checked call is refused);
- a managed settings file cannot be used: not root-owned, writable by group or others, a link, a folder above it
  unsafe, or not valid JSON;
- no managed `PreToolUse` entry is exactly the guard's: each entry is judged on its own, and the guard's entry must
  have exactly the kit's matcher (an old kit's matcher that misses a newly checked tool is reported until
  `model-guard-install install` rewrites it) **and** one hook that runs exactly
  `/usr/local/bin/model-guard || exit 2` with `"onFailure": "block"`, nothing more (no `timeout`, no `async`). A
  second entry that mentions the guard but is not its entry is reported too;
- a managed settings file sets `disableAllHooks`;
- `allowManagedHooksOnly` is not `true` in the managed settings (the later file wins, as in Claude Code), unless
  the machine file sets `"allowOtherHooks": true`;
- a policy row with `"ultracodeOption": false` whose model T3's model list (`t3ModelManifests`) now offers the
  ultracode option, gives no option list, or does not list; a named list that cannot be read (missing, a link, not a
  plain file, over 16 MiB, not JSON or not a version 1 model list); or no list named (not in mode `off`);
- a policy row with `efforts` whose driver reads its effort from an id the guard does not pin and that lists no
  `effortOptionIds` (#629; not in mode `off`): every T3 pick of it is refused until the id is listed;
- a `claudeAgent` row whose `efforts` hold a level its model does not offer in T3's model list (`t3ModelManifests`;
  the choices of the `effort` select, `xhigh` when that select offers `ultracode` (T3 puts `xhigh` in its place),
  and `ultracode` when the model has the ultracode option), or whose model the list gives no option list (#643;
  not in mode `off`): outside `delegate_task` such a level runs the model's default. The model is found as T3
  finds it: the entry whose slug it is, else the first whose alias it is. With several lists, a level must be
  offered in each. A row with `efforts` and no list named, a named list that cannot be read, and a list that has
  none of the rows' models (likely the wrong file) are reported too. One model that is not in a list that has the
  others (a custom model) is a `note:` line, not a problem;
- in `enforce`: Claude Code's model keys are missing or differ from the policy (see
  [Claude Code's model keys](#claude-codes-model-keys)); the line holds the JSON to put in `managed-settings.json`.

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
| the system log, name `model-guard`                       | one line per refusal or advise note (`journalctl -t model-guard`)  |
| `/etc/fork-kit/machine.json`                             | written by the owner, never the kit: where the policy is           |
| the policy file                                          | written by the owner, never the kit                                |
| `/etc/claude-code/managed-settings.json`, other settings | the owner's, never written by the kit                              |

## Install (owner, as root)

`RELEASE_SHA` is a commit on the fork's protected `ci-release` branch; the bootstrap refuses anything else.

```
sha=$(git ls-remote https://github.com/FORK_OWNER/FORK refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/FORK_OWNER/FORK/$sha/fork-kit/model-guard/bootstrap.sh" -o /root/model-guard-bootstrap.sh && sh /root/model-guard-bootstrap.sh --repo FORK_OWNER/FORK --sha "$sha"
```

Upgrade: the same line again; it removes the old version folders once the new one is in use. Remove:
`/usr/local/sbin/model-guard-install uninstall`. Its steps: the kit's managed settings file (first, so no session
runs the hook once the kit's files go; a folder in its place is reported and left alone), the hook link, the
install record, the old version folders, the installer link, `/usr/local/libexec/model-guard`, and last the lock
file. Any other settings file that still runs `/usr/local/bin/model-guard || exit 2` refuses every checked call
once the kit is gone: take that entry out too.

## Runs again, stops half-way, old or empty state

- **The hook runs again:** it keeps nothing between calls, so the same call with the same files gets the same answer.
- **The files change between calls:** every call reads the machine file and the policy again. A new mode or row
  counts from the next call. A file that is being rewritten and is half-written at that moment is invalid JSON, so
  that call is refused (write the policy to a temporary file and rename it over the old one).
- **Empty, old or invalid files:** a missing machine file, a missing `model-guard` section or `policyFile`, a
  missing, empty or invalid policy, a policy that others can write, or a machine file of a newer version: every
  checked call is refused, naming the file and the reason. An empty `allow` refuses every pick in `enforce`.
- **T3's model list changes, vanishes or is half-written** (#600): the self-check reads each named list again on
  every run and keeps nothing. A missing list, one being rewritten at that moment (invalid JSON), or one of another
  shape is reported, never taken as clean. An old machine file without `t3ModelManifests` is reported when the
  policy has a `false` row or, since #643, a `claudeAgent` row with `efforts`; an older kit given a machine file
  with the key refuses every checked call. A model that loses a level in a reload is reported on the next run.
- **Old policy files** stay valid. Since #601, a T3 pick of a row of another driver than Claude that names an option
  id other than the five effort ids and `ultracode` is refused in `enforce` (noted in `advise`); the refusal names
  `effortOptionIds` and `otherOptionIds`. Since #629 an old policy still parses, but some picks it passed are
  refused, each time the same, naming the key or option to add (advise notes it; the self-check names the rows):
  - a row of `codex`, `opencode`, `pi`, `grok`, `kiro`, `muse` or `fx` wants the effort under its driver's id (the
    table above): `{"effort": "low"}` for a Codex row, which Codex ignores, is refused; name `reasoningEffort`. A
    row that already lists that id in `effortOptionIds` (the old advice for Codex) works as before;
  - a row with `efforts` of `cursor`, `antigravity`, `acpRegistry` or a driver not built into T3, without
    `effortOptionIds`, refuses every T3 pick: list the id its driver reads;
  - an `opencode` row with `agent`, a `kiro` row with `mode`, or a row with its driver's effort id in
    `otherOptionIds`, an `opencode` row with `agent` or a `kiro` row with `mode` in `effortOptionIds`, or a row
    whose `driver` is a built-in kind in another case (`Codex`) makes the policy invalid: **every** checked call is
    refused, naming the row and the key, Claude calls included. So before updating the kit, run the new kit's
    self-check from its checkout, as root (`python3 -I -B fork-kit/model-guard/bin/model-guard --self-check`): a
    `the policy cannot be used` line names what to fix first;
  - on a `claudeAgent` row: `thinking` or `fastMode` with a text value is refused. Since #618, three old
    Claude pick shapes are refused too, because none names `effort`, the one id T3's Claude driver reads:
    `{"ultracode": true}` alone (it passed before #618 on a row allowing `ultracode`), and an effort named only
    under `thinking_level` or only under `reasoningEffort`. Name `"effort"` next to them.
- **The installer runs again:** every step checks what is there and only fixes what differs; a second run changes
  nothing. Two installers at once: the second is refused (a lock on `/etc/model-guard/install.lock`). An installer
  that opened the lock file just before an uninstall removed it is refused too ("run again").
- **The installer stops half-way:** the steps are check, folders, copy, self-test, switch, links, settings,
  record, prune. A version folder is copied to a temporary name and renamed; a link is made under a temporary name with
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
- **Old version folders** are removed by the prune step once the record names the new version; a stop before it
  leaves them, and the next run removes them. A stray file or link in `versions/` is removed, never followed.
- **The uninstall stops half-way:** run `/usr/local/sbin/model-guard-install uninstall` again; the installer link
  stays until its version folder goes. After a stop between the installer link and the lock, run
  `/usr/local/libexec/model-guard/current/sbin/model-guard-install uninstall` if it is still there; when only
  `/etc/model-guard/install.lock` is left, remove that file by hand. A managed settings file of the owner's is never
  touched.

## Tests

`python3 -I -B fork-kit/model-guard/tests/guard.test.py`: every rule above with temporary files (the current user
stands in for root). Its red check breaks each refuse rule and each self-check rule in a copy of
`lib/model_guard.py` and shows the test for that rule then fails. The `Contract` tests read the server's code: the
effort option ids from `apps/server/src/usage/UsagePacingGate.ts`, the ultracode mapping from
`packages/shared/src/model.ts`, T3's bundled model list (`apps/server/src/provider/model-manifest.json`, one model
with the effort `ultracode` and one without) with its cache path and shape in `ModelManifest.ts`, Claude's effort and
true/false ids in `apps/server/src/claudeModelOptions.ts`, and each built-in driver (`builtInDrivers.ts`), its kind
and the file where it reads its effort (#629), failing on any entry of the list it cannot read (a spread, a call),
and that T3's bundled list has option lists only for Claude models. The fork's CI checks out only the T3 files these tests read; three jobs
of `.github/workflows/fork-tests.yml` (`workflows`, `action_tests` and `kit_tests`) each have a sparse list that
names them.

`python3 -I -B fork-kit/model-guard/tests/install.test.py`: the installer and the self-check on a fake host in a
temporary folder, no root: a fresh install and an upgrade stopped after each step, a first install without a
policy, run again, leftovers, a second installer and a removed lock file, old state (a link, a folder or file root
does not own), pruning, an uninstall stopped after each step, Claude Code's model keys and a stale
`"ultracodeOption": false` row (#600) and a level a model does not offer (#643) in the self-check. Its
red check breaks the installer's and the self-check's rules.
