/**
 * Fork: MCP profiles. The list and thread picks are the environment's; the default profile and
 * the sub-agent rule may be a project's own. Each write is the person's settings patch naming only
 * the keys it changes; opening the panel writes nothing.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  MCP_PROFILE_ALL,
  MCP_SUBAGENT_INHERIT,
  MCP_T3_TOOL_GROUPS,
  type McpT3ToolGroup,
  type ServerSettingsPatch,
} from "@t3tools/contracts";
import { useState } from "react";

import { useProjects, useThreadShells } from "../../../state/entities";
import { threadPickEffect } from "../../fork/mcpProfile/threadMcpProfile.logic";
import { Button } from "../../ui/button";
import { Checkbox } from "../../ui/checkbox";
import { Input } from "../../ui/input";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../../ui/select";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import {
  useClearScopedSettings,
  useScopedSettingSource,
  useScopedSettings,
  useScopedSettingsMixed,
  useUpdateScopedSettings,
} from "../useScopedSettings";
import { ForkAdvanced, ForkRow, useForkRowLock } from "./ForkRow";
import {
  MCP_ALWAYS_KEPT_NOTE,
  MCP_NOT_A_SANDBOX_NOTE,
  MCP_BLANK_PICK_ISSUE,
  MCP_PICK_APPLIES_NOTE,
  type McpProfileDraft,
  type McpProfileLabels,
  clearThreadPicksPatch,
  deleteMcpProfilePatch,
  describeMcpProfileSet,
  draftOf,
  emptyMcpProfileDraft,
  mcpProfileChoices,
  mcpProfileRows,
  mcpPickProblemSummary,
  mcpServerSuggestions,
  mcpThreadPickRows,
  notSetUpNotes,
  saveMcpProfilePatch,
  shownProfileRef,
  threadPickPatch,
  threadPickProblem,
} from "./mcpProfileSettings.logic";
import { defaultIs } from "./forkDefaultText.logic";

const MIXED =
  "The selected environments or projects have different MCP profile settings. Select one to edit them.";

/**
 * The outside servers set up where the profiles apply. No server list reaches this client yet, so
 * the editor offers the names other profiles use and takes any name.
 */
const KNOWN_SERVERS: ReadonlyArray<string> | null = null;

const PROFILE_KEYS = [
  "mcpProfiles",
  "mcpProfile",
  "mcpSubagentProfile",
  "mcpThreadProfiles",
] as const;

function ProfileSelect({
  value,
  choices,
  label,
  extra,
  onChange,
}: {
  /** null: nothing chosen yet (a new pick, or a blank one); the select then asks for a choice. */
  readonly value: string | null;
  readonly choices: ReadonlyArray<string>;
  readonly label: string;
  /** Values offered before the profiles, such as `inherit`. */
  readonly extra?: ReadonlyArray<string>;
  readonly onChange: (name: string) => void;
}) {
  // A value no readable profile has (a broken or missing one) is shown as it is, never as `all`.
  const options = [...(extra ?? []), ...choices];
  const shown = value === null || options.includes(value) ? options : [...options, value];
  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next !== null && next !== value) onChange(next as string);
      }}
    >
      <SelectTrigger size="sm" className="w-full sm:w-40" aria-label={label}>
        <SelectValue>{value ?? "Choose a profile"}</SelectValue>
      </SelectTrigger>
      <SelectPopup align="end" alignItemWithTrigger={false}>
        {shown.map((name) => (
          <SelectItem hideIndicator key={name} value={name} disabled={!options.includes(name)}>
            {options.includes(name) ? name : `${name} (cannot apply)`}
          </SelectItem>
        ))}
      </SelectPopup>
    </Select>
  );
}

function ListOrAll<T extends string>({
  legend,
  allLabel,
  value,
  options,
  onChange,
}: {
  readonly legend: string;
  readonly allLabel: string;
  readonly value: "all" | ReadonlyArray<T>;
  readonly options: ReadonlyArray<T>;
  readonly onChange: (next: "all" | ReadonlyArray<T>) => void;
}) {
  const list = value === "all" ? [] : value;
  return (
    <fieldset className="grid gap-1.5">
      <legend className="mb-1 text-xs font-medium">{legend}</legend>
      <label className="flex items-center gap-2 text-xs">
        <Checkbox
          checked={value === "all"}
          aria-label={allLabel}
          onCheckedChange={(checked) => onChange(checked === true ? "all" : [])}
        />
        {allLabel}
      </label>
      {value === "all" ? null : (
        <div className="grid gap-1 pl-6 sm:grid-cols-3">
          {options.map((option) => (
            <label key={option} className="flex items-center gap-2 text-xs">
              <Checkbox
                checked={list.includes(option)}
                aria-label={option}
                onCheckedChange={(checked) =>
                  onChange(
                    checked === true ? [...list, option] : list.filter((entry) => entry !== option),
                  )
                }
              />
              {option}
            </label>
          ))}
        </div>
      )}
    </fieldset>
  );
}

function ProfileEditor({
  editing,
  initial,
  suggestions,
  onSave,
  onCancel,
}: {
  readonly editing: string | null;
  readonly initial: McpProfileDraft;
  readonly suggestions: ReadonlyArray<string>;
  readonly onSave: (draft: McpProfileDraft) => string | null;
  readonly onCancel: () => void;
}) {
  const [draft, setDraft] = useState(initial);
  const [serverName, setServerName] = useState("");
  const [problem, setProblem] = useState<string | null>(null);
  const servers = draft.servers === "all" ? [] : draft.servers;
  const serverOptions = [...new Set([...suggestions, ...servers])];
  const addServer = () => {
    const name = serverName.trim();
    if (name === "") return;
    setDraft({ ...draft, servers: [...new Set([...servers, name])] });
    setServerName("");
  };
  const notes = notSetUpNotes(draft.name.trim() || "this profile", draft.servers, KNOWN_SERVERS);
  return (
    <div className="grid gap-3 rounded-lg border border-border/60 p-3">
      {editing === null ? (
        <Input
          size="sm"
          aria-label="Profile name"
          placeholder="Profile name, such as lean"
          value={draft.name}
          onChange={(event) => {
            setProblem(null);
            setDraft({ ...draft, name: event.target.value });
          }}
        />
      ) : (
        <p className="text-xs font-medium">{editing}</p>
      )}
      <ListOrAll
        legend="Outside tool servers (MCP)"
        allLabel="All servers"
        value={draft.servers}
        options={serverOptions}
        onChange={(next) => setDraft({ ...draft, servers: next })}
      />
      {draft.servers === "all" ? null : (
        <div className="flex flex-wrap items-center gap-2 pl-6">
          <Input
            size="sm"
            className="min-w-0 flex-1"
            aria-label="Server name"
            placeholder="Server name, as Claude lists it"
            value={serverName}
            onChange={(event) => setServerName(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") addServer();
            }}
          />
          <Button size="xs" variant="outline" onClick={addServer}>
            Add server
          </Button>
        </div>
      )}
      <ListOrAll<McpT3ToolGroup>
        legend="T3 tool groups"
        allLabel="All T3 tools"
        value={draft.t3Tools}
        options={MCP_T3_TOOL_GROUPS}
        onChange={(next) => setDraft({ ...draft, t3Tools: next })}
      />
      <p className="text-xs text-muted-foreground">{MCP_ALWAYS_KEPT_NOTE}</p>
      <p className="text-xs text-muted-foreground">{MCP_NOT_A_SANDBOX_NOTE}</p>
      <label className="flex items-center gap-2 text-xs">
        <Checkbox
          checked={draft.noBuiltinTools === true}
          aria-label="No built-in tools"
          onCheckedChange={(checked) => setDraft({ ...draft, noBuiltinTools: checked === true })}
        />
        No built-in tools (no shell, files or web; MCP tools only). Claude only: a thread on another
        provider is refused.
      </label>
      {notes.map((note) => (
        <p key={note} className="text-xs text-muted-foreground">
          {note}
        </p>
      ))}
      {problem !== null ? (
        <p role="alert" className="text-xs text-destructive-foreground">
          {problem}
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button size="xs" onClick={() => setProblem(onSave(draft))}>
          Save profile
        </Button>
        <Button size="xs" variant="outline" onClick={onCancel}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

export function McpProfileSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const clearScopedSettings = useClearScopedSettings();
  const mixed = useScopedSettingsMixed([...PROFILE_KEYS]);
  const defaultSource = useScopedSettingSource(["mcpProfile"]);
  const ruleSource = useScopedSettingSource(["mcpSubagentProfile"]);
  const mcpProfiles = useScopedSettings((settings) => settings.mcpProfiles);
  const mcpProfile = useScopedSettings((settings) => settings.mcpProfile);
  const mcpSubagentProfile = useScopedSettings((settings) => settings.mcpSubagentProfile);
  const mcpThreadProfiles = useScopedSettings((settings) => settings.mcpThreadProfiles);
  const mcpCarriedProfiles = useScopedSettings((settings) => settings.mcpCarriedProfiles);
  const projectSettingsOverrides = useScopedSettings(
    (settings) => settings.projectSettingsOverrides,
  );
  // The assistant thread never runs wider than `assistant`, so a pick's effect depends on it too.
  const threadRoles = useScopedSettings((settings) => settings.threadRoles);
  const threadShells = useThreadShells();
  const projects = useProjects();
  const { environment } = useSettingsScope();
  const picksLock = useForkRowLock(["mcpThreadProfiles"]);
  const [editor, setEditor] = useState<{
    readonly editing: string | null;
    readonly draft: McpProfileDraft;
  } | null>(null);
  const [deleteProblem, setDeleteProblem] = useState<{
    readonly problem: string;
    readonly threadIds: ReadonlyArray<string>;
  } | null>(null);
  // The "Add thread" form: closed (null), or the id typed and the profile chosen (none at first).
  const [pickDraft, setPickDraft] = useState<{
    readonly threadId: string;
    readonly profile: string | null;
  } | null>(null);
  const [pickError, setPickError] = useState<string | null>(null);

  const settings = { mcpProfiles, mcpProfile, mcpSubagentProfile, mcpThreadProfiles };
  const labels: McpProfileLabels = {
    projects: new Map(projects.map((project) => [project.id as string, project.title])),
    threads: new Map(threadShells.map((thread) => [thread.id as string, thread.title])),
  };
  const rows = mcpProfileRows(settings, KNOWN_SERVERS);
  const choices = mcpProfileChoices(settings);
  const save = (patch: ServerSettingsPatch | null, options?: { onSaved?: () => void }) => {
    if (patch === null) options?.onSaved?.();
    else updateSettings(patch, options);
  };

  if (mixed) {
    return (
      <SettingsSection id="mcp-profiles-section" title="Tool profiles">
        <ForkRow
          serverScoped
          {...searchableSetting("mcp-profiles")}
          lockPaths={[...PROFILE_KEYS]}
          mixed
          description={MIXED}
        />
      </SettingsSection>
    );
  }

  const defaultRow = (
    <ForkRow
      serverScoped
      {...searchableSetting("mcp-profile-default")}
      settingKeys={["mcpProfile"]}
      lockPaths={["mcpProfile"]}
      onResetOverride={() => clearScopedSettings(["mcpProfile"])}
      description={`The profile a thread gets when nobody picked one for it. ${MCP_PROFILE_ALL}: every outside server and every T3 tool, as in upstream T3 Code. ${defaultIs("mcpProfile")}${
        isProjectScope && defaultSource === "project" ? " This project's own value." : ""
      } ${MCP_PICK_APPLIES_NOTE}`}
      control={
        <ProfileSelect
          value={shownProfileRef(mcpProfile, MCP_PROFILE_ALL)}
          choices={choices}
          label="Default MCP profile"
          onChange={(name) => save({ mcpProfile: name })}
        />
      }
    />
  );
  const ruleRow = (
    <ForkRow
      serverScoped
      {...searchableSetting("mcp-subagent-profile")}
      settingKeys={["mcpSubagentProfile"]}
      lockPaths={["mcpSubagentProfile"]}
      onResetOverride={() => clearScopedSettings(["mcpSubagentProfile"])}
      description={`The profile a sub-agent gets when nobody picked one for it. ${MCP_SUBAGENT_INHERIT}: the same as any other thread. A sub-agent never gets more than its parent. ${defaultIs("mcpSubagentProfile")}${
        isProjectScope && ruleSource === "project" ? " This project's own value." : ""
      }`}
      control={
        <ProfileSelect
          value={shownProfileRef(mcpSubagentProfile, MCP_SUBAGENT_INHERIT)}
          choices={choices}
          extra={[MCP_SUBAGENT_INHERIT]}
          label="Sub-agent MCP profile"
          onChange={(name) => save({ mcpSubagentProfile: name })}
        />
      }
    />
  );

  if (isProjectScope) {
    return (
      <SettingsSection id="mcp-profiles-section" title="Tool profiles">
        {defaultRow}
        {ruleRow}
      </SettingsSection>
    );
  }

  const pickRows = mcpThreadPickRows(mcpThreadProfiles, labels.threads);
  // The threads of the environment these settings are for: a pick's effect depends on its parent.
  const shells = threadShells.filter((shell) => shell.environmentId === environment?.environmentId);
  const threadSettings = { ...settings, mcpCarriedProfiles, projectSettingsOverrides, threadRoles };
  const pickEffect = (threadId: string, name: string) =>
    threadPickEffect(threadSettings, threadId, name, shells);
  const blankPicks = pickRows.filter((row) => row.pick === null).length;
  const widerPicks = pickRows.filter(
    (row) => row.pick !== null && pickEffect(row.threadId, row.pick).widerThanParent,
  ).length;
  const pickProblem = mcpPickProblemSummary(blankPicks, widerPicks);
  const draftEffect =
    pickDraft !== null && pickDraft.threadId.trim() !== "" && pickDraft.profile !== null
      ? pickEffect(pickDraft.threadId.trim(), pickDraft.profile)
      : null;
  // One save, only once a profile is chosen: that thread's key with that profile.
  const addPick = () => {
    if (pickDraft === null) return;
    const problem = threadPickProblem(settings, pickDraft.threadId, pickDraft.profile);
    if (problem !== null || pickDraft.profile === null) {
      setPickError(problem);
      return;
    }
    setPickError(null);
    save(threadPickPatch(mcpThreadProfiles, pickDraft.threadId.trim(), pickDraft.profile), {
      onSaved: () => setPickDraft(null),
    });
  };

  return (
    <SettingsSection id="mcp-profiles-section" title="Tool profiles">
      <ForkRow
        {...searchableSetting("mcp-profiles")}
        settingKeys={["mcpProfiles"]}
        lockPaths={["mcpProfiles"]}
        description="A profile is a named set of outside tool servers (MCP) and T3 tool groups. Give a thread a smaller profile so it sees only the tools it needs. Claude and Codex threads apply a smaller profile; other providers refuse a turn whose profile leaves out an outside server. Built in: all and t3-only."
      >
        <div className="grid gap-2 pb-2">
          {rows.map((row) => (
            <div key={`${row.kind}:${row.name}`} className="grid gap-1 text-xs">
              <div className="flex flex-wrap items-center gap-2">
                <span className="font-medium">{row.name}</span>
                <span className="text-muted-foreground">
                  {row.kind === "built-in" ? "built in" : null}
                  {row.state === "broken" ? " broken" : null}
                  {row.state === "foreign" ? " from a newer version" : null}
                </span>
                <span className="min-w-0 flex-1 truncate text-muted-foreground">
                  {row.state === "ok" ? describeMcpProfileSet(row.profile) : null}
                </span>
                {row.kind === "custom" && row.state === "ok" ? (
                  <Button
                    size="xs"
                    variant="outline"
                    aria-label={`Edit MCP profile ${row.name}`}
                    onClick={() => {
                      setDeleteProblem(null);
                      setEditor({ editing: row.name, draft: draftOf(row.name, row.profile) });
                    }}
                  >
                    Edit
                  </Button>
                ) : null}
                {(row.state === "ok" ? row.kind === "custom" : row.removable) ? (
                  <Button
                    size="xs"
                    variant="destructive-outline"
                    aria-label={`Delete MCP profile ${row.name}`}
                    onClick={() => {
                      const result = deleteMcpProfilePatch(
                        { ...settings, projectSettingsOverrides },
                        row.name,
                        labels,
                      );
                      if ("problem" in result) {
                        setDeleteProblem(result);
                        return;
                      }
                      setDeleteProblem(null);
                      save(result.patch);
                    }}
                  >
                    {row.kind === "built-in" ? "Remove the entry" : "Delete"}
                  </Button>
                ) : null}
              </div>
              {row.state === "ok" ? (
                row.notes.map((note) => (
                  <p key={note} className="text-muted-foreground">
                    {note}
                  </p>
                ))
              ) : (
                <p className="text-destructive-foreground">{row.issue}</p>
              )}
            </div>
          ))}
          {deleteProblem !== null ? (
            <div role="alert" className="grid gap-1 text-xs text-destructive-foreground">
              <p>{deleteProblem.problem}</p>
              {deleteProblem.threadIds.length > 0 ? (
                <div className="grid gap-1">
                  <Button
                    size="xs"
                    variant="outline"
                    className="justify-self-start"
                    // It writes the thread picks, so it follows their lock, not this row's.
                    disabled={picksLock !== undefined}
                    onClick={() =>
                      save(clearThreadPicksPatch(deleteProblem.threadIds), {
                        onSaved: () => setDeleteProblem(null),
                      })
                    }
                  >
                    Clear the thread picks that use it
                  </Button>
                  {picksLock !== undefined ? (
                    <p className="text-muted-foreground">Thread picks: {picksLock}</p>
                  ) : null}
                </div>
              ) : null}
            </div>
          ) : null}
          {editor === null ? (
            <div>
              <Button
                size="xs"
                variant="outline"
                onClick={() => {
                  setDeleteProblem(null);
                  setEditor({ editing: null, draft: emptyMcpProfileDraft() });
                }}
              >
                Add profile
              </Button>
            </div>
          ) : (
            <ProfileEditor
              key={editor.editing ?? ""}
              editing={editor.editing}
              initial={editor.draft}
              suggestions={mcpServerSuggestions(settings, KNOWN_SERVERS)}
              onCancel={() => setEditor(null)}
              onSave={(draft) => {
                const result = saveMcpProfilePatch(settings, editor.editing, draft);
                if ("problem" in result) return result.problem;
                save(result.patch, { onSaved: () => setEditor(null) });
                return null;
              }}
            />
          )}
        </div>
      </ForkRow>
      {defaultRow}
      {ruleRow}
      <ForkAdvanced targetIds={["mcp-thread-picks"]} problem={pickProblem}>
        <ForkRow
          {...searchableSetting("mcp-thread-picks")}
          settingKeys={["mcpThreadProfiles"]}
          lockPaths={["mcpThreadProfiles"]}
          description={`A thread's own pick wins over every default and is never cut down to its parent's, also for a sub-agent. Pick one from the thread's menu, or here by thread id. ${defaultIs("mcpThreadProfiles")} ${MCP_PICK_APPLIES_NOTE}`}
        >
          <div className="grid gap-2 pb-2">
            {pickRows.map((row) => {
              const effect = row.pick === null ? null : pickEffect(row.threadId, row.pick);
              return (
                <div key={row.threadId} className="grid gap-1 text-xs">
                  <div className="flex flex-wrap items-center gap-2">
                    <span className="flex min-w-0 flex-1 items-baseline gap-1.5">
                      <span className={row.title ? "truncate" : "truncate text-muted-foreground"}>
                        {row.title ?? "Unknown thread"}
                      </span>
                      <span className="shrink-0 text-muted-foreground">{row.threadId}</span>
                    </span>
                    <ProfileSelect
                      value={row.pick}
                      choices={choices}
                      label={`MCP profile for thread ${row.threadId}`}
                      onChange={(name) =>
                        save(threadPickPatch(mcpThreadProfiles, row.threadId, name))
                      }
                    />
                    <Button
                      size="xs"
                      variant="destructive-outline"
                      aria-label={`Clear the MCP profile pick of thread ${row.threadId}`}
                      onClick={() => save(threadPickPatch(mcpThreadProfiles, row.threadId, null))}
                    >
                      Clear
                    </Button>
                  </div>
                  {row.pick === null ? (
                    <p className="text-destructive-foreground">{MCP_BLANK_PICK_ISSUE}</p>
                  ) : effect?.widerThanParent ? (
                    <p className="text-warning-foreground">{effect.text}</p>
                  ) : null}
                </div>
              );
            })}
            {pickDraft === null ? (
              <div>
                <Button
                  size="xs"
                  variant="outline"
                  onClick={() => {
                    setPickError(null);
                    setPickDraft({ threadId: "", profile: null });
                  }}
                >
                  Add thread
                </Button>
              </div>
            ) : (
              <div className="grid gap-2 rounded-lg border border-border/60 p-3 text-xs">
                <div className="flex flex-wrap items-center gap-2">
                  <Input
                    size="sm"
                    className="min-w-0 flex-1"
                    aria-label="Thread id for an MCP profile pick"
                    placeholder="Thread id"
                    value={pickDraft.threadId}
                    onChange={(event) => {
                      setPickError(null);
                      setPickDraft({ ...pickDraft, threadId: event.target.value });
                    }}
                    onKeyDown={(event) => {
                      if (event.key === "Enter") addPick();
                    }}
                  />
                  <ProfileSelect
                    value={pickDraft.profile}
                    choices={choices}
                    label="MCP profile for the new thread pick"
                    onChange={(name) => {
                      setPickError(null);
                      setPickDraft({ ...pickDraft, profile: name });
                    }}
                  />
                </div>
                {draftEffect !== null ? (
                  <p
                    className={
                      draftEffect.widerThanParent
                        ? "text-warning-foreground"
                        : "text-muted-foreground"
                    }
                  >
                    {draftEffect.text}
                  </p>
                ) : null}
                {pickError !== null ? (
                  <p role="alert" className="text-destructive-foreground">
                    {pickError}
                  </p>
                ) : null}
                <div className="flex gap-2">
                  <Button size="xs" onClick={addPick}>
                    Save pick
                  </Button>
                  <Button
                    size="xs"
                    variant="outline"
                    onClick={() => {
                      setPickError(null);
                      setPickDraft(null);
                    }}
                  >
                    Cancel
                  </Button>
                </div>
              </div>
            )}
          </div>
        </ForkRow>
      </ForkAdvanced>
    </SettingsSection>
  );
}
