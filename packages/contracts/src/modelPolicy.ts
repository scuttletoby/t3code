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

/** Strict: a save refuses a bad value. Both fields default, as the writer saves sparse. */
export const ModelPolicy = Schema.Struct({
  mode: ModelPolicyMode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  allow: Schema.Array(ModelPolicyRow).pipe(Schema.withDecodingDefault(Effect.succeed([]))),
});
export type ModelPolicy = typeof ModelPolicy.Type;

export const DEFAULT_MODEL_POLICY: ModelPolicy = { mode: "off", allow: [] };
/** What a bad value in the settings file reads as: every agent pick refused. */
export const MODEL_POLICY_FAIL_CLOSED: ModelPolicy = { mode: "enforce", allow: [] };

const decodeModelPolicyOption = Schema.decodeUnknownOption(ModelPolicy);

/**
 * The settings file's value. A bad one (a hand edit) reads as enforce with nothing allowed, with a
 * warning (fail closed; the rest of the file still loads); the next save writes that in its place.
 */
export const ModelPolicyFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ModelPolicy,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("ModelPolicyFromSettingsFile.decode")(function* (raw: unknown) {
        if (decodeModelPolicyOption(raw)._tag === "Some") {
          return raw as typeof ModelPolicy.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid modelPolicy read as enforce with nothing allowed; fix it in settings",
        );
        return MODEL_POLICY_FAIL_CLOSED as typeof ModelPolicy.Encoded;
      }),
      encode: (policy) => Effect.succeed(policy as unknown),
    }),
  ),
);
