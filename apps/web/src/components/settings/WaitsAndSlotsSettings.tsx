/**
 * Fork: settings for agent waits (`waitFor`), named wait checks (`waitChecks`) and agent slots
 * (`agentSlots`). A thin add-on over the shared settings rows: every write is an ordinary
 * settings patch from the person at the screen, under the same permission as project scripts.
 */
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
import { searchableSetting } from "./settingsSearch";
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

type UpdateSettings = ReturnType<typeof useUpdateScopedSettings>;

const ADVANCED_WAIT_FOR_KEYS = new Set<string>([
  "fileSweepSeconds",
  "unitSweepSeconds",
  "prSweepSeconds",
  "noChecksGraceSeconds",
  "errorGraceSeconds",
  "keepEndedHours",
  "stoppedHandOverHours",
]);
const ADVANCED_AGENT_SLOTS_KEYS = new Set<string>(["reserveSeconds", "startGraceSeconds"]);

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
      description={field.description}
      settingKeys={[settingKey]}
      mixed={mixed}
      resetAction={
        !mixed && value !== defaultValue ? (
          <SettingResetButton
            label={field.title.toLowerCase()}
            onClick={() => onCommit(defaultValue)}
          />
        ) : null
      }
      control={
        <div className="flex items-center gap-2">
          <IntervalSecondsField
            label={field.title.toLowerCase()}
            value={value}
            min={field.min}
            step={field.step}
            unit={field.unit}
            onCommit={onCommit}
          />
          <span className="text-xs text-muted-foreground">{field.unit}</span>
        </div>
      }
    />
  );
}

function WaitForSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
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
    <SettingsSection id="waits" title="Waits">
      <SettingsRow
        serverScoped
        {...searchableSetting("wait-for")}
        description={waitForDescription(waitFor)}
        settingKeys={["waitFor"]}
        mixed={enabledMixed}
        resetAction={
          !enabledMixed && waitFor.enabled !== DEFAULT_SERVER_SETTINGS.waitFor.enabled ? (
            <SettingResetButton
              label="agent waits"
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
    "The program, then one fixed argument per line. No shell: pipes and quotes are not read. Exit code 0 means the event happened.",
  maxArgs: "How many arguments an agent may add after the fixed ones (0 to 20).",
  argPattern: "Every added argument must match this regular expression.",
  intervalSeconds: "How often the command runs, at least 5.",
  timeoutSeconds: "One run is stopped after this long (1 to 600).",
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
  readonly onSave: (name: string, check: WaitCheck) => string | null;
}) {
  const baseId = useId();
  const [draft, setDraft] = useState(initialDraft);
  const [errors, setErrors] = useState<Partial<Record<WaitCheckDraftField, string>>>({});
  const [saveError, setSaveError] = useState<string | null>(null);

  const save = () => {
    const parsed = parseWaitCheckDraft(draft);
    if (!parsed.ok) {
      setErrors(parsed.errors);
      setSaveError(null);
      return;
    }
    setErrors({});
    const error = onSave(parsed.name, parsed.check);
    if (error !== null) {
      setSaveError(error);
      return;
    }
    onOpenChange(false);
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
          <Button onClick={save}>Save check</Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

function WaitChecksSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
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
  const save = (previousName: string | null) => (name: string, check: WaitCheck) => {
    const result = withWaitCheck(checks, previousName, name, check);
    if (!result.ok) return result.error;
    // The server replaces the whole map, so the other checks are sent as they are.
    updateSettings({ waitChecks: result.checks } satisfies ServerSettingsPatch);
    return null;
  };

  return (
    <SettingsSection id="wait-checks-section" title="Wait checks">
      <SettingsRow
        serverScoped
        {...searchableSetting("wait-checks")}
        settingKeys={["waitChecks"]}
        mixed={mixed}
        description={
          mixed
            ? "The selected environments have different checks. Select one environment to edit them."
            : entries.length === 0
              ? "No checks yet. A check is a command agents can wait on by name, for example a build that writes a file when done."
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
          onSave={save(editing.previousName)}
        />
      ) : null}
    </SettingsSection>
  );
}

function AgentSlotsSection({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const slots = useScopedSettings((settings) => settings.agentSlots);
  const { targets, environment } = useSettingsScope();
  const providersByEnvironment = useEnvironmentProviders();
  const providers =
    (environment ? providersByEnvironment.get(environment.environmentId) : undefined) ??
    EMPTY_SERVER_PROVIDERS;
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
    <SettingsSection id="agent-slots-section" title="Agent slots">
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
              label="agent slots"
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
                ? "Most agents running at once on this provider account. 0 means no limit."
                : "This provider instance is not on this server any more. Set 0 to remove its limit."
          }
          settingKeys={["agentSlots"]}
          mixed={accountsMixed}
          control={
            accountsMixed ? null : (
              <div className="flex items-center gap-2">
                <IntervalSecondsField
                  label={`agents on ${account.label}`}
                  value={account.limit}
                  min={0}
                  step={1}
                  unit="agents"
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

/**
 * Waits and agent slots are environment-wide, so a project screen shows only its wait checks,
 * which a project may override as a whole map.
 */
export function WaitsAndSlotsSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const updateSettings = useUpdateScopedSettings();
  return (
    <>
      {isProjectScope ? null : <WaitForSection updateSettings={updateSettings} />}
      <WaitChecksSection updateSettings={updateSettings} />
      {isProjectScope ? null : <AgentSlotsSection updateSettings={updateSettings} />}
    </>
  );
}
