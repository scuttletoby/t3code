// Fork: the fork settings keep their behaviour when they move into forkSettings.ts (FS0). A file
// that sets every fork setting loads, takes one patch per patch rule and is written back exactly
// as it was before the move. The expected files were captured on the code before the move.

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { ServerSettingsError, ServerSettingsPatch } from "@t3tools/contracts";
import { ForkSettingsWriteError } from "@t3tools/shared/forkSettingsPatch";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
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
      Layer.fresh(ServerConfig.layerTest(process.cwd(), { prefix: "t3code-fork-settings-test-" })),
    ),
  );

const PROJECT = "project-1";

/** A settings file that sets every fork setting away from its default, and one upstream key. */
const FULL_FILE = {
  enableAgentBrowserAccess: false,
  automaticUpdates: true,
  automaticUpdatesQuietCommand: "/usr/local/bin/quiet-check",
  usagePacing: {
    "claude-a": { reservePercent: 20, burnFirst: true, noLimits: false },
    "codex-b": { reservePercent: 5, burnFirst: false, noLimits: true },
  },
  usagePacingGate: "advise",
  usagePacingLowerEffort: { high: "medium", xhigh: "high" },
  waitFor: { enabled: true, maxPerThread: 7, prSweepSeconds: 60 },
  waitChecks: {
    "ci-green": { argv: ["/bin/true"], maxArgs: 1 },
    "build-done": { argv: ["/bin/true"], intervalSeconds: 60 },
    // Dropped on read (bad name), so the next save leaves it out.
    "Bad Name": { argv: ["/bin/true"] },
  },
  agentSlots: { enabled: true, maxPerMachine: 4, maxPerAccount: { "claude-a": 2, "codex-b": 1 } },
  headlessUsageRefreshInterval: 300_000,
  projectSettingsOverrides: {
    [PROJECT]: { waitChecks: { "lint-ok": { argv: ["/bin/true"] } } },
    "project-2": { waitChecks: {} },
  },
};

const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

const readFile = Effect.gen(function* () {
  const config = yield* ServerConfig.ServerConfig;
  const fs = yield* FileSystem.FileSystem;
  return JSON.parse(yield* fs.readFileString(config.settingsPath)) as unknown;
});

const loadThenPatch = (start: unknown, json: unknown) =>
  Effect.gen(function* () {
    const patch = decodePatch(json);
    const config = yield* ServerConfig.ServerConfig;
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(config.settingsPath, JSON.stringify(start));
    const service = yield* ServerSettingsModule.ServerSettingsService;
    yield* service.getSettings;
    yield* service.updateSettings(patch);
    return yield* readFile;
  });

// Each case: the file on disk (FULL_FILE unless named), a patch as a client sends it, and the file
// the save leaves (captured before the move).
const ARG_PATTERN = "^[A-Za-z0-9._:/][A-Za-z0-9._:/@=+-]{0,199}$";
/** A check as the server saves it: with every default written out. */
const savedCheck = (argv: ReadonlyArray<string>, fields: Record<string, unknown> = {}) => ({
  argv,
  maxArgs: 0,
  argPattern: ARG_PATTERN,
  intervalSeconds: 30,
  timeoutSeconds: 20,
  description: "",
  ...fields,
});
const without = (file: Record<string, unknown>, ...keys: ReadonlyArray<string>) =>
  Object.fromEntries(Object.entries(file).filter(([key]) => !keys.includes(key)));

/** FULL_FILE as the build before FS0 saved it (captured from 17ead270c1), pinned. */
const SAVED_FULL_FILE: Record<string, unknown> = {
  automaticUpdates: true,
  automaticUpdatesQuietCommand: "/usr/local/bin/quiet-check",
  enableAgentBrowserAccess: false,
  usagePacing: {
    "claude-a": { reservePercent: 20, burnFirst: true, noLimits: false },
    "codex-b": { reservePercent: 5, burnFirst: false, noLimits: true },
  },
  usagePacingGate: "advise",
  usagePacingLowerEffort: { high: "medium", xhigh: "high" },
  waitFor: { enabled: true, maxPerThread: 7, prSweepSeconds: 60 },
  waitChecks: {
    "ci-green": savedCheck(["/bin/true"], { maxArgs: 1 }),
    "build-done": savedCheck(["/bin/true"], { intervalSeconds: 60 }),
  },
  agentSlots: { enabled: true, maxPerMachine: 4, maxPerAccount: { "claude-a": 2, "codex-b": 1 } },
  projectSettingsOverrides: {
    [PROJECT]: { waitChecks: { "lint-ok": savedCheck(["/bin/true"]) } },
    "project-2": { waitChecks: {} },
  },
  idleCompaction: "onReturn",
  headlessUsageRefreshInterval: 300_000,
};

const CASES: ReadonlyArray<{
  readonly name: string;
  readonly start?: unknown;
  readonly patch: unknown;
  readonly file: unknown;
}> = [
  // An older client that knows no fork setting sends only upstream keys.
  {
    name: "an upstream patch",
    patch: { enableAgentBrowserAccess: true },
    file: without(SAVED_FULL_FILE, "enableAgentBrowserAccess"),
  },
  { name: "an empty patch", patch: {}, file: SAVED_FULL_FILE },
  {
    name: "a patch on an empty file",
    start: {},
    patch: { waitFor: { enabled: true }, usagePacing: { "claude-a": { reservePercent: 10 } } },
    file: {
      usagePacing: { "claude-a": { reservePercent: 10, burnFirst: false, noLimits: false } },
      waitFor: { enabled: true },
      idleCompaction: "onReturn",
    },
  },
  {
    name: "a patch on a file with the old names",
    start: { autoUpdateServer: true, autoUpdateQuietCommand: "/usr/local/bin/quiet-check" },
    patch: { usagePacingGate: "advise" },
    file: {
      automaticUpdates: true,
      automaticUpdatesQuietCommand: "/usr/local/bin/quiet-check",
      usagePacingGate: "advise",
      idleCompaction: "onReturn",
    },
  },
  {
    name: "replaced values",
    patch: {
      automaticUpdates: false,
      automaticUpdatesQuietCommand: "",
      usagePacingGate: "enforce",
      usagePacingLowerEffort: { high: "low" },
      waitChecks: { "only-one": { argv: ["/bin/false"] } },
      headlessUsageRefreshInterval: 0,
    },
    file: {
      ...without(SAVED_FULL_FILE, "automaticUpdates", "automaticUpdatesQuietCommand"),
      usagePacingGate: "enforce",
      usagePacingLowerEffort: { high: "low" },
      waitChecks: { "only-one": savedCheck(["/bin/false"]) },
      headlessUsageRefreshInterval: 0,
    },
  },
  {
    name: "merged fields",
    patch: { waitFor: { maxPerThread: 9 }, agentSlots: { maxPerAccount: { "claude-a": 3 } } },
    file: {
      ...SAVED_FULL_FILE,
      waitFor: { enabled: true, maxPerThread: 9, prSweepSeconds: 60 },
      agentSlots: { enabled: true, maxPerMachine: 4, maxPerAccount: { "claude-a": 3 } },
    },
  },
  {
    name: "merged entries",
    patch: {
      usagePacing: {
        "claude-a": { burnFirst: false },
        "codex-b": null,
        "new-c": { reservePercent: 10 },
      },
    },
    file: {
      ...SAVED_FULL_FILE,
      usagePacing: {
        "claude-a": { reservePercent: 20, burnFirst: false, noLimits: false },
        "new-c": { reservePercent: 10, burnFirst: false, noLimits: false },
      },
    },
  },
  {
    name: "project overrides",
    patch: {
      projectSettingsOverrides: {
        [PROJECT]: { waitChecks: { "lint-ok": { argv: ["/bin/false"] } } },
        "project-2": null,
      },
    },
    file: {
      ...SAVED_FULL_FILE,
      projectSettingsOverrides: {
        [PROJECT]: { waitChecks: { "lint-ok": savedCheck(["/bin/false"]) } },
      },
    },
  },
];

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork settings round trip", (it) => {
  it.effect.each(CASES)("writes the same file after $name", ({ start, patch, file }) =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* loadThenPatch(start ?? FULL_FILE, patch), file);
    }).pipe(Effect.provide(layerServerSettings())),
  );
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork MCP profile keys", (it) => {
  it.effect("keeps every other setting when an MCP key in the file holds a bad value", () =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(
        config.settingsPath,
        JSON.stringify({ ...FULL_FILE, mcpThreadProfiles: { t1: null }, mcpProfile: 5 }),
      );
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.strictEqual(settings.enableAgentBrowserAccess, false);
      assert.strictEqual(settings.usagePacingGate, "advise");
      assert.strictEqual(settings.agentSlots.maxPerMachine, 4);
      assert.deepStrictEqual(Object.keys(settings.waitChecks).toSorted(), [
        "build-done",
        "ci-green",
      ]);
      // The unset pick is dropped; the bad default reads as a name no profile can have, so the
      // resolver refuses it instead of widening it to "all".
      assert.deepStrictEqual(settings.mcpThreadProfiles, {});
      assert.strictEqual(settings.mcpProfile, "(unreadable number)");
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect(
    "refuses a save that names a profile that is not set up, with the fix as its cause",
    () =>
      Effect.gen(function* () {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        yield* fs.writeFileString(config.settingsPath, JSON.stringify(FULL_FILE));
        const service = yield* ServerSettingsModule.ServerSettingsService;
        const error = yield* service
          .updateSettings(decodePatch({ mcpProfile: "lean" }))
          .pipe(Effect.flip);
        const reason =
          'mcpProfile names MCP profile "lean", which is not set up: add that profile first, or pick "all" or "t3-only".';
        // Upstream's error type, unchanged; the reason is its cause, a tagged error.
        assert.strictEqual(error.operation, "normalize");
        assert.instanceOf(error.cause, ForkSettingsWriteError);
        assert.strictEqual((error.cause as ForkSettingsWriteError)._tag, "ForkSettingsWriteError");
        assert.strictEqual((error.cause as Error).message, reason);
        // The reason reaches a client: the error as the RPC sends it and decodes it.
        const json = Schema.toCodecJson(ServerSettingsError);
        const received = yield* Schema.decodeEffect(json)(yield* Schema.encodeEffect(json)(error));
        assert.deepInclude(received.cause as object, {
          name: "ForkSettingsWriteError",
          message: reason,
        });
        // Nothing was written: the file still has no MCP keys.
        const file = (yield* readFile) as Record<string, unknown>;
        assert.isFalse("mcpProfile" in file);
      }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("keeps a project's MCP profile when an older client saves the project without it", () =>
    Effect.gen(function* () {
      const start = {
        ...FULL_FILE,
        mcpProfiles: { lean: { servers: [], t3Tools: [] } },
        projectSettingsOverrides: {
          [PROJECT]: { mcpProfile: "lean", mcpSubagentProfile: "t3-only" },
        },
      };
      // A client that does not know the MCP keys sends the project's entry without them.
      const file = (yield* loadThenPatch(start, {
        projectSettingsOverrides: { [PROJECT]: { enableAgentBrowserAccess: true } },
      })) as { projectSettingsOverrides: Record<string, unknown> };
      assert.deepStrictEqual(file.projectSettingsOverrides[PROJECT], {
        enableAgentBrowserAccess: true,
        mcpProfile: "lean",
        mcpSubagentProfile: "t3-only",
      });
    }).pipe(Effect.provide(layerServerSettings())),
  );
});
