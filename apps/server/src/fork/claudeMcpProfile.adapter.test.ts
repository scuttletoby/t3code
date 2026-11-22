import * as NodeCrypto from "@effect/platform-node/NodeCrypto";
import * as NodeServices from "@effect/platform-node/NodeServices";
import type { HookInput, SDKMessage } from "@anthropic-ai/claude-agent-sdk";
import { assert, describe, it } from "@effect/vitest";
import {
  ClaudeSettings,
  EnvironmentId,
  ProjectId,
  ProviderInstanceId,
  ProviderSessionId,
  type ServerSettings,
  ServerSettingsError,
  ThreadId,
} from "@t3tools/contracts";
import * as IdAllocator from "@t3tools/provider-core/server/IdAllocator";
import * as McpProviderSessions from "@t3tools/provider-core/server/McpProviderSessions";
import * as ProviderAdapter from "@t3tools/provider-core/server/ProviderAdapter";
import { makeProviderFailure } from "@t3tools/provider-core/server/failure";
import type * as McpProviderSession from "@t3tools/provider-core/server/mcpSession";
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
import { type LineageThread, makeClaudeMcpProfiles } from "./claudeMcpProfile.ts";
import * as T3ToolSets from "./mcpT3ToolSets.ts";

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

type McpSettings = Partial<Pick<ServerSettings, "mcpProfile" | "mcpProfiles" | "modelPolicy">>;
const NATIVE_THREAD = "native-thread-mcp-profile";

/**
 * Runs Claude turns on one thread with these server settings, saving each of `saves` before the
 * next turn; records each open, the sessions' closes and the thread's T3 tool set after each turn.
 * Stops at the first refused turn, unless `pastRefusals`.
 */
const runTurns = (
  settings: McpSettings,
  statuses: ReadonlyArray<{ readonly name: string; readonly status: "connected" }> | "unexpected",
  saves: ReadonlyArray<McpSettings | "unreadable"> = [],
  pastRefusals = false,
) => {
  /** True for a turn whose settings read fails (`"unreadable"`): only that one. */
  let unreadable = false;
  return Effect.gen(function* () {
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
              mcpAuthenticate: () => Effect.die("unused"),
              mcpSubmitOAuthCallbackUrl: () => Effect.die("unused"),
              reconnectMcpServer: () => Effect.die("unused"),
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
    const sets: Array<T3ToolSets.T3ToolSet | undefined> = [];
    for (const [index, save] of [undefined, ...saves].entries()) {
      unreadable = save === "unreadable";
      if (save !== undefined && save !== "unreadable") yield* settingsService.updateSettings(save);
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
      sets.push(T3ToolSets.t3ToolSetFor(threadId));
      if (started._tag === "Failure") {
        refusal ??= makeProviderFailure({ cause: started.cause, class: "provider_error" }).message;
        if (pastRefusals) continue;
        break;
      }
      if (index === saves.length) break;
      // Ends the turn so the next one may reuse the live process.
      yield* Queue.offer(processes.at(-1)!, resultFrame(index + 1));
      yield* Queue.take(terminals);
    }
    return { opened, closed, refusal, sets };
  }).pipe(
    Effect.scoped,
    Effect.provide(
      Layer.mergeAll(
        IdAllocator.layer,
        McpProviderSessions.layer,
        NodeServices.layer,
        NodeCrypto.layer,
        Layer.effect(
          ServerSettingsService.ServerSettingsService,
          Effect.map(ServerSettingsService.ServerSettingsService, (service) =>
            ServerSettingsService.ServerSettingsService.of({
              ...service,
              getSettings: Effect.suspend(() =>
                unreadable
                  ? Effect.fail(
                      new ServerSettingsError({
                        settingsPath: "settings.json",
                        operation: "read-secret",
                        cause: new Error("secret store unreadable"),
                      }),
                    )
                  : service.getSettings,
              ),
            }),
          ),
        ).pipe(Layer.provideMerge(ServerSettingsService.layerTest(settings))),
      ),
    ),
  );
};

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
  tool: unknown,
  /** Replaces the hook input, for a malformed one. */
  rawInput?: object,
  toolInput: object = {},
) =>
  Effect.promise(async () => {
    const input = (rawInput ?? {
      hook_event_name: "PreToolUse",
      tool_name: tool,
      tool_input: toolInput,
      tool_use_id: "toolu_profile",
      session_id: NATIVE_THREAD,
      transcript_path: "/tmp/transcript.jsonl",
      cwd: "/workspace",
    }) as unknown as HookInput;
    const answers = [];
    for (const matcher of options?.hooks?.PreToolUse ?? []) {
      // As Claude Code does: a matcher runs its hooks only for the tools it names.
      if (
        matcher.matcher !== undefined &&
        !(typeof tool === "string" && new RegExp(matcher.matcher).test(tool))
      ) {
        continue;
      }
      for (const hook of matcher.hooks) {
        answers.push(await hook(input, "toolu_profile", { signal: new AbortController().signal }));
      }
    }
    return answers;
  });

/** Hook input for `tool` whose `hook_event_name` is `eventName`, or left out when `omitEvent`. */
const eventCall = (tool: string, eventName: unknown, omitEvent: boolean) => ({
  ...(omitEvent ? {} : { hook_event_name: eventName }),
  tool_name: tool,
  tool_input: {},
  tool_use_id: "toolu_profile",
  session_id: NATIVE_THREAD,
  transcript_path: "/tmp/transcript.jsonl",
  cwd: "/workspace",
});

/** Event names a guard must not trust: missing, undefined, empty, another event, wrong case, a number. */
const UNTRUSTED_EVENTS: ReadonlyArray<[label: string, eventName: unknown, omit: boolean]> = [
  ["missing", undefined, true],
  ["undefined", undefined, false],
  ["empty", "", false],
  ["PostToolUse", "PostToolUse", false],
  ["lower case", "pretooluse", false],
  ["not a string", 7, false],
];

const decision = (answer: unknown) =>
  (answer as { hookSpecificOutput?: { permissionDecision?: string } } | undefined)
    ?.hookSpecificOutput?.permissionDecision;

/**
 * Under every untrusted event name, `refusedTool` is refused and `allowedTool` passes. Each call is
 * made twice: the same call made again gets the same answer, as nothing is remembered between calls.
 */
const expectPerEventName = (
  options: ClaudeAdapterV2.ClaudeAgentSdkQueryOptions | undefined,
  refusedTool: string,
  allowedTool: string,
) =>
  Effect.gen(function* () {
    for (const [label, eventName, omit] of UNTRUSTED_EVENTS) {
      for (const attempt of [1, 2]) {
        const answers = yield* preToolUse(
          options,
          refusedTool,
          eventCall(refusedTool, eventName, omit),
        );
        assert.isTrue(
          answers.some((answer) => decision(answer) === "deny"),
          `${refusedTool}, event ${label}, try ${attempt}: expected a deny`,
        );
        const passed = yield* preToolUse(
          options,
          allowedTool,
          eventCall(allowedTool, eventName, omit),
        );
        assert.isNotEmpty(passed, `${allowedTool}, event ${label}, try ${attempt}: no hook ran`);
        for (const answer of passed) {
          assert.deepStrictEqual(answer, {}, `${allowedTool}, event ${label}, try ${attempt}`);
        }
      }
    }
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

  it.effect("every query carries the model policy's check of Claude's Agent tool once (#128)", () =>
    Effect.gen(function* () {
      const modelPolicy = {
        mode: "enforce",
        allow: [{ driver: "claudeAgent", model: "claude-opus-5-5", efforts: ["high"] }],
      } as never;
      const bare = { servers: "all", t3Tools: "all", builtinTools: "none" } as const;
      // With no profile, and with a profile that adds its own PreToolUse hooks.
      for (const settings of [
        { modelPolicy },
        { modelPolicy, mcpProfile: "t3-only" },
        { modelPolicy, mcpProfile: "bare", mcpProfiles: { bare } },
      ] as ReadonlyArray<McpSettings>) {
        // Runs again: a second turn on the same thread.
        const { opened, refusal } = yield* runTurns(
          settings,
          settings.mcpProfile === "t3-only" ? [] : "unexpected",
          [settings],
        );
        assert.isUndefined(refusal);
        assert.isAtLeast(opened.length, 1);
        const reasons = (answers: ReadonlyArray<unknown>) =>
          answers.map(
            (answer) =>
              (answer as { hookSpecificOutput?: { permissionDecisionReason?: string } })
                .hookSpecificOutput?.permissionDecisionReason,
          );
        for (const options of opened) {
          // Once per query, first, refusing when it cannot answer: never stacked.
          const policyHooks = (options.hooks?.PreToolUse ?? []).filter(
            (matcher) => matcher.matcher === "^(Agent|Task|Skill)$",
          );
          assert.lengthOf(policyHooks, 1, JSON.stringify(settings));
          assert.strictEqual(options.hooks?.PreToolUse?.[0], policyHooks[0]);
          assert.strictEqual(policyHooks[0]?.onFailure, "block");
          const refused = yield* preToolUse(options, "Agent", undefined, {
            model: "sonnet",
            effort: "high",
          });
          assert.lengthOf(
            reasons(refused).filter((text) =>
              text?.startsWith(
                "Model policy refused this pick: claude-sonnet-5-5 (claudeAgent) is in no row.",
              ),
            ),
            1,
            JSON.stringify(settings),
          );
          // Inside the rows: the policy's hook lets it run (builtinTools "none" refuses Agent anyway).
          const inside = yield* preToolUse(options, "Agent", undefined, {
            model: "opus",
            effort: "high",
          });
          assert.isFalse(
            reasons(inside).some((text) => text?.startsWith("Model policy")),
            JSON.stringify(settings),
          );
        }
      }
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

  it.effect(
    "runs again after a refused turn: the next good turn on the same process gets its set back",
    () =>
      Effect.gen(function* () {
        T3ToolSets.__testing.clear();
        const lean = { servers: "all", t3Tools: ["waits"] } as const;
        // Turn 2's settings read fails once, so it is refused and the live process stays; turn 3
        // reads them again: the same plan as the process opened with, so it reuses that process.
        const { opened, refusal, sets } = yield* runTurns(
          { mcpProfile: "lean", mcpProfiles: { lean } },
          [],
          ["unreadable", {}],
          true,
        );
        assert.isDefined(refusal);
        assert.lengthOf(opened, 1);
        assert.deepStrictEqual(
          sets.map((set) =>
            set?._tag === "Groups" && set.groups !== "all" ? [...set.groups] : set?._tag,
          ),
          [["waits"], "Refused", ["waits"]],
        );
      }),
  );

  it.effect(
    'builtinTools "none" opens Claude with no built-in tools and refuses any at the call',
    () =>
      Effect.gen(function* () {
        const bare = { servers: "all", t3Tools: "all", builtinTools: "none" } as const;
        // Narrowing a running thread to it opens a new process: the old one still has its tools.
        const { opened, refusal } = yield* runTurns(
          {},
          [],
          [{ mcpProfile: "bare", mcpProfiles: { bare } }],
        );
        assert.isUndefined(refusal);
        assert.lengthOf(opened, 2);
        assert.notDeepEqual(opened[0]?.tools, []);
        assert.deepStrictEqual(opened[1]?.tools, []);
        assert.includeMembers(opened[1]?.disallowedTools ?? [], [
          "Bash",
          "Read",
          "Write",
          "WebFetch",
        ]);
        const [refused] = yield* preToolUse(opened[1], "Bash");
        assert.deepNestedInclude(refused, {
          "hookSpecificOutput.permissionDecision": "deny",
        });
        assert.include(
          (refused as { hookSpecificOutput: { permissionDecisionReason: string } })
            .hookSpecificOutput.permissionDecisionReason,
          'MCP profile "bare" has builtinTools "none", so T3 refused the built-in tool "Bash"',
        );
        assert.deepStrictEqual(yield* preToolUse(opened[1], "mcp__t3-code__t3_thread_list"), [{}]);
      }),
  );

  it.effect('the builtinTools "none" guard refuses a call it cannot read (fails closed)', () =>
    Effect.gen(function* () {
      const bare = { servers: "all", t3Tools: "all", builtinTools: "none" } as const;
      const { opened } = yield* runTurns({ mcpProfile: "bare", mcpProfiles: { bare } }, []);
      const decision = (answer: unknown) =>
        (answer as { hookSpecificOutput?: { permissionDecision?: string } } | undefined)
          ?.hookSpecificOutput?.permissionDecision;
      // A tool name that is not a string, or none at all.
      for (const tool of [undefined, 42, null, { name: "mcp__x" }]) {
        const [answer] = yield* preToolUse(opened[0], tool);
        assert.strictEqual(decision(answer), "deny", `tool_name ${String(tool)}`);
      }
      // An input that throws while it is read: a hook that threw would let the call through.
      const throwing = {
        hook_event_name: "PreToolUse",
        get tool_name(): string {
          throw new Error("malformed hook input");
        },
      };
      const [answer] = yield* preToolUse(opened[0], undefined, throwing);
      assert.strictEqual(decision(answer), "deny");
      assert.include(
        (answer as { hookSpecificOutput: { permissionDecisionReason: string } }).hookSpecificOutput
          .permissionDecisionReason,
        "could not check this tool call, so it refused it",
      );
    }),
  );

  it.effect(
    'the builtinTools "none" guard passes only MCP tools, whatever the event name says (#946)',
    () =>
      Effect.gen(function* () {
        const bare = { servers: "all", t3Tools: "all", builtinTools: "none" } as const;
        const { opened } = yield* runTurns({ mcpProfile: "bare", mcpProfiles: { bare } }, []);
        yield* expectPerEventName(opened[0], "Bash", "mcp__t3-code__t3_thread_list");
      }),
  );

  it.effect(
    "the server-list guard refuses a left-out server, whatever the event name says (#946)",
    () =>
      Effect.gen(function* () {
        const gh = { servers: ["gh"], t3Tools: "all" } as const;
        const { opened } = yield* runTurns({ mcpProfile: "gh", mcpProfiles: { gh } }, [
          { name: "gh", status: "connected" },
        ]);
        yield* expectPerEventName(opened[0], "mcp__x__Bash", "mcp__gh__list_issues");
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

  // Upstream's "invalidates live-query reuse when MCP credentials rotate" keys overrides no profile
  // planned, which now always differ; this keys planned ones, so it fails if the T3 server's
  // credentials leave the key.
  it.effect(
    "planned turns reuse a process for the same MCP credentials, not after a rotation",
    () =>
      Effect.gen(function* () {
        const threadId = ThreadId.make("thread-mcp-profile-rotation");
        const overridesFor = (authorizationHeader: string) =>
          ClaudeAdapterV2.claudeMcpQueryOverrides({
            mcpSession: {
              environmentId: EnvironmentId.make("environment-mcp-profile"),
              threadId,
              providerSessionId: "mcp-session-mcp-profile",
              providerInstanceId: ProviderInstanceId.make("claudeAgent"),
              endpoint: "http://127.0.0.1:43123/mcp",
              authorizationHeader,
              browserToolsAvailable: true,
            } satisfies McpProviderSession.McpProviderSessionConfig,
            readOnlySandbox: false,
          });
        const thread: LineageThread = {
          id: threadId,
          projectId: ProjectId.make("project-mcp-profile"),
          lineage: { parentThreadId: null, relationshipToParent: null, rootThreadId: threadId },
        };
        const queryPolicy = ClaudeAdapterV2.claudeRuntimeQueryPolicyForRuntimePolicy(POLICY);
        for (const settings of [{}, { mcpProfile: "t3-only" }] satisfies Array<McpSettings>) {
          const plan = yield* makeClaudeMcpProfiles.pipe(
            Effect.provide(ServerSettingsService.layerTest(settings)),
          );
          const keyFor = (authorizationHeader: string) =>
            Effect.gen(function* () {
              const overrides = overridesFor(authorizationHeader);
              yield* plan(thread, overrides);
              return ClaudeAdapterV2.claudeEffectiveQueryPolicyKey(queryPolicy, overrides);
            });
          const first = yield* keyFor("Bearer first-token");
          assert.strictEqual(yield* keyFor("Bearer first-token"), first);
          assert.notStrictEqual(yield* keyFor("Bearer rotated-token"), first);
        }
      }),
  );
});
