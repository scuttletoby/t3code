import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import type { SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, expect, it } from "@effect/vitest";
import {
  ClaudeSettings,
  type OrchestrationV2ProviderThread,
  type OrchestrationV2ThreadProjection,
  ProviderDriverKind,
  ProviderInstanceId,
  ProviderSessionId,
  ProviderThreadId,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as McpProviderSessions from "@t3tools/provider-core/server/McpProviderSessions";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as Exit from "effect/Exit";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Option from "effect/Option";
import * as Path from "effect/Path";
import * as Queue from "effect/Queue";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";
import { Tool } from "effect/ai";

import { OrchestratorToolkit } from "../mcp/toolkits/orchestrator/tools.ts";
import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as ClaudeAdapterV2 from "../orchestration-v2/Adapters/ClaudeAdapterV2.ts";
import * as CommandReceiptStore from "../orchestration-v2/CommandReceiptStore.ts";
import * as Orchestrator from "../orchestration-v2/Orchestrator.ts";
import * as ProviderSessionManager from "../orchestration-v2/ProviderSessionManager.ts";
import * as ThreadCommandExecutor from "../orchestration-v2/ThreadCommandExecutor.ts";
import * as ThreadManagementService from "../orchestration-v2/ThreadManagementService.ts";
import * as SqlitePersistence from "../persistence/Sqlite.ts";
import * as ServerSettings from "../serverSettings.ts";
import * as BackgroundWait from "./backgroundWait.ts";

const tool = OrchestratorToolkit.tools.t3_background_wait;
const encodeSuccess = Schema.encodeUnknownSync(tool.successSchema);
const CLAUDE_SETTINGS = Schema.decodeSync(ClaudeSettings)({});

describe("t3_background_wait parameters", () => {
  const decode = Schema.decodeUnknownExit(tool.parametersSchema);

  it("takes taskId as an alias of taskIds", () => {
    expect(decode({ taskId: "tests" })).toEqual(Exit.succeed({ taskIds: ["tests"] }));
    expect(decode({ taskId: " tests ", waiting: false })).toEqual(
      Exit.succeed({ taskIds: ["tests"], waiting: false }),
    );
    expect(decode({ taskIds: ["a", "b"] })).toEqual(Exit.succeed({ taskIds: ["a", "b"] }));
    expect(decode({ taskIds: ["a"], taskId: "a" })).toEqual(Exit.succeed({ taskIds: ["a"] }));
  });

  it("refuses a call without any task ID, or with too many", () => {
    expect(Exit.isFailure(decode({}))).toBe(true);
    expect(Exit.isFailure(decode({ taskIds: [] }))).toBe(true);
    expect(Exit.isFailure(decode({ taskId: "  " }))).toBe(true);
    const many = Array.from({ length: 20 }, (_, index) => `t${index}`);
    expect(Exit.isSuccess(decode({ taskIds: many }))).toBe(true);
    expect(Exit.isFailure(decode({ taskIds: many, taskId: "one-more" }))).toBe(true);
  });

  it("advertises both fields and requires neither", () => {
    const schema = Tool.getJsonSchema(tool) as {
      readonly properties: Readonly<Record<string, unknown>>;
      readonly required?: ReadonlyArray<string>;
    };
    expect(Object.keys(schema.properties)).toEqual(
      expect.arrayContaining(["taskIds", "taskId", "waiting"]),
    );
    expect(schema.required ?? []).not.toContain("taskIds");
  });

  it("keeps the ended field in the encoded result", () => {
    const encoded = encodeSuccess({
      matched: [],
      unmatched: [],
      ended: [{ taskId: "tests", status: "completed", summary: "Tests passed." }],
    });
    expect(encoded).toEqual({
      matched: [],
      unmatched: [],
      ended: [{ taskId: "tests", status: "completed", summary: "Tests passed." }],
    });
  });
});

const notification = (taskId: string, sessionId: string, summary = "Tests passed.") =>
  ({
    type: "system",
    subtype: "task_notification",
    task_id: taskId,
    status: "completed",
    output_file: `/tmp/tasks/${taskId}.output`,
    summary,
    uuid: "00000000-0000-4000-8000-000000000f71",
    session_id: sessionId,
  }) as unknown as SDKMessage;

/** Runs ThreadManagementService.awaitBackgroundTasks on a thread whose adapter roster holds `live`. */
const serviceWait = (input: {
  readonly nativeThreadId: string;
  readonly live: ReadonlyArray<string>;
  readonly taskIds: ReadonlyArray<string>;
  readonly awaited?: boolean;
}) =>
  Effect.gen(function* () {
    const threadId = ThreadId.make("thread:f7-background-wait");
    const providerThreadId = ProviderThreadId.make("provider-thread:f7-background-wait");
    const providerSessionId = ProviderSessionId.make("provider-session:f7-background-wait");
    const driver = ProviderDriverKind.make("claudeAgent");
    const now = yield* DateTime.now;
    const providerThread = {
      id: providerThreadId,
      driver,
      providerInstanceId: ProviderInstanceId.make("claudeAgent"),
      providerSessionId,
      appThreadId: threadId,
      ownerNodeId: null,
      nativeThreadRef: { driver, nativeId: input.nativeThreadId, strength: "strong" },
      nativeConversationHeadRef: null,
      status: "idle",
      firstRunOrdinal: 1,
      lastRunOrdinal: 1,
      handoffIds: [],
      forkedFrom: null,
      pendingBackgroundTasks: [],
      contextUsage: null,
      nativeMetadata: null,
      createdAt: now,
      updatedAt: now,
    } as OrchestrationV2ProviderThread;
    const runtime = {
      driver,
      providerSessionId,
      awaitBackgroundTasks: (call: { readonly taskIds: ReadonlyArray<string> }) =>
        Effect.succeed({ matched: call.taskIds.filter((taskId) => input.live.includes(taskId)) }),
    } as unknown as ProviderAdapter.ProviderAdapterV2SessionRuntime;
    const layer = ThreadManagementService.layer.pipe(
      Layer.provide(
        Layer.mergeAll(
          Layer.mock(Orchestrator.OrchestratorV2)({
            getThreadRecords: () =>
              Effect.succeed({
                thread: { id: threadId, deletedAt: null, activeProviderThreadId: providerThreadId },
                providerThreads: [providerThread],
              } as unknown as OrchestrationV2ThreadProjection),
          }),
          Layer.mock(ProviderSessionManager.ProviderSessionManagerV2)({
            get: () => Effect.succeed(Option.some(runtime)),
          }),
        ),
      ),
      Layer.provide(ServerSettings.layerTest()),
      Layer.provide(CommandReceiptStore.layer.pipe(Layer.provide(SqlitePersistence.layerMemory))),
    );
    return yield* Effect.gen(function* () {
      const service = yield* ThreadManagementService.ThreadManagementService;
      return yield* service
        .awaitBackgroundTasks({
          threadId,
          taskIds: input.taskIds,
          awaited: input.awaited ?? true,
        })
        .pipe(Effect.result);
    }).pipe(Effect.provide(Layer.merge(layer, ThreadCommandExecutor.layer)));
  });

describe("an awaited task that already ended", () => {
  it.effect("comes back under ended with its status and summary, not refused", () =>
    Effect.gen(function* () {
      const nativeThreadId = "native-f7-ended";
      yield* BackgroundWait.remember(nativeThreadId, notification("finished", nativeThreadId));

      const result = yield* serviceWait({ nativeThreadId, live: [], taskIds: ["finished"] });

      expect(result).toMatchObject({
        success: {
          matched: [],
          unmatched: [],
          ended: [{ taskId: "finished", status: "completed", summary: "Tests passed." }],
        },
      });
    }),
  );

  it.effect("sits next to live and unknown tasks in one answer", () =>
    Effect.gen(function* () {
      const nativeThreadId = "native-f7-mixed";
      yield* BackgroundWait.remember(nativeThreadId, notification("finished", nativeThreadId));

      const result = yield* serviceWait({
        nativeThreadId,
        live: ["tests"],
        taskIds: ["tests", "finished", "never-started"],
      });

      expect(result).toMatchObject({
        success: {
          matched: ["tests"],
          unmatched: ["never-started"],
          ended: [{ taskId: "finished", status: "completed" }],
        },
      });
    }),
  );

  it.effect("is only known on the thread it ran on", () =>
    Effect.gen(function* () {
      yield* BackgroundWait.remember(
        "native-f7-other",
        notification("elsewhere", "native-f7-other"),
      );

      const result = yield* serviceWait({
        nativeThreadId: "native-f7-here",
        live: [],
        taskIds: ["elsewhere"],
      });

      expect(result).toMatchObject({
        failure: { _tag: "ThreadManagementBackgroundWaitError", reason: "unmatched" },
      });
    }),
  );
});

const MODEL = {
  instanceId: ProviderInstanceId.make(ClaudeAdapterV2.CLAUDE_PROVIDER),
  model: "claude-sonnet-4-6",
};
const POLICY = ProviderAdapter.ProviderAdapterV2RuntimePolicy.make({
  runtimeMode: "full-access",
  interactionMode: "default",
  cwd: "/workspace",
});
const NATIVE_THREAD = "native-f7-adapter";

describe("a task that ends during the wait", () => {
  it.effect("is recorded by the Claude adapter and reported once", () =>
    Effect.gen(function* () {
      const processes: Array<Queue.Queue<SDKMessage>> = [];
      const adapter = yield* ClaudeAdapterV2.makeClaudeAdapterV2({
        instanceId: ClaudeAdapterV2.CLAUDE_DEFAULT_INSTANCE_ID,
        settings: CLAUDE_SETTINGS,
        environment: {},
        attachmentsDir: yield* (yield* FileSystem.FileSystem).makeTempDirectoryScoped(),
        fileSystem: yield* FileSystem.FileSystem,
        path: yield* Path.Path,
        random: yield* SecureRandom.SecureRandom,
        idAllocator: yield* IdAllocator.IdAllocatorV2,
        queryRunner: {
          allocateSessionId: Effect.succeed(NATIVE_THREAD),
          open: () =>
            Effect.gen(function* () {
              const messages = yield* Queue.unbounded<SDKMessage>();
              processes.push(messages);
              return {
                messages: Stream.fromQueue(messages),
                offer: () => Effect.void,
                setModel: () => Effect.void,
                applyFlagSettings: () => Effect.void,
                setPermissionMode: () => Effect.void,
                renameSession: () => Effect.void,
                backgroundTasks: () => Effect.succeed(false),
                mcpServerStatus: Effect.succeed([]),
                readMcpResource: () => Effect.die("unused"),
                mcpAuthenticate: () => Effect.die("unused"),
                mcpSubmitOAuthCallbackUrl: () => Effect.die("unused"),
                reconnectMcpServer: () => Effect.die("unused"),
                callMcpTool: () => Effect.die("unused"),
                listPermissionRules: Effect.die("unused"),
                stopTask: () => Effect.void,
                interrupt: Effect.void,
                close: Effect.void,
              };
            }),
          forkSession: () => Effect.die("unused"),
          subagentLaunchToolUseId: () => Effect.succeed(null),
          subagentModel: () => Effect.succeed(null),
          assertComplete: Effect.void,
        },
      });
      const threadId = ThreadId.make("thread-f7-adapter");
      const runtime = yield* adapter.openSession({
        threadId,
        providerSessionId: ProviderSessionId.make("provider-session-f7-adapter"),
        modelSelection: MODEL,
        runtimePolicy: POLICY,
      });
      const providerThread = yield* runtime.ensureThread({
        threadId,
        modelSelection: MODEL,
        runtimePolicy: POLICY,
      });
      const rosters = yield* Queue.unbounded<ReadonlyArray<string>>();
      yield* runtime.events.pipe(
        Stream.runForEach((event) =>
          event.type === "provider_thread.updated"
            ? Queue.offer(
                rosters,
                (event.providerThread.pendingBackgroundTasks ?? []).map((task) => task.taskId),
              )
            : Effect.void,
        ),
        Effect.forkScoped,
      );
      /** The next roster the adapter reports that `matches`. */
      const nextRoster = Effect.fn("nextRoster")(function* (
        matches: (taskIds: ReadonlyArray<string>) => boolean,
      ) {
        while (true) {
          const taskIds = yield* Queue.take(rosters);
          if (matches(taskIds)) return taskIds;
        }
      });

      yield* runtime.startTurn(
        makeTurnInput({
          threadId,
          providerThread,
          instanceId: MODEL.instanceId,
          runtimePolicy: POLICY,
          now: yield* DateTime.now,
          modelSelection: MODEL,
          ordinal: 1,
        }),
      );
      const messages = processes.at(-1)!;
      yield* Queue.offer(messages, {
        type: "system",
        subtype: "task_started",
        task_id: "tests",
        tool_use_id: "toolu_f7_tests",
        description: "Run the tests",
        is_backgrounded: true,
        task_type: "local_bash",
        uuid: "00000000-0000-4000-8000-000000000f72",
        session_id: NATIVE_THREAD,
      } as unknown as SDKMessage);
      yield* nextRoster((taskIds) => taskIds.includes("tests"));
      const awaitBackgroundTasks = runtime.awaitBackgroundTasks;
      assert.isDefined(awaitBackgroundTasks);
      const marked = yield* awaitBackgroundTasks!({
        providerThread,
        taskIds: ["tests"],
        awaited: true,
      });
      assert.deepStrictEqual(marked.matched, ["tests"]);
      assert.deepStrictEqual(yield* BackgroundWait.read(providerThread, ["tests"], ["tests"]), []);

      yield* Queue.offer(messages, notification("tests", NATIVE_THREAD, "  42 passed.  "));
      yield* nextRoster((taskIds) => !taskIds.includes("tests"));
      const after = yield* awaitBackgroundTasks!({
        providerThread,
        taskIds: ["tests"],
        awaited: true,
      });
      assert.deepStrictEqual(after.matched, []);

      assert.deepStrictEqual(yield* BackgroundWait.read(providerThread, ["tests", "tests"], []), [
        { taskId: "tests", status: "completed", summary: "42 passed." },
      ]);
      assert.deepStrictEqual(yield* BackgroundWait.read(providerThread, ["tests"], []), []);
    }).pipe(
      Effect.scoped,
      Effect.provide(
        Layer.mergeAll(
          IdAllocator.layer,
          McpProviderSessions.layer,
          NodeServices.layer,
          NodeCrypto.layer,
          ServerSettings.layerTest(),
        ),
      ),
    ),
  );
});
