/**
 * Fork: one owner per thread. Upstream's own idle compaction worker runs against mocked stores
 * behind the fork's orchestrator wrapper: a thread with handoff on is never compacted, any other
 * thread is compacted exactly as upstream does, and server.ts gives the wrapper to that worker.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import {
  DEFAULT_HANDOFF_SETTINGS,
  DEFAULT_SERVER_SETTINGS,
  EventId,
  type HandoffSettings,
  type OrchestrationV2ServerCommand,
  type OrchestrationV2ThreadProjection,
  type ServerSettings as ServerSettingsValue,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Stream from "effect/Stream";
import * as Tracer from "effect/Tracer";
import * as TestClock from "effect/testing/TestClock";

import * as IdleCompactionService from "../../orchestration-v2/IdleCompactionService.ts";
import * as Orchestrator from "../../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../../serverSettings.ts";
import { at, MINUTE_MS, NOW_MS, projection, run, runId, threadId } from "./handoff.testkit.ts";
import * as HandoffOwner from "./handoffOwner.ts";

const projectId = projection({ usedTokens: 0 }).thread.projectId;

interface State {
  data: OrchestrationV2ThreadProjection;
  /** The server's handoff settings, over the defaults (off). */
  server: Partial<HandoffSettings>;
  /** The thread's project override: absent, or its handoff settings over the defaults. */
  project?: Partial<HandoffSettings>;
  /** The wrapper's thread lookup fails (a store error at that moment). */
  findFails?: boolean;
  /** The wrapper's thread lookup finds no thread (deleted, or not projected yet). */
  threadMissing?: boolean;
  /** The wrapper's settings read fails (upstream's own settings reads still work). */
  ownerSettingsFail?: boolean;
  /** The thread's project override is one from before handoff existed: no `handoff` key. */
  oldProjectOverride?: boolean;
}

const settingsOf = (state: State): ServerSettingsValue => ({
  ...DEFAULT_SERVER_SETTINGS,
  // Upstream's idle compaction on, for the thread's built-in Claude instance.
  idleCompaction: "beforeCacheExpires",
  providerInstances: {},
  handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...state.server },
  projectSettingsOverrides: (state.oldProjectOverride === true
    ? { [projectId]: {} }
    : state.project === undefined
      ? {}
      : {
          [projectId]: { handoff: { ...DEFAULT_HANDOFF_SETTINGS, ...state.project } },
        }) as unknown as ServerSettingsValue["projectSettingsOverrides"],
});

/**
 * Upstream's idle compaction worker behind `HandoffOwner.make`, with mocked stores read at every
 * call (a test may change `state` while compaction waits). Returns the commands that reached
 * upstream's orchestrator and a wait for the worker's `count`th finished decision.
 */
const compaction = (state: State, completions = 1) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const sent: Array<OrchestrationV2ServerCommand> = [];
    let ended = 0;
    const done = yield* Deferred.make<void>();
    const tracer = Tracer.make({
      span(options) {
        const span = new Tracer.NativeSpan(options);
        const end = span.end.bind(span);
        span.end = (...args) => {
          end(...args);
          if (span.name === "IdleCompactionService.compactWhenDue" && ++ended >= completions)
            Deferred.doneUnsafe(done, Effect.void);
        };
        return span;
      },
    });
    const completed = {
      id: EventId.make("event:run-1:completed"),
      type: "run.updated",
      threadId,
      runId,
      occurredAt: at(),
      payload: run(),
    } as const;
    const upstream = Layer.mock(Orchestrator.OrchestratorV2)({
      streamDomainEvents: Stream.make(...Array.from({ length: completions }, () => completed)),
      dispatch: (command) =>
        Effect.sync(() => {
          sent.push(command);
          return { sequence: sent.length, storedEvents: [] };
        }),
    });
    const stores = Layer.mergeAll(
      Layer.mock(ProjectionStore.ProjectionStoreV2)({
        getThread: () => Effect.sync(() => state.data.thread),
        findThread: () =>
          state.findFails === true
            ? Effect.fail(new Error("store unavailable") as never)
            : Effect.sync(() => (state.threadMissing === true ? null : state.data.thread)),
        getThreadRecords: () => Effect.sync(() => state.data),
      }),
      Layer.mock(ServerSettings.ServerSettingsService)({
        getSettings: Effect.sync(() => settingsOf(state)),
      }),
    );
    // The wrapper reads the same settings through its own handle, so a test can fail only its read.
    const ownerSettings = Layer.mock(ServerSettings.ServerSettingsService)({
      getSettings: Effect.suspend(() =>
        state.ownerSettingsFail === true
          ? Effect.fail(new Error("settings unreadable") as never)
          : Effect.sync(() => settingsOf(state)),
      ),
    });
    const wrapped = Layer.effect(Orchestrator.OrchestratorV2, HandoffOwner.make).pipe(
      Layer.provide(upstream),
      Layer.provide(ownerSettings),
      Layer.provideMerge(stores),
    );
    yield* IdleCompactionService.make.pipe(
      Effect.flatMap((service) => service.start()),
      Effect.provide(wrapped),
      Effect.withTracer(tracer),
    );
    return { sent, done: Deferred.await(done) };
  });

/** A long Claude thread whose prompt cache expires in `expiresInMs` (compaction starts 2 min before). */
const longThread = (expiresInMs = 2 * MINUTE_MS) =>
  projection({ usedTokens: 250_000, cacheExpiresInMs: expiresInMs });

const compactions = (sent: ReadonlyArray<OrchestrationV2ServerCommand>) =>
  sent.filter((command) => command.type === "message.dispatch" && command.idleCompaction);

describe("one owner per thread: idle compaction and handoff", () => {
  it.effect.each([
    { name: "handoff off (the default)", server: {}, compacts: 1 },
    { name: "handoff mode compact", server: { mode: "compact" }, compacts: 1 },
    { name: "handoff on for the server", server: { mode: "handoff" }, compacts: 0 },
    {
      name: "handoff on for the server, its project off",
      server: { mode: "handoff" },
      project: { mode: "off" },
      compacts: 1,
    },
    {
      name: "handoff off for the server, its project on",
      server: {},
      project: { mode: "handoff" },
      compacts: 0,
    },
    {
      name: "handoff on for this thread only",
      server: { threads: { [threadId]: "handoff" } },
      compacts: 0,
    },
    {
      name: "handoff on for another thread only",
      server: { threads: { "other-thread": "handoff" } },
      compacts: 1,
    },
  ] as ReadonlyArray<{
    name: string;
    server: Partial<HandoffSettings>;
    project?: Partial<HandoffSettings>;
    compacts: number;
  }>)("$name: compacts $compacts time(s)", ({ server, project, compacts }) =>
    Effect.gen(function* () {
      const state: State = {
        data: longThread(),
        server,
        ...(project === undefined ? {} : { project }),
      };
      const { sent, done } = yield* compaction(state);
      yield* done;
      expect(compactions(sent)).toHaveLength(compacts);
      // Nothing else is sent on the thread's behalf.
      expect(sent).toHaveLength(compacts);
    }),
  );

  it.effect("handoff switched on while compaction waits: decided at the send, so none", () =>
    Effect.gen(function* () {
      const state: State = { data: longThread(10 * MINUTE_MS), server: {} };
      const { sent, done } = yield* compaction(state);
      yield* TestClock.adjust(MINUTE_MS);
      state.server = { mode: "handoff" };
      yield* TestClock.adjust(7 * MINUTE_MS);
      yield* done;
      expect(sent).toEqual([]);
    }),
  );

  it.effect("handoff switched off while compaction waits: upstream compacts as before", () =>
    Effect.gen(function* () {
      const state: State = { data: longThread(10 * MINUTE_MS), server: { mode: "handoff" } };
      const { sent, done } = yield* compaction(state);
      yield* TestClock.adjust(MINUTE_MS);
      state.project = { mode: "off" };
      yield* TestClock.adjust(7 * MINUTE_MS);
      yield* done;
      expect(compactions(sent)).toHaveLength(1);
    }),
  );

  it.effect("the same run reported twice (runs again): still no compaction", () =>
    Effect.gen(function* () {
      const state: State = { data: longThread(), server: { mode: "handoff" } };
      const { sent, done } = yield* compaction(state, 2);
      yield* done;
      expect(sent).toEqual([]);
    }),
  );

  it.effect("the owner cannot be read: upstream decides, and compacts", () =>
    Effect.gen(function* () {
      // Only the wrapper looks the thread up with `findThread`; upstream's own reads still work.
      const state: State = { data: longThread(), server: { mode: "handoff" }, findFails: true };
      const { sent, done } = yield* compaction(state);
      yield* done;
      expect(compactions(sent)).toHaveLength(1);
    }),
  );

  // Missing, empty or unreadable state: the wrapper cannot tell that handoff owns the thread, so
  // upstream decides, exactly as with handoff off. Each case has handoff on for the server.
  const unknownOwner: ReadonlyArray<{ name: string; patch: Partial<State> }> = [
    { name: "the thread is missing", patch: { threadMissing: true } },
    { name: "the settings cannot be read", patch: { ownerSettingsFail: true } },
  ];

  it.effect.each(unknownOwner)("$name: upstream decides, and compacts", ({ patch }) =>
    Effect.gen(function* () {
      const state: State = { data: longThread(), server: { mode: "handoff" }, ...patch };
      const { sent, done } = yield* compaction(state);
      yield* done;
      expect(compactions(sent)).toHaveLength(1);
      expect(sent).toHaveLength(1);
    }),
  );

  it.effect.each(unknownOwner)(
    "$name, the same run reported twice (runs again): as upstream alone",
    ({ patch }) =>
      Effect.gen(function* () {
        const alone = yield* compaction({ data: longThread(), server: {} }, 2);
        yield* alone.done;
        const state: State = { data: longThread(), server: { mode: "handoff" }, ...patch };
        const { sent, done } = yield* compaction(state, 2);
        yield* done;
        expect(compactions(alone.sent).length).toBeGreaterThan(0);
        expect(compactions(sent)).toHaveLength(compactions(alone.sent).length);
      }),
  );

  it.effect.each(unknownOwner)(
    "$name only after compaction began to wait (stopped half-way): decided at the send, compacts",
    ({ patch }) =>
      Effect.gen(function* () {
        const state: State = { data: longThread(10 * MINUTE_MS), server: { mode: "handoff" } };
        const { sent, done } = yield* compaction(state);
        yield* TestClock.adjust(MINUTE_MS);
        Object.assign(state, patch);
        yield* TestClock.adjust(7 * MINUTE_MS);
        yield* done;
        expect(compactions(sent)).toHaveLength(1);
      }),
  );

  it.effect("the thread is back and its settings readable at the next run: handoff owns it", () =>
    Effect.gen(function* () {
      // A missing thread or a failed read is not remembered: the next send reads afresh.
      const state: State = { data: longThread(), server: { mode: "handoff" }, threadMissing: true };
      const first = yield* compaction(state);
      yield* first.done;
      expect(compactions(first.sent)).toHaveLength(1);
      state.threadMissing = false;
      const second = yield* compaction(state);
      yield* second.done;
      expect(second.sent).toEqual([]);
    }),
  );

  it.effect.each([
    { name: "handoff on for the server", server: { mode: "handoff" }, compacts: 0 },
    { name: "handoff off for the server", server: {}, compacts: 1 },
  ] as ReadonlyArray<{ name: string; server: Partial<HandoffSettings>; compacts: number }>)(
    "an old project override with no handoff key, $name: the server's mode, compacts $compacts",
    ({ server, compacts }) =>
      Effect.gen(function* () {
        const state: State = { data: longThread(), server, oldProjectOverride: true };
        const { sent, done } = yield* compaction(state);
        yield* done;
        expect(compactions(sent)).toHaveLength(compacts);
      }),
  );
});

describe("skipIdleCompactionOfOwnedThreads", () => {
  const command = (idleCompaction: boolean): OrchestrationV2ServerCommand =>
    ({
      type: "message.dispatch",
      threadId,
      ...(idleCompaction ? { idleCompaction: "beforeCacheExpires" } : {}),
    }) as unknown as OrchestrationV2ServerCommand;

  it.effect(
    "passes every other command through, and asks about the owner only for compaction",
    () =>
      Effect.gen(function* () {
        const sent: Array<OrchestrationV2ServerCommand> = [];
        const asked: Array<string> = [];
        const upstream = {
          dispatch: (sentCommand: OrchestrationV2ServerCommand) =>
            Effect.sync(() => {
              sent.push(sentCommand);
              return { sequence: sent.length, storedEvents: [] };
            }),
        } as unknown as Orchestrator.OrchestratorV2["Service"];
        const wrapped = HandoffOwner.skipIdleCompactionOfOwnedThreads(upstream, (id) =>
          Effect.sync(() => {
            asked.push(id);
            return true;
          }),
        );
        const other = command(false);
        const stop = { type: "thread.stop", threadId } as unknown as OrchestrationV2ServerCommand;
        yield* wrapped.dispatch(other);
        yield* wrapped.dispatch(stop);
        expect(sent).toEqual([other, stop]);
        expect(asked).toEqual([]);
        expect(yield* wrapped.dispatch(command(true))).toEqual({ sequence: 0, storedEvents: [] });
        expect(sent).toEqual([other, stop]);
        expect(asked).toEqual([threadId]);
      }),
  );
});

describe("server.ts wiring", () => {
  it.effect(
    "gives the wrapper to the runtime workers' pipe, which holds upstream's idle compaction worker",
    () =>
      Effect.gen(function* () {
        const path = yield* Path.Path;
        const fileSystem = yield* FileSystem.FileSystem;
        const server = yield* fileSystem.readFileString(
          yield* path.fromFileUrl(new URL("../../server.ts", import.meta.url)),
        );
        const start = server.indexOf("const layerRuntimeCoreDependenciesBase = Layer.mergeAll(");
        expect(start).toBeGreaterThan(-1);
        // The mergeAll's own pipe, at the start of a line (its members have pipes of their own).
        const pipe = server.indexOf("\n).pipe(\n", start) + 1;
        expect(pipe).toBeGreaterThan(start);
        const block = server.slice(start, pipe);
        expect(block).toMatch(/^\s*layerIdleCompactionWorker,$/m);
        // First in the pipe: below it, upstream's orchestrator reaches only the workers above.
        const firstInPipe = server.slice(
          pipe,
          server.indexOf("\n", server.indexOf("\n", pipe) + 1),
        );
        expect(firstInPipe).toContain("Layer.provide(ForkLayers.handoffThreadsSkipIdleCompaction)");
        expect(server.split("ForkLayers.handoffThreadsSkipIdleCompaction")).toHaveLength(2);
      }).pipe(Effect.provide(NodeServices.layer)),
  );
});
