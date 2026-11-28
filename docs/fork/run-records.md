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
| `kind`, `kindRefused`                                           | `[run-kind: ...]` from the task text, else the delegated task's title. When the tag was refused, `kind` is null and `kindRefused` holds the tag's text.                           |
| `estimate`, `estimateRefused`                                   | `[run-estimate: ...]` from the task text: `wallMs`, `tokens`, or both. When the tag could not be read, `estimate` is null and `estimateRefused` holds the tag's text.             |
| `continuationOf`                                                | The run this one continues after a restart, a Continue or a usage-limit reset.                                                                                                    |
| `lane`                                                          | The lane the run worked for ([`server.lanes`](README.md#serverlanes)). Not written yet; absent in older records, which read as no lane.                                           |
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

- `[run-kind: lens: data]`: a free-text kind, grouped by in the summary. A kind over 80 characters is
  cut to 80 and ends in `…`. The first closed tag is used. An empty one (`[run-kind: ]`) is refused,
  and so is the first unclosed one when no tag is closed. A kind cannot hold `[` or `]`: a `[` ends
  the tag, so `[run-kind: lens [x]]` is an unclosed `lens`, refused. A refused kind is kept in
  `kindRefused`, logged as a warning, counted in the summary, and the run gets no kind (not the
  task's title).
- `[run-estimate: 40m 2M tokens]`: the parent's guess, a time, a token count, or both.
  - Times: `1h30m`, `1h 30m`, `1.5h`, `90 min`, `45s` (`h`, `hr`, `hour(s)`, `m`, `min`, `minute(s)`,
    `s`, `sec`, `second(s)`; any case).
  - Tokens need the word "tokens" (or `tok`): `2M tokens`, `2.5m tokens`, `300k tokens`,
    `2 million tokens`, `1e6 tokens`, `1,500,000 tokens`. The scale may touch the word: `2ktokens`,
    `2Mtokens`, `300ktok`. Without the word, `m` means minutes.
  - Parts may be joined by spaces, `,`, `;`, `+`, `~` or "and".
  - Anything else refuses the whole tag, and nothing is guessed: a range (`30-40m`), a lone `2M`
    (minutes or a million?), `300k` without "tokens", words like "about", a time unit twice, or two
    token counts. An empty tag is refused too. A refused tag is kept in `estimateRefused` (cut to 80
    characters plus `…`), logged as a warning, and counted in the summary.
  - The first closed tag of at most 80 characters decides. One with no closing `]` on its line (or
    before the next `[`), or over 80 characters, is passed over, so a brief may mention the tag
    (`see the [run-estimate: format below`) before its real one. A later tag never replaces the one
    that decides: `[run-estimate: 30-40m]` and further down `[run-estimate: 1h 2M tokens]` refuses
    `30-40m`, so a typo in the real tag is counted, not hidden. Only when no tag decides is the
    first one passed over refused.
- Characters are what a person sees: an emoji or a letter with its accent counts as one, and a cut
  never splits one.

## The weekly summary

```sh
node apps/server/src/fork/runRecords/summaryCli.ts [--days 7] [--to 2026-11-20] [--json] \
  [--state-dir <dir>] [more runs.jsonl files, e.g. copied from linked machines]
```

It reads files only, so it is safe beside a running server. It prints runs, cut-off and failed runs,
median and total time and tokens per kind, role and model, and estimate against actual per
delegated task (all of a task's runs summed). With the model rates the Usage page saved, it adds a
list price; that is the public per-token price, not what a subscription bills. A run is priced at
the model the provider reported running when it reported one, else at the model asked for. Runs
whose model has no rate are left out of the price and counted (`+2 unpriced`); `-` means none had
a rate.

The header names the window's first and last day: `--to 2026-11-20` (the end of that day, UTC)
prints `2026-11-14 to 2026-11-20`. `--to` takes only a real day written `YYYY-MM-DD`: `2026-02-30`,
`2026-11` and `2026/11/20` are refused with a non-zero exit.

It reads the files line by line and keeps only the window's records, so a file of any size works
(a whole string would stop past about 512 MB). A records file it cannot read (other than the state
folder's own file not existing yet) is an error with a non-zero exit, never "0 recorded runs".

Runs that ended before a machine first ran this version have no record, and are absent from the
summary.

## When the server restarts

The records follow the server's stored events from the cursor, so a run that ends while the server
is down or restarting is recorded at the next start. A run already recorded is not written again,
and readers keep the first line of a run, so a crash between writing a line and moving the cursor
costs nothing. If the cursor cannot be saved after the line was written (a full disk), the run is
remembered in memory, so retries write no second line. A cursor that cannot be read starts again at
the newest event, and so does a cursor ahead of the newest event (the database was restored or
replaced beside an old records folder), with a warning. A refused tag's warning is logged just
before its line is written, so after such a crash the replay logs it a second time; the summary
still counts the run once.

If a run's records cannot be read (a busy database), the follower stops and retries from its
cursor; after 3 failed tries in one server process the run is skipped with a warning. A run whose
thread is gone is skipped at once.
