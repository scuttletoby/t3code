import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ProjectId, ThreadId } from "@t3tools/contracts";
import type { WaitForInput } from "@t3tools/contracts/waitFor";
import { type Judgement, clipStored, storedBytes, validateWaitSpec } from "@t3tools/shared/waitFor";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Scheduler from "effect/Scheduler";
import { TestClock } from "effect/testing";

import { ServerSettingsService } from "../serverSettings.ts";
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
const fileWait = (path: string): WaitForInput => ({ kind: "file", file: { path } });
const letFibersRun = Effect.gen(function* () {
  for (let turn = 0; turn < 20; turn += 1) yield* Effect.yieldNow;
});
const stepSeconds = (seconds: number) =>
  Effect.gen(function* () {
    for (let second = 0; second < seconds; second += 1) {
      yield* TestClock.adjust(1_000);
      for (let turn = 0; turn < 5; turn += 1) yield* Effect.yieldNow;
    }
  });

it.layer(NodeServices.layer)("lens security round 3 on #90 (carried to #91)", (it) => {
  it.effect(
    "S1: a registration interrupted between taking a place and awaiting its check leaves a place that never turns late; the next sweep then hangs for good",
    () =>
      Effect.gen(function* () {
        const frozenAt: number[] = [];
        for (let k = 0; k < 80; k += 1) {
          yield* Effect.scoped(
            Effect.gen(function* () {
              yield* TestClock.setTime(NOW);
              // A dead mount: every check hangs, as a stat on it would.
              const judges: WaitJudges = { file: () => Effect.never as Effect.Effect<Judgement> };
              // Adapted to this PR's API (`delivery`); no file check process, so stuck checks
              // stay stuck and the test is about the place the cut-off register held.
              const service = yield* WaitService.make({
                filePath,
                delivery: {
                  receiver: () => Effect.succeed({ type: "ok" }),
                  deliver: () => Effect.succeed({ type: "delivered" }),
                },
                fileChildJudge: null,
                judges,
              });
              // The store is already loaded (any earlier call does that), so register does no I/O first.
              yield* service.list({ threadId: THREAD, projectId: PROJECT });
              // The caller (an MCP tool call in PR 3/4) is cancelled while register runs. Yielding on
              // every op stands in for an auto-yield at that point.
              const reg = yield* Effect.forkChild(
                service
                  .register({
                    threadId: THREAD,
                    projectId: PROJECT,
                    input: fileWait(watched("dead/a")),
                  })
                  .pipe(Effect.provideService(Scheduler.MaxOpsBeforeYield, 1)),
              );
              for (let i = 0; i < k; i += 1) yield* Effect.yieldNow;
              yield* Fiber.interrupt(reg);
              yield* letFibersRun;
              // An ordinary second wait on the same dead mount: its registration check goes late at 3 s.
              const second = yield* Effect.forkChild(
                service.register({
                  threadId: THREAD,
                  projectId: PROJECT,
                  input: fileWait(watched("dead/b")),
                }),
              );
              yield* letFibersRun;
              yield* stepSeconds(4);
              yield* Fiber.join(second);
              // A sweep must end: each check has a 10 s limit, and stuck ones pause the rest.
              yield* TestClock.adjust(5_000);
              const sweep = yield* Effect.forkChild(service.judgeDue());
              yield* letFibersRun;
              yield* stepSeconds(15);
              yield* letFibersRun;
              yield* stepSeconds(15);
              yield* letFibersRun;
              if (sweep.pollUnsafe() === undefined) frozenAt.push(k);
              yield* Fiber.interrupt(sweep);
            }).pipe(Effect.provideService(FileSystem.FileSystem, memoryFileSystem().fs)),
          );
        }
        assert.deepStrictEqual(
          frozenAt,
          [],
          `the sweep never ended when register was cut off after ${frozenAt.join(", ")} yields`,
        );
      }).pipe(Effect.provide(ServerSettingsService.layerTest(ON))),
  );

  it("S2 (note, Windows): a device name with a trailing colon passes the path check", () => {
    const accepted: string[] = [];
    for (const path of ["C:\\x\\COM1:", "C:\\x\\nul:", "C:\\x\\CON:", "C:\\x\\LPT1:.txt"]) {
      const result = validateWaitSpec({
        input: { kind: "file", file: { path } },
        checks: {},
        platform: "win32",
        accounts: new Set(),
      });
      if (result.ok) accepted.push(path);
    }
    assert.deepStrictEqual(accepted, []);
  });

  it("S3 (passes): ordinary Windows names still pass; edge device spellings refused", () => {
    const ok = (path: string) =>
      validateWaitSpec({
        input: { kind: "file", file: { path } },
        checks: {},
        platform: "win32",
        accounts: new Set(),
      }).ok;
    for (const path of [
      "C:\\x\\console.log",
      "C:\\x\\nullable.ts",
      "C:\\x\\com10",
      "C:\\x\\COM1x",
      "C:\\x\\auxiliary",
      "C:\\x\\lpt",
      "C:\\x\\.con",
    ]) {
      assert.isTrue(ok(path), path);
    }
    for (const path of [
      "C:\\x\\NUL ",
      "C:\\x\\COM1.",
      "C:\\x\\con.txt",
      "C:\\x\\Con .tar.gz",
      "C:\\x\\com¹",
      "C:/x/aux",
      "\\\\?\\C:\\x",
      "\\\\.\\COM1",
      "C:\\x\\CONIN$",
    ]) {
      assert.isFalse(ok(path), path);
    }
  });

  it("S4 (passes): clipStored never goes over its byte limit and never splits a character", () => {
    const samples = [
      "😀".repeat(400),
      "\u0001".repeat(400),
      "a".repeat(597) + "😀😀",
      "\ud800".repeat(300),
      "中".repeat(300),
      "x\u0001中😀\ud800".repeat(100),
    ];
    for (const sample of samples) {
      for (let max = 10; max <= 600; max += 1) {
        const clipped = clipStored(sample, max);
        assert.isAtMost(
          storedBytes(clipped),
          max,
          `${JSON.stringify(sample.slice(0, 8))} @ ${max}`,
        );
        assert.isTrue(sample.startsWith(clipped.slice(0, -1)) || clipped === sample);
        // No surrogate half was made by the cut.
        const body = clipped.endsWith("…") ? clipped.slice(0, -1) : clipped;
        const last = body.charCodeAt(body.length - 1);
        if (last >= 0xd800 && last <= 0xdbff && !sample.includes("\ud800")) {
          assert.fail(`split a pair at ${max}`);
        }
      }
    }
  });
});
