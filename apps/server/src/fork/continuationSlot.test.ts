/**
 * Fork: agent slots (issue 724) for upstream's provider continuation, through upstream's real
 * continuation worker over a gate with a set number of free slots.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ServerCommand,
  type OrchestrationV2ThreadProjection,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderThreadId,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as ProviderContinuationRequests from "@t3tools/provider-core/server/ProviderContinuationRequests";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Queue from "effect/Queue";
import * as TestClock from "effect/testing/TestClock";
import * as NodeServices from "@effect/platform-node/NodeServices";

import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProviderContinuationService from "../orchestration-v2/ProviderContinuationService.ts";
import * as ThreadManagement from "../orchestration-v2/ThreadManagementService.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as WaitWorker from "../waits/WaitWorker.ts";
import * as ContinuationSlot from "./continuationSlot.ts";

const INSTANCE = ProviderInstanceId.make("claudeAgent");
const [A, B] = ["thread:continuation-a", "thread:continuation-b"].map((id) =>
  ThreadId.make(id),
) as [ThreadId, ThreadId];

const request = (
  threadId: ThreadId,
  patch: Partial<ProviderContinuationRequests.ProviderContinuationRequest> = {},
): ProviderContinuationRequests.ProviderContinuationRequest => ({
  threadId,
  providerThreadId: ProviderThreadId.make(`provider:${threadId}`),
  driver: ProviderDriverKind.make("claudeAgent"),
  detail: null,
  ...patch,
});

interface World {
  /** Free slots; each start the gate allows takes one. */
  free: number;
  /** Threads the gate refuses whatever is free. */
  full: Set<ThreadId>;
  asks: Array<SlotGate.SlotAcquireInput>;
  /** Threads gone (`null` shell), and whether a shell read fails. */
  gone: Set<ThreadId>;
  unreadable: boolean;
  logs: Array<string>;
}

const newWorld = (free: number): World => ({
  free,
  full: new Set(),
  asks: [],
  gone: new Set(),
  unreadable: false,
  logs: [],
});

const gateOf = (world: World) =>
  Layer.mock(SlotGate.SlotGate)({
    acquire: (input) =>
      Effect.sync((): SlotGate.SlotAcquire => {
        world.asks.push(input);
        if (world.free === 0 || world.full.has(input.callerThreadId!))
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

const projection = {
  thread: { archivedAt: null, deletedAt: null },
  messages: [],
  runs: [],
} as unknown as OrchestrationV2ThreadProjection;

/** Upstream's queue and worker, the worker reading it through the gate; `null`: no gate. */
const server = (
  world: World,
  dispatched: Queue.Queue<OrchestrationV2ServerCommand>,
  gate: "gate" | null = "gate",
) => {
  const threads = Layer.mock(ThreadManagement.ThreadManagementService)({
    getThreadShell: (threadId) =>
      world.unreadable
        ? Effect.fail(new Orchestrator.OrchestratorProjectionError({ threadId }))
        : Effect.succeed(
            world.gone.has(threadId)
              ? null
              : ({
                  id: threadId,
                  deletedAt: null,
                  archivedAt: null,
                  modelSelection: { instanceId: INSTANCE },
                } as never),
          ),
    getThreadRecords: (threadId) =>
      Effect.succeed(
        world.gone.has(threadId)
          ? { ...projection, thread: { archivedAt: "2026-11-21T00:00:00.000Z", deletedAt: null } }
          : projection,
      ) as never,
    dispatch: (command) =>
      Queue.offer(dispatched, command).pipe(Effect.as({ sequence: 1, storedEvents: [] })),
  });
  const worker = ProviderContinuationService.layer.pipe(
    Layer.provide(ContinuationSlot.layerOver),
    Layer.provide(
      Layer.mergeAll(
        IdAllocator.layer,
        ProviderContinuationRequests.layer,
        threads,
        ...(gate === null ? [] : [gateOf(world)]),
      ),
    ),
  );
  return Layer.merge(ProviderContinuationRequests.layer, worker).pipe(
    Layer.provideMerge(
      Logger.layer(
        [
          Logger.make(({ message, logLevel }) => {
            if (logLevel === "Info") world.logs.push(JSON.stringify(message));
          }),
        ],
        { mergeWithExisting: false },
      ),
    ),
  );
};

const offer = (value: ProviderContinuationRequests.ProviderContinuationRequest) =>
  ProviderContinuationRequests.ProviderContinuationRequests.pipe(
    Effect.flatMap((requests) => requests.offer(value)),
  );

const waitLogs = (world: World) =>
  world.logs.filter((log) => log.includes("provider continuation waits for an agent slot"));

describe("provider continuations take an agent slot", () => {
  it.effect(
    "full: waits outside the adapter's lock, asked every 5 s, logged once, sent once with a slot",
    () =>
      Effect.gen(function* () {
        const world = newWorld(0);
        const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
        const locked: Array<ThreadId> = [];
        yield* Effect.gen(function* () {
          yield* offer(
            request(A, {
              // The adapter's own lock is taken here: never while the request waits.
              dispatchIfCurrent: (effect) =>
                Effect.sync(() => locked.push(A)).pipe(Effect.andThen(Effect.asSome(effect))),
            }),
          );
          yield* TestClock.adjust("0 seconds");
          yield* TestClock.adjust("1 minute");
          assert.strictEqual(yield* Queue.size(dispatched), 0);
          assert.deepStrictEqual(locked, []);
          assert.isAtLeast(world.asks.length, 12);
          assert.lengthOf(waitLogs(world), 1);
          assert.deepStrictEqual(world.asks[0], {
            callerThreadId: A,
            targetInstanceId: INSTANCE,
            intoThreadId: A,
            keepsHeld: true,
          });
          world.free = 1;
          yield* TestClock.adjust("5 seconds");
          const sent = yield* Queue.take(dispatched);
          assert.strictEqual(sent.type === "message.dispatch" && sent.threadId, A);
          assert.deepStrictEqual(locked, [A]);
          yield* TestClock.adjust("1 minute");
          assert.strictEqual(yield* Queue.size(dispatched), 0);
        }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
      }),
  );

  it.effect("a waiting thread holds up no other thread's continuation", () =>
    Effect.gen(function* () {
      const world = newWorld(5);
      world.full.add(A);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      yield* Effect.gen(function* () {
        yield* offer(request(A));
        yield* offer(request(B));
        const sent = yield* Queue.take(dispatched);
        assert.strictEqual(sent.type === "message.dispatch" && sent.threadId, B);
        yield* TestClock.adjust("30 seconds");
        assert.strictEqual(yield* Queue.size(dispatched), 0);
        world.full.delete(A);
        yield* TestClock.adjust("5 seconds");
        const late = yield* Queue.take(dispatched);
        assert.strictEqual(late.type === "message.dispatch" && late.threadId, A);
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
    }),
  );

  it.effect("a thread that cannot be read waits, then is sent once", () =>
    Effect.gen(function* () {
      const world = newWorld(5);
      world.unreadable = true;
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      yield* Effect.gen(function* () {
        yield* offer(request(A));
        yield* TestClock.adjust("30 seconds");
        assert.strictEqual(yield* Queue.size(dispatched), 0);
        assert.deepStrictEqual(world.asks, []);
        world.unreadable = false;
        yield* TestClock.adjust("5 seconds");
        yield* Queue.take(dispatched);
        yield* TestClock.adjust("1 minute");
        assert.strictEqual(yield* Queue.size(dispatched), 0);
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
    }),
  );

  it.effect("a gone thread is upstream's at once: no slot asked, the adapter's latch cleared", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      world.gone.add(A);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      const cleared: Array<ThreadId> = [];
      yield* Effect.gen(function* () {
        yield* offer(request(A, { clearIfCurrent: () => Effect.sync(() => cleared.push(A)) }));
        yield* TestClock.adjust("0 seconds");
        assert.deepStrictEqual(cleared, [A]);
        assert.deepStrictEqual(world.asks, []);
        assert.strictEqual(yield* Queue.size(dispatched), 0);
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
    }),
  );

  it.effect("stopped while one waits: nothing is sent later, half-way or twice", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      yield* Effect.gen(function* () {
        yield* offer(request(A));
        yield* TestClock.adjust("12 seconds");
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
      const asked = world.asks.length;
      world.free = 5;
      yield* TestClock.adjust("1 minute");
      assert.strictEqual(yield* Queue.size(dispatched), 0);
      assert.strictEqual(world.asks.length, asked);
    }),
  );

  it.effect("no gate: upstream's own queue, sent at once", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      yield* Effect.gen(function* () {
        yield* offer(request(A));
        yield* Queue.take(dispatched);
      }).pipe(Effect.provide(server(world, dispatched, null)), Effect.scoped);
      const inner = { offer: () => Effect.void, take: Effect.never };
      assert.strictEqual(
        ContinuationSlot.gateContinuations(
          inner,
          Option.none(),
          () => Effect.succeed(null),
          null as never,
        ),
        inner,
      );
    }),
  );

  it.effect("runtime wiring: upstream's continuation worker reads the gated queue", () =>
    Effect.gen(function* () {
      assert.strictEqual(WaitWorker.continuationSlot, ContinuationSlot.layer);
      const path = yield* Path.Path;
      const fileSystem = yield* FileSystem.FileSystem;
      const runtime = yield* fileSystem.readFileString(
        yield* path.fromFileUrl(new URL("../orchestration-v2/runtimeLayer.ts", import.meta.url)),
      );
      const start = runtime.indexOf(
        "const layerProviderContinuationWorkerProvided = ProviderContinuationService.layer.pipe(\n",
      );
      assert.isAbove(start, -1);
      const firstInPipe = runtime.slice(
        start,
        runtime.indexOf("\n", runtime.indexOf("\n", start) + 1),
      );
      assert.include(firstInPipe, "Layer.provide(WaitWorker.continuationSlot)");
      assert.lengthOf(runtime.split("ProviderContinuationService.layer"), 2);
      // Over the server's one gate.
      const own = yield* fileSystem.readFileString(
        yield* path.fromFileUrl(new URL("./continuationSlot.ts", import.meta.url)),
      );
      assert.include(own, "export const layer = layerOver.pipe(Layer.provide(SlotGate.layer));");
      // The wait worker keeps its gate, now given in its own file rather than runtimeLayer.ts.
      const worker = yield* fileSystem.readFileString(
        yield* path.fromFileUrl(new URL("../waits/WaitWorker.ts", import.meta.url)),
      );
      assert.include(worker, "Layer.provide(SlotGate.layer),");
    }).pipe(Effect.provide(NodeServices.layer)),
  );
});
