/**
 * Fork: pure helpers for the waits and agent slots settings (`waitFor`, `waitChecks`,
 * `agentSlots`). The UI only edits the person's own server settings through the normal settings
 * patch; nothing here is reachable by an agent.
 */
import type { ProviderInstanceId } from "@t3tools/contracts";
import {
  type AgentSlotsSettings,
  type WaitCheck,
  type WaitChecks,
  type WaitForSettings,
  WaitCheck as WaitCheckSchema,
} from "@t3tools/contracts/waitFor";
import * as Equal from "effect/Equal";
import * as Exit from "effect/Exit";
import * as Schema from "effect/Schema";

type NumberKey<T> = { [K in keyof T]: T[K] extends number ? K : never }[keyof T];

export interface NumberFieldSpec<K extends string> {
  readonly key: K;
  readonly title: string;
  readonly description: string;
  readonly unit: string;
  readonly min: number;
  readonly step: number;
}

/** Every number in `waitFor`, in the order the panel shows them. Minimums match the contract. */
export const WAIT_FOR_NUMBER_FIELDS: ReadonlyArray<NumberFieldSpec<NumberKey<WaitForSettings>>> = [
  {
    key: "defaultTimeoutHours",
    title: "Default time limit",
    description: "A wait ends as timed out after this long when the agent gives no limit.",
    unit: "hours",
    min: 1,
    step: 1,
  },
  {
    key: "maxTimeoutHours",
    title: "Longest time limit",
    description: "The longest limit an agent may ask for.",
    unit: "hours",
    min: 1,
    step: 1,
  },
  {
    key: "maxPerThread",
    title: "Open waits per thread",
    description: "A thread cannot register more waits than this at once.",
    unit: "waits",
    min: 1,
    step: 1,
  },
  {
    key: "fileSweepSeconds",
    title: "File check interval",
    description: "How often waits for a file are checked.",
    unit: "seconds",
    min: 1,
    step: 5,
  },
  {
    key: "unitSweepSeconds",
    title: "Service check interval",
    description: "How often waits for a system service are checked.",
    unit: "seconds",
    min: 1,
    step: 5,
  },
  {
    key: "prSweepSeconds",
    title: "Pull request check interval",
    description: "How often one pull request is read.",
    unit: "seconds",
    min: 1,
    step: 30,
  },
  {
    key: "noChecksGraceSeconds",
    title: "No checks after",
    description: "A pull request head with no checks at all is reported as such after this long.",
    unit: "seconds",
    min: 0,
    step: 60,
  },
  {
    key: "errorGraceSeconds",
    title: "Give up after errors",
    description: "A wait whose source fails this long without one good read ends with an error.",
    unit: "seconds",
    min: 0,
    step: 60,
  },
  {
    key: "keepEndedHours",
    title: "Keep ended waits",
    description: "Ended waits stay listed for agents this long.",
    unit: "hours",
    min: 0,
    step: 1,
  },
  {
    key: "stoppedHandOverHours",
    title: "Hand over from a stopped thread",
    description:
      "A thread you stopped is not woken. Still stopped after this long, its wake goes to its parent.",
    unit: "hours",
    min: 1,
    step: 1,
  },
];

/** The `agentSlots` numbers besides the per-account map. */
export const AGENT_SLOTS_NUMBER_FIELDS: ReadonlyArray<
  NumberFieldSpec<NumberKey<AgentSlotsSettings>>
> = [
  {
    key: "maxPerMachine",
    title: "Agents on this machine",
    description: "Most agents running at once on this server. 0 means no limit.",
    unit: "agents",
    min: 0,
    step: 1,
  },
  {
    key: "minFreeMemoryMb",
    title: "Free memory floor",
    description:
      "Refuse a new agent while less memory than this is free. 0 turns it off. Set it with a count limit.",
    unit: "MB",
    min: 0,
    step: 256,
  },
  {
    key: "reserveSeconds",
    title: "Hold a freed slot",
    description: "How long an agent woken by a free slot keeps that slot.",
    unit: "seconds",
    min: 0,
    step: 30,
  },
  {
    key: "startGraceSeconds",
    title: "Starting agents count for",
    description: "How long a started agent counts as running before its run shows up.",
    unit: "seconds",
    min: 0,
    step: 5,
  },
];

/**
 * Whether the selected environments hold different values for one part of a setting. Finer than
 * the whole-key check, so a different number does not make the on/off switch look mixed.
 */
export function targetsDiffer<S, T>(
  targets: ReadonlyArray<{ readonly settings: S }>,
  pick: (settings: S) => T,
): boolean {
  const first = targets[0];
  if (first === undefined) return false;
  const value = pick(first.settings);
  return targets.some((target) => !Equal.equals(value, pick(target.settings)));
}

/** The folded summary. Reads only stored values, so turning a switch off shows at once. */
export function waitForSummary(settings: WaitForSettings): string {
  return settings.enabled ? "On" : "Off";
}

/**
 * What the switch means right now. Off does not delete anything: stored waits stay and agents can
 * still list and cancel them, they are only not judged or delivered until it is on again.
 */
export function waitForDescription(settings: WaitForSettings): string {
  return settings.enabled
    ? "Agents can wait for a pull request, a file, a service or a named check, end their turn, and get one message when it happens."
    : "Off: agents cannot register waits and nothing is checked or delivered. Waits already stored are kept; agents can still list and cancel them.";
}

export function agentSlotsSummary(settings: AgentSlotsSettings): string {
  if (!settings.enabled) return "Off";
  const accounts = Object.values(settings.maxPerAccount).filter((limit) => limit > 0).length;
  const parts = [
    ...(settings.maxPerMachine > 0 ? [`${settings.maxPerMachine} per machine`] : []),
    ...(accounts > 0 ? [`${accounts} ${accounts === 1 ? "account" : "accounts"} limited`] : []),
    ...(settings.minFreeMemoryMb > 0 ? [`${settings.minFreeMemoryMb} MB floor`] : []),
  ];
  return parts.length === 0 ? "On, no limit set" : `On: ${parts.join(" · ")}`;
}

export function agentSlotsDescription(settings: AgentSlotsSettings): string {
  if (!settings.enabled) {
    return "Off: delegate_task starts every agent it is asked to. Agents already running are not affected by this switch.";
  }
  return agentSlotsSummary(settings) === "On, no limit set"
    ? "On, but no limit is set, so every agent still starts. Set a limit below."
    : "delegate_task refuses a new agent while a limit is reached; the agent can wait for a free slot. Agents already running are never stopped.";
}

/** One row per provider instance on this server, plus limits stored for instances it no longer has. */
export function agentSlotAccountRows(
  providers: ReadonlyArray<{
    readonly instanceId: ProviderInstanceId;
    readonly displayName?: string | undefined;
  }>,
  maxPerAccount: Readonly<Record<string, number>>,
): ReadonlyArray<{
  readonly instanceId: ProviderInstanceId;
  readonly label: string;
  readonly limit: number;
  readonly known: boolean;
}> {
  const seen = new Set<string>();
  const rows: Array<{
    instanceId: ProviderInstanceId;
    label: string;
    limit: number;
    known: boolean;
  }> = [];
  for (const provider of providers) {
    if (seen.has(provider.instanceId)) continue;
    seen.add(provider.instanceId);
    rows.push({
      instanceId: provider.instanceId,
      label: provider.displayName ?? provider.instanceId,
      limit: maxPerAccount[provider.instanceId] ?? 0,
      known: true,
    });
  }
  for (const [instanceId, limit] of Object.entries(maxPerAccount).toSorted(([a], [b]) =>
    a.localeCompare(b),
  )) {
    if (seen.has(instanceId)) continue;
    rows.push({
      instanceId: instanceId as ProviderInstanceId,
      label: instanceId,
      limit,
      known: false,
    });
  }
  return rows;
}

/** The whole new map (the server replaces it whole). 0 removes the entry: no limit. */
export function withAccountLimit(
  maxPerAccount: Readonly<Record<string, number>>,
  instanceId: ProviderInstanceId,
  limit: number,
): Record<ProviderInstanceId, number> {
  const next: Record<string, number> = { ...maxPerAccount };
  if (limit > 0) next[instanceId] = limit;
  else delete next[instanceId];
  return next as Record<ProviderInstanceId, number>;
}

/** The edit form of one check: plain strings, so an empty or half-typed field can be shown. */
export interface WaitCheckDraft {
  readonly name: string;
  /** Program and fixed arguments, one per line. */
  readonly command: string;
  readonly maxArgs: string;
  readonly argPattern: string;
  readonly intervalSeconds: string;
  readonly timeoutSeconds: string;
  readonly description: string;
}

export type WaitCheckDraftField = keyof WaitCheckDraft;

const decodeWaitCheck = Schema.decodeExit(WaitCheckSchema);
const DEFAULT_WAIT_CHECK: WaitCheck = Schema.decodeSync(WaitCheckSchema)({ argv: ["true"] });

export function emptyWaitCheckDraft(): WaitCheckDraft {
  return {
    name: "",
    command: "",
    maxArgs: String(DEFAULT_WAIT_CHECK.maxArgs),
    argPattern: DEFAULT_WAIT_CHECK.argPattern,
    intervalSeconds: String(DEFAULT_WAIT_CHECK.intervalSeconds),
    timeoutSeconds: String(DEFAULT_WAIT_CHECK.timeoutSeconds),
    description: "",
  };
}

export function waitCheckToDraft(name: string, check: WaitCheck): WaitCheckDraft {
  return {
    name,
    command: check.argv.join("\n"),
    maxArgs: String(check.maxArgs),
    argPattern: check.argPattern,
    intervalSeconds: String(check.intervalSeconds),
    timeoutSeconds: String(check.timeoutSeconds),
    description: check.description,
  };
}

const WAIT_CHECK_NAME = /^[a-z0-9][a-z0-9-]{0,39}$/u;

function wholeNumber(
  text: string,
  field: WaitCheckDraftField,
  min: number,
  max: number,
  errors: Partial<Record<WaitCheckDraftField, string>>,
): number {
  const trimmed = text.trim();
  const value = /^\d+$/u.test(trimmed) ? Number(trimmed) : Number.NaN;
  if (!Number.isSafeInteger(value) || value < min || value > max) {
    errors[field] =
      max === Number.MAX_SAFE_INTEGER
        ? `A whole number, at least ${min}.`
        : `A whole number from ${min} to ${max}.`;
  }
  return value;
}

function compiles(pattern: string): boolean {
  try {
    return new RegExp(pattern, "u") instanceof RegExp;
  } catch {
    return false;
  }
}

/**
 * Reads a draft into a stored check. Blank command lines are dropped; every field error is
 * reported at once. The contract's decode runs last, so what passes here is what the server takes.
 */
export function parseWaitCheckDraft(
  draft: WaitCheckDraft,
):
  | { readonly ok: true; readonly name: string; readonly check: WaitCheck }
  | { readonly ok: false; readonly errors: Partial<Record<WaitCheckDraftField, string>> } {
  const errors: Partial<Record<WaitCheckDraftField, string>> = {};
  const name = draft.name.trim();
  if (!WAIT_CHECK_NAME.test(name)) {
    errors.name =
      "Lower-case letters, digits and dashes, starting with a letter or digit, at most 40.";
  }
  const argv = draft.command
    .split(/\r?\n/u)
    .map((line) => line.trim())
    .filter((line) => line.length > 0);
  if (argv.length === 0) errors.command = "Enter the program, then one fixed argument per line.";
  const maxArgs = wholeNumber(draft.maxArgs, "maxArgs", 0, 20, errors);
  if (!compiles(draft.argPattern)) errors.argPattern = "Not a valid regular expression.";
  const intervalSeconds = wholeNumber(
    draft.intervalSeconds,
    "intervalSeconds",
    5,
    Number.MAX_SAFE_INTEGER,
    errors,
  );
  const timeoutSeconds = wholeNumber(draft.timeoutSeconds, "timeoutSeconds", 1, 600, errors);
  const description = draft.description.trim();
  if (description.length > 500) errors.description = "At most 500 characters.";
  if (Object.keys(errors).length > 0) return { ok: false, errors };

  const decoded = decodeWaitCheck({
    argv,
    maxArgs,
    argPattern: draft.argPattern,
    intervalSeconds,
    timeoutSeconds,
    description,
  });
  if (Exit.isFailure(decoded)) {
    return { ok: false, errors: { command: "The server would refuse this check." } };
  }
  return { ok: true, name, check: decoded.value };
}

/**
 * The whole new check map after saving one check. `previousName` is the name it was opened under
 * (null for a new check); a rename keeps its place in the list. Other checks stay as they are.
 */
export function withWaitCheck(
  checks: WaitChecks,
  previousName: string | null,
  name: string,
  check: WaitCheck,
):
  | { readonly ok: true; readonly checks: WaitChecks }
  | { readonly ok: false; readonly error: string } {
  if (name !== previousName && Object.hasOwn(checks, name)) {
    return { ok: false, error: `A check named "${name}" already exists.` };
  }
  const next: Record<string, WaitCheck> = {};
  let placed = false;
  for (const [existingName, existing] of Object.entries(checks)) {
    if (existingName === previousName) {
      next[name] = check;
      placed = true;
    } else {
      next[existingName] = existing;
    }
  }
  if (!placed) next[name] = check;
  return { ok: true, checks: next };
}

export function withoutWaitCheck(checks: WaitChecks, name: string): WaitChecks {
  return Object.fromEntries(
    Object.entries(checks).filter(([existingName]) => existingName !== name),
  ) as WaitChecks;
}

/** One line for the list: the command as an agent would see it run. */
export function waitCheckCommandLine(check: WaitCheck): string {
  const fixed = check.argv.join(" ");
  return check.maxArgs > 0
    ? `${fixed} ${Array.from({ length: check.maxArgs }, (_, index) => `<arg${index + 1}>`).join(" ")}`
    : fixed;
}
