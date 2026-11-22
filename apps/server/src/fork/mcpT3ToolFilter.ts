/**
 * Fork: the MCP profile's T3 tool groups on T3's own MCP server, so for every provider. One global
 * rule hides a T3 tool outside the calling thread's groups from `tools/list`, refuses it on
 * `tools/call` and keeps it out of `tool_search`; like upstream's `requiresCapability` (open PR).
 */
import { MCP_T3_UNGROUPED_TOOLS, type McpT3ToolGroup, type ThreadId } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as Context from "effect/Context";
import * as SqlClient from "effect/sql/SqlClient";
// oxlint-disable-next-line t3code/no-raw-mcp-registration -- A visibility rule registers no tool: it only narrows what McpHttpServer registered.
import { McpSchema, McpServer } from "effect/ai";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";
import * as ServerSettingsService from "../serverSettings.ts";
import { AttachmentToolkit } from "../mcp/toolkits/attachment/tools.ts";
import { DeviceToolkit } from "../mcp/toolkits/device/tools.ts";
import { EnvironmentToolkit } from "../mcp/toolkits/environment/tools.ts";
import { HtmlToolkit } from "../mcp/toolkits/html/tools.ts";
import { IssuesToolkit } from "../mcp/toolkits/issues/tools.ts";
import { MessageToolkit } from "../mcp/toolkits/message/tools.ts";
import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import { PreviewToolkit } from "../mcp/toolkits/preview/tools.ts";
import { PreviewControlsToolkit } from "../mcp/toolkits/previewControls/tools.ts";
import { ProjectToolkit } from "../mcp/toolkits/project/tools.ts";
import { PullRequestsToolkit } from "../mcp/toolkits/pullRequests/tools.ts";
import { ThemeToolkit } from "../mcp/toolkits/theme/tools.ts";
import { ThreadToolkit } from "../mcp/toolkits/thread/tools.ts";
import { WaitsToolkit } from "../mcp/toolkits/waits/tools.ts";
import { WorktreeToolkit } from "../mcp/toolkits/worktree/tools.ts";
import { makeThreadMcpProfilesById } from "./claudeMcpProfile.ts";
import { MCP_T3_TOOL_NOT_ALLOWED, mcpT3ToolGroup } from "./mcpProfile.ts";
import {
  type T3ToolSet,
  refusedT3ToolSet,
  rememberT3ToolSet,
  t3ToolSetFor,
  t3ToolSetOf,
} from "./mcpT3ToolSets.ts";

type ToolGroup = McpT3ToolGroup | "ungrouped" | typeof MCP_T3_TOOL_NOT_ALLOWED;

/**
 * Each toolkit the server registers, by its folder under src/mcp/toolkits (`toolSearch`'s one tool
 * is ungrouped by name: importing it would loop through McpToolExposure). Kept by hand, so
 * personOnlySettings.test.ts fails when the table differs from the folders' toolkits by one tool.
 */
const TOOLKITS: ReadonlyArray<readonly [string, { readonly tools: object }]> = [
  ["attachment", AttachmentToolkit],
  ["device", DeviceToolkit],
  ["environment", EnvironmentToolkit],
  ["html", HtmlToolkit],
  ["issues", IssuesToolkit],
  ["message", MessageToolkit],
  ["orchestrator", OrchestratorToolkit],
  ["preview", PreviewToolkit],
  ["previewControls", PreviewControlsToolkit],
  ["project", ProjectToolkit],
  ["pullRequests", PullRequestsToolkit],
  ["theme", ThemeToolkit],
  ["thread", ThreadToolkit],
  ["waits", WaitsToolkit],
  ["worktree", WorktreeToolkit],
];

const TOOL_SEARCH = "tool_search";

/**
 * Every T3 tool's group, by its name. A name two toolkits give different groups is in none this
 * version knows, so a profile narrower than `all` refuses it (the test keeps there being none).
 */
export const T3_TOOL_GROUPS: ReadonlyMap<string, ToolGroup> = (() => {
  const groups = new Map<string, ToolGroup>([
    [TOOL_SEARCH, mcpT3ToolGroup("toolSearch", TOOL_SEARCH)],
  ]);
  for (const [folder, toolkit] of TOOLKITS) {
    for (const name of Object.keys(toolkit.tools)) {
      const group = mcpT3ToolGroup(folder, name);
      const known = groups.get(name);
      groups.set(name, known === undefined || known === group ? group : MCP_T3_TOOL_NOT_ALLOWED);
    }
  }
  return groups;
})();

/** True when the set lets the thread see and call the T3 tool `name`. */
function t3ToolAllowed(set: T3ToolSet, name: string): boolean {
  // `all` is today's behaviour: every tool, also one this table does not know.
  if (set._tag === "Groups" && set.groups === "all") return true;
  const group = T3_TOOL_GROUPS.get(name);
  // The tools every profile keeps, matched by name and by the toolkit they are in.
  if (group === "ungrouped") return MCP_T3_UNGROUPED_TOOLS.includes(name);
  if (set._tag === "Refused" || set.groups === "all") return false;
  return group !== undefined && group !== MCP_T3_TOOL_NOT_ALLOWED && set.groups.has(group);
}

/** Why the call was refused, in plain words, with what fixes it. */
function t3ToolRefusal(set: T3ToolSet, name: string): string {
  const group = T3_TOOL_GROUPS.get(name);
  if (set._tag === "Refused") {
    return `T3 refused "${name}": this thread's MCP profile cannot be applied, so it keeps only the T3 tools every profile keeps (${MCP_T3_UNGROUPED_TOOLS.join(", ")}). The reason: ${set.reason}`;
  }
  if (group === undefined || group === "ungrouped" || group === MCP_T3_TOOL_NOT_ALLOWED) {
    // Fail closed, but name the real cause: no profile setting can add a tool with no group. (A
    // name the server does not have never gets here: upstream refuses it before this rule.)
    return `T3 refused "${name}": this tool has no T3 tool group, so MCP profile "${set.name}" cannot keep it. This is a T3 bug, not a setting to change: report it.`;
  }
  return `T3 refused "${name}": it is in the "${group}" T3 tool group, which this thread's MCP profile "${set.name}" leaves out. Ask the user to add "${group}" to that profile's t3Tools in Settings, or to pick a profile that has it for this thread; it applies from the thread's next turn.`;
}

/** The JSON-RPC error a refused call gets; its message is the plain reason. */
class T3ToolOutsideProfile extends Schema.Error<T3ToolOutsideProfile>("T3ToolOutsideProfile")({
  ...McpSchema.McpErrorBase.fields,
  _tag: Schema.tag("T3ToolOutsideProfile"),
  code: Schema.tag(-32001),
}) {}

const callerThread = (scope: Option.Option<McpInvocationContext.McpInvocationScope>) =>
  Option.getOrUndefined(scope)?.thread;

/** Servers in this process the rule is installed on; `tool_search` follows it only there. */
let installed = 0;

/**
 * `tool_search`'s filter: false for a tool the calling thread's set leaves out (none set: only the
 * ungrouped). Reads the caller from the request fiber, like upstream's `EnabledWhen`, so call it only
 * inside a request: outside one, and on a server without the rule, it lets every tool through.
 */
export const searchableT3Tool = (tool: { readonly name: string }): boolean => {
  if (installed === 0) return true;
  const fiber = Fiber.getCurrent();
  const scope =
    fiber && Context.getOrUndefined(fiber.context, McpInvocationContext.McpInvocationContext);
  const thread = scope?.thread;
  if (thread === undefined) return true;
  const set =
    t3ToolSetFor(thread.threadId) ??
    refusedT3ToolSet("no MCP profile was worked out for this thread yet.");
  return t3ToolAllowed(set, tool.name);
};

/**
 * The global rule: on `tools/list` it hides, on `tools/call` it refuses, a T3 tool the calling
 * thread's set leaves out. Other methods (T3 serves no resources or prompts) pass.
 */
export const layer = McpServer.visibility(
  Effect.gen(function* () {
    // Asked for, so a server wired without them is a type error, not a rule that refuses every
    // thread (no store) or reads every thread as `all` (no settings).
    yield* SqlClient.SqlClient;
    yield* ServerSettingsService.ServerSettingsService;
    const profileById = yield* makeThreadMcpProfilesById;
    yield* Effect.acquireRelease(
      Effect.sync(() => void (installed += 1)),
      () => Effect.sync(() => void (installed -= 1)),
    );
    /** The thread's set: the one its turn fixed, else worked out now from the settings. */
    const setFor = (threadId: ThreadId) =>
      Effect.suspend(() => {
        const fixed = t3ToolSetFor(threadId);
        if (fixed !== undefined) return Effect.succeed(fixed);
        return profileById(threadId).pipe(
          Effect.map(t3ToolSetOf),
          Effect.catchTags({
            ClaudeMcpProfileError: (refusal) => Effect.succeed(refusedT3ToolSet(refusal.reason)),
          }),
          Effect.map((set) => rememberT3ToolSet(threadId, set)),
        );
      });
    // Untraced: it runs once per tool on every list request.
    return Effect.fnUntraced(function* ({ method, target }: McpServer.MiddlewareOptions) {
      if (method !== "tools/list" && method !== "tools/call") return true;
      const thread = callerThread(
        yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext),
      );
      if (thread === undefined) return true;
      const set = yield* setFor(thread.threadId);
      if (t3ToolAllowed(set, target)) return true;
      if (method === "tools/list") return false;
      return yield* new T3ToolOutsideProfile({ message: t3ToolRefusal(set, target) });
    });
  }).pipe(Effect.withSpan("McpT3ToolFilter.make")),
  { global: true },
);

export const __testing = { t3ToolRefusal };
