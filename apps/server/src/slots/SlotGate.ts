/**
 * Fork: the agent slot gate on `delegate_task` (`agentSlots`), and on every other route that starts
 * an agent (`fork/agentSlotRoutes.ts`). Off and `fixed`: reads only the
 * settings. `observe`: the capacity rule runs beside each decision (`SlotObserver.ts`). Its own state
 * is memory only; slots held by delivered slot waits come from the wait store (`attachReservations`).
 */
import type {
  OrchestrationV2ThreadShellSnapshot,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import type { SlotRuleStatus, WaitToolFailure } from "@t3tools/contracts/waitFor";
import {
  accountOnlyThreads,
  agentSlotsMode,
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
import * as Cause from "effect/Cause";
import * as Clock from "effect/Clock";
import * as Context from "effect/Context";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as Fiber from "effect/Fiber";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Semaphore from "effect/Semaphore";

import * as HostResources from "../resourceTelemetry/HostResources.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as ActiveRunShells from "./ActiveRunShells.ts";
import { makeSlotObserver } from "./SlotObserver.ts";

/** Names one `starting` entry; `release` or `started` with it. */
export type SlotToken = number;

export type SlotAcquire =
  /**
   * `token` is `null` when the gate is off or has no limit (nothing to release), unless the
   * capacity rule beside it keeps the start itself (`observe` mode).
   */
  | { readonly type: "allow"; readonly token: SlotToken | null }
  | { readonly type: "refuse"; readonly message: string };

/** Slots held by delivered slot waits, as the wait store keeps them (`reservedUntil`). */
export interface StoredReservations {
  /**
   * The live ones; read only while waits are on. Fails when the store (or the settings) cannot be
   * read: that is never an empty store.
   */
  readonly read: Effect.Effect<ReadonlyArray<SlotReservation>, WaitToolFailure>;
  /** The gate let the holder start an agent: clear the wait's `reservedUntil` (a write). */
  readonly used: (key: string) => Effect.Effect<void>;
}

/**
 * The live held slots, one read. `whole: false`: the wait store could not be read, so `held` is the
 * gate's memory only (a lower count, never a good read).
 */
interface HeldRead {
  readonly held: ReadonlyArray<SlotReservation>;
  readonly whole: boolean;
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
   * slot it already held moves to this request once the grant is kept (`keep`), never added to.
   */
  | { readonly type: "granted"; readonly untilMs: number }
  /** An earlier request of the same thread was granted in this call: asked again next time. */
  | { readonly type: "sameThread" }
  | { readonly type: "full"; readonly decision: Extract<SlotDecision, { readonly type: "refuse" }> }
  /** The running agents or the held slots cannot be counted now: never "free". */
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
     * Allow or refuse one new agent on `targetInstanceId`. An allowed call counts as running at
     * once, so concurrent callers never get more slots than are free. `callerThreadId` is the
     * thread that starts it (its own run is not counted, and its held slot is used up), or `null`
     * for a start no thread asks for (a scheduled run, the owner's client, a linked machine).
     */
    readonly acquire: (input: {
      readonly callerThreadId: ThreadId | null;
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
    /**
     * Grants that were written: a slot their thread held before is used up now (memory, and
     * cleared in the store). Until then a re-ask only hides it, so a stop half-way keeps it.
     */
    readonly keep: (keys: ReadonlyArray<string>) => Effect.Effect<void>;
    /**
     * Grants that were not kept (the wait was cancelled, or its write failed) hold nothing; a slot
     * the thread held before counts again.
     */
    readonly drop: (keys: ReadonlyArray<string>) => Effect.Effect<void>;
    /**
     * For `wait_list`: null while slots are off, or when the running agents or the held slots
     * cannot be read.
     */
    readonly usage: Effect.Effect<SlotUsage | null>;
    /** For `wait_list`: the capacity rule beside the gate; null unless the mode is `observe`. */
    readonly observed: Effect.Effect<SlotRuleStatus | null>;
  }
>()("t3/slots/SlotGate") {}

/**
 * The longest the count read may take; after it the read counts as failed (a delegation that needs
 * the count is refused). A normal read takes well under 1 s, even with about 1M stored runs.
 */
export const COUNT_READ_TIMEOUT = Duration.seconds(5);

/** Why the running agents could not be counted: the read failed (or crashed), or took too long. */
export type CountReadProblem = "error" | "timeout";

/**
 * A delegation that needs the count while it cannot be read is refused, never let through on the
 * memory floor alone. Sent with the gate's other refusals (`slots_full`).
 */
export const countUnreadRefusalText = (problem: CountReadProblem): string =>
  "No agent was started: the slot gate could not read the thread list " +
  (problem === "timeout"
    ? `(the read took longer than ${Duration.toSeconds(COUNT_READ_TIMEOUT)} seconds)`
    : "(the read failed with an error)") +
  ", so it cannot count the running agents. Try again in a moment, or do the work yourself.";

/** One count read: the running agents and the threads that show a run, or why it failed. */
type CountRead =
  | {
      readonly type: "read";
      readonly running: ReadonlyArray<RunningAgent>;
      readonly visible: ReadonlySet<string>;
    }
  | { readonly type: "unread"; readonly problem: CountReadProblem };

const make = Effect.gen(function* () {
  const serverSettings = yield* ServerSettings.ServerSettingsService;
  const activeRunShells = yield* ActiveRunShells.ActiveRunShells;
  const hostResources = yield* HostResources.HostResources;
  const semaphore = yield* Semaphore.make(1);
  const entries = yield* Ref.make<ReadonlyArray<StartingEntry>>([]);
  const nextToken = yield* Ref.make<SlotToken>(1);
  /** Slots granted to slot waits (memory only; lost on a restart, which costs one more wait). */
  const held = yield* Ref.make<ReadonlyArray<SlotReservation>>([]);
  /** Keys used up, until their end: the store's copy stops counting even if clearing it failed. */
  const usedKeys = new Map<string, number>();
  /**
   * A re-ask's grant not kept yet, by its key: the thread's earlier holds, hidden (they count only
   * against their own account, see `hiddenHeld`) but not used up, so a grant that is dropped gives
   * them back.
   */
  const replacing = new Map<string, ReadonlyArray<SlotReservation>>();
  const hiddenKeys = () =>
    new Set([...replacing.values()].flatMap((olds) => olds.map((old) => old.key)));
  let stored: StoredReservations | null = null;
  /**
   * Threads started while the wait store could not be read: the next good read uses their stored
   * holds up and clears this. A counted grant needs a good read (or waits off, which clears this),
   * so none comes in between; an uncounted one may, and counts against nothing.
   */
  const unreadStarts = new Set<string>();
  /**
   * Stored holds that a good read used up for `unreadStarts`, not yet cleared in the store. Cleared
   * outside the semaphore by the next acquire, grant or usage (`clearUsedLater`), as `acquire`
   * clears its own; a key leaves only after its write was tried, so a call cut off half-way leaves
   * it for the next one (clearing twice changes nothing). Lost on a restart, like `unreadStarts`.
   */
  const usedLater = new Set<string>();
  /** Why the store last could not be read; warned once until the reason changes or a read works. */
  let storeProblem: string | null = null;

  /**
   * The one count read in flight, joined by every caller that needs the count while it runs.
   * `startedAtMs`: an answer later than COUNT_READ_TIMEOUT after it is no count. `stopped`: stopped
   * at a caller's timeout. Cleared when the read ends, any way, so an ended read refuses no one.
   */
  let inFlight: {
    readonly fiber: Fiber.Fiber<OrchestrationV2ThreadShellSnapshot, unknown>;
    readonly startedAtMs: number;
    stopped: boolean;
  } | null = null;
  /** Check and set of the marker as one step: a scheduler yield in between starts no second read. */
  const startLock = yield* Semaphore.make(1);

  /** The read in flight, or a new one; no interruption in between, so one is never lost. */
  const sharedRead = startLock.withPermits(1)(
    Effect.suspend(() => {
      if (inFlight !== null) return Effect.succeed(inFlight);
      return Effect.gen(function* () {
        const startedAtMs = yield* Clock.currentTimeMillis;
        const fiber = yield* Effect.forkDetach(activeRunShells.read);
        const flight = { fiber, startedAtMs, stopped: false };
        inFlight = flight;
        // Called at once if the read already ended.
        fiber.addObserver(() => {
          if (inFlight === flight) inFlight = null;
        });
        return flight;
      });
    }),
  );

  /**
   * Threads with an active run, or why they cannot be read in time (an empty list is a good read).
   * The read's fiber starts interruptible, so the timeout stops it even in an uninterruptible
   * acquire. A caller cut off while it waits leaves the read to the others; the mask keeps it from
   * being cut off between the fork and the marker (a read the next caller could not see).
   */
  const readCount: Effect.Effect<CountRead> = Effect.uninterruptibleMask((restore) =>
    Effect.gen(function* () {
      const flight = yield* sharedRead;
      const exit = yield* restore(
        Fiber.await(flight.fiber).pipe(Effect.timeout(COUNT_READ_TIMEOUT)),
      ).pipe(
        Effect.catch(() =>
          Effect.sync(() => {
            flight.stopped = true;
            flight.fiber.interruptUnsafe();
            return null;
          }),
        ),
      );
      // An answer later than the limit after the read started (joined from a read whose starter
      // was cut off) is as old as a timed-out one: never a count.
      const late =
        exit !== null &&
        Exit.isSuccess(exit) &&
        (yield* Clock.currentTimeMillis) - flight.startedAtMs >
          Duration.toMillis(COUNT_READ_TIMEOUT);
      if (exit === null || late) {
        yield* Effect.logWarning("agent slots: running agents could not be counted", {
          problem: "timeout",
        });
        return { type: "unread", problem: "timeout" } satisfies CountRead;
      }
      if (Exit.isSuccess(exit)) {
        const threads = [...exit.value.threads, ...exit.value.archivedThreads];
        return {
          type: "read",
          running: runningAgents(threads, null),
          visible: visibleRunThreadIds(threads),
        } satisfies CountRead;
      }
      // A read that ended by an interruption is a failed read too, never the caller's interruption:
      // "timeout" when a caller stopped it at the timeout, "error" when it interrupted itself.
      const problem: CountReadProblem =
        Cause.hasInterruptsOnly(exit.cause) && flight.stopped ? "timeout" : "error";
      // A bug in the count read (a defect, not a failure) is a failed read too.
      yield* Cause.hasDies(exit.cause)
        ? Effect.logWarning("agent slots: counting running agents crashed", { cause: exit.cause })
        : Effect.logWarning("agent slots: running agents could not be counted", {
            problem,
            cause: exit.cause,
          });
      return { type: "unread", problem } satisfies CountRead;
    }),
  );

  /** The count, or `undefined` when it cannot be read (slot waits and `wait_list`: never "free"). */
  const readRunning = readCount.pipe(
    Effect.map((read) => (read.type === "read" ? read : undefined)),
  );

  const observer = yield* makeSlotObserver(readRunning);

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
   * Every live held slot (memory and wait store, one per wait); none while waits are off. A store
   * that cannot be read gives `whole: false` (memory only). Taken now; the store's part is read when
   * the returned effect runs, without the used and hidden keys of now (the rule reads it later).
   */
  const heldAt = Effect.fn("SlotGate.heldAt")(function* (waitsOn: boolean, nowMs: number) {
    for (const [key, untilMs] of usedKeys) if (untilMs <= nowMs) usedKeys.delete(key);
    // A grant never kept nor dropped (a cut-off call) hides the old holds only until they end.
    for (const [key, olds] of replacing) {
      if (olds.every((old) => old.untilMs <= nowMs)) replacing.delete(key);
    }
    const memory = yield* Ref.updateAndGet(held, (current) => liveReservations(current, nowMs));
    if (!waitsOn) {
      // Stored holds do not count while waits are off; a start then never uses them up either.
      unreadStarts.clear();
      storeProblem = null;
      return Effect.succeed<HeldRead>({ held: [], whole: true });
    }
    const skipped = new Set([...usedKeys.keys(), ...hiddenKeys()]);
    const source = stored;
    const live = (reservations: ReadonlyArray<SlotReservation>) =>
      liveReservations(reservations, nowMs).filter((reservation) => !skipped.has(reservation.key));
    return (source === null ? Effect.succeed([]) : source.read).pipe(
      Effect.map((fromStore): HeldRead => {
        // A start allowed while the store was unread uses its thread's stored holds up now.
        for (const reservation of fromStore) {
          if (!unreadStarts.has(reservation.threadId)) continue;
          usedKeys.set(reservation.key, reservation.untilMs);
          usedLater.add(reservation.key);
          skipped.add(reservation.key);
        }
        unreadStarts.clear();
        storeProblem = null;
        return { held: live([...memory, ...fromStore]), whole: true };
      }),
      Effect.catch((cause) => {
        const unread = Effect.succeed<HeldRead>({ held: live(memory), whole: false });
        // Every decision and observation reads the store: one warning per reason, not per read.
        if (cause.message === storeProblem) return unread;
        storeProblem = cause.message;
        return Effect.logWarning("agent slots: the slots held in the wait store cannot be read", {
          cause,
        }).pipe(Effect.andThen(unread));
      }),
    );
  });

  const liveHeld = (waitsOn: boolean, nowMs: number) => Effect.flatten(heldAt(waitsOn, nowMs));

  /**
   * The live holds that re-asks not kept yet hide, of threads other than `threadId`. They still
   * count against their own account (never the machine, where the new hold counts), so a slot
   * that a dropped re-ask gives back is never booked twice. None while waits are off.
   */
  const hiddenHeld = (waitsOn: boolean, nowMs: number, threadId: string) =>
    waitsOn
      ? liveReservations([...replacing.values()].flat(), nowMs).filter(
          (old) => old.threadId !== threadId && !usedKeys.has(old.key),
        )
      : [];

  const freeMemoryBytes = (limits: SlotLimits) =>
    limits.minFreeMemoryBytes === null
      ? Effect.succeed(null)
      : hostResources.read.pipe(Effect.map((sample) => sample.availableMemoryBytes));

  /** Settings that cannot be read count as off, as for the usage pacing gate. */
  const readSettings = serverSettings.getSettings.pipe(Effect.option);

  /** `observe` mode; unreadable settings are `fixed` (nothing is read for the rule then). */
  const observing = readSettings.pipe(
    Effect.map((settings) =>
      Option.isSome(settings) && agentSlotsMode(settings.value.agentSlots) === "observe"
        ? Option.some({
            startGraceMs: settings.value.agentSlots.startGraceSeconds * 1000,
            // Off or without a limit, the gate keeps no records of new starts.
            gateRecords: effectiveSlotLimits(settings.value.agentSlots) !== null,
            waitsOn: settings.value.waitFor.enabled,
          })
        : Option.none(),
    ),
  );

  /**
   * Runs a decision and, in `observe` mode, hands today's answers to the rule beside it with the
   * records as they were before the decision changed them. Forked there: never changed, never
   * delayed (the wait store's held slots are read by the rule, off this path). Inside the
   * semaphore, so observations come in decision order.
   */
  const observeBeside = Effect.fn("SlotGate.observeBeside")(function* <A, E, R>(
    decision: Effect.Effect<A, E, R>,
    answers: (result: A) => ReadonlyArray<"allow" | "refuse">,
    /**
     * `acquire` only. An allow the gate keeps no record of (`untracked`) gets a token of the
     * rule's own, so `started` and `release` reach the rule's record of that start.
     */
    caller?: {
      readonly threadId: string | undefined;
      readonly instanceId: string;
      readonly untracked: (result: A) => boolean;
      readonly withToken: (result: A, token: SlotToken) => A;
    },
  ) {
    const observe = yield* observing;
    if (Option.isNone(observe)) {
      // A decision outside observe mode may grant a hold the rule's notes must never take.
      yield* observer.outside;
      return yield* decision;
    }
    const { startGraceMs, gateRecords, waitsOn } = observe.value;
    const atMs = yield* Clock.currentTimeMillis;
    const starting = yield* Ref.get(entries);
    const heldBefore = yield* heldAt(waitsOn, atMs);
    const result = yield* decision;
    // Only a start the gate did not record: never a token of the gate's own (settings changed
    // between the two reads).
    const ownToken =
      !gateRecords && caller !== undefined && caller.untracked(result)
        ? yield* Ref.getAndUpdate(nextToken, (value) => value + 1)
        : null;
    yield* observer.observe({
      today: answers(result),
      atMs,
      starting,
      held: Effect.map(heldBefore, (read) => (read.whole ? read.held : undefined)),
      callerThreadId: caller?.threadId,
      startGraceMs,
      gateRecords,
      ownStart:
        ownToken === null || caller === undefined
          ? undefined
          : { token: ownToken, instanceId: caller.instanceId, atMs },
    });
    return ownToken === null || caller === undefined ? result : caller.withToken(result, ownToken);
  });

  /** Used up: they stop counting (memory ones go; stored ones are skipped until their end). */
  const useUpAll = Effect.fn("SlotGate.useUpAll")(function* (
    reservations: ReadonlyArray<SlotReservation>,
  ) {
    if (reservations.length === 0) return [];
    for (const reservation of reservations) usedKeys.set(reservation.key, reservation.untilMs);
    const keys = new Set(reservations.map((reservation) => reservation.key));
    yield* Ref.update(held, (current) => current.filter((entry) => !keys.has(entry.key)));
    return [...keys];
  });

  /** The holds of `threadId` that a re-ask not kept yet hides; their entries go. */
  const takeHidden = (threadId: string) => {
    const taken: Array<SlotReservation> = [];
    for (const [key, olds] of replacing) {
      if (!olds.some((old) => old.threadId === threadId)) continue;
      taken.push(...olds);
      replacing.delete(key);
    }
    return taken;
  };

  /**
   * Every slot held for `threadId` (hidden ones too) is used up. Returns their keys, for the store
   * to clear. Under the semaphore.
   */
  const useUp = (reservations: ReadonlyArray<SlotReservation>, threadId: string) =>
    useUpAll([
      ...reservations.filter((reservation) => reservation.threadId === threadId),
      ...takeHidden(threadId),
    ]);

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
   * Clears the stored holds a later good read used up (`usedLater`), outside the semaphore. Each
   * key leaves the set once its write was tried (a failed write is logged; its copy ends by itself).
   * With no store attached nothing is written and the keys still leave: such a hold would count
   * again after a restart until it ends, the safe direction (fewer starts, never more).
   */
  const clearUsedLater = Effect.suspend(() => {
    const keys = [...usedLater];
    return clearStored(keys).pipe(
      Effect.andThen(
        Effect.sync(() => {
          for (const key of keys) usedLater.delete(key);
        }),
      ),
    );
  });

  /** A decision and the caller's held slots it used up (typed whole, for `observeBeside`). */
  const asDecided = (outcome: SlotAcquire, used: ReadonlyArray<string>) => ({ outcome, used });

  /**
   * The decision, under the semaphore, and the caller's held slot it used up (if any). The
   * caller's own held slots never count against it; they are not a pass past a limit either.
   */
  const decide = Effect.fn("SlotGate.decide")(
    function* (input: {
      readonly callerThreadId: ThreadId | null;
      readonly targetInstanceId: ProviderInstanceId;
    }) {
      const settings = yield* readSettings;
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      if (Option.isNone(settings) || limits === null) {
        return asDecided({ type: "allow", token: null }, []);
      }
      const waitsOn = settings.value.waitFor.enabled;
      const nowMs = yield* Clock.currentTimeMillis;
      const counted = limitsCount(limits, input.targetInstanceId);
      const count = counted ? yield* readCount : undefined;
      // Fails closed: a count it needs but cannot read refuses before anything is
      // recorded, so the refused call starts nothing, uses no held slot and writes nothing.
      if (count?.type === "unread") {
        return asDecided({ type: "refuse", message: countUnreadRefusalText(count.problem) }, []);
      }
      const read = count;
      const running = read?.running.filter((agent) => agent.threadId !== input.callerThreadId);
      const kept = yield* keepStarting(read?.visible, nowMs, limits);
      // A store that cannot be read: its holds go uncounted (memory only); the caller's stored
      // holds are used up at the next good read.
      const heldRead = yield* liveHeld(waitsOn, nowMs);
      const reservations = heldRead.held;
      const decision = slotDecision({
        // Read only when a count limit applies (`counted`), so no count means none to check.
        limits,
        running: running ?? [],
        starting: kept,
        reservations: onePerThread(
          reservations.filter((reservation) => reservation.threadId !== input.callerThreadId),
        ),
        accountOnly: hiddenHeld(waitsOn, nowMs, input.callerThreadId ?? ""),
        targetInstanceId: input.targetInstanceId,
        freeMemoryBytes: yield* freeMemoryBytes(limits),
      });
      if (decision.type === "refuse") {
        return asDecided({ type: "refuse", message: slotRefusalText(decision, waitsOn) }, []);
      }
      const token = yield* Ref.getAndUpdate(nextToken, (value) => value + 1);
      yield* Ref.update(entries, (current) => [
        ...current,
        { token, instanceId: input.targetInstanceId, atMs: nowMs },
      ]);
      // The agent it held a slot for starts now: its held slot is used up, with every older copy.
      // A start no thread asks for holds no slot: it uses none up.
      const caller = input.callerThreadId;
      const used = caller === null ? [] : yield* useUp(reservations, caller);
      if (!heldRead.whole && caller !== null) unreadStarts.add(caller);
      return asDecided({ type: "allow", token }, used);
    },
    (decided, input) =>
      observeBeside(decided, (result) => [result.outcome.type], {
        threadId: input.callerThreadId ?? undefined,
        instanceId: input.targetInstanceId,
        untracked: (result) => result.outcome.type === "allow" && result.outcome.token === null,
        withToken: (result, token) => asDecided({ type: "allow", token }, result.used),
      }),
    // One at a time: the next caller sees the entry the last one added.
    semaphore.withPermits(1),
  );

  const acquire = Effect.fn("SlotGate.acquire")(function* (input: {
    readonly callerThreadId: ThreadId | null;
    readonly targetInstanceId: ProviderInstanceId;
  }) {
    const { outcome, used } = yield* decide(input);
    // Cleared in the store after the decision, outside the semaphore; a failed write only means
    // the stored copy ends by itself (it no longer counts here either way).
    yield* clearStored(used);
    yield* clearUsedLater;
    return outcome;
  });

  const grantLocked = Effect.fn("SlotGate.grantLocked")(
    function* (requests: ReadonlyArray<SlotRequest>) {
      const settings = yield* readSettings;
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      if (Option.isNone(settings) || limits === null) {
        return requests.map((): SlotGrant => ({ type: "off" }));
      }
      if (requests.length === 0) return [];
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
      const heldRead = yield* liveHeld(settings.value.waitFor.enabled, nowMs);
      let reservations = heldRead.held;
      const grants: Array<SlotGrant> = [];
      const grantedThreads = new Set<string>();
      for (const request of requests) {
        // One wake per thread in a call: its other slot waits ask again next time.
        if (grantedThreads.has(request.threadId)) {
          grants.push({ type: "sameThread" });
          continue;
        }
        const own = limitsFor(request);
        if ((read === undefined || !heldRead.whole) && limitsCount(own, request.account ?? "")) {
          grants.push({
            type: "unread",
            message:
              read === undefined
                ? "The running agents cannot be counted now."
                : "The held slots cannot be counted now.",
          });
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
          // Read again for each request: a grant earlier in this call may have hidden holds.
          accountOnly: hiddenHeld(settings.value.waitFor.enabled, nowMs, request.threadId),
          targetInstanceId: request.account ?? "",
          freeMemoryBytes: memoryBytes,
        });
        if (decision.type === "refuse") {
          // Refused: a slot it already holds stays as it is.
          grants.push({ type: "full", decision });
          continue;
        }
        // Asking again moves its hold but never extends it: a thread that keeps asking without
        // starting an agent loses the slot at the old end, so an older waiter gets it (R17).
        const heldUntil = reservations
          .filter((reservation) => reservation.threadId === request.threadId)
          .reduce<number | null>(
            (latest, reservation) =>
              latest === null ? reservation.untilMs : Math.max(latest, reservation.untilMs),
            null,
          );
        const untilMs =
          heldUntil === null ? nowMs + reserveMs : Math.min(nowMs + reserveMs, heldUntil);
        const reservation: SlotReservation = {
          key: request.key,
          threadId: request.threadId,
          ...(request.account === undefined ? {} : { account: request.account }),
          untilMs,
        };
        // The slot it held before (memory or store) is replaced by this one once it is kept; until
        // then it is hidden, so a cancelled or unwritten re-ask gives it back.
        const olds = [
          ...reservations.filter((entry) => entry.threadId === request.threadId),
          ...takeHidden(request.threadId),
        ];
        if (olds.length > 0) replacing.set(request.key, olds);
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
      return grants;
    },
    // Only real answers: "off" has nothing to wait for, "sameThread" asks again next time.
    (granted) =>
      observeBeside(granted, (grants) =>
        grants.flatMap((answer) =>
          answer.type === "granted"
            ? ["allow" as const]
            : answer.type === "full" || answer.type === "unread"
              ? ["refuse" as const]
              : [],
        ),
      ),
    // Under the same semaphore as `acquire`: a delegation never takes a slot being granted.
    semaphore.withPermits(1),
  );

  const grant = Effect.fn("SlotGate.grant")(function* (requests: ReadonlyArray<SlotRequest>) {
    const grants = yield* grantLocked(requests);
    yield* clearUsedLater;
    return grants;
  });

  const keepLocked = Effect.fn("SlotGate.keepLocked")(function* (keys: ReadonlyArray<string>) {
    const replaced: Array<string> = [];
    for (const key of keys) {
      const olds = replacing.get(key);
      if (olds === undefined) continue;
      replacing.delete(key);
      replaced.push(...(yield* useUpAll(olds)));
    }
    return replaced;
  }, semaphore.withPermits(1));

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
    const heldRead = yield* liveHeld(settings.value.waitFor.enabled, nowMs);
    // Not under the semaphore: a hold this read used up is cleared in the store here.
    yield* clearUsedLater;
    if (!heldRead.whole) return null;
    const reservations = onePerThread(heldRead.held);
    // Holds hidden by a re-ask not kept yet: on their own account only, as `decide` counts them.
    const hidden = hiddenHeld(settings.value.waitFor.enabled, nowMs, "");
    const accountMax = (id: string) => {
      const max = agentSlots.maxPerAccount[id as ProviderInstanceId];
      return max === undefined || max <= 0 ? null : max;
    };
    const ids = new Set<string>([
      ...Object.keys(agentSlots.maxPerAccount).filter((id) => accountMax(id) !== null),
      ...read.running.map((agent) => agent.instanceId),
      ...starting.map((entry) => entry.instanceId),
      ...[...reservations, ...hidden].flatMap((reservation) =>
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
          reservations.filter((reservation) => reservation.account === id).length +
          accountOnlyThreads(reservations, hidden, id),
        max: accountMax(id),
      })),
      freeMemoryMb: Math.floor(memory.availableMemoryBytes / (1024 * 1024)),
    } satisfies SlotUsage;
  }).pipe(Effect.withSpan("SlotGate.usage"));

  return SlotGate.of({
    acquire,
    grant,
    usage,
    observed: observing.pipe(
      Effect.flatMap((observe) =>
        Option.isSome(observe) ? observer.status : Effect.succeed(null),
      ),
    ),
    attachReservations: Effect.fn("SlotGate.attachReservations")((source: StoredReservations) =>
      Effect.sync(() => {
        stored = source;
      }),
    ),
    // A replaced slot in the store is cleared like a used one, outside the semaphore.
    keep: Effect.fn("SlotGate.keep")(function* (keys: ReadonlyArray<string>) {
      yield* clearStored(yield* keepLocked(keys));
    }),
    drop: Effect.fn("SlotGate.drop")(function* (keys: ReadonlyArray<string>) {
      if (keys.length === 0) return;
      for (const key of keys) replacing.delete(key);
      yield* Ref.update(held, (current) => current.filter((entry) => !keys.includes(entry.key)));
    }),
    // A token is the gate's or the capacity rule's (`observeBeside`); each ignores the other's.
    started: Effect.fn("SlotGate.started")((token: SlotToken | null, childThreadId: ThreadId) =>
      token === null
        ? Effect.void
        : Ref.update(entries, (current) =>
            current.map((entry) => (entry.token === token ? { ...entry, childThreadId } : entry)),
          ).pipe(Effect.andThen(observer.started(token, childThreadId))),
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
      yield* observer.released(token, nowMs);
    }),
  });
});

/** The gate over a given read of the running agents (tests give their own). */
export const layerOver = Layer.effect(SlotGate, make);

/**
 * The server's gate, reading the running agents through `ActiveRunShells`. One constant, so
 * runtimeLayer.ts and server.ts share one gate (layers memoize by reference).
 */
export const layer = layerOver.pipe(Layer.provide(ActiveRunShells.layer));
