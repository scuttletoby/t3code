/**
 * Fork: pure parts of the agent slot gate on `delegate_task`: the limits, who counts as running,
 * the decision and the refusal text. Nothing here reads a clock, the projection or the host; the
 * gate (`apps/server/src/slots/SlotGate.ts`) passes `now` and what it read.
 */
import type { AgentSlotsSettings } from "@t3tools/contracts/waitFor";

const BYTES_PER_MB = 1024 * 1024;

/** The limits in force; `null` is "no limit". */
export interface SlotLimits {
  readonly machine: number | null;
  readonly account: (instanceId: string) => number | null;
  readonly minFreeMemoryBytes: number | null;
  readonly startGraceMs: number;
}

/**
 * The limits of `agentSlots`, or `null` when the gate is off or has nothing to limit. A `0` is "no
 * limit" for every key, `maxPerAccount` entries too, the same as `maxPerMachine: 0` (a missing
 * account is also no limit). To keep an account from starting agents, turn it off for the project.
 */
export function effectiveSlotLimits(settings: AgentSlotsSettings): SlotLimits | null {
  if (!settings.enabled) return null;
  const machine = settings.maxPerMachine > 0 ? settings.maxPerMachine : null;
  const accounts = new Map(
    Object.entries(settings.maxPerAccount).filter(([, max]) => max > 0) as Array<[string, number]>,
  );
  const minFreeMemoryBytes =
    settings.minFreeMemoryMb > 0 ? settings.minFreeMemoryMb * BYTES_PER_MB : null;
  if (machine === null && accounts.size === 0 && minFreeMemoryBytes === null) return null;
  return {
    machine,
    account: (instanceId) => accounts.get(instanceId) ?? null,
    minFreeMemoryBytes,
    startGraceMs: settings.startGraceSeconds * 1000,
  };
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
 * Threads with an active run, less the caller and less threads blocked in a thread wait (a
 * parent inside `t3_thread_wait` holds no model turn of its own while it waits).
 */
export function runningAgents(
  threads: ReadonlyArray<SlotThread>,
  callerThreadId: string,
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
}

/**
 * Entries that still count. An entry ends as soon as its child shows a run (it counts as running
 * then, once). Until its dispatch ends it counts whatever the start grace, so concurrent callers
 * never get more slots than are free (a lost end stops after `DISPATCH_SAFETY_MS`). After the
 * dispatch, a child that shows no run yet (a queued run) counts for the start grace.
 */
export function liveStarting(
  entries: ReadonlyArray<StartingEntry>,
  visibleRunIds: ReadonlySet<string>,
  nowMs: number,
  startGraceMs: number,
): ReadonlyArray<StartingEntry> {
  return entries.filter((entry) => {
    if (entry.childThreadId !== undefined && visibleRunIds.has(entry.childThreadId)) return false;
    return entry.endedAtMs === undefined
      ? nowMs - entry.atMs < DISPATCH_SAFETY_MS
      : nowMs - entry.endedAtMs < startGraceMs;
  });
}

export type SlotRule = "machine" | "account" | "memory";

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
    };

export interface SlotDecisionInput {
  readonly limits: SlotLimits;
  readonly running: ReadonlyArray<RunningAgent>;
  /** Live entries only (`liveStarting`). */
  readonly starting: ReadonlyArray<StartingEntry>;
  readonly targetInstanceId: string;
  /** `null` when the memory floor is off. */
  readonly freeMemoryBytes: number | null;
}

/** Machine limit first, then the target's account, then the memory floor. */
export function slotDecision(input: SlotDecisionInput): SlotDecision {
  const { limits, running, starting, targetInstanceId } = input;
  const refuse = (rule: SlotRule, used: number, max: number): SlotDecision => ({
    type: "refuse",
    rule,
    used,
    max,
    instanceId: targetInstanceId,
  });
  const machineUsed = running.length + starting.length;
  if (limits.machine !== null && machineUsed >= limits.machine) {
    return refuse("machine", machineUsed, limits.machine);
  }
  const accountMax = limits.account(targetInstanceId);
  if (accountMax !== null) {
    const accountUsed =
      running.filter((agent) => agent.instanceId === targetInstanceId).length +
      starting.filter((entry) => entry.instanceId === targetInstanceId).length;
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
  return { type: "allow" };
}

/**
 * The `slots_full` message. `waitsEnabled`: a slot wait can be registered (the `slot` kind of
 * `wait_for`), so the text names the call; else it says to try again later.
 */
export function slotRefusalText(
  decision: Extract<SlotDecision, { readonly type: "refuse" }>,
  waitsEnabled: boolean,
): string {
  const agents = (count: number) => (count === 1 ? "1 agent is" : `${count} agents are`);
  const reason =
    decision.rule === "machine"
      ? `${agents(decision.used)} running on this machine, and the limit is ${decision.max}`
      : decision.rule === "account"
        ? `${agents(decision.used)} running on account ${decision.instanceId}, and the limit is ${decision.max}`
        : `only ${decision.used} MB of memory is free, and a new agent needs at least ${decision.max} MB`;
  const slot =
    decision.rule === "account" ? `{"account":${JSON.stringify(decision.instanceId)}}` : "{}";
  const next = waitsEnabled
    ? `Call wait_for {"kind":"slot","slot":${slot}} and end your turn; you are woken when a slot is free.`
    : "Try again later.";
  return `No free agent slot: ${reason}. ${next} Or do the work yourself.`;
}
