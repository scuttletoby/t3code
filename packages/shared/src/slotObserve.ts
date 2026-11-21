/**
 * Fork: the capacity rule beside the agent slot gate (`agentSlots.mode: "observe"`), and deciding
 * in its place (`enforce`, the default). Pure: the gate (`apps/server/src/slots/SlotGate.ts`,
 * `SlotObserver.ts`) passes the facts it read; nothing here reads a clock or the machine.
 */
import type { SlotRuleObservation } from "@t3tools/contracts/waitFor";

import {
  capacityDecision,
  type CapacityDecision,
  capacityLevel,
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
 * Capacity kind from upstream's `delegate_task` role: research, review and design are light, any
 * other role or none is a writer. The role may lie: a light agent that grows past the light cost,
 * or whose memory cannot be read, counts as a writer (`runningKind`).
 */
export const kindOfRole = (role: string | undefined): string =>
  role === "research" || role === "review" || role === "design" ? LIGHT_KIND : WRITER_KIND;

/** Swap with less free than this (percent of swap) that is also filling trips the fallback. */
const SWAP_FULL_FREE_PERCENT = 10;
/** Whole, calm readings for this long end the fallback, and then lower one cost raise each. */
export const FALLBACK_CALM_MS = 10 * 60 * 1000;
/**
 * And at least this many of them, each `FALLBACK_READING_GAP_MS` after the last one counted, so a
 * clock that jumps forward (suspend and resume, an NTP step) never ends the wait in one reading.
 */
export const FALLBACK_CALM_READINGS = 10;
export const FALLBACK_READING_GAP_MS = 25 * 1000;
/** Each kill in the agents' own slices raises the agent costs by this much (design: 25 % per kill). */
const RAISE_PERCENT = 125;
/**
 * Kills that cannot be told apart (no agent slice to read) raise the costs at most this many times:
 * a build box hitting its own cap there raises them by about half at most, and that falls again.
 */
export const UNTOLD_MAX_RAISES = 2;
/** The highest derived agent ceiling (design section 4: min(3 x cores, 64)). */
export const MAX_AGENT_CEILING = 64;
/**
 * Readings that fail for this long (no whole reading since): the gate decides by count alone, up to
 * the rule's fixed ceilings (10 agents), until a whole reading comes back. Never a wait without end.
 */
export const UNREADABLE_FIXED_MS = 2 * 60 * 1000;

/** Raises past this change nothing: the rule clamps a cost at `learnedBounds.maxPercent` anyway. */
export const maxRaises = (rule: CapacityRule): number =>
  Math.ceil(Math.log(rule.learnedBounds.maxPercent / 100) / Math.log(RAISE_PERCENT / 100));

/**
 * The automatic fallback to the fixed ceilings (owner decision: lift at once, fall back after an
 * OOM kill or swap that fills up). Memory only: a restart starts in it (`FALLBACK_START`).
 */
export interface FallbackState {
  readonly on: boolean;
  /** Since when every reading was whole and calm; `null` while not (or not read yet). */
  readonly calmSinceMs: number | null;
  /** Calm readings counted since then (at least `FALLBACK_READING_GAP_MS` apart). */
  readonly calmReadings: number;
  /** When the last calm reading was counted. */
  readonly calmCountedMs: number | null;
  /** The kernel's OOM kill counter last read (`/proc/vmstat`); `null`: none read yet. */
  readonly oomKills: number | null;
  /** The OOM kills of each agents' slice last read (`memory.events`), by cgroup path. */
  readonly agentKills: Readonly<Record<string, number>>;
  /** The swap free of the last reading (MB), for its trend. */
  readonly swapFreeMb: number | null;
  /** The last reading tripped (one spell logs one trip). */
  readonly tripping: boolean;
  /** Agent cost raises in force: one per kill that matters, one less per calm spell. */
  readonly raises: number;
}

export const FALLBACK_START: FallbackState = {
  on: true,
  calmSinceMs: null,
  calmReadings: 0,
  calmCountedMs: null,
  oomKills: null,
  agentKills: {},
  swapFreeMb: null,
  tripping: false,
  raises: 0,
};

export interface FallbackReading {
  /** The kernel's OOM kill counter (`/proc/vmstat`); `null` on a system without one. */
  readonly oomKills: number | null | "unreadable";
  /**
   * The OOM kills of each agents' slice read now (`memory.events`, which counts the kills of its
   * processes by any OOM killer); `null`: no agent slice could be read, so a kill cannot be told
   * apart from one in a build box.
   */
  readonly agentKills: Readonly<Record<string, number>> | null;
  readonly swap: CapacityFacts["swap"];
  readonly nowMs: number;
}

export type FallbackEvent = "trip" | "recover" | "lower" | null;

/**
 * One fallback reading: a new OOM kill or swap nearly full and filling trips it. Only an agent-slice
 * kill raises costs (an untold kill at most `UNTOLD_MAX_RAISES`). Each calm spell (time and readings)
 * ends it and lowers one raise; an unreadable reading or a clock going back restarts the wait.
 */
export function fallbackStep(
  rule: CapacityRule,
  state: FallbackState,
  reading: FallbackReading,
): { readonly state: FallbackState; readonly event: FallbackEvent } {
  const { oomKills: kills, swap, nowMs } = reading;
  const oomKills = typeof kills === "number" ? kills : state.oomKills;
  const killed = typeof kills === "number" && state.oomKills !== null && kills > state.oomKills;
  const agentKills = reading.agentKills ?? state.agentKills;
  const agentKilled = Object.entries(agentKills).some(([slice, count]) => {
    const before = state.agentKills[slice];
    return before !== undefined && count > before;
  });
  const swapRead = typeof swap === "object" && swap !== null && swap.totalMb > 0 ? swap : null;
  const swapFreeMb = swapRead === null ? null : swapRead.freeMb;
  const swapFull =
    swapRead !== null &&
    state.swapFreeMb !== null &&
    swapRead.freeMb * 100 < swapRead.totalMb * SWAP_FULL_FREE_PERCENT &&
    state.swapFreeMb - swapRead.freeMb > rule.pressure.swapFillingMb;
  const base = { oomKills, agentKills, swapFreeMb };
  if (killed || agentKilled || swapFull) {
    const raised = agentKilled
      ? state.raises + 1
      : killed && reading.agentKills === null && state.raises < UNTOLD_MAX_RAISES
        ? state.raises + 1
        : state.raises;
    const news = killed || agentKilled || !state.tripping;
    return {
      state: {
        ...base,
        on: true,
        calmSinceMs: null,
        calmReadings: 0,
        calmCountedMs: null,
        tripping: true,
        raises: Math.min(raised, maxRaises(rule)),
      },
      event: news ? "trip" : null,
    };
  }
  if (kills === "unreadable" || swap === "unreadable") {
    return {
      state: { ...state, ...base, calmSinceMs: null, calmReadings: 0, calmCountedMs: null },
      event: null,
    };
  }
  const restart = state.calmSinceMs === null || nowMs < state.calmSinceMs;
  const since = restart ? nowMs : state.calmSinceMs!;
  const counts =
    restart ||
    state.calmCountedMs === null ||
    nowMs - state.calmCountedMs >= FALLBACK_READING_GAP_MS;
  const calmReadings = restart ? 1 : state.calmReadings + (counts ? 1 : 0);
  const calmCountedMs = counts ? nowMs : state.calmCountedMs;
  const done =
    nowMs - since >= FALLBACK_CALM_MS &&
    calmReadings >= FALLBACK_CALM_READINGS &&
    (state.on || state.raises > 0);
  if (done) {
    return {
      state: {
        ...base,
        on: false,
        calmSinceMs: nowMs,
        calmReadings: 1,
        calmCountedMs: nowMs,
        tripping: false,
        raises: Math.max(0, state.raises - 1),
      },
      event: state.on ? "recover" : "lower",
    };
  }
  return {
    state: { ...state, ...base, calmSinceMs: since, calmReadings, calmCountedMs, tripping: false },
    event: null,
  };
}

/**
 * The agent costs after `raises` raises: the rule file's start cost of each kind, raised 25 % per
 * raise (the rule clamps them at its `learnedBounds`). No cost lives in code.
 */
export function enforceCosts(rule: CapacityRule, raises: number): CapacityLearned {
  const factor = (RAISE_PERCENT / 100) ** Math.min(Math.max(0, raises), maxRaises(rule));
  const raised = (kind: string) => ({ mb: Math.ceil(rule.kinds[kind]!.startMb * factor) });
  return { [LIGHT_KIND]: raised(LIGHT_KIND), [WRITER_KIND]: raised(WRITER_KIND) };
}

/**
 * Where the gate can see what it needs to lift the ceilings: `full` is Linux with cgroup v2, agents
 * in their own scopes and the kernel's OOM kill counter. Anywhere else (`basic`: macOS, Windows,
 * Linux without systemd or cgroup v2) the rule's fixed ceilings always apply (design: "ceilings stay
 * at today's numbers").
 */
export type EnforceTier = "full" | "basic";

/**
 * Derived: agents min(3 x cores, 64) on the full tier out of the fallback. In the fallback, on the
 * basic tier and when the cores cannot be read: the rule's fixed ceilings (10 agents, 4 jobs), never
 * above the derived one.
 */
export function enforceCeilings(
  rule: CapacityRule,
  cpuCores: unknown,
  fallback: boolean,
  tier: EnforceTier,
): CapacityPerGroup {
  const derived =
    typeof cpuCores === "number" && cpuCores >= 1
      ? Math.min(Math.floor(3 * cpuCores), MAX_AGENT_CEILING)
      : rule.ceilings.agent;
  return {
    agent: fallback || tier === "basic" ? Math.min(rule.ceilings.agent, derived) : derived,
    job: rule.ceilings.job,
  };
}

/**
 * The kind a running agent counts as. Its memory unread: a writer whatever its role (growth that
 * cannot be seen is never cheap). A light agent that uses more than a light agent costs (clamped as
 * the rule clamps that cost) is a writer: a kind lie, or it grew.
 */
export function runningKind(
  rule: CapacityRule,
  kind: string,
  demandMb: number | null,
  learned: CapacityLearned,
): string {
  if (demandMb === null) return WRITER_KIND;
  if (kind !== LIGHT_KIND) return kind;
  const start = rule.kinds[LIGHT_KIND]!.startMb;
  const light = Math.min(
    learned[LIGHT_KIND]?.mb ?? start,
    Math.floor((start * rule.learnedBounds.maxPercent) / 100),
  );
  return demandMb > light ? WRITER_KIND : kind;
}

/** The gate's rule state, memory only; a restart starts at `ENFORCE_START`. */
export interface EnforceState {
  /**
   * After a restart the level starts "elevated" and waits for calm readings (design section 6): the
   * rule never says "ok" before it has seen the machine calm for a while.
   */
  readonly pressure: PressureState;
  readonly fallback: FallbackState;
  /** Since when no reading was whole; `null` while they are. */
  readonly unreadSinceMs: number | null;
  /** The level was "ok" once since the start: before, an "elevated" refusal says it just started. */
  readonly settled: boolean;
}

export const ENFORCE_START: EnforceState = {
  pressure: OBSERVE_START_PRESSURE,
  fallback: FALLBACK_START,
  unreadSinceMs: null,
  settled: false,
};

/**
 * The rule a level steps by. Until the level was "ok" once since the start, calm needs available
 * memory only above the margin (not the 150 % that guards a step down from real pressure): the
 * start's "elevated" is a wait, not pressure seen, so a host that sits just above its margin with
 * no stall lifts too. PSI and the swap trend still have to be calm.
 */
function levelRule(rule: CapacityRule, settled: boolean): CapacityRule {
  if (settled) return rule;
  const { pressure } = rule;
  return {
    ...rule,
    pressure: {
      ...pressure,
      calm: {
        ...pressure.calm,
        availableAtLeastMarginPercent: pressure.elevated.availableBelowMarginPercent,
      },
    },
  };
}

export interface EnforceReading extends Omit<FallbackReading, "swap"> {
  /** The machine as read now (pieces not needed here); `null`: nothing could be read. */
  readonly facts: CapacityFacts | null;
}

/**
 * One reading of the machine (a decision's, or the gate's timer): steps the level, the fallback and
 * the unread time. `previous` is the level before it, for the decisions on this same reading (the
 * rule steps the level from it again, to the same answer, so a reading is never its own trend).
 */
export function enforceStep(
  rule: CapacityRule,
  state: EnforceState,
  reading: EnforceReading,
): {
  readonly state: EnforceState;
  readonly previous: PressureState;
  /** Whether the level was "ok" once before this reading (`levelRule`). */
  readonly settledBefore: boolean;
  readonly event: FallbackEvent;
} {
  const level = capacityLevel(levelRule(rule, state.settled), {
    // The machine alone: whether it reads whole, and its level (the pieces are the decision's).
    facts: reading.facts === null ? NO_FACTS : { ...reading.facts, pieces: [] },
    previous: state.pressure,
    nowMs: reading.nowMs,
  });
  const whole = level.marginMb !== null;
  const step = fallbackStep(rule, state.fallback, {
    oomKills: reading.oomKills,
    agentKills: reading.agentKills,
    swap: whole ? reading.facts!.swap : "unreadable",
    nowMs: reading.nowMs,
  });
  return {
    state: {
      pressure: level.pressure,
      fallback: step.state,
      unreadSinceMs: whole ? null : (state.unreadSinceMs ?? reading.nowMs),
      settled: state.settled || level.level === "ok",
    },
    previous: state.pressure,
    settledBefore: state.settled,
    event: step.event,
  };
}

/** Readings failed for `UNREADABLE_FIXED_MS`: decide by count alone (fixed ceilings). */
export const unreadFixed = (state: EnforceState, nowMs: number): boolean =>
  state.unreadSinceMs !== null && nowMs - state.unreadSinceMs >= UNREADABLE_FIXED_MS;

export interface EnforceInput {
  /** The machine as read for this decision (its `pieces` are set here); `null`: unread. */
  readonly facts: CapacityFacts | null;
  readonly tier: EnforceTier;
  /** Agents that show a run: their kind and memory demand now (`null`: unread). */
  readonly running: ReadonlyArray<{ readonly kind: string; readonly demandMb: number | null }>;
  /**
   * Kinds of the agents allowed but not running yet (starts, held slots, wake-ups, waiters ahead):
   * each owes its full cost from the second it was allowed (the owed term).
   */
  readonly starting: ReadonlyArray<string>;
  readonly ask: string;
  /** The state after this reading (`enforceStep`). */
  readonly state: EnforceState;
  /** The level before this reading (`enforceStep`'s `previous`). */
  readonly previous: PressureState;
  /** `enforceStep`'s `settledBefore`. */
  readonly settledBefore: boolean;
  readonly nowMs: number;
}

/**
 * The rule's answer for one more agent of kind `ask`. An unread running agent already uses a light
 * start (counted once) and owes the rest of a writer's. With no readings the floor still starts, and
 * after `UNREADABLE_FIXED_MS` the fixed ceiling counts alone (an admit with reason `unreadable`).
 */
export function enforceDecision(rule: CapacityRule, input: EnforceInput): CapacityDecision {
  const learned = enforceCosts(rule, input.state.fallback.raises);
  const inUseMb = rule.kinds[LIGHT_KIND]!.startMb;
  const pieces: Array<CapacityPiece> = [
    ...input.running.map((piece) => ({
      kind: runningKind(rule, piece.kind, piece.demandMb, learned),
      demandMb: piece.demandMb ?? inUseMb,
    })),
    ...input.starting.map((kind) => ({ kind, demandMb: null })),
  ];
  const facts = input.facts;
  const ceilings = enforceCeilings(rule, facts?.cpuCores, input.state.fallback.on, input.tier);
  const decision = capacityDecision(
    { ...levelRule(rule, input.settledBefore), ceilings },
    {
      facts: facts === null ? NO_FACTS : { ...facts, pieces },
      ask: { kind: input.ask },
      learned,
      previous: input.previous,
      nowMs: input.nowMs,
    },
  );
  if (decision.reason !== "unreadable") return decision;
  // Nothing whole to read: counted agents only, against the floor and then the fixed ceiling.
  const counted = pieces.length;
  if (counted < rule.floors.agent) return { ...decision, admit: true, reason: "floor" };
  if (!unreadFixed(input.state, input.nowMs)) return decision;
  return counted < rule.ceilings.agent
    ? { ...decision, admit: true }
    : { ...decision, reason: "ceiling" };
}
