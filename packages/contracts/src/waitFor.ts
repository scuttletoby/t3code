/**
 * Fork: waits (`wait_for`) and agent slots. An agent registers a condition and ends its turn; the
 * server judges it and sends one message when it is met, times out or fails. Agent slots limit how
 * many agents `delegate_task` may start. Both are off by default and change nothing while off.
 *
 * This file holds the settings, the stored record and the tool schemas only.
 */
import * as Effect from "effect/Effect";
import * as Option from "effect/Option";
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
import { ProviderInstanceId } from "./providerInstance.ts";

const positiveIntDefault = (value: number) =>
  PositiveInt.pipe(Schema.withDecodingDefault(Effect.succeed(value)));
const nonNegativeIntDefault = (value: number) =>
  NonNegativeInt.pipe(Schema.withDecodingDefault(Effect.succeed(value)));

/** Server-wide wait settings. `enabled: false` judges and delivers nothing; list and cancel work. */
export const WaitForSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /** Deadline when the caller gives none. */
  defaultTimeoutHours: positiveIntDefault(24),
  /** Largest deadline a caller may ask for. */
  maxTimeoutHours: positiveIntDefault(168),
  /**
   * Open waits per thread. The server also holds at most 1000 open waits in all, and keeps at most
   * 2 × this many ended waits per thread and 1000 in all (the oldest go first).
   */
  maxPerThread: positiveIntDefault(20),
  fileSweepSeconds: positiveIntDefault(5),
  unitSweepSeconds: positiveIntDefault(15),
  /** How often one pull request is read. */
  prSweepSeconds: positiveIntDefault(120),
  /** A head with no checks at all is reported as `no_checks` after this long. */
  noChecksGraceSeconds: nonNegativeIntDefault(900),
  /** A wait whose source fails this long without one good read ends as `check_error`. */
  errorGraceSeconds: nonNegativeIntDefault(900),
  /**
   * Ended waits stay listed this long, then are removed; sooner beyond the caps on `maxPerThread`.
   * The store file is never written above 8 MiB.
   */
  keepEndedHours: nonNegativeIntDefault(24),
  /**
   * A thread a person stopped is not woken. Still stopped this long after it was first seen
   * stopped, its wake goes to its parent instead.
   */
  stoppedHandOverHours: positiveIntDefault(24),
});
export type WaitForSettings = typeof WaitForSettings.Type;

const WAIT_CHECK_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/u;
const DEFAULT_WAIT_CHECK_ARG_PATTERN = "^[A-Za-z0-9._:/@=+-]{1,200}$";

export const WaitCheckName = Schema.String.check(Schema.isPattern(WAIT_CHECK_NAME_PATTERN));
export type WaitCheckName = typeof WaitCheckName.Type;

const compiles = (pattern: string): boolean => {
  try {
    return new RegExp(pattern, "u") instanceof RegExp;
  } catch {
    return false;
  }
};

/**
 * One check command. It runs as an argument list without a shell: exit 0 = met, 1 = not yet,
 * anything else = error. Only settings can add or change one.
 */
export const WaitCheck = Schema.Struct({
  /** Program and fixed arguments. */
  argv: Schema.Array(TrimmedNonEmptyString).check(Schema.isMinLength(1)),
  /** How many arguments an agent may add. */
  maxArgs: NonNegativeInt.check(Schema.isLessThanOrEqualTo(20)).pipe(
    Schema.withDecodingDefault(Effect.succeed(0)),
  ),
  /** Every added argument must match all of it; one starting with `-` is always refused. */
  argPattern: Schema.String.check(
    Schema.makeFilter(compiles, { expected: "a valid regular expression" }),
  ).pipe(Schema.withDecodingDefault(Effect.succeed(DEFAULT_WAIT_CHECK_ARG_PATTERN))),
  intervalSeconds: Schema.Int.check(Schema.isGreaterThanOrEqualTo(5)).pipe(
    Schema.withDecodingDefault(Effect.succeed(30)),
  ),
  timeoutSeconds: Schema.Int.check(Schema.isBetween({ minimum: 1, maximum: 600 })).pipe(
    Schema.withDecodingDefault(Effect.succeed(20)),
  ),
  description: TrimmedString.check(Schema.isMaxLength(500)).pipe(
    Schema.withDecodingDefault(Effect.succeed("")),
  ),
});
export type WaitCheck = typeof WaitCheck.Type;

/**
 * By name. A project override replaces the whole map. Strict: a bad name or entry fails the decode,
 * so a save (settings patch) refuses it. Reading the settings file uses
 * {@link WaitChecksFromSettingsFile} instead.
 */
export const WaitChecks = Schema.Record(Schema.String, WaitCheck).check(
  Schema.makeFilter(
    (checks: { readonly [name: string]: unknown }) =>
      Object.keys(checks).every((name) => WAIT_CHECK_NAME_PATTERN.test(name)),
    { expected: "check names of lower-case letters, digits and dashes (at most 40)" },
  ),
);
export type WaitChecks = typeof WaitChecks.Type;

const decodeWaitCheckOption = Schema.decodeUnknownOption(WaitCheck);

/**
 * The settings file's check map. One bad entry (name, field or pattern) is dropped with a warning
 * and the rest load, so a typo in a hand-edited file cannot fail the whole settings decode. A
 * dropped check is answered as `unknown_check` with the list of names, and the next settings save
 * leaves it out of the file, like upstream's forward-compatible fields.
 */
export const WaitChecksFromSettingsFile = Schema.Record(Schema.String, Schema.Unknown).pipe(
  Schema.decodeTo(
    Schema.toType(WaitChecks),
    SchemaTransformation.transformEffect({
      decode: Effect.fn("WaitChecksFromSettingsFile.decode")(function* (raw: {
        readonly [name: string]: unknown;
      }) {
        const checks: Record<string, WaitCheck> = {};
        const dropped: Array<string> = [];
        for (const [name, value] of Object.entries(raw)) {
          const check = WAIT_CHECK_NAME_PATTERN.test(name)
            ? decodeWaitCheckOption(value)
            : Option.none();
          if (Option.isSome(check)) checks[name] = check.value;
          else dropped.push(name);
        }
        if (dropped.length > 0) {
          yield* Effect.logWarning(
            "settings: invalid wait checks dropped; the next settings save removes them from the file",
            { dropped },
          );
        }
        return checks as WaitChecks;
      }),
      encode: (checks) => Effect.succeed(checks as Record<string, unknown>),
    }),
  ),
);

/** Server-wide agent slot limits. `enabled: false` leaves `delegate_task` as upstream's. */
export const AgentSlotsSettings = Schema.Struct({
  enabled: Schema.Boolean.pipe(Schema.withDecodingDefault(Effect.succeed(false))),
  /** Most running agents on this server; 0 = no limit. */
  maxPerMachine: nonNegativeIntDefault(0),
  /** Most running agents per provider instance; a missing instance has no limit. */
  maxPerAccount: Schema.Record(ProviderInstanceId, NonNegativeInt).pipe(
    Schema.withDecodingDefault(Effect.succeed({})),
  ),
  /** Refuse a new child below this much available memory; 0 = off. */
  minFreeMemoryMb: nonNegativeIntDefault(0),
  /** How long a woken slot wait keeps its slot. */
  reserveSeconds: nonNegativeIntDefault(120),
  /** How long an accepted delegation counts as running before its run is visible. */
  startGraceSeconds: nonNegativeIntDefault(30),
});
export type AgentSlotsSettings = typeof AgentSlotsSettings.Type;

const PositiveIntPatch = Schema.optionalKey(PositiveInt);
const NonNegativeIntPatch = Schema.optionalKey(NonNegativeInt);

/** A field left out keeps its stored value. */
export const WaitForSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
  defaultTimeoutHours: PositiveIntPatch,
  maxTimeoutHours: PositiveIntPatch,
  maxPerThread: PositiveIntPatch,
  fileSweepSeconds: PositiveIntPatch,
  unitSweepSeconds: PositiveIntPatch,
  prSweepSeconds: PositiveIntPatch,
  noChecksGraceSeconds: NonNegativeIntPatch,
  errorGraceSeconds: NonNegativeIntPatch,
  keepEndedHours: NonNegativeIntPatch,
  stoppedHandOverHours: PositiveIntPatch,
} satisfies Record<keyof WaitForSettings, unknown>);
export type WaitForSettingsPatch = typeof WaitForSettingsPatch.Type;

/** A field left out keeps its stored value; `maxPerAccount` is replaced whole. */
export const AgentSlotsSettingsPatch = Schema.Struct({
  enabled: Schema.optionalKey(Schema.Boolean),
  maxPerMachine: NonNegativeIntPatch,
  maxPerAccount: Schema.optionalKey(Schema.Record(ProviderInstanceId, NonNegativeInt)),
  minFreeMemoryMb: NonNegativeIntPatch,
  reserveSeconds: NonNegativeIntPatch,
  startGraceSeconds: NonNegativeIntPatch,
} satisfies Record<keyof AgentSlotsSettings, unknown>);
export type AgentSlotsSettingsPatch = typeof AgentSlotsSettingsPatch.Type;

export const WaitKind = Schema.Literals(["pr", "file", "unit", "check", "slot"]);
export type WaitKind = typeof WaitKind.Type;

export const WaitPrUntil = Schema.Literals(["checks", "merged"]);
export const WaitFileUntil = Schema.Literals(["exists", "gone"]);
export const WaitUnitScope = Schema.Literals(["system", "user"]);

const PrNumber = PositiveInt;
const PrRepository = TrimmedNonEmptyString.check(Schema.isMaxLength(200));
const FilePath = Schema.String.check(Schema.isMinLength(1), Schema.isMaxLength(1000));
const UnitName = Schema.String.check(Schema.isMinLength(1), Schema.isMaxLength(200));
const CheckArg = Schema.String.check(Schema.isMaxLength(1000));
const SlotAccount = ProviderInstanceId;

/** Stored form: every default filled in, so the same condition always gives the same key. */
export const WaitPrSpec = Schema.Struct({
  number: PrNumber,
  repository: Schema.optionalKey(PrRepository),
  until: WaitPrUntil,
});
export const WaitFileSpec = Schema.Struct({ path: FilePath, until: WaitFileUntil });
export const WaitUnitSpec = Schema.Struct({ name: UnitName, scope: WaitUnitScope });
export const WaitCheckSpec = Schema.Struct({
  name: WaitCheckName,
  args: Schema.Array(CheckArg),
});
export const WaitSlotSpec = Schema.Struct({ account: Schema.optionalKey(SlotAccount) });

export const WaitTarget = Schema.Union([
  Schema.Struct({ kind: Schema.Literal("pr"), spec: WaitPrSpec }),
  Schema.Struct({ kind: Schema.Literal("file"), spec: WaitFileSpec }),
  Schema.Struct({ kind: Schema.Literal("unit"), spec: WaitUnitSpec }),
  Schema.Struct({ kind: Schema.Literal("check"), spec: WaitCheckSpec }),
  Schema.Struct({ kind: Schema.Literal("slot"), spec: WaitSlotSpec }),
]);
export type WaitTarget = typeof WaitTarget.Type;

/** `w_` plus 10 random base32 characters; never built from the event. */
export const WaitId = Schema.String.check(Schema.isPattern(/^w_[a-z2-7]{10}$/u));
export type WaitId = typeof WaitId.Type;

export const WaitState = Schema.Literals([
  "waiting",
  "delivering",
  "delivered",
  "cancelled",
  /** Gone (archived or deleted) and no parent could take the wake. */
  "thread_gone",
  /** Stopped past `stoppedHandOverHours` and no parent could take the wake. */
  "thread_stopped",
  /** Still refused by the thread long after its deadline. */
  "dropped",
]);
export type WaitState = typeof WaitState.Type;

export const WaitOutcome = Schema.Literals([
  "passed",
  "failed",
  "needs_action",
  "no_checks",
  "merged",
  "closed",
  "exists",
  "gone",
  "ended",
  "met",
  "free",
  "timeout",
  "check_error",
]);
export type WaitOutcome = typeof WaitOutcome.Type;

export const WaitResult = Schema.Struct({
  outcome: WaitOutcome,
  detail: Schema.String,
  at: IsoDateTime,
});
export type WaitResult = typeof WaitResult.Type;

const WaitRecordFields = {
  id: WaitId,
  /** The thread the message goes to; after a hand-over, the parent that took it. */
  threadId: ThreadId,
  projectId: ProjectId,
  /** 16 hex characters over thread, kind and spec; one open wait per key and thread. */
  key: Schema.String.check(Schema.isPattern(/^[0-9a-f]{16}$/u)),
  note: Schema.optionalKey(Schema.String.check(Schema.isMaxLength(200))),
  createdAt: IsoDateTime,
  deadlineAt: IsoDateTime,
  state: WaitState,
  /** Frozen when the wait leaves `waiting`. */
  result: Schema.optionalKey(WaitResult),
  /** Delivery attempts whose command was refused; part of the command id. */
  attempt: NonNegativeInt,
  /** Message id of the first send to the current receiver; every retry reuses it. */
  messageId: Schema.optionalKey(TrimmedNonEmptyString),
  /** First failed read of the current run of failures. */
  errorSince: Schema.optionalKey(IsoDateTime),
  lastError: Schema.optionalKey(Schema.String),
  /** Slot waits only: the slot is held for this thread until then. */
  reservedUntil: Schema.optionalKey(IsoDateTime),
  endedAt: Schema.optionalKey(IsoDateTime),
  /** The receiver's parent, kept at registration so a deleted child still reaches it. */
  parentThreadId: Schema.optionalKey(ThreadId),
  /** Threads that could not take the wake, oldest first; the first one registered it. */
  via: Schema.optionalKey(Schema.Array(ThreadId)),
  /** Why the last hand-over happened. */
  handedOverBecause: Schema.optionalKey(Schema.Literals(["gone", "stopped"])),
  /** When the receiver was first seen stopped; cleared when it runs again. */
  stoppedSince: Schema.optionalKey(IsoDateTime),
};

export const WaitRecord = Schema.Union([
  Schema.Struct({ ...WaitRecordFields, kind: Schema.Literal("pr"), spec: WaitPrSpec }),
  Schema.Struct({ ...WaitRecordFields, kind: Schema.Literal("file"), spec: WaitFileSpec }),
  Schema.Struct({ ...WaitRecordFields, kind: Schema.Literal("unit"), spec: WaitUnitSpec }),
  Schema.Struct({ ...WaitRecordFields, kind: Schema.Literal("check"), spec: WaitCheckSpec }),
  Schema.Struct({ ...WaitRecordFields, kind: Schema.Literal("slot"), spec: WaitSlotSpec }),
]);
export type WaitRecord = typeof WaitRecord.Type;

const WAIT_STORE_VERSION = 1;

/**
 * A message the server still owes a thread whose waits were lost in a store file that could not be
 * read. Kept in the file until it is sent, so a restart before the send does not lose it.
 */
export const WaitStoreNotice = Schema.Struct({
  threadId: ThreadId,
  messageId: TrimmedNonEmptyString,
  text: Schema.String,
});
export type WaitStoreNotice = typeof WaitStoreNotice.Type;

/** `<stateDir>/waits.json`. A file with a higher version is neither read nor written. */
export const WaitStoreFile = Schema.Struct({
  version: Schema.Literal(WAIT_STORE_VERSION),
  waits: Schema.Array(WaitRecord),
  notices: Schema.optionalKey(Schema.Array(WaitStoreNotice)),
});
export type WaitStoreFile = typeof WaitStoreFile.Type;

/** Flat on purpose: provider tool schemas must be one object, not a union. */
export const WaitForInput = Schema.Struct({
  kind: WaitKind,
  pr: Schema.optionalKey(
    Schema.Struct({
      number: PrNumber,
      repository: Schema.optionalKey(PrRepository),
      until: Schema.optionalKey(WaitPrUntil),
    }),
  ),
  file: Schema.optionalKey(
    Schema.Struct({ path: FilePath, until: Schema.optionalKey(WaitFileUntil) }),
  ),
  unit: Schema.optionalKey(
    Schema.Struct({ name: UnitName, scope: Schema.optionalKey(WaitUnitScope) }),
  ),
  check: Schema.optionalKey(
    Schema.Struct({
      name: TrimmedNonEmptyString.check(Schema.isMaxLength(40)),
      args: Schema.optionalKey(Schema.Array(CheckArg).check(Schema.isMaxLength(20))),
    }),
  ),
  slot: Schema.optionalKey(Schema.Struct({ account: Schema.optionalKey(SlotAccount) })),
  timeoutMinutes: Schema.optionalKey(PositiveInt),
  note: Schema.optionalKey(Schema.String.check(Schema.isMaxLength(200))),
});
export type WaitForInput = typeof WaitForInput.Type;

export const WaitForResult = Schema.Union([
  Schema.Struct({
    state: Schema.Literal("waiting"),
    waitId: WaitId,
    deadlineAt: IsoDateTime,
    deduped: Schema.optionalKey(Schema.Boolean),
    next: Schema.String,
  }),
  Schema.Struct({
    state: Schema.Literal("met"),
    outcome: WaitOutcome,
    detail: Schema.String,
    next: Schema.String,
  }),
]);
export type WaitForResult = typeof WaitForResult.Type;

export const WaitListInput = Schema.Struct({
  scope: Schema.optionalKey(Schema.Literals(["thread", "project"])),
  includeEnded: Schema.optionalKey(Schema.Boolean),
});
export type WaitListInput = typeof WaitListInput.Type;

const SlotUse = Schema.Struct({ used: NonNegativeInt, max: Schema.NullOr(NonNegativeInt) });

export const WaitListResult = Schema.Struct({
  enabled: Schema.Boolean,
  /** Set when the store cannot be read, for example a file from a newer build. */
  storeError: Schema.optionalKey(Schema.String),
  waits: Schema.Array(
    Schema.Struct({
      waitId: WaitId,
      threadId: ThreadId,
      kind: WaitKind,
      target: Schema.String,
      state: WaitState,
      outcome: Schema.optionalKey(WaitOutcome),
      createdAt: IsoDateTime,
      deadlineAt: IsoDateTime,
      lastError: Schema.optionalKey(Schema.String),
      via: Schema.optionalKey(Schema.Array(ThreadId)),
    }),
  ),
  checks: Schema.Array(
    Schema.Struct({ name: WaitCheckName, description: Schema.String, maxArgs: NonNegativeInt }),
  ),
  /** Only when agent slots are on. */
  slots: Schema.optionalKey(
    Schema.Struct({
      machine: SlotUse,
      accounts: Schema.Array(Schema.Struct({ id: ProviderInstanceId, ...SlotUse.fields })),
      freeMemoryMb: Schema.NullOr(NonNegativeInt),
    }),
  ),
});
export type WaitListResult = typeof WaitListResult.Type;

/** Exactly one of `waitId` and `threadId`; the handler refuses both or neither. */
export const WaitCancelInput = Schema.Struct({
  waitId: Schema.optionalKey(WaitId),
  threadId: Schema.optionalKey(ThreadId),
});
export type WaitCancelInput = typeof WaitCancelInput.Type;

export const WaitCancelResult = Schema.Struct({
  cancelled: Schema.Array(WaitId),
  notCancelled: Schema.Array(Schema.Struct({ waitId: WaitId, state: WaitState })),
});
export type WaitCancelResult = typeof WaitCancelResult.Type;

export const WaitToolFailureCode = Schema.Literals([
  "waits_off",
  "invalid",
  "unknown_check",
  "too_many",
  "not_supported",
  "not_found",
  "store_unavailable",
]);
export type WaitToolFailureCode = typeof WaitToolFailureCode.Type;

export class WaitToolFailure extends Schema.TaggedError<WaitToolFailure>()("WaitToolFailure", {
  code: WaitToolFailureCode,
  message: Schema.String,
}) {}
