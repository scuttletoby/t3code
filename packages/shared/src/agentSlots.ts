/**
 * Fork: pure parts of the agent slot gate on `delegate_task`: the limits, who counts as running,
 * the decision and the refusal text. Nothing here reads a clock, the projection or the host; the
 * gate (`apps/server/src/slots/SlotGate.ts`) passes `now` and what it read.
 */
import type { AgentSlotsMode, AgentSlotsSettings } from "@t3tools/contracts/waitFor";

const BYTES_PER_MB = 1024 * 1024;

/** The limits in force; `null` is "no limit". */
export interface SlotLimits {
  readonly machine: number | null;
  readonly account: (instanceId: string) => number | null;
  readonly minFreeMemoryBytes: number | null;
  readonly startGraceMs: number;
  /** `enforce` mode: the capacity rule decides after the count limits (the gate sets `capacity`). */
  readonly enforce?: boolean;
  /**
   * The capacity rule on one more agent of `kind`, with everything counted: `null` lets it start.
   * Absent: no capacity rule (`fixed`).
   */
  readonly capacity?: (input: CapacityAsk) => CapacityRefusal | null;
}

/** What the capacity rule is asked: the counted agents, as `slotDecision` counts them. */
export interface CapacityAsk {
  readonly running: ReadonlyArray<RunningAgent>;
  readonly starting: ReadonlyArray<StartingEntry>;
  readonly reservations: number;
  readonly kind: string | undefined;
}

export interface CapacityRefusal {
  readonly reason: string;
  /**
   * Free MB and the agent's cost (budget); agents counted and the ceiling (ceiling), or the floor
   * (unreadable); writers counted (writers); else agents counted.
   */
  readonly used: number;
  readonly max: number;
}

/**
 * `enforce` without a typed machine limit counts up to the highest derived ceiling; the capacity
 * rule's own ceiling (derived, or fixed in fallback) is checked with the rest of the rule.
 */
export const ENFORCE_COUNT_LIMIT = 64;

/**
 * The limits of `agentSlots`, or `null` when the gate is off or has nothing to limit. A `0` is "no
 * limit" for every key, `maxPerAccount` entries too, the same as `maxPerMachine: 0` (a missing
 * account is also no limit). To keep an account from starting agents, turn it off for the project.
 */
export function effectiveSlotLimits(settings: AgentSlotsSettings): SlotLimits | null {
  // `enforce` (the default) limits while switched off too; switched on, the typed limits also apply.
  const enforce = agentSlotsMode(settings) === "enforce";
  if (!settings.enabled && !enforce) return null;
  const typed = settings.enabled;
  const machine = typed && settings.maxPerMachine > 0 ? settings.maxPerMachine : null;
  const accounts = new Map(
    Object.entries(typed ? settings.maxPerAccount : {}).filter(([, max]) => max > 0) as Array<
      [string, number]
    >,
  );
  const minFreeMemoryBytes =
    typed && settings.minFreeMemoryMb > 0 ? settings.minFreeMemoryMb * BYTES_PER_MB : null;
  if (enforce) {
    return {
      machine: Math.min(machine ?? ENFORCE_COUNT_LIMIT, ENFORCE_COUNT_LIMIT),
      account: (instanceId) => accounts.get(instanceId) ?? null,
      minFreeMemoryBytes,
      startGraceMs: settings.startGraceSeconds * 1000,
      enforce: true,
    };
  }
  if (machine === null && accounts.size === 0 && minFreeMemoryBytes === null) return null;
  return {
    machine,
    account: (instanceId) => accounts.get(instanceId) ?? null,
    minFreeMemoryBytes,
    startGraceMs: settings.startGraceSeconds * 1000,
  };
}

/**
 * The gate's mode (`AgentSlotsMode`). A file without `mode` that is switched on with typed numbers
 * keeps its old behaviour, `fixed` (exactly those numbers); switched off (the default) is `enforce`:
 * the capacity rule decides (owner decision: on by default). Any other text, such as a mode of a
 * newer build, is `fixed`. To turn the gate off: `mode: "fixed"` while switched off.
 */
export function agentSlotsMode(
  settings: Pick<AgentSlotsSettings, "enabled" | "mode">,
): AgentSlotsMode {
  if (settings.mode === "observe" || settings.mode === "fixed" || settings.mode === "enforce") {
    return settings.mode;
  }
  if (settings.mode === undefined && !settings.enabled) return "enforce";
  return "fixed";
}

/** Whether these limits need the running agents counted (else only memory is read). */
export const limitsCount = (limits: SlotLimits, targetInstanceId: string): boolean =>
  limits.machine !== null || limits.account(targetInstanceId) !== null;

/** The fields of a thread shell the count reads. */
export interface SlotThread {
  readonly id: string;
  readonly providerInstanceId: string;
  readonly activeRunId: string | null;
  readonly blockedOnThreadWait?: boolean | undefined;
}

/** A running agent: a thread with an active run, on any provider. */
export interface RunningAgent {
  readonly threadId: string;
  readonly instanceId: string;
}

/**
 * Threads with an active run, less the caller (`null`: nobody) and less threads blocked in a
 * thread wait (a parent inside `t3_thread_wait` holds no model turn of its own while it waits).
 */
export function runningAgents(
  threads: ReadonlyArray<SlotThread>,
  callerThreadId: string | null,
): ReadonlyArray<RunningAgent> {
  const seen = new Set<string>();
  const running: Array<RunningAgent> = [];
  for (const thread of threads) {
    if (thread.activeRunId === null || thread.blockedOnThreadWait === true) continue;
    if (thread.id === callerThreadId || seen.has(thread.id)) continue;
    seen.add(thread.id);
    running.push({ threadId: thread.id, instanceId: thread.providerInstanceId });
  }
  return running;
}

/**
 * Every thread with an active run, the caller and blocked threads too: a started child that shows
 * a run is visible, even when it is the caller now or waits on a thread, so its entry ends.
 */
export const visibleRunThreadIds = (threads: ReadonlyArray<SlotThread>): ReadonlySet<string> =>
  new Set(threads.flatMap((thread) => (thread.activeRunId === null ? [] : [thread.id])));

/**
 * How long an entry whose dispatch never ended still counts. The caller ends it on every exit
 * (an Effect finalizer), so this only catches a lost end; it does not depend on the start grace.
 */
export const DISPATCH_SAFETY_MS = 10 * 60 * 1000;

/**
 * A delegation the gate let through whose child may not show a run yet. `childThreadId` is set
 * once the dispatch named the child; `endedAtMs` once the dispatch ended (the start grace runs
 * from there).
 */
export interface StartingEntry {
  readonly token: number;
  readonly instanceId: string;
  readonly atMs: number;
  readonly childThreadId?: string;
  readonly endedAtMs?: number;
  /**
   * A message sent into this existing thread while it showed a run: it only queued, so it counts
   * only while that thread shows none (its run ended before the message landed, so the message
   * starts the next one). Never named: it ends by time only (`liveStarting`).
   */
  readonly queuedInto?: string;
  /** The capacity kind of the start (`kindOfRole`); absent: a writer. */
  readonly kind?: string;
}

/**
 * Entries that still count. An entry ends as soon as its child shows a run (it counts as running
 * then, once). Until its dispatch ends it counts whatever the start grace, so concurrent callers
 * never get more slots than are free (a lost end stops after `DISPATCH_SAFETY_MS`). After the
 * dispatch, a child that shows no run yet (a queued run) counts for the start grace.
 */
export function liveStarting<Entry extends StartingEntry>(
  entries: ReadonlyArray<Entry>,
  visibleRunIds: ReadonlySet<string>,
  nowMs: number,
  startGraceMs: number,
): ReadonlyArray<Entry> {
  return entries.filter((entry) => {
    if (entry.childThreadId !== undefined && visibleRunIds.has(entry.childThreadId)) return false;
    return entry.endedAtMs === undefined
      ? nowMs - entry.atMs < DISPATCH_SAFETY_MS
      : nowMs - entry.endedAtMs < startGraceMs;
  });
}

/**
 * The live entries that count now: a message queued into a thread (`queuedInto`) counts only
 * while that thread shows no run (else its run is counted, once). Entries that name one thread
 * (queued into it, or a start in it) count once: a thread runs one run at a time, so more
 * messages into it, or a delivery tried again, start no second agent there.
 */
export function countedStarting<Entry extends StartingEntry>(
  entries: ReadonlyArray<Entry>,
  visibleRunIds: ReadonlySet<string>,
): ReadonlyArray<Entry> {
  const named = new Set<string>();
  return entries.filter((entry) => {
    if (entry.queuedInto !== undefined && visibleRunIds.has(entry.queuedInto)) return false;
    const thread = entry.queuedInto ?? entry.childThreadId;
    if (thread === undefined) return true;
    if (named.has(thread)) return false;
    named.add(thread);
    return true;
  });
}

/**
 * A slot held for a thread whose slot wait was met (gate memory, or a delivered wait's
 * `reservedUntil`; `key` is the wait id, so one wait counts once). Counts as a running agent on the
 * machine and its `account`; the thread's next allowed delegation uses it up.
 */
export interface SlotReservation {
  readonly key: string;
  readonly threadId: string;
  readonly account?: string;
  readonly untilMs: number;
}

/** Reservations still live at `nowMs`, one per key (the latest end wins), oldest end first. */
export function liveReservations(
  reservations: ReadonlyArray<SlotReservation>,
  nowMs: number,
): ReadonlyArray<SlotReservation> {
  const byKey = new Map<string, SlotReservation>();
  for (const reservation of reservations) {
    if (reservation.untilMs <= nowMs) continue;
    const seen = byKey.get(reservation.key);
    if (seen === undefined || seen.untilMs < reservation.untilMs) {
      byKey.set(reservation.key, reservation);
    }
  }
  return [...byKey.values()].sort((a, b) => a.untilMs - b.untilMs || (a.key < b.key ? -1 : 1));
}

/**
 * The held slots that count: at most one per thread (the latest end wins, then the last key), in
 * the order given. A thread that asks again moves its slot; it never holds two.
 */
export function onePerThread(
  reservations: ReadonlyArray<SlotReservation>,
): ReadonlyArray<SlotReservation> {
  const byThread = new Map<string, SlotReservation>();
  for (const reservation of reservations) {
    const seen = byThread.get(reservation.threadId);
    if (
      seen === undefined ||
      seen.untilMs < reservation.untilMs ||
      (seen.untilMs === reservation.untilMs && seen.key < reservation.key)
    ) {
      byThread.set(reservation.threadId, reservation);
    }
  }
  return reservations.filter((reservation) => byThread.get(reservation.threadId) === reservation);
}

/**
 * Wake-ups still to start for held slots (the woken run is an agent too): one per held thread that
 * shows no run, is not woken yet (`woken`: delivered wait keys) and has no live entry naming it.
 * Machine only (`instanceId` ""); its account is checked when the wake-up starts. Never recorded.
 */
export function pendingWakes(
  reservations: ReadonlyArray<SlotReservation>,
  visibleRunIds: ReadonlySet<string>,
  starting: ReadonlyArray<StartingEntry>,
  woken: ReadonlySet<string>,
): ReadonlyArray<StartingEntry> {
  const named = new Set(
    starting.flatMap((entry) => [
      ...(entry.childThreadId === undefined ? [] : [entry.childThreadId]),
      ...(entry.queuedInto === undefined ? [] : [entry.queuedInto]),
    ]),
  );
  const threads = new Set<string>();
  for (const reservation of reservations) {
    if (woken.has(reservation.key) || visibleRunIds.has(reservation.threadId)) continue;
    if (named.has(reservation.threadId)) continue;
    threads.add(reservation.threadId);
  }
  return [...threads].map((_, index) => ({ token: -1_000 - index, instanceId: "", atMs: 0 }));
}

export type SlotRule = "machine" | "account" | "memory" | "capacity";

export type SlotDecision =
  | { readonly type: "allow" }
  | {
      readonly type: "refuse";
      readonly rule: SlotRule;
      /** Agents counted (count rules) or free MB (memory). */
      readonly used: number;
      /** The limit (count rules) or the floor in MB (memory). */
      readonly max: number;
      readonly instanceId: string;
      /**
       * Of `used`, the starts waiting in the gate's line ahead of this one (count rules): they are
       * not running yet, so `wait_list` does not show them.
       */
      readonly inLine?: number | undefined;
      /** The capacity rule's reason (`capacity`): `budget`, `elevated`, `ceiling`, ... */
      readonly capacity?: string | undefined;
    };

export interface SlotDecisionInput {
  readonly limits: SlotLimits;
  readonly running: ReadonlyArray<RunningAgent>;
  /** Live entries only (`liveStarting`). */
  readonly starting: ReadonlyArray<StartingEntry>;
  /** Live reservations that count (`liveReservations`), without the caller's own. */
  readonly reservations?: ReadonlyArray<SlotReservation>;
  /**
   * Live held slots that count against their account only, never the machine: a thread's earlier
   * hold while its re-ask is not kept yet (its new hold counts on the machine). Each thread counts
   * once per account, and not where one of its `reservations` already holds that account.
   */
  readonly accountOnly?: ReadonlyArray<SlotReservation>;
  readonly targetInstanceId: string;
  /** `null` when the memory floor is off. */
  readonly freeMemoryBytes: number | null;
  /** The capacity kind of this start; absent: a writer. */
  readonly kind?: string | undefined;
}

/** Threads whose `accountOnly` holds are on `account` and not already counted there. */
export function accountOnlyThreads(
  counted: ReadonlyArray<SlotReservation>,
  accountOnly: ReadonlyArray<SlotReservation>,
  account: string,
): number {
  const already = new Set(
    counted.flatMap((reservation) =>
      reservation.account === account ? [reservation.threadId] : [],
    ),
  );
  return new Set(
    accountOnly.flatMap((reservation) =>
      reservation.account === account && !already.has(reservation.threadId)
        ? [reservation.threadId]
        : [],
    ),
  ).size;
}

/** Machine limit first, then the target's account, then the memory floor. */
export function slotDecision(input: SlotDecisionInput): SlotDecision {
  const { limits, running, starting, targetInstanceId } = input;
  const reservations = input.reservations ?? [];
  const refuse = (rule: SlotRule, used: number, max: number): SlotDecision => ({
    type: "refuse",
    rule,
    used,
    max,
    instanceId: targetInstanceId,
  });
  const machineUsed = running.length + starting.length + reservations.length;
  if (limits.machine !== null && machineUsed >= limits.machine) {
    return refuse("machine", machineUsed, limits.machine);
  }
  const accountMax = limits.account(targetInstanceId);
  if (accountMax !== null) {
    const accountUsed =
      running.filter((agent) => agent.instanceId === targetInstanceId).length +
      starting.filter((entry) => entry.instanceId === targetInstanceId).length +
      reservations.filter((reservation) => reservation.account === targetInstanceId).length +
      accountOnlyThreads(reservations, input.accountOnly ?? [], targetInstanceId);
    if (accountUsed >= accountMax) return refuse("account", accountUsed, accountMax);
  }
  if (
    limits.minFreeMemoryBytes !== null &&
    input.freeMemoryBytes !== null &&
    input.freeMemoryBytes < limits.minFreeMemoryBytes
  ) {
    return refuse(
      "memory",
      Math.floor(input.freeMemoryBytes / BYTES_PER_MB),
      limits.minFreeMemoryBytes / BYTES_PER_MB,
    );
  }
  const capacity = limits.capacity?.({
    running,
    starting,
    reservations: reservations.length,
    kind: input.kind,
  });
  if (capacity !== undefined && capacity !== null) {
    return {
      type: "refuse",
      rule: "capacity",
      used: capacity.used,
      max: capacity.max,
      instanceId: targetInstanceId,
      capacity: capacity.reason,
    };
  }
  return { type: "allow" };
}

/**
 * "N agents are running{where}", and the starts waiting in line ahead of this one apart
 * (`inLine`), so the count matches `wait_list`.
 */
export function slotRunningText(
  decision: Extract<SlotDecision, { readonly type: "refuse" }>,
  where: string,
): string {
  const agents = (count: number) => (count === 1 ? "1 agent is" : `${count} agents are`);
  const inLine = decision.inLine ?? 0;
  if (inLine === 0) return `${agents(decision.used)} running${where}`;
  const starts = inLine === 1 ? "1 start is" : `${inLine} starts are`;
  return `${agents(decision.used - inLine)} running${where} and ${starts} waiting in line ahead of this one`;
}

/** Why a slot is not free, without a full stop (held slots count as running agents). */
export function slotRefusalReason(
  decision: Extract<SlotDecision, { readonly type: "refuse" }>,
): string {
  return decision.rule === "machine"
    ? `${slotRunningText(decision, " on this machine")}, and the limit is ${decision.max}`
    : decision.rule === "account"
      ? `${slotRunningText(decision, ` on account ${decision.instanceId}`)}, and the limit is ${decision.max}`
      : decision.rule === "capacity"
        ? capacityRefusalReason(decision.capacity, decision.used, decision.max, "this machine")
        : `only ${decision.used} MB of memory is free, and a new agent needs at least ${decision.max} MB`;
}

/** The capacity rule's refusal, in one clause; `where`: "this machine", or "there". */
export function capacityRefusalReason(
  reason: string | undefined,
  used: number,
  max: number,
  where: string,
): string {
  switch (reason) {
    case "budget":
      return `only ${used} MB of memory is free for new work on ${where}, and this agent needs ${max} MB`;
    case "ceiling":
      return `${used} agents are running or starting on ${where}, and the limit is ${max} now`;
    case "writers":
      return `${used} writing agents are running or starting on ${where}, as many as its build room allows`;
    case "starting":
      return `the slot gate on ${where} started a short while ago and waits for about a minute of calm memory readings`;
    case "unreadable":
      return `the memory of ${where} cannot be read now and ${used} agents are running or starting; only ${max} start without readings, and the fixed limit applies if they stay unreadable for 2 minutes`;
    default:
      return `the memory of ${where} is under pressure (${reason ?? "unknown"})`;
  }
}

/**
 * The `slots_full` message. `waitsEnabled`: a slot wait can be registered (the `slot` kind of
 * `wait_for`), so the text names the call; else it says to try again later.
 */
export function slotRefusalText(
  decision: Extract<SlotDecision, { readonly type: "refuse" }>,
  waitsEnabled: boolean,
): string {
  const reason = slotRefusalReason(decision);
  const slot =
    decision.rule === "account" ? `{"account":${JSON.stringify(decision.instanceId)}}` : "{}";
  const next = waitsEnabled
    ? `Call wait_for {"kind":"slot","slot":${slot}} and end your turn; you are woken when a slot is free.`
    : "Try again later.";
  return `No free agent slot: ${reason}. ${next} Or do the work yourself.`;
}
