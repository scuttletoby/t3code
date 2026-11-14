/**
 * Fork: refreshes usage limits through upstream's `refreshUsage` while no client lease would,
 * so the usage history gets readings on a headless server. Never more often than upstream's
 * loop, only signed-in instances (no logins), backs off on failures and logs a streak once.
 */
import type {
  ProviderInstanceId,
  ServerProvider,
  ServerSettings,
  ServerSettingsError,
} from "@t3tools/contracts";
import type { ManagedServerProvider } from "@t3tools/provider-core/server/snapshot";
import { resolveServerBackgroundActivitySettings } from "@t3tools/shared/backgroundActivitySettings";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Semaphore from "effect/Semaphore";

import * as BackgroundPolicy from "../background/BackgroundPolicy.ts";
import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as Scheduler from "../scheduling/Scheduler.ts";
import * as ServerSettingsModule from "../serverSettings.ts";

/** The shortest interval honoured, whatever the settings say. */
const MIN_INTERVAL_MS = 60 * 1000;
/** Longest wait after failures, unless the interval itself is longer. */
const MAX_BACKOFF_MS = 2 * 60 * 60 * 1000;
/** A refresh still running after this counts as failed, and is interrupted. */
export const REFRESH_TIMEOUT_MS = 2 * 60 * 1000;
/** A provider check stamped further ahead than this came from a clock that was ahead. */
const FUTURE_SLACK_MS = 5 * 60 * 1000;

export interface RefreshTarget {
  readonly instanceId: ProviderInstanceId;
  readonly enabled: boolean;
  readonly snapshot: Pick<
    ManagedServerProvider,
    "getSnapshot" | "isAwaitingFirstCheck" | "refreshUsage"
  >;
}

export interface RefresherDeps {
  readonly listTargets: Effect.Effect<ReadonlyArray<RefreshTarget>>;
  readonly getSettings: Effect.Effect<ServerSettings, ServerSettingsError>;
  /** True while upstream's own loop would refresh this instance (a client lease). */
  readonly clientRefreshes: (instanceId: ProviderInstanceId) => Effect.Effect<boolean>;
  /** True while the host should do no background work (locked, low power, battery). */
  readonly hostConstrained: (settings: ServerSettings) => Effect.Effect<boolean>;
}

/** The interval in ms, or null when the refresher is off. */
export function effectiveIntervalMs(settings: ServerSettings): number | null {
  const own = Duration.toMillis(settings.headlessUsageRefreshInterval);
  const client = Duration.toMillis(
    resolveServerBackgroundActivitySettings(settings).providerHealthRefreshInterval,
  );
  if (!(own > 0) || !(client > 0)) return null;
  return Math.max(own, client, MIN_INTERVAL_MS);
}

/** Signed in, usage supported, nothing to log in to. */
function canRefresh(snapshot: ServerProvider): boolean {
  return (
    snapshot.enabled &&
    snapshot.installed &&
    snapshot.availability !== "unavailable" &&
    snapshot.auth.status === "authenticated" &&
    snapshot.usageLimits !== undefined &&
    snapshot.usageLimits.unavailable?.reason !== "unsupported"
  );
}

/** Due when the last provider check is an interval old, or stamped by a clock that was ahead. */
function isDue(snapshot: ServerProvider, nowMs: number, intervalMs: number): boolean {
  const checkedMs = Date.parse(snapshot.checkedAt);
  return nowMs - checkedMs >= intervalMs || checkedMs - nowMs > FUTURE_SLACK_MS;
}

/**
 * A refresh worked when it left another, available usage reading. Another, not a later one: a
 * reading stamped by a clock that was ahead must not make every new reading look old.
 */
function gotNewReading(before: ServerProvider, after: ServerProvider): boolean {
  const next = after.usageLimits;
  if (next === undefined || next.unavailable !== undefined) return false;
  return next.checkedAt !== before.usageLimits?.checkedAt;
}

export function backoffMs(intervalMs: number, failures: number): number {
  return Math.min(intervalMs * 2 ** failures, Math.max(MAX_BACKOFF_MS, intervalMs));
}

/** Room for a sweep that runs late: scheduler ticks, and another instance refreshed first. */
const SWEEP_SLACK_MS = REFRESH_TIMEOUT_MS + 60 * 1000;

/**
 * The longest gap between two good readings of one instance when one refresh fails: the failed
 * refresh is due an interval after the last good one, its retry waits `backoffMs(interval, 1)`
 * from that attempt and may run until the timeout; either sweep may start late.
 */
export function oneFailureGapMs(intervalMs: number): number {
  return intervalMs + backoffMs(intervalMs, 1) + REFRESH_TIMEOUT_MS + SWEEP_SLACK_MS;
}

interface FailureState {
  readonly failures: number;
  readonly retryAtMs: number;
  readonly waitMs: number;
}

export const make = Effect.fn("HeadlessUsageRefresher.make")(function* (deps: RefresherDeps) {
  const failing = new Map<ProviderInstanceId, FailureState>();
  const permit = yield* Semaphore.make(1);

  const refreshOne = Effect.fn("HeadlessUsageRefresher.refreshOne")(function* (
    target: RefreshTarget,
    refreshUsage: NonNullable<RefreshTarget["snapshot"]["refreshUsage"]>,
    before: ServerProvider,
    intervalMs: number,
    nowMs: number,
  ) {
    const exit = yield* refreshUsage.pipe(
      Effect.timeoutOption(Duration.millis(REFRESH_TIMEOUT_MS)),
      Effect.exit,
    );
    const after = yield* target.snapshot.getSnapshot;
    const timedOut = Exit.isSuccess(exit) && Option.isNone(exit.value);
    const ok = Exit.isSuccess(exit) && !timedOut && gotNewReading(before, after);
    const previous = failing.get(target.instanceId);
    if (ok) {
      if (previous !== undefined) {
        failing.delete(target.instanceId);
        yield* Effect.logInfo("headless usage refresh works again", {
          instanceId: target.instanceId,
          failures: previous.failures,
        });
      }
      return;
    }
    const failures = (previous?.failures ?? 0) + 1;
    const waitMs = backoffMs(intervalMs, failures);
    failing.set(target.instanceId, { failures, retryAtMs: nowMs + waitMs, waitMs });
    if (previous === undefined) {
      yield* Effect.logWarning("headless usage refresh failed; retrying with backoff", {
        instanceId: target.instanceId,
        reason: timedOut
          ? "timed out"
          : Exit.isFailure(exit)
            ? "refresh failed"
            : "no new usage reading",
        ...(Exit.isFailure(exit) ? { cause: exit.cause } : {}),
        retryInMs: waitMs,
      });
    }
  });

  const sweepOnce = Effect.gen(function* () {
    const settings = yield* deps.getSettings.pipe(Effect.option);
    if (Option.isNone(settings)) return;
    const intervalMs = effectiveIntervalMs(settings.value);
    if (intervalMs === null) return;
    if (yield* deps.hostConstrained(settings.value)) return;
    const targets = yield* deps.listTargets;
    const live = new Set(targets.map((target) => target.instanceId));
    for (const id of failing.keys()) if (!live.has(id)) failing.delete(id);
    for (const target of targets) {
      const refreshUsage = target.snapshot.refreshUsage;
      if (!target.enabled || refreshUsage === undefined) continue;
      if (target.snapshot.isAwaitingFirstCheck && (yield* target.snapshot.isAwaitingFirstCheck)) {
        continue;
      }
      const before = yield* target.snapshot.getSnapshot;
      if (!canRefresh(before)) continue;
      const nowMs = DateTime.toEpochMillis(yield* DateTime.now);
      if (!isDue(before, nowMs, intervalMs)) continue;
      const failure = failing.get(target.instanceId);
      // A wait longer than it was set to means the clock went back; retry now.
      if (
        failure !== undefined &&
        failure.retryAtMs > nowMs &&
        failure.retryAtMs - nowMs <= failure.waitMs
      ) {
        continue;
      }
      if (yield* deps.clientRefreshes(target.instanceId)) continue;
      yield* refreshOne(target, refreshUsage, before, intervalMs, nowMs);
    }
  });

  /** One pass over all instances; a pass that starts while another runs does nothing. */
  const sweep = permit.withPermitsIfAvailable(1)(sweepOnce).pipe(Effect.asVoid);
  return { sweep };
});

const SCHEDULER_SOURCE = "headless-usage-refresh";

export const layer = Layer.effectDiscard(
  Effect.gen(function* () {
    const instances = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
    const policy = yield* BackgroundPolicy.BackgroundPolicy;
    const serverSettings = yield* ServerSettingsModule.ServerSettingsService;
    const scheduler = yield* Scheduler.Scheduler;
    const refresher = yield* make({
      listTargets: instances.listInstances,
      getSettings: serverSettings.getSettings,
      clientRefreshes: (instanceId) =>
        Effect.all([
          policy.shouldRunScopeWork({ type: "provider-status" }),
          policy.shouldRunScopeWork({ type: "provider-status", instanceId }),
        ]).pipe(Effect.map(([generic, instance]) => generic || instance)),
      hostConstrained: (settings) =>
        policy.snapshot.pipe(
          Effect.map((snapshot) =>
            BackgroundPolicy.isHostConstrained(
              snapshot.hostPower,
              resolveServerBackgroundActivitySettings(settings),
            ),
          ),
        ),
    });
    yield* scheduler.register(SCHEDULER_SOURCE, refresher.sweep);
  }),
).pipe(Layer.provide(Scheduler.layer));
