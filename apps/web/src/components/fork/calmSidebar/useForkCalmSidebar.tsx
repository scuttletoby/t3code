/**
 * Fork (calm sidebar): the sidebar's calm view (rules: calmSidebar.logic.ts), one seam line per
 * part: `sidebarInput` (the list's input without calm's rows; `{}` while off), `topRows` (role rows
 * and the Other threads group, or the hint) and `roleThread` (a role row's menu).
 */
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { useAtomValue } from "@effect/atom-react";
import * as Schema from "effect/Schema";
import type { ReactNode } from "react";

import { useLocalStorage } from "../../../hooks/useLocalStorage";
import { useClientSettings } from "../../../hooks/useSettings";
import { useDelegatedUnderParent } from "../../../state/entities";
import { filterSidebarV2VisibleThreads } from "../../Sidebar.logic";
import { CalmSidebarTop, type CalmRowSection } from "./CalmSidebarTop";
import {
  calmHiddenKeys,
  calmSidebarView,
  calmThreadKey,
  type CalmSidebarView,
} from "./calmSidebar.logic";
import { forkThreadRolesAtom } from "./threadRolesAtom";
// Installs the Role entry's reads and saves for both thread menus (threadRoleMenu.ts).
import "./threadRoleMenuRuntime";

/** Whether "Other threads" is open, per device; closed by default. */
export const CALM_OTHER_EXPANDED_KEY = "t3code:fork:calm-sidebar:other-expanded";

const EMPTY_THREADS: ReadonlyArray<EnvironmentThreadShell> = [];
const NO_ROLE_THREADS: ReadonlyMap<string, EnvironmentThreadShell> = new Map();

export type RenderCalmRow = (thread: EnvironmentThreadShell, section: CalmRowSection) => ReactNode;

export interface ForkCalmSidebar {
  readonly view: CalmSidebarView;
  readonly sidebarInput: (routeRowThreadKey: string | null) => {
    readonly threads?: ReadonlyArray<EnvironmentThreadShell>;
  };
  readonly topRows: (render: RenderCalmRow) => ReadonlyArray<ReactNode>;
  readonly roleThread: (threadKey: string) => EnvironmentThreadShell | undefined;
}

export function useForkCalmSidebar(input: {
  readonly listedThreads: ReadonlyArray<EnvironmentThreadShell>;
  readonly scopedProjectKeys: ReadonlySet<string> | null;
  readonly isSearchingThreads: boolean;
}): ForkCalmSidebar {
  const enabled = useClientSettings((settings) => settings.calmSidebarEnabled);
  const roles = useAtomValue(forkThreadRolesAtom);
  const delegatedUnderParent = useDelegatedUnderParent();
  const [expanded, setExpanded] = useLocalStorage(CALM_OTHER_EXPANDED_KEY, false, Schema.Boolean);
  const view = calmSidebarView({
    visible: enabled
      ? filterSidebarV2VisibleThreads(
          input.listedThreads,
          input.scopedProjectKeys,
          delegatedUnderParent,
        )
      : EMPTY_THREADS,
    roles,
    enabled,
    searching: input.isSearchingThreads,
  });
  const roleThreads =
    view.kind === "calm"
      ? new Map(view.roleRows.map((row) => [calmThreadKey(row.thread), row.thread] as const))
      : NO_ROLE_THREADS;
  return {
    view,
    sidebarInput: (routeRowThreadKey) => {
      const hidden = calmHiddenKeys(view, expanded, routeRowThreadKey);
      return hidden.size === 0
        ? {}
        : { threads: input.listedThreads.filter((thread) => !hidden.has(calmThreadKey(thread))) };
    },
    topRows: (render) =>
      view.kind === "off"
        ? []
        : [
            <CalmSidebarTop
              key="fork-calm-sidebar"
              view={view}
              expanded={expanded}
              onToggle={() => setExpanded((value) => !value)}
              render={render}
            />,
          ],
    roleThread: (threadKey) => roleThreads.get(threadKey),
  };
}
