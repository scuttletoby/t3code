/**
 * Fork (placement): a forwarded `t3_environment_read` answer is decoded with
 * P3's schema before the rule engine reads it. An older build's answer (neither field) is "no
 * facts"; an answer the schema refuses is reported as not valid, never as a busy or unfit machine.
 */
import { describe, expect, it } from "@effect/vitest";

import {
  choosePlacement,
  type PlacementMachine,
} from "../../../../packages/shared/src/forkPlacement.ts";
import { factsFromAnswer } from "./environmentFacts.ts";

const NOW = 1_800_000_000_000;

const host = {
  sampledAt: NOW,
  cpuUtilization: 0.25,
  cpuCount: 8,
  availableMemoryBytes: 8 * 1024 ** 3,
  totalMemoryBytes: 16 * 1024 ** 3,
};
const placement = { labels: ["gpu"], available: true, slotsFree: 2, factsAt: 5 };
/** Upstream's own fields of the answer, which the facts ignore. */
const upstream = { environmentId: "environment:b", label: "B", platform: { os: "linux" } };

const linked = (facts: PlacementMachine["facts"]): PlacementMachine => ({
  environmentId: "b",
  here: false,
  link: {
    reachable: true,
    expiresAt: NOW + 60_000,
    access: "full-access",
    betaHere: true,
    betaThere: true,
  },
  facts,
  workspace: { matchingCheckouts: 1, providerPresent: true, modelPresent: true },
});

const place = (facts: PlacementMachine["facts"], named?: string) =>
  choosePlacement({
    mode: "enforce",
    task: {
      runtimeMode: "full-access",
      needs: [],
      never: [],
      fromPlacedChild: false,
      ...(named === undefined ? {} : { named }),
    },
    machines: [linked(facts)],
    now: NOW,
  });

describe("factsFromAnswer", () => {
  it("a good answer: both fields as sent, the receipt time ours; a newer build's field ignored", () => {
    const answer = { ...upstream, hostResources: host, placement: { ...placement, gpus: 2 } };
    expect(factsFromAnswer(answer, NOW)).toEqual({
      hostResources: host,
      placement,
      receivedAt: NOW,
    });
    expect(place(factsFromAnswer(answer, NOW))).toMatchObject({
      kind: "picked",
      environmentId: "b",
    });
  });

  it("neither field (an older build): no facts, so named it meets only the rules that need none", () => {
    for (const answer of [upstream, {}]) {
      expect(factsFromAnswer(answer, NOW)).toBeNull();
    }
    const facts = factsFromAnswer(upstream, NOW);
    expect(place(facts)).toMatchObject({ kind: "none" });
    expect(place(facts, "b")).toMatchObject({
      kind: "picked",
      why: "b was named and passes the hard rules that need no facts (H1, H3, H7, H9).",
    });
  });

  it("one field only: facts with that field (partial, refused unless here)", () => {
    expect(factsFromAnswer({ placement }, NOW)).toEqual({ placement, receivedAt: NOW });
    expect(place(factsFromAnswer({ placement }, NOW))).toMatchObject({
      kind: "none",
      reason: expect.stringContaining("The machine sent no CPU and memory facts."),
    });
  });

  it.each([
    ["a wrong type", { placement: { ...placement, available: "yes" } }],
    ["a negative count", { placement: { ...placement, slotsFree: -1 } }],
    ["CPU above 1", { hostResources: { ...host, cpuUtilization: 2 } }],
    ["no facts time", { placement: { labels: [], available: true } }],
    ["not an object", "facts"],
  ])("%s: reported as not valid, also when named", (_name, answer) => {
    const facts = factsFromAnswer(answer, NOW);
    expect(facts).toMatchObject({ receivedAt: NOW, problem: expect.any(String) });
    expect(facts).not.toHaveProperty("placement");
    for (const named of [undefined, "b"]) {
      const result = place(facts, named);
      expect(result.kind).toBe("none");
      expect(result.kind === "none" && result.reason).toContain(
        "H2: The machine's facts are not valid: ",
      );
    }
  });

  it('no answer at all (null or undefined): no facts, never "facts not valid"', () => {
    for (const answer of [null, undefined]) {
      expect(factsFromAnswer(answer, NOW)).toBeNull();
      const result = place(factsFromAnswer(answer, NOW));
      expect(result.kind === "none" && result.reason).toContain(
        "H2: The machine sent no facts (no answer, or an older build).",
      );
      expect(result.kind === "none" && result.reason).not.toContain("not valid");
      expect(place(factsFromAnswer(answer, NOW), "b")).toMatchObject({
        kind: "picked",
        why: "b was named and passes the hard rules that need no facts (H1, H3, H7, H9).",
      });
    }
  });

  it("one machine over time: never answered, empty, old, then answers again: ranked by what it sent", () => {
    const answer = { hostResources: host, placement };
    // Past the 15-second freshness window, within the link's minute.
    const LATER = NOW + 30_000;
    const reasonAt = (facts: ReturnType<typeof factsFromAnswer>, now: number) => {
      const result = choosePlacement({
        mode: "enforce",
        task: { runtimeMode: "full-access", needs: [], never: [], fromPlacedChild: false },
        machines: [linked(facts)],
        now,
      });
      return result.kind === "none" ? result.reason : result.kind;
    };
    // Never answered (the read gave nothing back): no facts.
    expect(reasonAt(factsFromAnswer(null, NOW), NOW)).toContain("The machine sent no facts");
    // Answered with empty facts (an older build): no facts, the same.
    expect(reasonAt(factsFromAnswer({}, NOW), NOW)).toContain("The machine sent no facts");
    // Answered, but long ago: too old, not "not valid".
    const old = factsFromAnswer(answer, NOW);
    expect(reasonAt(old, LATER)).toContain("The machine's facts are too old.");
    // Answers again later: the new answer is fresh and it is picked.
    expect(reasonAt(factsFromAnswer(answer, LATER), LATER)).toBe("picked");
    for (const facts of [null, {}, answer]) {
      expect(reasonAt(factsFromAnswer(facts, NOW), LATER)).not.toContain("not valid");
    }
  });

  it("runs again: the same answer decodes the same", () => {
    const answer = { hostResources: host, placement };
    expect(factsFromAnswer(answer, NOW)).toEqual(factsFromAnswer(answer, NOW));
  });
});
