/** Fork: test fixtures for the handoff code: one completed run on a Claude thread. */
import {
  type OrchestrationV2Run,
  type OrchestrationV2ThreadProjection,
  type OrchestrationV2TurnItem,
  MessageId,
  NodeId,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  ProviderTurnId,
  RunId,
  ThreadId,
  TurnItemId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import type { AgentAwarenessThread } from "../../orchestration-v2/ProjectionStore.ts";
import * as ProviderAdapterRegistry from "../../orchestration-v2/ProviderAdapterRegistry.ts";
import { quietHandoffShellMark } from "./handoffShell.ts";

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

/** A user message, a reply or a stop request in run `runId` (the write turn's items). */
export function turnItem(
  forRun: RunId,
  ordinal: number,
  item:
    | { readonly type: "user_message"; readonly messageId: string; readonly text: string }
    | { readonly type: "assistant_message"; readonly text: string }
    | { readonly type: "run_interrupt_request"; readonly message: string },
): OrchestrationV2TurnItem {
  const base = {
    id: TurnItemId.make(`item:${forRun}:${ordinal}`),
    threadId,
    runId: forRun,
    nodeId: null,
    providerThreadId: null,
    providerTurnId: null,
    nativeItemRef: null,
    parentItemId: null,
    ordinal,
    status: "completed" as const,
    title: null,
    startedAt: null,
    completedAt: null,
    updatedAt: at(),
  };
  if (item.type === "run_interrupt_request") {
    return { ...base, type: "run_interrupt_request", message: item.message };
  }
  return item.type === "user_message"
    ? {
        ...base,
        type: "user_message",
        messageId: MessageId.make(item.messageId),
        createdBy: "user",
        creationSource: "web",
        inputIntent: "turn_start",
        text: item.text,
        attachments: [],
      }
    : {
        ...base,
        type: "assistant_message",
        messageId: MessageId.make(`reply:${forRun}:${ordinal}`),
        text: item.text,
        streaming: false,
      };
}

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
    // As upstream's shell, with the fork's quiet handoff mark (`handoffShell.ts`).
    latestRunStopRequested: false,
    ...quietHandoffShellMark(data, last),
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

/**
 * Upstream's adapter lookup for the handoff's tests: an instance in `missing()` (read at each call,
 * so a test may change it) is not registered and fails as upstream's registry does for a removed
 * instance; any other one is there.
 */
export const adapterRegistry = (missing: () => ReadonlySet<string> = () => new Set()) =>
  Layer.mock(ProviderAdapterRegistry.ProviderAdapterRegistryV2)({
    get: (instanceId) =>
      Effect.suspend(() =>
        missing().has(instanceId)
          ? Effect.fail(
              new ProviderAdapterRegistry.ProviderAdapterRegistryLookupError({ instanceId }),
            )
          : Effect.succeed({} as never),
      ),
  });

/** The instance a handoff target names in `runnableCases`: another Claude instance. */
export const otherInstanceId = ProviderInstanceId.make("claude-two");

/** The server's config for `otherInstanceId` alone: on or turned off. */
export const otherInstanceConfig = (enabled: boolean) => ({
  [otherInstanceId]: { driver: ProviderDriverKind.make("claudeAgent"), enabled },
});

/**
 * Whether a handoff can run, for both owners' tests (one table, so the compaction wrapper and the
 * handoff service are tested against the same answers): `runs` true means handoff owns the thread
 * (no idle compaction, a handoff is sent); false means compaction covers it and no handoff is sent.
 */
export const runnableCases: ReadonlyArray<{
  readonly name: string;
  /** The handoff's `modelSelection` instance, or none (the thread's own model). */
  readonly target: ProviderInstanceId | null;
  /** The server's provider instance config for `target`: on, turned off, or not configured. */
  readonly config: "on" | "off" | "none";
  /** The thread's project turns `target` off. */
  readonly projectOff?: boolean;
  /** `target` (or the thread's own instance) is not in upstream's registry: removed. */
  readonly missing?: boolean;
  /** The thread runs on this instance (`runnableThread`), not the built-in `claudeAgent`. */
  readonly threadInstance?: ProviderInstanceId;
  readonly runs: boolean;
}> = [
  {
    name: "another instance, on and registered",
    target: otherInstanceId,
    config: "on",
    runs: true,
  },
  {
    name: "another instance, removed",
    target: otherInstanceId,
    config: "none",
    missing: true,
    runs: false,
  },
  {
    name: "another instance, removed from the registry but still configured",
    target: otherInstanceId,
    config: "on",
    missing: true,
    runs: false,
  },
  {
    name: "another instance, turned off but still registered",
    target: otherInstanceId,
    config: "off",
    runs: false,
  },
  {
    name: "another instance, turned off for the project only",
    target: otherInstanceId,
    config: "on",
    projectOff: true,
    runs: false,
  },
  {
    name: "old settings with no modelSelection: the thread's instance",
    target: null,
    config: "none",
    runs: true,
  },
  {
    name: "no modelSelection, the thread's instance removed",
    target: null,
    config: "none",
    missing: true,
    runs: false,
  },
  // The thread on another instance, so its own instance is not the built-in one that is on.
  {
    name: "no modelSelection, the thread on another instance, on and registered",
    target: null,
    threadInstance: otherInstanceId,
    config: "on",
    runs: true,
  },
  {
    name: "no modelSelection, the thread on another instance, removed from the registry",
    target: null,
    threadInstance: otherInstanceId,
    config: "on",
    missing: true,
    runs: false,
  },
];

/** The thread of `data` as a `runnableCases` entry puts it: on its `threadInstance`, if any. */
export const runnableThread = (
  entry: (typeof runnableCases)[number],
  data: OrchestrationV2ThreadProjection,
): OrchestrationV2ThreadProjection => {
  const other = entry.threadInstance;
  if (other === undefined) return data;
  const onOther = { instanceId: other, model: "claude-opus-5-5" };
  return {
    ...data,
    thread: { ...data.thread, providerInstanceId: other, modelSelection: onOther },
    runs: data.runs.map((one) => ({ ...one, providerInstanceId: other, modelSelection: onOther })),
    providerThreads: data.providerThreads.map((one) => ({ ...one, providerInstanceId: other })),
  };
};

/** The settings parts a `runnableCases` entry sets, over the thread's project id. */
export const runnableSettings = (entry: (typeof runnableCases)[number], projectId: ProjectId) => {
  const id = entry.target ?? entry.threadInstance ?? instanceId;
  return {
    handoff:
      entry.target === null
        ? {}
        : { modelSelection: { instanceId: entry.target, model: "claude-opus-5-5" } },
    providerInstances:
      entry.config === "none"
        ? {}
        : {
            [id]: {
              driver: ProviderDriverKind.make("claudeAgent"),
              enabled: entry.config === "on",
            },
          },
    projectProviderInstances:
      entry.projectOff === true ? { [projectId]: { [id]: { enabled: false } } } : {},
    missing: entry.missing === true ? new Set<string>([id]) : new Set<string>(),
  };
};
