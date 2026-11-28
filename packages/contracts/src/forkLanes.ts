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
export const LANE_NAME_PATTERN = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/u;
export const LANE_NAME_MAX_CHARS = 40;

/** A lane's name, the key of its definition in `lanes.definitions`. */
export const LaneName = Schema.String.check(
  Schema.isPattern(LANE_NAME_PATTERN),
  Schema.isMaxLength(LANE_NAME_MAX_CHARS),
).pipe(Schema.brand("LaneName"));
export type LaneName = typeof LaneName.Type;

export const isLaneName = (value: unknown): value is LaneName =>
  typeof value === "string" && value.length <= LANE_NAME_MAX_CHARS && LANE_NAME_PATTERN.test(value);

/** The kinds of item a lane can own. */
export const LANE_ITEM_KINDS = ["pr", "issue"] as const;
export const LaneItemKind = Schema.Literals(LANE_ITEM_KINDS);
export type LaneItemKind = typeof LaneItemKind.Type;

/**
 * Who runs a lane. Only `agent` for now: a delegated child thread with the lane's brief. A program
 * driver comes later; until then a definition naming another driver drops on read.
 */
export const LANE_DRIVERS = ["agent"] as const;
export const LaneDriver = Schema.Literals(LANE_DRIVERS);
export type LaneDriver = typeof LaneDriver.Type;

export const LANE_DESCRIPTION_MAX_CHARS = 2_000;

/** One lane: what it does (its thread's brief starts from this), what it owns, who runs it. */
export const LaneDefinition = Schema.Struct({
  description: TrimmedNonEmptyString.check(Schema.isMaxLength(LANE_DESCRIPTION_MAX_CHARS)),
  /** The kinds of item this lane may claim. */
  owns: Schema.Array(LaneItemKind),
  driver: LaneDriver.pipe(Schema.withDecodingDefault(Effect.succeed("agent" as const))),
});
export type LaneDefinition = typeof LaneDefinition.Type;

/** The built-in lanes, defined but not running until the orchestrator switches one on. */
export const DEFAULT_LANE_DEFINITIONS: { readonly [name: string]: LaneDefinition } = {
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

/**
 * The `lanes` settings block. `enabled` off: no lane can be switched on, whatever is defined. Each
 * field has its default, since the settings writer saves the value sparse.
 */
export const LanesSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  definitions: Schema.Record(LaneName, LaneDefinition).pipe(
    Schema.withDecodingDefault(Effect.succeed(DEFAULT_LANE_DEFINITIONS)),
  ),
});
export type LanesSettings = typeof LanesSettings.Type;

/** Off, with the built-in lanes defined: what a settings file without `lanes` reads as. */
export const DEFAULT_LANES_SETTINGS: LanesSettings = {
  enabled: false,
  definitions: DEFAULT_LANE_DEFINITIONS as LanesSettings["definitions"],
};

const isRecord = (value: unknown): value is Readonly<Record<string, unknown>> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const decodeDefinition = Schema.decodeUnknownOption(LaneDefinition);

/**
 * The settings file's `lanes`, read leniently: not an object (or missing) reads as the default.
 * Anything but `enabled: true` is off. `definitions` left out keeps the built-in lanes; a value that
 * is not an object defines no lane. An entry whose name is not a lane name, or whose definition does
 * not decode (an unknown driver or item kind included), drops alone.
 */
function readLanes(raw: unknown): typeof LanesSettings.Encoded {
  if (!isRecord(raw)) return DEFAULT_LANES_SETTINGS;
  const enabled = raw.enabled === true;
  if (!("definitions" in raw)) return { enabled, definitions: DEFAULT_LANE_DEFINITIONS };
  const definitions = isRecord(raw.definitions) ? raw.definitions : {};
  const kept: Record<string, LaneDefinition> = {};
  for (const [name, value] of Object.entries(definitions)) {
    if (!isLaneName(name)) continue;
    const definition = decodeDefinition(value);
    if (definition._tag === "Some") kept[name] = definition.value;
  }
  return { enabled, definitions: kept };
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
 * A save's change to `lanes`. A field left out keeps its value; each definition is set whole or
 * (`null`) removed on its own. Strict: a bad name or a bad definition is refused (a record keyed by
 * {@link LaneName} would drop a bad name and save nothing, so the names are checked here).
 */
export const LanesSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
  definitions: Schema.optionalKey(
    Schema.Record(Schema.String, Schema.NullOr(LaneDefinition)).check(
      Schema.makeFilter((definitions) => Object.keys(definitions).every(isLaneName), {
        expected: "lane names: lower-case words joined by '-', at most 40 characters",
      }),
    ),
  ),
});
export type LanesSettingsPatch = typeof LanesSettingsPatch.Type;

/** A defined lane by name, or undefined for a name the settings do not define (refuse it). */
export function findLaneDefinition(
  settings: LanesSettings,
  name: string,
): LaneDefinition | undefined {
  return isLaneName(name) && Object.hasOwn(settings.definitions, name)
    ? settings.definitions[name]
    : undefined;
}

/** `pr:<owner>/<repo>#<n>` or `issue:<owner>/<repo>#<n>`. */
export const LANE_ITEM_PATTERN =
  /^(pr|issue):([A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+)#([1-9][0-9]{0,9})$/u;

/** One item a lane can own, as text: `pr:owner/repo#12`. */
export const LaneItemRef = Schema.String.check(Schema.isPattern(LANE_ITEM_PATTERN)).pipe(
  Schema.brand("LaneItemRef"),
);
export type LaneItemRef = typeof LaneItemRef.Type;

export interface LaneItem {
  readonly kind: LaneItemKind;
  /** `owner/repo`. */
  readonly repo: string;
  readonly number: number;
}

/** The parts of an item reference, or null for text that is not one. */
export function parseLaneItemRef(text: string): LaneItem | null {
  const match = LANE_ITEM_PATTERN.exec(text);
  if (match === null) return null;
  const number = Number(match[3]);
  if (!Number.isSafeInteger(number)) return null;
  return { kind: match[1] as LaneItemKind, repo: match[2]!, number };
}

export const formatLaneItemRef = (item: LaneItem): LaneItemRef =>
  `${item.kind}:${item.repo}#${item.number}` as LaneItemRef;

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

export const LANE_REPORT_MAX_CHARS = 2_000;

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
