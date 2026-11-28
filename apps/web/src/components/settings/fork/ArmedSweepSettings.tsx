/**
 * Fork (issue 880): the armed PR sweep's mode and scope, and its timing in Advanced. Server-wide,
 * so a project screen does not show it. Every write is an ordinary settings save from the person
 * at the screen; agents cannot change these keys.
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: this panel is the fork's own and its text is English only; upstream's translation catalogs hold no fork keys. */
import {
  type ArmedSweepSettings as ArmedSweepValue,
  type ArmedSweepSettingsPatch,
  DEFAULT_SERVER_SETTINGS,
} from "@t3tools/contracts";

import { Select, SelectItem, SelectPopup, SelectTrigger, SelectValue } from "../../ui/select";
import { AdvancedSettings } from "../AdvancedSettings";
import { useSettingsScope } from "../SettingsScopeContext";
import { SettingResetButton, SettingsSection } from "../settingsLayout";
import { useSearchableSetting } from "../settingsSearch";
import { useScopedSettings, useUpdateScopedSettings } from "../useScopedSettings";
import { targetsDiffer } from "../waitsAndSlotsSettings.logic";
import {
  ARMED_SWEEP_MODE_OPTIONS,
  ARMED_SWEEP_SCOPE_OPTIONS,
  boundCount,
  labelOf,
  MIN_INTERVAL_SECONDS,
} from "./armedSweepSettings.logic";
import { BoundedNumberField, ForkRow } from "./ForkRow";
import { defaultIs } from "./forkDefaultText.logic";

const DEFAULTS = DEFAULT_SERVER_SETTINGS.armedSweep;

type NumberKey = "intervalSeconds" | "stallMinutes" | "rerunGraceMinutes" | "maxWakesPerHead";

const NUMBERS: ReadonlyArray<{
  readonly key: NumberKey;
  readonly id:
    | "armed-sweep-interval"
    | "armed-sweep-stall"
    | "armed-sweep-rerun-grace"
    | "armed-sweep-cap";
  readonly unit: string;
  readonly name: string;
  readonly description: string;
  readonly min: number;
  readonly step: number;
}> = [
  {
    key: "intervalSeconds",
    id: "armed-sweep-interval",
    unit: "seconds",
    name: "sweep interval",
    description: `How often the sweep lists the armed pull requests (one GitHub request each time). At least ${MIN_INTERVAL_SECONDS} seconds. Default: ${DEFAULTS.intervalSeconds} seconds.`,
    min: MIN_INTERVAL_SECONDS,
    step: 60,
  },
  {
    key: "stallMinutes",
    id: "armed-sweep-stall",
    unit: "minutes",
    name: "stall time",
    description: `An armed pull request not green this long (checks pending or none) wakes its thread as stalled. Default: ${DEFAULTS.stallMinutes} minutes.`,
    min: 0,
    step: 5,
  },
  {
    key: "rerunGraceMinutes",
    id: "armed-sweep-rerun-grace",
    unit: "minutes",
    name: "re-run grace",
    description: `A red waits this long after its last failed check, so an automatic re-run can try first. 0 for a project without one. Default: ${DEFAULTS.rerunGraceMinutes} minutes.`,
    min: 0,
    step: 1,
  },
  {
    key: "maxWakesPerHead",
    id: "armed-sweep-cap",
    unit: "wakes",
    name: "wakes per head",
    description: `At most this many wake-ups for one pull request head, whatever the reasons; a new push starts again. Default: ${DEFAULTS.maxWakesPerHead}.`,
    min: 0,
    step: 1,
  },
];

function ChoiceSelect<T extends string>({
  value,
  options,
  label,
  onChange,
}: {
  readonly value: T;
  readonly options: ReadonlyArray<{ readonly value: T; readonly label: string }>;
  readonly label: string;
  readonly onChange: (value: T) => void;
}) {
  return (
    <Select
      value={value}
      onValueChange={(next) => {
        if (next !== null && next !== value) onChange(next as T);
      }}
    >
      <SelectTrigger size="sm" className="w-full sm:w-48" aria-label={label}>
        <SelectValue>{labelOf(options, value)}</SelectValue>
      </SelectTrigger>
      <SelectPopup align="end" alignItemWithTrigger={false}>
        {options.map((option) => (
          <SelectItem hideIndicator key={option.value} value={option.value}>
            {option.label}
          </SelectItem>
        ))}
      </SelectPopup>
    </Select>
  );
}

export function ArmedSweepSettings() {
  const searchableSetting = useSearchableSetting();
  const updateSettings = useUpdateScopedSettings();
  const { targets } = useSettingsScope();
  const current = useScopedSettings((settings) => settings.armedSweep);
  const differs = (key: keyof ArmedSweepValue) =>
    targetsDiffer(targets, (settings) => settings.armedSweep[key]);
  const save = (patch: ArmedSweepSettingsPatch, options?: Parameters<typeof updateSettings>[1]) =>
    updateSettings({ armedSweep: patch }, options);
  const reset = (key: keyof ArmedSweepValue, label: string) =>
    !differs(key) && current[key] !== DEFAULTS[key] ? (
      <SettingResetButton
        ariaLabel={`Reset ${label} to default`}
        onClick={() => save({ [key]: DEFAULTS[key] })}
      />
    ) : null;
  return (
    <SettingsSection id="armed-sweep-section" title="Armed pull request sweep">
      <ForkRow
        serverScoped
        {...searchableSetting("armed-sweep")}
        description={`On: every few minutes T3 lists the open pull requests with auto-merge on and wakes the thread that owns one that is red, conflicted, stalled, turned off or closed, once per push. Dry run: it lists them in wait_list and logs what it would send, and sends nothing. ${defaultIs("armedSweep")}`}
        settingKeys={["armedSweep"]}
        lockPaths={["armedSweep"]}
        mixed={differs("mode")}
        resetAction={reset("mode", "the armed sweep")}
        control={
          <ChoiceSelect
            value={current.mode}
            options={ARMED_SWEEP_MODE_OPTIONS}
            label="Armed pull request sweep"
            onChange={(mode) => save({ mode })}
          />
        }
      />
      <ForkRow
        serverScoped
        {...searchableSetting("armed-sweep-scope")}
        description="Linked: only pull requests a thread linked. All: also pull requests armed by hand, which wake the project's orchestrator thread."
        settingKeys={["armedSweep"]}
        lockPaths={["armedSweep"]}
        mixed={differs("scope")}
        resetAction={reset("scope", "the sweep's scope")}
        control={
          <ChoiceSelect
            value={current.scope}
            options={ARMED_SWEEP_SCOPE_OPTIONS}
            label="Which armed pull requests"
            onChange={(scope) => save({ scope })}
          />
        }
      />
      <AdvancedSettings targetIds={NUMBERS.map((number) => number.id)}>
        {NUMBERS.map((number) => (
          <ForkRow
            key={number.key}
            serverScoped
            {...searchableSetting(number.id)}
            description={number.description}
            settingKeys={["armedSweep"]}
            lockPaths={["armedSweep"]}
            mixed={differs(number.key)}
            resetAction={reset(number.key, `the ${number.name}`)}
            control={
              <div className="flex items-center gap-2">
                <BoundedNumberField
                  labels={{
                    decrease: `Decrease ${number.name}`,
                    input: `The ${number.name} in ${number.unit}`,
                    increase: `Increase ${number.name}`,
                  }}
                  value={current[number.key]}
                  min={number.min}
                  step={number.step}
                  bound={(value) => boundCount(value, number.min)}
                  onCommit={(value, options) => save({ [number.key]: value }, options)}
                />
                <span className="text-xs text-muted-foreground">{number.unit}</span>
              </div>
            }
          />
        ))}
      </AdvancedSettings>
    </SettingsSection>
  );
}
