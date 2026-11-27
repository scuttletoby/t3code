// Fork (work panel): the capacity bar's numbers and the usage bars. Each test names the question
// it answers.
import type { EnvironmentId, ForkWorkPanelAnswer } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  agentsTotalText,
  ciRunnersText,
  type FleetMachine,
  fleetTotal,
  machineCapacity,
  memoryTotalText,
  REASON_FAILED,
  REASON_OLD_VERSION,
  REASON_UNREACHABLE,
  usageAccounts,
} from "./capacity.logic";

const GB = 1024;
const answer = (
  agents: { used: number; total: number | null },
  memory = { used: 6 * GB, total: 16 * GB },
) =>
  ({
    answeredAt: 0,
    repoQueues: [],
    pullRequests: [],
    capacity: {
      agents: { state: "answered", ...agents, source: "gate" },
      memoryMb: { state: "answered", ...memory, source: "host" },
      memoryFloorMb: 2 * GB,
      factsAt: 0,
    },
  }) satisfies ForkWorkPanelAnswer;
const machine = (id: string, status: FleetMachine["status"]): FleetMachine => ({
  environmentId: id as EnvironmentId,
  label: id,
  isThisMachine: id === "here",
  status,
});

describe("machineCapacity", () => {
  it.each([
    ["an unreachable machine", { kind: "unreachable" } as const, REASON_UNREACHABLE],
    ["a machine that did not answer", { kind: "failed" } as const, REASON_FAILED],
    [
      "an old machine (no capacity in its answer)",
      { kind: "answered", answer: { answeredAt: 0, repoQueues: [], pullRequests: [] } } as const,
      REASON_OLD_VERSION,
    ],
  ])("%s is unknown with why, with no count", (_name, status, reason) => {
    const view = machineCapacity(machine("m", status));
    for (const part of [view.agents, view.memoryMb]) {
      expect(part).toMatchObject({ state: "unknown", reason });
      expect(part.used).toBeUndefined();
    }
  });
});

describe("fleetTotal", () => {
  it("sums the machines that answered and counts the rest as unknown", () => {
    const machines = [
      machine("here", { kind: "answered", answer: answer({ used: 3, total: 8 }) }),
      machine("box", { kind: "answered", answer: answer({ used: 1, total: 4 }) }),
      machine("off", { kind: "unreachable" }),
    ].map(machineCapacity);
    const agents = fleetTotal(machines, (view) => view.agents);
    expect(agents).toEqual({ used: 4, total: 12, answered: 2, unknown: 1 });
    expect(agentsTotalText(agents)).toBe("Agents: 4 of 12 in use · 1 machine unknown");
    expect(memoryTotalText(fleetTotal(machines, (view) => view.memoryMb))).toBe(
      "Memory free: 20 GB of 32 GB · 1 machine unknown",
    );
  });

  it("a machine with no limit makes the total no limit, not a sum", () => {
    const machines = [
      machine("here", { kind: "answered", answer: answer({ used: 3, total: null }) }),
      machine("box", { kind: "answered", answer: answer({ used: 1, total: 4 }) }),
    ].map(machineCapacity);
    expect(agentsTotalText(fleetTotal(machines, (view) => view.agents))).toBe(
      "Agents: 4 of no limit in use",
    );
  });

  it("an empty fleet, or one where nobody answers, is unknown, not 0", () => {
    expect(agentsTotalText(fleetTotal([], (view) => view.agents))).toBe("Agents: unknown");
    const silent = [machine("off", { kind: "unreachable" })].map(machineCapacity);
    expect(memoryTotalText(fleetTotal(silent, (view) => view.memoryMb))).toBe("Memory: unknown");
  });
});

describe("ciRunnersText", () => {
  it("counts busy runners from the answered queue lines; free is never guessed", () => {
    expect(
      ciRunnersText([
        { host: "github.com", repository: "a/b", state: "answered", queued: 1, running: 2 },
        { host: "github.com", repository: "a/c", state: "unknown", reason: "x" },
      ]),
    ).toBe("CI runners: 2 busy · free unknown");
    expect(ciRunnersText([])).toBe("CI runners: unknown");
  });
});

describe("usageAccounts", () => {
  it("gives session and weekly room left, unknown when not reported or unavailable", () => {
    const window = (kind: "session" | "weekly", usedPercent: number) => ({
      id: kind,
      kind,
      label: kind,
      usedPercent,
    });
    expect(
      usageAccounts([
        {
          key: "a",
          driver: "claudeAgent",
          displayName: "Claude",
          email: "x@example.com",
          limits: { windows: [window("session", 38), window("weekly", 20.4)] },
        },
        {
          key: "b",
          driver: "codex",
          displayName: null,
          email: undefined,
          limits: { windows: [window("weekly", 50)] },
        },
        {
          key: "c",
          driver: "codex",
          displayName: "Codex",
          email: undefined,
          limits: { windows: [window("session", 10)], unavailable: { message: "probe failed" } },
        },
      ]),
    ).toEqual([
      { key: "a", name: "Claude · x@example.com", session: 62, weekly: 80, reason: null },
      { key: "b", name: "codex", session: null, weekly: 50, reason: null },
      { key: "c", name: "Codex", session: null, weekly: null, reason: "probe failed" },
    ]);
  });
});
