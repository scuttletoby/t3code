/**
 * Fork: the Settings page "Fork" (/settings/fork), every fork setting in one place. Each group
 * mounts the fork's own panels; a project screen shows only the settings a project may override.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";
import type { ReactNode } from "react";

import { ReviewRulesSettings } from "../ReviewRulesSettings";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsPageContainer, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { WaitsAndSlotsSettings } from "../WaitsAndSlotsSettings";
import { AssistantSettings } from "./AssistantSettings";
import { forkFeatureStatuses } from "./forkStatus.logic";
import { HandoffSettings } from "./HandoffSettings";
import { McpProfileSettings } from "./McpProfileSettings";
import { PlaytestSettings } from "./PlaytestSettings";
import { StopFinishedChildrenSettings } from "./StopFinishedChildrenSettings";
import { UpdatesSettings } from "./UpdatesSettings";
import { ModelPolicySettings } from "./ModelPolicySettings";
import { UsagePacingSettings } from "./UsagePacingSettings";

export function ForkSettingsPanel() {
  const { connectedEnvironments, scope } = useSettingsScope();
  const isProjectScope = scope.kind === "project" || scope.kind === "checkout";
  if (connectedEnvironments.length === 0) {
    return (
      <SettingsPageContainer>
        <SettingsSection id="fork-status-section" title="Fork">
          <SettingsRow
            title="No server connected"
            description="Fork settings live on a server. Connect one to see and change them."
          />
        </SettingsSection>
      </SettingsPageContainer>
    );
  }
  return (
    <SettingsPageContainer>
      <ForkStatusSection />
      <ForkGroup title="Agents">
        <WaitsAndSlotsSettings isProjectScope={isProjectScope} />
        {isProjectScope ? null : <UsagePacingSettings />}
        {isProjectScope ? null : <ModelPolicySettings />}
        {isProjectScope ? null : <StopFinishedChildrenSettings />}
      </ForkGroup>
      <ForkGroup title="Reviews">
        <ReviewRulesSettings isProjectScope={isProjectScope} />
      </ForkGroup>
      {isProjectScope ? null : <UpdatesSettings />}
      <HandoffSettings isProjectScope={isProjectScope} />
      <PlaytestSettings isProjectScope={isProjectScope} />
      <McpProfileSettings isProjectScope={isProjectScope} />
      {isProjectScope ? null : <AssistantSettings />}
    </SettingsPageContainer>
  );
}

function ForkStatusSection() {
  const searchableSetting = useSearchableSetting();
  const { targets } = useSettingsScope();
  const statuses = forkFeatureStatuses(
    targets.length > 0 ? targets : [{ projectId: null, settings: DEFAULT_SERVER_SETTINGS }],
  );
  return (
    <SettingsSection id="fork-status-section" title="Overview">
      <SettingsRow
        {...searchableSetting("fork-status")}
        description="What the selected server or project has on, by the fork report's rules: on a server, a project's override or one thread's handoff entry counts too. Every feature starts off."
      >
        <ul className="grid gap-x-8 gap-y-1 pb-2 text-xs sm:grid-cols-2">
          {statuses.map((status) => (
            <li key={status.key} className="flex justify-between gap-2">
              <span className="text-muted-foreground">{status.label}</span>
              <span className={status.on ? "font-medium text-foreground" : "text-muted-foreground"}>
                {status.detail}
              </span>
            </li>
          ))}
        </ul>
      </SettingsRow>
    </SettingsSection>
  );
}

function ForkGroup({ title, children }: { readonly title: string; readonly children: ReactNode }) {
  return (
    <div role="group" aria-label={title} className="flex flex-col gap-8">
      <p className="-mb-4 px-3 text-xs font-medium uppercase tracking-wide text-muted-foreground sm:px-4">
        {title}
      </p>
      {children}
    </div>
  );
}
