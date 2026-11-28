/**
 * Fork (waits): the agent tools over the wait service. Registered in `layerForkTools`, outside
 * upstream's measured `layerTools`; `ForkAgentContextBudget.test.ts` measures them. A deferring
 * client lists them only after `tool_search` finds them.
 */
import { OrchestratorMcpFailure } from "@t3tools/contracts";
import { PlaytestFailure, PlaytestToolInput, PlaytestToolResult } from "@t3tools/contracts";
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
import { McpSchema, Tool, Toolkit } from "effect/ai";

import * as ThreadManagementService from "../../../orchestration-v2/ThreadManagementService.ts";
import * as PeerForwarding from "../../../peer/PeerForwarding.ts";
import * as RemoteDelegation from "../../../peer/RemoteDelegation.ts";
import { Playtest } from "../../../playtest/PlaytestLayer.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";
import {
  LensStartToolInput,
  LensStartToolResult,
} from "../../../fork/reviewRules/lensStartTool.ts";
import {
  LensReportToolInput,
  LensReportToolResult,
} from "../../../fork/reviewRules/lensReportTool.ts";
import {
  LaneBoardResult,
  LaneClaimInput,
  LaneClaimResult,
  LaneReleaseInput,
  LaneReleaseResult,
  LaneStartInput,
  LaneStartResult,
  LaneStopInput,
  LaneStopResult,
  LaneToolsFailure,
} from "../../../fork/lanes/laneTools.ts";
import { LaneStore } from "../../../fork/lanes/LaneStore.ts";
import { ServerSettingsService } from "../../../serverSettings.ts";
import { TaskListInput, TaskListResult } from "./taskList.ts";

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
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

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
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

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

// Fork (issue 386): the caller's delegated tasks with unread results and live slot use.
const TaskListTool = Tool.make("task_list", {
  description:
    "List this thread's delegated tasks with unread results and live child counts per place. Read only; task_status marks read.",
  parameters: TaskListInput,
  success: TaskListResult,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
  dependencies: [
    McpInvocationContext.McpInvocationContext,
    ThreadManagementService.ThreadManagementService,
    RemoteDelegation.RemoteDelegation,
    PeerForwarding.PeerForwarding,
  ],
})
  .annotate(Tool.Title, "List delegated tasks")
  .annotate(Tool.Readonly, true)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

// Fork (playtest): one tool, four actions, on the calling thread's session (docs/fork/playtest.md).
const PlaytestTool = Tool.make("playtest", {
  description:
    "Let a person try this thread's work on a test target, round by round. start (target, optional note) claims it; push runs one round of the project's steps on this thread's worktree and returns when it ends; status shows the session; end releases the target. One session per thread; only a person can switch playtest on.",
  parameters: PlaytestToolInput,
  success: PlaytestToolResult,
  failure: Schema.Union([PlaytestFailure, OrchestratorMcpFailure]),
  failureMode: "return",
  dependencies: [
    McpInvocationContext.McpInvocationContext,
    ThreadManagementService.ThreadManagementService,
    Playtest,
  ],
})
  .annotate(Tool.Title, "Playtest session")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, true)
  .annotate(Tool.Idempotent, false)
  .annotate(Tool.OpenWorld, true)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

// Fork (review rules, issue 888): one read-only review child per lens the head still needs, through
// delegate_task; the server writes each prompt. In the orchestration group; hidden while review rules are off.
const LensStartTool = Tool.make("lens_start", {
  description:
    "Start the review lenses a PR head still needs (project review config, changed paths, lens comments): one read-only review child per lens; the server writes each prompt. A repeat returns running children.",
  parameters: LensStartToolInput,
  success: LensStartToolResult,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
  dependencies: [
    McpInvocationContext.McpInvocationContext,
    ThreadManagementService.ThreadManagementService,
    Waits,
  ],
})
  .annotate(Tool.Title, "Start review lenses")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  // The same head twice returns the same children.
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, true)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

const LensReportTool = Tool.make("lens_report", {
  description:
    "Post a lens child's report on a PR it reads: T3 writes the first line (lens, reviewer, head) and posts it while the head is the one read. A repeat posts nothing new.",
  parameters: LensReportToolInput,
  success: LensReportToolResult,
  failure: OrchestratorMcpFailure,
  failureMode: "return",
  dependencies,
})
  .annotate(Tool.Title, "Post a lens report")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  // The same report twice is posted once.
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, true)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

// Fork (lanes, issue 1156): the lane tools, in their own group `lanes`; hidden while lanes are off.
const laneDependencies = [
  McpInvocationContext.McpInvocationContext,
  ThreadManagementService.ThreadManagementService,
  LaneStore,
  ServerSettingsService,
];

// Each lane tool repeats safely: the same start, stop, claim or release twice changes nothing.
const LaneStartTool = Tool.make("lane_start", {
  description:
    "Switch a lane defined in settings on, with a thread of this project acting for it. Returns its generation, which that thread passes to lane_claim and lane_release.",
  parameters: LaneStartInput,
  success: LaneStartResult,
  failure: LaneToolsFailure,
  failureMode: "return",
  dependencies: laneDependencies,
})
  .annotate(Tool.Title, "Start a lane")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

const LaneStopTool = Tool.make("lane_stop", {
  description: "Switch a lane off for this project and release its claims.",
  parameters: LaneStopInput,
  success: LaneStopResult,
  failure: LaneToolsFailure,
  failureMode: "return",
  dependencies: laneDependencies,
})
  .annotate(Tool.Title, "Stop a lane")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, true)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

const LaneBoardTool = Tool.make("lane_board", {
  description:
    "This project's lanes and their threads, claimed items, waiting escalations and recent notes.",
  // No `parameters`: an empty Schema.Struct serializes to a schema MCP clients reject (worktree/tools.ts).
  success: LaneBoardResult,
  failure: LaneToolsFailure,
  failureMode: "return",
  dependencies: laneDependencies,
})
  .annotate(Tool.Title, "Lane board")
  .annotate(Tool.Readonly, true)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

const LaneClaimTool = Tool.make("lane_claim", {
  description:
    "From a lane's thread: claim an item for the lane. One lane owns an item; another lane's claim is refused.",
  parameters: LaneClaimInput,
  success: LaneClaimResult,
  failure: LaneToolsFailure,
  failureMode: "return",
  dependencies: laneDependencies,
})
  .annotate(Tool.Title, "Claim an item")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

const LaneReleaseTool = Tool.make("lane_release", {
  description: "From a lane's thread: release the lane's claim on an item.",
  parameters: LaneReleaseInput,
  success: LaneReleaseResult,
  failure: LaneToolsFailure,
  failureMode: "return",
  dependencies: laneDependencies,
})
  .annotate(Tool.Title, "Release an item")
  .annotate(Tool.Readonly, false)
  .annotate(Tool.Destructive, false)
  .annotate(Tool.Idempotent, true)
  .annotate(Tool.OpenWorld, false)
  .annotate(McpSchema.EnabledWhen, McpInvocationContext.hiddenFromOutsideClients);

export const WaitsToolkit = Toolkit.make(
  WaitForTool,
  WaitListTool,
  WaitCancelTool,
  TaskListTool,
  PlaytestTool,
  LensStartTool,
  LensReportTool,
  LaneStartTool,
  LaneStopTool,
  LaneBoardTool,
  LaneClaimTool,
  LaneReleaseTool,
);
