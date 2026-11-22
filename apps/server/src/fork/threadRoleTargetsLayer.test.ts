// Fork (calm sidebar): the real thread lookup for the `threadRoles` save check
// (threadRoleTargetsLayer.ts) over upstream's orchestrator, reached through the settings service as
// a client's save reaches it. Only the orchestrator's `getThreadShell` is a stand-in.
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  type OrchestrationV2ThreadShell,
  ServerSettingsError,
  ServerSettingsPatch,
  type ThreadId,
} from "@t3tools/contracts";
import { ForkSettingsWriteError } from "@t3tools/shared/forkSettingsPatch";
import { assert, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";
import { ThreadRoleTargets } from "./threadRoleTargets.ts";
import * as ThreadRoleTargetsLayer from "./threadRoleTargetsLayer.ts";

/** Only the fields the lookup reads; the rest of a shell is never touched. */
const shell = (
  projectId: string,
  extra: Partial<Pick<OrchestrationV2ThreadShell, "lineage">> & { delegatedFrom?: unknown } = {},
): OrchestrationV2ThreadShell =>
  ({
    projectId,
    lineage: {
      parentThreadId: null,
      relationshipToParent: null,
      rootThreadId: "root" as ThreadId,
    },
    ...extra,
  }) as unknown as OrchestrationV2ThreadShell;

const SHELLS: Record<string, OrchestrationV2ThreadShell> = {
  "thread-top": shell("project-1"),
  // An MCP-launched thread can carry `delegatedFrom: null` in memory; it is not delegated.
  "thread-mcp-launched": shell("project-1", { delegatedFrom: null }),
  // A "fork" lineage thread is listed, as upstream lists it.
  "thread-forked": shell("project-1", {
    lineage: {
      parentThreadId: "thread-top" as ThreadId,
      relationshipToParent: "fork",
      rootThreadId: "thread-top" as ThreadId,
    },
  }),
  "thread-other-project": shell("project-2"),
  "thread-subagent": shell("project-1", {
    lineage: {
      parentThreadId: "thread-top" as ThreadId,
      relationshipToParent: "subagent",
      rootThreadId: "thread-top" as ThreadId,
    },
  }),
  "thread-side-chat": shell("project-1", {
    lineage: {
      parentThreadId: "thread-top" as ThreadId,
      relationshipToParent: null,
      rootThreadId: "thread-top" as ThreadId,
      sideChat: true,
    },
  }),
  "thread-delegated": shell("project-1", {
    delegatedFrom: { environmentId: "env-1", threadId: "thread-x", title: "Task" },
  }),
};

const orchestrator = Layer.mock(Orchestrator.OrchestratorV2)({
  getThreadShell: (threadId: ThreadId) => Effect.succeed(SHELLS[threadId] ?? null),
});

/** The real lookup layer, built over the stand-in orchestrator. */
const realTargets = ThreadRoleTargetsLayer.layer.pipe(Layer.provide(orchestrator));

it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "threadRoles save check over the real thread lookup",
  (it) => {
    const layerServerSettings = () =>
      ServerSettingsModule.layer.pipe(
        Layer.provide(ServerSecretStore.layer),
        Layer.provide(ManagedPolicy.layerTest()),
        Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
        Layer.provideMerge(
          Layer.fresh(
            ServerConfig.layerTest(process.cwd(), { prefix: "t3code-thread-roles-layer-test-" }),
          ),
        ),
      );
    const withLookup = () => Layer.merge(layerServerSettings(), realTargets);

    const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

    const readFile = Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      return JSON.parse(yield* fs.readFileString(config.settingsPath)) as Record<string, unknown>;
    });

    const loadThenSave = (patch: unknown) =>
      Effect.gen(function* () {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        yield* fs.writeFileString(config.settingsPath, "{}");
        const service = yield* ServerSettingsModule.ServerSettingsService;
        yield* service.getSettings;
        return yield* service.updateSettings(decodePatch(patch));
      });

    const refusal = (patch: unknown) =>
      Effect.gen(function* () {
        const error = yield* loadThenSave(patch).pipe(Effect.flip);
        assert.instanceOf(error, ServerSettingsError);
        assert.instanceOf(error.cause, ForkSettingsWriteError);
        assert.deepStrictEqual(yield* readFile, {});
        return (error.cause as Error).message;
      });

    it.effect("maps each shell as the check needs it", () =>
      Effect.gen(function* () {
        const targets = yield* ThreadRoleTargets;
        const find = (threadId: string) => targets.find(threadId as ThreadId);
        assert.isNull(yield* find("thread-gone"));
        assert.deepStrictEqual(yield* find("thread-top"), {
          projectId: "project-1",
          listed: true,
          delegated: false,
        });
        assert.deepStrictEqual(yield* find("thread-mcp-launched"), {
          projectId: "project-1",
          listed: true,
          delegated: false,
        });
        assert.isTrue((yield* find("thread-forked"))?.listed);
        assert.isFalse((yield* find("thread-subagent"))?.listed);
        assert.isFalse((yield* find("thread-side-chat"))?.listed);
        assert.isTrue((yield* find("thread-delegated"))?.delegated);
        assert.strictEqual((yield* find("thread-other-project"))?.projectId, "project-2");
      }).pipe(Effect.provide(realTargets)),
    );

    it.effect("saves a top-level thread of the project as its orchestrator", () =>
      Effect.gen(function* () {
        const saved = yield* loadThenSave({
          threadRoles: {
            assistant: "thread-mcp-launched",
            orchestrators: { "project-1": "thread-top" },
          },
        });
        assert.deepStrictEqual<unknown>(saved.threadRoles, {
          assistant: "thread-mcp-launched",
          orchestrators: { "project-1": "thread-top" },
        });
        assert.deepStrictEqual((yield* readFile).threadRoles, {
          assistant: "thread-mcp-launched",
          orchestrators: { "project-1": "thread-top" },
        });
      }).pipe(Effect.provide(withLookup())),
    );

    it.effect("refuses a sub-agent, a side chat and a delegated task, and writes nothing", () =>
      Effect.gen(function* () {
        assert.match(
          yield* refusal({ threadRoles: { orchestrators: { "project-1": "thread-subagent" } } }),
          /sub-agent or side chat/u,
        );
        assert.match(
          yield* refusal({ threadRoles: { assistant: "thread-side-chat" } }),
          /sub-agent or side chat/u,
        );
        assert.match(
          yield* refusal({ threadRoles: { assistant: "thread-delegated" } }),
          /delegated from a linked machine/u,
        );
      }).pipe(Effect.provide(withLookup())),
    );

    it.effect("refuses another project's thread and a missing one, and writes nothing", () =>
      Effect.gen(function* () {
        assert.match(
          yield* refusal({
            threadRoles: { orchestrators: { "project-1": "thread-other-project" } },
          }),
          /belongs to project project-2/u,
        );
        assert.match(
          yield* refusal({ threadRoles: { assistant: "thread-gone" } }),
          /does not exist or is deleted/u,
        );
      }).pipe(Effect.provide(withLookup())),
    );
  },
);
