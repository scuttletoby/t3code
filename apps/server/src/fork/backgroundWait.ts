/**
 * Fork: background wait (F7). A `t3_background_wait` on a task that already ended answers it once
 * under `ended` instead of refusing it as unmatched: the Claude adapter drops ended tasks from its
 * roster, so `remember` keeps a bounded memory of them.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

/** Ended tasks kept across all threads; the oldest goes first. */
const MAX_REMEMBERED = 256;
const MAX_SUMMARY_CHARS = 2_000;

export const EndedTask = Schema.Struct({
  taskId: Schema.String,
  status: Schema.String,
  summary: Schema.optional(Schema.String),
});
export type EndedTask = typeof EndedTask.Type;

export const Success = Schema.Struct({
  matched: Schema.Array(Schema.String),
  unmatched: Schema.Array(Schema.String),
  /** Tasks that had already ended; each is reported once. */
  ended: Schema.optional(Schema.Array(EndedTask)),
});

const remembered = new Map<string, EndedTask>();
const memoryKey = (nativeThreadId: string, taskId: string) => `${nativeThreadId}\u0000${taskId}`;

/** Records the task a `task_notification` ends; any other message is ignored. */
export const remember = (
  nativeThreadId: string,
  message: {
    readonly type: string;
    readonly subtype?: string;
    readonly task_id?: unknown;
    readonly status?: unknown;
    readonly summary?: unknown;
  },
): Effect.Effect<void> =>
  Effect.sync(() => {
    if (message.type !== "system" || message.subtype !== "task_notification") return;
    const taskId = message.task_id;
    if (typeof taskId !== "string" || taskId.length === 0) return;
    const key = memoryKey(nativeThreadId, taskId);
    const summary =
      typeof message.summary === "string" && message.summary.trim().length > 0
        ? message.summary.trim().slice(0, MAX_SUMMARY_CHARS)
        : undefined;
    remembered.delete(key);
    remembered.set(key, {
      taskId,
      status: typeof message.status === "string" ? message.status : "ended",
      ...(summary === undefined ? {} : { summary }),
    });
    for (const oldest of remembered.keys()) {
      if (remembered.size <= MAX_REMEMBERED) break;
      remembered.delete(oldest);
    }
  });

/**
 * Takes the remembered ended tasks among `taskIds` that the adapter did not match, in request
 * order; each is handed back once.
 */
export const read = (
  providerThread: { readonly nativeThreadRef: { readonly nativeId: string } | null },
  taskIds: ReadonlyArray<string>,
  matched: ReadonlyArray<string>,
): Effect.Effect<ReadonlyArray<EndedTask>> =>
  Effect.sync(() => {
    const nativeThreadId = providerThread.nativeThreadRef?.nativeId;
    if (nativeThreadId == null) return [];
    const live = new Set(matched);
    const ended: EndedTask[] = [];
    for (const taskId of new Set(taskIds)) {
      if (live.has(taskId)) continue;
      const key = memoryKey(nativeThreadId, taskId);
      const task = remembered.get(key);
      if (task === undefined) continue;
      remembered.delete(key);
      ended.push(task);
    }
    return ended;
  });

/** The wait's answer when some tasks had already ended: those are neither matched nor unmatched. */
export const result = (
  taskIds: ReadonlyArray<string>,
  matched: ReadonlyArray<string>,
  ended: ReadonlyArray<EndedTask>,
): {
  readonly matched: ReadonlyArray<string>;
  readonly unmatched: ReadonlyArray<string>;
  readonly ended: ReadonlyArray<EndedTask>;
} => {
  const answered = new Set([...matched, ...ended.map((task) => task.taskId)]);
  return { matched, unmatched: taskIds.filter((taskId) => !answered.has(taskId)), ended };
};
