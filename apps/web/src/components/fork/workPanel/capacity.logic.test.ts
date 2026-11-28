// Fork (work panel): the capacity bar's numbers and the usage bars. Each test names the question
// it answers.
import type { EnvironmentId, ForkWorkPanelAnswer } from "@t3tools/contracts";
import * as Cause from "effect/Cause";
import * as Option from "effect/Option";
import { AsyncResult } from "effect/reactivity";
import { describe, expect, it } from "vite-plus/test";

import {
  agentsTotalText,
  ciRunnersText,
  type FleetMachine,
  fleetTotal,
  machineAnswerOf,
  machineCapacity,
  memoryTotalText,
  NOTE_RESET,
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
    const view = machineCapacity(machine("m", status), 0);
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
    ].map((view) => machineCapacity(view, 0));
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
    ].map((view) => machineCapacity(view, 0));
    expect(agentsTotalText(fleetTotal(machines, (view) => view.agents))).toBe(
      "Agents: 4 of no limit in use",
    );
  });

  it("an empty fleet, or one where nobody answers, is unknown, not 0", () => {
    expect(agentsTotalText(fleetTotal([], (view) => view.agents))).toBe("Agents: unknown");
    const silent = [machine("off", { kind: "unreachable" })].map((view) =>
      machineCapacity(view, 0),
    );
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
    ).toBe("CI runners (this thread's repositories): 2 busy · free unknown");
    expect(ciRunnersText([])).toBe("CI runners (this thread's repositories): unknown");
  });
});

describe("a machine's read as of now", () => {
  const current = answer({ used: 2, total: 8 });

  it("a failed refresh is failed, not the older answer AsyncResult keeps", () => {
    const failed = AsyncResult.failure<ForkWorkPanelAnswer, Error>(Cause.fail(new Error("down")), {
      previousSuccess: Option.some(AsyncResult.success(current)),
    });
    // AsyncResult.getOrElse hands back the old answer, which read as current before this fix.
    expect(AsyncResult.getOrElse(failed, () => null)).toBe(current);
    const status = machineAnswerOf(failed);
    expect(status).toEqual({ kind: "failed" });
    const received = machineAnswerOf(AsyncResult.success(current, { timestamp: 42 }));
    expect(received).toEqual({ kind: "answered", answer: current, receivedAt: 42 });
    expect(machineCapacity(machine("m", status), 0).agents).toMatchObject({
      state: "unknown",
      reason: REASON_FAILED,
    });
  });

  it("an answer older than three refreshes (a hung read) is unknown with its age", () => {
    const at = Date.parse("2026-11-27T12:00:00Z");
    const old = { ...current, capacity: { ...current.capacity, factsAt: at } };
    const status = { kind: "answered", answer: old } as const;
    expect(machineCapacity(machine("m", status), at + 3 * 60_000).agents.state).toBe("answered");
    const view = machineCapacity(machine("m", status), at + 4 * 60_000);
    expect(view.agents).toMatchObject({ state: "unknown" });
    expect(view.agents.reason).toContain("4 min old");
    expect(view.agents.used).toBeUndefined();
  });
});

describe("usageAccounts", () => {
  const now = Date.parse("2026-11-27T12:00:00Z");
  const checkedAt = new Date(now - 60_000).toISOString();
  const window = (kind: "session" | "weekly", usedPercent: number, resetsAt?: number) => ({
    id: `${kind}-${usedPercent}`,
    kind,
    label: kind,
    usedPercent,
    ...(resetsAt === undefined ? {} : { resetsAt: new Date(resetsAt).toISOString() }),
  });
  const account = (key: string, limits: object, displayName: string | null = "Claude") => ({
    key,
    driver: "claudeAgent",
    displayName,
    limits: { checkedAt, windows: [], ...limits },
  });

  it("gives session and the tightest weekly room left, by label only (no e-mail)", () => {
    const [view] = usageAccounts(
      [
        account("a", {
          windows: [window("session", 38), window("weekly", 20.4), window("weekly", 70)],
        }),
      ],
      now,
    );
    expect(view).toEqual({
      key: "a",
      name: "Claude",
      session: { left: 62, note: null },
      weekly: { left: 30, note: null },
      reason: null,
    });
  });

  it("a window whose reset has passed is unknown, never its old percentage", () => {
    const [view] = usageAccounts(
      [account("a", { windows: [window("session", 90, now - 60_000), window("weekly", 50)] })],
      now,
    );
    expect(view!.session).toEqual({ left: null, note: NOTE_RESET });
    expect(view!.weekly.left).toBe(50);
  });

  it("limits read too long ago, unreported or unavailable are unknown with why", () => {
    const views = usageAccounts(
      [
        account("old", {
          checkedAt: new Date(now - 45 * 60_000).toISOString(),
          windows: [window("session", 10)],
        }),
        account("none", { windows: [window("weekly", 50)] }, null),
        account("off", {
          windows: [window("session", 10)],
          unavailable: { message: "probe failed" },
        }),
      ],
      now,
    );
    expect(
      views.map((view) => [view.name, view.session.left, view.weekly.left, view.reason]),
    ).toEqual([
      ["Claude", null, null, "Last read 45 min ago."],
      ["claudeAgent", null, 50, null],
      ["Claude", null, null, "probe failed"],
    ]);
  });
});
