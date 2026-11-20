/**
 * Fork (issue 386): pure rules of `task_list` and the delegated result reminder. Upstream's
 * `completionDelivery.state`: `pending`/`claimed` = owed, `delivered`/`acknowledged`/`disposed` =
 * settled, no state = a row from before it was kept (nothing says whether it was seen).
 */
// @effect-diagnostics-next-line nodeBuiltinImport:off -- The reminder id must be a deterministic digest.
import * as NodeCrypto from "node:crypto";

import {
  isLiveSubagentShell,
  type EnvironmentId,
  type NodeId,
  type OrchestrationV2RunStatus,
  type OrchestrationV2Subagent,
  type OrchestrationV2ThreadShell,
  type ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

/** Where a task's result stands for its parent. `none`: no delivery state was recorded. */
export type ResultDelivery = "owed" | "settled" | "none";

/** Live: running there now. Ended: not running. Unknown: its environment could not be read. */
export type ChildActivity = "live" | "ended" | "unknown";

const TERMINAL_TASK_STATUSES: ReadonlySet<OrchestrationV2Subagent["status"]> = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

const LIVE_RUN_STATUSES: ReadonlySet<OrchestrationV2RunStatus> = new Set([
  "preparing",
  "queued",
  "starting",
  "running",
  "waiting",
]);

/** A task this thread delegated with `delegate_task`: a child here or a thread in a linked one. */
export const isDelegatedTask = (task: OrchestrationV2Subagent): boolean =>
  task.origin === "app_owned" && (task.childThreadId !== null || task.remoteChild !== undefined);

export const isEndedTask = (task: Pick<OrchestrationV2Subagent, "status">): boolean =>
  TERMINAL_TASK_STATUSES.has(task.status);

export const resultDelivery = (
  task: Pick<OrchestrationV2Subagent, "status" | "completionDelivery">,
): ResultDelivery | null => {
  if (!isEndedTask(task)) return null;
  const state = task.completionDelivery?.state;
  if (state === undefined) return "none";
  return state === "pending" || state === "claimed" ? "owed" : "settled";
};

/** A local child from its thread shell, upstream's own rule for a live child row. */
export const localActivity = (
  shell: Parameters<typeof isLiveSubagentShell>[0] | null,
): ChildActivity => (shell === null ? "unknown" : isLiveSubagentShell(shell) ? "live" : "ended");

/** A remote child from its run's status there; null: the environment did not answer. */
export const remoteActivity = (status: OrchestrationV2RunStatus | null): ChildActivity =>
  status === null ? "unknown" : LIVE_RUN_STATUSES.has(status) ? "live" : "ended";

/** Where a task runs: `local` for a child thread here, else the linked environment's id. */
export const taskWhere = (task: OrchestrationV2Subagent): "local" | EnvironmentId =>
  task.childThreadId !== null ? "local" : (task.remoteChild?.environmentId ?? "local");

export interface PlaceCount {
  readonly where: "local" | EnvironmentId;
  readonly live: number;
  readonly unknown: number;
}

/** Live and unread children per place, from their live activity only (never from any notes). */
export const countByPlace = (
  rows: ReadonlyArray<{
    readonly where: "local" | EnvironmentId;
    readonly activity: ChildActivity;
  }>,
): ReadonlyArray<PlaceCount> => {
  const counts = new Map<string, { live: number; unknown: number }>();
  for (const row of rows) {
    const count = counts.get(row.where) ?? { live: 0, unknown: 0 };
    if (row.activity === "live") count.live += 1;
    if (row.activity === "unknown") count.unknown += 1;
    counts.set(row.where, count);
  }
  return [...counts.entries()]
    .filter(([, count]) => count.live > 0 || count.unknown > 0)
    .map(([where, count]) => ({ where: where as "local" | EnvironmentId, ...count }))
    .toSorted((a, b) =>
      a.where === "local" ? -1 : b.where === "local" ? 1 : a.where < b.where ? -1 : 1,
    );
};

/** A reminder looks back this far; an older owed result belongs to an abandoned thread. */
export const REMINDER_MAX_AGE_MS = 24 * 60 * 60 * 1000;

/**
 * Owed and recent enough to remind: ended with an owed delivery state in the last day. Tasks with
 * no delivery state (rows from before upstream kept one) are never reminded.
 */
export const isRemindable = (task: OrchestrationV2Subagent, nowMs: number): boolean => {
  if (!isDelegatedTask(task) || resultDelivery(task) !== "owed") return false;
  const ended = task.completedAt ?? task.updatedAt;
  return nowMs - DateTime.toEpochMillis(ended) <= REMINDER_MAX_AGE_MS;
};

/** Busy: the parent is working or holds a question, so its own turn end comes first. */
export const parentBusy = (shell: Parameters<typeof isLiveSubagentShell>[0]): boolean =>
  isLiveSubagentShell(shell);

/** Gone: archived or deleted. It is never reminded. */
export const parentGone = (
  shell: Pick<OrchestrationV2ThreadShell, "archivedAt" | "deletedAt"> | null,
): boolean => shell === null || shell.archivedAt !== null || shell.deletedAt !== null;

const REMINDER_PREFIX = "delegated-reminder:";

/**
 * The same owed tasks always give the same id, so a check that runs again, after a restart too, is
 * answered from the message or its receipt instead of sending a second reminder.
 */
export const reminderIds = (
  parentThreadId: ThreadId,
  taskIds: ReadonlyArray<NodeId>,
): { readonly messageId: string; readonly commandId: string } => {
  const digest = NodeCrypto.createHash("sha256")
    .update([...new Set(taskIds)].toSorted().join("\n"))
    .digest("hex")
    .slice(0, 16);
  const messageId = `${REMINDER_PREFIX}${parentThreadId}:${digest}`;
  return { messageId, commandId: `${messageId}:0` };
};

const MAX_LISTED = 10;

export const reminderText = (tasks: ReadonlyArray<OrchestrationV2Subagent>): string => {
  const sorted = tasks.toSorted((a, b) => (a.id < b.id ? -1 : 1));
  const lines = sorted.slice(0, MAX_LISTED).map((task) => {
    const title = task.title ?? task.remoteChild?.title ?? task.remoteChild?.label ?? "untitled";
    return `- ${task.id} "${title}" (${task.status})`;
  });
  const more = sorted.length > MAX_LISTED ? [`- and ${sorted.length - MAX_LISTED} more`] : [];
  const count = sorted.length === 1 ? "1 delegated task" : `${sorted.length} delegated tasks`;
  return [
    `[delegated results] ${count} ended, but the result never reached this thread:`,
    ...lines,
    ...more,
    "Read each with task_status (that marks it read). task_list lists every task with an unread result.",
  ].join("\n");
};
