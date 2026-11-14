/**
 * Fork: `headlessUsageRefreshInterval` through the real settings service: old files, patches,
 * the sparse settings file and restarts.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { DEFAULT_SERVER_SETTINGS, ServerSettings, ServerSettingsPatch } from "@t3tools/contracts";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import { assert, it } from "@effect/vitest";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeSettingsJson = Schema.decodeUnknownEffect(Schema.fromJsonString(ServerSettings));

/** One settings service; each provide is one server run over the same settings file. */
const serverRun = () =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
  );

const minutesOf = (value: Duration.Duration) => Duration.toMillis(value) / 60_000;

it("a patch keeps the interval a whole Duration and leaves it alone when unrelated", () => {
  const set = applyServerSettingsPatch(
    DEFAULT_SERVER_SETTINGS,
    decodePatch({ headlessUsageRefreshInterval: 15 * 60_000 }),
  );
  assert.isTrue(Duration.isDuration(set.headlessUsageRefreshInterval));
  assert.equal(minutesOf(set.headlessUsageRefreshInterval), 15);
  const unrelated = applyServerSettingsPatch(set, decodePatch({ enableAgentBrowserAccess: false }));
  assert.equal(minutesOf(unrelated.headlessUsageRefreshInterval), 15);
  const off = applyServerSettingsPatch(set, decodePatch({ headlessUsageRefreshInterval: 0 }));
  assert.equal(Duration.toMillis(off.headlessUsageRefreshInterval), 0);
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("settings file", (it) => {
  it.effect("an old file gets the default; a saved interval survives a restart, unchanged", () =>
    Effect.gen(function* () {
      const { settingsPath } = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      // Old state: a settings file written before the setting existed.
      yield* fs.writeFileString(settingsPath, `{ "enableAgentBrowserAccess": false }`);
      const first = yield* Effect.gen(function* () {
        const service = yield* ServerSettingsModule.ServerSettingsService;
        assert.equal(minutesOf((yield* service.getSettings).headlessUsageRefreshInterval), 10);
        const saved = yield* service.updateSettings(
          decodePatch({ headlessUsageRefreshInterval: 30 * 60_000 }),
        );
        assert.isTrue(Duration.isDuration(saved.headlessUsageRefreshInterval));
        assert.equal(minutesOf(saved.headlessUsageRefreshInterval), 30);
        // A second, unrelated save keeps it.
        yield* service.updateSettings(decodePatch({ enableAgentBrowserAccess: true }));
        return JSON.parse(yield* fs.readFileString(settingsPath));
      }).pipe(Effect.provide(serverRun()));
      assert.equal(first.headlessUsageRefreshInterval, 30 * 60_000);
      assert.equal(
        minutesOf((yield* decodeSettingsJson(JSON.stringify(first))).headlessUsageRefreshInterval),
        30,
      );

      // A restart reads it back; turning it off and back to the default keeps the file sparse.
      const second = yield* Effect.gen(function* () {
        const service = yield* ServerSettingsModule.ServerSettingsService;
        assert.equal(minutesOf((yield* service.getSettings).headlessUsageRefreshInterval), 30);
        yield* service.updateSettings(decodePatch({ headlessUsageRefreshInterval: 0 }));
        const off = JSON.parse(yield* fs.readFileString(settingsPath));
        yield* service.updateSettings(decodePatch({ headlessUsageRefreshInterval: 10 * 60_000 }));
        return { off, back: JSON.parse(yield* fs.readFileString(settingsPath)) };
      }).pipe(Effect.provide(serverRun()));
      assert.equal(second.off.headlessUsageRefreshInterval, 0);
      assert.notProperty(second.back, "headlessUsageRefreshInterval");
    }).pipe(
      Effect.provide(
        Layer.fresh(
          ServerConfig.layerTest(process.cwd(), { prefix: "t3code-headless-usage-settings-test-" }),
        ),
      ),
    ),
  );
});
