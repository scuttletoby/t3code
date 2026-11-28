/**
 * Fork (calm sidebar): the role badge in the thread header, "Assistant" or "Orchestrator", for a
 * thread that holds a role now (the same rule as the sidebar's role rows). Shown whether the calm
 * view is on or off: the role is the server's, the view only this device's.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { threadRole } from "@t3tools/client-runtime/state/thread-roles";
import type { ScopedThreadRef } from "@t3tools/contracts";
import { useAtomValue } from "@effect/atom-react";

import { useThreadShell } from "../../../state/entities";
import { Badge } from "../../ui/badge";
import { Tooltip, TooltipPopup, TooltipTrigger } from "../../ui/tooltip";
import { forkThreadRolesAtom } from "./threadRolesAtom";

const TEXT = {
  assistant: {
    label: "Assistant",
    hint: "This thread is the assistant (threadRoles in the settings).",
  },
  orchestrator: {
    label: "Orchestrator",
    hint: "This thread is its project's orchestrator (threadRoles in the settings).",
  },
} as const;

export function ForkThreadRoleBadge(props: { readonly threadRef: ScopedThreadRef }) {
  const thread = useThreadShell(props.threadRef);
  const roles = useAtomValue(forkThreadRolesAtom);
  if (thread === null) return null;
  const role = threadRole(thread, roles.get(props.threadRef.environmentId));
  if (role !== "assistant" && role !== "orchestrator") return null;
  return (
    <Tooltip>
      <TooltipTrigger
        render={<Badge variant="info" size="sm" data-testid="fork-thread-role-badge" />}
      >
        {TEXT[role].label}
      </TooltipTrigger>
      <TooltipPopup side="top">{TEXT[role].hint}</TooltipPopup>
    </Tooltip>
  );
}
