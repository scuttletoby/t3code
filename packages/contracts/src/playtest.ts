/**
 * Fork: playtest sessions. A person tries an agent's unfinished work on a test target, round after
 * round. Settings for the project's adapter (steps only a person sets) and the contract a step
 * follows (environment, exit codes); docs/fork/playtest.md. Off by default.
 */
import * as Effect from "effect/Effect";
import * as Result from "effect/Result";
import * as Schema from "effect/Schema";
import * as SchemaTransformation from "effect/SchemaTransformation";

import {
  IsoDateTime,
  NonNegativeInt,
  PositiveInt,
  ProjectId,
  ThreadId,
  TrimmedNonEmptyString,
  TrimmedString,
} from "./baseSchemas.ts";

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

/**
 * A full path on some OS. The settings do not know the server's OS; the server's save checks it
 * ({@link isPlaytestProgramFullyQualified}). Placement's `factsCommand` uses it too.
 */
export const isAbsoluteProgram = (program: string): boolean =>
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

const decodePlaytestSettingsResult = Schema.decodeUnknownResult(PlaytestSettings);
/** Also refuses an unknown field, nested ones too: it may be a newer build's limit. */
const strictPlaytestSettingsProblem = (raw: unknown): string | undefined => {
  const decoded = decodePlaytestSettingsResult(raw, { onExcessProperty: "error" });
  return Result.isFailure(decoded) ? decoded.failure.message : undefined;
};
const isStrictPlaytestSettings = (raw: unknown) => strictPlaytestSettingsProblem(raw) === undefined;

/** A save's value: refused when bad, also for a field this build does not know (never dropped). */
export const PlaytestSettingsPatch = Schema.Unknown.check(
  Schema.makeFilter(isStrictPlaytestSettings, {
    expected: "playtest settings with only the fields this build knows",
  }),
).pipe(
  Schema.decodeTo(
    PlaytestSettings,
    // The value is already checked; the settings writer saves it sparse.
    SchemaTransformation.transformEffect({
      decode: (raw: unknown) => Effect.succeed(raw as typeof PlaytestSettings.Encoded),
      encode: (settings: typeof PlaytestSettings.Encoded) => Effect.succeed(settings as unknown),
    }),
  ),
);

/**
 * The settings file's value. A bad one reads as off with a warning (fail closed; the rest of the
 * file still loads), and the next save writes the default in its place. So does a field this build
 * does not know: it may be a newer build's limit that this build would not apply. In a project's
 * overrides it reads as off too, never as the server's adapter.
 */
export const PlaytestSettingsFromSettingsFile = Schema.Unknown.pipe(
  Schema.decodeTo(
    // To the encoded form, not the type: the settings writer saves this value sparse, without the
    // fields equal to their defaults, and the type side would refuse that.
    PlaytestSettings,
    SchemaTransformation.transformEffect({
      decode: Effect.fn("PlaytestSettingsFromSettingsFile.decode")(function* (raw: unknown) {
        const problem = strictPlaytestSettingsProblem(raw);
        if (problem === undefined) {
          return raw as typeof PlaytestSettings.Encoded;
        }
        yield* Effect.logWarning(
          `settings: invalid playtest settings read as off (${problem.replace(/\s+/g, " ").trim()}); the next settings save replaces them`,
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

/**
 * C0 control characters and DEL. Agent text that reaches a step's environment may hold none: a
 * NUL makes every spawn fail, release too.
 */
export const PLAYTEST_CONTROL_CHARACTERS = /[\u0000-\u001f\u007f]/gu;

/** The agent's reason for a session: one line, at most 200 characters, may be empty. */
export const PLAYTEST_NOTE_MAX = 200;
export const PlaytestNote = Schema.String.check(
  Schema.isMaxLength(PLAYTEST_NOTE_MAX),
  Schema.isPattern(/^[^\u0000-\u001f\u007f]*$/u),
);

/** The steps a session started with; it keeps them to its end, whatever the settings say later. */
export const PlaytestAdapter = Schema.Struct({
  claim: PlaytestStep,
  renew: PlaytestStep,
  push: Schema.Array(PlaytestPushStep).check(
    Schema.isMinLength(1),
    Schema.isMaxLength(PLAYTEST_PUSH_STEPS_MAX),
  ),
  release: PlaytestStep,
  renewMinutes: Schema.Int,
  idleMinutes: Schema.Int,
  maxHours: Schema.Int,
});
export type PlaytestAdapter = typeof PlaytestAdapter.Type;

/**
 * `claiming`: the claim step runs, or ran and T3 stopped before it saved the answer (the target
 * may be claimed, so only an end, which runs `release`, leaves it). `active`: claimed, rounds may
 * run. `ending`: `release` runs or must run again. `ended`: released, or the claim was refused.
 */
export const PlaytestSessionState = Schema.Literals(["claiming", "active", "ending", "ended"]);
export type PlaytestSessionState = typeof PlaytestSessionState.Type;

/**
 * Why a session ends. `agent` and `person` ask for it; `claim-refused` (exit 10, nothing to
 * release) and `claim-failed` (also a claim cut by a T3 restart) end a session that never became
 * active; the rest are T3's own ends: `claim-gone` (renew exited 10), `idle`, `time-limit`, and
 * `thread-gone` (archived or deleted).
 */
export const PlaytestEndReason = Schema.Literals([
  "agent",
  "person",
  "claim-refused",
  "claim-failed",
  "claim-gone",
  "idle",
  "time-limit",
  "thread-gone",
]);
export type PlaytestEndReason = typeof PlaytestEndReason.Type;

/** `stopped`: the session ended, or T3 stopped, while the round ran. */
export const PlaytestRoundState = Schema.Literals([
  "running",
  "done",
  "refused",
  "failed",
  "stopped",
]);
export type PlaytestRoundState = typeof PlaytestRoundState.Type;

/** The most of a step's output a record keeps (its end). */
export const PLAYTEST_DETAIL_MAX = 2000;
const PlaytestDetail = Schema.String.check(Schema.isMaxLength(PLAYTEST_DETAIL_MAX));

export const PlaytestRound = Schema.Struct({
  number: PositiveInt,
  state: PlaytestRoundState,
  /** The step running now, or the one that ended the round. */
  step: PlaytestStepName,
  /** The end of the last step's output, or why the round stopped. */
  detail: PlaytestDetail,
  startedAt: IsoDateTime,
  endedAt: Schema.optionalKey(IsoDateTime),
});
export type PlaytestRound = typeof PlaytestRound.Type;

export const PlaytestSessionRecord = Schema.Struct({
  id: PlaytestSessionId,
  threadId: ThreadId,
  projectId: ProjectId,
  target: PlaytestTarget,
  /**
   * Checked as `PlaytestNote` when a session starts. Read back with only its length checked, so an
   * older file's note never drops its session; steps get it without control characters.
   */
  note: Schema.String.check(Schema.isMaxLength(PLAYTEST_NOTE_MAX)),
  /** The thread's worktree, else the project's folder: claim and push run here (not renew, release). */
  worktree: TrimmedNonEmptyString,
  adapter: PlaytestAdapter,
  state: PlaytestSessionState,
  startedAt: IsoDateTime,
  /** The last round's start or end, or the person's last message; the idle limit counts from it. */
  lastActivityAt: IsoDateTime,
  /** The last renew that exited 0. */
  renewedAt: Schema.optionalKey(IsoDateTime),
  /** Rounds started; the next round is this plus one. */
  rounds: NonNegativeInt,
  lastRound: Schema.optionalKey(PlaytestRound),
  endReason: Schema.optionalKey(PlaytestEndReason),
  endedAt: Schema.optionalKey(IsoDateTime),
  /** The last failed claim, renew or release step, and why. */
  problem: Schema.optionalKey(PlaytestDetail),
  /**
   * T3 ended the session on its own (idle, time limit, claim gone, a claim cut by a restart):
   * `due` until one message about it reached the thread (or the thread could not take it), then
   * `sent`. Absent for ends the agent or the person asked for. An older T3 ignores it.
   */
  notice: Schema.optionalKey(Schema.Literals(["due", "sent"])),
});
export type PlaytestSessionRecord = typeof PlaytestSessionRecord.Type;

export const PLAYTEST_STORE_VERSION = 1;
export const PlaytestStoreFile = Schema.Struct({
  version: Schema.Literal(PLAYTEST_STORE_VERSION),
  sessions: Schema.Array(PlaytestSessionRecord),
});
export type PlaytestStoreFile = typeof PlaytestStoreFile.Type;

export const PlaytestFailureCode = Schema.Literals([
  /** Playtest is off, or incomplete, for the thread's project. */
  "playtest_off",
  "invalid",
  /** The thread has no session that is still on. */
  "no_session",
  /** A step of this session runs, or the session is claiming or ending. */
  "busy",
  /** The thread or its project cannot be read, or is gone. */
  "thread_unavailable",
  "store_unavailable",
]);
export type PlaytestFailureCode = typeof PlaytestFailureCode.Type;

/** T3 refused before any step ran. A step's own answer is a result, never this. */
export class PlaytestFailure extends Schema.TaggedError<PlaytestFailure>()("PlaytestFailure", {
  code: PlaytestFailureCode,
  message: Schema.String,
}) {}

/** What the `playtest` tool does: claim a target, run one round, show the session, release it. */
export const PlaytestToolAction = Schema.Literals(["start", "push", "status", "end"]);
export type PlaytestToolAction = typeof PlaytestToolAction.Type;

/**
 * The `playtest` tool's input. Flat on purpose (provider tool schemas must be one object). No
 * thread and no setting: the thread is the caller's own, from its MCP credential, and the steps
 * are the person's settings. `target` and `note` belong to `start` only; the service checks them.
 */
export const PlaytestToolInput = Schema.Struct({
  action: PlaytestToolAction,
  target: Schema.optionalKey(
    Schema.String.annotate({
      description: "start only: the test target to claim.",
    }),
  ),
  note: Schema.optionalKey(
    Schema.String.annotate({ description: "start only: why, in one line." }),
  ),
}).check(
  Schema.makeFilter((input) =>
    input.action === "start"
      ? input.target === undefined
        ? "start needs a target."
        : undefined
      : input.target !== undefined || input.note !== undefined
        ? `${input.action} takes no target or note: it acts on this thread's session.`
        : undefined,
  ),
);
export type PlaytestToolInput = typeof PlaytestToolInput.Type;

const { adapter: _adapter, ...sessionViewFields } = PlaytestSessionRecord.fields;
/** A session as the tool shows it: without the adapter's steps, with the step kind running now. */
export const PlaytestSessionView = Schema.Struct({
  ...sessionViewFields,
  running: Schema.optionalKey(Schema.Literals(["claim", "round", "release"])),
});
export type PlaytestSessionView = typeof PlaytestSessionView.Type;

/**
 * What the service answered. start: `started`, `already-on` (this thread's session for that target
 * was on), `refused` (the claim said no), `failed`, `stopped` (ended while claiming). push: `done`,
 * `refused`, `failed`, `stopped`. status: `on`, `ended` (the last one), `none`. end: `ended`,
 * `nothing-to-end`, `release-failed` (still ending; T3 tries again).
 */
export const PlaytestToolOutcome = Schema.Literals([
  "started",
  "already-on",
  "refused",
  "failed",
  "stopped",
  "done",
  "on",
  "ended",
  "none",
  "nothing-to-end",
  "release-failed",
]);
export type PlaytestToolOutcome = typeof PlaytestToolOutcome.Type;

export const PlaytestToolResult = Schema.Struct({
  action: PlaytestToolAction,
  outcome: PlaytestToolOutcome,
  /** One plain sentence or two: what happened and what to do next. */
  message: Schema.String,
  session: Schema.NullOr(PlaytestSessionView),
});
export type PlaytestToolResult = typeof PlaytestToolResult.Type;

/**
 * The web app's two playtest calls (the thread chip, F5): `GET` the thread's session that is on
 * (read scope) and `POST` its end with `{ threadId }` (operate scope). End is the tool's `end`
 * action with the reason `person`.
 */
export const PLAYTEST_STATE_PATH = "/api/fork/playtest/state";
export const PLAYTEST_END_PATH = "/api/fork/playtest/end";

/** The thread's session that is not ended, else null (none, the last one ended, or not readable). */
export const PlaytestRouteState = Schema.Struct({ session: Schema.NullOr(PlaytestSessionView) });
export type PlaytestRouteState = typeof PlaytestRouteState.Type;

/**
 * What a press of End did: as the tool's `end` (`ended`, `nothing-to-end`, `release-failed`), or
 * `unavailable` when T3 could not try (its playtest file cannot be read now), with why.
 */
export const PlaytestRouteEndAnswer = Schema.Struct({
  outcome: Schema.Literals(["ended", "nothing-to-end", "release-failed", "unavailable"]),
  session: Schema.NullOr(PlaytestSessionView),
  message: Schema.optionalKey(Schema.String),
});
export type PlaytestRouteEndAnswer = typeof PlaytestRouteEndAnswer.Type;
