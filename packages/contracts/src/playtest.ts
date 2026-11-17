/**
 * Fork: playtest sessions. A person tries an agent's unfinished work on a test target, round after
 * round. Settings for the project's adapter (steps only a person sets) and the contract a step
 * follows (environment, exit codes); docs/fork/playtest.md. Off by default.
 */
import * as Effect from "effect/Effect";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import { TrimmedNonEmptyString, TrimmedString } from "./baseSchemas.ts";

const PLAYTEST_STEP_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/u;
/** At most this many steps in one round. */
export const PLAYTEST_PUSH_STEPS_MAX = 10;

/**
 * Whether `program` is a full path on the server's OS (Node's `process.platform` name). Anything else
 * (on Windows also `/…` and `\…`, relative to the drive) resolves against the agent's worktree or
 * `PATH`. The server checks it when a save sets a program; the step runner checks it before a run.
 */
export const isPlaytestProgramFullyQualified = (program: string, platform: string): boolean =>
  platform === "win32"
    ? /^[A-Za-z]:[\\/]/u.test(program) || /^[\\/]{2}/u.test(program)
    : program.startsWith("/");

/** A full path on some OS. The settings do not know the server's OS; the server's save checks it. */
const isAbsoluteProgram = (program: string): boolean =>
  isPlaytestProgramFullyQualified(program, "linux") ||
  isPlaytestProgramFullyQualified(program, "win32");

/** What `T3_PLAYTEST_STEP` says for the three fixed steps, so no push step may take these names. */
const PLAYTEST_FIXED_STEPS: ReadonlyArray<string> = ["claim", "renew", "release"];

/** A push step's name; not `claim`, `renew` or `release`, which name the fixed steps. */
export const PlaytestStepName = Schema.String.check(
  Schema.isPattern(PLAYTEST_STEP_NAME_PATTERN),
  Schema.makeFilter((name: string) => !PLAYTEST_FIXED_STEPS.includes(name), {
    expected: "a push step name other than claim, renew or release",
  }),
);
export type PlaytestStepName = typeof PlaytestStepName.Type;

/**
 * One adapter step. It runs as an argument list without a shell, in the session's worktree, with
 * the server's environment plus the session facts ({@link PLAYTEST_ENV}). The arguments are fixed:
 * an agent adds none. Only settings can add or change one.
 */
export const PlaytestStep = Schema.Struct({
  /** Program (an absolute path) and fixed arguments: at most 50 items, the program included. */
  argv: Schema.Array(TrimmedNonEmptyString).check(
    Schema.isMinLength(1),
    Schema.isMaxLength(50),
    Schema.makeFilter((argv: ReadonlyArray<string>) => isAbsoluteProgram(argv[0] ?? ""), {
      expected: "a program given by its absolute path",
    }),
  ),
  /** One run is stopped after this long and counts as failed. */
  timeoutSeconds: Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: 3600 })).pipe(
    Schema.withDecodingDefault(Effect.succeed(120)),
  ),
  description: TrimmedString.check(Schema.isMaxLength(500)).pipe(
    Schema.withDecodingDefault(Effect.succeed("")),
  ),
});
export type PlaytestStep = typeof PlaytestStep.Type;

/** One step of a round, named so the thread can say which step a round is in or failed at. */
export const PlaytestPushStep = Schema.Struct({ name: PlaytestStepName, ...PlaytestStep.fields });
export type PlaytestPushStep = typeof PlaytestPushStep.Type;

const intBetween = (minimum: number, maximum: number, value: number) =>
  Schema.Int.check(Schema.isBetween({ minimum, maximum })).pipe(
    Schema.withDecodingDefault(Effect.succeed(value)),
  );

const PlaytestSettingsStruct = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /** Takes the target for this session; refuses (exit 10) when it is busy or not allowed. */
  claim: Schema.optionalKey(PlaytestStep),
  /** Moves the claim's expiry forward; exit 10 means the claim is gone and the session ends. */
  renew: Schema.optionalKey(PlaytestStep),
  /** One round, every step in this order; the first that does not exit 0 ends the round. */
  push: Schema.Array(PlaytestPushStep)
    .check(Schema.isMaxLength(PLAYTEST_PUSH_STEPS_MAX))
    .pipe(Schema.withDecodingDefault(Effect.succeed([]))),
  /** Puts the target back as it was and frees the claim; exit 0 also when there is no claim. */
  release: Schema.optionalKey(PlaytestStep),
  /** How often T3 renews a live session's claim. Keep it well under the claim's own expiry. */
  renewMinutes: intBetween(1, 60, 10),
  /** T3 ends a session with no round and no message from the person for this long. */
  idleMinutes: intBetween(5, 1440, 90),
  /** T3 ends every session this long after its start. */
  maxHours: intBetween(1, 48, 6),
});

const STRUCT_DEFAULTS = Schema.decodeSync(PlaytestSettingsStruct)({});

/** Why a decoded playtest setting cannot be used, or undefined when it can. */
function playtestSettingsProblem(settings: typeof PlaytestSettingsStruct.Type): string | undefined {
  const names = settings.push.map((step) => step.name);
  const repeated = names.filter((name, index) => names.indexOf(name) !== index);
  if (repeated.length > 0) return `push step names repeat: ${[...new Set(repeated)].join(", ")}`;
  if (!settings.enabled) return undefined;
  const missing: Array<string> = (["claim", "renew", "release"] as const).filter(
    (step) => settings[step] === undefined,
  );
  if (settings.push.length === 0) missing.push("push");
  return missing.length === 0 ? undefined : `on, but without the steps ${missing.join(", ")}`;
}

/**
 * Playtest settings. Strict: a save (settings patch) refuses repeated push step names, a step
 * whose program is not an absolute path, and `enabled` without all four steps. Reading the
 * settings file uses {@link PlaytestSettingsFromSettingsFile} instead.
 */
export const PlaytestSettings = PlaytestSettingsStruct.check(
  // A save is encoded sparse (the writer drops values equal to their defaults), so the check fills
  // them in again before it looks.
  Schema.makeFilter(
    (settings) => playtestSettingsProblem({ ...STRUCT_DEFAULTS, ...settings }) === undefined,
    {
      expected: "push step names used once, and claim, renew, push and release set while enabled",
    },
  ),
);
export type PlaytestSettings = typeof PlaytestSettings.Type;

/** Off, no steps, the default limits. */
export const DEFAULT_PLAYTEST_SETTINGS: PlaytestSettings = Schema.decodeSync(PlaytestSettings)({});

const decodePlaytestSettingsOption = Schema.decodeUnknownOption(PlaytestSettings);

/**
 * The settings file's value. A bad one reads as off with a warning (fail closed; the rest of the
 * file still loads), and the next save writes the default in its place. In a project's overrides
 * it reads as off too, never as the server's adapter.
 */
export const PlaytestSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    // To the encoded form, not the type: the settings writer saves this value sparse, without the
    // fields equal to their defaults, and the type side would refuse that.
    PlaytestSettings,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("PlaytestSettingsFromSettingsFile.decode")(function* (raw: unknown) {
        if (decodePlaytestSettingsOption(raw)._tag === "Some") {
          return raw as typeof PlaytestSettings.Encoded;
        }
        yield* Effect.logWarning(
          "settings: invalid playtest settings read as off; the next settings save replaces them",
        );
        return {};
      }),
      encode: (settings) => Effect.succeed(settings as unknown),
    }),
  ),
);

/**
 * What T3 passes to every step, as environment variables (never as arguments, so the argument
 * list stays the person's). A step reads only the ones it needs.
 */
export const PLAYTEST_ENV = {
  /** The session id ({@link PlaytestSessionId}); the same for every step of one session. */
  session: "T3_PLAYTEST_SESSION",
  /** What the agent asked to playtest (a module, a page); {@link PlaytestTarget}. */
  target: "T3_PLAYTEST_TARGET",
  /** The agent's reason, one line, at most 200 characters; may be empty. */
  note: "T3_PLAYTEST_NOTE",
  project: "T3_PLAYTEST_PROJECT",
  /** The thread that owns the session: who holds the claim. */
  thread: "T3_PLAYTEST_THREAD",
  /** The session's worktree, also the step's working folder. */
  worktree: "T3_PLAYTEST_WORKTREE",
  /** `claim`, `renew`, `release`, or the push step's name. */
  step: "T3_PLAYTEST_STEP",
  /** The round number: 0 for claim, renew and release before the first round; then 1, 2, … */
  round: "T3_PLAYTEST_ROUND",
} as const;

/** Exit 0: done (release: also when there was nothing to release). */
export const PLAYTEST_EXIT_OK = 0;
/**
 * Refused, nothing changed. claim: busy or not allowed. renew: the claim is gone or ended, so T3
 * ends the session. A push step: refused (a guard). Any other exit or a timeout is a failure:
 * claim and push stop there; T3 retries renew and release later.
 */
export const PLAYTEST_EXIT_REFUSED = 10;

/** `pt_` plus 10 random base32 characters; never built from the target or the thread. */
export const PlaytestSessionId = Schema.String.check(Schema.isPattern(/^pt_[a-z2-7]{10}$/u));
export type PlaytestSessionId = typeof PlaytestSessionId.Type;

/** What an agent may name as the target; the adapter's claim decides whether it is allowed. */
export const PlaytestTarget = Schema.String.check(
  Schema.isPattern(/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/u),
);
export type PlaytestTarget = typeof PlaytestTarget.Type;
