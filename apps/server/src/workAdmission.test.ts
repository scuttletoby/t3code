import * as NodeServices from "@effect/platform-node/NodeServices";
import { describe, expect, it } from "@effect/vitest";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Fiber from "effect/Fiber";
import * as FileSystem from "effect/FileSystem";
import * as Path from "effect/Path";
import * as Ref from "effect/Ref";
import * as TestClock from "effect/testing/TestClock";

import * as EffectWorker from "./orchestration-v2/EffectWorker.ts";
import {
  makeFileUpdateHoldStore,
  makeWorkAdmissionGate,
  memoryUpdateHoldStore,
  UPDATE_HOLD_MARKER_FILE,
  UPDATE_HOLD_MAX_AGE_MS,
  WORK_ADMISSION_CONSUMERS,
  WorkAdmission,
} from "./workAdmission.ts";

const settle = Effect.gen(function* () {
  for (let i = 0; i < 20; i++) yield* Effect.yieldNow;
});

describe("WorkAdmission", () => {
  it.effect("holds new work until released and counts work already admitted", () =>
    Effect.gen(function* () {
      const gate = yield* makeWorkAdmissionGate();
      const running = yield* Effect.forkChild(gate.admit(Effect.never));
      yield* settle;
      expect(yield* gate.hold).toBe(1);
      const ran = yield* Ref.make(false);
      const waiting = yield* Effect.forkChild(gate.admit(Ref.set(ran, true)));
      yield* settle;
      expect(yield* Ref.get(ran)).toBe(false);
      yield* Fiber.interrupt(running);
      expect(yield* gate.inFlight).toBe(0);
      yield* gate.release;
      yield* Fiber.join(waiting);
      expect(yield* Ref.get(ran)).toBe(true);
      expect(yield* gate.inFlight).toBe(0);
    }),
  );

  it.effect("defaults to a pass-through that says it does not hold", () =>
    Effect.gen(function* () {
      const gate = yield* WorkAdmission;
      expect(gate.enforced).toBe(false);
      expect(yield* gate.admit(Effect.succeed(1))).toBe(1);
      // Nothing uses it, so an automatic update refuses to hold it.
      expect(yield* gate.missingConsumers).toEqual(WORK_ADMISSION_CONSUMERS);
    }),
  );

  it.effect("stops the outbox daemon from claiming effects while held", () =>
    Effect.gen(function* () {
      const gate = yield* makeWorkAdmissionGate();
      const claims = yield* Ref.make(0);
      const worker = EffectWorker.OrchestrationEffectWorkerV2.of({
        awaitWork: Effect.never,
        runOnce: () => Ref.update(claims, (count) => count + 1).pipe(Effect.as(false)),
        runRecoveryOnce: Effect.succeed(false),
        nextClaimableAt: () => Effect.succeedNone,
        drain: () => Effect.succeed(0),
      });
      yield* gate.hold;
      expect(yield* gate.missingConsumers).toContain("outbox");
      const daemon = yield* Effect.forkChild(
        EffectWorker.runDaemonWithOptions({ concurrency: 2 }).pipe(
          Effect.provideService(EffectWorker.OrchestrationEffectWorkerV2, worker),
          Effect.provideService(WorkAdmission, gate),
        ),
      );
      yield* settle;
      expect(yield* Ref.get(claims)).toBe(0);
      // The daemon says it claims through this gate.
      expect(yield* gate.missingConsumers).not.toContain("outbox");
      yield* gate.release;
      yield* settle;
      expect(yield* Ref.get(claims)).toBe(2);
      yield* Fiber.interrupt(daemon);
    }),
  );

  it.effect("a hold longer than the slot wait keeps one waiting claim per slot", () =>
    Effect.gen(function* () {
      const gate = yield* makeWorkAdmissionGate();
      const claims = yield* Ref.make(0);
      const worker = EffectWorker.OrchestrationEffectWorkerV2.of({
        awaitWork: Effect.never,
        runOnce: () => Ref.update(claims, (count) => count + 1).pipe(Effect.as(false)),
        runRecoveryOnce: Effect.succeed(false),
        nextClaimableAt: () => Effect.succeedNone,
        drain: () => Effect.succeed(0),
      });
      yield* gate.hold;
      const daemon = yield* Effect.forkChild(
        EffectWorker.runDaemonWithOptions({
          concurrency: 2,
          livenessPollIntervalMs: 3_600_000,
        }).pipe(
          Effect.provideService(EffectWorker.OrchestrationEffectWorkerV2, worker),
          Effect.provideService(WorkAdmission, gate),
        ),
      );
      // Several 30-second slot waits pass while the gate is held.
      for (let i = 0; i < 5; i++) {
        yield* settle;
        yield* TestClock.adjust("31 seconds");
      }
      yield* settle;
      expect(yield* Ref.get(claims)).toBe(0);
      yield* gate.release;
      yield* settle;
      // One claim per slot, not one more for every wait that passed.
      expect(yield* Ref.get(claims)).toBe(2);
      yield* Fiber.interrupt(daemon);
    }),
  );

  it.effect("tells startup about a fresh update marker only, and release removes it", () =>
    Effect.gen(function* () {
      const marker = yield* memoryUpdateHoldStore;
      const gate = yield* makeWorkAdmissionGate({ marker });
      const heldAt = DateTime.makeUnsafe(10 * 60_000);
      yield* TestClock.setTime(DateTime.toEpochMillis(heldAt));
      expect(yield* gate.heldForUpdate).toBeUndefined();
      yield* gate.hold;
      yield* gate.markHeldForUpdate({ heldAt: DateTime.formatIso(heldAt), targetVersion: "2" });
      yield* TestClock.adjust(UPDATE_HOLD_MAX_AGE_MS);
      expect(yield* gate.heldForUpdate).toEqual({
        heldAt,
        runs: undefined,
        started: new Set(),
      });
      // Work released here must not be started again by some later startup.
      yield* gate.release;
      expect(yield* marker.current).toBeUndefined();
    }),
  );

  it.effect("removes a stale or invalid marker and reads it as none", () =>
    Effect.gen(function* () {
      const marker = yield* memoryUpdateHoldStore;
      const gate = yield* makeWorkAdmissionGate({ marker });
      const heldAt = DateTime.makeUnsafe(10 * 60_000);
      yield* TestClock.setTime(DateTime.toEpochMillis(heldAt) + UPDATE_HOLD_MAX_AGE_MS + 1);
      // Older than the maximum, it is left over from a hold that ended without clearing it.
      yield* marker.write({ heldAt: DateTime.formatIso(heldAt), targetVersion: "2" });
      expect(yield* gate.heldForUpdate).toBeUndefined();
      expect(yield* marker.current).toBeUndefined();
      yield* marker.set("invalid");
      expect(yield* gate.heldForUpdate).toBeUndefined();
      expect(yield* marker.current).toBeUndefined();
      yield* marker.write({ heldAt: "not a time", targetVersion: "2" });
      expect(yield* gate.heldForUpdate).toBeUndefined();
      expect(yield* marker.current).toBeUndefined();
      // Nothing to record against once no fresh marker was read.
      expect((yield* Effect.flip(gate.recordHeldRunsStarted(["run-1"])))._tag).toBe(
        "UpdateHoldMarkerError",
      );
    }),
  );

  it.effect("records the kept runs once and what was started, and finishing settles", () =>
    Effect.gen(function* () {
      const marker = yield* memoryUpdateHoldStore;
      const gate = yield* makeWorkAdmissionGate({ marker });
      const hold = { heldAt: DateTime.formatIso(yield* DateTime.now), targetVersion: "2" };
      // An old-format marker: no runs, nothing started.
      yield* marker.write(hold);
      expect(yield* gate.heldWorkSettled).toBe(false);
      expect((yield* gate.heldForUpdate)?.runs).toBeUndefined();
      yield* gate.recordHeldRuns(["run-1", "run-2"]);
      // A later startup keeps the first list: that is the database the hold left.
      yield* gate.recordHeldRuns(["run-1", "run-2", "run-3"]);
      yield* gate.recordHeldRunsStarted(["run-1"]);
      yield* gate.recordHeldRunsStarted(["run-1"]);
      expect(yield* marker.current).toEqual({
        ...hold,
        runs: ["run-1", "run-2"],
        started: ["run-1"],
      });
      // What the next startup reads.
      const again = yield* makeWorkAdmissionGate({ marker });
      expect(yield* again.heldForUpdate).toMatchObject({
        runs: new Set(["run-1", "run-2"]),
        started: new Set(["run-1"]),
      });
      yield* gate.finishHeldWork;
      expect(yield* marker.current).toBeUndefined();
      expect(yield* gate.heldWorkSettled).toBe(true);
      // The pass-through holds nothing, so nothing waits for it.
      expect(yield* (yield* WorkAdmission).heldWorkSettled).toBe(true);
    }),
  );

  it.effect("runs the held-work starter that recovery set, and nothing without one", () =>
    Effect.gen(function* () {
      const gate = yield* makeWorkAdmissionGate();
      yield* gate.startHeldWork;
      const runs = yield* Ref.make(0);
      yield* gate.setHeldWorkStarter(Ref.update(runs, (count) => count + 1));
      yield* gate.startHeldWork;
      expect(yield* Ref.get(runs)).toBe(1);
    }),
  );

  it.effect("keeps the update marker beside the launcher state and survives a bad file", () =>
    Effect.gen(function* () {
      const fs = yield* FileSystem.FileSystem;
      const path = yield* Path.Path;
      const baseDir = yield* fs.makeTempDirectoryScoped();
      const store = yield* makeFileUpdateHoldStore(baseDir);
      expect(yield* store.read).toBeUndefined();
      const hold = { heldAt: "2026-01-01T00:00:00.000Z", targetVersion: "2" };
      yield* store.write(hold);
      const file = path.join(baseDir, "runtime", UPDATE_HOLD_MARKER_FILE);
      expect(yield* fs.exists(file)).toBe(true);
      expect(yield* fs.exists(`${file}.tmp`)).toBe(false);
      expect(yield* (yield* makeFileUpdateHoldStore(baseDir)).read).toEqual(hold);
      const withRuns = { ...hold, runs: ["run-1"], started: [] };
      yield* store.write(withRuns);
      expect(yield* store.read).toEqual(withRuns);
      // Unknown fields from a newer version do not make it invalid.
      yield* fs.writeFileString(file, JSON.stringify({ ...hold, later: 1 }));
      expect(yield* store.read).toMatchObject(hold);
      yield* fs.writeFileString(file, "{not json");
      expect(yield* store.read).toBe("invalid");
      yield* store.clear;
      yield* store.clear;
      expect(yield* fs.exists(file)).toBe(false);
    }).pipe(Effect.provide(NodeServices.layer)),
  );
});
