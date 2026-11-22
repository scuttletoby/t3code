/**
 * Fork: the MCP profile the turn check (mcpProfileTurnCheck.ts) worked out for the turn it is
 * starting, so the Codex wrapper (codexMcpProfile.ts) reads it instead of working it out again.
 * Only the fiber that starts the turn, and fibers it forks, see it.
 */
import type { ThreadId } from "@t3tools/contracts";
import * as Context from "effect/Context";

import type { McpProfileResolved } from "./mcpProfile.ts";

export interface TurnMcpProfile {
  readonly threadId: ThreadId;
  readonly resolved: McpProfileResolved;
}

export const CurrentTurnMcpProfile = Context.Reference<TurnMcpProfile | undefined>(
  "t3/fork/mcpTurnProfile/CurrentTurnMcpProfile",
  { defaultValue: () => undefined },
);
