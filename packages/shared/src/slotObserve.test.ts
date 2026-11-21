import { describe, expect, it } from "vite-plus/test";

import { CAPACITY_RULE, type CapacityFacts } from "./capacityRule.ts";
import {
  enforceCeilings,
  enforceCosts,
  enforceDecision,
  type EnforceInput,
  FALLBACK_CALM_MS,
  FALLBACK_START,
  type FallbackReading,
  type FallbackState,
  fallbackStep,
  kindOfRole,
  LIGHT_KIND,
  MAX_AGENT_CEILING,
  OBSERVE_START_PRESSURE,
  observeSlotAnswers,
  runningKind,
  slotRuleObservation,
  WRITER_KIND,
} from "./slotObserve.ts";

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

describe("enforce: kinds from roles", () => {
  it("research, review and design are light; any other role, or none, is a writer", () => {
    for (const role of ["research", "review", "design"]) expect(kindOfRole(role)).toBe(LIGHT_KIND);
    for (const role of ["implementation", "test", "general", "", "REVIEW", undefined]) {
      expect(kindOfRole(role)).toBe(WRITER_KIND);
    }
  });
});

describe("enforce: the fallback to the fixed ceilings", () => {
  const swap = { totalMb: 4096, freeMb: 4096 };
  const read = (nowMs: number, patch: Partial<FallbackReading> = {}): FallbackReading => ({
    oomKills: 3,
    swap,
    nowMs,
    ...patch,
  });

  it("a restart starts in it; calm readings end it after the calm wait, not before", () => {
    const first = fallbackStep(FALLBACK_START, read(1_000));
    // The first reading is the baseline: kills from before the restart never trip it.
    expect(first).toEqual({
      state: { on: true, calmSinceMs: 1_000, oomKills: 3, tripping: false, trips: 0 },
      event: null,
    });
    const early = fallbackStep(first.state, read(1_000 + FALLBACK_CALM_MS - 1));
    expect(early.state.on).toBe(true);
    const calm = fallbackStep(early.state, read(1_000 + FALLBACK_CALM_MS));
    expect(calm).toMatchObject({ state: { on: false, trips: 0 }, event: "recover" });
    // Runs again: calm stays off, with no second event.
    expect(fallbackStep(calm.state, read(1_000 + 2 * FALLBACK_CALM_MS)).event).toBeNull();
  });

  const lifted: FallbackState = {
    on: false,
    calmSinceMs: 0,
    oomKills: 3,
    tripping: false,
    trips: 0,
  };

  it("a new OOM kill trips it and raises the costs once per kill", () => {
    const killed = fallbackStep(lifted, read(5_000, { oomKills: 4 }));
    expect(killed).toMatchObject({ state: { on: true, trips: 1, oomKills: 4 }, event: "trip" });
    expect(enforceCosts(killed.state.trips)[WRITER_KIND]?.mb).toBe(480);
    const again = fallbackStep(killed.state, read(6_000, { oomKills: 5 }));
    expect(again).toMatchObject({ state: { trips: 2 }, event: "trip" });
    // The same count again is no new kill: the calm wait starts.
    const calm = fallbackStep(again.state, read(7_000, { oomKills: 5 }));
    expect(calm).toMatchObject({ state: { on: true, trips: 2, calmSinceMs: 7_000 }, event: null });
  });

  it("swap below 10 % free trips it once per spell; it recovers after the calm wait", () => {
    const full = { totalMb: 4096, freeMb: 400 };
    const tripped = fallbackStep(lifted, read(1_000, { swap: full }));
    expect(tripped).toMatchObject({ state: { on: true, trips: 1 }, event: "trip" });
    const still = fallbackStep(tripped.state, read(2_000, { swap: full }));
    expect(still).toMatchObject({ state: { trips: 1, calmSinceMs: null }, event: null });
    const freed = fallbackStep(still.state, read(3_000));
    expect(fallbackStep(freed.state, read(3_000 + FALLBACK_CALM_MS)).event).toBe("recover");
    // 10 % free exactly is not full; no swap at all is not full.
    expect(fallbackStep(lifted, read(1_000, { swap: { totalMb: 4000, freeMb: 400 } })).event).toBe(
      null,
    );
    expect(fallbackStep(lifted, read(1_000, { swap: null })).state.on).toBe(false);
  });

  it("an unreadable reading never counts as calm; a clock that went back starts the wait again", () => {
    const tripped = fallbackStep(lifted, read(0, { oomKills: 4 })).state;
    const calm = fallbackStep(tripped, read(1_000, { oomKills: 4 })).state;
    const blind = fallbackStep(calm, read(2_000, { oomKills: "unreadable" }));
    expect(blind.state).toMatchObject({ on: true, calmSinceMs: null, oomKills: 4 });
    expect(fallbackStep(calm, read(2_000, { swap: "unreadable" })).state.calmSinceMs).toBeNull();
    // Unreadable for longer than the calm wait still ends nothing.
    expect(fallbackStep(blind.state, read(2_000 + FALLBACK_CALM_MS)).event).toBeNull();
    const back = fallbackStep(calm, read(500));
    expect(back.state.calmSinceMs).toBe(500);
    expect(fallbackStep(back.state, read(500 + FALLBACK_CALM_MS - 1)).event).toBeNull();
  });

  it("a counter that went down (a reboot) is the new baseline, not a kill", () => {
    const down = fallbackStep(lifted, read(1_000, { oomKills: 0 }));
    expect(down).toMatchObject({ state: { on: false, oomKills: 0 }, event: null });
    expect(fallbackStep(down.state, read(2_000, { oomKills: 1 })).event).toBe("trip");
    // A machine without the counter (`null`) trips on swap only.
    expect(fallbackStep(FALLBACK_START, read(1_000, { oomKills: null })).state.oomKills).toBeNull();
  });
});

describe("enforce: ceilings and floors", () => {
  it("lifts to min(3 x cores, 64); the fallback keeps the fixed 10, never above derived", () => {
    expect(enforceCeilings(CAPACITY_RULE, 4, false)).toEqual({ agent: 12, job: 4 });
    expect(enforceCeilings(CAPACITY_RULE, 32, false).agent).toBe(MAX_AGENT_CEILING);
    expect(enforceCeilings(CAPACITY_RULE, 12, true).agent).toBe(10);
    expect(enforceCeilings(CAPACITY_RULE, 2, true).agent).toBe(6);
    for (const cores of [null, undefined, 0, -1, "8", Number.NaN]) {
      expect(enforceCeilings(CAPACITY_RULE, cores, false).agent).toBe(10);
    }
  });

  const machine = (patch: Partial<CapacityFacts> = {}): CapacityFacts => ({
    pieceCaps: true,
    totalMb: 16_384,
    availableMb: 12_288,
    cpuCores: 8,
    swap: { totalMb: 4096, freeMb: 4096 },
    psi: { someAvg10Pct: 0, fullAvg10Pct: 0 },
    ...patch,
  });
  const decide = (input: Partial<EnforceInput>) =>
    enforceDecision(CAPACITY_RULE, {
      facts: machine(),
      running: [],
      starting: [],
      ask: LIGHT_KIND,
      fallback: { ...FALLBACK_START, on: false },
      previous: calmFor,
      nowMs: 0,
      ...input,
    });
  const lights = (count: number, demandMb: number | null = 400) =>
    Array.from({ length: count }, () => ({ kind: LIGHT_KIND, demandMb }));

  it("two starts in one second: each start owes its full cost to the next decision", () => {
    // 16 GB with caps: margin 1639 MB. 900 MB free for new work: one more agent fits, not two.
    const facts = machine({ availableMb: 1639 + 900, totalMb: 16_384 });
    const one = decide({ facts, running: lights(2), starting: [LIGHT_KIND] });
    expect(one).toMatchObject({ admit: true, reason: "fits", owedMb: 384 });
    const two = decide({ facts, running: lights(2), starting: [LIGHT_KIND, LIGHT_KIND] });
    expect(two).toMatchObject({ admit: false, reason: "budget", owedMb: 768 });
    // A running agent that has not grown to its cost yet owes the rest.
    expect(decide({ facts, running: lights(2, 100) }).owedMb).toBe(2 * 284);
  });

  it("a kind lie: a light agent that grew past a light cost counts as a writer", () => {
    // 2 cores: room for no build, so two writers at most.
    const facts = machine({ cpuCores: 2 });
    const liar = { kind: LIGHT_KIND, demandMb: 1_500 };
    const writer = { kind: WRITER_KIND, demandMb: 800 };
    expect(decide({ facts, running: [writer, liar], ask: WRITER_KIND })).toMatchObject({
      admit: false,
      reason: "writers",
    });
    const honest = { kind: LIGHT_KIND, demandMb: 300 };
    expect(decide({ facts, running: [writer, honest], ask: WRITER_KIND }).admit).toBe(true);
    expect(runningKind(LIGHT_KIND, null, enforceCosts(0))).toBe(LIGHT_KIND);
  });

  it("lifted past the fixed ten; the fallback stops at ten", () => {
    const running = lights(10);
    expect(decide({ running }).admit).toBe(true);
    expect(decide({ running, fallback: FALLBACK_START })).toMatchObject({
      admit: false,
      reason: "ceiling",
    });
    // Starts count to the ceiling too.
    expect(decide({ running: lights(8), starting: [LIGHT_KIND, WRITER_KIND] }).admit).toBe(true);
    expect(
      decide({ running: lights(8), starting: [LIGHT_KIND, WRITER_KIND], fallback: FALLBACK_START })
        .reason,
    ).toBe("ceiling");
  });

  it("the floor never overloads a small machine (4 GB), counting starts too", () => {
    // 4 GB without caps or swap: margin 1536; the floor needs 768 MB left after the start.
    const small = (availableMb: number) =>
      machine({ totalMb: 4096, availableMb, cpuCores: 2, pieceCaps: false, swap: null });
    expect(decide({ facts: small(1_100) })).toMatchObject({ admit: false, reason: "elevated" });
    expect(decide({ facts: small(1_200) })).toMatchObject({ admit: true, reason: "floor" });
    // The second start in the same second: the first owes 384 MB, so the floor says no.
    expect(decide({ facts: small(1_200), starting: [LIGHT_KIND] }).admit).toBe(false);
    expect(decide({ facts: small(700) })).toMatchObject({ admit: false, reason: "severe" });
  });

  it("unreadable readings are elevated or unreadable, never ok, and admit nothing past the floor", () => {
    const none = decide({ facts: null });
    expect(none).toMatchObject({ admit: false, reason: "unreadable", level: "elevated" });
    // Corrupt readings: a broken total, an impossible swap, a broken PSI.
    for (const patch of [
      { totalMb: Number.NaN },
      { availableMb: 99_999 },
      { cpuCores: null },
    ] satisfies Array<Partial<CapacityFacts>>) {
      expect(decide({ facts: machine(patch), running: lights(0) }).reason).toBe("unreadable");
    }
    for (const patch of [
      { swap: "unreadable" },
      { swap: { totalMb: 10, freeMb: 20 } },
      { psi: "unreadable" },
    ] as Array<Partial<CapacityFacts>>) {
      const decision = decide({ facts: machine(patch), running: lights(2) });
      expect(decision.level).not.toBe("ok");
      expect(decision.admit).toBe(false);
    }
  });
});
