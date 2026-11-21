/**
 * Fork (placement row P2): the server reads `placement.factsCommand` against its own OS when it
 * places work (`readPlacementForThisOs`). A path of another OS in the file reads there as off, kept
 * as written with its problem, on load and after every save; a save that sets one is refused. The
 * OS is injected (`HostProcess.Platform`), so each case runs on any machine.
 */
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, it } from "@effect/vitest";
import { DEFAULT_PLACEMENT_SETTINGS, ServerSettings } from "@t3tools/contracts";
import * as HostProcess from "@t3tools/shared/HostProcess";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import { readPlacementForThisOs } from "./placementRead.ts";

const decodeServerSettingsJson = Schema.decodeUnknownEffect(Schema.fromJsonString(ServerSettings));

const layerOn = (platform: NodeJS.Platform) =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
    Layer.provideMerge(
      Layer.fresh(
        ServerConfig.layerTest(process.cwd(), { prefix: "t3code-placement-settings-test-" }),
      ),
    ),
    Layer.provideMerge(Layer.succeed(HostProcess.Platform, platform)),
  );

const PLACEMENT = {
  mode: "enforce" as const,
  labels: ["gpu"],
  never: [["ci-runner", "owner-logins"]],
};

// [the server's OS, the factsCommand of another OS in the file, one of its own]
const CASES = [
  ["linux", "C:\\facts.exe", "/usr/local/bin/facts"],
  ["darwin", "C:\\facts.exe", "/usr/local/bin/facts"],
  ["win32", "/usr/bin/x", "C:\\facts.exe"],
] as const;

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("placement on this OS", (it) => {
  it.effect.each(CASES)(
    "on %s, places with %s as off, kept; refuses saving it",
    ([platform, foreign, own]) =>
      Effect.gen(function* () {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        const service = yield* ServerSettingsModule.ServerSettingsService;
        const written = { ...PLACEMENT, factsCommand: foreign };
        yield* fs.writeFileString(
          config.settingsPath,
          JSON.stringify({ placement: written, defaultAutoPull: false }),
        );
        const placing = () => Effect.flatMap(service.getSettings, readPlacementForThisOs);

        // The load holds the file's value; placing reads it as off, kept, with its problem.
        assert.deepEqual((yield* service.getSettings).placement, { ...written, weights: {} });
        const read = yield* placing();
        const { unreadable, problem, ...rules } = read;
        assert.deepEqual(rules, { ...DEFAULT_PLACEMENT_SETTINGS });
        assert.deepEqual(unreadable, { ...written, weights: {} });
        assert.include(problem, "is not a full path on this server's OS");
        // Reading it again (an off value) gives the same.
        assert.deepEqual(yield* readPlacementForThisOs({ placement: read }), read);

        // A save of another setting writes it back unchanged, and placing still reads it as off
        // (runs again the same).
        for (let run = 0; run < 2; run++) {
          yield* service.updateSettings({ defaultAutoPull: run === 0 });
          const file = JSON.parse(yield* fs.readFileString(config.settingsPath));
          assert.deepEqual(file.placement, written);
          assert.deepEqual(yield* placing(), read);
        }

        // A save that sets this OS's path is taken and read as it is; one that sets another OS's
        // path over it is refused and leaves the file as it was.
        const whole = { ...PLACEMENT, weights: {} };
        const saved = yield* service.updateSettings({
          placement: { ...whole, factsCommand: own },
        });
        assert.strictEqual(saved.placement.factsCommand, own);
        const placed = yield* placing();
        assert.strictEqual(placed.factsCommand, own);
        assert.isFalse(Object.hasOwn(placed, "problem"));
        const refused = yield* Effect.flip(
          service.updateSettings({ placement: { ...whole, factsCommand: foreign } }),
        );
        assert.include(String(refused.cause), "is not a full path on this server's OS");
        const persisted = yield* decodeServerSettingsJson(
          yield* fs.readFileString(config.settingsPath),
        );
        assert.strictEqual(persisted.placement.factsCommand, own);
      }).pipe(Effect.provide(layerOn(platform))),
  );
});
