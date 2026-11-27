/**
 * Fork (fleet usage, fork issue 1146): this machine's own accounts as it reports them over a link.
 * The same account hashes the same on every machine; no e-mail or fingerprint leaves the machine;
 * a source that cannot be read gives nothing, and a server with no source reports no usage.
 */
import type { ServerProvider, ServerProviderUsageLimits } from "@t3tools/contracts";
import { describe, expect, it } from "@effect/vitest";
import * as Effect from "effect/Effect";

import { ProviderRegistry } from "../../provider/ProviderRegistry.ts";
import {
  accountKey,
  type FleetUsageSource,
  type FleetUsageSourceAccount,
  providerSource,
  readSnapshot,
} from "./usageSnapshot.ts";

const limits = (over: Partial<ServerProviderUsageLimits> = {}): ServerProviderUsageLimits => ({
  checkedAt: "2026-11-27T11:59:00.000Z",
  windows: [{ id: "session", kind: "session", label: "Session", usedPercent: 25 }],
  ...over,
});

const account = (over: Partial<FleetUsageSourceAccount> = {}): FleetUsageSourceAccount => ({
  driver: "claudeAgent",
  identity: "claudeAgent:someone@example.test",
  local: "claude-main",
  name: "Claude",
  plan: "Max",
  limits: limits(),
  ...over,
});

const source = (
  accounts: ReadonlyArray<FleetUsageSourceAccount>,
  over: Partial<FleetUsageSource> = {},
): FleetUsageSource => ({
  id: "fake",
  present: Effect.succeed(true),
  read: Effect.succeed(accounts),
  ...over,
});

describe("usage snapshot", () => {
  it.effect("hashes the same account the same on two machines, and leaks no id", () =>
    Effect.gen(function* () {
      const fingerprint = limits({ credentialFingerprint: "fp-secret-value" });
      const machineA = yield* readSnapshot([source([account({ limits: fingerprint })])]);
      const machineB = yield* readSnapshot([
        source([account({ local: "claude-other-instance", name: "Claude (laptop)" })]),
      ]);
      expect(machineA?.accounts[0]?.key).toBe(machineB?.accounts[0]?.key);
      expect(machineA?.accounts[0]?.shared).toBe(true);
      const text = JSON.stringify(machineA);
      expect(text).not.toContain("someone@example.test");
      expect(text).not.toContain("fp-secret-value");
      expect(text).not.toContain("claude-main");
    }),
  );

  it.effect("marks an account without a stable id as this machine's only", () =>
    Effect.gen(function* () {
      const shot = yield* readSnapshot([source([account({ identity: null })])]);
      expect(shot?.accounts[0]?.shared).toBe(false);
      expect(shot?.accounts[0]?.key).toBe(accountKey({ identity: null, local: "claude-main" }));
      expect(shot?.accounts[0]?.key).not.toBe(accountKey(account()));
    }),
  );

  it.effect("gives the provider's reason and no windows when it reports no limits", () =>
    Effect.gen(function* () {
      const shot = yield* readSnapshot([
        source([
          account({
            limits: limits({ windows: [], unavailable: { reason: "probeFailed" } }),
          }),
        ]),
      ]);
      expect(shot?.accounts[0]).toMatchObject({
        windows: [],
        unavailable: "The provider's limits could not be read.",
      });
    }),
  );

  it.effect("keeps the other sources when one fails, and one account found twice once", () =>
    Effect.gen(function* () {
      const older = account({ limits: limits({ checkedAt: "2026-11-27T11:00:00.000Z" }) });
      const shot = yield* readSnapshot([
        source([], { read: Effect.die("broken") }),
        source([older]),
        source([account()]),
      ]);
      expect(shot?.accounts).toHaveLength(1);
      expect(shot?.accounts[0]?.checkedAt).toBe("2026-11-27T11:59:00.000Z");
    }),
  );

  it.effect("reports no usage on a server with none of the sources", () =>
    Effect.gen(function* () {
      expect(yield* readSnapshot()).toBeNull();
      expect(yield* readSnapshot([source([], { present: Effect.succeed(false) })])).toBeNull();
    }),
  );

  it.effect("names a provider as it shows it, with its plan", () =>
    Effect.gen(function* () {
      const provider = {
        instanceId: "codex-work",
        driver: "codex",
        displayName: "Codex (work)",
        enabled: true,
        installed: true,
        status: "ready",
        availability: "available",
        auth: { status: "authenticated", email: "dev@example.test", label: "Plus" },
        usageLimits: limits(),
      } as unknown as ServerProvider;
      const accounts = yield* providerSource.read.pipe(
        Effect.provideService(ProviderRegistry, {
          getProviders: Effect.succeed([provider]),
        } as never),
      );
      expect(accounts).toHaveLength(1);
      expect(accounts[0]).toMatchObject({
        driver: "codex",
        name: "Codex (work)",
        plan: "Plus",
        identity: "codex:dev@example.test",
      });
    }),
  );
});
