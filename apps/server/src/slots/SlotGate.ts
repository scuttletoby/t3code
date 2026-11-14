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
  visibleRunThreadIds,
  type RunningAgent,
  type StartingEntry,
} from "@t3tools/shared/agentSlots";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
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
    /** The dispatch named the child: its entry ends as soon as the child shows a run. */
    readonly started: (token: SlotToken | null, childThreadId: ThreadId) => Effect.Effect<void>;
    /**
     * The dispatch ended, whichever way (call it from a finalizer). Without a started child the
     * slot is free again at once; a started child that shows no run yet still counts for the
     * start grace. Calling it again does nothing more.
     */
    readonly release: (token: SlotToken | null) => Effect.Effect<void>;
  }
>()("t3/slots/SlotGate") {}

/** The longest the count read may take; after it the gate lets the call through, as unread. */
export const COUNT_READ_TIMEOUT = Duration.seconds(5);

const make = Effect.gen(function* () {
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const threadManagement = yield* ThreadManagementService.ThreadManagementService;
  const hostResources = yield* HostResources.HostResources;
  const semaphore = yield* Semaphore.make(1);
  const entries = yield* Ref.make<ReadonlyArray<StartingEntry>>([]);
  const nextToken = yield* Ref.make<SlotToken>(1);

  /**
   * Threads with an active run, settled or not (settling a thread does not stop its run). `undefined`
   * when they cannot be read in time: counts never stop a delegation; the memory floor still applies.
   * Interruptible on its own, so the timeout works inside the caller's uninterruptible acquire.
   */
  const readRunning = (callerThreadId: ThreadId) =>
    Effect.interruptible(threadManagement.getShellSnapshot({ activeRunOnly: true })).pipe(
      // A timeout fails with a TimeoutError, which the catch below logs like any other read failure.
      Effect.timeout(COUNT_READ_TIMEOUT),
      Effect.map((snapshot) => {
        const threads = [...snapshot.threads, ...snapshot.archivedThreads];
        return {
          running: runningAgents(threads, callerThreadId),
          visible: visibleRunThreadIds(threads),
        } satisfies { running: ReadonlyArray<RunningAgent>; visible: ReadonlySet<string> };
      }),
      Effect.catch((cause) =>
        Effect.logWarning("agent slots: running agents could not be counted", { cause }).pipe(
          Effect.as(undefined),
        ),
      ),
      // A bug in the count read (a defect, not a failure) must not refuse every delegation either.
      Effect.catchDefect((defect) =>
        Effect.logWarning("agent slots: counting running agents crashed", { defect }).pipe(
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
      const read = counted ? yield* readRunning(input.callerThreadId) : undefined;
      const running = read?.running;
      // Ended entries go for good: a child seen with a run counts as running from now on.
      const kept = yield* Ref.updateAndGet(entries, (current) =>
        liveStarting(current, read?.visible ?? new Set(), nowMs, limits.startGraceMs),
      );
      const freeMemoryBytes =
        limits.minFreeMemoryBytes === null
          ? null
          : (yield* hostResources.read).availableMemoryBytes;
      const decision = slotDecision({
        limits: running === undefined ? { ...limits, machine: null, account: () => null } : limits,
        running: running ?? [],
        starting: kept,
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
    release: Effect.fn("SlotGate.release")(function* (token: SlotToken | null) {
      if (token === null) return;
      const nowMs = yield* Clock.currentTimeMillis;
      yield* Ref.update(entries, (current) =>
        current.flatMap((entry) =>
          entry.token !== token
            ? [entry]
            : entry.childThreadId === undefined
              ? []
              : [entry.endedAtMs === undefined ? { ...entry, endedAtMs: nowMs } : entry],
        ),
      );
    }),
  });
});

export const layer = Layer.effect(SlotGate, make);
