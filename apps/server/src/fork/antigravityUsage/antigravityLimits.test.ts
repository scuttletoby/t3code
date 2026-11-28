import * as NodeServices from "@effect/platform-node/NodeServices";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { describe, expect, it } from "@effect/vitest";
import { ProviderDriverKind, ProviderInstanceId, type ServerProvider } from "@t3tools/contracts";
import type { ManagedServerProvider } from "@t3tools/provider-core/server/snapshot";
import * as Deferred from "effect/Deferred";
import type * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as Stream from "effect/Stream";
import * as TestClock from "effect/testing/TestClock";

import { makeAntigravityLimits, makeCliRun, overlay } from "./antigravityLimits.ts";
import { usageReport } from "./usageReport.fixture.ts";
import { MESSAGES, parseUsageReport, type UsageOutcome } from "./usageReport.ts";

const provider: ServerProvider = {
  driver: ProviderDriverKind.make("antigravity"),
  instanceId: ProviderInstanceId.make("antigravity"),
  enabled: true,
  installed: true,
  version: "1.5.18",
  status: "ready",
  checkedAt: "2026-11-27T00:00:00.000Z",
  auth: { status: "authenticated", type: "oauth-personal" },
  models: [],
  skills: [],
  slashCommands: [],
};
const reading = parseUsageReport(usageReport());
// The fake CLI is a shell script.
const IS_WINDOWS = HostProcess.Platform.defaultValue() === "win32";
const failed = (message: string, stop?: boolean): UsageOutcome =>
  stop ? { _tag: "failed", message, stop } : { _tag: "failed", message };

const fixture = Effect.fn("AntigravityLimitsTest.fixture")(function* (
  outcomes: ReadonlyArray<UsageOutcome>,
  options: {
    readonly snapshot?: Partial<ServerProvider>;
    readonly gate?: Deferred.Deferred<void>;
  } = {},
) {
  const snapshot = yield* Ref.make<ServerProvider>({ ...provider, ...options.snapshot });
  const base: ManagedServerProvider = {
    resolveMaintenance: () => Effect.die("unused"),
    getSnapshot: Ref.get(snapshot),
    refresh: Ref.get(snapshot),
    streamChanges: Stream.never,
    applyUsageLimits: () => Effect.void,
  };
  let runs = 0;
  const run = Effect.gen(function* () {
    const outcome = outcomes[Math.min(runs, outcomes.length - 1)]!;
    runs++;
    if (options.gate) yield* Deferred.await(options.gate);
    return outcome;
  });
  const limits = yield* makeAntigravityLimits({ base, run });
  return { limits, runs: () => runs };
});

/** Moves the test clock and lets the woken fibers finish. */
const advance = (duration: Duration.Input) =>
  TestClock.adjust(duration).pipe(Effect.andThen(Effect.yieldNow), Effect.andThen(Effect.yieldNow));

describe("Antigravity limits from /usage", () => {
  it.effect("asks at most every 10 minutes and stamps the reading onto every snapshot", () =>
    Effect.gen(function* () {
      const { limits, runs } = yield* fixture([reading]);
      expect((yield* limits.getSnapshot).usageLimits).toBeUndefined();
      yield* advance("1 minute");
      expect(runs()).toBe(1);
      const first = (yield* limits.getSnapshot).usageLimits;
      expect(first?.windows.map((window) => window.id).toSorted()).toEqual([
        "3p-weekly",
        "gemini-5h",
        "gemini-weekly",
      ]);
      expect(first?.unavailable).toBeUndefined();
      yield* advance("9 minutes");
      expect(runs()).toBe(1);
      yield* advance("1 minute");
      expect(runs()).toBe(2);
    }).pipe(Effect.scoped),
  );

  it.effect("asks on a refresh, such as opening the Usage tab, a minute after the last run", () =>
    Effect.gen(function* () {
      const { limits, runs } = yield* fixture([reading]);
      yield* advance("1 minute");
      expect(runs()).toBe(1);
      yield* advance("30 seconds");
      yield* limits.refresh;
      yield* Effect.yieldNow;
      expect(runs()).toBe(1);
      yield* advance("30 seconds");
      yield* limits.refresh;
      yield* advance("0 millis");
      expect(runs()).toBe(2);
    }).pipe(Effect.scoped),
  );

  it.effect("never starts a second run while one is going", () =>
    Effect.gen(function* () {
      const gate = yield* Deferred.make<void>();
      const { limits, runs } = yield* fixture([reading], { gate });
      yield* advance("1 minute");
      expect(runs()).toBe(1);
      // Ticks and refreshes during the run leave it be; refreshUsage waits for it.
      yield* advance("12 minutes");
      yield* limits.refresh;
      const waiting = yield* Effect.forkChild(limits.refreshUsage!);
      yield* advance("1 minute");
      expect(runs()).toBe(1);
      yield* Deferred.succeed(gate, undefined);
      yield* Fiber.join(waiting);
      // refreshUsage ran only after the first run ended: over 10 minutes had passed by then.
      expect(runs()).toBe(2);
      expect((yield* limits.getSnapshot).usageLimits?.windows).toHaveLength(3);
    }).pipe(Effect.scoped),
  );

  it.effect("keeps the last reading and its time after a failure, and adds the reason", () =>
    Effect.gen(function* () {
      const { limits } = yield* fixture([reading, failed(MESSAGES.timedOut)]);
      yield* advance("1 minute");
      const good = (yield* limits.getSnapshot).usageLimits;
      yield* advance("10 minutes");
      const kept = (yield* limits.getSnapshot).usageLimits;
      expect(kept?.checkedAt).toBe(good?.checkedAt);
      expect(kept?.windows).toEqual(good?.windows);
      expect(kept?.unavailable).toEqual({ reason: "probeFailed", message: MESSAGES.timedOut });
    }).pipe(Effect.scoped),
  );

  it.effect("shows only the reason, with no window, when no reading came yet", () =>
    Effect.gen(function* () {
      const { limits } = yield* fixture([failed(MESSAGES.unreadable)]);
      yield* advance("1 minute");
      const limitsNow = (yield* limits.getSnapshot).usageLimits;
      expect(limitsNow?.windows).toEqual([]);
      expect(limitsNow?.unavailable).toEqual({
        reason: "probeFailed",
        message: MESSAGES.unreadable,
      });
    }).pipe(Effect.scoped),
  );

  it.effect("stops for good once a CLI answers /usage with a model turn", () =>
    Effect.gen(function* () {
      const { limits, runs } = yield* fixture([failed(MESSAGES.spentTurn, true)]);
      yield* advance("1 minute");
      yield* advance("30 minutes");
      yield* limits.refreshUsage!;
      expect(runs()).toBe(1);
      expect((yield* limits.getSnapshot).usageLimits?.unavailable?.message).toBe(
        MESSAGES.spentTurn,
      );
    }).pipe(Effect.scoped),
  );

  it.effect("leaves a signed-out or disabled instance alone", () =>
    Effect.gen(function* () {
      for (const snapshot of [
        { auth: { status: "unauthenticated" as const } },
        { enabled: false },
      ]) {
        const { limits, runs } = yield* fixture([reading], { snapshot });
        yield* advance("11 minutes");
        yield* limits.refreshUsage!;
        expect(runs()).toBe(0);
        expect((yield* limits.getSnapshot).usageLimits).toBeUndefined();
      }
    }).pipe(Effect.scoped),
  );

  it.effect("publishes a fresh snapshot when a run finishes", () =>
    Effect.gen(function* () {
      const { limits } = yield* fixture([reading]);
      const next = yield* Effect.forkChild(Stream.runHead(limits.streamChanges));
      yield* Effect.yieldNow;
      yield* advance("1 minute");
      const published = yield* Fiber.join(next);
      expect(published._tag === "Some" && published.value.usageLimits?.windows).toHaveLength(3);
    }).pipe(Effect.scoped),
  );

  it.effect("wraps only Google account instances", () =>
    Effect.gen(function* () {
      const { limits } = yield* fixture([reading]);
      const prepare = () => Effect.die("never spawned");
      for (const authMethod of ["gemini-api-key", "agent-platform"] as const) {
        expect(yield* overlay(limits, { authMethod }, prepare, {})).toEqual({});
      }
      expect(
        (yield* overlay(limits, { authMethod: "oauth-business" }, prepare, {})).snapshot,
      ).toBeDefined();
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});

describe("Antigravity /usage run", () => {
  const fakeCli = Effect.fn("AntigravityLimitsTest.fakeCli")(function* (
    script: string,
    version: string | null = "1.5.18",
  ) {
    const fs = yield* FileSystem.FileSystem;
    const path = yield* Path.Path;
    const directory = yield* fs.makeTempDirectoryScoped();
    const runTemp = path.join(directory, "run-temp");
    yield* fs.makeDirectory(runTemp);
    const command = path.join(directory, "antigravity");
    const argsFile = path.join(directory, "args");
    const reportFile = path.join(directory, "report.json");
    yield* fs.writeFileString(reportFile, usageReport());
    yield* fs.writeFileString(command, `#!/bin/sh\n${script}\n`);
    yield* fs.chmod(command, 0o755);
    const prepareSpawn = (cwd: string, baseEnv: NodeJS.ProcessEnv) =>
      Effect.succeed({
        executable: { version },
        spawn: {
          command,
          cwd,
          env: {
            ...baseEnv,
            PATH: process.env.PATH,
            TMPDIR: runTemp,
            ARGS: argsFile,
            REPORT: reportFile,
          },
        },
      });
    const calls = fs
      .exists(argsFile)
      .pipe(
        Effect.flatMap((exists) => (exists ? fs.readFileString(argsFile) : Effect.succeed(""))),
      );
    return { prepareSpawn, calls, runTemp };
  });

  it.effect.skipIf(IS_WINDOWS)(
    "runs /usage in print mode inside the run's temp folder and reads the report",
    () =>
      Effect.gen(function* () {
        const fs = yield* FileSystem.FileSystem;
        const { prepareSpawn, calls, runTemp } = yield* fakeCli(
          'printf "%s\\n" "$PWD" "$@" > "$ARGS"; cat "$REPORT"',
        );
        const outcome = yield* makeCliRun(prepareSpawn, { AGY_CLI_DISABLE_AUTO_UPDATE: "1" });
        expect(outcome).toEqual(reading);
        const [cwd, ...args] = (yield* calls).trim().split("\n");
        expect(yield* fs.realPath(cwd!)).toBe(yield* fs.realPath(runTemp));
        expect(args).toEqual(["-p", "/usage", "--output-format", "json", "--print-timeout", "25s"]);
      }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect.skipIf(IS_WINDOWS)("does not start a CLI below the checked version", () =>
    Effect.gen(function* () {
      for (const version of ["1.4.2", null]) {
        const { prepareSpawn, calls } = yield* fakeCli('echo called > "$ARGS"', version);
        const outcome = yield* makeCliRun(prepareSpawn, {});
        expect(outcome).toEqual({
          _tag: "failed",
          message: version ? MESSAGES.tooOld(version) : MESSAGES.noVersion,
        });
        expect(yield* calls).toBe("");
      }
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.live.skipIf(IS_WINDOWS)("stops a run that takes too long", () =>
    Effect.gen(function* () {
      const { prepareSpawn } = yield* fakeCli("exec sleep 30");
      const outcome = yield* makeCliRun(prepareSpawn, {}, "300 millis");
      expect(outcome).toEqual({ _tag: "failed", message: MESSAGES.timedOut });
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );

  it.effect.skipIf(IS_WINDOWS)("reads a garbled report as a reason", () =>
    Effect.gen(function* () {
      const { prepareSpawn } = yield* fakeCli('echo "{garbled"');
      expect(yield* makeCliRun(prepareSpawn, {})).toEqual({
        _tag: "failed",
        message: MESSAGES.unreadable,
      });
    }).pipe(Effect.scoped, Effect.provide(NodeServices.layer)),
  );
});
