import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Layer from "effect/Layer";
import * as Scope from "effect/Scope";
import * as TestClock from "effect/testing/TestClock";

import * as DesktopUpdates from "./DesktopUpdates.ts";
import { flushCallbacks, makeHarness, type UpdatesHarnessOptions } from "./updatesTestHarness.ts";

// The first background check finds 1.2.4 and downloading it finishes; later checks find nothing new.
const autoUpdateHarness = (options: UpdatesHarnessOptions = {}) => {
  let checks = 0;
  const harness = makeHarness({
    autoUpdate: true,
    beforeSetUpdateChannel: Effect.void,
    checkForUpdates: Effect.sync(() => {
      checks += 1;
      if (checks === 1) harness.emit("update-available", { version: "1.2.4" });
    }),
    downloadUpdate: Effect.sync(() => {
      harness.emit("update-downloaded", { version: "1.2.4" });
    }),
    ...options,
  });
  return harness;
};

const records = (harness: ReturnType<typeof makeHarness>) =>
  [...harness.installRecords.values()].map((record) => JSON.parse(record));

const run = <E>(
  harness: ReturnType<typeof makeHarness>,
  body: (updates: DesktopUpdates.DesktopUpdates["Service"]) => Effect.Effect<void, E>,
) =>
  Effect.scoped(
    Effect.gen(function* () {
      const updates = yield* DesktopUpdates.DesktopUpdates;
      yield* updates.configure;
      yield* body(updates);
    }),
  ).pipe(Effect.provide(Layer.merge(TestClock.layer(), harness.layer)));

const passStartupCheck = Effect.gen(function* () {
  yield* TestClock.adjust("16 seconds");
  yield* flushCallbacks;
});

describe("DesktopUpdates with the autoUpdate setting", () => {
  it.effect("records an install on quit like the Update button does", () => {
    const harness = autoUpdateHarness();
    return run(harness, () =>
      Effect.gen(function* () {
        yield* passStartupCheck;
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
        // Nothing yet: the app has not quit.
        assert.deepEqual(records(harness), []);
      }),
    ).pipe(
      Effect.tap(() =>
        Effect.sync(() => {
          // The record is what lets the next start report an install that did not apply.
          assert.deepEqual(records(harness), [{ fromVersion: "1.2.3", targetVersion: "1.2.4" }]);
          assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
        }),
      ),
    );
  });

  it.effect("leaves no record when the app stops without quitting", () => {
    const harness = autoUpdateHarness();
    // A crash or power cut: the app's scope never closes, so its quit step never runs.
    return Effect.gen(function* () {
      const updates = yield* DesktopUpdates.DesktopUpdates;
      const scope = yield* Scope.make();
      yield* updates.configure.pipe(Scope.provide(scope));
      yield* passStartupCheck;
      assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
      assert.deepEqual(records(harness), []);
    }).pipe(Effect.provide(Layer.merge(TestClock.layer(), harness.layer)));
  });

  it.effect("installs nothing when the setting is turned off just before quitting", () => {
    const harness = autoUpdateHarness();
    return run(harness, () =>
      Effect.gen(function* () {
        yield* passStartupCheck;
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
        // Turned off and quit before the next background check could disarm.
        harness.setAutoUpdate(false);
      }),
    ).pipe(
      Effect.tap(() =>
        Effect.sync(() => {
          assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), false);
          assert.deepEqual(records(harness), []);
        }),
      ),
    );
  });

  it.effect("leaves a version that did not apply to the Update button", () => {
    const harness = autoUpdateHarness({
      installRecord: JSON.stringify({ fromVersion: "1.2.3", targetVersion: "1.2.4" }),
    });
    return run(harness, (updates) =>
      Effect.gen(function* () {
        assert.equal((yield* updates.getState).notAppliedVersion, "1.2.4");
        yield* passStartupCheck;
        assert.equal(harness.downloadCount(), 0);
        assert.notInclude(harness.autoInstallOnAppQuit, true);
        assert.equal((yield* updates.getState).notAppliedVersion, "1.2.4");
      }),
    );
  });

  it.effect("stops installing on quit when the setting is turned off", () => {
    const harness = autoUpdateHarness();
    return run(harness, () =>
      Effect.gen(function* () {
        yield* passStartupCheck;
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
        harness.setAutoUpdate(false);
        yield* TestClock.adjust("5 minutes");
        yield* flushCallbacks;
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), false);
        assert.deepEqual(records(harness), []);
      }),
    );
  });

  it.effect("stops installing on quit when the track changes", () => {
    const harness = autoUpdateHarness();
    return run(harness, (updates) =>
      Effect.gen(function* () {
        yield* passStartupCheck;
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), true);
        yield* updates.setChannel("nightly");
        assert.deepEqual(harness.autoInstallOnAppQuit.at(-1), false);
        assert.deepEqual(records(harness), []);
      }),
    );
  });

  it.effect("never arms install-on-quit without the setting", () => {
    const harness = autoUpdateHarness({ autoUpdate: false });
    return run(harness, () =>
      Effect.gen(function* () {
        yield* passStartupCheck;
        assert.equal(harness.downloadCount(), 0);
        assert.notInclude(harness.autoInstallOnAppQuit, true);
      }),
    );
  });
});
