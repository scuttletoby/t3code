/**
 * Fork: the server's agent slot gate over a world a test controls, for the tests of the
 * routes that start agents (`agentSlotRoutes.ts`). The gate is the real one; only what it counts
 * (the threads with a running agent) and the host's memory are made up.
 */
import {
  type OrchestrationV2ThreadShell,
  type ProviderInstanceId,
  RunId,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";

import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ActiveRunShells from "../slots/ActiveRunShells.ts";
import * as SlotGate from "../slots/SlotGate.ts";

/** What the gate counts: threads with a running agent, and how often it read them. */
export interface SlotWorld {
  running: Array<ThreadId>;
  /** The count read fails (the thread list cannot be read). */
  fails: boolean;
  reads: number;
}

export const slotWorld = (running: ReadonlyArray<ThreadId> = []): SlotWorld => ({
  running: [...running],
  fails: false,
  reads: 0,
});

const MB = 1024 * 1024;

/** The server's slot gate over `world`; it still needs the server settings and `NodeServices`. */
export const slotGateOver = (world: SlotWorld, instanceId: ProviderInstanceId) =>
  SlotGate.layerOver.pipe(
    Layer.provide(
      Layer.mergeAll(
        Layer.mock(ActiveRunShells.ActiveRunShells)({
          read: Effect.suspend(() => {
            world.reads += 1;
            if (world.fails) return Effect.die("thread list unreadable") as never;
            return Effect.succeed({
              schemaVersion: 1,
              snapshotSequence: 1,
              threads: world.running.map(
                (id) =>
                  ({
                    id,
                    providerInstanceId: instanceId,
                    activeRunId: RunId.make(`run:${id}`),
                  }) as unknown as OrchestrationV2ThreadShell,
              ),
              archivedThreads: [],
            });
          }),
        }),
        Layer.mock(HostResources.HostResources)({
          read: Effect.succeed({
            sampledAt: 0,
            cpuUtilization: null,
            cpuCount: 4,
            availableMemoryBytes: 16_000 * MB,
            totalMemoryBytes: 32_000 * MB,
          }),
        }),
      ),
    ),
  );
