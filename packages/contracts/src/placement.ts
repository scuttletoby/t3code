/**
 * Fork (placement row P2): settings only. `placement`: what this machine has and how it places work
 * on linked machines; `placementNeeds`: what a project's work needs. Off by default, person-only,
 * wired in only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Equal from "effect/Equal";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { EnvironmentId, TrimmedString } from "./baseSchemas.ts";
import { isAbsoluteProgram, isPlaytestProgramFullyQualified } from "./playtest.ts";

/** `off` places nothing (upstream's behaviour); `advise` only suggests; `enforce` decides. */
export const PlacementMode = Schema.Literals(["off", "advise", "enforce"]);
export type PlacementMode = typeof PlacementMode.Type;

/** How much work a linked machine gets: upstream's four load-balancing words. */
export const PlacementWeight = Schema.Literals(["prefer", "normal", "less", "manual"]);
export type PlacementWeight = typeof PlacementWeight.Type;

/** At most this many labels in one list (a machine's, a `never` group, a project's needs). */
const PLACEMENT_LABELS_MAX = 64;
/** At most this many characters in one label. */
const PLACEMENT_LABEL_MAX_LENGTH = 64;
/** At most this many `never` groups. */
const PLACEMENT_NEVER_GROUPS_MAX = 32;
/** At most this many linked machines with a weight. */
const PLACEMENT_WEIGHTS_MAX = 256;
/** At most this many characters in `factsCommand`. */
const FACTS_COMMAND_MAX_LENGTH = 1000;

/** A free word, as written ("gpu", "ci-runner"): no spaces, no control characters. */
const LABEL_PATTERN = new RegExp(`^[^\\s\\p{C}]{1,${PLACEMENT_LABEL_MAX_LENGTH}}$`, "u");

const canonicalLabel = (label: string): string => label.trim().toLowerCase();

/**
 * Labels' one stored and compared form: trimmed, lower-cased (no locale), blanks and repeats dropped.
 * Placement row P5's `canonicalLabels` (packages/shared/src/forkPlacement.ts) calls this one, so
 * there is one form. It does not normalise Unicode (NFC), so two spellings of `café` are two labels.
 */
export const canonicalPlacementLabels = (labels: ReadonlyArray<string>): ReadonlyArray<string> => [
  ...new Set(labels.map(canonicalLabel).filter((label) => label.length > 0)),
];

/**
 * One label: trimmed and lower-cased (`GPU` is `gpu`), then one word of at most
 * {@link PLACEMENT_LABEL_MAX_LENGTH} characters.
 */
export const PlacementLabel = Schema.String.pipe(
  Schema.decodeTo(
    Schema.String.check(
      Schema.makeFilter((label: string) => LABEL_PATTERN.test(label), {
        expected: `a label of 1 to ${PLACEMENT_LABEL_MAX_LENGTH} characters without spaces`,
      }),
    ),
    SchemaTransformation.transform({ decode: canonicalLabel, encode: (label) => label }),
  ),
);

/**
 * Labels in {@link canonicalPlacementLabels} form: each checked, a repeat dropped after
 * lower-casing (first one kept), at most 64 after that.
 */
export const PlacementLabels = Schema.Array(PlacementLabel).pipe(
  Schema.decodeTo(
    Schema.Array(Schema.String).check(Schema.isMaxLength(PLACEMENT_LABELS_MAX)),
    SchemaTransformation.transform({
      decode: canonicalPlacementLabels,
      encode: (labels) => labels,
    }),
  ),
);

/**
 * A `never` group: labels that must not meet on one machine; two or more after repeats are dropped
 * (`["GPU", "gpu"]` is one label, so not a group).
 */
export const PlacementNeverGroup = PlacementLabels.check(
  Schema.makeFilter((group: ReadonlyArray<string>) => group.length >= 2, {
    expected: "a never group of at least two different labels",
  }),
);

/** A newline, tab, NUL or other control character. */
const CONTROL_CHARACTER = /\p{Cc}/u;

/**
 * Empty (no command) or an absolute path on some OS (the settings do not know which one runs it;
 * {@link placementFactsCommandProblem} checks the server's). A relative path and a control
 * character are refused. It is one program's path, never a command line: spaces are part of it.
 */
export const PlacementFactsCommand = TrimmedString.check(
  Schema.isMaxLength(FACTS_COMMAND_MAX_LENGTH),
  Schema.makeFilter((command: string) => !CONTROL_CHARACTER.test(command), {
    expected: "a factsCommand without control characters",
  }),
  Schema.makeFilter((command: string) => command === "" || isAbsoluteProgram(command), {
    expected: "an empty factsCommand or an absolute path",
  }),
);

/** A linked machine's id as written: one word (a key with a space would be trimmed into another). */
const PlacementWeightKey = Schema.String.check(
  Schema.makeFilter((id: string) => /^\S+$/u.test(id), {
    expected: "a machine id without spaces",
  }),
).pipe(Schema.decodeTo(EnvironmentId));

const PlacementWeights = Schema.Record(PlacementWeightKey, PlacementWeight).check(
  Schema.makeFilter(
    (weights: { readonly [id: string]: unknown }) =>
      Object.keys(weights).length <= PLACEMENT_WEIGHTS_MAX,
    { expected: `at most ${PLACEMENT_WEIGHTS_MAX} machine weights` },
  ),
);

const PlacementNeverGroups = Schema.Array(PlacementNeverGroup).check(
  Schema.isMaxLength(PLACEMENT_NEVER_GROUPS_MAX),
);

const PlacementRules = {
  mode: PlacementMode,
  /** What this machine has. */
  labels: PlacementLabels,
  /**
   * A program asked when a placement needs this machine's facts: exit 0 = free for work, each
   * stdout line one more label; any other exit or a timeout = not available. Empty: none asked.
   */
  factsCommand: PlacementFactsCommand,
  /** Label groups that must not meet on one machine, e.g. `[["ci-runner", "owner-logins"]]`. */
  never: PlacementNeverGroups,
  /** Linked machine id to weight; a machine without one is `normal`. */
  weights: PlacementWeights,
};

/** The value as the settings file holds it: every field defaults, so `{}` (or no key) is off. */
const PlacementStored = Schema.Struct({
  mode: PlacementRules.mode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  labels: PlacementRules.labels.pipe(Schema.withDecodingDefault(Effect.succeed([]))),
  factsCommand: PlacementRules.factsCommand.pipe(Schema.withDecodingDefault(Effect.succeed(""))),
  never: PlacementRules.never.pipe(Schema.withDecodingDefault(Effect.succeed([]))),
  weights: PlacementRules.weights.pipe(Schema.withDecodingDefault(Effect.succeed({}))),
});

/**
 * The setting as the server holds it. `unreadable` and `problem` are set only when the file's value
 * is bad: the person's text, kept so a save of another setting writes it back unchanged, and why it
 * could not be read (for Settings to show). The rest is then {@link DEFAULT_PLACEMENT_SETTINGS}:
 * placement is off. A save of the key replaces it.
 */
export const PlacementSettings = Schema.Struct({
  ...PlacementStored.fields,
  unreadable: Schema.optionalKey(Schema.Unknown),
  problem: Schema.optionalKey(Schema.String),
});
export type PlacementSettings = typeof PlacementSettings.Type;

/** Off, no labels, no facts command, no never groups, no weights. */
export const DEFAULT_PLACEMENT_SETTINGS: PlacementSettings = Schema.decodeSync(PlacementSettings)(
  {},
);

/** A key nobody defined (a typo such as `label`) is an error, never dropped. */
const REFUSE_UNKNOWN_KEYS = { onExcessProperty: "error" } as const;

/** Why `decode` refuses `raw`, on one line (for the warning and Settings); none when it takes it. */
const problemFrom =
  (decode: (raw: unknown) => unknown) =>
  (raw: unknown): string | undefined => {
    try {
      decode(raw);
      return undefined;
    } catch (error) {
      const text = error instanceof Error ? error.message : String(error);
      return text.replaceAll(/\s+/gu, " ").trim().slice(0, 500) || "invalid value";
    }
  };

const decodeStoredSync = Schema.decodeUnknownSync(PlacementStored);
const problemOf = problemFrom((raw) => decodeStoredSync(raw, REFUSE_UNKNOWN_KEYS));

/**
 * The settings file's value. A bad one (a hand edit, an unknown key or a wrong type included) reads
 * as off, with a warning and the reason in `problem` (fail closed; the rest of the file still
 * loads). The person's text is kept in `unreadable` and written back as it was until they save the
 * key. Reading the written value again gives the same value.
 */
export const PlacementSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementSettings,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("PlacementSettingsFromSettingsFile.decode")(function* (raw: unknown) {
        const problem = problemOf(raw);
        if (problem === undefined) return raw as typeof PlacementSettings.Encoded;
        yield* Effect.logWarning(
          "settings: invalid placement read as off; kept as written until you fix it in settings",
          { problem },
        );
        return { unreadable: raw, problem } as typeof PlacementSettings.Encoded;
      }),
      encode: (settings) =>
        Effect.succeed("unreadable" in settings ? settings.unreadable : settings),
    }),
  ),
);

const PlacementPatchFields = Schema.Struct({
  mode: Schema.optionalKey(PlacementRules.mode),
  labels: Schema.optionalKey(PlacementRules.labels),
  factsCommand: Schema.optionalKey(PlacementRules.factsCommand),
  never: Schema.optionalKey(PlacementRules.never),
  weights: Schema.optionalKey(PlacementRules.weights),
} satisfies Record<keyof typeof PlacementStored.fields, unknown>);
const decodePatchFields = Schema.decodeUnknownEffect(PlacementPatchFields);

/**
 * A save's value: a field left out keeps its stored value (`[]`, `""` or `{}` clears one). Strict:
 * a bad field and an unknown key are refused, never dropped or read as off.
 */
export const PlacementSettingsPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementPatchFields,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        decodePatchFields(raw, REFUSE_UNKNOWN_KEYS).pipe(
          Effect.mapError((error) => error.issue),
          Effect.as(raw as typeof PlacementPatchFields.Encoded),
        ),
      encode: (patch) => Effect.succeed(patch as unknown),
    }),
  ),
);
export type PlacementSettingsPatch = typeof PlacementSettingsPatch.Type;

const encodeStoredSync = Schema.encodeSync(PlacementStored);

/**
 * Why this machine (`platform`, Node's `process.platform` name) cannot run `factsCommand`, or
 * `undefined`: it is not a full path on this OS (`C:\\…` on Linux or macOS, `/…` on Windows), by
 * playtest's check for its programs. The settings do not know the server's OS, so the server checks
 * it when it reads placement to place work ({@link placementOnPlatform}) and when a save sets it.
 */
export const placementFactsCommandProblem = (
  factsCommand: string,
  platform: string,
): string | undefined =>
  factsCommand === "" || isPlaytestProgramFullyQualified(factsCommand, platform)
    ? undefined
    : `factsCommand "${factsCommand}" is not a full path on this server's OS: give the program's full path (${platform === "win32" ? "C:\\… or \\\\server\\…" : "/…"})`;

/**
 * `placement` as this machine (`platform`) reads it: one whose `factsCommand` this OS cannot run
 * reads as off, kept as written with the reason in `problem` (fail closed, as for a bad value).
 * Anything else is returned as it is, so it runs again the same. A runner must still check the path
 * on its own host before it runs it, and treat a value it cannot run as "not available".
 */
export const placementOnPlatform = (
  placement: PlacementSettings,
  platform: string,
): PlacementSettings => {
  if (Object.hasOwn(placement, "unreadable")) return placement;
  const problem = placementFactsCommandProblem(placement.factsCommand, platform);
  if (problem === undefined) return placement;
  const { mode, labels, factsCommand, never, weights } = placement;
  const unreadable = encodeStoredSync({ mode, labels, factsCommand, never, weights });
  return { ...DEFAULT_PLACEMENT_SETTINGS, unreadable, problem };
};

/**
 * A `placementNeeds` value the file holds that is not valid: the person's text, kept so a save of
 * another setting writes it back unchanged, and why it could not be read (for Settings to show).
 */
const PlacementNeedsBroken = Schema.Struct({ unreadable: Schema.Unknown, problem: Schema.String });
export type PlacementNeedsBroken = typeof PlacementNeedsBroken.Type;

/**
 * Needs as the server holds them: the labels a machine must have, or a broken value that no machine
 * fits. Never a bare `[]` for a bad value: `[]` is "needs nothing", which every machine fits.
 */
export const PlacementNeeds = Schema.Union([PlacementLabels, PlacementNeedsBroken]);
export type PlacementNeeds = typeof PlacementNeeds.Type;

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

/** Whether `needs` is a kept bad value (no machine fits it) rather than labels. */
const isPlacementNeedsBroken = (needs: unknown): needs is PlacementNeedsBroken =>
  isRecord(needs) && Object.hasOwn(needs, "unreadable");

/**
 * What placing a project's work reads from its needs (the project's own, else the server's): the
 * labels a machine must have, or, for a broken value, the reason the work goes to no machine.
 */
export const readPlacementNeeds = (
  needs: PlacementNeeds,
):
  | { readonly fits: "labels"; readonly labels: ReadonlyArray<string> }
  | { readonly fits: "none"; readonly reason: string } =>
  isPlacementNeedsBroken(needs)
    ? {
        fits: "none",
        reason: `this project's placementNeeds setting is not valid: ${needs.problem}; fix it in Settings`,
      }
    : { fits: "labels", labels: needs };

const decodeLabelsSync = Schema.decodeUnknownSync(PlacementLabels);
const needsProblemOf = problemFrom(decodeLabelsSync);

/**
 * The settings file's `placementNeeds` (the server's, or a project's override): labels a machine
 * must have to take the work. A bad value fails closed: it reads as broken (the project's work goes
 * to no machine; never as no needs, never as the server's), with a warning and the reason in
 * `problem`. The person's text is kept and written back as it was until they save the key.
 */
export const PlacementNeedsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementNeeds,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("PlacementNeedsFromSettingsFile.decode")(function* (raw: unknown) {
        const problem = needsProblemOf(raw);
        if (problem === undefined) return raw as typeof PlacementNeeds.Encoded;
        yield* Effect.logWarning(
          "settings: invalid placementNeeds; that work goes to no machine until you fix it in settings",
          { problem },
        );
        return { unreadable: raw, problem } as typeof PlacementNeeds.Encoded;
      }),
      encode: (needs) =>
        Effect.succeed(isPlacementNeedsBroken(needs) ? needs.unreadable : (needs as unknown)),
    }),
  ),
);

const decodeLabelsEffect = Schema.decodeUnknownEffect(PlacementLabels);

/**
 * The server's `placementNeeds` in a save, replaced whole. Strict: only labels are taken (a bad
 * label, or a kept bad value sent back, is refused); a save of labels replaces a kept bad value.
 * Typed as {@link PlacementNeeds}, so a settings value fits a patch as every other setting's does.
 */
export const PlacementNeedsPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementNeeds,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        decodeLabelsEffect(raw).pipe(
          Effect.mapError((error) => error.issue),
          Effect.as(raw as typeof PlacementNeeds.Encoded),
        ),
      encode: (needs) => Effect.succeed(needs as unknown),
    }),
  ),
);
export type PlacementNeedsPatch = typeof PlacementNeedsPatch.Type;

/**
 * A project's kept bad value as a client sends it back with the project's other settings: only text
 * that still does not read as needs, so a save cannot set needs through it. The server also takes it
 * only when it is the text stored for that project ({@link isKeptPlacementNeedsUnchanged}).
 */
const isKeptBrokenNeeds = (raw: unknown): raw is { readonly unreadable: unknown } =>
  isPlacementNeedsBroken(raw) &&
  Object.keys(raw).every((key) => key === "unreadable" || key === "problem") &&
  needsProblemOf(raw.unreadable) !== undefined;

/**
 * A project's `placementNeeds` in a save. Strict (a bad label is refused), but clients resend a
 * project's whole entry when they save its other settings, so a kept bad value passes back
 * unchanged, its problem worked out again here.
 */
export const PlacementNeedsProjectPatch = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementNeeds,
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) =>
        isKeptBrokenNeeds(raw)
          ? Effect.succeed({
              unreadable: raw.unreadable,
              problem: needsProblemOf(raw.unreadable) ?? "invalid value",
            } as typeof PlacementNeeds.Encoded)
          : decodeLabelsEffect(raw).pipe(
              Effect.mapError((error) => error.issue),
              Effect.as(raw as typeof PlacementNeeds.Encoded),
            ),
      encode: (needs) => Effect.succeed(needs as unknown),
    }),
  ),
);

/**
 * Whether a project's needs in a save are labels, or the very bad text stored for that project.
 * Other kept text (another text, one for a project that stores none, a stale client's after the
 * person fixed it elsewhere) could read as needs in a later build; the server refuses it.
 */
export function isKeptPlacementNeedsUnchanged(
  stored: PlacementNeeds | null | undefined,
  next: PlacementNeeds | null | undefined,
): boolean {
  if (!isPlacementNeedsBroken(next)) return true;
  return isPlacementNeedsBroken(stored) && Equal.equals(stored.unreadable, next.unreadable);
}
