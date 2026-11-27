// @effect-diagnostics nodeBuiltinImport:off -- a synchronous hash inside a pure mapping; the readers of this file may run without the Digest service.
/**
 * Fork (fleet usage, fork issue 1146): this machine's own accounts, ids hashed, for its
 * `t3_environment_read` answer. A new kind of usage (the Antigravity usage command,
 * fork/antigravityUsage) joins as one more entry in `FLEET_USAGE_SOURCES`.
 */
import * as NodeCrypto from "node:crypto";

import {
  ForkUsageReading,
  type ForkUsageSnapshot,
  PROVIDER_DISPLAY_NAMES,
  type ServerProvider,
  type ServerProviderUsageLimits,
} from "@t3tools/contracts";
import { providerAccountIdentity, providersWithLimits } from "@t3tools/shared/usageLimits";
import * as Clock from "effect/Clock";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";

import { ProviderRegistry } from "../../provider/ProviderRegistry.ts";
import { UsageLimitSources } from "../../usage/UsageLimitSources.ts";

/** One account a source found, before its id is hashed. Nothing here leaves the machine. */
export interface FleetUsageSourceAccount {
  readonly driver: string;
  /**
   * The account's stable id, the same on every machine that uses it (driver and e-mail, or the
   * credential fingerprint, with the workspace), in upstream's Limits format; `null` when the
   * provider gives none, so the account is shown for this machine only.
   */
  readonly identity: string | null;
  /** Names the account on this machine when `identity` is `null` (the instance id). */
  readonly local: string;
  readonly name: string;
  readonly plan?: string | undefined;
  readonly limits: ServerProviderUsageLimits;
}

export interface FleetUsageSource {
  /** For the log only. */
  readonly id: string;
  /** Reads the source's services only if they are there; never fails. */
  readonly read: Effect.Effect<ReadonlyArray<FleetUsageSourceAccount>>;
  /** Whether this machine has the source at all; a machine with none leaves `usage` out. */
  readonly present: Effect.Effect<boolean>;
}

/** One source's read; longer, and it gives nothing for this read. */
const SOURCE_READ_TIMEOUT = Duration.seconds(2);

const driverName = (driver: string) =>
  (PROVIDER_DISPLAY_NAMES as Record<string, string | undefined>)[driver] ?? driver;

/** Upstream's Limits account key (packages/shared/src/usageLimits.ts `accountKey`), with workspace. */
const hubIdentity = (account: {
  readonly driver: string;
  readonly email?: string | undefined;
  readonly workspaceId?: string | undefined;
  readonly usageLimits: ServerProviderUsageLimits;
}): string | null => {
  const email = account.email?.trim().toLowerCase();
  const base = email
    ? `${account.driver}:${email}`
    : account.usageLimits.credentialFingerprint
      ? `${account.driver}:credential:${account.usageLimits.credentialFingerprint}`
      : null;
  if (base === null) return null;
  return account.workspaceId ? `${base}:${account.workspaceId}` : base;
};

/** The providers this machine runs, as its Limits show them (`providersWithLimits`). */
export const providerSource: FleetUsageSource = {
  id: "providers",
  present: Effect.map(Effect.serviceOption(ProviderRegistry), Option.isSome),
  read: Effect.gen(function* () {
    const registry = yield* Effect.serviceOption(ProviderRegistry);
    if (Option.isNone(registry)) return [];
    const providers: ReadonlyArray<ServerProvider> = yield* registry.value.getProviders;
    return providersWithLimits(providers).flatMap((provider) => {
      const limits = provider.usageLimits;
      if (limits === undefined) return [];
      const identity = providerAccountIdentity(provider);
      return [
        {
          driver: provider.driver,
          identity:
            identity === null
              ? null
              : identity.workspaceId
                ? `${identity.base}:${identity.workspaceId}`
                : identity.base,
          local: provider.instanceId,
          name: provider.displayName?.trim() || driverName(provider.driver),
          plan: provider.auth.label?.trim() || undefined,
          limits,
        },
      ];
    });
  }),
};

/** The usage hubs this machine reads (`usageLimitSources`), each account named by its hub. */
const hubSource: FleetUsageSource = {
  id: "hubs",
  present: Effect.map(Effect.serviceOption(UsageLimitSources), Option.isSome),
  read: Effect.gen(function* () {
    const sources = yield* Effect.serviceOption(UsageLimitSources);
    if (Option.isNone(sources)) return [];
    const snapshots = yield* sources.value.current;
    return snapshots.flatMap((source) =>
      source.accounts.map((account) => ({
        driver: account.driver,
        identity: hubIdentity(account),
        local: `${source.id}:${account.id}`,
        name: `${driverName(account.driver)} (${source.label})`,
        plan: account.plan,
        limits: account.usageLimits,
      })),
    );
  }),
};

/** Every source of this machine's usage. One more provider source is one more entry. */
const FLEET_USAGE_SOURCES: ReadonlyArray<FleetUsageSource> = [providerSource, hubSource];

/** Never the id itself: a hash, the same on every machine for the same account. */
export const accountKey = (account: Pick<FleetUsageSourceAccount, "identity" | "local">) =>
  NodeCrypto.createHash("sha256")
    .update(
      account.identity === null
        ? `t3-fleet-usage-local\n${account.local}`
        : `t3-fleet-usage\n${account.identity}`,
    )
    .digest("hex")
    .slice(0, 32);

const unavailableReason = (limits: ServerProviderUsageLimits): string | undefined => {
  if (limits.unavailable?.reason === "unsupported")
    return limits.unavailable.message ?? "This account has no subscription limits.";
  if (limits.unavailable?.reason === "probeFailed")
    return limits.unavailable.message ?? "The provider's limits could not be read.";
  return limits.windows.length === 0 ? "The provider reports no limits for it." : undefined;
};

const isReading = Schema.is(ForkUsageReading);

const toReading = (account: FleetUsageSourceAccount): ForkUsageReading => {
  const unavailable = unavailableReason(account.limits);
  return {
    key: accountKey(account),
    shared: account.identity !== null,
    driver: account.driver,
    name: account.name,
    ...(account.plan ? { plan: account.plan } : {}),
    checkedAt: account.limits.checkedAt,
    windows: unavailable === undefined ? account.limits.windows : [],
    ...(unavailable === undefined ? {} : { unavailable }),
  };
};

/**
 * This machine's snapshot, or `null` when it has none of the sources (a server wired without
 * them, as in a test): then `usage` is left out of `t3_environment_read`, as on an older build.
 * The same account found twice here (a provider and a hub) is kept once, the fresher reading.
 */
export const readSnapshot = Effect.fn("ForkFleetUsage.readSnapshot")(function* (
  sources: ReadonlyArray<FleetUsageSource> = FLEET_USAGE_SOURCES,
) {
  const present = yield* Effect.forEach(sources, (source) => source.present);
  if (!present.some(Boolean)) return null;
  const found = yield* Effect.forEach(
    sources,
    (source) =>
      source.read.pipe(
        Effect.timeoutOption(SOURCE_READ_TIMEOUT),
        Effect.map(Option.getOrElse(() => [])),
        Effect.catchCause((cause) =>
          Effect.logDebug("fleet usage: a source could not be read", source.id, cause).pipe(
            Effect.as([]),
          ),
        ),
      ),
    { concurrency: "unbounded" },
  );
  const byKey = new Map<string, ForkUsageReading>();
  // A reading the schema would refuse is dropped, so it can never fail the whole answer.
  for (const reading of found.flat().map(toReading).filter(isReading)) {
    const seen = byKey.get(reading.key);
    if (seen === undefined || Date.parse(reading.checkedAt) > Date.parse(seen.checkedAt))
      byKey.set(reading.key, reading);
  }
  const snapshot: ForkUsageSnapshot = {
    readAt: yield* Clock.currentTimeMillis,
    accounts: [...byKey.values()],
  };
  return snapshot;
});
