/** Fork: test fixtures for the handoff code: one completed run on a Claude thread. */
import {
  type OrchestrationV2Run,
  type OrchestrationV2ThreadProjection,
  MessageId,
  NodeId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  ProviderTurnId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";

export const NOW_MS = Date.parse("2026-11-15T12:00:00.000Z");
export const MINUTE_MS = 60_000;
export const at = (offset = 0) => DateTime.makeUnsafe(NOW_MS + offset);
export const threadId = ThreadId.make("handoff-thread");
export const runId = RunId.make("run-1");
const instanceId = ProviderInstanceId.make("claudeAgent");
const providerThreadId = ProviderThreadId.make("provider-thread");
const nodeId = NodeId.make("root-node");
export const modelSelection = { instanceId, model: "claude-opus-5-5" };
export const handoffMessageId = MessageId.make(`handoff:${threadId}:${runId}`);

export const run = (patch: Partial<OrchestrationV2Run> = {}): OrchestrationV2Run => ({
  id: runId,
  threadId,
  ordinal: 1,
  providerInstanceId: instanceId,
  modelSelection,
  providerThreadId,
  userMessageId: MessageId.make("message-1"),
  rootNodeId: nodeId,
  activeAttemptId: null,
  status: "completed",
  requestedAt: at(-MINUTE_MS),
  startedAt: at(-MINUTE_MS),
  completedAt: at(),
  checkpointId: null,
  contextHandoffId: null,
  ...patch,
});

export function projection(usage: {
  readonly usedTokens: number;
  readonly cacheExpiresInMs?: number;
}): OrchestrationV2ThreadProjection {
  return {
    thread: {
      id: threadId,
      projectId: ProjectId.make("project"),
      title: "Long thread",
      createdBy: "user",
      creationSource: "web",
      providerInstanceId: instanceId,
      modelSelection,
      runtimeMode: "full-access",
      interactionMode: "default",
      branch: null,
      worktreePath: null,
      activeProviderThreadId: providerThreadId,
      lineage: { rootThreadId: threadId, parentThreadId: null, relationshipToParent: null },
      forkedFrom: null,
      createdAt: at(-MINUTE_MS),
      updatedAt: at(),
      archivedAt: null,
      settledOverride: null,
      settledAt: null,
      lastVisitedAt: at(),
      deletedAt: null,
    },
    runs: [run()],
    messages: [
      {
        id: MessageId.make("message-1"),
        threadId,
        runId,
        nodeId,
        role: "user",
        text: "Keep going",
        attachments: [],
        streaming: false,
        createdBy: "user",
        creationSource: "web",
        createdAt: at(-MINUTE_MS),
        updatedAt: at(-MINUTE_MS),
      },
    ],
    providerThreads: [
      {
        id: providerThreadId,
        driver: ProviderDriverKind.make("claudeAgent"),
        providerInstanceId: instanceId,
        providerSessionId: null,
        appThreadId: threadId,
        ownerNodeId: nodeId,
        nativeThreadRef: null,
        nativeConversationHeadRef: null,
        status: "idle",
        firstRunOrdinal: 1,
        lastRunOrdinal: 1,
        handoffIds: [],
        forkedFrom: null,
        pendingBackgroundTasks: [],
        createdAt: at(-MINUTE_MS),
        updatedAt: at(),
      },
    ],
    providerTurns: [
      {
        id: ProviderTurnId.make("provider-turn-1"),
        providerThreadId,
        nodeId,
        runAttemptId: null,
        nativeTurnRef: null,
        ordinal: 1,
        status: "completed",
        startedAt: at(-MINUTE_MS),
        completedAt: at(),
        tokenUsage: {
          usedTokens: usage.usedTokens,
          ...(usage.cacheExpiresInMs === undefined
            ? {}
            : { promptCacheExpiresAt: DateTime.formatIso(at(usage.cacheExpiresInMs)) }),
          updatedAt: DateTime.formatIso(at()),
        },
      },
    ],
    runtimeRequests: [],
    turnItems: [],
    contextTransfers: [],
    attempts: [],
    nodes: [],
    subagents: [],
    providerSessions: [],
    plans: [],
    checkpointScopes: [],
    checkpoints: [],
    contextHandoffs: [],
    visibleTurnItems: [],
    updatedAt: at(),
  };
}

/**
 * The awareness row upstream's relay reads, derived from the records as upstream's projection does
 * for the fields the handoff looks at: the latest run, its pending request. Background tasks (a T3
 * child's row, a PR watch, a monitor) are not in the records the handoff reads, so a test passes them.
 */
export function awarenessShell(
  data: OrchestrationV2ThreadProjection,
  extra: Partial<AgentAwarenessThread> & { readonly updatedAt: DateTime.Utc },
): AgentAwarenessThread {
  const last = data.runs.reduce((a, b) => (b.ordinal > a.ordinal ? b : a));
  const request = data.runtimeRequests.find((one) => one.status === "pending");
  return {
    id: data.thread.id,
    projectId: data.thread.projectId,
    title: data.thread.title,
    lineage: data.thread.lineage,
    modelSelection: data.thread.modelSelection,
    creationSource: "web",
    status: last.status,
    activityRunStatus: null,
    latestRunId: last.id,
    latestRunCompletedAt: last.completedAt,
    latestRunStopRequested: false,
    latestRunBackgroundWake: false,
    activeRunId: last.status === "running" ? last.id : null,
    pendingRuntimeRequest:
      request === undefined
        ? null
        : { id: request.id, kind: "command", createdAt: extra.updatedAt },
    pendingBackgroundTasks: [],
    pendingGuestMessageCount: 0,
    hasActionableProposedPlan: false,
    settledOverride: null,
    archivedAt: null,
    snoozedUntil: null,
    snoozedAt: null,
    ...extra,
  };
}
