/**
 * Fork: a turn on a provider outside `MCP_PROFILE_DRIVERS` would get every MCP server and T3 tool,
 * so it refuses at start when its thread's profile is narrower than `all` or cannot be worked out.
 * Checked where the turn runs, so every route is covered; so is compaction (a `/compact` prompt on
 * some providers). A steer joins a turn that was checked as it started.
 */
import { MCP_PROFILE_ALL, type ProviderDriverKind } from "@t3tools/contracts";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import type { ProviderInstance } from "@t3tools/provider-core/server/driver";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as SqlClient from "effect/sql/SqlClient";

import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import {
  ClaudeMcpProfileError,
  type LineageThread,
  makeThreadMcpProfiles,
} from "./claudeMcpProfile.ts";
import { isEveryMcpTool, MCP_PROFILE_DRIVERS, type McpProfileResolved } from "./mcpProfile.ts";

type ProfileFor = Effect.Success<typeof makeThreadMcpProfiles>;
/** What a turn start, and a resume, are given that the check reads. */
type TurnIds = Pick<
  ProviderAdapter.ProviderAdapterV2TurnInput,
  "appThread" | "threadId" | "runId" | "providerThread"
>;

/**
 * `source` with `overrides` in place of those properties, every other one read from `source` at each
 * access: a spread would freeze a getter (ACP, Pi and OpenCode v2 replace `providerSession` as the
 * session changes). Over an empty target, so a frozen `source` or a prototype method still works.
 */
const withOverrides = <T extends object>(source: T, overrides: Partial<T>): T => {
  const overridden = (key: PropertyKey): key is keyof T => Object.hasOwn(overrides, key);
  return new Proxy({} as T, {
    get: (_, key) => (overridden(key) ? overrides[key] : Reflect.get(source, key, source)),
    has: (_, key) => overridden(key) || Reflect.has(source, key),
    ownKeys: () => [...new Set([...Reflect.ownKeys(source), ...Reflect.ownKeys(overrides)])],
    getOwnPropertyDescriptor: (_, key) => {
      if (overridden(key)) {
        return { configurable: true, enumerable: true, writable: false, value: overrides[key] };
      }
      const own = Reflect.getOwnPropertyDescriptor(source, key);
      // Reported configurable: the empty target has no such property to match.
      return own === undefined ? undefined : { ...own, configurable: true };
    },
    getPrototypeOf: () => Reflect.getPrototypeOf(source),
    set: (_, key, value) => !overridden(key) && Reflect.set(source, key, value, source),
    deleteProperty: (_, key) => !overridden(key) && Reflect.deleteProperty(source, key),
  });
};

/** Whether threads on this driver apply MCP profiles; any other is checked at each turn start. */
const appliesMcpProfiles = (driver: ProviderDriverKind): boolean => MCP_PROFILE_DRIVERS.has(driver);

const refusalReason = (resolved: McpProfileResolved, instance: ProviderInstance): string => {
  // A parent's profile (here, or the one another machine carried) can cut even `all` down.
  const profile = resolved.narrowedByParent
    ? `("${resolved.name}", cut down to its parent thread's)`
    : `"${resolved.name}"`;
  return `This thread's MCP profile ${profile} is narrower than "${MCP_PROFILE_ALL}", and ${instance.driverKind} (provider instance ${instance.instanceId}) does not apply MCP profiles (only Claude does), so this turn was not started: it would get every MCP server and T3 tool. Switch the thread to a Claude instance, or pick "${MCP_PROFILE_ALL}" for this thread in Settings.`;
};

/**
 * Nothing when the thread's profile is `all`; otherwise the refusal. A profile that cannot be
 * worked out refuses with the resolver's reason, as it does on Claude.
 */
const checkTurnProfile = Effect.fn("McpProfileTurnCheck.check")(function* (
  profileFor: ProfileFor,
  instance: ProviderInstance,
  thread: LineageThread,
) {
  const resolved = yield* profileFor(thread);
  if (isEveryMcpTool(resolved.profile)) return;
  return yield* new ClaudeMcpProfileError({ reason: refusalReason(resolved, instance) });
});

/** The instance with its turns checked first; the same object for the same instance. */
const makeWrap = (profileFor: ProfileFor) => {
  const wrapped = new WeakMap<ProviderInstance, ProviderInstance>();
  const refuseUnless =
    <I extends TurnIds>(
      instance: ProviderInstance,
      run: (input: I) => Effect.Effect<void, ProviderAdapter.ProviderAdapterV2Error>,
    ) =>
    (input: I): Effect.Effect<void, ProviderAdapter.ProviderAdapterV2Error> =>
      checkTurnProfile(profileFor, instance, input.appThread).pipe(
        Effect.mapError(
          (cause) =>
            new ProviderAdapter.ProviderAdapterTurnStartError({
              driver: instance.driverKind,
              threadId: input.threadId,
              providerThreadId: input.providerThread.id,
              runId: input.runId,
              cause,
            }),
        ),
        Effect.andThen(run(input)),
      );
  return (instance: ProviderInstance): ProviderInstance => {
    if (appliesMcpProfiles(instance.driverKind)) return instance;
    const known = wrapped.get(instance);
    if (known !== undefined) return known;
    const adapter = instance.orchestrationAdapter;
    const checked = (
      runtime: ProviderAdapter.ProviderAdapterV2SessionRuntime,
    ): ProviderAdapter.ProviderAdapterV2SessionRuntime =>
      withOverrides(runtime, {
        startTurn: refuseUnless(instance, runtime.startTurn),
        ...(runtime.resumeTurn === undefined
          ? {}
          : { resumeTurn: refuseUnless(instance, runtime.resumeTurn) }),
        ...(runtime.compactThread === undefined
          ? {}
          : { compactThread: refuseUnless(instance, runtime.compactThread) }),
      });
    const next = withOverrides(instance, {
      orchestrationAdapter: withOverrides(adapter, {
        openSession: (input) => adapter.openSession(input).pipe(Effect.map(checked)),
      }),
    });
    wrapped.set(instance, next);
    return next;
  };
};

/**
 * Upstream's registry with every instance outside `MCP_PROFILE_DRIVERS` checked at turn start.
 * Asks for the thread store and the settings service, so a server wired without them is a type
 * error, not a check that reads every thread as `all`.
 */
export const layer = Layer.effect(
  ProviderInstanceRegistry.ProviderInstanceRegistry,
  Effect.gen(function* () {
    const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
    yield* SqlClient.SqlClient;
    yield* ServerSettingsService.ServerSettingsService;
    const wrap = makeWrap(yield* makeThreadMcpProfiles);
    return ProviderInstanceRegistry.ProviderInstanceRegistry.of({
      ...registry,
      getInstance: Effect.fn("McpProfileTurnCheck.getInstance")(function* (instanceId) {
        const instance = yield* registry.getInstance(instanceId);
        return instance === undefined ? undefined : wrap(instance);
      }),
      listInstances: registry.listInstances.pipe(Effect.map((instances) => instances.map(wrap))),
    });
    // The real boot test (holdWork.boot.test.ts) reads these spans: server.ts wires the check, and
    // the orchestration layer's getInstance calls run inside it.
  }).pipe(Effect.withSpan("McpProfileTurnCheck.make")),
);
