/**
 * Fork: whether a thread hands off to a fresh session now, later or never. Pure: the caller (the
 * handoff service, a later PR) reads the facts; the same facts always give the same answer, so a
 * second call for the same run is harmless. Nothing here sends or changes anything.
 */
import { HANDOFF_MIN_TOKENS, type HandoffMode, type HandoffSource } from "@t3tools/contracts";

/** The agent writes its handoff in the warm cache, so it starts earlier than a command. */
export const HANDOFF_IDLE_LEAD_MS: Readonly<Record<HandoffSource, number>> = {
  agent: 4 * 60_000,
  command: 2 * 60_000,
};

/** `turn-end`: a run finished. `idle-timer`: the timer set at a turn end fired. `asked`: a person. */
export type HandoffTrigger = "turn-end" | "idle-timer" | "asked";

/** What keeps a thread from being quiet; `detail` names it for the "handoff waiting on" chip. */
export type HandoffBlocker =
  | { readonly kind: "run-active" }
  | { readonly kind: "request-pending" }
  | { readonly kind: "child-result-waiting" }
  | { readonly kind: "fork-or-merge" }
  | { readonly kind: "background-work"; readonly detail: string }
  | { readonly kind: "goal-active" };

export interface HandoffFacts {
  readonly trigger: HandoffTrigger;
  /** The run the trigger is about: the one that ended, or the one the idle timer was set for. */
  readonly runId: string;
  readonly threadId: string;
  /** The thread's resolved mode (`resolveHandoffSettings`). */
  readonly mode: HandoffMode;
  readonly source: HandoffSource;
  readonly sizeTokens: number;
  /** 0: never at the idle trigger. */
  readonly idleTokens: number;
  /** False for an archived, deleted or unreadable thread. */
  readonly threadAvailable: boolean;
  /** The thread's newest run; absent when it has none. */
  readonly lastRun?:
    | {
        readonly id: string;
        /** `handoff`: a handoff's fresh run. `handoff-write`: the agent writing its handoff. */
        readonly kind: "turn" | "handoff" | "handoff-write" | "compact";
        readonly state: "completed" | "failed" | "interrupted";
      }
    | undefined;
  /** The last provider turn's `usedTokens`; absent when the provider reports none. */
  readonly usedTokens?: number | undefined;
  /** When the prompt cache expires (ms); absent when unknown. */
  readonly cacheExpiresAtMs?: number | undefined;
  readonly nowMs: number;
  /** Empty when the thread is quiet. Running T3 children never block, so they are not listed. */
  readonly blockers: ReadonlyArray<HandoffBlocker>;
}

export type HandoffSkipReason =
  | "mode-not-handoff"
  | "thread-unavailable"
  | "no-runs"
  | "newer-run"
  | "last-run-handoff"
  | "last-run-compact"
  | "last-run-not-completed"
  | "no-size"
  | "too-small"
  | "idle-off"
  | "cache-expiry-unknown"
  | "cache-expired"
  | "busy";

export type HandoffDecision =
  /** Nothing now; the next turn end decides again. */
  | { readonly kind: "skip"; readonly reason: HandoffSkipReason }
  /** A handoff is due but the thread is not quiet: show what it waits on, try at the next turn end. */
  | { readonly kind: "wait"; readonly blockers: ReadonlyArray<HandoffBlocker> }
  /** Set the thread's one idle timer (it replaces an older one), then decide again. */
  | { readonly kind: "sleep"; readonly untilMs: number }
  /** Hand off now. Ids are per run, so every trigger for one run shares them: only one runs. */
  | {
      readonly kind: "handoff";
      readonly reason: "size" | "idle" | "asked";
      readonly commandId: string;
      readonly messageId: string;
    };

const skip = (reason: HandoffSkipReason): HandoffDecision => ({ kind: "skip", reason });

export function handoffDecision(facts: HandoffFacts): HandoffDecision {
  if (facts.mode !== "handoff") return skip("mode-not-handoff");
  if (!facts.threadAvailable) return skip("thread-unavailable");
  const run = facts.lastRun;
  if (run === undefined) return skip("no-runs");
  // A newer run replaces this trigger: its own turn end decides.
  if (run.id !== facts.runId) return skip("newer-run");
  if (run.kind === "handoff" || run.kind === "handoff-write") return skip("last-run-handoff");
  // Its size may still be the one before the compaction.
  if (run.kind === "compact") return skip("last-run-compact");
  const asked = facts.trigger === "asked";
  // A failed run (a usage limit too) or an interrupted one is never handed off unasked.
  if (!asked && run.state !== "completed") return skip("last-run-not-completed");
  const size = facts.usedTokens;
  if (size === undefined && !asked) return skip("no-size");
  if (size !== undefined && size < HANDOFF_MIN_TOKENS) return skip("too-small");

  const handoff = (reason: "size" | "idle" | "asked"): HandoffDecision =>
    facts.blockers.length > 0
      ? { kind: "wait", blockers: facts.blockers }
      : {
          kind: "handoff",
          reason,
          commandId: `server:handoff:${facts.threadId}:${run.id}`,
          messageId: `handoff:${facts.threadId}:${run.id}`,
        };
  if (asked) return handoff("asked");
  if (facts.trigger === "turn-end" && (size ?? 0) >= facts.sizeTokens) return handoff("size");

  // The idle trigger: only while the cache is warm, and only at a quiet thread.
  if (facts.idleTokens === 0) return skip("idle-off");
  if ((size ?? 0) < facts.idleTokens) return skip("too-small");
  if (facts.cacheExpiresAtMs === undefined) return skip("cache-expiry-unknown");
  if (facts.nowMs >= facts.cacheExpiresAtMs) return skip("cache-expired");
  const windowStartMs = facts.cacheExpiresAtMs - HANDOFF_IDLE_LEAD_MS[facts.source];
  // The timer reads the thread again then; a busy thread now may be quiet by then.
  if (facts.nowMs < windowStartMs) return { kind: "sleep", untilMs: windowStartMs };
  // Inside the window a busy thread is not waited for: its next turn end starts over.
  return facts.blockers.length > 0 ? skip("busy") : handoff("idle");
}
