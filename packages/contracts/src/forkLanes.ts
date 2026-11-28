/**
 * Fork (issue 1156, phase 1): the `lanes` settings (off, person-only; lanes are defined here, the
 * orchestrator only switches them on and off) and the shapes later phases use: item claims, lane
 * notes and escalations. Nothing reads them yet. Docs: docs/fork/README.md, `server.lanes`.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import {
  IsoDateTime,
  NonNegativeInt,
  ProjectId,
  ThreadId,
  TrimmedNonEmptyString,
} from "./baseSchemas.ts";

/** Lower-case words of letters and digits joined by `-`, starting with a letter: `pr-queue`. */
const LANE_NAME_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/u;
const LANE_NAME_MAX_CHARS = 40;

/** A lane's name, the key of its definition in `lanes.definitions`. */
export const LaneName = Schema.String.check(
  Schema.isPattern(LANE_NAME_PATTERN),
  Schema.isMaxLength(LANE_NAME_MAX_CHARS),
).pipe(Schema.brand("LaneName"));
export type LaneName = typeof LaneName.Type;

const isLaneName = (value: unknown): value is LaneName =>
  typeof value === "string" && value.length <= LANE_NAME_MAX_CHARS && LANE_NAME_PATTERN.test(value);

/** The kinds of item a lane can own. */
const LANE_ITEM_KINDS = ["pr", "issue"] as const;
export const LaneItemKind = Schema.Literals(LANE_ITEM_KINDS);
export type LaneItemKind = typeof LaneItemKind.Type;

/**
 * Who runs a lane. Only `agent` for now: a delegated child thread with the lane's brief. A program
 * driver comes later; until then a definition naming another driver drops on read.
 */
const LANE_DRIVERS = ["agent"] as const;
export const LaneDriver = Schema.Literals(LANE_DRIVERS);
export type LaneDriver = typeof LaneDriver.Type;

const LANE_DESCRIPTION_MAX_CHARS = 2_000;

const hasNoRepeat = Schema.makeFilter(
  (kinds: ReadonlyArray<string>) => new Set(kinds).size === kinds.length,
  { expected: "each item kind once" },
);

/** One lane: what it does (its thread's brief starts from this), what it owns, who runs it. */
export const LaneDefinition = Schema.Struct({
  description: TrimmedNonEmptyString.check(Schema.isMaxLength(LANE_DESCRIPTION_MAX_CHARS)),
  /** The kinds of item this lane may claim: at least one, each once. */
  owns: Schema.Array(LaneItemKind).check(Schema.isMinLength(1), hasNoRepeat),
  driver: LaneDriver.pipe(Schema.withDecodingDefault(Effect.succeed("agent" as const))),
});
export type LaneDefinition = typeof LaneDefinition.Type;

/**
 * The built-in lanes, defined without a word in the settings. They live here, not in the settings'
 * default: the settings writer leaves out what equals the default, so a default holding them would
 * lose them, or a change to one, on the next restart.
 */
export const BUILT_IN_LANE_DEFINITIONS: { readonly [name: string]: LaneDefinition } = {
  "pr-queue": {
    description:
      "Moves the project's open pull requests to a decision: reviews, fix rounds, checks and merges.",
    owns: ["pr"],
    driver: "agent",
  },
  issues: {
    description:
      "Works the project's open issues: triage, briefs and the writers that turn them into pull requests.",
    owns: ["issue"],
    driver: "agent",
  },
};

const isBuiltInLane = (name: string): boolean => Object.hasOwn(BUILT_IN_LANE_DEFINITIONS, name);

/** A stored lane: its definition, or `false` for a built-in lane the person removed. */
const LaneEntry = Schema.Union([LaneDefinition, Schema.Literal(false)]);

/**
 * The `lanes` settings block as stored. `enabled` off: no lane can be switched on, whatever is
 * defined. `definitions` holds only what differs from the built-in lanes (empty by default); read
 * the lanes with {@link laneDefinitions}.
 */
export const LanesSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  definitions: Schema.Record(LaneName, LaneEntry).pipe(
    Schema.withDecodingDefault(Effect.succeed({})),
  ),
});
export type LanesSettings = typeof LanesSettings.Type;

/** Off, nothing stored: what a settings file without `lanes` reads as. */
const DEFAULT_LANES_SETTINGS: LanesSettings = { enabled: false, definitions: {} };

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const decodeDefinition = Schema.decodeUnknownOption(LaneDefinition);

/**
 * The settings file's `lanes`, read leniently: not an object (or missing) reads as off with nothing
 * stored. Anything but `enabled: true` is off. A `definitions` that is not an object stores nothing.
 * An entry whose name is not a lane name, whose definition does not decode (another driver or item
 * kind, no item kind, a kind twice), or a `false` for a lane that is not built in, drops alone.
 */
function readLanes(raw: unknown): typeof LanesSettings.Encoded {
  if (!isRecord(raw)) return DEFAULT_LANES_SETTINGS;
  const definitions = isRecord(raw.definitions) ? raw.definitions : {};
  const kept: Record<string, LaneDefinition | false> = {};
  for (const [name, value] of Object.entries(definitions)) {
    if (!isLaneName(name)) continue;
    if (value === false) {
      if (isBuiltInLane(name)) kept[name] = false;
      continue;
    }
    const definition = decodeDefinition(value);
    if (definition._tag === "Some") kept[name] = definition.value;
  }
  return { enabled: raw.enabled === true, definitions: kept };
}

/** {@link LanesSettings} as the settings file holds it: read by {@link readLanes}. */
export const LanesSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    LanesSettings,
    SchemaTransformation.transform({
      decode: (raw: unknown): typeof LanesSettings.Encoded => readLanes(raw),
      encode: (settings: typeof LanesSettings.Encoded): unknown => settings,
    }),
  ),
  Schema.withDecodingDefault(Effect.succeed(DEFAULT_LANES_SETTINGS)),
);

/**
 * A save's change to `lanes`. A field left out keeps its value. Each lane on its own: a definition
 * sets it whole, `false` removes a built-in lane, `null` drops what is stored (a built-in lane is
 * back as built). Strict: a bad name or definition, or `false` for a lane that is not built in, is
 * refused (a record keyed by {@link LaneName} would drop a bad name and save nothing).
 */
export const LanesSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
  definitions: Schema.optionalKey(
    Schema.Record(Schema.String, Schema.NullOr(LaneEntry)).check(
      Schema.makeFilter((definitions) => Object.keys(definitions).every(isLaneName), {
        expected: "lane names: lower-case words joined by '-', at most 40 characters",
      }),
      Schema.makeFilter(
        (definitions) =>
          Object.entries(definitions).every(
            ([name, value]) => value !== false || isBuiltInLane(name),
          ),
        { expected: "false only for a built-in lane (pr-queue, issues)" },
      ),
    ),
  ),
});
export type LanesSettingsPatch = typeof LanesSettingsPatch.Type;

/** The lanes that exist: the built-in ones, changed or removed by the stored ones, then the rest. */
export function laneDefinitions(settings: LanesSettings): {
  readonly [name: string]: LaneDefinition;
} {
  const merged: Record<string, LaneDefinition | false> = {
    ...BUILT_IN_LANE_DEFINITIONS,
    ...settings.definitions,
  };
  const lanes: Record<string, LaneDefinition> = {};
  for (const [name, entry] of Object.entries(merged)) {
    if (entry !== false) lanes[name] = entry;
  }
  return lanes;
}

/** A defined lane by name, or undefined for a name the settings do not define (refuse it). */
export function findLaneDefinition(
  settings: LanesSettings,
  name: string,
): LaneDefinition | undefined {
  const lanes = laneDefinitions(settings);
  return isLaneName(name) && Object.hasOwn(lanes, name) ? lanes[name] : undefined;
}

/** An owner or repository name, never `.` or `..`. */
const REPO_PART = String.raw`(?!\.\.?[/#])[a-z0-9_.-]+`;
/** `pr:<owner>/<repo>#<n>` or `issue:<owner>/<repo>#<n>`, owner and repository in lower case. */
const LANE_ITEM_PATTERN = new RegExp(
  String.raw`^(pr|issue):(${REPO_PART}/${REPO_PART})#([1-9][0-9]{0,9})$`,
  "u",
);
/** As written by hand or a tool: any case, which GitHub ignores. */
const LANE_ITEM_INPUT_PATTERN = new RegExp(LANE_ITEM_PATTERN.source, "iu");

/**
 * One item a lane can own, as text: `pr:owner/repo#12`. Owner and repository are lower case, so
 * one pull request or issue has one reference (and one claim); {@link parseLaneItemRef} makes one.
 */
export const LaneItemRef = Schema.String.check(Schema.isPattern(LANE_ITEM_PATTERN)).pipe(
  Schema.brand("LaneItemRef"),
);
export type LaneItemRef = typeof LaneItemRef.Type;

export interface LaneItem {
  readonly kind: LaneItemKind;
  /** `owner/repo`, lower case. */
  readonly repo: string;
  readonly number: number;
}

/**
 * The parts of an item reference in any case (the kind too), owner and repository lower-cased; null
 * for text that is not one.
 */
export function parseLaneItemRef(text: string): LaneItem | null {
  const match = LANE_ITEM_INPUT_PATTERN.exec(text);
  if (match === null) return null;
  const number = Number(match[3]);
  if (!Number.isSafeInteger(number)) return null;
  return { kind: match[1]!.toLowerCase() as LaneItemKind, repo: match[2]!.toLowerCase(), number };
}

/** The one reference of an item (owner and repository lower-cased), or null for a bad item. */
export function formatLaneItemRef(item: LaneItem): LaneItemRef | null {
  const text = `${item.kind}:${item.repo.toLowerCase()}#${item.number}`;
  return LANE_ITEM_PATTERN.test(text) ? (text as LaneItemRef) : null;
}

/**
 * Which lane thread acts for a lane: bumped each time the lane gets a new thread, so a replaced
 * thread can no longer act as the lane.
 */
export const LaneGeneration = NonNegativeInt.pipe(Schema.brand("LaneGeneration"));
export type LaneGeneration = typeof LaneGeneration.Type;

/** Which running lane of which project: a lane belongs to the project, not to a thread. */
const LaneOwner = {
  projectId: ProjectId,
  lane: LaneName,
  generation: LaneGeneration,
  /** The lane thread of that generation. */
  threadId: ThreadId,
};

/** One owner per item: a second claim on the item is refused while this one stands. */
export const LaneClaim = Schema.Struct({
  item: LaneItemRef,
  ...LaneOwner,
  claimedAt: IsoDateTime,
});
export type LaneClaim = typeof LaneClaim.Type;

const LANE_REPORT_MAX_CHARS = 2_000;

const ReportText = TrimmedNonEmptyString.check(Schema.isMaxLength(LANE_REPORT_MAX_CHARS));

/** A short report up to the board; it wakes nobody. */
export const LaneNote = Schema.Struct({
  ...LaneOwner,
  at: IsoDateTime,
  text: ReportText,
  /** The items the note is about. */
  items: Schema.Array(LaneItemRef).pipe(Schema.withDecodingDefault(Effect.succeed([]))),
});
export type LaneNote = typeof LaneNote.Type;

/**
 * Something only the main orchestrator (or its owner) can decide: it wakes the main orchestrator.
 * Delivered at least once, so `id` lets a reader drop a repeat.
 */
export const LaneEscalation = Schema.Struct({
  id: TrimmedNonEmptyString,
  ...LaneOwner,
  at: IsoDateTime,
  text: ReportText,
  items: Schema.Array(LaneItemRef).pipe(Schema.withDecodingDefault(Effect.succeed([]))),
});
export type LaneEscalation = typeof LaneEscalation.Type;
