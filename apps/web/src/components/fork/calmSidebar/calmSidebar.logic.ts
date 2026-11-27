/**
 * Fork (calm sidebar, CAL3): what the calm view draws on top of the sidebar and which of upstream's
 * list items it hides. Grouping only, from client-runtime's `calmPartition`: every thread stays in
 * upstream's list (its menus, search and links read it there); the calm view only moves rows.
 */
import {
  calmPartition,
  type CalmRoleRow,
  type CalmWaitingInput,
  type ResolvedThreadRoles,
  type ThreadRoleCandidate,
} from "@t3tools/client-runtime/state/thread-roles";
import type { EnvironmentId } from "@t3tools/contracts";

import type { SidebarListItem } from "../../Sidebar.logic";

type CalmThread = ThreadRoleCandidate &
  CalmWaitingInput & { readonly environmentId: EnvironmentId };

/**
 * `off`: upstream's list. `hint`: calm is on but no listed thread holds a role; upstream's list and
 * one line saying how to give one. `calm`: the role rows on top, then "Other threads" with how many
 * there are and wait on the person; closed, upstream's list is hidden but for the open thread.
 */
export type CalmView<T> =
  | { readonly kind: "off" }
  | { readonly kind: "hint" }
  | {
      readonly kind: "calm";
      readonly roleRows: ReadonlyArray<CalmRoleRow<T>>;
      readonly roleKeys: ReadonlySet<string>;
      readonly otherCount: number;
      readonly waitingCount: number;
      readonly open: boolean;
      /** The open thread when it is folded away: drawn below the header so it stays in sight. */
      readonly routeOther: T | null;
    };

const OFF = { kind: "off" } as const;
const HINT = { kind: "hint" } as const;

export function calmView<T extends CalmThread>(input: {
  /** The sidebar's visible list: in scope, not archived, not shown under a parent. */
  readonly threads: ReadonlyArray<T>;
  readonly rolesByEnvironment: ReadonlyMap<EnvironmentId, ResolvedThreadRoles>;
  readonly enabled: boolean;
  readonly open: boolean;
  readonly routeRowThreadKey: string | null;
  readonly keyOf: (thread: T) => string;
}): CalmView<T> {
  const partition = calmPartition(input.threads, input.rolesByEnvironment, input.enabled);
  if (partition.kind === "off") return OFF;
  if (partition.kind === "no-roles") return HINT;
  const roleKeys = new Set(partition.roleRows.map((row) => input.keyOf(row.thread)));
  const routeOther =
    input.open || input.routeRowThreadKey === null
      ? null
      : (partition.otherThreads.find((thread) => input.keyOf(thread) === input.routeRowThreadKey) ??
        null);
  return {
    kind: "calm",
    roleRows: partition.roleRows,
    roleKeys,
    otherCount: partition.otherThreads.length,
    waitingCount: partition.waitingCount,
    open: input.open,
    routeOther,
  };
}

/**
 * Whether upstream's loop skips `item`. Open: only role rows (drawn on top). Closed: everything,
 * shelf headers too, since the calm view draws what stays in sight (role rows, the open thread).
 */
export function calmSkipsListItem<T>(view: CalmView<T>, item: SidebarListItem): boolean {
  if (view.kind !== "calm") return false;
  if (!view.open) return true;
  return item.kind === "thread" && view.roleKeys.has(item.key);
}

/** The "Other threads" header's trailing text: the waiting count first, as it matters most. */
export function otherThreadsAccessory(view: {
  readonly otherCount: number;
  readonly waitingCount: number;
}): string {
  return view.waitingCount > 0
    ? `${view.waitingCount} waiting · ${view.otherCount}`
    : `${view.otherCount}`;
}
