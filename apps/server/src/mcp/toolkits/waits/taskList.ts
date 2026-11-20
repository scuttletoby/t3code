/**
 * Fork (issue 386): `task_list`, the calling thread's delegated tasks with where each result stands
 * and how many children run now per place, read from their live status. Read only: reading here
 * marks nothing read; `task_status` does. The rules are in `fork/delegatedResults`.
 */
import {
  EnvironmentId,
  NodeId,
  OrchestratorMcpFailure,
  type OrchestrationV2Subagent,
  ThreadId,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import {
  type ChildActivity,
  countByPlace,
  isDelegatedTask,
  isEndedTask,
  localActivity,
  orphansOf,
  remoteActivity,
  resultDelivery,
  taskWhere,
} from "../../../fork/delegatedResults/delegatedResults.ts";
import {
  LINKED_PAGE_SIZE,
  listLinkedThreads,
} from "../../../fork/delegatedResults/linkedThreads.ts";
import * as PeerForwarding from "../../../peer/PeerForwarding.ts";
import * as RemoteDelegation from "../../../peer/RemoteDelegation.ts";
import type { McpThreadInvocationScope } from "../../McpInvocationContext.ts";
import { readCaller, unavailable } from "../../threadAccess.ts";
import { OrchestratorToolkit } from "../orchestrator/tools.ts";

export const TaskListInput = Schema.Struct({
  includeSettled: Schema.optionalKey(
    Schema.Boolean.annotate({
      description: "Also list read or delivered results.",
    }),
  ),
  checkLinked: Schema.optionalKey(
    Schema.Boolean.annotate({
      description:
        "Also ask linked environments for threads launched from here with no task. Slower.",
    }),
  ),
});
export type TaskListInput = typeof TaskListInput.Type;

const Where = Schema.Union([Schema.Literal("local"), EnvironmentId]);

export const TaskListEntry = Schema.Struct({
  taskId: NodeId,
  title: Schema.NullOr(Schema.String),
  where: Where,
  threadId: ThreadId,
  status: Schema.String,
  /** live: running now; ended; unknown: its environment did not answer. */
  activity: Schema.Literals(["live", "ended", "unknown"]),
  /** owed: ended, result not delivered; settled: delivered or read; none: no record; null: running. */
  result: Schema.NullOr(Schema.Literals(["owed", "settled", "none"])),
  completedAt: Schema.NullOr(Schema.String),
});

export const TaskListResult = Schema.Struct({
  tasks: Schema.Array(TaskListEntry),
  /** Ended tasks whose result never reached this thread. */
  owed: Schema.Number,
  /** Ended tasks with no delivery record (an old row, or a wait that never acknowledged it): read them to be sure. */
  unrecorded: Schema.Number,
  /** Children running now per place, from their live status; `unknown` could not be read. */
  slots: Schema.Array(Schema.Struct({ where: Where, live: Schema.Number, unknown: Schema.Number })),
  /** With `checkLinked`: threads there that name this thread as their parent but have no task. */
  orphans: Schema.optionalKey(
    Schema.Array(
      Schema.Struct({ environmentId: EnvironmentId, threadId: ThreadId, title: Schema.String }),
    ),
  ),
  /** With `checkLinked`: linked environments that did not answer. */
  unreachable: Schema.optionalKey(Schema.Array(EnvironmentId)),
  next: Schema.String,
});
export type TaskListResult = typeof TaskListResult.Type;

/**
 * The longest one linked environment may take to answer before it counts as unknown. After one
 * read there times out, its other tasks read as unknown without asking again.
 */
export const REMOTE_READ_TIMEOUT = Duration.seconds(5);
const REMOTE_READ_CONCURRENCY = 4;
/** Pages of `t3_thread_list` read per linked environment for orphans: its newest 500 threads. */
export const ORPHAN_PAGES = 5;

const iso = (time: DateTime.Utc | null) => (time === null ? null : DateTime.formatIso(time));

export const listTasks = Effect.fn("waits.taskList")(function* (input: TaskListInput) {
  const { scope, threads, caller } = yield* readCaller();
  if (caller === undefined || scope.thread === undefined) {
    return yield* new OrchestratorMcpFailure({
      code: "capability_denied",
      message: "Tasks belong to a thread. This MCP client is not running inside a T3 thread.",
    });
  }
  const threadScope = scope as McpThreadInvocationScope;
  const remote = yield* RemoteDelegation.RemoteDelegation;
  const records = yield* threads
    .getThreadRecords(
      caller.id,
      ["subagents"],
      input.includeSettled === true || input.checkLinked === true
        ? {}
        : { omitSettledSubagents: true },
    )
    .pipe(Effect.mapError(unavailable));
  const all = records.subagents.filter(
    (task) => isDelegatedTask(task) && task.threadId === caller.id,
  );
  const listed =
    input.includeSettled === true ? all : all.filter((task) => resultDelivery(task) !== "settled");

  // Environments where one read timed out: their other tasks are not asked again.
  const silent = new Set<EnvironmentId>();
  const remoteRead = (task: OrchestrationV2Subagent): Effect.Effect<ChildActivity> => {
    const environmentId = task.remoteChild!.environmentId;
    if (silent.has(environmentId)) return Effect.succeed("unknown");
    return remote.runStatus(threadScope, task).pipe(
      Effect.timeoutOption(REMOTE_READ_TIMEOUT),
      Effect.map((status) => {
        if (status._tag === "None") silent.add(environmentId);
        return remoteActivity(status._tag === "Some" ? status.value : null);
      }),
    );
  };
  const activityOf = (task: OrchestrationV2Subagent): Effect.Effect<ChildActivity> =>
    task.childThreadId !== null
      ? threads.getThreadShell(task.childThreadId).pipe(
          Effect.map(localActivity),
          Effect.orElseSucceed(() => "unknown" as const),
        )
      : // A follower recorded the end of a remote task; it is read there only while it is open,
        // or while the stop it owes there is not sent yet (its thread may still run there).
        isEndedTask(task) && task.remoteChild?.stoppedThere !== false
        ? Effect.succeed("ended" as const)
        : remoteRead(task);

  const tasks = yield* Effect.forEach(
    listed,
    (task) =>
      activityOf(task).pipe(
        Effect.map((activity) => ({
          taskId: task.id,
          title: task.title ?? task.remoteChild?.title ?? null,
          where: taskWhere(task),
          threadId: (task.childThreadId ?? task.remoteChild?.threadId)!,
          status: task.status,
          activity,
          result: resultDelivery(task),
          completedAt: iso(task.completedAt),
        })),
      ),
    { concurrency: REMOTE_READ_CONCURRENCY },
  );

  // Slots count every local child, also one of a settled task that was sent more work since.
  const listedChildren = new Set(tasks.map((task) => task.threadId));
  const otherChildren = (yield* threads
    .listAppOwnedChildThreadIds(caller.id)
    .pipe(Effect.orElseSucceed((): ReadonlyArray<ThreadId> => []))).filter(
    (threadId) => !listedChildren.has(threadId),
  );
  const otherActivity = yield* Effect.forEach(
    otherChildren,
    (threadId) =>
      threads.getThreadShell(threadId).pipe(
        Effect.map((shell) => ({ where: "local" as const, activity: localActivity(shell) })),
        Effect.orElseSucceed(() => ({ where: "local" as const, activity: "unknown" as const })),
      ),
    { concurrency: REMOTE_READ_CONCURRENCY },
  );

  const owed = tasks.filter((task) => task.result === "owed").length;
  const unrecorded = tasks.filter((task) => task.result === "none").length;
  const {
    partial,
    ...linked
  }: Partial<Pick<TaskListResult, "orphans" | "unreachable">> & {
    readonly partial?: ReadonlyArray<EnvironmentId>;
  } = input.checkLinked === true ? yield* findOrphans(threadScope, caller.id, all) : {};
  const orphanCount = linked.orphans?.length ?? 0;
  const partialNote =
    partial === undefined || partial.length === 0
      ? ""
      : ` Orphans were looked for only in the newest ${ORPHAN_PAGES * LINKED_PAGE_SIZE} threads of ${partial.join(", ")}.`;
  return {
    tasks,
    owed,
    unrecorded,
    slots: countByPlace([...tasks, ...otherActivity]),
    ...linked,
    next:
      (owed + unrecorded + orphanCount === 0
        ? "Nothing unread. Slots show the children running now."
        : `Read each owed or unrecorded task with task_status; that marks it read.${
            orphanCount === 0
              ? ""
              : " An orphan runs there with no task here: read it with t3_thread_read and its environmentId."
          }`) + partialNote,
  } satisfies TaskListResult;
});

/**
 * Threads in linked environments whose `delegatedFrom` names this thread but that no task here
 * follows: a launch whose record never ran. Reads the newest `ORPHAN_PAGES` pages of each
 * environment's threads; one with more is named in `partial`.
 */
const findOrphans = Effect.fn("waits.taskList.orphans")(function* (
  scope: McpThreadInvocationScope,
  callerId: ThreadId,
  tasks: ReadonlyArray<OrchestrationV2Subagent>,
) {
  const none = { orphans: [], unreachable: [], partial: [] };
  // An agent of a linked environment would be pointed at this machine's links.
  if (scope.linkedEnvironments === false || scope.client?.linked === true) return none;
  const forwarding = yield* PeerForwarding.PeerForwarding;
  const links = yield* forwarding.links(scope).pipe(Effect.orElseSucceed(() => null));
  if (links === null) return none;
  const followed = tasks.flatMap((task) =>
    task.remoteChild === undefined ? [] : [task.remoteChild],
  );
  const unreachable: Array<EnvironmentId> = [];
  const partial: Array<EnvironmentId> = [];
  const orphans: Array<{ environmentId: EnvironmentId; threadId: ThreadId; title: string }> = [];
  yield* Effect.forEach(
    links.links.filter((link) => link.status !== "expired"),
    (link) =>
      listLinkedThreads(
        (cursor) =>
          forwarding.call(scope, OrchestratorToolkit.tools.t3_thread_list, link.environmentId, {
            allProjects: true,
            limit: LINKED_PAGE_SIZE,
            cursor,
          }),
        link.environmentId,
        { maxPages: ORPHAN_PAGES, timeout: REMOTE_READ_TIMEOUT },
      ).pipe(
        Effect.map((read) => {
          if (read.state === "unreachable") unreachable.push(link.environmentId);
          if (read.state === "partial") partial.push(link.environmentId);
          for (const orphan of orphansOf(read.threads, scope.environmentId, callerId, followed)) {
            orphans.push({
              environmentId: orphan.environmentId,
              threadId: orphan.threadId,
              title: orphan.title,
            });
          }
        }),
      ),
    { concurrency: REMOTE_READ_CONCURRENCY, discard: true },
  );
  return { orphans, unreachable, partial };
});
