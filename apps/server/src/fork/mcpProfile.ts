/**
 * Fork: which MCP profile a thread gets. Pure: the same settings and lineage give the same answer.
 * A missing or unreadable profile is never replaced by `all`: the result says it cannot apply.
 * Nothing calls it yet; the provider adapters and the T3 tool filter use it in later PRs.
 */
import {
  MCP_CARRIED_PROFILES_REFUSE,
  MCP_PROFILE_ALL,
  MCP_PROFILE_T3_ONLY,
  MCP_SUBAGENT_INHERIT,
  MCP_T3_TOOL_GROUPS,
  MCP_T3_UNGROUPED_TOOLS,
  type McpT3ToolGroup,
  ProviderDriverKind,
  type ServerSettings,
  isBuiltInMcpProfileName,
  isDefinableMcpProfileName,
} from "@t3tools/contracts";

/**
 * The settings the resolver reads: the server's MCP profile keys. Each is required, so a caller
 * must say what this machine does with a carried profile rather than get `apply` by leaving it out.
 */
export type McpProfileSettings = Pick<
  ServerSettings,
  "mcpProfiles" | "mcpProfile" | "mcpSubagentProfile" | "mcpThreadProfiles" | "mcpCarriedProfiles"
>;

/** One thread of a lineage, as the resolver needs it. */
export interface McpProfileThread {
  readonly threadId: string;
  /** The thread's project's overrides; absent or empty fields use the server's. */
  readonly project?: {
    readonly mcpProfile?: string | undefined;
    readonly mcpSubagentProfile?: string | undefined;
  };
  /** True for a delegated child thread (a `delegate_task` sub-agent or a task from a linked environment). */
  readonly delegated: boolean;
}

/** A profile this build can apply: `"all"` or a keep-list, for servers and for T3 tool groups. */
export interface McpProfileSet {
  readonly servers: "all" | ReadonlyArray<string>;
  readonly t3Tools: "all" | ReadonlyArray<McpT3ToolGroup>;
}

/** Where a thread's profile came from. */
export type McpProfileSource = "thread-pick" | "subagent-rule" | "project" | "environment";

export interface McpProfileResolved {
  readonly _tag: "Resolved";
  /** The profile's name, before any cut to the parent's. */
  readonly name: string;
  readonly source: McpProfileSource;
  /** What the thread gets. */
  readonly profile: McpProfileSet;
  /** True when the parent's profile took something away from the named one. */
  readonly narrowedByParent: boolean;
  /** T3 tool groups the profile names that this build does not know; they keep nothing. */
  readonly unknownToolGroups: ReadonlyArray<string>;
}

export type McpProfileProblem =
  /** No profile has this name (or the name is not one a profile may have). */
  | "missing"
  /**
   * The profile is there, but this build cannot read it (a typo, or a newer build wrote it), or a
   * hand edit put an entry in `mcpProfiles` under a built-in's name.
   */
  | "broken"
  /** The parent thread's own profile cannot be applied, so the child's cannot be cut down. */
  | "parent"
  /**
   * The profile another machine sent with the task it delegated here cannot be read by this build
   * (a newer build wrote fields this one does not know), so the task cannot be cut down to it.
   */
  | "carried"
  /**
   * Another machine sent a profile narrower than `all` (or one this build cannot read) with the
   * task, and this machine's `mcpCarriedProfiles` is `refuse`.
   */
  | "carried-refused"
  /** The lineage repeats a thread or is too deep. */
  | "lineage";

export interface McpProfileCannotApply {
  readonly _tag: "CannotApply";
  readonly problem: McpProfileProblem;
  /** The profile name at fault (the parent's for `parent`); empty for `lineage`. */
  readonly profileName: string;
  /** The thread whose setting is at fault. */
  readonly threadId: string;
  /** One plain sentence on what is wrong and how to fix it, for the thread. */
  readonly message: string;
  /**
   * Where the fault starts: for `parent`, the message of the first thread up the lineage that is
   * not refused for its parent's sake; for any other problem, `message` itself.
   */
  readonly cause: string;
}

export type McpProfileResolution = McpProfileResolved | McpProfileCannotApply;

/**
 * Each T3 toolkit's group, by its folder under src/mcp/toolkits; `null` for a toolkit that holds
 * only tools of `MCP_T3_UNGROUPED_TOOLS`. The T3 tool filter (a later PR) uses it. The test in
 * personOnlySettings.test.ts fails when a toolkit or tool maps to nothing.
 */
export const MCP_T3_TOOLKIT_GROUPS: Readonly<Record<string, McpT3ToolGroup | null>> = {
  orchestrator: "orchestration",
  preview: "preview",
  previewControls: "preview",
  pullRequests: "pull-requests",
  issues: "issues",
  thread: "threads",
  project: "projects",
  device: "device",
  worktree: "worktree",
  attachment: "attachments",
  environment: "environment",
  theme: "theme",
  html: "html",
  waits: "waits",
  message: null,
  toolSearch: null,
};

/**
 * The toolkit each of `MCP_T3_UNGROUPED_TOOLS` lives in. A tool of the same name in another toolkit
 * is not ungrouped: it is in that toolkit's group. The test in personOnlySettings.test.ts checks
 * each row against the toolkit files.
 */
export const MCP_T3_UNGROUPED_TOOL_KITS: Readonly<Record<string, string>> = {
  request_secret: "orchestrator",
  send_to_user: "message",
  tool_search: "toolSearch",
};

/** {@link mcpT3ToolGroup}'s answer for a tool no profile may keep. */
export const MCP_T3_TOOL_NOT_ALLOWED = "not-allowed";

/**
 * A T3 tool's group; `"ungrouped"` for one of `MCP_T3_UNGROUPED_TOOLS` in its own toolkit, which
 * every profile keeps; {@link MCP_T3_TOOL_NOT_ALLOWED} for a tool this build does not know (its
 * toolkit has no row in {@link MCP_T3_TOOLKIT_GROUPS}, or it is an ungrouped toolkit's other tool).
 */
export function mcpT3ToolGroup(
  toolkit: string,
  tool: string,
): McpT3ToolGroup | "ungrouped" | typeof MCP_T3_TOOL_NOT_ALLOWED {
  if (
    MCP_T3_UNGROUPED_TOOLS.includes(tool) &&
    Object.hasOwn(MCP_T3_UNGROUPED_TOOL_KITS, tool) &&
    MCP_T3_UNGROUPED_TOOL_KITS[tool] === toolkit
  ) {
    return "ungrouped";
  }
  const group = Object.hasOwn(MCP_T3_TOOLKIT_GROUPS, toolkit)
    ? MCP_T3_TOOLKIT_GROUPS[toolkit]
    : undefined;
  return group ?? MCP_T3_TOOL_NOT_ALLOWED;
}

/** Longer lineages are refused; real delegation chains are a few threads deep. */
export const MCP_PROFILE_MAX_LINEAGE = 64;

const BUILT_IN_PROFILES: Readonly<Record<string, McpProfileSet>> = {
  [MCP_PROFILE_ALL]: { servers: "all", t3Tools: "all" },
  [MCP_PROFILE_T3_ONLY]: { servers: [], t3Tools: "all" },
};

const KNOWN_GROUPS: ReadonlySet<string> = new Set(MCP_T3_TOOL_GROUPS);
const PROFILE_FIELDS: ReadonlySet<string> = new Set(["servers", "t3Tools"]);
const CARRIED_FIELDS: ReadonlySet<string> = new Set(["name", ...PROFILE_FIELDS]);

/**
 * The drivers whose threads apply an MCP profile: Claude only (fork/claudeMcpProfile.ts). Any
 * other provider would give a thread every MCP server and T3 tool, so a turn there whose profile
 * is narrower than `all` is refused as it starts (fork/mcpProfileTurnCheck.ts), and a delegation
 * refuses to send a narrower profile to one on another machine (fork/remoteMcpProfile.ts).
 */
export const MCP_PROFILE_DRIVERS: ReadonlySet<ProviderDriverKind> = new Set([
  ProviderDriverKind.make("claudeAgent"),
]);

/** True when the profile is today's behaviour: every outside server and every T3 tool. */
export const isEveryMcpTool = (profile: McpProfileSet): boolean =>
  profile.servers === "all" && profile.t3Tools === "all";

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const unique = <A>(values: ReadonlyArray<A>): ReadonlyArray<A> => [...new Set(values)];

type Parsed<A> = { readonly ok: true; readonly value: A } | { readonly ok: false; issue: string };

function parseServers(value: unknown): Parsed<McpProfileSet["servers"]> {
  if (value === "all") return { ok: true, value };
  if (!Array.isArray(value)) return { ok: false, issue: '`servers` must be "all" or a list' };
  const names: Array<string> = [];
  for (const entry of value) {
    const name = typeof entry === "string" ? entry.trim() : "";
    if (name === "") return { ok: false, issue: "`servers` holds an entry that is not a name" };
    names.push(name);
  }
  return { ok: true, value: unique(names) };
}

function parseT3Tools(
  value: unknown,
): Parsed<{ readonly groups: McpProfileSet["t3Tools"]; readonly unknown: ReadonlyArray<string> }> {
  if (value === "all") return { ok: true, value: { groups: "all", unknown: [] } };
  if (!Array.isArray(value)) return { ok: false, issue: '`t3Tools` must be "all" or a list' };
  const groups: Array<McpT3ToolGroup> = [];
  const unknown: Array<string> = [];
  for (const entry of value) {
    if (typeof entry !== "string") {
      return { ok: false, issue: "`t3Tools` holds an entry that is not a group name" };
    }
    // A group this build does not know keeps nothing, like a server that is not set up.
    if (KNOWN_GROUPS.has(entry)) groups.push(entry as McpT3ToolGroup);
    else unknown.push(entry);
  }
  return { ok: true, value: { groups: unique(groups), unknown: unique(unknown) } };
}

type Lookup =
  | { readonly ok: true; readonly profile: McpProfileSet; readonly unknown: ReadonlyArray<string> }
  | { readonly ok: false; readonly problem: "missing" | "broken"; readonly issue: string };

/** A named profile: a built-in, or an entry of `mcpProfiles` checked now. */
function lookUp(settings: McpProfileSettings, name: string): Lookup {
  const profiles: unknown = settings.mcpProfiles;
  const builtIn = Object.hasOwn(BUILT_IN_PROFILES, name) ? BUILT_IN_PROFILES[name] : undefined;
  if (builtIn !== undefined) {
    // A save never writes an entry under a built-in's name, so one is a hand edit. Which of the
    // two the owner meant is unknown, so neither is used.
    if (isRecord(profiles) && Object.hasOwn(profiles, name)) {
      return {
        ok: false,
        problem: "broken",
        issue: `mcpProfiles also has an entry named "${name}", a built-in profile's name`,
      };
    }
    return { ok: true, profile: builtIn, unknown: [] };
  }
  if (!isDefinableMcpProfileName(name) || !isRecord(profiles) || !Object.hasOwn(profiles, name)) {
    return { ok: false, problem: "missing", issue: "" };
  }
  const entry = profiles[name];
  if (!isRecord(entry)) return { ok: false, problem: "broken", issue: "it is not an object" };
  const extra = Object.keys(entry).filter((field) => !PROFILE_FIELDS.has(field));
  if (extra.length > 0) {
    return {
      ok: false,
      problem: "broken",
      issue: `it has fields this version does not know (${extra.join(", ")})`,
    };
  }
  const servers = parseServers(entry.servers);
  if (!servers.ok) return { ok: false, problem: "broken", issue: servers.issue };
  const t3Tools = parseT3Tools(entry.t3Tools);
  if (!t3Tools.ok) return { ok: false, problem: "broken", issue: t3Tools.issue };
  return {
    ok: true,
    profile: { servers: servers.value, t3Tools: t3Tools.value.groups },
    unknown: t3Tools.value.unknown,
  };
}

/**
 * A delegating thread's resolved profile as another machine sends it with a task: its name and
 * its `servers` and `t3Tools`. {@link resolveMcpProfile} cuts the task's own profile down to it.
 */
export type McpProfileCarried = {
  readonly name: string;
  readonly servers: McpProfileSet["servers"];
  readonly t3Tools: McpProfileSet["t3Tools"];
};

/** A resolved profile as it is carried to another machine with a delegated task. */
export const carryMcpProfile = (resolved: McpProfileResolved): McpProfileCarried => ({
  name: resolved.name,
  servers: resolved.profile.servers,
  t3Tools: resolved.profile.t3Tools,
});

type CarriedLookup =
  | { readonly ok: true; readonly name: string; readonly profile: McpProfileSet }
  | { readonly ok: false; readonly name: string; readonly issue: string };

/**
 * A carried profile, checked as strictly as a settings entry: a field this build does not know
 * could narrow the profile, so it refuses rather than being dropped. T3 tool groups it does not
 * know keep nothing, which only narrows.
 */
function readCarried(value: unknown): CarriedLookup {
  const name = isRecord(value) && typeof value.name === "string" ? value.name : "";
  if (!isRecord(value)) return { ok: false, name, issue: "it is not an object" };
  const extra = Object.keys(value).filter((field) => !CARRIED_FIELDS.has(field));
  if (extra.length > 0) {
    return {
      ok: false,
      name,
      issue: `it has fields this version does not know (${extra.join(", ")})`,
    };
  }
  if (name.trim() === "") return { ok: false, name, issue: "it has no name" };
  const servers = parseServers(value.servers);
  if (!servers.ok) return { ok: false, name, issue: servers.issue };
  const t3Tools = parseT3Tools(value.t3Tools);
  if (!t3Tools.ok) return { ok: false, name, issue: t3Tools.issue };
  return { ok: true, name, profile: { servers: servers.value, t3Tools: t3Tools.value.groups } };
}

/** A set value; empty or absent means "not set". */
const nonEmpty = (value: string | undefined): string | undefined => {
  const trimmed = value?.trim();
  return trimmed === undefined || trimmed === "" ? undefined : trimmed;
};

/** The profile name a thread asks for on its own, before any cut to its parent's. */
function ownChoice(
  settings: McpProfileSettings,
  thread: McpProfileThread,
): { readonly name: string; readonly source: McpProfileSource } {
  const picks: unknown = settings.mcpThreadProfiles;
  const pick =
    isRecord(picks) && Object.hasOwn(picks, thread.threadId) ? picks[thread.threadId] : undefined;
  if (typeof pick === "string" && nonEmpty(pick) !== undefined) {
    return { name: pick.trim(), source: "thread-pick" };
  }
  if (thread.delegated) {
    const rule =
      nonEmpty(thread.project?.mcpSubagentProfile) ??
      nonEmpty(settings.mcpSubagentProfile) ??
      MCP_SUBAGENT_INHERIT;
    if (rule !== MCP_SUBAGENT_INHERIT) return { name: rule, source: "subagent-rule" };
  }
  const project = nonEmpty(thread.project?.mcpProfile);
  if (project !== undefined) return { name: project, source: "project" };
  return { name: nonEmpty(settings.mcpProfile) ?? MCP_PROFILE_ALL, source: "environment" };
}

/** `child` cut down to `parent`: a keep-list keeps only what both keep. */
function cutTo(child: McpProfileSet, parent: McpProfileSet): McpProfileSet {
  const servers =
    parent.servers === "all"
      ? child.servers
      : child.servers === "all"
        ? parent.servers
        : child.servers.filter((name) => parent.servers.includes(name));
  const t3Tools =
    parent.t3Tools === "all"
      ? child.t3Tools
      : child.t3Tools === "all"
        ? parent.t3Tools
        : child.t3Tools.filter((group) => parent.t3Tools.includes(group));
  return { servers, t3Tools };
}

const sameList = (a: "all" | ReadonlyArray<string>, b: "all" | ReadonlyArray<string>) =>
  a === "all" || b === "all"
    ? a === b
    : a.length === b.length && a.every((value) => b.includes(value));

/** True when `child` keeps nothing `parent` does not: cutting it to `parent` takes nothing away. */
export function isWithinMcpProfile(child: McpProfileSet, parent: McpProfileSet): boolean {
  const cut = cutTo(child, parent);
  return sameList(cut.servers, child.servers) && sameList(cut.t3Tools, child.t3Tools);
}

const FIX = `pick "${MCP_PROFILE_ALL}" for this thread, or fix the profile in Settings`;

/** ` "name"` for a message, or nothing when the name is empty (a carried value it cannot read). */
const quoted = (profileName: string): string =>
  profileName.trim() === "" ? "" : ` "${profileName}"`;

function cannotApply(
  problem: McpProfileProblem,
  thread: McpProfileThread,
  profileName: string,
  issue: string,
): McpProfileCannotApply {
  const name = quoted(profileName);
  const message =
    problem === "missing"
      ? `MCP profile${name} is not set up, so this thread cannot start: ${FIX}.`
      : problem === "broken"
        ? `MCP profile${name} cannot be read by this version (${issue}), so this thread cannot start: ${
            // Only an entry under its name breaks a built-in; picking it again would not help.
            isBuiltInMcpProfileName(profileName)
              ? `remove or rename that entry in the settings file`
              : FIX
          }.`
        : problem === "parent"
          ? `The parent thread's MCP profile${name} cannot be applied, so this thread cannot start: ${FIX}. The reason up the chain: ${issue}`
          : problem === "carried"
            ? `The MCP profile${name} of the thread on another machine that delegated this one cannot be read by this version (${issue}), so this thread cannot start: update T3 here, or pick a profile for this thread in Settings.`
            : problem === "carried-refused"
              ? // `issue` is set only for a carried value this version cannot read, which `apply`
                // refuses too, so setting it would not help.
                `This thread is a task another machine delegated here with ${
                  issue !== ""
                    ? `an MCP profile${name} this version cannot read (${issue})`
                    : `its MCP profile${name}`
                }, and this machine is set to refuse such tasks (mcpCarriedProfiles is "${MCP_CARRIED_PROFILES_REFUSE}"), so it cannot start: delegate the task on that machine, ${
                  issue !== "" ? "" : `set mcpCarriedProfiles to "apply" here, `
                }or pick a profile for this thread in Settings.`
              : `This thread's parent threads ${issue}, so its MCP profile cannot be worked out: ${FIX}.`;
  return {
    _tag: "CannotApply",
    problem,
    profileName,
    threadId: thread.threadId,
    message,
    cause: problem === "parent" ? issue : message,
  };
}

function resolveAt(
  settings: McpProfileSettings,
  lineage: ReadonlyArray<McpProfileThread>,
  index: number,
  carried: unknown,
): McpProfileResolution {
  const thread = lineage[index]!;
  const choice = ownChoice(settings, thread);
  const found = lookUp(settings, choice.name);
  if (!found.ok) return cannotApply(found.problem, thread, choice.name, found.issue);
  const own: McpProfileResolved = {
    _tag: "Resolved",
    name: choice.name,
    source: choice.source,
    profile: found.profile,
    narrowedByParent: false,
    unknownToolGroups: found.unknown,
  };
  // The owner's pick is final; a thread without a parent, here or on the machine that delegated
  // it, has nothing to be cut to.
  if (choice.source === "thread-pick") return own;
  const top = index === lineage.length - 1;
  if (top && carried === undefined) return own;
  let parentProfile: McpProfileSet;
  if (top) {
    const sent = readCarried(carried);
    // This machine takes no part in carrying: a profile that would cut the task, or one it
    // cannot read, refuses it instead. `all` cuts nothing, so it is the same either way.
    if (
      settings.mcpCarriedProfiles === MCP_CARRIED_PROFILES_REFUSE &&
      !(sent.ok && isEveryMcpTool(sent.profile))
    ) {
      return cannotApply("carried-refused", thread, sent.name, sent.ok ? "" : sent.issue);
    }
    if (!sent.ok) return cannotApply("carried", thread, sent.name, sent.issue);
    parentProfile = sent.profile;
  } else {
    const parent = resolveAt(settings, lineage, index + 1, carried);
    if (parent._tag === "CannotApply") {
      return cannotApply("parent", thread, parent.profileName, parent.cause);
    }
    parentProfile = parent.profile;
  }
  const profile = cutTo(own.profile, parentProfile);
  return {
    ...own,
    profile,
    narrowedByParent:
      !sameList(profile.servers, own.profile.servers) ||
      !sameList(profile.t3Tools, own.profile.t3Tools),
  };
}

/**
 * The MCP profile of `lineage[0]`; `lineage` goes up through parents (a fork's source counts).
 * The owner's pick is final; else the role default (`mcpSubagentProfile` for a delegated child
 * unless `inherit`, else `mcpProfile`; a project's override wins), cut down to the parent's, and
 * the top thread's to `carried`, the profile another machine sent with it (if any).
 */
export function resolveMcpProfile(
  settings: McpProfileSettings,
  lineage: ReadonlyArray<McpProfileThread>,
  carried?: unknown,
): McpProfileResolution {
  const self = lineage[0];
  if (self === undefined) throw new Error("resolveMcpProfile needs the thread itself");
  if (lineage.length > MCP_PROFILE_MAX_LINEAGE) {
    return cannotApply("lineage", self, "", `are more than ${MCP_PROFILE_MAX_LINEAGE} deep`);
  }
  if (new Set(lineage.map((thread) => thread.threadId)).size !== lineage.length) {
    return cannotApply("lineage", self, "", "repeat a thread");
  }
  return resolveAt(settings, lineage, 0, carried);
}
