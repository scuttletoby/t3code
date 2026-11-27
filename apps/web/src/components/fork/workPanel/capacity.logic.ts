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
import { AsyncResult } from "effect/reactivity";

import { formatAge, staleReason } from "./workPanel.logic";

/** What one machine of the fleet gave for its `fork.workPanel` read. */
export type MachineAnswer =
  | { readonly kind: "unreachable" }
  | { readonly kind: "pending" }
  | { readonly kind: "failed" }
  | {
      readonly kind: "answered";
      readonly answer: ForkWorkPanelAnswer;
      /** When the client received it (its own clock), which ages it whatever the server's skew. */
      readonly receivedAt?: number;
    };

/**
 * A machine's read as of now. A failed refresh is `failed` even when an older answer is kept
 * (`AsyncResult.getOrElse` would hand back that old success as if it were current).
 */
export function machineAnswerOf(
  result: AsyncResult.AsyncResult<ForkWorkPanelAnswer, unknown>,
): MachineAnswer {
  if (result._tag === "Failure") return { kind: "failed" };
  if (result._tag === "Success") {
    return { kind: "answered", answer: result.value, receivedAt: result.timestamp };
  }
  return { kind: "pending" };
}

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

export function machineCapacity(machine: FleetMachine, now: number): MachineCapacity {
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
            : staleReason(
                status.receivedAt ?? status.answer.capacity.factsAt ?? status.answer.answeredAt,
                now,
              );
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

export function formatGb(mb: number): string {
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
  "Busy: runs running in this thread's repositories (the queue lines above), not the whole fleet's. Free: GitHub does not report the hosted runners' concurrency cap, and the fleet starts a runner for each job (#13), so no count of free runners exists.";

/** CI runners: busy from the repo queue lines that answered; free is never known. */
export function ciRunnersText(lines: ReadonlyArray<ForkRepoQueue>): string {
  const counted = lines.filter((line) => line.state === "answered" && line.running !== undefined);
  if (counted.length === 0) return "CI runners (this thread's repositories): unknown";
  const busy = counted.reduce((sum, line) => sum + (line.running ?? 0), 0);
  return `CI runners (this thread's repositories): ${busy} busy · free unknown`;
}

/** One window kind's room left: `null` with a note when it is not known now. */
export interface UsageLeft {
  readonly left: number | null;
  readonly note: string | null;
}

/** One provider account's room left: session and weekly, each unknown when not reported. */
export interface UsageAccountView {
  readonly key: string;
  readonly name: string;
  readonly session: UsageLeft;
  readonly weekly: UsageLeft;
  readonly reason: string | null;
}

interface LimitAccountLike {
  readonly key: string;
  readonly driver: string;
  readonly displayName: string | null;
  readonly limits: {
    readonly checkedAt?: string | undefined;
    readonly windows: ReadonlyArray<ServerProviderUsageWindow>;
    readonly unavailable?: { readonly message?: string | undefined } | undefined;
  };
}

/** Limits read longer ago than this are not shown: the provider status has not been read since. */
const USAGE_STALE_MS = 30 * 60_000;
export const NOTE_RESET = "reset since the last read";

/**
 * The tightest window of `kind` (Claude reports several weekly ones: all models and per model),
 * leaving out a window whose reset time has passed, since its percentage was read before it.
 */
const leftOf = (
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  kind: "session" | "weekly",
  now: number,
): UsageLeft => {
  const ofKind = windows.filter((window) => window.kind === kind);
  if (ofKind.length === 0) return { left: null, note: null };
  const current = ofKind.filter((window) => {
    const resetsAt = window.resetsAt === undefined ? Number.NaN : Date.parse(window.resetsAt);
    return !(Number.isFinite(resetsAt) && resetsAt <= now);
  });
  if (current.length < ofKind.length) return { left: null, note: NOTE_RESET };
  const left = Math.min(
    ...current.map((window) => Math.round(100 - Math.max(0, Math.min(100, window.usedPercent)))),
  );
  return { left, note: null };
};

const UNKNOWN_LEFT: UsageLeft = { left: null, note: null };

/** Each account by its label only: the panel may be screenshotted, so no e-mail. */
export function usageAccounts(
  accounts: ReadonlyArray<LimitAccountLike>,
  now: number,
): ReadonlyArray<UsageAccountView> {
  return accounts.map((account) => {
    const unavailable = account.limits.unavailable;
    const checkedAt =
      account.limits.checkedAt === undefined ? Number.NaN : Date.parse(account.limits.checkedAt);
    const reason =
      unavailable !== undefined
        ? (unavailable.message ?? "The provider did not report its limits.")
        : !Number.isFinite(checkedAt)
          ? "The provider status gives no read time."
          : now - checkedAt > USAGE_STALE_MS
            ? `Last read ${formatAge(now - checkedAt)} ago.`
            : null;
    const windows = reason === null ? account.limits.windows : [];
    return {
      key: account.key,
      name: account.displayName ?? account.driver,
      session: reason === null ? leftOf(windows, "session", now) : UNKNOWN_LEFT,
      weekly: reason === null ? leftOf(windows, "weekly", now) : UNKNOWN_LEFT,
      reason,
    };
  });
}
