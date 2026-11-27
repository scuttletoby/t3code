/**
 * Fork (issue 880): the armed PR sweep panel's choices and words, pure so a test holds each one to
 * the setting's values (packages/contracts/src/forkArmedSweep.ts).
 */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own panel text is English only; upstream's translation catalogs hold no fork keys. */
import type { ArmedSweepMode, ArmedSweepScope } from "@t3tools/contracts";

export const ARMED_SWEEP_MODE_OPTIONS: ReadonlyArray<{
  readonly value: ArmedSweepMode;
  readonly label: string;
}> = [
  { value: "off", label: "Off" },
  { value: "dry", label: "Dry run" },
  { value: "on", label: "On" },
];

export const ARMED_SWEEP_SCOPE_OPTIONS: ReadonlyArray<{
  readonly value: ArmedSweepScope;
  readonly label: string;
}> = [
  { value: "linked", label: "Linked to a thread" },
  { value: "all", label: "All, also armed by hand" },
];

export const labelOf = <T extends string>(
  options: ReadonlyArray<{ readonly value: T; readonly label: string }>,
  value: T,
) => options.find((option) => option.value === value)?.label ?? value;

/** The interval's floor, as the server reads it (a save below it is refused). */
export const MIN_INTERVAL_SECONDS = 60;

/** Each number field's own bound: whole and not negative; the interval at least the floor. */
export function boundCount(value: number, min = 0): number {
  if (!Number.isFinite(value)) return min;
  return Math.max(min, Math.round(value));
}
