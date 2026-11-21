/**
 * Fork: a thread's MCP profile as the thread menu shows it, and the owner's pick there. The
 * effective profile is the resolver's own result (`resolveMcpProfile`, the server's rules) over
 * the thread's lineage as this client has it, so a child shows the set cut down to its parent's.
 * A pick writes `mcpThreadProfiles[threadId]` alone; the server merges it per key.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import {
  type ContextMenuItem,
  MCP_PROFILE_MAX_LINEAGE,
  type McpProfileResolution,
  type McpProfileSettings,
  type OrchestrationV2ThreadShell,
  type ServerSettings,
  isDelegatedAppThread,
  resolveMcpProfile,
} from "@t3tools/contracts";

import {
  MCP_PICK_APPLIES_NOTE,
  effectiveMcpProfileText,
  mcpProfileChoices,
  threadPickPatch,
} from "../../settings/fork/mcpProfileSettings.logic";

/** What the resolver needs of a thread shell (the client's shells hold `null` for no delegation). */
export type McpProfileShell = Pick<OrchestrationV2ThreadShell, "id" | "projectId" | "lineage"> & {
  readonly delegatedFrom?: OrchestrationV2ThreadShell["delegatedFrom"] | null;
};

export type McpProfileSettingsForThread = McpProfileSettings &
  Pick<ServerSettings, "projectSettingsOverrides">;

/** The thread's profile, or why this client cannot work it out. */
export type ThreadMcpProfile =
  | McpProfileResolution
  | { readonly _tag: "Unknown"; readonly message: string };

/**
 * The thread's profile from its lineage among `shells` (one environment's threads), as the server
 * works it out: a parent this client does not have refuses unless the owner picked a profile for
 * the thread itself, as the server refuses a parent it cannot read.
 */
export function threadMcpProfile(
  settings: McpProfileSettingsForThread,
  threadId: string,
  shells: ReadonlyArray<McpProfileShell>,
): ThreadMcpProfile {
  const byId = new Map(shells.map((shell) => [shell.id as string, shell]));
  const self = byId.get(threadId);
  if (self === undefined) return { _tag: "Unknown", message: "This thread has not loaded yet." };
  const lineage: Array<McpProfileShell> = [self];
  let parentId: string | null = self.lineage.parentThreadId;
  let parentMissing = false;
  // One past the limit, or a repeat, is kept so the resolver refuses it.
  while (parentId !== null && lineage.length <= MCP_PROFILE_MAX_LINEAGE) {
    const repeated = lineage.find((entry) => entry.id === parentId);
    const parent = repeated ?? byId.get(parentId);
    if (parent === undefined) {
      parentMissing = true;
      break;
    }
    lineage.push(parent);
    parentId = repeated === undefined ? parent.lineage.parentThreadId : null;
  }
  const top = lineage[lineage.length - 1]!;
  const carried = parentMissing ? undefined : (top.delegatedFrom?.mcpProfile ?? undefined);
  const resolved = resolveMcpProfile(
    settings,
    lineage.map((shell) => {
      const project = settings.projectSettingsOverrides[shell.projectId];
      return {
        threadId: shell.id,
        delegated: isDelegatedAppThread({
          lineage: shell.lineage,
          ...(shell.delegatedFrom == null ? {} : { delegatedFrom: shell.delegatedFrom }),
        }),
        project: {
          mcpProfile: project?.mcpProfile,
          mcpSubagentProfile: project?.mcpSubagentProfile,
        },
      };
    }),
    carried,
  );
  if (parentMissing && resolved._tag === "Resolved" && resolved.source !== "thread-pick") {
    return {
      _tag: "Unknown",
      message:
        "Its parent thread is not loaded here, so its profile cannot be worked out; the server refuses it if it cannot read that thread either.",
    };
  }
  return resolved;
}

/** One line on the thread's profile. */
export const threadMcpProfileText = (profile: ThreadMcpProfile): string =>
  profile._tag === "Unknown" ? profile.message : effectiveMcpProfileText(profile);

const MCP_PROFILE_MENU = "mcp-profile";
const MCP_PROFILE_MENU_CLEAR = "mcp-profile:clear";
const PICK_PREFIX = "mcp-profile:pick:";

export type McpProfileMenuId =
  | typeof MCP_PROFILE_MENU
  | typeof MCP_PROFILE_MENU_CLEAR
  | `${typeof PICK_PREFIX}${string}`;

/** What the thread menu needs to show the entry. */
export interface McpProfileMenuState {
  readonly threadId: string;
  readonly profile: ThreadMcpProfile;
  /** The owner's pick for this thread, or null. */
  readonly pick: string | null;
  readonly choices: ReadonlyArray<string>;
  /** False for a client that may not change settings: the entry then only shows the profile. */
  readonly canWrite: boolean;
}

export function mcpProfileMenuState(
  settings: McpProfileSettingsForThread,
  threadId: string,
  shells: ReadonlyArray<McpProfileShell>,
  canWrite: boolean,
): McpProfileMenuState {
  const pick = Object.hasOwn(settings.mcpThreadProfiles, threadId)
    ? (settings.mcpThreadProfiles[threadId] ?? null)
    : null;
  return {
    threadId,
    profile: threadMcpProfile(settings, threadId, shells),
    pick,
    choices: mcpProfileChoices(settings),
    canWrite,
  };
}

const shortName = (profile: ThreadMcpProfile) =>
  profile._tag === "Resolved"
    ? profile.narrowedByParent
      ? `${profile.name}, cut down to the parent's`
      : profile.name
    : profile._tag === "CannotApply"
      ? "cannot apply"
      : "unknown";

/** The thread menu's entry: the effective profile, and the owner's pick when the client may write. */
export function mcpProfileMenuItems(
  state: McpProfileMenuState | undefined,
): ReadonlyArray<ContextMenuItem<McpProfileMenuId>> {
  if (state === undefined) return [];
  const entry = {
    id: MCP_PROFILE_MENU,
    label: `MCP profile: ${shortName(state.profile)}`,
    detail: threadMcpProfileText(state.profile),
    icon: "settings",
  } as const;
  if (!state.canWrite) return [{ ...entry, disabled: true }];
  return [
    {
      ...entry,
      children: [
        ...state.choices.map((name) => ({
          id: `${PICK_PREFIX}${name}` as const,
          label: name,
          checked: state.pick === name,
        })),
        {
          id: MCP_PROFILE_MENU_CLEAR,
          label: "Clear this thread's pick",
          detail: MCP_PICK_APPLIES_NOTE,
          disabled: state.pick === null,
          separatorBefore: true,
        },
      ],
    },
  ];
}

/** True for an id of this entry. */
export const isMcpProfileMenuAction = (id: string): id is McpProfileMenuId =>
  id === MCP_PROFILE_MENU || id === MCP_PROFILE_MENU_CLEAR || id.startsWith(PICK_PREFIX);

/** The settings patch a click on the entry sends, or null for none (the entry itself, or no change). */
export function mcpProfileMenuPatch(
  picks: Readonly<Record<string, string>>,
  threadId: string,
  action: McpProfileMenuId,
) {
  if (action === MCP_PROFILE_MENU_CLEAR) return threadPickPatch(picks, threadId, null);
  if (action.startsWith(PICK_PREFIX)) {
    return threadPickPatch(picks, threadId, action.slice(PICK_PREFIX.length));
  }
  return null;
}
