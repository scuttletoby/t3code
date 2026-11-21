/**
 * Fork: the thread menu's MCP profile entry at run time, loaded by threadMcpProfileMenu.ts only
 * when a menu opens or is clicked. The rules are in threadMcpProfile.logic.ts.
 */
import { runAtomCommand } from "@t3tools/client-runtime/state/runtime";
import { AuthSettingsWriteScope, type ScopedThreadRef } from "@t3tools/contracts";

import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { readThreadShells } from "../../../state/entities";
import { readEnvironment } from "../../../state/environments";
import { serverEnvironment } from "../../../state/server";
import { readEnvironmentScope } from "../../../state/session";
import {
  type McpProfileMenuId,
  type McpProfileMenuState,
  mcpProfileMenuPatch,
  mcpProfileMenuState,
} from "./threadMcpProfile.logic";

/** The entry's state, or undefined (no entry) while the thread's server settings have not loaded. */
export function readMcpProfileMenuState(
  threadRef: ScopedThreadRef,
): McpProfileMenuState | undefined {
  const settings = readEnvironment(threadRef.environmentId)?.serverConfig?.settings;
  if (settings === undefined) return undefined;
  const shells = readThreadShells().filter(
    (shell) => shell.environmentId === threadRef.environmentId,
  );
  return mcpProfileMenuState(
    settings,
    threadRef.threadId,
    shells,
    readEnvironmentScope(threadRef.environmentId, AuthSettingsWriteScope),
  );
}

/**
 * Saves the entry's click: the one thread's pick, read against the settings as they are now. The
 * settings command reports a refused save.
 */
export function runMcpProfileMenuAction(threadRef: ScopedThreadRef, action: McpProfileMenuId) {
  const settings = readEnvironment(threadRef.environmentId)?.serverConfig?.settings;
  if (settings === undefined) return;
  if (!readEnvironmentScope(threadRef.environmentId, AuthSettingsWriteScope)) return;
  const patch = mcpProfileMenuPatch(settings.mcpThreadProfiles, threadRef.threadId, action);
  if (patch === null) return;
  void runAtomCommand(
    appAtomRegistry,
    serverEnvironment.updateSettings,
    { environmentId: threadRef.environmentId, input: { patch } },
    { label: "set the thread's MCP profile", reportFailure: true },
  );
}
