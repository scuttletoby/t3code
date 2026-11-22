/**
 * Fork: the profile the turn check (mcpProfileTurnCheck.ts) worked out for the turn it starts, read
 * by the Codex wrapper (codexMcpProfile.ts). Keyed by thread, not turn: never fork work inside a turn
 * start that outlives it (it would hand this turn's profile to a later turn); fork from the session.
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
