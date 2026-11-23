/**
 * Fork: the MCP profile's T3 tool groups, for every provider. One global rule on T3's MCP server
 * hides a T3 tool outside the calling thread's groups from `tools/list`, refuses it on `tools/call`
 * and keeps it out of `tool_search` (through the light filter in mcpT3ToolFilter.ts).
 */
import { MCP_T3_UNGROUPED_TOOLS, type McpT3ToolGroup, type ThreadId } from "@t3tools/contracts";
import * as Clock from "effect/Clock";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
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
import { installLensStartServer } from "./reviewRules/lensStartServer.ts";
import {
  LENS_START_OFF,
  LENS_START_TOOL,
  lensStartSearchable,
  makeLensStartOn,
} from "./reviewRules/lensStartVisibility.ts";
import * as ServerEnvironment from "../environment/ServerEnvironment.ts";
import { installT3ToolSearchCheck } from "./mcpT3ToolFilter.ts";
import {
  type T3ToolSet,
  T3_TOOL_SET_HOLD_WAIT_MS as HOLD_WAIT_MS,
  dropT3ToolSetHold,
  refusedT3ToolSet,
  rememberT3ToolSet,
  t3ToolSetFor,
  t3ToolSetHold,
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

const NO_SET_YET = "no MCP profile was worked out for this thread yet.";

/**
 * How long a refused set worked out by a request (not a turn) is reused: long enough that a list
 * request does not read the settings once per tool, short enough that a one-off read error does not
 * last the turn. Never remembered as the thread's set.
 */
export const REFUSED_SET_REUSE_MS = 5_000;
/** Threads whose refused set is reused at once; the oldest beyond this is worked out again. */
const MAX_REFUSED_SETS = 1024;
/** How long a turn Codex started may hold the thread's requests while its set is fixed. */
export const T3_TOOL_SET_HOLD_WAIT_MS = HOLD_WAIT_MS;
const HOLD_TIMED_OUT =
  "T3 is still working out this thread's MCP profile for the turn that just started, or that check stopped; the profile applies again once it ends or the thread's next turn starts. Try again in a moment.";

/** Recent refused sets, by thread, with when each stops being reused. */
const refused = new Map<string, { readonly set: T3ToolSet; readonly until: number }>();

/** True when the set lets the thread see and call the T3 tool `name`; no fixed set: the ungrouped. */
const searchable = (threadId: string, name: string) =>
  t3ToolAllowed(
    t3ToolSetFor(threadId) ?? refused.get(threadId)?.set ?? refusedT3ToolSet(NO_SET_YET),
    name,
  ) &&
  // Fork (review rules): off means off, also for a search.
  (name !== LENS_START_TOOL || lensStartSearchable(threadId));

/** The rule's check of one tool for one request, as {@link layer} runs it. */
const makeVisible = Effect.gen(function* () {
  const profileById = yield* makeThreadMcpProfilesById;
  const lensStartOn = yield* makeLensStartOn;
  /** Worked out now from the settings: kept when it applies, reused briefly when refused. */
  const workedOut = Effect.fnUntraced(function* (threadId: ThreadId) {
    const now = yield* Clock.currentTimeMillis;
    const recent = refused.get(threadId);
    if (recent !== undefined && recent.until > now) return recent.set;
    const resolved = yield* profileById(threadId).pipe(
      Effect.map(t3ToolSetOf),
      Effect.catchTags({
        ClaudeMcpProfileError: (refusal) => Effect.succeed(refusedT3ToolSet(refusal.reason)),
      }),
    );
    if (resolved._tag === "Groups") {
      refused.delete(threadId);
      return rememberT3ToolSet(threadId, resolved);
    }
    // A turn may have fixed a set while this read ran: that one stands.
    const fixed = t3ToolSetFor(threadId);
    if (fixed !== undefined) return fixed;
    refused.delete(threadId);
    refused.set(threadId, { set: resolved, until: now + REFUSED_SET_REUSE_MS });
    for (const oldest of refused.keys()) {
      if (refused.size <= MAX_REFUSED_SETS) break;
      refused.delete(oldest);
    }
    return resolved;
  });
  /**
   * The thread's set: the one its turn fixed, else worked out now from the settings. While a turn
   * Codex started is having its set fixed, wait for that set rather than use the one before.
   */
  const setFor: (threadId: ThreadId) => Effect.Effect<T3ToolSet> = Effect.fnUntraced(function* (
    threadId: ThreadId,
  ) {
    const hold = t3ToolSetHold(threadId, yield* Clock.currentTimeMillis);
    if (hold !== undefined) {
      // At most until the hold's bound, whichever request asks: a list request checks each tool
      // in turn and only its first waits, so a hold never released costs one wait, not one per
      // tool. It is then dropped, so the thread's later requests do not wait on it again.
      const released =
        hold.waitMs > 0
          ? yield* Deferred.await(hold.done).pipe(Effect.timeoutOption(hold.waitMs))
          : Option.none();
      if (Option.isNone(released)) {
        const timedOut = refusedT3ToolSet(HOLD_TIMED_OUT);
        if (dropT3ToolSetHold(threadId, hold, timedOut)) return timedOut;
        // Released, or a later turn's hold took its place, as the wait ended: look again.
        return yield* setFor(threadId);
      }
    }
    return t3ToolSetFor(threadId) ?? (yield* workedOut(threadId));
  });
  // Untraced: it runs once per tool on every list request.
  return Effect.fnUntraced(function* ({ method, target }: McpServer.MiddlewareOptions) {
    if (method !== "tools/list" && method !== "tools/call") return true;
    const thread = callerThread(
      yield* Effect.serviceOption(McpInvocationContext.McpInvocationContext),
    );
    if (thread === undefined) return true;
    const set = yield* setFor(thread.threadId);
    if (!t3ToolAllowed(set, target)) {
      if (method === "tools/list") return false;
      return yield* new T3ToolOutsideProfile({ message: t3ToolRefusal(set, target) });
    }
    // Fork (review rules): lens_start only where the thread's project has review rules on.
    if (target !== LENS_START_TOOL || (yield* lensStartOn(thread.threadId))) return true;
    if (method === "tools/list") return false;
    return yield* new T3ToolOutsideProfile({ message: LENS_START_OFF });
  });
});

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
    yield* installT3ToolSearchCheck(searchable);
    // Fork (review rules): lens_start's children start through this server's own delegate_task.
    const environment = yield* Effect.serviceOption(ServerEnvironment.ServerEnvironment);
    const here = Option.isSome(environment)
      ? yield* environment.value.getEnvironmentId.pipe(Effect.option)
      : Option.none();
    yield* installLensStartServer(yield* McpServer.McpServer, Option.getOrUndefined(here));
    return yield* makeVisible;
  }).pipe(Effect.withSpan("McpT3ToolRule.make")),
  { global: true },
);

export const __testing = { t3ToolRefusal, clearRefused: () => refused.clear(), makeVisible };
