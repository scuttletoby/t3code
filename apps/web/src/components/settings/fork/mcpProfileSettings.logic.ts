/**
 * Fork: the pure parts of the MCP profile settings. Entries are read by the resolver's rules, so
 * none shows wider than a thread gets. Patches name only the keys they change: the server merges
 * `mcpProfiles` and `mcpThreadProfiles` per key (`null` removes one).
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  MCP_BUILTIN_TOOLS_NONE,
  MCP_BUILT_IN_PROFILES,
  MCP_SUBAGENT_INHERIT,
  MCP_T3_TOOL_GROUPS,
  MCP_T3_UNGROUPED_TOOLS,
  MCP_THREAD_PROFILES_MAX,
  type McpProfileResolution,
  type McpProfileSet,
  type McpProfileSettings,
  type McpT3ToolGroup,
  type ServerSettingsPatch,
  isBuiltInMcpProfileName,
  isDefinableMcpProfileName,
  readMcpProfileEntry,
} from "@t3tools/contracts";
import { mcpProfileUses } from "@t3tools/shared/forkSettingsPatch";

/** The settings this panel reads: the resolver's keys and the projects' overrides. */
export type McpProfilePanelSettings = Pick<
  McpProfileSettings,
  "mcpProfiles" | "mcpProfile" | "mcpSubagentProfile" | "mcpThreadProfiles"
> & {
  readonly projectSettingsOverrides: {
    readonly [projectId: string]:
      | {
          readonly mcpProfile?: string | undefined;
          readonly mcpSubagentProfile?: string | undefined;
        }
      | undefined;
  };
};

/** T3 tools every profile keeps, whatever its `t3Tools` says. */
export const MCP_ALWAYS_KEPT_NOTE = `${MCP_T3_UNGROUPED_TOOLS.map((tool) => `\`${tool}\``).join(", ")} always stay, whatever the profile says.`;

/**
 * Under the T3 tool groups: only requests from a thread are filtered. The
 * agent-facing refusal text leaves this out on purpose, so it does not point a refused agent at it.
 */
export const MCP_NOT_A_SANDBOX_NOTE =
  "Profiles are not a sandbox: only requests that come from a thread are filtered. A request with no thread (an OAuth client, the `t3 thread` CLI, a script run from a shell with the owner's access) gets every T3 tool.";

export const MCP_PICK_APPLIES_NOTE = "A change applies from the next message.";

export type McpProfileRow =
  | {
      readonly name: string;
      readonly kind: "built-in" | "custom";
      readonly state: "ok";
      readonly profile: McpProfileSet;
      /** Notes to show under it, such as a server that is not set up. */
      readonly notes: ReadonlyArray<string>;
    }
  | {
      readonly name: string;
      readonly kind: "built-in" | "custom";
      /**
       * `broken`: a thread that uses it is refused (unknown fields from a newer build, a bad value,
       * a hand entry under a built-in's name). `foreign`: it names T3 tool groups this version does
       * not know, which keep nothing here; editing it here would change what it says. Neither is
       * editable here; both can be removed (`removable`).
       */
      readonly state: "broken" | "foreign";
      readonly issue: string;
      readonly removable: boolean;
    };

const entriesOf = (profiles: unknown): ReadonlyArray<readonly [string, unknown]> =>
  typeof profiles === "object" && profiles !== null && !Array.isArray(profiles)
    ? Object.entries(profiles)
    : [];

/** "`lean` names `docs`, which is not set up", for each listed server `known` lacks. */
export function notSetUpNotes(
  name: string,
  servers: McpProfileSet["servers"],
  known: ReadonlyArray<string> | null,
): ReadonlyArray<string> {
  if (servers === "all" || known === null) return [];
  return servers
    .filter((server) => !known.includes(server))
    .map((server) => `\`${name}\` names \`${server}\`, which is not set up.`);
}

/**
 * Every profile, the built-ins first, then the named ones by name. `knownServers` is the list of
 * outside servers set up where the profiles apply, or null when this client cannot tell.
 */
export function mcpProfileRows(
  settings: Pick<McpProfileSettings, "mcpProfiles">,
  knownServers: ReadonlyArray<string> | null,
): ReadonlyArray<McpProfileRow> {
  const entries = entriesOf(settings.mcpProfiles);
  const builtIns = Object.entries(MCP_BUILT_IN_PROFILES).map(([name, profile]): McpProfileRow => {
    // A save never writes one: a hand edit, and the resolver refuses the built-in while it stays.
    if (entries.some(([entryName]) => entryName === name)) {
      return {
        name,
        kind: "built-in",
        state: "broken",
        issue: `settings.json also has a profile entry named "${name}", a built-in profile's name, so threads that use "${name}" are refused until that entry is removed.`,
        removable: true,
      };
    }
    return { name, kind: "built-in", state: "ok", profile, notes: [] };
  });
  const custom = entries
    .filter(([name]) => !isBuiltInMcpProfileName(name))
    .toSorted(([a], [b]) => a.localeCompare(b))
    .map(([name, entry]): McpProfileRow => {
      if (!isDefinableMcpProfileName(name)) {
        return {
          name,
          kind: "custom",
          state: "broken",
          issue:
            "Its name is not one a profile may have (lower-case letters, digits and dashes, not inherit). Remove it from settings.json.",
          // A save cannot name it, so only a hand edit removes it.
          removable: false,
        };
      }
      const read = readMcpProfileEntry(entry);
      if (!read.ok) {
        return {
          name,
          kind: "custom",
          state: "broken",
          issue: `This version cannot read it (${read.issue}), so threads that use it are refused. Fix it in settings.json, or delete it.`,
          removable: true,
        };
      }
      if (read.unknown.length > 0) {
        return {
          name,
          kind: "custom",
          state: "foreign",
          issue: `It names T3 tool groups this version does not know (${read.unknown.join(", ")}); they keep nothing here. Edit it in settings.json, or delete it.`,
          removable: true,
        };
      }
      return {
        name,
        kind: "custom",
        state: "ok",
        profile: read.profile,
        notes: notSetUpNotes(name, read.profile.servers, knownServers),
      };
    });
  return [...builtIns, ...custom];
}

/** The profiles a default, rule or thread may name: the built-ins and each readable entry. */
export function mcpProfileChoices(
  settings: Pick<McpProfileSettings, "mcpProfiles">,
): ReadonlyArray<string> {
  return mcpProfileRows(settings, null)
    .filter((row) => row.state !== "broken")
    .map((row) => row.name);
}

/** Server names other profiles already list, for the editor to offer. */
export function mcpServerSuggestions(
  settings: Pick<McpProfileSettings, "mcpProfiles">,
  knownServers: ReadonlyArray<string> | null,
): ReadonlyArray<string> {
  const named = mcpProfileRows(settings, null).flatMap((row) =>
    row.state === "ok" && row.profile.servers !== "all" ? row.profile.servers : [],
  );
  return [...new Set([...(knownServers ?? []), ...named])].toSorted((a, b) => a.localeCompare(b));
}

const describeList = (list: "all" | ReadonlyArray<string>, all: string) =>
  list === "all" ? all : list.length === 0 ? "none" : list.join(", ");

/** "Servers: all · T3 tools: threads, preview", and " · Built-in tools: none" when it drops them. */
export const describeMcpProfileSet = (profile: McpProfileSet): string =>
  `Servers: ${describeList(profile.servers, "all")} · T3 tools: ${describeList(profile.t3Tools, "all")}${
    profile.builtinTools === MCP_BUILTIN_TOOLS_NONE ? " · Built-in tools: none" : ""
  }`;

/** What the profile editor holds. */
export interface McpProfileDraft {
  readonly name: string;
  readonly servers: "all" | ReadonlyArray<string>;
  readonly t3Tools: "all" | ReadonlyArray<McpT3ToolGroup>;
  /** True for `builtinTools: "none"`; an edit keeps it unless the person clears it. */
  readonly noBuiltinTools?: boolean;
}

export const emptyMcpProfileDraft = (): McpProfileDraft => ({
  name: "",
  servers: [],
  t3Tools: "all",
  noBuiltinTools: false,
});

export const draftOf = (name: string, profile: McpProfileSet): McpProfileDraft => ({
  name,
  servers: profile.servers,
  t3Tools: profile.t3Tools,
  noBuiltinTools: profile.builtinTools === MCP_BUILTIN_TOOLS_NONE,
});

/**
 * The patch that saves a profile (`editing`: the name of the one being edited, null for a new one),
 * or why it cannot be saved. Only that entry is sent.
 */
export function saveMcpProfilePatch(
  settings: Pick<McpProfileSettings, "mcpProfiles">,
  editing: string | null,
  draft: McpProfileDraft,
): { readonly patch: ServerSettingsPatch } | { readonly problem: string } {
  const name = editing ?? draft.name.trim();
  if (name === "") return { problem: "Enter a name." };
  if (isBuiltInMcpProfileName(name)) {
    return { problem: `"${name}" is a built-in profile; pick another name.` };
  }
  if (name === MCP_SUBAGENT_INHERIT) {
    return {
      problem: `"${MCP_SUBAGENT_INHERIT}" is the sub-agent rule's value; pick another name.`,
    };
  }
  if (!isDefinableMcpProfileName(name)) {
    return {
      problem: "Use lower-case letters, digits and dashes (at most 40), a letter or digit first.",
    };
  }
  if (editing === null && entriesOf(settings.mcpProfiles).some(([entry]) => entry === name)) {
    return { problem: `A profile named "${name}" already exists.` };
  }
  const servers =
    draft.servers === "all"
      ? "all"
      : [
          ...new Set(
            draft.servers.map((server) => server.trim()).filter((server) => server !== ""),
          ),
        ];
  const t3Tools =
    draft.t3Tools === "all"
      ? "all"
      : MCP_T3_TOOL_GROUPS.filter((group) =>
          (draft.t3Tools as ReadonlyArray<string>).includes(group),
        );
  const builtinTools =
    draft.noBuiltinTools === true ? { builtinTools: MCP_BUILTIN_TOOLS_NONE } : {};
  return { patch: { mcpProfiles: { [name]: { servers, t3Tools, ...builtinTools } } } };
}

/** Labels for the panel's messages: project and thread titles this client knows. */
export interface McpProfileLabels {
  readonly projects: ReadonlyMap<string, string>;
  readonly threads: ReadonlyMap<string, string>;
}

export interface McpProfileUsers {
  /** Plain descriptions of each setting that names it, the thread picks last. */
  readonly descriptions: ReadonlyArray<string>;
  /** The threads whose pick names it. */
  readonly threadIds: ReadonlyArray<string>;
}

/** Every setting that names `name`, by the server's own rule (`mcpProfileUses`). */
export function mcpProfileUsers(
  settings: McpProfilePanelSettings,
  name: string,
  labels: McpProfileLabels,
): McpProfileUsers {
  const descriptions: Array<string> = [];
  const threadIds: Array<string> = [];
  for (const use of mcpProfileUses(settings, name)) {
    const project = "projectId" in use ? (labels.projects.get(use.projectId) ?? use.projectId) : "";
    if (use.kind === "default") descriptions.push("the default profile");
    else if (use.kind === "subagent-rule") descriptions.push("the sub-agent rule");
    else if (use.kind === "project-default") {
      descriptions.push(`project ${project}'s default profile`);
    } else if (use.kind === "project-subagent-rule") {
      descriptions.push(`project ${project}'s sub-agent rule`);
    } else threadIds.push(use.threadId);
  }
  if (threadIds.length > 0) {
    const titles = threadIds.map((threadId) => labels.threads.get(threadId) ?? threadId);
    descriptions.push(
      `${threadIds.length === 1 ? "the pick of thread" : `the picks of ${threadIds.length} threads:`} ${titles.join(", ")}`,
    );
  }
  return { descriptions, threadIds };
}

/**
 * The patch that deletes a profile, or why not: a profile something names cannot be deleted, as
 * those threads would then be refused (never given `all`). The problem names each user.
 */
export function deleteMcpProfilePatch(
  settings: McpProfilePanelSettings,
  name: string,
  labels: McpProfileLabels,
):
  | { readonly patch: ServerSettingsPatch }
  | { readonly problem: string; readonly threadIds: ReadonlyArray<string> } {
  // A hand entry under a built-in's name may be removed: the built-in itself stays.
  if (!isBuiltInMcpProfileName(name)) {
    const users = mcpProfileUsers(settings, name, labels);
    if (users.descriptions.length > 0) {
      return {
        problem: `"${name}" is in use by ${users.descriptions.join("; ")}. Change those first.`,
        threadIds: users.threadIds,
      };
    }
  }
  return { patch: { mcpProfiles: { [name]: null } } };
}

/**
 * The patch that sets one thread's pick (`null` clears it), or null when `picks` already says so.
 * It names that thread alone: the server merges picks per key, so the other threads' picks stay,
 * also ones another client changed since this one read them.
 */
export function threadPickPatch(
  picks: Readonly<Record<string, string>>,
  threadId: string,
  name: string | null,
): ServerSettingsPatch | null {
  const current = Object.hasOwn(picks, threadId) ? picks[threadId] : undefined;
  if ((current ?? null) === name) return null;
  return { mcpThreadProfiles: { [threadId]: name } };
}

/** The patch that clears these threads' picks, and only theirs. */
export function clearThreadPicksPatch(threadIds: ReadonlyArray<string>): ServerSettingsPatch {
  return { mcpThreadProfiles: Object.fromEntries(threadIds.map((threadId) => [threadId, null])) };
}

/**
 * Why a new pick (thread id and chosen profile) cannot be saved, or null. Nothing is saved until
 * the owner has chosen a profile: a typed id alone never falls back to `all`.
 */
export function threadPickProblem(
  settings: Pick<McpProfileSettings, "mcpThreadProfiles">,
  threadId: string,
  profile: string | null,
): string | null {
  const id = threadId.trim();
  if (id === "") return "Enter a thread id.";
  if (profile === null || profile.trim() === "") return "Choose a profile for this thread.";
  if (Object.hasOwn(settings.mcpThreadProfiles, id)) return null;
  return Object.keys(settings.mcpThreadProfiles).length >= MCP_THREAD_PROFILES_MAX
    ? `At most ${MCP_THREAD_PROFILES_MAX} threads can have their own pick. Clear one first.`
    : null;
}

export interface McpThreadPickRow {
  readonly threadId: string;
  readonly title: string | null;
  /** The pick, or null for a blank one (a hand edit), which the resolver ignores. */
  readonly pick: string | null;
}

/** What a blank pick row says: it is broken (ignored), and how to fix it. */
export const MCP_BLANK_PICK_ISSUE =
  "Its pick in settings.json is blank, so it is ignored and the thread's defaults apply. Pick a profile, or clear it.";

/** The line above the closed Advanced fold when saved thread picks need a look, else null. */
export function mcpPickProblemSummary(blank: number, wider: number): string | null {
  const parts = [
    blank > 0 ? `${blank} thread ${blank === 1 ? "pick is" : "picks are"} blank and ignored` : null,
    wider > 0
      ? `${wider} thread ${wider === 1 ? "pick gives" : "picks give"} more than the thread's parent has`
      : null,
  ].filter((part) => part !== null);
  return parts.length === 0 ? null : `${parts.join("; ")}.`;
}

/** The thread picks, titled where this client knows the thread, by title then id. */
export function mcpThreadPickRows(
  picks: Readonly<Record<string, string>>,
  titles: ReadonlyMap<string, string>,
): ReadonlyArray<McpThreadPickRow> {
  return Object.entries(picks)
    .map(([threadId, pick]) => ({
      threadId,
      title: titles.get(threadId) ?? null,
      pick: pick.trim() || null,
    }))
    .toSorted(
      (a, b) =>
        Number(a.title === null) - Number(b.title === null) ||
        (a.title ?? "").localeCompare(b.title ?? "") ||
        a.threadId.localeCompare(b.threadId),
    );
}

/** A profile reference as the panel shows it: blank (not set) reads as `fallback`. */
export const shownProfileRef = (value: string | undefined, fallback: string): string => {
  const trimmed = value?.trim() ?? "";
  return trimmed === "" ? fallback : trimmed;
};

const SOURCE_TEXT = {
  "thread-pick": "picked for this thread",
  "subagent-rule": "the sub-agent rule",
  project: "the project's default",
  environment: "the default profile",
  "assistant-role": "the assistant thread's limit",
} as const;

/** One line on what a thread gets, from the resolver's result. */
export function effectiveMcpProfileText(resolution: McpProfileResolution): string {
  if (resolution._tag === "CannotApply") return `Cannot apply: ${resolution.message}`;
  const base = `${resolution.name} (${SOURCE_TEXT[resolution.source]})`;
  return resolution.narrowedByParent
    ? `${base}, cut down to the parent's: ${describeMcpProfileSet(resolution.profile)}`
    : base;
}
