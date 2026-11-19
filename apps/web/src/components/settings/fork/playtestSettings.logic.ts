/** Fork: the pure parts of the playtest panel (`playtest`, server and project). */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import { PlaytestSettings, type PlaytestStep } from "@t3tools/contracts";
import * as Schema from "effect/Schema";

const STEP_KEYS = ["claim", "renew", "push", "release"] as const;
type StepKey = (typeof STEP_KEYS)[number];

const decodePlaytestStrict = Schema.decodeUnknownExit(PlaytestSettings);

/** The steps that keep the switch off: on needs claim, renew, at least one push step and release. */
export function playtestMissingSteps(settings: PlaytestSettings): ReadonlyArray<string> {
  return [
    ...(settings.claim === undefined ? ["claim"] : []),
    ...(settings.renew === undefined ? ["renew"] : []),
    ...(settings.push.length === 0 ? ["push"] : []),
    ...(settings.release === undefined ? ["release"] : []),
  ];
}

/** The program and its arguments as one line, quoting an argument with a space. */
export const playtestCommandLine = (step: PlaytestStep) =>
  step.argv.map((arg) => (/\s/.test(arg) ? JSON.stringify(arg) : arg)).join(" ");

/** The four steps as the editor shows them: JSON, as in the settings file. */
export function playtestStepsText(settings: PlaytestSettings): string {
  const steps: Partial<Record<StepKey, unknown>> = {};
  for (const key of STEP_KEYS) {
    if (key === "push" ? settings.push.length > 0 : settings[key] !== undefined) {
      steps[key] = settings[key];
    }
  }
  return JSON.stringify(steps, null, 2);
}

/**
 * The whole new value with the edited steps in place of the old ones (a step left out is removed),
 * or why it cannot be saved. Checked as the server's save checks it, unknown fields refused.
 */
export function withPlaytestSteps(
  settings: PlaytestSettings,
  text: string,
): { readonly value: PlaytestSettings } | { readonly error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() === "" ? "{}" : text);
  } catch {
    return { error: "The steps are not valid JSON." };
  }
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    return { error: "The steps are a JSON object with claim, renew, push and release." };
  }
  const unknown = Object.keys(parsed).filter(
    (key) => !(STEP_KEYS as ReadonlyArray<string>).includes(key),
  );
  if (unknown.length > 0) return { error: `Not a step: ${unknown.join(", ")}.` };
  const { claim: _c, renew: _r, release: _l, ...rest } = settings;
  const exit = decodePlaytestStrict(
    { ...rest, push: [], ...parsed },
    { onExcessProperty: "error" },
  );
  if (exit._tag === "Success") return { value: exit.value };
  return {
    error:
      "The steps cannot be saved: each needs argv (its program by full path first, at most 50 items), an optional timeoutSeconds (1 to 3600) and description; push steps need a unique lower-case name other than claim, renew or release. While playtest is on, all four steps are needed.",
  };
}
