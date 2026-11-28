/**
 * Fork (issue 1156, phase 3): the lane tools over the lane store. Registered in the waits toolkit
 * (mcp/toolkits/waits), in the `lanes` T3 tool group, and listed only while `server.lanes.enabled`
 * is on (laneToolsVisibility.ts). The calling thread and its project come from the server's record
 * of the thread, never from input.
 */
import {
  LaneClaim,
  LaneEscalation,
  LaneGeneration,
  LaneItemKind,
  LaneNote,
  OrchestratorMcpFailure,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import { callingThread as threadOfCaller } from "../../mcp/toolkits/waits/callingThread.ts";
import { unavailable } from "../../mcp/threadAccess.ts";
import { LaneStore, type LaneStoreRefused } from "./LaneStore.ts";
import { type LaneOperator, type LaneRefusalReason, normalizeItem, own } from "./laneState.ts";

/** Notes per lane the board shows, newest last; the store keeps more. */
export const BOARD_NOTES_PER_LANE = 3;

/**
 * Every reason the store refuses with, once: a reason added to {@link LaneRefusalReason} and not
 * here is a type error, so no refusal is ever a code the failure's schema rejects.
 */
const LANE_REFUSAL_REASON_KEYS: { readonly [Reason in LaneRefusalReason]: true } = {
  "lanes-off": true,
  "settings-unreadable": true,
  "store-unreadable": true,
  "store-unwritable": true,
  "unknown-lane": true,
  "not-running": true,
  "already-running": true,
  "stale-owner": true,
  "self-lane": true,
  "not-starter": true,
  "kind-not-owned": true,
  "bad-item": true,
  "bad-input": true,
  claimed: true,
  "not-holder": true,
  "outbox-full": true,
};
const LANE_REFUSAL_REASONS = Object.keys(
  LANE_REFUSAL_REASON_KEYS,
) as ReadonlyArray<LaneRefusalReason>;

/** A step the lane store refused: its reason, and for `claimed` and `not-holder` the claim that stands. */
export class LaneToolFailure extends Schema.TaggedError<LaneToolFailure>()("LaneToolFailure", {
  code: Schema.Literals([...LANE_REFUSAL_REASONS, "thread-elsewhere"]),
  message: Schema.String,
  holder: Schema.optionalKey(LaneClaim),
}) {}

export const LaneToolsFailure = Schema.Union([LaneToolFailure, OrchestratorMcpFailure]);

// Plain strings: the store refuses an unknown lane or a bad item with its reason, and a short schema
// keeps the tool list small.
const laneField = Schema.String.annotate({ description: "e.g. pr-queue" });
const generationField = LaneGeneration.annotate({ description: "From lane_start." });
const itemField = Schema.String.annotate({ description: "pr:owner/repo#12 or issue:owner/repo#3" });

export const LaneStartInput = Schema.Struct({
  lane: laneField,
  threadId: ThreadId.annotate({ description: "A thread of this project." }),
});
export const LaneStartResult = Schema.Struct({
  lane: Schema.String,
  generation: Schema.Number,
  threadId: ThreadId,
  startedAt: Schema.String,
  next: Schema.String,
});

export const LaneStopInput = Schema.Struct({ lane: laneField });
export const LaneStopResult = Schema.Struct({
  lane: Schema.String,
  /** The items whose claims the stop released. */
  released: Schema.Array(Schema.String),
});

const BoardLane = Schema.Struct({
  lane: Schema.String,
  owns: Schema.Array(LaneItemKind),
  /** `off`: defined, never started. `undefined`: in the store, no longer in settings. */
  status: Schema.Literals(["running", "stopped", "off", "undefined"]),
  generation: Schema.NullOr(Schema.Number),
  threadId: Schema.NullOr(ThreadId),
  /** The thread that started it, the one that stops it while live. Null: none recorded (any thread). */
  startedBy: Schema.NullOr(ThreadId),
  startedAt: Schema.NullOr(Schema.String),
  stoppedAt: Schema.NullOr(Schema.String),
});
export const LaneBoardResult = Schema.Struct({
  lanes: Schema.Array(BoardLane),
  /** `live: false`: the lane stopped or is no longer defined, so the next claim takes the item. */
  claims: Schema.Array(Schema.Struct({ ...LaneClaim.fields, live: Schema.Boolean })),
  escalations: Schema.Array(LaneEscalation),
  notes: Schema.Array(LaneNote),
});

export const LaneClaimInput = Schema.Struct({
  lane: laneField,
  generation: generationField,
  item: itemField,
});
export const LaneClaimResult = Schema.Struct({
  claim: LaneClaim,
  /** The stale claim this one replaced (its lane stopped or is no longer defined). */
  takenFrom: Schema.NullOr(LaneClaim),
});

export const LaneReleaseInput = LaneClaimInput;
export const LaneReleaseResult = Schema.Struct({
  /** Null: the lane held no claim on the item. */
  released: Schema.NullOr(LaneClaim),
});

const callingThread = () =>
  threadOfCaller("Lanes belong to a project. This MCP client is not running inside a T3 thread.");

/** A lane tool always acts as the calling thread, never as the owner. */
const asThread = (threadId: ThreadId) => ({ _tag: "thread", threadId }) as const;

const failure = (refusal: LaneStoreRefused) =>
  new LaneToolFailure({
    code: refusal.reason as LaneToolFailure["code"],
    message: refusal.detail,
    ...(refusal.holder === undefined ? {} : { holder: refusal.holder as LaneClaim }),
  });

const refusedAs = <A, R>(step: Effect.Effect<A, LaneStoreRefused, R>) =>
  step.pipe(Effect.mapError(failure));

/** `lane_start`: the thread must be a live thread of the caller's project, and not the caller. */
export const laneStartCall = Effect.fn("laneTools.start")(function* (
  input: typeof LaneStartInput.Type,
) {
  const calling = yield* callingThread();
  const projectId = calling.caller.projectId;
  const target = yield* calling.threads
    .getThreadShell(input.threadId)
    .pipe(Effect.mapError(unavailable));
  if (target === null || target.deletedAt !== null) {
    return yield* new OrchestratorMcpFailure({
      code: "thread_not_found",
      message: `Thread ${input.threadId} does not exist.`,
    });
  }
  if (target.projectId !== projectId) {
    return yield* new LaneToolFailure({
      code: "thread-elsewhere",
      message: `Thread ${input.threadId} is in another project; a lane's thread is in the lane's project.`,
    });
  }
  const store = yield* LaneStore;
  const entry = yield* refusedAs(
    store.startLane(projectId, input.lane, input.threadId, asThread(calling.caller.id)),
  );
  return {
    lane: input.lane,
    generation: entry.generation,
    threadId: entry.threadId,
    startedAt: entry.startedAt,
    next: `Tell thread ${entry.threadId} it acts for lane '${input.lane}' at generation ${entry.generation}: lane_claim and lane_release take both.`,
  };
});

/**
 * `lane_stop`: from the thread that started the lane; once that thread is no longer a live thread
 * of the project (deleted, or moved to another project), from any thread of it, so a lane never
 * outlives everyone who may stop it.
 */
export const laneStopCall = Effect.fn("laneTools.stop")(function* (
  input: typeof LaneStopInput.Type,
) {
  const calling = yield* callingThread();
  const projectId = calling.caller.projectId;
  const store = yield* LaneStore;
  const stop = (by: LaneOperator) =>
    refusedAs(store.stopLane(projectId, input.lane, by)).pipe(
      Effect.map((released) => ({ lane: input.lane, released: released.map((c) => c.item) })),
    );
  const by = asThread(calling.caller.id);
  return yield* stop(by).pipe(
    Effect.catchIf(
      (refusal) => refusal._tag === "LaneToolFailure" && refusal.code === "not-starter",
      (refusal) =>
        Effect.gen(function* () {
          const starter = own(
            (yield* refusedAs(store.board(projectId))).lanes,
            input.lane,
          )?.startedBy;
          if (starter === undefined) return yield* refusal;
          const shell = yield* calling.threads
            .getThreadShell(starter)
            .pipe(Effect.mapError(unavailable));
          const live = shell !== null && shell.deletedAt === null && shell.projectId === projectId;
          if (live) return yield* refusal;
          // The store stops only while the lane still names this starter (no restart in between).
          return yield* stop({ ...by, goneStarter: starter });
        }),
    ),
  );
});

export const laneBoardCall = Effect.fn("laneTools.board")(function* () {
  const calling = yield* callingThread();
  const store = yield* LaneStore;
  // One read: the definitions come with the store's view (settings that cannot be read: refused).
  const board = yield* refusedAs(store.board(calling.caller.projectId));
  const definitions = board.definitions;
  const names = [...new Set([...Object.keys(definitions), ...Object.keys(board.lanes)])].toSorted();
  const lanes = names.map((lane) => {
    const entry = own(board.lanes, lane);
    const definition = own(definitions, lane);
    return {
      lane,
      owns: definition?.owns ?? [],
      status:
        definition === undefined
          ? ("undefined" as const)
          : entry === undefined
            ? ("off" as const)
            : entry.status,
      generation: entry?.generation ?? null,
      threadId: entry?.threadId ?? null,
      startedBy: entry?.startedBy ?? null,
      startedAt: entry?.startedAt ?? null,
      stoppedAt: entry?.stoppedAt ?? null,
    };
  });
  const notes = names.flatMap((lane) =>
    board.notes.filter((note) => note.lane === lane).slice(-BOARD_NOTES_PER_LANE),
  );
  return {
    lanes,
    claims: board.claims.map(({ claim, live }) => ({ ...claim, live })),
    escalations: board.outbox,
    notes,
  };
});

const actorOf = (
  input: typeof LaneClaimInput.Type,
  threadId: ThreadId,
): { readonly lane: string; readonly generation: number; readonly threadId: ThreadId } => ({
  lane: input.lane,
  generation: input.generation,
  threadId,
});

/** The item in the store's one spelling (owner/repo lower-case), or the refusal for text that is none. */
const itemOf = (text: string) => {
  const item = normalizeItem(text);
  return item === null
    ? Effect.fail(
        new LaneToolFailure({
          code: "bad-item",
          message: `'${text}' is not an item: pr:owner/repo#12 or issue:owner/repo#12.`,
        }),
      )
    : Effect.succeed(item);
};

/** `lane_claim`: the calling thread claims for its lane; a replaced thread is refused. */
export const laneClaimCall = Effect.fn("laneTools.claim")(function* (
  input: typeof LaneClaimInput.Type,
) {
  const calling = yield* callingThread();
  const item = yield* itemOf(input.item);
  const store = yield* LaneStore;
  return yield* refusedAs(
    store.claim(calling.caller.projectId, actorOf(input, calling.caller.id), item),
  );
});

export const laneReleaseCall = Effect.fn("laneTools.release")(function* (
  input: typeof LaneReleaseInput.Type,
) {
  const calling = yield* callingThread();
  const item = yield* itemOf(input.item);
  const store = yield* LaneStore;
  const released = yield* refusedAs(
    store.release(calling.caller.projectId, actorOf(input, calling.caller.id), item),
  );
  return { released };
});
