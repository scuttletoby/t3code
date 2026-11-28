/**
 * Fork (calm sidebar): the thread menu's "Role" entry. A person makes a thread the assistant or its
 * project's orchestrator, replaces the thread that holds it, or removes the thread's role. Each
 * pick saves one `threadRoles` patch (person-only; the server checks the thread again and refuses
 * one that cannot hold the role). Docs: docs/fork/README.md, `server.threadRoles`.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import {
  type ContextMenuItem,
  isListedThreadLineage,
  type ServerSettingsPatch,
  type ThreadRoles,
} from "@t3tools/contracts";

export const THREAD_ROLE_MENU = "fork-role";
export const MAKE_ORCHESTRATOR = "fork-role:orchestrator";
export const MAKE_ASSISTANT = "fork-role:assistant";
export const REMOVE_ROLE = "fork-role:remove";

export type ThreadRoleMenuId =
  | typeof THREAD_ROLE_MENU
  | typeof MAKE_ORCHESTRATOR
  | typeof MAKE_ASSISTANT
  | typeof REMOVE_ROLE;

/** What the menu shows for one thread; `undefined` (no entry) where no role can be set. */
export interface ThreadRoleMenuState {
  /** This thread is the assistant (by the pointer, also while it is archived). */
  readonly isAssistant: boolean;
  /** This thread is its own project's orchestrator. */
  readonly isOrchestrator: boolean;
  /** Another project's orchestrator pointer names this thread (it moved since). */
  readonly holdsOtherProjectRole: boolean;
  /** The title of the other thread that is the project's orchestrator now, if one is. */
  readonly orchestratorHolder: string | null;
  /** The title of the other thread that is the assistant now, if one is. */
  readonly assistantHolder: string | null;
}

/** A thread as the role entry reads it. */
export interface RoleMenuThread {
  readonly id: string;
  readonly projectId: string;
  readonly lineage: Parameters<typeof isListedThreadLineage>[0];
  readonly delegatedFrom?: unknown;
  readonly deletedAt?: string | null;
}

/** A pointer's thread: its title, or a stand-in while this client does not have it. */
const holderTitle = (title: string | null | undefined): string => title ?? "a thread not loaded";

/**
 * The role entry's state, or `undefined` for no entry: the client may not change settings, the
 * server's settings are not loaded (or predate roles), or the thread is a sub-agent, side chat,
 * delegated task or deleted (the server refuses a role for those).
 */
export function threadRoleMenuState(input: {
  readonly thread: RoleMenuThread | null;
  readonly roles: ThreadRoles | null | undefined;
  readonly canWriteSettings: boolean;
  readonly titleOf: (threadId: string) => string | null;
}): ThreadRoleMenuState | undefined {
  const { thread, roles } = input;
  if (!input.canWriteSettings || thread === null || roles == null) return undefined;
  if (!isListedThreadLineage(thread.lineage) || thread.delegatedFrom != null) return undefined;
  if (thread.deletedAt != null) return undefined;
  const orchestrators: Readonly<Record<string, string>> = roles.orchestrators ?? {};
  const projectHolder = orchestrators[thread.projectId] ?? null;
  const assistant = roles.assistant ?? null;
  return {
    isAssistant: assistant === thread.id,
    isOrchestrator: projectHolder === thread.id,
    holdsOtherProjectRole: Object.entries(orchestrators).some(
      ([projectId, threadId]) => threadId === thread.id && projectId !== thread.projectId,
    ),
    orchestratorHolder:
      projectHolder === null || projectHolder === thread.id
        ? null
        : holderTitle(input.titleOf(projectHolder)),
    assistantHolder:
      assistant === null || assistant === thread.id ? null : holderTitle(input.titleOf(assistant)),
  };
}

/** The "Role" entry with its picks, or none. */
export function threadRoleMenuItems(
  state: ThreadRoleMenuState | undefined,
): ReadonlyArray<ContextMenuItem<ThreadRoleMenuId>> {
  if (state === undefined) return [];
  // A role the thread holds shows checked and disabled, so the menu says which one it is.
  const children: Array<ContextMenuItem<ThreadRoleMenuId>> = [
    state.isOrchestrator
      ? { id: MAKE_ORCHESTRATOR, label: "Project orchestrator", checked: true, disabled: true }
      : {
          id: MAKE_ORCHESTRATOR,
          label:
            state.orchestratorHolder === null
              ? "Make project orchestrator"
              : "Replace project orchestrator",
          ...(state.orchestratorHolder === null
            ? {}
            : { detail: `now ${state.orchestratorHolder}` }),
        },
    state.isAssistant
      ? { id: MAKE_ASSISTANT, label: "Assistant", checked: true, disabled: true }
      : {
          id: MAKE_ASSISTANT,
          label: state.assistantHolder === null ? "Make assistant" : "Replace assistant",
          ...(state.assistantHolder === null ? {} : { detail: `now ${state.assistantHolder}` }),
        },
  ];
  if (state.isAssistant || state.isOrchestrator || state.holdsOtherProjectRole) {
    children.push({
      id: REMOVE_ROLE,
      label: "Remove role",
      destructive: true,
      separatorBefore: children.length > 0,
    });
  }
  return [{ id: THREAD_ROLE_MENU, label: "Role", icon: "user-plus", children }];
}

export function isThreadRoleMenuId(id: string): id is ThreadRoleMenuId {
  return id === THREAD_ROLE_MENU || id.startsWith(`${THREAD_ROLE_MENU}:`);
}

/**
 * The settings patch of one pick. Make: the thread takes the role (the old holder loses it, as one
 * pointer holds one thread). Remove: every pointer that names the thread is cleared, the
 * assistant and each project's entry on its own, so a concurrent change to another project stays.
 */
export function threadRolePatch(
  pick: ThreadRoleMenuId,
  thread: Pick<RoleMenuThread, "id" | "projectId">,
  roles: ThreadRoles,
): ServerSettingsPatch | null {
  switch (pick) {
    case MAKE_ORCHESTRATOR:
      return {
        threadRoles: { orchestrators: { [thread.projectId]: thread.id } },
      } as ServerSettingsPatch;
    case MAKE_ASSISTANT:
      return { threadRoles: { assistant: thread.id } } as ServerSettingsPatch;
    case REMOVE_ROLE: {
      const cleared = Object.fromEntries(
        Object.entries(roles.orchestrators ?? {}).flatMap(([projectId, threadId]) =>
          threadId === thread.id ? [[projectId, null]] : [],
        ),
      );
      const clearsAssistant = roles.assistant === thread.id;
      if (!clearsAssistant && Object.keys(cleared).length === 0) return null;
      return {
        threadRoles: {
          ...(clearsAssistant ? { assistant: null } : {}),
          ...(Object.keys(cleared).length === 0 ? {} : { orchestrators: cleared }),
        },
      } as ServerSettingsPatch;
    }
    case THREAD_ROLE_MENU:
      return null;
  }
}
