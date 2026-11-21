import type { HostResourcesSnapshot } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { chooseLoadBalancedEnvironment } from "../../client-runtime/src/load-balancing.ts";
import {
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
      name: "no host facts",
      machine: machine("a", {
        facts: { placement: { labels: [], available: true, factsAt: NOW }, receivedAt: NOW },
      }),
      reason: "The machine sent no CPU and memory facts.",
    },
    {
      name: "zero cores",
      machine: machine("a", { facts: withHost({ cpuCount: 0 }) }),
      reason: "The machine's CPU and memory facts are incomplete.",
    },
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
      name: "memory at the floor",
      machine: machine("a", {
        facts: withPlacement({ memoryFloorMb: 4096, freeMemoryMb: 4096 }),
      }),
      reason: "Free memory 4096 MB is not above the machine's floor of 4096 MB.",
    },
    {
      name: "host memory below the floor",
      machine: machine("a", { facts: withPlacement({ memoryFloorMb: 8192 }) }),
      reason: "Free memory 8192 MB is not above the machine's floor of 8192 MB.",
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

  it("no facts or an older build: never chosen automatically; named, only H1 and H3", () => {
    const old = machine("old", { facts: null, workspace: null });
    expect(place([old])).toMatchObject({ kind: "none" });
    expect(place([old, machine("a")])).toMatchObject({ environmentId: "a", order: ["a"] });
    expect(place([old], { task: task({ named: "old" }) })).toMatchObject({
      kind: "picked",
      environmentId: "old",
      why: "old was named and passes the hard rules that need no facts (H1, H3).",
    });
    const expired = machine("old", { facts: null, link: { ...link, expiresAt: NOW - 1 } });
    const named = place([expired], { task: task({ named: "old" }) });
    expect(named.kind === "none" && named.verdicts[0]?.failures.map((f) => f.rule)).toEqual(["H1"]);
    const narrow = machine("old", { facts: null, link: { ...link, access: "auto" } });
    const narrowNamed = place([narrow], { task: task({ named: "old" }) });
    expect(
      narrowNamed.kind === "none" && narrowNamed.verdicts[0]?.failures.map((f) => f.rule),
    ).toEqual(["H3"]);
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
      { placement: { labels: [], available: true, factsAt: NOW }, receivedAt: NOW },
      facts({ receivedAt: NOW - 60_000 }),
      withHost({ cpuUtilization: null }),
      withHost({ totalMemoryBytes: 0 }),
    ];
    for (const f of partial) {
      expect(place([machine("a", { facts: f })]).kind).toBe("none");
      expect(place([here({ facts: f })]).kind).toBe("none");
    }
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
