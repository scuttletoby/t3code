// Fork (issue 1156): `lanes` saves go through the real settings service, its sparse file write
// (values equal to the default left out) and the next load's decode, as a restart reads them.
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { ServerSettings, ServerSettingsPatch, laneDefinitions } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "../../auth/ServerSecretStore.ts";
import * as ServerConfig from "../../config.ts";
import * as SqlitePersistence from "../../persistence/Sqlite.ts";
import * as ManagedPolicy from "../../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../../serverSettings.ts";

const layerServerSettings = () =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
    Layer.provideMerge(
      Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-lanes-settings-test-" })),
    ),
  );

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);
const decodeFile = Schema.decodeUnknownSync(ServerSettings);

const ops = { description: "Machines and CI.", owns: ["issue"], driver: "agent" };

/** Saves each patch in turn from `start`, then reads the lanes the file on disk gives a restart. */
const saveThenRestart = (start: unknown, ...patches: ReadonlyArray<unknown>) =>
  Effect.gen(function* () {
    const config = yield* ServerConfig.ServerConfig;
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(config.settingsPath, JSON.stringify(start));
    const service = yield* ServerSettingsModule.ServerSettingsService;
    yield* service.getSettings;
    for (const patch of patches) yield* service.updateSettings(decodePatch(patch));
    const live = laneDefinitions((yield* service.getSettings).lanes);
    const file = JSON.parse(yield* fs.readFileString(config.settingsPath)) as unknown;
    return { live, file, restarted: laneDefinitions(decodeFile(file).lanes) };
  });

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "lanes saves survive the settings file",
  (it) => {
    it.effect("keeps the built-in lanes after a restart when a lane is added", () =>
      Effect.gen(function* () {
        const { live, restarted } = yield* saveThenRestart({}, { lanes: { definitions: { ops } } });
        assert.deepStrictEqual(Object.keys(restarted).toSorted(), ["issues", "ops", "pr-queue"]);
        assert.deepStrictEqual(restarted, live);
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("keeps a removed built-in lane removed after a restart", () =>
      Effect.gen(function* () {
        const { live, restarted } = yield* saveThenRestart(
          {},
          { lanes: { definitions: { issues: false } } },
        );
        assert.deepStrictEqual(Object.keys(restarted), ["pr-queue"]);
        assert.deepStrictEqual(restarted, live);
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("saves an edited built-in lane whole, and null gives the built-in back", () =>
      Effect.gen(function* () {
        const mine = { description: "Mine.", owns: ["pr"] as const };
        const edited = yield* saveThenRestart({}, { lanes: { definitions: { "pr-queue": mine } } });
        assert.deepStrictEqual(edited.restarted["pr-queue"], { ...mine, driver: "agent" });
        assert.deepStrictEqual(edited.restarted, edited.live);

        const reset = yield* saveThenRestart(edited.file, {
          lanes: { definitions: { "pr-queue": null } },
        });
        assert.notStrictEqual(reset.restarted["pr-queue"]?.description, "Mine.");
        assert.deepStrictEqual(Object.keys(reset.restarted).toSorted(), ["issues", "pr-queue"]);
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("keeps both of two saves for different lanes, and the switch, after a restart", () =>
      Effect.gen(function* () {
        const { restarted, file } = yield* saveThenRestart(
          {},
          { lanes: { enabled: true, definitions: { ops } } },
          { lanes: { definitions: { issues: false } } },
        );
        assert.deepStrictEqual(Object.keys(restarted).toSorted(), ["ops", "pr-queue"]);
        assert.isTrue(decodeFile(file).lanes.enabled);
      }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect(
      "writes nothing for lanes while they are off and only the built-ins are defined",
      () =>
        Effect.gen(function* () {
          const { file } = yield* saveThenRestart({}, { lanes: { enabled: false } });
          assert.isFalse("lanes" in (file as Record<string, unknown>));
        }).pipe(Effect.provide(layerServerSettings())),
    );

    it.effect("refuses a removal marker for a lane that is not built in, and writes nothing", () =>
      Effect.gen(function* () {
        const exit = yield* Effect.exit(
          saveThenRestart({}, { lanes: { definitions: { ops: false } } }),
        );
        assert.isTrue(Exit.isFailure(exit));
      }).pipe(Effect.provide(layerServerSettings())),
    );
  },
);
