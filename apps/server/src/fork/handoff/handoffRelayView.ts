/**
 * Fork: does upstream's relay show a thread's finish right now? Projected as the relay does
 * (`projectThreadAwarenessV2`, with a live child's request), so any work the relay shows counts as
 * "not finished". The handoff settle reads it at each check (`HandoffService.ts`).
 */
import { EnvironmentId, type ThreadId } from "@t3tools/contracts";
import {
  projectThreadAwarenessV2,
  type SubagentRequestAwareness,
  subagentAwaitsUser,
} from "@t3tools/shared/agentAwareness";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";

import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import type { ActiveShellIndexEntry } from "../../orchestration-v2/ProjectionStore.ts";
import { isSnoozed } from "../../orchestration-v2/ThreadSettlementService.ts";

/** Only the phase is read; the relay fills these from the environment and project. */
const PLACEHOLDER = { environmentId: EnvironmentId.make("handoff"), project: { title: "" } };

/**
 * Whether a child's chain of parents reaches the thread, walked as the relay walks it (through
 * subagents and side chats). A parent missing from the index counts as reaching it: unsure alerts.
 */
function reachesThread(
  child: ActiveShellIndexEntry,
  threadId: ThreadId,
  byId: ReadonlyMap<ThreadId, ActiveShellIndexEntry>,
): boolean {
  const seen = new Set<ThreadId>([child.id]);
  let current = child;
  while (current.lineage.relationshipToParent === "subagent" || current.lineage.sideChat === true) {
    const parentId = current.lineage.parentThreadId;
    if (parentId === threadId) return true;
    if (parentId === null || seen.has(parentId)) return false;
    seen.add(parentId);
    const parent = byId.get(parentId);
    if (parent === undefined) return true;
    current = parent;
  }
  return false;
}

/** The newest live child's request that the relay shows on the thread, as its relay map would. */
const childRequestOn = Effect.fnUntraced(function* (
  orchestrator: Orchestrator.OrchestratorV2["Service"],
  threadId: ThreadId,
) {
  const index = yield* orchestrator.listActiveShellIndex();
  const byId = new Map(index.map((entry) => [entry.id, entry]));
  let pending: SubagentRequestAwareness | null = null;
  for (const entry of index) {
    if (entry.lineage.relationshipToParent !== "subagent" || !entry.mayBeLive) continue;
    if (!reachesThread(entry, threadId, byId)) continue;
    const child = (yield* orchestrator.getAgentAwarenessSnapshot(entry.id)).threads[0];
    const request = child?.pendingRuntimeRequest;
    if (child === undefined || child.archivedAt !== null || !subagentAwaitsUser(child)) continue;
    if (request == null) continue;
    if (pending === null || DateTime.isGreaterThan(child.updatedAt, pending.updatedAt)) {
      pending = {
        requestId: request.id,
        updatedAt: child.updatedAt,
        waitsForInput: request.kind === "user_input",
      };
    }
  }
  return pending;
});

/** True only when the relay would show the thread as finished now (see the file comment). */
export const relayShowsFinish = Effect.fn("HandoffRelayView.relayShowsFinish")(function* (
  threadId: ThreadId,
) {
  const orchestrator = yield* Orchestrator.OrchestratorV2;
  const thread = (yield* orchestrator.getAgentAwarenessSnapshot(threadId)).threads[0];
  if (thread === undefined || thread.archivedAt !== null) return false;
  // A snoozed finish is not shown: the handoff run alerts instead, as upstream would.
  if (isSnoozed(thread, DateTime.toEpochMillis(yield* DateTime.now))) return false;
  const own = projectThreadAwarenessV2({ ...PLACEHOLDER, thread });
  // Most checks stop here; only a finished thread costs the read of its live children.
  if (own?.phase !== "completed") return false;
  const pendingSubagentRequest = yield* childRequestOn(orchestrator, threadId);
  if (pendingSubagentRequest === null) return true;
  return (
    projectThreadAwarenessV2({ ...PLACEHOLDER, thread, pendingSubagentRequest })?.phase ===
    "completed"
  );
});
