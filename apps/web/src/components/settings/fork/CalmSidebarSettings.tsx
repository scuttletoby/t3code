/**
 * Fork (calm sidebar, CAL3 + CAL4): the calm view's switch (saved on this device) and each server's
 * thread roles, marking a role whose thread cannot hold it now. Roles are given from a thread's menu
 * ("Thread role"); here a person removes one. Every write is the person's own settings save.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { useAtomValue } from "@effect/atom-react";
import type { EnvironmentId, ThreadRoles } from "@t3tools/contracts";

import { updateClientSettings, useClientSettings } from "../../../hooks/useSettings";
import { useProjects } from "../../../state/entities";
import { environmentThreadShells } from "../../../state/threads";
import { saveThreadRoles } from "../../fork/calmSidebar/roleMenu";
import { Button } from "../../ui/button";
import { Switch } from "../../ui/switch";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsRow, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { ROLE_PROBLEM_TEXT, type RoleEntry, threadRoleEntries } from "./calmSidebarSettings.logic";

export function CalmSidebarSettings() {
  const searchableSetting = useSearchableSetting();
  const enabled = useClientSettings((settings) => settings.calmSidebarEnabled);
  const { connectedEnvironments } = useSettingsScope();
  const multiple = connectedEnvironments.length > 1;
  return (
    <SettingsSection id="calm-sidebar-section" title="Calm sidebar">
      <SettingsRow
        {...searchableSetting("calm-sidebar")}
        devicePreference
        description="On: the assistant and each project's orchestrator go on top, and every other thread folds into Other threads, with how many wait on you. Off: the full list. This device only. Default: off."
        control={
          <Switch
            checked={enabled}
            onCheckedChange={(checked) =>
              void updateClientSettings({ calmSidebarEnabled: checked })
            }
            aria-label="Calm sidebar"
          />
        }
      />
      <SettingsRow
        {...searchableSetting("thread-roles")}
        description="The assistant and one orchestrator per project. Give a role from a thread's menu (Thread role); remove one here. A role whose thread is missing, archived or moved is marked and holds none."
      >
        <div className="flex flex-col gap-3 pb-2">
          {connectedEnvironments.map((environment) => (
            <EnvironmentRoles
              key={environment.environmentId}
              environmentId={environment.environmentId}
              label={multiple ? environment.label : null}
              roles={environment.serverConfig?.settings.threadRoles}
            />
          ))}
        </div>
      </SettingsRow>
    </SettingsSection>
  );
}

function EnvironmentRoles(props: {
  readonly environmentId: EnvironmentId;
  readonly label: string | null;
  readonly roles: ThreadRoles | undefined;
}) {
  const threads = useAtomValue(
    environmentThreadShells.environmentThreadIndexAtom(props.environmentId),
  );
  const projects = useProjects();
  const entries = threadRoleEntries(props.roles, threads);
  const projectName = (entry: RoleEntry) =>
    projects.find(
      (project) => project.environmentId === props.environmentId && project.id === entry.projectId,
    )?.title ?? entry.projectId;
  return (
    <div
      className="flex flex-col gap-1 text-xs"
      data-testid={`thread-roles-${props.environmentId}`}
    >
      {props.label === null ? null : <p className="font-medium">{props.label}</p>}
      {props.roles === undefined ? (
        <p className="text-muted-foreground">This server does not have thread roles.</p>
      ) : entries.length === 0 ? (
        <p className="text-muted-foreground">No thread has a role.</p>
      ) : (
        <ul className="flex flex-col gap-1">
          {entries.map((entry) => (
            <li
              key={`${entry.role}:${entry.projectId ?? ""}`}
              className="flex items-center justify-between gap-2"
            >
              <span className="min-w-0">
                <span className="text-muted-foreground">
                  {entry.role === "assistant"
                    ? "Assistant"
                    : `Orchestrator of ${projectName(entry)}`}
                  :{" "}
                </span>
                <span className="font-medium">{entry.title ?? entry.threadId}</span>
                {entry.problem === null ? null : (
                  <span className="block text-destructive-foreground">
                    {ROLE_PROBLEM_TEXT[entry.problem]}
                  </span>
                )}
              </span>
              <Button
                size="xs"
                variant="ghost"
                onClick={() =>
                  void saveThreadRoles(
                    props.environmentId,
                    entry.role === "assistant"
                      ? { assistant: null }
                      : { orchestrators: { [entry.projectId as string]: null } },
                  )
                }
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
