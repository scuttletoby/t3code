/**
 * Fork: the model policy, which models (and efforts) agents may pick, per provider driver. Only the
 * setting lives here; nothing reads it yet. Wired into the server settings only through
 * forkSettings.ts.
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
