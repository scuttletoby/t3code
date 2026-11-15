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
/** The two profiles every build knows; `mcpProfiles` cannot redefine them. */
const MCP_BUILT_IN_PROFILE_NAMES = [MCP_PROFILE_ALL, MCP_PROFILE_T3_ONLY] as const;
/** `mcpSubagentProfile` value: a delegated child follows `mcpProfile` like any other thread. */
export const MCP_SUBAGENT_INHERIT = "inherit";

const MCP_PROFILE_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/u;

/** A profile's name: lower-case letters, digits and dashes, at most 40. */
export const McpProfileName = Schema.String.check(Schema.isPattern(MCP_PROFILE_NAME_PATTERN));
export type McpProfileName = typeof McpProfileName.Type;

/** True for a name a profile in `mcpProfiles` may have: valid, not built in, not `inherit`. */
export const isDefinableMcpProfileName = (name: string): boolean =>
  MCP_PROFILE_NAME_PATTERN.test(name) &&
  name !== MCP_SUBAGENT_INHERIT &&
  !(MCP_BUILT_IN_PROFILE_NAMES as ReadonlyArray<string>).includes(name);

/**
 * Groups of T3's own tools, one per toolkit. The two message tools (`request_secret`,
 * `send_to_user`) are in no group: every profile keeps them.
 */
export const MCP_T3_TOOL_GROUPS = [
  "orchestration",
  "preview",
  "pull-requests",
  "threads",
  "projects",
  "device",
  "worktree",
  "attachments",
  "environment",
  "theme",
  "html",
  "waits",
] as const;
export const McpT3ToolGroup = Schema.Literals(MCP_T3_TOOL_GROUPS);
export type McpT3ToolGroup = typeof McpT3ToolGroup.Type;

/** An outside MCP server's name as the provider's config spells it. */
export const McpServerName = TrimmedString.check(Schema.isNonEmpty());

/**
 * One profile. `servers` and `t3Tools` are each `"all"` or a keep-list; a keep-list leaves out
 * anything not named, so a server added later is left out until the owner adds it. An empty list
 * keeps none.
 */
export const McpProfile = Schema.Struct({
  servers: Schema.Union([Schema.Literal("all"), Schema.Array(McpServerName)]),
  t3Tools: Schema.Union([Schema.Literal("all"), Schema.Array(McpT3ToolGroup)]),
});
export type McpProfile = typeof McpProfile.Type;

/**
 * The settings file's profiles. Each entry is kept as written and checked only when a thread uses
 * it, so one bad entry (or one written by a newer build, with fields this build does not know)
 * never stops the settings file from loading, and a save keeps it as it was.
 */
export const McpProfilesFromSettingsFile = Schema.Record(Schema.String, Schema.Unknown).pipe(
  Schema.withDecodingDefault(Effect.succeed({})),
);

const MCP_PROFILE_FIELDS: ReadonlyArray<string> = ["servers", "t3Tools"];

/**
 * A profile in a settings patch: a field this build does not know is refused, not dropped, so a
 * save never stores a profile cut down from what the person wrote. (Effect strips excess fields
 * unless the decode call itself asks for errors, so the check runs on the input first.)
 */
const McpProfileInPatch = Schema.Record(Schema.String, Schema.Unknown)
  .check(
    Schema.makeFilter(
      (fields: { readonly [name: string]: unknown }) =>
        Object.keys(fields).every((name) => MCP_PROFILE_FIELDS.includes(name)),
      { expected: `a profile with only the fields ${MCP_PROFILE_FIELDS.join(", ")}` },
    ),
  )
  .pipe(Schema.decodeTo(McpProfile, SchemaTransformation.passthrough({ strict: false })));

/**
 * Profiles in a settings patch: each entry replaces the stored one whole; `null` removes it. Names
 * are checked on the whole map: Effect drops, rather than refuses, a record key its key schema
 * rejects.
 */
export const McpProfilesPatch = Schema.Record(
  Schema.String,
  Schema.NullOr(McpProfileInPatch),
).check(
  Schema.makeFilter(
    (profiles: { readonly [name: string]: unknown }) =>
      Object.keys(profiles).every(isDefinableMcpProfileName),
    {
      expected: `profile names of lower-case letters, digits and dashes (at most 40), other than ${MCP_BUILT_IN_PROFILE_NAMES.join(", ")} and ${MCP_SUBAGENT_INHERIT}`,
    },
  ),
);

/** A profile reference as the settings file holds it; empty means "not set". */
export const McpProfileRef = TrimmedString;

/**
 * The owner's pick per thread in a settings patch; `null` removes the pick. Thread ids are checked
 * on the whole map, as for {@link McpProfilesPatch}.
 */
export const McpThreadProfilesPatch = Schema.Record(
  Schema.String,
  Schema.NullOr(McpProfileName),
).check(
  Schema.makeFilter(
    (picks: { readonly [threadId: string]: unknown }) =>
      Object.keys(picks).every((threadId) => threadId.length > 0 && threadId.trim() === threadId),
    { expected: "thread ids (non-blank, no outer spaces) as keys" },
  ),
);

/** The newest thread picks kept; a write that goes over drops the oldest. */
export const MCP_THREAD_PROFILES_KEEP_NEWEST = 500;

/** The settings file's thread picks: thread id to profile name. */
export const McpThreadProfiles = Schema.Record(Schema.String, Schema.String).pipe(
  Schema.withDecodingDefault(Effect.succeed({})),
);
