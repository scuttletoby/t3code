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
import * as Clock from "effect/Clock";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
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
  /** Keys that left the gate's line. */
  left: Array<string>;
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
  left: [],
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
    leave: (keys) => Effect.sync(() => world.left.push(...keys)),
  });

const QUESTION_PLACE = { key: `question-wake:${WAKE_COMMAND}` };

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

/** One server: the wrapper over upstream, on `disk` (a restart reuses it). */
const server = (world: World, disk: Disk) =>
  WorkerWakeSlots.layerOver.pipe(
    // The held-wakes file is on the test's disk; the server's folders are real.
    Layer.provide(disk.layer),
    Layer.provideMerge(
      Layer.mergeAll(
        upstreamOf(world),
        gateOf(world),
        ServerConfig.layerTest(process.cwd(), disk.dir),
        ServerSettings.layerTest({}),
      ),
    ),
    Layer.provideMerge(logsOf(world)),
    Layer.provideMerge(NodeServices.layer),
  );

/**
 * The wrapper's file system: in memory, so the test clock alone orders its reads and writes. The
 * server's folders are real (`dir`). `exists` of the held-wakes file fails while `failing.exists`
 * is set; every rename and sync is recorded in `ops` (by base name).
 */
interface Disk {
  readonly dir: string;
  readonly files: Map<string, string>;
  readonly ops: Array<string>;
  readonly failing: { exists: boolean; rename: boolean };
  readonly layer: Layer.Layer<FileSystem.FileSystem>;
}

const baseName = (file: string) => file.slice(file.lastIndexOf("/") + 1);
const diskError = (tag: "NotFound" | "PermissionDenied", method: string, file: string) =>
  PlatformError.systemError({ _tag: tag, module: "FileSystem", method, pathOrDescriptor: file });

const diskOver = (dir: string): Disk => {
  const files = new Map<string, string>();
  const ops: Array<string> = [];
  const failing = { exists: false, rename: false };
  const layer = FileSystem.layerNoop({
    exists: (file) =>
      failing.exists && baseName(file) === WorkerWakeSlots.HELD_WAKES_FILE
        ? Effect.fail(diskError("PermissionDenied", "exists", file))
        : Effect.succeed(files.has(file)),
    readFileString: (file) =>
      Effect.suspend(() => {
        const text = files.get(file);
        return text === undefined
          ? Effect.fail(diskError("NotFound", "readFileString", file))
          : Effect.succeed(text);
      }),
    rename: (from, to) =>
      Effect.suspend(() => {
        if (failing.rename && baseName(from) === WorkerWakeSlots.HELD_WAKES_FILE)
          return Effect.fail(diskError("PermissionDenied", "rename", from));
        const text = files.get(from);
        if (text === undefined) return Effect.fail(diskError("NotFound", "rename", from));
        ops.push(`rename ${baseName(from)} ${baseName(to)}`);
        files.delete(from);
        files.set(to, text);
        return Effect.void;
      }),
    open: (file, options) =>
      Effect.sync(() => {
        if (options?.flag === "w") files.set(file, "");
        return {
          writeAll: (bytes: Uint8Array) =>
            Effect.sync(() => {
              files.set(file, (files.get(file) ?? "") + new TextDecoder().decode(bytes));
            }),
          sync: Effect.sync(() => {
            ops.push(`sync ${baseName(file)}`);
          }),
        } as unknown as FileSystem.File;
      }),
  });
  return { dir, files, ops, failing, layer };
};

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

const heldFile = (disk: Disk) =>
  Effect.gen(function* () {
    const path = yield* Path.Path;
    const config = yield* ServerConfig.pipe(
      Effect.provide(ServerConfig.layerTest(process.cwd(), disk.dir)),
    );
    return path.join(config.stateDir, WorkerWakeSlots.HELD_WAKES_FILE);
  }).pipe(Effect.provide(NodeServices.layer));

const heldIds = (disk: Disk) =>
  Effect.gen(function* () {
    const text = disk.files.get(yield* heldFile(disk));
    if (text === undefined) return [];
    const parsed = JSON.parse(text) as { wakes: ReadonlyArray<{ commandId: string }> };
    return parsed.wakes.map((wake) => wake.commandId);
  });

/** A real folder for the server's own directories, and the held-wakes file in memory. */
const tempDir = FileSystem.FileSystem.pipe(
  Effect.flatMap((fs) => fs.makeTempDirectoryScoped({ prefix: "t3-fork-held-wakes-" })),
  Effect.map(diskOver),
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
            place: QUESTION_PLACE,
          });
          // Upstream's reactor asks again (a later event): still one held, never two.
          yield* ask;
          yield* TestClock.adjust("1 minute");
          assert.deepStrictEqual(wakes(world), []);
          assert.isAtLeast(world.asks.length, 12);
          // Every ask in its one place in the gate's line.
          assert.isTrue(world.asks.every((asked) => asked.place?.key === QUESTION_PLACE.key));
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
        // The next server asks at once, so the file's wakes take their places in line first.
        yield* TestClock.adjust("0 seconds").pipe(
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        assert.lengthOf(wakes(world), 1);
        assert.lengthOf(
          world.logs.filter((log) => log.includes("held delegated task questions loaded")),
          1,
        );
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
        const held = dir.files.get(file)!;
        world.free = 1;
        yield* TestClock.adjust("5 seconds").pipe(
          Effect.provide(server(world, dir)),
          Effect.scoped,
        );
        // The write after the send is lost: the file still names it.
        dir.files.set(file, held);
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
        // It holds no place in the line either.
        assert.include(world.left, QUESTION_PLACE.key);
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
        // Asked every 5 s, logged once.
        assert.lengthOf(
          world.logs.filter((log) => log.includes("its parent or question could not be read")),
          1,
        );
        world.parent = "idle";
        yield* TestClock.adjust("5 seconds");
        assert.lengthOf(wakes(world), 1);
      }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
    }).pipe(Effect.scoped),
  );

  it.effect(
    "old, empty, broken or missing file: nothing sent from it; a bad one is kept; a new hold writes it",
    () =>
      Effect.gen(function* () {
        for (const contents of [
          null,
          "",
          '{"version":1,"wakes":[]}',
          "{not json",
          '{"version":9}',
        ]) {
          const dir = yield* tempDir;
          const world = newWorld(0);
          const file = yield* heldFile(dir);
          if (contents !== null) dir.files.set(file, contents);
          yield* Effect.gen(function* () {
            yield* TestClock.adjust("10 seconds");
            assert.deepStrictEqual(world.sent, []);
            yield* ask;
            assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
          }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
          // A file that could not be read is kept beside the new one, and named in the log.
          const kept = [...dir.files.keys()]
            .map(baseName)
            .filter((name) => name.startsWith(`${WorkerWakeSlots.HELD_WAKES_FILE}.corrupt-`));
          if (contents === null || contents.startsWith('{"version":1')) {
            assert.deepStrictEqual(kept, []);
            continue;
          }
          assert.lengthOf(kept, 1);
          assert.strictEqual(
            dir.files.get(`${file}.corrupt-${kept[0]!.split("-").at(-1)}`),
            contents,
          );
          assert.isTrue(
            world.logs.some(
              (log) => log.includes("held wakes file unreadable") && log.includes(kept[0]!),
            ),
          );
        }
      }).pipe(Effect.scoped),
  );

  it.effect(
    "a send's save and a new hold's save run one at a time: the file ends with what is held",
    () =>
      Effect.gen(function* () {
        // Review B's case: one slot, W1 held. The retry sends W1 and starts a slow save of [].
        // Meanwhile W3 is refused and held. The file must end [W3], as memory does.
        const world = newWorld(1);
        const wake = (name: string) =>
          ({
            type: "message.dispatch",
            commandId: `server:delegated-task-question:${PARENT}:${name}`,
            messageId: `message:${name}`,
            threadId: PARENT,
          }) as never as Extract<OrchestrationV2ServerCommand, { type: "message.dispatch" }>;
        const [w1, w3] = [wake("w1"), wake("w3")];
        let file: ReadonlyArray<string> = [w1.commandId];
        let saves = 0;
        const firstSaveStarted = yield* Deferred.make<void>();
        const firstSaveMayEnd = yield* Deferred.make<void>();
        const refused = yield* Deferred.make<void>();
        const gate = yield* SlotGate.SlotGate.pipe(Effect.provide(gateOf(world)));
        const slots = yield* WorkerWakeSlots.makeWorkerWakeSlots({
          orchestrator: {
            dispatch: (command: OrchestrationV2ServerCommand) =>
              Effect.sync(() => {
                world.sent.push(command);
                return { sequence: world.sent.length, storedEvents: [] };
              }),
          } as never,
          gate: Option.some({
            ...gate,
            acquire: (input) =>
              gate
                .acquire(input)
                .pipe(
                  Effect.tap((slot) =>
                    slot.type === "refuse" ? Deferred.succeed(refused, undefined) : Effect.void,
                  ),
                ),
          }),
          threadOf: () => Effect.succeed({ instanceId: ProviderInstanceId.make("claudeAgent") }),
          questionOpen: () => Effect.succeed(true),
          compactionDue: () => Effect.succeed(false),
          store: {
            load: Effect.succeed([w1]),
            save: (held) =>
              Effect.gen(function* () {
                saves += 1;
                if (saves === 1) {
                  yield* Deferred.succeed(firstSaveStarted, undefined);
                  yield* Deferred.await(firstSaveMayEnd);
                }
                file = held.map((command) => command.commandId);
              }),
          },
        });
        const retrying = yield* Effect.forkChild(slots.retry);
        yield* Deferred.await(firstSaveStarted);
        assert.deepStrictEqual(
          world.sent.map((command) => command.commandId),
          [w1.commandId],
        );
        const holding = yield* Effect.forkChild(slots.orchestrator.dispatch(w3));
        // Refused, then held (one yield): its save runs now, or waits for the first (the lock).
        yield* Deferred.await(refused);
        yield* Effect.yieldNow;
        assert.isTrue(slots.held().some((command) => command.commandId === w3.commandId));
        yield* Deferred.succeed(firstSaveMayEnd, undefined);
        yield* Fiber.join(retrying);
        yield* Fiber.join(holding);
        assert.deepStrictEqual(
          slots.held().map((command) => command.commandId),
          [w3.commandId],
        );
        assert.deepStrictEqual(file, [w3.commandId]);
      }),
  );

  it.effect("an unreadable entry is left out; the readable one beside it is sent", () =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(0);
      yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
      const file = yield* heldFile(dir);
      const stored = JSON.parse(dir.files.get(file)!) as { version: 1; wakes: Array<unknown> };
      dir.files.set(file, JSON.stringify({ ...stored, wakes: [{ type: "?" }, ...stored.wakes] }));
      world.free = 1;
      yield* TestClock.adjust("5 seconds").pipe(Effect.provide(server(world, dir)), Effect.scoped);
      assert.lengthOf(wakes(world), 1);
      assert.isTrue(world.logs.some((log) => log.includes("held wakes left out")));
    }).pipe(Effect.scoped),
  );

  it.effect(
    "a file whose check fails is not missing: nothing written over it, sent once it reads",
    () =>
      Effect.gen(function* () {
        const dir = yield* tempDir;
        const world = newWorld(0);
        yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
        assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
        dir.failing.exists = true;
        world.free = 1;
        yield* Effect.gen(function* () {
          yield* TestClock.adjust("20 seconds");
          assert.deepStrictEqual(wakes(world), []);
          assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
          assert.lengthOf(
            world.logs.filter((log) => log.includes("held wakes file could not be read")),
            1,
          );
          dir.failing.exists = false;
          yield* TestClock.adjust("5 seconds");
          assert.lengthOf(wakes(world), 1);
          assert.deepStrictEqual(yield* heldIds(dir), []);
        }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
      }).pipe(Effect.scoped),
  );

  it.effect(
    "a broken file that cannot be set aside is kept as it is: nothing written over it, read again",
    () =>
      Effect.gen(function* () {
        const dir = yield* tempDir;
        const world = newWorld(0);
        const file = yield* heldFile(dir);
        dir.files.set(file, "{not json");
        dir.failing.rename = true;
        yield* Effect.gen(function* () {
          yield* ask;
          yield* TestClock.adjust("20 seconds");
          // Held in memory; the broken file is untouched, and the failure logged once.
          assert.strictEqual(dir.files.get(file), "{not json");
          assert.lengthOf(
            world.logs.filter((log) => log.includes("held wakes file could not be read")),
            1,
          );
          // It can be set aside now: kept as .corrupt-<ms>, and the held wake is written.
          dir.failing.rename = false;
          yield* TestClock.adjust("5 seconds");
          const kept = [...dir.files.keys()]
            .map(baseName)
            .filter((name) => name.startsWith(`${WorkerWakeSlots.HELD_WAKES_FILE}.corrupt-`));
          assert.lengthOf(kept, 1);
          assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
        }).pipe(Effect.provide(server(world, dir)), Effect.scoped);
      }).pipe(Effect.scoped),
  );

  it.effect(
    "a file that cannot be read yet: a new hold is kept in memory, the file's own wakes go first",
    () =>
      Effect.gen(function* () {
        const wake = (name: string) =>
          ({
            type: "message.dispatch",
            commandId: `server:delegated-task-question:${PARENT}:${name}`,
            messageId: `message:${name}`,
            threadId: PARENT,
          }) as never as Extract<OrchestrationV2ServerCommand, { type: "message.dispatch" }>;
        const [fromFile, held] = [wake("from-file"), wake("held")];
        const world = newWorld(0);
        let readable = false;
        const saves: Array<ReadonlyArray<string>> = [];
        const slots = yield* WorkerWakeSlots.makeWorkerWakeSlots({
          orchestrator: { dispatch: () => Effect.die("unused") } as never,
          gate: Option.some(yield* SlotGate.SlotGate.pipe(Effect.provide(gateOf(world)))),
          threadOf: () => Effect.succeed({ instanceId: ProviderInstanceId.make("claudeAgent") }),
          questionOpen: () => Effect.succeed(true),
          compactionDue: () => Effect.succeed(false),
          store: {
            load: Effect.suspend(() =>
              readable
                ? Effect.succeed([fromFile])
                : Effect.fail(diskError("PermissionDenied", "exists", "held")),
            ),
            save: (wakes) => Effect.sync(() => saves.push(wakes.map((one) => one.commandId))),
          },
        });
        yield* slots.orchestrator.dispatch(held);
        assert.deepStrictEqual(
          slots.held().map((one) => one.commandId),
          [held.commandId],
        );
        assert.deepStrictEqual(saves, []);
        readable = true;
        yield* slots.retry;
        assert.deepStrictEqual(
          slots.held().map((one) => one.commandId),
          [fromFile.commandId, held.commandId],
        );
        assert.deepStrictEqual(saves.at(-1), [fromFile.commandId, held.commandId]);
      }).pipe(Effect.provide(logsOf(newWorld(0)))),
  );

  it.effect("a save is synced before its rename, and the folder after it", () =>
    Effect.gen(function* () {
      const dir = yield* tempDir;
      const world = newWorld(0);
      const ops = dir.ops;
      yield* ask.pipe(Effect.provide(server(world, dir)), Effect.scoped);
      const file = WorkerWakeSlots.HELD_WAKES_FILE;
      const rename = ops.indexOf(`rename ${file}.tmp ${file}`);
      assert.isAbove(rename, -1);
      assert.include(ops.slice(0, rename), `sync ${file}.tmp`);
      const path = yield* Path.Path.pipe(Effect.provide(NodeServices.layer));
      const folder = baseName(path.dirname(yield* heldFile(dir)));
      assert.include(ops.slice(rename + 1), `sync ${folder}`);
      assert.deepStrictEqual(yield* heldIds(dir), [WAKE_COMMAND]);
    }).pipe(Effect.scoped),
  );

  it.effect("on the real disk: a save is read back whole, and a missing file is none", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const folder = yield* fs.makeTempDirectoryScoped({ prefix: "t3-fork-held-wakes-real-" });
      const store = yield* WorkerWakeSlots.fileStore(
        path.join(folder, WorkerWakeSlots.HELD_WAKES_FILE),
      );
      assert.deepStrictEqual(yield* store.load, []);
      const wake = {
        type: "message.dispatch",
        commandId: WAKE_COMMAND,
        messageId: `message:delegated-task-question:${PARENT}:${REQUEST}`,
        threadId: PARENT,
        text: "A question waits.",
        attachments: [],
        createdBy: "agent",
        creationSource: "server",
        dispatchMode: { type: "start_immediately" },
      } as never as Extract<OrchestrationV2ServerCommand, { type: "message.dispatch" }>;
      yield* store.save([wake]);
      assert.deepStrictEqual(
        (yield* store.load).map((one) => one.commandId),
        [WAKE_COMMAND],
      );
      assert.deepStrictEqual(yield* fs.readDirectory(folder), [WorkerWakeSlots.HELD_WAKES_FILE]);
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect("no gate: upstream's own orchestrator", () =>
    Effect.gen(function* () {
      const inner = { dispatch: () => Effect.die("unused") } as never;
      const slots = yield* WorkerWakeSlots.makeWorkerWakeSlots({
        orchestrator: inner,
        gate: Option.none(),
        threadOf: () => Effect.succeed(null),
        questionOpen: () => Effect.succeed(true),
        compactionDue: () => Effect.succeed(false),
        store: { load: Effect.succeed([]), save: () => Effect.void },
      });
      assert.strictEqual(slots.orchestrator, inner);
    }),
  );
});

/**
 * Upstream's idle compaction worker for the handoff test thread, behind the wrapper. Its window
 * runs from now to 2 minutes on; `thread.data` is what both upstream and the fork's check read.
 * Returns when upstream's worker is done with the run.
 */
const compaction = (
  world: World,
  thread: { data: ReturnType<typeof projection> },
  due?: (
    command: Extract<OrchestrationV2ServerCommand, { type: "message.dispatch" }>,
  ) => Effect.Effect<boolean, string>,
) =>
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
      threadOf: () => Effect.succeed({ instanceId: thread.data.thread.modelSelection.instanceId }),
      questionOpen: () => Effect.succeed(true),
      compactionDue:
        due ??
        ((command) =>
          Clock.currentTimeMillis.pipe(
            Effect.map((nowMs) =>
              WorkerWakeSlots.compactionWindowOpen(thread.data, command, nowMs),
            ),
          )),
      store: { load: Effect.succeed([]), save: () => Effect.void },
    });
    yield* IdleCompactionService.make.pipe(
      Effect.flatMap((service) => service.start()),
      Effect.provideService(Orchestrator.OrchestratorV2, slots.orchestrator),
      Effect.provide(
        Layer.mergeAll(
          Layer.mock(ProjectionStore.ProjectionStoreV2)({
            getThread: () => Effect.sync(() => thread.data.thread),
            getThreadRecords: () => Effect.sync(() => thread.data),
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
    yield* TestClock.adjust("0 seconds");
    return decided;
  });

const COMPACTION_PLACE = { key: `idle-compaction:server:idle-compact:${threadId}:${runId}` };
const warm = () => ({ data: projection({ usedTokens: 250_000, cacheExpiresInMs: 2 * MINUTE_MS }) });

describe("upstream's idle compaction takes an agent slot", () => {
  it.effect("a free slot: compacted as upstream does, asked as the thread's own start", () =>
    Effect.gen(function* () {
      const world = newWorld(1);
      yield* Deferred.await(yield* compaction(world, warm()));
      assert.lengthOf(world.sent, 1);
      assert.deepStrictEqual(world.asks[0], {
        callerThreadId: threadId,
        targetInstanceId: ProviderInstanceId.make("claudeAgent"),
        intoThreadId: threadId,
        keepsHeld: true,
        place: COMPACTION_PLACE,
      });
    }),
  );

  it.effect(
    "full, then a slot frees while the window is open: asked every 5 s in its place, sent once",
    () =>
      Effect.gen(function* () {
        const world = newWorld(0);
        const decided = yield* compaction(world, warm());
        yield* TestClock.adjust("30 seconds");
        assert.deepStrictEqual(world.sent, []);
        assert.isAtLeast(world.asks.length, 6);
        assert.isTrue(world.asks.every((asked) => asked.place?.key === COMPACTION_PLACE.key));
        assert.lengthOf(
          world.logs.filter((log) => log.includes("idle compaction waits for an agent slot")),
          1,
        );
        world.free = 1;
        yield* TestClock.adjust("5 seconds");
        yield* Deferred.await(decided);
        assert.lengthOf(world.sent, 1);
        yield* TestClock.adjust("1 hour");
        assert.lengthOf(world.sent, 1);
      }),
  );

  it.effect("full until the cache window closes: skipped, logged, never sent late", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const decided = yield* compaction(world, warm());
      yield* TestClock.adjust("2 minutes");
      yield* Deferred.await(decided);
      assert.deepStrictEqual(world.sent, []);
      assert.isTrue(
        world.logs.some((log) =>
          log.includes("idle compaction skipped: no agent slot while its cache window was open"),
        ),
      );
      assert.include(world.left, COMPACTION_PLACE.key);
      world.free = 5;
      yield* TestClock.adjust("1 hour");
      assert.deepStrictEqual(world.sent, []);
    }),
  );

  it.effect("full until under a minute of its window is left: skipped then, not sent cold", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const decided = yield* compaction(world, warm());
      // The window ends 2 minutes on; a slot that frees with less than a minute left is not used.
      yield* TestClock.adjust("55 seconds");
      assert.isFalse(world.left.includes(COMPACTION_PLACE.key));
      world.free = 5;
      yield* TestClock.adjust("5 seconds");
      yield* Deferred.await(decided);
      assert.deepStrictEqual(world.sent, []);
      assert.include(world.left, COMPACTION_PLACE.key);
    }),
  );

  it.effect("a check that fails: skipped, logged with its cause, not as a full gate", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const decided = yield* compaction(world, warm(), () => Effect.fail("records unreadable"));
      yield* TestClock.adjust("5 seconds");
      yield* Deferred.await(decided);
      assert.deepStrictEqual(world.sent, []);
      assert.isTrue(
        world.logs.some((log) =>
          log.includes("idle compaction skipped: whether it is still due could not be read"),
        ),
      );
      assert.isFalse(world.logs.some((log) => log.includes("no agent slot while its cache")));
      assert.include(world.left, COMPACTION_PLACE.key);
    }),
  );

  it.effect("full, and the thread is no longer idle: skipped at the next check, never sent", () =>
    Effect.gen(function* () {
      const world = newWorld(0);
      const thread = warm();
      const decided = yield* compaction(world, thread);
      // A question to the person is open now: no window, so no compaction.
      thread.data = {
        ...thread.data,
        runtimeRequests: [{ ...question(world), status: "pending" }] as never,
      };
      world.free = 5;
      yield* TestClock.adjust("5 seconds");
      yield* Deferred.await(decided);
      assert.deepStrictEqual(world.sent, []);
      assert.include(world.left, COMPACTION_PLACE.key);
    }),
  );
});

describe("the server's check that a waiting idle compaction is still due", () => {
  const commandFor = (run: string) =>
    ({
      type: "message.dispatch",
      commandId: `server:idle-compact:${threadId}:${run}`,
      threadId,
    }) as never as Extract<OrchestrationV2ServerCommand, { type: "message.dispatch" }>;

  it("open only for the run upstream's window is for, with a minute of it left", () => {
    const records = warm().data;
    // The window runs to 2 minutes on (NOW_MS + 2 min).
    assert.isTrue(WorkerWakeSlots.compactionWindowOpen(records, commandFor(runId), NOW_MS));
    // Another run's compaction (a newer run completed since): not this window's.
    assert.isFalse(WorkerWakeSlots.compactionWindowOpen(records, commandFor("run-older"), NOW_MS));
    const endMs = NOW_MS + 2 * MINUTE_MS;
    const minLeft = WorkerWakeSlots.COMPACTION_MIN_LEFT_MS;
    assert.isTrue(
      WorkerWakeSlots.compactionWindowOpen(records, commandFor(runId), endMs - minLeft - 1),
    );
    assert.isFalse(
      WorkerWakeSlots.compactionWindowOpen(records, commandFor(runId), endMs - minLeft),
    );
  });

  it.effect("reads the settings, the project, and upstream's records as its worker does", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const thread = warm();
      const queries: Array<unknown> = [];
      let idleCompaction: "beforeCacheExpires" | "off" = "beforeCacheExpires";
      let projectOff = false;
      const projections = {
        getThread: () => Effect.sync(() => thread.data.thread),
        getThreadRecords: (id: ThreadId, kinds: unknown, options: unknown) =>
          Effect.sync(() => {
            queries.push({ id, kinds, options });
            return thread.data;
          }),
      } as never as ProjectionStore.ProjectionStoreV2["Service"];
      const settings = {
        getSettings: Effect.sync(() => ({
          ...DEFAULT_SERVER_SETTINGS,
          idleCompaction,
          ...(projectOff
            ? {
                projectSettingsOverrides: {
                  [thread.data.thread.projectId]: {
                    providerInstances: {
                      [thread.data.thread.providerInstanceId]: { enabled: false },
                    },
                  },
                },
              }
            : {}),
        })),
      } as never as ServerSettings.ServerSettingsService["Service"];
      const due = WorkerWakeSlots.compactionDueOver(settings, projections);
      assert.isTrue(yield* due(commandFor(runId)));
      assert.isFalse(yield* due(commandFor("run-older")));
      // Upstream's query for its window: the same records, kinds and filters.
      const active = [thread.data.thread.activeProviderThreadId];
      assert.deepStrictEqual(queries[0], {
        id: threadId,
        kinds: [
          "runs",
          "messages",
          "providerThreads",
          "providerTurns",
          "runtimeRequests",
          "turnItems",
          "contextTransfers",
        ],
        options: {
          messageRunIds: [runId],
          turnItemStatuses: ["pending", "running", "waiting"],
          runStatuses: ["preparing", "starting", "running", "rolled_back"],
          includeLatestRun: true,
          providerThreadIds: active,
          providerTurnThreadIds: active,
          latestProviderTurnUsageOnly: true,
          runtimeRequestStatuses: ["pending"],
          contextTransferStatuses: ["pending"],
        },
      });
      // Idle compaction turned off since: no longer due, and nothing more read.
      idleCompaction = "off";
      const before = queries.length;
      assert.isFalse(yield* due(commandFor(runId)));
      assert.strictEqual(queries.length, before);
      idleCompaction = "beforeCacheExpires";
      projectOff = true;
      const enabled = yield* due(commandFor(runId));
      projectOff = false;
      assert.isFalse(enabled);
      assert.isTrue(yield* due(commandFor(runId)));
    }),
  );
});
