/**
 * Fork: review rules, how this machine runs a review lens; the repo's config says what a pull
 * request needs and its CI gate enforces it. Off by default; `{"preset": "recommended"}` turns the
 * fork's defaults on. The `lens_start` tool reads it; wired in only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Equal from "effect/Equal";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString, TrimmedString } from "./baseSchemas.ts";
import { ProviderDriverKind } from "./providerInstance.ts";

/** `off`: nothing is listed, changed or checked. `recommended`: {@link REVIEW_RULES_RECOMMENDED}. */
export const ReviewRulesPreset = Schema.Literals(["off", "recommended"]);
export type ReviewRulesPreset = typeof ReviewRulesPreset.Type;

/**
 * A lens name as the repo's config writes it (`review`, `approach`, `security`). Not a name every
 * object already has (`constructor`): looked up by name, it would find a function, not a runner.
 */
export const ReviewLensName = Schema.String.check(
  Schema.isPattern(/^[a-z][a-z0-9-]{0,39}$/u),
  Schema.makeFilter((name: string) => !(name in Object.prototype), {
    expected: "a lens name that is not a built-in object key such as `constructor`",
  }),
);
export type ReviewLensName = typeof ReviewLensName.Type;

/** Who runs a lens: a driver kind and a model, not a provider instance (ids differ per machine). */
export const ReviewRunner = Schema.Struct({
  driver: ProviderDriverKind,
  model: TrimmedNonEmptyString,
  effort: TrimmedNonEmptyString,
});
export type ReviewRunner = typeof ReviewRunner.Type;

/** Small findings: fixed in the pull request or dropped, filed as issues, or free. */
export const ReviewMinors = Schema.Literals(["same-round", "issue", "free"]);
export type ReviewMinors = typeof ReviewMinors.Type;

/** Not absolute (`/…`, `\…`, `C:…`), and no `..` part: it must stay inside the repo. */
const isRepoRelativePath = (path: string): boolean =>
  !/^[\\/]/u.test(path) &&
  !/^[A-Za-z]:/u.test(path) &&
  !path.split(/[\\/]/u).includes("..") &&
  !/[\u0000-\u001f\u007f]/u.test(path);

const ReviewRulesFields = {
  preset: ReviewRulesPreset,
  runner: Schema.optionalKey(ReviewRunner),
  /** A lens without an entry uses `runner`. */
  lensRunners: Schema.optionalKey(Schema.Record(ReviewLensName, ReviewRunner)),
  stateQuestion: Schema.optionalKey(
    TrimmedString.check(Schema.isMinLength(20), Schema.isMaxLength(600)),
  ),
  minors: Schema.optionalKey(ReviewMinors),
  /** The repo's config file, relative to the repo's root. */
  configPath: Schema.optionalKey(
    TrimmedNonEmptyString.check(
      Schema.isMaxLength(200),
      Schema.makeFilter(isRepoRelativePath, {
        expected: "a path inside the repo: not absolute, no `..`",
      }),
    ),
  ),
  /** The longest review report a lens may post. */
  reportMaxChars: Schema.optionalKey(
    Schema.Int.check(Schema.isBetween({ minimum: 1000, maximum: 65000 })),
  ),
};

/** As the settings file holds it: `preset` defaults, as the writer saves sparse (off left out). */
const ReviewRulesStored = Schema.Struct({
  ...ReviewRulesFields,
  preset: ReviewRulesPreset.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
});

/**
 * The setting as the server holds it. `unreadable` is set only when the file's value is bad: it is
 * the person's text, kept so a save of another setting writes it back unchanged (the setting then
 * reads as off). Saving the key itself replaces it.
 */
export const ReviewRules = Schema.Struct({
  ...ReviewRulesStored.fields,
  unreadable: Schema.optionalKey(Schema.Unknown),
});
export type ReviewRules = typeof ReviewRules.Type;

export const DEFAULT_REVIEW_RULES: ReviewRules = { preset: "off" };

/** Every field a preset fills in. */
export interface ReviewRulesValues {
  readonly runner: ReviewRunner;
  readonly lensRunners: Readonly<Record<ReviewLensName, ReviewRunner>>;
  readonly stateQuestion: string;
  readonly minors: ReviewMinors;
  readonly configPath: string;
  readonly reportMaxChars: number;
}

const CLAUDE = ProviderDriverKind.make("claudeAgent");

/**
 * The `recommended` preset. A fork update may change it (a reviewed pull request with a release
 * note); a machine that wants fixed values writes the fields out.
 */
export const REVIEW_RULES_RECOMMENDED: ReviewRulesValues = {
  runner: { driver: CLAUDE, model: "claude-opus-5-5", effort: "high" },
  // The "stuck in circles" audit, which only runs from round 3.
  lensRunners: { approach: { driver: CLAUDE, model: "claude-fable-5-2", effort: "high" } },
  stateQuestion:
    "What happens when this runs again, stops half-way, or meets existing, old or empty state?",
  // Small findings are fixed in the same pull request or dropped (the review kit's default).
  minors: "same-round",
  configPath: ".github/pr-queue.json",
  reportMaxChars: 60000,
};

/** The rules a lens runs with: off, or every field filled in. */
export type EffectiveReviewRules =
  | { readonly preset: "off" }
  | ({ readonly preset: "recommended" } & ReviewRulesValues);

/**
 * The rules for one project. A project override replaces the server's value whole (none: the
 * server's). Off, absent or unreadable is off; otherwise the preset's values with each field the
 * value writes out winning (`lensRunners` per lens).
 */
export function effectiveReviewRules(
  serverValue: ReviewRules | undefined,
  projectOverride?: ReviewRules | null,
): EffectiveReviewRules {
  const value = projectOverride ?? serverValue;
  if (value === undefined || value.preset !== "recommended" || "unreadable" in value) {
    return { preset: "off" };
  }
  const preset = REVIEW_RULES_RECOMMENDED;
  // No prototype: a lens without an entry finds nothing, whatever its name.
  const lensRunners: Record<ReviewLensName, ReviewRunner> = Object.assign(
    Object.create(null) as Record<ReviewLensName, ReviewRunner>,
    preset.lensRunners,
    value.lensRunners,
  );
  return {
    preset: "recommended",
    runner: value.runner ?? preset.runner,
    lensRunners,
    stateQuestion: value.stateQuestion ?? preset.stateQuestion,
    minors: value.minors ?? preset.minors,
    configPath: value.configPath ?? preset.configPath,
    reportMaxChars: value.reportMaxChars ?? preset.reportMaxChars,
  };
}

/** Who runs `lens`: its own entry in `lensRunners`, else `runner`. Never a prototype's key. */
export function reviewLensRunner(
  rules: Extract<EffectiveReviewRules, { readonly preset: "recommended" }>,
  lens: string,
): ReviewRunner {
  return (
    (Object.hasOwn(rules.lensRunners, lens) ? rules.lensRunners[lens] : undefined) ?? rules.runner
  );
}

/** A key nobody defined (a typo, or a field from a newer build) is an error, never dropped. */
const REFUSE_UNKNOWN_KEYS = { onExcessProperty: "error" } as const;

const decodeStoredOption = Schema.decodeUnknownOption(ReviewRulesStored);
const isStoredValue = (raw: unknown): boolean =>
  decodeStoredOption(raw, REFUSE_UNKNOWN_KEYS)._tag === "Some";

/**
 * The settings file's value. A bad one (a hand edit, an unknown preset or key from a newer build, a
 * bad runner) reads as off with a warning, never half on; the rest of the file still loads. The
 * person's text is kept in `unreadable` and written back as it was until they save the key.
 */
export const ReviewRulesFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    ReviewRules,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("ReviewRulesFromSettingsFile.decode")(function* (raw: unknown) {
        if (isStoredValue(raw)) return raw as typeof ReviewRules.Encoded;
        yield* Effect.logWarning(
          "settings: invalid reviewRules read as off; kept as written until you fix it in settings",
        );
        return { preset: "off", unreadable: raw } as typeof ReviewRules.Encoded;
      }),
      encode: (rules) => Effect.succeed("unreadable" in rules ? rules.unreadable : rules),
    }),
  ),
);

const ReviewRulesPatchRules = Schema.Struct(ReviewRulesFields);
const decodePatchRules = Schema.decodeUnknownEffect(ReviewRulesPatchRules);

/**
 * A save's value, replaced whole: `preset` is required (a save that leaves it out is refused, never
 * read as off), and an unknown key is refused, not dropped.
 */
export const ReviewRulesPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    ReviewRulesPatchRules,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        decodePatchRules(raw, REFUSE_UNKNOWN_KEYS).pipe(
          Effect.mapError((error) => error.issue),
          Effect.as(raw as typeof ReviewRulesPatchRules.Encoded),
        ),
      encode: (rules) => Effect.succeed(rules as unknown),
    }),
  ),
);
export type ReviewRulesPatch = typeof ReviewRulesPatch.Type;

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/**
 * A project's unreadable value as a client holds it (off, the text kept) and sends it back. The
 * server also takes it only when its text is the one stored for that project
 * ({@link isKeptReviewRulesUnchanged}), so a save never brings text in.
 */
const isKeptUnreadable = (raw: unknown): boolean =>
  isRecord(raw) &&
  Object.hasOwn(raw, "unreadable") &&
  Object.keys(raw).every((key) => key === "unreadable" || key === "preset") &&
  (raw.preset === undefined || raw.preset === "off") &&
  !isStoredValue(raw.unreadable);

/**
 * A project's value in a save. Strict, as {@link ReviewRulesPatch}, but clients resend a project's
 * whole entry when they save its other settings, so a kept unreadable value passes back unchanged
 * (only one that still reads as off: a save cannot turn rules on through it).
 */
export const ReviewRulesProjectPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    ReviewRules,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        isKeptUnreadable(raw)
          ? Effect.succeed(raw as typeof ReviewRules.Encoded)
          : decodePatchRules(raw, REFUSE_UNKNOWN_KEYS).pipe(
              Effect.mapError((error) => error.issue),
              Effect.as(raw as typeof ReviewRules.Encoded),
            ),
      encode: (rules) => Effect.succeed(rules as unknown),
    }),
  ),
);

/**
 * Whether a project's value in a save is either readable or the very unreadable text stored for
 * that project. Kept text that differs (another text, one for a project that stores none, a stale
 * client's after the person fixed it elsewhere) could read as on in a later build that knows its
 * keys, and has no size cap; the server refuses it (forkSettingsPatch.ts).
 */
export function isKeptReviewRulesUnchanged(
  stored: ReviewRules | null | undefined,
  next: ReviewRules | null | undefined,
): boolean {
  if (next == null || !Object.hasOwn(next, "unreadable")) return true;
  return (
    stored != null &&
    Object.hasOwn(stored, "unreadable") &&
    Equal.equals(stored.unreadable, next.unreadable)
  );
}
