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
  type WaitForSettings,
  type WaitKind,
  type WaitListResult,
  type WaitRecord,
  type WaitStoreNotice,
  type WaitTarget,
  WaitToolFailure,
  type WaitToolFailureCode,
} from "@t3tools/contracts/waitFor";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import {
  capEndedWaits,
  deliveryCommandId,
  type DeliveryGroup,
  deliveryGroups,
  deliveryRetryDelayMs,
  ENDED_PER_THREAD_FACTOR,
  joinedWakeMessage,
  type Judgement,
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
import * as Option from "effect/Option";
import * as Semaphore from "effect/Semaphore";

import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery, WaitDeliveryResult } from "./WaitDelivery.ts";
import {
  FILE_CHILD_LIMIT_MS,
  makeChildFileJudge,
  makeFileJudge,
  type WaitJudge,
  type WaitJudgeContext,
  type WaitJudges,
} from "./WaitJudges.ts";
import * as WaitStore from "./WaitStore.ts";

/**
 * At registration a judge that has not answered by then counts as "not yet" (R5). It is the whole
 * time register spends on its check: waiting for a free place and for the answer.
 */
const REGISTER_JUDGE_LIMIT_MS = 3_000;
/** At most this many judges, and this many sends, run at once. */
const JUDGE_CONCURRENCY = 4;
/**
 * A judge in a sweep that has not answered by then counts as a failed read (the error grace and
 * the deadline still end the wait), and the other waits go on. The check judge (PR 5) brings its
 * own limit from the check's `timeoutSeconds`.
 */
function judgeLimit(kind: WaitKind): Duration.Duration {
  return kind === "file" ? Duration.seconds(10) : Duration.seconds(60);
}
/**
 * Checks of one kind that have not returned (sweep, late and registration ones). A stuck `stat`
 * holds a thread of Node's I/O pool, so file checks use at most 2 of its 4 default threads. When
 * all are late, file checks run in a short-lived process (at most this many too), and waits of
 * other kinds answer "not yet", never "failed".
 */
const MAX_RUNNING_JUDGES_PER_KIND = 2;
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

/** How often a `waiting` wait of this kind is judged. */
function intervalMs(kind: WaitKind, settings: WaitForSettings): number {
  switch (kind) {
    case "file":
      return settings.fileSweepSeconds * 1000;
    case "pr":
      return settings.prSweepSeconds * 1000;
    // A check's own `intervalSeconds` comes with the check judge (PR 5).
    case "unit":
    case "check":
    case "slot":
      return settings.unitSweepSeconds * 1000;
  }
}

const stateSettings = (settings: ServerSettings): WaitStateSettings => ({
  errorGraceSeconds: settings.waitFor.errorGraceSeconds,
  keepEndedHours: settings.waitFor.keepEndedHours,
  stoppedHandOverHours: settings.waitFor.stoppedHandOverHours,
  reserveSeconds: settings.agentSlots.reserveSeconds,
});

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

interface RunningJudge {
  readonly pool: JudgePool;
  readonly waitId: string | null;
  readonly target: string;
  /**
   * When it counts as late (stuck while it still runs), fixed when it starts: whoever started it
   * may stop waiting for it at any time, even be cut off before it waits at all.
   */
  readonly lateAt: number;
}

interface StartedJudge {
  readonly fiber: Fiber.Fiber<Judgement>;
  readonly entry: RunningJudge;
}

/** A started check; or none, because every place is held by a check that may still answer in time
 * (`busy`, until `wakeAt` at the latest), or by late ones (`stuck`). */
type JudgeStart =
  | { readonly type: "started"; readonly started: StartedJudge }
  | { readonly type: "busy"; readonly wakeAt: number }
  | { readonly type: "stuck"; readonly count: number };

/** A stored wait's judge in a sweep: its answer to wait for, or `busy` (see `JudgeStart`). */
type StoredStart =
  | { readonly type: "answer"; readonly answer: Effect.Effect<Judgement> }
  | { readonly type: "busy"; readonly wakeAt: number };

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
}) {
  const onChange = input.onChange ?? Effect.void;
  const settingsService = yield* ServerSettingsService;
  const platform = yield* HostProcess.Platform;
  const store = yield* WaitStore.make({ filePath: input.filePath });
  const judges: WaitJudges = { file: yield* makeFileJudge(), ...input.judges };
  const fileChildJudge =
    input.fileChildJudge === undefined ? yield* makeChildFileJudge() : input.fileChildJudge;

  const lock = yield* Semaphore.make(1);
  const sweepLock = yield* Semaphore.make(1);
  /** Null until the file was read; only ever changed under `lock`. */
  let current: WaitStore.WaitStoreContents | null = null;
  let lastProblem: string | null = null;
  /** When each `waiting` wait is judged next; empty after a restart, so all are judged at once. */
  const nextCheckAt = new Map<string, number>();
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
    current = { waits: load.waits, notices: load.notices };
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
    const valid = validateWaitSpec({
      input: request.input,
      checks: resolveProjectSettings(settings, projectId).settings.waitChecks,
      platform,
      accounts: new Set(Object.keys(settings.providerInstances)),
    });
    if (!valid.ok) return yield* failure(valid.code, valid.message);
    const target = valid.target;
    const judge = judgeOf(target.kind);
    if (judge === undefined) {
      return yield* failure("not_supported", `This server cannot wait for "${target.kind}" yet.`);
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

    const existing = yield* lock.withPermits(1)(
      loadedOrFail(true).pipe(Effect.flatMap(findOrRefuse)),
    );
    if (Option.isSome(existing)) return waitingResult(existing.value, true);

    // Waits for a free place within its limit; with none, the next sweep judges it first.
    const until = now + REGISTER_JUDGE_LIMIT_MS;
    const context: WaitJudgeContext = { waitId: null, now };
    const started = yield* startJudgeWhenFree(
      target.kind,
      waitTargetText(target),
      until,
      () => judge(target, context),
      childJudgeOf(target, context),
    );
    const judgement: Judgement =
      started === null
        ? { type: "notYet" }
        : yield* awaitJudge(started, until).pipe(
            Effect.map(Option.getOrElse((): Judgement => ({ type: "notYet" }))),
          );
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
        const record = {
          id: newWaitId(new Set(contents.waits.map((wait) => wait.id))),
          threadId,
          projectId,
          key,
          ...(request.input.note === undefined ? {} : { note: request.input.note }),
          createdAt: iso(now),
          deadlineAt: iso(deadline.deadlineAt),
          state: "waiting",
          attempt: 0,
          ...(parentThreadId === undefined ? {} : { parentThreadId }),
          ...target,
        } as WaitRecord;
        yield* commitOrFail({ ...contents, waits: [...contents.waits, record] }, settings.waitFor);
        // Not checked here: the next sweep judges it, and before the waits it judged last time.
        if (started !== null) {
          nextCheckAt.set(record.id, now + intervalMs(record.kind, settings.waitFor));
        }
        yield* onChange;
        return waitingResult(record, false);
      }),
    );
  });

  /** `wait_list`: the caller's waits (or its project's), its project's checks, any store problem. */
  const list = Effect.fn("WaitService.list")(function* (request: {
    readonly threadId: ThreadId;
    readonly projectId: ProjectId;
    readonly scope?: "thread" | "project" | undefined;
    readonly includeEnded?: boolean | undefined;
  }) {
    const settings = yield* readSettings;
    const checks = Object.entries(
      resolveProjectSettings(settings, request.projectId).settings.waitChecks,
    )
      .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
      .map(([name, check]) => ({ name, description: check.description, maxArgs: check.maxArgs }));
    const loaded = yield* lock.withPermits(1)(ensureLoaded(settings.waitFor.enabled));
    if (!loaded.ok) {
      return {
        enabled: settings.waitFor.enabled,
        storeError: loaded.reason,
        waits: [],
        checks,
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
    return { enabled: settings.waitFor.enabled, waits, checks } satisfies WaitListResult;
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
          for (const id of cancelled) nextCheckAt.delete(id);
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

  /** How long a check of this place may run before it counts as late (stuck). */
  const poolLimitMs = (pool: JudgePool) =>
    pool === "fileChild" ? FILE_CHILD_LATE_MS : Duration.toMillis(judgeLimit(pool));

  /**
   * Takes a free slot and starts the check in its own fiber as one step that cannot be cut off, so
   * no slot is held by a check that never started. The slot is freed when the check returns. A
   * check is late by its own start time and limit, never by its waiter, so a cut-off caller cannot
   * leave a slot that looks busy forever.
   */
  const startJudge = (
    pool: JudgePool,
    waitId: string | null,
    target: string,
    judge: () => Effect.Effect<Judgement>,
  ): Effect.Effect<JudgeStart> =>
    Effect.uninterruptible(
      Effect.gen(function* () {
        const now = yield* Clock.currentTimeMillis;
        const set = runningOf(pool);
        if (set.size >= MAX_RUNNING_JUDGES_PER_KIND) {
          const onTime = [...set].filter((entry) => entry.lateAt > now);
          return onTime.length === 0
            ? ({ type: "stuck", count: set.size } as const)
            : ({ type: "busy", wakeAt: Math.min(...onTime.map((entry) => entry.lateAt)) } as const);
        }
        const entry: RunningJudge = { pool, waitId, target, lateAt: now + poolLimitMs(pool) };
        set.add(entry);
        // The check itself can still be stopped; only taking the slot and starting it is one step.
        const fiber = yield* Effect.forkDetach(
          Effect.suspend(judge).pipe(
            // A check that crashes is a failed read of its own wait, never of the sweep.
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.succeed(lateJudgement(`The check crashed: ${causeText(cause)}`)),
            ),
            Effect.ensuring(
              Effect.suspend(() => {
                set.delete(entry);
                blockedLogged.delete(pool);
                return signalJudgesChanged;
              }),
            ),
          ),
        );
        return { type: "started", started: { fiber, entry } } as const;
      }),
    );

  /** The check's answer by `until` (epoch ms), or none; the check itself runs on either way. */
  const awaitJudge = (started: StartedJudge, until: number) =>
    Effect.flatMap(Clock.currentTimeMillis, (now) =>
      Fiber.join(started.fiber).pipe(
        Effect.timeoutOption(Duration.millis(Math.max(0, until - now))),
      ),
    );

  /** One error log per stuck spell of a place; the next one after a check of it returns. */
  const logStuck = (pool: JudgePool, next: string) =>
    Effect.suspend(() => {
      if (blockedLogged.has(pool)) return Effect.void;
      blockedLogged.add(pool);
      const set = runningOf(pool);
      const stuck = [...set].map(
        (entry) => `${entry.waitId ?? "registration"}: ${entry.target.slice(0, 200)}`,
      );
      const what = pool === "fileChild" ? "file check processes" : `${pool} checks`;
      return Effect.logError(
        `waits: ${set.size} ${what} have not answered (${stuck.join("; ")}). ${next} A dead network mount is the usual cause, and a restart clears it.`,
      );
    });

  /**
   * A check of this kind in its place, or (file checks only) in a short-lived process when every
   * check of the kind is stuck. Never waits: `busy` when every slot is held by a check that may
   * still answer in time, `stuck` when the checks are paused.
   */
  const tryStartJudge = Effect.fnUntraced(function* (
    kind: WaitKind,
    waitId: string | null,
    target: string,
    judge: () => Effect.Effect<Judgement>,
    childJudge: (() => Effect.Effect<Judgement>) | null,
  ) {
    const first = yield* startJudge(kind, waitId, target, judge);
    if (first.type !== "stuck") return first;
    if (childJudge === null) {
      yield* logStuck(kind, `No new ${kind} check starts until one returns.`);
      return first;
    }
    yield* logStuck(
      kind,
      `Until one returns, each file check runs in its own process, which is killed after ${Duration.format(Duration.millis(FILE_CHILD_LIMIT_MS))}.`,
    );
    const second = yield* startJudge("fileChild", waitId, target, childJudge);
    if (second.type === "stuck") {
      yield* logStuck("fileChild", "No new file check starts until one of them ends.");
    }
    return second;
  });

  /**
   * At registration: a check started within `until` (epoch ms), waiting for a free slot while one
   * may still free up in time; null when the checks are stuck or the time is up.
   */
  const startJudgeWhenFree = Effect.fnUntraced(function* (
    kind: WaitKind,
    target: string,
    until: number,
    judge: () => Effect.Effect<Judgement>,
    childJudge: (() => Effect.Effect<Judgement>) | null,
  ) {
    for (;;) {
      const changed = judgesChanged;
      const result = yield* tryStartJudge(kind, null, target, judge, childJudge);
      if (result.type === "started") return result.started;
      if (result.type === "stuck") return null;
      const now = yield* Clock.currentTimeMillis;
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
   * Starts the judge of a stored wait: its answer (within its time limit), or `busy` when every
   * slot of its kind is held by a check that may still answer in time. The judge runs in its own
   * fiber, so one that never answers is left running (not started again) instead of holding up
   * the sweep.
   */
  const startStored = (wait: WaitRecord, now: number): Effect.Effect<StoredStart> => {
    const judge = judgeOf(wait.kind);
    // Its deadline still ends it, so a wait this build cannot judge is never stuck.
    if (judge === undefined) {
      return Effect.succeed(
        answered({ type: "notYet", detail: `This server cannot judge ${wait.kind} waits.` }),
      );
    }
    const own = (pool: JudgePool) => [...runningOf(pool)].some((entry) => entry.waitId === wait.id);
    if (own(wait.kind) || own("fileChild")) {
      return Effect.succeed(answered(lateJudgement("Its last check has not answered yet.")));
    }
    const target = targetOf(wait);
    const context: WaitJudgeContext = { waitId: wait.id, now };
    return Effect.map(
      tryStartJudge(
        wait.kind,
        wait.id,
        waitTargetText(target),
        () => judge(target, context),
        childJudgeOf(target, context),
      ),
      (result): StoredStart => {
        if (result.type === "busy") return result;
        if (result.type === "stuck") {
          return answered({
            type: "notYet",
            detail: `Checks are paused: ${result.count} earlier ${wait.kind} checks have not answered.`,
          });
        }
        const { entry } = result.started;
        return {
          type: "answer",
          answer: awaitJudge(result.started, entry.lateAt).pipe(
            Effect.map(
              Option.getOrElse(() =>
                lateJudgement(
                  `The check gave no answer in ${Duration.format(Duration.millis(poolLimitMs(entry.pool)))}.`,
                ),
              ),
            ),
          ),
        };
      },
    );
  };

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
        const changes = new Map<string, WaitTransition>();
        for (const [id, judgement] of answers) {
          const wait = loaded.contents.waits.find((candidate) => candidate.id === id);
          // Pruned while it was judged.
          if (wait === undefined) continue;
          nextCheckAt.set(id, now + intervalMs(wait.kind, settings.waitFor));
          // Null when it is no longer `waiting` (a cancel came in between) or nothing changed.
          const transition = nextWaitState(
            wait,
            { type: "judged", judgement },
            now,
            stateSettings(settings),
          );
          if (transition !== null) changes.set(id, transition);
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
        if (transition === null || transition.record.state === "waiting") return "later" as const;
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
  const isDue = (wait: WaitRecord, now: number, settings: WaitForSettings) => {
    const next = nextCheckAt.get(wait.id);
    return next === undefined || next <= now || next - now > intervalMs(wait.kind, settings);
  };

  const judgeSweep = Effect.fnUntraced(function* (settings: ServerSettings) {
    const startedAt = yield* Clock.currentTimeMillis;
    const due = yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return [];
        const waiting = new Set<string>();
        const due: WaitRecord[] = [];
        for (const wait of loaded.contents.waits) {
          if (wait.state !== "waiting") continue;
          waiting.add(wait.id);
          if (isDue(wait, startedAt, settings.waitFor)) due.push(wait);
        }
        for (const id of nextCheckAt.keys()) if (!waiting.has(id)) nextCheckAt.delete(id);
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
    const startUntil = startedAt + SWEEP_START_WINDOW_MS;
    const taken = new Set<string>();
    const later: Array<readonly [string, Judgement]> = [];
    let judged = 0;
    let ended = 0;
    const crashed = (cause: Cause.Cause<never>): Judgement =>
      lateJudgement(`The check crashed: ${causeText(cause)}`);
    const takeNext = Effect.gen(function* () {
      for (;;) {
        // Past the window nothing new starts, not even in a slot that has just been freed.
        if ((yield* Clock.currentTimeMillis) >= startUntil) return null;
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
          const start = yield* startStored(wait, startedAt).pipe(
            Effect.catchCauseIf(
              (cause) => !Cause.hasInterruptsOnly(cause),
              (cause) => Effect.succeed(answered(crashed(cause))),
            ),
          );
          if (start.type === "answer") return [wait, start.answer] as const;
          taken.delete(wait.id);
          full.add(wait.kind);
          left = true;
          wakeAt = Math.min(wakeAt, start.wakeAt);
        }
        if (!left) return null;
        const now = yield* Clock.currentTimeMillis;
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
        judged += 1;
        const saved = yield* saveIfEnds(wait.id, judgement, settings);
        if (saved === "ended") ended += 1;
        else if (saved === "later") later.push([wait.id, judgement]);
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
   * Judges every `waiting` wait that is due, at most `JUDGE_CONCURRENCY` at a time (and
   * `MAX_RUNNING_JUDGES_PER_KIND` per kind), each within its kind's `judgeLimit`. Answers that end
   * a wait are saved at once, the others in one write at the end. Does nothing while waits are off, and never overlaps itself: a call
   * during a sweep returns at once.
   */
  const judgeDue = Effect.fn("WaitService.judgeDue")(function* () {
    const settings = yield* readSettingsOrNull;
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
          return applyEvents(
            wait,
            eventsFor(result, wait, now, settings),
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
    for (const wait of current.waits) {
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
   * One sweep: deliver, judge, deliver what that ended, prune, notices; returns when the next one
   * has work. Settings are read again before each step, so "off" stops it at the next step; a
   * failing step is logged and skipped. It never overlaps itself.
   */
  const runDue = Effect.fn("WaitService.runDue")(function* () {
    const settings = yield* readSettingsOrNull;
    // Settings that cannot be read are tried again on the scheduler's next tick.
    if (settings === null) return { nextDueAt: 0 };
    if (!settings.waitFor.enabled) return { nextDueAt: Infinity };
    const stopped = { nextDueAt: Infinity };
    const result = yield* Semaphore.withPermitsIfAvailable(
      sweepLock,
      1,
    )(
      Effect.gen(function* () {
        const none = { sent: 0, ended: 0 };
        if ((yield* phase("deliver", deliverSweep, none)) === null) return stopped;
        const judged = yield* phase("judge", judgeSweep, { judged: 0, ended: 0 });
        if (judged === null) return stopped;
        if (judged.ended > 0 && (yield* phase("deliver", deliverSweep, none)) === null) {
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
