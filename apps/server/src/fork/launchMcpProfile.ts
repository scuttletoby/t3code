/**
 * Fork: a thread started by a narrower thread (`t3_thread_launch`, a `schedule_task` whose runs
 * start new threads, a `t3_thread_transfer` fork) is refused unless its profile is within the
 * starter's; `delegate_task` cuts a child to its parent as before.
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

import { type LineageThread, makeThreadMcpProfiles } from "./claudeMcpProfile.ts";
import { isEveryMcpTool, isWithinMcpProfile, type McpProfileResolved } from "./mcpProfile.ts";

type ProfileFor = Effect.Success<typeof makeThreadMcpProfiles>;

/** The launcher's profile, or a refusal when it cannot be worked out: never `all` on a guess. */
const launcherProfile = (profileFor: ProfileFor, launcher: LineageThread) =>
  profileFor(launcher).pipe(
    Effect.mapError(
      (refusal) =>
        new OrchestratorMcpFailure({
          code: "invalid_request",
          message: `This thread's MCP profile cannot be worked out, so it cannot start another thread. ${refusal.reason}`,
        }),
    ),
  );

const named = (resolved: McpProfileResolved) => `"${resolved.name}"`;

/**
 * Refuses a thread `launcher` starts when `started`, as it would be made, would get anything the
 * launcher's profile does not keep. `what` names the new thread in the refusal ("a thread it
 * starts in that project"); `instead` says how to go on. A launcher on `all` is not checked.
 */
const checkStarted = Effect.fn("LaunchMcpProfile.checkStarted")(function* (
  launcher: LineageThread,
  started: LineageThread,
  what: string,
  instead: (own: string) => string,
) {
  const profileFor = yield* makeThreadMcpProfiles;
  const own = yield* launcherProfile(profileFor, launcher);
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

/**
 * Refuses a launch from a thread with a narrower profile than `all` when the new thread, a
 * top-level thread in `projectId`, would get anything the launcher's profile does not keep. A
 * call with no calling thread (the owner's own client, or a task another machine delegated, which
 * carries its profile) is not checked here. A retry checks again, against the settings it reads.
 */
export const checkLaunchMcpProfile = Effect.fn("LaunchMcpProfile.check")(function* (
  launcher: OrchestrationV2ThreadShell | undefined,
  launched: { readonly threadId: ThreadId; readonly projectId: ProjectId },
) {
  if (launcher === undefined) return;
  return yield* checkStarted(
    launcher,
    topLevel(launched.threadId, launched.projectId),
    "a thread it starts in that project",
    (own) =>
      `Use delegate_task (its sub-agent keeps this thread's profile), start the thread in a project whose MCP profile is within ${own}, or ask the owner to start it.`,
  );
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
 * `schedule_task` changing a task here: refused when, after the change, its runs start a thread
 * wider than the caller's profile, whoever made it and whatever the change (unbind, rewrite,
 * re-enable). Deleting it or binding it to the caller is the way out; a missing task is the service's.
 */
export const checkScheduleEditMcpProfile = Effect.fn("LaunchMcpProfile.scheduleEdit")(function* (
  scheduledTaskId: ScheduledTaskId,
  bindToCurrentThread: boolean | undefined,
) {
  const { caller } = yield* loadCaller();
  if (caller === undefined || bindToCurrentThread === true) return;
  // Upstream's orchestrator toolkit does not name this service; the server provides it to every
  // toolkit, and without it the task cannot be read, so the change is refused.
  const service = yield* Effect.serviceOption(ScheduledTasks.ScheduledTaskService);
  if (Option.isNone(service)) {
    return yield* new OrchestratorMcpFailure({
      code: "orchestration_error",
      message: "The scheduled task cannot be read here, so it was not changed.",
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
  // The service keeps the stored thread unless the call binds or unbinds it.
  const threadId = bindToCurrentThread === false ? null : task.threadId;
  if (threadId !== null) return;
  return yield* checkStarted(
    caller,
    scheduledRun(task.projectId),
    "a thread this task's runs start in its project",
    (own) => `${SCHEDULE_INSTEAD(own)} delete_scheduled_task removes it. The task was not changed.`,
  );
});

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
) {
  if (launcher === undefined) return;
  const own = yield* launcherProfile(yield* makeThreadMcpProfiles, launcher);
  if (isEveryMcpTool(own.profile)) return;
  return yield* new OrchestratorMcpFailure({
    code: "capability_denied",
    message: `This thread's MCP profile is ${named(own)}, and a thread started on another machine gets that machine's profile, so it was not started. Use delegate_task with environmentId (the task keeps this thread's profile there), or start the thread on this machine.`,
  });
});

/** McpToolAccess.startsThreads' linked-environment check: a narrowed caller is refused first. */
export const checkedElsewhere = <
  C extends { readonly caller: OrchestrationV2ThreadShell | undefined },
  E,
  R,
  A,
  E2,
  R2,
>(
  load: Effect.Effect<C, E, R>,
  then: (caller: C) => Effect.Effect<A, E2, R2>,
) =>
  Effect.flatMap(load, (caller) =>
    Effect.andThen(refuseLaunchElsewhere(caller.caller), then(caller)),
  );
