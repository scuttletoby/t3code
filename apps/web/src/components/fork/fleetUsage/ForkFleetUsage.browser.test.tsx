// Fork (fleet usage, fork issue 1146): the Usage tab's fleet list in a browser, with the server's
// answer faked. Each test names the question it answers.
import type { ForkFleetUsageAnswer } from "@t3tools/contracts";
import { describe, expect, it, vi } from "vite-plus/test";

import "~/index.css";
import { render } from "../../../../test/render";

const NOW = Date.parse("2026-11-27T12:00:00Z");
const MINUTE = 60_000;

const state = vi.hoisted(() => ({ answer: null as unknown }));

vi.mock(import("./fleetUsageAtom"), () => ({
  useForkFleetUsage: (() => state.answer) as never,
}));

import { ForkFleetUsage } from "./ForkFleetUsage";

const fleet: ForkFleetUsageAnswer = {
  answeredAt: NOW,
  linked: true,
  machines: [
    { environmentId: "here" as never, label: "Desk", here: true, state: "answered" },
    { environmentId: "b" as never, label: "Laptop", here: false, state: "answered" },
    {
      environmentId: "c" as never,
      label: "Build box",
      here: false,
      state: "unknown",
      reason: "The linked environment stopped answering.",
    },
  ],
  accounts: [
    {
      key: "shared",
      driver: "claudeAgent",
      name: "Claude",
      plan: "Max",
      machines: [
        { environmentId: "here" as never, label: "Desk" },
        { environmentId: "b" as never, label: "Laptop" },
      ],
      state: "known",
      readFrom: { environmentId: "b" as never, label: "Laptop" },
      ageMs: 2 * MINUTE,
      windows: [
        { id: "session", kind: "session", label: "Session", usedPercent: 25 },
        { id: "weekly", kind: "weekly", label: "Weekly", usedPercent: 60 },
      ],
    },
    {
      key: "stale",
      driver: "codex",
      name: "Codex",
      machines: [{ environmentId: "c" as never, label: "Build box" }],
      state: "unknown",
      reason: "Build box did not answer: The linked environment stopped answering.",
      windows: [],
    },
  ],
};

describe("ForkFleetUsage", () => {
  it("renders nothing with no linked machine, so the tab is as it always was", async () => {
    state.answer = { ...fleet, linked: false };
    const screen = await render(<ForkFleetUsage now={NOW} />);
    expect(document.querySelector("[data-testid=fork-fleet-usage]")).toBeNull();
    await screen.unmount();
    state.answer = null;
    const empty = await render(<ForkFleetUsage now={NOW} />);
    expect(document.querySelector("[data-testid=fork-fleet-usage]")).toBeNull();
    await empty.unmount();
  });

  it("shows a shared account once, with both machines and where it was read", async () => {
    state.answer = fleet;
    const screen = await render(<ForkFleetUsage now={NOW} />);
    const accounts = document.querySelectorAll("[data-testid=fork-fleet-usage-account]");
    expect(accounts).toHaveLength(2);
    const shared = accounts[0]!;
    expect(shared.getAttribute("data-state")).toBe("known");
    expect(shared.textContent).toContain("Claude · Max");
    expect(shared.textContent).toContain("Used on Desk, Laptop");
    expect(shared.textContent).toContain("75% left");
    expect(shared.textContent).toContain("40% left");
    expect(shared.textContent).toContain("Read 2 min ago on Laptop");
    await screen.unmount();
  });

  it("shows an unreachable machine's account grey and unknown with why, never 0 or 100", async () => {
    state.answer = fleet;
    const screen = await render(<ForkFleetUsage now={NOW} />);
    const stale = document.querySelectorAll("[data-testid=fork-fleet-usage-account]")[1]!;
    expect(stale.getAttribute("data-state")).toBe("unknown");
    expect(stale.textContent).toContain(
      "Unknown: Build box did not answer: The linked environment stopped answering.",
    );
    expect(stale.textContent).not.toMatch(/\d+% left/);
    const machines = [...document.querySelectorAll("[data-testid=fork-fleet-usage-machine]")];
    expect(machines.map((machine) => machine.getAttribute("data-state"))).toEqual([
      "answered",
      "answered",
      "unknown",
    ]);
    expect(machines[2]?.textContent).toBe(
      "Build box: unknown. The linked environment stopped answering.",
    );
    await screen.unmount();
  });
});
