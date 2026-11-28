// Fork (fleet usage, fork issue 1146): the Usage tab's fleet list from the server's answer.
import type { ForkFleetUsageAnswer } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import { asLimitAccounts, fleetUsageView } from "./fleetUsage.logic";

const NOW = Date.parse("2026-11-27T12:00:00Z");
const MINUTE = 60_000;

const answer = (over: Partial<ForkFleetUsageAnswer> = {}): ForkFleetUsageAnswer => ({
  answeredAt: NOW,
  linked: true,
  machines: [
    { environmentId: "here" as never, label: "Desk", here: true, state: "answered" },
    {
      environmentId: "b" as never,
      label: "Laptop",
      here: false,
      state: "unknown",
      reason: "It did not answer within 10 seconds.",
    },
  ],
  accounts: [
    {
      key: "a1",
      driver: "claudeAgent",
      name: "Claude",
      plan: "Max",
      machines: [
        { environmentId: "here" as never, label: "Desk" },
        { environmentId: "b" as never, label: "Laptop" },
      ],
      state: "known",
      readFrom: { environmentId: "here" as never, label: "Desk" },
      ageMs: 3 * MINUTE,
      windows: [
        {
          id: "session",
          kind: "session",
          label: "Session",
          usedPercent: 38,
          resetsAt: new Date(NOW + 90 * MINUTE).toISOString(),
        },
      ],
    },
    {
      key: "a2",
      driver: "codex",
      name: "Codex",
      machines: [{ environmentId: "b" as never, label: "Laptop" }],
      state: "unknown",
      reason: "Laptop did not answer: It did not answer within 10 seconds.",
      windows: [],
    },
  ],
  ...over,
});

describe("fleetUsageView", () => {
  it("shows nothing with no linked machine or no answer, as the tab always was", () => {
    expect(fleetUsageView(null, NOW)).toBeNull();
    expect(fleetUsageView(answer({ linked: false }), NOW)).toBeNull();
  });

  it("gives a known account's room left, reset, machines and where it was read", () => {
    const view = fleetUsageView(answer(), NOW);
    expect(view?.accounts[0]).toEqual({
      key: "a1",
      title: "Claude · Max",
      machinesText: "Used on Desk, Laptop",
      known: true,
      detail: "Read 3 min ago on Desk",
      windows: [{ id: "session", label: "Session", left: 62, resetText: "resets in 90 min" }],
    });
  });

  it("gives an unknown account its reason and no number", () => {
    const view = fleetUsageView(answer(), NOW);
    expect(view?.accounts[1]).toMatchObject({
      known: false,
      detail: "Unknown: Laptop did not answer: It did not answer within 10 seconds.",
      windows: [],
    });
  });

  it("says why a machine is unknown", () => {
    const view = fleetUsageView(answer(), NOW);
    expect(view?.machines).toEqual([
      { environmentId: "here", known: true, text: "Desk (this server): answered" },
      {
        environmentId: "b",
        known: false,
        text: "Laptop: unknown. It did not answer within 10 seconds.",
      },
    ]);
  });
});

describe("asLimitAccounts", () => {
  it("hands the work panel the same accounts, an unknown one with its reason", () => {
    const accounts = asLimitAccounts(answer());
    expect(accounts[0]).toMatchObject({ key: "a1", displayName: "Claude · Max" });
    expect(accounts[0]?.limits.windows).toHaveLength(1);
    expect(accounts[1]?.limits).toEqual({
      windows: [],
      unavailable: { message: "Laptop did not answer: It did not answer within 10 seconds." },
    });
    expect(asLimitAccounts(null)).toEqual([]);
  });
});
