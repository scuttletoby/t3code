/**
 * Fork (calm sidebar): the Role entry's reads and saves over the app's settings and threads. It
 * installs itself into threadRoleMenu.ts when loaded (useForkCalmSidebar imports it, so the app
 * loads it with the sidebar), which keeps the thread menu hook's import free of the app graph.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own text is English only; upstream's translation catalogs hold no fork keys. */
import { thrownErrorMessage } from "@t3tools/client-runtime/errors";
import {
  isAtomCommandInterrupted,
  runAtomCommand,
  squashAtomCommandFailure,
} from "@t3tools/client-runtime/state/runtime";
import { AuthSettingsWriteScope, type ScopedThreadRef, type ThreadId } from "@t3tools/contracts";

import { m } from "../../../i18n/messages";
import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { readThreadShell } from "../../../state/entities";
import { serverEnvironment } from "../../../state/server";
import { readEnvironmentScope } from "../../../state/session";
import { stackedThreadToast, toastManager } from "../../ui/toast";
import {
  type ThreadRoleMenuId,
  type ThreadRoleMenuState,
  threadRoleMenuState,
  threadRolePatch,
} from "./threadRoleMenu.logic";
import { installThreadRoleMenuRuntime } from "./threadRoleMenu";

const readRoles = (threadRef: ScopedThreadRef) =>
  appAtomRegistry.get(serverEnvironment.settingsValueAtom(threadRef.environmentId))?.threadRoles;

/** The "Role" entry's state for the thread menu, or none. */
function readThreadRoleMenuState(threadRef: ScopedThreadRef): ThreadRoleMenuState | undefined {
  return threadRoleMenuState({
    thread: readThreadShell(threadRef),
    roles: readRoles(threadRef),
    canWriteSettings: readEnvironmentScope(threadRef.environmentId, AuthSettingsWriteScope),
    titleOf: (threadId) =>
      readThreadShell({ environmentId: threadRef.environmentId, threadId: threadId as ThreadId })
        ?.title ?? null,
  });
}

/** Runs a "Role" pick (the caller has checked the id). */
function runThreadRoleMenuAction(threadRef: ScopedThreadRef, id: ThreadRoleMenuId): void {
  const thread = readThreadShell(threadRef);
  const roles = readRoles(threadRef);
  if (thread === null || roles == null) return;
  const patch = threadRolePatch(id, thread, roles);
  if (patch === null) return;
  void runAtomCommand(
    appAtomRegistry,
    serverEnvironment.updateSettings,
    { environmentId: threadRef.environmentId, input: { patch } },
    { reportFailure: false },
  ).then((result) => {
    if (result._tag !== "Failure" || isAtomCommandInterrupted(result)) return;
    toastManager.add(
      stackedThreadToast({
        type: "error",
        title: "Role not saved",
        description:
          thrownErrorMessage(squashAtomCommandFailure(result), m) ?? "The server refused it.",
      }),
    );
  });
}

installThreadRoleMenuRuntime({ read: readThreadRoleMenuState, run: runThreadRoleMenuAction });
