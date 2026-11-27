/**
 * Fork (fleet usage, fork issue 1146): the fleet's accounts from each machine's last answer, pure.
 * An account several machines use is one row, shown from the freshest reading and listing every
 * machine. A number is shown only from a machine that answered this round with a reading younger
 * than `STALE_AFTER_MS`; otherwise the account is `unknown` with why, never 0 or 100.
 */
import type {
  EnvironmentId,
  ForkFleetMachineRef,
  ForkFleetUsageAccount,
  ForkFleetUsageAnswer,
  ForkFleetUsageMachine,
  ForkUsageReading,
  ForkUsageSnapshot,
} from "@t3tools/contracts";

/** Older than this, a reading is not shown (the usage pacer's limit too: six missed UI checks). */
export const STALE_AFTER_MS = 30 * 60 * 1000;

export const REASON_OLD_VERSION =
  "This machine's version does not report its usage, so its accounts are unknown here.";
export const REASON_NEVER_ANSWERED = "This machine has not answered since this server started.";

/** One machine's part of a round. */
export interface MachineRound {
  readonly environmentId: EnvironmentId;
  readonly label: string;
  readonly here: boolean;
  /** This round: an answer (`snapshot: null` is a version without usage), or why there is none. */
  readonly now:
    | { readonly _tag: "answered"; readonly snapshot: ForkUsageSnapshot | null }
    | { readonly _tag: "failed"; readonly reason: string };
  /**
   * The last answer with a snapshot (this round's when it gave one), and when it arrived (this
   * server's clock): it names a silent machine's accounts, never their numbers.
   */
  readonly last: { readonly snapshot: ForkUsageSnapshot; readonly receivedAt: number } | null;
}

const minutes = (ms: number) => {
  const total = Math.max(1, Math.round(ms / 60_000));
  return total < 120 ? `${total} min` : `${Math.round(total / 60)} h`;
};

/** A reading's age when the server answers: its machine's own clock, then the time in transit. */
const ageOf = (
  reading: ForkUsageReading,
  last: NonNullable<MachineRound["last"]>,
  answeredAt: number,
) => {
  const checkedAt = Date.parse(reading.checkedAt);
  const atRead = Number.isFinite(checkedAt) ? Math.max(0, last.snapshot.readAt - checkedAt) : 0;
  return atRead + Math.max(0, answeredAt - last.receivedAt);
};

interface Seen {
  readonly machine: ForkFleetMachineRef;
  readonly reading: ForkUsageReading;
  readonly ageMs: number;
  /** The machine answered this round with this reading. */
  readonly current: boolean;
  readonly silentReason: string | null;
}

const accountOf = (key: string, seen: ReadonlyArray<Seen>): ForkFleetUsageAccount => {
  const byAge = seen.toSorted((a, b) => a.ageMs - b.ageMs);
  const named = byAge.find((entry) => entry.current) ?? byAge[0]!;
  const machines = [
    ...new Map(byAge.map((entry) => [entry.machine.environmentId, entry.machine])).values(),
  ].toSorted((a, b) => a.label.localeCompare(b.label));
  const base = {
    key,
    driver: named.reading.driver,
    name: named.reading.name,
    ...(named.reading.plan ? { plan: named.reading.plan } : {}),
    machines,
  };
  const fresh = byAge.find(
    (entry) =>
      entry.current && entry.reading.unavailable === undefined && entry.ageMs <= STALE_AFTER_MS,
  );
  if (fresh !== undefined) {
    return {
      ...base,
      state: "known",
      readFrom: fresh.machine,
      ageMs: Math.round(fresh.ageMs),
      windows: fresh.reading.windows,
    };
  }
  const unavailable = byAge.find((entry) => entry.current && entry.reading.unavailable);
  const stale = byAge.find((entry) => entry.current);
  const silent = byAge.find((entry) => !entry.current);
  const reason =
    unavailable !== undefined
      ? `${unavailable.machine.label}: ${unavailable.reading.unavailable}`
      : stale !== undefined
        ? `Last read ${minutes(stale.ageMs)} ago on ${stale.machine.label}, too old to show.`
        : `${silent!.machine.label} did not answer: ${silent!.silentReason ?? REASON_NEVER_ANSWERED}`;
  return { ...base, state: "unknown", reason, windows: [] };
};

export const mergeFleet = (input: {
  readonly answeredAt: number;
  readonly linked: boolean;
  readonly machines: ReadonlyArray<MachineRound>;
}): ForkFleetUsageAnswer => {
  const machines: Array<ForkFleetUsageMachine> = [];
  const seen = new Map<string, Array<Seen>>();
  for (const round of input.machines) {
    const ref = { environmentId: round.environmentId, label: round.label };
    const answeredAgoMs =
      round.last === null
        ? {}
        : { answeredAgoMs: Math.max(0, input.answeredAt - round.last.receivedAt) };
    const current = round.now._tag === "answered" && round.now.snapshot !== null;
    const silentReason =
      round.now._tag === "failed"
        ? round.now.reason
        : round.now.snapshot === null
          ? REASON_OLD_VERSION
          : null;
    machines.push(
      silentReason === null
        ? { ...ref, here: round.here, state: "answered", ...answeredAgoMs }
        : { ...ref, here: round.here, state: "unknown", reason: silentReason, ...answeredAgoMs },
    );
    if (round.last === null) continue;
    for (const reading of round.last.snapshot.accounts) {
      // An account with no stable id is this machine's alone: it is never merged with another's.
      const key = reading.shared ? reading.key : `${round.environmentId}:${reading.key}`;
      const entry: Seen = {
        machine: ref,
        reading,
        ageMs: ageOf(reading, round.last, input.answeredAt),
        current,
        silentReason,
      };
      seen.set(key, [...(seen.get(key) ?? []), entry]);
    }
  }
  const accounts = [...seen]
    .map(([key, entries]) => accountOf(key, entries))
    .toSorted((a, b) => a.driver.localeCompare(b.driver) || a.name.localeCompare(b.name));
  return {
    answeredAt: input.answeredAt,
    linked: input.linked,
    machines: machines.toSorted(
      (a, b) => Number(b.here) - Number(a.here) || a.label.localeCompare(b.label),
    ),
    accounts,
  };
};
