/**
 * Fork: the `lens_report` tool's schemas and handler (registered in the waits toolkit, its own group `orchestration`
 * as lens_start's, hidden while review rules are off: fork/mcpT3ToolRule.ts). A lens child lens_start started posts
 * its report with it: T3 writes the first line from the child's stored record and the model it runs on, checks the
 * pull request's head, and posts (lensReport.ts, lensStartServer.ts).
 */
import { OrchestratorMcpFailure, PositiveInt } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import { callingThread } from "../../mcp/toolkits/waits/callingThread.ts";
import { Waits } from "../../waits/WaitWorker.ts";

export const LensReportToolInput = Schema.Struct({
  pr: PositiveInt.annotate({ description: "PR number this lens read." }),
  body: Schema.String.annotate({
    description: "The report without its first line: T3 writes that line.",
  }),
});
export type LensReportToolInput = typeof LensReportToolInput.Type;

export const LensReportToolResult = Schema.Struct({
  pr: Schema.Number,
  head: Schema.NullOr(Schema.String),
  lens: Schema.NullOr(Schema.String),
  state: Schema.Literals(["posted", "already", "refused", "failed"]),
  url: Schema.NullOr(Schema.String),
  refused: Schema.NullOr(Schema.String),
  next: Schema.String,
});
export type LensReportToolResult = typeof LensReportToolResult.Type;

/** `lens_report` for the calling thread, a lens child. */
export const lensReportCall = Effect.fn("lensReport.call")(function* (input: LensReportToolInput) {
  const { caller } = yield* callingThread(
    "lens_report posts a lens child's report. This MCP client is not running inside a T3 thread.",
  );
  const server = (yield* Waits).lensStart;
  if (server === undefined) {
    return yield* new OrchestratorMcpFailure({
      code: "capability_denied",
      message: "This server cannot post lens reports (no lens_start service).",
    });
  }
  const answer = yield* server.report({
    threadId: caller.id,
    projectId: caller.projectId,
    reviewer: caller.modelSelection.model,
    pr: input.pr,
    body: input.body,
  });
  switch (answer.type) {
    case "posted":
    case "already":
      return {
        pr: input.pr,
        head: answer.head,
        lens: answer.lens,
        state: answer.type,
        url: answer.url,
        refused: null,
        next:
          answer.type === "posted"
            ? `Posted: lens ${answer.lens} read #${input.pr} at ${answer.head}. Post nothing more for it.`
            : `This report was posted already: nothing new was posted. Post nothing more for #${input.pr}.`,
      } satisfies LensReportToolResult;
    case "refused":
      return {
        pr: input.pr,
        head: answer.head,
        lens: null,
        state: "refused",
        url: null,
        refused: answer.reason,
        next: `Nothing posted: ${answer.reason}. Fix the report and call lens_report again, or, if it cannot be fixed, post nothing and say why in your answer.`,
      } satisfies LensReportToolResult;
    case "failed":
      return {
        pr: input.pr,
        head: null,
        lens: null,
        state: "failed",
        url: null,
        refused: answer.reason,
        next: `It may or may not be posted (${answer.reason}). Call lens_report again with the same report: a report already posted is found, not posted twice.`,
      } satisfies LensReportToolResult;
  }
});
