/**
 * Fork (waits): register, list, cancel, judge, deliver, prune and lost-wait notices over one store
 * file. A change is kept in memory only after its write. With waits off, nothing is read or written
 * except by list and cancel, which never repair. A cancel between a judge and its apply wins. A
 * message's id is written before its first send, so a retry after a crash finds it again.
 */
import type { ProjectId, ServerSettings, ThreadId } from "@t3tools/contracts";
import {
  type WaitCancelResult,
  type WaitForInput,
  type WaitForResult,
  type WaitCheck,
  type WaitForSettings,
  type WaitKind,
  type WaitListResult,
  type WaitRecord,
  type WaitSlotSpec,
  type WaitStoreNotice,
  type WaitTarget,
  WaitToolFailure,
  type WaitToolFailureCode,
} from "@t3tools/contracts/waitFor";
import * as HostProcess from "@t3tools/shared/HostProcess";
import {
  effectiveSlotLimits,
  slotRefusalReason,
  type SlotReservation,
} from "@t3tools/shared/agentSlots";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import {
  boundHeldSlot,
  capEndedWaits,
  deliveryCommandId,
  type DeliveryGroup,
  deliveryGroups,
  deliveryRetryDelayMs,
  ENDED_PER_THREAD_FACTOR,
  joinedWakeMessage,
  type Judgement,
  limitErrorWakes,
  MAX_ENDED_WAITS,
  MAX_HAND_OVERS,
  MAX_OPEN_WAITS,
  messageText,
  nextWaitState,
  shouldPruneWait,
  validateWaitSpec,
  waitDeadline,
  type WaitEvent,
  waitKey,
  type WaitStateSettings,
  waitTargetText,
  type WaitTransition,
} from "@t3tools/shared/waitFor";
import * as Cause from "effect/Cause";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Latch from "effect/Latch";
import * as Option from "effect/Option";
import * as Semaphore from "effect/Semaphore";

import { ServerSettingsService } from "../serverSettings.ts";
import type { SlotGate, SlotGrant } from "../slots/SlotGate.ts";
import type { WaitDelivery, WaitDeliveryResult } from "./WaitDelivery.ts";
import {
  type CheckLookup,
  type CheckPlace,
  FILE_CHILD_LIMIT_MS,
  lookUpCheck,
  makeChildFileJudge,
  makeFileJudge,
  type WaitJudge,
  type WaitJudgeContext,
  type WaitJudges,
} from "./WaitJudges.ts";
import * as WaitStore from "./WaitStore.ts";
import { ARG_PATTERN_LIMIT_MS, testArgPattern } from "./argPattern.ts";

/**
 * At registration a judge that has not answered by then counts as "not yet" (R5). It is the whole
 * time register spends on its check: waiting for a free place and for the answer.
 */
const REGISTER_JUDGE_LIMIT_MS = 3_000;
/** At most this many judges, and this many sends, run at once. */
const JUDGE_CONCURRENCY = 4;
/**
 * A judge in a sweep that has not answered by then counts as a failed read (the error grace and
 * the deadline still end the wait), and the other waits go on. A check has its own limit (see
 * `judgeLimitMs`).
 */
function judgeLimit(kind: WaitKind): Duration.Duration {
  return kind === "file" ? Duration.seconds(10) : Duration.seconds(60);
}
/** A check process is killed at its `timeoutSeconds`; one still running this much later is stuck. */
const CHECK_KILL_GRACE_MS = 5_000;
/**
 * A pass waits for answers at most this long after it started. A check may run up to 10 minutes;
 * one that runs longer than this goes on in its place, and a later pass takes its answer, so a
 * slow check never holds up the other waits' messages. Every other judge answers within it.
 */
const SWEEP_ANSWER_WINDOW_MS = 70_000;
/**
 * Checks of one kind running at once. A stuck `stat` holds one of Node's 4 I/O threads, so file
 * checks use at most 2 (then a short-lived process); a stopped one keeps its place until its `stat`
 * returns. Other kinds answer "not yet" when all are late.
 * A thread runs at most one `check` at a time, so one agent cannot hold every place.
 */
const MAX_RUNNING_JUDGES_PER_KIND = 2;
/** Where a check runs is worked out within this; later is a failed read, tried again. */
const PLACE_LIMIT = Duration.seconds(10);
/**
 * A sweep starts checks for this long; due waits whose kind stays full until then are judged
 * first in the next pass, so slow checks of one kind cannot stretch one pass without end.
 */
const SWEEP_START_WINDOW_MS = 10_000;
/**
 * A file check process is killed at `FILE_CHILD_LIMIT_MS` and then answers; one that has not
 * ended a little after that could not be killed, and counts as stuck.
 */
const FILE_CHILD_LATE_MS = FILE_CHILD_LIMIT_MS + 2_000;
const SEND_CONCURRENCY = 4;
/**
 * Now on the monotonic clock, in ms: when a check counts as late and how long a pass starts and
 * waits, so a wall clock set back (a time sync, a restored snapshot) never stretches them. Only
 * ever kept in memory, so a restart starts from nothing.
 */
const monotonicMillis = Effect.map(Clock.monotonicTimeNanos, (nanos) => Number(nanos / 1_000_000n));
/**
 * A send, or a look at a receiver, that has not answered by then counts as "try again later". The
 * late send is stopped in the background, and its message is not sent again while it still runs;
 * the next try looks for the message in the thread and for its receipt before it sends.
 */
const SEND_LIMIT = Duration.seconds(60);
/** The most of a lost-wait notice's text that a message shows. */
const NOTICE_TEXT_MAX = 1000;
/** A held message to a stopped thread is looked at again this often, to see it run again. */
const STOPPED_RECHECK_MS = 60_000;
/** After the store could not be read, or a notice not sent, the next try is this much later. */
const PROBLEM_RECHECK_MS = 60_000;
const WAIT_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";

/** A message to a thread, sent through a `WaitDelivery`. */
export interface WaitMessage {
  readonly threadId: ThreadId;
  readonly messageId: string;
  /** The same id on every try, so a resend after a crash is answered from the receipt. */
  readonly commandId: string;
  readonly text: string;
}

const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

const failure = (code: WaitToolFailureCode, message: string) =>
  new WaitToolFailure({ code, message });

const targetOf = (wait: WaitRecord) => ({ kind: wait.kind, spec: wait.spec }) as WaitTarget;

const isOpen = (wait: WaitRecord) => wait.state === "waiting" || wait.state === "delivering";

/** A crash as one short line for a log or a stored error. */
const causeText = (cause: Cause.Cause<unknown>) => messageText(Cause.pretty(cause), 300);

/**
 * A lost-wait notice as its receiver reads it: one line, and after a hand-over it says whose it
 * was. Its text comes from a store file, so it goes through `messageText` like every wait field.
 */
function noticeMessage(notice: WaitStoreNotice): string {
  const text = messageText(notice.text, NOTICE_TEXT_MAX);
  const last = notice.via?.at(-1);
  if (last === undefined) return text;
  const what =
    notice.handedOverBecause === "stopped"
      ? "was stopped and has not run again"
      : "is gone (archived or deleted)";
  return `Thread ${messageText(last, 200)}, which you started, ${what}, so this notice for it comes to you: ${text}`;
}

function newWaitId(taken: ReadonlySet<string>): string {
  for (;;) {
    const bytes = crypto.getRandomValues(new Uint8Array(10));
    const id = `w_${Array.from(bytes, (byte) => WAIT_ID_ALPHABET[byte % 32]).join("")}`;
    if (!taken.has(id)) return id;
  }
}

/** The check a check wait names, in the settings of the project it was registered in. */
function checkOf(
  target: WaitTarget,
  projectId: ProjectId,
  settings: ServerSettings,
): WaitCheck | undefined {
  if (target.kind !== "check") return undefined;
  const checks = resolveProjectSettings(settings, projectId).settings.waitChecks;
  return Object.hasOwn(checks, target.spec.name) ? checks[target.spec.name] : undefined;
}

/** How often a `waiting` wait is judged: a check every `intervalSeconds` of its own. */
function intervalMs(target: WaitTarget, projectId: ProjectId, settings: ServerSettings): number {
  const waitFor = settings.waitFor;
  switch (target.kind) {
    case "file":
      return waitFor.fileSweepSeconds * 1000;
    case "pr":
      return waitFor.prSweepSeconds * 1000;
    case "check": {
      const check = checkOf(target, projectId, settings);
      return (check?.intervalSeconds ?? waitFor.unitSweepSeconds) * 1000;
    }
    case "unit":
    case "slot":
      return waitFor.unitSweepSeconds * 1000;
  }
}

/** How long a judge may run before it counts as late: a check's `timeoutSeconds` plus its kill. */
function judgeLimitMs(target: WaitTarget, projectId: ProjectId, settings: ServerSettings): number {
  const check = checkOf(target, projectId, settings);
  return check === undefined
    ? Duration.toMillis(judgeLimit(target.kind))
    : check.timeoutSeconds * 1000 + CHECK_KILL_GRACE_MS;
}

const stateSettings = (settings: ServerSettings): WaitStateSettings => ({
  errorGraceSeconds: settings.waitFor.errorGraceSeconds,
  keepEndedHours: settings.waitFor.keepEndedHours,
  stoppedHandOverHours: settings.waitFor.stoppedHandOverHours,
  // Slots off or without a limit: a woken slot wait holds nothing.
  reserveSeconds:
    effectiveSlotLimits(settings.agentSlots) === null ? 0 : settings.agentSlots.reserveSeconds,
});

/** A slot wait's answer when the gate gave or refused a slot (`atRegister`: the call's own answer). */
const slotJudgement = (
  grant: SlotGrant,
  settings: ServerSettings,
  atRegister: boolean,
): Judgement => {
  const seconds = settings.agentSlots.reserveSeconds;
  switch (grant.type) {
    case "off":
      return {
        type: "met",
        outcome: "free",
        detail: "Agent slots are off, so delegate_task is not held back.",
      };
    case "granted":
      return {
        type: "met",
        outcome: "free",
        detail:
          seconds === 0
            ? "Call delegate_task now."
            : atRegister
              ? `A slot is free and held for this thread for ${seconds} s: call delegate_task now.`
              : `Call delegate_task within ${seconds} s; the slot is held for this thread.`,
      };
    case "full":
      return { type: "notYet", detail: `No free slot yet: ${slotRefusalReason(grant.decision)}.` };
    case "sameThread":
      return { type: "notYet", detail: "This thread's other slot wait was just given a slot." };
    case "unread":
      return { type: "failed", message: grant.message, permanent: false };
  }
};

/**
 * The slot a delivered slot wait still holds, from its `reservedUntil`: never longer than
 * `reserveSeconds` (as set now) after its delivery, so a lowered setting shortens it too.
 */
const storedReservation = (
  wait: WaitRecord,
  now: number,
  reserveSeconds: number,
): ReadonlyArray<SlotReservation> => {
  if (wait.kind !== "slot" || wait.state !== "delivered" || wait.reservedUntil === undefined) {
    return [];
  }
  const endedMs = wait.endedAt === undefined ? Number.NaN : Date.parse(wait.endedAt);
  const untilMs = Math.min(Date.parse(wait.reservedUntil), endedMs + reserveSeconds * 1000);
  if (!(untilMs > now)) return [];
  const account = wait.spec.account;
  return [
    {
      key: wait.id,
      threadId: wait.threadId,
      untilMs,
      ...(account === undefined ? {} : { account }),
    },
  ];
};

const waitingResult = (wait: WaitRecord, deduped: boolean): WaitForResult => ({
  state: "waiting",
  waitId: wait.id,
  deadlineAt: wait.deadlineAt,
  ...(deduped ? { deduped: true } : {}),
  next: `End your turn now. One message starting with [wait ${wait.id}] arrives when this is met, times out or fails. Do not poll.`,
});

const logTransition = (transition: WaitTransition) =>
  transition.log === undefined
    ? Effect.void
    : transition.log.level === "error"
      ? Effect.logError(`waits: ${transition.log.message}`)
      : Effect.logWarning(`waits: ${transition.log.message}`);

/** The places checks run in: one per kind, and one for file checks in their own process. */
type JudgePool = WaitKind | "fileChild";

/**
 * Places whose stopped checks free their place at once (their process group is killed). Others
 * keep it until the check returns, so stuck ones never pass `MAX_RUNNING_JUDGES_PER_KIND`: a `stat`
 * in the server cannot be stopped, and a file check process on a dead mount may not die.
 */
const STOPPABLE_POOLS: ReadonlySet<JudgePool> = new Set<JudgePool>(["check", "unit"]);

/** Who a check runs for, and (check waits) which check in which project. */
interface JudgeRun {
  /** The stored wait, or the id a registration will store its wait under. */
  readonly waitId: string;
  readonly threadId: ThreadId;
  readonly target: string;
  readonly check?: {
    readonly spec: Extract<WaitTarget, { readonly kind: "check" }>["spec"];
    readonly projectId: ProjectId;
  };
}

interface RunningJudge extends JudgeRun {
  readonly pool: JudgePool;
  /** Completed to stop the check (its process group is killed); its answer is then this. */
  readonly stop: Deferred.Deferred<Judgement>;
  /** How long it may run before it counts as late. */
  readonly limitMs: number;
  /**
   * When it counts as late (stuck while it still runs), on the monotonic clock
   * (`monotonicMillis`), fixed when it starts: whoever started it may stop waiting for it at any
   * time, even be cut off before it waits at all.
   */
  readonly lateAt: number;
}

interface StartedJudge {
  readonly fiber: Fiber.Fiber<Judgement>;
  readonly entry: RunningJudge;
}

/** A started check; or none, because every place is held by a check that may still answer in time
 * (`busy`, until `wakeAt` at the latest, monotonic), or by late ones (`stuck`), or the thread already runs a
 * check (`threadBusy`). */
type JudgeStart =
  | { readonly type: "started"; readonly started: StartedJudge }
  | { readonly type: "busy"; readonly wakeAt: number }
  | { readonly type: "stuck"; readonly count: number }
  | { readonly type: "threadBusy" };

/**
 * A stored wait's judge in a sweep: its answer to wait for (null when the check goes on past the
 * pass, see `SWEEP_ANSWER_WINDOW_MS`), `busy` (see `JudgeStart`), or `skip` (its check from an
 * earlier pass is still running in time).
 */
type StoredStart =
  | { readonly type: "answer"; readonly answer: Effect.Effect<Judgement | null> }
  | { readonly type: "busy"; readonly wakeAt: number }
  | { readonly type: "skip" };

/**
 * A check that ran past its pass, or past its registration: its answer, once it came, for the next
 * pass to take. One per wait; it is the wait's only running check.
 */
interface CarriedJudge {
  readonly entry: RunningJudge;
  answer: Judgement | null;
}

const answered = (judgement: Judgement): StoredStart => ({
  type: "answer",
  answer: Effect.succeed(judgement),
});

type Loaded =
  | { readonly ok: true; readonly contents: WaitStore.WaitStoreContents }
  | { readonly ok: false; readonly reason: string };

export const make = Effect.fn("WaitService.make")(function* (input: {
  readonly filePath: string;
  readonly delivery: WaitDelivery;
  /** Replaces the built-in judge of a kind; tests use slow or scripted ones. */
  readonly judges?: WaitJudges;
  /**
   * The file check used while the file checks in the server are stuck: by default one in a
   * short-lived process (`makeChildFileJudge`); null for none.
   */
  readonly fileChildJudge?: WaitJudge<"file"> | null;
  /** Runs after a register or cancel changed the store, so the sweep looks again at once. */
  readonly onChange?: Effect.Effect<void>;
  /**
   * Where a check wait's check runs (the project the thread is in now). The service works it out
   * before each run, times the run by that project's check, and gives it to the judge. Without it
   * the check judge works it out itself, and runs are timed by the stored project.
   */
  readonly checkPlaceOf?: (context: WaitJudgeContext) => Effect.Effect<CheckPlace>;
  /**
   * The server's slot gate: it grants slot waits their slots, and counts the slots delivered slot
   * waits hold. Without it, slot waits are refused as not supported.
   */
  readonly slots?: SlotGate["Service"];
}) {
  const onChange = input.onChange ?? Effect.void;
  const settingsService = yield* ServerSettingsService;
  const platform = yield* HostProcess.Platform;
  const store = yield* WaitStore.make({ filePath: input.filePath });
  const judges: WaitJudges = { file: yield* makeFileJudge(), ...input.judges };
  const fileChildJudge =
    input.fileChildJudge === undefined ? yield* makeChildFileJudge() : input.fileChildJudge;

  /**
   * Every check runs in a fiber of this scope: when the service stops (the server stops), each is
   * interrupted, which kills its process group.
   */
  const runScope = yield* Effect.scope;
  const lock = yield* Semaphore.make(1);
  const sweepLock = yield* Semaphore.make(1);
  /** Null until the file was read; only ever changed under `lock`. */
  let current: WaitStore.WaitStoreContents | null = null;
  let lastProblem: string | null = null;
  /** When each `waiting` wait is judged next; empty after a restart, so all are judged at once. */
  const nextCheckAt = new Map<string, number>();
  /** Checks that ran past their pass, by wait id; empty after a restart (judged again). */
  const carried = new Map<string, CarriedJudge>();
  /** Check waits: the project their last run ran in, which times them. */
  const runProject = new Map<string, ProjectId>();
  /** Ids that registrations in progress will store their waits under. */
  const reserved = new Set<string>();
  /**
   * Granted slot waits whose thread started its agent before the wake was delivered (memory only;
   * after a restart such a slot counts until it ends, the safe side).
   */
  const usedBeforeDelivery = new Set<string>();
  /** Checks that have not returned, per place; late once past their own `lateAt`. */
  const running = new Map<JudgePool, Set<RunningJudge>>();
  /** Places whose checks are all stuck, already logged; cleared when one of them returns. */
  const blockedLogged = new Set<JudgePool>();
  /** Completed (and replaced) whenever a check returns. */
  let judgesChanged = yield* Deferred.make<void>();
  const signalJudgesChanged = Effect.gen(function* () {
    const previous = judgesChanged;
    judgesChanged = yield* Deferred.make<void>();
    yield* Deferred.succeed(previous, undefined);
  });
  /** When a message is tried again, by message id; empty after a restart, so all go at once. */
  const retryAt = new Map<string, number>();
  /** Tries of a message's current command id that recorded nothing, for its back-off. */
  const failures = new Map<string, number>();
  /** Threads that may have been archived or deleted, so their `waiting` waits are looked at. */
  const receiversToCheck = new Set<ThreadId>();
  /** The first sweep looks at every thread with a `waiting` wait (events missed while down). */
  let checkAllReceivers = true;
  let noticesRetryAt = 0;
  /** Sends that have not returned, by message id, each with its own token. */
  const sending = new Map<string, object>();
  /** Messages whose send crashed or ran late, already logged; cleared when the message is done. */
  const sendTroubleLogged = new Set<string>();

  const judgeOf = (kind: WaitKind) => judges[kind] as WaitJudge | undefined;

  const readSettings = settingsService.getSettings.pipe(
    Effect.mapError((error) =>
      failure("store_unavailable", `The settings cannot be read: ${error.message}`),
    ),
  );
  /** For the background steps: no settings means no work this time. */
  const readSettingsOrNull = settingsService.getSettings.pipe(
    Effect.catch((error) =>
      Effect.logWarning(`waits: the settings cannot be read: ${error.message}`).pipe(
        Effect.as(null),
      ),
    ),
  );

  /** Read again before each step that writes a result or sends, so "off" stops a running sweep. */
  const stillOn = readSettingsOrNull.pipe(
    Effect.map((settings) => settings !== null && settings.waitFor.enabled),
  );

  const logSendTrouble = (messageId: string, line: string) =>
    Effect.suspend(() => {
      if (sendTroubleLogged.has(messageId)) return Effect.void;
      sendTroubleLogged.add(messageId);
      return Effect.logError(`waits: ${line}`);
    });

  /**
   * One send, in its own fiber, within `SEND_LIMIT`. A crash or a late answer is "try again
   * later" (logged once per message), so one thread's send never holds up or stops the others. A
   * late send is stopped in the background; until it has stopped, its message is not sent again.
   */
  const send = Effect.fn("WaitService.send")(function* (message: WaitMessage) {
    if (sending.has(message.messageId)) {
      return { type: "retry", detail: "Its last send has not answered yet." } as const;
    }
    const token = {};
    sending.set(message.messageId, token);
    const fiber = yield* Effect.forkDetach(
      Effect.suspend(() => input.delivery.deliver(message)).pipe(
        Effect.catchCauseIf(
          (cause) => !Cause.hasInterruptsOnly(cause),
          (cause) =>
            logSendTrouble(
              message.messageId,
              `the send of message ${message.messageId} to thread ${message.threadId} crashed; it is tried again later: ${causeText(cause)}`,
            ).pipe(
              Effect.as<WaitDeliveryResult>({
                type: "retry",
                detail: `The send crashed: ${causeText(cause)}`,
              }),
            ),
        ),
        Effect.ensuring(
          Effect.sync(() => {
            if (sending.get(message.messageId) === token) sending.delete(message.messageId);
          }),
        ),
      ),
    );
    const answer = yield* Fiber.join(fiber).pipe(Effect.timeoutOption(SEND_LIMIT));
    if (Option.isSome(answer)) return answer.value;
    yield* Effect.forkDetach(Fiber.interrupt(fiber));
    const detail = `The send gave no answer in ${Duration.format(SEND_LIMIT)}.`;
    yield* logSendTrouble(
      message.messageId,
      `the send of message ${message.messageId} to thread ${message.threadId} gave no answer in ${Duration.format(SEND_LIMIT)}; it is stopped and tried again later.`,
    );
    return { type: "retry", detail } as const;
  });

  /** The receiver within `SEND_LIMIT`; null (look again next sweep) when late or crashed. */
  const receiverOf = (threadId: ThreadId) =>
    Effect.suspend(() => input.delivery.receiver(threadId)).pipe(
      Effect.timeoutOption(SEND_LIMIT),
      Effect.map(Option.getOrNull),
      Effect.catchCauseIf(
        (cause) => !Cause.hasInterruptsOnly(cause),
        (cause) =>
          Effect.logError(
            `waits: thread ${threadId} could not be looked at: ${causeText(cause)}`,
          ).pipe(Effect.as(null)),
      ),
    );

  const unavailable = Effect.fnUntraced(function* (reason: string) {
    if (reason !== lastProblem) {
      lastProblem = reason;
      yield* Effect.logError(`waits: ${reason}`);
    }
    return { ok: false, reason } satisfies Loaded;
  });

  /** Reads the file once. Under `lock` only. `repair` false (waits off) never changes a file. */
  const ensureLoaded = Effect.fnUntraced(function* (repair: boolean) {
    if (current !== null) return { ok: true, contents: current } satisfies Loaded;
    const nowMs = yield* Clock.currentTimeMillis;
    const result = yield* store.load({ nowMs, repair }).pipe(Effect.result);
    if (result._tag === "Failure") return yield* unavailable(result.failure.message);
    const load = result.success;
    switch (load._tag) {
      case "Newer":
        return yield* unavailable(
          `${store.filePath} is from a newer version (version ${load.version}); this server does not read or change it.`,
        );
      case "Damaged":
        return yield* unavailable(
          `${store.filePath} cannot be read; it is repaired when waits are switched on.`,
        );
      case "Repaired": {
        const threads = load.lost.map((lost) =>
          lost.waitIds.length === 0
            ? lost.threadId
            : `${lost.threadId} (${lost.waitIds.join(", ")})`,
        );
        yield* Effect.logError(
          `waits: ${store.filePath} could not be read in full and was saved as ${load.copyPath}; ${load.dropped} entries dropped. Threads that lost waits: ${threads.length === 0 ? "none found" : threads.join("; ")}.`,
        );
        if (load.writeError !== undefined) {
          yield* Effect.logWarning(
            `waits: the repaired store could not be written yet: ${load.writeError}`,
          );
        }
        break;
      }
      case "Loaded":
        break;
    }
    // Held slots are bounded as they are read (a clock that was ahead, a hand-edited file).
    const settings = yield* readSettingsOrNull;
    const reserveSeconds = settings?.agentSlots.reserveSeconds ?? 0;
    current = {
      waits: load.waits.map((wait) => boundHeldSlot(wait, nowMs, reserveSeconds)),
      notices: load.notices,
    };
    lastProblem = null;
    return { ok: true, contents: current } satisfies Loaded;
  });

  /**
   * Writes `next` and only then keeps it. Under `lock` only. Leaves out the oldest ended waits
   * beyond `ENDED_PER_THREAD_FACTOR × maxPerThread` per thread and `MAX_ENDED_WAITS` in all, and
   * beyond the store's size limit.
   */
  const commit = Effect.fnUntraced(function* (
    next: WaitStore.WaitStoreContents,
    settings: WaitForSettings,
  ) {
    const now = yield* Clock.currentTimeMillis;
    const capped = {
      ...next,
      waits: capEndedWaits(
        next.waits,
        { perThread: ENDED_PER_THREAD_FACTOR * settings.maxPerThread, total: MAX_ENDED_WAITS },
        now,
      ),
    };
    // Over the size limit the store also leaves out the oldest ended waits.
    current = yield* store.write(capped);
    // A wait that is no longer `waiting` has no running check.
    yield* stopEndedRuns;
  });

  const loadedOrFail = (repair: boolean) =>
    ensureLoaded(repair).pipe(
      Effect.flatMap((loaded) =>
        loaded.ok
          ? Effect.succeed(loaded.contents)
          : Effect.fail(failure("store_unavailable", loaded.reason)),
      ),
    );

  const commitOrFail = (next: WaitStore.WaitStoreContents, settings: WaitForSettings) =>
    commit(next, settings).pipe(
      Effect.mapError((error) => failure("store_unavailable", error.message)),
    );

  /**
   * A slot wait's answer at registration: the gate grants a free slot and holds it at once (under
   * the wait's id, which is never stored when it is met). Behind older slot waits it waits for the
   * sweep, which grants oldest first; with slots off it is met at once.
   */
  const slotAtRegister = Effect.fn("WaitService.slotAtRegister")(function* (
    gate: SlotGate["Service"],
    waitId: string,
    threadId: ThreadId,
    spec: typeof WaitSlotSpec.Type,
    queued: boolean,
  ) {
    const settings = yield* readSettings;
    if (queued && effectiveSlotLimits(settings.agentSlots) !== null) {
      return { type: "notYet", detail: "Older slot waits go first." } satisfies Judgement;
    }
    const [grant] = yield* gate.grant([{ key: waitId, threadId, account: spec.account }]);
    // Met at once, nothing to write: the slot the thread held before moves to this answer now.
    if (grant?.type === "granted") yield* gate.keep([waitId]);
    return grant === undefined
      ? ({ type: "notYet" } satisfies Judgement)
      : slotJudgement(grant, settings, true);
  });

  /** The slots delivered slot waits still hold, for the gate (it reads them only while waits are on). */
  const heldInStore = lock.withPermits(1)(
    Effect.gen(function* () {
      const loaded = yield* ensureLoaded(true);
      if (!loaded.ok) return [];
      const settings = yield* readSettingsOrNull;
      if (settings === null) return [];
      const now = yield* Clock.currentTimeMillis;
      return loaded.contents.waits.flatMap((wait) =>
        storedReservation(wait, now, settings.agentSlots.reserveSeconds),
      );
    }),
  );

  /**
   * A judge fixed a stored wait's target (`WaitJudgeContext.pin`): written only while waits are on
   * and the wait is still waiting with the target that was judged, so a cancel, an end or another
   * pin in between wins. A failed write is pinned again at the next read.
   */
  const pinStored = (id: string, judged: WaitTarget, pinned: WaitTarget) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const settings = yield* readSettingsOrNull;
        if (settings === null || !settings.waitFor.enabled) return;
        yield* pinInStore(id, judged, pinned, settings);
      }),
    );

  /** `pinStored`'s write, under `lock`. */
  const pinInStore = Effect.fnUntraced(function* (
    id: string,
    judged: WaitTarget,
    pinned: WaitTarget,
    settings: ServerSettings,
  ) {
    const loaded = yield* ensureLoaded(true);
    if (!loaded.ok) return;
    const wait = loaded.contents.waits.find((candidate) => candidate.id === id);
    if (wait === undefined || wait.state !== "waiting" || wait.kind !== pinned.kind) return;
    if (waitKey(wait.threadId, targetOf(wait)) !== waitKey(wait.threadId, judged)) return;
    const waits = loaded.contents.waits.map((candidate) =>
      candidate.id === id ? ({ ...candidate, spec: pinned.spec } as WaitRecord) : candidate,
    );
    yield* commitWaits(loaded.contents, waits, "it is pinned at its next read.", settings);
  });

  /** The gate let a holder start its agent: its wait's `reservedUntil` is cleared (row 15). */
  const useStoredSlot = (key: string) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const settings = yield* readSettingsOrNull;
        if (settings === null || !settings.waitFor.enabled) return;
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return;
        const wait = loaded.contents.waits.find((candidate) => candidate.id === key);
        if (wait === undefined) return;
        // Granted but not delivered yet: its slot is not held when the delivery is written.
        if (wait.kind === "slot" && (wait.state === "waiting" || wait.state === "delivering")) {
          usedBeforeDelivery.add(key);
          return;
        }
        const now = yield* Clock.currentTimeMillis;
        const transition = nextWaitState(wait, { type: "slotUsed" }, now, stateSettings(settings));
        if (transition === null) return;
        const waits = loaded.contents.waits.map((candidate) =>
          candidate.id === key ? transition.record : candidate,
        );
        yield* commitWaits(loaded.contents, waits, "the used slot ends by itself.", settings);
      }),
    );

  /** `wait_for`: an open wait for the same condition, a met answer, or a new stored wait. */
  const register = Effect.fn("WaitService.register")(function* (request: {
    readonly threadId: ThreadId;
    readonly projectId: ProjectId;
    /** The thread's subagent parent now, so a wait still reaches it if the thread is deleted. */
    readonly parentThreadId?: ThreadId | undefined;
    readonly input: WaitForInput;
  }) {
    const settings = yield* readSettings;
    if (!settings.waitFor.enabled) {
      return yield* failure("waits_off", "Waits are off. They are switched on in Settings.");
    }
    const { threadId, projectId, parentThreadId } = request;
    const checks = resolveProjectSettings(settings, projectId).settings.waitChecks;
    const valid = validateWaitSpec({
      input: request.input,
      checks,
      platform,
      accounts: new Set(Object.keys(settings.providerInstances)),
      // The owner's pattern runs below, in a worker with a time limit.
      testArgPattern: false,
    });
    if (!valid.ok) return yield* failure(valid.code, valid.message);
    const target = valid.target;
    const judge = judgeOf(target.kind);
    const slotGate = target.kind === "slot" ? input.slots : undefined;
    if (judge === undefined && slotGate === undefined) {
      return yield* failure("not_supported", `This server cannot wait for "${target.kind}" yet.`);
    }
    if (target.kind === "check") {
      const { name, args } = target.spec;
      const tested = yield* testArgPattern(checks[name]!.argPattern, args);
      if (tested.type === "bad") {
        return yield* failure(
          "invalid",
          // @effect-diagnostics-next-line globalJsonInEffect:off - quotes the argument for the message.
          `Argument ${JSON.stringify(messageText(tested.arg, 100))} is not allowed for check "${name}".`,
        );
      }
      if (tested.type === "slow") {
        return yield* failure(
          "invalid",
          `The argument pattern of check "${name}" gave no answer in ${ARG_PATTERN_LIMIT_MS / 1000}s. Try again later; if it keeps happening, ask the owner to make the pattern simpler.`,
        );
      }
      if (tested.type === "failed") {
        return yield* failure("invalid", `Check "${name}": ${tested.message} Try again later.`);
      }
    }
    const now = yield* Clock.currentTimeMillis;
    const deadline = waitDeadline(now, request.input.timeoutMinutes, settings.waitFor);
    if (!deadline.ok) return yield* failure(deadline.code, deadline.message);
    const key = waitKey(threadId, target);

    /** The same open condition gives the same wait (R7), even at the limit. */
    const findOrRefuse = (contents: WaitStore.WaitStoreContents) => {
      const open = contents.waits.filter((wait) => wait.threadId === threadId && isOpen(wait));
      const same = open.find((wait) => wait.key === key);
      if (same !== undefined) return Effect.succeedSome(same);
      const refusal =
        open.length >= settings.waitFor.maxPerThread
          ? `This thread already has ${open.length} open waits, the most allowed. Cancel one first.`
          : contents.waits.filter(isOpen).length >= MAX_OPEN_WAITS
            ? `The server already holds ${MAX_OPEN_WAITS} open waits, the most allowed. Try again later.`
            : null;
      return refusal === null
        ? Effect.succeed(Option.none<WaitRecord>())
        : Effect.fail(failure("too_many", refusal));
    };

    // The id is taken now, so the check started below is the wait's own run from the start.
    const first = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const contents = yield* loadedOrFail(true);
        const found = yield* findOrRefuse(contents);
        if (Option.isSome(found)) return { type: "existing", wait: found.value } as const;
        const id = newWaitId(new Set([...contents.waits.map((wait) => wait.id), ...reserved]));
        reserved.add(id);
        // Older slot waits go first: a new one waits behind them for the sweep (R17).
        const queued = contents.waits.some(
          (wait) => wait.kind === "slot" && wait.state === "waiting",
        );
        return { type: "new", id, queued } as const;
      }),
    );
    if (first.type === "existing") return waitingResult(first.wait, true);
    const waitId = first.id;
    let started: StartedJudge | null = null;
    /** Set once the stored wait owns the run; until then the run ends with the call. */
    let adopted = false;
    /** The target the judge fixed (`WaitJudgeContext.pin`) before the wait was stored. */
    let pinned: WaitTarget | null = null;

    return yield* Effect.gen(function* () {
      // Waits for a free place within its limit; with none, the next sweep judges it first.
      const until = (yield* monotonicMillis) + REGISTER_JUDGE_LIMIT_MS;
      const context: WaitJudgeContext = {
        waitId: null,
        now,
        threadId,
        projectId,
        // Stored with the wait; a run that answers after it was stored writes it then.
        pin: (next) =>
          adopted ? pinStored(waitId, target, next) : Effect.sync(() => void (pinned = next)),
      };
      // A slot wait asks the gate instead, which holds a slot it grants at once.
      started =
        slotGate !== undefined
          ? null
          : yield* startJudgeWhenFree(
              target.kind,
              {
                waitId,
                threadId,
                target: waitTargetText(target),
                ...(target.kind === "check" ? { check: { spec: target.spec, projectId } } : {}),
              },
              until,
              () => judge!(target, context),
              childJudgeOf(target, context),
              judgeLimitMs(target, projectId, settings),
            );
      const answer =
        slotGate !== undefined && target.kind === "slot"
          ? Option.some(
              yield* slotAtRegister(slotGate, waitId, threadId, target.spec, first.queued),
            )
          : started === null
            ? Option.none()
            : yield* awaitJudge(started, until);
      const judgement: Judgement = Option.getOrElse(answer, (): Judgement => ({ type: "notYet" }));
      // A wait that could only ever end in this error is refused, and nothing is stored.
      if (judgement.type === "failed" && judgement.permanent) {
        return yield* failure(
          target.kind === "unit" ? "not_supported" : "invalid",
          judgement.message,
        );
      }
      if (judgement.type === "met") {
        return {
          state: "met",
          outcome: judgement.outcome,
          detail: judgement.detail,
          next: "It is already met: nothing was stored and no message will come. Carry on.",
        } satisfies WaitForResult;
      }

      return yield* lock.withPermits(1)(
        Effect.gen(function* () {
          // Another call may have stored the same condition while this one was judging.
          const contents = yield* loadedOrFail(true);
          const again = yield* findOrRefuse(contents);
          if (Option.isSome(again)) return waitingResult(again.value, true);
          const builtWith = pinned;
          const record = {
            id: waitId,
            threadId,
            projectId,
            key,
            ...(request.input.note === undefined ? {} : { note: request.input.note }),
            createdAt: iso(now),
            deadlineAt: iso(deadline.deadlineAt),
            state: "waiting",
            attempt: 0,
            ...(parentThreadId === undefined ? {} : { parentThreadId }),
            // The key stays the asked target's, so asking the same again finds this wait.
            ...(pinned ?? target),
          } as WaitRecord;
          yield* commitOrFail(
            { ...contents, waits: [...contents.waits, record] },
            settings.waitFor,
          );
          if (target.kind === "check") runProject.set(waitId, projectId);
          if (started !== null && Option.isNone(answer)) {
            // Still running in time: it is the wait's one run, and the next pass takes its answer.
            adopted = true;
            // A pin the run made while the wait was being written is saved now, not lost until
            // its next check, which might judge a changed remote.
            if (pinned !== null && pinned !== builtWith) {
              yield* pinInStore(waitId, target, pinned, settings);
            }
            yield* carry(waitId, started);
          } else if (started !== null) {
            // Not checked here: the next sweep judges it, and before the waits it judged last time.
            nextCheckAt.set(waitId, now + intervalMs(target, projectId, settings));
          }
          yield* onChange;
          return waitingResult(record, false);
        }),
      );
    }).pipe(
      // Refused, already met, the same as a stored wait, or cut off: its run ends with the call.
      Effect.ensuring(
        Effect.suspend(() => {
          reserved.delete(waitId);
          const run: StartedJudge | null = started;
          return adopted || run === null
            ? Effect.void
            : Deferred.succeed(run.entry.stop, lateJudgement("Its wait was not stored.")).pipe(
                Effect.asVoid,
              );
        }),
      ),
    );
  });

  /** `wait_list`: the caller's waits (or its project's), its project's checks, any store problem. */
  const list = Effect.fn("WaitService.list")(function* (request: {
    readonly threadId: ThreadId;
    readonly projectId: ProjectId;
    readonly scope?: "thread" | "project" | undefined;
    readonly includeEnded?: boolean | undefined;
  }): Effect.fn.Return<WaitListResult, WaitToolFailure> {
    const settings = yield* readSettings;
    const checks = Object.entries(
      resolveProjectSettings(settings, request.projectId).settings.waitChecks,
    )
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([name, check]) => ({ name, description: check.description, maxArgs: check.maxArgs }));
    const loaded = yield* lock.withPermits(1)(ensureLoaded(settings.waitFor.enabled));
    // Outside the lock (the gate reads the store's held slots under it); only while slots are on.
    const usage =
      input.slots === undefined || !settings.agentSlots.enabled ? null : yield* input.slots.usage;
    const slots = usage === null ? {} : { slots: usage };
    if (!loaded.ok) {
      return {
        enabled: settings.waitFor.enabled,
        storeError: loaded.reason,
        waits: [],
        checks,
        ...slots,
      } satisfies WaitListResult;
    }
    const waits = loaded.contents.waits
      .filter((wait) =>
        request.scope === "project"
          ? wait.projectId === request.projectId
          : wait.threadId === request.threadId,
      )
      .filter((wait) => request.includeEnded === true || isOpen(wait))
      .map((wait) => ({
        waitId: wait.id,
        threadId: wait.threadId,
        kind: wait.kind,
        target: waitTargetText(targetOf(wait)),
        state: wait.state,
        ...(wait.result === undefined ? {} : { outcome: wait.result.outcome }),
        createdAt: wait.createdAt,
        deadlineAt: wait.deadlineAt,
        ...(wait.lastError === undefined ? {} : { lastError: wait.lastError }),
        ...(wait.via === undefined ? {} : { via: wait.via }),
      }));
    return { enabled: settings.waitFor.enabled, waits, checks, ...slots } satisfies WaitListResult;
  });

  /**
   * `wait_cancel`: one wait, or every wait of a thread in the caller's project, including waits
   * handed over from it. Only a `waiting` wait can be cancelled; once this answers, no message for
   * it is ever sent (R6).
   */
  const cancel = Effect.fn("WaitService.cancel")(function* (request: {
    readonly projectId: ProjectId;
    readonly waitId?: string | undefined;
    readonly threadId?: ThreadId | undefined;
  }) {
    const { projectId, waitId, threadId } = request;
    if ((waitId === undefined) === (threadId === undefined)) {
      return yield* failure("invalid", "Give exactly one of waitId and threadId.");
    }
    const settings = yield* readSettings;
    return yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const contents = yield* loadedOrFail(settings.waitFor.enabled);
        const matches = (wait: WaitRecord) =>
          wait.projectId === projectId &&
          (waitId !== undefined
            ? wait.id === waitId
            : wait.threadId === threadId || (wait.via ?? []).includes(threadId!));
        if (waitId !== undefined && !contents.waits.some(matches)) {
          return yield* failure("not_found", `No wait ${waitId} in this project.`);
        }
        const now = yield* Clock.currentTimeMillis;
        const cancelled: string[] = [];
        const notCancelled: Array<{ waitId: string; state: WaitRecord["state"] }> = [];
        const waits = contents.waits.map((wait) => {
          if (!matches(wait)) return wait;
          const transition = nextWaitState(wait, { type: "cancel" }, now, stateSettings(settings));
          if (transition === null) {
            notCancelled.push({ waitId: wait.id, state: wait.state });
            return wait;
          }
          cancelled.push(wait.id);
          return transition.record;
        });
        if (cancelled.length > 0) {
          yield* commitOrFail({ ...contents, waits }, settings.waitFor);
          for (const id of cancelled) {
            nextCheckAt.delete(id);
            carried.delete(id);
            runProject.delete(id);
          }
          yield* stopRuns(new Set(cancelled), lateJudgement("Its wait was cancelled."));
          yield* onChange;
        }
        return { cancelled, notCancelled } satisfies WaitCancelResult;
      }),
    );
  });

  const lateJudgement = (message: string): Judgement => ({
    type: "failed",
    message,
    permanent: false,
  });

  const runningOf = (pool: JudgePool) => {
    let set = running.get(pool);
    if (set === undefined) {
      set = new Set();
      running.set(pool, set);
    }
    return set;
  };

  /** Every running check, of every place. */
  const allRunning = () => [...running.values()].flatMap((set) => [...set]);

  /**
   * Stops the running checks of these waits (of all, for null): each answers `judgement` at once.
   * A check process has its group killed, and its slot is free once it is gone; a check in the
   * server, or a file check process, keeps its slot until it returns (see `STOPPABLE_POOLS`).
   */
  const stopRuns = (waitIds: ReadonlySet<string> | null, judgement: Judgement) =>
    Effect.forEach(
      allRunning().filter((entry) => waitIds === null || waitIds.has(entry.waitId)),
      (entry) => Deferred.succeed(entry.stop, judgement),
      { discard: true },
    );

  /**
   * Takes a free slot and starts the check in its own fiber as one step that cannot be cut off, so
   * no slot is held by a check that never started. The slot is freed when the check returns. A
   * check is late by its own start time and limit, never by its waiter, so a cut-off caller cannot
   * leave a slot that looks busy forever. The fiber lives in the service's scope.
   */
  const startJudge = (
    pool: JudgePool,
    run: JudgeRun,
    judge: () => Effect.Effect<Judgement>,
    limitMs: number,
  ): Effect.Effect<JudgeStart> =>
    Effect.uninterruptible(
      Effect.gen(function* () {
        const now = yield* monotonicMillis;
        const set = runningOf(pool);
        if (pool === "check" && [...set].some((entry) => entry.threadId === run.threadId)) {
          return { type: "threadBusy" } as const;
        }
        if (set.size >= MAX_RUNNING_JUDGES_PER_KIND) {
          const onTime = [...set].filter((entry) => entry.lateAt > now);
          return onTime.length === 0
            ? ({ type: "stuck", count: set.size } as const)
            : ({ type: "busy", wakeAt: Math.min(...onTime.map((entry) => entry.lateAt)) } as const);
        }
        const entry: RunningJudge = {
          ...run,
          pool,
          stop: yield* Deferred.make<Judgement>(),
          limitMs,
          lateAt: now + limitMs,
        };
        set.add(entry);
        const checked = Effect.suspend(judge).pipe(
          // A check that crashes is a failed read of its own wait, never of the sweep.
          Effect.catchCauseIf(
            (cause) => !Cause.hasInterruptsOnly(cause),
            (cause) => Effect.succeed(lateJudgement(`The check crashed: ${causeText(cause)}`)),
          ),
        );
        const release = Effect.suspend(() => {
          set.delete(entry);
          blockedLogged.delete(pool);
          return signalJudgesChanged;
        });
        // The check itself can still be stopped; only taking the slot and starting it is one step.
        if (STOPPABLE_POOLS.has(pool)) {
          const fiber = yield* Effect.forkIn(
            // Stopped: the judge is interrupted, which kills its process group, and frees the slot.
            Effect.interruptible(Effect.raceFirst(checked, Deferred.await(entry.stop))).pipe(
              Effect.ensuring(release),
            ),
            runScope,
          );
          return { type: "started", started: { fiber, entry } } as const;
        }
        // In the server or a file check process: stopped, it answers at once, but the slot is
        // freed only when the check itself returns (or the service stops).
        const own = yield* Effect.forkIn(
          Effect.interruptible(checked).pipe(Effect.ensuring(release)),
          runScope,
        );
        const fiber = yield* Effect.forkIn(
          Effect.interruptible(Effect.raceFirst(Fiber.join(own), Deferred.await(entry.stop))),
          runScope,
        );
        return { type: "started", started: { fiber, entry } } as const;
      }),
    );

  /** The check's answer by `until` (monotonic), or none; the check itself runs on either way. */
  const awaitJudge = (started: StartedJudge, until: number) =>
    Effect.flatMap(monotonicMillis, (now) =>
      Fiber.join(started.fiber).pipe(
        Effect.timeoutOption(Duration.millis(Math.max(0, until - now))),
      ),
    );

  /**
   * Keeps a check that runs on as its wait's one running check: the next pass takes its answer and
   * starts no other run of it.
   */
  const carry = Effect.fnUntraced(function* (waitId: string, started: StartedJudge) {
    const kept: CarriedJudge = { entry: started.entry, answer: null };
    carried.set(waitId, kept);
    // When it turns late, on the wall clock that times the waits.
    const left = Math.max(0, started.entry.lateAt - (yield* monotonicMillis));
    nextCheckAt.set(waitId, (yield* Clock.currentTimeMillis) + left);
    yield* Effect.forkIn(
      Fiber.join(started.fiber).pipe(
        Effect.flatMap((judgement) =>
          Effect.gen(function* () {
            if (carried.get(waitId) !== kept || kept.answer !== null) return;
            kept.answer = judgement;
            nextCheckAt.set(waitId, yield* Clock.currentTimeMillis);
            yield* onChange;
          }),
        ),
        Effect.ignoreCause,
      ),
      runScope,
    );
  });

  /** One error log per stuck spell of a place; the next one after a check of it returns. */
  const logStuck = (pool: JudgePool, next: string) =>
    Effect.suspend(() => {
      if (blockedLogged.has(pool)) return Effect.void;
      blockedLogged.add(pool);
      const set = runningOf(pool);
      const stuck = [...set].map((entry) => `${entry.waitId}: ${entry.target.slice(0, 200)}`);
      const what = pool === "fileChild" ? "file check processes" : `${pool} checks`;
      return Effect.logError(
        `waits: ${set.size} ${what} have not answered (${stuck.join("; ")}). ${next} A dead network mount is the usual cause, and a restart clears it.`,
      );
    });

  /**
   * A check of this kind in its place, or (file checks only) in a short-lived process when every
   * check of the kind is stuck. Never waits: `busy` when every slot is held by a check that may
   * still answer in time, `stuck` when the checks are paused, `threadBusy` when the thread runs a
   * check already.
   */
  const tryStartJudge = Effect.fnUntraced(function* (
    kind: WaitKind,
    run: JudgeRun,
    judge: () => Effect.Effect<Judgement>,
    childJudge: (() => Effect.Effect<Judgement>) | null,
    limitMs: number,
  ) {
    const first = yield* startJudge(kind, run, judge, limitMs);
    if (first.type !== "stuck") return first;
    if (childJudge === null) {
      yield* logStuck(kind, `No new ${kind} check starts until one returns.`);
      return first;
    }
    yield* logStuck(
      kind,
      `Until one returns, each file check runs in its own process, which is killed after ${Duration.format(Duration.millis(FILE_CHILD_LIMIT_MS))}.`,
    );
    const second = yield* startJudge("fileChild", run, childJudge, FILE_CHILD_LATE_MS);
    if (second.type === "stuck") {
      yield* logStuck("fileChild", "No new file check starts until one of them ends.");
    }
    return second;
  });

  /**
   * At registration: a check started within `until` (monotonic), waiting for a free slot while one
   * may still free up in time; null when the checks are stuck, the thread runs a check already, or
   * the time is up.
   */
  const startJudgeWhenFree = Effect.fnUntraced(function* (
    kind: WaitKind,
    run: JudgeRun,
    until: number,
    judge: () => Effect.Effect<Judgement>,
    childJudge: (() => Effect.Effect<Judgement>) | null,
    limitMs: number,
  ) {
    for (;;) {
      const changed = judgesChanged;
      const result = yield* tryStartJudge(kind, run, judge, childJudge, limitMs);
      if (result.type === "started") return result.started;
      if (result.type === "stuck" || result.type === "threadBusy") return null;
      const now = yield* monotonicMillis;
      if (now >= until) return null;
      // Woken when a check returns, or when the first running one turns late.
      yield* Deferred.await(changed).pipe(
        Effect.timeoutOption(Duration.millis(Math.min(until, result.wakeAt) - now)),
      );
    }
  });

  /** The file check in its own process, for a file target; null for other kinds. */
  const childJudgeOf = (target: WaitTarget, context: WaitJudgeContext) =>
    target.kind === "file" && fileChildJudge !== null
      ? () => fileChildJudge(target, context)
      : null;

  /**
   * Where a check wait's check runs now, within `PLACE_LIMIT`; null without `checkPlaceOf` (the
   * judge works it out, in the stored project's terms).
   */
  const placeOf = (context: WaitJudgeContext): Effect.Effect<CheckPlace | null> => {
    const resolve = input.checkPlaceOf;
    if (resolve === undefined) return Effect.succeed(null);
    return Effect.suspend(() => resolve(context)).pipe(
      Effect.timeoutOption(PLACE_LIMIT),
      Effect.map(
        Option.getOrElse((): CheckPlace => ({
          type: "unreadable",
          message: `Where the check runs could not be read in ${Duration.format(PLACE_LIMIT)}.`,
        })),
      ),
      Effect.catchCauseIf(
        (cause) => !Cause.hasInterruptsOnly(cause),
        (cause) =>
          Effect.succeed<CheckPlace>({
            type: "unreadable",
            message: `Where the check runs could not be read: ${causeText(cause)}`,
          }),
      ),
    );
  };

  /** The project that times a wait: where its check last ran, else where it was registered. */
  const timingProject = (wait: WaitRecord) => runProject.get(wait.id) ?? wait.projectId;

  /**
   * A `met` answer of a check wait counts only while its check, in the project it ran in, still
   * allows the wait: a check removed or narrowed meanwhile ends the wait with that error instead.
   */
  const confirmMet = Effect.fn("WaitService.confirmMet")(function* (
    wait: WaitRecord,
    judgement: Judgement,
    settings: ServerSettings,
  ) {
    if (wait.kind !== "check" || judgement.type !== "met") return judgement;
    // Read again: the check may have been removed while it ran.
    const found: CheckLookup = yield* lookUpCheck(
      wait.spec,
      timingProject(wait),
      (yield* readSettingsOrNull) ?? settings,
      platform,
    );
    return found.ok ? judgement : found.judgement;
  });

  /**
   * Starts the judge of a stored wait: its answer (within its time limit), `busy` when every slot
   * of its kind is held by a check that may still answer in time, or `skip`. The judge runs in its
   * own fiber, so one that never answers is left running (not started again) instead of holding up
   * the sweep.
   */
  const startStored: (
    wait: WaitRecord,
    now: number,
    settings: ServerSettings,
    answerUntil: number,
  ) => Effect.Effect<StoredStart> = Effect.fn("WaitService.startStored")(function* (
    wait: WaitRecord,
    now: number,
    settings: ServerSettings,
    answerUntil: number,
  ) {
    const judge = judgeOf(wait.kind);
    // Its deadline still ends it, so a wait this build cannot judge is never stuck.
    if (judge === undefined) {
      return answered({ type: "notYet", detail: `This server cannot judge ${wait.kind} waits.` });
    }
    const waiting = carried.get(wait.id);
    if (waiting !== undefined) {
      if (waiting.answer !== null) {
        carried.delete(wait.id);
        return answered(yield* confirmMet(wait, waiting.answer, settings));
      }
      if ((yield* monotonicMillis) < waiting.entry.lateAt) return { type: "skip" } as const;
      carried.delete(wait.id);
      return answered(
        lateJudgement(
          `The check gave no answer in ${Duration.format(Duration.millis(waiting.entry.limitMs))}.`,
        ),
      );
    }
    const own = (pool: JudgePool) => [...runningOf(pool)].some((entry) => entry.waitId === wait.id);
    if (own(wait.kind) || own("fileChild")) {
      return answered(lateJudgement("Its last check has not answered yet."));
    }
    const target = targetOf(wait);
    const base: WaitJudgeContext = {
      waitId: wait.id,
      now,
      threadId: wait.threadId,
      projectId: wait.projectId,
      pin: (next) => pinStored(wait.id, target, next),
    };
    let context = base;
    let checkRun: JudgeRun["check"];
    if (wait.kind === "check") {
      const place = yield* placeOf(base);
      if (place !== null) {
        if (place.type === "gone") {
          return answered({ type: "failed", message: place.message, permanent: true });
        }
        if (place.type === "unreadable") return answered(lateJudgement(place.message));
        runProject.set(wait.id, place.projectId);
        context = { ...base, place };
      }
      checkRun = { spec: wait.spec, projectId: timingProject(wait) };
    }
    const result = yield* tryStartJudge(
      wait.kind,
      {
        waitId: wait.id,
        threadId: wait.threadId,
        target: waitTargetText(target),
        ...(checkRun === undefined ? {} : { check: checkRun }),
      },
      () => judge(target, context),
      childJudgeOf(target, context),
      judgeLimitMs(target, timingProject(wait), settings),
    );
    if (result.type === "busy") return result;
    if (result.type === "threadBusy") {
      // Its thread runs another check; it is judged at its next interval.
      nextCheckAt.set(wait.id, now + intervalMs(target, timingProject(wait), settings));
      return { type: "skip" } as const;
    }
    if (result.type === "stuck") {
      return answered({
        type: "notYet",
        detail: `Checks are paused: ${result.count} earlier ${wait.kind} checks have not answered.`,
      });
    }
    const { entry } = result.started;
    const late = lateJudgement(
      `The check gave no answer in ${Duration.format(Duration.millis(entry.limitMs))}.`,
    );
    return {
      type: "answer",
      answer: awaitJudge(result.started, Math.min(entry.lateAt, answerUntil)).pipe(
        Effect.flatMap((answer) => {
          if (Option.isSome(answer)) return confirmMet(wait, answer.value, settings);
          if (entry.lateAt <= answerUntil) return Effect.succeed(late);
          // Still in time: it goes on, and a later pass takes its answer.
          return carry(wait.id, result.started).pipe(Effect.as(null));
        }),
      ),
    } as const;
  });

  /**
   * Applies the answers of one pass and saves them in one write. Returns how many waits ended;
   * none when the write failed (they are judged again next time).
   */
  const applyJudgements = (
    answers: ReadonlyArray<readonly [string, Judgement]>,
    settings: ServerSettings,
  ) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return 0;
        // The deadline is tested after the judge (R4), at the time of the answer.
        const now = yield* Clock.currentTimeMillis;
        const judged = new Map<string, WaitTransition>();
        const judgements = new Map<string, Judgement>();
        for (const [id, judgement] of answers) {
          const wait = loaded.contents.waits.find((candidate) => candidate.id === id);
          // Pruned while it was judged.
          if (wait === undefined) continue;
          nextCheckAt.set(id, now + intervalMs(targetOf(wait), timingProject(wait), settings));
          judgements.set(id, judgement);
          // Null when it is no longer `waiting` (a cancel came in between) or nothing changed.
          const transition = nextWaitState(
            wait,
            { type: "judged", judgement },
            now,
            stateSettings(settings),
          );
          if (transition !== null) judged.set(id, transition);
        }
        // One message per thread over its failing waits, and only so many threads per pass.
        const { changes, heldThreads } = limitErrorWakes({
          waits: loaded.contents.waits,
          changes: judged,
          judgements,
          now,
          settings: stateSettings(settings),
          maxThreads: settings.waitFor.maxErrorWakesPerSweep,
        });
        if (heldThreads > 0) {
          yield* Effect.logWarning(
            `waits: ${heldThreads} more threads have waits that ended in check_error; they are woken in later sweeps (at most ${settings.waitFor.maxErrorWakesPerSweep} per sweep).`,
          );
        }
        if (changes.size === 0) return 0;
        const waits = loaded.contents.waits.map(
          (candidate) => changes.get(candidate.id)?.record ?? candidate,
        );
        const written = yield* commit({ ...loaded.contents, waits }, settings.waitFor).pipe(
          Effect.as(true),
          Effect.catch((error) =>
            Effect.logWarning(`waits: ${error.message}; judged again next time.`).pipe(
              Effect.as(false),
            ),
          ),
        );
        if (!written) return 0;
        let ended = 0;
        for (const transition of changes.values()) {
          yield* logTransition(transition);
          if (transition.record.state !== "waiting") ended += 1;
        }
        return ended;
      }),
    );

  /**
   * Saves one answer at once when it ends its wait (met, failed for good, or past its deadline):
   * "ended" when saved, "later" when it does not end the wait (or the write failed; it goes into
   * the pass-end write), "skip" when waits were switched off or the wait is gone.
   */
  const saveIfEnds = (id: string, judgement: Judgement, settings: ServerSettings) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        if (!(yield* stillOn)) return "skip" as const;
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return "later" as const;
        const wait = loaded.contents.waits.find((candidate) => candidate.id === id);
        // Pruned while it was judged.
        if (wait === undefined) return "skip" as const;
        // The deadline is tested after the judge (R4), at the time of the answer.
        const now = yield* Clock.currentTimeMillis;
        const transition = nextWaitState(
          wait,
          { type: "judged", judgement },
          now,
          stateSettings(settings),
        );
        // A `check_error` waits for the pass end, where one thread's failing waits are joined and
        // the threads woken per pass are capped.
        if (
          transition === null ||
          transition.record.state === "waiting" ||
          transition.record.result?.outcome === "check_error"
        ) {
          return "later" as const;
        }
        const waits = loaded.contents.waits.map((candidate) =>
          candidate.id === id ? transition.record : candidate,
        );
        const written = yield* commit({ ...loaded.contents, waits }, settings.waitFor).pipe(
          Effect.as(true),
          Effect.catch((error) =>
            Effect.logWarning(`waits: ${error.message}; saved again at the end of the pass.`).pipe(
              Effect.as(false),
            ),
          ),
        );
        if (!written) return "later" as const;
        nextCheckAt.delete(id);
        yield* logTransition(transition);
        return "ended" as const;
      }),
    );

  /**
   * Due when its next check time has come, or lies more than one interval ahead: the wall clock
   * was set back (a time sync or a restored snapshot), which must not pause judging.
   */
  const isDue = (wait: WaitRecord, now: number, monoNow: number, settings: ServerSettings) => {
    const kept = carried.get(wait.id);
    if (kept !== undefined) return kept.answer !== null || monoNow >= kept.entry.lateAt;
    const next = nextCheckAt.get(wait.id);
    return (
      next === undefined ||
      next <= now ||
      next - now > intervalMs(targetOf(wait), timingProject(wait), settings)
    );
  };

  /**
   * Stops the running checks of waits that are no longer `waiting` (ended, cancelled, pruned, or
   * gone from a store read again), except ids a registration in progress holds.
   */
  const stopEndedRuns = Effect.suspend(() => {
    const waiting = new Set(
      (current?.waits ?? []).filter((wait) => wait.state === "waiting").map((wait) => wait.id),
    );
    for (const id of carried.keys()) if (!waiting.has(id)) carried.delete(id);
    for (const id of runProject.keys()) if (!waiting.has(id)) runProject.delete(id);
    const ended = new Set(
      allRunning()
        .map((entry) => entry.waitId)
        .filter((id) => !waiting.has(id) && !reserved.has(id)),
    );
    return ended.size === 0 ? Effect.void : stopRuns(ended, lateJudgement("Its wait has ended."));
  });

  /** Waits were switched off: every running check is stopped, and no answer is kept. */
  const stopAllRuns = Effect.suspend(() => {
    carried.clear();
    return stopRuns(null, lateJudgement("Waits are off."));
  });

  /**
   * Stops every running check whose check was removed from settings, or changed so it no longer
   * allows its wait: the wait's answer is that error, never a `met` from the run.
   */
  const stopRemovedChecks = (settings: ServerSettings) =>
    Effect.forEach(
      allRunning(),
      (entry) =>
        Effect.gen(function* () {
          if (entry.check === undefined) return;
          const found = yield* lookUpCheck(
            entry.check.spec,
            entry.check.projectId,
            settings,
            platform,
          );
          if (found.ok || found.judgement.type !== "failed" || !found.judgement.permanent) return;
          const kept = carried.get(entry.waitId);
          if (kept !== undefined && kept.entry === entry && kept.answer === null) {
            kept.answer = found.judgement;
            nextCheckAt.set(entry.waitId, yield* Clock.currentTimeMillis);
          }
          yield* Deferred.succeed(entry.stop, found.judgement);
        }),
      { discard: true },
    );

  /** `onEnded` runs each time an answer that ends its wait was saved, while the pass goes on. */
  const judgeSweep = Effect.fnUntraced(function* (
    settings: ServerSettings,
    onEnded: Effect.Effect<void> = Effect.void,
  ) {
    yield* stopRemovedChecks(settings);
    const startedAt = yield* Clock.currentTimeMillis;
    const monoStartedAt = yield* monotonicMillis;
    const due = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return [];
        const waiting = new Set<string>();
        const due: WaitRecord[] = [];
        for (const wait of loaded.contents.waits) {
          if (wait.state !== "waiting") continue;
          waiting.add(wait.id);
          // Slot waits are granted by their own step, oldest first.
          if (wait.kind !== "slot" && isDue(wait, startedAt, monoStartedAt, settings))
            due.push(wait);
        }
        for (const id of nextCheckAt.keys()) if (!waiting.has(id)) nextCheckAt.delete(id);
        yield* stopEndedRuns;
        return due;
      }),
    );
    if (due.length === 0) return { judged: 0, ended: 0 };
    // Waits passed over last time (their kind was busy) go first, so none waits for ever.
    const order = [...due].sort(
      (a, b) => (nextCheckAt.get(a.id) ?? 0) - (nextCheckAt.get(b.id) ?? 0),
    );
    // Each worker takes the first wait it can start now; it waits only when every remaining kind
    // is full, and starts nothing after `SWEEP_START_WINDOW_MS` (those waits go first next pass).
    // An answer that ends its wait is saved at once; the others in one write at the end. A late
    // answer is ignored and its wait judged again later.
    const startUntil = monoStartedAt + SWEEP_START_WINDOW_MS;
    const answerUntil = monoStartedAt + SWEEP_ANSWER_WINDOW_MS;
    const taken = new Set<string>();
    const later: Array<readonly [string, Judgement]> = [];
    let judged = 0;
    let ended = 0;
    const crashed = (cause: Cause.Cause<never>): Judgement =>
      lateJudgement(`The check crashed: ${causeText(cause)}`);
    const takeNext = Effect.gen(function* () {
      for (;;) {
        // Past the window nothing new starts, not even in a slot that has just been freed.
        if ((yield* monotonicMillis) >= startUntil) return null;
        const changed = judgesChanged;
        let wakeAt = Number.POSITIVE_INFINITY;
        let left = false;
        const full = new Set<WaitKind>();
        for (const wait of order) {
          if (taken.has(wait.id)) continue;
          if (full.has(wait.kind)) {
            left = true;
            continue;
          }
          // Taken before its start, so no other worker starts it too.
          taken.add(wait.id);
          const start = yield* startStored(wait, startedAt, settings, answerUntil).pipe(
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.succeed(answered(crashed(cause))),
            ),
          );
          if (start.type === "answer") return [wait, start.answer] as const;
          if (start.type === "skip") continue;
          taken.delete(wait.id);
          full.add(wait.kind);
          left = true;
          wakeAt = Math.min(wakeAt, start.wakeAt);
        }
        if (!left) return null;
        const now = yield* monotonicMillis;
        // Woken when a check returns, or when the first running one turns late.
        yield* Deferred.await(changed).pipe(
          Effect.timeoutOption(Duration.millis(Math.min(startUntil, wakeAt) - now)),
        );
      }
    });
    const worker = Effect.gen(function* () {
      for (;;) {
        const next = yield* takeNext;
        if (next === null) return;
        const [wait, answer] = next;
        const judgement = yield* answer.pipe(
          Effect.catchCauseIf(
            (cause) => !Cause.hasInterruptsOnly(cause),
            (cause) => Effect.succeed(crashed(cause)),
          ),
        );
        // Its check goes on past this pass; a later pass takes its answer.
        if (judgement === null) continue;
        judged += 1;
        const saved = yield* saveIfEnds(wait.id, judgement, settings);
        if (saved === "ended") {
          ended += 1;
          yield* onEnded;
        } else if (saved === "later") later.push([wait.id, judgement]);
      }
    });
    yield* Effect.all(
      Array.from({ length: Math.min(JUDGE_CONCURRENCY, order.length) }, () => worker),
      { concurrency: "unbounded", discard: true },
    );
    // Switched off while the checks ran: nothing more is written or sent.
    if (!(yield* stillOn)) return { judged, ended };
    ended += yield* applyJudgements(later, settings);
    return { judged, ended };
  });

  /**
   * Sweep step 3 (4.6): every `waiting` slot wait asks the gate for a slot, oldest first, so a
   * sweep wakes as many as there are free slots, each slot held for its thread. A full machine is
   * "not yet" (the deadline still ends it); counts that cannot be read are an error, never "free".
   * Reads nothing of the projection when no slot wait is waiting. Returns the waits it ended.
   */
  const grantSweep = Effect.fnUntraced(function* (settings: ServerSettings) {
    const gate = input.slots;
    if (gate === undefined) return 0;
    const waiting = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return [];
        return loaded.contents.waits
          .flatMap((wait) => (wait.kind === "slot" && wait.state === "waiting" ? [wait] : []))
          .sort((a, b) =>
            a.createdAt !== b.createdAt
              ? a.createdAt < b.createdAt
                ? -1
                : 1
              : a.id < b.id
                ? -1
                : 1,
          );
      }),
    );
    if (waiting.length === 0) return 0;
    // Outside the lock: the gate reads the store's held slots under it.
    const grants = yield* gate.grant(
      waiting.map((wait) => ({
        key: wait.id,
        threadId: wait.threadId,
        account: wait.spec.account,
      })),
    );
    const answers = waiting.map((wait, index) => {
      const grant = grants[index];
      return [
        wait.id,
        grant === undefined
          ? ({ type: "notYet" } satisfies Judgement)
          : slotJudgement(grant, settings, false),
      ] as const;
    });
    const freed = answers.flatMap(([id, judgement]) =>
      judgement.type === "met" ? [[id, judgement.detail] as const] : [],
    );
    // Switched off meanwhile: nothing is written, and nothing stays held.
    if (!(yield* stillOn)) {
      yield* gate.drop(freed.map(([id]) => id));
      return 0;
    }
    const kept = yield* applyGrants(new Map(freed), settings);
    // Written: the slots their threads held before are used up now, outside the lock (the gate
    // clears them in the store through it).
    yield* gate.keep(kept);
    const granted = kept.length;
    return (
      granted +
      (yield* applyJudgements(
        answers.filter(([, judgement]) => judgement.type !== "met"),
        settings,
      ))
    );
  });

  /**
   * Writes the slots granted and returns the ids written; a grant not kept (cancelled meanwhile,
   * or not written) holds nothing, and a slot its thread held before counts again.
   */
  const applyGrants = (granted: ReadonlyMap<string, string>, settings: ServerSettings) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        if (granted.size === 0) return [];
        const loaded = yield* ensureLoaded(true);
        const now = yield* Clock.currentTimeMillis;
        const changes = new Map<string, WaitTransition>();
        for (const wait of loaded.ok ? loaded.contents.waits : []) {
          const detail = granted.get(wait.id);
          if (detail === undefined) continue;
          const transition = nextWaitState(
            wait,
            { type: "slotGranted", detail },
            now,
            stateSettings(settings),
          );
          if (transition !== null) changes.set(wait.id, transition);
        }
        const written =
          loaded.ok &&
          changes.size > 0 &&
          (yield* commitWaits(
            loaded.contents,
            loaded.contents.waits.map((wait) => changes.get(wait.id)?.record ?? wait),
            "the slots are granted again next time.",
            settings,
          ));
        const kept = written ? changes : new Map<string, WaitTransition>();
        const notKept = [...granted.keys()].filter((id) => !kept.has(id));
        yield* input.slots!.drop(notKept);
        // A cancelled wait is never delivered; one not written is granted again from the start.
        for (const id of notKept) usedBeforeDelivery.delete(id);
        for (const [id, transition] of kept) {
          nextCheckAt.delete(id);
          yield* logTransition(transition);
        }
        return [...kept.keys()];
      }),
    );

  /**
   * Judges every `waiting` wait that is due, at most `JUDGE_CONCURRENCY` at a time (and
   * `MAX_RUNNING_JUDGES_PER_KIND` per kind), each within its kind's `judgeLimit`. Answers that end
   * a wait are saved at once, the others in one write at the end. Does nothing while waits are off, and never overlaps itself: a call
   * during a sweep returns at once.
   */
  const judgeDue = Effect.fn("WaitService.judgeDue")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings !== null && !settings.waitFor.enabled) yield* stopAllRuns;
    if (settings === null || !settings.waitFor.enabled) return { judged: 0, ended: 0 };
    const result = yield* Semaphore.withPermitsIfAvailable(sweepLock, 1)(judgeSweep(settings));
    return Option.getOrElse(result, () => ({ judged: 0, ended: 0 }));
  });

  /** Runs `events` in order on the stored wait; null when none of them changes it. */
  const applyEvents = (
    wait: WaitRecord,
    events: ReadonlyArray<WaitEvent>,
    now: number,
    settings: ServerSettings,
    transitions: WaitTransition[],
  ): WaitRecord => {
    let record = wait;
    for (const event of events) {
      const transition = nextWaitState(record, event, now, stateSettings(settings));
      if (transition === null) continue;
      transitions.push(transition);
      record = transition.record;
    }
    return record;
  };

  /** Writes the changed waits; false (with a warning) when the write failed. */
  const commitWaits = (
    contents: WaitStore.WaitStoreContents,
    waits: ReadonlyArray<WaitRecord>,
    what: string,
    settings: ServerSettings,
  ) =>
    commit({ ...contents, waits }, settings.waitFor).pipe(
      Effect.as(true),
      Effect.catch((error) =>
        Effect.logWarning(`waits: ${error.message}; ${what}`).pipe(Effect.as(false)),
      ),
    );

  const forgetMessage = (messageId: string) => {
    retryAt.delete(messageId);
    failures.delete(messageId);
    sendTroubleLogged.delete(messageId);
  };

  /** The events one send's answer makes for each wait of its message. */
  const eventsFor = (
    result: WaitDeliveryResult,
    wait: WaitRecord,
    now: number,
    settings: ServerSettings,
  ): ReadonlyArray<WaitEvent> => {
    switch (result.type) {
      case "delivered":
        return [{ type: "threadRunning" }, { type: "delivered" }];
      case "rejected":
        return [{ type: "threadRunning" }, { type: "rejected" }];
      case "retry": {
        // Nothing was recorded, so the same command id is tried again; long after the deadline
        // it counts as refused, which drops the wait with an error instead of trying forever.
        const late =
          now >= Date.parse(wait.deadlineAt) + settings.waitFor.keepEndedHours * 3_600_000;
        return late ? [{ type: "rejected" }] : [];
      }
      case "gone":
        return [{ type: "threadGone", parentThreadId: result.parentThreadId }];
      case "stopped":
        return [
          {
            type: "threadStopped",
            parentThreadId: result.parentThreadId,
            ...(result.stoppedAt === undefined ? {} : { stoppedAt: result.stoppedAt }),
          },
        ];
    }
  };

  /** When the message of `group` is tried next after `result`, or null when it is done. */
  const nextTry = (
    group: DeliveryGroup,
    result: WaitDeliveryResult,
    after: ReadonlyArray<WaitRecord>,
    now: number,
    settings: ServerSettings,
  ): number | null => {
    const still = after.filter(
      (wait) => wait.state === "delivering" && wait.messageId === group.messageId,
    );
    if (still.length === 0) return null;
    const attempt = Math.max(...still.map((wait) => wait.attempt));
    switch (result.type) {
      case "rejected":
        return now + deliveryRetryDelayMs(attempt);
      case "retry": {
        const tries = (failures.get(group.messageId) ?? 0) + 1;
        failures.set(group.messageId, tries);
        return now + deliveryRetryDelayMs(attempt + tries);
      }
      case "stopped": {
        const since = Math.min(
          ...still.map((wait) =>
            wait.stoppedSince === undefined ? now : Date.parse(wait.stoppedSince),
          ),
        );
        const handOverAt = since + settings.waitFor.stoppedHandOverHours * 3_600_000;
        return Math.min(now + STOPPED_RECHECK_MS, Math.max(handOverAt, now));
      }
      case "delivered":
      case "gone":
        return null;
    }
  };

  /**
   * Sends the message of every `delivering` wait that is due: one message per receiver, its id
   * written before the first send. Waits of a receiver that is gone go to its parent first.
   */
  const deliverSweep = Effect.fnUntraced(function* (settings: ServerSettings) {
    // 1. Receivers of `waiting` waits that may be gone (archived or deleted since).
    const toCheck = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return [];
        const threads = new Set<ThreadId>();
        for (const wait of loaded.contents.waits) {
          if (wait.state !== "waiting") continue;
          if (checkAllReceivers || receiversToCheck.has(wait.threadId)) threads.add(wait.threadId);
        }
        checkAllReceivers = false;
        receiversToCheck.clear();
        return [...threads];
      }),
    );
    const gone = new Map<ThreadId, ThreadId | null>();
    for (const threadId of toCheck) {
      const receiver = yield* receiverOf(threadId);
      if (receiver === null) receiversToCheck.add(threadId);
      else if (receiver.type === "gone") gone.set(threadId, receiver.parentThreadId);
    }

    // 2. Hand over the waits of gone receivers, pick the due messages, write their ids.
    const startedAt = yield* Clock.currentTimeMillis;
    const planned = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return [];
        const transitions: WaitTransition[] = [];
        let waits = loaded.contents.waits.map((wait) => {
          const parent = gone.get(wait.threadId);
          return parent === undefined || wait.state !== "waiting"
            ? wait
            : applyEvents(
                wait,
                [{ type: "threadGone", parentThreadId: parent }],
                startedAt,
                settings,
                transitions,
              );
        });
        const due = deliveryGroups(waits).filter(
          (group) => (retryAt.get(group.messageId) ?? 0) <= startedAt,
        );
        const fresh = new Map<string, DeliveryGroup>();
        for (const group of due) {
          if (!group.fresh) continue;
          for (const wait of group.waits) fresh.set(wait.id, group);
        }
        if (fresh.size > 0) {
          waits = waits.map((wait) => {
            const group = fresh.get(wait.id);
            if (group === undefined) return wait;
            const { messageId, replaces } = group;
            return applyEvents(
              wait,
              [{ type: "sendStarted", messageId, ...(replaces === undefined ? {} : { replaces }) }],
              startedAt,
              settings,
              [],
            );
          });
        }
        if (transitions.length === 0 && fresh.size === 0) return due;
        const written = yield* commitWaits(loaded.contents, waits, "sent next time.", settings);
        if (!written) return [];
        yield* Effect.forEach(transitions, logTransition, { discard: true });
        // The groups as written, so the waits carry their message id.
        const byId = new Map(waits.map((wait) => [wait.id, wait]));
        return due.map((group) => ({
          ...group,
          fresh: false,
          waits: group.waits.map((wait) => byId.get(wait.id) ?? wait),
        }));
      }),
    );
    if (planned.length === 0) return { sent: 0, ended: 0 };

    // 3. Send, outside the lock: a slow thread never holds up register, list or cancel. Each
    // message on its own: a crash or a late answer is "try again later" for that message only.
    // Switched off meanwhile: the rest are not sent (and nothing is recorded for them).
    const tried = yield* Effect.forEach(
      planned,
      (group) =>
        Effect.gen(function* () {
          if (!(yield* stillOn)) return null;
          const result = yield* send({
            threadId: group.threadId,
            messageId: group.messageId,
            commandId: deliveryCommandId(group),
            text: joinedWakeMessage(group.waits),
          });
          return [group, result] as const;
        }).pipe(
          Effect.catchCauseIf(
            (cause) => !Cause.hasInterruptsOnly(cause),
            (cause) =>
              logSendTrouble(
                group.messageId,
                `message ${group.messageId} to thread ${group.threadId} cannot be built; it is tried again later: ${causeText(cause)}`,
              ).pipe(
                Effect.as([
                  group,
                  { type: "retry", detail: `It cannot be built: ${causeText(cause)}` },
                ] as const),
              ),
          ),
        ),
      { concurrency: SEND_CONCURRENCY },
    );
    const results = tried.filter((entry) => entry !== null);
    if (results.length === 0) return { sent: 0, ended: 0 };

    // 4. Write what each send's answer means.
    return yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return { sent: 0, ended: 0 };
        const now = yield* Clock.currentTimeMillis;
        const answers = new Map<string, WaitDeliveryResult>();
        for (const [group, result] of results) {
          for (const wait of group.waits) answers.set(wait.id, result);
          if (result.type === "retry") {
            yield* Effect.logWarning(
              `waits: message ${group.messageId} to thread ${group.threadId} not sent yet: ${result.detail}`,
            );
          } else if (result.type === "rejected") {
            yield* Effect.logWarning(
              `waits: thread ${group.threadId} refused message ${group.messageId}: ${result.detail}`,
            );
          }
        }
        const transitions: WaitTransition[] = [];
        const waits = loaded.contents.waits.map((wait) => {
          const result = answers.get(wait.id);
          // Only the message that was sent: the wait may have been handed over since.
          if (result === undefined || wait.state !== "delivering") return wait;
          const group = planned.find((candidate) => candidate.waits.some((w) => w.id === wait.id));
          if (group === undefined || wait.messageId !== group.messageId) return wait;
          const events = eventsFor(result, wait, now, settings);
          // Its slot was used before this delivery: the delivered wait holds none.
          return applyEvents(
            wait,
            usedBeforeDelivery.has(wait.id) ? [...events, { type: "slotUsed" }] : events,
            now,
            settings,
            transitions,
          );
        });
        if (transitions.length > 0) {
          const written = yield* commitWaits(
            loaded.contents,
            waits,
            "answered again next time.",
            settings,
          );
          // Not written: the next sweep finds the message in the thread, or replays its receipt.
          if (!written) return { sent: 0, ended: 0 };
          yield* Effect.forEach(transitions, logTransition, { discard: true });
          // A granted slot wait handed over to the parent, or ended without a receiver: the slot
          // held for its thread is freed at once (the parent cannot use it).
          const noReceiver = transitions.flatMap(({ record }) =>
            record.kind === "slot" &&
            (record.handedOverBecause !== undefined ||
              ["thread_gone", "thread_stopped", "dropped"].includes(record.state))
              ? [record.id]
              : [],
          );
          if (input.slots !== undefined) yield* input.slots.drop(noReceiver);
          for (const id of usedBeforeDelivery) {
            const wait = waits.find((candidate) => candidate.id === id);
            if (wait === undefined || (wait.state !== "waiting" && wait.state !== "delivering")) {
              usedBeforeDelivery.delete(id);
            }
          }
        }
        for (const [group, result] of results) {
          const at = nextTry(group, result, waits, now, settings);
          if (at === null) forgetMessage(group.messageId);
          else retryAt.set(group.messageId, at);
        }
        const sent = results.filter(([, result]) => result.type === "delivered").length;
        const ended = transitions.filter((transition) =>
          ["delivered", "dropped", "thread_gone", "thread_stopped"].includes(
            transition.record.state,
          ),
        ).length;
        return { sent, ended };
      }),
    );
  });

  /** Removes ended waits older than `keepEndedHours`. Under `sweepLock`. */
  const pruneSweep = (settings: ServerSettings) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return 0;
        const now = yield* Clock.currentTimeMillis;
        const waits = loaded.contents.waits.filter(
          (wait) => !shouldPruneWait(wait, now, settings.waitFor),
        );
        const removed = loaded.contents.waits.length - waits.length;
        if (removed === 0) return 0;
        const written = yield* commitWaits(loaded.contents, waits, "pruned next time.", settings);
        return written ? removed : 0;
      }),
    );

  /** Removes ended waits older than `keepEndedHours`. Does nothing while waits are off. */
  const prune = Effect.fn("WaitService.prune")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return 0;
    return yield* pruneSweep(settings);
  });

  /**
   * The notice for the receiver's parent, or null when no parent can take it (none, a loop, or
   * `MAX_HAND_OVERS` reached), the same rule as a wait's hand-over.
   */
  const handOverNotice = (
    notice: WaitStoreNotice,
    because: "gone" | "stopped",
    parent: ThreadId | null,
  ): WaitStoreNotice | null => {
    const via = notice.via ?? [];
    if (
      parent === null ||
      parent === notice.threadId ||
      via.includes(parent) ||
      via.length >= MAX_HAND_OVERS
    ) {
      return null;
    }
    return {
      threadId: parent,
      messageId: `${notice.messageId}:via:${via.length + 1}`,
      text: notice.text,
      via: [...via, notice.threadId],
      handedOverBecause: because,
    };
  };

  /**
   * Sends the lost-wait notices; one leaves the file once sent or never sendable (same command id
   * on a resend). Gone receiver: to its parent; stopped: held `stoppedHandOverHours` from its latest
   * stop, then to its parent, like a wait. Only under `sweepLock`, so two never overlap.
   */
  const noticesSweep = Effect.fnUntraced(function* (settings: ServerSettings) {
    const pending = yield* lock.withPermits(1)(
      ensureLoaded(true).pipe(Effect.map((loaded) => (loaded.ok ? loaded.contents.notices : []))),
    );
    if (pending.length === 0) return 0;
    const done = new Set<string>();
    /** The notice that takes the place of one, by its message id (a hand-over, or a hold). */
    const replaced = new Map<string, WaitStoreNotice>();
    let held = false;
    let sent = 0;
    for (const notice of pending) {
      if (!(yield* stillOn)) {
        held = true;
        break;
      }
      const result = yield* send({
        threadId: notice.threadId,
        messageId: notice.messageId,
        commandId: `${notice.messageId}:0`,
        text: noticeMessage(notice),
      });
      const now = yield* Clock.currentTimeMillis;
      const handOver = Effect.fnUntraced(function* (
        because: "gone" | "stopped",
        parent: ThreadId | null,
        why: string,
      ) {
        const next = handOverNotice(notice, because, parent);
        if (next === null) {
          done.add(notice.messageId);
          yield* Effect.logError(
            `waits: thread ${notice.threadId} could not be told its waits were lost: ${why}, and no parent can take it.`,
          );
          return;
        }
        replaced.set(notice.messageId, next);
        yield* Effect.logWarning(
          `waits: thread ${notice.threadId} ${why}; its lost-wait notice goes to parent ${next.threadId}.`,
        );
      });
      switch (result.type) {
        case "delivered":
          sent += 1;
          done.add(notice.messageId);
          break;
        case "rejected":
          done.add(notice.messageId);
          yield* Effect.logError(
            `waits: thread ${notice.threadId} could not be told its waits were lost: ${result.detail}`,
          );
          break;
        case "gone":
          yield* handOver("gone", result.parentThreadId, "it is archived or deleted");
          break;
        case "stopped": {
          const since = notice.stoppedSince === undefined ? null : Date.parse(notice.stoppedSince);
          const latest = result.stoppedAt === undefined ? null : Math.min(result.stoppedAt, now);
          // Held from its latest stop: seen stopped for the first time, or stopped again since.
          if (since === null || (latest !== null && latest > since)) {
            const from = since === null ? now : latest!;
            replaced.set(notice.messageId, { ...notice, stoppedSince: iso(from) });
            held = true;
          } else if (now - since >= settings.waitFor.stoppedHandOverHours * 3_600_000) {
            yield* handOver("stopped", result.parentThreadId, "stayed stopped");
          } else {
            held = true;
          }
          break;
        }
        // Tried again later.
        case "retry":
          held = true;
          break;
      }
    }
    noticesRetryAt = held ? (yield* Clock.currentTimeMillis) + PROBLEM_RECHECK_MS : 0;
    if (done.size > 0 || replaced.size > 0) {
      yield* lock.withPermits(1)(
        Effect.gen(function* () {
          if (current === null) return;
          const contents = current;
          const notices = contents.notices
            .filter((notice) => !done.has(notice.messageId))
            .map((notice) => replaced.get(notice.messageId) ?? notice);
          yield* commit({ ...contents, notices }, settings.waitFor).pipe(
            Effect.catch((error) =>
              Effect.logWarning(`waits: ${error.message}; the notices are sent again next time.`),
            ),
          );
        }),
      );
    }
    for (const messageId of done) sendTroubleLogged.delete(messageId);
    return sent;
  });

  const sendNotices = Effect.fn("WaitService.sendNotices")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return 0;
    // Under the sweep lock, so it never runs beside the sweep's own notices.
    const result = yield* Semaphore.withPermitsIfAvailable(sweepLock, 1)(noticesSweep(settings));
    return Option.getOrElse(result, () => 0);
  });

  /** When the next sweep has work, from what is in memory; Infinity when nothing is open. */
  const nextDueAt = (settings: ServerSettings, now: number) => {
    if (current === null) return now + PROBLEM_RECHECK_MS;
    let next = Infinity;
    const at = (ms: number) => {
      next = Math.min(next, ms);
    };
    if (current.notices.length > 0) at(Math.max(noticesRetryAt, now));
    if (receiversToCheck.size > 0) at(now);
    // A held slot that ends may go to a slot wait still waiting.
    const slotsWaiting = current.waits.some(
      (wait) => wait.kind === "slot" && wait.state === "waiting",
    );
    for (const wait of current.waits) {
      if (slotsWaiting) {
        for (const held of storedReservation(wait, now, settings.agentSlots.reserveSeconds)) {
          at(held.untilMs);
        }
      }
      if (wait.state === "waiting") at(nextCheckAt.get(wait.id) ?? now);
      else if (wait.state !== "delivering" && wait.endedAt !== undefined) {
        at(
          Math.max(
            Date.parse(wait.endedAt) + settings.waitFor.keepEndedHours * 3_600_000,
            wait.reservedUntil === undefined ? 0 : Date.parse(wait.reservedUntil),
          ),
        );
      }
    }
    for (const group of deliveryGroups(current.waits)) at(retryAt.get(group.messageId) ?? now);
    return Math.max(next, now);
  };

  /**
   * One step of a sweep with the settings read again: null when waits are off or the settings
   * cannot be read (the sweep stops there). A step that fails or crashes is logged and gives
   * `fallback`, so one bad step never stops the sweep or the job.
   */
  const phase = Effect.fn("WaitService.phase")(function* <A, E>(
    name: string,
    run: (settings: ServerSettings) => Effect.Effect<A, E>,
    fallback: A,
  ) {
    const settings = yield* readSettingsOrNull;
    if (settings !== null && !settings.waitFor.enabled) yield* stopAllRuns;
    if (settings === null || !settings.waitFor.enabled) return null;
    return yield* Effect.suspend(() => run(settings)).pipe(
      Effect.catchCauseIf(
        (cause) => !Cause.hasInterruptsOnly(cause),
        (cause) =>
          Effect.logError(`waits: the ${name} step failed: ${causeText(cause)}`).pipe(
            Effect.as(fallback),
          ),
      ),
    );
  });

  /**
   * Runs `step` while sending the message of each wait it ends as soon as that is saved (a deliver
   * step after each, one at a time), so a met wait's message never waits for slow checks.
   * Returns once `step` and the last of those deliver steps are done.
   */
  const withEarlyDelivery = Effect.fnUntraced(function* <A>(
    step: (onEnded: Effect.Effect<void>) => Effect.Effect<A>,
  ) {
    let pending = false;
    let finished = false;
    const wake = yield* Latch.make(false);
    const none = { sent: 0, ended: 0 };
    const deliverer = yield* Effect.forkChild(
      Effect.gen(function* () {
        for (;;) {
          if (!pending) {
            if (finished) return;
            yield* wake.await;
            yield* wake.close;
            continue;
          }
          pending = false;
          // Waits were switched off: the step after this one stops the sweep.
          if ((yield* phase("deliver", deliverSweep, none)) === null) return;
        }
      }),
    );
    const result = yield* step(
      Effect.suspend(() => {
        pending = true;
        return Effect.asVoid(wake.open);
      }),
    );
    finished = true;
    yield* wake.open;
    yield* Fiber.join(deliverer);
    return result;
  });

  /**
   * One sweep: deliver, judge (sending each message it ends at once), deliver what that ended,
   * prune, notices; returns when the next one has work. Settings are read again before each step, so "off" stops it at the next step; a
   * failing step is logged and skipped. It never overlaps itself.
   */
  const runDue = Effect.fn("WaitService.runDue")(function* () {
    const settings = yield* readSettingsOrNull;
    // Settings that cannot be read are tried again on the scheduler's next tick.
    if (settings === null) return { nextDueAt: 0 };
    if (!settings.waitFor.enabled) {
      yield* stopAllRuns;
      return { nextDueAt: Infinity };
    }
    const stopped = { nextDueAt: Infinity };
    const result = yield* Semaphore.withPermitsIfAvailable(
      sweepLock,
      1,
    )(
      Effect.gen(function* () {
        const none = { sent: 0, ended: 0 };
        if ((yield* phase("deliver", deliverSweep, none)) === null) return stopped;
        const judged = yield* withEarlyDelivery((onEnded) =>
          phase("judge", (latest) => judgeSweep(latest, onEnded), { judged: 0, ended: 0 }),
        );
        if (judged === null) return stopped;
        const granted = yield* phase("grant", grantSweep, 0);
        if (granted === null) return stopped;
        if (judged.ended + granted > 0 && (yield* phase("deliver", deliverSweep, none)) === null) {
          return stopped;
        }
        if ((yield* phase("prune", pruneSweep, 0)) === null) return stopped;
        if ((yield* phase("notices", noticesSweep, 0)) === null) return stopped;
        const latest = (yield* readSettingsOrNull) ?? settings;
        const now = yield* Clock.currentTimeMillis;
        return yield* lock
          .withPermits(1)(Effect.sync(() => ({ nextDueAt: nextDueAt(latest, now) })))
          .pipe(
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) =>
                Effect.logError(
                  `waits: the next sweep time could not be worked out: ${causeText(cause)}`,
                ).pipe(Effect.as({ nextDueAt: now + PROBLEM_RECHECK_MS })),
            ),
          );
      }),
    );
    // Another sweep is running; it records its own answer.
    return Option.getOrElse(result, () => ({ nextDueAt: 0 }));
  });

  /** Sends the message of every due `delivering` wait. Does nothing while waits are off. */
  const deliverDue = Effect.fn("WaitService.deliverDue")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return { sent: 0, ended: 0 };
    const result = yield* Semaphore.withPermitsIfAvailable(sweepLock, 1)(deliverSweep(settings));
    return Option.getOrElse(result, () => ({ sent: 0, ended: 0 }));
  });

  /** A thread was archived or deleted: its `waiting` waits are looked at in the next sweep. */
  const receiverChanged = (threadId: ThreadId) =>
    Effect.sync(() => {
      // Before the store was read, the first sweep looks at every receiver anyway.
      if (current !== null) receiversToCheck.add(threadId);
    });

  // The gate counts the slots the store holds (only while waits are on) and clears a used one.
  if (input.slots !== undefined) {
    yield* input.slots.attachReservations({ read: heldInStore, used: useStoredSlot });
  }

  return {
    register,
    list,
    cancel,
    judgeDue,
    deliverDue,
    prune,
    sendNotices,
    runDue,
    receiverChanged,
  };
});

export type WaitService = Effect.Success<ReturnType<typeof make>>;
