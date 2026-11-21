/**
 * Fork: agent slots (issue 724) for upstream's idle compaction and a delegated task's question to
 * its parent, through upstream's own worker and reactor behind the fork's wrapper.
 */
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  DEFAULT_SERVER_SETTINGS,
  EventId,
  NodeId,
  type OrchestrationV2DomainEvent,
  type OrchestrationV2ServerCommand,
  ProviderInstanceId,
  RunId,
  RuntimeRequestId,
  ThreadId,
} from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Queue from "effect/Queue";
import * as Stream from "effect/Stream";
import * as Tracer from "effect/Tracer";
import * as TestClock from "effect/testing/TestClock";

import { ServerConfig } from "../config.ts";
import * as DelegatedTaskQuestionReactor from "../orchestration-v2/DelegatedTaskQuestionReactor.ts";
import * as IdleCompactionService from "../orchestration-v2/IdleCompactionService.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProjectionStore from "../orchestration-v2/ProjectionStore.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import {
  at,
  MINUTE_MS,
  NOW_MS,
  projection,
  run,
  runId,
  threadId,
} from "./handoff/handoff.testkit.ts";
import * as WorkerWakeSlots from "./workerWakeSlots.ts";

const PARENT = ThreadId.make("thread:question-parent");
const CHILD = ThreadId.make("thread:question-child");
const TASK = NodeId.make("node:question-task");
const CHILD_RUN = RunId.make("run:question-child");
const REQUEST = RuntimeRequestId.make("request:question");
const WAKE_COMMAND = `server:delegated-task-question:${PARENT}:${REQUEST}`;

interface World {
  free: number;
  asks: Array<SlotGate.SlotAcquireInput>;
  /** The parent: idle, gone (archived), or not readable now. */
  parent: "idle" | "gone" | "unreadable";
  requestStatus: "pending" | "resolved";
  taskStatus: "running" | "completed";
  /** Every command that reached upstream's orchestrator. */
  sent: Array<OrchestrationV2ServerCommand>;
  logs: Array<string>;
}

const newWorld = (free: number): World => ({
  free,
  asks: [],
  parent: "idle",
  requestStatus: "pending",
  taskStatus: "running",
  sent: [],
  logs: [],
});

const gateOf = (world: World) =>
  Layer.mock(SlotGate.SlotGate)({
    acquire: (input) =>
      Effect.sync((): SlotGate.SlotAcquire => {
        world.asks.push(input);
        if (world.free === 0)
          return {
            type: "refuse",
            message: "full",
            refusal: { type: "unread", problem: "timeout" },
          };
        world.free -= 1;
        return { type: "allow", token: world.asks.length };
      }),
    started: () => Effect.void,
    release: () => Effect.void,
  });

const logsOf = (world: World) =>
  Logger.layer(
    [
      Logger.make(({ message, logLevel }) => {
        if (logLevel !== "Debug") world.logs.push(JSON.stringify(message));
      }),
    ],
    { mergeWithExisting: false },
  );

const shellOf = (world: World, id: ThreadId) =>
  world.parent === "unreadable" && id === PARENT
    ? Effect.fail(new Orchestrator.OrchestratorProjectionError({ threadId: id }))
    : Effect.succeed({
        id,
        deletedAt: null,
        archivedAt: world.parent === "gone" && id === PARENT ? at() : null,
        modelSelection: { instanceId: "claudeAgent" },
      } as never);

const question = (world: World) => ({
  id: REQUEST,
  nodeId: NodeId.make("node:child-root"),
  kind: "user_input",
  status: world.requestStatus,
  responseCapability: { type: "message" },
});

/** Upstream's orchestrator (it records), the stores upstream's reactor and the wrapper read. */
const upstreamOf = (world: World) =>
  Layer.mergeAll(
    Layer.mock(Orchestrator.OrchestratorV2)({
      dispatch: (command) =>
        Effect.sync(() => {
          world.sent.push(command);
          return { sequence: world.sent.length, storedEvents: [] };
        }),
      getThreadShell: (id) => shellOf(world, id),
      streamDomainEvents: Stream.never,
    }),
    Layer.mock(ProjectionStore.ProjectionStoreV2)({
      getThread: (id) =>
        Effect.succeed({
          id,
          lineage: { parentThreadId: id === CHILD ? PARENT : null },
          forkedFrom: null,
        } as never),
      getRuntimeRequest: () => Effect.sync(() => question(world) as never),
      getThreadRecords: (id) =>
        Effect.sync(
          () =>
            (id === CHILD
              ? { runs: [{ id: CHILD_RUN, ordinal: 1, delegatedTaskId: TASK }] }
              : {
                  thread: { archivedAt: null, deletedAt: null },
                  runs: [],
                  subagents: [
                    {
                      origin: "app_owned",
                      id: TASK,
                      threadId: PARENT,
                      childThreadId: CHILD,
                      title: "Research",
                      prompt: "Research the sources.",
                      completionWake: "always",
                      status: world.taskStatus,
                    },
                  ],
                }) as never,
        ),
    }),
  );

/** One server: the wrapper over upstream, with the state folder `dir` (a restart reuses it). */
const server = (world: World, dir: string) =>
  WorkerWakeSlots.layerOver.pipe(
    Layer.provideMerge(
      Layer.mergeAll(upstreamOf(world), gateOf(world), ServerConfig.layerTest(process.cwd(), dir)),
    ),
    Layer.provideMerge(logsOf(world)),
    Layer.provideMerge(NodeServices.layer),
  );

const questionAsked: OrchestrationV2DomainEvent = {
  id: EventId.make("event:question"),
  type: "runtime-request.updated",
  threadId: CHILD,
  runId: CHILD_RUN,
  occurredAt: at(),
  payload: { id: REQUEST, kind: "user_input", status: "pending" },
} as never;

/** Upstream's reactor handles the child's question, through the wrapped orchestrator. */
const ask = DelegatedTaskQuestionReactor.DelegatedTaskQuestionReactor.pipe(
  Effect.flatMap((reactor) => reactor.handle(questionAsked)),
  Effect.provide(DelegatedTaskQuestionReactor.layer),
);

const wakes = (world: World) => world.sent.filter((command) => command.commandId === WAKE_COMMAND);

const heldFile = (dir: string) =>
  Effect.gen(function* () {
    const path = yield* Path.Path;
    const config = yield* ServerConfig.pipe(
      Effect.provide(ServerConfig.layerTest(process.cwd(), dir)),
    );
    return path.join(config.stateDir, WorkerWakeSlots.HELD_WAKES_FILE);
  }).pipe(Effect.provide(NodeServices.layer));

const heldIds = (dir: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const file = yield* heldFile(dir);
    if (!(yield* fs.exists(file))) return [];
    const parsed = JSON.parse(yield* fs.readFileString(file)) as {
      wakes: ReadonlyArray<{ commandId: string }>;
    };
    return parsed.wakes.map((wake) => wake.commandId);
  }).pipe(Effect.provide(NodeServices.layer));

const tempDir = FileSystem.FileSystem.pipe(
  Effect.flatMap((fs) => fs.makeTempDirectoryScoped({ prefix: "t3-fork-held-wakes-" })),
  Effect.provide(NodeServices.layer),
);

const waitLogs = (world: World) =>
  world.logs.filter((log) => log.includes("question waits for an agent slot"));

describe("a delegated task's question to an idle parent takes an agent slot", () => {
  it.effect(
    "full: held in the file, asked every 5 s, sent once with upstream's ids when a slot frees",
    () =>
      Effect.gen(function* () {
        const dir = yield* tempDir;
        const world = newWorld(0);
        yield* Effect.gen(function* () {
          yield* ask;
          assert.deepStrictEqual(wakes(world), []);
          assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
          assert.deepStrictEqual(world.asks[0], {
            callerThreadId: PARENT,
            targetInstanceId: ProviderInstanceId.make("claudeAgent"),
            intoThreadId: PARENT,
            keepsHeld: true,
          });
          // Upstream's reactor asks again (a later event): still one held, never two.
          yield* ask;
          yield* TestClock.adjust("1 minute");
          assert.deepStrictEqual(wakes(world), []);
          assert.isAtLeast(world.asks.length, 12);
          assert.lengthOf(waitLogs(world), 1);
          world.free = 1;
          yield* TestClock.adjust("5 seconds");
          assert.lengthOf(wakes(world), 1);
          assert.strictEqual(
            (wakes(world)[0] as { messageId: string }).messageId,
            `message:delegated-task-question:${PARENT}:${REQUEST}`,
          );
          assert.deepStrictEqual(yield* heldIds(dir), []);
          yield* TestClock.adjust("1 minute");
          assert.lengthOf(wakes(world), 1);
        }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
      }).pipe(Effect.scoped),
  );

  it.effect("a free slot sends it at once, as upstream does", () =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(1);
      yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
      assert.lengthOf(wakes(world), 1);
      assert.deepStrictEqual(yield* heldIds(dir), []);
    }).pipe(Effect.scoped),
  );

  it.effect(
    "a restart while one waits: the next server sends it once; the one after, nothing",
    () =>
      Effect.gen(function* () {
        const dir = yield* tempDir;
        const world = newWorld(0);
        yield* ask.pipe(
          Effect.andThen(TestClock.adjust("12 seconds")),
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        assert.deepStrictEqual(wakes(world), []);
        world.free = 1;
        yield* TestClock.adjust("5 seconds").pipe(
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        assert.lengthOf(wakes(world), 1);
        world.free = 5;
        yield* TestClock.adjust("1 minute").pipe(Effect.provide(server(world, dir)), Effect.scoped);
        assert.lengthOf(wakes(world), 1);
      }).pipe(Effect.scoped),
  );

  it.effect(
    "stopped between the send and the file write: sent again with the same command id",
    () =>
      Effect.gen(function* () {
        const dir = yield* tempDir;
        const world = newWorld(0);
        yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
        const file = yield* heldFile(dir);
        const fs = yield* FileSystem.FileSystem.pipe(Effect.provide(NodeServices.layer));
        const held = yield* fs.readFileString(file).pipe(Effect.provide(NodeServices.layer));
        world.free = 1;
        yield* TestClock.adjust("5 seconds").pipe(
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        // The write after the send is lost: the file still names it.
        yield* fs.writeFileString(file, held).pipe(Effect.provide(NodeServices.layer));
        world.free = 1;
        yield* TestClock.adjust("5 seconds").pipe(
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        // Upstream's command receipts replay a known command id: one message in the thread.
        assert.lengthOf(wakes(world), 2);
        assert.lengthOf(new Set(wakes(world).map((command) => command.commandId)), 1);
        assert.deepStrictEqual(yield* heldIds(dir), []);
      }).pipe(Effect.scoped),
  );

  it.effect.each([
    {
      name: "the question was answered",
      change: (world: World) => (world.requestStatus = "resolved"),
    },
    { name: "its task ended", change: (world: World) => (world.taskStatus = "completed") },
    { name: "the parent was archived", change: (world: World) => (world.parent = "gone") },
  ])("$name while it waited: it leaves the file and is never sent", ({ change }) =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(0);
      yield* Effect.gen(function* () {
        yield* ask;
        change(world);
        world.free = 5;
        yield* TestClock.adjust("5 seconds");
        assert.deepStrictEqual(yield* heldIds(dir), []);
        yield* TestClock.adjust("1 minute");
        assert.deepStrictEqual(wakes(world), []);
      }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
    }).pipe(Effect.scoped),
  );

  it.effect("a parent that cannot be read waits, then is sent once", () =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(5);
      world.parent = "unreadable";
      yield* Effect.gen(function* () {
        yield* ask;
        yield* TestClock.adjust("30 seconds");
        assert.deepStrictEqual(wakes(world), []);
        assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
        world.parent = "idle";
        yield* TestClock.adjust("5 seconds");
        assert.lengthOf(wakes(world), 1);
      }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
    }).pipe(Effect.scoped),
  );

  it.effect("old, empty, broken or missing file: nothing sent from it; a new hold writes it", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem.pipe(Effect.provide(NodeServices.layer));
      for (const contents of [null, "", '{"version":1,"wakes":[]}', "{not json", '{"version":9}']) {
        const dir = yield* tempDir;
        const world = newWorld(0);
        const file = yield* heldFile(dir);
        if (contents !== null)
          yield* fs.writeFileString(file, contents).pipe(Effect.provide(NodeServices.layer));
        yield* Effect.gen(function* () {
          yield* TestClock.adjust("10 seconds");
          assert.deepStrictEqual(world.sent, []);
          yield* ask;
          assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
        }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
      }
    }).pipe(Effect.scoped),
  );

  it.effect("an unreadable entry is left out; the readable one beside it is sent", () =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(0);
      yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
      const file = yield* heldFile(dir);
      const fs = yield* FileSystem.FileSystem.pipe(Effect.provide(NodeServices.layer));
      const stored = JSON.parse(
        yield* fs.readFileString(file).pipe(Effect.provide(NodeServices.layer)),
      ) as { version: 1; wakes: Array<unknown> };
      yield* fs
        .writeFileString(
          file,
          JSON.stringify({ ...stored, wakes: [{ type: "?" }, ...stored.wakes] }),
        )
        .pipe(Effect.provide(NodeServices.layer));
      world.free = 1;
      yield* TestClock.adjust("5 seconds").pipe(Effect.provide(server(world, dir)), Effect.scoped);
      assert.lengthOf(wakes(world), 1);
      assert.isTrue(world.logs.some((log) => log.includes("held wakes left out")));
    }).pipe(Effect.scoped),
  );

  it.effect("no gate: upstream's own orchestrator", () =>
    Effect.gen(function* () {
      const inner = { dispatch: () => Effect.die("unused") } as never;
      const slots = yield* WorkerWakeSlots.makeWorkerWakeSlots({
        orchestrator: inner,
        gate: Option.none(),
        threadOf: () => Effect.succeed(null),
        questionOpen: () => Effect.succeed(true),
        store: { load: Effect.succeed([]), save: () => Effect.void },
      });
      assert.strictEqual(slots.orchestrator, inner);
    }),
  );
});

/** Upstream's idle compaction worker for the handoff test thread, behind the wrapper. */
const compaction = (world: World) =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW_MS);
    const decided = yield* Deferred.make<void>();
    const tracer = Tracer.make({
      span(options) {
        const span = new Tracer.NativeSpan(options);
        const end = span.end.bind(span);
        span.end = (...args) => {
          end(...args);
          if (span.name === "IdleCompactionService.compactWhenDue")
            Deferred.doneUnsafe(decided, Effect.void);
        };
        return span;
      },
    });
    const events = yield* Queue.unbounded<OrchestrationV2DomainEvent>();
    yield* Queue.offer(events, {
      id: EventId.make("event:run-1:completed"),
      type: "run.updated",
      threadId,
      runId,
      occurredAt: at(),
      payload: run(),
    } as OrchestrationV2DomainEvent);
    const data = projection({ usedTokens: 250_000, cacheExpiresInMs: 2 * MINUTE_MS });
    const slots = yield* WorkerWakeSlots.makeWorkerWakeSlots({
      orchestrator: {
        streamDomainEvents: Stream.fromQueue(events),
        dispatch: (command: OrchestrationV2ServerCommand) =>
          Effect.sync(() => {
            world.sent.push(command);
            return { sequence: world.sent.length, storedEvents: [] };
          }),
      } as never,
      gate: Option.some(yield* SlotGate.SlotGate.pipe(Effect.provide(gateOf(world)))),
      threadOf: () => Effect.succeed({ instanceId: data.thread.modelSelection.instanceId }),
      questionOpen: () => Effect.succeed(true),
      store: { load: Effect.succeed([]), save: () => Effect.void },
    });
    yield* IdleCompactionService.make.pipe(
      Effect.flatMap((service) => service.start()),
      Effect.provideService(Orchestrator.OrchestratorV2, slots.orchestrator),
      Effect.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getThread: () => Effect.succeed(data.thread),
            getThreadRecords: () => Effect.succeed(data),
          }),
          Layer.mock(ServerSettings.ServerSettingsService)({
            getSettings: Effect.succeed({
              ...DEFAULT_SERVER_SETTINGS,
              idleCompaction: "beforeCacheExpires",
            }),
          }),
          logsOf(world),
        ),
      ),
      Effect.withTracer(tracer),
    );
    yield* Deferred.await(decided);
  });

describe("upstream's idle compaction takes an agent slot", () => {
  it.effect("a free slot: compacted as upstream does, asked as the thread's own start", () =>
    Effect.gen(function* () {
      const world = newWorld(1);
      yield* compaction(world);
      assert.lengthOf(world.sent, 1);
      assert.deepStrictEqual(world.asks[0], {
        callerThreadId: threadId,
        targetInstanceId: ProviderInstanceId.make("claudeAgent"),
        intoThreadId: threadId,
        keepsHeld: true,
      });
    }),
  );

  it.effect("full: skipped and logged, never sent late (the prompt cache it saves expires)", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      yield* compaction(world);
      assert.deepStrictEqual(world.sent, []);
      assert.isTrue(world.logs.some((log) => log.includes("idle compaction skipped")));
      world.free = 5;
      yield* TestClock.adjust("1 hour");
      assert.deepStrictEqual(world.sent, []);
    }),
  );
});
