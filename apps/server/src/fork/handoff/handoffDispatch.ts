/**
 * Fork: the one way a handoff is sent, so `Orchestrator.ts` stays upstream's. It checks just before
 * the send, and right after cancels a handoff that landed queued behind a newer run; upstream's
 * lock orders the send and refuses to cancel a run that already started (docs/fork/handoff.md).
 */
import { CommandId, type OrchestrationV2Command, type ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import { type HandoffRecords, handoffDispatchDropped } from "./handoffFacts.ts";

export type HandoffDispatchCommand = Extract<
  OrchestrationV2Command,
  { readonly type: "message.dispatch" }
>;

/** What the check reads: every run, and only the pending work and the active session. */
export const readHandoffRecords = Effect.fn("HandoffDispatch.readRecords")(function* (
  threadId: ThreadId,
) {
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  const thread = yield* projections.getThread(threadId);
  const active = thread.activeProviderThreadId === null ? [] : [thread.activeProviderThreadId];
  const records = yield* projections.getThreadRecords(
    threadId,
    [
      "runs",
      "providerThreads",
      "providerTurns",
      "runtimeRequests",
      "turnItems",
      "contextTransfers",
    ],
    {
      turnItemStatuses: ["pending", "running", "waiting"],
      providerThreadIds: active,
      providerTurnThreadIds: active,
      latestProviderTurnUsageOnly: true,
      runtimeRequestStatuses: ["pending"],
      contextTransferStatuses: ["pending"],
    },
  );
  // The messages the facts look at: each run's own (for `/compact`) and child results due.
  const { messages } = yield* projections.getThreadRecords(threadId, ["messages"], {
    messageIds: records.runs.flatMap((run) => [
      run.userMessageId,
      ...(run.delegatedCompletion?.delivery ? [run.delegatedCompletion.delivery.messageId] : []),
    ]),
  });
  return { ...records, thread, messages } satisfies HandoffRecords;
});

/**
 * Sends a handoff message only to a quiet thread at its source run (see the file comment). One
 * uninterruptible step: a replaced timer or a shutdown cannot stop it between the send and the cancel.
 */
export const dispatchHandoff = Effect.fn("HandoffDispatch.dispatch")(function* (
  command: HandoffDispatchCommand,
) {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const projections = yield* ProjectionStore.ProjectionStoreV2;
  if (handoffDispatchDropped(command, yield* readHandoffRecords(command.threadId))) {
    yield* Effect.logInfo("handoff dropped: the thread moved on", { threadId: command.threadId });
    return "dropped" as const;
  }
  yield* orchestrator.dispatch(command);
  const { runs } = yield* projections.getThreadRecords(command.threadId, ["runs"], {
    runStatuses: ["queued"],
  });
  const queued = runs.find(
    (run) => run.status === "queued" && run.userMessageId === command.messageId,
  );
  if (queued === undefined) return "sent" as const;
  return yield* orchestrator
    .dispatch({
      type: "queued-run.cancel",
      commandId: CommandId.make(`server:handoff-cancel:${command.messageId}`),
      threadId: command.threadId,
      runId: queued.id,
    })
    .pipe(
      Effect.as("cancelled" as const),
      // Refused when the run started meanwhile: the newer run ended first, and nothing is lost.
      Effect.catch((cause) =>
        Effect.logWarning("handoff started after a newer run", {
          threadId: command.threadId,
          cause,
        }).pipe(Effect.as("sent" as const)),
      ),
    );
}, Effect.uninterruptible);
