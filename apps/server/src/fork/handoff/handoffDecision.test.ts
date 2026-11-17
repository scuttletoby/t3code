// Fork: one case per line of the handoff design's "when" rules and its state table: what happens
// when the decision runs again, stops half-way, or meets existing, old or empty state.
import { DEFAULT_HANDOFF_SETTINGS } from "@t3tools/contracts";
import { describe, expect, it } from "vite-plus/test";

import {
  HANDOFF_IDLE_LEAD_MS,
  type HandoffBlocker,
  type HandoffFacts,
  handoffDecision,
} from "./handoffDecision.ts";

const NOW = 1_000_000_000;
const MINUTE = 60_000;
const run = { id: "run-9", kind: "turn", state: "completed" } as const;
const IDS = { commandId: "server:handoff:thread-1:run-9", messageId: "handoff:thread-1:run-9" };

/** A quiet `handoff` thread whose last run just completed at 160K, with the cache 5 minutes warm. */
const facts = (over: Partial<HandoffFacts> = {}): HandoffFacts => ({
  trigger: "turn-end",
  runId: "run-9",
  threadId: "thread-1",
  mode: "handoff",
  source: "agent",
  sizeTokens: 150_000,
  idleTokens: 75_000,
  threadAvailable: true,
  lastRun: run,
  usedTokens: 160_000,
  cacheExpiresAtMs: NOW + 5 * MINUTE,
  nowMs: NOW,
  blockers: [],
  ...over,
});
/** At the idle size, below the backstop. */
const idle = (over: Partial<HandoffFacts> = {}) => facts({ usedTokens: 80_000, ...over });

describe("handoffDecision: when", () => {
  it("hands off at the size backstop when a turn ends quiet", () => {
    expect(handoffDecision(facts())).toEqual({ kind: "handoff", reason: "size", ...IDS });
    expect(handoffDecision(facts({ usedTokens: 150_000 }))).toMatchObject({ reason: "size" });
  });

  it.each<[string, HandoffBlocker]>([
    ["a queued or active run", { kind: "run-active" }],
    ["a pending approval or question", { kind: "request-pending" }],
    ["a child result not yet delivered", { kind: "child-result-waiting" }],
    ["a fork or merge-back being applied", { kind: "fork-or-merge" }],
    ["the session's own background work", { kind: "background-work", detail: "command X" }],
    ["an active goal", { kind: "goal-active" }],
  ])("waits at the backstop on %s, naming it for the chip", (_name, blocker) => {
    expect(handoffDecision(facts({ blockers: [blocker] }))).toEqual({
      kind: "wait",
      blockers: [blocker],
    });
    // The next turn end checks again; once quiet it hands off.
    expect(
      handoffDecision(facts({ runId: "run-10", lastRun: { ...run, id: "run-10" } })),
    ).toMatchObject({ kind: "handoff", commandId: "server:handoff:thread-1:run-10" });
  });

  it("is not blocked by running T3 children (they report into the new session)", () => {
    // The caller lists no blocker for a running delegated child.
    expect(handoffDecision(facts({ blockers: [] }))).toMatchObject({ kind: "handoff" });
  });

  it("sets the idle timer at the idle size, earlier for the agent than for a command", () => {
    const expiry = NOW + 10 * MINUTE;
    expect(handoffDecision(idle({ cacheExpiresAtMs: expiry }))).toEqual({
      kind: "sleep",
      untilMs: expiry - 4 * MINUTE,
    });
    expect(handoffDecision(idle({ cacheExpiresAtMs: expiry, source: "command" }))).toEqual({
      kind: "sleep",
      untilMs: expiry - 2 * MINUTE,
    });
    expect(HANDOFF_IDLE_LEAD_MS).toEqual({ agent: 4 * MINUTE, command: 2 * MINUTE });
  });

  it("hands off when the idle timer fires inside the window, the thread quiet", () => {
    const timer = idle({ trigger: "idle-timer", cacheExpiresAtMs: NOW + 3 * MINUTE });
    expect(handoffDecision(timer)).toEqual({ kind: "handoff", reason: "idle", ...IDS });
    // A turn that ends already inside the window does not wait for a timer.
    expect(handoffDecision({ ...timer, trigger: "turn-end" })).toMatchObject({ reason: "idle" });
  });

  it("hands off when asked, at any size from 20K, also without a size reading", () => {
    const asked = (over: Partial<HandoffFacts>) =>
      handoffDecision(facts({ trigger: "asked", ...over }));
    expect(asked({ usedTokens: 20_000 })).toEqual({ kind: "handoff", reason: "asked", ...IDS });
    expect(asked({ usedTokens: undefined })).toMatchObject({ reason: "asked" });
    expect(asked({ usedTokens: 19_999 })).toEqual({ kind: "skip", reason: "too-small" });
    expect(asked({ blockers: [{ kind: "run-active" }] })).toMatchObject({ kind: "wait" });
    // A person may hand off after a failed run; T3 never does unasked.
    expect(asked({ lastRun: { ...run, state: "failed" } })).toMatchObject({ kind: "handoff" });
  });
});

describe("handoffDecision: never", () => {
  it.each<[string, Partial<HandoffFacts>, string]>([
    ["mode off (the default)", { mode: DEFAULT_HANDOFF_SETTINGS.mode }, "mode-not-handoff"],
    ["mode compact (a thread's opt-out)", { mode: "compact" }, "mode-not-handoff"],
    ["an archived or deleted thread", { threadAvailable: false }, "thread-unavailable"],
    ["a thread with no runs", { lastRun: undefined }, "no-runs"],
    ["a trigger for an older run", { runId: "run-8" }, "newer-run"],
    ["a run that was a handoff", { lastRun: { ...run, kind: "handoff" } }, "last-run-handoff"],
    ["the agent's write turn", { lastRun: { ...run, kind: "handoff-write" } }, "last-run-handoff"],
    ["a run that was a compaction", { lastRun: { ...run, kind: "compact" } }, "last-run-compact"],
    [
      "a failed run (also a usage limit)",
      { lastRun: { ...run, state: "failed" } },
      "last-run-not-completed",
    ],
    ["an interrupted run", { lastRun: { ...run, state: "interrupted" } }, "last-run-not-completed"],
    ["no size reading", { usedTokens: undefined }, "no-size"],
    ["under the idle size", { usedTokens: 74_999 }, "too-small"],
    ["idle off (0) under the backstop", { usedTokens: 80_000, idleTokens: 0 }, "idle-off"],
    [
      "an unknown cache expiry",
      { usedTokens: 80_000, cacheExpiresAtMs: undefined },
      "cache-expiry-unknown",
    ],
    ["an expired cache", { usedTokens: 80_000, cacheExpiresAtMs: NOW }, "cache-expired"],
  ])("skips %s", (_name, over, reason) => {
    expect(handoffDecision(facts(over))).toEqual({ kind: "skip", reason });
  });

  it("drops the idle trigger when the thread is busy by then (no chip)", () => {
    const timer = idle({ trigger: "idle-timer", cacheExpiresAtMs: NOW + MINUTE });
    expect(handoffDecision({ ...timer, blockers: [{ kind: "request-pending" }] })).toEqual({
      kind: "skip",
      reason: "busy",
    });
    // A newer finished run replaced the timer: its own turn end decides.
    expect(handoffDecision({ ...timer, lastRun: { ...run, id: "run-10" } })).toEqual({
      kind: "skip",
      reason: "newer-run",
    });
  });
});

describe("handoffDecision: runs again", () => {
  it("gives the same answer for the same facts", () => {
    for (const input of [facts(), idle(), facts({ blockers: [{ kind: "goal-active" }] })]) {
      expect(handoffDecision(input)).toEqual(handoffDecision(input));
    }
  });

  it("gives every trigger of one run the same ids, so only one handoff can run", () => {
    const window = { usedTokens: 160_000, cacheExpiresAtMs: NOW + MINUTE };
    const ids = (["turn-end", "idle-timer", "asked"] as const).map((trigger) => {
      const decision = handoffDecision(facts({ trigger, ...window }));
      return decision.kind === "handoff" ? [decision.commandId, decision.messageId] : decision;
    });
    expect(ids).toEqual([Object.values(IDS), Object.values(IDS), Object.values(IDS)]);
  });
});
