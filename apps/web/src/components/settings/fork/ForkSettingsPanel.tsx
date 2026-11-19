/**
 * Fork: the Settings page "Fork" (/settings/fork), every fork setting in one place. Each group
 * mounts the fork's own panels; a project screen shows only the settings a project may override.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this page is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import type { ReactNode } from "react";

import { ReviewRulesSettings } from "../ReviewRulesSettings";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsPageContainer, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useScopedSettings } from "../useScopedSettings";
import { WaitsAndSlotsSettings } from "../WaitsAndSlotsSettings";
import { forkFeatureStatuses } from "./forkStatus.logic";
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
      </ForkGroup>
      <ForkGroup title="Reviews">
        <ReviewRulesSettings isProjectScope={isProjectScope} />
      </ForkGroup>
    </SettingsPageContainer>
  );
}

function ForkStatusSection() {
  const searchableSetting = useSearchableSetting();
  const statuses = forkFeatureStatuses(useScopedSettings());
  return (
    <SettingsSection id="fork-status-section" title="Overview">
      <SettingsRow
        {...searchableSetting("fork-status")}
        description="What the selected server or project has switched on. Every fork feature starts off."
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
