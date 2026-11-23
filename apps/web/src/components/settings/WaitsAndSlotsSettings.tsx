/**
 * Fork: settings for agent waits (`waitFor`), named wait checks (`waitChecks`) and agent slots
 * (`agentSlots`). A thin add-on over the shared settings rows: every write is an ordinary
 * settings patch from the person at the screen, under the same permission as project scripts.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import { DEFAULT_SERVER_SETTINGS, type ServerSettingsPatch } from "@t3tools/contracts";
import type {
  AgentSlotsSettingsPatch,
  WaitCheck,
  WaitForSettingsPatch,
} from "@t3tools/contracts/waitFor";
import { PlusIcon } from "lucide-react";
import { useId, useState } from "react";

import { useEnvironmentProviders } from "../../state/environments";
import { EMPTY_SERVER_PROVIDERS } from "../../state/server";
import { Button } from "../ui/button";
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "../ui/dialog";
import { Input } from "../ui/input";
import { Label } from "../ui/label";
import { Switch } from "../ui/switch";
import { Textarea } from "../ui/textarea";
import { AdvancedSettings } from "./AdvancedSettings";
import { IntervalSecondsField } from "./IntervalSecondsField";
import { useSettingsScope } from "./SettingsScopeContext";
import { SettingResetButton, SettingsRow, SettingsSection } from "./settingsLayout";
import { useSearchableSetting } from "./settingsSearch";
import {
  useScopedSettings,
  useScopedSettingsMixed,
  useScopedSettingsWriteAllowed,
  useUpdateScopedSettings,
} from "./useScopedSettings";
import {
  AGENT_SLOTS_NUMBER_FIELDS,
  type NumberFieldSpec,
  WAIT_FOR_NUMBER_FIELDS,
  type WaitCheckDraft,
  type WaitCheckDraftField,
  agentSlotAccountRows,
  agentSlotsDescription,
  agentSlotsSummary,
  emptyWaitCheckDraft,
  parseWaitCheckDraft,
  targetsDiffer,
  waitCheckCommandLine,
  waitCheckToDraft,
  waitForDescription,
  withAccountLimit,
  withWaitCheck,
  withoutWaitCheck,
} from "./waitsAndSlotsSettings.logic";
import { defaultIs } from "./fork/forkDefaultText.logic";

type UpdateSettings = ReturnType<typeof useUpdateScopedSettings>;

const ADVANCED_WAIT_FOR_KEYS = new Set<string>([
  "fileSweepSeconds",
  "unitSweepSeconds",
  "prSweepSeconds",
  "noChecksGraceSeconds",
  "errorGraceSeconds",
  "maxErrorWakesPerSweep",
  "keepEndedHours",
  "stoppedHandOverHours",
]);
const ADVANCED_AGENT_SLOTS_KEYS = new Set<string>(["reserveSeconds", "startGraceSeconds"]);

/** The names upstream's number field reads out, with the unit this panel's fields count in. */
function numberFieldLabels(name: string, unit: string) {
  return {
    decrease: `Decrease ${name}`,
    input: `${name.charAt(0).toUpperCase()}${name.slice(1)} in ${unit}`,
    increase: `Increase ${name}`,
  };
}

function NumberSettingRow({
  field,
  value,
  defaultValue,
  mixed,
  settingKey,
  onCommit,
}: {
  readonly field: NumberFieldSpec<string>;
  readonly value: number;
  readonly defaultValue: number;
  readonly mixed: boolean;
  readonly settingKey: "waitFor" | "agentSlots";
  readonly onCommit: (value: number, options?: { readonly onFailed?: () => void }) => void;
}) {
  return (
    <SettingsRow
      serverScoped
      id={`${settingKey}-${field.key}`}
      title={field.title}
      description={`${field.description} Default: ${defaultValue} ${field.unit}.`}
      settingKeys={[settingKey]}
      mixed={mixed}
      resetAction={
        !mixed && value !== defaultValue ? (
          <SettingResetButton
            ariaLabel={`Reset ${field.title.toLowerCase()} to default`}
            onClick={() => onCommit(defaultValue)}
          />
        ) : null
      }
      control={
        <div className="flex items-center gap-2">
          <IntervalSecondsField
            labels={numberFieldLabels(field.title.toLowerCase(), field.unit)}
            value={value}
            min={field.min}
            step={field.step}
            onCommit={onCommit}
          />
          <span className="text-xs text-muted-foreground">{field.unit}</span>
        </div>
      }
    />
  );
}

function WaitForSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const waitFor = useScopedSettings((settings) => settings.waitFor);
  const { targets } = useSettingsScope();
  const enabledMixed = targetsDiffer(targets, (settings) => settings.waitFor.enabled);
  const rows = (advanced: boolean) =>
    WAIT_FOR_NUMBER_FIELDS.filter(
      (field) => ADVANCED_WAIT_FOR_KEYS.has(field.key) === advanced,
    ).map((field) => (
      <NumberSettingRow
        key={field.key}
        field={field}
        settingKey="waitFor"
        value={waitFor[field.key]}
        defaultValue={DEFAULT_SERVER_SETTINGS.waitFor[field.key]}
        mixed={targetsDiffer(targets, (settings) => settings.waitFor[field.key])}
        onCommit={(value, options) =>
          updateSettings({ waitFor: { [field.key]: value } as WaitForSettingsPatch }, options)
        }
      />
    ));

  return (
    <SettingsSection id="waits" title="Agent waits">
      <SettingsRow
        serverScoped
        {...searchableSetting("wait-for")}
        description={waitForDescription(waitFor)}
        settingKeys={["waitFor"]}
        mixed={enabledMixed}
        resetAction={
          !enabledMixed && waitFor.enabled !== DEFAULT_SERVER_SETTINGS.waitFor.enabled ? (
            <SettingResetButton
              ariaLabel="Reset agent waits to default"
              onClick={() =>
                updateSettings({ waitFor: { enabled: DEFAULT_SERVER_SETTINGS.waitFor.enabled } })
              }
            />
          ) : null
        }
        control={
          <Switch
            mixed={enabledMixed}
            checked={enabledMixed ? false : waitFor.enabled}
            onCheckedChange={(checked) =>
              updateSettings({ waitFor: { enabled: Boolean(checked) } })
            }
            aria-label="Let agents wait for events"
          />
        }
      />
      {rows(false)}
      <AdvancedSettings
        targetIds={WAIT_FOR_NUMBER_FIELDS.filter((field) =>
          ADVANCED_WAIT_FOR_KEYS.has(field.key),
        ).map((field) => `waitFor-${field.key}`)}
      >
        {rows(true)}
      </AdvancedSettings>
    </SettingsSection>
  );
}

const DRAFT_LABELS: Record<WaitCheckDraftField, string> = {
  name: "Name",
  command: "Command",
  maxArgs: "Extra arguments",
  argPattern: "Argument pattern",
  intervalSeconds: "Interval (seconds)",
  timeoutSeconds: "Time limit (seconds)",
  description: "Description",
};

const DRAFT_HINTS: Partial<Record<WaitCheckDraftField, string>> = {
  name: "What an agent passes, for example build-done.",
  command:
    "The program, then one fixed argument per line. No shell: pipes and quotes are not read. Exit code 0 means the event happened. It runs unsandboxed in the project folder with the server's environment, so use a fixed program that does not run files from the repository (no test runners or build scripts).",
  maxArgs: "How many arguments an agent may add after the fixed ones (0 to 20).",
  argPattern:
    "Every added argument must match this regular expression. Keep it simple: a pattern that takes over 2 seconds refuses the argument. The default refuses a leading -, =, @ or +.",
  intervalSeconds: "How often the command runs, at least 5.",
  timeoutSeconds:
    "One run is stopped after this long (1 to 600). If the server crashes, Linux still ends the run about 7 seconds after this; on macOS and Windows it runs until it ends by itself.",
};

function WaitCheckDialog({
  open,
  onOpenChange,
  previousName,
  initialDraft,
  onSave,
}: {
  readonly open: boolean;
  readonly onOpenChange: (open: boolean) => void;
  readonly previousName: string | null;
  readonly initialDraft: WaitCheckDraft;
  /**
   * Returns an error to show, or null once the write is sent. The caller closes the dialog when
   * the write is saved and calls `onFailed` when it is not, so the typed draft stays.
   */
  readonly onSave: (name: string, check: WaitCheck, onFailed: () => void) => string | null;
}) {
  const baseId = useId();
  const [draft, setDraft] = useState(initialDraft);
  const [errors, setErrors] = useState<Partial<Record<WaitCheckDraftField, string>>>({});
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const save = () => {
    if (saving) return;
    const parsed = parseWaitCheckDraft(draft);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      setSaveError(null);
      return;
    }
    setErrors({});
    setSaveError(null);
    setSaving(true);
    const error = onSave(parsed.name, parsed.check, () => {
      setSaving(false);
      setSaveError("The check was not saved. Your draft is kept, so you can try again.");
    });
    if (error !== null) {
      setSaving(false);
      setSaveError(error);
    }
  };

  const field = (key: WaitCheckDraftField, multiline = false) => {
    const id = `${baseId}-${key}`;
    const errorId = `${id}-error`;
    const props = {
      id,
      value: draft[key],
      spellCheck: false,
      "aria-invalid": errors[key] !== undefined,
      "aria-describedby": errors[key] !== undefined ? errorId : undefined,
    };
    return (
      <div className="grid gap-1.5" key={key}>
        <Label htmlFor={id}>{DRAFT_LABELS[key]}</Label>
        {multiline ? (
          <Textarea
            {...props}
            rows={key === "command" ? 4 : 2}
            placeholder={key === "command" ? "test\n-f\n/path/to/done" : undefined}
            onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
          />
        ) : (
          <Input
            {...props}
            size="sm"
            onChange={(event) => setDraft({ ...draft, [key]: event.target.value })}
          />
        )}
        {errors[key] !== undefined ? (
          <p id={errorId} className="text-xs text-destructive-foreground">
            {errors[key]}
          </p>
        ) : DRAFT_HINTS[key] !== undefined ? (
          <p className="text-xs text-muted-foreground">{DRAFT_HINTS[key]}</p>
        ) : null}
      </div>
    );
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-lg">
        <DialogHeader>
          <DialogTitle>{previousName === null ? "Add wait check" : "Edit wait check"}</DialogTitle>
          <DialogDescription>
            A command agents can wait on by name. It runs on this server with your rights, so only
            add commands you trust.
          </DialogDescription>
        </DialogHeader>
        <DialogPanel>
          <form
            className="grid gap-4"
            onSubmit={(event) => {
              event.preventDefault();
              save();
            }}
          >
            {field("name")}
            {field("command", true)}
            <div className="grid gap-4 sm:grid-cols-2">
              {field("maxArgs")}
              {field("argPattern")}
              {field("intervalSeconds")}
              {field("timeoutSeconds")}
            </div>
            {field("description", true)}
            {saveError !== null ? (
              <p role="alert" className="text-xs text-destructive-foreground">
                {saveError}
              </p>
            ) : null}
          </form>
        </DialogPanel>
        <DialogFooter variant="bare">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save check"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

function WaitChecksSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const checks = useScopedSettings((settings) => settings.waitChecks);
  const mixed = useScopedSettingsMixed(["waitChecks"]);
  const canWrite = useScopedSettingsWriteAllowed();
  const [editing, setEditing] = useState<{
    readonly previousName: string | null;
    readonly draft: WaitCheckDraft;
    readonly revision: number;
  } | null>(null);
  const [revision, setRevision] = useState(0);
  const editable = canWrite && !mixed;
  const entries = Object.entries(checks);

  const open = (previousName: string | null, draft: WaitCheckDraft) => {
    setEditing({ previousName, draft, revision });
    setRevision(revision + 1);
  };
  const save =
    (previousName: string | null, dialogRevision: number) =>
    (name: string, check: WaitCheck, onFailed: () => void) => {
      const result = withWaitCheck(checks, previousName, name, check);
      if (!result.ok) return result.error;
      // The server replaces the whole map, so the other checks are sent as they are.
      updateSettings({ waitChecks: result.checks } satisfies ServerSettingsPatch, {
        onFailed,
        // Close only the dialog that sent this write, not one opened since.
        onSaved: () =>
          setEditing((current) => (current?.revision === dialogRevision ? null : current)),
      });
      return null;
    };

  return (
    <SettingsSection id="wait-checks-section" title="Custom wait checks">
      <SettingsRow
        serverScoped
        {...searchableSetting("wait-checks")}
        settingKeys={["waitChecks"]}
        mixed={mixed}
        description={
          mixed
            ? "The selected environments have different checks. Select one environment to edit them."
            : entries.length === 0
              ? "None yet. A custom check is a command agents can wait on by name, for example one that tests whether a build finished. Done when it exits with 0."
              : "Commands agents can wait on by name. A check is done when its command exits with 0."
        }
        control={
          <Button
            size="xs"
            variant="outline"
            disabled={!editable}
            onClick={() => open(null, emptyWaitCheckDraft())}
          >
            <PlusIcon className="size-3" />
            Add check
          </Button>
        }
      />
      {mixed
        ? null
        : entries.map(([name, check]) => (
            <SettingsRow
              key={name}
              id={`wait-check-${name}`}
              // Not serverScoped (the section row carries the scope chip), but an administrator's
              // lock on waitChecks makes Edit and Remove inert like "Add check".
              settingKeys={["waitChecks"]}
              title={<span className="font-mono">{name}</span>}
              description={
                <>
                  <code className="break-all">{waitCheckCommandLine(check)}</code>
                  {check.description.length > 0 ? (
                    <span className="block">{check.description}</span>
                  ) : null}
                  <span className="block">
                    Every {check.intervalSeconds} s, stopped after {check.timeoutSeconds} s.
                  </span>
                </>
              }
              control={
                <div className="flex items-center gap-2">
                  <Button
                    size="xs"
                    variant="outline"
                    disabled={!editable}
                    aria-label={`Edit check ${name}`}
                    onClick={() => open(name, waitCheckToDraft(name, check))}
                  >
                    Edit
                  </Button>
                  <Button
                    size="xs"
                    variant="destructive-outline"
                    disabled={!editable}
                    aria-label={`Remove check ${name}`}
                    onClick={() =>
                      updateSettings({
                        waitChecks: withoutWaitCheck(checks, name),
                      } satisfies ServerSettingsPatch)
                    }
                  >
                    Remove
                  </Button>
                </div>
              }
            />
          ))}
      {editing !== null ? (
        <WaitCheckDialog
          key={editing.revision}
          open
          onOpenChange={(next) => {
            if (!next) setEditing(null);
          }}
          previousName={editing.previousName}
          initialDraft={editing.draft}
          onSave={save(editing.previousName, editing.revision)}
        />
      ) : null}
    </SettingsSection>
  );
}

function AgentSlotsSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const slots = useScopedSettings((settings) => settings.agentSlots);
  const { targets, environment, connectedEnvironments } = useSettingsScope();
  const providersByEnvironment = useEnvironmentProviders();
  // Every selected environment's accounts, the primary's first, so each can be limited here.
  const providers = [
    ...(environment ? [environment] : []),
    ...connectedEnvironments.filter(
      (candidate) => candidate.environmentId !== environment?.environmentId,
    ),
  ].flatMap(
    (candidate) => providersByEnvironment.get(candidate.environmentId) ?? EMPTY_SERVER_PROVIDERS,
  );
  const enabledMixed = targetsDiffer(targets, (settings) => settings.agentSlots.enabled);
  const accountsMixed = targetsDiffer(targets, (settings) => settings.agentSlots.maxPerAccount);
  const patch = (value: AgentSlotsSettingsPatch) => ({ agentSlots: value });
  const rows = (advanced: boolean) =>
    AGENT_SLOTS_NUMBER_FIELDS.filter(
      (field) => ADVANCED_AGENT_SLOTS_KEYS.has(field.key) === advanced,
    ).map((field) => (
      <NumberSettingRow
        key={field.key}
        field={field}
        settingKey="agentSlots"
        value={slots[field.key]}
        defaultValue={DEFAULT_SERVER_SETTINGS.agentSlots[field.key]}
        mixed={targetsDiffer(targets, (settings) => settings.agentSlots[field.key])}
        onCommit={(value, options) =>
          updateSettings(patch({ [field.key]: value } as AgentSlotsSettingsPatch), options)
        }
      />
    ));
  const accounts = agentSlotAccountRows(providers, slots.maxPerAccount);

  return (
    <SettingsSection id="agent-slots-section" title="Limit on running agents">
      <SettingsRow
        serverScoped
        {...searchableSetting("agent-slots")}
        description={agentSlotsDescription(slots)}
        settingKeys={["agentSlots"]}
        mixed={enabledMixed}
        status={enabledMixed ? null : agentSlotsSummary(slots)}
        resetAction={
          !enabledMixed && slots.enabled !== DEFAULT_SERVER_SETTINGS.agentSlots.enabled ? (
            <SettingResetButton
              ariaLabel="Reset agent slots to default"
              onClick={() =>
                updateSettings(patch({ enabled: DEFAULT_SERVER_SETTINGS.agentSlots.enabled }))
              }
            />
          ) : null
        }
        control={
          <Switch
            mixed={enabledMixed}
            checked={enabledMixed ? false : slots.enabled}
            onCheckedChange={(checked) => updateSettings(patch({ enabled: Boolean(checked) }))}
            aria-label="Limit running agents"
          />
        }
      />
      {rows(false)}
      {accounts.map((account) => (
        <SettingsRow
          serverScoped
          key={account.instanceId}
          id={`agent-slots-account-${account.instanceId}`}
          title={`Agents on ${account.label}`}
          description={
            accountsMixed
              ? "The selected environments have different account limits. Select one environment to edit them."
              : account.known
                ? `Most agents running at once on this provider account. 0 means no limit. ${defaultIs("agentSlotsPerAccount")}`
                : "This provider instance is not on this server any more. Set 0 to remove its limit."
          }
          settingKeys={["agentSlots"]}
          mixed={accountsMixed}
          control={
            accountsMixed ? null : (
              <div className="flex items-center gap-2">
                <IntervalSecondsField
                  labels={numberFieldLabels(`agents on ${account.label}`, "agents")}
                  value={account.limit}
                  min={0}
                  step={1}
                  onCommit={(value, options) =>
                    updateSettings(
                      patch({
                        maxPerAccount: withAccountLimit(
                          slots.maxPerAccount,
                          account.instanceId,
                          value,
                        ),
                      }),
                      options,
                    )
                  }
                />
                <span className="text-xs text-muted-foreground">agents</span>
              </div>
            )
          }
        />
      ))}
      <AdvancedSettings
        targetIds={AGENT_SLOTS_NUMBER_FIELDS.filter((field) =>
          ADVANCED_AGENT_SLOTS_KEYS.has(field.key),
        ).map((field) => `agentSlots-${field.key}`)}
      >
        {rows(true)}
      </AdvancedSettings>
    </SettingsSection>
  );
}

/** Agent waits and the server's wait checks; a project screen shows only its wait checks. */
export function WaitsSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const updateSettings = useUpdateScopedSettings();
  return (
    <>
      {isProjectScope ? null : <WaitForSection updateSettings={updateSettings} />}
      <WaitChecksSection updateSettings={updateSettings} />
    </>
  );
}

/** Agent slots are environment-wide, so a project screen does not show them. */
export function AgentSlotsSettings() {
  const updateSettings = useUpdateScopedSettings();
  return <AgentSlotsSection updateSettings={updateSettings} />;
}

/**
 * Waits and agent slots are environment-wide, so a project screen shows only its wait checks,
 * which a project may override as a whole map.
 */
export function WaitsAndSlotsSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  return (
    <>
      <WaitsSettings isProjectScope={isProjectScope} />
      {isProjectScope ? null : <AgentSlotsSettings />}
    </>
  );
}
