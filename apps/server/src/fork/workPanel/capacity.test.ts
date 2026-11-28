// Fork (work panel): this machine's capacity from the placement facts. Each test names the
// question it answers.
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as TestClock from "effect/testing/TestClock";

import { REASON_GATE_UNREAD, REASON_SLOTS_OFF } from "../environmentFacts.ts";
import { capacityFromFacts, REASON_NO_HOST_READ, REASON_OVER_LIMIT } from "./capacity.ts";
import { makeRepoQueueCache } from "./repoQueue.ts";
import { makeRead } from "./workPanel.ts";

const MB = 1024 * 1024;
const host = {
  sampledAt: 1,
  cpuUtilization: null,
  cpuCount: 8,
  availableMemoryBytes: 6_000 * MB,
  totalMemoryBytes: 16_000 * MB,
};

describe("capacityFromFacts", () => {
  it("gives the gate's agents in use of those allowed and the memory in use", () => {
    const capacity = capacityFromFacts({
      hostResources: host,
      placement: {
        labels: [],
        available: true,
        runningAgents: 3,
        slotsFree: 5,
        memoryFloorMb: 2_048,
        freeMemoryMb: 6_000,
        factsAt: 9,
      },
    });
    assert.deepStrictEqual(capacity.agents.state, "answered");
    assert.strictEqual(capacity.agents.used, 3);
    assert.strictEqual(capacity.agents.total, 8);
    assert.deepStrictEqual([capacity.memoryMb.used, capacity.memoryMb.total], [10_000, 16_000]);
    assert.strictEqual(capacity.memoryFloorMb, 2_048);
    assert.strictEqual(capacity.factsAt, 9);
  });

  it("a gate with no machine limit gives no total, not a guessed one", () => {
    const capacity = capacityFromFacts({
      placement: { labels: [], available: true, runningAgents: 2, slotsFree: null, factsAt: 1 },
    });
    assert.strictEqual(capacity.agents.total, null);
  });

  it("slots off or an unread gate: agents unknown with the gate's reason, never 0", () => {
    for (const reason of [REASON_SLOTS_OFF, REASON_GATE_UNREAD]) {
      const capacity = capacityFromFacts({
        placement: { labels: [], available: reason === REASON_SLOTS_OFF, reason, factsAt: 1 },
      });
      assert.strictEqual(capacity.agents.state, "unknown");
      assert.strictEqual(capacity.agents.reason, reason);
      assert.strictEqual(capacity.agents.used, undefined);
    }
  });

  it("more agents than a lowered limit: the gate's limit, not a clamped total", () => {
    // Lowered to 4 while 6 run: slotsFree is clamped at 0, so used + slotsFree would read 6 of 6.
    const capacity = capacityFromFacts(
      {
        placement: {
          labels: [],
          available: false,
          reason: "Every machine slot is in use.",
          runningAgents: 6,
          slotsFree: 0,
          factsAt: 1,
        },
      },
      4,
    );
    assert.deepStrictEqual([capacity.agents.used, capacity.agents.total], [6, 4]);
    assert.strictEqual(capacity.agents.reason, REASON_OVER_LIMIT);
  });

  it("no time from any source: no made-up facts time", () => {
    assert.isFalse("factsAt" in capacityFromFacts({}));
  });

  it("no host read: memory unknown", () => {
    const capacity = capacityFromFacts({
      placement: { labels: [], available: true, runningAgents: 0, slotsFree: 1, factsAt: 1 },
    });
    assert.strictEqual(capacity.memoryMb.state, "unknown");
    assert.strictEqual(capacity.memoryMb.reason, REASON_NO_HOST_READ);
  });
});

describe("the work panel's two reads", () => {
  it.effect("with a thread: CI only, so a slow capacity read never holds it back", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(5);
      const cache = yield* makeRepoQueueCache(() =>
        Effect.succeed({ _tag: "Unreachable", reason: "unused" } as const),
      );
      const read = makeRead(() => Effect.succeed([]), cache, Effect.never);
      const answer = yield* read({ threadId: "t1" as never });
      assert.deepStrictEqual(answer, { answeredAt: 5, repoQueues: [], pullRequests: [] });
    }),
  );

  it.effect("without a thread: only the capacity, without reading threads or GitHub", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(5);
      let calls = 0;
      let familyReads = 0;
      const cache = yield* makeRepoQueueCache(() =>
        Effect.sync(() => {
          calls += 1;
          return { _tag: "Unreachable", reason: "unused" } as const;
        }),
      );
      const capacity = capacityFromFacts({ hostResources: host });
      const read = makeRead(
        () => Effect.sync(() => ((familyReads += 1), [])),
        cache,
        Effect.succeed(capacity),
      );
      const answer = yield* read({});
      assert.deepStrictEqual(answer, { answeredAt: 5, repoQueues: [], pullRequests: [], capacity });
      assert.deepStrictEqual([calls, familyReads], [0, 0]);
    }),
  );
});
