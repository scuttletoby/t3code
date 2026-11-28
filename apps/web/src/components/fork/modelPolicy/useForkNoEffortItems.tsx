/**
 * Fork (model policy): the composer banner for a thread whose next turn names no effort
 * while the model policy is on (`noEffortWarning`). One ChatView line calls it with the route's
 * thread; its item joins both banner lists (.github/fork-seams.json).
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this banner is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import { scopedThreadKey, scopeProjectRef } from "@t3tools/client-runtime/environment";
import { resolveProvidersForCwd } from "@t3tools/client-runtime/providerSkills";
import { threadSupportsProviderHandoff } from "@t3tools/client-runtime/state/thread-workflows";
import { type ModelSelection, type ScopedThreadRef, type ServerProvider } from "@t3tools/contracts";
import { createModelSelection } from "@t3tools/shared/model";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { projectScriptCwd } from "@t3tools/shared/projectScripts";
import { TriangleAlertIcon } from "lucide-react";

import { useComposerDraftModelState, useComposerDraftStore } from "../../../composerDraftStore";
import { useNowMinute } from "../../../hooks/useNowMinute";
import { useEnvironmentSettings } from "../../../hooks/useSettings";
import { draftModelSelectionApplies, threadHasStarted } from "../../../lib/chatThreadActions";
import {
  resolveThreadDetailRef,
  useProject,
  useQuietThreadProjection,
  useQuietThreadShell,
} from "../../../state/entities";
import { useEnvironmentProviders } from "../../../state/environments";
import { deriveLockedProvider } from "../../ChatView.logic";
import { draftFanoutSelectionsAtom } from "../../chat/chatViewSend";
import type { ComposerBannerStackItem } from "../../chat/ComposerBannerStack";
import { useComposerProviderModel } from "../../chat/useComposerProviderModel";
import { draftThreadModelSelections, noEffortWarning } from "./noEffortWarning.logic";

const NO_ITEMS: ReadonlyArray<ComposerBannerStackItem> = [];
const NO_PROVIDERS: ReadonlyArray<ServerProvider> = [];

/**
 * The selections the thread's next turn sends: the composer's own selection hook
 * (`useComposerProviderModel`) fed what ChatView feeds the composer, so it reads the draft's
 * pick, else the thread's selection, else a new draft's project default (or fallback model).
 * A fan-out sends each of its models.
 */
function useNextTurnSelections(threadRef: ScopedThreadRef): {
  readonly selections: ReadonlyArray<ModelSelection>;
  readonly providers: ReadonlyArray<ServerProvider>;
  readonly known: boolean;
} {
  const { environmentId } = threadRef;
  const settings = useEnvironmentSettings(environmentId);
  const providersByEnvironment = useEnvironmentProviders();
  const nowMinute = useNowMinute();
  const draftThread = useComposerDraftStore((store) => store.getDraftSessionByRef(threadRef));
  const serverThread = useQuietThreadShell(threadRef);
  const serverProjection =
    useQuietThreadProjection(
      resolveThreadDetailRef(threadRef, {
        shellExists: serverThread !== null,
        waitForShell: draftThread !== null,
      }),
    )?.projection ?? null;
  const thread = serverThread ?? draftThread;
  const project = useProject(
    thread ? scopeProjectRef(thread.environmentId, thread.projectId) : null,
  );
  const draftSelections =
    serverThread === null && draftThread !== null
      ? draftThreadModelSelections({
          settings,
          project,
          projectId: draftThread.projectId,
          providers: providersByEnvironment.get(draftThread.environmentId) ?? NO_PROVIDERS,
          now: Date.parse(`${nowMinute}:00Z`),
        })
      : null;
  const threadModelSelection = serverThread?.modelSelection ?? draftSelections?.thread;
  const projectModelSelection = draftSelections
    ? draftSelections.project
    : resolveProjectSettings(settings, project?.id ?? null, project ?? undefined).settings
        .defaultModelSelection;
  const providers = resolveProvidersForCwd(
    (thread ? providersByEnvironment.get(thread.environmentId) : undefined) ?? NO_PROVIDERS,
    project
      ? projectScriptCwd({
          project: { cwd: project.workspaceRoot },
          worktreePath: thread?.worktreePath ?? null,
        })
      : null,
  );
  const threadStarted = threadHasStarted(serverThread);
  const draftModelState = useComposerDraftModelState(threadRef);
  const lockedProvider = threadSupportsProviderHandoff(serverProjection)
    ? null
    : deriveLockedProvider({
        thread: serverThread,
        selectedProvider: draftModelSelectionApplies(draftModelState, threadStarted)
          ? draftModelState.activeProvider
          : null,
        threadProvider:
          threadModelSelection?.instanceId ?? projectModelSelection?.instanceId ?? null,
        providers,
      });
  const runtimeInstanceId = serverThread?.runtime?.providerInstanceId;
  const { selectedInstanceId, selectedModel, composerProviderState } = useComposerProviderModel({
    providerStatuses: providers,
    settings,
    activeProjectId: project?.id ?? null,
    threadRef,
    threadModelSelection,
    projectModelSelection,
    // Only the composer's frame changes with an Ultrathink prompt, never what it sends.
    promptInjectionState: "none",
    threadStarted,
    candidateInstanceIds: [
      runtimeInstanceId,
      threadModelSelection?.instanceId,
      projectModelSelection?.instanceId,
    ],
    lockedProvider,
    lockedInstanceId: runtimeInstanceId ?? threadModelSelection?.instanceId,
  });
  const fanout = useAtomValue(draftFanoutSelectionsAtom(scopedThreadKey(threadRef)));
  const selection = createModelSelection(
    selectedInstanceId,
    selectedModel,
    composerProviderState.modelOptionsForDispatch,
  );
  return { selections: fanout ?? [selection], providers, known: thread !== null };
}

/** Zero or one banner: the thread's next turn names no effort while the model policy is on. */
export function useForkNoEffortItems(
  threadRef: ScopedThreadRef,
): ReadonlyArray<ComposerBannerStackItem> {
  const mode = useEnvironmentSettings(
    threadRef.environmentId,
    (settings) => settings.modelPolicy.mode,
  );
  const { selections, providers, known } = useNextTurnSelections(threadRef);
  const warning = known ? noEffortWarning({ mode, selections, providers }) : null;
  if (warning === null) return NO_ITEMS;
  return [
    {
      id: "fork-model-policy-no-effort",
      variant: "warning",
      icon: <TriangleAlertIcon />,
      title: "No effort set",
      description: `${warning.modelName} runs at its default level. Pick an effort in the model menu; the model policy expects one.`,
    },
  ];
}
