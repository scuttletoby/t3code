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
  MAX_OPEN_WAITS,
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
import { makeFileJudge, type WaitJudge, type WaitJudges } from "./WaitJudges.ts";
import * as WaitStore from "./WaitStore.ts";

/** At registration a judge that has not answered by then counts as "not yet" (R5). */
const REGISTER_JUDGE_LIMIT = "3 seconds";
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
 * all are late, waits of that kind answer "not yet", never "failed".
 */
const MAX_RUNNING_JUDGES_PER_KIND = 2;
const SEND_CONCURRENCY = 4;
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

interface RunningJudge {
  readonly waitId: string | null;
  readonly target: string;
  late: boolean;
}

interface StartedJudge {
  readonly fiber: Fiber.Fiber<Judgement>;
  readonly entry: RunningJudge;
}

type Loaded =
  | { readonly ok: true; readonly contents: WaitStore.WaitStoreContents }
  | { readonly ok: false; readonly reason: string };

export const make = Effect.fn("WaitService.make")(function* (input: {
  readonly filePath: string;
  readonly delivery: WaitDelivery;
  /** Replaces the built-in judge of a kind; tests use slow or scripted ones. */
  readonly judges?: WaitJudges;
  /** Runs after a register or cancel changed the store, so the sweep looks again at once. */
  readonly onChange?: Effect.Effect<void>;
}) {
  const onChange = input.onChange ?? Effect.void;
  const settingsService = yield* ServerSettingsService;
  const platform = yield* HostProcess.Platform;
  const store = yield* WaitStore.make({ filePath: input.filePath });
  const judges: WaitJudges = { file: yield* makeFileJudge(), ...input.judges };

  const lock = yield* Semaphore.make(1);
  const sweepLock = yield* Semaphore.make(1);
  /** Null until the file was read; only ever changed under `lock`. */
  let current: WaitStore.WaitStoreContents | null = null;
  let lastProblem: string | null = null;
  /** When each `waiting` wait is judged next; empty after a restart, so all are judged at once. */
  const nextCheckAt = new Map<string, number>();
  /** Checks that have not returned, per kind; `late` once past their limit. */
  const running = new Map<WaitKind, Set<RunningJudge>>();
  /** Kinds whose checks are all stuck, already logged; cleared when one of them returns. */
  const blockedLogged = new Set<WaitKind>();
  /** Completed (and replaced) whenever a check starts, returns or turns late. */
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

    // With no free check of its kind, the first sweep judges it instead.
    const started = yield* startJudge(target.kind, null, waitTargetText(target), () =>
      judge(target, { waitId: null, now }),
    );
    const judgement: Judgement =
      started === null
        ? { type: "notYet" }
        : yield* awaitJudge(started, REGISTER_JUDGE_LIMIT).pipe(
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
        nextCheckAt.set(record.id, now + intervalMs(record.kind, settings.waitFor));
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

  const runningOf = (kind: WaitKind) => {
    let set = running.get(kind);
    if (set === undefined) {
      set = new Set();
      running.set(kind, set);
    }
    return set;
  };

  /**
   * Starts a check in its own fiber when its kind has a free place (see
   * `MAX_RUNNING_JUDGES_PER_KIND`), or returns null. The place is freed when the check returns,
   * however late; a check that never returns keeps it until a restart.
   */
  const startJudge = (
    kind: WaitKind,
    waitId: string | null,
    target: string,
    judge: () => Effect.Effect<Judgement>,
  ): Effect.Effect<StartedJudge | null> =>
    Effect.suspend(() => {
      const set = runningOf(kind);
      if (set.size >= MAX_RUNNING_JUDGES_PER_KIND) return Effect.succeed(null);
      const entry: RunningJudge = { waitId, target, late: false };
      set.add(entry);
      return Effect.forkDetach(
        Effect.suspend(judge).pipe(
          Effect.ensuring(
            Effect.suspend(() => {
              set.delete(entry);
              blockedLogged.delete(kind);
              return signalJudgesChanged;
            }),
          ),
        ),
      ).pipe(
        Effect.tap(() => signalJudgesChanged),
        Effect.map((fiber) => ({ fiber, entry })),
      );
    });

  /**
   * The check's answer within `limit`, or none. Past the limit (or when the caller stops waiting)
   * the check is marked late, which is what lets `startJudgeWhenFree` stop waiting for it.
   */
  const awaitJudge = (started: StartedJudge, limit: Duration.Input) =>
    Fiber.join(started.fiber).pipe(
      Effect.timeoutOption(limit),
      Effect.onExit((exit) =>
        exit._tag === "Success" && Option.isSome(exit.value)
          ? Effect.void
          : Effect.suspend(() => {
              if (started.entry.late) return Effect.void;
              started.entry.late = true;
              return signalJudgesChanged;
            }),
      ),
    );

  /**
   * A free place for a check of this kind: waits while a running check may still answer in time,
   * null at once when every running one is late (stuck), with one error log per stuck spell.
   */
  const startJudgeWhenFree = Effect.fn("WaitService.startJudgeWhenFree")(function* (
    kind: WaitKind,
    waitId: string,
    target: string,
    judge: () => Effect.Effect<Judgement>,
  ) {
    for (;;) {
      const changed = judgesChanged;
      const started = yield* startJudge(kind, waitId, target, judge);
      if (started !== null) return started;
      const set = runningOf(kind);
      if ([...set].every((entry) => entry.late)) {
        if (!blockedLogged.has(kind)) {
          blockedLogged.add(kind);
          const stuck = [...set].map(
            (entry) => `${entry.waitId ?? "registration"}: ${entry.target.slice(0, 200)}`,
          );
          yield* Effect.logError(
            `waits: ${set.size} ${kind} checks have not answered (${stuck.join("; ")}). No new ${kind} check starts until one returns; a dead network mount is the usual cause, and a restart clears it.`,
          );
        }
        return null;
      }
      yield* Deferred.await(changed);
    }
  });

  /**
   * One judge for a stored wait, with a time limit. The judge runs in its own fiber, so one that
   * never answers is left running (not started again) instead of holding up the sweep.
   */
  const judgeStored = (wait: WaitRecord, now: number): Effect.Effect<Judgement> => {
    const judge = judgeOf(wait.kind);
    // Its deadline still ends it, so a wait this build cannot judge is never stuck.
    if (judge === undefined) {
      return Effect.succeed({
        type: "notYet",
        detail: `This server cannot judge ${wait.kind} waits.`,
      });
    }
    const set = runningOf(wait.kind);
    if ([...set].some((entry) => entry.waitId === wait.id)) {
      return Effect.succeed(lateJudgement("Its last check has not answered yet."));
    }
    const limit = judgeLimit(wait.kind);
    return Effect.gen(function* () {
      const started = yield* startJudgeWhenFree(
        wait.kind,
        wait.id,
        waitTargetText(targetOf(wait)),
        () => judge(targetOf(wait), { waitId: wait.id, now }),
      );
      if (started === null) {
        return {
          type: "notYet",
          detail: `Checks are paused: ${set.size} earlier ${wait.kind} checks have not answered.`,
        } satisfies Judgement;
      }
      const answer = yield* awaitJudge(started, limit);
      if (Option.isSome(answer)) return answer.value;
      return lateJudgement(`The check gave no answer in ${Duration.format(limit)}.`);
    });
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
    // Every judge has a time limit, so the pass ends, and its answers are saved in one write; a
    // check that answers after its limit is ignored, and its wait is judged again later.
    const answers = yield* Effect.forEach(
      due,
      (wait) =>
        judgeStored(wait, startedAt).pipe(Effect.map((judgement) => [wait.id, judgement] as const)),
      { concurrency: JUDGE_CONCURRENCY },
    );
    const ended = yield* applyJudgements(answers, settings);
    return { judged: due.length, ended };
  });

  /**
   * Judges every `waiting` wait that is due, at most `JUDGE_CONCURRENCY` at a time (and
   * `MAX_RUNNING_JUDGES_PER_KIND` per kind), each within its kind's `judgeLimit`, and saves the
   * answers in one write. Does nothing while waits are off, and never overlaps itself: a call
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
        return [{ type: "threadStopped", parentThreadId: result.parentThreadId }];
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
      const receiver = yield* input.delivery.receiver(threadId);
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
        const fresh = new Map<string, string>();
        for (const group of due) {
          if (!group.fresh) continue;
          for (const wait of group.waits) fresh.set(wait.id, group.messageId);
        }
        if (fresh.size > 0) {
          waits = waits.map((wait) => {
            const messageId = fresh.get(wait.id);
            return messageId === undefined
              ? wait
              : applyEvents(wait, [{ type: "sendStarted", messageId }], startedAt, settings, []);
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

    // 3. Send, outside the lock: a slow thread never holds up register, list or cancel.
    const results = yield* Effect.forEach(
      planned,
      (group) =>
        input.delivery
          .deliver({
            threadId: group.threadId,
            messageId: group.messageId,
            commandId: deliveryCommandId(group),
            text: joinedWakeMessage(group.waits),
          })
          .pipe(Effect.map((result) => [group, result] as const)),
      { concurrency: SEND_CONCURRENCY },
    );

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
   * Sends the notices for waits lost in a bad file. A notice leaves the file once it was sent or
   * can never be; a resend after a crash has the same command id, so it is one message. It runs
   * only under `sweepLock` (the sweep, or `sendNotices` when no sweep runs), so two never overlap.
   */
  const noticesSweep = Effect.fnUntraced(function* (settings: ServerSettings) {
    const pending = yield* lock.withPermits(1)(
      ensureLoaded(true).pipe(Effect.map((loaded) => (loaded.ok ? loaded.contents.notices : []))),
    );
    if (pending.length === 0) return 0;
    const done = new Set<string>();
    let sent = 0;
    for (const notice of pending) {
      const result = yield* input.delivery.deliver({
        threadId: notice.threadId,
        messageId: notice.messageId,
        commandId: `${notice.messageId}:0`,
        text: notice.text,
      });
      switch (result.type) {
        case "delivered":
          sent += 1;
          done.add(notice.messageId);
          break;
        case "rejected":
        case "gone":
          done.add(notice.messageId);
          yield* Effect.logError(
            `waits: thread ${notice.threadId} could not be told its waits were lost: ${result.type === "rejected" ? result.detail : "it is archived or deleted"}`,
          );
          break;
        // Held until the thread runs again, or tried again later.
        case "stopped":
        case "retry":
          break;
      }
    }
    if (done.size < pending.length) {
      noticesRetryAt = (yield* Clock.currentTimeMillis) + PROBLEM_RECHECK_MS;
    }
    if (done.size > 0) {
      yield* lock.withPermits(1)(
        Effect.gen(function* () {
          if (current === null) return;
          const contents = current;
          const notices = contents.notices.filter((notice) => !done.has(notice.messageId));
          yield* commit({ ...contents, notices }, settings.waitFor).pipe(
            Effect.catch((error) =>
              Effect.logWarning(`waits: ${error.message}; the notices are sent again next time.`),
            ),
          );
        }),
      );
    }
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
   * One sweep, in the spec's order: deliver, judge, deliver what that ended, prune, notices.
   * Returns when the next one has work (for the scheduler's due hint). With waits off it reads
   * and writes nothing; it never overlaps itself.
   */
  const runDue = Effect.fn("WaitService.runDue")(function* () {
    const settings = yield* readSettingsOrNull;
    // Settings that cannot be read are tried again on the scheduler's next tick.
    if (settings === null) return { nextDueAt: 0 };
    if (!settings.waitFor.enabled) return { nextDueAt: Infinity };
    const result = yield* Semaphore.withPermitsIfAvailable(
      sweepLock,
      1,
    )(
      Effect.gen(function* () {
        yield* deliverSweep(settings);
        const judged = yield* judgeSweep(settings);
        if (judged.ended > 0) yield* deliverSweep(settings);
        yield* pruneSweep(settings);
        yield* noticesSweep(settings);
        const now = yield* Clock.currentTimeMillis;
        return yield* lock.withPermits(1)(
          Effect.sync(() => ({ nextDueAt: nextDueAt(settings, now) })),
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
