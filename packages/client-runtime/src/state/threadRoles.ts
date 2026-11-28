/**
 * Fork (calm sidebar): which listed threads hold a role, and how the calm view groups a list.
 * Roles are the `threadRoles` setting's pointers (docs/fork/README.md), worked out on each read and
 * never stored. A pointer to a missing, deleted, archived or child thread, or to another project's
 * thread for an orchestrator, counts as no role; the pointer itself is left alone.
 */
import {
  type EnvironmentId,
  isListedThreadLineage,
  type OrchestrationV2ThreadShell,
  type ProjectId,
  type ThreadId,
  type ThreadRoles,
} from "@t3tools/contracts";
import { Atom } from "effect/reactivity";

import { type EnvironmentCatalogState, enabledEnvironmentIds } from "./connections.ts";

/** A thread's role. `child`: a sub-agent, side chat or delegated task, shown inside its parent. */
export type ThreadRole = "assistant" | "orchestrator" | "child" | "plain";

/** What the role rule reads of a thread: a live shell (dates) or a list row (text) both fit. */
export interface ThreadRoleCandidate {
  readonly id: ThreadId;
  readonly projectId: ProjectId;
  readonly lineage: OrchestrationV2ThreadShell["lineage"];
  readonly delegatedFrom?: unknown;
  readonly archivedAt: unknown;
  readonly deletedAt?: unknown;
}

/** One server's role pointers that name a thread able to hold the role. */
export interface ResolvedThreadRoles {
  readonly assistant: ThreadId | null;
  /** Project id to its orchestrator thread. */
  readonly orchestrators: ReadonlyMap<ProjectId, ThreadId>;
}

export const NO_THREAD_ROLES: ResolvedThreadRoles = Object.freeze({
  assistant: null,
  orchestrators: new Map<ProjectId, ThreadId>(),
});

const NO_ROLES_BY_ENVIRONMENT: ReadonlyMap<EnvironmentId, ResolvedThreadRoles> = new Map();

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const isIdText = (value: unknown): value is string =>
  typeof value === "string" && value.length > 0 && value.trim() === value;

/** A sub-agent, side chat or a task a linked machine delegated (upstream's `delegatedFrom != null`). */
const isChild = (thread: ThreadRoleCandidate): boolean =>
  !isListedThreadLineage(thread.lineage) || thread.delegatedFrom != null;

/**
 * Whether `thread` can hold a role now: it exists, is neither deleted nor archived, is top-level
 * and, for an orchestrator (`projectId` given), belongs to that project. The server's save check
 * uses the same rule except archive, which only pauses a role.
 */
function canHoldRole(
  thread: ThreadRoleCandidate | undefined,
  projectId: string | null,
): thread is ThreadRoleCandidate {
  return (
    thread !== undefined &&
    thread.deletedAt == null &&
    thread.archivedAt == null &&
    !isChild(thread) &&
    (projectId === null || thread.projectId === projectId)
  );
}

/**
 * The pointers in `roles` whose thread (looked up in `threads`, one server's) can hold the role.
 * Read leniently, so a settings value from an older server or a bad hand edit gives no role for
 * that entry alone: a missing or non-object value, a non-id assistant or an entry that is not an
 * id pair is skipped.
 */
export function resolveThreadRoles(
  roles: ThreadRoles | null | undefined,
  threads: ReadonlyMap<ThreadId, ThreadRoleCandidate>,
): ResolvedThreadRoles {
  const raw: unknown = roles;
  if (!isRecord(raw)) return NO_THREAD_ROLES;
  const assistantId = isIdText(raw.assistant) ? (raw.assistant as ThreadId) : null;
  const assistant =
    assistantId !== null && canHoldRole(threads.get(assistantId), null) ? assistantId : null;
  const orchestrators = new Map<ProjectId, ThreadId>();
  if (isRecord(raw.orchestrators)) {
    for (const [projectId, threadId] of Object.entries(raw.orchestrators)) {
      if (!isIdText(projectId) || !isIdText(threadId)) continue;
      if (canHoldRole(threads.get(threadId as ThreadId), projectId)) {
        orchestrators.set(projectId as ProjectId, threadId as ThreadId);
      }
    }
  }
  return assistant === null && orchestrators.size === 0
    ? NO_THREAD_ROLES
    : { assistant, orchestrators };
}

/**
 * `thread`'s role on its server. The thread is checked again, so roles read a moment earlier than
 * the list still never lift an archived, moved or child thread. A thread that is both the assistant
 * and its project's orchestrator is the assistant.
 */
export function threadRole(
  thread: ThreadRoleCandidate,
  roles: ResolvedThreadRoles | undefined,
): ThreadRole {
  if (isChild(thread)) return "child";
  if (roles === undefined || !canHoldRole(thread, null)) return "plain";
  if (roles.assistant === thread.id) return "assistant";
  if (roles.orchestrators.get(thread.projectId) === thread.id) return "orchestrator";
  return "plain";
}

/** The parts of a list row that say it waits on the person: as the sidebar's own status pills. */
export interface CalmWaitingInput {
  readonly hasPendingApprovals: boolean;
  readonly hasPendingUserInput: boolean;
  readonly pendingSubagentRequest?: "approval" | "user_input" | null;
  readonly pendingGuestMessageCount?: number;
}

/**
 * Waits on the person: an approval, a question or user-only card, or a guest message. A usage-limit
 * stop is left out on purpose: it clears without the person.
 */
export function isWaitingOnPerson(thread: CalmWaitingInput): boolean {
  return (
    thread.hasPendingApprovals ||
    thread.hasPendingUserInput ||
    thread.pendingSubagentRequest != null ||
    (thread.pendingGuestMessageCount ?? 0) > 0
  );
}

export interface CalmRoleRow<T> {
  readonly thread: T;
  readonly role: "assistant" | "orchestrator";
}

/**
 * `off`: the list as given (calm off, or no threads). `no-roles`: no row holds a role; the list as
 * given plus a hint, never all folded. `calm`: role rows on top (assistants, then orchestrators, in list order), every
 * other thread in `otherThreads` in list order. A view counts its own waiting threads with
 * `isWaitingOnPerson`, over the rows it folds.
 */
export type CalmPartition<T> =
  | { readonly kind: "off"; readonly threads: ReadonlyArray<T> }
  | { readonly kind: "no-roles"; readonly threads: ReadonlyArray<T> }
  | {
      readonly kind: "calm";
      readonly roleRows: ReadonlyArray<CalmRoleRow<T>>;
      readonly otherThreads: ReadonlyArray<T>;
    };

/**
 * Groups a list of threads (normally the navigation list: no archived, child or tag-hidden rows)
 * for the calm view. Grouping only: every thread given comes back once, so search, drafts and
 * links are untouched. Settle and snooze do not matter; a role row stays on top.
 */
export function calmPartition<
  T extends ThreadRoleCandidate & CalmWaitingInput & { readonly environmentId: EnvironmentId },
>(
  threads: ReadonlyArray<T>,
  rolesByEnvironment: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>,
  enabled: boolean,
): CalmPartition<T> {
  if (!enabled || threads.length === 0) return { kind: "off", threads };
  const assistants: Array<CalmRoleRow<T>> = [];
  const orchestrators: Array<CalmRoleRow<T>> = [];
  const otherThreads: Array<T> = [];
  for (const thread of threads) {
    const role = threadRole(thread, rolesByEnvironment.get(thread.environmentId));
    if (role === "assistant") assistants.push({ thread, role });
    else if (role === "orchestrator") orchestrators.push({ thread, role });
    else otherThreads.push(thread);
  }
  if (assistants.length === 0 && orchestrators.length === 0) return { kind: "no-roles", threads };
  return { kind: "calm", roleRows: [...assistants, ...orchestrators], otherThreads };
}

function sameRoles(a: ResolvedThreadRoles, b: ResolvedThreadRoles): boolean {
  if (a === b) return true;
  if (a.assistant !== b.assistant || a.orchestrators.size !== b.orchestrators.size) return false;
  for (const [projectId, threadId] of a.orchestrators) {
    if (b.orchestrators.get(projectId) !== threadId) return false;
  }
  return true;
}

function sameRolesByEnvironment(
  a: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>,
  b: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>,
): boolean {
  if (a.size !== b.size) return false;
  for (const [environmentId, roles] of a) {
    const other = b.get(environmentId);
    if (other === undefined || !sameRoles(roles, other)) return false;
  }
  return true;
}

const hasPointer = (roles: ThreadRoles | null | undefined): boolean => {
  const raw: unknown = roles;
  return (
    isRecord(raw) &&
    (raw.assistant != null ||
      (isRecord(raw.orchestrators) && Object.keys(raw.orchestrators).length > 0))
  );
};

/**
 * The resolved roles of every connected, enabled server, by environment; a server with none (or an
 * older one without the setting) is left out. It follows settings pushes and thread changes, and
 * keeps the same map while the result is equal, so a list that reads it redraws only on a change.
 * Threads are read only on servers that name a role, so streaming costs nothing while none do.
 */
export function createThreadRolesAtom(input: {
  readonly catalogValueAtom: Atom.Atom<EnvironmentCatalogState>;
  readonly settingsValueAtom: (
    environmentId: EnvironmentId,
  ) => Atom.Atom<{ readonly threadRoles?: ThreadRoles } | null>;
  readonly environmentThreadIndexAtom: (
    environmentId: EnvironmentId,
  ) => Atom.Atom<ReadonlyMap<ThreadId, ThreadRoleCandidate>>;
}): Atom.Atom<ReadonlyMap<EnvironmentId, ResolvedThreadRoles>> {
  let previous = NO_ROLES_BY_ENVIRONMENT;
  return Atom.make((get) => {
    const next = new Map<EnvironmentId, ResolvedThreadRoles>();
    for (const environmentId of enabledEnvironmentIds(get(input.catalogValueAtom))) {
      const roles = get(input.settingsValueAtom(environmentId))?.threadRoles;
      if (!hasPointer(roles)) continue;
      const resolved = resolveThreadRoles(
        roles,
        get(input.environmentThreadIndexAtom(environmentId)),
      );
      if (resolved !== NO_THREAD_ROLES) next.set(environmentId, resolved);
    }
    if (sameRolesByEnvironment(previous, next)) return previous;
    previous = next.size === 0 ? NO_ROLES_BY_ENVIRONMENT : next;
    return previous;
  }).pipe(Atom.withLabel("fork-thread-roles"));
}
