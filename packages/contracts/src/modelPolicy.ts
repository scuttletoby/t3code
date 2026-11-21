/**
 * Fork: the model policy, which models (and efforts) agents may pick, per provider driver: the
 * setting, the fork's start rows, and the one check of a pick that the server (agents' picks,
 * `apps/server/src/fork/modelPolicyCheck.ts`) and Settings (the "outside policy" mark) share.
 * Wired into the server settings only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString, TrimmedString } from "./baseSchemas.ts";
import { ProviderDriverKind } from "./providerInstance.ts";

/** `off` changes nothing; `advise` only tells the agent; `enforce` refuses a pick off the list. */
export const ModelPolicyMode = Schema.Literals(["off", "advise", "enforce"]);
export type ModelPolicyMode = typeof ModelPolicyMode.Type;

/** One allowed model. Without `efforts` only the model is checked (it has no effort control). */
export const ModelPolicyRow = Schema.Struct({
  driver: ProviderDriverKind,
  model: TrimmedNonEmptyString,
  efforts: Schema.optionalKey(Schema.NonEmptyArray(TrimmedNonEmptyString)),
  defaultEffort: Schema.optionalKey(TrimmedNonEmptyString),
  /** Shown to agents when they pick a model. */
  useFor: Schema.optionalKey(TrimmedString),
}).check(
  Schema.makeFilter(
    (row) =>
      row.efforts === undefined ||
      row.defaultEffort === undefined ||
      row.efforts.includes(row.defaultEffort),
    { expected: "a defaultEffort that is one of the row's efforts" },
  ),
);
export type ModelPolicyRow = typeof ModelPolicyRow.Type;

const ModelPolicyRules = {
  mode: ModelPolicyMode,
  allow: Schema.Array(ModelPolicyRow),
};

/**
 * The value as the settings file holds it. Both fields default, as the writer saves sparse
 * (`{mode: "enforce", allow: []}` is stored as `{mode: "enforce"}`).
 */
const ModelPolicyStored = Schema.Struct({
  mode: ModelPolicyRules.mode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  allow: ModelPolicyRules.allow.pipe(Schema.withDecodingDefault(Effect.succeed([]))),
});

/**
 * The setting as the server holds it. `unreadable` is set only when the file's value is bad: it is
 * the person's text, kept so a save of another setting writes it back unchanged (the rules then
 * stay {@link MODEL_POLICY_FAIL_CLOSED}). Saving the key itself replaces it.
 */
export const ModelPolicy = Schema.Struct({
  ...ModelPolicyStored.fields,
  unreadable: Schema.optionalKey(Schema.Unknown),
});
export type ModelPolicy = typeof ModelPolicy.Type;

export const DEFAULT_MODEL_POLICY: ModelPolicy = { mode: "off", allow: [] };
/** What a bad value in the settings file reads as: every agent pick refused. */
export const MODEL_POLICY_FAIL_CLOSED: ModelPolicy = { mode: "enforce", allow: [] };

/** A key nobody defined (a typo such as `effort`) is an error, never dropped. */
const REFUSE_UNKNOWN_KEYS = { onExcessProperty: "error" } as const;

const decodeStoredOption = Schema.decodeUnknownOption(ModelPolicyStored);

/**
 * The settings file's value. A bad one (a hand edit, an unknown key included) reads as enforce with
 * nothing allowed, with a warning (fail closed; the rest of the file still loads). The person's text
 * is kept in `unreadable` and written back as it was until they save the key.
 */
export const ModelPolicyFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ModelPolicy,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("ModelPolicyFromSettingsFile.decode")(function* (raw: unknown) {
        if (decodeStoredOption(raw, REFUSE_UNKNOWN_KEYS)._tag === "Some") {
          return raw as typeof ModelPolicy.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid modelPolicy read as enforce with nothing allowed; kept as written until you fix it in settings",
        );
        return { ...MODEL_POLICY_FAIL_CLOSED, unreadable: raw } as typeof ModelPolicy.Encoded;
      }),
      encode: (policy) => Effect.succeed("unreadable" in policy ? policy.unreadable : policy),
    }),
  ),
);

const ModelPolicyPatchRules = Schema.Struct(ModelPolicyRules);
const decodePatchRules = Schema.decodeUnknownEffect(ModelPolicyPatchRules);

/**
 * A save's value, replaced whole: both `mode` and `allow` are required (a save that leaves one out
 * is refused, never filled in with off or no rows), and an unknown key is refused, not dropped.
 */
export const ModelPolicyPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    ModelPolicyPatchRules,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        decodePatchRules(raw, REFUSE_UNKNOWN_KEYS).pipe(
          Effect.mapError((error) => error.issue),
          Effect.as(raw as typeof ModelPolicyPatchRules.Encoded),
        ),
      encode: (policy) => Effect.succeed(policy as unknown),
    }),
  ),
);
export type ModelPolicyPatch = typeof ModelPolicyPatch.Type;

/**
 * The fork's start rows: the rows Settings offers while none are saved, for the owner to
 * change. Not the setting's default: a file without rows keeps meaning no rows, as the writer saves
 * `allow: []` by leaving it out (a default here would widen such a file on update).
 */
export const MODEL_POLICY_START_ROWS: ReadonlyArray<ModelPolicyRow> = [
  {
    driver: ProviderDriverKind.make("claudeAgent"),
    model: "claude-opus-5-5",
    efforts: ["medium", "high", "xhigh"],
    defaultEffort: "high",
    useFor: "most work: building, routine work, review lenses",
  },
  {
    driver: ProviderDriverKind.make("claudeAgent"),
    model: "claude-fable-5-2",
    efforts: ["medium", "high", "xhigh"],
    defaultEffort: "high",
    useFor:
      "creative solutions and direction checks: design options, stuck audits, root causes others missed; it costs more, so not routine work",
  },
];

/**
 * The option ids a model's effort control goes by: the same list as the usage pacing gate
 * (`apps/server/src/usage/UsagePacingGate.ts`; a server test keeps the two in step).
 */
export const MODEL_POLICY_EFFORT_OPTION_IDS: ReadonlyArray<string> = [
  "effort",
  "reasoningEffort",
  "reasoning_effort",
  "reasoning",
  "thinking_level",
];
/**
 * The option id a built-in driver reads its reasoning level from, where it is not in the list
 * above: the model guard's `DRIVER_EFFORT_IDS` (the guard's own test keeps the two in step).
 */
const MODEL_POLICY_DRIVER_EFFORT_IDS: Readonly<Record<string, string>> = {
  opencode: "variant",
  pi: "thinking",
};
/** The ultracode option, and the effort name the policy gives it: it runs at xhigh in ultracode. */
const MODEL_POLICY_ULTRACODE = "ultracode";

/** One pick of a model, as a call or a setting names it. */
export interface ModelPolicyPick {
  /** The provider driver; `undefined` when it is not known (a task sent to a linked machine). */
  readonly driver?: string | undefined;
  /** The model id; `undefined` when the call names none. */
  readonly model: string | undefined;
  readonly options?: ReadonlyArray<{ readonly id: string; readonly value: unknown }> | undefined;
  /** More option ids read as the effort (a select the model marks as its reasoning control). */
  readonly effortOptionIds?: ReadonlyArray<string> | undefined;
}

/**
 * The efforts a pick runs at: the value of every option with an effort id (a decoy id must not hide
 * the one the driver reads; without a known driver every driver's id counts), plus `ultracode` while
 * that option is on. `xhigh` with ultracode reads as `ultracode` alone; the old effort value
 * `ultracode` means xhigh with the option on, unless the call gives the option.
 */
export function modelPolicyPickEfforts(pick: ModelPolicyPick): ReadonlyArray<string> {
  const options = pick.options ?? [];
  const driverIds =
    pick.driver === undefined
      ? Object.values(MODEL_POLICY_DRIVER_EFFORT_IDS)
      : Object.hasOwn(MODEL_POLICY_DRIVER_EFFORT_IDS, pick.driver)
        ? [MODEL_POLICY_DRIVER_EFFORT_IDS[pick.driver]!]
        : [];
  const effortIds = new Set([
    ...MODEL_POLICY_EFFORT_OPTION_IDS,
    ...driverIds,
    ...(pick.effortOptionIds ?? []),
  ]);
  const named = [
    ...new Set(
      options.flatMap((option) =>
        effortIds.has(option.id) && typeof option.value === "string" ? [option.value] : [],
      ),
    ),
  ];
  const ultracodeOption = options.find((option) => option.id === MODEL_POLICY_ULTRACODE);
  const legacy = named.includes(MODEL_POLICY_ULTRACODE);
  const ultracode = ultracodeOption === undefined ? legacy : ultracodeOption.value !== false;
  const efforts = [
    ...new Set(named.map((effort) => (effort === MODEL_POLICY_ULTRACODE ? "xhigh" : effort))),
  ];
  if (!ultracode) return efforts;
  return [MODEL_POLICY_ULTRACODE, ...efforts.filter((effort) => effort !== "xhigh")];
}

const rowAllows = (row: ModelPolicyRow, efforts: ReadonlyArray<string>) =>
  row.efforts === undefined
    ? efforts.length === 0
    : efforts.length > 0 && efforts.every((effort) => row.efforts!.includes(effort));

export type ModelPolicyVerdict =
  | { readonly inside: true; readonly row: ModelPolicyRow }
  | { readonly inside: false; readonly why: string };

/**
 * Whether one pick is inside the rows. A pick that names no model is outside, and so is a
 * model in no row. A row with efforts needs the pick to name one of them; a row without
 * efforts allows none. Without a driver, every row that names the model must allow the pick.
 */
export function checkModelPolicyPick(
  allow: ReadonlyArray<ModelPolicyRow>,
  pick: ModelPolicyPick,
): ModelPolicyVerdict {
  if (pick.model === undefined) {
    return { inside: false, why: "the call names no model" };
  }
  const model = pick.model;
  const rows = allow.filter(
    (row) => row.model === model && (pick.driver === undefined || row.driver === pick.driver),
  );
  if (rows.length === 0) {
    return {
      inside: false,
      why: `${model}${pick.driver === undefined ? "" : ` (${pick.driver})`} is in no row`,
    };
  }
  const efforts = modelPolicyPickEfforts(pick);
  const allowed = rows.filter((row) => rowAllows(row, efforts));
  const row = allowed[0];
  if (row !== undefined && (pick.driver !== undefined || allowed.length === rows.length)) {
    return { inside: true, row };
  }
  return {
    inside: false,
    why:
      efforts.length === 0
        ? `${model} needs an effort from its row, and the pick names none`
        : `${model} does not allow effort ${efforts.join(" + ")}`,
  };
}

/** One row as the refusal lists it. */
function modelPolicyRowText(row: ModelPolicyRow): string {
  const efforts =
    row.efforts === undefined
      ? "no effort"
      : `efforts ${row.efforts.join(", ")}${
          row.defaultEffort === undefined ? "" : `; default ${row.defaultEffort}`
        }`;
  return `${row.driver} ${row.model} (${efforts})`;
}

/** The allowed rows as one line, for a refusal or a note. */
export function modelPolicyRowsText(allow: ReadonlyArray<ModelPolicyRow>): string {
  return allow.length === 0
    ? "none: the policy has no rows, so every agent pick is outside it"
    : allow.map(modelPolicyRowText).join("; ");
}

/**
 * How the policy reads for a check: its mode, or, for a value the settings file holds that cannot
 * be read, `unreadable`. That one is Enforce (every agent pick refused), unless the file's own text
 * names the mode `advise`: then it is only a warning.
 */
export type ModelPolicyStance =
  | { readonly mode: "off" }
  | {
      readonly mode: "advise" | "enforce";
      readonly allow: ReadonlyArray<ModelPolicyRow>;
      readonly unreadable: false;
    }
  | { readonly mode: "advise" | "enforce"; readonly unreadable: true };

export function modelPolicyStance(policy: ModelPolicy): ModelPolicyStance {
  if ("unreadable" in policy) {
    const raw = policy.unreadable;
    const fileMode =
      typeof raw === "object" && raw !== null && !Array.isArray(raw)
        ? (raw as Record<string, unknown>).mode
        : undefined;
    return { mode: fileMode === "advise" ? "advise" : "enforce", unreadable: true };
  }
  if (policy.mode === "off") return { mode: "off" };
  return { mode: policy.mode, allow: policy.allow, unreadable: false };
}
