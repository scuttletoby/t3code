/**
 * Fork (calm sidebar): the `threadRoles` save check. Only pointers a save sets anew are checked.
 * Threads come from {@link ThreadRoleTargets} (threadRoleTargetsLayer.ts); this file imports no
 * orchestration code. Without that service a new pointer is refused: fail closed.
 */
import type { ServerSettings, ThreadId, ThreadRoles } from "@t3tools/contracts";
import { Context, Effect, Option } from "effect";

/** What the check needs to know about a thread. */
export interface ThreadRoleTarget {
  readonly projectId: string;
  /** False for a sub-agent or side chat (upstream's `isListedThreadLineage`). */
  readonly listed: boolean;
  /** True for a task a linked machine delegated here. */
  readonly delegated: boolean;
}

export interface ThreadRoleTargetsShape {
  /** The thread, or `null` when it does not exist or is deleted. Fails when it cannot be read. */
  readonly find: (threadId: ThreadId) => Effect.Effect<ThreadRoleTarget | null, Error>;
}

export class ThreadRoleTargets extends Context.Service<ThreadRoleTargets, ThreadRoleTargetsShape>()(
  "t3/fork/threadRoleTargets",
) {}

/** A role pointer a save sets anew. */
export type NewThreadRolePointer =
  | { readonly role: "assistant"; readonly threadId: ThreadId }
  | { readonly role: "orchestrator"; readonly projectId: string; readonly threadId: ThreadId };

/** The pointers `next` sets that `current` did not hold (a removal sets none). */
export function newThreadRolePointers(
  current: ThreadRoles,
  next: ThreadRoles,
): ReadonlyArray<NewThreadRolePointer> {
  const pointers: Array<NewThreadRolePointer> = [];
  if (next.assistant !== null && next.assistant !== current.assistant) {
    pointers.push({ role: "assistant", threadId: next.assistant });
  }
  for (const [projectId, threadId] of Object.entries(next.orchestrators)) {
    const before = Object.hasOwn(current.orchestrators, projectId)
      ? current.orchestrators[projectId as keyof ThreadRoles["orchestrators"]]
      : undefined;
    if (threadId !== before) pointers.push({ role: "orchestrator", projectId, threadId });
  }
  return pointers;
}

const describe = (pointer: NewThreadRolePointer): string =>
  pointer.role === "assistant"
    ? "threadRoles.assistant"
    : `threadRoles.orchestrators["${pointer.projectId}"]`;

/** Why `target` cannot hold the role `pointer` gives it, or `undefined`. */
function threadRolePointerProblem(
  pointer: NewThreadRolePointer,
  target: ThreadRoleTarget | null,
): string | undefined {
  const where = describe(pointer);
  if (target === null) {
    return `${where} names thread ${pointer.threadId}, which does not exist or is deleted: pick a thread that exists.`;
  }
  if (!target.listed) {
    return `${where} names thread ${pointer.threadId}, which is a sub-agent or side chat: only a top-level thread can hold a role.`;
  }
  if (target.delegated) {
    return `${where} names thread ${pointer.threadId}, which is a task delegated from a linked machine: only a top-level thread can hold a role.`;
  }
  if (pointer.role === "orchestrator" && target.projectId !== pointer.projectId) {
    return `${where} names thread ${pointer.threadId}, which belongs to project ${target.projectId}: a project's orchestrator must be one of its own threads.`;
  }
  return undefined;
}

/**
 * Why this save of `threadRoles` must be refused, or `undefined`. Reads only the threads the save
 * points at anew; a save that sets no new pointer reads nothing.
 */
export function threadRolesSaveProblem(
  current: Pick<ServerSettings, "threadRoles">,
  next: Pick<ServerSettings, "threadRoles">,
): Effect.Effect<string | undefined> {
  const pointers = newThreadRolePointers(current.threadRoles, next.threadRoles);
  if (pointers.length === 0) return Effect.undefined;
  return Effect.gen(function* () {
    const targets = yield* Effect.serviceOption(ThreadRoleTargets);
    if (Option.isNone(targets)) {
      return `${describe(pointers[0]!)} cannot be checked here (this server path cannot read threads): set roles from the app's settings.`;
    }
    for (const pointer of pointers) {
      const target = yield* targets.value.find(pointer.threadId).pipe(Effect.option);
      if (Option.isNone(target)) {
        return `${describe(pointer)}: thread ${pointer.threadId} could not be read, so the role was not saved: try again.`;
      }
      const problem = threadRolePointerProblem(pointer, target.value);
      if (problem !== undefined) return problem;
    }
    return undefined;
  });
}
