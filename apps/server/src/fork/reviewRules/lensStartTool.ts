/**
 * Fork: the `lens_start` tool's schemas and handler (registered in the waits toolkit, its own group `orchestration`,
 * hidden while review rules are off: fork/mcpT3ToolRule.ts). The handler builds the world from the wait service
 * (`Waits.lensStart`) and runs lensStartRun.ts; with `afterCi` it registers the CI wait through that same service.
 */
import { OrchestratorMcpFailure, PositiveInt } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import * as McpInvocationContext from "../../mcp/McpInvocationContext.ts";
import { callingThread } from "../../mcp/toolkits/waits/callingThread.ts";
import { parentOf } from "../../waits/WaitDelivery.ts";
import { Waits } from "../../waits/WaitWorker.ts";
import { runLensStart, type WaitForCiAnswer } from "./lensStartRun.ts";

export const LensStartToolInput = Schema.Struct({
  pr: PositiveInt.annotate({
    description: "PR number in this project's repository.",
  }),
  head: Schema.optionalKey(
    Schema.String.check(Schema.isPattern(/^[0-9a-fA-F]{40}$/u)).annotate({
      description: "Expected head sha; another is refused.",
    }),
  ),
  afterCi: Schema.optionalKey(
    Schema.Boolean.annotate({
      description: "true: return now; the server starts them once the head's CI checks pass.",
    }),
  ),
});
export type LensStartToolInput = typeof LensStartToolInput.Type;

export const LensStartToolResult = Schema.Struct({
  pr: Schema.Number,
  head: Schema.NullOr(Schema.String),
  state: Schema.Literals(["started", "nothing", "refused", "waiting"]),
  started: Schema.Array(
    Schema.Struct({
      lens: Schema.String,
      requestId: Schema.String,
      taskId: Schema.String,
      attempt: Schema.Number,
    }),
  ),
  running: Schema.Array(
    Schema.Struct({ requestId: Schema.String, taskId: Schema.NullOr(Schema.String) }),
  ),
  alreadyRead: Schema.Array(Schema.String),
  refused: Schema.Array(Schema.String),
  notes: Schema.Array(Schema.String),
  waitId: Schema.optionalKey(Schema.String),
  next: Schema.String,
});
export type LensStartToolResult = typeof LensStartToolResult.Type;

/** `lens_start` for the calling thread. */
export const lensStartCall = Effect.fn("lensStart.call")(function* (input: LensStartToolInput) {
  const { caller } = yield* callingThread(
    "lens_start starts lens children of a thread. This MCP client is not running inside a T3 thread.",
  );
  const waits = yield* Waits;
  const server = waits.lensStart;
  if (server === undefined) {
    return yield* new OrchestratorMcpFailure({
      code: "capability_denied",
      message: "This server cannot start review lenses (no lens_start service).",
    });
  }
  const scope = yield* McpInvocationContext.McpInvocationContext;
  const parentThreadId = parentOf(caller);
  const world = yield* server.worldFor({
    threadId: caller.id,
    projectId: caller.projectId,
    start: server.startInRequest(scope),
    waitForCi: ({ number, head, checks }) =>
      waits
        .register({
          threadId: caller.id,
          projectId: caller.projectId,
          ...(parentThreadId === null ? {} : { parentThreadId }),
          input: {
            kind: "pr",
            pr: { number },
            note: `lens_start #${number} after CI`,
          },
          narrow: { checks, head },
          action: server.waitForCiAction,
        })
        .pipe(
          Effect.map((result): WaitForCiAnswer =>
            result.state === "waiting"
              ? { type: "waiting", waitId: result.waitId }
              : { type: "met", outcome: result.outcome, detail: result.detail },
          ),
          Effect.catch((failure) =>
            Effect.succeed<WaitForCiAnswer>({ type: "refused", message: failure.message }),
          ),
        ),
  });
  return yield* runLensStart(world, {
    pr: input.pr,
    ...(input.head === undefined ? {} : { head: input.head.toLowerCase() }),
    ...(input.afterCi === undefined ? {} : { afterCi: input.afterCi }),
  });
});
