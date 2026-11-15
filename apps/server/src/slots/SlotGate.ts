/**
 * Fork: the agent slot gate on `delegate_task` (`agentSlots`, off: reads only the settings). Counts
 * running agents from the projection; its own state (starting agents, slots held for met slot
 * waits) is memory only, never disk. Slots held by delivered slot waits come from the wait store,
 * which the wait service attaches (`attachReservations`); while waits are off none count.
 */
import type { ProviderInstanceId, ThreadId } from "@t3tools/contracts";
import {
  effectiveSlotLimits,
  limitsCount,
  liveReservations,
  liveStarting,
  onePerThread,
  runningAgents,
  slotDecision,
  slotRefusalText,
  visibleRunThreadIds,
  type RunningAgent,
  type SlotDecision,
  type SlotLimits,
  type SlotReservation,
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

/** Slots held by delivered slot waits, as the wait store keeps them (`reservedUntil`). */
export interface StoredReservations {
  /** The live ones; read only while waits are on. */
  readonly read: Effect.Effect<ReadonlyArray<SlotReservation>>;
  /** The gate let the holder start an agent: clear the wait's `reservedUntil` (a write). */
  readonly used: (key: string) => Effect.Effect<void>;
}

/** One slot wait asking for a slot; `key` is its wait id. */
export interface SlotRequest {
  readonly key: string;
  readonly threadId: ThreadId;
  readonly account?: ProviderInstanceId | undefined;
}

export type SlotGrant =
  /** Slots are off or have no limit: nothing to wait for, nothing held. */
  | { readonly type: "off" }
  /**
   * Free: held for the thread until `untilMs` (memory only). A thread holds at most one slot: a
   * slot it already held moves to this request (its end renewed), it is never added to.
   */
  | { readonly type: "granted"; readonly untilMs: number }
  /** An earlier request of the same thread was granted in this call: asked again next time. */
  | { readonly type: "sameThread" }
  | { readonly type: "full"; readonly decision: Extract<SlotDecision, { readonly type: "refuse" }> }
  /** The running agents cannot be counted now: never "free". */
  | { readonly type: "unread"; readonly message: string };

/** What `wait_list` shows while slots are on (agents running, starting or held). */
export interface SlotUsage {
  readonly machine: { readonly used: number; readonly max: number | null };
  readonly accounts: ReadonlyArray<{
    readonly id: ProviderInstanceId;
    readonly used: number;
    readonly max: number | null;
  }>;
  readonly freeMemoryMb: number | null;
}

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
    /** The wait service gives the gate the slots its store holds (once, when it starts). */
    readonly attachReservations: (stored: StoredReservations) => Effect.Effect<void>;
    /**
     * Slot waits ask for slots, oldest first: each one granted is held for its thread at once, so
     * the next request (and every other thread's delegation) counts it.
     */
    readonly grant: (
      requests: ReadonlyArray<SlotRequest>,
    ) => Effect.Effect<ReadonlyArray<SlotGrant>>;
    /** Grants that were not kept (the wait was cancelled, or its write failed) hold nothing. */
    readonly drop: (keys: ReadonlyArray<string>) => Effect.Effect<void>;
    /** For `wait_list`: null while slots are off, or when the running agents cannot be read. */
    readonly usage: Effect.Effect<SlotUsage | null>;
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
  /** Slots granted to slot waits (memory only; lost on a restart, which costs one more wait). */
  const held = yield* Ref.make<ReadonlyArray<SlotReservation>>([]);
  /** Keys used up, until their end: the store's copy stops counting even if clearing it failed. */
  const usedKeys = new Map<string, number>();
  let stored: StoredReservations | null = null;

  /**
   * Threads with an active run, settled or not (settling a thread does not stop its run), nobody
   * left out. `undefined` when they cannot be read in time: counts never stop a delegation (the
   * memory floor still applies) and never make a slot wait "free". Interruptible on its own, so
   * the timeout works inside the caller's uninterruptible acquire.
   */
  const readRunning = Effect.interruptible(
    threadManagement.getShellSnapshot({ activeRunOnly: true }),
  ).pipe(
    // A timeout fails with a TimeoutError, which the catch below logs like any other read failure.
    Effect.timeout(COUNT_READ_TIMEOUT),
    Effect.map((snapshot) => {
      const threads = [...snapshot.threads, ...snapshot.archivedThreads];
      return {
        running: runningAgents(threads, null),
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

  /** Ended entries go for good: a child seen with a run counts as running from now on. */
  const keepStarting = (
    visible: ReadonlySet<string> | undefined,
    nowMs: number,
    limits: SlotLimits,
  ) =>
    Ref.updateAndGet(entries, (current) =>
      liveStarting(current, visible ?? new Set(), nowMs, limits.startGraceMs),
    );

  /**
   * Every live held slot: granted in memory and kept in the wait store, one per wait (counted one
   * per thread, `onePerThread`). None while waits are off (nothing of the store is read then). A
   * store that cannot be read holds none.
   */
  const liveHeld = Effect.fn("SlotGate.liveHeld")(function* (waitsOn: boolean, nowMs: number) {
    for (const [key, untilMs] of usedKeys) if (untilMs <= nowMs) usedKeys.delete(key);
    const memory = yield* Ref.updateAndGet(held, (current) => liveReservations(current, nowMs));
    if (!waitsOn) return [];
    const fromStore = stored === null ? [] : yield* stored.read;
    return liveReservations([...memory, ...fromStore], nowMs).filter(
      (reservation) => !usedKeys.has(reservation.key),
    );
  });

  const freeMemoryBytes = (limits: SlotLimits) =>
    limits.minFreeMemoryBytes === null
      ? Effect.succeed(null)
      : hostResources.read.pipe(Effect.map((sample) => sample.availableMemoryBytes));

  /** Settings that cannot be read count as off, as for the usage pacing gate. */
  const readSettings = serverSettings.getSettings.pipe(Effect.option);

  /**
   * Every slot held for `threadId` stops counting (memory ones go; stored ones are skipped until
   * their end). Returns their keys, for the store to clear. Under the semaphore.
   */
  const useUp = Effect.fnUntraced(function* (
    reservations: ReadonlyArray<SlotReservation>,
    threadId: string,
  ) {
    const own = reservations.filter((reservation) => reservation.threadId === threadId);
    if (own.length === 0) return [];
    for (const reservation of own) usedKeys.set(reservation.key, reservation.untilMs);
    yield* Ref.update(held, (current) => current.filter((entry) => entry.threadId !== threadId));
    return own.map((reservation) => reservation.key);
  });

  /** Clears used or replaced held slots in the store, outside the semaphore (see `acquire`). */
  const clearStored = (keys: ReadonlyArray<string>) =>
    stored === null || keys.length === 0
      ? Effect.void
      : Effect.forEach(keys, stored.used, { discard: true }).pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("agent slots: a used slot could not be cleared in the wait store", {
              cause,
            }),
          ),
        );

  /**
   * The decision, under the semaphore, and the caller's held slot it used up (if any). The
   * caller's own held slots never count against it; they are not a pass past a limit either.
   */
  const decide = Effect.fn("SlotGate.decide")(
    function* (input: {
      readonly callerThreadId: ThreadId;
      readonly targetInstanceId: ProviderInstanceId;
    }) {
      const settings = yield* readSettings;
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      if (Option.isNone(settings) || limits === null) {
        return {
          outcome: { type: "allow", token: null } satisfies SlotAcquire,
          used: [] as ReadonlyArray<string>,
        };
      }
      const waitsOn = settings.value.waitFor.enabled;
      const nowMs = yield* Clock.currentTimeMillis;
      const counted = limitsCount(limits, input.targetInstanceId);
      const read = counted ? yield* readRunning : undefined;
      const running = read?.running.filter((agent) => agent.threadId !== input.callerThreadId);
      const kept = yield* keepStarting(read?.visible, nowMs, limits);
      const reservations = yield* liveHeld(waitsOn, nowMs);
      const decision = slotDecision({
        limits: running === undefined ? { ...limits, machine: null, account: () => null } : limits,
        running: running ?? [],
        starting: kept,
        reservations: onePerThread(
          reservations.filter((reservation) => reservation.threadId !== input.callerThreadId),
        ),
        targetInstanceId: input.targetInstanceId,
        freeMemoryBytes: yield* freeMemoryBytes(limits),
      });
      if (decision.type === "refuse") {
        return {
          outcome: {
            type: "refuse",
            message: slotRefusalText(decision, waitsOn),
          } satisfies SlotAcquire,
          used: [] as ReadonlyArray<string>,
        };
      }
      const token = yield* Ref.getAndUpdate(nextToken, (value) => value + 1);
      yield* Ref.update(entries, (current) => [
        ...current,
        { token, instanceId: input.targetInstanceId, atMs: nowMs },
      ]);
      // The agent it held a slot for starts now: its held slot is used up, with every older copy.
      const used = yield* useUp(reservations, input.callerThreadId);
      return { outcome: { type: "allow", token } satisfies SlotAcquire, used };
    },
    // One at a time: the next caller sees the entry the last one added.
    semaphore.withPermits(1),
  );

  const acquire = Effect.fn("SlotGate.acquire")(function* (input: {
    readonly callerThreadId: ThreadId;
    readonly targetInstanceId: ProviderInstanceId;
  }) {
    const { outcome, used } = yield* decide(input);
    // Cleared in the store after the decision, outside the semaphore; a failed write only means
    // the stored copy ends by itself (it no longer counts here either way).
    yield* clearStored(used);
    return outcome;
  });

  const grantLocked = Effect.fn("SlotGate.grantLocked")(
    function* (requests: ReadonlyArray<SlotRequest>) {
      const settings = yield* readSettings;
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      if (Option.isNone(settings) || limits === null) {
        return { grants: requests.map((): SlotGrant => ({ type: "off" })), replaced: [] };
      }
      if (requests.length === 0) return { grants: [], replaced: [] };
      const nowMs = yield* Clock.currentTimeMillis;
      /** A wait without an account waits for the machine limit and the memory floor only. */
      const limitsFor = (request: SlotRequest): SlotLimits =>
        request.account === undefined ? { ...limits, account: () => null } : limits;
      const counted = requests.some((request) =>
        limitsCount(limitsFor(request), request.account ?? ""),
      );
      // Read only when a count limit applies to one of them (3.6).
      const read = counted ? yield* readRunning : undefined;
      const kept = yield* keepStarting(read?.visible, nowMs, limits);
      const memoryBytes = yield* freeMemoryBytes(limits);
      const reserveMs = settings.value.agentSlots.reserveSeconds * 1000;
      let reservations = yield* liveHeld(settings.value.waitFor.enabled, nowMs);
      const grants: Array<SlotGrant> = [];
      const replaced: Array<string> = [];
      const grantedThreads = new Set<string>();
      for (const request of requests) {
        // One wake per thread in a call: its other slot waits ask again next time.
        if (grantedThreads.has(request.threadId)) {
          grants.push({ type: "sameThread" });
          continue;
        }
        const own = limitsFor(request);
        if (read === undefined && limitsCount(own, request.account ?? "")) {
          grants.push({ type: "unread", message: "The running agents cannot be counted now." });
          continue;
        }
        const decision = slotDecision({
          limits: own,
          running: (read?.running ?? []).filter((agent) => agent.threadId !== request.threadId),
          starting: kept,
          // Its own held slot is not counted: a grant moves it here, so the thread keeps one.
          reservations: onePerThread(
            reservations.filter((reservation) => reservation.threadId !== request.threadId),
          ),
          targetInstanceId: request.account ?? "",
          freeMemoryBytes: memoryBytes,
        });
        if (decision.type === "refuse") {
          // Refused: a slot it already holds stays as it is.
          grants.push({ type: "full", decision });
          continue;
        }
        const untilMs = nowMs + reserveMs;
        const reservation: SlotReservation = {
          key: request.key,
          threadId: request.threadId,
          ...(request.account === undefined ? {} : { account: request.account }),
          untilMs,
        };
        // The slot it held before (memory or store) is replaced by this one.
        replaced.push(...(yield* useUp(reservations, request.threadId)));
        // Counted for the rest of this call even when held 0 s: one free slot, one wake (R17).
        reservations = [
          ...reservations.filter((entry) => entry.threadId !== request.threadId),
          reservation,
        ];
        // A slot held for 0 s is not held after this call; the wait is still woken.
        if (reserveMs > 0) yield* Ref.update(held, (current) => [...current, reservation]);
        grantedThreads.add(request.threadId);
        grants.push({ type: "granted", untilMs });
      }
      return { grants, replaced };
    },
    // Under the same semaphore as `acquire`: a delegation never takes a slot being granted.
    semaphore.withPermits(1),
  );

  const grant = Effect.fn("SlotGate.grant")(function* (requests: ReadonlyArray<SlotRequest>) {
    const { grants, replaced } = yield* grantLocked(requests);
    // A replaced slot in the store is cleared like a used one, outside the semaphore.
    yield* clearStored(replaced);
    return grants;
  });

  const usage = Effect.gen(function* () {
    const settings = yield* readSettings;
    if (Option.isNone(settings) || !settings.value.agentSlots.enabled) return null;
    const agentSlots = settings.value.agentSlots;
    const read = yield* readRunning;
    if (read === undefined) return null;
    const nowMs = yield* Clock.currentTimeMillis;
    const limits = effectiveSlotLimits(agentSlots);
    const starting =
      limits === null
        ? []
        : liveStarting(yield* Ref.get(entries), read.visible, nowMs, limits.startGraceMs);
    const reservations = onePerThread(yield* liveHeld(settings.value.waitFor.enabled, nowMs));
    const accountMax = (id: string) => {
      const max = agentSlots.maxPerAccount[id as ProviderInstanceId];
      return max === undefined || max <= 0 ? null : max;
    };
    const ids = new Set<string>([
      ...Object.keys(agentSlots.maxPerAccount).filter((id) => accountMax(id) !== null),
      ...read.running.map((agent) => agent.instanceId),
      ...starting.map((entry) => entry.instanceId),
      ...reservations.flatMap((reservation) =>
        reservation.account === undefined ? [] : [reservation.account],
      ),
    ]);
    const memory = yield* hostResources.read;
    return {
      machine: {
        used: read.running.length + starting.length + reservations.length,
        max: agentSlots.maxPerMachine > 0 ? agentSlots.maxPerMachine : null,
      },
      accounts: [...ids].sort().map((id) => ({
        id: id as ProviderInstanceId,
        used:
          read.running.filter((agent) => agent.instanceId === id).length +
          starting.filter((entry) => entry.instanceId === id).length +
          reservations.filter((reservation) => reservation.account === id).length,
        max: accountMax(id),
      })),
      freeMemoryMb: Math.floor(memory.availableMemoryBytes / (1024 * 1024)),
    } satisfies SlotUsage;
  }).pipe(Effect.withSpan("SlotGate.usage"));

  return SlotGate.of({
    acquire,
    grant,
    usage,
    attachReservations: Effect.fn("SlotGate.attachReservations")((source: StoredReservations) =>
      Effect.sync(() => {
        stored = source;
      }),
    ),
    drop: Effect.fn("SlotGate.drop")(function* (keys: ReadonlyArray<string>) {
      if (keys.length === 0) return;
      yield* Ref.update(held, (current) => current.filter((entry) => !keys.includes(entry.key)));
    }),
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
