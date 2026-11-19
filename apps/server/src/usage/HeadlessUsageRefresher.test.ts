import { assert, describe, it } from "@effect/vitest";
import {
  AuthSessionId,
  type BackgroundScope,
  DEFAULT_SERVER_SETTINGS,
  defaultInstanceIdForDriver,
  type HostPowerSnapshot,
  ProviderDriverKind,
  ProviderInstanceId,
  RpcClientId,
  type ServerProvider,
  ServerSettings,
  ServerSettingsError,
} from "@t3tools/contracts";
import type { ProviderInstance } from "@t3tools/provider-core/server/driver";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as PubSub from "effect/PubSub";
import * as Queue from "effect/Queue";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import * as BackgroundPolicy from "../background/BackgroundPolicy.ts";
import * as HostPowerMonitor from "../background/HostPowerMonitor.ts";
import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import * as HeadlessUsageRefresher from "./HeadlessUsageRefresher.ts";
import {
  backoffMs,
  effectiveIntervalMs,
  make,
  REFRESH_TIMEOUT_MS,
  type RefresherDeps,
  type RefreshTarget,
} from "./HeadlessUsageRefresher.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const INSTANCE = defaultInstanceIdForDriver(CLAUDE);
const SECOND = ProviderInstanceId.make("claude_second");
const NOW_MS = Date.parse("2026-11-14T12:00:00.000Z");
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const INTERVAL = 10 * MINUTE;

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

const provider = (
  checkedAtMs: number,
  extra?: Partial<ServerProvider>,
  instanceId: ProviderInstanceId = INSTANCE,
): ServerProvider => ({
  instanceId,
  driver: CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: { status: "authenticated" },
  checkedAt: iso(checkedAtMs),
  models: [],
  slashCommands: [],
  skills: [],
  usageLimits: {
    checkedAt: iso(checkedAtMs),
    windows: [{ id: "seven_day", kind: "weekly", label: "Weekly", usedPercent: 40 }],
  },
  ...extra,
});

type Outcome =
  | "ok"
  | "noReading"
  | "error"
  | "hang"
  | "unavailableReading"
  | "readingThenError"
  | "readingThenHang";

/** A provider instance whose refreshUsage stamps a new reading at the clock's time, or fails. */
const fakeTarget = (
  initial: ServerProvider,
  options?: {
    readonly awaitingFirstCheck?: () => boolean;
    readonly checksOnInterval?: () => boolean;
    readonly enabled?: boolean;
    readonly noRefreshUsage?: boolean;
  },
) => {
  let snapshot = initial;
  let calls = 0;
  let interrupts = 0;
  let outcome: Outcome = "ok";
  let gate: Deferred.Deferred<void> | undefined;
  const refreshUsage = Effect.gen(function* () {
    calls += 1;
    if (gate !== undefined) yield* Deferred.await(gate);
    if (outcome === "hang") {
      return yield* Effect.never.pipe(Effect.onInterrupt(() => Effect.sync(() => interrupts++)));
    }
    if (outcome === "error") {
      return yield* new ServerSettingsError({
        settingsPath: "settings.json",
        operation: "read-file",
      });
    }
    const now = iso(DateTime.toEpochMillis(yield* DateTime.now));
    snapshot =
      outcome === "noReading"
        ? { ...snapshot, checkedAt: now }
        : {
            ...snapshot,
            checkedAt: now,
            usageLimits: {
              ...snapshot.usageLimits!,
              checkedAt: now,
              unavailable: outcome === "unavailableReading" ? { reason: "probeFailed" } : undefined,
            },
          };
    if (outcome === "readingThenHang") return yield* Effect.never;
    if (outcome === "readingThenError") {
      return yield* new ServerSettingsError({
        settingsPath: "settings.json",
        operation: "read-file",
      });
    }
  });
  const target: RefreshTarget = {
    instanceId: initial.instanceId,
    enabled: options?.enabled ?? true,
    snapshot: {
      getSnapshot: Effect.sync(() => snapshot),
      isAwaitingFirstCheck: Effect.sync(() => options?.awaitingFirstCheck?.() ?? false),
      ...(options?.checksOnInterval
        ? { checksOnInterval: Effect.sync(options.checksOnInterval) }
        : {}),
      ...(options?.noRefreshUsage ? {} : { refreshUsage }),
    },
  };
  return {
    target,
    calls: () => calls,
    interrupts: () => interrupts,
    setOutcome: (next: Outcome) => {
      outcome = next;
    },
    setGate: (next: Deferred.Deferred<void> | undefined) => {
      gate = next;
    },
    setSnapshot: (next: ServerProvider) => {
      snapshot = next;
    },
  };
};

const decodeSettings = Schema.decodeSync(ServerSettings);
const settingsWith = (input: Record<string, unknown>) => decodeSettings(input as never);

const deps = (
  targets: ReadonlyArray<RefreshTarget>,
  overrides?: Partial<RefresherDeps>,
): RefresherDeps => ({
  listTargets: Effect.sync(() => targets),
  getSettings: Effect.succeed(DEFAULT_SERVER_SETTINGS),
  clientRefreshes: () => Effect.succeed(false),
  hostConstrained: () => Effect.succeed(false),
  ...overrides,
});

const logCollector = () => {
  const warnings: Array<string> = [];
  const infos: Array<string> = [];
  const logger = Logger.make(({ logLevel, message }) => {
    const text = String(Array.isArray(message) ? message[0] : message);
    if (logLevel === "Warn") warnings.push(text);
    if (logLevel === "Info") infos.push(text);
  });
  return { warnings, infos, layer: Logger.layer([logger], { mergeWithExisting: false }) };
};

const startAt = (ms: number) => TestClock.setTime(ms);

describe("HeadlessUsageRefresher", () => {
  it("settings: 10 minutes by default, 0 turns it off, never faster than the client loop", () => {
    assert.equal(Duration.toMillis(DEFAULT_SERVER_SETTINGS.headlessUsageRefreshInterval), INTERVAL);
    assert.equal(effectiveIntervalMs(DEFAULT_SERVER_SETTINGS), INTERVAL);
    assert.equal(effectiveIntervalMs(settingsWith({ headlessUsageRefreshInterval: 0 })), null);
    // The client loop refreshes every 5 minutes (balanced): a 1-minute setting waits 5.
    assert.equal(
      effectiveIntervalMs(settingsWith({ headlessUsageRefreshInterval: MINUTE })),
      5 * MINUTE,
    );
    // Battery saver refreshes clients every 15 minutes.
    assert.equal(
      effectiveIntervalMs(
        settingsWith({ backgroundActivity: { schemaVersion: 1, profile: "battery-saver" } }),
      ),
      15 * MINUTE,
    );
    // Client refresh turned off: the headless one is off too.
    assert.equal(
      effectiveIntervalMs(
        settingsWith({
          backgroundActivity: {
            schemaVersion: 1,
            profile: "custom",
            baseProfile: "balanced",
            overrides: { providerHealthRefreshInterval: 0 },
          },
        }),
      ),
      null,
    );
    // A custom 1-second client interval cannot make the headless one faster than a minute.
    assert.equal(
      effectiveIntervalMs(
        settingsWith({
          headlessUsageRefreshInterval: 1000,
          backgroundActivity: {
            schemaVersion: 1,
            profile: "custom",
            baseProfile: "balanced",
            overrides: { providerHealthRefreshInterval: 1000 },
          },
        }),
      ),
      MINUTE,
    );
  });

  it.effect("refreshes a stale signed-in instance once, then waits a full interval", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      const { sweep } = yield* make(deps([fake.target]));
      yield* sweep;
      assert.equal(fake.calls(), 1);
      // Second run right after: the new check is fresh.
      yield* sweep;
      assert.equal(fake.calls(), 1);
      yield* TestClock.adjust(INTERVAL - 1000);
      yield* sweep;
      assert.equal(fake.calls(), 1);
      yield* TestClock.adjust(1000);
      yield* sweep;
      assert.equal(fake.calls(), 2);
    }),
  );

  it.effect(
    "a restart mid-interval waits for the rest of it (no repeat of the start-up check)",
    () =>
      Effect.gen(function* () {
        yield* startAt(NOW_MS);
        // Upstream checked the provider at start-up 3 minutes ago; this refresher is new (no memory).
        const fake = fakeTarget(provider(NOW_MS - 3 * MINUTE));
        const { sweep } = yield* make(deps([fake.target]));
        yield* sweep;
        assert.equal(fake.calls(), 0);
        yield* TestClock.adjust(7 * MINUTE);
        yield* sweep;
        assert.equal(fake.calls(), 1);
      }),
  );

  it.effect("waits for an instance's first check, and does nothing with no instances", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      let awaiting = true;
      // A snapshot hydrated from an old cache file, while the first probe still runs.
      const fake = fakeTarget(provider(NOW_MS - 2 * 24 * HOUR), {
        awaitingFirstCheck: () => awaiting,
      });
      const empty = yield* make(deps([]));
      yield* empty.sweep;
      const { sweep } = yield* make(deps([fake.target]));
      yield* sweep;
      assert.equal(fake.calls(), 0);
      awaiting = false;
      yield* sweep;
      assert.equal(fake.calls(), 1);
    }),
  );

  it.effect("never refreshes a missing, signed-out, disabled or unsupported instance", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const old = NOW_MS - HOUR;
      const cases = [
        fakeTarget(provider(old, { installed: false })),
        fakeTarget(provider(old, { auth: { status: "unauthenticated" } })),
        fakeTarget(provider(old, { auth: { status: "unknown" } })),
        fakeTarget(provider(old, { enabled: false })),
        fakeTarget(provider(old), { enabled: false }),
        fakeTarget(provider(old, { availability: "unavailable" })),
        fakeTarget(provider(old, { usageLimits: undefined })),
        fakeTarget(
          provider(old, {
            usageLimits: {
              checkedAt: iso(old),
              windows: [],
              unavailable: { reason: "unsupported" },
            },
          }),
        ),
      ];
      const noRefresh = fakeTarget(provider(old), { noRefreshUsage: true });
      const log = logCollector();
      const { sweep } = yield* make(deps([...cases.map((c) => c.target), noRefresh.target]));
      yield* sweep.pipe(Effect.provide(log.layer));
      assert.deepEqual(
        cases.map((c) => c.calls()),
        cases.map(() => 0),
      );
      assert.deepEqual(log.warnings, []);
    }),
  );

  it.effect("refreshes an instance whose last probe failed, as long as it is signed in", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const old = NOW_MS - HOUR;
      const fake = fakeTarget(
        provider(old, {
          status: "error",
          usageLimits: { checkedAt: iso(old), windows: [], unavailable: { reason: "probeFailed" } },
        }),
      );
      const { sweep } = yield* make(deps([fake.target]));
      yield* sweep;
      assert.equal(fake.calls(), 1);
    }),
  );

  it.effect("skips an instance upstream's own loop skips, such as a healthy OpenCode", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const old = NOW_MS - HOUR;
      const opencode = { driver: ProviderDriverKind.make("opencode") };
      let healthy = true;
      // A check of a healthy OpenCode instance starts an OpenCode server; upstream skips it.
      const skipped = fakeTarget(provider(old, opencode), { checksOnInterval: () => !healthy });
      const otherDriver = fakeTarget(provider(old, {}, SECOND));
      const { sweep } = yield* make(deps([skipped.target, otherDriver.target]));
      yield* sweep;
      assert.equal(skipped.calls(), 0);
      assert.equal(otherDriver.calls(), 1);
      // Unhealthy (for example its usage probe failed): upstream checks it, and so does this.
      healthy = false;
      yield* sweep;
      assert.equal(skipped.calls(), 1);
    }),
  );

  it.effect("a healthy skip ends a failure streak: the next outage is logged, short retry", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const opencode = { driver: ProviderDriverKind.make("opencode") };
      let healthy = false;
      const fake = fakeTarget(provider(NOW_MS - HOUR, opencode), {
        checksOnInterval: () => !healthy,
      });
      fake.setOutcome("noReading");
      const log = logCollector();
      const { sweep } = yield* make(deps([fake.target]));
      const tick = (ms: number) =>
        TestClock.adjust(ms).pipe(Effect.andThen(sweep), Effect.provide(log.layer));
      // Three failures in a row: one warning.
      yield* tick(0);
      yield* tick(2 * INTERVAL);
      yield* tick(4 * INTERVAL);
      assert.equal(fake.calls(), 3);
      assert.equal(log.warnings.length, 1);
      // A client recovers usage; the instance is healthy, so it is skipped for days.
      healthy = true;
      const recoveredAt = NOW_MS + 6 * INTERVAL + MINUTE;
      yield* TestClock.setTime(recoveredAt);
      fake.setSnapshot(provider(recoveredAt, opencode));
      yield* tick(0);
      assert.equal(fake.calls(), 3);
      assert.deepEqual(log.infos, ["headless usage refresh works again"]);
      yield* tick(3 * 24 * HOUR);
      assert.equal(fake.calls(), 3);
      // A new outage: retried at once, logged as a new streak, retried after 2 intervals.
      healthy = false;
      yield* tick(0);
      assert.equal(fake.calls(), 4);
      assert.equal(log.warnings.length, 2);
      yield* tick(2 * INTERVAL);
      assert.equal(fake.calls(), 5);
      // Skipped again while healthy: the logged end is not repeated.
      healthy = true;
      yield* tick(0);
      yield* tick(0);
      assert.equal(log.infos.length, 2);
    }),
  );

  it.effect("a skip that is not healthy keeps the failure streak (no false works again)", () =>
    Effect.gen(function* () {
      const opencode = { driver: ProviderDriverKind.make("opencode") };
      const nowMs = NOW_MS + 6 * INTERVAL + MINUTE;
      // Upstream's loop also skips these, but no reading can be taken in any of them.
      const notHealthy: ReadonlyArray<readonly [string, Partial<ServerProvider>]> = [
        ["signed out (server answers 401)", { auth: { status: "unauthenticated" } }],
        ["CLI gone", { installed: false }],
        // Re-enabled in settings while the snapshot still says disabled.
        ["snapshot still disabled", { enabled: false }],
        ["provider unavailable", { availability: "unavailable" }],
        ["no usage limits", { usageLimits: undefined }],
        [
          "usage unsupported",
          {
            usageLimits: {
              checkedAt: iso(nowMs),
              windows: [],
              unavailable: { reason: "unsupported" },
            },
          },
        ],
        [
          "reading unavailable",
          {
            usageLimits: {
              checkedAt: iso(nowMs),
              windows: [],
              unavailable: { reason: "probeFailed" },
            },
          },
        ],
      ];
      for (const [label, extra] of notHealthy) {
        yield* startAt(NOW_MS);
        let skipped = false;
        const fake = fakeTarget(provider(NOW_MS - HOUR, opencode), {
          checksOnInterval: () => !skipped,
        });
        fake.setOutcome("noReading");
        const log = logCollector();
        const { sweep } = yield* make(deps([fake.target]));
        const tick = (ms: number) =>
          TestClock.adjust(ms).pipe(Effect.andThen(sweep), Effect.provide(log.layer));
        yield* tick(0);
        yield* tick(2 * INTERVAL);
        yield* tick(4 * INTERVAL);
        assert.equal(log.warnings.length, 1, label);
        skipped = true;
        yield* TestClock.setTime(nowMs);
        fake.setSnapshot(provider(nowMs, { ...opencode, ...extra }));
        yield* tick(0);
        yield* tick(0);
        assert.deepEqual(log.infos, [], label);
        // A usable reading later ends the streak, logged once.
        fake.setSnapshot(provider(nowMs, opencode));
        yield* tick(0);
        yield* tick(0);
        assert.deepEqual(log.infos, ["headless usage refresh works again"], label);
      }
    }),
  );

  it.effect(
    "a comeback after a skip in a bad state keeps the streak's retry delay (same outage)",
    () =>
      Effect.gen(function* () {
        yield* startAt(NOW_MS);
        let skipped = false;
        const fake = fakeTarget(provider(NOW_MS - HOUR), { checksOnInterval: () => !skipped });
        fake.setOutcome("noReading");
        const log = logCollector();
        const { sweep } = yield* make(deps([fake.target]));
        const at = (ms: number) =>
          TestClock.setTime(NOW_MS + ms).pipe(Effect.andThen(sweep), Effect.provide(log.layer));
        // One failure at T: one warning, retry at T+20.
        yield* at(0);
        assert.equal(fake.calls(), 1);
        assert.equal(log.warnings.length, 1);
        // T+1: the re-enable race; upstream skips it while the snapshot still says disabled.
        skipped = true;
        fake.setSnapshot(provider(NOW_MS + MINUTE, { enabled: false }));
        yield* at(MINUTE);
        // Re-enabled and checked at T+1: due by interval at T+11, but held by the retry at T+20.
        skipped = false;
        fake.setSnapshot(provider(NOW_MS + MINUTE));
        yield* at(INTERVAL + MINUTE);
        yield* at(2 * INTERVAL - 1000);
        assert.equal(fake.calls(), 1);
        yield* at(2 * INTERVAL);
        assert.equal(fake.calls(), 2);
        // That refresh fails too: failure 2 of the same streak, no new warning, retry 4 intervals on.
        assert.equal(log.warnings.length, 1);
        assert.deepEqual(log.infos, []);
        yield* at(6 * INTERVAL - 1000);
        assert.equal(fake.calls(), 2);
        fake.setOutcome("ok");
        yield* at(6 * INTERVAL);
        assert.equal(fake.calls(), 3);
        assert.deepEqual(log.infos, ["headless usage refresh works again"]);
      }),
  );

  it.effect(
    "leaves the refresh to upstream while a client lease holds, and pauses on a constrained host",
    () =>
      Effect.gen(function* () {
        yield* startAt(NOW_MS);
        const fake = fakeTarget(provider(NOW_MS - HOUR));
        let lease = true;
        let constrained = false;
        const { sweep } = yield* make(
          deps([fake.target], {
            clientRefreshes: (id) => Effect.sync(() => lease && id === INSTANCE),
            hostConstrained: () => Effect.sync(() => constrained),
          }),
        );
        yield* sweep;
        assert.equal(fake.calls(), 0);
        lease = false;
        constrained = true;
        yield* sweep;
        assert.equal(fake.calls(), 0);
        constrained = false;
        yield* sweep;
        assert.equal(fake.calls(), 1);
      }),
  );

  it.effect("a host that locks during a sweep stops the rest of that sweep", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const first = fakeTarget(provider(NOW_MS - HOUR));
      const second = fakeTarget(provider(NOW_MS - HOUR, {}, SECOND));
      // The host locks while the first refresh runs, and unlocks later.
      let locked = () => first.calls() > 0;
      const { sweep } = yield* make(
        deps([first.target, second.target], {
          hostConstrained: () => Effect.sync(() => locked()),
        }),
      );
      yield* sweep;
      assert.equal(first.calls(), 1);
      assert.equal(second.calls(), 0);
      locked = () => false;
      yield* sweep;
      assert.equal(first.calls(), 1);
      assert.equal(second.calls(), 1);
    }),
  );

  it.effect("off switch: interval 0 refreshes nothing", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      const { sweep } = yield* make(
        deps([fake.target], {
          getSettings: Effect.succeed(settingsWith({ headlessUsageRefreshInterval: 0 })),
        }),
      );
      yield* sweep;
      assert.equal(fake.calls(), 0);
    }),
  );

  it.effect("never faster than the client loop: a 1-minute setting waits 5 minutes", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - 2 * MINUTE));
      const { sweep } = yield* make(
        deps([fake.target], {
          getSettings: Effect.succeed(settingsWith({ headlessUsageRefreshInterval: MINUTE })),
        }),
      );
      yield* sweep;
      assert.equal(fake.calls(), 0);
      yield* TestClock.adjust(3 * MINUTE);
      yield* sweep;
      assert.equal(fake.calls(), 1);
    }),
  );

  it.effect("backs off after failures, logs a failing streak once and its end once", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      fake.setOutcome("noReading");
      const log = logCollector();
      const { sweep } = yield* make(deps([fake.target]));
      const tick = (ms: number) =>
        TestClock.adjust(ms).pipe(Effect.andThen(sweep), Effect.provide(log.layer));
      yield* tick(0);
      assert.equal(fake.calls(), 1);
      // First wait: 2 intervals.
      yield* tick(2 * INTERVAL - 1000);
      assert.equal(fake.calls(), 1);
      yield* tick(1000);
      assert.equal(fake.calls(), 2);
      // Second wait: 4 intervals; errors count as failures too.
      fake.setOutcome("error");
      yield* tick(4 * INTERVAL - 1000);
      assert.equal(fake.calls(), 2);
      yield* tick(1000);
      assert.equal(fake.calls(), 3);
      assert.deepEqual(log.warnings, ["headless usage refresh failed; retrying with backoff"]);
      fake.setOutcome("ok");
      yield* tick(8 * INTERVAL);
      assert.equal(fake.calls(), 4);
      assert.deepEqual(log.infos, ["headless usage refresh works again"]);
      // Back to the plain interval, and a new streak is logged again.
      fake.setOutcome("noReading");
      yield* tick(INTERVAL);
      assert.equal(fake.calls(), 5);
      assert.equal(log.warnings.length, 2);
    }),
  );

  it.effect("a streak whose retry is overdue by more than two hours is over: a new outage", () =>
    Effect.gen(function* () {
      // One refresh fails at T (retry at T+20); the instance is then skipped as signed out until
      // it comes back, signed in, past its retry by `overdue`, and the refresh fails again.
      const cases = [
        [2 * HOUR, false],
        [2 * HOUR + 1, true],
        [72 * HOUR, true],
      ] as const;
      for (const [overdue, newStreak] of cases) {
        const label = `overdue ${overdue} ms`;
        yield* startAt(NOW_MS);
        let signedOut = false;
        const fake = fakeTarget(provider(NOW_MS - HOUR), { checksOnInterval: () => !signedOut });
        fake.setOutcome("noReading");
        const log = logCollector();
        const { sweep } = yield* make(deps([fake.target]));
        const at = (ms: number) =>
          TestClock.setTime(NOW_MS + ms).pipe(Effect.andThen(sweep), Effect.provide(log.layer));
        yield* at(0);
        assert.equal(log.warnings.length, 1, label);
        signedOut = true;
        fake.setSnapshot(provider(NOW_MS, { auth: { status: "unauthenticated" } }));
        const backAt = 2 * INTERVAL + overdue;
        for (let ms = HOUR; ms < backAt; ms += HOUR) yield* at(ms);
        assert.equal(fake.calls(), 1, label);
        signedOut = false;
        fake.setSnapshot(provider(NOW_MS + backAt - HOUR));
        yield* at(backAt);
        assert.equal(fake.calls(), 2, label);
        // A new streak: logged again, and retried after 2 intervals, not 4.
        assert.equal(log.warnings.length, newStreak ? 2 : 1, label);
        yield* at(backAt + 2 * INTERVAL);
        assert.equal(fake.calls(), newStreak ? 3 : 2, label);
        yield* at(backAt + 4 * INTERVAL);
        assert.equal(fake.calls(), 3, label);
      }
    }),
  );

  it.effect("a sweep in the same millisecond as a failed refresh waits out the backoff", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      // An error leaves the last check an hour old, so the instance stays due.
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      fake.setOutcome("error");
      const { sweep } = yield* make(deps([fake.target]));
      yield* sweep;
      yield* sweep;
      assert.equal(fake.calls(), 1);
      yield* TestClock.adjust(2 * INTERVAL);
      yield* sweep;
      assert.equal(fake.calls(), 2);
    }),
  );

  it("backoff doubles and stops at two hours (or the interval, if longer)", () => {
    assert.equal(backoffMs(INTERVAL, 1), 2 * INTERVAL);
    assert.equal(backoffMs(INTERVAL, 3), 8 * INTERVAL);
    assert.equal(backoffMs(INTERVAL, 20), 2 * HOUR);
    assert.equal(backoffMs(3 * HOUR, 1), 3 * HOUR);
  });

  it.effect("a refresh that hangs times out, counts as a failure and is interrupted", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      fake.setOutcome("hang");
      const log = logCollector();
      const { sweep } = yield* make(deps([fake.target]));
      const fiber = yield* sweep.pipe(Effect.provide(log.layer), Effect.forkChild);
      yield* TestClock.adjust(REFRESH_TIMEOUT_MS);
      yield* Fiber.join(fiber);
      assert.equal(fake.calls(), 1);
      assert.equal(fake.interrupts(), 1);
      assert.equal(log.warnings.length, 1);
      // The next pass is not stuck behind the hung one; it waits out the backoff.
      fake.setOutcome("ok");
      yield* TestClock.adjust(2 * INTERVAL);
      yield* sweep;
      assert.equal(fake.calls(), 2);
    }),
  );

  it.effect("a refresh works only when it ends well and leaves an available reading", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const cases = ["unavailableReading", "readingThenError", "readingThenHang"] as const;
      for (const outcome of cases) {
        const fake = fakeTarget(provider(NOW_MS - HOUR));
        fake.setOutcome(outcome);
        const log = logCollector();
        const { sweep } = yield* make(deps([fake.target]));
        const fiber = yield* sweep.pipe(Effect.provide(log.layer), Effect.forkChild);
        yield* TestClock.adjust(REFRESH_TIMEOUT_MS);
        yield* Fiber.join(fiber);
        assert.equal(fake.calls(), 1, outcome);
        assert.equal(log.warnings.length, 1, outcome);
      }
    }),
  );

  it.effect("two passes that overlap refresh once", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      const gate = yield* Deferred.make<void>();
      fake.setGate(gate);
      const { sweep } = yield* make(deps([fake.target]));
      const first = yield* sweep.pipe(Effect.forkChild);
      yield* Effect.yieldNow;
      const second = yield* sweep.pipe(Effect.forkChild);
      yield* Effect.yieldNow;
      assert.equal(fake.calls(), 1);
      yield* Deferred.succeed(gate, undefined);
      yield* Fiber.join(first);
      yield* Fiber.join(second);
      assert.equal(fake.calls(), 1);
    }),
  );

  it.effect("a clock that went back neither stalls the refresh nor its backoff", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      // The last check was stamped a day ahead of now: the clock was ahead, then corrected.
      const fake = fakeTarget(provider(NOW_MS + 24 * HOUR));
      const { sweep } = yield* make(deps([fake.target]));
      yield* sweep;
      assert.equal(fake.calls(), 1);
      // A failure at a time far ahead sets its retry far ahead; after the clock goes back it retries.
      fake.setOutcome("noReading");
      yield* TestClock.adjust(INTERVAL);
      yield* sweep;
      assert.equal(fake.calls(), 2);
      fake.setSnapshot(provider(NOW_MS - 24 * HOUR));
      yield* TestClock.setTime(NOW_MS - 12 * HOUR);
      yield* sweep;
      assert.equal(fake.calls(), 3);
    }),
  );

  it.effect("forgets the failures of a removed instance", () =>
    Effect.gen(function* () {
      yield* startAt(NOW_MS);
      const fake = fakeTarget(provider(NOW_MS - HOUR));
      const other = fakeTarget(provider(NOW_MS, {}, SECOND));
      fake.setOutcome("noReading");
      let targets: ReadonlyArray<RefreshTarget> = [fake.target, other.target];
      const log = logCollector();
      const { sweep } = yield* make(deps([], { listTargets: Effect.sync(() => targets) }));
      yield* sweep.pipe(Effect.provide(log.layer));
      targets = [other.target];
      yield* sweep.pipe(Effect.provide(log.layer));
      // Back under the same id, a failure is a new streak: logged, not silently backed off.
      targets = [fake.target, other.target];
      yield* TestClock.adjust(INTERVAL);
      yield* sweep.pipe(Effect.provide(log.layer));
      assert.equal(fake.calls(), 2);
      assert.equal(log.warnings.length, 2);
    }),
  );

  it.effect(
    "the server layer refreshes through the Scheduler only while no client lease holds",
    () =>
      Effect.gen(function* () {
        // A client may watch all providers or one instance; either way upstream's loop refreshes.
        const scopes: ReadonlyArray<BackgroundScope> = [
          { type: "provider-status" },
          { type: "provider-status", instanceId: INSTANCE },
        ];
        for (const scope of scopes) {
          yield* withServerLayer(({ fake, policy, refreshed }) =>
            Effect.gen(function* () {
              // A focused client watches provider status: upstream's loop refreshes, not this job.
              yield* policy.reportClientActivity(AuthSessionId.make("s"), RpcClientId.make(1), {
                clientId: "c",
                clientKind: "web",
                visible: true,
                focused: true,
                recentlyInteracted: true,
                scopes: [scope],
                ttlMs: 60_000,
                observedAt: DateTime.makeUnsafe(NOW_MS),
              });
              yield* TestClock.adjust("10 seconds");
              assert.equal(fake.calls(), 0, JSON.stringify(scope));
              // The client goes away: its lease expires and the job takes over.
              yield* policy.removeRpcClient(AuthSessionId.make("s"), RpcClientId.make(1));
              yield* TestClock.adjust("5 seconds");
              yield* Queue.take(refreshed);
              assert.equal(fake.calls(), 1);
            }),
          );
        }
      }),
  );

  it.effect("the server layer pauses while the host is locked or in low power", () =>
    Effect.gen(function* () {
      const constrained: ReadonlyArray<Partial<HostPowerSnapshot>> = [
        // Default (balanced) settings pause on these; battery pauses only in battery-saver.
        { locked: "true" },
        { lowPowerMode: "true" },
      ];
      for (const power of constrained) {
        let hostPower: HostPowerSnapshot = { ...nominalHostPower, stale: false, ...power };
        yield* withServerLayer(
          ({ fake, refreshed }) =>
            Effect.gen(function* () {
              yield* TestClock.adjust("10 seconds");
              assert.equal(fake.calls(), 0, JSON.stringify(power));
              hostPower = { ...nominalHostPower, stale: false };
              yield* TestClock.adjust("5 seconds");
              yield* Queue.take(refreshed);
              assert.equal(fake.calls(), 1);
            }),
          Effect.sync(() => hostPower),
        );
      }
    }),
  );
});

/** Runs `body` with the refresher's server layer over one stale signed-in instance. */
const withServerLayer = <A, E>(
  body: (ctx: {
    readonly fake: ReturnType<typeof fakeTarget>;
    readonly policy: BackgroundPolicy.BackgroundPolicy["Service"];
    readonly refreshed: Queue.Queue<void>;
  }) => Effect.Effect<A, E>,
  hostPower: Effect.Effect<HostPowerSnapshot> = Effect.succeed(nominalHostPower),
) =>
  Effect.gen(function* () {
    yield* startAt(NOW_MS);
    const fake = fakeTarget(provider(NOW_MS - HOUR));
    const refreshed = yield* Queue.unbounded<void>();
    const target = fake.target;
    const instance = {
      ...target,
      snapshot: {
        ...target.snapshot,
        refreshUsage: target.snapshot.refreshUsage!.pipe(
          Effect.andThen(Queue.offer(refreshed, undefined)),
        ),
      },
    } as unknown as ProviderInstance;
    const registry = Layer.succeed(
      ProviderInstanceRegistry.ProviderInstanceRegistry,
      ProviderInstanceRegistry.ProviderInstanceRegistry.of({
        getInstance: () => Effect.succeed(instance),
        listInstances: Effect.succeed([instance]),
        listUnavailable: Effect.succeed([]),
        streamChanges: Stream.empty,
        subscribeChanges: Effect.die("unused"),
      }),
    );
    const policyLayer = BackgroundPolicy.layer.pipe(
      Layer.provide(Layer.merge(hostPowerLayer(hostPower), ServerSettingsModule.layerTest())),
    );
    return yield* Effect.gen(function* () {
      const policy = yield* BackgroundPolicy.BackgroundPolicy;
      return yield* Effect.scoped(
        Layer.build(
          HeadlessUsageRefresher.layer.pipe(
            Layer.provide(Layer.mergeAll(registry, ServerSettingsModule.layerTest())),
          ),
        ).pipe(Effect.andThen(body({ fake, policy, refreshed }))),
      );
    }).pipe(Effect.provide(policyLayer));
  });

const nominalHostPower: HostPowerSnapshot = {
  source: "unknown",
  idle: "unknown",
  idleSeconds: null,
  locked: "unknown",
  suspended: false,
  onBattery: "unknown",
  lowPowerMode: "unknown",
  thermalState: "unknown",
  stale: true,
  updatedAt: DateTime.makeUnsafe(NOW_MS),
};

const hostPowerLayer = (snapshot: Effect.Effect<HostPowerSnapshot>) =>
  Layer.effect(
    HostPowerMonitor.HostPowerMonitor,
    Effect.gen(function* () {
      const changes = yield* PubSub.sliding<HostPowerSnapshot>(1);
      return HostPowerMonitor.HostPowerMonitor.of({
        snapshot,
        report: () => Effect.void,
        streamChanges: Stream.fromPubSub(changes),
      });
    }),
  );
