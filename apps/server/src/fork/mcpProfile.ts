/**
 * Fork: which MCP profile a thread gets. The pure resolver lives in
 * packages/contracts/src/mcpProfileResolve.ts, so the app reads profiles by the same rules; it is
 * re-exported here. This file keeps what only the server needs: each T3 toolkit's group and the
 * drivers that apply a profile.
 */
import {
  MCP_T3_UNGROUPED_TOOLS,
  type McpT3ToolGroup,
  ProviderDriverKind,
} from "@t3tools/contracts";

export {
  MCP_PROFILE_MAX_LINEAGE,
  carryMcpProfile,
  isEveryMcpTool,
  isWithinMcpProfile,
  readCarried,
  resolveMcpProfile,
  type CarriedLookup,
  type McpProfileCannotApply,
  type McpProfileCarried,
  type McpProfileProblem,
  type McpProfileResolution,
  type McpProfileResolved,
  type McpProfileSet,
  type McpProfileSettings,
  type McpProfileSource,
  type McpProfileThread,
} from "@t3tools/contracts";

/**
 * Each T3 toolkit's group, by its folder under src/mcp/toolkits; `null` for a toolkit that holds
 * only tools of `MCP_T3_UNGROUPED_TOOLS`. The T3 tool filter (a later PR) uses it. The test in
 * personOnlySettings.test.ts fails when a toolkit or tool maps to nothing.
 */
export const MCP_T3_TOOLKIT_GROUPS: Readonly<Record<string, McpT3ToolGroup | null>> = {
  orchestrator: "orchestration",
  preview: "preview",
  previewControls: "preview",
  pullRequests: "pull-requests",
  issues: "issues",
  thread: "threads",
  project: "projects",
  device: "device",
  worktree: "worktree",
  attachment: "attachments",
  environment: "environment",
  theme: "theme",
  html: "html",
  waits: "waits",
  message: null,
  toolSearch: null,
};

/**
 * The toolkit each of `MCP_T3_UNGROUPED_TOOLS` lives in. A tool of the same name in another toolkit
 * is not ungrouped: it is in that toolkit's group. The test in personOnlySettings.test.ts checks
 * each row against the toolkit files.
 */
export const MCP_T3_UNGROUPED_TOOL_KITS: Readonly<Record<string, string>> = {
  request_secret: "orchestrator",
  send_to_user: "message",
  tool_search: "toolSearch",
};

/**
 * Tools in a group other than their toolkit's, each pinned to the toolkit that has it: `playtest`
 * claims a shared test target and runs build and deploy steps, so keeping `waits` (to wait and
 * list tasks) does not give it. The test in personOnlySettings.test.ts checks each row against the
 * toolkit files.
 */
export const MCP_T3_TOOL_OWN_GROUPS: Readonly<
  Record<string, { readonly toolkit: string; readonly group: McpT3ToolGroup }>
> = {
  playtest: { toolkit: "waits", group: "playtest" },
};

/** {@link mcpT3ToolGroup}'s answer for a tool no profile may keep. */
export const MCP_T3_TOOL_NOT_ALLOWED = "not-allowed";

/**
 * A T3 tool's group: its row in {@link MCP_T3_TOOL_OWN_GROUPS} when it is in that toolkit, else its
 * toolkit's; `"ungrouped"` for one of `MCP_T3_UNGROUPED_TOOLS` in its own toolkit, which every
 * profile keeps; {@link MCP_T3_TOOL_NOT_ALLOWED} for a tool this build does not know (its
 * toolkit has no row in {@link MCP_T3_TOOLKIT_GROUPS}, or it is an ungrouped toolkit's other tool).
 */
export function mcpT3ToolGroup(
  toolkit: string,
  tool: string,
): McpT3ToolGroup | "ungrouped" | typeof MCP_T3_TOOL_NOT_ALLOWED {
  if (
    MCP_T3_UNGROUPED_TOOLS.includes(tool) &&
    Object.hasOwn(MCP_T3_UNGROUPED_TOOL_KITS, tool) &&
    MCP_T3_UNGROUPED_TOOL_KITS[tool] === toolkit
  ) {
    return "ungrouped";
  }
  const own = Object.hasOwn(MCP_T3_TOOL_OWN_GROUPS, tool)
    ? MCP_T3_TOOL_OWN_GROUPS[tool]
    : undefined;
  if (own !== undefined && own.toolkit === toolkit) return own.group;
  const group = Object.hasOwn(MCP_T3_TOOLKIT_GROUPS, toolkit)
    ? MCP_T3_TOOLKIT_GROUPS[toolkit]
    : undefined;
  return group ?? MCP_T3_TOOL_NOT_ALLOWED;
}

/**
 * The drivers whose threads apply an MCP profile: Claude only (fork/claudeMcpProfile.ts). Any
 * other provider would give a thread every MCP server and T3 tool, so a turn there whose profile
 * is narrower than `all` is refused as it starts (fork/mcpProfileTurnCheck.ts), and a delegation
 * refuses to send a narrower profile to one on another machine (fork/remoteMcpProfile.ts).
 */
export const MCP_PROFILE_DRIVERS: ReadonlySet<ProviderDriverKind> = new Set([
  ProviderDriverKind.make("claudeAgent"),
]);
