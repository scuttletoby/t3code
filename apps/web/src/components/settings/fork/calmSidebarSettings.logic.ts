/**
 * Fork (calm sidebar): the Settings list of `threadRoles`, one line per pointer, each marked when
 * its thread cannot hold the role now (the reader's rule: client-runtime's threadRoles.ts). A marked
 * pointer still counts as no role; Remove clears it alone.
 */
import {
  isListedThreadLineage,
  type ServerSettingsPatch,
  type ThreadRoles,
} from "@t3tools/contracts";

/** What the list reads of a thread (a client thread shell fits). */
export interface RoleListThread {
  readonly id: string;
  readonly projectId: string;
  readonly title: string;
  readonly lineage: Parameters<typeof isListedThreadLineage>[0];
  readonly delegatedFrom?: unknown;
  readonly archivedAt: string | null;
  readonly deletedAt?: string | null;
}

/** Why a pointer gives no role now; `null`: it does. */
export type RoleProblem = "missing" | "deleted" | "archived" | "child" | "moved" | null;

export interface RoleListEntry {
  readonly key: string;
  readonly role: "assistant" | "orchestrator";
  /** The project an orchestrator pointer is for. */
  readonly projectId: string | null;
  readonly threadId: string;
  /** The thread's title, or `null` while this client does not have the thread. */
  readonly title: string | null;
  readonly problem: RoleProblem;
}

function problemOf(thread: RoleListThread | undefined, projectId: string | null): RoleProblem {
  if (thread === undefined) return "missing";
  if (thread.deletedAt != null) return "deleted";
  if (!isListedThreadLineage(thread.lineage) || thread.delegatedFrom != null) return "child";
  if (projectId !== null && thread.projectId !== projectId) return "moved";
  if (thread.archivedAt != null) return "archived";
  return null;
}

/** The pointers of one server's roles, the assistant first, orchestrators in the file's order. */
export function roleListEntries(
  roles: ThreadRoles | null | undefined,
  threads: ReadonlyArray<RoleListThread>,
): ReadonlyArray<RoleListEntry> {
  if (roles == null) return [];
  const byId = new Map(threads.map((thread) => [thread.id, thread] as const));
  const entry = (
    role: RoleListEntry["role"],
    projectId: string | null,
    threadId: string,
  ): RoleListEntry => {
    const thread = byId.get(threadId);
    return {
      key: role === "assistant" ? "assistant" : `orchestrator:${projectId}`,
      role,
      projectId,
      threadId,
      title: thread?.title ?? null,
      problem: problemOf(thread, projectId),
    };
  };
  return [
    ...(roles.assistant == null ? [] : [entry("assistant", null, roles.assistant)]),
    ...Object.entries(roles.orchestrators ?? {}).map(([projectId, threadId]) =>
      entry("orchestrator", projectId, threadId),
    ),
  ];
}

const PROBLEM_TEXT: Record<Exclude<RoleProblem, null>, string> = {
  missing: "missing: no such thread here",
  deleted: "missing: the thread was deleted",
  archived: "paused: the thread is archived (unarchive it to bring the role back)",
  child: "not used: a sub-agent, side chat or delegated task cannot hold a role",
  moved: "not used: the thread is in another project now",
};

/** One entry as the list shows it. */
export function roleEntryText(
  entry: RoleListEntry,
  projectTitle: (projectId: string) => string | null,
): { readonly role: string; readonly thread: string; readonly problem: string | null } {
  return {
    role:
      entry.role === "assistant"
        ? "Assistant"
        : `Orchestrator of ${projectTitle(entry.projectId!) ?? `project ${entry.projectId}`}`,
    thread: entry.title ?? `thread ${entry.threadId}`,
    problem: entry.problem === null ? null : PROBLEM_TEXT[entry.problem],
  };
}

/** Remove clears this one pointer; a concurrent change to another pointer stays. */
export function removeRolePatch(entry: RoleListEntry): ServerSettingsPatch {
  return (
    entry.role === "assistant"
      ? { threadRoles: { assistant: null } }
      : { threadRoles: { orchestrators: { [entry.projectId!]: null } } }
  ) as ServerSettingsPatch;
}
