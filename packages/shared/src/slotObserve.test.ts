import { describe, expect, it } from "vite-plus/test";

import { CAPACITY_RULE, type CapacityFacts } from "./capacityRule.ts";
import { OBSERVE_START_PRESSURE, observeSlotAnswers, slotRuleObservation } from "./slotObserve.ts";

const other = CAPACITY_RULE.unknownKinds.other;
const cost = CAPACITY_RULE.kinds[other]!.startMb;

/** A calm 16 GB machine with two agents running (past the floor of two). */
const calm = {
  pieceCaps: false,
  totalMb: 16_384,
  availableMb: 12_288,
  cpuCores: 8,
  swap: { totalMb: 4096, freeMb: 4096 },
  psi: { someAvg10Pct: 0, fullAvg10Pct: 0 },
  pieces: [
    { kind: other, demandMb: null },
    { kind: other, demandMb: null },
  ],
} satisfies CapacityFacts;

const calmFor = { level: "ok", calmSinceMs: null } as const;

describe("observeSlotAnswers", () => {
  it("counts each allow of the same call as a new piece for the next", () => {
    const { decisions } = observeSlotAnswers(CAPACITY_RULE, {
      facts: calm,
      today: ["allow", "refuse", "allow"],
      previous: calmFor,
      nowMs: 0,
    });
    expect(decisions.map((decision) => decision.owedMb)).toEqual([2 * cost, 3 * cost, 3 * cost]);
  });

  it("starts elevated and needs calm before the rule says ok", () => {
    const first = observeSlotAnswers(CAPACITY_RULE, {
      facts: calm,
      today: ["allow"],
      previous: OBSERVE_START_PRESSURE,
      nowMs: 1_000,
    });
    expect(first.decisions[0]).toMatchObject({ admit: false, reason: "elevated" });
    const later = observeSlotAnswers(CAPACITY_RULE, {
      facts: calm,
      today: ["allow"],
      previous: first.pressure,
      nowMs: 1_000 + CAPACITY_RULE.pressure.elevatedClearSeconds * 1000,
    });
    expect(later.decisions[0]).toMatchObject({ admit: true, reason: "fits", level: "ok" });
  });

  it("nothing read is unreadable, never fits, and the level stays at least elevated", () => {
    const { decisions, pressure } = observeSlotAnswers(CAPACITY_RULE, {
      facts: null,
      today: ["allow", "allow"],
      previous: calmFor,
      nowMs: 0,
    });
    expect(decisions.map((decision) => [decision.admit, decision.reason])).toEqual([
      [false, "unreadable"],
      [false, "unreadable"],
    ]);
    expect(pressure.level).toBe("elevated");
  });

  it("rounds the numbers for the record and never shows a negative count", () => {
    const [decision] = observeSlotAnswers(CAPACITY_RULE, {
      facts: { ...calm, availableMb: 100.4 },
      today: ["refuse"],
      previous: calmFor,
      nowMs: 0,
    }).decisions;
    const observation = slotRuleObservation({
      at: "2026-11-17T00:00:00.000Z",
      today: "refuse",
      decision: decision!,
      pieces: 2,
      readings: "ok",
    });
    expect(Number.isInteger(observation.freeMb)).toBe(true);
    expect(observation.freeMb).toBeLessThan(0);
    expect(observation).toMatchObject({ rule: "refuse", today: "refuse", pieces: 2 });
  });
});
