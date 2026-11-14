/**
 * Fork (waits): the agent tools over the wait service. Registered in `layerForkTools`, outside
 * upstream's measured `layerTools`; `ForkAgentContextBudget.test.ts` measures them. A deferring
 * client lists them only after `tool_search` finds them.
 */
import { OrchestratorMcpFailure } from "@t3tools/contracts";
import {
  WaitCancelInput,
  WaitCancelResult,
  WaitForInput,
  WaitForResult,
  WaitListInput,
  WaitListResult,
  WaitToolFailure,
} from "@t3tools/contracts/waitFor";
import * as Schema from "effect/Schema";
import { Tool, Toolkit } from "effect/ai";

import * as ThreadManagementService from "../../../orchestration-v2/ThreadManagementService.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";

const dependencies = [
  McpInvocationContext.McpInvocationContext,
  ThreadManagementService.ThreadManagementService,
  Waits,
];

const WaitsToolFailure = Schema.Union([WaitToolFailure, OrchestratorMcpFailure]);

const WaitForTool = Tool.make("wait_for", {
  description:
    "Wait without polling for: a pull request's checks or merge, a file, a systemd unit ending, a configured check, or a free agent slot. Set `kind` and its field. Returns at once. End your turn; one message starting with [wait <id>] wakes this thread when it is met, times out or fails; the deadline counts from registration. A unit that never started counts as ended.",
  parameters: WaitForInput,
  success: WaitForResult,
  failure: WaitsToolFailure,
  failureMode: "return",
  dependencies,
})
  .annotate(Tool.Title, "Wait for a condition")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  // The same condition twice is the same wait.
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false);

const WaitListTool = Tool.make("wait_list", {
  description:
    'List this thread\'s waits (scope "project": every wait in the project), the configured checks and the current agent slot use.',
  parameters: WaitListInput,
  success: WaitListResult,
  failure: WaitsToolFailure,
  failureMode: "return",
  dependencies,
})
  .annotate(Tool.Title, "List waits")
  .annotate(Tool.Readonly, true)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false);

const WaitCancelTool = Tool.make("wait_cancel", {
  description:
    "Cancel one wait by id, or every wait of a thread, including waits it handed to its parent. A cancelled wait never sends a message.",
  parameters: WaitCancelInput,
  success: WaitCancelResult,
  failure: WaitsToolFailure,
  failureMode: "return",
  dependencies,
})
  .annotate(Tool.Title, "Cancel waits")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, true)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false);

export const WaitsToolkit = Toolkit.make(WaitForTool, WaitListTool, WaitCancelTool);
