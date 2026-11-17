// Fork: interval (Duration) settings. Every one, found by walking the settings schema, survives a
// patch and a save and load (#45: upstream's patch merge and save each list them by hand). A save
// refuses an interval no run could use, and a file that holds one loads with that interval at its
// default instead of failing whole (#46).

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  DEFAULT_SERVER_SETTINGS,
  ServerSettings,
  ServerSettingsError,
  ServerSettingsPatch,
} from "@t3tools/contracts";
import {
  findDurationPaths,
  ForkSettingsWriteError,
  SETTINGS_INTERVAL_PATHS,
} from "@t3tools/shared/forkSettingsPatch";
import { applyServerSettingsPatch } from "@t3tools/shared/serverSettings";
import { assert, it } from "@effect/vitest";
import * as Duration from "effect/Duration";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "./auth/ServerSecretStore.ts";
import * as ServerConfig from "./config.ts";
import * as SqlitePersistence from "./persistence/Sqlite.ts";
import * as ManagedPolicy from "./policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "./serverSettings.ts";

const layerServerSettings = () =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
    Layer.provideMerge(
      Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-fork-intervals-test-" })),
    ),
  );

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const getPath = (value: unknown, path: ReadonlyArray<string>): unknown =>
  path.reduce<unknown>((node, key) => (isRecord(node) ? node[key] : undefined), value);

const setPath = (value: unknown, path: ReadonlyArray<string>, leaf: unknown): unknown => {
  const [key, ...rest] = path;
  if (key === undefined) return leaf;
  const node = isRecord(value) ? value : {};
  return { ...node, [key]: setPath(node[key], rest, leaf) };
};

const millisAt = (settings: ServerSettings, path: ReadonlyArray<string>): number | undefined => {
  const value = getPath(settings, path);
  return Duration.isDuration(value) ? Duration.toMillis(value) : undefined;
};

/** What else a patch needs for an interval under this top key to apply. */
const PATCH_CONTEXT: Readonly<Record<string, unknown>> = {
  // Overrides apply only to a custom profile.
  backgroundActivity: { profile: "custom" },
};

/** A patch, as a client sends it, that sets the interval at `path` to `millis`. */
const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

const patchSetting = (path: ReadonlyArray<string>, millis: number): ServerSettingsPatch =>
  decodePatch(setPath({ [path[0]!]: PATCH_CONTEXT[path[0]!] }, path, millis));

const readFileText = Effect.gen(function* () {
  const config = yield* ServerConfig.ServerConfig;
  const fs = yield* FileSystem.FileSystem;
  return yield* fs.readFileString(config.settingsPath);
});

const writeFileText = (text: string) =>
  Effect.gen(function* () {
    const config = yield* ServerConfig.ServerConfig;
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(config.settingsPath, text);
  });

const decodeFile = (text: string): ServerSettings => {
  const decoded = ServerSettingsModule.decodeSettingsFileText(text);
  if (Exit.isFailure(decoded)) assert.fail(`the file does not decode: ${text}`);
  return decoded.value;
};

const SET_MILLIS = 123_456;
const pathName = (path: ReadonlyArray<string>) => path.join(".");

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "interval settings found from the schema (#45)",
  (it) => {
    it("finds every interval, each at a key path", () => {
      const { paths, outOfReach } = findDurationPaths(ServerSettings);
      // An interval inside a record or array has no fixed path for the checks below to follow.
      assert.deepStrictEqual(outOfReach, []);
      for (const known of [
        "automaticGitFetchInterval",
        "providerHealthRefreshInterval",
        "backgroundActivity.overrides.hostPowerMonitorIdleInterval",
        "headlessUsageRefreshInterval",
      ]) {
        assert.include(paths.map(pathName), known);
      }
      // No default is the value set below, so each check sees a change.
      for (const path of paths)
        assert.notStrictEqual(millisAt(DEFAULT_SERVER_SETTINGS, path), SET_MILLIS);
    });

    it.each(SETTINGS_INTERVAL_PATHS.map((path) => ({ name: pathName(path), path })))(
      "keeps $name through a patch",
      ({ path }) => {
        const patched = applyServerSettingsPatch(
          DEFAULT_SERVER_SETTINGS,
          patchSetting(path, SET_MILLIS),
        );
        assert.strictEqual(millisAt(patched, path), SET_MILLIS);
      },
    );

    it.effect.each(SETTINGS_INTERVAL_PATHS.map((path) => ({ name: pathName(path), path })))(
      "keeps $name through a save and the next load",
      ({ path }) =>
        Effect.gen(function* () {
          yield* writeFileText("{}");
          const service = yield* ServerSettingsModule.ServerSettingsService;
          const saved = yield* service.updateSettings(patchSetting(path, SET_MILLIS));
          assert.strictEqual(millisAt(saved, path), SET_MILLIS);
          const text = yield* readFileText;
          assert.strictEqual(getPath(JSON.parse(text), path), SET_MILLIS);
          assert.strictEqual(millisAt(decodeFile(text), path), SET_MILLIS);
        }).pipe(Effect.provide(layerServerSettings())),
    );
  },
);

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "an interval no run could use (#46)",
  (it) => {
    const REFUSED = [
      // A client's JSON can say 1e400, which parses as Infinity and would be saved as null.
      {
        name: "1e400",
        path: ["providerHealthRefreshInterval"],
        millis: JSON.parse("1e400") as number,
      },
      { name: "a negative value", path: ["headlessUsageRefreshInterval"], millis: -5 },
      {
        name: "a value past the largest",
        path: ["backgroundActivity", "overrides", "hostPowerMonitorActiveInterval"],
        millis: 1e300,
      },
    ];

    it.effect.each(REFUSED)(
      "refuses a save of $name and leaves the file as it was",
      ({ path, millis }) =>
        Effect.gen(function* () {
          yield* writeFileText('{"enableAgentBrowserAccess": false}');
          const service = yield* ServerSettingsModule.ServerSettingsService;
          yield* service.getSettings;
          const patch = patchSetting(path, millis);
          const failure = yield* Effect.flip(service.updateSettings(patch));
          assert.instanceOf(failure, ServerSettingsError);
          assert.instanceOf(failure.cause, ForkSettingsWriteError);
          assert.include((failure.cause as ForkSettingsWriteError).problem, pathName(path));
          assert.strictEqual(yield* readFileText, '{"enableAgentBrowserAccess": false}');
        }).pipe(Effect.provide(layerServerSettings())),
    );

    // As a hand edit or an older server's save of 1e400 leaves it.
    const BAD_FILE = `{
    "enableAgentBrowserAccess": false,
    "automaticGitFetchInterval": null,
    "headlessUsageRefreshInterval": "10m",
    "backgroundActivity": {
      "profile": "custom",
      "overrides": {
        "providerHealthRefreshInterval": 90000,
        "hostPowerMonitorActiveInterval": -5,
        "hostPowerMonitorIdleInterval": 1e400
      }
    }
  }`;

    it.effect(
      "loads a file holding bad intervals with those at their defaults and the rest kept",
      () =>
        Effect.gen(function* () {
          yield* writeFileText(BAD_FILE);
          const service = yield* ServerSettingsModule.ServerSettingsService;
          const loaded = yield* service.getSettings;
          // The rest of the file is read, not reset to every default.
          assert.strictEqual(loaded.enableAgentBrowserAccess, false);
          assert.strictEqual(
            millisAt(loaded, ["backgroundActivity", "overrides", "providerHealthRefreshInterval"]),
            90_000,
          );
          for (const path of [
            ["headlessUsageRefreshInterval"],
            ["backgroundActivity", "overrides", "hostPowerMonitorActiveInterval"],
            ["backgroundActivity", "overrides", "hostPowerMonitorIdleInterval"],
          ]) {
            assert.strictEqual(
              millisAt(loaded, path),
              millisAt(DEFAULT_SERVER_SETTINGS, path),
              pathName(path),
            );
          }

          // Loaded again before any save (a start that stopped before saving): the same result.
          assert.deepStrictEqual(decodeFile(BAD_FILE), decodeFile(BAD_FILE));
          assert.strictEqual(decodeFile(BAD_FILE).enableAgentBrowserAccess, false);

          // A save is not refused, and leaves a file with no bad interval that loads as it was saved.
          yield* service.updateSettings({ enableAgentBrowserAccess: true });
          const text = yield* readFileText;
          const file = JSON.parse(text) as unknown;
          assert.notInclude(text, "null");
          assert.isUndefined(getPath(file, ["headlessUsageRefreshInterval"]));
          assert.isUndefined(
            getPath(file, ["backgroundActivity", "overrides", "hostPowerMonitorIdleInterval"]),
          );
          assert.strictEqual(
            getPath(file, ["backgroundActivity", "overrides", "providerHealthRefreshInterval"]),
            90_000,
          );
          const reloaded = decodeFile(text);
          assert.strictEqual(reloaded.enableAgentBrowserAccess, true);
          assert.strictEqual(
            millisAt(reloaded, [
              "backgroundActivity",
              "overrides",
              "providerHealthRefreshInterval",
            ]),
            90_000,
          );
        }).pipe(Effect.provide(layerServerSettings())),
    );
  },
);
