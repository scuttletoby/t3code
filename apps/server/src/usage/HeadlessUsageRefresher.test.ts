import { assert, describe, it } from "@effect/vitest";
import {
  AuthSessionId,
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
    readonly enabled?: boolean;
    readonly noRefreshUsage?: boolean;
  },
) => {
  let snapshot = initial;
  let calls = 0;
  let outcome: Outcome = "ok";
  let gate: Deferred.Deferred<void> | undefined;
  const refreshUsage = Effect.gen(function* () {
    calls += 1;
    if (gate !== undefined) yield* Deferred.await(gate);
    if (outcome === "hang") return yield* Effect.never;
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
      ...(options?.noRefreshUsage ? {} : { refreshUsage }),
    },
  };
  return {
    target,
    calls: () => calls,
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
          Layer.provide(Layer.merge(hostPowerLayer, ServerSettingsModule.layerTest())),
        );
        yield* Effect.gen(function* () {
          const policy = yield* BackgroundPolicy.BackgroundPolicy;
          // A focused client watches provider status: upstream's loop refreshes, not this job.
          yield* policy.reportClientActivity(AuthSessionId.make("s"), RpcClientId.make(1), {
            clientId: "c",
            clientKind: "web",
            visible: true,
            focused: true,
            recentlyInteracted: true,
            scopes: [{ type: "provider-status" }],
            ttlMs: 60_000,
            observedAt: DateTime.makeUnsafe(NOW_MS),
          });
          yield* Effect.scoped(
            Layer.build(
              HeadlessUsageRefresher.layer.pipe(
                Layer.provide(Layer.mergeAll(registry, ServerSettingsModule.layerTest())),
              ),
            ).pipe(
              Effect.andThen(
                Effect.gen(function* () {
                  yield* TestClock.adjust("10 seconds");
                  assert.equal(fake.calls(), 0);
                  // The client goes away: its lease expires and the job takes over.
                  yield* policy.removeRpcClient(AuthSessionId.make("s"), RpcClientId.make(1));
                  yield* TestClock.adjust("5 seconds");
                  yield* Queue.take(refreshed);
                  assert.equal(fake.calls(), 1);
                }),
              ),
            ),
          );
        }).pipe(Effect.provide(policyLayer));
      }),
  );
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

const hostPowerLayer = Layer.effect(
  HostPowerMonitor.HostPowerMonitor,
  Effect.gen(function* () {
    const changes = yield* PubSub.sliding<HostPowerSnapshot>(1);
    return HostPowerMonitor.HostPowerMonitor.of({
      snapshot: Effect.succeed(nominalHostPower),
      report: () => Effect.void,
      streamChanges: Stream.fromPubSub(changes),
    });
  }),
);
