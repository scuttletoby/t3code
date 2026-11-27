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

/**
 * `none` (the default): parts 1, 3 and 4 only, as before the sources existed. `command`: a program
 * prints the handoff. `agent`: the old session writes it in one turn, only when set.
 */
export const HandoffSource = Schema.Literals(["none", "command", "agent"]);
export type HandoffSource = typeof HandoffSource.Type;

/** At most this many per-thread modes. */
export const HANDOFF_THREADS_MAX = 500;
/** No handoff of a smaller thread, not even when a person asks. */
export const HANDOFF_MIN_TOKENS = 20_000;
/** No handoff size above this; a save of a larger one is refused. */
export const HANDOFF_MAX_TOKENS = 10_000_000;

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
  // Not `agent`: settings saved before the sources existed must not start write turns unasked.
  source: HandoffSource.pipe(Schema.withDecodingDefault(Effect.succeed("none"))),
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
  /**
   * The mode of a thread that holds a role (`threadRoles`: the assistant, or its project's
   * orchestrator); wins over `mode`, a thread's own mode wins over it. Not set: `mode`.
   */
  roles: Schema.optionalKey(HandoffMode),
});

const STRUCT_DEFAULTS = Schema.decodeSync(HandoffSettingsStruct)({});

/**
 * Handoff settings: a bad value or `source: command` without a command fails. A save uses
 * {@link HandoffSettingsPatch} and reading the settings file {@link HandoffSettingsFromSettingsFile}:
 * both also refuse a field this build does not know.
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

/** Off, no thread modes, no source (T3's state only), 150K backstop, 75K idle. */
export const DEFAULT_HANDOFF_SETTINGS: HandoffSettings = Schema.decodeSync(HandoffSettings)({});

const decodeHandoffSettingsOption = Schema.decodeUnknownOption(HandoffSettings);
/** Also refuses an unknown field, nested ones too: a typo would else be dropped quietly. */
const isStrictHandoffSettings = (raw: unknown) =>
  decodeHandoffSettingsOption(raw, { onExcessProperty: "error" })._tag === "Some";

/** The value is already checked; the settings writer saves it sparse. */
const checkedHandoffSettings = SchemaTransformation.transformEffect({
  decode: (raw: unknown) => Effect.succeed(raw as typeof HandoffSettings.Encoded),
  encode: (settings: typeof HandoffSettings.Encoded) => Effect.succeed(settings as unknown),
});

/** A save's value: refused when bad, also for a field this build does not know. */
export const HandoffSettingsPatch = Schema.Unknown.check(
  Schema.makeFilter(isStrictHandoffSettings, {
    expected: "handoff settings with only the fields this build knows",
  }),
).pipe(Schema.decodeTo(HandoffSettings, checkedHandoffSettings));

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
        // Strict about unknown fields: a newer build's field may narrow what it does.
        if (isStrictHandoffSettings(raw)) {
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
 * bad one reads as off, never as the server's). A thread's own mode wins over every `mode`: the
 * project's entry for it first, then the server's. Next, for a thread that holds a role
 * (`holdsRole`), the settings' `roles`; else their `mode`.
 */
export function resolveHandoffSettings(
  server: HandoffSettings,
  project: HandoffSettings | null | undefined,
  threadId: string,
  holdsRole = false,
): { readonly settings: HandoffSettings; readonly mode: HandoffMode } {
  const settings = project ?? server;
  const own = (value: HandoffSettings | null | undefined) =>
    value !== null && value !== undefined && Object.hasOwn(value.threads, threadId)
      ? value.threads[threadId]
      : undefined;
  return {
    settings,
    mode: own(project) ?? own(server) ?? (holdsRole ? settings.roles : undefined) ?? settings.mode,
  };
}

/**
 * Whether a thread holds a role in `threadRoles`: it is the assistant, or its own project's
 * orchestrator. The caller checks the thread is there and not archived (the handoff's read does).
 */
export function holdsThreadRole(
  roles: {
    readonly assistant: string | null;
    readonly orchestrators: { readonly [projectId: string]: string };
  },
  thread: { readonly id: string; readonly projectId: string },
): boolean {
  return (
    roles.assistant === thread.id ||
    (Object.hasOwn(roles.orchestrators, thread.projectId) &&
      roles.orchestrators[thread.projectId] === thread.id)
  );
}

/** The web app's handoff routes (fork/handoff/handoffRoutes.ts on the server). */
export const HANDOFF_STATE_PATH = "/api/fork/handoff/state";
export const HANDOFF_NOW_PATH = "/api/fork/handoff/now";

/** What keeps a due handoff from going: the "handoff waiting on" chip names each. */
export type HandoffRouteBlocker =
  | { readonly kind: "run-active" }
  | { readonly kind: "request-pending" }
  | { readonly kind: "child-result-waiting" }
  | { readonly kind: "fork-or-merge" }
  | { readonly kind: "background-work"; readonly detail: string }
  | { readonly kind: "goal-active" };

/**
 * A press of "Hand off now", or what one would get: off, started (or already going), skipped with
 * the decision's reason, or waiting on what keeps the thread busy (nothing is queued then).
 */
export type HandoffRouteAnswer =
  | { readonly kind: "off" }
  | { readonly kind: "started" }
  | { readonly kind: "skip"; readonly reason: string }
  | { readonly kind: "wait"; readonly blockers: ReadonlyArray<HandoffRouteBlocker> };

/** A thread's handoff for the thread menu and the chip. */
export interface HandoffRouteState {
  readonly on: boolean;
  /** The last turn's size; null when the provider reports none. */
  readonly usedTokens: number | null;
  readonly ask: HandoffRouteAnswer;
  /** What a due handoff of the latest turn waits on now; empty when none waits. */
  readonly waitingOn: ReadonlyArray<HandoffRouteBlocker>;
}
