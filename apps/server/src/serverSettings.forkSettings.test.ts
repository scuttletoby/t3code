// Fork: the fork settings keep their behaviour when they move into forkSettings.ts (FS0). A file
// that sets every fork setting loads, takes one patch per patch rule and is written back exactly
// as it was before the move. The expected files were captured on the code before the move.

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { ServerSettingsPatch } from "@t3tools/contracts";
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
