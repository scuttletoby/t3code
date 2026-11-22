/**
 * Fork (issue 925): the switch `stopFinishedChildren.enabled` and its grace time. Server-wide, so a
 * project screen does not show it. Every write is an ordinary settings save from the person at the
 * screen; agents cannot change these keys.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_SERVER_SETTINGS } from "@t3tools/contracts";

import { Switch } from "../../ui/switch";
import { IntervalSecondsField } from "../IntervalSecondsField";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useScopedSettings, useUpdateScopedSettings } from "../useScopedSettings";
import { targetsDiffer } from "../waitsAndSlotsSettings.logic";
import { ForkRow } from "./ForkRow";

const DEFAULTS = DEFAULT_SERVER_SETTINGS.stopFinishedChildren;

export function StopFinishedChildrenSettings() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const { targets } = useSettingsScope();
  const current = useScopedSettings((settings) => settings.stopFinishedChildren);
  const enabledMixed = targetsDiffer(targets, (settings) => settings.stopFinishedChildren.enabled);
  const graceMixed = targetsDiffer(
    targets,
    (settings) => settings.stopFinishedChildren.graceSeconds,
  );
  return (
    <SettingsSection id="stop-finished-children-section" title="Finished children">
      <ForkRow
        serverScoped
        {...searchableSetting("stop-finished-children")}
        description="Stops a finished delegated task's provider process once its thread has been idle for the grace time, so it no longer holds memory. A message to the thread later starts a new process with its history. Never a thread that works, waits or asks."
        settingKeys={["stopFinishedChildren"]}
        lockPaths={["stopFinishedChildren"]}
        mixed={enabledMixed}
        resetAction={
          !enabledMixed && current.enabled !== DEFAULTS.enabled ? (
            <SettingResetButton
              ariaLabel="Reset stopping finished children to default"
              onClick={() =>
                updateSettings({ stopFinishedChildren: { enabled: DEFAULTS.enabled } })
              }
            />
          ) : null
        }
        control={
          <Switch
            mixed={enabledMixed}
            checked={enabledMixed ? false : current.enabled}
            onCheckedChange={(checked) =>
              updateSettings({ stopFinishedChildren: { enabled: Boolean(checked) } })
            }
            aria-label="Stop finished children"
          />
        }
      />
      <ForkRow
        serverScoped
        {...searchableSetting("stop-finished-children-grace")}
        description="How long a finished child stays idle before its process is stopped. Any activity in the thread starts the time again."
        settingKeys={["stopFinishedChildren"]}
        lockPaths={["stopFinishedChildren"]}
        mixed={graceMixed}
        resetAction={
          !graceMixed && current.graceSeconds !== DEFAULTS.graceSeconds ? (
            <SettingResetButton
              ariaLabel="Reset grace time to default"
              onClick={() =>
                updateSettings({ stopFinishedChildren: { graceSeconds: DEFAULTS.graceSeconds } })
              }
            />
          ) : null
        }
        control={
          <div className="flex items-center gap-2">
            <IntervalSecondsField
              labels={{
                decrease: "Decrease grace time",
                input: "Grace time in seconds",
                increase: "Increase grace time",
              }}
              value={current.graceSeconds}
              min={0}
              step={10}
              onCommit={(seconds, options) =>
                updateSettings({ stopFinishedChildren: { graceSeconds: seconds } }, options)
              }
            />
            <span className="text-xs text-muted-foreground">seconds</span>
          </div>
        }
      />
    </SettingsSection>
  );
}
