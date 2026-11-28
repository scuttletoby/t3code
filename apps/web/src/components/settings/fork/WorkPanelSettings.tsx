/**
 * Fork (work panel, fork issue 1136): the switch `workPanelEnabled` (this device only, off by
 * default) that shows CI and capacity in a thread's side panel, next to its sub-agents.
 * Docs: docs/fork/README.md.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_CLIENT_SETTINGS } from "@t3tools/contracts";

import { updateClientSettings, useClientSettings } from "../../../hooks/useSettings";
import { Switch } from "../../ui/switch";
import { SettingResetButton, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";

export function WorkPanelSettings() {
  const searchableSetting = useSearchableSetting();
  const enabled = useClientSettings((settings) => settings.workPanelEnabled);
  return (
    <SettingsSection id="work-panel-section" title="Work panel">
      <SettingsRow
        {...searchableSetting("work-panel")}
        description="On: a thread's side panel shows, after its sub-agents, the CI of the pull requests it and its sub-agents link and the repository's queue. This device only. Off (default): as usual."
        resetAction={
          enabled !== DEFAULT_CLIENT_SETTINGS.workPanelEnabled ? (
            <SettingResetButton
              ariaLabel="Reset work panel to default"
              onClick={() =>
                void updateClientSettings({
                  workPanelEnabled: DEFAULT_CLIENT_SETTINGS.workPanelEnabled,
                })
              }
            />
          ) : null
        }
        control={
          <Switch
            checked={enabled}
            onCheckedChange={(checked) =>
              void updateClientSettings({ workPanelEnabled: Boolean(checked) })
            }
            aria-label="Work panel"
          />
        }
      />
    </SettingsSection>
  );
}
