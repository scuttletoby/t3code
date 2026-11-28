/**
 * Fork (assistant): the switch `assistant.enabled` and "Make assistant", which makes a thread in the
 * Scratch project and names it the assistant (`threadRoles.assistant`) with the built-in profile
 * `assistant`. Server-wide, so a project screen does not show it. Every write is an ordinary settings
 * save from the person at the screen; agents cannot change these keys. Docs: docs/fork/assistant.md.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_PROVIDER_INTERACTION_MODE,
  DEFAULT_RUNTIME_MODE,
  type EnvironmentId,
  type ScopedThreadRef,
  type ServerSettingsPatch,
} from "@t3tools/contracts";
import { scopeThreadRef } from "@t3tools/client-runtime/environment";
import { useNavigate } from "@tanstack/react-router";
import { useRef, useState } from "react";

import { newThreadId } from "~/lib/utils";
import { useScratchProject } from "../../../hooks/useScratchProject";
import { appAtomRegistry } from "../../../rpc/atomRegistry";
import { useThreadShells, waitForThreadShell } from "../../../state/entities";
import { useEnvironmentProviders } from "../../../state/environments";
import { environmentThreadShells, threadEnvironment } from "../../../state/threads";
import { useAtomCommand } from "../../../state/use-atom-command";
import { waitForAtomValue } from "../../../state/waitForAtomValue";
import { buildThreadRouteParams } from "../../../threadRoutes";
import { Button } from "../../ui/button";
import { Switch } from "../../ui/switch";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useUpdateScopedSettings } from "../useScopedSettings";
import { ForkRow } from "./ForkRow";
import {
  assistantModelSelection,
  assistantThreadState,
  assistantThreadText,
  makeAssistantThread,
} from "./assistantSetup.logic";
import { defaultIs } from "./forkDefaultText.logic";

/** True once this client holds no thread `ref` (deleted, or never made); false after 5 seconds. */
const waitForThreadShellGone = (ref: ScopedThreadRef) =>
  waitForAtomValue({
    registry: appAtomRegistry,
    atom: environmentThreadShells.threadShellAtom(ref),
    predicate: (thread) => thread === null,
    timeoutMs: 5_000,
  });

export function AssistantSettings() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const { targets } = useSettingsScope();
  const { openScratchProject } = useScratchProject();
  const createThread = useAtomCommand(threadEnvironment.create, { reportFailure: false });
  const deleteThread = useAtomCommand(threadEnvironment.delete, { reportFailure: false });
  const providers = useEnvironmentProviders();
  const shells = useThreadShells();
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const running = useRef(false);
  const [error, setError] = useState<string | null>(null);

  const target = targets.length === 1 ? targets[0]! : null;
  if (target === null) {
    return (
      <SettingsSection id="assistant-section" title="Assistant">
        <ForkRow
          serverScoped
          {...searchableSetting("assistant")}
          settingKeys={["assistant"]}
          lockPaths={["assistant"]}
          mixed
          description="The assistant is set per environment. Select one environment to set it up."
        />
      </SettingsSection>
    );
  }
  const environmentId: EnvironmentId = target.environmentId;
  const enabled = target.settings.assistant.enabled;
  const state = assistantThreadState(
    target.settings.threadRoles.assistant,
    shells.filter((thread) => thread.environmentId === environmentId),
  );
  const modelSelection = assistantModelSelection(providers.get(environmentId) ?? []);

  // Resolves once the save settled: true when every target saved it.
  const save = (patch: ServerSettingsPatch) =>
    new Promise<boolean>((resolve) =>
      updateSettings(patch, { onFailed: () => resolve(false), onSaved: () => resolve(true) }),
    );

  const makeAssistant = () => {
    // A ref, not only the state: a double click arrives before the disabled button re-renders.
    if (modelSelection === null || running.current) return;
    running.current = true;
    setBusy(true);
    setError(null);
    // `makeAssistantThread` never rejects; `.finally` still leaves the busy state on any path.
    void makeAssistantThread({
      openScratchProject: async () => (await openScratchProject(environmentId))?.id ?? null,
      newThreadId,
      save,
      createThread: async (threadId, projectId) =>
        (
          await createThread({
            environmentId,
            input: {
              threadId,
              projectId,
              title: "Assistant",
              modelSelection,
              runtimeMode: DEFAULT_RUNTIME_MODE,
              interactionMode: DEFAULT_PROVIDER_INTERACTION_MODE,
              branch: null,
              worktreePath: null,
            },
          })
        )._tag !== "Failure",
      waitForThread: (threadId) => waitForThreadShell(scopeThreadRef(environmentId, threadId)),
      discardThread: async (threadId) =>
        (await deleteThread({ environmentId, input: { threadId, onlyIfUnused: true } }))._tag !==
          "Failure" && (await waitForThreadShellGone(scopeThreadRef(environmentId, threadId))),
    })
      .then((outcome) => {
        if (outcome._tag === "Stopped") {
          setError(outcome.error);
          return;
        }
        const ref = scopeThreadRef(environmentId, outcome.threadId);
        void navigate({ to: "/$environmentId/$threadId", params: buildThreadRouteParams(ref) });
      })
      .finally(() => {
        running.current = false;
        setBusy(false);
      });
  };

  return (
    <SettingsSection id="assistant-section" title="Assistant">
      <ForkRow
        serverScoped
        {...searchableSetting("assistant")}
        settingKeys={["assistant"]}
        lockPaths={["assistant"]}
        description={`On: Settings offers Make assistant, one Claude thread you talk to that works across all your projects. Off: no assistant is offered. ${defaultIs("assistant")} Its profile \`assistant\` has no shell, files, web or outside servers, and of T3's tools only those that read, start and message work: it can read threads and projects, delegate tasks, make and launch threads, and write to threads, without asking you. It cannot delete, stop, interrupt, move or change threads, projects, tasks or environments; you do that. It acts only on what you tell it: text it reads (other threads, mails, pages, documents) is data, not instructions.`}
        control={
          <Switch
            checked={enabled}
            onCheckedChange={(checked) =>
              updateSettings({ assistant: { enabled: Boolean(checked) } })
            }
            aria-label="Offer Make assistant"
          />
        }
      />
      {enabled ? (
        <ForkRow
          serverScoped
          {...searchableSetting("assistant-thread")}
          settingKeys={["threadRoles"]}
          lockPaths={["threadRoles", "mcpThreadProfiles"]}
          status={error}
          description={`${assistantThreadText(state)} Make assistant makes a new thread in the Scratch project and names it the assistant; an earlier one stays as it is.${
            modelSelection === null ? " It needs a Claude provider that is on and available." : ""
          }`}
          control={
            <Button
              size="sm"
              variant="outline"
              disabled={busy || modelSelection === null}
              onClick={() => makeAssistant()}
            >
              Make assistant
            </Button>
          }
        />
      ) : null}
    </SettingsSection>
  );
}
