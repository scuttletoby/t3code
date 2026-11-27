/**
 * Fork (calm sidebar, CAL3): the calm view in upstream's sidebar. Role rows (the assistant, one
 * orchestrator per project) on top with their badge, then "Other threads" (count, waiting count)
 * folding the rest. Sidebar.tsx calls the hook once and asks it per list item (docs/fork/README.md).
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { scopedThreadKey, scopeThreadRef } from "@t3tools/client-runtime/environment";
import * as Schema from "effect/Schema";
import type { ReactNode } from "react";

import { useLocalStorage } from "../../../hooks/useLocalStorage";
import { useClientSettings } from "../../../hooks/useSettings";
import {
  filterSidebarV2VisibleThreads,
  type SidebarListItem,
  type SidebarSection,
} from "../../Sidebar.logic";
import { CollapsibleSectionHeader } from "../../ui/collapsible-section-header";
import {
  type CalmView,
  calmSkipsListItem,
  calmView,
  otherThreadsAccessory,
} from "./calmSidebar.logic";
import { threadRolesAtom } from "./threadRolesAtom";

/** Per device, like upstream's shelves: closed until the person opens it. */
const CALM_OTHER_OPEN_KEY = "t3code:fork:calm-sidebar:other-open";

const keyOf = (thread: EnvironmentThreadShell) =>
  scopedThreadKey(scopeThreadRef(thread.environmentId, thread.id));

const ROLE_BADGE = { assistant: "Assistant", orchestrator: "Orchestrator" } as const;

type RenderRow = (thread: EnvironmentThreadShell, section: SidebarSection) => ReactNode;

/** The rows the calm view draws above upstream's list, in order. */
export function calmTopItems(
  view: CalmView<EnvironmentThreadShell>,
  renderRow: RenderRow,
  sectionByThreadKey: ReadonlyMap<string, SidebarSection>,
  onToggle: () => void,
): ReactNode[] {
  if (view.kind === "off") return [];
  if (view.kind === "hint") {
    return [
      <li
        key="fork-calm-hint"
        className="list-none px-2.5 py-1.5 text-xs text-sidebar-muted-foreground"
      >
        Calm view is on, but no thread has a role yet. Give one from a thread's menu: Thread role.
      </li>,
    ];
  }
  const row = (thread: EnvironmentThreadShell) =>
    renderRow(thread, sectionByThreadKey.get(keyOf(thread)) ?? "active");
  const items: ReactNode[] = [];
  for (const { thread, role } of view.roleRows) {
    items.push(
      <li
        key={`fork-calm-badge:${keyOf(thread)}`}
        data-fork-calm-role={role}
        className="list-none px-2.5 pt-2 text-2xs font-medium uppercase tracking-wide text-sidebar-muted-foreground"
      >
        {ROLE_BADGE[role]}
      </li>,
      row(thread),
    );
  }
  items.push(
    <li
      key="fork-calm-other"
      // Closed, upstream's "Show more" of the Settled shelf (after the list) has nothing to show.
      className={
        view.open
          ? "relative mx-0.5 mt-2 h-8 list-none"
          : "relative mx-0.5 mt-2 h-8 list-none [&~li:has(>[data-sidebar-row^=settled])]:hidden"
      }
    >
      <CollapsibleSectionHeader
        data-testid="fork-calm-other-toggle"
        expanded={view.open}
        onClick={onToggle}
        accessory={<span className="font-normal tabular-nums">{otherThreadsAccessory(view)}</span>}
      >
        Other threads
      </CollapsibleSectionHeader>
    </li>,
  );
  if (view.routeOther !== null) items.push(row(view.routeOther));
  return items;
}

/**
 * The calm view for this render. `skipsListItem`: upstream's loop leaves the item out (a role row,
 * or anything while "Other threads" is closed). `drawnOnTop`: a project folder leaves a role row out.
 */
export function useCalmSidebar(
  input: {
    readonly listedThreads: ReadonlyArray<EnvironmentThreadShell>;
    readonly scopedProjectKeys: ReadonlySet<string> | null;
    readonly delegatedUnderParent: ReadonlySet<string>;
  },
  routeRowThreadKey: string | null,
) {
  const enabled = useClientSettings((settings) => settings.calmSidebarEnabled);
  const rolesByEnvironment = useAtomValue(threadRolesAtom);
  const [open, setOpen] = useLocalStorage(CALM_OTHER_OPEN_KEY, false, Schema.Boolean);
  const view = calmView({
    threads: enabled
      ? filterSidebarV2VisibleThreads(
          input.listedThreads,
          input.scopedProjectKeys,
          input.delegatedUnderParent,
        )
      : [],
    rolesByEnvironment,
    enabled,
    open,
    routeRowThreadKey,
    keyOf,
  });
  return {
    view,
    skipsListItem: (item: SidebarListItem) => calmSkipsListItem(view, item),
    drawnOnTop: (thread: EnvironmentThreadShell) =>
      view.kind === "calm" && view.roleKeys.has(keyOf(thread)),
    topItems: (renderRow: RenderRow, sectionByThreadKey: ReadonlyMap<string, SidebarSection>) =>
      calmTopItems(view, renderRow, sectionByThreadKey, () => setOpen((value) => !value)),
  };
}

// The sidebar's own thread menu fills and runs the "Thread role" entry through this one import.
export { readRoleMenuState, runIfRoleMenuAction } from "./roleMenu";
