// Fork (calm sidebar): the server's save check for `threadRoles`. Each refusal, the pointers it
// leaves alone, and that it fails closed where threads cannot be read.
import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import {
  DEFAULT_SERVER_SETTINGS,
  ServerSettingsError,
  ServerSettingsPatch,
  type ThreadId,
  type ThreadRoles,
} from "@t3tools/contracts";
import { ForkSettingsWriteError } from "@t3tools/shared/forkSettingsPatch";
import { assert, describe, it } from "@effect/vitest";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Schema from "effect/Schema";

import * as ServerSecretStore from "../auth/ServerSecretStore.ts";
import * as ServerConfig from "../config.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ManagedPolicy from "../policy/ManagedPolicy.ts";
import * as ServerSettingsModule from "../serverSettings.ts";

import {
  newThreadRolePointers,
  type ThreadRoleTarget,
  ThreadRoleTargets,
  threadRolesSaveProblem,
} from "./threadRoleTargets.ts";

class DatabaseBusy extends Schema.TaggedError<DatabaseBusy>()("DatabaseBusy", {}) {}

const id = (value: string) => value as ThreadId;
const roles = (
  assistant: string | null,
  orchestrators: Record<string, string> = {},
): { readonly threadRoles: ThreadRoles } => ({
  threadRoles: {
    assistant: assistant === null ? null : id(assistant),
    orchestrators: orchestrators as ThreadRoles["orchestrators"],
  },
});
const NONE = roles(null);

const top = (projectId: string): ThreadRoleTarget => ({
  projectId,
  listed: true,
  delegated: false,
});

/** Threads by id; a missing id is a thread that does not exist or is deleted. */
const THREADS: Record<string, ThreadRoleTarget> = {
  "thread-top": top("project-1"),
  "thread-other-project": top("project-2"),
  "thread-subagent": { ...top("project-1"), listed: false },
  "thread-delegated": { ...top("project-1"), delegated: true },
};

const targets = (reads: Array<string> = []) =>
  Layer.succeed(
    ThreadRoleTargets,
    ThreadRoleTargets.of({
      find: (threadId) =>
        Effect.sync(() => {
          reads.push(threadId);
          return THREADS[threadId] ?? null;
        }),
    }),
  );

const problemOf = (
  current: { readonly threadRoles: ThreadRoles },
  next: { readonly threadRoles: ThreadRoles },
) => threadRolesSaveProblem(current, next).pipe(Effect.provide(targets()));

describe("threadRoles save check", () => {
  it("is off by default: the default settings set no pointer", () => {
    assert.deepStrictEqual(
      newThreadRolePointers(
        DEFAULT_SERVER_SETTINGS.threadRoles,
        DEFAULT_SERVER_SETTINGS.threadRoles,
      ),
      [],
    );
  });

  it.effect("lets a top-level thread take the assistant role and its project's orchestrator", () =>
    Effect.gen(function* () {
      assert.isUndefined(yield* problemOf(NONE, roles("thread-top")));
      assert.isUndefined(yield* problemOf(NONE, roles(null, { "project-1": "thread-top" })));
      assert.isUndefined(yield* problemOf(NONE, roles("thread-other-project")));
    }),
  );

  it.effect("refuses a thread that does not exist or is deleted", () =>
    Effect.gen(function* () {
      const problem = yield* problemOf(NONE, roles("thread-gone"));
      assert.match(
        problem ?? "",
        /threadRoles\.assistant names thread thread-gone, which does not exist or is deleted/u,
      );
    }),
  );

  it.effect("refuses a sub-agent or side chat", () =>
    Effect.gen(function* () {
      const problem = yield* problemOf(NONE, roles(null, { "project-1": "thread-subagent" }));
      assert.match(
        problem ?? "",
        /threadRoles\.orchestrators\["project-1"\] names thread thread-subagent, which is a sub-agent or side chat/u,
      );
    }),
  );

  it.effect("refuses a task delegated from a linked machine", () =>
    Effect.gen(function* () {
      const problem = yield* problemOf(NONE, roles("thread-delegated"));
      assert.match(problem ?? "", /which is a task delegated from a linked machine/u);
    }),
  );

  it.effect("refuses an orchestrator from another project", () =>
    Effect.gen(function* () {
      const problem = yield* problemOf(NONE, roles(null, { "project-1": "thread-other-project" }));
      assert.match(
        problem ?? "",
        /which belongs to project project-2: a project's orchestrator must be one of its own threads/u,
      );
    }),
  );

  it.effect("never refuses or reads a pointer the save leaves as it was, nor a removal", () =>
    Effect.gen(function* () {
      const reads: Array<string> = [];
      // An old pointer to a thread deleted since, kept by a save that changes something else.
      const old = roles("thread-gone", { "project-1": "thread-gone" });
      const keep = threadRolesSaveProblem(old, old).pipe(Effect.provide(targets(reads)));
      assert.isUndefined(yield* keep);
      const remove = threadRolesSaveProblem(old, roles(null)).pipe(Effect.provide(targets(reads)));
      assert.isUndefined(yield* remove);
      // Adding another project reads only the new thread.
      const add = threadRolesSaveProblem(
        old,
        roles("thread-gone", { "project-1": "thread-gone", "project-2": "thread-other-project" }),
      ).pipe(Effect.provide(targets(reads)));
      assert.isUndefined(yield* add);
      assert.deepStrictEqual(reads, ["thread-other-project"]);
    }),
  );

  it.effect("fails closed where threads cannot be read", () =>
    Effect.gen(function* () {
      // No ThreadRoleTargets service at all.
      const problem = yield* threadRolesSaveProblem(NONE, roles("thread-top"));
      assert.match(problem ?? "", /cannot be checked here/u);
      // A save that sets no new pointer needs no service.
      assert.isUndefined(yield* threadRolesSaveProblem(NONE, NONE));
    }),
  );

  it.effect("refuses when a thread read fails", () =>
    Effect.gen(function* () {
      const failing = Layer.succeed(
        ThreadRoleTargets,
        ThreadRoleTargets.of({ find: () => Effect.fail(new DatabaseBusy()) }),
      );
      const problem = yield* threadRolesSaveProblem(NONE, roles("thread-top")).pipe(
        Effect.provide(failing),
      );
      assert.match(
        problem ?? "",
        /thread thread-top could not be read, so the role was not saved/u,
      );
    }),
  );
});

// Through the settings service, as a client's save reaches it: a refusal writes nothing.
it.layer(Layer.mergeAll(NodeServices.layer, NodeCrypto.layer))(
  "threadRoles saves through the settings service",
  (it) => {
    const layerServerSettings = () =>
      ServerSettingsModule.layer.pipe(
        Layer.provide(ServerSecretStore.layer),
        Layer.provide(ManagedPolicy.layerTest()),
        Layer.provideMerge(Layer.fresh(SqlitePersistence.layerMemory)),
        Layer.provideMerge(
          Layer.fresh(
            ServerConfig.layerTest(process.cwd(), { prefix: "t3code-thread-roles-test-" }),
          ),
        ),
      );

    const decodePatch = Schema.decodeUnknownSync(ServerSettingsPatch);

    const readFile = Effect.gen(function* () {
      const config = yield* ServerConfig.ServerConfig;
      const fs = yield* FileSystem.FileSystem;
      return JSON.parse(yield* fs.readFileString(config.settingsPath)) as Record<string, unknown>;
    });

    /** Writes `start` as the settings file, loads it, then saves `patch`. */
    const loadThenSave = (start: unknown, patch: unknown) =>
      Effect.gen(function* () {
        const config = yield* ServerConfig.ServerConfig;
        const fs = yield* FileSystem.FileSystem;
        yield* fs.writeFileString(config.settingsPath, JSON.stringify(start));
        const service = yield* ServerSettingsModule.ServerSettingsService;
        yield* service.getSettings;
        return yield* service.updateSettings(decodePatch(patch));
      });

    it.effect("loads an old file with no roles and saves other settings as before", () =>
      Effect.gen(function* () {
        const saved = yield* loadThenSave(
          { enableAgentBrowserAccess: false },
          { enableAgentBrowserAccess: true },
        );
        assert.deepStrictEqual(saved.threadRoles, { assistant: null, orchestrators: {} });
        assert.isFalse("threadRoles" in (yield* readFile));
      }).pipe(Effect.provide(Layer.merge(layerServerSettings(), targets()))),
    );

    it.effect("saves a role for a thread that can hold it", () =>
      Effect.gen(function* () {
        const saved = yield* loadThenSave(
          {},
          { threadRoles: { orchestrators: { "project-1": "thread-top" } } },
        );
        assert.deepStrictEqual<unknown>(saved.threadRoles, {
          assistant: null,
          orchestrators: { "project-1": "thread-top" },
        });
        // Saved sparse, as every setting is: the assistant at its default is left out.
        assert.deepStrictEqual((yield* readFile).threadRoles, {
          orchestrators: { "project-1": "thread-top" },
        });
      }).pipe(Effect.provide(Layer.merge(layerServerSettings(), targets()))),
    );

    it.effect("refuses a role for a sub-agent, and writes nothing", () =>
      Effect.gen(function* () {
        const error = yield* loadThenSave(
          { enableAgentBrowserAccess: false },
          { threadRoles: { assistant: "thread-subagent" }, enableAgentBrowserAccess: true },
        ).pipe(Effect.flip);
        assert.instanceOf(error, ServerSettingsError);
        assert.strictEqual(error.operation, "normalize");
        assert.instanceOf(error.cause, ForkSettingsWriteError);
        assert.match((error.cause as Error).message, /sub-agent or side chat/u);
        assert.deepStrictEqual(yield* readFile, { enableAgentBrowserAccess: false });
      }).pipe(Effect.provide(Layer.merge(layerServerSettings(), targets()))),
    );

    it.effect("refuses a new role where the server cannot read threads", () =>
      Effect.gen(function* () {
        const error = yield* loadThenSave({}, { threadRoles: { assistant: "thread-top" } }).pipe(
          Effect.flip,
        );
        assert.instanceOf(error.cause, ForkSettingsWriteError);
        assert.match((error.cause as Error).message, /cannot be checked here/u);
      }).pipe(Effect.provide(layerServerSettings())),
    );
  },
);
