# Handoff instead of compaction

Fork only. When a long thread gets big, or sits idle while its prompt cache is about to run out, T3
starts a fresh session in the same thread and gives it a written handoff. The thread keeps its id,
its running child tasks, its watched pull requests and its waits: they all deliver into the new
session, because T3 sends them to the thread's active session, and the fresh start makes the new one
active. Upstream's compaction rewrites the old session's context instead.

It is off by default. The setting is [`server.handoff`](README.md#serverhandoff), and a project can
have its own ([`projectOverride.handoff`](README.md#projectoverridehandoff)). Only the person can
change it; agents cannot.

## When a thread hands off

The handoff service looks at a thread each time one of its runs completes. It reads the thread's
settings first: with a mode other than `handoff` it reads nothing more and sends nothing.

- **Size backstop.** At the end of a turn, a thread at `sizeTokens` (150,000) or more hands off.
- **Idle.** A thread at `idleTokens` (75,000; `0` never) or more hands off shortly before its prompt
  cache expires: 4 minutes before with the `agent` source, 2 minutes with `command`. The service
  sets one timer per thread; a newer run's turn end replaces it. When the timer fires and a newer
  run has finished since, that newer run decides instead.
- Never after a failed or interrupted run, after a `/compact`, or after a handoff run.
- Under 20,000 tokens never.

Token counts are the provider's own last reading for the active session. A provider that reports
none never hands off.

## Only at a quiet thread

A thread is quiet when none of these is true: a run is queued or running, a request (an approval or
a question) is pending, a child's result is on its way into the thread, a fork or merge is being
applied, the session itself still runs background agents or commands, or the session's goal is
active. Running T3 child tasks never count: their results arrive in the new session.

At the size backstop, a thread that is not quiet waits: the service reads it again each time the
thread changes, and hands off as soon as it is quiet (unless a newer run finished meanwhile). In the
idle window it does not wait: the next turn end starts over.

The service decides from what it read; the message can still land later. One check in the
orchestrator, under the thread's lock, drops a handoff message that lands at a thread that is no
longer quiet, or whose latest run is no longer the one the handoff was made from. A message from the
person or a child result that lands first always wins, and nothing of theirs is lost.

## The message

The new session's first message has these parts:

1. **T3's state**, read when it is sent: the handoff's number, the thread, the run it was made from,
   the model and effort of the new session, the open child tasks (and which wait for an approval or
   an answer), and the watched pull requests with their checks.
2. **The old session's own handoff.** Not built yet: the `agent` and `command` sources come later.
   Until then the message says that no handoff source is set up, and has parts 1, 3 and 4 only.
3. **The recent conversation**, chosen by upstream's handoff budget with a smaller cap (8,000).
4. **The rules**: this is context, not a request; reply with one line and act on the next message or
   event; older history is in the thread (`t3_thread_read`).

The new session uses `modelSelection` from the setting, else the thread's current model, always
written out in full. The message says when no effort is set.

## After the handoff run

- **Read stays read.** If the person had seen the thread before the handoff, the handoff's own
  completion does not mark it unread.
- **One retry.** A handoff run that fails or is interrupted is sent once more with the same text. If
  that also fails, the text stays in the thread and the server log says so; the next turn end
  decides again.

## Limits

- Timers and waits live in memory. After a server restart, the next turn end decides again.
- A handoff run still notifies like any finished run (the browser notice and the relay). Keeping it
  silent needs a marker the app can read; that comes with the settings screen.
- The same run never hands off twice: every trigger for one run uses the same ids, so a second one
  replays the first. A dropped handoff spends that run's ids; the next run's turn end decides.

## For developers

- Fork code: `apps/server/src/fork/handoff/` (`HandoffService.ts`, `handoffFacts.ts`,
  `handoffMessage.ts`, `handoffDecision.ts`). Settings: `packages/contracts/src/handoff.ts`.
- The only upstream edit is one call in `Orchestrator.ts` (`handoffDispatchDropped`), plus the
  service's line in `server.ts`. A test counts the call sites.
- The message reuses upstream's handoff budget (`handoffBudget`) and transcript reading
  (`ContextHandoffService.ts`); nothing is copied.
