/**
 * Fork (calm sidebar): which rows the web sidebar's own list keeps in the calm view; the grouping is
 * the shared `calmPartition`. Snoozed and settled threads stay in upstream's shelves, the open
 * thread is never folded away, and a search sees every thread. Docs: docs/fork/README.md.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { scopedThreadKey, scopeThreadRef } from "@t3tools/client-runtime/environment";
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { effectiveSnoozed } from "@t3tools/client-runtime/state/thread-settled";
import {
  type CalmRoleRow,
  calmPartition,
  isWaitingOnPerson,
  type ResolvedThreadRoles,
} from "@t3tools/client-runtime/state/thread-roles";
import type { EnvironmentId } from "@t3tools/contracts";

export const calmThreadKey = (thread: Pick<EnvironmentThreadShell, "environmentId" | "id">) =>
  scopedThreadKey(scopeThreadRef(thread.environmentId, thread.id));

export type CalmSidebarView =
  /** Upstream's list: calm is off, a search runs, or there are no threads. */
  | { readonly kind: "off" }
  /** Calm is on, but no listed thread holds a role: the whole list, and a hint. */
  | { readonly kind: "no-roles" }
  | {
      readonly kind: "calm";
      readonly roleRows: ReadonlyArray<CalmRoleRow<EnvironmentThreadShell>>;
      /** The threads folded into "Other threads", in list order. */
      readonly folded: ReadonlyArray<EnvironmentThreadShell>;
      /** How many folded threads wait on the person (approval, question, guest message). */
      readonly waitingCount: number;
      /** The time "snoozed now" was read at. */
      readonly now: string;
    };

/** In a shelf upstream folds by itself (settled or snoozed now), so calm leaves it there. */
function inFoldedShelf(thread: EnvironmentThreadShell, now: string): boolean {
  return thread.settledOverride === "settled" || effectiveSnoozed(thread, { now });
}

/**
 * The calm view of `visible`, the threads the sidebar lists (scope applied, no archived or child
 * rows). `roles` are the resolved roles of every server.
 */
export function calmSidebarView(input: {
  readonly visible: ReadonlyArray<EnvironmentThreadShell>;
  readonly roles: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>;
  readonly enabled: boolean;
  readonly searching: boolean;
  /** For "snoozed now"; the clock by default. */
  readonly now?: string;
}): CalmSidebarView {
  if (input.searching) return { kind: "off" };
  const partition = calmPartition(input.visible, input.roles, input.enabled);
  if (partition.kind !== "calm") return { kind: partition.kind };
  const now = input.now ?? new Date().toISOString();
  const folded = partition.otherThreads.filter((thread) => !inFoldedShelf(thread, now));
  return {
    kind: "calm",
    roleRows: partition.roleRows,
    folded,
    waitingCount: folded.filter(isWaitingOnPerson).length,
    now,
  };
}

/**
 * The thread keys the sidebar's own list leaves out: the role rows (drawn on top) and, while the
 * group is closed, the folded threads but the open one (`routeRowThreadKey`).
 */
export function calmHiddenKeys(
  view: CalmSidebarView,
  expanded: boolean,
  routeRowThreadKey: string | null,
): ReadonlySet<string> {
  if (view.kind !== "calm") return EMPTY_KEYS;
  const hidden = new Set(view.roleRows.map((row) => calmThreadKey(row.thread)));
  if (!expanded) {
    for (const thread of view.folded) {
      const key = calmThreadKey(thread);
      if (key !== routeRowThreadKey) hidden.add(key);
    }
  }
  return hidden;
}

const EMPTY_KEYS: ReadonlySet<string> = new Set();

/** The group's heading: "Other threads (N)", and "W waiting" when one waits on the person. */
export function calmGroupText(view: Extract<CalmSidebarView, { kind: "calm" }>): {
  readonly label: string;
  readonly waiting: string | null;
} {
  return {
    label: `Other threads (${view.folded.length})`,
    waiting: view.waitingCount === 0 ? null : `${view.waitingCount} waiting`,
  };
}
