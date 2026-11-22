/**
 * Fork: `tool_search`'s MCP profile filter, the one name McpToolExposure.ts imports. Kept light on
 * purpose: it imports no toolkit, profile resolver or store, so the upstream
 * module that imports it pulls in none of them. The global rule (mcpT3ToolRule.ts, installed in
 * McpHttpServer's `layer`) installs the check this filter asks.
 */
import * as Context from "effect/Context";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";

import * as McpInvocationContext from "../mcp/McpInvocationContext.ts";

/** True when the thread may see and call the T3 tool `name`. */
export type T3ToolSearchCheck = (threadId: string, name: string) => boolean;

/** The check the rule installed, and on how many servers; none installed: every tool passes. */
let check: T3ToolSearchCheck | undefined;
let installed = 0;

/**
 * Installs the rule's check for the scope's life: one per server the rule is on (every one installs
 * the same check). The filter passes every tool again once the last one is gone.
 */
export const installT3ToolSearchCheck = (allowed: T3ToolSearchCheck) =>
  Effect.acquireRelease(
    Effect.sync(() => {
      installed += 1;
      check = allowed;
    }),
    () =>
      Effect.sync(() => {
        installed -= 1;
        if (installed === 0) check = undefined;
      }),
  );

/**
 * `tool_search`'s filter: false for a tool the calling thread's set leaves out (none set: only the
 * ungrouped). Reads the caller from the request fiber, like upstream's `EnabledWhen`, so call it only
 * inside a request: outside one, and on a server without the rule, it lets every tool through.
 */
export const searchableT3Tool = (tool: { readonly name: string }): boolean => {
  const allowed = check;
  if (allowed === undefined) return true;
  const fiber = Fiber.getCurrent();
  const scope =
    fiber && Context.getOrUndefined(fiber.context, McpInvocationContext.McpInvocationContext);
  const thread = scope?.thread;
  if (thread === undefined) return true;
  return allowed(thread.threadId, tool.name);
};
