/**
 * Fork: a launched thread (`t3_thread_launch`, `create_threads`) has no parent, so its profile
 * comes from its project or the server and may be wider than its launcher's. A launch from a
 * narrower thread is refused unless the new thread's profile is within the launcher's;
 * `delegate_task` cuts a child to its parent as before.
 */
import {
  OrchestratorMcpFailure,
  type OrchestrationV2ThreadShell,
  type ProjectId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import {
  type LineageThread,
  makeThreadMcpProfileReads,
  makeThreadMcpProfiles,
} from "./claudeMcpProfile.ts";
import { isEveryMcpTool, isWithinMcpProfile, type McpProfileResolved } from "./mcpProfile.ts";

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
 * Refuses a thread with a narrower profile than `all` starting a thread on another machine
 * (`t3_thread_launch` or `schedule_task` with `environmentId`): there the thread would get that
 * machine's profile, and a launch carries none. `delegate_task` to that machine carries it.
 */
const refuseLaunchElsewhere = Effect.fn("LaunchMcpProfile.elsewhere")(function* (
  launcher: OrchestrationV2ThreadShell | undefined,
) {
  if (launcher === undefined) return;
  const own = yield* Effect.mapError((yield* makeThreadMcpProfiles)(launcher), launcherUnknown);
  if (isEveryMcpTool(own.profile)) return;
  return yield* new OrchestratorMcpFailure({
    code: "capability_denied",
    message: `This thread's MCP profile is ${named(own)}, and a thread started on another machine gets that machine's profile, so it was not started. Use delegate_task with environmentId (the task keeps this thread's profile there), or start the thread on this machine.`,
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
