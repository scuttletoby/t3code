import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";

import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import { renameRetiredAutoUpdateKeys } from "./autoUpdateSettings.ts";

// One settings service on a fresh temp base dir per test, as serverSettings.test.ts builds it.
const layerServerSettings = () =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
    Layer.provideMerge(
      Layer.fresh(
        ServerConfig.layerTest(process.cwd(), { prefix: "t3code-auto-update-settings-test-" }),
      ),
    ),
  );

/** Loads a settings file with these contents (none when undefined) and returns what the server reads. */
const loadSettingsFile = (contents: string | undefined) =>
  Effect.gen(function* () {
    const config = yield* ServerConfig.ServerConfig;
    const fs = yield* FileSystem.FileSystem;
    if (contents !== undefined) yield* fs.writeFileString(config.settingsPath, contents);
    const service = yield* ServerSettingsModule.ServerSettingsService;
    return yield* service.getSettings;
  });

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "automatic update settings saved under the old names",
  (it) => {
    it.effect("loads an old file's choice under the new names, then saves only those", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile(
          JSON.stringify({
            autoUpdateServer: true,
            autoUpdateQuietCommand: "/usr/local/bin/quiet",
            enableAgentBrowserAccess: false,
          }),
        );
        assert.isTrue(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "/usr/local/bin/quiet");
        assert.isFalse(settings.enableAgentBrowserAccess);

        // Any write saves the new names; the old keys are gone, so a later load reads the same.
        const service = yield* ServerSettingsModule.ServerSettingsService;
        yield* service.updateSettings({ enableAgentBrowserAccess: true });
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        const persisted = JSON.parse(yield* fs.readFileString(config.settingsPath));
        assert.isTrue(persisted.automaticUpdates);
        assert.equal(persisted.automaticUpdatesQuietCommand, "/usr/local/bin/quiet");
        assert.notProperty(persisted, "autoUpdateServer");
        assert.notProperty(persisted, "autoUpdateQuietCommand");
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("keeps a new name already in the file over the old one", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile(
          JSON.stringify({
            autoUpdateServer: true,
            automaticUpdates: false,
            autoUpdateQuietCommand: "/old/quiet",
            automaticUpdatesQuietCommand: "/new/quiet",
          }),
        );
        assert.isFalse(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "/new/quiet");
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("drops an old value of the wrong type without resetting the other settings", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile(
          JSON.stringify({
            autoUpdateServer: "yes",
            autoUpdateQuietCommand: 7,
            enableAgentBrowserAccess: false,
          }),
        );
        assert.isFalse(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "");
        assert.isFalse(settings.enableAgentBrowserAccess);
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("stays off with no settings file", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile(undefined);
        assert.isFalse(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "");
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("stays off with an empty settings object", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile("{}");
        assert.isFalse(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "");
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("stays off when the file was cut off half-way through an old key", () =>
      Effect.gen(function* () {
        const settings = yield* loadSettingsFile('{ "autoUpdateServer": tr');
        assert.isFalse(settings.automaticUpdates);
        assert.equal(settings.automaticUpdatesQuietCommand, "");
      }).pipe(Effect.provide(layerServerSettings())),
    );
  },
);

it("passes through what is not a settings object, and an object without old keys unchanged", () => {
  for (const value of [null, 3, "text", [true]]) {
    assert.equal(renameRetiredAutoUpdateKeys(value), value);
  }
  const current = { automaticUpdates: true };
  assert.strictEqual(renameRetiredAutoUpdateKeys(current), current);
});
