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
import * as Clock from "effect/Clock";
import * as DateTime from "effect/DateTime";
import * as Deferred from "effect/Deferred";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";
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
const failingReadsOf = (unreadable: string, denied: () => boolean = () => true) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const faulty: FileSystem.FileSystem = {
      ...fs,
      readFileString: (filePath, encoding) =>
        filePath === unreadable && denied()
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

/** Collects warning and info messages logged by the effect it is provided to. */
const logCollector = () => {
  const warnings: Array<string> = [];
  const infos: Array<string> = [];
  const logger = Logger.make(({ logLevel, message }) => {
    const text = String(Array.isArray(message) ? message[0] : message);
    if (logLevel === "Warn") warnings.push(text);
    if (logLevel === "Info") infos.push(text);
  });
  return { warnings, infos, layer: Logger.layer([logger], { mergeWithExisting: false }) };
};

const diskFull = (filePath: string) =>
  PlatformError.systemError({
    _tag: "Unknown",
    module: "FileSystem",
    method: "writeFileString",
    description: "disk full",
    pathOrDescriptor: filePath,
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
    // A reset that disappears or appears, a window of another kind, a window under another id.
    const noReset = (checkedAt: string): UsageReading => ({
      checkedAt,
      windows: [{ id: "seven_day", kind: "weekly", label: "Weekly", usedPercent: 40 }],
    });
    assert.isTrue(shouldRecord(first, noReset(at(MINUTE))));
    assert.isTrue(shouldRecord(noReset(at(0)), reading(at(MINUTE), 40)));
    assert.isTrue(shouldRecord(first, next({ kind: "session" })));
    assert.isTrue(shouldRecord(first, next({ id: "seven_day_opus" })));
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

  it.effect("drops a reading appended before an earlier-stamped one: its clock was ahead", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS + 2 * MINUTE);
      const directory = yield* historyDirectory;
      // Appended in this order: the clock jumped two days ahead for one reading, then was corrected.
      const file = yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(at(0), 40), reading(at(2 * DAY), 90), reading(at(MINUTE), 45)]),
      );
      const history = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 45]);
      yield* history.compact;
      assert.strictEqual((yield* fileLines(file)).length, 2);

      // Even once the clock passes its stamp, the ahead reading does not mix back in.
      yield* seedHistory(
        directory,
        INSTANCE,
        jsonLines([reading(at(0), 40), reading(at(2 * DAY), 90), reading(at(MINUTE), 45)]),
      );
      yield* TestClock.setTime(NOW_MS + 3 * DAY);
      const later = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* later.readings(INSTANCE)), [40, 45]);
    }),
  );

  it.effect(
    "keeps readings ahead of a clock that is behind until a later append contradicts them",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW_MS);
        const directory = yield* historyDirectory;
        // Real readings; the clock is now a day behind them and nothing was appended since.
        const file = yield* seedHistory(
          directory,
          INSTANCE,
          jsonLines([reading(at(DAY), 40), reading(at(DAY + MINUTE), 41)]),
        );
        const history = yield* make({ directory });
        yield* history.compact;
        yield* history.compact;
        assert.deepStrictEqual(yield* history.readings(INSTANCE), []);
        assert.strictEqual((yield* fileLines(file)).length, 2);

        // The clock is put right: the readings are served again.
        yield* TestClock.setTime(NOW_MS + DAY + 2 * MINUTE);
        const restarted = yield* make({ directory });
        assert.deepStrictEqual(percents(yield* restarted.readings(INSTANCE)), [40, 41]);
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
      // The append after it shows that reading came from a clock that was ahead: compaction drops it.
      yield* history.compact;
      assert.strictEqual((yield* fileLines(file)).length, 2);
      assert.notInclude((yield* fileLines(file)).join("\n"), at(2 * DAY));

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

  it.effect("logs a failing history once per instance until it works again", () => {
    const logs = logCollector();
    return Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const file = yield* seedHistory(directory, INSTANCE, jsonLines([reading(at(-MINUTE), 40)]));
      let denied = true;
      let appendsFail = false;
      const readsDenied = yield* failingReadsOf(file, () => denied);
      const fs: FileSystem.FileSystem = {
        ...readsDenied,
        writeFileString: (filePath, data, writeOptions) =>
          appendsFail && writeOptions?.flag === "a"
            ? Effect.fail(diskFull(filePath))
            : readsDenied.writeFileString(filePath, data, writeOptions),
      };
      const history = yield* make({ directory }).pipe(
        Effect.provideService(FileSystem.FileSystem, fs),
      );
      yield* history.compact;
      for (let step = 0; step < 3; step++) {
        yield* history.record([provider(limits(at(step * MINUTE), 45 + step))]);
      }
      assert.strictEqual(logs.warnings.length, 1);

      denied = false;
      yield* history.record([provider(limits(at(3 * MINUTE), 50))]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40, 50]);
      assert.strictEqual(logs.infos.length, 1);

      // Appends fail as on a full disk: one warning for the run of failures, not one per update.
      appendsFail = true;
      for (let step = 4; step < 7; step++) {
        yield* history.record([provider(limits(at(step * MINUTE), 50 + step))]);
      }
      assert.strictEqual(logs.warnings.length, 2);
    }).pipe(Effect.provide(logs.layer));
  });

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

  it.effect("a reading torn by a failed append in a running server does not swallow the next", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const real = yield* FileSystem.FileSystem;
      let tearNextAppend = false;
      const fs: FileSystem.FileSystem = {
        ...real,
        writeFileString: (filePath, data, writeOptions) => {
          if (!tearNextAppend || writeOptions?.flag !== "a") {
            return real.writeFileString(filePath, data, writeOptions);
          }
          tearNextAppend = false;
          // Half the line lands, then the disk is full.
          return real
            .writeFileString(filePath, data.slice(0, data.length / 2), writeOptions)
            .pipe(Effect.andThen(Effect.fail(diskFull(filePath))));
        },
      };
      const history = yield* make({ directory }).pipe(
        Effect.provideService(FileSystem.FileSystem, fs),
      );
      yield* history.record([provider(limits(at(0), 40))]);
      tearNextAppend = true;
      yield* history.record([provider(limits(at(MINUTE), 45))]);
      yield* history.record([provider(limits(at(2 * MINUTE), 50))]);

      const restarted = yield* make({ directory });
      assert.deepStrictEqual(percents(yield* restarted.readings(INSTANCE)), [40, 50]);
    }),
  );

  it.effect("removes a temp folder left by a crash during compaction and keeps the history", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const directory = yield* historyDirectory;
      yield* seedHistory(directory, INSTANCE, jsonLines([reading(at(-MINUTE), 40)]));
      // What writeFileStringAtomically leaves when killed before its rename.
      const leftover = path.join(directory, `${INSTANCE}.jsonl.Ab12Cd`);
      yield* fs.makeDirectory(leftover);
      yield* fs.writeFileString(path.join(leftover, "contents.tmp"), '{"checkedAt":');
      // Not a leftover: a file of that shape is left alone.
      yield* fs.writeFileString(path.join(directory, `${SECOND}.jsonl.note`), "keep\n");

      const history = yield* make({ directory });
      yield* history.compact;

      assert.deepStrictEqual((yield* fs.readDirectory(directory)).toSorted(), [
        `${INSTANCE}.jsonl`,
        `${SECOND}.jsonl.note`,
      ]);
      assert.deepStrictEqual(percents(yield* history.readings(INSTANCE)), [40]);
    }),
  );

  it.effect("reads each history file once, and a read does not wait behind a write", () =>
    TestClock.withLive(
      Effect.gen(function* () {
        const directory = yield* historyDirectory;
        const real = yield* FileSystem.FileSystem;
        let fileReads = 0;
        const writeStarted = yield* Deferred.make<void>();
        const releaseWrite = yield* Deferred.make<void>();
        let holdNextAppend = false;
        const fs: FileSystem.FileSystem = {
          ...real,
          readFileString: (filePath, encoding) => {
            fileReads++;
            return real.readFileString(filePath, encoding);
          },
          writeFileString: (filePath, data, writeOptions) => {
            if (!holdNextAppend || writeOptions?.flag !== "a") {
              return real.writeFileString(filePath, data, writeOptions);
            }
            holdNextAppend = false;
            return Deferred.succeed(writeStarted, undefined).pipe(
              Effect.andThen(Deferred.await(releaseWrite)),
              Effect.andThen(real.writeFileString(filePath, data, writeOptions)),
            );
          },
        };
        const history = yield* make({ directory }).pipe(
          Effect.provideService(FileSystem.FileSystem, fs),
        );
        const nowMs = yield* Clock.currentTimeMillis;
        const stamp = (offsetMs: number) =>
          DateTime.formatIso(DateTime.makeUnsafe(nowMs + offsetMs));
        for (let step = 0; step < 5; step++) {
          yield* history.record([provider(limits(stamp(step * MINUTE - HOUR), 40 + step))]);
        }
        // One read when the instance was first seen, none for each later snapshot.
        assert.strictEqual(fileReads, 1);

        holdNextAppend = true;
        const writing = yield* Effect.forkChild(
          history.record([provider(limits(stamp(-MINUTE), 50))]),
        );
        yield* Deferred.await(writeStarted).pipe(Effect.timeout("5 seconds"));
        // The write holds the lock; a read of a loaded history is served at once.
        const during = yield* history.readings(INSTANCE).pipe(Effect.timeout("2 seconds"));
        assert.deepStrictEqual(percents(during), [40, 41, 42, 43, 44]);
        yield* Deferred.succeed(releaseWrite, undefined);
        yield* Fiber.join(writing);
        assert.deepStrictEqual(
          percents(yield* history.readings(INSTANCE)),
          [40, 41, 42, 43, 44, 50],
        );
        assert.strictEqual(fileReads, 1);
      }),
    ),
  );

  it.effect("a probe that reports less of the account keeps the known account", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW_MS);
      const directory = yield* historyDirectory;
      const history = yield* make({ directory });
      const codex = ProviderInstanceId.make("codex_work");
      const second = ProviderInstanceId.make("codex_second");
      const email = "person@example.com";
      const full = { email, workspaceId: "workspace-a" };
      const codexAt = (
        offset: number,
        percent: number,
        auth: Partial<ServerProviderAuth>,
        instanceId: ProviderInstanceId = codex,
        overrides?: Partial<ServerProviderUsageLimits>,
      ) => provider(limits(at(offset), percent, overrides), instanceId, { driver: CODEX, auth });

      yield* history.record([codexAt(0, 40, full)]);
      // The rate-limit read failed, so no workspace id came with the email.
      yield* history.record([
        codexAt(MINUTE, 40, { email }, codex, {
          windows: [],
          unavailable: { reason: "probeFailed" },
        }),
      ]);
      yield* history.record([codexAt(2 * MINUTE, 41, full)]);
      // The auth probe failed too: no identity at all.
      yield* history.record([codexAt(3 * MINUTE, 42, {})]);
      // Another instance on the same login that reports no workspace joins the same account.
      yield* history.record([
        codexAt(4 * MINUTE, 43, full),
        codexAt(4 * MINUTE, 10, { email }, second),
      ]);

      const known = accountOf(codexAt(0, 40, full));
      const accounts = (readings: ReadonlyArray<UsageReading>) =>
        readings.map((entry) => entry.account);
      assert.deepStrictEqual(accounts(yield* history.readings(codex)), Array(5).fill(known));
      assert.deepStrictEqual(accounts(yield* history.readings(second)), [known]);
      assert.strictEqual((yield* history.readingsOfAccount(known!)).length, 6);

      // Another login on the instance is still another account.
      yield* history.record([codexAt(5 * MINUTE, 43, { email: "other@example.com" })]);
      const swapped = (yield* history.readings(codex)).at(-1)?.account;
      assert.isString(swapped);
      assert.notStrictEqual(swapped, known);
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
