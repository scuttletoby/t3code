/**
 * Fork: usage pacing (`usagePacingGate`, `usagePacingLowerEffort`, `usagePacing`). Server-wide, so
 * a project screen does not show it. Every write is an ordinary settings patch from the person at
 * the screen; agents cannot change it.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  burnFirstWindowKinds,
  DEFAULT_SERVER_SETTINGS,
  type ServerSettingsPatch,
  type UsagePacingGateMode,
  type UsagePacingInstanceSettingsPatch,
} from "@t3tools/contracts";
import { useState } from "react";

import { Button } from "../../ui/button";
import { Checkbox } from "../../ui/checkbox";
import { Input } from "../../ui/input";
import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../../ui/select";
import { Switch } from "../../ui/switch";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { targetsDiffer } from "../waitsAndSlotsSettings.logic";
import { useScopedSettings, useUpdateScopedSettings } from "../useScopedSettings";
import { BoundedNumberField, ForkRow, useSelectedProviders } from "./ForkRow";
import {
  USAGE_PACING_GATE_OPTIONS,
  USAGE_PACING_WINDOW_KINDS,
  USAGE_PACING_WINDOW_LABELS,
  burnFirstWindowsAfterToggle,
  formatLowerEffort,
  knownEffortLevels,
  parseLowerEffort,
  usagePacingAccountRows,
  usagePacingAccountSummary,
} from "./usagePacingSettings.logic";

type UpdateSettings = ReturnType<typeof useUpdateScopedSettings>;

const MIXED = "The selected environments have different values. Select one environment to edit it.";

export function UsagePacingSettings() {
  const updateSettings = useUpdateScopedSettings();
  return (
    <SettingsSection id="usage-pacing-section" title="Usage pacing">
      <GateRow updateSettings={updateSettings} />
      <LowerEffortRow updateSettings={updateSettings} />
      <AccountRows updateSettings={updateSettings} />
    </SettingsSection>
  );
}

function GateRow({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const gate = useScopedSettings((settings) => settings.usagePacingGate);
  const { targets } = useSettingsScope();
  const mixed = targetsDiffer(targets, (settings) => settings.usagePacingGate);
  const option = USAGE_PACING_GATE_OPTIONS.find((candidate) => candidate.value === gate);
  const save = (value: UsagePacingGateMode) => updateSettings({ usagePacingGate: value });
  return (
    <ForkRow
      serverScoped
      {...searchableSetting("usage-pacing-gate")}
      settingKeys={["usagePacingGate"]}
      lockPaths={["usagePacingGate"]}
      mixed={mixed}
      description={
        mixed
          ? MIXED
          : `What happens when an agent starts a task with delegate_task. ${option?.description ?? ""}`
      }
      resetAction={
        !mixed && gate !== DEFAULT_SERVER_SETTINGS.usagePacingGate ? (
          <SettingResetButton
            ariaLabel="Reset usage pacing gate to default"
            onClick={() => save(DEFAULT_SERVER_SETTINGS.usagePacingGate)}
          />
        ) : null
      }
      control={
        <Select
          value={mixed ? null : gate}
          onValueChange={(value) => {
            if (value !== null && value !== gate) save(value as UsagePacingGateMode);
          }}
        >
          <SelectTrigger size="sm" className="w-full sm:w-36" aria-label="Usage pacing gate">
            <SelectValue>{mixed ? "Mixed" : (option?.label ?? gate)}</SelectValue>
          </SelectTrigger>
          <SelectPopup align="end" alignItemWithTrigger={false}>
            {USAGE_PACING_GATE_OPTIONS.map((candidate) => (
              <SelectItem hideIndicator key={candidate.value} value={candidate.value}>
                {candidate.label}
              </SelectItem>
            ))}
          </SelectPopup>
        </Select>
      }
    />
  );
}

function LowerEffortRow({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const stored = useScopedSettings((settings) => settings.usagePacingLowerEffort);
  const { targets } = useSettingsScope();
  const providers = useSelectedProviders();
  const mixed = targetsDiffer(targets, (settings) => settings.usagePacingLowerEffort);
  const storedText = formatLowerEffort(stored);
  const [draft, setDraft] = useState({ saved: storedText, text: storedText });
  const [error, setError] = useState<string | null>(null);
  if (draft.saved !== storedText) setDraft({ saved: storedText, text: storedText });

  const commit = () => {
    if (draft.text === storedText) return;
    const parsed = parseLowerEffort(draft.text, knownEffortLevels(providers));
    if ("error" in parsed) {
      setError(parsed.error);
      return;
    }
    setError(null);
    updateSettings(
      { usagePacingLowerEffort: parsed.value },
      { onFailed: () => setDraft({ saved: storedText, text: storedText }) },
    );
  };

  return (
    <ForkRow
      serverScoped
      {...searchableSetting("usage-pacing-lower-effort")}
      settingKeys={["usagePacingLowerEffort"]}
      lockPaths={["usagePacingLowerEffort"]}
      mixed={mixed}
      description={
        mixed
          ? MIXED
          : "With the gate on Enforce: the effort a task drops to while its account is ahead of pace, as pairs like high=medium. A level not listed waits instead. Empty never lowers one."
      }
      resetAction={
        !mixed && storedText !== "" ? (
          <SettingResetButton
            ariaLabel="Reset lower effort to default"
            onClick={() => updateSettings({ usagePacingLowerEffort: {} })}
          />
        ) : null
      }
      control={
        mixed ? null : (
          <Input
            size="sm"
            className="w-full sm:w-56"
            aria-label="Lower effort when ahead of pace"
            placeholder="high=medium"
            value={draft.text}
            onChange={(event) => {
              setError(null);
              setDraft({ ...draft, text: event.target.value });
            }}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key === "Enter") commit();
            }}
          />
        )
      }
    >
      {error !== null ? (
        <p role="alert" className="pb-2 text-xs text-destructive-foreground">
          {error}
        </p>
      ) : null}
    </ForkRow>
  );
}

function AccountRows({ updateSettings }: { readonly updateSettings: UpdateSettings }) {
  const searchableSetting = useSearchableSetting();
  const usagePacing = useScopedSettings((settings) => settings.usagePacing);
  const { targets } = useSettingsScope();
  const providers = useSelectedProviders();
  const mixed = targetsDiffer(targets, (settings) => settings.usagePacing);
  const rows = usagePacingAccountRows(providers, usagePacing);
  const patch = (instanceId: string, value: UsagePacingInstanceSettingsPatch | null) =>
    ({ usagePacing: { [instanceId]: value } }) satisfies ServerSettingsPatch;

  return (
    <>
      <ForkRow
        serverScoped
        {...searchableSetting("usage-pacing")}
        settingKeys={["usagePacing"]}
        lockPaths={["usagePacing"]}
        mixed={mixed}
        description={
          mixed
            ? MIXED
            : rows.length === 0
              ? "No provider accounts on this server yet."
              : "Per account: the share of each usage window kept free for you (released over its last 12 hours), and whether agents spend it first or it has no limits. Only the gate acts on these."
        }
      />
      {mixed
        ? null
        : rows.map((row) => (
            <ForkRow
              key={row.instanceId}
              id={`usage-pacing-account-${row.instanceId}`}
              // Not serverScoped (the row above carries the scope chip), but a lock still applies,
              // to the switches below the title too.
              settingKeys={["usagePacing"]}
              lockPaths={["usagePacing"]}
              title={`Reserve on ${row.label}`}
              status={usagePacingAccountSummary(row.settings)}
              description={
                row.known
                  ? "Points of each limit window kept free for you. 0 keeps none."
                  : "This account is not on this server any more. Remove its entry, or keep it for when the account comes back."
              }
              resetAction={
                row.stored && row.known ? (
                  <SettingResetButton
                    ariaLabel={`Reset usage pacing for ${row.label} to default`}
                    onClick={() => updateSettings(patch(row.instanceId, null))}
                  />
                ) : null
              }
              control={
                row.known ? (
                  <div className="flex items-center gap-2">
                    <BoundedNumberField
                      labels={{
                        decrease: `Decrease reserve on ${row.label}`,
                        input: `Reserve on ${row.label} in percent`,
                        increase: `Increase reserve on ${row.label}`,
                      }}
                      value={row.settings.reservePercent}
                      min={0}
                      step={5}
                      bound={(value) => Math.min(100, value)}
                      onCommit={(value, options) =>
                        updateSettings(patch(row.instanceId, { reservePercent: value }), options)
                      }
                    />
                    <span className="text-xs text-muted-foreground">%</span>
                  </div>
                ) : (
                  <Button
                    size="xs"
                    variant="destructive-outline"
                    aria-label={`Remove usage pacing for ${row.label}`}
                    onClick={() => updateSettings(patch(row.instanceId, null))}
                  >
                    Remove
                  </Button>
                )
              }
            >
              {row.known ? (
                <div className="flex flex-wrap gap-x-6 gap-y-2 pb-2 text-xs text-muted-foreground">
                  <label className="flex items-center gap-2">
                    <Switch
                      size="sm"
                      checked={row.settings.burnFirst}
                      onCheckedChange={(checked) =>
                        updateSettings(patch(row.instanceId, { burnFirst: Boolean(checked) }))
                      }
                      aria-label={`Spend ${row.label} first`}
                    />
                    Spend first (no even pace; only the reserve holds agents back)
                  </label>
                  <label className="flex items-center gap-2">
                    <Switch
                      size="sm"
                      checked={row.settings.noLimits}
                      onCheckedChange={(checked) =>
                        updateSettings(patch(row.instanceId, { noLimits: Boolean(checked) }))
                      }
                      aria-label={`${row.label} has no usage limits`}
                    />
                    No usage limits (an API key)
                  </label>
                  {row.settings.burnFirst ? (
                    <div
                      role="group"
                      aria-label={`Windows ${row.label} spends first`}
                      className="flex basis-full flex-wrap items-center gap-x-4 gap-y-1"
                    >
                      <span>Windows spent first:</span>
                      {USAGE_PACING_WINDOW_KINDS.map((kind) => (
                        <label key={kind} className="flex items-center gap-1.5">
                          <Checkbox
                            checked={burnFirstWindowKinds(row.settings).includes(kind)}
                            aria-label={`Spend ${row.label} first on ${USAGE_PACING_WINDOW_LABELS[kind]} windows`}
                            onCheckedChange={(checked) =>
                              updateSettings(
                                patch(row.instanceId, {
                                  burnFirstWindows: burnFirstWindowsAfterToggle(
                                    row.settings,
                                    kind,
                                    checked === true,
                                  ),
                                }),
                              )
                            }
                          />
                          {USAGE_PACING_WINDOW_LABELS[kind]}
                        </label>
                      ))}
                    </div>
                  ) : null}
                </div>
              ) : null}
            </ForkRow>
          ))}
    </>
  );
}
