// Gate in front of outbox claims and worktree preparation. An automatic update closes it before
// its last quiet check and keeps it closed until exit. The marker makes startup keep held work and
// start it once the update is committed, once: it records the held `runs` and those `started`
// (a failed update restores the database). See ProviderRuntimeRecoveryService.
import type { CommandId, RunId, ThreadId } from "@t3tools/contracts";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Latch from "effect/Latch";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as Schema from "effect/Schema";

/** The parts of the server that must use one shared gate before an update may hold it. */
export const WORK_ADMISSION_CONSUMERS = ["outbox", "thread-launch", "recovery"] as const;
export type WorkAdmissionConsumer = (typeof WORK_ADMISSION_CONSUMERS)[number];

export const UPDATE_HOLD_MARKER_FILE = "work-held-for-update.json";
/** An older marker is left over from a hold that ended without clearing it, not this restart. */
export const UPDATE_HOLD_MAX_AGE_MS = 2 * 60 * 60_000;

const UpdateHold = Schema.Struct({
  heldAt: Schema.String,
  targetVersion: Schema.String,
  /** The runs the first startup after the hold kept: later startups consider only these. */
  runs: Schema.optional(Schema.Array(Schema.String)),
  /** Runs a startup started (or was about to): they are never started again. */
  started: Schema.optional(Schema.Array(Schema.String)),
});
export type UpdateHold = typeof UpdateHold.Type;

/** What a fresh marker tells startup recovery. */
export interface HeldForUpdate {
  readonly heldAt: DateTime.Utc;
  readonly runs: ReadonlySet<string> | undefined;
  readonly started: ReadonlySet<string>;
}

export class UpdateHoldMarkerError extends Schema.TaggedError<UpdateHoldMarkerError>()(
  "UpdateHoldMarkerError",
  { reason: Schema.String },
) {}

/** Where the update marker lives: `<baseDir>/runtime`, beside the launcher's state. */
export interface UpdateHoldStore {
  /** `undefined` when there is no marker or it cannot be read; "invalid" when it is not one. */
  readonly read: Effect.Effect<UpdateHold | "invalid" | undefined>;
  readonly write: (hold: UpdateHold) => Effect.Effect<void, UpdateHoldMarkerError>;
  readonly clear: Effect.Effect<void>;
}

export const makeFileUpdateHoldStore = Effect.fn("workAdmission.makeFileUpdateHoldStore")(
  function* (baseDir: string) {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const runtimeDir = path.join(baseDir, "runtime");
    const file = path.join(runtimeDir, UPDATE_HOLD_MARKER_FILE);
    const json = Schema.fromJsonString(UpdateHold);
    const decode = Schema.decodeUnknownOption(json);
    const encode = Schema.encodeEffect(json);
    return {
      read: fs.readFileString(file).pipe(
        Effect.map((contents) => {
          const decoded = decode(contents);
          return decoded._tag === "Some" ? decoded.value : ("invalid" as const);
        }),
        Effect.catch((error) =>
          error.reason._tag === "NotFound"
            ? Effect.undefined
            : Effect.logWarning("The update hold marker could not be read.", {
                reason: error.reason._tag,
              }).pipe(Effect.as(undefined)),
        ),
      ),
      write: Effect.fn("workAdmission.writeUpdateHold")(
        function* (hold: UpdateHold) {
          yield* fs.makeDirectory(runtimeDir, { recursive: true });
          const staging = `${file}.tmp`;
          yield* fs.writeFileString(staging, yield* encode(hold));
          yield* fs.rename(staging, file);
        },
        Effect.mapError((error) => new UpdateHoldMarkerError({ reason: error.message })),
      ),
      clear: fs.remove(file, { force: true }).pipe(
        Effect.catch((error) =>
          Effect.logWarning("The update hold marker could not be removed.", {
            reason: error.message,
          }),
        ),
      ),
    } satisfies UpdateHoldStore;
  },
);

export const memoryUpdateHoldStore: Effect.Effect<
  UpdateHoldStore & {
    readonly current: Effect.Effect<UpdateHold | "invalid" | undefined>;
    readonly set: (hold: UpdateHold | "invalid" | undefined) => Effect.Effect<void>;
  }
> = Effect.gen(function* () {
  const marker = yield* Ref.make<UpdateHold | "invalid" | undefined>(undefined);
  return {
    read: Ref.get(marker),
    write: (hold) => Ref.set(marker, hold),
    clear: Ref.set(marker, undefined),
    current: Ref.get(marker),
    set: (hold) => Ref.set(marker, hold),
  };
});

/** A worktree preparation that waited at the gate when the last process stopped. */
export interface HeldPreparation {
  readonly commandId: CommandId;
  readonly threadId: ThreadId;
  readonly runId: RunId;
}

export interface WorkAdmissionGate {
  /** False for the default pass-through: nothing is held, so callers must not rely on `hold`. */
  readonly enforced: boolean;
  /** Runs `effect` once the gate is open, counting it as in flight while it runs. */
  readonly admit: <A, E, R>(effect: Effect.Effect<A, E, R>) => Effect.Effect<A, E, R>;
  /** Closes the gate. New work waits; returns how many admitted units are still running. */
  readonly hold: Effect.Effect<number>;
  /** Opens the gate again, removes the update marker and lets waiting work start. */
  readonly release: Effect.Effect<void>;
  readonly inFlight: Effect.Effect<number>;
  readonly isHeld: Effect.Effect<boolean>;
  /** Each consumer says it admits its work through this gate. */
  readonly register: (consumer: WorkAdmissionConsumer) => Effect.Effect<void>;
  /** Consumers that never registered: an update must not hold a gate they do not use. */
  readonly missingConsumers: Effect.Effect<ReadonlyArray<WorkAdmissionConsumer>>;
  /** Records that new work is held for an update, for the next startup. */
  readonly markHeldForUpdate: (hold: UpdateHold) => Effect.Effect<void, UpdateHoldMarkerError>;
  /** A fresh marker: work saved while it was held never reached a provider. A stale or invalid
   * marker is removed and reads as none. */
  readonly heldForUpdate: Effect.Effect<HeldForUpdate | undefined>;
  /** Saves which runs this startup kept, unless an earlier startup after the hold already did. */
  readonly recordHeldRuns: (
    runIds: ReadonlyArray<string>,
  ) => Effect.Effect<void, UpdateHoldMarkerError>;
  /** Saves that these runs are being started; callers start them only once this succeeded. */
  readonly recordHeldRunsStarted: (
    runIds: ReadonlyArray<string>,
  ) => Effect.Effect<void, UpdateHoldMarkerError>;
  /** Every held run was started: removes the marker and says so through `heldWorkSettled`. */
  readonly finishHeldWork: Effect.Effect<void>;
  /** This process has started what an earlier update held, so it may hold work again. */
  readonly heldWorkSettled: Effect.Effect<boolean>;
  /** Startup recovery's way to start the work it kept for an update. */
  readonly setHeldWorkStarter: (start: Effect.Effect<void>) => Effect.Effect<void>;
  /** Starts the kept work. Startup calls it only once the update is committed, so a failed
   * update that rolls back has started nothing. It never fails. */
  readonly startHeldWork: Effect.Effect<void>;
  /** The thread-launch service's way to start a held worktree preparation again. */
  readonly setPreparationResumer: (
    resume: (preparation: HeldPreparation) => Effect.Effect<void>,
  ) => Effect.Effect<void>;
  readonly resumePreparation: (preparation: HeldPreparation) => Effect.Effect<void>;
}

interface GateState {
  readonly held: boolean;
  readonly inFlight: number;
}

const noMarker: UpdateHoldStore = {
  read: Effect.undefined,
  write: () => Effect.fail(new UpdateHoldMarkerError({ reason: "this gate keeps no marker" })),
  clear: Effect.void,
};

const markerSince = (hold: UpdateHold, nowMs: number) => {
  const heldAt = DateTime.make(hold.heldAt);
  if (heldAt._tag === "None") return undefined;
  const ageMs = nowMs - DateTime.toEpochMillis(heldAt.value);
  return ageMs >= 0 && ageMs <= UPDATE_HOLD_MAX_AGE_MS ? heldAt.value : undefined;
};

export const makeWorkAdmissionGate = Effect.fn("workAdmission.makeWorkAdmissionGate")(function* (
  options: { readonly marker?: UpdateHoldStore } = {},
) {
  const marker = options.marker ?? noMarker;
  const state = yield* Ref.make<GateState>({ held: false, inFlight: 0 });
  const registered = yield* Ref.make(new Set<WorkAdmissionConsumer>());
  // The fresh marker as this process last read or wrote it.
  const freshMarker = yield* Ref.make<UpdateHold | undefined>(undefined);
  const settled = yield* Ref.make(false);
  const rewriteMarker = Effect.fn("workAdmission.rewriteMarker")(function* (
    change: (hold: UpdateHold) => UpdateHold,
  ) {
    const current = yield* Ref.get(freshMarker);
    if (current === undefined) {
      return yield* new UpdateHoldMarkerError({ reason: "no fresh update marker was read" });
    }
    const next = change(current);
    if (next === current) return;
    yield* marker.write(next);
    yield* Ref.set(freshMarker, next);
  });
  const heldWorkStarter = yield* Ref.make<Effect.Effect<void> | undefined>(undefined);
  const resumer = yield* Ref.make<
    ((preparation: HeldPreparation) => Effect.Effect<void>) | undefined
  >(undefined);
  const open = yield* Latch.make(true);
  const admit = <A, E, R>(effect: Effect.Effect<A, E, R>): Effect.Effect<A, E, R> =>
    Effect.uninterruptibleMask((restore) =>
      Effect.gen(function* () {
        // The held check and the count change in one step, so a unit admitted before `hold`
        // is always counted and none starts after it.
        while (true) {
          yield* restore(open.await);
          const admitted = yield* Ref.modify(state, (current): [boolean, GateState] =>
            current.held
              ? [false, current]
              : [true, { ...current, inFlight: current.inFlight + 1 }],
          );
          if (admitted) break;
        }
        return yield* restore(effect).pipe(
          Effect.ensuring(
            Ref.update(state, (current) => ({ ...current, inFlight: current.inFlight - 1 })),
          ),
        );
      }),
    );
  const gate: WorkAdmissionGate = {
    enforced: true,
    admit,
    hold: Effect.gen(function* () {
      const inFlight = yield* Ref.modify(state, (current): [number, GateState] => [
        current.inFlight,
        { ...current, held: true },
      ]);
      yield* open.close;
      return inFlight;
    }),
    // The marker goes first: work released here must not be restarted by a later startup.
    release: marker.clear.pipe(
      Effect.andThen(Ref.update(state, (current) => ({ ...current, held: false }))),
      Effect.andThen(open.open),
      Effect.asVoid,
    ),
    inFlight: Ref.get(state).pipe(Effect.map((current) => current.inFlight)),
    isHeld: Ref.get(state).pipe(Effect.map((current) => current.held)),
    register: (consumer) => Ref.update(registered, (current) => new Set([...current, consumer])),
    missingConsumers: Ref.get(registered).pipe(
      Effect.map((current) =>
        WORK_ADMISSION_CONSUMERS.filter((consumer) => !current.has(consumer)),
      ),
    ),
    markHeldForUpdate: marker.write,
    heldForUpdate: Effect.gen(function* () {
      const hold = yield* marker.read;
      yield* Ref.set(freshMarker, undefined);
      if (hold === undefined) return undefined;
      const since =
        hold === "invalid"
          ? undefined
          : markerSince(hold, DateTime.toEpochMillis(yield* DateTime.now));
      if (hold === "invalid" || since === undefined) {
        // Left over from a hold that ended without clearing it: none of its work is held now.
        yield* Effect.logWarning("Removing a stale or invalid update hold marker.", {
          heldAt: hold === "invalid" ? undefined : hold.heldAt,
        });
        yield* marker.clear;
        return undefined;
      }
      yield* Ref.set(freshMarker, hold);
      return {
        heldAt: since,
        runs: hold.runs === undefined ? undefined : new Set(hold.runs),
        started: new Set(hold.started ?? []),
      };
    }),
    recordHeldRuns: (runIds) =>
      rewriteMarker((hold) => (hold.runs !== undefined ? hold : { ...hold, runs: [...runIds] })),
    recordHeldRunsStarted: (runIds) =>
      rewriteMarker((hold) => ({
        ...hold,
        started: [...new Set([...(hold.started ?? []), ...runIds])],
      })),
    finishHeldWork: marker.clear.pipe(
      Effect.andThen(Ref.set(freshMarker, undefined)),
      Effect.andThen(Ref.set(settled, true)),
    ),
    heldWorkSettled: Ref.get(settled),
    setHeldWorkStarter: (start) => Ref.set(heldWorkStarter, start),
    startHeldWork: Ref.get(heldWorkStarter).pipe(Effect.flatMap((start) => start ?? Effect.void)),
    setPreparationResumer: (resume) => Ref.set(resumer, resume),
    resumePreparation: (preparation) =>
      Ref.get(resumer).pipe(
        Effect.flatMap((resume) =>
          resume === undefined
            ? Effect.logError(
                "A held worktree preparation cannot start again: no thread-launch service uses this gate.",
                preparation,
              )
            : resume(preparation),
        ),
      ),
  };
  return gate;
});

const passThrough: WorkAdmissionGate = {
  enforced: false,
  admit: (effect) => effect,
  hold: Effect.succeed(0),
  release: Effect.void,
  inFlight: Effect.succeed(0),
  isHeld: Effect.succeed(false),
  register: () => Effect.void,
  missingConsumers: Effect.succeed(WORK_ADMISSION_CONSUMERS),
  markHeldForUpdate: noMarker.write,
  heldForUpdate: Effect.undefined,
  recordHeldRuns: () => Effect.void,
  recordHeldRunsStarted: () => Effect.void,
  finishHeldWork: Effect.void,
  heldWorkSettled: Effect.succeed(true),
  setHeldWorkStarter: () => Effect.void,
  startHeldWork: Effect.void,
  setPreparationResumer: () => Effect.void,
  resumePreparation: () => Effect.void,
};

/** The server provides one shared gate. Tests and tools that do not see the pass-through, which
 * holds nothing; automatic updates refuse to run on it (see `enforced` and `missingConsumers`). */
export class WorkAdmission extends Context.Reference<WorkAdmissionGate>("t3/workAdmission", {
  defaultValue: () => passThrough,
}) {}
