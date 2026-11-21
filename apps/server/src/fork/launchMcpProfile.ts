/**
 * Fork: a thread started by a narrower thread (`t3_thread_launch`, `create_threads`, a
 * `schedule_task` whose runs start new threads, a `t3_thread_transfer` fork) is refused unless its
 * profile is within the starter's; `delegate_task` cuts a child to its parent as before. The
 * scheduled task service checks a save and a run again (`scheduledTaskMcpProfile.ts`).
 */
import {
  OrchestratorMcpFailure,
  type OrchestrationV2ThreadShell,
  type ProjectId,
  type ScheduledTaskId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import { loadCaller } from "../mcp/threadAccess.ts";
import * as ScheduledTasks from "../scheduledTasks/ScheduledTaskService.ts";

import {
  type LineageThread,
  makeThreadMcpProfileReads,
  makeThreadMcpProfiles,
} from "./claudeMcpProfile.ts";
import { isEveryMcpTool, isWithinMcpProfile, type McpProfileResolved } from "./mcpProfile.ts";
import { boundToWiderRefusal } from "./scheduledTaskMcpProfile.ts";

type Refusal = { readonly reason: string };

/** The launcher's profile cannot be worked out: a refusal, never `all` on a guess. */
const launcherUnknown = (refusal: Refusal) =>
  new OrchestratorMcpFailure({
    code: "invalid_request",
    message: `This thread's MCP profile cannot be worked out, so it cannot start another thread. ${refusal.reason}`,
  });

const named = (resolved: McpProfileResolved) => `"${resolved.name}"`;

/**
 * Stands in for a new thread whose id is not known before its dispatch (`create_threads`): it has
 * no pick of its own, so its project's or the server's profile applies.
 */
const UNNAMED_NEW_THREAD = ThreadId.make("(new thread)");

/** A thread that launches another, and where: its route words the refusal. */
export interface Launched {
  /** The new thread's id, when known before its dispatch. */
  readonly threadId?: ThreadId | undefined;
  readonly projectId: ProjectId;
  /** `create_threads` starts the thread in the caller's own project. */
  readonly route?: "t3_thread_launch" | "create_threads";
}

/**
 * Refuses a launch whose new top-level thread in `projectId` would get anything the launcher's
 * profile does not keep (both from one settings read). No calling thread (the owner's client, a
 * task delegated here) is not checked. A retry checks again.
 */
export const makeLaunchMcpProfileCheck = Effect.gen(function* () {
  const reads = yield* makeThreadMcpProfileReads;
  return Effect.fn("LaunchMcpProfile.check")(function* (
    launcher: LineageThread | undefined,
    launched: Launched,
  ) {
    if (launcher === undefined) return;
    const profileFor = yield* Effect.mapError(reads, launcherUnknown);
    const own = yield* Effect.mapError(profileFor(launcher), launcherUnknown);
    if (isEveryMcpTool(own.profile)) return;
    const threadId = launched.threadId ?? UNNAMED_NEW_THREAD;
    const theirs = yield* profileFor({
      id: threadId,
      projectId: launched.projectId,
      lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: threadId },
    }).pipe(
      Effect.mapError(
        (refusal) =>
          new OrchestratorMcpFailure({
            code: "invalid_request",
            message: `The new thread's MCP profile cannot be worked out, so it was not started. ${refusal.reason}`,
          }),
      ),
    );
    if (isWithinMcpProfile(theirs.profile, own.profile)) return;
    const instead =
      launched.route === "create_threads"
        ? `Use delegate_task (its sub-agent keeps this thread's profile), or ask the owner to start it.`
        : `Use delegate_task (its sub-agent keeps this thread's profile), start the thread in a project whose MCP profile is within ${named(own)}, or ask the owner to start it.`;
    return yield* new OrchestratorMcpFailure({
      code: "capability_denied",
      message: `This thread's MCP profile is ${named(own)}, and a thread it starts in that project would get ${named(theirs)}, which keeps MCP servers or T3 tools ${named(own)} does not, so it was not started. ${instead}`,
    });
  });
});

/** {@link makeLaunchMcpProfileCheck} for one `t3_thread_launch`, in the handler's services. */
export const checkLaunchMcpProfile = (
  launcher: OrchestrationV2ThreadShell | undefined,
  launched: Launched,
) => Effect.flatMap(makeLaunchMcpProfileCheck, (check) => check(launcher, launched));

/**
 * Refuses a thread `launcher` starts when `started`, as it would be made, would get anything the
 * launcher's profile does not keep (both from one settings read). `what` names the new thread in the refusal ("a thread it
 * starts in that project"); `instead` says how to go on. A launcher on `all` is not checked.
 */
const checkStarted = Effect.fn("LaunchMcpProfile.checkStarted")(function* (
  launcher: LineageThread,
  started: LineageThread,
  what: string,
  instead: (own: string) => string,
) {
  const profileFor = yield* Effect.mapError(yield* makeThreadMcpProfileReads, launcherUnknown);
  const own = yield* Effect.mapError(profileFor(launcher), launcherUnknown);
  if (isEveryMcpTool(own.profile)) return;
  const theirs = yield* profileFor(started).pipe(
    Effect.mapError(
      (refusal) =>
        new OrchestratorMcpFailure({
          code: "invalid_request",
          message: `The new thread's MCP profile cannot be worked out, so it was not started. ${refusal.reason}`,
        }),
    ),
  );
  if (isWithinMcpProfile(theirs.profile, own.profile)) return;
  return yield* new OrchestratorMcpFailure({
    code: "capability_denied",
    message: `This thread's MCP profile is ${named(own)}, and ${what} would get ${named(theirs)}, which keeps MCP servers or T3 tools ${named(own)} does not, so it was not started. ${instead(named(own))}`,
  });
});

/** A top-level thread in `projectId`, as a launch or a scheduled run makes it. */
const topLevel = (threadId: ThreadId, projectId: ProjectId): LineageThread => ({
  id: threadId,
  projectId,
  lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: threadId },
});

/** The thread a scheduled run starts has no id until it runs, and no pick can name it. */
const scheduledRun = (projectId: ProjectId) =>
  topLevel(ThreadId.make(`scheduled-task-run:${projectId}`), projectId);

const SCHEDULE_INSTEAD = (own: string) =>
  `Bind the task to this thread (bindToCurrentThread: true), schedule it in a project whose MCP profile is within ${own}, use delegate_task, or ask the owner to schedule it.`;

/**
 * `schedule_task` creating a task on this machine: a task whose runs start a new top-level thread
 * (not bound to the calling thread) is refused when that thread would get more than the caller's
 * profile. The project and binding are worked out as OrchestratorMcpService.scheduleTask does. A
 * retry with the same `clientRequestId` is checked again, before anything is stored.
 */
export const checkScheduleMcpProfile = Effect.fn("LaunchMcpProfile.schedule")(function* (input: {
  readonly projectId?: ProjectId | undefined;
  readonly bindToCurrentThread?: boolean | undefined;
}) {
  const { caller } = yield* loadCaller();
  if (caller === undefined) return;
  const projectId = input.projectId ?? caller.projectId;
  // Bound to the caller, its runs go into the caller's own thread and keep its profile. A bind
  // to a thread of another project is refused by the service.
  if (input.bindToCurrentThread ?? caller.projectId === projectId) return;
  return yield* checkStarted(
    caller,
    scheduledRun(projectId),
    "a thread this task's runs start in that project",
    SCHEDULE_INSTEAD,
  );
});

/**
 * A task here whose runs, after a change (`bindToCurrentThread` and `prompt` as the call gives
 * them), start a thread wider than the caller's profile is refused, whoever made it, and so is a
 * change to the prompt or thread of a task bound to another, wider thread; `done` ends the refusal.
 */
const checkTaskRuns = Effect.fn("LaunchMcpProfile.taskRuns")(function* (
  scheduledTaskId: ScheduledTaskId,
  bindToCurrentThread: boolean | undefined,
  done: string,
  prompt?: string | undefined,
) {
  const { caller, threads } = yield* loadCaller();
  if (caller === undefined) return;
  const changesBound = prompt !== undefined || bindToCurrentThread !== undefined;
  // Upstream's orchestrator toolkit does not name this service; the server provides it to every
  // toolkit, and without it the task cannot be read, so the call is refused.
  const service = yield* Effect.serviceOption(ScheduledTasks.ScheduledTaskService);
  if (Option.isNone(service)) {
    return yield* new OrchestratorMcpFailure({
      code: "orchestration_error",
      message: `The scheduled task cannot be read here, so ${done}.`,
    });
  }
  const { tasks } = yield* service.value.list().pipe(
    Effect.mapError(
      (error) =>
        new OrchestratorMcpFailure({
          code: "orchestration_error",
          message: `Could not load scheduled task: ${error.message}`,
        }),
    ),
  );
  const task = tasks.find((candidate) => candidate.id === scheduledTaskId);
  if (task === undefined) return;
  // Bound to another thread: the service's rule (scheduledTaskMcpProfile.ts), here too so a refused
  // change does not use up the webhook secretRef it carries (the service consumes it first).
  if (task.threadId !== null && changesBound) {
    const profileFor = yield* Effect.mapError(yield* makeThreadMcpProfileReads, launcherUnknown);
    const own = yield* Effect.mapError(profileFor(caller), launcherUnknown);
    const refusal = yield* boundToWiderRefusal({
      profileFor,
      threads,
      own,
      callerId: caller.id,
      existing: task,
      next: {
        prompt: prompt ?? task.prompt,
        projectId: task.projectId,
        threadId:
          bindToCurrentThread === true
            ? caller.id
            : bindToCurrentThread === false
              ? null
              : task.threadId,
      },
    });
    if (refusal !== null) {
      return yield* new OrchestratorMcpFailure({ code: "capability_denied", message: refusal });
    }
  }
  if (bindToCurrentThread === true) return;
  // The service keeps the stored thread unless the call binds or unbinds it.
  const threadId = bindToCurrentThread === false ? null : task.threadId;
  if (threadId !== null) return;
  return yield* checkStarted(
    caller,
    scheduledRun(task.projectId),
    "a thread this task's runs start in its project",
    (own) => `${SCHEDULE_INSTEAD(own)} delete_scheduled_task removes it. The ${done.slice(4)}.`,
  );
});

/**
 * `schedule_task` changing a scheduled task here (unbind, rewrite, re-enable, pause): refused by
 * the rules above. Deleting it or binding it to the caller is the way out; a missing task is the
 * service's.
 */
export const checkEditMcpProfile = (
  scheduledTaskId: ScheduledTaskId,
  bindToCurrentThread: boolean | undefined,
  prompt: string | undefined,
) => checkTaskRuns(scheduledTaskId, bindToCurrentThread, "the task was not changed", prompt);

/** `run_scheduled_task_now` here: the run starts the task's thread now, so the same rule holds. */
export const checkScheduleRunMcpProfile = (scheduledTaskId: ScheduledTaskId) =>
  checkTaskRuns(scheduledTaskId, undefined, "the task was not run");

/**
 * `t3_thread_transfer` with `action: "fork"`: the fork is a thread whose parent is `source`, so
 * its profile is its project's or the server's cut to the source's, which may be wider than the
 * caller's own when the caller forks another thread. Refused then, before the fork is made.
 */
export const checkForkMcpProfile = Effect.fn("LaunchMcpProfile.fork")(function* (
  source: LineageThread,
  targetThreadId: ThreadId,
) {
  const { caller } = yield* loadCaller();
  if (caller === undefined) return;
  // As ThreadForkService.plan makes it: the source's project, and none of its delegation.
  const fork: LineageThread = {
    id: targetThreadId,
    projectId: source.projectId,
    lineage: {
      parentThreadId: source.id,
      relationshipToParent: "fork",
      rootThreadId: source.lineage.rootThreadId,
    },
  };
  return yield* checkStarted(
    caller,
    fork,
    "a fork of that thread",
    (own) =>
      `Fork this thread instead, or a thread whose MCP profile is within ${own}, use delegate_task, or ask the owner to fork it.`,
  );
});

/**
 * Refuses a thread with a narrower profile than `all` starting a thread on another machine
 * (`t3_thread_launch` or `schedule_task` with `environmentId`): there the thread would get that
 * machine's profile, and a launch carries none. `delegate_task` to that machine carries it.
 */
const refuseLaunchElsewhere = Effect.fn("LaunchMcpProfile.elsewhere")(function* (
  launcher: OrchestrationV2ThreadShell | undefined,
  done = "it was not started",
) {
  if (launcher === undefined) return;
  const own = yield* Effect.mapError((yield* makeThreadMcpProfiles)(launcher), launcherUnknown);
  if (isEveryMcpTool(own.profile)) return;
  return yield* new OrchestratorMcpFailure({
    code: "capability_denied",
    message: `This thread's MCP profile is ${named(own)}, and a thread started on another machine gets that machine's profile, so ${done}. Use delegate_task with environmentId (the task keeps this thread's profile there), or start the thread on this machine.`,
  });
});

/** The callers of `McpToolAccess.startsThreads` that start their threads on this machine. */
const startingHere = new WeakSet<object>();

/**
 * McpToolAccess.startsThreads' own path, before it works out the new thread's modes: the thread
 * starts on this machine, where `checkLaunchMcpProfile` checks it. A caller is loaded per call.
 */
export const startsHere = (caller: object): void => void startingHere.add(caller);

/**
 * McpToolAccess.startsThreads' modes, worked out on both paths: a caller its own path did not mark
 * starts the thread on another machine, which a narrowed caller is refused (fail closed: any other
 * path to the modes is treated as that).
 */
export const refuseUnlessHere = (caller: {
  readonly caller: OrchestrationV2ThreadShell | undefined;
}) => (startingHere.has(caller) ? Effect.void : refuseLaunchElsewhere(caller.caller));

/**
 * A call forwarded to another machine that may start a thread there outside `startsThreads`: a
 * fork, a change to a scheduled task, or running one now. A narrowed caller is refused, as the
 * thread there would get that machine's profile; the call is not sent.
 */
export const checkForwarded = Effect.fn("LaunchMcpProfile.forwarded")(function* (
  tool: string,
  params: object,
) {
  const input = params as { readonly action?: unknown; readonly scheduledTaskId?: unknown };
  const done =
    tool === "t3_thread_transfer" && input.action === "fork"
      ? "it was not forked"
      : tool === "schedule_task" && input.scheduledTaskId !== undefined
        ? "the task was not changed"
        : tool === "run_scheduled_task_now"
          ? "the task was not run"
          : undefined;
  if (done === undefined) return;
  const { caller } = yield* loadCaller();
  return yield* refuseLaunchElsewhere(caller, done);
});
