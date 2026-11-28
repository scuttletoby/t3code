/**
 * Fork: the model policy (`modelPolicy`), which models agents may pick. Server-wide (a project cannot
 * override it), so a project screen does not show it. A save sends the whole value, `mode` and
 * `allow`, as the server requires. Every write is an ordinary settings patch from the person at the
 * screen; agents cannot change it.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_MODEL_POLICY,
  type ModelPolicy,
  type ModelPolicyMode,
  type ModelPolicyPatch,
} from "@t3tools/contracts";
import { useState } from "react";

import { Button } from "../../ui/button";
import {
  Dialog,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogPanel,
  DialogPopup,
  DialogTitle,
} from "../../ui/dialog";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../../ui/select";
import { Textarea } from "../../ui/textarea";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useScopedSettings, useUpdateScopedSettings } from "../useScopedSettings";
import { targetsDiffer } from "../waitsAndSlotsSettings.logic";
import { ForkRow, OptionLines, useSelectedProvidersByEnvironment } from "./ForkRow";
import {
  aliasModelPolicyRows,
  MODEL_POLICY_MODE_OPTIONS,
  modelPolicyEditorText,
  modelPolicyListedRows,
  type ModelPolicyListProvider,
  modelPolicyRowLine,
  modelPolicyStartsFromStartRows,
  modelPolicyTextRows,
  personModelPickMarks,
  staleModelPolicyRows,
  unknownModelPolicyDrivers,
  unreadableModelPolicyMode,
  unreadableModelPolicyText,
  withAddedModelPolicyRow,
  withModelPolicyAllow,
} from "./modelPolicySettings.logic";
import { defaultIs } from "./forkDefaultText.logic";

const MIXED = "The selected environments have different values. Select one environment to edit it.";

/** The warning for a settings-file value that cannot be read, with the file's own text. */
function UnreadableWarning({
  text,
  children,
}: {
  readonly text: string;
  readonly children: string;
}) {
  return (
    <div className="text-xs text-destructive-foreground">
      The settings file&apos;s model policy cannot be read, so it reads as Enforce with nothing
      allowed: every agent pick is refused (when the file&apos;s text says advise, agents are only
      warned). It is kept as written until you save here. {children}
      <code className="mt-1 block break-all text-muted-foreground">{text}</code>
    </div>
  );
}

function AllowDialog({
  value,
  providers,
  onOpenChange,
  onSave,
}: {
  readonly value: ModelPolicy;
  /** The selected machines' providers, whose model lists the person adds rows from. */
  readonly providers: ReadonlyArray<ModelPolicyListProvider>;
  readonly onOpenChange: (open: boolean) => void;
  readonly onSave: (next: ModelPolicyPatch, onSaved: () => void, onFailed: () => void) => void;
}) {
  const unreadableText = unreadableModelPolicyText(value);
  const [text, setText] = useState(() => modelPolicyEditorText(value));
  // An unreadable value has no mode to keep: its `enforce` is only the fail-closed reading, so the
  // person picks the mode this save writes, the file's own one offered first when it names one.
  const [mode, setMode] = useState<ModelPolicyMode | null>(
    unreadableText === null ? value.mode : null,
  );
  const fileMode = unreadableModelPolicyMode(value);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const listed = modelPolicyListedRows(modelPolicyTextRows(text), providers);
  const addListed = (key: string | null) => {
    const entry = listed.find((candidate) => candidate.key === key);
    if (entry === undefined) return;
    const result = withAddedModelPolicyRow(text, entry.row);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setError(null);
    setText(result.text);
  };
  const save = () => {
    if (saving) return;
    if (mode === null) {
      setError("Choose the mode this save writes.");
      return;
    }
    const result = withModelPolicyAllow(mode, text);
    if ("error" in result) {
      setError(result.error);
      return;
    }
    setError(null);
    setSaving(true);
    onSave(
      result.value,
      () => onOpenChange(false),
      () => {
        setSaving(false);
        setError("The server refused the rows; your text is kept to fix and save again.");
      },
    );
  };
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit allowed models</DialogTitle>
          <DialogDescription>
            A JSON list, as in the settings file. Each row names a driver (such as claudeAgent or
            codex) and a model; efforts limits its efforts (a row without it checks only the model),
            defaultEffort is one of them, and useFor is a note shown to agents. Add a model appends
            a row for a model the selected machines list, with the efforts its own control offers.
          </DialogDescription>
        </DialogHeader>
        <DialogPanel>
          <div className="grid gap-3">
            {modelPolicyStartsFromStartRows(value) ? (
              <p className="text-xs text-muted-foreground">
                No rows are saved, so this starts from the fork&apos;s start rows (Opus 5.5 and
                Fable 5.2). Save them as they are, or change them first.
              </p>
            ) : null}
            {unreadableText !== null ? (
              <>
                <UnreadableWarning text={unreadableText}>
                  Saving here replaces it with the rows below, which start from the file&apos;s own
                  rows when it has a list of them, and the mode you choose.
                </UnreadableWarning>
                <Select
                  value={mode}
                  onValueChange={(next) => {
                    if (next === null) return;
                    setError(null);
                    setMode(next as ModelPolicyMode);
                  }}
                >
                  <SelectTrigger size="sm" className="w-full sm:w-64" aria-label="Mode to save">
                    <SelectValue>
                      {mode === null
                        ? "Choose a mode"
                        : MODEL_POLICY_MODE_OPTIONS.find((candidate) => candidate.value === mode)
                            ?.label}
                    </SelectValue>
                  </SelectTrigger>
                  <SelectPopup alignItemWithTrigger={false}>
                    {MODEL_POLICY_MODE_OPTIONS.map((candidate) => (
                      <SelectItem hideIndicator key={candidate.value} value={candidate.value}>
                        {candidate.label}
                        {candidate.value === fileMode ? " (as the file says)" : ""}
                      </SelectItem>
                    ))}
                  </SelectPopup>
                </Select>
              </>
            ) : null}
            {listed.length > 0 ? (
              <Select value={null} onValueChange={addListed}>
                <SelectTrigger size="sm" className="w-full sm:w-96" aria-label="Add a listed model">
                  <SelectValue>Add a model from the selected machines&apos; lists</SelectValue>
                </SelectTrigger>
                <SelectPopup alignItemWithTrigger={false}>
                  {listed.map((entry) => (
                    <SelectItem hideIndicator key={entry.key} value={entry.key}>
                      {entry.label}
                      {entry.row.efforts === undefined
                        ? " (no effort control)"
                        : ` (efforts ${entry.row.efforts.join(", ")})`}
                    </SelectItem>
                  ))}
                </SelectPopup>
              </Select>
            ) : null}
            <Textarea
              aria-label="Allowed models"
              className="min-h-72"
              value={text}
              onChange={(event) => {
                setError(null);
                setText(event.target.value);
              }}
            />
            {error !== null ? (
              <p role="alert" className="text-xs text-destructive-foreground">
                {error}
              </p>
            ) : null}
          </div>
        </DialogPanel>
        <DialogFooter variant="bare">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={saving} onClick={save}>
            {saving
              ? "Saving…"
              : unreadableText === null
                ? "Save rows"
                : "Replace the unreadable value"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

export function ModelPolicySettings() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const shown = useScopedSettings((settings) => settings.modelPolicy);
  const { targets, target } = useSettingsScope();
  const mixed = targetsDiffer(targets, (settings) => settings.modelPolicy);
  const [editing, setEditing] = useState(false);
  const unreadableText = mixed ? null : unreadableModelPolicyText(shown);
  const option = MODEL_POLICY_MODE_OPTIONS.find((candidate) => candidate.value === shown.mode);
  const providers = useSelectedProvidersByEnvironment();
  // One read per pick, each a stored value: a selector building a new object would never settle.
  const picks = {
    defaultModelSelection: useScopedSettings((settings) => settings.defaultModelSelection),
    fallbackModelSelection: useScopedSettings((settings) => settings.fallbackModelSelection),
    textGenerationModelSelection: useScopedSettings(
      (settings) => settings.textGenerationModelSelection,
    ),
    sourceControlWriterModelSelection: useScopedSettings(
      (settings) => settings.sourceControlWriterModelSelection,
    ),
    reviewGuideModelSelection: useScopedSettings((settings) => settings.reviewGuideModelSelection),
    speechPostProcessingModelSelection: useScopedSettings(
      (settings) => settings.speechPostProcessingModelSelection,
    ),
  };

  const save = (
    next: ModelPolicyPatch,
    options?: { onSaved?: () => void; onFailed?: () => void },
  ) => updateSettings({ modelPolicy: next }, options);
  const isDefault =
    unreadableText === null && shown.mode === DEFAULT_MODEL_POLICY.mode && shown.allow.length === 0;

  if (mixed) {
    return (
      <SettingsSection id="model-policy-section" title="Models agents may use">
        <ForkRow
          serverScoped
          {...searchableSetting("model-policy")}
          settingKeys={["modelPolicy"]}
          lockPaths={["modelPolicy"]}
          mixed
          description={MIXED}
        />
      </SettingsSection>
    );
  }

  const unknownDrivers = new Set(
    unknownModelPolicyDrivers(shown.allow, new Set(providers.map((provider) => provider.driver))),
  );
  const aliasRows = new Map(
    aliasModelPolicyRows(shown.allow, providers).map(({ row, aliasOf }) => [row, aliasOf]),
  );
  const staleRows = new Set(staleModelPolicyRows(shown.allow, providers));
  const marks = personModelPickMarks(shown, picks, providers, target?.environmentId);
  const byDriver = new Map<string, Array<ModelPolicy["allow"][number]>>();
  for (const row of shown.allow)
    byDriver.set(row.driver, [...(byDriver.get(row.driver) ?? []), row]);
  return (
    <SettingsSection id="model-policy-section" title="Models agents may use">
      <ForkRow
        serverScoped
        {...searchableSetting("model-policy")}
        settingKeys={["modelPolicy"]}
        lockPaths={["modelPolicy"]}
        description={
          <OptionLines
            lead="Which models and efforts agents may pick when they start threads or sub-agents."
            options={MODEL_POLICY_MODE_OPTIONS}
            selected={shown.mode}
            tail={`Your own picks are never refused; one outside the list is marked below. While it is on, a sub-agent that names its model keeps only the options it names. ${defaultIs("modelPolicy")}`}
          />
        }
        resetAction={
          isDefault ? null : (
            <SettingResetButton
              ariaLabel="Reset model policy to default"
              onClick={() => save({ mode: DEFAULT_MODEL_POLICY.mode, allow: [] })}
            />
          )
        }
        control={
          <Select
            value={shown.mode}
            onValueChange={(next) => {
              if (next === null || next === shown.mode) return;
              save({ mode: next as ModelPolicyMode, allow: shown.allow });
            }}
          >
            <SelectTrigger
              size="sm"
              className="w-full sm:w-36"
              aria-label="Models agents may use: mode"
            >
              <SelectValue>{option?.label ?? shown.mode}</SelectValue>
            </SelectTrigger>
            <SelectPopup align="end" alignItemWithTrigger={false}>
              {MODEL_POLICY_MODE_OPTIONS.map((candidate) => (
                <SelectItem hideIndicator key={candidate.value} value={candidate.value}>
                  {candidate.label}
                </SelectItem>
              ))}
            </SelectPopup>
          </Select>
        }
      >
        {unreadableText !== null ? (
          <div role="alert" className="pb-2">
            <UnreadableWarning text={unreadableText}>
              Changing the mode, resetting, or saving the allowed models replaces it.
            </UnreadableWarning>
          </div>
        ) : null}
      </ForkRow>
      <ForkRow
        {...searchableSetting("model-policy-allow")}
        lockPaths={["modelPolicy"]}
        description={
          shown.allow.length === 0
            ? "No model is allowed yet. With Enforce, every agent pick is refused. Edit allowed models starts from the fork's start rows."
            : "The models agents may pick, per provider driver."
        }
        control={
          <Button size="xs" variant="outline" onClick={() => setEditing(true)}>
            Edit allowed models
          </Button>
        }
      >
        {byDriver.size > 0 ? (
          <dl className="grid gap-1 pb-2 text-xs">
            {[...byDriver].map(([driver, rows]) => (
              <div key={driver} className="grid gap-0.5 sm:grid-cols-[8rem_1fr]">
                <dt className="text-muted-foreground">{driver}</dt>
                <dd className="grid gap-0.5 break-all">
                  {unknownDrivers.has(driver) ? (
                    <span role="note" className="text-destructive-foreground">
                      No provider here uses the driver {driver}; check the name. With Enforce, its
                      picks are refused.
                    </span>
                  ) : null}
                  {rows.map((row) => {
                    const aliasOf = aliasRows.get(row);
                    return (
                      <span key={JSON.stringify(row)} className="grid gap-0.5">
                        <code>{modelPolicyRowLine(row)}</code>
                        {aliasOf === undefined ? null : (
                          <span role="note" className="text-destructive-foreground">
                            {aliasOf.length === 0
                              ? `${row.model} is an alias ${row.driver} resolves by itself`
                              : `${row.model} is an alias of ${aliasOf.join(" or ")}`}
                            ; name the full id. A row naming an alias follows it when a provider
                            moves it to a new model, and does not allow a pick of the full id.
                          </span>
                        )}
                        {staleRows.has(row) ? (
                          <span role="note" className="text-destructive-foreground">
                            No {row.driver} model list on a selected machine holds {row.model}; it
                            may be retired or misspelled, so this row may allow nothing there.
                          </span>
                        ) : null}
                      </span>
                    );
                  })}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
      </ForkRow>
      {marks.length > 0 ? (
        <ForkRow
          {...searchableSetting("model-policy-picks")}
          // Nothing to change here: the picks are set where they are made.
          lockPaths={[]}
          description="Your own model picks. They are never refused; the mark shows a pick an agent could not make."
        >
          <ul className="grid gap-1 pb-2 text-xs">
            {marks.map((mark) => (
              <li key={mark.key} className="grid gap-0.5 sm:grid-cols-[14rem_1fr]">
                <span className="text-muted-foreground">{mark.label}</span>
                <span className="break-all">
                  <code>{mark.pick}</code>
                  {mark.outside === null ? null : (
                    <span className="ms-2 text-warning-foreground">
                      outside policy ({mark.outside})
                    </span>
                  )}
                </span>
              </li>
            ))}
          </ul>
        </ForkRow>
      ) : null}
      {editing ? (
        <AllowDialog
          value={shown}
          providers={providers}
          onOpenChange={setEditing}
          onSave={(next, onSaved, onFailed) => save(next, { onSaved, onFailed })}
        />
      ) : null}
    </SettingsSection>
  );
}
