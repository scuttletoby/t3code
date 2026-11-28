/**
 * Fork: account failover by the owner's order (`accountOrder` setting). When the account a
 * delegated task would run on is spent, the task goes to the first account in the order that has
 * room. Pure: the paces come from the `UsagePacer`, so "spent" and "has room" are read by the
 * pacer's own rules (its stale bound, its reset check, the owner's reserve).
 */
import type { ProviderInstanceId, ServerProvider } from "@t3tools/contracts";

import type { InstancePace, PaceUnknownReason, WindowPace } from "./UsagePacer.ts";
import { sameAccount } from "./UsagePacingGate.ts";

/** How one account stands for the order. */
export type AccountState =
  /** Its usage is known and no window is at its limit; or the owner marked it `noLimits`. */
  | { readonly kind: "open" }
  /** A window whose usage is current is at or over 100 minus the owner's reserve. */
  | { readonly kind: "spent"; readonly windows: ReadonlyArray<WindowPace> }
  /** Its usage is not known now (stale, reset passed, never read...): no new work goes there. */
  | { readonly kind: "unknown"; readonly reason: PaceUnknownReason | "notRead" };

/**
 * A window whose value is current: paced, or fresh without a reset clock. A stale value, or one
 * whose reset passed (it belongs to the window before), says nothing about now.
 */
const isCurrent = (window: WindowPace) =>
  window.usedPercent !== null &&
  (window.verdict !== "unknown" || window.unknownReason === "noResetClock");

/** The owner's reserve as the pacer releases it; the whole reserve where no reset clock places it. */
const isFull = (window: WindowPace, pace: InstancePace) =>
  window.usedPercent! >= 100 - (window.effectiveReservePercent ?? pace.reservePercent);

export function accountStateOf(pace: InstancePace | undefined): AccountState {
  if (pace === undefined) return { kind: "unknown", reason: "notRead" };
  const full = pace.windows.filter((window) => isCurrent(window) && isFull(window, pace));
  if (full.length > 0) return { kind: "spent", windows: full };
  if (pace.verdict !== "unknown" || pace.unknownReason === "noLimits") return { kind: "open" };
  return { kind: "unknown", reason: pace.unknownReason ?? "noReading" };
}

export interface OrderCandidate {
  readonly instanceId: ProviderInstanceId;
  readonly state: AccountState;
}

/**
 * The accounts of the order a task on `current` may move to, in the order: the same provider, an
 * instance the server has, and not the same login as `current` (it shares its limits). The caller
 * takes the first `open` one the task can run on.
 */
export function orderCandidateIds(input: {
  readonly order: ReadonlyArray<ProviderInstanceId>;
  readonly current: ServerProvider;
  readonly providers: ReadonlyArray<ServerProvider>;
}): ReadonlyArray<ProviderInstanceId> {
  return input.order.filter((instanceId) => {
    if (instanceId === input.current.instanceId) return false;
    const provider = input.providers.find((entry) => entry.instanceId === instanceId);
    return (
      provider !== undefined &&
      provider.driver === input.current.driver &&
      !sameAccount(provider, input.current)
    );
  });
}

export interface OrderCandidate {
  readonly instanceId: ProviderInstanceId;
  readonly state: AccountState;
}

const percent = (value: number) => `${Math.round(value)}%`;

/** "Weekly 100% used", for a note. */
export function describeSpent(windows: ReadonlyArray<WindowPace>): string {
  return windows
    .map((window) => `${window.label} ${percent(window.usedPercent ?? 0)} used`)
    .join("; ");
}

/** Why a candidate took no work, for a note: "claude_second is at its limit (Weekly 100% used)". */
export function describeSkipped(candidate: OrderCandidate): string {
  switch (candidate.state.kind) {
    case "spent":
      return `${candidate.instanceId} is at its limit (${describeSpent(candidate.state.windows)})`;
    case "unknown":
      return `${candidate.instanceId} has no current usage reading (${candidate.state.reason})`;
    case "open":
      return `${candidate.instanceId} has room`;
  }
}
