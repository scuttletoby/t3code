/** Fork: the pure parts of the model policy panel (`modelPolicy`, server only). */
/* oxlint-disable t3code/no-untranslated-copy -- Fork: the fork's own settings are English only; upstream's translation catalogs hold no fork keys. */
import {
  type ModelPolicy,
  type ModelPolicyMode,
  ModelPolicyPatch,
  type ModelPolicyRow,
} from "@t3tools/contracts";
import * as Schema from "effect/Schema";

const decodePatch = Schema.decodeUnknownExit(ModelPolicyPatch);

export const MODEL_POLICY_MODE_OPTIONS: ReadonlyArray<{
  readonly value: ModelPolicyMode;
  readonly label: string;
  readonly description: string;
}> = [
  { value: "off", label: "Off", description: "Agents pick any model, as upstream's do." },
  {
    value: "advise",
    label: "Advise",
    description: "Agents are told which models they may pick; a pick off the list still runs.",
  },
  {
    value: "enforce",
    label: "Enforce",
    description: "A pick off the list is refused.",
  },
];

/** The settings file's own text when it could not be read (the rules then read as enforce, nothing allowed). */
export function unreadableModelPolicyText(policy: ModelPolicy): string | null {
  return "unreadable" in policy
    ? (JSON.stringify(policy.unreadable) ?? String(policy.unreadable))
    : null;
}

/** The allowed rows as the editor shows them: JSON, as in the settings file. */
export const modelPolicyAllowText = (policy: ModelPolicy) => JSON.stringify(policy.allow, null, 2);

/** The unreadable value's own fields, when it is a JSON object. */
function unreadableFields(policy: ModelPolicy): Readonly<Record<string, unknown>> | null {
  if (!("unreadable" in policy)) return null;
  const raw = policy.unreadable;
  return typeof raw === "object" && raw !== null && !Array.isArray(raw)
    ? (raw as Record<string, unknown>)
    : null;
}

/**
 * The text the rows editor starts from. For an unreadable value, its own rows when it has a list
 * of them (a bare list counts too), so the person fixes the typo instead of retyping every row;
 * the fail-closed `[]` would throw the text away without saying so.
 */
export function modelPolicyEditorText(policy: ModelPolicy): string {
  if (!("unreadable" in policy)) return modelPolicyAllowText(policy);
  const raw = Array.isArray(policy.unreadable)
    ? policy.unreadable
    : unreadableFields(policy)?.allow;
  return JSON.stringify(Array.isArray(raw) ? raw : [], null, 2);
}

/** The mode an unreadable value names, when it names one of the three; never the fail-closed one. */
export function unreadableModelPolicyMode(policy: ModelPolicy): ModelPolicyMode | null {
  const mode = unreadableFields(policy)?.mode;
  return MODEL_POLICY_MODE_OPTIONS.find((option) => option.value === mode)?.value ?? null;
}

/**
 * The drivers the rows name that no provider on the selected environments has, in the rows' order
 * (a typo such as `claudAgent`: with Enforce, every pick of that provider is refused). Empty when
 * no provider is known, so a disconnected environment flags nothing.
 */
export function unknownModelPolicyDrivers(
  rows: ReadonlyArray<ModelPolicyRow>,
  knownDrivers: ReadonlySet<string>,
): ReadonlyArray<string> {
  if (knownDrivers.size === 0) return [];
  return [...new Set(rows.map((row) => row.driver))].filter((driver) => !knownDrivers.has(driver));
}

/** One allowed row as one line. */
export function modelPolicyRowLine(row: ModelPolicyRow): string {
  const efforts =
    row.efforts === undefined
      ? ""
      : ` (efforts ${row.efforts.join(", ")}${
          row.defaultEffort === undefined ? "" : `; default ${row.defaultEffort}`
        })`;
  return `${row.model}${efforts}${row.useFor ? `: ${row.useFor}` : ""}`;
}

/**
 * The whole value a save sends, with `mode` and the edited rows, or why it cannot be saved. Checked
 * as the server's save checks it: strict, an unknown key refused.
 */
export function withModelPolicyAllow(
  mode: ModelPolicyMode,
  text: string,
): { readonly value: ModelPolicyPatch } | { readonly error: string } {
  let parsed: unknown;
  try {
    parsed = JSON.parse(text.trim() === "" ? "[]" : text);
  } catch {
    return { error: "The rows are not valid JSON." };
  }
  if (!Array.isArray(parsed)) return { error: "The rows are a JSON list, [ ... ]." };
  const exit = decodePatch({ mode, allow: parsed });
  if (exit._tag === "Success") return { value: exit.value };
  return {
    error:
      "The rows cannot be saved: each needs driver and model; efforts is a non-empty list, defaultEffort one of them, useFor a short note. Any other key is refused.",
  };
}
