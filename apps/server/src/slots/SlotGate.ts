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
  countedStarting,
  effectiveSlotLimits,
  limitsCount,
  liveReservations,
  liveStarting,
  onePerThread,
  pendingWakes,
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
  /** `message` is the text for an agent that asked (`slots_full`); `refusal` says why, for other routes. */
  | { readonly type: "refuse"; readonly message: string; readonly refusal: SlotRefusal };

/** Why `acquire` refused. */
export type SlotRefusal =
  /**
   * A limit is reached. `batch`: the call asked for more than one start (`alsoNeeds`); `free` of
   * the `needed` slots are free. `waitsOn`: a slot wait can be registered.
   */
  | {
      readonly type: "full";
      readonly decision: Extract<SlotDecision, { readonly type: "refuse" }>;
      readonly waitsOn: boolean;
      readonly batch?: { readonly needed: number; readonly free: number } | undefined;
    }
  /** The running agents could not be counted. */
  | { readonly type: "unread"; readonly problem: CountReadProblem };

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
  /** Keys of holds whose wait was delivered (the store's): their thread was woken already. */
  readonly woken: ReadonlySet<string>;
}

/** One slot wait asking for a slot; `key` is its wait id. */
export interface SlotRequest {
  readonly key: string;
  readonly threadId: ThreadId;
  readonly account?: ProviderInstanceId | undefined;
  /**
   * The thread asks from its own run and is answered at once (a slot wait met when registered):
   * it needs no wake-up, so its hold counts no wake-up still to start (`pendingWakes`).
   */
  readonly awake?: boolean | undefined;
  /** When the wait was made: its place in the gate's one line (`SlotPlace`); else now. */
  readonly sinceMs?: number | undefined;
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

/**
 * A start that asks again while it is refused (a wake-up of an idle thread): its place in the
 * gate's one line. Its first ask joins the line; every later ask keeps that place, so a slot that
 * frees goes to the oldest waiter, never to whoever asks first. It leaves when it is allowed, by
 * `leave`, or when it has not asked for `staleAfter` (default `LINE_STALE`).
 */
export interface SlotPlace {
  readonly key: string;
  readonly staleAfter?: Duration.Input | undefined;
}

/** A waiter that has not asked again for this long has left the line (6 asks of 5 s). */
const LINE_STALE = Duration.seconds(30);

export interface SlotAcquireInput {
  readonly callerThreadId: ThreadId | null;
  readonly targetInstanceId: ProviderInstanceId;
  /**
   * More starts the caller makes right after this one (their instances, one entry each): allowed
   * only when all of them would be too. Only this start is taken; the others take their own later.
   */
  readonly alsoNeeds?: ReadonlyArray<ProviderInstanceId> | undefined;
  /**
   * A message into this existing thread. Shown with a run, it only queues: allowed, and counted
   * only while the thread shows no run (its run ended first; `queuedInto`). Idle, it is a start.
   * Unread, it refuses only where a count limit applies.
   */
  readonly intoThreadId?: ThreadId | undefined;
  /**
   * The caller is the woken thread itself (a wait's message): its held slots are not counted
   * against it and are not used up, so the agent it wakes up for still has its slot.
   */
  readonly keepsHeld?: boolean | undefined;
  /**
   * Its place in the line, for a start that asks again while refused. Without one the start asks
   * from the back of the line: it never takes a slot a waiter in line is owed.
   */
  readonly place?: SlotPlace | undefined;
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
    readonly acquire: (input: SlotAcquireInput) => Effect.Effect<SlotAcquire>;
    /** The dispatch named the child: its entry ends as soon as the child shows a run. */
    readonly started: (token: SlotToken | null, childThreadId: ThreadId) => Effect.Effect<void>;
    /**
     * The dispatch ended, whichever way (call it from a finalizer). Without a started child the
     * slot is free again at once; a started child that shows no run yet still counts for the
     * start grace, and so does a message queued into a running thread (`intoThreadId`), while
     * that thread shows no run. Calling it again does nothing more.
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
    /** Waiters that no longer need a slot (gone, answered, stopped) leave the line now. */
    readonly leave: (keys: ReadonlyArray<string>) => Effect.Effect<void>;
    /**
     * Every slot wait still waiting, by key (the wait service's whole list, at each sweep): any
     * other slot wait in the line ended (met, cancelled, handed over) and leaves it.
     */
    readonly slotWaitsWaiting: (keys: ReadonlyArray<string>) => Effect.Effect<void>;
    /** The line's keys, oldest first (waiters that went stale are left out). */
    readonly line: Effect.Effect<ReadonlyArray<string>>;
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

/** One waiter in the line. `instanceId` null: a slot wait with no account (the machine only). */
interface LineEntry {
  readonly key: string;
  /** A route's wake-up, or a slot wait (granted only on a whole read of the held slots). */
  readonly kind: "wake" | "slotWait";
  readonly threadId: string;
  readonly instanceId: string | null;
  readonly sinceMs: number;
  /** Joins at the same time keep their order. */
  readonly order: number;
  lastAskMs: number;
  staleMs: number;
}

const staleMsOf = (place: SlotPlace) =>
  Duration.toMillis(Duration.fromInputUnsafe(place.staleAfter ?? LINE_STALE));

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
  /** Holds granted to a thread that asked from its own run (`awake`), by key, until their end. */
  const awakeKeys = new Map<string, number>();
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
   * The one line of starts that wait for a slot, by key: every route's waiter (`SlotPlace`) and
   * every slot wait (`grant`), oldest first. Memory only: after a restart each waiter joins again
   * at its next ask (a slot wait at its own age).
   */
  const line = new Map<string, LineEntry>();
  let lineOrder = 0;

  /** Joins the line, or keeps the place it has (asked again). */
  const joinLine = (entry: {
    readonly key: string;
    readonly kind: LineEntry["kind"];
    readonly threadId: string;
    readonly instanceId: string | null;
    readonly sinceMs: number;
    readonly nowMs: number;
    readonly staleMs: number;
  }): LineEntry => {
    const seen = line.get(entry.key);
    if (seen !== undefined) {
      seen.lastAskMs = entry.nowMs;
      seen.staleMs = entry.staleMs;
      return seen;
    }
    const joined: LineEntry = {
      key: entry.key,
      kind: entry.kind,
      threadId: entry.threadId,
      instanceId: entry.instanceId,
      sinceMs: entry.sinceMs,
      order: lineOrder++,
      lastAskMs: entry.nowMs,
      staleMs: entry.staleMs,
    };
    line.set(entry.key, joined);
    return joined;
  };

  /** The live line, oldest first; a waiter that stopped asking has left it. */
  const lineInOrder = (nowMs: number) => {
    for (const [key, entry] of line) {
      if (nowMs - entry.lastAskMs > entry.staleMs) line.delete(key);
    }
    return [...line.values()].sort(
      (left, right) => left.sinceMs - right.sinceMs || left.order - right.order,
    );
  };

  /**
   * The waiters ahead of a start, oldest first, as starts: each that would get a slot now keeps it
   * from those behind; one that would not (its account full) holds none. Left out: the asker's own
   * thread, a thread that shows a run (it only queues), and one counted already (held, or named).
   */
  const lineAhead = (input: {
    readonly ahead: ReadonlyArray<LineEntry>;
    readonly threadId: string | null;
    readonly limits: SlotLimits;
    readonly running: ReadonlyArray<RunningAgent>;
    readonly visible: ReadonlySet<string>;
    readonly kept: ReadonlyArray<StartingEntry>;
    readonly held: HeldRead;
    readonly accountOnly: (threadId: string) => ReadonlyArray<SlotReservation>;
    readonly freeMemoryBytes: number | null;
    readonly nowMs: number;
  }): ReadonlyArray<StartingEntry> => {
    const counted = new Set<string>([
      ...input.held.held.map((reservation) => reservation.threadId),
      ...input.kept.flatMap((entry) =>
        [entry.childThreadId, entry.queuedInto].filter((id) => id !== undefined),
      ),
    ]);
    if (input.threadId !== null) counted.add(input.threadId);
    const taken: Array<StartingEntry> = [];
    for (const entry of input.ahead) {
      if (counted.has(entry.threadId) || input.visible.has(entry.threadId)) continue;
      // A slot wait is granted only when the held slots read whole: until then it takes none.
      if (entry.kind === "slotWait" && !input.held.whole) continue;
      counted.add(entry.threadId);
      const others = onePerThread(
        input.held.held.filter((reservation) => reservation.threadId !== entry.threadId),
      );
      const decision = slotDecision({
        limits: entry.instanceId === null ? { ...input.limits, account: () => null } : input.limits,
        running: input.running.filter((agent) => agent.threadId !== entry.threadId),
        starting: [
          ...input.kept,
          ...pendingWakes(others, input.visible, input.kept, input.held.woken),
          ...taken,
        ],
        reservations: others,
        accountOnly: input.accountOnly(entry.threadId),
        targetInstanceId: entry.instanceId ?? "",
        freeMemoryBytes: input.freeMemoryBytes,
      });
      if (decision.type === "allow") {
        taken.push({
          token: -10_000 - taken.length,
          instanceId: entry.instanceId ?? "",
          atMs: input.nowMs,
          childThreadId: entry.threadId,
        });
      }
    }
    return taken;
  };

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
  /**
   * The problem last logged while the count stays unreadable, so a count unreadable for hours logs
   * once (and once more when its problem changes or it reads again), not on every read.
   */
  let unreadLogged: "timeout" | "error" | "crash" | null = null;
  const logUnread = (problem: "timeout" | "error" | "crash", log: Effect.Effect<void>) =>
    Effect.suspend(() => {
      if (unreadLogged === problem) return Effect.void;
      unreadLogged = problem;
      return log;
    });
  const logReadAgain = Effect.suspend(() => {
    if (unreadLogged === null) return Effect.void;
    unreadLogged = null;
    return Effect.logInfo("agent slots: running agents can be counted again");
  });

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
        yield* logUnread(
          "timeout",
          Effect.logWarning("agent slots: running agents could not be counted", {
            problem: "timeout",
          }),
        );
        return { type: "unread", problem: "timeout" } satisfies CountRead;
      }
      if (Exit.isSuccess(exit)) {
        yield* logReadAgain;
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
        ? logUnread(
            "crash",
            Effect.logWarning("agent slots: counting running agents crashed", {
              cause: exit.cause,
            }),
          )
        : logUnread(
            problem,
            Effect.logWarning("agent slots: running agents could not be counted", {
              problem,
              cause: exit.cause,
            }),
          );
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
    for (const [key, untilMs] of awakeKeys) if (untilMs <= nowMs) awakeKeys.delete(key);
    // A grant never kept nor dropped (a cut-off call) hides the old holds only until they end.
    for (const [key, olds] of replacing) {
      if (olds.every((old) => old.untilMs <= nowMs)) replacing.delete(key);
    }
    const memory = yield* Ref.updateAndGet(held, (current) => liveReservations(current, nowMs));
    if (!waitsOn) {
      // Stored holds do not count while waits are off; a start then never uses them up either.
      unreadStarts.clear();
      storeProblem = null;
      return Effect.succeed<HeldRead>({ held: [], whole: true, woken: new Set() });
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
        return {
          held: live([...memory, ...fromStore]),
          whole: true,
          woken: new Set([...awakeKeys.keys(), ...fromStore.map((reservation) => reservation.key)]),
        };
      }),
      Effect.catch((cause) => {
        // Unread: every hold in memory counts its wake-up as still to start (fewer starts, never more).
        const unread = Effect.succeed<HeldRead>({
          held: live(memory),
          whole: false,
          woken: new Set(awakeKeys.keys()),
        });
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
    function* (input: SlotAcquireInput) {
      const settings = yield* readSettings;
      const limits = Option.isNone(settings)
        ? null
        : effectiveSlotLimits(settings.value.agentSlots);
      const place = input.place;
      if (Option.isNone(settings) || limits === null) {
        if (place !== undefined) line.delete(place.key);
        return asDecided({ type: "allow", token: null }, []);
      }
      const waitsOn = settings.value.waitFor.enabled;
      const nowMs = yield* Clock.currentTimeMillis;
      const counted = limitsCount(limits, input.targetInstanceId);
      const into = input.intoThreadId;
      const askingThread = into ?? input.callerThreadId;
      // A waiter keeps its place from its first ask, refused or not.
      const own =
        place === undefined || askingThread === null
          ? undefined
          : joinLine({
              key: place.key,
              kind: "wake",
              threadId: askingThread,
              instanceId: input.targetInstanceId,
              sinceMs: nowMs,
              nowMs,
              staleMs: staleMsOf(place),
            });
      const allowed = (outcome: SlotAcquire, used: ReadonlyArray<string>) => {
        if (place !== undefined) line.delete(place.key);
        return asDecided(outcome, used);
      };
      const count = counted || into !== undefined ? yield* readCount : undefined;
      // Fails closed: a count it needs but cannot read refuses before anything is
      // recorded, so the refused call starts nothing, uses no held slot and writes nothing.
      if (count?.type === "unread" && counted) {
        return asDecided(
          {
            type: "refuse",
            message: countUnreadRefusalText(count.problem),
            refusal: { type: "unread", problem: count.problem },
          },
          [],
        );
      }
      const shown = count?.type === "read" ? count : undefined;
      // A message into a thread that shows a run only queues: it starts no agent now.
      if (into !== undefined && shown?.visible.has(into) === true) {
        yield* keepStarting(shown.visible, nowMs, limits);
        const token = yield* Ref.getAndUpdate(nextToken, (value) => value + 1);
        yield* Ref.update(entries, (current) => [
          ...current,
          { token, instanceId: input.targetInstanceId, atMs: nowMs, queuedInto: into },
        ]);
        return allowed({ type: "allow", token }, []);
      }
      // The running agents only where a count limit applies (no count means none to check).
      const read = counted ? shown : undefined;
      const running = read?.running.filter((agent) => agent.threadId !== input.callerThreadId);
      const visible = shown?.visible ?? new Set<string>();
      const kept = countedStarting(yield* keepStarting(shown?.visible, nowMs, limits), visible);
      // A store that cannot be read: its holds go uncounted (memory only); the caller's stored
      // holds are used up at the next good read.
      const heldRead = yield* liveHeld(waitsOn, nowMs);
      const reservations = heldRead.held;
      const others = onePerThread(
        reservations.filter((reservation) => reservation.threadId !== input.callerThreadId),
      );
      // Other held threads not woken yet: each wake-up is a run of its own.
      const wakes = pendingWakes(others, visible, kept, heldRead.woken);
      const memoryBytes = yield* freeMemoryBytes(limits);
      // The waiters ahead of it in the line (all of them for a start without a place) come first.
      const ordered = read === undefined ? [] : lineInOrder(nowMs);
      const ahead =
        read === undefined
          ? []
          : lineAhead({
              ahead: own === undefined ? ordered : ordered.slice(0, ordered.indexOf(own)),
              threadId: askingThread,
              limits,
              running: read.running,
              visible,
              kept,
              held: heldRead,
              accountOnly: (threadId) => hiddenHeld(waitsOn, nowMs, threadId),
              freeMemoryBytes: memoryBytes,
              nowMs,
            });
      const decisionWith = (
        targetInstanceId: ProviderInstanceId,
        starting: ReadonlyArray<StartingEntry>,
      ) =>
        slotDecision({
          // Read only when a count limit applies (`counted`), so no count means none to check.
          limits,
          running: running ?? [],
          starting,
          reservations: others,
          accountOnly: hiddenHeld(waitsOn, nowMs, input.callerThreadId ?? ""),
          targetInstanceId,
          freeMemoryBytes: memoryBytes,
        });
      const decision = decisionWith(input.targetInstanceId, [...kept, ...wakes, ...ahead]);
      const alsoNeeds = input.alsoNeeds ?? [];
      const full = (
        refused: Extract<SlotDecision, { readonly type: "refuse" }>,
        free: number,
      ): SlotAcquire => ({
        type: "refuse",
        message: slotRefusalText(refused, waitsOn),
        refusal: {
          type: "full",
          decision: refused,
          waitsOn,
          ...(alsoNeeds.length === 0 ? {} : { batch: { needed: alsoNeeds.length + 1, free } }),
        },
      });
      if (decision.type === "refuse") return asDecided(full(decision, 0), []);
      // The later starts of the batch, counted as starting one after another (nothing recorded).
      const starting: Array<StartingEntry> = [
        ...kept,
        ...wakes,
        ...ahead,
        { token: -1, instanceId: input.targetInstanceId, atMs: nowMs },
      ];
      for (const [index, instanceId] of alsoNeeds.entries()) {
        const next = decisionWith(instanceId, starting);
        if (next.type === "refuse") return asDecided(full(next, index + 1), []);
        starting.push({ token: -2 - index, instanceId, atMs: nowMs });
      }
      const token = yield* Ref.getAndUpdate(nextToken, (value) => value + 1);
      // A message into an idle thread names that thread at once, so no other caller counts its
      // wake-up again (`pendingWakes`) before `started` runs.
      yield* Ref.update(entries, (current) => [
        ...current,
        {
          token,
          instanceId: input.targetInstanceId,
          atMs: nowMs,
          ...(into === undefined ? {} : { childThreadId: into }),
        },
      ]);
      // The agent it held a slot for starts now: its held slot is used up, with every older copy.
      // A start no thread asks for holds no slot: it uses none up. A woken thread keeps its own.
      const caller = input.keepsHeld === true ? null : input.callerThreadId;
      const used = caller === null ? [] : yield* useUp(reservations, caller);
      if (!heldRead.whole && caller !== null) unreadStarts.add(caller);
      return allowed({ type: "allow", token }, used);
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

  const acquire = Effect.fn("SlotGate.acquire")(function* (input: SlotAcquireInput) {
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
      const kept = countedStarting(
        yield* keepStarting(read?.visible, nowMs, limits),
        read?.visible ?? new Set(),
      );
      const memoryBytes = yield* freeMemoryBytes(limits);
      const reserveMs = settings.value.agentSlots.reserveSeconds * 1000;
      const heldRead = yield* liveHeld(settings.value.waitFor.enabled, nowMs);
      let reservations = heldRead.held;
      const grants: Array<SlotGrant> = [];
      const grantedThreads = new Set<string>();
      // Every slot wait is in the line at its own age; one the sweep stopped asking for leaves it.
      const staleMs = Math.max(
        Duration.toMillis(LINE_STALE),
        3 * settings.value.waitFor.unitSweepSeconds * 1000,
      );
      const places = requests.map((request) =>
        joinLine({
          key: request.key,
          kind: "slotWait",
          threadId: request.threadId,
          instanceId: request.account ?? null,
          sinceMs: request.sinceMs ?? nowMs,
          nowMs,
          staleMs,
        }),
      );
      for (const [index, request] of requests.entries()) {
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
        const others = onePerThread(
          reservations.filter((reservation) => reservation.threadId !== request.threadId),
        );
        const visible = read?.visible ?? new Set<string>();
        // The waiters ahead of it in the line (route wake-ups and older slot waits) come first.
        const ordered = read === undefined ? [] : lineInOrder(nowMs);
        const place = places[index];
        const ahead =
          read === undefined || place === undefined || !ordered.includes(place)
            ? []
            : lineAhead({
                ahead: ordered.slice(0, ordered.indexOf(place)),
                threadId: request.threadId,
                // Each waiter ahead by its own limits (its account's too), not this wait's.
                limits,
                running: read.running,
                visible,
                kept,
                held: { ...heldRead, held: reservations },
                accountOnly: (threadId) =>
                  hiddenHeld(settings.value.waitFor.enabled, nowMs, threadId),
                freeMemoryBytes: memoryBytes,
                nowMs,
              });
        const decision = slotDecision({
          limits: own,
          running: (read?.running ?? []).filter((agent) => agent.threadId !== request.threadId),
          // Every other held thread not woken yet needs a slot for its wake-up as well as its
          // agent, the ones granted earlier in this call too. Its own wake-up is
          // not counted, as a thread's own run never counts against its own start.
          starting: [...kept, ...pendingWakes(others, visible, kept, heldRead.woken), ...ahead],
          // Its own held slot is not counted: a grant moves it here, so the thread keeps one.
          reservations: others,
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
        if (request.awake === true) awakeKeys.set(request.key, untilMs);
        line.delete(request.key);
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
        : countedStarting(
            liveStarting(yield* Ref.get(entries), read.visible, nowMs, limits.startGraceMs),
            read.visible,
          );
    const heldRead = yield* liveHeld(settings.value.waitFor.enabled, nowMs);
    // Not under the semaphore: a hold this read used up is cleared in the store here.
    yield* clearUsedLater;
    if (!heldRead.whole) return null;
    const reservations = onePerThread(heldRead.held);
    // Wake-ups still to start: on the machine only, as `decide` counts them.
    const wakes = pendingWakes(reservations, read.visible, starting, heldRead.woken).length;
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
        used: read.running.length + starting.length + reservations.length + wakes,
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
    leave: Effect.fn("SlotGate.leave")((keys: ReadonlyArray<string>) =>
      Effect.sync(() => {
        for (const key of keys) line.delete(key);
      }),
    ),
    slotWaitsWaiting: Effect.fn("SlotGate.slotWaitsWaiting")((keys: ReadonlyArray<string>) =>
      Effect.sync(() => {
        const waiting = new Set(keys);
        for (const [key, entry] of line) {
          if (entry.kind === "slotWait" && !waiting.has(key)) line.delete(key);
        }
      }),
    ),
    line: Clock.currentTimeMillis.pipe(
      Effect.map((nowMs) => lineInOrder(nowMs).map((entry) => entry.key)),
    ),
    drop: Effect.fn("SlotGate.drop")(function* (keys: ReadonlyArray<string>) {
      if (keys.length === 0) return;
      for (const key of keys) {
        replacing.delete(key);
        awakeKeys.delete(key);
      }
      yield* Ref.update(held, (current) => current.filter((entry) => !keys.includes(entry.key)));
    }),
    // A token is the gate's or the capacity rule's (`observeBeside`); each ignores the other's.
    started: Effect.fn("SlotGate.started")((token: SlotToken | null, childThreadId: ThreadId) =>
      token === null
        ? Effect.void
        : Ref.update(entries, (current) =>
            current.map((entry) =>
              // A queued message is tied to its thread already and ends by time only.
              entry.token === token && entry.queuedInto === undefined
                ? { ...entry, childThreadId }
                : entry,
            ),
          ).pipe(Effect.andThen(observer.started(token, childThreadId))),
    ),
    release: Effect.fn("SlotGate.release")(function* (token: SlotToken | null) {
      if (token === null) return;
      const nowMs = yield* Clock.currentTimeMillis;
      yield* Ref.update(entries, (current) =>
        current.flatMap((entry) =>
          entry.token !== token
            ? [entry]
            : entry.childThreadId === undefined && entry.queuedInto === undefined
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
