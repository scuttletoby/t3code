# Run records

Fork only. The server writes one record for every agent run that ends: every thread, every project,
writers, review lenses, audits and orchestrators alike, and tasks delegated here from a linked
machine. The records answer "what did this kind of work cost, and how long did it take", and they
let a parent's estimate be checked against what happened.

There is no setting. The server only adds lines to its own files; it changes nothing an agent or a
person sees.

## Where the records are

`<stateDir>/run-records/runs.jsonl` (the state folder is `~/.t3/userdata` unless `T3CODE_HOME` or
`--base-dir` moves it), one JSON object per line. The file is append-only and nothing prunes it.
`cursor.json` beside it says how far the server has read the stored events.

Each record holds:

| Field                                                           | What                                                                                                                                                                              |
| --------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `runId`, `threadId`, `threadTitle`, `projectId`, `projectTitle` | Which run.                                                                                                                                                                        |
| `parent`                                                        | The thread that delegated this one (`threadId`, the task's `taskId`), or for a task from a linked machine that machine's `environmentId` and thread. Null for a top-level thread. |
| `role`                                                          | The delegate call's role, read from "Act as the _role_ sub-agent".                                                                                                                |
| `kind`                                                          | `[run-kind: ...]` from the task text, else the delegated task's title.                                                                                                            |
| `estimate`                                                      | `[run-estimate: ...]` from the task text: `wallMs`, `tokens`, or both.                                                                                                            |
| `continuationOf`                                                | The run this one continues after a restart, a Continue or a usage-limit reset.                                                                                                    |
| `model`, `effort`, `modelOptions`, `executedModels`             | What was asked for, and what the provider reported running.                                                                                                                       |
| `requestedAt`, `startedAt`, `endedAt`, `wallMs`, `queuedMs`     | Times. `wallMs` is start to end.                                                                                                                                                  |
| `status`, `endStatus`                                           | The run's own status, and `completed`, `cut_off` or `failed`.                                                                                                                     |
| `usage`                                                         | Tokens as the provider reported them: input (cache included), cache reads, cache writes, output, reasoning, and work outside the main agent.                                      |

`cut_off` is a run that started but stopped half-way: stopped by a person, interrupted by a restart,
or ended without a final answer. Its usage is whatever its provider reported until then, and
`usage.status` says whether that was all of it. A run that never started is not recorded. A re-run
(a restart's continuation, a Continue) is a new run and a new record.

## Labels a parent can give

Put these anywhere in the `delegate_task` task text:

- `[run-kind: lens: data]`: a free-text kind, grouped by in the summary.
- `[run-estimate: 40m 2M tokens]`: the parent's guess. Times take `h`, `m`, `min` and `s`; tokens
  take `k` and `M`.

## The weekly summary

```sh
node apps/server/src/fork/runRecords/summaryCli.ts [--days 7] [--to 2026-11-20] [--json] \
  [--state-dir <dir>] [more runs.jsonl files, e.g. copied from linked machines]
```

It reads files only, so it is safe beside a running server. It prints runs, cut-off and failed runs,
median and total time and tokens per kind, role and model, and estimate against actual per
delegated task (all of a task's runs summed). With the model rates the Usage page saved, it adds a
list price; that is the public per-token price, not what a subscription bills.

Runs that ended before a machine first ran this version have no record, and are absent from the
summary.

## When the server restarts

The records follow the server's stored events from the cursor, so a run that ends while the server
is down or restarting is recorded at the next start. A run already recorded is not written again,
and readers keep the first line of a run, so a crash between writing a line and moving the cursor
costs nothing. A cursor that cannot be read starts again at the newest event.
