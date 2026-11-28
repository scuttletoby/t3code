/**
 * Fork (fleet usage, fork issue 1146): the `fork.fleetUsage` answer as the Usage tab shows it. A
 * number only for a `known` account, else unknown and why; nothing without a linked machine. The
 * work panel's usage row reads the same answer through `asLimitAccounts`.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import type {
  ForkFleetUsageAccount,
  ForkFleetUsageAnswer,
  ServerProviderUsageWindow,
} from "@t3tools/contracts";

export interface FleetWindowView {
  readonly id: string;
  readonly label: string;
  /** Whole percent left, 0 to 100; only for a known account. */
  readonly left: number;
  readonly resetText: string | null;
}

export interface FleetAccountView {
  readonly key: string;
  readonly title: string;
  readonly machinesText: string;
  readonly known: boolean;
  /** For a known account: "Read 3 min ago on Machine B". For an unknown one: why. */
  readonly detail: string;
  readonly windows: ReadonlyArray<FleetWindowView>;
}

export interface FleetMachineView {
  readonly environmentId: string;
  readonly text: string;
  readonly known: boolean;
}

export interface FleetUsageView {
  readonly accounts: ReadonlyArray<FleetAccountView>;
  readonly machines: ReadonlyArray<FleetMachineView>;
}

const agoText = (ms: number) => {
  const minutes = Math.round(ms / 60_000);
  if (minutes < 1) return "just now";
  if (minutes < 120) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h ago` : `${Math.round(hours / 24)} d ago`;
};

const inText = (ms: number) => {
  const minutes = Math.max(1, Math.round(ms / 60_000));
  if (minutes < 120) return `${minutes} min`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h` : `${Math.round(hours / 24)} d`;
};

const resetText = (window: ServerProviderUsageWindow, now: number) => {
  if (window.resetsAt === undefined) return null;
  const at = Date.parse(window.resetsAt);
  if (!Number.isFinite(at)) return null;
  return at <= now ? "resets now" : `resets in ${inText(at - now)}`;
};

const leftOf = (window: ServerProviderUsageWindow) =>
  Math.round(100 - Math.max(0, Math.min(100, window.usedPercent)));

const accountView = (account: ForkFleetUsageAccount, now: number): FleetAccountView => {
  const known = account.state === "known";
  return {
    key: account.key,
    title: account.plan ? `${account.name} · ${account.plan}` : account.name,
    machinesText: `Used on ${account.machines.map((machine) => machine.label).join(", ")}`,
    known,
    detail: known
      ? `Read ${agoText(account.ageMs ?? 0)} on ${account.readFrom?.label ?? "an unnamed machine"}`
      : `Unknown: ${account.reason ?? "no machine has a fresh reading."}`,
    windows: known
      ? account.windows.map((window) => ({
          id: window.id,
          label: window.label,
          left: leftOf(window),
          resetText: resetText(window, now),
        }))
      : [],
  };
};

/** What the tab shows, or `null` to show nothing more than upstream's list. */
export function fleetUsageView(
  answer: ForkFleetUsageAnswer | null,
  now: number,
): FleetUsageView | null {
  if (answer === null || !answer.linked) return null;
  return {
    accounts: answer.accounts.map((account) => accountView(account, now)),
    machines: answer.machines.map((machine) => ({
      environmentId: machine.environmentId,
      known: machine.state === "answered",
      text:
        machine.state === "answered"
          ? `${machine.label}${machine.here ? " (this server)" : ""}: answered`
          : `${machine.label}: unknown. ${machine.reason ?? "It did not answer."}`,
    })),
  };
}

/**
 * The fleet's accounts in the shape upstream's `LimitAccount` has for the work panel's usage row
 * (`usageAccounts` there): an unknown account carries its reason as `limits.unavailable`.
 */
export function asLimitAccounts(answer: ForkFleetUsageAnswer | null) {
  return (answer?.accounts ?? []).map((account) => ({
    key: account.key,
    driver: account.driver,
    displayName: account.plan ? `${account.name} · ${account.plan}` : account.name,
    email: undefined,
    limits: {
      windows: account.state === "known" ? account.windows : [],
      ...(account.state === "known"
        ? {}
        : { unavailable: { message: account.reason ?? "No machine has a fresh reading." } }),
    },
  }));
}
