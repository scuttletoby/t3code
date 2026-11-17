/**
 * Fork: the capacity rule beside the slot gate (`agentSlots.mode: "observe"`). Reads the machine off
 * the decision path (forked, one at a time, time-limited) and records what the rule would answer;
 * never changes or delays an answer. Memory only: a restart starts at "elevated" (a calm wait).
 */
import type { SlotRuleStatus } from "@t3tools/contracts/waitFor";
import { AgentScope } from "@t3tools/shared/AgentScope";
import {
  DISPATCH_SAFETY_MS,
  liveReservations,
  liveStarting,
  onePerThread,
  type SlotReservation,
  type StartingEntry,
} from "@t3tools/shared/agentSlots";
import {
  CAPACITY_RULE,
  type CapacityFacts,
  type CapacityPiece,
} from "@t3tools/shared/capacityRule";
import {
  capacityFactsOf,
  nodeCapacityHost,
  readBoxPsi,
  readMachine,
  readPieceDemand,
  type CapacityHost,
} from "@t3tools/shared/capacityReadings";
import * as HostProcess from "@t3tools/shared/HostProcess";
import {
  OBSERVE_START_PRESSURE,
  observeSlotAnswers,
  slotRuleObservation,
  type SlotAnswer,
  type SlotReadings,
} from "@t3tools/shared/slotObserve";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FiberSet from "effect/FiberSet";
import * as Option from "effect/Option";
import * as Ref from "effect/Ref";
import * as Semaphore from "effect/Semaphore";

/** The machine the rule reads; `null` is the real one. Tests put a fake here. */
export const SlotCapacityHost = Context.Reference<CapacityHost | null>(
  "t3/slots/SlotCapacityHost",
  {
    defaultValue: () => null,
  },
);

/** The longest the readings may take; after it the rule sees nothing ("unreadable", elevated). */
export const OBSERVE_READ_TIMEOUT = Duration.seconds(2);
/** Observations waiting for the one before; more are skipped and counted. */
export const OBSERVE_MAX_WAITING = 4;
/** Reads still hanging after their time limit; while this many hang no new read starts. */
export const OBSERVE_MAX_HANGING = 2;

/** One slot decision, as the gate saw it before it changed its records. */
export interface SlotObserveInput {
  readonly today: ReadonlyArray<SlotAnswer>;
  readonly atMs: number;
  readonly starting: ReadonlyArray<StartingEntry>;
  /**
   * The live held slots as the gate counts them: memory as of the decision, the wait store as of
   * when the observation runs (read in the observer's fiber, so a slow store never delays the
   * decision). An observation that waits behind others can miss a hold a later decision used up
   * meanwhile, or count one granted after its decision: one agent off, for a few seconds.
   */
  readonly held: Effect.Effect<ReadonlyArray<SlotReservation>>;
  /** `acquire`'s caller: its own held slot is used up by the start it asks for (counted once). */
  readonly callerThreadId?: string | undefined;
  readonly startGraceMs: number;
  /** The gate keeps `starting` (slots on with a limit); else the rule keeps its own allows. */
  readonly gateRecords: boolean;
  /**
   * While the gate keeps no records: the allowed start, under a token of its own that the gate's
   * `started` and `release` hand on (`started`, `released` here).
   */
  readonly ownStart?: StartingEntry | undefined;
}

/** systemd nests `a-b-c.slice` as `a.slice/a-b.slice/a-b-c.slice`. */
export function slicePath(slice: string): string {
  const parts = slice.replace(/\.slice$/, "").split("-");
  return parts.map((_, index) => `/${parts.slice(0, index + 1).join("-")}.slice`).join("");
}

interface OwnPiece {
  readonly scope: { readonly unit: string; readonly slice: string } | undefined;
}

/**
 * `readRunning`: the gate's projection read (every thread with an active run), `undefined` when
 * it failed; then the pieces are unknown and the rule answers "unreadable".
 */
export const makeSlotObserver = Effect.fn("makeSlotObserver")(function* (
  readRunning: Effect.Effect<{ readonly visible: ReadonlySet<string> } | undefined>,
) {
  const host = (yield* SlotCapacityHost) ?? nodeCapacityHost(yield* HostProcess.Platform);
  const uid = yield* HostProcess.UserId;
  // The systemd user manager's cgroup; agent scopes live below it. A wrong guess only reads as
  // "scope gone", which keeps the piece at its full cost.
  const userRoot = uid === undefined ? null : `/user.slice/user-${uid}.slice/user@${uid}.service`;
  const agentScope = yield* AgentScope;
  const kind = CAPACITY_RULE.unknownKinds.other;
  const fibers = yield* FiberSet.make<void>();
  const lock = yield* Semaphore.make(1);
  const pressure = yield* Ref.make(OBSERVE_START_PRESSURE);
  const status = yield* Ref.make<SlotRuleStatus>({
    mode: "observe",
    last: null,
    observed: 0,
    disagreed: 0,
    skipped: 0,
  });
  let waiting = 0;
  let hanging = 0;
  /**
   * While the gate keeps no records (slots off), the agents allowed lately, kept as the gate keeps
   * its own (`liveStarting`): each counts until its dispatch ends, then for `startGraceMs` unless
   * its child shows a run (then it counts as that run, once). A dispatch that ends with no child
   * stops counting at once.
   */
  let ownStarts: ReadonlyArray<StartingEntry> = [];
  /**
   * While the gate keeps no records: held slots of the threads those starts were for, by key, until
   * they end. A start uses its thread's hold up, as the gate's `useUp` does with slots on (with slots
   * off the gate leaves it in the store), so the start and the hold never both count, not even once
   * the start stops counting (its child shows its run, or its dispatch ends).
   */
  const usedHolds = new Map<string, number>();
  /**
   * Slots off: threads allowed a start, noted at the decision (before any skip). The next good store
   * read uses their holds up, whatever became of the start meanwhile, and drops the notes. Slots on
   * drops them all; a note no read took goes after `DISPATCH_SAFETY_MS`.
   */
  const startedThreads = new Map<string, number>();

  const readFacts = async (pieces: ReadonlyArray<OwnPiece>): Promise<CapacityFacts> => {
    const machine = await readMachine(host);
    const read: Array<CapacityPiece> = [];
    let box: string | null = null;
    for (const piece of pieces) {
      const path =
        piece.scope === undefined || userRoot === null
          ? null
          : `${userRoot}${slicePath(piece.scope.slice)}`;
      if (path === null || piece.scope === undefined) {
        read.push({ kind, demandMb: null });
        continue;
      }
      const demand = await readPieceDemand(host, machine.cgroup, `${path}/${piece.scope.unit}`);
      // The gate's records say this agent runs, so a scope that reads as gone keeps the piece,
      // owing its full cost. Only the records end a piece.
      read.push({ kind, demandMb: demand.gone ? null : demand.demandMb });
      if (!demand.gone) box = path;
    }
    // A box with no live piece of its own passes no box pressure.
    const boxPsi = box === null ? null : await readBoxPsi(host, machine, box);
    return capacityFactsOf(machine, { pieceCaps: false, pieces: read, boxPsi });
  };

  /** Never fails, never waits past the time limit, never piles up hanging reads. */
  const readings = (pieces: ReadonlyArray<OwnPiece>) =>
    Effect.suspend(() => {
      if (hanging >= OBSERVE_MAX_HANGING) {
        return Effect.succeed({ facts: null, readings: "busy" as SlotReadings });
      }
      hanging += 1;
      const promise = Promise.resolve()
        .then(() => readFacts(pieces))
        .finally(() => {
          hanging -= 1;
        });
      return Effect.tryPromise({ try: () => promise, catch: () => "failed" as const }).pipe(
        Effect.timeout(OBSERVE_READ_TIMEOUT),
        Effect.map((facts) => ({ facts, readings: "ok" as SlotReadings })),
        Effect.catch((error) =>
          Effect.succeed({
            facts: null,
            readings: (error === "failed" ? "failed" : "slow") as SlotReadings,
          }),
        ),
      );
    });

  const run = Effect.fn("SlotObserver.run")(function* (
    input: SlotObserveInput,
    ownBefore: ReadonlyArray<StartingEntry>,
  ) {
    const running = yield* readRunning;
    // Off the decision path; a store read past the time limit leaves the pieces unknown.
    const heldRead =
      running === undefined
        ? Option.none()
        : yield* Effect.timeoutOption(input.held, OBSERVE_READ_TIMEOUT);
    let pieces: ReadonlyArray<OwnPiece> | null = null;
    if (running !== undefined && Option.isSome(heldRead)) {
      // The starts of before the decision as they are now, read with the runs of now: a child
      // named or a dispatch ended while this waited is seen (else a child that shows its run
      // meanwhile would count twice).
      const before = new Set(ownBefore.map((entry) => entry.token));
      const own = ownStarts.filter((entry) => before.has(entry.token));
      // A child seen with a run counts as running from now on, as in the gate's own records.
      ownStarts = ownStarts.filter(
        (entry) => entry.childThreadId === undefined || !running.visible.has(entry.childThreadId),
      );
      const starting = liveStarting(
        [...input.starting, ...own],
        running.visible,
        input.atMs,
        input.startGraceMs,
      );
      const live = liveReservations(heldRead.value, input.atMs);
      if (!input.gateRecords) {
        for (const reservation of live) {
          if (startedThreads.has(reservation.threadId)) {
            usedHolds.set(reservation.key, reservation.untilMs);
          }
        }
        startedThreads.clear();
      }
      const held = onePerThread(
        live.filter(
          (reservation) =>
            reservation.threadId !== input.callerThreadId &&
            (input.gateRecords || !usedHolds.has(reservation.key)),
        ),
      );
      const scopes = yield* Effect.forEach([...running.visible], (threadId) =>
        agentScope.ownScope === undefined ? Effect.undefined : agentScope.ownScope(threadId),
      );
      pieces = [
        ...scopes.map((scope) => ({ scope })),
        ...[...starting, ...held].map(() => ({ scope: undefined })),
      ];
    }
    const read =
      pieces === null
        ? {
            facts: null,
            readings: (running === undefined ? "failed" : "slow") as SlotReadings,
          }
        : yield* readings(pieces);
    const { decisions, pressure: next } = observeSlotAnswers(CAPACITY_RULE, {
      facts: read.facts,
      today: input.today,
      previous: yield* Ref.get(pressure),
      nowMs: input.atMs,
    });
    yield* Ref.set(pressure, next);
    const at = DateTime.formatIso(DateTime.makeUnsafe(input.atMs));
    for (const [index, decision] of decisions.entries()) {
      const observation = slotRuleObservation({
        at,
        today: input.today[index]!,
        decision,
        pieces: pieces === null ? null : pieces.length,
        readings: read.readings,
      });
      const disagreed = observation.rule !== observation.today;
      yield* Ref.update(status, (current) => ({
        ...current,
        last: observation,
        observed: current.observed + 1,
        disagreed: current.disagreed + (disagreed ? 1 : 0),
      }));
      yield* Effect.logInfo(
        `agent slots: the capacity rule would ${observation.rule} (today: ${observation.today})`,
        observation,
      );
    }
  });

  /** Forked at once; the caller never waits for it, and nothing it does fails the caller. */
  const observe = (input: SlotObserveInput): Effect.Effect<void> =>
    Effect.suspend(() => {
      if (input.today.length === 0) return Effect.void;
      // Kept at the call, before any skip, so a skipped allow still counts for the next ones.
      ownStarts = input.gateRecords
        ? []
        : liveStarting(ownStarts, new Set(), input.atMs, input.startGraceMs);
      for (const [key, untilMs] of usedHolds) {
        if (input.gateRecords || untilMs <= input.atMs) usedHolds.delete(key);
      }
      for (const [threadId, atMs] of startedThreads) {
        if (input.gateRecords || input.atMs - atMs >= DISPATCH_SAFETY_MS) {
          startedThreads.delete(threadId);
        }
      }
      const ownBefore = ownStarts;
      if (!input.gateRecords && input.ownStart !== undefined) {
        ownStarts = [...ownStarts, input.ownStart];
        if (input.callerThreadId !== undefined)
          startedThreads.set(input.callerThreadId, input.atMs);
      }
      if (waiting >= OBSERVE_MAX_WAITING) {
        return Ref.update(status, (current) => ({ ...current, skipped: current.skipped + 1 }));
      }
      waiting += 1;
      return lock
        .withPermits(1)(run(input, ownBefore))
        .pipe(
          Effect.catchCause((cause) =>
            Effect.logWarning("agent slots: the capacity rule could not be observed", { cause }),
          ),
          Effect.ensuring(
            Effect.sync(() => {
              waiting -= 1;
            }),
          ),
          // Not on the caller's stack: it runs once the decision is answered, whatever it reads.
          FiberSet.run(fibers, { startImmediately: false }),
          Effect.asVoid,
        );
    }).pipe(Effect.ignoreCause);

  return {
    observe,
    /** The gate's `started` with one of the rule's own tokens: the child that start names. */
    started: (token: number, childThreadId: string) =>
      Effect.sync(() => {
        ownStarts = ownStarts.map((entry) =>
          entry.token === token ? { ...entry, childThreadId } : entry,
        );
      }),
    /** The gate's `release`: the dispatch ended (as the gate ends its own entries). */
    released: (token: number, atMs: number) =>
      Effect.sync(() => {
        ownStarts = ownStarts.flatMap((entry) =>
          entry.token !== token
            ? [entry]
            : entry.childThreadId === undefined
              ? []
              : [entry.endedAtMs === undefined ? { ...entry, endedAtMs: atMs } : entry],
        );
      }),
    status: Ref.get(status),
    /** What the rule keeps in memory (for tests: nothing past the point it stops counting). */
    kept: Effect.sync(() => ({
      starts: ownStarts.length,
      usedHolds: usedHolds.size,
      notes: startedThreads.size,
    })),
  };
});
