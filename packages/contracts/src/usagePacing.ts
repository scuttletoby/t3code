/**
 * Fork: usage pacing settings. How agents pace one provider instance's usage limits, what the
 * pacing gate does when an agent delegates a task, and what effort levels it may lower to. Wired
 * into the server settings only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString } from "./baseSchemas.ts";
import { ProviderInstanceId } from "./providerInstance.ts";
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
            "settings: usagePacing burnFirstWindows entries that are not window kinds are ignored; those windows keep their even pace, and the next settings save writes the cleaned list back to the file",
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

const USAGE_PACING_INSTANCE_KEYS: ReadonlyArray<string> = Object.keys(
  UsagePacingInstanceSettings.fields,
);

const isPlainRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * Fork: `usagePacing` as the settings file holds it, by provider instance id. A key an entry does
 * not know (a typo such as `burnFirstWindow`) is dropped like any unknown settings key, but with a
 * warning naming it: with `burnFirst` on, the dropped list means every window burns first.
 */
export const UsagePacingFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.Unknown,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("UsagePacingFromSettingsFile.decode")(function* (raw: unknown) {
        const unknownKeys: Record<string, ReadonlyArray<string>> = {};
        for (const [instanceId, entry] of Object.entries(isPlainRecord(raw) ? raw : {})) {
          if (!isPlainRecord(entry)) continue;
          const keys = Object.keys(entry).filter(
            (key) => !USAGE_PACING_INSTANCE_KEYS.includes(key),
          );
          if (keys.length > 0) unknownKeys[instanceId] = keys;
        }
        if (Object.keys(unknownKeys).length > 0) {
          yield* Effect.logWarning(
            "settings: usagePacing keys that are not pacing settings are ignored, and the next settings save removes them from the file",
            { unknownKeys, keys: USAGE_PACING_INSTANCE_KEYS },
          );
        }
        return raw;
      }),
      encode: Effect.succeed,
    }),
  ),
  Schema.decodeTo(Schema.Record(ProviderInstanceId, UsagePacingInstanceSettings)),
);

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

/**
 * Fork: the accounts new delegated work moves through, in order, when the account it would
 * run on is spent. Empty (the default) moves nothing. A save is strict: an id that is not an
 * instance id, or one listed twice, is refused.
 */
export const AccountOrder = Schema.Array(ProviderInstanceId).check(
  Schema.makeFilter((ids) => new Set(ids).size === ids.length, {
    expected: "each account listed once",
  }),
);
export type AccountOrder = typeof AccountOrder.Type;

const isProviderInstanceId = Schema.is(ProviderInstanceId);

/**
 * `accountOrder` as the settings file holds it, read leniently with a warning: an entry that is not
 * an instance id, or a repeat, is dropped (no work moves to it), and a value that is not a list
 * reads as empty (nothing moves), so a typo never moves work and the rest of the file still loads.
 */
export const AccountOrderFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    Schema.toType(AccountOrder),
    SchemaTransformation.transformEffect({
      decode: Effect.fn("AccountOrder.decode")(function* (raw: unknown) {
        const listed = Array.isArray(raw) ? raw : [];
        const ids = [...new Set(listed.filter(isProviderInstanceId))];
        const dropped = Array.isArray(raw) ? listed.length - ids.length : 1;
        if (dropped > 0) {
          yield* Effect.logWarning(
            "settings: accountOrder entries that are not provider instance ids, or repeats, are ignored; no work moves to them, and the next settings save writes the cleaned list back to the file",
            { value: raw },
          );
        }
        return ids as AccountOrder;
      }),
      encode: (ids) => Effect.succeed(ids as unknown),
    }),
  ),
);
