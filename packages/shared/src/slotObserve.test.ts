import { describe, expect, it } from "vite-plus/test";

import { CAPACITY_RULE, capacityLevel, type CapacityFacts } from "./capacityRule.ts";
import {
  ENFORCE_START,
  enforceCeilings,
  enforceCosts,
  enforceDecision,
  type EnforceInput,
  type EnforceReading,
  type EnforceState,
  enforceStep,
  FALLBACK_CALM_MS,
  FALLBACK_CALM_READINGS,
  FALLBACK_READING_GAP_MS,
  FALLBACK_START,
  type FallbackReading,
  type FallbackState,
  fallbackStep,
  kindOfRole,
  LIGHT_KIND,
  MAX_AGENT_CEILING,
  maxRaises,
  OBSERVE_START_PRESSURE,
  observeSlotAnswers,
  runningKind,
  slotRuleObservation,
  UNREADABLE_FIXED_MS,
  UNTOLD_MAX_RAISES,
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
    // The first read starts the calm wait (it learns the swap trend for the next read).
    const second = observeSlotAnswers(CAPACITY_RULE, {
      facts: calm,
      today: ["allow"],
      previous: first.pressure,
      nowMs: 2_000,
    });
    expect(second.pressure.calmSinceMs).toBe(1_000);
    const later = observeSlotAnswers(CAPACITY_RULE, {
      facts: calm,
      today: ["allow"],
      previous: second.pressure,
      nowMs: 2_000 + CAPACITY_RULE.pressure.elevatedClearSeconds * 1000,
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

const LIGHT_MB = CAPACITY_RULE.kinds[LIGHT_KIND]!.startMb;
const WRITER_MB = CAPACITY_RULE.kinds[WRITER_KIND]!.startMb;
/** One reading every 30 s from `fromMs` up to and with `toMs`; the last step and its state. */
function stepEvery<S, E>(
  state: S,
  fromMs: number,
  toMs: number,
  step: (state: S, nowMs: number) => { readonly state: S; readonly event: E },
): { readonly state: S; readonly events: ReadonlyArray<E> } {
  const events: Array<E> = [];
  for (let nowMs = fromMs; nowMs <= toMs; nowMs += 30_000) {
    const next = step(state, nowMs);
    state = next.state;
    events.push(next.event);
  }
  return { state, events };
}

describe("enforce: the fallback to the fixed ceilings", () => {
  const swap = { totalMb: 4096, freeMb: 4096 };
  const slice = "/user.slice/agents.slice";
  const read = (nowMs: number, patch: Partial<FallbackReading> = {}): FallbackReading => ({
    oomKills: 3,
    agentKills: { [slice]: 0 },
    swap,
    nowMs,
    ...patch,
  });
  const step = (state: FallbackState, reading: FallbackReading) =>
    fallbackStep(CAPACITY_RULE, state, reading);
  const calmFrom = (state: FallbackState, fromMs: number, patch: Partial<FallbackReading> = {}) =>
    stepEvery(state, fromMs, fromMs + FALLBACK_CALM_MS, (current, nowMs) =>
      step(current, read(nowMs, patch)),
    );

  it("a restart starts in it; ten minutes of calm readings end it, not before", () => {
    const first = step(FALLBACK_START, read(0));
    // The first reading is the baseline: kills from before the restart never trip it.
    expect(first).toMatchObject({
      state: { on: true, calmSinceMs: 0, oomKills: 3, raises: 0 },
      event: null,
    });
    const early = stepEvery(first.state, 30_000, FALLBACK_CALM_MS - 1, (state, nowMs) =>
      step(state, read(nowMs)),
    );
    expect(early.state.on).toBe(true);
    const calm = step(early.state, read(FALLBACK_CALM_MS));
    expect(calm).toMatchObject({ state: { on: false, raises: 0 }, event: "recover" });
    // Runs again: calm stays off, with no second event.
    expect(calmFrom(calm.state, FALLBACK_CALM_MS + 30_000).events.every((e) => e === null)).toBe(
      true,
    );
  });

  it("a clock that jumps forward never ends it in one reading", () => {
    const first = step(FALLBACK_START, read(0)).state;
    // Suspend and resume: the next reading is an hour later.
    const jumped = step(first, read(60 * 60 * 1000));
    expect(jumped.state.on).toBe(true);
    // Readings close together count as one: the wait needs readings spread over time.
    let state = jumped.state;
    for (let index = 1; index < 3 * FALLBACK_CALM_READINGS; index += 1) {
      state = step(state, read(60 * 60 * 1000 + index * 1_000)).state;
    }
    expect(state.on).toBe(true);
    const spread = stepEvery(
      state,
      60 * 60 * 1000 + FALLBACK_READING_GAP_MS,
      60 * 60 * 1000 + FALLBACK_CALM_READINGS * FALLBACK_READING_GAP_MS,
      (current, nowMs) => step(current, read(nowMs)),
    );
    expect(spread.events).toContain("recover");
  });

  const lifted: FallbackState = { ...FALLBACK_START, on: false, calmSinceMs: 0, oomKills: 3 };
  const withSlice: FallbackState = { ...lifted, agentKills: { [slice]: 0 } };

  it("a kill in the agents' slice trips it and raises the agent costs, one per kill", () => {
    const killed = step(withSlice, read(5_000, { oomKills: 4, agentKills: { [slice]: 1 } }));
    expect(killed).toMatchObject({ state: { on: true, raises: 1, oomKills: 4 }, event: "trip" });
    expect(enforceCosts(CAPACITY_RULE, killed.state.raises)[WRITER_KIND]?.mb).toBe(960);
    const again = step(killed.state, read(6_000, { oomKills: 5, agentKills: { [slice]: 2 } }));
    expect(again).toMatchObject({ state: { raises: 2 }, event: "trip" });
    // The same counts again are no new kill: the calm wait starts.
    const calm = step(again.state, read(7_000, { oomKills: 5, agentKills: { [slice]: 2 } }));
    expect(calm).toMatchObject({ state: { on: true, raises: 2, calmSinceMs: 7_000 }, event: null });
  });

  it("a kill outside the agents' slices (a build box at its own cap) raises no cost", () => {
    const box = step(withSlice, read(5_000, { oomKills: 4 }));
    // The fixed ceilings come back (the machine may be short), the agent costs stay.
    expect(box).toMatchObject({ state: { on: true, raises: 0 }, event: "trip" });
    // A slice seen for the first time is its own baseline, not a kill.
    const fresh = step(withSlice, read(5_000, { agentKills: { [slice]: 0, "/other.slice": 7 } }));
    expect(fresh).toMatchObject({ state: { on: false, raises: 0 }, event: null });
  });

  it("kills that cannot be told apart raise the costs, at most UNTOLD_MAX_RAISES times", () => {
    let state = lifted;
    for (let kill = 4; kill < 10; kill += 1) {
      state = step(state, read(kill * 1_000, { oomKills: kill, agentKills: null })).state;
    }
    expect(state).toMatchObject({ on: true, raises: UNTOLD_MAX_RAISES });
  });

  it("raised costs fall again after calm, one per calm spell, never only on a restart", () => {
    let state = withSlice;
    for (let kill = 1; kill <= 20; kill += 1) {
      state = step(state, read(kill, { oomKills: 3 + kill, agentKills: { [slice]: kill } })).state;
    }
    // Raises stop where the rule's clamp (4 x the start cost) takes over.
    expect(state.raises).toBe(maxRaises(CAPACITY_RULE));
    expect(enforceCosts(CAPACITY_RULE, state.raises)[LIGHT_KIND]?.mb).toBeGreaterThanOrEqual(
      4 * LIGHT_MB,
    );
    const kills = { oomKills: 23, agentKills: { [slice]: 20 } };
    const first = calmFrom(state, 1_000, kills);
    expect(first.events).toContain("recover");
    expect(first.state).toMatchObject({ on: false, raises: maxRaises(CAPACITY_RULE) - 1 });
    let calm = first.state;
    for (let spell = 1; spell <= maxRaises(CAPACITY_RULE); spell += 1) {
      calm = calmFrom(calm, 1_000 + spell * (FALLBACK_CALM_MS + 30_000), kills).state;
    }
    expect(calm.raises).toBe(0);
    expect(enforceCosts(CAPACITY_RULE, calm.raises)).toEqual({
      [LIGHT_KIND]: { mb: LIGHT_MB },
      [WRITER_KIND]: { mb: WRITER_MB },
    });
  });

  it("swap nearly full trips it only while it fills; full but steady cold swap lifts", () => {
    const nearlyFull = (freeMb: number) => ({ swap: { totalMb: 4096, freeMb } });
    const before = step(lifted, read(1_000, nearlyFull(400))).state;
    // Below 10 % free and filling by more than the rule's `swapFillingMb` (200 MB): a trip.
    const filling = step(before, read(2_000, nearlyFull(200)));
    expect(filling).toMatchObject({ state: { on: true, raises: 0 }, event: "trip" });
    // Old cold pages: swap 5 % free that does not move. The fallback lifts after the calm wait.
    const steady = calmFrom(filling.state, 3_000, nearlyFull(200));
    expect(steady.events).toContain("recover");
    // No swap at all is not full.
    expect(step(lifted, read(1_000, { swap: null })).state.on).toBe(false);
  });

  it("an unreadable reading never counts as calm; a clock that went back starts the wait again", () => {
    const tripped = step(lifted, read(0, { oomKills: 4 })).state;
    const calm = step(tripped, read(1_000, { oomKills: 4 })).state;
    const blind = step(calm, read(2_000, { oomKills: "unreadable" }));
    expect(blind.state).toMatchObject({ on: true, calmSinceMs: null, oomKills: 4 });
    expect(step(calm, read(2_000, { swap: "unreadable" })).state.calmSinceMs).toBeNull();
    const back = step(calm, read(500, { oomKills: 4 }));
    expect(back.state.calmSinceMs).toBe(500);
  });

  it("Linux without an OOM counter line never lifts it (no kill could be seen)", () => {
    const run = calmFrom(FALLBACK_START, 0, { oomKills: "unreadable" });
    expect(run.state.on).toBe(true);
    expect(run.events.every((event) => event === null)).toBe(true);
  });

  it("a counter that went down (a reboot) is the new baseline, not a kill", () => {
    const down = step(lifted, read(1_000, { oomKills: 0 }));
    expect(down).toMatchObject({ state: { on: false, oomKills: 0 }, event: null });
    expect(step(down.state, read(2_000, { oomKills: 1 })).event).toBe("trip");
    // A machine without the counter (`null`) keeps none.
    expect(step(FALLBACK_START, read(1_000, { oomKills: null })).state.oomKills).toBeNull();
  });
});

describe("enforce: costs come from the rule file", () => {
  it("a writer costs the rule's writer start cost, a light agent the light one", () => {
    expect(enforceCosts(CAPACITY_RULE, 0)).toEqual({
      [LIGHT_KIND]: { mb: LIGHT_MB },
      [WRITER_KIND]: { mb: WRITER_MB },
    });
    // Another rule file, other costs: nothing in code.
    const rule = {
      ...CAPACITY_RULE,
      kinds: {
        ...CAPACITY_RULE.kinds,
        [LIGHT_KIND]: { ...CAPACITY_RULE.kinds[LIGHT_KIND]!, startMb: 500 },
        [WRITER_KIND]: { ...CAPACITY_RULE.kinds[WRITER_KIND]!, startMb: 1000 },
      },
    };
    expect(enforceCosts(rule, 0)).toEqual({
      [LIGHT_KIND]: { mb: 500 },
      [WRITER_KIND]: { mb: 1000 },
    });
  });
});

describe("enforce: ceilings and floors", () => {
  it("lifts to min(3 x cores, 64); the fallback keeps the fixed 10, never above derived", () => {
    expect(enforceCeilings(CAPACITY_RULE, 4, false, "full")).toEqual({ agent: 12, job: 4 });
    expect(enforceCeilings(CAPACITY_RULE, 32, false, "full").agent).toBe(MAX_AGENT_CEILING);
    expect(enforceCeilings(CAPACITY_RULE, 12, true, "full").agent).toBe(10);
    expect(enforceCeilings(CAPACITY_RULE, 2, true, "full").agent).toBe(6);
    for (const cores of [null, undefined, 0, -1, "8", Number.NaN]) {
      expect(enforceCeilings(CAPACITY_RULE, cores, false, "full").agent).toBe(10);
    }
  });

  it("the basic tier (no agent scopes or no OOM counter) keeps the fixed 10, lifted or not", () => {
    expect(enforceCeilings(CAPACITY_RULE, 32, false, "basic").agent).toBe(10);
    expect(enforceCeilings(CAPACITY_RULE, 2, false, "basic").agent).toBe(6);
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
  const lifted: EnforceState = {
    ...ENFORCE_START,
    pressure: calmFor,
    fallback: { ...FALLBACK_START, on: false },
    settled: true,
  };
  const decide = (input: Partial<EnforceInput>) =>
    enforceDecision(CAPACITY_RULE, {
      facts: machine(),
      tier: "full",
      running: [],
      starting: [],
      ask: LIGHT_KIND,
      state: lifted,
      previous: calmFor,
      settledBefore: true,
      nowMs: 0,
      ...input,
    });
  const inFallback: EnforceState = { ...lifted, fallback: FALLBACK_START };
  const lights = (count: number, demandMb: number | null = 300) =>
    Array.from({ length: count }, () => ({ kind: LIGHT_KIND, demandMb }));

  it("two starts in one second: each start owes its full cost to the next decision", () => {
    // 16 GB with caps: margin 1639 MB. 900 MB free for new work: one more agent fits, not two.
    const facts = machine({ availableMb: 1639 + 900, totalMb: 16_384 });
    const one = decide({ facts, running: lights(2, LIGHT_MB), starting: [LIGHT_KIND] });
    expect(one).toMatchObject({ admit: true, reason: "fits", owedMb: LIGHT_MB });
    const two = decide({ facts, running: lights(2, LIGHT_MB), starting: [LIGHT_KIND, LIGHT_KIND] });
    expect(two).toMatchObject({ admit: false, reason: "budget", owedMb: 2 * LIGHT_MB });
    // A running agent that has not grown to its cost yet owes the rest.
    expect(decide({ facts, running: lights(2, 100) }).owedMb).toBe(2 * (LIGHT_MB - 100));
    // A starting writer owes the rule's writer cost, not a light one.
    expect(decide({ facts, starting: [WRITER_KIND] }).owedMb).toBe(WRITER_MB);
    expect(decide({ facts, ask: WRITER_KIND }).costMb).toBe(WRITER_MB);
  });

  it("an agent whose memory cannot be read is not counted twice, and owes as a writer", () => {
    // No agent scopes (macOS, Windows, Linux without systemd): its memory is out of the available
    // read already. It is taken to use a light start cost and owes the rest of a writer's.
    const unread = decide({ running: [{ kind: LIGHT_KIND, demandMb: null }] });
    expect(unread.owedMb).toBe(WRITER_MB - LIGHT_MB);
    expect(decide({ running: [{ kind: WRITER_KIND, demandMb: null }] }).owedMb).toBe(
      WRITER_MB - LIGHT_MB,
    );
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
  });

  it("where growth cannot be seen, a light role never dodges the writers cap", () => {
    const facts = machine({ cpuCores: 2 });
    const unseen = lights(2, null);
    expect(decide({ facts, running: unseen, ask: WRITER_KIND })).toMatchObject({
      admit: false,
      reason: "writers",
    });
    expect(runningKind(CAPACITY_RULE, LIGHT_KIND, null, enforceCosts(CAPACITY_RULE, 0))).toBe(
      WRITER_KIND,
    );
  });

  it("after many raises the grow check still uses the clamped light cost", () => {
    const learned = enforceCosts(CAPACITY_RULE, maxRaises(CAPACITY_RULE));
    const clamp = (LIGHT_MB * CAPACITY_RULE.learnedBounds.maxPercent) / 100;
    expect(learned[LIGHT_KIND]!.mb).toBeGreaterThan(clamp);
    expect(runningKind(CAPACITY_RULE, LIGHT_KIND, clamp, learned)).toBe(LIGHT_KIND);
    expect(runningKind(CAPACITY_RULE, LIGHT_KIND, clamp + 1, learned)).toBe(WRITER_KIND);
  });

  it("lifted past the fixed ten; the fallback stops at ten", () => {
    const running = lights(10);
    expect(decide({ running }).admit).toBe(true);
    expect(decide({ running, state: inFallback })).toMatchObject({
      admit: false,
      reason: "ceiling",
    });
    expect(decide({ running, tier: "basic" })).toMatchObject({ admit: false, reason: "ceiling" });
    // Starts count to the ceiling too.
    expect(decide({ running: lights(8), starting: [LIGHT_KIND, WRITER_KIND] }).admit).toBe(true);
    expect(
      decide({ running: lights(8), starting: [LIGHT_KIND, WRITER_KIND], state: inFallback }).reason,
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

  it("readings that fail are elevated: the floor still starts, the fixed ten after a while", () => {
    const unread = { ...lifted, unreadSinceMs: 0 };
    // Nothing read, no cgroup files, a container cap without its usage, more than 1 TiB of RAM.
    for (const facts of [
      null,
      machine({ availableMb: null }),
      machine({ totalMb: 2_000_000, availableMb: 1_000_000 }),
      machine({ cpuCores: null }),
    ]) {
      expect(decide({ facts, state: unread })).toMatchObject({ admit: true, reason: "floor" });
      expect(decide({ facts, state: unread, running: lights(1) }).admit).toBe(true);
      // Past the floor: elevated (no new piece) for a while, never without end.
      expect(decide({ facts, state: unread, running: lights(2) })).toMatchObject({
        admit: false,
        reason: "unreadable",
        level: "elevated",
      });
      const later = { state: unread, nowMs: UNREADABLE_FIXED_MS };
      expect(decide({ facts, ...later, running: lights(9) })).toMatchObject({ admit: true });
      expect(decide({ facts, ...later, running: lights(8), starting: [WRITER_KIND] }).admit).toBe(
        true,
      );
      expect(decide({ facts, ...later, running: lights(10) })).toMatchObject({
        admit: false,
        reason: "ceiling",
      });
    }
    // Readings that are partly unreadable are elevated, and the floor admits there too.
    for (const patch of [
      { swap: "unreadable" },
      { swap: { totalMb: 10, freeMb: 20 } },
      { psi: "unreadable" },
    ] as Array<Partial<CapacityFacts>>) {
      expect(decide({ facts: machine(patch), running: lights(2) })).toMatchObject({
        admit: false,
        level: "elevated",
      });
      expect(decide({ facts: machine(patch), running: lights(1) }).admit).toBe(true);
    }
  });
});

describe("enforce: one reading steps the level, the fallback and the unread time", () => {
  /** 12 GB, 12 cores, 8 GB free, no stall, old cold swap at 20 % free (lens A's machine). */
  const host = (patch: Partial<CapacityFacts> = {}): CapacityFacts => ({
    pieceCaps: false,
    totalMb: 12_288,
    availableMb: 8_192,
    cpuCores: 12,
    swap: { totalMb: 4096, freeMb: 820 },
    psi: { someAvg10Pct: 0, fullAvg10Pct: 0 },
    pieces: [],
    ...patch,
  });
  const reading = (nowMs: number, facts: CapacityFacts | null = host()): EnforceReading => ({
    facts,
    oomKills: 0,
    agentKills: {},
    nowMs,
  });
  const running = [
    { kind: WRITER_KIND, demandMb: 500 },
    { kind: WRITER_KIND, demandMb: 500 },
  ];

  /** Readings every 30 s from a restart; the times a light agent was admitted. */
  const fromRestart = (facts: CapacityFacts, untilMs = 5 * 60_000, from = ENFORCE_START) => {
    let state = from;
    const admitted: Array<number> = [];
    for (let nowMs = 0; nowMs <= untilMs; nowMs += 30_000) {
      const step = enforceStep(CAPACITY_RULE, state, reading(nowMs, facts));
      state = step.state;
      const decision = enforceDecision(CAPACITY_RULE, {
        facts,
        tier: "full",
        running,
        starting: [],
        ask: LIGHT_KIND,
        state,
        previous: step.previous,
        settledBefore: step.settledBefore,
        nowMs,
      });
      if (decision.admit) admitted.push(nowMs);
    }
    return { state, admitted };
  };

  it("a restart on a host with used but idle swap lifts to ok within about a minute", () => {
    const { state, admitted } = fromRestart(host());
    // Read 1 starts the calm (and learns the swap trend); 60 s later, on a known trend, it is ok.
    expect(admitted[0]).toBe(60_000);
    expect(state).toMatchObject({ settled: true, pressure: { level: "ok" } });
  });

  it("a restart on a host just above its margin lifts too; real pressure keeps the 150 % bar", () => {
    const marginMb = capacityLevel(CAPACITY_RULE, { facts: host(), previous: null }).marginMb!;
    const near = host({ availableMb: Math.ceil(marginMb * 1.2) });
    expect(fromRestart(near).state).toMatchObject({ settled: true, pressure: { level: "ok" } });
    // Once settled, a real "elevated" (memory under the margin) needs 150 % of it again.
    const settled = fromRestart(near).state;
    const pressed = enforceStep(
      CAPACITY_RULE,
      settled,
      reading(400_000, host({ availableMb: Math.floor(marginMb * 0.9) })),
    ).state;
    expect(pressed.pressure.level).toBe("elevated");
    let state = pressed;
    for (let nowMs = 430_000; nowMs <= 900_000; nowMs += 30_000) {
      state = enforceStep(CAPACITY_RULE, state, reading(nowMs, near)).state;
    }
    expect(state.pressure.level).toBe("elevated");
  });

  it("swap that fills keeps it elevated; a failed reading starts the unread time, a whole one ends it", () => {
    const first = enforceStep(CAPACITY_RULE, ENFORCE_START, reading(0)).state;
    const filling = enforceStep(
      CAPACITY_RULE,
      first,
      reading(30_000, host({ swap: { totalMb: 4096, freeMb: 820 - 200 } })),
    ).state;
    expect(filling.pressure).toMatchObject({ level: "elevated", calmSinceMs: null });
    const blind = enforceStep(CAPACITY_RULE, filling, reading(60_000, null)).state;
    expect(blind.unreadSinceMs).toBe(60_000);
    const still = enforceStep(CAPACITY_RULE, blind, reading(90_000, host({ totalMb: 2_000_000 })));
    expect(still.state.unreadSinceMs).toBe(60_000);
    expect(
      enforceStep(CAPACITY_RULE, still.state, reading(120_000)).state.unreadSinceMs,
    ).toBeNull();
  });
});
