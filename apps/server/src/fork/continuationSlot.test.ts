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
  MessageId,
  RunId,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as ProviderContinuationRequests from "@t3tools/provider-core/server/ProviderContinuationRequests";
import * as Deferred from "effect/Deferred";
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
  /** The gate's calls after an ask: `started <thread>`, `release <token>`, `leave <key>`. */
  calls: Array<string>;
  /** Threads whose ask takes this long (a slow count read), then is refused. */
  slow: Set<ThreadId>;
  /** Threads gone (`null` shell), and whether a shell read fails. */
  gone: Set<ThreadId>;
  /** Threads the gate sees live but upstream's worker then reads archived. */
  archivedForUpstream: Set<ThreadId>;
  unreadable: boolean;
  logs: Array<string>;
}

const newWorld = (free: number): World => ({
  free,
  full: new Set(),
  asks: [],
  calls: [],
  slow: new Set(),
  gone: new Set(),
  archivedForUpstream: new Set(),
  unreadable: false,
  logs: [],
});

const gateOf = (world: World) =>
  Layer.mock(SlotGate.SlotGate)({
    acquire: (input) =>
      Effect.gen(function* () {
        world.asks.push(input);
        const refused: SlotGate.SlotAcquire = {
          type: "refuse",
          message: "full",
          refusal: { type: "unread", problem: "timeout" },
        };
        if (world.slow.has(input.callerThreadId!)) {
          yield* Effect.sleep("5 seconds");
          return refused;
        }
        if (world.free === 0 || world.full.has(input.callerThreadId!)) return refused;
        world.free -= 1;
        return { type: "allow", token: world.asks.length } satisfies SlotGate.SlotAcquire;
      }),
    started: (token, childThreadId) =>
      Effect.sync(() => world.calls.push(`started ${childThreadId} ${token}`)),
    release: (token) => Effect.sync(() => world.calls.push(`release ${token}`)),
    leave: (keys) => Effect.sync(() => world.calls.push(...keys.map((key) => `leave ${key}`))),
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
        world.gone.has(threadId) || world.archivedForUpstream.has(threadId)
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

/** Lets forked fibers run (scheduler turns, no clock moved). */
const settle = Effect.forEach(Array.from({ length: 50 }), () => Effect.yieldNow, { discard: true });

/** The gate's slot calls, without the line's. */
const slotCalls = (world: World) => world.calls.filter((call) => !call.startsWith("leave "));

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
            place: { key: "continuation:0" },
          });
          // Every ask in its one place in the gate's line.
          assert.isTrue(world.asks.every((asked) => asked.place?.key === "continuation:0"));
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

  it.effect("a slow count read for one thread holds up no other thread's continuation", () =>
    Effect.gen(function* () {
      const world = newWorld(5);
      world.slow.add(A);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      yield* Effect.gen(function* () {
        yield* offer(request(A));
        yield* offer(request(B));
        // No time passes: B is not queued behind A's 5-second ask.
        yield* settle;
        assert.strictEqual(yield* Queue.size(dispatched), 1);
        const sent = yield* Queue.take(dispatched);
        assert.strictEqual(sent.type === "message.dispatch" && sent.threadId, B);
        // A's ask ends (it cannot be cut off half-way), so the server can stop.
        yield* TestClock.adjust("5 seconds");
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
    }),
  );

  it.effect(
    "a granted slot is held until upstream's dispatch of it ends, however long it waits",
    () =>
      Effect.gen(function* () {
        const world = newWorld(1);
        const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
        const lockFree = yield* Deferred.make<void>();
        yield* Effect.gen(function* () {
          yield* offer(
            request(A, {
              // Behind its own thread's earlier dispatch (its parent's lock) for 2 minutes.
              dispatchIfCurrent: (effect) =>
                Deferred.await(lockFree).pipe(Effect.andThen(Effect.asSome(effect))),
            }),
          );
          yield* TestClock.adjust("2 minutes");
          assert.lengthOf(world.asks, 1);
          // Asked and granted, not ended: the start counts whatever the start grace.
          assert.deepStrictEqual(slotCalls(world), []);
          yield* Deferred.succeed(lockFree, undefined);
          yield* Queue.take(dispatched);
          yield* settle;
          assert.deepStrictEqual(slotCalls(world), [`started ${A} 1`, "release 1"]);
        }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
      }),
  );

  it.effect("a granted request upstream drops (its thread archived meanwhile) frees its slot", () =>
    Effect.gen(function* () {
      const world = newWorld(1);
      const dispatched = yield* Queue.unbounded<OrchestrationV2ServerCommand>();
      const cleared: Array<ThreadId> = [];
      yield* Effect.gen(function* () {
        // Granted on a live shell; upstream then reads the thread archived and clears the latch.
        world.archivedForUpstream.add(A);
        yield* offer(request(A, { clearIfCurrent: () => Effect.sync(() => cleared.push(A)) }));
        yield* settle;
        assert.deepStrictEqual(cleared, [A]);
        assert.strictEqual(yield* Queue.size(dispatched), 0);
        assert.deepStrictEqual(slotCalls(world), [`started ${A} 1`, "release 1"]);
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
        yield* settle;
        assert.deepStrictEqual(cleared, [A]);
        assert.deepStrictEqual(world.asks, []);
        assert.strictEqual(yield* Queue.size(dispatched), 0);
      }).pipe(Effect.provide(server(world, dispatched)), Effect.scoped);
    }),
  );

  it.effect("a delegated result is upstream's at once: no slot asked, even with none free", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const inner =
        yield* Queue.unbounded<ProviderContinuationRequests.ProviderContinuationRequest>();
      const gated = yield* ContinuationSlot.gateContinuations(
        {
          offer: (value) => Queue.offer(inner, value).pipe(Effect.asVoid),
          take: Queue.take(inner),
        },
        Option.some(yield* SlotGate.SlotGate.pipe(Effect.provide(gateOf(world)))),
        () => Effect.succeed({ instanceId: INSTANCE }),
        yield* Effect.scope,
      );
      const delegated = request(A, {
        delegatedCompletion: {
          parentRunId: RunId.make("run:continuation-a"),
          generation: 1,
          messageId: MessageId.make("message:continuation-a"),
        },
      });
      yield* gated.offer(delegated);
      const taken: Array<ProviderContinuationRequests.ProviderContinuationRequest> = [];
      yield* gated.take.pipe(
        Effect.tap((value) => Effect.sync(() => taken.push(value))),
        Effect.forkScoped,
      );
      yield* TestClock.adjust("1 minute");
      assert.deepStrictEqual(taken, [delegated]);
      assert.deepStrictEqual(world.asks, []);
    }).pipe(Effect.scoped),
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
      // Its place in the line goes with it.
      assert.include(world.calls, "leave continuation:0");
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
        yield* ContinuationSlot.gateContinuations(
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
