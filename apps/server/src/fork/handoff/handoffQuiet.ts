/**
 * Fork: a completed handoff run does not alert through the relay. The mark is stored with the run
 * (its `handoff:` message, sent by the server as an agent), so a restart keeps it; the relay sees
 * the run as upstream's stopped run (`latestRunStopRequested`). Every other run alerts as before.
 */
import {
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2Run,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ThreadManagement from "../../orchestration-v2/ThreadManagementService.ts";
import { parseHandoffMessageId } from "./handoffFacts.ts";

type QuietMessage = Pick<
  OrchestrationV2ConversationMessage,
  "id" | "runId" | "createdBy" | "creationSource"
>;

/**
 * Whether a completed run is a handoff run and nothing else: its message carries the handoff id
 * and was sent by the server as an agent (a person cannot send that), and the person steered no
 * message of their own into it (that makes the run theirs, as upstream does for background wakes).
 * A failed handoff run, or one whose message is missing, alerts as before.
 */
export function isQuietHandoffRun(
  threadId: ThreadId,
  run: Pick<OrchestrationV2Run, "id" | "status" | "userMessageId">,
  messages: ReadonlyArray<QuietMessage>,
): boolean {
  if (run.status !== "completed") return false;
  if (parseHandoffMessageId(threadId, run.userMessageId) === null) return false;
  const prompt = messages.find((message) => message.id === run.userMessageId);
  if (prompt?.createdBy !== "agent" || prompt.creationSource !== "server") return false;
  return !messages.some((message) => message.runId === run.id && message.createdBy === "user");
}

/** Reads the thread's latest run and its messages, and says whether it is a quiet handoff run. */
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
  // Most runs stop here: only a handoff id costs the message reads.
  if (run === undefined || parseHandoffMessageId(thread.id, run.userMessageId) === null) {
    return false;
  }
  const [prompt, steered] = yield* Effect.all([
    projections.getThreadRecords(thread.id, ["messages"], { messageIds: [run.userMessageId] }),
    projections.getThreadRecords(thread.id, ["messages"], {
      messageRunIds: [run.id],
      messageRoles: ["user"],
    }),
  ]);
  return isQuietHandoffRun(thread.id, run, [...prompt.messages, ...steered.messages]);
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
