/**
 * Fork (work panel, fork issue 1136): the capacity bar and the usage bars. Every machine and every
 * account says where its number comes from; one with no answer is unknown, never a guessed 0 or
 * 100.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import type {
  EnvironmentId,
  ForkCapacityPart,
  ForkRepoQueue,
  ForkWorkPanelAnswer,
  ServerProviderUsageWindow,
} from "@t3tools/contracts";

/** What one machine of the fleet gave for its `fork.workPanel` read. */
export type MachineAnswer =
  | { readonly kind: "unreachable" }
  | { readonly kind: "pending" }
  | { readonly kind: "failed" }
  | { readonly kind: "answered"; readonly answer: ForkWorkPanelAnswer };

export interface FleetMachine {
  readonly environmentId: EnvironmentId;
  readonly label: string;
  readonly isThisMachine: boolean;
  readonly status: MachineAnswer;
}

export interface MachineCapacity {
  readonly environmentId: EnvironmentId;
  readonly label: string;
  readonly isThisMachine: boolean;
  readonly agents: ForkCapacityPart;
  readonly memoryMb: ForkCapacityPart;
  readonly memoryFloorMb: number | null;
}

export const REASON_UNREACHABLE = "Not connected to this machine.";
const REASON_PENDING = "Waiting for this machine's answer.";
export const REASON_FAILED =
  "This machine did not answer (it may be offline or run a version without the work panel).";
export const REASON_OLD_VERSION = "This machine's version does not report capacity.";
const SOURCE_NONE = "No answer from this machine.";

const unknown = (reason: string): ForkCapacityPart => ({
  state: "unknown",
  reason,
  source: SOURCE_NONE,
});

export function machineCapacity(machine: FleetMachine): MachineCapacity {
  const base = {
    environmentId: machine.environmentId,
    label: machine.label,
    isThisMachine: machine.isThisMachine,
  };
  const { status } = machine;
  const reason =
    status.kind === "unreachable"
      ? REASON_UNREACHABLE
      : status.kind === "pending"
        ? REASON_PENDING
        : status.kind === "failed"
          ? REASON_FAILED
          : status.answer.capacity === undefined
            ? REASON_OLD_VERSION
            : null;
  if (reason !== null || status.kind !== "answered" || status.answer.capacity === undefined) {
    const part = unknown(reason ?? REASON_OLD_VERSION);
    return { ...base, agents: part, memoryMb: part, memoryFloorMb: null };
  }
  const { capacity } = status.answer;
  return {
    ...base,
    agents: capacity.agents,
    memoryMb: capacity.memoryMb,
    memoryFloorMb: capacity.memoryFloorMb ?? null,
  };
}

const answered = (part: ForkCapacityPart) =>
  part.state === "answered" && part.used !== undefined && part.total !== undefined;

export interface FleetTotal {
  /** Sum over the machines that answered. */
  readonly used: number;
  /** Sum over them; `null` when one of them sets no limit. */
  readonly total: number | null;
  readonly answered: number;
  readonly unknown: number;
}

export function fleetTotal(
  machines: ReadonlyArray<MachineCapacity>,
  pick: (machine: MachineCapacity) => ForkCapacityPart,
): FleetTotal {
  let used = 0;
  let total: number | null = 0;
  let count = 0;
  for (const machine of machines) {
    const part = pick(machine);
    if (!answered(part)) continue;
    count += 1;
    used += part.used!;
    total = total === null || part.total === null ? null : total + part.total!;
  }
  return { used, total, answered: count, unknown: machines.length - count };
}

const unknownNote = (count: number) =>
  count === 0 ? "" : ` · ${count} machine${count === 1 ? "" : "s"} unknown`;

export function agentsTotalText(total: FleetTotal): string {
  if (total.answered === 0) return "Agents: unknown";
  const allowed = total.total === null ? "no limit" : `${total.total}`;
  return `Agents: ${total.used} of ${allowed} in use${unknownNote(total.unknown)}`;
}

function formatGb(mb: number): string {
  return `${(mb / 1024).toFixed(mb >= 10 * 1024 ? 0 : 1)} GB`;
}

export function memoryTotalText(total: FleetTotal): string {
  if (total.answered === 0 || total.total === null) return "Memory: unknown";
  return `Memory free: ${formatGb(total.total - total.used)} of ${formatGb(total.total)}${unknownNote(total.unknown)}`;
}

export function partText(part: ForkCapacityPart, unit: "agents" | "memory"): string {
  if (!answered(part)) return `unknown: ${part.reason ?? "no answer"}`;
  if (unit === "memory") {
    return part.total === null
      ? "unknown"
      : `${formatGb(part.total! - part.used!)} free of ${formatGb(part.total!)}`;
  }
  return `${part.used} of ${part.total === null ? "no limit" : part.total} in use`;
}

/** How full a part is, 0 to 1; `null` when it is unknown or has no limit. */
export function partShare(part: ForkCapacityPart): number | null {
  if (!answered(part) || part.total === null || part.total === 0) return null;
  return Math.min(1, part.used! / part.total!);
}

export const SOURCE_CI_RUNNERS =
  "Busy: runs running in the repository's shared read. Free: GitHub does not report the hosted runners' concurrency cap, and the fleet starts a runner for each job (#13), so no count of free runners exists.";

/** CI runners: busy from the repo queue lines that answered; free is never known. */
export function ciRunnersText(lines: ReadonlyArray<ForkRepoQueue>): string {
  const counted = lines.filter((line) => line.state === "answered" && line.running !== undefined);
  if (counted.length === 0) return "CI runners: unknown";
  const busy = counted.reduce((sum, line) => sum + (line.running ?? 0), 0);
  return `CI runners: ${busy} busy · free unknown`;
}

/** One provider account's room left: session and weekly, each unknown when not reported. */
export interface UsageAccountView {
  readonly key: string;
  readonly name: string;
  readonly session: number | null;
  readonly weekly: number | null;
  readonly reason: string | null;
}

interface LimitAccountLike {
  readonly key: string;
  readonly driver: string;
  readonly displayName: string | null;
  readonly email: string | undefined;
  readonly limits: {
    readonly windows: ReadonlyArray<ServerProviderUsageWindow>;
    readonly unavailable?: { readonly message?: string | undefined } | undefined;
  };
}

const leftOf = (windows: ReadonlyArray<ServerProviderUsageWindow>, kind: "session" | "weekly") => {
  const window = windows.find((candidate) => candidate.kind === kind);
  return window === undefined
    ? null
    : Math.round(100 - Math.max(0, Math.min(100, window.usedPercent)));
};

export function usageAccounts(
  accounts: ReadonlyArray<LimitAccountLike>,
): ReadonlyArray<UsageAccountView> {
  return accounts.map((account) => {
    const unavailable = account.limits.unavailable;
    const windows = unavailable === undefined ? account.limits.windows : [];
    return {
      key: account.key,
      name: [account.displayName ?? account.driver, account.email].filter(Boolean).join(" · "),
      session: leftOf(windows, "session"),
      weekly: leftOf(windows, "weekly"),
      reason:
        unavailable !== undefined
          ? (unavailable.message ?? "The provider did not report its limits.")
          : null,
    };
  });
}
