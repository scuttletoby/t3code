/**
 * Fork (fleet usage, fork issue 1146): the fleet's accounts from each machine's answer, with fakes
 * for a duplicate account, an unreachable machine, a stale reading and an old version. A number is
 * shown only from a fresh reading of a machine that answered; otherwise unknown with why.
 */
import type { EnvironmentId, ForkUsageReading, ForkUsageSnapshot } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";

import {
  type MachineRound,
  mergeFleet,
  REASON_NEVER_ANSWERED,
  REASON_OLD_VERSION,
  STALE_AFTER_MS,
} from "./fleetMerge.ts";

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

const NOW = Date.parse("2026-11-27T12:00:00Z");
const MINUTE = 60_000;

const reading = (over: Partial<ForkUsageReading> = {}): ForkUsageReading => ({
  key: "a1",
  shared: true,
  driver: "claudeAgent",
  name: "Claude",
  plan: "Max",
  checkedAt: iso(NOW - MINUTE),
  windows: [{ id: "session", kind: "session", label: "Session", usedPercent: 40 }],
  ...over,
});

const snapshot = (accounts: Array<ForkUsageReading>, readAt = NOW): ForkUsageSnapshot => ({
  readAt,
  accounts,
});

const machine = (
  id: string,
  over: Partial<MachineRound> & { snapshot?: ForkUsageSnapshot | null } = {},
): MachineRound => {
  const { snapshot: given, ...rest } = over;
  const shot = given === undefined ? snapshot([reading()]) : given;
  return {
    environmentId: id as EnvironmentId,
    label: `Machine ${id}`,
    here: false,
    now: { _tag: "answered", snapshot: shot },
    last: shot === null ? null : { snapshot: shot, receivedAt: NOW },
    ...rest,
  };
};

describe("mergeFleet", () => {
  it("shows an account two machines use once, from the fresher reading, naming both machines", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [
        machine("here", { here: true }),
        machine("b", {
          snapshot: snapshot([
            reading({
              checkedAt: iso(NOW - 5 * 1000),
              windows: [{ id: "session", kind: "session", label: "Session", usedPercent: 70 }],
            }),
          ]),
        }),
      ],
    });
    expect(answer.accounts).toHaveLength(1);
    const [account] = answer.accounts;
    expect(account?.state).toBe("known");
    expect(account?.machines.map((m) => m.environmentId)).toEqual(["b", "here"]);
    expect(account?.readFrom?.environmentId).toBe("b");
    expect(account?.windows[0]?.usedPercent).toBe(70);
    expect(account?.ageMs).toBe(5000);
  });

  it("never merges accounts without a stable id, even with the same local key", () => {
    const local = reading({ key: "same", shared: false });
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [
        machine("here", { here: true, snapshot: snapshot([local]) }),
        machine("b", { snapshot: snapshot([local]) }),
      ],
    });
    expect(answer.accounts).toHaveLength(2);
    expect(answer.accounts.every((account) => account.machines.length === 1)).toBe(true);
  });

  it("shows an unreachable machine unknown with why, and its own account unknown, never 0 or 100", () => {
    const silent = machine("b", {
      now: { _tag: "failed", reason: "The linked environment stopped answering." },
      last: { snapshot: snapshot([reading({ key: "only-b" })]), receivedAt: NOW - 3 * MINUTE },
    });
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [machine("here", { here: true }), silent],
    });
    const b = answer.machines.find((m) => m.environmentId === "b");
    expect(b).toMatchObject({
      state: "unknown",
      reason: "The linked environment stopped answering.",
      answeredAgoMs: 3 * MINUTE,
    });
    const onlyB = answer.accounts.find((account) => account.key === "only-b");
    expect(onlyB).toMatchObject({ state: "unknown", windows: [] });
    expect(onlyB?.reason).toBe(
      "Machine b did not answer: The linked environment stopped answering.",
    );
    expect(onlyB?.machines.map((m) => m.environmentId)).toEqual(["b"]);
  });

  it("keeps a shared account known from a machine that answered when another is silent", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [
        machine("here", { here: true }),
        machine("b", {
          now: { _tag: "failed", reason: "It did not answer within 10 seconds." },
          last: { snapshot: snapshot([reading()]), receivedAt: NOW - MINUTE },
        }),
      ],
    });
    expect(answer.accounts).toHaveLength(1);
    expect(answer.accounts[0]).toMatchObject({
      state: "known",
      readFrom: { environmentId: "here" },
    });
    expect(answer.accounts[0]?.machines).toHaveLength(2);
  });

  it("shows a reading older than the stale limit unknown with its age", () => {
    const old = iso(NOW - STALE_AFTER_MS - 10 * MINUTE);
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [machine("b", { snapshot: snapshot([reading({ checkedAt: old })]) })],
    });
    expect(answer.accounts[0]).toMatchObject({ state: "unknown", windows: [] });
    expect(answer.accounts[0]?.reason).toBe("Last read 40 min ago on Machine b, too old to show.");
  });

  it("ages a reading by its own machine's clock, not this server's", () => {
    // Machine b's clock runs an hour ahead: its reading is a minute old by its own clock.
    const ahead = NOW + 60 * MINUTE;
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [
        machine("b", {
          snapshot: snapshot([reading({ checkedAt: iso(ahead - MINUTE) })], ahead),
        }),
      ],
    });
    expect(answer.accounts[0]).toMatchObject({ state: "known", ageMs: MINUTE });
  });

  it("gives the provider's reason when it reports no limits for an account", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: false,
      machines: [
        machine("here", {
          here: true,
          snapshot: snapshot([reading({ windows: [], unavailable: "Could not read limits." })]),
        }),
      ],
    });
    expect(answer.accounts[0]).toMatchObject({
      state: "unknown",
      reason: "Machine here: Could not read limits.",
    });
  });

  it("shows a machine on an older version unknown, with why", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [machine("here", { here: true }), machine("old", { snapshot: null })],
    });
    expect(answer.machines.find((m) => m.environmentId === "old")).toMatchObject({
      state: "unknown",
      reason: REASON_OLD_VERSION,
    });
    expect(answer.machines.find((m) => m.environmentId === "old")?.answeredAgoMs).toBeUndefined();
    expect(answer.accounts).toHaveLength(1);
  });

  it("names a machine that never answered", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [
        machine("b", { now: { _tag: "failed", reason: REASON_NEVER_ANSWERED }, last: null }),
      ],
    });
    expect(answer.machines[0]).toMatchObject({ state: "unknown", reason: REASON_NEVER_ANSWERED });
    expect(answer.accounts).toEqual([]);
  });

  it("lists this machine first", () => {
    const answer = mergeFleet({
      answeredAt: NOW,
      linked: true,
      machines: [machine("a"), machine("z", { here: true })],
    });
    expect(answer.machines.map((m) => m.environmentId)).toEqual(["z", "a"]);
  });
});
