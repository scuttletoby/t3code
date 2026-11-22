// Fork: the fork settings keep their behaviour when they move into forkSettings.ts (FS0). A file
// that sets every fork setting loads, takes one patch per patch rule and is written back exactly
// as it was before the move. The expected files were captured on the code before the move.

import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  DEFAULT_MODEL_POLICY,
  type ModelPolicy,
  modelPolicyStance,
  ProjectId,
  ServerSettingsError,
  ServerSettingsPatch,
} from "@t3tools/contracts";
import {
  clearProjectSettingsOverridesWithForkKeys,
  ForkSettingsWriteError,
} from "@t3tools/shared/forkSettingsPatch";
import * as HostProcess from "@t3tools/shared/HostProcess";
import { resolveProjectSettings } from "@t3tools/shared/projectSettings";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "./auth/ServerSecretStore.ts";
import * as ServerConfig from "./config.ts";
import * as ForkModelPolicyCheck from "./fork/modelPolicyCheck.ts";
import * as ForkModelPolicyFile from "./fork/modelPolicyFile.ts";
import * as SqlitePersistence from "./persistence/Sqlite.ts";
import * as ManagedPolicy from "./policy/ManagedPolicy.ts";
import * as PlaytestKit from "./playtest/PlaytestService.testkit.ts";
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

/** The model policy check's refusal of an allowed-looking agent pick under this policy. */
const refusalOf = (policy: ModelPolicy) => {
  const answer = ForkModelPolicyCheck.answerPick(policy, "delegate_task", {
    driver: "claudeAgent",
    model: "m",
    options: [{ id: "effort", value: "high" }],
  });
  return "refuse" in answer ? answer.refuse : "";
};

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

  it.effect("refuses deleting a profile a thread still picks, and writes nothing", () =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      const start = {
        ...FULL_FILE,
        mcpProfiles: { lean: { servers: [], t3Tools: [] } },
        mcpThreadProfiles: { t1: "lean" },
      };
      yield* fs.writeFileString(config.settingsPath, JSON.stringify(start));
      const service = yield* ServerSettingsModule.ServerSettingsService;
      // A second client, or one with stale settings, deletes it without clearing the pick.
      const error = yield* service
        .updateSettings(decodePatch({ mcpProfiles: { lean: null } }))
        .pipe(Effect.flip);
      assert.instanceOf(error.cause, ForkSettingsWriteError);
      assert.strictEqual(
        (error.cause as Error).message,
        'MCP profile "lean" cannot be deleted: it is in use by the pick for thread t1. Change those first.',
      );
      const kept = (yield* readFile) as { mcpProfiles: Record<string, unknown> };
      assert.deepStrictEqual(Object.keys(kept.mcpProfiles), ["lean"]);
      // Cleared in the same save, it goes; the same save again changes nothing and is taken.
      const both = decodePatch({ mcpProfiles: { lean: null }, mcpThreadProfiles: { t1: null } });
      yield* service.updateSettings(both);
      yield* service.updateSettings(both);
      const file = (yield* readFile) as Record<string, unknown>;
      assert.isFalse("mcpProfiles" in file && Object.keys(file.mcpProfiles as object).length > 0);
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

  it.effect("clears a project's MCP profile when the screen resets it with the fork's helper", () =>
    Effect.gen(function* () {
      const start = {
        ...FULL_FILE,
        mcpProfile: "t3-only",
        projectSettingsOverrides: { [PROJECT]: { mcpProfile: "all", defaultAutoPull: true } },
      };
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(config.settingsPath, JSON.stringify(start));
      const settings = yield* service.getSettings;
      // What the client sends for "reset to inherited" on the project's MCP profile.
      const entry = clearProjectSettingsOverridesWithForkKeys(settings, ProjectId.make(PROJECT), [
        "mcpProfile",
      ]);
      yield* service.updateSettings(
        decodePatch({ projectSettingsOverrides: { [PROJECT]: entry } }),
      );
      const file = (yield* readFile) as { projectSettingsOverrides: Record<string, unknown> };
      assert.deepStrictEqual(file.projectSettingsOverrides[PROJECT], { defaultAutoPull: true });
    }).pipe(Effect.provide(layerServerSettings())),
  );
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork playtest settings", (it) => {
  const ADAPTER = {
    enabled: true,
    claim: { argv: ["/usr/local/bin/game", "claim"] },
    renew: { argv: ["/usr/local/bin/game", "renew"] },
    push: [{ name: "deploy", argv: ["/usr/local/bin/game", "deploy"], timeoutSeconds: 300 }],
    release: { argv: ["/usr/local/bin/game", "release"] },
  };
  const savedStep = (argv: ReadonlyArray<string>, fields: Record<string, unknown> = {}) => ({
    argv,
    timeoutSeconds: 120,
    description: "",
    ...fields,
  });
  /** ADAPTER as the server saves it: steps whole, limits equal to their defaults left out. */
  const SAVED_ADAPTER = {
    enabled: true,
    claim: savedStep(["/usr/local/bin/game", "claim"]),
    renew: savedStep(["/usr/local/bin/game", "renew"]),
    push: [
      { name: "deploy", ...savedStep(["/usr/local/bin/game", "deploy"], { timeoutSeconds: 300 }) },
    ],
    release: savedStep(["/usr/local/bin/game", "release"]),
  };
  /** As read back: the limits the file leaves out (they are the defaults) filled in. */
  const READ_ADAPTER = { ...SAVED_ADAPTER, renewMinutes: 10, idleMinutes: 90, maxHours: 6 };

  it.effect("saves an adapter, and the same save again leaves the same file", () =>
    Effect.gen(function* () {
      const once = yield* loadThenPatch(FULL_FILE, { playtest: ADAPTER });
      assert.deepStrictEqual(once, { ...SAVED_FULL_FILE, playtest: SAVED_ADAPTER });
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.updateSettings(decodePatch({ playtest: ADAPTER }));
      assert.deepStrictEqual(yield* readFile, once);
      assert.deepStrictEqual((yield* service.getSettings).playtest, READ_ADAPTER);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("writes nothing for playtest while it is off and the file never had it", () =>
    Effect.gen(function* () {
      const file = yield* loadThenPatch({}, { playtest: { enabled: false } });
      assert.isFalse("playtest" in (file as Record<string, unknown>));
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("replaces a saved adapter whole: a step left out of the save is gone", () =>
    Effect.gen(function* () {
      const file = yield* loadThenPatch(
        { ...FULL_FILE, playtest: ADAPTER },
        { playtest: { claim: ADAPTER.claim } },
      );
      const playtest = (file as { playtest?: Record<string, unknown> }).playtest;
      assert.deepStrictEqual(Object.keys(playtest ?? {}).toSorted(), ["claim"]);
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.isFalse(settings.playtest.enabled);
      assert.isUndefined(settings.playtest.renew);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("reads a bad file value as off, keeps the rest, and the next save drops it", () =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      // Turned on by hand without a release step, and a project with a relative program.
      const bad = { ...ADAPTER, release: undefined };
      yield* fs.writeFileString(
        config.settingsPath,
        JSON.stringify({
          ...FULL_FILE,
          playtest: bad,
          projectSettingsOverrides: {
            [PROJECT]: { playtest: { ...ADAPTER, claim: { argv: ["scripts/claim"] } } },
          },
        }),
      );
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.isFalse(settings.playtest.enabled);
      assert.isFalse(
        settings.projectSettingsOverrides[ProjectId.make(PROJECT)]?.playtest?.enabled ?? true,
      );
      assert.strictEqual(settings.enableAgentBrowserAccess, false);
      assert.strictEqual(settings.waitFor.maxPerThread, 7);
      yield* service.updateSettings(decodePatch({ enableAgentBrowserAccess: true }));
      const file = (yield* readFile) as Record<string, unknown>;
      assert.isFalse("playtest" in file);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("refuses a program that is not a full path on the server's OS", () =>
    Effect.gen(function* () {
      // Relative to the worktree there: `/…` is drive-relative on Windows, `C:\…` a file name elsewhere.
      for (const [platform, program] of [
        ["win32", "/opt/game"],
        ["linux", "C:\\game"],
      ] as const) {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        yield* fs.writeFileString(config.settingsPath, JSON.stringify(FULL_FILE));
        const service = yield* ServerSettingsModule.ServerSettingsService;
        const error = yield* service
          .updateSettings(
            decodePatch({ playtest: { ...ADAPTER, claim: { argv: [program, "claim"] } } }),
          )
          .pipe(Effect.provideService(HostProcess.Platform, platform), Effect.flip);
        assert.instanceOf(error.cause, ForkSettingsWriteError, platform);
        assert.include((error.cause as Error).message, "not a full path on this server's OS");
      }
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect(
    "keeps a project off after every save: it never falls back to the server's adapter",
    () =>
      Effect.gen(function* () {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        // The server's adapter is on; the project's value is bad (a relative program), so it is off.
        yield* fs.writeFileString(
          config.settingsPath,
          JSON.stringify({
            ...FULL_FILE,
            playtest: ADAPTER,
            projectSettingsOverrides: {
              [PROJECT]: { playtest: { ...ADAPTER, claim: { argv: ["scripts/claim"] } } },
            },
          }),
        );
        const service = yield* ServerSettingsModule.ServerSettingsService;
        const projectPlaytest = Effect.gen(function* () {
          const file = (yield* readFile) as {
            projectSettingsOverrides: Record<string, { playtest?: { enabled: boolean } }>;
          };
          const read = (yield* service.getSettings).projectSettingsOverrides[
            ProjectId.make(PROJECT)
          ];
          return {
            file: file.projectSettingsOverrides[PROJECT]?.playtest?.enabled,
            read: read?.playtest?.enabled,
          };
        });
        // An unrelated save writes the project's value back as an explicit off.
        yield* service.updateSettings(decodePatch({ enableAgentBrowserAccess: true }));
        assert.deepStrictEqual(yield* projectPlaytest, { file: false, read: false });
        // A client that does not know `playtest` saves the project's entry without it.
        yield* service.updateSettings(
          decodePatch({ projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: true } } }),
        );
        assert.deepStrictEqual(yield* projectPlaytest, { file: false, read: false });
        assert.isTrue((yield* service.getSettings).playtest.enabled);
      }).pipe(Effect.provide(layerServerSettings())),
  );
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork handoff settings", (it) => {
  const ON = { mode: "handoff", threads: { "thread-1": "compact" }, sizeTokens: 120_000 };

  it.effect("saves a value, and the same save again leaves the same file", () =>
    Effect.gen(function* () {
      const once = yield* loadThenPatch(FULL_FILE, { handoff: ON });
      assert.deepStrictEqual((once as { handoff?: unknown }).handoff, ON);
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.updateSettings(decodePatch({ handoff: ON }));
      assert.deepStrictEqual(yield* readFile, once);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("reads a hand-edited or newer value as off, keeps the rest, and a save drops it", () =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(
        config.settingsPath,
        JSON.stringify({
          ...FULL_FILE,
          handoff: { ...ON, fromANewerBuild: true },
          projectSettingsOverrides: { [PROJECT]: { handoff: { mode: "always" } } },
        }),
      );
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.strictEqual(settings.handoff.mode, "off");
      assert.strictEqual(
        settings.projectSettingsOverrides[ProjectId.make(PROJECT)]?.handoff?.mode,
        "off",
      );
      assert.strictEqual(settings.waitFor.maxPerThread, 7);
      yield* service.updateSettings(decodePatch({ enableAgentBrowserAccess: true }));
      assert.isFalse("handoff" in ((yield* readFile) as Record<string, unknown>));
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("keeps a project's value when a client that does not know the key saves", () =>
    Effect.gen(function* () {
      const file = yield* loadThenPatch(
        { ...FULL_FILE, projectSettingsOverrides: { [PROJECT]: { handoff: ON } } },
        { projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: true } } },
      );
      const project = (file as { projectSettingsOverrides: Record<string, unknown> })
        .projectSettingsOverrides[PROJECT];
      // Kept whole; a project's entry is written with its defaults filled in.
      assert.deepInclude((project as { handoff?: object }).handoff, ON);
      // `null` clears it: the project then uses the server's.
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.updateSettings(
        decodePatch({
          projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: true, handoff: null } },
        }),
      );
      const read = (yield* service.getSettings).projectSettingsOverrides[ProjectId.make(PROJECT)];
      assert.isUndefined(read?.handoff);
    }).pipe(Effect.provide(layerServerSettings())),
  );
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork review rules", (it) => {
  const ON = { preset: "recommended" };
  const CUSTOM = {
    preset: "recommended",
    runner: { driver: "codex", model: "gpt-review", effort: "medium" },
    minors: "issue",
  };

  it.effect(
    "writes nothing for reviewRules when the file never had it, or held an empty value",
    () =>
      Effect.gen(function* () {
        for (const start of [{}, { reviewRules: {} }]) {
          const file = yield* loadThenPatch(start, { enableAgentBrowserAccess: false });
          assert.isFalse("reviewRules" in (file as Record<string, unknown>), JSON.stringify(start));
          const service = yield* ServerSettingsModule.ServerSettingsService;
          assert.deepStrictEqual((yield* service.getSettings).reviewRules, { preset: "off" });
        }
      }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("saves a value whole, and the same save again leaves the same file", () =>
    Effect.gen(function* () {
      const once = yield* loadThenPatch({ ...FULL_FILE, reviewRules: CUSTOM }, { reviewRules: ON });
      assert.deepStrictEqual((once as Record<string, unknown>).reviewRules, ON);
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.updateSettings(decodePatch({ reviewRules: ON }));
      assert.deepStrictEqual(yield* readFile, once);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("keeps a bad hand edit as written through saves of other keys, read as off", () =>
    Effect.gen(function* () {
      const bad = { preset: "strict", runner: { driver: "claudeAgent" } };
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(
        config.settingsPath,
        JSON.stringify({
          ...FULL_FILE,
          reviewRules: bad,
          projectSettingsOverrides: { [PROJECT]: { reviewRules: bad, defaultAutoPull: false } },
        }),
      );
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.strictEqual(settings.reviewRules.preset, "off");
      assert.strictEqual(settings.waitFor.maxPerThread, 7);
      // Saved twice: the second save starts from what the first one wrote. The project's entry is
      // resent whole, as a client does when it saves the project's other settings.
      for (const enableAgentBrowserAccess of [true, false]) {
        const entry = (yield* service.getSettings).projectSettingsOverrides[
          ProjectId.make(PROJECT)
        ];
        yield* service.updateSettings(
          decodePatch({
            enableAgentBrowserAccess,
            projectSettingsOverrides: { [PROJECT]: { ...entry, defaultAutoPull: true } },
          }),
        );
        const file = (yield* readFile) as {
          reviewRules?: unknown;
          projectSettingsOverrides: Record<string, { reviewRules?: unknown }>;
        };
        assert.deepStrictEqual(file.reviewRules, bad);
        assert.deepStrictEqual(file.projectSettingsOverrides[PROJECT]?.reviewRules, bad);
        const read = yield* service.getSettings;
        assert.strictEqual(read.reviewRules.preset, "off");
        assert.strictEqual(
          read.projectSettingsOverrides[ProjectId.make(PROJECT)]?.reviewRules?.preset,
          "off",
        );
      }
      // The person's own save of the key replaces it.
      yield* service.updateSettings(decodePatch({ reviewRules: ON }));
      assert.deepStrictEqual(((yield* readFile) as Record<string, unknown>).reviewRules, ON);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("keeps a project's value when a client that does not know the key saves", () =>
    Effect.gen(function* () {
      const file = yield* loadThenPatch(
        {
          ...FULL_FILE,
          projectSettingsOverrides: { [PROJECT]: { reviewRules: { preset: "off" } } },
        },
        { projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: true } } },
      );
      const project = (file as { projectSettingsOverrides: Record<string, unknown> })
        .projectSettingsOverrides[PROJECT];
      assert.deepStrictEqual(project, { defaultAutoPull: true, reviewRules: { preset: "off" } });
      // `null` clears it: the project then uses the server's.
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service.updateSettings(
        decodePatch({
          projectSettingsOverrides: { [PROJECT]: { defaultAutoPull: true, reviewRules: null } },
        }),
      );
      const read = (yield* service.getSettings).projectSettingsOverrides[ProjectId.make(PROJECT)];
      assert.isUndefined(read?.reviewRules);
    }).pipe(Effect.provide(layerServerSettings())),
  );
});

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))("fork model policy", (it) => {
  const ROW = { driver: "claudeAgent", model: "m", efforts: ["low", "high"] };

  it.effect.each([
    { name: "a bad mode", bad: { mode: "Enforce", allow: [ROW] } },
    { name: "a misspelt row key", bad: { mode: "advise", allow: [{ ...ROW, effort: ["low"] }] } },
  ])("keeps $name as written when another key is saved, read as nothing allowed", ({ bad }) =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      yield* fs.writeFileString(
        config.settingsPath,
        JSON.stringify({ ...FULL_FILE, modelPolicy: bad }),
      );
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const settings = yield* service.getSettings;
      assert.strictEqual(settings.modelPolicy.mode, "enforce");
      assert.deepStrictEqual(settings.modelPolicy.allow, []);
      // Saved twice: the second save starts from what the first one wrote.
      for (const enableAgentBrowserAccess of [true, false]) {
        yield* service.updateSettings(decodePatch({ enableAgentBrowserAccess }));
        const file = (yield* readFile) as Record<string, unknown>;
        assert.deepStrictEqual(file.modelPolicy, bad);
        assert.deepStrictEqual((yield* service.getSettings).modelPolicy.allow, []);
      }
      // The person's own save of the key replaces it.
      const good = { mode: "advise", allow: [ROW] };
      yield* service.updateSettings(decodePatch({ modelPolicy: good }));
      assert.deepStrictEqual(((yield* readFile) as Record<string, unknown>).modelPolicy, good);
      assert.deepStrictEqual((yield* service.getSettings).modelPolicy as unknown, good);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("writes nothing for modelPolicy when the file never had it", () =>
    Effect.gen(function* () {
      const file = yield* loadThenPatch({}, { enableAgentBrowserAccess: false });
      assert.isFalse("modelPolicy" in (file as Record<string, unknown>));
    }).pipe(Effect.provide(layerServerSettings())),
  );
  /** The policy a load gives for this file text, read twice (runs again: the same answer). */
  const loadPolicy = (text: string | undefined) =>
    Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      if (text !== undefined) yield* fs.writeFileString(config.settingsPath, text);
      const service = yield* ServerSettingsModule.ServerSettingsService;
      const first = (yield* service.getSettings).modelPolicy;
      assert.deepStrictEqual((yield* service.getSettings).modelPolicy, first);
      return first;
    });

  it.effect.each([
    { name: "broken JSON", text: `{ "modelPolicy": { "mode": "off", "allow": [] }, ` },
    { name: "an empty file", text: "" },
    { name: "a file that is no object", text: `["advise"]` },
  ])("$name: the policy cannot be read, so it fails closed (never off)", ({ text }) =>
    Effect.gen(function* () {
      const policy = yield* loadPolicy(text);
      assert.deepStrictEqual(policy, {
        mode: "enforce",
        allow: [],
        unreadable: ForkModelPolicyFile.SETTINGS_FILE_UNPARSEABLE,
      });
      assert.strictEqual(modelPolicyStance(policy).mode, "enforce");
      assert.include(
        refusalOf(policy),
        "a settings file that does not parse), so every agent pick is refused",
      );
      // Nothing is written back: the save is refused while the file does not load.
      const service = yield* ServerSettingsModule.ServerSettingsService;
      yield* service
        .updateSettings(decodePatch({ enableAgentBrowserAccess: true }))
        .pipe(Effect.flip);
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      assert.strictEqual(yield* fs.readFileString(config.settingsPath), text);
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("a file that parses but fails elsewhere keeps its own policy", () =>
    Effect.gen(function* () {
      const policy = { mode: "advise", allow: [ROW] };
      const bad = { enableAgentBrowserAccess: "yes" };
      assert.deepStrictEqual(
        (yield* loadPolicy(JSON.stringify({ ...bad, modelPolicy: policy }))) as unknown,
        policy,
      );
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("a file that fails elsewhere with a bad policy reads it as a bad value reads", () =>
    Effect.gen(function* () {
      const raw = { mode: "Enforce", allow: [ROW] };
      const policy = yield* loadPolicy(
        JSON.stringify({ enableAgentBrowserAccess: "yes", modelPolicy: raw }),
      );
      assert.deepStrictEqual(policy, { mode: "enforce", allow: [], unreadable: raw });
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("old and missing state: a failing file with no policy, or no file, is off", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(
        yield* loadPolicy(JSON.stringify({ enableAgentBrowserAccess: "yes" })),
        DEFAULT_MODEL_POLICY,
      );
    }).pipe(Effect.provide(layerServerSettings())),
  );

  it.effect("missing state: no settings file at all is off", () =>
    Effect.gen(function* () {
      assert.deepStrictEqual(yield* loadPolicy(undefined), DEFAULT_MODEL_POLICY);
    }).pipe(Effect.provide(layerServerSettings())),
  );
});

describe.skipIf(HostProcess.Platform.defaultValue() === "win32")(
  "fork playtest with a project's hand-written null",
  () => {
    it.live("uses the server's adapter for that project: a session starts and ends", () =>
      PlaytestKit.live(
        Effect.gen(function* () {
          const fx = yield* PlaytestKit.fixture;
          const dir = yield* fx.adapterDir("a");
          const config = yield* ServerConfig.ServerConfig;
          const fs = yield* FileSystem.FileSystem;
          // An old file: the project's playtest hand-written as `null` (once read as off).
          yield* fs.writeFileString(
            config.settingsPath,
            JSON.stringify({
              playtest: PlaytestKit.adapter(fx, dir),
              projectSettingsOverrides: {
                [PlaytestKit.PROJECT]: { playtest: null, defaultAutoPull: false },
              },
            }),
          );
          const settings = yield* ServerSettingsModule.ServerSettingsService;
          const loaded = yield* settings.getSettings;
          const resolved = resolveProjectSettings(loaded, PlaytestKit.PROJECT);
          assert.strictEqual(resolved.sources.playtest, "environment");
          assert.deepStrictEqual(resolved.settings.playtest, loaded.playtest);

          const service = yield* PlaytestKit.makeService(fx, settings);
          const started = yield* service.start({ threadId: PlaytestKit.THREAD, target: "dev-1" });
          assert.strictEqual(started.outcome, "started");
          const ended = yield* service.end({ threadId: PlaytestKit.THREAD, reason: "agent" });
          assert.strictEqual(ended.outcome, "ended");
          assert.deepStrictEqual(
            (yield* PlaytestKit.ran(dir)).filter((line) => !line.startsWith("finished")),
            ["claim:0", "release:0"],
          );

          // A save of another key writes the entry without the `null`; still the server's.
          yield* settings.updateSettings(decodePatch({ enableAgentBrowserAccess: false }));
          const file = (yield* readFile) as { projectSettingsOverrides: Record<string, unknown> };
          assert.deepStrictEqual(file.projectSettingsOverrides[PlaytestKit.PROJECT], {
            defaultAutoPull: false,
          });
          const after = resolveProjectSettings(yield* settings.getSettings, PlaytestKit.PROJECT);
          assert.strictEqual(after.sources.playtest, "environment");
        }).pipe(Effect.provide(layerServerSettings().pipe(Layer.provide(NodeCrypto.layer)))),
      ),
    );
  },
);
