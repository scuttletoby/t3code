/**
 * Fork: the agent slot gate on `delegate_task` (`agentSlots`, off: reads only the settings). Counts
 * running agents from the projection; its own state (starting agents) is memory only, never disk.
 */
import type { ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import {
  effectiveSlotLimits,
  limitsCount,
  liveStarting,
  runningAgents,
  slotDecision,
  slotRefusalText,
  type RunningAgent,
  type StartingEntry,
} from "@t3tools/shared/agentSlots";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Semaphore from "effect/Semaphore";

import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";

/** Names one `starting` entry; `release` or `started` with it. */
export type SlotToken = number;

export type SlotAcquire =
  /** `token` is `null` when the gate is off or has no limit (nothing to release). */
  | { readonly type: "allow"; readonly token: SlotToken | null }
  | { readonly type: "refuse"; readonly message: string };

export class SlotGate extends Context.Service<
  SlotGate,
  {
    /**
     * Allow or refuse one new delegated agent on `targetInstanceId`. An allowed call counts as
     * running at once, so concurrent callers never get more slots than are free.
     */
    readonly acquire: (input: {
      readonly callerThreadId: ThreadId;
      readonly targetInstanceId: ProviderInstanceId;
    }) => Effect.Effect<SlotAcquire>;
    /** The dispatch named the child: its entry ends as soon as the child is counted as running. */
    readonly started: (token: SlotToken | null, childThreadId: ThreadId) => Effect.Effect<void>;
    /** The dispatch failed: the slot is free again. */
    readonly release: (token: SlotToken | null) => Effect.Effect<void>;
  }
>()("t3/slots/SlotGate") {}

const make = Effect.gen(function* () {
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const threadManagement = yield* ThreadManagementService.ThreadManagementService;
  const hostResources = yield* HostResources.HostResources;
  const semaphore = yield* Semaphore.make(1);
  const entries = yield* Ref.make<ReadonlyArray<StartingEntry>>([]);
  const nextToken = yield* Ref.make<SlotToken>(1);

  const readRunning = (callerThreadId: ThreadId) =>
    threadManagement.getShellSnapshot({ unsettledOnly: true }).pipe(
      Effect.map((snapshot): ReadonlyArray<RunningAgent> | undefined =>
        runningAgents([...snapshot.threads, ...snapshot.archivedThreads], callerThreadId),
      ),
      // Counts that cannot be read never stop a delegation; the memory floor still applies.
      Effect.catch((cause) =>
        Effect.logWarning("agent slots: running agents could not be counted", { cause }).pipe(
          Effect.as(undefined),
        ),
      ),
    );

  const acquire = Effect.fn("SlotGate.acquire")(
    function* (input: {
      readonly callerThreadId: ThreadId;
      readonly targetInstanceId: ProviderInstanceId;
    }) {
      // Settings that cannot be read count as off, as for the usage pacing gate.
      const settings = yield* serverSettings.getSettings.pipe(Effect.option);
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      if (limits === null) return { type: "allow", token: null } satisfies SlotAcquire;
      const nowMs = yield* Clock.currentTimeMillis;
      const counted = limitsCount(limits, input.targetInstanceId);
      const running = counted ? yield* readRunning(input.callerThreadId) : undefined;
      const kept = yield* Ref.updateAndGet(entries, (current) =>
        current.filter((entry) => nowMs - entry.atMs < limits.startGraceMs),
      );
      const freeMemoryBytes =
        limits.minFreeMemoryBytes === null
          ? null
          : (yield* hostResources.read).availableMemoryBytes;
      const decision = slotDecision({
        limits: running === undefined ? { ...limits, machine: null, account: () => null } : limits,
        running: running ?? [],
        starting: liveStarting(kept, running ?? [], nowMs, limits.startGraceMs),
        targetInstanceId: input.targetInstanceId,
        freeMemoryBytes,
      });
      if (decision.type === "refuse") {
        // Slot waits (the `slot` kind of wait_for) come with the wait tools; until then: later.
        return { type: "refuse", message: slotRefusalText(decision, false) } satisfies SlotAcquire;
      }
      const token = yield* Ref.getAndUpdate(nextToken, (value) => value + 1);
      yield* Ref.update(entries, (current) => [
        ...current,
        { token, instanceId: input.targetInstanceId, atMs: nowMs },
      ]);
      return { type: "allow", token } satisfies SlotAcquire;
    },
    // One at a time: the next caller sees the entry the last one added.
    semaphore.withPermits(1),
  );

  return SlotGate.of({
    acquire,
    started: Effect.fn("SlotGate.started")((token: SlotToken | null, childThreadId: ThreadId) =>
      token === null
        ? Effect.void
        : Ref.update(entries, (current) =>
            current.map((entry) => (entry.token === token ? { ...entry, childThreadId } : entry)),
          ),
    ),
    release: Effect.fn("SlotGate.release")((token: SlotToken | null) =>
      token === null
        ? Effect.void
        : Ref.update(entries, (current) => current.filter((entry) => entry.token !== token)),
    ),
  });
});

export const layer = Layer.effect(SlotGate, make);
