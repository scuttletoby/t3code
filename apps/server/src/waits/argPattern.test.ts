// @effect-diagnostics nodeBuiltinImport:off - a worker that fails, as a test double.
/**
 * Fork (waits): a check's argument pattern is the owner's and runs on an agent's input; a
 * pattern that backtracks without end must not stall the server.
 */
import * as NodeWorkerThreads from "node:worker_threads";

import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";

import { testArgPattern } from "./argPattern.ts";

const EVIL = "(a+)+$";
const INPUT = `${"a".repeat(40)}!`;

it.live(
  "a catastrophic pattern answers 'slow' at its time limit and the server stays responsive",
  () =>
    Effect.gen(function* () {
      const slow = yield* Effect.forkChild(testArgPattern(EVIL, [INPUT]));
      // While the bad pattern runs, the server still answers another pattern test.
      const quick = yield* testArgPattern("[a-z]+", ["responsive"]);
      assert.deepStrictEqual(quick, { type: "ok" });
      assert.isUndefined(slow.pollUnsafe(), "the bad pattern answered before the quick one");
      assert.deepStrictEqual(yield* Fiber.join(slow), { type: "slow" });
    }),
);

it.live("answers ok, or the first argument that does not match; anchors are whole-argument", () =>
  Effect.gen(function* () {
    assert.deepStrictEqual(yield* testArgPattern("[a-z]+", ["abc", "def"]), { type: "ok" });
    assert.deepStrictEqual(yield* testArgPattern("[a-z]+", ["abc", "de1", "x"]), {
      type: "bad",
      arg: "de1",
    });
    assert.deepStrictEqual(yield* testArgPattern("[a-z]+", []), { type: "ok" });
  }),
);

it.live(
  "a worker that cannot start (low memory) is a passing error, not a refusal, and is not kept",
  () =>
    Effect.gen(function* () {
      const cannotStart = () => {
        throw Object.assign(new Error("no memory"), { code: "ERR_WORKER_INIT_FAILED" });
      };
      const failed = yield* testArgPattern("[a-y]+", ["start"], 2_000, cannotStart);
      assert.deepStrictEqual(failed, {
        type: "failed",
        message: "The pattern test could not start (ERR_WORKER_INIT_FAILED).",
      });
      // The next try, with a worker that starts, gives the real answer.
      assert.deepStrictEqual(yield* testArgPattern("[a-y]+", ["start"]), { type: "ok" });
    }),
);

it.live(
  "a worker that fails while it runs is a passing error, not a refusal, and is not kept",
  () =>
    Effect.gen(function* () {
      const failing = (_script: string, options: NodeWorkerThreads.WorkerOptions) =>
        new NodeWorkerThreads.Worker(
          `throw Object.assign(new Error("out of memory"), { code: "ERR_WORKER_OUT_OF_MEMORY" });`,
          { ...options, eval: true },
        );
      const failed = yield* testArgPattern("[a-x]+", ["run"], 2_000, failing);
      assert.strictEqual(failed.type, "failed");
      assert.deepStrictEqual(yield* testArgPattern("[a-x]+", ["run"]), { type: "ok" });
    }),
);
