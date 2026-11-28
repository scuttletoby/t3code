import type { ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";

import { parentOf } from "../../../waits/WaitDelivery.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import { assertSameLink } from "../../linkOrigin.ts";
import * as McpToolAccess from "../../McpToolAccess.ts";
import { assertTargetWithinLimits, type Caller, unavailable } from "../../threadAccess.ts";
import {
  laneBoardCall,
  laneClaimCall,
  laneReleaseCall,
  laneStartCall,
  laneStopCall,
} from "../../../fork/lanes/laneTools.ts";
import { lensReportCall } from "../../../fork/reviewRules/lensReportTool.ts";
import { lensStartCall } from "../../../fork/reviewRules/lensStartTool.ts";
import { callingThread as threadOfCaller } from "./callingThread.ts";
import { playtestCall } from "./playtest.ts";
import { listTasks } from "./taskList.ts";
import { WaitsToolkit } from "./tools.ts";

/** The calling thread as the server knows it (callingThread.ts). */
const callingThread = () =>
  threadOfCaller("Waits belong to a thread. This MCP client is not running inside a T3 thread.");

/** The same rule `McpToolAccess.writesThreads` applies to a thread it changes. */
const assertMayChange = Effect.fn("waits.assertMayChange")(function* (
  caller: Caller,
  threadId: ThreadId,
) {
  if (threadId === caller.caller?.id) return;
  const target = yield* caller.threads.getThreadShell(threadId).pipe(Effect.mapError(unavailable));
  if (target === null || target.deletedAt !== null) return;
  yield* assertTargetWithinLimits(caller.limits, target);
  yield* assertSameLink(caller, target);
});

const handlers = {
  wait_for: McpToolAccess.actsAsCaller((input) =>
    Effect.gen(function* () {
      const { caller } = yield* callingThread();
      const waits = yield* Waits;
      const parentThreadId = parentOf(caller);
      return yield* waits.register({
        threadId: caller.id,
        projectId: caller.projectId,
        ...(parentThreadId === null ? {} : { parentThreadId }),
        input,
      });
    }),
  ),
  wait_list: McpToolAccess.readsAsCaller((input) =>
    Effect.gen(function* () {
      const { caller } = yield* callingThread();
      const waits = yield* Waits;
      return yield* waits.list({
        threadId: caller.id,
        projectId: caller.projectId,
        scope: input.scope,
        includeEnded: input.includeEnded,
      });
    }),
  ),
  // A named thread is checked as the thread tools check it; so is every thread that now holds
  // a matching wait, since a handed-over wait lives on the parent.
  wait_cancel: McpToolAccess.writesThreads(
    (input) => [input.threadId],
    (input) =>
      Effect.gen(function* () {
        const calling = yield* callingThread();
        const projectId = calling.caller.projectId;
        const waits = yield* Waits;
        const request = { projectId, waitId: input.waitId, threadId: input.threadId };
        // Neither or both: the service refuses it before reading anything.
        if ((input.waitId === undefined) === (input.threadId === undefined)) {
          return yield* waits.cancel(request);
        }
        const open = yield* waits.list({
          threadId: calling.caller.id,
          projectId,
          scope: "project",
        });
        // Only a waiting wait can be cancelled; a delivering one must not make the call refused.
        const holders = new Set(
          open.waits
            .filter((wait) => wait.state === "waiting")
            .filter((wait) =>
              input.waitId !== undefined
                ? wait.waitId === input.waitId
                : wait.threadId === input.threadId ||
                  (input.threadId !== undefined && (wait.via ?? []).includes(input.threadId)),
            )
            .map((wait) => wait.threadId),
        );
        for (const holder of holders) yield* assertMayChange(calling, holder);
        return yield* waits.cancel(request);
      }),
  ),
  task_list: McpToolAccess.readsAsCaller((input) => listTasks(input)),
  playtest: McpToolAccess.actsAsCaller((input) => playtestCall(input)),
  lens_start: McpToolAccess.actsAsCaller((input) => lensStartCall(input)),
  lens_report: McpToolAccess.actsAsCaller((input) => lensReportCall(input)),
  // Fork (lanes): the lane thread is checked as the thread tools check a thread they change.
  lane_start: McpToolAccess.writesThreads(
    (input) => [input.threadId],
    (input) => laneStartCall(input),
  ),
  lane_stop: McpToolAccess.actsAsCaller((input) => laneStopCall(input)),
  lane_board: McpToolAccess.readsAsCaller(() => laneBoardCall()),
  lane_claim: McpToolAccess.actsAsCaller((input) => laneClaimCall(input)),
  lane_release: McpToolAccess.actsAsCaller((input) => laneReleaseCall(input)),
} satisfies McpToolAccess.Handlers<typeof WaitsToolkit.tools>;

export const layer = McpToolAccess.toLayer(WaitsToolkit, handlers);
