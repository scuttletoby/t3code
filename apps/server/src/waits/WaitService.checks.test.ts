/**
 * Fork (waits, PR 5): the check and unit kinds in the wait service. A check runs a command from
 * settings and may run for minutes; a unit reads systemd. Each test answers part of the state
 * question: runs again, stops half-way, a check removed while a wait uses it, a restart mid-check,
 * a server without systemd, waits switched off, and one source failing for many threads at once.
 */
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitCheck, WaitRecord } from "@t3tools/contracts/waitFor";
import type { Judgement } from "@t3tools/shared/waitFor";
import * as DateTime from "effect/DateTime";
import * as Clock from "effect/Clock";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import { TestClock } from "effect/testing";

import * as ProcessRunner from "../processRunner.ts";
import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery } from "./WaitDelivery.ts";
import { testArgPattern } from "./argPattern.ts";
import {
  type CheckPlace,
  makeCheckJudge,
  makeUnitJudge,
  type WaitJudgeContext,
  type WaitJudges,
} from "./WaitJudges.ts";
import * as WaitService from "./WaitService.ts";
import { memoryFileSystem } from "./WaitService.testkit.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const SECOND = 1_000;
const MINUTE = 60 * SECOND;
const GRACE = 15 * MINUTE;
const THREAD = ThreadId.make("thread-a");
const PROJECT = ProjectId.make("project-a");
const filePath = `/waits/state/${WAIT_STORE_FILE}`;

const check = (fields: Partial<WaitCheck> = {}): WaitCheck => ({
  argv: ["build-check"],
  maxArgs: 0,
  argPattern: "^[a-z]+$",
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
  ...fields,
});
const ON = { waitFor: { enabled: true } } as const;
const withChecks = (checks: Record<string, WaitCheck>) =>
  ServerSettingsService.layerTest({ waitFor: { enabled: true }, waitChecks: checks });

const ALPHABET = "abcdefghijklmnopqrstuvwxyz234567";
const idOf = (n: number) =>
  `w_${Array.from({ length: 10 }, (_, index) => ALPHABET[Math.floor(n / 32 ** (9 - index)) % 32]).join("")}`;
const iso = (ms: number) => DateTime.formatIso(DateTime.makeUnsafe(ms));

/** A check wait as an earlier run stored it (PR 5 adds no record fields). */
const checkRecord = (n: number, overrides: Record<string, unknown> = {}) => ({
  id: idOf(n),
  threadId: THREAD,
  projectId: PROJECT,
  key: n.toString(16).padStart(16, "0"),
  createdAt: iso(NOW - 2 * GRACE),
  deadlineAt: iso(NOW + 24 * 60 * MINUTE),
  state: "waiting",
  attempt: 0,
  kind: "check",
  spec: { name: "build", args: [] },
  ...overrides,
});

/** The store in memory; `failRenames` makes the atomic write fail, as a full disk would. */
const memoryStore = () => {
  const memory = memoryFileSystem();
  const touched: string[] = [];
  let failRenames = false;
  const fs = new Proxy(memory.fs, {
    get(target, property, receiver) {
      const value = Reflect.get(target, property, receiver);
      if (typeof value !== "function") return value;
      return (...args: unknown[]) => {
        touched.push(String(property));
        if (property === "rename" && failRenames) {
          return Effect.fail(
            PlatformError.systemError({
              _tag: "Unknown",
              module: "FileSystem",
              method: "rename",
              description: "disk full",
              pathOrDescriptor: String(args[1]),
            }),
          );
        }
        return value.apply(target, args);
      };
    },
  });
  return {
    fs,
    touched,
    files: memory.files,
    seed: (waits: ReadonlyArray<unknown>) =>
      void memory.files.set(filePath, JSON.stringify({ version: 1, waits })),
    stored: () =>
      (JSON.parse(memory.files.get(filePath) ?? '{"waits":[]}') as { waits: WaitRecord[] }).waits,
    failRenames: (on: boolean) => {
      failRenames = on;
    },
  };
};

/** Every thread is there and takes every message; the messages are kept. */
const recordingDelivery = () => {
  const sent: WaitService.WaitMessage[] = [];
  const delivery: WaitDelivery = {
    receiver: () => Effect.succeed({ type: "ok" }),
    deliver: (message) =>
      Effect.sync(() => {
        sent.push(message);
        return { type: "delivered" } as const;
      }),
  };
  return { sent, delivery };
};

const makeService = (input: {
  readonly store: ReturnType<typeof memoryStore>;
  readonly judges?: WaitJudges;
  readonly delivery?: WaitDelivery;
  readonly onChange?: Effect.Effect<void>;
  readonly checkPlaceOf?: (context: WaitJudgeContext) => Effect.Effect<CheckPlace>;
}) =>
  WaitService.make({
    filePath,
    delivery: input.delivery ?? recordingDelivery().delivery,
    fileChildJudge: null,
    ...(input.judges === undefined ? {} : { judges: input.judges }),
    ...(input.onChange === undefined ? {} : { onChange: input.onChange }),
    ...(input.checkPlaceOf === undefined ? {} : { checkPlaceOf: input.checkPlaceOf }),
  }).pipe(Effect.provideService(FileSystem.FileSystem, input.store.fs), Effect.provide(Path.layer));

const registerCheck = (service: WaitService.WaitService, name = "build", threadId = THREAD) =>
  service.register({
    threadId,
    projectId: PROJECT,
    input: { kind: "check", check: { name } },
  });

const letFibersRun = Effect.gen(function* () {
  for (let turn = 0; turn < 20; turn += 1) yield* Effect.yieldNow;
});
const stepSeconds = (seconds: number) =>
  Effect.gen(function* () {
    for (let second = 0; second < seconds; second += 1) {
      yield* TestClock.adjust(SECOND);
      for (let turn = 0; turn < 5; turn += 1) yield* Effect.yieldNow;
    }
  });

/** A process runner that answers with `exitCode` and records every command line. */
const fakeRunner = (answer: () => Partial<ProcessRunner.ProcessRunOutput>) => {
  const runs: string[][] = [];
  const layer = Layer.succeed(ProcessRunner.ProcessRunner, {
    run: (input) =>
      Effect.sync(() => {
        runs.push([input.command, ...input.args]);
        return {
          stdout: "",
          stderr: "",
          code: 0 as ProcessRunner.ProcessRunOutput["code"],
          timedOut: false,
          stdoutTruncated: false,
          stderrTruncated: false,
          stdoutInvalidUtf8: false,
          stderrInvalidUtf8: false,
          ...answer(),
        };
      }),
  });
  return { runs, layer };
};

const warnings = () => {
  const lines: string[] = [];
  const logger = Logger.make(({ logLevel, message }) => {
    if (logLevel === "Warn") lines.push(String(Array.isArray(message) ? message[0] : message));
  });
  return { lines, layer: Logger.layer([logger], { mergeWithExisting: false }) };
};

const notYet: Judgement = { type: "notYet" };
/** A judge that answers not yet and counts its runs. */
const counting = () => {
  const counter = { runs: 0 };
  const judge = () =>
    Effect.sync((): Judgement => {
      counter.runs += 1;
      return notYet;
    });
  return { counter, judge };
};

it.effect("a check wait is judged at its own intervalSeconds", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const { counter, judge } = counting();
    const service = yield* makeService({ store, judges: { check: judge } });
    const wait = yield* registerCheck(service);
    assert.strictEqual(wait.state, "waiting");
    assert.strictEqual(counter.runs, 1);
    yield* TestClock.adjust(44 * SECOND);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
    yield* TestClock.adjust(SECOND);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
    assert.strictEqual(counter.runs, 2);
  }).pipe(Effect.provide(withChecks({ build: check({ intervalSeconds: 45 }) }))),
);

it.effect(
  "a check that runs past its pass goes on in its place; the pass ends, a later pass takes its answer (#95)",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      store.seed([checkRecord(1)]);
      let starts = 0;
      let changes = 0;
      const service = yield* makeService({
        store,
        // 100 s, under its 300 s limit.
        judges: {
          check: () =>
            Effect.sync(() => (starts += 1)).pipe(
              Effect.andThen(Effect.sleep(100 * SECOND)),
              Effect.as<Judgement>({ type: "met", outcome: "met", detail: "green" }),
            ),
        },
        onChange: Effect.sync(() => void (changes += 1)),
      });
      const pass = yield* Effect.forkChild(service.judgeDue());
      yield* letFibersRun;
      yield* stepSeconds(75);
      yield* letFibersRun;
      assert.isDefined(pass.pollUnsafe(), "the pass ended before the check");
      assert.deepStrictEqual(yield* Fiber.join(pass), { judged: 0, ended: 0 });
      assert.strictEqual(store.stored()[0]?.state, "waiting");

      // While it still runs, a pass neither starts it again nor gives up on it.
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
      assert.strictEqual(starts, 1);

      yield* stepSeconds(30);
      yield* letFibersRun;
      assert.isAtLeast(changes, 1, "its answer wakes the sweep");
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      assert.strictEqual(store.stored()[0]?.result?.detail, "green");
      assert.strictEqual(starts, 1);
    }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect("a check that never answers counts as a failed read after its timeout plus 5 s", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([checkRecord(1)]);
    const service = yield* makeService({ store, judges: { check: () => Effect.never } });
    const pass = yield* Effect.forkChild(service.judgeDue());
    yield* letFibersRun;
    yield* stepSeconds(75);
    yield* letFibersRun;
    assert.deepStrictEqual(yield* Fiber.join(pass), { judged: 0, ended: 0 });
    yield* stepSeconds(55);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
    const [stored] = store.stored();
    assert.strictEqual(stored?.state, "waiting");
    assert.strictEqual(stored?.lastError, "The check gave no answer in 2m 5s.");
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 120 }) }))),
);

it.effect("after a restart mid-check, the wait is judged again at once", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([checkRecord(1)]);
    const before = yield* makeService({ store, judges: { check: () => Effect.never } });
    const pass = yield* Effect.forkChild(before.judgeDue());
    yield* letFibersRun;
    yield* stepSeconds(75);
    yield* Fiber.join(pass);
    assert.strictEqual(store.stored()[0]?.state, "waiting");

    // The new server knows nothing of the old one's check and runs its own.
    let starts = 0;
    const after = yield* makeService({
      store,
      judges: {
        check: () =>
          Effect.sync(() => (starts += 1)).pipe(
            Effect.as<Judgement>({ type: "met", outcome: "met", detail: "green" }),
          ),
      },
    });
    assert.deepStrictEqual(yield* after.judgeDue(), { judged: 1, ended: 1 });
    assert.strictEqual(starts, 1);
    assert.strictEqual(store.stored()[0]?.state, "delivering");
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect(
  "outage: at most 3 threads woken per pass, the longest failing first; one message per thread",
  () => {
    const logs = warnings();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      // Thread n has failed for the grace plus n minutes; thread 0 has a second wait failing for
      // half the grace, which joins its message.
      const threadOf = (n: number) => ThreadId.make(`thread-${n}`);
      store.seed([
        ...[0, 1, 2, 3, 4].map((n) =>
          checkRecord(n, {
            threadId: threadOf(n),
            errorSince: iso(NOW - GRACE - n * MINUTE),
            lastError: "connection refused",
          }),
        ),
        checkRecord(10, {
          threadId: threadOf(0),
          spec: { name: "deploy", args: [] },
          errorSince: iso(NOW - GRACE / 2),
          lastError: "connection refused",
        }),
      ]);
      const send = recordingDelivery();
      const failing: Judgement = {
        type: "failed",
        message: "connection refused",
        permanent: false,
      };
      const service = yield* makeService({
        store,
        delivery: send.delivery,
        judges: { check: () => Effect.succeed(failing) },
      });

      // Stops half-way: the write fails, nothing is kept, and the next pass does the same again.
      store.failRenames(true);
      yield* service.judgeDue().pipe(Effect.ignore);
      assert.isTrue(store.stored().every((wait) => wait.state === "waiting"));
      store.failRenames(false);
      yield* TestClock.adjust(30 * SECOND);

      yield* service.judgeDue();
      const states = () => store.stored().map((wait) => `${wait.threadId}:${wait.state}`);
      assert.deepStrictEqual(states(), [
        "thread-0:waiting",
        "thread-1:waiting",
        "thread-2:delivering",
        "thread-3:delivering",
        "thread-4:delivering",
        "thread-0:waiting",
      ]);
      assert.isTrue(logs.lines.some((line) => line.includes("2 more threads")));
      yield* service.deliverDue();
      assert.deepStrictEqual(
        send.sent.map((message) => message.threadId),
        [threadOf(2), threadOf(3), threadOf(4)],
      );

      // The next pass wakes the other two; thread 0's two failing waits are one message.
      yield* TestClock.adjust(30 * SECOND);
      yield* service.judgeDue();
      yield* service.deliverDue();
      assert.deepStrictEqual(
        send.sent.slice(3).map((message) => message.threadId),
        [threadOf(0), threadOf(1)],
      );
      const zero = send.sent[3]!.text;
      assert.include(zero, `[wait ${idOf(0)}]`);
      assert.include(zero, `[wait ${idOf(10)}]`);
      assert.isTrue(store.stored().every((wait) => wait.result?.outcome === "check_error"));
    }).pipe(
      Effect.provide(
        Layer.merge(
          logs.layer,
          withChecks({ build: check(), deploy: check({ argv: ["deploy-check"] }) }),
        ),
      ),
    );
  },
);

it.effect("maxErrorWakesPerSweep sets the cap", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed(
      [0, 1, 2, 3].map((n) =>
        checkRecord(n, {
          threadId: ThreadId.make(`thread-${n}`),
          errorSince: iso(NOW - GRACE),
          lastError: "x",
        }),
      ),
    );
    const service = yield* makeService({
      store,
      judges: {
        check: () => Effect.succeed<Judgement>({ type: "failed", message: "x", permanent: true }),
      },
    });
    yield* service.judgeDue();
    assert.strictEqual(store.stored().filter((wait) => wait.state === "delivering").length, 1);
  }).pipe(
    Effect.provide(
      ServerSettingsService.layerTest({
        waitFor: { enabled: true, maxErrorWakesPerSweep: 1 },
        waitChecks: { build: check() },
      }),
    ),
  ),
);

/** The real check judge with a fake process runner; the project root is `/work`. */
const realCheckJudge = (
  runner: ReturnType<typeof fakeRunner>,
  timeoutProgram: string | null = null,
) =>
  makeCheckJudge({
    placeOf: () => Effect.succeed<CheckPlace>({ type: "ok", projectId: PROJECT, cwd: "/work" }),
    timeoutProgram,
  }).pipe(Effect.provide(runner.layer));

it.effect(
  "a check removed from settings while a wait uses it ends in check_error and runs nothing; registering it again is refused",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      const runner = fakeRunner(() => ({ code: 1 as ProcessRunner.ProcessRunOutput["code"] }));
      const service = yield* makeService({
        store,
        judges: { check: yield* realCheckJudge(runner) },
      });
      const wait = yield* registerCheck(service);
      assert.strictEqual(wait.state, "waiting");
      assert.deepStrictEqual(runner.runs, [["build-check"]]);

      const settings = yield* ServerSettingsService;
      yield* settings.updateSettings({ waitChecks: {} });
      yield* TestClock.adjust(30 * SECOND);
      assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
      const [stored] = store.stored();
      assert.strictEqual(stored?.result?.outcome, "check_error");
      assert.strictEqual(
        stored?.result?.detail,
        `Check "build" is no longer in this project's settings.`,
      );
      assert.strictEqual(runner.runs.length, 1, "nothing ran after the removal");

      // Registering it again is refused before anything runs, and nothing is stored.
      const refused = yield* registerCheck(service).pipe(Effect.flip);
      assert.strictEqual(refused.code, "unknown_check");
      assert.strictEqual(store.stored().length, 1);
      assert.strictEqual(runner.runs.length, 1);
    }).pipe(Effect.provide(withChecks({ build: check() }))),
);

it.effect("a check the judge can never run is refused at registration, and nothing is stored", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const service = yield* makeService({
      store,
      judges: {
        check: () =>
          Effect.succeed<Judgement>({
            type: "failed",
            message: "The project of this wait is gone.",
            permanent: true,
          }),
      },
    });
    const refused = yield* registerCheck(service).pipe(Effect.flip);
    assert.strictEqual(refused.code, "invalid");
    assert.strictEqual(refused.message, "The project of this wait is gone.");
    assert.isFalse(store.files.has(filePath));
  }).pipe(Effect.provide(withChecks({ build: check() }))),
);

it.effect(
  "on a server without systemd a unit wait is refused, and a stored one ends in check_error, never met",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      const runner = fakeRunner(() => ({
        code: 1 as ProcessRunner.ProcessRunOutput["code"],
        stdout: "ActiveState=inactive\n",
        stderr: "System has not been booted with systemd as init system (PID 1).\n",
      }));
      const unit = yield* makeUnitJudge().pipe(Effect.provide(runner.layer));
      const service = yield* makeService({ store, judges: { unit } });
      const refused = yield* service
        .register({
          threadId: THREAD,
          projectId: PROJECT,
          input: { kind: "unit", unit: { name: "build.service" } },
        })
        .pipe(Effect.flip);
      assert.strictEqual(refused.code, "not_supported");
      assert.include(refused.message, "not available");
      assert.isFalse(store.files.has(filePath));

      // A unit wait stored on a server that had systemd, read after a restart on one that does not.
      store.seed([
        checkRecord(1, { kind: "unit", spec: { name: "build.service", scope: "user" } }),
      ]);
      const restarted = yield* makeService({ store, judges: { unit } });
      assert.deepStrictEqual(yield* restarted.judgeDue(), { judged: 1, ended: 1 });
      const [stored] = store.stored();
      assert.strictEqual(stored?.result?.outcome, "check_error");
      assert.include(stored?.result?.detail ?? "", "not available");
    }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
);

it.effect("with waits off, check and unit waits never run and the store is not touched", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([
      checkRecord(1),
      checkRecord(2, { kind: "unit", spec: { name: "build.service", scope: "user" } }),
    ]);
    const text = store.files.get(filePath);
    const { counter, judge } = counting();
    const service = yield* makeService({ store, judges: { check: judge, unit: judge } });
    store.touched.length = 0;
    yield* service.runDue();
    const refused = yield* registerCheck(service).pipe(Effect.flip);
    assert.strictEqual(refused.code, "waits_off");
    assert.strictEqual(counter.runs, 0);
    assert.deepStrictEqual(store.touched, []);
    assert.strictEqual(store.files.get(filePath), text);
  }).pipe(Effect.provide(ServerSettingsService.layerTest({ waitChecks: { build: check() } }))),
);

it.effect("a newer or corrupt store runs no check", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    for (const text of [
      JSON.stringify({ version: 2, waits: [checkRecord(1)] }),
      `{"version":1,"waits":[${JSON.stringify(checkRecord(1))}`,
    ]) {
      const store = memoryStore();
      store.files.set(filePath, text);
      const { counter, judge } = counting();
      const service = yield* makeService({ store, judges: { check: judge } });
      yield* service.runDue();
      assert.strictEqual(counter.runs, 0);
    }
  }).pipe(Effect.provide(withChecks({ build: check() }))),
);

// One owner of running checks: each wait has at most one running check, each thread at most one,
// and a run is stopped when its wait ends, its check goes, or waits go off.

/** A judge that runs until stopped, counting starts, running copies and stops. */
const tracked = (answer: Effect.Effect<Judgement> = Effect.never) => {
  const state = { starts: 0, running: 0, most: 0, stopped: 0 };
  const judge = () =>
    Effect.sync(() => {
      state.starts += 1;
      state.running += 1;
      state.most = Math.max(state.most, state.running);
    }).pipe(
      Effect.andThen(answer),
      Effect.onInterrupt(() => Effect.sync(() => void (state.stopped += 1))),
      Effect.ensuring(Effect.sync(() => void (state.running -= 1))),
    );
  return { state, judge };
};

const registerForked = (
  service: WaitService.WaitService,
  name: string,
  threadId: ThreadId = THREAD,
  args: ReadonlyArray<string> = [],
) =>
  Effect.gen(function* () {
    const fiber = yield* Effect.forkChild(
      service.register({
        threadId,
        projectId: PROJECT,
        input: { kind: "check", check: { name, args: [...args] } },
      }),
    );
    yield* letFibersRun;
    yield* stepSeconds(4);
    return yield* Fiber.join(fiber);
  });

it.effect("one wait never has two copies of its check running at once", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const { state, judge } = tracked(
      Effect.sleep(100 * SECOND).pipe(
        Effect.as<Judgement>({ type: "met", outcome: "met", detail: "green" }),
      ),
    );
    const service = yield* makeService({ store, judges: { check: judge } });
    const wait = yield* registerForked(service, "build");
    assert.strictEqual(wait.state, "waiting");
    // The run started at registration is the wait's one run: a pass starts no second copy.
    yield* stepSeconds(30);
    const pass = yield* Effect.forkChild(service.judgeDue());
    yield* stepSeconds(5);
    assert.strictEqual(state.most, 1, "two copies of the same check ran at once");
    yield* stepSeconds(70);
    yield* Fiber.join(pass);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 1 });
    assert.strictEqual(state.starts, 1);
    assert.strictEqual(store.stored()[0]?.result?.detail, "green");
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect("a cancelled wait's running check is stopped and frees its place", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const { state, judge } = tracked();
    const service = yield* makeService({ store, judges: { check: judge } });
    const wait = yield* registerForked(service, "build");
    assert.strictEqual(wait.state, "waiting");
    assert.strictEqual(state.running, 1);
    if (wait.state === "waiting")
      yield* service.cancel({ projectId: PROJECT, waitId: wait.waitId });
    yield* letFibersRun;
    assert.strictEqual(state.stopped, 1);
    assert.strictEqual(state.running, 0);
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect(
  "a wait that ends while its check runs (its thread was deleted) has the check stopped",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      store.seed([checkRecord(1)]);
      const { state, judge } = tracked();
      let gone = false;
      const delivery: WaitDelivery = {
        receiver: () =>
          Effect.sync(() =>
            gone ? ({ type: "gone", parentThreadId: null } as const) : ({ type: "ok" } as const),
          ),
        deliver: () => Effect.succeed({ type: "delivered" } as const),
      };
      const service = yield* makeService({ store, judges: { check: judge }, delivery });
      const pass = yield* Effect.forkChild(service.judgeDue());
      yield* stepSeconds(75);
      yield* Fiber.join(pass);
      assert.strictEqual(state.running, 1, "carried past the pass");
      gone = true;
      yield* service.receiverChanged(THREAD);
      yield* service.runDue();
      yield* letFibersRun;
      assert.notStrictEqual(store.stored()[0]?.state, "waiting");
      assert.strictEqual(state.running, 0);
      assert.strictEqual(state.stopped, 1);
    }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect("switching waits off stops every running check", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const { state, judge } = tracked();
    const service = yield* makeService({ store, judges: { check: judge } });
    yield* registerForked(service, "build");
    assert.strictEqual(state.running, 1);
    const settings = yield* ServerSettingsService;
    yield* settings.updateSettings({ waitFor: { enabled: false } });
    yield* service.runDue();
    yield* letFibersRun;
    assert.strictEqual(state.running, 0);
    assert.strictEqual(state.stopped, 1);
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect(
  "a check removed while its run is carried ends in check_error, not met, and is stopped",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      store.seed([checkRecord(1)]);
      const { state, judge } = tracked(
        Effect.sleep(100 * SECOND).pipe(
          Effect.as<Judgement>({ type: "met", outcome: "met", detail: "green" }),
        ),
      );
      const service = yield* makeService({ store, judges: { check: judge } });
      const pass = yield* Effect.forkChild(service.judgeDue());
      yield* stepSeconds(75);
      yield* Fiber.join(pass);
      const settings = yield* ServerSettingsService;
      yield* settings.updateSettings({ waitChecks: {} });
      // The next pass stops the run before it can answer met.
      yield* service.judgeDue();
      yield* letFibersRun;
      assert.strictEqual(state.stopped, 1);
      const [stored] = store.stored();
      assert.strictEqual(stored?.result?.outcome, "check_error");
      assert.strictEqual(
        stored?.result?.detail,
        `Check "build" is no longer in this project's settings.`,
      );
    }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect("a carried met answer from a check removed meanwhile ends in check_error, not met", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([checkRecord(1)]);
    const service = yield* makeService({
      store,
      judges: {
        check: () =>
          Effect.sleep(100 * SECOND).pipe(
            Effect.as<Judgement>({ type: "met", outcome: "met", detail: "green" }),
          ),
      },
    });
    const pass = yield* Effect.forkChild(service.judgeDue());
    yield* stepSeconds(75);
    yield* Fiber.join(pass);
    // The run answers met first; then the owner removes the check.
    yield* stepSeconds(30);
    const settings = yield* ServerSettingsService;
    yield* settings.updateSettings({ waitChecks: {} });
    yield* service.judgeDue();
    assert.strictEqual(store.stored()[0]?.result?.outcome, "check_error");
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 300 }) }))),
);

it.effect("a check narrowed so it no longer allows the wait's argument stops its run", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    // The pattern runs in a worker thread (real time); its answers are kept, so this warms them.
    yield* testArgPattern("^[a-z]+$", ["abc"]);
    yield* testArgPattern("^[0-9]+$", ["abc"]);
    const store = memoryStore();
    const { state, judge } = tracked();
    const service = yield* makeService({ store, judges: { check: judge } });
    const wait = yield* registerForked(service, "build", THREAD, ["abc"]);
    assert.strictEqual(wait.state, "waiting");
    const settings = yield* ServerSettingsService;
    yield* settings.updateSettings({
      waitChecks: { build: check({ maxArgs: 1, argPattern: "^[0-9]+$", timeoutSeconds: 300 }) },
    });
    yield* service.judgeDue();
    yield* letFibersRun;
    assert.strictEqual(state.stopped, 1);
    const [stored] = store.stored();
    assert.strictEqual(stored?.result?.outcome, "check_error");
    assert.include(stored?.result?.detail ?? "", "no longer allows this wait");
  }).pipe(Effect.provide(withChecks({ build: check({ maxArgs: 1, timeoutSeconds: 300 }) }))),
);

it.effect("a thread moved to another project: its check is timed by the check that runs", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([checkRecord(1)]);
    const MOVED = ProjectId.make("project-b");
    // The real judge, with the thread now in MOVED; its runner takes 40 s, within MOVED's 300 s.
    const runner = Layer.succeed(ProcessRunner.ProcessRunner, {
      run: () =>
        Effect.sleep(40 * SECOND).pipe(
          Effect.as({
            stdout: "green\n",
            stderr: "",
            code: 0 as ProcessRunner.ProcessRunOutput["code"],
            timedOut: false,
            stdoutTruncated: false,
            stderrTruncated: false,
            stdoutInvalidUtf8: false,
            stderrInvalidUtf8: false,
          }),
        ),
    });
    const placeOf = () =>
      Effect.succeed<CheckPlace>({ type: "ok", projectId: MOVED, cwd: "/work" });
    const judge = yield* makeCheckJudge({ placeOf, timeoutProgram: null }).pipe(
      Effect.provide(runner),
    );
    const service = yield* makeService({ store, judges: { check: judge }, checkPlaceOf: placeOf });
    const pass = yield* Effect.forkChild(service.judgeDue());
    yield* stepSeconds(45);
    const result = yield* Fiber.join(pass);
    const [stored] = store.stored();
    assert.isUndefined(
      stored?.lastError,
      `a healthy check counted as failed: ${stored?.lastError}`,
    );
    assert.deepStrictEqual(result, { judged: 1, ended: 1 });
  }).pipe(
    Effect.provide(
      ServerSettingsService.layerTest({
        waitFor: { enabled: true },
        waitChecks: { build: check({ timeoutSeconds: 20 }) },
        projectSettingsOverrides: {
          ["project-b"]: { waitChecks: { build: check({ timeoutSeconds: 300 }) } },
        },
      } as never),
    ),
  ),
);

it.effect("a thread moved to another project is judged at that project's interval", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    store.seed([checkRecord(1)]);
    const { counter, judge } = counting();
    const placeOf = () =>
      Effect.succeed<CheckPlace>({ type: "ok", projectId: ProjectId.make("project-b"), cwd: "/w" });
    const service = yield* makeService({ store, judges: { check: judge }, checkPlaceOf: placeOf });
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
    // The stored project's interval (30 s) has passed, the current project's (120 s) has not.
    yield* TestClock.adjust(60 * SECOND);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 0, ended: 0 });
    yield* TestClock.adjust(60 * SECOND);
    assert.deepStrictEqual(yield* service.judgeDue(), { judged: 1, ended: 0 });
    assert.strictEqual(counter.runs, 2);
  }).pipe(
    Effect.provide(
      ServerSettingsService.layerTest({
        waitFor: { enabled: true },
        waitChecks: { build: check({ intervalSeconds: 30 }) },
        projectSettingsOverrides: {
          ["project-b"]: { waitChecks: { build: check({ intervalSeconds: 120 }) } },
        },
      } as never),
    ),
  ),
);

// One thread could hold every check place with register + cancel on a slow
// check, so other threads' check waits were never judged.
it.effect("one thread's register/cancel loop on a slow check does not starve other threads", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    yield* testArgPattern("^[a-z]+$", ["a"]);
    yield* testArgPattern("^[a-z]+$", ["b"]);
    const store = memoryStore();
    const attacker = ThreadId.make("thread-attacker");
    const victim = ThreadId.make("thread-victim");
    let victimRuns = 0;
    const service = yield* makeService({
      store,
      judges: {
        check: (_target, context) =>
          context.threadId === attacker
            ? Effect.never
            : Effect.sync((): Judgement => {
                victimRuns += 1;
                return notYet;
              }),
      },
    });
    for (const arg of ["a", "b"]) {
      const wait = yield* registerForked(service, "slow", attacker, [arg]);
      assert.strictEqual(wait.state, "waiting");
      if (wait.state === "waiting") {
        yield* service.cancel({ projectId: PROJECT, waitId: wait.waitId });
      }
    }
    yield* registerForked(service, "build", victim);
    for (let round = 0; round < 4; round += 1) {
      const pass = yield* Effect.forkChild(service.judgeDue());
      yield* letFibersRun;
      yield* stepSeconds(75);
      yield* letFibersRun;
      yield* Fiber.join(pass);
    }
    assert.isAtLeast(victimRuns, 2, "the victim's check is judged at registration and after");
  }).pipe(
    Effect.provide(
      withChecks({
        build: check(),
        slow: check({ maxArgs: 1, timeoutSeconds: 600, intervalSeconds: 600 }),
      }),
    ),
  ),
);

it.effect(
  "two slow checks of one thread hold one place; another thread's check runs every interval",
  () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const store = memoryStore();
      const busy = ThreadId.make("thread-busy");
      const other = ThreadId.make("thread-other");
      let busyRunning = 0;
      let busyMost = 0;
      const otherRuns: number[] = [];
      const service = yield* makeService({
        store,
        judges: {
          check: (_target, context) =>
            context.threadId === busy
              ? Effect.sync(() => {
                  busyRunning += 1;
                  busyMost = Math.max(busyMost, busyRunning);
                }).pipe(
                  Effect.andThen(Effect.never),
                  Effect.ensuring(Effect.sync(() => void (busyRunning -= 1))),
                )
              : Effect.flatMap(Clock.currentTimeMillis, (now) =>
                  Effect.sync((): Judgement => {
                    otherRuns.push(now);
                    return notYet;
                  }),
                ),
        },
      });
      assert.strictEqual((yield* registerForked(service, "slow", busy)).state, "waiting");
      assert.strictEqual((yield* registerForked(service, "slower", busy)).state, "waiting");
      assert.strictEqual((yield* registerForked(service, "build", other)).state, "waiting");
      for (let round = 0; round < 4; round += 1) {
        const pass = yield* Effect.forkChild(service.judgeDue());
        yield* letFibersRun;
        yield* stepSeconds(30);
        yield* Fiber.join(pass);
      }
      assert.strictEqual(busyMost, 1, "one thread ran two checks at once");
      // Judged at registration, then at each pass once its 30 s interval has passed.
      assert.isAtLeast(otherRuns.length, 4);
      for (let index = 1; index < otherRuns.length; index += 1) {
        assert.isAtMost(otherRuns[index]! - otherRuns[index - 1]!, 75 * SECOND);
      }
    }).pipe(
      Effect.provide(
        withChecks({
          build: check(),
          slow: check({ timeoutSeconds: 600 }),
          slower: check({ timeoutSeconds: 600 }),
        }),
      ),
    ),
);

it.effect("on Linux a check runs under timeout, so it ends by itself if the server is gone", () =>
  Effect.gen(function* () {
    yield* TestClock.setTime(NOW);
    const store = memoryStore();
    const runner = fakeRunner(() => ({ code: 1 as ProcessRunner.ProcessRunOutput["code"] }));
    const service = yield* makeService({
      store,
      judges: { check: yield* realCheckJudge(runner, "/usr/bin/timeout") },
    });
    yield* registerCheck(service);
    assert.deepStrictEqual(runner.runs, [["/usr/bin/timeout", "-k", "2", "25", "build-check"]]);
  }).pipe(Effect.provide(withChecks({ build: check({ timeoutSeconds: 20 }) }))),
);
