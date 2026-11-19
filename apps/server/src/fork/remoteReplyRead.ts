/**
 * Fork: a task's reply from a linked environment on another version. Its thread details may lack a
 * field this version requires, which fails the whole read and kept the task running forever; the
 * reply needs none of them. Runs and items keep their schemas, so a reply is never guessed.
 */
import {
  OrchestratorMcpFailure,
  OrchestratorMcpThreadReadInput,
  OrchestratorMcpThreadReadResult,
} from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Struct from "effect/Struct";
import * as Tool from "effect/ai/Tool";

import type * as PeerForwarding from "../peer/PeerForwarding.ts";

/** A thread read without the thread's details: all a task's reply is read from. */
export const ReplyPage = OrchestratorMcpThreadReadResult.mapFields(Struct.omit(["thread"]));
export type ReplyPage = typeof ReplyPage.Type;

/** `t3_thread_read` there, decoding its answer as a ReplyPage. */
const ReplyRead = Tool.make("t3_thread_read", {
  parameters: OrchestratorMcpThreadReadInput,
  success: ReplyPage,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
});

type Forwarding = PeerForwarding.PeerForwarding["Service"];

/**
 * Reads a task's reply there: the full read, then, if that fails as an answer this version cannot
 * decode does (`orchestration_error`), the same read as a ReplyPage. If that fails too, or `call`
 * refuses it because linked environments are off here, the first error stands, as before.
 */
export const replyReads = (forwarding: Forwarding) => ({
  readThread: (
    ...[scope, environmentId, input]: Parameters<Forwarding["readThread"]>
  ): Effect.Effect<ReplyPage, OrchestratorMcpFailure> =>
    forwarding.readThread(scope, environmentId, input).pipe(
      Effect.catchIf(
        (error) => error.code === "orchestration_error",
        (error) =>
          forwarding
            .call(scope, ReplyRead, environmentId, input)
            .pipe(Effect.mapError(() => error)),
      ),
    ),
});
