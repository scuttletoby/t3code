/**
 * Fork: the T3 tool groups each thread's MCP credential may use, fixed as each of its turns starts
 * (MCP profile design PR 4). A running turn keeps the set it started with, and a profile change
 * applies from the thread's next turn. Memory only, like the credentials it goes with: after a
 * restart each thread's set is worked out again from the settings (mcpT3ToolRule.ts).
 */
import type { McpT3ToolGroup } from "@t3tools/contracts";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";

import type { McpProfileResolved } from "./mcpProfile.ts";

/** What a thread's T3 tools may be: its profile's groups, or none when the profile cannot apply. */
export type T3ToolSet =
  | {
      readonly _tag: "Groups";
      /** The profile's name, for refusals. */
      readonly name: string;
      readonly groups: "all" | ReadonlySet<McpT3ToolGroup>;
    }
  | {
      /** The profile cannot be worked out: only the tools every profile keeps. Never `all`. */
      readonly _tag: "Refused";
      /** The resolver's plain reason, with its fix. */
      readonly reason: string;
    };

export const t3ToolSetOf = (resolved: McpProfileResolved): T3ToolSet => ({
  _tag: "Groups",
  name: resolved.name,
  groups: resolved.profile.t3Tools === "all" ? "all" : new Set(resolved.profile.t3Tools),
});

export const refusedT3ToolSet = (reason: string): T3ToolSet => ({ _tag: "Refused", reason });

/**
 * Threads kept; the oldest beyond this is dropped, and its next request works its set out again
 * from the settings then (never wider than they allow).
 */
export const MAX_T3_TOOL_SETS = 4096;

/**
 * A turn's place among the thread's turns: drawn as its check starts, so a later turn has a higher
 * one whatever order the checks finish in. One counter for every thread; only order matters.
 */
let lastTurn = 0;

/** The next turn's place, drawn as its check starts (before it reads the profile). */
export const nextT3ToolSetTurn = (): number => ++lastTurn;

/**
 * Each thread's set, with the turn that fixed it (`undefined` set: that turn stopped half-way, so
 * none stands, but an older turn's late check still may not write).
 */
const sets = new Map<string, { readonly turn: number; readonly set: T3ToolSet | undefined }>();

/**
 * The latest turn of any thread dropped at the cap. A thread with no entry may have been dropped
 * with a turn up to this one, so a check older than it writes nothing: the thread's next request
 * works its set out again from the settings instead.
 */
let droppedTurn = 0;

const keep = (threadId: string, turn: number, set: T3ToolSet | undefined) => {
  sets.delete(threadId);
  sets.set(threadId, { turn, set });
  for (const [oldest, entry] of sets) {
    if (sets.size <= MAX_T3_TOOL_SETS) break;
    droppedTurn = Math.max(droppedTurn, entry.turn);
    sets.delete(oldest);
  }
};

/** The latest turn that fixed the thread's set (or stopped half-way), or may have, if dropped. */
const lastFixed = (threadId: string) => sets.get(threadId)?.turn ?? droppedTurn;

/** True when no turn after `turn` has fixed the thread's set (or stopped half-way). */
const newest = (threadId: string, turn: number) => lastFixed(threadId) <= turn;

/**
 * A turn of the thread starts: its set from now until the next turn starts. A check that finishes
 * after a later turn's has fixed its set writes nothing: the later turn's set stands. `turn` is
 * the one drawn before the profile was read (a number drawn after would rank a stale read newest).
 */
export const fixT3ToolSet = (threadId: string, set: T3ToolSet, turn: number): void => {
  if (newest(threadId, turn)) keep(threadId, turn, set);
};

/**
 * A turn start stopped half-way: no set stands, so the next request works it out again (unless a
 * later turn fixed one meanwhile, which stands).
 */
export const forgetT3ToolSet = (threadId: string, turn: number): void => {
  if (newest(threadId, turn)) keep(threadId, turn, undefined);
};

export const t3ToolSetFor = (threadId: string): T3ToolSet | undefined => sets.get(threadId)?.set;

/**
 * A set worked out for a request before any turn fixed one (a provider that lists tools as its
 * session opens). Kept only if no turn fixed one meanwhile, so it never replaces a turn's set.
 */
export const rememberT3ToolSet = (threadId: string, set: T3ToolSet): T3ToolSet => {
  const fixed = sets.get(threadId);
  if (fixed?.set !== undefined) return fixed.set;
  // As old as the last turn: that turn's late check, or any later turn, still replaces it.
  keep(threadId, lastFixed(threadId), set);
  return set;
};

/**
 * How long a hold lasts: a request waits for it at most until then, and a hold still there after
 * that is dropped (mcpT3ToolRule.ts), so one that is never released holds the thread's requests
 * once, not until a restart.
 */
export const T3_TOOL_SET_HOLD_WAIT_MS = 10_000;

/** Threads whose next set is being worked out right now, as a turn Codex started begins. */
const holds = new Map<
  string,
  {
    readonly done: Deferred.Deferred<void>;
    /** Done once a request waits on this hold (a milestone tests await). */
    readonly asked: Deferred.Deferred<void>;
    /** When the hold stops holding: requests then stop waiting, and it is dropped. */
    readonly until: number;
    /** The latest turn holding it: a drop stands for that turn's set, not yet fixed. */
    turn: number;
    count: number;
  }
>();

/**
 * Holds the thread's T3 requests (mcpT3ToolRule.ts) until the returned release runs, so a turn
 * Codex started gets the set its check fixes. Release always; a second release does nothing. A hold
 * taken while one is in force joins it (keeping its bound); one taken after that bound starts
 * afresh. `turn` is the turn whose check takes it, drawn before that check reads the profile.
 */
export const holdT3ToolSet = (threadId: string, now: number, turn: number): (() => void) => {
  let hold = holds.get(threadId);
  if (hold === undefined || hold.until <= now) {
    hold = {
      done: Deferred.makeUnsafe<void>(),
      asked: Deferred.makeUnsafe<void>(),
      until: now + T3_TOOL_SET_HOLD_WAIT_MS,
      turn,
      count: 0,
    };
    holds.set(threadId, hold);
  }
  const held = hold;
  held.turn = Math.max(held.turn, turn);
  held.count += 1;
  let released = false;
  return () => {
    if (released) return;
    released = true;
    held.count -= 1;
    if (held.count > 0) return;
    if (holds.get(threadId) === held) holds.delete(threadId);
    Deferred.doneUnsafe(held.done, Effect.void);
  };
};

/** The thread's hold: done once every hold on it is released; wait for it at most `waitMs`. */
export interface T3ToolSetHold {
  readonly done: Deferred.Deferred<void>;
  readonly waitMs: number;
}

/** The hold on the thread's set; `undefined` when none is held. */
export const t3ToolSetHold = (threadId: string, now: number): T3ToolSetHold | undefined => {
  const hold = holds.get(threadId);
  if (hold === undefined) return undefined;
  Deferred.doneUnsafe(hold.asked, Effect.void);
  return { done: hold.done, waitMs: Math.max(0, hold.until - now) };
};

/**
 * A hold its bound passed: dropped, and the thread keeps `set` (never the one before the hold)
 * until a turn, or the late check, fixes one. False when it was released meanwhile, or when the
 * hold's latest turn or a later one has fixed its set (or may have, if dropped at the cap).
 */
export const dropT3ToolSetHold = (
  threadId: string,
  hold: T3ToolSetHold,
  set: T3ToolSet,
): boolean => {
  const held = holds.get(threadId);
  if (held?.done !== hold.done) return false;
  holds.delete(threadId);
  if (lastFixed(threadId) >= held.turn) return false;
  // As the hold's latest turn: that turn's late check still replaces it; an older one does not.
  keep(threadId, held.turn, set);
  return true;
};

export const __testing = {
  clear: () => {
    sets.clear();
    holds.clear();
    droppedTurn = 0;
  },
  size: () => sets.size,
  /** Done once a request waits on the thread's hold; `undefined` when none is held. */
  asked: (threadId: string) => holds.get(threadId)?.asked,
};
