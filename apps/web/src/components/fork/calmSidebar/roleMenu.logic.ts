/**
 * Fork (calm sidebar, CAL4): the thread menu's "Thread role" entry: make the thread the assistant
 * or its project's orchestrator (replacing the thread that holds it), or remove its role. Pure;
 * roleMenu.ts reads the state and saves the person's pick as a `threadRoles` settings patch.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import type { ContextMenuItem, ProjectId, ThreadId, ThreadRolesPatch } from "@t3tools/contracts";

import type { ThreadActionMenuId } from "../../threadActionMenu.logic";

const ROLE_MENU = "fork-role";
export const ROLE_MAKE_ASSISTANT = "fork-role:assistant";
export const ROLE_MAKE_ORCHESTRATOR = "fork-role:orchestrator";
export const ROLE_REMOVE = "fork-role:remove";
export type RoleMenuAction =
  | typeof ROLE_MAKE_ASSISTANT
  | typeof ROLE_MAKE_ORCHESTRATOR
  | typeof ROLE_REMOVE;

/** A thread that can hold a role (top-level, not archived) on a server that has the setting. */
export interface RoleMenuState {
  readonly isAssistant: boolean;
  readonly isOrchestrator: boolean;
  /** Another thread holds that role now, so a press replaces it. */
  readonly assistantElsewhere: boolean;
  readonly orchestratorElsewhere: boolean;
}

export function isRoleMenuAction(action: string): action is RoleMenuAction {
  return (
    action === ROLE_MAKE_ASSISTANT || action === ROLE_MAKE_ORCHESTRATOR || action === ROLE_REMOVE
  );
}

// The menu's id type is upstream's union; the fork's ids join it here, not in upstream's file.
const id = (value: string) => value as ThreadActionMenuId;

/** The entry, or none (a child or archived thread, an older server, a thread not loaded). */
export function roleMenuItems(
  state: RoleMenuState | undefined,
): ReadonlyArray<ContextMenuItem<ThreadActionMenuId>> {
  if (state === undefined) return [];
  const hasRole = state.isAssistant || state.isOrchestrator;
  return [
    {
      id: id(ROLE_MENU),
      label: "Thread role",
      children: [
        {
          id: id(ROLE_MAKE_ASSISTANT),
          label: state.assistantElsewhere
            ? "Make assistant (replaces the current one)"
            : "Make assistant",
          checked: state.isAssistant,
          disabled: state.isAssistant,
        },
        {
          id: id(ROLE_MAKE_ORCHESTRATOR),
          label: state.orchestratorElsewhere
            ? "Make project orchestrator (replaces the current one)"
            : "Make project orchestrator",
          checked: state.isOrchestrator,
          disabled: state.isOrchestrator,
        },
        ...(hasRole ? [{ id: id(ROLE_REMOVE), label: "Remove role", separatorBefore: true }] : []),
      ],
    },
  ];
}

/** The settings patch for a pick: only the entry it changes, so other changes are kept. */
export function roleMenuPatch(
  action: RoleMenuAction,
  thread: { readonly id: ThreadId; readonly projectId: ProjectId },
  state: Pick<RoleMenuState, "isAssistant" | "isOrchestrator">,
): ThreadRolesPatch | null {
  switch (action) {
    case ROLE_MAKE_ASSISTANT:
      return { assistant: thread.id };
    case ROLE_MAKE_ORCHESTRATOR:
      return { orchestrators: { [thread.projectId]: thread.id } };
    case ROLE_REMOVE: {
      if (!state.isAssistant && !state.isOrchestrator) return null;
      return {
        ...(state.isAssistant ? { assistant: null } : {}),
        ...(state.isOrchestrator ? { orchestrators: { [thread.projectId]: null } } : {}),
      };
    }
  }
}
