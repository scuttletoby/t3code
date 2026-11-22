/**
 * Fork (assistant): the pure parts of "Make assistant". The thread is made in the Scratch project and
 * named the assistant by one settings save from the person at the screen; the server's role check
 * (apps/server/src/fork/threadRoleTargets.ts) refuses a thread that cannot hold the role.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_MODEL_BY_PROVIDER,
  MCP_PROFILE_ASSISTANT,
  ProviderDriverKind,
  type ModelSelection,
  type ServerProvider,
  type ServerSettingsPatch,
  type ThreadId,
} from "@t3tools/contracts";
import { createModelSelection } from "@t3tools/shared/model";

const CLAUDE = ProviderDriverKind.make("claudeAgent");

/**
 * The assistant's model: a Claude instance that is on and available (the default one first), with
 * Claude's default model when it offers it. Null when there is none: the assistant's profile has no
 * built-in tools, which only Claude can apply, so any other provider would refuse its turns.
 */
export function assistantModelSelection(
  providers: ReadonlyArray<ServerProvider>,
  defaultInstanceId: string = CLAUDE,
): ModelSelection | null {
  const usable = providers.filter(
    (provider) =>
      provider.driver === CLAUDE && provider.enabled && provider.availability !== "unavailable",
  );
  const pick = usable.find((provider) => provider.instanceId === defaultInstanceId) ?? usable[0];
  if (pick === undefined) return null;
  const wanted = DEFAULT_MODEL_BY_PROVIDER[CLAUDE];
  const model =
    pick.models.find((entry) => entry.slug === wanted) ??
    pick.models.find((entry) => entry.isDefault === true) ??
    pick.models[0];
  return model === undefined ? null : createModelSelection(pick.instanceId, model.slug);
}

/**
 * The first save: the thread's profile pick, made before the thread exists, so a thread named
 * "Assistant" never runs with a wider profile, even when the setup stops half-way.
 */
export const assistantProfilePatch = (threadId: ThreadId): ServerSettingsPatch => ({
  mcpThreadProfiles: { [threadId]: MCP_PROFILE_ASSISTANT },
});

/** The second save, once the thread exists: the role pointer (the server's role check decides). */
export const assistantRolePatch = (threadId: ThreadId): ServerSettingsPatch => ({
  threadRoles: { assistant: threadId },
});

/**
 * The thread `threadRoles.assistant` names, as Settings shows it. The client's thread list holds
 * active threads only, so an archived one reads as not active, like a deleted one.
 */
export type AssistantThreadState =
  | { readonly kind: "none" }
  | { readonly kind: "inactive"; readonly threadId: string }
  | { readonly kind: "ok"; readonly threadId: string; readonly title: string };

/** Readers treat a deleted or archived assistant thread as no assistant (threadRoles docs). */
export function assistantThreadState(
  assistant: string | null,
  activeThreads: ReadonlyArray<{ readonly id: string; readonly title: string }>,
): AssistantThreadState {
  if (assistant === null) return { kind: "none" };
  const thread = activeThreads.find((candidate) => candidate.id === assistant);
  return thread === undefined
    ? { kind: "inactive", threadId: assistant }
    : { kind: "ok", threadId: assistant, title: thread.title };
}

export function assistantThreadText(state: AssistantThreadState): string {
  switch (state.kind) {
    case "none":
      return "No thread is the assistant yet.";
    case "inactive":
      return `The assistant was thread ${state.threadId}, which is archived, deleted or not on this server, so there is none. Unarchive it or make a new one.`;
    case "ok":
      return `The assistant is "${state.title}".`;
  }
}
