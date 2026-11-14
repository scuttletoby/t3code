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
import * as Effect from "effect/Effect";
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
  freeMemoryBytes: number;
}

const newWorld = (): World => ({
  threads: [],
  snapshotReads: 0,
  memoryReads: 0,
  snapshotFails: false,
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
              assert.deepStrictEqual(options, { unsettledOnly: true });
              world.snapshotReads += 1;
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
    "off (the default) or with no limit: allows and reads neither projection nor memory",
    () =>
      Effect.gen(function* () {
        for (const settings of [
          {},
          { agentSlots: { maxPerMachine: 1, minFreeMemoryMb: 100_000 } },
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
    "stops half-way: a released slot is free at once; a missed release ends after the grace",
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
            // Never released nor started (say the call was cut off): it ends after the grace.
            yield* TestClock.adjust("29 seconds");
            assert.strictEqual((yield* acquire()).type, "refuse");
            yield* TestClock.adjust("1 second");
            assert.strictEqual((yield* acquire()).type, "allow");
          }),
        );
      }),
  );

  it.effect("a started child that never shows a run stops counting after the grace", () =>
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
          assert.strictEqual((yield* acquire()).type, "refuse");
          yield* TestClock.adjust("10 seconds");
          assert.strictEqual((yield* acquire()).type, "allow");
        }),
      );
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
});
