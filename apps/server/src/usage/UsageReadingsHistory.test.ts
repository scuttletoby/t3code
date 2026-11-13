import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  defaultInstanceIdForDriver,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerProviderUsageLimits,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import { TestClock } from "effect/testing";

import {
  DEFAULT_RETENTION_MS,
  make,
  shouldRecord,
  type UsageReading,
} from "./UsageReadingsHistory.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const INSTANCE = defaultInstanceIdForDriver(CLAUDE);
const SECOND = ProviderInstanceId.make("claude_second");
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));
const MINUTE = 60 * 1000;

const limits = (
  checkedAt: string,
  weeklyPercent: number,
  overrides?: Partial<ServerProviderUsageLimits>,
): ServerProviderUsageLimits => ({
  checkedAt,
  windows: [
    {
      id: "seven_day",
      kind: "weekly",
      label: "Weekly",
      usedPercent: weeklyPercent,
      resetsAt: "2026-11-20T13:00:00.000Z",
    },
  ],
  ...overrides,
});

const provider = (
  usageLimits: ServerProviderUsageLimits | undefined,
  instanceId: ProviderInstanceId = INSTANCE,
): ServerProvider => ({
  instanceId,
  driver: CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: { status: "authenticated" },
  checkedAt: usageLimits?.checkedAt ?? at(0),
  models: [],
  slashCommands: [],
  skills: [],
  ...(usageLimits === undefined ? {} : { usageLimits }),
});

const reading = (checkedAt: string, weeklyPercent: number): UsageReading => ({
  checkedAt,
  windows: limits(checkedAt, weeklyPercent).windows,
});

const percents = (readings: ReadonlyArray<UsageReading>) =>
  readings.map((entry) => entry.windows[0]?.usedPercent);

const historyDirectory = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "usage-readings-" });
  return path.join(root, "usage-readings");
});

it.layer(NodeServices.layer)("UsageReadingsHistory", (it) => {
  it("records a reading only when the meter moved, an account changed or the heartbeat is due", () => {
    const first = reading(at(0), 40);
    assert.isTrue(shouldRecord(undefined, first));
    assert.isFalse(shouldRecord(first, reading(at(MINUTE), 40)));
    assert.isFalse(shouldRecord(first, reading(at(MINUTE), 40.04)));
    assert.isTrue(shouldRecord(first, reading(at(MINUTE), 41)));
    assert.isTrue(shouldRecord(first, reading(at(30 * MINUTE), 40)));
    assert.isTrue(
      shouldRecord(first, { ...reading(at(MINUTE), 40), credentialFingerprint: "other" }),
    );
    assert.isTrue(shouldRecord(first, { ...reading(at(MINUTE), 40), windows: [] }));
    // The same probe republished, or an older one arriving late, is not history.
    assert.isFalse(shouldRecord(first, reading(at(0), 55)));
    assert.isFalse(shouldRecord(first, reading(at(-MINUTE), 55)));
  });

  it.effect("appends changed readings per instance and skips providers without limits", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const history = yield* make({ directory });

      yield* history.record([provider(limits(at(0), 40)), provider(undefined, SECOND)]);
      yield* history.record([provider(limits(at(0), 40))]);
      yield* history.record([provider(limits(at(MINUTE), 40))]);
      yield* history.record([provider(limits(at(2 * MINUTE), 42))]);

      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 42]);
      assert.deepStrictEqual(yield* history.readings(SECOND), []);
      assert.deepStrictEqual(
        percents(yield* history.readings(INSTANCE, { sinceMs: NOW_MS + MINUTE })),
        [42],
      );
      const fs = yield* FileSystem.FileSystem;
      assert.deepStrictEqual(yield* fs.readDirectory(directory), [`${INSTANCE}.jsonl`]);
    }),
  );

  it.effect("a restart continues the history without repeating its last reading", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const before = yield* make({ directory });
      yield* before.record([provider(limits(at(0), 40))]);

      // No compaction first: the history must still find its last reading on disk.
      const after = yield* make({ directory });
      yield* after.record([provider(limits(at(0), 40))]);
      yield* after.record([provider(limits(at(MINUTE), 40))]);
      yield* after.record([provider(limits(at(2 * MINUTE), 43))]);

      assert.deepStrictEqual(percents(yield* after.readings(INSTANCE)), [40, 43]);
    }),
  );

  it.effect("drops a line torn by a crash and keeps appending after it", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* historyDirectory;
      yield* fs.makeDirectory(directory, { recursive: true });
      const file = path.join(directory, `${INSTANCE}.jsonl`);
      yield* fs.writeFileString(
        file,
        `${JSON.stringify(reading(at(0), 40))}\n{"checkedAt":"2026-11-13T12:0`,
      );

      const history = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40]);
      yield* history.compact;
      yield* history.record([provider(limits(at(MINUTE), 45))]);

      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 45]);
      const lines = (yield* fs.readFileString(file)).split("\n").filter(Boolean);
      assert.strictEqual(lines.length, 2);
    }),
  );

  it.effect("retention drops old readings and removes a history with none left", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* historyDirectory;
      yield* fs.makeDirectory(directory, { recursive: true });
      const old = at(-DEFAULT_RETENTION_MS - MINUTE);
      yield* fs.writeFileString(
        path.join(directory, `${INSTANCE}.jsonl`),
        [reading(old, 10), reading(at(-MINUTE), 60)]
          .map((entry) => JSON.stringify(entry))
          .join("\n") + "\n",
      );
      yield* fs.writeFileString(
        path.join(directory, `${SECOND}.jsonl`),
        `${JSON.stringify(reading(old, 99))}\n`,
      );
      yield* fs.writeFileString(path.join(directory, "notes.txt"), "not a history\n");
      yield* fs.writeFileString(path.join(directory, "Not An Id.jsonl"), "{}\n");

      const history = yield* make({ directory });
      yield* history.compact;

      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [60]);
      assert.deepStrictEqual(yield* history.readings(SECOND), []);
      assert.deepStrictEqual((yield* fs.readDirectory(directory)).toSorted(), [
        "Not An Id.jsonl",
        `${INSTANCE}.jsonl`,
        "notes.txt",
      ]);
    }),
  );

  it.effect("starts from a missing directory", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const history = yield* make({ directory });
      assert.deepStrictEqual(yield* history.readings(INSTANCE), []);
      yield* history.compact;
      yield* history.record([provider(limits(at(0), 5))]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [5]);
    }),
  );
});
