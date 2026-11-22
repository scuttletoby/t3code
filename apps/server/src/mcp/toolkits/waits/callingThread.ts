/**
 * Fork (waits, playtest): the calling thread as the server knows it, for the tools of the waits
 * toolkit. Its id, project, modes and subagent parent come from the server's record of the thread
 * the credential names, never from tool input: a thread taken from input would let a call reach any
 * thread.
 */
import { OrchestratorMcpFailure } from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import { loadCaller } from "../../threadAccess.ts";

/** The calling thread; `outsideThread` is the refusal for an MCP client not in a T3 thread. */
export const callingThread = Effect.fn("waits.callingThread")(function* (outsideThread: string) {
  const caller = yield* loadCaller();
  if (caller.caller === undefined) {
    return yield* new OrchestratorMcpFailure({ code: "capability_denied", message: outsideThread });
  }
  return { ...caller, caller: caller.caller };
});
