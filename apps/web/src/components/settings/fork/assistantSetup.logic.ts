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

/** The one save that makes `threadId` the assistant: its profile pick and the role pointer. */
export const makeAssistantPatch = (threadId: ThreadId): ServerSettingsPatch => ({
  mcpThreadProfiles: { [threadId]: MCP_PROFILE_ASSISTANT },
  threadRoles: { assistant: threadId },
});

/** The thread `threadRoles.assistant` names, as Settings shows it. */
export type AssistantThreadState =
  | { readonly kind: "none" }
  | { readonly kind: "gone"; readonly threadId: string }
  | { readonly kind: "archived" | "ok"; readonly threadId: string; readonly title: string };

/** Readers treat a deleted or archived assistant thread as no assistant (threadRoles docs). */
export function assistantThreadState(
  assistant: string | null,
  threads: ReadonlyArray<{
    readonly id: string;
    readonly title: string;
    readonly archivedAt: string | null;
  }>,
): AssistantThreadState {
  if (assistant === null) return { kind: "none" };
  const thread = threads.find((candidate) => candidate.id === assistant);
  if (thread === undefined) return { kind: "gone", threadId: assistant };
  return {
    kind: thread.archivedAt === null ? "ok" : "archived",
    threadId: assistant,
    title: thread.title,
  };
}

export function assistantThreadText(state: AssistantThreadState): string {
  switch (state.kind) {
    case "none":
      return "No thread is the assistant yet.";
    case "gone":
      return `The assistant was thread ${state.threadId}, which is deleted or not on this server. Make a new one.`;
    case "archived":
      return `The assistant is "${state.title}", which is archived, so it counts as none. Unarchive it or make a new one.`;
    case "ok":
      return `The assistant is "${state.title}".`;
  }
}
