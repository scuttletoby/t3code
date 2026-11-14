/**
 * Fork (usage pacing part 3): `UsagePacer`. One test per row of the PR's failure table (empty
 * history, one reading, history from before a reset, clock step back, restart, two windows with
 * one label, corrupt or unreadable history, instance removed), then each guard on its own.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  defaultInstanceIdForDriver,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerProviderUsageWindow,
  ServerSettings,
  ServerSettingsPatch,
  type UsagePacingInstanceSettings,
} from "@t3tools/contracts";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import { paceOf as upstreamPaceOf } from "@t3tools/shared/usageLimits";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import { TestClock } from "effect/testing";

import * as ProviderRegistryMock from "../provider/testUtils/providerRegistryMock.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import { effectiveIntervalMs, REFRESH_TIMEOUT_MS } from "./HeadlessUsageRefresher.ts";
import * as UsagePacer from "./UsagePacer.ts";
import {
  currentSeries,
  DEFAULT_PACING,
  type InstancePace,
  make,
  paceInstance,
  staleAfterMs,
  type WindowPace,
} from "./UsagePacer.ts";
import * as UsageReadingsHistory from "./UsageReadingsHistory.ts";
import {
  accountOf,
  isAccountSwap,
  UsageHistoryUnreadable,
  type UsageReading,
} from "./UsageReadingsHistory.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const INSTANCE = defaultInstanceIdForDriver(CLAUDE);
const GONE = ProviderInstanceId.make("claude_gone");
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const STALE = 30 * MINUTE;

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));

/** Weekly window half way through: even pace is 50%. */
const weekly = (
  usedPercent: number,
  resetInMs = 3.5 * DAY,
  extra?: Partial<ServerProviderUsageWindow>,
): ServerProviderUsageWindow => ({
  id: "seven_day",
  kind: "weekly",
  label: "Weekly",
  usedPercent,
  resetsAt: at(resetInMs),
  windowDurationMins: 7 * 24 * 60,
  ...extra,
});

/** A window without a reset time (no pace can be placed on it). */
const open = (
  usedPercent: number,
  id = "seven_day",
  kind: ServerProviderUsageWindow["kind"] = "weekly",
): ServerProviderUsageWindow => ({ id, kind, label: "Weekly", usedPercent });

/** Five-hour window half way through: even pace is 50%. */
const session = (usedPercent: number, resetInMs = 2.5 * HOUR): ServerProviderUsageWindow => ({
  id: "five_hour",
  kind: "session",
  label: "Session",
  usedPercent,
  resetsAt: at(resetInMs),
  windowDurationMins: 5 * 60,
});

const reading = (
  offsetMs: number,
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  extra?: Partial<UsageReading>,
): UsageReading => ({ checkedAt: at(offsetMs), windows, ...extra });

const provider = (
  offsetMs: number,
  windows: ReadonlyArray<ServerProviderUsageWindow>,
  extra?: {
    readonly unavailable?: "unsupported" | "probeFailed";
    readonly instanceId?: ProviderInstanceId;
  },
): ServerProvider => ({
  instanceId: extra?.instanceId ?? INSTANCE,
  driver: CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: { status: "authenticated" },
  checkedAt: at(offsetMs),
  models: [],
  slashCommands: [],
  skills: [],
  usageLimits: {
    checkedAt: at(offsetMs),
    windows,
    ...(extra?.unavailable === undefined ? {} : { unavailable: { reason: extra.unavailable } }),
  },
});

const pace = (
  history: ReadonlyArray<UsageReading> | "unreadable",
  live: ServerProvider | undefined,
  options?: {
    readonly pacing?: Partial<UsagePacingInstanceSettings>;
    readonly nowMs?: number;
    readonly staleAfterMs?: number;
  },
): InstancePace =>
  paceInstance({
    instanceId: INSTANCE,
    provider: live,
    history,
    pacing: { ...DEFAULT_PACING, ...options?.pacing },
    nowMs: options?.nowMs ?? NOW_MS,
    staleAfterMs: options?.staleAfterMs ?? STALE,
  });

const windowOf = (result: InstancePace, id: string): WindowPace => {
  const found = result.windows.find((window) => window.id === id);
  assert.isDefined(found, `window ${id}`);
  return found!;
};

const near = (actual: number | null, expected: number) => {
  assert.isNotNull(actual);
  assert.closeTo(actual!, expected, 1e-9);
};

const settingsWith = (usagePacing: ServerSettings["usagePacing"]): ServerSettings => ({
  ...DEFAULT_SERVER_SETTINGS,
  usagePacing,
});

describe("UsagePacer failure table", () => {
  it("empty history: the live snapshot alone paces; no burn rate from one point", () => {
    const result = pace([], provider(-MINUTE, [weekly(40)]));
    const window = windowOf(result, "seven_day");
    assert.strictEqual(window.verdict, "behind");
    near(window.pacePercent, 50);
    assert.strictEqual(window.usedPercent, 40);
    assert.strictEqual(window.checkedAt, at(-MINUTE));
    assert.isNull(window.burnPercentPerHour);
    assert.strictEqual(result.verdict, "behind");
  });

  it("one reading, also the live one: paced, no burn rate", () => {
    const only = reading(-10 * MINUTE, [weekly(60)]);
    const result = pace([only], provider(-10 * MINUTE, [weekly(60)]));
    const window = windowOf(result, "seven_day");
    assert.strictEqual(window.verdict, "ahead");
    assert.isNull(window.burnPercentPerHour);
    assert.isNull(window.resetDetectedAt);
  });

  it("history from before a reset: burn and confirmations restart with the new window", () => {
    const before = (used: number) => weekly(used, -HOUR);
    const after = (used: number) => weekly(used, 7 * DAY - HOUR);
    const history = [
      reading(-3 * HOUR, [before(70)]),
      reading(-2 * HOUR, [before(85)]),
      reading(-40 * MINUTE, [after(1)]),
      reading(-10 * MINUTE, [after(3)]),
    ];
    const result = pace(history, provider(-10 * MINUTE, [after(3)]));
    const window = windowOf(result, "seven_day");
    assert.strictEqual(window.resetDetectedAt, at(-40 * MINUTE));
    near(window.burnPercentPerHour, 4);
    assert.strictEqual(window.verdict, "onPace");
  });

  it("history from before a reset: a meter drop alone starts a new window", () => {
    // No reset time: only the drop can tell. The window cannot pace, but burn is measured.
    const history = [
      reading(-50 * MINUTE, [open(60)]),
      reading(-30 * MINUTE, [open(2)]),
      reading(-10 * MINUTE, [open(4)]),
    ];
    const window = windowOf(pace(history, provider(-10 * MINUTE, [open(4)])), "seven_day");
    assert.strictEqual(window.resetDetectedAt, at(-30 * MINUTE));
    near(window.burnPercentPerHour, 6);
    assert.strictEqual(window.unknownReason, "noResetClock");
  });

  it("history from before a reset: a reset time jump alone starts a new window", () => {
    // A quiet week: 4% used, then 0% after the reset (a drop under the noise bound).
    const history = [
      reading(-30 * MINUTE, [weekly(4, -5 * MINUTE)]),
      reading(-MINUTE, [weekly(0, 7 * DAY - 5 * MINUTE)]),
    ];
    const window = windowOf(
      pace(history, provider(-MINUTE, [weekly(0, 7 * DAY - 5 * MINUTE)])),
      "seven_day",
    );
    assert.strictEqual(window.resetDetectedAt, at(-MINUTE));
  });

  it("history from before a reset: a live value whose reset time passed is unknown", () => {
    const old = weekly(90, -5 * MINUTE);
    const result = pace([reading(-10 * MINUTE, [old])], provider(-MINUTE, [old]));
    assert.strictEqual(windowOf(result, "seven_day").unknownReason, "resetPassed");
    assert.strictEqual(result.verdict, "unknown");
  });

  it("clock step back (#37): the series starts after the backwards stamp", () => {
    const history = [
      reading(-90 * MINUTE, [weekly(40)]),
      // Stamped by a clock that was ahead, then corrected; thinning may hide the step.
      reading(-5 * MINUTE, [weekly(58)]),
      reading(-50 * MINUTE, [weekly(55)]),
      reading(-40 * MINUTE, [weekly(56)]),
    ];
    const window = windowOf(pace(history, provider(-MINUTE, [weekly(57)])), "seven_day");
    near(window.burnPercentPerHour, (2 / 49) * 60);
    assert.strictEqual(window.checkedAt, at(-MINUTE));
    assert.deepStrictEqual(
      currentSeries(history).map((entry) => entry.checkedAt),
      [at(-50 * MINUTE), at(-40 * MINUTE)],
    );
  });

  it("restart: a snapshot restored with its old stamp is stale until a fresh read", () => {
    const old = reading(-3 * HOUR, [weekly(40), session(10)]);
    const restored = pace([old], provider(-3 * HOUR, [weekly(40), session(10)]));
    assert.strictEqual(restored.verdict, "unknown");
    assert.strictEqual(restored.unknownReason, "stale");
    assert.strictEqual(windowOf(restored, "seven_day").checkedAt, at(-3 * HOUR));

    const reread = pace([old], provider(-MINUTE, [weekly(40), session(10)]));
    assert.strictEqual(reread.verdict, "behind");
    assert.strictEqual(windowOf(reread, "seven_day").checkedAt, at(-MINUTE));
  });

  it("two windows with the same label are paced apart by id", () => {
    const opus = (used: number) => weekly(used, 3.5 * DAY, { id: "seven_day_opus" });
    const history = [reading(-30 * MINUTE, [weekly(40), opus(10)])];
    const result = pace(history, provider(-MINUTE, [weekly(44), opus(80)]));
    assert.strictEqual(windowOf(result, "seven_day").verdict, "behind");
    near(windowOf(result, "seven_day").burnPercentPerHour, (4 / 29) * 60);
    assert.strictEqual(windowOf(result, "seven_day_opus").verdict, "ahead");
    assert.strictEqual(result.verdict, "ahead");
  });

  it("two windows with the same id: live is ambiguous; in history the first one counts", () => {
    const ambiguous = pace([], provider(-MINUTE, [weekly(40), weekly(90)]));
    assert.strictEqual(ambiguous.windows.length, 1);
    assert.strictEqual(windowOf(ambiguous, "seven_day").unknownReason, "ambiguous");
    assert.strictEqual(ambiguous.verdict, "unknown");

    const history = [reading(-20 * MINUTE, [weekly(50), weekly(10)])];
    const window = windowOf(pace(history, provider(-MINUTE, [weekly(51)])), "seven_day");
    assert.isNull(window.resetDetectedAt);
    near(window.burnPercentPerHour, (1 / 19) * 60);
  });

  it("an unreadable history leaves every window unknown, never free", () => {
    const result = pace("unreadable", provider(-MINUTE, [weekly(10), session(5)]));
    assert.strictEqual(result.verdict, "unknown");
    assert.strictEqual(result.unknownReason, "historyUnreadable");
    for (const window of result.windows) {
      assert.strictEqual(window.verdict, "unknown");
      assert.strictEqual(window.unknownReason, "historyUnreadable");
    }
    assert.strictEqual(windowOf(result, "seven_day").usedPercent, 10);
  });

  it("instance removed: unknown, not free", () => {
    const result = pace([reading(-MINUTE, [weekly(10)])], undefined);
    assert.strictEqual(result.verdict, "unknown");
    assert.strictEqual(result.unknownReason, "noInstance");
    assert.deepStrictEqual(result.windows, []);
  });
});

describe("UsagePacer guards", () => {
  it("a window seen in the history but absent now is missing", () => {
    const history = [reading(-10 * MINUTE, [weekly(40), session(10)])];
    const result = pace(history, provider(-MINUTE, [weekly(41)]));
    assert.strictEqual(windowOf(result, "five_hour").unknownReason, "missing");
    assert.strictEqual(result.verdict, "unknown");
    assert.strictEqual(result.unknownReason, "missing");
  });

  it("a value confirmed longer ago than the stale bound is unknown", () => {
    const live = provider(-31 * MINUTE, [weekly(40)]);
    assert.strictEqual(windowOf(pace([], live), "seven_day").unknownReason, "stale");
    assert.strictEqual(
      windowOf(pace([], live, { staleAfterMs: HOUR }), "seven_day").verdict,
      "behind",
    );
  });

  it("a value stamped ahead of the clock is unknown", () => {
    const window = windowOf(pace([], provider(10 * MINUTE, [weekly(40)])), "seven_day");
    assert.strictEqual(window.unknownReason, "aheadOfClock");
    const within = windowOf(pace([], provider(4 * MINUTE, [weekly(40)])), "seven_day");
    assert.strictEqual(within.verdict, "behind");
  });

  it("a streamed update of one window does not confirm another's old value", () => {
    // Full read 2 h ago; since then only session updates streamed in, each carrying the weekly.
    const history = [
      reading(-2 * HOUR, [weekly(40), session(10)]),
      reading(-20 * MINUTE, [weekly(40), session(20)]),
      reading(-5 * MINUTE, [weekly(40), session(30)]),
    ];
    const result = pace(history, provider(-5 * MINUTE, [weekly(40), session(30)]));
    assert.strictEqual(windowOf(result, "seven_day").unknownReason, "stale");
    assert.strictEqual(windowOf(result, "seven_day").checkedAt, at(-2 * HOUR));
    assert.strictEqual(windowOf(result, "five_hour").verdict, "behind");
    assert.strictEqual(result.verdict, "unknown");

    // The same with the live snapshot newer than the history's last reading.
    const newer = pace(history.slice(0, 2), provider(-5 * MINUTE, [weekly(40), session(30)]));
    assert.strictEqual(windowOf(newer, "seven_day").unknownReason, "stale");
  });

  it("a full re-read with nothing changed confirms every window", () => {
    const history = [reading(-2 * HOUR, [weekly(40), session(10)])];
    const result = pace(history, provider(-MINUTE, [weekly(40), session(10)]));
    assert.strictEqual(windowOf(result, "seven_day").checkedAt, at(-MINUTE));
    assert.strictEqual(windowOf(result, "five_hour").checkedAt, at(-MINUTE));
    assert.strictEqual(result.verdict, "behind");
  });

  it("a failed probe keeps old bars under a new stamp: confirms nothing", () => {
    const history = [reading(-2 * HOUR, [weekly(40), session(10)])];
    const result = pace(
      history,
      provider(-MINUTE, [weekly(40), session(10)], { unavailable: "probeFailed" }),
    );
    assert.strictEqual(windowOf(result, "seven_day").unknownReason, "stale");
    assert.strictEqual(windowOf(result, "five_hour").unknownReason, "stale");
  });

  it("without the usage API, rate_limit_event windows still pace", () => {
    const history = [reading(-2 * HOUR, [weekly(40), session(10)], { unavailable: "probeFailed" })];
    const result = pace(
      history,
      provider(-MINUTE, [weekly(40), session(12)], { unavailable: "probeFailed" }),
    );
    assert.strictEqual(windowOf(result, "five_hour").verdict, "behind");
    assert.strictEqual(windowOf(result, "seven_day").unknownReason, "stale");
  });

  it("an unsupported provider has no limits to pace", () => {
    const result = pace([], provider(-MINUTE, [], { unavailable: "unsupported" }));
    assert.strictEqual(result.unknownReason, "noLimits");
  });

  it("burn rate needs ten minutes of cover and never goes negative", () => {
    const short = pace([reading(-6 * MINUTE, [weekly(40)])], provider(-MINUTE, [weekly(42)]));
    assert.isNull(windowOf(short, "seven_day").burnPercentPerHour);

    const falling = pace([reading(-31 * MINUTE, [weekly(43)])], provider(-MINUTE, [weekly(40)]));
    assert.strictEqual(windowOf(falling, "seven_day").burnPercentPerHour, 0);
    assert.isNull(windowOf(falling, "seven_day").resetDetectedAt);
  });

  it("burn rate counts only the recent span", () => {
    const history = [reading(-5 * HOUR, [weekly(10)]), reading(-50 * MINUTE, [weekly(30)])];
    const window = windowOf(pace(history, provider(-10 * MINUTE, [weekly(32)])), "seven_day");
    near(window.burnPercentPerHour, 3);
  });

  it("an account swap starts a new series", () => {
    const history = [
      reading(-40 * MINUTE, [weekly(70)], { account: "aaaa" }),
      reading(-20 * MINUTE, [weekly(10)], { account: "bbbb" }),
    ];
    const window = windowOf(pace(history, provider(-MINUTE, [weekly(11)])), "seven_day");
    assert.isNull(window.resetDetectedAt);
    near(window.burnPercentPerHour, (1 / 19) * 60);
  });

  it("isAccountSwap: different bases or workspaces of one base; knowing less is none (#39)", () => {
    assert.isFalse(isAccountSwap({}, { account: "a" }));
    assert.isTrue(isAccountSwap({ account: "a" }, { account: "b" }));
    assert.isTrue(isAccountSwap({ account: "a.w1" }, { account: "b.w1" }));
    assert.isFalse(isAccountSwap({ account: "a" }, { account: "a.w1" }));
    assert.isFalse(isAccountSwap({ account: "a.w1" }, { account: "a" }));
    assert.isTrue(isAccountSwap({ account: "a.w1" }, { account: "a.w2" }));
    assert.isFalse(isAccountSwap({ account: "a.w1" }, { account: "a.w2", borrowed: true }));
    assert.isFalse(isAccountSwap({ account: "a.w1", borrowed: true }, { account: "a.w2" }));
    assert.isFalse(isAccountSwap({ account: "a.w1" }, { account: "a.w1" }));
  });

  it("the test provider reports no account, so the live reading never swaps", () => {
    assert.isUndefined(accountOf(provider(0, [weekly(1)])));
  });
});

describe("UsagePacer reserve and verdicts", () => {
  it("defaults give upstream's even-pace verdict", () => {
    for (const used of [0, 30, 44, 45, 50, 55, 56, 90, 100]) {
      const window = weekly(used);
      const result = windowOf(pace([], provider(-MINUTE, [window])), "seven_day");
      const expected = { ahead: "ahead", on: "onPace", under: "behind" }[
        upstreamPaceOf(window, NOW_MS)!
      ];
      assert.strictEqual(result.verdict, expected, `used ${used}`);
      assert.strictEqual(result.effectiveReservePercent, 0);
      near(result.targetPercent, 50);
    }
  });

  it("the reserve holds in full until the last 12 hours", () => {
    const result = pace([], provider(-MINUTE, [weekly(40, DAY)]), {
      pacing: { reservePercent: 20 },
    });
    const window = windowOf(result, "seven_day");
    near(window.effectiveReservePercent, 20);
    near(window.targetPercent, 80);
  });

  it("the reserve is released over the window's last 12 hours", () => {
    // 6 h left: pace is ~96%, the reserve is half released, so 90% is the target.
    const result = pace([], provider(-MINUTE, [weekly(96, 6 * HOUR)]), {
      pacing: { reservePercent: 20 },
    });
    const window = windowOf(result, "seven_day");
    near(window.effectiveReservePercent, 10);
    near(window.targetPercent, 90);
    assert.strictEqual(window.verdict, "ahead");
    const free = pace([], provider(-MINUTE, [weekly(96, 6 * HOUR)]));
    assert.strictEqual(windowOf(free, "seven_day").verdict, "onPace");
  });

  it("burn first drops the even pace; only the reserve holds", () => {
    const paced = pace([], provider(-MINUTE, [weekly(80)]));
    assert.strictEqual(windowOf(paced, "seven_day").verdict, "ahead");
    const burn = pace([], provider(-MINUTE, [weekly(80)]), { pacing: { burnFirst: true } });
    near(windowOf(burn, "seven_day").targetPercent, 100);
    assert.strictEqual(windowOf(burn, "seven_day").verdict, "behind");
    const reserved = pace([], provider(-MINUTE, [weekly(97)]), {
      pacing: { burnFirst: true, reservePercent: 10 },
    });
    assert.strictEqual(windowOf(reserved, "seven_day").verdict, "ahead");
  });

  it("a window without a reset clock cannot pace and does not decide the instance", () => {
    const other = open(99, "other", "other");
    const mixed = pace([], provider(-MINUTE, [weekly(40), other]));
    assert.strictEqual(windowOf(mixed, "other").unknownReason, "noResetClock");
    assert.strictEqual(windowOf(mixed, "other").usedPercent, 99);
    assert.strictEqual(mixed.verdict, "behind");
    const alone = pace([], provider(-MINUTE, [other]));
    assert.strictEqual(alone.verdict, "unknown");
    assert.strictEqual(alone.unknownReason, "noResetClock");
    const reserved = pace([], provider(-MINUTE, [other]), { pacing: { reservePercent: 10 } });
    assert.isNull(windowOf(reserved, "other").effectiveReservePercent);
    // Burning first with no reserve needs no clock: the ceiling is 100%.
    const burn = pace([], provider(-MINUTE, [other]), { pacing: { burnFirst: true } });
    assert.strictEqual(windowOf(burn, "other").verdict, "onPace");
  });

  it("ahead outranks unknown, unknown outranks on pace", () => {
    const history = [reading(-10 * MINUTE, [weekly(40), session(10)])];
    const ahead = pace(history, provider(-MINUTE, [weekly(80)]));
    assert.strictEqual(ahead.verdict, "ahead");
    const unknown = pace(history, provider(-MINUTE, [weekly(50)]));
    assert.strictEqual(unknown.verdict, "unknown");
    const onPace = pace([], provider(-MINUTE, [weekly(50), session(10)]));
    assert.strictEqual(onPace.verdict, "onPace");
  });
});

describe("UsagePacer settings", () => {
  const decodeSettings = Schema.decodeUnknownSync(ServerSettings);
  const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

  it("old settings files decode to no pacing; an entry fills its defaults", () => {
    assert.deepStrictEqual(decodeSettings({}).usagePacing, {});
    assert.deepStrictEqual(DEFAULT_SERVER_SETTINGS.usagePacing, {});
    const entry = decodeSettings({ usagePacing: { [INSTANCE]: {} } }).usagePacing[INSTANCE];
    assert.deepStrictEqual(entry, DEFAULT_PACING);
    assert.throws(() => decodeSettings({ usagePacing: { [INSTANCE]: { reservePercent: 101 } } }));
  });

  it("patches merge per instance; null removes one", () => {
    const added = applyServerSettingsPatch(
      DEFAULT_SERVER_SETTINGS,
      decodePatch({ usagePacing: { [INSTANCE]: { reservePercent: 20 } } }),
    );
    const both = applyServerSettingsPatch(
      added,
      decodePatch({ usagePacing: { [GONE]: { burnFirst: true } } }),
    );
    assert.deepStrictEqual(both.usagePacing[INSTANCE], { reservePercent: 20, burnFirst: false });
    assert.deepStrictEqual(both.usagePacing[GONE], { reservePercent: 0, burnFirst: true });
    const removed = applyServerSettingsPatch(both, decodePatch({ usagePacing: { [GONE]: null } }));
    assert.deepStrictEqual(Object.keys(removed.usagePacing), [INSTANCE]);
    const unrelated = applyServerSettingsPatch(
      removed,
      decodePatch({ sidebarAutoSettleOnMerge: true }),
    );
    assert.deepStrictEqual(unrelated.usagePacing, removed.usagePacing);
  });

  it("stale bound: two refresh intervals plus a timeout, never under 30 minutes", () => {
    const off = { ...DEFAULT_SERVER_SETTINGS, headlessUsageRefreshInterval: Duration.zero };
    assert.strictEqual(staleAfterMs(off), STALE);
    const hourly = {
      ...DEFAULT_SERVER_SETTINGS,
      headlessUsageRefreshInterval: Duration.minutes(60),
    };
    const interval = effectiveIntervalMs(hourly);
    assert.isNotNull(interval);
    assert.isAtLeast(interval!, HOUR);
    assert.strictEqual(staleAfterMs(hourly), 2 * interval! + REFRESH_TIMEOUT_MS);
  });
});

it.layer(NodeServices.layer)("UsagePacer service", (it) => {
  it.effect("reads past corrupt and torn history lines", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const root = yield* fs.makeTempDirectoryScoped({ prefix: "usage-pacer-" });
      const directory = path.join(root, "usage-readings");
      yield* fs.makeDirectory(directory, { recursive: true });
      const line = (entry: UsageReading) => `${JSON.stringify(entry)}\n`;
      yield* fs.writeFileString(
        path.join(directory, `${INSTANCE}.jsonl`),
        [
          line(reading(-40 * MINUTE, [weekly(30)])),
          "not json\n",
          `${JSON.stringify({ checkedAt: "yesterday", windows: 3 })}\n`,
          line(reading(-20 * MINUTE, [weekly(32)])),
          `{"checkedAt":"${at(-10 * MINUTE)}","wind`,
        ].join(""),
      );
      const history = yield* UsageReadingsHistory.make({ directory });
      const pacer = make({
        getProviders: Effect.succeed([provider(-MINUTE, [weekly(33)])]),
        readings: history.readings,
        getSettings: Effect.succeed(DEFAULT_SERVER_SETTINGS),
      });
      const result = yield* pacer.paceOf(INSTANCE);
      const window = windowOf(result, "seven_day");
      near(window.burnPercentPerHour, (3 / 39) * 60);
      assert.strictEqual(result.verdict, "behind");
    }),
  );

  it.effect("an unreadable history is unknown; removed instances with settings are listed", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const pacer = make({
        getProviders: Effect.succeed([provider(-MINUTE, [weekly(10)])]),
        readings: (instanceId) =>
          Effect.fail(new UsageHistoryUnreadable({ instanceId, detail: "permission denied" })),
        getSettings: Effect.succeed(
          settingsWith({ [GONE]: { reservePercent: 10, burnFirst: false } }),
        ),
      });
      const all = yield* pacer.paceAll;
      assert.deepStrictEqual(
        all.map((entry) => [entry.instanceId, entry.unknownReason]),
        [
          [INSTANCE, "historyUnreadable"],
          [GONE, "noInstance"],
        ],
      );
      assert.strictEqual(all[1]!.reservePercent, 10);
    }),
  );

  it.effect("the layer paces from the registry, history and settings with the clock", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const asked: Array<number | undefined> = [];
      const historyStub = UsageReadingsHistory.UsageReadingsHistory.of({
        record: () => Effect.void,
        readings: (_instanceId, options) => {
          asked.push(options?.sinceMs);
          return Effect.succeed([{ ...reading(-30 * MINUTE, [weekly(40)]), segment: 0 }]);
        },
        readingsOfAccount: () => Effect.succeed([]),
        compact: Effect.void,
      });
      const result = yield* Effect.gen(function* () {
        const pacer = yield* UsagePacer.UsagePacer;
        return yield* pacer.paceOf(INSTANCE);
      }).pipe(
        Effect.provide(
          UsagePacer.layer.pipe(
            Layer.provide(ProviderRegistryMock.layer([provider(-MINUTE, [weekly(80)])])),
            Layer.provide(Layer.succeed(UsageReadingsHistory.UsageReadingsHistory, historyStub)),
            Layer.provide(
              ServerSettingsModule.layerTest({
                usagePacing: { [INSTANCE]: { reservePercent: 5, burnFirst: true } },
              }),
            ),
          ),
        ),
      );
      assert.deepStrictEqual(asked, [NOW_MS - 8 * DAY]);
      assert.strictEqual(result.reservePercent, 5);
      assert.isTrue(result.burnFirst);
      assert.strictEqual(result.verdict, "behind");
      near(windowOf(result, "seven_day").burnPercentPerHour, (40 / 29) * 60);
    }),
  );
});
