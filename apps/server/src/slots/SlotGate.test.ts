/**
 * Fork: the agent slot gate as a service: what it reads, the `starting` entries between the
 * dispatch and the child's first visible run, and the state question (runs again, stops half-way,
 * empty or old state, a restart). The rules themselves are in `shared/agentSlots.test.ts`.
 */
import { assert, describe, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  type OrchestrationV2ThreadShellSnapshot,
  ProviderInstanceId,
  type ServerSettings as ServerSettingsValue,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import type { AgentSlotsSettings } from "@t3tools/contracts/waitFor";
import type { DeepPartial } from "@t3tools/shared/Struct";
import { DISPATCH_SAFETY_MS, type SlotReservation } from "@t3tools/shared/agentSlots";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import { TestClock } from "effect/testing";

import * as OrchestratorV2 from "../orchestration-v2/Orchestrator.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as SlotGate from "./SlotGate.ts";

const MAIN = ProviderInstanceId.make("codex");
const SECOND = ProviderInstanceId.make("claude-second");
const MB = 1024 * 1024;
const caller = ThreadId.make("thread:slot-caller");

interface World {
  /** Threads the projection shows with an active run (id, instance, blocked). */
  threads: Array<{ id: string; instanceId: string; blocked?: boolean; archived?: boolean }>;
  snapshotReads: number;
  memoryReads: number;
  snapshotFails: boolean;
  /** The count read never answers (a hung database read). */
  snapshotHangs: boolean;
  /** The count read dies (a bug in the read, not a failure). */
  snapshotDies?: boolean;
  freeMemoryBytes: number;
}

const newWorld = (): World => ({
  threads: [],
  snapshotReads: 0,
  memoryReads: 0,
  snapshotFails: false,
  snapshotHangs: false,
  freeMemoryBytes: 16_000 * MB,
});

const snapshotOf = (world: World): OrchestrationV2ThreadShellSnapshot => {
  const shells = world.threads.map(
    (thread) =>
      ({
        id: ThreadId.make(thread.id),
        providerInstanceId: ProviderInstanceId.make(thread.instanceId),
        activeRunId: `run:${thread.id}`,
        ...(thread.blocked === undefined ? {} : { blockedOnThreadWait: thread.blocked }),
      }) as unknown as OrchestrationV2ThreadShell,
  );
  return {
    schemaVersion: 1,
    snapshotSequence: 1,
    threads: shells.filter((_, index) => world.threads[index]?.archived !== true),
    archivedThreads: shells.filter((_, index) => world.threads[index]?.archived === true),
  };
};

const layerFor = (world: World, settings: DeepPartial<ServerSettingsValue> | "unreadable") =>
  SlotGate.layer.pipe(
    // Merged out too, so a test can change the settings while the gate runs.
    Layer.provideMerge(
      Layer.mergeAll(
        settings === "unreadable"
          ? Layer.mock(ServerSettings.ServerSettingsService)({
              getSettings: Effect.fail(
                new ServerSettingsError({ settingsPath: "settings.json", operation: "read-file" }),
              ),
            })
          : ServerSettings.layerTest(settings),
        Layer.mock(ThreadManagementService.ThreadManagementService)({
          getShellSnapshot: (options) =>
            Effect.gen(function* () {
              // Settled threads too: settling a thread does not stop its run.
              assert.deepStrictEqual(options, { activeRunOnly: true });
              world.snapshotReads += 1;
              if (world.snapshotHangs) return yield* Effect.never;
              if (world.snapshotDies) return yield* Effect.die(new Error("corrupt row"));
              // Lets concurrent callers interleave here, the slow part of a real acquire.
              yield* Effect.yieldNow;
              if (world.snapshotFails) {
                return yield* new OrchestratorV2.OrchestratorProjectionError({ threadId: caller });
              }
              return snapshotOf(world);
            }),
        }),
        Layer.mock(HostResources.HostResources)({
          read: Effect.gen(function* () {
            yield* Effect.yieldNow;
            world.memoryReads += 1;
            return {
              sampledAt: 0,
              cpuUtilization: null,
              cpuCount: 4,
              availableMemoryBytes: world.freeMemoryBytes,
              totalMemoryBytes: 32_000 * MB,
            };
          }),
        }),
      ),
    ),
  );

const on = (patch: Partial<AgentSlotsSettings>) => ({ agentSlots: { enabled: true, ...patch } });

const acquire = (targetInstanceId: ProviderInstanceId = MAIN) =>
  SlotGate.SlotGate.pipe(
    Effect.flatMap((gate) => gate.acquire({ callerThreadId: caller, targetInstanceId })),
  );

const run = <A, E>(
  world: World,
  settings: DeepPartial<ServerSettingsValue> | "unreadable",
  body: Effect.Effect<A, E, SlotGate.SlotGate | ServerSettings.ServerSettingsService>,
) => body.pipe(Effect.provide(layerFor(world, settings)));

describe("SlotGate", () => {
  it.effect(
    "off or with no limit: allows and reads neither projection nor memory (fixed mode; observe in SlotObserver.test.ts)",
    () =>
      Effect.gen(function* () {
        for (const settings of [
          { agentSlots: { mode: "fixed" } },
          { agentSlots: { mode: "fixed", maxPerMachine: 1, minFreeMemoryMb: 100_000 } },
          on({}),
          "unreadable" as const,
        ]) {
          const world = newWorld();
          world.threads = [{ id: "a", instanceId: MAIN }];
          const outcome = yield* run(world, settings, acquire());
          assert.deepStrictEqual(outcome, { type: "allow", token: null });
          assert.strictEqual(world.snapshotReads, 0);
          assert.strictEqual(world.memoryReads, 0);
        }
      }),
  );

  it.effect("only a memory floor: reads memory, not the projection", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.freeMemoryBytes = 500 * MB;
      const outcome = yield* run(world, on({ minFreeMemoryMb: 1000 }), acquire());
      assert.strictEqual(outcome.type, "refuse");
      if (outcome.type === "refuse")
        assert.include(outcome.message, "only 500 MB of memory is free");
      assert.strictEqual(world.snapshotReads, 0);
      assert.strictEqual(world.memoryReads, 1);
    }),
  );

  it.effect("an account limit on another account reads nothing for this target", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const outcome = yield* run(world, on({ maxPerAccount: { [SECOND]: 1 } }), acquire(MAIN));
      assert.strictEqual(outcome.type, "allow");
      assert.strictEqual(world.snapshotReads, 0);
    }),
  );

  it.effect("counts running agents, archived ones too, less blocked parents (T14)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.threads = [
        { id: "a", instanceId: MAIN },
        { id: "b", instanceId: SECOND, archived: true },
        { id: "parent", instanceId: MAIN, blocked: true },
        { id: caller, instanceId: MAIN },
      ];
      const outcome = yield* run(world, on({ maxPerMachine: 2 }), acquire());
      assert.strictEqual(outcome.type, "refuse");
      if (outcome.type === "refuse") {
        assert.strictEqual(
          outcome.message,
          "No free agent slot: 2 agents are running on this machine, and the limit is 2. Try again later. Or do the work yourself.",
        );
      }
    }),
  );

  it.effect("concurrent callers with 2 free slots: exactly 2 are allowed (T15)", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.threads = [{ id: "a", instanceId: MAIN }];
      const outcomes = yield* run(
        world,
        // The memory floor puts a real wait (the host read) between counting and adding the entry.
        on({ maxPerMachine: 3, minFreeMemoryMb: 100 }),
        Effect.all(
          Array.from({ length: 5 }, () => acquire()),
          { concurrency: "unbounded" },
        ),
      );
      assert.strictEqual(outcomes.filter((outcome) => outcome.type === "allow").length, 2);
      const tokens = outcomes.flatMap((outcome) =>
        outcome.type === "allow" && outcome.token !== null ? [outcome.token] : [],
      );
      assert.strictEqual(new Set(tokens).size, 2);
    }),
  );

  it.effect("an account limit counts starting entries of that account only", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const outcomes = yield* run(
        world,
        on({ maxPerAccount: { [MAIN]: 1 } }),
        Effect.all([acquire(SECOND), acquire(MAIN), acquire(MAIN)]),
      );
      assert.deepStrictEqual(
        outcomes.map((outcome) => outcome.type),
        ["allow", "allow", "refuse"],
      );
    }),
  );

  it.effect("runs again: a started child counts once, as running, not also as starting", () =>
    Effect.gen(function* () {
      const world = newWorld();
      yield* run(
        world,
        on({ maxPerMachine: 2 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire();
          assert.strictEqual(first.type, "allow");
          if (first.type !== "allow") return;
          yield* gate.started(first.token, ThreadId.make("child-1"));
          // The same call again before the child is visible: it still counts (1 of 2).
          yield* gate.started(first.token, ThreadId.make("child-1"));
          world.threads = [{ id: "child-1", instanceId: MAIN }];
          // Visible now: counted once, so a second child fits.
          const second = yield* acquire();
          assert.strictEqual(second.type, "allow");
          const third = yield* acquire();
          assert.strictEqual(third.type, "refuse");
        }),
      );
    }),
  );

  it.effect(
    "stops half-way: a released slot is free at once; a lost release ends after the safety time",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          on({ maxPerMachine: 1, startGraceSeconds: 30 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            const first = yield* acquire();
            assert.strictEqual(first.type, "allow");
            if (first.type !== "allow") return;
            assert.strictEqual((yield* acquire()).type, "refuse");
            // The dispatch failed: the caller releases, and releasing twice is harmless.
            yield* gate.release(first.token);
            yield* gate.release(first.token);
            const second = yield* acquire();
            assert.strictEqual(second.type, "allow");
            // Never released (a bug lost it): it ends after the safety time, not the grace.
            yield* TestClock.adjust(DISPATCH_SAFETY_MS - 1_000);
            assert.strictEqual((yield* acquire()).type, "refuse");
            yield* TestClock.adjust("1 second");
            assert.strictEqual((yield* acquire()).type, "allow");
          }),
        );
      }),
  );

  it.effect("a dispatch slower than the start grace keeps its slot until it ends", () =>
    Effect.gen(function* () {
      const world = newWorld();
      yield* run(
        world,
        on({ maxPerMachine: 1, startGraceSeconds: 30 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire();
          if (first.type !== "allow") return assert.fail("expected allow");
          yield* TestClock.adjust("31 seconds");
          assert.strictEqual((yield* acquire()).type, "refuse");
          // The dispatch failed after all: free at once.
          yield* gate.release(first.token);
          assert.strictEqual((yield* acquire()).type, "allow");
        }),
      );
    }),
  );

  it.effect("start grace 0: concurrent callers still get only the free slots", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.threads = [{ id: "a", instanceId: MAIN }];
      const outcomes = yield* run(
        world,
        on({ maxPerMachine: 3, startGraceSeconds: 0 }),
        Effect.all(
          Array.from({ length: 5 }, () => acquire()),
          { concurrency: "unbounded" },
        ),
      );
      assert.strictEqual(outcomes.filter((outcome) => outcome.type === "allow").length, 2);
    }),
  );

  it.effect(
    "a started child that never shows a run stops counting a grace after its dispatch",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          on({ maxPerMachine: 1, startGraceSeconds: 10 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            const first = yield* acquire();
            if (first.type !== "allow") return assert.fail("expected allow");
            yield* gate.started(first.token, ThreadId.make("child-queued"));
            // The dispatch (and the child's run preparation) took a minute.
            yield* TestClock.adjust("60 seconds");
            assert.strictEqual((yield* acquire()).type, "refuse");
            yield* gate.release(first.token);
            // A second release (a finalizer run again) does not restart the grace.
            yield* TestClock.adjust("5 seconds");
            yield* gate.release(first.token);
            assert.strictEqual((yield* acquire()).type, "refuse");
            yield* TestClock.adjust("5 seconds");
            assert.strictEqual((yield* acquire()).type, "allow");
          }),
        );
      }),
  );

  it.effect("a started child that delegates itself is not counted through its entry", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.threads = [{ id: "x", instanceId: MAIN }];
      yield* run(
        world,
        on({ maxPerMachine: 2 }),
        Effect.gen(function* () {
          const gate = yield* SlotGate.SlotGate;
          const first = yield* acquire();
          if (first.type !== "allow") return assert.fail("expected allow");
          const child = ThreadId.make("thread:child-c");
          yield* gate.started(first.token, child);
          yield* gate.release(first.token);
          world.threads.push({ id: child, instanceId: MAIN });
          yield* TestClock.adjust("10 seconds");
          // Running, less the caller: only x (1 of 2).
          const second = yield* gate.acquire({ callerThreadId: child, targetInstanceId: MAIN });
          assert.strictEqual(second.type, "allow");
        }),
      );
    }),
  );

  it.effect(
    "a child seen with a run never counts through its entry again (blocked, or its run ended)",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          on({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            const first = yield* acquire();
            if (first.type !== "allow") return assert.fail("expected allow");
            const child = ThreadId.make("thread:child-b");
            yield* gate.started(first.token, child);
            yield* gate.release(first.token);
            // Its run shows, blocked in a thread wait: not counted, by its run nor its entry.
            world.threads = [{ id: child, instanceId: MAIN, blocked: true }];
            const second = yield* acquire();
            if (second.type !== "allow") return assert.fail("expected allow");
            yield* gate.release(second.token);
            // Its run ended inside the grace: the entry is gone for good.
            world.threads = [];
            assert.strictEqual((yield* acquire()).type, "allow");
          }),
        );
      }),
  );

  it.effect(
    "a hung count read lets the call through after the timeout, even inside an uninterruptible acquire",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.snapshotHangs = true;
        world.threads = [{ id: "a", instanceId: MAIN }];
        const outcome = yield* run(
          world,
          on({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            const fiber = yield* Effect.forkChild(Effect.uninterruptible(acquire()));
            yield* TestClock.adjust(SlotGate.COUNT_READ_TIMEOUT);
            return yield* Fiber.join(fiber);
          }),
        );
        assert.strictEqual(outcome.type, "allow");
      }),
  );

  it.effect(
    "a projection that cannot be read lets the delegation through; memory still applies",
    () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.snapshotFails = true;
        world.threads = [{ id: "a", instanceId: MAIN }];
        const allowed = yield* run(world, on({ maxPerMachine: 1 }), acquire());
        assert.strictEqual(allowed.type, "allow");
        world.freeMemoryBytes = 10 * MB;
        const refused = yield* run(
          world,
          on({ maxPerMachine: 1, minFreeMemoryMb: 100 }),
          acquire(),
        );
        assert.strictEqual(refused.type, "refuse");
        if (refused.type === "refuse") assert.include(refused.message, "memory");
      }),
  );

  it.effect("a count read that dies (a bug, not a failure) lets the call through", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.snapshotDies = true;
      world.threads = [{ id: "a", instanceId: MAIN }];
      const outcome = yield* run(world, on({ maxPerMachine: 1 }), acquire());
      assert.strictEqual(outcome.type, "allow");
    }),
  );

  it.effect("settings changed while running: off to on takes effect at the next call", () =>
    Effect.gen(function* () {
      const world = newWorld();
      world.threads = [{ id: "a", instanceId: MAIN }];
      yield* run(
        world,
        {},
        Effect.gen(function* () {
          assert.strictEqual((yield* acquire()).type, "allow");
          const settings = yield* ServerSettings.ServerSettingsService;
          yield* settings.updateSettings({ agentSlots: { enabled: true, maxPerMachine: 1 } });
          assert.strictEqual((yield* acquire()).type, "refuse");
          yield* settings.updateSettings({ agentSlots: { enabled: false } });
          assert.strictEqual((yield* acquire()).type, "allow");
        }),
      );
    }),
  );

  it.effect("server restart: nothing is kept; the new gate counts from the projection alone", () =>
    Effect.gen(function* () {
      const world = newWorld();
      const settings = on({ maxPerMachine: 2 });
      const before = yield* run(world, settings, Effect.all([acquire(), acquire(), acquire()]));
      assert.deepStrictEqual(
        before.map((outcome) => outcome.type),
        ["allow", "allow", "refuse"],
      );
      // After the restart both children run: still full.
      world.threads = [
        { id: "child-1", instanceId: MAIN },
        { id: "child-2", instanceId: MAIN },
      ];
      assert.strictEqual((yield* run(world, settings, acquire())).type, "refuse");
      // A child whose run is not active yet (queued at the restart) is not counted: accepted.
      world.threads = [{ id: "child-1", instanceId: MAIN }];
      assert.strictEqual((yield* run(world, settings, acquire())).type, "allow");
    }),
  );

  describe("slots held for slot waits (4.6)", () => {
    const holder = ThreadId.make("thread:slot-holder");
    const other = ThreadId.make("thread:slot-other");
    const waitsOn = (patch: Partial<AgentSlotsSettings>) => ({
      ...on(patch),
      waitFor: { enabled: true },
    });
    const acquireAs = (callerThreadId: ThreadId, targetInstanceId = MAIN) =>
      SlotGate.SlotGate.pipe(
        Effect.flatMap((gate) => gate.acquire({ callerThreadId, targetInstanceId })),
      );
    const grant = (...requests: ReadonlyArray<SlotGate.SlotRequest>) =>
      SlotGate.SlotGate.pipe(Effect.flatMap((gate) => gate.grant(requests)));
    const types = (grants: ReadonlyArray<SlotGate.SlotGrant>) => grants.map((g) => g.type);

    it.effect("off, or no limit: every request is 'off', nothing read, nothing held", () =>
      Effect.gen(function* () {
        for (const settings of [{}, { waitFor: { enabled: true } }, waitsOn({})]) {
          const world = newWorld();
          world.threads = [{ id: "a", instanceId: MAIN }];
          const grants = yield* run(
            world,
            settings,
            grant({ key: "w1", threadId: holder }, { key: "w2", threadId: other }),
          );
          assert.deepStrictEqual(types(grants), ["off", "off"]);
          assert.strictEqual(world.snapshotReads, 0);
          assert.strictEqual(world.memoryReads, 0);
        }
      }),
    );

    it.effect(
      "two waits for one free slot: the first is held, the second is not; no double start",
      () =>
        Effect.gen(function* () {
          const world = newWorld();
          world.threads = [{ id: "a", instanceId: MAIN }];
          yield* run(
            world,
            waitsOn({ maxPerMachine: 2, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const grants = yield* grant(
                { key: "w1", threadId: holder },
                { key: "w2", threadId: other },
              );
              assert.deepStrictEqual(grants, [{ type: "granted", untilMs: 60_000 }, grants[1]!]);
              assert.strictEqual(grants[1]!.type, "full");
              // The slot is held: the other thread is refused, and told to wait for a slot.
              const refused = yield* acquireAs(other);
              assert.strictEqual(refused.type, "refuse");
              if (refused.type === "refuse") assert.include(refused.message, "wait_for");
              // The holder starts its agent; its held slot is used up, not counted twice.
              assert.strictEqual((yield* acquireAs(holder)).type, "allow");
              assert.strictEqual((yield* acquireAs(holder)).type, "refuse");
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            }),
          );
        }),
    );

    it.effect("a held slot ends after reserveSeconds, and a dropped grant holds nothing", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1, reserveSeconds: 30 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), [
              "granted",
            ]);
            assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            yield* TestClock.adjust("30 seconds");
            assert.deepStrictEqual(types(yield* grant({ key: "w2", threadId: holder })), [
              "granted",
            ]);
            const gate = yield* SlotGate.SlotGate;
            yield* gate.drop(["w2"]);
            yield* gate.drop([]);
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
          }),
        );
      }),
    );

    it.effect("reserveSeconds 0: the wait is woken but no slot is held", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1, reserveSeconds: 0 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(yield* grant({ key: "w1", threadId: holder }), [
              { type: "granted", untilMs: 0 },
            ]);
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
          }),
        );
      }),
    );

    it.effect("held slots count only while waits are on (turned off while held)", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            yield* grant({ key: "w1", threadId: holder });
            assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            const settings = yield* ServerSettings.ServerSettingsService;
            yield* settings.updateSettings({ waitFor: { enabled: false } });
            const allowed = yield* acquireAs(other);
            assert.strictEqual(allowed.type, "allow");
            if (allowed.type === "allow") {
              const gate = yield* SlotGate.SlotGate;
              yield* gate.release(allowed.token);
            }
            // Slots turned off: everything is allowed and slot waits are 'off'.
            yield* settings.updateSettings({ waitFor: { enabled: true } });
            yield* settings.updateSettings({ agentSlots: { enabled: false } });
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
            assert.deepStrictEqual(types(yield* grant({ key: "w2", threadId: other })), ["off"]);
          }),
        );
      }),
    );

    it.effect("the limit raised while held: the next grant sees the new limit", () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.threads = [{ id: "a", instanceId: MAIN }];
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), ["full"]);
            const settings = yield* ServerSettings.ServerSettingsService;
            yield* settings.updateSettings({ agentSlots: { maxPerMachine: 3 } });
            assert.deepStrictEqual(
              types(
                yield* grant(
                  { key: "w1", threadId: holder },
                  { key: "w2", threadId: other },
                  { key: "w3", threadId: caller },
                ),
              ),
              ["granted", "granted", "full"],
            );
          }),
        );
      }),
    );

    it.effect("an account wait counts its account; a wait with no account only the machine", () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.threads = [{ id: "a", instanceId: SECOND }];
        yield* run(
          world,
          waitsOn({ maxPerAccount: { [SECOND]: 1 } }),
          Effect.gen(function* () {
            assert.deepStrictEqual(
              types(
                yield* grant(
                  { key: "w1", threadId: holder, account: SECOND },
                  { key: "w2", threadId: other, account: MAIN },
                ),
              ),
              ["full", "granted"],
            );
            // No account and no machine limit: nothing to count, so nothing is read for it.
            const reads = world.snapshotReads;
            assert.deepStrictEqual(types(yield* grant({ key: "w3", threadId: caller })), [
              "granted",
            ]);
            assert.strictEqual(world.snapshotReads, reads);
          }),
        );
      }),
    );

    it.effect("running agents that cannot be counted: 'unread', never granted, nothing held", () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.snapshotFails = true;
        yield* run(
          world,
          waitsOn({ maxPerMachine: 5 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), [
              "unread",
            ]);
            assert.isNull(yield* (yield* SlotGate.SlotGate).usage);
            world.snapshotFails = false;
            world.snapshotDies = true;
            assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), [
              "unread",
            ]);
          }),
        );
      }),
    );

    it.effect("slots held in the wait store: read only while waits are on; used up once", () =>
      Effect.gen(function* () {
        const world = newWorld();
        let reads = 0;
        const used: Array<string> = [];
        let inStore: ReadonlyArray<SlotReservation> = [
          { key: "stored", threadId: holder, untilMs: 90_000 },
        ];
        yield* run(
          world,
          on({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.sync(() => {
                reads += 1;
                return inStore;
              }),
              used: (key) =>
                Effect.sync(() => {
                  used.push(key);
                  inStore = inStore.filter((entry) => entry.key !== key);
                }),
            });
            // Waits off: the store is never read and its slot does not count.
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
            assert.strictEqual(reads, 0);
            assert.deepStrictEqual(used, []);
          }),
        );
        // Restart with waits on (stored slots outlive the gate's memory).
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            yield* gate.attachReservations({
              read: Effect.sync(() => {
                reads += 1;
                return inStore;
              }),
              used: (key) =>
                Effect.sync(() => {
                  used.push(key);
                  inStore = inStore.filter((entry) => entry.key !== key);
                }),
            });
            assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            assert.isAbove(reads, 0);
            const usage = yield* gate.usage;
            assert.deepStrictEqual(usage?.machine, { used: 1, max: 1 });
            assert.strictEqual((yield* acquireAs(holder)).type, "allow");
            assert.deepStrictEqual(used, ["stored"]);
          }),
        );
      }),
    );

    it.effect(
      "a used slot whose store write fails no longer counts; the acquire still allows",
      () =>
        Effect.gen(function* () {
          const world = newWorld();
          yield* run(
            world,
            waitsOn({ maxPerMachine: 2 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              yield* gate.attachReservations({
                read: Effect.succeed([{ key: "stored", threadId: holder, untilMs: 90_000 }]),
                used: () => Effect.die(new Error("disk full")),
              });
              assert.strictEqual((yield* acquireAs(holder)).type, "allow");
              // One starting agent, the store's copy (not cleared) not counted again: one slot free.
              assert.strictEqual((yield* acquireAs(other)).type, "allow");
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            }),
          );
        }),
    );

    it.effect("usage: null while slots are off; counts running, starting and held", () =>
      Effect.gen(function* () {
        const world = newWorld();
        world.threads = [{ id: "a", instanceId: SECOND }];
        assert.isNull(
          yield* run(world, {}, SlotGate.SlotGate.pipe(Effect.flatMap((gate) => gate.usage))),
        );
        assert.strictEqual(world.snapshotReads, 0);
        const usage = yield* run(
          world,
          waitsOn({ maxPerMachine: 4, maxPerAccount: { [MAIN]: 2 } }),
          Effect.gen(function* () {
            yield* grant({ key: "w1", threadId: holder, account: MAIN });
            yield* acquireAs(other, MAIN);
            return yield* (yield* SlotGate.SlotGate).usage;
          }),
        );
        assert.deepStrictEqual(usage, {
          machine: { used: 3, max: 4 },
          accounts: [
            { id: SECOND, used: 1, max: null },
            { id: MAIN, used: 2, max: 2 },
          ].sort((a, b) => (a.id < b.id ? -1 : 1)),
          freeMemoryMb: 16_000,
        });
      }),
    );

    // R17: "as many wakes as free slots". reserveSeconds 0 is a valid setting.
    it.effect("reserveSeconds 0: one free slot wakes one wait, not every wait", () =>
      Effect.gen(function* () {
        const world = newWorld();
        const grants = yield* run(
          world,
          waitsOn({ maxPerMachine: 1, reserveSeconds: 0 }),
          grant(
            { key: "w1", threadId: holder },
            { key: "w2", threadId: other },
            { key: "w3", threadId: caller },
          ),
        );
        assert.deepStrictEqual(types(grants), ["granted", "full", "full"]);
      }),
    );

    it.effect("one held slot per thread: asking again moves it, never adds or extends one", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 3, reserveSeconds: 60 }),
          Effect.gen(function* () {
            const gate = yield* SlotGate.SlotGate;
            // Asked three times (three slots free, at 0, 20 and 40 s): still one slot held.
            for (const [index, key] of ["w1", "w2", "w3"].entries()) {
              if (index > 0) yield* TestClock.adjust("20 seconds");
              assert.deepStrictEqual(types(yield* grant({ key, threadId: holder })), ["granted"]);
            }
            assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 3 });
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
            assert.strictEqual((yield* acquireAs(other)).type, "allow");
            assert.strictEqual((yield* acquireAs(other)).type, "refuse");
            // Not renewed by the later asks: held until the first ask's end (60 s), then gone.
            yield* TestClock.adjust("19 seconds");
            assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 3, max: 3 });
            yield* TestClock.adjust("1 second");
            assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 2, max: 3 });
          }),
        );
      }),
    );

    it.effect("two slot waits of one thread in one sweep: one wake, the other asks next time", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 3 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(
              types(
                yield* grant(
                  { key: "w1", threadId: holder, account: MAIN },
                  { key: "w2", threadId: holder, account: SECOND },
                  { key: "w3", threadId: other },
                ),
              ),
              ["granted", "sameThread", "granted"],
            );
            assert.deepStrictEqual((yield* (yield* SlotGate.SlotGate).usage)?.machine, {
              used: 2,
              max: 3,
            });
          }),
        );
      }),
    );

    it.effect(
      "two stored held slots of one thread (a restart): counted once, both replaced by a new ask",
      () =>
        Effect.gen(function* () {
          const world = newWorld();
          const used: Array<string> = [];
          let inStore: ReadonlyArray<SlotReservation> = [
            { key: "stored-1", threadId: holder, untilMs: 50_000 },
            // A restart lost the gate's note that this older copy was replaced.
            { key: "stored-0", threadId: holder, untilMs: 40_000 },
          ];
          yield* run(
            world,
            waitsOn({ maxPerMachine: 2, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              yield* gate.attachReservations({
                read: Effect.sync(() => inStore),
                used: (key) =>
                  Effect.sync(() => {
                    used.push(key);
                    inStore = inStore.filter((entry) => entry.key !== key);
                  }),
              });
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 2 });
              // Asked again: granted, and both stored copies are cleared (replaced).
              assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), [
                "granted",
              ]);
              yield* gate.keep(["w1"]);
              assert.deepStrictEqual([...used].sort(), ["stored-0", "stored-1"]);
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 2 });
              assert.strictEqual((yield* acquireAs(other)).type, "allow");
              // Its agent starts: the one held slot is used up and the store asked to clear it.
              assert.strictEqual((yield* acquireAs(holder)).type, "allow");
              assert.deepStrictEqual(used.slice(2), ["w1"]);
              assert.strictEqual((yield* acquireAs(caller)).type, "refuse");
            }),
          );
        }),
    );

    // The old hold goes only once the new grant is written (`keep`).
    it.effect(
      "a re-ask that is not kept (cancelled, or its write failed) leaves the slot the thread held",
      () =>
        Effect.gen(function* () {
          const world = newWorld();
          const used: Array<string> = [];
          let inStore: ReadonlyArray<SlotReservation> = [
            { key: "stored", threadId: holder, untilMs: 50_000 },
          ];
          const attach = (gate: SlotGate.SlotGate["Service"]) =>
            gate.attachReservations({
              read: Effect.sync(() => inStore),
              used: (key) =>
                Effect.sync(() => {
                  used.push(key);
                  inStore = inStore.filter((entry) => entry.key !== key);
                }),
            });
          yield* run(
            world,
            waitsOn({ maxPerMachine: 1, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              yield* attach(gate);
              assert.deepStrictEqual(types(yield* grant({ key: "w1", threadId: holder })), [
                "granted",
              ]);
              // Granted but not written yet: the stored hold is still there (a stop here keeps it).
              assert.deepStrictEqual(used, []);
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 1 });
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");
              // The re-ask was cancelled (or its write failed): its grant is dropped.
              yield* gate.drop(["w1"]);
              assert.deepStrictEqual(used, []);
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 1 });
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");

              // Asked again and written this time: the stored hold is replaced, still one slot.
              assert.deepStrictEqual(types(yield* grant({ key: "w2", threadId: holder })), [
                "granted",
              ]);
              yield* gate.keep(["w2"]);
              yield* gate.keep(["w2"]);
              assert.deepStrictEqual(used, ["stored"]);
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 1 });
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");
              // A late drop of a kept grant (or of one never made) changes nothing.
              yield* gate.drop(["w1"]);
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");

              // Not kept, then its agent starts: the old and the new hold are both used up.
              yield* TestClock.adjust("10 seconds");
              assert.deepStrictEqual(types(yield* grant({ key: "w3", threadId: holder })), [
                "granted",
              ]);
              assert.strictEqual((yield* acquireAs(holder)).type, "allow");
              yield* gate.drop(["w3"]);
              assert.deepStrictEqual(used.slice(1).sort(), ["w2", "w3"]);
              // Only the started agent counts now (its start grace), no held slot.
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 1, max: 1 });
            }),
          );
          // A memory-only hold: dropped re-ask, the hold stays until its own end.
          yield* run(
            world,
            waitsOn({ maxPerMachine: 1, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              assert.deepStrictEqual(types(yield* grant({ key: "m1", threadId: holder })), [
                "granted",
              ]);
              yield* gate.keep(["m1"]);
              yield* TestClock.adjust("30 seconds");
              assert.deepStrictEqual(types(yield* grant({ key: "m2", threadId: holder })), [
                "granted",
              ]);
              yield* gate.drop(["m2"]);
              assert.strictEqual((yield* acquireAs(other)).type, "refuse");
              yield* TestClock.adjust("30 seconds");
              assert.deepStrictEqual((yield* gate.usage)?.machine, { used: 0, max: 1 });
            }),
          );
        }),
    );

    // While a re-ask on another account (or none) is not kept, the old hold still counts
    // against its own account, so a dropped re-ask never leaves that account booked twice.
    it.effect(
      "a re-ask not kept yet still counts the old hold against its account, once on the machine",
      () =>
        Effect.gen(function* () {
          const world = newWorld();
          const third = ThreadId.make("thread:slot-third");
          yield* run(
            world,
            waitsOn({ maxPerMachine: 2, maxPerAccount: { [SECOND]: 1 }, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              assert.deepStrictEqual(
                types(yield* grant({ key: "h0", threadId: holder, account: SECOND })),
                ["granted"],
              );
              yield* gate.keep(["h0"]);
              // One sweep: the holder re-asks with no account, then `other` asks for SECOND.
              // The holder's SECOND hold is hidden but still counts there: `other` waits.
              assert.deepStrictEqual(
                types(
                  yield* grant(
                    { key: "w1", threadId: holder },
                    { key: "w2", threadId: other, account: SECOND },
                  ),
                ),
                ["granted", "full"],
              );
              // The settings screen shows the hidden hold on SECOND too (1 of 1, not 0 of 1).
              assert.deepStrictEqual(
                (yield* gate.usage)?.accounts.find((account) => account.id === SECOND),
                { id: SECOND, used: 1, max: 1 },
              );
              assert.strictEqual((yield* acquireAs(other, SECOND)).type, "refuse");
              // Not twice against the machine: the holder counts once (its new hold), so a
              // third thread still gets the second machine slot.
              assert.deepStrictEqual(types(yield* grant({ key: "w3", threadId: third })), [
                "granted",
              ]);
              // The re-ask is cancelled before it is written: the old hold is back, and SECOND
              // holds it once (not over its limit), for the holder only.
              yield* gate.drop(["w1"]);
              assert.deepStrictEqual(
                types(yield* grant({ key: "w2", threadId: other, account: SECOND })),
                ["full"],
              );
              const usage = yield* gate.usage;
              assert.deepStrictEqual(
                usage?.accounts.find((account) => account.id === SECOND),
                { id: SECOND, used: 1, max: 1 },
              );
              assert.strictEqual((yield* acquireAs(holder, SECOND)).type, "allow");
            }),
          );
          // Kept this time: the old SECOND hold is used up, and `other` gets SECOND next sweep.
          yield* run(
            newWorld(),
            waitsOn({ maxPerMachine: 3, maxPerAccount: { [SECOND]: 1 }, reserveSeconds: 60 }),
            Effect.gen(function* () {
              const gate = yield* SlotGate.SlotGate;
              yield* grant({ key: "h0", threadId: holder, account: SECOND });
              yield* gate.keep(["h0"]);
              assert.deepStrictEqual(
                types(
                  yield* grant(
                    { key: "w1", threadId: holder },
                    { key: "w2", threadId: other, account: SECOND },
                  ),
                ),
                ["granted", "full"],
              );
              yield* gate.keep(["w1"]);
              assert.deepStrictEqual(
                types(yield* grant({ key: "w2", threadId: other, account: SECOND })),
                ["granted"],
              );
            }),
          );
        }),
    );

    it.effect("a thread already holding a slot that is refused keeps the slot it holds", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 2, maxPerAccount: { [SECOND]: 1 } }),
          Effect.gen(function* () {
            yield* grant({ key: "w1", threadId: other, account: SECOND });
            assert.deepStrictEqual(types(yield* grant({ key: "w2", threadId: holder })), [
              "granted",
            ]);
            // The account is full: refused, and its held machine slot stays.
            assert.deepStrictEqual(
              types(yield* grant({ key: "w3", threadId: holder, account: SECOND })),
              ["full"],
            );
            assert.deepStrictEqual((yield* (yield* SlotGate.SlotGate).usage)?.machine, {
              used: 2,
              max: 2,
            });
          }),
        );
      }),
    );
    it.effect("asking again moves a held slot but never extends it past the first end", () =>
      Effect.gen(function* () {
        const world = newWorld();
        yield* run(
          world,
          waitsOn({ maxPerMachine: 1, reserveSeconds: 60 }),
          Effect.gen(function* () {
            assert.deepStrictEqual(types(yield* grant({ key: "h0", threadId: holder })), [
              "granted",
            ]);
            // An older wait of another thread comes first in every sweep; the holder asks
            // again every 50 s without starting an agent.
            const otherGot: Array<string> = [];
            for (let round = 1; round <= 10; round += 1) {
              yield* TestClock.adjust("50 seconds");
              const grants = yield* grant(
                { key: "o1", threadId: other },
                { key: `h${round}`, threadId: holder },
              );
              otherGot.push(grants[0]!.type);
            }
            // The first hold ended at 60 s: the other thread got the slot at the 100 s sweep.
            assert.deepStrictEqual(otherGot.slice(0, 2), ["full", "granted"]);
          }),
        );
      }),
    );
  });
});
