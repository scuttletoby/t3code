/**
 * Fork (placement row P2): settings only. `placement`: what this machine has and how it places work
 * on linked machines; `placementNeeds`: what a project's work needs. Off by default, person-only,
 * wired in only through forkSettings.ts.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { EnvironmentId, TrimmedString } from "./baseSchemas.ts";

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

/** One label: trimmed, then one word of at most {@link PLACEMENT_LABEL_MAX_LENGTH} characters. */
export const PlacementLabel = TrimmedString.check(
  Schema.makeFilter((label: string) => LABEL_PATTERN.test(label), {
    expected: `a label of 1 to ${PLACEMENT_LABEL_MAX_LENGTH} characters without spaces`,
  }),
);

const dedupe = (labels: ReadonlyArray<string>): ReadonlyArray<string> => [...new Set(labels)];

/** Labels: each trimmed and checked, a repeat dropped (first one kept), at most 64 after that. */
export const PlacementLabels = Schema.Array(PlacementLabel).pipe(
  Schema.decodeTo(
    Schema.Array(Schema.String).check(Schema.isMaxLength(PLACEMENT_LABELS_MAX)),
    SchemaTransformation.transform({ decode: dedupe, encode: (labels) => labels }),
  ),
);

/** A `never` group: labels that must not meet on one machine; two or more, repeats dropped. */
export const PlacementNeverGroup = PlacementLabels.check(
  Schema.makeFilter((group: ReadonlyArray<string>) => group.length >= 2, {
    expected: "a never group of at least two different labels",
  }),
);

/** A full path on some OS; the settings do not know which one runs the command. */
const isAbsolutePath = (path: string): boolean =>
  path.startsWith("/") || /^[A-Za-z]:[\\/]/u.test(path) || /^[\\/]{2}/u.test(path);

/** Empty (no command) or an absolute path; a relative path is refused. */
export const PlacementFactsCommand = TrimmedString.check(
  Schema.isMaxLength(FACTS_COMMAND_MAX_LENGTH),
  Schema.makeFilter((command: string) => command === "" || isAbsolutePath(command), {
    expected: "an empty factsCommand or an absolute path",
  }),
);

const PlacementWeights = Schema.Record(EnvironmentId, PlacementWeight).check(
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

const decodeStoredSync = Schema.decodeUnknownSync(PlacementStored);

/** Why the value cannot be read, on one line (for the warning and Settings); none when it can. */
const problemOf = (raw: unknown): string | undefined => {
  try {
    decodeStoredSync(raw, REFUSE_UNKNOWN_KEYS);
    return undefined;
  } catch (error) {
    const text = error instanceof Error ? error.message : String(error);
    return text.replaceAll(/\s+/gu, " ").trim().slice(0, 500) || "invalid value";
  }
};

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

const decodeLabelsExit = Schema.decodeUnknownExit(PlacementLabels);

/**
 * The settings file's `placementNeeds` (the server's, or a project's override): labels a machine
 * must have to take the work. A bad value reads as no needs (`[]`) with a warning, never as the
 * server's; the next save writes `[]` in its place.
 */
export const PlacementNeedsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    PlacementLabels,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("PlacementNeedsFromSettingsFile.decode")(function* (raw: unknown) {
        if (decodeLabelsExit(raw)._tag === "Success") {
          return raw as typeof PlacementLabels.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid placementNeeds read as none; the next settings save replaces them",
        );
        return [];
      }),
      encode: (needs) => Effect.succeed(needs as unknown),
    }),
  ),
);
