/**
 * Fork (calm sidebar): the top of the sidebar list in the calm view. Role rows under their role's
 * heading (the badge), drawn with the list's own row so they act like every other row, then the
 * "Other threads (N)" group heading with its waiting count. With calm on and no role held: a hint.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import type { EnvironmentThreadShell } from "@t3tools/client-runtime/state/models";
import { effectiveSnoozed } from "@t3tools/client-runtime/state/thread-settled";
import type { ReactNode } from "react";

import { Badge } from "../../ui/badge";
import { CollapsibleSectionHeader } from "../../ui/collapsible-section-header";
import { calmGroupText, type CalmSidebarView } from "./calmSidebar.logic";

/** The list section a role row is drawn as: its own state, so its row actions stay right. */
export type CalmRowSection = "pinned" | "active" | "snoozed" | "settled";

function calmRowSection(thread: EnvironmentThreadShell, now: string): CalmRowSection {
  if (thread.settledOverride === "settled") return "settled";
  if (effectiveSnoozed(thread, { now })) return "snoozed";
  return thread.pinnedAt != null ? "pinned" : "active";
}

function RoleHeading(props: { readonly children: ReactNode }) {
  return (
    <li
      role="presentation"
      data-thread-selection-safe
      className="list-none px-2.5 pt-1 text-2xs font-medium uppercase tracking-wide text-sidebar-foreground/60"
    >
      {props.children}
    </li>
  );
}

export function CalmSidebarTop(props: {
  readonly view: Exclude<CalmSidebarView, { kind: "off" }>;
  readonly expanded: boolean;
  readonly onToggle: () => void;
  readonly render: (thread: EnvironmentThreadShell, section: CalmRowSection) => ReactNode;
}) {
  const { view } = props;
  if (view.kind === "no-roles") {
    return (
      <li
        role="presentation"
        data-testid="fork-calm-no-roles"
        className="mx-0.5 list-none rounded-md px-2 py-1.5 text-xs text-sidebar-foreground/70"
      >
        Calm view is on, but no thread holds a role yet. Open a thread&apos;s menu and pick Role to
        make it the assistant or its project&apos;s orchestrator.
      </li>
    );
  }
  const now = view.now;
  const assistants = view.roleRows.filter((row) => row.role === "assistant");
  const orchestrators = view.roleRows.filter((row) => row.role === "orchestrator");
  const group = calmGroupText(view);
  return (
    <>
      {assistants.length > 0 ? <RoleHeading>Assistant</RoleHeading> : null}
      {assistants.map((row) => props.render(row.thread, calmRowSection(row.thread, now)))}
      {orchestrators.length > 0 ? (
        <RoleHeading>{orchestrators.length === 1 ? "Orchestrator" : "Orchestrators"}</RoleHeading>
      ) : null}
      {orchestrators.map((row) => props.render(row.thread, calmRowSection(row.thread, now)))}
      <li
        role="presentation"
        data-thread-selection-safe
        data-testid="fork-calm-other"
        className="relative mx-0.5 h-8 list-none"
      >
        <CollapsibleSectionHeader
          expanded={props.expanded}
          onClick={props.onToggle}
          data-testid="fork-calm-other-toggle"
          accessory={
            group.waiting === null ? null : (
              <Badge variant="warning" size="sm">
                {group.waiting}
              </Badge>
            )
          }
        >
          {group.label}
        </CollapsibleSectionHeader>
      </li>
    </>
  );
}
