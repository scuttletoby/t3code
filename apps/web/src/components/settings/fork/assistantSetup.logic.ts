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
  type ProjectId,
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

/** Removes the first save's pick, once its thread is gone (or was never made). */
export const assistantPickRemovalPatch = (threadId: ThreadId): ServerSettingsPatch => ({
  mcpThreadProfiles: { [threadId]: null },
});

/** The steps "Make assistant" takes; the Settings row passes the app's own. */
export interface MakeAssistantSteps {
  /** The Scratch project's id, or null when there is none (the hook has said why). */
  readonly openScratchProject: () => Promise<ProjectId | null>;
  readonly newThreadId: () => ThreadId;
  /** One settings save from the person at the screen: true when it saved. */
  readonly save: (patch: ServerSettingsPatch) => Promise<boolean>;
  /** Makes the thread "Assistant" in the project: true when the server took the command. */
  readonly createThread: (threadId: ThreadId, projectId: ProjectId) => Promise<boolean>;
  /** True once the new thread has reached this client. */
  readonly waitForThread: (threadId: ThreadId) => Promise<boolean>;
  /**
   * Deletes the thread only if it has no message or run yet (`onlyIfUnused`): true only when the
   * delete was taken and this client then sees no such thread. A thread someone wrote in stays.
   */
  readonly discardThread: (threadId: ThreadId) => Promise<boolean>;
}

export type MakeAssistantOutcome =
  | { readonly _tag: "Made"; readonly threadId: ThreadId }
  /** Nothing named: `error` says why (null when the Scratch hook already said it). */
  | { readonly _tag: "Stopped"; readonly error: string | null };

const stopped = (error: string | null): MakeAssistantOutcome => ({ _tag: "Stopped", error });

/**
 * A run that stopped after the pick: the empty thread is deleted, and only once it is gone is its
 * pick removed, so retries leave no picks behind. A thread that stays (someone wrote in it, or the
 * delete was not taken) keeps its narrow pick: removing it would give the thread the defaults.
 */
async function discardUnused(steps: MakeAssistantSteps, threadId: ThreadId): Promise<void> {
  try {
    if (await steps.discardThread(threadId)) await steps.save(assistantPickRemovalPatch(threadId));
  } catch {
    // The pick stays, which is harmless: a pick for a thread that does not exist does nothing.
  }
}

/** The run's steps in order; `run.picked` is set once a pick may have been saved. */
async function makeAssistantSteps(
  steps: MakeAssistantSteps,
  run: { picked: ThreadId | null },
): Promise<MakeAssistantOutcome> {
  const projectId = await steps.openScratchProject();
  if (projectId === null) return stopped(null);
  const threadId = steps.newThreadId();
  // A save that throws may still have saved the pick, so the clean-up counts from here on.
  run.picked = threadId;
  if (!(await steps.save(assistantProfilePatch(threadId)))) {
    // Refused: no pick was saved, so there is nothing to clean up.
    run.picked = null;
    return stopped("The server refused the assistant profile. Nothing was made.");
  }
  if (!(await steps.createThread(threadId, projectId))) {
    return stopped("Could not make the assistant's thread. Try again.");
  }
  if (!(await steps.waitForThread(threadId))) {
    return stopped("The assistant's thread was made, but it did not reach this client. Try again.");
  }
  if (!(await steps.save(assistantRolePatch(threadId)))) {
    return stopped(
      "The server refused to name the new thread the assistant. It is removed unless someone already wrote in it.",
    );
  }
  return { _tag: "Made", threadId };
}

/**
 * "Make assistant": the pick first (a stop at any later point leaves no thread, or one already
 * narrowed), then the thread, then the role. A run that stops after the pick cleans up
 * ({@link discardUnused}). Never rejects: a step that throws stops the run with an error, so the
 * caller always gets an outcome and leaves its busy state.
 */
export async function makeAssistantThread(
  steps: MakeAssistantSteps,
): Promise<MakeAssistantOutcome> {
  const run: { picked: ThreadId | null } = { picked: null };
  let outcome: MakeAssistantOutcome;
  try {
    outcome = await makeAssistantSteps(steps, run);
  } catch {
    outcome = stopped(
      "Something went wrong while making the assistant. Nothing was named. Try again.",
    );
  }
  if (outcome._tag === "Stopped" && run.picked !== null) await discardUnused(steps, run.picked);
  return outcome;
}
