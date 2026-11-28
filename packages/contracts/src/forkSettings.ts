/**
 * Fork: every setting this fork adds, so a new one touches no upstream file. Add its field, patch
 * field, patch rule and a forkDefaults.ts row here. Never import settings.ts; `automaticUpdates`
 * alone stays inline there ({@link FORK_INLINE_SERVER_KEYS}). See docs/fork/updates.md.
 */
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedString } from "./baseSchemas.ts";
import { HandoffSettingsFromSettingsFile, HandoffSettingsPatch } from "./handoff.ts";
import {
  MCP_CARRIED_PROFILES_APPLY,
  MCP_PROFILE_ALL,
  MCP_SUBAGENT_INHERIT,
  MCP_THREAD_PROFILES_MAX,
  McpCarriedProfiles,
  McpCarriedProfilesFromSettingsFile,
  McpProfileName,
  McpProfilePatch,
  McpProfileRef,
  McpProfilesFromSettingsFile,
  McpProfilesPatch,
  McpProjectProfileRefPatch,
  McpThreadProfiles,
  McpThreadProfilesPatch,
} from "./mcpProfile.ts";
import { ModelPolicyFromSettingsFile, ModelPolicyPatch } from "./modelPolicy.ts";
import {
  PlacementNeedsFromSettingsFile,
  PlacementNeedsPatch,
  PlacementNeedsProjectPatch,
  PlacementSettingsFromSettingsFile,
  PlacementSettingsPatch,
} from "./placement.ts";
import { PlaytestSettingsFromSettingsFile, PlaytestSettingsPatch } from "./playtest.ts";
import { ProviderInstanceId } from "./providerInstance.ts";
import {
  ReviewRulesFromSettingsFile,
  ReviewRulesPatch,
  ReviewRulesProjectPatch,
} from "./reviewRules.ts";
import { ThreadRolesFromSettingsFile, ThreadRolesPatch } from "./threadRoles.ts";
import { AssistantSettingsFromSettingsFile, AssistantSettingsPatch } from "./assistant.ts";
import {
  StopFinishedChildrenSettingsFromSettingsFile,
  StopFinishedChildrenSettingsPatch,
} from "./stopFinishedChildren.ts";
import { LanesSettingsFromSettingsFile, LanesSettingsPatch } from "./forkLanes.ts";
import {
  DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
  UsagePacingFromSettingsFile,
  UsagePacingGateMode,
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

export * from "./assistant.ts";
export * from "./forkEnvironmentFileLinks.ts";
export * from "./forkFeatures.ts";
export * from "./forkOwnerQuestions.ts";
export * from "./forkWorkPanel.ts";
export * from "./handoff.ts";
export * from "./mcpProfile.ts";
export * from "./mcpProfileResolve.ts";
export * from "./modelPolicy.ts";
export * from "./placement.ts";
export * from "./playtest.ts";
export * from "./reviewRules.ts";
export * from "./stopFinishedChildren.ts";
export * from "./forkLanes.ts";
export * from "./threadRoles.ts";
export * from "./usagePacing.ts";

/** How often usage limits refresh while no client watches; 0 turns it off. */
const DEFAULT_HEADLESS_USAGE_REFRESH_INTERVAL = Duration.minutes(10);

/** The server settings file's fork fields; each carries its default. */
export const ForkServerSettingsFields = {
  /** Optional executable asked before an automatic update; a non-zero exit means "not now". */
  automaticUpdatesQuietCommand: TrimmedString.pipe(Schema.withDecodingDefault(Effect.succeed(""))),
  /** Answers "is T3 busy?" read-only on loopback (GET /api/fork/activity); off: no such route. */
  activityProbe: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /** Usage pacing per provider instance; an instance without an entry has no reserve. */
  usagePacing: UsagePacingFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
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
  /** What a task a linked machine delegates here gets of the profile it carries; `apply` cuts it. */
  mcpCarriedProfiles: McpCarriedProfilesFromSettingsFile.pipe(
    Schema.withDecodingDefault(Effect.succeed(MCP_CARRIED_PROFILES_APPLY)),
  ),
  /** Playtest sessions and their adapter's steps; off by default. A bad value reads as off. */
  playtest: PlaytestSettingsFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /** Handoff instead of compaction for long threads; off by default. A bad value reads as off. */
  handoff: HandoffSettingsFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /** How this machine runs a review lens; off by default. A bad value reads as off, kept as written. */
  reviewRules: ReviewRulesFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /** Which models agents may pick; off by default. A bad value reads as enforce, nothing allowed. */
  modelPolicy: ModelPolicyFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /** What this machine has and how it places work on linked machines; off. A bad value reads as off, kept as written. */
  placement: PlacementSettingsFromSettingsFile.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  /**
   * Labels a machine must have to take a project's work, for projects without their own; none. A bad
   * value fits no machine, kept as written.
   */
  placementNeeds: PlacementNeedsFromSettingsFile.pipe(
    Schema.withDecodingDefault(Effect.succeed([])),
  ),
  /** Which thread is the assistant and each project's orchestrator; none. A bad entry drops alone. */
  threadRoles: ThreadRolesFromSettingsFile,
  /** Offers "Make assistant"; off. A bad value reads as off. */
  assistant: AssistantSettingsFromSettingsFile,
  /** Stops a finished delegated child's provider session after a grace time; off. A bad value reads as off. */
  stopFinishedChildren: StopFinishedChildrenSettingsFromSettingsFile,
  /** Lanes under a project's orchestrator (issue 1156); off. A bad value reads as off, a bad lane drops alone. */
  lanes: LanesSettingsFromSettingsFile,
};

/** A key of {@link ForkServerSettingsFields}. */
export type ForkServerSettingKey = keyof typeof ForkServerSettingsFields;

/** The settings patch's fork fields: one per server field, how a client may change it. */
export const ForkServerSettingsPatchFields = {
  automaticUpdatesQuietCommand: Schema.optionalKey(TrimmedString),
  activityProbe: Schema.optionalKey(Schema.Boolean),
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
  /** Strict: `apply` or `refuse`. */
  mcpCarriedProfiles: Schema.optionalKey(McpCarriedProfiles),
  /** Strict: a save refuses a bad step, an unknown field, or `enabled` without all four steps. */
  playtest: Schema.optionalKey(PlaytestSettingsPatch),
  /** Strict: a save refuses a bad value, an unknown field, or `source: command` without a command. */
  handoff: Schema.optionalKey(HandoffSettingsPatch),
  /** Strict: a save refuses a bad field, an unknown key, or a value without `preset`. */
  reviewRules: Schema.optionalKey(ReviewRulesPatch),
  /**
   * Strict: a save refuses a bad row, a `defaultEffort` not in its row's `efforts`, an unknown key,
   * or a value without both `mode` and `allow` (it replaces the stored value whole).
   */
  modelPolicy: Schema.optionalKey(ModelPolicyPatch),
  /** Strict: a save refuses a bad field or an unknown key; a field left out keeps its value. */
  placement: Schema.optionalKey(PlacementSettingsPatch),
  /** Strict: a save refuses a bad label; it replaces a kept bad value. */
  placementNeeds: Schema.optionalKey(PlacementNeedsPatch),
  /** Strict ids; the server also refuses a pointer to a thread that cannot hold the role. */
  threadRoles: Schema.optionalKey(ThreadRolesPatch),
  /** Strict: a save refuses a bad value or an unknown field. */
  assistant: Schema.optionalKey(AssistantSettingsPatch),
  /** Strict: a save refuses a bad value or an unknown field. */
  stopFinishedChildren: Schema.optionalKey(StopFinishedChildrenSettingsPatch),
  /** Strict: a save refuses a bad lane name or definition; `null` removes one lane. */
  lanes: Schema.optionalKey(LanesSettingsPatch),
} satisfies Record<ForkServerSettingKey, unknown>;

/**
 * `replace` whole; `merge-fields` the named fields; `fields` each named field by its own rule. Per
 * entry (`null` removes): `mergeEntries` merges, starting from defaults; `replaceEntries` replaces
 * whole, newest last. A save leaving more than `maxEntries` (and more than before) is refused.
 */
export type ForkPatchRule =
  | "replace"
  | "merge-fields"
  | { readonly mergeEntries: Readonly<Record<string, unknown>> }
  | { readonly replaceEntries: { readonly maxEntries?: number } }
  | { readonly fields: Readonly<Record<string, ForkPatchRule>> };

/** One rule per fork patch field. */
export const FORK_PATCH_RULES: { readonly [K in ForkServerSettingKey]: ForkPatchRule } = {
  automaticUpdatesQuietCommand: "replace",
  activityProbe: "replace",
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
  // One pick per thread, so two picks at once keep both; a save past the cap is refused.
  mcpThreadProfiles: { replaceEntries: { maxEntries: MCP_THREAD_PROFILES_MAX } },
  mcpCarriedProfiles: "replace",
  // Whole, so a step left out is removed and the strict check sees the value that is saved.
  playtest: "replace",
  // Whole, so the strict check (thread cap included) sees the value that is saved.
  handoff: "replace",
  // Whole, so the strict check sees the value that is saved; two saves at once, the last one wins.
  reviewRules: "replace",
  // Whole, as playtest, so the strict check sees the value that is saved; two saves at once, the last
  // one wins. The patch has no defaults, so a save that leaves out `mode` or `allow` is refused.
  modelPolicy: "replace",
  // A field left out keeps its value. Over a kept unreadable value only a save of every field is
  // taken (it replaces the text); the save check refuses a partial one (forkSettingsPatch.ts).
  placement: "merge-fields",
  placementNeeds: "replace",
  // One orchestrator per project entry, so two people setting different projects keep both; two
  // saves of the same project, the last one wins.
  threadRoles: { fields: { assistant: "replace", orchestrators: { replaceEntries: {} } } },
  assistant: "merge-fields",
  stopFinishedChildren: "merge-fields",
  // One lane per entry, so two saves for different lanes keep both.
  lanes: { fields: { enabled: "replace", definitions: { replaceEntries: {} } } },
};

/**
 * The client settings' fork fields (stored on each device, not on the server). Their patch fields
 * are {@link ForkClientSettingsPatchFields}, spread into upstream's `ClientSettingsPatch`.
 */
export const ForkClientSettingsFields = {
  /** Calm sidebar: role threads on top, every other thread folded into one group; off. */
  calmSidebarEnabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /**
   * Issues opens the fork's "Waiting on you" page (owner questions of every project); off gives
   * upstream's Issues page back. On (owner decision, fork issue 828).
   */
  ownerQuestionsPage: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(true))),
  /** Work panel: CI and capacity in a thread's side panel, next to its sub-agents; off. */
  workPanelEnabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
};

/** A client settings save's fork fields: left out keeps the stored value. */
export const ForkClientSettingsPatchFields = {
  calmSidebarEnabled: Schema.optionalKey(Schema.Boolean),
  ownerQuestionsPage: Schema.optionalKey(Schema.Boolean),
  workPanelEnabled: Schema.optionalKey(Schema.Boolean),
};

/** Fork settings a project may override; listed in `PROJECT_SCOPED_SERVER_SETTING_KEYS`. */
export const FORK_PROJECT_SCOPED_KEYS = [
  // A project lists every check it uses; its map replaces the server's.
  "waitChecks",
  "mcpProfile",
  "mcpSubagentProfile",
  // A project's adapter replaces the server's whole, `enabled` included.
  "playtest",
  // A project's value replaces the server's whole.
  "handoff",
  // A project's value replaces the server's whole, `preset` included.
  "reviewRules",
  // A project's needs replace the server's whole.
  "placementNeeds",
] as const satisfies ReadonlyArray<ForkServerSettingKey>;

/** Drops a `null`: it is a save's "clear", never a project's own value. */
const dropNull = <A>(raw: Option.Option<A>): Option.Option<A> =>
  Option.filter(raw, (entry): boolean => entry !== null);

/**
 * A clear-by-`null` project key in the file: a `null` reads as left out (the server's) and is never
 * written. `Schema.Null` first, as the lenient file schemas take any value. The patch has its own
 * schema (`ForkProjectOverridePatchFields`), so a client's clear still keeps its `null`.
 */
const nullReadsAsUnset = <S extends Schema.Top>(value: S) =>
  Schema.optionalKey(Schema.Union([Schema.Null, value])).pipe(
    Schema.decodeTo(
      Schema.optionalKey(Schema.Union([Schema.Null, Schema.toType(value)])),
      SchemaTransformation.transformOptional({ decode: dropNull, encode: dropNull }),
    ),
  );

/** A project's overrides of fork settings, as the settings file holds them. */
export const ForkProjectOverrideFields = {
  /** Bad entries are dropped on read; the settings patch refuses them. */
  waitChecks: Schema.optionalKey(WaitChecksFromSettingsFile),
  /** Absent or empty: the server's `mcpProfile`. Read leniently, as the server's. */
  mcpProfile: Schema.optionalKey(McpProfileRef),
  /** Absent or empty: the server's `mcpSubagentProfile`. Read leniently, as the server's. */
  mcpSubagentProfile: Schema.optionalKey(McpProfileRef),
  /**
   * A bad value reads as off for that project, never as the server's adapter. `null` is a save's
   * "clear", which the save removes; a hand-written one means the same: it reads as left out, so
   * the project uses the server's (`nullReadsAsUnset`).
   */
  playtest: nullReadsAsUnset(PlaytestSettingsFromSettingsFile),
  /** As `playtest`: a bad value reads as off for that project, never as the server's. */
  handoff: nullReadsAsUnset(HandoffSettingsFromSettingsFile),
  /** As `playtest`; a bad value is also kept as written (reviewRules.ts). */
  reviewRules: nullReadsAsUnset(ReviewRulesFromSettingsFile),
  /**
   * As `playtest`, but a bad value fits no machine for that project (never no needs, never the
   * server's), kept as written.
   */
  placementNeeds: nullReadsAsUnset(PlacementNeedsFromSettingsFile),
} satisfies Record<(typeof FORK_PROJECT_SCOPED_KEYS)[number], unknown>;

/**
 * Fork project keys a project save keeps when the project's entry leaves them out, each with the
 * value that clears it: upstream replaces a project's entry whole, and a client that does not know a
 * key (an older one) leaves it out, which would quietly switch that project to the server's MCP
 * profile or playtest adapter. (`waitChecks` keeps upstream's rule: left out is removed.)
 */
export const FORK_PROJECT_KEYS_KEPT_WHEN_LEFT_OUT = {
  mcpProfile: "",
  mcpSubagentProfile: "",
  playtest: null,
  handoff: null,
  reviewRules: null,
  placementNeeds: null,
} as const satisfies Partial<Record<keyof typeof ForkProjectOverrideFields, "" | null>>;

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
  /**
   * A save refuses a bad step, an unknown field, or `enabled` without all four steps. Left out keeps
   * the stored value; `null` clears it (the project then uses the server's).
   */
  playtest: Schema.optionalKey(Schema.NullOr(PlaytestSettingsPatch)),
  /** As `playtest`: left out keeps the stored value; `null` clears it. */
  handoff: Schema.optionalKey(Schema.NullOr(HandoffSettingsPatch)),
  /**
   * Strict, but a kept unreadable value passes back as clients resend it. Left out keeps the stored
   * value; `null` clears it (the project then uses the server's).
   */
  reviewRules: Schema.optionalKey(Schema.NullOr(ReviewRulesProjectPatch)),
  /**
   * A save refuses a bad label, but takes the kept bad value back unchanged (the server checks it is
   * the stored one). Left out keeps the stored value; `null` clears it.
   */
  placementNeeds: Schema.optionalKey(Schema.NullOr(PlacementNeedsProjectPatch)),
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
