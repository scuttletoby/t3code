/**
 * Fork: each turn start fixes the thread's T3 tool groups (mcpT3ToolRule.ts) on every driver but
 * Claude. It refuses outside `MCP_PROFILE_DRIVERS` when the profile leaves out an outside server, and
 * on every wrapped driver for `builtinTools: "none"` or a profile that cannot be worked out.
 */
import { MCP_BUILTIN_TOOLS_NONE, MCP_PROFILE_ALL, ProviderDriverKind } from "@t3tools/contracts";
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
import {
  MCP_PROFILE_DRIVERS,
  type McpProfileResolved,
  builtinToolsNoneRefusal,
} from "./mcpProfile.ts";
import {
  fixT3ToolSet,
  forgetT3ToolSet,
  nextT3ToolSetTurn,
  refusedT3ToolSet,
  t3ToolSetOf,
} from "./mcpT3ToolSets.ts";
import { CurrentTurnMcpProfile } from "./mcpTurnProfile.ts";

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

/** Whether threads on this driver apply a profile's outside servers themselves. */
const appliesMcpProfiles = (driver: ProviderDriverKind): boolean => MCP_PROFILE_DRIVERS.has(driver);

/**
 * Drivers that fix their threads' T3 tool groups themselves, as each query opens
 * (claudeMcpProfile.ts), and so are not wrapped here. Every other driver's turns are.
 */
const FIXES_OWN_T3_TOOL_SETS: ReadonlySet<ProviderDriverKind> = new Set([
  ProviderDriverKind.make("claudeAgent"),
]);

const refusalReason = (resolved: McpProfileResolved, instance: ProviderInstance): string => {
  // A parent's profile (here, or the one another machine carried) can cut even `all` down.
  const profile = resolved.narrowedByParent
    ? `("${resolved.name}", cut down to its parent thread's)`
    : `"${resolved.name}"`;
  return `This thread's MCP profile ${profile} leaves out some outside MCP servers, and ${instance.driverKind} (provider instance ${instance.instanceId}) does not apply a profile's outside servers (only Claude and Codex do), so this turn was not started: it would get every outside MCP server. Switch the thread to a Claude or Codex instance, or pick "${MCP_PROFILE_ALL}" (or a profile whose servers are "all") for this thread in Settings.`;
};

/**
 * Fixes the thread's T3 tool groups for this turn, then nothing when the driver applies outside
 * servers itself (Codex) or the profile keeps every one; otherwise the refusal. A profile that
 * cannot be worked out refuses with the resolver's reason, as it does on Claude, and keeps only the
 * tools every profile keeps. Returns the profile, so the turn reads it once.
 */
const checkTurnProfile = Effect.fn("McpProfileTurnCheck.check")(function* (
  profileFor: ProfileFor,
  instance: ProviderInstance,
  thread: LineageThread,
) {
  // Drawn before the read: a later turn's set stands over this one's, whichever finishes first.
  const turn = nextT3ToolSetTurn();
  const resolved = yield* profileFor(thread).pipe(
    Effect.tapError((refusal) =>
      Effect.sync(() => fixT3ToolSet(thread.id, refusedT3ToolSet(refusal.reason), turn)),
    ),
    // Stopped half-way: no set stands, so the next request works it out again.
    Effect.onInterrupt(() => Effect.sync(() => forgetT3ToolSet(thread.id, turn))),
  );
  fixT3ToolSet(thread.id, t3ToolSetOf(resolved), turn);
  if (resolved.profile.builtinTools === MCP_BUILTIN_TOOLS_NONE) {
    return yield* new ClaudeMcpProfileError({
      reason: builtinToolsNoneRefusal(resolved.name, instance.driverKind),
    });
  }
  if (appliesMcpProfiles(instance.driverKind) || resolved.profile.servers === "all") {
    return resolved;
  }
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
        // The driver's own checks (Codex's) read this profile rather than work it out again. It is
        // keyed by thread, not turn, so it assumes nothing forked here outlives this turn start:
        // a fiber that did would hand this turn's profile to a later turn of the thread (wider,
        // after a narrowing). Fork long-lived work from the session (openSession), never here.
        Effect.flatMap((resolved) =>
          run(input).pipe(
            Effect.provideService(CurrentTurnMcpProfile, {
              threadId: input.appThread.id,
              resolved,
            }),
          ),
        ),
      );
  return (instance: ProviderInstance): ProviderInstance => {
    if (FIXES_OWN_T3_TOOL_SETS.has(instance.driverKind)) return instance;
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
 * Upstream's registry with every instance but Claude's checked at turn start.
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
