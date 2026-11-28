/**
 * Fork (issue 1156, phase 2): one project's lane file (registry with generations, item claims,
 * outbox, notes) and the pure steps that change it; LaneStore.ts runs them. Docs:
 * docs/fork/README.md, `server.lanes`.
 */
import {
  type LaneClaim,
  type LaneDefinition,
  LaneClaim as LaneClaimSchema,
  type LaneEscalation,
  LaneEscalation as LaneEscalationSchema,
  type LaneItemKind,
  type LaneNote,
  LaneNote as LaneNoteSchema,
  formatLaneItemRef,
  parseLaneItemRef,
  ProjectId,
  ThreadId,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";

const LANE_STATE_VERSION = 1;
/** Notes kept per lane, newest last; older ones drop. */
export const NOTES_PER_LANE = 50;
/** Escalations waiting for delivery; one more is refused (never dropped) until some are delivered. */
const OUTBOX_LIMIT = 200;
/** Threads remembered per lane (run records find a lane by any of them). */
const THREADS_PER_LANE = 50;

const LaneThread = Schema.Struct({
  generation: Schema.Number,
  threadId: ThreadId,
  at: Schema.String,
});

/** One lane of the project: running or stopped, and the thread of its current generation. */
const LaneEntry = Schema.Struct({
  status: Schema.Literals(["running", "stopped"]),
  /** Bumped by every start and every replacement; a number is never used twice. */
  generation: Schema.Number,
  threadId: ThreadId,
  startedAt: Schema.String,
  /**
   * The thread that started it (an orchestrator): while it is a live thread of the project, it
   * alone (or the owner) stops the lane. Absent (the owner started it, or an older server rewrote
   * the file and dropped the key): no starter to wait for, so any thread of the project stops it.
   */
  startedBy: Schema.optionalKey(ThreadId),
  stoppedAt: Schema.NullOr(Schema.String),
  /** Every thread that acted as this lane, oldest first, at most {@link THREADS_PER_LANE}. */
  threads: Schema.Array(LaneThread),
});
export type LaneEntry = typeof LaneEntry.Type;

/** The lane file of one project, version 1. */
const LaneProjectStateSchema = Schema.Struct({
  v: Schema.Literal(LANE_STATE_VERSION),
  projectId: ProjectId,
  lanes: Schema.Record(Schema.String, LaneEntry),
  /** By item reference. */
  claims: Schema.Record(Schema.String, LaneClaimSchema),
  /** Escalations not yet delivered, oldest first. */
  outbox: Schema.Array(LaneEscalationSchema),
  notes: Schema.Array(LaneNoteSchema),
});
export type LaneProjectState = typeof LaneProjectStateSchema.Type;

export const emptyLaneState = (projectId: ProjectId): LaneProjectState => ({
  v: LANE_STATE_VERSION,
  projectId,
  lanes: {},
  claims: {},
  outbox: [],
  notes: [],
});

const decodeState = Schema.decodeUnknownOption(LaneProjectStateSchema);

/**
 * The file's state, or null when it cannot be trusted: not JSON, another version (a newer fork's)
 * or any part that does not decode. Null is unknown, not empty: the store refuses every change and
 * leaves the file as it is.
 */
export function decodeLaneState(text: string, projectId: ProjectId): LaneProjectState | null {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    return null;
  }
  const decoded = decodeState(raw);
  if (decoded._tag === "None") return null;
  const state = decoded.value;
  // Every record names this project, and each claim sits under its own item's key.
  const records = [...Object.values(state.claims), ...state.outbox, ...state.notes];
  if (state.projectId !== projectId || records.some((record) => record.projectId !== projectId)) {
    return null;
  }
  if (Object.entries(state.claims).some(([key, claim]) => key !== claim.item)) return null;
  return state;
}

/** `record[key]` only for the record's own key, never an inherited one (`constructor`). */
export const own = <V>(record: { readonly [key: string]: V }, key: string): V | undefined =>
  Object.hasOwn(record, key) ? record[key] : undefined;

/** An item reference in one spelling (owner and repository lower-case), or null for no item. */
export function normalizeItem(text: string): string | null {
  const parsed = parseLaneItemRef(text.trim());
  return parsed === null ? null : formatLaneItemRef(parsed);
}

/** Escalation ids are stored trimmed; compare them the same way. */
const normalizeEscalationId = (id: string): string => id.trim();

export const encodeLaneState = (state: LaneProjectState): string =>
  `${JSON.stringify(state, null, 2)}\n`;

/** Why the store refused a step. */
export type LaneRefusalReason =
  /** `server.lanes.enabled` is off. */
  | "lanes-off"
  /** The server settings cannot be read now, so whether lanes are on is unknown. */
  | "settings-unreadable"
  /** The project's lane file exists but cannot be read or decoded. */
  | "store-unreadable"
  /** The project's lane file cannot be written now (a full disk, no permission). */
  | "store-unwritable"
  /** The settings do not define the lane. */
  | "unknown-lane"
  | "not-running"
  | "already-running"
  /** The caller is not the lane's current thread and generation (a replaced or old thread). */
  | "stale-owner"
  /** A thread named as the lane's thread is the thread that starts the lane. */
  | "self-lane"
  /** Only the thread that started a lane (while it is a live thread of the project), or the owner, stops it. */
  | "not-starter"
  /** The lane's definition does not own this kind of item. */
  | "kind-not-owned"
  | "bad-item"
  /** A note's or escalation's text or id is empty or too long, or an item in it is not one. */
  | "bad-input"
  /** Another running lane holds the item. */
  | "claimed"
  | "not-holder"
  | "outbox-full";

export interface LaneRefusal {
  readonly _tag: "refused";
  readonly reason: LaneRefusalReason;
  readonly message: string;
  /** For `claimed`: the claim that stands. */
  readonly holder?: LaneClaim;
}

export interface LaneStep<A> {
  readonly _tag: "ok";
  readonly state: LaneProjectState;
  /** False when the step changed nothing (a repeat): the store skips the write. */
  readonly changed: boolean;
  readonly value: A;
}

export type LaneStepResult<A> = LaneStep<A> | LaneRefusal;

export const refuse = (
  reason: LaneRefusalReason,
  message: string,
  holder?: LaneClaim,
): LaneRefusal => ({
  _tag: "refused",
  reason,
  message,
  ...(holder !== undefined ? { holder } : {}),
});

const ok = <A>(state: LaneProjectState, value: A, changed = true): LaneStep<A> => ({
  _tag: "ok",
  state,
  changed,
  value,
});

/** The lanes the settings define, by name. */
export type LaneDefinitions = { readonly [name: string]: LaneDefinition };

/**
 * Who starts or stops a lane: a thread (an orchestrator, through the lane tools), or the owner.
 * `goneStarter`: a starter the caller found is no longer a live thread of the project; a stop
 * passes the starter check only while the lane still names that same starter.
 */
export type LaneOperator =
  | { readonly _tag: "thread"; readonly threadId: ThreadId; readonly goneStarter?: ThreadId }
  | { readonly _tag: "owner" };

const definitionOf = (definitions: LaneDefinitions, lane: string): LaneDefinition | undefined =>
  own(definitions, lane);

/** Who asks to act as a lane: the lane, the generation it was told, and its own thread. */
export interface LaneActor {
  readonly lane: string;
  readonly generation: number;
  readonly threadId: ThreadId;
}

/** The lane, running and defined, with `actor` as its current thread; else the refusal. */
function checkActor(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  actor: LaneActor,
): { readonly entry: LaneEntry; readonly definition: LaneDefinition } | LaneRefusal {
  const definition = definitionOf(definitions, actor.lane);
  if (definition === undefined) {
    return refuse("unknown-lane", `The settings do not define the lane '${actor.lane}'.`);
  }
  const entry = own(state.lanes, actor.lane);
  if (entry === undefined || entry.status !== "running") {
    return refuse("not-running", `The lane '${actor.lane}' is not running.`);
  }
  if (entry.generation !== actor.generation || entry.threadId !== actor.threadId) {
    return refuse(
      "stale-owner",
      `Thread ${actor.threadId} (generation ${actor.generation}) no longer acts for the lane '${actor.lane}': generation ${entry.generation} does.`,
    );
  }
  return { entry, definition };
}

/**
 * `value` checked against the schema the file is read with, so a record the next read would refuse
 * (and with it the whole file) is never written.
 */
function checked<A>(
  decode: (
    value: unknown,
  ) => { readonly _tag: "Some"; readonly value: A } | { readonly _tag: "None" },
  value: unknown,
  what: string,
): A | LaneRefusal {
  const decoded = decode(value);
  return decoded._tag === "Some"
    ? decoded.value
    : refuse(
        "bad-input",
        `The ${what} is not valid: text 1-2000 characters, items as pr:owner/repo#12.`,
      );
}

const decodeClaim = Schema.decodeUnknownOption(LaneClaimSchema);
const decodeNote = Schema.decodeUnknownOption(LaneNoteSchema);
const decodeEscalation = Schema.decodeUnknownOption(LaneEscalationSchema);

/** Each item in one spelling; text that is no item is kept as it is, so the record check refuses it. */
const normalizeItems = (items: ReadonlyArray<string>): ReadonlyArray<string> =>
  items.map((item) => normalizeItem(item) ?? item);

const isRefusal = (value: unknown): value is LaneRefusal =>
  typeof value === "object" && value !== null && (value as LaneRefusal)._tag === "refused";

/**
 * A claim counts while its lane is defined, running and still owns the item's kind: it then belongs
 * to the lane's current thread, whatever thread made it. Any other claim is stale and the next
 * claim on the item takes it.
 */
export function claimIsLive(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  claim: LaneClaim,
): boolean {
  const entry = own(state.lanes, claim.lane);
  const definition = definitionOf(definitions, claim.lane);
  const kind = parseLaneItemRef(claim.item)?.kind;
  return (
    entry !== undefined &&
    entry.status === "running" &&
    definition !== undefined &&
    kind !== undefined &&
    definition.owns.includes(kind)
  );
}

const rememberThread = (
  threads: LaneEntry["threads"],
  generation: number,
  threadId: ThreadId,
  at: string,
): LaneEntry["threads"] => [...threads, { generation, threadId, at }].slice(-THREADS_PER_LANE);

/**
 * Switches a defined lane on with `threadId` as its thread, at the next generation. Repeating the
 * same start (the same thread, still running) changes nothing.
 */
export function startLane(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: {
    readonly lane: string;
    readonly threadId: ThreadId;
    readonly by: LaneOperator;
    readonly now: string;
  },
): LaneStepResult<LaneEntry> {
  if (definitionOf(definitions, input.lane) === undefined) {
    return refuse("unknown-lane", `The settings do not define the lane '${input.lane}'.`);
  }
  // The starter stops the lane; as the lane's thread too, it could stop and claim at once.
  if (input.by._tag === "thread" && input.by.threadId === input.threadId) {
    return refuse(
      "self-lane",
      `Thread ${input.threadId} starts the lane '${input.lane}', so it cannot be the lane's thread: name another thread of the project.`,
    );
  }
  const current = own(state.lanes, input.lane);
  if (current?.status === "running") {
    return current.threadId === input.threadId
      ? ok(state, current, false)
      : refuse(
          "already-running",
          `The lane '${input.lane}' already runs in thread ${current.threadId} (generation ${current.generation}).`,
        );
  }
  // One lane per thread: run records and claims name a thread's lane by its thread.
  const busy = Object.entries(state.lanes).find(
    ([lane, entry]) =>
      lane !== input.lane && entry.status === "running" && entry.threadId === input.threadId,
  );
  if (busy !== undefined) {
    return refuse(
      "already-running",
      `Thread ${input.threadId} already acts for the lane '${busy[0]}'; a lane needs its own thread.`,
    );
  }
  const generation = (current?.generation ?? 0) + 1;
  const entry: LaneEntry = {
    status: "running",
    generation,
    threadId: input.threadId,
    startedAt: input.now,
    ...(input.by._tag === "thread" ? { startedBy: input.by.threadId } : {}),
    stoppedAt: null,
    threads: rememberThread(current?.threads ?? [], generation, input.threadId, input.now),
  };
  return ok({ ...state, lanes: { ...state.lanes, [input.lane]: entry } }, entry);
}

/**
 * Gives a running lane a new thread (its old one died or was switched out): the next generation,
 * so the old thread can no longer act for the lane. The lane's claims follow it. `fromGeneration`
 * is the generation being replaced; a repeat after it already happened (the same new thread)
 * changes nothing, and a replacement of any other generation is refused.
 */
export function replaceLaneThread(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: {
    readonly lane: string;
    readonly fromGeneration: number;
    readonly threadId: ThreadId;
    readonly now: string;
  },
): LaneStepResult<LaneEntry> {
  if (definitionOf(definitions, input.lane) === undefined) {
    return refuse("unknown-lane", `The settings do not define the lane '${input.lane}'.`);
  }
  const current = own(state.lanes, input.lane);
  if (current === undefined || current.status !== "running") {
    return refuse("not-running", `The lane '${input.lane}' is not running.`);
  }
  if (current.generation === input.fromGeneration + 1 && current.threadId === input.threadId) {
    return ok(state, current, false);
  }
  if (current.generation !== input.fromGeneration) {
    return refuse(
      "stale-owner",
      `The lane '${input.lane}' is at generation ${current.generation}, not ${input.fromGeneration}.`,
    );
  }
  if (current.startedBy === input.threadId) {
    return refuse(
      "self-lane",
      `Thread ${input.threadId} started the lane '${input.lane}', so it cannot be the lane's thread.`,
    );
  }
  const generation = current.generation + 1;
  const entry: LaneEntry = {
    ...current,
    generation,
    threadId: input.threadId,
    threads: rememberThread(current.threads, generation, input.threadId, input.now),
  };
  const claims = Object.fromEntries(
    Object.entries(state.claims).map(([item, claim]) => [
      item,
      claim.lane === input.lane
        ? { ...claim, generation: generation as LaneClaim["generation"], threadId: input.threadId }
        : claim,
    ]),
  );
  return ok({ ...state, lanes: { ...state.lanes, [input.lane]: entry }, claims }, entry);
}

/**
 * Switches a lane off and releases its claims (returned). A lane not running, or no longer
 * defined, stops too: stopping is how its claims go. Repeating it changes nothing. Only the
 * thread that started the lane, or the owner, stops it.
 */
export function stopLane(
  state: LaneProjectState,
  input: { readonly lane: string; readonly by: LaneOperator; readonly now: string },
): LaneStepResult<ReadonlyArray<LaneClaim>> {
  const current = own(state.lanes, input.lane);
  const starter = current?.startedBy;
  if (
    starter !== undefined &&
    input.by._tag === "thread" &&
    starter !== input.by.threadId &&
    starter !== input.by.goneStarter
  ) {
    return refuse(
      "not-starter",
      `Thread ${starter} started the lane '${input.lane}'; while it is a live thread of the project, only it (or the owner) stops it.`,
    );
  }
  const released = Object.values(state.claims).filter((claim) => claim.lane === input.lane);
  if ((current === undefined || current.status === "stopped") && released.length === 0) {
    return ok(state, [], false);
  }
  const claims = Object.fromEntries(
    Object.entries(state.claims).filter(([, claim]) => claim.lane !== input.lane),
  );
  const lanes =
    current === undefined
      ? state.lanes
      : {
          ...state.lanes,
          [input.lane]: {
            ...current,
            status: "stopped" as const,
            stoppedAt: current.stoppedAt ?? input.now,
          },
        };
  return ok({ ...state, lanes, claims }, released);
}

export interface ClaimOutcome {
  readonly claim: LaneClaim;
  /** The stale claim this one replaced (its lane stopped or is no longer defined). */
  readonly takenFrom: LaneClaim | null;
}

/**
 * Claims an item for the actor's lane. One owner per item: a live claim of another lane refuses
 * it; a stale one is replaced; the lane's own claim is kept as it is (a repeat).
 */
export function claimItem(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: { readonly actor: LaneActor; readonly item: string; readonly now: string },
): LaneStepResult<ClaimOutcome> {
  const actor = checkActor(state, definitions, input.actor);
  if (isRefusal(actor)) return actor;
  const parsed = parseLaneItemRef(input.item.trim());
  const item = parsed === null ? null : formatLaneItemRef(parsed);
  if (parsed === null || item === null) {
    return refuse(
      "bad-item",
      `'${input.item}' is not an item: pr:owner/repo#12 or issue:owner/repo#12.`,
    );
  }
  if (!actor.definition.owns.includes(parsed.kind as LaneItemKind)) {
    return refuse(
      "kind-not-owned",
      `The lane '${input.actor.lane}' does not own items of kind '${parsed.kind}'.`,
    );
  }
  const existing = own(state.claims, item);
  if (existing !== undefined && existing.lane === input.actor.lane) {
    return ok(state, { claim: existing, takenFrom: null }, false);
  }
  if (existing !== undefined && claimIsLive(state, definitions, existing)) {
    return refuse(
      "claimed",
      `${item} is claimed by the lane '${existing.lane}' (thread ${existing.threadId}).`,
      existing,
    );
  }
  const claim = checked(
    decodeClaim,
    {
      item,
      projectId: state.projectId,
      lane: input.actor.lane,
      generation: input.actor.generation,
      threadId: input.actor.threadId,
      claimedAt: input.now,
    },
    "claim",
  );
  if (isRefusal(claim)) return claim;
  return ok(
    { ...state, claims: { ...state.claims, [item]: claim } },
    { claim, takenFrom: existing ?? null },
  );
}

/** Releases the actor's lane's claim on an item; an item it does not hold is refused. */
export function releaseItem(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: { readonly actor: LaneActor; readonly item: string },
): LaneStepResult<LaneClaim | null> {
  const actor = checkActor(state, definitions, input.actor);
  if (isRefusal(actor)) return actor;
  const item = normalizeItem(input.item);
  if (item === null) {
    return refuse(
      "bad-item",
      `'${input.item}' is not an item: pr:owner/repo#12 or issue:owner/repo#12.`,
    );
  }
  const existing = own(state.claims, item);
  if (existing === undefined) return ok(state, null, false);
  if (existing.lane !== input.actor.lane) {
    return refuse("not-holder", `${item} is claimed by the lane '${existing.lane}'.`, existing);
  }
  const claims = Object.fromEntries(Object.entries(state.claims).filter(([key]) => key !== item));
  return ok({ ...state, claims }, existing);
}

/** Adds a note to the board (it wakes nobody); each lane keeps its last {@link NOTES_PER_LANE}. */
export function addNote(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: {
    readonly actor: LaneActor;
    readonly text: string;
    readonly items: ReadonlyArray<string>;
    readonly now: string;
  },
): LaneStepResult<LaneNote> {
  const actor = checkActor(state, definitions, input.actor);
  if (isRefusal(actor)) return actor;
  const note = checked(
    decodeNote,
    {
      projectId: state.projectId,
      lane: input.actor.lane,
      generation: input.actor.generation,
      threadId: input.actor.threadId,
      at: input.now,
      text: input.text,
      items: normalizeItems(input.items),
    },
    "note",
  );
  if (isRefusal(note)) return note;
  const own = state.notes.filter((entry) => entry.lane === input.actor.lane);
  const drop = new Set(own.slice(0, Math.max(0, own.length + 1 - NOTES_PER_LANE)));
  const notes = [...state.notes.filter((entry) => !drop.has(entry)), note];
  return ok({ ...state, notes }, note);
}

/**
 * Puts an escalation in the outbox for the main orchestrator. The same lane and trimmed `id` again
 * while it waits (a repeat) changes nothing; once acked, the id is free again. A full outbox
 * refuses a new one rather than drop one.
 */
export function addEscalation(
  state: LaneProjectState,
  definitions: LaneDefinitions,
  input: {
    readonly actor: LaneActor;
    readonly id: string;
    readonly text: string;
    readonly items: ReadonlyArray<string>;
    readonly now: string;
  },
): LaneStepResult<LaneEscalation> {
  const actor = checkActor(state, definitions, input.actor);
  if (isRefusal(actor)) return actor;
  const id = normalizeEscalationId(input.id);
  // One escalation per lane and id: a repeat changes nothing; another lane's same id is its own.
  const repeat = state.outbox.find((entry) => entry.lane === input.actor.lane && entry.id === id);
  if (repeat !== undefined) return ok(state, repeat, false);
  if (state.outbox.length >= OUTBOX_LIMIT) {
    return refuse(
      "outbox-full",
      `${OUTBOX_LIMIT} escalations wait for delivery; deliver some before adding more.`,
    );
  }
  const escalation = checked(
    decodeEscalation,
    {
      id,
      projectId: state.projectId,
      lane: input.actor.lane,
      generation: input.actor.generation,
      threadId: input.actor.threadId,
      at: input.now,
      text: input.text,
      items: normalizeItems(input.items),
    },
    "escalation",
  );
  if (isRefusal(escalation)) return escalation;
  return ok({ ...state, outbox: [...state.outbox, escalation] }, escalation);
}

/** An escalation's key in the outbox: its lane and its trimmed id. */
export interface EscalationKey {
  readonly lane: string;
  readonly id: string;
}

const escalationKey = (key: EscalationKey) =>
  JSON.stringify([key.lane, normalizeEscalationId(key.id)]);

/** Removes delivered escalations from the outbox; a key not there (a repeat) is ignored. */
export function ackEscalations(
  state: LaneProjectState,
  keys: ReadonlyArray<EscalationKey>,
): LaneStepResult<number> {
  const done = new Set(keys.map(escalationKey));
  const outbox = state.outbox.filter((entry) => !done.has(escalationKey(entry)));
  const removed = state.outbox.length - outbox.length;
  return removed === 0 ? ok(state, 0, false) : ok({ ...state, outbox }, removed);
}

/**
 * The lane a thread acted for (by any generation) when a run of it ended at `endedAt`, or null.
 * A stopped lane counts only for runs that ended by its stop.
 */
export function laneOfThread(
  state: LaneProjectState,
  threadId: string,
  endedAt: string,
): string | null {
  for (const [lane, entry] of Object.entries(state.lanes)) {
    const acted = entry.threadId === threadId || entry.threads.some((t) => t.threadId === threadId);
    const inTime =
      entry.status === "running" || (entry.stoppedAt !== null && endedAt <= entry.stoppedAt);
    if (acted && inTime) return lane;
  }
  return null;
}
