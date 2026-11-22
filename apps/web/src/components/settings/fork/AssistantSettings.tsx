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
} from "@t3tools/contracts";
import { scopeThreadRef } from "@t3tools/client-runtime/environment";
import { useNavigate } from "@tanstack/react-router";
import { useState } from "react";

import { newThreadId } from "~/lib/utils";
import { useScratchProject } from "../../../hooks/useScratchProject";
import { useThreadShells, waitForThreadShell } from "../../../state/entities";
import { useEnvironmentProviders } from "../../../state/environments";
import { threadEnvironment } from "../../../state/threads";
import { useAtomCommand } from "../../../state/use-atom-command";
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
  makeAssistantPatch,
} from "./assistantSetup.logic";

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

  const makeAssistant = async () => {
    if (modelSelection === null) return;
    setBusy(true);
    setError(null);
    try {
      const project = await openScratchProject(environmentId);
      if (project === null) return;
      const threadId = newThreadId();
      const created = await createThread({
        environmentId,
        input: {
          threadId,
          projectId: project.id,
          title: "Assistant",
          modelSelection,
          runtimeMode: DEFAULT_RUNTIME_MODE,
          interactionMode: DEFAULT_PROVIDER_INTERACTION_MODE,
          branch: null,
          worktreePath: null,
        },
      });
      if (created._tag === "Failure") {
        setError("Could not make the assistant's thread. Try again.");
        return;
      }
      // An empty thread the save did not name is removed; one that got a message stays.
      const discard = () =>
        void deleteThread({ environmentId, input: { threadId, onlyIfUnused: true } });
      const ref = scopeThreadRef(environmentId, threadId);
      if (!(await waitForThreadShell(ref))) {
        setError("The assistant's thread was made, but it did not reach this client. Try again.");
        discard();
        return;
      }
      updateSettings(makeAssistantPatch(threadId), {
        onFailed: () => {
          setError("The server refused to name the new thread the assistant. Nothing was changed.");
          discard();
        },
        onSaved: () =>
          void navigate({ to: "/$environmentId/$threadId", params: buildThreadRouteParams(ref) }),
      });
    } finally {
      setBusy(false);
    }
  };

  return (
    <SettingsSection id="assistant-section" title="Assistant">
      <ForkRow
        serverScoped
        {...searchableSetting("assistant")}
        settingKeys={["assistant"]}
        lockPaths={["assistant"]}
        description="One thread you talk to across projects. It runs with the assistant profile: no shell, files, web or outside servers; it can read, start and message threads. Claude only. Off: Settings offers no Make assistant."
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
          lockPaths={["threadRoles"]}
          status={error}
          description={`${assistantThreadText(state)} Make assistant makes a new thread in the Scratch project and names it the assistant; an earlier one stays as it is.${
            modelSelection === null ? " It needs a Claude provider that is on and available." : ""
          }`}
          control={
            <Button
              size="sm"
              variant="outline"
              disabled={busy || modelSelection === null}
              onClick={() => void makeAssistant()}
            >
              Make assistant
            </Button>
          }
        />
      ) : null}
    </SettingsSection>
  );
}
