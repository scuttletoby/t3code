/**
 * Fork (issues 386, 625): pure rules of `task_list` and the delegated result reminder. Upstream's
 * `completionDelivery.state`: `pending`/`claimed` = owed, `delivered`/`acknowledged`/`disposed` =
 * settled. No state (an old row, or a wait that never acknowledged it): nobody knows it was seen.
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

/** Where a task's result stands for its parent. `none`: no delivery state (see above). */
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
 * Unread and recent enough to remind: ended in the last day with its result owed or with no
 * delivery state. The reminder checks only an idle parent, so a `mode: "wait"` task whose wait is
 * still blocking is never reminded; one whose wait acknowledged it is settled. Rows from before
 * upstream kept a delivery state are all older than a day.
 */
export const isRemindable = (task: OrchestrationV2Subagent, nowMs: number): boolean => {
  const delivery = resultDelivery(task);
  if (!isDelegatedTask(task) || (delivery !== "owed" && delivery !== "none")) return false;
  const ended = task.completedAt ?? task.updatedAt;
  return nowMs - DateTime.toEpochMillis(ended) <= REMINDER_MAX_AGE_MS;
};

/**
 * Busy: the parent's run is going or holds a question, so its own run end comes first and checks
 * again. Background work the parent left running (a monitor, a command) does not hold it: upstream
 * counts that as a live child, but no run end may come for hours, and a queued message is safe.
 */
export const parentBusy = (shell: Parameters<typeof isLiveSubagentShell>[0]): boolean =>
  isLiveSubagentShell({ ...shell, pendingBackgroundTasks: [] });

/** Gone: archived or deleted. It is never reminded. */
export const parentGone = (
  shell: Pick<OrchestrationV2ThreadShell, "archivedAt" | "deletedAt"> | null,
): boolean => shell === null || shell.archivedAt !== null || shell.deletedAt !== null;

const REMINDER_PREFIX = "delegated-reminder:";
const ORPHAN_PREFIX = "delegated-orphans:";

const idsFor = (prefix: string, parentThreadId: ThreadId, keys: ReadonlyArray<string>) => {
  const digest = NodeCrypto.createHash("sha256")
    .update([...new Set(keys)].toSorted().join("\n"))
    .digest("hex")
    .slice(0, 16);
  const messageId = `${prefix}${parentThreadId}:${digest}`;
  return { messageId, commandId: `${messageId}:0` };
};

/**
 * The same owed tasks always give the same id, so a check that runs again, after a restart too, is
 * answered from the message or its receipt instead of sending a second reminder.
 */
export const reminderIds = (
  parentThreadId: ThreadId,
  taskIds: ReadonlyArray<NodeId>,
): { readonly messageId: string; readonly commandId: string } =>
  idsFor(REMINDER_PREFIX, parentThreadId, taskIds);

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
    // Upstream's wake can land between the check and this send, so "may not".
    `[delegated results] ${count} ended, but the result may not have reached this thread:`,
    ...lines,
    ...more,
    "Read each with task_status (that marks it read). task_list lists every task with an unread result.",
  ].join("\n");
};

/** A thread in a linked environment whose `delegatedFrom` names a thread here. */
export interface LinkedThread {
  readonly environmentId: EnvironmentId;
  readonly threadId: ThreadId;
  readonly title: string;
  readonly delegatedFrom: { readonly environmentId: EnvironmentId; readonly threadId: ThreadId };
}

const linkedKey = (environmentId: EnvironmentId, threadId: ThreadId) =>
  `${environmentId}\n${threadId}`;

/**
 * Orphans: threads there launched from `parentThreadId` here that no task of it follows, so a
 * launch whose record here never ran (the server stopped half-way through `delegate_task`).
 */
export const orphansOf = (
  listed: ReadonlyArray<LinkedThread>,
  here: EnvironmentId,
  parentThreadId: ThreadId,
  followed: ReadonlyArray<{ readonly environmentId: EnvironmentId; readonly threadId: ThreadId }>,
): ReadonlyArray<LinkedThread> => {
  const known = new Set(followed.map((child) => linkedKey(child.environmentId, child.threadId)));
  return listed.filter(
    (thread) =>
      thread.delegatedFrom.environmentId === here &&
      thread.delegatedFrom.threadId === parentThreadId &&
      !known.has(linkedKey(thread.environmentId, thread.threadId)),
  );
};

/** The same orphans always give the same id, as `reminderIds` does for tasks. */
export const orphanReminderIds = (parentThreadId: ThreadId, orphans: ReadonlyArray<LinkedThread>) =>
  idsFor(
    ORPHAN_PREFIX,
    parentThreadId,
    orphans.map((orphan) => linkedKey(orphan.environmentId, orphan.threadId)),
  );

export const orphanReminderText = (orphans: ReadonlyArray<LinkedThread>): string => {
  const sorted = orphans.toSorted((a, b) =>
    linkedKey(a.environmentId, a.threadId) < linkedKey(b.environmentId, b.threadId) ? -1 : 1,
  );
  const lines = sorted
    .slice(0, MAX_LISTED)
    .map((orphan) => `- ${orphan.threadId} "${orphan.title}" in ${orphan.environmentId}`);
  const more = sorted.length > MAX_LISTED ? [`- and ${sorted.length - MAX_LISTED} more`] : [];
  const count = sorted.length === 1 ? "1 thread" : `${sorted.length} threads`;
  return [
    `[delegated results] ${count} in linked environments were launched from this thread, but no task here follows them (the server stopped during delegate_task), so their results will not arrive:`,
    ...lines,
    ...more,
    "Read each with t3_thread_read and its environmentId; stop one you no longer need with t3_thread_interrupt.",
  ].join("\n");
};
