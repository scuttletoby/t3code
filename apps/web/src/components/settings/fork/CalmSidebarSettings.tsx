/**
 * Fork (calm sidebar): the calm view's switch (`calmSidebarEnabled`, this device only) and the list
 * of `threadRoles` on the selected server, each pointer marked when its thread cannot hold the role
 * now, with Remove. Roles are made from a thread's menu (Role). Every write is an ordinary settings
 * save from the person at the screen; agents cannot change `threadRoles`. Docs: docs/fork/README.md.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_CLIENT_SETTINGS } from "@t3tools/contracts";

import { updateClientSettings, useClientSettings } from "../../../hooks/useSettings";
import { useProjects, useThreadShells } from "../../../state/entities";
import { Button } from "../../ui/button";
import { Switch } from "../../ui/switch";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useUpdateScopedSettings } from "../useScopedSettings";
import { ForkRow } from "./ForkRow";
import { removeRolePatch, roleEntryText, roleListEntries } from "./calmSidebarSettings.logic";

export function CalmSidebarSettings() {
  const searchableSetting = useSearchableSetting();
  const enabled = useClientSettings((settings) => settings.calmSidebarEnabled);
  return (
    <SettingsSection id="calm-sidebar-section" title="Calm sidebar">
      <SettingsRow
        {...searchableSetting("calm-sidebar")}
        description="On: the assistant and each project's orchestrator go on top of the sidebar; other threads fold into Other threads, with how many wait on you. This device only. Off (default): as usual."
        resetAction={
          enabled !== DEFAULT_CLIENT_SETTINGS.calmSidebarEnabled ? (
            <SettingResetButton
              ariaLabel="Reset calm sidebar to default"
              onClick={() =>
                void updateClientSettings({
                  calmSidebarEnabled: DEFAULT_CLIENT_SETTINGS.calmSidebarEnabled,
                })
              }
            />
          ) : null
        }
        control={
          <Switch
            checked={enabled}
            onCheckedChange={(checked) =>
              void updateClientSettings({ calmSidebarEnabled: Boolean(checked) })
            }
            aria-label="Calm sidebar"
          />
        }
      />
      <ThreadRolesRow />
    </SettingsSection>
  );
}

function ThreadRolesRow() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const { targets } = useSettingsScope();
  const shells = useThreadShells();
  const projects = useProjects();
  const target = targets.length === 1 ? targets[0]! : null;
  const common = {
    serverScoped: true,
    ...searchableSetting("thread-roles"),
    settingKeys: ["threadRoles"],
    lockPaths: ["threadRoles"],
  } as const;
  if (target === null) {
    return (
      <ForkRow
        {...common}
        mixed
        description="Roles are set per environment. Select one environment to see them."
      />
    );
  }
  const environmentId = target.environmentId;
  const entries = roleListEntries(
    target.settings.threadRoles,
    shells.filter((thread) => thread.environmentId === environmentId),
  );
  const projectTitle = (projectId: string) =>
    projects.find((project) => project.environmentId === environmentId && project.id === projectId)
      ?.title ?? null;
  return (
    <ForkRow
      {...common}
      description="The assistant and one orchestrator per project; change them from a thread's menu (Role). A marked role gives no role until you remove or fix it."
    >
      {entries.length === 0 ? (
        <p className="pb-2 text-xs text-muted-foreground">No thread holds a role.</p>
      ) : (
        <ul className="grid gap-1 pb-2 text-xs" aria-label="Thread roles">
          {entries.map((entry) => {
            const text = roleEntryText(entry, projectTitle);
            return (
              <li
                key={entry.key}
                data-testid={`thread-role-${entry.key}`}
                className="flex items-center justify-between gap-2"
              >
                <span className="min-w-0">
                  <span className="text-muted-foreground">{text.role}: </span>
                  <span className="font-medium text-foreground">{text.thread}</span>
                  {text.problem === null ? null : (
                    <span className="ms-2 text-warning-foreground">({text.problem})</span>
                  )}
                </span>
                <Button
                  size="compact"
                  variant="outline"
                  aria-label={`Remove ${text.role.toLowerCase()} role`}
                  onClick={() => updateSettings(removeRolePatch(entry))}
                >
                  Remove
                </Button>
              </li>
            );
          })}
        </ul>
      )}
    </ForkRow>
  );
}
