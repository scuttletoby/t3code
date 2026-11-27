/**
 * Fork (playtest F5): the handlers of the thread chip's two RPCs (contracts forkPlaytestRpc.ts),
 * spread into ws.ts's handler table. `status` shows only a session that is not ended; `end` is the
 * tool's `end` with reason `person`, so the release, the store and the thread notice are the
 * service's own. Docs: docs/fork/playtest.md.
 */
import {
  FORK_PLAYTEST_WS_METHODS as METHODS,
  type ForkPlaytestChipSession,
  PlaytestFailure,
  type ThreadId,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";

import type { PlaytestService, PlaytestSessionView } from "../playtest/PlaytestService.ts";

/** The chip's part of a session; an ended one (the thread's last) shows no chip. */
export function chipSessionOf(session: PlaytestSessionView | null): ForkPlaytestChipSession | null {
  if (session === null || session.state === "ended") return null;
  return {
    id: session.id,
    target: session.target,
    note: session.note,
    state: session.state,
    startedAt: session.startedAt,
    rounds: session.rounds,
  };
}

type Service = Pick<PlaytestService, "status" | "end">;

/** A server built without the playtest service (some tests) answers that it cannot read it. */
const unavailable = Effect.fail(
  new PlaytestFailure({ code: "store_unavailable", message: "Playtest is not available here." }),
);

/** The two handlers, over the service when the server has it. */
export const make = (service: Option.Option<Service>) => ({
  [METHODS.status]: (input: { readonly threadId: ThreadId }) =>
    Option.match(service, {
      onNone: () => unavailable,
      onSome: (playtest) =>
        playtest.status({ threadId: input.threadId }).pipe(Effect.map(chipSessionOf)),
    }),
  [METHODS.end]: (input: { readonly threadId: ThreadId }) =>
    Option.match(service, {
      onNone: () => unavailable,
      onSome: (playtest) =>
        playtest
          .end({ threadId: input.threadId, reason: "person" })
          .pipe(Effect.map(({ outcome }) => ({ outcome }))),
    }),
});
