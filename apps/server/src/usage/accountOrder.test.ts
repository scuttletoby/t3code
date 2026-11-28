/**
 * Fork: how the account order reads an account. Paces come from the real `paceInstance`, so
 * "spent", "stale" and "reset passed" are the pacer's own verdicts, not a copy of its rules.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerProviderUsageWindow,
  type UsagePacingInstanceSettings,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";

import { accountStateOf, describeSkipped, orderCandidateIds } from "./accountOrder.ts";
import { DEFAULT_PACING, paceInstance } from "./UsagePacer.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const CODEX = ProviderDriverKind.make("codex");
const MAIN = ProviderInstanceId.make("claude_main");
const SECOND = ProviderInstanceId.make("claude_second");
const THIRD = ProviderInstanceId.make("claude_third");
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));

/** Weekly window, half way through by default. */
const weekly = (usedPercent: number, resetInMs = 3.5 * DAY): ServerProviderUsageWindow => ({
  id: "seven_day",
  kind: "weekly",
  label: "Weekly",
  usedPercent,
  resetsAt: at(resetInMs),
  windowDurationMins: 7 * 24 * 60,
});

const provider = (
  instanceId: ProviderInstanceId,
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  extra?: {
    readonly driver?: ProviderDriverKind;
    readonly email?: string;
    readonly checkedAtMs?: number;
  },
): ServerProvider => ({
  instanceId,
  driver: extra?.driver ?? CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: { status: "authenticated", email: extra?.email ?? `${instanceId}@example.com` },
  checkedAt: at(extra?.checkedAtMs ?? -MINUTE),
  models: [],
  slashCommands: [],
  skills: [],
  usageLimits: { checkedAt: at(extra?.checkedAtMs ?? -MINUTE), windows },
});

const stateOf = (live: ServerProvider, pacing?: Partial<UsagePacingInstanceSettings>) =>
  accountStateOf(
    paceInstance({
      instanceId: live.instanceId,
      provider: live,
      history: [],
      pacing: { ...DEFAULT_PACING, ...pacing },
      nowMs: NOW_MS,
      staleAfterMs: 30 * MINUTE,
    }),
  );

describe("accountStateOf", () => {
  it("an account at its limit is spent, and the note names the full window", () => {
    const state = stateOf(provider(MAIN, [weekly(100)]));
    assert.strictEqual(state.kind, "spent");
    assert.strictEqual(
      describeSkipped({ instanceId: MAIN, state }),
      "claude_main is at its limit (Weekly 100% used)",
    );
  });

  it("the owner's reserve counts: at 100 minus the reserve the account is spent", () => {
    assert.strictEqual(stateOf(provider(MAIN, [weekly(80)]), { reservePercent: 20 }).kind, "spent");
    assert.strictEqual(stateOf(provider(MAIN, [weekly(79)]), { reservePercent: 20 }).kind, "open");
    // The pacer hands the reserve back over a window's last 12 hours: 6 hours before the reset
    // half of a 20% reserve is left, so 85% is not spent yet.
    assert.strictEqual(
      stateOf(provider(MAIN, [weekly(85, 6 * HOUR)]), { reservePercent: 20 }).kind,
      "open",
    );
  });

  it("an account with room is open, even when it is ahead of its even pace", () => {
    assert.strictEqual(stateOf(provider(MAIN, [weekly(10)])).kind, "open");
    assert.strictEqual(stateOf(provider(MAIN, [weekly(90)])).kind, "open");
  });

  it("stale usage is neither spent nor open: no new work goes there", () => {
    for (const used of [10, 100]) {
      const state = stateOf(provider(MAIN, [weekly(used)], { checkedAtMs: -2 * HOUR }));
      assert.deepStrictEqual(state, { kind: "unknown", reason: "stale" });
    }
  });

  it("a reset in the past: the old full value belongs to the window before, so it is not known", () => {
    const state = stateOf(provider(MAIN, [weekly(100, -MINUTE)]));
    assert.deepStrictEqual(state, { kind: "unknown", reason: "resetPassed" });
  });

  it("a fresh window without a reset clock still counts when it is full", () => {
    const untimed: ServerProviderUsageWindow = {
      id: "seven_day",
      kind: "weekly",
      label: "Weekly",
      usedPercent: 100,
    };
    assert.strictEqual(stateOf(provider(MAIN, [untimed])).kind, "spent");
    assert.deepStrictEqual(stateOf(provider(MAIN, [{ ...untimed, usedPercent: 10 }])), {
      kind: "unknown",
      reason: "noResetClock",
    });
  });

  it("no pace, or no reading, is not known; an account marked no limits is open", () => {
    assert.deepStrictEqual(accountStateOf(undefined), { kind: "unknown", reason: "notRead" });
    assert.deepStrictEqual(stateOf(provider(MAIN, [])), { kind: "unknown", reason: "emptyRead" });
    assert.strictEqual(stateOf(provider(MAIN, [weekly(100)]), { noLimits: true }).kind, "open");
  });
});

describe("orderCandidateIds", () => {
  it("keeps the owner's order; skips the current account, other providers, unknown ids and the same login", () => {
    const current = provider(MAIN, [weekly(100)]);
    const providers = [
      current,
      provider(SECOND, [weekly(10)]),
      provider(THIRD, [weekly(10)]),
      provider(ProviderInstanceId.make("codex"), [weekly(10)], { driver: CODEX }),
      // Another instance of the main login: it shares the main account's limits.
      provider(ProviderInstanceId.make("claude_main_too"), [weekly(100)], {
        email: "claude_main@example.com",
      }),
    ];
    const order = [
      THIRD,
      MAIN,
      ProviderInstanceId.make("codex"),
      ProviderInstanceId.make("gone"),
      ProviderInstanceId.make("claude_main_too"),
      SECOND,
    ];
    assert.deepStrictEqual(orderCandidateIds({ order, current, providers }), [THIRD, SECOND]);
  });
});
