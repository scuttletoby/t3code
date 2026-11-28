/**
 * Fork: which MCP profile a thread gets. Pure: the same settings and lineage give the same answer.
 * A missing or unreadable profile is never replaced by `all`: the result says it cannot apply.
 * The server applies it (apps/server/src/fork/mcpProfile.ts re-exports it); the app shows a
 * thread's result in its menu and reads profile entries by the same rules in Settings.
 */
import {
  MCP_BUILTIN_TOOLS_NONE,
  MCP_CARRIED_PROFILES_REFUSE,
  MCP_PROFILE_ALL,
  MCP_PROFILE_ASSISTANT,
  MCP_PROFILE_T3_ONLY,
  MCP_SUBAGENT_INHERIT,
  MCP_T3_NARROW_GROUPS,
  MCP_T3_TOOL_GROUPS,
  type McpT3ToolGroup,
  isBuiltInMcpProfileName,
  isDefinableMcpProfileName,
} from "./mcpProfile.ts";
import type { ServerSettings } from "./settings.ts";

/**
 * The settings the resolver reads: the server's MCP profile keys. Each is required, so a caller
 * must say what this machine does with a carried profile rather than get `apply` by leaving it out.
 */
export type McpProfileSettings = Pick<
  ServerSettings,
  "mcpProfiles" | "mcpProfile" | "mcpSubagentProfile" | "mcpThreadProfiles" | "mcpCarriedProfiles"
> & {
  /**
   * The role pointers; only `assistant` is read: that thread never gets more than the built-in
   * `assistant` profile (see {@link resolveMcpProfile}).
   */
  readonly threadRoles: { readonly assistant: string | null };
};

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

/**
 * A profile this build can apply: `"all"` or a keep-list, for servers and for T3 tool groups; and
 * `builtinTools: "none"` for a thread with no built-in provider tools (absent means all of them).
 */
export interface McpProfileSet {
  readonly servers: "all" | ReadonlyArray<string>;
  readonly t3Tools: "all" | ReadonlyArray<McpT3ToolGroup>;
  readonly builtinTools?: typeof MCP_BUILTIN_TOOLS_NONE;
}

/**
 * Where a thread's profile came from. `assistant-role`: the thread is `threadRoles.assistant` and
 * what it would get otherwise keeps more than the built-in `assistant` profile, so it gets that.
 */
export type McpProfileSource =
  | "thread-pick"
  | "subagent-rule"
  | "project"
  | "environment"
  | "assistant-role";

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

/** Longer lineages are refused; real delegation chains are a few threads deep. */
export const MCP_PROFILE_MAX_LINEAGE = 64;

/** What each built-in profile gives; Settings lists them from here. */
export const MCP_BUILT_IN_PROFILES: Readonly<Record<string, McpProfileSet>> = {
  [MCP_PROFILE_ALL]: { servers: "all", t3Tools: "all" },
  [MCP_PROFILE_T3_ONLY]: { servers: [], t3Tools: "all" },
  [MCP_PROFILE_ASSISTANT]: {
    servers: [],
    // Only these: never `orchestration`, `threads` or `projects` whole (the owner's decision).
    t3Tools: ["read", "start", "message"],
    builtinTools: MCP_BUILTIN_TOOLS_NONE,
  },
};

const KNOWN_GROUPS: ReadonlySet<string> = new Set(MCP_T3_TOOL_GROUPS);
const PROFILE_FIELDS: ReadonlySet<string> = new Set(["servers", "t3Tools", "builtinTools"]);
const CARRIED_FIELDS: ReadonlySet<string> = new Set(["name", ...PROFILE_FIELDS]);

/** True when the profile is today's behaviour: every outside server, T3 tool and built-in tool. */
export const isEveryMcpTool = (profile: McpProfileSet): boolean =>
  profile.servers === "all" && profile.t3Tools === "all" && profile.builtinTools === undefined;

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

/**
 * `builtinTools`: absent or `"all"` keeps them; `"none"` drops them. Any other value (a typo, or a
 * newer build's) refuses the profile, so it never reads as more than was meant.
 */
function parseBuiltinTools(value: unknown): Parsed<McpProfileSet["builtinTools"]> {
  if (value === undefined || value === "all") return { ok: true, value: undefined };
  if (value === MCP_BUILTIN_TOOLS_NONE) return { ok: true, value };
  return { ok: false, issue: '`builtinTools` must be "all" or "none"' };
}

/** `servers` and `t3Tools` with `builtinTools` added only when it drops them. */
const withBuiltinTools = (
  set: Omit<McpProfileSet, "builtinTools">,
  builtinTools: McpProfileSet["builtinTools"],
): McpProfileSet => (builtinTools === undefined ? set : { ...set, builtinTools });

type Lookup =
  | { readonly ok: true; readonly profile: McpProfileSet; readonly unknown: ReadonlyArray<string> }
  | { readonly ok: false; readonly problem: "missing" | "broken"; readonly issue: string };

/** A named profile: a built-in, or an entry of `mcpProfiles` checked now. */
function lookUp(settings: McpProfileSettings, name: string): Lookup {
  const profiles: unknown = settings.mcpProfiles;
  const builtIn = Object.hasOwn(MCP_BUILT_IN_PROFILES, name)
    ? MCP_BUILT_IN_PROFILES[name]
    : undefined;
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
  const read = readMcpProfileEntry(profiles[name]);
  return read.ok ? read : { ok: false, problem: "broken", issue: read.issue };
}

/** One entry of `mcpProfiles` as this build reads it; `issue` says why it cannot be used. */
export type McpProfileEntryRead =
  | { readonly ok: true; readonly profile: McpProfileSet; readonly unknown: ReadonlyArray<string> }
  | { readonly ok: false; readonly issue: string };

/**
 * One entry of `mcpProfiles`, checked as a thread that uses it checks it: a field this build does
 * not know refuses the entry (it could narrow it); a T3 tool group it does not know keeps nothing
 * and is listed in `unknown`. Settings shows each entry by this read.
 */
export function readMcpProfileEntry(entry: unknown): McpProfileEntryRead {
  if (!isRecord(entry)) return { ok: false, issue: "it is not an object" };
  const extra = Object.keys(entry).filter((field) => !PROFILE_FIELDS.has(field));
  if (extra.length > 0) {
    return { ok: false, issue: `it has fields this version does not know (${extra.join(", ")})` };
  }
  const servers = parseServers(entry.servers);
  if (!servers.ok) return { ok: false, issue: servers.issue };
  const t3Tools = parseT3Tools(entry.t3Tools);
  if (!t3Tools.ok) return { ok: false, issue: t3Tools.issue };
  const builtinTools = parseBuiltinTools(entry.builtinTools);
  if (!builtinTools.ok) return { ok: false, issue: builtinTools.issue };
  return {
    ok: true,
    profile: withBuiltinTools(
      { servers: servers.value, t3Tools: t3Tools.value.groups },
      builtinTools.value,
    ),
    unknown: t3Tools.value.unknown,
  };
}

/**
 * A delegating thread's resolved profile as another machine sends it with a task: its name, its
 * `servers` and `t3Tools`, and `builtinTools` only when `"none"` (a machine from before that field
 * refuses such a task, never widens it). {@link resolveMcpProfile} cuts the task's own profile down
 * to it.
 */
export type McpProfileCarried = {
  readonly name: string;
  readonly servers: McpProfileSet["servers"];
  readonly t3Tools: McpProfileSet["t3Tools"];
  readonly builtinTools?: typeof MCP_BUILTIN_TOOLS_NONE;
};

/** A resolved profile as it is carried to another machine with a delegated task. */
export const carryMcpProfile = (resolved: McpProfileResolved): McpProfileCarried => ({
  name: resolved.name,
  ...resolved.profile,
});

export type CarriedLookup =
  | { readonly ok: true; readonly name: string; readonly profile: McpProfileSet }
  | { readonly ok: false; readonly name: string; readonly issue: string };

/**
 * A carried profile, checked as strictly as a settings entry: a field this build does not know
 * could narrow the profile, so it refuses rather than being dropped. T3 tool groups it does not
 * know keep nothing, which only narrows. Also reads a profile a scheduled task was saved with
 * (`scheduledTaskMcpProfile.ts`).
 */
export function readCarried(value: unknown): CarriedLookup {
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
  const builtinTools = parseBuiltinTools(value.builtinTools);
  if (!builtinTools.ok) return { ok: false, name, issue: builtinTools.issue };
  const profile = withBuiltinTools(
    { servers: servers.value, t3Tools: t3Tools.value.groups },
    builtinTools.value,
  );
  // A carried `assistant` is re-narrowed as it is read: one recorded before the split names
  // the whole groups `orchestration`, `threads` and `projects`, and keeps only what the built-in
  // `assistant` keeps now. An older build's own profile of that name is cut the same way.
  if (name === MCP_PROFILE_ASSISTANT) {
    return {
      ok: true,
      name,
      profile: cutTo(profile, MCP_BUILT_IN_PROFILES[MCP_PROFILE_ASSISTANT]!),
    };
  }
  return { ok: true, name, profile };
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

/**
 * True when the keep-list has the group, or (for a narrow group) every whole group its tools are
 * in (`MCP_T3_NARROW_GROUPS`), which then keeps all of its tools too.
 */
const keepsGroup = (groups: ReadonlyArray<McpT3ToolGroup>, group: McpT3ToolGroup): boolean => {
  if (groups.includes(group)) return true;
  const wholes = MCP_T3_NARROW_GROUPS[group];
  return wholes !== undefined && wholes.every((whole) => groups.includes(whole));
};

/**
 * The T3 groups two keep-lists both keep: each group one list keeps that the other also keeps
 * (a narrow group counts as kept by a list with all of its whole groups). A whole group cut to a
 * list of narrow groups keeps those narrow groups, never the whole group.
 */
const bothKeep = (
  child: ReadonlyArray<McpT3ToolGroup>,
  parent: ReadonlyArray<McpT3ToolGroup>,
): ReadonlyArray<McpT3ToolGroup> =>
  unique([
    ...child.filter((group) => keepsGroup(parent, group)),
    ...parent.filter((group) => !child.includes(group) && keepsGroup(child, group)),
  ]);

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
        : bothKeep(child.t3Tools, parent.t3Tools);
  // Either one without built-in tools leaves the thread without them.
  return withBuiltinTools({ servers, t3Tools }, child.builtinTools ?? parent.builtinTools);
}

const sameList = (a: "all" | ReadonlyArray<string>, b: "all" | ReadonlyArray<string>) =>
  a === "all" || b === "all"
    ? a === b
    : a.length === b.length && a.every((value) => b.includes(value));

/** True when `child` keeps nothing `parent` does not: cutting it to `parent` takes nothing away. */
export function isWithinMcpProfile(child: McpProfileSet, parent: McpProfileSet): boolean {
  const cut = cutTo(child, parent);
  return (
    sameList(cut.servers, child.servers) &&
    sameList(cut.t3Tools, child.t3Tools) &&
    cut.builtinTools === child.builtinTools
  );
}

/**
 * What `child` keeps that `parent` does not, named for a refusal ("MCP servers", "T3 tools",
 * "built-in tools", joined with "or"); an empty string when `child` is within `parent`.
 */
export function mcpProfileWiderParts(child: McpProfileSet, parent: McpProfileSet): string {
  const cut = cutTo(child, parent);
  const parts = [
    ...(sameList(cut.servers, child.servers) ? [] : ["MCP servers"]),
    ...(sameList(cut.t3Tools, child.t3Tools) ? [] : ["T3 tools"]),
    ...(cut.builtinTools === child.builtinTools ? [] : ["built-in tools"]),
  ];
  return parts.length <= 1
    ? (parts[0] ?? "")
    : `${parts.slice(0, -1).join(", ")} or ${parts[parts.length - 1]}`;
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

/**
 * The thread's profile, held within the built-in `assistant` for the `threadRoles.assistant` thread,
 * so a managed policy that drops its pick cannot give it the wide defaults. Within stands; wider is
 * cut to `assistant`; a broken `assistant` refuses.
 */
function resolveAt(
  settings: McpProfileSettings,
  lineage: ReadonlyArray<McpProfileThread>,
  index: number,
  carried: unknown,
): McpProfileResolution {
  const thread = lineage[index]!;
  const resolved = resolveOwnAt(settings, lineage, index, carried);
  const roles: unknown = settings.threadRoles;
  const assistant = isRecord(roles) ? roles.assistant : undefined;
  if (resolved._tag === "CannotApply" || assistant !== thread.threadId) return resolved;
  const bound = lookUp(settings, MCP_PROFILE_ASSISTANT);
  if (!bound.ok) return cannotApply(bound.problem, thread, MCP_PROFILE_ASSISTANT, bound.issue);
  if (isWithinMcpProfile(resolved.profile, bound.profile)) return resolved;
  return {
    ...resolved,
    name: MCP_PROFILE_ASSISTANT,
    source: "assistant-role",
    profile: cutTo(resolved.profile, bound.profile),
  };
}

function resolveOwnAt(
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
      !sameList(profile.t3Tools, own.profile.t3Tools) ||
      profile.builtinTools !== own.profile.builtinTools,
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
