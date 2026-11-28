/**
 * Fork (work panel, fork issue 1136): the one RPC the thread side panel's work panel reads from a
 * server. Each part carries its own `state`: `unknown` with a reason when the server has no answer
 * for it, never a guessed count. Every field a later part adds is optional, so an older or newer
 * server still decodes. Nothing here is stored: the server's shared cache lives in memory.
 */
import * as Rpc from "effect/rpc/Rpc";
import * as Schema from "effect/Schema";

import { EnvironmentAuthorizationError } from "./auth.ts";
import { NonNegativeInt, PositiveInt, ThreadId, TrimmedNonEmptyString } from "./baseSchemas.ts";

/** The method name. Not in upstream's `WS_METHODS`: it is the fork's own. */
export const FORK_WORK_PANEL_METHOD = "fork.workPanel" as const;

/** Read for one thread: the server reads that thread's family (its root, forks and sub-agents). */
export const ForkWorkPanelInput = Schema.Struct({ threadId: ThreadId });
export type ForkWorkPanelInput = typeof ForkWorkPanelInput.Type;

export const ForkWorkPanelPartState = Schema.Literals(["answered", "unknown"]);
export type ForkWorkPanelPartState = typeof ForkWorkPanelPartState.Type;

/**
 * One repository's queue line: workflow runs queued and running across its workflows, counted
 * over the latest runs GitHub lists (`scanned`), and the oldest queued run.
 */
export const ForkRepoQueue = Schema.Struct({
  host: TrimmedNonEmptyString,
  repository: TrimmedNonEmptyString,
  state: ForkWorkPanelPartState,
  /** Why there are no counts, or why the counts shown are from an older read. */
  reason: Schema.optionalKey(Schema.String),
  /** When GitHub answered the read the counts come from (ms since the epoch). */
  readAt: Schema.optionalKey(NonNegativeInt),
  queued: Schema.optionalKey(NonNegativeInt),
  running: Schema.optionalKey(NonNegativeInt),
  /** When the oldest queued run was created (ms); `null`: none is queued. */
  oldestQueuedAt: Schema.optionalKey(Schema.NullOr(NonNegativeInt)),
  oldestQueuedUrl: Schema.optionalKey(Schema.NullOr(Schema.String)),
  /** How many of the latest runs the counts cover. */
  scanned: Schema.optionalKey(NonNegativeInt),
  /** The earliest time the server asks GitHub again (ms): later after a low budget or a refusal. */
  nextReadAt: Schema.optionalKey(NonNegativeInt),
});
export type ForkRepoQueue = typeof ForkRepoQueue.Type;

/**
 * One linked pull request's runs on its head, from the same shared repository read (no call of
 * its own): the latest run of each workflow, by state.
 */
export const ForkPullRequestRuns = Schema.Struct({
  host: TrimmedNonEmptyString,
  repository: TrimmedNonEmptyString,
  number: PositiveInt,
  queued: NonNegativeInt,
  running: NonNegativeInt,
  failed: NonNegativeInt,
  passed: NonNegativeInt,
  /** When its oldest queued run was created (ms); `null`: none is queued. */
  oldestQueuedAt: Schema.NullOr(NonNegativeInt),
  /** The run to open: the oldest queued, else a running, else a failed, else the latest one. */
  url: Schema.NullOr(Schema.String),
});
export type ForkPullRequestRuns = typeof ForkPullRequestRuns.Type;

export const ForkWorkPanelAnswer = Schema.Struct({
  /** The server's clock when it answered (ms), for the ages the panel shows. */
  answeredAt: NonNegativeInt,
  repoQueues: Schema.Array(ForkRepoQueue),
  /** Only pull requests whose repository read answered and whose head has runs in it. */
  pullRequests: Schema.Array(ForkPullRequestRuns),
});
export type ForkWorkPanelAnswer = typeof ForkWorkPanelAnswer.Type;

/** Spread into upstream's `WsRpcGroup` (one seam line in rpc.ts). */
export const WsForkWorkPanelRpc = Rpc.make(FORK_WORK_PANEL_METHOD, {
  payload: ForkWorkPanelInput,
  success: ForkWorkPanelAnswer,
  error: EnvironmentAuthorizationError,
});
