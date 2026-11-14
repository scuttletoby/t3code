/**
 * Fork (waits): a check's argument pattern is the owner's and runs on an agent's input; a
 * pattern that backtracks without end must not stall the server.
 */
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
