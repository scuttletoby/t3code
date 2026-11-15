import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";

import * as DueHint from "./DueHint.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const HOUR = 3_600_000;

it.effect("a recorded hint keeps the poller idle until its work is due", () =>
  Effect.gen(function* () {
    const due = DueHint.make();
    yield* due.record(yield* due.version, NOW + 30_000, NOW);
    assert.isTrue(yield* due.isIdle(NOW + 5_000));
    assert.isFalse(yield* due.isIdle(NOW + 30_000));
  }),
);

it.effect("a hint recorded before the wall clock was set back does not keep the poller idle", () =>
  Effect.gen(function* () {
    const due = DueHint.make();
    // The next poll has work in 30 s.
    yield* due.record(yield* due.version, NOW + 30_000, NOW);
    // The wall clock is set back an hour; a scheduler tick 5 s of real time later.
    assert.isFalse(yield* due.isIdle(NOW - HOUR + 5_000));
  }),
);
