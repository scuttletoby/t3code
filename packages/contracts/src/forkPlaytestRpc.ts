/**
 * Fork (playtest F5): the two client RPCs behind the thread chip. `status` reads the thread's
 * session that is on (else none); `end` ends it as the person, the same as the tool's `end`
 * (reason `person`). rpc.ts lists them in upstream's group; the server's scope and span tables
 * spread the maps below. Docs: docs/fork/playtest.md.
 */
import * as Rpc from "effect/rpc/Rpc";
import * as Schema from "effect/Schema";

import {
  AuthOrchestrationOperateScope,
  AuthOrchestrationReadScope,
  EnvironmentAuthorizationError,
} from "./auth.ts";
import { IsoDateTime, NonNegativeInt, ThreadId } from "./baseSchemas.ts";
import {
  PLAYTEST_NOTE_MAX,
  PlaytestFailure,
  PlaytestSessionId,
  PlaytestSessionState,
  PlaytestTarget,
} from "./playtest.ts";

export const FORK_PLAYTEST_WS_METHODS = {
  status: "fork.playtest.status",
  end: "fork.playtest.end",
} as const;

/** What the chip shows of a session: no steps, worktree or step output. */
export const ForkPlaytestChipSession = Schema.Struct({
  id: PlaytestSessionId,
  target: PlaytestTarget,
  note: Schema.String.check(Schema.isMaxLength(PLAYTEST_NOTE_MAX)),
  state: PlaytestSessionState,
  startedAt: IsoDateTime,
  rounds: NonNegativeInt,
});
export type ForkPlaytestChipSession = typeof ForkPlaytestChipSession.Type;

const ThreadPayload = Schema.Struct({ threadId: ThreadId });
const RpcError = Schema.Union([PlaytestFailure, EnvironmentAuthorizationError]);

/** The thread's session that is not ended yet, or `null`. */
export const WsForkPlaytestStatusRpc = Rpc.make(FORK_PLAYTEST_WS_METHODS.status, {
  payload: ThreadPayload,
  success: Schema.NullOr(ForkPlaytestChipSession),
  error: RpcError,
});

/** Ends the thread's session as the person; `release-failed`: still ending, T3 tries again. */
export const WsForkPlaytestEndRpc = Rpc.make(FORK_PLAYTEST_WS_METHODS.end, {
  payload: ThreadPayload,
  success: Schema.Struct({
    outcome: Schema.Literals(["ended", "nothing-to-end", "release-failed"]),
  }),
  error: RpcError,
});

/** Reading needs what reading the thread needs; ending runs the release step, so operate. */
export const FORK_PLAYTEST_RPC_SCOPES = {
  [FORK_PLAYTEST_WS_METHODS.status]: AuthOrchestrationReadScope,
  [FORK_PLAYTEST_WS_METHODS.end]: AuthOrchestrationOperateScope,
} as const;

export const FORK_PLAYTEST_RPC_SPANS = {
  [FORK_PLAYTEST_WS_METHODS.status]: "playtest",
  [FORK_PLAYTEST_WS_METHODS.end]: "playtest",
} as const;
