import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, expect, it } from "@effect/vitest";
import {
  EnvironmentId,
  type OrchestrationV2ThreadShell,
  ProjectId,
  ProviderInstanceId,
  ThreadId,
} from "@t3tools/contracts";
import type { WaitRecord } from "@t3tools/contracts/waitFor";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as PlatformError from "effect/PlatformError";
import { McpSchema, McpServer, Tool } from "effect/ai";
import { TestClock } from "effect/testing";

import * as ThreadManagement from "../../../orchestration-v2/ThreadManagementService.ts";
import { ServerSettingsService } from "../../../serverSettings.ts";
import type { WaitDelivery } from "../../../waits/WaitDelivery.ts";
import * as WaitService from "../../../waits/WaitService.ts";
import { Waits } from "../../../waits/WaitWorker.ts";
import { WAIT_STORE_FILE } from "../../../waits/WaitStore.ts";
import * as McpHttpServer from "../../McpHttpServer.ts";
import * as McpInvocationContext from "../../McpInvocationContext.ts";
import * as McpToolAccessTestkit from "../../McpToolAccess.testkit.ts";
import * as McpToolExposure from "../../McpToolExposure.ts";
import { WaitsToolkit } from "./tools.ts";

const NOW = Date.parse("2026-11-14T12:00:00.000Z");
const PROJECT = ProjectId.make("project:mcp-test");
const OTHER_PROJECT = ProjectId.make("project:elsewhere");
const CALLER = ThreadId.make("thread-caller");
const PARENT = ThreadId.make("thread-parent");
const CHILD = ThreadId.make("thread-child");
const SIBLING = ThreadId.make("thread-sibling");
const FOREIGN = ThreadId.make("thread-foreign");
const ON = { waitFor: { enabled: true } } as const;

/** The calling thread: a subagent of PARENT, as the server's own records show it. */
const callerShell = (
  overrides: Partial<OrchestrationV2ThreadShell> = {},
): OrchestrationV2ThreadShell => ({
  ...McpToolAccessTestkit.liveThreadShell(CALLER),
  lineage: { rootThreadId: PARENT, parentThreadId: PARENT, relationshipToParent: "subagent" },
  ...overrides,
});

const threadsLayer = (shells: ReadonlyMap<ThreadId, OrchestrationV2ThreadShell>) =>
  Layer.mock(ThreadManagement.ThreadManagementService)({
    getThreadShell: (threadId) =>
      Effect.succeed(shells.get(threadId) ?? McpToolAccessTestkit.liveThreadShell(threadId)),
  });

/** Never called: no test here lets a wait reach a send. */
const noDelivery: WaitDelivery = {
  receiver: () => Effect.succeed({ type: "ok" }),
  deliver: () => Effect.die("no message is sent in the tool tests"),
};

const scopeOf = (
  overrides: Partial<McpInvocationContext.McpInvocationScope> = {},
): McpInvocationContext.McpInvocationScope => ({
  environmentId: EnvironmentId.make("waits-environment"),
  requestNamespace: "waits-session",
  thread: {
    threadId: CALLER,
    providerSessionId: "waits-session",
    providerInstanceId: ProviderInstanceId.make("codex"),
  },
  client: undefined,
  issuedAt: 0,
  capabilities: new Set(["orchestration"]),
  ...overrides,
});

const client = McpSchema.McpServerClient.of({
  clientId: 1,
  protocolVersion: "2025-06-18",
  clientCapabilities: {},
  clientInfo: { name: "waits", version: "1" },
  initializePayload: {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "waits", version: "1" },
  },
  getClient: Effect.die("unused"),
});

/** The fork's wait tools as the server registers them, over a real wait service and store file. */
const toolsLayer = (input: {
  readonly filePath: string;
  readonly settings?: Parameters<typeof ServerSettingsService.layerTest>[0];
  readonly shells?: ReadonlyMap<ThreadId, OrchestrationV2ThreadShell>;
  readonly fs?: FileSystem.FileSystem;
}) => {
  const base = Layer.mergeAll(
    ServerSettingsService.layerTest(input.settings ?? {}),
    threadsLayer(input.shells ?? new Map([[CALLER, callerShell()]])),
  );
  const made = WaitService.make({
    filePath: input.filePath,
    delivery: noDelivery,
    fileChildJudge: null,
  });
  const waits = Layer.effect(
    Waits,
    input.fs === undefined
      ? made
      : made.pipe(Effect.provideService(FileSystem.FileSystem, input.fs)),
  );
  return McpHttpServer.layerForkTools.pipe(
    Layer.provideMerge(McpHttpServer.layerInProcessMcpServer),
    Layer.provideMerge(waits),
    Layer.provideMerge(base),
  );
};

const call = (
  name: string,
  args: Record<string, unknown>,
  scope: McpInvocationContext.McpInvocationScope = scopeOf(),
) =>
  Effect.gen(function* () {
    const server = yield* McpServer.McpServer;
    return yield* server
      .callTool({ name, arguments: args })
      .pipe(
        Effect.provideService(McpInvocationContext.McpInvocationContext, scope),
        Effect.provideService(McpSchema.McpServerClient, client),
      );
  });

/** A call the MCP layer turns away before any handler runs (an argument the tool does not take). */
const refusedArguments = (
  name: string,
  args: Record<string, unknown>,
  scope?: McpInvocationContext.McpInvocationScope,
) =>
  call(name, args, scope).pipe(
    Effect.flip,
    Effect.map((error) => assert.include(String(error.message), "Unknown argument", name)),
  );

const succeeded = (result: McpSchema.CallToolResult) => {
  assert.notStrictEqual(result.isError, true, JSON.stringify(result.content));
  return result.structuredContent as Record<string, unknown> & {
    readonly waits?: ReadonlyArray<Record<string, unknown>>;
  };
};

const failed = (result: McpSchema.CallToolResult) => {
  const text = result.content[0];
  assert.isTrue(result.isError === true && text?.type === "text", "expected a declared failure");
  return JSON.parse(text?.type === "text" ? text.text : "{}") as {
    readonly code: string;
    readonly message: string;
  };
};

const workspace = Effect.gen(function* () {
  const fs = yield* FileSystem.FileSystem;
  const path = yield* Path.Path;
  const root = yield* fs.makeTempDirectoryScoped({ prefix: "wait-tools-" });
  const stateDir = path.join(root, "state");
  yield* fs.makeDirectory(stateDir);
  return {
    stateDir,
    filePath: path.join(stateDir, WAIT_STORE_FILE),
    watched: (name: string) => path.join(root, name),
  };
});

/** A record as an earlier version wrote it: none of the fields added later. */
const oldRecord = (overrides: Record<string, unknown> = {}) => ({
  id: "w_aaaaaaaaaa",
  threadId: CALLER,
  projectId: PROJECT,
  key: "0123456789abcdef", // gitleaks:allow (a fake test value, not a key)
  createdAt: "2026-11-14T11:00:00.000Z",
  deadlineAt: "2026-11-14T13:00:00.000Z",
  state: "waiting",
  attempt: 0,
  kind: "file",
  spec: { path: "/nowhere/ready", until: "exists" },
  ...overrides,
});

const seed = (filePath: string, contents: unknown) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    yield* fs.writeFileString(
      filePath,
      typeof contents === "string" ? contents : JSON.stringify(contents),
    );
  });

const storedWaits = (filePath: string) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    return JSON.parse(yield* fs.readFileString(filePath)).waits as ReadonlyArray<WaitRecord>;
  });

const exists = (filePath: string) =>
  FileSystem.FileSystem.pipe(Effect.flatMap((fs) => fs.exists(filePath)));

const fileWait = (path: string) => ({ kind: "file", file: { path } });

/** The real file system, recording each call on a path under `root`; renames onto `failRenamesTo` fail while `failing()`. */
const instrumentedFs = (options: {
  readonly root: string;
  readonly failRenamesTo?: string;
  readonly failing?: () => boolean;
}) =>
  Effect.gen(function* () {
    const fs = yield* FileSystem.FileSystem;
    const touched: string[] = [];
    const instrumented = new Proxy(fs, {
      get(target, property, receiver) {
        const value = Reflect.get(target, property, receiver);
        if (typeof value !== "function") return value;
        return (...args: unknown[]) => {
          const first = args[0];
          if (typeof first === "string" && first.startsWith(options.root)) {
            touched.push(`${String(property)} ${first}`);
          }
          if (
            property === "rename" &&
            args[1] === options.failRenamesTo &&
            (options.failing?.() ?? true)
          ) {
            return Effect.fail(
              PlatformError.systemError({
                _tag: "Unknown",
                module: "FileSystem",
                method: "rename",
                description: "disk full",
                pathOrDescriptor: String(args[1]),
              }),
            );
          }
          return value.apply(target, args);
        };
      },
    });
    return { fs: instrumented, touched };
  });

const WRITES =
  /^(writeFile|writeFileString|rename|remove|makeDirectory|copy|copyFile|truncate|open)/u;

it.layer(NodeServices.layer)("wait tools", (it) => {
  it.effect("T19: with waits off no tool creates, changes or repairs the store", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { stateDir, filePath, watched } = yield* workspace;
      const { fs, touched } = yield* instrumentedFs({ root: stateDir });
      yield* Effect.gen(function* () {
        // wait_for refuses before it reads anything.
        expect(failed(yield* call("wait_for", fileWait(watched("ready"))))).toMatchObject({
          code: "waits_off",
        });
        assert.deepStrictEqual(touched, []);

        // list and cancel answer from no file, and make none.
        expect(succeeded(yield* call("wait_list", {}))).toMatchObject({
          enabled: false,
          waits: [],
        });
        expect(succeeded(yield* call("wait_cancel", { threadId: CALLER }))).toEqual({
          cancelled: [],
          notCancelled: [],
        });
        assert.deepStrictEqual(
          touched.filter((entry) => WRITES.test(entry)),
          [],
        );
      }).pipe(Effect.provide(toolsLayer({ filePath, fs })));
      assert.isFalse(yield* exists(filePath));
      assert.deepStrictEqual(yield* (yield* FileSystem.FileSystem).readDirectory(stateDir), []);
    }),
  );

  it.effect("with waits off a damaged store is reported by wait_list and left as it is", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { stateDir, filePath, watched } = yield* workspace;
      yield* seed(filePath, "{ not json");
      yield* Effect.gen(function* () {
        expect(failed(yield* call("wait_for", fileWait(watched("ready"))))).toMatchObject({
          code: "waits_off",
        });
        const listed = succeeded(yield* call("wait_list", {}));
        assert.strictEqual(listed.enabled, false);
        assert.include(String(listed.storeError), "repaired when waits are switched on");
        expect(failed(yield* call("wait_cancel", { threadId: CALLER }))).toMatchObject({
          code: "store_unavailable",
        });
      }).pipe(Effect.provide(toolsLayer({ filePath })));
      assert.strictEqual(yield* fs.readFileString(filePath), "{ not json");
      assert.deepStrictEqual(yield* fs.readDirectory(stateDir), [WAIT_STORE_FILE]);
    }),
  );

  it.effect(
    "wait_for stores the wait for the calling thread; project and parent come from the server",
    () =>
      Effect.gen(function* () {
        yield* TestClock.setTime(NOW);
        const { filePath, watched } = yield* workspace;
        yield* Effect.gen(function* () {
          // Not parameters: a parent from input could send a wake to any thread.
          yield* refusedArguments("wait_for", {
            ...fileWait(watched("ready")),
            parentThreadId: FOREIGN,
            threadId: FOREIGN,
            projectId: OTHER_PROJECT,
          });
          assert.isFalse(yield* exists(filePath));
          const result = succeeded(
            yield* call("wait_for", { ...fileWait(watched("ready")), note: "build done" }),
          );
          assert.strictEqual(result.state, "waiting");
          assert.match(String(result.waitId), /^w_[a-z2-7]{10}$/u);
          assert.strictEqual(
            result.next,
            `End your turn now. One message starting with [wait ${String(result.waitId)}] arrives when this is met, times out or fails. Do not poll.`,
          );
        }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
        const stored = yield* storedWaits(filePath);
        assert.strictEqual(stored.length, 1);
        expect(stored[0]).toMatchObject({
          threadId: CALLER,
          projectId: PROJECT,
          parentThreadId: PARENT,
          note: "build done",
          state: "waiting",
        });
        assert.notInclude(JSON.stringify(stored), FOREIGN);
        assert.notInclude(JSON.stringify(stored), OTHER_PROJECT);
      }),
  );

  it.effect("a thread that is not a subagent stores no parent", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      const shells = new Map([
        [
          CALLER,
          callerShell({
            lineage: { rootThreadId: PARENT, parentThreadId: PARENT, relationshipToParent: "fork" },
          }),
        ],
      ]);
      yield* call("wait_for", fileWait(watched("ready"))).pipe(
        Effect.provide(toolsLayer({ filePath, settings: ON, shells })),
      );
      const [stored] = yield* storedWaits(filePath);
      assert.isDefined(stored);
      assert.isFalse(Object.hasOwn(stored!, "parentThreadId"));
    }),
  );

  it.effect("the same wait twice is one wait, also after a restart", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      const target = fileWait(watched("ready"));
      const first = yield* Effect.gen(function* () {
        const first = succeeded(yield* call("wait_for", target));
        const again = succeeded(yield* call("wait_for", target));
        assert.strictEqual(again.waitId, first.waitId);
        assert.isTrue(again.deduped);
        return first;
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));

      // A new server over the same file: the wait is there and the same call finds it.
      yield* Effect.gen(function* () {
        const listed = succeeded(yield* call("wait_list", {}));
        assert.deepStrictEqual(
          listed.waits?.map((wait) => [wait.waitId, wait.state]),
          [[first.waitId, "waiting"]],
        );
        const again = succeeded(yield* call("wait_for", target));
        assert.strictEqual(again.waitId, first.waitId);
        assert.isTrue(again.deduped);
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      assert.strictEqual((yield* storedWaits(filePath)).length, 1);
    }),
  );

  it.effect("a store write that fails half-way keeps nothing; the next call stores it once", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { stateDir, filePath, watched } = yield* workspace;
      let failing = true;
      const { fs } = yield* instrumentedFs({
        root: stateDir,
        failRenamesTo: filePath,
        failing: () => failing,
      });
      yield* Effect.gen(function* () {
        const target = fileWait(watched("ready"));
        expect(failed(yield* call("wait_for", target))).toMatchObject({
          code: "store_unavailable",
        });
        assert.deepStrictEqual(succeeded(yield* call("wait_list", {})).waits, []);
        failing = false;
        const stored = succeeded(yield* call("wait_for", target));
        assert.strictEqual(stored.state, "waiting");
        assert.isUndefined(stored.deduped);
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON, fs })));
      assert.strictEqual((yield* storedWaits(filePath)).length, 1);
    }),
  );

  it.effect("a store from a newer version is reported and never changed", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { filePath, watched } = yield* workspace;
      const newer = JSON.stringify({ version: 2, waits: [oldRecord()], extra: true });
      yield* seed(filePath, newer);
      yield* Effect.gen(function* () {
        const listed = succeeded(yield* call("wait_list", {}));
        assert.include(String(listed.storeError), "newer version");
        assert.deepStrictEqual(listed.waits, []);
        expect(failed(yield* call("wait_for", fileWait(watched("ready"))))).toMatchObject({
          code: "store_unavailable",
        });
        expect(failed(yield* call("wait_cancel", { waitId: "w_aaaaaaaaaa" }))).toMatchObject({
          code: "store_unavailable",
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      assert.strictEqual(yield* fs.readFileString(filePath), newer);
    }),
  );

  it.effect("a store damaged later is set aside once waits are on; the tools carry on", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const fs = yield* FileSystem.FileSystem;
      const { stateDir, filePath, watched } = yield* workspace;
      yield* seed(filePath, "{ not json");
      yield* Effect.gen(function* () {
        const listed = succeeded(yield* call("wait_list", {}));
        assert.isUndefined(listed.storeError);
        assert.deepStrictEqual(listed.waits, []);
        assert.strictEqual(
          succeeded(yield* call("wait_for", fileWait(watched("ready")))).state,
          "waiting",
        );
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      assert.strictEqual((yield* storedWaits(filePath)).length, 1);
      // The damaged file is kept beside it for a person to look at.
      const kept = (yield* fs.readDirectory(stateDir)).filter((name) => name !== WAIT_STORE_FILE);
      assert.strictEqual(kept.length, 1);
      assert.strictEqual(yield* fs.readFileString(`${stateDir}/${kept[0]}`), "{ not json");
    }),
  );

  it.effect("records an older version wrote list and cancel as they are", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* seed(filePath, { version: 1, waits: [oldRecord()] });
      yield* Effect.gen(function* () {
        const listed = succeeded(yield* call("wait_list", {}));
        assert.deepStrictEqual(
          listed.waits?.map((wait) => [wait.waitId, wait.kind, wait.target]),
          [["w_aaaaaaaaaa", "file", "file /nowhere/ready"]],
        );
        expect(succeeded(yield* call("wait_cancel", { waitId: "w_aaaaaaaaaa" }))).toEqual({
          cancelled: ["w_aaaaaaaaaa"],
          notCancelled: [],
        });
        // Again: nothing left to cancel, and nothing written.
        expect(succeeded(yield* call("wait_cancel", { waitId: "w_aaaaaaaaaa" }))).toEqual({
          cancelled: [],
          notCancelled: [{ waitId: "w_aaaaaaaaaa", state: "cancelled" }],
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      expect(yield* storedWaits(filePath)).toMatchObject([{ state: "cancelled" }]);
    }),
  );

  it.effect("cancel by thread also ends the waits that thread handed to its parent", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* seed(filePath, {
        version: 1,
        waits: [
          oldRecord({
            id: "w_handedover",
            threadId: CALLER,
            via: [CHILD],
            handedOverBecause: "gone",
            key: "1111111111111111", // gitleaks:allow (a fake test value, not a key)
          }),
          oldRecord({
            id: "w_ownwaitaaa",
            threadId: CALLER,
            key: "2222222222222222", // gitleaks:allow (a fake test value, not a key)
          }),
        ],
      });
      const shells = new Map([
        [CALLER, callerShell()],
        [CHILD, { ...McpToolAccessTestkit.liveThreadShell(CHILD), deletedAt: null }],
      ]);
      yield* Effect.gen(function* () {
        expect(succeeded(yield* call("wait_cancel", { threadId: CHILD }))).toEqual({
          cancelled: ["w_handedover"],
          notCancelled: [],
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON, shells })));
      expect(yield* storedWaits(filePath)).toMatchObject([
        { id: "w_handedover", state: "cancelled" },
        { id: "w_ownwaitaaa", state: "waiting" },
      ]);
    }),
  );

  it.effect("waits stay inside the caller's project", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* seed(filePath, {
        version: 1,
        waits: [
          oldRecord({ id: "w_mineaaaaaa", threadId: SIBLING }),
          oldRecord({
            id: "w_foreignaaa",
            threadId: FOREIGN,
            projectId: OTHER_PROJECT,
            key: "3333333333333333", // gitleaks:allow (a fake test value, not a key)
          }),
        ],
      });
      yield* Effect.gen(function* () {
        const project = succeeded(yield* call("wait_list", { scope: "project" }));
        assert.deepStrictEqual(
          project.waits?.map((wait) => wait.waitId),
          ["w_mineaaaaaa"],
        );
        // The default scope is the caller's own thread.
        assert.deepStrictEqual(succeeded(yield* call("wait_list", {})).waits, []);
        expect(failed(yield* call("wait_cancel", { waitId: "w_foreignaaa" }))).toMatchObject({
          code: "not_found",
        });
        expect(succeeded(yield* call("wait_cancel", { threadId: FOREIGN }))).toEqual({
          cancelled: [],
          notCancelled: [],
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      expect(yield* storedWaits(filePath)).toMatchObject([
        { id: "w_mineaaaaaa", state: "waiting" },
        { id: "w_foreignaaa", state: "waiting" },
      ]);
    }),
  );

  it.effect("cancel follows the thread tools' rules for the threads that hold the waits", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* seed(filePath, {
        version: 1,
        waits: [oldRecord({ id: "w_siblingaaa", threadId: SIBLING })],
      });
      // A caller in a narrower mode than the thread that holds the wait.
      const narrow = new Map([
        [CALLER, callerShell({ runtimeMode: "approval-required" })],
        [SIBLING, McpToolAccessTestkit.liveThreadShell(SIBLING)],
      ]);
      yield* Effect.gen(function* () {
        for (const args of [{ waitId: "w_siblingaaa" }, { threadId: SIBLING }]) {
          expect(failed(yield* call("wait_cancel", args))).toMatchObject({
            code: "runtime_mode_escalation_denied",
          });
        }
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON, shells: narrow })));
      // A caller a fenced link started may not change a thread that link did not start.
      const linked = new Map([
        [CALLER, callerShell({ linkOrigin: { sessionId: "link-1", label: "Other machine" } })],
        [SIBLING, McpToolAccessTestkit.liveThreadShell(SIBLING)],
      ]);
      yield* Effect.gen(function* () {
        expect(failed(yield* call("wait_cancel", { waitId: "w_siblingaaa" }))).toMatchObject({
          code: "capability_denied",
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON, shells: linked })));
      expect(yield* storedWaits(filePath)).toMatchObject([{ state: "waiting" }]);

      // The same caller in full access may.
      yield* Effect.gen(function* () {
        expect(succeeded(yield* call("wait_cancel", { waitId: "w_siblingaaa" }))).toEqual({
          cancelled: ["w_siblingaaa"],
          notCancelled: [],
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
    }),
  );

  it.effect("cancel needs exactly one of waitId and threadId", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* Effect.gen(function* () {
        expect(failed(yield* call("wait_cancel", {}))).toMatchObject({ code: "invalid" });
        expect(
          failed(yield* call("wait_cancel", { waitId: "w_aaaaaaaaaa", threadId: CALLER })),
        ).toMatchObject({ code: "invalid" });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      assert.isFalse(yield* exists(filePath));
    }),
  );

  it.effect("a read-only client and a caller outside a thread change nothing", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      yield* seed(filePath, { version: 1, waits: [oldRecord()] });
      const readOnly = scopeOf({
        client: { sessionId: "client-1", label: "Viewer", access: "read-only" },
      });
      const noThread = scopeOf({
        thread: undefined,
        client: { sessionId: "client-2", label: "Outside", access: "full-access" },
      });
      yield* Effect.gen(function* () {
        expect(failed(yield* call("wait_for", fileWait(watched("ready")), readOnly))).toMatchObject(
          { code: "capability_denied" },
        );
        expect(
          failed(yield* call("wait_cancel", { waitId: "w_aaaaaaaaaa" }, readOnly)),
        ).toMatchObject({ code: "capability_denied" });
        // Reading is allowed.
        assert.strictEqual(succeeded(yield* call("wait_list", {}, readOnly)).waits?.length, 1);

        for (const [name, args] of [
          ["wait_for", fileWait(watched("ready"))],
          ["wait_list", {}],
          ["wait_cancel", { waitId: "w_aaaaaaaaaa" }],
        ] as const) {
          assert.isTrue((yield* call(name, args, noThread)).isError, name);
        }
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      expect(yield* storedWaits(filePath)).toMatchObject([{ state: "waiting" }]);
      assert.strictEqual((yield* storedWaits(filePath)).length, 1);
    }),
  );

  it.effect("no tool can change waitFor, waitChecks or agentSlots", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath, watched } = yield* workspace;
      const settingsKeys = ["waitFor", "waitChecks", "agentSlots"];
      for (const tool of Object.values(WaitsToolkit.tools)) {
        const schema = JSON.stringify(Tool.getJsonSchema(tool));
        for (const key of settingsKeys) assert.notInclude(schema, `"${key}"`, tool.name);
      }
      yield* Effect.gen(function* () {
        const settings = yield* ServerSettingsService;
        const before = yield* settings.getSettings;
        yield* refusedArguments("wait_for", {
          ...fileWait(watched("ready")),
          waitFor: { enabled: false },
        });
        yield* refusedArguments("wait_list", { waitChecks: { boom: { argv: ["rm"] } } });
        yield* refusedArguments("wait_cancel", { threadId: CALLER, agentSlots: { enabled: true } });
        assert.deepStrictEqual(yield* settings.getSettings, before);
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON })));
      assert.isFalse(yield* exists(filePath));
    }),
  );

  it.effect(
    "T22: short descriptions, and a deferring client lists them only after tool_search",
    () =>
      Effect.gen(function* () {
        const { filePath } = yield* workspace;
        yield* Effect.gen(function* () {
          const server = yield* McpServer.McpServer;
          const listed = server.tools.map(({ tool }) => ({
            name: tool.name,
            description: tool.description ?? "",
          }));
          assert.deepStrictEqual(
            listed.map((tool) => tool.name),
            ["wait_for", "wait_list", "wait_cancel"],
          );
          const total = listed.reduce((sum, tool) => sum + tool.description.length, 0);
          assert.isAtMost(total, 900);
          const waitFor = listed[0]!.description;
          assert.include(waitFor, "the deadline counts from registration");
          assert.include(waitFor, "[wait <id>]");

          const exposure = McpToolExposure.make(
            () => listed,
            () => Effect.void,
          );
          for (const tool of listed) assert.isFalse(exposure.isListed("session", tool.name));
          const found = yield* exposure.search("session", "wait");
          assert.includeMembers(
            found.map((tool) => tool.name),
            ["wait_for", "wait_list", "wait_cancel"],
          );
          for (const tool of listed) assert.isTrue(exposure.isListed("session", tool.name));
          assert.isFalse(exposure.isListed("other-session", "wait_for"));
        }).pipe(Effect.provide(toolsLayer({ filePath })));
      }),
  );

  // A delivering wait cannot be cancelled, so it must not make a narrower child's own cancel refused.
  it.effect("cancel by own threadId ignores a handed-over wait that is delivering", () =>
    Effect.gen(function* () {
      yield* TestClock.setTime(NOW);
      const { filePath } = yield* workspace;
      yield* seed(filePath, {
        version: 1,
        waits: [
          oldRecord({
            id: "w_handedaaaa",
            threadId: PARENT,
            via: [CALLER],
            handedOverBecause: "stopped",
            state: "delivering",
            key: "4444444444444444", // gitleaks:allow (a fake test value, not a key)
          }),
          oldRecord({ id: "w_ownwaitbbb", threadId: CALLER }),
        ],
      });
      const shells = new Map([
        [CALLER, callerShell({ runtimeMode: "approval-required" })],
        [PARENT, McpToolAccessTestkit.liveThreadShell(PARENT)],
      ]);
      yield* Effect.gen(function* () {
        const result = yield* call("wait_cancel", { threadId: CALLER });
        expect(succeeded(result)).toEqual({
          cancelled: ["w_ownwaitbbb"],
          notCancelled: [{ waitId: "w_handedaaaa", state: "delivering" }],
        });
      }).pipe(Effect.provide(toolsLayer({ filePath, settings: ON, shells })));
    }),
  );
});
