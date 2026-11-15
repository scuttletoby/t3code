/**
 * Fork: every setting this fork adds, so a new one touches no upstream file. Add its field, patch
 * field, patch rule and a forkDefaults.ts row here. Never import settings.ts; `automaticUpdates`
 * alone stays inline there ({@link FORK_INLINE_SERVER_KEYS}). See docs/fork/updates.md.
 */
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import { TrimmedString } from "./baseSchemas.ts";
import {
  MCP_PROFILE_ALL,
  MCP_SUBAGENT_INHERIT,
  MCP_THREAD_PROFILES_KEEP_NEWEST,
  McpProfileName,
  McpProfilePatch,
  McpProfileRef,
  McpProfilesFromSettingsFile,
  McpProfilesPatch,
  McpProjectProfileRefPatch,
  McpThreadProfiles,
  McpThreadProfilesPatch,
} from "./mcpProfile.ts";
import { ProviderInstanceId } from "./providerInstance.ts";
import {
  DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
  UsagePacingGateMode,
  UsagePacingInstanceSettings,
  UsagePacingInstanceSettingsPatch,
  UsagePacingLowerEffort,
} from "./usagePacing.ts";
import {
  AgentSlotsSettings,
  AgentSlotsSettingsPatch,
  WaitChecks,
  WaitChecksFromSettingsFile,
  WaitForSettings,
  WaitForSettingsPatch,
} from "./waitFor.ts";

export * from "./mcpProfile.ts";
export * from "./usagePacing.ts";

/** How often usage limits refresh while no client watches; 0 turns it off. */
const DEFAULT_HEADLESS_USAGE_REFRESH_INTERVAL = Duration.minutes(10);

/** The server settings file's fork fields; each carries its default. */
export const ForkServerSettingsFields = {
  /** Optional executable asked before an automatic update; a non-zero exit means "not now". */
  automaticUpdatesQuietCommand: TrimmedString.pipe(Schema.withDecodingDefault(Effect.succeed(""))),
  /** Usage pacing per provider instance; an instance without an entry has no reserve. */
  usagePacing: Schema.Record(ProviderInstanceId, UsagePacingInstanceSettings).pipe(
    Schema.withDecodingDefault(Effect.succeed({})),
  ),
  /** The usage pacing gate on agent-delegated tasks; `off` keeps upstream's behaviour. */
  usagePacingGate: UsagePacingGateMode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  /** Effort levels the gate may lower to; empty never lowers one. */
  usagePacingLowerEffort: UsagePacingLowerEffort.pipe(
    Schema.withDecodingDefault(Effect.succeed({})),
  ),
  /** Waits (`wait_for`); off by default. */
  waitFor: WaitForSettings.pipe(
    Schema.withDecodingDefault(Effect.succeed(Schema.decodeSync(WaitForSettings)({}))),
  ),
  /** Named check commands for waits; only settings can add one. Bad entries are dropped on read. */
  waitChecks: WaitChecksFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /** Agent slot limits on `delegate_task`; off by default. */
  agentSlots: AgentSlotsSettings.pipe(
    Schema.withDecodingDefault(Effect.succeed(Schema.decodeSync(AgentSlotsSettings)({}))),
  ),
  /** Never more often than `providerHealthRefreshInterval`; 0 turns it off. */
  headlessUsageRefreshInterval: Schema.DurationFromMillis.pipe(
    Schema.withDecodingDefault(
      Effect.succeed(Duration.toMillis(DEFAULT_HEADLESS_USAGE_REFRESH_INTERVAL)),
    ),
  ),
  /** Named MCP profiles besides the built-in `all` and `t3-only`; each checked when used. */
  mcpProfiles: McpProfilesFromSettingsFile,
  /** The MCP profile a thread gets; `all` (or empty) is today's behaviour. Read leniently. */
  mcpProfile: McpProfileRef.pipe(Schema.withDecodingDefault(Effect.succeed(MCP_PROFILE_ALL))),
  /** The MCP profile of a delegated child thread; `inherit` (or empty) follows `mcpProfile`. */
  mcpSubagentProfile: McpProfileRef.pipe(
    Schema.withDecodingDefault(Effect.succeed(MCP_SUBAGENT_INHERIT)),
  ),
  /** The owner's MCP profile pick per thread; it wins over the defaults above. */
  mcpThreadProfiles: McpThreadProfiles,
};

/** A key of {@link ForkServerSettingsFields}. */
export type ForkServerSettingKey = keyof typeof ForkServerSettingsFields;

/** The settings patch's fork fields: one per server field, how a client may change it. */
export const ForkServerSettingsPatchFields = {
  automaticUpdatesQuietCommand: Schema.optionalKey(TrimmedString),
  usagePacing: Schema.optionalKey(
    Schema.Record(ProviderInstanceId, Schema.NullOr(UsagePacingInstanceSettingsPatch)),
  ),
  usagePacingGate: Schema.optionalKey(UsagePacingGateMode),
  usagePacingLowerEffort: Schema.optionalKey(UsagePacingLowerEffort),
  waitFor: Schema.optionalKey(WaitForSettingsPatch),
  /** Strict: a save refuses a bad check (reading the file drops it with a warning). */
  waitChecks: Schema.optionalKey(WaitChecks),
  agentSlots: Schema.optionalKey(AgentSlotsSettingsPatch),
  headlessUsageRefreshInterval: Schema.optionalKey(Schema.DurationFromMillis),
  /** Strict: a save refuses a bad profile or a built-in name (reading the file keeps it as is). */
  mcpProfiles: Schema.optionalKey(McpProfilesPatch),
  /** A profile name, not `inherit`; the server also refuses a name no profile has. */
  mcpProfile: Schema.optionalKey(McpProfilePatch),
  /** `inherit` or a profile name; the server also refuses a name no profile has. */
  mcpSubagentProfile: Schema.optionalKey(McpProfileName),
  mcpThreadProfiles: Schema.optionalKey(McpThreadProfilesPatch),
} satisfies Record<ForkServerSettingKey, unknown>;

/**
 * How a patch field changes the stored value: `replace` whole; `merge-fields` only the named fields.
 * Per entry (`null` removes it): `mergeEntries` merges fields, a new entry starting from its
 * defaults; `replaceEntries` replaces the entry whole, makes it newest and keeps `keepNewest`.
 */
export type ForkPatchRule =
  | "replace"
  | "merge-fields"
  | { readonly mergeEntries: Readonly<Record<string, unknown>> }
  | { readonly replaceEntries: { readonly keepNewest?: number } };

/** One rule per fork patch field. */
export const FORK_PATCH_RULES: { readonly [K in ForkServerSettingKey]: ForkPatchRule } = {
  automaticUpdatesQuietCommand: "replace",
  usagePacing: { mergeEntries: DEFAULT_USAGE_PACING_INSTANCE_SETTINGS },
  usagePacingGate: "replace",
  // A level left out of the patch is removed.
  usagePacingLowerEffort: "replace",
  waitFor: "merge-fields",
  // A check left out of the patch is removed.
  waitChecks: "replace",
  // `maxPerAccount` is one field, so a patch that names it replaces the whole map.
  agentSlots: "merge-fields",
  headlessUsageRefreshInterval: "replace",
  // One profile per entry, so two people editing different profiles keep both.
  mcpProfiles: { replaceEntries: {} },
  mcpProfile: "replace",
  mcpSubagentProfile: "replace",
  // One pick per thread, so two picks at once keep both; old picks go beyond the cap.
  mcpThreadProfiles: { replaceEntries: { keepNewest: MCP_THREAD_PROFILES_KEEP_NEWEST } },
};

/** The client settings' fork fields (stored in the browser); none yet. */
export const ForkClientSettingsFields = {};

/** Fork settings a project may override; listed in `PROJECT_SCOPED_SERVER_SETTING_KEYS`. */
export const FORK_PROJECT_SCOPED_KEYS = [
  // A project lists every check it uses; its map replaces the server's.
  "waitChecks",
  "mcpProfile",
  "mcpSubagentProfile",
] as const satisfies ReadonlyArray<ForkServerSettingKey>;

/** A project's overrides of fork settings, as the settings file holds them. */
export const ForkProjectOverrideFields = {
  /** Bad entries are dropped on read; the settings patch refuses them. */
  waitChecks: Schema.optionalKey(WaitChecksFromSettingsFile),
  /** Absent or empty: the server's `mcpProfile`. Read leniently, as the server's. */
  mcpProfile: Schema.optionalKey(McpProfileRef),
  /** Absent or empty: the server's `mcpSubagentProfile`. Read leniently, as the server's. */
  mcpSubagentProfile: Schema.optionalKey(McpProfileRef),
} satisfies Record<(typeof FORK_PROJECT_SCOPED_KEYS)[number], unknown>;

/** A project's overrides of fork settings in a patch (each project's set is replaced whole). */
export const ForkProjectOverridePatchFields = {
  /** A save refuses a bad check. */
  waitChecks: Schema.optionalKey(WaitChecks),
  /**
   * Any text, as reading keeps it: clients resend a project's whole entry when they save its other
   * settings. The server refuses only a changed value that names no profile (forkSettingsPatch.ts).
   */
  mcpProfile: Schema.optionalKey(McpProjectProfileRefPatch),
  mcpSubagentProfile: Schema.optionalKey(McpProjectProfileRefPatch),
} satisfies Record<keyof typeof ForkProjectOverrideFields, unknown>;

/**
 * Fork settings an agent's tool may write. Empty: every fork setting is the person's to change.
 * An entry needs its reason in the pull request that adds it.
 */
export const FORK_AGENT_WRITABLE_KEYS: ReadonlyArray<ForkServerSettingKey> = [];

/**
 * Fork settings written inline in settings.ts, not here: `automaticUpdates` has the name and place
 * of upstream's pending setting, whose line replaces the fork's when it lands.
 */
export const FORK_INLINE_SERVER_KEYS = ["automaticUpdates"] as const;

/** Every server fork setting's key: the inline ones and those of {@link ForkServerSettingsFields}. */
export const FORK_SERVER_SETTING_KEYS: ReadonlyArray<string> = [
  ...FORK_INLINE_SERVER_KEYS,
  ...Object.keys(ForkServerSettingsFields),
];

/**
 * Every fork setting only a person may change: each server, project and client fork key but those
 * in {@link FORK_AGENT_WRITABLE_KEYS}. No T3 tool's input may name one (the server's
 * personOnlySettings test). Derived, so a new fork setting is covered without a row.
 */
export const FORK_PERSON_ONLY_KEYS: ReadonlyArray<string> = [
  ...new Set([
    ...FORK_SERVER_SETTING_KEYS,
    ...Object.keys(ForkProjectOverrideFields),
    ...Object.keys(ForkClientSettingsFields),
  ]),
].filter((key) => !(FORK_AGENT_WRITABLE_KEYS as ReadonlyArray<string>).includes(key));
