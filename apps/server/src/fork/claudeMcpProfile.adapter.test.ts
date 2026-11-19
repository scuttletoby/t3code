import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import { assert, describe, it } from "@effect/vitest";
import {
  ClaudeSettings,
  ProviderInstanceId,
  ProviderSessionId,
  type ServerSettings,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as McpProviderSessions from "@t3tools/provider-core/server/McpProviderSessions";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import { makeProviderFailure } from "@t3tools/provider-core/server/failure";
import * as SecureRandom from "effect/crypto/SecureRandom";
import * as DateTime from "effect/DateTime";
import * as Effect from "effect/Effect";
import * as FileSystem from "effect/FileSystem";
import * as Layer from "effect/Layer";
import * as Path from "effect/Path";
import * as Schema from "effect/Schema";
import * as Stream from "effect/Stream";

import { makeTurnInput } from "../orchestration-v2/Adapters/AcpAdapterV2.testkit.ts";
import * as ClaudeAdapterV2 from "../orchestration-v2/Adapters/ClaudeAdapterV2.ts";
import * as ServerSettingsService from "../serverSettings.ts";

const MODEL = {
  instanceId: ProviderInstanceId.make(ClaudeAdapterV2.CLAUDE_PROVIDER),
  model: "claude-sonnet-4-6",
};
const CLAUDE_SETTINGS = Schema.decodeSync(ClaudeSettings)({});
const POLICY = ProviderAdapter.ProviderAdapterV2RuntimePolicy.make({
  runtimeMode: "full-access",
  interactionMode: "default",
  cwd: "/workspace",
});

/** Starts one Claude turn with these server settings; records the open and the session's close. */
const startTurn = (
  settings: Partial<Pick<ServerSettings, "mcpProfile" | "mcpProfiles">>,
  statuses: ReadonlyArray<{ readonly name: string; readonly status: "connected" }> | "unexpected",
) =>
  Effect.gen(function* () {
    const opened: Array<ClaudeAdapterV2.ClaudeAgentSdkQueryOptions> = [];
    let closed = 0;
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
        allocateSessionId: Effect.succeed("native-thread-mcp-profile"),
        open: (input) =>
          Effect.sync(() => {
            opened.push(input.options);
            return {
              messages: Stream.never,
              offer: () => Effect.void,
              setModel: () => Effect.void,
              applyFlagSettings: () => Effect.void,
              setPermissionMode: () => Effect.void,
              renameSession: () => Effect.void,
              backgroundTasks: () => Effect.succeed(false),
              mcpServerStatus:
                statuses === "unexpected"
                  ? Effect.die("the default profile asked Claude for its MCP servers")
                  : Effect.succeed(statuses),
              readMcpResource: () => Effect.die("unused"),
              callMcpTool: () => Effect.die("unused"),
              listPermissionRules: Effect.die("unused"),
              stopTask: () => Effect.void,
              interrupt: Effect.void,
              close: Effect.sync(() => void closed++),
            };
          }),
        forkSession: () => Effect.die("unused"),
        subagentLaunchToolUseId: () => Effect.succeed(null),
        subagentModel: () => Effect.succeed(null),
        assertComplete: Effect.void,
      },
    });
    const threadId = ThreadId.make("thread-mcp-profile");
    const runtime = yield* adapter.openSession({
      threadId,
      providerSessionId: ProviderSessionId.make("provider-session-mcp-profile"),
      modelSelection: MODEL,
      runtimePolicy: POLICY,
    });
    const providerThread = yield* runtime.ensureThread({
      threadId,
      modelSelection: MODEL,
      runtimePolicy: POLICY,
    });
    const started = yield* runtime
      .startTurn(
        makeTurnInput({
          threadId,
          providerThread,
          instanceId: MODEL.instanceId,
          runtimePolicy: POLICY,
          now: yield* DateTime.now,
          modelSelection: MODEL,
        }),
      )
      .pipe(Effect.exit);
    const refusal =
      started._tag === "Failure"
        ? makeProviderFailure({ cause: started.cause, class: "provider_error" }).message
        : undefined;
    return { opened, closed, refusal };
  }).pipe(
    Effect.provide(
      Layer.mergeAll(
        IdAllocator.layer,
        McpProviderSessions.layer,
        NodeServices.layer,
        NodeCrypto.layer,
        ServerSettingsService.layerTest(settings),
      ),
    ),
  );

describe("ClaudeAdapterV2 MCP profile", () => {
  it.effect("with no profile set, opens Claude as before and never checks its servers", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* startTurn({}, "unexpected");
      assert.isUndefined(refusal);
      assert.lengthOf(opened, 1);
      assert.notProperty(opened[0], "strictMcpConfig");
      assert.notProperty(opened[0], "managedSettings");
    }),
  );

  it.effect("t3-only opens Claude in strict MCP mode", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* startTurn({ mcpProfile: "t3-only" }, []);
      assert.isUndefined(refusal);
      assert.isTrue(opened[0]?.strictMcpConfig);
    }),
  );

  it.effect("a list opens Claude with only those servers allowed", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* startTurn(
        { mcpProfile: "docs", mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } } },
        [{ name: "docs", status: "connected" }],
      );
      assert.isUndefined(refusal);
      assert.deepStrictEqual(opened[0]?.managedSettings, {
        allowedMcpServers: [{ serverName: "docs" }],
      });
    }),
  );

  it.effect("refuses the turn, without opening Claude, when the profile is missing", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* startTurn({ mcpProfile: "lean" }, []);
      assert.lengthOf(opened, 0);
      assert.include(refusal, 'MCP profile "lean" is not set up, so this thread cannot start');
    }),
  );

  it.effect(
    "refuses the turn and closes Claude when it still loads a server the profile leaves out",
    () =>
      Effect.gen(function* () {
        const { closed, refusal } = yield* startTurn({ mcpProfile: "t3-only" }, [
          { name: "claude.ai Calendar", status: "connected" },
        ]);
        assert.equal(closed, 1);
        assert.include(refusal, "Claude still loaded claude.ai Calendar");
      }),
  );
});
