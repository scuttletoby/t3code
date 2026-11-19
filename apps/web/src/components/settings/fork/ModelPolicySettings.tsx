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
import { ForkRow } from "./ForkRow";
import {
  MODEL_POLICY_MODE_OPTIONS,
  modelPolicyAllowText,
  modelPolicyRowLine,
  unreadableModelPolicyText,
  withModelPolicyAllow,
} from "./modelPolicySettings.logic";

const MIXED = "The selected environments have different values. Select one environment to edit it.";

function AllowDialog({
  value,
  onOpenChange,
  onSave,
}: {
  readonly value: ModelPolicy;
  readonly onOpenChange: (open: boolean) => void;
  readonly onSave: (next: ModelPolicyPatch, onSaved: () => void, onFailed: () => void) => void;
}) {
  const [text, setText] = useState(() => modelPolicyAllowText(value));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = () => {
    if (saving) return;
    const result = withModelPolicyAllow(value.mode, text);
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
            defaultEffort is one of them, and useFor is a note shown to agents.
          </DialogDescription>
        </DialogHeader>
        <DialogPanel>
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
            <p role="alert" className="mt-2 text-xs text-destructive-foreground">
              {error}
            </p>
          ) : null}
        </DialogPanel>
        <DialogFooter variant="bare">
          <Button variant="outline" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button disabled={saving} onClick={save}>
            {saving ? "Saving…" : "Save rows"}
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
  const { targets } = useSettingsScope();
  const mixed = targetsDiffer(targets, (settings) => settings.modelPolicy);
  const [editing, setEditing] = useState(false);
  const unreadableText = mixed ? null : unreadableModelPolicyText(shown);
  const option = MODEL_POLICY_MODE_OPTIONS.find((candidate) => candidate.value === shown.mode);

  const save = (
    next: ModelPolicyPatch,
    options?: { onSaved?: () => void; onFailed?: () => void },
  ) => updateSettings({ modelPolicy: next }, options);
  const isDefault =
    unreadableText === null && shown.mode === DEFAULT_MODEL_POLICY.mode && shown.allow.length === 0;

  if (mixed) {
    return (
      <SettingsSection id="model-policy-section" title="Model policy">
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

  const byDriver = new Map<string, Array<ModelPolicy["allow"][number]>>();
  for (const row of shown.allow)
    byDriver.set(row.driver, [...(byDriver.get(row.driver) ?? []), row]);
  return (
    <SettingsSection id="model-policy-section" title="Model policy">
      <ForkRow
        serverScoped
        {...searchableSetting("model-policy")}
        settingKeys={["modelPolicy"]}
        lockPaths={["modelPolicy"]}
        description={`Which models agents may pick with delegate_task. ${option?.description ?? ""} Nothing reads the policy yet.`}
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
            <SelectTrigger size="sm" className="w-full sm:w-36" aria-label="Model policy mode">
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
          <div role="alert" className="pb-2 text-xs text-destructive-foreground">
            The settings file&apos;s model policy cannot be read, so it reads as Enforce with
            nothing allowed: every pick is refused. It is kept as written until you save here; a
            save replaces it.
            <code className="mt-1 block break-all text-muted-foreground">{unreadableText}</code>
          </div>
        ) : null}
      </ForkRow>
      <ForkRow
        {...searchableSetting("model-policy-allow")}
        lockPaths={["modelPolicy"]}
        description={
          shown.allow.length === 0
            ? "No model is allowed yet. With Enforce, every pick is refused."
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
                  {rows.map((row) => (
                    <code key={JSON.stringify(row)}>{modelPolicyRowLine(row)}</code>
                  ))}
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
      </ForkRow>
      {editing ? (
        <AllowDialog
          value={shown}
          onOpenChange={setEditing}
          onSave={(next, onSaved, onFailed) => save(next, { onSaved, onFailed })}
        />
      ) : null}
    </SettingsSection>
  );
}
