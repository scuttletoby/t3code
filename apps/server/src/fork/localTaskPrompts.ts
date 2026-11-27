/**
 * Fork: what a delegated task's thread here waits on for a person, by the rule of upstream's
 * `pendingTaskApproval`: live questions and approvals of its own runs not ended, and its pending
 * secret cards (label and reason, never a value) and pairing cards.
 */
import {
  NodeId,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadProjection,
  ProviderRequestKind,
  type RunId,
  type RuntimeRequestId,
  ThreadId,
  type TurnItemId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SqlClient from "effect/sql/SqlClient";
import * as NodeSqliteClient from "@t3tools/shared/nodeSqliteClient";

import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";

/** What a task's thread here waits on. Questions are upstream's to wake for (its reactor). */
export interface LocalTaskPrompts {
  readonly questions: ReadonlyArray<{
    readonly requestId: RuntimeRequestId;
    readonly runId: RunId;
  }>;
  /** Oldest first: the first is the `pendingApproval` the task's status reports. */
  readonly approvals: ReadonlyArray<{
    readonly requestId: RuntimeRequestId;
    readonly kind: ProviderRequestKind;
    readonly runId: RunId;
  }>;
  readonly secrets: ReadonlyArray<{
    readonly itemId: TurnItemId;
    readonly label: string;
    readonly reason: string;
  }>;
  /** Cards asking the user to link a machine; the pairing code never passes through here. */
  readonly pairings: ReadonlyArray<{
    readonly itemId: TurnItemId;
    /** What the agent named: the machine's label, the user's words for it, or its address. */
    readonly label: string | null;
    readonly reason: string;
  }>;
}

export const NO_PROMPTS: LocalTaskPrompts = {
  questions: [],
  approvals: [],
  secrets: [],
  pairings: [],
};

const isApprovalKind = Schema.is(ProviderRequestKind);

export type ChildRecords = Pick<
  OrchestrationV2ThreadProjection,
  "thread" | "runs" | "nodes" | "runtimeRequests" | "turnItems" | "contextTransfers"
>;

/** The records `localPromptsOf` reads, and only the turn items it needs. */
export const readChildRecords = (
  threads: Pick<ThreadManagement.ThreadManagementService["Service"], "getThreadRecords">,
  childThreadId: ThreadId,
) =>
  threads.getThreadRecords(
    childThreadId,
    ["runs", "nodes", "runtimeRequests", "turnItems", "contextTransfers"],
    { turnItemTypes: ["secret_request", "link_request"], runtimeRequestStatuses: ["pending"] },
  );

/**
 * The run an older task with no run marked owns: the one its spawn started, else the thread's
 * first (upstream's `delegatedTaskRun` in OrchestratorMcpService.ts, which has no export).
 */
const originalRun = (task: TaskRef, child: ChildRecords): RunId | undefined => {
  const spawn = child.contextTransfers.find(
    (transfer) =>
      transfer.type === "subagent_spawn" &&
      transfer.sourceThreadId === task.threadId &&
      transfer.targetThreadId === task.childThreadId,
  );
  if (spawn === undefined) return child.runs[0]?.id;
  return spawn.targetRunId ?? undefined;
};

export type TaskRef = Pick<OrchestrationV2Subagent, "id" | "threadId" | "childThreadId">;

/**
 * The prompts of `task` in its thread here, by upstream's `pendingTaskApproval` rule: of its runs
 * that have not ended (a run marked with the task or, for an older task with none marked, only
 * its original run).
 */
export const localPromptsOf = (task: TaskRef, child: ChildRecords): LocalTaskPrompts => {
  const marked = child.runs.some((run) => run.delegatedTaskId === task.id);
  const legacyRun = marked ? undefined : originalRun(task, child);
  const taskRuns = new Set(
    child.runs
      .filter(
        (run) =>
          !ThreadManagement.isTerminalRunStatus(run.status) &&
          (run.delegatedTaskId === task.id ||
            (run.delegatedTaskId === undefined && run.id === legacyRun)),
      )
      .map((run) => run.id),
  );
  const runOf = (nodeId: NodeId) => child.nodes.find((node) => node.id === nodeId)?.runId ?? null;
  const live = child.runtimeRequests.flatMap((request) => {
    const runId = runOf(request.nodeId);
    return request.status === "pending" &&
      request.responseCapability.type === "live" &&
      runId !== null &&
      taskRuns.has(runId)
      ? [{ request, runId }]
      : [];
  });
  const items = child.turnItems.filter((item) => item.runId !== null && taskRuns.has(item.runId));
  return {
    questions: live
      .filter(({ request }) => request.kind === "user_input")
      .map(({ request, runId }) => ({ requestId: request.id, runId })),
    approvals: live
      .flatMap(({ request, runId }) =>
        isApprovalKind(request.kind)
          ? [{ requestId: request.id, kind: request.kind, runId, at: request.createdAt }]
          : [],
      )
      .toSorted((a, b) => DateTime.toEpochMillis(a.at) - DateTime.toEpochMillis(b.at))
      .map(({ requestId, kind, runId }) => ({ requestId, kind, runId })),
    secrets: items.flatMap((item) =>
      item.type === "secret_request" && item.secretStatus === "pending"
        ? [{ itemId: item.id, label: item.label, reason: item.reason }]
        : [],
    ),
    pairings: items.flatMap((item) =>
      item.type === "link_request" && item.linkStatus === "pending"
        ? [
            {
              itemId: item.id,
              label: item.label?.trim() || item.hint?.trim() || item.url?.trim() || null,
              reason: item.reason,
            },
          ]
        : [],
    ),
  };
};

/** An open delegated task whose thread runs on this machine. */
export interface OpenLocalTask {
  readonly parentThreadId: ThreadId;
  readonly taskId: NodeId;
  readonly childThreadId: ThreadId;
}

/**
 * Every app-owned task with a thread here that has no result and has not settled: the tasks whose
 * thread can wait on a person. (Upstream's store reads only the linked ones in one query.)
 */
export const openLocalTasks = Effect.fn("LocalTaskPrompts.openLocalTasks")(function* () {
  const sql = yield* SqlClient.SqlClient;
  const rows = yield* sql<{
    readonly thread_id: string;
    readonly subagent_id: string;
    readonly child_thread_id: string;
  }>`
      SELECT thread_id, subagent_id, child_thread_id FROM orchestration_v2_projection_subagents
      WHERE origin = 'app_owned'
        AND child_thread_id IS NOT NULL
        AND json_type(payload_json, '$.result') IS NOT 'text'
        AND status NOT IN ('completed', 'failed', 'cancelled', 'interrupted')
      ORDER BY thread_id, subagent_id
    `;
  return rows.map((row): OpenLocalTask => ({
    parentThreadId: ThreadId.make(row.thread_id),
    taskId: NodeId.make(row.subagent_id),
    childThreadId: ThreadId.make(row.child_thread_id),
  }));
}, NodeSqliteClient.readOnly);
