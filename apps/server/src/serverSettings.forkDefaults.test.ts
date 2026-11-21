// Fork: a machine whose settings file says nothing about a fork setting runs it at its pinned
// default (packages/contracts/src/forkDefaults.ts), whatever state the file is in. One exception:
// a file that does not parse may hold an enforced model policy, so the policy fails closed.

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { MODEL_POLICY_FAIL_CLOSED } from "@t3tools/contracts";
import {
  FORK_SETTING_DEFAULTS,
  forkSettingAsSaved,
  pinnedPart,
} from "@t3tools/contracts/forkDefaults";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Logger from "effect/Logger";

import * as ServerSecretStore from "./auth/ServerSecretStore.ts";
import * as ServerConfig from "./config.ts";
import {
  SETTINGS_FILE_UNPARSEABLE,
  SETTINGS_FILE_UNPARSEABLE_WARNING,
} from "./fork/modelPolicyFile.ts";
import * as SqlitePersistence from "./persistence/Sqlite.ts";
import * as ManagedPolicy from "./policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "./serverSettings.ts";

const layerServerSettings = () =>
  ServerSettingsModule.layer.pipe(
    Layer.provide(ServerSecretStore.layer),
    Layer.provide(ManagedPolicy.layerTest()),
    Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
    Layer.provideMerge(
      Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-fork-defaults-test-" })),
    ),
  );

const SERVER_ROWS = FORK_SETTING_DEFAULTS.filter((row) => row.scope === "server");

/** Each server fork setting as loaded, in the file's form, cut to the part its row pins. */
const loadedForkSettings = (contents: string | undefined) =>
  Effect.gen(function* () {
    const config = yield* ServerConfig.ServerConfig;
    const fs = yield* FileSystem.FileSystem;
    if (contents !== undefined) yield* fs.writeFileString(config.settingsPath, contents);
    const settings = yield* (yield* ServerSettingsModule.ServerSettingsService).getSettings;
    const values = settings as unknown as Readonly<Record<string, unknown>>;
    return Object.fromEntries(
      SERVER_ROWS.map((row) => [
        row.key,
        pinnedPart(row, forkSettingAsSaved(row, values[row.key])),
      ]),
    );
  });

const PINNED = Object.fromEntries(SERVER_ROWS.map((row) => [row.key, row.default]));

/** As PINNED, but the model policy fails closed (fork/modelPolicyFile.ts). */
const UNPARSEABLE = Object.fromEntries(
  SERVER_ROWS.map((row) => [
    row.key,
    row.key === "modelPolicy"
      ? pinnedPart(
          row,
          forkSettingAsSaved(row, {
            ...MODEL_POLICY_FAIL_CLOSED,
            unreadable: SETTINGS_FILE_UNPARSEABLE,
          }),
        )
      : row.default,
  ]),
);

const STATES: ReadonlyArray<{
  readonly state: string;
  readonly contents: string | undefined;
  readonly expected?: Readonly<Record<string, unknown>>;
}> = [
  { state: "no settings file", contents: undefined },
  { state: "an empty settings object", contents: "{}" },
  // Upstream writes only values that differ from its defaults, and its automatic updates default
  // on: a machine that ran an upstream build in between has no key and comes back off.
  {
    state: "a file an upstream build wrote",
    contents: JSON.stringify({ enableAgentBrowserAccess: false }),
  },
  { state: "a file cut off half-way", contents: '{ "automaticUpdates": tr', expected: UNPARSEABLE },
  {
    state: "a file with a value of the wrong type",
    contents: JSON.stringify({ automaticUpdates: "yes" }),
  },
];

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork setting defaults", (it) => {
  it.effect.each(STATES)(
    "loads every fork setting at its pinned default with $state",
    ({ contents, expected }) =>
      Effect.gen(function* () {
        assert.deepStrictEqual(yield* loadedForkSettings(contents), expected ?? PINNED);
      }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect.each([
    { state: "a file cut off half-way", contents: '{ "automaticUpdates": tr', warned: 1 },
    { state: "an empty file", contents: "", warned: 1 },
    {
      state: "a file with a value of the wrong type",
      contents: '{ "automaticUpdates": "yes" }',
      warned: 0,
    },
    { state: "an empty settings object", contents: "{}", warned: 0 },
  ])("the model policy warning for $state reaches the server's logger", ({ contents, warned }) => {
    const logs: Array<{ readonly level: string; readonly message: unknown }> = [];
    // The server's logger, under which the settings service is built and read.
    const serverLogger = Logger.layer(
      [Logger.make(({ logLevel, message }) => logs.push({ level: logLevel, message }))],
      { mergeWithExisting: false },
    );
    const isWarning = (log: (typeof logs)[number]) =>
      log.level === "Warn" &&
      JSON.stringify(log.message).includes(SETTINGS_FILE_UNPARSEABLE_WARNING);
    return Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(config.settingsPath, contents);
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.getSettings;
      assert.lengthOf(logs.filter(isWarning), warned);
      // Read again: the same answer, from the same load.
      yield* service.getSettings;
      assert.lengthOf(logs.filter(isWarning), warned);
    }).pipe(Effect.provide(layerServerSettings().pipe(Layer.provideMerge(serverLogger))));
  });

  it.effect("serves no settings at all when the file cannot be read", () =>
    Effect.gen(function* () {
      // A directory where the file should be cannot be read by anyone, root included.
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.makeDirectory(config.settingsPath, { recursive: true });
      const service = yield* ServerSettingsModule.ServerSettingsService;
      // The update loop reads its switch from here; a failed read is a tick that does nothing
      // (autoUpdate.test.ts: "does nothing while the settings cannot be read").
      assert.isTrue(Exit.isFailure(yield* Effect.exit(service.getSettings)));
    }).pipe(Effect.provide(layerServerSettings())),
  );
});
