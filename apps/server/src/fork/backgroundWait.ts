/**
 * Fork: background wait (F7). `t3_background_wait` takes `taskId` as well as `taskIds`, and a task
 * that already ended is answered once under `ended` instead of refused as unmatched: the Claude
 * adapter drops ended tasks from its roster, so `remember` keeps a bounded memory of them.
 */
import { TrimmedNonEmptyString } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

const MAX_TASK_IDS = 20;
/** Ended tasks kept across all threads; the oldest goes first. */
const MAX_REMEMBERED = 256;
const MAX_SUMMARY_CHARS = 2_000;

const TaskIds = Schema.Array(Schema.String).check(
  Schema.isMinLength(1),
  Schema.isMaxLength(MAX_TASK_IDS),
);

/** Either `taskIds` or `taskId` (or both); decodes to the `{ taskIds, waiting? }` upstream reads. */
export const Parameters = Schema.Struct({
  taskIds: Schema.optional(
    Schema.Array(TrimmedNonEmptyString).check(Schema.isMaxLength(MAX_TASK_IDS)),
  ),
  taskId: Schema.optional(TrimmedNonEmptyString).annotate({
    description: "One task ID; the same as `taskIds: [taskId]`.",
  }),
  waiting: Schema.optional(Schema.Boolean),
}).pipe(
  Schema.decodeTo(
    Schema.Struct({ taskIds: TaskIds, waiting: Schema.optional(Schema.Boolean) }),
    SchemaTransformation.transform({
      decode: ({ taskIds, taskId, waiting }) => ({
        taskIds: [...new Set([...(taskIds ?? []), ...(taskId === undefined ? [] : [taskId])])],
        ...(waiting === undefined ? {} : { waiting }),
      }),
      encode: ({ taskIds, waiting }) => ({
        taskIds,
        ...(waiting === undefined ? {} : { waiting }),
      }),
    }),
  ),
);

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
