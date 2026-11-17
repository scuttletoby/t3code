/**
 * Fork (waits, PR 8): slot waits with the real slot gate. Who is woken when slots free, that a
 * woken thread's slot is held for it and used once, and the state question: settings changed while
 * waiting, a stopped or archived waiter, a restart with held slots in the store, counts that cannot
 * be read, and waits off (nothing read or written).
 */
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  type OrchestrationV2ThreadShellSnapshot,
  ProjectId,
  ProviderInstanceId,
  type ServerSettings as ServerSettingsValue,
  ThreadId,
} from "@t3tools/contracts";
import type { WaitForInput, WaitRecord } from "@t3tools/contracts/waitFor";
import type { DeepPartial } from "@t3tools/shared/Struct";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import { TestClock } from "effect/testing";

import * as OrchestratorV2 from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import { SlotCapacityHost } from "../slots/SlotObserver.ts";
import type { WaitDelivery, WaitReceiver } from "./WaitDelivery.ts";
import * as WaitService from "./WaitService.ts";
import { memoryFileSystem } from "./WaitService.testkit.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const PROJECT = ProjectId.make("project-a");
const PARENT = ThreadId.make("thread-parent");
const [T1, T2, T3, OUTSIDER] = ["thread-1", "thread-2", "thread-3", "thread-4"].map((id) =>
  ThreadId.make(id),
) as [ThreadId, ThreadId, ThreadId, ThreadId];
const MAIN = ProviderInstanceId.make("codex");
const FILE = `/waits/state/${WAIT_STORE_FILE}`;
const SLOT: WaitForInput = { kind: "slot" };

/** Slots and waits on, `max` agents on this machine, a slot held 60 s. */
const slotsOn = (max: number, extra: DeepPartial<ServerSettingsValue> = {}) =>
  ({
    ...extra,
    waitFor: { enabled: true, ...extra.waitFor },
    agentSlots: { enabled: true, maxPerMachine: max, reserveSeconds: 60, ...extra.agentSlots },
  }) satisfies DeepPartial<ServerSettingsValue>;

interface World {
  /** Agents the projection shows running (thread id per agent). */
  running: Array<string>;
  snapshotReads: number;
  snapshotFails: boolean;
  /** Runs once, right after the next count read (a slot frees while a wait is being made). */
  afterRead: (() => void) | null;
}

const newWorld = (): World => ({
  running: [],
  snapshotReads: 0,
  snapshotFails: false,
  afterRead: null,
});

const snapshotOf = (world: World): OrchestrationV2ThreadShellSnapshot => ({
  schemaVersion: 1,
  snapshotSequence: 1,
  threads: world.running.map(
    (id) =>
      ({
        id: ThreadId.make(id),
        providerInstanceId: MAIN,
        activeRunId: `run:${id}`,
      }) as unknown as OrchestrationV2ThreadShell,
  ),
  archivedThreads: [],
});

/** One server: settings, the gate (built fresh on each provide, as on a restart) and its inputs. */
const serverLayer = (world: World, settings: DeepPartial<ServerSettingsValue>) =>
  SlotGate.layer.pipe(
    Layer.provideMerge(
      Layer.mergeAll(
        ServerSettings.layerTest(settings),
        Layer.mock(ThreadManagementService.ThreadManagementService)({
          getShellSnapshot: () =>
            Effect.gen(function* () {
              world.snapshotReads += 1;
              if (world.snapshotFails) {
                return yield* new OrchestratorV2.OrchestratorProjectionError({ threadId: T1 });
              }
              const snapshot = snapshotOf(world);
              const after = world.afterRead;
              world.afterRead = null;
              after?.();
              return snapshot;
            }),
        }),
        Layer.mock(HostResources.HostResources)({
          read: Effect.succeed({
            sampledAt: 0,
            cpuUtilization: null,
            cpuCount: 4,
            availableMemoryBytes: 16_000 * 1024 * 1024,
            totalMemoryBytes: 32_000 * 1024 * 1024,
          }),
        }),
        // The default observes: it reads no real machine here (all files missing).
        Layer.succeed(SlotCapacityHost, {
          platform: "linux",
          readFile: () => Promise.resolve({ error: "missing" }),
          run: () => Promise.resolve(null),
          totalMemBytes: () => 0,
          freeMemBytes: () => 0,
          cpuCount: () => 0,
        }),
      ),
    ),
  );

/** Delivers at once unless the receiver is stopped or gone; records every message sent. */
const fakeDelivery = () => {
  const sent: Array<WaitService.WaitMessage> = [];
  const receivers = new Map<ThreadId, WaitReceiver>();
  const delivery: WaitDelivery = {
    receiver: (threadId) => Effect.sync(() => receivers.get(threadId) ?? { type: "ok" }),
    deliver: (message) =>
      Effect.sync(() => {
        const receiver = receivers.get(message.threadId);
        if (receiver !== undefined && receiver.type !== "ok") return receiver;
        if (!sent.some((earlier) => earlier.messageId === message.messageId)) sent.push(message);
        return { type: "delivered" } as const;
      }),
  };
  return { sent, receivers, delivery };
};

/** The store on the in-memory disk, with a count of its reads. */
const disk = () => {
  const memory = memoryFileSystem();
  const counts = { reads: 0, writes: 0 };
  const fs: FileSystem.FileSystem = {
    ...memory.fs,
    readFileString: (path, encoding) => {
      counts.reads += 1;
      return memory.fs.readFileString(path, encoding);
    },
    writeFileString: (path, contents, options) => {
      counts.writes += 1;
      return memory.fs.writeFileString(path, contents, options);
    },
  };
  return { fs, files: memory.files, counts };
};

const stored = (files: Map<string, string>) =>
  (JSON.parse(files.get(FILE) ?? '{"waits":[]}') as { waits: ReadonlyArray<WaitRecord> }).waits;

const startService = (fs: FileSystem.FileSystem, delivery: WaitDelivery) =>
  Effect.gen(function* () {
    const gate = yield* SlotGate.SlotGate;
    const service = yield* WaitService.make({
      filePath: FILE,
      delivery,
      fileChildJudge: null,
      slots: gate,
    }).pipe(Effect.provideService(FileSystem.FileSystem, fs), Effect.provide(Path.layer));
    const register = (threadId: ThreadId, input: WaitForInput = SLOT) =>
      service.register({ threadId, projectId: PROJECT, parentThreadId: PARENT, input });
    const delegate = (callerThreadId: ThreadId) =>
      gate
        .acquire({ callerThreadId, targetInstanceId: MAIN })
        .pipe(Effect.map((outcome) => outcome.type));
    return { gate, service, register, delegate };
  });

const waitingId = (result: { readonly state: string; readonly waitId?: string }) => {
  assert.strictEqual(result.state, "waiting");
  return result.waitId!;
};

describe("WaitService slot waits", () => {
  it.effect("T17: three waits, two slots free: the two oldest are woken and held for", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1", "busy-2"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, send.delivery);
        const woken = () => send.sent.map((message) => message.threadId).sort();
        const ids: Array<string> = [];
        for (const thread of [T1, T2, T3]) {
          ids.push(waitingId(yield* register(thread)));
          yield* TestClock.adjust(1_000);
        }
        // Both agents end: one sweep wakes the two oldest, the third keeps waiting.
        world.running = [];
        yield* service.runDue();
        // Woken together; their messages may go out in either order.
        assert.deepStrictEqual(woken(), [T1, T2]);
        assert.include(send.sent[0]!.text, "free");
        assert.include(send.sent[0]!.text, "delegate_task");
        const after = stored(store.files);
        assert.deepStrictEqual(
          after.map((wait) => [wait.threadId, wait.state, wait.reservedUntil !== undefined]),
          [
            [T1, "delivered", true],
            [T2, "delivered", true],
            [T3, "waiting", false],
          ],
        );
        // Another thread finds no slot; the woken ones each start exactly one agent.
        assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
        assert.strictEqual(yield* delegate(T1), "allow");
        assert.strictEqual(yield* delegate(T1), "refuse");
        assert.isUndefined(stored(store.files).find((wait) => wait.id === ids[0])?.reservedUntil);
        // T2 never delegates: its slot ends after 60 s and goes to T3.
        yield* TestClock.adjust(59_000);
        yield* service.runDue();
        assert.strictEqual(send.sent.length, 2);
        yield* TestClock.adjust(2_000);
        yield* service.runDue();
        assert.deepStrictEqual(woken(), [T1, T2, T3]);
      }).pipe(Effect.provide(serverLayer(world, slotsOn(2))));
    }),
  );

  it.effect("two waits for one free slot: only the oldest is woken; no double start", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T2));
        yield* TestClock.adjust(1_000);
        waitingId(yield* register(T1));
        world.running = [];
        // Two sweeps: the second must not wake T1 on the slot held for T2.
        yield* service.runDue();
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T2],
        );
        assert.strictEqual(yield* delegate(T1), "refuse");
        assert.strictEqual(yield* delegate(T2), "allow");
        assert.strictEqual(yield* delegate(T2), "refuse");
        // Its agent counts as running: the slot is still taken.
        yield* service.runDue();
        assert.strictEqual(send.sent.length, 1);
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("a free slot at registration is met at once, held, and nothing is stored", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate } = yield* startService(store.fs, send.delivery);
        const result = yield* register(T1);
        assert.strictEqual(result.state, "met");
        if (result.state === "met") {
          assert.strictEqual(result.outcome, "free");
          assert.include(result.detail, "held for this thread for 60 s");
        }
        assert.deepStrictEqual(stored(store.files), []);
        assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
        assert.strictEqual(yield* delegate(T1), "allow");
        // A newer slot wait behind it waits (the slot is now running T1's agent).
        waitingId(yield* register(T2));
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("a slot that frees while the wait is being made: stored, then woken next sweep", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      world.afterRead = () => {
        world.running = [];
      };
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        const id = waitingId(yield* register(T1));
        assert.deepStrictEqual(
          stored(store.files).map((wait) => [wait.id, wait.state]),
          [[id, "waiting"]],
        );
        // Due at once, not after a sweep interval.
        const { nextDueAt } = yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
        assert.isAbove(nextDueAt, NOW);
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("a newer wait never jumps the queue, even when it sees a free slot", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        world.running = [];
        yield* TestClock.adjust(1_000);
        // Free now, but T1 is older: T2 waits for the sweep, which wakes T1.
        waitingId(yield* register(T2));
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("slots turned off while waiting: woken with 'slots are off'; limit raised: woken", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1", "busy-2"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        const settings = yield* ServerSettings.ServerSettingsService;
        waitingId(yield* register(T1));
        yield* TestClock.adjust(1_000);
        waitingId(yield* register(T2));
        yield* service.runDue();
        assert.deepStrictEqual(send.sent, []);
        yield* settings.updateSettings({ agentSlots: { maxPerMachine: 3 } });
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
        yield* settings.updateSettings({ agentSlots: { enabled: false } });
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1, T2],
        );
        assert.include(send.sent[1]!.text, "slots are off");
        // Off: no slot is held for it.
        assert.isUndefined(stored(store.files).find((wait) => wait.threadId === T2)?.reservedUntil);
      }).pipe(Effect.provide(serverLayer(world, slotsOn(2))));
    }),
  );

  it.effect("an archived waiter: its wait goes to the parent and never holds a slot", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        send.receivers.set(T1, { type: "gone", parentThreadId: PARENT });
        yield* service.receiverChanged(T1);
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [PARENT],
        );
        world.running = [];
        yield* service.runDue();
        assert.strictEqual(send.sent.length, 1);
        assert.isUndefined(stored(store.files)[0]?.reservedUntil);
        assert.strictEqual(yield* delegate(OUTSIDER), "allow");
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("a stopped waiter: granted, its message held, its slot freed after 60 s", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        send.receivers.set(T1, { type: "stopped", parentThreadId: PARENT });
        world.running = [];
        yield* service.runDue();
        assert.deepStrictEqual(send.sent, []);
        assert.strictEqual(stored(store.files)[0]?.state, "delivering");
        assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
        yield* TestClock.adjust(61_000);
        yield* service.runDue();
        const allowed = yield* SlotGate.SlotGate.pipe(
          Effect.flatMap((gate) =>
            gate.acquire({ callerThreadId: OUTSIDER, targetInstanceId: MAIN }),
          ),
        );
        assert.strictEqual(allowed.type, "allow");
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("server restart: open waits are woken again, and stored held slots still count", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1", "busy-2"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        yield* TestClock.adjust(1_000);
        waitingId(yield* register(T2));
        world.running = ["busy-1"];
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
      }).pipe(Effect.provide(serverLayer(world, slotsOn(2))));
      // A new server: the gate's memory is gone, the store keeps T1's held slot and T2's wait.
      yield* Effect.gen(function* () {
        const { delegate, service } = yield* startService(store.fs, send.delivery);
        yield* service.runDue();
        assert.strictEqual(send.sent.length, 1);
        assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
        assert.strictEqual(yield* delegate(T1), "allow");
        assert.isUndefined(stored(store.files).find((wait) => wait.threadId === T1)?.reservedUntil);
        // T1's agent is now starting; busy-1 ends and T2 is woken.
        world.running = [];
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1, T2],
        );
      }).pipe(Effect.provide(serverLayer(world, slotsOn(2))));
    }),
  );

  it.effect("counts that cannot be read: an error, then check_error; never 'free'", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.snapshotFails = true;
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        yield* service.runDue();
        assert.deepStrictEqual(send.sent, []);
        assert.include(stored(store.files)[0]?.lastError ?? "", "cannot be counted");
        // The read works again: the error clears and the free slot is granted.
        world.snapshotFails = false;
        yield* TestClock.adjust(15_000);
        yield* service.runDue();
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
        // Unreadable for longer than the grace: the wait ends as an error.
        world.snapshotFails = true;
        waitingId(yield* register(T2));
        for (let step = 0; step < 6; step += 1) {
          yield* TestClock.adjust(15_000);
          yield* service.runDue();
        }
        const ended = stored(store.files).find((wait) => wait.threadId === T2);
        assert.strictEqual(ended?.result?.outcome, "check_error");
        assert.isUndefined(ended?.reservedUntil);
      }).pipe(
        Effect.provide(serverLayer(world, slotsOn(1, { waitFor: { errorGraceSeconds: 60 } }))),
      );
    }),
  );

  it.effect("waits off (the default): no store read or written, its held slots do not count", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      const store = disk();
      store.files.set(
        FILE,
        JSON.stringify({
          version: 1,
          waits: [
            {
              id: "w_aaaaaaaaaa",
              threadId: T1,
              projectId: PROJECT,
              key: "0123456789abcdef", // gitleaks:allow (a fake test value, not a key)
              createdAt: "2026-11-14T11:00:00.000Z",
              deadlineAt: "2026-11-14T13:00:00.000Z",
              state: "delivered",
              endedAt: "2026-11-14T11:59:30.000Z",
              attempt: 0,
              kind: "slot",
              spec: {},
              result: { outcome: "free", detail: "", at: "2026-11-14T11:59:30.000Z" },
              reservedUntil: "2026-11-14T12:00:30.000Z",
            },
          ],
        }),
      );
      const before = store.files.get(FILE);
      const send = fakeDelivery();
      const settings = { agentSlots: { enabled: true, maxPerMachine: 1 } };
      yield* Effect.gen(function* () {
        const { delegate, service } = yield* startService(store.fs, send.delivery);
        assert.strictEqual(yield* delegate(OUTSIDER), "allow");
        yield* service.runDue();
        assert.deepStrictEqual(store.counts, { reads: 0, writes: 0 });
        assert.strictEqual(store.files.get(FILE), before);
      }).pipe(Effect.provide(serverLayer(world, settings)));
      // Slots off too: nothing is counted at all (fixed mode; the default only observes, off the
      // decision path, see SlotObserver.test.ts).
      for (const off of [{ agentSlots: { mode: "fixed" } }, {}]) {
        world.snapshotReads = 0;
        yield* Effect.gen(function* () {
          const { delegate, service } = yield* startService(store.fs, send.delivery);
          assert.strictEqual(yield* delegate(OUTSIDER), "allow");
          yield* service.runDue();
          if ("agentSlots" in off) assert.strictEqual(world.snapshotReads, 0);
          assert.deepStrictEqual(store.counts, { reads: 0, writes: 0 });
        }).pipe(Effect.provide(serverLayer(world, off)));
      }
      // Waits on: the same stored slot counts, so another thread is refused.
      yield* Effect.gen(function* () {
        const { delegate } = yield* startService(store.fs, send.delivery);
        assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
      }).pipe(Effect.provide(serverLayer(world, { ...settings, waitFor: { enabled: true } })));
    }),
  );

  it.effect("wait_list shows slot usage only while slots are on", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, service } = yield* startService(store.fs, send.delivery);
        waitingId(yield* register(T1));
        const listed = yield* service.list({ threadId: T1, projectId: PROJECT });
        assert.deepStrictEqual(listed.slots?.machine, { used: 1, max: 1 });
        const settings = yield* ServerSettings.ServerSettingsService;
        yield* settings.updateSettings({ agentSlots: { enabled: false } });
        const reads = world.snapshotReads;
        const off = yield* service.list({ threadId: T1, projectId: PROJECT });
        assert.isUndefined(off.slots);
        assert.strictEqual(world.snapshotReads, reads);
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect(
    "one thread holds at most one slot: repeated met slot waits do not hoard every free slot",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const world = newWorld();
        const store = disk();
        const send = fakeDelivery();
        yield* Effect.gen(function* () {
          const { register, delegate, service } = yield* startService(store.fs, send.delivery);
          // Nothing runs; three slots free. T1 asks three times and starts nothing.
          for (let i = 0; i < 3; i += 1) {
            const result = yield* register(T1);
            assert.strictEqual(result.state, "met");
          }
          // Nothing is stored (so maxPerThread never applied), and one slot is held.
          assert.deepStrictEqual(stored(store.files), []);
          const listed = yield* service.list({ threadId: T1, projectId: PROJECT });
          assert.deepStrictEqual(listed.slots?.machine, { used: 1, max: 3 });
          // Another thread, with no agent running anywhere, still finds a slot.
          assert.strictEqual(yield* delegate(OUTSIDER), "allow");
          assert.strictEqual(yield* delegate(OUTSIDER), "allow");
          assert.strictEqual(yield* delegate(OUTSIDER), "refuse");
          // T1 still has its one slot.
          assert.strictEqual(yield* delegate(T1), "allow");
        }).pipe(Effect.provide(serverLayer(world, slotsOn(3))));
      }),
  );

  it.effect("a woken thread that asks again keeps one slot: the stored one is replaced", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1", "busy-2", "busy-3"];
      const store = disk();
      const send = fakeDelivery();
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, send.delivery);
        const id = waitingId(yield* register(T1));
        world.running = ["busy-1"];
        yield* service.runDue();
        assert.isDefined(stored(store.files).find((wait) => wait.id === id)?.reservedUntil);
        // Asked again before using it: met, and the slot it held moves to this answer.
        assert.strictEqual((yield* register(T1)).state, "met");
        assert.isUndefined(stored(store.files).find((wait) => wait.id === id)?.reservedUntil);
        const listed = yield* service.list({ threadId: T1, projectId: PROJECT });
        assert.deepStrictEqual(listed.slots?.machine, { used: 2, max: 3 });
        assert.strictEqual(yield* delegate(OUTSIDER), "allow");
        assert.strictEqual(yield* delegate(T1), "allow");
      }).pipe(Effect.provide(serverLayer(world, slotsOn(3))));
    }),
  );

  it.effect(
    "reserveSeconds 0: one free slot wakes one waiter per sweep, not all of them (R17)",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const world = newWorld();
        world.running = ["busy-1"];
        const store = disk();
        const send = fakeDelivery();
        yield* Effect.gen(function* () {
          const { register, service } = yield* startService(store.fs, send.delivery);
          for (const thread of [T1, T2, T3]) {
            waitingId(yield* register(thread));
            yield* TestClock.adjust(1_000);
          }
          world.running = [];
          yield* service.runDue();
          assert.deepStrictEqual(
            send.sent.map((message) => message.threadId),
            [T1],
          );
        }).pipe(
          Effect.provide(serverLayer(world, slotsOn(1, { agentSlots: { reserveSeconds: 0 } }))),
        );
      }),
  );

  it.effect(
    "a stored held slot far ahead (clock ahead, hand-edited file) ends reserveSeconds after it is read, and is pruned",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const world = newWorld();
        const store = disk();
        const heldWait = (
          id: string,
          threadId: ThreadId,
          endedAt: string,
          reservedUntil: string,
        ) => ({
          id,
          threadId,
          projectId: PROJECT,
          key: id === "w_aaaaaaaaaa" ? "000000000000000a" : "000000000000000b", // gitleaks:allow (fake test values)
          createdAt: "2026-11-14T11:00:00.000Z",
          deadlineAt: "2026-11-14T13:00:00.000Z",
          state: "delivered",
          endedAt,
          attempt: 0,
          kind: "slot",
          spec: {},
          result: { outcome: "free", detail: "", at: endedAt },
          reservedUntil,
        });
        store.files.set(
          FILE,
          JSON.stringify({
            version: 1,
            waits: [
              // Delivered 10 s ago, but held until the year 9999.
              heldWait("w_aaaaaaaaaa", T1, "2026-11-14T11:59:50.000Z", "9999-01-01T00:00:00.000Z"),
              // Written while the clock was a year ahead.
              heldWait("w_bbbbbbbbbb", T2, "2027-11-14T12:00:00.000Z", "2027-11-14T12:01:00.000Z"),
            ],
          }),
        );
        const send = fakeDelivery();
        yield* Effect.gen(function* () {
          const { service } = yield* startService(store.fs, send.delivery);
          const used = Effect.map(
            service.list({ threadId: OUTSIDER, projectId: PROJECT }),
            (listed) => listed.slots?.machine.used,
          );
          assert.strictEqual(yield* used, 2);
          // 60 s after its delivery the first one ends; 60 s after the read the second.
          yield* TestClock.adjust(51_000);
          assert.strictEqual(yield* used, 1);
          yield* TestClock.adjust(10_000);
          assert.strictEqual(yield* used, 0);
          // Ended waits are pruned after keepEndedHours; the held slot no longer keeps them.
          yield* TestClock.adjust(3_600_000);
          yield* service.runDue();
          assert.deepStrictEqual(
            stored(store.files).map((wait) => wait.id),
            ["w_bbbbbbbbbb"],
          );
          assert.isAtMost(
            Date.parse(stored(store.files)[0]!.reservedUntil ?? "1970-01-01T00:00:00.000Z"),
            NOW + 60_000,
          );
        }).pipe(Effect.provide(serverLayer(world, slotsOn(2, { waitFor: { keepEndedHours: 1 } }))));
      }),
  );

  it.effect("a granted wait handed over to the parent frees its slot at once", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1"];
      const store = disk();
      const send = fakeDelivery();
      // Archived after the slot is granted: it looks alive until its wake is sent.
      let archived = false;
      const delivery: WaitDelivery = {
        receiver: (threadId) =>
          threadId === T1 && archived
            ? Effect.succeed({ type: "gone", parentThreadId: PARENT })
            : send.delivery.receiver(threadId),
        deliver: (message) => {
          if (message.threadId !== T1) return send.delivery.deliver(message);
          archived = true;
          return Effect.succeed({ type: "gone", parentThreadId: PARENT });
        },
      };
      yield* Effect.gen(function* () {
        const { register, delegate, service } = yield* startService(store.fs, delivery);
        waitingId(yield* register(T1));
        world.running = [];
        yield* service.runDue();
        yield* service.runDue();
        assert.isTrue(archived);
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [PARENT],
        );
        assert.isUndefined(stored(store.files)[0]?.reservedUntil);
        assert.strictEqual(yield* delegate(OUTSIDER), "allow");
      }).pipe(Effect.provide(serverLayer(world, slotsOn(1))));
    }),
  );

  it.effect("a slot used before its wake is delivered is not held again by the delivery", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const world = newWorld();
      world.running = ["busy-1", "busy-2", "busy-3"];
      const store = disk();
      const send = fakeDelivery();
      let gate: SlotGate.SlotGate["Service"] | null = null;
      const allowed: Array<string> = [];
      const delivery: WaitDelivery = {
        receiver: send.delivery.receiver,
        // The thread delegates (on its held slot) while its wake is on the way.
        deliver: (message) =>
          gate!.acquire({ callerThreadId: message.threadId, targetInstanceId: MAIN }).pipe(
            Effect.tap((outcome) => Effect.sync(() => allowed.push(outcome.type))),
            Effect.andThen(send.delivery.deliver(message)),
          ),
      };
      yield* Effect.gen(function* () {
        const started = yield* startService(store.fs, delivery);
        gate = started.gate;
        waitingId(yield* started.register(T1));
        world.running = ["busy-1"];
        yield* started.service.runDue();
        assert.deepStrictEqual(allowed, ["allow"]);
        assert.deepStrictEqual(
          send.sent.map((message) => message.threadId),
          [T1],
        );
        assert.strictEqual(stored(store.files)[0]?.state, "delivered");
        assert.isUndefined(stored(store.files)[0]?.reservedUntil);
        // busy-1 and T1's starting agent (2 of 3): the used slot does not count again.
        assert.strictEqual(yield* started.delegate(OUTSIDER), "allow");
      }).pipe(Effect.provide(serverLayer(world, slotsOn(3))));
    }),
  );
});
