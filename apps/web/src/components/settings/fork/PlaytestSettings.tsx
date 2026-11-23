/**
 * Fork: playtest sessions (`playtest`). A project's adapter replaces the environment's whole, so a
 * change on a project screen saves the whole value it shows as the project's own. Every write is an
 * ordinary settings patch from the person at the screen; agents cannot change it.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  DEFAULT_SERVER_SETTINGS,
  type PlaytestSettings as PlaytestValue,
} from "@t3tools/contracts";
import * as Equal from "effect/Equal";
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
import { Switch } from "../../ui/switch";
import { Textarea } from "../../ui/textarea";
import { AdvancedSettings } from "../AdvancedSettings";
import { SettingResetButton, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import {
  useClearScopedSettings,
  useScopedSettingSource,
  useScopedSettings,
  useScopedSettingsMixed,
  useUpdateScopedSettings,
} from "../useScopedSettings";
import {
  playtestCommandLine,
  playtestMissingSteps,
  playtestStepsText,
  withPlaytestSteps,
} from "./playtestSettings.logic";
import { BoundedNumberField, ForkRow, useSubRowReset } from "./ForkRow";
import { defaultIs } from "./forkDefaultText.logic";

const MIXED =
  "The selected environments or projects have different playtest settings. Select one to edit them.";

const LIMITS = [
  {
    key: "renewMinutes",
    id: "playtest-renew-minutes",
    min: 1,
    max: 60,
    unit: "minutes",
    description:
      "How often T3 renews a live session's claim. Keep it well under the claim's expiry.",
  },
  {
    key: "idleMinutes",
    id: "playtest-idle-minutes",
    min: 5,
    max: 1440,
    unit: "minutes",
    description: "A session with no round and no message from you for this long ends.",
  },
  {
    key: "maxHours",
    id: "playtest-max-hours",
    min: 1,
    max: 48,
    unit: "hours",
    description: "Every session ends this long after it starts.",
  },
] as const;

function StepsDialog({
  value,
  onOpenChange,
  onSave,
}: {
  readonly value: PlaytestValue;
  readonly onOpenChange: (open: boolean) => void;
  readonly onSave: (next: PlaytestValue, onSaved: () => void, onFailed: () => void) => void;
}) {
  const [text, setText] = useState(() => playtestStepsText(value));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const save = () => {
    if (saving) return;
    const result = withPlaytestSteps(value, text);
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
        setError("The server refused the steps; your text is kept to fix and save again.");
      },
    );
  };
  return (
    <Dialog open onOpenChange={onOpenChange}>
      <DialogPopup className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Edit playtest steps</DialogTitle>
          <DialogDescription>
            The adapter&apos;s four steps as JSON, as in the settings file. Each runs on this server
            with your rights, without a shell, by its program&apos;s full path: only add programs
            you trust.
          </DialogDescription>
        </DialogHeader>
        <DialogPanel>
          <Textarea
            aria-label="Playtest steps"
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
            {saving ? "Saving…" : "Save steps"}
          </Button>
        </DialogFooter>
      </DialogPopup>
    </Dialog>
  );
}

export function PlaytestSettings({ isProjectScope }: { readonly isProjectScope: boolean }) {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const clearScopedSettings = useClearScopedSettings();
  const source = useScopedSettingSource(["playtest"]);
  const mixed = useScopedSettingsMixed(["playtest"]) || source === "mixed";
  const shown = useScopedSettings((settings) => settings.playtest);
  const [editing, setEditing] = useState(false);

  const save = (next: PlaytestValue, options?: { onSaved?: () => void; onFailed?: () => void }) =>
    updateSettings({ playtest: next }, options);
  const subRowReset = useSubRowReset(
    "playtest",
    shown,
    isProjectScope && source === "project",
    save,
  );
  const missing = playtestMissingSteps(shown);
  const scopeNote = !isProjectScope
    ? ""
    : source === "project"
      ? " This project's adapter replaces the environment's whole."
      : " A change here gives this project its own copy of the environment's adapter.";
  const steps = [
    ...(shown.claim ? [["claim", shown.claim] as const] : []),
    ...(shown.renew ? [["renew", shown.renew] as const] : []),
    ...shown.push.map((step) => [`push: ${step.name}`, step] as const),
    ...(shown.release ? [["release", shown.release] as const] : []),
  ];

  if (mixed) {
    return (
      <SettingsSection id="playtest-section" title="Playtest sessions">
        <ForkRow
          serverScoped
          {...searchableSetting("playtest")}
          settingKeys={["playtest"]}
          lockPaths={["playtest"]}
          mixed
          description={MIXED}
        />
      </SettingsSection>
    );
  }

  return (
    <SettingsSection id="playtest-section" title="Playtest sessions">
      <ForkRow
        serverScoped
        {...searchableSetting("playtest")}
        settingKeys={["playtest"]}
        lockPaths={["playtest"]}
        onResetOverride={() => clearScopedSettings(["playtest"])}
        description={`${
          !shown.enabled && missing.length > 0
            ? `Set the steps below first (missing: ${missing.join(", ")}).`
            : `On: an agent can start a playtest with its playtest tool: T3 claims your test target, sends each round with your steps, and gives the target back at the end, so you can try unfinished work. Off: no playtest starts. ${defaultIs("playtest")}`
        }${scopeNote}`}
        resetAction={
          !isProjectScope && !Equal.equals(shown, DEFAULT_SERVER_SETTINGS.playtest) ? (
            <SettingResetButton
              ariaLabel="Reset playtest to default"
              onClick={() => save(DEFAULT_SERVER_SETTINGS.playtest)}
            />
          ) : null
        }
        control={
          <Switch
            checked={shown.enabled}
            disabled={!shown.enabled && missing.length > 0}
            onCheckedChange={(checked) => save({ ...shown, enabled: Boolean(checked) })}
            aria-label="Playtest sessions"
          />
        }
      />
      <ForkRow
        {...searchableSetting("playtest-steps")}
        // Not settingKeys: upstream's reset there would clear the project's whole override.
        managedPaths={["playtest"]}
        lockPaths={["playtest"]}
        resetAction={subRowReset(
          ["claim", "renew", "push", "release"],
          "Reset playtest steps to the environment's",
        )}
        description="The commands T3 runs: claim takes the target, renew keeps the claim, push sends a round (one or more named steps), release gives the target back. All four are needed before playtests can be turned on."
        control={
          <Button size="xs" variant="outline" onClick={() => setEditing(true)}>
            Edit steps
          </Button>
        }
      >
        {steps.length > 0 ? (
          <dl className="grid gap-1 pb-2 text-xs">
            {steps.map(([label, step]) => (
              <div key={label} className="grid gap-0.5 sm:grid-cols-[8rem_1fr]">
                <dt className="text-muted-foreground">{label}</dt>
                <dd className="break-all">
                  <code>{playtestCommandLine(step)}</code>
                </dd>
              </div>
            ))}
          </dl>
        ) : null}
      </ForkRow>
      <AdvancedSettings targetIds={LIMITS.map((limit) => limit.id)}>
        {LIMITS.map((limit) => {
          const title = searchableSetting(limit.id).title;
          return (
            <ForkRow
              key={limit.key}
              {...searchableSetting(limit.id)}
              managedPaths={["playtest"]}
              lockPaths={["playtest"]}
              resetAction={subRowReset(
                [limit.key],
                `Reset ${title.toLowerCase()} to the environment's`,
              )}
              description={`${limit.description} Default: ${DEFAULT_SERVER_SETTINGS.playtest[limit.key]} ${limit.unit}.`}
              control={
                <div className="flex items-center gap-2">
                  <BoundedNumberField
                    labels={{
                      decrease: `Decrease ${title.toLowerCase()}`,
                      input: `${title} in ${limit.unit}`,
                      increase: `Increase ${title.toLowerCase()}`,
                    }}
                    value={shown[limit.key]}
                    min={limit.min}
                    step={1}
                    bound={(value) => Math.min(limit.max, Math.max(limit.min, value))}
                    onCommit={(value, options) => save({ ...shown, [limit.key]: value }, options)}
                  />
                  <span className="text-xs text-muted-foreground">{limit.unit}</span>
                </div>
              }
            />
          );
        })}
      </AdvancedSettings>
      {editing ? (
        <StepsDialog
          value={shown}
          onOpenChange={setEditing}
          onSave={(next, onSaved, onFailed) => save(next, { onSaved, onFailed })}
        />
      ) : null}
    </SettingsSection>
  );
}
