/**
 * Fork (issue 1156, phase 3): the lane tools over the lane store. Registered in the waits toolkit
 * (mcp/toolkits/waits), in the `lanes` T3 tool group, and listed only while `server.lanes.enabled`
 * is on (laneToolsVisibility.ts). The calling thread and its project come from the server's record
 * of the thread, never from input.
 */
import {
  LaneClaim,
  type LaneDefinition,
  LaneEscalation,
  LaneGeneration,
  LaneItemKind,
  LaneNote,
  laneDefinitions,
  OrchestratorMcpFailure,
  ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import { callingThread as threadOfCaller } from "../../mcp/toolkits/waits/callingThread.ts";
import { unavailable } from "../../mcp/threadAccess.ts";
import { ServerSettingsService } from "../../serverSettings.ts";
import { LaneStore, type LaneStoreRefused } from "./LaneStore.ts";
import { type LaneRefusalReason, normalizeItem, own } from "./laneState.ts";

/** Notes per lane the board shows, newest last; the store keeps more. */
export const BOARD_NOTES_PER_LANE = 3;

const LANE_REFUSAL_REASONS = [
  "lanes-off",
  "store-unreadable",
  "unknown-lane",
  "not-running",
  "already-running",
  "stale-owner",
  "kind-not-owned",
  "bad-item",
  "bad-input",
  "claimed",
  "not-holder",
  "outbox-full",
] as const satisfies ReadonlyArray<LaneRefusalReason>;

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

const failure = (refusal: LaneStoreRefused) =>
  new LaneToolFailure({
    code: refusal.reason as LaneToolFailure["code"],
    message: refusal.detail,
    ...(refusal.holder === undefined ? {} : { holder: refusal.holder as LaneClaim }),
  });

const refusedAs = <A, R>(step: Effect.Effect<A, LaneStoreRefused, R>) =>
  step.pipe(Effect.mapError(failure));

/** `lane_start`: the thread must be a live thread of the caller's project. */
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
  const entry = yield* refusedAs(store.startLane(projectId, input.lane, input.threadId));
  return {
    lane: input.lane,
    generation: entry.generation,
    threadId: entry.threadId,
    startedAt: entry.startedAt,
    next: `Tell thread ${entry.threadId} it acts for lane '${input.lane}' at generation ${entry.generation}: lane_claim and lane_release take both.`,
  };
});

export const laneStopCall = Effect.fn("laneTools.stop")(function* (
  input: typeof LaneStopInput.Type,
) {
  const calling = yield* callingThread();
  const store = yield* LaneStore;
  const released = yield* refusedAs(store.stopLane(calling.caller.projectId, input.lane));
  return { lane: input.lane, released: released.map((claim) => claim.item) };
});

export const laneBoardCall = Effect.fn("laneTools.board")(function* () {
  const calling = yield* callingThread();
  const store = yield* LaneStore;
  const board = yield* refusedAs(store.board(calling.caller.projectId));
  // The store read the settings an instant ago; a read error now only drops the defined-but-off rows.
  const definitions: Readonly<Record<string, LaneDefinition>> = yield* ServerSettingsService.pipe(
    Effect.flatMap((service) => service.getSettings),
    Effect.map((settings) => laneDefinitions(settings.lanes)),
    Effect.orElseSucceed(() => ({})),
  );
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
