import { useState } from "react";
import {
  NumberField,
  NumberFieldDecrement,
  NumberFieldGroup,
  NumberFieldIncrement,
  NumberFieldInput,
} from "../ui/number-field";
import { normalizeIntervalSeconds } from "./SettingsPanels.logic";

export function IntervalSecondsField({
  label,
  value,
  min,
  step,
  unit = "seconds",
  onCommit,
}: {
  label: string;
  value: number;
  min: number;
  step: number;
  /** Fork: the field's unit for its accessible name; seconds unless given. */
  unit?: string;
  onCommit: (seconds: number, options: { onFailed: () => void }) => void;
}) {
  const [field, setField] = useState({
    saved: value,
    draft: value as number | null,
    committed: value,
    revision: 0,
  });
  if (field.saved !== value) {
    setField({ ...field, saved: value, draft: value, committed: value });
  }

  return (
    <NumberField
      value={field.draft}
      min={min}
      step={step}
      size="sm"
      className="w-32"
      onValueChange={(draft) => setField((current) => ({ ...current, draft }))}
      onValueCommitted={(next) => {
        if (next === null) {
          setField((current) => ({ ...current, draft: current.saved }));
          return;
        }
        const seconds = normalizeIntervalSeconds(next, min);
        if (seconds === field.committed) {
          setField((current) => ({ ...current, draft: seconds }));
          return;
        }
        const revision = field.revision + 1;
        setField((current) => ({ ...current, draft: seconds, committed: seconds, revision }));
        onCommit(seconds, {
          onFailed: () =>
            setField((current) =>
              current.revision !== revision
                ? current
                : {
                    ...current,
                    committed: current.saved,
                    draft: current.draft === seconds ? current.saved : current.draft,
                  },
            ),
        });
      }}
    >
      <NumberFieldGroup>
        <NumberFieldDecrement aria-label={`Decrease ${label}`} />
        <NumberFieldInput
          aria-label={`${label.charAt(0).toUpperCase()}${label.slice(1)} in ${unit}`}
        />
        <NumberFieldIncrement aria-label={`Increase ${label}`} />
      </NumberFieldGroup>
    </NumberField>
  );
}
