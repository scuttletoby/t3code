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
  NodeId,
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

/** The start of every reminder id, of both formats (see `legacyReminderSetId`). */
export const REMINDER_ID_PREFIX = "delegated-reminder:";
const reminderPrefix = (parentThreadId: ThreadId) => `${REMINDER_ID_PREFIX}${parentThreadId}:`;
const TASK_KEY = /^[0-9a-f]{12}$/;

const digestOf = (keys: ReadonlyArray<string>) =>
  NodeCrypto.createHash("sha256")
    .update([...new Set(keys)].toSorted().join("\n"))
    .digest("hex");

const idsFor = (prefix: string, parentThreadId: ThreadId, keys: ReadonlyArray<string>) => {
  const messageId = `${prefix}${parentThreadId}:${digestOf(keys).slice(0, 16)}`;
  return { messageId, commandId: `${messageId}:0` };
};

/** One task's own key: the same task always gives the same key. */
export const reminderKey = (taskId: NodeId): string => digestOf([taskId]).slice(0, 12);

/** At most this many tasks in one reminder, each listed by name. */
export const REMINDERS_PER_MESSAGE = 10;

/** A reminder's text lists this many tasks by name (a batch is never longer). */
const MAX_LISTED = 10;

/**
 * A reminder's ids name the key of each task it tells about, so the parent's message and receipt
 * ids are the record of what it was told: a task is reminded once, also when the owed set grows
 * (another task ends while one reminded is still unread), a check runs again, or after a restart.
 */
export const reminderBatchIds = (
  parentThreadId: ThreadId,
  taskIds: ReadonlyArray<NodeId>,
): { readonly messageId: string; readonly commandId: string } => {
  const keys = [...new Set(taskIds.map(reminderKey))].toSorted();
  const messageId = `${reminderPrefix(parentThreadId)}${keys.join(".")}`;
  return { messageId, commandId: `${messageId}:0` };
};

/** The task keys that earlier message or command ids of this parent named. */
export const toldReminderKeys = (
  parentThreadId: ThreadId,
  ids: ReadonlyArray<string>,
): ReadonlySet<string> => {
  const prefix = reminderPrefix(parentThreadId);
  const told = new Set<string>();
  for (const id of ids) {
    if (!id.startsWith(prefix)) continue;
    for (const key of id.slice(prefix.length).replace(/:0$/, "").split(".")) {
      if (TASK_KEY.test(key)) told.add(key);
    }
  }
  return told;
};

/**
 * Ids of the first reminder format: one message for a whole owed set (a 16-digit digest, never a
 * task key). Its message's text is split into the tasks it told (`legacyReminderKeys`). With no
 * text to read (only its receipt is left), a message of that id for exactly the owed set, or for
 * one task alone, means those were told.
 */
export const legacyReminderSetId = (parentThreadId: ThreadId, taskIds: ReadonlyArray<NodeId>) =>
  idsFor(REMINDER_ID_PREFIX, parentThreadId, taskIds).messageId;

const LEGACY_SET_DIGEST = /^[0-9a-f]{16}$/;

/** The first-format message ids among this parent's sent ids (receipts' command ids included). */
export const legacyReminderMessageIds = (
  parentThreadId: ThreadId,
  sentIds: ReadonlyArray<string>,
): ReadonlyArray<string> => {
  const prefix = reminderPrefix(parentThreadId);
  const ids = sentIds
    .map((id) => id.replace(/:0$/, ""))
    .filter((id) => id.startsWith(prefix) && LEGACY_SET_DIGEST.test(id.slice(prefix.length)));
  return [...new Set(ids)].toSorted();
};

const LEGACY_HEADER = /^\[delegated results\] (\d+) delegated tasks? ended, /;
const LEGACY_TASK_LINE = /^- (\S+) "/;
const LEGACY_MORE_LINE = /^- and (\d+) more$/;

/**
 * The keys of the tasks a first-format reminder told, from its text (sorted `- <id> "..."` lines,
 * ten at most), for this parent's own first-format id only. Up to ten, the ids must give its id
 * back; over ten, the ten listed count (the rest were never named). Else it gives nothing.
 */
export const legacyReminderKeys = (
  parentThreadId: ThreadId,
  messageId: string,
  text: string,
): ReadonlyArray<string> => {
  if (!legacyReminderMessageIds(parentThreadId, [messageId]).includes(messageId)) return [];
  const lines = text.split("\n");
  const header = LEGACY_HEADER.exec(lines[0] ?? "");
  if (header === null) return [];
  const count = Number(header[1]);
  const listed = lines.flatMap((line) => {
    const match = LEGACY_TASK_LINE.exec(line);
    return match === null ? [] : [match[1]!];
  });
  const more = lines.flatMap((line) => {
    const match = LEGACY_MORE_LINE.exec(line);
    return match === null ? [] : [Number(match[1])];
  });
  const sorted = listed.every((id, index) => index === 0 || listed[index - 1]! < id);
  if (!sorted || listed.length !== Math.min(count, MAX_LISTED)) return [];
  if (count <= MAX_LISTED) {
    const exact = legacyReminderSetId(
      parentThreadId,
      listed.map((id) => NodeId.make(id)),
    );
    if (more.length !== 0 || exact !== messageId) return [];
    // Over ten, no digest check: it covers the unnamed rest too. No risk added: a writer of this
    // parent's reminder ids can already mark any task told with its one-task id.
  } else if (more.length !== 1 || more[0] !== count - MAX_LISTED) {
    return [];
  }
  return listed.map((id) => reminderKey(NodeId.make(id)));
};

/**
 * The owed tasks not reminded yet, sorted, in messages of at most `REMINDERS_PER_MESSAGE`.
 * `legacyTexts`: the text of each first-format message still in the thread, by message id.
 */
export const reminderBatches = <T extends Pick<OrchestrationV2Subagent, "id">>(
  parentThreadId: ThreadId,
  owed: ReadonlyArray<T>,
  sentIds: ReadonlyArray<string>,
  legacyTexts: ReadonlyMap<string, string> = new Map(),
): ReadonlyArray<ReadonlyArray<T>> => {
  const sent = new Set(sentIds.map((id) => id.replace(/:0$/, "")));
  if (
    owed.length > 0 &&
    sent.has(
      legacyReminderSetId(
        parentThreadId,
        owed.map((t) => t.id),
      ),
    )
  ) {
    return [];
  }
  const told = new Set(toldReminderKeys(parentThreadId, sentIds));
  for (const [messageId, text] of legacyTexts) {
    for (const key of legacyReminderKeys(parentThreadId, messageId, text)) told.add(key);
  }
  const fresh = [...new Map(owed.map((task) => [task.id, task])).values()]
    .filter(
      (task) =>
        !told.has(reminderKey(task.id)) &&
        !sent.has(legacyReminderSetId(parentThreadId, [task.id])),
    )
    .toSorted((a, b) => (a.id < b.id ? -1 : 1));
  const batches: Array<ReadonlyArray<T>> = [];
  for (let start = 0; start < fresh.length; start += REMINDERS_PER_MESSAGE) {
    batches.push(fresh.slice(start, start + REMINDERS_PER_MESSAGE));
  }
  return batches;
};

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

/** The start of every orphan reminder id, of both formats (see `legacyOrphanSetId`). */
export const ORPHAN_ID_PREFIX = "delegated-orphan";
const orphanPrefix = (parentThreadId: ThreadId) => `${ORPHAN_ID_PREFIX}:${parentThreadId}:`;
const ORPHAN_KEY = /^[0-9a-f]{12}$/;

/** One orphan's own key: the same thread there always gives the same key. */
export const orphanKey = (orphan: Pick<LinkedThread, "environmentId" | "threadId">): string =>
  digestOf([linkedKey(orphan.environmentId, orphan.threadId)]).slice(0, 12);

/** At most this many orphans in one message, each listed by name. */
export const ORPHANS_PER_MESSAGE = 10;

/**
 * A message's ids name the key of each orphan it tells about, so the parent's message and receipt
 * ids are the record of who was told: an orphan is never told twice, also when the set grows
 * (a machine that answers late) or the one-day window moves (a restart).
 */
export const orphanBatchIds = (
  parentThreadId: ThreadId,
  batch: ReadonlyArray<Pick<LinkedThread, "environmentId" | "threadId">>,
) => {
  const keys = [...new Set(batch.map(orphanKey))].toSorted();
  const messageId = `${orphanPrefix(parentThreadId)}${keys.join(".")}`;
  return { messageId, commandId: `${messageId}:0` };
};

/** The orphan keys that earlier message or command ids of this parent named. */
export const toldOrphanKeys = (
  parentThreadId: ThreadId,
  ids: ReadonlyArray<string>,
): ReadonlySet<string> => {
  const prefix = orphanPrefix(parentThreadId);
  const told = new Set<string>();
  for (const id of ids) {
    if (!id.startsWith(prefix)) continue;
    for (const key of id.slice(prefix.length).replace(/:0$/, "").split(".")) {
      if (ORPHAN_KEY.test(key)) told.add(key);
    }
  }
  return told;
};

/**
 * The id of the first orphan scan's one message for a whole orphan set (before each orphan had
 * its own key). A message of that id for exactly this set means all of it was told.
 */
export const legacyOrphanSetId = (
  parentThreadId: ThreadId,
  orphans: ReadonlyArray<Pick<LinkedThread, "environmentId" | "threadId">>,
): string =>
  idsFor(
    `${ORPHAN_ID_PREFIX}s:`,
    parentThreadId,
    orphans.map((orphan) => linkedKey(orphan.environmentId, orphan.threadId)),
  ).messageId;

/** The orphans not told yet, sorted, in messages of at most `ORPHANS_PER_MESSAGE`. */
export const orphanBatches = (
  parentThreadId: ThreadId,
  orphans: ReadonlyArray<LinkedThread>,
  sentIds: ReadonlyArray<string>,
): ReadonlyArray<ReadonlyArray<LinkedThread>> => {
  if (orphans.length > 0 && sentIds.includes(legacyOrphanSetId(parentThreadId, orphans))) {
    return [];
  }
  const told = toldOrphanKeys(parentThreadId, sentIds);
  const fresh = orphans
    .filter((orphan) => !told.has(orphanKey(orphan)))
    .toSorted((a, b) =>
      linkedKey(a.environmentId, a.threadId) < linkedKey(b.environmentId, b.threadId) ? -1 : 1,
    );
  const batches: Array<ReadonlyArray<LinkedThread>> = [];
  for (let start = 0; start < fresh.length; start += ORPHANS_PER_MESSAGE) {
    batches.push(fresh.slice(start, start + ORPHANS_PER_MESSAGE));
  }
  return batches;
};

/** One message's text; a batch is at most `ORPHANS_PER_MESSAGE` long, so every orphan is named. */
export const orphanReminderText = (orphans: ReadonlyArray<LinkedThread>): string => {
  const lines = orphans
    .toSorted((a, b) =>
      linkedKey(a.environmentId, a.threadId) < linkedKey(b.environmentId, b.threadId) ? -1 : 1,
    )
    .map((orphan) => `- ${orphan.threadId} "${orphan.title}" in ${orphan.environmentId}`);
  const one = orphans.length === 1;
  return [
    // Neutral on why: the server stopped during delegate_task, or the record failed (that stops
    // the thread there, and the agent saw an error then).
    `[delegated results] ${one ? "1 thread" : `${orphans.length} threads`} in linked environments ${one ? "was" : "were"} launched from this thread, but no task here follows ${one ? "it" : "them"} (delegate_task did not record ${one ? "it" : "them"} here), so ${one ? "its result" : "their results"} will not arrive:`,
    ...lines,
    "Read each with t3_thread_read and its environmentId; stop one you no longer need with t3_thread_interrupt.",
  ].join("\n");
};
