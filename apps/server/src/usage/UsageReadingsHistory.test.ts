import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import {
  defaultInstanceIdForDriver,
  ProviderDriverKind,
  ProviderInstanceId,
  type ServerProvider,
  type ServerProviderAuth,
  type ServerProviderUsageLimits,
  type ServerProviderUsageWindow,
} from "@t3tools/contracts";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import * as Queue from "effect/Queue";
import * as Stream from "effect/Stream";
import { TestClock } from "effect/testing";

import { ServerConfig } from "../config.ts";
import { ProviderRegistry } from "../provider/ProviderRegistry.ts";
import * as UsageReadingsHistory from "./UsageReadingsHistory.ts";
import {
  accountOf,
  DEFAULT_RETENTION_MS,
  make,
  shouldRecord,
  type UsageReading,
} from "./UsageReadingsHistory.ts";

const CLAUDE = ProviderDriverKind.make("claudeAgent");
const CODEX = ProviderDriverKind.make("codex");
const ANTIGRAVITY = ProviderDriverKind.make("antigravity");
const INSTANCE = defaultInstanceIdForDriver(CLAUDE);
const SECOND = ProviderInstanceId.make("claude_second");
const NOW_MS = Date.parse("2026-11-13T12:00:00.000Z");

const at = (offsetMs: number) => DateTime.formatIso(DateTime.makeUnsafe(NOW_MS + offsetMs));
const MINUTE = 60 * 1000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;
const RESET = "2026-11-20T13:00:00.000Z";

const weekly = (usedPercent: number, extra?: Partial<ServerProviderUsageWindow>) => ({
  id: "seven_day",
  kind: "weekly" as const,
  label: "Weekly",
  usedPercent,
  resetsAt: RESET,
  ...extra,
});

const limits = (
  checkedAt: string,
  weeklyPercent: number,
  overrides?: Partial<ServerProviderUsageLimits>,
): ServerProviderUsageLimits => ({
  checkedAt,
  windows: [weekly(weeklyPercent)],
  ...overrides,
});

const provider = (
  usageLimits: ServerProviderUsageLimits | undefined,
  instanceId: ProviderInstanceId = INSTANCE,
  extra?: { readonly auth?: Partial<ServerProviderAuth>; readonly driver?: ProviderDriverKind },
): ServerProvider => ({
  instanceId,
  driver: extra?.driver ?? CLAUDE,
  enabled: true,
  installed: true,
  version: "1.0.0",
  status: "ready",
  auth: { status: "authenticated", ...extra?.auth },
  checkedAt: usageLimits?.checkedAt ?? at(0),
  models: [],
  slashCommands: [],
  skills: [],
  ...(usageLimits === undefined ? {} : { usageLimits }),
});

const reading = (
  checkedAt: string,
  weeklyPercent: number,
  extra?: Partial<UsageReading>,
): UsageReading => ({
  checkedAt,
  windows: limits(checkedAt, weeklyPercent).windows,
  ...extra,
});

const percents = (readings: ReadonlyArray<UsageReading>) =>
  readings.map((entry) => entry.windows[0]?.usedPercent);

const historyDirectory = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "usage-readings-" });
  return path.join(root, "usage-readings");
});

/** Writes a history file the way an earlier run (or another T3 version) left it. */
const seedHistory = (directory: string, instanceId: ProviderInstanceId, lines: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    yield* fs.makeDirectory(directory, { recursive: true });
    const file = path.join(directory, `${instanceId}.jsonl`);
    yield* fs.writeFileString(file, lines);
    return file;
  });

const jsonLines = (readings: ReadonlyArray<UsageReading>) =>
  readings.map((entry) => `${JSON.stringify(entry)}\n`).join("");

const fileLines = (file: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return (yield* fs.readFileString(file)).split("\n").filter(Boolean);
  });

/** The real file system, except that reads of `unreadable` fail as if access were denied. */
const failingReadsOf = (unreadable: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const faulty: FileSystem.FileSystem = {
      ...fs,
      readFileString: (filePath, encoding) =>
        filePath === unreadable
          ? Effect.fail(
              PlatformError.systemError({
                _tag: "PermissionDenied",
                module: "FileSystem",
                method: "readFileString",
                pathOrDescriptor: filePath,
              }),
            )
          : fs.readFileString(filePath, encoding),
    };
    return faulty;
  });

it.layer(NodeServices.layer)("UsageReadingsHistory", (it) => {
  it("records a reading only when the meter moved, an account changed or the heartbeat is due", () => {
    const first = reading(at(0), 40);
    assert.isTrue(shouldRecord(undefined, first));
    assert.isFalse(shouldRecord(first, reading(at(MINUTE), 40)));
    assert.isFalse(shouldRecord(first, reading(at(MINUTE), 40.04)));
    assert.isTrue(shouldRecord(first, reading(at(MINUTE), 41)));
    assert.isTrue(shouldRecord(first, reading(at(30 * MINUTE), 40)));
    assert.isTrue(shouldRecord(first, reading(at(MINUTE), 40, { account: "other" })));
    assert.isTrue(shouldRecord(first, { ...reading(at(MINUTE), 40), windows: [] }));
    // The same probe republished, or an older one arriving late, is not history.
    assert.isFalse(shouldRecord(first, reading(at(0), 55)));
    assert.isFalse(shouldRecord(first, reading(at(-MINUTE), 55)));
  });

  it("records a move of at least 0.1 point, in either direction, and nothing smaller", () => {
    const from = (percent: number) => reading(at(0), percent);
    const to = (percent: number) => reading(at(MINUTE), percent);
    // 40.04 and 40.06 sit on either side of a 0.1 rounding edge but are 0.02 apart.
    assert.isFalse(shouldRecord(from(40.04), to(40.06)));
    assert.isFalse(shouldRecord(from(40), to(40.09)));
    assert.isTrue(shouldRecord(from(40), to(40.1)));
    assert.isTrue(shouldRecord(from(40), to(39.9)));
    assert.isTrue(shouldRecord(from(0.2), to(0.3)));
  });

  it("records a moved reset, a changed availability and a changed window length", () => {
    const first = reading(at(0), 40);
    const next = (window: Partial<ServerProviderUsageWindow>) => ({
      checkedAt: at(MINUTE),
      windows: [weekly(40, window)],
    });
    assert.isTrue(shouldRecord(first, next({ resetsAt: "2026-11-20T14:00:00.000Z" })));
    assert.isFalse(shouldRecord(first, next({ resetsAt: "2026-11-20T13:00:00.400Z" })));
    assert.isTrue(shouldRecord(first, next({ windowDurationMins: 10_080 })));
    assert.isTrue(shouldRecord(first, reading(at(MINUTE), 40, { unavailable: "probeFailed" })));
  });

  it("compares windows by id, whatever order they arrive in", () => {
    const session = { id: "five_hour", kind: "session" as const, label: "5h", usedPercent: 10 };
    const first: UsageReading = { checkedAt: at(0), windows: [weekly(40), session] };
    const swapped: UsageReading = { checkedAt: at(MINUTE), windows: [session, weekly(40)] };
    assert.isFalse(shouldRecord(first, swapped));
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

  it.effect("finds the last reading by time when lines are out of order", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(at(2 * MINUTE), 50), reading(at(0), 40)]),
      );
      const history = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 50]);
      // Older than the newest reading on disk, so it is not history.
      yield* history.record([provider(limits(at(MINUTE), 45))]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 50]);
    }),
  );

  it.effect("drops a line torn by a crash and keeps appending after it", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const file = yield* seedHistory(
        directory,
        INSTANCE,
        `${JSON.stringify(reading(at(0), 40))}\n{"checkedAt":"2026-11-13T12:0`,
      );

      const history = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40]);
      yield* history.compact;
      yield* history.record([provider(limits(at(MINUTE), 45))]);

      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 45]);
      assert.strictEqual((yield* fileLines(file)).length, 2);
    }),
  );

  it.effect("a torn last line does not swallow the next reading, without a compaction", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      yield* seedHistory(
        directory,
        INSTANCE,
        `${JSON.stringify(reading(at(0), 40))}\n{"checkedAt":"2026-11-13T12:0`,
      );

      const history = yield* make({ directory });
      yield* history.record([provider(limits(at(MINUTE), 45))]);
      // A fresh process reads the file, so this checks what is on disk.
      const restarted = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* restarted.readings(INSTANCE)), [40, 45]);
    }),
  );

  it.effect("retention drops old readings and removes a history with none left", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* historyDirectory;
      const old = at(-DEFAULT_RETENTION_MS - HOUR);
      yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(old, 10), reading(at(-MINUTE), 60)]),
      );
      yield* seedHistory(directory, SECOND, jsonLines([reading(old, 99)]));
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

  it.effect("a clock far ahead at start does not age the history out", () =>
    Effect.gen(function* () {
      const directory = yield* historyDirectory;
      const file = yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(at(-HOUR), 40), reading(at(0), 41)]),
      );
      yield* TestClock.setTime(NOW_MS + 100 * DAY);
      const history = yield* make({ directory });
      yield* history.compact;
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 41]);
      assert.strictEqual((yield* fileLines(file)).length, 2);
    }),
  );

  it.effect("a reading stamped ahead of the clock does not block recording", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      // Written while the clock was two days ahead, then the clock was corrected.
      const file = yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(at(-10 * MINUTE), 40), reading(at(2 * DAY), 90)]),
      );
      const history = yield* make({ directory });
      yield* history.compact;
      // Not served while ahead of the clock, but kept: the clock may be the one that is wrong.
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40]);
      assert.strictEqual((yield* fileLines(file)).length, 2);
      yield* history.record([provider(limits(at(MINUTE), 45))]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 45]);

      // The clock steps back while running: the last reading is now ahead of it.
      yield* TestClock.setTime(NOW_MS - DAY);
      yield* history.record([provider(limits(at(-DAY + MINUTE), 30))]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [30]);
    }),
  );

  it.effect("an unreadable history survives start-up compaction and is not reported empty", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const contents = jsonLines([reading(at(-MINUTE), 40)]);
      const file = yield* seedHistory(directory, INSTANCE, contents);
      const fs = yield* failingReadsOf(file);

      const history = yield* make({ directory }).pipe(
        Effect.provideService(FileSystem.FileSystem, fs),
      );
      yield* history.compact;
      yield* history.record([provider(limits(at(0), 45))]);

      const real = yield* FileSystem.FileSystem;
      assert.strictEqual(yield* real.readFileString(file), contents);
      const read = yield* Effect.flip(history.readings(INSTANCE));
      assert.strictEqual(read._tag, "UsageHistoryUnreadable");
    }),
  );

  it.effect("keeps lines that no longer decode, and skips them one by one", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      // A window kind this build does not know, as a newer T3 version could write.
      const newer = JSON.stringify({
        checkedAt: at(-MINUTE),
        windows: [{ ...weekly(45), kind: "fortnightly" }],
      });
      const file = yield* seedHistory(
        directory,
        INSTANCE,
        `${JSON.stringify(reading(at(-2 * MINUTE), 40))}\n${newer}\n${JSON.stringify(reading(at(0), 50))}\n`,
      );
      const onlyNewer = yield* seedHistory(directory, SECOND, `${newer}\n`);

      const history = yield* make({ directory });
      yield* history.compact;

      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 50]);
      assert.include(yield* fileLines(file), newer);
      assert.deepStrictEqual(yield* fileLines(onlyNewer), [newer]);
    }),
  );

  it.effect("compacts every few appends and holds the per-instance cap", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS + 10 * MINUTE);
      const directory = yield* historyDirectory;
      const history = yield* make({
        directory,
        appendsBetweenCompactions: 3,
        maxReadingsPerInstance: 4,
      });
      for (let step = 0; step < 10; step++) {
        yield* history.record([provider(limits(at(step * MINUTE), step))]);
      }
      const path = yield* Path.Path;
      // Compactions after appends 3, 6 and 9 each cut to 4; append 10 adds one.
      assert.strictEqual((yield* fileLines(path.join(directory, `${INSTANCE}.jsonl`))).length, 5);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [5, 6, 7, 8, 9]);
    }),
  );

  it.effect("thins old readings to one per interval but keeps every reset", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const start = -3 * DAY;
      // One reading a minute for an hour, three days ago, with a reset at minute 37.
      const old = Array.from({ length: 60 }, (_, minute): UsageReading => ({
        checkedAt: at(start + minute * MINUTE),
        windows: [minute < 37 ? weekly(minute) : weekly(minute - 37, { resetsAt: at(4 * DAY) })],
      }));
      const recent = [reading(at(-2 * MINUTE), 70), reading(at(-MINUTE), 71)];
      yield* seedHistory(directory, INSTANCE, jsonLines([...old, ...recent]));

      const history = yield* make({ directory, thinAfterMs: DAY, thinIntervalMs: 15 * MINUTE });
      yield* history.compact;

      const kept = (yield* history.readings(INSTANCE)).map(
        (entry) => (Date.parse(entry.checkedAt) - (NOW_MS + start)) / MINUTE,
      );
      // Minutes 0, 15, 30, the reset (37) and 52; the last two minutes are recent and all kept.
      assert.deepStrictEqual(kept.slice(0, 5), [0, 15, 30, 37, 52]);
      assert.strictEqual(kept.length, 7);
    }),
  );

  it.effect("records the account, hashed, for each kind of provider identity", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const history = yield* make({ directory });
      const codex = ProviderInstanceId.make("codex_work");
      const antigravity = ProviderInstanceId.make("antigravity_main");
      const anonymous = ProviderInstanceId.make("claude_anonymous");
      const claudeAt = (offset: number, email: string) =>
        provider(limits(at(offset), 40), INSTANCE, { auth: { email } });
      const codexAt = (offset: number, workspaceId: string) =>
        provider(limits(at(offset), 40), codex, {
          driver: CODEX,
          auth: { email: "person@example.com", workspaceId },
        });
      const antigravityAt = (offset: number, credentialFingerprint: string) =>
        provider(limits(at(offset), 40, { credentialFingerprint }), antigravity, {
          driver: ANTIGRAVITY,
        });

      yield* history.record([
        claudeAt(0, "person@example.com"),
        codexAt(0, "workspace-a"),
        antigravityAt(0, "fingerprint-a"),
        provider(limits(at(0), 40), anonymous),
      ]);
      // Same meters, another account: a new reading in a new series.
      yield* history.record([
        claudeAt(MINUTE, "other@example.com"),
        codexAt(MINUTE, "workspace-b"),
        antigravityAt(MINUTE, "fingerprint-b"),
        provider(limits(at(MINUTE), 40), anonymous),
      ]);

      const accounts = (readings: ReadonlyArray<UsageReading>) =>
        readings.map((entry) => entry.account);
      const claude = accounts(yield* history.readings(INSTANCE));
      const codexAccounts = accounts(yield* history.readings(codex));
      const antigravityAccounts = accounts(yield* history.readings(antigravity));
      for (const series of [claude, codexAccounts, antigravityAccounts]) {
        assert.strictEqual(series.length, 2);
        assert.isString(series[0]);
        assert.isString(series[1]);
        assert.notStrictEqual(series[0], series[1]);
      }
      assert.deepStrictEqual(accounts(yield* history.readings(anonymous)), [undefined]);
      assert.strictEqual(claude[1], accountOf(claudeAt(0, "Other@Example.com ")));

      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      for (const name of yield* fs.readDirectory(directory)) {
        const contents = yield* fs.readFileString(path.join(directory, name));
        assert.notInclude(contents, "example.com");
        assert.notInclude(contents, "workspace-");
      }
    }),
  );

  it.effect("groups two instances on one account into one series", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const history = yield* make({ directory });
      const on = (instanceId: ProviderInstanceId, offset: number, percent: number, email: string) =>
        provider(limits(at(offset), percent), instanceId, { auth: { email } });

      yield* history.record([on(INSTANCE, 0, 40, "shared@example.com")]);
      yield* history.record([on(SECOND, MINUTE, 41, "shared@example.com")]);
      yield* history.record([on(INSTANCE, 2 * MINUTE, 42, "elsewhere@example.com")]);

      const shared = accountOf(on(INSTANCE, 0, 40, "shared@example.com"));
      assert.isDefined(shared);
      const series = yield* history.readingsOfAccount(shared!);
      assert.deepStrictEqual(
        series.map((entry) => [entry.instanceId, entry.reading.windows[0]?.usedPercent]),
        [
          [INSTANCE, 40],
          [SECOND, 41],
        ],
      );
    }),
  );

  it.effect("serves loaded readings from memory, without reading the file again", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      yield* seedHistory(directory, INSTANCE, jsonLines([reading(at(-MINUTE), 40)]));
      const fs = yield* FileSystem.FileSystem;
      let fileReads = 0;
      const counting: FileSystem.FileSystem = {
        ...fs,
        readFileString: (filePath, encoding) => {
          fileReads++;
          return fs.readFileString(filePath, encoding);
        },
      };
      const history = yield* make({ directory }).pipe(
        Effect.provideService(FileSystem.FileSystem, counting),
      );
      yield* history.compact;
      yield* history.record([provider(limits(at(0), 45))]);
      const readsAfterStart = fileReads;
      for (let call = 0; call < 3; call++) {
        assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 45]);
      }
      assert.strictEqual(fileReads, readsAfterStart);
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

  it.effect(
    "the server layer records the start snapshot and later changes, past a failed append",
    () =>
      TestClock.withLive(
        Effect.gen(function* () {
          const fs = yield* FileSystem.FileSystem;
          const path = yield* Path.Path;
          const baseDir = yield* fs.realPath(
            yield* fs.makeTempDirectoryScoped({ prefix: "usage-readings-layer-" }),
          );
          const changes = yield* Queue.unbounded<ReadonlyArray<ServerProvider>>();
          // Milestones: every successful append, and the one append that fails as on a full disk.
          const appended = yield* Queue.unbounded<string>();
          const appendFailed = yield* Deferred.make<void>();
          let failNextAppend = false;
          const faultyFs: FileSystem.FileSystem = {
            ...fs,
            writeFileString: (filePath, data, writeOptions) => {
              if (writeOptions?.flag !== "a") {
                return fs.writeFileString(filePath, data, writeOptions);
              }
              if (!failNextAppend) {
                return fs
                  .writeFileString(filePath, data, writeOptions)
                  .pipe(Effect.tap(() => Queue.offer(appended, data)));
              }
              failNextAppend = false;
              return Deferred.succeed(appendFailed, undefined).pipe(
                Effect.andThen(
                  Effect.fail(
                    PlatformError.systemError({
                      _tag: "Unknown",
                      module: "FileSystem",
                      method: "writeFileString",
                      description: "disk full",
                      pathOrDescriptor: filePath,
                    }),
                  ),
                ),
              );
            },
          };
          const dependencies = Layer.mergeAll(
            Layer.mock(ProviderRegistry)({
              getProviders: Effect.succeed([provider(limits(at(0), 40))]),
              streamChanges: Stream.fromQueue(changes),
            }),
            Layer.succeed(FileSystem.FileSystem, faultyFs),
          );
          const milestone = <A, E>(effect: Effect.Effect<A, E>) =>
            effect.pipe(Effect.timeout("5 seconds"));

          yield* Effect.gen(function* () {
            yield* UsageReadingsHistory.UsageReadingsHistory;
            // Start-up records the providers it finds.
            yield* milestone(Queue.take(appended));
            // A failed append is logged; the subscription keeps running.
            failNextAppend = true;
            yield* Queue.offer(changes, [provider(limits(at(MINUTE), 45))]);
            yield* milestone(Deferred.await(appendFailed));
            yield* Queue.offer(changes, [provider(limits(at(2 * MINUTE), 50))]);
            yield* milestone(Queue.take(appended));
          }).pipe(
            Effect.provide(
              UsageReadingsHistory.layer.pipe(
                Layer.provide(dependencies),
                Layer.provide(ServerConfig.layerTest(baseDir, baseDir)),
              ),
            ),
          );

          const { stateDir } = yield* ServerConfig.pipe(
            Effect.provide(ServerConfig.layerTest(baseDir, baseDir)),
          );
          const directory = path.join(stateDir, "usage-readings");
          const recorded = (yield* fileLines(path.join(directory, `${INSTANCE}.jsonl`))).map(
            (line) => JSON.parse(line) as UsageReading,
          );
          assert.deepStrictEqual(percents(recorded), [40, 50]);
        }),
      ),
  );
});
