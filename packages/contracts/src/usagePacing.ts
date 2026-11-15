/**
 * Fork: usage pacing settings. How agents pace one provider instance's usage limits, what the
 * pacing gate does when an agent delegates a task, and what effort levels it may lower to. Wired
 * into the server settings only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";

import { TrimmedNonEmptyString } from "./baseSchemas.ts";

const UsagePacingReservePercent = Schema.Number.check(
  Schema.isBetween({ minimum: 0, maximum: 100 }),
);

/** Fork: how agents pace one provider instance's usage limits. The defaults change nothing. */
export const UsagePacingInstanceSettings = Schema.Struct({
  /** Points of each window kept free for the owner, released over the window's last 12 hours. */
  reservePercent: UsagePacingReservePercent.pipe(Schema.withDecodingDefault(Effect.succeed(0))),
  /** Spend this instance first: no even pace, only the reserve holds agents back. */
  burnFirst: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
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
  noLimits: Schema.optionalKey(Schema.Boolean),
});
export type UsagePacingInstanceSettingsPatch = typeof UsagePacingInstanceSettingsPatch.Type;

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
