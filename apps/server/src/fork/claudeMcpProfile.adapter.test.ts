import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import type { HookInput, SDKMessage } from "@anthropic-ai/claude-agent-sdk";
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
import * as Queue from "effect/Queue";
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

type McpSettings = Partial<Pick<ServerSettings, "mcpProfile" | "mcpProfiles">>;
const NATIVE_THREAD = "native-thread-mcp-profile";

/**
 * Runs Claude turns on one thread with these server settings, saving each of `saves` before the
 * next turn; records each open and the sessions' closes. Stops at the first refused turn.
 */
const runTurns = (
  settings: McpSettings,
  statuses: ReadonlyArray<{ readonly name: string; readonly status: "connected" }> | "unexpected",
  saves: ReadonlyArray<McpSettings> = [],
) =>
  Effect.gen(function* () {
    const opened: Array<ClaudeAdapterV2.ClaudeAgentSdkQueryOptions> = [];
    const processes: Array<Queue.Queue<SDKMessage>> = [];
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
        allocateSessionId: Effect.succeed(NATIVE_THREAD),
        open: (input) =>
          Effect.gen(function* () {
            const messages = yield* Queue.unbounded<SDKMessage>();
            processes.push(messages);
            opened.push(input.options);
            return {
              messages: Stream.fromQueue(messages),
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
    const terminals = yield* Queue.unbounded<ProviderAdapter.ProviderAdapterV2Event>();
    yield* runtime.events.pipe(
      Stream.filter((event) => event.type === "turn.terminal"),
      Stream.runForEach((event) => Queue.offer(terminals, event)),
      Effect.forkScoped,
    );
    const settingsService = yield* ServerSettingsService.ServerSettingsService;
    let refusal: string | undefined;
    for (const [index, save] of [undefined, ...saves].entries()) {
      if (save !== undefined) yield* settingsService.updateSettings(save);
      const started = yield* runtime
        .startTurn(
          makeTurnInput({
            threadId,
            providerThread,
            instanceId: MODEL.instanceId,
            runtimePolicy: POLICY,
            now: yield* DateTime.now,
            modelSelection: MODEL,
            ordinal: index + 1,
          }),
        )
        .pipe(Effect.exit);
      if (started._tag === "Failure") {
        refusal = makeProviderFailure({ cause: started.cause, class: "provider_error" }).message;
        break;
      }
      if (index === saves.length) break;
      // Ends the turn so the next one may reuse the live process.
      yield* Queue.offer(processes.at(-1)!, resultFrame(index + 1));
      yield* Queue.take(terminals);
    }
    return { opened, closed, refusal };
  }).pipe(
    Effect.scoped,
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

const resultFrame = (ordinal: number) =>
  ({
    type: "result",
    subtype: "success",
    duration_ms: 1,
    duration_api_ms: 1,
    is_error: false,
    num_turns: 1,
    result: "Done.",
    stop_reason: "end_turn",
    total_cost_usd: 0,
    usage: {
      input_tokens: 1,
      output_tokens: 1,
      cache_creation_input_tokens: 0,
      cache_read_input_tokens: 0,
    },
    modelUsage: {},
    permission_denials: [],
    uuid: `00000000-0000-4000-8000-00000000000${ordinal}`,
    session_id: NATIVE_THREAD,
    terminal_reason: "completed",
  }) as unknown as SDKMessage;

/** What the session's PreToolUse hooks answer for a call to this tool. */
const preToolUse = (
  options: ClaudeAdapterV2.ClaudeAgentSdkQueryOptions | undefined,
  tool: string,
) =>
  Effect.promise(async () => {
    const input = {
      hook_event_name: "PreToolUse",
      tool_name: tool,
      tool_input: {},
      tool_use_id: "toolu_profile",
      session_id: NATIVE_THREAD,
      transcript_path: "/tmp/transcript.jsonl",
      cwd: "/workspace",
    } as unknown as HookInput;
    const answers = [];
    for (const matcher of options?.hooks?.PreToolUse ?? []) {
      for (const hook of matcher.hooks) {
        answers.push(await hook(input, "toolu_profile", { signal: new AbortController().signal }));
      }
    }
    return answers;
  });

describe("ClaudeAdapterV2 MCP profile", () => {
  it.effect("with no profile set, opens Claude as before and never checks its servers", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* runTurns({}, "unexpected");
      assert.isUndefined(refusal);
      assert.lengthOf(opened, 1);
      assert.notProperty(opened[0], "strictMcpConfig");
      assert.notProperty(opened[0], "managedSettings");
      assert.deepStrictEqual(yield* preToolUse(opened[0], "mcp__github__x"), []);
    }),
  );

  it.effect("t3-only opens Claude in strict MCP mode", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* runTurns({ mcpProfile: "t3-only" }, []);
      assert.isUndefined(refusal);
      assert.isTrue(opened[0]?.strictMcpConfig);
    }),
  );

  it.effect("a list opens Claude with only those servers allowed", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* runTurns(
        { mcpProfile: "docs", mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } } },
        [{ name: "docs", status: "connected" }],
      );
      assert.isUndefined(refusal);
      assert.deepStrictEqual(opened[0]?.managedSettings, {
        allowedMcpServers: [{ serverName: "docs" }],
      });
    }),
  );

  it.effect("refuses, at the call, a tool of a server the profile leaves out", () =>
    Effect.gen(function* () {
      const { opened } = yield* runTurns(
        { mcpProfile: "docs", mcpProfiles: { docs: { servers: ["docs"], t3Tools: "all" } } },
        [{ name: "docs", status: "connected" }],
      );
      const [refused] = yield* preToolUse(opened[0], "mcp__github__x");
      assert.deepInclude(refused, {
        hookSpecificOutput: {
          hookEventName: "PreToolUse",
          permissionDecision: "deny",
          permissionDecisionReason: `MCP profile "docs" leaves out the MCP server "github", so T3 refused this call. Claude loaded it anyway: a managed Claude policy on this machine, --mcp-config in the Claude launch arguments, or an agent file's servers can add one. To use it, pick "all" for this thread, or fix the profile in Settings so the profile names it.`,
        },
      });
      assert.deepStrictEqual(yield* preToolUse(opened[0], "mcp__docs__search"), [{}]);
    }),
  );

  it.effect("a narrowed profile opens a new Claude process; the same profile reuses it", () =>
    Effect.gen(function* () {
      const narrowed = yield* runTurns({}, [], [{ mcpProfile: "t3-only" }]);
      assert.isUndefined(narrowed.refusal);
      assert.lengthOf(narrowed.opened, 2);
      assert.isTrue(narrowed.opened[1]?.strictMcpConfig);
      const same = yield* runTurns({ mcpProfile: "t3-only" }, [], [{ mcpProfile: "t3-only" }]);
      assert.isUndefined(same.refusal);
      assert.lengthOf(same.opened, 1);
    }),
  );

  it.effect("refuses the turn, without opening Claude, when the profile is missing", () =>
    Effect.gen(function* () {
      const { opened, refusal } = yield* runTurns({ mcpProfile: "lean" }, []);
      assert.lengthOf(opened, 0);
      assert.include(refusal, 'MCP profile "lean" is not set up, so this thread cannot start');
    }),
  );

  it.effect(
    "refuses the turn and closes Claude when it still loads a server the profile leaves out",
    () =>
      Effect.gen(function* () {
        const { closed, refusal } = yield* runTurns({ mcpProfile: "t3-only" }, [
          { name: "claude.ai Calendar", status: "connected" },
        ]);
        assert.equal(closed, 1);
        assert.include(refusal, "Claude still loaded claude.ai Calendar");
      }),
  );
});
