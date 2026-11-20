/**
 * Fork: a completed handoff run does not alert through the relay, because the person's own turn
 * already did. The mark is stored with the run (its `handoff:` message, sent by the server as an
 * agent), so a restart keeps it; the relay sees the run as upstream's stopped run
 * (`latestRunStopRequested`). Every other run alerts as before.
 */
import {
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  RunId,
  type ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import { parseHandoffMessageId } from "./handoffFacts.ts";

/**
 * Upstream's relay holds a "finished" alert this long, because a run can end moments before its
 * continuation starts; a run that starts in the hold drops it (`AgentAwarenessRelay.ts`).
 */
export const RELAY_COMPLETION_HOLD_MS = 5_000;

/**
 * The handoff service waits this long at a quiet thread before it sends (`HandoffService.ts`), so
 * the person's alert for their own turn has gone out first; twice the hold, for a relay that lags.
 */
export const HANDOFF_SETTLE_MS = 2 * RELAY_COMPLETION_HOLD_MS;

/** How often a settle checks the relay's view between events (`handoffRelayView.ts`). */
export const HANDOFF_SETTLE_CHECK_MS = 1_000;

type QuietMessage = Pick<
  OrchestrationV2ConversationMessage,
  "id" | "runId" | "createdBy" | "creationSource"
>;

/**
 * A completed first-send handoff run, not marked loud (its settle saw the relay show the person's
 * finish at every check), sent by the server, no message of the person's in it, and requested at
 * least the relay's hold after the person's run finished (so their alert went out). Anything else,
 * a retry included, alerts as the turn's finish.
 */
export function isQuietHandoffRun(
  threadId: ThreadId,
  run: Pick<OrchestrationV2Run, "id" | "status" | "userMessageId" | "requestedAt">,
  messages: ReadonlyArray<QuietMessage>,
  sourceRun: Pick<OrchestrationV2Run, "id" | "completedAt"> | undefined,
): boolean {
  if (run.status !== "completed") return false;
  const handoff = parseHandoffMessageId(threadId, run.userMessageId);
  if (handoff === null || handoff.retry || handoff.loud) return false;
  const prompt = messages.find((message) => message.id === run.userMessageId);
  if (prompt?.createdBy !== "agent" || prompt.creationSource !== "server") return false;
  if (messages.some((message) => message.runId === run.id && message.createdBy === "user")) {
    return false;
  }
  if (sourceRun?.id !== handoff.runId || sourceRun.completedAt == null) return false;
  return (
    DateTime.toEpochMillis(run.requestedAt) - DateTime.toEpochMillis(sourceRun.completedAt) >=
    RELAY_COMPLETION_HOLD_MS
  );
}

/** Reads the thread's latest run, its messages and source run; says whether it is a quiet handoff run. */
const latestRunIsQuietHandoff = Effect.fn("HandoffQuiet.latestRunIsQuietHandoff")(function* (
  projections: ProjectionStore.ProjectionStoreV2["Service"],
  thread: AgentAwarenessThread,
) {
  const latestRunId = thread.latestRunId;
  if (latestRunId === null || thread.status !== "completed") return false;
  const { runs } = yield* projections.getThreadRecords(thread.id, ["runs"], {
    runIds: [latestRunId],
  });
  const run = runs.find((candidate) => candidate.id === latestRunId);
  // Most runs stop here: only a first send's handoff id costs the other reads.
  const handoff = run === undefined ? null : parseHandoffMessageId(thread.id, run.userMessageId);
  if (run === undefined || handoff === null || handoff.retry || handoff.loud) return false;
  const [prompt, steered, source] = yield* Effect.all([
    projections.getThreadRecords(thread.id, ["messages"], { messageIds: [run.userMessageId] }),
    projections.getThreadRecords(thread.id, ["messages"], {
      messageRunIds: [run.id],
      messageRoles: ["user"],
    }),
    projections.getThreadRecords(thread.id, ["runs"], { runIds: [RunId.make(handoff.runId)] }),
  ]);
  return isQuietHandoffRun(
    thread.id,
    run,
    [...prompt.messages, ...steered.messages],
    source.runs.find((candidate) => candidate.id === handoff.runId),
  );
});

/**
 * Upstream's thread reads, except that one thread's awareness read shows a completed handoff run
 * as quiet. The full snapshot (the relay's catch-up list) stays as it is: the relay reads each
 * thread again before it publishes. A failed read leaves the thread as upstream read it.
 */
export const quietHandoffAwareness = (
  threads: ThreadManagement.ThreadManagementService["Service"],
  projections: ProjectionStore.ProjectionStoreV2["Service"],
): ThreadManagement.ThreadManagementService["Service"] => ({
  ...threads,
  getAgentAwarenessSnapshot: (threadId) =>
    threadId === undefined
      ? threads.getAgentAwarenessSnapshot()
      : threads.getAgentAwarenessSnapshot(threadId).pipe(
          Effect.flatMap((snapshot) =>
            Effect.forEach(snapshot.threads, (thread) =>
              thread.latestRunStopRequested === true
                ? Effect.succeed(thread)
                : latestRunIsQuietHandoff(projections, thread).pipe(
                    Effect.catch((cause) =>
                      Effect.logWarning("handoff quiet check failed; the relay alerts as usual", {
                        threadId: thread.id,
                        cause,
                      }).pipe(Effect.as(false)),
                    ),
                    Effect.map((quiet) =>
                      quiet ? { ...thread, latestRunStopRequested: true } : thread,
                    ),
                  ),
            ).pipe(Effect.map((quieted) => ({ ...snapshot, threads: quieted }))),
          ),
        ),
});

/** Given to the relay only (`ForkLayers.holdWorkAgentActivity`): upstream's reads, made quiet. */
export const layer = Layer.effect(
  ThreadManagement.ThreadManagementService,
  Effect.gen(function* () {
    return quietHandoffAwareness(
      yield* ThreadManagement.ThreadManagementService,
      yield* ProjectionStore.ProjectionStoreV2,
    );
  }),
).pipe(Layer.provide(ProjectionStore.layer));
