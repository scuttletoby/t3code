/**
 * Fork (placement P3): the facts read over the real agent slot gate (`SlotGate`), not
 * a stand-in, so a change to the gate's status read that the facts depend on fails here. Only the
 * gate's inputs are stand-ins: the running threads, the host sample and the settings.
 */
import { expect, it } from "@effect/vitest";
import {
  type OrchestrationV2ThreadShell,
  type OrchestrationV2ThreadShellSnapshot,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ActiveRunShells from "../slots/ActiveRunShells.ts";
import * as SlotGate from "../slots/SlotGate.ts";
import * as EnvironmentFacts from "./environmentFacts.ts";

const MB = 1024 * 1024;

const sample = (freeMb: number) => ({
  sampledAt: 0,
  cpuUtilization: 0.1,
  cpuCount: 4,
  availableMemoryBytes: freeMb * MB,
  totalMemoryBytes: 32_000 * MB,
});

const snapshotOf = (running: number): OrchestrationV2ThreadShellSnapshot => ({
  schemaVersion: 1,
  snapshotSequence: 1,
  threads: Array.from(
    { length: running },
    (_, index) =>
      ({
        id: ThreadId.make(`thread:running-${index}`),
        providerInstanceId: ProviderInstanceId.make("codex"),
        activeRunId: `run:${index}`,
      }) as unknown as OrchestrationV2ThreadShell,
  ),
  archivedThreads: [],
});

const readOver = (
  world: { readonly running: number; readonly freeMb: number },
  agentSlots: Record<string, unknown>,
) =>
  EnvironmentFacts.read.pipe(
    Effect.provide(
      SlotGate.layerOver.pipe(
        Layer.provideMerge(
          Layer.mergeAll(
            ServerSettings.layerTest({ agentSlots }),
            Layer.mock(ActiveRunShells.ActiveRunShells)({
              read: Effect.succeed(snapshotOf(world.running)),
            }),
            Layer.mock(HostResources.HostResources)({
              read: Effect.succeed(sample(world.freeMb)),
            }),
          ),
        ),
      ),
    ),
  );

it.effect("the real gate's counts: running agents, slots free, the memory floor", () =>
  Effect.gen(function* () {
    const slots = { enabled: true, maxPerMachine: 3, minFreeMemoryMb: 1000 };
    const { placement, hostResources } = yield* readOver({ running: 2, freeMb: 4000 }, slots);
    expect(hostResources).toEqual(sample(4000));
    expect(placement).toEqual({
      labels: [],
      available: true,
      runningAgents: 2,
      slotsFree: 1,
      memoryFloorMb: 1000,
      freeMemoryMb: 4000,
      factsAt: 0,
    });
  }),
);

it.effect("the real gate full, or below its floor: not available, with the reason", () =>
  Effect.gen(function* () {
    const slots = { enabled: true, maxPerMachine: 3, minFreeMemoryMb: 1000 };
    const full = yield* readOver({ running: 3, freeMb: 4000 }, slots);
    expect(full.placement).toMatchObject({
      available: false,
      reason: EnvironmentFacts.REASON_FULL,
      runningAgents: 3,
      slotsFree: 0,
    });
    const low = yield* readOver({ running: 0, freeMb: 500 }, slots);
    expect(low.placement).toMatchObject({
      available: false,
      reason: EnvironmentFacts.REASON_LOW_MEMORY,
      freeMemoryMb: 500,
    });
    // Runs again: the same facts from the same state.
    expect((yield* readOver({ running: 3, freeMb: 4000 }, slots)).placement).toEqual(
      full.placement,
    );
  }),
);

it.effect("the real gate with slots off: available, no counts", () =>
  Effect.gen(function* () {
    const { placement } = yield* readOver({ running: 5, freeMb: 100 }, { enabled: false });
    expect(placement).toEqual({
      labels: [],
      available: true,
      reason: EnvironmentFacts.REASON_SLOTS_OFF,
      factsAt: 0,
    });
  }),
);
