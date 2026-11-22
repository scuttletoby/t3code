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

const sets = new Map<string, T3ToolSet>();

const keep = (threadId: string, set: T3ToolSet) => {
  sets.delete(threadId);
  sets.set(threadId, set);
  for (const oldest of sets.keys()) {
    if (sets.size <= MAX_T3_TOOL_SETS) break;
    sets.delete(oldest);
  }
};

/** A turn of the thread starts: its set from now until the next turn starts. */
export const fixT3ToolSet = (threadId: string, set: T3ToolSet): void => keep(threadId, set);

/** A turn start stopped half-way: no set stands, so the next request works it out again. */
export const forgetT3ToolSet = (threadId: string): void => void sets.delete(threadId);

export const t3ToolSetFor = (threadId: string): T3ToolSet | undefined => sets.get(threadId);

/**
 * A set worked out for a request before any turn fixed one (a provider that lists tools as its
 * session opens). Kept only if no turn fixed one meanwhile, so it never replaces a turn's set.
 */
export const rememberT3ToolSet = (threadId: string, set: T3ToolSet): T3ToolSet => {
  const fixed = sets.get(threadId);
  if (fixed !== undefined) return fixed;
  keep(threadId, set);
  return set;
};

/** Threads whose next set is being worked out right now, as a turn Codex started begins. */
const holds = new Map<
  string,
  {
    readonly done: Deferred.Deferred<void>;
    /** Done once a request waits on this hold (a milestone tests await). */
    readonly asked: Deferred.Deferred<void>;
    count: number;
  }
>();

/**
 * A turn that started before T3 could fix its set (Codex starts a goal's next turn by itself):
 * the thread's T3 requests wait (mcpT3ToolRule.ts) until the returned release runs, so the turn's
 * first request already gets the set its check fixes, not the one before. Release once the set is
 * fixed, and always (also on failure or interruption); a second release does nothing.
 */
export const holdT3ToolSet = (threadId: string): (() => void) => {
  let hold = holds.get(threadId);
  if (hold === undefined) {
    hold = { done: Deferred.makeUnsafe<void>(), asked: Deferred.makeUnsafe<void>(), count: 0 };
    holds.set(threadId, hold);
  }
  const held = hold;
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

/** Done once every hold on the thread's set is released; `undefined` when none is held. */
export const t3ToolSetHold = (threadId: string): Deferred.Deferred<void> | undefined => {
  const hold = holds.get(threadId);
  if (hold === undefined) return undefined;
  Deferred.doneUnsafe(hold.asked, Effect.void);
  return hold.done;
};

export const __testing = {
  clear: () => {
    sets.clear();
    holds.clear();
  },
  size: () => sets.size,
  /** Done once a request waits on the thread's hold; `undefined` when none is held. */
  asked: (threadId: string) => holds.get(threadId)?.asked,
};
