# model-guard: keep agents' model picks inside a policy

`model-guard` is a Claude Code `PreToolUse` hook. It checks every call where an agent picks the model of another
agent, against a policy file that the person who runs the machine writes:

- T3's MCP tools `delegate_task`, `create_threads`, `t3_thread_launch` and `t3_thread_configure` (under any MCP
  server name);
- Claude Code's own `Agent` tool (`Task` in older versions).

Every other tool passes untouched. This is version 0 of the fork's model policy: a hook on the agent's side. A later
version checks the same picks inside the T3 server, where it can also see the session's own model.

Linux, Python 3 standard library only. No state: the hook reads the machine file and the policy again on every call.

## Rules

| Case                                                                           | `off`  | `advise`                     | `enforce`                 |
| ------------------------------------------------------------------------------ | ------ | ---------------------------- | ------------------------- |
| a tool the hook does not check                                                 | pass   | pass                         | pass                      |
| the machine file or the policy missing, unreadable, not root-owned, invalid    | refuse | refuse                       | refuse                    |
| the call names no model, or `inherit`                                          | pass   | pass + what enforce would do | refuse, listing the rows  |
| the model (or, for `Agent`, an alias) is in no row (of `driverKind`, if named) | pass   | pass + note                  | refuse, listing the rows  |
| the row has efforts and the call names none, or one not in the row             | pass   | pass + note                  | refuse, listing the rows  |
| `create_threads`: any thread breaks a rule above, or `threads` is empty        | pass   | pass + note                  | refuse, listing the rows  |
| `t3_thread_launch` without `modelSelection`                                    | pass   | pass + note                  | refuse                    |
| `t3_thread_configure` without `modelSelection` (it only reads)                 | pass   | pass                         | pass                      |
| the hook input is not a JSON object                                            | refuse | refuse                       | refuse                    |
| an allowed pick                                                                | pass   | pass + the row's `useFor`    | pass + the row's `useFor` |

A broken policy is refused in every mode, `off` too: the guard fails closed, and a file nobody can read is not a
decision to turn it off. "Pass" means the hook says nothing about permission: the call still goes through the
session's own permission rules (the hook never answers "allow"). A refusal names the problem, every allowed row
with its efforts, aliases and `useFor` note, and the policy file.

Where the effort is read:

- `Agent`: its `effort` field;
- the T3 tools: `options` of the target or `modelSelection`, either `{"effort": "high"}` or
  `[{"id": "effort", "value": "high"}]`, under any of the option ids `effort`, `reasoningEffort`,
  `reasoning_effort`, `reasoning`, `thinking_level` (the same list T3's usage pacing gate uses; a test keeps them in
  step). Every effort named must be allowed.

What v0 does not check: which provider instance or account a T3 call names (`providerInstanceId`, `environmentId`),
and the model of the session that makes the call.

### Decisions

- **Every pick names its model and effort.** The hook cannot see the parent's model or the provider's catalog, so
  nothing may be inherited: a T3 call without a model, or an `Agent` call without `model` (or with `inherit`), is
  refused.
- **`Agent` with `subagent_type: "fork"`** is checked like every other `Agent` call: it must name an allowed model
  and effort. Claude Code runs a fork on the parent's own model whatever the call says, so v0 cannot see what a fork
  really runs on. A fork from a session whose model is outside the policy is a known gap until the server-side
  version, which checks the session's model.
- **Aliases.** `Agent` takes short names (`opus`, `sonnet` and so on) that Claude Code maps to a model. The hook has
  no built-in list: a row's `aliases` are the only short names `Agent` may use for it, and the policy's author
  names them. `Agent` always runs on Claude, so it matches only rows of driver `claudeAgent`. The T3 tools take only
  the exact `model` of a row; an alias there is refused.
- **Driver.** A T3 call that names `driverKind` must match the row's `driver`. Without it, the model name alone
  decides. The same model name may be in rows of two drivers; one driver may not name a model or alias twice.

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

| Key             | Required | What                                                                          |
| --------------- | -------- | ----------------------------------------------------------------------------- |
| `mode`          | yes      | `off`, `advise` or `enforce`                                                  |
| `allow`         | yes      | the rows; an empty list refuses every pick in `enforce`                       |
| `driver`        | yes      | T3's driver kind (`claudeAgent` for Claude)                                   |
| `model`         | yes      | the exact model id T3 uses                                                    |
| `efforts`       | no       | the efforts allowed with it; without the key, the model has no effort control |
| `defaultEffort` | no       | one of `efforts`; shown in the refusal so the agent knows what to pick        |
| `aliases`       | no       | the short names the `Agent` tool may use for this row                         |
| `useFor`        | no       | what the model is for; shown to the agent with every pick of it (no refusal)  |

Rules of the file: JSON with no key given twice and no unknown key; lists not empty and without repeats; texts not
empty. The file must be owned by root, not writable by group or others, and not a link (the same rules as the
machine file), so an agent cannot widen its own policy.

## Turn it on (owner, as root)

1. Install the kit (below).
2. Write the policy, for example `/etc/fork-kit/model-policy.json`, root-owned, mode 644. Start with
   `"mode": "advise"`.
3. Name it in the machine file `/etc/fork-kit/machine.json` (see [common](../common/README.md)):

   ```json
   "model-guard": {
     "policyFile": "/etc/fork-kit/model-policy.json",
     "claudeSettings": ["/home/USER/.claude/settings.json"]
   }
   ```

   `claudeSettings` lists every Claude Code settings file on this machine that must run the hook; the self-check
   reports each one that does not.

4. Add the hook to each of those settings files (or to Claude Code's managed settings):

   ```json
   "hooks": {
     "PreToolUse": [
       {
         "matcher": "^(Agent|Task|mcp__.+__(delegate_task|create_threads|t3_thread_launch|t3_thread_configure))$",
         "hooks": [{ "type": "command", "command": "/usr/local/bin/model-guard || exit 2", "timeout": 10 }]
       }
     ]
   }
   ```

   `|| exit 2` matters: Claude Code lets a call through when a hook fails with any other exit code, so without it a
   missing or broken guard would let every pick through. The hook itself exits 2 on any error.

5. Run `model-guard --self-check` as each user. When advise mode has shown no surprises, set `"mode": "enforce"`.
   The change counts from the next call; nothing needs restarting.

## Self-check (the drift check)

`model-guard --self-check` (or `model-guard-install self-check`) prints one `PROBLEM:` line for each of these and
exits 1:

- the kit is not installed, or an installed file or link differs from the install record;
- the machine file or the policy cannot be used (then every checked call is refused);
- the machine file lists no `claudeSettings`;
- a listed settings file cannot be read, turns all hooks off, has no `PreToolUse` hook that runs
  `/usr/local/bin/model-guard`, has a matcher that misses one of the checked tools, or runs it without `|| exit 2`.

The fork kit has no machine-wide drift check yet; when it has one, it runs this self-check. Until then, run it by
hand after an install or a settings change.

## Files on the host

| Path                                             | What                                                               |
| ------------------------------------------------ | ------------------------------------------------------------------ |
| `/usr/local/bin/model-guard`                     | link to `current/bin/model-guard` (the hook)                       |
| `/usr/local/sbin/model-guard-install`            | link to `current/sbin/model-guard-install`                         |
| `/usr/local/libexec/model-guard/versions/<sha>/` | the code of one release, with `MANIFEST.sha256` (root 755)         |
| `/usr/local/libexec/model-guard/current`         | link to the release in use (switched in one step)                  |
| `/etc/model-guard/installed.tsv`                 | every installed path with its link target or hash (`--self-check`) |
| `/etc/fork-kit/machine.json`                     | written by the owner, never the kit: where the policy is           |
| the policy file                                  | written by the owner, never the kit                                |
| Claude Code settings files                       | edited by the owner, never the kit                                 |

## Install (owner, as root)

`RELEASE_SHA` is a commit on the fork's protected `ci-release` branch; the bootstrap refuses anything else.

```
sha=$(git ls-remote https://github.com/FORK_OWNER/FORK refs/heads/ci-release | cut -f1) && curl -fsSL "https://raw.githubusercontent.com/FORK_OWNER/FORK/$sha/fork-kit/model-guard/bootstrap.sh" -o /root/model-guard-bootstrap.sh && sh /root/model-guard-bootstrap.sh --repo FORK_OWNER/FORK --sha "$sha"
```

Upgrade: the same line again. Remove: `/usr/local/sbin/model-guard-install uninstall`, then take the hook out of
every settings file (a settings file that still runs `/usr/local/bin/model-guard || exit 2` refuses every checked
call once the kit is gone).

## Runs again, stops half-way, old or empty state

- **The hook runs again:** it keeps nothing between calls, so the same call with the same files gets the same answer.
- **The files change between calls:** every call reads the machine file and the policy again. A new mode or row
  counts from the next call. A file that is being rewritten and is half-written at that moment is invalid JSON, so
  that call is refused (write the policy to a temporary file and rename it over the old one).
- **Empty, old or invalid files:** a missing machine file, a missing `model-guard` section or `policyFile`, a
  missing, empty or invalid policy, a policy that others can write, or a machine file of a newer version: every
  checked call is refused, naming the file and the reason. An empty `allow` refuses every pick in `enforce`.
- **The installer runs again:** every step checks what is there and only fixes what differs; a second run changes
  nothing.
- **The installer stops half-way:** the steps are check, folders, copy, self-test, switch, links, record. A version
  folder is copied to a temporary name and renamed; a half-copied or changed one is made again. The new copy must
  refuse a checked call when it finds no policy before it is switched in. Running the same line again finishes
  the job.
- **Old state:** a link at `/usr/local/bin/model-guard` that points elsewhere, or a file where the kit expects a
  folder, is refused with a message, never overwritten. The kit never writes the policy, the machine file or a
  settings file.

## Tests

`python3 -I -B fork-kit/model-guard/tests/guard.test.py`: every rule above with temporary files (the current user
stands in for root). Its red check breaks each refuse rule in a copy of `lib/model_guard.py` and shows the test for
that rule then fails. One test reads `apps/server/src/usage/UsagePacingGate.ts` to keep the effort option ids in
step with the server.

`python3 -I -B fork-kit/model-guard/tests/install.test.py`: the installer and the self-check on a fake host in a
temporary folder, no root.
