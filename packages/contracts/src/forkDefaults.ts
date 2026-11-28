// Fork: every setting this fork adds, with its default, the reason and where it was decided. A
// machine whose file never names one must behave as upstream's T3 Code: nothing changes unasked.
// forkDefaults.test.ts fails while a fork setting in forkSettings.ts (or the inline
// `automaticUpdates`) has no row here, or a row's default is not the code's. See docs/fork/updates.md.

import * as Schema from "effect/Schema";

import { ClientSettingsSchema, ProjectSettingsOverrides, ServerSettings } from "./settings.ts";

/** Which settings object holds the key: the server's file, one project's overrides, or a client. */
export type ForkSettingScope = "server" | "projectOverride" | "client";

export interface ForkSettingDefault {
  readonly scope: ForkSettingScope;
  readonly key: string;
  /**
   * The default as the settings file writes it (a duration in milliseconds); `undefined` when the
   * key is absent by default. With `fields`, only the fields named here are pinned.
   */
  readonly default: unknown;
  /**
   * For a settings group with an `enabled` switch: pin only the fields in `default` (the switch),
   * since the group's other fields are tuning that does nothing while it is off.
   */
  readonly fields?: true;
  /** True when the default leaves the machine as upstream's would be; false says why not in `reason`. */
  readonly off: boolean;
  readonly reason: string;
  /** The fork pull request or issue that decided it. */
  readonly decidedIn: string;
}

export const FORK_SETTING_DEFAULTS: ReadonlyArray<ForkSettingDefault> = [
  {
    scope: "server",
    key: "automaticUpdates",
    default: false,
    off: true,
    reason:
      "A machine never updates itself unless its owner turned this on. Upstream's setting of the same name defaults on; the fork keeps false through every sync.",
    decidedIn: "#41",
  },
  {
    scope: "server",
    key: "automaticUpdatesQuietCommand",
    default: "",
    off: true,
    reason: "No command is run before an automatic update.",
    decidedIn: "#5",
  },
  {
    scope: "server",
    key: "activityProbe",
    default: false,
    off: true,
    reason: "No activity route: a quiet check falls back to what it can see from outside.",
    decidedIn: "#509",
  },
  {
    scope: "server",
    key: "usagePacing",
    default: {},
    off: true,
    reason: "No provider instance keeps a reserve, so pacing holds nothing back.",
    decidedIn: "#52",
  },
  {
    scope: "server",
    key: "usagePacingGate",
    default: "off",
    off: true,
    reason: "Delegated tasks start as upstream's do; no pace is read.",
    decidedIn: "#73",
  },
  {
    scope: "server",
    key: "usagePacingLowerEffort",
    default: {},
    off: true,
    reason: "The pacing gate never lowers a task's effort.",
    decidedIn: "#73",
  },
  {
    scope: "server",
    key: "waitFor",
    default: { enabled: false },
    fields: true,
    off: true,
    reason: "Waits judge and deliver nothing until switched on.",
    decidedIn: "#84",
  },
  {
    scope: "server",
    key: "waitChecks",
    default: {},
    off: true,
    reason: "No named check commands; only the settings file can add one.",
    decidedIn: "#84",
  },
  {
    scope: "server",
    key: "playtest",
    default: { enabled: false },
    fields: true,
    off: true,
    reason:
      "No playtest sessions and no adapter steps until a person sets them and switches it on.",
    decidedIn: "#260",
  },
  {
    scope: "server",
    key: "handoff",
    default: { mode: "off" },
    fields: true,
    off: true,
    reason:
      "No thread hands off to a fresh session; upstream's compaction settings apply as they are.",
    decidedIn: "#215",
  },
  {
    scope: "server",
    key: "reviewRules",
    default: { preset: "off" },
    off: true,
    reason:
      "No review lens is listed, changed or checked until a person sets the preset; the repo's CI gate stays the guarantee.",
    decidedIn: "#291",
  },
  {
    scope: "server",
    key: "modelPolicy",
    default: { mode: "off", allow: [] },
    off: true,
    reason: "Agents pick any model, as upstream's do, until a person sets the policy's mode.",
    decidedIn: "#268",
  },
  {
    scope: "server",
    key: "placement",
    default: { mode: "off" },
    fields: true,
    off: true,
    reason:
      "No delegated task is placed on another machine by rule; an agent picks a machine as upstream's do. A bad value reads as off.",
    decidedIn: "#134",
  },
  {
    scope: "server",
    key: "placementNeeds",
    default: [],
    off: true,
    reason: "No project's work needs a label, and nothing reads it while placement is off.",
    decidedIn: "#134",
  },
  {
    scope: "server",
    key: "agentSlots",
    default: { enabled: false },
    fields: true,
    off: false,
    reason:
      'delegate_task is never refused for slots until switched on. Without `mode` while off, the mode is `observe`: at each slot decision the capacity rule reads live memory (read only, off the decision path) and records what it would have answered, in the log and in wait_list. It never changes or delays an answer. `mode: "fixed"` reads nothing.',
    decidedIn: "#84",
  },
  {
    scope: "server",
    key: "headlessUsageRefreshInterval",
    default: 600_000,
    off: false,
    reason:
      "On by default: usage limits are read every 10 minutes while no client watches, so pacing has current numbers. Read-only: it changes no thread, setting or install. 0 turns it off.",
    decidedIn: "#42",
  },
  {
    scope: "server",
    key: "mcpProfiles",
    default: {},
    off: true,
    reason: "No named MCP profiles; only the built-in `all` and `t3-only` exist.",
    decidedIn: "#145",
  },
  {
    scope: "server",
    key: "mcpProfile",
    default: "all",
    off: true,
    reason: "Every thread gets every outside MCP server and every T3 tool, as upstream's does.",
    decidedIn: "#145",
  },
  {
    scope: "server",
    key: "mcpSubagentProfile",
    default: "inherit",
    off: true,
    reason: "A delegated child follows `mcpProfile` like any other thread.",
    decidedIn: "#145",
  },
  {
    scope: "server",
    key: "mcpThreadProfiles",
    default: {},
    off: true,
    reason: "No thread has its own MCP profile pick.",
    decidedIn: "#145",
  },
  {
    scope: "server",
    key: "mcpCarriedProfiles",
    default: "apply",
    off: true,
    reason:
      "A task a linked machine delegates here is cut down to the profile it carries, which with no profiles set anywhere is `all`: nothing changes.",
    decidedIn: "#640",
  },
  {
    scope: "server",
    key: "threadRoles",
    default: { assistant: null, orchestrators: {} },
    off: true,
    reason: "No thread holds a role, so every thread is shown as upstream shows it.",
    decidedIn: "#138",
  },
  {
    scope: "server",
    key: "assistant",
    default: { enabled: false },
    off: true,
    reason: 'Settings offers no "Make assistant"; no thread is made.',
    decidedIn: "#879",
  },
  {
    scope: "server",
    key: "stopFinishedChildren",
    default: { enabled: false },
    fields: true,
    off: true,
    reason:
      "A finished delegated child keeps its provider process until upstream's own idle release.",
    decidedIn: "#925",
  },
  {
    scope: "projectOverride",
    key: "mcpProfile",
    default: undefined,
    off: true,
    reason: "A project without its own MCP profile uses the server's.",
    decidedIn: "#145",
  },
  {
    scope: "projectOverride",
    key: "mcpSubagentProfile",
    default: undefined,
    off: true,
    reason: "A project without its own sub-agent rule uses the server's.",
    decidedIn: "#145",
  },
  {
    scope: "projectOverride",
    key: "waitChecks",
    default: undefined,
    off: true,
    reason: "A project without its own list uses the server's checks.",
    decidedIn: "#84",
  },
  {
    scope: "projectOverride",
    key: "playtest",
    default: undefined,
    off: true,
    reason: "A project without its own adapter uses the server's playtest settings.",
    decidedIn: "#260",
  },
  {
    scope: "projectOverride",
    key: "handoff",
    default: undefined,
    off: true,
    reason: "A project without its own handoff settings uses the server's.",
    decidedIn: "#215",
  },
  {
    scope: "projectOverride",
    key: "reviewRules",
    default: undefined,
    off: true,
    reason: "A project without its own review rules uses the server's.",
    decidedIn: "#291",
  },
  {
    scope: "projectOverride",
    key: "placementNeeds",
    default: undefined,
    off: true,
    reason: "A project without its own needs uses the server's.",
    decidedIn: "#134",
  },
  {
    scope: "client",
    key: "calmSidebarEnabled",
    default: false,
    off: true,
    reason: "Each device shows upstream's thread list until its owner turns the calm view on.",
    decidedIn: "#139",
  },
  {
    scope: "client",
    key: "ownerQuestionsPage",
    default: true,
    off: false,
    reason:
      "On by default (the owner's decision): Issues opens Waiting on you, the open owner questions of every project, instead of upstream's Issues page. It changes which page the Issues entry opens, nothing else; turn it off in Settings > Fork for upstream's page.",
    decidedIn: "#828",
  },
  {
    scope: "client",
    key: "workPanelEnabled",
    default: false,
    off: true,
    reason: "Each device shows upstream's side panel until its owner turns the work panel on.",
    decidedIn: "#1136",
  },
];

const FIELDS: Readonly<Record<ForkSettingScope, Readonly<Record<string, Schema.Top>>>> = {
  server: ServerSettings.fields,
  projectOverride: ProjectSettingsOverrides.fields,
  client: ClientSettingsSchema.fields,
};

/** A decoded value of the row's setting as the settings file writes it. */
export function forkSettingAsSaved(row: ForkSettingDefault, value: unknown): unknown {
  const field = FIELDS[row.scope][row.key];
  if (field === undefined || value === undefined) return undefined;
  return Schema.encodeUnknownSync(field as Schema.Codec<unknown, unknown>)(value);
}

/** The part of a saved value the row pins: the whole value, or only the fields `default` names. */
export function pinnedPart(row: ForkSettingDefault, saved: unknown): unknown {
  if (row.fields !== true || typeof saved !== "object" || saved === null) return saved;
  const record = saved as Readonly<Record<string, unknown>>;
  return Object.fromEntries(
    Object.keys(row.default as object).map((field) => [field, record[field]]),
  );
}
