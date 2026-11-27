/**
 * Fork (calm sidebar, CAL4): the Settings list of role pointers, each with the thread it names and,
 * when that thread cannot hold the role now, why. The pointer is never changed by reading it: an
 * archived thread holds its role again once unarchived; a person removes a pointer here.
 */
import {
  resolveThreadRoles,
  type ThreadRoleCandidate,
} from "@t3tools/client-runtime/state/thread-roles";
import {
  isListedThreadLineage,
  type ProjectId,
  type ThreadId,
  type ThreadRoles,
} from "@t3tools/contracts";

/** Why a pointer holds no role now; `null`: it does. */
export type RoleEntryProblem = "missing" | "archived" | "not-top-level" | "other-project" | null;

export interface RoleEntry {
  readonly role: "assistant" | "orchestrator";
  /** The orchestrator's project; null for the assistant. */
  readonly projectId: ProjectId | null;
  readonly threadId: ThreadId;
  readonly title: string | null;
  readonly problem: RoleEntryProblem;
}

type Candidate = ThreadRoleCandidate & { readonly title?: string };

function problemOf(thread: Candidate | undefined, projectId: ProjectId | null): RoleEntryProblem {
  if (thread === undefined || thread.deletedAt != null) return "missing";
  if (!isListedThreadLineage(thread.lineage) || thread.delegatedFrom != null) {
    return "not-top-level";
  }
  if (thread.archivedAt != null) return "archived";
  if (projectId !== null && thread.projectId !== projectId) return "other-project";
  return null;
}

/** The assistant first, then each orchestrator as the settings list them. */
export function threadRoleEntries(
  roles: ThreadRoles | undefined,
  threads: ReadonlyMap<ThreadId, Candidate>,
): ReadonlyArray<RoleEntry> {
  if (roles === undefined) return [];
  const resolved = resolveThreadRoles(roles, threads);
  const entry = (
    role: RoleEntry["role"],
    projectId: ProjectId | null,
    threadId: ThreadId,
  ): RoleEntry => {
    const thread = threads.get(threadId);
    const holds =
      role === "assistant"
        ? resolved.assistant === threadId
        : resolved.orchestrators.get(projectId as ProjectId) === threadId;
    return {
      role,
      projectId,
      threadId,
      title: thread?.title ?? null,
      // The shared rule decides; the reason only explains a pointer it set aside.
      problem: holds ? null : (problemOf(thread, projectId) ?? "missing"),
    };
  };
  return [
    ...(roles.assistant === null ? [] : [entry("assistant", null, roles.assistant)]),
    ...Object.entries(roles.orchestrators).map(([projectId, threadId]) =>
      entry("orchestrator", projectId as ProjectId, threadId),
    ),
  ];
}

export const ROLE_PROBLEM_TEXT: Record<Exclude<RoleEntryProblem, null>, string> = {
  missing: "Missing: the thread is deleted or not on this server.",
  archived: "Archived: it holds the role again once unarchived.",
  "not-top-level": "Not a top-level thread: a sub-agent, side chat or delegated task.",
  "other-project": "In another project now.",
};
