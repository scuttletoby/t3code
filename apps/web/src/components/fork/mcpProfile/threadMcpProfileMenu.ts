/**
 * Fork: what upstream's thread menus call for the MCP profile entry. The settings and thread state
 * it reads (threadMcpProfileMenu.runtime) load in the background when this file loads, so a menu
 * still opens at once; until then (or if they cannot load) there is no entry. The menus import
 * only this file and threadMcpProfile.logic (plain functions over settings, no stores) statically.
 */
import type { ScopedThreadRef } from "@t3tools/contracts";

import { type McpProfileMenuState, isMcpProfileMenuAction } from "./threadMcpProfile.logic";

type Runtime = typeof import("./threadMcpProfileMenu.runtime");

let runtime: Runtime | null = null;
void import("./threadMcpProfileMenu.runtime").then(
  (loaded) => {
    runtime = loaded;
  },
  () => undefined,
);

/** The entry's state, or undefined (no entry) when it cannot be read now. */
export function readMcpProfileMenuState(
  threadRef: ScopedThreadRef,
): McpProfileMenuState | undefined {
  try {
    return runtime?.readMcpProfileMenuState(threadRef);
  } catch {
    // No entry rather than a menu that does not open.
    return undefined;
  }
}

/** True when `action` is this entry's; its save then starts in the background. */
export function runIfMcpProfileMenuAction(threadRef: ScopedThreadRef, action: string): boolean {
  if (!isMcpProfileMenuAction(action)) return false;
  runtime?.runMcpProfileMenuAction(threadRef, action);
  return true;
}
