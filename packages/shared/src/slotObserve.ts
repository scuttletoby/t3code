/**
 * Fork: the capacity rule beside the agent slot gate (`agentSlots.mode: "observe"`), and deciding
 * in its place (`enforce`, the default). Pure: the gate (`apps/server/src/slots/SlotGate.ts`,
 * `SlotObserver.ts`) passes the facts it read; nothing here reads a clock or the machine.
 */
import type { SlotRuleObservation } from "@t3tools/contracts/waitFor";

import {
  capacityDecision,
  type CapacityDecision,
  type CapacityFacts,
  type CapacityLearned,
  type CapacityPerGroup,
  type CapacityPiece,
  type CapacityRule,
  type PressureState,
} from "./capacityRule.ts";

export type SlotAnswer = "allow" | "refuse";
export type SlotReadings = SlotRuleObservation["readings"];

/**
 * After a restart the level starts "elevated" and waits for calm readings (design section 6): the
 * rule never says "ok" before it has seen the machine calm for a while.
 */
export const OBSERVE_START_PRESSURE: PressureState = { level: "elevated", calmSinceMs: null };

/** Nothing could be read: the rule answers "unreadable", never "fits". */
const NO_FACTS: CapacityFacts = { pieceCaps: false, pieces: null };

/**
 * The rule's answer for each of today's answers, in order. Every one today allowed counts as a new
 * piece for the next (owing its full cost), so two answers in one call never share the same room.
 * `previous` is the pressure state kept in memory; the last one comes back for the next call.
 */
export function observeSlotAnswers(
  rule: CapacityRule,
  input: {
    readonly facts: CapacityFacts | null;
    readonly today: ReadonlyArray<SlotAnswer>;
    readonly previous: PressureState;
    readonly nowMs: number;
  },
): { readonly decisions: ReadonlyArray<CapacityDecision>; readonly pressure: PressureState } {
  const kind = rule.unknownKinds.other;
  const facts = input.facts ?? NO_FACTS;
  // A copy that grows by each allow; `null` (unread) stays unread.
  const pieces: Array<CapacityPiece> | null = Array.isArray(facts.pieces)
    ? [...facts.pieces]
    : null;
  let pressure = input.previous;
  const decisions: Array<CapacityDecision> = [];
  for (const today of input.today) {
    const decision = capacityDecision(rule, {
      facts: { ...facts, pieces: pieces === null ? null : [...pieces] },
      ask: { kind },
      previous: pressure,
      nowMs: input.nowMs,
    });
    decisions.push(decision);
    pressure = decision.pressure;
    if (today === "allow" && pieces !== null) pieces.push({ kind, demandMb: null });
  }
  return { decisions, pressure };
}

const wholeMb = (mb: number | null) => (mb === null ? null : Math.round(mb));
const countMb = (mb: number | null) => (mb === null ? null : Math.max(0, Math.round(mb)));

/** One observation for `wait_list` and the log. */
export function slotRuleObservation(input: {
  readonly at: string;
  readonly today: SlotAnswer;
  readonly decision: CapacityDecision;
  readonly pieces: number | null;
  readonly readings: SlotReadings;
}): SlotRuleObservation {
  const { decision } = input;
  return {
    at: input.at,
    today: input.today,
    rule: decision.admit ? "allow" : "refuse",
    reason: decision.reason,
    level: decision.level,
    freeMb: wholeMb(decision.freeMb),
    marginMb: countMb(decision.marginMb),
    owedMb: countMb(decision.owedMb),
    costMb: Math.max(0, Math.round(decision.costMb)),
    pieces: input.pieces,
    readings: input.readings,
  };
}

export const LIGHT_KIND = "agent-light";
export const WRITER_KIND = "agent-writer";

/**
 * The capacity kind of a delegated agent, from upstream's `delegate_task` role: `research`, `review`
 * and `design` are light; any other role, or none, is a writer (the rule's default). The caller
 * names the role, so it may lie: a light agent that grows past a light cost counts as a writer
 * (`runningKind`), and what it grew is out of the available memory read anyway.
 */
export const kindOfRole = (role: string | undefined): string =>
  role === "research" || role === "review" || role === "design" ? LIGHT_KIND : WRITER_KIND;

/**
 * The start cost of an agent of either kind until costs are learned: measured idle agents use
 * about 300 MB (209-322 MB; p95 x 1.15, rounded up to 128 MB). Passed as a learned cost, so the
 * rule's own bounds (half to four times its start cost) still hold.
 */
const MEASURED_AGENT_MB = 384;
/** Swap with less free than this (percent of swap) trips the fallback (earlyoom's own line). */
const SWAP_FULL_FREE_PERCENT = 10;
/** Whole readings with no trip for this long end the fallback. */
export const FALLBACK_CALM_MS = 10 * 60 * 1000;
/** Each trip raises the agent costs by this much (design section 4: 25 % per kill). */
const FALLBACK_RAISE_PERCENT = 125;
/** The highest derived agent ceiling (design section 4: min(3 x cores, 64)). */
export const MAX_AGENT_CEILING = 64;

/**
 * The automatic fallback to the fixed ceilings (owner decision: lift at once, fall back after an
 * OOM kill or a swap-full reading). Memory only: a restart starts in it (`FALLBACK_START`).
 */
export interface FallbackState {
  readonly on: boolean;
  /** Since when every reading was whole and calm; `null` while not (or not read yet). */
  readonly calmSinceMs: number | null;
  /** The kernel's OOM kill counter last read; `null`: none read yet. */
  readonly oomKills: number | null;
  /** The last reading tripped (one swap-full spell raises the costs once). */
  readonly tripping: boolean;
  /** Trips since the start: each raised the agent costs once. */
  readonly trips: number;
}

export const FALLBACK_START: FallbackState = {
  on: true,
  calmSinceMs: null,
  oomKills: null,
  tripping: false,
  trips: 0,
};

export interface FallbackReading {
  /** The kernel's OOM kill counter (`/proc/vmstat`); `null` on a system without one. */
  readonly oomKills: number | null | "unreadable";
  readonly swap: CapacityFacts["swap"];
  readonly nowMs: number;
}

/**
 * One reading of the fallback. A new OOM kill, or swap below `SWAP_FULL_FREE_PERCENT` free, trips
 * it (on, costs raised once per spell); `FALLBACK_CALM_MS` of whole, calm readings end it. An
 * unreadable reading starts the calm wait again, a clock that went back too; a counter that went
 * down (another boot) is the new baseline.
 */
export function fallbackStep(
  state: FallbackState,
  reading: FallbackReading,
): { readonly state: FallbackState; readonly event: "trip" | "recover" | null } {
  const { oomKills: kills, swap, nowMs } = reading;
  const oomKills = typeof kills === "number" ? kills : state.oomKills;
  const killed = typeof kills === "number" && state.oomKills !== null && kills > state.oomKills;
  const swapFull =
    typeof swap === "object" &&
    swap !== null &&
    swap.totalMb > 0 &&
    swap.freeMb * 100 < swap.totalMb * SWAP_FULL_FREE_PERCENT;
  if (killed || swapFull) {
    const news = killed || !state.tripping;
    const trips = state.trips + (news ? 1 : 0);
    return {
      state: { on: true, calmSinceMs: null, oomKills, tripping: true, trips },
      event: news ? "trip" : null,
    };
  }
  if (kills === "unreadable" || swap === "unreadable") {
    return { state: { ...state, oomKills, calmSinceMs: null }, event: null };
  }
  const since = state.calmSinceMs === null || nowMs < state.calmSinceMs ? nowMs : state.calmSinceMs;
  const recover = state.on && nowMs - since >= FALLBACK_CALM_MS;
  return {
    state: {
      on: state.on && !recover,
      calmSinceMs: since,
      oomKills,
      tripping: false,
      trips: state.trips,
    },
    event: recover ? "recover" : null,
  };
}

/** The agent costs after `trips` trips: the measured cost, raised 25 % per trip (rule-bounded). */
export function enforceCosts(trips: number): CapacityLearned {
  const mb = Math.ceil(MEASURED_AGENT_MB * (FALLBACK_RAISE_PERCENT / 100) ** Math.min(trips, 20));
  return { [LIGHT_KIND]: { mb }, [WRITER_KIND]: { mb } };
}

/**
 * Derived: agents min(3 x cores, 64). In fallback the rule's fixed ceilings (10 agents, 4 jobs),
 * never above the derived one. Cores that cannot be read keep the fixed ceiling.
 */
export function enforceCeilings(
  rule: CapacityRule,
  cpuCores: unknown,
  fallback: boolean,
): CapacityPerGroup {
  const derived =
    typeof cpuCores === "number" && cpuCores >= 1
      ? Math.min(Math.floor(3 * cpuCores), MAX_AGENT_CEILING)
      : rule.ceilings.agent;
  return {
    agent: fallback ? Math.min(rule.ceilings.agent, derived) : derived,
    job: rule.ceilings.job,
  };
}

/** A light agent that uses more than a light agent costs is a writer (a kind lie, or it grew). */
export function runningKind(
  kind: string,
  demandMb: number | null,
  learned: CapacityLearned,
): string {
  if (kind !== LIGHT_KIND || demandMb === null) return kind;
  return demandMb > (learned[LIGHT_KIND]?.mb ?? MEASURED_AGENT_MB) ? WRITER_KIND : kind;
}

export interface EnforceInput {
  /** The machine as read for this decision (its `pieces` are set here); `null`: unread. */
  readonly facts: CapacityFacts | null;
  /** Agents that show a run: their kind and memory demand now (`null`: unread, owes in full). */
  readonly running: ReadonlyArray<{ readonly kind: string; readonly demandMb: number | null }>;
  /**
   * Kinds of the agents allowed but not running yet (starts, held slots, wake-ups, waiters ahead):
   * each owes its full cost from the second it was allowed (the owed term).
   */
  readonly starting: ReadonlyArray<string>;
  readonly ask: string;
  readonly fallback: FallbackState;
  readonly previous: PressureState;
  readonly nowMs: number;
}

/** The rule's answer for one more agent of kind `ask` (lifted or fallback ceilings, floors). */
export function enforceDecision(rule: CapacityRule, input: EnforceInput): CapacityDecision {
  const learned = enforceCosts(input.fallback.trips);
  const pieces: Array<CapacityPiece> = [
    ...input.running.map((piece) => ({
      kind: runningKind(piece.kind, piece.demandMb, learned),
      demandMb: piece.demandMb,
    })),
    ...input.starting.map((kind) => ({ kind, demandMb: null })),
  ];
  const facts = input.facts;
  return capacityDecision(
    { ...rule, ceilings: enforceCeilings(rule, facts?.cpuCores, input.fallback.on) },
    {
      facts: facts === null ? NO_FACTS : { ...facts, pieces },
      ask: { kind: input.ask },
      learned,
      previous: input.previous,
      nowMs: input.nowMs,
    },
  );
}
