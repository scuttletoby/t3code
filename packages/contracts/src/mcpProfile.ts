/**
 * Fork: MCP profile settings. A profile names the outside MCP servers and T3 tool groups a thread
 * gets. Only a person sets them; no agent tool takes these keys. Off by default: every thread
 * resolves to `all`, today's behaviour. The resolver is apps/server/src/fork/mcpProfile.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedString } from "./baseSchemas.ts";

/** Today's behaviour: every outside server and every T3 tool. */
export const MCP_PROFILE_ALL = "all";
/** No outside servers, every T3 tool. */
export const MCP_PROFILE_T3_ONLY = "t3-only";
/**
 * The assistant's: no outside servers, no built-in tools, and only the T3 groups `read`, `start`
 * and `message` (docs/fork/assistant.md lists their tools).
 */
export const MCP_PROFILE_ASSISTANT = "assistant";
/** The profiles every build knows; `mcpProfiles` cannot redefine them. */
const MCP_BUILT_IN_PROFILE_NAMES: ReadonlyArray<string> = [
  MCP_PROFILE_ALL,
  MCP_PROFILE_T3_ONLY,
  MCP_PROFILE_ASSISTANT,
];
/**
 * A profile's `builtinTools`: `none` starts the thread with no built-in tools of the provider (no
 * shell, files or web; only MCP tools). Only Claude applies it; a turn on any other driver is
 * refused. Absent or `all` is today's behaviour.
 */
export const MCP_BUILTIN_TOOLS_NONE = "none";
const MCP_BUILTIN_TOOLS_VALUES: ReadonlyArray<string> = ["all", MCP_BUILTIN_TOOLS_NONE];
/** `mcpSubagentProfile` value: a delegated child follows `mcpProfile` like any other thread. */
export const MCP_SUBAGENT_INHERIT = "inherit";

const MCP_PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/u;

/** A profile's name: lower-case letters, digits and dashes, at most 40. */
export const McpProfileName = Schema.String.check(Schema.isPattern(MCP_PROFILE_NAME_PATTERN));
export type McpProfileName = typeof McpProfileName.Type;

/** True for a built-in profile's name: `all`, `t3-only` and `assistant`. */
export const isBuiltInMcpProfileName = (name: string): boolean =>
  MCP_BUILT_IN_PROFILE_NAMES.includes(name);

/** True for a name a profile in `mcpProfiles` may have: valid, not built in, not `inherit`. */
export const isDefinableMcpProfileName = (name: string): boolean =>
  MCP_PROFILE_NAME_PATTERN.test(name) &&
  name !== MCP_SUBAGENT_INHERIT &&
  !isBuiltInMcpProfileName(name);

/** A profile name a thread can run with: a valid name other than `inherit`. */
const McpProfileChoice = McpProfileName.check(
  Schema.makeFilter((name: string) => name !== MCP_SUBAGENT_INHERIT, {
    expected: `a profile name other than ${MCP_SUBAGENT_INHERIT} (only mcpSubagentProfile takes ${MCP_SUBAGENT_INHERIT})`,
  }),
);

/**
 * Groups of T3's own tools: one per toolkit (`preview` has two); `playtest`, a waits tool that runs
 * build and deploy steps; and the narrow groups ({@link MCP_T3_NARROW_GROUPS}). {@link
 * MCP_T3_UNGROUPED_TOOLS} are in no group; apps/server/src/fork/mcpProfile.ts maps each tool.
 */
export const MCP_T3_TOOL_GROUPS = [
  "orchestration",
  "preview",
  "pull-requests",
  "issues",
  "threads",
  "projects",
  "device",
  "worktree",
  "attachments",
  "environment",
  "theme",
  "html",
  "waits",
  "playtest",
  "workflows",
  "feedback",
  "read",
  "start",
  "message",
] as const;
export type McpT3ToolGroup = (typeof MCP_T3_TOOL_GROUPS)[number];

/**
 * Narrow groups, each with the whole groups its tools are in: `read` (changes nothing), `start`
 * (delegate, make or launch a thread) and `message` (write to a thread). One is within a profile
 * that keeps all of its whole groups; apps/server/src/fork/mcpProfile.ts names each tool.
 */
export const MCP_T3_NARROW_GROUPS: Readonly<
  Partial<Record<McpT3ToolGroup, ReadonlyArray<McpT3ToolGroup>>>
> = {
  read: ["orchestration", "threads", "projects"],
  start: ["orchestration", "projects"],
  message: ["orchestration"],
};

/**
 * T3 tools every profile keeps, in no group: the two message tools, and `tool_search`, which only
 * finds tools the thread may already use (the server's T3 tool filter leaves the others out of its
 * results).
 */
export const MCP_T3_UNGROUPED_TOOLS: ReadonlyArray<string> = [
  "request_secret",
  "send_to_user",
  "tool_search",
];

const isRecord = (value: unknown): value is { readonly [key: string]: unknown } =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * The settings file's profiles. Each entry is kept as written and checked only when a thread uses
 * it, so one bad entry (or one written by a newer build, with fields this build does not know)
 * never stops the settings file from loading, and a save keeps it as it was.
 */
export const McpProfilesFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.Record(Schema.String, Schema.Unknown),
    SchemaTransformation.transform({
      // A map that is not an object (a hand edit gone wrong) reads as no profiles; a thread that
      // names one is then refused, never widened.
      decode: (raw): { readonly [name: string]: unknown } => (isRecord(raw) ? raw : {}),
      encode: (profiles) => profiles,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed({})),
);

const MCP_PROFILE_FIELDS: ReadonlyArray<string> = ["servers", "t3Tools"];
const MCP_PROFILE_OPTIONAL_FIELDS: ReadonlyArray<string> = ["builtinTools"];
const KNOWN_GROUPS: ReadonlyArray<string> = MCP_T3_TOOL_GROUPS;

const isAllOrListOf = (value: unknown, isEntry: (entry: unknown) => boolean): boolean =>
  value === "all" || (Array.isArray(value) && value.every(isEntry));

/**
 * A profile a save may store: `servers` (non-blank names) and `t3Tools` (known groups), each
 * `"all"` or a keep-list; a keep-list leaves out anything not named; and optionally `builtinTools`
 * (`"all"` or `"none"`). An unknown field is refused, not dropped, so a save never stores a profile
 * cut down from what the person wrote.
 */
const isSavableMcpProfile = (entry: unknown): boolean =>
  isRecord(entry) &&
  MCP_PROFILE_FIELDS.every((field) => Object.hasOwn(entry, field)) &&
  Object.keys(entry).every(
    (field) => MCP_PROFILE_FIELDS.includes(field) || MCP_PROFILE_OPTIONAL_FIELDS.includes(field),
  ) &&
  (!Object.hasOwn(entry, "builtinTools") ||
    (typeof entry.builtinTools === "string" &&
      MCP_BUILTIN_TOOLS_VALUES.includes(entry.builtinTools))) &&
  isAllOrListOf(entry.servers, (name) => typeof name === "string" && name.trim() !== "") &&
  isAllOrListOf(
    entry.t3Tools,
    (group) => typeof group === "string" && KNOWN_GROUPS.includes(group),
  );

/**
 * Profiles in a settings patch: each entry replaces the stored one whole; `null` removes it (also
 * under a built-in name, to remove a hand-edited entry that breaks it). Entries are `unknown`, so a
 * client may send back what it read. Names are checked on the whole map: Effect drops, rather than
 * refuses, a record key its key schema rejects.
 */
export const McpProfilesPatch = Schema.Record(Schema.String, Schema.Unknown)
  .check(
    Schema.makeFilter(
      (profiles: { readonly [name: string]: unknown }) =>
        Object.entries(profiles).every(
          ([name, entry]) =>
            isDefinableMcpProfileName(name) || (entry === null && isBuiltInMcpProfileName(name)),
        ),
      {
        expected: `profile names of lower-case letters, digits and dashes (at most 40), other than ${MCP_BUILT_IN_PROFILE_NAMES.join(", ")} (null only, to remove one) and ${MCP_SUBAGENT_INHERIT}`,
      },
    ),
  )
  .check(
    Schema.makeFilter(
      (profiles: { readonly [name: string]: unknown }) =>
        Object.values(profiles).every((entry) => entry === null || isSavableMcpProfile(entry)),
      {
        expected: `profiles with only ${MCP_PROFILE_FIELDS.join(" and ")}, each "all" or a list (servers: non-blank names; t3Tools: ${MCP_T3_TOOL_GROUPS.join(", ")}), and optionally builtinTools ("all" or "none"), or null to remove one`,
      },
    ),
  );

/**
 * What a profile reference in the settings file reads as. Text is trimmed (empty means "not set");
 * `null` reads as not set. Any other value (a hand edit gone wrong, or a newer build's) reads as a
 * marker no profile can have, so a thread that uses it is refused, never widened, and the rest of
 * the file still loads.
 */
const readMcpProfileRef = (value: unknown): string =>
  typeof value === "string"
    ? value.trim()
    : value === null || value === undefined
      ? ""
      : `(unreadable ${Array.isArray(value) ? "list" : typeof value})`;

/**
 * `mcpCarriedProfiles`: what this machine does with the MCP profile a linked machine sends with a
 * task it delegates here. `apply` (the default) cuts the task down to it. `refuse` takes no part in
 * carrying: a task that carries a profile narrower than `all` is refused here, and this machine
 * tells linked machines so, so they do not send one. Neither ever gives a task more.
 */
export const MCP_CARRIED_PROFILES_APPLY = "apply";
export const MCP_CARRIED_PROFILES_REFUSE = "refuse";
export const McpCarriedProfiles = Schema.Literals([
  MCP_CARRIED_PROFILES_APPLY,
  MCP_CARRIED_PROFILES_REFUSE,
]);
export type McpCarriedProfiles = typeof McpCarriedProfiles.Type;

/**
 * `mcpCarriedProfiles` as the settings file holds it: `apply` (any case, outer spaces ignored) or
 * `null` reads as `apply`, the default; anything else, `refuse` or a value it does not know (a typo,
 * or a newer build's), as `refuse`, which only refuses: a bad value never opens up to `apply`, and
 * the rest of the file still loads. A save writes only the two values.
 */
export const McpCarriedProfilesFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    McpCarriedProfiles,
    SchemaTransformation.transform<McpCarriedProfiles, unknown>({
      decode: (value) =>
        value === null ||
        (typeof value === "string" && value.trim().toLowerCase() === MCP_CARRIED_PROFILES_APPLY)
          ? MCP_CARRIED_PROFILES_APPLY
          : MCP_CARRIED_PROFILES_REFUSE,
      encode: (value) => value,
    }),
  ),
);

/** A profile reference as the settings file holds it; see {@link readMcpProfileRef}. */
export const McpProfileRef = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.String,
    SchemaTransformation.transform<string, unknown>({
      decode: readMcpProfileRef,
      encode: (ref) => ref,
    }),
  ),
);

/**
 * A project's profile reference in a settings patch: any text, as reading the file keeps it, so a
 * client that saves a project's other settings can send back what it read. The server refuses a
 * new value that names no profile when it saves (packages/shared/src/forkSettingsPatch.ts).
 */
export const McpProjectProfileRefPatch = TrimmedString;

/** The server's default profile in a settings patch: a profile name, not `inherit`. */
export const McpProfilePatch = McpProfileChoice;

/**
 * The owner's pick per thread in a settings patch; `null` removes the pick. A pick is a profile
 * name, not `inherit`. Thread ids are checked on the whole map, as for {@link McpProfilesPatch}.
 */
export const McpThreadProfilesPatch = Schema.Record(
  Schema.String,
  Schema.NullOr(McpProfileChoice),
).check(
  Schema.makeFilter(
    (picks: { readonly [threadId: string]: unknown }) =>
      Object.keys(picks).every((threadId) => threadId.length > 0 && threadId.trim() === threadId),
    { expected: "thread ids (non-blank, no outer spaces) as keys" },
  ),
);

/**
 * The most thread picks the settings hold. A save that would go over is refused (remove old picks
 * first); no pick is ever dropped to make room, since its thread would fall back to a wider role
 * default. Pruning deleted threads' picks comes later.
 */
export const MCP_THREAD_PROFILES_MAX = 500;

/**
 * The settings file's thread picks: thread id to profile name. Read leniently, like
 * {@link McpProfileRef}: a `null` pick is dropped (no pick), any other non-text pick is kept as a
 * marker the resolver refuses, and a map that is not an object reads as no picks. One bad pick never
 * stops the settings file from loading.
 */
export const McpThreadProfiles = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.Record(Schema.String, Schema.String),
    SchemaTransformation.transform({
      decode: (raw): { readonly [threadId: string]: string } =>
        Object.fromEntries(
          Object.entries(isRecord(raw) ? raw : {})
            .filter(([, pick]) => pick !== null && pick !== undefined)
            .map(([threadId, pick]) => [threadId, readMcpProfileRef(pick)]),
        ),
      encode: (picks) => picks,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed({})),
);
