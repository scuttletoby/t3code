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
  run has finished since, that newer run decides instead. A late update of an older run (a child
  of an earlier run settling, say) does not lose the timer: the service takes over the latest
  completed run, as upstream's idle compaction does.
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

The service decides from what it read, but something else can land before the handoff does. So it
checks twice more around the send:

- **Right before.** The thread must still be quiet, and its latest run must still be the one the
  handoff was made from. Otherwise nothing is sent.
- **Right after.** If something landed in between, the orchestrator has queued the handoff behind
  it. The service cancels the queued handoff (upstream's own "remove a queued message"), which
  refuses a run that already started.

A message from the person or a child result that lands first always wins, and nothing of theirs is
lost. The send and the cancel are one step that a late event or a shutdown cannot cut in half. One
narrow case remains: if that message's whole run ends between the send and the cancel, the handoff
runs after it. Nothing is lost then either, but the handoff's text does not include that
last turn; the new session can read it in the thread.

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
- **One retry.** A handoff run that fails is sent once more with the same text. If that also fails,
  the text stays in the thread and the server log says so; the next turn end decides again. A
  handoff the person stopped stays stopped: an interrupted handoff run is never sent again.

## Limits

- Timers and waits live in memory. After a server restart, the next turn end, or a late update of
  an older run, decides again. A crash between the send and the cancel leaves a queued handoff
  queued; it runs after the newer run.
- A handoff run still notifies like any finished run (the browser notice and the relay). Keeping it
  silent needs a marker the app can read; that comes with the settings screen.
- The same run never hands off twice: the check before the send drops a second trigger, and every
  trigger for one run uses the same ids, so one that got past would replay the first. A cancelled
  handoff spends that run's ids; the next run's turn end decides.

## For developers

- Fork code: `apps/server/src/fork/handoff/` (`HandoffService.ts`, `handoffDispatch.ts`,
  `handoffFacts.ts`, `handoffMessage.ts`, `handoffDecision.ts`). Settings:
  `packages/contracts/src/handoff.ts`.
- `Orchestrator.ts` stays upstream's (the fork check refuses edits to it). Every handoff send goes
  through `dispatchHandoff` (`handoffDispatch.ts`), and a test checks that. No upstream file is
  edited: the service starts from `ForkLayers.layerRuntimeServices` (`apps/server/src/fork/forkLayers.ts`),
  the fork file `server.ts` already wires.
- The message reuses upstream's handoff budget (`handoffBudget`) and transcript reading
  (`ContextHandoffService.ts`); nothing is copied.
