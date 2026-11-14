/**
 * Fork (waits): register, list, cancel, judge, prune and lost-wait notices over one store file.
 * A change is kept in memory only after its write. With waits off, nothing is read or written
 * except by list and cancel, which never repair. A cancel between a judge and its apply wins.
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
  ENDED_PER_THREAD_FACTOR,
  type Judgement,
  MAX_ENDED_WAITS,
  MAX_OPEN_WAITS,
  nextWaitState,
  shouldPruneWait,
  validateWaitSpec,
  waitDeadline,
  waitKey,
  type WaitStateSettings,
  waitTargetText,
  type WaitTransition,
} from "@t3tools/shared/waitFor";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as Option from "effect/Option";
import * as Schema from "effect/Schema";
import * as Semaphore from "effect/Semaphore";

import { ServerSettingsService } from "../serverSettings.ts";
import { makeFileJudge, type WaitJudge, type WaitJudges } from "./WaitJudges.ts";
import * as WaitStore from "./WaitStore.ts";

/** At registration a judge that has not answered by then counts as "not yet" (R5). */
const REGISTER_JUDGE_LIMIT = "3 seconds";
/** At most this many judges run at once. */
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
 * A judge that did not answer in time keeps running (a `stat` on a dead mount holds a thread of
 * the I/O pool until the system gives up), and its wait gets no second one meanwhile. While this
 * many judges of a kind are still running late, no new judge of that kind starts.
 */
const MAX_LATE_JUDGES_PER_KIND = 2;
const WAIT_ID_ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";

/** A message to a thread. PR 3 sends it with the thread tools' send; tests use a fake. */
export interface WaitMessage {
  readonly threadId: ThreadId;
  readonly messageId: string;
  /** The same id on every try, so a resend after a crash is answered from the receipt. */
  readonly commandId: string;
  readonly text: string;
}

export class WaitSendError extends Schema.TaggedError<WaitSendError>()("WaitSendError", {
  detail: Schema.String,
  /** False when the same command can never be accepted (the thread is gone, the id refused). */
  retry: Schema.Boolean,
}) {}

export type WaitSend = (message: WaitMessage) => Effect.Effect<void, WaitSendError>;

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

type Loaded =
  | { readonly ok: true; readonly contents: WaitStore.WaitStoreContents }
  | { readonly ok: false; readonly reason: string };

export const make = Effect.fn("WaitService.make")(function* (input: {
  readonly filePath: string;
  readonly send: WaitSend;
  /** Replaces the built-in judge of a kind; tests use slow or scripted ones. */
  readonly judges?: WaitJudges;
}) {
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
  /** Judges started by a sweep that have not returned, by wait id; `late` once past the limit. */
  const running = new Map<string, { readonly kind: WaitKind; late: boolean }>();

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
   * beyond `ENDED_PER_THREAD_FACTOR × maxPerThread` per thread and `MAX_ENDED_WAITS` in all.
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
    yield* store.write(capped);
    current = capped;
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

    const judgement = yield* judge(target, { waitId: null, now }).pipe(
      Effect.timeoutOption(REGISTER_JUDGE_LIMIT),
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
    if (running.has(wait.id)) {
      return Effect.succeed(lateJudgement("Its last check has not answered yet."));
    }
    const late = [...running.values()].filter(
      (entry) => entry.kind === wait.kind && entry.late,
    ).length;
    if (late >= MAX_LATE_JUDGES_PER_KIND) {
      return Effect.succeed(
        lateJudgement(`${late} ${wait.kind} checks have not answered, so no new one was started.`),
      );
    }
    const limit = judgeLimit(wait.kind);
    return Effect.gen(function* () {
      const entry = { kind: wait.kind, late: false };
      running.set(wait.id, entry);
      const fiber = yield* Effect.forkDetach(
        judge(targetOf(wait), { waitId: wait.id, now }).pipe(
          Effect.ensuring(
            Effect.sync(() => {
              if (running.get(wait.id) === entry) running.delete(wait.id);
            }),
          ),
        ),
      );
      const answer = yield* Fiber.join(fiber).pipe(Effect.timeoutOption(limit));
      if (Option.isSome(answer)) return answer.value;
      entry.late = true;
      return lateJudgement(`The check gave no answer in ${Duration.format(limit)}.`);
    });
  };

  /** Applies one judge's answer and saves it on its own, so a slow wait holds back no other. */
  const applyJudgement = (id: string, judgement: Judgement, settings: ServerSettings) =>
    lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return false;
        const wait = loaded.contents.waits.find((candidate) => candidate.id === id);
        // Pruned while it was judged.
        if (wait === undefined) return false;
        // The deadline is tested after the judge (R4), at the time of the answer.
        const now = yield* Clock.currentTimeMillis;
        nextCheckAt.set(id, now + intervalMs(wait.kind, settings.waitFor));
        // Null when it is no longer `waiting` (a cancel came in between) or nothing changed.
        const transition = nextWaitState(
          wait,
          { type: "judged", judgement },
          now,
          stateSettings(settings),
        );
        if (transition === null) return false;
        const waits = loaded.contents.waits.map((candidate) =>
          candidate.id === id ? transition.record : candidate,
        );
        const written = yield* commit({ ...loaded.contents, waits }, settings.waitFor).pipe(
          Effect.as(true),
          Effect.catch((error) =>
            Effect.logWarning(`waits: ${error.message}; judged again next time.`).pipe(
              Effect.as(false),
            ),
          ),
        );
        if (!written) return false;
        yield* logTransition(transition);
        return transition.record.state !== "waiting";
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

  const sweep = Effect.fnUntraced(function* (settings: ServerSettings) {
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
    const ended = yield* Effect.forEach(
      due,
      (wait) =>
        judgeStored(wait, startedAt).pipe(
          Effect.flatMap((judgement) => applyJudgement(wait.id, judgement, settings)),
        ),
      { concurrency: JUDGE_CONCURRENCY },
    );
    return { judged: due.length, ended: ended.filter(Boolean).length };
  });

  /**
   * Judges every `waiting` wait that is due, at most `JUDGE_CONCURRENCY` at a time, each within
   * its kind's `judgeLimit`. Does nothing while waits are off, and never overlaps itself: a call
   * during a sweep returns at once.
   */
  const judgeDue = Effect.fn("WaitService.judgeDue")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return { judged: 0, ended: 0 };
    const result = yield* Semaphore.withPermitsIfAvailable(sweepLock, 1)(sweep(settings));
    return Option.getOrElse(result, () => ({ judged: 0, ended: 0 }));
  });

  /** Removes ended waits older than `keepEndedHours`. Does nothing while waits are off. */
  const prune = Effect.fn("WaitService.prune")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return 0;
    return yield* lock.withPermits(1)(
      Effect.gen(function* () {
        const loaded = yield* ensureLoaded(true);
        if (!loaded.ok) return 0;
        const now = yield* Clock.currentTimeMillis;
        const waits = loaded.contents.waits.filter(
          (wait) => !shouldPruneWait(wait, now, settings.waitFor),
        );
        const removed = loaded.contents.waits.length - waits.length;
        if (removed === 0) return 0;
        return yield* commit({ ...loaded.contents, waits }, settings.waitFor).pipe(
          Effect.as(removed),
          Effect.catch((error) =>
            Effect.logWarning(`waits: ${error.message}; pruned next time.`).pipe(Effect.as(0)),
          ),
        );
      }),
    );
  });

  /**
   * Sends the notices for waits lost in a bad file. A notice leaves the file once it was sent or
   * can never be; a resend after a crash has the same command id, so it is one message. Call it
   * from one place only (PR 3's loop): two calls at once both send every pending notice, and only
   * the command-id receipt would keep that to one message.
   */
  const sendNotices = Effect.fn("WaitService.sendNotices")(function* () {
    const settings = yield* readSettingsOrNull;
    if (settings === null || !settings.waitFor.enabled) return 0;
    const pending = yield* lock.withPermits(1)(
      ensureLoaded(true).pipe(Effect.map((loaded) => (loaded.ok ? loaded.contents.notices : []))),
    );
    if (pending.length === 0) return 0;
    const done = new Set<string>();
    let sent = 0;
    for (const notice of pending) {
      const result = yield* input
        .send({
          threadId: notice.threadId,
          messageId: notice.messageId,
          commandId: `${notice.messageId}:0`,
          text: notice.text,
        })
        .pipe(Effect.result);
      if (result._tag === "Success") {
        sent += 1;
        done.add(notice.messageId);
      } else if (!result.failure.retry) {
        done.add(notice.messageId);
        yield* Effect.logError(
          `waits: thread ${notice.threadId} could not be told its waits were lost: ${result.failure.detail}`,
        );
      }
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

  return { register, list, cancel, judgeDue, prune, sendNotices };
});

export type WaitService = Effect.Success<ReturnType<typeof make>>;
