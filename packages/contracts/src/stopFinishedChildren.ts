/**
 * Fork (issue 925): the `stopFinishedChildren` settings block. `enabled` stops a finished delegated
 * child's provider session once its thread has been idle for `graceSeconds`; off by default, so a
 * finished child keeps its process until upstream's own idle release. Docs: docs/fork/README.md.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { NonNegativeInt } from "./baseSchemas.ts";

const DEFAULT_STOP_FINISHED_CHILDREN_GRACE_SECONDS = 60;

export const StopFinishedChildrenSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /** How long the finished child stays idle before its session is stopped. */
  graceSeconds: NonNegativeInt.pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULT_STOP_FINISHED_CHILDREN_GRACE_SECONDS)),
  ),
});
export type StopFinishedChildrenSettings = typeof StopFinishedChildrenSettings.Type;

const DEFAULT_STOP_FINISHED_CHILDREN: StopFinishedChildrenSettings = {
  enabled: false,
  graceSeconds: DEFAULT_STOP_FINISHED_CHILDREN_GRACE_SECONDS,
};

const isGraceSeconds = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/**
 * The settings file's `stopFinishedChildren`, read leniently: anything but `enabled: true` reads as
 * off, and a bad `graceSeconds` as the default.
 */
export const StopFinishedChildrenSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    StopFinishedChildrenSettings,
    SchemaTransformation.transform({
      decode: (raw: unknown): typeof StopFinishedChildrenSettings.Encoded => {
        const record =
          typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
        return {
          enabled: record.enabled === true,
          graceSeconds: isGraceSeconds(record.graceSeconds)
            ? record.graceSeconds
            : DEFAULT_STOP_FINISHED_CHILDREN_GRACE_SECONDS,
        };
      },
      encode: (settings: typeof StopFinishedChildrenSettings.Encoded): unknown => settings,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed(DEFAULT_STOP_FINISHED_CHILDREN)),
);

/** A save's change: strict, so a bad value is refused; a field left out keeps its value. */
export const StopFinishedChildrenSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
  graceSeconds: Schema.optionalKey(NonNegativeInt),
});
export type StopFinishedChildrenSettingsPatch = typeof StopFinishedChildrenSettingsPatch.Type;
