import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import { DEFAULT_CLIENT_SETTINGS } from "@t3tools/contracts";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";

import * as DesktopConfig from "../app/DesktopConfig.ts";
import * as DesktopEnvironment from "../app/DesktopEnvironment.ts";
import * as DesktopClientSettings from "./DesktopClientSettings.ts";

// Fork-only. An earlier fork build saved `anchorEnvironmentId` in the client settings file.
// The setting is gone; a file that still holds it must load (an invalid file fails the read),
// keep the user's other choices, and lose the key on the next save.
function layer(baseDir: string) {
  const layerEnvironment = DesktopEnvironment.layer({
    dirname: "/repo/apps/desktop/src",
    homeDirectory: baseDir,
    platform: "darwin",
    processArch: "x64",
    appVersion: "1.2.3",
    appPath: "/repo",
    isPackaged: true,
    resourcesPath: "/missing/resources",
    runningUnderArm64Translation: false,
  }).pipe(
    Layer.provide(
      Layer.mergeAll(
        NodeServices.layer,
        NodeCrypto.layer,
        DesktopConfig.layerTest({ T3CODE_HOME: baseDir }),
      ),
    ),
  );
  return DesktopClientSettings.layer.pipe(
    Layer.provideMerge(layerEnvironment),
    Layer.provideMerge(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)),
  );
}

const withClientSettings = <A, E, R>(
  effect: Effect.Effect<A, E, R | DesktopClientSettings.DesktopClientSettings>,
) =>
  Effect.gen(function* () {
    const fileSystem = yield* FileSystem.FileSystem;
    const baseDir = yield* fileSystem.makeTempDirectoryScoped({
      prefix: "t3-desktop-client-settings-removed-keys-test-",
    });
    return yield* effect.pipe(Effect.provide(layer(baseDir)));
  }).pipe(Effect.provide(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer)), Effect.scoped);

const weights = { "environment-anchor": 100, "environment-other": 0 };

describe("DesktopClientSettings saved with a removed fork key", () => {
  it.effect.each([
    { label: "direct", wrap: (settings: object) => settings },
    { label: "wrapped", wrap: (settings: object) => ({ settings }) },
  ])("loads a $label document and drops the key on the next save", ({ wrap }) =>
    withClientSettings(
      Effect.gen(function* () {
        const environment = yield* DesktopEnvironment.DesktopEnvironment;
        const fileSystem = yield* FileSystem.FileSystem;
        const settings = yield* DesktopClientSettings.DesktopClientSettings;
        yield* fileSystem.makeDirectory(environment.stateDir, { recursive: true });
        yield* fileSystem.writeFileString(
          environment.clientSettingsPath,
          JSON.stringify(
            wrap({
              anchorEnvironmentId: "environment-anchor",
              loadBalancingEnabled: true,
              loadBalancingWeights: weights,
            }),
          ),
        );

        const persisted = yield* settings.get;
        assert.isTrue(Option.isSome(persisted));
        if (Option.isNone(persisted)) return;
        assert.deepStrictEqual(persisted.value, {
          ...DEFAULT_CLIENT_SETTINGS,
          loadBalancingEnabled: true,
          loadBalancingWeights: weights,
        });

        yield* settings.set(persisted.value);
        const saved = yield* fileSystem.readFileString(environment.clientSettingsPath);
        assert.notInclude(saved, "anchorEnvironmentId");
      }),
    ),
  );
});
