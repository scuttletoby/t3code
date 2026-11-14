import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitRecord } from "@t3tools/contracts/waitFor";
import { type Judgement, validateWaitSpec } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import { TestClock } from "effect/testing";

import { ServerSettingsService } from "../serverSettings.ts";
import type { WaitDelivery } from "./WaitDelivery.ts";
import type { WaitJudges } from "./WaitJudges.ts";
import * as WaitService from "./WaitService.ts";
import { memoryFileSystem } from "./WaitService.testkit.ts";
import { WAIT_STORE_FILE } from "./WaitStore.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const THREAD = ThreadId.make("thread-a");
const PROJECT = ProjectId.make("project-a");

const ON = { waitFor: { enabled: true } } as const;
// Adapted: the store lives in memory, so no real write is still running when the clock moves.
const filePath = `/waits/state/${WAIT_STORE_FILE}`;
const watched = (name: string) => `/waits/${name}`;

/** A record as an earlier run wrote it. */
const record = (overrides: Record<string, unknown> = {}) => ({
  id: "w_aaaaaaaaaa",
  threadId: THREAD,
  projectId: PROJECT,
  key: "0123456789abcdef", // gitleaks:allow (a fake test value, not a key)
  createdAt: "2026-11-14T11:00:00.000Z",
  deadlineAt: "2026-11-14T13:00:00.000Z",
  state: "waiting",
  attempt: 0,
  kind: "file",
  spec: { path: "/nowhere/ready", until: "exists" },
  ...overrides,
});

const storedWaits = (filePath: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return JSON.parse(yield* fs.readFileString(filePath)).waits as ReadonlyArray<WaitRecord>;
  });

/** Every thread is there and takes every message. */
const delivery: WaitDelivery = {
  receiver: () => Effect.succeed({ type: "ok" }),
  deliver: () => Effect.succeed({ type: "delivered" }),
};

// Adapted to this PR's API: `delivery` instead of #90's `send`; no file check process, so a
// stuck check stays stuck the same way on every machine.
const makeService = (input: {
  readonly filePath: string;
  readonly judges?: WaitJudges;
  readonly fs?: FileSystem.FileSystem;
}) => {
  const made = WaitService.make({
    filePath: input.filePath,
    delivery,
    fileChildJudge: null,
    ...(input.judges === undefined ? {} : { judges: input.judges }),
  });
  return input.fs === undefined
    ? made
    : made.pipe(Effect.provideService(FileSystem.FileSystem, input.fs));
};

/** Moves the test clock a second at a time and lets other fibers run, as the real loop would. */
const stepSeconds = (seconds: number) =>
  Effect.gen(function* () {
    for (let second = 0; second < seconds; second += 1) {
      yield* TestClock.adjust(1_000);
      for (let turn = 0; turn < 5; turn += 1) yield* Effect.yieldNow;
    }
  });

const ALPHA = "abcdefghijklmnopqrstuvwxyz234567";
const idOf = (n: number) =>
  `w_${Array.from({ length: 10 }, (_, i) => ALPHA[Math.floor(n / 32 ** (9 - i)) % 32]).join("")}`;
const keyOf = (n: number) => n.toString(16).padStart(16, "0");

it.layer(NodeServices.layer)("lens correctness round 3 on #90 (carried to #91)", (it) => {
  it.effect(
    "C1: one pass's single save holds a met file wait until slow (healthy) checks of another kind finish",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const fs = yield* FileSystem.FileSystem;
        const ready = watched("ready");
        yield* fs.writeFileString(ready, "");
        // The met file wait comes first in the store; then 8 unit waits whose checks take 30 s
        // each (healthy: under the 60 s limit, e.g. a slow systemctl or a slow GitHub for pr).
        const waits: unknown[] = [
          record({ id: idOf(1), key: keyOf(1), spec: { path: ready, until: "exists" } }),
        ];
        for (let n = 0; n < 8; n += 1) {
          waits.push(
            record({
              id: idOf(100 + n),
              key: keyOf(100 + n),
              kind: "unit",
              spec: { name: `u${n}.service`, scope: "user" },
            }),
          );
        }
        yield* fs.writeFileString(filePath, `${JSON.stringify({ version: 1, waits }, null, 2)}\n`);
        const judges: WaitJudges = {
          file: (target) =>
            fs.exists(target.spec.path).pipe(
              Effect.orDie,
              Effect.map((exists): Judgement =>
                exists
                  ? { type: "met", outcome: "exists", detail: "It exists." }
                  : { type: "notYet" },
              ),
            ),
          unit: () => Effect.sleep(30_000).pipe(Effect.as<Judgement>({ type: "notYet" })),
        };
        const service = yield* makeService({ filePath, judges });
        const sweep = yield* Effect.forkChild(service.judgeDue());
        let savedAfter = -1;
        for (let second = 1; second <= 200 && savedAfter < 0; second += 1) {
          yield* stepSeconds(1);
          const stored = (yield* storedWaits(filePath)).find((wait) => wait.id === idOf(1));
          if (stored?.state === "delivering") savedAfter = second;
        }
        // Adapted: saved early, the pass itself still runs its slow checks, so time moves on
        // until it ends (and it must end).
        for (let step = 0; step < 60 && sweep.pollUnsafe() === undefined; step += 1) {
          yield* stepSeconds(5);
        }
        if (sweep.pollUnsafe() === undefined) yield* Fiber.interrupt(sweep);
        else yield* Fiber.join(sweep);
        // Before the batching (per-answer save) it was saved within a second or two.
        assert.isAtLeast(savedAfter, 0, "never saved");
        assert.isAtMost(savedAfter, 5, `the met file wait was saved only after ${savedAfter} s`);
      }).pipe(
        Effect.provideService(FileSystem.FileSystem, memoryFileSystem().fs),
        Effect.provide(ServerSettingsService.layerTest(ON)),
      ),
  );

  it("W1: Windows device names: refused forms and ordinary names", () => {
    const check = (path: string) =>
      validateWaitSpec({
        input: { kind: "file", file: { path } },
        checks: {},
        platform: "win32",
        accounts: new Set(),
      }).ok;
    const refused = [
      "C:\\x\\COM1",
      "C:\\NUL",
      "C:\\x\\con.txt",
      "C:\\x\\NUL ",
      "C:\\x\\COM1.",
      "C:\\x\\COM1 .txt",
      "C:/x/aux",
      "C:\\x\\LPT¹",
      "C:\\x\\conin$",
      "\\\\?\\C:\\x",
      "\\\\.\\NUL",
      "\\\\.\\COM1",
    ];
    const passed = [
      "C:\\x\\console.log",
      "C:\\x\\nullable.ts",
      "C:\\x\\com10",
      "C:\\x\\lpt10.txt",
      "C:\\x\\connection",
      "C:\\x\\auxiliary\\y",
      "C:\\x\\ nul",
    ];
    assert.deepStrictEqual(refused.filter(check), []);
    assert.deepStrictEqual(
      passed.filter((p) => !check(p)),
      [],
    );
  });

  it("W2 (probe): NUL: and con:stream", () => {
    const check = (path: string) =>
      validateWaitSpec({
        input: { kind: "file", file: { path } },
        checks: {},
        platform: "win32",
        accounts: new Set(),
      }).ok;
    // As written in the lens (`map(...)` against `[]`) it could never pass; the intent is that
    // none of them is accepted.
    assert.deepStrictEqual(["C:\\x\\NUL:", "C:\\x\\con:x", "C:\\x\\NUL:.txt"].filter(check), []);
  });
});
