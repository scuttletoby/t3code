/**
 * Fork: a turn on a provider that cannot apply MCP profiles (all but Claude) refuses when its
 * thread's profile is narrower than `all`. The check wraps upstream's provider instance
 * registry as server.ts wires it, over upstream's settings test service and a real thread store
 * (SQLite in memory), so each route a thread takes to such a provider meets it at turn start.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  EnvironmentId,
  EventId,
  type OrchestrationV2AppThread,
  type OrchestrationV2DelegatedFrom,
  ProjectId,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  type ServerSettings,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import type { ProviderInstance } from "@t3tools/provider-core/server/driver";
import { makeProviderFailure } from "@t3tools/provider-core/server/failure";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as PubSub from "effect/PubSub";
import * as Stream from "effect/Stream";

import * as ServerConfig from "../config.ts";
import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ProviderInstanceRegistry from "../provider/ProviderInstanceRegistry.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import * as ForkLayers from "./forkLayers.ts";
import { MCP_PROFILE_DRIVERS } from "./mcpProfile.ts";

const CLAUDE = ProviderInstanceId.make("claudeAgent");
const CODEX = ProviderInstanceId.make("codex");
const OPENCODE = ProviderInstanceId.make("opencode-work");
const DRIVERS: Record<string, ProviderDriverKind> = {
  [CLAUDE]: ProviderDriverKind.make("claudeAgent"),
  [CODEX]: ProviderDriverKind.make("codex"),
  [OPENCODE]: ProviderDriverKind.make("opencode"),
};
const PROJECT = ProjectId.make("project");
const POLICY = ProviderAdapter.ProviderAdapterV2RuntimePolicy.make({
  runtimeMode: "full-access",
  interactionMode: "default",
  cwd: "/workspace",
});
const T3_ONLY = { name: "t3-only", servers: [], t3Tools: "all" } as const;

type Mcp = Partial<
  Pick<
    ServerSettings,
    "mcpProfile" | "mcpSubagentProfile" | "mcpThreadProfiles" | "mcpCarriedProfiles"
  >
> & { readonly projectSettingsOverrides?: Record<string, { readonly mcpProfile?: string }> };

/** What each provider's session was asked to do, in order: `start:thread`, `resume:…`, `compact:…`. */
type Calls = Array<string>;

/** A provider instance whose sessions only record their calls; enough for the check. */
const fakeInstance = (instanceId: ProviderInstanceId, calls: Calls): ProviderInstance => {
  const driver = DRIVERS[instanceId]!;
  const record = (kind: string) => (input: { readonly threadId: ThreadId }) =>
    Effect.sync(() => void calls.push(`${kind}:${instanceId}:${input.threadId}`));
  return {
    instanceId,
    driverKind: driver,
    enabled: true,
    orchestrationAdapter: {
      instanceId,
      driver,
      getCapabilities: () => Effect.die("not used"),
      planSelectionTransition: () => Effect.die("not used"),
      openSession: () =>
        Effect.succeed({
          startTurn: record("start"),
          resumeTurn: record("resume"),
          compactThread: record("compact"),
        } as unknown as ProviderAdapter.ProviderAdapterV2SessionRuntime),
    },
  } as unknown as ProviderInstance;
};

const fakeRegistry = (instances: ReadonlyArray<ProviderInstance>) =>
  Layer.effect(
    ProviderInstanceRegistry.ProviderInstanceRegistry,
    Effect.gen(function* () {
      const changes = yield* PubSub.unbounded<void>();
      return ProviderInstanceRegistry.ProviderInstanceRegistry.of({
        getInstance: (id) => Effect.succeed(instances.find((entry) => entry.instanceId === id)),
        listInstances: Effect.succeed(instances),
        listUnavailable: Effect.succeed([]),
        streamChanges: Stream.empty,
        subscribeChanges: PubSub.subscribe(changes),
      });
    }),
  );

const appThread = (
  id: string,
  patch: {
    readonly parent?: string;
    readonly delegatedFrom?: OrchestrationV2DelegatedFrom | null;
    readonly creationSource?: OrchestrationV2AppThread["creationSource"];
  } = {},
): OrchestrationV2AppThread => {
  const threadId = ThreadId.make(id);
  const base = turn(threadId, CODEX).appThread;
  return {
    ...base,
    projectId: PROJECT,
    ...(patch.creationSource === undefined ? {} : { creationSource: patch.creationSource }),
    lineage: {
      parentThreadId: patch.parent === undefined ? null : ThreadId.make(patch.parent),
      relationshipToParent: patch.parent === undefined ? null : "subagent",
      rootThreadId: ThreadId.make(patch.parent ?? id),
    },
    // MCP-launched threads store `delegatedFrom: null` at runtime (isDelegatedAppThread).
    ...(patch.delegatedFrom === undefined ? {} : { delegatedFrom: patch.delegatedFrom as never }),
  };
};

const NOW = DateTime.makeUnsafe("2026-11-20T00:00:00.000Z");

function turn(threadId: ThreadId, instanceId: ProviderInstanceId) {
  return makeTurnInput({
    threadId,
    instanceId,
    providerThread: {
      id: ProviderThreadId.make(`provider-thread:${threadId}`),
      threadId,
      providerInstanceId: instanceId,
    } as never,
    runtimePolicy: POLICY,
    now: NOW,
  });
}

/** A task another machine delegated here; `mcpProfile` absent is a sender on an older T3. */
const delegatedFrom = (mcpProfile?: OrchestrationV2DelegatedFrom["mcpProfile"]) =>
  ({
    environmentId: EnvironmentId.make("environment-there"),
    threadId: ThreadId.make("thread-there"),
    title: "Parent there",
    ...(mcpProfile === undefined ? {} : { mcpProfile }),
  }) as OrchestrationV2DelegatedFrom;

/**
 * Runs `body` with the checked registry server.ts wires (the fork layer over the instances), the
 * settings test service (`settings`), and a thread store holding `stored`.
 */
const withCheck = <A, E>(
  settings: Mcp,
  stored: ReadonlyArray<OrchestrationV2AppThread>,
  body: (input: {
    readonly calls: Calls;
    readonly raw: ReadonlyArray<ProviderInstance>;
    /** Starts a turn of `thread` on `instanceId`: `ok`, or the message the user sees. */
    readonly start: (
      thread: OrchestrationV2AppThread,
      instanceId: ProviderInstanceId,
      kind?: "start" | "resume" | "compact",
    ) => Effect.Effect<string>;
  }) => Effect.Effect<
    A,
    E,
    | ProviderInstanceRegistry.ProviderInstanceRegistry
    | ServerSettingsService.ServerSettingsService
    | ServerConfig.ServerConfig
    | FileSystem.FileSystem
  >,
  settingsLayer: Layer.Layer<
    ServerSettingsService.ServerSettingsService,
    ServerSettingsError
  > = ServerSettingsService.ServerSettingsService.layerTest(settings as never),
) => {
  const calls: Calls = [];
  const raw = [CLAUDE, CODEX, OPENCODE].map((id) => fakeInstance(id, calls));
  return Effect.gen(function* () {
    const store = yield* ProjectionStore.ProjectionStoreV2;
    for (const thread of stored) {
      yield* store.apply({
        id: EventId.make(`seed:${thread.id}`),
        type: "thread.created",
        threadId: thread.id,
        occurredAt: NOW,
        payload: thread,
      } as never);
    }
    const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
    const start = (
      thread: OrchestrationV2AppThread,
      instanceId: ProviderInstanceId,
      kind: "start" | "resume" | "compact" = "start",
    ) =>
      Effect.gen(function* () {
        const instance = yield* registry.getInstance(instanceId);
        const session = yield* instance!.orchestrationAdapter.openSession({} as never);
        const input = { ...turn(thread.id, instanceId), appThread: thread };
        return yield* kind === "start"
          ? session.startTurn(input)
          : kind === "resume"
            ? session.resumeTurn!({ ...input, messageId: input.message.messageId })
            : session.compactThread!(input);
      }).pipe(
        Effect.scoped,
        Effect.exit,
        Effect.map((exit) =>
          exit._tag === "Success"
            ? "ok"
            : makeProviderFailure({ cause: exit.cause, class: "provider_error" }).message,
        ),
      );
    return yield* body({ calls, raw, start });
  }).pipe(
    Effect.provide(
      ForkLayers.mcpProfileTurnCheck.pipe(
        Layer.provide(fakeRegistry(raw)),
        Layer.provideMerge(
          Layer.mergeAll(
            settingsLayer,
            ProjectionStore.layer.pipe(Layer.provideMerge(SqlitePersistence.layerMemory)),
            ManagedPolicy.layerTest(),
            Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-turn-check-" })),
          ).pipe(Layer.provideMerge(NodeServices.layer)),
        ),
      ),
    ),
  );
};

const CUT =
  /MCP profile \("all", cut down to its parent thread's\) is narrower than "all", and codex/;
const REFUSED =
  /MCP profile "t3-only" is narrower than "all", and codex \(provider instance codex\) does not apply MCP profiles/;

describe("mcpProfileTurnCheck", () => {
  it("checks every driver but those that apply profiles", () => {
    assert.deepStrictEqual([...MCP_PROFILE_DRIVERS], ["claudeAgent"]);
  });

  it.effect("with no profile anywhere (old and empty state), every provider runs as before", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const own = appThread("own");
        for (const id of [CLAUDE, CODEX, OPENCODE]) assert.strictEqual(yield* start(own, id), "ok");
        assert.strictEqual(yield* start(own, CODEX, "resume"), "ok");
        assert.deepStrictEqual(calls, [
          "start:claudeAgent:own",
          "start:codex:own",
          "start:opencode-work:own",
          "resume:codex:own",
        ]);
      }),
    ),
  );

  it.effect("a thread switched off Claude refuses on the new provider before any call", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ calls, raw, start }) =>
      Effect.gen(function* () {
        const own = appThread("switched");
        // Claude applies the profile itself: its instance is upstream's, untouched.
        const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
        assert.strictEqual(yield* registry.getInstance(CLAUDE), raw[0]);
        assert.strictEqual(yield* start(own, CLAUDE), "ok");
        // The same thread, switched to Codex or any other non-Claude provider.
        assert.match(yield* start(own, CODEX), REFUSED);
        assert.match(yield* start(own, CODEX, "resume"), REFUSED);
        assert.include(yield* start(own, OPENCODE), "opencode (provider instance opencode-work)");
        // Compaction summarises and calls no tools, so it is not held back.
        assert.strictEqual(yield* start(own, CODEX, "compact"), "ok");
        assert.deepStrictEqual(calls, ["start:claudeAgent:switched", "compact:codex:switched"]);
      }),
    ),
  );

  it.effect("the message says how to get the turn running", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ start }) =>
      Effect.gen(function* () {
        const message = yield* start(appThread("own"), CODEX);
        assert.include(message, "so this turn was not started");
        assert.include(message, 'Switch the thread to a Claude instance, or pick "all"');
      }),
    ),
  );

  it.effect("a thread made by t3_thread_launch in a narrowed project refuses on Codex", () =>
    withCheck(
      { projectSettingsOverrides: { [PROJECT]: { mcpProfile: "t3-only" } } },
      [],
      ({ calls, start }) =>
        Effect.gen(function* () {
          const launched = appThread("launched", { creationSource: "mcp", delegatedFrom: null });
          assert.match(yield* start(launched, CODEX), REFUSED);
          assert.strictEqual(yield* start(launched, CLAUDE), "ok");
          assert.deepStrictEqual(calls, ["start:claudeAgent:launched"]);
        }),
    ),
  );

  it.effect("a local delegation of a narrowed parent refuses on Codex; all's runs", () =>
    Effect.gen(function* () {
      const parent = appThread("parent");
      const child = appThread("child", { parent: "parent" });
      const picks = { mcpThreadProfiles: { [parent.id]: "t3-only" } } as Mcp;
      yield* withCheck(picks, [parent], ({ calls, start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(child, CODEX), CUT);
          assert.deepStrictEqual(calls, []);
        }),
      );
      // A sub-agent rule narrows only delegated children: the parent itself still runs on Codex.
      yield* withCheck({ mcpSubagentProfile: "t3-only" }, [parent], ({ start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(child, CODEX), REFUSED);
          assert.strictEqual(yield* start(parent, CODEX), "ok");
        }),
      );
      yield* withCheck({}, [parent], ({ start }) =>
        Effect.gen(function* () {
          assert.strictEqual(yield* start(child, CODEX), "ok");
        }),
      );
    }),
  );

  it.effect("a child whose parent the store does not have refuses on Codex", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const orphan = appThread("orphan", { parent: "gone" });
        assert.include(yield* start(orphan, CODEX), "parent thread cannot be read");
        assert.deepStrictEqual(calls, []);
      }),
    ),
  );

  it.effect("a task another machine delegated with a narrower profile refuses on Codex", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const task = appThread("task", { delegatedFrom: delegatedFrom(T3_ONLY) });
        assert.match(yield* start(task, CODEX), CUT);
        const all = appThread("task-all", {
          delegatedFrom: delegatedFrom({ name: "all", servers: "all", t3Tools: "all" }),
        });
        assert.strictEqual(yield* start(all, CODEX), "ok");
        assert.deepStrictEqual(calls, ["start:codex:task-all"]);
      }),
    ),
  );

  it.effect("a task from an older sender (no profile carried) gets this machine's profile", () =>
    Effect.gen(function* () {
      const task = appThread("old-task", { delegatedFrom: delegatedFrom() });
      yield* withCheck({ mcpSubagentProfile: "t3-only" }, [], ({ start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(task, CODEX), REFUSED);
        }),
      );
      yield* withCheck({}, [], ({ start }) =>
        Effect.gen(function* () {
          assert.strictEqual(yield* start(task, CODEX), "ok");
        }),
      );
    }),
  );

  it.effect("runs again: each turn reads the settings as they are then", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const own = appThread("own");
        const first = yield* start(own, CODEX);
        assert.strictEqual(yield* start(own, CODEX), first);
        const settings = yield* ServerSettingsService.ServerSettingsService;
        yield* settings.updateSettings({ mcpThreadProfiles: { [own.id]: "all" } } as never);
        assert.strictEqual(yield* start(own, CODEX), "ok");
        yield* settings.updateSettings({ mcpThreadProfiles: { [own.id]: "t3-only" } } as never);
        assert.match(yield* start(own, CODEX), REFUSED);
        assert.deepStrictEqual(calls, ["start:codex:own"]);
      }),
    ),
  );

  it.effect("the same instance comes back as the same object, from either read", () =>
    withCheck({}, [], () =>
      Effect.gen(function* () {
        const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
        const codex = yield* registry.getInstance(CODEX);
        assert.strictEqual(yield* registry.getInstance(CODEX), codex);
        const listed = yield* registry.listInstances;
        assert.strictEqual(
          listed.find((entry) => entry.instanceId === CODEX),
          codex,
        );
        assert.isUndefined(yield* registry.getInstance(ProviderInstanceId.make("none")));
      }),
    ),
  );

  it.effect("stopped half-way: a turn interrupted while its profile is read never starts", () =>
    Effect.gen(function* () {
      const reading = yield* Deferred.make<void>();
      const release = yield* Deferred.make<void>();
      let reads = 0;
      const blocking = Layer.effect(
        ServerSettingsService.ServerSettingsService,
        Effect.gen(function* () {
          const service = yield* ServerSettingsService.ServerSettingsService;
          return ServerSettingsService.ServerSettingsService.of({
            ...service,
            getSettings: Effect.suspend(() =>
              reads++ === 0
                ? Deferred.succeed(reading, undefined).pipe(
                    Effect.andThen(Deferred.await(release)),
                    Effect.andThen(service.getSettings),
                  )
                : service.getSettings,
            ),
          });
        }),
      ).pipe(Layer.provide(ServerSettingsService.ServerSettingsService.layerTest()));
      yield* withCheck(
        {},
        [],
        ({ calls, start }) =>
          Effect.gen(function* () {
            const own = appThread("own");
            const fiber = yield* Effect.forkChild(start(own, CODEX));
            yield* Deferred.await(reading);
            yield* Fiber.interrupt(fiber);
            assert.deepStrictEqual(calls, []);
            // The next turn reads again and starts.
            assert.strictEqual(yield* start(own, CODEX), "ok");
            assert.deepStrictEqual(calls, ["start:codex:own"]);
          }),
        blocking,
      );
    }),
  );

  it.effect("a failed settings read: own threads get all, a narrower carried task refuses", () =>
    Effect.gen(function* () {
      const failing = Layer.effect(
        ServerSettingsService.ServerSettingsService,
        Effect.gen(function* () {
          const service = yield* ServerSettingsService.ServerSettingsService;
          return ServerSettingsService.ServerSettingsService.of({
            ...service,
            getSettings: Effect.fail(
              new ServerSettingsError({
                settingsPath: "settings.json",
                operation: "read-secret",
                cause: new Error("secret store unreadable"),
              }),
            ),
          });
        }),
      ).pipe(Layer.provide(ServerSettingsService.ServerSettingsService.layerTest()));
      yield* withCheck(
        {},
        [],
        ({ start }) =>
          Effect.gen(function* () {
            // No settings.json (missing state): the defaults, so this machine's own thread runs.
            assert.strictEqual(yield* start(appThread("own"), CODEX), "ok");
            // settings.json that cannot be read as a file: a carried narrower task is refused.
            const { settingsPath } = yield* ServerConfig.ServerConfig;
            yield* (yield* FileSystem.FileSystem).makeDirectory(settingsPath);
            const task = appThread("task", { delegatedFrom: delegatedFrom(T3_ONLY) });
            assert.notStrictEqual(yield* start(task, CODEX), "ok");
          }),
        failing,
      );
    }),
  );
});
