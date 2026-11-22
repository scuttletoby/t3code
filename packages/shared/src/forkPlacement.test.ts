import { canonicalPlacementLabels, type HostResourcesSnapshot } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { chooseLoadBalancedEnvironment } from "../../client-runtime/src/load-balancing.ts";
import {
  canonicalLabels,
  checkHardRules,
  choosePlacement,
  type HardRule,
  type MachineFacts,
  type PlacementInput,
  type PlacementMachine,
  type PlacementTask,
  UPSTREAM_WEIGHTS,
} from "./forkPlacement.ts";

const NOW = 1_000_000_000;
const MB = 1024 * 1024;
const GB = 1024 * MB;

const host: HostResourcesSnapshot = {
  sampledAt: NOW,
  cpuUtilization: 0.2,
  cpuCount: 8,
  availableMemoryBytes: 8 * GB,
  totalMemoryBytes: 16 * GB,
};

const facts = (over: Partial<MachineFacts> = {}): MachineFacts => ({
  hostResources: host,
  placement: { labels: [], available: true, factsAt: NOW },
  receivedAt: NOW,
  ...over,
});

const machine = (
  environmentId: string,
  over: Partial<PlacementMachine> = {},
): PlacementMachine => ({
  environmentId,
  here: false,
  link: {
    reachable: true,
    expiresAt: NOW + 3_600_000,
    access: "full-access",
    betaHere: true,
    betaThere: true,
  },
  facts: facts(),
  workspace: { matchingCheckouts: 1, providerPresent: true, modelPresent: true },
  ...over,
});

const here = (over: Partial<PlacementMachine> = {}) =>
  machine("here", { here: true, link: null, ...over });

const withPlacement = (over: Partial<NonNullable<MachineFacts["placement"]>>) =>
  facts({ placement: { labels: [], available: true, factsAt: NOW, ...over } });

const withHost = (over: Partial<HostResourcesSnapshot>) =>
  facts({ hostResources: { ...host, ...over } });

const task = (over: Partial<PlacementTask> = {}): PlacementTask => ({
  runtimeMode: "full-access",
  needs: [],
  never: [],
  fromPlacedChild: false,
  ...over,
});

const place = (machines: PlacementMachine[], over: Partial<PlacementInput> = {}) =>
  choosePlacement({ mode: "enforce", task: task(), machines, now: NOW, ...over });

const link = machine("x").link!;

/** A machine's facts with placement facts but no host sample (no sampler, or its read failed). */
const noHost = (over: Partial<NonNullable<MachineFacts["placement"]>> = {}): MachineFacts => ({
  placement: { labels: [], available: true, factsAt: NOW, ...over },
  receivedAt: NOW,
});

/** `placementNeeds` as the settings keep a bad value: it fits no machine. */
const brokenNeeds = { unreadable: ["no spaces allowed"], problem: "bad label" };
const BROKEN_NEEDS_REASON =
  "this project's placementNeeds setting is not valid: bad label; fix it in Settings";

interface Row {
  readonly name: string;
  readonly machine: PlacementMachine;
  readonly task?: Partial<PlacementTask>;
  /** `null`: the rule passes. */
  readonly reason: string | null;
}

const table: Record<HardRule, Row[]> = {
  H1: [
    { name: "linked, reachable, beta on", machine: machine("a"), reason: null },
    { name: "here needs no link", machine: here(), reason: null },
    {
      name: "no link",
      machine: machine("a", { link: null }),
      reason: "There is no link to this machine.",
    },
    {
      name: "not reachable now",
      machine: machine("a", { link: { ...link, reachable: false } }),
      reason: "The machine did not answer now.",
    },
    {
      name: "expired",
      machine: machine("a", { link: { ...link, expiresAt: NOW } }),
      reason: "The link to this machine has expired.",
    },
    {
      name: "expires 1 ms from now: still open",
      machine: machine("a", { link: { ...link, expiresAt: NOW + 1 } }),
      reason: null,
    },
    {
      name: "expiry time NaN",
      machine: machine("a", { link: { ...link, expiresAt: Number.NaN } }),
      reason: "The link's expiry time is not a valid number.",
    },
    {
      name: "beta off there",
      machine: machine("a", { link: { ...link, betaThere: false } }),
      reason: "Linked machines beta is off at one end.",
    },
    {
      name: "beta off here",
      machine: machine("a", { link: { ...link, betaHere: false } }),
      reason: "Linked machines beta is off at one end.",
    },
  ],
  H2: [
    { name: "fresh facts", machine: machine("a"), reason: null },
    {
      name: "15 s old is still fresh",
      machine: machine("a", { facts: facts({ receivedAt: NOW - 15_000 }) }),
      reason: null,
    },
    {
      name: "no facts (older build)",
      machine: machine("a", { facts: null }),
      reason: "The machine sent no facts (no answer, or an older build).",
    },
    {
      name: "no placement facts",
      machine: machine("a", { facts: { hostResources: host, receivedAt: NOW } }),
      reason: "The machine sent no placement facts.",
    },
    {
      name: "an answer that did not decode: reported as not valid",
      machine: machine("a", {
        facts: { ...facts(), problem: "placement.available: expected a boolean" },
      }),
      reason: "The machine's facts are not valid: placement.available: expected a boolean",
    },
    {
      name: "here without a host sample: its own gate checks at launch",
      machine: here({ facts: noHost() }),
      reason: null,
    },
    {
      name: "here with a broken host sample: still refused",
      machine: here({ facts: withHost({ totalMemoryBytes: 0 }) }),
      reason: "The machine's CPU and memory facts are incomplete or not valid numbers.",
    },
    {
      name: "no host facts",
      machine: machine("a", {
        facts: { placement: { labels: [], available: true, factsAt: NOW }, receivedAt: NOW },
      }),
      reason: "The machine sent no CPU and memory facts.",
    },
    {
      name: "exactly 5 s ahead is still trusted",
      machine: machine("a", { facts: facts({ receivedAt: NOW + 5_000 }) }),
      reason: null,
    },
    ...(
      [
        ["zero cores", { cpuCount: 0 }],
        ["NaN cores", { cpuCount: Number.NaN }],
        ["negative cores", { cpuCount: -2 }],
        ["infinite cores", { cpuCount: Number.POSITIVE_INFINITY }],
        ["NaN total memory", { totalMemoryBytes: Number.NaN }],
        ["negative total memory", { totalMemoryBytes: -GB }],
        ["NaN free memory", { availableMemoryBytes: Number.NaN }],
        ["negative free memory", { availableMemoryBytes: -GB }],
        ["infinite free memory", { availableMemoryBytes: Number.POSITIVE_INFINITY }],
      ] as const
    ).map(([name, over]) => ({
      name,
      machine: machine("a", { facts: withHost(over) }),
      reason: "The machine's CPU and memory facts are incomplete or not valid numbers.",
    })),
    ...[Number.NaN, Number.POSITIVE_INFINITY, Number.NEGATIVE_INFINITY].map((receivedAt) => ({
      name: `receipt time ${receivedAt}`,
      machine: machine("a", { facts: facts({ receivedAt }) }),
      reason: "The facts have no valid receipt time, so they count as too old.",
    })),
    {
      name: "stale",
      machine: machine("a", { facts: facts({ receivedAt: NOW - 15_001 }) }),
      reason: "The machine's facts are too old.",
    },
    {
      name: "received in the future",
      machine: machine("a", { facts: facts({ receivedAt: NOW + 5_001 }) }),
      reason: "The facts' receipt time is ahead.",
    },
  ],
  H3: [
    { name: "full-access link, full-access task", machine: machine("a"), reason: null },
    { name: "here has no link to check", machine: here(), reason: null },
    {
      name: "auto link, auto-accept-edits task",
      machine: machine("a", { link: { ...link, access: "auto" } }),
      task: { runtimeMode: "auto-accept-edits" },
      reason: null,
    },
    {
      name: "approval link, full-access task",
      machine: machine("a", { link: { ...link, access: "approval-required" } }),
      reason: "The link allows approval-required, lower than the task's full-access.",
    },
    {
      name: "read-only link",
      machine: machine("a", { link: { ...link, access: "read-only" } }),
      task: { runtimeMode: "approval-required" },
      reason: "The link allows read-only, lower than the task's approval-required.",
    },
    {
      name: "no link",
      machine: machine("a", { link: null }),
      reason: "There is no link, so its access is unknown.",
    },
    {
      name: "an access value this build does not know",
      machine: machine("a", { link: { ...link, access: "admin" as never } }),
      task: { runtimeMode: "approval-required" },
      reason: "The link allows admin, which this build does not know.",
    },
    {
      name: "the task's mode left unresolved (inherit)",
      machine: machine("a"),
      task: { runtimeMode: "inherit" as never },
      reason: "The task's runtime mode inherit is not one this build knows.",
    },
  ],
  H4: [
    { name: "available", machine: machine("a"), reason: null },
    {
      name: "busy with a reason",
      machine: machine("a", { facts: withPlacement({ available: false, reason: "gaming" }) }),
      reason: "The machine says it is not available: gaming.",
    },
    {
      name: "not available, no reason",
      machine: machine("a", { facts: withPlacement({ available: false }) }),
      reason: "The machine says it is not available: no reason given.",
    },
    {
      name: "no placement facts",
      machine: machine("a", { facts: { hostResources: host, receivedAt: NOW } }),
      reason: "No facts say whether the machine is free.",
    },
  ],
  H5: [
    {
      name: "one label of the group only",
      machine: machine("a", { facts: withPlacement({ labels: ["ci-runner"] }) }),
      task: { never: [["ci-runner", "owner-logins"]] },
      reason: null,
    },
    {
      name: "an empty group is no group",
      machine: machine("a"),
      task: { never: [[]] },
      reason: null,
    },
    {
      name: "whole group present",
      machine: machine("a", { facts: withPlacement({ labels: ["owner-logins", "ci-runner"] }) }),
      task: {
        never: [
          ["gpu", "x"],
          ["ci-runner", "owner-logins"],
        ],
      },
      reason: "The machine has all of a never group: ci-runner + owner-logins.",
    },
    {
      name: "no labels to check",
      machine: machine("a", { facts: null }),
      task: { never: [["ci-runner", "owner-logins"]] },
      reason: "No labels to check the never groups against.",
    },
    {
      name: "mixed case and spaces still match (labels are trimmed and lower-cased)",
      machine: machine("a", { facts: withPlacement({ labels: ["work", "laptop "] }) }),
      task: { never: [["Work", " LAPTOP"]] },
      reason: "The machine has all of a never group: work + laptop.",
    },
    {
      name: "a blank label in a group is dropped, so the group gets stricter",
      machine: machine("a", { facts: withPlacement({ labels: ["work"] }) }),
      task: { never: [["work", "  "]] },
      reason: "The machine has all of a never group: work.",
    },
    {
      name: "a group of blanks only is no group",
      machine: machine("a"),
      task: { never: [[" ", ""]] },
      reason: null,
    },
  ],
  H6: [
    { name: "no needs", machine: machine("a"), reason: null },
    {
      name: "every need present",
      machine: machine("a", { facts: withPlacement({ labels: ["gpu", "cuda", "x"] }) }),
      task: { needs: ["gpu", "cuda"] },
      reason: null,
    },
    {
      name: "a need missing",
      machine: machine("a", { facts: withPlacement({ labels: ["gpu"] }) }),
      task: { needs: ["gpu", "cuda"] },
      reason: "The machine lacks what the project needs: cuda.",
    },
    {
      name: "no labels to check",
      machine: machine("a", { facts: null }),
      reason: "No labels to check the project's needs against.",
    },
    {
      name: "mixed case and spaces still match (the same canonical form)",
      machine: machine("a", { facts: withPlacement({ labels: [" GPU", "Cuda "] }) }),
      task: { needs: ["gpu ", " CUDA"] },
      reason: null,
    },
    {
      name: "a blank need is never met",
      machine: machine("a", { facts: withPlacement({ labels: ["gpu"] }) }),
      task: { needs: ["gpu", "  "] },
      reason: "The machine lacks what the project needs: (a blank need).",
    },
    {
      name: "a broken needs value fits no machine, never read as no needs",
      machine: machine("a", { facts: withPlacement({ labels: ["gpu"] }) }),
      task: { needs: brokenNeeds },
      reason: BROKEN_NEEDS_REASON,
    },
  ],
  H7: [
    { name: "one checkout, provider and model", machine: machine("a"), reason: null },
    {
      name: "not looked up",
      machine: machine("a", { workspace: null }),
      reason: "The checkout, provider and model there were not checked.",
    },
    {
      name: "no checkout",
      machine: machine("a", {
        workspace: { matchingCheckouts: 0, providerPresent: true, modelPresent: true },
      }),
      reason: "0 matching checkouts there, not exactly one.",
    },
    {
      name: "two checkouts",
      machine: machine("a", {
        workspace: { matchingCheckouts: 2, providerPresent: true, modelPresent: true },
      }),
      reason: "2 matching checkouts there, not exactly one.",
    },
    {
      name: "no provider",
      machine: machine("a", {
        workspace: { matchingCheckouts: 1, providerPresent: false, modelPresent: true },
      }),
      reason: "The provider is not there.",
    },
    {
      name: "no model",
      machine: machine("a", {
        workspace: { matchingCheckouts: 1, providerPresent: true, modelPresent: false },
      }),
      reason: "The model is not there.",
    },
  ],
  H8: [
    {
      name: "here without a host sample: slots and the gate's free memory only",
      machine: here({ facts: noHost({ slotsFree: 1, memoryFloorMb: 1024, freeMemoryMb: 2048 }) }),
      reason: null,
    },
    {
      name: "here without a host sample: CPU not measured yet is no refusal",
      machine: here({ facts: withHost({ cpuUtilization: null }) }),
      reason: null,
    },
    {
      name: "here without a host sample: still no slot free",
      machine: here({ facts: noHost({ slotsFree: 0 }) }),
      reason: "No agent slot is free there.",
    },
    {
      name: "here without a host sample or the gate's free memory: the floor cannot pass",
      machine: here({ facts: noHost({ memoryFloorMb: 1024 }) }),
      reason: "The machine's free memory is not known.",
    },
    {
      name: "a linked machine with CPU not measured: refused as before",
      machine: machine("a", { facts: withHost({ cpuUtilization: null }) }),
      reason: "The machine's CPU use is not measured.",
    },
    {
      name: "a slot free, memory above the floor",
      machine: machine("a", {
        facts: withPlacement({ slotsFree: 1, memoryFloorMb: 1024, freeMemoryMb: 2048 }),
      }),
      reason: null,
    },
    {
      name: "gate off: no slot limit, CPU and memory only",
      machine: machine("a", { facts: withPlacement({ slotsFree: null, memoryFloorMb: null }) }),
      reason: null,
    },
    {
      name: "floor checked against the host reading when the gate gives no free memory",
      machine: machine("a", { facts: withPlacement({ memoryFloorMb: 8191 }) }),
      reason: null,
    },
    {
      name: "no slot free",
      machine: machine("a", { facts: withPlacement({ slotsFree: 0 }) }),
      reason: "No agent slot is free there.",
    },
    {
      name: "memory at the floor: available, as the slot gate and P3",
      machine: machine("a", {
        facts: withPlacement({ memoryFloorMb: 4096, freeMemoryMb: 4096 }),
      }),
      reason: null,
    },
    {
      name: "memory below the floor",
      machine: machine("a", {
        facts: withPlacement({ memoryFloorMb: 4096, freeMemoryMb: 4095 }),
      }),
      reason: "Free memory 4095 MB is below the machine's floor of 4096 MB.",
    },
    {
      name: "host memory at the floor",
      machine: machine("a", { facts: withPlacement({ memoryFloorMb: 8192 }) }),
      reason: null,
    },
    {
      name: "host memory below the floor",
      machine: machine("a", { facts: withPlacement({ memoryFloorMb: 8193 }) }),
      reason: "Free memory 8192 MB is below the machine's floor of 8193 MB.",
    },
    {
      name: "a floor of 0 keeps nothing back",
      machine: machine("a", { facts: withPlacement({ memoryFloorMb: 0, freeMemoryMb: 0 }) }),
      reason: null,
    },
    ...[Number.NaN, -1, Number.POSITIVE_INFINITY].flatMap((bad) => [
      {
        name: `free slot count ${bad}`,
        machine: machine("a", { facts: withPlacement({ slotsFree: bad }) }),
        reason: "The machine's free slot count is not a valid number.",
      },
      {
        name: `memory floor ${bad}`,
        machine: machine("a", { facts: withPlacement({ memoryFloorMb: bad }) }),
        reason: "The machine's memory floor is not a valid number.",
      },
      {
        name: `free memory ${bad}`,
        machine: machine("a", { facts: withPlacement({ freeMemoryMb: bad }) }),
        reason: "The machine's free memory is not a valid number.",
      },
      {
        name: `CPU use ${bad}`,
        machine: machine("a", { facts: withHost({ cpuUtilization: bad }) }),
        reason: "The machine's CPU use is not a valid number.",
      },
    ]),
    {
      name: "host free memory NaN under a floor",
      machine: machine("a", {
        facts: facts({
          hostResources: { ...host, availableMemoryBytes: Number.NaN },
          placement: { labels: [], available: true, factsAt: NOW, memoryFloorMb: 1024 },
        }),
      }),
      reason: "The machine's free memory is not a valid number.",
    },
    {
      name: "CPU just under 95 %",
      machine: machine("a", { facts: withHost({ cpuUtilization: 0.9499 }) }),
      reason: null,
    },
    {
      name: "CPU at 95 %",
      machine: machine("a", { facts: withHost({ cpuUtilization: 0.95 }) }),
      reason: "The machine's CPU is at 95 % or more.",
    },
    {
      name: "CPU not measured",
      machine: machine("a", { facts: withHost({ cpuUtilization: null }) }),
      reason: "The machine's CPU use is not measured.",
    },
    {
      name: "no facts",
      machine: machine("a", { facts: null }),
      reason: "No facts to check capacity.",
    },
  ],
  H9: [
    { name: "a top-level task places", machine: machine("a"), reason: null },
    {
      name: "a placed child's children stay here",
      machine: here(),
      task: { fromPlacedChild: true },
      reason: null,
    },
    {
      name: "a placed child does not place onward",
      machine: machine("a"),
      task: { fromPlacedChild: true },
      reason: "This task was itself placed: its children run where it runs.",
    },
  ],
};

describe("hard rules", () => {
  const rows = (Object.entries(table) as [HardRule, Row[]][]).flatMap(([rule, ruleRows]) =>
    ruleRows.map((row) => ({ ...row, rule })),
  );
  it.each(rows)("$rule: $name", (row) => {
    const failures = checkHardRules(row.machine, task(row.task), NOW).filter(
      (failure) => failure.rule === row.rule,
    );
    expect(failures.map((failure) => failure.reason)).toEqual(
      row.reason === null ? [] : [row.reason],
    );
    if (row.reason !== null) {
      // A machine failing any rule is not chosen, and the result says why.
      const result = place([row.machine], { task: task(row.task) });
      expect(result.kind).toBe("none");
      if (result.kind === "none") expect(result.reason).toContain(row.reason);
    }
  });

  it("a machine that fits passes all nine", () => {
    expect(checkHardRules(machine("a"), task(), NOW)).toEqual([]);
    expect(checkHardRules(here(), task({ fromPlacedChild: true }), NOW)).toEqual([]);
  });
});

describe("rank: upstream's score, unchanged", () => {
  it("orders as upstream's chooseLoadBalancedEnvironment does", () => {
    const machines = [
      machine("busy", { facts: withHost({ cpuUtilization: 0.9 }) }),
      machine("idle"),
      machine("preferred", { facts: withHost({ cpuCount: 6 }), weight: "prefer" }),
      machine("less", { facts: withHost({ cpuCount: 12 }), weight: "less" }),
    ];
    const result = place(machines);
    expect(result.kind === "picked" && result.order).toEqual(["preferred", "idle", "less", "busy"]);
    const upstream = machines.map((m) => ({
      environmentId: m.environmentId,
      resources: m.facts?.hostResources ?? null,
      receivedAt: NOW,
      weight: UPSTREAM_WEIGHTS[m.weight ?? "normal"],
    }));
    expect(chooseLoadBalancedEnvironment(upstream, NOW)).toBe("preferred");
    expect(UPSTREAM_WEIGHTS).toEqual({ prefer: 100, normal: 50, less: 25, manual: 0 });
  });

  it("manual: never chosen automatically, used when named", () => {
    const manual = machine("m", { weight: "manual" });
    const result = place([manual]);
    expect(result.kind).toBe("none");
    expect(result.kind === "none" && result.reason).toContain(
      "Set to manual: used only when named.",
    );
    expect(place([manual], { task: task({ named: "m" }) })).toMatchObject({
      kind: "picked",
      environmentId: "m",
    });
  });

  it("passes the hard rules but upstream's score leaves it out: says so", () => {
    const full = machine("a", { facts: withHost({ availableMemoryBytes: (16 * GB) / 20 }) });
    const result = place([full]);
    expect(result.kind === "none" && result.reason).toContain(
      "Upstream's score leaves it out (free memory 5 % or less).",
    );
  });

  it("a weight word this build does not know: not ranked, and the reason names the word", () => {
    for (const weight of ["Prefer", "toString", ""]) {
      const odd = machine("a", { weight: weight as never });
      const result = place([odd]);
      expect(result).toMatchObject({
        kind: "none",
        reason: `No machine fits. a: Its weight word "${weight}" is not one of prefer, normal, less or manual.`,
      });
      // Named: the weight is not a rule, so it is still used.
      expect(place([odd], { task: task({ named: "a" }) })).toMatchObject({ kind: "picked" });
    }
  });
});

describe("the state question (design section 7, rows with no I/O)", () => {
  it("same call again: same pick, also with the machines listed in another order", () => {
    const machines = [machine("b"), machine("a"), here(), machine("c", { weight: "less" })];
    const first = place(machines);
    expect(place(machines)).toEqual(first);
    expect(place([...machines].reverse())).toEqual(first);
    expect(first).toMatchObject({
      kind: "picked",
      environmentId: "here",
      order: ["here", "a", "b", "c"],
    });
  });

  it("ties: here first, then by id", () => {
    expect(place([machine("b"), machine("a")])).toMatchObject({ environmentId: "a" });
    expect(place([machine("a"), here()])).toMatchObject({ environmentId: "here" });
  });

  it("stale facts or two orchestrators: the receiver refuses, the sender tries the next", () => {
    const machines = [machine("a"), machine("b", { facts: withHost({ cpuUtilization: 0.5 }) })];
    const first = place(machines);
    expect(first).toMatchObject({ environmentId: "a", order: ["a", "b"] });
    expect(place(machines.filter((m) => m.environmentId !== "a"))).toMatchObject({
      environmentId: "b",
    });
  });

  it("no facts or an older build: never chosen automatically; named, the rules that need none", () => {
    const old = machine("old", { facts: null });
    expect(place([old])).toMatchObject({ kind: "none" });
    expect(place([old, machine("a")])).toMatchObject({ environmentId: "a", order: ["a"] });
    const named = place([old], { task: task({ named: "old" }) });
    expect(named).toMatchObject({
      kind: "picked",
      environmentId: "old",
      why: "old was named and passes the hard rules that need no facts (H1, H3, H7, H9).",
    });
    // Runs again: the same answer.
    expect(place([old], { task: task({ named: "old" }) })).toEqual(named);
  });

  const noFactsRows: ReadonlyArray<{
    readonly name: string;
    readonly over: Partial<PlacementMachine>;
    readonly task?: Partial<PlacementTask>;
    readonly rule: HardRule;
    readonly reason: string;
  }> = [
    {
      name: "link expired",
      over: { link: { ...link, expiresAt: NOW - 1 } },
      rule: "H1",
      reason: "The link to this machine has expired.",
    },
    {
      name: "link access below the task's mode",
      over: { link: { ...link, access: "auto" } },
      rule: "H3",
      reason: "The link allows auto, lower than the task's full-access.",
    },
    {
      name: "two checkouts there",
      over: { workspace: { matchingCheckouts: 2, providerPresent: true, modelPresent: true } },
      rule: "H7",
      reason: "2 matching checkouts there, not exactly one.",
    },
    {
      name: "no provider there",
      over: { workspace: { matchingCheckouts: 1, providerPresent: false, modelPresent: false } },
      rule: "H7",
      reason: "The provider is not there.",
    },
    {
      name: "the lookup did not run (a missing lookup never fits)",
      over: { workspace: null },
      rule: "H7",
      reason: "The checkout, provider and model there were not checked.",
    },
    {
      name: "asked by a placed child",
      over: {},
      task: { fromPlacedChild: true },
      rule: "H9",
      reason: "This task was itself placed: its children run where it runs.",
    },
  ];
  it.each(noFactsRows)("named without facts, $name: refused by $rule", (row) => {
    const result = place([machine("old", { facts: null, ...row.over })], {
      task: task({ named: "old", ...row.task }),
    });
    expect(result).toMatchObject({
      kind: "none",
      reason: `old was named but fails ${row.rule}: ${row.reason}`,
    });
    expect(result.kind === "none" && result.verdicts[0]?.failures.map((f) => f.rule)).toEqual([
      row.rule,
    ]);
  });

  it("named with facts: every hard rule, no ranking", () => {
    const busy = machine("a", { facts: withPlacement({ available: false, reason: "in use" }) });
    expect(place([busy, machine("b")], { task: task({ named: "a" }) })).toMatchObject({
      kind: "none",
      reason: "a was named but fails H4: The machine says it is not available: in use.",
    });
    expect(place([machine("b")], { task: task({ named: "a" }) })).toMatchObject({
      kind: "none",
      reason: "No machine named a.",
    });
  });

  it("missing, stale or partial facts never fit", () => {
    const partial: Array<MachineFacts | null> = [
      null,
      { receivedAt: NOW },
      { hostResources: host, receivedAt: NOW },
      noHost(),
      facts({ receivedAt: NOW - 60_000 }),
      withHost({ cpuUtilization: null }),
      withHost({ totalMemoryBytes: 0 }),
      { ...facts(), problem: "not valid" },
    ];
    for (const f of partial) {
      expect(place([machine("a", { facts: f })]).kind).toBe("none");
    }
    // Here, only a missing host sample is let through (next test); the rest never fit.
    for (const f of partial.filter((f) => f !== partial[3] && f !== partial[5])) {
      expect(place([here({ facts: f })]).kind).toBe("none");
    }
  });

  it("a sampler failure here does not stop all delegation: here is the fallback, last", () => {
    // A lone here without host facts is picked (advise and enforce alike), says why.
    for (const f of [noHost(), withHost({ cpuUtilization: null })]) {
      for (const mode of ["advise", "enforce"] as const) {
        const lone = place([here({ facts: f })], { mode });
        expect(lone).toMatchObject({
          kind: "picked",
          mode,
          environmentId: "here",
          order: ["here"],
        });
        expect(lone.kind === "picked" && lone.why).toBe(
          "here passes every hard rule; its CPU use was not measured, so it is picked only because no machine ranks.",
        );
        // Runs again: the same answer.
        expect(place([here({ facts: f })], { mode })).toEqual(lone);
      }
      // A ranked linked machine comes first; here stays the next candidate on a refusal.
      const both = place([here({ facts: f }), machine("a", { weight: "less" })]);
      expect(both).toMatchObject({ environmentId: "a", order: ["a", "here"] });
      expect(both.kind === "picked" && both.verdicts.find((v) => v.here)?.rank).toBe(2);
      // Its hard rules still apply: a full gate, or needs it lacks, refuse it.
      expect(place([here({ facts: noHost({ slotsFree: 0 }) })]).kind).toBe("none");
      expect(place([here({ facts: f })], { task: task({ needs: ["gpu"] }) }).kind).toBe("none");
      // Upstream's own limits still hold: manual (only when named) or an unknown weight word.
      for (const weight of ["manual", "most" as never] as const) {
        const result = place([here({ facts: f, weight })]);
        expect(result.kind).toBe("none");
        expect(result.kind === "none" ? result.verdicts[0]?.unranked : null).toBe(
          weight === "manual"
            ? "Set to manual: used only when named."
            : 'Its weight word "most" is not one of prefer, normal, less or manual.',
        );
      }
    }
    // With memory sampled but CPU not: 5 % free or less is left out, as upstream's score does.
    const nearlyFull = withHost({
      cpuUtilization: null,
      availableMemoryBytes: 0.02 * host.totalMemoryBytes,
    });
    expect(place([here({ facts: nearlyFull })])).toMatchObject({
      kind: "none",
      verdicts: [{ unranked: "Upstream's score leaves it out (free memory 5 % or less)." }],
    });
  });

  it("a broken needs value fits no machine: named, here, or without facts", () => {
    const needs = brokenNeeds;
    const expected = {
      kind: "none",
      mode: "enforce",
      reason: `No machine fits: ${BROKEN_NEEDS_REASON}.`,
      verdicts: [],
    };
    expect(place([here(), machine("a")], { task: task({ needs }) })).toEqual(expected);
    expect(
      place([machine("old", { facts: null })], { task: task({ needs, named: "old" }) }),
    ).toEqual(expected);
    expect(place([here()], { task: task({ needs, named: "here" }) })).toEqual(expected);
    // The same needs once fixed: labels again.
    expect(place([here()], { task: task({ needs: [] }) })).toMatchObject({ kind: "picked" });
  });

  it("new install (empty settings): no labels, gate off; takes only work with no needs", () => {
    const fresh = machine("new", { facts: withPlacement({ labels: [], slotsFree: null }) });
    expect(place([fresh])).toMatchObject({ kind: "picked", environmentId: "new" });
    expect(place([fresh], { task: task({ needs: ["gpu"] }) })).toMatchObject({ kind: "none" });
  });

  it("receiver's gates off: sender-side CPU and memory rules still apply", () => {
    const gatesOff = withPlacement({ slotsFree: null, memoryFloorMb: null });
    const hot = machine("a", {
      facts: { ...gatesOff, hostResources: { ...host, cpuUtilization: 0.97 } },
    });
    expect(place([hot])).toMatchObject({ kind: "none" });
  });

  it("factsCommand missing, not absolute or hung: not available, with its reason", () => {
    for (const reason of ["factsCommand is not set", "factsCommand is not absolute", "timed out"]) {
      const result = place([machine("a", { facts: withPlacement({ available: false, reason }) })]);
      expect(result.kind === "none" && result.reason).toContain(`not available: ${reason}.`);
    }
  });

  it("clock difference: age is from the receipt time, not the machine's clock", () => {
    const skewed = facts({
      hostResources: { ...host, sampledAt: NOW + 3_600_000 },
      placement: { labels: [], available: true, factsAt: NOW - 3_600_000 },
      receivedAt: NOW - 1_000,
    });
    expect(place([machine("a", { facts: skewed })])).toMatchObject({ environmentId: "a" });
    expect(place([machine("a", { facts: { ...skewed, receivedAt: NOW - 20_000 } })])).toMatchObject(
      { kind: "none" },
    );
  });

  it("old settings file, a bad value, or switched off mid-task: off, upstream behaviour", () => {
    for (const mode of [undefined, null, "", "on", "ENFORCE", 1, {}, "off"]) {
      expect(place([machine("a")], { mode })).toEqual({
        kind: "off",
        reason: "Placement is off: the task runs as upstream.",
      });
    }
    expect(place([machine("a")], { mode: "advise" })).toMatchObject({
      kind: "picked",
      mode: "advise",
      why: "a passes every hard rule and ranks first of 1 by upstream's score.",
    });
  });

  it("enforce with no linked machine: here", () => {
    expect(place([here()])).toMatchObject({ kind: "picked", environmentId: "here" });
  });

  it("duplicate machine ids: a plain refusal, whatever the order or a name", () => {
    const reason = "Two machines share the id a, so none is picked.";
    const twins = [machine("a"), machine("a", { facts: null }), machine("b")];
    for (const machines of [twins, [...twins].reverse()]) {
      expect(place(machines)).toEqual({ kind: "none", mode: "enforce", reason, verdicts: [] });
      expect(place(machines, { task: task({ named: "b" }) })).toMatchObject({ reason });
    }
    expect(place([here({ environmentId: "a" }), machine("a")])).toMatchObject({
      reason,
    });
  });

  it("an empty machine list: a plain no machine result", () => {
    expect(place([])).toEqual({
      kind: "none",
      mode: "enforce",
      reason: "No machine to choose from.",
      verdicts: [],
    });
  });

  it("a placed child's children run where it runs", () => {
    const result = place([machine("a", { weight: "prefer" }), here()], {
      task: task({ fromPlacedChild: true }),
    });
    expect(result).toMatchObject({ kind: "picked", environmentId: "here", order: ["here"] });
  });
});

describe("labels: one canonical form (trimmed, lower-cased, blanks and repeats dropped)", () => {
  it("the engine's form is contracts' canonicalPlacementLabels, the one P2 stores with", () => {
    const written = [" GPU", "gpu", "Work ", "", "  ", "ci-runner"];
    expect(canonicalLabels(written)).toEqual(["gpu", "work", "ci-runner"]);
    expect(canonicalLabels(written)).toEqual(canonicalPlacementLabels(written));
    expect(canonicalLabels([])).toEqual([]);
    // A value from another machine's answer that is not a string never matches.
    expect(canonicalLabels([1, null, "GPU"])).toEqual(["gpu"]);
    const anyLabel = machine("a", { facts: withPlacement({ labels: [7 as never, "gpu"] }) });
    expect(place([anyLabel], { task: task({ needs: ["7"] }) }).kind).toBe("none");
  });
});
