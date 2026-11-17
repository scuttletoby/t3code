/**
 * Fork (playtest): the worker's sweep (`runDue`) with a fake adapter: renew, the idle and time
 * limits, a thread that is gone, what a restart cut, and the one message about an end T3 made
 * itself. Steps are real processes; time is the real clock plus an offset a test moves.
 */
import { assert, describe, it } from "@effect/vitest";
import { type PlaytestSessionRecord, ThreadId } from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";

import * as PlaytestService from "./PlaytestService.ts";
import {
  adapter,
  type Fixture,
  fixture,
  holdStep,
  live,
  logLines,
  makeService,
  openGate,
  OTHER_THREAD,
  ran,
  removeFile,
  setFile,
  settingsWith,
  stepProcesses,
  stepStarted,
  storedSession,
  THREAD,
  writeStore,
} from "./PlaytestService.testkit.ts";

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;

/** The real clock moved by `advance`; sleeps (step time limits) stay real. */
const shiftedClock = Effect.gen(function* () {
  const real = yield* Clock.Clock;
  let offset = 0;
  const ms = () => real.currentTimeMillisUnsafe() + offset;
  const clock: Clock.Clock = {
    currentTimeMillisUnsafe: ms,
    currentTimeMillis: Effect.sync(ms),
    currentTimeNanosUnsafe: () => BigInt(ms()) * 1_000_000n,
    currentTimeNanos: Effect.sync(() => BigInt(ms()) * 1_000_000n),
    monotonicTimeNanosUnsafe: () => real.monotonicTimeNanosUnsafe(),
    monotonicTimeNanos: real.monotonicTimeNanos,
    sleep: (duration) => real.sleep(duration),
  };
  return {
    clock,
    nowMs: ms,
    iso: (shiftMs = 0) => DateTime.formatIso(DateTime.makeUnsafe(ms() + shiftMs)),
    advance: (byMs: number) => Effect.sync(() => void (offset += byMs)),
  };
});
type Time = Effect.Success<typeof shiftedClock>;

/** Every change the service reports, so a test can wait for a background step to settle. */
const changeSignal = Effect.sync(() => {
  let next = Deferred.makeUnsafe<void>();
  return {
    onChange: Effect.suspend(() => {
      const reached = next;
      next = Deferred.makeUnsafe<void>();
      return Deferred.succeed(reached, undefined);
    }).pipe(Effect.asVoid),
    /** Waits until `check` holds, looking again after every change (20 s at most). */
    until: <E, R>(check: Effect.Effect<boolean, E, R>) =>
      Effect.gen(function* () {
        while (true) {
          const change = next;
          if (yield* check) return;
          yield* Deferred.await(change);
        }
      }).pipe(
        Effect.timeoutOrElse({
          duration: "20 seconds",
          orElse: () => Effect.die(new Error("timed out waiting for the playtest service")),
        }),
      ),
  };
});

/** A notify fake: records each notice and answers from `answers` (then "delivered"). */
const notifyFake = (answers: Array<PlaytestService.PlaytestNoticeResult | "crash"> = []) => {
  const notices: Array<PlaytestService.PlaytestNotice> = [];
  return {
    notices,
    notify: (notice: PlaytestService.PlaytestNotice) =>
      Effect.suspend(() => {
        notices.push(notice);
        const answer = answers.shift() ?? "delivered";
        return answer === "crash" ? Effect.die(new Error("boom")) : Effect.succeed(answer);
      }),
  };
};

/** A sweep world: the clock, the change signal, and a service on the fixture's store. */
const world = Effect.gen(function* () {
  const fx = yield* fixture;
  const dir = yield* fx.adapterDir("a");
  const time = yield* shiftedClock;
  const signal = yield* changeSignal;
  return { fx, dir, time, signal };
});

const serviceIn = (
  w: {
    readonly fx: Fixture;
    readonly dir: string;
    readonly signal: { onChange: Effect.Effect<void> };
  },
  options: {
    readonly threadGone?: (threadId: ThreadId) => Effect.Effect<boolean | null>;
    readonly notify?: (
      notice: PlaytestService.PlaytestNotice,
    ) => Effect.Effect<PlaytestService.PlaytestNoticeResult>;
    readonly overrides?: Record<string, unknown>;
  } = {},
) =>
  Effect.gen(function* () {
    return yield* makeService(
      w.fx,
      yield* settingsWith(adapter(w.fx, w.dir, options.overrides ?? {})),
      {
        onChange: w.signal.onChange,
        ...(options.threadGone === undefined ? {} : { threadGone: options.threadGone }),
        ...(options.notify === undefined ? {} : { notify: options.notify }),
      },
    );
  });

/** Runs a test body with the shifted clock in place (forked steps inherit it). */
const withTime = <A, E, R>(body: (w: Effect.Success<typeof world>) => Effect.Effect<A, E, R>) =>
  Effect.gen(function* () {
    const w = yield* world;
    return yield* body(w).pipe(Effect.provideService(Clock.Clock, w.time.clock));
  });

/** How many times `step` started, from the fake's log. */
const starts = (dir: string, step: string) =>
  Effect.map(ran(dir), (lines) => lines.filter((line) => line.startsWith(`${step}:`)).length);
const finished = (dir: string, step: string) =>
  Effect.map(ran(dir), (lines) => lines.filter((line) => line === `finished|${step}`).length);

/** An active session that was just claimed and touched, from an earlier T3 (no renew yet). */
const freshSession = (
  fx: Fixture,
  dir: string,
  time: Time,
  overrides: Record<string, unknown> = {},
) =>
  storedSession(fx, dir, {
    startedAt: time.iso(),
    lastActivityAt: time.iso(),
    ...overrides,
  });

const status = (service: PlaytestService.PlaytestService, threadId: ThreadId = THREAD) =>
  Effect.map(service.status({ threadId }), (session) => {
    assert.isNotNull(session);
    return session!;
  });

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")("PlaytestService sweep", () => {
  it.live("after a restart the sweep renews at once, then only when renewMinutes passed", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [freshSession(fx, dir, time)]);
          const service = yield* serviceIn({ fx, dir, signal });

          const first = yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.renewedAt !== undefined));
          assert.strictEqual(yield* starts(dir, "renew"), 1);
          // Renew gets the session facts and runs in T3's own folder.
          const line = (yield* logLines(dir)).find((entry) => entry.startsWith("renew|"))!;
          assert.deepStrictEqual(line.split("|").slice(2, 4), ["pt_aaaaaaaaaa", "dev-1"]);
          assert.strictEqual(line.split("|")[8], fx.stepsFolder);
          assert.isAtMost(first.nextDueAt, time.nowMs() + 6 * HOUR);

          // Runs again before it is due: no second renew; the next one is due in 10 minutes.
          const again = yield* service.runDue();
          assert.strictEqual(yield* starts(dir, "renew"), 1);
          assert.isAtLeast(again.nextDueAt, time.nowMs() + 9 * MINUTE);
          assert.isAtMost(again.nextDueAt, time.nowMs() + 10 * MINUTE);

          yield* time.advance(10 * MINUTE);
          yield* service.runDue();
          yield* signal.until(Effect.map(finished(dir, "renew"), (count) => count === 2));
          assert.strictEqual(yield* starts(dir, "renew"), 2);
          assert.strictEqual((yield* status(service)).state, "active");
        }),
      ),
    ),
  );

  it.live("a fresh claim counts as a renew; two sweeps at once start one renew", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          const service = yield* serviceIn({ fx, dir, signal });
          assert.strictEqual(
            (yield* service.start({ threadId: THREAD, target: "dev-1" })).outcome,
            "started",
          );
          // No renew now (one would be in flight, and the next due time would be the idle limit).
          const fresh = yield* service.runDue();
          assert.isAtMost(fresh.nextDueAt, time.nowMs() + 10 * MINUTE);
          assert.isAtLeast(fresh.nextDueAt, time.nowMs() + 9 * MINUTE);
          assert.strictEqual(yield* starts(dir, "renew"), 0);

          yield* time.advance(10 * MINUTE);
          yield* holdStep(dir, "renew");
          yield* service.runDue();
          yield* stepStarted(dir, "renew");
          const [a, b] = yield* Effect.all([service.runDue(), service.runDue()], {
            concurrency: 2,
          });
          assert.isAbove(a.nextDueAt, time.nowMs());
          assert.isAbove(b.nextDueAt, time.nowMs());
          yield* openGate(dir, "renew");
          yield* signal.until(Effect.map(finished(dir, "renew"), (count) => count === 1));
          assert.strictEqual(yield* starts(dir, "renew"), 1);
        }),
      ),
    ),
  );

  it.live("renew beside a running round: both run, and neither waits for the other", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [freshSession(fx, dir, time)]);
          const service = yield* serviceIn({ fx, dir, signal });
          yield* holdStep(dir, "build");
          const round = yield* Effect.forkChild(service.push({ threadId: THREAD }));
          yield* stepStarted(dir, "build");

          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.renewedAt !== undefined));
          assert.strictEqual((yield* status(service)).running, "round");
          yield* openGate(dir, "build");
          assert.strictEqual((yield* Fiber.join(round)).outcome, "done");
        }),
      ),
    ),
  );

  it.live("renew exit 10 ends the session (claim-gone), releases, and tells the thread once", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [freshSession(fx, dir, time)]);
          const fake = notifyFake();
          const service = yield* serviceIn({ fx, dir, signal }, { notify: fake.notify });
          yield* setFile(dir, "renew.code", "10");
          yield* setFile(dir, "renew.out", "the claim on dev-1 expired");

          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.state === "ended"));
          const ended = yield* status(service);
          assert.deepInclude(ended, { endReason: "claim-gone", notice: "due" });
          assert.include(ended.problem, "renew: the claim on dev-1 expired");
          assert.deepStrictEqual((yield* ran(dir)).slice(-2), ["release:0", "finished|release"]);

          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.notice === "sent"));
          assert.strictEqual(fake.notices.length, 1);
          const [notice] = fake.notices;
          assert.deepInclude(notice, {
            threadId: THREAD,
            messageId: "playtest-pt_aaaaaaaaaa-end",
            commandId: "playtest-pt_aaaaaaaaaa-end:0",
          });
          assert.include(notice!.text, "its claim was gone when T3 renewed it");
          assert.include(notice!.text, "Release ran");
          assert.include(notice!.text, "the claim on dev-1 expired");

          // Runs again, and after a restart: no second message, no step.
          yield* service.runDue();
          const restarted = yield* serviceIn({ fx, dir, signal }, { notify: fake.notify });
          assert.strictEqual((yield* restarted.runDue()).nextDueAt, Number.POSITIVE_INFINITY);
          assert.strictEqual(fake.notices.length, 1);
          assert.strictEqual(yield* starts(dir, "release"), 1);
        }),
      ),
    ),
  );

  it.live("a failed renew keeps the session, says why, and tries again after a minute", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [freshSession(fx, dir, time)]);
          const service = yield* serviceIn({ fx, dir, signal });
          yield* setFile(dir, "renew.code", "3");

          yield* service.runDue();
          yield* signal.until(
            Effect.map(status(service), (s) => s.problem?.startsWith("renew: ") ?? false),
          );
          const failed = yield* status(service);
          assert.strictEqual(failed.state, "active");
          assert.include(failed.problem, "Exit code 3.");
          assert.isUndefined(failed.renewedAt);

          const soon = yield* service.runDue();
          assert.strictEqual(yield* starts(dir, "renew"), 1);
          assert.isAtMost(soon.nextDueAt, time.nowMs() + MINUTE);

          yield* removeFile(dir, "renew.code");
          yield* time.advance(MINUTE);
          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.renewedAt !== undefined));
          const renewed = yield* status(service);
          assert.notProperty(renewed, "problem");
          assert.strictEqual(yield* starts(dir, "renew"), 2);
        }),
      ),
    ),
  );

  it.live("an end while renew runs kills the renew, then releases", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [freshSession(fx, dir, time)]);
          const service = yield* serviceIn({ fx, dir, signal });
          yield* holdStep(dir, "renew");
          yield* service.runDue();
          yield* stepStarted(dir, "renew");

          const ended = yield* service.end({ threadId: THREAD, reason: "person" });
          assert.deepInclude(ended.session, { state: "ended", endReason: "person" });
          assert.notProperty(ended.session, "notice");
          assert.isUndefined(ended.session?.renewedAt);
          assert.strictEqual(stepProcesses(dir), "");
          const lines = yield* ran(dir);
          assert.notInclude(lines, "finished|renew");
          assert.deepStrictEqual(lines.slice(-2), ["release:0", "finished|release"]);
        }),
      ),
    ),
  );

  it.live("idle: a round or a message from the person holds it off; then the session ends", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [
            freshSession(fx, dir, time, { lastActivityAt: time.iso(-89 * MINUTE) }),
          ]);
          const fake = notifyFake();
          const service = yield* serviceIn({ fx, dir, signal }, { notify: fake.notify });

          // One minute left: not idle yet, and due again then.
          const first = yield* service.runDue();
          assert.isAtMost(first.nextDueAt, time.nowMs() + MINUTE);
          assert.strictEqual((yield* status(service)).state, "active");
          yield* signal.until(Effect.map(status(service), (s) => s.renewedAt !== undefined));

          // A round that runs past the limit is activity.
          yield* holdStep(dir, "deploy");
          const round = yield* Effect.forkChild(service.push({ threadId: THREAD }));
          yield* stepStarted(dir, "deploy");
          yield* time.advance(91 * MINUTE);
          yield* service.runDue();
          assert.strictEqual((yield* status(service)).state, "active");
          yield* openGate(dir, "deploy");
          assert.strictEqual((yield* Fiber.join(round)).outcome, "done");

          // A message from the person moves it; one from the past or the future does not.
          const afterRound = (yield* status(service)).lastActivityAt;
          yield* service.noteActivity({ threadId: THREAD, atMs: time.nowMs() - 5 * HOUR });
          assert.strictEqual((yield* status(service)).lastActivityAt, afterRound);
          yield* time.advance(80 * MINUTE);
          yield* service.noteActivity({ threadId: THREAD, atMs: time.nowMs() + 24 * HOUR });
          const touched = Date.parse((yield* status(service)).lastActivityAt);
          assert.isAbove(touched, Date.parse(afterRound));
          assert.isAtMost(touched, time.nowMs());
          // Another thread's message changes nothing here.
          yield* service.noteActivity({ threadId: OTHER_THREAD, atMs: time.nowMs() });

          yield* time.advance(89 * MINUTE);
          yield* service.runDue();
          assert.strictEqual((yield* status(service)).state, "active");
          yield* time.advance(2 * MINUTE);
          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.state === "ended"));
          assert.deepInclude(yield* status(service), { endReason: "idle", notice: "due" });
          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.notice === "sent"));
          assert.include(fake.notices[0]!.text, "nothing happened for 90 minutes");
        }),
      ),
    ),
  );

  it.live("the time limit ends the session even while a round runs, and kills the round", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [
            freshSession(fx, dir, time, { startedAt: time.iso(-6 * HOUR + MINUTE) }),
          ]);
          const service = yield* serviceIn({ fx, dir, signal });
          const before = yield* service.runDue();
          assert.isAtMost(before.nextDueAt, time.nowMs() + MINUTE);
          yield* holdStep(dir, "deploy");
          const round = yield* Effect.forkChild(service.push({ threadId: THREAD }));
          yield* stepStarted(dir, "deploy");

          yield* time.advance(MINUTE);
          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.state === "ended"));
          const ended = yield* status(service);
          assert.deepInclude(ended, { endReason: "time-limit", notice: "due" });
          assert.deepInclude(ended.lastRound, { number: 1, state: "stopped" });
          assert.strictEqual((yield* Fiber.join(round)).outcome, "stopped");
          assert.strictEqual(stepProcesses(dir), "");
          assert.notInclude(yield* ran(dir), "finished|deploy");
          assert.strictEqual(yield* starts(dir, "release"), 1);
        }),
      ),
    ),
  );

  it.live("a thread archived or deleted ends its session without a message; unreadable waits", () =>
    live(
      withTime(({ fx, dir, time, signal }) =>
        Effect.gen(function* () {
          yield* writeStore(fx, [
            freshSession(fx, dir, time),
            freshSession(fx, dir, time, { id: "pt_bbbbbbbbbb", threadId: OTHER_THREAD }),
          ]);
          const fake = notifyFake();
          const asked: Array<ThreadId> = [];
          const service = yield* serviceIn(
            { fx, dir, signal },
            {
              notify: fake.notify,
              threadGone: (threadId) =>
                Effect.sync(() => {
                  asked.push(threadId);
                  return threadId === THREAD ? true : null;
                }),
            },
          );
          yield* service.runDue();
          yield* signal.until(Effect.map(status(service), (s) => s.state === "ended"));
          const gone = yield* status(service);
          assert.strictEqual(gone.endReason, "thread-gone");
          assert.notProperty(gone, "notice");
          assert.strictEqual((yield* status(service, OTHER_THREAD)).state, "active");
          yield* service.runDue();
          assert.deepStrictEqual(fake.notices, []);
          // An ended session's thread is not read again.
          assert.notInclude(asked.slice(2), THREAD);
        }),
      ),
    ),
  );

  it.live(
    "after a restart: a cut claim is released (claim-failed); a cut or failed release reruns every renewMinutes",
    () =>
      live(
        withTime(({ fx, dir, time, signal }) =>
          Effect.gen(function* () {
            yield* writeStore(fx, [
              freshSession(fx, dir, time, { state: "claiming" }),
              freshSession(fx, dir, time, {
                id: "pt_bbbbbbbbbb",
                threadId: OTHER_THREAD,
                state: "ending",
                endReason: "agent",
              }),
            ]);
            const fake = notifyFake();
            const service = yield* serviceIn({ fx, dir, signal }, { notify: fake.notify });
            yield* setFile(dir, "release.code", "4");

            yield* service.runDue();
            const bothTried = Effect.gen(function* () {
              const a = yield* status(service);
              const b = yield* status(service, OTHER_THREAD);
              return (
                a.running === undefined &&
                b.running === undefined &&
                (yield* finished(dir, "release")) === 2
              );
            });
            yield* signal.until(bothTried);
            const cut = yield* status(service);
            assert.deepInclude(cut, { state: "ending", endReason: "claim-failed", notice: "due" });
            assert.include(cut.problem, "release: Exit code 4.");
            assert.deepInclude(yield* status(service, OTHER_THREAD), { state: "ending" });
            assert.strictEqual(yield* starts(dir, "claim"), 0);

            // The failed release is said once; it is not tried again before renewMinutes.
            const soon = yield* service.runDue();
            yield* signal.until(Effect.map(status(service), (s) => s.notice === "sent"));
            assert.strictEqual(fake.notices.length, 1);
            assert.include(fake.notices[0]!.text, "T3 stopped while its claim ran");
            assert.include(fake.notices[0]!.text, "has not worked yet");
            assert.strictEqual(yield* starts(dir, "release"), 2);
            assert.isAtMost(soon.nextDueAt, time.nowMs() + 10 * MINUTE);
            yield* time.advance(9 * MINUTE);
            yield* service.runDue();
            assert.strictEqual(yield* starts(dir, "release"), 2);

            yield* removeFile(dir, "release.code");
            yield* time.advance(MINUTE);
            yield* service.runDue();
            yield* signal.until(
              Effect.map(
                Effect.all([status(service), status(service, OTHER_THREAD)]),
                ([a, b]) => a.state === "ended" && b.state === "ended",
              ),
            );
            assert.strictEqual(yield* starts(dir, "release"), 4);
            // The agent's end gets no message; the claim's message went out already.
            yield* service.runDue();
            assert.strictEqual(fake.notices.length, 1);
            assert.notProperty(yield* status(service, OTHER_THREAD), "notice");
          }),
        ),
      ),
  );

  it.live(
    "the message: retry and a crash keep it due for a minute; a thread that cannot take it drops it",
    () =>
      live(
        withTime(({ fx, dir, time, signal }) =>
          Effect.gen(function* () {
            yield* writeStore(fx, [
              freshSession(fx, dir, time, { lastActivityAt: time.iso(-2 * HOUR) }),
            ]);
            const fake = notifyFake(["retry", "crash", "dropped"]);
            const service = yield* serviceIn({ fx, dir, signal }, { notify: fake.notify });
            yield* service.runDue();
            yield* signal.until(Effect.map(status(service), (s) => s.state === "ended"));

            const tries = (count: number) =>
              signal.until(Effect.sync(() => fake.notices.length === count));
            // While a send runs the sweep has nothing due; once it answered "retry", its retry time.
            const settledTry = Effect.map(service.runDue(), (next) => next.nextDueAt);
            yield* service.runDue();
            yield* tries(1);
            yield* signal.until(Effect.map(settledTry, (at) => at !== Number.POSITIVE_INFINITY));
            assert.strictEqual((yield* status(service)).notice, "due");
            assert.strictEqual(fake.notices.length, 1);

            yield* time.advance(MINUTE);
            yield* service.runDue();
            yield* tries(2);
            yield* signal.until(Effect.map(settledTry, (at) => at !== Number.POSITIVE_INFINITY));
            assert.strictEqual((yield* status(service)).notice, "due");

            yield* time.advance(MINUTE);
            yield* service.runDue();
            yield* signal.until(Effect.map(status(service), (s) => s.notice === "sent"));
            assert.strictEqual(fake.notices.length, 3);
            // The same ids every time: the thread gets it at most once.
            assert.deepStrictEqual(
              new Set(fake.notices.map((notice) => `${notice.messageId} ${notice.commandId}`)).size,
              1,
            );
          }),
        ),
      ),
  );

  it.live(
    "empty, old and unreadable stores: nothing runs, and the sweep says when to look again",
    () =>
      live(
        withTime(({ fx, dir, time, signal }) =>
          Effect.gen(function* () {
            const fs = yield* FileSystem.FileSystem;
            const empty = yield* serviceIn({ fx, dir, signal });
            assert.strictEqual((yield* empty.runDue()).nextDueAt, Number.POSITIVE_INFINITY);
            assert.isFalse(yield* fs.exists(fx.filePath));

            // A store from before the worker (no `notice`) with an ended session.
            const old = freshSession(fx, dir, time, {
              state: "ended",
              endReason: "agent",
              endedAt: time.iso(),
            });
            yield* writeStore(fx, [old]);
            const before = yield* serviceIn({ fx, dir, signal });
            assert.strictEqual((yield* before.runDue()).nextDueAt, Number.POSITIVE_INFINITY);
            assert.strictEqual((yield* status(before)).state, "ended");

            // A broken store is set aside (by the store) and read as empty.
            yield* fs.writeFileString(fx.filePath, "{ not json");
            const broken = yield* serviceIn({ fx, dir, signal });
            assert.strictEqual((yield* broken.runDue()).nextDueAt, Number.POSITIVE_INFINITY);

            // A store from a newer T3 is never read nor written: looked at again in a minute.
            const newer = `${JSON.stringify({ version: 99, sessions: [] })}\n`;
            yield* fs.writeFileString(fx.filePath, newer);
            const later = yield* serviceIn({ fx, dir, signal });
            const due = yield* later.runDue();
            assert.isAtMost(due.nextDueAt, time.nowMs() + MINUTE);
            assert.isAbove(due.nextDueAt, time.nowMs());
            assert.strictEqual(yield* fs.readFileString(fx.filePath), newer);
            assert.deepStrictEqual(yield* ran(dir), []);
          }),
        ),
      ),
  );

  it("noticeText says why, whether the target is back, and that the worktree stays", () => {
    const base = {
      id: "pt_aaaaaaaaaa",
      target: "dev-1",
      adapter: { renewMinutes: 10, idleMinutes: 90, maxHours: 6 },
    } as unknown as PlaytestSessionRecord;
    const limit = PlaytestService.noticeText({ ...base, state: "ended", endReason: "time-limit" });
    assert.include(limit, "time limit of 6 hours");
    assert.include(limit, "Release ran");
    assert.include(limit, "worktree and its changes are kept");
    const stuck = PlaytestService.noticeText({
      ...base,
      state: "ending",
      endReason: "idle",
      problem: "release: Exit code 4.",
    });
    assert.include(stuck, "has not worked yet");
    assert.include(stuck, "every 10 minutes");
  });
});
