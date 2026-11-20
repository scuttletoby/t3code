/**
 * Fork: a launched thread has no parent, so its profile comes from its project or the server and
 * may be wider than its launcher's. A launch from a narrower thread is refused unless the new
 * thread's profile is within the launcher's; `delegate_task` cuts a child to its parent as before.
 */
import {
  OrchestratorMcpFailure,
  type OrchestrationV2ThreadShell,
  type ProjectId,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";

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
  const profileFor = yield* makeThreadMcpProfiles;
  const own = yield* launcherProfile(profileFor, launcher);
  if (isEveryMcpTool(own.profile)) return;
  const thread: LineageThread = {
    id: launched.threadId,
    projectId: launched.projectId,
    lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: launched.threadId },
  };
  const theirs = yield* profileFor(thread).pipe(
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
    message: `This thread's MCP profile is ${named(own)}, and a thread it starts in that project would get ${named(theirs)}, which keeps MCP servers or T3 tools ${named(own)} does not, so it was not started. Use delegate_task (its sub-agent keeps this thread's profile), start the thread in a project whose MCP profile is within ${named(own)}, or ask the owner to start it.`,
  });
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
