/**
 * Fork: handoff instead of compaction. A long thread moves to a fresh session in the same thread,
 * with a written handoff, so its children, watched PRs and waits keep working. The settings here
 * (person-only); the pure decision lives on the server (fork/handoff). Off by default.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString, TrimmedString } from "./baseSchemas.ts";
import { ModelSelection } from "./modelSelection.ts";

/**
 * `off`: the fork does nothing; upstream's compaction settings apply as they are. `compact`: the
 * same, so one thread can opt out of its project's `handoff`. `handoff`: hand off instead.
 */
export const HandoffMode = Schema.Literals(["off", "compact", "handoff"]);
export type HandoffMode = typeof HandoffMode.Type;

/** `command`: a program prints the handoff. `agent`: the old session writes it in one turn. */
export const HandoffSource = Schema.Literals(["command", "agent"]);
export type HandoffSource = typeof HandoffSource.Type;

/** At most this many per-thread modes. */
export const HANDOFF_THREADS_MAX = 500;
/** No handoff of a smaller thread, not even when a person asks. */
export const HANDOFF_MIN_TOKENS = 20_000;
const HANDOFF_MAX_TOKENS = 10_000_000;

const tokens = (minimum: number, value: number) =>
  Schema.Int.check(Schema.isBetween({ minimum, maximum: HANDOFF_MAX_TOKENS })).pipe(
    Schema.withDecodingDefault(Effect.succeed(value)),
  );

const HandoffSettingsStruct = Schema.Struct({
  mode: HandoffMode.pipe(Schema.withDecodingDefault(Effect.succeed("off"))),
  /** Thread id to mode; wins over `mode`. A thread id that no longer exists is never read. */
  threads: Schema.Record(TrimmedNonEmptyString, HandoffMode)
    .check(
      Schema.makeFilter(
        (threads: { readonly [threadId: string]: unknown }) =>
          Object.keys(threads).length <= HANDOFF_THREADS_MAX,
        { expected: `at most ${HANDOFF_THREADS_MAX} thread modes` },
      ),
    )
    .pipe(Schema.withDecodingDefault(Effect.succeed({}))),
  source: HandoffSource.pipe(Schema.withDecodingDefault(Effect.succeed("agent"))),
  /**
   * For `source: command`: program and fixed arguments, run without a shell. It must only read and
   * be safe to run twice. Only the person's settings set it, never an agent or a repository file.
   */
  command: Schema.optionalKey(Schema.Array(TrimmedNonEmptyString).check(Schema.isMinLength(1))),
  /** For `source: agent`; empty uses the built-in text. */
  instructions: TrimmedString.check(Schema.isMaxLength(8000)).pipe(
    Schema.withDecodingDefault(Effect.succeed("")),
  ),
  /** Size backstop: a turn that ends at this size hands off. */
  sizeTokens: tokens(HANDOFF_MIN_TOKENS, 150_000),
  /** The smallest idle thread handed off before its prompt cache expires; 0 never. */
  idleTokens: Schema.Union([Schema.Literal(0), tokens(HANDOFF_MIN_TOKENS, 75_000)]).pipe(
    Schema.withDecodingDefault(Effect.succeed(75_000)),
  ),
  /** The new session's model and effort; not set uses the thread's current one, written out. */
  modelSelection: Schema.optionalKey(ModelSelection),
});

const STRUCT_DEFAULTS = Schema.decodeSync(HandoffSettingsStruct)({});

/**
 * Handoff settings. Strict: a save (settings patch) refuses a bad value or `source: command`
 * without a command; like every setting, it drops a field this build does not know. Reading the
 * settings file uses {@link HandoffSettingsFromSettingsFile} instead.
 */
export const HandoffSettings = HandoffSettingsStruct.check(
  // A save is encoded sparse (defaults left out), so the check fills them in before it looks.
  Schema.makeFilter(
    (settings) => {
      const filled = { ...STRUCT_DEFAULTS, ...settings };
      return filled.source !== "command" || filled.command !== undefined;
    },
    { expected: "a command while the source is `command`" },
  ),
);
export type HandoffSettings = typeof HandoffSettings.Type;

/** Off, no thread modes, the agent writes, 150K backstop, 75K idle. */
export const DEFAULT_HANDOFF_SETTINGS: HandoffSettings = Schema.decodeSync(HandoffSettings)({});

const decodeHandoffSettingsOption = Schema.decodeUnknownOption(HandoffSettings);

/**
 * The settings file's value. A bad one (also an unknown field, as from a newer build) reads as off
 * with a warning, never as `handoff`; the rest of the file still loads, and the next save writes
 * the default in its place. In a project's overrides it reads as off too, never as the server's.
 */
export const HandoffSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    // To the encoded form: the settings writer saves this value sparse.
    HandoffSettings,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("HandoffSettingsFromSettingsFile.decode")(function* (raw: unknown) {
        // Strict about unknown fields here: a newer build's field may narrow what it does.
        if (decodeHandoffSettingsOption(raw, { onExcessProperty: "error" })._tag === "Some") {
          return raw as typeof HandoffSettings.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid handoff settings read as off; the next settings save replaces them",
        );
        return {};
      }),
      encode: (settings) => Effect.succeed(settings as unknown),
    }),
  ),
);

/**
 * The handoff settings and mode of one thread: a project's value replaces the server's whole (a
 * bad one reads as off, never as the server's); the thread's own mode wins over `mode`.
 */
export function resolveHandoffSettings(
  server: HandoffSettings,
  project: HandoffSettings | null | undefined,
  threadId: string,
): { readonly settings: HandoffSettings; readonly mode: HandoffMode } {
  const settings = project ?? server;
  const mode = Object.hasOwn(settings.threads, threadId)
    ? (settings.threads[threadId] ?? settings.mode)
    : settings.mode;
  return { settings, mode };
}
