/**
 * Fork: the capacity rule beside the agent slot gate (`agentSlots.mode: "observe"`). Pure: the gate
 * (`apps/server/src/slots/SlotObserver.ts`) passes the facts it read and today's answers; nothing
 * here changes an answer. A later mode lets the rule decide; it reuses `observeSlotAnswers`.
 */
import type { SlotRuleObservation } from "@t3tools/contracts/waitFor";

import {
  capacityDecision,
  type CapacityDecision,
  type CapacityFacts,
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
