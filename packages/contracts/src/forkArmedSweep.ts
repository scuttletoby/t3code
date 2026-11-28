/**
 * Fork (issue 880): the `armedSweep` settings block. The armed PR sweep lists every open pull
 * request with auto-merge on in the repositories T3 knows, judges each and wakes the thread that
 * owns it when it is red, conflicted, turned off, closed or stalled. `off` by default; `dry` logs and
 * lists (`wait_list`) what it would send and sends nothing. Docs: docs/fork/README.md.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { NonNegativeInt } from "./baseSchemas.ts";

export const ArmedSweepMode = Schema.Literals(["off", "dry", "on"]);
export type ArmedSweepMode = typeof ArmedSweepMode.Type;

/** `linked`: only pull requests a thread links; `all`: also armed by hand (the project's orchestrator). */
export const ArmedSweepScope = Schema.Literals(["linked", "all"]);
export type ArmedSweepScope = typeof ArmedSweepScope.Type;

/** The sweep never runs more often than this, whatever the file says. */
const ARMED_SWEEP_MIN_INTERVAL_SECONDS = 60;

const DEFAULTS = {
  mode: "off",
  scope: "linked",
  /** 5 minutes: a red seen right after a sweep still wakes within 10 (issue 880's goal). */
  intervalSeconds: 300,
  /** Not green this long (checks pending or missing) counts as stalled. */
  stallMinutes: 30,
  /**
   * A red waits this long after its last failed check ends, so the fork-ci re-run (every 10
   * minutes) has had its one try (issue 160); a re-run starts checks again, which is not red.
   */
  rerunGraceMinutes: 12,
  /** At most this many wake-ups per pull request head, whatever the reasons. */
  maxWakesPerHead: 3,
} as const satisfies Record<string, unknown>;

export const ArmedSweepSettings = Schema.Struct({
  mode: ArmedSweepMode.pipe(Schema.withDecodingDefault(Effect.succeed(DEFAULTS.mode))),
  scope: ArmedSweepScope.pipe(Schema.withDecodingDefault(Effect.succeed(DEFAULTS.scope))),
  intervalSeconds: NonNegativeInt.pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULTS.intervalSeconds)),
  ),
  stallMinutes: NonNegativeInt.pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULTS.stallMinutes)),
  ),
  rerunGraceMinutes: NonNegativeInt.pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULTS.rerunGraceMinutes)),
  ),
  maxWakesPerHead: NonNegativeInt.pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULTS.maxWakesPerHead)),
  ),
});
export type ArmedSweepSettings = typeof ArmedSweepSettings.Type;

export const DEFAULT_ARMED_SWEEP_SETTINGS: ArmedSweepSettings = { ...DEFAULTS };

type NumberKey = "intervalSeconds" | "stallMinutes" | "rerunGraceMinutes" | "maxWakesPerHead";

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/**
 * The settings file's `armedSweep`, read leniently: a mode other than `dry` or `on` reads as off,
 * a scope other than `all` as `linked`, a bad number as its default, an interval below the floor as
 * the floor.
 */
export const ArmedSweepSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ArmedSweepSettings,
    SchemaTransformation.transform({
      decode: (raw: unknown): typeof ArmedSweepSettings.Encoded => {
        const record =
          typeof raw === "object" && raw !== null ? (raw as Record<string, unknown>) : {};
        const count = (key: NumberKey): number => {
          const value = record[key];
          return isCount(value) ? value : DEFAULTS[key];
        };
        return {
          mode: record.mode === "dry" || record.mode === "on" ? record.mode : "off",
          scope: record.scope === "all" ? "all" : "linked",
          intervalSeconds: Math.max(ARMED_SWEEP_MIN_INTERVAL_SECONDS, count("intervalSeconds")),
          stallMinutes: count("stallMinutes"),
          rerunGraceMinutes: count("rerunGraceMinutes"),
          maxWakesPerHead: count("maxWakesPerHead"),
        };
      },
      encode: (settings: typeof ArmedSweepSettings.Encoded): unknown => settings,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed(DEFAULT_ARMED_SWEEP_SETTINGS)),
);

/** A save's change: strict, so a bad value is refused; a field left out keeps its value. */
export const ArmedSweepSettingsPatch = Schema.Struct({
  mode: Schema.optionalKey(ArmedSweepMode),
  scope: Schema.optionalKey(ArmedSweepScope),
  intervalSeconds: Schema.optionalKey(
    Schema.Int.check(Schema.isGreaterThanOrEqualTo(ARMED_SWEEP_MIN_INTERVAL_SECONDS)),
  ),
  stallMinutes: Schema.optionalKey(NonNegativeInt),
  rerunGraceMinutes: Schema.optionalKey(NonNegativeInt),
  maxWakesPerHead: Schema.optionalKey(NonNegativeInt),
});
export type ArmedSweepSettingsPatch = typeof ArmedSweepSettingsPatch.Type;
