/**
 * Fork (issue 1156, phase 2): the lane store, one file per project at `<stateDir>/lanes/`. Each step
 * reads, changes and atomically rewrites it under one lock. Off: every step refused, no file read or
 * written. An unreadable file is unknown, not empty: every step refused, the file kept.
 */
import {
  type LaneClaim,
  laneDefinitions,
  type LaneEscalation,
  type LaneNote,
  type ProjectId,
  type ThreadId,
} from "@t3tools/contracts";
import { writeFileStringAtomically } from "@t3tools/shared/atomicWrite";
import * as Context from "effect/Context";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import { ServerConfig } from "../../config.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import * as LaneState from "./laneState.ts";

const LANES_DIRECTORY = "lanes";

/** A refused step: why, in words an agent can act on. */
export class LaneStoreRefused extends Schema.TaggedError<LaneStoreRefused>()("LaneStoreRefused", {
  reason: Schema.String,
  detail: Schema.String,
  holder: Schema.optional(Schema.Unknown),
}) {
  override get message(): string {
    return this.detail;
  }
}

/** A claim as the board shows it: stale when its lane stopped or is no longer defined. */
export interface BoardClaim {
  readonly claim: LaneClaim;
  readonly live: boolean;
}

export interface LaneBoard {
  readonly lanes: { readonly [lane: string]: LaneState.LaneEntry };
  readonly claims: ReadonlyArray<BoardClaim>;
  readonly outbox: ReadonlyArray<LaneEscalation>;
  readonly notes: ReadonlyArray<LaneNote>;
}

type Step<A> = Effect.Effect<A, LaneStoreRefused>;

export class LaneStore extends Context.Service<
  LaneStore,
  {
    readonly startLane: (
      projectId: ProjectId,
      lane: string,
      threadId: ThreadId,
    ) => Step<LaneState.LaneEntry>;
    readonly replaceLaneThread: (
      projectId: ProjectId,
      lane: string,
      fromGeneration: number,
      threadId: ThreadId,
    ) => Step<LaneState.LaneEntry>;
    /** The claims it released. */
    readonly stopLane: (projectId: ProjectId, lane: string) => Step<ReadonlyArray<LaneClaim>>;
    readonly claim: (
      projectId: ProjectId,
      actor: LaneState.LaneActor,
      item: string,
    ) => Step<LaneState.ClaimOutcome>;
    readonly release: (
      projectId: ProjectId,
      actor: LaneState.LaneActor,
      item: string,
    ) => Step<LaneClaim | null>;
    readonly note: (
      projectId: ProjectId,
      actor: LaneState.LaneActor,
      text: string,
      items: ReadonlyArray<string>,
    ) => Step<LaneNote>;
    readonly escalate: (
      projectId: ProjectId,
      actor: LaneState.LaneActor,
      id: string,
      text: string,
      items: ReadonlyArray<string>,
    ) => Step<LaneEscalation>;
    /** Removes delivered escalations; the number removed. */
    readonly ackEscalations: (projectId: ProjectId, ids: ReadonlyArray<string>) => Step<number>;
    /** The live claim on an item, or null: none, or only a stale one. */
    readonly ownerOf: (projectId: ProjectId, item: string) => Step<LaneClaim | null>;
    readonly board: (projectId: ProjectId) => Step<LaneBoard>;
    /**
     * The lane a run belongs to: its thread's, or (a lane's worker) its parent's. Null while lanes
     * are off, for a thread of no lane, and when the file cannot be read (logged): never fails.
     */
    readonly laneOfRun: (
      projectId: ProjectId,
      threadId: ThreadId,
      parentThreadId: ThreadId | null,
    ) => Effect.Effect<string | null>;
  }
>()("t3/fork/lanes/LaneStore") {}

const refused = (refusal: LaneState.LaneRefusal) =>
  new LaneStoreRefused({
    reason: refusal.reason,
    detail: refusal.message,
    ...(refusal.holder !== undefined ? { holder: refusal.holder } : {}),
  });

export const make = Effect.fn("LaneStore.make")(function* (options: {
  readonly directory: string;
}) {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const settings = yield* ServerSettingsService;
  // One step at a time across every project: steps are small, and a claim must see the last one.
  const lock = yield* Semaphore.make(1);

  const fileOf = (projectId: ProjectId) =>
    path.join(options.directory, `${encodeURIComponent(projectId)}.json`);

  /** The lanes that exist (built-in, then the stored ones over them) while lanes are on; else refused. */
  const definitionsNow = settings.getSettings.pipe(
    Effect.mapError(() =>
      refused(LaneState.refuse("lanes-off", "The server settings cannot be read now.")),
    ),
    Effect.flatMap((current) =>
      current.lanes.enabled
        ? Effect.succeed(laneDefinitions(current.lanes))
        : Effect.fail(
            refused(LaneState.refuse("lanes-off", "Lanes are off (server.lanes.enabled).")),
          ),
    ),
  );

  /** The project's state: empty when there is no file, refused when it cannot be trusted. */
  const read = (projectId: ProjectId) =>
    fs.readFileString(fileOf(projectId)).pipe(
      Effect.map((text) => LaneState.decodeLaneState(text, projectId)),
      Effect.catchIf(
        (error) => error._tag === "PlatformError" && error.reason._tag === "NotFound",
        () => Effect.succeed(LaneState.emptyLaneState(projectId)),
      ),
      Effect.orElseSucceed(() => null),
      Effect.tap((state) =>
        state === null
          ? Effect.logWarning("lane store: a project's lane file cannot be read", { projectId })
          : Effect.void,
      ),
      Effect.filterOrFail(
        (state): state is LaneState.LaneProjectState => state !== null,
        () =>
          refused(
            LaneState.refuse(
              "store-unreadable",
              "The project's lane file cannot be read; nothing is changed until it reads again.",
            ),
          ),
      ),
    );

  const write = (projectId: ProjectId, state: LaneState.LaneProjectState) =>
    writeFileStringAtomically({
      filePath: fileOf(projectId),
      contents: LaneState.encodeLaneState(state),
    }).pipe(
      Effect.provideService(FileSystem.FileSystem, fs),
      Effect.provideService(Path.Path, path),
      Effect.mapError(() =>
        refused(
          LaneState.refuse("store-unreadable", "The project's lane file cannot be written now."),
        ),
      ),
    );

  /** Runs one step under the lock: settings, read, step, write if it changed anything. */
  const run = <A>(
    projectId: ProjectId,
    step: (
      state: LaneState.LaneProjectState,
      definitions: LaneState.LaneDefinitions,
      now: string,
    ) => LaneState.LaneStepResult<A>,
  ): Step<A> =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const definitions = yield* definitionsNow;
        const state = yield* read(projectId);
        const now = DateTime.formatIso(yield* DateTime.now);
        const result = step(state, definitions, now);
        if (result._tag === "refused") return yield* refused(result);
        if (result.changed) yield* write(projectId, result.state);
        return result.value;
      }),
    );

  const view = <A>(
    projectId: ProjectId,
    look: (state: LaneState.LaneProjectState, definitions: LaneState.LaneDefinitions) => A,
  ): Step<A> =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const definitions = yield* definitionsNow;
        return look(yield* read(projectId), definitions);
      }),
    );

  return LaneStore.of({
    startLane: Effect.fn("LaneStore.startLane")(function* (projectId, lane, threadId) {
      return yield* run(projectId, (state, definitions, now) =>
        LaneState.startLane(state, definitions, { lane, threadId, now }),
      );
    }),
    replaceLaneThread: Effect.fn("LaneStore.replaceLaneThread")(
      function* (projectId, lane, fromGeneration, threadId) {
        return yield* run(projectId, (state, definitions, now) =>
          LaneState.replaceLaneThread(state, definitions, { lane, fromGeneration, threadId, now }),
        );
      },
    ),
    stopLane: Effect.fn("LaneStore.stopLane")(function* (projectId, lane) {
      return yield* run(projectId, (state, _definitions, now) =>
        LaneState.stopLane(state, { lane, now }),
      );
    }),
    claim: Effect.fn("LaneStore.claim")(function* (projectId, actor, item) {
      return yield* run(projectId, (state, definitions, now) =>
        LaneState.claimItem(state, definitions, { actor, item, now }),
      );
    }),
    release: Effect.fn("LaneStore.release")(function* (projectId, actor, item) {
      return yield* run(projectId, (state, definitions) =>
        LaneState.releaseItem(state, definitions, { actor, item }),
      );
    }),
    note: Effect.fn("LaneStore.note")(function* (projectId, actor, text, items) {
      return yield* run(projectId, (state, definitions, now) =>
        LaneState.addNote(state, definitions, { actor, text, items, now }),
      );
    }),
    escalate: Effect.fn("LaneStore.escalate")(function* (projectId, actor, id, text, items) {
      return yield* run(projectId, (state, definitions, now) =>
        LaneState.addEscalation(state, definitions, { actor, id, text, items, now }),
      );
    }),
    ackEscalations: Effect.fn("LaneStore.ackEscalations")(function* (projectId, ids) {
      return yield* run(projectId, (state) => LaneState.ackEscalations(state, ids));
    }),
    ownerOf: Effect.fn("LaneStore.ownerOf")(function* (projectId, item) {
      return yield* view(projectId, (state, definitions) => {
        const claim = state.claims[item];
        return claim !== undefined && LaneState.claimIsLive(state, definitions, claim)
          ? claim
          : null;
      });
    }),
    board: Effect.fn("LaneStore.board")(function* (projectId) {
      return yield* view(projectId, (state, definitions) => ({
        lanes: state.lanes,
        claims: Object.values(state.claims).map((claim) => ({
          claim,
          live: LaneState.claimIsLive(state, definitions, claim),
        })),
        outbox: state.outbox,
        notes: state.notes,
      }));
    }),
    laneOfRun: Effect.fn("LaneStore.laneOfRun")(
      function* (projectId, threadId, parentThreadId) {
        return yield* view(
          projectId,
          (state) =>
            LaneState.laneOfThread(state, threadId) ??
            (parentThreadId === null ? null : LaneState.laneOfThread(state, parentThreadId)),
        );
      },
      Effect.orElseSucceed(() => null),
    ),
  });
});

/** Over `<stateDir>/lanes`. */
export const layer = Layer.effect(
  LaneStore,
  Effect.gen(function* () {
    const config = yield* ServerConfig;
    const path = yield* Path.Path;
    return yield* make({ directory: path.join(config.stateDir, LANES_DIRECTORY) });
  }),
);
