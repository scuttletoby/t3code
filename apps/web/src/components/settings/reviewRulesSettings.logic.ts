/**
 * Fork: pure helpers for the review rules panel (`reviewRules`). The panel only writes the
 * person's own settings through the normal settings patch; nothing here is reachable by an agent.
 */
import {
  type ReviewMinors,
  type ReviewRules,
  type ReviewRunner,
  effectiveReviewRules,
} from "@t3tools/contracts";
import * as Equal from "effect/Equal";

export type ReviewRulesPresetChoice = "recommended" | "off";

/** What each button saves. A save replaces the whole stored value (the `replace` patch rule). */
export const REVIEW_RULES_BUTTON_VALUE = {
  recommended: { preset: "recommended" },
  off: { preset: "off" },
} as const satisfies Record<ReviewRulesPresetChoice, ReviewRules>;

/** `unreadable`: the stored text is not a valid value, so it reads as off. */
export type ReviewRulesState = "off" | "on" | "unreadable";

export function reviewRulesState(value: ReviewRules | null | undefined): ReviewRulesState {
  if (value != null && "unreadable" in value) return "unreadable";
  return effectiveReviewRules(value ?? undefined).preset === "recommended" ? "on" : "off";
}

export function reviewRulesSummary(value: ReviewRules | null | undefined): string {
  switch (reviewRulesState(value)) {
    case "on":
      return "On: recommended preset";
    case "unreadable":
      return "Off: the stored value cannot be read";
    case "off":
      return "Off";
  }
}

const FIELD_LABELS: Readonly<Record<string, string>> = {
  runner: "Lens runner",
  lensRunners: "Lens runners",
  stateQuestion: "State question",
  minors: "Small findings",
  configPath: "Repository config",
  reportMaxChars: "Longest report",
};

/** The fields a stored value writes out besides `preset`, in the panel's order. */
export function handSetFields(value: ReviewRules | null | undefined): ReadonlyArray<string> {
  if (value == null || "unreadable" in value) return [];
  return Object.keys(FIELD_LABELS).filter(
    (key) => Object.hasOwn(value, key) && value[key as keyof ReviewRules] !== undefined,
  );
}

function handSetFieldLabels(value: ReviewRules | null | undefined): ReadonlyArray<string> {
  return handSetFields(value).map((key) => FIELD_LABELS[key]!.toLowerCase());
}

const MINORS_LABELS: Readonly<Record<ReviewMinors, string>> = {
  "same-round": "Fixed in the same review round",
  issue: "Filed as follow-up issues",
  free: "No rule",
};

function formatReviewRunner(runner: ReviewRunner): string {
  return `${runner.driver} · ${runner.model} · ${runner.effort} effort`;
}

export interface ReviewRulesValueRow {
  readonly key: string;
  readonly label: string;
  readonly value: string;
  /** False when the stored value writes this field out by hand. */
  readonly fromPreset: boolean;
}

/**
 * The values a review lens would run with, each marked as the preset's or set by hand. Empty when
 * the rules are off (an unreadable value is off too). The `approach` lens comes first, then any
 * other lens with its own runner.
 */
export function effectiveReviewRulesRows(
  value: ReviewRules | null | undefined,
): ReadonlyArray<ReviewRulesValueRow> {
  const effective = effectiveReviewRules(value ?? undefined);
  if (effective.preset !== "recommended" || value == null) return [];
  const own = (key: keyof ReviewRules) => Object.hasOwn(value, key) && value[key] !== undefined;
  const ownLenses = value.lensRunners ?? {};
  const lensNames = Object.keys(effective.lensRunners).toSorted((a, b) =>
    a === "approach" ? -1 : b === "approach" ? 1 : a.localeCompare(b),
  );
  return [
    {
      key: "runner",
      label: "Lens runner",
      value: formatReviewRunner(effective.runner),
      fromPreset: !own("runner"),
    },
    ...lensNames.map((name) => ({
      key: `lensRunners.${name}`,
      label: `Runner for the ${name} lens`,
      value: formatReviewRunner(effective.lensRunners[name]!),
      fromPreset: !Object.hasOwn(ownLenses, name),
    })),
    {
      key: "stateQuestion",
      label: "State question",
      value: effective.stateQuestion,
      fromPreset: !own("stateQuestion"),
    },
    {
      key: "minors",
      label: "Small findings",
      value: `${MINORS_LABELS[effective.minors]} (${effective.minors})`,
      fromPreset: !own("minors"),
    },
    {
      key: "configPath",
      label: "Repository config",
      value: effective.configPath,
      fromPreset: !own("configPath"),
    },
    {
      key: "reportMaxChars",
      label: "Longest report",
      value: `${effective.reportMaxChars} characters`,
      fromPreset: !own("reportMaxChars"),
    },
  ];
}

const UNREADABLE_SHOWN_CHARS = 600;

/** The person's unreadable text as it is stored, shortened for the panel; null when readable. */
export function unreadableReviewRulesText(value: ReviewRules | null | undefined): string | null {
  if (value == null || !("unreadable" in value)) return null;
  let text: string;
  try {
    text = JSON.stringify(value.unreadable) ?? String(value.unreadable);
  } catch {
    text = String(value.unreadable);
  }
  return text.length > UNREADABLE_SHOWN_CHARS ? `${text.slice(0, UNREADABLE_SHOWN_CHARS)}…` : text;
}

export type ReviewRulesWritePlan =
  | { readonly kind: "same" }
  | { readonly kind: "write" }
  | { readonly kind: "confirm"; readonly reasons: ReadonlyArray<string> };

/**
 * What a button does to the values it replaces (one per selected environment or checkout; a
 * project with no override of its own is `undefined`). `same` when every one is already the
 * button's value, so a second click saves nothing. `confirm` when the save would drop something
 * the person wrote: an unreadable value's text, or fields written out by hand.
 */
export function planReviewRulesWrite(
  stored: ReadonlyArray<ReviewRules | null | undefined>,
  choice: ReviewRulesPresetChoice,
): ReviewRulesWritePlan {
  const next = REVIEW_RULES_BUTTON_VALUE[choice];
  if (stored.length > 0 && stored.every((value) => value != null && Equal.equals(value, next))) {
    return { kind: "same" };
  }
  const reasons: string[] = [];
  if (stored.some((value) => reviewRulesState(value) === "unreadable")) {
    reasons.push(
      "The stored value cannot be read. Saving replaces it, and the text written there is lost.",
    );
  }
  const dropped = [...new Set(stored.flatMap((value) => handSetFieldLabels(value)))];
  if (dropped.length > 0) {
    reasons.push(`Saving removes the fields set by hand: ${dropped.join(", ")}.`);
  }
  return reasons.length > 0 ? { kind: "confirm", reasons } : { kind: "write" };
}

/** Clearing a project's override: confirm only when it drops an unreadable value's text. */
export function planReviewRulesOverrideClear(
  overrides: ReadonlyArray<ReviewRules | null | undefined>,
): ReviewRulesWritePlan {
  return overrides.some((value) => reviewRulesState(value) === "unreadable")
    ? {
        kind: "confirm",
        reasons: [
          "This project's stored value cannot be read. Using the environment's rules removes it, and the text written there is lost.",
        ],
      }
    : { kind: "write" };
}

export function reviewRulesDescription(
  value: ReviewRules | null | undefined,
  scope: { readonly isProjectScope: boolean; readonly projectOverrides: boolean },
): string {
  const where = scope.isProjectScope
    ? scope.projectOverrides
      ? "This project overrides the environment's review rules. "
      : "This project uses the environment's review rules. "
    : "";
  switch (reviewRulesState(value)) {
    case "on":
      return `${where}On: review lenses run with the values below. Nothing uses them yet; the review runner comes in a later release.`;
    case "unreadable":
      return `${where}The stored value is not valid, so review rules are off. It is kept as written until it is fixed in the settings file or replaced here.`;
    case "off":
      return handSetFields(value).length > 0
        ? `${where}Off. The fields written out by hand do nothing while the preset is off.`
        : `${where}Off: nothing is listed, changed or checked.`;
  }
}

/**
 * The panel's message for a failed save. When some environments saved, it names the ones that did
 * not instead of claiming nothing changed; the toast has the reasons.
 */
export function reviewRulesSaveError(
  failure: { readonly failed: readonly string[]; readonly saved: readonly string[] } | undefined,
): string {
  if (failure === undefined || failure.saved.length === 0) {
    return "Not saved. The review rules stay as they were.";
  }
  return `Not saved on ${failure.failed.join(", ")}. Saved on ${failure.saved.join(", ")}.`;
}
