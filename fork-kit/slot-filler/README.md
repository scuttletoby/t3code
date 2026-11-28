# slot-filler

Fills free agent slots from a queue folder, by code: a timer runs `slot-filler` every minute, it counts the live
children of one parent (orchestrator) thread per machine from T3 itself, and starts the highest-priority ready
items until every machine is full. Slots that no queue item fits take standing work from a backlog folder, and
slots that stay idle with nothing ready for them make the filler tell the parent. Part of fork issue #870 ("orchestrator logic as software, not prose").

One file, Python 3 standard library only: `bin/slot-filler`. Tests: `python3 -I -B fork-kit/slot-filler/tests/filler.test.py`.

## How it starts a task

T3 has no command a script can call to start a delegated task: `delegate_task` is an MCP tool, and its caller must
be a thread with an active run (the parent). The fork adds one small route that runs the server's own
`delegate_task` as the parent thread (`apps/server/src/fork/fillerDelegate.ts`, the same path the lens starter
uses):

- `POST /api/fork/delegate` `{parentThreadId, requestId, wake?, task}` where `task` is `delegate_task`'s input
  (task text, title, `target: {environmentId?, providerInstanceId, model, options}`, role, isolation). It returns
  `{state: "started", taskId, childThreadId}` or `{state: "refused", code, message}`. When the parent has no run,
  the route sends it one short queued message (once per parent, request id and half hour) so it gets one, then
  starts the task in that run. All of `delegate_task`'s checks apply (slot gate, environments, providers). The route looks the
  `requestId` up first: one that already started returns its task whatever the parent's state now, so asking
  again never starts a second one. One whose start was refused for good answers `request_rejected` (use a new id).
  A remote start also needs a parent run, so an idle parent takes one short wake turn per start (this uses usage).
  With `lookupOnly: true` it only answers what the id started (`not_found` when nothing yet), and never starts.
- `POST /api/fork/delegate/notify` `{parentThreadId, requestId, text}` sends the parent one message the way the
  wake is sent (queued after its run, once per request id and half hour, through the slot gate): `{state: "sent"}`
  or `{state: "refused", code, message}`. The idle alarm uses it.
- `GET /api/fork/delegate?parentThreadId=...` answers `{children, parentSettled}`: the parent's children
  `{taskId, title, status, live, environmentId, childThreadId, providerInstanceId, model}`, and whether the parent
  was settled by hand.

Both need a session token with the scopes `orchestration:read` and `orchestration:operate` (`POST`), or
`orchestration:read` (`GET`). Issue one with the running server's binary:
`t3 auth session issue --scopes orchestration:read,orchestration:operate --label slot-filler --token-only`, and keep
it in a file only you can read (`server.tokenFile`, mode `600`: the filler refuses a file group or others can read,
without printing it).

## Queue

`<queueDir>/<id>.json`, one item per file:

```json
{
  "id": "fix-login-42",
  "title": "Fix the login retry",
  "kind": "writer",
  "brief": "/abs/path/brief.md",
  "priority": 5,
  "readyWhen": { "type": "pr-green", "pr": 41 },
  "machines": ["main", "spare"],
  "account": "claudeAgent",
  "model": "claude-opus-5-5",
  "effort": "high",
  "role": "worker",
  "isolation": "worktree"
}
```

- `id` equals the file name; letters, digits, `.`, `_`, `-`. `kind`: `writer`, `lens` or `other`.
- `readyWhen`: `{"type": "none"}` (the default), `{"type": "pr-green", "pr": n}` (the newest run of the CI check
  `ciCheck` on the PR's current head succeeded; the PR is open), or `{"type": "pr-merged", "pr": n}`. A `head`
  field pins `pr-green` to one head.
- `machines`: allowed machines, in order of preference (default: every machine in the config, in config order).
  `account`, `model`, `effort` default from the config's `defaults`.
- `account` (in an item or in `defaults`): one account, or a list in order of preference, e.g.
  `["claudeAgent_claude_annemette", "claudeAgent"]`. On each machine the filler uses the first account that passes
  the usage rules below; when none does, the item waits and the log line gives each account's reason. The account
  used is in the started record (`account`) and the log line ("started `<id>` on `<machine>` as `<account>`").
- The task text is "Act as the `<kind>` sub-agent for this task. Read `<brief>` fully and do what it says. It is
  your whole brief." (`taskTemplate` changes it).

A started item moves to `<queueDir>/started/<id>.json` with `taskId`, `childThreadId`, `machine`, `requestId` and
`startedAt`. An id that is already in `started/` is refused: use a new id.

Lenses: `<queueDir>/lens-watch/<name>.json` `{"pr": n, "lenses": [{"lens": "code", "focus": "..."}], "priority",
"maxRounds": 2, "machines"?}`. When the PR's CI check is green on its head, it writes one brief per lens
(`<queueDir>/briefs/lens-<pr>-<lens>-<head7>.md`: PR, head, lens, focus, and "Read `<lensRules>` fully and follow
it") and queues one `lens` item pinned to that head, with role `review`. A new head is a new round; at most
`maxRounds` rounds per lens. Delete the watch file when the PR is done.

## Backlog

`<backlogDir>/<id>.json` (default: `backlog/` next to the queue folder): standing items in the queue's format, plus

- `repeat`: minutes. A repeating item stays in the folder and runs again: each run gets the id `<id>.r<n>`
  (its record `started/<id>.r<n>.json`), and a new run starts only when the last one is no longer live in T3 and
  `repeat` minutes have passed since it started. Without `repeat` the item is a one-off: it moves to
  `started/<id>.json` like a queue item.
- `expires`: an ISO UTC time; after it the item is not started (logged once, kept: delete it).

Queue items always go first: the backlog only fills the slots that are still free after every ready queue item
that fits was started. Within the backlog the order is the queue's (priority, then age). The same machine,
account, usage and capacity rules apply, with the same request ids, markers and atomic move. A backlog item with
the same id as a queue item is refused. Lens items never come from the backlog.

## Idle alarm

A machine is idle on a run when it has a free agent slot, its capacity check passes, at least one account
(`defaults.account`, or `claudeAgent`) passes the usage rules there, and no ready queue or backlog item fits it (a
ready item that waits only for an account's usage does not count as idle; nor do writer items when only lens or
other slots are free). After `idleAlarm.passes` (3) idle runs in a row, the filler sends the parent one message
through `POST /api/fork/delegate/notify`: "[slot filler] slots idle: `<machine>` `<free>/<max>` agents
(`<free>/<max>` writers) free since T; ...; queue+backlog empty for those machines." It sends again at most every
`idleAlarm.everyMinutes` (15) while that lasts, and logs every alarm, delivered or not ("idle alarm NOT
delivered (...)": a T3 without the notify route, or no slot to wake the parent). A busy run, or full slots,
restarts the count. `"idleAlarm": {"enabled": false}` turns it off. The state is in `stateDir/idle.json`.

`slot-filler status` (read only) prints each machine's live agents and writers from T3, its free slots, the
queue, backlog and in-flight counts, and an `idle:` line per idle machine (free slots, since when, the runs counted,
the last alarm). It exits 1 when T3 does not answer (the rest is still printed).

## Config

`--config` (default `~/.config/slot-filler/config.json`, or `$SLOT_FILLER_CONFIG`):

```json
{
  "queueDir": "~/queue",
  "backlogDir": "~/backlog",
  "idleAlarm": { "enabled": true, "passes": 3, "everyMinutes": 15 },
  "stateDir": "~/.local/state/slot-filler",
  "log": "~/.local/state/slot-filler/filler.log",
  "parentThreadId": "<orchestrator thread id>",
  "server": { "url": "http://127.0.0.1:3773", "tokenFile": "~/.config/slot-filler/token" },
  "repo": "owner/repo",
  "ciCheck": "fork-ci-result",
  "lensRules": "/abs/path/lens-rules.md",
  "defaults": { "account": "claudeAgent", "model": "claude-opus-5-5", "effort": "high" },
  "usage": { "guardConfig": "~/.config/usage-guard/config.json" },
  "unknownKindCountsAs": "writer",
  "machines": {
    "main": {
      "maxAgents": 10,
      "maxWriters": 4,
      "capacity": {
        "writer": { "command": ["t3-capacity", "3072"] },
        "default": {
          "command": ["t3-capacity", "1024"],
          "free": { "availableMb": 1024, "swapfreeMb": 1024 }
        }
      }
    },
    "spare": { "environmentId": "<linked environment id>", "maxAgents": 6, "maxWriters": 3 },
    "wsl": {
      "environmentId": "<linked environment id>",
      "projectId": "<that machine's project id>",
      "maxAgents": 2,
      "maxWriters": 1
    },
    "mac": {
      "environmentId": "<linked environment id>",
      "projectId": "<that machine's project id>",
      "worktreeIsolation": false,
      "maxAgents": 2,
      "maxWriters": 1
    }
  }
}
```

A queue item for a linked machine names it and leaves out `isolation`, since the child runs in that project's
checkout:

```json
{
  "id": "fix-login-43",
  "title": "Fix the login retry on Windows",
  "kind": "writer",
  "brief": "/abs/path/brief.md",
  "machines": ["wsl", "mac"],
  "account": ["claudeAgent"]
}
```

- A machine without `environmentId` is the server's own machine.
- `projectId`: the project the child runs in, sent as `target.projectId`. A linked machine needs it; without it,
  T3 refuses the start ("Pass target.projectId").
- `worktreeIsolation` (default: false when the machine has an `environmentId`, else true): whether T3 can give a
  child its own worktree there. Set it to false for a project that is not a git checkout. An item with
  `"isolation": "worktree"` is refused when it is read (logged once, kept) if one of the machines it names, or
  every machine when it names none, has this false. When it names no machines, it runs only where this is true.
- `inlineBrief` (default: true when the machine has an `environmentId`): the brief's text goes in the task
  text, since another machine cannot read this machine's files (at most 100,000 characters, else refused).
- `capacity.<kind>` (or `default`): a command that must exit 0, or, with `free`, whose
  `available=NNNMB swapfree=NNNMB` line must show at least those numbers (its exit status is then not used).
- `usage`: `maxUsedPercent` (95), `maxAgeMinutes` (90), `cacheDir` (`~/.t3/caches`), `remoteAccounts`
  `{environmentId: {account: meterName}}`, read from `guardConfig` when given. A machine is skipped for an
  account whose meter (`<cacheDir>/<meter>.json` `usageLimits`) has a window at or above `maxUsedPercent`, or was
  read longer ago than `maxAgeMinutes`, or is missing. Windows scoped to another model family and windows whose
  reset time has passed do not count: the same rules as the usage guard.
- Children the filler did not start count as `unknownKindCountsAs` (default `writer`, the safe side).
- Slots are counted only from this parent's children. Agents of other parents on the same machine are not seen
  here; the server's own slot gate still counts them and refuses a start over its limit.

## The state question

- **Runs again:** one run at a time (a lock in `stateDir`; a second run exits at once). A run with nothing new
  writes nothing: a waiting or refused item is logged once and again only when its reason changes
  (`stateDir/notes.json`).
- **Stops half-way:** each queue item gets one request id, kept in `stateDir/requests/<id>.json` and reused on
  every run until the item started or left the queue. Before asking T3 it writes `stateDir/inflight/<id>.json`
  with that id. The next run asks again with the same id, and T3 looks it up before any refusal: it answers with
  the task it already started (even if the parent is idle or the slots are full by now), or starts it now. So a
  start is never lost and never doubled. Only `request_rejected` (T3 refused that id for good) gets a new id;
  its backoff count stays.
- **An unclear answer:** a refusal T3 gives before it dispatches (`slots_full`, `parent_not_active`,
  `usage_pacing_hold`, `provider_unavailable`, ...) means nothing started. Any other answer (`orchestration_error`,
  such as a linked machine that stopped answering after it launched the thread, or a code the filler does not
  know) may hide a start there. The marker then stays on that machine, it counts as a slot there, and the item is
  asked again only there (same request id, after its backoff) until that machine answers with a task or a clear
  refusal. Only then is another machine tried.
- **A stop and then a handover:** the marker and request file keep their parent. A left-over start whose parent is
  not the configured one is only looked up there (`lookupOnly`). It is never started again: when it is not found,
  the item waits with a note to check that thread's children and then delete the marker. If the started record was written before the stop, it only finishes the
  move. The queue file moves only after T3 returned a task id. Every file it writes is written atomically.
- **T3 down or the token bad:** the run stops at its first call, logs it once, and keeps everything. When T3 answers
  again it logs that and carries on.
- **Machine offline or environment unlinked:** T3 refuses the start (`environment_*`, `slots_full`, ...). The filler
  logs the refusal once, removes the marker (nothing was started), keeps the item, and tries the next allowed machine
  or the next run. Each clear refusal doubles that item's wait on that machine (1 minute, then 2, 4, ... up to 15),
  logged as "<machine> backing off after N refusal(s) (last: code) until T", so a refusal the server checks after the
  wake does not wake the parent every minute, and a machine that keeps refusing does not hold back a free one. A
  wait more than 15 minutes ahead (the clock was set back) is cut to 15 minutes. A child on an environment no machine names still counts, under no machine.
- **Meter missing or old:** that machine is skipped for that account, with a log line; the item's next account,
  if it names one, is tried there.
- **An account fills during a start:** the intent marker records the account it asked with. A start that stopped
  half-way, or got no clear answer, is asked again with the same request id on the same account, never moved to
  the next one (it may have started there). A clear refusal (the server's usage guard: `usage_pacing_hold`)
  removes the marker; after that machine's backoff the next run picks again from the list, so the next account is
  used. The queue file is never rewritten: it keeps its list.
- **Old queue files and markers:** an `account` string is a list of one; a marker from before account lists
  holds the item with its string and is asked again as it was.
- **Brief deleted:** the item is refused and kept until the brief is back.
- **PR closed:** `pr-green` and `pr-merged` items wait with a log line ("closed without a merge"). Delete them
  by hand.
- **Parent thread gone:** the run stops, logs it and starts nothing.
- **Parent handed over:** a parent settled by hand is taken as handed over to a new orchestrator. The run stops
  before it asks anything (left-over starts too) with "set parentThreadId to the orchestrator that runs now".
- **Empty, broken or incomplete queue file:** refused with the reason, logged once, and kept.
- **Missing queue folder, empty queue:** nothing to do. A missing backlog folder (an old config without
  `backlogDir`) is an empty backlog; the filler never makes it.
- **Backlog item killed half-way:** its marker records where it came from (`from`, and `keep` for a repeating
  item). The next run asks again with the same request id and only finishes the move, so a one-off never starts
  twice; a repeating item's run id `<id>.r<n>` is fixed until that run's record is written.
- **Idle alarm runs again or stops half-way:** the time of an alarm is written to `idle.json` before the message
  is sent, so a stop never sends a second one within 15 minutes (a stop before the send loses that one alarm; the
  next comes 15 minutes later). Each alarm has its own request id (`idle-alarm:<time>`), so T3 sends it once.
  `idle.json` is written only when it changes (the count stops at `passes`), so long idle runs write nothing.
- **Recovery starts even if caps are now full:** the route's own slot gate still refuses it.

## Install

The units belong to the machine's private config: a user service running
`/usr/bin/python3 -I -B <path>/bin/slot-filler --config <config>` (`Type=oneshot`), and a timer with
`OnUnitActiveSec=60s`. Enable with `systemctl --user enable --now slot-filler.timer`.
