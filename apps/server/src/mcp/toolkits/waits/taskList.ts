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
  countByPlace,
  isDelegatedTask,
  isEndedTask,
  localActivity,
  remoteActivity,
  resultDelivery,
  taskWhere,
} from "../../../fork/delegatedResults/delegatedResults.ts";
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
  /** Ended tasks with no delivery record (from before it was kept): read them to be sure. */
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

/** The longest one linked environment may take to answer before it counts as unknown. */
export const REMOTE_READ_TIMEOUT = Duration.seconds(5);
const REMOTE_READ_CONCURRENCY = 4;

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

  const activityOf = (task: OrchestrationV2Subagent) =>
    task.childThreadId !== null
      ? threads.getThreadShell(task.childThreadId).pipe(
          Effect.map(localActivity),
          Effect.orElseSucceed(() => "unknown" as const),
        )
      : // A follower already recorded the end of a remote task; only an open one is read there.
        isEndedTask(task)
        ? Effect.succeed("ended" as const)
        : remote.runStatus(threadScope, task).pipe(
            Effect.timeoutOption(REMOTE_READ_TIMEOUT),
            Effect.map((status) => remoteActivity(status._tag === "Some" ? status.value : null)),
          );

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
  const linked: Partial<Pick<TaskListResult, "orphans" | "unreachable">> =
    input.checkLinked === true ? yield* findOrphans(threadScope, caller.id, all) : {};
  const orphanCount = linked.orphans?.length ?? 0;
  return {
    tasks,
    owed,
    unrecorded,
    slots: countByPlace([...tasks, ...otherActivity]),
    ...linked,
    next:
      owed + unrecorded + orphanCount === 0
        ? "Nothing unread. Slots show the children running now."
        : `Read each owed or unrecorded task with task_status; that marks it read.${
            orphanCount === 0
              ? ""
              : " An orphan runs there with no task here: read it with t3_thread_read and its environmentId."
          }`,
  } satisfies TaskListResult;
});

/**
 * Threads in linked environments whose `delegatedFrom` names this thread but that no task here
 * follows: a launch whose record never ran. Reads the newest 100 threads of each environment.
 */
const findOrphans = Effect.fn("waits.taskList.orphans")(function* (
  scope: McpThreadInvocationScope,
  callerId: ThreadId,
  tasks: ReadonlyArray<OrchestrationV2Subagent>,
) {
  // An agent of a linked environment would be pointed at this machine's links.
  if (scope.linkedEnvironments === false || scope.client?.linked === true) {
    return { orphans: [], unreachable: [] };
  }
  const forwarding = yield* PeerForwarding.PeerForwarding;
  const links = yield* forwarding.links(scope).pipe(Effect.orElseSucceed(() => null));
  if (links === null) return { orphans: [], unreachable: [] };
  const known = new Set(
    tasks.flatMap((task) =>
      task.remoteChild === undefined
        ? []
        : [`${task.remoteChild.environmentId}\n${task.remoteChild.threadId}`],
    ),
  );
  const unreachable: Array<EnvironmentId> = [];
  const orphans: Array<{ environmentId: EnvironmentId; threadId: ThreadId; title: string }> = [];
  yield* Effect.forEach(
    links.links.filter((link) => link.status !== "expired"),
    (link) =>
      forwarding
        .call(scope, OrchestratorToolkit.tools.t3_thread_list, link.environmentId, {
          allProjects: true,
          limit: 100,
        })
        .pipe(
          Effect.timeoutOption(REMOTE_READ_TIMEOUT),
          Effect.map((listed) => {
            if (listed._tag === "None") return void unreachable.push(link.environmentId);
            for (const thread of listed.value.threads) {
              const from = thread.delegatedFrom;
              if (
                from !== undefined &&
                from.environmentId === scope.environmentId &&
                from.threadId === callerId &&
                !known.has(`${link.environmentId}\n${thread.threadId}`)
              ) {
                orphans.push({
                  environmentId: link.environmentId,
                  threadId: thread.threadId,
                  title: thread.title,
                });
              }
            }
          }),
          Effect.orElseSucceed(() => void unreachable.push(link.environmentId)),
        ),
    { concurrency: REMOTE_READ_CONCURRENCY, discard: true },
  );
  return { orphans, unreachable };
});
