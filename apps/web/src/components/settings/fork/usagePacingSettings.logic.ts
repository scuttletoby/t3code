/** Fork: the pure parts of the usage pacing panel (`usagePacing`, `usagePacingGate`, `usagePacingLowerEffort`). */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
  type ProviderInstanceId,
  type ProviderOptionDescriptor,
  type UsagePacingGateMode,
  type UsagePacingInstanceSettings,
  type UsagePacingLowerEffort,
} from "@t3tools/contracts";

export interface UsagePacingAccountRow {
  readonly instanceId: ProviderInstanceId;
  readonly label: string;
  readonly settings: UsagePacingInstanceSettings;
  /** Stored in the settings file: a reset or a removal then has something to clear. */
  readonly stored: boolean;
  /** On this server now; an entry for an account that was removed is still listed, to clear it. */
  readonly known: boolean;
}

/** One row per provider account on the server, by its display name, then any stored entry left over. */
export function usagePacingAccountRows(
  providers: ReadonlyArray<{
    readonly instanceId: ProviderInstanceId;
    readonly displayName?: string | undefined;
  }>,
  usagePacing: Readonly<Record<string, UsagePacingInstanceSettings>>,
): ReadonlyArray<UsagePacingAccountRow> {
  const rows: Array<UsagePacingAccountRow> = [];
  const seen = new Set<string>();
  for (const provider of providers) {
    if (seen.has(provider.instanceId)) continue;
    seen.add(provider.instanceId);
    const stored = usagePacing[provider.instanceId];
    rows.push({
      instanceId: provider.instanceId,
      label: provider.displayName ?? provider.instanceId,
      settings: stored ?? DEFAULT_USAGE_PACING_INSTANCE_SETTINGS,
      stored: stored !== undefined,
      known: true,
    });
  }
  for (const [instanceId, settings] of Object.entries(usagePacing).toSorted(([a], [b]) =>
    a.localeCompare(b),
  )) {
    if (seen.has(instanceId)) continue;
    rows.push({
      instanceId: instanceId as ProviderInstanceId,
      label: instanceId,
      settings,
      stored: true,
      known: false,
    });
  }
  return rows;
}

export function usagePacingAccountSummary(settings: UsagePacingInstanceSettings): string {
  if (settings.noLimits) return "No usage limits";
  const reserve =
    settings.reservePercent > 0 ? `${settings.reservePercent}% kept free` : "No reserve";
  return settings.burnFirst ? `${reserve}, spent first` : reserve;
}

export const USAGE_PACING_GATE_OPTIONS: ReadonlyArray<{
  readonly value: UsagePacingGateMode;
  readonly label: string;
  readonly description: string;
}> = [
  {
    value: "off",
    label: "Off",
    description: "Agents start tasks as asked; the pace is not read.",
  },
  {
    value: "advise",
    label: "Advise",
    description:
      "Tasks start as asked, and the agent is told when an account is ahead of its pace.",
  },
  {
    value: "enforce",
    label: "Enforce",
    description:
      "A task on an account ahead of its pace moves to another account of the same provider, runs at a lower effort, or waits.",
  },
];

/** `high=medium, medium=low`, sorted by level, as the effort field shows a map. */
export function formatLowerEffort(map: UsagePacingLowerEffort): string {
  return Object.entries(map)
    .toSorted(([a], [b]) => a.localeCompare(b))
    .map(([from, to]) => `${from}=${to}`)
    .join(", ");
}

/** The option ids the pacing gate reads as a model's effort (apps/server/src/usage/UsagePacingGate.ts). */
const EFFORT_OPTION_IDS: ReadonlySet<string> = new Set([
  "effort",
  "reasoningEffort",
  "reasoning_effort",
  "reasoning",
  "thinking_level",
]);

/** The usual effort levels, known before any provider has listed its models. */
const COMMON_EFFORT_LEVELS = ["none", "minimal", "low", "medium", "high", "xhigh", "max"];

/** Every effort level a model on the selected servers offers, and the usual ones. */
export function knownEffortLevels(
  providers: ReadonlyArray<{
    readonly models?:
      | ReadonlyArray<{
          readonly capabilities: {
            readonly optionDescriptors?: ReadonlyArray<ProviderOptionDescriptor> | undefined;
          } | null;
        }>
      | undefined;
  }>,
): ReadonlySet<string> {
  const levels = new Set(COMMON_EFFORT_LEVELS);
  for (const provider of providers) {
    for (const model of provider.models ?? []) {
      for (const descriptor of model.capabilities?.optionDescriptors ?? []) {
        if (descriptor.type !== "select") continue;
        if (descriptor.role !== "reasoning" && !EFFORT_OPTION_IDS.has(descriptor.id)) continue;
        for (const choice of descriptor.options) levels.add(choice.id);
      }
    }
  }
  return levels;
}

/**
 * The map typed in the effort field, or why it cannot be read. Empty clears it. A level no model
 * offers (a typo such as `hgih`) is refused: the gate would never match it, so the task would wait.
 */
export function parseLowerEffort(
  text: string,
  known: ReadonlySet<string>,
): { readonly value: UsagePacingLowerEffort } | { readonly error: string } {
  const value: Record<string, string> = {};
  for (const part of text.split(/[,\n]/)) {
    const pair = part.trim();
    if (pair === "") continue;
    const match = /^([^=\s]+)\s*=\s*([^=\s]+)$/.exec(pair);
    if (match === null) {
      return { error: `"${pair}" is not a pair like high=medium.` };
    }
    const [, from, to] = match as unknown as [string, string, string];
    const unknown = [from, to].find((level) => !known.has(level));
    if (unknown !== undefined) {
      return {
        error: `${unknown} is not an effort level any model here offers (${[...known].join(", ")}).`,
      };
    }
    if (Object.hasOwn(value, from)) return { error: `${from} is listed twice.` };
    value[from] = to;
  }
  return { value };
}
