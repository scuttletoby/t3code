/**
 * Fork: every turn but Claude's fixes its thread's T3 tool groups; outside Claude and Codex a turn
 * whose profile leaves out an outside server also refuses. The check wraps upstream's provider
 * instance registry as server.ts wires it, over upstream's settings test service and a real
 * thread store (SQLite in memory), so each route a thread takes meets it at turn start.
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
import * as T3ToolSets from "./mcpT3ToolSets.ts";
import { CurrentTurnMcpProfile } from "./mcpTurnProfile.ts";

/** The groups the thread's set holds now, or its tag when it is not a list. */
const groupsOf = (threadId: string) => {
  const set = T3ToolSets.t3ToolSetFor(threadId);
  return set?._tag === "Groups" && set.groups !== "all" ? [...set.groups] : set?._tag;
};

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
    "mcpProfile" | "mcpProfiles" | "mcpSubagentProfile" | "mcpThreadProfiles" | "mcpCarriedProfiles"
  >
> & { readonly projectSettingsOverrides?: Record<string, { readonly mcpProfile?: string }> };

/** What each provider's session was asked to do, in order: `start:thread`, `resume:…`, `compact:…`. */
type Calls = Array<string>;

/** The profile each recorded call was given to read (`thread:name`), in order. */
const turnProfiles: Array<string | undefined> = [];

/** A provider instance whose sessions only record their calls; enough for the check. */
const fakeInstance = (instanceId: ProviderInstanceId, calls: Calls): ProviderInstance => {
  const driver = DRIVERS[instanceId]!;
  const record = (kind: string) => (input: { readonly threadId: ThreadId }) =>
    Effect.gen(function* () {
      calls.push(`${kind}:${instanceId}:${input.threadId}`);
      const current = yield* CurrentTurnMcpProfile;
      turnProfiles.push(
        current === undefined ? undefined : `${current.threadId}:${current.resolved.name}`,
      );
    });
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
  const base = turn(threadId, OPENCODE).appThread;
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
  /** More instances in the registry, after the three fakes. */
  extra: ReadonlyArray<ProviderInstance> = [],
) => {
  const calls: Calls = [];
  const raw = [...[CLAUDE, CODEX, OPENCODE].map((id) => fakeInstance(id, calls)), ...extra];
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
  /MCP profile \("all", cut down to its parent thread's\) leaves out some outside MCP servers, and opencode/;
const REFUSED =
  /MCP profile "t3-only" leaves out some outside MCP servers, and opencode \(provider instance opencode-work\) does not apply a profile's outside servers/;

describe("mcpProfileTurnCheck", () => {
  it("checks every driver but those that apply profiles", () => {
    assert.deepStrictEqual([...MCP_PROFILE_DRIVERS], ["claudeAgent", "codex"]);
  });

  it.effect("with no profile anywhere (old and empty state), every provider runs as before", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const own = appThread("own");
        for (const id of [CLAUDE, CODEX, OPENCODE]) assert.strictEqual(yield* start(own, id), "ok");
        assert.strictEqual(yield* start(own, OPENCODE, "resume"), "ok");
        assert.deepStrictEqual(calls, [
          "start:claudeAgent:own",
          "start:codex:own",
          "start:opencode-work:own",
          "resume:opencode-work:own",
        ]);
      }),
    ),
  );

  it.effect("a thread switched off Claude refuses on the new provider before any call", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ calls, raw, start }) =>
      Effect.gen(function* () {
        const own = appThread("switched");
        // Claude applies the whole profile itself: its instance is upstream's, untouched. Codex
        // applies the outside servers itself: its turns only get their T3 tool groups fixed here.
        const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
        assert.strictEqual(yield* registry.getInstance(CLAUDE), raw[0]);
        assert.strictEqual(yield* start(own, CLAUDE), "ok");
        assert.notStrictEqual(yield* registry.getInstance(CODEX), raw[1]);
        assert.strictEqual(yield* start(own, CODEX), "ok");
        // The same thread, switched to a provider that does not apply profiles.
        assert.match(yield* start(own, OPENCODE), REFUSED);
        assert.match(yield* start(own, OPENCODE, "resume"), REFUSED);
        assert.include(yield* start(own, OPENCODE), "opencode (provider instance opencode-work)");
        // Some providers compact by running a `/compact` prompt: it is held back too.
        assert.match(yield* start(own, OPENCODE, "compact"), REFUSED);
        assert.strictEqual(yield* start(own, CLAUDE, "compact"), "ok");
        assert.deepStrictEqual(calls, [
          "start:claudeAgent:switched",
          "start:codex:switched",
          "compact:claudeAgent:switched",
        ]);
      }),
    ),
  );

  it.effect("the message says how to get the turn running", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ start }) =>
      Effect.gen(function* () {
        const message = yield* start(appThread("own"), OPENCODE);
        assert.include(message, "so this turn was not started");
        assert.include(message, 'Switch the thread to a Claude or Codex instance, or pick "all"');
        // Retrying cannot help, so upstream's turn-start guidance is not what the user reads.
        assert.notInclude(message, "Retry");
        assert.notInclude(message, "server logs");
      }),
    ),
  );

  it.effect(
    "every outside server but fewer T3 tools runs on every provider, its groups fixed",
    () =>
      withCheck(
        {
          mcpProfiles: { "servers-only": { servers: "all", t3Tools: ["threads"] } },
          mcpProfile: "servers-only",
        } as Mcp,
        [],
        ({ calls, start }) =>
          Effect.gen(function* () {
            T3ToolSets.__testing.clear();
            const own = appThread("own");
            assert.strictEqual(yield* start(own, OPENCODE), "ok");
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
            assert.strictEqual(yield* start(own, CODEX), "ok");
            assert.strictEqual(yield* start(own, CLAUDE), "ok");
            assert.deepStrictEqual(calls, [
              "start:opencode-work:own",
              "start:codex:own",
              "start:claudeAgent:own",
            ]);
          }),
      ),
  );

  it.effect(
    "Codex (in MCP_PROFILE_DRIVERS): a narrowed t3Tools gets exactly that set, and outside servers are left to Codex",
    () =>
      withCheck(
        {
          mcpProfiles: { lean: { servers: [], t3Tools: ["threads", "waits"] } },
          mcpProfile: "lean",
        } as Mcp,
        [],
        ({ calls, start }) =>
          Effect.gen(function* () {
            T3ToolSets.__testing.clear();
            const own = appThread("own");
            // Every outside server left out: OpenCode cannot apply that, Codex does it itself.
            assert.match(yield* start(own, OPENCODE), /"lean" leaves out some outside MCP servers/);
            T3ToolSets.__testing.clear();
            for (const how of ["start", "resume", "compact"] as const) {
              T3ToolSets.fixT3ToolSet(
                own.id,
                { _tag: "Groups", name: "all", groups: "all" },
                T3ToolSets.nextT3ToolSetTurn(),
              );
              assert.strictEqual(yield* start(own, CODEX, how), "ok");
              assert.deepStrictEqual(groupsOf(own.id), ["threads", "waits"]);
            }
            assert.deepStrictEqual(calls, [
              "start:codex:own",
              "resume:codex:own",
              "compact:codex:own",
            ]);
            // Codex's own checks read this profile, not work it out again.
            assert.deepStrictEqual(turnProfiles.slice(-3), ["own:lean", "own:lean", "own:lean"]);
          }),
      ),
  );

  it.effect(
    "Claude is unchanged: its turns are upstream's, its set fixed where its query opens",
    () =>
      withCheck(
        {
          mcpProfiles: { lean: { servers: [], t3Tools: ["threads"] } },
          mcpProfile: "lean",
        } as Mcp,
        [],
        ({ calls, start }) =>
          Effect.gen(function* () {
            T3ToolSets.__testing.clear();
            const own = appThread("own");
            assert.strictEqual(yield* start(own, CLAUDE), "ok");
            // Not fixed here: claudeMcpProfile.ts fixes it as the query opens.
            assert.isUndefined(T3ToolSets.t3ToolSetFor(own.id));
            assert.deepStrictEqual(calls, ["start:claudeAgent:own"]);
          }),
      ),
  );

  it.effect('builtinTools "none" runs only on Claude; Codex and OpenCode refuse the turn', () =>
    withCheck(
      {
        mcpProfiles: { bare: { servers: "all", t3Tools: "all", builtinTools: "none" } },
        mcpProfile: "bare",
      } as Mcp,
      [],
      ({ calls, start }) =>
        Effect.gen(function* () {
          const own = appThread("own");
          assert.strictEqual(yield* start(own, CLAUDE), "ok");
          for (const id of [CODEX, OPENCODE]) {
            const message = yield* start(own, id);
            assert.include(message, 'MCP profile "bare" has builtinTools "none"');
            assert.include(message, "Switch the thread to a Claude instance");
            assert.include(yield* start(own, id, "resume"), "builtinTools");
          }
          assert.deepStrictEqual(calls, ["start:claudeAgent:own"]);
        }),
    ),
  );

  it.effect("a profile change applies from the next turn: the running one keeps its set", () =>
    withCheck(
      {
        mcpProfiles: { lean: { servers: "all", t3Tools: ["threads"] } },
        mcpProfile: "lean",
      } as Mcp,
      [],
      ({ start }) =>
        Effect.gen(function* () {
          T3ToolSets.__testing.clear();
          const own = appThread("own");
          for (const id of [CODEX, OPENCODE]) {
            assert.strictEqual(yield* start(own, id), "ok");
            const settings = yield* ServerSettingsService.ServerSettingsService;
            yield* settings.updateSettings({
              mcpProfiles: { lean: { servers: "all", t3Tools: ["threads", "orchestration"] } },
            } as never);
            // No turn started since: the set stays what the running turn started with.
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
            assert.strictEqual(yield* start(own, id, "resume"), "ok");
            assert.deepStrictEqual(groupsOf(own.id), ["threads", "orchestration"]);
            yield* settings.updateSettings({
              mcpProfiles: { lean: { servers: "all", t3Tools: ["threads"] } },
            } as never);
            assert.strictEqual(yield* start(own, id), "ok");
          }
        }),
    ),
  );

  it.effect(
    "a profile that is gone refuses the turn on Codex and OpenCode and keeps only the ungrouped",
    () =>
      withCheck({ mcpProfile: "gone" }, [], ({ calls, start }) =>
        Effect.gen(function* () {
          const own = appThread("own");
          for (const id of [CODEX, OPENCODE]) {
            T3ToolSets.fixT3ToolSet(
              own.id,
              { _tag: "Groups", name: "all", groups: "all" },
              T3ToolSets.nextT3ToolSetTurn(),
            );
            assert.include(yield* start(own, id, "compact"), 'MCP profile "gone" is not set up');
            assert.strictEqual(T3ToolSets.t3ToolSetFor(own.id)?._tag, "Refused");
          }
          assert.deepStrictEqual(calls, []);
        }),
      ),
  );

  it.effect("a thread made by t3_thread_launch in a narrowed project refuses on OpenCode", () =>
    withCheck(
      { projectSettingsOverrides: { [PROJECT]: { mcpProfile: "t3-only" } } },
      [],
      ({ calls, start }) =>
        Effect.gen(function* () {
          const launched = appThread("launched", { creationSource: "mcp", delegatedFrom: null });
          assert.match(yield* start(launched, OPENCODE), REFUSED);
          assert.strictEqual(yield* start(launched, CLAUDE), "ok");
          assert.deepStrictEqual(calls, ["start:claudeAgent:launched"]);
        }),
    ),
  );

  it.effect("a local delegation of a narrowed parent refuses on OpenCode; all's runs", () =>
    Effect.gen(function* () {
      const parent = appThread("parent");
      const child = appThread("child", { parent: "parent" });
      const picks = { mcpThreadProfiles: { [parent.id]: "t3-only" } } as Mcp;
      yield* withCheck(picks, [parent], ({ calls, start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(child, OPENCODE), CUT);
          assert.deepStrictEqual(calls, []);
        }),
      );
      // A sub-agent rule narrows only delegated children: the parent itself still runs on OpenCode.
      yield* withCheck({ mcpSubagentProfile: "t3-only" }, [parent], ({ start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(child, OPENCODE), REFUSED);
          assert.strictEqual(yield* start(parent, OPENCODE), "ok");
        }),
      );
      yield* withCheck({}, [parent], ({ start }) =>
        Effect.gen(function* () {
          assert.strictEqual(yield* start(child, OPENCODE), "ok");
        }),
      );
    }),
  );

  it.effect("a child whose parent the store does not have refuses on OpenCode", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const orphan = appThread("orphan", { parent: "gone" });
        assert.include(yield* start(orphan, OPENCODE), "parent thread cannot be read");
        assert.deepStrictEqual(calls, []);
      }),
    ),
  );

  it.effect("a task another machine delegated with a narrower profile refuses on OpenCode", () =>
    withCheck({}, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const task = appThread("task", { delegatedFrom: delegatedFrom(T3_ONLY) });
        assert.match(yield* start(task, OPENCODE), CUT);
        const all = appThread("task-all", {
          delegatedFrom: delegatedFrom({ name: "all", servers: "all", t3Tools: "all" }),
        });
        assert.strictEqual(yield* start(all, OPENCODE), "ok");
        assert.deepStrictEqual(calls, ["start:opencode-work:task-all"]);
      }),
    ),
  );

  it.effect("a task from an older sender (no profile carried) gets this machine's profile", () =>
    Effect.gen(function* () {
      const task = appThread("old-task", { delegatedFrom: delegatedFrom() });
      yield* withCheck({ mcpSubagentProfile: "t3-only" }, [], ({ start }) =>
        Effect.gen(function* () {
          assert.match(yield* start(task, OPENCODE), REFUSED);
        }),
      );
      yield* withCheck({}, [], ({ start }) =>
        Effect.gen(function* () {
          assert.strictEqual(yield* start(task, OPENCODE), "ok");
        }),
      );
    }),
  );

  it.effect("runs again: each turn reads the settings as they are then", () =>
    withCheck({ mcpProfile: "t3-only" }, [], ({ calls, start }) =>
      Effect.gen(function* () {
        const own = appThread("own");
        const first = yield* start(own, OPENCODE);
        assert.strictEqual(yield* start(own, OPENCODE), first);
        const settings = yield* ServerSettingsService.ServerSettingsService;
        yield* settings.updateSettings({ mcpThreadProfiles: { [own.id]: "all" } } as never);
        assert.strictEqual(yield* start(own, OPENCODE), "ok");
        yield* settings.updateSettings({ mcpThreadProfiles: { [own.id]: "t3-only" } } as never);
        assert.match(yield* start(own, OPENCODE), REFUSED);
        assert.deepStrictEqual(calls, ["start:opencode-work:own"]);
      }),
    ),
  );

  it.effect("the same instance comes back as the same object, from either read", () =>
    withCheck({}, [], () =>
      Effect.gen(function* () {
        const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
        const other = yield* registry.getInstance(OPENCODE);
        assert.strictEqual(yield* registry.getInstance(OPENCODE), other);
        const listed = yield* registry.listInstances;
        assert.strictEqual(
          listed.find((entry) => entry.instanceId === OPENCODE),
          other,
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
            T3ToolSets.fixT3ToolSet(
              own.id,
              { _tag: "Groups", name: "all", groups: "all" },
              T3ToolSets.nextT3ToolSetTurn(),
            );
            const fiber = yield* Effect.forkChild(start(own, OPENCODE));
            yield* Deferred.await(reading);
            yield* Fiber.interrupt(fiber);
            assert.deepStrictEqual(calls, []);
            // No set stands for a turn that never started: the next request works it out again.
            assert.isUndefined(T3ToolSets.t3ToolSetFor(own.id));
            // The next turn reads again and starts.
            assert.strictEqual(yield* start(own, OPENCODE), "ok");
            assert.deepStrictEqual(calls, ["start:opencode-work:own"]);
          }),
        blocking,
      );
    }),
  );

  /**
   * Settings whose first read takes what they hold as it starts and gives it only once released,
   * so the turn reading them finishes after a later turn's.
   */
  const slowFirstRead = Effect.gen(function* () {
    const reading = yield* Deferred.make<void>();
    const release = yield* Deferred.make<void>();
    let reads = 0;
    const layer = Layer.effect(
      ServerSettingsService.ServerSettingsService,
      Effect.gen(function* () {
        const service = yield* ServerSettingsService.ServerSettingsService;
        return ServerSettingsService.ServerSettingsService.of({
          ...service,
          getSettings: Effect.suspend(() =>
            reads++ === 0
              ? service.getSettings.pipe(
                  Effect.tap(() => Deferred.succeed(reading, undefined)),
                  Effect.tap(() => Deferred.await(release)),
                )
              : service.getSettings,
          ),
        });
      }),
    ).pipe(Layer.provide(ServerSettingsService.ServerSettingsService.layerTest()));
    return { reading, release, layer };
  });
  /** The thread's profile narrowed to the threads group. */
  const LEAN = {
    mcpProfile: "lean",
    mcpProfiles: { lean: { servers: "all", t3Tools: ["threads"] } },
  } as never;

  it.effect("two turns' checks finishing out of order: the later turn's set stands (#986)", () =>
    Effect.gen(function* () {
      const slow = yield* slowFirstRead;
      yield* withCheck(
        {},
        [],
        ({ start }) =>
          Effect.gen(function* () {
            T3ToolSets.__testing.clear();
            const own = appThread("own");
            // The first turn reads `all`, then is slow; the profile is narrowed meanwhile.
            const first = yield* Effect.forkChild(start(own, OPENCODE));
            yield* Deferred.await(slow.reading);
            const settings = yield* ServerSettingsService.ServerSettingsService;
            yield* settings.updateSettings(LEAN);
            assert.strictEqual(yield* start(own, OPENCODE), "ok");
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
            // The first turn's check finishes last: it does not widen the thread back to all.
            yield* Deferred.succeed(slow.release, undefined);
            assert.strictEqual(yield* Fiber.join(first), "ok");
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
          }),
        slow.layer,
      );
    }),
  );

  it.effect("stopped half-way after a later turn fixed its set: that set stands (#986)", () =>
    Effect.gen(function* () {
      const slow = yield* slowFirstRead;
      yield* withCheck(
        {},
        [],
        ({ start }) =>
          Effect.gen(function* () {
            T3ToolSets.__testing.clear();
            const own = appThread("own");
            const settings = yield* ServerSettingsService.ServerSettingsService;
            yield* settings.updateSettings(LEAN);
            const first = yield* Effect.forkChild(start(own, OPENCODE));
            yield* Deferred.await(slow.reading);
            assert.strictEqual(yield* start(own, OPENCODE), "ok");
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
            // The first turn's start is stopped: the later turn's set is not cleared.
            yield* Fiber.interrupt(first);
            assert.deepStrictEqual(groupsOf(own.id), ["threads"]);
          }),
        slow.layer,
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
            assert.strictEqual(yield* start(appThread("own"), OPENCODE), "ok");
            // settings.json that cannot be read as a file: a carried narrower task is refused.
            const { settingsPath } = yield* ServerConfig.ServerConfig;
            yield* (yield* FileSystem.FileSystem).makeDirectory(settingsPath);
            const task = appThread("task", { delegatedFrom: delegatedFrom(T3_ONLY) });
            assert.notStrictEqual(yield* start(task, OPENCODE), "ok");
          }),
        failing,
      );
    }),
  );
  /** Session runtimes as providers build them; each reads `providerSession` from `current`. */
  const SHAPES: ReadonlyArray<
    readonly [
      string,
      (current: () => unknown, calls: Calls) => ProviderAdapter.ProviderAdapterV2SessionRuntime,
    ]
  > = [
    [
      "an object with a getter and startTurn only",
      (current, calls) =>
        ({
          get providerSession() {
            return current();
          },
          startTurn: () => Effect.sync(() => void calls.push("start")),
        }) as never,
    ],
    [
      "an object with a getter, resumeTurn and compactThread (ACP, Pi, OpenCode v2)",
      (current, calls) =>
        ({
          get providerSession() {
            return current();
          },
          startTurn: () => Effect.sync(() => void calls.push("start")),
          resumeTurn: () => Effect.sync(() => void calls.push("resume")),
          compactThread: () => Effect.sync(() => void calls.push("compact")),
        }) as never,
    ],
    [
      "a class instance whose getter is on its prototype",
      (current, calls) => {
        class Runtime {
          get providerSession() {
            return current();
          }
          startTurn() {
            return Effect.sync(() => void calls.push("start"));
          }
        }
        return new Runtime() as never;
      },
    ],
  ];

  const DRIVER_KINDS = ["pi", "opencode", "cursor"] as const;

  describe.each(DRIVER_KINDS)("a wrapped %s session", (driverKind) => {
    it.effect.each(SHAPES)("%s keeps its live getters and its checks", ([, shape]) =>
      Effect.gen(function* () {
        const instanceId = ProviderInstanceId.make(`${driverKind}-live`);
        const driver = ProviderDriverKind.make(driverKind);
        let current: { readonly model: string } = { model: "first" };
        let enabled = true;
        const sessionCalls: Calls = [];
        const runtime = shape(() => current, sessionCalls);
        const instance = {
          instanceId,
          driverKind: driver,
          get enabled() {
            return enabled;
          },
          orchestrationAdapter: {
            instanceId,
            driver,
            openSession: () => Effect.succeed(runtime),
          },
        } as unknown as ProviderInstance;
        const input = { ...turn(ThreadId.make("own"), instanceId), appThread: appThread("own") };
        const wrappedInstance = Effect.gen(function* () {
          const registry = yield* ProviderInstanceRegistry.ProviderInstanceRegistry;
          const wrapped = (yield* registry.getInstance(instanceId))!;
          assert.notStrictEqual(wrapped, instance);
          return wrapped;
        });
        const open = wrappedInstance.pipe(
          Effect.flatMap((wrapped) => wrapped.orchestrationAdapter.openSession({} as never)),
        );
        const refused = <E>(run: Effect.Effect<void, E>) =>
          Effect.exit(run).pipe(Effect.map((exit) => exit._tag === "Failure"));

        // Profile `all`: the turn runs and the session reads as it is now, not as it was opened.
        yield* withCheck(
          {},
          [],
          () =>
            Effect.gen(function* () {
              const session = yield* open;
              assert.strictEqual(
                (session.providerSession as never as typeof current).model,
                "first",
              );
              current = { model: "second" };
              assert.strictEqual(
                (session.providerSession as never as typeof current).model,
                "second",
              );
              yield* session.startTurn(input);
              assert.deepStrictEqual(sessionCalls, ["start"]);
              // The instance's own getters stay live as well.
              const wrapped = yield* wrappedInstance;
              enabled = false;
              assert.isFalse(wrapped.enabled);
              enabled = true;
            }).pipe(Effect.scoped),
          undefined,
          [instance],
        );

        // A narrower profile: refused, and still live; a spread copy (upstream's
        // ProviderSessionManager decorates the runtime with one) keeps the check.
        current = { model: "first" };
        yield* withCheck(
          { mcpProfile: "t3-only" },
          [],
          () =>
            Effect.gen(function* () {
              const session = yield* open;
              current = { model: "third" };
              assert.strictEqual(
                (session.providerSession as never as typeof current).model,
                "third",
              );
              assert.isTrue(yield* refused(session.startTurn(input)));
              assert.isTrue(yield* refused({ ...session }.startTurn(input)));
              if (session.resumeTurn !== undefined) {
                assert.isTrue(
                  yield* refused(
                    session.resumeTurn({ ...input, messageId: input.message.messageId }),
                  ),
                );
              }
              if (session.compactThread !== undefined) {
                assert.isTrue(yield* refused(session.compactThread(input)));
              }
              assert.deepStrictEqual(sessionCalls, ["start"]);
            }).pipe(Effect.scoped),
          undefined,
          [instance],
        );
      }),
    );
  });
});
