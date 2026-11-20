/** Fork: fixtures for the run records tests: runs, provider turns and threads as the projection keeps them. */
import {
  MessageId,
  NodeId,
  ProjectId,
  ProviderInstanceId,
  ProviderThreadId,
  ProviderTurnId,
  RunAttemptId,
  RunId,
  ThreadId,
  type OrchestrationV2AppThread,
  type OrchestrationV2ConversationMessage,
  type OrchestrationV2ProviderTurn,
  type OrchestrationV2Run,
  type OrchestrationV2RunAttempt,
  type OrchestrationV2Subagent,
  type TurnTokenUsage,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

export const NOW_MS = Date.parse("2026-11-20T12:00:00.000Z");
export const MINUTE_MS = 60_000;
export const at = (offsetMs = 0) => DateTime.makeUnsafe(NOW_MS + offsetMs);
/** The ISO text a record stores for an epoch millisecond. */
export const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));
const instanceId = ProviderInstanceId.make("claudeAgent");
const providerThreadId = ProviderThreadId.make("provider-thread");
const projectId = ProjectId.make("project-1");

export const thread = (
  id: string,
  patch: Partial<OrchestrationV2AppThread> = {},
): OrchestrationV2AppThread =>
  ({
    id: ThreadId.make(id),
    projectId,
    title: `Thread ${id}`,
    createdBy: "user",
    creationSource: "web",
    providerInstanceId: instanceId,
    modelSelection: { instanceId, model: "claude-opus-5-5" },
    runtimeMode: "full-access",
    interactionMode: "default",
    branch: null,
    worktreePath: null,
    activeProviderThreadId: providerThreadId,
    lineage: { rootThreadId: ThreadId.make(id), parentThreadId: null, relationshipToParent: null },
    forkedFrom: null,
    createdAt: at(-60 * MINUTE_MS),
    updatedAt: at(),
    archivedAt: null,
    settledOverride: null,
    settledAt: null,
    lastVisitedAt: null,
    deletedAt: null,
    ...patch,
  }) as OrchestrationV2AppThread;

/** A delegated child of `parentId`, as `delegate_task` makes it here. */
export const childThread = (id: string, parentId: string, title: string) =>
  thread(id, {
    title,
    createdBy: "agent",
    lineage: {
      rootThreadId: ThreadId.make(parentId),
      parentThreadId: ThreadId.make(parentId),
      relationshipToParent: "subagent",
    },
  });

export const run = (
  threadId: string,
  id: string,
  patch: Partial<OrchestrationV2Run> = {},
): OrchestrationV2Run => ({
  id: RunId.make(id),
  threadId: ThreadId.make(threadId),
  ordinal: 1,
  providerInstanceId: instanceId,
  modelSelection: {
    instanceId,
    model: "claude-opus-5-5",
    options: [{ id: "effort", value: "high" }],
  },
  providerThreadId,
  userMessageId: MessageId.make(`prompt:${id}`),
  rootNodeId: NodeId.make(`node:${id}`),
  activeAttemptId: null,
  status: "completed",
  requestedAt: at(-11 * MINUTE_MS),
  startedAt: at(-10 * MINUTE_MS),
  completedAt: at(),
  checkpointId: null,
  contextHandoffId: null,
  ...patch,
});

export const attempt = (runId: string, ordinal = 1): OrchestrationV2RunAttempt => ({
  id: RunAttemptId.make(`attempt:${runId}:${ordinal}`),
  runId: RunId.make(runId),
  attemptOrdinal: ordinal,
  rootNodeId: NodeId.make(`node:${runId}`),
  providerInstanceId: instanceId,
  providerThreadId,
  providerTurnId: null,
  reason: ordinal === 1 ? "initial" : "steering_restart",
  status: "completed",
  startedAt: at(-10 * MINUTE_MS),
  completedAt: at(),
});

export const usage = (
  inputTokens: number,
  outputTokens: number,
  patch: Partial<TurnTokenUsage> = {},
): TurnTokenUsage =>
  ({
    usageScope: "main_agent",
    usageStatus: "complete",
    inputTokens,
    outputTokens,
    hasSubagents: false,
    ...patch,
  }) as TurnTokenUsage;

let turns = 0;
export const providerTurn = (
  runId: string,
  turnTokenUsage: TurnTokenUsage | undefined,
  patch: Partial<OrchestrationV2ProviderTurn> = {},
): OrchestrationV2ProviderTurn => ({
  id: ProviderTurnId.make(`turn:${(turns += 1)}`),
  providerThreadId,
  nodeId: NodeId.make(`node:${runId}`),
  runAttemptId: RunAttemptId.make(`attempt:${runId}:1`),
  nativeTurnRef: null,
  ordinal: turns,
  status: "completed",
  startedAt: at(-10 * MINUTE_MS),
  completedAt: at(),
  ...(turnTokenUsage === undefined ? {} : { turnTokenUsage }),
  ...patch,
});

export const message = (
  threadId: string,
  runId: string,
  role: "user" | "assistant",
  text: string,
): OrchestrationV2ConversationMessage =>
  ({
    id: MessageId.make(role === "user" ? `prompt:${runId}` : `answer:${runId}`),
    threadId: ThreadId.make(threadId),
    runId: RunId.make(runId),
    role,
    text,
    createdBy: role === "user" ? "agent" : "provider",
    creationSource: role === "user" ? "server" : "provider",
    createdAt: at(),
    updatedAt: at(),
  }) as unknown as OrchestrationV2ConversationMessage;

export const task = (
  parentId: string,
  childId: string,
  prompt: string,
  title: string,
): OrchestrationV2Subagent =>
  ({
    id: NodeId.make(`task:${childId}`),
    threadId: ThreadId.make(parentId),
    runId: null,
    parentNodeId: NodeId.make("parent-node"),
    origin: "app_owned",
    createdBy: "agent",
    driver: "claudeAgent",
    providerInstanceId: instanceId,
    providerThreadId: null,
    childThreadId: ThreadId.make(childId),
    nativeTaskRef: null,
    prompt,
    title,
    model: "claude-opus-5-5",
    status: "completed",
    result: null,
    startedAt: at(-10 * MINUTE_MS),
    completedAt: at(),
    updatedAt: at(),
  }) as unknown as OrchestrationV2Subagent;
