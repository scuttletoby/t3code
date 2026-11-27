/**
 * Fork: the Settings page "Fork" (/settings/fork), every fork setting in one place, grouped by what a
 * person wants to do. Each group mounts the fork's own panels; a project screen shows only the
 * settings a project may override, and a group with nothing to show is left out.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";
import { Children, type ReactNode, useId } from "react";

import { ReviewRulesSettings } from "../ReviewRulesSettings";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsPageContainer, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { AgentSlotsSettings, WaitsSettings } from "../WaitsAndSlotsSettings";
import { AssistantSettings } from "./AssistantSettings";
import { forkFeatureStatuses } from "./forkStatus.logic";
import { HandoffSettings } from "./HandoffSettings";
import { McpProfileSettings } from "./McpProfileSettings";
import { OwnerQuestionsSettings } from "./OwnerQuestionsSettings";
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
  const server = (node: ReactNode) => (isProjectScope ? null : node);
  return (
    <SettingsPageContainer>
      <ForkStatusSection />
      <ForkGroup
        title="Plan usage and machine load"
        description="Keep part of each plan for yourself and stop agents from crowding a machine."
      >
        {server(<UsagePacingSettings />)}
        {server(<AgentSlotsSettings />)}
        {server(<StopFinishedChildrenSettings />)}
      </ForkGroup>
      <ForkGroup
        title="Waiting instead of polling"
        description="Agents end their turn and are woken when something they wait for happens."
      >
        <WaitsSettings isProjectScope={isProjectScope} />
      </ForkGroup>
      <ForkGroup
        title="Models and tools agents get"
        description="Which models agents may pick and which tools each thread sees."
      >
        {server(<ModelPolicySettings />)}
        <McpProfileSettings isProjectScope={isProjectScope} />
      </ForkGroup>
      {/* A group of one section shows no group heading: the section's own title names it. */}
      <HandoffSettings isProjectScope={isProjectScope} />
      <ReviewRulesSettings isProjectScope={isProjectScope} />
      {server(<AssistantSettings />)}
      <PlaytestSettings isProjectScope={isProjectScope} />
      {server(<UpdatesSettings />)}
      {server(<OwnerQuestionsSettings />)}
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
        description="Features this fork adds. All start off, so untouched settings work like upstream T3 Code. You change them here or in the settings file. Below: what is on (a project or thread turning one on counts)."
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

/** A heading for related sections, left out when its sections have nothing to show here. */
function ForkGroup({
  title,
  description,
  children,
}: {
  readonly title: string;
  readonly description: string;
  readonly children: ReactNode;
}) {
  const titleId = useId();
  const descriptionId = useId();
  if (Children.toArray(children).length === 0) return null;
  return (
    <div
      role="group"
      aria-labelledby={titleId}
      aria-describedby={descriptionId}
      className="flex flex-col gap-8"
    >
      <div className="-mb-4 px-3 sm:px-4">
        <p
          id={titleId}
          className="text-xs font-medium uppercase tracking-wide text-muted-foreground"
        >
          {title}
        </p>
        <p id={descriptionId} className="text-xs text-muted-foreground">
          {description}
        </p>
      </div>
      {children}
    </div>
  );
}
