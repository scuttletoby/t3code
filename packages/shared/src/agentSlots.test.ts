import { DEFAULT_SERVER_SETTINGS, ProviderInstanceId } from "@t3tools/contracts";
import type { AgentSlotsSettings } from "@t3tools/contracts/waitFor";
import { describe, expect, it } from "vite-plus/test";

import {
  DISPATCH_SAFETY_MS,
  effectiveSlotLimits,
  limitsCount,
  liveStarting,
  runningAgents,
  slotDecision,
  slotRefusalText,
  type SlotDecision,
  type SlotLimits,
  type SlotThread,
  type StartingEntry,
  visibleRunThreadIds,
} from "./agentSlots.ts";

const MB = 1024 * 1024;
const MAIN = ProviderInstanceId.make("codex");
const SECOND = ProviderInstanceId.make("claude-second");

const slots = (patch: Partial<AgentSlotsSettings>): AgentSlotsSettings => ({
  ...DEFAULT_SERVER_SETTINGS.agentSlots,
  enabled: true,
  ...patch,
});

const limitsOf = (patch: Partial<AgentSlotsSettings>): SlotLimits => {
  const limits = effectiveSlotLimits(slots(patch));
  if (limits === null) throw new Error("expected limits");
  return limits;
};

const shell = (
  id: string,
  instanceId: string,
  options: { readonly active?: boolean; readonly blocked?: boolean } = {},
): SlotThread => ({
  id,
  providerInstanceId: instanceId,
  activeRunId: options.active === false ? null : `run:${id}`,
  ...(options.blocked === undefined ? {} : { blockedOnThreadWait: options.blocked }),
});

const refused = (decision: SlotDecision) => {
  if (decision.type !== "refuse") throw new Error("expected a refusal");
  return decision;
};

describe("effectiveSlotLimits", () => {
  it("is null while off, even with limits set (the default file has it off)", () => {
    expect(effectiveSlotLimits(DEFAULT_SERVER_SETTINGS.agentSlots)).toBeNull();
    expect(
      effectiveSlotLimits({ ...slots({ maxPerMachine: 2, minFreeMemoryMb: 100 }), enabled: false }),
    ).toBeNull();
  });

  it("is null when on with nothing to limit", () => {
    expect(effectiveSlotLimits(slots({}))).toBeNull();
    expect(effectiveSlotLimits(slots({ maxPerAccount: { [MAIN]: 0 } }))).toBeNull();
  });

  it("reads 0 as no limit for every key, an account entry too", () => {
    const limits = limitsOf({ maxPerMachine: 0, maxPerAccount: { [MAIN]: 0, [SECOND]: 2 } });
    expect(limits.machine).toBeNull();
    expect(limits.account(MAIN)).toBeNull();
    expect(limits.account(SECOND)).toBe(2);
    expect(limits.account("not-listed")).toBeNull();
    expect(limits.minFreeMemoryBytes).toBeNull();
  });

  it("converts the memory floor and the start grace", () => {
    const limits = limitsOf({ minFreeMemoryMb: 2048, startGraceSeconds: 45 });
    expect(limits.minFreeMemoryBytes).toBe(2048 * MB);
    expect(limits.startGraceMs).toBe(45_000);
  });

  it("counts agents only when a count limit applies to the target", () => {
    expect(limitsCount(limitsOf({ minFreeMemoryMb: 100 }), MAIN)).toBe(false);
    expect(limitsCount(limitsOf({ maxPerAccount: { [SECOND]: 1 } }), MAIN)).toBe(false);
    expect(limitsCount(limitsOf({ maxPerAccount: { [SECOND]: 1 } }), SECOND)).toBe(true);
    expect(limitsCount(limitsOf({ maxPerMachine: 3 }), MAIN)).toBe(true);
  });
});

describe("runningAgents (T14)", () => {
  it("counts threads with an active run on any provider, less the caller and blocked parents", () => {
    const running = runningAgents(
      [
        shell("caller", MAIN),
        shell("child-codex", MAIN),
        shell("child-claude", SECOND),
        shell("parent-in-thread-wait", MAIN, { blocked: true }),
        shell("finished-child", MAIN, { active: false }),
        shell("not-blocked", SECOND, { blocked: false }),
      ],
      "caller",
    );
    expect(running).toEqual([
      { threadId: "child-codex", instanceId: MAIN },
      { threadId: "child-claude", instanceId: SECOND },
      { threadId: "not-blocked", instanceId: SECOND },
    ]);
  });

  it("counts a thread listed twice once", () => {
    expect(runningAgents([shell("a", MAIN), shell("a", MAIN)], "caller")).toHaveLength(1);
  });
});

describe("liveStarting", () => {
  const entry = (
    token: number,
    atMs: number,
    options: { readonly child?: string; readonly endedAtMs?: number } = {},
  ): StartingEntry => ({
    token,
    instanceId: MAIN,
    atMs,
    ...(options.child === undefined ? {} : { childThreadId: options.child }),
    ...(options.endedAtMs === undefined ? {} : { endedAtMs: options.endedAtMs }),
  });
  const none = new Set<string>();

  it("keeps an entry while its dispatch runs, however long and whatever the grace", () => {
    const entries = [entry(1, 0), entry(2, 0, { child: "child-2" })];
    for (const grace of [0, 30_000]) {
      expect(liveStarting(entries, none, 5 * 60_000, grace).map((e) => e.token)).toEqual([1, 2]);
    }
  });

  it("ends a dispatch that never ended after the safety time (a lost release)", () => {
    expect(liveStarting([entry(1, 0)], none, DISPATCH_SAFETY_MS - 1, 0)).toHaveLength(1);
    expect(liveStarting([entry(1, 0)], none, DISPATCH_SAFETY_MS, 0)).toEqual([]);
  });

  it("after the dispatch, keeps a child with no run for the start grace from the dispatch end", () => {
    const ended = [entry(1, 0, { child: "child-1", endedAtMs: 50_000 })];
    expect(liveStarting(ended, none, 79_999, 30_000)).toHaveLength(1);
    expect(liveStarting(ended, none, 80_000, 30_000)).toEqual([]);
    // A start grace of 0 ends it with the dispatch; while the dispatch ran it still counted.
    expect(liveStarting(ended, none, 50_000, 0)).toEqual([]);
  });

  it("drops an entry once its child shows a run, so the child counts once", () => {
    const entries = [
      entry(1, 0, { child: "child-1" }),
      entry(2, 0, { child: "child-2", endedAtMs: 0 }),
      entry(3, 0, { child: "child-3" }),
      entry(4, 0),
    ];
    const visible = new Set(["child-1", "child-2"]);
    expect(liveStarting(entries, visible, 1_000, 30_000).map((e) => e.token)).toEqual([3, 4]);
  });
});

describe("visibleRunThreadIds", () => {
  it("names every thread with an active run, blocked ones and any caller too", () => {
    const threads = [
      shell("a", MAIN),
      shell("blocked", MAIN, { blocked: true }),
      shell("idle", MAIN, { active: false }),
    ];
    expect([...visibleRunThreadIds(threads)]).toEqual(["a", "blocked"]);
  });
});

describe("slotDecision (T13)", () => {
  const start = (instanceId: string): StartingEntry => ({ token: 1, instanceId, atMs: 0 });
  const agents = (...instances: ReadonlyArray<string>) =>
    instances.map((instanceId, index) => ({ threadId: `t${index}`, instanceId }));

  it("allows below every limit", () => {
    expect(
      slotDecision({
        limits: limitsOf({ maxPerMachine: 3, maxPerAccount: { [MAIN]: 2 }, minFreeMemoryMb: 100 }),
        running: agents(MAIN, SECOND),
        starting: [],
        targetInstanceId: MAIN,
        freeMemoryBytes: 200 * MB,
      }),
    ).toEqual({ type: "allow" });
  });

  it("machine: running plus starting at the limit refuses, with the numbers", () => {
    const decision = refused(
      slotDecision({
        limits: limitsOf({ maxPerMachine: 3 }),
        running: agents(MAIN, SECOND),
        starting: [start(SECOND)],
        targetInstanceId: MAIN,
        freeMemoryBytes: null,
      }),
    );
    expect(decision).toMatchObject({ rule: "machine", used: 3, max: 3 });
  });

  it("account: only the target's agents count", () => {
    const limits = limitsOf({ maxPerAccount: { [MAIN]: 2 } });
    const input = {
      limits,
      running: agents(MAIN, SECOND, SECOND),
      starting: [start(MAIN)],
      freeMemoryBytes: null,
    };
    expect(refused(slotDecision({ ...input, targetInstanceId: MAIN }))).toMatchObject({
      rule: "account",
      used: 2,
      max: 2,
      instanceId: MAIN,
    });
    // SECOND has no limit and the machine has none: two of its agents do not matter.
    expect(slotDecision({ ...input, targetInstanceId: SECOND })).toEqual({ type: "allow" });
  });

  it("memory: below the floor refuses with free and needed MB; no reading skips it", () => {
    const limits = limitsOf({ minFreeMemoryMb: 2048 });
    const input = { limits, running: [], starting: [], targetInstanceId: MAIN };
    expect(refused(slotDecision({ ...input, freeMemoryBytes: 812.7 * MB }))).toMatchObject({
      rule: "memory",
      used: 812,
      max: 2048,
    });
    expect(slotDecision({ ...input, freeMemoryBytes: 2048 * MB })).toEqual({ type: "allow" });
    expect(slotDecision({ ...input, freeMemoryBytes: null })).toEqual({ type: "allow" });
  });

  it("names the machine rule first, then the account, then memory", () => {
    const limits = limitsOf({
      maxPerMachine: 1,
      maxPerAccount: { [MAIN]: 1 },
      minFreeMemoryMb: 10,
    });
    const base = { limits, starting: [], targetInstanceId: MAIN, freeMemoryBytes: 0 };
    expect(refused(slotDecision({ ...base, running: agents(MAIN) })).rule).toBe("machine");
    const noMachine = { ...limits, machine: null };
    expect(refused(slotDecision({ ...base, limits: noMachine, running: agents(MAIN) })).rule).toBe(
      "account",
    );
    expect(refused(slotDecision({ ...base, limits: noMachine, running: [] })).rule).toBe("memory");
  });
});

describe("slotRefusalText", () => {
  const refusal = (rule: "machine" | "account" | "memory", used: number, max: number) =>
    ({ type: "refuse", rule, used, max, instanceId: MAIN }) as const;

  it("says the rule, the numbers and the next step, with waits off", () => {
    expect(slotRefusalText(refusal("machine", 4, 4), false)).toBe(
      "No free agent slot: 4 agents are running on this machine, and the limit is 4. Try again later. Or do the work yourself.",
    );
    expect(slotRefusalText(refusal("account", 1, 1), false)).toBe(
      "No free agent slot: 1 agent is running on account codex, and the limit is 1. Try again later. Or do the work yourself.",
    );
    expect(slotRefusalText(refusal("memory", 812, 2048), false)).toBe(
      "No free agent slot: only 812 MB of memory is free, and a new agent needs at least 2048 MB. Try again later. Or do the work yourself.",
    );
  });

  it("names the slot wait to call when waits are on (the account only for its own rule)", () => {
    expect(slotRefusalText(refusal("account", 2, 2), true)).toContain(
      'Call wait_for {"kind":"slot","slot":{"account":"codex"}} and end your turn',
    );
    expect(slotRefusalText(refusal("machine", 2, 2), true)).toContain(
      'Call wait_for {"kind":"slot","slot":{}} and end your turn',
    );
  });
});
