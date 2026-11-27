/**
 * Fork (calm sidebar, CAL4): the thread menu's "Thread role" entry, read from the thread's server
 * (its settings and thread index) and saved there as a `threadRoles` patch. The server checks
 * every new pointer (fork/threadRoleTargets.ts); a refusal shows its reason in a toast.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { thrownErrorMessage } from "@t3tools/client-runtime/errors";
import { runAtomCommand, squashAtomCommandFailure } from "@t3tools/client-runtime/state/runtime";
import { resolveThreadRoles, threadRole } from "@t3tools/client-runtime/state/thread-roles";
import type { EnvironmentId, ScopedThreadRef, ThreadRolesPatch } from "@t3tools/contracts";

import { m } from "../../../i18n/messages";
import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { serverEnvironment } from "../../../state/server";
import { environmentThreadShells } from "../../../state/threads";
import { toastManager } from "../../ui/toast";
import {
  isRoleMenuAction,
  type RoleMenuAction,
  type RoleMenuState,
  roleMenuPatch,
} from "./roleMenu.logic";

function readThread(threadRef: ScopedThreadRef) {
  const index = appAtomRegistry.get(
    environmentThreadShells.environmentThreadIndexAtom(threadRef.environmentId),
  );
  return { index, thread: index.get(threadRef.threadId) };
}

/** The entry's state, or none: an older server, a thread not loaded, a child or archived thread. */
export function readRoleMenuState(threadRef: ScopedThreadRef): RoleMenuState | undefined {
  const settings = appAtomRegistry.get(
    serverEnvironment.settingsValueAtom(threadRef.environmentId),
  );
  if (settings === null || settings.threadRoles === undefined) return undefined;
  const { index, thread } = readThread(threadRef);
  if (thread === undefined || thread.archivedAt != null) return undefined;
  if (threadRole(thread, undefined) === "child") return undefined;
  const roles = resolveThreadRoles(settings.threadRoles, index);
  const orchestrator = roles.orchestrators.get(thread.projectId) ?? null;
  return {
    isAssistant: roles.assistant === thread.id,
    isOrchestrator: orchestrator === thread.id,
    assistantElsewhere: roles.assistant !== null && roles.assistant !== thread.id,
    orchestratorElsewhere: orchestrator !== null && orchestrator !== thread.id,
  };
}

/** Saves `patch` on that server; a refusal (or a failed call) is a toast with its reason. */
export async function saveThreadRoles(
  environmentId: EnvironmentId,
  patch: ThreadRolesPatch,
): Promise<boolean> {
  const result = await runAtomCommand(
    appAtomRegistry,
    serverEnvironment.updateSettings,
    { environmentId, input: { patch: { threadRoles: patch } } },
    { reportFailure: false },
  );
  if (result._tag === "Success") return true;
  toastManager.add({
    type: "error",
    title: "Thread role not saved",
    description: thrownErrorMessage(squashAtomCommandFailure(result), m) ?? "An error occurred.",
  });
  return false;
}

async function runRoleAction(threadRef: ScopedThreadRef, action: RoleMenuAction) {
  const state = readRoleMenuState(threadRef);
  const { thread } = readThread(threadRef);
  if (state === undefined || thread === undefined) return;
  const patch = roleMenuPatch(action, thread, state);
  if (patch !== null) await saveThreadRoles(threadRef.environmentId, patch);
}

/** Runs a "Thread role" pick and says so; any other menu action is left to upstream's switch. */
export function runIfRoleMenuAction(threadRef: ScopedThreadRef, action: string): boolean {
  if (!isRoleMenuAction(action)) return false;
  void runRoleAction(threadRef, action);
  return true;
}
