/**
 * Fork: usage pacing settings. How agents pace one provider instance's usage limits, what the
 * pacing gate does when an agent delegates a task, and what effort levels it may lower to. Wired
 * into the server settings only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString } from "./baseSchemas.ts";
import { ServerProviderUsageWindow } from "./providerUsageLimits.ts";

const UsagePacingReservePercent = Schema.Number.check(
  Schema.isBetween({ minimum: 0, maximum: 100 }),
);

/** Fork: a usage window's kind (upstream's `ServerProviderUsageWindow.kind`). */
export const UsagePacingWindowKind = ServerProviderUsageWindow.fields.kind;
export type UsagePacingWindowKind = typeof UsagePacingWindowKind.Type;

/** Every window kind, in upstream's order: what `burnFirst` without a list spends first. */
const USAGE_PACING_WINDOW_KINDS: ReadonlyArray<UsagePacingWindowKind> =
  UsagePacingWindowKind.literals;

const isWindowKind = (value: unknown): value is UsagePacingWindowKind =>
  (USAGE_PACING_WINDOW_KINDS as ReadonlyArray<unknown>).includes(value);

/** Fork: the window kinds `burnFirst` applies to, as a save gives them (strict: unknown refused). */
export const UsagePacingBurnFirstWindows = Schema.Array(UsagePacingWindowKind);
export type UsagePacingBurnFirstWindows = typeof UsagePacingBurnFirstWindows.Type;

/**
 * The list as the settings file holds it, read leniently: an unknown name is dropped (that window
 * keeps its even pace) and a value that is not a list reads as empty (nothing burns first), both
 * with a warning, so a typo never spends faster and the rest of the file still loads.
 */
const UsagePacingBurnFirstWindowsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.toType(UsagePacingBurnFirstWindows),
    SchemaTransformation.transformEffect({
      decode: Effect.fn("UsagePacingBurnFirstWindows.decode")(function* (raw: unknown) {
        const listed = Array.isArray(raw) ? raw : [];
        const kinds = [...new Set(listed.filter(isWindowKind))];
        const dropped = Array.isArray(raw) ? listed.filter((name) => !isWindowKind(name)) : [raw];
        if (dropped.length > 0) {
          yield* Effect.logWarning(
            "settings: usagePacing burnFirstWindows entries that are not window kinds are ignored; those windows keep their even pace",
            { dropped, kinds: USAGE_PACING_WINDOW_KINDS },
          );
        }
        return kinds as UsagePacingBurnFirstWindows;
      }),
      encode: (kinds) => Effect.succeed(kinds as unknown),
    }),
  ),
);

/** Fork: how agents pace one provider instance's usage limits. The defaults change nothing. */
export const UsagePacingInstanceSettings = Schema.Struct({
  /** Points of each window kept free for the owner, released over the window's last 12 hours. */
  reservePercent: UsagePacingReservePercent.pipe(Schema.withDecodingDefault(Effect.succeed(0))),
  /** Spend this instance first: no even pace, only the reserve holds agents back. */
  burnFirst: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /**
   * The window kinds `burnFirst` applies to (`["weekly", "monthly"]`: 5-hour windows keep the even
   * pace). Absent: every kind, as before the list existed. Nothing without `burnFirst`.
   */
  burnFirstWindows: Schema.optionalKey(UsagePacingBurnFirstWindowsFromSettingsFile),
  /**
   * The owner says this instance has no usage limits (an API key, pay per use). The only source
   * of "no limits": no reading ever means it, since upstream's `unsupported` means "cannot read".
   */
  noLimits: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
});
export type UsagePacingInstanceSettings = typeof UsagePacingInstanceSettings.Type;

export const DEFAULT_USAGE_PACING_INSTANCE_SETTINGS: UsagePacingInstanceSettings = {
  reservePercent: 0,
  burnFirst: false,
  noLimits: false,
};

/** Fork: a patch of one instance's pacing; a field left out keeps its stored value. */
export const UsagePacingInstanceSettingsPatch = Schema.Struct({
  reservePercent: Schema.optionalKey(UsagePacingReservePercent),
  burnFirst: Schema.optionalKey(Schema.Boolean),
  /** Strict: a save refuses a name that is not a window kind. Every kind is the same as none. */
  burnFirstWindows: Schema.optionalKey(UsagePacingBurnFirstWindows),
  noLimits: Schema.optionalKey(Schema.Boolean),
});
export type UsagePacingInstanceSettingsPatch = typeof UsagePacingInstanceSettingsPatch.Type;

/** The window kinds this instance spends first: none without `burnFirst`, every kind without a list. */
export function burnFirstWindowKinds(
  pacing: Pick<UsagePacingInstanceSettings, "burnFirst" | "burnFirstWindows">,
): ReadonlyArray<UsagePacingWindowKind> {
  if (!pacing.burnFirst) return [];
  return pacing.burnFirstWindows ?? USAGE_PACING_WINDOW_KINDS;
}

/**
 * Fork: what the usage pacing gate does when an agent delegates a task. `off` (the default) changes
 * nothing and reads no pace. `advise` never changes or refuses a task, it only adds a note.
 * `enforce` moves a task off an account that is ahead of pace, lowers its effort or holds it.
 */
export const UsagePacingGateMode = Schema.Literals(["off", "advise", "enforce"]);
export type UsagePacingGateMode = typeof UsagePacingGateMode.Type;

/**
 * Fork: what each effort level turns into while an account is ahead of its even pace and no other
 * account has room, by level id (`{ "high": "medium", "medium": "medium" }`). A level that is not
 * listed waits instead. Empty (the default) never lowers an effort.
 */
export const UsagePacingLowerEffort = Schema.Record(TrimmedNonEmptyString, TrimmedNonEmptyString);
export type UsagePacingLowerEffort = typeof UsagePacingLowerEffort.Type;
