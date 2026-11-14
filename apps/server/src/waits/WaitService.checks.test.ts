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
import { type CheckPlace, makeCheckJudge, makeUnitJudge, type WaitJudges } from "./WaitJudges.ts";
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
}) =>
  WaitService.make({
    filePath,
    delivery: input.delivery ?? recordingDelivery().delivery,
    fileChildJudge: null,
    ...(input.judges === undefined ? {} : { judges: input.judges }),
    ...(input.onChange === undefined ? {} : { onChange: input.onChange }),
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
const realCheckJudge = (runner: ReturnType<typeof fakeRunner>) =>
  makeCheckJudge({
    placeOf: () => Effect.succeed<CheckPlace>({ type: "ok", projectId: PROJECT, cwd: "/work" }),
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
