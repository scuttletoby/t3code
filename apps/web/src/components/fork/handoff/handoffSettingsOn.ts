/**
 * Fork (HO5): whether the app's server may hand off a thread, from the config this app holds, by
 * the server's rule (handoffOwner.ts); the menus skip the state read when it may not. Unsure (no
 * config or no thread yet) counts as on: the read then answers.
 */
import { holdsThreadRole, resolveHandoffSettings, type ScopedThreadRef } from "@t3tools/contracts";

import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { readThreadShell } from "../../../state/entities";
import { primaryServerConfigAtom } from "../../../state/server";

export function handoffMaybeOn(threadRef: ScopedThreadRef): boolean {
  const settings = appAtomRegistry.get(primaryServerConfigAtom)?.settings;
  const thread = readThreadShell(threadRef);
  if (settings === undefined || thread === null) return true;
  const { projectSettingsOverrides: projects } = settings;
  const project = Object.hasOwn(projects, thread.projectId)
    ? projects[thread.projectId]?.handoff
    : undefined;
  return (
    resolveHandoffSettings(
      settings.handoff,
      project,
      thread.id,
      holdsThreadRole(settings.threadRoles, thread),
    ).mode === "handoff"
  );
}
