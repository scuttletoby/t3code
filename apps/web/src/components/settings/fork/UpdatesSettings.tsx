/**
 * Fork: automatic updates (`automaticUpdates`, `automaticUpdatesQuietCommand`). Server-wide, so a
 * project screen does not show it. Every write is an ordinary settings patch from the person at the
 * screen; agents cannot change it.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";

import { Switch } from "../../ui/switch";
import { AdvancedSettings } from "../AdvancedSettings";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { targetsDiffer } from "../waitsAndSlotsSettings.logic";
import { useScopedSettings, useUpdateScopedSettings } from "../useScopedSettings";
import { CommitTextField } from "./CommitTextField";
import { defaultIs } from "./forkDefaultText.logic";

const MIXED = "The selected environments have different values. Select one environment to edit it.";

export function UpdatesSettings() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const { targets } = useSettingsScope();
  const automatic = useScopedSettings((settings) => settings.automaticUpdates);
  const quietCommand = useScopedSettings((settings) => settings.automaticUpdatesQuietCommand);
  const automaticMixed = targetsDiffer(targets, (settings) => settings.automaticUpdates);
  const quietMixed = targetsDiffer(targets, (settings) => settings.automaticUpdatesQuietCommand);

  return (
    <SettingsSection id="updates-section" title="Automatic updates">
      <SettingsRow
        serverScoped
        {...searchableSetting("automatic-updates")}
        settingKeys={["automaticUpdates"]}
        mixed={automaticMixed}
        description={
          automaticMixed
            ? MIXED
            : `On: a server that starts with the computer (a boot service) updates itself to the newest fork release once no thread is working. Off: it updates only when you run t3 update or press Update. ${defaultIs("automaticUpdates")}`
        }
        resetAction={
          !automaticMixed && automatic !== DEFAULT_SERVER_SETTINGS.automaticUpdates ? (
            <SettingResetButton
              ariaLabel="Reset automatic updates to default"
              onClick={() =>
                updateSettings({ automaticUpdates: DEFAULT_SERVER_SETTINGS.automaticUpdates })
              }
            />
          ) : null
        }
        control={
          <Switch
            mixed={automaticMixed}
            checked={automaticMixed ? false : automatic}
            onCheckedChange={(checked) => updateSettings({ automaticUpdates: Boolean(checked) })}
            aria-label="Update automatically"
          />
        }
      />
      <AdvancedSettings targetIds={["automatic-updates-quiet-command"]}>
        <SettingsRow
          serverScoped
          {...searchableSetting("automatic-updates-quiet-command")}
          settingKeys={["automaticUpdatesQuietCommand"]}
          mixed={quietMixed}
          description={
            quietMixed
              ? MIXED
              : `A program (its full path) asked before each automatic update; a non-zero exit means not now, so the update waits. Empty: no program is asked. ${defaultIs("automaticUpdatesQuietCommand")}`
          }
          resetAction={
            !quietMixed && quietCommand !== "" ? (
              <SettingResetButton
                ariaLabel="Reset quiet check to default"
                onClick={() => updateSettings({ automaticUpdatesQuietCommand: "" })}
              />
            ) : null
          }
          control={
            quietMixed ? null : (
              <CommitTextField
                value={quietCommand}
                ariaLabel="Quiet check before an update"
                placeholder="/usr/local/bin/quiet-check"
                onCommit={(text, revert) => {
                  updateSettings(
                    { automaticUpdatesQuietCommand: text.trim() },
                    { onFailed: revert },
                  );
                  return null;
                }}
              />
            )
          }
        />
      </AdvancedSettings>
    </SettingsSection>
  );
}
